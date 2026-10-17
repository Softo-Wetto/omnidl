"""Stream a ZIP of finished downloads straight to the browser.

Save used to build the whole archive in a temp file first: a big playlist needed that much
free disk, and through the Cloudflare tunnel the response has to *start* within 100 seconds
or it's cut off — which a multi-gigabyte zip couldn't. This writes the archive as it's sent.

Entries are stored, not deflated (audio is already compressed). Each file's CRC is computed
just before the file is sent — a quick read that leaves it in the OS cache for the real one —
so every local header carries the true CRC and sizes, with no trailing data descriptors. That
is the most widely readable layout (Windows Explorer, macOS Archive Utility, iOS Files,
streaming unzippers). ZIP64 records are added only when an archive needs them (>4 GB).
Because every size is known up front, the exact Content-Length is too, so browsers show
real progress.
"""
from __future__ import annotations

import os
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

ZIP64_LIMIT = 0xFFFFFFFF          # a field this size or more moves into a ZIP64 extra
_FULL32 = 0xFFFFFFFF              # what an overflowed field holds: "see the ZIP64 record"
_FULL16 = 0xFFFF
_MAX_ENTRIES = 0xFFFF
_CHUNK = 1 << 20
_UTF8_NAMES = 0x0800


class FileChanged(OSError):
    """A file changed after the archive was planned: sending it would make a corrupt zip."""


@dataclass
class _Entry:
    path: Path
    name: bytes
    size: int
    offset: int
    dos_time: int
    dos_date: int
    mtime_ns: int = 0

    @property
    def big(self) -> bool:
        return self.size >= ZIP64_LIMIT

    @property
    def far(self) -> bool:
        return self.offset >= ZIP64_LIMIT

    @property
    def local_extra(self) -> int:
        return 20 if self.big else 0

    @property
    def central_extra(self) -> int:
        fields = (16 if self.big else 0) + (8 if self.far else 0)
        return 4 + fields if fields else 0


def _dos_stamp(mtime: float) -> tuple[int, int]:
    t = time.localtime(max(mtime, 315532800))          # ZIP can't go before 1980
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
            ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


def arc_names(files: list[Path]) -> list[str]:
    """Unique names inside the archive, compared case-insensitively (Windows and macOS
    extract "Song.mp3" and "song.mp3" onto one file). A repeated name gets its folders
    prefixed, nearest first ("Disc 1 - Song.mp3", "Album B - Disc 1 - Song.mp3"), then a
    number if even that collides — one prefix alone gave three "Disc 1 - Song.mp3" entries."""
    used: set[str] = set()
    names = []
    for f in files:
        folders = [part for part in f.parent.parts if part not in (f.anchor, "")]
        candidates = [f.name] + [" - ".join([*folders[-depth:], f.name])
                                 for depth in range(1, len(folders) + 1)]
        name = next((c for c in candidates if c.casefold() not in used), None)
        if name is None:
            n = 2
            while (name := f"{f.stem} ({n}){f.suffix}").casefold() in used:
                n += 1
        used.add(name.casefold())
        names.append(name)
    return names


class ZipPlan:
    """The archive's layout, worked out before a byte is sent."""

    def __init__(self, files: list[Path]):
        self.entries: list[_Entry] = []
        offset = 0
        for path, name in zip(files, arc_names(files)):
            st = path.stat()
            dos_time, dos_date = _dos_stamp(st.st_mtime)
            entry = _Entry(path, name.encode("utf-8"), st.st_size, offset, dos_time, dos_date,
                           st.st_mtime_ns)
            self.entries.append(entry)
            offset += 30 + len(entry.name) + entry.local_extra + entry.size
        self.cd_offset = offset
        self.cd_size = sum(46 + len(e.name) + e.central_extra for e in self.entries)
        self.zip64_end = (len(self.entries) >= _MAX_ENTRIES or self.cd_offset >= ZIP64_LIMIT
                          or self.cd_size >= ZIP64_LIMIT)
        self.length = self.cd_offset + self.cd_size + (56 + 20 if self.zip64_end else 0) + 22

    @staticmethod
    def _unchanged(entry: _Entry, src) -> None:
        st = os.fstat(src.fileno())
        if st.st_size != entry.size or st.st_mtime_ns != entry.mtime_ns:
            raise FileChanged(f"{entry.path.name} changed while it was being saved - try again")

    @staticmethod
    def _crc(entry: _Entry, src) -> int:
        crc, left = 0, entry.size
        while left:
            block = src.read(min(_CHUNK, left))
            if not block:
                raise FileChanged(f"{entry.path.name} got shorter while it was being saved - try again")
            crc = zlib.crc32(block, crc)
            left -= len(block)
        return crc

    def chunks(self) -> Iterator[bytes]:
        """The archive, in order.

        Every file is checked to be exactly the one planned (size and modified time) before its
        header is sent, and checked again after its data: a file that changes mid-export —
        a tag repair, another download — fails the export with FileChanged instead of being
        sent with a header that doesn't match its bytes (a zip that "downloads" but is corrupt).
        """
        crcs: list[int] = []
        for entry in self.entries:
            with open(entry.path, "rb") as src:
                self._unchanged(entry, src)
                crc = self._crc(entry, src)
                self._unchanged(entry, src)
                src.seek(0)
                crcs.append(crc)
                yield self._local_header(entry, crc)
                sent_crc, left = 0, entry.size
                while left:
                    block = src.read(min(_CHUNK, left))
                    if not block:
                        raise FileChanged(f"{entry.path.name} got shorter while it was being saved - try again")
                    sent_crc = zlib.crc32(block, sent_crc)
                    left -= len(block)
                    yield block
                self._unchanged(entry, src)
                if sent_crc != crc:
                    raise FileChanged(f"{entry.path.name} changed while it was being saved - try again")
        yield b"".join(self._central_header(e, crc) for e, crc in zip(self.entries, crcs))
        yield self._end_records()

    @staticmethod
    def _version(entry: _Entry) -> int:
        return 45 if (entry.big or entry.far) else 20

    def _local_header(self, e: _Entry, crc: int) -> bytes:
        size = _FULL32 if e.big else e.size
        extra = struct.pack("<HHQQ", 0x0001, 16, e.size, e.size) if e.big else b""
        return struct.pack("<IHHHHHIIIHH", 0x04034B50, self._version(e), _UTF8_NAMES, 0,
                           e.dos_time, e.dos_date, crc, size, size, len(e.name), len(extra)) + e.name + extra

    def _central_header(self, e: _Entry, crc: int) -> bytes:
        fields = b""
        if e.big:
            fields += struct.pack("<QQ", e.size, e.size)
        if e.far:
            fields += struct.pack("<Q", e.offset)
        extra = struct.pack("<HH", 0x0001, len(fields)) + fields if fields else b""
        size = _FULL32 if e.big else e.size
        offset = _FULL32 if e.far else e.offset
        version = self._version(e)
        return struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, version, version, _UTF8_NAMES, 0,
                           e.dos_time, e.dos_date, crc, size, size, len(e.name), len(extra), 0,
                           0, 0, 0, offset) + e.name + extra

    def _end_records(self) -> bytes:
        count = len(self.entries)
        out = b""
        if self.zip64_end:
            end64_at = self.cd_offset + self.cd_size
            out += struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, count, count,
                               self.cd_size, self.cd_offset)
            out += struct.pack("<IIQI", 0x07064B50, 0, end64_at, 1)
        entries = _FULL16 if count >= _MAX_ENTRIES else count
        cd_size = _FULL32 if self.cd_size >= ZIP64_LIMIT else self.cd_size
        cd_offset = _FULL32 if self.cd_offset >= ZIP64_LIMIT else self.cd_offset
        out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, entries, entries, cd_size, cd_offset, 0)
        return out
