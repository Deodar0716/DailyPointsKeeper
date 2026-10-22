# -*- coding: utf-8 -*-
"""Entry point that works from any working directory, which is what a
scheduled task needs:

    pythonw.exe  daily_points_keeper.py claim

Using the file (instead of `-m keeper`) keeps the task action free of
`--working-directory` assumptions and of PYTHONPATH tricks.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from keeper.__main__ import main          # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
