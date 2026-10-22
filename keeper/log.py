# -*- coding: utf-8 -*-
"""UTF-8 safe logger that works under pythonw.exe.

Two things will silently kill a scheduled task if you forget them:

1. `pythonw.exe` has **no console**, so `sys.stdout` is None or a broken pipe.
   Never let a logging call raise on print — swallow it.
2. Under a Chinese Windows locale the default stdout encoding is GBK, so the
   first emoji in a message raises UnicodeEncodeError and the whole run dies
   *after* the claim already succeeded. Hence reconfigure(stdout) to UTF-8.

Each provider gets its own file (`var/logs/<name>.log`) plus one shared
`keeper.log`, and every file truncates itself to the last 400 lines past 256 KB
so a year of scheduled runs cannot fill the disk.
"""

import os
import sys
import time

from . import config

_MAX_BYTES = 256 * 1024
_KEEP_LINES = 400


def force_utf8_streams():
    for s in (sys.stdout, sys.stderr):
        try:
            if s is not None:
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def mask(secret, keep=6):
    """Log *shape*, never value: `mask(tok)` → `<len=60 head=mmoat_>`.

    Every token in this project is a live bearer credential. If you add a
    provider, keep this the only way a secret reaches the log.
    """
    s = secret or ""
    return "<len=%d head=%s…>" % (len(s), s[:keep]) if s else "<empty>"


class Log(object):
    def __init__(self, name="keeper"):
        self.path = os.path.join(config.var_dir("logs"), "%s.log" % name)

    def write(self, msg):
        line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if os.path.getsize(self.path) > _MAX_BYTES:
                with open(self.path, encoding="utf-8", errors="replace") as f:
                    keep = f.readlines()[-_KEEP_LINES:]
                with open(self.path, "w", encoding="utf-8") as f:
                    f.writelines(keep)
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    __call__ = write
