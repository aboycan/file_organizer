"""Logging setup: a detailed file log plus a quiet console channel.

The interactive progress/summary/per-file screens are drawn separately with
plain ``print()`` calls to stdout; the logger here is the durable audit
trail (every file's outcome, always, regardless of --verbose) and the
channel for warnings/errors that must not get lost under the progress bar.
The console handler is intentionally always WARNING+ only, so it never
duplicates or interleaves with the organizer's own stdout output.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def default_log_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / "logs" / f"file_organizer_{timestamp}.log"


def setup_logger(log_file: Optional[Path]) -> logging.Logger:
    log_file = log_file or default_log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("file_organizer")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console_handler)

    logger.log_file_path = log_file  # type: ignore[attr-defined]
    return logger
