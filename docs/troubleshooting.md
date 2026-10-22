# Troubleshooting

Work down the list; each step is a command with a readable answer, not a guess.

## 0. Three questions, in this order

```
python daily_points_keeper.py probe      # what does each server say right now?
type var\logs\keeper.log                 # what did the last run decide?
schtasks /Query /TN DailyPointsKeeper /V /FO LIST | findstr /I "Last Task Result Next Run State"
```

`probe` is read-only — it never claims and never notifies — so it is always safe
to run while you debug. If `probe` says "已领" the money is there and the rest is
about messaging, not claiming.

## 1. "It did not run at all"

| symptom | cause | fix |
|---|---|---|
| `Last Task Result = 267009` | still running | wait; it exits in seconds unless a provider is refreshing a token |
| result `0x1` / `Task Scheduler failed to start` | interpreter path moved (Python upgrade) | re-run `install.bat`, or `register_task.ps1 -PythonExe <new path>` |
| no run and machine was asleep/off | slot missed | `StartWhenAvailable` makes it up on next logon — you must log in as the same user |
| never runs | task registered for another user | `register_task.ps1` uses `$env:USERDOMAIN\$env:USERNAME`; check `schtasks /Query /TN DailyPointsKeeper /V` `Run As User` |
| runs but nothing happens | `config.json` lists no providers | `... list` prints the enabled set |

Retries: **do not expect the scheduler to retry a non-zero exit.** It only
restarts tasks that failed to start or were killed. Extra slots in the day are
the retry mechanism, and re-running is free because every provider re-reads the
server.

## 2. Exit code 2 — credential

| provider | meaning | what to do |
|---|---|---|
| `qoder_cn` | `auth.v1.dat` / `Local State` missing, or `CryptUnprotectData` failed | log into the Qoder client once. If the task runs as another user or with "run whether user is logged on or not", DPAPI can't work — it must be the interactive user |
| `qoder_cn` | "缺 cryptography 库" | `python -m pip install -r requirements.txt` **with the same interpreter the task uses** (PATH `python` may be a different venv) |
| `workbuddy` | 解密失败 rc=2 | the client updated and changed the field format; re-check `tools/wb_desktop_session.cjs` against the current bundle, or read docs/mechanisms.md §2 |
| `workbuddy` | 找不到 WorkBuddy.exe | set `workbuddy.electron` in `config.json` |
| `minimax_code` | `oauth2/token HTTP 40x` | the login is dead — open MiniMax Code and re-login (the 1-hour token means the client is the renewer) |
| any | "令牌已过期" | open the client once; every provider re-reads whatever it wrote |

## 3. Exit code 3 — API

* `status_code=1406010011 / invalid timezone_id` (MiniMax): the required query
  parameters changed. Grep `%APPDATA%\MiniMax\logs\api-*.log` for `signin/status`
  and copy the current parameter list — order matters if a signature is enforced.
* `campaigns HTTP 401` (Qoder): token expired mid-run; re-login in the client.
* `daily-checkin HTTP 400` (WorkBuddy) is **not** an error if the body says
  `code:10001`; that is "already claimed". If your own code treats 4xx as failure,
  read the body first — see `keeper/providers/workbuddy.py::_api`.
* repeated 3s-retry timeouts: your network or a VPN, not the vendor.

## 4. "I claimed it but got no message"

1. `type var\notify_pending.json` — if the item is there, delivery failed and it
   will be retried at the next slot. That is the designed behaviour: a claim is
   never re-done because its notification failed.
2. `type var\logs\notify.log` and look for `NOT-DELIVERED` plus the reason line.
   Only the JSON `error` field counts as a failure, so the reason is real.
3. `python daily_points_keeper.py notify-test` — exercises the channel alone.
4. `hermes` channel and a WeChat target: bot-initiated pushes are refused until
   you send the bot one message (`sendmessage rate limited; cooldown active for
   30.0s`). That is a *rate window*, not a session expiry: waiting does nothing,
   an inbound message fixes it. Configure a second target (a QQ bot has no such
   window) so `notify.hermes.targets` has a fallback.
5. `webhook` channel: check the URL's host is one of the shaped ones in
   `keeper/notify.py`; anything else gets a generic `{"title","text"}` JSON body.

## 5. "Too many messages in the morning"

Failure alerts are gated by `alert.last_slot_minutes`: nothing is announced
before that minute of the vendor day. Keep it at (or just below) your **last**
slot, and the five slots mean silence-then-at-most-one-alert.
"Nothing claimable yet" (exit 4) is deliberately never alerted early — vendors
publish late.

## 6. Nothing is claimable, ever

* Check the campaign/activity is still running in the client's own UI once.
  Promotional campaigns do end (WorkBuddy's shows an `end_time`).
* Check you are looking at the same account: every provider reads the local
  login of the current Windows user, so a second profile in the same app is a
  different account to the server.

## 7. Reporting a vendor change

Open an issue with: `probe` output (already masked), the exit code, and the
provider's log file tail — never `config.json`, never a token. Masking lives in
`keeper/log.py::mask`; if you see a full `mmoat_`/`dt-`/`eyJ…` string in a log
you posted, revoke the session (log out and in) before publishing.
