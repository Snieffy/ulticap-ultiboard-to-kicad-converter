# KIUB - KiCad Ultiboard Import Tool
# Copyright (C) 2026  Snieffy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://gnu.org>.

"""
kiub_library.py -- the library board and footprint library: every shape of
a DDF or an Ultiboard library laid out once

A DDF holds every shape it defines, placed or not, but its design board
holds footprints only for placed components: a shape the DDF never places
does not reach KiCad that way. This module rewrites the DDF so that every
shape is placed exactly once -- unrotated, on the front side, in the DDF's
own order, packed in rows by bounding box -- whatever the DDF itself
places, and kiub.py converts the result like any design into the library
board <name>_lib.kicad_pcb, whose footprints make the footprint library.

Kept from the DDF: the header (its bounds line rewritten) and the
technology (*T) records, which the pads need. Left out: its components
(*C), nets (*N), copper (*L), vias (*V), texts (*X) and its BOARD outline;
a new BOARD rectangle encloses the laid-out shapes. Each shape is placed as
component S<n> with the shape's name as value, both texts at the shape's
own reference and alias text positions.

An Ultiboard library (.LIB for V4, .L55 for V5) holds shapes without their
technology: read_library and load_technology read it and its .TEC data,
and library_ddf turns both into DDF text for the same layout.

The footprints of the laid-out board also make a KiCad footprint library
(<name>.pretty, one .kicad_mod per shape) registered in the project's
fp-lib-table: footprint_names, kicad_mod and fp_lib_table here (the table
and the name rule shared with KIUC: kiut.core.kicad_project), the
conversion in kiub.py.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from kiut.core import ddf, kicad_project

# Distances in inches, converted to the DDF's units (1/1200 inch for V4 --
# V2/V3 files are pre-converted to V4.60 before they get here -- and
# nanometres for V5).
_GAP_IN = 0.2       # between shapes, and between shapes and the board edge
_GRID_IN = 0.025    # placement grid
_ROW_RATIO = 1.4    # row width / square root of the total area: a landscape board


class LibraryError(ValueError):
    """A DDF that cannot be laid out as a library (no shapes)."""


@dataclass
class LibraryLayout:
    """The rewritten DDF and what the layout did. ``report`` holds the
    lines for the conversion log, ``warnings`` the lines that need the
    user's attention."""
    text: str
    shapes: int
    board_mm: tuple[float, float]
    report: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _sections(lines: list[str]) -> list[list[str]]:
    """The ';'-terminated sections of a shape body (outline, pads, arcs)."""
    secs, cur = [], []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        cur.append(ln)
        if ln.endswith(';'):
            secs.append(cur)
            cur = []
    return secs


def _pad_extents(tech: list[str]) -> tuple[dict[int, tuple[int, int]], dict[int, int]]:
    """Pad code -> largest copper width and height over the three pad
    tables, and drill code -> diameter: what a pad of that code covers."""
    size: dict[int, tuple[int, int]] = {}
    drill: dict[int, int] = {}
    for ln in tech:
        if ln[:3] in ('*T0', '*T1', '*T2'):
            f = [int(x) for x in ln[4:].split(',')]
            w, h = f[1] + f[2], f[3]
            ow, oh = size.get(f[0], (0, 0))
            size[f[0]] = (max(ow, w), max(oh, h))
        elif ln.startswith('*TD'):
            c, d = (int(x) for x in ln[4:].split(','))
            drill[c] = d
    return size, drill


def _bbox(body: str, size, drill):
    """Bounding box of a shape around its origin -- outline, pads (each as
    the largest circle its code can need), arcs and its two texts -- with
    its reference and alias text lines and its pad count."""
    lines = body.split('\n')
    ref = [int(x) for x in lines[0].split()]
    ali = [int(x) for x in lines[1].split()]
    secs = _sections(lines[3:])
    xs: list[float] = []
    ys: list[float] = []
    if secs and secs[0] != [';']:
        nums = [int(v) for v in ''.join(secs[0]).rstrip(';').split(',') if v.strip()]
        for i in range(0, len(nums) - 1, 2):
            xs.append(nums[i] - nums[i] % 2)        # an odd x marks a segment start
            ys.append(nums[i + 1])
    npads = 0
    if len(secs) > 1 and secs[1] != [';']:
        for ln in secs[1]:
            f = ln.rstrip(',;').split(',')
            code, px, py = int(f[0]), int(f[3]), int(f[4])
            w, h = size.get(code, (0, 0))
            r = max(w, h, drill.get(code, 0)) / 2
            xs += [px - r, px + r]
            ys += [py - r, py + r]
            npads += 1
    if len(secs) > 2 and secs[2] != [';']:
        for ln in secs[2]:
            f = [int(v) for v in ln.rstrip(',;').split(',')]
            xs += [f[0] - f[2], f[0] + f[2]]
            ys += [f[1] - f[2], f[1] + f[2]]
    for t in (ref, ali):                            # text anchor, padded by its height
        if t[2]:
            xs += [t[0] - t[2], t[0] + t[2]]
            ys += [t[1] - t[2], t[1] + t[2]]
    if not xs:
        xs, ys = [0], [0]
    return (min(xs), min(ys), max(xs), max(ys)), ref, ali, npads


def _snap(v: float, g: int) -> int:
    return int(math.floor(v / g) * g)


# The DDF text as kiub.py reads it (kiut.core.ddf.lines, shared with KIUC).
_lines = ddf.lines


def layout_library(ddf_text: str) -> LibraryLayout:
    """Rewrite a V4/V5 DDF (V2/V3 already pre-converted) so that every
    shape is placed once. Raises LibraryError if the DDF defines no shape
    besides BOARD."""
    text = _lines(ddf_text)
    recs = re.split(r'\n(?=\*)', text)
    head = recs[0].split('\n')                       # *P header block
    tech = [r.split('\n')[0] for r in recs if r.startswith('*T')]
    inch = 25_400_000 if head[1].split()[0] == '5' else 1200
    gap, grid = round(inch * _GAP_IN), round(inch * _GRID_IN)
    size, drill = _pad_extents(tech)
    report: list[str] = []
    warnings: list[str] = []

    shapes, seen = [], {}
    for r in recs:
        if not r.startswith('*S'):
            continue
        name, _, body = r.partition('\n')
        name = name[2:].strip()
        if name == 'BOARD' or name.endswith('.BAK'):
            continue
        try:
            entry = (name, body.rstrip('\n')) + _bbox(body, size, drill)
        except (ValueError, IndexError):
            warnings.append(f'shape {name} left out: its definition could not be read')
            continue
        if name in seen:
            # kiub.py keeps the last definition of a name for every placement:
            # lay that one out, in the first one's place.
            warnings.append(f'shape {name} is defined more than once; the last '
                            f'definition is used')
            shapes[seen[name]] = entry
            continue
        seen[name] = len(shapes)
        shapes.append(entry)
    if not shapes:
        raise LibraryError('the DDF defines no shapes to lay out')

    area = sum((b[2] - b[0] + gap) * (b[3] - b[1] + gap) for _, _, b, *_ in shapes)
    row_w = math.sqrt(area) * _ROW_RATIO
    placed, cx, top, row_h = [], 0, 0, 0
    for name, body, b, ref, ali, npads in shapes:
        w = b[2] - b[0]
        if cx and cx + w > row_w:                    # next row
            cx, top, row_h = 0, top - row_h - gap, 0
        ox = _snap(cx - b[0] + grid - 1, grid)       # bbox left edge at or right of cx
        oy = _snap(top - b[3], grid)                 # bbox top edge at or below the row top
        placed.append((name, ox, oy, ref, ali, npads))
        cx = ox + b[2] + gap
        row_h = max(row_h, top - (oy + b[1]))
    ext_x = max(p[1] + s[2][2] for p, s in zip(placed, shapes))
    ext_y = min(p[2] + s[2][1] for p, s in zip(placed, shapes))
    x0, y0, x1, y1 = -gap, gap, _snap(ext_x + gap + grid, grid), _snap(ext_y - gap, grid)
    x0, y0, x1, y1 = (v + v % 2 for v in (x0, y0, x1, y1))   # even: an odd x is a marker

    tail = head[2].split(',')[4:]                    # grid, swap level, [routing], max layers
    head[2] = f'{x0}, {y0}, {x1}, {y1},' + ','.join(tail)
    # The six power-plane lines after the layer-pair line name nets the
    # layout leaves out: no power planes (65535) on the library board.
    for i in range(7, min(13, len(head))):
        head[i] = re.sub(r'\d+', '65535', head[i])
    out = head + tech
    out += ['*SBOARD', '0 0 0 0 0 100', '0 0 0 0 0 100', '0.000000',
            f'{x0 + 1},{y0},{x1},{y0},{x1},{y1},{x0},{y1},{x0},{y0};', ';', ';', ';']
    for name, body, *_ in shapes:
        out += ['*S' + name] + body.split('\n')
    for n, (name, ox, oy, ref, ali, npads) in enumerate(placed, 1):
        # *C text fields are x, y, rotation, width, height, thickness; a
        # shape's text lines are x, y, height, rotation, width, thickness.
        out += [f'*C S{n} /{name} {name}',
                f'{ox},{oy},0,{ref[0]},{ref[1]},{ref[3]},{ref[4]},{ref[2]},{ref[5]},'
                f'{ali[0]},{ali[1]},{ali[3]},{ali[4]},{ali[2]},{ali[5]}',
                '0 0 0 0 0 0.000000 0']
        pins = ['65535 ffffffff'] * npads             # every pin unconnected
        out += [' '.join(pins[i:i + 16]) for i in range(0, len(pins), 16)] + [';']

    mm = 25.4 / inch
    board = (round((x1 - x0) * mm, 1), round((y0 - y1) * mm, 1))
    report.append(f'Library board: {len(placed)} shape{"s" if len(placed) != 1 else ""} '
                  f'laid out, board {board[0]} x {board[1]} mm')
    return LibraryLayout(text='\n'.join(out) + '\n\n', shapes=len(placed), board_mm=board,
                         report=report, warnings=warnings)


def has_components(ddf_text: str) -> bool:
    """Whether the DDF places any component (a *C record)."""
    return re.search(r'(?m)^\r*\*C ', ddf_text) is not None


# ---------------------------------------------------------------------------
# Ultiboard libraries (.LIB for V4, .L55 for V5) and their technology (.TEC)
# ---------------------------------------------------------------------------
#
# A compiled Ultiboard library holds, per shape, exactly the DDF shape text
# (the *S record without its name line) -- but no technology: pad codes
# only, their sizes and drills live in the design's technology, which
# Ultiboard keeps in .TEC files. So a library converts only together with
# its .TEC data. Both are turned back into DDF text here and laid out by
# layout_library like any DDF. See FILEFORMAT_ULTIBOARD_LIB_TEC.md.

ULTIBOARD_SIGNATURE = b'ULTImate-LIB\0'
ULTICAP_SIGNATURE = b'ULTImate LIB\0'       # Ulticap's DOS symbol libraries (KIUC)
_INDEX_AT = 0x14
_SLOT = 22                                  # bytes per index slot
_NAME_MAX = 12                              # longest shape name a library holds
# Largest value in a library body or technology table: V4 data (1/1200
# inch) stays far below it, V5 data (nanometres) far above.
_V5_THRESHOLD = 50_000
_V4_TO_V5 = 25_400_000 / 1200               # nanometres per V4 unit
_PAD_CODES, _DRILL_CODES, _TRACE_CODES = 256, 256, 32


def library_kind(path) -> str | None:
    """'ultiboard' for an Ultiboard library, 'ulticap' for an Ulticap DOS
    symbol library (same extension .LIB, a space for the hyphen in its
    signature), None otherwise. Decided by the signature, not the name."""
    try:
        with open(path, 'rb') as f:
            head = f.read(len(ULTIBOARD_SIGNATURE))
    except OSError:
        return None
    if head == ULTIBOARD_SIGNATURE:
        return 'ultiboard'
    if head == ULTICAP_SIGNATURE:
        return 'ulticap'
    return None


@dataclass
class UltiboardLibrary:
    """A library's shapes as (name, DDF shape text without the name line),
    in index order, and what reading it found."""
    path: str
    units: int                                  # 4 (1/1200 inch) or 5 (nanometres)
    shapes: list[tuple[str, str]]
    skipped: list[str] = field(default_factory=list)       # slots left out, with the reason
    full_length_names: list[str] = field(default_factory=list)


def _ints(text: str) -> list[int]:
    return [int(v) for v in re.findall(r'-?\d+', text)]


def _body_problem(name: str, text: str) -> str | None:
    """Why a library slot cannot be a shape, or None. The body must read as
    a DDF shape: two text lines of six integers, the thermal value, then
    the outline, pad and arc sections, each ended by ';'."""
    if not name or any(ch.isspace() or ch == '*' or not ch.isprintable() for ch in name):
        return 'its name is not a shape name'
    lines = text.split('\n')
    if len(lines) < 4:
        return 'its definition is too short'
    for ln in lines[:2]:
        if not re.fullmatch(r'\s*-?\d+(\s+-?\d+){5}\s*', ln):
            return 'its text lines are not those of a shape'
    try:
        float(lines[2])
    except ValueError:
        return 'its thermal value is not a number'
    rest = [ln.strip() for ln in lines[3:] if ln.strip()]
    secs = _sections(rest)
    if len(secs) < 3 or sum(len(sec) for sec in secs) != len(rest):
        return 'its outline, pad and arc sections are incomplete'
    outline = ''.join(secs[0]).rstrip(';').split(',')
    if secs[0] != [';'] and (len([v for v in outline if v.strip()]) % 2
                             or not all(re.fullmatch(r'-?\d+', v.strip()) for v in outline if v.strip())):
        return 'its outline is not a list of coordinate pairs'
    for ln in secs[1] if secs[1] != [';'] else []:
        f = ln.rstrip(',;').split(',', 5)
        if len(f) != 6 or not all(re.fullmatch(r'-?\d+', v) for v in (f[0], f[1], f[3], f[4])) \
                or not re.fullmatch(r'[0-9a-fA-F]{1,8}', f[2]) or not f[5]:
            return 'a pad line is not a pad'
    for ln in secs[2] if secs[2] != [';'] else []:
        f = ln.rstrip(',;').split(',')
        if len(f) != 5 or not all(re.fullmatch(r'-?\d+', v) for v in f):
            return 'an arc line is not an arc'
    return None


def read_library(path) -> UltiboardLibrary:
    """Read an Ultiboard library. Raises LibraryError for an Ulticap or
    unknown file, a damaged index, or a library without a readable shape;
    a slot that does not hold a shape is left out and reported."""
    data = open(path, 'rb').read()
    if data[:len(ULTIBOARD_SIGNATURE)] != ULTIBOARD_SIGNATURE:
        if data[:len(ULTICAP_SIGNATURE)] == ULTICAP_SIGNATURE:
            raise LibraryError('this is an Ulticap symbol library, not an Ultiboard one: '
                               'convert it with KIUC')
        raise LibraryError('not an Ultiboard library (no "ULTImate-LIB" signature)')
    if len(data) < _INDEX_AT:
        raise LibraryError('the library ends inside its header')
    slots = int.from_bytes(data[0x10:0x12], 'little')
    if _INDEX_AT + slots * _SLOT > len(data):
        raise LibraryError('the library\'s index runs past the end of the file')
    shapes: list[tuple[str, str]] = []
    skipped: list[str] = []
    biggest = 0
    for i in range(slots):
        at = _INDEX_AT + i * _SLOT
        used = int.from_bytes(data[at:at + 2], 'little')
        if not used:
            continue
        offset = int.from_bytes(data[at + 2:at + 6], 'little')
        length = int.from_bytes(data[at + 6:at + 8], 'little')
        name = data[at + 9:at + 22].split(b'\0')[0].decode('cp437')
        if offset + length > len(data):
            skipped.append(f'{name!r}: its definition lies outside the file')
            continue
        text = data[offset:offset + length].rstrip(b'\0').decode('cp437')
        text = text.replace('\r\n', '\n').rstrip('\n')
        problem = _body_problem(name, text)
        if problem:
            skipped.append(f'{name!r}: {problem}')
            continue
        shapes.append((name, text))
        biggest = max([biggest] + [abs(v) for v in _ints(text)])
    if not shapes:
        raise LibraryError('the library holds no readable shapes'
                           + (f' ({len(skipped)} damaged)' if skipped else ''))
    return UltiboardLibrary(path=str(path), units=5 if biggest > _V5_THRESHOLD else 4,
                            shapes=shapes, skipped=skipped,
                            full_length_names=[n for n, _ in shapes if len(n) == _NAME_MAX])


# Technology files. A single .TEC (V4.60 and later) holds every table: *P0,
# *P1, *P2 (inner, top, bottom pads -- the DDF's *T0/*T1/*T2), *D drills,
# *T traces, *C drill tolerance. Older Ultiboard versions split them over several files:
# one pad table per file (*P, no layer digit; which layer it is depends
# only on how the user uses it), a drill file (*D, *C) and a trace file
# (*T). The structure, not the *V line, tells them apart: Ultiboard's own
# version numbers are not reliable.

TEC_SINGLE, TEC_TOP, TEC_BOTTOM, TEC_INNER, TEC_DRILLS, TEC_TRACES = (
    'single', 'top', 'bottom', 'inner', 'drills', 'traces')
TEC_ROLE_NAMES = {TEC_SINGLE: 'Technology file', TEC_TOP: 'Top pads',
                  TEC_BOTTOM: 'Bottom pads', TEC_INNER: 'Inner pads',
                  TEC_DRILLS: 'Drills', TEC_TRACES: 'Traces'}
_PAD_LAYER = {TEC_INNER: '0', TEC_TOP: '1', TEC_BOTTOM: '2'}   # the DDF's *T0/*T1/*T2


@dataclass
class TecFile:
    """One .TEC file as read: its records by kind and what it holds."""
    path: str
    version: str = ''
    kind: str = ''                      # 'single', 'pads', 'drills', 'traces' or '' (unclear)
    pads: dict = field(default_factory=dict)        # (layer or None, code) -> 9 values
    drills: dict = field(default_factory=dict)      # code -> diameter
    traces: dict = field(default_factory=dict)      # code -> (width, clearance)
    drill_tolerance: int | None = None   # *C, the DDF's first *TC field
    problem: str = ''                   # why it could not be read


def read_tec(path) -> TecFile:
    """Read a .TEC file of either layout (never raises: problem says what
    is wrong with it)."""
    tec = TecFile(path=str(path))
    try:
        raw = open(path, 'rb').read().decode('cp437')
    except OSError as e:
        tec.problem = f'cannot be read ({e.strerror})'
        return tec
    lines = [ln.strip() for ln in raw.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    seen = set()
    try:
        for ln in lines:
            if not ln.startswith('*'):
                continue
            if ln.startswith('*V'):
                tec.version = ' '.join(ln[2:].split())
            elif re.match(r'\*P[012] ', ln):
                v = [int(x) for x in ln[4:].split(',')]
                tec.pads[(ln[2], v[0])] = tuple(v[1:])
                seen.add('P*')
            elif re.match(r'\*P\s', ln):
                v = [int(x) for x in ln[2:].split(',')]
                tec.pads[(None, v[0])] = tuple(v[1:])
                seen.add('P')
            elif ln.startswith('*D'):
                c, d = (int(x) for x in ln[2:].split(','))
                tec.drills[c] = d
                seen.add('D')
            elif ln.startswith('*T'):
                v = [int(x) for x in ln[2:].split(',')]
                tec.traces[v[0]] = tuple(v[1:3])
                seen.add('T')
            elif ln.startswith('*C'):
                tec.drill_tolerance = int(ln[2:].split()[0])
            elif ln.startswith('*G'):
                pass                        # no DDF counterpart; not needed for shapes
            else:
                seen.add('?')
    except (ValueError, IndexError):
        tec.problem = 'holds a record that is not technology data'
        return tec
    if 'P*' in seen and seen <= {'P*', 'D', 'T'}:
        tec.kind = TEC_SINGLE
    elif seen == {'P'}:
        tec.kind = 'pads'
    elif seen == {'D'}:
        tec.kind = TEC_DRILLS
    elif seen == {'T'}:
        tec.kind = TEC_TRACES
    elif not seen:
        tec.problem = 'holds no technology data'
    else:
        tec.problem = 'holds a mix of records no technology file has'
    return tec


def check_tec(path, role: str) -> tuple[TecFile, str, list[str]]:
    """Read a .TEC file for a role (TEC_SINGLE, or one file of a multi-file
    set) and check that it holds what the role needs. Returns the file, an
    error ('' when it fits) and incomplete tables (a warning: the missing
    codes count as undefined)."""
    tec = read_tec(path)
    if tec.problem:
        return tec, f'{TEC_ROLE_NAMES[role]}: this file {tec.problem}', []
    want = TEC_SINGLE if role == TEC_SINGLE else 'pads' if role in _PAD_LAYER else role
    if tec.kind != want:
        holds = {TEC_SINGLE: 'all tables (a single-file .TEC)',
                 'pads': 'one pad table (one file of a multi-file set)',
                 TEC_DRILLS: 'a drill table (one file of a multi-file set)',
                 TEC_TRACES: 'a trace table (one file of a multi-file set)'}[tec.kind]
        return tec, f'{TEC_ROLE_NAMES[role]}: this file holds {holds}', []
    short: list[str] = []
    name = TEC_ROLE_NAMES[role]
    if tec.kind == TEC_SINGLE:
        for layer, what in (('1', 'top'), ('2', 'bottom'), ('0', 'inner')):
            n = sum(1 for (lay, _c) in tec.pads if lay == layer)
            if n < _PAD_CODES:
                short.append(f'{name}: {n} of {_PAD_CODES} {what} pad codes')
        if len(tec.drills) < _DRILL_CODES:
            short.append(f'{name}: {len(tec.drills)} of {_DRILL_CODES} drill codes')
    elif tec.kind == 'pads' and len(tec.pads) < _PAD_CODES:
        short.append(f'{name}: {len(tec.pads)} of {_PAD_CODES} pad codes')
    elif tec.kind == TEC_DRILLS and len(tec.drills) < _DRILL_CODES:
        short.append(f'{name}: {len(tec.drills)} of {_DRILL_CODES} drill codes')
    return tec, '', short


@dataclass
class Technology:
    """The technology for a library, gathered from its .TEC file(s)."""
    pads: dict                          # (layer '0'/'1'/'2', code) -> 9 values
    drills: dict
    traces: dict
    drill_tolerance: int                # *C: the DDF's first *TC field
    sources: list[str]                  # 'role: path' lines for the log
    incomplete: list[str]
    units: int | None                   # 4, 5, or None when every value is 0


def load_technology(single=None, top=None, bottom=None, inner=None, drills=None,
                    traces=None) -> Technology:
    """Gather the technology from one single-file .TEC, or from a
    multi-file set (top, bottom, inner pads and drills; traces optional).
    Raises LibraryError when a file does not fit its role."""
    roles = ([(TEC_SINGLE, single)] if single else
             [(r, p) for r, p in ((TEC_TOP, top), (TEC_BOTTOM, bottom), (TEC_INNER, inner),
                                  (TEC_DRILLS, drills), (TEC_TRACES, traces)) if p])
    if single and any((top, bottom, inner, drills, traces)):
        raise LibraryError('give either a single .TEC file or a multi-file set, not both')
    if not single:
        missing = [TEC_ROLE_NAMES[r] for r in (TEC_TOP, TEC_BOTTOM, TEC_INNER, TEC_DRILLS)
                   if not dict(roles).get(r)]
        if missing:
            raise LibraryError(f'the multi-file technology lacks: {", ".join(missing)}')
    pads: dict = {}
    drill_table: dict = {}
    trace_table: dict = {}
    drill_tolerance = None
    incomplete: list[str] = []
    sources: list[str] = []
    for role, path in roles:
        tec, error, short = check_tec(path, role)
        if error:
            raise LibraryError(error)
        incomplete += short
        sources.append(f'{TEC_ROLE_NAMES[role]}: {path}'
                       + (f' (*V {tec.version})' if tec.version else ''))
        if role == TEC_SINGLE:
            pads.update(tec.pads)
        elif role in _PAD_LAYER:
            pads.update({(_PAD_LAYER[role], code): v for (_l, code), v in tec.pads.items()})
        drill_table.update(tec.drills)
        trace_table.update(tec.traces)
        if tec.drill_tolerance is not None:
            drill_tolerance = tec.drill_tolerance
    biggest = max([0] + [abs(x) for v in pads.values() for x in v[:5]]
                  + [abs(x) for x in drill_table.values()])
    units = None if not biggest else 5 if biggest > _V5_THRESHOLD else 4
    return Technology(pads=pads, drills=drill_table, traces=trace_table,
                      drill_tolerance=drill_tolerance or 0, sources=sources, incomplete=incomplete,
                      units=units)


def _round(x: float) -> int:
    return int(math.floor(x + 0.5))


def convert_technology(tech: Technology, units: int) -> tuple[Technology, str]:
    """The technology in the library's units, and a log note ('' when
    nothing was converted). V4 -> V5 follows Ultiboard's own conversion
    (reproduces every V5 table derived from a V4 one that was checked):
    widths, clearances, drills, traces and the drill tolerance round to
    nearest; a symmetric
    pad's height (y = x1 + x2) rounds down; a fully rounded pad's radius
    (2r = y) is the converted height halved. V5 -> V4 rounds to nearest
    1/1200 inch, but never turns a value that is not zero into zero -- a
    drill below 0.05 mm marks pads without a hole (FILEFORMAT_ULTIBOARD_DDF.md
    10.4) and must keep doing so."""
    if tech.units in (None, units):
        return tech, ''
    if units == 5:
        def conv(x):
            return _round(x * _V4_TO_V5)

        def pad(v):
            x1, x2, y, r, cl, *rest = v
            Y = math.floor(y * _V4_TO_V5) if y == x1 + x2 else conv(y)
            R = Y // 2 if 2 * r == y else conv(r)
            return (conv(x1), conv(x2), Y, R, conv(cl), *rest)
        note = 'converted from V4 units (1/1200 inch) to the library\'s V5 units (nm)'
    else:
        def conv(x):
            return max(1, _round(x / _V4_TO_V5)) if x else 0

        def pad(v):
            x1, x2, y, r, cl, *rest = v
            X1, X2 = conv(x1), conv(x2)
            Y = X1 + X2 if y == x1 + x2 else conv(y)
            R = Y // 2 if 2 * r == y else conv(r)
            return (X1, X2, Y, R, conv(cl), *rest)
        note = ('converted from V5 units (nm) to the library\'s V4 units '
                '(1/1200 inch), rounding to the nearest unit')
    return Technology(pads={k: pad(v) for k, v in tech.pads.items()},
                      drills={c: conv(d) for c, d in tech.drills.items()},
                      traces={c: tuple(conv(x) for x in v) for c, v in tech.traces.items()},
                      drill_tolerance=conv(tech.drill_tolerance), sources=tech.sources,
                      incomplete=tech.incomplete, units=units), note


def _technology_records(tech: Technology) -> list[str]:
    """The technology as DDF *T records. All-zero entries are left out, as
    Ultiboard leaves them out of a DDF: a drill of 0 would otherwise read
    as a drill below the SMD threshold (FILEFORMAT_ULTIBOARD_DDF.md 10.4)."""
    tt = [f'*TT {c}, {w}, {cl}' for c, (w, cl) in sorted(tech.traces.items())]
    td = [f'*TD {c}, {d}' for c, d in sorted(tech.drills.items()) if d]
    tp = [f'*T{layer} {code}, ' + ', '.join(map(str, v))
          for layer in '012'
          for (lay, code), v in sorted(tech.pads.items(), key=lambda kv: kv[0][1])
          if lay == layer and any(v)]
    return ['*TP ffffffff'] + tt + [f'*TC {tech.drill_tolerance}'] + td + tp + ['*TS 0 0 0']


def library_ddf(lib: UltiboardLibrary, tech: Technology) -> str:
    """A DDF holding the library's shapes and the technology (already in the
    library's units): what layout_library lays out. Two copper layers --
    the footprints' pads go on the outer layers."""
    grid = 30 if lib.units == 4 else 635_000            # 0.025 inch
    head = ['*P PCB', '4 80' if lib.units == 4 else '5 50', f'0, 0, 0, 0, {grid}, 3, 2;',
            '(|)', '0, 0', '0 0 0 15 30 1', ' '.join(['1 2'] * 16) + ' ',
            '65535 ', '65535 ', '65535 ', '65535 ', '65535 ', '65535 65535 ']
    out = head + _technology_records(tech)
    for name, body in lib.shapes:
        out += ['*S' + name] + body.split('\n')
    return '\n'.join(out) + '\n\n'


# ---------------------------------------------------------------------------
# KiCad footprint library (<name>.pretty) and the project's fp-lib-table
# ---------------------------------------------------------------------------
#
# The footprints are those of the library board (every shape once,
# unrotated, on the front side, check marks included): each footprint block
# of that board becomes one <footprint>.kicad_mod. kiub.py does the
# conversion; the text handling is here.

# Characters a footprint name or library nickname cannot hold: KiCad's item
# separator ':', path separators and what Windows file names refuse.
FP_LIB_TABLE = kicad_project.FP_LIB_TABLE


# The shapes a DDF defines, as layout_library lays them out, and their
# footprint names: kiut.core.ddf, shared with KIUC, which names a symbol's
# Footprint field after them.
shape_names = ddf.shape_names
footprint_names = ddf.footprint_names


# A shape or design name as a KiCad footprint name or library nickname: the
# rule KIUC names its symbol library by too (kiut.core.kicad_project).
kicad_name = kicad_project.kicad_name


def board_footprints(board_text: str) -> list[tuple[str, str]]:
    """The footprint blocks of a .kicad_pcb written by kiub.py, as
    (library item name, block text) in board order."""
    out = []
    for m in re.finditer(r'(?ms)^  \(footprint "([^"]*)"\n.*?^  \)\n', board_text):
        out.append((m.group(1).split(':', 1)[-1], m.group(0)))
    return out


def kicad_mod(block: str, name: str, version: str, generator_version: str) -> str:
    """A footprint block of a library board as a .kicad_mod file: the
    footprint's own position dropped (it is placed at 0,0, unrotated), the
    reference 'REF**', no nets."""
    lines = block.rstrip('\n').split('\n')
    out = [f'(footprint "{name}"', f'  (version {version})', '  (generator "KIUB")',
           f'  (generator_version "{generator_version}")']
    body = lines[1:]
    at = next((i for i, ln in enumerate(body) if ln.lstrip().startswith('(at ')), None)
    if at is not None and at < 2:                   # the footprint's own (at x y rot)
        del body[at]
    for ln in body:
        if ln.strip() == '(net 0 "")':
            continue
        if ln.lstrip().startswith('(property "Reference" '):
            ln = re.sub(r'(\(property "Reference" )"[^"]*"', r'\1"REF**"', ln)
        out.append(ln[2:] if ln.startswith('  ') else ln)
    return '\n'.join(out) + '\n'


def fp_lib_table(existing: str | None, nickname: str, uri: str,
                 descr: str) -> tuple[str | None, str]:
    """The project's fp-lib-table with the library nickname -> uri entry
    added or replaced, every other entry kept (kiut.core.kicad_project.
    lib_table, as KIUC's sym-lib-table). Returns (text, what was done); text
    is None when an existing file is not a footprint library table (left
    alone)."""
    return kicad_project.lib_table('fp', existing, nickname, uri, descr)


__all__ = ['LibraryError', 'LibraryLayout', 'Technology', 'TecFile', 'UltiboardLibrary',
           'TEC_SINGLE', 'TEC_TOP', 'TEC_BOTTOM', 'TEC_INNER', 'TEC_DRILLS', 'TEC_TRACES',
           'TEC_ROLE_NAMES', 'check_tec', 'convert_technology', 'has_components',
           'layout_library', 'library_ddf', 'library_kind', 'load_technology', 'read_library',
           'read_tec', 'FP_LIB_TABLE', 'board_footprints', 'footprint_names', 'fp_lib_table',
           'kicad_mod', 'kicad_name', 'shape_names']
