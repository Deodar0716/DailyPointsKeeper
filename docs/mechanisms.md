# Mechanisms

What each vendor actually does, as observed on a machine the author owns in
October 2026. Everything here is the reason a line of code exists; when a
provider starts failing, this is the file to re-verify.

Common shape of the whole project: read the login the desktop client already
stored locally → call the same HTTP endpoint the client's own UI calls → let the
server decide whether today is claimable → record, optionally notify. No GUI,
no image matching, no model in the loop, so a locked screen cannot break it.

---

## 1. Qoder CN — "每天领 100 Credits"

**Endpoints** (`https://openapi.qoder.com.cn`):

```
GET  /sash/api/v1/me/campaigns
     campaigns[]: {campaignKey, campaignId, actionType, claimStatus, benefit}
     the reward row is the one with actionType == "CLAIM_BENEFIT"
     claimStatus: CLAIMABLE = not yet | CLAIMED = already got it

POST /sash/api/v1/me/campaigns/{campaignId}/claim     body {}
     → status == "CLAIMED"; `replayed: true` when it had already been granted
       ⇒ idempotent by design, an extra slot can never double-grant

GET  /api/v2/quota/usage
     → walk the tree for addOnQuota.remaining (the balance we report)
```

**Headers**: `Authorization: Bearer <token>`, `User-Agent: Qoder`,
`Cosy-ClientType: 10`, `Cosy-Version: <client version>` and the optional
`Cosy-Machine*` fingerprint family.

**Credential**: `%APPDATA%\com.qodercn.app.stable\auth.v1.dat` is a Chromium
`v10` container (`v10` + 12-byte nonce + ciphertext + 16-byte tag, AES-256-GCM).
The key is `%APPDATA%\com.qodercn.app.stable\Local State → os_crypt.encrypted_key`
(base64, strip the 5-byte `DPAPI` prefix, then `CryptUnprotectData` for the
current Windows user). Decoded JSON holds `token` (`dt-`), `refreshToken`
(`drt-`), `expiresAt`, `refreshTokenExpiresAt`, `user.*`.

Consequences: the task must run **as the same interactive Windows user** (a
`RunLevel`/service account has no such DPAPI master key); and because the client
rewrites this file whenever it refreshes the session, re-reading it every run is
the entire "keep me logged in" strategy — there is nothing to renew by hand.

**Fingerprint**: `%APPDATA%\QoderCN\SharedClientCache\cache\machine_token.json`
is plaintext and maps 1:1 onto the five `Cosy-Machine*` headers; the fallback is
the client's own `resources/umid/runtime-info.exe`. Observed: the CN endpoint
answers identically **without** the fingerprint, while the international
endpoint is reported to need it — so the header family is sent when available
and never a hard requirement.

**Two traps that cost a full day each**:

1. *The campaign key's date is not the cycle date.* Two consecutive days both
   carried the same date inside the key (`act-YYYYMMDD-250` on day N and
   `act-YYYYMMDD-603` on day N+1). Filter only on
   `claimStatus == "CLAIMABLE"`, never on the key.
2. *The cycle rolls at 10:00 (UTC+8), not midnight.* Before 10:00 the endpoint
   still shows the **previous** cycle as `CLAIMED`. Any "did I already run
   today?" logic keyed on the calendar date will therefore skip the real claim.
   Which is why `keeper/state.py` exists but is only ever written.
   Also: the day's campaign is sometimes published *late* — an empty
   `CLAIM_BENEFIT` list at 10:05 is normal, hence exit 4 and a last-slot-only
   alert.

---

## 2. WorkBuddy (Tencent) — daily credits

**Endpoints** (`https://<domain>/v2/billing/meter`, domain from the session file,
default `www.codebuddy.cn`):

```
POST /checkin-activity-status   → data {activity_name, today_checked_in,
                                        total_credits, today_credit,
                                        streak_days, end_time}
POST /daily-checkin             body {}  → code 0 = granted this call
```

Headers: `Authorization: Bearer <jwt>`, `X-User-Id`, `X-Domain`.

**The one that fools people**: `daily-checkin` answers **"already checked in
today" as HTTP 400 with `{"code":10001}`**, not HTTP 200. Judge the business
code in the body — if you require `status == 200`, every idempotent re-run looks
like an outage (and, worse, an "unavailable" result can swallow a real failure).

**Credential**: `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\
workbuddy-desktop.info`. Since 2026-10 the `auth.accessToken` field is wrapped:

```
{"$wbEncrypted":1,"envelope":base64({suite,keyId,nonce,authTag,ciphertext})}
```

The at-rest key is **not stored on disk**. It is only reachable inside the
client's own Electron binary through
`process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()`,
which returns `{version:1, atRestSecretKey, atRestDeveloperPublicKey}`;
`key = sha256(atRestSecretKey)`; AES-256-GCM with AAD
`'WB-AAD\0' + 0x01 + str('WBEV1') + str('sym-v1') + u32(1) + str(keyId) + [2,0,0]`
(`str(x)` = 4-byte big-endian length + UTF-8).

The design move that makes this tractable: **do not extract the key — ask the
program that holds it to decrypt for you**
(`ELECTRON_RUN_AS_NODE=1 WorkBuddy.exe tools/wb_desktop_session.cjs
--private-pipe`, envelope on stdin, token on a private stdout pipe, refusing to
run against a TTY). `expiresAt` and friends stay in plaintext next to the
envelope, so liveness can be checked without decrypting anything.

**Reusable trick**: `deriveAtRestKeyId(key) = sha256(key).hex[:16]` is a
deterministic *verification oracle*. Any candidate key can be checked
instantly against `envelope.keyId`, which is the fastest way to prove that a
key is genuinely absent from disk instead of merely un-obvious.

Note: `~/.workbuddy/connectors/<uid>/.master.key` protects *connector*
credentials (mail/docs/drive), not the login token. Do not go down that path.

---

## 3. MiniMax Code — 每日签到

**Endpoints** (`https://agent.minimax.cn`), Tencent-style envelope
`{base_resp:{status_code,status_msg}, data:{...}}`, success only when
`status_code == 0`:

```
GET  /minimax-cloud/api/v1/signin/status
     → data {scene, days:[{day_no, points, status, is_today, bonus_points}]}
POST /minimax-cloud/api/v1/signin/claim     body {}  (send it; two chars matter)
     → data {claim_id, claim_result, day_no, points, expire_at_ms, panel}
GET  /minimax-cloud/api/v1/signin/config    (copy and images only)
```

Enums, read out of the client's own bundle rather than guessed:

```
day.status      1=Upcoming  2=Claimable  3=Claimed  4=Disabled
claim_result    1=Claimed   2=AlreadyClaimed        ⇒ re-claiming is safe
panel rules     at most one day status==2, exactly one is_today
observed cycle  400/400/400/1000/400/400/1000, points valid 30 days
```

**Required query parameters** — omit them and the answer is
`status_code=1406010011 / invalid timezone_id`:

```
device_platform=web & biz_id=3 & app_id=3001 & version_code=22201 & unix=<ms>
& timezone_offset=28800 & is_desktop=1 & desktop_version=<client version>
& sys_language=zh & lang=zh & os_name=Windows & browser_name=Chrome
& browser_language=zh-CN & browser_platform=Win32 & client=desktop
```

Measured: `uuid`, `device_id`, `user_id`, screen size, `device_memory`,
`cpu_core_num` are all optional ⇒ this provider needs no device fingerprint and
never reads LevelDB.

`x-signature` and `yy` headers exist in the renderer but are **not enforced**:

```
x-signature = md5(unix_seconds + "I*7Cf%WZ#S&%1RlZJ&C2" + body)
yy          = md5(encodeURIComponent(pathWithQueryString) + "_" + body
                  + md5(str(unix_millis)) + "ooui")
```

If enforcement ever appears, parameter *order* is part of the hash — copy
`_params()` order from the client's own log line (see "where to look" below).

**Credential**: `%USERPROFILE%\.minimax\auth\<env>\<region>\<clientId>\auth.json`
— **plaintext OAuth2**, no encryption:

```json
{"schemaVersion":1,"records":{"<service>\0<accountHash>": {
   "accessToken":"mmoat_…","refreshToken":"mmort_…","tokenType":"Bearer",
   "clientId":"mcode-public","scopes":["agent.default"],
   "audience":"agent-backend","expiresAtMs":…,"generation":1,"loginEpoch":"…"}}}
```

Renew: `POST https://account.minimax.cn/oauth2/token`, form-encoded
`grant_type=refresh_token&refresh_token=…&client_id=mcode-public&scope=agent.default&audience=agent-backend`
→ `expires_in=3600`, **and a new refresh_token every time**.

The three invariants you must respect when writing that file back (all enforced
in `keeper/providers/minimax_code.py`):

1. **Write back, or you log the user out.** Rotation means the client's stored
   refresh token dies the moment you refresh.
2. **Take the client's lock.** `@mavis/oauth-core` locks `<namespace>/auth.lock`
   via `proper-lockfile`, i.e. it creates the directory `auth.lock.lock`, with a
   30-second stale window. Use the same directory; if you cannot get it, wait for
   the client's write instead of minting a competing pair.
3. **Preserve the format and `generation`.** Files are `JSON.stringify(…, null,
   2) + "\n"`, written by temp-file + rename. `auth-core` compares
   `credential.generation` to `auth-state.json`; a mismatch is treated as
   "not logged in". `auth-state.json` deliberately holds no tokens — leave it
   alone.

---

## Where to look when a vendor changes something

* The clients are Electron apps: the app bundle (`resources/app.asar`, or
  `Contents/Resources/app.asar` on macOS) is a JSON header + concatenated files.
  Parse the header (first `{` after the pickle, ~16-byte offset), then read any
  file by `base + offset`. Enums and endpoints live in the renderer chunks under
  `out/_next/static/chunks/**`, main-process logic under `dist/main/**`.
* The desktop clients usually log their own requests. MiniMax Code writes
  `%APPDATA%\MiniMax\logs\api-<MM-DD>.log` with one `[IPC Proxy]` line per
  request: full URL **with every query parameter**, request headers (secrets
  `[REDACTED]`, but `yy` and `x-timestamp` come through verbatim), response
  headers and `baseRespStatusCode`. Grep that before reaching for a proxy tool —
  you get the real request, including parameter order.
* `sha256(key).hex[:16] == envelope.keyId` style checks are verification oracles;
  use them before assuming a secret is unreachable.
