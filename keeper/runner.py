# -*- coding: utf-8 -*-
"""The one place the judgment rules live.

    run_all()    claim every enabled provider, in config order
    probe_all()  read-only: ask each provider what the server says
    doctor()     local readiness: credentials found? optional deps present?

Rules enforced here (each one is the residue of a real missed day):

1. **Server is the truth.** `state` files are written, never consulted to skip.
2. `done` → ALREADY → silent, exit 0. So a day claimed by hand, or by the
   previous slot, does not produce a message or an error.
3. `claimable` false and not done → UNAVAILABLE → exit 4, and the failure
   message is only sent at `alert.last_slot_minutes` or later. Vendors publish
   the day's activity *late* sometimes; panicking at 10:05 would cry wolf every
   single morning.
4. CredError → exit 2, ApiError → exit 3, both alertable at the last slot only.
5. A provider blowing up cannot stop the others, and a notification failing
   cannot change a claim result.
"""

import time

from . import config, providers, state
from .log import Log
from .notify import Notifier
from .providers.base import ApiError, CredError
from .result import (ALREADY, CRED, ERROR, EX_API, EX_CRED, EX_NOACT, EX_OK,
                     FRESH, UNAVAILABLE, Result, combine, to_exit_code)

_MAIN_LOG = "keeper"


class Runner(object):
    def __init__(self, cfg=None):
        self.cfg = cfg or config.load()
        self.log = Log(_MAIN_LOG)
        self.notify = Notifier(self.cfg)
        self.day_offset = int(config.get(self.cfg, "timezone_offset_hours", 8) or 8)
        self.last_slot = int(config.get(self.cfg, "alert.last_slot_minutes", 21 * 60))
        self.providers, self.skipped = providers.build(
            config.get(self.cfg, "providers", []) or [],
            self.cfg, lambda key: Log(key))

    # ---------------- public ----------------

    def claim_all(self):
        self.log("== run start · %d providers · notify=%s =="
                 % (len(self.providers), self.notify.describe()))
        self.notify.flush()          # pay back anything a previous run owed
        results = []
        for p in self.providers:
            results.append(self._claim_one(p))
        for key, why in self.skipped:
            self.log("[%s] SKIP %s" % (key, why))
            results.append((key, Result(ERROR, "未加载：%s" % why, EX_API)))
        rc = combine([r[1].code for r in results])
        self.log("== run end · exit=%s · %s =="
                 % (rc, " ".join("%s=%s" % (k, s.state) for k, s in results)))
        return rc, results

    def probe_all(self):
        self.log("== probe start（read-only，不领取、不通知）==")
        rows = []
        for p in self.providers:
            try:
                snap = p.check()
                row = (p.label, "ok", snap.summary)
            except CredError as e:
                row = (p.label, "cred", str(e))
            except Exception as e:
                row = (p.label, "api", "%r" % e)
            self.log("[%s] %-5s %s" % row)      # print as we go: a provider's own
            rows.append(row)                    # log line must not interleave oddly
        for key, why in self.skipped:
            self.log("[%s] skip  %s" % (key, why))
            rows.append((key, "skip", why))
        bad = [r for r in rows if r[1] != "ok"]
        return (EX_OK if not bad else EX_API), rows

    def doctor(self):
        rows = []
        rows.append(("config", bool(self.cfg.get("_loaded")),
                     self.cfg.get("_path") + ("" if self.cfg.get("_loaded")
                                              else "  ← 不存在，正在用默认值（复制 config.example.json 过去）")))
        if self.cfg.get("_error"):
            rows.append(("config-parse", False, self.cfg["_error"]))
        rows.append(("notify", True, self.notify.describe()))
        rows.append(("providers", bool(self.providers),
                     ", ".join(p.key for p in self.providers) or "一个都没启用"))
        for key, why in self.skipped:
            rows.append((key, False, why))
        for p in self.providers:
            for name, ok, note in p.doctor():
                rows.append(("%s.%s" % (p.key, name), ok, note))
        for name, ok, note in rows:
            self.log("%-26s %-5s %s" % (name, "OK" if ok else "WARN", note))
        return EX_OK if all(ok for _, ok, _ in rows) else EX_NOACT

    # ---------------- internals ----------------

    def _claim_one(self, p):
        t0 = time.time()
        now = time.strftime("%Y-%m-%d %H:%M")
        late = state.minutes_of_day(self.day_offset) >= self.last_slot
        try:
            snap = p.check()
        except CredError as e:
            res = Result(CRED, "凭据不可用：%s" % e, EX_CRED)
        except ApiError as e:
            res = Result(ERROR, "接口异常：%s" % e, EX_API)
        except Exception as e:
            res = Result(ERROR, "%s 抛异常：%r" % (p.label, e), EX_API)
        else:
            if snap.done:
                res = Result(ALREADY, snap.summary or "本周期已领", EX_OK, snap.detail)
            elif not snap.claimable:
                res = Result(UNAVAILABLE, snap.summary or "当前没有可领档位", EX_NOACT,
                             snap.detail)
            else:
                res = self._do_claim(p, now)
        self._record(p, res, now)
        if res.state == FRESH:
            if not self.notify.send("✅ %s 每日签到" % p.label,
                                    "%s %s" % (res.message, snap_note(res))):
                self.log("[%s] ⚠ 领取成功但通知未送达，已入队" % p.key)
        elif res.alertable and late:
            self.notify.send("⚠️ %s 签到失败" % p.label, "%s\n%s" % (now, res.message))
        self.log("[%s] %s · %s (%.1fs)" % (p.key, res.state, res.message,
                                           time.time() - t0))
        return p.key, res

    def _do_claim(self, p, now):
        try:
            snap = p.do_claim()
        except CredError as e:
            return Result(CRED, "领取时凭据失效：%s" % e, EX_CRED)
        except ApiError as e:
            return Result(ERROR, "领取失败：%s" % e, EX_API)
        except Exception as e:
            return Result(ERROR, "领取时抛异常：%r" % e, EX_API)
        outcome = snap.outcome
        if outcome is None:
            # Fallback inference for providers that do not set `outcome`.
            # Prefer setting it explicitly — "already claimed" and "gained"
            # are different facts and must not be conflated.
            if snap.gained is not None or snap.done:
                outcome = FRESH
            elif snap.claimable:
                outcome = ERROR          # still claimable ⇒ the claim did not land
            else:
                outcome = ALREADY
        if outcome == FRESH:
            return Result(FRESH, snap.summary or "已领到手 +%s %s" % (snap.gained, p.unit),
                          EX_OK, snap.detail)
        if outcome == ALREADY:
            return Result(ALREADY, snap.summary or "服务端判定已领（幂等）", EX_OK, snap.detail)
        if outcome == UNAVAILABLE:
            return Result(UNAVAILABLE, snap.summary or "领取未生效（当前无可领档位）",
                          EX_NOACT, snap.detail)
        return Result(ERROR, snap.summary or "领取未生效", EX_API, snap.detail)

    def _record(self, p, res, now):
        state.save(p.key, {
            "day": state.local_day(self.day_offset),
            "state": res.state,
            "exit": res.code,
            "message": res.message,
            "detail": res.detail,
            "at": now,
        })


def snap_note(res):
    d = res.detail or {}
    bits = []
    for k in ("balance", "total", "remaining", "streak", "cycle"):
        if d.get(k) not in (None, ""):
            bits.append("%s=%s" % (k, d[k]))
    return ("（%s）" % " ".join(bits)) if bits else ""
