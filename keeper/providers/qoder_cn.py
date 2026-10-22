# -*- coding: utf-8 -*-
"""Qoder CN — "每天领 100 Credits".

Observed API (verified 2026-10 on the CN client, app id `com.qodercn.app`):

    GET  {base}/sash/api/v1/me/campaigns
         → campaigns[].actionType == "CLAIM_BENEFIT", claimStatus CLAIMABLE|CLAIMED
    POST {base}/sash/api/v1/me/campaigns/{campaignId}/claim   body {}
         → status == "CLAIMED"; the response carries `replayed: true` when the
           server had already granted it ⇒ calling twice is safe by design
    GET  {base}/api/v2/quota/usage → recursive search for addOnQuota.remaining

Credential: the client stores its session in `%APPDATA%\\com.qodercn.app.stable\\
auth.v1.dat` as a Chromium `v10` container (AES-256-GCM). The master key is
`Local State → os_crypt.encrypted_key`, base64, minus the leading `DPAPI`, then
unprotected with the *current Windows user's* DPAPI. Consequence worth knowing:
the script needs no login of its own and automatically follows whatever the
client refreshed — but it can only run as the same Windows user, and cannot run
under a different account or on a scheduled task with "run whether user is
logged on or not" (Service SIDs have no DPAPI master key).

Two gotchas encoded below:
* the campaign key's date part does **not** identify the cycle (a key dated day N
  was still the live one on day N+1). Judge only `claimStatus`.
* the day's campaign is published **after** the stated 10:00 reset, sometimes
  much later, so "no CLAIM_BENEFIT" during the day means "retry later", not
  "broken". runner.py turns that into exit 4 + a last-slot-only alert.
"""

import base64
import ctypes
import ctypes.wintypes as wt
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

_UMID_MARKER = os.path.join("resources", "umid", "runtime-info.exe")
_NEEDLES = ("Qoder",)
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class QoderCNProvider(Provider):
    key = "qoder_cn"
    label = "Qoder"
    unit = "Credits"

    def __init__(self, cfg, log):
        super(QoderCNProvider, self).__init__(cfg, log)
        appdata = os.environ.get("APPDATA", "")
        self.auth_dir = os.path.join(appdata, "com.qodercn.app.stable")
        self.auth_file = os.path.join(self.auth_dir, "auth.v1.dat")
        self.local_state = os.path.join(self.auth_dir, "Local State")
        self.machine_token = os.path.join(appdata, "QoderCN", "SharedClientCache",
                                          "cache", "machine_token.json")
        self.base = (os.environ.get("DPK_QODER_API_BASE")
                     or config.get(cfg, "qoder.api_base")
                     or "https://openapi.qoder.com.cn")
        self._ssl = ssl.create_default_context()

    # ---------------- credential ----------------

    def _dpapi(self, data):
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32

        class Blob(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(data, len(data))
        src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        dst = Blob()
        if not crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None,
                                          0, ctypes.byref(dst)):
            raise CredError("CryptUnprotectData failed (err=%d) — this task must "
                            "run as the interactive Windows user"
                            % ctypes.GetLastError())
        try:
            return ctypes.string_at(dst.pbData, dst.cbData)
        finally:
            kernel32.LocalFree(dst.pbData)

    def _master_key(self):
        try:
            with open(self.local_state, encoding="utf-8", errors="replace") as f:
                ek = json.load(f)["os_crypt"]["encrypted_key"]
        except Exception as e:
            raise CredError("读不到 %s 里的 os_crypt.encrypted_key：%r" % (self.local_state, e))
        raw = base64.b64decode(ek)
        if raw[:5] != b"DPAPI":
            raise CredError("encrypted_key 前缀不是 DPAPI，客户端加密方案变了")
        return self._dpapi(raw[5:])

    def _decrypt(self, key, data):
        if not data.startswith((b"v10", b"v11", b"v20")):
            raise CredError("auth.v1.dat 不是 v10 容器，客户端存储格式变了")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise CredError("缺 cryptography 库：%s -m pip install cryptography"
                            % _python_exe())
        # 'v10' + 12-byte nonce + ciphertext + 16-byte tag, empty AAD
        return AESGCM(key).decrypt(data[3:15], data[15:], b"")

    def token(self):
        if not os.path.isfile(self.auth_file):
            raise CredError("找不到 %s——先打开并登录 Qoder 客户端" % self.auth_file)
        try:
            d = json.loads(self._decrypt(self._master_key(),
                                         open(self.auth_file, "rb").read())
                           .decode("utf-8", "replace"))
        except CredError:
            raise
        except Exception as e:
            raise CredError("解 auth.v1.dat 失败：%r" % e)
        tok = d.get("token") or ""
        if not tok:
            raise CredError("auth.v1.dat 里没有 token 字段，客户端格式变了")
        return tok, d.get("expiresAt") or ""

    def _fingerprint(self):
        try:
            with open(self.machine_token, encoding="utf-8") as f:
                j = json.load(f)
            if j.get("token"):
                return {"Cosy-MachineToken": j.get("token", ""),
                        "Cosy-MachineCode": j.get("code", ""),
                        "Cosy-MachineType": j.get("type", ""),
                        "Cosy-MachineOS": j.get("os", ""),
                        "Cosy-MachineId": j.get("id", "")}
        except Exception:
            pass
        return self._fingerprint_from_exe()

    def _install_dir(self):
        cfg_dir = os.environ.get("DPK_QODER_INSTALL_DIR") or \
            config.get(self.cfg, "qoder.install_dir") or ""
        if cfg_dir and os.path.isdir(cfg_dir):
            return cfg_dir
        if cfg_dir:      # explicit but wrong is a configuration mistake, say so
            raise CredError("config qoder.install_dir 指向的目录不存在：%s" % cfg_dir)
        return discover.find_file(_UMID_MARKER, _NEEDLES)

    def _fingerprint_from_exe(self):
        install = self._install_dir()
        exe = os.path.join(install, _UMID_MARKER) if install else ""
        if not os.path.isfile(exe):
            # The CN endpoint has been observed to answer without fingerprints,
            # so this is a degradation, not a hard failure.
            self.log("[qoder] 没有 machine_token.json 也找不到 runtime-info.exe，"
                     "改为不带设备指纹请求")
            return {}
        payload = json.dumps({"account": ""}) + " "
        for args in (["x", "--account-stdin"], ["--account-stdin"]):
            try:
                p = subprocess.run([exe] + args, input=payload, capture_output=True,
                                   text=True, timeout=25, creationflags=_CREATE_NO_WINDOW)
                out = (p.stdout or "").strip()
                if not out:
                    continue
                d = json.loads(out.splitlines()[0].split(" ", 1)[0])
                if d.get("machineToken") or d.get("machineCode"):
                    return {"Cosy-MachineToken": d.get("machineToken", ""),
                            "Cosy-MachineCode": d.get("machineCode", ""),
                            "Cosy-MachineType": d.get("machineType", ""),
                            "Cosy-MachineOS": d.get("machineOS", ""),
                            "Cosy-MachineId": d.get("machineId") or d.get("machineCode", "")}
            except Exception:
                continue
        raise CredError("runtime-info.exe 调用失败（设备指纹拿不到）")

    def _client_version(self):
        cfg_v = config.get(self.cfg, "qoder.client_version") or ""
        if cfg_v:
            return cfg_v
        try:
            v = discover.newest_version_dir(self._install_dir())
            if v:
                return v
        except Exception:
            pass
        return "0.0.0"

    def _headers(self, tok, fp=None):
        h = {"Authorization": "Bearer %s" % tok, "User-Agent": "Qoder",
             "Cosy-ClientType": "10", "Cosy-Version": self._client_version(),
             "Accept": "application/json", "Content-Type": "application/json"}
        h.update(fp or {})
        return h

    # ---------------- api ----------------

    def _api(self, path, headers, method="GET", body=None, tries=3):
        data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
        last = None
        for i in range(tries):
            req = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=25, context=self._ssl) as r:
                    return r.status, json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    detail = ""
                if e.code in (401, 403) or 400 <= e.code < 500:
                    return e.code, {"_detail": detail}
                last = (e.code, {"_detail": detail})
            except Exception as e:
                last = (0, {"_detail": repr(e)})
            if i < tries - 1:
                time.sleep(3)
        raise ApiError("请求 %s 连续失败：%s" % (path, last))

    @staticmethod
    def _campaigns(payload):
        out = []
        for c in (payload or {}).get("campaigns") or []:
            if isinstance(c, dict):
                out.append({"key": c.get("campaignKey"), "id": c.get("campaignId"),
                            "action": c.get("actionType"),
                            "claim": (c.get("claimStatus") or "").upper(),
                            "benefit": json.dumps(c.get("benefit"), ensure_ascii=False)[:120]})
        return out

    @staticmethod
    def _remaining(obj):
        if isinstance(obj, dict):
            aq = obj.get("addOnQuota")
            if isinstance(aq, dict) and isinstance(aq.get("remaining"), (int, float)):
                return aq["remaining"]
            for v in obj.values():
                r = QoderCNProvider._remaining(v)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = QoderCNProvider._remaining(v)
                if r is not None:
                    return r
        return None

    # ---------------- provider contract ----------------

    def check(self):
        tok, _ = self.token()
        fp = self._fingerprint()
        code, d = self._api("/sash/api/v1/me/campaigns", self._headers(tok, fp))
        if code in (401, 403):
            raise CredError("登录令牌被拒 HTTP %s，请在客户端重新登录一次" % code)
        if code != 200:
            raise ApiError("campaigns HTTP %s %s" % (code, json.dumps(d, ensure_ascii=False)[:160]))
        camps = [c for c in self._campaigns(d) if c["action"] == "CLAIM_BENEFIT"]
        if not camps:
            return Snapshot(claimable=False, done=False,
                            summary="接口没有 CLAIM_BENEFIT 活动（多半是当天还没发布）",
                            detail={"campaigns": 0})
        claimable = [c for c in camps if c["claim"] == "CLAIMABLE"]
        if claimable:
            return Snapshot(claimable=True, done=False,
                            summary="可领：%s" % claimable[0]["key"],
                            detail={"campaign_key": claimable[0]["key"],
                                    "campaign_id": claimable[0]["id"]})
        return Snapshot(claimable=False, done=True,
                        summary="本周期已领（%s）" % camps[0]["key"],
                        detail={"campaign_key": camps[0]["key"]})

    def do_claim(self):
        snap = self.check()
        cid = snap.detail.get("campaign_id")
        if not cid:
            raise ApiError("领取前复查没找到可领活动")
        tok, _ = self.token()
        code, res = self._api("/sash/api/v1/me/campaigns/%s/claim" % cid,
                              self._headers(tok, self._fingerprint()),
                              method="POST", body={})
        if code != 200 or (res.get("status") or "").upper() != "CLAIMED":
            raise ApiError("claim HTTP %s %s" % (code, json.dumps(res, ensure_ascii=False)[:160]))
        _, quota = self._api("/api/v2/quota/usage", self._headers(tok, {}))
        bal = self._remaining(quota)
        amount = 100
        fresh = not res.get("replayed")
        summary = "%s +%s Credits（%s）资源包剩余 %s" % (
            "已领到手" if fresh else "此前已领（服务端幂等重放）", amount,
            snap.detail.get("campaign_key"), bal)
        return Snapshot(claimable=False, done=fresh, gained=amount if fresh else None,
                        outcome=FRESH if fresh else ALREADY,
                        summary=summary, detail={"balance": bal,
                                                  "campaign_key": snap.detail.get("campaign_key"),
                                                  "replayed": bool(res.get("replayed"))})

    def doctor(self):
        rows = [("auth.v1.dat", os.path.isfile(self.auth_file), self.auth_file),
                ("Local State", os.path.isfile(self.local_state), self.local_state)]
        try:
            import cryptography  # noqa: F401
            rows.append(("cryptography", True, "已安装"))
        except ImportError:
            rows.append(("cryptography", False, "pip install cryptography（只这个适配器要用）"))
        rows.append(("fingerprint cache", os.path.isfile(self.machine_token),
                     self.machine_token if os.path.isfile(self.machine_token)
                     else "缺 → 回落 runtime-info.exe / 无指纹请求"))
        rows.append(("api base", True, config.get(self.cfg, "qoder.api_base")))
        return rows


def _python_exe():
    return os.environ.get("DPK_PYTHON") or os.path.join(
        os.path.dirname(os.__file__), "python.exe")
