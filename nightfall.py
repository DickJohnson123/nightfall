#!/usr/bin/env python3
"""Точка входа Nightfall.

    python3 nightfall.py            # графическое окно
    python3 nightfall.py --headless # без окна (например, из systemd)
    python3 nightfall.py --plan     # расписание в терминал
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nightfall.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
