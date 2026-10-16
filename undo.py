"""Undo a previous File Organizer run (--undo).

Reads the record of a past run - either the CSV report it wrote with
--export-csv, or the log file every run writes to .\\logs\\ - and puts its
files back:

- Moved / Renamed & Moved: the file is moved back to its original path,
  under its original name. Missing original folders are recreated.
- Copied / Renamed & Copied: the copy is removed, but only when the
  original is still in place and the copy is byte-for-byte identical to it
  (SHA-256), so nothing unique is ever deleted.
- Overwritten: undone like a move or a copy (whichever it was), with a
  warning that the destination file it replaced cannot be brought back.

Nothing is ever overwritten: if something already sits at the original
path, that file is skipped and reported. Running the same undo twice is
safe - files already back are recognised and skipped. Folders that the
undo leaves empty in the destination are removed (only empty ones).

Part of File Organizer.
Author: Madhu Kumar <madhu.a@hotmail.com>
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from utils import format_duration, hash_file, long_path

MOVED = ("Moved", "Renamed & Moved")
COPIED = ("Copied", "Renamed & Copied")
OVERWRITTEN = ("Overwritten",)
DONE_STATUSES = MOVED + COPIED + OVERWRITTEN

_LOG_LINE = re.compile(
    r"\] (Moved|Renamed & Moved|Copied|Renamed & Copied|Overwritten): '(.*)' -> '(.*)'\s*$"
)
_DRY_LINE = re.compile(r"\] Would [A-Za-z &]+: '")

BANNER_WIDTH = 50


@dataclass
class Record:
    status: str
    source: str       # where the file originally was
    destination: str  # where the run put it


@dataclass
class UndoStats:
    restored: int = 0
    copies_removed: int = 0
    already_back: int = 0
    conflicts: int = 0
    not_found: int = 0
    copy_differs: int = 0
    overwritten_warnings: int = 0
    errors: int = 0
    folders_removed: int = 0


def load_records(path: Path) -> Tuple[List[Record], int]:
    """Return (records of files the run actually changed, number of dry-run lines seen)."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        first = fh.readline()
        fh.seek(0)
        if "Source Path" in first and "Destination Path" in first and "Status" in first:
            records, dry = [], 0
            for row in csv.DictReader(fh):
                status = (row.get("Status") or "").strip()
                if status.startswith("Would"):
                    dry += 1
                elif status in DONE_STATUSES and row.get("Destination Path"):
                    records.append(Record(status, row["Source Path"], row["Destination Path"]))
            return records, dry
        records, dry = [], 0
        for line in fh:
            match = _LOG_LINE.search(line)
            if match:
                records.append(Record(*match.groups()))
            elif _DRY_LINE.search(line):
                dry += 1
        return records, dry


def _exists(p: str) -> bool:
    return os.path.lexists(long_path(p))


def _same_content(a: str, b: str) -> bool:
    try:
        if os.path.getsize(long_path(a)) != os.path.getsize(long_path(b)):
            return False
    except OSError:
        return False
    ha = hash_file(a)
    return ha is not None and ha == hash_file(b)


def _plan(rec: Record) -> Tuple[str, str]:
    """(action, reason) for one record. action: restore | remove_copy | skip_*."""
    src_there, dst_there = _exists(rec.source), _exists(rec.destination)
    was_copy = rec.status in COPIED or (rec.status in OVERWRITTEN and src_there)
    if was_copy:
        if not dst_there:
            return "skip_gone", "copy no longer exists"
        if not src_there:
            return "skip_nosrc", "original no longer exists - keeping the copy"
        if not _same_content(rec.source, rec.destination):
            return "skip_differs", "copy differs from the original - keeping it"
        return "remove_copy", ""
    if src_there and not dst_there:
        return "skip_back", "already back in place"
    if src_there:
        return "skip_conflict", "something already exists at the original path"
    if not dst_there:
        return "skip_gone", "not found at the destination"
    return "restore", ""


def _remove_empty_parents(folder: Path, removed: set, logger) -> int:
    count = 0
    while True:
        key = str(folder)
        if key in removed or folder.parent == folder:
            return count
        try:
            os.rmdir(long_path(key))  # only succeeds when the folder is empty
        except OSError:
            return count
        removed.add(key)
        logger.info("Removed empty folder: '%s'", folder)
        count += 1
        folder = folder.parent


def run_undo(args, logger) -> int:
    start = time.monotonic()
    record_path = Path(args.undo)
    if not record_path.is_file():
        print(f"ERROR: Undo record not found: {record_path}")
        return 1
    try:
        records, dry_lines = load_records(record_path)
    except OSError as exc:
        print(f"ERROR: Cannot read {record_path}: {exc}")
        return 1

    print(f"Undo record : {record_path}")
    if not records:
        if dry_lines:
            print("\nThat record is from a --dry-run: nothing was copied or moved, so there is nothing to undo.")
        else:
            print("\nNo copied or moved files found in that record - nothing to undo.\n"
                  "(Use the CSV from --export-csv or the log file of a real, non --dry-run run.)")
        return 0

    plans = [(rec, *_plan(rec)) for rec in reversed(records)]  # newest first
    todo = [p for p in plans if p[1] in ("restore", "remove_copy")]
    n_restore = sum(1 for p in todo if p[1] == "restore")
    n_remove = len(todo) - n_restore
    overwritten = sum(1 for rec, act, _ in todo if rec.status in OVERWRITTEN)

    print("\n" + "=" * BANNER_WIDTH)
    print("UNDO".center(BANNER_WIDTH))
    print("=" * BANNER_WIDTH)
    print(f"Files in record          : {len(records):,}")
    print(f"Move back to original    : {n_restore:,}")
    if n_remove:
        print(f"Remove identical copies  : {n_remove:,}  (originals are still in place)")
    skipped = len(plans) - len(todo)
    if skipped:
        print(f"Skipped                  : {skipped:,}")
        reasons = {}
        for _, act, reason in plans:
            if act.startswith("skip"):
                reasons[reason] = reasons.get(reason, 0) + 1
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  - {reason}: {n:,}")
    if overwritten:
        print(f"\nNOTE: {overwritten:,} file(s) had overwritten an existing file at the destination;"
              "\nthe file that was overwritten cannot be brought back.")
    print()

    if args.dry_run or args.verbose:
        for rec, act, reason in plans:
            if act == "restore":
                print(f"{'Would move back' if args.dry_run else 'Move back'}: {rec.destination}\n    -> {rec.source}")
            elif act == "remove_copy":
                print(f"{'Would remove copy' if args.dry_run else 'Remove copy'}: {rec.destination}")
            elif args.verbose:
                print(f"Skip ({reason}): {rec.destination}")
        print()
    if args.dry_run:
        print("DRY RUN - nothing was changed.")
        return 0
    if not todo:
        print("Nothing to undo - every file is already back in place (or was skipped above).")
        return 0
    if not args.yes:
        try:
            if input("Undo these changes? [y/N]: ").strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 1
        except EOFError:
            return 1

    stats = UndoStats(overwritten_warnings=overwritten)
    removed_dirs: set = set()
    total = len(todo)
    try:
        for i, (rec, act, _) in enumerate(todo, start=1):
            try:
                if act == "restore":
                    Path(rec.source).parent.mkdir(parents=True, exist_ok=True)
                    if _exists(rec.source):  # appeared meanwhile: never overwrite
                        stats.conflicts += 1
                        logger.warning("Undo skipped (original path taken): '%s'", rec.source)
                        continue
                    shutil.move(long_path(rec.destination), long_path(rec.source))
                    stats.restored += 1
                    logger.info("Undo - moved back: '%s' -> '%s'", rec.destination, rec.source)
                else:
                    os.remove(long_path(rec.destination))
                    stats.copies_removed += 1
                    logger.info("Undo - removed copy: '%s' (original kept: '%s')", rec.destination, rec.source)
                stats.folders_removed += _remove_empty_parents(Path(rec.destination).parent, removed_dirs, logger)
            except (OSError, shutil.Error) as exc:
                stats.errors += 1
                logger.error("Undo failed for '%s': %s", rec.destination, exc)
                print(f"\nERROR: {rec.destination}: {exc}")
            if args.verbose:
                print(f"[{i}/{total}] {'Moved back' if act == 'restore' else 'Removed copy'}: {rec.source if act == 'restore' else rec.destination}")
            elif i % 25 == 0 or i == total:
                print(f"\rUndoing: {i:,}/{total:,}", end="", flush=True)
    except KeyboardInterrupt:
        print("\n\nUndo cancelled. Files already moved back stay back; run the same undo again to finish.")
        logger.warning("Undo cancelled by user (Ctrl+C).")
    print("\n" + "=" * BANNER_WIDTH)
    print("UNDO SUMMARY".center(BANNER_WIDTH))
    print("=" * BANNER_WIDTH)
    print(f"Moved back       : {stats.restored:,}")
    if stats.copies_removed:
        print(f"Copies removed   : {stats.copies_removed:,}")
    if stats.conflicts:
        print(f"Skipped (taken)  : {stats.conflicts:,}")
    print(f"Empty folders removed: {stats.folders_removed:,}")
    print(f"Errors           : {stats.errors:,}")
    print(f"Duration         : {format_duration(time.monotonic() - start)}")
    print(f"Log file         : {logger.log_file_path}")  # type: ignore[attr-defined]
    print("=" * BANNER_WIDTH)
    logger.info("Undo of '%s' complete: %s", record_path, vars(stats))
    return 0 if stats.errors == 0 else 2
