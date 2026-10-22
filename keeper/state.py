# -*- coding: utf-8 -*-
"""Per-provider state files.

Rule this exists to enforce: **a state file is a record, never a gate.**

Real incident this rule came from: a provider's cycle rolled over at 10:00, not
midnight. A test run at 00:15 read the *previous* cycle's "CLAIMED" from the
server, wrote `{date: tomorrow, checked: true}`, and the 10:05 scheduled run
short-circuited on that file — so a whole day's points were silently skipped.
Since then the rule is:

* every run asks the server again, no matter what the file says;
* the file only records what happened, for humans and for `doctor`.

Files are `var/state/<provider>.json`, git-ignored, and deliberately carry no
credential material (only ids, amounts and timestamps).
"""

import json
import os
import time

from . import config


def path_for(name):
    return os.path.join(config.var_dir("state"), "%s.json" % name)


def load(name):
    try:
        with open(path_for(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(name, record):
    record = dict(record)
    record.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        with open(path_for(name), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def local_day(offset_hours):
    """Vendor date, e.g. UTC+8 — the day the *service* thinks it is."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset_hours * 3600))


def minutes_of_day(offset_hours):
    n = time.gmtime(time.time() + offset_hours * 3600)
    return n.tm_hour * 60 + n.tm_min
