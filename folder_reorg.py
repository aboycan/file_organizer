#!/usr/bin/env python3
"""Folder Reorg - gather every copy of a named folder into one place.

After organizing by date, the same source folder (for example
"Rajesh HDD") often ends up split across many Year/Month folders:

    Images\\2000\\Sep2000\\Rajesh HDD\\...
    Images\\2012\\Jan2012\\TP\\Rajesh HDD\\...

folder_reorg scans a source tree (any depth) for every folder with the
name you give, and merges all of their contents into ONE destination
folder. The structure *inside* each match is kept; whatever sat above it
(year, month, category, TP, ...) is dropped. When two matches contain a
file at the same relative path:

- byte-for-byte identical files are skipped (only one copy is kept);
- different files are both kept, the later one renamed photo_1.jpg, ...
  (or skipped / overwritten with --duplicate).

Copy is the default (originals untouched); --move moves the files, and
--remove-empty then removes the matched folders left empty. --dry-run
shows exactly what would happen without touching anything.

Part of File Organizer (shares its logging, hashing and path helpers).
Author: Madhu Kumar <madhu.a@hotmail.com>
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from logger import setup_logger
from utils import (
    IS_WINDOWS,
    format_duration,
    hash_file,
    human_size,
    is_same_volume,
    long_path,
    path_is_within,
    path_key,
)

BANNER_WIDTH = 60


@dataclass
class Match:
    folder: Path                      # e.g. I:\Org\Images\2000\Sep2000\Rajesh HDD
    files: List[Tuple[Path, int]] = field(default_factory=list)  # (path, size)

    @property
    def size(self) -> int:
        return sum(s for _, s in self.files)


@dataclass
class Stats:
    copied: int = 0
    moved: int = 0
    renamed: int = 0
    identical: int = 0
    skipped: int = 0
    overwritten: int = 0
    errors: int = 0
    removed_dirs: int = 0


def _is_link_dir(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    if is_junction is not None:
        return is_junction()
    if IS_WINDOWS:
        import stat as _stat
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return False


class FolderReorg:
    def __init__(self, args):
        self.args = args
        self.stats = Stats()
        self.logger = setup_logger(Path(args.log_file) if args.log_file else None)
        self.start = time.monotonic()
        self.names = {n.strip().strip('"').lower() for n in args.folder.split(",") if n.strip(' "')}
        self.exclude = {n.strip().strip('"').lower() for n in (args.exclude_folders or "").split(",") if n.strip(' "')}
        self.scan_errors = 0
        self.csv_rows: List[dict] = []

    # ------------------------------------------------------------------ #
    def _validate(self) -> Optional[str]:
        source = Path(self.args.source)
        if not source.is_dir():
            return f"Source folder does not exist or is not a folder: {source}"
        self.source = source.resolve()
        first_name = self.args.folder.split(",")[0].strip().strip('"')
        dest = Path(self.args.dest) if self.args.dest else self.source / first_name
        self.dest = dest.resolve()
        if path_is_within(self.source, self.dest):
            return "The destination cannot be the source folder itself or one of its parents."
        return None

    # ------------------------------------------------------------------ #
    def _find_matches(self) -> List[Match]:
        """Every folder named like --folder, anywhere under the source.

        A match is taken whole: we don't look for further matches inside
        it (a nested "Rajesh HDD" just becomes a subfolder). The
        destination folder itself is never treated as a match.
        """
        matches: List[Match] = []
        dest_key = path_key(self.dest)
        stack = [self.source]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(long_path(str(current))), key=lambda e: e.name.lower())
            except OSError as exc:
                self.scan_errors += 1
                self.logger.warning("Cannot read folder '%s': %s", current, exc)
                continue
            subdirs = []
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False) or _is_link_dir(entry):
                        continue
                except OSError:
                    continue
                path = current / entry.name
                if path_key(path) == dest_key:
                    continue
                name = entry.name.lower()
                if name in self.exclude:
                    continue
                if name in self.names:
                    matches.append(Match(path))
                else:
                    subdirs.append(path)
            stack.extend(reversed(subdirs))
        for match in matches:
            match.files = self._files_under(match.folder)
        matches.sort(key=lambda m: path_key(m.folder))
        return matches

    def _files_under(self, folder: Path) -> List[Tuple[Path, int]]:
        files: List[Tuple[Path, int]] = []
        dest_key = path_key(self.dest)
        stack = [folder]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(os.scandir(long_path(str(current))), key=lambda e: e.name.lower())
            except OSError as exc:
                self.scan_errors += 1
                self.logger.warning("Cannot read folder '%s': %s", current, exc)
                continue
            subdirs = []
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        sub = current / entry.name
                        if not _is_link_dir(entry) and path_key(sub) != dest_key:
                            subdirs.append(sub)
                    elif entry.is_file(follow_symlinks=False):
                        files.append((current / entry.name, entry.stat(follow_symlinks=False).st_size))
                except OSError as exc:
                    self.scan_errors += 1
                    self.logger.warning("Cannot read '%s': %s", entry.path, exc)
            stack.extend(reversed(subdirs))
        return files

    # ------------------------------------------------------------------ #
    def _resolve(self, src: Path, target: Path, planned: Dict[str, Path]) -> Tuple[str, Path]:
        """Decide what happens to ``src`` whose natural place is ``target``.

        Returns (action, final_path) with action in copy/identical/skip/overwrite.
        ``planned`` maps destination keys already claimed in this run to the
        source file that claimed them (so a dry run detects identical files
        too, before anything is written).
        """
        src_hash: List[Optional[str]] = []  # computed at most once, only if needed

        def same_content(path: Path, exists: bool, key: str) -> bool:
            if not self.args.hash_check:
                return False
            # On disk (real run, or already there before) compare with that file;
            # otherwise (dry run) compare with the source file planned for it.
            other = path if exists else planned.get(key)
            if other is None:
                return False
            if not src_hash:
                src_hash.append(hash_file(src))
            return src_hash[0] is not None and src_hash[0] == hash_file(other)

        key = path_key(target)
        exists = os.path.exists(long_path(str(target)))
        if not exists and key not in planned:
            planned[key] = src
            return "copy", target
        if same_content(target, exists, key):
            return "identical", target
        if self.args.duplicate == "skip":
            return "skip", target
        if self.args.duplicate == "overwrite":
            planned[key] = src
            return "overwrite", target
        counter = 1
        while True:
            candidate = target.with_name(f"{target.stem}_{counter}{target.suffix}")
            ckey = path_key(candidate)
            cexists = os.path.exists(long_path(str(candidate)))
            if ckey not in planned and not cexists:
                planned[ckey] = src
                return "copy", candidate
            if same_content(candidate, cexists, ckey):  # same content already placed as photo_1.jpg etc.
                return "identical", candidate
            counter += 1

    def _process(self, matches: List[Match]) -> None:
        dry, move = self.args.dry_run, self.args.move
        verb, done_verb = ("Move", "Moved") if move else ("Copy", "Copied")
        planned: Dict[str, Path] = {}
        total = sum(len(m.files) for m in matches)
        done = 0
        print("\nDRY RUN - nothing will be copied, moved or deleted.\n" if dry else "\nProcessing...\n")
        for match in matches:
            for src, size in match.files:
                done += 1
                rel = src.relative_to(match.folder)
                action, final = self._resolve(src, self.dest / rel, planned)
                status, error = "", ""
                if action == "identical":
                    self.stats.identical += 1
                    status = "Skipped (identical)"
                elif action == "skip":
                    self.stats.skipped += 1
                    status = "Skipped (exists)"
                else:
                    renamed = final.name != src.name
                    if dry:
                        status = "Would " + ("Overwrite" if action == "overwrite"
                                             else f"Rename & {verb}" if renamed else verb)
                    else:
                        status = ("Overwritten" if action == "overwrite"
                                  else f"Renamed & {done_verb}" if renamed else done_verb)
                    if not dry:
                        try:
                            final.parent.mkdir(parents=True, exist_ok=True)
                            dst = long_path(str(final))
                            if action == "overwrite" and os.path.exists(dst):
                                os.remove(dst)
                            if move:
                                shutil.move(long_path(str(src)), dst)
                            else:
                                shutil.copy2(long_path(str(src)), dst)
                        except (OSError, shutil.Error) as exc:
                            self.stats.errors += 1
                            status, error = "Error", str(exc)
                            self.logger.error("Failed: '%s' -> '%s': %s", src, final, exc)
                    if status != "Error":
                        if action == "overwrite":
                            self.stats.overwritten += 1
                        elif renamed:
                            self.stats.renamed += 1
                        if move:
                            self.stats.moved += 1
                        else:
                            self.stats.copied += 1
                if status != "Error":
                    self.logger.info("%s: '%s' -> '%s'", status, src, final)
                if self.args.verbose or dry:
                    print(f"[{done}/{total}] {status}: {src}\n        -> {final}" + (f"  ({error})" if error else ""))
                elif done % 25 == 0 or done == total:
                    sys.stdout.write(f"\rProgress: {done:,}/{total:,}")
                    sys.stdout.flush()
                if self.args.export_csv:
                    self.csv_rows.append({
                        "Source Path": str(src), "Matched Folder": str(match.folder),
                        "Destination Path": str(final), "File Size": size, "Status": status, "Error": error,
                    })
        print()
        if move and self.args.remove_empty and not dry:
            for match in matches:
                self._remove_empty_dirs(match.folder)

    def _remove_empty_dirs(self, folder: Path) -> None:
        """Remove now-empty folders inside (and including) a matched folder. Never touches files."""
        for dirpath, _dirs, _files in os.walk(long_path(str(folder)), topdown=False):
            try:
                os.rmdir(dirpath)  # fails (and is ignored) unless the folder is empty
                self.stats.removed_dirs += 1
                self.logger.info("Removed empty folder: '%s'", dirpath)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    def _write_csv(self) -> None:
        path = Path(self.args.export_csv)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=[
                    "Source Path", "Matched Folder", "Destination Path", "File Size", "Status", "Error"])
                writer.writeheader()
                writer.writerows(self.csv_rows)
        except OSError as exc:
            print(f"\nWARNING: Could not write CSV report: {exc}")

    def run(self) -> int:
        error = self._validate()
        if error:
            print(f"ERROR: {error}")
            self.logger.error(error)
            return 1
        names = ", ".join(f'"{n}"' for n in self.args.folder.split(",") if n.strip(' "'))
        print(f"Scanning {self.source} for folders named {names} ...")
        matches = self._find_matches()
        total_files = sum(len(m.files) for m in matches)
        total_size = sum(m.size for m in matches)

        print("\n" + "=" * BANNER_WIDTH)
        print("FOLDER REORG".center(BANNER_WIDTH))
        print("=" * BANNER_WIDTH)
        print(f"Source       : {self.source}")
        print(f"Destination  : {self.dest}")
        print(f"Operation    : {'MOVE' if self.args.move else 'COPY'}")
        print(f"Duplicates   : identical files skipped{'' if self.args.hash_check else ' (OFF)'}; "
              f"different files -> {self.args.duplicate}")
        print(f"Dry Run      : {'YES' if self.args.dry_run else 'NO'}")
        print(f"\nFound {len(matches)} matching folder(s), {total_files:,} file(s), {human_size(total_size)}:")
        for m in matches:
            print(f"  {len(m.files):>7,} files {human_size(m.size):>11}  {m.folder}")
        if self.scan_errors:
            print(f"  ({self.scan_errors} item(s) could not be read - see log)")
        self.logger.info("Matched folders: %s", [str(m.folder) for m in matches])
        if not matches or not total_files:
            print("\nNothing to do.")
            return 0

        if not self.args.dry_run:
            if not self.args.move or not is_same_volume(self.source, self.dest):
                anc = self.dest
                while not anc.exists() and anc.parent != anc:
                    anc = anc.parent
                try:
                    free = shutil.disk_usage(anc).free
                except OSError:
                    free = None
                if free is not None and total_size > free:
                    print(f"\nWARNING: needs up to {human_size(total_size)} but only {human_size(free)} is free.")
                    if self.args.yes:
                        print("Aborting because --yes was given.")
                        return 1
            if self.args.move:
                print("\nMOVE mode: files will be removed from the matched folders after being placed.")
            if not self.args.yes:
                try:
                    if input("\nContinue? [y/N]: ").strip().lower() not in ("y", "yes"):
                        print("Cancelled.")
                        return 1
                except EOFError:
                    return 1

        try:
            self._process(matches)
        except KeyboardInterrupt:
            print("\n\nCancelled by user. Files already placed stay where they are.")
            self.logger.warning("Cancelled by user (Ctrl+C).")
        s = self.stats
        print("=" * BANNER_WIDTH)
        print("SUMMARY".center(BANNER_WIDTH))
        print("=" * BANNER_WIDTH)
        if self.args.dry_run:
            label = "Files to move" if self.args.move else "Files to copy"
        else:
            label = "Files moved" if self.args.move else "Files copied"
        print(f"Matched folders   : {len(matches):,}")
        print(f"{label:<18}: {(s.moved if self.args.move else s.copied):,}")
        print(f"  of which renamed: {s.renamed:,}")
        print(f"Identical skipped : {s.identical:,}"
              + ("  (left in place; already in the destination)" if self.args.move and s.identical else ""))
        if s.skipped:
            print(f"Skipped (exists)  : {s.skipped:,}")
        if s.overwritten:
            print(f"Overwritten       : {s.overwritten:,}")
        if s.removed_dirs:
            print(f"Empty folders removed: {s.removed_dirs:,}")
        print(f"Errors            : {s.errors:,}")
        print(f"Duration          : {format_duration(time.monotonic() - self.start)}")
        print(f"Log file          : {self.logger.log_file_path}")  # type: ignore[attr-defined]
        if self.args.export_csv:
            self._write_csv()
            print(f"CSV report        : {self.args.export_csv}")
        print("=" * BANNER_WIDTH)
        self.logger.info("Done: %s", vars(s))
        self.logger.info("Command-line arguments: %s", vars(self.args))
        return 0 if s.errors == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    py = "python" if IS_WINDOWS else "python3"
    src = "I:\\Organized_Final" if IS_WINDOWS else "/Volumes/Photos/Organized"
    parser = argparse.ArgumentParser(
        prog="folder_reorg.py",
        description="Find every folder with a given name anywhere under SOURCE (for example in "
                    "different Year/Month folders) and merge their contents into one folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
Examples:

  Preview gathering every "Rajesh HDD" folder into {src}{os.sep}Rajesh HDD:
    {py} folder_reorg.py {src} --folder "Rajesh HDD" --dry-run

  Do it, moving the files and removing the emptied "Rajesh HDD" folders:
    {py} folder_reorg.py {src} --folder "Rajesh HDD" --move --remove-empty

  Gather into a folder of your choice, with a CSV report:
    {py} folder_reorg.py {src} --folder "Rajesh HDD" --dest "{src}{os.sep}People{os.sep}Rajesh HDD" --move --export-csv rajesh.csv

Notes:
  - Folder names match exactly, ignoring upper/lower case. Several names can be
    given comma-separated (e.g. "Rajesh HDD,Rajesh_HDD") and all go to one place.
  - The subfolders inside each match are kept; the year/month/etc. above it are not.
  - Byte-for-byte identical files are kept only once; different files with the
    same name are renamed (photo_1.jpg). Use --duplicate to change that.
""",
    )
    parser.add_argument("source", help="Folder to search, at any depth (e.g. your organized drive)")
    parser.add_argument("--folder", required=True, metavar="NAME",
                        help='Folder name to gather, e.g. "Rajesh HDD" (case-insensitive; '
                             'comma-separate several names to merge them together)')
    parser.add_argument("--dest", metavar="PATH",
                        help="Where to gather everything (default: SOURCE\\<NAME>)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--copy", action="store_true", help="Copy files (default; originals untouched)")
    mode.add_argument("--move", action="store_true", help="Move files instead of copying")
    parser.add_argument("--remove-empty", action="store_true",
                        help="With --move: remove the matched folders (and their subfolders) left empty. "
                             "Only empty folders are removed, never files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen; change nothing")
    parser.add_argument("--duplicate", choices=["rename", "skip", "overwrite"], default="rename",
                        help="When a DIFFERENT file already has that name: rename (default), skip, or overwrite")
    parser.add_argument("--no-hash-check", dest="hash_check", action="store_false",
                        help="Don't compare contents; treat same-named files as different "
                             "(faster, but identical copies are kept twice)")
    parser.add_argument("--exclude-folders", metavar="LIST",
                        help="Comma-separated folder names never searched (e.g. \"$RECYCLE.BIN,System Volume Information\")")
    parser.add_argument("--export-csv", metavar="PATH", help="Write a CSV report of every file and its outcome")
    parser.add_argument("--verbose", action="store_true", help="Print a line for every file")
    parser.add_argument("--log-file", metavar="PATH", help="Log file path (default: .\\logs\\file_organizer_<time>.log)")
    parser.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.folder.strip(' ",'):
        parser.error('--folder needs a folder name, e.g. --folder "Rajesh HDD"')
    if args.remove_empty and not args.move:
        parser.error("--remove-empty can only be used with --move")
    try:
        return FolderReorg(args).run()
    except KeyboardInterrupt:
        print("\n\nOperation cancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
