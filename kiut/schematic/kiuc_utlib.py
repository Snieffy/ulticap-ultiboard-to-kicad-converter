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
kiuc_utlib.py -- Reader for Ulticap V5.x symbol libraries (.UTLIB)

A .UTLIB is a flat file (not a compound document like a .UTSCH): a header
with a directory of five sections -- the symbol index, the font list, a
string pool, a class table and the symbol bodies (FILEFORMAT_ULTICAP_UTLIB.md). Every
string is an index into the pool.

A library symbol holds the same data as the shape Ulticap makes of it when a
design uses it, so each symbol is rebuilt here as that shape, in the node
form kiuc_binary_walk gives a design's shapes, and converted by the .UTSCH
route's own code (kiuc_binary_semantic.shape_symbols). A library therefore
converts exactly as the same symbols in a design do. Where a design's
shape differs from the library, the library is followed:

  * Texts keep their case (a design's texts are upper case).
  * The device attribute's value is the device name the symbol shows
    (PART), not its keyword (PART\\SO), which names the symbol.
  * The '-' placeholders a design gives every shape (device, package,
    value) are not added: the library has none.

Two repairs for symbols whose texts were attached to the wrong objects:
a pin's second and further pin names are plain texts at their own
positions, the first one names the pin; a net name on the gate drawing
itself, off its pins, is a plain text too.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from kiut.schematic import kiuc_binary_semantic as bs
from kiut.schematic.kiuc_model import Library
from kiut.schematic.kiuc_binary import UtschError, _parse_logfont
from kiut.schematic.kiuc_binary_walk import Node, _Reader

SIGNATURE = b'ULTImate Library'

# Library class -> the class of the same object in a design's shape.
_GRAPHIC_CLASS = {'CPolyLine': 'CUcPolyline', 'CLine': 'CUcLine', 'CArc': 'CUcArc',
                  'CRectangle': 'CUcRect', 'CEllipse': 'CUcEllipse', 'CPolygon': 'CUcPolygon',
                  'CPie': 'CUcPie'}

# Attribute kinds: an ordinary attribute (or a keyless graphic text), the
# device (keyword = the symbol's name, text = the device it shows), the
# designator (keyword = its template, U?).
_KIND_PLAIN, _KIND_DEVICE, _KIND_DESIGNATOR = 0, 4, 5


class UtlibError(UtschError):
    """A file that is not a .UTLIB, or one laid out other than expected."""


def looks_like_utlib(path) -> bool:
    """Whether the file starts with a .UTLIB's signature."""
    try:
        with open(path, 'rb') as f:
            return f.read(len(SIGNATURE)) == SIGNATURE
    except OSError:
        return False


# ── the file ──────────────────────────────────────────────────────────────────

class _File:
    """Header, directory, fonts, string pool and class table of a .UTLIB."""

    def __init__(self, data: bytes):
        if data[:len(SIGNATURE)] != SIGNATURE:
            raise UtlibError('not a .UTLIB file (no "ULTImate Library" signature)')
        self.data = data
        r = _Reader(data, len(SIGNATURE))
        r.u16()                             # schema 100
        r.u8()
        r.u32()                             # save time
        sections = [(r.u32(), r.u32()) for _ in range(5)]
        (index_at, _), (pool_at, _), (fonts_at, _), (bodies_at, _), (classes_at, _) = sections
        for at, _n in sections:
            if not 0 < at < len(data):
                raise UtlibError(f'section offset {at:#x} outside the file')

        r.pos = pool_at
        self.pool = [r.cstring() for _ in range(r.u32())]

        r.pos = fonts_at
        self.fonts = []
        for _ in range(r.u32()):
            r._need(60)
            font = Node('CSerialFont', 0, r.pos)
            font['font'] = _parse_logfont(data[r.pos:r.pos + 60])
            self.fonts.append(font)
            r.pos += 60

        r.pos = classes_at
        self.classes = [None]
        for _ in range(r.u32()):
            if r.u16() != 0xFFFF:
                raise UtlibError(f'class table entry without its marker at {r.pos - 2:#x}')
            r.u16()                         # schema
            n = r.u16()
            r._need(n)
            self.classes.append(data[r.pos:r.pos + n].decode('ascii'))
            r.pos += n

        r.pos = index_at
        self.index = []                     # (gate drawing names, body offset)
        for _ in range(r.u32()):
            names = [r.cstring() for _ in range(r.u32())]
            for _ in range(r.u32()):        # package types: the attribute decides
                r.cstring()
            r.cstring()                     # designator template
            r.cstring()                     # device
            self.index.append((names, r.u32()))
        self.bodies_at = bodies_at

    def string(self, i: int) -> str:
        if i >= len(self.pool):
            raise UtlibError(f'string index {i} beyond the pool ({len(self.pool)} strings)')
        return self.pool[i]


class _Body:
    """Reads one symbol body: gate drawings and their objects, raw."""

    def __init__(self, lib: _File, at: int):
        self.lib = lib
        self.r = _Reader(lib.data, at)

    def tag(self) -> str:
        at = self.r.pos
        t = self.r.u16()
        if not t & 0x8000 or not 0 < (t & 0x7FFF) < len(self.lib.classes):
            raise UtlibError(f'no class tag at {at:#x}')
        return self.lib.classes[t & 0x7FFF]

    def marker(self, name: str) -> None:
        at = self.r.pos
        if self.tag() != name:
            raise UtlibError(f'expected {name} at {at:#x}')

    def record(self) -> dict:
        """The drawing record: pen (colour, width, style) and points."""
        self.marker('CDrawObject')
        r = self.r
        pen = (r.u32(), r.u32(), r.u32())
        points = r.points()
        self.marker('CUndoableObject')
        return {'pen': pen, 'points': points}

    def text(self) -> dict:
        self.marker('CText')
        r = self.r
        t = {'visible': r.u32(), 'anchor': r.u8(), 'orientation': r.u32(),
             'font': r.u32(), 'string': self.lib.string(r.u32())}
        for _ in range(r.count(4)):         # overline positions: none seen
            r.u32()
        t.update(self.record())
        return t

    def obj(self) -> dict:
        at = self.r.pos
        cls = self.tag()
        o = {'cls': cls, 'at': at}
        r = self.r
        if cls == 'CBBoxNC':
            self.marker('CBBox')
            o.update(self.record())
        elif cls in ('CBBox', 'CArc'):
            o.update(self.record())
        elif cls in ('CPolyLine', 'CLine'):
            if cls == 'CLine':              # a single segment: a polyline of two points
                self.marker('CPolyLine')
            self.marker('CMultiPointObject')
            o.update(self.record())
        elif cls in ('CRectangle', 'CEllipse', 'CPolygon', 'CPie'):
            # (filled, fill colour), then the class the shape is drawn as:
            # an ellipse by its bounding rectangle, a pie by its arc.
            o['fill'] = (r.u32(), r.u32())
            self.marker({'CPolygon': 'CMultiPointObject', 'CPie': 'CArc'}.get(cls, 'CTwoPointObject'))
            o.update(self.record())
        elif cls == 'CLibText':
            o['text'] = self.text()
        elif cls == 'CLibAttribute':
            self.marker('CAttribute')
            o['shown'] = r.u32()
            o['kind'] = r.u32()
            o['key'] = self.lib.string(r.u32())
            o['text'] = self.text()
        elif cls == 'CLibPin':
            self.marker('CPin')
            o['internal_connect'], o['swap'], o['orientation'], o['pin_type'], o['style'] = (
                r.u8() for _ in range(5))
            o.update(self.record())
            o['numbers'] = [self.lib.string(r.u32()) for _ in range(r.u32())]
            o['label'] = self.obj()
            o['attrs'] = [self.obj() for _ in range(r.u16())]
        else:
            # Nothing gives an object's length, so an unknown class cannot be
            # skipped: the symbol cannot be read past it.
            raise UtlibError(f'{cls} objects are not supported yet (at {at:#x})')
        return o

    def symbol(self) -> List[dict]:
        r = self.r
        r.u8()
        r.u8()
        in_netlist = r.u32()
        gates = []
        for _ in range(r.u32()):
            g = {'name': self.lib.string(r.u32()), 'designator_text': self.lib.string(r.u32()),
                 'gate_count': r.u32(), 'external_gate_swap': r.u32(), 'in_netlist': in_netlist}
            items = [self.obj() for _ in range(r.u32())]
            if not items or items[0]['cls'] not in ('CBBoxNC', 'CBBox'):
                raise UtlibError(f'gate drawing {g["name"]!r} does not start with its box')
            g['box'], g['objects'] = items[0], items[1:]
            gates.append(g)
        return gates


# ── a library symbol as a design's shape ──────────────────────────────────────

def _node(cls: str, **fields) -> Node:
    n = Node(cls, 0, 0)
    n.update(fields)
    return n


class _ShapeBuilder:
    def __init__(self, lib: _File, name: str, warnings: List[str]):
        self.lib = lib
        self.name = name
        self.warnings = warnings

    def text(self, t: dict, string: Optional[str] = None, visible: Optional[int] = None) -> dict:
        """A library text as a design text: position = its point, vertical =
        orientation 90, colour = its pen."""
        font = t['font']
        if font >= len(self.lib.fonts):
            raise UtlibError(f'{self.name}: font index {font} beyond the font list')
        x, y = t['points'][0] if t['points'] else (0, 0)
        return {'x': x, 'y': y, 'anchor': t['anchor'],
                'visible': t['visible'] if visible is None else visible,
                'colour': t['pen'][0], 'vertical': 1 if t['orientation'] == 90 else 0,
                'font': self.lib.fonts[font],
                'string': t['string'] if string is None else string}

    def attribute(self, o: dict, key: Optional[str] = None) -> Node:
        # Keys are upper case in a design; one library stores a key in mixed case.
        return _node('CUcAttribute', key=(o['key'] if key is None else key).upper(),
                     text=self.text(o['text']))

    def pin(self, o: dict, gate_name: str, extra_texts: List[Node]) -> Node:
        label = o['label']['text']
        attrs = []
        named = False
        for a in o['attrs']:
            if a['cls'] != 'CLibAttribute':
                raise UtlibError(f'{self.name}: a pin carries a {a["cls"]}')
            if a['key'].upper() == 'UT_PINNAME':
                if named:
                    # A second pin name is a text attached to the wrong
                    # object: a plain text at its own position.
                    extra_texts.append(self.attribute(a, key=''))
                    self.warnings.append(
                        f'{gate_name}: pin {"/".join(o["numbers"])} has a second pin name '
                        f'{a["text"]["string"]!r}; written as a plain text')
                    continue
                named = True
            attrs.append(self.attribute(a))
        x, y = o['points'][0]
        return _node('CShapePin', x=x, y=y, internal_connect=o['internal_connect'],
                     colour=o['pen'][0], style=o['style'], orientation=o['orientation'],
                     pin_type=o['pin_type'], swap=o['swap'],
                     number_labels=[_node('CUcText', text=self.text(label, string=n))
                                    for n in o['numbers']],
                     attributes=attrs)

    def gate_drawing(self, g: dict) -> Tuple[Node, Optional[str]]:
        """(CSubShape, designator template)."""
        (x0, y0), (x1, y1) = g['box']['points'][:2]
        graphics, pins, attributes, extra = [], [], [], []
        designator = designator_text = None
        for o in g['objects']:
            cls = o['cls']
            if cls in _GRAPHIC_CLASS:
                drawing = {'pen': o['pen'], 'points': o['points']}
                fields = {}
                # Where a design's walker keeps the fill, the shape builder reads it.
                if cls in ('CRectangle', 'CEllipse'):
                    drawing['fill'] = o['fill']
                elif cls in ('CPolygon', 'CPie'):
                    fields['fill'] = o['fill']
                graphics.append(_node(_GRAPHIC_CLASS[cls], drawing=drawing, **fields))
            elif cls == 'CLibPin':
                pins.append(self.pin(o, g['name'], extra))
            elif cls == 'CLibText':
                attributes.append(_node('CUcText', text=self.text(o['text'])))
            elif cls == 'CLibAttribute' and o['kind'] == _KIND_DESIGNATOR:
                designator = o['key']
                designator_text = self.text(o['text'], string=g['designator_text'],
                                            visible=o['shown'])
            elif cls == 'CLibAttribute' and o['kind'] == _KIND_DEVICE:
                attributes.append(_node('CUcAttribute', key='UT_DEVICE',
                                        text=self.text(o['text'], visible=o['shown'])))
            elif cls == 'CLibAttribute':
                key = o['key'].upper()
                if key == 'UT_NETNAME':
                    # A net name belongs on a pin; on the gate drawing it is
                    # a label drawn in the body.
                    key = ''
                    self.warnings.append(f'{g["name"]}: net name {o["text"]["string"]!r} on the '
                                         f'symbol itself; written as a plain text')
                attributes.append(self.attribute(o, key=key))
            else:
                raise UtlibError(f'{g["name"]}: unexpected {cls} in a gate drawing')
        sub = _node('CSubShape', name=g['name'], graphics=graphics, pins=pins,
                    attributes=attributes + extra, gate_count=g['gate_count'],
                    external_gate_swap=g['external_gate_swap'], text=designator_text,
                    frame={'points': [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]})
        return sub, designator

    def shape(self, gates: List[dict]) -> Node:
        """The CShape: its gate drawings, its designator template as its text."""
        subs, designator = [], None
        for g in gates:
            sub, t = self.gate_drawing(g)
            subs.append(sub)
            designator = designator if designator is not None else t
        hidden = {'x': 0, 'y': 0, 'anchor': 6, 'visible': 0, 'colour': 0, 'vertical': 0,
                  'font': self.lib.fonts[0] if self.lib.fonts else None, 'string': designator or ''}
        return _node('CShape', name=self.name, gate_drawings=subs, attributes=[],
                     in_netlist=gates[0]['in_netlist'] if gates else 1,
                     text=hidden if designator is not None else None)


def _device_keyword(gates: List[dict]) -> Optional[str]:
    for g in gates:
        for o in g['objects']:
            if o['cls'] == 'CLibAttribute' and o['kind'] == _KIND_DEVICE:
                return o['key']
    return None


def read_library(path) -> Library:
    """Read a .UTLIB into symbols named after their keywords (the device
    keyword, as Ulticap names the shape it makes of a library symbol). A
    second symbol with a keyword already taken gets '_2' (then '_3', ...)
    and a warning; the index name stands in for a symbol without one."""
    path = Path(path)
    lib = _File(path.read_bytes())
    out = Library(path=path, count=len(lib.index))
    taken: set = set()
    # Each body ends where the next begins (the last at the end of the file):
    # a layout read wrong shows as a symbol ending elsewhere.
    starts = sorted(at for _, at in lib.index) + [len(lib.data)]
    for names, at in lib.index:
        try:
            body = _Body(lib, at)
            gates = body.symbol()
            end = starts[starts.index(at) + 1]
            if body.r.pos != end:
                raise UtlibError(f'the symbol ends at {body.r.pos:#x}, the next one starts at {end:#x}')
        except (UtlibError, UtschError) as e:
            raise UtlibError(f'symbol {names[0] if names else "?"!r}: {e}') from e
        keyword = _device_keyword(gates) or (names[0] if names else f'symbol@{at:#x}')
        name, n = keyword, 2
        while name in taken:
            name, n = f'{keyword}_{n}', n + 1
        if name != keyword:
            out.warnings.append(f'{keyword}: a second symbol of this name, written as {name}')
        taken.add(name)
        shape = _ShapeBuilder(lib, name, out.warnings).shape(gates)
        for sym_name, sym in bs.shape_symbols(name, shape).items():
            out.symbols[sym_name] = sym
    return out


__all__ = ['SIGNATURE', 'Library', 'UtlibError', 'looks_like_utlib', 'read_library']
