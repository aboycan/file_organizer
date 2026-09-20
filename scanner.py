"""Recursive, error-tolerant directory scanner.

Uses an explicit stack with ``os.scandir`` (rather than plain recursion)
so there is no practical limit on nesting depth and a single bad
directory can never take down the whole scan.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Set

from categorizer import Categorizer
from utils import get_file_attributes, path_key, safe_stat


@dataclass
class FileEntry:
    path: Path
    size: int
    category: str
    hidden: bool
    system: bool
    modified: float  # st_mtime (last modified time), used by --date-folders


class Scanner:
    def __init__(
        self,
        source: Path,
        categorizer: Categorizer,
        logger,
        destination: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        exclude_names: Optional[Set[str]] = None,
        exclude_paths: Optional[Set[str]] = None,
    ):
        self.source = source
        self.categorizer = categorizer
        self.logger = logger
        self.destination = destination
        self.progress_callback = progress_callback
        self.exclude_names = exclude_names or set()  # lower-cased bare folder names, matched at any depth
        self.exclude_paths = exclude_paths or set()  # resolved absolute paths, via utils.path_key()
        self.errors = 0
        self.dirs_skipped = 0
        self.dirs_excluded = 0
        self.files_seen = 0
        self.scan_interrupted = False

    def _should_skip_dir(self, dir_path: Path, visited_real: Set[str]) -> bool:
        if self.destination is not None:
            try:
                if dir_path.resolve() == self.destination.resolve():
                    return True
            except OSError:
                pass

        if self.exclude_names and dir_path.name.lower() in self.exclude_names:
            self.dirs_excluded += 1
            self.logger.debug("Excluding folder (name match): %s", dir_path)
            return True

        if self.exclude_paths:
            try:
                resolved_str = path_key(dir_path.resolve())
            except OSError:
                resolved_str = path_key(dir_path)
            if resolved_str in self.exclude_paths:
                self.dirs_excluded += 1
                self.logger.debug("Excluding folder (path match): %s", dir_path)
                return True

        try:
            real = os.path.realpath(dir_path)
        except OSError:
            real = str(dir_path)
        if real in visited_real:
            self.logger.debug("Skipping already-visited directory (link loop guard): %s", dir_path)
            return True
        visited_real.add(real)
        return False

    def scan(self) -> Iterator[FileEntry]:
        visited_real: Set[str] = set()
        stack = [self.source]
        try:
            root_real = os.path.realpath(self.source)
            visited_real.add(root_real)
        except OSError:
            pass

        while stack:
            current_dir = stack.pop()
            try:
                entries = list(os.scandir(current_dir))
            except (PermissionError, OSError) as exc:
                self.errors += 1
                self.dirs_skipped += 1
                self.logger.warning("Cannot access directory '%s': %s", current_dir, exc)
                continue

            for entry in entries:
                try:
                    entry_path = Path(entry.path)
                    is_symlink = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError as exc:
                    self.errors += 1
                    self.logger.warning("Cannot stat entry '%s': %s", getattr(entry, "path", "?"), exc)
                    continue

                if is_dir:
                    if is_symlink:
                        self.logger.debug("Not descending into symlink/junction: %s", entry_path)
                        continue
                    if self._should_skip_dir(entry_path, visited_real):
                        continue
                    stack.append(entry_path)
                    continue

                self.files_seen += 1
                if self.progress_callback:
                    self.progress_callback(self.files_seen, str(entry_path))

                st = safe_stat(entry_path)
                if st is None:
                    self.errors += 1
                    self.logger.warning("Cannot stat file (skipped): %s", entry_path)
                    continue

                hidden, system = get_file_attributes(entry_path)
                category = self.categorizer.categorize(entry_path.name)
                yield FileEntry(
                    path=entry_path,
                    size=st.st_size,
                    category=category,
                    hidden=hidden,
                    system=system,
                    modified=st.st_mtime,
                )
