# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/gui/common.py -- What KIUC's and KIUB's windows do alike (Tk)

- center_over_parent: a dialog centred over its window, on the screen;
- browse_kicad_exe, launch, open_with_system: KiCad found and started,
  for Open in KiCad (each window keeps its own sequence: its saved path,
  else the system's association, else it asks);
- is_monospaced, installed_fonts, ordered_fonts, next_font: the Text font
  list (only what the list offers can be chosen; the pinned fonts first,
  a divider, every other installed font; a letter jumps to the next font
  starting with it);
- Tooltip: the hover help of every window (the author, 2026-10-31: after 0.5 s,
  hidden on leaving or a click, Segoe UI 9, wrapped at 420 px);
- OverwriteDialog: the question before replacing KiCad files (KIUC's
  layout, the author 2026-10-31: the folder, the files, Overwrite / Cancel
  conversion well apart; one statement per line, no closing question).

Each window binds these under its own names (_launch, _center_over_parent,
...), which its tests replace. Until KIUT stage 4 each window had its own
copy.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog
from tkinter import ttk
from typing import Callable, Iterable, List, Optional, Sequence


def center_over_parent(win: tk.Toplevel, parent: tk.Misc) -> None:
    """Position win centred over parent, clamped to stay fully on-screen.

    Tkinter's default placement for a new Toplevel is the window manager's
    own (often the screen's top-left corner), not the parent's: transient()
    ties stacking and lifetime to the parent, not placement. Call it once
    the dialog's widgets are built (update_idletasks gives its real size).
    A parent whose geometry can't be read: the screen's top-left corner.
    """
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        x, y = px + (pw - w) // 2, py + (ph - h) // 2
    except tk.TclError:
        x, y = 0, 0
    # Clamp so the dialog never opens partially off-screen -- e.g. when the
    # parent window itself sits close to a screen edge.
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x = max(0, min(x, sw - w))
    y = max(0, min(y, sh - h))
    win.geometry(f'+{x}+{y}')


def browse_kicad_exe(parent: Optional[tk.Misc] = None) -> str:
    """A file browser to locate the KiCad executable: its path, or '' when
    cancelled."""
    if sys.platform.startswith('win'):
        filetypes = [('Executable', '*.exe'), ('All files', '*.*')]
    else:
        filetypes = [('All files', '*')]
    path = filedialog.askopenfilename(
        parent=parent,
        title='Locate the KiCad executable (kicad or kicad.exe)',
        filetypes=filetypes,
    )
    return str(Path(path)) if path else ''


def launch(exe: str, target: Path) -> bool:
    """Start KiCad with target; whether it started."""
    try:
        subprocess.Popen([exe, str(target)])
        return True
    except OSError:
        return False


def open_with_system(target: Path) -> bool:
    """Open target with the program the system associates with its type
    (KiCad, for a .kicad_pro, once KiCad is installed). Whether that
    worked: on Windows and macOS a missing association is an error; on
    Linux xdg-open reports one by its exit code."""
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(target))       # type: ignore[attr-defined]  # Windows only
            return True
        cmd = ['open', str(target)] if sys.platform == 'darwin' else ['xdg-open', str(target)]
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return True                         # still busy starting it
    except (OSError, AttributeError):
        return False


# A divider row in the font list, never a font.
FONT_SEPARATOR = '\u2500' * 24


def is_monospaced(family: str) -> bool:
    """Whether every character of a font family has one advance width: a
    narrow 'i' and a wide 'W' measured equal at a neutral size. A family
    Tk can't render counts as not."""
    try:
        f = tkfont.Font(family=family, size=12)
        return f.measure('i') == f.measure('W')
    except Exception:
        return False


def installed_fonts(root: Optional[tk.Misc] = None) -> List[str]:
    """The installed font families, sorted, each once: no blank names, no
    vertical '@' faces (Windows lists each CJK font twice)."""
    families = tkfont.families(root) if root is not None else tkfont.families()
    return sorted({f for f in families if f.strip() and not f.startswith('@')})


def ordered_fonts(default: str, pinned: Iterable[str], fonts: Sequence[str]) -> List[str]:
    """The font list as shown: the default (KiCad's own font) first, the
    pinned fonts that are installed (in their order), the divider, then
    every other font as given."""
    present = [f for f in pinned if f in fonts]
    rest = [f for f in fonts if f not in present and f != default]
    return [default] + present + [FONT_SEPARATOR] + rest


def next_font(values: Sequence[str], current: str, ch: str) -> Optional[str]:
    """The next font after current (round to the start) starting with the
    letter ch, the divider skipped; None when there is none."""
    values = [v for v in values if v != FONT_SEPARATOR]
    if not values:
        return None
    start = values.index(current) + 1 if current in values else 0
    for v in values[start:] + values[:start]:
        if v.lower().startswith(ch):
            return v
    return None


class MessageBlock(tk.Frame):
    """The KIUT window's messages about the files chosen (stage 10d, the author): a
    block of LINES lines that is always there, so the section around it
    doesn't grow and shrink, framed like the window's other sections with its
    title in the border line -- ERROR, WARNING or NOTE after the most serious
    message, the frame, title and text in that level's colour; every line in
    its own level's colour. Empty: a faint frame, no title. The frame is drawn
    here: Tk's own framed sections can't take a colour. Messages that don't
    fit end in MORE (the window's log has them all)."""

    LEVELS = ('error', 'warn', 'note')
    COLOURS = {'error': '#b00000', 'warn': '#8a5300', 'note': '#1f4e79'}
    TITLES = {'error': 'ERROR', 'warn': 'WARNING', 'note': 'NOTE'}
    FAINT = '#b9b9b9'
    LINES = 3
    MORE = '(more in the log)'

    def __init__(self, parent) -> None:
        bg = ttk.Style().lookup('TLabelframe', 'background') or ttk.Style().lookup('TFrame', 'background') \
            or parent.winfo_toplevel().cget('background')
        super().__init__(parent, background=bg)
        self.bg = bg
        self.font = tkfont.nametofont('TkDefaultFont')
        self.title_font = self.font.copy()
        self.title_font.configure(weight='bold')
        line = self.font.metrics('linespace')
        self.top = self.title_font.metrics('linespace')
        self.canvas = tk.Canvas(self, background=bg, highlightthickness=0,
                                height=self.top + self.LINES * line + 10)
        self.canvas.pack(fill='x', expand=True)
        self.text = tk.Text(self.canvas, height=self.LINES, wrap='word', background=bg, relief='flat',
                            borderwidth=0, highlightthickness=0, font=self.font, cursor='arrow',
                            takefocus=0)
        for level in self.LEVELS:
            self.text.tag_configure(level, foreground=self.COLOURS[level])
        self.text.configure(state='disabled')
        self.messages: list = []
        self.level: Optional[str] = None
        self._window = self.canvas.create_window(8, self.top + 2, window=self.text, anchor='nw')
        self.canvas.bind('<Configure>', lambda e: self._draw())

    def set(self, messages) -> None:
        """messages: [(level, text)], level 'error', 'warn' or 'note';
        shown most serious first."""
        order = {lv: i for i, lv in enumerate(self.LEVELS)}
        self.messages = sorted(messages, key=lambda m: order[m[0]])
        self.level = self.messages[0][0] if self.messages else None
        t = self.text
        t.configure(state='normal')
        t.delete('1.0', 'end')
        for i, (level, text) in enumerate(self.messages):
            t.insert('end', ('\n' if i else '') + text, level)
        t.configure(state='disabled')
        self._draw()

    def shown_text(self) -> str:
        return self.text.get('1.0', 'end-1c')

    def _draw(self) -> None:
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1:
            return
        c.delete('frame')
        colour = self.COLOURS[self.level] if self.level else self.FAINT
        y = self.top // 2
        c.create_rectangle(1, y, w - 2, h - 2, outline=colour, tags='frame')
        if self.level:
            t = c.create_text(10, y, text=f' {self.TITLES[self.level]} ', anchor='w', fill=colour,
                              font=self.title_font, tags='frame')
            x0, y0, x1, y1 = c.bbox(t)
            c.tag_lower(c.create_rectangle(x0, y0, x1, y1, fill=self.bg, outline=self.bg, tags='frame'), t)
        c.itemconfigure(self._window, width=max(10, w - 16))
        c.tag_raise(self._window)
        self._fit()

    def _fit(self) -> None:
        """Lines that don't fit: the last shown one says MORE instead (only
        once the block is on screen: before, Tk can't tell what fits)."""
        t = self.text
        if not t.winfo_ismapped() or t.winfo_height() < 10 or not self.messages:
            return
        try:
            shown = t.count('1.0', 'end-1c', 'displaylines')
        except tk.TclError:
            return
        n = (shown[0] if isinstance(shown, tuple) else shown) or 0
        if n + 1 > self.LINES and not self.shown_text().endswith(self.MORE):
            last = t.index(f'@0,{t.winfo_height() - 2} display linestart')
            t.configure(state='normal')
            t.delete(f'{last} -1c', 'end')
            t.insert('end', '\n' + self.MORE, self.level)
            t.configure(state='disabled')


class Tooltip:
    """A delayed hover tooltip for one widget: shows DELAY_MS after the
    mouse enters (no flicker while just passing over), hides when it leaves
    or on a click; text: a string, or a callable asked each time. The widget's other <Enter>/<Leave>/<ButtonPress>
    bindings are kept. enabled: whether tooltips are switched on (a
    window's Show tooltips), asked each time; None: always."""

    DELAY_MS = 500

    def __init__(self, widget: tk.Widget, text,
                 enabled: Optional[Callable[[], bool]] = None) -> None:
        self._widget = widget
        self._text = text
        self._enabled = enabled
        self._after_id: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self._widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            self._widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if self._tip is not None or (self._enabled is not None and not self._enabled()):
            return
        x = self._widget.winfo_rootx()
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f'+{x}+{y}')
        tk.Label(
            self._tip, text=self._text() if callable(self._text) else self._text, justify='left',
            background='#ffffe0', relief='solid', borderwidth=1,
            font=('Segoe UI', 9), wraplength=420, padx=6, pady=4,
        ).pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class OverwriteDialog(tk.Toplevel):
    """Modal dialog shown before a conversion when KiCad files it would
    write already exist in the output folder -- the last pre-conversion
    check. The output folder is always the design's own (<name>_KiCad, or
    <name>_lib_KiCad), so existing files there are normally a previous
    conversion of the same input, or the other tool's files of the same
    design. Overwriting or cancelling are the only choices: to keep the
    files, the user copies them elsewhere first. The .kicad_pro is updated
    in place, never replaced.

    what names the files ('KiCad', 'KiCad library', 'KiCad block'); folder
    is where they are (default: the first one's). Sets self.choice to
    'continue' (overwrite) or 'cancel' (also on closing it) before closing.
    """
    _MAX_LISTED = 12

    def __init__(self, parent, existing: list, what: str = 'KiCad', folder=None):
        super().__init__(parent)
        self.title('Overwrite existing files?')
        self.transient(parent)
        self.resizable(False, False)
        self.choice = 'cancel'

        existing = [Path(p) for p in existing]
        shown = existing[:self._MAX_LISTED]
        more = len(existing) - len(shown)
        if folder is None:
            folder = existing[0].parent if existing else ''
        # The author's wording (2026-10-31): one statement per line; no closing
        # question, as the buttons say what each does.
        intro = (
            f'{len(existing)} {what} file(s) already exist in the output folder and will be '
            f'replaced.\n'
            'Changes made to them in KiCad are lost.\n\n'
            f'{folder}\n'
            + '\n'.join(f'  \u2022 {p.name}' for p in shown)
            + (f'\n  \u2026 and {more} more' if more else '') +
            '\n\nTo keep them, cancel and copy them elsewhere first.\n'
            'The output folder is always the design\'s own.'
        )
        ttk.Label(self, text=intro, wraplength=520, justify='left').pack(
            anchor='w', padx=12, pady=12)

        frm_btns = ttk.Frame(self)
        frm_btns.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(frm_btns, text='Overwrite',
                   command=self._on_continue).pack(side='left')
        ttk.Button(frm_btns, text='Cancel conversion',
                   command=self._on_cancel).pack(side='right')

        self.protocol('WM_DELETE_WINDOW', self._on_cancel)
        center_over_parent(self, parent)
        self.grab_set()

    def _on_continue(self):
        self.choice = 'continue'
        self.destroy()

    def _on_cancel(self):
        self.choice = 'cancel'
        self.destroy()
