"""Small stand-alone helpers shared by the other modules.

Kept dependency-free (standard library only) so the utility can be
packaged with PyInstaller without surprises.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# Windows and (by default) macOS filesystems are case-insensitive for path
# identity purposes; Linux (ext4 etc.) is case-sensitive, so "Photo.jpg" and
# "photo.jpg" are two distinct, non-colliding files there. Used only for
# duplicate/exclusion *path-identity* comparisons below — never for
# extension categorization or bare-name matching, which stay
# case-insensitive everywhere as a deliberate UX choice.
PATH_CASE_INSENSITIVE = IS_WINDOWS or IS_MACOS


def path_key(path: object) -> str:
    """Normalize a path to a comparison key respecting platform case-sensitivity."""
    text = str(path)
    return text.lower() if PATH_CASE_INSENSITIVE else text

_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
UNKNOWN_DATE_FOLDER = "UnknownDate"

_SIZE_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
    "TB": 1024 ** 4,
}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")


class SizeParseError(ValueError):
    """Raised when a human-readable size string cannot be parsed."""


def parse_size(value: str) -> int:
    """Parse a human readable size such as ``10MB`` / ``5 GB`` / ``2048`` into bytes."""
    match = _SIZE_RE.match(value)
    if not match:
        raise SizeParseError(f"Invalid size value: {value!r}")
    number, unit = match.groups()
    unit = unit.upper() or "B"
    if unit not in _SIZE_UNITS:
        raise SizeParseError(
            f"Unknown size unit {unit!r} in {value!r}. Use B, KB, MB, GB or TB."
        )
    return int(float(number) * _SIZE_UNITS[unit])


def human_size(num_bytes: float) -> str:
    """Format a byte count as a human-readable string, e.g. ``12.3 MB``."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def date_folder_parts(timestamp: float) -> tuple[str, str]:
    """Return ``(year_folder, month_folder)`` for a Unix timestamp, e.g. ``("2025", "Jan2025")``.

    Falls back to a single ``UnknownDate`` pair if the timestamp is invalid
    (corrupt metadata, out-of-range value, etc.) rather than raising.
    """
    try:
        dt = datetime.fromtimestamp(timestamp)
    except (OSError, OverflowError, ValueError):
        return UNKNOWN_DATE_FOLDER, UNKNOWN_DATE_FOLDER
    year = str(dt.year)
    month = f"{_MONTH_ABBR[dt.month - 1]}{dt.year}"
    return year, month


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as ``1h 23m 45s`` (dropping empty leading units)."""
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def get_file_attributes(path: os.PathLike) -> tuple[bool, bool]:
    """Return ``(is_hidden, is_system)`` for a path using Windows file attributes.

    Falls back to a dotfile-name heuristic for hidden detection on platforms
    (or filesystems) that do not expose ``st_file_attributes``.
    """
    hidden = False
    system = False
    try:
        st = os.stat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        hidden = bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN)
        system = bool(attrs & stat.FILE_ATTRIBUTE_SYSTEM)
    except OSError:
        pass
    if not hidden and Path(path).name.startswith("."):
        hidden = True
    return hidden, system


def long_path(path: str) -> str:
    """Return an extended-length path form (``\\\\?\\...``) for very long Windows paths."""
    if not IS_WINDOWS:
        return path
    if path.startswith("\\\\?\\"):
        return path
    abs_path = os.path.abspath(path)
    if len(abs_path) < 240:
        return path
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path.lstrip("\\")
    return "\\\\?\\" + abs_path


def safe_stat(path: os.PathLike) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except OSError:
        return None


def hash_file(path: os.PathLike, block_size: int = 1024 * 1024) -> Optional[str]:
    """SHA-256 hash of a file's contents, streamed in chunks. Returns None on error."""
    hasher = hashlib.sha256()
    try:
        with open(long_path(str(path)), "rb") as fh:
            while True:
                chunk = fh.read(block_size)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()


def unique_destination(dest_path: Path, reserved: set[str]) -> Path:
    """Find a non-conflicting path by appending ``_1``, ``_2``, ... to the stem.

    Checks both the real filesystem and an in-memory ``reserved`` set (paths
    already earmarked earlier in the same run but not yet written).
    """
    key = path_key(dest_path)
    if not dest_path.exists() and key not in reserved:
        return dest_path
    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        candidate_key = path_key(candidate)
        if not candidate.exists() and candidate_key not in reserved:
            return candidate
        counter += 1


def get_config_path() -> Path:
    """Location of config.json.

    As a plain script: next to this file. When frozen with PyInstaller: an
    external config.json next to the .exe takes precedence (so it can be
    customized without rebuilding), falling back to the copy bundled inside
    the executable via --add-data.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        external = exe_dir / "config.json"
        if external.exists():
            return external
        return Path(getattr(sys, "_MEIPASS", exe_dir)) / "config.json"
    return Path(__file__).resolve().parent / "config.json"


def is_drive_root(path: Path) -> bool:
    """True if ``path`` is a filesystem root: ``D:\\`` on Windows, ``/`` on Unix."""
    resolved = path.resolve()
    return str(resolved) == resolved.drive + "\\" or str(resolved) == resolved.anchor


def is_same_volume(path_a: Path, path_b: Path) -> Optional[bool]:
    """True/False if two paths are on the same filesystem/volume, None if undeterminable.

    Uses ``st_dev`` rather than comparing Windows drive letters, since a
    drive-letter comparison is always trivially "equal" on POSIX (where
    ``Path.drive`` is always empty) and also misses Windows volumes mounted
    without a drive letter. Walks up to the nearest existing ancestor for a
    path that doesn't exist yet (e.g. a destination not yet created).
    """
    def existing_ancestor(p: Path) -> Path:
        p = p.resolve()
        while not p.exists() and p.parent != p:
            p = p.parent
        return p

    try:
        dev_a = os.stat(existing_ancestor(path_a)).st_dev
        dev_b = os.stat(existing_ancestor(path_b)).st_dev
    except OSError:
        return None
    return dev_a == dev_b


def path_is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is the same as, or nested inside, ``parent``."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
