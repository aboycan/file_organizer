"""Resolves what to do when a destination path already exists."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Set

from utils import hash_file, path_key, unique_destination

VALID_MODES = ("skip", "rename", "overwrite")


class Resolution(NamedTuple):
    action: str  # "copy" | "skip" | "overwrite" | "identical"
    final_path: Path


class DuplicateHandler:
    def __init__(self, mode: str = "rename", hash_check: bool = False):
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid duplicate mode: {mode!r}")
        self.mode = mode
        self.hash_check = hash_check

    def resolve(self, source_path: Path, dest_path: Path, reserved: Set[str]) -> Resolution:
        """Decide the final destination for ``source_path`` given a planned ``dest_path``.

        ``reserved`` holds destination paths already claimed earlier in this
        same run (so two same-named source files never collide with each
        other, even before either is written to disk).
        """
        key = path_key(dest_path)
        conflict = dest_path.exists() or key in reserved

        if not conflict:
            reserved.add(key)
            return Resolution("copy", dest_path)

        if self.hash_check and dest_path.exists():
            src_hash = hash_file(source_path)
            dst_hash = hash_file(dest_path)
            if src_hash is not None and src_hash == dst_hash:
                return Resolution("identical", dest_path)

        if self.mode == "skip":
            return Resolution("skip", dest_path)

        if self.mode == "overwrite":
            reserved.add(key)
            return Resolution("overwrite", dest_path)

        # rename
        new_path = unique_destination(dest_path, reserved)
        reserved.add(path_key(new_path))
        return Resolution("copy", new_path)
