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
kiuc_binary_walk.py -- Reader for the object graph of a .UTSCH stream

kiuc_binary.py reads the container, the header and the two short lists that
open the stream body (the document variants and the fonts). This module reads
everything after them: the nets and, through the nets, the whole design --
symbols, parts, pins, shapes, attributes, wires, buses and the sheet -- and
the containers that close the stream.

The body is an MFC object archive. Objects are written depth-first, each with
a class tag, and a pointer to an object that was written earlier is a small
back-reference. Nothing in the stream gives the length of an object, so an
object can only be skipped by reading it; every class that occurs must
therefore have a layout here. A stream is fully understood when the walk ends
on its last byte, which is what check_walk() reports.

Two properties matter for real designs:

  * The graph is deep: a net reaches a connection, which reaches a pin, a
    part, a symbol, its shapes, and so on. The walk therefore uses an explicit
    stack of generators instead of recursion, so the depth of a design is
    limited by memory, not by the interpreter's recursion limit.
  * The graph is cyclic (a connection lists the pin that lists the
    connection). Nodes compare and hash by identity and print as one short
    line, never as their contents.

Public entry points:
    read_document(stream) -> Document       the tree; raises UtschError on failure
    check_walk(stream)    -> WalkCheck      the same, as a status; never raises

Field names are those of FILEFORMAT_ULTICAP_UTSCH.md; the three fields whose meaning is
not known are named by their type and position (a symbol pin's byte_c, a
sheet's dword_e, a wire's dword).
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from types import GeneratorType
from typing import Any, Dict, List, Optional, Tuple

from kiut.schematic.kiuc_binary import (MalformedStreamError, Prologue, UnsupportedSchemaError, decode_ansi,
                         UtschError, read_prologue)


# ── errors, results ──────────────────────────────────────────────────────

class UnknownClassError(MalformedStreamError):
    """An object of a class for which this module has no layout."""

    def __init__(self, name: str, offset: int):
        super().__init__(f"no layout for class {name} (object at offset {offset:#x})")
        self.class_name = name
        self.offset = offset


WALK_OK = 'ok'
WALK_MISMATCH = 'mismatch'
WALK_ERROR = 'error'
WALK_NA = 'not applicable'


class Node(dict):
    """One object of the stream.

    cls is the class name, idx the object's number in the archive's numbering,
    at the offset of its tag. Its fields are the dict items. Identity is
    object identity, and repr() is a single short line, because the graph is
    cyclic and printing a node's contents would print the design.
    """
    __slots__ = ('cls', 'idx', 'at')

    def __init__(self, cls: str, idx: int, at: int):
        super().__init__()
        self.cls = cls
        self.idx = idx
        self.at = at

    def __repr__(self) -> str:
        return f"<{self.cls} #{self.idx} @{self.at:#x}>"

    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other

    def __ne__(self, other: object) -> bool:
        return self is not other


@dataclass
class Document:
    """The whole body of a stream, after the header."""
    prologue: Prologue
    nets: List[Optional[Node]] = field(default_factory=list)
    library_map: Dict[str, Optional[Node]] = field(default_factory=dict)   # library name -> object
    designator_map: Dict[str, Optional[Node]] = field(default_factory=dict)  # reference designator -> symbol
    sheets: List[Optional[Node]] = field(default_factory=list)
    label_index: List[Optional[Node]] = field(default_factory=list)       # CObArrays of one CLabelInfo each
    net_name_map: Dict[str, Optional[Node]] = field(default_factory=dict)  # net name -> CObArray of CLabelInfo
    ole_items: List[Optional[Node]] = field(default_factory=list)
    end_word: int = 0                                                     # the WORD 0 that ends the body
    objects: int = 0                                                      # objects read after the prologue
    end_offset: int = 0
    class_counts: Dict[str, int] = field(default_factory=dict)
    # Where the designators lie in the stream, as (start, end) byte spans of the stored string,
    # length prefix included: kiuc_binary_write renames a designator by replacing exactly these
    # strings. A designator is stored twice -- as its symbol's text (every CSymbol and CSheet
    # node here) and as its key in the designator map -- and nowhere else.
    designator_spans: Dict[Node, Tuple[int, int]] = field(default_factory=dict)
    designator_key_spans: Dict[str, Tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class WalkCheck:
    """Outcome of check_walk(): never an exception."""
    status: str                    # one of the WALK_* values
    detail: str = ''
    objects: int = 0
    end_offset: int = 0
    stream_length: int = 0
    class_counts: Tuple[Tuple[str, int], ...] = ()
    unknown_class: Optional[str] = None


# ── reader ───────────────────────────────────────────────────────────────

# The schema numbers these classes carry in every file examined
# (FILEFORMAT_ULTICAP_UTSCH.md §3.6); the layouts below were checked against them. A
# file that registers one of these classes with a lower number is refused
# rather than read with a layout never checked against it.
_MIN_SCHEMA = {
    'CNet': 0x215,
    'CSymbolPartPin': 0x217,
    'CSymbolPart': 0x217,
    'CLabelInfo': 0x217,
    'CSymbolPin': 0x226,
    'CSheet': 0x226,
}

TAG_NULL, TAG_NEW_CLASS, TAG_CLASS, TAG_OBJECT = 'null', 'new class', 'class', 'object'

_CLASS_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_PTR = object()      # yielded by a decoder to ask the driver to read a pointer


class _Reader:
    """Cursor over the stream plus the archive's numbering.

    Classes and objects share one counter that starts at 1: a class takes the
    next number the first time it appears (as an object's class or as a
    marker), and an object takes the next number before its fields are read.
    """
    __slots__ = ('data', 'pos', 'end', 'next_index', 'classes', 'objs', 'counts', 'designator_spans')

    def __init__(self, data: bytes, pos: int):
        self.data = data
        self.pos = pos
        self.end = len(data)
        self.next_index = 1
        self.classes: Dict[int, str] = {}
        self.objs: Dict[int, Node] = {}
        self.counts: Dict[str, int] = {}
        self.designator_spans: Dict[Node, Tuple[int, int]] = {}

    # -- primitives --

    def _need(self, n: int) -> None:
        if self.pos + n > self.end:
            raise MalformedStreamError(f"stream ends inside an object (offset {self.pos:#x})")

    def u8(self) -> int:
        self._need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        self._need(2)
        v = struct.unpack_from('<H', self.data, self.pos)[0]
        self.pos += 2
        return v

    def u32(self) -> int:
        self._need(4)
        v = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return v

    def i32(self) -> int:
        self._need(4)
        v = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        return v

    def cstring(self) -> str:
        """A string: a length byte and the characters. A length byte of 0xFF is followed by a
        word holding the length; a word of 0xFFFF is followed by a dword holding it (strings of
        65,534 characters or more). A word of 0xFFFE would announce wide characters, which the
        files examined do not use."""
        n = self.u8()
        if n == 0xFF:
            n = self.u16()
            if n == 0xFFFE:
                raise MalformedStreamError(f"unsupported wide string at offset {self.pos:#x}")
            if n == 0xFFFF:
                n = self.u32()
        self._need(n)
        text = decode_ansi(self.data[self.pos:self.pos + n])
        self.pos += n
        return text

    def string_end(self, at: int) -> int:
        """The offset just past the string that starts at `at` (already read), by its length prefix."""
        n = self.data[at]
        at += 1
        if n == 0xFF:
            n = struct.unpack_from('<H', self.data, at)[0]
            at += 2
            if n == 0xFFFF:
                n = struct.unpack_from('<I', self.data, at)[0]
                at += 4
        return at + n

    def count(self, unit: int) -> int:
        """An element count; refuses a count that the rest of the stream could not hold at `unit` bytes each."""
        at = self.pos
        c = self.u16()
        if c == 0xFFFF:
            c = self.u32()
        if c * unit > self.end - self.pos:
            raise MalformedStreamError(f"implausible element count {c} at offset {at:#x}")
        return c

    def strarray(self) -> List[str]:
        return [self.cstring() for _ in range(self.count(1))]

    def points(self) -> List[Tuple[int, int]]:
        c = self.u16()
        self._need(8 * c)
        out = list(struct.iter_unpack('<ii', self.data[self.pos:self.pos + 8 * c]))
        self.pos += 8 * c
        return out

    # -- class tags --

    def _register(self) -> str:
        """After the 0xFFFF tag of a new class: schema, name; the class takes the next number."""
        at = self.pos
        schema = self.u16()
        n = self.u16()
        if not 1 <= n <= 64:
            raise MalformedStreamError(f"implausible class name length at offset {at:#x}")
        self._need(n)
        name = self.data[self.pos:self.pos + n].decode('latin-1')
        self.pos += n
        if not _CLASS_NAME.fullmatch(name):
            raise MalformedStreamError(f"implausible class name at offset {at:#x}")
        minimum = _MIN_SCHEMA.get(name)
        if minimum is not None and schema < minimum:
            raise UnsupportedSchemaError(
                f"class {name} has schema {schema:#x}; the layout implemented needs {minimum:#x} or later")
        self.classes[self.next_index] = name
        self.next_index += 1
        return name

    def tag(self) -> Tuple[int, int]:
        """Read a tag: (kind, index).

        A tag is a word: 0 is a null pointer; 0xFFFF introduces a class written in full; a word
        with the high bit set names an earlier class by its number, and any other word is a
        reference to an earlier object by its number. Numbers of 0x7FFF and above do not fit:
        the word 0x7FFF is then followed by a dword, which is the object number, or, for a class,
        the class number with the top bit set.
        """
        word = self.u16()
        if word == 0:
            return TAG_NULL, 0
        if word == 0xFFFF:
            return TAG_NEW_CLASS, 0
        if word == 0x7FFF:
            big = self.u32()
            if big & 0x80000000:
                return TAG_CLASS, big & 0x7FFFFFFF
            return TAG_OBJECT, big
        if word & 0x8000:
            return TAG_CLASS, word & 0x7FFF
        return TAG_OBJECT, word

    def marker(self, expect: Optional[str] = None) -> str:
        """A class marker: a class tag with no object after it."""
        at = self.pos
        kind, index = self.tag()
        if kind == TAG_NEW_CLASS:
            name = self._register()
        elif kind == TAG_CLASS:
            found = self.classes.get(index)
            if found is None:
                raise MalformedStreamError(f"marker at offset {at:#x} names a class never registered")
            name = found
        else:
            raise MalformedStreamError(f"expected a class marker at offset {at:#x}")
        if expect is not None and name != expect:
            raise MalformedStreamError(f"expected the marker of {expect} at offset {at:#x}, found {name}")
        return name


# ── decoders ─────────────────────────────────────────────────────────────
#
# A decoder fills a Node. It is a generator when it reads pointers: it yields
# _PTR to ask for one and receives the pointed-to Node (or None) back. Helpers
# that read pointers are generators too and are entered with `yield from`;
# their nesting is shallow. Helpers that read none are plain functions.

def _obarray(r: _Reader):
    """An array of pointers: a count, then that many pointers."""
    out = []
    for _ in range(r.count(2)):
        out.append((yield _PTR))
    return out


def _mapstrob(r: _Reader, spans: Optional[Dict[str, Tuple[int, int]]] = None):
    """A map from strings to pointers: a count, then (string, pointer) pairs. With `spans`, each
    key's byte span in the stream is recorded there."""
    out: Dict[str, Optional[Node]] = {}
    for _ in range(r.count(3)):
        at = r.pos
        key = r.cstring()
        if spans is not None:
            spans[key] = (at, r.pos)
        out[key] = yield _PTR
    return out


def _sap(r: _Reader):
    """The part shared by pins, shapes, symbols and wires: marker, the attribute array, two markers."""
    r.marker('CSchemaAttributePart')
    attributes = yield from _obarray(r)
    r.marker('CSchemaPart')
    r.marker('CUndoableObject')
    return attributes


def _draw(r: _Reader) -> Dict[str, Any]:
    """The drawing record: three dwords and a point array, then the closing marker."""
    pen = (r.u32(), r.u32(), r.u32())
    points = r.points()
    r.marker('CUndoableObject')
    return {'pen': pen, 'points': points}


def _draw2(r: _Reader) -> Dict[str, Any]:
    r.marker('CDrawObject')
    return _draw(r)


def _rectangle(r: _Reader) -> Dict[str, Any]:
    """A rectangle's or ellipse's own two extra fields, ahead of its drawing
    record: whether it is filled (0 or 1), then its fill colour. Verified
    against a deliberately filled/unfilled pair of otherwise-identical
    symbols -- both fields differed by exactly this, nothing else."""
    fill = (r.u32(), r.u32())
    r.marker('CTwoPointObject')
    rec = _draw2(r)
    rec['fill'] = fill
    return rec


def _polyline(r: _Reader) -> Dict[str, Any]:
    r.marker('CMultiPointObject')
    return _draw2(r)


def _line(r: _Reader) -> Dict[str, Any]:
    r.marker('CPolyLine')
    return _polyline(r)


def _uc_rect(r: _Reader) -> Dict[str, Any]:
    r.marker('CRectangle')
    return _rectangle(r)


def _uc_line(r: _Reader) -> Dict[str, Any]:
    r.marker('CLine')
    return _line(r)


def _text_properties(r: _Reader):
    frame = _uc_rect(r)
    props: Dict[str, Any] = {'frame': frame, 'x': r.i32(), 'y': r.i32(), 'key_type': r.u16(),
                             'anchor': r.u8(), 'visible': r.u8(), 'colour': r.u32(), 'vertical': r.u32()}
    props['font'] = yield _PTR
    r.marker('CSchemaPart')
    r.marker('CUndoableObject')
    return props


def _text(r: _Reader):
    """A text object: its string, then the properties (position, colour, font)."""
    string = r.cstring()
    r.marker('CTextProperties')
    props = yield from _text_properties(r)
    props['string'] = string
    return props


def _undo_block(r: _Reader):
    """The sheet's undo block: the live object list (the sheet's content) and, when the undo
    option was on, the ring of saved steps."""
    present = r.u32()
    block: Dict[str, Any] = {'present': present}
    if present:
        block['owner'] = yield _PTR
        history = r.u32()
        block['history'] = history
        if history:
            block['ring'] = [r.u32() for _ in range(4)]   # newest slot, current slot, flag, newest step
            block['newest_step'] = yield _PTR
        block['live'] = yield from _obarray(r)
        if history:
            block['steps'] = yield from _obarray(r)
    return block


def _symbol_fields(r: _Reader, n: Node):
    n['id'] = r.u32()                           # the symbol ID
    n['shape'] = yield _PTR
    n['in_netlist'] = r.u32()                   # 1 for components, 0 for power symbols, ports, sheets
    at = r.pos                                  # a text object starts with its string
    n['text'] = yield from _text(r)             # the reference designator
    r.designator_spans[n] = (at, r.string_end(at))
    n['parts'] = yield from _obarray(r)         # one part per unit
    n['pins'] = yield from _mapstrob(r)         # pin number -> symbol pin
    n['variants'] = yield from _mapstrob(r)
    n['attributes'] = yield from _sap(r)


def _d_net(r: _Reader, n: Node):
    n['connections'] = yield from _obarray(r)
    n['names'] = r.strarray()
    n['name'] = r.cstring()
    n['id'] = r.u32()


def _d_connection(r: _Reader, n: Node):
    n['items'] = yield from _obarray(r)
    n['x'] = r.i32()
    n['y'] = r.i32()
    n['copy_index'] = r.u32()
    n['sheet'] = yield _PTR
    n['connected'] = r.u32()
    n['net'] = yield _PTR


def _d_part_pin(r: _Reader, n: Node):
    n['shape_pin'] = yield _PTR
    n['part'] = yield _PTR
    n['connection'] = yield _PTR
    n['shown'] = r.u32()
    n['symbol_pin'] = yield _PTR
    n['colour'] = r.u32()
    n['number_label'] = yield from _text(r)
    n['attributes'] = yield from _sap(r)


def _d_shape_pin(r: _Reader, n: Node):
    n['x'] = r.i32()
    n['y'] = r.i32()
    n['internal_connect'] = r.u8()
    n['colour'] = r.u32()
    n['style'] = r.u8()                         # flags: 1 IEC, 2 clock, 4 inverted, 8 invisible, 16 bus, 32 common
    n['orientation'] = r.u32()
    n['pin_type'] = r.u8()
    n['swap'] = r.u8()
    n['number_labels'] = yield from _obarray(r)
    n['attributes'] = yield from _sap(r)


def _d_text(r: _Reader, n: Node):
    n['text'] = yield from _text(r)


def _d_uc_attribute(r: _Reader, n: Node):
    n['key'] = r.cstring()
    r.marker('CUcText')
    n['text'] = yield from _text(r)             # its string is the value


def _d_string_attribute(r: _Reader, n: Node) -> None:
    n['value'] = r.cstring()
    n['key'] = r.cstring()
    n['key_type'] = r.u16()
    r.marker('CSchemaPart')
    r.marker('CUndoableObject')


def _d_part(r: _Reader, n: Node):
    n['x'] = r.i32()
    n['y'] = r.i32()
    n['gate_swap'] = r.u32()                    # a copy of the gate drawing's external-gate-swap flag; 0 on frame parts
    n['orientation'] = r.u32()                  # 0-7, the ASCII format's rotation code
    n['gate_drawing'] = yield _PTR
    n['symbol'] = yield _PTR
    n['text'] = yield from _text(r)             # the unit letter
    n['pins'] = yield from _obarray(r)
    n['variants'] = yield from _obarray(r)
    n['attributes'] = yield from _sap(r)


def _d_sub_shape(r: _Reader, n: Node):
    n['graphics'] = yield from _obarray(r)
    n['frame'] = _uc_rect(r)
    n['pins'] = yield from _obarray(r)
    n['text'] = yield from _text(r)             # the "$" designator text
    n['gate_count'] = r.u32()
    n['name'] = r.cstring()
    n['external_gate_swap'] = r.u32()
    n['attributes'] = yield from _sap(r)


def _d_shape(r: _Reader, n: Node):
    n['gate_drawings'] = yield from _obarray(r)
    n['text'] = yield from _text(r)             # the designator template
    n['name'] = r.cstring()
    n['in_netlist'] = r.u32()
    n['attributes'] = yield from _sap(r)


def _d_symbol(r: _Reader, n: Node):
    yield from _symbol_fields(r, n)


def _d_symbol_pin(r: _Reader, n: Node):
    n['pin_type'] = r.u8()
    n['symbol'] = yield _PTR
    n['swap'] = r.u8()
    n['byte_c'] = r.u8()                        # meaning unknown; 0 in every file
    n['package_index'] = r.u32()
    n['placed_pins'] = yield from _obarray(r)
    n['text'] = yield from _text(r)             # the pin number
    n['attributes'] = yield from _sap(r)


def _d_symbol_variant(r: _Reader, n: Node):
    n['name'] = r.cstring()
    n['symbol'] = yield _PTR
    n['values'] = yield from _obarray(r)        # three CStringAttributes
    n['document_variants'] = yield from _obarray(r)
    n['alternatives'] = yield from _obarray(r)
    n['part_variants'] = yield from _obarray(r)


def _d_part_variant(r: _Reader, n: Node):
    """One unit's attributes for a variant. The trailing pointer is a back-reference to the
    owning CSymbolVariant, not to the CSymbolPart: a part finds its part variants through its
    own `variants` list, never by position in a symbol variant's part_variants."""
    n['attributes'] = yield from _sap(r)
    n['symbol_variant'] = yield _PTR


def _d_sheet(r: _Reader, n: Node):
    # The sheet's own fields get names of their own: the CSymbol fields that
    # follow (id, in_netlist, text) would otherwise overwrite them.
    n['view_x'] = r.i32()                       # the visible window's top-left
    n['view_y'] = r.i32()                       # corner, stored coordinates
    n['zoom'] = r.u32()                         # in percent
    n['grid'] = r.u32()                         # the sheet's grid: 25 in every file
    n['parent'] = yield _PTR
    n['dword_e'] = r.u32()                      # meaning unknown; 0 in every file
    n['name'] = yield from _text(r)             # the sheet's name
    n['connections'] = yield from _obarray(r)
    n['frame'] = _uc_rect(r)
    n['undo'] = yield from _undo_block(r)
    r.marker('CSymbol')                         # a sheet derives from a symbol
    yield from _symbol_fields(r, n)


def _d_label_info(r: _Reader, n: Node):
    n['attribute'] = yield _PTR                 # the attribute that names a net
    n['sheet'] = yield _PTR


def _d_wire(r: _Reader, n: Node):
    n['line'] = _uc_line(r)
    n['connections'] = yield from _obarray(r)
    n['dword'] = r.u32()                        # meaning unknown; 0 in every file
    n['attributes'] = yield from _sap(r)


def _d_bus(r: _Reader, n: Node):
    r.marker('CWire')
    yield from _d_wire(r, n)


def _d_ob_array(r: _Reader, n: Node):
    n['items'] = yield from _obarray(r)


def _d_uc_polyline(r: _Reader, n: Node) -> None:
    r.marker('CPolyLine')
    n['drawing'] = _polyline(r)


def _d_uc_arc(r: _Reader, n: Node) -> None:
    r.marker('CArc')
    n['drawing'] = _draw2(r)


def _d_uc_line(r: _Reader, n: Node) -> None:
    n['drawing'] = _uc_line(r)


def _d_uc_rect(r: _Reader, n: Node) -> None:
    n['drawing'] = _uc_rect(r)


def _d_uc_ellipse(r: _Reader, n: Node) -> None:
    r.marker('CEllipse')
    n['drawing'] = _rectangle(r)


def _d_uc_polygon(r: _Reader, n: Node) -> None:
    r.marker('CPolygon')
    # Same (filled, fill colour) pair as a rectangle or ellipse (_rectangle).
    n['fill'] = (r.u32(), r.u32())
    r.marker('CMultiPointObject')
    n['drawing'] = _draw2(r)


def _d_uc_pie(r: _Reader, n: Node) -> None:
    r.marker('CPie')
    # Same (filled, fill colour) pair as a rectangle or ellipse (_rectangle).
    n['fill'] = (r.u32(), r.u32())
    r.marker('CArc')
    n['drawing'] = _draw2(r)


def _sheet_marks(r: _Reader) -> None:
    """The two markers that close a sheet's own graphic."""
    r.marker('CSchemaPart')
    r.marker('CUndoableObject')


def _d_sheet_line(r: _Reader, n: Node) -> None:
    """A line drawn on the sheet itself: the record of a line, then two markers; no attributes."""
    n['drawing'] = _uc_line(r)
    _sheet_marks(r)


def _d_sheet_arc(r: _Reader, n: Node) -> None:
    r.marker('CArc')
    n['drawing'] = _draw2(r)
    _sheet_marks(r)


def _d_sheet_rect(r: _Reader, n: Node) -> None:
    n['drawing'] = _uc_rect(r)
    _sheet_marks(r)


def _d_doc_variant(r: _Reader, n: Node) -> None:
    """A document variant written inline (the prologue's are read by kiuc_binary)."""
    n['name'] = r.cstring()
    n['visible'] = r.u32()
    n['index'] = r.u32()


def _d_action_array(r: _Reader, n: Node):
    """One step of a sheet's undo history: its step number, then its change records."""
    n['step'] = r.u32()
    r.marker('CObArray')
    n['records'] = yield from _obarray(r)


def _d_undo_info(r: _Reader, n: Node):
    """One change record: its type (1 added, 2 removed, 3 changed), the object, its previous
    state (type 3; null otherwise) and its position in the live object list."""
    n['type'] = r.u8()
    n['object'] = yield _PTR
    n['previous'] = yield _PTR
    n['position'] = r.u32()


_DECODERS = {
    'CDocVariant': _d_doc_variant,
    'CSheetLine': _d_sheet_line,
    'CSheetArc': _d_sheet_arc,
    'CSheetRect': _d_sheet_rect,
    'CActionArray': _d_action_array,
    'CUndoInfo': _d_undo_info,
    'CNet': _d_net,
    'CConnection': _d_connection,
    'CSymbolPartPin': _d_part_pin,
    'CShapePin': _d_shape_pin,
    'CUcText': _d_text,
    'CUcAttribute': _d_uc_attribute,
    'CStringAttribute': _d_string_attribute,
    'CSymbolPart': _d_part,
    'CSubShape': _d_sub_shape,
    'CShape': _d_shape,
    'CSymbol': _d_symbol,
    'CSymbolPin': _d_symbol_pin,
    'CSymbolVariant': _d_symbol_variant,
    'CSymbolPartVariant': _d_part_variant,
    'CSheet': _d_sheet,
    'CLabelInfo': _d_label_info,
    'CWire': _d_wire,
    'CBus': _d_bus,
    'CObArray': _d_ob_array,
    'CUcPolyline': _d_uc_polyline,
    'CUcArc': _d_uc_arc,
    'CUcLine': _d_uc_line,
    'CUcRect': _d_uc_rect,
    'CUcEllipse': _d_uc_ellipse,
    'CUcPolygon': _d_uc_polygon,
    'CUcPie': _d_uc_pie,
}


# ── driver ───────────────────────────────────────────────────────────────

def _seed(r: _Reader, pro: Prologue) -> None:
    """Replay the archive numbering of the prologue: the variants, then the font entries."""
    def new_class(name: str) -> None:
        if name not in r.classes.values():
            r.classes[r.next_index] = name
            r.next_index += 1

    for v in pro.variants:
        new_class('CDocVariant')
        # Symbol variants refer to these objects by archive number only, so the
        # variant's own data is put on the node: which document variant a
        # symbol variant belongs to, and which one Ulticap shows, is read from here.
        node = Node('CDocVariant', r.next_index, 0)
        node['name'] = v.name
        node['visible'] = v.visible
        node['index'] = v.index
        r.objs[r.next_index] = node
        r.next_index += 1
    for entry in pro.fonts:
        new_class('CAttribFontInfo')
        info = Node('CAttribFontInfo', r.next_index, 0)
        r.objs[r.next_index] = info
        r.next_index += 1
        new_class('CSerialFont')
        face = Node('CSerialFont', r.next_index, 0)
        face['font'] = entry.font
        r.objs[r.next_index] = face
        r.next_index += 1
        info['font_object'] = face
        info['usage'] = entry.usage
        info['index'] = entry.index
    if r.next_index != pro.next_index:
        raise MalformedStreamError("archive numbering after the prologue does not match")


def _read_pointer(r: _Reader, stack: list) -> Tuple[Optional[Node], bool]:
    """Read one pointer. Returns (node, started): started is True when a new object begins
    and its decoder has been pushed on the stack."""
    at = r.pos
    kind, index = r.tag()
    if kind == TAG_NULL:
        return None, False
    if kind == TAG_NEW_CLASS:
        name = r._register()
    elif kind == TAG_CLASS:
        found = r.classes.get(index)
        if found is None:
            raise MalformedStreamError(f"pointer at offset {at:#x} names a class never registered")
        name = found
    else:
        node = r.objs.get(index)
        if node is None:
            raise MalformedStreamError(f"back-reference at offset {at:#x} to object {index}, which does not exist")
        return node, False
    decoder = _DECODERS.get(name)
    if decoder is None:
        raise UnknownClassError(name, at)
    node = Node(name, r.next_index, at)
    r.objs[r.next_index] = node
    r.next_index += 1
    r.counts[name] = r.counts.get(name, 0) + 1
    produced = decoder(r, node)
    if isinstance(produced, GeneratorType):
        stack.append((produced, node))
        return node, True
    return node, False


def _drive(r: _Reader, top) -> Any:
    """Run `top` to completion. Each new object's decoder is pushed on an explicit stack, so the
    depth of the object graph never becomes Python recursion."""
    stack: List[Tuple[Any, Optional[Node]]] = [(top, None)]
    send: Any = None
    while stack:
        gen, node = stack[-1]
        try:
            request = gen.send(send)
        except StopIteration as done:
            stack.pop()
            if not stack:
                return done.value
            send = node                          # the finished object goes back to its parent
            continue
        if request is not _PTR:
            raise MalformedStreamError("internal error: a decoder asked for something other than a pointer")
        target, started = _read_pointer(r, stack)
        send = None if started else target
    return None


def _document(r: _Reader, doc: Document):
    for _ in range(r.count(2)):
        doc.nets.append((yield _PTR))            # the first net pulls in the graph
    doc.library_map = yield from _mapstrob(r)
    doc.designator_map = yield from _mapstrob(r, doc.designator_key_spans)
    doc.sheets = yield from _obarray(r)
    for _ in range(r.u32()):                     # the label index: a dword count, then pointers
        if r.end - r.pos < 2:
            raise MalformedStreamError(f"stream ends inside the label index (offset {r.pos:#x})")
        doc.label_index.append((yield _PTR))
    doc.net_name_map = yield from _mapstrob(r)
    for _ in range(r.count(2)):                  # the OLE part: a count, then pointers
        doc.ole_items.append((yield _PTR))
    doc.end_word = r.u16()


def read_document(stream: bytes) -> Document:
    """Read the whole body of the stream into a tree.

    Only the verified layout (see kiuc_binary.DocumentHeader.is_verified_layout)
    is handled. Raises UtschError -- UnsupportedSchemaError, MalformedStreamError
    or its subclass UnknownClassError -- if the stream does not follow the
    layouts implemented here. It does not require the walk to end on the last
    byte; check_walk() does.
    """
    pro = read_prologue(stream)
    r = _Reader(stream, pro.net_count_offset)
    _seed(r, pro)
    doc = Document(prologue=pro)
    _drive(r, _document(r, doc))
    doc.objects = len(r.objs) - len(pro.variants) - 2 * len(pro.fonts)
    doc.end_offset = r.pos
    doc.class_counts = dict(r.counts)
    doc.designator_spans = r.designator_spans
    return doc


def check_walk(stream: bytes) -> WalkCheck:
    """Walk the stream and report whether the walk ends exactly on its last byte. Never raises."""
    length = len(stream)
    try:
        doc = read_document(stream)
    except UnsupportedSchemaError as exc:
        return WalkCheck(WALK_NA, detail=str(exc), stream_length=length)
    except UnknownClassError as exc:
        return WalkCheck(WALK_ERROR, detail=str(exc), stream_length=length, unknown_class=exc.class_name)
    except UtschError as exc:
        return WalkCheck(WALK_ERROR, detail=str(exc), stream_length=length)
    except (MemoryError, RecursionError) as exc:                       # never expected; reported, not raised
        return WalkCheck(WALK_ERROR, detail=f"{type(exc).__name__} while walking", stream_length=length)
    counts = tuple(sorted(doc.class_counts.items(), key=lambda kv: (-kv[1], kv[0])))
    if doc.end_offset == length:
        return WalkCheck(WALK_OK, objects=doc.objects, end_offset=doc.end_offset,
                         stream_length=length, class_counts=counts)
    return WalkCheck(WALK_MISMATCH, detail=f"the walk ends {length - doc.end_offset} byte(s) before the end of the stream",
                     objects=doc.objects, end_offset=doc.end_offset, stream_length=length, class_counts=counts)
