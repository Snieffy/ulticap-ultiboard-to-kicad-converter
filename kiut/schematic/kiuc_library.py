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
kiuc_library.py -- Converting Ulticap symbol libraries to KiCad

One library -- a Windows .UTLIB (kiuc_utlib) or a DOS .LIB (kiuc_lib), told
apart by their signatures, not their extensions -- becomes one KiCad symbol
library (kiuc_writer.write_symbol_library) with a log of its own. Used by
the command line and the KIUT window alike.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from kiut.schematic import kiuc_lib
from kiut.schematic import kiuc_utlib
from kiut.schematic import kiuc_writer
from kiut.schematic.kiuc_binary import UtschError

# The extensions offered for libraries; the signature decides the format.
LIBRARY_EXTENSIONS = ('.UTLIB', '.LIB')


def is_library_path(path) -> bool:
    """Whether a path names a library by its extension."""
    return Path(path).suffix.upper() in LIBRARY_EXTENSIONS


def looks_like_library(path) -> bool:
    """Whether the file starts with the signature of either library format."""
    return kiuc_utlib.looks_like_utlib(path) or kiuc_lib.looks_like_lib(path)


def library_output_files(path, out_dir) -> List[Path]:
    """The files converting a library writes: <name>.kicad_sym and its log."""
    stem = Path(path).stem
    return [Path(out_dir) / f'{stem}.kicad_sym', Path(out_dir) / f'{stem}.log']


@dataclass
class LibraryResult:
    """One library's conversion: whether it succeeded, the KiCad library
    written, and its report as (text, level) lines -- level 'info', 'ok',
    'warn' or 'err' -- which are also its log file. `renamed` lists the
    (library name, KiCad name) pairs that changed; the log lists each one,
    the report only counts them. `clashes` are names taken by another
    symbol of the library (a non-homogeneous device joined into one symbol
    beside a symbol of the device's own name), each reported."""
    path: Path
    ok: bool = False
    output: Optional[Path] = None
    log: Optional[Path] = None
    report: List[Tuple[str, str]] = field(default_factory=list)
    renamed: List[Tuple[str, str]] = field(default_factory=list)
    clashes: List[Tuple[str, str]] = field(default_factory=list)


def _kicad_safe(name: str) -> str:
    return name.replace('\\', '_').replace('/', '_').replace(':', '_')


def read_library(path):
    """Read a library of either format (kiuc_model.Library)."""
    if kiuc_utlib.looks_like_utlib(path):
        return kiuc_utlib.read_library(path)
    if kiuc_lib.looks_like_lib(path):
        return kiuc_lib.read_library(path)
    if kiuc_lib.looks_like_ultiboard_lib(path):
        raise kiuc_lib.LibError(kiuc_lib.ULTIBOARD_MESSAGE)
    raise kiuc_lib.LibError('not an Ulticap library (neither a .UTLIB nor a DOS .LIB signature) '
                            '-- wrong file, or not Ulticap data at all')


def convert_library(path, out_dir, preamble: Tuple[Tuple[str, str], ...] = ()) -> LibraryResult:
    """Convert one library into <out_dir>/<name>.kicad_sym and write its
    log, <out_dir>/<name>.log -- also when the library cannot be read, so
    every library of a batch leaves its own record. Never raises for a bad
    library: the error is in the result. `preamble` lines (the text font)
    open the log."""
    path, out_dir = Path(path), Path(out_dir)
    sym_path, log_path = library_output_files(path, out_dir)
    res = LibraryResult(path=path, log=log_path)
    rep = res.report
    rep.append((f'Library: {path}', 'info'))
    try:
        lib = read_library(path)
        out_dir.mkdir(parents=True, exist_ok=True)
        names = kiuc_writer.write_symbol_library(lib.symbols, sym_path, lib.is_less_than_v500)
    except (kiuc_utlib.UtlibError, kiuc_lib.LibError, UtschError, OSError) as e:
        rep.append((f'ERROR: {e}. Nothing written.', 'err'))
    else:
        res.ok, res.output = True, sym_path
        rep.append((f'Symbols: {lib.count} read, {len(names)} written', 'info'))
        for w in lib.warnings:
            rep.append((f'WARNING: {w}', 'warn'))
        res.renamed = [(a, b) for a, b in names if a != b and b == _kicad_safe(a)]
        res.clashes = [(a, b) for a, b in names if a != b and b != _kicad_safe(a)]
        if res.renamed:
            rep.append((f'Names changed for KiCad (\\ / : become _): {len(res.renamed)}, '
                        f'listed in {log_path.name}', 'info'))
        for a, b in res.clashes:
            rep.append((f'Written as {b}: {a} (its name is taken by another symbol of the '
                        f'library)', 'info'))
        rep.append((f'Written: {sym_path}', 'ok'))
        # Listed in the folder's sym-lib-table under its own name, as a
        # design's project library is (and KIUB's footprint libraries in
        # fp-lib-table): KiCad finds it when a project in this folder is
        # opened, and the table travels with the libraries if the folder is
        # copied into a project.
        try:
            rep.append((kiuc_writer.add_to_sym_lib_table(out_dir, sym_path.stem, path), 'info'))
        except OSError as e:
            rep.append((f'WARNING: {kiuc_writer.SYM_LIB_TABLE} not written: {e}', 'warn'))
    lines = [t for t, _ in preamble] + [t for t, _ in rep]
    if res.renamed:
        at = next(i for i, (t, _) in enumerate(rep) if t.startswith('Names changed')) + len(preamble)
        lines[at + 1:at + 1] = [f'  {a} -> {b}' for a, b in res.renamed]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    except OSError as e:
        rep.append((f'WARNING: log not written: {e}', 'warn'))
        res.log = None
    return res


__all__ = ['LIBRARY_EXTENSIONS', 'LibraryResult', 'convert_library', 'is_library_path',
           'library_output_files', 'looks_like_library', 'read_library']
