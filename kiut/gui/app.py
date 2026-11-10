# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/gui/app.py -- the KIUT window (kiut_gui.py)

One window for every input KIUT converts, as kiut.py takes them: a schematic (.SCH, .BLK, .UTSCH),
Ulticap libraries, a board (.DDF), Ultiboard libraries (.LIB, .L55); a schematic whose board lies
next to it (same name, same folder) is converted with it ("With its board"). The conversion is
kiut.py's (kiut.cli.convert: the same jobs, the same settings, the same output), run in a thread
of its own; its report goes to the log, and its questions come up as dialogs -- only those kiut.py
asks (files to replace, out-of-date corrected copies, a DDF that may be a library), plus the
record-integrity dialog and an Ultiboard library's technology, which need a choice no setting can
make. Everything else is decided by the settings, shown in the window.

Settings (docs/SETTINGS.md, kiut.core.kiut_settings): the design's own copy
(<name>_KiCad/<name>_kiut.ini), the current ones (kiut.ini, next to kiut.py: the last used) or the
built-in defaults -- the selector, Design / Current / Defaults; opening a design takes its copy
when it has one, else the current ones. A value changed by hand makes the set the current one
(kiut.ini written at once; the selector moves to Current); switching the selector alone writes
nothing. A design's first conversion writes its copy; after that only *Save current settings*
does. The main window holds the settings used most; *Advanced…* the rest. On exit, the question whether to save the settings for the design comes
only when a conversion ran since and the values differ from the design's copy.

The Schematic section and menu hold what applies to a schematic only: Open in Viewer
(kiut_viewer.py, its own process), Compare netlists (the converted schematic's netlist, made by
KiCad's kicad-cli, against Ulticap's <name>.NET next to the design: Netlist Compare's report), Fix
designators only (kiut.py --fix-only, after a confirmation), the review of the pin labels a
conversion corrected, and KIUC's two dumps. File: Open, Import old settings (kiuc.ini, kiub_gui.ini;
offered once at the first start when they lie next to kiut.py), Save log, KiCad program (the KiCad
Open in KiCad starts, or the system's choice), Library output folder (KIUT's library folder,
kiut.core.folders: where library-only runs go); Help. Browse… chooses the files, Add… adds more,
from any folder.

kiut.ini also keeps the window's own: [window] (KiCad's path, Show tooltips, the last folder, the
window's place and size), [font_ratios] (the board's text height/width ratios per font, as KIUB's
window kept them), [technology] (the last Ultiboard library technology chosen) and [folders]
(libraries: KIUT's library folder, when not the default; kiut.py reads it too).
"""
from __future__ import annotations

import contextlib
import copy as _copy
import io
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional

from kiut import APP_DIR, cli
from kiut.core import folders, kiut_settings as kset
from kiut.gui import common, dialogs

TITLE = 'KIUT — Ulticap / Ultiboard to KiCad'
KICAD_FONT = kset.KICAD_FONT
PINNED = {'schematic': ('Ulticap Stroke', 'DejaVu Sans Mono'),
          'board': ('Ultiboard Stroke', 'DejaVu Sans Mono')}
# The board's text ratios per font, for a kiut.ini without any yet (KIUB's window's own seed):
# KiCad Font and DejaVu Sans Mono with the settings' defaults, Ultiboard Stroke made to match
# Ultiboard's own font 1:1.
SEED_RATIOS = {'KiCad Font': (1.208, 1.186), 'DejaVu Sans Mono': (1.208, 1.186),
               'Ultiboard Stroke': (1.0, 1.4)}
SCHEMATIC_SWITCHES = (('fix_designators', 'Rename designators KiCad won’t accept'),
                      ('kicad_colors', 'KiCad colours'),
                      ('v5_header', 'SCH V5.x header'),
                      ('allow_missing_sheets', 'Allow missing sub-sheets'),
                      ('block_library', 'Block library (.kicad_blocks)'))
SOURCE_NAMES = {'design': "the design's own", 'kiut': 'current', 'default': 'built-in defaults'}
TECH_KEYS = ('single', 'top', 'bottom', 'inner', 'drills', 'traces')
VIEWER = 'kiut_viewer.py'
OPEN_RECENT, EMPTY_RECENT = 'Open Recent', 'Empty Recent Files List'   # File menu (as the viewer's)
MAX_RECENT = 10
RENAME_ONLY = 'Rename designators without converting…'   # the Schematic menu's (Fix designators only… until 10d)
KICAD_PROGRAM = 'KiCad program…'                         # the File menu's (stage 10e)
LIBRARY_FOLDER = 'Library output folder…'
OLD_SETTINGS = ('kiuc.ini', 'kiub_gui.ini')         # KIUC's and KIUB's windows' own files

LOG_BG, LOG_FG = '#1e1e1e', '#d4d4d4'
TAGS = {'info': '#9cdcfe', 'ok': '#4ec9b0', 'warn': '#dcdcaa', 'err': '#f44747', 'plain': LOG_FG}
POLL_MS = 50


def line_tag(line: str) -> str:
    """The log colour of a report line, by its prefix."""
    s = line.lstrip()
    if s.startswith(('ERROR', 'Traceback')):
        return 'err'
    if s.startswith('WARNING') or s == cli.CANCELLED:
        return 'warn'
    if s.startswith('NOTE'):
        return 'info'
    if s.startswith(('Written', 'Settings saved', 'Conversion done', 'Done')):
        return 'ok'
    return 'plain'


class _QueueWriter(io.TextIOBase):
    """stdout and stderr of the conversion thread: whole lines to the
    window's queue."""

    def __init__(self, q: queue.Queue) -> None:
        self._q = q
        self._part = ''

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self._part += s
        *lines, self._part = self._part.split('\n')
        for line in lines:
            self._q.put(('line', line))
        return len(s)

    def flush(self) -> None:
        if self._part:
            self._q.put(('line', self._part))
            self._part = ''


# ── the questions, as dialogs ────────────────────────────────────────────────

class _WindowAsking:
    """kiut.cli.convert's `asking`: the command line's questions, asked in
    the window. Settings decide what they decide (as on the command line);
    a yes/no question becomes a dialog, files to replace the overwrite
    dialog, out-of-date corrected copies KIUC's redo dialog, merged records
    KIUC's record-integrity dialog."""

    def __init__(self, app: 'App') -> None:
        self.app = app

    def schematic(self, chosen, fix_only: bool):
        Questions, _ = cli._schematic_classes()

        class WindowSchematicQuestions(_DialogMixin, Questions):
            def record_integrity(self, issues_by_file: dict):
                def show():
                    dlg = dialogs._RecordIntegrityDialog(self.app, {f: v for f, v in issues_by_file.items() if v})
                    self.app.wait_window(dlg)
                    return dlg
                dlg = self.app.on_main(show)
                if dlg.choice == 'cancel':
                    print(cli.CANCELLED)
                    return None
                for iss, (a, b) in dlg.resolutions.items():
                    print(f'Record integrity: {iss.symbol_name!r} ({iss.section})  {iss.merged_token!r} '
                          f'-> {a!r} | {b!r}')
                return list(dlg.resolutions.items())

        return WindowSchematicQuestions(self.app, False, False, chosen, fix_only)

    def board(self, library_only: str, together: bool):
        Questions = cli._board_classes()

        class WindowBoardQuestions(_DialogMixin, Questions):
            def legacy_v3(self, text: str) -> bool:
                # said in the window's message block when the DDF was chosen: Convert answers it
                print(f'WARNING: {text}')
                print('Converted as a design of its own (Convert, after the warning shown).')
                return True

        return WindowBoardQuestions(self.app, False, False, library_only, together)

    def several_libraries(self, text: str) -> bool:
        # said in the window's message block when the libraries were chosen: Convert answers it
        print(f'WARNING: {text}')
        return True

    def library_ddfs(self, text: str) -> bool:
        # the same: library DDFs that may be designs, said in the message block (stage 10f)
        print(f'WARNING: {text}')
        print('Converted as libraries (Convert, after the warning shown).')
        return True


class _DialogMixin:
    """The questions cli._Asking asks on the console, as dialogs: the lines
    already reported (the question's reasons) are in the log and repeated
    in the dialog; the answer is logged."""

    def __init__(self, app: 'App', *args) -> None:
        self.app = app
        super().__init__(*args)

    def _ask(self, question: str, default: bool, go_on: bool = True) -> bool:
        def show():
            # the reasons: the lines reported before the question -- read here, in the window's
            # thread, once the queue has brought them (in the conversion's thread they may not be)
            return messagebox.askyesno(
                'KIUT', question, detail=self.app.recent_lines(), icon=messagebox.WARNING,
                default=messagebox.YES if default else messagebox.NO, parent=self.app)
        answer = bool(self.app.on_main(show))
        print(f'{question} -- {"yes" if answer else "no"}')
        return answer

    def redo(self, text: str, default: bool) -> bool:
        for line in text.splitlines():
            print(f'WARNING: {line}' if line else '')

        def show():
            dlg = dialogs._RedoCorrectedDialog(self.app, text, default)
            self.app.wait_window(dlg)
            return dlg.choice == 'redo'
        answer = bool(self.app.on_main(show))
        print(f'Redo from the original files? -- {"yes" if answer else "no"}')
        return answer

    def replace(self, existing) -> bool:
        existing = [Path(p) for p in existing]
        print(f'WARNING: {len(existing)} KiCad file(s) already exist in the output folder and will be '
              f'replaced; changes made to them in KiCad are lost:')
        for p in existing:
            print(f'  {p}')
        block = any(p.suffix == '.kicad_block' or p.parent.suffix == '.kicad_blocks' for p in existing)
        library = any(p.parent.name.endswith('_lib_KiCad') or p.suffix == '.kicad_sym'
                      and p.parent.name == folders.SYMBOLS for p in existing)   # KIUT's Symbols (10e)
        what = 'KiCad block' if block else 'KiCad library' if library else 'KiCad'

        def show():
            dlg = common.OverwriteDialog(self.app, existing, what)
            self.app.wait_window(dlg)
            return dlg.choice == 'continue'
        answer = bool(self.app.on_main(show))
        print(f'Replace them? -- {"yes" if answer else "no"}')
        return answer


# ── the window ───────────────────────────────────────────────────────────────

class App(tk.Tk):
    """The KIUT window."""

    def __init__(self, settings_file: Optional[Path] = None) -> None:
        super().__init__()
        self.title(TITLE)
        self.settings_file = Path(settings_file) if settings_file else kset.SETTINGS_FILE
        folders.INI = self.settings_file                # the library folder: this window's kiut.ini
        self.values = kset.defaults()
        self.source = 'default'
        self.inputs: Optional[cli.Inputs] = None      # the File(s) field's inputs, its board aside
        self.board_ddf: Optional[Path] = None         # the schematic's board, found next to it
        self.design_file: Optional[Path] = None       # <out>/<name>_kiut.ini
        self.design_name = ''
        self.last_project: Optional[Path] = None
        self.review: Optional[tuple] = None           # (components, corrections): pin labels corrected
        self.converted_since_save = False
        self.running = False
        self._loading = False
        self._queue: queue.Queue = queue.Queue()
        self._recent: List[str] = []
        self._run_lines: List[str] = []
        self._fonts: Optional[List[str]] = None
        self._mono: Dict[str, bool] = {}
        self._handoff = None                          # the KIUT window's channel (kiut/core/handoff.py)
        self.handoff_name: Optional[str] = None
        self.own_viewer: Optional[str] = None         # KIUT's own viewer: its channel id
        self._read_window_settings()
        self._build()
        self._load_source(None, say=False)
        self._check_fonts()
        self.protocol('WM_DELETE_WINDOW', self.on_exit)
        self.after(POLL_MS, self._poll)
        self.after_idle(self._offer_import)

    # ── kiut.ini: the window's own sections ──
    def _read_window_settings(self) -> None:
        others = kset.other_sections(self.settings_file)
        w = dict(others.items('window')) if others.has_section('window') else {}
        self.kicad_exe = w.get('kicad', '')
        self.library_folder_text = (dict(others.items('folders')).get('libraries', '').strip()
                                    if others.has_section('folders') else '')
        self.tooltips_on = w.get('tooltips', 'yes') != 'no'
        self.last_folder = w.get('folder', '')
        self.geometry_text = w.get('geometry', '')
        r = dict(others.items('recent')) if others.has_section('recent') else {}
        self.recent_files: List[str] = [r[f'file{i}'] for i in range(MAX_RECENT)
                                        if r.get(f'file{i}', '').strip()]
        self.ratios: Dict[str, tuple] = dict(SEED_RATIOS)
        if others.has_section('font_ratios'):
            self.ratios = {}
            for name, text in others.items('font_ratios'):
                try:
                    h, w_ = text.split('|')
                    self.ratios[name] = (float(h), float(w_))
                except ValueError:
                    continue
        t = dict(others.items('technology')) if others.has_section('technology') else {}
        self.technology = {'mode': t.get('mode', 'single') if t.get('mode') in ('single', 'multi')
                           else 'single'}
        self.technology.update({k: t.get(k, '') for k in TECH_KEYS})
        if self.geometry_text:
            try:
                self.geometry(self.geometry_text)
            except tk.TclError:
                pass

    def _window_sections(self) -> Dict[str, Dict[str, str]]:
        return {
            'window': {'kicad': self.kicad_exe, 'tooltips': 'yes' if self.tooltips_on else 'no',
                       'folder': self.last_folder, 'geometry': self.geometry_text},
            'font_ratios': {name: f'{h:g}|{w:g}' for name, (h, w) in self.ratios.items()},
            'technology': dict(self.technology),
            'folders': {'libraries': self.library_folder_text} if self.library_folder_text else {},
            'recent': {f'file{i}': text for i, text in enumerate(self.recent_files[:MAX_RECENT])},
        }

    def save_last_used(self) -> None:
        """The values shown as the last used ones (kiut.ini), the window's own
        sections with them."""
        kset.write(self.settings_file, self.values, extra=self._window_sections())

    def save_window(self) -> None:
        """The window's own sections only; kiut.ini's settings as they are."""
        on_disk = kset.read(self.settings_file)[0] if self.settings_file.is_file() else kset.defaults()
        kset.write(self.settings_file, on_disk, extra=self._window_sections())

    # ── building ──
    def _tip(self, widget, text: str) -> None:
        common.Tooltip(widget, text, enabled=lambda: self.tooltips_on)

    def _setting_tip(self, widget, section: str, key: str) -> None:
        """A setting's tooltip: its description and its place in the
        settings file."""
        self._tip(widget, f'{kset._find(section, key).text}\n\n[{section}] {key}')

    def _build(self) -> None:
        if not self.geometry_text:
            self.geometry('980x760')
        self.minsize(820, 600)
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=0)
        file_menu.add_command(label='Open…', command=self.browse)
        self.recent_menu = tk.Menu(file_menu, tearoff=False, postcommand=self._fill_recent_menu)
        file_menu.add_cascade(label=OPEN_RECENT, menu=self.recent_menu)
        file_menu.add_command(label='Import old settings…', command=self.import_settings)
        file_menu.add_command(label='Save log…', command=self.save_log)
        file_menu.add_separator()
        file_menu.add_command(label=KICAD_PROGRAM, command=self.choose_kicad)
        file_menu.add_command(label=LIBRARY_FOLDER, command=self.choose_library_folder)
        file_menu.add_separator()
        file_menu.add_command(label='Exit', command=self.on_exit)
        self.file_menu = file_menu
        menu.add_cascade(label='File', menu=file_menu)
        self.schematic_menu = tk.Menu(menu, tearoff=0)
        self.schematic_menu.add_command(label=RENAME_ONLY, command=self.fix_designators)
        self.schematic_menu.add_separator()
        self.schematic_menu.add_command(label='Review auto-corrected pins…', command=self.review_pins)
        self.schematic_menu.add_command(label='Dump / Inspect', command=lambda: self.dump(False))
        self.schematic_menu.add_command(label='Dump Symbols', command=lambda: self.dump(True))
        menu.add_cascade(label='Schematic', menu=self.schematic_menu)
        help_menu = tk.Menu(menu, tearoff=0)
        help_menu.add_command(label='README', command=lambda: self.open_doc(APP_DIR / 'README.md'))
        help_menu.add_command(label='Settings reference',
                              command=lambda: self.open_doc(APP_DIR / 'docs' / 'SETTINGS.md'))
        help_menu.add_separator()
        help_menu.add_command(label='About KIUT', command=self.about)
        menu.add_cascade(label='Help', menu=help_menu)
        self.config(menu=menu)
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill='both', expand=True)
        self._build_design(outer)
        self._build_settings(outer)
        cols = ttk.Frame(outer)
        cols.pack(fill='x', pady=(6, 0))
        cols.columnconfigure(0, weight=1, uniform='c')
        cols.columnconfigure(1, weight=1, uniform='c')
        self._build_schematic(cols)
        self._build_board(cols)
        act = ttk.Frame(outer)
        act.pack(fill='x', pady=(8, 4))
        self.convert_button = ttk.Button(act, text='▶  Convert', command=self.convert)
        self.convert_button.pack(side='left')
        self._tip(self.convert_button, 'Convert the file(s) above -- the schematic with its board when '
                                       '"With its board" is ticked -- into the output folder shown.')
        self.kicad_button = ttk.Button(act, text='⎋  Open in KiCad', command=self.open_in_kicad)
        self.kicad_button.pack(side='left', padx=6)
        self._tip(self.kicad_button, lambda: "Open the converted design's KiCad project in KiCad: "
                  + (self.kicad_exe if self.kicad_exe else "with the program the system opens .kicad_pro "
                     "files with") + " (File → KiCad program…).")
        lf = ttk.LabelFrame(outer, text='Log', padding=4)
        lf.pack(fill='both', expand=True)
        self.log = tk.Text(lf, bg=LOG_BG, fg=LOG_FG, insertbackground=LOG_FG, height=10, wrap='word',
                           font=('Consolas', 9), relief='flat', state='disabled')
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.log.pack(fill='both', expand=True)
        for tag, colour in TAGS.items():
            self.log.tag_config(tag, foreground=colour)
        bar = ttk.Frame(outer)
        bar.pack(fill='x', pady=(4, 0))
        self.status = tk.StringVar(value='Ready')
        ttk.Label(bar, textvariable=self.status, relief='sunken', anchor='w').pack(
            side='left', fill='x', expand=True)
        self.tooltips_var = tk.BooleanVar(value=self.tooltips_on)
        ttk.Checkbutton(bar, text='Show tooltips', variable=self.tooltips_var,
                        command=self._on_tooltips).pack(side='right', padx=(8, 0))
        self._update_state()

    def _build_design(self, outer) -> None:
        des = ttk.LabelFrame(outer, text='Design', padding=6)
        des.pack(fill='x')
        des.columnconfigure(1, weight=1)
        ttk.Label(des, text='File(s):').grid(row=0, column=0, sticky='w')
        self.files_var = tk.StringVar()
        entry = ttk.Entry(des, textvariable=self.files_var)
        entry.grid(row=0, column=1, sticky='ew', padx=4)
        entry.bind('<Return>', lambda e: self.take_files())
        entry.bind('<FocusOut>', lambda e: self.take_files())
        self._tip(entry, 'The file(s) to convert, as kiut.py takes them: a .SCH (the main sheet), a .BLK '
                         'or a .UTSCH; one or more Ulticap libraries (.UTLIB, .LIB); a .DDF; an Ultiboard '
                         'library (.LIB, .L55), with its .TEC -- or several, with one. Several files: '
                         'separated by ";".')
        ttk.Button(des, text='Browse…', command=self.browse).grid(row=0, column=2)
        self.add_button = ttk.Button(des, text='Add…', command=self.add_files)
        self.add_button.grid(row=0, column=3, padx=(4, 0))
        self._tip(self.add_button, 'More files, from this folder or another, added to the File(s) field '
                                   '(Browse… replaces them).')
        self.with_board_var = tk.BooleanVar(value=True)
        self.with_board = ttk.Checkbutton(des, variable=self.with_board_var, command=self._on_with_board)
        self.with_board.grid(row=1, column=1, sticky='w', padx=4, pady=(2, 0))
        self._tip(self.with_board, "The schematic's board, found next to it (the same name, in the same "
                                   "folder): converted with it in one run, after the schematic -- one "
                                   "designator check for both, the footprints linked to their symbols.")
        # the messages about the files chosen (stage 10d): refused, warned or noted
        self.messages = common.MessageBlock(des)
        self.messages.grid(row=2, column=0, columnspan=4, sticky='ew', pady=(6, 0))
        ttk.Label(des, text='Output folder:').grid(row=3, column=0, sticky='w', pady=(4, 0))
        self.out_var = tk.StringVar()
        out_label = ttk.Label(des, textvariable=self.out_var, foreground='#333')
        out_label.grid(row=3, column=1, columnspan=3, sticky='w', padx=4, pady=(4, 0))
        self._tip(out_label, lambda: 'Where Convert writes. A design: next to its files. A library-only '
                                     'run: KIUT\'s library folder, ' + str(folders.library_root()) +
                                     ' (File → Library output folder…).')

    def _build_settings(self, outer) -> None:
        st = ttk.LabelFrame(outer, text='Settings', padding=6)
        st.pack(fill='x', pady=(6, 0))
        self.source_var = tk.StringVar(value='default')
        self.source_buttons = {}
        for value, text in (('design', 'Design'), ('kiut', 'Current'), ('default', 'Defaults')):
            b = ttk.Radiobutton(st, text=text, value=value, variable=self.source_var,
                                command=lambda v=value: self._load_source(v))
            b.pack(side='left', padx=(6, 6))
            self.source_buttons[value] = b
        self._tip(self.source_buttons['design'], "The design's own settings, kept next to its KiCad files "
                                                 "(<name>_KiCad/<name>_kiut.ini): written by its first "
                                                 "conversion, then by Save current settings.")
        self._tip(self.source_buttons['kiut'], 'The current settings, as last used or changed in this '
                                               'window (kiut.ini, next to kiut.py), saved by themselves. '
                                               'A setting changed by hand makes the set the current one.')
        self._tip(self.source_buttons['default'], "KIUT's built-in settings (docs/SETTINGS.md).")
        self.all_button = ttk.Button(st, text='\u2699  Advanced\u2026', command=self.all_settings)
        self.all_button.pack(side='right')
        self._tip(self.all_button, 'The settings not in this window: the schematic\'s fine tuning, the '
                                   'board\'s defaults, geometry, fallback values and V2/V3 staircases.')
        self.save_button = ttk.Button(st, text='Save current settings', command=self.save_design)
        self.save_button.pack(side='right', padx=6)
        self._tip(self.save_button, "Save the current settings as the design's own "
                                    "(<name>_KiCad/<name>_kiut.ini), read whenever the design is "
                                    "opened -- by this window and by kiut.py. The current settings "
                                    "themselves are saved by themselves, in kiut.ini next to kiut.py.")

    def _font_row(self, frame, section: str) -> tk.StringVar:
        var = tk.StringVar(value=KICAD_FONT)
        ttk.Label(frame, text='Font:').grid(row=0, column=0, sticky='w')
        combo = ttk.Combobox(frame, textvariable=var, width=26, state='readonly',
                             postcommand=lambda: self._fill_fonts(combo, section))
        combo.grid(row=0, column=1, columnspan=2, sticky='w', padx=4)
        combo.bind('<KeyPress>', lambda e: self._font_key(e, combo, var))
        self.font_combos[section] = combo
        self._setting_tip(combo, section, 'font')
        b = ttk.Button(frame, text='Use KiCad Font', command=lambda: self.use_kicad_font(section))
        b.grid(row=1, column=1, sticky='w', padx=4, pady=(4, 0))
        mono = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(frame, text='Mono only', variable=mono,
                             command=lambda: self._fill_fonts(combo, section))
        cb.grid(row=1, column=2, sticky='w', padx=4, pady=(4, 0))
        self._tip(cb, 'List monospaced fonts only.')
        self._mono_vars[section] = mono
        ttk.Separator(frame).grid(row=2, column=0, columnspan=3, sticky='ew', pady=6)
        var.trace_add('write', lambda *a: self._on_font(section, var))
        return var

    _mono_vars: Dict[str, tk.BooleanVar] = {}

    def _build_schematic(self, cols) -> None:
        self._mono_vars = {}
        self.font_combos: Dict[str, ttk.Combobox] = {}
        sch = ttk.LabelFrame(cols, text='Schematic', padding=6)
        sch.grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        self.schematic_frame = sch
        self.font_vars = {'schematic': self._font_row(sch, 'schematic')}
        self.switch_vars: Dict[str, tk.BooleanVar] = {}
        self.switch_widgets: Dict[str, ttk.Checkbutton] = {}
        for r, (key, text) in enumerate(SCHEMATIC_SWITCHES, 3):
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(sch, text=text, variable=var)
            self.switch_widgets[key] = cb
            cb.grid(row=r, column=0, columnspan=3, sticky='w')
            self._setting_tip(cb, 'schematic', key)
            var.trace_add('write', lambda *a, k=key, v=var: self._on_value('schematic', k, v.get()))
            self.switch_vars[key] = var
        row = ttk.Frame(sch)
        row.grid(row=9, column=0, columnspan=3, sticky='w', pady=(8, 0))
        self.viewer_button = ttk.Button(row, text='Open in Viewer', command=self.open_in_viewer)
        self.viewer_button.pack(side='left')
        self._tip(self.viewer_button, 'Open the schematic (.SCH, .UTSCH) in the KIUT viewer, a program of '
                                      'its own (kiut_viewer.py; PySide6).')
        self.compare_button = ttk.Button(row, text='Compare netlists', command=self.compare_netlists)
        self.compare_button.pack(side='left', padx=6)
        self._tip(self.compare_button, "The converted schematic's nets against Ulticap's netlist of the "
                                       "design (<name>.NET next to its files), net by net -- the pins on "
                                       "each net, the names ignored; designators the designator fix "
                                       "renamed are read as renamed. KiCad's netlist is made with "
                                       "kicad-cli; the report goes to the output folder.")

    def _build_board(self, cols) -> None:
        brd = ttk.LabelFrame(cols, text='Board', padding=6)
        brd.grid(row=0, column=1, sticky='nsew', padx=(4, 0))
        self.board_frame = brd
        self.font_vars['board'] = self._font_row(brd, 'board')
        self.ratio_vars: Dict[str, tk.StringVar] = {}
        self.ratio_entries: Dict[str, ttk.Entry] = {}
        style = ttk.Style(self)
        style.configure('Invalid.TEntry', fieldbackground='#ffd0d0')
        for r, (key, text) in enumerate((('font_height_ratio', 'Text height ratio:'),
                                         ('font_width_ratio', 'Text width ratio:')), 3):
            ttk.Label(brd, text=text).grid(row=r, column=0, sticky='w', pady=(0, 4))
            var = tk.StringVar()
            e = ttk.Entry(brd, textvariable=var, width=8)
            e.grid(row=r, column=1, sticky='w', padx=4, pady=(0, 4))
            self._setting_tip(e, 'board', key)
            var.trace_add('write', lambda *a, k=key: self._on_ratio(k))
            self.ratio_vars[key], self.ratio_entries[key] = var, e
        ttk.Label(brd, text='Library only:').grid(row=5, column=0, sticky='w')
        self.library_only_var = tk.StringVar(value='ask')
        lo = ttk.Combobox(brd, textvariable=self.library_only_var, values=('ask', 'yes', 'no'), width=6,
                          state='readonly')
        lo.grid(row=5, column=1, sticky='w', padx=4)
        self._setting_tip(lo, 'board', 'library_only')
        self.library_only_var.trace_add(
            'write', lambda *a: self._on_value('board', 'library_only', self.library_only_var.get()))
        self.staircase_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(brd, text='V2/V3: merge staircases', variable=self.staircase_var)
        self.staircase_check = cb
        cb.grid(row=6, column=0, columnspan=3, sticky='w', pady=(4, 0))
        self._setting_tip(cb, 'board', 'v2v3_staircase_merge')
        self.staircase_var.trace_add(
            'write', lambda *a: self._on_value('board', 'v2v3_staircase_merge', self.staircase_var.get()))

    # ── the fonts ──
    def _check_fonts(self) -> None:
        """A font the settings name that isn't installed here: said (kept --
        the KiCad files name it, and KiCad shows its own font until it is
        installed; KIUC's writer replaces DejaVu Sans Mono and Ulticap Stroke
        when missing, with its own warning)."""
        if self._fonts is None:
            self._fonts = common.installed_fonts(self)
        for section in ('schematic', 'board'):
            font = self.values[section]['font']
            if font != KICAD_FONT and font not in self._fonts:
                self.write_log(f"WARNING: the {section}'s font, {font}, is not installed on this computer "
                               f"(see Fonts in the README: Help \u2192 README).")

    def _fill_fonts(self, combo, section: str) -> None:
        if self._fonts is None:
            self._fonts = common.installed_fonts(self)
        fonts = self._fonts
        if self._mono_vars[section].get():
            for f in fonts:
                if f not in self._mono:
                    self._mono[f] = common.is_monospaced(f)
            fonts = [f for f in fonts if self._mono[f]]
        combo['values'] = common.ordered_fonts(KICAD_FONT, PINNED[section], fonts)

    def use_kicad_font(self, section: str) -> None:
        """Use KiCad Font: KiCad's own font, and every font listed again (Mono
        only off), as KIUC's and KIUB's windows did."""
        self._mono_vars[section].set(False)
        self._fill_fonts(self.font_combos[section], section)
        self.font_vars[section].set(KICAD_FONT)

    def _font_key(self, event, combo, var) -> Optional[str]:
        if len(event.char) != 1 or not event.char.isprintable():
            return None
        found = common.next_font(list(combo['values']) or [var.get()], var.get(), event.char.lower())
        if found:
            var.set(found)
        return 'break'

    def _on_font(self, section: str, var: tk.StringVar) -> None:
        value = var.get().strip()
        if value == common.FONT_SEPARATOR:
            self._loading = True
            var.set(self.values[section]['font'])
            self._loading = False
            return
        if self._loading or not value:
            return
        self.values[section]['font'] = value       # before the fields are shown again
        if section == 'board' and value in self.ratios:
            h, w = self.ratios[value]
            self.values['board']['font_height_ratio'] = h
            self.values['board']['font_width_ratio'] = w
            self._show_values()
        self._on_value(section, 'font', value)

    def _on_ratio(self, key: str) -> None:
        if self._loading:
            return
        st = kset._find('board', key)
        text = self.ratio_vars[key].get().strip()
        try:
            value = float(text)
            ok = st.low <= value <= st.high
        except ValueError:
            value, ok = None, False
        self.ratio_entries[key].configure(style='TEntry' if ok else 'Invalid.TEntry')
        if value is None:
            return
        self.values['board'][key] = value
        font = self.values['board']['font']
        self.ratios[font] = (self.values['board']['font_height_ratio'], self.values['board']['font_width_ratio'])
        self._changed_by_hand()

    # ── the settings ──
    def _on_value(self, section: str, key: str, value) -> None:
        if self._loading:
            return
        self.values[section][key] = value
        if (section, key) == ('schematic', 'block_library'):
            self._show_output()
        self._changed_by_hand()

    def _changed_by_hand(self) -> None:
        """A value changed in the window: the set is the last used one now."""
        self.source = 'kiut'
        self.source_var.set('kiut')
        try:
            self.save_last_used()
        except OSError as e:
            self.write_log(f'WARNING: could not save the settings in {self.settings_file}: {e}')
        self._update_state()

    def _show_values(self) -> None:
        """The values in the window's fields (no change is triggered)."""
        self._loading = True
        try:
            for section in ('schematic', 'board'):
                self.font_vars[section].set(self.values[section]['font'])
            for key, var in self.switch_vars.items():
                var.set(bool(self.values['schematic'][key]))
            for key, var in self.ratio_vars.items():
                var.set(kset.text_of(kset._find('board', key), self.values['board'][key]))
                self.ratio_entries[key].configure(style='TEntry')
            self.library_only_var.set(self.values['board']['library_only'])
            self.staircase_var.set(bool(self.values['board']['v2v3_staircase_merge']))
        finally:
            self._loading = False

    def _load_source(self, source: Optional[str], say: bool = True) -> None:
        """The settings from source ('design', 'kiut', 'default'; None: the
        design's copy, else the last used, else the defaults), shown and
        named in the log. Nothing is written."""
        chosen = kset.choose(source, self.design_file, self.settings_file)
        self.values, self.source = chosen.values, chosen.source
        self.source_var.set(self.source)
        self._show_values()
        self._show_output()
        if say:
            for level, text in chosen.messages:
                if level == 'info' and text.endswith('(last used)'):
                    text = text[:-len('(last used)')] + f"({SOURCE_NAMES['kiut']})"
                self.write_log({'warn': 'WARNING: ', 'note': 'NOTE: ', 'info': ''}[level] + text)
            self._check_fonts()
        self._update_state()

    def chosen(self) -> kset.Chosen:
        """The values shown, as a run's settings."""
        path = {'design': self.design_file, 'kiut': self.settings_file}.get(self.source)
        text = f'Settings: {path} ({SOURCE_NAMES[self.source]})' if path else 'Settings: built-in defaults'
        return kset.Chosen(_copy.deepcopy(self.values), self.source, path, [('info', text)])

    def save_design(self) -> None:
        if self.design_file is None or not self.design_file.is_file():
            return
        if not messagebox.askyesno('Save current settings',
                                   f"Replace the design's settings with the current ones?\n\n{self.design_file}",
                                   parent=self):
            return
        try:
            kset.write(self.design_file, self.values)
        except OSError as e:
            messagebox.showerror('Save current settings', f'Could not write {self.design_file}:\n{e}',
                                 parent=self)
            return
        self.source = 'design'
        self.source_var.set('design')
        self.converted_since_save = False
        self.write_log(f"Settings saved for the design: {self.design_file}", 'ok')
        self._update_state()

    def all_settings(self) -> None:
        dlg = AdvancedDialog(self, self.values,
                             f"the design's ({self.design_file.name})" if self.source == 'design'
                             else SOURCE_NAMES[self.source])
        self.wait_window(dlg)
        if dlg.result is None or dlg.result == self.values:
            return
        for level, text in dlg.notes:
            self.write_log(f'NOTE: {text}')
        self.values = dlg.result
        font = self.values['board']['font']
        self.ratios[font] = (self.values['board']['font_height_ratio'], self.values['board']['font_width_ratio'])
        self._show_values()
        self._show_output()
        self._changed_by_hand()

    # ── the input ──
    def browse(self) -> None:
        names = filedialog.askopenfilenames(
            parent=self, title='Choose the file(s) to convert', initialdir=self.last_folder or None,
            filetypes=[('Ulticap and Ultiboard files',
                        '*.sch *.SCH *.blk *.BLK *.utsch *.UTSCH *.utlib *.UTLIB *.lib *.LIB '
                        '*.ddf *.DDF *.l55 *.L55 *.tec *.TEC'),
                       ('All files', '*.*')])
        if not names:
            return
        self.last_folder = str(Path(names[0]).parent)
        self.files_var.set('; '.join(names))
        self.take_files()
        try:
            self.save_window()
        except OSError:
            pass

    def add_files(self) -> None:
        """More files added to the File(s) field, from any folder (the
        system's file dialog picks in one folder at a time); a file already
        there not added twice."""
        names = filedialog.askopenfilenames(
            parent=self, title='Add file(s) to convert', initialdir=self.last_folder or None,
            filetypes=[('Ulticap and Ultiboard files',
                        '*.sch *.SCH *.blk *.BLK *.utsch *.UTSCH *.utlib *.UTLIB *.lib *.LIB '
                        '*.ddf *.DDF *.l55 *.L55 *.tec *.TEC'),
                       ('All files', '*.*')])
        if not names:
            return
        self.last_folder = str(Path(names[0]).parent)
        have = self.file_names()
        seen = {os.path.normcase(os.path.abspath(n)) for n in have}
        for n in names:
            key = os.path.normcase(os.path.abspath(n))
            if key not in seen:
                seen.add(key)
                have.append(str(Path(n)))
        self.files_var.set('; '.join(have))
        self.take_files()
        try:
            self.save_window()
        except OSError:
            pass

    def open_files(self, names: List[str]) -> None:
        """Files given to the window (kiut_gui.py's arguments, the viewer's
        Open in KIUT…)."""
        self.files_var.set('; '.join(str(n) for n in names))
        self.take_files()

    def file_names(self) -> List[str]:
        return [n.strip() for n in self.files_var.get().split(';') if n.strip()]

    def take_files(self) -> None:
        """The File(s) field read: its inputs, a board found next to a
        schematic, the design's settings file -- and its settings loaded when
        the design changed."""
        names = self.file_names()
        if self.inputs is not None and [str(f) for f in self.inputs.files] + (
                [str(self.inputs.tec)] if self.inputs.tec else []) == names \
                and all(Path(n).exists() for n in names):
            return                                  # the same files, still there
        self.inputs, self.board_ddf = None, None
        if not names:
            self._set_design(None, '')
            self._say([])
            return
        try:
            inputs = cli.classify(names)
        except cli.InputError as e:
            self._set_design(None, '')
            self._say([('error', str(e))])
            for line in e.details:              # the log only (stage 10g)
                self.write_log(line)
            return
        bad = _not_what_it_says(inputs)
        if bad:
            self._set_design(None, '')
            self._say([('error', bad)])
            return
        said = []
        if inputs.kind == 'design':                 # a schematic and its DDF both given: one design
            self.board_ddf = inputs.ddf
            inputs = cli.Inputs(inputs.sch_kind, inputs.files)
            self.with_board_var.set(True)
        self.inputs = inputs
        if inputs.kind in ('schematic', 'utsch') and self.board_ddf is None:
            self.board_ddf = self._board_of(inputs)
            self.with_board_var.set(True)
        if inputs.kind == 'board':
            sch = self._schematic_of(inputs.files[0])
            if sch is not None:
                said.append(('note', f'Its schematic, {sch.name}, is in this folder: choose the schematic '
                                     f'to convert both together.'))
        said += [(('warn' if level == 'warn' else 'note'), text) for level, text in
                 cli.selection_messages(inputs, self.values['board']['library_only'])]
        self._add_recent(names)
        design_file, name = cli._design_file(self.run_inputs() or inputs)
        changed = design_file != self.design_file
        self._set_design(design_file, name)
        self._say(said)
        if changed:
            self._list_sheets(inputs)

    # ── File > Open Recent ──
    def _add_recent(self, names: List[str]) -> None:
        """A selection the window took, first in the recent list (once; at most
        MAX_RECENT), kept in kiut.ini."""
        text = '; '.join(str(n) for n in names)
        key = os.path.normcase(text)
        updated = [text] + [t for t in self.recent_files if os.path.normcase(t) != key]
        updated = updated[:MAX_RECENT]
        if updated != self.recent_files:
            self.recent_files = updated
            try:
                self.save_window()
            except OSError:
                pass

    @staticmethod
    def recent_label(text: str) -> str:
        """A recent selection as the menu shows it: its first file, '+ N more'
        for a set."""
        names = [n.strip() for n in text.split(';') if n.strip()]
        return names[0] + (f'  + {len(names) - 1} more' if len(names) > 1 else '')

    def _fill_recent_menu(self) -> None:
        m = self.recent_menu
        m.delete(0, 'end')
        for text in self.recent_files:
            there = all(Path(n.strip()).exists() for n in text.split(';') if n.strip())
            m.add_command(label=self.recent_label(text), state='normal' if there else 'disabled',
                          command=lambda t=text: self.open_recent(t))
        if self.recent_files:
            m.add_separator()
        m.add_command(label=EMPTY_RECENT, command=self.empty_recent,
                      state='normal' if self.recent_files else 'disabled')

    def open_recent(self, text: str) -> None:
        names = [n.strip() for n in text.split(';') if n.strip()]
        if self.running:
            return
        self.last_folder = str(Path(names[0]).parent)
        self.open_files(names)
        self._add_recent(names)                     # first again, also when it was open already

    def empty_recent(self) -> None:
        self.recent_files = []
        try:
            self.save_window()
        except OSError:
            pass

    # ── one KIUT window; the viewer's Open in KIUT… (kiut/core/handoff.py) ──
    def start_handoff(self, name: Optional[str] = None) -> bool:
        """Listening as the KIUT window: files given to a second start, and the
        viewers' Open in KIUT…. False when another window listens already (one
        started at the same moment)."""
        from kiut.core import handoff
        self.handoff_name = name
        listener = handoff.Listener(handoff.WINDOW,
                                    lambda paths, sender: self.on_main(lambda: self.receive(paths, sender)),
                                    name=name)
        if listener.start():
            self._handoff = listener
            return True
        return False

    def stop_handoff(self) -> None:
        if self._handoff is not None:
            self._handoff.stop()
            self._handoff = None

    def receive(self, paths: List[Path], sender: Optional[str] = None) -> str:
        """Files handed to the window -- by a second start of KIUT or a viewer's
        Open in KIUT… (`sender`, that viewer's id, then KIUT's own viewer):
        opened (a corrected copy as its original), or, when they are the ones
        shown or there are none, only brought to the front; refused while the
        window is busy."""
        from kiut.core import handoff
        originals = [handoff.corrected_original(p) for p in paths]
        if self.running and originals:
            self.write_log(f'NOTE: {originals[0].name} handed to KIUT refused: KIUT is busy.')
            return handoff.BUSY
        if sender:
            self.own_viewer = sender
        shown = not originals or (self.inputs is not None and (
            handoff.same_files(originals, self.inputs.files)
            or handoff.same_files(originals, self.inputs.files + ([self.board_ddf] if self.board_ddf else []))))
        if not shown:
            self.last_folder = str(originals[0].parent)
            self.open_files([str(o) for o in originals])
        self.to_front()
        return handoff.SHOWN if shown else handoff.OPENED

    def to_front(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.attributes('-topmost', True)
            self.after(300, lambda: self.attributes('-topmost', False))
            self.focus_force()
        except tk.TclError:
            pass

    def _say(self, messages) -> None:
        """The message block's messages, also in the log."""
        self.messages.set(messages)
        prefix = {'error': 'ERROR: ', 'warn': 'WARNING: ', 'note': 'NOTE: '}
        for level, text in self.messages.messages:
            self.write_log(prefix[level] + text)

    def _list_sheets(self, inputs: cli.Inputs) -> None:
        """A schematic's sheets in the log when it is chosen (as KIUC's window
        listed them): a .SCH design's sub-sheets found, a .UTSCH's sheets, a
        sub-sheet that's missing; reading the file's messages too."""
        if inputs.kind not in ('schematic', 'utsch') or any(f.suffix.upper() == '.BLK' for f in inputs.files):
            return
        from kiut.core.corrected import CorrectedFolder, locate
        from kiut.schematic.kiuc_ascii import collect_hierarchy_paths, parse_sch
        from kiut.schematic.kiuc_writer import check_missing_sheets
        first = inputs.files[0]
        try:
            working = CorrectedFolder(locate(first), 'KIUT').working(first)
        except Exception:
            working = first
        working = Path(working) if working and Path(working).is_file() else first
        self.status.set('Reading the design…')
        self.update_idletasks()
        try:
            if inputs.kind == 'utsch':
                from kiut.schematic import kiuc_binary_semantic as ks
                sch, warnings = ks.read_schematic_file(working)
                for w in warnings:
                    self.write_log(f'WARNING: {w}')
                for sh in sch.sheets[1:]:
                    self.write_log(f'Sub-sheet in the file: {sh.name}')
                missing = check_missing_sheets(sch.sheets)
            else:
                errors: list = []
                paths = collect_hierarchy_paths(working, errors=errors)
                for p in paths[1:]:
                    self.write_log(f'Sub-sheet found: {p.name}')
                sheets = []
                for p in paths:
                    sheet, errs = parse_sch(p)
                    sheets.append(sheet)
                    errors += [e for e in errs if e not in errors]
                for e in errors:
                    self.write_log(e)
                missing = check_missing_sheets(sheets)
            for w in missing:
                self.write_log(f'WARNING: {w}')
        except Exception as e:                       # the conversion says it in full
            self.write_log(f'WARNING: the design could not be read in full ({e}).')
        finally:
            self.status.set('Ready')

    def _board_of(self, inputs: cli.Inputs) -> Optional[Path]:
        """A schematic's board: the DDF of its name in its folder."""
        first = inputs.files[0]
        if first.suffix.upper() == '.BLK':
            return None
        from kiut.core.corrected import locate
        loc = locate(first)
        for p in sorted(loc.home.iterdir()) if loc.home.is_dir() else []:
            if p.suffix.lower() == '.ddf' and p.stem.lower() == loc.name.lower() and p.is_file():
                return p
        return None

    def _schematic_of(self, ddf: Path) -> Optional[Path]:
        from kiut.core.corrected import locate
        loc = locate(ddf)
        for ext in ('.sch', '.utsch'):
            for p in sorted(loc.home.iterdir()) if loc.home.is_dir() else []:
                if p.suffix.lower() == ext and p.stem.lower() == loc.name.lower() and p.is_file():
                    return p
        return None

    def run_inputs(self) -> Optional[cli.Inputs]:
        """What Convert converts: the File(s) field's inputs, with the board
        when "With its board" is ticked."""
        if self.inputs is None:
            return None
        if self.board_ddf is not None and self.with_board_var.get():
            return cli.Inputs('design', self.inputs.files, ddf=self.board_ddf, sch_kind=self.inputs.kind)
        return self.inputs

    def _set_design(self, design_file: Optional[Path], name: str) -> None:
        changed = design_file != self.design_file
        if changed:
            self.review = None
            self.clear_log()                        # another design (or none): its own log
        self.design_file, self.design_name = design_file, name
        if changed and design_file is not None:
            self.write_log(f'Design: {"; ".join(self.file_names())}' + (
                f' and its board {self.board_ddf.name}' if self.run_inputs() is not None
                and self.run_inputs().kind == 'design' else ''))
            self._load_source(None)
        self._show_output()
        self._update_state()

    def _on_with_board(self) -> None:
        self._update_state()
        self._show_output()

    def output_folder(self) -> Optional[Path]:
        """Where Convert writes, as kiut.py would (the settings shown)."""
        inputs = self.run_inputs()
        if inputs is None:
            return None
        first = inputs.files[0]
        if inputs.kind in ('board', 'board_library'):
            from kiut.board import kiub
            home = cli.library_output(inputs)
            if home is None and inputs.kind == 'board' and self._library_ddf(first):
                home = folders.library_folder('footprints')     # a library DDF (stage 10f)
            if home is not None:            # KIUT's Footprints folder: the overview board below it
                return home / (kiub.design_name(str(first)) + kiub.LIBRARY_FOLDER_SUFFIX)
            return Path(kiub.default_output_dir(str(first), inputs.kind == 'board_library'
                                                or self.values['board']['library_only'] == 'yes'
                                                or self._shapes_only(first)))
        from kiut.schematic.kiuc_writer import default_output_dir
        block = self._block_run(inputs)
        return cli.library_output(inputs, block) or default_output_dir(first, block=block)

    def _library_ddf(self, ddf: Path) -> bool:
        """A DDF that goes to KIUT's Footprints folder (kiub's library_home):
        shapes only, or components without nets with library_only = yes."""
        from kiut.board import kiub
        kind = cli.ddf_kind(ddf)
        return kind == kiub.DDF_LIBRARY or (kind == kiub.DDF_UNSURE and self.values['board']['library_only'] == 'yes')

    def _block_run(self, inputs: cli.Inputs) -> bool:
        return inputs.kind == 'schematic' and (
            any(f.suffix.upper() == '.BLK' for f in inputs.files) or bool(self.values['schematic']['block_library']))

    def output_text(self) -> str:
        """The Output folder line: the folder Convert writes to; for
        Ultiboard libraries the Footprints folder and what goes where."""
        inputs = self.run_inputs()
        if inputs is None:
            return ''
        if inputs.kind == 'board_library':
            home = cli.library_output(inputs)
            if home is None:
                from kiut.core.corrected import locate
                homes = {locate(f).home.resolve() for f in inputs.files}
                if len(inputs.files) == 1:
                    return str(self.output_folder())
                where = str(locate(inputs.files[0]).home) if len(homes) == 1 else 'next to each library'
                return f'{where}  —  each library into its own <name>_lib_KiCad'
            if len(inputs.files) == 1:
                name = inputs.files[0].stem
                return f'{home}  —  {name}.pretty, its overview board in {name}_lib_KiCad'
            return f'{home}  —  each library its own <name>.pretty, its overview board in <name>_lib_KiCad'
        if inputs.kind == 'board' and folders.library_folder('footprints') is not None:
            from kiut.board import kiub
            first = inputs.files[0]
            name = kiub.design_name(str(first))
            fp = folders.library_folder('footprints')
            if self._library_ddf(first):
                return f'{fp}  —  {name}.pretty, its overview board in {name}_lib_KiCad'
            if cli.ddf_kind(first) == kiub.DDF_UNSURE and self.values['board']['library_only'] == 'ask':
                return f'{self.output_folder()}  —  or, converted as a library, {fp}'
        out = self.output_folder()
        return str(out) if out else ''

    _v2v3_seen: Dict[tuple, bool] = {}

    def _is_v2v3(self, ddf: Path) -> bool:
        """A V2/V3 DDF (its version line), cached by file and time."""
        from kiut.board import kiub
        try:
            key = (str(ddf), ddf.stat().st_mtime_ns)
            if key not in self._v2v3_seen:
                self._v2v3_seen[key] = kiub._peek_ddf_version(str(ddf)) in (2, 3)
            return self._v2v3_seen[key]
        except (OSError, ValueError):
            return False

    _shapes_seen: Dict[tuple, bool] = {}

    def _shapes_only(self, ddf: Path) -> bool:
        """A DDF holding shapes only: always a library (KIUB's ddf_kind), so
        its output folder is <name>_lib_KiCad before converting it."""
        from kiut.board import kiub
        try:
            key = (str(ddf), ddf.stat().st_mtime_ns)
            if key not in self._shapes_seen:
                self._shapes_seen[key] = kiub.ddf_kind(ddf.read_bytes()) == kiub.DDF_LIBRARY
            return self._shapes_seen[key]
        except OSError:
            return False

    def _show_output(self) -> None:
        self.out_var.set(self.output_text())

    # ── states ──
    def _update_state(self) -> None:
        inputs = self.run_inputs()
        kind = inputs.kind if inputs else None
        # "With its board" always there (the Design section never changes height), greyed out and
        # unticked when there is no board to take along (stage 10e)
        if self.board_ddf is not None and self.inputs is not None:
            self.with_board.configure(text=f'With its board: {self.board_ddf.name}  (found next to the '
                                           f'schematic)')
            _set_enabled(self.with_board, not self.running)
        else:
            sch_alone = (self.inputs is not None and self.inputs.kind in ('schematic', 'utsch')
                         and not any(f.suffix.upper() == '.BLK' for f in self.inputs.files))
            self.with_board.configure(text='With its board: none found next to the schematic' if sch_alone
                                      else 'With its board')
            self.with_board_var.set(False)
            _set_enabled(self.with_board, False)
        sch = kind in ('schematic', 'utsch', 'libraries', 'design')
        brd = kind in ('board', 'board_library', 'design')
        _set_enabled(self.schematic_frame, sch or kind is None)
        _set_enabled(self.board_frame, brd or kind is None)
        # the switches that don't apply to this input (as KIUC's window had them)
        not_for = {'libraries': ('v5_header', 'block_library', 'fix_designators', 'allow_missing_sheets'),
                   'utsch': ('v5_header', 'block_library')}.get(self.inputs.kind if self.inputs else None, ())
        for key in not_for:
            _set_enabled(self.switch_widgets[key], False)
        # V2/V3: merge staircases only for a V2/V3 DDF
        ddf = self.board_ddf if kind == 'design' else self.inputs.files[0] if kind == 'board' else None
        _set_enabled(self.staircase_check, (ddf is not None and self._is_v2v3(ddf)) or kind is None)
        has_copy = self.design_file is not None and self.design_file.is_file()
        _set_enabled(self.source_buttons['design'], has_copy)
        _set_enabled(self.save_button, has_copy and not self.running)
        _set_enabled(self.convert_button, inputs is not None and not self.running)
        _set_enabled(self.all_button, not self.running)
        _set_enabled(self.kicad_button, self.kicad_target() is not None and not self.running)
        idle = not self.running
        own = self.inputs.kind if self.inputs is not None else None
        blk = self.inputs is not None and any(f.suffix.upper() == '.BLK' for f in self.inputs.files)
        design = own in ('schematic', 'utsch') and not blk
        _set_enabled(self.viewer_button, design and idle)
        _set_enabled(self.compare_button, design and idle and self._converted_schematic() is not None)
        for label, on in ((RENAME_ONLY, design),
                          ('Review auto-corrected pins…', self.review is not None),
                          ('Dump / Inspect', own in ('schematic', 'utsch')),
                          ('Dump Symbols', own in ('schematic', 'utsch'))):
            self.schematic_menu.entryconfig(label, state='normal' if on and idle else 'disabled')

    def _on_tooltips(self) -> None:
        self.tooltips_on = bool(self.tooltips_var.get())
        try:
            self.save_window()
        except OSError:
            pass

    # ── the log ──
    def write_log(self, line: str, tag: Optional[str] = None) -> None:
        self.log.configure(state='normal')
        self.log.insert('end', line + '\n', tag or line_tag(line))
        self.log.configure(state='disabled')
        self.log.see('end')

    def clear_log(self) -> None:
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def recent_lines(self) -> str:
        """The report's lines since the last question (a question's
        reasons), the last 15."""
        lines, self._recent = self._recent[-15:], []
        return '\n'.join(lines)

    # ── the conversion, in its own thread ──
    def on_main(self, fn: Callable):
        """fn() run by the window's own thread (a dialog), from the
        conversion's thread; its result."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        box: dict = {}
        done = threading.Event()
        self._queue.put(('call', fn, box, done))
        done.wait()
        if 'error' in box:
            raise box['error']
        return box.get('value')

    def _poll(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                if item[0] == 'line':
                    self._recent.append(item[1])
                    self._run_lines.append(item[1])
                    self.write_log(item[1])
                elif item[0] == 'call':
                    _, fn, box, done = item
                    try:
                        box['value'] = fn()
                    except BaseException as e:          # back to the conversion's thread
                        box['error'] = e
                    done.set()
                elif item[0] == 'done':
                    _, done, result = item
                    self.running = False
                    try:
                        done(result)
                    finally:
                        self._update_state()
        except queue.Empty:
            pass
        self.after(POLL_MS, self._poll)

    def convert(self) -> None:
        self.take_files()
        inputs = self.run_inputs()
        if inputs is None or self.running:
            return
        inputs = _copy.copy(inputs)
        chosen = self.chosen()
        if inputs.kind == 'board_library' and inputs.tec is None and not chosen.get('board', 'tec_split'):
            if not self._ask_technology(inputs):
                return
        try:
            cli.check(inputs, chosen)
        except cli.InputError as e:
            messagebox.showerror('KIUT', str(e), parent=self)
            return
        try:
            self.save_last_used()                   # a conversion's settings: the last used ones
        except OSError as e:
            self.write_log(f'WARNING: could not save the settings in {self.settings_file}: {e}')
        where = {'out_dir': self.output_folder(), 'name': self.design_name}
        self.last_project = None                    # until this conversion gives its own
        self.run_in_thread('Converting…',
                           lambda: _in_window_words(lambda: cli.convert(inputs, chosen, asking=_WindowAsking(self),
                                                                        where=where)),
                           lambda code: self._finished(1 if code is None else code, inputs, where))

    def run_in_thread(self, status: str, work: Callable[[], object], done: Callable[[object], None]) -> None:
        """work() in a thread of its own, its output to the (cleared) log, its
        questions through on_main; then done(its result -- None when it
        failed) in the window's thread."""
        self.clear_log()
        self._recent, self._run_lines = [], []
        self.running = True
        self.status.set(status)
        self._update_state()
        threading.Thread(target=self._thread, args=(work, done), daemon=True).start()

    def _thread(self, work, done) -> None:
        writer = _QueueWriter(self._queue)
        result = None
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            try:
                result = work()
            except Exception:
                print(f'ERROR: {traceback.format_exc()}')
            writer.flush()
        self._queue.put(('done', done, result))

    def _finished(self, code: int, inputs, where: dict) -> None:
        out_dir, name = where.get('out_dir'), where.get('name')
        if code == 0 and inputs.kind in ('schematic', 'utsch', 'design'):
            from kiut.schematic import kiuc_writer as kw
            comps = kw.get_pin_override_corrected_components()
            self.review = (comps, kw.get_pin_override_corrections()) if comps else None
        if code == 0:
            self.write_log('Conversion done.', 'ok')
            self.status.set('Conversion done')
            self.converted_since_save = True
            if out_dir is not None:
                pro = Path(out_dir) / f'{name}.kicad_pro'
                self.last_project = pro if pro.is_file() else self.last_project
        else:
            self.status.set('Conversion cancelled' if cli.CANCELLED in self._run_lines else
                            'Conversion failed -- see the log')
        for out_dir, name in where.get('folders') or [(out_dir, name)]:     # several libraries: each folder
            if out_dir is not None and Path(out_dir).is_dir() and inputs.kind != 'libraries':
                try:
                    (Path(out_dir) / f'{name}.log').write_text('\n'.join(self._run_lines) + '\n',
                                                               encoding='utf-8')
                except OSError as e:
                    self.write_log(f'WARNING: could not write the log {Path(out_dir) / (name + ".log")}: {e}')
        self._update_state()

    def _ask_technology(self, inputs: cli.Inputs) -> bool:
        """An Ultiboard library's technology, asked (KIUB's dialog); put on
        inputs for this run, remembered in kiut.ini. False when cancelled."""
        names = ', '.join(f.name for f in inputs.files)
        dlg = dialogs._TechnologyDialog(self, names, dict(self.technology))
        self.wait_window(dlg)
        if dlg.result is None:
            return False
        self.technology = {k: dlg.result.get(k, '') for k in ('mode',) + TECH_KEYS}
        if dlg.result['mode'] == 'single':
            inputs.tec = Path(dlg.result['single'])
        else:
            inputs.tec_split = [Path(dlg.result[k]) for k in ('top', 'bottom', 'inner', 'drills')]
            if dlg.result.get('traces'):
                inputs.tec_split.append(Path(dlg.result['traces']))
        try:
            self.save_window()
        except OSError:
            pass
        return True

    # ── the schematic's actions ──
    def _converted_schematic(self) -> Optional[Path]:
        """The design's converted schematic, <out>/<name>.kicad_sch, when there."""
        out = self.output_folder()
        sch = out / f'{self.design_name}.kicad_sch' if out is not None else None
        return sch if sch is not None and sch.is_file() else None

    def confirm(self, title: str, text: str, ok: str = 'Continue') -> bool:
        dlg = _Confirm(self, title, text, ok)
        self.wait_window(dlg)
        return dlg.ok

    def fix_designators(self) -> None:
        """Rename designators without converting: the renames shown first (a confirmation),
        then kiut.py --fix-only's steps -- the corrected copies of the
        schematic and its DDF written, nothing converted."""
        self.take_files()
        inputs = self.run_inputs()
        if inputs is None or self.running:
            return
        try:
            cli.check_fix_only(inputs)
        except cli.InputError as e:
            messagebox.showerror(RENAME_ONLY[:-1], str(e), parent=self)
            return
        from kiut.core.corrected import CORRECTED_SUFFIX, locate
        from kiut.core.kicad_links import _natural
        from kiut.schematic.kiuc_refdes import plan_new_names
        root = inputs.files[0]
        loc = locate(root)
        folder = loc.home / f'{loc.name}{CORRECTED_SUFFIX}'
        working = folder / root.name if (folder / root.name).is_file() else root
        ddf = None
        if self.board_ddf is not None:
            ddf = folder / self.board_ddf.name if (folder / self.board_ddf.name).is_file() else self.board_ddf
        try:
            offending, new = plan_new_names(working, str(ddf) if ddf else None)
        except Exception as e:                      # an unreadable file: said, nothing done
            self.write_log(f'ERROR: the designators could not be read: {e}')
            return
        if not offending:
            self.write_log('Designators: every one ends in a digit -- nothing to fix, nothing written.')
            return
        names = sorted({o.refdes for o in offending}, key=_natural)
        pairs = [f'{o} -> {new[o]}' if new.get(o) else f'{o} (left as it is)' for o in names]
        shown = ', '.join(pairs[:5]) + ('   (the first 5; the rest in the log)' if len(pairs) > 5 else '')
        what = 'the schematic and its DDF' if self.board_ddf is not None else 'the schematic'
        text = (f"{len(names)} designator(s) KiCad won't accept as annotated are renamed:\n  {shown}\n\n"
                f"Renamed copies of {what} are written to\n  {folder}\n"
                f"The originals stay unchanged and nothing is converted. Convert reads these copies "
                f"from then on.")
        if not self.confirm(RENAME_ONLY[:-1], text):
            return
        chosen = self.chosen()
        self.run_in_thread('Fixing designators…',
                           lambda: cli.convert(inputs, chosen, fix_only=True, asking=_WindowAsking(self)),
                           self._fixed)

    def _fixed(self, code) -> None:
        if code == 0:
            self.write_log('Designators fixed: the corrected copies written, nothing converted.', 'ok')
            self.status.set('Designators fixed')
        else:
            self.status.set('Cancelled' if cli.CANCELLED in self._run_lines else 'Failed -- see the log')

    def review_pins(self) -> None:
        """KIUC's review of the pin labels the last conversion corrected."""
        if self.review is None:
            return
        dialogs._PinOverrideReviewDialog(self, *self.review)

    def dump(self, symbols: bool) -> None:
        """KIUC's Dump / Inspect (symbols: Dump Symbols) of the schematic, to
        the log -- and to <out>/<name>_sheets.log (_symbols.log) when the
        output folder is there."""
        if self.inputs is None or self.inputs.kind not in ('schematic', 'utsch') or self.running:
            return
        files, utsch = list(self.inputs.files), self.inputs.kind == 'utsch'
        out, name = self.output_folder(), self.design_name

        def done(code):
            if code != 0:
                self.status.set('Failed -- see the log')
                return
            self.status.set('Dump done')
            if out is not None and out.is_dir():
                log = out / f'{name}_{"symbols" if symbols else "sheets"}.log'
                try:
                    log.write_text('\n'.join(self._run_lines) + '\n', encoding='utf-8')
                except OSError as e:
                    self.write_log(f'WARNING: could not write the log {log}: {e}')
        self.run_in_thread('Reading…', lambda: dump_schematic(files, utsch, symbols), done)

    def open_in_viewer(self) -> None:
        """The schematic in the viewer (kiut_viewer.py), a process of its own:
        the design's working copy -- its corrected copy when there is one, as
        a conversion reads it. Never the viewer's --v5, which rewrites the
        file it is given: the V5 header is set in the corrected copies."""
        if self.inputs is None or self.inputs.kind not in ('schematic', 'utsch'):
            return
        from kiut.core.corrected import CorrectedFolder, locate
        original = self.inputs.files[0]
        try:
            shown = CorrectedFolder(locate(original), 'KIUT').working(original)
        except Exception:                       # no readable corrected folder: the original
            shown = original
        shown = Path(shown) if shown and Path(shown).is_file() else original
        from kiut.core import handoff
        if self.own_viewer:                         # KIUT's own viewer, still open: it shows it
            reply = handoff.offer(handoff.viewer_channel(self.own_viewer), [shown], name=self.handoff_name)
            if reply in (handoff.OPENED, handoff.SHOWN):
                self.write_log(f'Viewer: {shown}' if shown != original else f'Viewer: {original.name}')
                return
            self.own_viewer = None                  # closed: a new one becomes KIUT's own
        viewer_id = handoff.new_viewer_id()
        try:
            # its own process, its output not in the window's console (the viewer shows what it has
            # to say itself; stage 10d); told its channel, so it is KIUT's own viewer
            no_window = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            subprocess.Popen([sys.executable, str(APP_DIR / VIEWER), '--channel', viewer_id, str(shown)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=no_window)
        except OSError as e:
            messagebox.showerror('Open in Viewer', f'The viewer could not be started:\n{e}', parent=self)
            return
        self.own_viewer = viewer_id
        self.write_log(f'Viewer: {shown}' if shown != original else f'Viewer: {original.name}')
        if (self.values['schematic']['v5_header'] and self.inputs.kind == 'schematic'
                and shown == original):
            self.write_log('NOTE: SCH V5.x header: the viewer shows the file with its own header until the '
                           'design is converted (the corrected copies then have the V5 header).')

    def find_kicad_cli(self) -> Optional[Path]:
        """KiCad's command-line program: next to the KiCad program the window
        knows, else on the PATH, else where KiCad installs it."""
        name = 'kicad-cli.exe' if sys.platform.startswith('win') else 'kicad-cli'
        if self.kicad_exe:
            p = Path(self.kicad_exe).with_name(name)
            if p.is_file():
                return p
        found = shutil.which('kicad-cli')
        if found:
            return Path(found)
        candidates: List[Path] = []
        if sys.platform.startswith('win'):
            for root in (os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('ProgramW6432', '')):
                if root:
                    candidates += sorted(Path(root).glob('KiCad/*/bin/kicad-cli.exe'), reverse=True)
        elif sys.platform == 'darwin':
            candidates.append(Path('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli'))
        return next((c for c in candidates if c.is_file()), None)

    def _ulticap_netlist(self) -> Optional[Path]:
        """Ulticap's netlist of the design: <name>.NET (any case) in the
        folder of its Ulticap files."""
        from kiut.core.corrected import locate
        loc = locate(self.inputs.files[0])
        for p in sorted(loc.home.iterdir()) if loc.home.is_dir() else []:
            if p.suffix.lower() == '.net' and p.stem.lower() == loc.name.lower() and p.is_file():
                return p
        return None

    def compare_netlists(self) -> None:
        """The converted schematic's netlist (kicad-cli; else a KiCad .net
        picked) against Ulticap's (<name>.NET; else one picked): Netlist
        Compare's report, <out>/<name>_netlist_compare.txt, its counts in the
        log."""
        sch = self._converted_schematic()
        if sch is None or self.running:
            if sch is None:
                messagebox.showinfo('Compare netlists', 'Convert the schematic first: its KiCad netlist is '
                                                        'compared.', parent=self)
            return
        ulticap = self._ulticap_netlist()
        if ulticap is None:
            name = filedialog.askopenfilename(
                parent=self, title=f"Ulticap's netlist of {self.design_name}",
                initialdir=str(self.inputs.files[0].parent),
                filetypes=[('Netlist', '*.net *.NET'), ('All files', '*.*')])
            if not name:
                return
            ulticap = Path(name)
        kicad_cli = self.find_kicad_cli()
        out, name = sch.parent, self.design_name
        from kiut.core import netlist
        try:
            renames = netlist.designator_renames(self.inputs.files[0], self.board_ddf)
        except Exception as e:                      # a design that can't be read: compared as it is
            self.write_log(f'WARNING: the designator fix\'s renames could not be read ({e}); compared '
                           f'without them.')
            renames = {}
        net, report = out / f'{name}.net', out / f'{name}_netlist_compare.txt'

        def work():
            print(f'Ulticap netlist: {ulticap}')
            if renames:
                print("Ulticap's designators read as the designator fix renamed them: "
                      + ', '.join(f'{o} -> {n}' for o, n in sorted(renames.items())))
            kicad_net = None
            if kicad_cli is not None:
                cmd = [str(kicad_cli), 'sch', 'export', 'netlist', '--output', str(net), str(sch)]
                print(f'KiCad netlist: {kicad_cli.name} sch export netlist -> {net}')
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                    if r.returncode == 0 and net.is_file():
                        kicad_net = net
                    else:
                        print(f'WARNING: kicad-cli failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}')
                except (OSError, subprocess.TimeoutExpired) as e:
                    print(f'WARNING: kicad-cli could not be run: {e}')
            else:
                print("NOTE: KiCad's kicad-cli was not found (KiCad 7 or later, next to KiCad's program): "
                      "pick the KiCad netlist (exported from KiCad's schematic editor).")
            if kicad_net is None:
                picked = self.on_main(lambda: filedialog.askopenfilename(
                    parent=self, title=f'The KiCad netlist of {name}', initialdir=str(out),
                    filetypes=[('KiCad netlist', '*.net'), ('All files', '*.*')]))
                if not picked:
                    print('Compare netlists cancelled.')
                    return 1
                kicad_net = Path(picked)
            text = netlist.build_report(str(kicad_net), str(ulticap), renames)
            report.write_text(text, encoding='utf-8')
            for line in text.splitlines()[8:13]:              # the counts
                bad = line.split(':')[0].endswith(('missing in KiCad', 'not in Ulticap')) and not line.endswith(': 0')
                print(('WARNING: ' if bad else '') + line)
            print(f'Report: {report}')
            return 0

        def done(code):
            self.status.set('Netlists compared' if code == 0 else 'Compare netlists: not done -- see the log')
        self.run_in_thread('Comparing netlists…', work, done)

    # ── File and Help ──
    def save_log(self) -> None:
        out = self.output_folder()
        name = filedialog.asksaveasfilename(
            parent=self, title='Save the log', defaultextension='.log',
            initialdir=str(out) if out is not None and out.is_dir() else (self.last_folder or None),
            initialfile=f'{self.design_name or "kiut"}.log',
            filetypes=[('Log', '*.log'), ('Text', '*.txt'), ('All files', '*.*')])
        if not name:
            return
        try:
            Path(name).write_text(self.log.get('1.0', 'end-1c'), encoding='utf-8')
        except OSError as e:
            messagebox.showerror('Save log', f'Could not write {name}:\n{e}', parent=self)

    def open_doc(self, path: Path) -> None:
        """A document as a web page in the browser: every document rendered
        into the temporary folder's kiut_docs, each time (stage 10d)."""
        from kiut.core import docs_html
        try:
            docs_html.open_document(APP_DIR, str(Path(path).resolve().relative_to(APP_DIR.resolve())))
        except Exception as e:                      # the documents can't be rendered or opened
            messagebox.showinfo('KIUT', f'{path} could not be shown as a web page ({e}).', parent=self)

    def about(self) -> None:
        from kiut import CREDIT, REPOSITORY, VERSION
        messagebox.showinfo('About KIUT', f'KIUT {VERSION} \u2014 KiCad Ulticap/Ultiboard import tools\n\n'
                                          'Ulticap schematics and libraries and Ultiboard boards and '
                                          'libraries to KiCad 9 and later.\n\n'
                                          f'{CREDIT}\n\n{REPOSITORY}\n\nCopyright (C) 2026 Snieffy\n'
                                          'GNU General Public License version 3 or later.', parent=self)

    def _old_settings_files(self) -> List[Path]:
        return [self.settings_file.parent / n for n in OLD_SETTINGS if (self.settings_file.parent / n).is_file()]

    def _offer_import(self) -> None:
        """At the first start (no kiut.ini yet), KIUC's and KIUB's settings
        files next to kiut.py: offered once."""
        if self.settings_file.exists():
            return
        old = self._old_settings_files()
        if not old:
            return
        if messagebox.askyesno('Import old settings', 'Import the settings of KIUC and KIUB ('
                               + ', '.join(p.name for p in old) + ')?\n\nThey are read, not changed. '
                               'Later: File \u2192 Import old settings\u2026', parent=self):
            self.import_old(old)
        try:
            self.save_window()                      # asked once: kiut.ini exists from now on
        except OSError:
            pass

    def import_settings(self) -> None:
        names = filedialog.askopenfilenames(
            parent=self, title="KIUC's or KIUB's settings file (kiuc.ini, kiub_gui.ini)",
            initialdir=str(self.settings_file.parent),
            filetypes=[('Settings', '*.ini'), ('All files', '*.*')])
        if names:
            self.import_old([Path(n) for n in names])

    def import_old(self, paths: List[Path]) -> None:
        """KIUC's (kiuc.ini) or KIUB's (kiub_gui.ini) window settings taken
        as the current ones: the font, KIUC's fine tuning, KIUB's board
        defaults and fine tuning and its per-font text ratios, KiCad's path,
        Show tooltips, KIUB's last technology. The files are left as they
        are; what was taken is logged."""
        from kiut.core import settings as old_ini
        values = _copy.deepcopy(self.values)
        known = {(st.section, st.key): st for st in kset.settings()}
        for path in paths:
            cfg = old_ini.load(path, keep_case=True)
            board = 'kiub' in path.name.lower() or cfg.has_section('board_defaults') or cfg.has_section('fine_tuning')
            side = 'board' if board else 'schematic'
            taken: List[str] = []

            def take(section: str, key: str, text: str) -> None:
                st = known.get((section, key))
                if st is None:
                    return
                try:
                    values[section][key] = kset._value_of(st, text)
                    taken.append(f'{key}')
                except ValueError:
                    pass
            if cfg.has_option('last_used', 'font'):
                take(side, 'font', cfg.get('last_used', 'font'))
            for sec in (('tuning',) if not board else ('board_defaults', 'fine_tuning')):
                if cfg.has_section(sec):
                    held = {k.lower() for k in cfg.options(sec)}
                    for key, text in cfg.items(sec):
                        for new in kset.split_old(side, key.lower(), text, held.__contains__):
                            take(side, new, text)     # KIUC's one text-height multiplier
                        take(side, key.lower(), text)
            if board and cfg.has_section('font_ratios'):
                for font, text in cfg.items('font_ratios'):
                    try:
                        h, w = text.split('|')
                        self.ratios[font] = (float(h), float(w))
                    except ValueError:
                        continue
                taken.append(f'the text ratios of {len(cfg.items("font_ratios"))} font(s)')
            if board and cfg.has_section('technology'):
                t = dict(cfg.items('technology'))
                if t.get('mode') in ('single', 'multi'):
                    self.technology = {'mode': t['mode'], **{k: t.get(k, '') for k in TECH_KEYS}}
                    taken.append('the last technology')
            exe = cfg.get('kicad', 'executable', fallback='')
            if exe and not self.kicad_exe:
                self.kicad_exe = exe
                taken.append("KiCad's path")
            if cfg.has_option('ui', 'show_tooltips'):
                self.tooltips_on = cfg.get('ui', 'show_tooltips').strip().lower() not in ('no', 'false', '0', 'off')
                self.tooltips_var.set(self.tooltips_on)
                taken.append('Show tooltips')
            self.write_log(f'Imported from {path}: ' + (', '.join(taken) if taken else 'nothing KIUT uses') + '.')
        font = values['board']['font']
        if font in self.ratios:
            values['board']['font_height_ratio'], values['board']['font_width_ratio'] = self.ratios[font]
        self.values = values
        self._show_values()
        self._show_output()
        self._changed_by_hand()

    # ── KiCad ──
    def kicad_target(self) -> Optional[Path]:
        """What Open in KiCad opens: the last conversion's project, else the
        design's project from an earlier conversion."""
        if self.last_project is not None and self.last_project.is_file():
            return self.last_project
        out = self.output_folder()
        if out is None:
            return None
        places = [out]
        if self.inputs is not None and self.inputs.kind == 'board':   # a DDF that turned out a library
            from kiut.board import kiub
            places.append(Path(kiub.default_output_dir(str(self.inputs.files[0]), True)))
            fp = folders.library_folder('footprints')
            if fp is not None:
                places.append(fp / (kiub.design_name(str(self.inputs.files[0])) + kiub.LIBRARY_FOLDER_SUFFIX))
        for folder in places:
            for p in (folder / f'{self.design_name}.kicad_pro', folder / f'{self.design_name}_lib.kicad_pro'):
                if p.is_file():
                    return p
        return None

    def open_in_kicad(self) -> None:
        """The project in KiCad (its project manager, which .kicad_pro files
        open with): with KiCad's saved path, else the program the system
        opens .kicad_pro with; only when both fail, KiCad asked for (saved)."""
        target = self.kicad_target()
        if target is None and self.inputs is not None and self.inputs.kind == 'libraries':
            messagebox.showinfo('Nothing to open', 'Symbol libraries make no KiCad project: their '
                                '.kicad_sym files are in the output folder, listed in its sym-lib-table.',
                                parent=self)
            return
        if target is None:
            messagebox.showinfo('Nothing to open yet',
                                'Convert the design first: Open in KiCad opens its KiCad project.', parent=self)
            return
        if self.kicad_exe and Path(self.kicad_exe).is_file() and common.launch(self.kicad_exe, target):
            return
        if common.open_with_system(target):
            return
        while True:
            exe = common.browse_kicad_exe(self)
            if not exe:
                return
            self.kicad_exe = exe
            try:
                self.save_window()
            except OSError:
                pass
            if common.launch(exe, target):
                return
            messagebox.showerror('Could not start KiCad', f'{exe} could not be started.', parent=self)

    def choose_kicad(self) -> None:
        """File → KiCad program…: the KiCad Open in KiCad starts (several
        versions may be installed side by side), or none -- the system's
        choice for .kicad_pro files (stage 10e)."""
        current = self.kicad_exe or ("None chosen: a project opens with the program the system opens "
                                     ".kicad_pro files with.")
        dlg = dialogs.PathDialog(
            self, 'KiCad program',
            'The KiCad that Open in KiCad starts. With several KiCad versions installed side by side, '
            'choose the one to use: its kicad.exe (Windows), its kicad program (Linux, macOS). Without '
            'one, a project opens with the program the system opens .kicad_pro files with.',
            current, common.browse_kicad_exe, "Use the system's choice")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        self.kicad_exe = dlg.result
        try:
            self.save_window()
        except OSError:
            pass
        self.write_log(f'KiCad program: {self.kicad_exe}' if self.kicad_exe else
                       "KiCad program: the system's choice for .kicad_pro files.")

    def choose_library_folder(self) -> None:
        """File → Library output folder…: KIUT's library folder, where
        library-only runs go (kiut.core.folders), or the default again
        (stage 10e)."""
        root = folders.library_root()
        current = str(root) + ('' if self.library_folder_text else '  (the default)')
        if os.environ.get(folders.ENV, '').strip():
            current += f'\n{folders.ENV} is set: it is used instead of the folder chosen here.'

        def choose(parent) -> str:
            start = root if root is not None and root.is_dir() else Path.home()
            return filedialog.askdirectory(parent=parent, title='KIUT library folder',
                                           initialdir=str(start), mustexist=False) or ''
        dlg = dialogs.PathDialog(
            self, 'Library output folder',
            'Where library-only conversions go, side by side, to be copied to KiCad together: Ulticap '
            'symbol libraries into its Symbols folder, block libraries into Blocks, Ultiboard libraries '
            'into Footprints (each with its overview board in <name>_lib_KiCad) -- created when needed. '
            'A design, and the libraries made from it, stays next to its files.',
            current, choose, f'Use the default ({folders.default_root()})')
        self.wait_window(dlg)
        if dlg.result is None:
            return
        self.library_folder_text = str(Path(dlg.result)) if dlg.result else ''
        try:
            self.save_window()
        except OSError:
            pass
        self.write_log(f'Library output folder: {folders.library_root()}'
                       + ('' if self.library_folder_text else ' (the default)'))
        self._show_output()
        self._update_state()

    # ── exit ──
    def on_exit(self) -> None:
        if self.running:
            if not messagebox.askyesno('KIUT', 'A conversion is still running. Close anyway?', parent=self):
                return
        elif (self.converted_since_save and self.design_file is not None and self.design_file.is_file()
              and kset.read(self.design_file)[0] != self.values):
            answer = messagebox.askyesnocancel(
                'KIUT', "The current settings differ from the design's own. Save them as the design's "
                        f"settings?\n\n{self.design_file}", parent=self)
            if answer is None:
                return
            if answer:
                kset.write(self.design_file, self.values)
        self.stop_handoff()
        self.geometry_text = self.geometry()
        try:
            self.save_window()
        except OSError:
            pass
        self.destroy()


def _in_window_words(work: Callable[[], object]):
    """work(), its report naming the switches as the window shows them
    (cli.WINDOW_WORDS, kiub.TOGETHER_HINT); kiut.py's words again after."""
    from kiut.board import kiub
    saved = cli.WINDOW_WORDS, kiub.TOGETHER_HINT
    cli.WINDOW_WORDS = dict(SCHEMATIC_SWITCHES)
    kiub.TOGETHER_HINT = ("choose {sch}, with its board ticked and '" + dict(SCHEMATIC_SWITCHES)['fix_designators']
                          + "' on")
    try:
        return work()
    finally:
        cli.WINDOW_WORDS, kiub.TOGETHER_HINT = saved


def _not_what_it_says(inputs: cli.Inputs) -> str:
    """A schematic file whose content isn't what its extension says (a .UTSCH
    named .SCH, ...): why, as KIUC's window said it when a file was chosen
    (the conversion refuses it too); '' when every file is."""
    from kiut.schematic.kiuc_ascii import looks_like_sch
    from kiut.schematic.kiuc_binary import looks_like_utsch
    files = list(inputs.files) if inputs.kind in ('schematic', 'utsch', 'design') else []
    for f in files:
        utsch = f.suffix.upper() == '.UTSCH'
        if not (looks_like_utsch(f) if utsch else looks_like_sch(f)):
            kind = 'a .UTSCH container' if utsch else 'an Ulticap .SCH/.BLK file'
            return f'{f.name} does not look like {kind} -- wrong file, or not Ulticap data at all.'
    return ''


def _set_enabled(widget, enabled: bool) -> None:
    """A widget, or every widget in a frame, enabled or greyed out."""
    children = widget.winfo_children()
    if children:
        for c in children:
            _set_enabled(c, enabled)
    try:
        if isinstance(widget, ttk.Combobox):
            widget.state(['!disabled', 'readonly'] if enabled else ['disabled'])
        elif isinstance(widget, (ttk.Widget,)):
            widget.state(['!disabled'] if enabled else ['disabled'])
    except tk.TclError:
        pass


# ── Advanced ─────────────────────────────────────────────────────────────────

# The settings in the main window (and tec_split: the window asks for a library's technology):
# not in the Advanced dialog.
MAIN_SETTINGS = ({('schematic', 'font'), ('board', 'font'), ('board', 'font_height_ratio'),
                  ('board', 'font_width_ratio'), ('board', 'library_only'),
                  ('board', 'v2v3_staircase_merge'), ('board', 'tec_split')}
                 | {('schematic', key) for key, _ in SCHEMATIC_SWITCHES})


def advanced_groups() -> list:
    """The Advanced dialog's tabs: [(tab, [(heading, [Setting, ...]), ...]), ...]
    -- KIUC's fine-tuning headings and KIUB's conversion-settings headings,
    every setting not in the main window once."""
    from kiut.board.kiub import BOARD_DEFAULTS_SPEC, FINE_TUNING_SPEC
    from kiut.schematic.kiuc_writer import TUNING_SPEC
    cats = {name.lower(): cat for name, _, _, _, _, cat in TUNING_SPEC}
    cats.update({name: cat for name, _, _, _, _, cat in FINE_TUNING_SPEC})
    defaults = {name for name, *_ in BOARD_DEFAULTS_SPEC}
    staircases = ('v2v3_staircase_limit_mil', 'v2v3_chamfer', 'v2v3_corner_slant_limit_mil')
    shown = [st for st in kset.settings() if (st.section, st.key) not in MAIN_SETTINGS]

    def pick(section, test):
        return [st for st in shown if st.section == section and test(st.key)]
    return [
        ('Schematic', [
            ('Net labels', pick('schematic', lambda k: k == 'label_keep_alignment')),
            ('Empirically-tuned values', pick('schematic', lambda k: cats.get(k) == 'empirical')),
            ('Ulticap DOS line widths', pick('schematic', lambda k: cats.get(k) == 'ulticap')),
            ('KiCad project (new projects only)', pick('schematic', lambda k: cats.get(k) == 'kicad'))]),
        ('Board', [
            ('Board defaults', pick('board', lambda k: k in defaults)),
            ('Geometry', pick('board', lambda k: cats.get(k) == 'geometry')),
            ('Fallback', pick('board', lambda k: cats.get(k) == 'clearance')),
            ('V2/V3 staircases', pick('board', lambda k: k in staircases))]),
    ]


class AdvancedDialog(tk.Toplevel):
    """The settings not in the main window, in two tabs, Schematic and
    Board, under KIUC's and KIUB's headings: each by its name in the
    settings file (its description as its tooltip), its value, its default
    and its suggested range. result: the new values (None: cancelled); notes:
    (level, text) for numbers outside their suggested range (used as
    given)."""

    def __init__(self, parent: App, values: dict, source: str) -> None:
        super().__init__(parent)
        self.title(f'Advanced settings \u2014 {source}')
        self.transient(parent)
        self.geometry('660x620')
        self.result: Optional[dict] = None
        self.notes: List[tuple] = []
        self._values = values
        self._vars: Dict[tuple, tk.Variable] = {}
        self._shown: List = []
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        for title, groups in advanced_groups():
            nb.add(self._tab(nb, groups, parent), text=title)
        btns = ttk.Frame(self)
        btns.pack(fill='x', padx=8, pady=(0, 8))
        ttk.Button(btns, text='Reset to defaults', command=self._reset).pack(side='left')
        ttk.Button(btns, text='Cancel', command=self.destroy).pack(side='right')
        ttk.Button(btns, text='OK', command=self._ok).pack(side='right', padx=6)
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        common.center_over_parent(self, parent)
        self.grab_set()

    def _tab(self, nb, groups, app: App):
        outer = ttk.Frame(nb)
        canvas = tk.Canvas(outer, highlightthickness=0)
        sb = ttk.Scrollbar(outer, command=canvas.yview)
        f = ttk.Frame(canvas, padding=8)
        canvas.create_window(0, 0, window=f, anchor='nw')
        canvas.config(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        canvas.pack(fill='both', expand=True)
        f.bind('<Configure>', lambda e: canvas.config(scrollregion=canvas.bbox('all')))
        r = 0
        for c, head in ((1, 'Value'), (2, 'Default'), (3, 'Suggested range')):
            ttk.Label(f, text=head, foreground='#555').grid(row=r, column=c, sticky='w', padx=(0, 12))
        for heading, sts in groups:
            r += 1
            ttk.Label(f, text=heading, font=('TkDefaultFont', 9, 'bold')).grid(
                row=r, column=0, columnspan=4, sticky='w', pady=(10 if r > 1 else 2, 0))
            r += 1
            ttk.Separator(f).grid(row=r, column=0, columnspan=4, sticky='ew', pady=(2, 4))
            for st in sts:
                r += 1
                value = self._values[st.section][st.key]
                name = ttk.Label(f, text=st.key)
                name.grid(row=r, column=0, sticky='w', pady=1, padx=(0, 16))
                if st.kind == 'yes_no':
                    var: tk.Variable = tk.BooleanVar(value=bool(value))
                    w = ttk.Checkbutton(f, variable=var)
                elif st.kind == 'choice':
                    var = tk.StringVar(value=value)
                    w = ttk.Combobox(f, textvariable=var, values=st.choices, width=8, state='readonly')
                else:
                    var = tk.StringVar(value=kset.text_of(st, value))
                    w = ttk.Entry(f, textvariable=var, width=12)
                w.grid(row=r, column=1, sticky='w', pady=1, padx=(0, 12))
                default = kset.text_of(st, st.default) or ('(auto)' if st.kind == 'auto_number' else '')
                ttk.Label(f, text=default, foreground='#555').grid(row=r, column=2, sticky='w', padx=(0, 12))
                if st.low is not None or 'KiCad project' in st.group:
                    rng = (f'{st.low:g} to {st.high:g}' + (f' ({kset.KICAD_LIMIT})' if st.hard else '')
                           if st.low is not None else kset.KICAD_RANGE)
                    ttk.Label(f, text=rng, foreground='#555').grid(row=r, column=3, sticky='w')
                tip = st.text
                for widget in (name, w):
                    common.Tooltip(widget, tip, enabled=lambda: app.tooltips_on)
                self._vars[(st.section, st.key)] = var
                self._shown.append(st)
        return outer

    def _reset(self) -> None:
        for st in self._shown:
            var = self._vars[(st.section, st.key)]
            var.set(bool(st.default) if st.kind == 'yes_no' else
                    st.default if st.kind == 'choice' else kset.text_of(st, st.default))

    def _ok(self) -> None:
        values = _copy.deepcopy(self._values)
        errors, notes = [], []
        for st in self._shown:
            var = self._vars[(st.section, st.key)]
            if st.kind in ('yes_no', 'choice'):
                values[st.section][st.key] = var.get()
                continue
            try:
                value = kset._value_of(st, str(var.get()))
            except ValueError as e:
                errors.append(f'{st.key}: not {e}')
                continue
            if (st.kind in ('number', 'auto_number') and value is not None and st.low is not None
                    and not st.low <= value <= st.high):
                notes.append(('note', f'{st.key} = {value:g} is outside the suggested range '
                                      f'{st.low:g} to {st.high:g}; used as given.'))
            values[st.section][st.key] = value
        if errors:
            messagebox.showerror('Advanced settings', 'These values can\'t be read:\n\n' + '\n'.join(errors),
                                 parent=self)
            return
        self.result, self.notes = values, notes
        self.destroy()


class _Confirm(tk.Toplevel):
    """A confirmation: the text, then <ok> / Cancel (closing it: Cancel)."""

    def __init__(self, parent, title: str, text: str, ok: str) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.ok = False
        ttk.Label(self, text=text, wraplength=620, justify='left').pack(anchor='w', padx=12, pady=12)
        btns = ttk.Frame(self)
        btns.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(btns, text='Cancel', command=self.destroy).pack(side='right')
        b = ttk.Button(btns, text=ok, command=self._on_ok)
        b.pack(side='right', padx=6)
        b.focus_set()
        self.bind('<Escape>', lambda e: self.destroy())
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        common.center_over_parent(self, parent)
        self.grab_set()

    def _on_ok(self) -> None:
        self.ok = True
        self.destroy()


def dump_schematic(files: List[Path], utsch: bool, symbols: bool) -> int:
    """KIUC's Dump / Inspect (symbols: Dump Symbols) of a schematic, printed:
    a .UTSCH read whole, a .SCH with its sub-sheets (a .BLK by itself) --
    each file's corrected copy when it has one (what Convert reads, as
    KIUC's dumps did)."""
    from kiut.core.corrected import CorrectedFolder, locate
    from kiut.schematic import kiuc_writer as kw

    def working(p: Path) -> Path:
        w = CorrectedFolder(locate(p), 'KIUT').working(p)
        return w if w.is_file() else p
    from kiut.schematic.kiuc_ascii import collect_hierarchy_paths, parse_sch
    from kiut.schematic.kiuc_model import Schematic

    def write(text: str = '', *_tag) -> None:
        print(text)
    if utsch:
        from kiut.schematic import kiuc_binary_semantic as ks
        sch, warnings = ks.read_schematic_file(working(files[0]))
        for w in warnings:
            print(f'WARNING: {w}')
    else:
        found: List[Path] = []
        for f in files:
            for p in (collect_hierarchy_paths(f) if f.suffix.upper() == '.SCH' else [f]):
                if p not in found:
                    found.append(p)
        sheets, seen_errors = [], set()
        for f in found:
            sheet, errors = parse_sch(working(f))
            for e in errors:
                if str(e) not in seen_errors:
                    seen_errors.add(str(e))
                    print(f'WARNING: {e}')
            sheets.append(sheet)
        sch = Schematic(sheets=sheets)
    if symbols:
        kw.dump_symbols(sch, write)
        if not utsch:
            for w in kw.check_pkg_type_consistency(sch.sheets):
                print(f'WARNING: {w}')
            for w in kw.check_slash_symbol_refdes(sch.sheets):
                print(w)
    else:
        for w in kw.check_missing_sheets(sch.sheets):
            print(f'WARNING: {w}')
        kw.dump_schematic(sch, write)
    return 0


def main(argv: Optional[List[str]] = None) -> None:
    """The window; files given: opened in it. One KIUT window: when one is open
    already, the files are handed to it (it comes to the front) and this start
    ends."""
    argv = list(sys.argv[1:] if argv is None else argv)
    viewer = None
    if len(argv) >= 2 and argv[0] == '--viewer':    # started by a viewer's Open in KIUT…: its own viewer
        viewer, argv = argv[1], argv[2:]
    from kiut.core import handoff
    reply = handoff.offer(handoff.WINDOW, [Path(a).resolve() for a in argv], sender=viewer)
    if reply in (handoff.OPENED, handoff.SHOWN):
        return
    if reply is not None:                           # busy, or it couldn't take them
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(TITLE, handoff.BUSY_TEXT if reply == handoff.BUSY
                            else f'The open KIUT window could not take the files:\n{reply}', parent=root)
        root.destroy()
        return
    app = App()
    app.own_viewer = viewer
    app.start_handoff()
    if argv:
        app.open_files(argv)
    try:
        app.mainloop()
    finally:
        app.stop_handoff()


if __name__ == '__main__':
    main()
