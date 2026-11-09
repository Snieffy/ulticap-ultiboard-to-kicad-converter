# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/corrected.py -- A design's corrected source files, <name>_corrected

Shared by KIUC (kiut.schematic), Refdes Reannotate and KIUB (kiut.board):
until KIUT stage 4 the two tools had a copy each, kiuc_corrected.py and
kiub_corrected.py, differing only in their default tool name and the name of
the write-test file. Every caller now names its tool.

The original files of a design are never written. Every correction KIUC,
Refdes Reannotate or KIUB makes to a design's source files goes into one
folder next to them, <name>_corrected, where each file keeps its original
name: the folder is a complete design Ulticap and Ultiboard open as it is,
and copying its files over the originals adopts the corrections without any
renaming. Next to it are the KiCad project folder <name>_KiCad and, for
library-only conversions, <name>_lib_KiCad. CORRECTED_FOLDER.md describes
this layout and the rules below for every tool that writes to the folder.

Every tool reads each file from the folder when it is there, else the
original (the working set, CorrectedFolder.working), and writes its
corrections into the folder, so the tools build on each other whatever
order they run in. The folder's manifest, _corrected.json, records each
original as it was when it was first copied and each copy as a tool last
wrote it; from those the checks find:

  - an original changed since its copy was made or gone from disk (every
    recorded original, so a tool that doesn't read a hierarchy, like KIUB,
    sees a changed sheet too), or a sheet new to a hierarchy whose sheets
    are copied: the copies are out of date;
  - a copy changed since a tool wrote it: edited by hand, never
    overwritten unasked.

The questions those findings lead to are the callers' (CLI and GUI): this
module only finds and reports. Read-only originals are refused (is_writable):
nothing can be written next to them.

No other KIUT module needed; ask_console is the console form of the
questions, shared by the command-line tools.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# Folder names, shared with KIUB; compared ignoring letter case, as Windows does.
CORRECTED_SUFFIX = '_corrected'
KICAD_FOLDER_SUFFIX = '_KiCad'
LIBRARY_FOLDER_SUFFIX = '_lib_KiCad'
MANIFEST_NAME = '_corrected.json'   # the underscore sorts it first, apart from design files
MANIFEST_FORMAT = 1


# ── where a design lives ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Location:
    """Where an input file's design lives: `home` is the folder of the
    originals, `name` the design name, `original` the input's original
    (the input itself, unless it lies in the design's corrected folder)."""
    home: Path
    name: str
    original: Path

    @property
    def corrected_dir(self) -> Path:
        return self.home / f'{self.name}{CORRECTED_SUFFIX}'

    @property
    def kicad_dir(self) -> Path:
        return self.home / f'{self.name}{KICAD_FOLDER_SUFFIX}'

    @property
    def lib_kicad_dir(self) -> Path:
        return self.home / f'{self.name}{LIBRARY_FOLDER_SUFFIX}'


def _corrected_folder_name(folder: Path) -> Optional[str]:
    """The design name a folder is the corrected folder of, or None."""
    n = folder.name
    if n.lower().endswith(CORRECTED_SUFFIX.lower()) and len(n) > len(CORRECTED_SUFFIX):
        return n[:-len(CORRECTED_SUFFIX)]
    return None


def locate(input_path) -> Location:
    """Where the design of `input_path` lives. A file inside <name>_corrected
    belongs to the design <name> whose originals are in the parent folder
    (so its output goes to the parent's <name>_KiCad, never into the
    corrected folder); any other file is its own design, named after it."""
    p = Path(input_path)
    folder = p.parent
    name = _corrected_folder_name(folder)
    if name is not None:
        home = folder.parent
        return Location(home=home, name=name, original=home / p.name)
    return Location(home=folder, name=p.stem, original=p)


def is_writable(folder) -> bool:
    """Whether files can be written in `folder` (the originals' folder): a
    test file is created and removed, as permissions alone don't tell
    (read-only media, network shares)."""
    folder = Path(folder)
    probe = folder / f'.kiut_write_test_{os.getpid()}'
    try:
        with open(probe, 'xb'):
            pass
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


READ_ONLY_MESSAGE = ('The folder of the design\'s files cannot be written to:\n{folder}\n\n'
                     'Copy the design to a folder you can write to, then convert it there.')


# ── the manifest ─────────────────────────────────────────────────────────────

def fingerprint(path) -> Dict[str, object]:
    """A file's size and SHA-256, as the manifest records them."""
    data = Path(path).read_bytes()
    return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


@dataclass
class Manifest:
    """_corrected.json: `originals` maps each original (path relative to the
    originals' folder) to its fingerprint when it was first copied; `files`
    maps each copy (its file name) to {'sha256', 'corrections'} as a tool last
    wrote it; `log` lists every correction written, oldest first; `logs`
    names the tools' log files in the folder (cleared with the copies)."""
    design: str
    originals: Dict[str, Dict[str, object]] = field(default_factory=dict)
    files: Dict[str, Dict[str, object]] = field(default_factory=dict)
    log: List[Dict[str, str]] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({'format': MANIFEST_FORMAT, 'design': self.design,
                           'originals': self.originals, 'files': self.files,
                           'log': self.log, 'logs': self.logs},
                          indent=2, ensure_ascii=False) + '\n'

    @classmethod
    def from_json(cls, text: str) -> 'Manifest':
        """Raises ValueError for a manifest this version cannot read."""
        try:
            d = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f'not a valid manifest: {exc}') from exc
        if not isinstance(d, dict) or d.get('format') != MANIFEST_FORMAT:
            raise ValueError(f'unknown manifest format: {d.get("format") if isinstance(d, dict) else d!r}')
        return cls(design=str(d.get('design', '')), originals=dict(d.get('originals', {})),
                   files=dict(d.get('files', {})), log=list(d.get('log', [])),
                   logs=list(d.get('logs', [])))


def _atomic_write(path: Path, data: bytes) -> None:
    """Write to a temporary name in the same folder, then rename: a reader
    never sees a half-written file (another tool, or a crash midway)."""
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ── the folder ───────────────────────────────────────────────────────────────

@dataclass
class Status:
    """What check() found. `state` is 'absent' (no folder, or one without a
    manifest that holds none of the design's files), 'unreadable' (copies
    without a manifest this version can read), 'stale' or 'current';
    `edited` lists copies changed since a tool wrote them, also when the
    copies are otherwise current."""
    state: str
    changed: List[str] = field(default_factory=list)   # originals whose content changed
    added: List[str] = field(default_factory=list)     # originals new to the design
    removed: List[str] = field(default_factory=list)   # recorded originals gone from disk
    edited: List[str] = field(default_factory=list)    # copies changed by hand (or missing)
    reason: str = ''                                   # for 'unreadable'

    @property
    def stale(self) -> bool:
        return self.state in ('stale', 'unreadable')


class CorrectedFolder:
    """A design's <name>_corrected folder and its manifest. Nothing is written
    until a copy is: a design without corrections has no folder at all."""

    def __init__(self, location: Location, tool: str):
        self.location = location
        self.tool = tool
        self._manifest: Optional[Manifest] = None

    @property
    def path(self) -> Path:
        return self.location.corrected_dir

    @property
    def manifest_path(self) -> Path:
        return self.path / MANIFEST_NAME

    def exists(self) -> bool:
        return self.path.is_dir()

    def _key(self, original: Path) -> str:
        """An original's manifest key: its path relative to the originals'
        folder, '/'-separated (a sub-sheet elsewhere keeps its '..')."""
        return Path(os.path.relpath(Path(original), self.location.home)).as_posix()

    def copy_path(self, original) -> Path:
        """Where an original's copy lives: the folder, under the original's
        own file name (all of a hierarchy's sheets side by side, so Ulticap
        resolves the FILE= references in the folder)."""
        return self.path / Path(original).name

    def working(self, original) -> Path:
        """The file to read for `original`: its copy when there is one."""
        copy = self.copy_path(original)
        return copy if copy.is_file() else Path(original)

    def manifest(self) -> Manifest:
        """The manifest as read (or a new, empty one). Raises ValueError for a
        manifest this version cannot read; check() reports that case."""
        if self._manifest is None:
            if self.manifest_path.is_file():
                self._manifest = Manifest.from_json(self.manifest_path.read_text(encoding='utf-8'))
            else:
                self._manifest = Manifest(design=self.location.name)
        return self._manifest

    def check(self, originals: Iterable, sheets: Iterable = ()) -> Status:
        """Compare the folder with the design's originals. `originals` are the
        design's files as the caller knows them (sheets, .UTSCH, DDF); `sheets`
        those of them that must all be in the folder once one is -- a
        hierarchy's sheets, so that it stays complete.

        Every original the manifest records is checked, also one the caller
        doesn't know (KIUB doesn't read a hierarchy): changed, or gone from
        disk, the copies are out of date. A sheet the manifest doesn't record
        while the folder holds the design's sheets is new to the design: out of
        date too. A file neither recorded nor copied (a DDF no correction
        touched) is simply read from its original."""
        originals = [Path(o) for o in originals]
        sheets = [Path(s) for s in sheets]
        if not self.exists():
            return Status('absent')
        if not self.manifest_path.is_file():
            # Without a manifest, only copies of the design's own files make
            # the folder unusable; anything else in it is the user's.
            if any(self.copy_path(o).is_file() for o in originals + sheets):
                return Status('unreadable', reason=f'{MANIFEST_NAME} is missing')
            return Status('absent')
        try:
            m = self.manifest()
        except (OSError, ValueError) as exc:
            return Status('unreadable', reason=str(exc))
        status = Status('current')
        home = self.location.home
        for key, recorded in m.originals.items():
            path = home / key
            if not path.is_file():
                status.removed.append(key)
            elif fingerprint(path) != recorded:
                status.changed.append(key)
        if any(self.copy_path(s).is_file() for s in sheets):
            status.added = [self._key(s) for s in sheets if self._key(s) not in m.originals]
        for name, rec in m.files.items():
            copy = self.path / name
            if not copy.is_file() or fingerprint(copy)['sha256'] != rec.get('sha256'):
                status.edited.append(name)
        if status.changed or status.added or status.removed:
            status.state = 'stale'
        return status

    def write(self, original, data: bytes, correction: Optional[str] = None,
              detail: str = '') -> Path:
        """Write `data` as `original`'s copy. `correction` names what changed it
        (None: copied unchanged, e.g. the other sheets of a hierarchy); a
        copy keeps every correction it ever had. The original is recorded the
        first time it is copied. Returns the copy's path."""
        original = Path(original)
        m = self.manifest()
        self.path.mkdir(parents=True, exist_ok=True)
        key = self._key(original)
        if key not in m.originals:
            m.originals[key] = fingerprint(original)
        copy = self.copy_path(original)
        _atomic_write(copy, data)
        rec = m.files.setdefault(copy.name, {'sha256': '', 'corrections': []})
        rec['sha256'] = hashlib.sha256(data).hexdigest()
        if correction and correction not in rec['corrections']:
            rec['corrections'].append(correction)
        if correction:
            m.log.append({'time': datetime.datetime.now().isoformat(timespec='seconds'),
                          'tool': self.tool, 'file': copy.name, 'correction': correction,
                          'detail': detail})
        self._save()
        return copy

    def ensure_copies(self, originals: Iterable) -> List[Path]:
        """Copy every original that has no copy yet, unchanged -- before a
        correction changes one sheet of a hierarchy, so the folder is a
        complete design. Returns the copies written."""
        written = []
        for o in originals:
            if not self.copy_path(o).is_file():
                written.append(self.write(o, Path(o).read_bytes()))
        return written

    def copies_for(self, files: Iterable, sheets: Iterable) -> List[Path]:
        """Make sure every one of the design's `sheets` (originals) has a copy
        in the folder -- the whole hierarchy, so it stays complete -- and return
        the copies of `files` (originals or copies already), in their order:
        the paths an in-place correction then edits, followed by record()."""
        self.ensure_copies(sheets)
        return [self.copy_path(locate(f).original) for f in files]

    def record(self, copy, correction: str, detail: str = '') -> bool:
        """Record that `correction` changed `copy` in place (a correction
        that edits a file itself, run on the copy). Nothing is recorded when
        the copy is as the manifest has it. Returns whether it changed."""
        copy = Path(copy)
        m = self.manifest()
        rec = m.files.setdefault(copy.name, {'sha256': '', 'corrections': []})
        sha = hashlib.sha256(copy.read_bytes()).hexdigest()
        if sha == rec['sha256']:
            return False
        rec['sha256'] = sha
        if correction not in rec['corrections']:
            rec['corrections'].append(correction)
        m.log.append({'time': datetime.datetime.now().isoformat(timespec='seconds'),
                      'tool': self.tool, 'file': copy.name, 'correction': correction,
                      'detail': detail})
        self._save()
        return True

    def reset(self, originals: Iterable = ()) -> None:
        """Empty the folder for a redo from the originals: every file the
        manifest lists, the copies of `originals` (the design's files; what is
        left of the copies when the manifest is missing or unreadable), and
        the manifest. Anything else in it is the user's and stays (the folder
        too, then)."""
        try:
            m = self.manifest()
            names = set(m.files) | set(m.logs)
        except (OSError, ValueError):
            names = set()
        names |= {self.copy_path(o).name for o in originals}
        for name in sorted(names):
            try:
                (self.path / name).unlink()
            except FileNotFoundError:
                pass
        try:
            self.manifest_path.unlink()
        except FileNotFoundError:
            pass
        self._manifest = None
        try:
            self.path.rmdir()               # only when nothing else is left in it
        except OSError:
            pass

    def write_log_file(self, name: str, text: str) -> Path:
        """Write a tool's log into the folder (e.g. the refdes fix's), listed in
        the manifest so a redo clears it with the copies. Returns its path."""
        m = self.manifest()
        self.path.mkdir(parents=True, exist_ok=True)
        path = self.path / name
        _atomic_write(path, text.encode('utf-8'))
        if name not in m.logs:
            m.logs.append(name)
        self._save()
        return path

    def _save(self) -> None:
        _atomic_write(self.manifest_path, self._manifest.to_json().encode('utf-8'))


# ── a conversion's preparation (shared by the CLI and the GUIs) ─────────────

def sibling_ddf(location: Location) -> Optional[Path]:
    """The design's original DDF, <name>.DDF next to its originals, if any."""
    for ext in ('.DDF', '.ddf'):
        p = location.home / f'{location.name}{ext}'
        if p.is_file():
            return p
    return None


@dataclass
class Preparation:
    """What a conversion of `inputs` (a design's sheets, or its .UTSCH) works
    from. `error` set: refuse with that message, nothing else is filled in."""
    location: Location
    folder: CorrectedFolder
    sheets: List[Path] = field(default_factory=list)      # the inputs' originals, in order
    originals: List[Path] = field(default_factory=list)   # the design's files: sheets, then its DDF
    status: Optional[Status] = None
    error: str = ''

    def working_sheets(self) -> List[Path]:
        """The files to convert: each sheet's corrected copy, else the original."""
        return [self.folder.working(s) for s in self.sheets]

    def working_ddf(self) -> Optional[Path]:
        """The DDF to use: its corrected copy (KIUB's V4.60, a refdes fix), else
        the original; None without a DDF."""
        ddf = sibling_ddf(self.location)
        return self.folder.working(ddf) if ddf else None

    def redo_question(self) -> Optional[tuple]:
        """(text, default) when the corrected copies are out of date, else None.
        The default is to redo from the originals (True) -- unless that would
        throw away copies edited by hand: then it is to cancel (False)."""
        st = self.status
        if st is None or not st.stale:
            return None
        text = (f'The corrected files in {self.folder.path.name} no longer match the design:\n\n'
                + '\n'.join(describe(st)) + '\n\n'
                'Redo the corrections from the original files? The corrected copies are '
                'removed first' + (', including the ones edited by hand.' if st.edited else '.'))
        return text, not st.edited


def prepare(inputs: Iterable, tool: str) -> Preparation:
    """Locate the design of `inputs` (the first is the root), refuse what can't
    be converted safely -- an input in a corrected folder whose original is
    gone (a redo would leave nothing), a folder of originals that can't be
    written -- and check its corrected folder against its originals."""
    inputs = [Path(i) for i in inputs]
    loc = locate(inputs[0])
    prep = Preparation(location=loc, folder=CorrectedFolder(loc, tool))
    sheets = [locate(i).original for i in inputs]
    missing = [s for s in sheets if not s.is_file()]
    if missing:
        prep.error = (f'The original of {missing[0].name} is not in\n{missing[0].parent}\n\n'
                      f'Convert the design from its original files: the corrected folder only '
                      f'holds copies of them.')
        return prep
    if not is_writable(loc.home):
        prep.error = READ_ONLY_MESSAGE.format(folder=loc.home)
        return prep
    prep.sheets = sheets
    ddf = sibling_ddf(loc)
    prep.originals = sheets + ([ddf] if ddf else [])
    prep.status = prep.folder.check(prep.originals, prep.sheets)
    return prep


def ask_console(question: str, default: bool, write=print) -> bool:
    """A yes/no question on the console, [Y/n] or [y/N] after the default;
    `write` prints (a tool's own output function). Without a terminal to
    answer it (a scheduled or piped run), the default is taken, and said."""
    import sys
    hint = '[Y/n]' if default else '[y/N]'
    if not sys.stdin or not sys.stdin.isatty():
        write(f'{question} {hint} -- no terminal to answer: {"yes" if default else "no"} (the default)')
        return default
    write(f'{question} {hint}')
    answer = input('> ').strip().lower()
    return default if not answer else answer in ('y', 'yes')


def describe(status: Status) -> List[str]:
    """Lines for a log or a question: what check() found."""
    lines = []
    if status.state == 'unreadable':
        lines.append(f'The corrected folder cannot be used: {status.reason}.')
    for k in status.changed:
        lines.append(f'Changed since its corrected copy was made: {k}')
    for k in status.added:
        lines.append(f'New in the design since the corrected copies were made: {k}')
    for k in status.removed:
        lines.append(f'Original no longer there: {k}')
    for k in status.edited:
        lines.append(f'Corrected copy changed since it was written (edited by hand?): {k}')
    return lines
