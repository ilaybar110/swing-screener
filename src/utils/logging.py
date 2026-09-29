"""Console + rotating file logging, shared by every entrypoint.

Usage: from src.utils.logging import get_logger; log = get_logger(__name__)
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

_CONFIGURED = False


def setup_logging(logs_dir: Optional[Path] = None, level: int = logging.INFO) -> None:
    """Configure the root logger once. Safe to call more than once (no-op after
    the first call)."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if logs_dir is not None:
        logs_dir = Path(logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_dir / "swing_screener.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. Calls setup_logging() with defaults if nobody has
    configured logging yet (so ad-hoc scripts and tests still get console output)."""
    if not _CONFIGURED:
        setup_logging(logs_dir=None)
    return logging.getLogger(name)
