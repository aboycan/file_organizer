"""Work out when a file was *originally* created, for --date-folders.

The filesystem "Created" date is reset whenever a file is copied to another
drive (Windows sets it to the moment of the copy), so for photos and videos
copied off a phone, camera card or backup it usually means "the day I
copied it", not "the day I took it". The date is therefore taken from, in
order:

1. Photos: the camera's "Date taken" stored inside the file (EXIF
   DateTimeOriginal, else DateTimeDigitized). Supported: JPEG/MPO,
   TIFF and TIFF-based RAW (DNG, NEF, CR2, ARW, ORF, RW2, ...), PNG, WebP,
   HEIC/HEIF/AVIF.
2. Videos (MP4, MOV, 3GP, M4V, M4A, ...): the recording date stored in the
   file's movie header.
3. A date in the file name, as phones and apps write it when they create
   the file: ``Screenshot_2016-03-02-12-30-58.png``,
   ``IMG-20151231-WA0001.jpg``, ``VID_20160101_123456.mp4``,
   ``20160830_200615.jpg``, ``msgstore-2016-12-07.1.db.crypt12``. Only a
   full year-month-day (year first) that stands on its own in the name is
   accepted, so random digits or hashes are never mistaken for a date.
4. Anything else: the EARLIER of the file's filesystem creation date and
   last-modified date. Copying keeps the modified date but resets the
   creation date, so the earlier of the two is the closest to when the
   file really came to be.

The file's contents are sniffed (not its extension), so a mis-named file
is still read correctly. Standard library only, and it never raises: an
unreadable, locked or corrupt file just falls through to the next step.

Part of File Organizer.
Author: Madhu Kumar <madhu.a@hotmail.com>
"""

from __future__ import annotations

import os
import re
import struct
import time
from datetime import datetime
from typing import BinaryIO, Optional, Tuple

from utils import long_path

SOURCE_PHOTO = "photo date taken"
SOURCE_VIDEO = "video recorded date"
SOURCE_NAME = "date in file name"
SOURCE_FILE = "file date"
SOURCES = (SOURCE_PHOTO, SOURCE_VIDEO, SOURCE_NAME, SOURCE_FILE)

# Nothing digital predates 1980 (the FAT/DOS epoch); anything earlier is an
# unset clock or a corrupt value.
_MIN_TS = 315532800.0  # 1980-01-01 00:00 UTC
_QT_EPOCH_OFFSET = 2082844800  # seconds from 1904-01-01 (QuickTime/MP4) to 1970-01-01

_TAG_EXIF_IFD = 0x8769
_TAG_DATE_TAKEN = 0x9003  # DateTimeOriginal
_TAG_DATE_DIGITIZED = 0x9004  # DateTimeDigitized
_TIFF_MAGICS = (42, 0x55, 0x4F52, 0x5352)  # TIFF/DNG/NEF/CR2/ARW..., Panasonic RW2, Olympus ORF

_EXIF_DT_RE = re.compile(rb"^\s*(\d{4})[:\-/.](\d{1,2})[:\-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?")

# YYYYMMDD / YYYY-MM-DD / YYYY_MM_DD / YYYY.MM.DD (same separator twice),
# optionally followed by a HHMMSS time, not glued to other digits.
_NAME_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d\d)([-_.]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])"
    r"(?:[-_. T]?([01]\d|2[0-3])[-_.]?([0-5]\d)[-_.]?([0-5]\d))?(?!\d)"
)

_MAX_BOXES = 4096
_MAX_JPEG_SEGMENTS = 128
_MAX_PNG_CHUNKS = 8192
_MAX_META_BYTES = 16 * 1024 * 1024


def original_date(path, fs_created: float, modified: float) -> Tuple[float, str]:
    """Return ``(timestamp, source)`` for the date ``path`` was originally created.

    ``source`` is one of :data:`SOURCES`, for logging/summary purposes.
    """
    found = embedded_date(path)
    if found is not None:
        return found
    named = name_date(os.path.basename(str(path)))
    if named is not None:
        return named, SOURCE_NAME
    candidates = [t for t in (fs_created, modified) if _valid_file_ts(t)]
    return (min(candidates) if candidates else modified), SOURCE_FILE


def name_date(filename: str) -> Optional[float]:
    """Date written into a file name by a phone/app, e.g. ``IMG-20151231-WA0001.jpg``.

    The date must stand on its own: preceded by the start of the name, a
    separator, or a letters-only prefix (``IMG``, ``Screenshot``...), and
    followed by the end of the name or a separator. Directory names are
    never used.
    """
    stem = os.path.splitext(filename)[0]
    for match in _NAME_DATE_RE.finditer(stem):
        before, after = stem[:match.start()], stem[match.end():]
        if before and before[-1].isalnum() and not before.isalpha():
            continue
        if after and after[0].isalnum():
            continue
        y, _, mo, d, hh, mi, ss = match.groups()
        try:
            ts = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0)).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        if _plausible(ts):
            return ts
    return None


def embedded_date(path) -> Optional[Tuple[float, str]]:
    """Date stored inside a photo/video file, or None if there isn't a usable one."""
    try:
        with open(long_path(str(path)), "rb") as fh:
            head = fh.read(16)
            if head[:2] == b"\xff\xd8":
                ts = _jpeg_date(fh)
                return (ts, SOURCE_PHOTO) if ts else None
            if head[:2] in (b"II", b"MM") and _tiff_magic(head) in _TIFF_MAGICS:
                ts = _tiff_date(_FileView(fh, 0))
                return (ts, SOURCE_PHOTO) if ts else None
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                ts = _png_date(fh)
                return (ts, SOURCE_PHOTO) if ts else None
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                ts = _webp_date(fh)
                return (ts, SOURCE_PHOTO) if ts else None
            if head[4:8] == b"ftyp":
                return _isobmff_date(fh)
    except Exception:  # metadata is best-effort: never let a bad file break a run
        return None
    return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _valid_file_ts(ts) -> bool:
    try:
        return ts is not None and float(ts) >= _MIN_TS
    except (TypeError, ValueError):
        return False


def _plausible(ts: float) -> Optional[float]:
    """Reject unset-clock / corrupt / future dates."""
    if _MIN_TS <= ts <= time.time() + 2 * 86400:
        return ts
    return None


def _tiff_magic(head: bytes) -> int:
    endian = "<" if head[:2] == b"II" else ">"
    return struct.unpack(endian + "H", head[2:4])[0]


def _parse_exif_datetime(raw: Optional[bytes]) -> Optional[float]:
    """``b"2016:12:25 20:34:16"`` -> local timestamp (EXIF times are camera-local)."""
    if not raw:
        return None
    match = _EXIF_DT_RE.match(raw)
    if not match:
        return None
    y, mo, d, hh, mi, ss = (int(g) if g else 0 for g in match.groups())
    try:
        return _plausible(datetime(y, mo, d, hh, mi, ss).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


class _BytesView:
    """Random access into an in-memory buffer (offsets relative to ``base``)."""

    def __init__(self, data: bytes, base: int = 0):
        self.data, self.base = data, base

    def read(self, offset: int, size: int) -> bytes:
        start = self.base + offset
        if offset < 0 or start > len(self.data):
            return b""
        return self.data[start:start + size]


class _FileView:
    """Random access into an open file (offsets relative to ``base``)."""

    def __init__(self, fh: BinaryIO, base: int = 0):
        self.fh, self.base = fh, base

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0:
            return b""
        self.fh.seek(self.base + offset)
        return self.fh.read(size)


# --------------------------------------------------------------------------- #
# EXIF (TIFF structure) - shared by JPEG, TIFF/RAW, PNG, WebP and HEIF
# --------------------------------------------------------------------------- #
def _read_ifd(view, offset: int, endian: str) -> dict:
    """Return {tag: value} for the few tags we care about in one IFD."""
    found: dict = {}
    raw_count = view.read(offset, 2)
    if len(raw_count) < 2:
        return found
    count = struct.unpack(endian + "H", raw_count)[0]
    if count > 1000:
        return found
    entries = view.read(offset + 2, count * 12)
    for i in range(len(entries) // 12):
        tag, typ, n, value = struct.unpack_from(endian + "HHI4s", entries, i * 12)
        if tag == _TAG_EXIF_IFD:
            found[tag] = struct.unpack(endian + "I", value)[0]
        elif tag in (_TAG_DATE_TAKEN, _TAG_DATE_DIGITIZED) and typ == 2:  # ASCII
            if n <= 4:
                found[tag] = value[:n]
            elif n <= 64:
                found[tag] = view.read(struct.unpack(endian + "I", value)[0], n)
    return found


def _tiff_date(view) -> Optional[float]:
    """Date taken from a TIFF structure starting at offset 0 of ``view``."""
    header = view.read(0, 8)
    if len(header) < 8 or header[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if header[:2] == b"II" else ">"
    magic, ifd0 = struct.unpack(endian + "HI", header[2:8])
    if magic not in _TIFF_MAGICS:
        return None
    tags = _read_ifd(view, ifd0, endian)  # DNG may carry the date in IFD0 itself
    exif_ifd = tags.get(_TAG_EXIF_IFD)
    if exif_ifd:
        tags.update(_read_ifd(view, exif_ifd, endian))
    for tag in (_TAG_DATE_TAKEN, _TAG_DATE_DIGITIZED):
        ts = _parse_exif_datetime(tags.get(tag))
        if ts:
            return ts
    return None


def _exif_blob_date(blob: bytes) -> Optional[float]:
    """EXIF payload that may or may not start with the ``Exif\\0\\0`` prefix."""
    if blob.startswith(b"Exif\x00\x00"):
        blob = blob[6:]
    return _tiff_date(_BytesView(blob))


# --------------------------------------------------------------------------- #
# Container formats
# --------------------------------------------------------------------------- #
def _jpeg_date(fh: BinaryIO) -> Optional[float]:
    fh.seek(2)
    for _ in range(_MAX_JPEG_SEGMENTS):
        byte = fh.read(1)
        if byte != b"\xff":
            return None
        marker = fh.read(1)
        while marker == b"\xff":  # fill bytes
            marker = fh.read(1)
        if not marker or marker in (b"\xda", b"\xd9"):  # start of scan / end of image
            return None
        if marker == b"\x01" or b"\xd0" <= marker <= b"\xd7":  # no length field
            continue
        raw_len = fh.read(2)
        if len(raw_len) < 2:
            return None
        length = struct.unpack(">H", raw_len)[0] - 2
        if length < 0:
            return None
        if marker == b"\xe1":  # APP1
            segment = fh.read(length)
            if segment.startswith(b"Exif\x00\x00"):
                return _tiff_date(_BytesView(segment, 6))
        else:
            fh.seek(length, 1)
    return None


def _png_date(fh: BinaryIO) -> Optional[float]:
    fh.seek(8)
    for _ in range(_MAX_PNG_CHUNKS):
        header = fh.read(8)
        if len(header) < 8:
            return None
        length, ctype = struct.unpack(">I4s", header)
        if ctype == b"eXIf" and length <= _MAX_META_BYTES:
            return _exif_blob_date(fh.read(length))
        if ctype == b"IEND":
            return None
        fh.seek(length + 4, 1)  # data + CRC
    return None


def _webp_date(fh: BinaryIO) -> Optional[float]:
    fh.seek(12)
    for _ in range(_MAX_PNG_CHUNKS):
        header = fh.read(8)
        if len(header) < 8:
            return None
        ctype, length = struct.unpack("<4sI", header)
        if ctype == b"EXIF" and length <= _MAX_META_BYTES:
            return _exif_blob_date(fh.read(length))
        fh.seek(length + (length & 1), 1)  # chunks are padded to even sizes
    return None


def _boxes(fh: BinaryIO, start: int, end: int):
    """Yield ``(type, payload_start, box_end)`` for ISO-BMFF boxes in [start, end)."""
    offset = start
    for _ in range(_MAX_BOXES):
        if offset + 8 > end:
            return
        fh.seek(offset)
        header = fh.read(8)
        if len(header) < 8:
            return
        size, btype = struct.unpack(">I4s", header)
        header_len = 8
        if size == 1:
            large = fh.read(8)
            if len(large) < 8:
                return
            size = struct.unpack(">Q", large)[0]
            header_len = 16
        elif size == 0:
            size = end - offset
        if size < header_len:
            return
        yield btype, offset + header_len, min(offset + size, end)
        offset += size


def _isobmff_date(fh: BinaryIO) -> Optional[Tuple[float, str]]:
    fh.seek(0, 2)
    file_end = fh.tell()
    meta = None
    for btype, start, end in _boxes(fh, 0, file_end):
        if btype == b"moov":
            ts = _mvhd_date(fh, start, end)
            return (ts, SOURCE_VIDEO) if ts else None
        if btype == b"meta" and meta is None:
            meta = (start, end)
    if meta is not None:
        ts = _heif_exif_date(fh, *meta)
        return (ts, SOURCE_PHOTO) if ts else None
    return None


def _mvhd_date(fh: BinaryIO, start: int, end: int) -> Optional[float]:
    for btype, payload, _ in _boxes(fh, start, end):
        if btype != b"mvhd":
            continue
        fh.seek(payload)
        version = fh.read(4)[:1]
        if version == b"\x01":
            raw = fh.read(8)
            created = struct.unpack(">Q", raw)[0] if len(raw) == 8 else 0
        else:
            raw = fh.read(4)
            created = struct.unpack(">I", raw)[0] if len(raw) == 4 else 0
        if not created:
            return None
        return _plausible(float(created - _QT_EPOCH_OFFSET))  # stored as UTC
    return None


def _uint(data: bytes, pos: int, size: int) -> Tuple[int, int]:
    if size == 0:
        return 0, pos
    fmt = {1: ">B", 2: ">H", 4: ">I", 8: ">Q"}.get(size)
    if fmt is None or pos + size > len(data):
        raise ValueError("bad field")
    return struct.unpack_from(fmt, data, pos)[0], pos + size


def _heif_exif_date(fh: BinaryIO, start: int, end: int) -> Optional[float]:
    """HEIC/HEIF/AVIF: find the 'Exif' item via iinf + iloc, then parse it."""
    start += 4  # 'meta' is a FullBox: skip version + flags
    iinf = iloc = None
    for btype, payload, box_end in _boxes(fh, start, end):
        if btype == b"iinf":
            iinf = (payload, box_end)
        elif btype == b"iloc":
            iloc = (payload, box_end)
    if not iinf or not iloc:
        return None

    exif_id = None
    fh.seek(iinf[0])
    version = fh.read(4)[0]
    first_child = iinf[0] + 4 + (2 if version == 0 else 4)
    for btype, payload, _ in _boxes(fh, first_child, iinf[1]):
        if btype != b"infe":
            continue
        fh.seek(payload)
        infe = fh.read(16)
        if len(infe) < 12 or infe[0] < 2:
            continue
        if infe[0] == 2:
            item_id, item_type = struct.unpack_from(">H", infe, 4)[0], infe[8:12]
        else:
            item_id, item_type = struct.unpack_from(">I", infe, 4)[0], infe[10:14]
        if item_type == b"Exif":
            exif_id = item_id
            break
    if exif_id is None:
        return None

    if iloc[1] - iloc[0] > _MAX_META_BYTES:
        return None
    fh.seek(iloc[0])
    data = fh.read(iloc[1] - iloc[0])
    version = data[0]
    offset_size, length_size = data[4] >> 4, data[4] & 0x0F
    base_offset_size, index_size = data[5] >> 4, data[5] & 0x0F
    pos = 6
    count, pos = _uint(data, pos, 2 if version < 2 else 4)
    for _ in range(count):
        item_id, pos = _uint(data, pos, 2 if version < 2 else 4)
        method = 0
        if version in (1, 2):
            method, pos = _uint(data, pos, 2)
            method &= 0x0F
        pos += 2  # data_reference_index
        base, pos = _uint(data, pos, base_offset_size)
        extents, pos = _uint(data, pos, 2)
        first = None
        for _ in range(extents):
            if version in (1, 2) and index_size:
                _, pos = _uint(data, pos, index_size)
            ext_offset, pos = _uint(data, pos, offset_size)
            ext_length, pos = _uint(data, pos, length_size)
            if first is None:
                first = (ext_offset, ext_length)
        if item_id != exif_id:
            continue
        if method != 0 or first is None or first[1] > _MAX_META_BYTES:
            return None
        fh.seek(base + first[0])
        blob = fh.read(first[1])
        if len(blob) < 4:
            return None
        tiff_start = 4 + struct.unpack(">I", blob[:4])[0]
        if blob[tiff_start:tiff_start + 2] in (b"II", b"MM"):
            return _tiff_date(_BytesView(blob, tiff_start))
        return _exif_blob_date(blob[4:])
    return None
