# -*- coding: utf-8 -*-
"""Result value object and process exit codes.

Exit codes are part of the public contract because Windows Task Scheduler
records them as `LastTaskResult` — that number is the only thing you can grep
for when a run misbehaves.

    0  everything this run did succeeded (claimed, or already claimed)
    2  credential problem on at least one provider (re-login needed)
    3  API/network problem on at least one provider
    4  no claimable activity today for at least one provider (may be benign)

⚠ Scheduler note: `RestartCount` / `RestartInterval` do **not** fire for a task
that exits with a non-zero code — Task Scheduler only restarts tasks that fail
to start or are terminated abnormally. Retries therefore come from having
several trigger slots per day, never from the scheduler.
"""

EX_OK = 0
EX_CRED = 2
EX_API = 3
EX_NOACT = 4

# Result states. `FRESH` is the only one that triggers a success notification.
FRESH = "fresh"            # points actually landed this run
ALREADY = "already"        # server says today is already claimed (idempotent)
UNAVAILABLE = "unavailable"  # no claimable activity right now
ERROR = "error"            # API / business-code failure
CRED = "cred"              # local credential missing, expired or rejected

_ALL = (FRESH, ALREADY, UNAVAILABLE, ERROR, CRED)


class Result(object):
    """One provider's outcome. `message` is what ends up in the log and,
    for FRESH, in the notification."""

    __slots__ = ("state", "message", "code", "detail")

    def __init__(self, state, message="", code=EX_OK, detail=None):
        if state not in _ALL:
            raise ValueError("unknown result state %r" % (state,))
        self.state = state
        self.message = message
        self.code = code
        self.detail = detail or {}

    def __repr__(self):
        return "Result(%s, %r)" % (self.state, self.message)

    @property
    def notify(self):
        """FRESH → say it. Everything else is silence unless it is a hard
        failure at the last slot of the day (that decision lives in runner)."""
        return self.state == FRESH

    @property
    def alertable(self):
        return self.state in (ERROR, CRED)


def to_exit_code(result):
    return {FRESH: EX_OK, ALREADY: EX_OK, UNAVAILABLE: EX_NOACT,
            ERROR: EX_API, CRED: EX_CRED}[result.state]


def combine(codes):
    """Merge per-provider exit codes.

    2/3 dominate (they mean "something is broken"), then 4, then 0.
    A provider that simply has nothing to claim today must not be able to
    hide a provider whose credential died.
    """
    codes = list(codes)
    for rc in codes:
        if rc not in (EX_OK, EX_NOACT):
            return rc
    return EX_NOACT if EX_NOACT in codes else EX_OK
