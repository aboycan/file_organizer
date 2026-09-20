# File Organizer

A safe, dependency-free command-line utility, for Windows, macOS, and Linux,
that recursively scans a drive/volume or folder — no matter how deeply
nested — and organizes every accessible file into category folders (Images,
Excel, Word, PDF, Videos, ...) based on its extension.

The default operation is a **non-destructive copy**: your original files are
never deleted or modified.

## What it does

1. Recursively walks the source (a whole drive/volume like `D:\` or
   `/Volumes/MyDrive`, or any folder), descending into every accessible
   subfolder regardless of depth.
2. Classifies each file by its extension using `config.json`.
3. Copies (or, optionally, moves) each file into `<destination>/<Category>/...`.
4. Never silently overwrites an existing file — duplicates are skipped,
   renamed, or overwritten only if you explicitly ask for that.
5. Shows a scan summary, asks for confirmation, then reports live progress
   and a final summary. Everything is also written to a log file, and
   optionally to a CSV report.

## Installation

Requires Python 3.11+ on Windows, macOS, or Linux. No third-party packages
are needed (see [requirements.txt](requirements.txt)).

```bash
git clone <this-repo>
cd file_organizer
python file_organizer.py --help   # Windows
python3 file_organizer.py --help  # macOS/Linux
```

Or just copy the `file_organizer/` folder anywhere and run it the same way.
On macOS/Linux you can also make it directly executable:
`chmod +x file_organizer.py && ./file_organizer.py --help` (it already has a
`#!/usr/bin/env python3` shebang).

## Basic usage

```text
python file_organizer.py <SOURCE> <DESTINATION> [options]
```

Works the same way from Command Prompt, PowerShell, macOS Terminal, or a
Linux shell.

```bash
python file_organizer.py D:\ D:\Organized
```

```bash
python file_organizer.py D:\MyFiles D:\Organized
```

On macOS/Linux, use `python3` and forward-slash paths:

```bash
python3 file_organizer.py /Volumes/MyDrive /Volumes/MyDrive/Organized
```

```bash
python3 file_organizer.py ~/MyFiles ~/Organized
```

Paths containing spaces must be quoted:

```bash
python file_organizer.py "E:\My External Drive" "D:\Organized"
```
```bash
python3 file_organizer.py "/Volumes/My External Drive" "/Volumes/Backup/Organized"
```

**Gotcha with quoted drive roots (Command Prompt only):** in `cmd.exe`,
don't quote a path that ends in a single backslash, like `"F:\"` — it
treats `\"` as an *escaped quote*, not "path + closing quote," so the quote
never actually closes and every argument after it gets swallowed into that
one path (you'll see `error: the following arguments are required:
destination` even though you did pass one). Since `F:\` has no spaces, just
leave it unquoted:

```bash
python file_organizer.py F:\ D:\Organized
```

If you do need to quote a drive root in Command Prompt, double the trailing
backslash instead: `"F:\\"`. PowerShell isn't affected — it parses `"F:\"`
correctly on its own.

## Command-line options

| Option | Description |
|---|---|
| `--copy` | Copy files to the destination (default). |
| `--move` | Move files instead of copying them. |
| `--dry-run` | Scan and report what *would* happen; makes zero filesystem changes. |
| `--preserve-structure` | Keep the original folder hierarchy beneath each category folder. Default is to flatten everything directly into the category folder. |
| `--date-folders` | Add `Year\MonYear` subfolders (by file last-modified date) beneath each category folder, e.g. `Images\2025\Jan2025\photo.jpg`. Combines with `--preserve-structure` (date folders come first). |
| `--ignore-hidden` | Skip files with the Windows "hidden" attribute (and dotfiles). |
| `--ignore-system` | Skip files with the Windows "system" attribute. |
| `--duplicate {skip,rename,overwrite}` | How to handle a destination file that already exists. Default: `rename`. |
| `--hash-check` | Before applying `--duplicate`, compare SHA-256 hashes and silently skip files that are byte-for-byte identical to what's already at the destination. |
| `--categories LIST` | Comma-separated list of categories to process, e.g. `Images,Excel,PDF`. Other files are left untouched in the source. |
| `--exclude-folders LIST` | Comma-separated folders to skip entirely — never scanned, so never copied/moved. Each entry is a bare name matched at any depth (`node_modules`) or a specific path (`D:\Backup\Old`). |
| `--min-size SIZE` | Only process files at least this size, e.g. `10MB`. |
| `--max-size SIZE` | Only process files at most this size, e.g. `5GB`. |
| `--export-csv PATH` | Write a CSV report of every discovered file and its outcome. |
| `--verbose` | Print a live line for every file processed (source, destination, outcome) instead of just the progress bar. The log file always has this detail regardless of this flag. |
| `--log-file PATH` | Path to the log file. Default: `.\logs\file_organizer_<timestamp>.log`. |
| `--yes` | Skip the confirmation prompt (for scripts / Scheduled Tasks). |

Size values accept `B`, `KB`, `MB`, `GB`, `TB` (case-insensitive), e.g.
`500KB`, `1.5GB`. A bare number is treated as bytes.

Run `python file_organizer.py --help` for the full help text with examples.

## Recursive scanning

The scanner uses an explicit-stack traversal built on `os.scandir()` — not
plain recursion — so there is **no fixed depth limit**. A file 50 levels deep
is found exactly like one at the top level. It is also resilient:

- A folder that can't be read (permissions, a broken junction, a disconnected
  network share) is logged and skipped; the scan continues with everything
  else.
- Symlinks/junctions are not followed as directories, which avoids infinite
  loops from circular links.
- The destination folder is **never** scanned as part of the source, even
  when it lives inside the source (the common `D:\` → `D:\Organized` case).

## Dry run

```bash
python file_organizer.py D:\ D:\Organized --dry-run
```

Dry run scans, categorizes, computes destination paths, and simulates
duplicate handling (so renamed names like `photo_1.jpg` are accurate) — but
it copies nothing, moves nothing, deletes nothing, and doesn't even create
the destination folder. It combines well with `--export-csv` to get a full
report before touching anything.

## Copy vs. Move

- **Copy** (default, `--copy`): uses `shutil.copy2()`, which preserves
  metadata (timestamps, etc.). The source file is untouched.
- **Move** (`--move`): moves the file to the destination. The source file is
  removed only after a **successful** transfer.

The selected operation is always shown in the confirmation summary before
anything happens.

## Duplicate handling

If the computed destination path already exists, the utility never silently
overwrites it. Choose a policy with `--duplicate`:

- `rename` (default): keeps both files — `photo.jpg`, then `photo_1.jpg`,
  `photo_2.jpg`, and so on, guaranteed not to collide with anything already
  on disk or already placed earlier in the same run.
- `skip`: leaves the existing destination file alone; the source file is not
  copied/moved.
- `overwrite`: replaces the existing destination file.

Add `--hash-check` to have the utility compare file contents (SHA-256) before
applying the policy above — a source file that is byte-for-byte identical to
what's already at the destination is simply skipped, regardless of filename.

## Preserve structure

By default the source hierarchy is **flattened** under each category folder:

```text
D:\Work\Reports\2026\Final\report.xlsx  ->  D:\Organized\Excel\report.xlsx
```

With `--preserve-structure`, the original folders are kept beneath the
category folder:

```text
D:\Work\Reports\2026\Final\report.xlsx  ->  D:\Organized\Excel\Work\Reports\2026\Final\report.xlsx
```

## Date folders

`--date-folders` adds `Year\MonYear` subfolders beneath each category
folder, based on the file's last-modified date:

```text
D:\Personal\Photos\vacation.jpg  ->  D:\Organized\Images\2025\Jan2025\vacation.jpg
```

It composes with `--preserve-structure` — the date folders come first, the
original subfolders (if any) come after:

```bash
python file_organizer.py D:\ D:\Organized --date-folders --preserve-structure
```

```text
D:\Personal\Photos\2024\vacation.jpg  ->  D:\Organized\Images\2025\Jan2025\Personal\Photos\2024\vacation.jpg
```

Notes:
- The date used is the file's **last-modified date** (`st_mtime`), not
  "date taken" from photo EXIF data — this works uniformly for every file
  type without needing extra libraries, and unlike creation date it
  survives being copied between drives (the modified timestamp travels
  with the file; creation date typically resets to "now" on copy). For
  photos edited after the fact, this reflects the edit date rather than
  when the photo was originally taken.
- A file with unreadable/corrupt date metadata falls back to an
  `UnknownDate\UnknownDate` folder instead of failing.
- **`Executables` is exempt from `--date-folders` by default.** An app's
  own files (e.g. `app.exe` and a `bin\helper.exe` it depends on) are often
  modified on very different dates, so date-sorting them independently
  would scatter them into different Year/MonYear folders *relative to each
  other* — breaking the app even with `--preserve-structure` keeping their
  relative paths intact. Executables always land at
  `Executables\<preserved-path>\...` with no date split, regardless of
  `--date-folders`. This is controlled per-category in `config.json` via a
  `"date_folders": false` flag (see Configuration below) — you can apply
  the same exemption to other categories by editing it, or remove it from
  `Executables` if you're only ever organizing standalone installers
  rather than already-installed applications.
- **This only fixes the date-splitting problem, not app breakage in
  general.** Moving or copying an installed application's files at all
  changes their absolute path — any shortcut, PATH entry, or registry
  entry pointing at the *original* location still breaks, regardless of
  `--date-folders` or `--preserve-structure`. If your drive has a mix of
  loose standalone installer files (safe to organize) and already-installed
  application folders (risky to touch), exclude the latter from the scan
  entirely instead, e.g. `--exclude-folders "Program Files,Program Files (x86)"`.

## External-drive usage

Works the same way against USB drives, external HDDs/SSDs, a mapped drive
letter (Windows), or a mounted volume (`/Volumes/...` on macOS,
`/mnt/...`/`/media/...` on Linux) — just point `SOURCE` at the root or a
folder on it. If a drive/volume is disconnected mid-scan, the error is
logged and the scan continues with whatever else is reachable; if the
destination disappears during processing, individual copy errors are
logged and, after enough consecutive failures, remaining processing is
safely aborted rather than looping on a dead drive.

## Large-file / large-drive handling

- Files are streamed by the OS (`shutil.copy2`) — never loaded fully into
  memory — so multi-gigabyte videos and archives copy efficiently.
- The scanner is generator-based and iterative, so it comfortably handles
  drives with hundreds of thousands of files.
- Console progress updates are throttled so they don't slow down processing
  on very large jobs.

## Error handling

Permission errors, locked files, files deleted mid-scan, disconnected
drives, invalid paths, and other filesystem surprises are caught per-item,
logged, and counted as errors — they never abort the whole run. A summary of
error counts is shown at the end and full details go to the log file.

Press **Ctrl+C** at any time to cancel. You'll get a clean summary (no Python
traceback) of what was discovered/copied/moved before cancellation. Nothing
already copied is rolled back, and the source is never touched by
cancellation.

## Logging and per-file details

Every run writes a detailed log containing the timestamp, source,
destination, operation, full command-line arguments, discovery/processing
counts, total duration — **and one line per file** showing exactly what
happened to it, e.g.:

```text
2026-09-14 12:15:46 [INFO    ] Copied: 'D:\Photos\2025\photo.jpg' -> 'D:\Organized\Images\photo.jpg'
2026-09-14 12:15:46 [INFO    ] Renamed & Copied: 'D:\Backup\photo.jpg' -> 'D:\Organized\Images\photo_1.jpg'
2026-09-14 12:15:46 [ERROR   ] Failed to process 'D:\Locked\video.mp4' -> 'D:\Organized\Videos\video.mp4': ...
```

This is written **every run**, regardless of `--verbose` or `--export-csv` —
so after any copy/move you can always open the log file named in the final
summary ("Log file : ...") to see exactly where each file ended up. If
`--log-file` is not given, a timestamped log is created under `.\logs\` in
the current directory.

```bash
python file_organizer.py D:\ D:\Organized --log-file D:\Logs\file_organizer.log
```

For a live, on-screen version of the same per-file detail while the run is
happening (instead of just the compact progress bar), add `--verbose`:

```text
[1/3] Copied: D:\Photos\photo.jpg -> D:\Organized\Images\photo.jpg
[2/3] Renamed & Copied: D:\Backup\photo.jpg -> D:\Organized\Images\photo_1.jpg
[3/3] Copied: D:\Reports\report.pdf -> D:\Organized\PDF\report.pdf
```

For a structured, spreadsheet-friendly version of the same information, use
`--export-csv` (see below) — it's the best option when you want to filter,
sort, or search the results afterwards.

## CSV export

```bash
python file_organizer.py D:\ D:\Organized --dry-run --export-csv D:\Reports\scan_results.csv
```

Produces one row per discovered file with:

`Source Path, File Name, Extension, Category, File Size, Destination Path, Status, Error`

`Status` reflects the actual (or, in dry-run, simulated) outcome — `Copied`,
`Moved`, `Renamed & Copied`, `Skipped (exists)`, `Skipped (identical)`,
`Overwritten`, `Ignored (Hidden)`, `Ignored (System)`, `Filtered (Category)`,
`Filtered (Size)`, or `Error` (with the message in the `Error` column).

## Configuration

Extension-to-category mappings live in [config.json](config.json), loaded at
startup. Each category is either a plain list of extensions (the common
case):

```json
{
    "Images": [".jpg", ".jpeg", ".png", ".gif"],
    "Excel": [".xls", ".xlsx", ".xlsm"],
    ...
    "Other": []
}
```

...or an object with `"extensions"` and `"date_folders": false`, for a
category that should never be split into Year/MonYear subfolders even when
`--date-folders` is passed (see "Date folders" above for why — this is how
`Executables` avoids scattering an installed app's own files by date):

```json
{
    "Executables": {
        "extensions": [".exe", ".msi"],
        "date_folders": false
    }
}
```

Omitting `"date_folders"` (or using the plain-list form) means "yes,
`--date-folders` applies normally" — that's the default for every other
category.

### Adding new extensions

Just edit `config.json` — add the extension (including the leading dot, any
case) to the relevant category's list, or add a whole new category. No code
changes required. Any extension not listed anywhere falls into `Other`.

## Category filtering

```bash
python file_organizer.py D:\ D:\Organized --categories Images,Excel,PDF
```

Only the listed categories are processed; everything else is left untouched
in the source (and shown as `Filtered (Category)` in the CSV/dry-run report).

## Excluding folders

`--exclude-folders` skips entire folders **before** they're scanned, so
anything inside them is never discovered, never categorized, and never
copied/moved — not even looked at. Each comma-separated entry is one of:

- A **bare name**, matched at any depth anywhere under the source, e.g.
  `node_modules` skips every `node_modules` folder no matter how deeply
  nested.
- A **specific path** (contains `\`, `/`, or `:`), which only skips that one
  folder. Relative paths are resolved against `SOURCE`.

```bash
python file_organizer.py D:\ D:\Organized --exclude-folders "node_modules,.git,$RECYCLE.BIN,D:\Backup\Old"
```

Good candidates when scanning a whole drive: `node_modules`, `.git`,
`$RECYCLE.BIN`, `System Volume Information` (the last two are Windows
system folders that are usually inaccessible anyway, but excluding them
explicitly avoids the wasted scan attempt and any permission-error noise in
the log).

Since excluded folders are never scanned, this works identically under
`--copy`, `--move`, and `--dry-run` — the excluded content is always left
exactly as-is.

## Platform notes

The tool behaves identically across Windows, macOS, and Linux for the core
workflow (scan, categorize, copy/move, duplicates, filters, logging). A few
things differ because the underlying OS does:

- **Hidden files** (`--ignore-hidden`): on Windows, matches the actual
  "hidden" file attribute; on macOS/Linux, there's no such attribute, so it
  matches dotfiles (`.bashrc`, `.DS_Store`, etc.) instead. Both also always
  treat dotfiles as hidden as a fallback.
- **System files** (`--ignore-system`): Windows-only concept (the "system"
  file attribute). It's a no-op on macOS/Linux — there's nothing there to
  match, so nothing gets excluded by it.
- **Filename case sensitivity**: Windows and default macOS volumes
  (APFS/HFS+) are case-insensitive, so `Photo.jpg` and `photo.jpg` are the
  same file/name for duplicate-detection purposes. Linux (ext4, the common
  default) is case-sensitive, so those are two distinct, non-colliding
  files there — the tool detects this per-platform and won't force a
  rename between them on Linux. (File **extension** matching for
  categorization is always case-insensitive everywhere, regardless of this
  — `.JPG` and `.jpg` are always both `Images`.)
- **Same-volume move detection**: moving within one drive/volume is a fast
  rename with no disk-space check needed; moving across volumes is a real
  copy-then-delete that does need free space. This is detected via the
  filesystem's own device ID (`st_dev`) on every platform, so it correctly
  handles Windows volumes without a drive letter, and mount points on
  macOS/Linux, not just drive-letter differences.
- **Scanning a root filesystem**: warns when the source is a full drive
  root (`D:\`, `/Volumes/MyDrive`) or, on macOS/Linux, the system root
  (`/`) itself — the closest equivalent to scanning Windows' `C:\`.

## Safety considerations

- **Copy is the default.** Nothing is deleted or modified in the source
  unless you explicitly pass `--move`.
- Source and destination are validated up front: the source must exist and
  be readable, the destination must exist or be creatable, and the two must
  not be the same location.
- The destination is never traversed as part of the scan, even when nested
  inside the source.
- Free space on the destination drive is checked against the estimated
  total size before a real copy run; if there isn't enough room you're
  warned and asked to confirm (or the run aborts automatically under
  `--yes`, since there's no one to prompt).
- Scanning a drive/volume root is flagged, with an extra warning if it's
  the system root (`C:\` on Windows, `/` on macOS/Linux) — on Windows, use
  `--ignore-system` to leave protected system files alone (no equivalent
  needed on macOS/Linux; see Platform notes above). The utility never
  intentionally writes to system files; it only ever reads from the source
  and writes into the destination folder you specify.
- A full confirmation summary (source, destination, operation, duplicate
  policy, file count) is shown before any real copy/move, unless `--yes` is
  given for unattended/scheduled use.

## Project structure

```text
file_organizer/
│
├── file_organizer.py     # CLI entry point (argparse, wiring)
├── scanner.py             # Recursive, error-tolerant directory scanning
├── categorizer.py         # Extension -> category classification
├── organizer.py           # Orchestrates scan -> filter -> confirm -> copy/move
├── duplicate_handler.py   # skip / rename / overwrite resolution
├── logger.py               # Logging setup
├── utils.py                 # Size parsing, hashing, path helpers
├── config.json              # Extension -> category mapping
├── requirements.txt
└── README.md
```

## Building a standalone executable

Install PyInstaller (not needed to just run the script):

```bash
pip install pyinstaller
```

PyInstaller builds a native binary for whichever OS you run it **on** —
there's no cross-compiling a Windows `.exe` from a Mac, or vice versa. Build
it on each OS you want to distribute to. The `--add-data` separator differs
between platforms: `;` on Windows, `:` on macOS/Linux.

**Windows:**
```bash
pyinstaller --onefile --name FileOrganizer --add-data "config.json;." file_organizer.py
```
Creates `dist\FileOrganizer.exe`, usable on any Windows machine without
Python installed:
```text
FileOrganizer.exe D:\ D:\Organized
FileOrganizer.exe "E:\My External Drive" "D:\Organized" --dry-run
FileOrganizer.exe D:\ D:\Organized --duplicate rename --preserve-structure
```

**macOS/Linux:**
```bash
pyinstaller --onefile --name FileOrganizer --add-data "config.json:." file_organizer.py
```
Creates `dist/FileOrganizer` (no extension), usable on that same OS without
Python installed:
```text
./FileOrganizer /Volumes/MyDrive /Volumes/MyDrive/Organized
./FileOrganizer "/Volumes/My External Drive" "/Volumes/Backup" --dry-run
./FileOrganizer /Volumes/MyDrive /Volumes/MyDrive/Organized --duplicate rename --preserve-structure
```

On any platform, the built executable first looks for a `config.json`
**next to itself**; if found, it's used as-is (no rebuild needed to tweak
categories). Otherwise it falls back to the copy bundled inside the
executable at build time.
