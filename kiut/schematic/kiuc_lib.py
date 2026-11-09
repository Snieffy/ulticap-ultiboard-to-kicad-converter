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
kiuc_lib.py -- Reader for Ulticap for DOS symbol libraries (.LIB)

A DOS library is a flat binary file (FILEFORMAT_ULTICAP_LIB.md): a header, an index
of fixed-size slots, the symbol bodies and two string blocks shared by all
symbols, one of attribute tags and one of values. A symbol body holds the
same records as a shape definition (*S) of a .SCH file -- size, outline,
pins, circles and arcs, attribute lines -- in binary form.

So each symbol is written back as that *S text and read by kiuc_ascii's own
parser: a library symbol converts exactly as the same symbol in a DOS
design does, the DOS font's characters included (CP437 with Ulticap's own
glyphs, kiuc_model.DOS_GLYPHS).
"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import List

from kiut.schematic.kiuc_ascii import _AsciiParser, SCH_ENCODING
from kiut.schematic.kiuc_model import DOS_GLYPHS, Library

SIGNATURE = b'ULTImate LIB\0'
# Ultiboard for DOS writes .LIB files too: shape (footprint) libraries, a
# different container, told apart by a hyphen in the signature (KIUB's
# FILEFORMAT_ULTIBOARD_LIB_TEC.md). KIUB converts them.
ULTIBOARD_SIGNATURE = b'ULTImate-LIB\0'
ULTIBOARD_MESSAGE = 'This is an Ultiboard shape library, not an Ulticap one: convert it with KIUB.'

_INDEX_SLOT = 22          # bytes per index slot
_NAME_AT, _NAME_LEN = 9, 13
# Start and sweep are signed: a start of -5760 (-90 degrees) occurs, and draws as 17280.
_CIRCLE = struct.Struct('<hhhhhBBB')        # cx cy r rotate angle colour thick linetype
_ATTRIBUTE = struct.Struct('<hhhhHBBHH')    # dx dy poe-x poe-y size colour visibility tag value
_PIN = struct.Struct('<hhBB')               # x y format rotation
_COUNTS = struct.Struct('<6H')              # outline pairs, pins, circles, attributes, width, height

# The version line of the .SCH text handed to the parser: a DOS version, so
# arcs are read the DOS way (the library tools are version 4.83).
_SCH_HEAD = ['*P CAP', '4 83 00000000', '-3000, -2150, 3000, 2150, 50;']


class LibError(ValueError):
    """A file that is not a DOS .LIB, or one laid out other than expected."""


def looks_like_ultiboard_lib(path) -> bool:
    """Whether the file starts with an Ultiboard DOS shape library's signature."""
    try:
        with open(path, 'rb') as f:
            return f.read(len(ULTIBOARD_SIGNATURE)) == ULTIBOARD_SIGNATURE
    except OSError:
        return False


def looks_like_lib(path) -> bool:
    """Whether the file starts with a DOS library's signature."""
    try:
        with open(path, 'rb') as f:
            return f.read(len(SIGNATURE)) == SIGNATURE
    except OSError:
        return False


class _File:
    def __init__(self, data: bytes):
        if data[:len(ULTIBOARD_SIGNATURE)] == ULTIBOARD_SIGNATURE:
            raise LibError(ULTIBOARD_MESSAGE)
        if data[:len(SIGNATURE)] != SIGNATURE:
            raise LibError('not a DOS .LIB file (no "ULTImate LIB" signature)')
        if len(data) < 0x22:
            raise LibError('the file ends inside its header')
        self.data = data
        self.slots, = struct.unpack_from('<H', data, 0x10)
        self.index_at, = struct.unpack_from('<H', data, 0x14)
        self.tags_at, self.values_at = struct.unpack_from('<II', data, 0x1A)
        if not (self.index_at + self.slots * _INDEX_SLOT <= len(data)
                and self.tags_at <= self.values_at <= len(data)):
            raise LibError('header offsets outside the file')

    def string(self, block: int, offset: int) -> str:
        at = block + offset
        end = self.data.find(b'\0', at)
        if at >= len(self.data) or end < 0:
            raise LibError(f'string offset {offset} outside the file')
        return self.data[at:end].decode(SCH_ENCODING)

    def entries(self):
        """(name, body offset, body length) of every used index slot, in
        index order (sorted by name)."""
        for i in range(self.slots):
            at = self.index_at + i * _INDEX_SLOT
            used, offset, length = struct.unpack_from('<HIH', self.data, at)
            if not used:
                continue
            name = self.data[at + _NAME_AT:at + _NAME_AT + _NAME_LEN].split(b'\0')[0]
            yield name.decode(SCH_ENCODING), offset, length

    def symbol_text(self, name: str, offset: int, length: int) -> List[str]:
        """One body as the *S block of a .SCH file."""
        d = self.data
        if offset + length > len(d):
            raise LibError('the body runs past the end of the file')
        pairs, npins, ncircles, nattrs, width, height = _COUNTS.unpack_from(d, offset)
        expected = (_COUNTS.size + 4 * pairs + _PIN.size * npins + _CIRCLE.size * ncircles
                    + _ATTRIBUTE.size * nattrs)
        if expected != length:
            raise LibError(f'the body holds {length} bytes, its counts say {expected}')
        p = offset + _COUNTS.size
        outline = struct.unpack_from(f'<{2 * pairs}h', d, p)
        p += 4 * pairs
        pins = []
        for _ in range(npins):
            x, y, fmt, rot = _PIN.unpack_from(d, p)
            p += _PIN.size
            pins.append(f'{fmt},{rot},{x},{y}')
        circles = []
        for _ in range(ncircles):
            circles.append(','.join(map(str, _CIRCLE.unpack_from(d, p))))
            p += _CIRCLE.size
        attrs = []
        for _ in range(nattrs):
            dx, dy, px, py, size, colour, vis, tag, value = _ATTRIBUTE.unpack_from(d, p)
            p += _ATTRIBUTE.size
            attrs.append(f'{dx} {dy} {px} {py} {size} {colour} {vis} '
                         f'{self.string(self.tags_at, tag)}={self.string(self.values_at, value)}')

        def section(items):     # comma-ended lines, the last one ';'-ended
            return [x + ',' for x in items[:-1]] + [items[-1] + ';'] if items else [';']
        return ([f'*S{name}', f'{width} {height}', ','.join(map(str, outline)) + ';']
                + section(pins) + section(circles) + attrs + [';'])


def read_library(path) -> Library:
    """Read a DOS .LIB into symbols named as in the library (and in a DOS
    design). A name met twice -- not seen, the index is unique -- gets '_2'
    and a warning; the parser's messages about a symbol are warnings."""
    path = Path(path)
    lib = _File(path.read_bytes())
    lines = list(_SCH_HEAD) + [f'*R {path.stem}']
    taken: set = set()
    warnings: List[str] = []
    count = 0
    for name, offset, length in lib.entries():
        count += 1
        unique, n = name, 2
        while unique in taken:
            unique, n = f'{name}_{n}', n + 1
        if unique != name:
            warnings.append(f'{name}: a second symbol of this name, written as {unique}')
        taken.add(unique)
        try:
            lines += lib.symbol_text(unique, offset, length)
        except (LibError, struct.error) as e:
            if count == 1:
                # An Ultiboard library is told apart by its signature
                # (_File); this is an Ulticap signature over another layout.
                raise LibError(f'not laid out as an Ulticap symbol library: '
                               f'symbol {name!r}: {e}') from e
            raise LibError(f'symbol {name!r}: {e}') from e
    lines += ['', ';', '', '**']
    lines = [ln.translate(DOS_GLYPHS) for ln in lines]
    sheet, errors = _AsciiParser(lines, name=path.name).parse()
    # The parser names a line of the text built here, which means nothing to
    # the reader of the log: keep the message, from the symbol on.
    warnings += [re.sub(r'^(ERROR: )?.*? line \d+: ', '', e) for e in errors]
    return Library(path=path, symbols=dict(sheet.symbols), warnings=warnings, count=count,
                   is_less_than_v500=sheet.is_less_than_v500)


__all__ = ['SIGNATURE', 'LibError', 'looks_like_lib', 'read_library']
