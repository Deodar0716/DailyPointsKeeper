# -*- coding: utf-8 -*-
"""MiniMax Code — 每日签到领积分.

Observed API (verified 2026-10-22 against client 3.1.4, app id
`com.minimax.agent.cn`, region `cn`):

    GET  https://agent.minimax.cn/minimax-cloud/api/v1/signin/status
    POST https://agent.minimax.cn/minimax-cloud/api/v1/signin/claim   body {}
    envelope: {"base_resp": {"status_code": 0, "status_msg": "ok"}, "data": {...}}
    — `status_code == 0` is the only success signal (Tencent-style envelope).

Enums, read out of the client's own bundle (`app.asar` →
`/out/_next/static/chunks/app/(pages)/(mavis)/archon/page-*.js`) rather than guessed:

    day.status      1=Upcoming  2=Claimable  3=Claimed  4=Disabled
    claim_result    1=Claimed (points landed)  2=AlreadyClaimed (idempotent)
    panel rules     at most one day with status==2, exactly one with is_today

So "did I already get today's" is `is_today` day's `status == 3`, and repeating
the claim is safe (`claim_result=2`).

⚠ Those 15 query parameters are **required**: without them the server answers
`status_code=1406010011 / invalid timezone_id`. The machine-specific ones
(`uuid`, `device_id`, `user_id`, screen size) are provably optional, so this
provider never reads LevelDB and never needs a device fingerprint.

No request signature is needed either: `x-signature` and `yy` are computed by
the renderer (`md5(ts + "I*7Cf%WZ#S&%1RlZJ&C2" + body)` and
`md5(encodeURIComponent(pathWithQuery) + "_" + body + md5(str(ts_ms)) + "ooui")`),
but a plain `Authorization: Bearer` call succeeds without them. Kept here so
nobody re-derives them; if MiniMax ever starts enforcing them, implement
`_params()` order exactly — parameter order changes the hash.

Credential: **plaintext OAuth2 on disk**, no encryption (unlike the other two
providers).

    %USERPROFILE%\\.minimax\\auth\\<env>\\<region>\\<clientId>\\auth.json
      {schemaVersion:1, records:{"<service>\\0<account>": {
          accessToken(mmoat_), refreshToken(mmort_), tokenType:"Bearer",
          clientId:"mcode-public", scopes:["agent.default"],
          audience:"agent-backend", expiresAtMs, generation, loginEpoch}}}

⚠ The access token lives **one hour** (`expires_in=3600`) and the token endpoint
**rotates the refresh token every time**. Therefore a script that refreshes MUST
write the new pair back, otherwise the desktop client's own stored refresh token
is dead and the user gets logged out of MiniMax Code. Writing back is done with:

* the client's own lock — `@mavis/oauth-core` uses `proper-lockfile` on
  `<namespace>/auth.lock`, i.e. it creates the directory `auth.lock.lock`;
  we create the same one, treat >30 s without a heartbeat as stale, and if we
  cannot take it we wait for the client to finish and use its token instead;
* re-reading inside the lock, so we never refresh a token the client already
  rotated;
* `os.replace` on a temp file, keeping their exact format (`indent=2` + trailing
  newline) and every other field (`generation` especially — `auth-core` compares
  `credential.generation` against `auth-state.json` and treats a mismatch as
  "not logged in");
* rollback only when the file still contains what *we* wrote, so a concurrent
  client write is never clobbered.

Reference for those invariants: `keeper/providers/README-oauth-core.md`-level
detail lives in docs/mechanisms.md.
"""

import glob
import json
import os
import shutil
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config
from ..result import ALREADY, FRESH, UNAVAILABLE
from .base import ApiError, CredError, Provider, Snapshot

CLIENT_ID = "mcode-public"
SCOPE = "agent.default"
AUDIENCE = "agent-backend"
_STATUS_NAME = {1: "未到", 2: "可领", 3: "已领", 4: "不可用"}
_LOCK_STALE_MS = 30 * 1000
_LOCK_WAIT_S = 12.0

# the 15 params the endpoint insists on (order matters only for a signature we
# do not send); everything else the client adds is optional
_BASE_PARAMS = (("device_platform", "web"), ("biz_id", "3"), ("app_id", "3001"),
                ("version_code", "22201"), ("timezone_offset", 28800),
                ("is_desktop", "1"), ("sys_language", "zh"), ("lang", "zh"),
                ("os_name", "Windows"), ("browser_name", "Chrome"),
                ("browser_language", "zh-CN"), ("browser_platform", "Win32"),
                ("client", "desktop"))


class MiniMaxCodeProvider(Provider):
    key = "minimax_code"
    label = "MiniMax Code"
    unit = "积分"

    def __init__(self, cfg, log):
        super(MiniMaxCodeProvider, self).__init__(cfg, log)
        self.host = config.get(cfg, "minimax.api_host") or "https://agent.minimax.cn"
        self.token_url = (config.get(cfg, "minimax.token_url")
                          or "https://account.minimax.cn/oauth2/token")
        self.margin_ms = int(config.get(cfg, "minimax.refresh_margin_minutes", 5)) * 60 * 1000
        self.version = config.get(cfg, "minimax.desktop_version") or ""
        self._ssl = ssl.create_default_context()
        self._ua = ("MiniMaxAgent Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) MiniMax/3.1.4 "
                    "Chrome/148.0.7778.280 Safari/537.36")

    # ---------------- credential file ----------------

    def cred_dir(self):
        override = os.environ.get("MINIMAX_AUTH_DIR", "")
        if override and os.path.isfile(os.path.join(override, "auth.json")):
            return override
        home = os.path.expanduser("~")
        for env in ("prod", "staging", "dev"):
            for region in ("cn", "en"):
                d = os.path.join(home, ".minimax", "auth", env, region, CLIENT_ID)
                if os.path.isfile(os.path.join(d, "auth.json")):
                    return d
        raise CredError("找不到 ~/.minimax/auth/**/%s/auth.json——先打开并登录 MiniMax Code"
                        % CLIENT_ID)

    def _read_cred(self, d):
        path = os.path.join(d, "auth.json")
        raw = open(path, encoding="utf-8").read()
        doc = json.loads(raw)
        recs = doc.get("records") or {}
        if not recs:
            raise CredError("auth.json 里没有 records，客户端存储结构变了")
        key = sorted(recs.keys())[0]
        rec = recs[key]
        for field in ("accessToken", "refreshToken", "expiresAtMs"):
            if not rec.get(field):
                raise CredError("auth.json 缺 %s 字段" % field)
        if rec.get("tokenType") != "Bearer" or rec.get("clientId") != CLIENT_ID:
            raise CredError("auth.json 的 tokenType/clientId 与预期不符，客户端可能改了鉴权方案")
        return raw, doc, key

    def _write_raw(self, d, text):
        self._atomic_write(os.path.join(d, "auth.json"), text)

    def _atomic_write(self, path, text):
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), int(time.time() * 1000))
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ---------------- proper-lockfile compatible mutex ----------------

    def _lock_dir(self, d):
        return os.path.join(d, "auth.lock.lock")

    def _lock_acquire(self, d):
        lp = self._lock_dir(d)
        t0 = time.time()
        while True:
            try:
                os.mkdir(lp)
                try:
                    with open(os.path.join(lp, "payload"), "w", encoding="utf-8") as f:
                        f.write("%d\n" % os.getpid())
                except Exception:
                    pass
                return lp
            except FileExistsError:
                stale = True
                try:
                    up = os.path.join(lp, "payload")
                    mtime = os.path.getmtime(up if os.path.exists(up) else lp)
                    stale = (time.time() - mtime) * 1000 > _LOCK_STALE_MS
                except Exception:
                    stale = True
                if stale:
                    shutil.rmtree(lp, ignore_errors=True)
                    continue
                if time.time() - t0 > _LOCK_WAIT_S:
                    return ""
                time.sleep(0.15)

    @staticmethod
    def _lock_release(lp):
        if lp:
            shutil.rmtree(lp, ignore_errors=True)

    # ---------------- token ----------------

    def _request_new_token(self, refresh_token):
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": CLIENT_ID, "scope": SCOPE, "audience": AUDIENCE}).encode("utf-8")
        req = urllib.request.Request(self.token_url, data=body, method="POST", headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": self._ua})
        try:
            with urllib.request.urlopen(req, timeout=25, context=self._ssl) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            raise CredError("oauth2/token HTTP %s %s（多半是登录已失效，请重新登录 MiniMax Code）"
                            % (e.code, detail))
        except Exception as e:
            raise CredError("oauth2/token 请求失败：%r" % e)

    def ensure_token(self):
        """Return a bearer that is valid now, refreshing + writing back if needed."""
        d = self.cred_dir()
        raw, doc, key = self._read_cred(d)
        rec = doc["records"][key]
        if rec["expiresAtMs"] - int(time.time() * 1000) >= self.margin_ms:
            return rec["accessToken"]

        lp = self._lock_acquire(d)
        new_access = ""
        try:
            if not lp:
                # MiniMax itself is refreshing. Wait for its write rather than
                # minting a competing token pair.
                for _ in range(int(_LOCK_WAIT_S / 0.5)):
                    time.sleep(0.5)
                    try:
                        r2 = self._read_cred(d)[1]["records"][key]
                    except Exception:
                        continue
                    if r2["expiresAtMs"] - int(time.time() * 1000) >= self.margin_ms:
                        self.log("[minimax] 客户端正在续期，直接用它的令牌")
                        return r2["accessToken"]
                raise CredError("拿不到 MiniMax 的凭据锁，本次不续期（下个时段自动重试）")

            raw, doc, key = self._read_cred(d)      # re-read under lock
            rec = doc["records"][key]
            if rec["expiresAtMs"] - int(time.time() * 1000) >= self.margin_ms:
                return rec["accessToken"]
            used = rec["refreshToken"]
            j = self._request_new_token(used)
            new_access = j.get("access_token") or ""
            new_refresh = j.get("refresh_token") or used
            expires_in = int(j.get("expires_in") or 0)
            if not new_access or expires_in <= 0:
                raise CredError("token 端点返回缺字段：%s"
                                % json.dumps({k: v for k, v in j.items()
                                              if "token" not in k}, ensure_ascii=False)[:160])
            rec["accessToken"] = new_access
            rec["refreshToken"] = new_refresh
            rec["expiresAtMs"] = int((time.time() + expires_in) * 1000)
            rec["tokenType"] = "Bearer"
            self._atomic_write(os.path.join(d, "auth.json"),
                               json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
            self.log("[minimax] 令牌已续期 +%d 分钟并原子写回（refresh 轮换=%s）"
                     % (expires_in // 60, "是" if new_refresh != used else "否"))
            return new_access
        except Exception as e:
            # Roll back only if the file still holds exactly what we wrote.
            try:
                cur = self._read_cred(d)[1]["records"].get(key, {})
                if new_access and cur.get("accessToken") == new_access:
                    self._write_raw(d, raw)
                    self.log("[minimax] 续期后校验失败，已回滚 auth.json：%r" % e)
                else:
                    self.log("[minimax] 续期异常且凭据文件已被客户端更新，不动它：%r" % e)
            except Exception as e2:
                self.log("[minimax] 续期失败且回滚未成功（%r / %r）——"
                         "若 MiniMax 掉登录请手动重新登录一次" % (e, e2))
            if isinstance(e, (CredError, ApiError)):
                raise
            raise CredError("续期失败：%r" % e)
        finally:
            self._lock_release(lp)

    # ---------------- params ----------------

    def _desktop_version(self):
        if self.version:
            return self.version
        local = os.environ.get("LOCALAPPDATA", "")
        best = ""
        for p in sorted(glob.glob(os.path.join(
                local, "MiniMax", "installer-logs", "nsis-com.minimax.agent.*-*.log"))):
            for seg in os.path.basename(p)[:-4].split("-"):
                if seg[:1].isdigit() and "." in seg:
                    best = seg
        return best or "0.0.0"

    def _params(self):
        try:
            off = -(time.altzone if (time.daylight and time.localtime().tm_isdst)
                    else time.timezone)
        except Exception:
            off = 28800
        pairs = list(_BASE_PARAMS)
        pairs.insert(4, ("unix", int(time.time() * 1000)))
        pairs.insert(7, ("desktop_version", self._desktop_version()))
        out = []
        for k, v in pairs:
            if k == "timezone_offset" and off > 0:
                v = off
            out.append((k, v))
        return out

    # ---------------- api ----------------

    def _api(self, path, tok, method="GET", body=None, tries=3):
        url = "%s%s?%s" % (self.host, path, urllib.parse.urlencode(self._params()))
        data = json.dumps(body if body is not None else {}).encode("utf-8") \
            if method == "POST" else None
        headers = {"Authorization": "Bearer %s" % tok, "Content-Type": "application/json",
                   "Accept": "application/json, text/plain, */*", "User-Agent": self._ua,
                   "Origin": self.host, "Referer": self.host + "/"}
        last = (0, {"_detail": "未执行"})
        for i in range(tries):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=25, context=self._ssl) as r:
                    return r.status, json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")
                except Exception:
                    detail = ""
                try:
                    parsed = json.loads(detail)
                    if isinstance(parsed, dict):
                        parsed["_http"] = e.code
                        return e.code, parsed
                except Exception:
                    pass
                last = (e.code, {"_detail": detail[:180]})
                if e.code in (401, 403) or 400 <= e.code < 500:
                    return last
            except Exception as e:
                last = (0, {"_detail": repr(e)})
            if i < tries - 1:
                time.sleep(3)
        raise ApiError("%s 连续失败：%s" % (path, last))

    @staticmethod
    def _biz(j):
        br = (j or {}).get("base_resp")
        if isinstance(br, dict) and "status_code" in br:
            return int(br["status_code"]), str(br.get("status_msg") or "")
        return None, ""

    @staticmethod
    def _panel(data):
        days = [x for x in ((data or {}).get("days") or []) if isinstance(x, dict)]
        today_day = next((x for x in days if x.get("is_today")), None)
        claimed = sum(1 for x in days if x.get("status") == 3)
        return today_day, days, claimed

    # ---------------- provider contract ----------------

    def check(self):
        tok = self.ensure_token()
        code, j = self._api("/minimax-cloud/api/v1/signin/status", tok)
        biz, msg = self._biz(j)
        if biz == 1406010011:
            raise ApiError("公共参数被拒（%s）——客户端大概改了参数格式，见 docs/mechanisms.md" % msg)
        if code in (401, 403):
            raise CredError("鉴权被拒 HTTP %s，请重新登录 MiniMax Code" % code)
        if biz != 0:
            raise ApiError("status HTTP %s biz=%s %s" % (code, biz, msg or str(j)[:160]))
        day, days, claimed = self._panel(j.get("data"))
        if not day:
            return Snapshot(claimable=False, done=False,
                            summary="面板里没有 is_today 档位（活动可能已下线）",
                            detail={"scene": (j.get("data") or {}).get("scene")})
        st = day.get("status")
        detail = {"day_no": day.get("day_no"), "points": day.get("points"),
                  "claimed_days": claimed, "cycle": len(days),
                  "status": st, "status_name": _STATUS_NAME.get(st, st)}
        if st == 3:
            return Snapshot(claimable=False, done=True,
                            summary="今日已领（第 %s/%s 天 %s 分）"
                                    % (day.get("day_no"), len(days), day.get("points")),
                            detail=detail)
        if st == 2:
            return Snapshot(claimable=True, done=False,
                            summary="可领：第 %s/%s 天 %s 分"
                                    % (day.get("day_no"), len(days), day.get("points")),
                            detail=detail)
        return Snapshot(claimable=False, done=False,
                        summary="今日档位状态=%s（%s），本次不领"
                                % (st, _STATUS_NAME.get(st, "?")), detail=detail)

    def do_claim(self):
        tok = self.ensure_token()
        code, j = self._api("/minimax-cloud/api/v1/signin/claim", tok,
                            method="POST", body={})
        biz, msg = self._biz(j)
        d = (j.get("data") or {}) if isinstance(j, dict) else {}
        if biz != 0 or d.get("claim_result") not in (1, 2):
            raise ApiError("claim HTTP %s biz=%s %s %s"
                           % (code, biz, msg, json.dumps(j, ensure_ascii=False)[:160]))
        fresh = d.get("claim_result") == 1
        _, j3 = self._api("/minimax-cloud/api/v1/signin/status", tok)
        _day, days3, claimed3 = self._panel(j3.get("data"))
        pts = d.get("points")
        expire = time.strftime("%Y-%m-%d", time.localtime((d.get("expire_at_ms") or 0) / 1000))
        detail = {"day_no": d.get("day_no"), "cycle": len(days3), "claimed_days": claimed3,
                  "total": pts, "claim_id": d.get("claim_id"), "expire": expire}
        if not fresh:
            return Snapshot(outcome=ALREADY,
                            summary="服务端判定已领过（幂等，第 %s 天）" % d.get("day_no"),
                            detail=detail)
        return Snapshot(outcome=FRESH, gained=pts,
                        summary="已领到手 +%s %s（本轮第 %s/%s 天，已签 %d 天，%s 到期）"
                                % (pts, self.unit, d.get("day_no"), len(days3), claimed3, expire),
                        detail=detail)

    def doctor(self):
        rows = []
        try:
            d = self.cred_dir()
            rows.append(("auth.json", True, os.path.join(d, "auth.json")))
            try:
                rec = list(self._read_cred(d)[1]["records"].values())[0]
                left = (rec["expiresAtMs"] / 1000 - time.time()) / 60
                rows.append(("token lifetime", True,
                             ("剩余 %.0f 分钟" % left) if left > 0 else
                             ("已过期 %.0f 分钟 → 本次运行会自动续期并写回（令牌只活 1 小时）"
                              % -left)))
            except Exception as e:
                rows.append(("record schema", False, str(e)))
        except Exception as e:
            rows.append(("auth.json", False, str(e)))
        rows.append(("desktop_version", bool(self._desktop_version()),
                     self._desktop_version()))
        rows.append(("api host", True, self.host))
        return rows
