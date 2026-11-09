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
kiuc_binary_write.py -- Writing corrections back into a .UTSCH

KIUC reads a .UTSCH (kiuc_binary, kiuc_binary_walk); this module writes one
back with a correction applied. It does not re-serialise the design: it
replaces individual strings in the original stream and puts the stream into a
new compound file. That is enough for the corrections KIUC makes, and it
keeps every byte that is not corrected exactly as Ulticap wrote it.

Why replacing a string is safe: the stream is an MFC object archive, in which
a pointer is an object *number*, never a byte offset, and nothing records the
length of an object. The only length is each string's own length prefix. A
string can therefore grow or shrink without touching anything else, as long as
no object is added or removed.

Renaming a reference designator (rename_designators) replaces the two strings
that hold it: the text of its symbol and its key in the document's designator
map. Ulticap's own rename changes the same two strings and, besides them, only
data nothing reads back: font usage counts (which need only be at least the
number of texts using a font), the on-screen frames of two texts, and a copy of
the part's designator text properties into the symbol's own, otherwise unused,
text record. None of these is reproduced. The map keeps its stored key order:
Ulticap writes a map in hash order and reverses it within each hash bucket on
every load and save, so the order has no meaning, while moving an entry could
move the first full copy of an object and renumber the archive.

The container (build_container) follows the layout Ulticap V5.72 itself
writes: two free sectors, the directory at sector 2, the allocation table from
sector 3, then the mini allocation table and the mini stream (a stream under
4,096 bytes) or the stream itself. Rebuilt this way, a file Ulticap saved
without leftover sectors comes out byte for byte the same, apart from the two
free sectors and the unused tail of the stream's last sector, which hold
leftover memory in Ulticap's files and zeros here.

Public entry points:
    encode_string(text)                 -> bytes   MFC's string form
    rename_designators(stream, renames) -> bytes   a new Contents stream
    build_container(stream, modified)   -> bytes   a new .UTSCH file
    root_modified_time(data)            -> int     a file's own save time (FILETIME)
    filetime(when=None)                 -> int     a datetime as FILETIME
"""
from __future__ import annotations

import datetime
import math
import struct
from typing import Iterable, List, Mapping, Optional, Tuple

from kiut.schematic.kiuc_binary import EXPECTED_ROOT_CLSID, UtschError, encode_ansi
from kiut.schematic.kiuc_binary_walk import read_document


class WriteError(UtschError):
    """A correction that cannot be written, or whose result does not read back."""


# ── strings ──────────────────────────────────────────────────────────────

def encode_string(text: str) -> bytes:
    """A string as MFC's archive writes it: a length byte; for 255 characters or more the byte
    0xFF and a word; for 65,534 or more the word 0xFFFF and a dword. ANSI (Windows-1252,
    kiuc_binary.encode_ansi) only: the files carry no wide strings."""
    try:
        raw = encode_ansi(text)
    except UnicodeEncodeError as exc:
        raise WriteError(f"{text!r} has a character a .UTSCH string cannot hold") from exc
    n = len(raw)
    if n < 0xFF:
        return bytes([n]) + raw
    if n < 0xFFFE:
        return b'\xff' + struct.pack('<H', n) + raw
    return b'\xff\xff\xff' + struct.pack('<I', n) + raw


def _splice(stream: bytes, edits: Iterable[Tuple[int, int, bytes]]) -> bytes:
    """Replace each (start, end) span of the stream with its bytes; spans must not overlap."""
    ordered = sorted(edits, key=lambda e: e[0])
    for (_, end, _), (start, _, _) in zip(ordered, ordered[1:]):
        if start < end:
            raise WriteError("internal error: overlapping edits")
    out = bytearray()
    pos = 0
    for start, end, data in ordered:
        out += stream[pos:start]
        out += data
        pos = end
    out += stream[pos:]
    return bytes(out)


# ── designators ──────────────────────────────────────────────────────────

def rename_designators(stream: bytes, renames: Mapping[str, str]) -> bytes:
    """Return a copy of a Contents stream with reference designators renamed.

    `renames` maps a stored designator -- a key of the designator map, i.e. without the unit
    letter of a multi-unit part -- to its new name. The new name may not be empty, may not hold
    '!' (Ulticap's mark for a placeholder designator) and may not equal another designator of the
    design, ignoring case: Ulticap upper-cases the map's keys when it loads a file.

    The result is checked before it is returned: it must walk to its last byte with the same
    number of objects, and hold every new name in place of the old. Raises WriteError otherwise.
    """
    doc = read_document(stream)
    keys = doc.designator_map
    edits: List[Tuple[int, int, bytes]] = []
    taken = {k.upper() for k in keys if k not in renames}
    for old, new in renames.items():
        symbol = keys.get(old)
        if old not in keys or symbol is None or symbol.cls != 'CSymbol':
            raise WriteError(f"{old!r} is not a designator of this design")
        if symbol['text']['string'] != old:
            raise WriteError(f"{old!r}: the symbol's own text does not match its map key")
        if not new or '!' in new:
            raise WriteError(f"{new!r} is not a valid designator")
        if new.upper() in taken:
            raise WriteError(f"{new!r} is already used in this design")
        taken.add(new.upper())
        encoded = encode_string(new)
        edits.append((*doc.designator_spans[symbol], encoded))
        edits.append((*doc.designator_key_spans[old], encoded))
    if not edits:
        return stream
    result = _splice(stream, edits)

    # Read the result back once: it must walk to its last byte with the original's object count.
    try:
        back = read_document(result)
    except UtschError as exc:
        raise WriteError(f"the corrected stream does not read back ({exc})") from exc
    if back.end_offset != len(result) or back.objects != doc.objects:
        raise WriteError("the corrected stream does not read back: "
                         f"{back.objects} objects, ending at {back.end_offset:#x} of {len(result):#x}")
    check = back.designator_map
    expected = {renames.get(k, k) for k in keys}
    if set(check) != expected or any(check[new] is None or check[new]['text']['string'] != new
                                     for new in renames.values()):
        raise WriteError("the corrected stream does not hold the new designators in place of the old")
    return result


# ── container ────────────────────────────────────────────────────────────

_SECTOR = 512
_MINI_SECTOR = 64
_MINI_CUTOFF = 4096
_PER_SECTOR = _SECTOR // 4                  # allocation-table entries per sector
_HEADER_FAT = 109                           # allocation-table sectors listed in the header
_FREE, _END, _FAT, _DIFAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
_NONE = 0xFFFFFFFF
_RED, _BLACK = 0, 1                         # directory entry colours
_STORAGE_ROOT, _STREAM = 5, 2


def _dir_entry(name: str, kind: int, colour: int, child: int, start: int, size: int,
               clsid: bytes = bytes(16), modified: int = 0) -> bytes:
    e = bytearray(128)
    if name:
        raw = name.encode('utf-16-le') + b'\0\0'
        e[0:len(raw)] = raw
        struct.pack_into('<H', e, 64, len(raw))
    e[66] = kind
    e[67] = colour
    struct.pack_into('<III', e, 68, _NONE, _NONE, child)
    e[80:96] = clsid
    struct.pack_into('<QQ', e, 100, 0, modified)          # creation time 0, as Ulticap
    struct.pack_into('<II', e, 116, start, size)
    return bytes(e)


def build_container(stream: bytes, modified: int) -> bytes:
    """A .UTSCH file holding `stream` as its Contents, laid out as Ulticap V5.72 writes one.

    `modified` is the root storage's modification time as a Windows FILETIME (the only time
    Ulticap sets): root_modified_time() of the original file keeps it, filetime() gives now.

    Layout: header; sectors 0 and 1 free; the directory (one sector: the root entry, Contents and
    two empty entries) at sector 2; the allocation table from sector 3; then the mini allocation
    table and the mini stream for a stream under 4,096 bytes, or the stream itself. A stream large
    enough to need more than 109 allocation-table sectors (about 6.9 MB) needs the header's
    extension (DIFAT) sectors; as in Ulticap's own fresh saves, each one comes right after the
    first table sector it has to list (the 110th, the 237th, ...), and the last one's link to a
    next is 0xFFFFFFFF (free), not end-of-chain.
    """
    small = len(stream) < _MINI_CUTOFF
    if small:
        n_mini = math.ceil(len(stream) / _MINI_SECTOR)
        payload = stream + bytes(n_mini * _MINI_SECTOR - len(stream))
        n_minifat = math.ceil(n_mini / _PER_SECTOR)
    else:
        payload = stream
        n_minifat = 0
    n_data = math.ceil(len(payload) / _SECTOR)

    # The table's own sectors (and the extension's) are counted in what it describes.
    n_fat = n_difat = 0
    while True:
        total = 3 + n_fat + n_difat + n_minifat + n_data
        need_fat = math.ceil(total / _PER_SECTOR)
        need_difat = math.ceil(max(0, need_fat - _HEADER_FAT) / (_PER_SECTOR - 1))
        if (need_fat, need_difat) == (n_fat, n_difat):
            break
        n_fat, n_difat = need_fat, need_difat

    fat = [_FREE] * (n_fat * _PER_SECTOR)
    fat[2] = _END                                         # the directory
    # Table and extension sectors in Ulticap's order: an extension sector follows the first
    # table sector it lists -- table sector 110 (index 109), then every 127 further.
    order: List[str] = []
    while order.count('F') < n_fat:
        order.append('F')
        listed = order.count('F') - _HEADER_FAT
        if listed > 0 and (listed - 1) % (_PER_SECTOR - 1) == 0:
            order.append('D')
    fat_secs = [3 + i for i, kind in enumerate(order) if kind == 'F']
    difat_secs = [3 + i for i, kind in enumerate(order) if kind == 'D']
    for s in fat_secs:
        fat[s] = _FAT
    for s in difat_secs:
        fat[s] = _DIFAT
    pos = 3 + n_fat + n_difat

    def chain(start: int, count: int) -> None:
        for i in range(count):
            fat[start + i] = start + i + 1 if i < count - 1 else _END

    minifat_start = _END
    if small:
        minifat_start = pos
        chain(pos, n_minifat)
        pos += n_minifat
    data_start = pos
    chain(pos, n_data)

    if small:
        root = _dir_entry('Root Entry', _STORAGE_ROOT, _RED, 1, data_start, len(payload),
                          EXPECTED_ROOT_CLSID, modified)
        contents = _dir_entry('Contents', _STREAM, _BLACK, _NONE, 0, len(stream))
        minifat = [i + 1 for i in range(n_mini - 1)] + [_END]
        minifat += [_FREE] * (n_minifat * _PER_SECTOR - n_mini)
    else:
        root = _dir_entry('Root Entry', _STORAGE_ROOT, _RED, 1, _END, 0, EXPECTED_ROOT_CLSID, modified)
        contents = _dir_entry('Contents', _STREAM, _BLACK, _NONE, data_start, len(stream))
        minifat = []
    empty = _dir_entry('', 0, _RED, _NONE, 0, 0)

    header = bytearray(_SECTOR)
    header[0:8] = bytes.fromhex('D0CF11E0A1B11AE1')
    struct.pack_into('<HHHHH', header, 24, 0x3E, 3, 0xFFFE, 9, 6)   # version 3, 512-byte sectors
    struct.pack_into('<IIII', header, 40, 0, n_fat, 2, 0)
    struct.pack_into('<IIIII', header, 56, _MINI_CUTOFF, minifat_start, n_minifat,
                     difat_secs[0] if difat_secs else _END, n_difat)
    listed = fat_secs[:_HEADER_FAT]
    struct.pack_into(f'<{_HEADER_FAT}I', header, 76, *(listed + [_FREE] * (_HEADER_FAT - len(listed))))

    out = bytearray(header)
    out += bytes(2 * _SECTOR)                             # sectors 0 and 1, free
    out += root + contents + empty + empty
    blocks = {}                                           # sector -> bytes, table and extension
    for i, sec in enumerate(fat_secs):
        blocks[sec] = struct.pack(f'<{_PER_SECTOR}I', *fat[i * _PER_SECTOR:(i + 1) * _PER_SECTOR])
    rest = fat_secs[_HEADER_FAT:]
    per = _PER_SECTOR - 1
    for i, sec in enumerate(difat_secs):
        part = rest[i * per:(i + 1) * per]
        nxt = difat_secs[i + 1] if i + 1 < n_difat else _FREE      # Ulticap: free, not end-of-chain
        blocks[sec] = struct.pack(f'<{_PER_SECTOR}I', *(part + [_FREE] * (per - len(part)) + [nxt]))
    for sec in sorted(blocks):
        out += blocks[sec]
    if minifat:
        out += struct.pack(f'<{len(minifat)}I', *minifat)
    out += payload + bytes(n_data * _SECTOR - len(payload))
    return bytes(out)


def root_modified_time(data: bytes) -> int:
    """The root storage's modification time (FILETIME) of a compound file: when it was saved.
    The root entry is the first entry of the directory's first sector."""
    try:
        shift, first_dir = struct.unpack_from('<H', data, 30)[0], struct.unpack_from('<I', data, 48)[0]
        return struct.unpack_from('<Q', data, (1 << shift) * (first_dir + 1) + 108)[0]
    except struct.error as exc:
        raise WriteError("not a readable compound file") from exc


def filetime(when: Optional[datetime.datetime] = None) -> int:
    """A moment (default: now) as a Windows FILETIME: 100-ns steps since 1601-01-01 UTC."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    if when.tzinfo is None:
        when = when.astimezone()
    epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
    delta = when - epoch
    return (delta.days * 86_400 + delta.seconds) * 10_000_000 + delta.microseconds * 10
