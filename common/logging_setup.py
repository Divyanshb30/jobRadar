"""Central logging configuration.

Logs go to stdout (captured by GitHub Actions) and to ``logs/jobradar.log``
(uploaded as a workflow artifact for post-mortem). Call ``setup()`` once from
main.py; every module then uses ``logging.getLogger(__name__)``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

_CONFIGURED = False


def setup(level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("jobradar")
    if _CONFIGURED:
        return logger

    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)

    file_handler = logging.FileHandler(LOG_DIR / "jobradar.log", encoding="utf-8")
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(file_handler)

    # Quiet noisy third-party libraries.
    for noisy in ("urllib3", "googleapiclient", "google", "gspread"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return logger
