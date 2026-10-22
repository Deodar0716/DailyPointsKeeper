# -*- coding: utf-8 -*-
"""DailyPointsKeeper — claim the free daily points of desktop AI clients,
pure HTTP, no GUI automation, no model tokens.

Package layout (keep it boring on purpose):

    keeper/config.py      config.json + environment overrides
    keeper/result.py      Result value object + process exit codes
    keeper/log.py         UTF-8 safe logger (pythonw has no console)
    keeper/state.py       per-provider state record — a *record*, never a gate
    keeper/notify.py      notification channels + retry queue
    keeper/runner.py      sequential orchestration
    keeper/providers/     one module per service; see base.py for the contract

The contract every provider implements is two methods: `status()` (read-only)
and `claim()` (idempotent). All the judgment rules — "already claimed today is
silence", "only alert on the last slot of the day", "server is the truth" —
live in runner.py, so a new provider cannot get them wrong.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
