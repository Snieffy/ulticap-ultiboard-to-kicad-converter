# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/gui/dialogs.py -- the dialogs the KIUT window takes from KIUC's and KIUB's windows (stage 9c,
moved unchanged when those windows were retired)

- _RedoCorrectedDialog: the design's corrected copies out of date -- redo them from the originals,
  or cancel (KIUC's).
- _RecordIntegrityDialog: merged records in a .SCH, each issue shown in its file and resolved (KIUC's).
- _PinOverrideReviewDialog: the pin labels a conversion corrected, each component drawn (KIUC's).
- _TechnologyDialog: an Ultiboard library's technology, single or multi-file .TEC (KIUB's).
- PathDialog: a path kept in kiut.ini -- KiCad's program, KIUT's library folder (stage 10e).
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from kiut.board import kiub as KIUB
from kiut.gui import common
from kiut.schematic.kiuc_ascii import SCH_ENCODING, split_corrupted_line
from kiut.schematic.kiuc_model import pin_conn_point

_center_over_parent = common.center_over_parent
_center_toplevel = common.center_over_parent

_TECH_KEYS = ("single", "top", "bottom", "inner", "drills", "traces")


class _RedoCorrectedDialog(tk.Toplevel):
    """Modal dialog shown before conversion when the design's corrected
    copies (<name>_corrected, kiut.core.corrected) no longer match its original
    files: redo the corrections from the originals, or cancel. The default
    button is Redo -- unless that would throw away copies edited by hand:
    then it is Cancel.

    Sets self.choice to 'redo' or 'cancel' before closing.
    """

    def __init__(self, parent, text: str, default_redo: bool):
        super().__init__(parent)
        self.title('Corrected files out of date')
        self.transient(parent)
        self.resizable(False, False)
        self.choice = 'cancel'

        ttk.Label(self, text=text, wraplength=560, justify='left').pack(
            anchor='w', padx=12, pady=12)
        frm_btns = ttk.Frame(self)
        frm_btns.pack(fill='x', padx=12, pady=(0, 12))
        b_redo = ttk.Button(frm_btns, text='Redo from the originals', command=self._on_redo)
        b_redo.pack(side='left')
        b_cancel = ttk.Button(frm_btns, text='Cancel conversion', command=self._on_cancel)
        b_cancel.pack(side='right')
        # The button Enter presses: Redo, or Cancel when redoing would lose hand edits.
        self.default_button = b_redo if default_redo else b_cancel
        self.default_button.focus_set()
        self.bind('<Return>', lambda _e: self.default_button.invoke())

        self.protocol('WM_DELETE_WINDOW', self._on_cancel)
        _center_over_parent(self, parent)
        self.grab_set()

    def _on_redo(self):
        self.choice = 'redo'
        self.destroy()

    def _on_cancel(self):
        self.choice = 'cancel'
        self.destroy()


# ── record-integrity pre-conversion dialog ──────────────────────────────────

def _render_file_with_resolutions(original_lines: list, file_issues: list,
                                  resolutions: dict):
    """Return (rendered_lines, issue_line_no) for one file: original_lines
    with every RESOLVED issue (present as a key in resolutions) shown split
    into its two correctly-terminated replacement lines, using the exact
    same split_corrupted_line() the real disk-write uses -- this is a pure
    in-memory preview, nothing here ever touches the file. Unresolved
    issues are left exactly as they are in the file (still merged), so the
    user sees precisely what's wrong. issue_line_no maps each of
    file_issues to the 1-based line number, in rendered_lines, of its
    first (or only, if unresolved) line -- for scrolling/highlighting.
    """
    sorted_issues = sorted(file_issues, key=lambda iss: iss.line_no)
    lines = list(original_lines)
    offset = 0
    issue_line_no = {}
    for issue in sorted_issues:
        idx = issue.line_no - 1 + offset
        issue_line_no[issue] = idx + 1
        if issue in resolutions:
            new_a, new_b = split_corrupted_line(lines[idx], issue.merge_token_index,
                                                resolutions[issue])
            lines[idx:idx + 1] = [new_a, new_b]
            offset += 1
    return lines, issue_line_no


class _RecordIntegrityDialog(tk.Toplevel):
    """Modal dialog shown before conversion when one or more *S blocks
    (across any file in the hierarchy) have the 'missing ,\\n between two
    records' corruption (see kiuc_ascii.py's module-level comment for the
    full mechanism). Every unambiguous issue -- exactly one candidate
    split survives the leading-zero filter -- is pre-resolved
    automatically before this dialog even opens; it's still shown here,
    already marked resolved, purely so the user can see what was changed.
    Only genuinely ambiguous issues need a click.

    A single navigable view serves the whole hierarchy: Next/Previous
    step through every issue in every affected file in order, swapping
    the displayed file's content and re-scrolling/highlighting as needed,
    so resolving an issue always shows its real surrounding context (the
    actual file, not an isolated snippet) without leaving this dialog.

    The dialog writes nothing: Apply only collects the resolutions, and
    _check_record_integrity applies them to the sheets' corrected copies
    (kiut.core.corrected). Closing or cancelling it leaves every file untouched.
    """
    def __init__(self, parent, issues_by_file: dict):
        super().__init__(parent)
        self.title('Record integrity check')
        self.transient(parent)
        self.geometry('880x640')
        self.minsize(640, 420)
        self.choice = 'cancel'

        self._issues_by_file = issues_by_file
        self.all_issues = sorted(
            (iss for issues in issues_by_file.values() for iss in issues),
            key=lambda iss: (str(iss.sch_path), iss.line_no))

        self.resolutions: dict = {}
        for iss in self.all_issues:
            if iss.is_unambiguous:
                self.resolutions[iss] = iss.candidates[0]

        self._file_lines: dict = {}     # Path -> original lines (list[str])
        for path in issues_by_file:
            # The same lossless decoding the fix itself uses (kiuc_ascii.SCH_ENCODING),
            # so the preview shows the lines exactly as they will be rewritten.
            with open(path, 'r', newline='', encoding=SCH_ENCODING) as f:
                text = f.read()
            newline = '\r\n' if '\r\n' in text else '\n'
            self._file_lines[path] = text.split(newline)

        self._current_index = 0

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill='both', expand=True)

        intro = (f'{len(self.all_issues)} record-integrity issue(s) found across '
                 f'{len(issues_by_file)} file(s). Each is a single physical line '
                 f'missing its \u2019,\\n\u2019 between two records, merging the '
                 f'last field of one with the first field of the next. Issues '
                 f'with only one plausible split are corrected automatically '
                 f'(shown in green); the rest need you to choose (shown in '
                 f'yellow).')
        ttk.Label(frm, text=intro, wraplength=840, justify='left').pack(
            anchor='w', pady=(0, 8))

        self._header_lbl = ttk.Label(frm, font=('Segoe UI', 9, 'bold'))
        self._header_lbl.pack(anchor='w')

        text_frame = ttk.Frame(frm)
        text_frame.pack(fill='both', expand=True, pady=(6, 6))
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self._text = tk.Text(text_frame, wrap='none', font=('Consolas', 9),
                             state='disabled', background='white')
        vbar = ttk.Scrollbar(text_frame, orient='vertical', command=self._text.yview)
        hbar = ttk.Scrollbar(text_frame, orient='horizontal', command=self._text.xview)
        self._text.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self._text.grid(row=0, column=0, sticky='nsew')
        vbar.grid(row=0, column=1, sticky='ns')
        hbar.grid(row=1, column=0, sticky='ew')
        self._text.tag_configure('unresolved', background='#fff3b0')
        self._text.tag_configure('resolved', background='#c8f7c5')
        self._text.tag_configure('current', borderwidth=1, relief='solid')

        cand_frame = ttk.Frame(frm)
        cand_frame.pack(fill='x')
        self._cand_label = ttk.Label(cand_frame, text='')
        self._cand_label.pack(side='left')
        self._cand_buttons_frame = ttk.Frame(cand_frame)
        self._cand_buttons_frame.pack(side='left', padx=(8, 0))

        nav_frame = ttk.Frame(frm)
        nav_frame.pack(fill='x', pady=(8, 0))
        ttk.Button(nav_frame, text='\u25c0 Previous', command=self._on_prev).pack(side='left')
        ttk.Button(nav_frame, text='Next \u25b6', command=self._on_next).pack(
            side='left', padx=(6, 0))
        self._counter_lbl = ttk.Label(nav_frame, font=('Segoe UI', 9, 'bold'))
        self._counter_lbl.pack(side='right')

        btn_frame = ttk.Frame(frm)
        btn_frame.pack(fill='x', pady=(12, 0))
        ttk.Button(btn_frame, text='Apply corrections',
                  command=self._on_apply).pack(side='left')
        ttk.Button(btn_frame, text='Cancel', command=self._on_cancel).pack(side='right')

        self.protocol('WM_DELETE_WINDOW', self._on_cancel)
        _center_over_parent(self, parent)
        self.grab_set()

        self._show_issue(0)

    def _resolved_count(self) -> int:
        return sum(1 for iss in self.all_issues if iss in self.resolutions)

    def _show_issue(self, index: int):
        self._current_index = index
        issue = self.all_issues[index]
        path = issue.sch_path
        file_issues = self._issues_by_file[path]
        rendered_lines, issue_line_no = _render_file_with_resolutions(
            self._file_lines[path], file_issues, self.resolutions)

        self._text.configure(state='normal')
        self._text.delete('1.0', 'end')
        self._text.insert('1.0', '\n'.join(rendered_lines))
        for iss in file_issues:
            ln = issue_line_no[iss]
            tag = 'resolved' if iss in self.resolutions else 'unresolved'
            self._text.tag_add(tag, f'{ln}.0', f'{ln + 1}.0')
        cur_ln = issue_line_no[issue]
        self._text.tag_add('current', f'{cur_ln}.0', f'{cur_ln + 1}.0')
        self._text.configure(state='disabled')
        self._text.see(f'{max(1, cur_ln - 5)}.0')
        self._text.see(f'{cur_ln}.0')

        self._header_lbl.config(
            text=f'Issue {index + 1} of {len(self.all_issues)} \u2014 {path.name}, '
                 f'line {issue.line_no}, symbol {issue.symbol_name!r} ({issue.section})')

        self._update_candidates(issue)
        self._update_counter()

    def _update_candidates(self, issue):
        for w in self._cand_buttons_frame.winfo_children():
            w.destroy()
        if issue in self.resolutions:
            a, b = self.resolutions[issue]
            auto = ' (resolved automatically -- only one plausible split)' if issue.is_unambiguous else ''
            self._cand_label.config(text=f'Resolved: {a!r} | {b!r}{auto}')
        else:
            self._cand_label.config(text='Choose the correct split:')
            for a, b in issue.candidates:
                ttk.Button(self._cand_buttons_frame, text=f'{a} | {b}',
                          command=lambda a=a, b=b: self._on_choose(issue, a, b)
                          ).pack(side='left', padx=2)

    def _on_choose(self, issue, a, b):
        self.resolutions[issue] = (a, b)
        self._show_issue(self._current_index)

    def _update_counter(self):
        self._counter_lbl.config(
            text=f'{self._resolved_count()} of {len(self.all_issues)} resolved')

    def _on_prev(self):
        if self._current_index > 0:
            self._show_issue(self._current_index - 1)

    def _on_next(self):
        if self._current_index < len(self.all_issues) - 1:
            self._show_issue(self._current_index + 1)

    def _on_apply(self):
        unresolved = len(self.all_issues) - self._resolved_count()
        if unresolved > 0:
            ok = messagebox.askyesno(
                'Unresolved issues remain',
                f'{unresolved} of {len(self.all_issues)} issue(s) are still '
                f'unresolved. Apply corrections anyway?',
                parent=self, icon='warning')
            if not ok:
                return
        # Applied by the caller (_check_record_integrity), to the corrected copies.
        self.choice = 'apply'
        self.destroy()

    def _on_cancel(self):
        self.choice = 'cancel'
        self.destroy()


# ── pin-override review dialog ──────────────────────────────────────────────

def _diagram_size_for_overrides(sym) -> int:
    """Pick the diagram size (logical canvas size, in pixels, at 1:1 with
    the scrollregion _show sets) needed for every label to have enough
    room to stay readable and non-overlapping, based on the tightest
    real gap between two adjacent pins on the same edge. Unlike the
    window/frame's own visible size, this is never shrunk to fit --
    _PinOverrideReviewDialog wraps the canvas in a scrollable viewport,
    so a large or densely-packed symbol scrolls rather than becoming
    illegible.
    """
    if not sym.pins:
        return 260
    if sym.polylines:
        xs = [pt[0] for pl in sym.polylines for pt in pl.points]
        ys = [pt[1] for pl in sym.polylines for pt in pl.points]
    else:
        xs = [p.x for p in sym.pins]
        ys = [p.y for p in sym.pins]
    conn_pts = [pin_conn_point(p, sym) for p in sym.pins]
    all_xs = xs + [c[0] for c in conn_pts]
    all_ys = ys + [c[1] for c in conn_pts]
    w = max(1, max(all_xs) - min(all_xs))
    h = max(1, max(all_ys) - min(all_ys))

    min_gap = None
    for rot in (0, 1, 2, 3):
        group = [p for p in sym.pins if (p.pin_rotation & 3) == rot]
        vals = sorted((p.y if rot in (0, 2) else p.x) for p in group)
        for a, b in zip(vals, vals[1:]):
            gap = b - a
            if gap > 0 and (min_gap is None or gap < min_gap):
                min_gap = gap
    if not min_gap:
        min_gap = max(w, h)

    pad = 50
    # Measured actual glyph height for this font/size is 14px, so two
    # adjacent single-line labels need at least that much separation,
    # plus a margin, to avoid touching.
    min_px_gap = 20
    required_scale = min_px_gap / min_gap
    size = max(required_scale * w + 2 * pad, required_scale * h + 2 * pad, 260)
    return int(min(4000, size))


def _instance_pin_number(pin, comp, sym) -> str:
    """The pin number this particular placed instance actually uses for
    `pin`. A multi-gate symbol's pin carries one number per gate, comma-
    joined ("1,4,9,12"); the instance's own '#' override at this pin's
    connection point names the one it uses, and failing that, the unit
    letter at the end of its refdes (A = first) picks it from the list.
    A single-gate pin's number is returned as-is."""
    numbers = [n for n in (pin.number or '').split(',') if n]
    if len(numbers) <= 1:
        return pin.number or ''
    conn = pin_conn_point(pin, sym)
    for ca in comp.comp_attrs:
        if ca.tag == '#' and (ca.dx_poe, ca.dy_poe) == conn and ca.value:
            return ca.value
    refdes = comp.refdes or comp.attributes.get('REFDES') or ''
    if refdes and refdes[-1].isalpha():
        idx = ord(refdes[-1].upper()) - ord('A')
        if 0 <= idx < len(numbers):
            return numbers[idx]
    return pin.number


def _draw_component_overrides(canvas: tk.Canvas, comp, sym, corrected_keys: set,
                              canvas_px: int, show_pin_numbers: bool = False) -> None:
    """Draw sym's true body outline (from sym.polylines), pin stubs and
    connection-point dots OUTSIDE it (from sym.pins -- the pin's own body
    point sits exactly on the outline's edge, the stub extends out to its
    connection point), and comp's current *C LABEL/WIRELABEL override text
    at each one's own (dx, dy) position -- exactly how kiut_viewer.py (and
    Ulticap itself) renders it, since (dx, dy) is never the corrupted
    field, only (dx_poe, dy_poe) can be (see kiuc_writer.
    _resolve_pin_override_poes for the full mechanism). A label whose
    (dx, dy, tag, value) appears in corrected_keys -- i.e. one that was
    actually auto-corrected -- is drawn in a distinct colour as a hint of
    which are worth inspecting; every other label is plain black.

    Clicking a label draws a single help line from its own (dx, dy) to
    wherever its (dx_poe, dy_poe) actually points right now -- mirroring
    Ulticap's own 'select a label, see a line to its connection point' UI
    convention. For a corrected override this reveals it used to point to
    a different pin than the one it visually sits next to (comp.comp_attrs
    is never mutated by the correction, so this still shows the original,
    uncorrected data); for an unaffected one, the line points right back
    to itself. Clicking empty canvas clears the line.

    show_pin_numbers adds each pin's number beside its stub -- for a
    multi-gate symbol only the number this instance uses (see
    _instance_pin_number), so a complex part stays readable.
    """
    if sym.polylines:
        xs = [pt[0] for pl in sym.polylines for pt in pl.points]
        ys = [pt[1] for pl in sym.polylines for pt in pl.points]
    else:
        xs = [p.x for p in sym.pins]
        ys = [p.y for p in sym.pins]
    bx_min, bx_max = min(xs), max(xs)
    by_min, by_max = min(ys), max(ys)

    conn_pts = [pin_conn_point(p, sym) for p in sym.pins]
    # The drawing's extent: body, pin stubs, and every override's
    # (dx_poe, dy_poe) -- the click-arrow's target, which must stay on the
    # canvas. An override's own raw (dx, dy) is deliberately NOT included:
    # labels are drawn next to their pin (see below), never at (dx, dy),
    # so including it only added empty space -- e.g. a dual diode's labels sit
    # 150 units below the body, which shrank the symbol to a fraction of
    # the diagram.
    override_poes = [(ca.dx_poe, ca.dy_poe) for ca in comp.comp_attrs if ca.tag in ('LABEL', 'WIRELABEL')]
    all_xs = xs + [c[0] for c in conn_pts] + [p[0] for p in override_poes]
    all_ys = ys + [c[1] for c in conn_pts] + [p[1] for p in override_poes]
    x0, x1 = min(all_xs), max(all_xs)
    y0, y1 = min(all_ys), max(all_ys)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    pad = 50
    scale = min((canvas_px - 2 * pad) / w, (canvas_px - 2 * pad) / h)

    def to_canvas(ux, uy):
        cx = pad + (ux - x0) * scale
        cy = canvas_px - (pad + (uy - y0) * scale)
        return cx, cy

    bx0, by0 = to_canvas(bx_min, by_max)
    bx1, by1 = to_canvas(bx_max, by_min)
    canvas.create_rectangle(bx0, by0, bx1, by1, outline='#888')

    for p in sym.pins:
        poe = pin_conn_point(p, sym)
        body_x, body_y = to_canvas(p.x, p.y)
        conn_x, conn_y = to_canvas(poe[0], poe[1])
        canvas.create_line(body_x, body_y, conn_x, conn_y, fill='#444')
        canvas.create_oval(conn_x - 3, conn_y - 3, conn_x + 3, conn_y + 3,
                          fill='#444', outline='')
        if show_pin_numbers:
            number = _instance_pin_number(p, comp, sym)
            if number:
                # Beside the stub's midpoint, clear of the stub itself:
                # above a horizontal stub, right of a vertical one.
                mid_x, mid_y = (body_x + conn_x) / 2, (body_y + conn_y) / 2
                if (p.pin_rotation & 3) in (0, 2):
                    canvas.create_text(mid_x, mid_y - 2, text=number, fill='#2471a3',
                                       font=('Segoe UI', 7), anchor='s',
                                       tags=('pinnumber',))
                else:
                    canvas.create_text(mid_x + 3, mid_y, text=number, fill='#2471a3',
                                       font=('Segoe UI', 7), anchor='w',
                                       tags=('pinnumber',))

    item_pos: dict = {}     # canvas item id -> (x, y) of the label's OWN position
    item_target: dict = {}  # canvas item id -> (x, y) the help line points to

    _dirs = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}
    _label_inset = 12   # how far inside the matched pin's own edge the label sits

    def _nearest_pin_for_label(ca):
        """The pin ca's own (dx, dy) most plausibly belongs to, for DISPLAY
        positioning only -- never used to determine the override's actual
        electrical connection, which remains governed purely by
        (dx_poe, dy_poe) (see _resolve_pin_override_poes). Exact axis
        match preferred (the normal, uncorrupted case, and what the click-
        to-trace-POE line below still reveals a mismatch against if the
        electrical connection is wrong); falls back to nearest Euclidean
        distance if none matches exactly.
        """
        exact = [p for p in sym.pins if
                (ca.dy == p.y if (p.pin_rotation & 3) in (0, 2) else ca.dx == p.x)]
        candidates = exact if len(exact) == 1 else (exact or sym.pins)
        return min(candidates, key=lambda p: (ca.dx - p.x) ** 2 + (ca.dy - p.y) ** 2)

    for ca in comp.comp_attrs:
        if ca.tag not in ('LABEL', 'WIRELABEL'):
            continue
        if ca.value in ('?', '-', ''):
            continue
        pin = _nearest_pin_for_label(ca)
        dx_dir, dy_dir = _dirs[pin.pin_rotation & 3]
        # Step back from the pin's body point AGAINST its facing direction,
        # i.e. into the body, on both axes alike. (The Y term used to have
        # the opposite sign, which put every north/south-facing pin's label
        # out on its own stub, right on the connection dot.)
        disp_x = pin.x - dx_dir * _label_inset
        disp_y = pin.y - dy_dir * _label_inset
        label_x, label_y = to_canvas(disp_x, disp_y)
        key = (ca.dx, ca.dy, ca.tag, ca.value)
        colour = '#c0392b' if key in corrected_keys else '#222'
        weight = 'bold' if key in corrected_keys else 'normal'
        # Anchor from the matched pin's own facing direction, so the text
        # grows away from the pin, further into the body. Canvas Y runs
        # downward (to_canvas flips it): a north-facing pin's label hangs
        # below its anchor ('n'), a south-facing one's sits above it ('s').
        if dx_dir > 0:
            anchor = 'e'
        elif dx_dir < 0:
            anchor = 'w'
        elif dy_dir > 0:
            anchor = 'n'
        else:
            anchor = 's'
        item = canvas.create_text(label_x, label_y, text=ca.value, fill=colour,
                                  font=('Segoe UI', 8, weight), anchor=anchor,
                                  tags=('overridelabel',))
        item_pos[item] = (label_x, label_y)
        item_target[item] = to_canvas(ca.dx_poe, ca.dy_poe)

    _line_id = {'id': None}

    def _clear_line():
        if _line_id['id'] is not None:
            canvas.delete(_line_id['id'])
            _line_id['id'] = None

    def _on_canvas_click(event):
        cx = canvas.canvasx(event.x)
        cy = canvas.canvasy(event.y)
        hit = canvas.find_closest(cx, cy)
        clicked = hit[0] if hit else None
        _clear_line()
        if clicked not in item_pos:
            return
        # item_pos/item_target were recorded while drawing, but the caller
        # may move the whole drawing afterwards (_PinOverrideReviewDialog
        # centres it in the viewport). Read the label's CURRENT position
        # back from the canvas and shift the target by the same amount --
        # using the recorded values directly drew the arrow displaced by
        # exactly that centring offset, up and to the left.
        x1, y1 = canvas.coords(clicked)
        shift_x, shift_y = x1 - item_pos[clicked][0], y1 - item_pos[clicked][1]
        x2, y2 = item_target[clicked]
        x2, y2 = x2 + shift_x, y2 + shift_y
        _line_id['id'] = canvas.create_line(x1, y1, x2, y2, fill='#2980b9',
                                            dash=(3, 2), width=1, arrow='last')
        canvas.tag_lower(_line_id['id'])
        canvas.tag_raise(clicked)

    canvas.bind('<Button-1>', _on_canvas_click)


class _PinOverrideReviewDialog(tk.Toplevel):
    """Non-blocking, post-conversion review of every pin-label override
    the writer auto-corrected (see kiuc_writer._resolve_pin_override_poes).
    Opened on demand from the 'Review auto-corrected pins…' button; never
    shown automatically, and never blocks or gates the conversion, which
    has already completed and already been written -- there is no
    fix/continue/cancel decision left to make, unlike the old detection
    dialog this replaces, since every correction here is unconditional
    and applied automatically.

    Master-detail layout: a list of every corrected component on the
    left, a live diagram of whichever one is selected on the right (see
    _draw_component_overrides) -- selecting a different row swaps the
    diagram directly, no separate popup.
    """
    def __init__(self, parent, corrected_components: list, corrections: list):
        super().__init__(parent)
        self.title('Auto-corrected pin labels')
        self.transient(parent)
        self.geometry('880x560')
        self.minsize(600, 400)
        self._show_pin_numbers = tk.BooleanVar(value=False)
        self._show = None   # set below; exposed for tests

        # (sheet_name, comp_x, comp_y, comp_rotation, comp_symbol_name) ->
        # set of (dx, dy, tag, value) actually corrected for that component.
        self._corrected_keys_by_comp: dict = {}
        for c in corrections:
            comp_key = (c.sheet_name, c.comp_x, c.comp_y, c.comp_rotation, c.comp_symbol_name)
            self._corrected_keys_by_comp.setdefault(comp_key, set()).add(
                (c.dx, c.dy, c.tag, c.value))

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill='both', expand=True)
        _hint_label = ttk.Label(frm, text='Click a name to draw a line to its actual connection '
                 'point. Bold red names were auto-corrected during conversion; '
                 'click empty space to clear the line.',
                 foreground='#666')
        _hint_label.pack(anchor='w', pady=(0, 8), fill='x')
        frm.bind('<Configure>', lambda e: _hint_label.config(wraplength=max(200, e.width - 24)))

        body = ttk.Frame(frm)
        body.pack(fill='both', expand=True)

        list_frame = ttk.Frame(body, width=340)
        list_frame.pack(side='left', fill='y', padx=(0, 12))
        list_frame.pack_propagate(False)
        list_frame.grid_propagate(False)
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        listbox = tk.Listbox(list_frame, exportselection=False)
        list_vbar = ttk.Scrollbar(list_frame, orient='vertical', command=listbox.yview)
        list_hbar = ttk.Scrollbar(list_frame, orient='horizontal', command=listbox.xview)
        listbox.configure(yscrollcommand=list_vbar.set, xscrollcommand=list_hbar.set)
        listbox.grid(row=0, column=0, sticky='nsew')
        list_vbar.grid(row=0, column=1, sticky='ns')
        list_hbar.grid(row=1, column=0, sticky='ew')
        for sheet_name, comp, sym in corrected_components:
            # comp.refdes is set by both readers; attributes['REFDES'] only
            # by the ASCII one -- reading only that left .UTSCH rows showing
            # the bare symbol name.
            refdes = comp.refdes or comp.attributes.get('REFDES')
            if refdes:
                ident = f'{refdes} ({comp.symbol_name})'
            elif comp.file_ref:
                ident = f'{comp.file_ref} ({comp.symbol_name})'
            else:
                ident = comp.symbol_name
            listbox.insert('end', f'{ident}  on {sheet_name}')

        canvas_frame = ttk.Frame(body)
        canvas_frame.pack(side='left', fill='both', expand=True)
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        canvas = tk.Canvas(canvas_frame, background='white',
                          highlightthickness=1, highlightbackground='#ccc')
        canvas_vbar = ttk.Scrollbar(canvas_frame, orient='vertical', command=canvas.yview)
        canvas_hbar = ttk.Scrollbar(canvas_frame, orient='horizontal', command=canvas.xview)
        canvas.configure(yscrollcommand=canvas_vbar.set, xscrollcommand=canvas_hbar.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        canvas_vbar.grid(row=0, column=1, sticky='ns')
        canvas_hbar.grid(row=1, column=0, sticky='ew')

        def _show(index):
            canvas.delete('all')
            sheet_name, comp, sym = corrected_components[index]
            comp_key = (sheet_name, comp.x, comp.y, comp.rotation, comp.symbol_name)
            corrected_keys = self._corrected_keys_by_comp.get(comp_key, set())
            size = _diagram_size_for_overrides(sym)

            _draw_component_overrides(canvas, comp, sym, corrected_keys, size,
                                      show_pin_numbers=self._show_pin_numbers.get())

            # Center a drawing that's smaller than the canvas's own
            # (constant, unchanging) viewport, rather than leaving it
            # pinned in the top-left corner -- and rather than resizing
            # the canvas widget itself to match, which would make the
            # canvas visibly grow/shrink and reposition on every
            # selection. A drawing that needs more room than the current
            # viewport still scrolls, exactly as before.
            canvas.update_idletasks()
            avail_w = canvas.winfo_width() or size
            avail_h = canvas.winfo_height() or size
            off_x = max(0, (avail_w - size) // 2)
            off_y = max(0, (avail_h - size) // 2)
            if off_x or off_y:
                canvas.move('all', off_x, off_y)
            canvas.configure(scrollregion=(0, 0, max(size, avail_w), max(size, avail_h)))
            canvas.xview_moveto(0)
            canvas.yview_moveto(0)

        def _on_select(_event):
            sel = listbox.curselection()
            if sel:
                _show(sel[0])

        self._show, self._canvas, self._listbox = _show, canvas, listbox

        listbox.bind('<<ListboxSelect>>', _on_select)
        listbox.selection_set(0)
        self.after(50, lambda: _show(0))

        def _redraw():
            sel = listbox.curselection()
            _show(sel[0] if sel else 0)

        btns = ttk.Frame(frm)
        btns.pack(fill='x', pady=(12, 0))
        ttk.Checkbutton(btns, text='Show pin numbers', variable=self._show_pin_numbers,
                        command=_redraw).pack(side='left')
        ttk.Button(btns, text='Close', command=self.destroy).pack(side='right')
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        _center_over_parent(self, parent)


# ── refdes reannotation pre-conversion dialog ───────────────────────────────



class _TechnologyDialog(tk.Toplevel):
    """Asks for the technology of an Ultiboard library (.LIB/.L55): one
    single-file .TEC, or a multi-file set (top, bottom and inner pads,
    drills, optional traces). Each file is checked for its role as it is
    picked; OK is available only when every needed file fits. The same
    file may fill several pad roles. result is the choice dict, or None
    when cancelled."""

    _ROLES_MULTI = ("top", "bottom", "inner", "drills", "traces")

    def __init__(self, parent: tk.Tk, library_name: str, choice: dict[str, str]) -> None:
        super().__init__(parent)
        self.title("Technology")
        self.transient(parent)
        self.resizable(True, False)
        self.result: dict[str, str] | None = None
        self._lib = KIUB._load_kiub_library_module()
        self.columnconfigure(1, weight=1)

        ttk.Label(self, text=(
            f"{library_name} is an Ultiboard library: it holds pad, drill and trace codes "
            "only. Pick the technology (.TEC data) the library was made with."),
            wraplength=640, justify="left").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(12, 8))

        self._mode_var = tk.StringVar(value=choice.get("mode", "single"))
        mode_frame = ttk.Frame(self)
        mode_frame.grid(row=1, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 6))
        ttk.Label(mode_frame, text="Single or multi-file .TEC data:").pack(side="left")
        ttk.Radiobutton(mode_frame, text="Single file", value="single",
                        variable=self._mode_var, command=self._update).pack(
            side="left", padx=(8, 0))
        ttk.Radiobutton(mode_frame, text="Multi-file set", value="multi",
                        variable=self._mode_var, command=self._update).pack(
            side="left", padx=(8, 0))

        lib = self._lib
        roles = {"single": lib.TEC_SINGLE, "top": lib.TEC_TOP, "bottom": lib.TEC_BOTTOM,
                 "inner": lib.TEC_INNER, "drills": lib.TEC_DRILLS, "traces": lib.TEC_TRACES}
        self._roles = roles
        self._vars: dict[str, tk.StringVar] = {}
        self._status: dict[str, ttk.Label] = {}
        self._rows: dict[str, list[tk.Widget]] = {}
        for row, key in enumerate(_TECH_KEYS, start=2):
            label = lib.TEC_ROLE_NAMES[roles[key]] + (" (optional)" if key == "traces" else "")
            var = tk.StringVar(value=choice.get(key, ""))
            var.trace_add("write", lambda *_: self._update())
            self._vars[key] = var
            widgets: list[tk.Widget] = [
                ttk.Label(self, text=label + ":"),
                ttk.Entry(self, textvariable=var, width=64),
                ttk.Button(self, text="Browse…",
                           command=lambda k=key: self._browse(k)),
                ttk.Label(self, width=2)]
            widgets[0].grid(row=row, column=0, sticky="w", padx=(12, 6), pady=2)
            widgets[1].grid(row=row, column=1, sticky="ew", pady=2)
            widgets[2].grid(row=row, column=2, padx=6, pady=2)
            widgets[3].grid(row=row, column=3, sticky="w", padx=(0, 12), pady=2)
            self._status[key] = widgets[3]
            self._rows[key] = widgets

        self._message = ttk.Label(self, wraplength=640, justify="left")
        self._message.grid(row=8, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 4))

        buttons = ttk.Frame(self)
        buttons.grid(row=9, column=0, columnspan=4, sticky="ew", padx=12, pady=(4, 12))
        ttk.Button(buttons, text="Cancel", command=self._on_cancel).pack(side="right")
        self._ok = ttk.Button(buttons, text="OK", command=self._on_ok)
        self._ok.pack(side="right", padx=(0, 6))

        self._update()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        _center_toplevel(self, parent)
        self.grab_set()

    def _active(self) -> tuple[str, ...]:
        return ("single",) if self._mode_var.get() == "single" else self._ROLES_MULTI

    def _browse(self, key: str) -> None:
        current = self._vars[key].get().strip()
        path = filedialog.askopenfilename(
            parent=self, title=f"Select {self._lib.TEC_ROLE_NAMES[self._roles[key]]}",
            initialdir=str(Path(current).parent) if current else None,
            filetypes=[("Technology files", "*.tec *.TEC"), ("All files", "*.*")])
        if path:
            self._vars[key].set(str(Path(path)))

    def _check(self, key: str) -> tuple[bool, str, list[str]]:
        """(fits, error, incomplete tables) for one role's field."""
        path = self._vars[key].get().strip()
        if not path:
            if key == "traces":
                return True, "", []
            return False, "", []
        if not Path(path).is_file():
            return False, f"{self._lib.TEC_ROLE_NAMES[self._roles[key]]}: file not found", []
        _tec, error, short = self._lib.check_tec(path, self._roles[key])
        return not error, error, short

    def _update(self) -> None:
        active = self._active()
        errors: list[str] = []
        warnings: list[str] = []
        ok = True
        for key in _TECH_KEYS:
            for w in self._rows[key]:
                w.grid() if key in active else w.grid_remove()
            if key not in active:
                continue
            fits, error, short = self._check(key)
            filled = bool(self._vars[key].get().strip())
            self._status[key].config(
                text=("✓" if fits else "✗") if filled else "",
                foreground="green" if fits else "red")
            ok = ok and fits
            if error:
                errors.append(error)
            warnings += short
        lines = errors + [f"Incomplete table: {w}; the missing codes count as undefined"
                          for w in warnings]
        self._message.config(text="\n".join(lines),
                             foreground="red" if errors else "black")
        self._message.grid() if lines else self._message.grid_remove()
        self._ok.config(state=tk.NORMAL if ok else tk.DISABLED)

    def _on_ok(self) -> None:
        self.result = {"mode": self._mode_var.get()}
        for key in _TECH_KEYS:
            self.result[key] = self._vars[key].get().strip()
        self.destroy()

    def _on_cancel(self) -> None:
        self.result = None
        self.destroy()


# ── a path KIUT keeps: KiCad's program, the library folder (stage 10e) ─────────

class PathDialog(tk.Toplevel):
    """Modal dialog for one path kept in kiut.ini: what it is for, the path
    in use, and Choose… (choose(dialog): asks for a new one, '' when
    cancelled), the reset button (reset_label: back to what KIUT uses
    without a path of its own) and Close.

    Sets self.result: None (unchanged), '' (reset) or the path chosen.
    """

    def __init__(self, parent, title: str, text: str, current: str, choose, reset_label: str):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self._choose = choose
        ttk.Label(self, text=text, wraplength=560, justify='left').pack(anchor='w', padx=12, pady=(12, 6))
        self.current_var = tk.StringVar(value=current)
        ttk.Label(self, textvariable=self.current_var, foreground='#1f4e79', wraplength=560,
                  justify='left').pack(anchor='w', padx=12, pady=(0, 12))
        frm = ttk.Frame(self)
        frm.pack(fill='x', padx=12, pady=(0, 12))
        self.choose_button = ttk.Button(frm, text='Choose…', command=self._on_choose)
        self.choose_button.pack(side='left')
        self.reset_button = ttk.Button(frm, text=reset_label, command=self._on_reset)
        self.reset_button.pack(side='left', padx=(6, 0))
        ttk.Button(frm, text='Close', command=self.destroy).pack(side='right')
        self.bind('<Escape>', lambda _e: self.destroy())
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        _center_over_parent(self, parent)
        self.grab_set()

    def _on_choose(self):
        path = self._choose(self)
        if path:
            self.result = path
            self.destroy()

    def _on_reset(self):
        self.result = ''
        self.destroy()
