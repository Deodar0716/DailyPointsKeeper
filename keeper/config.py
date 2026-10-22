# -*- coding: utf-8 -*-
"""Configuration: defaults < config.json < environment variables.

Nothing machine-specific is hardcoded in the providers. Anything that differs
between two people's machines (install directories, notification target,
task slots, the timezone the vendor resets in) is either auto-discovered at
runtime or lives here.

Search order for config.json:
    1. path in $DPK_CONFIG
    2. <repo root>/config.json          (copy config.example.json to start)
    3. built-in defaults below, with a warning printed once by `doctor`

Environment overrides are dotted keys upper-cased, e.g.
    DPK_NOTIFY_CHANNEL=webhook
    DPK_QODER_INSTALL_DIR=C:/path/to/Qoder CN
which is handy for a scheduled task you do not want to edit files for.
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAR_DIR = os.path.join(ROOT, "var")

DEFAULTS = {
    # Order matters: this is also the order the providers run in.
    "providers": ["qoder_cn", "workbuddy", "minimax_code"],
    "timezone_offset_hours": 8,          # the vendor resets are UTC+8 based
    "notify": {
        "channel": "none",               # none | hermes | webhook
        "gap_seconds": 35,               # between two pushes; see notify.py
        "pending_max": 8,
        "pending_keep_days": 3,
        "hermes": {"exe": "", "targets": []},
        "webhook": {"url": ""},
    },
    "alert": {
        # Only the last slot of the day may send a failure notification,
        # otherwise five slots would mean five panic messages every morning.
        "last_slot_minutes": 21 * 60 + 0,
    },
    "qoder": {
        "api_base": "https://openapi.qoder.com.cn",
        "install_dir": "",               # empty → auto-discovered
        "client_version": "",            # empty → read from install dir
    },
    "workbuddy": {
        "electron": "",                  # empty → auto-discovered
        "helper": os.path.join(ROOT, "tools", "wb_desktop_session.cjs"),
    },
    "minimax": {
        "api_host": "https://agent.minimax.cn",
        "token_url": "https://account.minimax.cn/oauth2/token",
        "refresh_margin_minutes": 5,
        "desktop_version": "",           # empty → read from installer logs
    },
}

_MISSING = object()


def _merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path():
    env = os.environ.get("DPK_CONFIG", "")
    if env:
        return env
    return os.path.join(ROOT, "config.json")


def load():
    cfg = json.loads(json.dumps(DEFAULTS))     # deep copy
    p = config_path()
    cfg["_path"] = p
    cfg["_loaded"] = False
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                user = json.load(f)
            cfg = _merge(cfg, user)
            cfg["_loaded"] = True
        except Exception as e:
            cfg["_error"] = "%s: %r" % (p, e)
    return _apply_env(cfg)


def _apply_env(cfg):
    """DPK_NOTIFY_CHANNEL / DPK_QODER_INSTALL_DIR / ... (2 levels max)."""
    for key, val in os.environ.items():
        if not key.startswith("DPK_") or key == "DPK_CONFIG":
            continue
        parts = [p.lower() for p in key[4:].split("_")]
        node = cfg
        for seg in parts[:-1]:
            node = node.setdefault(seg, {})
            if not isinstance(node, dict):
                break
        else:
            node[parts[-1]] = _coerce(val)
    return cfg


def _coerce(val):
    low = val.strip().lower()
    if low in ("1", "true", "yes"):
        return True
    if low in ("0", "false", "no"):
        return False
    try:
        return int(val)
    except ValueError:
        return val


def get(cfg, dotted, default=None):
    """get(cfg, "notify.gap_seconds") — returns `default` if absent."""
    node = cfg
    for seg in dotted.split("."):
        if not isinstance(node, dict) or seg not in node:
            return default
        node = node[seg]
    return default if node is _MISSING else node


def var_dir(*sub):
    d = os.path.join(VAR_DIR, *sub)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    return d
