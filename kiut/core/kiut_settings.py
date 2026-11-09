# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/kiut_settings.py -- KIUT's conversion settings: what they are, where they are kept, which
ones a run uses

Every conversion setting lives in a settings file, none on the command line (docs/SETTINGS.md):

- kiut_defaults.ini, next to kiut.py: the default settings (comes with KIUT; edit it to change
  them). Without it, or for a value it can't give, the built-in ones, with a note.
- kiut.ini, next to kiut.py: the last used settings (written by the KIUT window).
- <output folder>/<name>_kiut.ini: the design's own copy, written by its first conversion.

A run uses, without --settings, the design's copy, else kiut.ini, else the defaults;
--settings design|kiut|default forces one (choose). A file holds two sections, [schematic] and
[board], each with its own font (a file of an earlier KIUT with [common] font: that font for both);
other sections (kiut.ini keeps the window's own there) are left alone, their keys' case kept. A value that can't be read keeps its default, with a warning; a number outside its
suggested range is used, with a note.

The settings are built from the converters' own lists (kiuc_writer.TUNING_SPEC,
kiub.BOARD_DEFAULTS_SPEC, kiub.FINE_TUNING_SPEC) plus the switches below, so a value added there
appears in the file and in docs/SETTINGS.md (markdown(); a test keeps the two equal).

    python -m kiut.core.kiut_settings --markdown      the text of docs/SETTINGS.md
    python -m kiut.core.kiut_settings --defaults      the text of kiut_defaults.ini (built-in values)
"""
from __future__ import annotations

import configparser
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kiut import APP_DIR

SETTINGS_FILE = APP_DIR / 'kiut.ini'
DEFAULTS_FILE = APP_DIR / 'kiut_defaults.ini'
SOURCES = ('design', 'kiut', 'default')
SECTIONS = ('schematic', 'board')
OLD_COMMON = 'common'                   # an earlier KIUT's [common] font: read as both fonts
KICAD_FONT = 'KiCad Font'
KICAD_RANGE = "KiCad's"                 # the range shown for a KiCad project setting without a limit
KICAD_LIMIT = "KiCad's limit"           # ... and after the range of one with a limit (Setting.hard)
HEADER = '; KIUT settings -- every setting is explained in docs/SETTINGS.md\n'


@dataclass(frozen=True)
class Setting:
    """One setting: its section and key in the file, its kind and default.
    kind: 'number', 'auto_number' (empty: worked out from the input),
    'yes_no', 'choice' (one of choices), 'text', 'paths' (file names
    separated by ';', relative to the input's folder). name: the code's
    own name for it (TUNING_SPEC's REF_HEIGHT_TUNE, the key itself for
    the others). group: its heading in docs/SETTINGS.md. default: the
    default in use (kiut_defaults.ini's), builtin: the code's own. hard:
    low..high is KiCad's limit -- a value outside it can't be used (it is
    not a value of the setting), where a suggested range only gets a note."""
    section: str
    key: str
    kind: str
    default: object
    text: str
    group: str
    low: Optional[float] = None
    high: Optional[float] = None
    choices: Tuple[str, ...] = ()
    name: str = ''
    builtin: object = None
    hard: bool = False                  # low..high is a hard limit (KiCad's), not a suggestion

    @property
    def code_name(self) -> str:
        return self.name or self.key


def _settings() -> List[Setting]:
    from kiut.schematic.kiuc_writer import TUNING_SPEC
    from kiut.board.kiub import BOARD_DEFAULTS_SPEC, FINE_TUNING_SPEC
    from kiut.board.kiub_v2v3 import STAIRCASE_CEILING_MIL
    s = [Setting('schematic', 'font', 'text', KICAD_FONT, (
        'Font of every text in the schematic: "KiCad Font", KiCad\'s own font, or the family name '
        'of any installed font. "DejaVu Sans Mono" also draws the characters KiCad\'s font lacks '
        '(e.g. the DOS box-drawing characters); "Ulticap Stroke" looks as Ulticap does. A font must '
        'be installed on every computer that opens the KiCad files; DejaVu Sans Mono and Ulticap '
        'Stroke are checked on this one (KiCad Font is used, with a warning, when missing), any '
        'other name is not.'), 'Font')]
    s += [
        Setting('schematic', 'kicad_colors', 'yes_no', False, (
            'yes: KiCad\'s default colour theme instead of Ulticap\'s measured colour palette -- no '
            'explicit colours are written, so every item takes its colour from the KiCad theme in '
            'use.'), 'Schematic'),
        Setting('schematic', 'v5_header', 'yes_no', False, (
            'yes: the .SCH version header is rewritten to V5.00 before reading, for files made with '
            'Ulticap V5.x (Windows) and saved in the old format. Written in the design\'s corrected '
            'copies (<name>_corrected); the originals are never changed. .SCH/.BLK only.'),
            'Schematic'),
        Setting('schematic', 'fix_designators', 'yes_no', True, (
            'yes: designators that don\'t end in a digit (KiCad doesn\'t take them as annotated) are '
            'renamed in corrected copies of the schematic -- and of the design\'s DDF, when there is '
            'one -- in <name>_corrected, and those are converted. no: the design is converted '
            'unchanged, with a warning and a log of the designators.'), 'Schematic'),
        Setting('schematic', 'allow_missing_sheets', 'yes_no', False, (
            'yes: a sub-sheet the hierarchy refers to that isn\'t there is left out and the rest '
            'converted. no: the conversion stops. A known cause is a double-dot or double-extension '
            'file name left by a Ulticap V5.72 re-save ("SHEET2..SCH" or "SHEET2.SCH.SCH" instead of '
            '"SHEET2.SCH").'), 'Schematic'),
        Setting('schematic', 'block_library', 'yes_no', False, (
            'yes: a .SCH (with its sub-sheets) converts to a KiCad block library, '
            '<name>.kicad_blocks in KIUT\'s Blocks folder, instead of a schematic project: no .kicad_pro or '
            '.kicad_wks, title symbols left out. A .BLK always converts to a block library. Not for a '
            '.UTSCH.'), 'Schematic'),
        Setting('schematic', 'label_keep_alignment', 'yes_no', False, (
            'A net label sits where KiCad can connect it -- a wire end or a point on the wire -- '
            'which is not always where Ulticap anchored it. no: the text stays on the side of that '
            'point where Ulticap drew it (its justification turned if need be). yes: Ulticap\'s '
            'justification kept, the text then possibly on the other side of the point. '
            'anchor_wire_max_dist applies either way.'), 'Schematic'),
    ]
    for name, default, lo, hi, text, cat in TUNING_SPEC:
        s.append(Setting('schematic', name.lower(), 'number', default, text,
                         'Schematic: KiCad project (new projects only)' if cat == 'kicad' else
                         'Schematic fine tuning' + (' (Ulticap line widths)' if cat == 'ulticap' else ''),
                         lo, hi, name=name, hard=cat == 'kicad' and lo is not None))
    s.append(Setting('board', 'font', 'text', KICAD_FONT, (
        'Font of every text on the board: "KiCad Font", KiCad\'s own font, or the family name of any '
        'installed font; "Ultiboard Stroke" looks as Ultiboard does. The text sizes follow '
        'font_height_ratio and font_width_ratio. A font must be installed on every computer that '
        'opens the KiCad files.'), 'Font'))
    s.append(Setting('board', 'library_only', 'choice', 'ask', (
        'A DDF that places components but holds no nets, copper or vias may be a design or a shape '
        'library. ask: the question is asked (yes, the command line\'s --yes: a design); yes: '
        'converted as a library only (the library board and the footprint library, no design '
        'board); no: as a design. A DDF holding shapes only is always a library.'), 'Board',
        choices=('ask', 'yes', 'no')))
    s.append(Setting('board', 'tec_split', 'paths', [], (
        'An Ultiboard library\'s technology as a multi-file set (versions before V4.60), in this '
        'order: TOP; BOTTOM; INNER; DRILLS[; TRACES] -- the top, bottom and inner pad tables, the '
        'drill table and, optionally, the trace table; the same file may serve several pad tables. '
        'File names relative to the library\'s folder, separated by ";". Empty: the technology is '
        'the .TEC file given with the library (V4.60 and later).'), 'Board'))
    for name, default, lo, hi, text, target in BOARD_DEFAULTS_SPEC:
        s.append(Setting('board', name, 'number', default, f'{text} Written to the .{target}.',
                         'Board defaults', lo, hi))
    for name, default, lo, hi, text, cat in FINE_TUNING_SPEC:
        s.append(Setting('board', name, 'number', default, text,
                         'Board V2/V3' if name.startswith('v2v3_') else f'Board fine tuning ({cat})',
                         lo, hi))
    s += [
        Setting('board', 'v2v3_staircase_merge', 'yes_no', True, (
            'DDF V2/V3 only: staircase traces (a diagonal drawn as grid steps) recovered as '
            'diagonal traces.'), 'Board V2/V3'),
        Setting('board', 'v2v3_staircase_limit_mil', 'auto_number', None, (
            f'DDF V2/V3 only: the longest single staircase step recovered as a diagonal, mil. '
            f'Empty: the file\'s own declared grid step. Never more than '
            f'{STAIRCASE_CEILING_MIL:g} mil.'), 'Board V2/V3', 0.0, STAIRCASE_CEILING_MIL),
        Setting('board', 'v2v3_chamfer', 'yes_no', True, (
            'DDF V2/V3 only: ordinary 90-degree trace corners chamfered (slanted).'), 'Board V2/V3'),
        Setting('board', 'v2v3_corner_slant_limit_mil', 'auto_number', None, (
            'DDF V2/V3 only: the most trimmed off each leg of a 90-degree corner when chamfering '
            'it, mil. Empty: as v2v3_staircase_limit_mil.'), 'Board V2/V3', 0.0, STAIRCASE_CEILING_MIL),
    ]
    return s


_CACHE: List[Setting] = []
# What reading kiut_defaults.ini found wrong: (level, text) each, shown with every run's settings.
DEFAULTS_MESSAGES: List[Tuple[str, str]] = []


def settings() -> List[Setting]:
    """Every setting, in the file's order, its default kiut_defaults.ini's
    (the built-in one where the file doesn't give one it can use)."""
    if not _CACHE:
        built = [replace(st, builtin=st.default) for st in _settings()]
        DEFAULTS_MESSAGES[:] = []
        given = _read_defaults(built, DEFAULTS_FILE, DEFAULTS_MESSAGES)
        _CACHE.extend(replace(st, default=given[(st.section, st.key)]) if (st.section, st.key) in given else st
                      for st in built)
    return list(_CACHE)


def reload_defaults() -> None:
    """Read kiut_defaults.ini again (the next settings())."""
    _CACHE.clear()


def _read_defaults(built: List[Setting], path: Path,
                   messages: List[Tuple[str, str]]) -> Dict[Tuple[str, str], object]:
    """kiut_defaults.ini's values, {(section, key): value}; what it can't
    give noted in messages (the built-in value used)."""
    if not path.is_file():
        messages.append(('note', f'{path.name} is missing ({path}); the built-in defaults are used.'))
        return {}
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding='utf-8') as f:
            cfg.read_file(f)
    except (OSError, UnicodeDecodeError, configparser.Error) as e:
        messages.append(('note', f'{path.name} could not be read ({e}); the built-in defaults are used.'))
        return {}
    known = {(st.section, st.key): st for st in built}
    given: Dict[Tuple[str, str], object] = {}
    for sec in cfg.sections():
        for key, text in cfg.items(sec):
            st = known.get((sec, key))
            if st is None:
                messages.append(('note', f'{path.name}: [{sec}] {key} is not a KIUT setting; ignored.'))
                continue
            try:
                given[(sec, key)] = _value_of(st, text)
            except ValueError as e:
                messages.append(('note', f'{path.name}: [{sec}] {key} = {text!r} is not {e}; the built-in '
                                         f'default ({text_of(st, st.default) or "empty"}) is used.'))
    return given


def _comment(text: str, width: int = 96) -> str:
    """A setting's explanation cut to one comment line: its first sentence,
    shortened at a word when longer than width."""
    first = re.split(r'(?<!e\.g)(?<!i\.e)(?<!vs)\.\s+(?=[A-Z0-9"(])', text)[0].rstrip('.') + '.'
    if len(first) > width:
        first = first[:width].rsplit(' ', 1)[0].rstrip(',;:-') + ' ...'
    return first


DEFAULTS_HEADER = ('; KIUT default settings -- what "built-in defaults", --settings default and the window\'s\n'
                   '; reset use. Change a value to change a default; a setting left out, or a value that\n'
                   '; can\'t be read, takes the code\'s own (with a note). Every setting is explained in\n'
                   '; docs/SETTINGS.md.\n')


def defaults_text() -> str:
    """kiut_defaults.ini as it comes with KIUT: every setting at its
    built-in value, a one-line comment above it."""
    lines = [DEFAULTS_HEADER]
    for sec in SECTIONS:
        lines.append(f'\n[{sec}]\n')
        for st in (st for st in settings() if st.section == sec):
            lines.append(f'; {_comment(st.text)}\n{st.key} = {text_of(st, st.builtin)}\n')
    return ''.join(lines)


def _find(section: str, key: str) -> Setting:
    for st in settings():
        if st.section == section and st.key == key:
            return st
    raise KeyError(f'{section}.{key}')


def defaults() -> Dict[str, Dict[str, object]]:
    """The default values (kiut_defaults.ini's, else built-in): {section:
    {key: value}}."""
    values: Dict[str, Dict[str, object]] = {sec: {} for sec in SECTIONS}
    for st in settings():
        values[st.section][st.key] = list(st.default) if st.kind == 'paths' else st.default
    return values


def design_copy(output_dir, name: str) -> Path:
    """The design's own settings file: <output folder>/<name>_kiut.ini --
    in a folder shared by every Ulticap symbol library converted there
    (KIUT's Symbols folder, kiut.core.folders; Ulticap_lib_KiCad next to
    the libraries), Ulticap_lib_kiut.ini. A block library keeps its own,
    <name>_kiut.ini in Blocks: a .SCH's block_library is one of its
    settings."""
    from kiut.core import folders
    output_dir = Path(output_dir)
    shared = output_dir.name.lower() == 'ulticap_lib_kicad'
    f = folders.library_folder('symbols')
    if f is not None and output_dir.name.lower() == f.name.lower() and _same(output_dir, f):
        shared = True
    if shared:
        return output_dir / 'Ulticap_lib_kiut.ini'
    return output_dir / f'{name}_kiut.ini'


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


# ── reading and writing ──────────────────────────────────────────────────────

def text_of(st: Setting, value) -> str:
    """A value as the file holds it."""
    if st.kind == 'yes_no':
        return 'yes' if value else 'no'
    if st.kind in ('number', 'auto_number'):
        if value is None:
            return ''
        text = repr(float(value))           # the shortest text that reads back exactly
        return text[:-2] if text.endswith('.0') else text
    if st.kind == 'paths':
        return '; '.join(value)
    return str(value)


def _value_of(st: Setting, text: str):
    """A file's text as the setting's value; ValueError when it isn't one."""
    t = text.strip()
    if st.kind == 'yes_no':
        low = t.lower()
        if low in ('yes', 'true', 'on', '1'):
            return True
        if low in ('no', 'false', 'off', '0'):
            return False
        raise ValueError('yes or no')
    if st.kind == 'auto_number' and not t:
        return None
    if st.kind in ('number', 'auto_number'):
        try:
            value = float(t)
        except ValueError:
            raise ValueError('a number (with a point: 0.25)') from None
        if st.hard and not st.low <= value <= st.high:
            raise ValueError(f'from {st.low:g} to {st.high:g} ({KICAD_LIMIT})')
        return value
    if st.kind == 'choice':
        if t.lower() not in st.choices:
            raise ValueError(' or '.join(st.choices))
        return t.lower()
    if st.kind == 'paths':
        return [p.strip() for p in t.split(';') if p.strip()]
    if not t:
        raise ValueError('a name')
    return t


# Settings an earlier KIUT wrote that this one no longer has: skipped without a word when a file
# (kiut.ini, a design's copy) still holds them -- the ratio rule for a label's justification along
# its wire (KIUC's), replaced by the side its text lies on (kiuc_writer._reading_axis_justify).
RETIRED = {('schematic', 'along_wire_centre_lo'), ('schematic', 'along_wire_centre_hi')}
# An earlier KIUT's (and KIUC's) one text-height multiplier: in a file without the multipliers that
# replaced it, its value is used for each of them.
SPLIT = {('schematic', 'height_tune'): ('ref_height_tune', 'value_height_tune', 'field_height_tune',
                                        'text_height_tune', 'label_height_tune')}


def split_old(section: str, key: str, text: str, holds) -> List[str]:
    """An old setting split into new ones (SPLIT): the new keys the file
    doesn't hold itself (holds(key)) that take its text."""
    return [k for k in SPLIT.get((section, key), ()) if not holds(k)]


def read(path) -> Tuple[Dict[str, Dict[str, object]], List[Tuple[str, str]]]:
    """The settings in the file at path, over the defaults: (values,
    messages), messages as (level, text) -- 'warn' for a value that can't
    be read (its default kept) or a key KIUT doesn't know (ignored), 'note'
    for a number outside its suggested range (used). A setting the file
    doesn't hold keeps its default (a file written by an earlier KIUT)."""
    path = Path(path)
    values = defaults()
    messages: List[Tuple[str, str]] = []
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding='utf-8') as f:
            cfg.read_file(f)
    except (OSError, UnicodeDecodeError, configparser.Error) as e:
        messages.append(('warn', f'{path.name} could not be read ({e}); the built-in defaults are used.'))
        return values, messages
    known = {(st.section, st.key): st for st in settings()}
    if cfg.has_section(OLD_COMMON):
        for key, text in cfg.items(OLD_COMMON):
            if key != 'font' or not text.strip():
                messages.append(('warn', f'{path.name}: [{OLD_COMMON}] {key} is not a KIUT setting; '
                                         f'ignored.'))
                continue
            for sec in SECTIONS:
                if not (cfg.has_section(sec) and cfg.has_option(sec, 'font')):
                    values[sec]['font'] = text.strip()
            messages.append(('note', f'{path.name}: [{OLD_COMMON}] font (an earlier KIUT\'s) used for '
                                     f'the schematic and the board; it is written as [schematic] font '
                                     f'and [board] font from now on.'))
    for sec in SECTIONS:
        if not cfg.has_section(sec):
            continue
        for key, text in cfg.items(sec):
            st = known.get((sec, key))
            if st is None and (sec, key) in SPLIT:
                taking = split_old(sec, key, text, lambda k, sec=sec: cfg.has_option(sec, k))
                for k in taking:
                    try:
                        values[sec][k] = _value_of(_find(sec, k), text)
                    except ValueError:
                        pass
                if taking:
                    messages.append(('note', f'{path.name}: [{sec}] {key} (an earlier version\'s) used for '
                                             f'{", ".join(taking)}.'))
                continue
            if st is None:
                if (sec, key) not in RETIRED:     # an earlier KIUT's, retired: passed over quietly
                    messages.append(('warn', f'{path.name}: [{sec}] {key} is not a KIUT setting; ignored.'))
                continue
            try:
                value = _value_of(st, text)
            except ValueError as e:
                messages.append(('warn', f'{path.name}: [{sec}] {key} = {text!r} is not {e}; '
                                         f'the default ({text_of(st, st.default) or "empty"}) is used.'))
                continue
            if (st.kind in ('number', 'auto_number') and value is not None and st.low is not None
                    and not st.low <= value <= st.high):
                messages.append(('note', f'{path.name}: [{sec}] {key} = {format(value, "g")} is outside '
                                         f'the suggested range {st.low:g} to {st.high:g}; used as given.'))
            values[sec][key] = value
    return values, messages


def _others() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str                   # type: ignore[assignment,method-assign]  # keys as written
    return cfg


def other_sections(path) -> configparser.ConfigParser:
    """A settings file's sections other than the settings' (kiut.ini: the
    window's own), keys as written; empty when there is no file."""
    cfg = _others()
    if Path(path).exists():
        try:
            with open(path, encoding='utf-8') as f:
                cfg.read_file(f)
        except (OSError, UnicodeDecodeError, configparser.Error):
            cfg = _others()
    for sec in SECTIONS + (OLD_COMMON,):
        cfg.remove_section(sec)
    return cfg


def write(path, values: Dict[str, Dict[str, object]],
          extra: Optional[Dict[str, Dict[str, str]]] = None) -> None:
    """Write values (every setting; one missing from values: its default)
    as a settings file. A kiut.ini's other sections (the window's own) are
    kept as they are; extra sets whole sections of those ({section: {key:
    text}}, replacing the section)."""
    path = Path(path)
    others = other_sections(path)
    for sec, items in (extra or {}).items():
        others.remove_section(sec)
        others.add_section(sec)
        for key, text in items.items():
            others.set(sec, key, str(text))
    lines = [HEADER]
    for sec in SECTIONS:
        lines.append(f'\n[{sec}]\n')
        for st in (st for st in settings() if st.section == sec):
            value = values.get(sec, {}).get(st.key, st.default)
            lines.append(f'{st.key} = {text_of(st, value)}\n')
    for sec in others.sections():
        lines.append(f'\n[{sec}]\n')
        for key, text in others.items(sec, raw=True):
            lines.append(f'{key} = {text}\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(lines), encoding='utf-8')


# ── which settings a run uses ────────────────────────────────────────────────

@dataclass
class Chosen:
    """The settings a run uses: values ({section: {key: value}}), where
    they came from (source: 'design', 'kiut' or 'default'; path: the file,
    None for the defaults) and the messages to show, (level, text) each --
    the first one names the source."""
    values: Dict[str, Dict[str, object]]
    source: str
    path: Optional[Path]
    messages: List[Tuple[str, str]] = field(default_factory=list)

    def get(self, section: str, key: str):
        return self.values[section][key]


def choose(source: Optional[str], design_file: Optional[Path], kiut_file: Optional[Path] = None) -> Chosen:
    """The settings a run uses. source None (no --settings): the design's
    copy (design_file) when it exists, else kiut.ini (kiut_file, default
    SETTINGS_FILE), else the built-in defaults. 'design', 'kiut' or
    'default' forces one; a forced file that doesn't exist: the defaults,
    with a warning."""
    kiut_file = Path(kiut_file) if kiut_file is not None else SETTINGS_FILE
    design_file = Path(design_file) if design_file is not None else None
    if source not in (None,) + SOURCES:
        raise ValueError(f'source: one of {", ".join(SOURCES)}')
    messages: List[Tuple[str, str]] = []
    if source is None:
        if design_file is not None and design_file.is_file():
            source = 'design'
        elif kiut_file.is_file():
            source = 'kiut'
        else:
            source = 'default'
    elif source == 'design' and not (design_file is not None and design_file.is_file()):
        where = f' ({design_file})' if design_file is not None else ''
        messages.append(('warn', f'The design has no settings of its own yet{where}; '
                                 f'the built-in defaults are used.'))
        source = 'default'
    elif source == 'kiut' and not kiut_file.is_file():
        messages.append(('warn', f'There is no {kiut_file.name} yet ({kiut_file}); '
                                 f'the built-in defaults are used.'))
        source = 'default'
    settings()
    messages += DEFAULTS_MESSAGES
    if source == 'default':
        return Chosen(defaults(), 'default', None, [('info', 'Settings: built-in defaults')] + messages)
    path = design_file if source == 'design' else kiut_file
    values, read_messages = read(path)
    what = "the design's own" if source == 'design' else 'last used'
    return Chosen(values, source, path, [('info', f'Settings: {path} ({what})')] + messages + read_messages)


# ── docs/SETTINGS.md ─────────────────────────────────────────────────────────

_INTRO = '''# KIUT settings

Every conversion setting lives in a settings file; the command line takes the input files and
which settings file to use, nothing else (and `--fix-only`, the designator fix without a
conversion):

    python kiut.py <file(s)> [--settings design|kiut|default] [--yes] [-q] [--fix-only]

## The files

| File | Where | Written |
|---|---|---|
| `kiut_defaults.ini` | next to `kiut.py` | comes with KIUT: the defaults -- every setting with its value and a one-line comment; change a value to change a default (without the file, or for a value it can't give, the code's own, with a note) |
| `kiut.ini` | next to `kiut.py` | by the KIUT window: the current settings (the last used) |
| `<name>_kiut.ini` | in the design's output folder, `<name>_KiCad`; one for a schematic and its board (in KIUT's library folder: Ulticap symbol libraries `Symbols/Ulticap_lib_kiut.ini`, one for every library there; a block library `Blocks/<name>_kiut.ini`; an Ultiboard library `Footprints/<name>_lib_KiCad/<name>_kiut.ini`) | by the design's first conversion; after that, only by the window's *Save current settings* |

Neither of the last two has to be there: without them, the defaults. `kiut.ini` also keeps the window's
own sections, among them `[folders]` `libraries`: KIUT's library folder, where library-only
conversions go (Documents/KIUT when not set; the window's *File → Library output folder…*; the
environment variable `KIUT_LIBRARY_FOLDER` overrides it). kiut.py reads it too. The viewer keeps its own settings in
`kiut_viewer.ini` ([VIEWER.md](VIEWER.md)); they never change a conversion.

## Which settings a run uses

Without `--settings`: the design's own `<name>_kiut.ini` when there is one, else `kiut.ini`, else
the built-in defaults. `--settings design`, `--settings kiut` or `--settings default` uses that
one; a file that isn't there yet gives the built-in defaults, with a warning. The output's first
line says which settings were used.

## The format

A plain text file in two sections, `[schematic]` and `[board]`, one `key = value` per line; a line
starting with `;` is a comment. A setting a file doesn't hold keeps its default, so an older file
still works -- one with a `[common]` section holding `font` (an earlier KIUT's) gives that font to
both, with a note.

- **yes / no** settings take `yes` or `no` (also `true`/`false`, `on`/`off`, `1`/`0`).
- **Numbers** are written with a point: `0.254`. A number outside its suggested range is used,
  with a note: the range is what was found to work, not a limit.
- **Empty** means "worked out from the input" where a setting says so.
- A value that can't be read keeps its default, with a warning; so does a misspelt key.
'''


def _cell(text: str) -> str:
    return text.replace('|', '\\|').replace('\n', ' ')


def markdown() -> str:
    """The text of docs/SETTINGS.md."""
    out = [_INTRO]
    group = None
    section = None
    for st in settings():
        if st.section != section:
            section = st.section
            out.append(f'\n## [{section}]\n')
        if st.group != group:
            group = st.group
            out.append(f'\n### {group}\n\n| Setting | Default | Suggested range | What it does |\n'
                       f'|---|---|---|---|\n')
        default = f'`{text_of(st, st.default)}`' if text_of(st, st.default) else '*empty*'
        if st.kind == 'choice':
            rng = ' / '.join(f'`{c}`' for c in st.choices)
        elif st.low is not None:
            rng = f'{st.low:g} to {st.high:g}' + (f' ({KICAD_LIMIT})' if st.hard else '')
        elif 'KiCad project' in st.group:
            rng = KICAD_RANGE
        elif st.kind == 'yes_no':
            rng = '`yes` / `no`'
        else:
            rng = ''
        out.append(f'| `{st.key}` | {default} | {rng} | {_cell(st.text)} |\n')
    from kiut.core import docs_nav               # its Contents and up links, as every document's
    return docs_nav.navigate(''.join(out), docs_nav.up_link(APP_DIR / 'docs' / 'SETTINGS.md', APP_DIR))


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--markdown']:
        sys.stdout.write(markdown())
    elif sys.argv[1:] == ['--defaults']:
        sys.stdout.write(defaults_text())
    else:
        sys.stdout.write(__doc__)
