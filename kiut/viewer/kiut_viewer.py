#!/usr/bin/env python3
# KIUT - KiCad Ulticap/Ultiboard import tools
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
kiut_viewer.py — the KIUT Viewer: Ulticap .SCH, .BLK and .UTSCH files, as Ulticap draws them.

Requires Qt (PySide6) for rendering, navigation, and PNG/SVG/PDF export.

Usage:
    python kiut_viewer.py [design.SCH | design.BLK | design.UTSCH]   (from the KIUT folder)

Controls:
    Mouse wheel         Zoom (centred on cursor)
    Right/middle drag   Pan
    Space               Set relative-coordinate origin at the cursor
    C                   Toggle full-window crosshair
    F                   Fit sheet border
    Double-click        Enter a component's sub-sheet (if it has one)
    B                   Back to the previous sheet
    Esc                 Clear the current highlight (symbol/label/annotation)
    Q                   Quit

See the in-app Help menu for the full reference (layers, toolbar, and
hierarchy navigation).

Install Qt backend (required):
    pip install PySide6
"""
from __future__ import annotations

import argparse
import base64
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from kiut.schematic.kiuc_model import (
    Sheet, Component,
    MM_PER_UNIT,
    UC_POLYLINE_STYLE_BY_CODE, UC_ARC_STYLE_BY_CODE,
    rot_transform as _rot_transform,
    pin_conn_point as _pin_conn_point,
    tight_bounds as _tight_bounds,
    tight_bounds_with_gap as _tight_bounds_with_gap,
    palette_rgb, ulticap_translate, is_pwr_symbol, is_port_sym, u_to_justify,
    _uc_box_metrics, _uc_overline_gap, _uc_vertical_metrics, x_text_angle,
)
from kiut.schematic.kiuc_ascii import (
    load_sheet as _kiuc_load_sheet,
    build_hierarchy_tree as _kiuc_build_hierarchy_tree,
    collect_hierarchy_paths as _kiuc_collect_hierarchy_paths,
    natural_sort_key as _natural_sort_key,
    resolve_file_ref as _resolve_file_ref,
)
from kiut.schematic import kiuc_binary_semantic as _ks
from kiut.schematic.kiuc_writer import (
    _build_subsheet_map, _find_root_sheet, _subsheet_key, _sheet_stem,
    _ellipse_or_pie_points, _is_field_tag, _drop_import_copies,
)


# ══════════════════════════════════════════════════════════════════════════════
# .UTSCH DESIGNS — a .UTSCH holds a whole hierarchy in one file, so its sheets
# have no file of their own to be opened by. The viewer reads the design once,
# keeps every sheet in memory, and names each by the converter's own lookup
# key (_subsheet_key of its stored name); a subsheet box's FILE= is followed
# to its sheet the same way the converter follows it, and the root is found
# the same way too (_find_root_sheet), so the viewer and the converter always
# agree on which sheet is which.
# ══════════════════════════════════════════════════════════════════════════════

def _is_utsch(path) -> bool:
    return Path(path).suffix.upper() == '.UTSCH'


class _UtschNode:
    """A Sheets-panel node for a .UTSCH sheet: like kiuc_ascii's HierNode,
    plus the sheet's key -- path alone can't tell a .UTSCH's sheets apart."""
    __slots__ = ('label', 'path', 'key', 'children')

    def __init__(self, label: str, path: Path, key: str):
        self.label, self.path, self.key, self.children = label, path, key, []


class _UtschDesign:
    """One .UTSCH design, read once, every sheet kept in memory."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.mtime = self.path.stat().st_mtime
        # The root is named after the file, as every conversion names it
        # (read_schematic_file); `warnings` says when that isn't possible.
        sch, self.warnings = _ks.read_schematic_file(self.path)
        self.sheets = sch.sheets
        self.by_key = _build_subsheet_map(self.sheets)
        self.root_key = _subsheet_key(_find_root_sheet(self.sheets, self.by_key).name)
        self.tree = self._node(self.root_key, frozenset())

    def sheet(self, key: str):
        return self.by_key.get(key)

    def stem(self, key: str) -> str:
        """File stem for exporting this sheet: the root after the .UTSCH file,
        a sub-sheet after its stored name made file-safe -- the names the
        converter gives the same sheets' .kicad_sch files."""
        return self.path.stem if key == self.root_key else _sheet_stem(self.by_key[key].name, key)

    def child_key(self, file_ref: str) -> Optional[str]:
        key = _subsheet_key(file_ref or '')
        return key if key in self.by_key else None

    def _node(self, key: str, ancestors: frozenset) -> _UtschNode:
        sheet = self.by_key[key]
        node = _UtschNode(self.path.stem if key == self.root_key else sheet.name, self.path, key)
        if key in ancestors:
            return node        # a cycle: leave as a leaf, as build_hierarchy_tree does
        children = []
        for comp in sheet.components:
            child = self.child_key(getattr(comp, 'file_ref', None))
            if child is not None and child not in [c for c, _ in children]:
                children.append((child, self.by_key[child].name))
        children.sort(key=lambda entry: _natural_sort_key(entry[1]))
        node.children = [self._node(child, ancestors | {key}) for child, _ in children]
        return node

    def keys(self) -> List[str]:
        """Every sheet reachable from the root, root first, each once -- the
        order collect_hierarchy_paths gives an ASCII hierarchy."""
        out: List[str] = []

        def walk(node):
            if node.key not in out:
                out.append(node.key)
                for child in node.children:
                    walk(child)
        walk(self.tree)
        return out


_UTSCH_CACHE: Dict[Path, _UtschDesign] = {}


def _utsch_design(path: Path, fresh: bool = False) -> _UtschDesign:
    """The design in a .UTSCH file, read again only when asked to (File ▸
    Open, a recent file) or when the file changed on disk since."""
    key = Path(path).resolve()
    design = _UTSCH_CACHE.get(key)
    if fresh or design is None or design.mtime != key.stat().st_mtime:
        design = _UTSCH_CACHE[key] = _UtschDesign(key)
        _note_read(f'WARNING: {w}' for w in design.warnings)
    return design


# What reading the design's files had to say (KIUT stage 10d): kept here, in order and without
# repeats, never printed -- started from the KIUT window, the viewer shares its console, and every
# sheet is read more than once. The status bar counts them; View -> Reading messages... lists them.
_READ_MESSAGES: List[str] = []


def _note_read(messages) -> None:
    for m in messages:
        if m not in _READ_MESSAGES:
            _READ_MESSAGES.append(m)


def _load_sheet(path, errors: Optional[list] = None):
    found: list = []
    sheet = _kiuc_load_sheet(path, errors=found)
    _note_read(found)
    if errors is not None:
        errors.extend(found)
    return sheet


def _build_hierarchy_tree(root_path, *args, **kw):
    found: list = []
    tree = _kiuc_build_hierarchy_tree(root_path, *args, errors=found, **kw)
    _note_read(found)
    return tree


def _collect_hierarchy_paths(root_path):
    found: list = []
    paths = _kiuc_collect_hierarchy_paths(root_path, errors=found)
    _note_read(found)
    return paths


def _load_design_sheet(path: Path, key: Optional[str] = None, fresh: bool = False):
    """(sheet, key) for any input: an ASCII .SCH/.BLK file is one sheet (key
    None); a .UTSCH's sheet is the one `key` names, its root by default."""
    if not _is_utsch(path):
        return _load_sheet(path), None
    design = _utsch_design(path, fresh)
    key = key or design.root_key
    sheet = design.sheet(key)
    if sheet is None:
        raise ValueError(f'{Path(path).name} has no sheet {key!r}')
    return sheet, key


# ══════════════════════════════════════════════════════════════════════════════
# TOOLBAR ICONS — embedded as base64 PNGs (20px, generated with Pillow) so the
# viewer stays a single self-contained file with no separate asset files to
# lose or ship alongside it. Decoded lazily by whichever backend is active.
# ══════════════════════════════════════════════════════════════════════════════

_ICON_PNG = {
    'open': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAACmUlEQVR4nNVUzYoTQRD+qmac9P7NDmwQFGWTHAbZHPMEQfAFPItvIXqaqw+hL7DgyQeYk+JB1OPuMux6EA8ya8jMMJtstvvzYBKjxjUKHvygKOiu+uqnuwr4b0FSSOqvZH9/3/tnwZMk0VVtJU1Tv9/vX2RZdj+KovuDwcCKyDw7km5jY0Orqnodx/EDkh4At5RMBAAIAP7W1pYAgHMuXl9f71dVBd/3v3Ow1iKKov7BwcGhiDxdJeMZg4iI32g00Gg0fiImCVVFq9V6kmWZBfBCVdU556b33N7eljzPP+/t7X0mCf/4+NgBoLX2+XA4LKqqcgCW9ZJlWQrJawDuWGsXAzsR0SAIXgE4nfunaWpINlYpcVbhghaSM72WpqmZGxwdHT1sNpuPTk9PCWCVAFxyNt7Z2ZE8zx/HcfzYBwBVPSQZAnAiUv9B5t8ikSFJVdVDYPp4IpIHQQAAqYjc9X1fLy4uln6pHzGzJfksCILbIpIvEn8qiuJcVWNVHe/u7o7+JNuTkxMDIC6K4lxEPs2Jx+Pxx8lk4kSkGYbhFZJjfO3/sl4uQgAwz/Mrg8GgWVWVc859BABNkkSNMRPP896HYahlWV4XEU4r4WUysynL8noYhup53ntjzCRJEtVutyvtdntE8sPm5maD5NWFbH6HrzNMXp36fmi326NutyuLgzBUVYhIi6QC8C7bdos2ItJSVQAYzsi00+nMyN8YY2CtvSkiTkTOp/oyORcRZ629aYwBgDcA0Ol0dHEpyHA4dADuZVnWFxEheenjzWxI3pj6ztvn93o9BwDOubeqqlEU3TLG3Fqhv3OMRiOoKpxzbwGg1+s5H9PdWtf1y7qu+2dnZ64oit99s58yX1tb07qu302PVhquv8IXa/RzsvSyIBYAAAAASUVORK5CYII=',
    'export_png': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAACSUlEQVR4nMVUwUobURQ9581EmOi4MJCFi3ZTBBcWbEvpohUK/QW3brvoxj+YbgVx5R906R90IXSnJaUuUqLxTRChUB1sMpOahKd5twsnaYxpHJHSA7M5c+555917eUBGiIhTKpVyIsKsNf8HvYSHh4crzWbzfa1We5jyalzd2J8pCAAk301NTQXW2keD/H2Me6gnSXJJ0mQRjzVOr9tL5pB0STJtz9had4wpSdphnqQiKQAuUo1kTiwiiqTs7e0thmH4QESYcrDWto6Pj2cqlcqzq/Cjh3iDTIVydHT0pFgs7lhrP6SpLABYay87nc5GsVj8HIbhy6uSm+ajWiEA0G63f5L8Pjs7u3RwcLAG4KLZbEJE1gqFwps4jnfOz8+/DdZca9mYVthKpfJ0enr6o7V2ptPp/FJK5T3PU8aYH8aYx3Nzc1FPe2srAICkFRF3fn7+S71ef+u6Lkh6ALoiglartZKaOqNMb0WpVMoBwP7+/mqj0ZBGoyHVanU1vdVfN+paSBFRo4YgIg4AaK03tdabg9yQTg3t/T9EGIYLSZJsa63XB55HZ/Arl8sT5XJ5YpjvabXW60mSbIdhuACk66aUKvi+//r09NQl2QXQHXH+KK7Pa62f+77/6uzsrNA3NsbYOI4NgFytVlvqdrvKcZxM0+5prbW5OI6NMcb2jQGA5ITv+y88z/uUrYHX0W63Qf6ZmxsEgSK5G0XRRj6fXzw5ObnE3Z5TALCTk5NuFEVfSe4GQXDX+uzoZxcRZ2tr615my8vLSIeP3zcEYS200zcLAAAAAElFTkSuQmCC',
    'export_svg': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAACW0lEQVR4nMWUPWiTURSGn3OTr6Y/iknBwUGhOCkK/qAOWhCc6uRQx45VKU0jDv4st1mELoJUEWe3dtDGCOJSBAUVfwbR0U0RxBatTW2+ft/rkLTEJk1jQXymyzn3vBzec+6FFumfUGLwlQIka7Xm/6BqhyMPNHDlmUYvFrUTwHu5ZnVNkwCjYAAyhtq34sOYXQAf9tDUknWFa5gtfWfJGeVWLjcV9pLbw0pnCYOkDJNk6Z7mtcm1EpLMzOL6BM7MBIRIRuXcWsfey5mZzk1qf7agHUhWEYQIShceKTN0X4cw01pDrAt6yeVHUa6gA51pnsu4i5kwYoCkWFoqc70zw8tsQcfyo8irXrzOijxIQC5iJgr5tGUbvcMFjQHh4jxEMNa+lZOLczwPHe8FGNTZ0XBlvOTyZvH5ezrY0cXjWGSiRX7i6AhSuKjMl0jsG++zr3g58vWzaOhP3iz200rePm2vF+Y5m0gAjnZERAxhmYHxPvvaP6FEI9F1GbyjAGB4SrnLT6XLT6XhKeUA/LTW3KgaZF5yjSbcP6EEQLaom9mibtbGavFerjLEf/xJGcDQQ+3dnObGwgxvPpe4lO7BzX7kD+92QwLgA0S18eW72zsYa89wYG6WkVun7F3Fp5juVBcnSjMkJ89YxKriKo1iK/FsUYdTXRyf+0Y3VPdYEfHCD8qIYKSoXoRbfhDrUr0rESz8oKyo+pAAAsCMtrZOjgYpnrQkuIrwF5hVtADMe7mZIwQWcy3YxP7yL5awv/pOQcRtKZLhIm/luJp5QbiR5lpiZeca7eZGqA6f32hdCrRyH7QDAAAAAElFTkSuQmCC',
    'export_pdf': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAIAAABL1vtsAAAAW0lEQVR4nGNgoCKwwQuob0RUVBRFRkSBAflGRMEAmUZEIQFyjIhCBSQbgcdro0bQyIgtcnIkIZxG/EcCyFw0WdKMgIvDZQm7Atm1mK4g7BGKwmJwRCoVjCAbAAC4Qxl9gDkMRgAAAABJRU5ErkJggg==',
    'fit_sheet': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAADNUlEQVR4nK1VTWsjRxCt192j0Ugef0SIGGPZBMTiJeCLwgb2kNUxyY8Iy0Kuue5R1jXHXANLCLn5vnu0ySGQIF0MZo1kHGQL42D8oRmN50MzXTlYMysHzZINKSim6ap+XVP9+jXRjLVaLcHMcsZBOcbM6HQ6RurMLHITc+bV3t6eYmY1O87JzcBVCgqADw4OPq7Vap/e3NwklmXJ1dXVfQDxPJDT09OPDMP4zvO8uFKpqPPz8zcAfmdmAUAj3WUwGGwKIX5ZWFh46roumaZJruv+RETHAAQzayKidAzgUaVS+SYIArJtmxzH+cPzvJf1ev3X3d1dqPtcJP1+/1GlUnl6eXk5KZVKhhCCarXac8Mw5rZuMpnQ7e0tRVFEw+HQr9VqT8bj8dcA9judjjHbq9DzvMSyLBWG4X4YhvuO4zAzJ/OAAcjpbz8rlUpfeJ6XEJH7oMdEREIIQyklpZQURdGber3+/dxS/2G9Xu+laZpNpRQRkZXhEZFmZqmU6vi+/2pxcZGIqDhlQfqd50VmVkIIc2lpiVzXfaW1/pGZZaPRiAUAJiJsbm7eJEnytlAoEDMnUzbEAOZ6GmPmpFAoUBAEb7e2tv6cntk73jEzmNli5n/TgQfGzCSltFqtVoaXDQAwAP3BqO/AdbvdztbPv4b/g2XArVZLAPjPGzGzmJWFDKjdbuskSXwgV3dyDQAB8KdEuAee7sJHR0efFIvFx1EUpeRXRJRHtSwGQEZRRFLKx4PBYOW+eIbodrsKQCKE+Na27Rej0Yi01uGUVkEe3dIYEQWO45BlWS/iOP4MQEJEYvZK+3EcJ2EYQgjxZb/ftwDo911pIgIRNYMgYABaaz1J47PAdrlcltfX175lWU3TNJvLy8v0PhFyHIfCMKS7u7tJtVo1giAwM+CTkxPNzOL4+Pj1aDR6tr6+/iSVzeFw+DMz9+bJJhHVbdt+rrWmarVqjMfj3wzD6E1l+J4eqTgfHh5+vra29tXV1VVcLpfVZDL5YWNj43pexcwsLi4umr7vJysrK/Ls7Oxwe3v7r/TReJCYA/AhT1PG1SxhelCi2+3KdK7RaKSCkweSFbOzs/NAEv4GkLkVTJrHJfoAAAAASUVORK5CYII=',
    'crosshair': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAABq0lEQVR4nM2UsY7TQBCG/xl7I0VJLEiTR0h5QrRIoeAxKKiuoeUxaKG+Oi9xoqUCpBQp4ihdau+uI7Hx/hR4c+FIInMCHb80zeifTzOzYwOPJZICAGVZXllrX67X6yfH+XPSDmxpQR8Gg8EtgGddaruA0YK9tTbGGPdd/J3BAFREVEQuruAh4D/S44LbCyAAiEinGk2FJPV+zOfz7CdLqKqGJFTVkZTFYpGdqklnmLddMHV0SqvV6qMx5oVzDiGEaxG5BvD9UscCAMvl8qooivfOOaZXT+OLiMnzfNY0DUgiyzI0TfOJZEjTtH4Oh0OpqurddDr9mjqeTCaTV/1+H6q/rpAknHPY7/cgCWMMxuPx7P7VxRhRFAWstZPDKrz3n7fb7cxaC2PM3TgiGkKgqr7u9XpvVDXf7XY3dV3fiIhmWdYkbwgBdV3De//t0op+U1mWt1VVcbPZPO/iP76K7EwYkhJjDO3IozbfO+OXA1hEKCLNqQDQtA+UHjUe5U8FD+B/of8CHElGkmc/pAeBRWQwGo1UVfMu/i6m9PN5671/CuBLm49dm/qr+gHkBR81OLzdQwAAAABJRU5ErkJggg==',
    'launch_gui': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAAdElEQVR4nO2TSwrAIAxEZ4r3v3K6ioiYmg8uhL6lmOckROA2mCkSETGFJAHgySbaPZpKbMkUkiwntsZSEo9SnW1Z/CUFgGZd9rKSAge2QmmrQyvFrqOxzp14N9MZt1hlHmlIHJGGxRF+cWe5x5kfOHMs8X28jaEoLQaeZfkAAAAASUVORK5CYII=',
    'origin': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAAdklEQVR4nO3UwQ6AMAgDUPD//xkvcxksLGztabE3o3mGSRW5ImZmqPFkKIo7OGII3uEMOcU7rKo63hivGWcu1oKi08eLiZPQ4NP8MA/OtgaCV/VP4cr+rkoE/ysy3C0/pbrtZfSt+CaY6urGKdR59/mtsI6wlBc7hFvnMwtOtAAAAABJRU5ErkJggg==',
    'theme_dark': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAAcElEQVR4nO2USQ7AIAwDSf//Z3OJVCSyOBFVL/hsDyYsY1x9LWFMALB6RfJY6FCg64sWeLpQ9dTAiBIk3G2sos6AAlfaLpkcfEq/gctjccHC3P49k4MNtVqbYKO1C/c2yD5pMxNNrfIJvaHGOVyd1wT4UigR9j0DtgAAAABJRU5ErkJggg==',
    'theme_light': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAAAoklEQVR4nMWU2w7AIAhD6eL//zJ7mcYRwOJM1lfsoeJF5E/po4rnsoBTYQa4Q3fgnmeAASBbuIJaD6KFttkqqV3rGlVVKzvwAqSJdqEiIo2FVs/gigrp/IgmzSvOxmirAJAlT0dRkW3SslRfFAJX93lVDw8vgjBQkeSBMA37AVKJbRrmVnhBXqbKP1H2ZGOIPnydxIShwV4g6lYwsmM4Bra6AR/co+OlDlMmAAAAAElFTkSuQmCC',
    'crosshair_on': 'iVBORw0KGgoAAAANSUhEUgAAABYAAAAWCAYAAADEtGw7AAABwUlEQVR4nMWUv2tTcRTFP+f7TWxN7BAUurs6FNFRqIOjDg7p5ODURQkI7qG7IAoKbp37xrZzcRWE6tC/wUURMWmTl/c9DklKSNL8QErPeDnnfO89794HlwTNZdhC8st9b1RvUTv7wfG7p/o9rF8kC/N8m4PHg/iwep2jVOYuQD2brZ1rPIJWp0WKkd4i5GWMg0RwsUB8SxovhSs2tmXT34C4mKY0UKrpyexOMnQHQEo6dBmDEn9taysjNu2JddsRBnlgLPcLk8iAxoE/xhIPOm1IZlvSNtCd1bEAXu17QxXe5G0s9Ws2Uv+xcohsOoENIUBKfAbyEQ42LleQ27x++0TfSoPi+tpNHnVXQGMJ2tBtQ68DGEIZVitsaiw4J7h2A/60WD/P2AVfWj/Z7J2OzRMJKccSz2KZ54qUemfs5qfsEggBilF63gEXfD+PYhE0Dn1UrfGw9Yv77x/r6zx+aTCvLrr92m3Cp3v0GgfkAIqs1fccgchYxwBZnYSGWyE5m0ICaNqW5MaB+9MVpGxLRX3PZFuaqoErv7xLNk42SXH6If2PcXWlSiiK4W9gNuaSduh3mMyLs1NqIecYBl//KvAPXnWyeuxTvJYAAAAASUVORK5CYII=',
}


def _icon_bytes(name: str) -> bytes:
    return base64.b64decode(_ICON_PNG[name])


# ══════════════════════════════════════════════════════════════════════════════
# SHARED LAYER — no GUI imports, used by both backends and SVG renderer
# ══════════════════════════════════════════════════════════════════════════════

# ── colour palette ─────────────────────────────────────────────────────────────
# ULTICAP_PALETTE / palette_rgb (screen colours, per-object index lookup) live
# in kiuc_model.py as the single source of truth shared with kiuc_writer.py.
# The white/light-background colour set is intentionally NOT a per-index
# table: see _light_theme_colours() below (screen paint) and the _PRINT_*
# constants (SVG) -- both flatten to fixed per-category colours, same
# rationale as PDF/PNG export's long-standing printability choice.
_BG_RGB  = (30, 30, 30)          # #1e1e1e
_BG_HEX  = '#1e1e1e'
_SYM_RGB = (170, 0, 170)         # palette[5] magenta — default symbol colour


def _hex(colour: int) -> str:
    r, g, b = palette_rgb(colour)
    return f'#{r:02x}{g:02x}{b:02x}'


# ── geometry helpers ───────────────────────────────────────────────────────────


def _u_to_halign_valign_angle(u: int):
    """Return (halign, valign, angle_deg) for SVG/Qt use.
    halign: 'left'|'center'|'right'
    valign: 'bottom'|'center'|'top'   (bottom = text sits above point)
    angle:  0 or 90

    Thin viewer-local adapter over the canonical u_to_justify() in
    kiuc_model.py (which also returns is_vertical, unused here).
    """
    hjust, vjust, _is_v, angle = u_to_justify(u)
    return hjust, vjust, angle


def _overline_segments(text: str):
    """Split text into (chunk, is_overlined) runs per KiCad ~{...} markup.

    Any stray literal '^' characters (should not normally occur in text
    that has already passed through convert_overline(), but handled
    defensively in case some text bypasses it) are dropped rather than
    treated as an overline toggle — matching prior _strip_overline()
    behaviour for that edge case.
    """
    text = text.replace('^', '')
    segments = []
    i = 0
    n = len(text)
    while i < n:
        m = re.match(r'~\{([^}]*)\}', text[i:])
        if m:
            inner = m.group(1)
            if inner:
                segments.append((inner, True))
            i += m.end()
        else:
            j = text.find('~{', i)
            if j == -1:
                chunk = text[i:]
                if chunk:
                    segments.append((chunk, False))
                break
            else:
                chunk = text[i:j]
                if chunk:
                    segments.append((chunk, False))
                i = j
    if not segments:
        segments = [('', False)]
    return segments


def _text_display_angle(file_angle: int, comp_rot: int) -> int:
    """Effective display angle: (file_angle + comp_rotation_deg) % 180."""
    return (file_angle + (comp_rot & 3) * 90) % 180


def _arc_transform(start_deg: float, sweep_deg: float, rotation: int):
    """Apply component rotation+mirror to arc angles (screen convention)."""
    r = rotation & 3;  mirror = bool(rotation & 4)
    start_deg = (start_deg + r * 90) % 360
    if mirror:
        start_deg = (180 - start_deg) % 360
        sweep_deg = -sweep_deg
    return start_deg, sweep_deg


# ── viewport ───────────────────────────────────────────────────────────────────

class _VP:
    """Ulticap units → screen pixels.  Ulticap Y+ UP, screen Y+ DOWN."""
    def __init__(self):
        self.ox = 0.0;  self.oy = 0.0;  self.scale = 1.0

    def cx(self, x): return self.ox + x * self.scale
    def cy(self, y): return self.oy - y * self.scale
    def cs(self, u): return abs(u * self.scale)

    def wx(self, cx): return (cx - self.ox) / self.scale
    def wy(self, cy): return (self.oy - cy) / self.scale

    def zoom(self, f, cpx, cpy):
        wx = self.wx(cpx);  wy = self.wy(cpy)
        self.scale *= f
        self.ox = cpx - wx * self.scale
        self.oy = cpy + wy * self.scale

    def fit(self, x0, y0, x1, y1, cw, ch, margin=None):
        if margin is None: margin = _ZOOM_OUT_MARGIN_PX
        if x1 == x0 or y1 == y0: return
        self.scale = min((cw-2*margin)/(x1-x0), (ch-2*margin)/(y1-y0))
        self.ox = cw/2 - (x0+x1)/2 * self.scale
        self.oy = ch/2 + (y0+y1)/2 * self.scale


# ── layer definitions ──────────────────────────────────────────────────────────

_GRID_MIN_PX   = 8
_MARKER_MIN_PX = 0.20   # vp.scale threshold below which anchor markers are hidden
                        # entirely (see _show_markers) -- NOT a pixel size; that's
                        # _ANC_MARKER_MIN_PX below.

# Anchor-diamond/POE-square marker size floor (pixel space), applied after
# zoom scaling -- see _draw_diamond/_draw_poe_sq and _anc_marker_radius_units.
# The world-unit size itself now uses the same base+threshold+gentle-excess
# formula as junctions (_junc_radius_units) rather than an independent
# min/max pixel clamp -- a fixed ceiling could be caught up to and
# overtaken by a thick line that has no ceiling of its own (wire/bus width
# grows unbounded with zoom), making the marker indistinguishable from the
# line it sits on at high zoom. The junction-style formula avoids that by
# construction. Floor is 0 by design: these are a debug/inspection aid, not
# something that needs to stay visible when zoomed out (they're gated off
# entirely below _MARKER_MIN_PX anyway).
_ANC_MARKER_MIN_PX = 0

# Base size (world units) for the two markers that can sit directly on a
# possibly-thick line (wire-start, pin-conn). Junctions avoid the "same
# size as what it's marking" problem by using two different BASE radii for
# normal vs bus-entry (_JUNC_R_NORMAL_UNITS=6 vs _JUNC_R_BUS_UNITS=8), not
# just the threshold+excess term -- the excess only grows beyond a fixed
# threshold, and at the tunables' own defaults that threshold exactly
# equals the default thick width, so relying on excess alone gives zero
# visible difference by default. These two bases restore that same
# always-different-by-default distinction for wire-start/pin-conn.
_ANC_MARKER_BASE_THIN  = 4
_ANC_MARKER_BASE_THICK = 6

# ── Cursor snap (user-configurable, see _SnapSpinBox) ──────────────────────────
# Stored/edited in mm (see toolbar spinbox); converted to internal 'u' once per
# change and used as the divisor in _update_status's round(wx/snap_u)*snap_u.
# This is display/readout-only — the viewer never writes coordinates back to the
# model or file, so a non-integer 'u' value here is safe (unlike sheet.grid,
# which must stay an integer because it comes straight from the file).
_SNAP_MM_DEFAULT = 0.254        # 5 U — 1/5 of the common 50 mil (1.27mm) grid spacing
_SNAP_MM_STEP    = MM_PER_UNIT  # 1 U (0.0508mm) — the database's real, indivisible
                                 # unit. Every offered step is therefore always a
                                 # whole number of U's, so a snapped position is
                                 # never a half-unit — this is what keeps abs's u
                                 # and mm columns in exact agreement for any step.
_SNAP_MM_MIN     = 0.0          # 0 == free/unsnapped cursor movement
_SNAP_MM_MAX     = 1.27         # 25 U — one full 50 mil grid spacing
_INI_SNAP_SECTION = 'viewer_snap'
_INI_SNAP_KEY      = 'value_mm'

_FLOOR_EPS = 1e-6   # nudge for float round-off before flooring to whole U's —
                    # see _floor_u(); far smaller than any real position
                    # difference, large enough to absorb chained-division noise

def _floor_u(v):
    """Floor to the nearest whole Ulticap unit for status-bar display.

    Values reaching here are computed through chains of float division
    (screen-to-world mapping, snap-increment division), so a value that is
    mathematically exactly on a unit boundary (e.g. 2.0) can arrive as
    1.999999999999. A bare math.floor() would then report the wrong cell.
    Nudging by _FLOOR_EPS before flooring corrects that without affecting
    any genuinely non-boundary value.
    """
    return math.floor(v + _FLOOR_EPS)

# ── Non-colour rendering constants (all in one place for easy in-line tuning) ──
_ZOOM_WHEEL_FACTOR      = 1.15   # scale multiplier per wheel-notch
_ZOOM_SETTLE_MS         = 150    # wheel idle this long: zoom preview ends, exact render
_PAN_IMAGE_MAX_PIXELS   = 24_000_000   # device pixels (~96 MB) of the image a pan
                                       # moves over -- see SchCanvas._make_pan_image
_ZOOM_OUT_MARGIN_PX     = 40     # margin used when fitting content for min zoom
_ZOOM_MAX_GRID_UNITS    = 10     # max zoom: this many grid units span the viewport width
_GRID_FALLBACK_MIN      = 5      # below this, sheet.grid is considered unset/bogus
_GRID_FALLBACK_DEFAULT  = 25     # ...and this value is used instead
_TEXT_SCALE_DEFAULT     = 1.10   # world-unit-size -> pixel font-size multiplier
_TEXT_REF_PX            = 100    # font pixel size text is laid out at, then scaled
                                 # to its true size (SchCanvas._text_font)
_MIN_SVG_FONT_SIZE      = 4      # floor for SVG font-size attribute (world-unit domain, not px)
_NEGATION_BUBBLE_R      = 15.0   # active-low pin negation-circle radius (world units)
_ORIGIN_CROSSHAIR_ARM_PX = 10    # origin (0,0) crosshair arm half-length
_SNAP_CROSSHAIR_ARM_PX  = 8      # snap-cursor crosshair base arm length (drawn at +/-2x)
_REF_CROSSHAIR_ARM_PX   = 6      # relative-origin marker crosshair arm length
_PNG_EXPORT_DPI         = 300
_PDF_EXPORT_DPI         = 150
_TIGHT_BOUNDS_GAP_UNITS = 100    # clearance (Ulticap units, 200 mils) between tight
                                 # content bounds and the border rectangle — covers
                                 # worst-case rotated attribute text overhang; shared
                                 # by the SVG/PNG/PDF exporters and the on-screen
                                 # border/fit-sheet paint (was duplicated 5x)
_ZOOM_SYMBOL_MARGIN_UNITS = 150  # extra half-size margin (Ulticap units) added around
                                 # a symbol's own body when framing it for the Signals
                                 # panel's 'zoom to symbol' click-to-locate
_ZOOM_LOOSE_EXTENT_UNITS = 1800  # half-width framing (Ulticap units) for 'Jump to
                                 # sheet origin' and the toolbar text-search's
                                 # zoom-to-fit -- deliberately looser than
                                 # _ZOOM_SYMBOL_MARGIN_UNITS's tight component
                                 # framing so surrounding context stays visible,
                                 # unlike the Signals/Refdes panels' tight
                                 # click-to-locate
_LABEL_HIT_RADIUS_PX = 10       # screen-pixel click tolerance around a *X label's
                                 # anchor, or a *A annotation's anchor/POE -- they
                                 # have no bounding box the way a symbol does, so
                                 # proximity substitutes for the bbox hit-test
_DIM_OPACITY = 0.25             # rest-of-schematic opacity while a *X/*A label is
                                 # click-inspected (see _inspect_annot)
_NOT_FITTED_OPACITY = 0.4       # a part left out of the shown schematic variant:
                                 # drawn faded, its texts in italics (Component.fitted)

# Symbol names known to be non-part placeholders (title-block stamps etc.),
# excluded from the Components list even though the writer/model treat them
# as ordinary components. NOT for mounting holes, heatsinks, IC sockets, or
# anything else that's a genuine physical part just without (m)any pins --
# only for things that categorically aren't a part at all. Matched
# case-insensitively against the *S symbol name. Add further names here as
# they're discovered; kept separate from is_pwr_symbol/is_port_sym since
# those are structural (any pin/attribute layout of that shape), while this
# is specific named symbols.
_NON_PART_SYMBOL_NAMES = {
    'TITLE',
}
_TEXT_FONT_FAMILY       = 'ISOCTEUR'  # Ulticap V4.91's DOS font is closely matched by
                                 # this ISO 3098/DIN 6776 engineering-lettering font,
                                 # used for actual glyph rendering only — box POSITIONING
                                 # below is governed by Ulticap's own measured convention,
                                 # independent of whichever font renders the glyphs.
                                 # Qt/browsers silently substitute a fallback if it isn't
                                 # installed, so a missing font won't crash — but visual
                                 # fidelity (not positioning) depends on it being present.
# Shown once at start when ISOCTEUR is missing (_run_qt), as Qt's silent
# substitution would otherwise leave the different look unexplained.
_FONT_MISSING_TEXT = (
    'The viewer draws texts in ISOCTEUR, an engineering-lettering font that '
    "closely matches Ulticap's own DOS lettering. ISOCTEUR is not installed "
    'on this computer, so another font is used: texts look different and can '
    'sit slightly off where they are aligned right or centred.\n\n'
    'Install ISOCTEUR for the closest text rendering (see Fonts in KIUT\'s '
    'README). The KiCad output does not depend on it.')

# Ulticap's own virtual-bounding-box model, measured directly from the DOS
# program at its highest zoom level (all units are Ulticap native units,
# i.e. 1/500 inch, at the reference size below). Ulticap always positions a
# box per halign/valign (same as _u_to_halign_valign_angle), then draws the
# Ulticap text-box model constants/helpers (_UC_*, _uc_box_metrics, etc.)
# now live in kiuc_model.py, shared with kiuc_writer.py — see that module
# for the model description. Imported at the top of this file alongside
# the other kiuc_model imports.
_PDF_PAGE_MARGIN_MM     = 2      # margin (mm) between the schematic border rectangle
                                 # and the PDF page edge, on all page sizes — PDF
                                 # viewers add their own printer margin on top

# Dash patterns shared by the SVG and Qt renderers for UserLine/Polyline/Circle-arc
# linetypes. Each object type encodes the same three visual styles under different
# numeric codes, so the codes map to these three shared tuples rather than each
# renderer defining its own copy of [6,4] / [6,2,2,2] / [2,4].
_DASH_DASH    = [6, 4]
_DASH_DASHDOT = [6, 2, 2, 2]
_DASH_DOT     = [2, 4]
_DASH_BY_USERLINE_TYPE = {256: _DASH_DASH, 512: _DASH_DASHDOT, 768: _DASH_DOT}

# Qt dash pattern by canonical style name. The Ulticap code → style-name
# lookup itself lives in kiuc_model (UC_POLYLINE_STYLE_BY_CODE /
# UC_ARC_STYLE_BY_CODE), shared with kiuc_writer.py so a new code only
# needs to be taught to one table. 'solid' is intentionally absent —
# solid lines use no dash pattern.
_DASH_BY_STYLE_NAME = {
    'dash': _DASH_DASH, 'dash_dot': _DASH_DASHDOT, 'dot': _DASH_DOT,
}

def _svg_dash(d):
    """Render a Qt-style dash list (e.g. [6,4]) as an SVG dasharray string."""
    return ','.join(str(v) for v in d)

# ── Ulticap semantic colours (screen-measured palette indices) ─────────────────
# These match the palette above; named here so a single change propagates everywhere.
_COL_WIRE          = '#aa0000'  # palette[4]  red           — net wires
_COL_BUS           = '#2020eb'  # palette[9]  light blue    — buses
_COL_JUNCTION      = '#00aa00'  # palette[2]  green         — junction dots
_COL_STUB          = '#00aaaa'  # palette[3]  cyan          — pin stubs / power-symbol default
_COL_STUB_RGB      = 'rgb(0,170,170)'  # same in CSS rgb() form for SVG renderer

# ── Viewer UI chrome colours (not schematic palette colours) ───────────────────
_COL_BORDER        = '#445566'  # sheet border rectangle
_COL_ORIGIN        = '#556677'  # origin 0,0 crosshair marker
_COL_GRID          = '#2a2a3a'  # grid lines
_COL_MISSING       = '#ff4444'  # missing-symbol x marker
_COL_ATTR_FALLBACK = '#e0e0a0'  # component attribute text with no colour set
_COL_ATTR_FALLBACK_RGB = 'rgb(224,224,160)'  # same in CSS rgb() form for SVG renderer
_COL_TEXT_DEFAULT  = '#ffffff'  # label / annotation default text colour
_COL_TEXT_DEFAULT_RGB = 'rgb(255,255,255)'  # same in CSS rgb() form for SVG renderer
_COL_SNAP          = '#a8a8a8'  # cursor snap crosshair

# ── Print palette — white background, dark legible colours for PDF export ─────
# Symbol bodies and pin stubs carry their own colour; the constants below are
# the *fallback* values used when the object colour field is 0 (the common case
# in most Ulticap files).
_PRINT_BG          = '#ffffff'
_PRINT_WIRE        = '#8b0000'   # dark red    (screen: red)
_PRINT_BUS         = '#00008b'   # dark blue   (screen: light blue)
_PRINT_JUNCTION    = '#006400'   # dark green   (screen: green)
_PRINT_STUB        = '#006666'   # dark teal    (screen: cyan)
_PRINT_SYM_BODY    = '#660066'   # dark magenta (screen: magenta)
_PRINT_ATTR        = '#5c5c00'   # dark olive   (screen: yellowish-cream — resembles original hue)
_PRINT_TEXT        = '#000000'   # black        (screen: white)
_PRINT_PIN_NAME    = '#006400'   # dark green   (screen: green — matches junction colour)
_PRINT_PIN_NUMBER  = '#8b0000'   # dark red     (screen: red  — matches wire colour)
_PRINT_BORDER      = '#aaaaaa'   # light grey   (screen: slate)
_PRINT_ORIGIN      = '#888888'   # mid grey     (screen: slate)
_PRINT_GRID        = '#d8d8e2'   # pale grey    (screen: near-black blue-grey)


# ── Anchor / POE overlay colours (debug markers) ──────────────────────────────
# Deliberately mid-luminance (not maxed for one background) so a single set
# reads clearly on both the dark and light canvas themes -- unlike schematic
# content colours, these are an overlay we invented ourselves, not Ulticap
# palette values, so there's no authenticity reason to tune them per-theme.
_COL_ANC_COMP  = '#22b2e1'   # symbol origin
_COL_ANC_WIRE  = '#1fce59'
_COL_ANC_PIN   = '#d67b20'
_COL_ANC_ANN   = '#b726ff'
_COL_ANC_PNAME = '#20d820'   # pin name anchor
_COL_ANC_PNUM  = '#ff2626'   # pin number anchor
_COL_ANC_ATTR  = '#a39718'   # all other comp attrs (REFDES, VALUE, DEVICE, ...)
_COL_POE       = '#b48e1b'

_LAYERS = [
    ('sheet_border',  'Sheet border',      True,  _COL_BORDER),
    ('grid',          'Grid (25 u)',        True,  _COL_GRID),
    ('wires',         'Wires',             True,  _COL_WIRE),
    ('buses',         'Buses',             True,  _COL_BUS),
    ('user_lines',    'User lines',        True,  '#7d5514'),   # palette[6] brown screen-measured
    ('symbols',       'Symbols',           True,  '#aa00aa'),   # palette[5] magenta screen-measured
    ('pin_names',     'Pin names',         True,  '#9cdcfe'),   # indented under symbols
    ('pin_numbers',   'Pin numbers',       True,  '#9cdcfe'),   # indented under symbols
    ('refdes',        'Reference',         True,  _COL_ATTR_FALLBACK),   # indented under symbols
    ('value',         'Value',             True,  _COL_ATTR_FALLBACK),   # indented under symbols
    ('junctions',     'Junctions',         True,  _COL_JUNCTION),
    ('labels',        'Text (*X)',          True,  _COL_JUNCTION),
    ('annotations',   'Annotations (*A)',  True,  '#c8c8c8'),
    ('annotations_hidden', 'Hidden annotations', False, '#c8c8c8'),   # under its parent row
    ('anc_comp',      '⬦ Symbol origin',   True,  _COL_ANC_COMP),
    ('anc_pin',       '⬦ Pin conn.',        True,  _COL_ANC_PIN),
    ('anc_pin_name',  '⬦ Pin name anchor', True,  _COL_ANC_PNAME),
    ('anc_pin_number','⬦ Pin number anchor', True, _COL_ANC_PNUM),
    ('anc_attr',      '⬦ Comp attr anchor', True, _COL_ANC_ATTR),
    ('poe_comp',      '□ Comp attr POE',    True,  _COL_POE),
    ('anc_wire',      '⬦ Wire start',      False, _COL_ANC_WIRE),
    ('anc_ann',       '⬦ Ann./lbl anchor', True,  _COL_ANC_ANN),
    ('poe_ann',       '□ Ann. POE',         True,  _COL_POE),
]

# Texts Ulticap stores as hidden (visibility bit 7 clear) are not drawn, as in
# Ulticap. They are shown on demand, in italic at full brightness:
#   - a symbol's hidden pin names, pin numbers, reference and value only for
#     the click-inspected symbol, per group, through the "hidden" toggles that
#     appear on those rows (and one on Symbols for all four) while a symbol is
#     inspected -- drawn for the whole sheet, they land inside neighbouring
#     symbols and nobody can tell what belongs where;
#   - hidden *A annotations through the "Hidden annotations" row, which takes
#     effect while Annotations is on.
# Other hidden attributes always stay hidden (too much overlap to read); *X
# texts have no visibility field.
_SYMBOL_TEXT_GROUPS = ('pin_names', 'pin_numbers', 'refdes', 'value')
_HIDDEN_LAYER_OF = {'annotations': 'annotations_hidden'}
_HIDDEN_PARENT_OF = {v: k for k, v in _HIDDEN_LAYER_OF.items()}


def _hidden_group(tag: str, is_power: bool = False):
    """The group (_SYMBOL_TEXT_GROUPS) whose "hidden" toggle reveals a hidden
    attribute with this tag, or None for one that always stays hidden. A
    power symbol's net name (its LABEL/WIRELABEL) is not a pin name."""
    if tag in ('LABEL', 'WIRELABEL'):
        return None if is_power else 'pin_names'
    if tag.startswith('#'):
        return 'pin_numbers'
    if tag == 'REFDES':
        return 'refdes'
    if tag == 'VALUE':
        return 'value'
    return None


# A symbol text drawn outside the symbol's outline -- a pin name moved away
# from its pin, a symbol-level UT_NETNAME or LABELn sitting next to a wire
# (some older lesson designs) -- reads like a net label but is not one. While its
# symbol is inspected it gets a dotted connector, in its own colour, to where
# it belongs, ending in an open circle; clicking it selects the symbol and
# the status bar says what it is.
def _text_pin(comp, sym, dx_poe, dy_poe):
    """The symbol pin a text belongs to (its point of effect is that pin's
    connection point), or None for a symbol-level text."""
    if sym is None:
        return None
    return next((p for p in sym.pins if _pin_conn_point(p, sym) == (dx_poe, dy_poe)), None)


def _text_owner_point(comp, sym, dx_poe, dy_poe):
    """World point a detached text's connector ends at: for a pin's text
    the point where the pin meets the symbol body -- not its connection
    point, which sits on the wire and would make the text look like a net
    label -- for any other text its point of effect, the symbol's centre."""
    pin = _text_pin(comp, sym, dx_poe, dy_poe)
    lx, ly = (pin.x, pin.y) if pin is not None else (dx_poe, dy_poe)
    ox, oy = _rot_transform(lx, ly, comp.rotation)
    return comp.x + ox, comp.y + oy


def _text_is_detached(comp, sym, wx, wy) -> bool:
    """Whether a symbol text's anchor (world wx, wy) lies outside the
    symbol's outline (its body box)."""
    x0, y0, x1, y1 = _comp_bbox_world(comp, sym)
    return not (x0 <= wx <= x1 and y0 <= wy <= y1)


def _describe_variants(comp, sheet) -> str:
    """The status-bar line for a part of a design with schematic variants:
    the shown variant only, as <reference> - <symbol variant>/<document
    variant>: <value> (R1 - SYMVAR1/DEFAULT: 10k), symbol variant first as
    the file nests them; a part left out of the shown variant is None and
    drawn faded. Every variant: the Variants window (_variant_table). Empty
    for a design without variants."""
    shown = getattr(sheet, 'shown_variant', None) if sheet is not None else None
    if not shown:
        return ''
    ref = comp.display_refdes()
    if not comp.fitted:
        return f'{ref} - None/{shown} -- drawn faded (KiCad: do not populate)'
    return f'{ref} - {comp.variant_name or "?"}/{shown}: {comp.value or ""}'


def _variant_table(comp, sheet):
    """(summary, rows) for the Variants window: one row per symbol variant of
    the part -- (symbol variant, its document variants, value), the shown
    document variant marked -- and one row (None) for the variants the part
    is left out of; the summary says what matters: placed or not in the
    shown variant, and where it is left out."""
    shown = getattr(sheet, 'shown_variant', None) if sheet is not None else None
    ref = comp.display_refdes()

    def _docs(names):
        return ', '.join(f'{n} (shown)' if n == shown else n for n in names)

    rows = [(symvar or '?', _docs(docs), value) for symvar, docs, value in comp.variant_rows]
    absent = ([shown] if not comp.fitted else []) + list(comp.not_in_variants)
    if absent:
        rows.append(('None', _docs(absent), 'not fitted'))
    if not comp.fitted:
        summary = (f'{ref} is not placed in {shown}, the shown variant: drawn faded; '
                   f'KiCad: do not populate (not in BOM, not on board).')
    else:
        summary = f'{ref} is placed in {shown}, the shown variant.'
        if comp.not_in_variants:
            summary += f' Not placed in {", ".join(comp.not_in_variants)}.'
    return summary, rows


def _describe_comp_text(comp, sym, tag, val, dx_poe, dy_poe, hidden, is_power) -> str:
    """One status-bar line saying what a symbol's text is -- above all,
    whether it names a net (Ulticap's rule, FILEFORMAT_ULTICAP_UTSCH.md §9.7: only a wire
    label on a wire/bus, or a pin's own net name, does)."""
    ref = comp.display_refdes()
    pin = _text_pin(comp, sym, dx_poe, dy_poe)
    num = ''
    if pin is not None:
        num = next((ca.value for ca in comp.comp_attrs
                    if ca.tag.startswith('#') and (ca.dx_poe, ca.dy_poe) == (dx_poe, dy_poe)
                    and ca.value), '') or (pin.number or '').split(',')[0]
    at_pin = f'{ref} pin {num}' if num else ref
    if tag.startswith('#'):
        what = f'Pin number of {at_pin}'
    elif tag == 'LABEL' and pin is not None and not is_power:
        what = f'Pin name of {at_pin}, drawn outside the symbol \u2014 names no net'
    elif tag in ('WIRELABEL', 'LABEL') and (pin is not None or is_power):
        what = f'Net name of {at_pin} \u2014 names the net on this pin'
    elif tag in ('WIRELABEL', 'LABEL', 'UT_NETNAME') or (tag.startswith('LABEL') and tag[5:].isdigit()):
        where = f'at {at_pin}' if pin is not None else f'of {ref}, not on a pin'
        what = f'{tag} text {where} \u2014 names no net (Ulticap\u2019s netlist ignores it)'
    elif tag == 'REFDES':
        what = f'Reference of {ref}'
    elif tag == 'VALUE':
        what = f'Value of {ref}'
    else:
        what = f'{tag} attribute of {ref}'
    return what + (' (hidden in the file)' if hidden else '')


# Marker types that actually apply to a component (as opposed to wires or
# annotations) -- these are the ones that get a secondary per-component
# toggle in the "Selected" column when something is inspected (see
# _on_inspect_click / _paint_component's force_inspect handling).
_COMP_INSPECT_KEYS = {'anc_comp', 'anc_pin', 'anc_pin_name', 'anc_pin_number',
                      'anc_attr', 'poe_comp'}


# ── ini persistence (kiut_viewer.ini, the viewer's own) ────────────

import configparser as _cp

from kiut import APP_DIR, CREDIT, VERSION   # noqa: E402  the KIUT folder: kiut_viewer.ini is kept there
_INI_FILE              = APP_DIR / 'kiut_viewer.ini'
# Until KIUT stage 10a the viewer kept its sections in KIUC's kiuc.ini, next to its own file: copied
# from there once, when the viewer's own file isn't there yet; kiuc.ini is left as it is.
_OLD_INI_NAME          = 'kiuc.ini'
from kiut.core import settings  # noqa: E402
_INI_LAYERS_SECTION    = 'viewer_layers'
_INI_THEME_SECTION     = 'viewer_theme'
_INI_THEME_KEY         = 'theme'
_INI_RECENT_SECTION    = 'viewer_recent'
_INI_EXPORT_PNG_SECTION = 'viewer_export_png'
# The viewer's own line widths -- deliberately NOT the converter's [tuning]
# keys of the same name: sharing them let a viewer toolbar change silently
# alter the next conversion's symbol line widths.
_INI_LINE_WIDTH_SECTION = 'viewer_line_widths'
# Start-up notices the user chose not to see again ("Do not show this message
# again"), one key per notice, '1' = hidden.
_INI_NOTICES_SECTION   = 'viewer_notices'
_NOTICE_FONT_MISSING   = 'text_font_missing'
# View > Show tooltips (on by default): users who know the viewer can
# switch the hover tooltips off.
_INI_TOOLTIPS_SECTION  = 'viewer_tooltips'
_INI_TOOLTIPS_KEY      = 'show'
_MAX_RECENT            = 10

# The viewer's thin/thick line widths, in Ulticap units (real mil = this / 2;
# see kiuc_writer's _sym_line_width_mm). Defaults are Ulticap DOS's two fixed
# symbol line widths, 6 and 30 -- built into the DOS program, not stored in a
# .SCH file or in ULTIC.SET. The viewer uses them for more than symbol lines:
# thin for wires and pin stubs, thick for buses, and both for junction size
# (_thin_thick_width_units), on screen and in its SVG/PNG/PDF export. Saved in
# the viewer's own kiut_viewer.ini section, independent of the converter.
THIN_LINE_WIDTH  = 6.0
THICK_LINE_WIDTH = 30.0

# Editable range for the Thin/Thick spinboxes, in Ulticap's own raw units.
# Ulticap only stores even values here (see kiuc_writer's THIN_LINE_WIDTH/
# THICK_LINE_WIDTH comment for the /2-real-mil relationship); minimum 2
# avoids a zero/negative real width, maximum 400 (= 5.08mm real) is already
# implausibly thick for a schematic symbol line.
_LINE_WIDTH_MIN  = 2
_LINE_WIDTH_MAX  = 400
_LINE_WIDTH_STEP = 2


def _load_line_width_tuning() -> None:
    """Load THIN_LINE_WIDTH/THICK_LINE_WIDTH from the viewer's own
    [viewer_line_widths] section if present, leaving the built-in defaults
    otherwise. The converter's [tuning] keys of the same name are never
    read here."""
    global THIN_LINE_WIDTH, THICK_LINE_WIDTH
    cfg = _ini_load()
    if not cfg.has_section(_INI_LINE_WIDTH_SECTION):
        return
    for name in ('THIN_LINE_WIDTH', 'THICK_LINE_WIDTH'):
        if cfg.has_option(_INI_LINE_WIDTH_SECTION, name):
            try:
                value = cfg.getfloat(_INI_LINE_WIDTH_SECTION, name)
            except ValueError:
                continue
            if name == 'THIN_LINE_WIDTH':
                THIN_LINE_WIDTH = value
            else:
                THICK_LINE_WIDTH = value


def _save_line_width_tuning() -> None:
    """Persist the current THIN_LINE_WIDTH/THICK_LINE_WIDTH to the viewer's
    own [viewer_line_widths] section -- never to the converter's [tuning]."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_LINE_WIDTH_SECTION):
        cfg.add_section(_INI_LINE_WIDTH_SECTION)
    cfg.set(_INI_LINE_WIDTH_SECTION, 'THIN_LINE_WIDTH', str(THIN_LINE_WIDTH))
    cfg.set(_INI_LINE_WIDTH_SECTION, 'THICK_LINE_WIDTH', str(THICK_LINE_WIDTH))
    _ini_save(cfg)


def _thin_thick_width_units(is_thick: bool) -> float:
    """Line width, in world (Ulticap coordinate) units, for anything driven
    by the THIN_LINE_WIDTH/THICK_LINE_WIDTH tunables -- wires, buses,
    stubs, junction radius, symbol-body graphics (polylines/circles) and
    .SCH sheet lines.
    Used by both the interactive on-screen view (scaled through vp.cs()
    so it responds to zoom, the way junction dots already did before these
    tunables existed) and the SVG export path (used directly as SVG
    stroke-width, since SVG's viewBox is itself in world units and its
    width/height attributes carry real mm dimensions -- see _render_svg's
    <svg> root -- so a world-unit stroke-width already has a well-defined
    physical size with no separate export-vs-screen distinction needed).

    raw is in Ulticap units; real mil = raw/2 (see
    _sym_line_width_mm in kiuc_writer.py for the empirical basis); world
    units = real_mil * MM_PER_MIL / MM_PER_UNIT, which simplifies to raw/4.
    """
    raw = THICK_LINE_WIDTH if is_thick else THIN_LINE_WIDTH
    return raw / 4


def _user_arc_geometry(ua):
    """(cx, cy, r, start_deg, sweep_deg) of a .UTSCH sheet arc: it runs
    counter-clockwise from its stored start point to its end point around
    its centre, at the start point's radius -- the same reading the
    converter's _userarc_sexp uses. A full circle has a 360 degree sweep."""
    r = math.hypot(ua.x1 - ua.cx, ua.y1 - ua.cy)
    a0 = math.degrees(math.atan2(ua.y1 - ua.cy, ua.x1 - ua.cx))
    a1 = math.degrees(math.atan2(ua.y2 - ua.cy, ua.x2 - ua.cx))
    sweep = 360.0 if ua.is_full_circle else (a1 - a0) % 360.0
    return ua.cx, ua.cy, r, a0, sweep


def _pen_units(pen_width, is_thick: bool) -> float:
    """Line width, in world units, of a drawing object: a .UTSCH object's own
    stored pen width when it has one -- in Ulticap units (2 mil), the same
    as a world unit, so used as-is, matching the converter's _pen_width_mm
    -- else the viewer's thin/thick width."""
    return pen_width if pen_width is not None else _thin_thick_width_units(is_thick)


# All junction dot sizing constants live together here (all world units
# unless noted). _JUNC_R_NORMAL_UNITS/_JUNC_R_BUS_UNITS are also still used
# as-is by the SVG/PNG/PDF export path (fixed, not zoom-scaled there).
_JUNC_R_NORMAL_UNITS    = 6      # base/floor radius, normal junction
_JUNC_R_BUS_UNITS       = 8      # base/floor radius, bus-entry junction

# For on-screen zoom-scaled rendering only (see _junc_radius_units below):
# the base radii above are reused as a FLOOR -- the old fixed sizes already
# read well, so a wire/bus width at or below the threshold doesn't grow the
# dot at all, only shrink-with-zoom like before. Only width beyond the
# threshold adds any extra radius, and only gently (_JUNC_EXCESS_SCALE), so
# cranking up THICK_LINE_WIDTH doesn't balloon into an oversized blob the
# way a straight proportional scale did. At the Thin/Thick tunables' own
# defaults this reproduces the old fixed radii exactly (6/8 units) --
# nothing changes on screen unless those spinboxes are moved from default.
_JUNC_WIDTH_THRESHOLD_UNITS = 7.5   # = default THICK_LINE_WIDTH/4 (see _thin_thick_width_units)
_JUNC_EXCESS_SCALE          = 0.5   # multiplier applied only to width beyond the threshold

# Pixel-space floor, applied after zoom scaling, so a junction dot never
# shrinks below this even when heavily zoomed out on a large sheet with
# thin wires -- independent of the two constants above, which only affect
# the world-unit radius before zoom is applied.
_JUNC_MIN_RADIUS_PX = 2.5


def _junc_radius_units(is_bus_entry: bool) -> float:
    base = _JUNC_R_BUS_UNITS if is_bus_entry else _JUNC_R_NORMAL_UNITS
    width = _thin_thick_width_units(is_bus_entry)
    excess = max(0.0, width - _JUNC_WIDTH_THRESHOLD_UNITS)
    return base + excess * _JUNC_EXCESS_SCALE


def _anc_marker_radius_units(base: float, is_thick: bool) -> float:
    """Anchor-marker (diamond/POE-square) half-size, in world units, using
    the exact same base+threshold+gentle-excess shape as junctions (see
    _junc_radius_units) rather than an independent min/max pixel clamp.
    base is the marker's own pre-existing size (the old fixed 3/4 px
    values, now reinterpreted as world units, same as junctions reused
    their old fixed 6/8 unit radii). is_thick only matters for markers
    that can sit directly on a wire/bus segment (wire-start, pin-conn);
    everything else always passes False, since their position is never
    coincident with a possibly-thick line and they have no natural
    thick/thin concept of their own.
    """
    width = _thin_thick_width_units(is_thick)
    excess = max(0.0, width - _JUNC_WIDTH_THRESHOLD_UNITS)
    return base + excess * _JUNC_EXCESS_SCALE

# PNG export layer defaults — all on except grid and debug overlays
_PNG_EXPORT_DEFAULTS: dict = {
    key: (False if (key == 'grid' or key.startswith('anc_') or key.startswith('poe_'))
          else default)
    for key, _label, default, _col in _LAYERS
}


def _ini_load() -> _cp.ConfigParser:
    if not _INI_FILE.exists():
        _ini_take_old()
    return settings.load(_INI_FILE)            # kiut.core.settings


def _ini_take_old() -> bool:
    """The viewer's [viewer_...] sections copied once from kiuc.ini next to
    its settings file (where the viewer kept them until KIUT stage 10a) into
    its own file; kiuc.ini left as it is. Whether anything was copied."""
    old = _INI_FILE.parent / _OLD_INI_NAME
    if not old.is_file():
        return False
    try:
        src = settings.load(old)
    except Exception:              # an unreadable kiuc.ini: the viewer starts with its defaults
        return False
    cfg = _cp.ConfigParser()
    for section in src.sections():
        if section.startswith('viewer_'):
            cfg[section] = dict(src.items(section))
    if not cfg.sections():
        return False
    _ini_save(cfg)
    return _INI_FILE.exists()


def about_text() -> str:
    """The About box's text (Help -> About)."""
    return (f'KIUT Viewer {VERSION}\n\nPart of KIUT, the KiCad Ulticap/Ultiboard import tools.\n'
            'Shows Ulticap .SCH, .BLK and .UTSCH files as Ulticap draws them, and exports them '
            'as SVG, PNG and PDF.\n\n'
            f'{CREDIT}\n\nCopyright (C) 2026 Snieffy\n'
            'GNU General Public License version 3 or later.')


def _ini_save(cfg: _cp.ConfigParser):
    try:
        settings.save(_INI_FILE, cfg)
    except OSError:
        pass   # non-fatal — viewer works fine without persistence


def load_layer_visibility() -> dict:
    """Return {key: bool} from [viewer_layers], or {} if section absent."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_LAYERS_SECTION):
        return {}
    result = {}
    for key, _label, default, _col in _LAYERS:
        if cfg.has_option(_INI_LAYERS_SECTION, key):
            try:
                result[key] = cfg.getboolean(_INI_LAYERS_SECTION, key)
            except ValueError:
                result[key] = default
    return result


def save_layer_visibility(vis: dict):
    """Persist {key: bool} to [viewer_layers] in the shared ini."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_LAYERS_SECTION):
        cfg.add_section(_INI_LAYERS_SECTION)
    for key, _label, _default, _col in _LAYERS:
        if key in vis:
            cfg.set(_INI_LAYERS_SECTION, key, '1' if vis[key] else '0')
    _ini_save(cfg)


def load_export_png_layers() -> dict:
    """Return {key: bool} for PNG export layer selection.
    Falls back to _PNG_EXPORT_DEFAULTS for any key absent from the INI."""
    cfg = _ini_load()
    result = dict(_PNG_EXPORT_DEFAULTS)   # start from built-in defaults
    if cfg.has_section(_INI_EXPORT_PNG_SECTION):
        for key in result:
            if cfg.has_option(_INI_EXPORT_PNG_SECTION, key):
                try:
                    result[key] = cfg.getboolean(_INI_EXPORT_PNG_SECTION, key)
                except ValueError:
                    pass
    return result


def save_export_png_layers(vis: dict):
    """Persist {key: bool} PNG export layer selection to INI."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_EXPORT_PNG_SECTION):
        cfg.add_section(_INI_EXPORT_PNG_SECTION)
    for key, _label, _default, _col in _LAYERS:
        if key in vis:
            cfg.set(_INI_EXPORT_PNG_SECTION, key, '1' if vis[key] else '0')
    _ini_save(cfg)


def load_theme() -> str:
    """Return the persisted viewer canvas theme, 'dark' (default) or 'light'."""
    cfg = _ini_load()
    if cfg.has_section(_INI_THEME_SECTION):
        val = cfg.get(_INI_THEME_SECTION, _INI_THEME_KEY, fallback='dark')
        if val in ('dark', 'light'):
            return val
    return 'dark'


def save_theme(theme: str):
    """Persist the viewer canvas theme ('dark' or 'light') to [viewer_theme]."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_THEME_SECTION):
        cfg.add_section(_INI_THEME_SECTION)
    cfg.set(_INI_THEME_SECTION, _INI_THEME_KEY, theme)
    _ini_save(cfg)


def notice_hidden(key: str) -> bool:
    """True when the start-up notice `key` was dismissed with "Do not show
    this message again" ([viewer_notices])."""
    cfg = _ini_load()
    try:
        return cfg.getboolean(_INI_NOTICES_SECTION, key, fallback=False)
    except ValueError:
        return False


def hide_notice(key: str):
    """Persist that the start-up notice `key` is not to be shown again."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_NOTICES_SECTION):
        cfg.add_section(_INI_NOTICES_SECTION)
    cfg.set(_INI_NOTICES_SECTION, key, '1')
    _ini_save(cfg)


def load_show_tooltips() -> bool:
    cfg = _ini_load()
    try:
        return cfg.getboolean(_INI_TOOLTIPS_SECTION, _INI_TOOLTIPS_KEY, fallback=True)
    except ValueError:
        return True


def save_show_tooltips(show: bool):
    cfg = _ini_load()
    if not cfg.has_section(_INI_TOOLTIPS_SECTION):
        cfg.add_section(_INI_TOOLTIPS_SECTION)
    cfg.set(_INI_TOOLTIPS_SECTION, _INI_TOOLTIPS_KEY, 'yes' if show else 'no')
    _ini_save(cfg)


def load_snap_mm() -> float:
    """Return the persisted cursor-snap value in mm, or _SNAP_MM_DEFAULT if unset/invalid."""
    cfg = _ini_load()
    if cfg.has_section(_INI_SNAP_SECTION) and cfg.has_option(_INI_SNAP_SECTION, _INI_SNAP_KEY):
        try:
            v = cfg.getfloat(_INI_SNAP_SECTION, _INI_SNAP_KEY)
            if _SNAP_MM_MIN <= v <= _SNAP_MM_MAX:
                return v
        except ValueError:
            pass
    return _SNAP_MM_DEFAULT


def save_snap_mm(value_mm: float):
    """Persist the cursor-snap value (mm) to [viewer_snap] in the shared ini."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_SNAP_SECTION):
        cfg.add_section(_INI_SNAP_SECTION)
    cfg.set(_INI_SNAP_SECTION, _INI_SNAP_KEY, f'{value_mm:.3f}')
    _ini_save(cfg)


def load_recent_files() -> List[Path]:
    """Return up to _MAX_RECENT Paths from [viewer_recent], existing only."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_RECENT_SECTION):
        return []
    paths = []
    for i in range(_MAX_RECENT):
        val = cfg.get(_INI_RECENT_SECTION, f'file{i}', fallback='')
        if val:
            paths.append(Path(val))
    return paths


def save_recent_files(paths: List[Path]):
    """Persist up to _MAX_RECENT Paths to [viewer_recent]."""
    cfg = _ini_load()
    if not cfg.has_section(_INI_RECENT_SECTION):
        cfg.add_section(_INI_RECENT_SECTION)
    # Clear old entries first
    for i in range(_MAX_RECENT):
        cfg.remove_option(_INI_RECENT_SECTION, f'file{i}')
    for i, p in enumerate(paths[:_MAX_RECENT]):
        cfg.set(_INI_RECENT_SECTION, f'file{i}', str(p))
    _ini_save(cfg)


def add_recent_file(path: Path) -> List[Path]:
    """Prepend path to the recent list (dedup, cap at _MAX_RECENT)."""
    existing = load_recent_files()
    resolved = path.resolve()
    updated = [resolved] + [p for p in existing if p.resolve() != resolved]
    updated = updated[:_MAX_RECENT]
    save_recent_files(updated)
    return updated


# ── comp-attr collector (shared render logic) ──────────────────────────────────

# Tags rendered by dedicated pin/signal drawing — must not appear as comp attrs.
_SKIP_TAGS = frozenset({
    'WIRELABEL', 'PINTYPE', 'SIGNAL', 'TRANSX', 'TRANSY',
    'PARTS', 'PINSWAP', 'SPICE#', 'PORT',
})  # LABEL excluded — pin names, controlled by show_pin_names
# LABEL1 (and any LABELn) is not skipped: a visible one is a plain text a
# symbol carries, drawn where Ulticap draws it; it names no net (FILEFORMAT_ULTICAP_UTSCH.md §9.7).


def _comp_bbox_world(comp: Component, sym) -> tuple:
    """Axis-aligned world-space bounding box (x0, y0, x1, y1) for a
    component's symbol body, accounting for its rotation.

    Ulticap rotation is always a multiple of 90 degrees (plus optional
    mirror, see kiuc_model.rot_transform's docstring for all 8 states), so
    the box stays axis-aligned regardless of rotation -- transform all 4
    corners of the local (0,0)-(width,height) rectangle and take the
    min/max, rather than assuming any particular corner is the origin.
    """
    if sym is None:
        pad = 250   # generic box around the origin point when the symbol
                    # definition is missing/unresolved
        return comp.x-pad, comp.y-pad, comp.x+pad, comp.y+pad
    w, h = sym.width, sym.height
    xs, ys = [], []
    for dx, dy in ((0,0), (w,0), (w,h), (0,h)):
        tx, ty = _rot_transform(dx, dy, comp.rotation)
        xs.append(comp.x + tx); ys.append(comp.y + ty)
    return min(xs), min(ys), max(xs), max(ys)


def _comp_attr_entries(comp: Component, sym,
                       show_pin_names: bool = True,
                       show_pin_numbers: bool = True,
                       is_power: bool = False,
                       show_refdes: bool = True,
                       show_value: bool = True,
                       show_hidden: frozenset = frozenset()):
    """Yield (tag, val, dx, dy, dx_poe, dy_poe, size, colour, vis) for visible attrs.

    show_hidden names the groups (_hidden_group) whose hidden entries are
    yielded too; a caller tells them apart by the shown bit (vis & 128).

    *C overrides *S; multi-value tags (TXTn) all yielded.
    Excludes pin-related tags (LABEL, #, PINTYPE, …) rendered by pin drawing.

    For LABEL and # tags (one entry per pin), *S provides the full list and *C
    provides per-position overrides keyed on (dx, dy).  A *C block may contain
    only a subset of the pins (e.g. just the one pin that differs from the
    symbol template), so the wholesale *C-replaces-*S rule must NOT apply here.
    For all other tags the existing *C-overrides-*S rule is correct.

    WIRELABEL is normally skipped entirely (see _SKIP_TAGS) -- for power
    symbols it's shown separately, below, via the dedicated is_power branch.
    For hierarchical subsheet-block placements (comp.file_ref set) and
    module-port marker symbols (is_port_sym(sym)), WIRELABEL is each pin's
    own net-name label -- Ulticap's equivalent of a hierarchical/port label
    -- and must be shown the same way LABEL/# are, one entry per pin with
    per-position *C overrides. This is deliberately scoped to just those two
    cases rather than applied wherever WIRELABEL happens to appear, since
    other symbol types that do carry a WIRELABEL (real power/ground symbols)
    already render correctly via the is_power path and must not be touched.
    """
    _is_hier_wirelabel = bool(getattr(comp, 'file_ref', None)) or is_port_sym(sym)

    seen_tags, seen_set = [], set()
    if sym:
        for sa in sym.sym_attrs:
            if sa.tag not in seen_set:
                seen_tags.append(sa.tag); seen_set.add(sa.tag)
    # A .UTSCH subsheet box's name (Component.file_text) counts as its *C FILE=.
    c_attrs = list(comp.comp_attrs)
    file_text = getattr(comp, 'file_text', None)
    if file_text is not None and not any(ca.tag == 'FILE' for ca in c_attrs):
        c_attrs.append(file_text)
    for ca in c_attrs:
        if ca.tag not in seen_set:
            seen_tags.append(ca.tag); seen_set.add(ca.tag)

    c_by_tag: dict = {}
    for ca in c_attrs:
        c_by_tag.setdefault(ca.tag, []).append(ca)

    # Per-position override maps for pin-per-entry tags: (dx_poe,dy_poe) → comp_attr
    # Keyed on connection-point (POE) coordinates, not body position, so that
    # *C gate-specific pin number overrides are found regardless of component rotation.
    _PIN_TAGS = ('LABEL', '#') + (('WIRELABEL',) if _is_hier_wirelabel else ())
    c_pin_override: dict = {}   # tag → {(dx_poe,dy_poe): comp_attr}
    for pt in _PIN_TAGS:
        if pt in c_by_tag:
            c_pin_override[pt] = {(ca.dx_poe, ca.dy_poe): ca for ca in c_by_tag[pt]}

    for tag in seen_tags:
        # Power symbols: WIRELABEL and LABEL carry the net name — always show.
        if is_power and tag in ('WIRELABEL', 'LABEL'): pass
        # Hierarchical subsheet/port-marker WIRELABEL: handled via _PIN_TAGS below.
        elif tag == 'WIRELABEL' and _is_hier_wirelabel: pass
        elif tag in _SKIP_TAGS: continue
        if tag in ('LABEL', 'WIRELABEL') and not show_pin_names and not is_power: continue
        if tag.startswith('#') and not show_pin_numbers: continue
        if tag == 'REFDES' and not show_refdes: continue
        if tag == 'VALUE' and not show_value: continue

        # Pin-per-entry tags: always iterate sym_attrs; override per position.
        # A port's or subsheet box's WIRELABEL is shown even where the *S has
        # no WIRELABEL template at that pin: a .UTSCH port, and a V5.72
        # export of one, carry the visible net name on the placement only.
        # Not extended to LABEL/#: older files hold stray *C LABEL entries
        # off the pins (or beside a template already shown), never drawn.
        if tag in _PIN_TAGS and not is_power:
            if not sym: continue
            overrides = c_pin_override.get(tag, {})
            templ = [sa for sa in sym.sym_attrs if sa.tag == tag]
            templ_poes = {(sa.dx_poe, sa.dy_poe) for sa in templ}
            extra = ([ca for poe, ca in overrides.items() if poe not in templ_poes]
                     if tag == 'WIRELABEL' else [])
            for sa in templ + extra:
                e = overrides.get((sa.dx_poe, sa.dy_poe), sa)
                val = e.value if hasattr(e, 'value') else e.default_value
                if not (e.visibility & 128) and _hidden_group(tag, is_power) not in show_hidden:
                    continue
                if not val: continue
                # Multi-gate symbols store comma-separated pin numbers (e.g. "1,4,10,13")
                # in a single *S # attr covering all gates.  Show only the first number
                # since the viewer renders the symbol template (gate 1 equivalent).
                if tag.startswith('#') and ',' in val:
                    val = val.split(',')[0]
                yield tag, val, e.dx, e.dy, e.dx_poe, e.dy_poe, e.size, e.colour, e.visibility
            continue

        # All other tags: *C overrides *S wholesale, without the template
        # copies V5.72's ASCII import adds to a placement (the converter's
        # own rule, so both show the same text: a moved code once, not twice)
        if tag in c_by_tag:
            entries = c_by_tag[tag];  use_c = True
            if sym and _is_field_tag(tag):
                entries = _drop_import_copies(
                    entries, [sa for sa in sym.sym_attrs if sa.tag == tag])
        elif sym:
            entries = [sa for sa in sym.sym_attrs if sa.tag == tag];  use_c = False
        else:
            continue
        for e in entries:
            val = e.value if use_c else e.default_value
            if not (e.visibility & 128) and _hidden_group(tag, is_power) not in show_hidden:
                continue
            if not val:                  continue
            yield tag, val, e.dx, e.dy, e.dx_poe, e.dy_poe, e.size, e.colour, e.visibility


# ══════════════════════════════════════════════════════════════════════════════
# SVG RENDERER  (pure Python, no GUI, shared by both backends)
# ══════════════════════════════════════════════════════════════════════════════

def _render_svg(sheet: Sheet, vis: dict) -> str:
    """Render sheet to SVG with toggleable Inkscape-compatible layers.

    Each schematic layer is wrapped in <g inkscape:groupmode="layer">.
    A JS+HTML panel inside a <foreignObject> allows toggling layers in browsers.
    vis: dict mapping layer key → bool (initial visibility when exported).
    """
    # SVG box positioning uses Ulticap's own exact measured convention
    # (_uc_box_metrics / _UC_* constants) for its MARGINS only. The box's
    # CONTENT width is measured from the real font (via Qt's own font
    # metrics, at export time) rather than the model's fixed per-character
    # advance — see the whole-string rendering notes in
    # ULTICAP_TEXT_MODEL_IMPLEMENTATION.md (and the model's §2/§5).
    # This means positioning is NOT fully independent of font availability
    # any more: if the machine that later opens this SVG doesn't have
    # ISOCTEUR installed and substitutes a different font, the anchor math
    # (measured here against ISOCTEUR) can drift from the substituted
    # font's real rendered width, the same way the old fixed-model width
    # could drift from ISOCTEUR's own natural width. This is accepted as
    # strictly better than the previous model-only math, which drifted
    # with _text_scale even when ISOCTEUR *is* available.
    # A small residual offset visible on some right-anchored pins (seen on
    # certain pins of a real multi-pin symbol during testing) was confirmed
    # present in real Ulticap itself at maximum zoom — not a bug in this
    # model — so it's intentionally left uncorrected rather than "fixed"
    # against a font-metric quirk.

    # Lazy import: kiut_viewer's module scope stays PySide6-free so the
    # rest of the file can be used headlessly; PySide6 is only touched
    # here, at call time, purely to measure text width for anchor math —
    # not to draw anything (drawing stays pure-Python SVG markup).
    from PySide6.QtGui import QFont as _SvgQFont, QFontMetricsF as _SvgFontMetricsF
    _svg_font_cache: dict = {}

    def _svg_fm(fsz: float):
        """Cached QFontMetricsF for SVG font-size `fsz` — shared by
        _svg_measure (width) and the manual overline positioning (cap
        height / descent), so a given size is only ever measured once."""
        key = round(fsz, 2)
        fm = _svg_font_cache.get(key)
        if fm is None:
            f = _SvgQFont(_TEXT_FONT_FAMILY)
            f.setPixelSize(max(1, round(fsz)))
            fm = _SvgFontMetricsF(f)
            _svg_font_cache[key] = fm
        return fm

    def _svg_measure(chunk: str, fsz: float) -> float:
        """Real rendered width of `chunk` at SVG font-size `fsz`, via Qt's
        own font metrics — used only for anchor/box-width math, never for
        positioning individual characters (those still flow naturally)."""
        return _svg_fm(fsz).horizontalAdvance(chunk)

    # Use print palette (white background, dark colours) for SVG output —
    # same flattened category set as PDF/PNG export (see _light_theme_colours
    # in _run_qt); per-object custom colours are intentionally not
    # preserved here, for the same printability reasons PDF/PNG already
    # chose this over full per-object fidelity.
    _COL_WIRE         = _PRINT_WIRE
    _COL_BUS          = _PRINT_BUS
    _COL_JUNCTION     = _PRINT_JUNCTION
    _COL_STUB_RGB     = _PRINT_STUB
    _COL_SYM_BODY     = _PRINT_SYM_BODY
    _COL_ATTR_FALLBACK_RGB = _PRINT_ATTR
    _COL_TEXT_DEFAULT      = _PRINT_TEXT
    _COL_PIN_NAME     = _PRINT_PIN_NAME
    _COL_PIN_NUMBER   = _PRINT_PIN_NUMBER
    _COL_BORDER       = _PRINT_BORDER
    _COL_ORIGIN       = _PRINT_ORIGIN
    _BG_HEX           = _PRINT_BG

    _tb = _tight_bounds_with_gap(sheet, _TIGHT_BOUNDS_GAP_UNITS)
    if _tb:
        x0, y0, x1, y1 = _tb
    else:
        x0, y0, x1, y1 = sheet.xmin, sheet.ymin, sheet.xmax, sheet.ymax
    W = x1 - x0
    H = y1 - y0
    ox = -x0        # world x → SVG x:  sx = x + ox
    oy =  y1        # world y → SVG y:  sy = oy - y  (y-flip)

    def sx(x): return x + ox
    def sy(y): return oy - y
    def ss(u): return abs(u)

    # Per-layer element buckets
    layer_lines: dict = {k: [] for k, *_ in _LAYERS}
    _cur: list = ['symbols']   # mutable current-layer pointer

    def L(): return layer_lines[_cur[0]]
    def _begin(k): _cur[0] = k
    def _end():    _cur[0] = 'symbols'

    def _esc(t):
        return t.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

    def _line(x1,y1,x2,y2,col,w=1,dash=''):
        da = f' stroke-dasharray="{dash}"' if dash else ''
        L().append(f'<line x1="{sx(x1):.1f}" y1="{sy(y1):.1f}" x2="{sx(x2):.1f}" y2="{sy(y2):.1f}" stroke="{col}" stroke-width="{w}"{da}/>')

    def _poly(pts,col,w=1,dash='',fill='none'):
        if len(pts)<2: return
        da = f' stroke-dasharray="{dash}"' if dash else ''
        L().append(f'<polyline points="{" ".join(f"{sx(x):.1f},{sy(y):.1f}" for x,y in pts)}" stroke="{col}" stroke-width="{w}" fill="{fill}"{da}/>')

    def _circle(cx,cy,r,col,fill='none',w=1,dash=''):
        da = f' stroke-dasharray="{dash}"' if dash else ''
        L().append(f'<circle cx="{sx(cx):.1f}" cy="{sy(cy):.1f}" r="{ss(r):.1f}" stroke="{col}" stroke-width="{w}" fill="{fill}"{da}/>')

    def _arc(cx,cy,r,sd,sw,col,w=1,dash=''):
        if abs(sw)<0.01: return
        r=ss(r);
        if r<0.5: return
        x1=sx(cx)+r*math.cos(math.radians(sd));  y1=sy(cy)-r*math.sin(math.radians(sd))
        x2=sx(cx)+r*math.cos(math.radians(sd+sw)); y2=sy(cy)-r*math.sin(math.radians(sd+sw))
        lf=1 if abs(sw)>180 else 0; sf=0 if sw>0 else 1
        da = f' stroke-dasharray="{dash}"' if dash else ''
        L().append(f'<path d="M {x1:.1f},{y1:.1f} A {r:.1f},{r:.1f} 0 {lf},{sf} {x2:.1f},{y2:.1f}" stroke="{col}" stroke-width="{w}" fill="none"{da}/>')

    def _text(x,y,text,size_u,col,halign,valign,angle_deg):
        fsz=max(_MIN_SVG_FONT_SIZE,size_u*_TEXT_SCALE_DEFAULT)
        # Rotation pivots on the TRUE anchor (x, y) — the per-character shift
        # below must not affect the pivot, only where characters are placed.
        # The same transform is applied to any manual overline <line>s below
        # so they rotate together with the text they belong to.
        rot=f' transform="rotate({-angle_deg},{sx(x):.1f},{sy(y):.1f})"' if angle_deg else ''

        segs = [(chunk, ov) for chunk, ov in _overline_segments(text) if chunk]
        if not segs:
            return

        # Margins come from Ulticap's own measured world-unit convention
        # (same _uc_box_metrics used by the Qt path). Content width is NOT
        # taken from the model any more — it's measured from the real font
        # (see _svg_measure above) so the anchor math matches what actually
        # gets drawn, instead of drifting from it at non-default scales.
        left_u, _content_u, right_u, _box_w_u = _uc_box_metrics(text, size_u)
        top_u, bottom_u = _uc_vertical_metrics(size_u)
        fm = _svg_fm(fsz)
        run_widths = [_svg_measure(chunk, fsz) for chunk, _ov in segs]
        actual_content_u = sum(run_widths)
        box_w_u = left_u + actual_content_u + right_u

        if   halign == 'left':   start_x = x
        elif halign == 'right':  start_x = x - box_w_u
        else:                    start_x = x - box_w_u / 2

        x0 = start_x + left_u

        # Baseline computed from the vertical box model, exactly mirroring
        # the horizontal treatment: a box is positioned per valign using
        # Ulticap's own measured top/bottom margins, and content is always
        # baseline-justified within it. capHeight (unlike the margins) is
        # measured from the real rendering font — no Ulticap-measured,
        # font-independent cap-height number exists — the same reasoning
        # as the horizontal content-width hybrid fix. Always emitting
        # dominant-baseline="auto" at this exact computed y avoids relying
        # on 'middle'/'hanging' baseline interpretation, which varies
        # slightly between renderers.
        cap_u = fm.capHeight()
        box_h_u = top_u + cap_u + bottom_u
        if   valign == 'bottom': box_bottom_y = sy(y)
        elif valign == 'top':    box_bottom_y = sy(y) + box_h_u
        else:                    box_bottom_y = sy(y) + box_h_u / 2
        baseline_y = box_bottom_y - bottom_u
        gap_u = _uc_overline_gap(size_u)
        line_y = baseline_y - cap_u - gap_u

        parts = []
        lines = []
        first = True
        run_x = x0
        for (chunk, ov), w in zip(segs, run_widths):
            body = _esc(chunk)
            if first:
                parts.append(f'<tspan x="{sx(x0):.1f}">{body}</tspan>')
                first = False
            else:
                parts.append(f'<tspan>{body}</tspan>')
            if ov:
                lines.append(f'<line x1="{sx(run_x):.1f}" y1="{line_y:.1f}" '
                              f'x2="{sx(run_x + w):.1f}" y2="{line_y:.1f}" '
                              f'stroke="{col}" stroke-width="1"{rot}/>')
            run_x += w
        body_str = ''.join(parts)
        L().append(f'<text y="{baseline_y:.1f}" font-family="{_TEXT_FONT_FAMILY}, monospace" font-size="{fsz:.1f}" fill="{col}" dominant-baseline="auto"{rot}>{body_str}</text>')
        for ln in lines:
            L().append(ln)

    def _diamond(x,y,col,s=5):
        L().append(f'<polygon points="{sx(x):.0f},{sy(y)-s:.0f} {sx(x)+s:.0f},{sy(y):.0f} {sx(x):.0f},{sy(y)+s:.0f} {sx(x)-s:.0f},{sy(y):.0f}" fill="{col}"/>')

    def _poe_sq(x,y,s=4):
        L().append(f'<rect x="{sx(x)-s:.0f}" y="{sy(y)-s:.0f}" width="{2*s}" height="{2*s}" stroke="{_COL_POE}" fill="none" stroke-width="1"/>')

    # ── sheet border ──────────────────────────────────────────────────────────
    _begin('sheet_border')
    _line(x0, y0, x1, y0, _COL_BORDER)
    _line(x1, y0, x1, y1, _COL_BORDER)
    _line(x1, y1, x0, y1, _COL_BORDER)
    _line(x0, y1, x0, y0, _COL_BORDER)
    L().append(f'<line x1="{sx(0)-10}" y1="{sy(0)}" x2="{sx(0)+10}" y2="{sy(0)}" stroke="{_COL_ORIGIN}" stroke-width="1"/>')
    L().append(f'<line x1="{sx(0)}" y1="{sy(0)-10}" x2="{sx(0)}" y2="{sy(0)+10}" stroke="{_COL_ORIGIN}" stroke-width="1"/>')
    _end()

    # ── user lines ────────────────────────────────────────────────────────────
    _begin('user_lines')
    for ul in sheet.user_lines:
        _ul_dash = _DASH_BY_USERLINE_TYPE.get(ul.linetype)
        dash = _svg_dash(_ul_dash) if _ul_dash else ''
        _ul_w = _pen_units(ul.pen_width, ul.thickness != 6)
        _line(ul.x1,ul.y1,ul.x2,ul.y2,_COL_TEXT_DEFAULT,_ul_w,dash)
    # sheet arcs and circles (.UTSCH only), on the same layer
    for ua in sheet.user_arcs:
        cx, cy, r, sd, sw = _user_arc_geometry(ua)
        _ua_dash_t = _DASH_BY_STYLE_NAME.get(UC_ARC_STYLE_BY_CODE.get(ua.arc_linetype, 'solid'))
        _ua_dash = _svg_dash(_ua_dash_t) if _ua_dash_t else ''
        _ua_w = _pen_units(ua.pen_width, False)
        if ua.is_full_circle: _circle(cx,cy,r,_COL_TEXT_DEFAULT,'none',_ua_w,_ua_dash)
        else:                 _arc(cx,cy,r,sd,sw,_COL_TEXT_DEFAULT,_ua_w,_ua_dash)
    _end()

    # ── wires ─────────────────────────────────────────────────────────────────
    _begin('wires')
    for wire in sheet.wires:
        if not wire.is_bus: _line(wire.x1,wire.y1,wire.x2,wire.y2,_COL_WIRE,_thin_thick_width_units(False))
    _end()

    # ── buses ─────────────────────────────────────────────────────────────────
    _begin('buses')
    for wire in sheet.wires:
        if wire.is_bus: _line(wire.x1,wire.y1,wire.x2,wire.y2,_COL_BUS,_thin_thick_width_units(True))
    _end()

    # ── wire start anchors ────────────────────────────────────────────────────
    _begin('anc_wire')
    for wire in sheet.wires:
        _diamond(wire.x1,wire.y1,_COL_ANC_WIRE,4)
    _end()

    # ── junctions ─────────────────────────────────────────────────────────────
    _begin('junctions')
    for j in sheet.junctions:
        _r = _JUNC_R_BUS_UNITS if j.is_bus_entry else _JUNC_R_NORMAL_UNITS
        _circle(j.x,j.y,_r,_COL_JUNCTION,_COL_JUNCTION)
    _end()

    # ── symbols ───────────────────────────────────────────────────────────────
    # A part left out of the shown schematic variant is drawn faded with
    # italic texts, as on screen: what it adds to each layer is wrapped in a
    # group that says so, once the part is done (_close_faded).
    faded_from = None

    def _close_faded():
        for key, start in faded_from.items():
            elems = layer_lines[key]
            if len(elems) > start:
                elems[start:] = ([f'<g opacity="{_NOT_FITTED_OPACITY}" font-style="italic">']
                                 + elems[start:] + ['</g>'])

    for comp in sheet.components:
        if faded_from is not None:
            _close_faded()
            faded_from = None
        if not comp.fitted:
            faded_from = {k: len(v) for k, v in layer_lines.items()}
        sym = sheet.symbols.get(comp.symbol_name)
        rot = comp.rotation

        _begin('anc_comp'); _diamond(comp.x,comp.y,_COL_ANC_COMP); _end()

        _begin('symbols')
        if sym is None:
            _line(comp.x-30,comp.y-30,comp.x+30,comp.y+30,_COL_MISSING)
            _line(comp.x-30,comp.y+30,comp.x+30,comp.y-30,_COL_MISSING)
            continue

        for pl in sym.polylines:
            col=_COL_SYM_BODY; pts=[]
            for dx,dy in pl.points:
                odx,ody=_rot_transform(dx,dy,rot); pts.append((comp.x+odx,comp.y+ody))
            _pl_dash_t = _DASH_BY_STYLE_NAME.get(UC_POLYLINE_STYLE_BY_CODE.get(pl.linetype, 'solid'))
            _pl_dash = _svg_dash(_pl_dash_t) if _pl_dash_t else ''
            # a filled .UTSCH shape is filled in the body colour, as the SVG
            # draws every symbol body in its one theme colour
            _poly(pts,col,_pen_units(pl.pen_width, pl.width != 6),_pl_dash,
                  col if pl.filled and len(pts) >= 3 else 'none')

        for ci in sym.circles:
            col=_COL_SYM_BODY
            odx,ody=_rot_transform(ci.cx,ci.cy,rot)
            scx=comp.x+odx; scy=comp.y+ody
            _ci_dash_t = _DASH_BY_STYLE_NAME.get(UC_ARC_STYLE_BY_CODE.get(ci.arc_linetype, 'solid'))
            _ci_dash = _svg_dash(_ci_dash_t) if _ci_dash_t else ''
            _ci_w=_pen_units(ci.pen_width, ci.thick != 6)
            _ci_fill = col if ci.filled else 'none'
            outline = _ellipse_or_pie_points(ci)   # elongated ellipse or pie (.UTSCH)
            if outline is not None:
                _poly([(comp.x+tx, comp.y+ty) for tx, ty in
                       (_rot_transform(x, y, rot) for x, y in outline)],
                      col, _ci_w, _ci_dash, _ci_fill)
            elif ci.is_full_circle: _circle(scx,scy,ci.r,col,_ci_fill,_ci_w,_ci_dash)
            else:
                sd=ci.rotate/64.0; sw=ci.angle/64.0
                sd,sw=_arc_transform(sd,sw,rot)
                _arc(scx,scy,ci.r,sd,sw,col,_ci_w,_ci_dash)
        _end()

        for pin in sym.pins:
            conn_x,conn_y=_pin_conn_point(pin,sym)
            bdx,bdy=_rot_transform(pin.x,pin.y,rot)
            cdx,cdy=_rot_transform(conn_x,conn_y,rot)
            bx=comp.x+bdx; by=comp.y+bdy; cx=comp.x+cdx; cy=comp.y+cdy
            _begin('symbols')
            if pin.pin_format == 1:
                _R = _NEGATION_BUBBLE_R
                _sl = math.hypot(cx-bx,cy-by)
                _ux,_uy = ((cx-bx)/_sl,(cy-by)/_sl) if _sl>0 else (1.0,0.0)
                _bcx=bx+_R*_ux; _bcy=by+_R*_uy
                _line(_bcx+_R*_ux,_bcy+_R*_uy,cx,cy,_COL_STUB_RGB,_thin_thick_width_units(False))
                _circle(_bcx,_bcy,_R,_COL_STUB_RGB,_BG_HEX)
            else:
                _line(bx,by,cx,cy,_COL_STUB_RGB,_thin_thick_width_units(pin.pin_format==9))
            _end()
            _begin('anc_pin'); _diamond(cx,cy,_COL_ANC_PIN,4); _end()

        for tag,val,dx,dy,dpx,dpy,size,colour,vis_byte in _comp_attr_entries(comp,sym,True,True,is_pwr_symbol(sym)):
            odx,ody=_rot_transform(dx,dy,rot)
            wx=comp.x+odx; wy=comp.y+ody
            u=ulticap_translate(rot,vis_byte)
            ha,va,ang=_u_to_halign_valign_angle(u)
            ang=_text_display_angle(ang,rot)
            if tag in ('LABEL', 'WIRELABEL'): col=_COL_PIN_NAME
            elif tag.startswith('#'):         col=_COL_PIN_NUMBER
            else:                             col=_COL_ATTR_FALLBACK_RGB
            # Route pin names/numbers/REFDES/VALUE to their own layer buckets
            if tag in ('LABEL', 'WIRELABEL'): _begin('pin_names')
            elif tag.startswith('#'):         _begin('pin_numbers')
            elif tag=='REFDES':               _begin('refdes')
            elif tag=='VALUE':                _begin('value')
            else:                             _begin('symbols')
            _text(wx,wy,val,size or 35,col,ha,va,ang)
            _begin('poe_comp'); _poe_sq(comp.x+_rot_transform(dpx,dpy,rot)[0],comp.y+_rot_transform(dpx,dpy,rot)[1])
            if tag in ('LABEL', 'WIRELABEL'): _begin('anc_pin_name');   _diamond(wx,wy,_COL_ANC_PNAME,3)
            elif tag.startswith('#'):         _begin('anc_pin_number'); _diamond(wx,wy,_COL_ANC_PNUM,3)
            else:                             _begin('anc_attr');       _diamond(wx,wy,_COL_ANC_ATTR,3)
            _end()

    if faded_from is not None:
        _close_faded()

    # ── labels (*X) ───────────────────────────────────────────────────────────
    _begin('labels')
    for lbl in sheet.labels:
        col=_COL_TEXT_DEFAULT
        h,v,ang=_u_to_halign_valign_angle(lbl.align)
        ang=x_text_angle(lbl.rotation)
        _text(lbl.x,lbl.y,lbl.text,lbl.size or 35,col,h,v,ang)
    _end()
    _begin('anc_ann')
    for lbl in sheet.labels: _diamond(lbl.x,lbl.y,_COL_ANC_ANN)
    _end()

    # ── annotations (*A) ──────────────────────────────────────────────────────
    for ann in sheet.annotations:
        _begin('annotations' if ann.visibility & 128 else 'annotations_hidden')
        col=_COL_TEXT_DEFAULT
        u=ulticap_translate(0,ann.visibility)
        h,v,ang=_u_to_halign_valign_angle(u)
        _text(ann.x,ann.y,ann.text,ann.size or 35,col,h,v,ang)
    _end()
    _begin('poe_ann')
    for ann in sheet.annotations: _poe_sq(ann.x_poe,ann.y_poe)
    _end()
    _begin('anc_ann')
    for ann in sheet.annotations: _diamond(ann.x,ann.y,_COL_ANC_ANN)
    _end()

    # ── assemble SVG with layer groups + JS toggle panel ──────────────────────
    # width/height carry physical mm units so the document has a real size
    # (a bare number here is CSS pixels per the SVG spec, which made this a
    # multi-metre "page" for a typical sheet); viewBox stays in world units,
    # so it -- and every stroke-width already expressed in that same
    # coordinate space -- gets scaled uniformly to the physical size,
    # instead of stroke-width looking disproportionately thin relative to
    # PNG/PDF's actual-DPI-based rendering.
    #
    # The toggle panel is reserved canvas space of its own, to the right of
    # the sheet border, rather than sitting inside the content area: at its
    # on-screen size (see _panel_scale below) it's physically too large to
    # tuck into the existing tight-bounds gap without risking overlap with
    # whatever happens to be near a given sheet's edge.
    _panel_scale = 1 / (MM_PER_UNIT * 96 / 25.4)   # world-units-per-CSS-px, fixed
    panel_h = len(_LAYERS) * 20 + 16
    _panel_w_units  = 178     * _panel_scale
    _panel_h_units  = panel_h * _panel_scale
    _panel_inset_units = 4 * _panel_scale          # the foreignObject's own x="4" y="4"
    _panel_gap_units = _TIGHT_BOUNDS_GAP_UNITS     # clearance from the border, both sides
    _panel_x0_units  = W + _panel_gap_units        # world-x where the panel's own
                                                    # local origin (0,0) lands
    canvas_W = _panel_x0_units + _panel_w_units + _panel_inset_units + _panel_gap_units
    canvas_H = max(H, _panel_h_units + 2 * _panel_inset_units)

    out = ['<?xml version="1.0" encoding="UTF-8"?>']
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'width="{canvas_W*MM_PER_UNIT:.2f}mm" height="{canvas_H*MM_PER_UNIT:.2f}mm" '
        f'viewBox="0 0 {canvas_W:.0f} {canvas_H:.0f}">'
    )

    # JS toggle function
    out.append(
        '<script type="text/javascript"><![CDATA[\n'
        'function toggleLayer(id,cb){\n'
        '  var g=document.getElementById(id);\n'
        '  if(g) g.style.display=cb.checked?"":"none";\n'
        '}\n'
        'window.onload=function(){\n'
        '  var fo=document.getElementById("fo_panel");\n'
        '  if(fo) fo.style.display="";\n'
        '};\n'
        ']]></script>'
    )

    # Layer groups (Inkscape-compatible + initial visibility from vis)
    for key, label, default, _col in _LAYERS:
        elems = layer_lines.get(key, [])
        if not elems: continue
        show = vis.get(key, default)
        # A "Hidden ..." layer opens shown only with its parent shown too,
        # and is set in italic as on screen.
        parent = _HIDDEN_PARENT_OF.get(key)
        if parent is not None:
            show = show and vis.get(parent, True)
        disp = '' if show else ' style="display:none"'
        ital = ' font-style="italic"' if parent is not None else ''
        out.append(f'<g id="{key}" inkscape:groupmode="layer" inkscape:label="{_esc(label)}"{disp}{ital}>')
        out.extend(elems)
        out.append('</g>')

    # HTML toggle panel via foreignObject (works in browsers; Inkscape ignores it).
    # x/y/width/height on the foreignObject itself are authored as if 1 unit
    # == 1 CSS px (the panel's original design assumption, back when the
    # <svg> root had no physical size and world units WERE CSS pixels).
    # Now that the root carries real mm dimensions, 1 world unit is much
    # smaller than 1 CSS px, so the panel is wrapped in a fixed counter-scale
    # to restore its original on-screen size (independent of sheet size,
    # since it's purely the ratio between a world unit's physical size and
    # a CSS pixel's physical size at 96 CSS px/inch) and translated into the
    # reserved margin to the right of the border, aligned with its top edge.
    out.append(f'<g transform="translate({_panel_x0_units:.0f},0) '
               f'scale({_panel_scale:.6f})">')
    out.append(f'<foreignObject x="4" y="4" width="178" height="{panel_h}" '
               'id="fo_panel" style="display:none">')
    out.append('<div xmlns="http://www.w3.org/1999/xhtml" '
               'style="background:rgba(245,245,245,0.92);padding:5px 6px;'
               'font:11px sans-serif;color:#222;border-radius:5px;'
               'box-shadow:0 1px 4px rgba(0,0,0,.25);">')
    prev_group = None
    for key, label, default, col in _LAYERS:
        grp = 'anc' if key.startswith('anc') or key.startswith('poe') else 'sch'
        if grp != prev_group and prev_group is not None:
            out.append('<hr style="border:0;border-top:1px solid #ccc;margin:3px 0"/>')
        prev_group = grp
        show = vis.get(key, default)
        chk  = ' checked="checked"' if show else ''
        # glyph rows: colour the glyph character; others: small inline swatch
        is_glyph = key.startswith('anc') or key.startswith('poe')
        if is_glyph:
            glyph, rest = label[0], label[1:]
            lbl_html = (f'<span style="color:{col}">{_esc(glyph)}</span>{_esc(rest)}')
        else:
            lbl_html = (f'<span style="display:inline-block;width:10px;height:10px;'
                        f'background:{col};border:1px solid #888;'
                        f'vertical-align:middle;margin-right:4px"></span>{_esc(label)}')
        out.append(
            f'<label style="display:block;cursor:pointer;white-space:nowrap">'
            f'<input type="checkbox" id="cb_{key}"{chk} '
            f'onchange="toggleLayer(\'{key}\',this)"/> {lbl_html}'
            f'</label>'
        )
    out.append('</div></foreignObject>')
    out.append('</g>')
    out.append('</svg>')
    return '\n'.join(out)


def _sheet_info_text(sheet: Sheet) -> str:
    """Multi-line schematic summary shown in a popup (View menu ->
    Schematic info…) — previously always occupied space in the sidebar."""
    return (f'Symbols: {len(sheet.symbols)}\n'
            f'Components: {len(sheet.components)}\n'
            f'Wires: {len(sheet.wires)}\n'
            f'Junctions: {len(sheet.junctions)}\n'
            f'Grid: {sheet.grid} u')


def _dominant_layer_colours(sheet: Sheet) -> dict:
    """Return {layer_key: colour_hex} for layers whose colour can be
    meaningfully derived from the schematic's object data.

    Wire/bus/junction colours are NOT included: Wire.net_id is a net
    *number*, not a palette index — net_id % 16 produces an arbitrary
    palette entry unrelated to the drawn colour.  The viewer draws
    wires hardcoded from _COL_WIRE and buses from _COL_BUS.

    Pin names and pin numbers come from the sym_attr / comp_attr colour
    fields via _comp_attr_entries; when colour==0 the renderer falls
    back to #e0e0a0 (yellowish-cream)."""
    from collections import Counter

    def dominant(colour_list, fallback: str):
        """Most-common non-zero palette index → hex, or fallback."""
        counts = Counter(c for c in colour_list if c != 0)
        return _hex(counts.most_common(1)[0][0]) if counts else fallback

    result = {}

    c = dominant([ul.colour for ul in sheet.user_lines],  '')
    if c: result['user_lines']  = c

    c = dominant([lb.colour for lb in sheet.labels],      '')
    if c: result['labels']      = c

    c = dominant([a.colour  for a  in sheet.annotations], '')
    if c: result['annotations'] = c

    # Pin names (LABEL tag), pin numbers (# tag), REFDES, and VALUE: collect
    # all colour fields from symbol and component attributes across all
    # components.
    pname_cols, pnum_cols, refdes_cols, value_cols = [], [], [], []
    for comp in sheet.components:
        sym = sheet.symbols.get(comp.symbol_name)
        try:
            for tag, _v, _dx, _dy, _dpx, _dpy, _sz, colour, _vis in \
                    _comp_attr_entries(comp, sym,
                                       show_pin_names=True,
                                       show_pin_numbers=True,
                                       is_power=is_pwr_symbol(sym)):
                if tag in ('LABEL', 'WIRELABEL'):
                    pname_cols.append(colour)
                elif tag.startswith('#'):
                    pnum_cols.append(colour)
                elif tag == 'REFDES':
                    refdes_cols.append(colour)
                elif tag == 'VALUE':
                    value_cols.append(colour)
        except Exception:
            pass   # defensive: never let colour extraction break loading

    result['pin_names']   = dominant(pname_cols,  _COL_ATTR_FALLBACK)
    result['pin_numbers'] = dominant(pnum_cols,   _COL_ATTR_FALLBACK)
    result['refdes']      = dominant(refdes_cols, _COL_ATTR_FALLBACK)
    result['value']       = dominant(value_cols,  _COL_ATTR_FALLBACK)

    return result


def _light_theme_layer_colours() -> dict:
    """Fixed left-pane swatch colours for the white/light canvas theme.

    Unlike _dominant_layer_colours (dark theme), this does NOT scan the
    file's per-object colour fields: force_colours flattens every object
    in a category to the same fixed colour regardless of what the file
    specifies (see _light_theme_colours in _run_qt / _PRINT_* constants),
    so scanning would be wasted work whose result could never actually be
    painted. sheet_border/grid/wires/buses/junctions/symbols are included
    here too (dark theme leaves these as _LAYERS' static default and never
    revisits them, since those defaults already match dark-theme reality)."""
    return {
        'sheet_border':  _PRINT_BORDER,
        'grid':          _PRINT_GRID,
        'wires':         _PRINT_WIRE,
        'buses':         _PRINT_BUS,
        'user_lines':    _PRINT_TEXT,
        'symbols':       _PRINT_SYM_BODY,
        'pin_names':     _PRINT_PIN_NAME,
        'pin_numbers':   _PRINT_PIN_NUMBER,
        'refdes':        _PRINT_ATTR,
        'value':         _PRINT_ATTR,
        'junctions':     _PRINT_JUNCTION,
        'labels':        _PRINT_TEXT,
        'annotations':   _PRINT_TEXT,
    }


def _layer_swatch_colours(sheet: Sheet, theme: str) -> dict:
    """Theme dispatcher for the left-pane layer swatches. Light theme uses
    the fixed flattened set, since that's what actually gets painted
    regardless of file content. Dark theme merges the static _LAYERS
    screen defaults for categories that never vary with file content
    (sheet_border/grid/wires/buses/user_lines/symbols/junctions) with the
    existing per-object dominant-colour scan for the rest -- recomputing
    the static half explicitly (rather than assuming old swatches were
    never touched) is what makes toggling light->dark always land back on
    the correct screen colours, even after the light theme's swatches
    overwrote them. 'Text (*X)'/green and 'Annotations'/grey are
    deliberately NOT overridden here: they match Ulticap's own on-screen
    defaults for these, not a bug."""
    if theme == 'light':
        base = _light_theme_layer_colours()
    else:
        base = {key: default_colour for key, _label, _vis, default_colour in _LAYERS
                if key in ('sheet_border', 'grid', 'wires', 'buses',
                           'user_lines', 'symbols', 'junctions')}
        base.update(_dominant_layer_colours(sheet))
    # A "Hidden ..." row shows its parent row's colour.
    for parent, hid in _HIDDEN_LAYER_OF.items():
        if parent in base:
            base[hid] = base[parent]
    return base


_HELP_TEXT = """\
MOUSE
  Wheel              Zoom, centred on the cursor
  Right/middle drag  Pan
  Right click        On a symbol, without moving the mouse: the part menu
                     (the part is selected). Variants (a design with
                     schematic variants only; greyed out otherwise) opens
                     a small table: Symbol variant | Document variant(s)
                     | Value, the shown variant marked, None for the
                     variants the part is left out of -- with a button
                     to copy it. Overlapping parts: a submenu per part.
  Faded symbol       .UTSCH with schematic variants: the viewer shows the
                     variant Ulticap shows. A part left out of it is drawn
                     faded, its texts in italic. Clicking a part, the
                     status bar shows its shown variant as
                     <reference> - <symbol variant>/<document variant>:
                     <value> (R1 - SYMVAR1/DEFAULT: 10k), or None for a
                     part left out (drawn faded; KiCad: do not populate).
  Double-click       On a component with a sub-sheet (FILE_REF, e.g. a
                     hierarchical block), enters it — same as clicking it
                     in the SHEETS panel. The first click still runs as an
                     ordinary inspect click first (see Left click above),
                     that's just how double-clicks work.

KEYBOARD
  Space              Set the relative-coordinate origin at the cursor
  C                  Toggle full-window crosshair
  F                  Fit the schematic content in view
  +/-                Cycle the toolbar snap step (whole 0.0508mm units)
  B                  Back to the previous sheet (undoes a double-click
                     into a sub-sheet, a SHEETS panel jump, or File Open —
                     whatever was showing right before the current sheet)
  Esc                Clear the current highlight (a click-inspected symbol,
                     *X/*A label, or Jump-to-origin), if any. Does nothing
                     otherwise — it no longer quits the program.
  Q                  Quit

TOOLBAR
  Open               Load a .SCH, .BLK or .UTSCH file
  Export PNG/SVG/PDF Save the current sheet, or the full sheet hierarchy,
                     as PNG/SVG/PDF
  Fit Sheet          Same as F
  Crosshair          Same as C — toggles full-window crosshair mode
  White/Black        Switches the canvas background between Ulticap's own
  background          on-screen colours (black background; each wire,
                     symbol, net label etc. in its own screen-palette
                     colour, exactly as the file specifies) and a white
                     background theme. The white theme does NOT try to
                     reproduce each object's individual colour on white —
                     it uses one fixed colour per category (all wires one
                     colour, all buses one colour, all symbol bodies one
                     colour, etc.), the same reduced palette the SVG and
                     PDF exports use (PNG keeps Ulticap's screen colours on
                     a dark background), chosen for legibility on a light
                     background rather than for matching Ulticap's screen
                     colours one-for-one. Persisted across sessions
                     (kiut_viewer.ini).
  Snap               Cursor-snap step in mm (0/"free" = unsnapped). Read-only
                     aid for the crosshair and status-bar readout — never
                     written to the file. Use the spinbox arrows or +/- (see
                     KEYBOARD above) to cycle through whole-unit steps.
  Line width —       The viewer's thin and thick line widths, in Ulticap
  Thin / Thick       units (even values only, 2-400 — real mil = the value
                     shown ÷ 2). Thin draws thin symbol lines, wires and
                     stubs; thick draws thick symbol lines and buses; both
                     set the junction size. All scale together with zoom,
                     and the SVG/PNG/PDF export uses them too. Defaults are
                     Ulticap DOS's two fixed symbol line widths (6 / 30).
                     Saved for the viewer only (kiut_viewer.ini) — they never
                     change the converter's output.

MENUS
  View → Schematic info…   Symbol/component/wire/junction/grid counts for
                           the current sheet, in a popup (kept out of the
                           side pane to save space).

COMPONENTS / SIGNALS panel
  Two tabs below Sheets — click any entry to zoom straight to it.
  Components lists every real part as "REFDES   VALUE" (mounting
  hardware, hierarchical blocks, and power symbols excluded) — always
  populated. Signals lists SIGNAL= pin groupings per component, with
  pin numbers shown alongside each net name; may be empty on sheets
  with none, which is expected — it still stays visible either way.

SHEETS panel
  Shows the full sub-sheet hierarchy of the currently open design — every
  descendant sheet, not just the next level — resolved from each
  component's FILE_REF attribute: to a file next to it for a .SCH design,
  to a sheet stored in the same file for a .UTSCH (listed under its stored
  name). The sheet currently on screen is
  highlighted; click any other entry (a child, a grandchild, an ancestor,
  a sibling — anywhere in the tree) to jump straight to it, no need to
  step back one level at a time. Hidden for designs with no sub-sheets.
  Double-clicking a sub-sheet component directly on the canvas is a
  quicker way in for the common case (see MOUSE); B (see KEYBOARD) is the
  quick way back out, since it doesn't need a trip to this panel either.

LAYERS panel
  Each checkbox shows/hides one category of drawn elements (wires, buses,
  symbols, annotations, anchor/POE markers, grid, …) — purely a display
  filter, it does not change the underlying file. Reference and Value
  (indented under Symbols) show/hide just those two attributes' text.

  Hidden texts: like Ulticap, the viewer leaves out texts the file stores
  as hidden. They can be shown, in italic:
  - for one symbol: select it (left click); a "Show hidden" column appears
    with a square, in the row's colour, on the Pin names, Pin numbers,
    Reference and Value rows. Click a square to show that group's hidden
    texts (filled = shown). Only the selected
    symbol's hidden texts are drawn -- they can sit far from their symbol,
    inside a neighbouring one, and the dimmed rest of the sheet makes clear
    what belongs to the selected one. The toggles start off, and keep their
    setting when you select another symbol;
  - for annotations (*A): the "Hidden annotations" row under Annotations
    (only while Annotations is on). A hidden annotation can be clicked
    only while it is shown.
  Other hidden attributes (device, package, user and SPICE attributes, ...)
  stay hidden. Free texts (*X) cannot be hidden in Ulticap. Exports never
  include a selection; the SVG export puts hidden annotations on their own
  layer, in italic.

MARKERS (bottom of the Layers panel)
  Debug/inspection aids — anchor diamonds and POE squares — off by
  default in every export. "Hide all"/"Show all" mutes or restores
  exactly the markers that were on, without forcing on any you'd
  deliberately left off; while muted, the individual rows above it are
  locked (avoids odd interplay with the eventual restore) until
  un-muted again.

  Left-click a symbol (see MOUSE) to inspect it: a "Selected" column
  appears next to six of the marker rows — Symbol origin, Pin conn., Pin
  name/number anchor, Comp attr anchor/POE — each independently
  showing/hiding just that marker type for the inspected symbol only,
  regardless of the main checkboxes or Hide all. Useful for telling
  overlapping markers apart. This filter is deliberately NOT reset when
  you inspect a different symbol, so it stays put for comparing
  identical components side by side.

TEXT FONT
  The viewer draws every text in ISOCTEUR, an ISO 3098 engineering-
  lettering font that closely matches Ulticap's own DOS lettering. It is
  not part of KIUT: install it for the closest rendering (see Fonts in
  KIUT's README). Without it
  another font stands in, so texts look different and can sit slightly
  off where they are aligned right or centred; a message at start says
  so until "Do not show this message again" is ticked (kiut_viewer.ini). The SVG
  export names ISOCTEUR too, so it shows best where ISOCTEUR is installed.
  Three IEC qualifying symbols of the DOS font (codes 219, 221 and 222:
  ⊓ ≷ ▫) are not in ISOCTEUR and are drawn by whatever font has
  them. The KiCad output does not depend on the viewer's font -- see the
  schematic's Font in the KIUT window.
"""


# ══════════════════════════════════════════════════════════════════════════════
# QT BACKEND
# ══════════════════════════════════════════════════════════════════════════════

def _run_qt(sheet: Optional[Sheet], initial_dir: Path, initial_path: Optional[Path] = None,
            initial_key: Optional[str] = None, handoff: Optional[str] = None) -> None:
    """The viewer's window; handoff: its channel id, listening for the KIUT
    window's Open in Viewer (the program, not the tests)."""
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QSplitter,
        QVBoxLayout, QHBoxLayout, QScrollArea, QCheckBox, QPushButton,
        QLabel, QFileDialog, QMessageBox, QFrame, QSizePolicy,
        QDialog, QPlainTextEdit, QToolBar, QDoubleSpinBox, QSpinBox, QMenu,
        QTreeWidget, QTreeWidgetItem, QProgressDialog, QTabWidget, QLineEdit,
        QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem, QAbstractItemView,
    )
    from PySide6.QtGui import (
        QPainter, QPen, QColor, QFont, QFontMetricsF,
        QPolygonF, QTransform, QImage, QIcon, QPixmap, QAction,
        QPdfWriter, QPicture, QCursor, QFontDatabase,
    )
    from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, QSize, QMarginsF, QPoint, QRect, QEvent, QObject
    from PySide6.QtCore import Signal as _Signal

    class _HandoffBridge(QObject):
        """A file offered by the KIUT window's Open in Viewer (kiut/core/handoff.py),
        handed from the listening thread to the window's own thread; the reply
        handed back."""
        offered = _Signal(object)

        def __init__(self, receive):
            super().__init__()
            self._receive = receive
            self.offered.connect(self._on_offered)     # queued: runs in the window's thread

        def _on_offered(self, box):
            path, reply, done = box
            try:
                reply.append(self._receive(path))
            except Exception as e:
                reply.append(f'error: {e}')
            done.set()

        def handle(self, paths, sender=None):
            import threading
            path = paths[0] if paths else None
            reply, done = [], threading.Event()
            self.offered.emit((path, reply, done))
            done.wait(10)
            return reply[0] if reply else 'error: no answer'

    BG   = QColor(_BG_HEX)
    WIRE = QColor(_COL_WIRE)
    BUS  = QColor(_COL_BUS)
    JUNC = QColor(_COL_JUNCTION)

    def _qcol_u(colour: int) -> QColor:
        return QColor(_hex(colour))

    def _qicon(name: str) -> QIcon:
        pix = QPixmap()
        pix.loadFromData(_icon_bytes(name))
        return QIcon(pix)

    def _light_theme_colours(bw: bool = False) -> dict:
        """Single source of truth for the white/light-background colour set.

        Deliberately flattened (force_colours=True): every object within a
        category renders in that category's fixed colour rather than its
        own per-object palette index. This isn't a fidelity shortcut --
        it's the same reduced-palette choice already made for PDF/PNG
        printability (colour or greyscale print reproduces a handful of
        high-contrast colours far more reliably than a full 16-colour
        per-net rainbow), applied consistently wherever content needs to
        sit on a white/light background: PDF export and the live viewer's
        white-canvas theme both read this same dict, so a future tweak
        here updates both at once.

        bw=True collapses schematic content to black (true black & white
        print); bw=False uses the dark-but-legible per-category colours.
        Border/grid/origin (viewer chrome, not schematic content) stay at
        their light-but-visible values in both cases.
        """
        if bw:
            content = {
                'bg':           QColor('#ffffff'),
                'wire':         QColor('#000000'),
                'bus':          QColor('#000000'),
                'junction':     QColor('#000000'),
                'text_default': QColor('#000000'),
                'stub':         QColor('#000000'),
                'sym_body':     QColor('#000000'),
                'attr':         QColor('#000000'),
                'pin_name':     QColor('#000000'),
                'pin_number':   QColor('#000000'),
            }
        else:
            content = {
                'bg':           QColor(_PRINT_BG),
                'wire':         QColor(_PRINT_WIRE),
                'bus':          QColor(_PRINT_BUS),
                'junction':     QColor(_PRINT_JUNCTION),
                'text_default': QColor(_PRINT_TEXT),
                'stub':         QColor(_PRINT_STUB),
                'sym_body':     QColor(_PRINT_SYM_BODY),
                'attr':         QColor(_PRINT_ATTR),
                'pin_name':     QColor(_PRINT_PIN_NAME),
                'pin_number':   QColor(_PRINT_PIN_NUMBER),
            }
        content.update({
            'border':         QColor(_PRINT_BORDER),
            'grid':           QColor(_PRINT_GRID),
            'origin':         QColor(_PRINT_ORIGIN),
            'force_colours':  True,
            'junction_scale': 1.5,
        })
        return content

    # ── canvas widget ─────────────────────────────────────────────────────────

    class _TooltipFilter(QObject):
        """Swallows every tooltip event while tooltips are switched off (View
        > Show tooltips). Installed on the application, so it covers every
        widget -- also the toolbar buttons, whose tooltips Qt makes from
        their actions' text by itself."""
        def __init__(self, parent=None):
            super().__init__(parent)
            self.enabled = True

        def eventFilter(self, obj, e):
            if e.type() == QEvent.ToolTip and not self.enabled:
                return True
            return False

    class _PanContextMenuBlocker(QObject):
        """Swallows the context-menu request that a right-button release
        makes, when that release ends a pan. Qt sends the request to the
        widget under the cursor, not to the viewer that has the mouse: a pan
        released over the toolbar opened the main window's toolbar menu (a
        'Main' checkbox), which took the focus. Watches the application's
        events only from the press to the request, or to the next mouse
        input without it."""

        def __init__(self, canvas):
            super().__init__(canvas)
            self._canvas = canvas
            self._active = False

        def arm(self):
            if not self._active:
                QApplication.instance().installEventFilter(self)
                self._active = True

        def disarm(self):
            if self._active:
                QApplication.instance().removeEventFilter(self)
                self._active = False

        def eventFilter(self, obj, e):
            if e.type() == QEvent.ContextMenu:
                self.disarm()
                return True
            if (e.type() in (QEvent.MouseMove, QEvent.MouseButtonPress)
                    and self._canvas._drag_pos is None):
                self.disarm()
            return False

    class SchCanvas(QWidget):

        def __init__(self, parent=None):
            super().__init__(parent)
            self.sheet      = None
            self._vp        = _VP()
            self._auto_fit  = False      # the view is the sheet's fit: kept so on a resize,
                                         # until the user zooms, pans or jumps elsewhere
            self._grid      = _GRID_FALLBACK_DEFAULT
            self._vis       = {k: d for k,_,d,_c in _LAYERS}
            self._theme     = load_theme()   # 'dark' (default, screen palette) or 'light'
            self._snap_mm   = _SNAP_MM_DEFAULT  # 0 == free/unsnapped; see _update_status
            self._snap_wx   = 0          # rounded to grid — drives crosshair + readout
            self._snap_wy   = 0
            self._drag_pos  = None
            self._drag_at   = None       # the cursor within the window, while dragging
            self._drag_painted = False   # a pan step was shown during this drag
            self._inspected_comp = None   # click-to-inspect (see mousePressEvent);
                                           # persists across zoom/pan, cleared on
                                           # sheet load or an outside-bbox click
            self._inspected_text = None   # (comp, tag, value, dx, dy) of a clicked
                                           # detached symbol text (see _comp_text_hits);
                                           # only meaningful while comp is inspected
            self._inspected_annot = None  # click-to-inspect for *X labels / *A
                                           # annotations -- (kind, obj) tuple,
                                           # kind is 'label' or 'annotation';
                                           # mutually exclusive with
                                           # _inspected_comp (see _inspect_annot
                                           # / _set_inspected). See
                                           # _on_inspect_click for hit-testing
                                           # and _draw_inspected_annot_overlay
                                           # for the dim+highlight rendering.
            # Which component-marker TYPES show while something is
            # inspected (see the secondary "Selected" column in the
            # Markers panel) -- deliberately NOT reset per component, so
            # a filter chosen while inspecting one part stays in place
            # when inspecting the next (useful for comparing identical
            # components). Missing key = shown, via .get(key, True).
            self._inspect_marker_vis = {}
            # Per-group "hidden" toggles for the inspected symbol's texts
            # (_SYMBOL_TEXT_GROUPS): off whenever the viewer starts, and, like
            # _inspect_marker_vis, kept when another symbol is inspected.
            self._inspect_hidden = {g: False for g in _SYMBOL_TEXT_GROUPS}
            self.inspect_cb = None   # notifies the main window when
                                      # inspection changes (show/hide the
                                      # secondary column), same pattern as info_cb
            self._last_screen   = None   # (sx, sy) of last known cursor pos
            self._sheet_image = None       # rendered sheet, see update()/paintEvent
            self._sheet_image_key = None
            self._sheet_image_stale = True
            self._pan_picture = None        # the sheet recorded for panning, and the
            self._pan_picture_at = None     # recording replayed around the view:
            self._pan_image = None          # see _record_pan_picture and
            self._pan_image_at = None       # _paint_from_pan_picture
            # The pan image is kept for a moment after a drag, for a next press
            # it can serve (rapid clicks), then freed: it can be ~100 MB.
            self._pan_free_timer = QTimer(self)
            self._pan_free_timer.setSingleShot(True)
            self._pan_free_timer.setInterval(2000)
            self._pan_free_timer.timeout.connect(self._free_pan_image)
            # Wheel zoom: previews from the recording while the wheel turns;
            # the exact rendering once it has stopped this long.
            self._zoom_preview = False
            self._zoom_timer = QTimer(self)
            self._zoom_timer.setSingleShot(True)
            self._zoom_timer.setInterval(_ZOOM_SETTLE_MS)
            self._zoom_timer.timeout.connect(self._end_zoom_preview)
            self._ref_wx    = 0.0        # KiCad-style relative-origin (Space)
            self._ref_wy    = 0.0
            self._ref_set   = False
            self._full_crosshair = False
            self._timer     = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self.update)
            # Separate periodic timer for live cursor tracking (mouseMoveEvent).
            # Unlike self._timer (single-shot, restarted per event — fine for
            # settle-after-a-burst cases like wheel-zoom/resize/pan), this one
            # free-runs at a fixed ~60fps cadence and only repaints when the
            # cursor has actually moved since the last tick. That bounds the
            # extra work from cursor tracking to at most once per tick no
            # matter how fast move events arrive, without ever "freezing"
            # the crosshair during continuous motion the way restarting a
            # single-shot timer on every event would.
            self._move_dirty = False
            self._move_timer = QTimer(self)
            self._move_timer.setInterval(16)
            self._move_timer.timeout.connect(self._flush_move)
            self._move_timer.start()
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.StrongFocus)
            self.setCursor(Qt.BlankCursor)
            self.setMinimumSize(400, 300)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            # RMB is pan, not a menu -- nor a menu of the main window (see
            # _PanContextMenuBlocker for a release beyond the viewer).
            self.setContextMenuPolicy(Qt.PreventContextMenu)
            self._menu_blocker = _PanContextMenuBlocker(self)
            self.status_cb     = None   # callable(str)
            self.info_cb       = None   # callable(str) — sidebar schematic summary
            self.colours_cb    = None   # callable(dict) — dynamic layer colour update
            self.grid_cb       = None   # callable(int) — sync sidebar 'Grid (N u)' label
            self.crosshair_cb  = None   # callable(bool) — sync sidebar checkbox
            self.snap_step_cb  = None   # callable(+1/-1) — cycle the toolbar grid spinbox
            self.enter_subsheet_cb = None   # callable(Component) — double-clicked a
                                             # component with a FILE_REF; MainWindow
                                             # resolves+loads the target (see
                                             # mouseDoubleClickEvent)
            self.back_cb       = None   # callable() — 'B' pressed; MainWindow pops
                                         # its sheet-navigation history, if any
            self._hint_line1 = ('RMB/MMB drag: pan   │   Wheel: zoom   │   '
                                'Dbl-click: enter subsheet')
            self._hint_line2 = ('F: fit sheet   │   Space: set relative origin   │   '
                                'C: crosshair   │   +/-: snap step   │   '
                                'B: back   │   Esc: clear highlight')

        @property
        def _show_markers(self): return self._vp.scale >= _MARKER_MIN_PX

        def load(self, sheet: Sheet):
            self.sheet = sheet
            self._inspected_comp = None
            self._inspected_annot = None
            if self.inspect_cb: self.inspect_cb()
            self._grid = sheet.grid if sheet.grid >= _GRID_FALLBACK_MIN else _GRID_FALLBACK_DEFAULT
            self._vp   = _VP()
            self._fit_sheet()
            if self.info_cb:
                self.info_cb(_sheet_info_text(sheet))
            if self.colours_cb:
                self.colours_cb(_layer_swatch_colours(sheet, self._theme))
            if self.grid_cb:
                self.grid_cb(self._grid)

        def _schedule(self):
            self._timer.start(16)

        def _show_hint(self):
            """Push the keyboard/mouse shortcut hint to the status bar.
            Only called when no file is loaded; guarded for status_cb not
            yet set (canvas resize can fire before MainWindow wires it up).
            Uses both lines for actual content (not just line 1 padded with
            an empty line 2) -- a single long line was getting cut off in a
            small window; splitting it across both of the coordinate
            readout's own lines keeps the status bar's height constant
            either way, same as before, while fitting more before truncating."""
            if self.status_cb and not self.sheet:
                self.status_cb(self._hint_line1 + '\n' + self._hint_line2)

        def _fit_sheet(self):
            if not self.sheet: return
            # 'Fit sheet' re-frames to the whole sheet, which almost always
            # moves away from whatever was click-inspected -- leaving the
            # old highlight/dim in place would just look like the rest of
            # the sheet got dimmed for no reason. Clear it, same as an
            # empty-space click would.
            self._inspected_comp  = None
            self._inspected_annot = None
            if self.inspect_cb: self.inspect_cb()
            self._fit_view()

        def _fit_view(self):
            """The view fitted to the whole sheet at the canvas's present
            size; kept fitted on a resize (_auto_fit) -- a sheet opened before
            the window has its final size still opens fitted."""
            if not self.sheet: return
            sh = self.sheet
            b = _tight_bounds_with_gap(sh, _TIGHT_BOUNDS_GAP_UNITS)
            if b:
                x0, y0, x1, y1 = b
            else:
                x0, y0, x1, y1 = sh.xmin, sh.ymin, sh.xmax, sh.ymax
            self._vp.fit(x0, y0, x1, y1,
                         self.width() or 900, self.height() or 650)
            self._auto_fit = True
            self.update()

        def _zoom_to(self, x, y, extent):
            """Fit the viewport tightly around a point, framed by extent
            (already includes a reasonable margin — see callers). Used by
            the Signals panel's click-to-locate so it frames the component
            regardless of whatever zoom level was previously selected,
            rather than just panning to it at the current scale.

            Never zooms out further than the whole-sheet fit: 'zoom to a
            component' should always zoom IN, never out, even if a caller's
            extent turns out larger than the sheet's own content (e.g. a
            large symbol on a small/tight sheet). Conversely, never zooms
            in past _scale_bounds' own max (the same ceiling the mouse
            wheel respects) either -- a small extent with nothing to
            naturally bound it (e.g. a *X/*A label, which has no 'symbol
            size' the way a component does) would otherwise land on an
            enormous, disorienting zoom level."""
            if not self.sheet: return
            self._auto_fit = False
            cw = self.width() or 900
            ch = self.height() or 650
            self._vp.fit(x-extent, y-extent, x+extent, y+extent, cw, ch)
            _lo, hi = self._scale_bounds()
            if self._vp.scale > hi:
                self._vp.scale = hi
                self._vp.ox = cw/2 - x*self._vp.scale
                self._vp.oy = ch/2 + y*self._vp.scale
            tb = _tight_bounds(self.sheet)
            if tb and tb[2] != tb[0] and tb[3] != tb[1]:
                margin = _ZOOM_OUT_MARGIN_PX   # matches _VP.fit()'s own default margin
                sheet_scale = min((cw-2*margin)/(tb[2]-tb[0]),
                                  (ch-2*margin)/(tb[3]-tb[1]))
                if self._vp.scale < sheet_scale:
                    self._vp.scale = sheet_scale
                    self._vp.ox = cw/2 - x*self._vp.scale
                    self._vp.oy = ch/2 + y*self._vp.scale
            self.update()

        def _jump_to_origin(self):
            """Toolbar 'Jump to sheet origin' -- goes through the same
            click-to-inspect highlight system as *X/*A labels and symbols
            (dims the rest of the sheet, keeps the origin crosshair at
            full brightness) instead of silently navigating with nothing
            to show what changed. Also means it correctly clears/replaces
            whatever was previously inspected, rather than leaving a stale
            highlight dimmed over unrelated content once the view has
            moved. See _inspect_annot / _draw_inspected_annot_overlay's
            'origin' kind."""
            self._inspect_annot('origin', None)

        def _inspect_annot(self, kind, obj, extent=None):
            """Click-to-inspect for a *X label, *A annotation, or the
            sheet origin (kind='origin', obj=None -- see _jump_to_origin).
            Mutually exclusive with the symbol click-to-inspect
            (_inspected_comp); selecting one clears the other.

            extent, if given, means deliberate navigation to a specific
            framing (Find passes its own, to match the Components/Signals
            panels' zoom) via _zoom_to/_vp.fit. Left as None (an ordinary
            canvas click), an off-screen target instead gets the gentler
            _pan_to_fit treatment -- pan without changing zoom where
            possible, zoom out only as much as truly necessary otherwise
            -- see _reframe_for_annot for the full reasoning."""
            self._inspected_comp  = None
            self._inspected_annot = (kind, obj)
            self._reframe_for_annot(kind, obj, extent)
            if self.inspect_cb: self.inspect_cb()
            self.update()

        def _reframe_for_annot(self, kind, obj, extent=None):
            """Auto-fit the view around an inspected label/annotation, but
            ONLY when its point(s) aren't already fully on-screen -- an
            ordinary click on something already visible shouldn't jump the
            view.

            kind='origin' is one exception: it's reached only via the
            explicit 'Jump to sheet origin' toolbar action, whose whole
            purpose is to navigate there, so it always zooms (matching
            Fit Sheet's always-re-frame behaviour) rather than only when
            off-screen.

            extent is the other: when given (Find passes its own, to match
            the Components/Signals panels' framing), an off-screen target
            reframes to that specific extent via _zoom_to/_vp.fit, same as
            before -- that's deliberate navigation, meant to land on a
            defined view regardless of the current one.

            But an ordinary click (extent=None) reaching an off-screen *X/
            *A goes through _pan_to_fit instead: pan to bring it into
            view, changing zoom only if it doesn't actually fit at the
            current scale, and even then only zooming OUT (never in) by
            the minimum needed. Jumping to some fixed 'loose' extent every
            time -- the previous behaviour -- was jarring when the current
            zoom was already tighter than that extent; this keeps the
            common case (a single off-screen point) a pure pan with no
            zoom change at all, while still handling a *A whose anchor and
            POE are far apart by zooming out just enough to fit both."""
            if not self.sheet: return
            cw = self.width()  or 900
            ch = self.height() or 650
            if kind == 'origin':
                ext = extent if extent is not None else _ZOOM_LOOSE_EXTENT_UNITS
                self._zoom_to(0, 0, ext)
                return
            pts = [(obj.x, obj.y)] if kind == 'label' else \
                  [(obj.x, obj.y), (obj.x_poe, obj.y_poe)]
            on_screen = all(0 <= self._vp.cx(x) <= cw and 0 <= self._vp.cy(y) <= ch
                            for x, y in pts)
            if on_screen:
                return
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            if extent is not None:
                if x0 == x1 and y0 == y1:
                    self._zoom_to(x0, y0, extent)
                else:
                    self._auto_fit = False
                    self._vp.fit(x0-extent, y0-extent, x1+extent, y1+extent, cw, ch)
            else:
                self._pan_to_fit(x0, y0, x1, y1, _ZOOM_SYMBOL_MARGIN_UNITS)

        def _pan_to_fit(self, x0, y0, x1, y1, margin):
            """Bring world-space bbox (x0,y0)-(x1,y1), padded by margin,
            fully into view with the least disruption possible: pans to
            centre it without touching zoom if it already fits at the
            CURRENT scale, and only zooms OUT -- never in, and only as far
            as necessary -- when the bbox is too big to fit otherwise
            (e.g. a *A whose anchor and POE are far apart). Used only for
            the passive 'a plain click landed on something off-screen'
            reframe (see _reframe_for_annot) -- deliberate navigation
            (Find, Jump to origin, Fit Sheet) uses _zoom_to/_vp.fit
            directly instead, since those are meant to reframe to a
            specific, defined view regardless of the current one."""
            cw = self.width()  or 900
            ch = self.height() or 650
            x0 -= margin; y0 -= margin; x1 += margin; y1 += margin
            bw, bh = x1 - x0, y1 - y0
            m = _ZOOM_OUT_MARGIN_PX
            needed = min((cw-2*m)/bw if bw > 0 else 1e9,
                        (ch-2*m)/bh if bh > 0 else 1e9)
            lo, _hi = self._scale_bounds()
            scale = max(lo, min(self._vp.scale, needed))
            self._auto_fit = False
            self._vp.scale = scale
            cx, cy = (x0+x1)/2, (y0+y1)/2
            self._vp.ox = cw/2 - cx*scale
            self._vp.oy = ch/2 + cy*scale
            self.update()

        def _search_matches(self, query):
            """Toolbar text search: find every *X label, *A annotation, or
            component refdes/attribute value (VALUE, DEVICE, PKG_TYPE,
            WIRELABEL, custom *C tags -- pin numbers live on Symbol pins,
            not Component, so they're naturally excluded) containing
            `query` as a case-insensitive substring. Returns a list of
            (display_text, kind, obj, x, y, extent) tuples, one per match,
            where kind is 'label'/'annotation'/'component' and obj is the
            actual Label/Annotation/Component -- so the caller can select a
            match through the same click-to-inspect highlight system a
            canvas click uses (_inspect_annot / _set_inspected), not just
            navigate there with no indication of what changed. Offered as
            a pulldown when there's more than one match -- a bbox fit
            across every match at once tends to just zoom out to the whole
            sheet when hits are scattered, which isn't useful.

            extent matches the Components/Signals panels' own tight
            symbol-based framing (not the old, looser _ZOOM_LOOSE_EXTENT_
            UNITS) -- Find is predominantly a component-lookup tool, so
            it's confusing for it to land at a different zoom level than
            clicking the same component in those panels would. *X/*A
            labels have no 'symbol size' to base an extent on, so they get
            a small fixed one instead -- _zoom_to's own max-scale clamp
            (see its docstring) keeps that from ever zooming in absurdly
            close on a short piece of text."""
            if not self.sheet: return []
            q = query.strip().lower()
            if not q: return []
            hits = []
            for lbl in self.sheet.labels:
                if q in (lbl.text or '').lower():
                    hits.append((f'*X   {lbl.text}', 'label', lbl, lbl.x, lbl.y,
                                _ZOOM_SYMBOL_MARGIN_UNITS))
            for ann in self.sheet.annotations:
                if q in (ann.text or '').lower():
                    hits.append((f'*A   {ann.text}', 'annotation', ann, ann.x, ann.y,
                                _ZOOM_SYMBOL_MARGIN_UNITS))
            for comp in self.sheet.components:
                texts = [comp.display_refdes()] + list(comp.attributes.values())
                if any(q in (t or '').lower() for t in texts):
                    value = (comp.value or comp.attributes.get('VALUE')
                              or comp.attributes.get('DEVICE') or '')
                    label = f'{comp.display_refdes()}   {value}' if value else comp.display_refdes()
                    # Same centring + extent formula as _populate_refdes /
                    # _populate_signals -- see their comments for why
                    # (symbol-local origin isn't the body's centre, and
                    # extent is a half-size-plus-margin, not a multiple).
                    sym = self.sheet.symbols.get(comp.symbol_name)
                    MARGIN = _ZOOM_SYMBOL_MARGIN_UNITS
                    extent = max(sym.width, sym.height, 100)/2 + MARGIN if sym else 400
                    if sym:
                        cdx, cdy = _rot_transform(sym.width/2, sym.height/2, comp.rotation)
                        cx, cy = comp.x + cdx, comp.y + cdy
                    else:
                        cx, cy = comp.x, comp.y
                    hits.append((label, 'component', comp, cx, cy, extent))
            # Ascending, natural order (C1, C2, ... C10 rather than the
            # lexicographic C1, C10, C2) -- same key _populate_refdes uses
            # for the Refdes panel, applied here to the full display label
            # so it sorts on refdes first for components (its own natural
            # prefix) while still giving labels/annotations a sensible
            # order among themselves.
            hits.sort(key=lambda h: _natural_sort_key(h[0]))
            return hits

        def enterEvent(self, e):
            # Grab keyboard focus the instant the cursor enters the canvas,
            # so Space/C/F/G work immediately without first clicking inside
            # it — otherwise focus can be left on a sidebar widget (the
            # folder browser, a checkbox…) and the relative-origin point
            # silently goes stale instead of tracking the live cursor.
            self.setFocus(Qt.MouseFocusReason)
            self._show_hint()
            super().enterEvent(e)

        def _scale_bounds(self):
            """Dynamic zoom limits for the current sheet + current window
            size:
              min: tight content bounds fill the viewport (same fit basis
                   used for the sheet border and PNG/PDF export, so 'zoomed
                   all the way out' matches what those already draw).
              max: 10 horizontal grid units span the viewport width (an
                   already-huge zoom level; grid spacing is per-sheet).
            Both are recomputed on every call rather than cached, since they
            depend on the live canvas size (window resize) as well as the
            loaded sheet.
            """
            if not self.sheet:
                return (1e-6, 1e6)
            cw = self.width() or 900
            ch = self.height() or 650
            tb = _tight_bounds(self.sheet)
            if tb:
                x0, y0, x1, y1 = tb
            else:
                x0, y0 = self.sheet.xmin, self.sheet.ymin
                x1, y1 = self.sheet.xmax, self.sheet.ymax
            margin = _ZOOM_OUT_MARGIN_PX
            if x1 == x0 or y1 == y0:
                min_scale = 1e-6
            else:
                min_scale = max(1e-6, min((cw-2*margin)/(x1-x0),
                                          (ch-2*margin)/(y1-y0)))
            grid = self.sheet.grid if self.sheet.grid >= _GRID_FALLBACK_MIN else _GRID_FALLBACK_DEFAULT
            max_scale = (cw / (_ZOOM_MAX_GRID_UNITS*grid)) if grid > 0 else 1e6
            return (min_scale, max(min_scale, max_scale))

        def wheelEvent(self, e):
            delta = e.angleDelta().y()
            f = _ZOOM_WHEEL_FACTOR if delta > 0 else 1/_ZOOM_WHEEL_FACTOR
            p = e.position()
            lo, hi = self._scale_bounds()
            target = max(lo, min(hi, self._vp.scale * f))
            if target != self._vp.scale:
                # Same cursor-anchor math as _VP.zoom(), but against the
                # clamped target scale so ox/oy stay consistent — calling
                # zoom() then overwriting .scale afterward would leave
                # ox/oy computed for the wrong (unclamped) scale and the
                # anchor point would drift under the cursor.
                wx = self._vp.wx(p.x());  wy = self._vp.wy(p.y())
                self._auto_fit = False
                self._vp.scale = target
                self._vp.ox = p.x() - wx * target
                self._vp.oy = p.y() + wy * target
            if self._drag_pos is not None:
                # Zoomed while panning: the drag goes on from here. Without
                # this the next move put the view back at the drag's start
                # position plus the move, computed for the old zoom -- a jump.
                self._drag_pos = QPointF(*self._drag_at)
                self._drag_ox, self._drag_oy = self._vp.ox, self._vp.oy
                self._last_screen = self._drag_at
            else:
                self._zoom_preview = True
                self._zoom_timer.start()
                self._last_screen = (p.x(), p.y())
            self._update_status()
            self._update_pan()

        def _end_zoom_preview(self):
            """The wheel has stopped: render exactly (a drag renders on release)."""
            self._zoom_preview = False
            if self._drag_pos is None:
                self._update_pan()

        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                self._on_inspect_click(e)
                return
            if e.button() in (Qt.RightButton, Qt.MiddleButton):
                self._drag_pos  = e.position()
                self._drag_ox   = self._vp.ox
                self._drag_oy   = self._vp.oy
                self._drag_at   = (e.position().x(), e.position().y())
                self._drag_painted = False
                self._menu_blocker.arm()
                self._prepare_pan()

        def mouseDoubleClickEvent(self, e):
            """Double-clicking a component that references a sub-sheet
            (FILE_REF, e.g. a hierarchical block symbol) enters it --
            MainWindow resolves the reference and loads it (see
            enter_subsheet_cb). The first click of the double-click has
            already run through mousePressEvent/_on_inspect_click as an
            ordinary single click (that's how Qt delivers double-clicks),
            so the component briefly highlights before navigating away --
            harmless, and consistent with how a single click there would
            have behaved anyway.

            A right or middle double-click is a press: rapid clicks arrive
            as one, and dropping it left the hold-and-move after them
            without a pan."""
            if e.button() in (Qt.RightButton, Qt.MiddleButton):
                self.mousePressEvent(e)
                return
            if e.button() != Qt.LeftButton or not self.sheet:
                return
            wx, wy = self._vp.wx(e.position().x()), self._vp.wy(e.position().y())
            matches = self._hit_test_components(wx, wy)
            refs = [c for c in matches if getattr(c, 'file_ref', None)]
            if len(refs) == 1 and self.enter_subsheet_cb:
                self.enter_subsheet_cb(refs[0])

        def _hit_test_components(self, wx, wy):
            """Every component whose bounding box contains world point
            (wx,wy), for click-to-inspect and double-click-to-enter-
            subsheet to share -- both need the same 'which symbol did the
            click land on' test."""
            matches = []
            for comp in self.sheet.components:
                sym = self.sheet.symbols.get(comp.symbol_name)
                x0, y0, x1, y1 = _comp_bbox_world(comp, sym)
                if x0 <= wx <= x1 and y0 <= wy <= y1:
                    matches.append(comp)
            return matches

        def _comp_text_hits(self, sx, sy):
            """(distance, comp, (tag, value, dx, dy)) for every symbol text
            drawn outside its symbol's outline whose rendered box contains
            screen point (sx, sy) -- the texts as _paint_component draws
            them (same layers and, for the inspected symbol, its forced and
            revealed hidden texts)."""
            hits = []
            vis = self._vis
            for comp in self.sheet.components:
                insp = comp is self._inspected_comp
                if not vis.get('symbols', True) and not insp:
                    continue
                sym = self.sheet.symbols.get(comp.symbol_name)
                if sym is None:
                    continue
                rot = comp.rotation
                for tag, val, dx, dy, _px, _py, size, _col, vis_byte in _comp_attr_entries(
                        comp, sym,
                        show_pin_names=vis['pin_names'] or insp,
                        show_pin_numbers=vis['pin_numbers'] or insp,
                        is_power=is_pwr_symbol(sym),
                        show_refdes=vis['refdes'] or insp,
                        show_value=vis['value'] or insp,
                        show_hidden=(frozenset(g for g in _SYMBOL_TEXT_GROUPS
                                               if self._inspect_hidden.get(g))
                                     if insp else frozenset())):
                    odx, ody = _rot_transform(dx, dy, rot)
                    wx, wy = comp.x + odx, comp.y + ody
                    if not _text_is_detached(comp, sym, wx, wy):
                        continue
                    h, v, ang = _u_to_halign_valign_angle(ulticap_translate(rot, vis_byte))
                    ang = _text_display_angle(ang, rot)
                    ax, ay = self._vp.cx(wx), self._vp.cy(wy)
                    if self._point_hits_text(ax, ay, val, size or 35, h, v, ang, sx, sy):
                        hits.append((math.hypot(ax - sx, ay - sy), comp, (tag, val, dx, dy)))
            return hits

        def _on_inspect_click(self, e):
            """Click-to-inspect: left-clicking inside a symbol's bounding
            box, or on/near a *X label or *A annotation, highlights it
            (dims the rest of the sheet, shows all its markers regardless
            of the marker-list toggles) until a click lands somewhere else
            -- either clearing back to normal (empty space) or switching
            to a different item.

            All candidates under the click are considered together --
            components AND labels/annotations, not just whichever kind
            happens to be checked first -- so a *X/*A that sits inside a
            symbol's bounding box is still reachable: any ambiguity (more
            than one candidate total, of either kind) is resolved with a
            small picker popup rather than one kind silently winning.
            """
            if not self.sheet:
                return
            wx, wy = self._vp.wx(e.position().x()), self._vp.wy(e.position().y())
            sx, sy = e.position().x(), e.position().y()

            # sort_key 0 for every component keeps them all ranked ahead of
            # labels/annotations in the picker menu (see below) without
            # needing a proximity metric for a bbox containment test;
            # labels/annotations use click distance so the closest one
            # appears first among themselves.
            candidates = []   # (sort_key, menu_label, kind, obj)
            for comp in self._hit_test_components(wx, wy):
                _menu_label = comp.display_refdes()
                if _menu_label == '?':
                    # No real refdes (e.g. a PORT_BUS_* bus-port component)
                    # -- '?' alone can't distinguish two adjacent such
                    # components in this menu, so fall back to whatever
                    # DOES identify it: its own net/bus name (LABEL or
                    # WIRELABEL -- the same tag-priority convention
                    # kiuc_writer.py's own bus-naming logic uses), the
                    # subsheet it represents if it's a hierarchical
                    # placement, or finally its symbol name.
                    _menu_label = (comp.attributes.get('LABEL') or
                                  comp.attributes.get('WIRELABEL') or
                                  comp.file_ref or comp.symbol_name or '?')
                candidates.append((0, _menu_label, 'component', comp))

            # A symbol's texts drawn outside its outline: clicking one
            # selects its symbol, like clicking the symbol itself, and the
            # status bar says what the text is (see _comp_text_hits).
            for d, comp, text in self._comp_text_hits(sx, sy):
                candidates.append((d, f'{comp.display_refdes()}   {text[1]}', 'comp_text',
                                   (comp, text)))

            # *X labels / *A annotations: proximity to the anchor (and,
            # for annotations, the POE too) within a small pixel radius --
            # catches the common case and still works even when the text
            # itself is empty/whitespace -- OR falling inside the actual
            # rendered text box, which is what makes a high-zoom click
            # land: at large zoom the glyphs can span far more screen
            # space than the small anchor radius, especially when the
            # anchor sits at one corner of the box (e.g. right/top-aligned
            # text) rather than under the visible characters themselves.
            for lbl in self.sheet.labels:
                ax, ay = self._vp.cx(lbl.x), self._vp.cy(lbl.y)
                d = math.hypot(ax-sx, ay-sy)
                h, v, ang = _u_to_halign_valign_angle(lbl.align)
                ang = x_text_angle(lbl.rotation)
                if d <= _LABEL_HIT_RADIUS_PX or \
                   self._point_hits_text(ax, ay, lbl.text, lbl.size or 35, h, v, ang, sx, sy):
                    candidates.append((d, f'*X   {lbl.text}', 'label', lbl))
            for ann in self.sheet.annotations:
                if not (ann.visibility & 128) and not (
                        self._vis.get('annotations') and self._vis.get('annotations_hidden')):
                    continue   # not drawn, so not clickable either
                ax, ay = self._vp.cx(ann.x),     self._vp.cy(ann.y)
                px_, py_ = self._vp.cx(ann.x_poe), self._vp.cy(ann.y_poe)
                d = min(math.hypot(ax-sx, ay-sy), math.hypot(px_-sx, py_-sy))
                u = ulticap_translate(0, ann.visibility)
                h, v, ang = _u_to_halign_valign_angle(u)
                if d <= _LABEL_HIT_RADIUS_PX or \
                   self._point_hits_text(ax, ay, ann.text, ann.size or 35, h, v, ang, sx, sy):
                    candidates.append((d, f'*A   {ann.text}', 'annotation', ann))

            if not candidates:
                self._inspected_comp  = None
                self._inspected_annot = None
                if self.inspect_cb: self.inspect_cb()
                self.update()
                return

            if len(candidates) == 1:
                _key, _label, kind, obj = candidates[0]
                self._select_inspect_candidate(kind, obj)
                return

            candidates.sort(key=lambda c: c[0])
            menu = QMenu(self)
            for _key, label, kind, obj in candidates:
                act = menu.addAction(label)
                act.triggered.connect(
                    lambda _checked, k=kind, o=obj: self._select_inspect_candidate(k, o))
            menu.exec(e.globalPosition().toPoint())

        def _select_inspect_candidate(self, kind, obj):
            """Dispatch one click-to-inspect selection (from
            _on_inspect_click, either directly for an unambiguous click or
            via its picker menu for an ambiguous one) to the right
            highlight method for its kind."""
            if kind == 'component':
                self._set_inspected(obj)
            elif kind == 'comp_text':
                comp, text = obj
                self._set_inspected(comp, text)
            else:
                self._inspect_annot(kind, obj)

        def _parts_at(self, sx, sy):
            """The parts under screen point (sx, sy): those whose outline
            holds it, and those with a text drawn outside the outline there;
            the inspected part first."""
            if not self.sheet:
                return []
            wx, wy = self._vp.wx(sx), self._vp.wy(sy)
            parts = list(self._hit_test_components(wx, wy))
            for _d, comp, _text in self._comp_text_hits(sx, sy):
                if comp not in parts:
                    parts.append(comp)
            parts.sort(key=lambda c: c is not self._inspected_comp)
            return parts

        def _open_part_menu(self, press, global_pos):
            """The right-click menu for the part under the cursor: its
            actions (_add_part_actions), or one submenu per part where
            several overlap. The part is selected, so the status bar and the
            highlight show which one the menu is about. Nothing under the
            cursor: no menu."""
            parts = self._parts_at(press.x(), press.y())
            if not parts:
                return
            if len(parts) == 1 and parts[0] is not self._inspected_comp:
                self._set_inspected(parts[0])
            menu = QMenu(self)
            if len(parts) == 1:
                self._add_part_actions(menu, parts[0])
            else:
                for comp in parts:
                    sub = menu.addMenu(f'{comp.display_refdes()}   {comp.symbol_name or ""}')
                    sub.aboutToShow.connect(lambda c=comp: self._set_inspected(c))
                    self._add_part_actions(sub, comp)
            self._part_menu = menu     # kept: a popup must outlive this call
            menu.popup(global_pos)

        def _add_part_actions(self, menu, comp):
            """The part menu's entries for comp -- add new ones here.
            Variants is offered in a design with schematic variants only;
            elsewhere it is shown greyed out."""
            act = menu.addAction('Variants')
            act.setEnabled(bool(self.sheet is not None and self.sheet.shown_variant))
            act.triggered.connect(lambda _checked=False, c=comp: self._show_part_variants(c))

        def _show_part_variants(self, comp):
            """A small window with comp's variants (_variant_table): a line on
            what matters, then Symbol variant | Document variant(s) | Value.
            One window: showing another part replaces its content."""
            self._set_inspected(comp)
            summary, rows = _variant_table(comp, self.sheet)
            dlg = getattr(self, '_variants_dlg', None)
            if dlg is None:
                dlg = QDialog(self.window(), Qt.WindowType.Window | Qt.WindowType.WindowTitleHint
                              | Qt.WindowType.WindowSystemMenuHint | Qt.WindowType.WindowCloseButtonHint)
                lay = QVBoxLayout(dlg)
                dlg._summary = QLabel()
                dlg._summary.setWordWrap(True)
                lay.addWidget(dlg._summary)
                dlg._table = QTableWidget(0, 3)
                dlg._table.setHorizontalHeaderLabels(['Symbol variant', 'Document variant(s)', 'Value'])
                dlg._table.verticalHeader().setVisible(False)
                dlg._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
                dlg._table.horizontalHeader().setStretchLastSection(True)
                lay.addWidget(dlg._table)
                row = QHBoxLayout()
                row.addStretch(1)
                copy_btn = QPushButton('Copy to clipboard')
                copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(dlg._copy_text))
                close_btn = QPushButton('Close')
                close_btn.clicked.connect(dlg.close)
                row.addWidget(copy_btn); row.addWidget(close_btn)
                lay.addLayout(row)
                self._variants_dlg = dlg
            dlg.setWindowTitle(f'Variants \u2014 {comp.display_refdes()}')
            dlg._summary.setText(summary)
            t = dlg._table
            t.setRowCount(len(rows))
            for i, cells in enumerate(rows):
                for j, cell in enumerate(cells):
                    t.setItem(i, j, QTableWidgetItem(cell))
            t.resizeColumnsToContents()
            dlg._copy_text = summary + '\n' + '\n'.join(
                '\t'.join(r) for r in [('Symbol variant', 'Document variant(s)', 'Value')] + rows) + '\n'
            width = max(420, t.horizontalHeader().length() + 40)
            height = t.horizontalHeader().height() + sum(t.rowHeight(i) for i in range(len(rows)))
            t.setMinimumHeight(height + 6)
            dlg.resize(width, height + 110)
            dlg.show(); dlg.raise_(); dlg.activateWindow()

        def _set_inspected(self, comp, text=None):
            """Inspect comp; text = (tag, value, dx, dy) when the click
            landed on one of its texts drawn outside its outline."""
            self._inspected_comp  = comp
            self._inspected_text  = (comp, *text) if text is not None else None
            self._inspected_annot = None
            if self.inspect_cb: self.inspect_cb()
            self.update()

        def mouseMoveEvent(self, e):
            if self._drag_pos is not None and e.buttons() & (Qt.RightButton|Qt.MiddleButton):
                # The pan follows the cursor only within the window: beyond
                # an edge the sheet stops (release and drag again to go on).
                # That bounds how far one drag can move the sheet, which is
                # what lets the pan image cover exactly that reach (see
                # _make_pan_image).
                # The cursor itself is put back inside, too: released beyond
                # the edge it would leave the viewer.
                x, y = e.position().x(), e.position().y()
                self._drag_at = (min(max(x, 0), self.width() - 1),
                                 min(max(y, 0), self.height() - 1))
                if self._drag_at != (x, y):
                    QCursor.setPos(self.mapToGlobal(QPoint(int(self._drag_at[0]), int(self._drag_at[1]))))
                self._auto_fit = False
                self._vp.ox = self._drag_ox + self._drag_at[0] - self._drag_pos.x()
                self._vp.oy = self._drag_oy + self._drag_at[1] - self._drag_pos.y()
                self._update_pan()
                self._last_screen = self._drag_at
            else:
                self._last_screen = (e.position().x(), e.position().y())
            self._move_dirty = True

        def _flush_move(self):
            """Periodic (~60fps) handler for _move_timer — see its setup in
            __init__ for why this is separate from the wheel/resize/pan
            debounce timer. Only does work if the cursor actually moved
            since the last tick."""
            if self._move_dirty:
                self._move_dirty = False
                self._update_status()
                self._update_cursor()

        def _update_status(self):
            """Recompute abs/relative position and push to the status bar.
            Called from any event that can change the world↔screen mapping
            or cursor position (move, wheel-zoom, pan, resize) so the scale
            indicator and coordinates stay live, not just on mouse move.

            'abs' reflects the current position honouring the active snap
            setting (toolbar spinbox) — snapped to a grid step when > 0,
            or the raw free cursor position when 0. 'rel' is abs's position
            relative to the reference point set with Space.

            Two-line display: line 1 is the u-value layout (whole units
            only — the Ulticap database has no sub-unit resolution, so
            these only change once the cursor crosses a unit boundary).
            Line 2 adds the mm equivalent, computed continuously (not from
            the floored u values) so it stays exact for any snap step,
            left-padded to align under each field's numbers (monospace
            font)."""
            if self._last_screen is None:
                return
            spx, spy = self._last_screen
            vp = self._vp
            wx = vp.wx(spx);  wy = vp.wy(spy)
            if self._snap_mm > 0:
                snap_u = self._snap_mm / MM_PER_UNIT
                sx = round(wx/snap_u)*snap_u;  sy = round(wy/snap_u)*snap_u
            else:
                sx, sy = wx, wy   # free/unsnapped cursor movement
            # Always update snap position — this drives the crosshair drawing.
            # The early return for 'no sheet' must NOT skip this or the
            # crosshair stays locked at (0,0) when the canvas is empty.
            self._snap_wx = sx;  self._snap_wy = sy
            if not self.sheet:
                self._show_hint()
                return
            fsx = _floor_u(sx);  fsy = _floor_u(sy)
            fref_x = _floor_u(self._ref_wx);  fref_y = _floor_u(self._ref_wy)
            # rel's u-column is the floored (whole-unit) difference — the
            # database-resolution distance. Its mm-column is computed
            # continuously from sx/ref_wx directly (the same pattern abs
            # uses) rather than from that floored difference, so it's exact
            # even mid-transition between two unit boundaries.
            rx = fsx - fref_x;  ry = fsy - fref_y
            rel_mm_x = (sx - self._ref_wx) * MM_PER_UNIT
            rel_mm_y = (sy - self._ref_wy) * MM_PER_UNIT
            if self.status_cb:
                def _dual(label, xv_str, yv_str, unit, xv_mm, yv_mm):
                    """Build matching (line1, line2) segments for one field.
                    line2's numbers are +10.4f (4 decimals): the snap step
                    is now always a whole number of U's (_SNAP_MM_STEP is
                    MM_PER_UNIT itself), so mm values are exact multiples of
                    0.0508mm — a value that needs 4 decimal digits to show
                    exactly for anything but a multiple of 5 U. Showing only
                    3 would silently round most of them, making an exact
                    value look approximate. xv_str/yv_str (line 1) are
                    widened to 10 characters to match, so the two lines'
                    fields stay column-aligned (u sits above the mm line's
                    first digit; the 'u'/'mm' unit suffixes then line up
                    with 'u' sitting above the first 'm' of 'mm')."""
                    lead = f'{label:<4}  '   # 'abs '/'rel ' -> 6 chars
                    seg1 = f'{lead}{xv_str}  {yv_str} {unit}'
                    seg2 = f'{" "*len(lead)}{xv_mm:+10.4f}  {yv_mm:+10.4f} mm'
                    return seg1, seg2

                abs1, abs2 = _dual('abs', f'{fsx:+10d}', f'{fsy:+10d}', 'u',
                                   sx*MM_PER_UNIT, sy*MM_PER_UNIT)
                rel1, rel2 = _dual('rel', f'{rx:+10d}', f'{ry:+10d}', 'u',
                                   rel_mm_x, rel_mm_y)
                sep = '  │  '
                line1 = f'  {abs1} {sep}{rel1}{sep}×{vp.scale:.3f}'
                line2 = f'  {abs2}{sep}{rel2}'
                self.status_cb(line1 + '\n' + line2)

        def mouseReleaseEvent(self, e):
            if self._drag_pos is not None:
                press = QPointF(self._drag_pos)
                pos = e.position()
                still = (abs(pos.x() - press.x()) + abs(pos.y() - press.y())
                         <= QApplication.startDragDistance())
                if e.button() == Qt.RightButton and still and not self._drag_painted:
                    # A right-click without a pan: the part menu. Opened
                    # after this event, once the context-menu request the
                    # release makes (on Windows) has been swallowed by
                    # _menu_blocker, and as a popup, so nothing waits on it.
                    gpos = e.globalPosition().toPoint()
                    QTimer.singleShot(0, lambda: self._open_part_menu(press, gpos))
                self._drag_pos = self._drag_at = None
                self._pan_free_timer.start()
                # The exact rendering at the final position -- needed only if
                # a pan step replaced it (a moved view not yet painted is
                # rendered exactly anyway): a click without a move costs nothing.
                if self._drag_painted:
                    self._update_pan()

        def resizeEvent(self, e):
            if self._auto_fit and self.sheet:
                self._fit_view()
            self._update_status()
            self._schedule()

        def keyPressEvent(self, e):
            if   e.key() == Qt.Key_F: self._fit_sheet()
            elif e.key() == Qt.Key_Space:
                if self._last_screen:
                    self._update_status()   # ensure _snap_wx/_snap_wy are current
                    self._ref_wx = self._snap_wx
                    self._ref_wy = self._snap_wy
                    self._ref_set = True
                    self._update_status()
                    self.update()
            elif e.key() == Qt.Key_C:
                self._full_crosshair = not self._full_crosshair
                if self.crosshair_cb: self.crosshair_cb(self._full_crosshair)
                self.update()
            elif e.key() in (Qt.Key_Plus, Qt.Key_Equal):
                if self.snap_step_cb: self.snap_step_cb(+1)
            elif e.key() == Qt.Key_Minus:
                if self.snap_step_cb: self.snap_step_cb(-1)
            elif e.key() == Qt.Key_B:
                if self.back_cb: self.back_cb()
            elif e.key() == Qt.Key_Escape:
                if self._inspected_comp is not None or self._inspected_annot is not None:
                    self._inspected_comp  = None
                    self._inspected_annot = None
                    if self.inspect_cb: self.inspect_cb()
                    self.update()
                # No highlight active: Esc does nothing rather than
                # quitting -- an unexpected quit is exactly what this
                # change was meant to avoid. Q is the sole quit key now.
            elif e.key() == Qt.Key_Q:
                self.window().close()

        # ── paint ─────────────────────────────────────────────────────────────

        # The rendered sheet is kept as an image (_sheet_image) and only
        # re-rendered when something it shows changes. A cursor move only
        # moves the cursor marker, drawn over the image: before, every move
        # (up to 60 a second) re-rendered the whole sheet, which on a large
        # design at zoom-to-fit takes far longer than the 16 ms between
        # moves -- the cursor stuttered and lagged behind the mouse.
        #
        # update() -- called by every change of view, layer, highlight,
        # theme or sheet, directly or through the debounce timer -- marks
        # the image stale; _update_cursor() repaints without doing so.
        # A resize, or a move to a screen with another pixel ratio, is
        # caught by the size check in paintEvent.
        #
        # Panning (a right/middle drag) does not render either. Rendering is
        # almost all Python work per object -- ~130 ms on the largest test
        # sheet at zoom-to-fit, still ~40 ms zoomed far in, however little is
        # visible -- so a pan that re-renders cannot keep up with the mouse.
        # Instead:
        #   1. the whole sheet is recorded once as drawing commands (a
        #      QPicture, _record_pan_picture) -- the only Python work;
        #   2. the recording is replayed, in C++, into an image of everything
        #      the drag can still bring into view (the pan image). The pan
        #      stops where the cursor leaves the window, so that is known: the
        #      view plus, on each side, the cursor's distance to the opposite
        #      edge -- always twice the window's width and height. Both are
        #      made at the button press (_prepare_pan), so the moment before
        #      the mouse moves hides the work;
        #   3. each pan step shows the pan image shifted -- a plain copy. It is
        #      replayed again only if that image would exceed its memory limit
        #      and was made smaller (_make_pan_image), or after a change that
        #      discards it (replaying costs about as much wherever it is
        #      clipped, so a larger image, not a partial one, saves time).
        # So every step shows the complete sheet at its new position, however
        # far or fast the pan goes. The recording leaves out the grid (it
        # spans the window, not the sheet); the pan image draws it. Both stay
        # valid while only the position changes; update() -- anything else
        # changing -- discards them; the pan image is also freed shortly after
        # a drag. Releasing the button renders exactly once more if the drag
        # showed a pan step.
        #
        # Wheel zoom works the same way, from a recording replayed scaled:
        # while the wheel turns, each step is a replay (_paint_zoom_preview);
        # once it has stopped (_ZOOM_SETTLE_MS) the view is rendered exactly.
        # A zoom keeps the recording -- it is only scaled -- but a drag
        # records again at the new zoom when it starts, so its pan steps show
        # the exact drawing. A zoom during a drag replays it scaled until the
        # release.

        def update(self, *args):
            self._sheet_image_stale = True
            self._pan_picture = None
            self._pan_image = None
            super().update(*args)

        def _update_pan(self):
            """Repaint for a change of position only: the recording is reused."""
            self._sheet_image_stale = True
            super().update()

        def _update_cursor(self):
            """Repaint for a cursor move only: the sheet image is reused."""
            super().update()

        def _render_sheet_image(self):
            dpr = self.devicePixelRatioF()
            img = QPixmap(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
            img.setDevicePixelRatio(dpr)
            p = QPainter(img)
            try:
                p.setRenderHint(QPainter.Antialiasing, True)
                theme_colours = self._theme_colours()
                p.fillRect(self.rect(), theme_colours['bg'] if theme_colours else BG)
                if self.sheet and self._drag_pos is not None:
                    self._paint_from_pan_picture(p, theme_colours)
                elif self.sheet and self._zoom_preview:
                    self._paint_zoom_preview(p, theme_colours)
                elif self.sheet:
                    self._paint(p, colour_overrides=theme_colours)
            finally:
                p.end()
            self._sheet_image = img
            self._sheet_image_key = (self.width(), self.height(), dpr)
            self._sheet_image_stale = False

        def _theme_colours(self):
            """The colour overrides of the light theme, or None for the dark one."""
            return _light_theme_colours() if self._theme == 'light' else None

        def _prepare_pan(self):
            """At a right/middle press: have the pan image ready before the first
            move -- recording the sheet first if something changed -- so the
            time until the mouse moves hides the work. An image kept from the
            previous drag is used if it serves every position this drag can
            reach."""
            self._pan_free_timer.stop()
            if not self.sheet:
                return
            theme_colours = self._theme_colours()
            if self._pan_picture is None or self._pan_picture_at[2] != self._vp.scale:
                self._record_pan_picture(theme_colours)
                self._pan_image = None
            if not self._pan_image_covers_drag():
                self._make_pan_image(theme_colours)

        def _pan_image_covers_drag(self):
            """Whether the pan image serves a whole drag from the current press:
            the view stays inside it wherever the cursor goes in the window."""
            if self._pan_image is None:
                return False
            ox, oy, left, right, top, bottom, _, w, h, dpr, s = self._pan_image_at
            if (w, h, dpr, s) != (self.width(), self.height(), self.devicePixelRatioF(), self._vp.scale):
                return False
            cx, cy = self._drag_at
            dx, dy = self._vp.ox - ox, self._vp.oy - oy
            return (dx + w - cx <= left and dx - cx >= -right
                    and dy + h - cy <= top and dy - cy >= -bottom)

        def _free_pan_image(self):
            if self._drag_pos is None:
                self._pan_image = None

        def _record_pan_picture(self, theme_colours):
            self._pan_picture, self._pan_picture_at = self._record_sheet(theme_colours)

        def _record_sheet(self, theme_colours):
            """Record the whole sheet, grid left out, as _paint draws it at the
            current zoom. It is recorded under a view shifted by a whole number
            of pixels so that its top-left lies near (0, 0); that keeps the
            sub-pixel part of the position, so a replay at the recording's own
            position matches a rendering there pixel for pixel. The recorded
            area is the sheet's content plus a quarter of its size on each side
            (and the origin): nothing is culled that the rendering would draw.
            Returns the recording and (x, y offset, zoom) to replay it by."""
            sh, vp = self.sheet, self._vp
            s = vp.scale
            b = _tight_bounds_with_gap(sh, _TIGHT_BOUNDS_GAP_UNITS) or (sh.xmin, sh.ymin, sh.xmax, sh.ymax)
            x0, y0, x1, y1 = min(b[0], 0), min(b[1], 0), max(b[2], 0), max(b[3], 0)
            m = max(x1 - x0, y1 - y0) / 4
            x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
            rec = _VP()
            rec.scale = s
            rec.ox = vp.ox - math.floor(vp.ox + x0 * s)
            rec.oy = vp.oy - math.floor(vp.oy - y1 * s)
            size = (math.ceil((x1 - x0) * s) + 2, math.ceil((y1 - y0) * s) + 2)
            pic = QPicture()
            p = QPainter(pic)
            self._vp = rec
            try:
                p.setRenderHint(QPainter.Antialiasing, True)
                self._paint(p, colour_overrides=theme_colours, canvas_size=size, skip_grid=True)
            finally:
                self._vp = vp
                p.end()
            return pic, (rec.ox, rec.oy, s)

        def _replay_recording(self, p, vp):
            """Replay the recording as the view vp shows it -- shifted, and
            scaled if vp's zoom differs from the recording's (line widths and
            marker sizes are then scaled too, not drawn anew for the zoom: fine
            for a preview, replaced by an exact render after)."""
            pic, (rox, roy, rs) = self._pan_picture, self._pan_picture_at
            r = vp.scale / rs
            if r == 1.0:
                p.drawPicture(QPointF(vp.ox - rox, vp.oy - roy), pic)
                return
            p.save()
            p.translate(vp.ox - rox * r, vp.oy - roy * r)
            p.scale(r, r)
            p.drawPicture(0, 0, pic)
            p.restore()

        def _paint_zoom_preview(self, p, theme_colours):
            """One wheel-zoom step: the grid, then the recording scaled to the new
            zoom. Text scales true to size, as the render draws it (_text_font).
            Markers and pen widths, which the render adapts to the zoom, are
            only scaled -- so the recording is made again once the zoom is three
            times its own, or a third."""
            if (self._pan_picture is None
                    or not 1 / 3 <= self._vp.scale / self._pan_picture_at[2] <= 3):
                self._record_pan_picture(theme_colours)
                self._pan_image = None
            if self._vis['grid']:
                self._paint_grid(p, self._vp, self.width(), self.height(),
                                 (theme_colours or {}).get('grid', QColor(_COL_GRID)))
            self._replay_recording(p, self._vp)

        def _make_pan_image(self, theme_colours):
            """Replay the recording, with background and grid, into an image of
            the view plus what the rest of the drag can bring into view: moving
            the cursor right as far as the window allows uncovers that distance
            on the left, and so on. The image is then exactly twice the window's
            width and height, and never needs replaying during the drag -- unless
            that exceeds _PAN_IMAGE_MAX_PIXELS: then every margin is scaled down
            to fit (to a quarter at least), and the image is replayed again when
            a pan nears its edge."""
            w, h, dpr, vp = self.width(), self.height(), self.devicePixelRatioF(), self._vp
            cx, cy = self._drag_at
            # margins total w and h at k = 1, so the image is w*h*(1+k)^2
            k = min(1.0, max(0.25, (_PAN_IMAGE_MAX_PIXELS / (w * h * dpr * dpr)) ** 0.5 - 1))
            # (a pixel more than the reach: the pan offset is a float sum)
            left, right = math.ceil((w - cx) * k) + 1, math.ceil(cx * k) + 1
            top, bottom = math.ceil((h - cy) * k) + 1, math.ceil(cy * k) + 1
            iw, ih = w + left + right, h + top + bottom
            img = QPixmap(round(iw * dpr), round(ih * dpr))
            img.setDevicePixelRatio(dpr)
            around = _VP()
            around.scale, around.ox, around.oy = vp.scale, vp.ox + left, vp.oy + top
            p = QPainter(img)
            try:
                p.setRenderHint(QPainter.Antialiasing, True)
                p.fillRect(QRectF(0, 0, iw, ih), theme_colours['bg'] if theme_colours else BG)
                if self._vis['grid']:
                    self._paint_grid(p, around, iw, ih, (theme_colours or {}).get('grid', QColor(_COL_GRID)))
                self._replay_recording(p, around)
            finally:
                p.end()
            self._pan_image = img
            self._pan_image_at = (vp.ox, vp.oy, left, right, top, bottom, k, w, h, dpr, vp.scale)

        def _paint_from_pan_picture(self, p, theme_colours):
            """One pan step: the pan image, shifted to the current position --
            recorded and replayed first where needed (see the note above
            update())."""
            if self._pan_picture is None:
                self._record_pan_picture(theme_colours)
                self._pan_image = None
            vp = self._vp
            if self._pan_image is not None:
                ox, oy, left, right, top, bottom, k, w, h, dpr, s = self._pan_image_at
                # A full-reach image (k = 1) covers every position the drag
                # can reach. A reduced one is replayed again once the view is
                # two thirds of the way to its edge, so a pan never reaches the
                # edge between two steps.
                f = 1.0 if k >= 1.0 else 2 / 3
                dx, dy = vp.ox - ox, vp.oy - oy
                if ((w, h, dpr, s) != (self.width(), self.height(), self.devicePixelRatioF(), vp.scale)
                        or dx > left * f or -dx > right * f or dy > top * f or -dy > bottom * f):
                    self._pan_image = None
            if self._pan_image is None:
                self._make_pan_image(theme_colours)
            ox, oy, left, _, top = self._pan_image_at[:5]
            p.drawPixmap(QPointF(vp.ox - ox - left, vp.oy - oy - top), self._pan_image)
            self._drag_painted = True

        def paintEvent(self, _):
            if (self._sheet_image_stale or self._sheet_image is None
                    or self._sheet_image_key != (self.width(), self.height(),
                                                 self.devicePixelRatioF())):
                try:
                    self._render_sheet_image()
                except Exception as e:
                    # A drawing-time exception must not leave the widget
                    # unpainted; the next repaint tries again.
                    print(f'paintEvent error: {e!r}', file=sys.stderr)
            p = QPainter(self)
            try:
                if self._sheet_image is not None:
                    p.drawPixmap(0, 0, self._sheet_image)
                p.setRenderHint(QPainter.Antialiasing, True)
                self._paint_snap_cursor(p)   # always shown — mouse position aid
            except Exception as e:
                # Never let a drawing-time exception skip p.end() — that
                # leaves the painter attached to the widget's backing store,
                # and every subsequent repaint then collides with it
                # ("QBackingStore::endPaint() called with active painter").
                print(f'paintEvent error: {e!r}', file=sys.stderr)
            finally:
                p.end()

        def _paint(self, p: QPainter, export_mode: bool = False, force_mk: bool = False,
                   colour_overrides: dict = None, canvas_size: tuple = None,
                   skip_grid: bool = False):
            """Paint the current sheet onto painter p.

            colour_overrides: optional dict with keys 'bg', 'wire', 'bus',
            'junction' supplying QColor objects.  When present these replace
            the closure-captured BG/WIRE/BUS/JUNC constants so that PDF export
            can apply print/BW palettes without patching module globals.

            canvas_size: optional (width, height) in device pixels overriding
            self.width()/height() for culling.  Used by PDF export so the
            virtual canvas dimensions (vcw, vch) are used instead of the screen
            widget size.

            skip_grid: leave the grid out (the pan recording, which draws it
            separately -- see _record_pan_picture).
            """
            vp  = self._vp
            sh  = self.sheet
            vis = self._vis
            mk  = True if force_mk else self._show_markers
            cw, ch = canvas_size if canvas_size else (self.width(), self.height())

            # While a *X/*A label or a symbol is click-inspected, the rest
            # of the schematic is dimmed and the inspected item is redrawn
            # at full opacity on top at the very end (see
            # _draw_inspected_annot_overlay / _draw_inspected_comp_overlay)
            # -- but never during export (SVG/PNG/PDF have their own,
            # separate render paths anyway; this only guards a stray future
            # caller passing export_mode=True through this Qt-canvas path).
            # The grid is drawn below BEFORE the dim opacity is applied, so
            # it stays at full brightness as a fixed spatial reference even
            # while everything else is dimmed.
            dim_active = ((self._inspected_annot is not None or
                           self._inspected_comp  is not None)
                          and not export_mode)

            # Apply colour overrides (PDF print/BW palette) or use viewer defaults
            _co = colour_overrides or {}
            _BG          = _co.get('bg',            BG)
            _WIRE        = _co.get('wire',          WIRE)
            _BUS         = _co.get('bus',           BUS)
            _JUNC        = _co.get('junction',      JUNC)
            _TEXT_DEF    = _co.get('text_default',  QColor('white'))
            _STUB_COL    = _co.get('stub',          QColor(_COL_STUB))
            _SYM_BODY    = _co.get('sym_body',      None)   # None → use per-object palette
            _ATTR_COL    = _co.get('attr',          QColor(_COL_ATTR_FALLBACK))
            _PIN_NAME    = _co.get('pin_name',      None)   # None → use per-object palette
            _PIN_NUM     = _co.get('pin_number',    None)   # None → use per-object palette
            _FORCE_COL   = _co.get('force_colours', False)
            _JUNC_SCALE  = _co.get('junction_scale', 1.0)
            _GRID_COL    = _co.get('grid',           QColor(_COL_GRID))
            _BORDER_COL  = _co.get('border',         QColor(_COL_BORDER))
            _ORIGIN_COL  = _co.get('origin',         QColor(_COL_ORIGIN))

            # Viewport bounds in world coords
            vx0,vy0,vx1,vy1 = vp.wx(0),vp.wy(ch),vp.wx(cw),vp.wy(0)
            def in_view(x0,y0,x1,y1):
                return not(x1<vx0 or x0>vx1 or y1<vy0 or y0>vy1)

            # ── grid ──────────────────────────────────────────────────────────
            # Drawn at full opacity (before dim_active's opacity is applied
            # below) so it stays visible as a spatial reference even while
            # an inspected label/symbol dims everything else.
            if vis['grid'] and not skip_grid:
                self._paint_grid(p, vp, cw, ch, _GRID_COL)

            if dim_active:
                p.setOpacity(_DIM_OPACITY)

            # ── sheet border ──────────────────────────────────────────────────
            # Always drawn from tight_bounds + GAP so the border closely wraps
            # actual content.  In PDF export_mode this block is not reached since
            # _export_sheet_pdf() draws its own border before calling _paint().
            if vis['sheet_border'] and not export_mode:
                _tb = _tight_bounds_with_gap(sh, _TIGHT_BOUNDS_GAP_UNITS)
                if _tb:
                    _bx0, _by0, _bx1, _by1 = _tb
                else:
                    _bx0, _by0 = sh.xmin, sh.ymin
                    _bx1, _by1 = sh.xmax, sh.ymax
                p.setPen(QPen(_BORDER_COL, 1))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(vp.cx(_bx0), vp.cy(_by1),
                                  vp.cs(_bx1 - _bx0), vp.cs(_by1 - _by0)))
                p.setPen(QPen(_ORIGIN_COL,1))
                ox=vp.cx(0); oy=vp.cy(0)
                _oa = _ORIGIN_CROSSHAIR_ARM_PX
                p.drawLine(int(ox-_oa),int(oy),int(ox+_oa),int(oy))
                p.drawLine(int(ox),int(oy-_oa),int(ox),int(oy+_oa))

            # ── user lines ────────────────────────────────────────────────────
            if vis['user_lines']:
                for ul in sh.user_lines:
                    if not in_view(min(ul.x1,ul.x2),min(ul.y1,ul.y2),
                                   max(ul.x1,ul.x2),max(ul.y1,ul.y2)): continue
                    # Drawn as symbol lines are: a .UTSCH line's stored pen
                    # width, a .SCH line's thin or thick width -- both scale
                    # with zoom.
                    _ul_w = max(1, round(vp.cs(_pen_units(ul.pen_width, ul.thickness != 6))))
                    pen = QPen(_TEXT_DEF if _FORCE_COL else _qcol_u(ul.colour), _ul_w)
                    _ul_dash = _DASH_BY_USERLINE_TYPE.get(ul.linetype)
                    if _ul_dash: pen.setDashPattern(_ul_dash)
                    else:                  pen.setCapStyle(Qt.FlatCap)
                    p.setPen(pen)
                    p.drawLine(QPointF(vp.cx(ul.x1),vp.cy(ul.y1)),
                               QPointF(vp.cx(ul.x2),vp.cy(ul.y2)))
                # Sheet arcs and circles (.UTSCH only), on the same layer
                for ua in sh.user_arcs:
                    cx, cy, r, sd, sw = _user_arc_geometry(ua)
                    if not in_view(cx - r, cy - r, cx + r, cy + r): continue
                    pen = QPen(_TEXT_DEF if _FORCE_COL else _qcol_u(ua.colour),
                               max(1, round(vp.cs(_pen_units(ua.pen_width, False)))))
                    _ua_dash = _DASH_BY_STYLE_NAME.get(UC_ARC_STYLE_BY_CODE.get(ua.arc_linetype, 'solid'))
                    if _ua_dash: pen.setDashPattern(_ua_dash)
                    else:                  pen.setCapStyle(Qt.FlatCap)
                    p.setPen(pen); p.setBrush(Qt.NoBrush)
                    cr = vp.cs(r)
                    rect = QRectF(vp.cx(cx)-cr, vp.cy(cy)-cr, cr*2, cr*2)
                    if ua.is_full_circle: p.drawEllipse(rect)
                    else:                 p.drawArc(rect, int(sd*16), int(sw*16))

            # Anchor/POE markers are collected here and drawn in one final
            # pass at the end of this function, instead of inline as each
            # element is painted -- otherwise a later-drawn symbol/label can
            # visually cover an earlier-drawn marker (e.g. a wire-start
            # marker getting painted over by a symbol on top of it), since
            # markers were interleaved with the very geometry they're meant
            # to always stay visible above.
            pending_markers = []

            # ── wires + buses ─────────────────────────────────────────────────
            for wire in sh.wires:
                if wire.is_bus and not vis['buses']: continue
                if not wire.is_bus and not vis['wires']: continue
                if not in_view(min(wire.x1,wire.x2),min(wire.y1,wire.y2),
                               max(wire.x1,wire.x2),max(wire.y1,wire.y2)): continue
                pen = QPen(_BUS if wire.is_bus else _WIRE,
                           max(1, round(vp.cs(_thin_thick_width_units(wire.is_bus)))))
                pen.setCapStyle(Qt.FlatCap)
                p.setPen(pen)
                p.drawLine(QPointF(vp.cx(wire.x1),vp.cy(wire.y1)),
                           QPointF(vp.cx(wire.x2),vp.cy(wire.y2)))
                if mk and vis['anc_wire']:
                    pending_markers.append(('diamond',vp.cx(wire.x1),vp.cy(wire.y1),
                                            _COL_ANC_WIRE,
                                            _ANC_MARKER_BASE_THICK if wire.is_bus else _ANC_MARKER_BASE_THIN,
                                            wire.is_bus))

            # ── junctions ─────────────────────────────────────────────────────
            if vis['junctions'] and mk:
                p.setPen(Qt.NoPen); p.setBrush(_JUNC)
                for j in sh.junctions:
                    if not in_view(j.x,j.y,j.x,j.y): continue
                    r = max(_JUNC_MIN_RADIUS_PX,
                            vp.cs(_junc_radius_units(j.is_bus_entry) * _JUNC_SCALE))
                    cx=vp.cx(j.x); cy=vp.cy(j.y)
                    p.drawEllipse(QRectF(cx-r,cy-r,r*2,r*2))

            # ── symbols ───────────────────────────────────────────────────────
            inspected = self._inspected_comp
            for comp in sh.components:
                is_insp = comp is inspected
                if dim_active and is_insp:
                    continue   # redrawn full-opacity in the overlay pass below instead
                if not vis['symbols'] and not is_insp:
                    continue
                sym = sh.symbols.get(comp.symbol_name)
                rr  = 500 if sym is None else max(sym.width,sym.height,100)
                if not is_insp and not in_view(comp.x-rr,comp.y-rr,comp.x+rr,comp.y+rr):
                    continue
                self._paint_component(p, comp, sym, mk, vis, vp, _BG,
                                      _STUB_COL, _SYM_BODY, _ATTR_COL,
                                      _PIN_NAME, _PIN_NUM, _FORCE_COL,
                                      pending_markers=pending_markers,
                                      force_inspect=is_insp)
                if is_insp:
                    x0,y0,x1,y1 = _comp_bbox_world(comp, sym)
                    pen = QPen(QColor(_COL_ANC_COMP), 1, Qt.DashLine)
                    p.setPen(pen); p.setBrush(Qt.NoBrush)
                    p.drawRect(QRectF(vp.cx(x0), vp.cy(y1), vp.cs(x1-x0), vp.cs(y1-y0)))

            # ── labels (*X) ───────────────────────────────────────────────────
            if vis['labels']:
                for lbl in sh.labels:
                    if not self._text_may_be_visible(lbl.x, lbl.y, lbl.text,
                                                     lbl.size or 35, vx0,vy0,vx1,vy1):
                        continue
                    col = _TEXT_DEF if _FORCE_COL else (_qcol_u(lbl.colour) if lbl.colour else _TEXT_DEF)
                    h,v,ang = _u_to_halign_valign_angle(lbl.align)
                    ang = x_text_angle(lbl.rotation)
                    self._draw_text(p,vp.cx(lbl.x),vp.cy(lbl.y),
                                    lbl.text,lbl.size or 35,
                                    col,h,v,ang)
                    if mk and vis['anc_ann']:
                        pending_markers.append(('diamond',vp.cx(lbl.x),vp.cy(lbl.y),_COL_ANC_ANN,4,False))

            # ── annotations (*A) ──────────────────────────────────────────────
            if vis['annotations']:
                _show_hidden_ann = vis.get('annotations_hidden', False)
                for ann in sh.annotations:
                    _ann_hidden = not (ann.visibility & 128)
                    if _ann_hidden and not _show_hidden_ann:
                        continue   # hidden in Ulticap: not drawn (and not clickable)
                    # Culled on the TEXT's own anchor (ann.x/y), not the POE
                    # (ann.x_poe/y_poe) -- the POE can be far from the text
                    # (that's the whole point of *A having a separate POE),
                    # so testing it instead of the anchor was hiding text
                    # that was clearly on-screen whenever its POE wasn't.
                    if not self._text_may_be_visible(ann.x, ann.y, ann.text,
                                                     ann.size or 35, vx0,vy0,vx1,vy1):
                        continue
                    col = _TEXT_DEF if _FORCE_COL else (_qcol_u(ann.colour) if ann.colour else _TEXT_DEF)
                    u   = ulticap_translate(0,ann.visibility)
                    h,v,ang = _u_to_halign_valign_angle(u)
                    self._draw_text(p,vp.cx(ann.x),vp.cy(ann.y),
                                    ann.text,ann.size or 35,
                                    col,h,v,ang,italic=_ann_hidden)
                    if mk and vis['poe_ann']:
                        pending_markers.append(('poe',vp.cx(ann.x_poe),vp.cy(ann.y_poe),None,4,False))
                    if mk and vis['anc_ann']:
                        pending_markers.append(('diamond',vp.cx(ann.x),vp.cy(ann.y),_COL_ANC_ANN,4,False))

            # Final pass: draw every collected marker on top of everything
            # else painted above, so a symbol/label/wire drawn later never
            # visually covers an earlier marker.
            for kind, mcx, mcy, mcol, ms, mthick in pending_markers:
                if kind == 'diamond':
                    self._draw_diamond(p, mcx, mcy, mcol, ms, mthick)
                else:
                    self._draw_poe_sq(p, mcx, mcy, ms, mthick)

            # Restore full opacity (must happen even if nothing else is
            # drawn below -- callers after _paint(), e.g. _paint_snap_cursor
            # in paintEvent, must never inherit the dimmed opacity) and, if
            # something is click-inspected, redraw it full-brightness on
            # top of the now-dimmed rest of the sheet -- a *X/*A label
            # (with its POE/connector) or a symbol (body, pins, and every
            # attribute). Height/coordinates go to the status bar's
            # selection section instead of an on-canvas readout.
            if dim_active:
                p.setOpacity(1.0)
                if self._inspected_annot is not None:
                    self._draw_inspected_annot_overlay(p, vp, _TEXT_DEF, _FORCE_COL)
                if self._inspected_comp is not None:
                    self._draw_inspected_comp_overlay(p, vp, sh, mk, vis, _BG,
                                                      _STUB_COL, _SYM_BODY, _ATTR_COL,
                                                      _PIN_NAME, _PIN_NUM, _FORCE_COL)

        def _paint_grid(self, p, vp, cw, ch, colour):
            """The grid over a cw x ch canvas, unless too dense to show."""
            g = self._grid
            if vp.cs(g) < _GRID_MIN_PX:
                return
            vx0, vy0, vx1, vy1 = vp.wx(0), vp.wy(ch), vp.wx(cw), vp.wy(0)
            p.setPen(QPen(colour, 1))
            x = math.floor(vx0/g)*g
            while x <= vx1+g:
                cpx = vp.cx(x)
                if 0 <= cpx <= cw: p.drawLine(int(cpx),0,int(cpx),ch)
                x += g
            y = math.floor(vy0/g)*g
            while y <= vy1+g:
                cpy = vp.cy(y)
                if 0 <= cpy <= ch: p.drawLine(0,int(cpy),cw,int(cpy))
                y += g

        def _draw_inspected_annot_overlay(self, p, vp, default_col, force_colours=False):
            """Full-opacity redraw of the currently click-inspected *X
            label, *A annotation, or sheet origin, on top of the dimmed
            rest of the sheet (see the dim_active block in _paint). Also
            draws a dashed connector between an annotation's anchor and
            POE -- useful when the two are far apart, since
            _reframe_for_annot may have just zoomed out to fit both. Height/
            coordinates for the inspected item are shown in the status
            bar's selection section (see MainWindow._on_inspection_changed),
            not drawn here -- an on-canvas floating readout risked covering
            the very item it described.
            force_colours mirrors the normal (dimmed-pass) rendering rule:
            when set (light/print theme), a custom per-object colour is
            ignored in favour of default_col, instead of always falling
            back to the raw screen palette -- otherwise the highlighted
            item would pop back to its screen colour while everything
            else around it uses the theme's flattened colours."""
            kind, obj = self._inspected_annot
            if kind == 'origin':
                ox, oy = vp.cx(0), vp.cy(0)
                arm = _ORIGIN_CROSSHAIR_ARM_PX * 2.5
                p.setPen(QPen(QColor(_COL_ANC_ANN), 2))
                p.drawLine(QPointF(ox-arm, oy), QPointF(ox+arm, oy))
                p.drawLine(QPointF(ox, oy-arm), QPointF(ox, oy+arm))
            elif kind == 'label':
                col = default_col if force_colours else (_qcol_u(obj.colour) if obj.colour else default_col)
                h, v, ang = _u_to_halign_valign_angle(obj.align)
                ang = x_text_angle(obj.rotation)
                ax, ay = vp.cx(obj.x), vp.cy(obj.y)
                self._draw_text(p, ax, ay, obj.text, obj.size or 35, col, h, v, ang)
                self._draw_diamond(p, ax, ay, _COL_ANC_ANN, _ANC_MARKER_BASE_THICK, True)
            else:
                ann = obj
                col = default_col if force_colours else (_qcol_u(ann.colour) if ann.colour else default_col)
                u = ulticap_translate(0, ann.visibility)
                h, v, ang = _u_to_halign_valign_angle(u)
                ax, ay   = vp.cx(ann.x),     vp.cy(ann.y)
                px, py   = vp.cx(ann.x_poe), vp.cy(ann.y_poe)
                pen = QPen(QColor(_COL_ANC_ANN), 1, Qt.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(ax, ay), QPointF(px, py))
                self._draw_text(p, ax, ay, ann.text, ann.size or 35, col, h, v, ang)
                self._draw_diamond(p, ax, ay, _COL_ANC_ANN, _ANC_MARKER_BASE_THICK, True)
                self._draw_poe_sq(p, px, py, _ANC_MARKER_BASE_THICK, True)

        def _draw_inspected_comp_overlay(self, p, vp, sh, mk, vis, bg_colour,
                                         stub_colour, sym_body_colour, attr_colour,
                                         pin_name_colour, pin_number_colour, force_colours):
            """Full-opacity redraw of the currently click-inspected symbol
            -- body, pins, and ALL its attributes (refdes, value, pin
            names/numbers, via force_inspect's visibility override) -- on
            top of the dimmed rest of the sheet, plus its dashed bounding
            box. Mirrors _draw_inspected_annot_overlay's treatment of
            *X/*A labels, so a component click gets the same 'dim
            everything else' highlighting."""
            comp = self._inspected_comp
            sym = sh.symbols.get(comp.symbol_name)
            fresh_markers = []
            self._paint_component(p, comp, sym, mk, vis, vp, bg_colour,
                                  stub_colour, sym_body_colour, attr_colour,
                                  pin_name_colour, pin_number_colour, force_colours,
                                  pending_markers=fresh_markers,
                                  force_inspect=True)
            for kind, mcx, mcy, mcol, ms, mthick in fresh_markers:
                if kind == 'diamond':
                    self._draw_diamond(p, mcx, mcy, mcol, ms, mthick)
                else:
                    self._draw_poe_sq(p, mcx, mcy, ms, mthick)
            x0, y0, x1, y1 = _comp_bbox_world(comp, sym)
            pen = QPen(QColor(_COL_ANC_COMP), 1, Qt.DashLine)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(vp.cx(x0), vp.cy(y1), vp.cs(x1-x0), vp.cs(y1-y0)))

        def _paint_component(self, p, comp, *args, **kwargs):
            """Draw one placed part (_paint_component_body). A part left out
            of the shown schematic variant (comp.fitted False) is drawn
            faded, whatever else is on; its texts are italic. Kept visible
            and clickable: the status bar says why it is faded."""
            if comp.fitted:
                self._paint_component_body(p, comp, *args, **kwargs)
                return
            p.save()
            p.setOpacity(p.opacity() * _NOT_FITTED_OPACITY)
            try:
                self._paint_component_body(p, comp, *args, **kwargs)
            finally:
                p.restore()

        def _paint_component_body(self, p, comp, sym, mk, vis, vp, bg_colour=None,
                                  stub_colour=None, sym_body_colour=None,
                                  attr_colour=None, pin_name_colour=None,
                                  pin_number_colour=None, force_colours=False,
                                  pending_markers=None, force_inspect=False):
            rot = comp.rotation
            # Click-to-inspect overrides the ambient marker-list/mute state
            # for this one component (an explicit, deliberate action takes
            # priority -- see _on_inspect_click). While inspecting, each
            # marker TYPE's visibility is decided SOLELY by the persistent
            # per-type filter from the "Selected" column
            # (_inspect_marker_vis) -- not unioned with the ambient
            # mk/vis state, otherwise toggling a type off here would have
            # no visible effect whenever the main list already had that
            # type on (which is the normal case outside of Hide all).
            def _marker_shown(key):
                if force_inspect:
                    return self._inspect_marker_vis.get(key, True)
                return mk and vis[key]
            if _marker_shown('anc_comp'):
                pending_markers.append(('diamond',vp.cx(comp.x),vp.cy(comp.y),_COL_ANC_COMP,4,False))

            if sym is None:
                p.setPen(QPen(QColor(_COL_MISSING),1))
                s=6; cx=vp.cx(comp.x); cy=vp.cy(comp.y)
                p.drawLine(QPointF(cx-s,cy-s),QPointF(cx+s,cy+s))
                p.drawLine(QPointF(cx-s,cy+s),QPointF(cx+s,cy-s))
                return

            # polylines
            for pl in sym.polylines:
                col = sym_body_colour if force_colours else (_qcol_u(pl.colour) if pl.colour else (sym_body_colour if sym_body_colour is not None else QColor(_COL_STUB)))
                pts = []
                for dx,dy in pl.points:
                    odx,ody=_rot_transform(dx,dy,rot)
                    pts.append(QPointF(vp.cx(comp.x+odx),vp.cy(comp.y+ody)))
                if len(pts)>=2:
                    pen = QPen(col, max(1, round(vp.cs(_pen_units(pl.pen_width, pl.width != 6)))))
                    _pl_dash = _DASH_BY_STYLE_NAME.get(UC_POLYLINE_STYLE_BY_CODE.get(pl.linetype, 'solid'))
                    if _pl_dash: pen.setDashPattern(_pl_dash)
                    else:                    pen.setCapStyle(Qt.FlatCap)
                    if pl.filled and len(pts) >= 3:
                        # .UTSCH fill, under the outline (the outline is
                        # drawn segment by segment below, as before)
                        p.setPen(Qt.NoPen)
                        p.setBrush(sym_body_colour if force_colours and sym_body_colour is not None
                                   else _qcol_u(pl.fill_colour))
                        p.drawPolygon(QPolygonF(pts))
                    p.setPen(pen); p.setBrush(Qt.NoBrush)
                    for i in range(len(pts)-1):
                        p.drawLine(pts[i],pts[i+1])

            # circles / arcs
            for ci in sym.circles:
                col = sym_body_colour if force_colours else (_qcol_u(ci.colour) if ci.colour else (sym_body_colour if sym_body_colour is not None else QColor(_COL_STUB)))
                pen = QPen(col, max(1, round(vp.cs(_pen_units(ci.pen_width, ci.thick != 6)))))
                _ci_dash = _DASH_BY_STYLE_NAME.get(UC_ARC_STYLE_BY_CODE.get(ci.arc_linetype, 'solid'))
                if _ci_dash: pen.setDashPattern(_ci_dash)
                else:                    pen.setCapStyle(Qt.FlatCap)
                p.setPen(pen)
                p.setBrush((sym_body_colour if force_colours and sym_body_colour is not None
                            else _qcol_u(ci.fill_colour)) if ci.filled else Qt.NoBrush)
                odx,ody=_rot_transform(ci.cx,ci.cy,rot)
                scx=comp.x+odx; scy=comp.y+ody
                cr=vp.cs(ci.r)
                # An elongated ellipse or a pie (.UTSCH): its closed outline
                # (_ellipse_or_pie_points; the converter writes KiCad curves),
                # placed point by point so rotation and mirroring stay exact.
                outline = _ellipse_or_pie_points(ci)
                if outline is not None:
                    p.drawPolygon(QPolygonF([
                        QPointF(vp.cx(comp.x + tx), vp.cy(comp.y + ty))
                        for tx, ty in (_rot_transform(x, y, rot) for x, y in outline)]))
                elif ci.is_full_circle:
                    p.drawEllipse(QPointF(vp.cx(scx),vp.cy(scy)),cr,cr)
                else:
                    sd=ci.rotate/64.0; sw=ci.angle/64.0
                    sd,sw=_arc_transform(sd,sw,rot)
                    # Qt drawArc: angles in 1/16 degree, CCW, start from East
                    # Our sd/sw: screen convention (CCW positive, 0=East)
                    rect=QRectF(vp.cx(scx)-cr,vp.cy(scy)-cr,cr*2,cr*2)
                    p.drawArc(rect,int(sd*16),int(sw*16))

            # pins (stubs, bubbles, anchors — names/numbers via _comp_attr_entries)
            ap_vis  = _marker_shown('anc_pin')
            for pin in sym.pins:
                conn_x,conn_y=_pin_conn_point(pin,sym)
                bdx,bdy=_rot_transform(pin.x,pin.y,rot)
                cdx,cdy=_rot_transform(conn_x,conn_y,rot)
                bx=comp.x+bdx; by=comp.y+bdy
                cx=comp.x+cdx; cy=comp.y+cdy
                p.setPen(QPen(stub_colour if stub_colour is not None else QColor(_COL_STUB),
                              max(1, round(vp.cs(_thin_thick_width_units(pin.pin_format == 9))))))
                p.setBrush(Qt.NoBrush)
                if pin.pin_format == 1:
                    _R = _NEGATION_BUBBLE_R
                    _sl = math.hypot(cx-bx, cy-by)
                    _ux,_uy = ((cx-bx)/_sl,(cy-by)/_sl) if _sl>0 else (1.0,0.0)
                    _bcx=bx+_R*_ux; _bcy=by+_R*_uy
                    p.drawLine(QPointF(vp.cx(_bcx+_R*_ux),vp.cy(_bcy+_R*_uy)),
                               QPointF(vp.cx(cx),vp.cy(cy)))
                    p.setBrush(bg_colour if bg_colour is not None else QColor(_BG_HEX))
                    p.drawEllipse(QPointF(vp.cx(_bcx),vp.cy(_bcy)),vp.cs(_R),vp.cs(_R))
                    p.setBrush(Qt.NoBrush)
                else:
                    p.drawLine(QPointF(vp.cx(bx),vp.cy(by)),
                               QPointF(vp.cx(cx),vp.cy(cy)))
                if ap_vis:
                    _pin_thick = pin.pin_format == 9
                    pending_markers.append(('diamond',vp.cx(cx),vp.cy(cy),_COL_ANC_PIN,
                                            _ANC_MARKER_BASE_THICK if _pin_thick else _ANC_MARKER_BASE_THIN,
                                            _pin_thick))

            # comp attrs (includes LABEL=pin names, #=pin numbers via sym_attrs)
            pp = _marker_shown('poe_comp')
            for tag,val,dx,dy,dpx,dpy,size,colour,vis_byte in _comp_attr_entries(
                    comp, sym,
                    show_pin_names=vis['pin_names'] or force_inspect,
                    show_pin_numbers=vis['pin_numbers'] or force_inspect,
                    is_power=is_pwr_symbol(sym),
                    show_refdes=vis['refdes'] or force_inspect,
                    show_value=vis['value'] or force_inspect,
                    show_hidden=(frozenset(g for g in _SYMBOL_TEXT_GROUPS
                                           if self._inspect_hidden.get(g))
                                 if force_inspect else frozenset())):
                odx,ody=_rot_transform(dx,dy,rot)
                wx=comp.x+odx; wy=comp.y+ody
                u  =ulticap_translate(rot,vis_byte)
                h,v,ang=_u_to_halign_valign_angle(u)
                ang=_text_display_angle(ang,rot)
                if tag in ('LABEL', 'WIRELABEL'):
                    anc_on, anc_col = _marker_shown('anc_pin_name'), _COL_ANC_PNAME
                elif tag.startswith('#'):
                    anc_on, anc_col = _marker_shown('anc_pin_number'), _COL_ANC_PNUM
                else:
                    anc_on, anc_col = _marker_shown('anc_attr'), _COL_ANC_ATTR
                if force_colours:
                    if tag in ('LABEL', 'WIRELABEL'):
                        col = pin_name_colour if pin_name_colour is not None else attr_colour
                    elif tag.startswith('#'):
                        col = pin_number_colour if pin_number_colour is not None else attr_colour
                    else:
                        col = attr_colour if attr_colour is not None else QColor(_COL_ATTR_FALLBACK)
                else:
                    col = _qcol_u(colour) if colour else (attr_colour if attr_colour is not None else QColor(_COL_ATTR_FALLBACK))
                self._draw_text(p,vp.cx(wx),vp.cy(wy),val,
                                size or 35,col,h,v,ang,
                                # a hidden text revealed, or a part not fitted
                                italic=not (vis_byte & 128) or not comp.fitted)
                if force_inspect and _text_is_detached(comp, sym, wx, wy):
                    # Dotted connector, in the text's own colour, to where the
                    # text belongs (a pin's body end, or the symbol's centre),
                    # ending in an open circle; heavier for the clicked text.
                    tx, ty = _text_owner_point(comp, sym, dpx, dpy)
                    clicked = self._inspected_text == (comp, tag, val, dx, dy)
                    pen = QPen(QColor(col), 2 if clicked else 1, Qt.DotLine)
                    p.setPen(pen); p.setBrush(Qt.NoBrush)
                    p.drawLine(QPointF(vp.cx(wx), vp.cy(wy)), QPointF(vp.cx(tx), vp.cy(ty)))
                    p.setPen(QPen(QColor(col), 2 if clicked else 1))
                    p.drawEllipse(QPointF(vp.cx(tx), vp.cy(ty)), 3.0, 3.0)
                if pp:
                    qx,qy=_rot_transform(dpx,dpy,rot)
                    pending_markers.append(('poe',vp.cx(comp.x+qx),vp.cy(comp.y+qy),None,4,False))
                if anc_on:
                    pending_markers.append(('diamond',vp.cx(wx),vp.cy(wy),anc_col,3,False))

        # ── drawing primitives ────────────────────────────────────────────────

        def _draw_diamond(self, p, cx, cy, col, s=4, is_thick=False):
            # s is the marker's base world-unit size (old fixed 3/4 px,
            # reinterpreted as world units); scaled via the same
            # base+threshold+gentle-excess formula junctions use (see
            # _anc_marker_radius_units), not an independent pixel clamp --
            # that shape is what keeps a marker visually distinguishable
            # from the line it may sit on, at any zoom level, without an
            # arbitrary ceiling that a thick/growing line can catch up to.
            # Floor is 0 by design -- these are a debug/inspection aid, not
            # something that needs to stay visible when zoomed out.
            s_px = max(_ANC_MARKER_MIN_PX, self._vp.cs(_anc_marker_radius_units(s, is_thick)))
            p.setBrush(QColor(col)); p.setPen(Qt.NoPen)
            poly = QPolygonF([QPointF(cx,cy-s_px),QPointF(cx+s_px,cy),
                              QPointF(cx,cy+s_px),QPointF(cx-s_px,cy)])
            p.drawPolygon(poly)

        def _draw_poe_sq(self, p, cx, cy, s=4, is_thick=False):
            # Same scaling approach as _draw_diamond -- see its comment.
            s_px = max(_ANC_MARKER_MIN_PX, self._vp.cs(_anc_marker_radius_units(s, is_thick)))
            p.setBrush(Qt.NoBrush); p.setPen(QPen(QColor(_COL_POE),1))
            p.drawRect(QRectF(cx-s_px,cy-s_px,s_px*2,s_px*2))

        def _text_font(self, size_u):
            """The font a text is laid out with, at the fixed size _TEXT_REF_PX,
            and the factor k that scales it to the text's true size at the
            current zoom: every glyph, advance and metric of the drawn text is
            the reference one times k.

            A font's own pixel size is whole pixels only. Sized directly, a
            3.4 px text came out at 3 px and a 4.5 px one at 4, so the widths
            that right- and centre-aligned text is placed by did not follow the
            zoom: at low zoom text shifted as the zoom changed, and a wheel-zoom
            preview (a scaled replay) put it visibly elsewhere than the exact
            render after it. Scaling one reference layout keeps size and width
            proportional to the zoom, fractions included. There is no minimum
            size either: a readable minimum made low-zoom text far larger than
            the drawing around it."""
            font = QFont(_TEXT_FONT_FAMILY)
            font.setPixelSize(_TEXT_REF_PX)
            k = self._vp.cs(size_u) * getattr(self, '_text_scale', _TEXT_SCALE_DEFAULT) / _TEXT_REF_PX
            return font, k

        def _draw_text(self, p, cx, cy, text, size_u, col, halign, valign, angle,
                       italic=False):
            """Draw text matching Ulticap's own placement model: a virtual
            bounding box is positioned relative to (cx, cy) according to
            halign/valign, but the actual glyphs are ALWAYS justified
            toward one fixed edge of that box regardless of which alignment
            was requested — left-justified horizontally, bottom/baseline-
            justified vertically. Confirmed empirically against real
            Ulticap files on both axes (ULTICAP_TEXT_MODEL.md §2).

            Box POSITION (where the box sits) comes from Ulticap's own
            measured MARGINS on both axes (_UC_LEFT/RIGHT_MARGIN_U
            horizontally, _UC_TOP/BOTTOM_MARGIN_U vertically) — that's what
            keeps the anchor placement matching real Ulticap. The box's
            CONTENT dimension, on both axes, is measured from the font's
            own actual rendered size (horizontalAdvance for width, capHeight
            for height) rather than any Ulticap-measured content number:
            at non-default _text_scale values a model-only content size
            diverges from what's actually drawn, badly enough that
            right/center-anchored text visibly drifted (see the Step 2
            hybrid-width fix). The vertical axis has an added reason to use
            the real font's capHeight — no Ulticap-measured, font-
            independent cap-height number exists to use instead. See
            ULTICAP_TEXT_MODEL.md §2/§5 and _uc_vertical_metrics.

            Vertical positioning bypasses Qt's own AlignBottom/VCenter/Top
            layout entirely — those don't know about Ulticap's margins —
            in favour of computing the baseline ourselves from the box
            model, then drawing with AlignTop against that computed
            baseline. This also makes the baseline exact for the overline
            line's position, rather than reverse-engineered via
            boundingRect().

            halign: 'left'|'center'|'right' — positions the box, not the text
            valign: 'bottom'|'center'|'top'
            (cx,cy) is the anchor point that the box is positioned against.

            Text containing ~{...} (overline) markup is drawn run by run.
            The overline itself is drawn as a manual line (not native
            QFont.setOverline()) positioned _uc_overline_gap(size_u) above
            the text's cap height — native overline's vertical position
            isn't exposed by Qt or CSS/SVG for tuning, so matching
            Ulticap's own measured gap (ULTICAP_TEXT_MODEL.md §3/§4)
            requires drawing it ourselves.
            """
            font, k = self._text_font(size_u)
            if italic:              # a hidden text, revealed
                font.setItalic(True)
            p.setPen(QPen(col))

            segs = _overline_segments(text)
            if not segs or not any(chunk for chunk, _ov in segs):
                return

            # Margins come from Ulticap's own measured world-unit convention,
            # converted to device pixels via the same viewport scale used
            # for any other world-space length (NOT via _text_scale, which
            # is a font-pixel-size calibration factor and doesn't apply to
            # this geometric margin). Content width, unlike Step 2, is NOT
            # taken from the model — it's measured below from the real font
            # so the anchor math matches what's actually drawn.
            left_u, _content_u, right_u, _box_w_u = _uc_box_metrics(text, size_u)
            top_u, bottom_u = _uc_vertical_metrics(size_u)
            left_px   = self._vp.cs(left_u)
            right_px  = self._vp.cs(right_u)
            top_px    = self._vp.cs(top_u)
            bottom_px = self._vp.cs(bottom_u)
            gap_px    = self._vp.cs(_uc_overline_gap(size_u))

            fm = QFontMetricsF(font)
            run_widths = [fm.horizontalAdvance(chunk) * k for chunk, _ov in segs if chunk]
            actual_content_px = sum(run_widths)
            box_w_px = left_px + actual_content_px + right_px

            cap_px = fm.capHeight() * k
            box_h_px = top_px + cap_px + bottom_px

            if angle:
                p.save()
                t = QTransform()
                t.translate(cx, cy)
                t.rotate(-angle)
                t.translate(-cx, -cy)
                p.setTransform(t, True)

            # Box positioned per halign — but content is always drawn
            # left-justified WITHIN the box (offset by the left margin),
            # matching Ulticap's own model.
            if   halign == 'left':   box_left = cx
            elif halign == 'right':  box_left = cx - box_w_px
            else:                    box_left = cx - box_w_px / 2

            # Box positioned per valign (Qt/device space is Y-DOWN, so the
            # box's Ulticap-Y-up "bottom" — the anchor for valign='bottom'
            # — is the LARGER pixel-y value, i.e. box_bottom_py = cy for
            # 'bottom', and 'top' pushes box_bottom_py further down by the
            # full box height). Content is always baseline-justified within
            # the box, offset up from the box's bottom edge by bottom_px —
            # matching Ulticap's own model on this axis too.
            if   valign == 'bottom': box_bottom_py = cy
            elif valign == 'top':    box_bottom_py = cy + box_h_px
            else:                    box_bottom_py = cy + box_h_px / 2
            baseline_py = box_bottom_py - bottom_px

            x_cursor = box_left + left_px
            for chunk, ov in segs:
                if not chunk:
                    continue
                w = fm.horizontalAdvance(chunk) * k
                # Drawn at the reference size under a scale of k, with its
                # baseline on the computed one (no layout rect: nothing clips).
                p.save()
                p.translate(x_cursor, baseline_py)
                p.scale(k, k)
                p.setFont(font)
                p.drawText(QPointF(0, 0), chunk)
                p.restore()
                if ov:
                    line_y = baseline_py - cap_px - gap_px
                    p.drawLine(QPointF(x_cursor, line_y), QPointF(x_cursor + w, line_y))
                x_cursor += w

            if angle:
                p.restore()

        def _text_hit_rect(self, cx, cy, text, size_u, halign, valign):
            """Screen-pixel (left, top, width, height) of the rendered text
            box at (cx,cy), in the UNROTATED frame -- i.e. exactly the
            box_left/box_bottom_py/box_w_px/box_h_px _draw_text computes
            before applying its rotation transform. Kept in sync with
            _draw_text's box-metrics math by construction (same formulas);
            used for click-to-inspect hit-testing against the actual
            visible glyphs instead of just a small radius around the raw
            anchor point -- necessary at high zoom, where rendered text can
            span far more screen space than a fixed-pixel anchor radius.
            Returns None for empty/whitespace-only text (nothing to hit)."""
            segs = _overline_segments(text)
            if not segs or not any(chunk for chunk, _ov in segs):
                return None
            font, k = self._text_font(size_u)
            fm = QFontMetricsF(font)
            left_u, _content_u, right_u, _box_w_u = _uc_box_metrics(text, size_u)
            top_u, bottom_u = _uc_vertical_metrics(size_u)
            left_px   = self._vp.cs(left_u)
            right_px  = self._vp.cs(right_u)
            top_px    = self._vp.cs(top_u)
            bottom_px = self._vp.cs(bottom_u)
            run_widths = [fm.horizontalAdvance(chunk) * k for chunk, _ov in segs if chunk]
            box_w_px = left_px + sum(run_widths) + right_px
            box_h_px = top_px + fm.capHeight() * k + bottom_px
            if   halign == 'left':   box_left = cx
            elif halign == 'right':  box_left = cx - box_w_px
            else:                    box_left = cx - box_w_px / 2
            if   valign == 'bottom': box_bottom_py = cy
            elif valign == 'top':    box_bottom_py = cy + box_h_px
            else:                    box_bottom_py = cy + box_h_px / 2
            return box_left, box_bottom_py - box_h_px, box_w_px, box_h_px

        def _point_hits_text(self, cx, cy, text, size_u, halign, valign, angle,
                             sx, sy, pad_px=3):
            """True if screen point (sx,sy) falls inside the rendered text
            box at (cx,cy) -- see _text_hit_rect. angle (0, 90, 180 or 270, matching
            _draw_text's own domain) is undone via the exact inverse of the
            QTransform _draw_text applies, so the rotated-text case is
            handled the same way the rotated DRAW is, rather than
            re-deriving the rotation by hand."""
            box = self._text_hit_rect(cx, cy, text, size_u, halign, valign)
            if box is None:
                return False
            box_left, box_top, box_w, box_h = box
            if angle:
                t = QTransform()
                t.translate(cx, cy); t.rotate(-angle); t.translate(-cx, -cy)
                inv, ok = t.inverted()
                pt = inv.map(QPointF(sx, sy)) if ok else QPointF(sx, sy)
            else:
                pt = QPointF(sx, sy)
            return (box_left - pad_px <= pt.x() <= box_left + box_w + pad_px and
                    box_top  - pad_px <= pt.y() <= box_top  + box_h + pad_px)

        def _text_may_be_visible(self, x, y, text, size_u, vx0, vy0, vx1, vy1):
            """Conservative (over-inclusive) check for whether text
            anchored at world point (x,y) could have any part on-screen --
            used to decide whether a *X/*A label is worth drawing at all.
            Testing only the exact anchor point (the old behaviour) wrongly
            hides text whenever the anchor itself sits just off-screen but
            the glyphs -- positioned per halign/valign box margins, which
            can put the anchor at a corner of the box rather than under the
            visible characters -- are still partly on-screen; that's what
            made *X/*A text invisible until clicked (the click path's
            full-opacity overlay redraw has no in-view check at all, so it
            always "worked", making the normal render's culling look like
            a bug that only cleared on click).

            Uses the box-model's own char-advance math (kiuc_model's
            _uc_box_metrics/_uc_vertical_metrics) rather than real font
            metrics, so it's cheap enough to call for every label without
            constructing a QFont -- the actual _draw_text call still does
            that per-glyph work, this is just the cull. Padding is
            generous by design: a wrongly-kept label costs one harmless
            extra draw call; a wrongly-culled one hides text the user can
            see should be there."""
            _l, _content_w, _r, box_w = _uc_box_metrics(text, size_u)
            top_u, bottom_u = _uc_vertical_metrics(size_u)
            box_h = top_u + bottom_u + size_u   # size_u stands in for cap height here
            pad = max(box_w, box_h, size_u)
            return not (x+pad < vx0 or x-pad > vx1 or y+pad < vy0 or y-pad > vy1)

        def _paint_snap_cursor(self, p):
            vp = self._vp
            cx = vp.cx(self._snap_wx);  cy = vp.cy(self._snap_wy)
            p.setPen(QPen(QColor(_COL_SNAP),1))
            if self._full_crosshair:
                p.drawLine(QPointF(0,cy),QPointF(self.width(),cy))
                p.drawLine(QPointF(cx,0),QPointF(cx,self.height()))
            else:
                s = _SNAP_CROSSHAIR_ARM_PX
                p.drawLine(QPointF(cx-s*2,cy),QPointF(cx+s*2,cy))
                p.drawLine(QPointF(cx,cy-s*2),QPointF(cx,cy+s*2))
            if self._ref_set:
                rx = vp.cx(self._ref_wx);  ry = vp.cy(self._ref_wy)
                p.setPen(QPen(QColor('#ffd700'),1))
                s2 = _REF_CROSSHAIR_ARM_PX
                p.drawLine(QPointF(rx-s2,ry),QPointF(rx+s2,ry))
                p.drawLine(QPointF(rx,ry-s2),QPointF(rx,ry+s2))

    # ── PNG export layer selection dialog ────────────────────────────────────

    class _ExportLayerDialog(QDialog):
        """Modal dialog for selecting which layers to include in PNG export.
        Pre-populated from load_export_png_layers(); saves on OK."""

        def __init__(self, parent, live_colours: dict = None, fmt: str = 'PNG'):
            super().__init__(parent)
            self.setWindowTitle(f'{fmt} Export — Layer Selection')
            self.setModal(True)
            self._vis = load_export_png_layers()   # {key: bool}
            self._rows = {}   # key → (swatch, lbl) for dim update
            _lc = live_colours or {}   # dominant colours from last file load

            lay = QVBoxLayout(self)
            lay.addWidget(QLabel(f'<b>Layers to include in the {fmt} export:</b><br>'
                                 f'(one choice for the PNG and PDF exports)'))

            prev_grp = None
            for key, label, _default, col in _LAYERS:
                grp = 'anc' if (key.startswith('anc_') or key.startswith('poe_')) else 'sch'
                if grp != prev_grp and prev_grp is not None:
                    sep = QFrame(); sep.setFrameShape(QFrame.HLine)
                    sep.setStyleSheet('color:#444;'); lay.addWidget(sep)
                prev_grp = grp

                cb = QCheckBox(); cb.setVisible(False)
                cb.setChecked(self._vis.get(key, True))

                is_pin = key.startswith('pin_') or key in ('refdes', 'value')
                left_margin = 18 if is_pin else 2
                row = QWidget(); row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(left_margin,1,2,1); row_lay.setSpacing(6)
                row.setCursor(Qt.PointingHandCursor)
                row.setStyleSheet('QWidget:hover { background: #e8e8e8; }')

                if grp == 'anc':
                    glyph, rest = label[0], label[1:]
                    gl = QLabel(f'<span style="color:{col};font-size:13px">{glyph}</span>'
                                f'<span style="color:#222222">{rest}</span>')
                    gl.setTextFormat(Qt.RichText)
                    row_lay.addWidget(gl); row_lay.addStretch()
                    self._rows[key] = (None, gl)
                    def _glyph_dim(st, g=glyph, r=rest, c=col, gl=gl):
                        op = c if st else '#bbbbbb'
                        tc = '#222222' if st else '#aaaaaa'
                        gl.setText(f'<span style="color:{op};font-size:13px">{g}</span>'
                                   f'<span style="color:{tc}">{r}</span>')
                    cb.stateChanged.connect(lambda st, fn=_glyph_dim: fn(bool(st)))
                    _glyph_dim(cb.isChecked())
                else:
                    sw = QLabel(); sw.setFixedSize(10, 10)
                    lb = QLabel(label)
                    _dc = [_lc.get(key, col)]   # live dominant colour, falls back to _LAYERS
                    def _dim(st, sw=sw, lb=lb, cc=_dc):
                        sw.setStyleSheet(f'background:{cc[0]};border:1px solid #999;'
                                         if st else 'background:#cccccc;border:1px solid #bbb;')
                        lb.setStyleSheet('color:#222222;' if st else 'color:#aaaaaa;')
                    cb.stateChanged.connect(lambda st, fn=_dim: fn(bool(st)))
                    _dim(cb.isChecked())
                    row_lay.addWidget(sw); row_lay.addWidget(lb); row_lay.addStretch()
                    self._rows[key] = (sw, lb)

                row.mousePressEvent = lambda _e, c=cb: c.setChecked(not c.isChecked())
                cb.stateChanged.connect(lambda st, k=key: self._vis.update({k: bool(st)}))
                lay.addWidget(row)

            # ── buttons ───────────────────────────────────────────────────────
            sep = QFrame(); sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet('color:#444;'); lay.addWidget(sep)
            btn_row = QHBoxLayout()
            btn_ok  = QPushButton('OK');     btn_ok.setDefault(True)
            btn_can = QPushButton('Cancel')
            btn_ok.clicked.connect(self._on_ok)
            btn_can.clicked.connect(self.reject)
            btn_row.addStretch(); btn_row.addWidget(btn_ok); btn_row.addWidget(btn_can)
            lay.addLayout(btn_row)

        def _on_ok(self):
            save_export_png_layers(self._vis)
            self.accept()

    # ── main window ───────────────────────────────────────────────────────────

    class MainWindow(QMainWindow):

        def __init__(self, sheet, initial_dir: Path, initial_path: Optional[Path] = None,
                     initial_key: Optional[str] = None):
            super().__init__()
            self.setWindowTitle('KIUT Viewer')
            self.resize(1200, 800)

            self._initial_dir   = initial_dir
            self._current_path  = None        # Path of the currently displayed file
            self._current_key   = None        # its sheet's key in a .UTSCH, else None
            self._root_path     = None        # Path the Sheets tree is rooted at
            self._handoff       = None        # its own channel (kiut/core/handoff.py)
            self.viewer_id      = None        # its id: KIUT's Open in Viewer finds it by it
            self.handoff_name   = None
            # stack of previously-shown (path, key) sheets, for the 'B' back
            # shortcut (see _go_back / _load_and_show)
            self._sheet_history: List[tuple] = []
            self._last_pane_width = 220
            self._layer_colours = {}          # cached from last _update_layer_colours call

            self._build_menu()
            self._build_toolbar()

            # ── splitter: single resizable/hideable pane | canvas ──────────────
            self._splitter = QSplitter(Qt.Horizontal)
            self.setCentralWidget(self._splitter)

            pane = QWidget(); pane_layout = QVBoxLayout(pane)
            pane.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            pane_layout.setContentsMargins(6,6,6,6); pane_layout.setSpacing(2)
            pane.setMinimumWidth(150)
            pane.setStyleSheet('background:#252526; color:#cccccc;')
            self._pane = pane

            # ── Sheets (hierarchy) — hidden until a file with sub-sheet
            # references is loaded, so flat designs don't show an empty panel.
            # ── Layers ───────────────────────────────────────────────────────
            _hdr = QLabel('<b>Layers</b>')
            _hint = QLabel('<span style="font-size:9px">(click to toggle)</span>')
            _hint.setTextFormat(Qt.RichText)
            _hdr_row = QWidget(); _hdr_lay = QHBoxLayout(_hdr_row)
            _hdr_lay.setContentsMargins(0,0,0,0); _hdr_lay.setSpacing(4)
            _hdr_lay.addWidget(_hdr); _hdr_lay.addWidget(_hint); _hdr_lay.addStretch()
            pane_layout.addWidget(_hdr_row)
            self._checks = {}
            self._marker_mute_snapshot = None   # None = not muted; else {key: was_checked}
            prev_group = None
            self._swatches      = {}   # key → QLabel swatch (schematic rows)
            self._glyph_labels  = {}   # key → QLabel rich-text (anc/poe rows)
            self._glyph_chars   = {}   # key → (glyph_char, rest_of_label)
            self._pin_rows      = {}   # key → row widget (pin_names / pin_numbers / refdes / value)
            self._swatch_cells  = {}   # key → [colour] mutable cell (all other schematic rows;
                                        # lets _apply_dim always read the current theme's colour
                                        # instead of the stale _LAYERS default captured at build
                                        # time -- same fix already applied to _pin_rows above)
            self._inspect_toggle_labels = {}   # key → secondary "Selected" column icon
            self._marker_rows   = {}   # key → row widget (anc_*/poe_*, for mute-lock)
            self._row_dim       = {}   # key → that row's dim/undim function (schematic rows)
            self._hidden_toggle_refresh = []   # redraw functions of the "hidden" toggles
            for key, label, default, col in _LAYERS:
                grp = 'anc' if key.startswith('anc') or key.startswith('poe') else 'sch'
                if grp != prev_group and prev_group is not None:
                    sep = QFrame(); sep.setFrameShape(QFrame.HLine)
                    sep.setStyleSheet('color:#444;'); pane_layout.addWidget(sep)
                    # Marker-section header: only the enabled markers get
                    # hidden/restored, not a blanket force-on -- see
                    # _on_toggle_all_markers. The "Selected" column title
                    # only appears while a component is being inspected
                    # (see _on_inspection_changed) -- it labels the small
                    # per-marker-type toggles that appear on the relevant
                    # rows for isolating one marker type at a time on that
                    # one component.
                    _mk_hdr_row = QWidget(); _mk_hdr_lay = QHBoxLayout(_mk_hdr_row)
                    _mk_hdr_lay.setContentsMargins(2,2,2,2); _mk_hdr_lay.setSpacing(4)
                    _mk_hdr_lay.addWidget(QLabel('<b>Markers</b>'))
                    self._marker_mute_btn = QPushButton('Hide all')
                    self._marker_mute_btn.setFixedHeight(20)
                    self._marker_mute_btn.setStyleSheet(
                        'font-size:10px; padding:1px 6px;')
                    self._marker_mute_btn.clicked.connect(self._on_toggle_all_markers)
                    _mk_hdr_lay.addWidget(self._marker_mute_btn)
                    _mk_hdr_lay.addStretch()
                    self._inspect_col_title = QLabel('<b>Selected</b>')
                    self._inspect_col_title.setStyleSheet('font-size:10px; color:#9cdcfe;')
                    self._inspect_col_title.setVisible(False)
                    _mk_hdr_lay.addWidget(self._inspect_col_title)
                    pane_layout.addWidget(_mk_hdr_row)
                prev_group = grp
                is_glyph = grp == 'anc'
                is_pin   = key.startswith('pin_') or key in ('refdes', 'value')
                is_hidden_row = key in _HIDDEN_PARENT_OF
                # Invisible QCheckBox tracks state; row click dims/brightens
                # the swatch and label — no checkbox box rendered at all.
                cb = QCheckBox(); cb.setVisible(False)
                cb.setChecked(default)
                cb.stateChanged.connect(self._on_layer_change)
                self._checks[key] = cb

                # Pin rows indented under Symbols; a "Hidden ..." row one
                # step further, under its parent row
                left_margin = (18 if is_pin else 2) + (16 if is_hidden_row else 0)
                row = QWidget(); row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(left_margin,1,2,1); row_lay.setSpacing(6)
                row.setCursor(Qt.PointingHandCursor)
                row.setStyleSheet('QWidget:hover { background: #2a2a2a; }')

                if is_glyph:
                    # Colour the leading glyph character; rest of label is normal
                    glyph, rest = label[0], label[1:]
                    self._glyph_chars[key] = (glyph, rest)
                    glyph_lbl = QLabel(f'<span style="color:{col};font-size:13px">'
                                       f'{glyph}</span>'
                                       f'<span style="color:#cccccc">{rest}</span>')
                    glyph_lbl.setTextFormat(Qt.RichText)
                    row_lay.addWidget(glyph_lbl); row_lay.addStretch()
                    if key in _COMP_INSPECT_KEYS:
                        # Secondary per-component marker-type toggle -- only
                        # shown while something is inspected (see
                        # _on_inspection_changed); same glyph/colour as the
                        # main row, so it reads as "the same marker, but
                        # scoped to the selected component". Independent
                        # on/off, like the main list, and deliberately NOT
                        # reset per component (see _inspect_marker_vis).
                        insp_lbl = QLabel(f'<span style="color:{col};font-size:13px">{glyph}</span>')
                        insp_lbl.setTextFormat(Qt.RichText)
                        insp_lbl.setCursor(Qt.PointingHandCursor)
                        insp_lbl.setVisible(False)
                        def _apply_insp_dim(shown, lb=insp_lbl, g=glyph, c=col):
                            lb.setText(f'<span style="color:{c if shown else "#5a5a5a"};'
                                      f'font-size:13px">{g}</span>')
                        def _on_insp_click(_e, k=key, fn=_apply_insp_dim):
                            cur = self._canvas._inspect_marker_vis.get(k, True)
                            self._canvas._inspect_marker_vis[k] = not cur
                            fn(not cur)
                            self._canvas.update()
                        insp_lbl.mousePressEvent = _on_insp_click
                        _apply_insp_dim(True)
                        self._inspect_toggle_labels[key] = insp_lbl
                        row_lay.addWidget(insp_lbl)
                    pane_layout.addWidget(row)
                    self._glyph_labels[key] = glyph_lbl
                    # dim glyph row when disabled
                    def _apply_glyph_dim(checked, gl=glyph_lbl, g=glyph, r=rest, c=col):
                        opacity = c if checked else '#5a5a5a'
                        txt_col = '#cccccc' if checked else '#7a7a7a'
                        gl.setText(f'<span style="color:{opacity};font-size:13px">{g}</span>'
                                   f'<span style="color:{txt_col}">{r}</span>')
                    cb.stateChanged.connect(lambda st, fn=_apply_glyph_dim: fn(bool(st)))
                    _apply_glyph_dim(default)
                else:
                    # [■ swatch] [label text] — click row to dim/undim
                    swatch = QLabel()
                    swatch.setFixedSize(10, 10)
                    lbl = QLabel(label)
                    if key == 'grid':
                        self._grid_label = lbl   # updated dynamically by grid_cb on load
                    if is_pin:
                        # Pin rows: colour updated by _update_layer_colours after load.
                        # Use a mutable container so _apply_dim always uses the current
                        # dominant colour rather than the stale _LAYERS default.
                        # Start grey — real colour arrives on first file load.
                        _cur_col = ['#555555']
                        def _apply_dim(checked, sw=swatch, lb=lbl, cc=_cur_col):
                            sw.setStyleSheet(
                                f'background:{cc[0]};border:1px solid #555;'
                                if checked else
                                'background:#333;border:1px solid #444;')
                            lb.setStyleSheet(
                                'color:#cccccc;' if checked else 'color:#7a7a7a;')
                        self._pin_rows[key] = (row, cb, swatch, lbl, _apply_dim, _cur_col)
                    else:
                        # All other schematic rows: _update_layer_colours updates
                        # both the swatch style AND this mutable cell, so toggling
                        # the row's checkbox off/on afterwards re-applies the
                        # CURRENT (theme-appropriate) colour rather than reverting
                        # to the _LAYERS default captured here at build time.
                        _cur_col = [col]
                        def _apply_dim(checked, sw=swatch, lb=lbl, cc=_cur_col):
                            sw.setStyleSheet(
                                f'background:{cc[0]};border:1px solid #555;'
                                if checked else
                                'background:#333;border:1px solid #444;')
                            lb.setStyleSheet(
                                'color:#cccccc;' if checked else 'color:#7a7a7a;')
                        self._swatch_cells[key] = _cur_col
                    self._row_dim[key] = _apply_dim
                    if not is_hidden_row:
                        # a "Hidden ..." row's look also depends on its
                        # parent row: set in _on_layer_change
                        cb.stateChanged.connect(lambda st, fn=_apply_dim: fn(bool(st)))
                    _apply_dim(default)
                    row_lay.addWidget(swatch)
                    row_lay.addWidget(lbl)
                    row_lay.addStretch()
                    if key in _SYMBOL_TEXT_GROUPS or key == 'symbols':
                        row_lay.addWidget(self._make_hidden_toggle(key))
                    pane_layout.addWidget(row)
                    self._swatches[key] = swatch

                if is_pin:
                    row.mousePressEvent = lambda _e, c=cb: (
                        c.setChecked(not c.isChecked())
                        if self._checks['symbols'].isChecked() else None)
                elif is_glyph:
                    # Locked while "Hide all"/mute is active -- toggling an
                    # individual marker mid-mute would be silently
                    # overwritten by the eventual "Show all" restore (it
                    # replays the snapshot taken when Hide all was
                    # pressed), which reads as erratic/broken rather than
                    # as the deliberate override it would otherwise be.
                    row.mousePressEvent = lambda _e, c=cb: (
                        c.setChecked(not c.isChecked())
                        if self._marker_mute_snapshot is None else None)
                    self._marker_rows[key] = row
                else:
                    row.mousePressEvent = lambda _e, c=cb: c.setChecked(not c.isChecked())

            sep3 = QFrame(); sep3.setFrameShape(QFrame.HLine)
            sep3.setStyleSheet('color:#444;'); pane_layout.addWidget(sep3)

            # ── Sheets (hierarchy) — at the bottom so appearing/disappearing
            # never pushes the Layers section (actively used) upward.
            # Hidden until a hierarchical file is loaded.
            self._sheets_sep = QFrame(); self._sheets_sep.setFrameShape(QFrame.HLine)
            self._sheets_sep.setStyleSheet('color:#444;')
            self._sheets_sep.setVisible(False)
            pane_layout.addWidget(self._sheets_sep)
            self._sheets_group = QWidget(); sg_layout = QVBoxLayout(self._sheets_group)
            sg_layout.setContentsMargins(0,0,0,0); sg_layout.setSpacing(2)
            sg_layout.addWidget(QLabel('<b>Sheets</b>'))
            self._sheets_tree = QTreeWidget()
            self._sheets_tree.setHeaderHidden(True)
            self._sheets_tree.setFixedHeight(160)
            self._sheets_tree.setStyleSheet(
                'background:#1e1e1e; color:#cccccc; border:1px solid #444;')
            self._sheets_tree.itemClicked.connect(self._on_sheets_click)
            sg_layout.addWidget(self._sheets_tree)
            self._sheets_group.setVisible(False)
            pane_layout.addWidget(self._sheets_group)

            # ── Components (all real parts) + Signals (SIGNAL= tags) —
            # tabbed together since both are "find something, click to
            # zoom" lists. Unlike Sheets (only meaningful for hierarchical
            # designs, so it stays a separate always-or-never block), these
            # two are always relevant once any sheet is loaded: Components
            # always has content, so it comes first; Signals staying
            # visible even when empty is intentional — it's still useful
            # to see "this sheet has none" at a glance.
            self._lists_tabs = QTabWidget()
            self._lists_tabs.setStyleSheet(
                'QTabWidget::pane { border: 1px solid #444; }'
                'QTabBar::tab { background:#2d2d2d; color:#cccccc; padding:3px 8px; }'
                'QTabBar::tab:selected { background:#1e1e1e; color:#ffffff; }')
            self._lists_tabs.setVisible(False)

            _refdes_tab = QWidget(); rd_layout = QVBoxLayout(_refdes_tab)
            rd_layout.setContentsMargins(4,4,4,4); rd_layout.setSpacing(2)
            _rd_hint = QLabel('<span style="font-size:9px">(click to locate)</span>')
            _rd_hint.setTextFormat(Qt.RichText)
            rd_layout.addWidget(_rd_hint)
            self._refdes_tree = QTreeWidget()
            self._refdes_tree.setHeaderHidden(True)
            self._refdes_tree.setFixedHeight(160)
            self._refdes_tree.setStyleSheet(
                'background:#1e1e1e; color:#cccccc; border:1px solid #444;')
            self._refdes_tree.itemClicked.connect(self._on_refdes_click)
            rd_layout.addWidget(self._refdes_tree)
            self._lists_tabs.addTab(_refdes_tab, 'Components')

            _signals_tab = QWidget(); sig_layout = QVBoxLayout(_signals_tab)
            sig_layout.setContentsMargins(4,4,4,4); sig_layout.setSpacing(2)
            _sig_hint = QLabel('<span style="font-size:9px">(click to locate)</span>')
            _sig_hint.setTextFormat(Qt.RichText)
            sig_layout.addWidget(_sig_hint)
            self._signals_tree = QTreeWidget()
            self._signals_tree.setHeaderHidden(True)
            self._signals_tree.setFixedHeight(160)
            self._signals_tree.setStyleSheet(
                'background:#1e1e1e; color:#cccccc; border:1px solid #444;')
            self._signals_tree.itemClicked.connect(self._on_signals_click)
            sig_layout.addWidget(self._signals_tree)
            self._lists_tabs.addTab(_signals_tab, 'Signals')

            pane_layout.addWidget(self._lists_tabs)

            # Wrap the pane in a real scroll area (QScrollArea was already
            # imported but never instantiated) so content that no longer
            # fits a small/resized window scrolls instead of being clipped
            # with no way to reach it — Layers, Sheets, and now Signals can
            # all be present simultaneously without fighting each other for
            # a fixed vertical budget.
            pane_scroll = QScrollArea()
            pane_scroll.setWidget(pane)
            pane_scroll.setWidgetResizable(True)
            pane_scroll.setFrameShape(QFrame.NoFrame)
            pane_scroll.setStyleSheet('background:#252526;')
            pane_scroll.setMinimumWidth(150)
            self._splitter.addWidget(pane_scroll)

            # canvas
            self._canvas = SchCanvas()
            self._info_text           = '(none loaded)'
            self._canvas.info_cb      = lambda s: setattr(self, '_info_text', s)
            self._canvas.inspect_cb   = self._on_inspection_changed
            self._canvas.colours_cb   = self._update_layer_colours
            self._canvas.crosshair_cb = lambda v: self._act_crosshair.setChecked(v)
            self._canvas.snap_step_cb = lambda d: (self._snap_spin.stepUp() if d > 0
                                                    else self._snap_spin.stepDown())
            self._canvas.grid_cb = lambda g: self._grid_label.setText(f'Grid ({g} u)')
            self._canvas.enter_subsheet_cb = self._on_enter_subsheet
            self._canvas.back_cb           = self._go_back
            self._splitter.addWidget(self._canvas)
            self._splitter.setStretchFactor(0, 0)
            self._splitter.setStretchFactor(1, 1)
            self._splitter.setSizes([self._last_pane_width, 1000])

            # Status bar — use a QLabel widget rather than showMessage() so
            # the text survives QMenuBar hover events.  Qt internally calls
            # showMessage(action.statusTip()) on menu hover; since our actions
            # have no statusTip set, that call becomes showMessage('') which
            # temporarily overlays the status area and makes our text vanish.
            # Connecting messageChanged to restore the last known string
            # eliminates the flicker without needing to touch every QAction.
            sb = self.statusBar()
            sb.setStyleSheet(
                'background:#141428;color:#9cdcfe;font-family:Consolas;font-size:11px;')
            self._status_label = QLabel('')
            self._status_label.setStyleSheet(
                'color:#9cdcfe;font-family:Consolas;font-size:11px;padding-left:4px;')
            sb.addWidget(self._status_label, 1)   # stretch=1: fill the whole bar
            self._last_status_text = ''

            def _set_status(text: str):
                self._last_status_text = text
                self._status_label.setText(text)

            def _on_message_changed(msg: str):
                # Qt clears our label when menu hover fires showMessage('').
                # Restore the last coordinate / hint string immediately.
                if not msg:
                    self._status_label.setText(self._last_status_text)

            sb.messageChanged.connect(_on_message_changed)
            self._canvas.status_cb = _set_status

            # Second, independent status-bar section for whatever's
            # currently click-inspected (height/coordinates for a *X/*A
            # label or annotation, origin coordinates, or a component's
            # placement origin) -- replaces the old on-canvas floating
            # readout, which could cover the very item it was describing.
            # addPermanentWidget is a genuinely separate area from the one
            # above (addWidget) -- Qt's own idiom for a persistent
            # indicator that shouldn't be affected by temporary messages --
            # so this needs none of the messageChanged workaround the
            # coordinate label above needed.
            self._selection_label = QLabel('')
            self._selection_label.setStyleSheet(
                'color:#dcdcaa;font-family:Consolas;font-size:11px;padding-right:6px;')
            # A third line above those two, for what a clicked symbol text
            # is (pin name, a text that names no net, ...): bold, in a colour
            # that draws attention. The section is always three lines tall,
            # so the bar does not grow or shrink as the line fills or empties.
            self._selection_info = QLabel('')
            self._selection_info.setStyleSheet(
                'color:#ffb454;font-family:Consolas;font-size:11px;font-weight:bold;'
                'padding-right:6px;')
            _sel_font = QFont('Consolas'); _sel_font.setPixelSize(11)
            _ls = math.ceil(QFontMetricsF(_sel_font).lineSpacing())
            self._selection_info.setFixedHeight(_ls + 2)
            self._selection_label.setFixedHeight(2 * _ls + 2)
            _sel_box = QWidget()
            _sel_lay = QVBoxLayout(_sel_box)
            _sel_lay.setContentsMargins(0, 0, 0, 0); _sel_lay.setSpacing(0)
            _sel_lay.addWidget(self._selection_info)
            _sel_lay.addWidget(self._selection_label)
            sb.addPermanentWidget(_sel_box)
            # what reading the files said, counted (View -> Reading messages... lists them)
            self._read_label = QLabel('')
            self._read_label.setStyleSheet(
                'color:#ffb454;font-family:Consolas;font-size:11px;padding-right:6px;')
            sb.addPermanentWidget(self._read_label)

            if sheet:
                self._current_path = initial_path
                self._current_key  = initial_key
                self._root_path    = initial_path
                self._canvas.load(sheet)
                self.setWindowTitle(f'KIUT Viewer — {sheet.name}')
                self._populate_sheets()
                self._populate_signals()
                self._populate_refdes()
                self._lists_tabs.setVisible(True)
            else:
                self._canvas._show_hint()

            # Load persisted layer visibility from ini
            saved_vis = load_layer_visibility()
            if saved_vis:
                for key, cb in self._checks.items():
                    if key in saved_vis:
                        cb.setChecked(saved_vis[key])
                self._on_layer_change(None)   # sync canvas._vis

            # Load the viewer's own persisted thin/thick line widths
            # (kiut_viewer.ini [viewer_line_widths]; independent of the converter)
            _load_line_width_tuning()
            self._thin_spin.blockSignals(True)
            self._thin_spin.setValue(int(THIN_LINE_WIDTH))
            self._thin_spin.blockSignals(False)
            self._thick_spin.blockSignals(True)
            self._thick_spin.setValue(int(THICK_LINE_WIDTH))
            self._thick_spin.blockSignals(False)

            # Load persisted cursor-snap value from ini
            saved_snap_mm = load_snap_mm()
            self._canvas._snap_mm = saved_snap_mm
            self._snap_spin.blockSignals(True)
            self._snap_spin.setValue(saved_snap_mm)
            self._snap_spin.blockSignals(False)

            # Recent files (used by _build_menu which ran already — rebuild now)
            self._recent = load_recent_files()
            self._rebuild_recent_menu()

        # ── menu bar ─────────────────────────────────────────────────────────
        def _build_menu(self):
            mb = self.menuBar()

            m_file = mb.addMenu('&File')
            act_open = QAction('&Open…', self); act_open.setShortcut('Ctrl+O')
            act_open.triggered.connect(self._open_dialog)
            m_file.addAction(act_open)

            self._recent_menu = m_file.addMenu('Open &Recent')
            # Populated by _rebuild_recent_menu() once self._recent is set.

            m_file.addSeparator()
            act_svg = QAction('Export as &SVG…', self)
            act_svg.triggered.connect(self._save_svg)
            m_file.addAction(act_svg)
            act_png = QAction('Export as &PNG…', self)
            act_png.triggered.connect(self._save_png)
            m_file.addAction(act_png)
            act_pdf = QAction('Export as P&DF…', self)
            act_pdf.triggered.connect(self._save_pdf)
            m_file.addAction(act_pdf)

            m_file.addSeparator()
            act_exit = QAction('E&xit', self); act_exit.triggered.connect(self.close)
            m_file.addAction(act_exit)

            m_view = mb.addMenu('&View')
            self._act_pane = QAction('Side pane', self, checkable=True, checked=True)
            self._act_pane.toggled.connect(self._toggle_pane)
            m_view.addAction(self._act_pane)
            act_info = QAction('Schematic info…', self)
            act_info.triggered.connect(self._show_sheet_info)
            m_view.addAction(act_info)
            self._act_read = QAction('Reading messages…', self)
            self._act_read.triggered.connect(self._show_read_messages)
            m_view.addAction(self._act_read)
            m_view.addSeparator()
            self._tooltip_filter = _TooltipFilter(self)
            self._tooltip_filter.enabled = load_show_tooltips()
            QApplication.instance().installEventFilter(self._tooltip_filter)
            self._act_tooltips = QAction('Show tooltips', self, checkable=True,
                                         checked=self._tooltip_filter.enabled)
            self._act_tooltips.toggled.connect(self._on_tooltips_toggled)
            m_view.addAction(self._act_tooltips)

            m_help = mb.addMenu('&Help')
            act_doc = QAction('KIUT Viewer &documentation', self)
            act_doc.triggered.connect(self._show_documentation)
            m_help.addAction(act_doc)
            act_help = QAction('&Shortcuts && Layers…', self)
            act_help.triggered.connect(self._show_help)
            m_help.addAction(act_help)
            act_about = QAction('&About', self)
            act_about.triggered.connect(self._show_about)
            m_help.addAction(act_about)

        def _on_tooltips_toggled(self, on: bool):
            self._tooltip_filter.enabled = on
            save_show_tooltips(on)

        def _rebuild_recent_menu(self):
            self._recent_menu.clear()
            if self._recent:
                for p in self._recent:
                    act = QAction(str(p), self)
                    act.setEnabled(p.exists())
                    act.triggered.connect(lambda _checked, path=p: self._open_recent(path))
                    self._recent_menu.addAction(act)
                self._recent_menu.addSeparator()
            act_clear = QAction('Empty Recent Files List', self)
            act_clear.setEnabled(bool(self._recent))
            act_clear.triggered.connect(self._clear_recent)
            self._recent_menu.addAction(act_clear)

        def _open_recent(self, path: Path):
            self._root_path = path
            self._load_and_show(path, fresh=True)

        def _clear_recent(self):
            self._recent = []
            save_recent_files([])
            self._rebuild_recent_menu()

        def start_handoff(self, viewer_id: str, name: Optional[str] = None) -> bool:
            """Listening on its own channel, for the KIUT window's Open in Viewer
            once it is KIUT's own viewer (KIUT started it, or it sent KIUT a
            design)."""
            from kiut.core import handoff
            self.viewer_id, self.handoff_name = viewer_id, name
            self._handoff_bridge = _HandoffBridge(self.receive)
            listener = handoff.Listener(handoff.viewer_channel(viewer_id), self._handoff_bridge.handle,
                                        name=name)
            if listener.start():
                self._handoff = listener
                return True
            return False

        def stop_handoff(self):
            if self._handoff is not None:
                self._handoff.stop()
                self._handoff = None

        def receive(self, path: Optional[Path]) -> str:
            """A file the KIUT window offers: only brought to the front when it is
            the design shown (its zoom, sheet and selection kept), else opened as
            File > Open does."""
            from kiut.core import handoff
            if path is None:
                self.to_front()
                return handoff.SHOWN
            path = Path(path)
            shown = handoff.same_file(path, self._root_path) or handoff.same_file(path, self._current_path)
            if not shown:
                self._root_path = path
                self._load_and_show(path, fresh=True)
            self.to_front()
            return handoff.SHOWN if shown else handoff.OPENED

        def to_front(self):
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()

        def closeEvent(self, e):
            self.stop_handoff()
            # Persist layer visibility and recent files on close
            save_layer_visibility(
                {k: cb.isChecked() for k, cb in self._checks.items()})
            super().closeEvent(e)

        # ── toolbar ──────────────────────────────────────────────────────────
        def _build_toolbar(self):
            tb = QToolBar('Main', self)
            tb.setMovable(False)
            tb.setIconSize(QSize(20, 20))
            tb.setStyleSheet(
                'QToolBar{background:#2d2d30;border:none;spacing:4px;padding:2px;}'
                'QToolButton{padding:3px;border-radius:3px;}'
                'QToolButton:hover{background:#3c3c3c;}'
                'QToolButton:checked{background:#0e639c;}')
            self.addToolBar(tb)

            act_open = QAction(_qicon('open'), '', self)
            act_open.setToolTip('Open .SCH, .BLK or .UTSCH file…  (Ctrl+O)')
            act_open.triggered.connect(self._open_dialog)
            tb.addAction(act_open)
            tb.addSeparator()

            act_png = QAction(_qicon('export_png'), '', self)
            act_png.setToolTip('Export current view as PNG…')
            act_png.triggered.connect(self._save_png)
            tb.addAction(act_png)

            act_svg = QAction(_qicon('export_svg'), '', self)
            act_svg.setToolTip('Export current view as SVG…')
            act_svg.triggered.connect(self._save_svg)
            tb.addAction(act_svg)

            act_pdf = QAction(_qicon('export_pdf'), '', self)
            act_pdf.setToolTip('Export current view as PDF…')
            act_pdf.triggered.connect(self._save_pdf)
            tb.addAction(act_pdf)
            tb.addSeparator()

            act_gui = QAction(_qicon('launch_gui'), '', self)
            act_gui.setToolTip(
                'Open in KIUT…\n'
                'Launches the KIUT window, pre-loaded with this\n'
                'design\'s root sheet (sub-sheets auto-discover\n'
                'there too).')
            act_gui.triggered.connect(self._open_in_kiut)
            tb.addAction(act_gui)
            tb.addSeparator()

            act_fs = QAction(_qicon('fit_sheet'), '', self)
            act_fs.setToolTip('Fit sheet  (F)')
            act_fs.triggered.connect(self._fit_sheet)
            tb.addAction(act_fs)

            act_origin = QAction(_qicon('origin'), '', self)
            act_origin.setToolTip('Jump to sheet origin (0,0)')
            act_origin.triggered.connect(self._jump_to_origin)
            tb.addAction(act_origin)

            tb.addSeparator()

            self._act_crosshair = QAction(_qicon('crosshair'), '', self)
            self._act_crosshair.setCheckable(True)
            self._act_crosshair.setToolTip('Toggle full-window crosshair  (C)')
            self._act_crosshair.toggled.connect(self._on_crosshair_toggle)
            tb.addAction(self._act_crosshair)

            tb.addSeparator()

            _initial_theme = load_theme()
            self._act_theme = QAction(
                _qicon('theme_light' if _initial_theme == 'light' else 'theme_dark'),
                '', self)
            self._act_theme.setCheckable(True)
            self._act_theme.setChecked(_initial_theme == 'light')
            self._act_theme.setToolTip('Toggle white/black canvas background')
            self._act_theme.toggled.connect(self._on_theme_toggle)
            tb.addAction(self._act_theme)

            tb.addSeparator()
            _snap_label = QLabel(' Snap: ')
            _snap_label.setStyleSheet('color: white;')
            tb.addWidget(_snap_label)
            self._snap_spin = QDoubleSpinBox()
            self._snap_spin.setRange(_SNAP_MM_MIN, _SNAP_MM_MAX)
            self._snap_spin.setSingleStep(_SNAP_MM_STEP)
            self._snap_spin.setDecimals(4)
            self._snap_spin.setSuffix(' mm')
            self._snap_spin.setSpecialValueText('free')   # shown when value == minimum (0)
            self._snap_spin.lineEdit().setReadOnly(True)   # blocks typing only — arrows/+/- still work
            self._snap_spin.setToolTip(
                'Cursor snap step, in mm (0 = free/unsnapped cursor movement).\n'
                'Use the arrows, or +/-/= on the schematic view, to cycle\n'
                'through whole-unit (0.0508mm) steps. Read-only aid for the\n'
                'crosshair and status-bar readout — never written back to\n'
                'the file.')
            self._snap_spin.setValue(_SNAP_MM_DEFAULT)
            self._snap_spin.valueChanged.connect(self._on_snap_changed)
            tb.addWidget(self._snap_spin)

            tb.addSeparator()
            _lw_label = QLabel(' Line width — Thin: ')
            _lw_label.setStyleSheet('color: white;')
            tb.addWidget(_lw_label)
            self._thin_spin = QSpinBox()
            self._thin_spin.setRange(_LINE_WIDTH_MIN, _LINE_WIDTH_MAX)
            self._thin_spin.setSingleStep(_LINE_WIDTH_STEP)
            # typing is allowed (400 is a long way to scroll to); odd values
            # are snapped down to even on commit, in _on_line_width_changed
            self._thin_spin.setToolTip(
                'Thin line width, in Ulticap units (real mil = this ÷ 2):\n'
                'thin symbol lines, wires and pin stubs, on screen and in\n'
                'exports. Type a value directly or use the arrows/scroll —\n'
                'odd values are rounded down to the nearest even number on\n'
                'commit, matching Ulticap. Viewer only: never changes the\n'
                'converter\'s output.')
            self._thin_spin.setValue(int(THIN_LINE_WIDTH))
            self._thin_spin.valueChanged.connect(
                lambda v: self._on_line_width_changed('THIN_LINE_WIDTH', v))
            self._thin_spin.editingFinished.connect(
                lambda: self._on_line_width_committed('THIN_LINE_WIDTH'))
            tb.addWidget(self._thin_spin)

            _lw_label2 = QLabel(' Thick: ')
            _lw_label2.setStyleSheet('color: white;')
            tb.addWidget(_lw_label2)
            self._thick_spin = QSpinBox()
            self._thick_spin.setRange(_LINE_WIDTH_MIN, _LINE_WIDTH_MAX)
            self._thick_spin.setSingleStep(_LINE_WIDTH_STEP)
            # typing is allowed (400 is a long way to scroll to); odd values
            # are snapped down to even on commit, in _on_line_width_changed
            self._thick_spin.setToolTip(
                'Thick line width, in Ulticap units (real mil = this ÷ 2):\n'
                'thick symbol lines and buses, on screen and in exports.\n'
                'Type a value directly or use the arrows/scroll — odd\n'
                'values are rounded down to the nearest even number on\n'
                'commit, matching Ulticap. Viewer only: never changes the\n'
                'converter\'s output.')
            self._thick_spin.setValue(int(THICK_LINE_WIDTH))
            self._thick_spin.valueChanged.connect(
                lambda v: self._on_line_width_changed('THICK_LINE_WIDTH', v))
            self._thick_spin.editingFinished.connect(
                lambda: self._on_line_width_committed('THICK_LINE_WIDTH'))
            tb.addWidget(self._thick_spin)

            tb.addSeparator()
            _search_label = QLabel(' Find: ')
            _search_label.setStyleSheet('color: white;')
            tb.addWidget(_search_label)
            self._search_edit = QLineEdit()
            self._search_edit.setPlaceholderText('label / annotation / refdes / value…')
            self._search_edit.setFixedWidth(200)
            self._search_edit.setToolTip(
                'Find text among *X labels, *A annotations, and component\n'
                'refdes/attribute values (VALUE, DEVICE, PKG_TYPE, WIRELABEL,\n'
                'custom *C tags — not pin numbers). Case-insensitive\n'
                'substring match. Matches appear in a dropdown as you type,\n'
                'sorted by reference. Step through them with the Down/Up\n'
                'arrows or the mouse wheel (each one zooms/highlights as you\n'
                'go); press Enter to jump to the highlighted match and close\n'
                'the list, or click a match directly. Click the field again,\n'
                'or click elsewhere, to reopen or close the list.')

            # Live popup: a small floating QListWidget positioned under the
            # field, rebuilt on every keystroke. Qt.Tool + FramelessWindowHint
            # + WA_ShowWithoutActivating keep it from ever taking keyboard
            # focus away from the line edit (unlike a QMenu, which grabs
            # keyboard input the moment it's shown) -- the whole point is to
            # let the user keep typing while the list updates underneath,
            # rather than the field losing focus/closing on every character.
            self._search_popup = QListWidget(self)
            self._search_popup.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
            self._search_popup.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self._search_popup.setFocusPolicy(Qt.NoFocus)
            self._search_popup.itemClicked.connect(self._on_search_popup_item_clicked)
            # Keyboard/wheel navigation (see eventFilter) moves the current
            # row via setCurrentRow rather than calling _go_to_search_hit
            # directly, so a click and an arrow-key/wheel step both zoom
            # through this single path.
            self._search_popup.currentRowChanged.connect(self._on_search_popup_row_changed)

            self._search_edit.textChanged.connect(self._update_search_popup)
            # Application-wide, not just on the field: a wheel event over
            # the popup itself (very natural, since it sits right under the
            # field) needs to reach us too instead of falling through to the
            # list's own native scrolling, and a click that should close the
            # popup can land on any other widget entirely (the canvas, a
            # menu, ...), not just ones filtered directly.
            QApplication.instance().installEventFilter(self)
            # editingFinished fires on Enter or focus-out; with the popup
            # covering the "pick one of several" case, this is left only to
            # handle the single-match / no-match cases (see
            # _on_search_activated).
            self._search_edit.editingFinished.connect(self._on_search_activated)
            tb.addWidget(self._search_edit)

        def eventFilter(self, obj, event):
            etype = event.type()

            # Close the popup on a click anywhere outside it and outside the
            # Find field -- checked first, ahead of the object-specific
            # branches below, since the click that should close it is most
            # often on some unrelated widget (the canvas, a menu, ...) that
            # none of those branches would otherwise ever see.
            if etype == QEvent.MouseButtonPress and self._search_popup.isVisible():
                gp = event.globalPosition().toPoint()
                edit_rect = QRect(self._search_edit.mapToGlobal(QPoint(0, 0)),
                                  self._search_edit.size())
                if not self._search_popup.geometry().contains(gp) \
                        and not edit_rect.contains(gp):
                    self._search_popup.hide()

            if obj is self._search_edit:
                if etype == QEvent.MouseButtonPress:
                    # Re-show the popup with whatever currently matches when
                    # the user clicks back into a non-empty field -- the
                    # popup is otherwise only driven by textChanged, which
                    # doesn't fire again just because the field regains focus.
                    if self._search_edit.text().strip():
                        self._update_search_popup()
                elif etype == QEvent.KeyPress:
                    if event.key() == Qt.Key_Escape:
                        self._search_popup.hide()
                    elif event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                            and self._search_popup.isVisible() \
                            and self._search_popup.currentRow() >= 0:
                        # Jump to the highlighted row and close, same as a
                        # click -- it's already been zoomed/highlighted live
                        # as the user arrowed/scrolled to it (currentRowChanged),
                        # so only closing the popup is left to do. Consumed
                        # (return True) so this doesn't also fire
                        # editingFinished -> _on_search_activated for the same
                        # keystroke. Falls through to that single-hit path
                        # instead when nothing's been highlighted yet (e.g. the
                        # popup just opened on exactly one match).
                        self._on_search_popup_item_clicked(None)
                        return True
                    elif event.key() in (Qt.Key_Down, Qt.Key_Up) \
                            and self._search_popup.isVisible() and self._search_popup.count():
                        # Step through the still-open popup and zoom to each
                        # in turn (via currentRowChanged), without leaving the
                        # Find field or closing the list -- consumed (return
                        # True) so the key doesn't also move the text cursor.
                        self._move_search_selection(1 if event.key() == Qt.Key_Down else -1)
                        return True

            if etype == QEvent.Wheel and self._search_popup.isVisible() and self._search_popup.count() \
                    and (obj is self._search_edit or obj is self._search_popup
                         or (isinstance(obj, QWidget) and self._search_popup.isAncestorOf(obj))):
                # angleDelta is the normal case (positive = wheel rotated up,
                # i.e. Qt.Key_Up's direction); pixelDelta is the fallback for
                # precision touchpads/trackpad drivers that report only a
                # pixel delta and leave angleDelta at (0, 0) -- without this
                # fallback, scrolling on such a device always took the "else"
                # branch below regardless of direction, which read as
                # "downwards works, upwards doesn't."
                dy = event.angleDelta().y() or event.pixelDelta().y()
                if dy:
                    self._move_search_selection(-1 if dy > 0 else 1)
                return True

            return super().eventFilter(obj, event)

        def _move_search_selection(self, delta):
            """Move the popup's current row by delta (+1/-1), clamped to
            the list's bounds (no wraparound), and let currentRowChanged
            zoom/highlight the newly-current match. Shared by the arrow
            keys and the mouse wheel."""
            row = self._search_popup.currentRow()
            row = 0 if row < 0 else max(0, min(self._search_popup.count() - 1, row + delta))
            self._search_popup.setCurrentRow(row)

        def _update_search_popup(self):
            """Rebuild and (re)show the live match list under the Find
            field from its current text. Hidden entirely for empty text or
            zero matches -- a single match still gets a one-row list (the
            user can click it, or just press Enter as before)."""
            query = self._search_edit.text()
            self._search_popup.hide()
            if not query.strip():
                return
            hits = self._canvas._search_matches(query)
            if not hits:
                return
            self._search_popup.clear()
            for label, kind, obj, x, y, extent in hits:
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, (kind, obj, x, y, extent))
                self._search_popup.addItem(item)

            row_h = self._search_popup.sizeHintForRow(0)
            visible_rows = min(len(hits), 8)
            height = visible_rows * row_h + 4
            pos = self._search_edit.mapToGlobal(QPoint(0, self._search_edit.height()))
            self._search_popup.setGeometry(pos.x(), pos.y(),
                                           self._search_edit.width(), height)
            self._search_popup.show()

        def _on_search_popup_item_clicked(self, _item):
            # currentRowChanged has already zoomed/highlighted this row by
            # the time itemClicked fires (Qt sets the current row on mouse
            # press, before emitting clicked() on release) -- only closing
            # the popup is left to do here.
            self._search_popup.hide()

        def _on_search_popup_row_changed(self, row):
            """Zoom/highlight the newly-current popup row -- fired by both
            arrow-key/wheel navigation (_move_search_selection) and by
            clicking a row (QListWidget sets currentRow on click too, just
            before itemClicked's own handler runs). row is -1 right after
            .clear() in _update_search_popup, which is a no-op here."""
            if row < 0:
                return
            item = self._search_popup.item(row)
            if item is None:
                return
            kind, obj, x, y, extent = item.data(Qt.UserRole)
            self._go_to_search_hit(kind, obj, x, y, extent)

        def _on_search_activated(self):
            query = self._search_edit.text()
            if not query.strip():
                return
            canvas = self._canvas
            hits = canvas._search_matches(query)
            if not hits:
                # Brief red-border nudge for "no matches", auto-clearing —
                # consistent with the toolbar's otherwise-quiet feedback
                # style (no dialog for a routine miss).
                self._search_edit.setStyleSheet('border: 1px solid #cc4444;')
                QTimer.singleShot(700, lambda: self._search_edit.setStyleSheet(''))
                return
            self._search_edit.setStyleSheet('')
            if len(hits) == 1:
                _label, kind, obj, x, y, extent = hits[0]
                self._go_to_search_hit(kind, obj, x, y, extent)
                self._search_popup.hide()
            # More than one hit: leave it to the live popup (already showing
            # the choices) rather than popping a second, redundant menu here.

        def _go_to_search_hit(self, kind, obj, x, y, extent):
            """Select a Find match through the same click-to-inspect
            highlight system a canvas click uses (dims the rest of the
            sheet, keeps the match at full brightness) instead of just
            navigating there -- also means a stale highlight from a prior
            inspection is correctly replaced rather than left dimmed over
            unrelated content. extent matches the Components/Signals
            panels' own framing (see _search_matches) instead of the
            looser default an ordinary canvas click uses."""
            canvas = self._canvas
            if kind == 'component':
                canvas._set_inspected(obj)
                # _set_inspected alone doesn't move the view (a canvas
                # click is already where it needs to be); Find can jump
                # from anywhere on the sheet, so zoom to it explicitly.
                canvas._zoom_to(x, y, extent)
            else:
                canvas._inspect_annot(kind, obj, extent)
            canvas.update()

        def _jump_to_origin(self):   self._canvas._jump_to_origin()

        # ── side pane show/hide + resizing ──────────────────────────────────
        def _toggle_pane(self, checked):
            sizes = self._splitter.sizes()
            total = sum(sizes) or 1200
            if checked:
                self._splitter.setSizes([self._last_pane_width, total - self._last_pane_width])
            else:
                if sizes[0] > 0:
                    self._last_pane_width = sizes[0]
                self._splitter.setSizes([0, total])

        def _make_hidden_toggle(self, key):
            """The "Show hidden" column, shown only while a symbol is inspected
            (registered with the markers' "Selected" column for that): on the
            Symbols row its plain title, on the pin-name, pin-number, reference
            and value rows a clickable square in the row's own colour -- filled
            when that group's hidden texts of the inspected symbol are shown
            (in italic), dim when not. Its state is the canvas's
            _inspect_hidden. A wording such as "hidden" read as "hidden
            symbols"; the squares say "this row's texts"."""
            if key == 'symbols':
                title = QLabel('<b>Show hidden</b>')
                title.setStyleSheet('font-size:10px; color:#9cdcfe;')
                title.setVisible(False)
                self._inspect_toggle_labels['hidden:symbols'] = title
                return title
            sq = QLabel()
            sq.setObjectName('hiddenSquare')
            sq.setFixedSize(10, 10)
            sq.setCursor(Qt.PointingHandCursor)
            sq.setVisible(False)
            colour_cell = self._pin_rows[key][5]   # the row's live swatch colour

            def _refresh(w=sq, k=key, cc=colour_cell):
                # the panel is built before the canvas: off until it exists
                canvas = getattr(self, '_canvas', None)
                on = canvas is not None and canvas._inspect_hidden[k]
                # Scoped to the square's own object name: a bare style sheet
                # (or a QLabel rule -- the tooltip is a QLabel too) also
                # styles the square's tooltip, which took the square's colour.
                w.setStyleSheet(f'#hiddenSquare {{ background:{cc[0]}; border:1px solid #888; }}' if on else
                                '#hiddenSquare { background:transparent; border:1px solid #5a5a5a; }')

            def _on_click(_e, k=key):
                h = self._canvas._inspect_hidden
                h[k] = not h[k]
                for fn in self._hidden_toggle_refresh:
                    fn()
                self._canvas.update()

            sq.mousePressEvent = _on_click
            self._hidden_toggle_refresh.append(_refresh)
            _refresh()
            self._inspect_toggle_labels[f'hidden:{key}'] = sq
            return sq

        def _on_layer_change(self, _):
            for key,cb in self._checks.items():
                self._canvas._vis[key] = cb.isChecked()
            # Cosmetically dim pin rows when symbols layer is disabled;
            # also block clicks on pin rows when symbols is off.
            sym_on = self._checks['symbols'].isChecked()
            for key, (row, cb, sw, lb, apply_dim, _cur_col) in self._pin_rows.items():
                apply_dim(sym_on and cb.isChecked())
                row.setCursor(Qt.PointingHandCursor if sym_on else Qt.ArrowCursor)
            # A "Hidden ..." row looks active only while its parent row (and,
            # for the symbol texts, Symbols) is on as well.
            for hid, parent in _HIDDEN_PARENT_OF.items():
                if hid in self._row_dim and hid in self._checks:
                    on = self._checks[hid].isChecked() and self._checks[parent].isChecked()
                    if parent != 'annotations':
                        on = on and sym_on
                    self._row_dim[hid](on)
            self._canvas.update()

        def _on_toggle_all_markers(self):
            """Hide/restore exactly the markers that were enabled -- NOT a
            blanket force-everything-on, so markers the user had
            deliberately left off stay off when restoring."""
            marker_keys = [k for k in self._checks
                          if k.startswith('anc') or k.startswith('poe')]
            if self._marker_mute_snapshot is None:
                self._marker_mute_snapshot = {k: self._checks[k].isChecked()
                                              for k in marker_keys}
                for k in marker_keys:
                    self._checks[k].setChecked(False)
                self._marker_mute_btn.setText('Show all')
                for row in self._marker_rows.values():
                    row.setCursor(Qt.ArrowCursor)
            else:
                for k, was_on in self._marker_mute_snapshot.items():
                    self._checks[k].setChecked(was_on)
                self._marker_mute_snapshot = None
                self._marker_mute_btn.setText('Hide all')
                for row in self._marker_rows.values():
                    row.setCursor(Qt.PointingHandCursor)

        def _on_inspection_changed(self):
            """Show/hide the secondary "Selected" column (per-component
            marker-type toggles) based on whether something is currently
            inspected. The toggles' own on/off state is untouched here --
            only visibility changes; see _inspect_marker_vis for why the
            state itself persists across different inspected components.

            Also drives the status bar's selection section (see
            _selection_label) -- this fires from every place inspection
            state changes (component click, *X/*A click, Find, Jump to
            origin, Fit Sheet's clear, an empty-canvas click's clear),
            since all of them already call inspect_cb.

            Two lines, like the coordinate readout: line 1 is the item's
            type (+ height for *X/*A), line 2 is its anchor/POE
            coordinates in fixed-width fields so the numbers line up
            regardless of which kind of item is currently shown. The *X/*A
            TEXT CONTENT itself is deliberately never shown here -- it can
            be arbitrarily long and would push the coordinates off screen
            in a narrow window; the text is already visible on the canvas
            itself, this bar is for the info that isn't."""
            comp = self._canvas._inspected_comp
            shown = comp is not None
            self._inspect_col_title.setVisible(shown)
            for lbl in self._inspect_toggle_labels.values():
                lbl.setVisible(shown)

            def _xy(x, y):
                return (f'{x:+7.0f}u,{y:+7.0f}u '
                        f'({x*MM_PER_UNIT:+8.2f},{y*MM_PER_UNIT:+8.2f} mm)')

            line1, line2, info = '', '', ''
            if comp is not None:
                line1 = f'{comp.display_refdes()}   (component)'
                line2 = f'origin: {_xy(comp.x, comp.y)}'
                text = self._canvas._inspected_text
                if text is not None and text[0] is comp:
                    info = self._describe_clicked_text(comp, text[1:])
                variants = _describe_variants(comp, self._canvas.sheet)
                if variants:
                    info = f'{info}   |   {variants}' if info else variants
            else:
                annot = self._canvas._inspected_annot
                if annot is not None:
                    kind, obj = annot
                    if kind == 'origin':
                        line1 = 'origin'
                        line2 = f'anchor: {_xy(0, 0)}'
                    elif kind == 'label':
                        size_u = obj.size or 35
                        line1 = f'*X   h: {size_u}u ({size_u*MM_PER_UNIT:.2f}mm)'
                        line2 = f'anchor: {_xy(obj.x, obj.y)}'
                    else:   # annotation
                        size_u = obj.size or 35
                        line1 = f'*A   h: {size_u}u ({size_u*MM_PER_UNIT:.2f}mm)'
                        line2 = (f'anchor: {_xy(obj.x, obj.y)}'
                                 f'   POE: {_xy(obj.x_poe, obj.y_poe)}')
            self._selection_label.setText(f'{line1}\n{line2}' if line1 else '')
            self._selection_info.setText(info)

        def _describe_clicked_text(self, comp, text):
            """The status-bar line for a clicked detached text of comp,
            text = (tag, value, dx, dy): what it is (_describe_comp_text)."""
            sheet = self._canvas.sheet
            sym = sheet.symbols.get(comp.symbol_name) if sheet else None
            if sym is None:
                return ''
            pwr = is_pwr_symbol(sym)
            for tag, val, dx, dy, dpx, dpy, _sz, _col, vis_byte in _comp_attr_entries(
                    comp, sym, True, True, pwr, True, True,
                    show_hidden=frozenset(_SYMBOL_TEXT_GROUPS)):
                if (tag, val, dx, dy) == tuple(text):
                    return _describe_comp_text(comp, sym, tag, val, dpx, dpy,
                                               not (vis_byte & 128), pwr)
            return ''

        def _update_layer_colours(self, colours: dict):
            """Called by canvas.colours_cb after each load() with the
            dominant palette colour per layer.  Updates swatches (schematic
            rows) and coloured glyphs (anc/poe rows) in place."""
            self._layer_colours = colours   # cache for export dialog
            for key, col in colours.items():
                if key in self._swatches:
                    self._swatches[key].setStyleSheet(
                        f'background:{col};border:1px solid #888;')
                    # Keep the row's mutable colour cell in sync so _apply_dim
                    # uses the live/current colour, not the stale _LAYERS
                    # default captured at build time (pin/refdes/value rows
                    # via _pin_rows, everything else via _swatch_cells).
                    if key in self._pin_rows:
                        self._pin_rows[key][5][0] = col
                    if key in self._swatch_cells:
                        self._swatch_cells[key][0] = col
                if key in self._glyph_labels:
                    glyph, rest = self._glyph_chars[key]
                    self._glyph_labels[key].setText(
                        f'<span style="color:{col};font-size:13px">{glyph}</span>'
                        f'<span style="color:#cccccc">{rest}</span>')
            for fn in self._hidden_toggle_refresh:   # "Show hidden" squares: row colours
                fn()
            # The swatches were just recoloured whatever their row's state:
            # re-apply the dimmed look of the rows that are off (a "Hidden
            # annotations" row that is off showed a bright swatch).
            for key, dim in self._row_dim.items():
                if key in self._pin_rows or key in _HIDDEN_PARENT_OF:
                    continue   # set below, with their parent rows
                dim(self._checks[key].isChecked())
            self._on_layer_change(None)

        def _on_crosshair_toggle(self, checked):
            self._canvas._full_crosshair = checked
            self._act_crosshair.setIcon(_qicon('crosshair_on' if checked else 'crosshair'))
            self._canvas.update()

        def _on_theme_toggle(self, checked):
            self._canvas._theme = 'light' if checked else 'dark'
            self._act_theme.setIcon(_qicon('theme_light' if checked else 'theme_dark'))
            save_theme(self._canvas._theme)
            if self._canvas.sheet is not None:
                self._update_layer_colours(
                    _layer_swatch_colours(self._canvas.sheet, self._canvas._theme))
            self._canvas.update()

        def _on_snap_changed(self, value_mm):
            # Snap arrow-stepping always lands on an exact multiple of
            # _SNAP_MM_STEP already; only correct here when the value came
            # from direct typing, so a user can't dial in an off-step value.
            steps = round(value_mm / _SNAP_MM_STEP) if _SNAP_MM_STEP else 0
            corrected = max(_SNAP_MM_MIN, min(_SNAP_MM_MAX, steps * _SNAP_MM_STEP))
            if abs(corrected - value_mm) > 1e-6:
                self._snap_spin.blockSignals(True)
                self._snap_spin.setValue(corrected)
                self._snap_spin.blockSignals(False)
                value_mm = corrected
            self._canvas._snap_mm = value_mm
            self._canvas._update_status()
            self._canvas.update()
            save_snap_mm(value_mm)

        def _on_line_width_changed(self, name, value):
            # Live preview while the field is being edited. Arrow/scroll
            # changes always land on an even value already (step=2 from an
            # even start), so persist those immediately; a value reached by
            # typing only gets corrected + persisted on commit, in
            # _on_line_width_committed — otherwise typing a multi-digit
            # odd-first value like 381 would get clobbered after the first
            # keystroke.
            globals()[name] = float(value)
            if value % 2 == 0:
                _save_line_width_tuning()
            self._canvas.update()

        def _on_line_width_committed(self, name):
            spin = self._thin_spin if name == 'THIN_LINE_WIDTH' else self._thick_spin
            value = spin.value()
            corrected = value - (value % 2)   # Ulticap only stores even values
            if corrected != value:
                spin.blockSignals(True)
                spin.setValue(corrected)
                spin.blockSignals(False)
            globals()[name] = float(corrected)
            _save_line_width_tuning()
            self._canvas.update()

        # ── file open / hierarchy navigation ────────────────────────────────
        def _open_dialog(self):
            start = str(self._current_path.parent) if self._current_path else str(self._initial_dir)
            path,_ = QFileDialog.getOpenFileName(
                self,'Open Ulticap schematic', start,
                'Ulticap schematic (*.SCH *.sch *.BLK *.blk *.UTSCH *.utsch);;All files (*)')
            if not path: return
            self._root_path = Path(path)   # File ▸ Open starts a fresh hierarchy
            _READ_MESSAGES.clear()         # ... and its own messages
            self._load_and_show(Path(path), fresh=True)

        def _is_current(self, path: Path, key: Optional[str]) -> bool:
            """Whether (path, key) is the sheet already on screen."""
            return (self._current_path is not None
                    and self._current_path.resolve() == Path(path).resolve()
                    and self._current_key == key)

        def _load_and_show(self, path: Path, add_to_recent: bool = True,
                           push_history: bool = True, key: Optional[str] = None,
                           fresh: bool = False):
            """Show a sheet: an ASCII file, or the sheet `key` names in a
            .UTSCH (its root when None). `fresh` re-reads a .UTSCH even when
            it is already in memory (File ▸ Open, a recent file)."""
            try:
                sheet, key = _load_design_sheet(path, key, fresh)
            except Exception as e:
                QMessageBox.critical(self,'Open failed',str(e)); return
            if not sheet:
                QMessageBox.critical(self,'Open failed', f'Could not parse {path.name}'); return
            if push_history and self._current_path and not self._is_current(path, key):
                self._sheet_history.append((self._current_path, self._current_key))
            self._current_path = path
            self._current_key = key
            self._canvas.load(sheet)
            self.setWindowTitle(f'KIUT Viewer — {sheet.name}')
            self._populate_sheets()
            self._update_read_messages()
            self._populate_signals()
            self._populate_refdes()
            self._lists_tabs.setVisible(True)
            if add_to_recent:
                self._recent = add_recent_file(path)
                self._rebuild_recent_menu()

        def _populate_signals(self):
            self._signals_tree.clear()
            sheet = self._canvas.sheet
            if sheet:
                entries = []
                for comp in sheet.components:
                    sym = sheet.symbols.get(comp.symbol_name)
                    sig_pins = getattr(sym, 'signal_pins', None) if sym else None
                    if sig_pins:
                        # Half-width of the eventual fit box = half the
                        # symbol's largest dimension + a fixed margin, NOT a
                        # multiple of symbol size -- multiplying scales the
                        # box faster than the symbol grows, so large symbols
                        # (e.g. a 1100x500-unit IC) produced boxes rivaling
                        # or exceeding the whole sheet's own content extent,
                        # making 'zoom to' zoom OUT instead of in. _zoom_to
                        # also has its own hard floor against this, but the
                        # formula itself should stay sane for typical cases.
                        MARGIN = _ZOOM_SYMBOL_MARGIN_UNITS
                        extent = max(sym.width, sym.height, 100)/2 + MARGIN if sym else 400
                        # comp.x/comp.y is the symbol's local (0,0) -- its
                        # bottom-left corner, not its centre (see
                        # kiuc_model.tight_bounds' own comment on symbol local
                        # coords) -- so centring the fit box there directly
                        # skews it away from the actual body by half its
                        # width/height, rotated. This is why a component
                        # whose visible attributes (e.g. RefDes) sit well
                        # clear of the body could end up outside the frame:
                        # the box wasn't even centred on the body itself.
                        # Attributes are deliberately NOT included here (they
                        # can be placed arbitrarily far from the symbol,
                        # which would make 'zoom to' balloon out to the point
                        # of being useless) -- only the symbol's own body is
                        # centred and framed.
                        if sym:
                            cdx, cdy = _rot_transform(sym.width/2, sym.height/2,
                                                       comp.rotation)
                            cx, cy = comp.x + cdx, comp.y + cdy
                        else:
                            cx, cy = comp.x, comp.y
                        entries.append((comp.display_refdes(), cx, cy,
                                       extent, sig_pins, comp))
                entries.sort(key=lambda e: e[0])
                for refdes, x, y, extent, sig_pins, comp in entries:
                    # net name plus its pin numbers, e.g. "GND (1,3,8)" --
                    # pin numbers are otherwise only visible by hovering the
                    # symbol itself, so surfacing them here saves a trip.
                    nets = ', '.join(
                        f'{net} ({",".join(pins)})' if pins else net
                        for net, pins in sig_pins)
                    item = QTreeWidgetItem([f'{refdes}   {nets}'])
                    item.setData(0, Qt.UserRole, (x, y, extent, comp))
                    self._signals_tree.addTopLevelItem(item)

        def _on_signals_click(self, item, _column):
            data = item.data(0, Qt.UserRole)
            if not data: return
            x, y, extent, comp = data
            # Highlight the component through the same click-to-inspect
            # system a canvas click uses, not just navigate to it --
            # otherwise a highlight/dim left over from a previous
            # inspection stays stuck (only an empty-canvas click cleared
            # it before). _set_inspected doesn't move the view on its own
            # (a canvas click is already where it needs to be), so zoom
            # explicitly since this can jump from anywhere on the sheet.
            self._canvas._set_inspected(comp)
            self._canvas._zoom_to(x, y, extent)

        def _populate_refdes(self):
            self._refdes_tree.clear()
            sheet = self._canvas.sheet
            if not sheet:
                return
            entries = []
            for comp in sheet.components:
                if comp.file_ref:
                    continue   # hierarchical sub-sheet block, not a real part
                sym = sheet.symbols.get(comp.symbol_name)
                if sym and is_pwr_symbol(sym):
                    continue   # power symbol (GND/VCC/...), not a real part
                if sym and is_port_sym(sym):
                    continue   # hierarchical port marker, not a real part
                if sym and sym.name.upper() in _NON_PART_SYMBOL_NAMES:
                    continue   # known non-part placeholder (see _NON_PART_SYMBOL_NAMES)
                # Same fit-box sizing/centring as _populate_signals -- see
                # its comments for why extent and centring are computed
                # this way (symbol body only, not attribute positions).
                MARGIN = _ZOOM_SYMBOL_MARGIN_UNITS
                extent = max(sym.width, sym.height, 100)/2 + MARGIN if sym else 400
                if sym:
                    cdx, cdy = _rot_transform(sym.width/2, sym.height/2, comp.rotation)
                    cx, cy = comp.x + cdx, comp.y + cdy
                else:
                    cx, cy = comp.x, comp.y
                # Effective VALUE and DEVICE each resolved *C-overrides-*S
                # per tag -- the same general rule _comp_attr_entries uses
                # for canvas display of any attribute, applied here to both
                # tags individually rather than mixing *C-DEVICE in as a
                # fallback for *S-VALUE (which would wrongly skip a real
                # *S DEVICE default when *C has neither tag at all).
                # DEVICE (e.g. a connector's real part number, "B7B-XH-A")
                # is a more useful fallback than a generic symbol default
                # when a part simply has no VALUE tag at all -- common for
                # connectors, crystals, ICs identified by device rather
                # than value. Shown even when hidden on the sheet itself
                # (same idea as showing pin numbers in Signals — the data
                # exists, it's just not drawn).
                eff_value  = comp.value  or (sym.attributes.get('VALUE')  if sym else None)
                eff_device = comp.device or (sym.attributes.get('DEVICE') if sym else None)
                value = eff_value or eff_device or '-'
                entries.append((comp.display_refdes(), value, cx, cy, extent, comp))
            entries.sort(key=lambda e: _natural_sort_key(e[0]))
            for refdes, value, x, y, extent, comp in entries:
                item = QTreeWidgetItem([f'{refdes}   {value}'])
                item.setData(0, Qt.UserRole, (x, y, extent, comp))
                self._refdes_tree.addTopLevelItem(item)

        def _on_refdes_click(self, item, _column):
            data = item.data(0, Qt.UserRole)
            if not data: return
            x, y, extent, comp = data
            # Same highlight-through-inspect treatment as Signals -- see
            # its _on_signals_click for the rationale.
            self._canvas._set_inspected(comp)
            self._canvas._zoom_to(x, y, extent)

        def _populate_sheets(self):
            self._sheets_tree.clear()
            has_tree = False
            if self._root_path:
                tree = (_utsch_design(self._root_path).tree if _is_utsch(self._root_path)
                        else _build_hierarchy_tree(self._root_path))
                if tree and tree.children:
                    self._add_sheet_node(None, tree)
                    self._sheets_tree.expandAll()
                    has_tree = True
            self._sheets_group.setVisible(has_tree)
            self._sheets_sep.setVisible(has_tree)

        def _add_sheet_node(self, parent_item, node):
            item = QTreeWidgetItem([node.label])
            key = getattr(node, 'key', None)      # set for a .UTSCH sheet only
            item.setData(0, Qt.UserRole, (str(node.path), key))
            if self._is_current(node.path, key):
                font = item.font(0); font.setBold(True); item.setFont(0, font)
                item.setForeground(0, QColor('#4ec9b0'))
            if parent_item is None:
                self._sheets_tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in node.children:
                self._add_sheet_node(item, child)
            return item

        def _on_sheets_click(self, item, _column):
            data = item.data(0, Qt.UserRole)
            if not data:
                return
            path_str, key = data
            if self._is_current(Path(path_str), key):
                return   # already showing this sheet
            self._load_and_show(Path(path_str), add_to_recent=False, key=key)

        def _on_enter_subsheet(self, comp):
            """Canvas double-click on a component with a FILE_REF (see
            SchCanvas.mouseDoubleClickEvent/enter_subsheet_cb) -- resolve
            it the same way the Sheets panel's tree does and navigate
            there."""
            ref = getattr(comp, 'file_ref', None)
            if not ref or not self._current_path:
                return
            if _is_utsch(self._current_path):
                # The sub-sheet is in the same file: found by name, the
                # way the converter finds it.
                target, key = self._current_path, \
                    _utsch_design(self._current_path).child_key(ref)
            else:
                target, key = _resolve_file_ref(ref, self._current_path.parent), None
            if target is None or (_is_utsch(target) and key is None):
                return
            if self._is_current(target, key):
                return   # already showing this sheet
            self._load_and_show(target, add_to_recent=False, key=key)

        def _go_back(self):
            """'B' shortcut (see SchCanvas.keyPressEvent/back_cb): pop the
            sheet-navigation history and return to whatever was showing
            before the current sheet -- covers double-click-into-subsheet,
            Sheets-panel clicks, and File > Open alike, since all of them
            go through _load_and_show's push_history. No forward/redo --
            just a quick way back that doesn't need a trip to the mouse,
            which a dedicated Back button wouldn't have offered over the
            Sheets tree anyway."""
            if not self._sheet_history:
                return
            target, key = self._sheet_history.pop()
            self._load_and_show(target, add_to_recent=False, push_history=False, key=key)

        # ── help ─────────────────────────────────────────────────────────────
        def _show_help(self):
            if hasattr(self, '_help_dlg') and self._help_dlg.isVisible():
                self._help_dlg.raise_()
                self._help_dlg.activateWindow()
                return
            flags = (Qt.WindowType.Window |
                     Qt.WindowType.WindowTitleHint |
                     Qt.WindowType.WindowSystemMenuHint |
                     Qt.WindowType.WindowCloseButtonHint)
            dlg = QDialog(None, flags)
            dlg.setWindowTitle('KIUT Viewer — Help')
            dlg.setFixedSize(600, 500)
            dlg.move(self.x() + (self.width()  - dlg.width())  // 2,
                     self.y() + (self.height() - dlg.height()) // 2)
            lay = QVBoxLayout(dlg)
            txt = QPlainTextEdit(_HELP_TEXT)
            txt.setReadOnly(True)
            txt.setStyleSheet('background:#1e1e1e;color:#cccccc;font-family:Consolas;')
            lay.addWidget(txt)
            self._help_dlg = dlg
            dlg.show()
            if sys.platform == 'win32':
                try:
                    import ctypes
                    GWL_STYLE      = -16
                    WS_MINIMIZEBOX = 0x00020000
                    WS_MAXIMIZEBOX = 0x00010000
                    SWP_NOMOVE     = 0x0002
                    SWP_NOSIZE     = 0x0001
                    SWP_NOZORDER   = 0x0004
                    SWP_FRAMECHANGED = 0x0020
                    hwnd  = int(dlg.winId())
                    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
                    ctypes.windll.user32.SetWindowLongW(
                        hwnd, GWL_STYLE, style & ~WS_MINIMIZEBOX & ~WS_MAXIMIZEBOX)
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, 0, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
                except Exception:
                    pass

        def _show_about(self):
            QMessageBox.about(self, 'About KIUT Viewer', about_text())

        def _show_documentation(self):
            """VIEWER.md as a web page in the browser (kiut.core.docs_html:
            every document rendered into the temporary folder, each time)."""
            from kiut.core import docs_html
            try:
                docs_html.open_document(APP_DIR, 'docs/VIEWER.md')
            except Exception as e:
                QMessageBox.information(self, 'KIUT Viewer', f'The documentation could not be shown ({e}).')

        def _update_read_messages(self):
            n = len(_READ_MESSAGES)
            self._read_label.setText(f'{n} message{"s" if n != 1 else ""} reading the design: '
                                     f'View → Reading messages…' if n else '')
            self._act_read.setEnabled(bool(n))

        def _show_read_messages(self):
            """What reading the design's files said (the conversion's log says
            the same), in a window of its own."""
            dlg = QDialog(self)
            dlg.setWindowTitle('KIUT Viewer — reading messages')
            lay = QVBoxLayout(dlg)
            lay.addWidget(QLabel('What reading the design\'s files said. It changes nothing in the drawing '
                                 'or in a conversion, whose log says the same.'))
            txt = QPlainTextEdit('\n'.join(_READ_MESSAGES))
            txt.setReadOnly(True)
            txt.setMinimumSize(720, 240)
            lay.addWidget(txt)
            ok = QPushButton('Close')
            ok.clicked.connect(dlg.accept)
            lay.addWidget(ok)
            dlg.exec()

        def _show_sheet_info(self):
            """Symbols/components/wires/junctions/grid summary for the
            currently loaded sheet -- used to always occupy left-pane
            space; now a popup (View menu) to keep the pane less crowded."""
            QMessageBox.information(self, 'Schematic info',
                f'<pre style="font-family:Consolas">{self._info_text}</pre>')

        def _fit_sheet(self):   self._canvas._fit_sheet()

        def _vis_dict(self):
            return {k: cb.isChecked() for k,cb in self._checks.items()}

        def _has_hierarchy(self) -> bool:
            """Whether the open design has more than one sheet (the
            export dialogs offer 'All sheets' only then)."""
            if not self._root_path:
                return False
            if _is_utsch(self._root_path):
                return len(_utsch_design(self._root_path).keys()) > 1
            return (self._root_path != self._current_path
                    or len(_collect_hierarchy_paths(self._root_path)) > 1)

        def _current_stem(self) -> str:
            """Default file stem for exporting the sheet on screen: its file's
            stem, or for a .UTSCH sheet the stem the converter names it by."""
            if not self._current_path:
                return 'schematic'
            if self._current_key is not None:
                return _utsch_design(self._current_path).stem(self._current_key)
            return self._current_path.stem

        def _export_sheets(self) -> list:
            """Every sheet of the open design, for an 'All sheets' export, as
            (label, output path without extension, loader) -- one output per
            sheet next to the design: per file for an ASCII hierarchy, per
            stored sheet (named as the converter names it) for a .UTSCH."""
            if _is_utsch(self._root_path):
                d = _utsch_design(self._root_path)
                return [(d.path.name if k == d.root_key else d.by_key[k].name,
                         d.path.parent / d.stem(k), (lambda k=k: d.sheet(k)))
                        for k in d.keys()]
            return [(p.name, p.parent / p.stem, (lambda p=p: _load_sheet(p)))
                    for p in _collect_hierarchy_paths(self._root_path)]

        def _ask_export_scope(self, fmt: str) -> str:
            """SVG scope dialog: 'current', 'all', or '' (cancelled).
            For single-sheet designs silently returns 'current'."""
            has_hierarchy = self._has_hierarchy()
            if not has_hierarchy:
                return 'current'
            dlg = QMessageBox(self)
            dlg.setWindowTitle(f'Export {fmt}')
            dlg.setText(f'Export as {fmt} — which sheets?')
            btn_cur = dlg.addButton('Current sheet only', QMessageBox.ButtonRole.AcceptRole)
            btn_all = dlg.addButton('All sheets',         QMessageBox.ButtonRole.ActionRole)
            dlg.addButton('Cancel',                       QMessageBox.ButtonRole.RejectRole)
            dlg.exec()
            clicked = dlg.clickedButton()
            if clicked is btn_cur: return 'current'
            if clicked is btn_all: return 'all'
            return ''

        def _ask_png_export_options(self) -> str:
            """PNG export dialog with scope buttons + Layers button.
            Layers button opens _ExportLayerDialog (persists to INI on OK)
            then returns to this dialog.  Returns 'current', 'all', or ''.
            Always shown — even for single-sheet designs — so the Layers
            button is always accessible without requiring a repeat click."""
            has_hierarchy = self._has_hierarchy()
            result = ['']
            dlg = QDialog(self)
            dlg.setWindowTitle('Export PNG')
            dlg.setModal(True)
            lay = QVBoxLayout(dlg)
            lay.addWidget(QLabel('Export as PNG:'))
            btn_row = QHBoxLayout()
            if has_hierarchy:
                btn_cur = QPushButton('Current sheet')
                btn_all = QPushButton('All sheets')
                btn_cur.clicked.connect(lambda: (result.__setitem__(0,'current'), dlg.accept()))
                btn_all.clicked.connect(lambda: (result.__setitem__(0,'all'),     dlg.accept()))
                btn_row.addWidget(btn_cur)
                btn_row.addWidget(btn_all)
            else:
                btn_exp = QPushButton('Export')
                btn_exp.setDefault(True)
                btn_exp.clicked.connect(lambda: (result.__setitem__(0,'current'), dlg.accept()))
                btn_row.addWidget(btn_exp)
            btn_lay = QPushButton('Layers…')
            btn_can = QPushButton('Cancel')
            def _open_layers():
                ld = _ExportLayerDialog(dlg, self._layer_colours, 'PNG')
                ld.exec()   # saves to INI on OK; no return value needed here
            btn_lay.clicked.connect(_open_layers)
            btn_can.clicked.connect(dlg.reject)
            btn_row.addWidget(btn_lay)
            btn_row.addWidget(btn_can)
            lay.addLayout(btn_row)
            dlg.exec()
            return result[0]

        def _export_sheet_png(self, sheet: Sheet, out_path: Path,
                              export_vis: dict = None):
            """Render one sheet to PNG at a fixed 300 DPI, sized from the
            sheet's actual physical (mm) content dimensions — mirroring how
            _export_sheet_pdf sizes its page — rather than fitting into the
            current on-screen widget's pixel size. Fitting into an arbitrary
            widget size meant the world-unit-to-pixel ratio (and therefore
            every fixed-pixel-width pen in the renderer) varied with sheet
            size: larger sheets got a smaller ratio, so the same constant
            pen width covered a larger fraction of the shrunk spacing,
            visually "fattening" every line and pushing strokes into text/
            overline gaps that were otherwise sized and positioned correctly.
            A physical-size-based, fixed-DPI resolution keeps that ratio
            consistent across sheet sizes, matching PDF's already-correct
            behaviour.
            export_vis: {key: bool} layer selection; when None, falls back
            to load_export_png_layers() defaults. The 0,0 origin crosshair
            is always suppressed in export mode.
            """
            if export_vis is None:
                export_vis = load_export_png_layers()

            PNG_DPI = _PNG_EXPORT_DPI
            _tb = _tight_bounds_with_gap(sheet, _TIGHT_BOUNDS_GAP_UNITS)
            if _tb:
                bx0, by0, bx1, by1 = _tb
            else:
                bx0, by0, bx1, by1 = (
                    sheet.xmin, sheet.ymin, sheet.xmax, sheet.ymax)
            content_w_mm = (bx1 - bx0) * MM_PER_UNIT
            content_h_mm = (by1 - by0) * MM_PER_UNIT
            png_w = max(1, round(content_w_mm / 25.4 * PNG_DPI))
            png_h = max(1, round(content_h_mm / 25.4 * PNG_DPI))

            img = QImage(png_w, png_h, QImage.Format_ARGB32)
            img.fill(QColor(_BG_HEX))
            painter = QPainter(img)
            painter.setRenderHint(QPainter.Antialiasing)
            # No painter.scale(): tmp_vp below already maps world units to
            # device pixels at the correct physical density directly.
            # Temporarily replace canvas sheet+vp+vis so _paint() renders this
            # sheet fitted to its own border rather than the live viewport.
            old_sheet = self._canvas.sheet
            old_vp    = self._canvas._vp
            old_vis   = self._canvas._vis
            old_text_scale = getattr(self._canvas, '_text_scale', _TEXT_SCALE_DEFAULT)
            tmp_vp = _VP()
            tmp_vp.fit(bx0, by0, bx1, by1, png_w, png_h, margin=0)
            self._canvas.sheet = sheet
            self._canvas._vp   = tmp_vp
            self._canvas._vis  = export_vis
            # No override needed: setPixelSize() (see _draw_text) makes
            # font rendering DPI-independent, so PNG now matches on-screen
            # sizing at the same _text_scale (_TEXT_SCALE_DEFAULT) without a
            # separate fudge factor. Kept as an explicit override point
            # in case further visual tuning is needed.
            self._canvas._text_scale = _TEXT_SCALE_DEFAULT
            try:
                # Draw border rectangle explicitly (export_mode suppresses the
                # Ulticap sheet border; we replace it with tight_bounds + GAP)
                if export_vis.get('sheet_border', True) and _tb:
                    _bpen = QPen(QColor(_COL_BORDER), 1)
                    painter.setPen(_bpen); painter.setBrush(Qt.NoBrush)
                    painter.drawRect(QRectF(
                        tmp_vp.cx(bx0), tmp_vp.cy(by1),
                        tmp_vp.cs(bx1 - bx0), tmp_vp.cs(by1 - by0)))
                self._canvas._paint(painter, export_mode=True, force_mk=True,
                                    canvas_size=(png_w, png_h))
            finally:
                self._canvas.sheet = old_sheet
                self._canvas._vp   = old_vp
                self._canvas._vis  = old_vis
                self._canvas._text_scale = old_text_scale
                painter.end()
            if not img.save(str(out_path)):
                raise RuntimeError(
                    f"Could not write '{out_path.name}' — it may be open in "
                    f"another program, or the destination is not writable.")

        def _open_in_kiut(self):
            """The design in the KIUT window -- the open one (kiut/core/handoff.py),
            else kiut_gui.py started -- which then has this viewer as its own:
            this design's ROOT sheet -- not necessarily
            self._current_path, which may be a sub-sheet the user has
            navigated into (double-click, Sheets panel, Find): the window
            finds the sub-sheets from the root, as File > Open here always
            re-roots the Sheets tree at the file it's given."""
            if not self._canvas.sheet:
                QMessageBox.information(self, 'KIUT', 'No schematic loaded.'); return
            gui_script = APP_DIR / 'kiut_gui.py'
            if not gui_script.exists():
                QMessageBox.critical(
                    self, 'KIUT',
                    f'Could not find {gui_script.name} in {APP_DIR}.\n'
                    'Keep the KIUT folder as it is.')
                return
            from kiut.core import handoff
            # the design's original, not its copy in <name>_corrected (the window hands the
            # viewer the working copy): the window then knows its own design
            target = handoff.corrected_original(self._root_path or self._current_path)
            # the KIUT window, when open: it opens the design, and this viewer becomes its own
            reply = handoff.offer(handoff.WINDOW, [target], sender=self.viewer_id, name=self.handoff_name)
            if reply == handoff.BUSY:
                QMessageBox.information(self, 'KIUT', handoff.BUSY_TEXT); return
            if reply in (handoff.OPENED, handoff.SHOWN):
                return
            if reply:
                QMessageBox.warning(self, 'KIUT', f'The open KIUT window could not take it:\n{reply}'); return
            try:
                own = ['--viewer', self.viewer_id] if self.viewer_id else []
                subprocess.Popen([sys.executable, str(gui_script), *own, str(target)])
            except OSError as exc:
                QMessageBox.critical(self, 'KIUT', f'Failed to launch the KIUT window:\n{exc}')

        def _save_svg(self):
            if not self._canvas.sheet:
                QMessageBox.information(self,'Export','No schematic loaded.'); return
            scope = self._ask_export_scope('SVG')
            if not scope: return
            vis = self._vis_dict()
            if scope == 'current':
                stem = self._current_stem()
                out,_ = QFileDialog.getSaveFileName(
                    self, 'Save as SVG',
                    str(self._current_path.parent / stem) + '.svg'
                        if self._current_path else stem + '.svg',
                    'SVG vector (*.svg)')
                if not out: return
                try:
                    Path(out).write_text(
                        _render_svg(self._canvas.sheet, vis), encoding='utf-8')
                    QMessageBox.information(self,'Export',f'Saved: {Path(out).name}')
                except Exception as e:
                    QMessageBox.critical(self,'Export failed',str(e))
            else:
                saved, errors = [], []
                for label, base, load in self._export_sheets():
                    try:
                        sh = load()
                        if sh:
                            out = base.with_name(base.name + '.svg')
                            out.write_text(_render_svg(sh, vis), encoding='utf-8')
                            saved.append(out.name)
                    except Exception as e:
                        errors.append(f'{label}: {e}')
                msg = f'Saved {len(saved)} file(s) to {self._root_path.parent}'
                if errors:
                    msg += '\n\nErrors:\n' + '\n'.join(errors)
                QMessageBox.information(self, 'Export', msg)

        def _save_png(self):
            if not self._canvas.sheet:
                QMessageBox.information(self,'Export','No schematic loaded.'); return
            scope = self._ask_png_export_options()
            if not scope: return
            # Load layer selection from INI (may have just been updated via Layers… button)
            export_vis = load_export_png_layers()
            if scope == 'current':
                stem = self._current_stem()
                out,_ = QFileDialog.getSaveFileName(
                    self, 'Save as PNG',
                    str(self._current_path.parent / stem) + '.png'
                        if self._current_path else stem + '.png',
                    'PNG image (*.png)')
                if not out: return
                try:
                    self._export_sheet_png(self._canvas.sheet, Path(out), export_vis)
                    QMessageBox.information(self,'Export',f'Saved: {Path(out).name}')
                except Exception as e:
                    QMessageBox.critical(self,'Export failed',str(e))
            else:
                sheets = self._export_sheets()
                total = len(sheets)
                saved, errors = [], []
                dlg = QProgressDialog('Preparing export…', 'Cancel', 0, total, self)
                dlg.setWindowTitle('Exporting PNG')
                dlg.setWindowModality(Qt.WindowModality.WindowModal)
                dlg.setMinimumDuration(0)
                dlg.setValue(0)
                for i, (label, base, load) in enumerate(sheets):
                    if dlg.wasCanceled():
                        break
                    dlg.setLabelText(
                        f'Exporting {label}  ({i + 1} / {total})\n'
                        f'Cancel takes effect after the current sheet finishes.')
                    dlg.setValue(i)
                    QApplication.processEvents()
                    try:
                        sh = load()
                        if sh:
                            out = base.with_name(base.name + '.png')
                            self._export_sheet_png(sh, out, export_vis)
                            saved.append(out.name)
                    except Exception as e:
                        errors.append(f'{label}: {e}')
                dlg.setValue(total)
                cancelled = dlg.wasCanceled()
                dlg.close()
                if cancelled and not saved:
                    return
                msg = f'Saved {len(saved)} of {total} file(s) to {self._root_path.parent}'
                if cancelled:
                    msg += '\n\nExport was cancelled — remaining sheets were skipped.'
                if errors:
                    msg += '\n\nErrors:\n' + '\n'.join(errors)
                QMessageBox.information(self, 'Export', msg)

        # ── PDF export ────────────────────────────────────────────────────────

        def _ask_pdf_export_options(self):
            """PDF export dialog: scope (current/all), path, options, layers.

            Returns (scope, path_str, fit_a4, bw) where scope is 'current'
            or 'all', or (None, None, False, False) on cancel.

            For single-sheet designs the scope buttons are replaced by a plain
            Export button.  The Layers… button reuses _ExportLayerDialog and
            saves to the shared PNG/PDF layer INI section.
            """
            has_hierarchy = self._has_hierarchy()

            stem = self._current_stem()
            default = (str(self._current_path.parent / stem) + '.pdf'
                       if self._current_path else stem + '.pdf')

            dlg = QDialog(self)
            dlg.setWindowTitle('Export PDF')
            dlg.setModal(True)
            lay = QVBoxLayout(dlg)

            # ── path row ──────────────────────────────────────────────────────
            path_row = QHBoxLayout()
            path_label = QLabel(default)
            path_label.setStyleSheet('QLabel{background:#1e1e1e;color:#d4d4d4;'
                                     'border:1px solid #555;padding:2px 4px;}')
            path_label.setMinimumWidth(340)
            path_result = [default]
            btn_browse = QPushButton('Browse…')
            def _browse():
                p, _ = QFileDialog.getSaveFileName(
                    dlg, 'Save as PDF', default, 'PDF document (*.pdf)')
                if p:
                    path_result[0] = p
                    path_label.setText(p)
            btn_browse.clicked.connect(_browse)
            path_row.addWidget(path_label, 1)
            path_row.addWidget(btn_browse)
            lay.addLayout(path_row)

            # ── options ───────────────────────────────────────────────────────
            cb_a4 = QCheckBox('Fit to A4')
            cb_bw = QCheckBox('Black on white (monochrome)')
            cb_a4.setChecked(False)
            cb_bw.setChecked(False)
            lay.addWidget(cb_a4)
            lay.addWidget(cb_bw)

            # ── button row ────────────────────────────────────────────────────
            scope_result = ['']
            btn_row = QHBoxLayout()

            if has_hierarchy:
                btn_cur = QPushButton('Current sheet')
                btn_all = QPushButton('All sheets')
                btn_cur.clicked.connect(
                    lambda: (scope_result.__setitem__(0, 'current'), dlg.accept()))
                btn_all.clicked.connect(
                    lambda: (scope_result.__setitem__(0, 'all'), dlg.accept()))
                btn_row.addWidget(btn_cur)
                btn_row.addWidget(btn_all)
            else:
                btn_exp = QPushButton('Export')
                btn_exp.setDefault(True)
                btn_exp.clicked.connect(
                    lambda: (scope_result.__setitem__(0, 'current'), dlg.accept()))
                btn_row.addWidget(btn_exp)

            btn_lay = QPushButton('Layers…')
            btn_can = QPushButton('Cancel')
            def _open_layers():
                ld = _ExportLayerDialog(dlg, self._layer_colours, 'PDF')
                ld.exec()   # saves to INI on OK
            btn_lay.clicked.connect(_open_layers)
            btn_can.clicked.connect(dlg.reject)
            btn_row.addWidget(btn_lay)
            btn_row.addWidget(btn_can)
            lay.addLayout(btn_row)

            dlg.exec()
            if not scope_result[0]:
                return None, None, False, False
            return scope_result[0], path_result[0], cb_a4.isChecked(), cb_bw.isChecked()

        def _export_sheet_pdf(self, sheet: Sheet, out_path: Path,
                              fit_a4: bool = False, bw: bool = False,
                              export_vis: dict = None):
            """Render one sheet to a single-page PDF file via QPdfWriter.

            Uses the same viewport pattern as _export_sheet_png:
              - tmp_vp is fitted to (cw, ch) — canvas widget pixel dimensions —
                so _paint()'s in_view() culling is consistent with the viewport.
              - painter.scale(scale) maps canvas pixels to PDF page pixels.
              - Background is filled explicitly; the Ulticap sheet border is
                suppressed (export_mode=True) and replaced by a tight border
                rectangle drawn at tight_bounds + GAP.
            """
            from PySide6.QtGui import QPageSize, QPageLayout
            from PySide6.QtCore import QSizeF

            if export_vis is None:
                export_vis = load_export_png_layers()

            # Pre-flight writability check with plain Python I/O, *before*
            # QPdfWriter/QPainter are ever constructed. If the target file is
            # locked (e.g. open in a PDF viewer), Qt's own C++ layer prints
            # 'QPainter::begin(): Returned false' the instant begin() fails —
            # that message comes from Qt itself, synchronously, so checking
            # painter.isActive() afterwards is too late to prevent it. Failing
            # here instead, with ordinary file I/O, avoids ever calling
            # QPainter on a doomed writer, so the message never fires.
            try:
                with open(out_path, 'ab'):
                    pass
            except OSError:
                raise RuntimeError(
                    f"Could not open '{out_path.name}' for writing — it may "
                    f"be open in another program (e.g. a PDF viewer). "
                    f"Close it and try again.")

            # ── content bounds + border clearance ──────────────────────────────
            _tb = _tight_bounds_with_gap(sheet, _TIGHT_BOUNDS_GAP_UNITS)
            if _tb:
                brx0, bry0, brx1, bry1 = _tb
            else:
                brx0, bry0 = sheet.xmin, sheet.ymin
                brx1, bry1 = sheet.xmax, sheet.ymax
            # Viewport = border rect exactly; the 2mm page margin (set on the
            # QPdfWriter below) provides the gap between border rect and page edge.
            vp_w_u = brx1 - brx0
            vp_h_u = bry1 - bry0

            # ── colour overrides (single shared source — see _light_theme_colours) ──
            paint_colours = _light_theme_colours(bw=bw)
            col_bg     = paint_colours['bg']
            col_border = paint_colours['border']

            # ── set up QPdfWriter ──────────────────────────────────────────────
            writer = QPdfWriter(str(out_path))
            writer.setCreator(f'KIUT Viewer {VERSION}')
            writer.setTitle(sheet.name)
            DPI = _PDF_EXPORT_DPI
            writer.setResolution(DPI)

            # 2mm margin on all page sizes — provides a consistent small gap
            # between the schematic border rectangle and the PDF page edge.
            # PDF viewers add their own printer margin on top when printing.
            PAGE_MARGIN_MM = _PDF_PAGE_MARGIN_MM

            if fit_a4:
                if vp_w_u > vp_h_u:
                    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
                    writer.setPageOrientation(QPageLayout.Orientation.Landscape)
                else:
                    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
                    writer.setPageOrientation(QPageLayout.Orientation.Portrait)
                writer.setPageMargins(QMarginsF(PAGE_MARGIN_MM, PAGE_MARGIN_MM,
                                                PAGE_MARGIN_MM, PAGE_MARGIN_MM),
                                      QPageLayout.Unit.Millimeter)
            else:
                # Natural size: page = border rect + 2mm margin on each side.
                w_mm = vp_w_u * 2 / 1000.0 * 25.4 + 2 * PAGE_MARGIN_MM
                h_mm = vp_h_u * 2 / 1000.0 * 25.4 + 2 * PAGE_MARGIN_MM
                writer.setPageSize(QPageSize(QSizeF(w_mm, h_mm),
                                             QPageSize.Unit.Millimeter))
                writer.setPageMargins(QMarginsF(PAGE_MARGIN_MM, PAGE_MARGIN_MM,
                                                PAGE_MARGIN_MM, PAGE_MARGIN_MM),
                                      QPageLayout.Unit.Millimeter)

            pr = writer.pageLayout().paintRectPixels(DPI)
            pdf_w, pdf_h = pr.width(), pr.height()

            # Virtual canvas = PDF paintable area (inside the 2mm margins).
            # Both A4 and natural size fit the viewport to the border rect
            # directly — the 2mm page margin provides the gap to the page edge.
            vcw = pdf_w
            vch = pdf_h

            tmp_vp = _VP()
            tmp_vp.fit(brx0, bry0, brx1, bry1, vcw, vch, margin=0)

            # ── build export layer vis ─────────────────────────────────────────
            pdf_vis = dict(export_vis)
            for k in ('grid', 'anc_comp', 'anc_pin_name', 'anc_pin_number',
                      'anc_attr', 'anc_wire', 'anc_pin',
                      'anc_ann', 'poe_comp', 'poe_ann'):
                pdf_vis[k] = False

            # ── swap canvas state, paint, restore ──────────────────────────────
            old_sheet = self._canvas.sheet
            old_vp    = self._canvas._vp
            old_vis   = self._canvas._vis
            old_text_scale = getattr(self._canvas, '_text_scale', _TEXT_SCALE_DEFAULT)
            self._canvas.sheet = sheet
            self._canvas._vp   = tmp_vp
            self._canvas._vis  = pdf_vis
            # No override needed: setPixelSize() (see _draw_text) makes
            # font rendering DPI-independent, so PDF now matches on-screen
            # sizing at the same _text_scale (_TEXT_SCALE_DEFAULT) without a
            # separate fudge factor. Kept as an explicit override point
            # in case further visual tuning is needed.
            self._canvas._text_scale = _TEXT_SCALE_DEFAULT
            try:
                painter = QPainter(writer)
                if not painter.isActive():
                    raise RuntimeError(
                        f"Could not open '{out_path.name}' for writing — it may "
                        f"be open in another program (e.g. a PDF viewer). "
                        f"Close it and try again.")
                painter.setRenderHint(QPainter.Antialiasing)
                # No painter.scale(): tmp_vp already maps world units to device
                # pixels at the correct density.  Only centre on the longer axis.
                tx = (pdf_w - vcw) / 2
                ty = (pdf_h - vch) / 2
                if tx or ty:
                    painter.translate(tx, ty)
                # Background fill (paintEvent is bypassed during export)
                painter.fillRect(QRectF(0, 0, vcw, vch), col_bg)
                # Border rectangle at tight_bounds + GAP via VP transform
                painter.setPen(QPen(col_border, 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(
                    tmp_vp.cx(brx0), tmp_vp.cy(bry1),
                    tmp_vp.cs(brx1 - brx0), tmp_vp.cs(bry1 - bry0)))
                self._canvas._paint(painter, export_mode=True, force_mk=True,
                                    colour_overrides=paint_colours,
                                    canvas_size=(vcw, vch))
                painter.end()
            finally:
                self._canvas.sheet = old_sheet
                self._canvas._vp   = old_vp
                self._canvas._vis  = old_vis
                self._canvas._vis  = old_vis
                self._canvas._text_scale = old_text_scale

        def _save_pdf(self):
            if not self._canvas.sheet:
                QMessageBox.information(self, 'Export', 'No schematic loaded.')
                return
            scope, out, fit_a4, bw = self._ask_pdf_export_options()
            if not scope:
                return
            export_vis = load_export_png_layers()

            if scope == 'current':
                if not out:
                    return
                try:
                    self._export_sheet_pdf(
                        self._canvas.sheet, Path(out),
                        fit_a4=fit_a4, bw=bw, export_vis=export_vis)
                    QMessageBox.information(self, 'Export',
                                            f'Saved: {Path(out).name}')
                except Exception as e:
                    QMessageBox.critical(self, 'Export failed', str(e))
            else:
                # All sheets — one PDF file per sheet, named after each SCH
                # file (a .UTSCH's sheets as the converter names them)
                sheets = self._export_sheets()
                total = len(sheets)
                saved, errors = [], []
                dlg = QProgressDialog('Preparing export…', 'Cancel', 0, total, self)
                dlg.setWindowTitle('Exporting PDF')
                dlg.setWindowModality(Qt.WindowModality.WindowModal)
                dlg.setMinimumDuration(0)
                dlg.setValue(0)
                for i, (label, base, load) in enumerate(sheets):
                    if dlg.wasCanceled():
                        break
                    dlg.setLabelText(
                        f'Exporting {label}  ({i + 1} / {total})\n'
                        f'Cancel takes effect after the current sheet finishes.')
                    dlg.setValue(i)
                    QApplication.processEvents()
                    try:
                        sh = load()
                        if sh:
                            pdf_out = base.with_name(base.name + '.pdf')
                            self._export_sheet_pdf(
                                sh, pdf_out,
                                fit_a4=fit_a4, bw=bw, export_vis=export_vis)
                            saved.append(pdf_out.name)
                    except Exception as e:
                        errors.append(f'{label}: {e}')
                dlg.setValue(total)
                cancelled = dlg.wasCanceled()
                dlg.close()
                if cancelled and not saved:
                    return
                msg = (f'Saved {len(saved)} of {total} file(s) to '
                       f'{self._root_path.parent}')
                if cancelled:
                    msg += '\n\nExport was cancelled — remaining sheets were skipped.'
                if errors:
                    msg += '\n\nErrors:\n' + '\n'.join(errors)
                QMessageBox.information(self, 'Export', msg)

    def _font_notice():
        """Tell the user once that texts are drawn in a stand-in font because
        ISOCTEUR is missing -- Qt substitutes silently, so without this the
        different look would go unexplained. Opened, not exec'd: it must
        never hold up the window behind it."""
        box = QMessageBox(QMessageBox.Information, 'KIUT Viewer — text font',
                          _FONT_MISSING_TEXT, QMessageBox.Ok, win)
        # Held in a variable the handler closes over: passed inline, its
        # Python wrapper could be collected and the box left pointing at a
        # deleted widget (a crash on closing, seen in testing).
        dont_show = QCheckBox('Do not show this message again')
        box.setCheckBox(dont_show)
        box.finished.connect(lambda _r: hide_notice(_NOTICE_FONT_MISSING)
                             if dont_show.isChecked() else None)
        box.setWindowModality(Qt.WindowModal)
        win._font_notice = box          # keeps it alive while open
        box.open()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow(sheet, initial_dir, initial_path, initial_key)
    if handoff:
        win.start_handoff(handoff)
    win.show()
    win._update_read_messages()
    families = {f.lower() for f in QFontDatabase.families()}
    if _TEXT_FONT_FAMILY.lower() not in families and not notice_hidden(_NOTICE_FONT_MISSING):
        QTimer.singleShot(0, _font_notice)   # after the window is on screen
    app.exec()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description='Ulticap .SCH / .UTSCH viewer')
    ap.add_argument('input', nargs='?',
                    help='.SCH, .BLK or .UTSCH file (optional — use File ▸ Open if omitted)')
    ap.add_argument('--channel', help=argparse.SUPPRESS)    # given by the KIUT window: its own viewer
    args = ap.parse_args()

    sheet = None
    initial_path: Optional[Path] = None
    initial_key: Optional[str] = None
    initial_dir = Path.cwd()
    if args.input:
        path = Path(args.input)
        if not path.exists():             ap.error(f'File not found: {path}')
        if path.suffix.upper() not in ('.SCH', '.BLK', '.UTSCH'):
            ap.error('Only .SCH, .BLK and .UTSCH files are supported')
        try:
            sheet, initial_key = _load_design_sheet(path)
        except Exception as e:
            ap.error(f'Could not read {path.name}: {e}')
        initial_path = path.resolve()
        initial_dir = initial_path.parent

    try:
        from kiut.core import handoff
        _run_qt(sheet, initial_dir, initial_path, initial_key,
                handoff=args.channel or handoff.new_viewer_id())
    except ImportError:
        print('─' * 60)
        print('PySide6 is required but not installed.')
        print('Install it with:')
        print('    pip install PySide6')
        print('─' * 60)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
