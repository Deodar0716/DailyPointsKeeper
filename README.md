# DailyPointsKeeper

Claim the free daily rewards of desktop AI clients — **pure HTTP scripts**, no GUI
automation, no model in the loop, zero token cost per day.

You already log into these apps. They each hand out a small daily reward
("check in today, get 100 credits"). Doing that by hand is five clicks a day you
will forget; doing it by clicking through the UI with a bot is a house of cards —
it breaks when the screen is locked, the window moves, or the app updates its
layout. This project instead reads the login the client already stored on your
machine and calls the same endpoint the client's own button calls.

```
C:\> python daily_points_keeper.py probe
[2026-10-22 20:19:38] == probe start（read-only，不领取、不通知）==
[Qoder]        ok    本周期已领（act-YYYYMMDD-NNN）
[WorkBuddy]    ok    今日已签（活动=Buddy应用 累计=100 连签=1）
[MiniMax Code] ok    今日已领（第 1/7 天 400 分）
```

中文文档：[README.zh-CN.md](README.zh-CN.md)

## Supported services

| provider key | app | daily reward | how it authenticates |
|---|---|---|---|
| `qoder_cn` | Qoder CN | 100 Credits | client's DPAPI-protected session file (`auth.v1.dat`) |
| `workbuddy` | WorkBuddy (Tencent) | credits, streak bonus | encrypted field in the client's session file, decrypted **by the client's own Electron binary** |
| `minimax_code` | MiniMax Code | 400–1000 points/7-day cycle | plaintext OAuth2 file; the script renews and writes it back |

Every mechanism, endpoint, enum and gotcha is written down in
[docs/mechanisms.md](docs/mechanisms.md) — including two designs that cost a
whole missed day before they were understood.

## Quick start (Windows)

1. Install Python 3.9+ for your user. `git clone` this repo, then double-click
   **`install.bat`**. It picks `pythonw.exe`, creates `config.json` from the
   example, runs a self-check, and registers one scheduled task with five slots
   a day (`10:05 12:05 15:05 18:05 21:30`). No administrator rights.
2. Optionally `python -m pip install -r requirements.txt` — only the Qoder
   provider needs it (`cryptography`).
3. Only use some of the apps? Set `providers` in `config.json`; unused ones
   report a credential warning and nothing else.
4. Want a message when it lands: set `notify.channel`
   (`hermes` / `webhook` / `none`) — see [Notifications](#notifications).

Manual runs, which is also what you debug with:

```
python daily_points_keeper.py doctor        # local readiness, no claims
python daily_points_keeper.py probe         # read-only: what do the servers say
python daily_points_keeper.py claim         # the task's command
python daily_points_keeper.py notify-test   # exercise the notification channel
python -m unittest discover -s tests        # 29 offline rule tests
```

macOS / Linux: the claim logic is portable (stdlib + `urllib`); the credential
readers are not yet — `qoder_cn` needs DPAPI, `workbuddy`/`minimax_code` need
their app's on-disk layout. See [Limitations](#limitations).

## How it decides

```
check()  →  done?          → ALREADY      silent, exit 0            (no message)
            claimable?     → UNAVAILABLE  exit 4, alert only late
            neither/yes    → do_claim()   → FRESH  ✅ message, exit 0
                                          → ALREADY ⏭ silent, exit 0
                                          → ApiError/CredError      ❌ exit 3/2,
                                                          alert only at the last slot
```

Four rules carry the design, each because something broke without them:

* **The server is the only truth.** `var/state/*.json` is written, never read to
  skip a run. A cycle that rolls at 10:00 plus a `checked: true` flag written at
  00:15 silently lost an entire day's reward.
* **Claims must be idempotent or the run is silent.** Every vendor here returns
  "already claimed today" as a *success-shaped* answer, and several slots per
  day are how transient failures get retried — so "already" cannot be an error.
* **Only the last slot of the day may send a failure alert.** Vendors publish the
  day's campaign late; alerting at 10:05 would cry wolf every morning.
* **A failed notification never changes a claim result.** Undelivered messages go
  to `var/notify_pending.json` (capped, 3-day TTL) and are re-sent at the start of
  the next run. Worst case is "it landed and nobody told you yet".

Exit codes are the contract with Task Scheduler (`LastTaskResult`):
`0` ok · `2` credential · `3` API · `4` nothing claimable today.
`RestartCount` will **not** retry a non-zero exit, so retries come from slots.

## Notifications

`notify.channel` in `config.json`, nothing else:

* `hermes` — runs `hermes send --to <target> --json <text>` from a local
  [Hermes](https://github.com/NousResearch/hermes-agent) bot gateway, so the
  script never holds a messaging key. Targets are tried in order (primary, then
  fallbacks). WeChat targets can be refused until you send the bot one message,
  which is why a second target is worth configuring.
* `webhook` — one POST URL, payload auto-shaped for WeCom group bot, DingTalk
  bot, PushPlus, Server酱 (`*.ftqq.com`), else generic JSON.
* `none` — no messages; delivery is reported as successful so the queue cannot
  fill up on a machine that deliberately wants silence.

Two successful pushes in a row are spaced by `notify.gap_seconds` (35) from
inside `Notifier`, keyed on the last *delivered* message. Providers therefore
need no `sleep` between them: on a day when nothing new lands, the run costs
seconds.

## Security & privacy

* **No secret ever leaves the machine.** Tokens are read per run, used in one
  HTTPS request, and never written to a log, a state file, argv, or a message
  body. `keeper/log.py::mask()` is the only way a credential reaches the log, and
  it emits `<len=60 head=mmoat_…>`.
* **`config.json` is git-ignored**, and with good reason: it holds chat ids and
  local paths. Only `config.example.json` is published, with placeholders.
* Credentials are only ever read from the current user's own profile, and only
  the client that created them can decrypt them. For WorkBuddy the script does
  not extract a key at all — it runs the app's own Electron as the decryptor.
* One provider writes a file it does not own: MiniMax's `auth.json`, because that
  vendor rotates `refresh_token` on every renewal and *refusing* to write back
  would log the desktop client out. That write takes the client's own lock, keeps
  its exact format, preserves `generation`, and rolls back if verification fails.
  Read [docs/mechanisms.md](docs/mechanisms.md#3-minimax-code--每日签到) before
  changing it.
* TLS verification is on everywhere; there is no `verify=False` in this repo.
* Rate limits are respected by spreading attempts over a few slots per day. There
  is no account creation, no payment bypass, no multi-account farming. Check each
  vendor's terms yourself — see [NOTICE](NOTICE).

## Adding a provider

Two methods, `check()` and `do_claim()`, in one file. The judgment rules, exit
codes, notification wording, log rotation and retry queue are already done.
A worked ~60-line example with the table of outcomes:
[docs/adding-a-provider.md](docs/adding-a-provider.md).

## Layout

```
daily_points_keeper.py     entry point for the scheduled task
keeper/
  config.py                defaults < config.json < DPK_* environment
  result.py                Result + exit codes + combine precedence
  log.py                   UTF-8-safe logger (pythonw has no console)
  notify.py                hermes / webhook / none + pending queue + throttle
  state.py                 per-provider record — never a gate
  runner.py                the judgment rules live here
  discover.py              find an installed client without hardcoding paths
  providers/               base.py contract + one file per service
tools/wb_desktop_session.cjs   third-party MIT helper (see NOTICE)
tests/test_rules.py        29 offline tests for the rules above
docs/                      mechanisms · adding-a-provider · troubleshooting
SKILL.md                   the same instructions, written for an AI agent
var/                       logs, state, queue (git-ignored, created on demand)
```

## Limitations

* Windows-first. `qoder_cn` is Windows-only today (DPAPI); the other two need
  their client's on-disk layout ported (macOS keychain / a different profile dir).
* The task runs only when you are logged in as that user, because the
  credentials belong to that user's session. The screen may be locked.
* Campaigns end. WorkBuddy's currently shows an end date; MiniMax rotates its
  token hourly through the client. When a vendor changes an API, the provider
  reports a clear credential/API failure rather than guessing — see
  [docs/troubleshooting.md](docs/troubleshooting.md).
* Observed October 2026 against client versions 3.1.4 / 5.7.x / 0.4.x. Nothing
  here is a promise that those endpoints still exist.

## License

[MIT](LICENSE) · third-party notes in [NOTICE](NOTICE)
