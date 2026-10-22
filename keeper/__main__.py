# -*- coding: utf-8 -*-
"""CLI: `python -m keeper <command>` (or `python daily_points_keeper.py <command>`).

    claim        check + claim every enabled provider, notify, record   ← the task
    probe        read-only: what does each server say right now?
    doctor       local readiness, no network for the claim path
    notify-test  send one test message through the configured channel
    list         show providers, config path, notification target

Exit code is the combined per-provider code (see keeper/result.py), so
`LastTaskResult` in Task Scheduler means something.
"""

import sys

from . import config
from .log import force_utf8_streams
from .runner import Runner

HELP = __doc__


def main(argv=None):
    force_utf8_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "claim"
    runner = Runner()

    if cmd == "claim":
        rc, _ = runner.claim_all()
        return rc
    if cmd == "probe":
        rc, _ = runner.probe_all()
        return rc
    if cmd == "doctor":
        return runner.doctor()
    if cmd == "notify-test":
        ok = runner.notify.send("🔔 DailyPointsKeeper 测试",
                                "通道配置生效了。config=%s" % config.get(
                                    runner.cfg, "_path"))
        return 0 if ok else 1
    if cmd == "list":
        print("config      : %s (loaded=%s)" % (config.get(runner.cfg, "_path"),
                                                config.get(runner.cfg, "_loaded")))
        print("providers   : %s" % ", ".join(p.key for p in runner.providers))
        for key, why in runner.skipped:
            print("              %s — %s" % (key, why))
        print("notify      : %s" % runner.notify.describe())
        print("alert window: failures only after minute %d of the vendor day"
              % int(config.get(runner.cfg, "alert.last_slot_minutes", 21 * 60)))
        return 0
    print(HELP)
    return 2


if __name__ == "__main__":
    sys.exit(main())
