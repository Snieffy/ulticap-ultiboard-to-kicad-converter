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
kiuc_binary_semantic.py -- Turn a .UTSCH object graph into a Sheet

kiuc_binary_walk.py reads the object graph of a stream. This module gives
that graph meaning: it walks the graph the way a schematic actually holds
together (nets to wires and pins, symbols to their units and attributes)
and builds the same Sheet/Symbol/Component/Wire/Junction objects that
kiuc_ascii.load_sheet() builds from a .SCH file, so the rest of the
converter (kiuc_writer.py and onward) does not need to know which source
format a design came from.

Scope of this reader: a .UTSCH holds the whole design, every sheet of a
hierarchy included, so nothing is read from other files. read_schematic and
read_hierarchy build every sheet; read_sheet and read_root_sheet build one.

Field-by-field correspondence with the ASCII format, established by
comparing this reader's output against kiuc_ascii.load_sheet() on the same
design saved in both formats:

  - A placed component (kiuc_model.Component) is one symbol *unit*: a
    CSymbolPart, not a CSymbol. A single-unit symbol yields one Component;
    a six-unit symbol yields six, matching how the ASCII format writes one
    *C record per unit. Its reference designator is the owning symbol's own
    text plus the unit's own text (e.g. "U5" + "A" = "U5A").
  - A unit's attributes (device, value, package, free text) are not on the
    CSymbolPart itself -- that array is empty on every placed component in
    the files examined, and holds only UT_SHEETNAME on a subsheet box.
    They are on the part's own CSymbolPartVariants (its `variants` list),
    one per symbol variant; _part_variant_for picks the one of the
    document variant Ulticap shows.
  - The attribute keys UT_DEVICE, UT_VALUE and UT_PKGTYPE hold exactly what
    the ASCII format's DEVICE, VALUE and PKG_TYPE tags hold, including
    position: absolute (dx, dy) in the binary equals the placement plus the
    ASCII record's own (dx, dy) for an unrotated placement, and the general
    rule (confirmed on a six-unit symbol placed at different points) is
    that the binary's attribute position is the placement plus that
    attribute's own local offset rotated by the unit's own orientation --
    the same rot_transform() this project already uses for the ASCII side.
    A blank key holds free text (the ASCII format's TXT tag).
  - A pin's number is its own text, on the shape's pin object, shared by
    every placement of that shape -- matching the ASCII format, where pin
    number/name/type live on the *S definition, not the *C placement.
  - A pin's electrical type (the ASCII format's PINTYPE tag) is not stored
    as a named attribute; it is a small field on the shape's pin object,
    the index in the symbol editor's pin-type list: 0 input, 1 output,
    2 open collector, 3 open emitter, 4 bidirectional, 5 tri-state,
    6 passive, 7 power, 8 not connected (_PIN_TYPE_BY_CODE).
  - A pin's name (the ASCII format's LABEL tag, or WIRELABEL when LABEL is
    absent) is the UT_PINNAME attribute on the shape's pin object, again
    shared by every placement. A pin implicitly tied to a net without a
    visible wire (the ASCII format's SIGNAL tag, and WIRELABEL on power
    pins) is UT_NETNAME on the pin instead.
  - A wire or bus's two points are the ends of its drawing record, in sheet
    coordinates; a bus differs from a wire only in class. Some connections
    (seen holding a bus, or a lone unconnected pin) belong to no net at
    all, so this reader reads every wire and bus through the sheet's own
    connection list, not through the document's nets.
  - A junction dot (the ASCII format's *V record) is not a stored object,
    and not simply wherever two or more wires or buses meet -- most such
    points are a corner or a plain two-way join, not a dot. A connection
    is a dot when a wire meets a bus there (a bus entry is always marked),
    when any wire or bus passes through the point without ending there,
    or when three or more wires, buses and pins end there; two things that
    only end at the same point make no dot (_is_junction;
    FILEFORMAT_ULTICAP_UTSCH.md §10.3).

A sheet's free graphics (the ASCII format's UserLine), standalone free-text
labels (Label) and wire/net labels (Annotation) are read too, from where
they turned out to actually live: a sheet's own CSheetLine/CSheetArc/
CSheetRect and standalone text objects are not reachable from the sheet's
connection list at all -- they are in the live object list of the sheet's
undo block (holding the sheet's current contents, whether or not the file
keeps an undo history in the same block); a wire's or bus's implicit net label is an attribute on the wire
or bus object itself, using the same key a pin's own implicit net name
uses (UT_NETNAME).

A symbol's outline circles, arcs and pies (Symbol.circles), and its filled
rectangles and polygons (Symbol.polylines), are read too -- see
_read_circle's, _read_ellipse's and the drawing loop's own comments for how
each shape's own points give its geometry, for a known, already-corrected-
for ASCII-side export bug a handful of plain arcs legitimately disagree
with their own file's ASCII text over, and for why a pie's own ASCII text
cannot be used to check its geometry at all (the .SCH format has no way to
represent a pie -- confirmed directly: Ulticap's own export of one draws a
plain arc, in the wrong place, at the wrong size). Fill itself -- an option
of the symbol editor with no .SCH representation whatsoever, confirmed
against a file with two otherwise byte-for-byte identical symbols, one
filled and one not -- is new to kiuc_model.py (Polyline.filled/fill_colour,
Circle.filled/fill_colour/is_pie), added so that nothing built from a .SCH
file is ever affected by it. A sheet's own lines and rectangles become
UserLines (a rectangle as its four sides) and its arcs UserArcs, a
.UTSCH-only dataclass -- see _read_user_lines and _read_user_arc.

A file's own stored coordinates are always taken as-is, unconditionally --
this is correct, not a shortcut: a small number of designs, all traced to
an SCH-to-UTSCH import at some point in their history, have their own
component coordinates shifted from where the original ASCII file they came
from once placed them (Ulticap re-centres a sheet's own stored frame on
import, and shifts every component to compensate -- the shift's own exact
formula is understood, in terms of that original ASCII file's own header,
but is not recoverable from a .UTSCH file's bytes alone, since the
information it depends on is exactly what import discards). Ulticap itself
never revisits this once a design has been imported and saved -- reopening
the resulting .UTSCH file anywhere thereafter uses its own stored
coordinates directly, the same ones this reader reads, with no correction
applied and none expected. The only place this shift is visible at all is
in comparing a design against an independent, pre-import copy of its own
ASCII source -- not a concern for reading a .UTSCH file on its own terms,
which is what every function in this module does.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kiut.schematic import kiuc_binary as kb
from kiut.schematic import kiuc_binary_walk as kw
from kiut.schematic.kiuc_binary_walk import Node, UtschError

from kiut.schematic import kiuc_model as km


class UnsupportedDesignError(UtschError):
    """The design uses something this reader does not yet handle."""


# The electrical type stored in a shape pin's own third byte field: the index of
# the type in the symbol editor's pin-type list (In, Out, Open collector, Open
# emitter, Bi-directional, Tri-state, Passive, Power, Not connected). 0-2 and
# 4-7 were established against the ASCII PINTYPE of 674 pins in five designs;
# 3 and 8 by a test symbol with one pin of every type (FILEFORMAT_ULTICAP_UTSCH.md §5.3). A code
# outside the list reads as passive, the neutral type.
_PIN_TYPE_BY_CODE = {
    0: 'IN', 1: 'OUT', 2: 'OC', 3: 'OE', 4: 'BI', 5: 'TRI', 6: 'PAS', 7: 'PWR', 8: 'NC',
}

# A shape pin's decoration (the ASCII pin_format), from the flag bits of its
# style and, for the IEC forms, its pin_type -- FILEFORMAT_ULTICAP_UTSCH.md §5.3.
# Verified: 4 inverted (bubble) -> 1, 2 clock -> 2, 1 IEC -> 3 on an
# input / 4 on an output, 16 bus -> 9, against the V5.72 export and the
# pre-V5 original of the two-sheet test design (which agree with the binary wherever the
# export itself drops a clock or an input/output bubble). 8 marks the
# hidden power pins, which get no decoration. Combined forms follow the
# ASCII code table by name, with no example yet: IEC inverted in/out 5/6,
# IEC inverted clock 7, IEC bidirectional 8, inverted clock 10.
_PIN_FLAG_IEC, _PIN_FLAG_CLOCK, _PIN_FLAG_INVERTED, _PIN_FLAG_BUS = 1, 2, 4, 16
# The output types (pin_type), which take the IEC output form; open emitter is an
# output like open collector.
_OUTPUT_PIN_TYPES = (1, 2, 3, 5)  # OUT, OC, OE, TRI


def _pin_format(flags: int, pin_type: int) -> int:
    inverted = bool(flags & _PIN_FLAG_INVERTED)
    if flags & _PIN_FLAG_BUS:
        return 9
    if flags & _PIN_FLAG_IEC:
        if flags & _PIN_FLAG_CLOCK:
            return 7
        if pin_type == 4:                       # BI
            return 8
        if pin_type in _OUTPUT_PIN_TYPES:
            return 6 if inverted else 4
        return 5 if inverted else 3
    if flags & _PIN_FLAG_CLOCK:
        return 10 if inverted else 2
    return 1 if inverted else 0


# Placeholder values Ulticap writes for an attribute with nothing
# meaningful in it -- the same set kiuc_ascii.py normalizes to "absent".
_PLACEHOLDERS = frozenset(('-', '?', '*', '--', 'N/A', 'n/a', ''))


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return None if value in _PLACEHOLDERS else value


def _pin_tip(pin: Node, width: int, height: int):
    """A pin's wire-connection point in its shape's own local coordinates: its
    own (x, y) gives the perpendicular coordinate; the coordinate along the
    pin's own facing direction is the shape's bounding-box edge, not the
    pin's stored (x, y) -- that stored value is the stub's root, where it
    meets the symbol body, not its outer tip."""
    facing = pin['orientation'] & 3
    if facing == 0:
        return width, pin['y']
    if facing == 1:
        return pin['x'], height
    if facing == 2:
        return 0, pin['y']
    return pin['x'], 0


# ── text presentation ───────────────────────────────────────────────────────
#
# How a stored text object (CUcText -- the 'text' of an attribute, a free text,
# a wire label, a unit's reference designator or a pin's number label) maps
# onto the ASCII format's presentation fields. Every rule below was verified
# against Ulticap V5.72's own ASCII export of the same file, attribute by
# attribute: 1,652/1,652 on the two-sheet test design and 1,152/1,152 on
# the rotation test design (one symbol in all 144 rotation/mirror
# combinations).
#
#   visible  shown (1), hidden (0), shown with its key (2) -> visibility bits 128 (and 64)
#   anchor   3x3 grid, rows counted from the TOP (0-2 = top row,
#            6-8 = bottom row) -- the ASCII grid counts from the bottom
#   vertical text is vertical AS DISPLAYED on the sheet
#   font     LOGFONT height; ASCII size = height * 2 // 3
#   colour   Windows COLORREF (0x00BBGGRR) -- kept as full RGB (RgbColour)
#
# The ASCII anchor is local to the component: its vertical flag is the
# displayed one XOR an odd rotation code, and its grid position is the
# displayed one passed back through the component's rotation/mirror
# transform (ulticap_translate, which is its own inverse). Visibility bit 32
# has no fixed meaning found yet but is fully determined by the rotation
# code and the vertical flag (all 16 combinations observed); the writer does not use
# it, it is set only so the model matches the ASCII one exactly.

_BIT32 = {  # (rotation code, vertical) -> visibility bit 32 set
    (0, 0): False, (0, 1): False, (1, 0): True,  (1, 1): False,
    (2, 0): True,  (2, 1): True,  (3, 0): False, (3, 1): True,
    (4, 0): True,  (4, 1): False, (5, 0): True,  (5, 1): False,
    (6, 0): False, (6, 1): True,  (7, 0): False, (7, 1): True,
}


def _text_size(text) -> int:
    """ASCII size from the font's LOGFONT height: |height| * 2 // 3. Windows
    stores a NEGATIVE height to mean character height rather than cell
    height; the export treats both alike (six-sheet test design: -35 -> 23,
    -25 -> 16, -40 -> 26, all 884 such texts); the signed value would give
    negative sizes."""
    return abs(text['font']['font'].height) * 2 // 3


def _text_colour(text) -> km.RgbColour:
    return km.RgbColour.from_colorref(text['colour'])


def _display_anchor(text) -> int:
    """The stored anchor as an ASCII grid position (0-8, rows from the
    bottom), still in the sheet's own frame."""
    row, col = divmod(text['anchor'], 3)
    return (2 - row) * 3 + col


def _text_visibility(text, rotation: int = 0) -> int:
    """The ASCII visibility byte for a text belonging to a component placed
    with this rotation code (0 for a free text or wire label).

    The stored visibility is 0 hidden, 1 shown, 2 shown with its key
    (KEY=value): 2 becomes bits 128 and 64 (tag shown), which is how the
    ASCII source of every such text had it (FILEFORMAT_ULTICAP_UTSCH.md §4.2), so a design
    converts the same from its .SCH and from its .UTSCH. Only attribute texts
    carry 2; on a *C line bit 64 would mean something else."""
    r = rotation & 7
    dc = 1 if text['vertical'] else 0
    vertical = bool(dc) ^ bool(r & 1)
    anchor = km.ulticap_translate(r, (16 if vertical else 0) + _display_anchor(text))
    return ((128 if text['visible'] else 0)
            | (64 if text['visible'] == 2 else 0)
            | (32 if _BIT32[(r, dc)] else 0)
            | anchor)


def _shape_size(sub_shape: Node):
    points = sub_shape['frame']['points']
    return max(p[0] for p in points), max(p[1] for p in points)


def _text_value(node: Optional[Node]) -> str:
    """A stored string, with the binary format's own overline markup (a
    literal backslash before each caret: '\\^TEXT\\^') converted the same
    way the ASCII side converts its own, bare '^TEXT^' form."""
    if node is None or node.cls not in ('CUcAttribute', 'CUcText'):
        return ''
    return km.convert_overline(node['text']['string'].replace('\\^', '^'))


def _is_attr(node: Optional[Node]) -> bool:
    """An entry of an attribute list: a CUcAttribute, or a CUcText -- a
    keyless text object that sits in the same lists (a shape's "&" gate
    label on a quad-gate IC; FILEFORMAT_ULTICAP_UTSCH.md §4.3) and that the export
    writes as TXT, exactly like an attribute with an empty key."""
    return node is not None and node.cls in ('CUcAttribute', 'CUcText')


def _attr_key(node: Node) -> str:
    """An attribute-list entry's key: a CUcText has none ('' -> TXT)."""
    return node['key'] if node.cls == 'CUcAttribute' else ''


def _attr_tag(node: Node) -> str:
    """An attribute-list entry's ASCII tag: its key, the device keys mapped
    (_ATTR_TAG; UT_DEVICE -> DEVICE, ..., a keyless text -> TXT)."""
    key = _attr_key(node)
    return _ATTR_TAG.get(key, key)


def _find_attribute(attributes, key: str) -> Optional[Node]:
    """The CUcAttribute with this key in an attribute list, or None."""
    return next((a for a in attributes or []
                 if a is not None and a.cls == 'CUcAttribute' and a['key'] == key), None)


def _gate_centre(shape: Optional[Node], gate_drawing: Optional[Node] = None) -> Tuple[int, int]:
    """The centre of a gate drawing's frame -- the point of effect of a
    symbol's component-level attributes. Without gate_drawing, the shape's
    first gate drawing; (0, 0) when there is none."""
    if gate_drawing is None and shape is not None:
        gate_drawing = next((x for x in shape['gate_drawings']
                             if x is not None and x.cls == 'CSubShape'), None)
    if gate_drawing is None:
        return (0, 0)
    width, height = _shape_size(gate_drawing)
    return (width // 2, height // 2)


_REFERENCE = re.compile(r'\\@([^@\\]*)\\@')


def _expand_references(value: str, attributes: Dict[str, str]) -> str:
    """Replace each attribute reference in a value -- \\@KEY\\@, e.g. a SPICE
    parameter stored as "V=\\@UT_VALUE\\@" -- with that attribute's value
    from the same set (a placement's own attributes, or the shape's for its
    template), as Ulticap itself shows it and its export writes it: a
    capacitor's "V=<its value>". Only UT_VALUE and UT_TOLERANCE occur (two
    designs); a reference to an attribute
    the set lacks is left as it is."""
    return _REFERENCE.sub(lambda m: attributes.get(m.group(1), m.group(0)), value)


def _pin_attributes(pin: Node) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for a in pin['attributes']:
        if a is not None and a.cls == 'CUcAttribute':
            out[a['key']] = _text_value(a)
    return out


# A sheet graphic's pen (FILEFORMAT_ULTICAP_UTSCH.md §4.4, §11): colour as full RGB, and
# the Windows pen style onto UserLine's own line-type codes -- which differ
# from a symbol outline's; all four styles verified on the
# line-and-shape test design (white and green lines alike). The export
# writes every pen width as thickness 0 (widths 4 and 10 seen), so the
# ASCII-side thickness stays 0 and the stored width travels separately as
# pen_width, which the writer draws. A CSheetLine is one UserLine,
# a CSheetRect four; a CSheetArc is a UserArc (_read_user_arc).
_USERLINE_STYLE_BY_PEN = {0: 0, 1: 256, 2: 768, 3: 512}   # -> UserLine.linetype


def _read_user_arc(item: Node, off) -> Optional[km.UserArc]:
    """A CSheetArc: stored as (start, end, centre), like a symbol arc
    (_read_circle), with the same pen. Kept as
    points; the writer draws it through them."""
    drawing = item['drawing']
    points = drawing['points']
    if len(points) != 3:
        return None
    (x1, y1), (x2, y2), (cx, cy) = points
    return km.UserArc(x1=x1 + off[0], y1=y1 + off[1], x2=x2 + off[0], y2=y2 + off[1],
                      cx=cx + off[0], cy=cy + off[1], colour=_pen_colour(drawing),
                      arc_linetype=_ARC_STYLE_BY_PEN.get(_pen_style(drawing), 0),
                      pen_width=drawing['pen'][1])


def _read_user_lines(item: Node, off) -> List[km.UserLine]:
    """A CSheetLine as one user line, a CSheetRect as four -- its sides, in
    stored corner order, closed; exactly how the V5.72 export writes a
    sheet rectangle. A CSheetArc has no user-line
    form (the export drops it) -- see _read_user_arc."""
    drawing = item['drawing']
    points = [tuple(p) for p in drawing['points']]
    if item.cls == 'CSheetLine' and len(points) == 2:
        segments = [points]
    elif item.cls == 'CSheetRect' and len(points) == 4:
        segments = [(points[i], points[(i + 1) % 4]) for i in range(4)]
    else:
        return []
    linetype = _USERLINE_STYLE_BY_PEN.get(_pen_style(drawing), 0)
    colour = _pen_colour(drawing)
    return [km.UserLine(x1=a[0] + off[0], y1=a[1] + off[1], x2=b[0] + off[0], y2=b[1] + off[1],
                        linetype=linetype, colour=colour,
                        pen_width=drawing['pen'][1])
            for a, b in segments]


def _is_free_text(item: Node) -> bool:
    """A sheet-level free text, in any of its three stored forms (FILEFORMAT_ULTICAP_UTSCH.md
    §11): a CUcAttribute keyed TEXT; one keyed with a numbered
    variant of it (TEXT-10-1, TEXT-9-1-1, ... -- the six-sheet test design has 20,
    apparently from copied texts, which the export writes under that same
    key as its tag); and a CUcText, a plain text object with no key at all,
    written by the export as TXT (15 in the same design)."""
    if item.cls == 'CUcText':
        return True
    return item.cls == 'CUcAttribute' and (item['key'] == 'TEXT' or item['key'].startswith('TEXT-'))


def _read_label(node: Node, off) -> km.Label:
    text = node['text']
    font = text['font']['font']
    style = (2 if font.weight >= 700 else 0) + (4 if font.italic else 0)
    return km.Label(
        x=text['x'] + off[0], y=text['y'] + off[1], text=_text_value(node),
        size=_text_size(text), colour=_text_colour(text),
        rotation=5760 if text['vertical'] else 0,
        # A free text has no component rotation; its vertical flag is
        # carried by `rotation` above, so only the grid position goes here.
        style=style, align=_display_anchor(text),
    )


def _read_wire_annotations(item: Node, off) -> List[km.Annotation]:
    """A wire's or bus's attributes, as the ASCII format's *A records.

    Its own implicit net label (UT_NETNAME) becomes a LABEL annotation, the
    writer's net label. Its point of effect -- where the label attaches -- is
    the wire's FIRST stored point: all 314 wire labels of the two-sheet test
    design against its V5.72 export (FILEFORMAT_ULTICAP_UTSCH.md §10.3). The wire
    end nearer to the label's text would be wrong for 73 of them.

    Any other attribute (a design read from ASCII keeps e.g. WIDTH=30 on a
    wire) is plain text, tagged with its key as the .SCH route's *A parser
    tags it, so both routes write the same KiCad text: only UT_NETNAME names
    a net (FILEFORMAT_ULTICAP_UTSCH.md §9.7). The one key that cannot be passed on
    as its own tag is LABEL, the writer's tag for a net label: a user can key
    a wire attribute LABEL in V5.72, and Ulticap's netlist then names no net
    by it (see the README, "One export change does affect connectivity"), so
    it goes over as TXT, its key put into the text when the text is shown
    with its key."""
    out: List[km.Annotation] = []
    px, py = tuple(item['line']['points'][0])
    for a in item['attributes']:
        if a is None or a.cls != 'CUcAttribute':
            continue
        text = a['text']
        key, value = a['key'], _text_value(a)
        if key == 'UT_NETNAME':
            tag = 'LABEL'
        elif key.upper() == 'LABEL':
            tag = 'TXT'
            if text['visible'] == 2:
                value = f'{key}={value}'
        else:
            tag = key or 'TXT'
        out.append(km.Annotation(
            x=text['x'] + off[0], y=text['y'] + off[1],
            x_poe=px + off[0], y_poe=py + off[1],
            text=value, size=_text_size(text), colour=_text_colour(text),
            visibility=_text_visibility(text), tag=tag,
        ))
    return out


def _read_circle(points, filled: bool = False, fill_colour=0,
                  is_pie: bool = False) -> Optional[km.Circle]:
    """A CUcArc's own three points are (start, end, centre) -- not the ASCII
    format's own (centre, radius, start angle, sweep). Recovered here as:
    radius from centre to either end; a full circle when start and end
    coincide (its own start angle is then unrecoverable, and does not
    matter -- a whole circle looks the same regardless); otherwise the
    start angle from the centre to the start point, and the sweep as the
    counter-clockwise angle from there to the end point.

    Verified exactly on 45 of 49 arcs across five designs (all of them on a
    file built to cover every quadrant combination on purpose); the other
    4, all from one shape, disagree with that shape's own ASCII export only
    in their stored sweep, by exactly its complement -- matching a known
    Ulticap export bug already documented and corrected for in this
    project's ASCII-side writer, not a discrepancy in this formula. Since
    that bug is specific to serializing to the ASCII format, and a file's
    own version header cannot be trusted for which files it affects, no
    correction is attempted here: this reader is not producing ASCII text,
    and has no reason to reproduce a bug that belongs to that format.

    A CUcPie shares this exact byte layout and the same three-point
    geometry (confirmed directly: a pie draws the same as an arc, with a
    straight line from each endpoint to the centre closing it into a
    wedge), so is_pie routes it through the same formula rather than a
    separate one. Its own file's ASCII export cannot be used to check this
    -- the .SCH format has no way to represent a pie at all, and Ulticap's
    own export of one draws a plain arc, in the wrong place, at the wrong
    size, confirmed directly against a real file.
    """
    if len(points) != 3:
        return None
    (x0, y0), (x1, y1), (cx, cy) = points
    r = round(math.hypot(x0 - cx, y0 - cy))
    if (x0, y0) == (x1, y1):
        return km.Circle(cx=cx, cy=cy, r=r, rotate=0, angle=23040,
                          filled=filled, fill_colour=fill_colour, is_pie=is_pie)
    start = math.degrees(math.atan2(y0 - cy, x0 - cx)) % 360
    end = math.degrees(math.atan2(y1 - cy, x1 - cx)) % 360
    sweep = (end - start) % 360
    return km.Circle(cx=cx, cy=cy, r=r, rotate=round(start * 64), angle=round(sweep * 64),
                      filled=filled, fill_colour=fill_colour, is_pie=is_pie)


# A drawing object's pen is (COLORREF, width, Windows pen style). Against
# the V5.72 export: the colour is the outline's
# colour (kept as full RGB, like every .UTSCH colour); the width is always
# written as the ASCII "thin" 6 (pen widths 4 and 1 alike -- 1 on the
# filled polygons V5.72 turns wide lines into); the style maps onto the
# ASCII line-type codes -- dash, dot and dash-dot all verified on polylines
# and lines (two test designs); arc styles by name only (no dashed arc
# in any file yet).
_POLYLINE_STYLE_BY_PEN = {0: 0, 1: 4096, 2: 12288, 3: 8192}   # -> Polyline.linetype
_ARC_STYLE_BY_PEN = {0: 0, 1: 1, 2: 3, 3: 2}                  # -> Circle.arc_linetype


def _pen_colour(drawing) -> km.RgbColour:
    return km.RgbColour.from_colorref(drawing['pen'][0])


def _pen_style(drawing) -> int:
    """The pen's line style (Windows PS_SOLID 0, PS_DASH 1, PS_DOT 2,
    PS_DASHDOT 3), without the flags Windows keeps in the same field's
    high bits -- PS_GEOMETRIC (0x10000) is set on many symbol pens."""
    return drawing['pen'][2] & 0xFF


def _closed(points) -> list:
    """A closed shape's corner list with its first point repeated at the end,
    as the ASCII format writes a closed outline -- a rectangle or polygon is
    stored as its corners only, and drawn as given it had one side missing."""
    pts = [tuple(p) for p in points]
    # A degenerate shape -- every corner the same point (a zero-size
    # rectangle in a connector symbol of the six-sheet test design) -- is
    # closed too:
    # its first and last points are equal only because all of them are.
    if len(pts) > 2 and (pts[0] != pts[-1] or len(set(pts)) == 1):
        pts.append(pts[0])
    return pts


def _read_ellipse(points, filled: bool, fill_colour) -> Optional[km.Circle]:
    """An ellipse's own drawing record holds its bounding box's four corners,
    not a centre/radius pair -- a different structure from an arc's own
    (start, end, centre) triple (_read_circle): an ellipse's record is laid
    out like a rectangle's (_rectangle in the walker). Radius is
    the average of the box's half-width and half-height -- exact for a true
    circle, and what the ASCII export writes (it represents an ellipse as a
    full circle; centre and radius matched on the one design available).
    The true radii are kept as rx/ry (half the box's width and height), so
    the writer can draw an elongated ellipse as one.
    """
    if len(points) != 4:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # A box with no width or no height draws nothing in Ulticap -- not even
    # in its own symbol editor -- so it is left out. Drawn as an ellipse it
    # would collapse into a visible straight line (FILEFORMAT_ULTICAP_UTSCH.md §4.5).
    if max(xs) == min(xs) or max(ys) == min(ys):
        return None
    # A centre on a half unit is rounded UP, as the export does (both such
    # ellipses of a transceiver symbol).
    cx = (min(xs) + max(xs) + 1) // 2
    cy = (min(ys) + max(ys) + 1) // 2
    r = round(((max(xs) - min(xs)) + (max(ys) - min(ys))) / 4)
    return km.Circle(cx=cx, cy=cy, r=r, rotate=0, angle=23040,
                      filled=filled, fill_colour=fill_colour,
                      rx=(max(xs) - min(xs)) / 2, ry=(max(ys) - min(ys)) / 2)


# A pin's own declared name: UT_PINNAME on an ordinary shape's pin,
# UT_SHEETNETNAME on a subsheet box's (FILEFORMAT_ULTICAP_UTSCH.md §5.3).
_PIN_NAME_KEYS = ('UT_PINNAME', 'UT_SHEETNETNAME')


def _pin_number(labels) -> str:
    """A shape pin's number as the ASCII format writes it: its per-gate
    number labels joined by commas ("1,4,9,12") -- or just one when every
    gate uses the same pin, as a shared power pin does (TL074CD's pin 4 is
    stored as "4" four times and exported as "4")."""
    numbers = [t['text']['string'] for t in labels if t is not None]
    if numbers and all(n == numbers[0] for n in numbers):
        return numbers[0]
    return ','.join(numbers)


def _is_hidden_pin(shape_pin: Node) -> bool:
    """Whether a shape pin is a hidden, name-connected pin (Symbol.signal_pins,
    the ASCII side's SIGNAL= pins) rather than an ordinary one: bit 8 of its
    style -- the same flag byte as its decoration (FILEFORMAT_ULTICAP_UTSCH.md §5.3) --
    is set exactly then, on all 960 shape pins of four test designs against
    their exports.
    _read_symbol builds the symbol with it, _unit_pin_attributes and
    _symbol_attributes skip the same pins, so an attribute never targets a
    pin the symbol doesn't have.

    Replaces an earlier inferred rule (power type, a net name, no
    declared name, other pins on the shape), which held on the two-sheet test
    design but not on the six-sheet one: the hidden GND pins of a header and
    of a pad symbol are not of power type, and the pad has no visible pin at
    all."""
    return bool(shape_pin['style'] & 8)


def _sub_shapes(shape: Optional[Node]) -> List[Node]:
    """A shape's gate drawings (CSubShape), in gate order. One for an
    ordinary symbol; one per gate type for a non-homogeneous part (FILEFORMAT_ULTICAP_UTSCH.md
    §5.4) -- e.g. a 7423's '7423/1' and '7423/2'."""
    if shape is None:
        return []
    return [x for x in shape['gate_drawings'] if x is not None and x.cls == 'CSubShape']


def _is_whole_package(shape: Node, sub: Node) -> bool:
    """A gate drawing named exactly as its shape, beside others: the whole
    package in one drawing, stored with the device's gate types (37 symbols
    of a 1998 library: 7423 beside 7423/1 and 7423/2). It is not a gate
    type but an ordinary symbol of the device's name -- as in the DOS
    library, where the same three are three symbols."""
    name = (shape['name'] or '').upper()
    return len(_sub_shapes(shape)) > 1 and (sub['name'] or '').upper() == name


def _gate_types(shape: Node) -> List[Node]:
    """A shape's gate types: its gate drawings but a whole-package one."""
    return [x for x in _sub_shapes(shape) if not _is_whole_package(shape, x)]


def _gate_type_name(shape: Node, sub: Node) -> str:
    """The symbol name a placed part uses: the shape's own name, or -- for a
    shape with several gate drawings (a non-homogeneous part) -- the gate
    type's own '<device>/<n>' name, so each gate type becomes a symbol of
    its own with its own drawing and pins, as the parts' own ASCII *S blocks
    are. The gate drawing's stored name is used when it is the shape's name
    plus '/...' (how V5.72 stores them); otherwise '<shape>/<n>', n counting
    from 1 in gate order among the gate types (a sub-shape's name can be
    stale, see _read_component). A whole-package drawing (_is_whole_package)
    has the shape's own name."""
    subs = _sub_shapes(shape)
    name = shape['name'] or ''
    if len(subs) < 2 or _is_whole_package(shape, sub):
        return name
    own = sub['name'] or ''
    if own.upper().startswith(name.upper() + '/'):
        return own
    return f"{name}/{next(i for i, x in enumerate(_gate_types(shape), 1) if x is sub)}"


def _read_symbol(name: str, shape: Node, sub: Optional[Node] = None) -> km.Symbol:
    """The KiCad-side symbol for a shape -- for a shape with several gate
    drawings, for the one gate drawing `sub` (see _gate_type_name)."""
    sub_shapes = _sub_shapes(shape)
    if not sub_shapes:
        raise UnsupportedDesignError(f"library shape {name!r} has no sub-shape")
    if sub is None:
        sub = sub_shapes[0]
    width, height = _shape_size(sub)

    pins: List[km.SymbolPin] = []
    signal_pins: List[Tuple[str, List[str]]] = []
    symbol_attributes: Dict[str, str] = {}
    max_gate_count = 1
    for item in sub['pins']:
        if item is None or item.cls != 'CShapePin':
            continue
        attrs = _pin_attributes(item)
        # A hierarchical port shape's own PORT= value (this project's
        # is_port_sym, shared with the ASCII side, looks for it on the
        # symbol itself) is stored per-pin in the binary, not shape-wide as
        # the ASCII *S block's own sym_attrs model it -- a port shape has
        # exactly one relevant pin, so reading it here is equivalent.
        if 'PORT' in attrs:
            symbol_attributes['PORT'] = attrs['PORT']
        # A pin of a multi-gate shape (each gate at the same relative position
        # but a different real pin) carries one label per gate; ASCII stores
        # these joined by commas on the one shared SymbolPin too, and leaves
        # picking the right one per placed unit to whatever resolves a
        # reference designator's actual pin -- not attempted here.
        max_gate_count = max(max_gate_count, len(item['number_labels']))
        net_name = (attrs.get('UT_PINNAME') or attrs.get('UT_SHEETNETNAME')
                    or attrs.get('UT_NETNAME', ''))
        number = _pin_number(item['number_labels']) if item['number_labels'] else ''
        pin = km.SymbolPin(
            x=item['x'], y=item['y'],
            pin_rotation=item['orientation'] & 3,
            pin_type=_PIN_TYPE_BY_CODE.get(item['pin_type'], 'PAS'),
            pin_format=_pin_format(item['style'], item['pin_type']),
            number=number,
            # UT_PINNAME is the ASCII format's LABEL; when a pin has none (the
            # usual case for a bare power pin) its implicit net name --
            # UT_NETNAME, ASCII's WIRELABEL/SIGNAL -- is used instead.
            name=net_name,
        )
        if not _is_hidden_pin(item):
            pins.append(pin)
            continue
        # A hidden pin (_is_hidden_pin: a transceiver's VCC/GND among its
        # signal pins, a header's and a pad's GND pins) connects by its net
        # name only.
        # It goes into signal_pins in the ASCII side's own (netname,
        # [numbers]) shape, so the unmodified _lib_sym_signal_pins renders it
        # hidden and off-grid as it does a SIGNAL= line. Read as an ordinary
        # visible pin it was a real regression: drawn, and able to attract an
        # unrelated named-stub override in the resolver's axis match.
        #
        # One entry per hidden pin, as the ASCII side has one per SIGNAL=
        # line, with the pin's per-gate numbers de-duplicated: a shared power
        # pin stores the same number once per gate (a quad-gate IC's VCC: "14" four
        # times), and listing each copy made the writer emit four identical
        # hidden pins numbered 14 into the library symbol.
        #
        # A hidden pin with no net name of its own (UT_NETNAME) has nothing to
        # connect by: it is omitted altogether, as the export omits it --
        # a protection IC's two NC pins (type 8), one of them sitting exactly
        # on another pin's connection point, which made the override resolver
        # "correct" the part and the review window draw overlapping pins.
        # Taking its declared name (UT_PINNAME,
        # "NC") as the net instead would have tied both to one global net.
        net = attrs.get('UT_NETNAME')
        if not net:
            continue
        numbers = list(dict.fromkeys(t['text']['string'] for t in item['number_labels'] if t is not None))
        signal_pins.append((net, numbers))

    polylines: List[km.Polyline] = []
    circles: List[km.Circle] = []
    for item in sub['graphics']:
        if item is None:
            continue
        pen_style = _pen_style(item['drawing'])
        # CUcLine: a single straight segment, the symbol editor's line tool
        # (the line-and-shape test symbol has eight); same record as a polyline's.
        if item.cls in ('CUcPolyline', 'CUcLine'):
            polylines.append(km.Polyline(points=list(item['drawing']['points']),
                                         colour=_pen_colour(item['drawing']),
                                         linetype=_POLYLINE_STYLE_BY_PEN.get(pen_style, 0),
                                         pen_width=item['drawing']['pen'][1]))
        elif item.cls == 'CUcArc':
            circle = _read_circle(item['drawing']['points'])
            if circle is not None:
                circle.colour = _pen_colour(item['drawing'])
                circle.arc_linetype = _ARC_STYLE_BY_PEN.get(pen_style, 0)
                circle.pen_width = item['drawing']['pen'][1]
                circles.append(circle)
        elif item.cls in ('CUcRect', 'CUcPolygon'):
            # A rectangle's own record has fill nested inside its drawing
            # record (_rectangle adds it there); a polygon's is read before
            # its drawing record and sits directly on the polygon itself --
            # different only in where the walker happened to read it, not
            # in what it means.
            fill = item['drawing']['fill'] if item.cls == 'CUcRect' else item['fill']
            polylines.append(km.Polyline(
                points=_closed(item['drawing']['points']),
                colour=_pen_colour(item['drawing']),
                linetype=_POLYLINE_STYLE_BY_PEN.get(pen_style, 0),
                pen_width=item['drawing']['pen'][1],
                filled=bool(fill[0]), fill_colour=km.RgbColour.from_colorref(fill[1]),
            ))
        elif item.cls == 'CUcEllipse':
            fill = item['drawing']['fill']
            circle = _read_ellipse(item['drawing']['points'], bool(fill[0]),
                                   km.RgbColour.from_colorref(fill[1]))
            if circle is not None:
                circle.colour = _pen_colour(item['drawing'])
                circle.arc_linetype = _ARC_STYLE_BY_PEN.get(pen_style, 0)
                circle.pen_width = item['drawing']['pen'][1]
                circles.append(circle)
        elif item.cls == 'CUcPie':
            # CUcPie shares CUcArc's exact byte layout and geometry, closed
            # into a wedge by a line from each end to the centre -- see
            # _read_circle's own docstring for why its file's own ASCII
            # export cannot be used to check this (it draws a plain arc, in
            # the wrong place, at the wrong size -- the .SCH format has no
            # way to represent a pie at all).
            fill = item['fill']
            circle = _read_circle(item['drawing']['points'], bool(fill[0]),
                                  km.RgbColour.from_colorref(fill[1]), is_pie=True)
            if circle is not None:
                circle.colour = _pen_colour(item['drawing'])
                circle.arc_linetype = _ARC_STYLE_BY_PEN.get(pen_style, 0)
                circle.pen_width = item['drawing']['pen'][1]
                circles.append(circle)

    # is_port_sym's other recognised form -- DEVICE starting with 'PORT_' and
    # the shape's own name starting with the same -- has no separate stored
    # DEVICE default to read (a placed instance's own DEVICE lives on its own
    # part variant, not the shared shape; the shared shape's own default is
    # a placeholder, '-'). The convention itself is that the two are always
    # equal for this class of shape, so setting it from the shape's own name
    # is exact, not a guess.
    if name.upper().startswith('PORT_'):
        symbol_attributes['DEVICE'] = name

    # PARTS: no dedicated stored field found anywhere in a shape's own
    # object graph despite an explicit, positioned PARTS=N in every design
    # checked -- derived instead from the same comma-joined pin-label count
    # already established for a multi-gate shape's own pin numbers (a
    # six-gate shape's own pins carry six comma-joined labels each; PARTS=6
    # is exactly that count). Confirmed exactly against four different
    # multi-unit shapes' own real PARTS values (2, 4, 4 and 4).
    symbol_attributes.setdefault('PARTS', str(max_gate_count))

    sym_attrs = _symbol_attributes(shape, sub, width, height, max_gate_count)
    # Like the ASCII parser's own *S dict: every attribute, last value wins
    # per tag; PORT and PARTS, collected above, are among them anyway.
    for a in sym_attrs:
        symbol_attributes[a.tag] = a.default_value
    return km.Symbol(name=name, width=width, height=height, pins=pins,
                      polylines=polylines, circles=circles, attributes=symbol_attributes,
                      sym_attrs=sym_attrs,
                      signal_pins=signal_pins)


def shape_symbols(name: str, shape: Node) -> Dict[str, km.Symbol]:
    """The symbols of one library shape: for an ordinary shape one, named
    `name`; for a shape with several gate drawings (a non-homogeneous part)
    one per gate type, named by _gate_type_name and marked with its device
    (the shape's name) and gate number, so the writer joins them into one
    KiCad symbol (FILEFORMAT_ULTICAP_UTSCH.md §5.4); a whole-package drawing beside
    them is an ordinary symbol named `name` (_is_whole_package). Used for a design's shapes and
    for a .UTLIB's symbols (kiuc_utlib), which have the same form."""
    subs = _sub_shapes(shape)
    if len(subs) < 2:
        return {name: _read_symbol(name, shape)}
    out: Dict[str, km.Symbol] = {}
    for sub in subs:
        if _is_whole_package(shape, sub):
            out[name] = _read_symbol(name, shape, sub)
    for index, sub in enumerate(_gate_types(shape), 1):
        gate_name = _gate_type_name(shape, sub)
        if gate_name in out:
            continue
        sym = _read_symbol(gate_name, shape, sub)
        sym.gate_device = shape['name'] or ''
        sym.gate_index = index
        out[gate_name] = sym
    return out


# Presentation the ASCII export gives the two *S attributes that have no
# stored text of their own -- constant in every export seen: PINTYPE on all
# 238 pins of two test designs, PARTS on all 3 multi-gate shapes.
_PINTYPE_PRESENTATION = dict(dx=0, dy=-20, size=35, colour=1, visibility=4)
_PARTS_PRESENTATION = dict(dx=0, dy=-10, size=35, colour=12, visibility=4)


def _symbol_attributes(shape: Node, sub: Node, width: int, height: int,
                       gate_count: int) -> List[km.SymbolAttribute]:
    """The shape's own attributes as the ASCII *S block lists them, in the
    export's order:

      REFDES   the shape's own text -- the designator template ("U?")
      DEVICE, VALUE, PKG_TYPE, TXT...
               the sub-shape's attributes, then the shape's own for any tag
               the sub-shape doesn't carry (a quad-gate IC's two VALUE entries)
      PARTS    the gate count, when above 1 -- not stored
      per visible pin:
        PINTYPE  the pin's electrical type -- not stored as text
        #        its number labels, one per gate, comma-joined
        LABEL, WIRELABEL, PORT, SPICE#...
                 its own attributes, as for a placed unit's pins

    A hidden pin (_is_hidden_pin) gets none of these, as in the
    export: it lives on in Symbol.signal_pins and connects by name.
    Point of effect: a pin's own attributes use its connection point, every
    other one the shape's centre (FILEFORMAT_ULTICAP_UTSCH.md §9.6).
    """
    centre = (width // 2, height // 2)

    def from_text(tag, value, text, poe):
        return km.SymbolAttribute(
            tag=tag, default_value=value, dx=text['x'], dy=text['y'],
            dx_poe=poe[0], dy_poe=poe[1], size=_text_size(text),
            colour=_text_colour(text), visibility=_text_visibility(text))

    out: List[km.SymbolAttribute] = []
    # A bare '?' (or empty) template is emitted too; whether the ASCII form
    # keeps it depends on the shape's placements, not the shape -- see
    # _drop_unused_refdes_templates, applied once the sheet's components
    # are known.
    if shape['text'] is not None:
        # Value from the shape's own text ("U?"); presentation from the
        # sub-shape's own text, a "$" placeholder marking where the
        # designator is drawn -- 131 of 131 REFDES templates of four designs
        # against their exports. The shape's text often sits unset at (0, 0),
        # hidden (86 in the six-sheet test design, and the line-and-shape
        # test symbol); where it
        # is set it equals the "$" text (FILEFORMAT_ULTICAP_UTSCH.md §5.2).
        where = sub['text'] if sub.get('text') is not None else shape['text']
        out.append(from_text('REFDES', shape['text']['string'], where, centre))
    sub_attrs = [a for a in sub['attributes'] if _is_attr(a)]
    own = [a for a in shape['attributes'] if _is_attr(a)]
    sub_tags = {_attr_tag(a) for a in sub_attrs}
    template = sub_attrs + [a for a in own if _attr_tag(a) not in sub_tags]
    refs = {_attr_key(a): _text_value(a) for a in template}
    for a in template:
        out.append(from_text(_attr_tag(a),
                             _expand_references(_text_value(a), refs), a['text'], centre))
    if gate_count > 1:
        out.append(km.SymbolAttribute(tag='PARTS', default_value=str(gate_count),
                                      dx_poe=centre[0], dy_poe=centre[1], **_PARTS_PRESENTATION))
    for item in sub['pins']:
        if item is None or item.cls != 'CShapePin' or _is_hidden_pin(item):
            continue
        tip = _pin_tip(item, width, height)
        out.append(km.SymbolAttribute(tag='PINTYPE', default_value=_PIN_TYPE_BY_CODE.get(item['pin_type'], 'PAS'),
                                      dx_poe=tip[0], dy_poe=tip[1], **_PINTYPE_PRESENTATION))
        labels = [t for t in item['number_labels'] if t is not None]
        if labels:
            out.append(from_text('#', _pin_number(labels), labels[0]['text'], tip))
        for a in item['attributes']:
            if a is not None and a.cls == 'CUcAttribute':
                out.append(from_text(_PIN_ATTR_TAG.get(a['key'], a['key']), _text_value(a), a['text'], tip))
    return out


# Binary attribute key -> ASCII tag, for the ones read into ComponentAttribute
# and promoted onto Component. A blank key is the ASCII format's own TXT tag.
_ATTR_TAG = {'UT_DEVICE': 'DEVICE', 'UT_VALUE': 'VALUE', 'UT_PKGTYPE': 'PKG_TYPE', '': 'TXT'}


def _shown_variant(variants) -> Optional[str]:
    """The document variant Ulticap shows: the one whose first DWORD is 1.

    Variant settings (Symbols -> Variants) mark one variant visible; test
    files made for this flip exactly that DWORD (DEFAULT 1 / VARIANT1 0, or the
    reverse) and nothing else. DEFAULT, then the first one, stands in if no
    variant is marked -- not seen in any file, but a design must still read.
    """
    shown = next((v.name for v in variants if v.visible == 1), None)
    if shown is not None:
        return shown
    names = [v.name for v in variants]
    return 'DEFAULT' if 'DEFAULT' in names else (names[0] if names else None)


def _variant_doc_names(part_variant: Node) -> List[str]:
    """The document variants a part variant belongs to: those its symbol
    variant lists. A part is left out of a document variant by removing that
    variant from this list (in the part's properties); the part then
    disappears when that variant is shown."""
    symbol_variant = part_variant['symbol_variant']
    if symbol_variant is None:
        return []
    return [d.get('name') for d in symbol_variant['document_variants'] if d is not None]


def _part_variant_for(part: Node, shown: Optional[str]) -> Tuple[Optional[Node], bool]:
    """The CSymbolPartVariant that carries a placed part's own attributes, and
    whether the part is fitted in the shown document variant.

    The part links to its part variants directly: its own `variants` list --
    one per symbol variant of its symbol. A design without variants has
    exactly one (all 3,635 placed parts of the test designs). With several,
    the one whose symbol variant belongs to the shown document variant is
    Ulticap's displayed one (two test files: R1 is 10k with DEFAULT shown,
    100k with VARIANT1 shown). When none belongs to it, the part is left out
    of that variant: its first part variant still supplies its data, and it
    is reported as not fitted. A positional correspondence (the symbol's
    first variant's part_variants, in the order of its parts) was used long
    ago; it is wrong on 442 of 1,195 parts of the six-sheet test design.
    """
    own = [v for v in (part['variants'] or []) if v is not None]
    if shown is not None:
        match = next((v for v in own if shown in _variant_doc_names(v)), None)
        if match is not None:
            return match, True
        if own:
            return own[0], False
    return (own[0] if own else None), True


def variant_summary(schematic: km.Schematic) -> Optional[str]:
    """One log line for a design with schematic variants, None without:
    which variant was converted (the one Ulticap shows), the others, and the
    parts not fitted in it (kept in the KiCad schematic, marked)."""
    shown = next((s.shown_variant for s in schematic.sheets if s.shown_variant), None)
    if shown is None:
        return None
    others = sorted({v for s in schematic.sheets for c in s.components
                     for v in [x[0] for x in c.variant_values] + c.not_in_variants})
    not_fitted = sorted(c.display_refdes() for s in schematic.sheets for c in s.components
                        if not c.fitted)
    line = f'Schematic variants: {shown} shown and converted'
    if others:
        line += f'; other values kept as fields for {", ".join(others)}'
    if not_fitted:
        line += f'; not fitted in {shown} (kept, marked do-not-populate): {", ".join(not_fitted)}'
    return line


def _variant_attr_values(part_variant: Node) -> Dict[str, str]:
    """A part variant's attribute values by ASCII tag, as _read_component
    reads them (references such as %=5 expanded)."""
    own = [a for a in part_variant['attributes'] if _is_attr(a)]
    refs = {_attr_key(a): _text_value(a) for a in own}
    return {_attr_tag(a): _expand_references(_text_value(a), refs)
            for a in own}


def _other_variants(part: Node, chosen: Optional[Node], shown: Optional[str],
                    variants) -> Tuple[List[Tuple[str, str, str, str]], List[str]]:
    """What the shown variant hides: for every other document variant, the
    attribute values that differ from the shown ones -- (document variant,
    symbol variant, tag, value), a tag missing there giving '' -- and the
    variants the part is left out of. Equal values are not repeated: a part
    whose one symbol variant serves every document variant yields nothing."""
    own = [v for v in (part['variants'] or []) if v is not None]
    base = _variant_attr_values(chosen) if chosen is not None else {}
    values: List[Tuple[str, str, str, str]] = []
    absent: List[str] = []
    for v in variants:
        if v.name == shown:
            continue
        pv = next((p for p in own if v.name in _variant_doc_names(p)), None)
        if pv is None:
            absent.append(v.name)
            continue
        if pv is chosen:
            continue
        other = _variant_attr_values(pv)
        symbol_variant = pv['symbol_variant']['name'] or ''
        for tag in list(base) + [t for t in other if t not in base]:
            if other.get(tag, '') != base.get(tag, ''):
                values.append((v.name, symbol_variant, tag, other.get(tag, '')))
    return values, absent


# A unit pin's own attribute keys -> the ASCII tag the export writes for them
# (UT_PINNAME is the pin's name, LABEL; UT_NETNAME its implicit net name,
# WIRELABEL; PORT and SPICE# keep their own names). Verified on every
# per-instance pin attribute of the two-sheet test design.
# UT_SHEETNETNAME is a subsheet box's pin name -- the export writes the one
# instance of it (a subsheet box's bus pin) as LABEL, like UT_PINNAME.
_PIN_ATTR_TAG = {'UT_PINNAME': 'LABEL', 'UT_NETNAME': 'WIRELABEL', 'UT_SHEETNETNAME': 'LABEL'}


def _unit_pin_attributes(symbol: Node, part: Node) -> List[km.ComponentAttribute]:
    """Every pin-owned per-instance attribute of one placed unit: each pin's
    own number label ('#', the per-gate pin number -- "1" on U91A where the
    shared symbol pin says "1,4,9,12"), and each pin's own attributes
    (LABEL, WIRELABEL, PORT, SPICE#), exactly as the ASCII export writes
    them for the same instance.

    Shaped like a stored ASCII attribute: dx/dy at the text's own local
    (unrotated) position, dx_poe/dy_poe at the pin's connection point
    (_pin_tip), presentation from the text itself (_text_visibility & co.).
    The project's POE-matching resolver and the writer's multi-gate
    detection consume these exactly as they do the ASCII ones -- the latter
    is what makes a multi-unit part come out as separate KiCad units with
    only its own gate's pin numbers.

    A pin that _is_hidden_pin routes into the symbol's hidden
    signal_pins has no visible pin to attach to, so no '#' or name is
    emitted for it -- one aimed at a pin the symbol doesn't have would only
    produce an unresolvable-override warning (confirmed earlier for the
    named-stub case). It gets what the export writes for it instead: a
    SIGNAL=<net>,<number> attribute at its own number label's position,
    with that label's presentation and the shape's centre as its point of
    effect, after all the others.

    Replaces the earlier _named_stub_attributes, which emitted only one
    name per pin (UT_PINNAME, else UT_NETNAME) under a tag chosen by shape
    type rather than by key, and no pin numbers at all.
    """
    sub_shape = part['gate_drawing']
    if sub_shape is None:
        return []
    width, height = _shape_size(sub_shape)
    rotation = part['orientation']

    def attr(tag, value, text, tip):
        ldx, ldy = km.inverse_rot_transform(text['x'] - part['x'], text['y'] - part['y'], rotation)
        return km.ComponentAttribute(
            tag=tag, value=value, dx=ldx, dy=ldy, dx_poe=tip[0], dy_poe=tip[1],
            size=_text_size(text), colour=_text_colour(text),
            visibility=_text_visibility(text, rotation))

    centre = (width // 2, height // 2)
    out: List[km.ComponentAttribute] = []
    signals: List[km.ComponentAttribute] = []
    for pp in part['pins'] or []:
        if pp is None:
            continue
        shape_pin = pp['shape_pin']
        if shape_pin is None:
            continue
        if _is_hidden_pin(shape_pin):
            own_net = _find_attribute(pp['attributes'], 'UT_NETNAME')
            net = (_text_value(own_net) if own_net is not None
                   else _pin_attributes(shape_pin).get('UT_NETNAME', ''))
            label = pp['number_label']
            if net and label is not None and label['string']:
                signals.append(attr('SIGNAL', f"{net},{label['string']}", label, centre))
            continue
        tip = _pin_tip(shape_pin, width, height)
        if pp['number_label'] is not None and pp['number_label']['string']:
            out.append(attr('#', pp['number_label']['string'], pp['number_label'], tip))
        for a in pp['attributes'] or []:
            if a is None or a.cls != 'CUcAttribute':
                continue
            out.append(attr(_PIN_ATTR_TAG.get(a['key'], a['key']), _text_value(a), a['text'], tip))
    return out + signals


def _box_name_text(symbol: Node, part: Node, attr: Node) -> km.ComponentAttribute:
    """A subsheet box's sheet name as Ulticap draws it: its UT_SHEETNAME
    text, at its own stored (user-movable) position, presented like a
    component-level attribute (point of effect: the shape's centre)."""
    centre = _gate_centre(symbol['shape'])
    text = attr['text']
    ldx, ldy = km.inverse_rot_transform(text['x'] - part['x'], text['y'] - part['y'], part['orientation'])
    return km.ComponentAttribute(
        tag='FILE', value=_text_value(attr), dx=ldx, dy=ldy,
        dx_poe=centre[0], dy_poe=centre[1],
        size=_text_size(text), colour=_text_colour(text),
        visibility=_text_visibility(text, part['orientation']))


def _read_component(symbol: Node, part: Node, part_variant: Optional[Node]) -> km.Component:
    # The unit letter is only part of the reference designator for a
    # multi-unit symbol -- confirmed against single-unit parts ("U1", not
    # "U1A") and six-gate ICs ("U5A".."U5F") in two designs.
    unit = part['text']['string'] if len(symbol['parts']) > 1 else ''
    refdes = symbol['text']['string'] + unit
    if not symbol['in_netlist']:
        # No real, user-facing reference designator: its in_netlist flag is
        # 0 exactly then -- a title block, power symbols, ports -- on all
        # 1,558 placements of four designs against their exports; the ASCII
        # export writes no REFDES for any of them, and kiuc_ascii gives them
        # refdes=None. What the refdes TEXT holds for such a part varies:
        # Ulticap's "!18!" placeholder in one design, but bare digits ("166")
        # in another. Testing for the "!N!" form only would give every power
        # symbol and title block of the latter a numeric refdes -- which,
        # among other things, stops its title block from filling in.
        refdes = None
    # The library shape is symbol['shape'] (a CShape), matching the ASCII
    # format's *S name -- not part['gate_drawing']: that inner CSubShape's own
    # name field can be stale (seen holding a different, earlier shape's
    # name after the shape it lives in was copied from that other one).
    shape = symbol['shape']
    symbol_name = shape['name'] if shape is not None else ''
    own_sub = part['gate_drawing'] if len(_sub_shapes(shape)) > 1 else None
    if own_sub is not None:
        # A non-homogeneous part: this unit's own gate drawing (FILEFORMAT_ULTICAP_UTSCH.md §5.4).
        symbol_name = _gate_type_name(shape, own_sub)

    comp_attrs: List[km.ComponentAttribute] = []
    attributes: Dict[str, str] = {}
    # A component-level attribute's point of effect is the shape's centre
    # (a pin-owned one's is its pin's tip -- see _unit_pin_attributes);
    # verified on every such attribute of two test designs.
    centre = _gate_centre(shape, own_sub)
    if part_variant is not None:
        own = [a for a in part_variant['attributes'] if _is_attr(a)]
        refs = {_attr_key(a): _text_value(a) for a in own}
        for a in own:
            tag = _attr_tag(a)
            text = a['text']
            ldx, ldy = text['x'] - part['x'], text['y'] - part['y']
            ldx, ldy = km.inverse_rot_transform(ldx, ldy, part['orientation'])
            value = _expand_references(_text_value(a), refs)
            comp_attrs.append(km.ComponentAttribute(
                tag=tag, value=value, dx=ldx, dy=ldy,
                dx_poe=centre[0], dy_poe=centre[1],
                size=_text_size(text), colour=_text_colour(text),
                visibility=_text_visibility(text, part['orientation']),
            ))
            attributes[tag] = value

    # The displayed reference designator: the unit's own text holds its
    # position and presentation, showing the symbol's base text plus the
    # unit letter (FILEFORMAT_ULTICAP_UTSCH.md §9.1, §9.2). First, as in an export.
    if _clean(refdes) is not None and part['text'] is not None:
        t = part['text']
        ldx, ldy = km.inverse_rot_transform(t['x'] - part['x'], t['y'] - part['y'], part['orientation'])
        comp_attrs.insert(0, km.ComponentAttribute(
            tag='REFDES', value=refdes, dx=ldx, dy=ldy,
            dx_poe=centre[0], dy_poe=centre[1],
            size=_text_size(t), colour=_text_colour(t),
            visibility=_text_visibility(t, part['orientation'])))
        attributes['REFDES'] = refdes

    # Pin-owned attributes after the component-level ones, again as in an
    # export; like the ASCII parser, a repeated tag's last value wins in
    # `attributes`.
    for a in _unit_pin_attributes(symbol, part):
        comp_attrs.append(a)
        attributes[a.tag] = a.value

    return km.Component(
        x=part['x'], y=part['y'], rotation=part['orientation'],
        symbol_name=symbol_name,
        comp_attrs=comp_attrs, attributes=attributes,
        refdes=_clean(refdes), device=_clean(attributes.get('DEVICE')),
        value=_clean(attributes.get('VALUE')), pkg_type=_clean(attributes.get('PKG_TYPE')),
        wirelabel=_clean(attributes.get('WIRELABEL')),
        # Pin-owned texts take their pin's connection point (_unit_pin_attributes).
        pin_owned_poe=True,
    )


def _point_between(px: int, py: int, x1: int, y1: int, x2: int, y2: int) -> bool:
    """Whether (px, py) lies strictly between (x1, y1) and (x2, y2): on the
    same line, but not equal to either end. A wire whose own two stored
    points do not include a connection's coordinate, yet passes through it
    this way, is not really meeting anything there -- something else's own
    endpoint has simply landed partway along its run."""
    if (px, py) in ((x1, y1), (x2, y2)):
        return False
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    if cross != 0:
        return False
    along = (px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)
    if along <= 0:
        return False
    return along < (x2 - x1) ** 2 + (y2 - y1) ** 2


def _is_junction(conn: Node) -> bool:
    """Whether a connection is a real junction dot, not just a wire passing
    straight through or turning a corner at that point.

    A wire meeting a bus -- whether the wire's own endpoint lands on the
    bus, or the bus is only passing through -- is always a dot (a bus
    entry point, always marked). Otherwise a dot exists where any wire or
    bus passes through the point without ending there -- one run tapping
    into the middle of another, or two runs crossing at a stored connection
    -- or where three or more wires, buses and pins end at the same point.
    Two conductors that only end at the same point, with nothing passing
    through, are a corner or a plain two-way join -- not a dot. Verified
    exactly (981 of 981 wire/bus connections, 4 designs) against each
    design's own junction records, and against the export of a test with
    crossing wires.
    """
    items = [it for it in conn['items'] if it is not None]
    wires = [it for it in items if it.cls == 'CWire']
    buses = [it for it in items if it.cls == 'CBus']
    if wires and buses:
        return True
    n_terminal = sum(1 for it in items if it.cls == 'CSymbolPartPin')
    n_pass = 0
    px, py = conn['x'], conn['y']
    for it in wires + buses:
        (x1, y1), (x2, y2) = it['line']['points']
        if (px, py) in ((x1, y1), (x2, y2)):
            n_terminal += 1
        elif _point_between(px, py, x1, y1, x2, y2):
            n_pass += 1
    return n_pass >= 1 or n_terminal >= 3


def _read_wires(sheet_node: Node, off):
    """Walk the sheet's own connection list directly, not through the nets.

    A net's own connections do not reach everything: some connections (seen
    so far only ones holding a bus, or a lone unconnected pin) belong to no
    net at all, and are only reachable through the sheet's own item list.
    Reading from there instead is complete on the designs checked (exactly
    matching each one's ASCII netlist-layer line count) and correct either
    way, since every connection reachable through a net is also here.

    Returns (wires, junctions).
    """
    wires: List[km.Wire] = []
    junction_points: Dict[tuple, bool] = {}
    seen_wire = set()
    for conn in sheet_node['connections']:
        if conn is None:
            continue
        net = conn['net']
        net_id = net['id'] if net is not None else -1
        items = [it for it in conn['items'] if it is not None]
        run = [it for it in items if it.cls in ('CWire', 'CBus')]
        for item in run:
            if id(item) in seen_wire:
                continue
            seen_wire.add(id(item))
            points = item['line']['points']
            if len(points) != 2:
                continue
            (x1, y1), (x2, y2) = points
            wires.append(km.Wire(
                x1=x1 + off[0], y1=y1 + off[1], x2=x2 + off[0], y2=y2 + off[1],
                net_id=net_id, is_bus=(item.cls == 'CBus'),
            ))
        n_pin = sum(1 for it in items if it.cls == 'CSymbolPartPin')
        if run and len(run) + n_pin >= 2 and _is_junction(conn):
            # A physical point can be reached through more than one
            # connection (its wire and bus can each belong to a different
            # net); deduplicated below by coordinate.
            is_bus_entry = any(it.cls == 'CBus' for it in run)
            key = (conn['x'] + off[0], conn['y'] + off[1])
            junction_points[key] = junction_points.get(key, False) or is_bus_entry
    junctions = [km.Junction(x=x, y=y, is_bus_entry=e) for (x, y), e in junction_points.items()]
    return wires, junctions


def _read_sheet_node(sheet_node: Node, library_map: Dict[str, Node], off,
                     variants=()) -> km.Sheet:
    """Build a Sheet from one already-located CSheet node. The shared core of
    read_sheet (one sheet) and read_hierarchy (every sheet of a design).
    `variants` are the document's variants (its prologue): the components are
    read in the shown one, the others kept on each component."""
    xs = [p[0] for p in sheet_node['frame']['points']]
    ys = [p[1] for p in sheet_node['frame']['points']]
    sheet = km.Sheet(
        name=_sheet_name(sheet_node),
        xmin=min(xs) + off[0], xmax=max(xs) + off[0],
        ymin=min(ys) + off[1], ymax=max(ys) + off[1],
    )
    shown = _shown_variant(variants)
    if len(variants) > 1:
        sheet.shown_variant = shown

    # Every placed CSymbolPart of this sheet -- read from the sheet's own
    # change-history block, not the document-wide symbol map: that map has
    # no entry at all for a part with no reference designator (a subsheet
    # box, a PCB-only pseudo-part like a fiducial), and in a multi-sheet
    # design it holds every sheet's parts together, not just this one's.
    # A part carries its own owning symbol pointer, so nothing besides the
    # part itself is needed to resolve it.
    own_frame_part_ids = {id(p) for p in sheet_node['parts'] if p is not None}
    used_shapes: Dict[str, Tuple[Node, Optional[Node]]] = {}   # symbol name -> (shape, gate drawing)
    for part in sheet_node['undo']['live']:
        if part is None or part.cls != 'CSymbolPart':
            continue
        if id(part) in own_frame_part_ids:
            continue  # this sheet's own frame part, not a placed component
        symbol = part['symbol']
        if symbol is None:
            continue
        # A subsheet box is a part whose own UT_SHEETNAME names the child
        # sheet it represents. Everything else is an ordinary component --
        # including a part whose third DWORD (a copy of its gate drawing's
        # external-gate-swap flag) is 0, which is no PCB-only pseudo-part. The
        # six-sheet test design has 63 of them, all written by the V5.72
        # export as ordinary components and shown by the schematic --
        # isolators, transceivers, logic gates, a card socket, a MOSFET, and
        # its three fiducials too.
        #
        # Only placed units are in this list. A multi-unit symbol lists every
        # unit in its own `parts`, placed or not, and an unplaced one sits at
        # (0, 0) -- but it is not in any sheet's live list (32 such units in
        # six test designs, none there). So no position test is needed; one
        # would drop a unit really placed at the origin.
        name_attr = _find_attribute(part['attributes'], 'UT_SHEETNAME')
        part_variant, fitted = _part_variant_for(part, shown)
        comp = _read_component(symbol, part, part_variant)
        comp.fitted = fitted
        if len(variants) > 1:
            comp.variant_values, comp.not_in_variants = _other_variants(part, part_variant, shown, variants)
            if part_variant is not None and part_variant['symbol_variant'] is not None:
                comp.variant_name = part_variant['symbol_variant']['name'] or ''
            comp.variant_rows = [
                ((pv['symbol_variant']['name'] or '') if pv['symbol_variant'] is not None else '',
                 _variant_doc_names(pv), _variant_attr_values(pv).get('VALUE', ''))
                for pv in (part['variants'] or []) if pv is not None]
        comp.x += off[0]
        comp.y += off[1]
        if name_attr is not None:
            comp.file_ref = name_attr['text']['string']
            comp.file_text = _box_name_text(symbol, part, name_attr)
        sheet.components.append(comp)
        if comp.symbol_name:
            shape_node = symbol['shape']
            own_sub = part['gate_drawing'] if len(_sub_shapes(shape_node)) > 1 else None
            used_shapes.setdefault(comp.symbol_name, (shape_node, own_sub))

    for name, (shape_node, own_sub) in used_shapes.items():
        if own_sub is None:
            shape_node = library_map.get(name)
            if shape_node is not None and shape_node.cls == 'CShape':
                sheet.symbols[name] = _read_symbol(name, shape_node)
            continue
        # A non-homogeneous part: every gate type of its device, placed on
        # this sheet or not, so the writer can join them into one KiCad
        # symbol with one unit per gate (FILEFORMAT_ULTICAP_UTSCH.md §5.4).
        for gate_name, sym in shape_symbols(name, shape_node).items():
            sheet.symbols.setdefault(gate_name, sym)

    sheet.wires, sheet.junctions = _read_wires(sheet_node, off)

    # The sheet's current graphics and standalone text are not reachable
    # through its connection list; they sit in its undo block's live object
    # list -- present whether or not the file keeps an undo history.
    for item in sheet_node['undo']['live']:
        if item is None:
            continue
        if item.cls in ('CSheetLine', 'CSheetRect'):
            sheet.user_lines.extend(_read_user_lines(item, off))
        elif item.cls == 'CSheetArc':
            arc = _read_user_arc(item, off)
            if arc is not None:
                sheet.user_arcs.append(arc)
        elif _is_free_text(item):
            sheet.labels.append(_read_label(item, off))
        elif item.cls in ('CWire', 'CBus'):
            sheet.annotations.extend(_read_wire_annotations(item, off))

    _drop_unused_refdes_templates(sheet)
    return sheet


def _drop_unused_refdes_templates(sheet: km.Sheet) -> None:
    """Remove the REFDES template of every shape whose template is a bare
    '?' (or empty) and none of whose placements on this sheet has a real
    reference designator -- power symbols, ports, title blocks, subsheet
    boxes, whose placements carry only Ulticap's !N! placeholder (read as
    no refdes) or nothing. The ASCII form writes no REFDES line for those,
    but does for a '?' template whose placement is really designated
    (the line-and-shape test symbol, placed as "2"): 7 such shapes, 3 designs, no
    exception."""
    designated = {c.symbol_name for c in sheet.components if c.refdes is not None}
    for name, sym in sheet.symbols.items():
        if name in designated:
            continue
        sym.sym_attrs = [a for a in sym.sym_attrs
                         if not (a.tag == 'REFDES' and a.default_value in ('?', ''))]
        if sym.attributes.get('REFDES') in ('?', ''):
            del sym.attributes['REFDES']


def _sheet_name(sheet_node: Node) -> str:
    """A sheet's own UT_SHEETNAME, on its own frame part's attributes
    directly (not through the variant system ordinary components use) --
    confirmed across five designs, both with and without a file extension
    in the value depending on how the design happened to be saved."""
    frame_part = sheet_node['parts'][0] if sheet_node['parts'] else None
    if frame_part is None:
        return ''
    name = _find_attribute(frame_part['attributes'], 'UT_SHEETNAME')
    return name['text']['string'] if name is not None else sheet_node['name']['string']


def read_sheet(stream: bytes) -> km.Sheet:
    """Build a Sheet from a stream's single sheet.

    Refuses a document with more than one sheet -- use read_hierarchy or
    read_root_sheet for those -- rather than guess which one was meant.

    A file's own stored coordinates are read as-is -- see the module
    docstring for why that is the correct behaviour, not merely the
    reader's default, even for the small number of designs whose own
    coordinates were shifted by an earlier SCH-to-UTSCH import.
    """
    doc = kw.read_document(stream)
    if len(doc.sheets) != 1:
        raise UnsupportedDesignError(f"expected exactly one sheet, found {len(doc.sheets)}")
    return _read_sheet_node(doc.sheets[0], doc.library_map, (0, 0), doc.prologue.variants)


def read_root_sheet(stream: bytes) -> km.Sheet:
    """Build just the root sheet of a design, whether it has one sheet or
    many -- unlike read_sheet, which refuses more than one, and unlike
    read_hierarchy, which builds all of them. Useful wherever only the root
    sheet's own contents are wanted (comparing a hierarchical design's own
    root against a same-named external file, for instance) without the cost
    of reading every sheet just to discard all but one.

    The root is the sheet with no parent; if a document somehow has none
    (not seen in any file examined -- every design has exactly one), the
    first sheet is used.
    """
    doc = kw.read_document(stream)
    if not doc.sheets:
        raise UnsupportedDesignError("no sheets found")
    root = next((sn for sn in doc.sheets if sn is not None and sn['parent'] is None), doc.sheets[0])
    return _read_sheet_node(root, doc.library_map, (0, 0), doc.prologue.variants)


def read_hierarchy(stream: bytes) -> Dict[str, km.Sheet]:
    """Build every sheet of a hierarchical design, keyed by each sheet's own
    name (Sheet.name -- the same string a subsheet-box Component's own
    file_ref carries, so the two can be matched directly by name without
    resolving any actual filename: confirmed exactly across five designs,
    two and three levels deep, including one whose own filenames are
    corrupted by an 8.3-name export defect that has no bearing on this,
    since nothing here depends on a filename at all).

    Every sheet's own stored coordinates are read as-is -- see the module
    docstring for why that is correct, not merely convenient, for a
    hierarchical design's own sheets exactly as for a single one.
    """
    return _hierarchy(kw.read_document(stream))


def _hierarchy(doc: kw.Document) -> Dict[str, km.Sheet]:
    """read_hierarchy for a document already read."""
    return {
        _sheet_name(sheet_node): _read_sheet_node(sheet_node, doc.library_map, (0, 0), doc.prologue.variants)
        for sheet_node in doc.sheets if sheet_node is not None
    }



def read_schematic(stream: bytes) -> km.Schematic:
    """Build a Schematic ready for kiuc_writer.write_schematic() -- every
    sheet of a hierarchical design (read_hierarchy), with the fields that
    exist only for the writer's benefit filled in the way it actually needs
    them, not the way their own names suggest:

    - root_sheet is computed here from the parent chain, not trusted from
      any binary equivalent of the ASCII format's own *R record -- there
      isn't one, and *R itself is not reliable for this even on the ASCII
      side (confirmed directly: a nested sub-sheet's own *R can name its
      immediate parent instead of the true root).
    - is_less_than_v500 is set True on every sheet unconditionally. Its
      real effect, not its name, is what matters here: it tells the writer
      to skip a correction for a known ASCII-export-specific arc defect
      that this reader's own geometry was never subject to in the first
      place, whatever the original design's true DOS/Windows origin
      -- confirmed necessary directly: left at its dataclass default
      (False, "assume the defect may be present"), the writer wrongly
      re-corrects an already-correct wide-sweep arc into the wrong one.

    Verified end to end, not just by inspection: run through the project's
    own kiuc_writer.write_schematic() unmodified, on every hierarchical
    design available, including a real six-sheet, 1,100-plus-component
    design (0.6 s, no crash, no warning) -- a subsheet box's own pin and
    its child's matching hierarchical_label came out with the identical
    name and shape on both sides of the boundary, exactly as intended.
    """
    doc = kw.read_document(stream)
    sheets_by_name = _hierarchy(doc)
    if not sheets_by_name:
        return km.Schematic(sheets=[])

    parent_name_of: Dict[str, str] = {}
    for sheet_node in doc.sheets:
        if sheet_node is None or sheet_node['parent'] is None:
            continue
        parent_name_of[_sheet_name(sheet_node)] = _sheet_name(sheet_node['parent'])

    root_name = next((n for n in sheets_by_name if n not in parent_name_of), next(iter(sheets_by_name)))
    for sheet in sheets_by_name.values():
        sheet.root_sheet = root_name
        sheet.is_less_than_v500 = True

    ordered = [sheets_by_name[root_name]]
    seen = {root_name}
    stack = [root_name]
    while stack:
        name = stack.pop()
        for comp in sheets_by_name[name].components:
            if comp.file_ref and comp.file_ref in sheets_by_name and comp.file_ref not in seen:
                seen.add(comp.file_ref)
                ordered.append(sheets_by_name[comp.file_ref])
                stack.append(comp.file_ref)
    # Any sheet not reached this way (should not happen for a design this
    # reader can walk at all, since every sheet's parent chain ends at some
    # sheet in the same document) is still included, so nothing is silently
    # dropped -- appended after the reachable tree, in no particular order.
    ordered.extend(sh for name, sh in sheets_by_name.items() if name not in seen)
    return km.Schematic(sheets=ordered)


def read_schematic_file(path) -> Tuple[km.Schematic, List[str]]:
    """read_schematic() for a .UTSCH file on disk, with the root sheet named
    after the file itself -- what every conversion path uses.

    A .UTSCH stores its own sheet names, and the root's is whatever it was
    when the design was created or imported: a Save As changes the file
    name, never the stored names (a design saved as NEW.UTSCH still calls its
    root "OLD.SCH"). Output is named after the sheets, so without this
    the root -- and with it the .kicad_pro -- would ignore the name the
    user actually gave the design, and could miss a DDF renamed alongside
    it (a DDF normally shares its root file's name). Ulticap's own ASCII
    export does the same: the root .SCH is named after the .UTSCH file,
    sub-sheets keep their stored names. Sub-sheets are left alone here
    too -- their stored names are the only names they have, and the
    subsheet boxes that reference them use exactly those.

    One exception, returned as a warning rather than applied: if the file
    name would give the root the same name as one of its own sub-sheets,
    the two would be written to the same .kicad_sch, so the root keeps its
    stored name instead.

    Returns (schematic, warnings)."""
    path = Path(path)
    sch = read_schematic(kb.read_container(path).stream)
    warnings: List[str] = []
    if not sch.sheets:
        return sch, warnings
    root = sch.sheets[0]
    new_name = path.name
    new_stem = path.stem.upper()
    clash = next((sh.name for sh in sch.sheets[1:]
                  if Path(sh.name or '').stem.upper() == new_stem), None)
    if clash is not None:
        warnings.append(
            f"root sheet keeps its stored name {root.name!r} rather than the file's "
            f"{new_name!r}: a sub-sheet is already named {clash!r}, and both would be "
            f"written to the same .kicad_sch.")
        return sch, warnings
    root.name = new_name
    for sh in sch.sheets:
        sh.root_sheet = new_name
    return sch, warnings


def read_sheet_from_path(path) -> km.Sheet:
    """Convenience wrapper: read a .UTSCH file straight from disk."""
    return read_sheet(kb.read_container(path).stream)
