# -*- coding: utf-8 -*-
"""The provider contract: **two methods**.

    check()     read-only. What does the server say right now?
    do_claim()  perform the claim. Must be safe to call again tomorrow.

Everything else — "already done today means silence", "only alert at the last
slot", "state file is a record, never a gate", exit codes, notification
wording — is done once, in `runner.py`. A provider that only implements these
two methods cannot get those rules wrong, which is the whole point of the
split.

`Snapshot.claimable` / `.done` are the only two decisions the runner needs:

    done=True                  → ALREADY     (silent, exit 0)
    done=False claimable=False  → UNAVAILABLE (silent-ish, exit 4)
    done=False claimable=True   → call do_claim()

Raise `CredError` for anything that means "this person must re-login or install
the app" and `ApiError` for transport/business failures; the runner maps them to
exit codes 2 and 3 and to the alert rule.

See docs/adding-a-provider.md for a worked 40-line example.
"""

import time


class CredError(Exception):
    """Local credential missing / expired / rejected — needs a human."""


class ApiError(Exception):
    """Network, HTTP or business-code failure."""


class Snapshot(object):
    """One read of the service. `summary` is the human sentence for logs.

    `outcome` is only meaningful on the value returned by `do_claim()`: set it
    to result.FRESH / ALREADY / UNAVAILABLE to say exactly what happened
    (idempotent "already claimed" must not be reported as an error). When it is
    None the runner infers from `gained` / `done`.
    """

    __slots__ = ("claimable", "done", "summary", "detail", "gained", "outcome")

    def __init__(self, claimable=False, done=False, summary="", detail=None, gained=None,
                 outcome=None):
        self.claimable = bool(claimable)
        self.done = bool(done)
        self.summary = summary
        self.detail = detail or {}
        self.gained = gained          # amount just credited, only from do_claim()
        self.outcome = outcome

    def __repr__(self):
        return "Snapshot(claimable=%s done=%s %r)" % (self.claimable, self.done,
                                                      self.summary)


class Provider(object):
    key = "base"
    label = "Base"
    #: what the reward even is, for wording ("100 Credits", "400 积分")
    unit = "points"

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

    # -- required -----------------------------------------------------

    def check(self):
        raise NotImplementedError("%s must implement check()" % self.__class__.__name__)

    def do_claim(self):
        raise NotImplementedError("%s must implement do_claim()" % self.__class__.__name__)

    # -- optional -----------------------------------------------------

    def doctor(self):
        """Report readiness without touching the network.

        Return a list of `(name, ok, note)` lines. Used by `python -m keeper doctor`
        so a fresh machine explains *why* a provider is disabled.
        """
        return []

    @staticmethod
    def now():
        return time.strftime("%Y-%m-%d %H:%M")
