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
kiuc_refdes.py — standalone reference-designator reannotation tool.

Some Ulticap component reference designators (e.g. mating-connector pairs
like PL95MV/SK95MV) are literal, user-meaningful strings that do not end in
a digit. KiCad's schematic editor data model requires every reference
designator to end in a number to be considered "annotated": ERC flags a
non-digit-ending refdes as unannotated, and KiCad's netlist generation
refuses to proceed until it's fixed. There is no value KiCad will accept
here that doesn't change the refdes text, so any fix breaks the existing
match between the Ulticap SCH and its accompanying Ultiboard DDF/PCB unless
BOTH are updated identically and consistently. There is no fully
automatic, risk-free solution to this -- this tool exists to make the
*manual* part of the job (deciding whether to do it, and verifying the
result) as small and well-documented as possible, by doing the tedious,
error-prone part (finding and consistently renaming every occurrence
across both file formats and an entire sheet hierarchy) for the user.

This module NEVER edits the original files. The corrected files go into
the design's corrected folder, <name>_corrected next to the originals
(kiut.core.corrected), under their original names: the folder is a complete
design Ulticap and Ultiboard open as it is, its FILE= references unchanged,
and copying its files over the originals adopts the fix. A .SCH hierarchy is
rewritten as text (reannotate_hierarchy); a .UTSCH design, a binary file,
through kiuc_binary_write (reannotate_utsch). Each reads the design's
working set -- a file's corrected copy where there is one -- so the fix
builds on earlier corrections, KIUB's V4.60 DDF included.

New refdes values are computed as old_refdes + the lowest unused integer
suffix starting at 1 (e.g. "PL95MV" -> "PL95MV1"), checked for collisions
against every refdes anywhere in the sheet hierarchy (KiCad refdes
uniqueness is project-wide, not per-sheet). This intentionally matches
what KiCad's own Annotate tool does to a no-digit refdes, so if the user
later runs Annotate in KiCad, an already-suffixed refdes like this is a
no-op there -- nothing drifts further out of sync after this fix is
applied.

Pure logic, no UI: every "should I do this" decision is a parameter, not
an interactive prompt, so this module can be wrapped by:
  - the KIUT window (its designator fix and Fix designators only…)
  - KIUT's command line (kiut.py, its designator fix and --fix-only), through
    the schematic's job
"""
from __future__ import annotations

import re
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kiut.schematic.kiuc_ascii import parse_sch
from kiut.schematic.kiuc_model import Sheet, nonhomogeneous_devices, symbol_gate_type
from kiut.core.corrected import CorrectedFolder, locate
from kiut.core import ddf

CORRECTION = 'refdes'                # the correction's name in the corrected folder's manifest
LOG_NAME = '_refdes_log.txt'         # its log in the corrected folder (CorrectedFolder.write_log_file)


# ── refdes-validity check ────────────────────────────────────────────────────

# A KiCad-valid refdes is PREFIX + DIGITS, optionally followed by a SINGLE
# trailing letter indicating the unit -- but ONLY when the symbol genuinely
# HAS multiple units. KiCad's own ERC distinguishes two completely separate
# checks: "unannotated" (ERCE_UNANNOTATED) and "invalid part count for a
# multi-unit symbol" (ERCE_EXTRA_UNITS) -- they sound like they could cover
# this independently, but a direct KiCad test (refdes "R1A" placed on a
# genuinely single-unit symbol) confirmed KiCad's annotation/netlist gate
# does NOT treat that shape as already-annotated: it still produces the
# same mandatory-annotate block as a bare no-digit refdes. So the trailing
# single letter is only accepted as a real unit suffix -- and the refdes
# only counted as already annotated -- when the symbol it's placed on
# actually has more than one unit (Ulticap's PARTS attribute > 1, the same
# field already used elsewhere in this project's kiuc_writer.py for
# multi-gate lib_symbol detection). A refdes with that exact shape on a
# PARTS<=1 symbol is treated exactly like a bare no-digit refdes (e.g.
# "PL95MV"): offending, and fixed the same way (append the next free
# integer suffix).
#
# e.g. "U2A"/"U2B" (gates of a real dual-gate IC, PARTS=2) are valid,
# already-annotated refdes. "R1A" on a PARTS=1 (or absent) resistor symbol
# is NOT valid, despite matching the same string shape, and must be
# treated as offending.
_REFDES_SHAPE_RE = re.compile(r'^(.+\d)([A-Za-z])\Z')   # captures (base, unit_letter)


def is_genuinely_annotated(refdes: str, parts: int) -> bool:
    """True if KiCad will accept this refdes as already-annotated, given
    the actual unit/part count of the symbol it's placed on.

    parts is the symbol's PARTS attribute (Ulticap; 1 or absent = single
    unit). A bare PREFIX+DIGITS refdes (no trailing letter) is always
    valid regardless of parts. A refdes with one trailing letter after the
    digits is only valid when parts > 1; otherwise it's offending, exactly
    like a refdes with no trailing digit at all.
    """
    if not refdes:
        return False
    m = _REFDES_SHAPE_RE.match(refdes)
    if m:
        return parts > 1
    return refdes[-1].isdigit()


# ── result data structures ──────────────────────────────────────────────────
# Plain data, no behaviour -- every UI wrapper renders these however it likes.

@dataclass
class OffendingRef:
    refdes: str
    sheet_name: str          # the .SCH stem this component lives on
    symbol_name: str
    parts: int = 1           # the symbol's unit count as the check saw it


@dataclass
class RefdesChange:
    old: str
    new: str


@dataclass
class ReannotateResult:
    """Everything a caller (any UI) needs to report what happened."""
    offending: List[OffendingRef] = field(default_factory=list)
    changes: List[RefdesChange] = field(default_factory=list)
    sch_files_written: List[Path] = field(default_factory=list)
    ddf_file_written: Optional[Path] = None
    ddf_refdes_not_found: List[str] = field(default_factory=list)
    # .UTSCH only: offending refdes left alone because the design stores no
    # real designator for them (Ulticap's import placeholder "!n!").
    placeholders: List[str] = field(default_factory=list)
    applied: bool = False            # True only once new files were actually written
    errors: List[str] = field(default_factory=list)


# ── hierarchy loading ────────────────────────────────────────────────────────

def _load_hierarchy(root_sch_path) -> Dict[str, Sheet]:
    """Parse root_sch_path and every sub-sheet it (transitively) references
    via FILE=, returning {stem_upper: Sheet}.

    A sheet referenced but not found on disk is silently skipped -- this
    tool only needs to touch sheets it can actually read and rewrite; a
    missing sub-sheet is a separate, pre-existing problem this tool isn't
    responsible for surfacing.
    """
    root_sch_path = Path(root_sch_path)
    folder = root_sch_path.parent
    sheets: Dict[str, Sheet] = {}

    def _visit(path: Path):
        stem = path.stem.upper()
        if stem in sheets:
            return
        if not path.exists():
            return
        sh, _errors = parse_sch(str(path))
        sheets[stem] = sh
        for comp in sh.components:
            if comp.file_ref:
                _visit(folder / comp.file_ref)

    _visit(root_sch_path)
    return sheets


# ── detection ─────────────────────────────────────────────────────────────────

def _symbol_parts(sh: Sheet, symbol_name: str) -> int:
    """The symbol's PARTS attribute (1 if absent/unparseable, matching the
    same default used throughout kiuc_writer.py for single-gate symbols).
    """
    sym = sh.symbols.get(symbol_name)
    if sym is None:
        return 1
    try:
        return int(sym.attributes.get('PARTS', '1'))
    except (TypeError, ValueError):
        return 1


def detect_offending_refdes(sheets: Dict[str, Sheet]) -> List[OffendingRef]:
    """Scan every sheet in the hierarchy for components whose refdes KiCad
    will not accept as already-annotated (see is_genuinely_annotated).
    One entry per offending COMPONENT INSTANCE (not per unique refdes
    text) -- collision-checking and the log both need to know exactly
    which sheet/component each one belongs to, and the same literal refdes
    text could in principle appear validly on more than one placement in
    different design contexts before it's renamed.
    """
    found: List[OffendingRef] = []
    # A gate of a non-homogeneous part (one symbol per gate type,
    # FILEFORMAT_ULTICAP_UTSCH.md §5.4) is one unit of a multi-unit KiCad symbol: U2A/U2B
    # are annotated, whatever each gate type's own PARTS says.
    devices = nonhomogeneous_devices(list(sheets.values()))
    for stem, sh in sheets.items():
        for comp in sh.components:
            rd = comp.refdes
            if not rd:
                continue
            if comp.file_ref:
                continue   # hierarchical sheet-symbol placement, not a real refdes
            parts = _symbol_parts(sh, comp.symbol_name)
            gate = symbol_gate_type(sh.symbols.get(comp.symbol_name))
            if gate and gate[0] in devices:
                parts = max(parts, 2)
            if not is_genuinely_annotated(rd, parts):
                found.append(OffendingRef(refdes=rd, sheet_name=stem,
                                          symbol_name=comp.symbol_name, parts=parts))
    return found


def _all_refdes(sheets: Dict[str, Sheet]) -> Set[str]:
    """Every refdes in use anywhere in the hierarchy -- project-wide
    uniqueness, matching KiCad's own refdes scope (a refdes must be unique
    across the whole project, not just within one sheet).
    """
    out: Set[str] = set()
    for sh in sheets.values():
        for comp in sh.components:
            if comp.refdes and not comp.file_ref:
                out.add(comp.refdes)
    return out


# ── new-name computation ─────────────────────────────────────────────────────

def compute_new_refdes(old_refdes: str, all_refdes: Set[str]) -> str:
    """old_refdes + the lowest unused integer suffix, starting at 1.
    Collision-checked against every refdes anywhere in the hierarchy.
    """
    n = 1
    while f'{old_refdes}{n}' in all_refdes:
        n += 1
    return f'{old_refdes}{n}'


_UNIT_SUFFIX_RE = re.compile(r'^(.+?)([A-Za-z])\Z')   # (package, unit letter)


def _plan_sch_renames(offending: List[OffendingRef],
                      all_refdes: Set[str]) -> Tuple[List[RefdesChange], List[Tuple[str, str]]]:
    """The renames for a .SCH hierarchy: (changes, placed).

    changes are what the DDF and the log see -- one per package; placed are
    the REFDES= texts to rewrite in the sheets.

    A .SCH stores a multi-unit part's designator on every placement, unit
    letter included (ICA, ICB, ...); the DDF names the package (IC). When
    such a part is flagged, its package designator itself does not end in
    a digit, so the package is renamed -- once, for all its units -- and
    each placement keeps its unit letter (IC -> IC1: IC1A, IC1B, ...).
    Renaming each placement on its own (ICA1, ICB1, ...) would split the
    package into separate parts and lose the DDF's name for it. A
    single-unit designator is renamed as a whole (PL95MV -> PL95MV1).
    all_refdes is updated with every name given out.
    """
    changes: List[RefdesChange] = []
    placed: List[Tuple[str, str]] = []
    done: Set[str] = set()
    for o in offending:
        if o.refdes in done:
            continue   # same literal refdes already scheduled for renaming
        m = _UNIT_SUFFIX_RE.match(o.refdes)
        if o.parts > 1 and m:
            package = m.group(1)
            members = sorted({x.refdes for x in offending if x.parts > 1
                              and _UNIT_SUFFIX_RE.match(x.refdes)
                              and _UNIT_SUFFIX_RE.match(x.refdes).group(1) == package})
            units = sorted({r[-1] for r in members} | {chr(ord('A') + i) for i in range(o.parts)})
            new = _next_free_designator(package, units, {r.upper() for r in all_refdes})
            all_refdes.add(new)
            all_refdes.update(f'{new}{u}' for u in units)
            changes.append(RefdesChange(old=package, new=new))
            placed.extend((r, f'{new}{r[-1]}') for r in members)
            done.update(members)
        else:
            new = compute_new_refdes(o.refdes, all_refdes)
            all_refdes.add(new)   # reserve it so a later offending refdes can't collide with it
            changes.append(RefdesChange(old=o.refdes, new=new))
            placed.append((o.refdes, new))
            done.add(o.refdes)
    return changes, placed


# ── SCH text-level rewriting ─────────────────────────────────────────────────
# All rewriting operates on the RAW source text (read/written as CP437, a
# lossless 1:1 byte<->codepoint mapping, so every byte outside the matched
# regions round-trips exactly) -- never by re-serializing through the
# parser/writer. This tool's only job is a surgical, minimal rename; every
# other byte of the original file (formatting, comments, unrelated data)
# must come through completely unchanged.

def _split_at_nrm_marker(text: str) -> Tuple[str, str]:
    """Split raw SCH text into (schematic_part, nrm_part) at the '**' line.

    Everything from a line that is exactly '**' onward is appended by a
    separate tool (NRM -- Netlist Representation Model), used by Ulticap to
    interface with other programs, and is not always present. The existing
    parser (_AsciiParser.parse) already stops reading at this exact marker,
    so refdes/FILE= occurrences anywhere in the NRM section are outside
    what this project's schematic conversion ever looks at -- and, since
    NRM's own data format isn't characterized here, outside what this tool
    should attempt to rewrite. schematic_part includes the '**' marker line
    itself; nrm_part is everything after it, both returned with their
    original line endings intact so concatenating them back together
    reproduces the input exactly when no substitution applies.
    """
    idx = 0
    while True:
        nl = text.find('\n', idx)
        if nl == -1:
            return text, ''   # no '**' found -- nothing to split off
        line = text[idx:nl]
        if line.rstrip('\r') == '**':
            return text[:nl + 1], text[nl + 1:]
        idx = nl + 1


def _rewrite_refdes_in_sch_text(text: str, old: str, new: str) -> str:
    """Replace REFDES=<old> with REFDES=<new> on a *C component instance's
    attribute line. Anchored to end-of-line so e.g. old='PL95MV' can never
    accidentally match an unrelated, already-distinct 'PL95MV1'.
    """
    pattern = re.compile(r'REFDES=' + re.escape(old) + r'(\r\n|\r|\n|$)')
    return pattern.sub(lambda m: f'REFDES={new}' + m.group(1), text)


# ── DDF text-level rewriting ─────────────────────────────────────────────────

def _ddf_refdes(ddf_text: str) -> Set[str]:
    """Every designator a DDF's components already use (kiut.core.ddf).

    A DDF can contain board-only items with no schematic representation at
    all -- mechanical parts such as mounting spacers are a real example,
    confirmed against an actual board file alongside this tool's
    development. The SCH-derived collision set (_all_refdes) has no
    visibility into these, so without this check a computed new refdes
    could coincide with an existing DDF-only refdes that the SCH model
    never knew about, producing a genuine duplicate reference in the
    output DDF.
    """
    return set(ddf.component_refdes(ddf_text))


# ── main entry point ─────────────────────────────────────────────────────────

def reannotate_hierarchy(root_sch_path, ddf_path: Optional[str] = None,
                         tool: str = 'KIUC') -> ReannotateResult:
    """Detect offending refdes across the whole hierarchy rooted at
    root_sch_path, and -- if any are found -- write corrected copies of
    EVERY sheet in the hierarchy (not just the ones containing an offending
    refdes: the corrected folder must hold a complete design that Ulticap
    opens as it is) plus the DDF, if provided, with collision-safe new
    refdes values applied throughout.

    root_sch_path is the root as the design's working set gives it (its
    corrected copy when there is one, kiut.core.corrected), so the fix builds on
    earlier corrections. Every copy goes into the design's corrected folder
    under its original name; FILE= references stay as they are. Nothing at
    an original's path is ever modified. `tool` names the writer in the
    folder's manifest.

    New refdes values are collision-checked against every refdes in the
    SCH hierarchy AND, when a ddf_path is given, against every refdes
    already present in the DDF -- including board-only items (e.g.
    mechanical mounting hardware) that have no schematic representation at
    all and so would otherwise be invisible to the SCH-only check.

    If no offending refdes are found anywhere in the hierarchy, returns
    immediately with result.applied == False and empty change/file lists
    -- callers should treat that as "nothing to do", not an error.
    """
    result = ReannotateResult()
    root_sch_path = Path(root_sch_path)
    folder = root_sch_path.parent
    sheets = _load_hierarchy(root_sch_path)

    offending = detect_offending_refdes(sheets)
    result.offending = offending
    if not offending:
        return result

    # Read the DDF early (before computing new names) so its own refdes
    # namespace -- which can include board-only items with no schematic
    # representation at all, e.g. mechanical mounting spacers -- is part
    # of the collision check from the start, not just the SCH model's.
    ddf_path_obj, ddf_text = _read_ddf(ddf_path, result)

    all_refdes = _all_refdes(sheets)
    if ddf_text is not None:
        all_refdes |= _ddf_refdes(ddf_text)

    changes, placed = _plan_sch_renames(offending, all_refdes)
    result.changes = changes
    corrected = CorrectedFolder(locate(root_sch_path), tool)
    detail = _detail(changes)

    for stem, sh in sheets.items():
        src_path = folder / sh.name   # Sheet.name is always a bare filename
        if not src_path.exists():
            result.errors.append(f'Could not locate source file for sheet {stem}; skipped')
            continue

        # Bytes in, bytes out: cp437 maps every byte, and decoding the bytes
        # (rather than reading text) keeps the CRLF line endings as they are.
        original_text = src_path.read_bytes().decode('cp437')
        sch_part, nrm_part = _split_at_nrm_marker(original_text)
        for old, new in placed:
            sch_part = _rewrite_refdes_in_sch_text(sch_part, old, new)
        text = sch_part + nrm_part
        changed = text != original_text
        out_path = corrected.write(locate(src_path).original, text.encode('cp437'),
                                   CORRECTION if changed else None, detail if changed else '')
        result.sch_files_written.append(out_path)

    _write_ddf_copy(result, ddf_path_obj, ddf_text, changes, corrected)
    result.applied = True
    return result


def _detail(changes: List[RefdesChange]) -> str:
    """The changes in one line, for the corrected folder's manifest log."""
    return ', '.join(f'{ch.old} -> {ch.new}' for ch in changes)


def _read_ddf(ddf_path: Optional[str], result: ReannotateResult) -> Tuple[Optional[Path], Optional[str]]:
    """The DDF's path and text, or (path, None) with an error noted when it does not exist."""
    if not ddf_path:
        return None, None
    path = Path(ddf_path)
    if not path.exists():
        result.errors.append(f'DDF path does not exist: {path}')
        return path, None
    return path, path.read_bytes().decode('cp437')      # line endings kept as they are


def _write_ddf_copy(result: ReannotateResult, ddf_path: Optional[Path], ddf_text: Optional[str],
                    changes: List[RefdesChange], corrected: CorrectedFolder) -> None:
    """Write the DDF with every change applied as its copy in the design's
    corrected folder, under its own name; note any refdes it does not hold.
    `ddf_path` may be the DDF's corrected copy already (KIUB's V4.60)."""
    if ddf_path is None or ddf_text is None:
        return
    for ch in changes:
        ddf_text, found = ddf.rename_component(ddf_text, ch.old, ch.new)
        if not found:
            result.ddf_refdes_not_found.append(ch.old)
    result.ddf_file_written = corrected.write(locate(ddf_path).original, ddf_text.encode('cp437'),
                                              CORRECTION, _detail(changes))


# ── .UTSCH entry point ───────────────────────────────────────────────────────
#
# A .UTSCH stores a designator twice -- as its symbol's text and as its key in
# the document's designator map -- and nowhere else, without the unit letter
# of a multi-unit part (stored "U5", placed "U5A".."U5F"). kiuc_binary_write
# replaces those two strings in a copy of the file; nothing else changes.
#
# One kind of offending designator cannot be fixed: Ulticap's import writes a
# placeholder "!n!" for a gate whose place in its package was already taken,
# and the original designator is not stored anywhere. Those are listed, not
# renamed -- any name chosen here would be a guess.

def _stored_designator(refdes: str, designator_map) -> Optional[str]:
    """The designator a .UTSCH stores for a placed refdes: the refdes itself, or, for a unit of a
    multi-unit part, the refdes without its unit letter. None when neither is stored."""
    if refdes in designator_map:
        return refdes
    base = refdes[:-1]
    symbol = designator_map.get(base)
    if symbol is not None and len(symbol['parts']) > 1 and \
            refdes[-1:] in {p['text']['string'] for p in symbol['parts'] if p is not None}:
        return base
    return None


def _next_free_designator(old: str, units: List[str], taken: Set[str]) -> str:
    """old + the lowest integer from 1 such that neither the new designator nor any of its placed
    forms (with a unit letter) is taken; `taken` holds upper-case names, as Ulticap compares them."""
    n = 1
    while f'{old}{n}'.upper() in taken or any(f'{old}{n}{u}'.upper() in taken for u in units):
        n += 1
    return f'{old}{n}'


def _plan_utsch_renames(offending: List[OffendingRef], designator_map, taken: Set[str]
                        ) -> Tuple[List[RefdesChange], List[Tuple[str, str]], List[str]]:
    """The renames for a .UTSCH design: (changes, placed, placeholders).

    changes rename stored designators (one per part, as the DDF names it);
    placed are the placed refdes before and after (with unit letters);
    placeholders are offending refdes left alone (no real designator stored).
    taken holds upper-case names and is updated with every name given out.
    """
    changes: List[RefdesChange] = []
    placed: List[Tuple[str, str]] = []
    placeholders: List[str] = []
    renames: Dict[str, str] = {}
    for o in offending:
        old = _stored_designator(o.refdes, designator_map)
        if old is None or '!' in old:
            if o.refdes not in placeholders:
                placeholders.append(o.refdes)
            continue
        if old not in renames:
            units = [p['text']['string'] for p in designator_map[old]['parts'] if p is not None] \
                if len(designator_map[old]['parts']) > 1 else []
            new = _next_free_designator(old, units, taken)
            taken.add(new.upper())
            taken.update(f'{new}{u}'.upper() for u in units)
            renames[old] = new
            changes.append(RefdesChange(old=old, new=new))
        placed.append((o.refdes, renames[old] + o.refdes[len(old):]))
    return changes, placed, placeholders


def plan_new_names(path, ddf_path: Optional[str] = None
                   ) -> Tuple[List[OffendingRef], Dict[str, str]]:
    """Detection plus a preview, without writing anything: (offending, new), where new maps
    each offending placed refdes to the refdes it will have after reannotate_hierarchy() or
    reannotate_utsch() with the same DDF -- '' for one that is left alone (a .UTSCH
    placeholder). `path` is a root .SCH or a .UTSCH file."""
    from kiut.schematic.kiuc_binary import looks_like_utsch
    path = Path(path)
    sink = ReannotateResult()                    # _read_ddf notes a missing DDF here; unused
    _ddf, ddf_text = _read_ddf(ddf_path, sink)
    ddf_refdes = _ddf_refdes(ddf_text) if ddf_text is not None else set()
    if looks_like_utsch(path):
        from kiut.schematic import kiuc_binary as kb
        from kiut.schematic import kiuc_binary_semantic as ks
        from kiut.schematic import kiuc_binary_walk as kw
        schematic, _warnings = ks.read_schematic_file(path)
        sheets = {Path(sh.name).stem.upper(): sh for sh in schematic.sheets}
        offending = detect_offending_refdes(sheets)
        if not offending:
            return offending, {}
        designator_map = kw.read_document(kb.read_container(path).stream).designator_map
        taken = {k.upper() for k in designator_map} | {r.upper() for r in _all_refdes(sheets)} \
            | {r.upper() for r in ddf_refdes}
        _changes, placed, _placeholders = _plan_utsch_renames(offending, designator_map, taken)
    else:
        sheets = _load_hierarchy(path)
        offending = detect_offending_refdes(sheets)
        _changes, placed = _plan_sch_renames(offending, _all_refdes(sheets) | ddf_refdes)
    new = dict(placed)
    return offending, {o.refdes: new.get(o.refdes, '') for o in offending}


def reannotate_utsch(utsch_path, ddf_path: Optional[str] = None,
                     tool: str = 'KIUC') -> ReannotateResult:
    """reannotate_hierarchy() for a .UTSCH design: detect offending refdes and, if any can be
    renamed, write the .UTSCH (and the DDF, if given) with the new designators into the design's
    corrected folder, under their own names. utsch_path is the design's working file (its
    corrected copy when there is one). The original files are never modified; the corrected
    copy keeps the original's save time.

    New designators follow the same rule as the .SCH route (old + the lowest free integer),
    checked against every designator of the design and of the DDF, ignoring case. Placeholder
    designators ("!n!") are reported in result.placeholders and left alone. result.applied is
    True only when a corrected copy was written.
    """
    # Imported here so the .SCH route does not load the binary reader.
    from kiut.schematic import kiuc_binary as kb
    from kiut.schematic import kiuc_binary_semantic as ks
    from kiut.schematic import kiuc_binary_walk as kw
    from kiut.schematic import kiuc_binary_write as bw

    result = ReannotateResult()
    utsch_path = Path(utsch_path)
    schematic, _warnings = ks.read_schematic_file(utsch_path)
    sheets = {Path(sh.name).stem.upper(): sh for sh in schematic.sheets}
    result.offending = detect_offending_refdes(sheets)
    if not result.offending:
        return result

    data = utsch_path.read_bytes()
    stream = kb.read_container(utsch_path).stream
    designator_map = kw.read_document(stream).designator_map

    ddf_path_obj, ddf_text = _read_ddf(ddf_path, result)
    taken = {k.upper() for k in designator_map} | {r.upper() for r in _all_refdes(sheets)}
    if ddf_text is not None:
        taken |= {r.upper() for r in _ddf_refdes(ddf_text)}

    result.changes, _placed, result.placeholders = _plan_utsch_renames(result.offending, designator_map, taken)
    renames = {ch.old: ch.new for ch in result.changes}
    if not renames:
        return result

    try:
        fixed = bw.build_container(bw.rename_designators(stream, renames),
                                   bw.root_modified_time(data))
    except kb.UtschError as exc:
        result.errors.append(f'Could not write the corrected design: {exc}')
        return result
    corrected = CorrectedFolder(locate(utsch_path), tool)
    result.sch_files_written.append(
        corrected.write(locate(utsch_path).original, fixed, CORRECTION, _detail(result.changes)))
    _write_ddf_copy(result, ddf_path_obj, ddf_text, result.changes, corrected)
    result.applied = True
    return result


# ── log file ─────────────────────────────────────────────────────────────────

def write_log(result: ReannotateResult, log_path,
             root_sch_name: str = '', not_applied_reason: str = '') -> None:
    """Write log_text() to log_path -- the record of a fix declined during a
    conversion, next to its output. An applied fix's log goes into the
    corrected folder instead (write_corrected_log)."""
    Path(log_path).write_text(log_text(result, root_sch_name, not_applied_reason), encoding='utf-8')


def write_corrected_log(result: ReannotateResult, root_path, tool: str = 'KIUC',
                        root_sch_name: str = '') -> Path:
    """Write an applied fix's log into the design's corrected folder, beside
    the copies it describes, as LOG_NAME (listed in the folder's manifest, so a
    redo clears it with them). Returns its path."""
    corrected = CorrectedFolder(locate(root_path), tool)
    return corrected.write_log_file(LOG_NAME, log_text(result, root_sch_name))


def log_text(result: ReannotateResult, root_sch_name: str = '',
             not_applied_reason: str = '') -> str:
    """The durable record of what was found and what (if anything) was done
    about it: documentation of an applied change and, when the user
    declines, the record that the issue was flagged.

    not_applied_reason replaces the default explanation of why nothing was
    applied, for callers where the default is simply not true -- a .UTSCH
    design, where no fix was ever on offer, was not "declined".
    """
    lines: List[str] = []
    lines.append('Reference-designator reannotation log')
    lines.append(f'Generated: {datetime.datetime.now().isoformat(timespec="seconds")}')
    if root_sch_name:
        lines.append(f'Source design: {root_sch_name}')
    lines.append('')

    if not result.offending:
        lines.append('No non-digit-ending reference designators found.')
    else:
        lines.append(f'{len(result.offending)} offending component instance(s) found:')
        for o in result.offending:
            lines.append(f'  {o.refdes}  (symbol {o.symbol_name}, sheet {o.sheet_name})')
        lines.append('')

        if result.applied:
            lines.append('Reannotation APPLIED. Corrected copies were written into the design\'s')
            lines.append('corrected folder, under their original names; originals untouched.')
            lines.append('')
            lines.append('Refdes changes:')
            for ch in result.changes:
                lines.append(f'  {ch.old}  ->  {ch.new}')
            lines.append('')
            lines.append('Files written:')
            for p in result.sch_files_written:
                lines.append(f'  {p}')
            if result.ddf_file_written:
                lines.append(f'  {result.ddf_file_written}')
                lines.append('')
                lines.append('A fresh KIUB conversion of the new DDF file is required.')
            if result.ddf_refdes_not_found:
                lines.append('')
                lines.append('WARNING: the following refdes were NOT found in the DDF '
                             '(*C definition line) and were left unchanged there:')
                for rd in result.ddf_refdes_not_found:
                    lines.append(f'  {rd}')
        else:
            lines.append(f'Reannotation NOT applied ({not_applied_reason}).'
                         if not_applied_reason else
                         'Reannotation NOT applied (declined by user, or no DDF '
                         'available and the SCH-only fix was not selected).')
            lines.append('The original schematic will be converted unchanged. KiCad')
            lines.append('will flag these components as unannotated (ERC warning),')
            lines.append('and netlist generation will be blocked until annotated.')

    if result.placeholders:
        lines.append('')
        lines.append('Not renamed -- placeholder designators written by Ulticap when it read')
        lines.append('an ASCII design; the original designator is not stored, so rename these')
        lines.append('by hand (in Ulticap or in KiCad):')
        for rd in result.placeholders:
            lines.append(f'  {rd}')

    if result.errors:
        lines.append('')
        lines.append('Errors:')
        for e in result.errors:
            lines.append(f'  {e}')

    return '\n'.join(lines) + '\n'


# ── command-line interface ──────────────────────────────────────────────────
