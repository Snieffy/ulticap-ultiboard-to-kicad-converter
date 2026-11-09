# KIUC - KiCad Ulticap Schematic Converter
# Copyright (C) 2026 Snieffy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://gnu.org>.
"""
kiuc_binary.py — Parser for Ulticap binary .UTSCH files

File format coverage: the .UTSCH files of Ulticap V5 for Windows (an OLE2
compound-document container; every file seen, from 1999 to V5.72, has
document schema 0x230 and format version 5.33). Binary-only: does not read or require a
companion .SCH file. A .UTSCH file is fully self-contained, and an
ASCII-merge fallback was deliberately rejected for this module — it is
silently wrong whenever the .UTSCH was edited after the .SCH was last
generated, with no way to detect that from the file itself.

File structure:
    OLE2 compound document, single stream named "Contents", holding an
    MFC CArchive serialization of the whole design.

Layers implemented so far (each verified against real files before the next
one is added):
    1. container  -- read_container(): compound-file check, root storage CLSID
                     check, extraction of the 'Contents' stream
    2. header     -- parse_header(): document schema number and the two-part
                     file-format version that open the stream
    3. prologue   -- read_prologue() / check_prologue(): the document
                     variants and the font list, which end on the word that
                     counts the nets
    4. net check  -- check_net_array(): compares the declared net count with
                     the nets found by their tags (an independent consistency
                     check for the inspector, not used for conversion)
Everything after the prologue -- the object graph that hangs off the first
net -- is read by kiuc_binary_walk.py, and given meaning by
kiuc_binary_semantic.py.

The class and object tags in the stream are not fixed byte values: MFC's
archive numbers classes and objects in the order they are first written, so
the same class has a different tag in different files. The walker follows
that numbering; nothing needs to be derived from a file in advance.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ── errors and format constants ──────────────────────────────────────────

class UtschError(ValueError):
    """Base class for every failure raised by the binary-format layers.

    Subclasses ValueError so callers written against the earlier skeleton,
    which raised plain ValueError, keep working.
    """


class ContainerError(UtschError):
    """The file is not a usable compound-file container."""


class NotCompoundFileError(ContainerError):
    """No compound-file signature: probably an ASCII .SCH, not a .UTSCH.

    Kept distinct so a caller can route such a file to the ASCII converter
    instead of reporting a failure.
    """


class ForeignStorageError(ContainerError):
    """A compound file whose root storage carries another type's CLSID.

    Every .UTSCH file carries EXPECTED_ROOT_CLSID; a compound file with
    another is some other kind of document, so the parser reports an error
    instead of guessing at a layout it does not know.
    """

    def __init__(self, clsid: bytes):
        super().__init__(
            "compound file with a foreign root CLSID "
            f"{clsid.hex(' ')} (expected {EXPECTED_ROOT_CLSID.hex(' ')})")
        self.clsid = clsid


class MissingContentsError(ContainerError):
    """The compound file has no 'Contents' stream."""


class UnsupportedSchemaError(UtschError):
    """The stream header names a schema or version other than the one every file has."""


class MalformedStreamError(UtschError):
    """The stream ends early or is not laid out as expected."""


_OLE_MAGIC = bytes.fromhex('D0CF11E0A1B11AE1')

# Root-storage CLSID of a .UTSCH file, in the on-disk (little-endian GUID)
# byte order. Every .UTSCH file carries it (FILEFORMAT_ULTICAP_UTSCH.md §1.1), so an
# exact match is required.
EXPECTED_ROOT_CLSID = bytes.fromhex('c09da5aa91efce118513444553540000')

# The (schema, version) pair of every file examined, from 1999 to V5.72
# (FILEFORMAT_ULTICAP_UTSCH.md §2); the body layout is known for it alone, so the
# readers refuse anything else.
VERIFIED_SCHEMA = 0x230
VERIFIED_VERSION = ('5', '33')


# ── OLE2 container reader ────────────────────────────────────────────────

# FAT entries at or above this value are markers, not sector numbers.
_SPECIAL = 0xFFFFFFFC
_FREESECT = 0xFFFFFFFF


@dataclass(frozen=True)
class DirEntry:
    """One allocated entry of the compound file's directory."""
    index: int
    name: str
    path: str                 # '/'-joined from the root; '(unlinked)/...' if unreachable
    kind: str                 # 'root', 'storage' or 'stream'
    size: int
    start_sector: int
    clsid: bytes              # 16 bytes; all zero when none is set


@dataclass(frozen=True)
class ContainerLayout:
    """How the bytes of a compound file are spent.

    Every sector after the header is counted exactly once, in one of
    reachable / free / orphan / unmapped, so the four always add up to
    total_sectors. 'Orphan' means the FAT marks a sector as in use but no
    chain reachable from the directory leads to it -- typically space left
    behind by incremental saves.
    """
    file_size: int
    sector_size: int
    total_sectors: int
    entries: Tuple[DirEntry, ...]
    fat_sectors: int
    difat_sectors: int
    directory_sectors: int
    mini_fat_sectors: int
    mini_stream_sectors: int              # the root entry's own stream
    stream_sectors: Tuple[Tuple[str, int], ...]   # (path, sectors) for FAT streams
    reachable_sectors: int
    free_sectors: int
    orphan_sectors: int
    unmapped_sectors: int                 # beyond the range the FAT describes
    slack_bytes: int                      # trailing bytes that do not fill a sector


class _CompoundFile:
    """The tables of a compound file: header, FAT (with DIFAT), mini-FAT and
    directory. Reading them never touches stream contents, so the same parse
    serves both stream extraction and the space report.

    Every chain walk carries a loop guard: a damaged FAT can otherwise send a
    reader round in circles forever.
    """

    def __init__(self, data: bytes):
        if len(data) < 512 or not data.startswith(_OLE_MAGIC):
            raise NotCompoundFileError(
                "no compound-file signature (not a .UTSCH container)")
        self.data = data
        shift = struct.unpack_from('<H', data, 30)[0]
        mini_shift = struct.unpack_from('<H', data, 32)[0]
        # Only the two sector sizes the format defines. Anything else is
        # damage, and trusting it would size buffers from garbage.
        if shift not in (9, 12) or mini_shift != 6:
            raise ContainerError(
                f"unsupported sector sizes (shifts {shift}/{mini_shift})")
        self.sec_size = 1 << shift
        self.mini_size = 1 << mini_shift
        self.cutoff = struct.unpack_from('<I', data, 56)[0]
        self.total_sectors = max(0, (len(data) - self.sec_size) // self.sec_size)

        # FAT sector numbers: the 109 in the header, then any DIFAT sectors.
        # Each DIFAT sector holds sec_size/4 - 1 further numbers and, in its
        # last dword, the number of the next DIFAT sector.
        self.fat_sector_numbers: List[int] = [
            s for s in struct.unpack_from('<109I', data, 76) if s < _SPECIAL]
        self.difat_chain = self._read_difat(
            struct.unpack_from('<I', data, 68)[0],
            struct.unpack_from('<I', data, 72)[0])
        self.fat: List[int] = []
        for sec in self.fat_sector_numbers:
            self.fat.extend(struct.unpack_from(
                f'<{self.sec_size // 4}I', data, (sec + 1) * self.sec_size))

        first_mini_fat = struct.unpack_from('<I', data, 60)[0]
        self.mini_fat_chain = (self.follow(first_mini_fat)
                               if first_mini_fat < _SPECIAL else [])
        self.mini_fat: List[int] = []
        for sec in self.mini_fat_chain:
            self.mini_fat.extend(struct.unpack_from(
                f'<{self.sec_size // 4}I', data, (sec + 1) * self.sec_size))

        self.dir_chain = self.follow(struct.unpack_from('<I', data, 48)[0])
        self.entries = self._read_directory()
        self._mini_container: Optional[bytes] = None

    # -- tables ----------------------------------------------------------

    def _read_difat(self, first: int, declared: int) -> List[int]:
        # With no DIFAT sectors declared, the first-DIFAT field is ignored, as
        # the earlier reader ignored it: a writer that leaves that unused
        # field uninitialised must not turn a readable file into an error.
        if declared == 0:
            return []
        chain: List[int] = []
        seen = set()
        per = self.sec_size // 4 - 1
        cur = first
        while cur < _SPECIAL:
            if cur in seen:
                raise ContainerError("corrupt DIFAT chain (loop)")
            seen.add(cur)
            chain.append(cur)
            vals = struct.unpack_from(f'<{per + 1}I', self.data,
                                      (cur + 1) * self.sec_size)
            self.fat_sector_numbers.extend(v for v in vals[:per] if v < _SPECIAL)
            cur = vals[per]
        if len(chain) != declared:
            raise ContainerError(
                f"DIFAT chain has {len(chain)} sector(s) but the header "
                f"declares {declared}")
        return chain

    def follow(self, start: int) -> List[int]:
        """Sector numbers of the chain beginning at start, per the FAT."""
        chain: List[int] = []
        seen = set()
        cur = start
        while cur < _SPECIAL:
            if cur in seen:
                raise ContainerError("corrupt sector chain (loop)")
            seen.add(cur)
            chain.append(cur)
            if cur >= len(self.fat):
                break
            cur = self.fat[cur]
        return chain

    def _read_directory(self) -> Tuple[DirEntry, ...]:
        data, sec = self.data, self.sec_size
        raw = []                      # per slot: None (unused) or a tuple
        for s in self.dir_chain:
            base = (s + 1) * sec
            for i in range(sec // 128):
                e = base + i * 128
                nlen = struct.unpack_from('<H', data, e + 64)[0]
                if nlen == 0 or nlen > 64:
                    raw.append(None)
                    continue
                name = data[e:e + nlen].decode('utf-16-le', errors='replace').rstrip('\x00')
                obj_type = data[e + 66]
                left, right, child = struct.unpack_from('<III', data, e + 68)
                clsid = bytes(data[e + 80:e + 96])
                start = struct.unpack_from('<I', data, e + 116)[0]
                # Version-3 files keep a 32-bit size; only 4096-byte-sector
                # files use all 64 bits.
                size = (struct.unpack_from('<Q', data, e + 120)[0]
                        if sec == 4096 else struct.unpack_from('<I', data, e + 120)[0])
                raw.append((name, obj_type, left, right, child, clsid, start, size))
        if not raw or raw[0] is None or raw[0][1] != 5:
            raise ContainerError("compound file has no root directory entry")

        # Walk the sibling trees from the root; a storage's children hang off
        # its 'child' id. Guard against loops in a damaged tree.
        paths: Dict[int, str] = {}
        stack = [(0, '')]
        while stack:
            idx, parent = stack.pop()
            if idx >= len(raw) or raw[idx] is None:
                continue
            if idx in paths:
                raise ContainerError("corrupt directory tree (loop)")
            name, obj_type, left, right, child, _c, _s, _z = raw[idx]
            path = f"{parent}/{name}"
            paths[idx] = path
            for sib in (left, right):
                if sib < _SPECIAL:
                    stack.append((sib, parent))
            if obj_type in (1, 5) and child < _SPECIAL:
                stack.append((child, path))

        kinds = {5: 'root', 1: 'storage', 2: 'stream'}
        out = []
        for idx, r in enumerate(raw):
            if r is None or r[1] not in kinds:
                continue
            name, obj_type, _l, _r, _ch, clsid, start, size = r
            out.append(DirEntry(index=idx, name=name,
                                path=paths.get(idx, f"(unlinked)/{name}"),
                                kind=kinds[obj_type], size=size,
                                start_sector=start, clsid=clsid))
        return tuple(out)

    # -- contents --------------------------------------------------------

    @property
    def root(self) -> DirEntry:
        return self.entries[0]

    def read_stream(self, entry: DirEntry) -> bytes:
        """The bytes of a stream, refusing one shorter than its entry says."""
        if entry.size < self.cutoff:
            if self._mini_container is None:
                self._mini_container = b''.join(
                    self.data[(s + 1) * self.sec_size:(s + 2) * self.sec_size]
                    for s in self.follow(self.root.start_sector))
            if not self.mini_fat:
                raise ContainerError("small stream but the file has no mini-FAT")
            raw = b''
            seen = set()
            cur = entry.start_sector
            while cur < _SPECIAL:
                if cur >= len(self.mini_fat):
                    break
                if cur in seen:
                    raise ContainerError("corrupt mini-stream chain (loop)")
                seen.add(cur)
                off = cur * self.mini_size
                raw += self._mini_container[off:off + self.mini_size]
                cur = self.mini_fat[cur]
        else:
            raw = b''.join(self.data[(s + 1) * self.sec_size:(s + 2) * self.sec_size]
                           for s in self.follow(entry.start_sector))
        _require_full_stream(raw, entry.size)
        return raw[:entry.size]

    def layout(self) -> ContainerLayout:
        total = self.total_sectors
        reach = set(self.fat_sector_numbers) | set(self.difat_chain)
        reach |= set(self.dir_chain) | set(self.mini_fat_chain)
        root_chain = self.follow(self.root.start_sector)
        reach |= set(root_chain)
        stream_sectors = []
        for e in self.entries:
            if e.kind == 'stream' and e.size >= self.cutoff:
                chain = self.follow(e.start_sector)
                reach |= set(chain)
                stream_sectors.append((e.path, len(chain)))
        reachable = free = orphan = unmapped = 0
        for s in range(total):
            if s in reach:
                reachable += 1
            elif s >= len(self.fat):
                unmapped += 1
            elif self.fat[s] == _FREESECT:
                free += 1
            else:
                orphan += 1
        return ContainerLayout(
            file_size=len(self.data), sector_size=self.sec_size,
            total_sectors=total, entries=self.entries,
            fat_sectors=len(self.fat_sector_numbers),
            difat_sectors=len(self.difat_chain),
            directory_sectors=len(self.dir_chain),
            mini_fat_sectors=len(self.mini_fat_chain),
            mini_stream_sectors=len(root_chain),
            stream_sectors=tuple(stream_sectors),
            reachable_sectors=reachable, free_sectors=free,
            orphan_sectors=orphan, unmapped_sectors=unmapped,
            slack_bytes=len(self.data) - self.sec_size - total * self.sec_size)


def _read_compound_body(data: bytes,
                        expected_clsid: Optional[bytes]) -> Tuple[bytes, bytes]:
    """Read an OLE2 Compound Document; return (root CLSID, 'Contents' stream).

    When expected_clsid is given, a root storage with any other CLSID raises
    ForeignStorageError *before* the stream is looked up: a foreign compound
    file normally has no 'Contents' stream, and the CLSID is the more useful
    thing to report.
    """
    cf = _CompoundFile(data)
    root_clsid = cf.root.clsid
    if expected_clsid is not None and root_clsid != expected_clsid:
        raise ForeignStorageError(root_clsid)
    contents = next((e for e in cf.entries
                     if e.kind == 'stream' and e.name == 'Contents'), None)
    if contents is None:
        raise MissingContentsError(
            "'Contents' stream not found -- not a valid .UTSCH file")
    return root_clsid, cf.read_stream(contents)


def _require_full_stream(raw: bytes, declared: int) -> None:
    """Refuse a stream shorter than its directory entry says.

    Slicing silently returns whatever bytes exist, so a truncated file would
    otherwise yield a short stream with no error at all.
    """
    if len(raw) < declared:
        raise ContainerError(
            f"stream is {len(raw)} bytes but its directory entry declares "
            f"{declared} (truncated or damaged file)")


def _read_compound(data: bytes,
                   expected_clsid: Optional[bytes]) -> Tuple[bytes, bytes]:
    """_read_compound_body() with reads past the end turned into ContainerError.

    A truncated file makes the structure readers run off the buffer; that is
    a damaged-file condition, not a programming error, and callers scanning a
    folder should be able to catch one exception family for it.
    """
    try:
        return _read_compound_body(data, expected_clsid)
    except (struct.error, IndexError) as exc:
        raise ContainerError(
            f"truncated or damaged compound file ({exc})") from exc


def read_layout(path: str | Path) -> ContainerLayout:
    """The directory entries and space accounting of a compound file.

    Works on any compound file, whatever its root CLSID -- it is meant for
    finding out where a file's bytes go, e.g. when a file is much larger
    than its 'Contents' stream.
    """
    data = Path(path).read_bytes()
    try:
        return _CompoundFile(data).layout()
    except (struct.error, IndexError) as exc:
        raise ContainerError(
            f"truncated or damaged compound file ({exc})") from exc


# ── container layer: the CLSID-gated entry point ─────────────────────────

@dataclass(frozen=True)
class ContainerInfo:
    """Result of read_container(): the verified root CLSID and the stream."""
    root_clsid: bytes
    stream: bytes


def read_container(path: str | Path) -> ContainerInfo:
    """Open a .UTSCH and return its 'Contents' stream, telling apart the
    three cases a path can be:

        no compound-file signature  -> NotCompoundFileError (ASCII .SCH?)
        compound, other root CLSID  -> ForeignStorageError
        compound, expected CLSID    -> ContainerInfo

    Raises MissingContentsError / ContainerError for compound files it
    cannot read.
    """
    data = Path(path).read_bytes()
    clsid, stream = _read_compound(data, EXPECTED_ROOT_CLSID)
    return ContainerInfo(root_clsid=clsid, stream=stream)


def looks_like_utsch(path: str | Path) -> bool:
    """Quick check for whether a file is a genuine Ulticap .UTSCH container
    -- the same three-way split read_container itself applies (compound-
    file signature, then root storage CLSID), just discarding the result
    instead of decoding the stream any further. Exists for the same reason
    as kiuc_ascii.looks_like_sch: to catch a wrong-format file selected by
    mistake before any real parsing is attempted on it, most easily done
    through a GUI file dialog's own "All files" fallback filter.
    """
    try:
        read_container(path)
    except (UtschError, OSError):
        return False
    return True


# ── header layer ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DocumentHeader:
    """The schema number and two-part version that open the stream."""
    schema: int
    version_major: str
    version_minor: str
    body_offset: int          # offset of the first byte after the header

    @property
    def is_verified_layout(self) -> bool:
        """True only for the (schema, version) the body layout was checked
        against; later layers should refuse anything else."""
        return (self.schema == VERIFIED_SCHEMA
                and (self.version_major, self.version_minor) == VERIFIED_VERSION)


# Strings in a .UTSCH are the Windows program's ANSI text: Windows-1252. Python's
# cp1252 codec has no character for 0x81, 0x8D, 0x8F, 0x90 and 0x9D and would
# refuse them; Windows maps them to the control characters of the same number,
# and so do these, so every byte decodes and encodes back unchanged.
_ANSI_FROM_LATIN1 = {b: bytes([b]).decode('cp1252') for b in range(0x80, 0xA0)
                     if b not in (0x81, 0x8D, 0x8F, 0x90, 0x9D)}
_ANSI_DECODE = str.maketrans({chr(b): c for b, c in _ANSI_FROM_LATIN1.items()})
_ANSI_ENCODE = str.maketrans({c: chr(b) for b, c in _ANSI_FROM_LATIN1.items()})


def decode_ansi(raw: bytes) -> str:
    """A .UTSCH string's bytes as text (Windows-1252, every byte kept)."""
    return raw.decode('latin-1').translate(_ANSI_DECODE)


def encode_ansi(text: str) -> bytes:
    """The inverse of decode_ansi. Raises UnicodeEncodeError for a character
    Windows-1252 cannot hold."""
    return text.translate(_ANSI_ENCODE).encode('latin-1')


def _read_short_cstring(stream: bytes, off: int) -> Tuple[str, int]:
    """Read a length-prefixed string in its one-byte-length form.

    The longer forms exist in the serialization but are not expected for the
    two version strings, and reading them would be untested code, so they are
    rejected instead of guessed at.
    """
    if off >= len(stream):
        raise MalformedStreamError("stream ends inside the header")
    n = stream[off]
    if n == 0xFF:
        raise MalformedStreamError(
            "long-form string in the header, not expected")
    end = off + 1 + n
    if end > len(stream):
        raise MalformedStreamError("header string runs past the end")
    return decode_ansi(stream[off + 1:end]), end


def parse_header(stream: bytes) -> DocumentHeader:
    """Parse the schema number and version pair at the start of the stream.

    Any values are returned; DocumentHeader.is_verified_layout tells whether
    they are the ones whose body layout is known, and the readers of the
    body (read_prologue, kiuc_binary_walk) refuse anything else.
    """
    if len(stream) < 4:
        raise MalformedStreamError("stream too short for a schema number")
    schema = struct.unpack_from('<I', stream, 0)[0]
    major, off = _read_short_cstring(stream, 4)
    minor, off = _read_short_cstring(stream, off)
    return DocumentHeader(schema=schema, version_major=major,
                          version_minor=minor, body_offset=off)


# ── prologue layer: variants and fonts ───────────────────────────────────
#
# The stream body opens with two short lists -- the document variants and the
# fonts -- and then the net array. Reading them needs the archive's object
# numbering (classes and objects share one counter), so this layer carries a
# minimal archive reader; a reader of the rest of the body builds on the same
# primitives.

PRO_OK = 'ok'
PRO_MISMATCH = 'mismatch'
PRO_ERROR = 'error'
PRO_NA = 'not applicable'

_MAX_VARIANTS = 0x100
_MAX_FONTS = 0x3000
_LOGFONT_SIZE = 60


@dataclass(frozen=True)
class DocVariant:
    """A document variant: a name and two dwords (the default variant is
    "DEFAULT", 1, 0)."""
    name: str
    visible: int      # 1 on the variant Ulticap shows, 0 on the others
    index: int        # the variant's index (0, 1, 2, ...)


@dataclass(frozen=True)
class LogFont:
    """A font description: a Windows LOGFONTA, 60 bytes.

    The bytes after the terminating NUL of the 32-byte face-name field hold
    leftover memory and differ between saves of the same design, so they are
    kept in `raw` but left out of comparisons.
    """
    height: int
    width: int
    escapement: int
    orientation: int
    weight: int
    italic: int
    underline: int
    strike_out: int
    char_set: int
    out_precision: int
    clip_precision: int
    quality: int
    pitch_and_family: int
    face_name: str
    raw: bytes = field(default=b'', compare=False, repr=False)


@dataclass(frozen=True)
class FontEntry:
    """One entry of the font list: a font and two dwords."""
    font: LogFont
    usage: int        # usage count: at least the number of texts using the font
    index: int        # the entry's position in the list (0, 1, 2, ...)


@dataclass(frozen=True)
class Prologue:
    """The variants and the font list, and where they end."""
    header: DocumentHeader
    variants: Tuple[DocVariant, ...]
    fonts: Tuple[FontEntry, ...]
    net_count_offset: int         # offset of the word that counts the nets
    net_count: Optional[int]      # that word (None if the stream ends there)
    next_index: int               # next number in the archive's numbering


@dataclass(frozen=True)
class PrologueCheck:
    """Outcome of check_prologue(): never an exception."""
    status: str                   # one of the PRO_* values
    detail: str = ''
    variants: Optional[int] = None
    fonts: Tuple[LogFont, ...] = ()
    net_count_offset: Optional[int] = None
    net_count: Optional[int] = None


class _Archive:
    """Minimal reader for the object-archive encoding of the stream body.

    A pointer is introduced by a 16-bit tag: 0xFFFF a new class (schema,
    name length, name) followed by the object; 0x8000 | k a new object of the
    class already registered as number k; anything else below 0x7FFF a
    back-reference to an earlier object; 0 a null pointer. Classes and objects
    share one numbering that starts at 1: a new class takes the next number
    and the object after it the next, a new object of a known class the next.
    Only what the prologue needs is implemented (new objects, no
    back-references, no large-index escape).
    """

    def __init__(self, data: bytes, pos: int):
        self.data = data
        self.pos = pos
        self.next_index = 1
        self.classes: Dict[int, str] = {}

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise MalformedStreamError("stream ends inside an object")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def u16(self) -> int:
        return struct.unpack('<H', self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack('<I', self.take(4))[0]

    def cstring(self) -> str:
        text, self.pos = _read_short_cstring(self.data, self.pos)
        return text

    def new_object(self, expected: str) -> None:
        """Read the tag of a new object and require it to be of class
        `expected`."""
        tag = self.u16()
        if tag == 0xFFFF:
            self.u16()                                # the class's schema number
            length = self.u16()
            if not 1 <= length <= 64:
                raise MalformedStreamError("implausible class name length")
            name = self.take(length).decode('latin-1')
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                raise MalformedStreamError("implausible class name")
            self.classes[self.next_index] = name
            self.next_index += 2                      # the class, then its first object
        elif tag & 0x8000:
            name = self.classes.get(tag & 0x7FFF)
            if name is None:
                raise MalformedStreamError("tag names a class never registered")
            self.next_index += 1
        else:
            raise MalformedStreamError(
                f"expected a new {expected} object, found a null or back-reference")
        if name != expected:
            raise MalformedStreamError(f"expected a {expected} object, found {name}")


def _parse_logfont(raw: bytes) -> LogFont:
    height, width, escapement, orientation, weight = struct.unpack_from('<5i', raw, 0)
    (italic, underline, strike_out, char_set, out_prec, clip_prec, quality,
     pitch) = raw[20:28]
    face = decode_ansi(raw[28:60].split(b'\0')[0])
    return LogFont(height, width, escapement, orientation, weight, italic,
                   underline, strike_out, char_set, out_prec, clip_prec,
                   quality, pitch, face, raw)


def read_prologue(stream: bytes) -> Prologue:
    """Parse the document variants and the font list.

    Layout (verified for schema 0x230, version 5.33): a word counting the
    variants, each a `CDocVariant` object (name, dword, dword); a word
    counting the fonts, each a `CAttribFontInfo` object made of one
    `CSerialFont` object (60 bytes, a LOGFONTA) and two dwords. The next
    word counts the nets. Raises UtschError if the stream does not follow
    this layout.
    """
    header = parse_header(stream)
    if not header.is_verified_layout:
        raise UnsupportedSchemaError(
            "prologue layout verified only for schema "
            f"{VERIFIED_SCHEMA:#x}, version {'.'.join(VERIFIED_VERSION)}")
    ar = _Archive(stream, header.body_offset)
    count = ar.u16()
    if count > _MAX_VARIANTS:
        raise MalformedStreamError(f"implausible variant count {count}")
    variants = []
    for _ in range(count):
        ar.new_object('CDocVariant')
        variants.append(DocVariant(ar.cstring(), ar.u32(), ar.u32()))
    count = ar.u16()
    if count > _MAX_FONTS:
        raise MalformedStreamError(f"implausible font count {count}")
    fonts = []
    for _ in range(count):
        ar.new_object('CAttribFontInfo')
        ar.new_object('CSerialFont')
        font = _parse_logfont(ar.take(_LOGFONT_SIZE))
        fonts.append(FontEntry(font, ar.u32(), ar.u32()))
    net_count = (struct.unpack_from('<H', stream, ar.pos)[0]
                 if ar.pos + 2 <= len(stream) else None)
    return Prologue(header=header, variants=tuple(variants), fonts=tuple(fonts),
                    net_count_offset=ar.pos, net_count=net_count,
                    next_index=ar.next_index)


def check_prologue(stream: bytes) -> PrologueCheck:
    """Parse the prologue and test that it ends where the nets begin.

    The independent test is the first net class registration in the stream:
    the word that counts the nets sits two bytes before it. A stream with no
    net class must instead have a zero count there. Never raises.
    """
    try:
        pro = read_prologue(stream)
    except UnsupportedSchemaError as exc:
        return PrologueCheck(PRO_NA, detail=str(exc))
    except UtschError as exc:
        return PrologueCheck(PRO_ERROR, detail=str(exc))
    fonts = tuple(entry.font for entry in pro.fonts)
    base = dict(variants=len(pro.variants), fonts=fonts,
                net_count_offset=pro.net_count_offset, net_count=pro.net_count)
    first_net = stream.find(_NET_REGISTRATION)
    if first_net >= 0:
        if first_net - 2 != pro.net_count_offset:
            return PrologueCheck(
                PRO_MISMATCH, f"prologue ends at {pro.net_count_offset:#x} but "
                f"the first net class begins at {first_net:#x}", **base)
    elif pro.net_count != 0:
        return PrologueCheck(
            PRO_MISMATCH, f"no net class, but the count word is {pro.net_count}",
            **base)
    return PrologueCheck(PRO_OK, **base)


# ── net-array self-check ─────────────────────────────────────────────────
#
# A consistency check, not a parse. The stream begins with a few small
# containers whose sizes fix how far the archive's object numbering has
# advanced by the time the net array starts; from that the tag that introduces
# every later net can be predicted, and counted, and compared with the net
# count written just before the array. Agreement is strong evidence that the
# numbering model and the array layout are right for that file; disagreement
# is a finding to investigate, not proof of a bug here.
#
# Nothing in it assumes a single sheet: the net array belongs to the document
# as a whole, so hierarchical designs go through the same test.

NET_OK = 'ok'
NET_MISMATCH = 'mismatch'
NET_NONE = 'no nets found'
NET_NA = 'not applicable'

_DOC_VARIANT_REGISTRATION = b'\xff\xff\x12\x02\x0b\x00CDocVariant'
_NET_REGISTRATION = b'\xff\xff\x15\x02\x04\x00CNet'


@dataclass(frozen=True)
class NetArrayCheck:
    status: str                       # one of the NET_* values
    declared: Optional[int] = None    # the count written before the array
    found: Optional[int] = None       # nets located by their tags
    detail: str = ''


def _u16(stream: bytes, off: int) -> Optional[int]:
    if off < 0 or off + 2 > len(stream):
        return None
    return struct.unpack_from('<H', stream, off)[0]


def check_net_array(stream: bytes) -> NetArrayCheck:
    """Compare the declared net count with the nets found by their tags.

    Only the verified layout (see DocumentHeader.is_verified_layout) with a
    single document variant is handled; anything else is reported as not
    applicable rather than guessed at.
    """
    try:
        hdr = parse_header(stream)
    except UtschError as exc:
        return NetArrayCheck(NET_NA, detail=str(exc))
    if not hdr.is_verified_layout:
        return NetArrayCheck(NET_NA, detail="layout not verified")

    # Start of the stream body: <variant count> <variant objects> <font count>.
    # Each object takes one index in the archive's numbering, plus one for
    # each class the first time it appears; that fixes the index of the net
    # class (variants + 2 * fonts + 4 when there is exactly one variant).
    off = hdr.body_offset
    variants = _u16(stream, off)
    if variants != 1:
        return NetArrayCheck(NET_NA, detail=f"{variants} document variants (only 1 is handled)")
    off += 2
    if stream[off:off + len(_DOC_VARIANT_REGISTRATION)] != _DOC_VARIANT_REGISTRATION:
        return NetArrayCheck(NET_NA, detail="unexpected first object")
    off += len(_DOC_VARIANT_REGISTRATION)
    if off >= len(stream):
        return NetArrayCheck(NET_NA, detail="stream ends inside the first object")
    off += 1 + stream[off] + 8            # its name string and two dwords
    fonts = _u16(stream, off)
    if fonts is None or fonts > 0x3000:
        return NetArrayCheck(NET_NA, detail="implausible font count")

    first = stream.find(_NET_REGISTRATION, off)
    if first < 0:
        return NetArrayCheck(NET_NONE, detail="no net class registration in the stream")
    declared = _u16(stream, first - 2)
    if declared is None or declared == 0 or declared == 0xFFFF:
        return NetArrayCheck(NET_NA, detail=f"unusable net count {declared}")
    index = variants + 2 * fonts + 4
    if index >= 0x7FFF:
        return NetArrayCheck(NET_NA, detail="class index needs the long tag form")

    # Every net after the first starts with the class's short tag, preceded by
    # the tail of the object before it: a sheet reference (a word whose value
    # is specific to the file) and a closing dword that is a flag, 1 or 0
    # depending on the connection. Requiring exactly 1 misses every net whose
    # connection has the flag 0, while requiring nothing at all counts stray
    # occurrences of the tag (one on a real file), so the prefix accepts 0 or 1.
    # The word after the tag is the net's connection count and must be
    # plausible to count the hit.
    pattern = re.compile(rb'[\x00\x01]\x00\x00\x00'
                         + re.escape(struct.pack('<H', 0x8000 | index)))
    found = 1
    pos = first + len(_NET_REGISTRATION)
    while True:
        m = pattern.search(stream, pos)
        if m is None:
            break
        conns = _u16(stream, m.end())
        if conns is not None and 1 <= conns <= 4096:
            found += 1
        pos = m.start() + 1
    status = NET_OK if found == declared else NET_MISMATCH
    return NetArrayCheck(status, declared, found,
                         f"declared {declared}, found {found} (tag 0x{0x8000 | index:04x})")
