# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/cli.py -- KIUT on the command line: python kiut.py <file(s)> [--settings ...] [--yes] [-q]
[--fix-only]

Converts one kind of input per run -- or a schematic and its board together. A design goes next to
its originals (<name>_KiCad); a library-only run into KIUT's library folder (kiut.core.folders:
Documents/KIUT, its Symbols, Blocks or Footprints):

- a schematic: .SCH sheets (the main sheet; sub-sheets are found by themselves), a .BLK (to a
  KiCad block library; a .SCH too with block_library = yes), or one .UTSCH;
- Ulticap symbol libraries: one or more .UTLIB or DOS .LIB;
- a board: one .DDF (the extension may be left out);
- Ultiboard libraries (.LIB, .L55) with their technology: a .TEC given with them, or a split
  technology set in the settings (tec_split); several with one technology after a warning; library
  DDFs (shapes only, or components without nets, asked once for all) with them or on their own,
  each with the technology it holds;
- a schematic and its board: X.SCH (or X.UTSCH) and X.DDF of one design. The schematic first: one
  designator check, its fix (fix_designators) renaming them in the schematic's and the DDF's
  corrected copies; then the board, from that copy -- the same designators on both sides.

The board's designators that don't end in a digit: with its schematic in the run, the schematic's
decision holds (no question); a board converted alone while its schematic is there gets a question
(default no) and the advice to convert both together; without a schematic they are kept, with a
note (KiCad's board editor accepts them).

--fix-only: the designator fix only, as Refdes Reannotate did: the corrected copies written into
<name>_corrected, nothing converted; for a schematic, or a schematic and its board.

Every conversion setting comes from a settings file (docs/SETTINGS.md, kiut.core.kiut_settings):
without --settings, the design's own <name>_kiut.ini, else kiut.ini, else the built-in defaults.
A design's first conversion writes its <name>_kiut.ini with the settings used; kiut.ini is never
written here (the KIUT window keeps it).

The questions are asked on the console ([y/N], a piped answer is read too; no answer to read:
the default, said); --yes answers each one "go on" (the corrected-copies question: its default);
-q shows nothing and asks nothing (each question its default), the run's report going to
<output folder>/<name>.log instead. A name with * or ? is expanded here (kiut.py libs/*.DDF, on
Windows too). Exit codes: 0 done, 1 refused, cancelled or failed, 2 a wrong
command line.

The conversions are kiut.schematic.kiuc.SchematicJob and kiut.board.kiub.BoardJob (stage 5);
this module only reads the command line and the settings, and asks. The KIUT window (kiut.gui.app)
runs the same parts: classify and check (InputError: what is wrong with the files given), and
convert, with its dialogs as the `asking` object.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path
from typing import Optional

from kiut.core import kiut_settings as kset

CANCELLED = 'Conversion cancelled -- nothing written.'


class InputError(Exception):
    """The files given can't be converted as given (the command line: exit
    2); its text says why. details: lines for the log only, after it (the
    window's message block shows the text alone; stage 10g)."""

    def __init__(self, text: str, details: list | None = None) -> None:
        super().__init__(text)
        self.details = list(details or [])


# ── asking ───────────────────────────────────────────────────────────────────

def ask(question: str, default: bool, quiet: bool = False) -> bool:
    """A yes/no question on its own line, [Y/n] or [y/N] after it (the
    default: an empty answer), the answer after '> '. Read from a terminal or
    a pipe; with nothing to read (input closed, e.g. a scheduled run) the
    default is taken, and said. With -q nothing is asked: the default."""
    hint = '[Y/n]' if default else '[y/N]'
    said = 'yes' if default else 'no'
    if quiet:
        print(f'{question} {hint} -- not asked (-q): {said} (the default)')
        return default
    print(f'{question} {hint}')
    sys.stdout.write('> ')
    sys.stdout.flush()
    line = sys.stdin.readline() if sys.stdin else ''
    if not line:
        print(f'-- no answer to read: {said} (the default)')
        return default
    answer = line.strip().lower()
    if not sys.stdin.isatty():
        print(answer)                       # a piped answer, shown as if typed
    return default if not answer else answer in ('y', 'yes')


def _given(question: str, answer: bool, why: str) -> bool:
    """A question --yes answers: shown with its answer."""
    print(f'{question} -- {"yes" if answer else "no"} ({why})')
    return answer


class _Asking:
    """What both converters ask the same way."""

    def __init__(self, yes: bool, quiet: bool) -> None:
        self.yes, self.quiet = yes, quiet

    def _ask(self, question: str, default: bool, go_on: bool = True) -> bool:
        """A question; --yes answers it go_on (True: "go on")."""
        if self.yes:
            return _given(question, go_on, '--yes')
        return ask(question, default, self.quiet)

    def redo(self, text: str, default: bool) -> bool:
        for line in text.splitlines():
            print(f'WARNING: {line}' if line else '')
        if self.yes:
            return _given('Redo from the original files?', default, 'the default, --yes')
        return ask('Redo from the original files?', default, self.quiet)

    def replace(self, existing) -> bool:
        print(f'WARNING: {len(existing)} KiCad file(s) already exist in the output folder and will be '
              f'replaced; changes made to them in KiCad are lost:')
        for p in existing:
            print(f'  {p}')
        return self._ask('Replace them?', False)


def _board_classes():
    from kiut.board import kiub

    class BoardQuestions(_Asking, kiub.BoardQuestions):
        """KIUB's questions on KIUT's command line."""

        def __init__(self, yes: bool, quiet: bool, library_only: str, together: bool = False) -> None:
            super().__init__(yes, quiet)
            self.library_only_setting = library_only
            self.together = together            # converted with its schematic, in one run

        def library_only(self) -> bool:
            if self.library_only_setting == 'no':
                return False
            print(f'NOTE: {kiub.UNSURE_QUESTION}')
            return self._ask('Convert it as a library only (no design board)?', False, go_on=False)

        def legacy_v3(self, text: str) -> bool:
            print(f'WARNING: {text}')
            return self._ask('Convert it as a design of its own anyway?', False)

        def incomplete_technology(self, lines) -> bool:
            for line in lines:
                print(f'WARNING: incomplete technology table -- {line}; the missing codes count '
                      f'as undefined')
            return self._ask('Continue with the conversion anyway?', False)

        def non_digit_refdes(self, refs, advice: str, has_schematic: bool) -> bool:
            if self.together or not has_schematic:
                # With its schematic in the run, the schematic's check decided (fix_designators);
                # without a schematic, KiCad's board editor takes them as they are.
                why = ("the schematic's designator check decided" if self.together
                       else "no schematic belongs to this board; KiCad's board editor accepts them")
                print(f'NOTE: designators that do not end in a digit, kept as they are ({why}): '
                      + ', '.join(refs))
                return True
            print('WARNING: the following component reference designators do not end in a digit:')
            for rd in refs:
                print(f'  {rd}')
            print(advice)
            return self._ask('Continue with this file anyway (not recommended)?', False)

    return BoardQuestions


def _schematic_classes():
    from kiut.schematic import kiuc

    class SchematicQuestions(_Asking, kiuc.SchematicQuestions):
        """KIUC's questions on KIUT's command line: missing sheets and the
        designator fix answered by the settings, merged records by the rule
        (all resolved when none is ambiguous)."""

        def __init__(self, yes: bool, quiet: bool, chosen: kset.Chosen, fix_only: bool = False) -> None:
            super().__init__(yes, quiet)
            self.chosen = chosen
            self.fix_only = fix_only

        def missing_sheets(self, missing, utsch: bool) -> bool:
            for w in missing:
                print(f'WARNING: {w}')
            if self.chosen.get('schematic', 'allow_missing_sheets'):
                print(f'NOTE: converted without the missing sub-sheet(s) ({setting_on("allow_missing_sheets")}).')
                return True
            if not utsch:
                print('WARNING: a known cause is a double-dot or double-extension file name left by a '
                      'Ulticap V5.72 re-save (e.g. "SHEET2..SCH" or "SHEET2.SCH.SCH" instead of '
                      '"SHEET2.SCH").')
            print(f'WARNING: {turn_on("allow_missing_sheets")} to convert without the missing sub-sheet(s).')
            print(CANCELLED)
            return False

        def record_integrity(self, issues_by_file: dict):
            total = sum(len(v) for v in issues_by_file.values())
            ambiguous = [(f, iss) for f, issues in issues_by_file.items()
                         for iss in issues if not iss.is_unambiguous]
            if ambiguous:
                for f, iss in ambiguous:
                    cands = ', '.join(f'{a!r}|{b!r}' for a, b in iss.candidates)
                    print(f'WARNING: {f.name}:{iss.line_no}  {iss.symbol_name!r} ({iss.section})  '
                          f'ambiguous merged value {iss.merged_token!r} (candidates: {cands})')
                print(f'WARNING: {len(ambiguous)} of {total} record-integrity issue(s) cannot be '
                      f'resolved without a choice: only the window can convert this design.')
                print(CANCELLED)
                return None
            resolutions = []
            for f, issues in issues_by_file.items():
                for iss in issues:
                    resolutions.append((iss, iss.candidates[0]))
                    print(f'Record integrity: {f.name}:{iss.line_no}  {iss.symbol_name!r} '
                          f'({iss.section})  {iss.merged_token!r} -> '
                          f'{iss.candidates[0][0]!r} | {iss.candidates[0][1]!r}')
            return resolutions

        def fix_refdes(self, offending, utsch: bool) -> bool:
            return self.fix_only or bool(self.chosen.get('schematic', 'fix_designators'))

    class SchematicJob(kiuc.SchematicJob):
        """KIUC's job on KIUT's command line: the writer's tuning from the
        settings."""
        UNCHANGED_HINT = 'Set fix_designators = yes in the settings to convert {what} instead.'
        FIX_WHY = '(fix_designators = yes)'           # both in the window's words when it converts

        def __init__(self, args, questions, tuning: dict, fix_only: bool = False, board: bool = False,
                     **files) -> None:
            super().__init__(args, questions, **files)
            self._tuning = tuning
            self.fix_only = fix_only
            on = turn_on('fix_designators')
            self.UNCHANGED_HINT = on[0].upper() + on[1:] + ' to convert {what} instead.'
            self.FIX_WHY = f'({setting_on("fix_designators")})'
            if fix_only:
                self.FIX_WHY = '(--fix-only)' if not WINDOW_WORDS else '(renaming without converting)'
            self.DDF_NOTE = ('The board is converted from it next.' if board and not fix_only else
                             'Convert the board from it: the schematic with its board ticked (or the DDF '
                             'alone).' if WINDOW_WORDS else
                             'Convert the board from it: kiut.py with the schematic and its DDF '
                             '(or the DDF alone).')

        def tuning(self) -> dict:
            return dict(self._tuning)

        def steps(self) -> None:
            if not self.fix_only:
                return super().steps()
            # The designator fix only (Refdes Reannotate): the design's checks and its working set,
            # then the fix into <name>_corrected; nothing converted.
            self.check_files()
            utsch = self.utsch_file is not None
            if not utsch:
                self.discover_sheets()
            self.use_corrected()
            if utsch:
                self.parse_utsch()
                fixed = self.check_refdes_utsch()
            else:
                self.parse()
                fixed = self.check_refdes()
            if not fixed and not self._found:
                self.say('Designators: every one ends in a digit -- nothing to fix, nothing written.')

        def _refdes(self, offending, utsch):
            self._found = True
            return super()._refdes(offending, utsch)

        _found = False

    return SchematicQuestions, SchematicJob


# ── the command line ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='kiut.py',
        description='Convert Ulticap schematics and libraries and Ultiboard boards and libraries to '
                    'KiCad 9. Every conversion setting comes from a settings file: see '
                    'docs/SETTINGS.md.',
        epilog='Inputs, one kind per run: .SCH (the main sheet), .BLK or one .UTSCH; .UTLIB or DOS '
               '.LIB symbol libraries; one .DDF; an Ultiboard .LIB or .L55 with its .TEC; or a '
               'schematic and its board together (X.SCH X.DDF). The output goes next to the '
               'originals: <name>_KiCad, or <name>_lib_KiCad for a library.')
    ap.add_argument('inputs', nargs='*', metavar='file', help='the file(s) to convert')
    ap.add_argument('--settings', choices=kset.SOURCES, default=None,
                    help="the settings to use: the design's own <name>_kiut.ini, kiut.ini (the last "
                         "used) or the built-in defaults. Default: the design's, else kiut.ini, "
                         "else the built-in defaults.")
    ap.add_argument('--yes', action='store_true',
                    help='answer every question "go on" (the corrected-copies question: its default)')
    ap.add_argument('-q', '--quiet', action='store_true',
                    help='show nothing and ask nothing (each question: its default); the report goes '
                         'to <output folder>/<name>.log')
    ap.add_argument('--fix-only', action='store_true',
                    help='the designator fix only: the corrected copies written into <name>_corrected '
                         '(for a schematic, or a schematic and its board), nothing converted')
    return ap


class Inputs:
    """The command line's files, by kind: kind is 'schematic', 'utsch',
    'libraries' (Ulticap), 'board', 'board_library' (Ultiboard, with tec:
    its .TEC or None) or 'design' (a schematic, files, and its board, ddf;
    sch_kind: 'schematic' or 'utsch')."""

    def __init__(self, kind: str, files: list, tec: Path | None = None, ddf: Path | None = None,
                 sch_kind: str = '', tec_split: list | None = None) -> None:
        self.kind, self.files, self.tec, self.ddf, self.sch_kind = kind, files, tec, ddf, sch_kind
        self.tec_split = tec_split          # an Ultiboard library's multi-file technology, this run


# The KIUT window's words for the switches it shows (stage 10d, the author): while it converts, the
# report names a switch as the window shows it; kiut.py names the settings file's key.
WINDOW_WORDS: dict | None = None


def setting_on(key: str) -> str:
    """'fix_designators = yes', or in the window "'Rename designators ...' on"."""
    return f"'{WINDOW_WORDS[key]}' on" if WINDOW_WORDS and key in WINDOW_WORDS else f'{key} = yes'


def turn_on(key: str) -> str:
    """'set fix_designators = yes in the settings', or in the window "tick '...'"."""
    return (f"tick '{WINDOW_WORDS[key]}'" if WINDOW_WORDS and key in WINDOW_WORDS
            else f'set {key} = yes in the settings')


ONE_DDF = 'One .DDF per run: each board is a design of its own, with its own KiCad project.'


def selection_messages(inputs: 'Inputs', library_only: str = 'ask') -> list:
    """What to say about inputs that are accepted, before converting them:
    [(level, text)], level 'warn' or 'note'. The KIUT window shows them in its
    message block, where Convert then answers a warning; kiut.py asks
    several_libraries and library_ddfs itself. library_only: the setting
    (a library DDF that may be a design is asked about only with ask)."""
    out = []
    if inputs.kind == 'board_library' and len(ultiboard_libraries(inputs)) > 1:
        out.append(('warn', several_libraries_text(inputs)))
    if inputs.kind == 'board_library' and library_ddfs(inputs):
        from kiut.board import kiub
        unsure = [f for f in library_ddfs(inputs) if ddf_kind(f) == kiub.DDF_UNSURE]
        if unsure and library_only == 'ask':
            out.append(('warn', library_ddfs_text(unsure)))
        out.append(('note', f'{len(library_ddfs(inputs))} library DDF'
                            f'{"s" if len(library_ddfs(inputs)) != 1 else ""}: each converted with the '
                            f'technology it holds'
                            + (' (the .TEC is for the Ultiboard libraries only)' if inputs.tec else '')
                            + ', into its own <name>.pretty.'))
    if inputs.kind == 'board':
        from kiut.board import kiub
        q = kiub.legacy_v3_question(str(inputs.files[0]))
        if q:
            out.append(('warn', q))
    if inputs.kind == 'libraries' and library_output(inputs) is None:
        from kiut.core.corrected import locate
        homes = {locate(f).home.resolve() for f in inputs.files}
        if len(homes) > 1:
            out.append(('note', f'The libraries come from {len(homes)} folders: the output goes next to the '
                                f'first, {locate(inputs.files[0]).home}.'))
    return out


def library_ddfs_text(unsure: list) -> str:
    """The warning for library DDFs that may be designs, converted as libraries."""
    n = len(unsure)
    names = ', '.join(f.name for f in unsure[:3]) + (', ...' if n > 3 else '')
    if n == 1:
        return (f'{names} places components but holds no nets, copper or vias: a shape library, as '
                f'Ultiboard\'s library DDFs place their shapes -- or a design not yet routed. Converted with '
                f'the others, as a library only (no design board).')
    return (f'{n} DDFs ({names}) place components but hold no nets, copper or vias: shape libraries, as '
            f'Ultiboard\'s library DDFs place their shapes -- or designs not yet routed. Converted with the '
            f'others, as libraries only (no design board).')


def several_libraries_text(inputs: 'Inputs') -> str:
    """The warning for several Ultiboard libraries converted with one technology."""
    tec = (f' ({inputs.tec.name})' if inputs.tec is not None else
           ' (a multi-file set)' if inputs.tec_split else '')
    return (f'{len(ultiboard_libraries(inputs))} Ultiboard libraries with one technology{tec}: only right when every library '
            f'was made with it -- the same pad and drill codes, as Ultiboard\'s manual advises for codes in '
            f'use. A library made with another technology converts with wrong pads. Each library becomes its '
            f'own <name>.pretty, with its overview board in <name>_lib_KiCad.')


def classify(names: list) -> Inputs:
    """The inputs' kind; a wrong combination or a missing file: InputError
    (on the command line, exit 2)."""
    from kiut.board.kiub_library import library_kind
    by_kind: dict = {}
    order: list = []
    for name in names:
        p = Path(name)
        ext = p.suffix.upper()
        if not ext and not p.exists():
            for e in ('.DDF', '.ddf'):
                if p.with_name(p.name + e).exists():
                    p, ext = p.with_name(p.name + e), '.DDF'
                    break
        if not p.exists():
            raise InputError(f'File not found: {name}')
        if ext in ('.SCH', '.BLK'):
            kind = 'schematic'
        elif ext == '.UTSCH':
            kind = 'utsch'
        elif ext == '.UTLIB':
            kind = 'libraries'
        elif ext == '.DDF':
            kind = 'board'
        elif ext == '.TEC':
            kind = 'tec'
        elif ext in ('.LIB', '.L55'):
            lk = library_kind(p)
            if lk == 'ultiboard':
                kind = 'board_library'
            elif lk == 'ulticap' or ext == '.LIB':
                kind = 'libraries'
            else:
                raise InputError(f'{name}: not an Ultiboard library')
        else:
            raise InputError(f'{name}: not a file KIUT converts (.SCH, .BLK, .UTSCH, .UTLIB, .LIB, .DDF, '
                     f'.L55, with a .TEC)')
        by_kind.setdefault(kind, []).append(p)
        order.append(p)
    tec = by_kind.pop('tec', [])
    kinds = sorted(by_kind)
    if len(kinds) == 2 and 'board' in kinds and set(kinds) & {'schematic', 'utsch'} and not tec:
        return _design(by_kind)
    if set(kinds) <= {'board', 'board_library'} and (len(by_kind.get('board', [])) > 1 or len(kinds) == 2):
        return _footprint_libraries([p for p in order if p not in tec], by_kind, tec)
    if len(kinds) > 1:
        raise InputError('One kind of input per run -- a design (a schematic, with its board), Ulticap '
                 'libraries, a board, or Ultiboard libraries: each becomes a different kind of KiCad '
                 'output (given: ' + ', '.join(kinds) + ').')
    if not kinds:
        raise InputError('A .TEC is the technology of an Ultiboard library (.LIB, .L55): give the library '
                         'with it.')
    kind = kinds[0]
    files = by_kind[kind]
    if tec and kind != 'board_library':
        raise InputError('A .TEC is the technology of an Ultiboard library (.LIB, .L55) only: a .DDF holds '
                         'its own, a schematic or an Ulticap library has none.')
    if len(tec) > 1:
        raise InputError(ONE_TEC)
    if kind in ('utsch', 'board') and len(files) > 1:
        raise InputError({'utsch': 'One .UTSCH per run: it holds its whole hierarchy.',
                          'board': ONE_DDF}[kind])
    if kind in ('libraries', 'board_library'):
        _one_of_each_name(files, kind)
    return Inputs(kind, files, tec[0] if tec else None)


ONE_TEC = ("One .TEC per run: it is the technology of every library given (a technology split over several "
           "files: tec_split in the settings, or the window's Multi-file set).")


def _one_of_each_name(files: list, kind: str) -> None:
    """Two libraries of one name: InputError (they would write the same files)."""
    seen: dict = {}
    for f in files:
        other = seen.setdefault(f.stem.lower(), f)
        if other is not f:
            what = (f'they would write the same {f.stem}.kicad_sym' if kind == 'libraries'
                    else f'they would share one output folder, {f.stem}_lib_KiCad')
            raise InputError(f'Two libraries named {f.stem!r} ({other} and {f}): {what}. Convert them '
                             f'in separate runs.')


_kinds_seen: dict = {}


def ddf_kind(path) -> str:
    """kiub.input_kind of a DDF (library, unsure, design), cached by file
    and time."""
    from kiut.board import kiub
    p = Path(path)
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        return kiub.DDF_DESIGN
    if key not in _kinds_seen:
        _kinds_seen[key] = kiub.input_kind(str(p))
    return _kinds_seen[key]


def is_ddf(path) -> bool:
    return Path(path).suffix.upper() == '.DDF'


def ultiboard_libraries(inputs: 'Inputs') -> list:
    """The .LIB/.L55 Ultiboard libraries of a board_library run (the ones a
    .TEC is for)."""
    return [f for f in inputs.files if not is_ddf(f)]


def library_ddfs(inputs: 'Inputs') -> list:
    """The library DDFs of a board_library run (each holds its own technology)."""
    return [f for f in inputs.files if is_ddf(f)]


def _footprint_libraries(files: list, by_kind: dict, tec: list) -> 'Inputs':
    """Several DDFs, or DDFs with Ultiboard libraries (KIUT stage 10f): one
    run of footprint libraries when every DDF is a library -- shapes only, or
    components without nets, copper or vias (asked once for all of them); a
    design among them refused, named. The .TEC (one) is for the .LIB/.L55
    only: each DDF holds its own technology."""
    from kiut.board import kiub
    ddfs = by_kind.get('board', [])
    designs = [f for f in ddfs if ddf_kind(f) == kiub.DDF_DESIGN]
    if designs:
        # no names in the text (the message block shows it; the author, 2026-11-04): which ones, in the log
        if len(designs) == len(ddfs):
            what = 'These DDFs are designs' if len(ddfs) > 1 else 'This DDF is a design'
        else:
            what = (f'{len(designs)} of these {len(ddfs)} DDFs are designs' if len(designs) > 1
                    else f'1 of these {len(ddfs)} DDFs is a design')
        names = sorted({f.name for f in designs}, key=str.lower)
        hold = 'they hold' if len(designs) > 1 else 'it holds'
        raise InputError(f'{what} ({hold} nets, copper or vias): a design is converted on its own, one per '
                         f'run, with its own KiCad project. Library DDFs (shapes only, or components without '
                         f'nets) can be converted together, also with Ultiboard libraries.',
                         [f'Design{"s" if len(names) > 1 else ""}: ' + ', '.join(names)])
    if tec and not by_kind.get('board_library'):
        raise InputError('A .TEC is the technology of an Ultiboard library (.LIB, .L55) only: a .DDF holds '
                         'its own.')
    if len(tec) > 1:
        raise InputError(ONE_TEC)
    _one_of_each_name(files, 'board_library')
    return Inputs('board_library', files, tec[0] if tec else None)


def _design(by_kind: dict) -> Inputs:
    """A schematic and its board: one design -- the DDF named as the
    schematic (the main sheet, or the .UTSCH) and in its folder, so its
    corrected copy is the one the schematic's designator fix writes."""
    from kiut.core.corrected import locate
    sch_kind = 'schematic' if 'schematic' in by_kind else 'utsch'
    files, ddfs = by_kind[sch_kind], by_kind['board']
    if len(ddfs) > 1:
        raise InputError(ONE_DDF)
    if sch_kind == 'utsch' and len(files) > 1:
        raise InputError('One .UTSCH per run: it holds its whole hierarchy.')
    if any(f.suffix.upper() == '.BLK' for f in files):
        raise InputError('A .BLK converts to a block library, which has no board: convert the .DDF on its own.')
    s, b = locate(files[0]), locate(ddfs[0])
    if s.name.lower() != b.name.lower() or s.home.resolve() != b.home.resolve():
        raise InputError(f'{ddfs[0].name} is not the board of {files[0].name}: a schematic and its board are '
                 f'converted together when they are one design -- the same name, in the same folder.')
    return Inputs('design', files, ddf=ddfs[0], sch_kind=sch_kind)


def _say_settings(chosen: kset.Chosen) -> None:
    prefix = {'warn': 'WARNING: ', 'note': 'NOTE: ', 'info': ''}
    for level, text in chosen.messages:
        print(prefix[level] + text)


def _schematic(inputs: Inputs, chosen: kset.Chosen, args, fix_only: bool = False,
               board: bool = False, asking=None) -> tuple:
    """The schematic's job and its output folder and name."""
    from kiut.schematic import kiuc
    from kiut.schematic.kiuc_writer import default_output_dir, design_name, TUNING_SPEC
    _, Job = _schematic_classes()
    first = inputs.files[0]
    block = inputs.kind == 'schematic' and (any(f.suffix.upper() == '.BLK' for f in inputs.files)
                                            or bool(chosen.get('schematic', 'block_library')))
    out = library_output(inputs, block) or default_output_dir(first, block=block)
    s = chosen.values['schematic']
    jargs = kiuc.job_args(
        inputs.files, out, yes=args.yes, quiet=args.quiet, block=block,
        v5=bool(s['v5_header']) and inputs.kind == 'schematic', fix_refdes=bool(s['fix_designators']),
        allow_missing_sheets=bool(s['allow_missing_sheets']),
        kicad_colors=bool(s['kicad_colors']), label_keep_alignment=bool(s['label_keep_alignment']),
        font=chosen.get('schematic', 'font'))
    tuning = {name: s[name.lower()] for name, *_ in TUNING_SPEC}
    q = (asking or Console(args.yes, args.quiet)).schematic(chosen, fix_only)
    files = {'libraries': dict(lib_files=inputs.files), 'utsch': dict(utsch_file=first)}.get(
        inputs.kind, dict(sch_files=list(inputs.files)))
    job = Job(jargs, q, tuning, fix_only, board, **files)
    job.compare_board = not board           # the board follows in this run: nothing to compare yet
    return job, out, design_name(first)


def _board(inputs: Inputs, chosen: kset.Chosen, args, together: bool = False, asking=None) -> tuple:
    """The board's job and its output folder and name; together: converted
    with its schematic, in one run."""
    from kiut.board import kiub
    infile = inputs.files[0]
    b = chosen.values['board']
    # The board's details (layers, shapes, ...) always shown, as KIUB's window shows them.
    jargs = kiub.job_args(str(infile), font=chosen.get('board', 'font'), yes=args.yes, verbose=True)
    for name, *_ in kiub.BOARD_DEFAULTS_SPEC + kiub.FINE_TUNING_SPEC:
        setattr(jargs, name, b[name])
    jargs.v2v3_staircase_limit_mil = 0 if not b['v2v3_staircase_merge'] else b['v2v3_staircase_limit_mil']
    jargs.v2v3_corner_slant_limit_mil = 0 if not b['v2v3_chamfer'] else b['v2v3_corner_slant_limit_mil']
    library = inputs.kind == 'board_library'
    if library and not is_ddf(infile):
        if inputs.tec is not None:
            jargs.tec = str(inputs.tec)
        elif inputs.tec_split:
            jargs.tec_split = [str(f) for f in inputs.tec_split]
        elif b['tec_split']:
            jargs.tec_split = [str(infile.parent / f) for f in b['tec_split']]
    jargs.library_only = b['library_only'] == 'yes' or (library and is_ddf(infile))  # asked once for all
    q = (asking or Console(args.yes, args.quiet)).board(b['library_only'], together)
    home = library_output(inputs)
    if home is not None:                    # KIUT's Footprints folder: <name>_lib_KiCad below it
        jargs.library_folder = str(home)
        out = home / (kiub.design_name(str(infile)) + kiub.LIBRARY_FOLDER_SUFFIX)
    else:
        out = Path(kiub.default_output_dir(str(infile), library or jargs.library_only))
        if inputs.kind == 'board' and not together:
            # a library DDF (shapes only, or answered a library) goes to Footprints too: kiub decides,
            # after its question (stage 10f); out is the design's folder until then
            from kiut.core import folders
            fp = folders.library_folder('footprints')
            jargs.library_folder = str(fp) if fp is not None else None
    return kiub.BoardJob(jargs, q), out, kiub.design_name(str(infile))


def library_output(inputs: Inputs, block: bool = False) -> Optional[Path]:
    """Where a library-only run's output goes: KIUT's library folder's
    Symbols (Ulticap libraries), Blocks (a block library: block) or
    Footprints (Ultiboard libraries) -- kiut.core.folders; None for a
    design, which stays next to its files (and so does what a design makes,
    a DDF converted as a library only included)."""
    from kiut.core import folders
    if inputs.kind == 'board_library':
        return folders.library_folder('footprints')
    if inputs.kind == 'libraries':
        return folders.library_folder('symbols')
    if block and inputs.kind == 'schematic':
        return folders.library_folder('blocks')
    return None


def _design_file(inputs: Inputs) -> tuple:
    """The design's settings file, and every folder it may be in (a DDF's
    output folder depends on its answer to "design or library?")."""
    if inputs.kind in ('board', 'board_library'):
        from kiut.board import kiub
        f = str(inputs.files[0])
        name = kiub.design_name(f)
        home = library_output(inputs)
        folders = [home / (name + kiub.LIBRARY_FOLDER_SUFFIX) if home is not None else
                   Path(kiub.default_output_dir(f, inputs.kind == 'board_library'))]
        if inputs.kind == 'board':
            from kiut.core import folders as kfolders
            folders.append(Path(kiub.default_output_dir(f, True)))
            fp = kfolders.library_folder('footprints')
            if fp is not None and ddf_kind(f) != kiub.DDF_DESIGN:   # a library DDF (stage 10f)
                folders.insert(0 if ddf_kind(f) == kiub.DDF_LIBRARY else 1, fp / (name + kiub.LIBRARY_FOLDER_SUFFIX))
    else:                               # a schematic (with its board), or libraries
        from kiut.schematic.kiuc_writer import default_output_dir, design_name
        first = inputs.files[0]
        block = any(f.suffix.upper() == '.BLK' for f in inputs.files)
        name = design_name(first)
        folders = [library_output(inputs, block) or default_output_dir(first, block=block)]
        if inputs.kind in ('schematic', 'design') and not block:  # block_library = yes: the blocks' folder
            blocks = library_output(Inputs('schematic', inputs.files), True)
            if blocks is None:
                folders.append(default_output_dir(first, block=True))
            elif _blocks_from(blocks, name, first):
                folders.append(blocks)
    for folder in folders:
        if kset.design_copy(folder, name).is_file():
            return kset.design_copy(folder, name), name
    return kset.design_copy(folders[0], name), name


def _blocks_from(folder: Path, name: str, source: Path) -> bool:
    """Whether KIUT's Blocks folder holds <name>.kicad_blocks converted from
    source (its design-block-lib-table says so): only then is the
    <name>_kiut.ini there this schematic's own -- the Blocks folder is
    shared by every block library, another design may have its name."""
    from kiut.core import kicad_project
    was = kicad_project.lib_source(folder, 'dbl', kicad_project.kicad_name(name))
    try:
        return was is not None and Path(was) == Path(str(Path(source).resolve()).replace(chr(92), '/'))
    except OSError:
        return False


class Console:
    """The questions as the command line asks them (the `asking` object
    convert takes; the KIUT window gives its own)."""

    def __init__(self, yes: bool = False, quiet: bool = False) -> None:
        self.yes, self.quiet = yes, quiet

    def schematic(self, chosen: kset.Chosen, fix_only: bool):
        Questions, _ = _schematic_classes()
        return Questions(self.yes, self.quiet, chosen, fix_only)

    def board(self, library_only: str, together: bool):
        return _board_classes()(self.yes, self.quiet, library_only, together)

    def several_libraries(self, text: str) -> bool:
        print(f'WARNING: {text}')
        return _Asking(self.yes, self.quiet)._ask('Convert them all with this technology?', False)

    def library_ddfs(self, text: str) -> bool:
        print(f'WARNING: {text}')
        return _Asking(self.yes, self.quiet)._ask('Convert them all as libraries?', False)


def check_fix_only(inputs: Inputs) -> None:
    """--fix-only (the window's Fix designators only) on these inputs:
    InputError when it doesn't apply."""
    if inputs.kind not in ('schematic', 'utsch', 'design'):
        raise InputError('--fix-only renames designators in a schematic (and its board, given with it): '
                         'a board, or a library, is never renamed on its own.')
    if any(f.suffix.upper() == '.BLK' for f in inputs.files):
        raise InputError('--fix-only: a .BLK is a block library, not a design.')


def check(inputs: Inputs, chosen: kset.Chosen) -> None:
    """The inputs with these settings: InputError when they don't go
    together."""
    if inputs.kind == 'design' and chosen.get('schematic', 'block_library'):
        raise InputError('block_library = yes converts the schematic to a block library, which has no '
                         'board: convert the .DDF on its own.')
    if inputs.kind == 'board_library' and chosen.get('board', 'library_only') == 'no':
        from kiut.board import kiub
        unsure = [f for f in library_ddfs(inputs) if ddf_kind(f) == kiub.DDF_UNSURE]
        if unsure:
            raise InputError(f'{unsure[0].name} places components but holds no nets, copper or vias, and '
                             f'library_only = no converts it as a design -- one per run. Set library_only to '
                             f'ask or yes to convert it as a library with the others.')
    if inputs.kind == 'board_library' and ultiboard_libraries(inputs) and inputs.tec is None \
            and not inputs.tec_split:
        split = chosen.get('board', 'tec_split')
        if not split:
            raise InputError(f'An Ultiboard library holds pad codes only: give its technology with it '
                             f'(kiut.py {" ".join(f.name for f in ultiboard_libraries(inputs))} <technology>.TEC), or a '
                             f'split technology as '
                             f'tec_split in the settings (docs/SETTINGS.md).')
        if len(split) not in (4, 5):
            raise InputError(f'tec_split in the settings takes four or five files, TOP; BOTTOM; INNER; '
                             f'DRILLS[; TRACES] ({len(split)} given).')


class _Together:
    """The asking for a schematic and its board in one run: one question
    for the files both would replace -- the board's existing files listed
    with the schematic's, the board's own question then answered by it."""

    def __init__(self, asking, board_files: list) -> None:
        self.asking = asking
        self.board_files = [Path(p) for p in board_files]
        self.answer = None                  # the schematic's question's, once asked

    def schematic(self, chosen: kset.Chosen, fix_only: bool):
        q = self.asking.schematic(chosen, fix_only)
        ask = q.replace

        def replace(existing):
            self.answer = ask(list(existing) + self.board_files)
            return self.answer
        q.replace = replace
        return q

    def board(self, library_only: str, together: bool):
        q = self.asking.board(library_only, together)
        ask = q.replace

        def replace(existing):
            listed = {p.resolve() for p in self.board_files}
            if self.answer is not None and all(Path(p).resolve() in listed for p in existing):
                print(f"The board's {len(existing)} existing file(s): replaced, as answered with the "
                      f"schematic's.")
                return self.answer
            return ask(existing)
        q.replace = replace
        return q


def _board_files(ddf: Path, chosen: kset.Chosen) -> list:
    """The files a design's board conversion would replace (kiub.OutputPlan),
    as they are now."""
    from kiut.board import kiub
    library_only = chosen.get('board', 'library_only') == 'yes'
    return [Path(p) for p in kiub.existing_outputs(
        kiub.OutputPlan(kiub.default_output_path(str(ddf), library_only), library_only))]


def convert(inputs: Inputs, chosen: kset.Chosen, yes: bool = False, quiet: bool = False,
            fix_only: bool = False, asking=None, where: dict | None = None) -> int:
    """Convert inputs with chosen's settings, asking through asking (None:
    the console's): the settings named, the schematic's job (and its board's,
    a design), the design's settings copy written by its first conversion.
    The exit code. where ({'out_dir', 'name'}) follows the output folder and
    name as they become known."""
    args = argparse.Namespace(yes=yes, quiet=quiet)
    where = where if where is not None else {}
    _say_settings(chosen)
    if inputs.kind == 'design' and not fix_only:
        asking = _Together(asking or Console(yes, quiet), _board_files(inputs.ddf, chosen))
    if inputs.kind == 'board_library' and len(inputs.files) > 1:
        return _board_libraries(inputs, chosen, args, asking or Console(yes, quiet), where)
    if inputs.kind in ('board', 'board_library'):
        job, where['out_dir'], where['name'] = _board(inputs, chosen, args, asking=asking)
    elif inputs.kind == 'design':
        sch = Inputs(inputs.sch_kind, inputs.files)
        job, where['out_dir'], where['name'] = _schematic(sch, chosen, args, fix_only, board=True,
                                                          asking=asking)
    else:
        job, where['out_dir'], where['name'] = _schematic(inputs, chosen, args, fix_only, asking=asking)
    code = job.run()
    if code == 0 and inputs.kind == 'design' and not fix_only:
        # The board, from the design's working DDF: its corrected copy when the schematic's
        # designator fix wrote one.
        print(f'\nBoard: {inputs.ddf}')
        job, where['out_dir'], _ = _board(Inputs('board', [inputs.ddf]), chosen, args, together=True,
                                          asking=asking)
        code = job.run()
    plan = getattr(job, 'plan', None)
    if plan is not None:
        where['out_dir'] = Path(plan.folder)
    out_dir, name = where['out_dir'], where['name']
    copy = kset.design_copy(out_dir, name)
    if code == 0 and not fix_only and out_dir.is_dir() and not copy.exists():
        kset.write(copy, chosen.values)
        print(f'Settings saved for the design: {copy}')
    return code


def _board_libraries(inputs: Inputs, chosen: kset.Chosen, args, asking, where: dict) -> int:
    """Several Ultiboard libraries with one technology: asked first (the
    window's message block has said it: Convert answers it), then each
    converted on its own, into its own <name>_lib_KiCad with its own settings
    copy; one that fails or is cancelled doesn't stop the next. The exit
    code: 0 when every library was converted."""
    if len(ultiboard_libraries(inputs)) > 1 and not asking.several_libraries(several_libraries_text(inputs)):
        print(CANCELLED)
        return 1
    from kiut.board import kiub
    unsure = [f for f in library_ddfs(inputs) if ddf_kind(f) == kiub.DDF_UNSURE]
    if unsure and chosen.get('board', 'library_only') == 'ask' \
            and not asking.library_ddfs(library_ddfs_text(unsure)):
        print(CANCELLED)
        return 1
    codes, where['folders'] = [], []
    for i, lib in enumerate(inputs.files, 1):
        print(f'{"" if i == 1 else chr(10)}Library {i} of {len(inputs.files)}: {lib}')
        one = Inputs('board_library', [lib], None if is_ddf(lib) else inputs.tec,
                     tec_split=None if is_ddf(lib) else inputs.tec_split)
        job, out_dir, name = _board(one, chosen, args, asking=asking)
        code = job.run()
        plan = getattr(job, 'plan', None)
        out_dir = Path(plan.folder) if plan is not None else out_dir
        where['folders'].append((out_dir, name))
        copy = kset.design_copy(out_dir, name)
        if code == 0 and out_dir.is_dir() and not copy.exists():
            kset.write(copy, chosen.values)
            print(f'Settings saved for the library: {copy}')
        codes.append(code)
    where['out_dir'], where['name'] = where['folders'][0]
    done = sum(c == 0 for c in codes)
    if done < len(codes):
        print(f'WARNING: {done} of {len(codes)} libraries converted.')
    return 0 if done == len(codes) else 1


def expand_wildcards(names: list) -> list:
    """Each name with * or ? that isn't a file itself: the files it matches,
    sorted (as a Linux shell does; Windows' command line leaves that to the
    program -- stage 10f). One that matches nothing stays as it is (File not
    found)."""
    import glob
    out = []
    for name in names:
        if ('*' in name or '?' in name) and not Path(name).exists():
            hits = sorted(h for h in glob.glob(name) if Path(h).is_file())
            out += hits or [name]
        else:
            out.append(name)
    return out


def run(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.inputs:
        print('kiut.py: no input file given.', file=sys.stderr)
        ap.print_usage(sys.stderr)
        print('Every conversion setting is in a settings file: see docs/SETTINGS.md.', file=sys.stderr)
        return 2
    args.inputs = expand_wildcards(args.inputs)
    try:
        inputs = classify(args.inputs)
        if args.fix_only:
            check_fix_only(inputs)
        design_file, name = _design_file(inputs)
        chosen = kset.choose(args.settings, design_file)
        check(inputs, chosen)
    except InputError as e:
        ap.error('\n'.join([str(e)] + e.details))
    buffer = io.StringIO()
    where = {'out_dir': design_file.parent, 'name': name}
    code = 1
    with contextlib.ExitStack() as stack:
        if args.quiet:
            stack.enter_context(contextlib.redirect_stdout(buffer))
            stack.enter_context(contextlib.redirect_stderr(buffer))
        try:
            code = convert(inputs, chosen, args.yes, args.quiet, args.fix_only, where=where)
        finally:
            if args.quiet and inputs.kind != 'libraries':
                # several Ultiboard libraries: the run's log in each one's folder
                for out_dir, name in where.get('folders') or [(where['out_dir'], where['name'])]:
                    try:
                        out_dir.mkdir(parents=True, exist_ok=True)
                        (out_dir / f'{name}.log').write_text(buffer.getvalue(), encoding='utf-8')
                    except OSError as exc:
                        print(f'WARNING: could not write the log {out_dir / (name + ".log")}: {exc}',
                              file=sys.__stderr__)
    return code


def main() -> None:
    sys.exit(run())


if __name__ == '__main__':
    main()
