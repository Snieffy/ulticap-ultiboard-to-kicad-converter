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
kiuc_model.py — Shared data model for Ulticap schematics.

Coordinates are in Ulticap units: 1 unit = 2 mils = 0.0508 mm.
Y-axis convention: positive Y is UP in both ASCII and binary.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
import math
from typing import Optional, List, Dict, Tuple


# ── constants ──────────────────────────────────────────────────────────────────

MM_PER_UNIT  = 0.0508     # 1 Ulticap unit = 2 mils = 0.0508 mm
MM_PER_MIL   = 0.0254     # for values stored directly in raw mils (e.g. line/circle thickness)

# rotation encoding: 0=0° 1=90° 2=180° 3=270° 4=mirror+0° … 7=mirror+270°
_ROT_ANGLE  = (0, 90, 180, 270, 0,  90, 180, 270)
_ROT_MIRROR = (0,  0,   0,   0, 1,   1,   1,   1)

# PINTYPE tag values (from attribute lines) → KiCad electrical type
PINTYPE_TAG_MAP = {
    'PAS': 'passive',   'IN':  'input',    'OUT': 'output',
    'OC':  'open_collector', 'OE': 'open_emitter',
    'BI':  'bidirectional',  'TRI': 'tri_state',
    'PWR': 'power_in',  'NC':  'no_connect',
}

# Ulticap symbol-polyline linetype code → canonical style name.
# Shared by kiuc_writer.py (maps the name directly to a KiCad stroke "type")
# and kiut_viewer.py (maps the name to a Qt dash pattern for on-screen
# rendering). Kept as a single source so a newly discovered code — e.g.
# from the binary UTSCH format — only needs to be added in one place.
UC_POLYLINE_STYLE_BY_CODE: Dict[int, str] = {
    0: 'solid', 4096: 'dash', 8192: 'dash_dot', 12288: 'dot',
}

# Ulticap symbol-arc linetype code → canonical style name. Same sharing
# rationale as UC_POLYLINE_STYLE_BY_CODE above; arcs use a distinct,
# smaller code range from polylines so the two are kept as separate tables.
UC_ARC_STYLE_BY_CODE: Dict[int, str] = {
    0: 'solid', 1: 'dash', 2: 'dash_dot', 3: 'dot',
}

# The DOS program's own text font (ASCII .SCH/.BLK files, decoded as CP437)
# follows the CP437 layout except for eight codes. Observed from experiments
# (every code drawn on screen in V4.91): 176-178 draw nothing, and 219-223 are
# IEC qualifying symbols, not CP437's block shapes. Applied to the decoded
# text, so the characters KiCad receives are the ones Ulticap drew. Files of
# the Windows program (.UTSCH) use Windows fonts and do not need this.
DOS_GLYPHS = str.maketrans({
    '\u2591': ' ', '\u2592': ' ', '\u2593': ' ',     # 176-178: blank
    '\u2588': '\u2293',                               # 219
    '\u2584': '\u25B7',                               # 220
    '\u258C': '\u2277',                               # 221
    '\u2590': '\u25AB',                               # 222
    '\u2580': '\u25C1',                               # 223 (220 mirrored)
})


def convert_overline(text: str) -> str:
    """Convert Ulticap overline syntax ^text^ to KiCad ~{text}.

    Rules (from spec):
    - First '^' starts overline.
    - Second '^' ends it.
    - If no second '^', overline continues to end of text.
    """
    if '^' not in text:
        return text
    result = []
    i = 0
    while i < len(text):
        if text[i] == '^':
            # Find closing '^'
            j = text.find('^', i + 1)
            if j == -1:
                # No closing: overline to end
                inner = text[i + 1:]
                result.append(f'~{{{inner}}}')
                break
            else:
                inner = text[i + 1:j]
                result.append(f'~{{{inner}}}')
                i = j + 1
        else:
            # Find next '^'
            j = text.find('^', i)
            if j == -1:
                result.append(text[i:])
                break
            else:
                result.append(text[i:j])
                i = j
    return ''.join(result)


def rotation_angle(rot: int) -> int:
    return _ROT_ANGLE[rot & 7]

def rotation_mirror(rot: int) -> bool:
    return bool(_ROT_MIRROR[rot & 7])


# ── geometry primitives ────────────────────────────────────────────────────────

@dataclass
class Polyline:
    """Graphic polyline (list of (x,y) integer tuples).
    Group header token is a combined <line_ctw> value:
    <line_ctw> = line_color | line_type | line_width
      line_color (bits 0-3):  0..15 palette index
      line_type  (bits 12-13): {0:solid,4096:dash,8192:dash-dot,12288:dot}
      line_width (bit 8):      {0:thin,256:thick} -- Ulticap DOS's two fixed
                               widths, 6 and 30 Ulticap units
    """
    points: List[Tuple[int, int]] = field(default_factory=list)
    colour: int = 0     # masked palette index (0-15), from the 1,<colour>,... group header
    is_bus: bool = False
    linetype: int = 0   # raw code: 0=solid,4096=dash,8192=dash-dot,12288=dot
    width: int = 6       # thin 6 (flag=0) or thick 30 (flag=256), Ulticap units
    # Fill: UTSCH-only, an option of the symbol editor with no ASCII .SCH
    # representation at all (confirmed against a file with two otherwise-
    # identical symbols, one filled and one not: their .SCH export is
    # byte-for-byte identical). filled/fill_colour are therefore always
    # False/0 for a Polyline built from a .SCH file; kiuc_binary_semantic.py
    # is the only source that ever sets them. fill_colour is a direct RGB
    # value (0xBBGGRR), not a palette index like colour above -- the binary
    # format does not use the ASCII side's 16-colour palette for this field.
    filled: bool = False
    fill_colour: int = 0
    # Pen width: UTSCH-only -- a drawing object's stored pen width, in
    # Ulticap units (1 unit = 2 mil). None for
    # anything read from a .SCH file, which has only the thin/thick flag
    # above; when set, the writer draws exactly this width instead.
    pen_width: Optional[int] = None


@dataclass
class Circle:
    """Circle/arc from a *S definition.
    Spec: <cx>,<cy>,<r>,<rotate>,<angle>,<colour>,<thick>,<arc_linetype>,
    rotate:       start angle raw (degrees = value/64), CCW, 0=East
    angle:        arc sweep raw (23040 = full circle = 360 deg)
    thick:        raw file value is a 0/1 flag {0:thin,1:thick}; stored here
                  as the width value, 6 or 30 (Ulticap DOS's fixed widths, in
                  Ulticap units).
    arc_linetype: raw code {0:solid,1:dash,2:dash-dot,3:dot}
    """
    cx: int
    cy: int
    r: int
    rotate: int = 0      # raw start angle (deg = rotate/64)
    angle: int = 23040   # raw sweep (23040 = full circle)
    colour: int = 0
    thick: int = 6        # 6 or 30, Ulticap units (from the 0/1 file flag)
    arc_linetype: int = 0  # raw code: 0=solid,1=dash,2=dash-dot,3=dot
    # Fill: UTSCH-only; see Polyline.filled/fill_colour above -- same
    # meaning, same reason it is never set by anything reading a .SCH file.
    filled: bool = False
    fill_colour: int = 0
    # Pen width: UTSCH-only -- a drawing object's stored pen width, in
    # Ulticap units (1 unit = 2 mil). None for
    # anything read from a .SCH file, which has only the thin/thick flag
    # above; when set, the writer draws exactly this width instead.
    pen_width: Optional[int] = None
    # Pie: UTSCH-only, a distinct shape from a plain arc -- a straight line
    # is drawn from each arc endpoint to (cx, cy), closing it into a wedge,
    # whether or not it is filled. The .SCH format has no way to represent
    # this at all: Ulticap's own export of one draws a plain arc, in the
    # wrong place, at the wrong size, with no cutout -- confirmed against a
    # real file, not merely inferred from the format's own syntax.
    is_pie: bool = False
    # Ellipse radii: UTSCH-only -- a CUcEllipse's own horizontal/vertical
    # radii (half its stored bounding box). None for a true circle or arc
    # and for anything read from a .SCH file (which has no ellipse); r above
    # then still holds their average, so every existing reader of r keeps
    # working. When they differ, the writer draws the true ellipse.
    rx: Optional[float] = None
    ry: Optional[float] = None

    @property
    def start_deg(self) -> float:
        return self.rotate / 64.0

    @property
    def sweep_deg(self) -> float:
        return self.angle / 64.0

    @property
    def is_full_circle(self) -> bool:
        return self.angle >= 23040


@dataclass
class SymbolPin:
    """A pin entry in a *S block.
    Spec: <pin format>, <pin rotation>, <pin rel X>, <pin rel Y>
    pin_format: decorative marker (0=none, 1=inverted, 2=clock, …)
    pin_rotation: 0=EAST(right), 1=NORTH(up), 2=WEST(left), 3=SOUTH(down)
    x, y: relative position within symbol bounding box
    """
    x: int
    y: int
    pin_format: int = 0    # decorative: {0:none,1:INVERTED,2:CLOCK,...}
    pin_rotation: int = 0  # direction: {0:E,1:N,2:W,3:S}
    pin_type: str = 'PAS'  # from paired PINTYPE attribute tag
    number: str = ''       # from paired # attribute tag
    name: str = ''         # from paired LABEL attribute tag

    @property
    def kicad_angle(self) -> int:
        """KiCad pin angle (degrees, CCW from East)."""
        return (self.pin_rotation * 90) % 360


@dataclass
class SymbolAttribute:
    """One attribute slot in a *S definition (template with position info).
    Spec: <X rel> <Y rel> <X POE rel> <Y POE rel> <size> <color> <visibility> <tag> = <value>
    """
    tag: str
    default_value: str = ''
    dx: int = 0       # X relative to symbol origin
    dy: int = 0       # Y relative to symbol origin
    dx_poe: int = 0   # X POE relative
    dy_poe: int = 0   # Y POE relative
    size: int = 35    # text height (character cell), Ulticap units (2 mil)
    colour: int = 0
    visibility: int = 128  # 0=none,64=TAG,128=VALUE,192=TAG+VALUE + alignment


@dataclass
class ComponentAttribute:
    """One attribute on a placed component (*C block).
    Spec: <X rel> <Y rel> <X POE rel> <Y POE rel> <size> <color> <visibility> <tag> = <value>
    """
    tag: str
    value: str
    dx: int = 0
    dy: int = 0
    dx_poe: int = 0
    dy_poe: int = 0
    size: int = 35
    colour: int = 0
    visibility: int = 128


@dataclass(frozen=True)
class PinOverrideCorrection:
    """One *C-level pin-name/number override whose (dx_poe, dy_poe) was
    corrected during conversion (see kiuc_writer._resolve_pin_override_poes
    for the full detection/correction rationale). Accumulated by
    kiuc_writer.py during conversion (get_pin_override_corrections()),
    consumed by kiuc_ascii.write_pin_override_corrections() to patch the
    actual .SCH source file(s) afterwards -- the two live in kiuc_model.py
    rather than either producer/consumer module importing the other,
    since kiuc_ascii.py must not import from kiuc_writer.py.

    Identifies the specific raw *C attribute line to patch by the
    component's own identifying fields (sheet + position + rotation +
    symbol name -- unique per placement) plus the override's own (tag,
    value, dx, dy) -- unique within one component's overrides, and
    unaffected by the correction itself, which only ever touches
    dx_poe/dy_poe.
    """
    sheet_name: str
    comp_x: int
    comp_y: int
    comp_rotation: int
    comp_symbol_name: str
    tag: str
    value: str
    dx: int
    dy: int
    old_dx_poe: int
    old_dy_poe: int
    new_dx_poe: int
    new_dy_poe: int


# ── schematic objects ──────────────────────────────────────────────────────────

@dataclass
class Symbol:
    """Symbol definition (*S block)."""
    name: str
    width: int = 0
    height: int = 0
    polylines: List[Polyline]        = field(default_factory=list)
    pins:      List[SymbolPin]       = field(default_factory=list)
    circles:   List[Circle]          = field(default_factory=list)
    sym_attrs: List[SymbolAttribute] = field(default_factory=list)
    # Convenience dict: tag → default_value (for quick lookup)
    attributes: Dict[str, str]       = field(default_factory=dict)
    # Invisible power pins from SIGNAL= tags: list of (net_name, [pin_numbers])
    # e.g. SIGNAL=VCC,14  →  ('VCC', ['14'])
    # e.g. SIGNAL=GND,1,21 → ('GND', ['1', '21'])
    signal_pins: List[Tuple[str, List[str]]] = field(default_factory=list)
    # A gate type of a non-homogeneous part (FILEFORMAT_ULTICAP_UTSCH.md §5.4):
    # the device it belongs to and its 1-based position among the device's
    # gate types. Set by the .UTSCH reader for a shape with several gate
    # drawings; a .SCH symbol's comes from its DEVICE=<device>/<n> instead
    # (see symbol_gate_type).
    gate_device: str = ''
    gate_index: int = 0


@dataclass
class Component:
    """Placed component instance (*C block)."""
    x: int
    y: int
    rotation: int          # 0–7: 0–3 quarter turns CCW, 4–7 the same mirrored
    symbol_name: str
    # Full attribute list preserving position/style metadata
    comp_attrs: List[ComponentAttribute] = field(default_factory=list)
    # Backward-compat flat dict (tag → value)
    attributes: Dict[str, str] = field(default_factory=dict)

    # Promoted well-known attributes
    refdes:    Optional[str] = None
    device:    Optional[str] = None
    value:     Optional[str] = None
    pkg_type:  Optional[str] = None
    wirelabel: Optional[str] = None
    file_ref:  Optional[str] = None
    # A .UTSCH subsheet box's displayed sheet name (its UT_SHEETNAME text,
    # tag FILE), kept apart from comp_attrs: the writer derives the KiCad
    # sheet's labels from a .SCH box's FILE= and is left unchanged for
    # .UTSCH input; the viewer draws this text where Ulticap shows it.
    file_text: Optional[ComponentAttribute] = None
    # True for a .UTSCH part: there a pin's number and name texts belong to
    # the pin itself, and their point of effect is that pin's connection
    # point, so it cannot name a wrong pin. A .SCH stores the point as
    # coordinates, which V5.72's export can scramble (see the writer's
    # _resolve_pin_override_poes); only those are checked and corrected.
    pin_owned_poe: bool = False
    # Schematic variants (.UTSCH only). Ulticap shows one document variant at a
    # time; the attributes above are that variant's. fitted is False when the
    # part is left out of it (Ulticap then hides the part): the converter keeps
    # such a part, marked, so its connections and data survive.
    fitted: bool = True
    # The part's attribute values in the document's other variants, only where
    # they differ from the shown one: (document variant, symbol variant, tag,
    # value) -- for a later variant switch in KiCad without losing data.
    variant_values: List[Tuple[str, str, str, str]] = field(default_factory=list)
    # The other document variants this part is left out of.
    not_in_variants: List[str] = field(default_factory=list)
    # The symbol variant the attributes above come from (e.g. SYMVAR1), set
    # only for a design with several document variants; for display.
    variant_name: str = ''
    # Every symbol variant of the part, as the file nests them: (symbol
    # variant, the document variants it serves, its value), in the part's
    # own order; set only for a design with several document variants; for
    # display (the viewer's Variants window).
    variant_rows: List[Tuple[str, List[str], str]] = field(default_factory=list)

    sheet_name: Optional[str] = None

    @property
    def angle(self) -> int:
        return rotation_angle(self.rotation)

    @property
    def mirror(self) -> bool:
        return rotation_mirror(self.rotation)

    def display_refdes(self) -> str:
        return (self.refdes or self.attributes.get('REFDES') or
               (self.symbol_name if self.symbol_name in ('TITLE', 'TITLE_REV') else '?'))


@dataclass
class Wire:
    """Net wire segment (*LV level=1)."""
    x1: int
    y1: int
    x2: int
    y2: int
    net_id: int = 0    # l1 = net number
    is_bus: bool = False  # l2: 0=wire, 1=bus


@dataclass
class UserLine:
    """User/graphic layer line (*LV level=2)."""
    x1: int
    y1: int
    x2: int
    y2: int
    linetype: int = 0   # 0=solid,256=dashed,512=dash-dot,768=dot
    colour: int = 0
    thickness: int = 6  # the thin/thick flag's width value, 6 or 30 (Ulticap
                        # DOS's fixed widths, as for symbol lines)
    # Pen width: UTSCH-only -- a drawing object's stored pen width, in
    # Ulticap units (1 unit = 2 mil). None for
    # anything read from a .SCH file, which has only the thin/thick flag
    # above; when set, the writer draws exactly this width instead.
    pen_width: Optional[int] = None


@dataclass
class UserArc:
    """A sheet-level arc or circle (graphic layer): UTSCH-only -- the ASCII
    format has no such record, and V5.72's export drops them. Kept as the
    stored (start, end, centre) points; the arc runs counter-clockwise from
    start to end around the centre, as a symbol arc does, and start == end
    means a full circle. Pen fields as for
    Circle: colour (RgbColour), arc_linetype (0 solid, 1 dash, 2 dash-dot,
    3 dot), pen_width (Ulticap units)."""
    x1: int
    y1: int
    x2: int
    y2: int
    cx: int
    cy: int
    colour: object = 0
    arc_linetype: int = 0
    pen_width: Optional[int] = None

    @property
    def is_full_circle(self) -> bool:
        return (self.x1, self.y1) == (self.x2, self.y2)


@dataclass
class Junction:
    """Junction dot (*V).

    is_bus_entry: True when the *V record's junction_type field == 1,
    meaning the junction connects a signal wire to a bus.  False (0)
    means an ordinary wire-to-wire (or bus-to-bus) junction.
    """
    x: int
    y: int
    is_bus_entry: bool = False


@dataclass
class Annotation:
    """Free-text annotation (*A).
    Spec: *A <X abs> <Y abs> <X POE abs> <Y POE abs> <size> <color> <visibility> <tag>=<value>
    """
    x: int       # absolute position
    y: int
    x_poe: int   # POE absolute position
    y_poe: int
    text: str
    size: int = 35
    colour: int = 0
    visibility: int = 128
    tag: str = 'LABEL'  # e.g. LABEL, WIDTH, TXT


@dataclass
class Label:
    """Cross-reference / net label (*X records)."""
    x: int
    y: int
    text: str
    size: int = 35
    colour: int = 0
    rotation: int = 0  # raw 1/64-degree units: 0, 5760 (90°), 11520 (180°), 17280 (270°)
    style: int = 0     # 0=normal,2=bold,4=italic,3=bold+italic
    align: int = 0     # alignment A-byte 0-8 {0:BL,1:BC,2:BR,3:CL,4:CC,5:CR,6:TL,7:TC,8:TR}


def x_text_angle(rotation: int) -> int:
    """A free text's (*X) angle in degrees, 0/90/180/270, from its raw
    1/64-degree rotation. Ulticap DOS draws 180 and 270 upside down; the
    .UTSCH reader only ever produces 0 or 5760 (V5.72 stores a horizontal
    or vertical flag)."""
    return {5760: 90, 11520: 180, 17280: 270}.get(rotation, 0)


# ── sheet ──────────────────────────────────────────────────────────────────────

@dataclass
class Sheet:
    """One schematic sheet."""
    name: str                  # filename, e.g. 'DESIGN.SCH'
    customer_name: str = ''    # *P <customer name> (optional documentation)
    major: int = 4             # version major (must be 4)
    minor: int = 60            # version minor (must be 60)
    nrm: str = '00000000'      # 8-digit NRM data
    xmin: int = -3025          # sheet bounds (Ulticap units)
    ymin: int = -2175
    xmax: int = 3021
    ymax: int = 2159
    grid: int = 25             # grid spacing (5th field of the bounds line; 25 when not given)
    root_sheet: str = ''       # *R <root sheet name>

    symbols:     Dict[str, Symbol]   = field(default_factory=dict)
    components:  List[Component]     = field(default_factory=list)
    wires:       List[Wire]          = field(default_factory=list)
    user_lines:  List[UserLine]      = field(default_factory=list)
    user_arcs:   List[UserArc]       = field(default_factory=list)   # UTSCH-only
    junctions:   List[Junction]      = field(default_factory=list)
    annotations: List[Annotation]    = field(default_factory=list)
    labels:      List[Label]         = field(default_factory=list)

    # Format discriminator: True = file created by Ulticap DOS versions (V4.xx and below).
    # False = file created by Ulticap Windows versions (V5.xx and above).
    # Affects arc rendering: V5.xx has arc encoding differences (complement-arc bug,
    # semicircle flip) that require correction before KiCad output.
    # Set from the version number in the SCH header (major < 5), not from *X records.
    is_less_than_v500: bool = False

    # .UTSCH only: the document variant the components were read in (the one
    # Ulticap shows), set when the design has more than one; None otherwise.
    shown_variant: Optional[str] = None

    # Writer-internal scratch state — NOT part of the parsed Ulticap model.
    # Populated by kiuc_writer.py during S-expression emission to record
    # which (possibly de-duplicated/renamed) lib_id each component was
    # written under, keyed by id(component). Declared here (rather than
    # monkey-patched onto the instance) so static type checkers can see it.
    _lib_id_override: Dict[int, str] = field(default_factory=dict)

    # Writer-internal scratch state, same rationale as _lib_id_override:
    # maps an original Ulticap symbol name to its collision-safe KiCad
    # lib_id (e.g. when two differently-cased or invalid-character symbol
    # names would otherwise clash).
    _lib_name_map: Dict[str, str] = field(default_factory=dict)

    def contains(self, x: int, y: int) -> bool:
        return self.xmin <= x <= self.xmax and self.ymin <= y <= self.ymax

    @property
    def width_mm(self) -> float:
        """Sheet width in mm (for KiCad paper size selection)."""
        return (self.xmax - self.xmin) * 0.0508

    @property
    def height_mm(self) -> float:
        """Sheet height in mm."""
        return (self.ymax - self.ymin) * 0.0508


@dataclass
class Schematic:
    """Top-level container: one or more sheets."""
    sheets: List[Sheet] = field(default_factory=list)


@dataclass
class Library:
    """A read symbol library (.UTLIB or .LIB): its symbols ({name: Symbol};
    the gate types of a non-homogeneous part under their own names, which
    the writer joins), what the reader had to say (renamed duplicates,
    repaired symbols) and how many symbols the library holds. A DOS
    library's arcs are drawn the DOS way (Sheet.is_less_than_v500)."""
    path: object
    symbols: Dict[str, Symbol] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    count: int = 0
    is_less_than_v500: bool = False


# ── geometry helpers ───────────────────────────────────────────────────────────

def rot_transform(dx, dy, rotation: int) -> tuple:
    """Apply Ulticap rotation+mirror to a (dx, dy) offset.

    The mirror flag (bit 2) negates the x-component of the rotation result.
    Verified against netlist ground truth for all 8 rotation values:

      rot 0 (  0°, no mirror): (+dx, +dy)
      rot 1 ( 90°, no mirror): (-dy, +dx)
      rot 2 (180°, no mirror): (-dx, -dy)   ← full 180° rotation
      rot 3 (270°, no mirror): (+dy, -dx)
      rot 4 (  0°, mirror):    (-dx, +dy)   ← mirror negates x
      rot 5 ( 90°, mirror):    (+dy, +dx)   ← mirror negates x vs rot 1
      rot 6 (180°, mirror):    (+dx, -dy)   ← mirror negates x vs rot 2
      rot 7 (270°, mirror):    (-dy, -dx)   ← mirror negates x vs rot 3
    """
    r = rotation & 3
    mirror = rotation & 4
    if r == 0:
        ox, oy =  dx,  dy
    elif r == 1:
        ox, oy = -dy,  dx
    elif r == 2:
        ox, oy = -dx, -dy
    else:
        ox, oy =  dy, -dx
    if mirror:
        ox = -ox
    return ox, oy


def inverse_rot_transform(dx, dy, rotation: int) -> tuple:
    """The (dx, dy) that rot_transform(dx, dy, rotation) turned into this
    (dx, dy): recovers a text's local, unrotated offset from its placed,
    rotated position (the .UTSCH reader stores texts already turned)."""
    if rotation & 4:
        dx = -dx
    r = rotation & 3
    if r == 0:
        return dx, dy
    if r == 1:
        return dy, -dx
    if r == 2:
        return -dx, -dy
    return -dy, dx


def pin_conn_point(pin: 'SymbolPin', sym: 'Symbol') -> tuple:
    """Return the pin connection point (wire-attach point) in *S coordinates.

    *S coordinate system: origin = BOTTOM-LEFT of bbox, x+ right, y+ UP.
    The connection point is on the bbox face the pin stub points TOWARD
    (i.e. away from the symbol body):
      rot=0 (E, stub right):  conn = (sym.width,  pin.y)      — right edge
      rot=1 (N, stub up):     conn = (pin.x,      sym.height) — top edge
      rot=2 (W, stub left):   conn = (0,           pin.y)     — left edge
      rot=3 (S, stub down):   conn = (pin.x,       0)         — bottom edge
    """
    r = pin.pin_rotation & 3
    if r == 0:  return (sym.width,  pin.y)
    if r == 1:  return (pin.x,      sym.height)
    if r == 2:  return (0,           pin.y)
    return          (pin.x,      0)


# ── colour / alignment helpers ──────────────────────────────────────────────
# Single source of truth shared by kiuc_writer.py (KiCad export) and
# kiut_viewer.py (screen/SVG/PDF/PNG rendering), so screen colours, print
# colours, and KiCad output colours never drift apart across the two files.

ULTICAP_PALETTE: Dict[int, tuple] = {
    # Screen-measured values from DOS Ulticap (CRT output, EGA DAC levels).
    # The EGA half-intensity level is 170 (0xAA), not the theoretical 128.
    0:  (0,   0,   0  ),  # black
    1:  (0,   0,   142),  # blue          — invisible pins
    2:  (0,   170, 0  ),  # green         — junctions
    3:  (0,   170, 170),  # cyan          — pin stubs
    4:  (170, 0,   0  ),  # red           — wires
    5:  (170, 0,   170),  # magenta       — symbol body default
    6:  (125, 85,  20 ),  # brown         — user lines
    7:  (170, 170, 170),  # light gray
    8:  (40,  40,  65 ),  # gray          — dark blue-gray
    9:  (32,  32,  235),  # light blue    — buses
    10: (0,   255, 0  ),  # light green
    11: (0,   170, 150),  # light cyan
    12: (170, 0,   85 ),  # light red
    13: (235, 40,  125),  # light magenta
    14: (227, 227, 0  ),  # yellow
    15: (227, 227, 227),  # white
}


class RgbColour(tuple):
    """A full-RGB colour, as a .UTSCH file stores it -- unlike an ASCII
    file's 16-colour palette index. Lives in the same `colour` fields as a
    palette index (Label, Annotation, ComponentAttribute, ...); every
    consumer resolves both through palette_rgb(), which passes this through
    unchanged.

    Kept as full RGB on purpose: a .UTSCH can use any colour, and mapping
    it onto the 16-entry palette would both lose that and substitute the
    DOS screen-measured palette values (ULTICAP_PALETTE) for what V5.72
    actually shows. Where colours don't suit KiCad, the converter's
    "KiCad default palette" option drops every colour override anyway.
    """
    def __new__(cls, r: int, g: int, b: int):
        return super().__new__(cls, (r, g, b))

    @classmethod
    def from_colorref(cls, value: int) -> 'RgbColour':
        """From a Windows COLORREF, which stores its bytes as 0x00BBGGRR."""
        return cls(value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)

    def __repr__(self) -> str:
        return 'RgbColour(%d, %d, %d)' % tuple(self)


def palette_rgb(colour) -> tuple:
    """Resolve a colour to (R,G,B): an RgbColour as-is, otherwise an Ulticap
    colour index.
    colour & 0x0F isolates the low nibble (palette index 0-15), stripping any
    flag bits that may be set in higher bits of the colour field (e.g. bit 8
    in symbol polyline group headers).  Safe for direct 0-15 values too:
    c & 0x0F == c for all c in 0..15."""
    if isinstance(colour, RgbColour):
        return tuple(colour)
    return ULTICAP_PALETTE.get(colour & 0x0F, (0, 0, 0))


def ulticap_translate(r_code: int, v: int) -> int:
    """Compute U (displayed alignment) from (R, V).

    Applies the R-dependent 3x3 grid transformation to A=V&31:
      R0,R1 : identity
      R2,R3 : row=2-row, col=2-col  (180° flip)
      R4,R7 : col=2-col             (L<->R mirror)
      R5,R6 : row=2-row             (B<->T mirror)
    Returns U in range 0-8 (H) or 16-24 (V).
    """
    a = v & 31
    is_v = a >= 16
    n = (a - 16) if is_v else a
    row, col = n // 3, n % 3
    r = r_code & 7
    if r in (2, 3):
        row, col = 2 - row, 2 - col
    elif r in (4, 7):
        col = 2 - col
    elif r in (5, 6):
        row = 2 - row
    return (16 if is_v else 0) + row * 3 + col


def is_pwr_symbol(sym) -> bool:
    """True if *sym* is a real KiCad power symbol: exactly one PWR-type pin.

    Such symbols already get correct power_in pin behaviour and graphics
    from the dedicated power-symbol pipeline (the is_pwr branches in
    _comp_sexp / _build_lib_symbols, which resolve the net name from *C/*S
    WIRELABEL or LABEL into the Value property).  They must be excluded
    from the named-stub mechanism — which exists only for pins that would
    NOT otherwise get KiCad's invisible-power_in name-merge, e.g. GND_LINK's
    *S PINTYPE=PAS — and from multi-gate / named-stub-variant lib_symbol
    splitting.  A generic *S WIRELABEL='?' template whose pin is already
    PINTYPE=PWR (e.g. a generic *SPOWER symbol) must flow through the normal power
    pipeline instead, exactly like any other power symbol.

    This is the single source of truth for that distinction, shared by
    kiuc_writer.py and kiut_viewer.py.
    """
    return sym is not None and len(sym.pins) == 1 and sym.pins[0].pin_type.upper() == 'PWR'


def is_port_sym(sym) -> bool:
    """True when *sym* is a subsheet module-port symbol.

    Two encoding styles:
    - V4.xx: *S has a PORT= attribute in its sym_attrs  (e.g. SPORT_OUT with PORT=OUT)
    - V5.xx: *S has DEVICE= whose value starts with 'PORT_' (e.g. DEVICE=PORT_IN)
      AND the symbol name itself starts with 'PORT_'.
    Both styles produce KiCad (hierarchical_label ...) elements, not regular
    (symbol ...) placements — a hierarchical net connection marker, not a
    real part.

    This is the single source of truth for that distinction, shared by
    kiuc_writer.py and kiut_viewer.py.
    """
    if sym is None:
        return False
    if sym.attributes.get('PORT') is not None:
        return True
    dev = sym.attributes.get('DEVICE', '')
    if dev.upper().startswith('PORT_') and sym.name.upper().startswith('PORT_'):
        return True
    return False


# ── non-homogeneous parts (FILEFORMAT_ULTICAP_UTSCH.md §5.4) ────────────────────────
# A part with dissimilar gates is one symbol per gate type in Ulticap, each
# with DEVICE=<device>/<n> (n = 1, 2, ... -- the manual's rule, p. 2125),
# GATE1=<letter> for the gate type's first gate letter and PARTS=<n> for its
# number of gates. Only a DEVICE value of that form counts on the .SCH side:
# a '/' elsewhere (a DEVICE value like ABC/TT, a symbol named CONN/90deg with
# DEVICE=CONN-90) is part of an ordinary name. The .UTSCH reader sets
# Symbol.gate_device/gate_index itself, from the shape's gate drawings.

_GATE_DEVICE_RE = re.compile(r'^(.+)/(\d+)$')


def symbol_gate_type(sym) -> Optional[Tuple[str, int]]:
    """(device, gate type number) for a gate type of a non-homogeneous
    part, else None."""
    if sym is None or is_pwr_symbol(sym) or is_port_sym(sym):
        return None
    if sym.gate_device:
        return sym.gate_device, sym.gate_index
    m = _GATE_DEVICE_RE.match((sym.attributes.get('DEVICE') or '').strip())
    if m and int(m.group(2)) > 0:
        return m.group(1), int(m.group(2))
    return None


def _symbol_parts(sym) -> int:
    try:
        return max(1, int(sym.attributes.get('PARTS', '1')))
    except (TypeError, ValueError):
        return 1


def gate_type_units(types: Dict[int, 'Symbol']) -> Dict[int, List[int]]:
    """The units (1 = gate letter A, 2 = B, ...) each gate type of one
    device covers: from its GATE1 letter, or else straight after the
    previous gate type's, for PARTS gates. A unit already taken by an
    earlier gate type is not given twice."""
    out: Dict[int, List[int]] = {}
    taken = set()
    nxt = 1
    for n in sorted(types):
        sym = types[n]
        g1 = (sym.attributes.get('GATE1') or '').strip().upper()
        start = ord(g1) - ord('A') + 1 if len(g1) == 1 and 'A' <= g1 <= 'Z' else nxt
        parts = _symbol_parts(sym)
        units = [u for u in range(start, start + parts) if u not in taken and u <= 26]
        taken.update(units)
        out[n] = units
        nxt = start + parts
    return out


def nonhomogeneous_devices(sheets) -> Dict[str, Dict[int, Tuple[str, 'Symbol']]]:
    """Every non-homogeneous device the design places a part of:
    device -> {gate type number: (symbol name, symbol)}, the gate types
    gathered from all sheets (one sheet may place only some of them).
    A device needs two gate types or more; one alone is an ordinary
    symbol."""
    placed = set()
    for sh in sheets:
        for comp in sh.components:
            if comp.file_ref is not None:
                continue
            g = symbol_gate_type(sh.symbols.get(comp.symbol_name))
            if g:
                placed.add(g[0])
    found: Dict[str, Dict[int, Tuple[str, Symbol]]] = {}
    for sh in sheets:
        for sn, sym in sh.symbols.items():
            g = symbol_gate_type(sym)
            if g and g[0] in placed:
                found.setdefault(g[0], {}).setdefault(g[1], (sn, sym))
    return {d: t for d, t in found.items() if len(t) >= 2}


def gate_unit_of(refdes: Optional[str], units: List[int]) -> Tuple[int, Optional[str]]:
    """(unit, package reference) of a part placed with a gate type covering
    `units`: the reference's last letter is the gate (U2B -> unit 2, U2)
    when it is one of the gate type's; else the gate type's first unit,
    and the reference unchanged (an unannotated U? included)."""
    rd = refdes or ''
    if len(rd) > 1 and 'A' <= rd[-1] <= 'Z':
        u = ord(rd[-1]) - ord('A') + 1
        if u in units:
            return u, rd[:-1]
    return (units[0] if units else 1), (refdes or None)


def u_to_justify(u: int):
    """Map U alignment code to (hjust, vjust, is_vertical, angle_deg).

    U encodes the anchor position within the rendered text box.
    Ulticap and KiCad define top/bottom relative to the screen in the same way
    (anchor at top edge = text hangs below), so no inversion is needed.

    Returns (hjust, vjust, is_vertical, angle_deg).
    """
    is_v = u >= 16
    n = (u - 16) if is_v else u
    col = n % 3    # 0=L, 1=C, 2=R
    row = n // 3   # 0=B, 1=C, 2=T  (Ulticap y-up)

    hjust = ('left', 'center', 'right')[col]
    vjust = ('bottom', 'center', 'top')[row]
    angle = 90 if is_v else 0
    return hjust, vjust, is_v, angle


def tight_bounds(sheet: 'Sheet') -> Optional[Tuple[int, int, int, int]]:
    """Return the true content bounding box of *sheet* as
    (min_x, min_y, max_x, max_y) in Ulticap units.

    Scans every wire endpoint, user-line endpoint, junction, annotation
    anchor, the box of every free text (*X), and all four corners of every placed component's
    symbol bounding box (after rotation transform).  Visible component
    attribute text anchor positions are also included so that REFDES/VALUE
    labels placed outside the symbol body do not cause clipping.

    Returns None when the sheet contains no scannable objects (empty
    sheet), in which case callers should fall back to sheet.xmin/ymin/
    xmax/ymax.
    """
    xs: List[int] = []
    ys: List[int] = []

    for w in sheet.wires:
        xs += [w.x1, w.x2];  ys += [w.y1, w.y2]

    for ul in sheet.user_lines:
        xs += [ul.x1, ul.x2];  ys += [ul.y1, ul.y2]

    for j in sheet.junctions:
        xs.append(j.x);  ys.append(j.y)

    for a in sheet.annotations:
        xs += [a.x, a.x_poe];  ys += [a.y, a.y_poe]

    for lb in sheet.labels:
        # A free text (*X): its whole box, not only its anchor -- a sheet of
        # text alone has no other content to size its paper by.
        for px, py in x_text_box(lb):
            xs.append(px);  ys.append(py)

    for c in sheet.components:
        rot = c.rotation
        sym = sheet.symbols.get(c.symbol_name)

        if sym is not None:
            # All four corners of the symbol bounding box after rotation.
            # Symbol local coords: (0,0) = bottom-left, (width,height) = top-right.
            w, h = sym.width, sym.height
            for dx, dy in ((0, 0), (w, 0), (0, h), (w, h)):
                ox, oy = rot_transform(dx, dy, rot)
                xs.append(c.x + ox);  ys.append(c.y + oy)

            # Visible attribute text anchors (REFDES, VALUE, etc.) — these
            # often extend well beyond the symbol body and are the most
            # common cause of border-clipping on printed output.
            # Use comp_attrs when present (per-instance overrides), fall back
            # to sym_attrs (symbol defaults) for tags not overridden.
            seen_tags: set = set()
            for ca in c.comp_attrs:
                if ca.visibility & 0x7F:   # any visible bits set
                    ox, oy = rot_transform(ca.dx, ca.dy, rot)
                    xs.append(c.x + ox);  ys.append(c.y + oy)
                    seen_tags.add(ca.tag)
            for sa in sym.sym_attrs:
                if sa.tag not in seen_tags and (sa.visibility & 0x7F):
                    ox, oy = rot_transform(sa.dx, sa.dy, rot)
                    xs.append(c.x + ox);  ys.append(c.y + oy)
        else:
            # Unknown symbol — use placement point only
            xs.append(c.x);  ys.append(c.y)

    if not xs:
        return None

    return (min(xs), min(ys), max(xs), max(ys))


def x_text_box(lb: 'Label') -> List[Tuple[int, int]]:
    """The four corners of a free text's (*X) box, in Ulticap units: Ulticap's
    box model (_uc_box_metrics, _uc_vertical_metrics) placed by its alignment
    (0-8: BL, BC, BR, CL, CC, CR, TL, TC, TR) around its anchor and turned by
    its angle (x_text_angle). Rounded outward."""
    _, _, _, bw = _uc_box_metrics(lb.text, lb.size)
    top, bottom = _uc_vertical_metrics(lb.size)
    bh = lb.size + top + bottom
    col, row = lb.align % 3, min(lb.align // 3, 2)
    x0 = (0.0, -bw / 2, -bw)[col]
    y0 = (0.0, -bh / 2, -bh)[row]
    a = math.radians(x_text_angle(lb.rotation))
    c, s = round(math.cos(a)), round(math.sin(a))
    out = []
    for px, py in ((x0, y0), (x0 + bw, y0), (x0, y0 + bh), (x0 + bw, y0 + bh)):
        rx, ry = lb.x + px * c - py * s, lb.y + px * s + py * c
        out.append((math.floor(rx) if rx < lb.x else math.ceil(rx),
                    math.floor(ry) if ry < lb.y else math.ceil(ry)))
    return out


def tight_bounds_with_gap(sheet: 'Sheet', gap_units: int) -> Optional[Tuple[int, int, int, int]]:
    """tight_bounds() padded by gap_units on all four sides.

    Shared by kiut_viewer.py (on-screen sheet-border rectangle, its own
    larger gap for visual comfort) and kiuc_writer.py (paper-size fitting,
    a smaller gap for physical page-fit calculations) — same underlying
    tight-bounds-plus-clearance pattern, different gap value per caller
    since the two gaps answer different questions and are not meant to
    match. Returns None when tight_bounds() itself returns None (empty
    sheet); callers should fall back to sheet.xmin/ymin/xmax/ymax as before.
    """
    tb = tight_bounds(sheet)
    if tb is None:
        return None
    return (tb[0] - gap_units, tb[1] - gap_units,
            tb[2] + gap_units, tb[3] + gap_units)

# ── Ulticap text-box positioning model ──────────────────────────────────────
# Shared by kiut_viewer.py (on-screen/SVG rendering) and kiuc_writer.py
# (.kicad_sch export) — the single source of truth for these constants.
# The model is a property of the file format, not of either renderer, so
# it lives in the shared module both import.
#
# Ulticap does not align text to an anchor point. It positions a virtual
# bounding box relative to the anchor per halign/valign, then always draws
# actual characters LEFT-JUSTIFIED within that box regardless of halign —
# confirmed empirically against real Ulticap files (see pins 5/7/8/9 of a
# real multi-pin symbol during testing). Box width = _UC_LEFT_MARGIN_U +
# sum(per-char advance) +
# _UC_RIGHT_MARGIN_U; margins are a fixed overhead independent of string
# length. Values scale linearly with size_u relative to _UC_REF_SIZE_U.
_UC_REF_SIZE_U          = 35.0   # size_u at which the measurements below were taken
_UC_CHAR_ADVANCE_U      = 20.0   # per-character advance width at _UC_REF_SIZE_U —
                                 # the font is monospaced except for the one
                                 # exception below
_UC_CHAR_ADVANCE_EXCEPTIONS = {'7': 23.0}  # '7' is measurably wider than all
                                 # other characters in Ulticap's DOS font
_UC_LEFT_MARGIN_U       = 3.0    # gap between box left edge and first character's ink
_UC_RIGHT_MARGIN_U      = 18.0   # gap between last character's ink and box right edge —
                                 # fixed regardless of string length (not per-character)
_UC_TOP_MARGIN_U        = 8.0    # gap between box top edge and cap height
_UC_BOTTOM_MARGIN_U     = 3.0    # gap between box bottom edge and baseline
_UC_DESCENDER_U         = 6.0    # descender allowance below the box bottom —
                                 # descriptive only, doesn't participate in
                                 # position math (see _uc_vertical_metrics)
_UC_OVERLINE_GAP_U      = 3.0    # measured gap between the top of overlined text
                                 # (cap height) and the overline decoration itself —
                                 # Ulticap's own fixed convention (ULTICAP_TEXT_MODEL.md
                                 # §3/§4), independent of rendering font, scales with
                                 # size_u the same way the other margins do


def _uc_char_advance(ch: str, size_u: float) -> float:
    """Per-character advance width matching Ulticap's own measured DOS-font
    convention (monospaced except '7'), scaled to the given size_u."""
    base = _UC_CHAR_ADVANCE_EXCEPTIONS.get(ch, _UC_CHAR_ADVANCE_U)
    return base * (size_u / _UC_REF_SIZE_U)


def _uc_box_metrics(text: str, size_u: float):
    """Returns (left_margin, content_width, right_margin, box_width) for a
    string, matching Ulticap's virtual-bounding-box model: margins are a
    fixed overhead independent of string length; content_width is the sum
    of each character's own advance (see _uc_char_advance)."""
    left = _UC_LEFT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    right = _UC_RIGHT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    content_w = sum(_uc_char_advance(ch, size_u) for ch in text)
    return left, content_w, right, left + content_w + right


def _uc_overline_gap(size_u: float) -> float:
    """Measured gap between the top (cap height) of overlined text and the
    overline decoration itself, scaled to size_u — see _UC_OVERLINE_GAP_U
    and ULTICAP_TEXT_MODEL.md §3/§4."""
    return _UC_OVERLINE_GAP_U * (size_u / _UC_REF_SIZE_U)


def _uc_vertical_metrics(size_u: float):
    """Returns (top_margin, bottom_margin) scaled to size_u — the vertical
    counterpart of _uc_box_metrics. Confirmed in ULTICAP_TEXT_MODEL.md §2/§6
    to follow the identical box-then-justify-toward-one-edge rule as
    horizontal: a virtual box is positioned per valign, and content is
    always baseline/bottom-justified within it, offset from the box's
    bottom edge by bottom_margin. Unlike the horizontal case, the box's
    'content' dimension (cap height) is font-dependent by nature — no
    Ulticap-measured, font-independent cap-height number exists to use
    instead — so viewer callers measure it from the real rendering font
    (the same hybrid approach used for the horizontal content-width fix).
    Writer callers, which don't choose or measure a font at all (KiCad
    does its own glyph layout), don't need a content dimension for this
    axis the way the viewer does — they use uc_anchor_shift_v.
    _UC_DESCENDER_U is descriptive only (how far descenders may ink below
    the box) and doesn't participate in this position math."""
    top = _UC_TOP_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    bottom = _UC_BOTTOM_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    return top, bottom


def uc_anchor_shift_u(hjust: str, size_u: float) -> float:
    """Local-frame horizontal anchor shift, in Ulticap units, so that a
    KiCad-style direct-anchor (justify hjust) — fed this shifted coordinate
    instead of the raw Ulticap anchor — renders at the same position
    Ulticap's own virtual-box model would put it (ULTICAP_TEXT_MODEL.md
    §2/§5/§7).

    Used by kiuc_writer.py for REFDES/VALUE/property placement. Unlike the
    viewer's hybrid-width fix, this needs no font metrics at all: KiCad
    performs its own glyph layout using whatever font the user has
    configured, so only the ANCHOR COORDINATE needs correcting, using
    Ulticap's fixed, font-independent margins alone. For 'center', content
    width cancels out of the box-model formula entirely (left_margin and
    right_margin are both independent of string length — see
    _uc_box_metrics), making this a pure per-size constant, not dependent
    on the actual text at all.

    Only the horizontal axis is handled here — uc_anchor_shift_v is the
    vertical counterpart."""
    left = _UC_LEFT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    right = _UC_RIGHT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    if hjust == 'left':
        return left
    elif hjust == 'right':
        return -right
    else:
        return (left - right) / 2.0


def uc_anchor_shift_v(vjust: str, size_u: float) -> float:
    """Local-frame vertical anchor shift (Ulticap units) — the vertical
    counterpart of uc_anchor_shift_u.

    Derivation mirrors the horizontal case exactly, but rests on one
    assumption that horizontal didn't need: that KiCad's own vertical
    justify keywords are self-referential the same structural way
    Ulticap's box model is — 'bottom' anchors KiCad's own rendered
    baseline, 'top' anchors KiCad's own rendered cap-height top, 'center'
    anchors the midpoint between them. Under that assumption, the box's
    content dimension (cap height) cancels out of all three formulas
    exactly the way content_width cancels for horizontal 'center' — so,
    like uc_anchor_shift_u, this needs no font metrics, only the fixed
    top/bottom margins.

    Measured in KiCad 9 (ULTICAP_TEXT_MODEL.md §7, "KiCad's vertical
    justify, measured"): KiCad aligns a box 1.17 × size tall, so the
    assumption holds for symbol fields to within about 0.1 mm -- too small
    to correct."""
    top = _UC_TOP_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    bottom = _UC_BOTTOM_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    if vjust == 'bottom':
        return bottom
    elif vjust == 'top':
        return -top
    else:
        return (bottom - top) / 2.0
