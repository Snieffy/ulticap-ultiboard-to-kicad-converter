# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/kicad_links.py -- A board's footprints linked to their schematic symbols

KiCad links a footprint to its symbol by a path of UUIDs: in the .kicad_pcb, each footprint has

    (path "/<symbol uuid>")                          a symbol on the root sheet
    (path "/<sheet symbol uuid>/.../<symbol uuid>")  a symbol on a sub-sheet
    (sheetname "/")  or  (sheetname "/<sheet name>/.../")
    (sheetfile "<the symbol's .kicad_sch>")

(as KiCad 9 writes them; its complex_hierarchy demo, checked 2026-11-01). The root file's own UUID is
not in the path. A multi-unit part links to its unit 1 (the lowest unit number placed); KiCad may
move the link to another of its units (its Update PCB from Schematic does for some parts), and a
link to any of the part's units is as good: compare counts it as linked, and keep_units keeps it
when the board is converted again. With them KiCad's "Update PCB from Schematic" matches footprints
to symbols without asking.

The links are read from the design's converted schematic (schematic_links: the .kicad_sch files in
<name>_KiCad -- KIUC's, or KiCad's after an edit), not recomputed from the Ulticap files, so a
schematic edited in KiCad keeps its links. KIUC's writer derives the UUIDs a link uses from the
sheet, the designator and the unit (kiuc_writer._named_uid), so a schematic converted again keeps
them and a board converted before stays linked; board_links reads an existing board's, and
compare tells which parts are linked, changed, or on one side only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([()])|([^\s()"]+)')


class _Str(str):
    """A quoted string, told apart from an atom."""


def parse(text: str) -> list:
    """An S-expression text as nested lists: atoms as str, quoted strings as
    _Str (backslash escapes undone)."""
    stack: List[list] = [[]]
    for m in _TOKEN.finditer(text):
        q, paren, atom = m.groups()
        if paren == '(':
            stack.append([])
        elif paren == ')':
            if len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
        elif q is not None:
            stack[-1].append(_Str(re.sub(r'\\(.)', r'\1', q)))
        else:
            stack[-1].append(atom)
    return stack[0][0] if stack[0] else []


def _items(node, name: str):
    return [x for x in node if isinstance(x, list) and x and x[0] == name]


def _first(node, name: str):
    for x in node:
        if isinstance(x, list) and x and x[0] == name:
            return x
    return None


def _value(node, name: str, default=None):
    x = _first(node, name)
    return x[1] if x is not None and len(x) > 1 else default


def _property(node, name: str) -> Optional[str]:
    for x in _items(node, 'property'):
        if len(x) > 2 and x[1] == name:
            return x[2]
    return None


@dataclass(frozen=True)
class Link:
    """A footprint's link: its path, sheet name and sheet file. units: the
    links of every unit of the part (a multi-unit part's), this one among
    them; empty for a part of one unit."""
    path: str
    sheetname: str
    sheetfile: str
    units: Tuple['Link', ...] = field(default=(), compare=False, repr=False)

    def paths(self) -> set:
        """The paths a footprint of this part may link to: any of its units'."""
        return {u.path for u in self.units} | {self.path}

    def tokens(self, indent: str = '     ') -> str:
        def q(t: str) -> str:
            return '"' + t.replace('\\', '\\\\').replace('"', '\\"') + '"'
        return (f'{indent}(path {q(self.path)})\n{indent}(sheetname {q(self.sheetname)})\n'
                f'{indent}(sheetfile {q(self.sheetfile)})\n')


def schematic_links(folder, name: str) -> Optional[Dict[str, Link]]:
    """The links of the design's converted schematic: {reference: Link}, for
    every symbol on the board (power symbols, '#...', and on_board no left
    out); a multi-unit part's link names its unit 1 (the lowest placed),
    and carries every unit's (Link.units). None when <folder>/<name>.kicad_sch
    isn't there."""
    folder = Path(folder)
    root = folder / f'{name}.kicad_sch'
    if not root.is_file():
        return None
    found: Dict[str, List[Tuple[int, Link]]] = {}
    root_tree = parse(root.read_text(encoding='utf-8', errors='replace'))
    root_uuid = _value(root_tree, 'uuid', '')
    cache: Dict[str, list] = {root.name: root_tree}

    def walk(tree, file_name: str, prefix: str, sheetname: str, depth: int) -> None:
        if depth > 32:
            return
        instance = f'/{root_uuid}{prefix}'
        for sym in _items(tree, 'symbol'):
            if _value(sym, 'lib_id') is None:
                continue
            uid = _value(sym, 'uuid')
            if not uid or _value(sym, 'on_board', 'yes') == 'no':
                continue
            ref, unit = _property(sym, 'Reference'), _value(sym, 'unit', '1')
            for inst in _items(sym, 'instances'):
                for proj in _items(inst, 'project'):
                    for path in _items(proj, 'path'):
                        if len(path) > 1 and path[1] == instance:
                            ref = _value(path, 'reference', ref)
                            unit = _value(path, 'unit', unit)
            if not ref or ref.startswith('#'):
                continue
            try:
                u = int(unit)
            except (TypeError, ValueError):
                u = 1
            link = Link(f'{prefix}/{uid}', sheetname, file_name)
            found.setdefault(ref, []).append((u, link))
        for sheet in _items(tree, 'sheet'):
            uid = _value(sheet, 'uuid')
            child = _property(sheet, 'Sheetfile')
            title = _property(sheet, 'Sheetname') or ''
            if not uid or not child:
                continue
            if child not in cache:
                f = folder / child
                if not f.is_file():
                    continue
                cache[child] = parse(f.read_text(encoding='utf-8', errors='replace'))
            walk(cache[child], child, f'{prefix}/{uid}', f'{sheetname}{title}/', depth + 1)

    walk(root_tree, root.name, '', '/', 0)
    out: Dict[str, Link] = {}
    for ref, units in found.items():
        units.sort(key=lambda x: x[0])
        first = units[0][1]
        out[ref] = first if len(units) == 1 else Link(first.path, first.sheetname, first.sheetfile,
                                                      tuple(link for _, link in units))
    return out


def keep_units(schematic: Dict[str, Link], board: Optional[Dict[str, Optional[str]]]) -> Dict[str, Link]:
    """The links for a board converted again: a part the existing board
    links to another of its units (KiCad's choice, after Update PCB) keeps
    that unit's link; the rest unchanged."""
    if not board:
        return schematic
    out = dict(schematic)
    for ref, link in schematic.items():
        path = board.get(ref)
        if path and path != link.path:
            for unit in link.units:
                if unit.path == path:
                    out[ref] = Link(unit.path, unit.sheetname, unit.sheetfile, link.units)
    return out


_FOOTPRINT = re.compile(r'\(footprint\s')


def board_links(pcb) -> Optional[Dict[str, Optional[str]]]:
    """An existing board's footprints: {reference: its link's path, or None
    without one}. None when the file isn't there."""
    pcb = Path(pcb)
    if not pcb.is_file():
        return None
    tree = parse(pcb.read_text(encoding='utf-8', errors='replace'))
    out: Dict[str, Optional[str]] = {}
    for fp in _items(tree, 'footprint'):
        ref = _property(fp, 'Reference')
        if ref is None:
            ref = _value(fp, 'reference')       # an older layout: (fp_text reference ...)
        if ref and not ref.startswith('#'):
            out[ref] = _value(fp, 'path')
    return out


@dataclass
class Comparison:
    """The schematic's links against a board's: linked (the same path, or
    another unit's of the same part), changed (on both, another path: a
    symbol replaced, or its sheet), and
    the parts on one side only."""
    linked: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    board_only: List[str] = field(default_factory=list)
    schematic_only: List[str] = field(default_factory=list)


def compare(schematic: Dict[str, Link], board: Dict[str, Optional[str]]) -> Comparison:
    c = Comparison()
    for ref in sorted(board, key=_natural):
        if ref not in schematic:
            c.board_only.append(ref)
        elif board[ref] in schematic[ref].paths():
            c.linked.append(ref)
        else:
            c.changed.append(ref)
    c.schematic_only = sorted((r for r in schematic if r not in board), key=_natural)
    return c


def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', ref)]


def report(c: Comparison, other: str, advice: str) -> List[Tuple[str, str]]:
    """The comparison as (level, line)s: the count, then each list. other:
    what was compared with (the board's or the schematic's file)."""
    total = len(c.linked) + len(c.changed) + len(c.board_only)
    lines = [('info', f'Links to the schematic: {len(c.linked)} of {total} footprint(s) linked ({other}).')]
    if c.changed:
        lines.append(('warn', 'linked to another symbol than the schematic now has (the board needs '
                              'converting again): ' + ', '.join(c.changed)))
    if c.board_only:
        lines.append(('note', 'on the board only (not in the schematic): ' + ', '.join(c.board_only)))
    if c.schematic_only:
        lines.append(('note', 'in the schematic only (not on the board): ' + ', '.join(c.schematic_only)))
    if c.changed or c.schematic_only:
        lines.append(('note', advice))
    return lines
