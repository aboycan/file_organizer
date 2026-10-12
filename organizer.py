"""Orchestrates the scan -> filter -> confirm -> copy/move pipeline.

Validates the source and destination, scans, applies the hidden / system /
category / size filters, shows the confirmation summary, checks free
space, then copies or moves each file (or only simulates it with
--dry-run). Destination paths are built here, including the optional
preserved folder structure and Year/MonYear date folders. Every file's
outcome goes to the log, and optionally to a CSV report. --find runs the
same scan and filters but only lists the matching files (optionally with
file and folder sizes) and changes nothing.

Part of File Organizer.
Author: Madhu Kumar <madhu.a@hotmail.com>
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from categorizer import Categorizer
from duplicate_handler import DuplicateHandler
from file_dates import SOURCES as DATE_SOURCES, original_date
from logger import setup_logger
from scanner import FileEntry, Scanner
from utils import (
    IS_WINDOWS,
    FolderSizer,
    date_folder_parts,
    format_duration,
    human_size,
    is_drive_root,
    is_same_volume,
    long_path,
    parse_extensions,
    path_is_within,
    path_key,
)

BANNER_WIDTH = 50


@dataclass
class Stats:
    discovered: int = 0
    selected: int = 0
    copied: int = 0
    moved: int = 0
    renamed: int = 0
    overwritten: int = 0
    skipped: int = 0
    identical: int = 0
    errors: int = 0
    filtered_category: int = 0
    filtered_extension: int = 0
    filtered_size: int = 0
    ignored_hidden: int = 0
    ignored_system: int = 0


def _term_width() -> int:
    try:
        return shutil.get_terminal_size((100, 20)).columns
    except OSError:
        return 100


def _print_progress(line: str) -> None:
    width = _term_width()
    sys.stdout.write("\r" + line[: max(width - 1, 10)].ljust(min(width - 1, len(line))))
    sys.stdout.flush()


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


class Organizer:
    def __init__(self, args):
        self.args = args
        self.stats = Stats()
        self.reserved: set[str] = set()
        self.csv_rows: List[dict] = []
        self.start_time = time.monotonic()
        self.logger = setup_logger(Path(args.log_file) if args.log_file else None)
        self.categorizer = Categorizer()
        self.duplicate_handler = DuplicateHandler(args.duplicate, hash_check=args.hash_check)
        self.date_sources: Dict[str, int] = {}  # --date-folders: where each file's date came from
        self.find_mode = bool(getattr(args, "find", False))  # --find: list only, change nothing
        self.extensions = parse_extensions(getattr(args, "extensions", None))
        self.destination: Optional[Path] = None

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def _validate(self) -> Optional[str]:
        args = self.args
        source = Path(args.source)

        if not source.exists():
            return f"Source path does not exist: {source}"
        if not source.is_dir():
            return f"Source path is not a directory: {source}"
        try:
            next(os.scandir(source), None)
        except OSError as exc:
            return f"Source path is not accessible: {source} ({exc})"

        if self.find_mode:  # nothing is written anywhere, so no destination is needed
            try:
                self.source = source.resolve()
            except OSError as exc:
                return f"Could not resolve paths: {exc}"
            return None

        destination = Path(args.destination)
        try:
            source_resolved = source.resolve()
            dest_resolved = destination.resolve()
        except OSError as exc:
            return f"Could not resolve paths: {exc}"

        if source_resolved == dest_resolved:
            return "Source and destination cannot be the same location."

        if args.dry_run:
            ancestor = dest_resolved
            while not ancestor.exists():
                if ancestor.parent == ancestor:
                    return f"Destination drive/path does not exist: {destination}"
                ancestor = ancestor.parent
            if not ancestor.is_dir():
                return f"Destination path is invalid: {destination}"
        else:
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return f"Cannot create destination directory: {destination} ({exc})"

        self.source = source_resolved
        self.destination = dest_resolved
        return None

    def _warn_root_drive(self) -> None:
        if not is_drive_root(self.source):
            return

        if IS_WINDOWS:
            system_drive = (os.environ.get("SystemDrive", "C:") + "\\").upper()
            is_system = str(self.source).upper() == system_drive
        else:
            # On Unix there's one root filesystem ("/"); treat scanning it
            # like scanning the Windows system drive.
            is_system = str(self.source) == "/"

        if is_system:
            print(
                f"\n*** WARNING: '{self.source}' is the system root. ***\n"
                "Scanning it will enumerate a very large number of files, including\n"
                "protected system files (use --ignore-system to skip those, where "
                "applicable).\n"
            )
        else:
            print(f"\nNote: '{self.source}' is a full drive/filesystem root — this may take a while.\n")

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #
    def _scan(self) -> List[FileEntry]:
        print("Scanning...\n")
        last_update = 0.0

        def progress(count: int, current_path: str) -> None:
            nonlocal last_update
            now = time.monotonic()
            if now - last_update < 0.05 and count % 200 != 0:
                return
            last_update = now
            _print_progress(f"Files discovered: {count:,}  |  {current_path}")

        exclude_names, exclude_paths = self._parse_exclude_folders()
        scanner = Scanner(
            source=self.source,
            categorizer=self.categorizer,
            logger=self.logger,
            destination=(
                self.destination
                if self.destination is not None and path_is_within(self.destination, self.source)
                else None
            ),
            progress_callback=progress,
            exclude_names=exclude_names,
            exclude_paths=exclude_paths,
        )
        entries: List[FileEntry] = []
        try:
            for entry in scanner.scan():
                entries.append(entry)
        except KeyboardInterrupt:
            print("\n\nCancellation requested during scan...")
            self.stats.discovered = len(entries)
            self._print_cancelled_summary()
            raise

        self.stats.discovered = len(entries)
        print(f"\n\nScan complete. Files discovered: {len(entries):,}")
        if scanner.dirs_excluded:
            print(f"  ({scanner.dirs_excluded} folder(s) skipped via --exclude-folders)")
        if scanner.errors:
            print(f"  ({scanner.errors} item(s) could not be accessed and were skipped — see log)")
        if len(entries) == 0 and scanner.dirs_skipped > 0:
            print(
                "\nWARNING: No files were found and some directories could not be read.\n"
                "The source may be disconnected or inaccessible.\n"
            )
        return entries

    def _print_category_summary(self, entries: List[FileEntry]) -> None:
        counts: Dict[str, int] = {name: 0 for name in self.categorizer.category_names()}
        for entry in entries:
            counts[entry.category] = counts.get(entry.category, 0) + 1
        print("\nCategory Summary")
        print("-" * 40)
        for name, count in counts.items():
            print(f"{name:<14}: {count:>8,}")
        self.logger.info("Category summary: %s", counts)

    def _parse_exclude_folders(self) -> tuple[set[str], set[str]]:
        """Split --exclude-folders into bare-name matches and resolved-path matches.

        An entry containing a path separator or drive letter (e.g.
        ``D:\\Backup\\Old`` or ``Old\\Stuff``) is treated as one specific
        folder; anything else (e.g. ``node_modules``) is matched by name at
        any depth in the tree.
        """
        names: set[str] = set()
        paths: set[str] = set()
        if not self.args.exclude_folders:
            return names, paths
        for raw in self.args.exclude_folders.split(","):
            entry = raw.strip().strip('"')
            if not entry:
                continue
            if "\\" in entry or "/" in entry or ":" in entry:
                p = Path(entry)
                if not p.is_absolute():
                    p = self.source / p
                try:
                    paths.add(path_key(p.resolve()))
                except OSError:
                    paths.add(path_key(p))
            else:
                names.add(entry.lower())
        return names, paths

    # ------------------------------------------------------------------ #
    # Filtering
    # ------------------------------------------------------------------ #
    def _apply_filters(self, entries: List[FileEntry]) -> List[FileEntry]:
        args = self.args
        wanted_categories = None
        if args.categories:
            requested = [c.strip() for c in args.categories.split(",") if c.strip()]
            canonical = {name.lower(): name for name in self.categorizer.category_names()}
            wanted_categories = set()
            for req in requested:
                if req.lower() not in canonical:
                    valid = ", ".join(self.categorizer.category_names())
                    raise SystemExit(f"ERROR: Unknown category '{req}'. Valid categories: {valid}")
                wanted_categories.add(canonical[req.lower()])

        selected: List[FileEntry] = []
        for entry in entries:
            if args.ignore_hidden and entry.hidden:
                self.stats.ignored_hidden += 1
                if args.export_csv:
                    self.csv_rows.append(self._base_row(entry, status="Ignored (Hidden)"))
                continue
            if args.ignore_system and entry.system:
                self.stats.ignored_system += 1
                if args.export_csv:
                    self.csv_rows.append(self._base_row(entry, status="Ignored (System)"))
                continue
            if wanted_categories is not None and entry.category not in wanted_categories:
                self.stats.filtered_category += 1
                if args.export_csv:
                    self.csv_rows.append(self._base_row(entry, status="Filtered (Category)"))
                continue
            if self.extensions is not None and entry.path.suffix.lower() not in self.extensions:
                self.stats.filtered_extension += 1
                if args.export_csv:
                    self.csv_rows.append(self._base_row(entry, status="Filtered (Extension)"))
                continue
            if args.min_size is not None and entry.size < args.min_size:
                self.stats.filtered_size += 1
                if args.export_csv:
                    self.csv_rows.append(self._base_row(entry, status="Filtered (Size)"))
                continue
            if args.max_size is not None and entry.size > args.max_size:
                self.stats.filtered_size += 1
                if args.export_csv:
                    self.csv_rows.append(self._base_row(entry, status="Filtered (Size)"))
                continue
            selected.append(entry)

        self.stats.selected = len(selected)
        return selected

    def _base_row(self, entry: FileEntry, status: str, destination: str = "", error: str = "") -> dict:
        return {
            "Source Path": str(entry.path),
            "File Name": entry.path.name,
            "Extension": entry.path.suffix,
            "Category": entry.category,
            "File Size": entry.size,
            "Destination Path": destination,
            "Status": status,
            "Error": error,
        }

    # ------------------------------------------------------------------ #
    # Destination path computation
    # ------------------------------------------------------------------ #
    def _dest_for(self, entry: FileEntry) -> Path:
        dest_dir = self.destination / entry.category

        if self.args.date_folders and self.categorizer.allows_date_folders(entry.category):
            when, source = original_date(entry.path, entry.created, entry.modified)
            year, month = date_folder_parts(when)
            self.date_sources[source] = self.date_sources.get(source, 0) + 1
            self.logger.debug("Date for '%s': %s (from %s)", entry.path, month, source)
            dest_dir = dest_dir / year / month

        if self.args.preserve_structure:
            try:
                rel_dir = entry.path.parent.relative_to(self.source)
            except ValueError:
                rel_dir = Path(".")
            dest_dir = dest_dir / rel_dir

        return dest_dir / entry.path.name

    # ------------------------------------------------------------------ #
    # Confirmation
    # ------------------------------------------------------------------ #
    def _print_summary_box(self) -> bool:
        args = self.args
        op = "MOVE" if args.move else "COPY"
        print("\n" + "=" * BANNER_WIDTH)
        print("FILE ORGANIZER".center(BANNER_WIDTH))
        print("=" * BANNER_WIDTH)
        print(f"Source      : {self.source}")
        print(f"Destination : {self.destination}")
        print(f"Operation   : {op}")
        print(f"Duplicate   : {args.duplicate.upper()}")
        print(f"Dry Run     : {'YES' if args.dry_run else 'NO'}")
        print()
        print(f"Files to process: {self.stats.selected:,}")
        excluded = (
            self.stats.filtered_category + self.stats.filtered_extension + self.stats.filtered_size
            + self.stats.ignored_hidden + self.stats.ignored_system
        )
        if excluded:
            print(f"Files excluded by filters: {excluded:,}")
            if self.stats.ignored_hidden:
                print(f"  - Hidden       : {self.stats.ignored_hidden:,}")
            if self.stats.ignored_system:
                print(f"  - System       : {self.stats.ignored_system:,}")
            if self.stats.filtered_category:
                print(f"  - Category     : {self.stats.filtered_category:,}")
            if self.stats.filtered_extension:
                print(f"  - Extension    : {self.stats.filtered_extension:,}")
            if self.stats.filtered_size:
                print(f"  - Size         : {self.stats.filtered_size:,}")
        print()
        if not args.move:
            print("Original source files will NOT be deleted or")
            print("modified in COPY mode.")
        else:
            print("MOVE mode: source files WILL be removed after a")
            print("successful transfer to the destination.")
        print()

        if args.dry_run:
            return True
        if args.yes:
            return True
        return _confirm("Continue? [y/N]: ")

    def _check_disk_space(self, selected: List[FileEntry]) -> bool:
        if self.args.move and is_same_volume(self.source, self.destination):
            return True  # same-volume move is a rename: no extra space needed
        # A cross-volume move (e.g. D:\ -> an external E:\ drive, or across
        # mount points on Mac/Linux) is really a copy-then-delete under the
        # hood, so it needs free space just like a copy.
        total_needed = sum(e.size for e in selected)
        ancestor = self.destination
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        try:
            free = shutil.disk_usage(ancestor).free
        except OSError:
            return True
        if total_needed > free:
            print(
                f"\nWARNING: Estimated space needed ({human_size(total_needed)}) exceeds "
                f"free space on destination ({human_size(free)})."
            )
            if self.args.yes:
                print("Aborting because --yes was given (cannot prompt in non-interactive mode).")
                self.logger.error("Insufficient disk space; aborted in non-interactive mode.")
                return False
            return _confirm("Continue anyway? [y/N]: ")
        return True

    # ------------------------------------------------------------------ #
    # Dry run
    # ------------------------------------------------------------------ #
    def _run_dry(self, selected: List[FileEntry]) -> None:
        print("\nDRY RUN MODE")
        print("No files will be copied or moved.\n")
        print(f"Source      : {self.source}")
        print(f"Destination : {self.destination}\n")

        for entry in selected:
            dest_path = self._dest_for(entry)
            resolution = self.duplicate_handler.resolve(entry.path, dest_path, self.reserved)
            print(str(entry.path))
            print(f"  Category    : {entry.category}")
            if resolution.action == "skip":
                print(f"  Destination : {resolution.final_path}  (SKIPPED - already exists)")
                self.stats.skipped += 1
                status = "Would Skip (exists)"
            elif resolution.action == "identical":
                print(f"  Destination : {resolution.final_path}  (SKIPPED - identical file exists)")
                self.stats.identical += 1
                status = "Would Skip (identical)"
            elif resolution.action == "overwrite":
                print(f"  Destination : {resolution.final_path}  (would OVERWRITE)")
                self.stats.overwritten += 1
                status = "Would Overwrite"
            else:
                renamed = resolution.final_path.name != entry.path.name
                print(f"  Destination : {resolution.final_path}")
                if renamed:
                    self.stats.renamed += 1
                    status = "Would Rename & Copy"
                else:
                    status = "Would Copy"
                self.stats.copied += 1
            print()

            self.logger.info("%s: '%s' -> '%s'", status, entry.path, resolution.final_path)

            if self.args.export_csv:
                self.csv_rows.append(
                    self._base_row(entry, status=status, destination=str(resolution.final_path))
                )

    # ------------------------------------------------------------------ #
    # Real processing
    # ------------------------------------------------------------------ #
    def _run_real(self, selected: List[FileEntry]) -> None:
        op_label = "Moved" if self.args.move else "Copied"
        verbose = self.args.verbose
        print("\nProcessing...\n")
        total = len(selected)
        consecutive_errors = 0
        last_update = 0.0

        try:
            for index, entry in enumerate(selected, start=1):
                dest_path = self._dest_for(entry)
                resolution = self.duplicate_handler.resolve(entry.path, dest_path, self.reserved)
                final_path = resolution.final_path
                status = ""
                error_msg = ""

                if resolution.action == "skip":
                    self.stats.skipped += 1
                    status = "Skipped (exists)"
                    self.logger.info("%s: '%s' -> '%s'", status, entry.path, final_path)
                elif resolution.action == "identical":
                    self.stats.identical += 1
                    status = "Skipped (identical)"
                    self.logger.info("%s: '%s' -> '%s'", status, entry.path, final_path)
                else:
                    try:
                        final_path.parent.mkdir(parents=True, exist_ok=True)
                        src_str = long_path(str(entry.path))
                        dst_str = long_path(str(final_path))

                        if resolution.action == "overwrite" and final_path.exists():
                            os.remove(dst_str)

                        if self.args.move:
                            shutil.move(src_str, dst_str)
                            self.stats.moved += 1
                        else:
                            shutil.copy2(src_str, dst_str)
                            self.stats.copied += 1

                        if resolution.action == "overwrite":
                            self.stats.overwritten += 1
                            status = "Overwritten"
                        elif final_path.name != entry.path.name:
                            self.stats.renamed += 1
                            status = "Renamed & Copied" if not self.args.move else "Renamed & Moved"
                        else:
                            status = "Copied" if not self.args.move else "Moved"
                        consecutive_errors = 0
                        self.logger.info("%s: '%s' -> '%s'", status, entry.path, final_path)
                    except (OSError, shutil.Error) as exc:
                        self.stats.errors += 1
                        consecutive_errors += 1
                        status = "Error"
                        error_msg = str(exc)
                        self.logger.error("Failed to process '%s' -> '%s': %s", entry.path, final_path, exc)
                        if consecutive_errors >= 25:
                            print(
                                "\n\nToo many consecutive errors — the destination may be "
                                "unavailable. Aborting remaining files."
                            )
                            self.logger.error("Aborting: %d consecutive errors.", consecutive_errors)
                            if self.args.export_csv:
                                self.csv_rows.append(
                                    self._base_row(entry, status=status, destination=str(final_path), error=error_msg)
                                )
                            break

                if self.args.export_csv:
                    self.csv_rows.append(
                        self._base_row(entry, status=status, destination=str(final_path), error=error_msg)
                    )

                if verbose:
                    detail = f"[{index}/{total}] {status}: {entry.path} -> {final_path}"
                    if error_msg:
                        detail += f"  ({error_msg})"
                    print(detail)
                else:
                    now = time.monotonic()
                    if now - last_update > 0.05 or index == total:
                        last_update = now
                        pct = int(index / total * 100) if total else 100
                        _print_progress(
                            f"Progress: {pct}% ({index:,}/{total:,})  "
                            f"{op_label} {self.stats.copied + self.stats.moved:,}  "
                            f"Renamed {self.stats.renamed:,}  Skipped {self.stats.skipped + self.stats.identical:,}  "
                            f"Errors {self.stats.errors:,}  |  {entry.path.name}"
                        )
        except KeyboardInterrupt:
            print("\n\nCancellation requested...")
            self._print_cancelled_summary()
            raise

        print()

    # ------------------------------------------------------------------ #
    # Summaries
    # ------------------------------------------------------------------ #
    def _print_cancelled_summary(self) -> None:
        print("\nOperation cancelled.\n")
        print(f"Files discovered : {self.stats.discovered:,}")
        print(f"Files copied     : {self.stats.copied:,}")
        print(f"Files moved      : {self.stats.moved:,}")
        print(f"Files renamed    : {self.stats.renamed:,}")
        print(f"Files skipped    : {self.stats.skipped + self.stats.identical:,}")
        print(f"Errors           : {self.stats.errors:,}")
        self.logger.warning("Operation cancelled by user (Ctrl+C).")
        self._finalize_log(cancelled=True)
        self._write_csv()

    def _print_final_summary(self) -> None:
        elapsed = time.monotonic() - self.start_time
        print("=" * BANNER_WIDTH)
        print("SUMMARY".center(BANNER_WIDTH))
        print("=" * BANNER_WIDTH)
        print(f"Files discovered : {self.stats.discovered:,}")
        print(f"Files selected   : {self.stats.selected:,}")
        if not self.args.dry_run:
            print(f"Files copied     : {self.stats.copied:,}")
            print(f"Files moved      : {self.stats.moved:,}")
        print(f"Files renamed    : {self.stats.renamed:,}")
        print(f"Files skipped    : {self.stats.skipped + self.stats.identical:,}")
        print(f"Errors           : {self.stats.errors:,}")
        if self.date_sources:
            print(f"Dates taken from : {self._date_sources_text()}")
        print(f"Duration         : {format_duration(elapsed)}")
        print(f"Log file         : {self.logger.log_file_path}")  # type: ignore[attr-defined]
        if self.args.export_csv:
            print(f"CSV report       : {self.args.export_csv}")
        print("=" * BANNER_WIDTH)

        if not self.args.export_csv and not self.args.verbose:
            print(
                "\nTip: every file's source, destination, and outcome was written to\n"
                "the log file above. Re-run with --export-csv <path> for a\n"
                "spreadsheet-friendly report, or --verbose to see each file live."
            )

    def _finalize_log(self, cancelled: bool = False) -> None:
        elapsed = time.monotonic() - self.start_time
        self.logger.info("=" * 60)
        self.logger.info("Run %s", "CANCELLED" if cancelled else "COMPLETE")
        self.logger.info("Source: %s", self.source)
        self.logger.info("Destination: %s", self.destination)
        self.logger.info("Operation: %s", "MOVE" if self.args.move else "COPY")
        self.logger.info("Command-line arguments: %s", vars(self.args))
        self.logger.info("Files discovered: %d", self.stats.discovered)
        self.logger.info("Files selected: %d", self.stats.selected)
        self.logger.info("Files copied: %d", self.stats.copied)
        self.logger.info("Files moved: %d", self.stats.moved)
        self.logger.info("Files renamed: %d", self.stats.renamed)
        self.logger.info("Files skipped: %d", self.stats.skipped + self.stats.identical)
        self.logger.info("Errors: %d", self.stats.errors)
        if self.date_sources:
            self.logger.info("Dates taken from: %s", self._date_sources_text())
        self.logger.info("Total processing duration: %s", format_duration(elapsed))

    def _date_sources_text(self) -> str:
        return ", ".join(
            f"{self.date_sources[s]:,} {s}" for s in DATE_SOURCES if self.date_sources.get(s)
        )

    def _write_csv(self, fieldnames: Optional[List[str]] = None) -> None:
        if not self.args.export_csv:
            return
        csv_path = Path(self.args.export_csv)
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                fieldnames = fieldnames or [
                    "Source Path", "File Name", "Extension", "Category",
                    "File Size", "Destination Path", "Status", "Error",
                ]
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.csv_rows)
            self.logger.info("CSV report written to: %s", csv_path)
        except OSError as exc:
            self.logger.error("Could not write CSV report '%s': %s", csv_path, exc)
            print(f"\nWARNING: Could not write CSV report: {exc}")

    # ------------------------------------------------------------------ #
    # Find mode (--find): list matching files, change nothing
    # ------------------------------------------------------------------ #
    def _run_find(self, selected: List[FileEntry]) -> None:
        args = self.args
        show_size = bool(getattr(args, "show_size", False))
        selected = sorted(selected, key=lambda e: path_key(e.path))
        criteria = []
        if args.categories:
            criteria.append(f"categories {args.categories}")
        if self.extensions:
            criteria.append("extensions " + ", ".join(sorted(self.extensions)))
        print("\nFIND MODE - nothing will be copied, moved or deleted.")
        print(f"Source   : {self.source}")
        print(f"Matching : {'; '.join(criteria) if criteria else 'all files'}")
        excluded = (
            self.stats.filtered_category + self.stats.filtered_extension + self.stats.filtered_size
            + self.stats.ignored_hidden + self.stats.ignored_system
        )
        print(f"Found    : {len(selected):,} file(s)" + (f"  ({excluded:,} other files skipped)" if excluded else ""))

        folder_sizes: Dict[str, int] = {}
        if show_size and selected:
            print("\nMeasuring folder sizes (each folder's total, including subfolders)...")
            sizer = FolderSizer()
            for entry in selected:
                key = path_key(entry.path.parent)
                if key not in folder_sizes:
                    folder_sizes[key] = sizer.size(entry.path.parent)

        print()
        if show_size:
            print(f"{'File size':>12}  {'Folder size':>12}  File")
            print(f"{'-' * 12}  {'-' * 12}  {'-' * 40}")
        self.csv_rows = []  # the find report replaces the organizer's per-file rows
        total_size = 0
        for entry in selected:
            total_size += entry.size
            if show_size:
                fsize = folder_sizes[path_key(entry.path.parent)]
                print(f"{human_size(entry.size):>12}  {human_size(fsize):>12}  {entry.path}")
                self.logger.info("Found: '%s' (%s) in folder '%s' (%s)", entry.path,
                                 human_size(entry.size), entry.path.parent, human_size(fsize))
            else:
                print(str(entry.path))
                self.logger.info("Found: '%s'", entry.path)
            if args.export_csv:
                row = {
                    "Source Path": str(entry.path),
                    "File Name": entry.path.name,
                    "Extension": entry.path.suffix,
                    "Category": entry.category,
                    "File Size": entry.size,
                    "Folder": str(entry.path.parent),
                }
                if show_size:
                    row["Folder Size"] = folder_sizes[path_key(entry.path.parent)]
                self.csv_rows.append(row)

        elapsed = time.monotonic() - self.start_time
        print("\n" + "=" * BANNER_WIDTH)
        print("FIND SUMMARY".center(BANNER_WIDTH))
        print("=" * BANNER_WIDTH)
        print(f"Files scanned    : {self.stats.discovered:,}")
        print(f"Files found      : {len(selected):,}")
        print(f"Total size found : {human_size(total_size)}")
        if show_size:
            print(f"In folders       : {len(folder_sizes):,}")
        print(f"Duration         : {format_duration(elapsed)}")
        print(f"Log file         : {self.logger.log_file_path}")  # type: ignore[attr-defined]
        if args.export_csv:
            print(f"CSV report       : {args.export_csv}")
        print("=" * BANNER_WIDTH)

        self.logger.info("=" * 60)
        self.logger.info("Find COMPLETE (nothing copied, moved or deleted)")
        self.logger.info("Source: %s", self.source)
        self.logger.info("Command-line arguments: %s", vars(args))
        self.logger.info("Files scanned: %d", self.stats.discovered)
        self.logger.info("Files found: %d (%s)", len(selected), human_size(total_size))
        self.logger.info("Total duration: %s", format_duration(elapsed))
        if args.export_csv:
            fields = ["Source Path", "File Name", "Extension", "Category", "File Size", "Folder"]
            if show_size:
                fields.append("Folder Size")
            self._write_csv(fields)

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def run(self) -> int:
        error = self._validate()
        if error:
            print(f"ERROR: {error}")
            self.logger.error(error)
            return 1

        self._warn_root_drive()

        try:
            entries = self._scan()
        except KeyboardInterrupt:
            return 130

        self._print_category_summary(entries)

        try:
            selected = self._apply_filters(entries)
        except SystemExit as exc:
            print(str(exc))
            return 1

        if self.find_mode:
            try:
                self._run_find(selected)
            except KeyboardInterrupt:
                print("\n\nOperation cancelled.")
                self.logger.warning("Find cancelled by user (Ctrl+C).")
                return 130
            return 0

        if not self._print_summary_box():
            print("\nOperation cancelled by user.")
            self.logger.info("Operation cancelled at confirmation prompt.")
            return 1

        if not self.args.dry_run:
            if not self._check_disk_space(selected):
                return 1

        try:
            if self.args.dry_run:
                self._run_dry(selected)
            else:
                self._run_real(selected)
        except KeyboardInterrupt:
            return 130

        self._print_final_summary()
        self._finalize_log(cancelled=False)
        self._write_csv()
        return 0
