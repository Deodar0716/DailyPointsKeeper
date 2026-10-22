# -*- coding: utf-8 -*-
"""WorkBuddy (Tencent) — daily credit check-in.

Observed API (verified 2026-10, client 5.7.x, same backend family as CodeBuddy):

    POST https://{domain}/v2/billing/meter/checkin-activity-status
    POST https://{domain}/v2/billing/meter/daily-checkin          body {}
    headers: Authorization: Bearer <token>, X-User-Id, X-Domain
    envelope: {"code": 0, "data": {...}}; code 0 = ok

⚠ The interesting detail: **"already checked in today" comes back as
HTTP 400 with `{"code":10001}`**, not as HTTP 200. Judge the business code in
the body; if you require HTTP 200 you will report every idempotent re-run as a
failure. (`api()` below reads 4xx bodies for exactly this reason.)

Credential: `%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\
workbuddy-desktop.info`. Since 2026-10 the `auth.accessToken` field is wrapped:

    {"$wbEncrypted": 1, "envelope": base64(JSON{suite,keyId,nonce,authTag,ciphertext})}

The at-rest key is **not on disk** — it is compiled into the client's own
Electron binary and reached through
`process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()`,
which returns `{version:1, atRestSecretKey, atRestDeveloperPublicKey}`;
`key = sha256(atRestSecretKey)`, and
`sha256(key).hex[:16]` must equal `envelope.keyId` (a handy deterministic oracle
for checking any candidate key). AES-256-GCM with a fixed AAD.

Rather than reimplement that, this provider *asks the client to do it*: it runs
the app's own Electron as a Node interpreter (`ELECTRON_RUN_AS_NODE=1`) with
`tools/wb_desktop_session.cjs`, feeds the envelope on stdin and reads the token
back from a private pipe. No key ever lands on disk, nothing is printed, and the
helper zeroes its buffers. Helper is third-party MIT code — see LICENSE/NOTICE.

The plaintext metadata next to the envelope (`expiresAt`, `lastRefreshTime`) is
enough to know whether the session is alive, so the token file is re-read every
run and follows whatever the client refreshed.
"""

import base64
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request

from .. import config, discover
from ..result import ALREADY, FRESH
from .base import ApiError, CredError, Provider, Snapshot

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class WorkBuddyProvider(Provider):
    key = "workbuddy"
    label = "WorkBuddy"
    unit = "积分"

    def __init__(self, cfg, log):
        super(WorkBuddyProvider, self).__init__(cfg, log)
        local = os.environ.get("LOCALAPPDATA", "")
        self.session_file = os.path.join(
            local, "CodeBuddyExtension", "Data", "Public", "auth",
            "workbuddy-desktop.info")
        cfg_helper = config.get(cfg, "workbuddy.helper") or ""
        root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        if cfg_helper and not os.path.isabs(cfg_helper):
            cfg_helper = os.path.join(root, cfg_helper)   # config may use a relative path
        self.helper = cfg_helper or os.path.join(root, "tools", "wb_desktop_session.cjs")
        self._ssl = ssl.create_default_context()

    # ---------------- credential ----------------

    def _read_session(self):
        if not os.path.isfile(self.session_file):
            raise CredError("找不到 WorkBuddy 会话文件 %s（先打开并登录客户端）"
                            % self.session_file)
        try:
            d = json.load(open(self.session_file, encoding="utf-8"))
        except Exception as e:
            raise CredError("WorkBuddy 会话文件读不出 JSON：%r" % e)
        auth = d.get("auth") or {}
        acc = d.get("account") or {}
        tok = auth.get("accessToken")
        uid = acc.get("uid") or (d.get("accounts") or [{}])[0].get("uid")
        domain = auth.get("domain") or "www.codebuddy.cn"
        if not tok or not uid:
            raise CredError("会话文件里缺 accessToken 或 uid，请重新登录 WorkBuddy")
        return tok, uid, domain, auth.get("expiresAt") or ""

    def _electron(self):
        for cand in (os.environ.get("WORKBUDDY_ELECTRON"),
                     config.get(self.cfg, "workbuddy.electron") or ""):
            if cand and os.path.isfile(cand):
                return cand
        found = discover.find_exe("WorkBuddy.exe", ("WorkBuddy", "CodeBuddy"))
        if not found:
            raise CredError("找不到 WorkBuddy.exe——它要当解密器用，"
                            "装好后在 config.json 的 workbuddy.electron 里写死路径也行")
        return found

    def _decrypt(self, wrapper):
        if not isinstance(wrapper, dict):        # older builds stored it in plaintext
            return str(wrapper)
        if not os.path.isfile(self.helper):
            raise CredError("缺 tools/wb_desktop_session.cjs（%s）" % self.helper)
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        try:
            p = subprocess.run([self._electron(), self.helper, "--private-pipe"],
                               input=json.dumps(wrapper).encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=env, timeout=30, creationflags=_CREATE_NO_WINDOW)
        except Exception as e:
            raise CredError("调起 WorkBuddy 自带 Electron 失败：%r" % e)
        tok = (p.stdout or b"").decode("utf-8", "replace").strip()
        if p.returncode != 0 or not tok:
            raise CredError("解密失败（WorkBuddy 可能升级换了字段格式），rc=%s" % p.returncode)
        if not tok.startswith("eyJ") or tok.count(".") != 2:
            raise CredError("解出来的不是 JWT，客户端存储格式变了")
        return tok

    @staticmethod
    def jwt_exp(tok):
        try:
            part = tok.split(".")[1]
            part += "=" * (-len(part) % 4)
            return (json.loads(base64.urlsafe_b64decode(part)) or {}).get("exp") or 0
        except Exception:
            return 0

    def token(self):
        wrapper, uid, domain, exp_at = self._read_session()
        tok = self._decrypt(wrapper)
        e2 = self.jwt_exp(tok)
        if e2 and e2 < time.time():
            raise CredError("本机登录令牌已过期（%s）——打开一次 WorkBuddy 让它续期"
                            % time.strftime("%Y-%m-%d %H:%M", time.localtime(e2)))
        return tok, uid, domain, e2

    # ---------------- api ----------------

    def _api(self, path, tok, uid, domain, body=None, tries=3):
        url = "https://%s/v2/billing/meter%s" % (domain, path)
        data = json.dumps(body or {}).encode("utf-8")
        headers = {"Authorization": "Bearer %s" % tok, "X-User-Id": str(uid),
                   "X-Domain": domain, "Content-Type": "application/json",
                   "Accept": "application/json, text/plain, */*", "User-Agent": _UA}
        last = (0, {"_detail": "未执行"})
        for i in range(tries):
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
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
                except Exception:
                    parsed = None
                # 4xx bodies carry the real business code — see the module docstring.
                if isinstance(parsed, dict) and "code" in parsed:
                    parsed["_http"] = e.code
                    return e.code, parsed
                last = (e.code, {"_detail": detail[:180]})
                if e.code in (401, 403) or 400 <= e.code < 500:
                    return last
            except Exception as e:
                last = (0, {"_detail": repr(e)})
            if i < tries - 1:
                time.sleep(3)
        raise ApiError("%s 连续失败：%s" % (path, last))

    def _status(self, tok, uid, domain):
        code, j = self._api("/checkin-activity-status", tok, uid, domain)
        if code in (401, 403):
            raise CredError("鉴权被拒 HTTP %s，请重新登录 WorkBuddy" % code)
        if code != 200 and "code" not in j:
            raise ApiError("status HTTP %s %s" % (code, str(j.get("_detail"))[:160]))
        return j.get("data") or {}

    # ---------------- provider contract ----------------

    def check(self):
        tok, uid, domain, _ = self.token()
        d = self._status(tok, uid, domain)
        name = d.get("activity_name")
        if not d:
            return Snapshot(claimable=False, done=False,
                            summary="状态接口没返回活动数据（活动可能已结束或账号不可领）")
        if d.get("today_checked_in"):
            return Snapshot(claimable=False, done=True,
                            summary="今日已签（活动=%s 累计=%s 连签=%s）"
                                    % (name, d.get("total_credits"), d.get("streak_days")),
                            detail={"activity": name, "total": d.get("total_credits"),
                                    "streak": d.get("streak_days")})
        return Snapshot(claimable=True, done=False,
                        summary="可领：活动=%s 累计=%s 连签=%s"
                                % (name, d.get("total_credits"), d.get("streak_days")),
                        detail={"activity": name, "end_time": d.get("end_time")})

    def do_claim(self):
        tok, uid, domain, _ = self.token()
        code, j = self._api("/daily-checkin", tok, uid, domain)
        biz = j.get("code")
        if biz == 10001:                       # HTTP 400 + code 10001 = 已签，幂等
            return Snapshot(outcome=ALREADY, summary="服务端判定今日已签（幂等）",
                            detail={"code": biz})
        if biz != 0:
            raise ApiError("daily-checkin HTTP %s code=%s %s"
                           % (code, biz, json.dumps(j, ensure_ascii=False)[:160]))
        d3 = self._status(tok, uid, domain)    # re-read; never trust the write code alone
        gained = d3.get("today_credit") or 0
        return Snapshot(outcome=FRESH, gained=gained,
                        summary="已领到手 +%s %s（累计 %s）"
                                % (gained, self.unit, d3.get("total_credits")),
                        detail={"total": d3.get("total_credits"),
                                "activity": d3.get("activity_name"),
                                "streak": d3.get("streak_days")})

    def doctor(self):
        rows = [("session file", os.path.isfile(self.session_file), self.session_file),
                ("helper", os.path.isfile(self.helper), self.helper)]
        try:
            rows.append(("electron", True, self._electron()))
        except Exception as e:
            rows.append(("electron", False, str(e)))
        try:
            wrapper, uid, domain, _ = self._read_session()
            rows.append(("account", True, "uid=%s… domain=%s" % (str(uid)[:8], domain)))
        except Exception as e:
            rows.append(("account", False, str(e)))
        return rows
