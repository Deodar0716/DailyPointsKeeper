# -*- coding: utf-8 -*-
"""Provider registry.

Add a file under `keeper/providers/`, expose a class with `key`, then list the
key in `config.json → providers`. Import is dynamic so a provider whose
optional dependency is missing (e.g. no `cryptography` installed) does not take
the whole run down — it becomes a CRED-style result with a clear message.
"""

import importlib

from .base import ApiError, CredError, Provider, Snapshot   # noqa: F401

_MODULES = {
    "qoder_cn": ("qoder_cn", "QoderCNProvider"),
    "workbuddy": ("workbuddy", "WorkBuddyProvider"),
    "minimax_code": ("minimax_code", "MiniMaxCodeProvider"),
}


def build(keys, cfg, log_factory):
    out, skipped = [], []
    for key in keys or []:
        entry = _MODULES.get(key)
        if not entry:
            skipped.append((key, "unknown provider key (see keeper/providers/__init__.py)"))
            continue
        mod_name, cls_name = entry
        try:
            mod = importlib.import_module("keeper.providers.%s" % mod_name)
            cls = getattr(mod, cls_name)
        except Exception as e:
            skipped.append((key, "import failed: %r" % e))
            continue
        try:
            out.append(cls(cfg, log_factory(key)))
        except Exception as e:
            skipped.append((key, "construct failed: %r" % e))
    return out, skipped
