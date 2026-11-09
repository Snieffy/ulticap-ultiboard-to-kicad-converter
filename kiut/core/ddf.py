# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/ddf.py -- What both converters read from an Ultiboard DDF alike

KIUB converts the DDF; KIUC reads the design's DDF too: the footprint each
part is placed with names its symbol's Footprint field (the board decides,
CORRECTED_FOLDER.md section 8), and Refdes Reannotate renames a component in
the DDF's corrected copy when it renames the symbol. Both must read the DDF
the same way, so a footprint gets the same name on the board, in the
library and in the schematic. That is here (FILEFORMAT_ULTIBOARD_DDF.md describes the
records):

- lines: the text as both read it (no carriage return at either end of a
  line: some DDFs hold a stray CR before a record);
- shape_names, footprint_names: the shapes a DDF defines (*S), and the
  footprint name each gets;
- component_shapes, component_refdes, non_digit_refdes: its components
  (*C <refdes> /<alias> <shape>) and their designators;
- rename_component: a component's designator changed in the DDF text, the
  rest of it byte for byte as it was.

Until KIUT stage 4 KIUC had its own copies of these (kiuc_writer,
kiuc_refdes), held equal to KIUB's (kiub_library, kiub.py) by a test.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from kiut.core.kicad_project import kicad_name


def lines(ddf_text: str) -> str:
    """The DDF text with LF line ends and no carriage return at either end
    of a line, as KIUB reads it: some DDFs hold a stray CR before a record
    ('\\r\\n\\r*S...')."""
    return '\n'.join(ln.strip('\r') for ln in ddf_text.split('\n'))


def shape_names(ddf_text: str) -> List[str]:
    """The shapes a DDF defines, as KIUB lays them out in a library (BOARD
    and *.BAK left out), in file order, each name once."""
    names: List[str] = []
    for m in re.finditer(r'(?m)^\*S(.*)$', lines(ddf_text)):
        name = m.group(1).strip()
        if name and name != 'BOARD' and not name.endswith('.BAK') and name not in names:
            names.append(name)
    return names


def footprint_names(names: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """The footprint name for every shape name (kicad_name's rule), and a
    report line per renamed shape. Names differing only in letter case
    would be one file on Windows and macOS: a later one gets '_2', '_3',
    ..."""
    result: Dict[str, str] = {}
    used: set = set()
    renamed: List[str] = []
    for name in names:
        base = kicad_name(name)
        fp, n = base, 1
        while fp.lower() in used:
            n += 1
            fp = f'{base}_{n}'
        used.add(fp.lower())
        result[name] = fp
        if fp != name:
            renamed.append(f'{name} -> {fp}')
    return result, renamed


def component_shapes(ddf_text: str) -> Dict[str, str]:
    """{reference designator: shape name} of every component a DDF places
    (*C <refdes> /<alias> <shape>; the alias never holds a space, a shape
    name may)."""
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r'(?m)^\*C (\S+) /\S* (.+)$', lines(ddf_text))}


# A component record's designator; a stray CR may stand before the record.
_COMPONENT = re.compile(r'^\r*\*C (\S+) ', re.MULTILINE)


def component_refdes(ddf_text: str) -> List[str]:
    """Every component designator of a DDF (its *C records), in first-seen
    order, each once."""
    seen: List[str] = []
    found: set = set()
    for m in _COMPONENT.finditer(ddf_text):
        rd = m.group(1)
        if rd not in found:
            found.add(rd)
            seen.append(rd)
    return seen


def non_digit_refdes(ddf_text: str) -> List[str]:
    """The component designators that do not end in a digit (KiCad treats
    them as not annotated), in first-seen order, each once. A board
    component has no units, so no trailing letter after a digit counts as
    annotated here (unlike a schematic's gates U2A, U2B)."""
    return [rd for rd in component_refdes(ddf_text) if not rd[-1].isdigit()]


def rename_component(ddf_text: str, old: str, new: str) -> Tuple[str, bool]:
    """The DDF text with component old's designator in its *C record made
    new, every other byte as it was (line ends, a stray CR before the
    record). Only the *C record: a *X text showing the designator is plain
    silkscreen text, not what the component is bound to. Returns (text,
    found): found is False when no *C record of old was there."""
    pattern = re.compile(r'(^\r*\*C )' + re.escape(old) + r'(\s)', re.MULTILINE)
    text, n = pattern.subn(lambda m: m.group(1) + new + m.group(2), ddf_text)
    return text, n > 0
