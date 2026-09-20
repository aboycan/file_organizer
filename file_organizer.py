#!/usr/bin/env python3
"""File Organizer - recursively scans a drive or folder and sorts files
into category folders (Images, Excel, Word, PDF, Videos, ...) by extension.

Run with -h/--help for the full list of options and examples.
"""

from __future__ import annotations

import argparse
import os
import sys

from organizer import Organizer
from utils import IS_WINDOWS, SizeParseError, parse_size


def _build_epilog() -> str:
    py = "python" if IS_WINDOWS else "python3"
    if IS_WINDOWS:
        src, dst = "D:\\", "D:\\Organized"
        src_folder = "D:\\MyFiles"
        spaced_src, spaced_dst = '"E:\\My External Drive"', '"D:\\Organized"'
        exclude_example = '"node_modules,.git,D:\\Backup\\Old"'
        csv_path = "D:\\Reports\\scan.csv"
        date_example = "Images\\2025\\Jan2025"
        space_note = 'Windows paths with spaces must be quoted, e.g. "E:\\My External Drive".'
    else:
        src, dst = "/Volumes/MyDrive", "/Volumes/MyDrive/Organized"
        src_folder = "~/MyFiles"
        spaced_src, spaced_dst = '"/Volumes/My External Drive"', '"~/Organized"'
        exclude_example = '"node_modules,.git,/mnt/backup/old"'
        csv_path = "~/Reports/scan.csv"
        date_example = "Images/2025/Jan2025"
        space_note = 'Paths with spaces must be quoted, e.g. "/Volumes/My External Drive".'

    return f"""\
Examples:

  Organize an entire drive/volume (copies files, originals are untouched):
    {py} file_organizer.py {src} {dst}

  Organize a single folder:
    {py} file_organizer.py {src_folder} {dst}

  Preview what would happen without touching anything:
    {py} file_organizer.py {src} {dst} --dry-run

  Move instead of copy, keep the original folder layout under each category:
    {py} file_organizer.py {src} {dst} --move --preserve-structure

  Sort into Category/Year/MonYear subfolders, e.g. {date_example}:
    {py} file_organizer.py {src} {dst} --date-folders

  Skip specific folders entirely (never scanned, so never touched):
    {py} file_organizer.py {src} {dst} --exclude-folders {exclude_example}

  Only organize pictures and PDFs larger than 1 MB, skip hidden files:
    {py} file_organizer.py {src} {dst} --categories Images,PDF --min-size 1MB --ignore-hidden

  Non-interactive run for scripting/scheduled tasks, with a CSV report:
    {py} file_organizer.py {src} {dst} --yes --export-csv {csv_path}

  Handle a path with spaces (quote it):
    {py} file_organizer.py {spaced_src} {spaced_dst}

Default behavior:
  - Operation is COPY. Original files are never deleted or modified.
  - Duplicate destination names are renamed (photo.jpg -> photo_1.jpg).
  - The source folder hierarchy is flattened under each category folder.
    Use --preserve-structure to keep the original nested layout instead.

Notes:
  - {space_note}
  - The destination is never scanned as part of the source, even when it is
    located inside the source (e.g. {src} -> {dst}).
  - Runs on Windows, macOS, and Linux. --ignore-system only has an effect on
    Windows (there's no equivalent "system file" attribute elsewhere).
"""


EPILOG = _build_epilog()


def size_arg(value: str) -> int:
    try:
        return parse_size(value)
    except SizeParseError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]) or "file_organizer.py",
        description=(
            "Recursively scan a drive/volume or folder, classify every accessible "
            "file by extension, and safely copy (or move) it into category folders. "
            "Runs on Windows, macOS, and Linux."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "source",
        help='Source drive/volume or folder to scan, e.g. D:\\ (Windows) or '
             '/Volumes/MyDrive (macOS) or /mnt/data (Linux)',
    )
    parser.add_argument("destination", help="Destination folder to organize files into")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--copy", action="store_true", help="Copy files to the destination (default)")
    mode.add_argument("--move", action="store_true", help="Move files instead of copying them")

    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and show what would happen without copying, moving, or creating anything",
    )
    parser.add_argument(
        "--preserve-structure", action="store_true",
        help="Keep the original folder hierarchy beneath each category folder "
             "(default is to flatten all files directly into the category folder)",
    )
    parser.add_argument(
        "--date-folders", action="store_true",
        help="Add Year\\MonYear subfolders (by file last-modified date) beneath each "
             "category folder, e.g. Images\\2025\\Jan2025\\photo.jpg. "
             "Combines with --preserve-structure (date folders come first). "
             "Categories marked date_folders:false in config.json (Executables, "
             "by default) are exempt, so an app's own files never get split "
             "into different date folders relative to each other.",
    )
    parser.add_argument(
        "--ignore-hidden", action="store_true",
        help="Skip hidden files: dotfiles everywhere, plus the Windows "
             "'hidden' attribute on Windows",
    )
    parser.add_argument(
        "--ignore-system", action="store_true",
        help="Skip files with the Windows 'system' attribute. Windows-only; "
             "has no effect on macOS/Linux (no equivalent attribute there)",
    )
    parser.add_argument(
        "--duplicate", choices=["skip", "rename", "overwrite"], default="rename",
        help="How to handle a destination file that already exists: "
             "skip it, rename the new file (default), or overwrite the existing one",
    )
    parser.add_argument(
        "--hash-check", action="store_true",
        help="Before applying --duplicate, compare file hashes and silently skip "
             "true duplicates (byte-for-byte identical files)",
    )
    parser.add_argument(
        "--categories",
        help="Comma-separated list of categories to process, e.g. Images,Excel,PDF "
             "(default: all categories)",
    )
    parser.add_argument(
        "--exclude-folders", metavar="LIST",
        help="Comma-separated folders to skip entirely (never scanned, so never "
             "copied/moved and left completely untouched). Each entry is either a "
             "bare folder name matched at any depth (e.g. node_modules,.git) or a "
             "full/relative path to one specific folder (e.g. D:\\Backup\\Old). "
             "Example: --exclude-folders \"node_modules,.git,D:\\Backup\\Old\"",
    )
    parser.add_argument(
        "--min-size", type=size_arg, default=None, metavar="SIZE",
        help="Only process files at least this size, e.g. 10MB, 500KB, 1GB",
    )
    parser.add_argument(
        "--max-size", type=size_arg, default=None, metavar="SIZE",
        help="Only process files at most this size, e.g. 5GB",
    )
    parser.add_argument(
        "--export-csv", metavar="PATH",
        help="Write a CSV report of every discovered file and its outcome to PATH",
    )
    parser.add_argument("--verbose", action="store_true", help="Show extra detail while running")
    parser.add_argument(
        "--log-file", metavar="PATH",
        help="Path to the log file (default: a timestamped file under "
             "./logs in the current directory)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Do not prompt for confirmation (for scripts/scheduled tasks)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.min_size is not None and args.max_size is not None and args.min_size > args.max_size:
        parser.error("--min-size cannot be greater than --max-size")

    try:
        organizer = Organizer(args)
        return organizer.run()
    except KeyboardInterrupt:
        print("\n\nOperation cancelled.")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # last-resort safety net so users never see a raw traceback
        print(f"\nUnexpected error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
