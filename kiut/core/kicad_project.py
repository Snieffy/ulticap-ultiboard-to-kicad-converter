# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/kicad_project.py -- The KiCad project files both converters write

A design's KiCad project folder, <name>_KiCad, is shared: KIUC writes the
schematic and the symbol library, KIUB the board and the footprint library,
both the one <name>.kicad_pro (CORRECTED_FOLDER.md, sections 7 and 8). What
both do alike is here:

- kicad_name: a design, library or shape name as a KiCad library nickname
  or footprint name, so one design gets one nickname in both tables;
- lib_table / update_lib_table: an entry added to or replaced in the
  folder's sym-lib-table, fp-lib-table or design-block-lib-table, every
  other entry kept, a file that isn't such a table left alone; lib_source:
  the file an entry was converted from, as its description records it;
- start_project / write_project: a .kicad_pro as KiCad 9 starts one, and
  written as KiCad writes it (keys sorted, indent 2, a final newline).

A .kicad_pro KiCad starts holds a part both its editors write, and a part
of each editor's own (the author's test, 2026-10-31: a blank schematic and a
blank board saved without a project, each in a folder of its own):

- the schematic editor's: erc, schematic (but legacy_lib_dir and
  legacy_lib_list), sheets;
- the board editor's: board.design_settings;
- both: everything else (meta, net_settings, pcbnew, libraries, ...).

kicad_pro_template.json is a KiCad 9.0.7 project of both. A conversion
takes the existing .kicad_pro, or the common part when there is none (or
it can't be read), adds its editor's part where anything of it is missing,
then sets its own keys (kiuc_writer.write_kicad_pro,
kiub.Converter.write_kicad_pro). So a schematic converted alone gets
KiCad's schematic-only layout, a board alone KiCad's board-only one, and
both the whole template, the same file whichever tool runs first. The
project file is never replaced, only updated: nothing either tool doesn't
own is changed, a key is only ever added where it is missing.

Until KIUT stage 4 each tool had its own copy (kiuc_writer.sym_lib_table and
library_nickname, kiub_library.fp_lib_table and kicad_name).
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Optional, Tuple

SYM_LIB_TABLE = 'sym-lib-table'
FP_LIB_TABLE = 'fp-lib-table'
DESIGN_BLOCK_LIB_TABLE = 'design-block-lib-table'

# What a nickname or footprint name can't hold: KiCad's item separator ':',
# path separators and what Windows file names refuse.
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def kicad_name(name: str) -> str:
    """A design, library or shape name as a KiCad library nickname or
    footprint name: every character KiCad or a file name refuses becomes
    '_'."""
    return _ILLEGAL.sub('_', name) or '_'


# A table's kind: its file name, its first token, and what it lists.
_TABLES = {
    'sym': (SYM_LIB_TABLE, 'sym_lib_table', 'symbol'),
    'fp': (FP_LIB_TABLE, 'fp_lib_table', 'footprint'),
    'dbl': (DESIGN_BLOCK_LIB_TABLE, 'design_block_lib_table', 'design block'),   # KiCad 9 (stage 10e)
}

SOURCE_PREFIX = 'Converted by KIUT from '


def source_descr(source) -> str:
    """A table entry's description naming the file a library was converted
    from (forward slashes, no quotes: a KiCad string)."""
    return f'{SOURCE_PREFIX}{str(source).replace(chr(92), "/").replace(chr(34), "")}'


def lib_source(folder, kind: str, nickname: str) -> Optional[str]:
    """The file the library nickname in folder's table of this kind was
    converted from, as its entry's description records it; None when the
    table, the entry or the record isn't there."""
    try:
        text = (Path(folder) / _TABLES[kind][0]).read_text(encoding='utf-8')
    except OSError:
        return None
    m = re.search(r'\(name "' + re.escape(nickname) + r'"\).*?\(descr "' + re.escape(SOURCE_PREFIX)
                  + r'([^"]*)"\)', text)
    return m.group(1) if m else None


def lib_table(kind: str, existing: Optional[str], nickname: str, uri: str,
              descr: str) -> Tuple[Optional[str], str]:
    """A library table (kind 'sym', 'fp' or 'dbl') with the entry nickname -> uri
    added or replaced, every other entry kept. Returns (text, what was
    done); text is None when an existing file is not a table of this kind,
    or not laid out as KiCad writes it: it is then left alone."""
    _, token, what = _TABLES[kind]
    entry = (f'\t(lib (name "{nickname}")(type "KiCad")(uri "{uri}")(options "")'
             f'(descr "{descr}"))')
    if existing is None or not existing.strip():
        return f'({token}\n\t(version 7)\n{entry}\n)\n', 'created'
    if not existing.lstrip().startswith(f'({token}'):
        return None, f'not a {what} library table: left unchanged'
    lines = existing.rstrip('\n').split('\n')
    mine = [i for i, ln in enumerate(lines) if f'(name "{nickname}")' in ln]
    if mine:
        if lines[mine[0]].strip() == entry.strip():
            return existing, 'unchanged'
        lines[mine[0]] = entry
        return '\n'.join(lines) + '\n', 'updated'
    closing = [i for i, ln in enumerate(lines) if ln.strip() == ')']
    if not closing:                                 # KiCad closes the table on a line of its own
        return None, 'not laid out as KiCad writes it: left unchanged'
    lines.insert(closing[-1], entry)
    return '\n'.join(lines) + '\n', 'updated'


def update_lib_table(folder, kind: str, nickname: str, uri: str,
                     descr: str) -> Tuple[Path, str, bool]:
    """List nickname -> uri in folder's table of this kind (lib_table),
    written only when it changes. Returns (the table's path, what was done,
    whether the table lists the library)."""
    table = Path(folder) / _TABLES[kind][0]
    try:
        existing = table.read_text(encoding='utf-8')
    except OSError:
        existing = None
    text, done = lib_table(kind, existing, nickname, uri, descr)
    if text is not None and text != existing:
        table.write_text(text, encoding='utf-8')
    return table, done, text is not None


TEMPLATE_FILE = Path(__file__).with_name('kicad_pro_template.json')
SCHEMATIC = 'schematic'
BOARD = 'board'


def template() -> dict:
    """A KiCad 9.0.7 project of both editors (a fresh copy)."""
    with open(TEMPLATE_FILE, encoding='utf-8') as f:
        return json.load(f)


def _split(t: dict) -> Tuple[dict, dict, dict]:
    """The template as (both editors' part, the schematic editor's, the board
    editor's)."""
    common = copy.deepcopy(t)
    sch = {'erc': common.pop('erc'),
           'schematic': {k: v for k, v in common['schematic'].items()
                         if k not in ('legacy_lib_dir', 'legacy_lib_list')}}
    common['schematic'] = {k: common['schematic'][k] for k in ('legacy_lib_dir', 'legacy_lib_list')}
    board = {'board': {'design_settings': common['board'].pop('design_settings')}}
    return common, sch, board


def _add_missing(data: dict, part: dict) -> None:
    """Every key of part that data lacks, added; nothing data holds changed."""
    for key, value in part.items():
        if key not in data:
            data[key] = copy.deepcopy(value)
        elif isinstance(data[key], dict) and isinstance(value, dict):
            _add_missing(data[key], value)


def start_project(path, project_name: str, editor: str) -> Tuple[dict, str]:
    """The .kicad_pro at path to update for a conversion by editor
    (SCHEMATIC or BOARD): the existing one, or KiCad's common part when
    there is none or it can't be read; with the editor's part added where
    anything of it is missing, and meta.filename set. Returns (data, what
    was found: 'new', 'existing' or 'unreadable')."""
    data, unreadable = read_project(path)
    common, sch, board = _split(template())
    if data is None:
        found, data = ('unreadable' if unreadable else 'new'), common
    else:
        found = 'existing'
        _add_missing(data, common)
    _add_missing(data, sch if editor == SCHEMATIC else board)
    data.setdefault('meta', {})['filename'] = f'{project_name}.kicad_pro'
    return data, found


def read_project(path) -> Tuple[Optional[dict], bool]:
    """A .kicad_pro as (its data, unreadable): (None, False) when there is
    none, (None, True) when it can't be read or isn't JSON -- each tool then
    decides what to start from."""
    path = Path(path)
    if not path.exists():
        return None, False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f), False
    except (json.JSONDecodeError, OSError):
        return None, True


def write_project(path, data: dict) -> None:
    """Write a .kicad_pro as KiCad writes it: JSON, keys sorted, indent 2, a
    final newline."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write('\n')
