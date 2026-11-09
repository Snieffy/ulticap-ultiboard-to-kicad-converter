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
kiuc_ascii.py — Parser for Ulticap ASCII .SCH files

File format coverage: Ulticap SCH V1.50 through V5.72. See online documentation.

File structure:
    Header       *P / version line / bounds line / *R
    Body         *S … *C … *LV … *V … *A … *X …
    End          **
"""
from __future__ import annotations

import dataclasses
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from kiut.schematic.kiuc_model import (
    Sheet, Symbol, SymbolPin, SymbolAttribute, Circle,
    Component, ComponentAttribute, PinOverrideCorrection,
    Wire, UserLine, Junction, Annotation, Label,
    Polyline, convert_overline, DOS_GLYPHS,
)


# Every read and write of a .SCH/.BLK file's own text uses CP437: it maps all
# 256 byte values one-to-one, so a file that is read, edited and written back
# keeps every byte nobody changed -- in every corrected copy alike. The platform's default encoding cannot promise
# that (UTF-8 rejects most bytes above 0x7F; Windows-1252 has no character
# for 0x81, 0x8D, 0x8F, 0x90 and 0x9D -- CP437's ü, ì, Å, É and ¥).
SCH_ENCODING = 'cp437'


# ── public API ─────────────────────────────────────────────────────────────────

def looks_like_sch(path) -> bool:
    """Quick, cheap check for whether a file's own first two lines have the
    shape every genuine Ulticap ASCII .SCH/.BLK file starts with: a '*P '
    customer-name line, followed by a version line of at least two
    integers (major, minor) -- see _parse_header's own docstring for the
    full header shape this is checking the start of. Reads only a small
    prefix of the file, and is not a full parse: an existing, genuine SCH
    file with some other, unrelated problem still passes this and reports
    that problem normally through parse_sch's own errors list. This exists
    to catch a file that isn't Ulticap ASCII data at all -- most easily
    selected by mistake through a GUI file dialog's own "All files"
    fallback filter, which does not enforce the SCH/BLK extension filter
    it defaults to.
    """
    try:
        with open(path, 'rb') as f:
            prefix = f.read(512)
    except OSError:
        return False
    # cp437 maps every byte 0-255 to some character, so this never raises --
    # matching parse_sch's own decoding, and exactly why a structural check
    # here, not a decoding one, is what catches a wrong-format file.
    lines = prefix.decode(SCH_ENCODING).splitlines()
    if not lines or not lines[0].startswith('*P '):
        return False
    if len(lines) < 2:
        return False
    vparts = lines[1].split()
    if len(vparts) < 2:
        return False
    try:
        int(vparts[0])
        int(vparts[1])
    except ValueError:
        return False
    return True


def parse_sch(path) -> tuple:
    """Parse a single .SCH file and return (Sheet, errors).

    errors is a list of error strings (empty for clean files).
    """
    # Only the parsed copy gets the DOS font's glyphs; every rewrite of the
    # file itself reads the bytes again, so the source stays byte-identical.
    text = Path(path).read_text(encoding=SCH_ENCODING).translate(DOS_GLYPHS)
    lines = [l.rstrip('\r\n') for l in text.splitlines()]
    return _AsciiParser(lines, name=Path(path).name).parse()


def force_v5_header(path) -> tuple:
    """Rewrite the SCH version line in-place to mark the file as V5.00.

    Background
    ----------
    Ulticap SCH files have a two-field version line immediately after the
    *P <customer name> record:

        *P CAP
        4 60 00000000       ← this line: <major> <minor> <NRM>

    Files created by the DOS versions of Ulticap (V4.xx and below) encode
    arcs differently from files created by the Windows 95 version (V5.xx+).
    The converter uses the major version number to select the correct arc
    decoding path (is_less_than_v500).

    When a V5.xx file has been incorrectly identified as V4.xx — for example
    because the user saved it with "Save as old format" — the arc fix for
    V5.xx files will not be applied, producing mis-drawn arcs in KiCad.

    This function rewrites the version line to '5 00 00000000', forcing the
    converter to treat the file as V5.xx and apply the arc corrections.

    The NRM field (8-digit project identifier) is reset to '00000000'.
    All other lines in the file are unchanged.

    The file is modified in place: the converter runs it on the sheet's
    copy in the design's corrected folder (kiut.core.corrected), never on the
    original.

    Parameters
    ----------
    path : str or Path
        Path to the .SCH file to modify.

    Returns
    -------
    (True, old_line : str)
        Modification was successful; old_line is the original version line
        (stripped), useful for logging.
    (False, reason : str)
        Modification failed; reason describes the problem.
    """
    path = Path(path)
    try:
        # newline='' on read and write: the line endings stay as they are.
        # Without it, CRLF read back as LF and was written as the platform's
        # own ending -- LF on Linux, which lost every CR of a DOS file.
        with open(path, 'r', newline='', encoding=SCH_ENCODING) as f:
            text = f.read()
    except OSError as exc:
        return (False, f'Cannot read file: {exc}')

    lines = text.splitlines(keepends=True)
    found_p = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith('*P ') or stripped == '*P':
            found_p = True
            continue
        if found_p:
            # This is the version line — rewrite it.
            old_line = stripped
            # Preserve line ending of the original line.
            ending = '\r\n' if raw.endswith('\r\n') else '\n'
            lines[i] = '5 00 00000000' + ending
            try:
                # CP437, matching the read above -- this function only
                # intends to touch the version line and leave everything
                # else byte-for-byte unchanged, so the round-trip encoding
                # must be lossless for whatever's in the untouched lines
                # (extended DOS characters included). ascii+replace
                # previously corrupted those in place on every call.
                with open(path, 'w', newline='', encoding=SCH_ENCODING) as f:
                    f.write(''.join(lines))
            except OSError as exc:
                return (False, f'Cannot write file: {exc}')
            return (True, old_line)

    return (False, 'Version line not found (no *P record in file)')


# ── helpers ────────────────────────────────────────────────────────────────────

def _user_line_width(flag: int) -> int:
    """A sheet line's width value from its *LV / *LT user-layer thin/thick
    flag (0 thin, 1 thick): Ulticap DOS's fixed widths 6 and 30, the same two
    as symbol lines (see kiuc_writer's THIN_LINE_WIDTH). Confirmed with a DOS
    file drawn with thin and thick lines in every line type, and its V5.72
    export, which writes the same flags in *LV records."""
    return 30 if flag else 6


def _to_int(s, default=0):
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


def _attr_kv(tail):
    """Extract (tag, value) from '... TAG = value' tail string."""
    eq = tail.find('=')
    if eq < 0:
        return None
    tag = tail[:eq].strip()
    val = tail[eq + 1:].strip()
    if not tag:
        return None
    return tag, convert_overline(val)


def _parse_attr_line(line):
    """Parse a full attribute line.

    Format:
        <X rel> <Y rel> <X POE rel> <Y POE rel> <size> <color> <visibility> <tag>=<value>
    Returns (dx,dy,dx_poe,dy_poe,size,colour,vis,tag,val) or None.
    """
    parts = line.split(None, 7)
    if len(parts) < 8:
        return None
    try:
        dx     = int(parts[0])
        dy     = int(parts[1])
        dx_poe = int(parts[2])
        dy_poe = int(parts[3])
        size   = int(parts[4])
        colour = int(parts[5])
        vis    = int(parts[6])
    except ValueError:
        return None
    kv = _attr_kv(parts[7])
    if not kv:
        return None
    tag, val = kv
    return dx, dy, dx_poe, dy_poe, size, colour, vis, tag, val


_PLACEHOLDERS = frozenset(('-', '?', '*', '--', 'N/A', 'n/a', ''))

def _clean(v):
    if v is None:
        return None
    v = v.strip()
    return None if v in _PLACEHOLDERS else v


def _point_on_ortho_segment(px: int, py: int, x1: int, y1: int,
                            x2: int, y2: int) -> bool:
    """True if integer point (px,py) lies exactly on the axis-aligned segment
    (x1,y1)->(x2,y2). Diagonal segments always return False.

    Mirrors kiuc_writer._point_on_ortho_segment; kept as an independent
    copy here since this module must not import from kiuc_writer.
    """
    if x1 == x2:   # vertical
        return px == x1 and min(y1, y2) <= py <= max(y1, y2)
    elif y1 == y2: # horizontal
        return py == y1 and min(x1, x2) <= px <= max(x1, x2)
    return False


def _recover_bus_entry_flags(sheet: Sheet) -> None:
    """OR the parsed *V junction_type flag with a geometric bus-touch check.

    Ulticap always auto-places a junction the instant a wire-end (or a
    cluster of joined wires) lands on a bus -- there is no construct in
    Ulticap for a wire-junction that coincides with a bus point without
    that being a real connection, short of a corrupted or hand-edited
    file. So "this junction's point lies exactly on a bus segment" is, in
    valid Ulticap data, equivalent to "this is a wire-to-bus connection"
    -- the same fact the *V record's junction_type field is meant to
    encode for the same point.

    junction_type was found to come back as 0 for every single junction
    in files re-saved by Ulticap 5.72 (confirmed across a real, otherwise
    fully wire/bus-identical test pair: a V4.91 file has 50 of 99
    junctions correctly flagged junction_type=1, every one of which
    independently sits on a bus segment; the same design saved by V5.72
    has the literal *V token always 0 across all 99 junctions, while the
    wire/bus geometry itself, and the *V record's own net_number field,
    are both intact and decode correctly). That looks like the V5.72
    single-point *V export path simply never populates the field, not a
    parser misalignment: a misaligned read would also corrupt net_number,
    which it does not.

    Rather than special-case V5.72 (Ulticap's own version stamp in the
    header is not reliable, and the apparent "5 00" marker in some test
    files is this converter's own GUI-driven arc-fix flag from
    force_v5_header, not anything Ulticap itself writes), this recomputes
    is_bus_entry from geometry for every junction regardless of version
    and ORs it with whatever the file's *V record already said, so it is
    a strict no-op wherever the flag is already correct: in every test
    file checked, every flagged junction already sits on a bus segment
    and no unflagged junction does, so this OR changes nothing there
    while recovering the missing flag for V5.72-style exports.
    """
    bus_segs = [(w.x1, w.y1, w.x2, w.y2) for w in sheet.wires if w.is_bus]
    if not bus_segs:
        return
    for j in sheet.junctions:
        if j.is_bus_entry:
            continue
        if any(_point_on_ortho_segment(j.x, j.y, *s) for s in bus_segs):
            j.is_bus_entry = True


# ── parser ─────────────────────────────────────────────────────────────────────

class _AsciiParser:
    def __init__(self, lines, name=''):
        self.lines  = lines
        self.name   = name
        self.i      = 0
        self.errors: List[str] = []
        # Pin/POE misalignments found while parsing *S blocks. Reported
        # only once the whole sheet has been parsed (see
        # _report_misaligned_pins), because the placements that make the
        # message locatable -- which refdes use this symbol -- come from *C
        # records that follow the *S blocks in the file.
        self._misaligned: list = []
        # 1-based line number of each *S header, for the same messages.
        self._sym_lines: dict = {}

    # ── line helpers ──────────────────────────────────────────────────────────

    def _next(self):
        while self.i < len(self.lines):
            l = self.lines[self.i]
            self.i += 1
            if l.strip():
                return l
        return None

    def _consume_until_semicolon(self):
        """Accumulate lines until one ends with ';'; strip the ';' and join."""
        buf = []
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            self.i += 1
            if l.endswith(';'):
                buf.append(l[:-1])
                break
            buf.append(l)
        return ','.join(buf)

    # ── top-level ─────────────────────────────────────────────────────────────

    def parse(self):
        sheet = Sheet(name=self.name)
        self._parse_header(sheet)
        self._parse_body(sheet)
        _recover_bus_entry_flags(sheet)
        self._report_misaligned_pins(sheet)
        return sheet, self.errors

    def _report_misaligned_pins(self, sheet):
        """Turn the misalignments collected in _parse_symbol_attrs into
        error messages that say where to look: file, *S line, the pin
        itself, and every placement of the symbol on this sheet. The symbol
        name alone is not enough -- a symbol is a definition, and the thing
        a user actually finds and fixes in Ulticap is a placed part."""
        for sym_name, cp, poe, num_val, label in self._misaligned:
            pin_bits = []
            if num_val:
                pin_bits.append(f'#{num_val}')
            if label:
                pin_bits.append(f'label {label!r}')
            pin_desc = ' '.join(pin_bits) or 'unnumbered'
            placed = [c for c in sheet.components if c.symbol_name == sym_name]
            if not placed:
                where = 'not placed on this sheet'
            else:
                where = 'used by ' + ', '.join(
                    c.refdes if c.refdes else f'unannotated part at ({c.x},{c.y})'
                    for c in placed)
            line = self._sym_lines.get(sym_name)
            loc = f'{self.name} line {line}' if line else self.name
            # A symbol the sheet defines but doesn't place draws nothing and connects nothing: a
            # note (KIUT stage 10d); a placed one's pin really is used.
            level = 'NOTE' if not placed else 'ERROR'
            self.errors.append(
                f"{level}: {loc}: symbol {sym_name}: pin ({pin_desc}) connection "
                f"point {cp} doesn't align with a wire entry point (nearest "
                f"entry point {poe} used instead) -- {where}."
            )

    # ── header ────────────────────────────────────────────────────────────────

    def _parse_header(self, sheet):
        """*P <customer name>
           <major> <minor> [<NRM>]
           <x0>,<y0>,<x1>,<y1>,<id>;
           *R <root sheet name>
        """
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue

            if l.startswith('*P '):
                sheet.customer_name = l[3:].strip()
                self.i += 1

                # version line: <major> <minor> [<NRM>]
                vline = self._next() or ''
                vparts = vline.split()
                if len(vparts) >= 2:
                    sheet.major = _to_int(vparts[0], 4)
                    sheet.minor = _to_int(vparts[1], 60)
                    if len(vparts) >= 3:
                        sheet.nrm = vparts[2]
                    # V4.xx and below = DOS Ulticap; V5.xx+ = Windows Ulticap.
                    # Arc encoding differs between the two families.
                    sheet.is_less_than_v500 = (sheet.major < 5)

                # bounds line: <x0>,<y0>,<x1>,<y1>,<id>;
                bline = (self._next() or '').rstrip(';').strip()
                bparts = [p.strip() for p in bline.split(',')]
                if len(bparts) >= 4:
                    sheet.xmin = _to_int(bparts[0])
                    sheet.ymin = _to_int(bparts[1])
                    sheet.xmax = _to_int(bparts[2])
                    sheet.ymax = _to_int(bparts[3])
                    if len(bparts) >= 5:
                        sheet.grid = _to_int(bparts[4])  # grid spacing
                continue

            if l.startswith('*R '):
                sheet.root_sheet   = l[3:].strip()
                self.i += 1
                continue

            # Any other directive ends the header
            break

    # ── body ──────────────────────────────────────────────────────────────────

    def _parse_body(self, sheet):
        # is_less_than_v500 is already set from the header version number;
        # no *X scan needed here.
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue
            if l == '**':
                self.i += 1
                return  # ** marks end of schematic data; extended section not used
            if l.startswith('*S'):
                self._parse_symbol(sheet, l)
            elif l.startswith('*C '):
                self._parse_component(sheet, l)
            elif l.startswith('*LV '):
                self._parse_wire_lv(sheet, l)
            elif l.startswith('*LT '):
                self._parse_wire_lt(sheet, l)
            elif l.startswith('*V '):
                self._parse_junction(sheet, l)
            elif l.startswith('*A '):
                self._parse_annotation(sheet, l)
            elif l.startswith('*X '):
                self._parse_label(sheet, l)
            elif l.startswith('*R ') and not sheet.root_sheet:
                sheet.root_sheet   = l[3:].strip()
                self.i += 1
            else:
                self.i += 1

    # ── *S symbol ─────────────────────────────────────────────────────────────

    def _parse_symbol(self, sheet, header):
        """*S<name>
           <width> <height>
           <outline>;         section 1
           <pins>;            section 2
           <circles>;         section 3
           <attrs>
           ;                  end of symbol
        """
        sym_name = header[2:].strip()
        self._sym_lines[sym_name] = self.i + 1
        self.i += 1
        sym = Symbol(name=sym_name)

        wh = self._next() or ''
        parts = wh.split()
        if len(parts) >= 2:
            sym.width  = _to_int(parts[0])
            sym.height = _to_int(parts[1])

        sym.polylines = self._parse_outline()
        sym.pins      = self._parse_pins()
        sym.circles   = self._parse_circles()
        sym.sym_attrs, sym.attributes, sym.signal_pins = self._parse_symbol_attrs(
            pins=sym.pins, sym_width=sym.width, sym_height=sym.height,
            sym_name=sym_name)

        sheet.symbols[sym_name] = sym

    def _parse_outline(self):
        """Section 1: outline polylines, ends at ';'.

        The section is a flat comma-separated stream terminated by ';'.
        It contains one or more colour-groups; each group starts with the
        two-token header  1,<line_ctw>  followed by x,y coordinate pairs:

            1,<line_ctw>,x0,y0,...,xN,yN,1,<line_ctw>,x0,y0,...,xN,yN;

        <line_ctw> is a combined line_color + line_type + line_width value:
          line_color (bits 0-3):   0..15 palette index
          line_type  (bits 12-13): {0:solid,4096:dash,8192:dash-dot,12288:dot}
          line_width (bit 8):      {0:thin,256:thick} -- Ulticap DOS's two fixed
                                   widths, 6 and 30 Ulticap units (kept as the
                                   width value 6/30; see kiuc_writer's
                                   THIN_LINE_WIDTH)

        Because coordinates can equal 1, the group-start sentinel is identified
        positionally: a '1' that appears at an x-coordinate slot (i.e. right
        after the previous group's last y coordinate) starts a new group.
        The algorithm always consumes tokens in pairs after the header, so
        any '1' seen at an x-position is unambiguously a new group header.
        """
        polys = []
        raw = self._consume_until_semicolon()
        if not raw.strip():
            return polys

        tokens = [t.strip() for t in raw.split(',') if t.strip()]
        nums = []
        for t in tokens:
            try:
                nums.append(int(t))
            except ValueError:
                pass

        if len(nums) < 4:
            return polys

        def _decode_ctw(line_ctw):
            colour   = line_ctw & 0x000F
            linetype = line_ctw & 0x3000
            width    = 30 if (line_ctw & 0x0100) else 6
            return colour, linetype, width

        # Scan positionally: start by expecting the first '1' sentinel
        i = 0
        colour, linetype, width = 0, 0, 6
        points = []
        expecting_header = True

        while i < len(nums):
            if expecting_header:
                # Skip anything before the first '1'
                if nums[i] != 1:
                    i += 1
                    continue
                if i + 1 >= len(nums):
                    break
                if points:
                    polys.append(Polyline(points=points, colour=colour,
                                          linetype=linetype, width=width))
                colour, linetype, width = _decode_ctw(nums[i + 1])
                points = []
                i += 2
                expecting_header = False
            else:
                # At an x-position: a '1' here starts a new group
                if nums[i] == 1 and i + 1 < len(nums):
                    if points:
                        polys.append(Polyline(points=points, colour=colour,
                                              linetype=linetype, width=width))
                    colour, linetype, width = _decode_ctw(nums[i + 1])
                    points = []
                    i += 2
                elif i + 1 < len(nums):
                    points.append((nums[i], nums[i + 1]))
                    i += 2
                else:
                    break

        if points:
            polys.append(Polyline(points=points, colour=colour,
                                  linetype=linetype, width=width))
        return polys

    def _parse_pins(self):
        """Section 2: pins, ends at ';'.
        Format: <pin_format>,<pin_rotation>,<rel_x>,<rel_y>,
        pin_format   = {0:none,1:INVERTED,2:CLOCK,...,10:CLOCK INVERTED}
        pin_rotation = {0:E(right),1:N(up),2:W(left),3:S(down)}
        """
        pins = []
        raw = self._consume_until_semicolon()
        if not raw.strip():
            return pins

        tokens = [t.strip() for t in raw.split(',') if t.strip()]
        nums = []
        for t in tokens:
            try:
                nums.append(int(t))
            except ValueError:
                pass

        i = 0
        while i + 3 < len(nums):
            pin_format   = nums[i]
            pin_rotation = nums[i + 1]
            x            = nums[i + 2]
            y            = nums[i + 3]
            pins.append(SymbolPin(x=x, y=y,
                                  pin_format=pin_format,
                                  pin_rotation=pin_rotation))
            i += 4
        return pins

    def _parse_circles(self):
        """Section 3: circles, ends at ';'.
        Format per circle: <cx>,<cy>,<r>,<rotate>,<angle>,<colour>,<thick>,<arc_linetype>,
          thick:         0/1 flag {0:thin,1:thick} -> width value 6/30 (Ulticap
                         DOS's fixed widths, in Ulticap units)
          arc_linetype:  {0:solid,1:dash,2:dash-dot,3:dot}
        """
        circles = []
        raw = self._consume_until_semicolon()
        if not raw.strip():
            return circles

        tokens = [t.strip() for t in raw.split(',') if t.strip()]
        nums = []
        for t in tokens:
            try:
                nums.append(int(t))
            except ValueError:
                pass

        i = 0
        while i + 7 < len(nums):
            cx           = nums[i]
            cy           = nums[i + 1]
            r            = nums[i + 2]
            rotate       = nums[i + 3]
            angle        = nums[i + 4]
            colour       = nums[i + 5]
            thick_flag   = nums[i + 6]
            arc_linetype = nums[i + 7]
            thick = 30 if thick_flag else 6   # 0/1 flag → thin/thick width value
            circles.append(Circle(cx=cx, cy=cy, r=r,
                                  rotate=rotate, angle=angle,
                                  colour=colour, thick=thick,
                                  arc_linetype=arc_linetype))
            i += 8
        return circles

    def _parse_symbol_attrs(self, pins=None, sym_width=0, sym_height=0, sym_name=''):
        """Section 4: attribute definitions, ends at lone ';'.

        Groups PINTYPE / # / LABEL records by their shared (dx_poe, dy_poe)
        coordinates and stamps the resolved values directly onto the matching
        SymbolPin.  This is order-independent: # before PINTYPE works fine.

        Matching uses the pin connection point (the wire-attach endpoint on the
        bbox face) rather than the pin body position.  Connection points are
        exact integers that coincide with the POE coords of the paired # / LABEL /
        PINTYPE attr lines, so the match is always exact rather than nearest.
        The nearest-distance fallback is retained for symbols where the POE does
        not land exactly on the connection point.

        TRANSX / TRANSY tags are silently discarded — redundant editor artefacts.
        SIGNAL tags are parsed into signal_pins and excluded from sym_attrs.
        """
        if pins is None:
            pins = []

        def _conn_pt(pin):
            """Pin connection point in *S local coords."""
            r = pin.pin_rotation & 3
            if r == 0: return (sym_width,  pin.y)
            if r == 1: return (pin.x,      sym_height)
            if r == 2: return (0,           pin.y)
            return          (pin.x,      0)

        sym_attrs = []
        attr_dict = {}

        # Collect all attr lines first
        raw_attrs = []
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue
            if l == ';':
                self.i += 1
                break
            if l.startswith('*'):
                break
            self.i += 1
            parsed = _parse_attr_line(l)
            if parsed:
                raw_attrs.append(parsed)

        # Build sym_attrs, POE groups, and signal_pins
        from collections import defaultdict
        poe_groups: dict = defaultdict(dict)
        signal_pins = []

        for dx, dy, dx_poe, dy_poe, size, colour, vis, tag, val in raw_attrs:
            if tag in ('TRANSX', 'TRANSY'):
                continue  # discard — redundant, see docstring
            if tag == 'SIGNAL':
                # SIGNAL=VCC,14  or  SIGNAL=GND,1,21
                parts = [p.strip() for p in val.split(',')]
                if len(parts) >= 2:
                    signal_pins.append((parts[0], parts[1:]))
                continue  # metadata only, not a display attribute
            sym_attrs.append(SymbolAttribute(
                tag=tag, default_value=val,
                dx=dx, dy=dy, dx_poe=dx_poe, dy_poe=dy_poe,
                size=size, colour=colour, visibility=vis,
            ))
            attr_dict[tag] = val
            if tag in ('PINTYPE', 'LABEL', 'WIRELABEL') or tag.startswith('#'):
                poe_groups[(dx_poe, dy_poe)][tag] = val

        # Stamp PINTYPE / # / LABEL onto matching SymbolPin.
        #
        # Strategy: try exact match on connection point first (conn_pt == POE key).
        # Connection points are the wire-attach endpoints on the bbox face, which
        # always coincide exactly with the POE coordinates of the paired attr lines
        # in well-formed Ulticap symbols.  Using connection points instead of pin
        # body positions avoids tie-breaking ambiguity when two pins on opposite
        # sides of the body are equidistant from two POE groups (e.g. SHE14_2X3).
        #
        # For symbols where the POE does not land on the connection point (manual
        # placement), fall back to the nearest-distance heuristic.
        if pins and poe_groups:
            for pin in pins:
                cp = _conn_pt(pin)
                if cp in poe_groups:
                    best_key = cp
                else:
                    # Fallback: nearest POE by Euclidean distance.
                    # This only occurs when the POE coordinate in the *S attr
                    # does not coincide with any pin connection point — indicating
                    # a manually edited or corrupt symbol definition.
                    best_key = min(poe_groups,
                                   key=lambda p: (pin.x - p[0])**2 + (pin.y - p[1])**2)
                    best = poe_groups[best_key]
                    num_val = best.get('#') or next(
                        (v for k, v in best.items() if k.startswith('#')), '')
                    self._misaligned.append((sym_name, cp, best_key, num_val,
                                             best.get('LABEL') or best.get('WIRELABEL')))
                group = poe_groups[best_key]
                if 'PINTYPE' in group:
                    pin.pin_type = group['PINTYPE']
                num_val = group.get('#')
                if num_val is None:
                    for k, v in group.items():
                        if k.startswith('#'):
                            num_val = v
                            break
                if num_val is not None:
                    pin.number = num_val
                # LABEL takes priority; WIRELABEL is fallback for PWR pins
                if 'LABEL' in group:
                    pin.name = group['LABEL']
                elif 'WIRELABEL' in group:
                    pin.name = group['WIRELABEL']

        return sym_attrs, attr_dict, signal_pins

    # ── *C component ─────────────────────────────────────────────────────────

    def _parse_component(self, sheet, header):
        """*C <X abs> <Y abs> <rotation> <symbol name>
           <attr line> …
           ;
        """
        parts = header.split(None, 4)
        if len(parts) < 5:
            self.i += 1
            return
        x        = _to_int(parts[1])
        y        = _to_int(parts[2])
        rot      = _to_int(parts[3])
        sym_name = parts[4].strip()
        self.i += 1

        comp = Component(x=x, y=y, rotation=rot, symbol_name=sym_name)

        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue
            if l == ';':
                self.i += 1
                break
            if l.startswith('*'):
                break
            self.i += 1
            parsed = _parse_attr_line(l)
            if parsed:
                dx, dy, dx_poe, dy_poe, size, colour, vis, tag, val = parsed
                comp.comp_attrs.append(ComponentAttribute(
                    tag=tag, value=val,
                    dx=dx, dy=dy, dx_poe=dx_poe, dy_poe=dy_poe,
                    size=size, colour=colour, visibility=vis,
                ))
                comp.attributes[tag] = val

        a = comp.attributes
        comp.refdes    = _clean(a.get('REFDES'))
        comp.device    = _clean(a.get('DEVICE'))
        comp.value     = _clean(a.get('VALUE'))
        comp.pkg_type  = _clean(a.get('PKG_TYPE'))
        comp.wirelabel = _clean(a.get('WIRELABEL'))
        _fr = _clean(a.get('FILE'))
        # If *C has no FILE=, check the *S symbol definition (FILE= lives there
        # when the subsheet box has no per-instance attribute override).
        if _fr is None and comp.symbol_name in sheet.symbols:
            _sym_fr = _clean(sheet.symbols[comp.symbol_name].attributes.get('FILE'))
            if _sym_fr is not None:
                _fr = _sym_fr
        # Normalise: if FILE= has no extension, append .SCH (e.g. SHEET1 → SHEET1.SCH)
        if _fr is not None and '.' not in Path(_fr).name:
            _fr = _fr + '.SCH'
        comp.file_ref  = _fr
        comp.sheet_name = sheet.name

        sheet.components.append(comp)

    # ── *LV wire ─────────────────────────────────────────────────────────────

    def _parse_wire_lv(self, sheet, line):
        """*LV <level> <startx> <starty> <endx> <endy> <l1> <l2> <l3>

        level=1: netlist layer  l1=net_number  l2={0:wire,1:bus}
        level=2: user layer     l1=linetype+colour  l2={0:thin,1:thick}
        other  : log warning
        """
        parts = line.split()
        if len(parts) < 6:
            self.i += 1
            return

        level  = _to_int(parts[1])
        startx = _to_int(parts[2])
        starty = _to_int(parts[3])
        endx   = _to_int(parts[4])
        endy   = _to_int(parts[5])
        l1     = _to_int(parts[6]) if len(parts) > 6 else 0
        l2     = _to_int(parts[7]) if len(parts) > 7 else 0

        if level == 1:
            sheet.wires.append(Wire(
                x1=startx, y1=starty, x2=endx, y2=endy,
                net_id=l1, is_bus=(l2 == 1),
            ))
        elif level == 2:
            linetype  = l1 & 0xFF00
            colour    = l1 % 16
            sheet.user_lines.append(UserLine(
                x1=startx, y1=starty, x2=endx, y2=endy,
                linetype=linetype, colour=colour,
                thickness=_user_line_width(l2),
            ))
        else:
            # Into the error list, not print(): the GUI only shows what
            # comes back through parse()'s errors, and a bare print lands
            # on a console the GUI user usually doesn't have.
            self.errors.append(f"WARNING: {self.name} line {self.i + 1}: "
                               f"unknown *LV level {level}: {line.strip()}")

        self.i += 1

    # ── *LT wire ──────────────────────────────────────────────────────────────

    def _parse_wire_lt(self, sheet, header):
        """*LT <level> <start0>
        <start1> <end1> <l1> <l2> <l3> ;

        start0  : fixed/anchor coordinate
        start1  : start of varying coordinate
        end1    : end of varying coordinate
        l1      : net number (level 0/1)  OR  colour (level 2)
        l2      : {0:wire, 1:bus} (level 0/1)  OR  {0:thin, 1:thick} (level 2)
        l3      : orientation {4:horizontal, 5:vertical, 6:diagonal NE/SW, 7:diagonal SE/NW}

        Multiple segments share the same header and may appear on separate data lines
        before the closing ';'.

        Orientation → coordinates (verified against LTV1_colors.SCH colour test):
          4 horizontal:        y fixed = start0;  x: start1→end1
          5 vertical:          x fixed = start0;  y: start1→end1
          6 diagonal NE/SW (slope +1): x1=(start0+start1)/2, y1=(start1-start0)/2,
                                        x2=(start0+end1)/2,   y2=(end1-start0)/2
          7 diagonal SE/NW (slope -1): x1=(start0+start1)/2, y1=(start0-start1)/2,
                                        x2=(start0+end1)/2,   y2=(start0-end1)/2
        """
        parts = header.split()
        if len(parts) < 3:
            self.i += 1
            return
        level  = _to_int(parts[1])
        start0 = _to_int(parts[2])
        self.i += 1

        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            done = l.endswith(';')
            if done:
                l = l[:-1].strip()
            self.i += 1

            if not l or l.startswith('*'):
                if l.startswith('*'):
                    self.i -= 1
                break

            seg_parts = l.split()
            if len(seg_parts) >= 5:
                start1 = _to_int(seg_parts[0])
                end1   = _to_int(seg_parts[1])
                l1     = _to_int(seg_parts[2])
                l2     = _to_int(seg_parts[3])
                l3     = _to_int(seg_parts[4])   # orientation

                # Compute (x1,y1) -> (x2,y2) from start0/start1/end1 + orientation
                if l3 == 4:    # HORIZONTAL: y=start0 fixed, x: start1→end1
                    x1, y1, x2, y2 = start1, start0, end1, start0
                elif l3 == 5:  # VERTICAL: x=start0 fixed, y: start1→end1
                    x1, y1, x2, y2 = start0, start1, start0, end1
                elif l3 == 6:  # DIAGONAL NE/SW: x=(start0+start)/2, y=(start-start0)/2
                    x1 = (start0 + start1) // 2
                    y1 = (start1 - start0) // 2
                    x2 = (start0 + end1)   // 2
                    y2 = (end1   - start0) // 2
                elif l3 == 7:  # DIAGONAL SE/NW: x=(start0+start)/2, y=(start0-start)/2
                    x1 = (start0 + start1) // 2
                    y1 = (start0 - start1) // 2
                    x2 = (start0 + end1)   // 2
                    y2 = (start0 - end1)   // 2
                else:
                    if done:
                        break
                    continue

                if level in (0, 1):
                    sheet.wires.append(Wire(
                        x1=x1, y1=y1, x2=x2, y2=y2,
                        net_id=l1, is_bus=(l2 == 1),
                    ))
                elif level == 2:
                    # l1 = linetype + colour (same encoding as *LV level 2):
                    #   high byte: {0:solid,256:dashed,512:dash-dot,768:dot}
                    #   low byte:  colour index
                    # l2 = {0:thin, 1:thick}
                    colour    = l1 & 0x00FF
                    linetype  = l1 & 0xFF00
                    sheet.user_lines.append(UserLine(
                        x1=x1, y1=y1, x2=x2, y2=y2,
                        linetype=linetype, colour=colour,
                        thickness=_user_line_width(l2),
                    ))

            if done:
                break

    # ── *V junction ──────────────────────────────────────────────────────────

    def _parse_junction(self, sheet, header):
        """*V <X common>
           <Y> <net_number> <junction_type> <type>;

        junction_type: 0 = wire-to-wire (or bus-to-bus) junction,
                       1 = wire-to-bus connection (bus entry).

        Old format groups multiple Y values under one X, one group per line.
        _consume_until_semicolon joins lines with ',' so replace commas
        with spaces before tokenising.
        """
        x = _to_int(header.split()[1])
        self.i += 1

        raw = self._consume_until_semicolon()
        tokens = raw.replace(',', ' ').split()

        i = 0
        while i < len(tokens):
            try:
                y = int(tokens[i])
            except ValueError:
                i += 1
                continue
            junction_type = int(tokens[i + 2]) if i + 2 < len(tokens) else 0
            sheet.junctions.append(Junction(x=x, y=y,
                                            is_bus_entry=(junction_type != 0)))
            i += 4   # Y + net_number + junction_type + type

    # ── *A annotation ─────────────────────────────────────────────────────────

    def _parse_annotation(self, sheet, line):
        """*A <X abs> <Y abs> <X POE abs> <Y POE abs> <size> <color> <visibility> <tag>=<value>
        Single line, no trailing ';'.
        """
        parts = line.split(None, 8)
        if len(parts) < 9:
            self.i += 1
            return
        try:
            x      = int(parts[1]); y      = int(parts[2])
            x_poe  = int(parts[3]); y_poe  = int(parts[4])
            size   = int(parts[5]); colour = int(parts[6])
            vis    = int(parts[7])
        except (ValueError, IndexError):
            self.i += 1
            return

        kv = _attr_kv(parts[8])
        if kv:
            tag, val = kv
        else:
            tag, val = 'LABEL', convert_overline(parts[8])

        sheet.annotations.append(Annotation(
            x=x, y=y, x_poe=x_poe, y_poe=y_poe,
            text=val, size=size, colour=colour, visibility=vis, tag=tag,
        ))
        self.i += 1

    # ── *X label ─────────────────────────────────────────────────────────────

    def _parse_label(self, sheet, line):
        """*X <x> <y> <size> <colour> <rotation-t> <align-t> <text>

        rotation-t: 1/64-degree units; 0, 5760 (90°), 11520 (180°) and
                    17280 (270°); any other value is treated as 0.
        align-t:    0-8 {0:BL,1:BC,2:BR,3:CL,4:CC,5:CR,6:TL,7:TC,8:TR};
                    always visible.
        """
        parts = line.split(None, 7)
        if len(parts) < 8:
            self.i += 1
            return
        try:
            x        = int(parts[1]); y      = int(parts[2])
            size     = int(parts[3]); colour = int(parts[4])
            rotation = int(parts[5])
            align    = int(parts[6]) if parts[6].lstrip('-').isdigit() else 0
            text     = convert_overline(parts[7])
        except (ValueError, IndexError):
            self.i += 1
            return

        # Ulticap DOS rotates a free text in 90° steps and draws 180° and 270°
        # upside down (a 180° text reads right to left, hanging from its
        # anchor), so all four are kept; anything else is not a DOS value.
        rotation = rotation if rotation in (0, 5760, 11520, 17280) else 0
        # Clamp align to valid range 0-8
        align = max(0, min(8, align))

        sheet.labels.append(Label(x=x, y=y, text=text,
                                  size=size, colour=colour,
                                  rotation=rotation, align=align))
        self.i += 1


# ── hierarchical design discovery ─────────────────────────────────────────────
#
# Shared by the viewer (auto-populated "Sheets" tree) and the converter
# (its input list found from a single selected main sheet). Single-sourced here since both consumers already import parse_sch
# from this module.

def natural_sort_key(text: str):
    """Splits a label into alternating text/number chunks so 'U2' sorts
    before 'U10' instead of after it (plain string sort would put '1'
    ahead of '2' character-by-character, e.g. U10 < U2)."""
    return [int(chunk) if chunk.isdigit() else chunk.lower()
            for chunk in re.split(r'(\d+)', text)]


def load_sheet(path, errors: Optional[list] = None) -> Optional[Sheet]:
    """Parse one .SCH/.BLK file, or None if it doesn't exist or isn't one.

    Parse errors go into `errors` when a list is given -- the GUI passes
    one so they reach its log window instead of a console it usually
    doesn't have. With the default None they are printed to stderr as
    before, which is what the viewer (no log window of its own) relies on.
    """
    path = Path(path)
    if not path.exists() or path.suffix.upper() not in ('.SCH', '.BLK'):
        return None
    sheet, parse_errors = parse_sch(path)
    if errors is not None:
        errors.extend(parse_errors)
    else:
        for e in parse_errors:
            print(e, file=sys.stderr)
    return sheet


def resolve_file_ref(ref: str, folder: Path) -> Optional[Path]:
    """Resolve one component's FILE_REF string to an actual file on disk,
    against the folder its parent sheet lives in. Handles the '.SCH'
    suffix being implicit and Ulticap sometimes storing the reference in
    the wrong case (falls back to a case-insensitive folder scan). Returns
    None if nothing on disk matches. Shared by hierarchy_children (builds
    the whole Sheets-panel tree) and the canvas's double-click-to-enter-
    subsheet handler (resolves a single clicked component)."""
    name = ref if ref.upper().endswith(('.SCH', '.BLK')) else ref + '.SCH'
    candidate = folder / name
    if candidate.exists():
        return candidate
    match = next((p for p in folder.iterdir()
      if p.suffix.upper() in ('.SCH', '.BLK')
      and p.name.upper() == name.upper()), None)
    return match


def hierarchy_children(sheet: Sheet, folder: Path) -> List[Tuple[str, Path]]:
    """Sub-sheets referenced by the currently loaded sheet (its components'
    FILE_REF attribute — the same field kiuc_writer.py follows to write
    sub-sheets recursively), resolved against the folder the parent file
    lives in. Returns (display_label, resolved_path) pairs for files that
    actually exist on disk, naturally sorted by refdes (U2 before U10) with
    duplicates removed."""
    seen = set()
    out: List[Tuple[str, Path]] = []
    for comp in sheet.components:
        ref = getattr(comp, 'file_ref', None)
        if not ref:
            continue
        candidate = resolve_file_ref(ref, folder)
        if candidate is None:
            continue
        key = candidate.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append((candidate.stem, candidate))
    out.sort(key=lambda entry: natural_sort_key(entry[0]))
    return out


class HierNode:
    """One node of the sub-sheet hierarchy tree."""
    __slots__ = ('label', 'path', 'children')
    def __init__(self, label: str, path: Path, children: List['HierNode']):
        self.label = label
        self.path = path
        self.children = children


def build_hierarchy_tree(root_path: Path, max_depth: int = 12,
                         errors: Optional[list] = None) -> Optional[HierNode]:
    """Recursively resolve the full sub-sheet hierarchy starting at
    root_path (parses every descendant file, not just the next level —
    needed to show grandchildren etc. all at once instead of one level
    at a time). Guards against two failure modes a hand-edited Ulticap
    hierarchy could in principle hit:
      - a cycle (a sheet that, directly or via descendants, references one
        of its own ancestors) — detected per-branch, not globally, so two
        unrelated sub-sheets are still free to reference the same file;
      - runaway depth, as a backstop in case cycle detection ever misses
        an unusual reference pattern.
    Returns None if root_path itself can't be read. `errors` is passed
    straight to load_sheet for every file parsed -- see there."""
    root_path = Path(root_path)
    if not root_path.exists():
        return None

    def walk(path: Path, ancestors: frozenset, depth: int, label: str) -> HierNode:
        node = HierNode(label, path, [])
        key = path.resolve()
        if key in ancestors or depth >= max_depth:
            return node   # cycle or depth guard — leave as a leaf, don't recurse further
        sheet = load_sheet(path, errors)
        if sheet:
            branch = ancestors | {key}
            for child_label, child_path in hierarchy_children(sheet, path.parent):
                node.children.append(walk(child_path, branch, depth + 1, child_label))
        return node

    return walk(root_path, frozenset(), 0, root_path.stem)


def collect_hierarchy_paths(root_path: Path,
                            errors: Optional[list] = None) -> List[Path]:
    """Return every unique .SCH/.BLK path in the hierarchy rooted at root_path,
    naturally sorted, starting with the root itself. Used by batch export and
    by the converter's input list. `errors`: see load_sheet."""
    tree = build_hierarchy_tree(root_path, errors=errors)
    if not tree:
        return [Path(root_path)]
    seen: set = set()
    paths: List[Path] = []
    def walk(node: HierNode):
        key = node.path.resolve()
        if key in seen:
            return
        seen.add(key)
        paths.append(node.path)
        for child in node.children:
            walk(child)
    walk(tree)
    return paths


# ── record integrity (missing ',\n' between two records) ────────────────────
#
# A known Ulticap 5.72 export defect: within a *S block's polylines/pins/
# circles section, one physical line is occasionally missing its trailing
# ',\n' -- both the comma AND the newline together, not just one -- so what
# should have been two separate lines end up glued onto a single physical
# line with no separator between them at all. The last field of the first
# record and the first field of the second collapse into one merged digit
# string (e.g. "0" + "150" -> "0150"), which Python's own int() parses
# without error (leading zeros are not a syntax error), so this corrupts
# data completely silently -- verified directly against two independent
# real-world instances: every field after the merge point in that section
# shifts by one position, and the section's true final record is dropped
# entirely for running out of numbers to complete it.
#
# Detection is per-line and purely mechanical, validated against the whole
# of this session's test corpus with zero false positives: pins and circles
# sections both always contain a whole number of fixed-size records on any
# well-formed line (4 fields per pin, 8 per circle) -- Ulticap never splits
# a single record across two lines -- so a line whose comma-split token
# count is exactly one short of a clean multiple (2*record_size - 1) means
# exactly one merge occurred, and it is always at index (record_size - 1):
# the last field of the first of the two combined records. Polylines are
# deliberately NOT covered here: each group is a header plus a *variable*
# number of point pairs (no fixed record size for a whole line to be a
# multiple of), so the same simple per-line check does not apply, and no
# polyline corruption instance has actually been observed to justify a
# more complex, unvalidated mechanism.
_RECORD_SECTIONS = (('pins', 4), ('circles', 8))


@dataclass(eq=False)
class RecordIntegrityIssue:
    """One detected 'missing ,\\n between two records' corruption: a single
    physical line, within one *S block's pins or circles section, whose
    comma-split token count is exactly one short of a whole number of
    fixed-size records. merge_token_index is always (record_size - 1) --
    see the module-level comment above for the full derivation.
    """
    sch_path: Path
    line_no: int              # 1-based line number in the file
    symbol_name: str
    section: str               # 'pins' | 'circles'
    record_size: int
    line_text: str              # the raw corrupted line, CRLF stripped
    merge_token_index: int      # 0-indexed position of the glued token
    merged_token: str            # the raw glued digit string, e.g. "0150"

    @property
    def candidates(self) -> List[Tuple[str, str]]:
        """Every plausible (fieldA, fieldB) split of the glued token,
        filtered to exclude a leading zero on either side -- real field
        values in this format are never observed with one, so this is a
        strong filter, not just a guess (verified: it resolves both known
        real-world instances to exactly one candidate each). A leading
        '-' sign always belongs to fieldA; the merge point is never
        inside a negative number's own digits, since the sign itself
        would have had to go missing too -- a different, unobserved
        failure mode.
        """
        tok = self.merged_token
        sign, digits = ('-', tok[1:]) if tok.startswith('-') else ('', tok)
        out = []
        for split_at in range(1, len(digits)):
            a, b = digits[:split_at], digits[split_at:]
            if len(a) > 1 and a[0] == '0':
                continue
            if len(b) > 1 and b[0] == '0':
                continue
            out.append((sign + a, b))
        if not out:
            # Filter eliminated every split (e.g. an all-zero merged
            # token) -- fall back to every raw split so the user always
            # has something to choose from rather than a dead end.
            out = [(sign + digits[:i], digits[i:]) for i in range(1, len(digits))]
        return out

    @property
    def is_unambiguous(self) -> bool:
        return len(self.candidates) == 1


def scan_record_integrity(path) -> List[RecordIntegrityIssue]:
    """Scan one .SCH file for the 'missing ,\\n between two records'
    corruption (see module comment and RecordIntegrityIssue) across every
    *S block's pins and circles sections. Pure read-only inspection --
    never modifies the file.
    """
    path = Path(path)
    raw_lines = path.read_text(encoding=SCH_ENCODING).splitlines()
    issues: List[RecordIntegrityIssue] = []

    i = 0
    n = len(raw_lines)
    while i < n:
        line = raw_lines[i]
        if not line.startswith('*S'):
            i += 1
            continue
        sym_name = line[2:].strip()
        i += 1
        if i < n:
            i += 1   # width/height line

        # Section 1 (polylines) is skipped over, not scanned -- see the
        # module comment for why. Section 2 (pins) and section 3
        # (circles) are scanned in order.
        section_order = [None] + list(_RECORD_SECTIONS)   # None = polylines, skip
        for entry in section_order:
            while i < n:
                cur = raw_lines[i].strip()
                terminated = cur.endswith(';')
                body = cur[:-1] if terminated else cur
                tokens = [t.strip() for t in body.split(',') if t.strip()]
                if entry is not None:
                    section, record_size = entry
                    if len(tokens) == 2 * record_size - 1:
                        merge_idx = record_size - 1
                        issues.append(RecordIntegrityIssue(
                            sch_path=path, line_no=i + 1, symbol_name=sym_name,
                            section=section, record_size=record_size,
                            line_text=raw_lines[i].rstrip('\r\n'),
                            merge_token_index=merge_idx,
                            merged_token=tokens[merge_idx]))
                i += 1
                if terminated:
                    break
            else:
                break   # ran off the end of the file without a ';'

    return issues


def split_corrupted_line(line_text: str, merge_token_index: int,
                         split: Tuple[str, str]) -> Tuple[str, str]:
    """Given one corrupted line's raw text (as RecordIntegrityIssue.line_text
    -- CRLF already stripped) and a chosen (fieldA, fieldB) split of its
    merged token, return the two correctly-terminated replacement lines
    (no CRLF appended; the caller adds whatever line ending it's using).
    Pure text transform, no file I/O -- shared by apply_record_integrity_fix
    (writes to disk) and the record-integrity dialog's preview, so the two
    can never disagree about what a resolved issue actually looks like.
    """
    field_a, field_b = split
    stripped = line_text.rstrip()
    terminated = stripped.endswith(';')
    body = stripped[:-1] if terminated else stripped
    tokens = [t.strip() for t in body.split(',') if t.strip()]

    line_a_tokens = tokens[:merge_token_index] + [field_a]
    line_b_tokens = [field_b] + tokens[merge_token_index + 1:]
    new_line_a = ','.join(line_a_tokens) + ','
    new_line_b = ','.join(line_b_tokens) + (';' if terminated else ',')
    return new_line_a, new_line_b


def apply_record_integrity_fix(issue: RecordIntegrityIssue,
                               split: Tuple[str, str]) -> None:
    """Apply one chosen (fieldA, fieldB) split to issue.sch_path in place:
    replace the corrupted line with two correctly-terminated lines (the
    missing ',\\n' restored at the merge point), preserving the file's own
    CRLF/LF convention and leaving every other line byte-for-byte
    unchanged.

    Re-derives the merged token from the file's current content and
    raises if it no longer matches issue.merged_token -- a caller applying
    several issues in the same file must not apply them out of the order
    documented by apply_record_integrity_fixes (descending line number),
    since inserting a line shifts every later line number by one.
    """
    path = issue.sch_path

    with open(path, 'r', newline='', encoding=SCH_ENCODING) as f:
        text = f.read()

    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.split(newline)

    old_line = lines[issue.line_no - 1]
    stripped = old_line.rstrip()
    terminated = stripped.endswith(';')
    body = stripped[:-1] if terminated else stripped
    tokens = [t.strip() for t in body.split(',') if t.strip()]

    if (issue.merge_token_index >= len(tokens)
            or tokens[issue.merge_token_index] != issue.merged_token):
        found = (tokens[issue.merge_token_index]
                if issue.merge_token_index < len(tokens) else '<missing>')
        raise ValueError(
            f'{path}:{issue.line_no}: expected merged token '
            f'{issue.merged_token!r}, found {found!r} -- line numbers may '
            f'have shifted; fixes in one file must be applied in '
            f'descending line-number order (see apply_record_integrity_fixes).')

    new_line_a, new_line_b = split_corrupted_line(old_line, issue.merge_token_index, split)
    lines[issue.line_no - 1:issue.line_no] = [new_line_a, new_line_b]

    with open(path, 'w', newline='', encoding=SCH_ENCODING) as f:
        f.write(newline.join(lines))


def apply_record_integrity_fixes(
        resolutions: List[Tuple[RecordIntegrityIssue, Tuple[str, str]]],
        path_map: Optional[dict] = None) -> None:
    """Apply every (issue, chosen_split) pair. Groups by file and, within
    each file, applies in descending line-number order so inserting a new
    line for one issue never shifts another pending issue's line_no in
    the same file out from under it. path_map moves each issue to another
    file of the same content first -- the sheet scanned (an original) to its
    corrected copy (kiut.core.corrected), so the original is never written.
    """
    by_file: dict = {}
    for issue, split in resolutions:
        if path_map and issue.sch_path in path_map:
            issue = dataclasses.replace(issue, sch_path=path_map[issue.sch_path])
        by_file.setdefault(issue.sch_path, []).append((issue, split))
    for _path, items in by_file.items():
        items.sort(key=lambda pair: pair[0].line_no, reverse=True)
        for issue, split in items:
            apply_record_integrity_fix(issue, split)


# ── space-in-symbol-name sanitisation (V4.91 compatibility) ─────────────────
#
# A space in a *S<symbol name> or *C <x> <y> <rot> <symbol name> causes
# Ulticap V4.91 to fail opening the file with a "disk read error" -- our own
# parser and KiCad's own output both handle the space fine (verified
# earlier), so this is purely a V4.91-openability concern. It is one of the
# corrections of the design's corrected folder (kiut.core.corrected), applied
# before the conversion, so the corrected design opens in every Ulticap
# version and the KiCad symbol names follow it (spaces become underscores).
# A sibling DDF is unaffected: it holds no schematic symbol name -- its *C
# line is designator, /VALUE (upper-cased, spaces as underscores, at most 19
# characters) and PKG_TYPE, checked against a real design's DDF and sheets
# (441 of 445 designators; the four others are board/schematic differences
# of that design, none a symbol name). Scoped to exactly the two fields the
# real V4.91 failure was traced to (*S and *C); DEVICE= attribute lines
# also often repeat the same name but are purely cosmetic display text,
# never used for symbol matching by any parser in this codebase (see
# kiuc_ascii.py's own _parse_component -- comp.symbol_name comes only from
# the *C line's own trailing field), so they're deliberately left alone.

def find_space_symbol_names(path) -> dict:
    """Return {original_name: sanitised_name} for every *S/*C symbol name
    in this one file that contains a space (spaces replaced with
    underscores). Symbol names are file-scoped in this format -- a *S
    definition is only ever matched by *C placements in that same file --
    so this never needs to look outside a single file. Returns an empty
    dict if nothing needs changing. Read-only; never modifies the file.
    """
    with open(path, 'r', newline='', encoding=SCH_ENCODING) as f:
        text = f.read()
    lines = text.split('\r\n' if '\r\n' in text else '\n')

    renamed: dict = {}
    for line in lines:
        name = None
        if line.startswith('*S'):
            name = line[2:].rstrip()
        elif line.startswith('*C '):
            parts = line.split(None, 4)
            if len(parts) >= 5:
                name = parts[4].rstrip()
        if name and ' ' in name and name not in renamed:
            renamed[name] = name.replace(' ', '_')
    return renamed


def write_sanitised_copy(src_path, dst_path, renamed: dict) -> None:
    """Write a copy of src_path to dst_path with every *S/*C symbol name
    in *renamed* (as returned by find_space_symbol_names) rewritten to its
    sanitised form, preserving the source file's own CRLF/LF convention
    and leaving every other line -- including DEVICE= attribute lines
    that happen to repeat the same name -- byte-for-byte unchanged. If
    renamed is empty, this is a plain copy.
    """
    with open(src_path, 'r', newline='', encoding=SCH_ENCODING) as f:
        text = f.read()
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.split(newline)

    if renamed:
        new_lines = []
        for line in lines:
            if line.startswith('*S'):
                name = line[2:].rstrip()
                if name in renamed:
                    line = '*S' + renamed[name]
            elif line.startswith('*C '):
                parts = line.split(None, 4)
                if len(parts) >= 5:
                    name = parts[4].rstrip()
                    if name in renamed:
                        line = f'{parts[0]} {parts[1]} {parts[2]} {parts[3]} {renamed[name]}'
            new_lines.append(line)
        lines = new_lines

    dst_path = Path(dst_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dst_path, 'w', newline='', encoding=SCH_ENCODING) as f:
        f.write(newline.join(lines))


def sanitise_space_symbol_names(path) -> dict:
    """Rewrite, in place, every *S/*C symbol name of this one file that
    contains a space (find_space_symbol_names, write_sanitised_copy). Run on
    a corrected copy, never on an original. Returns {old: new}, empty when
    nothing needed changing (the file is then not written)."""
    renamed = find_space_symbol_names(path)
    if renamed:
        write_sanitised_copy(path, path, renamed)
    return renamed


# ── pin-override POE correction: SCH source-file patching ───────────────────
#
# See kiuc_writer._resolve_pin_override_poes for the full detection/
# correction rationale, and kiuc_model.PinOverrideCorrection for the record
# type produced during conversion. This patches the actual raw *C attribute
# line(s) -- format "<dx> <dy> <dx_poe> <dy_poe> <size> <colour> <visibility>
# <TAG>=<value>" -- replacing only the (dx_poe, dy_poe) fields, leaving
# every other field and every other line byte-for-byte unchanged.
#
# The file patched is the sheet's copy in the design's corrected folder
# (kiut.core.corrected), never the original: the corrected design is then
# usable for further work in Ulticap, and the original stays as it was, so
# no backup is needed. The fix is idempotent: a corrected file has nothing
# left to find on a later run.
def write_pin_override_corrections(corrections: List[PinOverrideCorrection],
                                   sheet_name_to_path: dict) -> List[str]:
    """Apply every PinOverrideCorrection to the .SCH file it belongs to, in
    place. sheet_name_to_path maps each correction's sheet_name (e.g.
    "MULTI SE.SCH") to the file to patch -- the sheet's corrected copy.
    Returns one human-readable summary line per file actually patched.
    """
    by_file: dict = {}
    for c in corrections:
        by_file.setdefault(c.sheet_name, []).append(c)

    summaries: List[str] = []
    for sheet_name, items in by_file.items():
        path = sheet_name_to_path.get(sheet_name)
        if path is None:
            continue
        path = Path(path)

        with open(path, 'r', newline='', encoding=SCH_ENCODING) as f:
            text = f.read()
        newline = '\r\n' if '\r\n' in text else '\n'
        lines = text.split(newline)

        applied = 0
        i, n = 0, len(lines)
        while i < n:
            stripped = lines[i].strip()
            m = re.match(r'^\*C\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.*)$', stripped)
            if not m:
                i += 1
                continue
            cx, cy, crot = int(m.group(1)), int(m.group(2)), int(m.group(3))
            cname = m.group(4).rstrip()
            block_corrections = [
                c for c in items if c.comp_x == cx and c.comp_y == cy
                and c.comp_rotation == crot and c.comp_symbol_name == cname]
            i += 1
            if not block_corrections:
                continue
            # Scan forward through this *C block's own attribute lines,
            # until the next *C/*S header or the block's own terminator.
            while i < n:
                bstripped = lines[i].strip()
                if bstripped.startswith('*C') or bstripped.startswith('*S') or bstripped == ';':
                    break
                parts = bstripped.split(None, 7)
                if len(parts) == 8 and '=' in parts[7]:
                    p_dx, p_dy, p_poe_x, p_poe_y, p_size, p_colour, p_vis, p_tagval = parts
                    p_tag, p_val = p_tagval.split('=', 1)
                    for c in block_corrections:
                        if (c.tag == p_tag and c.value == p_val
                                and c.dx == int(p_dx) and c.dy == int(p_dy)
                                and c.old_dx_poe == int(p_poe_x)
                                and c.old_dy_poe == int(p_poe_y)):
                            lines[i] = (f'{p_dx} {p_dy} {c.new_dx_poe} {c.new_dy_poe} '
                                       f'{p_size} {p_colour} {p_vis} {p_tagval}')
                            applied += 1
                            break
                i += 1

        if not applied:
            continue

        with open(path, 'w', newline='', encoding=SCH_ENCODING) as f:
            f.write(newline.join(lines))
        summaries.append(f'{path.name}: {applied} pin-label override(s) corrected')

    return summaries
