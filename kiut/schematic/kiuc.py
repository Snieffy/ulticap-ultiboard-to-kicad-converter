#!/usr/bin/env python3
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
kiut/schematic/kiuc.py -- KIUC's conversion as a job (kiut.core.job): Ulticap .SCH/.BLK (ASCII)
or .UTSCH (binary) to KiCad 9 .kicad_sch (a .BLK: a KiCad block library), and Ulticap symbol
libraries (.UTLIB of the Windows versions, .LIB of the DOS version) to KiCad 9 .kicad_sym.

SchematicJob holds the checks, the questions (SchematicQuestions) and the conversion, as steps;
KIUT's command line (kiut.cli: python kiut.py) and KIUC's window run them. job_args gives the
job's options with their defaults.

The output always goes next to the design's original files, into the folder KIUB shares:
<name>_KiCad for a design, <name>_lib_KiCad for libraries and a block library. Corrected copies of
the sources go to <name>_corrected. There is no choosing another place: the fixed layout is what
lets the schematic and the board find each other's files.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from typing import List

from kiut.schematic.kiuc_model import Schematic, Sheet
from kiut.schematic.kiuc_ascii  import parse_sch, force_v5_header, collect_hierarchy_paths, \
                        scan_record_integrity, apply_record_integrity_fixes, \
                        find_space_symbol_names, sanitise_space_symbol_names, \
                        write_pin_override_corrections, \
                        looks_like_sch
from kiut.schematic.kiuc_writer   import write_schematic, write_block_library, planned_output_files, \
                           write_project_symbol_library, set_footprint_ddf, backannotation_report, \
                           defines_symbols, \
                           design_name, \
                           _sheet_stem, \
                           check_missing_sheets, dump_schematic, dump_symbols, \
                           set_use_kicad_colors, set_label_keep_alignment, TUNING_SPEC, set_tuning, \
                           set_text_font, get_text_font, font_installed, KICAD_FONT, \
                           get_pin_override_warnings, get_pin_override_corrections, \
                           get_pin_override_corrected_components, check_slash_symbol_refdes
from kiut.schematic.kiuc_refdes import (detect_offending_refdes, reannotate_hierarchy,
                               reannotate_utsch, write_log, write_corrected_log,
                               ReannotateResult)
from kiut.schematic import kiuc_binary as kb
from kiut.schematic import kiuc_binary_semantic as ks
from kiut.schematic import kiuc_library as kl
from kiut.core import corrected as kcor
from kiut.core.job import Job, Stop

from kiut import APP_DIR                       # noqa: E402
from kiut.core import settings                 # noqa: E402

_CONFIG_FILE    = APP_DIR / 'kiuc.ini'
_TUNING_SECTION = 'tuning'

def _apply_text_font(choice: str) -> None:
    """Set the writer's text font. A font KIUC knows (kiuc_writer.
    KNOWN_FONT_FILES) that isn't installed falls back to KiCad's own font:
    KiCad installs no fonts of its own, so the face would only make KiCad
    substitute another font. Any other name is used as given, with a note
    that it wasn't checked. The font used is always reported, as it changes
    how the texts look."""
    installed = font_installed(choice)
    if installed is False:
        _out(f'WARNING: {choice} is not installed on this computer; using {KICAD_FONT} instead.')
        choice = KICAD_FONT
    set_text_font(choice)
    _out(f'Text font: {get_text_font()}')
    if installed is None:
        _out(f'NOTE: whether {get_text_font()} is installed was not checked: where it is not, '
             f'KiCad shows another font.')


def _out(text='', file=None):
    """Report a line (the job's say, and the writer's reports): printed."""
    print(text, file=file or sys.stdout)


def board_comparison(out_dir: Path, base: str) -> List[tuple]:
    """The schematic just written against the design's board already in
    out_dir (<base>.kicad_pcb, converted before): which footprints stay
    linked to their symbols, which changed, which parts are on one side only
    (kiut.core.kicad_links). (level, line)s; none without a board."""
    from kiut.core import kicad_links
    board = kicad_links.board_links(Path(out_dir) / f'{base}.kicad_pcb')
    links = kicad_links.schematic_links(out_dir, base) if board is not None else None
    if board is None or links is None:
        return []
    return kicad_links.report(kicad_links.compare(links, board), f'{base}.kicad_pcb',
                              'Convert the schematic and the board together (kiut.py X.SCH X.DDF) to link '
                              'them again, or re-link them in KiCad (Update PCB from Schematic, by reference).')


def _apply_record_integrity(prep, sch_files, resolutions):
    """Apply the record-integrity resolutions to the sheets' corrected copies
    (kiut.core.corrected), never the originals: each issue moves to its sheet's
    copy, then the copies changed are recorded. Returns the sheets' copies."""
    copies = prep.folder.copies_for(sch_files, prep.sheets)
    apply_record_integrity_fixes(resolutions, dict(zip(sch_files, copies)))
    for f in copies:
        prep.folder.record(f, 'record-integrity')
    return copies


def _fix_ddf(args, prep):
    """The DDF a refdes fix rewrites: --ddf, else the design's own; either one
    read from its corrected copy when there is one (KIUB's V4.60, an earlier
    fix)."""
    if args.ddf:
        return str(prep.folder.working(kcor.locate(args.ddf).original))
    ddf = prep.working_ddf()
    return str(ddf) if ddf else None


def _refdes_log(result, root: Path, out_dir: Path) -> Path:
    """Write the refdes log: an applied fix's into the corrected folder, with
    the copies it describes; a fix not applied (declined, or nothing that
    could be renamed) next to the conversion's output, as
    <name>_refdes_log.txt. Returns its path."""
    if result.applied:
        return write_corrected_log(result, root, root_sch_name=root.name)
    path = out_dir / f'{design_name(root)}_refdes_log.txt'
    write_log(result, path, root_sch_name=root.name)
    return path


def _load_tuning_from_ini() -> dict:
    """Load any fine-tuning values saved previously (e.g. via the GUI's
    Fine-tuning dialog) from kiuc.ini, so CLI conversions stay in
    sync with whatever the GUI was last tuned to. Names not present in the
    file are left at kiuc_writer's built-in defaults."""
    return settings.floats(settings.load(_CONFIG_FILE), _TUNING_SECTION,
                           [name for name, *_ in TUNING_SPEC])


def job_args(inputs, out, **values) -> argparse.Namespace:
    """A SchematicJob's options with their defaults (KIUT's command line takes
    them from the settings file); values overrides any of them. inputs: the
    files given, out: the output folder."""
    args = argparse.Namespace(
        inputs=[str(f) for f in inputs], out=str(out), yes=False, dump=False, dump_symbols=False,
        block=False, v5=False, fix_refdes=False, ddf=None, allow_missing_sheets=False,
        kicad_colors=False, label_keep_alignment=False, font=KICAD_FONT, quiet=False)
    for name, value in values.items():
        if not hasattr(args, name):
            raise TypeError(f'job_args: no option {name!r}')
        setattr(args, name, value)
    return args


class SchematicQuestions:
    """KIUC's questions before and during a conversion (SchematicJob), one
    method each. A front end answers them its way: KIUT's command line
    (kiut.cli) or KIUC's window."""

    def redo(self, text: str, default: bool) -> bool:
        """The corrected copies no longer match the originals (text): redo
        them from the originals?"""
        raise NotImplementedError

    def missing_sheets(self, missing: List[str], utsch: bool) -> bool:
        """Sub-sheets the hierarchy refers to that aren't there (missing:
        one line each): convert anyway, without them?"""
        raise NotImplementedError

    def record_integrity(self, issues_by_file: dict):
        """Merged records in the sheets (issues_by_file: each sheet's
        RecordIntegrityIssue list, at least one issue in all): the
        resolutions, (issue, (fieldA, fieldB)) each, or None to stop."""
        raise NotImplementedError

    def fix_refdes(self, offending: list, utsch: bool) -> bool:
        """Designators that don't end in a digit: write corrected copies
        and convert those?"""
        raise NotImplementedError

    def replace(self, existing: List[Path]) -> bool:
        """Output files that already exist: replace them?"""
        raise NotImplementedError


class SchematicJob(Job):
    """A KIUC conversion (kiut.core.job): its checks and questions
    (SchematicQuestions), then the conversion, as steps. Three paths, each
    steps() in the command line's order:

    - symbol libraries (lib_files): check_library_folder, convert_libraries
    - a .UTSCH design (utsch_file): check_files, use_corrected, parse_utsch,
      check_missing_sheets, [dump], check_refdes_utsch, write_utsch
    - .SCH/.BLK sheets (sch_files): check_files, discover_sheets,
      use_corrected, fix_v5, parse, check_missing_sheets,
      check_record_integrity, [dump], fix_space_names, check_slash_names,
      check_refdes, write, correct_pin_labels

    args: the job's options (job_args), args.out the output
    folder (the design's own). The job's state: files (the files converted,
    each one's corrected copy where there is one), prep, sheets, schematic."""

    # The command line's words for what decides the designator fix.
    UNCHANGED_HINT = ('Re-run with --fix-refdes to write {what} automatically, or use '
                      'kiuc_refdes.py directly.')
    FIX_WHY = '(--fix-refdes)'
    # Said when the fix wrote the DDF's corrected copy.
    DDF_NOTE = 'A fresh KIUB conversion of the new DDF file is required.'

    def __init__(self, args: argparse.Namespace, questions: SchematicQuestions,
                 sch_files=(), utsch_file: Path | None = None, lib_files=()) -> None:
        super().__init__(questions)
        self.args = args
        self.files: List[Path] = list(sch_files)
        self.utsch_file = utsch_file
        self.lib_files: List[Path] = list(lib_files)
        self.prep = None
        self.sheets: List[Sheet] = []
        self.schematic: Schematic | None = None
        self.out_dir = Path(args.out)
        self.utsch_read = None

    def say(self, text: str = '', level: str = 'info') -> None:
        """Report a line: printed, or kept for the -q log (_out); 'err' on
        the error output."""
        _out(text, file=sys.stderr if level == 'err' else None)

    def refuse(self, text: str) -> None:
        """A refusal (text: why): its lines as ERROR lines on the error
        output. A window shows it in a dialog instead (Stop.refusal)."""
        for line in text.splitlines():
            self.say(f'ERROR: {line}' if line else '', 'err')

    def steps(self) -> None:
        args = self.args
        if self.lib_files:
            self.check_library_folder()
            self.convert_libraries()
            return
        self.check_files()
        if self.utsch_file is not None:
            self.use_corrected()
            self.parse_utsch()
            self.check_missing_sheets()
            if args.dump or args.dump_symbols:
                self.dump()
                return
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self.check_refdes_utsch()
            self.write_utsch()
            return
        self.discover_sheets()
        self.use_corrected()
        self.fix_v5()
        self.parse()
        self.check_missing_sheets()
        if self.check_record_integrity():
            self._reparse()
        if args.dump or args.dump_symbols:
            self.dump()
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if self.fix_space_names():
            self._reparse()
        self.check_slash_names()
        if self.check_refdes():
            # Continue the SAME conversion run on the new, renamed files -- one
            # extra, deliberate parse pass over the corrected copies, not a
            # second attempt at the original ones.
            self._reparse()
        self.write()
        self.correct_pin_labels()

    # ── symbol libraries ─────────────────────────────────────────────────────
    def check_library_folder(self) -> None:
        """The output folder must be writable, or be made (KIUT's library
        folder, or the libraries' own folder: kiut.core.folders)."""
        from kiut.core import folders
        if not folders.writable(self.out_dir):
            home = kcor.locate(self.lib_files[0]).home if self.lib_files else None
            message = (kcor.READ_ONLY_MESSAGE.format(folder=home)
                       if home is not None and self.out_dir.parent == home
                       else folders.READ_ONLY.format(folder=self.out_dir.parent))
            self.refuse(message)
            raise Stop(refusal=message)

    def convert_libraries(self) -> None:
        """Convert each library to <out>/<name>.kicad_sym with its own
        <name>.log (kiuc_library.convert_library). A library that cannot be
        read is reported and the next one converted; exit code 1 if any
        failed."""
        set_use_kicad_colors(self.args.kicad_colors)
        set_label_keep_alignment(self.args.label_keep_alignment)
        _apply_text_font(self.args.font)
        preamble = ((f'Text font: {get_text_font()}', 'info'),)
        # A library of the same name already in the folder (KIUT's Symbols folder, shared by every
        # library converted there): where it came from, before the replace question.
        from kiut.schematic.kiuc_writer import sym_lib_source
        for f in self.lib_files:
            sym = kl.library_output_files(f, self.out_dir)[0]
            if sym.exists():
                was = sym_lib_source(self.out_dir, sym.stem)
                same = was is not None and Path(was) == Path(str(f).replace(chr(92), '/'))
                self.say(f'NOTE: {sym.name} is already in {self.out_dir.name}'
                         + (f', converted earlier from the same file, {f}.' if same else
                            f', converted from {was} -- not from {f}.' if was else '.'))
        self.check_replace([o for f in self.lib_files for o in kl.library_output_files(f, self.out_dir)])
        done = 0
        for f in self.lib_files:
            res = kl.convert_library(f, self.out_dir, preamble)
            for text, level in res.report:
                self.say(text, 'err' if level == 'err' else 'info')
            done += res.ok
        self.say(f'Libraries: {done} of {len(self.lib_files)} converted.')
        if done != len(self.lib_files):
            raise Stop()

    # ── a design: both paths ─────────────────────────────────────────────────
    def check_files(self) -> None:
        """A file's extension says what it claims to be; this checks what it
        actually is -- most needed for a file picked through a GUI file
        dialog's own "All files" fallback, which bypasses the extension
        filter entirely, but applied here uniformly so a CLI caller gets
        the same protection."""
        if self.utsch_file is not None:
            bad = [f for f in [self.utsch_file] if not kb.looks_like_utsch(f)]
        else:
            bad = [f for f in self.files if not looks_like_sch(f)]
        if bad:
            kind = 'a .UTSCH container' if self.utsch_file is not None else 'an Ulticap .SCH/.BLK file'
            self.say(f"{bad[0]} does not look like {kind} -- wrong file, or not "
                     f"Ulticap data at all. Not attempting a conversion.", 'err')
            raise Stop()

    def use_corrected(self) -> None:
        """The design's corrected folder (kiut.core.corrected): refuse a
        design that can't be converted safely, ask when the corrected copies
        no longer match the originals (redo from the originals, or cancel);
        the files to convert -- each one's corrected copy where there is one
        -- and the Preparation (prep)."""
        inputs = [self.utsch_file] if self.utsch_file is not None else self.files
        prep = kcor.prepare(inputs, 'KIUC')
        if prep.error:
            self.refuse(prep.error)
            raise Stop(refusal=prep.error)
        question = prep.redo_question()
        if question:
            text, default = question
            if not self.questions.redo(text, default):
                raise Stop('Conversion cancelled -- nothing written.')
            prep.folder.reset(prep.originals)
            prep.status = prep.folder.check(prep.originals, prep.sheets)
            self.say(f'Corrected copies removed from {prep.folder.path}; converting the original files.',
                     'warn')
        elif prep.status.edited:
            for line in kcor.describe(prep.status):
                self.say(f'NOTE: {line}', 'warn')
        working = prep.working_sheets()
        used = [w.name for w, s in zip(working, prep.sheets) if w != s]
        if used:
            self.say(f'Using the corrected copies in {prep.folder.path}: {", ".join(used)}')
        self.prep = prep
        if self.utsch_file is not None:
            [self.utsch_file] = working
        else:
            self.files = working

    def check_missing_sheets(self) -> None:
        """Sub-sheets the hierarchy refers to that aren't there: asked. Runs
        before dump modes or any other check -- a missing sub-sheet
        invalidates everything downstream that operates on "the sheet set
        we're about to convert." For a .UTSCH, a subsheet box's own name not
        resolving to a sheet in the same document is not known to be
        possible, but it is not proven impossible either."""
        missing = check_missing_sheets(self.sheets)
        if missing and not self.questions.missing_sheets(missing, self.utsch_file is not None):
            raise Stop()

    def dump(self) -> None:
        """Dump modes -- pure inspection, never mutate or write files, so
        the refdes check/fix does not apply."""
        if self.args.dump:
            _dump(self.schematic)
        else:
            _dump_symbols(self.schematic)

    def _offending_report(self, offending, what: str) -> None:
        self.say(f'WARNING: {len(offending)} reference designator(s) do not end in a '
                 f'digit -- KiCad will treat these as unannotated:', 'warn')
        for o in offending:
            self.say(f'  WARNING:   {o.refdes}  (symbol {o.symbol_name}, sheet {o.sheet_name})', 'warn')
        self.say(f'WARNING: converting unchanged. {self.UNCHANGED_HINT.format(what=what)}', 'warn')

    def _prepare_write(self) -> str:
        """The writer's settings; the design's name (the corrected copies
        keep their original names)."""
        base = design_name(self.utsch_file if self.utsch_file is not None else self.files[0])
        set_use_kicad_colors(self.args.kicad_colors)
        set_label_keep_alignment(self.args.label_keep_alignment)
        _apply_text_font(self.args.font)
        set_tuning(self.tuning())
        # Footprint fields named as KIUB names the board's footprints (kiuc_writer.footprint_field)
        set_footprint_ddf(_fix_ddf(self.args, self.prep))
        return base

    def tuning(self) -> dict:
        """The writer's fine-tuning values (kiuc_writer.TUNING_SPEC's names):
        kiuc.ini's, as the window last saved them (_load_tuning_from_ini)."""
        return _load_tuning_from_ini()

    def _written(self, base: str) -> None:
        out_dir = self.out_dir
        if len(self.sheets) == 1:
            self.say(f'Written: {out_dir / (base + ".kicad_sch")}')
        else:
            for sh in self.sheets:
                self.say(f'Written: {out_dir / (_sheet_stem(sh.name, base) + ".kicad_sch")}')
        for line in write_project_symbol_library(self.schematic, out_dir, base) + backannotation_report():
            self.say(line)
        if self.compare_board:
            for level, line in board_comparison(out_dir, base):
                self.say({'info': '', 'warn': 'WARNING: ', 'note': 'NOTE: '}[level] + line,
                         'info' if level == 'info' else 'warn')

    # The schematic converted alone: compared with the design's board already there. False when
    # the board is converted right after it (KIUT's command line, both in one run).
    compare_board = True

    def check_replace(self, planned) -> None:
        """Output files that already exist: asked before replacing them. The
        output folder is always the design's own, so a design converted
        again -- or from both its .SCH files and its .UTSCH file -- meets
        its earlier files here."""
        existing = [p for p in planned if p.exists()]
        if existing and not self.questions.replace(existing):
            raise Stop('Conversion cancelled -- nothing written.')

    # ── a .UTSCH design ──────────────────────────────────────────────────────
    # Deliberately not sharing the sheets' steps: most of what they do before
    # the write -- sub-sheet discovery by FILE= path, the --v5 header patch,
    # record-integrity scanning -- is specific to the ASCII text format and
    # has no binary equivalent (one .UTSCH already contains its whole
    # hierarchy, discovered by name; there is no ASCII header to patch or
    # merged text record to disambiguate). What carries over is everything
    # downstream of parsing: the missing-sheets and refdes checks work on
    # Sheet objects, and write_schematic takes this reader's output as it
    # takes kiuc_ascii's. --block and --v5 are refused by main(). The
    # pin-override correction has nothing to do here: a .UTSCH part's pin
    # texts belong to their pin (Component.pin_owned_poe).
    def parse_utsch(self) -> None:
        self.say(f'Parsing: {self.utsch_file}')
        # Root sheet named after the .UTSCH file itself -- see read_schematic_file.
        self.schematic, naming_warnings = ks.read_schematic_file(self.utsch_file)
        for w in naming_warnings:
            self.say(f'WARNING: {w}', 'warn')
        variants_line = ks.variant_summary(self.schematic)
        if variants_line:
            self.say(variants_line)
        self.sheets = self.schematic.sheets

    def check_refdes_utsch(self) -> bool:
        """Designators that don't end in a digit (_refdes): with a fix, a
        corrected copy of the .UTSCH (kiuc_refdes.reannotate_utsch, which
        replaces the stored designator strings, nothing else) and the DDF
        go into the corrected folder, and the conversion continues on that
        copy (read again: utsch_read); without, a warning and a log. True if
        the copy is converted."""
        offending = detect_offending_refdes({_sheet_stem(sh.name, '').upper(): sh for sh in self.sheets})
        if not offending:
            return False
        return self._refdes(offending, True)

    def write_utsch(self) -> None:
        base = self._prepare_write()
        self.check_replace(planned_output_files([sh.name for sh in self.schematic.sheets], self.out_dir, base,
                                                symbol_library=defines_symbols(self.schematic.sheets)))
        write_schematic(self.schematic, self.out_dir, base_name=base)
        self._written(base)

    # ── .SCH/.BLK sheets ─────────────────────────────────────────────────────
    def discover_sheets(self) -> None:
        """Auto-discover hierarchical sub-sheets, mirroring the GUI and
        viewer: any .SCH input's own referenced sub-sheets are added, in
        addition to whatever was explicitly listed. Block libraries (.BLK)
        are never hierarchical. The first explicitly-given input stays
        first -- it must remain the root sheet (write_schematic and
        write_block_library use sheets[0] as root)."""
        for f in list(self.files):
            if f.suffix.upper() != '.SCH':
                continue
            # Discovery parses every file in the hierarchy, but its errors are
            # deliberately dropped: every file it finds is parsed again in
            # parse(), and that parse reports the same errors through _out().
            # Left to load_sheet's stderr fallback instead, they would print
            # twice and bypass --quiet entirely.
            for child in collect_hierarchy_paths(f, errors=[]):
                if child not in self.files:
                    self.files.append(child)
                    self.say(f'Auto-loaded sub-sheet: {child}')

    def fix_v5(self) -> None:
        """--v5: the V5 header fix -- in the corrected copies, never the
        originals (kiut.core.corrected), like every correction below."""
        if not self.args.v5:
            return
        prep = self.prep
        self.files = prep.folder.copies_for(self.files, prep.sheets)
        for f in self.files:
            ok, detail = force_v5_header(f)
            if ok:
                prep.folder.record(f, 'v5-header', f'{detail} -> 5 00 00000000')
                self.say(f'V5 header fix: {f.name}  [{detail}] → 5 00 00000000')
            else:
                self.say(f'WARNING: V5 header fix failed for {f.name}: {detail}', 'warn')

    def parse(self) -> None:
        self.sheets = []
        for f in self.files:
            self.say(f'Parsing: {f}')
            sheet, errors = parse_sch(f)
            for e in errors:
                self.say(e, 'err')
            self.sheets.append(sheet)
        self.schematic = Schematic(sheets=self.sheets)

    def _reparse(self) -> None:
        """Parse the sheets again after a correction changed them."""
        self.sheets = []
        for f in self.files:
            sheet, errors = parse_sch(f)
            for e in errors:
                self.say(e, 'err')
            self.sheets.append(sheet)
        self.schematic = Schematic(sheets=self.sheets)

    def check_record_integrity(self) -> bool:
        """Merged records (see kiuc_ascii.py's module-level comment for the
        mechanism): asked how to resolve them; the resolutions go into the
        sheets' corrected copies. True if the files changed."""
        issues_by_file = {f: scan_record_integrity(f) for f in self.files}
        total = sum(len(v) for v in issues_by_file.values())
        if not total:
            return False
        resolutions = self.questions.record_integrity(issues_by_file)
        if resolutions is None:
            raise Stop()
        if resolutions:
            self.files = _apply_record_integrity(self.prep, self.files, resolutions)
        self.report_record_integrity(resolutions, total)
        return bool(resolutions)

    def report_record_integrity(self, resolutions: list, total: int) -> None:
        """The record-integrity summary, after the resolutions were applied."""
        if resolutions:
            self.say(f'Record integrity: {len(resolutions)} of {total} issue(s) '
                     f'corrected automatically.')

    def fix_space_names(self) -> None:
        """Symbol names with a space (Ulticap V4.91 can't open them):
        corrected like the rest, so the corrected design opens in every
        Ulticap version; the KiCad symbol names follow (kiuc_ascii's comment
        above sanitise_space_symbol_names). True if the files changed."""
        if not any(find_space_symbol_names(f) for f in self.files):
            return False
        prep = self.prep
        self.files = prep.folder.copies_for(self.files, prep.sheets)
        for f in self.files:
            renamed = sanitise_space_symbol_names(f)
            if renamed:
                detail = ', '.join(f'{old!r} -> {new!r}' for old, new in renamed.items())
                prep.folder.record(f, 'symbol-names', detail)
                self.say(f'Symbol names with a space (Ulticap V4.91 cannot open them): {f.name}: {detail}')
        return True

    def check_slash_names(self) -> None:
        """A '/' symbol name placed as J1A: V5.72's import would split it
        (FILEFORMAT_ULTICAP_UTSCH.md, Appendix D)."""
        for w in check_slash_symbol_refdes(self.sheets):
            self.say(w)

    def check_refdes(self) -> bool:
        """Designators that don't end in a digit: KiCad won't take them as
        annotated (kiuc_refdes.py). With a fix, corrected copies of the
        hierarchy (and the DDF) are written and the run continues on them
        (the caller parses them again); without, a warning and a log. True
        if the files changed."""
        offending = detect_offending_refdes({Path(sh.name).stem.upper(): sh for sh in self.sheets})
        if not offending:
            return False
        return self._refdes(offending, False)

    def _refdes(self, offending: list, utsch: bool) -> bool:
        """The designator question and fix, both paths: asked whether to fix
        (fix_refdes); the fix (reannotate) and its report; the files to
        convert switched to the corrected copies when the fix wrote them."""
        root = self.utsch_file if utsch else self.files[0]
        if not self.questions.fix_refdes(offending, utsch):
            self.refdes_unchanged(offending, root, utsch)
            return False
        ddf_path = self.refdes_ddf()
        result, log_path = self.reannotate(root, ddf_path, utsch)
        self.report_refdes_fixed(result, ddf_path, log_path, utsch)
        if not result.applied:
            return False
        if utsch:
            self.utsch_file = result.sch_files_written[0]
            self.utsch_read = ks.read_schematic_file(self.utsch_file)
            self.schematic = self.utsch_read[0]
            self.sheets = self.schematic.sheets
        else:
            self.files = list(result.sch_files_written)
        return True

    def refdes_ddf(self) -> str | None:
        """The DDF a designator fix rewrites (_fix_ddf: --ddf, else the
        design's own)."""
        return _fix_ddf(self.args, self.prep)

    def reannotate(self, root: Path, ddf_path: str | None, utsch: bool):
        """The fix: corrected copies (and the DDF's) written, and its log
        (_refdes_log). Returns (result, the log's path)."""
        result = (reannotate_utsch if utsch else reannotate_hierarchy)(root, ddf_path=ddf_path)
        if not result.applied:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        return result, _refdes_log(result, root, self.out_dir)

    def report_refdes_fixed(self, result, ddf_path: str | None, log_path: Path, utsch: bool) -> None:
        """What the fix did."""
        self.say(f'{len(result.offending)} offending component instance(s) found '
                 f'-- fixing automatically {self.FIX_WHY}:')
        for ch in result.changes:
            self.say(f'  {ch.old}  ->  {ch.new}')
        if utsch:
            if result.placeholders:
                self.say('WARNING: not renamed (placeholder designators, no original stored): '
                         + ', '.join(result.placeholders), 'warn')
            for e in result.errors:
                self.say(f'WARNING: {e}', 'warn')
        for p in result.sch_files_written:
            self.say(f'Written: {p}')
        if result.ddf_file_written:
            self.say(f'Written: {result.ddf_file_written}')
            self.say(self.DDF_NOTE)
        elif ddf_path:
            self.say(f'WARNING: DDF path given/found ({ddf_path}) but could not be used; '
                     f'see {log_path}', 'warn')

    def refdes_unchanged(self, offending: list, root: Path, utsch: bool) -> None:
        """Designators left as they are: the warning, and the log next to
        the output."""
        self._offending_report(offending, 'a corrected copy' if utsch else 'corrected copies')
        _refdes_log(ReannotateResult(offending=offending), root, self.out_dir)

    def write(self) -> None:
        """The KiCad schematic (or, --block, the block library)."""
        base = self._prepare_write()
        names = [sh.name for sh in self.schematic.sheets]
        if self.args.block:
            self.check_library_folder()
            lib_dir = self.out_dir / f'{base}.kicad_blocks'
            if lib_dir.exists():                # KIUT's Blocks folder, shared: where it came from
                from kiut.core import kicad_project
                was = kicad_project.lib_source(self.out_dir, 'dbl', kicad_project.kicad_name(base))
                src = Path(self.args.inputs[0])
                same = was is not None and Path(was) == Path(str(src.resolve()).replace(chr(92), '/'))
                self.say(f'NOTE: {lib_dir.name} is already in {self.out_dir.name}'
                         + (f', converted earlier from the same file, {src}.' if same else
                            f', converted from {was} -- not from {src}.' if was else '.'))
            self.check_replace(planned_output_files(names, self.out_dir, base, block=True))
            write_block_library(self.schematic, self.out_dir, lib_name=base)
            self.say(f'Written: {lib_dir}')
            from kiut.schematic.kiuc_writer import add_to_block_lib_table
            self.say(add_to_block_lib_table(self.out_dir, base, Path(self.args.inputs[0]).resolve()))
            for line in backannotation_report():
                self.say(line)
        else:
            self.check_replace(planned_output_files(names, self.out_dir, base,
                                                    symbol_library=defines_symbols(self.schematic.sheets)))
            write_schematic(self.schematic, self.out_dir, base_name=base)
            self._written(base)

    def correct_pin_labels(self) -> None:
        """Pin-label override corrections (kiuc_writer.
        _resolve_pin_override_poes): made during the write; what's left is
        reporting them and patching the sheets' corrected copies."""
        for w in get_pin_override_warnings():
            self.say(f'  {w}')
        pin_corrections = get_pin_override_corrections()
        if not pin_corrections:
            return
        prep = self.prep
        self.files = prep.folder.copies_for(self.files, prep.sheets)
        for s in write_pin_override_corrections(pin_corrections, {f.name: f for f in self.files}):
            self.say(f'  {s}')
        for f in self.files:
            prep.folder.record(f, 'pin-labels')
        corrected_comps = get_pin_override_corrected_components()
        self.say(f'Pin-label overrides: {len(pin_corrections)} override(s) auto-corrected '
                 f'across {len(corrected_comps)} component(s).')


def _dump(sch: Schematic):
    """CLI wrapper: routes dump_schematic output to stdout."""
    def _print(text, tag=''):
        _out(text)
    dump_schematic(sch, _print)


def _dump_symbols(sch: Schematic):
    """CLI wrapper: routes dump_symbols output to stdout."""
    def _print(text, tag=''):
        _out(text)
    dump_symbols(sch, _print)
