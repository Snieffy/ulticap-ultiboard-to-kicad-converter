# kiub_v2v3.py  –  Ultiboard DDF V2/V3 → V4.60 pre-processor for KIUB
# Copyright (C) 2026  Snieffy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://gnu.org>.

"""
kiub_v2v3: Ultiboard DDF V2/V3 → V4.60 pre-processor.

Primary API (used by kiub.py):
    converted_str = convert_str(source_str)   # str → str

Standalone CLI:
    python kiub_v2v3.py input[.DDF] [output_V4.DDF]

    - The .DDF extension is added automatically if omitted.
    - If only the input filename is given, the output filename is derived
      automatically by appending _V4 before the extension:
          mon330.DDF  →  mon330_V4.DDF
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROT_MAP = {
    0: 0, 1: 17280, 2: 11520, 3: 5760,
    4: -23040, 5: -5760, 6: -11520, 7: -17280
}

# ---------------------------------------------------------------------------
# Fine-tunable text-geometry estimation ratios (see kiub.py's FINE_TUNING_SPEC,
# entries 'v2v3_text_width_ratio' / 'v2v3_text_thickness_ratio'). Unlike V4/V5,
# V2/V3 DDFs only store a text *height* per shape/alias/component-text record
# -- width and stroke thickness aren't stored at all, so both are estimated
# from height using these empirically-fitted ratios. When this module is
# loaded by kiub.py's open_ddf(), these two module attributes may be
# overridden per-conversion from a CLI flag or the GUI's "Fine-tuning…"
# dialog; running this file standalone always uses the defaults below.
TEXT_WIDTH_RATIO: float = 0.8       # estimated text width = height * this
TEXT_THICKNESS_RATIO: float = 0.1667  # estimated text stroke thickness = height * this

# Absolute hard ceiling (in mil) for a single V2/V3 staircase grid step
# -- see DDFConverter._is_pairable(). An ordinary right-angle routing
# corner can coincidentally have two equal-length legs; without a cap,
# that's geometrically indistinguishable from a genuine one-step
# staircase and gets merged into a diagonal it was never meant to be,
# which can cut through nearby copper (pads, other traces) that the
# original 90-degree corner safely cleared. A real Ultiboard-drawn
# diagonal staircase only ever uses small, deliberate grid steps, so
# capping the length filters this out. Empirically derived: a genuine
# 100 mil right-angle corner with coincidentally equal legs was
# observed getting misidentified as a staircase before this cap
# existed, so 100 mil is already inside ambiguous territory rather
# than a safe ceiling -- 25 mil was chosen as a broadly reasonable
# value clear of that, with anything a genuine staircase might still
# need beyond it left to the chamfer feature instead (see
# CORNER_SLANT_LIMIT_MIL below).
#
# This ceiling is enforced unconditionally -- effective_staircase_limit_
# units() applies it to *every* source of the effective limit, including
# an explicit override from a CLI flag or the GUI's staircase-conversion
# prompt, not just the automatic default. A user correction can only
# ever move the effective limit down from this ceiling, never past it.
STAIRCASE_CEILING_MIL: float = 25.0

# Below the ceiling, the effective limit is further constrained by the
# DDF header's own declared default grid step -- Ultiboard draws a
# staircase along the routing grid by default, and on a densely
# populated board (SMD components, small pads, tight trace/pad
# clearance) that grid step is itself already sized to the board's own
# clearance requirements: a staircase spanning several grid steps
# between two SMD pads is exactly the geometry that, if merged into one
# long diagonal, cuts across the gap DRC needs kept clear. So a small
# declared grid genuinely means a small safe staircase-step ceiling,
# board-density-dependent in a way the fixed ceiling above can't be on
# its own -- it only ever *tightens* the ceiling, never loosens it past
# STAIRCASE_CEILING_MIL. This relies on the header's declared grid being
# correct for the whole file, which is only true if the user never
# changed Ultiboard's own routing grid mid-design without it being
# reflected in the header -- outside this tool's control to verify, so
# the GUI's staircase-conversion prompt surfaces the declared value and
# lets the user correct it before conversion if they know otherwise.
STAIRCASE_LIMIT_MIL: float | None = None
STAIRCASE_LIMIT_EXPLICIT: bool = False  # True once a caller has set an
                                         # explicit value above (e.g. a
                                         # user's own grid correction),
                                         # disabling the header's own
                                         # declared-grid value -- still
                                         # subject to STAIRCASE_CEILING_MIL
                                         # regardless.
# A value of 0 (with STAIRCASE_LIMIT_EXPLICIT True) disables staircase
# merging entirely: every *LH/*LV run is emitted unmerged, exactly as
# kiub_v2v3 did before this feature existed.

CORNER_SLANT_LIMIT_MIL: float | None = None
CORNER_SLANT_LIMIT_EXPLICIT: bool = False  # True once a caller has set an
                                            # explicit value above, disabling
                                            # the "track the staircase limit"
                                            # default. A value of 0 (with
                                            # this flag True) disables
                                            # corner slanting entirely.

# Whatever the limit above resolves to, each corner's trim is further
# capped by its own trace code's width w and clearance c (the *TT record),
# so that the chamfer never brings the trace closer to anything than the
# clearance the corner already kept (DDFConverter.clearance_safe_trim):
#
#     trim <= (2 - sqrt 2) * c + (1 - sqrt 2 / 2) * w   (~0.586 c + 0.293 w)
#
# The worst case is copper of another net tucked into the inside of the
# bend at exactly c from both legs (a pad or via the trace was routed
# around): the chamfer's diagonal cuts towards it, and with this trim it
# ends up at exactly c. Anything farther away stays farther away, and the
# outside of the bend only loses copper. No neighbouring copper is looked
# up: the bound follows from the corner's own width and clearance alone.
# A 25 mil trim, the earlier cap, brought a 12.5 mil trace bent around a
# pad from 13.7 to 4.4 mil of it against a 9.2 mil clearance; this rule
# trims about 9 mil there. It also keeps a corner at a small pad's centre
# connected: the trace end stays within the trim of the centre.

DB_UNITS_PER_MIL: float = 1.2  # 1 mil = 1/1000 inch; 1200 database units = 1 inch


def nums(line):
    return list(map(int, re.findall(r'-?\d+', line)))


class DDFConverter:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0
        self.out = []

        self.shapes = []
        self.board_w = 0
        self.board_h = 0

        # via centers by net, populated by prescan_vias() before the main
        # conversion pass runs -- needed because *V records appear after
        # *LH/*LV in file order, so via locations aren't known yet when
        # traces are processed in a single left-to-right pass.
        self.via_points_by_net = {}
        self.via_pad_diameter = {}   # pad_code -> [inner, front, back] diameter, native db units

        # trace code -> (width, clearance), native db units, from the *TT
        # records (handle_T): caps each corner's chamfer trim, see
        # clearance_safe_trim.
        self.trace_codes = {}
        self.corners_chamfered = 0   # counted by _apply_corner_slant

        self.declared_grid_units = None  # from the *P header, set by handle_P
        self._staircase_limit_resolved = False   # True once resolved (below)
        self._staircase_limit_units_cache = None  # the resolved limit, or
                                                    # None if merging is disabled

        # Populated by _find_open_chain_runs(): the per-edge length
        # (database units) of every genuinely multi-step run found (2+
        # consecutive matching-length H/V pairs) -- not single-pair runs,
        # since a single pair is exactly the case that's structurally
        # ambiguous between a genuine staircase step and an ordinary
        # corner with coincidentally equal legs (see _is_pairable's own
        # docstring), so it's not a trustworthy contributor to "what
        # step length do this file's real staircases actually use".
        # Used by detect_staircases() to warn (never to silently change
        # anything) when this disagrees with the declared grid.
        self.multi_step_run_lengths = []

        self.t_store = {"TD": [], "T0": [], "T1": [], "T2": []}

    # =========================================================
    def convert(self):
        self.prescan_vias()
        self.srecord_start = False
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()

            if line.startswith("*P"):
                self.handle_P()

            elif line.startswith("*T"):
                self.handle_T()

            elif line.startswith("*S"):
                if not self.srecord_start:
                    self.emit_TS_and_SBOARD()
                    self.srecord_start = True
                self.handle_S()

            elif line.startswith("*N"):
                self.copy_block()

            elif line.startswith("*C"):
                self.handle_C()

            elif line.startswith("*LH") or line.startswith("*LV"):
                self.handle_LH_LV()

            elif line.startswith("*V"):
                self.handle_V()

            elif line.startswith("*X"):
                self.handle_X()

            else:
                self.i += 1

        return "\n".join(self.out)

    # =========================================================
    # Via pre-scan: populates self.via_points_by_net from every *V
    # record in the file, before the main conversion pass begins. This
    # is used later by the *LH/*LV staircase merge to make sure a
    # diagonal is never drawn through a point where a via actually sits
    # (which would disconnect it) -- see _split_at_via_waypoints().
    # Empirically measured (across a range of via pad sizes from 2 mil to
    # 100 mil): Ultiboard's autorouter can nudge a via's center off its
    # ideal position, toward one of four diagonal quadrants, by a
    # distance -- identical along both axes -- that scales with the
    # via's own pad (copper) diameter, up to a fixed ceiling:
    #   per_axis_shift_mil = min(VIA_SHIFT_COEFF * diameter_mil, VIA_SHIFT_CAP_MIL)
    # Not drill-size or trace-width related -- purely a function of the
    # via's own pad size. VIA_SHIFT_CAP_MIL is applied for any via whose
    # pad diameter is roughly 33 mil or larger; below that the shift
    # scales down linearly, dropping to well under half the ceiling for
    # small vias.
    VIA_SHIFT_COEFF   = 0.35
    VIA_SHIFT_CAP_MIL = 11.67

    def prescan_vias(self):
        """Populate self.via_points_by_net with each via's position and
        pad code -- entries are (x, y, pad_code), not a pre-baked
        radius -- because *T0/*T1/*T2 (Inner/Front/Back) can each
        declare a genuinely different pad diameter for the same code,
        and which one is relevant depends on which layer is actually
        being checked, known only later at the call site (see
        via_radius_for_layer). Baking in a single radius here (e.g. the
        largest of the three) would over-protect whichever layer has
        the smaller pad.

        self.via_pad_diameter[pad_code] holds a 3-tuple
        (inner, front, back) in native database units, straight from
        each *T0/*T1/*T2 record's own `y` field (see FILEFORMAT_ULTIBOARD_DDF.md's
        *T0/*T1/*T2 section for why that field, and not *TD -- drill
        diameter, a different quantity that doesn't correlate with the
        observed nudge at all). These records reliably appear near the
        top of the file, well before any *V record that could reference
        them, so a single linear pass collecting both is safe.
        """
        i = 0
        while i < len(self.lines):
            line = self.lines[i].strip()
            if line.startswith(("*T0 ", "*T1 ", "*T2 ")):
                try:
                    sub    = int(line[2])  # 0/1/2 from "*T0"/"*T1"/"*T2"
                    prefix, rest = line.split(",", 1)
                    idx = int(prefix.split()[1])
                    y   = float(rest.split(",")[2])
                except (ValueError, IndexError):
                    i += 1
                    continue
                entry = self.via_pad_diameter.setdefault(idx, [0.0, 0.0, 0.0])
                entry[sub] = y
                i += 1
            elif line.startswith("*V"):
                x = int(line[2:].strip())
                i += 1
                while True:
                    raw      = self.lines[i].strip()
                    is_last  = raw.endswith(";")
                    body     = raw[:-1] if is_last else raw
                    parts    = body.split()
                    y, net   = int(parts[0]), int(parts[1])
                    pad_code = int(parts[2]) if len(parts) >= 3 else None
                    self.via_points_by_net.setdefault(net, set()).add((x, y, pad_code))
                    i += 1
                    if is_last:
                        break
            else:
                i += 1

    def _via_radius_for_layer(self, pad_code, layer):
        """The search radius (database units, Euclidean) for this pad
        code's via nudge on this specific DDF layer.

        Layer-to-sub-table mapping: DDF layer 1 is always Front, DDF
        layer 2 is always Back, and any other layer number is Inner --
        confirmed against kiub.py's own layersCu table, whose first two
        list positions (used directly as bit-compress indices via
        layer_from_bit) are F.Cu and B.Cu regardless of the board's
        total layer count, with every inner layer coming after. Reusing
        that same convention here (rather than re-deriving it) is what
        keeps this in sync with how kiub.py itself will interpret the
        very same layer number later.
        """
        inner, front, back = self.via_pad_diameter.get(pad_code, (0.0, 0.0, 0.0))
        diameter_units = front if layer == 1 else back if layer == 2 else inner
        diameter_mil = diameter_units / DB_UNITS_PER_MIL
        shift_mil = min(self.VIA_SHIFT_COEFF * diameter_mil, self.VIA_SHIFT_CAP_MIL)
        return shift_mil * DB_UNITS_PER_MIL * (2 ** 0.5)

    def _point_near_via(self, point, via_points, layer):
        """True if `point` falls within any via's *own* nudge-search
        radius for this specific layer -- i.e. genuinely close enough
        to that via's real connection point on THIS layer, not just
        "nearby" by some tolerance shared across every via and layer.

        Each entry in `via_points` is (x, y, pad_code), populated by
        prescan_vias(); the radius itself is computed per-via, per-call
        via _via_radius_for_layer(pad_code, layer) rather than being
        baked in ahead of time, since *T0/*T1/*T2 can each declare a
        genuinely different pad diameter for the same code -- using one
        blanket value (e.g. the largest of the three) would
        over-protect whichever layer actually has the smaller pad. A
        via whose resolved radius is <= 0 (its pad code had no matching
        diameter for this layer, an unexpected but possible
        malformed-file case) is skipped rather than treated as having
        infinite or zero-tolerance reach either way.
        """
        for vx, vy, pad_code in via_points:
            radius = self._via_radius_for_layer(pad_code, layer)
            if radius <= 0:
                continue
            if (point[0] - vx) ** 2 + (point[1] - vy) ** 2 <= radius * radius:
                return True
        return False

    # =========================================================
    # *P
    def handle_P(self):
        header = self.lines[self.i].strip()
        self.i += 1

        self.i += 1  # skip version line

        dims = self.lines[self.i].strip()
        self.i += 1

        n = nums(dims)
        self.board_w, self.board_h = n[0], n[1]
        # The 3rd field is the default grid step (database units). It is
        # NOT used to detect individual *LH/*LV staircase edges -- see
        # _is_pairable() -- since Ultiboard lets the user change the
        # routing grid at any time and it doesn't reliably reflect the
        # step size actually used for any given trace. It's kept here
        # only as a candidate for effective_staircase_limit_mil()'s
        # auto-adjustment, which cross-checks it against how many edges
        # actually share that exact length before trusting it.
        self.declared_grid_units = n[2] if len(n) > 2 else None

        self.out.append(header)
        self.out.append("4 60")
        self.out.append(f"{self.board_w}, {self.board_h}, 0, 0, 6, 0, 22;")
        self.out.append("(|+|+|+|+|+|+|+|+|+|+|)")
        self.out.append("0, 0")
        self.out.append("240 0 0 15 30 1")
        self.out.append(" ".join(["1 2"] * 16))

        for _ in range(5):
            self.out.append("65535 65535 65535 65535 65535 65535")
        self.out.append("65535 65535")

    # =========================================================
    # *T
    def handle_T(self):
        block = []

        # -----------------------------
        # COLLECT *T* BLOCK
        # -----------------------------
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()

            if not line.startswith("*T"):
                break

            block.append(line)
            self.i += 1

        # -----------------------------
        # PROCESS INNER RECORDS
        # -----------------------------
        td_block = []
        td_dups  = []

        t_blocks = {"T0": [], "T1": [], "T2": []}
        t_dups   = {"T0": [], "T1": [], "T2": []}

        for line in block:
            rec = line.split()[0][1:]

            # -------------------------
            # PASS-THROUGH
            # -------------------------
            if rec == "TT":
                vals = [int(v) for v in re.findall(r'-?\d+', line)[:3]]
                if len(vals) == 3:
                    self.trace_codes[vals[0]] = (vals[1], vals[2])
            if rec in ("TP", "TT", "TC"):
                if line.startswith("*TP"):
                    line = '*TP ffffffff'
                if line.startswith("*TC"):
                    for r in range(16, 32):
                        self.out.append(f"*TT {r}, 0, 30")
                self.out.append(line)
                continue

            # -------------------------
            # TD BLOCK
            # -------------------------
            if rec == "TD":
                vals = re.findall(r'-?\d+', line)
                if len(vals) >= 2:
                    idx     = int(line.split(",", 1)[0].split()[1])
                    val     = int(vals[1])
                    new_val = int(val * 1200 / 254)
                    new_line = f"*TD {idx}, {new_val}"
                else:
                    new_line = line

                td_block.append(new_line)

                # prepare duplicate (0–15 → 240–255)
                if len(vals) >= 2 and idx < 16:
                    td_dups.append(f"*TD {idx + 240}, {new_val}")

                continue

            # -------------------------
            # T0 / T1 / T2 BLOCKS
            # -------------------------
            if rec in ("T0", "T1", "T2"):

                try:
                    prefix, rest = line.split(",", 1)
                    idx = int(prefix.split()[1])
                except (ValueError, IndexError):
                    t_blocks[rec].append(line)
                    continue

                vals = [v.strip() for v in rest.split(",")]

                if len(vals) < 9:  # original format has fewer fields
                    rest = rest.rstrip() + ", 0, 0, 0, 0"

                new_line = prefix + "," + rest
                t_blocks[rec].append(new_line)

                # DUPLICATE 0–15 → 240–255
                if idx < 16:
                    prefix_parts    = prefix.split()
                    prefix_parts[1] = str(idx + 240)
                    t_dups[rec].append(" ".join(prefix_parts) + "," + rest)

                continue

        # -------------------------
        # EMIT GROUPED BLOCKS
        # -------------------------
        self.out.extend(td_block)
        self.out.extend(td_dups)

        for rec in ("T0", "T1", "T2"):
            self.out.extend(t_blocks[rec])
            self.out.extend(t_dups[rec])

    # =========================================================
    # *S
    def handle_S(self):
        header = self.lines[self.i].strip()

        # --------------------------------------------------
        # IGNORE SBOARD (NOT indexed)
        if header.startswith("*SBOARD"):
            self.copy_block()
            return

        # --------------------------------------------------
        # HANDLE SNO_SHP
        if header.startswith("*SNO_SHP"):
            self.i += 1
            while self.i < len(self.lines):
                if self.lines[self.i].startswith("*"):
                    break
                self.i += 1

            self.shapes.append({"name": "*SNO_SHP", "X": 30, "Y": -150, "Height": 100, "Rot": 0})

            self.out.extend([
                "*SNO_SHP",
                "30 -150 100 0 80 100",
                "30 -300 100 0 80 100",
                "0.000000",
                "61,180,60,450,-59,480,-60,180;",
                "1,17280,ffffffff,0,0,1;",
                "0,0,60,0,23040,",
                "0,180,60,11520,11520,",
                "-300,780,60,11520,11520,",
                "150,420,95,6940,3399,",
                "-61,751,301,19639,14553,",
                "59,484,120,6658,4862,",
                "-64,756,182,19262,14691;",
                ";"
            ])
            return

        # --------------------------------------------------
        # NORMAL SHAPE
        self.out.append(header)
        self.i += 1

        meta = self.lines[self.i].strip()
        self.i += 1

        n   = nums(meta)
        h   = n[2]
        rot = ROT_MAP.get(n[3], 0)

        self.shapes.append({"name": header[2:], "X": n[0], "Y": n[1], "Height": h, "Rot": rot})

        self.out.append(f"{n[0]} {n[1]} {h} {rot} {h} 100")
        self.out.append(f"{n[0]} {n[1]} {h} {rot} {h} 100")
        self.out.append("0.000000")

        # --------------------------------------------------
        # COLLECT UNTIL NEXT '*'
        collected = []
        while self.i < len(self.lines):
            line = self.lines[self.i].strip()
            if line.startswith("*"):
                break
            collected.append(line)
            self.i += 1

        # --------------------------------------------------
        # SPLIT INTO OUTLINE / PADS
        outline = []
        pads    = []
        mode    = "outline"

        for line in collected:
            if line == ";":
                mode = "pads"
                continue

            if mode == "outline":
                outline.append(line)
                if line.endswith(";"):
                    # if text x,y = 0,0: center text in shape outline
                    if self.shapes[-1]['X'] == 0 and self.shapes[-1]['Y'] == 0:
                        shape_center = [
                            (lambda v: (min(v) + max(v)) // 2)(
                                [int(n) for n in outline[0].strip(';').split(',')][i::2]
                            )
                            for i in (0, 1)
                        ]
                        self.shapes[-1]['X'] = shape_center[0]
                        self.shapes[-1]['Y'] = shape_center[1]
                    mode = "pads"
            else:
                pads.append(line)

        # --------------------------------------------------
        # OUTLINE (ensure exactly one ';')
        if outline:
            for l in outline[:-1]:
                self.out.append(l.rstrip(","))
            last = outline[-1].rstrip(",")
            if not last.endswith(";"):
                last += ";"
            self.out.append(last)
        else:
            self.out.append(";")

        # --------------------------------------------------
        # PADS
        pin = 1

        if pads:
            for i, l in enumerate(pads):
                line    = l.strip()
                if not line:
                    continue
                is_last = (i == len(pads) - 1)
                if line.endswith(";"):
                    line = line[:-1]
                # mapping V2/V3 to V4: rotation and pad layerset
                parts    = line.split(',')
                parts[1] = str(ROT_MAP[int(parts[1])])
                parts[2] = f"{(int(parts[2], 16) >> 12):08x}"
                line     = ",".join(parts)

                if is_last:
                    if not line.endswith(","):
                        line += ","
                    self.out.append(f"{line}{pin};")
                else:
                    self.out.append(f"{line}{pin},")

                pin += 1
        else:
            self.out.append(";")

        self.out.append(";\n;")

    # =========================================================
    def handle_C(self):
        header = self.lines[self.i].strip()
        self.i += 1

        data = self.lines[self.i].strip()
        self.i += 1

        n        = nums(data)
        shape_id = n[0]
        x, y     = n[1], n[2]
        rot      = ROT_MAP.get(n[3], 0)

        # Signed integer correction for the reference x,y values
        n_x        = (n[4] if n[4] <= 32768 else n[4] - 65536) + self.shapes[shape_id]['X']
        n_y        = (n[5] if n[5] <= 32768 else n[5] - 65536) + self.shapes[shape_id]['Y']
        n_h        = self.shapes[shape_id]['Height']
        n_w        = int(n_h * TEXT_WIDTH_RATIO)
        n_t        = int(n_h * TEXT_THICKNESS_RATIO)
        n_rot      = ROT_MAP.get(self.shapes[shape_id]['Rot'], 0)
        shape_name = self.shapes[shape_id]['name']

        # --------------------------------------------------
        # Rewrite header
        self.out.append(header + " " + shape_name)
        # Replace data line (REMOVE shape_id)
        # Position line: <x>,<y>,<rotation>,<name_x>,<name_y>,<name_rot>,<name_w>,<name_h>,<name_thick>,
        #                <alias_x>,<alias_y>,<alias_rot>,<alias_w>,<alias_h>,<alias_thick>
        self.out.append(
            f"{x},{y},{rot},{n_x},{n_y},{n_rot},{n_w},{n_h},{n_t},"
            f"{n_x},{n_y},{n_rot},{n_w},{n_h},{n_t}"
        )
        self.out.append("0,0,0,0,0,0,0")

        # --------------------------------------------------
        # Read net lines until NEXT RECORD (line starting with '*')
        net_lines = []
        while self.i < len(self.lines):
            line = self.lines[self.i].rstrip("\n")
            if line.startswith("*"):
                break
            # pad layerset mapping V2/V3 to V4
            line = ' '.join(
                f"{(int(v, 16) >> 12):08x}" if i % 2 == 1 else v
                for i, v in enumerate(line.split())
            )
            net_lines.append(line.strip())
            self.i += 1

        net_lines[-1] += "\n;"
        self.out.extend(net_lines)

    # =========================================================
    # *LH / *LV
    #
    # V2/V3 has no native record for 45-degree traces: unlike V4/V5's *LT
    # (which supports orientation 4/8 = NE/SE diagonal directly), *LH/*LV
    # can only express strictly horizontal or vertical segments. A
    # diagonal trace therefore gets rasterised into a staircase of tiny
    # H/V steps. The DDF header's declared default grid step is NOT a
    # reliable indicator of the step size used, though: Ultiboard lets
    # the user change the routing grid at any time, so different traces
    # (and even different runs within the same file) can be rasterised
    # at different step sizes. Staircase detection below is therefore
    # purely geometric -- see _is_pairable() -- and never consults the
    # header's grid value.
    #
    # Ultiboard's own plot output reconstructs the true diagonal from
    # this staircase; the logic below does the same thing here, so the
    # V4 intermediate file (and everything downstream of it) gets a
    # single diagonal *LT segment instead of dozens of tiny H/V ones.
    def handle_LH_LV(self):
        edges = []  # buffered across the whole contiguous *LH/*LV run

        while self.i < len(self.lines):
            line = self.lines[self.i].strip()
            if not (line.startswith("*LH") or line.startswith("*LV")):
                break

            header = line
            is_LH  = header.startswith("*LH")
            hparts = header[3:].split()
            layer, coord1 = int(hparts[0]), int(hparts[1])
            self.i += 1

            while True:
                raw     = self.lines[self.i].strip()
                is_last = raw.endswith(";")
                body    = raw[:-1] if is_last else raw
                parts   = body.split()

                c0, c1, net = int(parts[0]), int(parts[1]), int(parts[2])
                tcode_raw   = parts[3]
                # trailing letter (e.g. "1F") is dropped, matching the
                # original behaviour -- trace_type is always emitted as 0
                flag  = tcode_raw[-1] if tcode_raw[-1].isalpha() else ""
                tcode = int(tcode_raw[:-1] if flag else tcode_raw)

                if is_LH:
                    p1, p2 = (c0, coord1), (c1, coord1)
                else:
                    p1, p2 = (coord1, c0), (coord1, c1)

                edges.append({
                    "p1": p1, "p2": p2, "layer": layer, "net": net,
                    "tcode": tcode, "flag": flag,
                    "orient": "H" if is_LH else "V",
                    "length": abs(c1 - c0),
                })

                self.i += 1
                if is_last:
                    break

        self._emit_LH_LV_edges(edges)

    @staticmethod
    def _sign(v):
        return 1 if v > 0 else (-1 if v < 0 else 0)

    def _build_LH_LV_components(self, group_edges, junctions=frozenset()):
        """Group edges (already filtered to one layer/net/tcode/flag key)
        into connected components via shared endpoints. A component is
        not continued through a point in `junctions` (where a trace of
        another trace code meets this one): each side becomes a chain
        of its own that ends there, so the point is a true endpoint that
        no staircase merge or corner chamfer moves."""
        adjacency = {}
        for idx, e in enumerate(group_edges):
            adjacency.setdefault(e["p1"], []).append((idx, e["p2"]))
            adjacency.setdefault(e["p2"], []).append((idx, e["p1"]))

        visited = [False] * len(group_edges)
        components = []
        for start_idx in range(len(group_edges)):
            if visited[start_idx]:
                continue
            stack, comp = [start_idx], set()
            while stack:
                idx = stack.pop()
                if idx in comp:
                    continue
                comp.add(idx)
                visited[idx] = True
                e = group_edges[idx]
                for pt in (e["p1"], e["p2"]):
                    if pt in junctions:
                        continue
                    for j, _ in adjacency[pt]:
                        if j not in comp:
                            stack.append(j)
            components.append(comp)
        return adjacency, components

    def _order_LH_LV_component(self, group_edges, comp_edge_idxs):
        """Walk one connected component into an ordered edge sequence.

        Returns (ordered, is_loop) where ordered is a list of
        (edge_idx, from_point, to_point), or (None, None) if the
        component branches (a point with >2 edges) -- callers should
        leave a branching component's edges unmerged, since it isn't a
        simple staircase/polyline.
        """
        point_edges = {}
        for idx in comp_edge_idxs:
            e = group_edges[idx]
            point_edges.setdefault(e["p1"], []).append(idx)
            point_edges.setdefault(e["p2"], []).append(idx)

        if max(len(v) for v in point_edges.values()) > 2:
            return None, None

        endpoints = [p for p, v in point_edges.items() if len(v) == 1]
        is_loop   = not endpoints

        if endpoints:
            start_point = endpoints[0]
        else:
            # Closed loop: start anywhere: _compress_LH_LV_chain's caller
            # rotates the walk to a safe seam afterwards (see
            # _rotate_to_safe_seam), so the exact starting point here
            # doesn't matter.
            first_idx = next(iter(comp_edge_idxs))
            start_point = group_edges[first_idx]["p1"]

        ordered, used, cur = [], set(), start_point
        while True:
            candidates = [idx for idx in point_edges[cur] if idx not in used]
            if not candidates:
                break
            idx = candidates[0]
            e = group_edges[idx]
            other = e["p2"] if e["p1"] == cur else e["p1"]
            ordered.append((idx, cur, other))
            used.add(idx)
            cur = other
            if is_loop and cur == start_point:
                break
            if len(used) == len(comp_edge_idxs):
                break
        return ordered, is_loop

    def _is_pairable(self, e_a, e_b):
        """Two adjacent edges can form one 45-degree step of a staircase
        if they run in different directions (H then V, or V then H),
        have exactly equal length -- regardless of what that length
        actually is -- and that length doesn't exceed the effective
        staircase limit (see effective_staircase_limit_units()). The
        length cap exists because an ordinary right-angle routing
        corner can coincidentally have two equal-length legs, which is
        otherwise geometrically indistinguishable from a genuine
        one-step staircase; a real Ultiboard-drawn diagonal only ever
        uses small, deliberate grid steps, so anything longer is
        assumed to be an ordinary corner instead and left alone. The
        DDF header's declared default grid step is *not* used for the
        equality check itself: Ultiboard lets the user change the
        routing grid at any time, so different traces (and even
        different runs within the same file) can be rasterised at
        different step sizes."""
        if e_a["orient"] == e_b["orient"] or e_a["length"] != e_b["length"]:
            return False
        limit = self.effective_staircase_limit_units()
        return limit is not None and e_a["length"] <= limit

    def effective_staircase_limit_units(self):
        """Resolve (and cache) the maximum edge length, in database
        units, that's still eligible to be treated as a staircase grid
        step. Precedence:
          1. An explicit override (STAIRCASE_LIMIT_EXPLICIT) -- e.g. a
             user's own grid correction -- if set.
          2. Otherwise, the header's declared default grid step
             directly, unconditionally (Ultiboard draws a staircase
             along the routing grid by default, and that step's own
             correctness is the caller's responsibility to verify --
             see STAIRCASE_LIMIT_MIL's module comment).
          3. Otherwise (no declared grid at all), STAIRCASE_CEILING_MIL
             itself.
        Whichever of these applies is then capped at STAIRCASE_CEILING_MIL
        -- always, including the explicit-override case -- so a bad
        correction can only ever tighten the effective limit, never
        loosen it past that ceiling.
        Returns None if merging is disabled (limit resolves to 0).
        """
        if self._staircase_limit_resolved:
            return self._staircase_limit_units_cache

        ceiling_units = STAIRCASE_CEILING_MIL * DB_UNITS_PER_MIL

        if STAIRCASE_LIMIT_EXPLICIT:
            base_units = STAIRCASE_LIMIT_MIL * DB_UNITS_PER_MIL if STAIRCASE_LIMIT_MIL else 0
        elif self.declared_grid_units:
            base_units = self.declared_grid_units
        else:
            base_units = ceiling_units

        units = min(base_units, ceiling_units) if base_units else 0
        self._staircase_limit_units_cache = units if units else None
        self._staircase_limit_resolved = True
        return self._staircase_limit_units_cache

    def effective_corner_slant_limit_units(self):
        """Resolve the maximum length, in database units, to trim off
        each leg of an ordinary 90-degree trace corner when chamfering
        it. Tracks effective_staircase_limit_units() by default (an
        explicit CORNER_SLANT_LIMIT_MIL overrides that). Returns None
        if slanting is disabled.
        """
        if CORNER_SLANT_LIMIT_EXPLICIT:
            units = CORNER_SLANT_LIMIT_MIL * DB_UNITS_PER_MIL if CORNER_SLANT_LIMIT_MIL else 0
            return units if units else None
        return self.effective_staircase_limit_units()

    def _rotate_to_safe_seam(self, group_edges, ordered):
        """For a closed loop, rotate the ordered edge list so it starts
        right after a point where two adjacent edges can't pair up. Every
        closed loop must have at least one such point (it can't be a
        single unbroken diagonal all the way around, since it has to
        turn to close) -- starting there guarantees a genuine diagonal
        run is never artificially cut in half by an arbitrary starting
        point."""
        n = len(ordered)
        for k in range(n):
            idx_prev, _, _ = ordered[k - 1]
            idx_cur, _, _  = ordered[k]
            if not self._is_pairable(group_edges[idx_prev], group_edges[idx_cur]):
                return ordered[k:] + ordered[:k]
        return ordered  # fully-uniform loop (shouldn't happen); leave as-is

    def _compress_LH_LV_chain(self, group_edges, ordered):
        """Used only for CLOSED LOOPS (shape/outline boundaries with no
        real electrical endpoints to preserve, e.g. a pad or via's
        annular outline) -- open chains (real routed traces) use
        _find_open_chain_runs() + _emit_middle_out_run() instead, which
        additionally exploit Ultiboard's fixed H-start/V-end staircase
        convention and check via-waypoint safety. A closed loop has no
        such convention to lean on, so it keeps the original
        corner-based placement.

        Scan an ordered edge chain and collapse runs of alternating
        equal-length H/V pairs (each pair forms an exact 45-degree step,
        whatever its length) into single diagonal segments. A run only
        ever extends by whole (H,V) pairs, so the merged result always
        has equal total H and V displacement -- i.e. it is always an
        exact 45-degree line.

        A run must contain at least 2 qualifying pairs (4 edges) to be
        merged. A single lone pair is deliberately left unmerged: it is
        geometrically indistinguishable from an ordinary right-angle
        routing corner whose two legs happen to be the same length, and
        only a genuine multi-step staircase (which routing corners never
        produce) gives enough confidence to safely collapse it.

        Returns a list of ('orig', edge_idx) / ('diag', p_start, p_end).
        """
        out, n = [], len(ordered)

        def step_sign(k):
            _, p_from, p_to = ordered[k]
            return (self._sign(p_to[0] - p_from[0]), self._sign(p_to[1] - p_from[1]))

        i = 0
        while i < n:
            idx, _, _ = ordered[i]
            e = group_edges[idx]

            if i + 1 >= n or not self._is_pairable(e, group_edges[ordered[i + 1][0]]):
                out.append(("orig", idx))
                i += 1
                continue

            run_sign = (step_sign(i)[0] or step_sign(i + 1)[0],
                        step_sign(i)[1] or step_sign(i + 1)[1])
            run, j, pair_count = [i, i + 1], i + 1, 1
            while j + 2 < n:
                idxA, _, _ = ordered[j + 1]
                idxB, _, _ = ordered[j + 2]
                eA, eB = group_edges[idxA], group_edges[idxB]
                if not self._is_pairable(eA, eB):
                    break
                if eA["orient"] == group_edges[ordered[j][0]]["orient"]:
                    break
                sA, sB = step_sign(j + 1), step_sign(j + 2)
                if (sA[0] or sB[0], sA[1] or sB[1]) != run_sign:
                    break
                run.extend([j + 1, j + 2])
                pair_count += 1
                j += 2

            if pair_count >= 2:
                out.append(("diag", ordered[run[0]][1], ordered[run[-1]][2]))
                i = run[-1] + 1
            else:
                # lone pair: not enough evidence of a genuine staircase,
                # leave both edges as their original H/V segments
                out.append(("orig", idx))
                i += 1
        return out

    @staticmethod
    def _diag_LT_encoding(p_start, p_end):
        """Encode a 45-degree segment into *LT's <coord1>/<coord2>/<coord3>
        + orientation, matching kiub.py's _handle_trace decoding.

        kiub.py negates Y for orientation 1/2 (H/V) but NOT for 4/8
        (diagonal) -- so for a diagonal segment's decoded Y to line up
        with the Y sign convention already used by the surrounding H/V
        geometry (i.e. final_y = -raw_y), the constant/per-point roles
        work out swapped from the naive x+y / x-y pairing:
          orientation 4 (NE): coord1 = x-y (constant), coord2/3 = x+y per endpoint
          orientation 8 (SE): coord1 = x+y (constant), coord2/3 = x-y per endpoint

        Asserts the input is actually a 45-degree pair rather than
        silently falling through to orientation 8 for anything that
        isn't orientation 4 -- a caller bug that hands this two points
        that aren't really on a 45-degree line from each other (e.g. a
        corner-slant chamfer built from the wrong pair of endpoints)
        would otherwise produce a syntactically valid but geometrically
        meaningless *LT record with no error at all.
        """
        x1, y1 = p_start
        x2, y2 = p_end
        if x1 - y1 == x2 - y2:
            return x1 - y1, x1 + y1, x2 + y2, 4
        assert x1 + y1 == x2 + y2, (
            f"_diag_LT_encoding: {p_start} -> {p_end} is not a 45-degree pair "
            f"(dx={x2-x1}, dy={y2-y1})"
        )
        return x1 + y1, x1 - y1, x2 - y2, 8

    def _emit_orig_edge(self, e, layer):
        """Emit a single unmerged H/V edge in *LT format."""
        self._emit_orig_points(e, layer, e["p1"], e["p2"])

    def _emit_orig_points(self, e, layer, p1, p2):
        """Emit an H/V edge using the given endpoints, which may be a
        shortened version of e's own original ones if an adjacent
        diagonal run borrowed part of its length (see
        _resolve_and_emit_open_chain)."""
        if e["orient"] == "H":
            coord1, c0, c1, orient = p1[1], p1[0], p2[0], 1
        else:
            coord1, c0, c1, orient = p1[0], p1[1], p2[1], 2
        self.out.append(f"*LT {layer} {coord1}")
        self.out.append(f"{c0} {c1} {e['net']} {e['tcode']} 0 {orient};")

    def _resolve_and_emit_open_chain(self, group_edges, ordered, entries, layer, net, tcode):
        """Resolve and emit a flattened list of ('orig', position) /
        ('run', start, end) entries for one open-chain component (all
        indices into `ordered`).

        Each 'run' becomes a middle-out diagonal (see
        _diag_LT_encoding for the placement rationale: a line through
        the boundary unit segments' midpoints is always exactly
        halfway between the two possible corner-based placements).
        Rather than reconnecting each end to the run's true boundary
        vertex with a separate perpendicular half-length stub, the
        diagonal first tries to extend along its own slope directly
        into the immediately adjacent connecting segment, shortening
        that segment by half a stair-unit-length from its own far end
        instead -- this lands exactly on the connecting segment's own
        axis (since it runs perpendicular to the stair-unit edge it
        replaces), so no separate stub segment is needed at all. This
        only applies when the neighbouring entry is a plain 'orig'
        segment (not another run), runs perpendicular to the run's
        boundary edge, and has enough spare length to give up; a hinge
        edge between two reversing runs can be borrowed from by both
        sides at once, and is dropped entirely if that consumes it
        exactly. Otherwise -- most commonly when the run's boundary
        sits directly at a pad/via with no connecting segment at all
        to shorten -- the original perpendicular half-stub is used as
        a safe fallback.
        """
        n = len(entries)

        # Exact, possibly half-unit, points: a staircase step of an odd
        # number of database units (a 15-unit = 12.5 mil grid, or 1 unit)
        # has its midpoints, and half-step borrows, on half units.
        # _snap_half_units turns them into whole units after Pass 1.
        def midpoint(p1, p2):
            return ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)

        remaining = {}          # orig position -> spare length left to borrow
        borrow_from = {}        # orig position -> {shared_point: new_point}
        run_geom = {}           # entry index -> resolved geometry (see below)
        run_line = {}           # entry index -> "sum" (x+y const) or "diff" (x-y const)

        # Pass 1: resolve each run's actual diagonal endpoints.
        for k, entry in enumerate(entries):
            if entry[0] != "run":
                continue
            _, start, end = entry
            first_idx, c_start, _ = ordered[start]
            last_idx, _, c_end    = ordered[end]
            first_e, last_e       = group_edges[first_idx], group_edges[last_idx]

            m_start = midpoint(first_e["p1"], first_e["p2"])
            m_end   = midpoint(last_e["p1"], last_e["p2"])

            # Which 45-degree line this run's diagonal actually lies on
            # (x-y=const or x+y=const) -- a borrowed point is only valid
            # if it lies on this SAME line. This is what distinguishes a
            # genuine straight connecting trace (borrowing from it stays
            # on the line) from a direction-reversal hinge shared with a
            # differently-sloped neighboring run (borrowing from it
            # would not).
            if m_start[0] - m_start[1] == m_end[0] - m_end[1]:
                line_kind, line_val = "diff", m_start[0] - m_start[1]
            else:
                line_kind, line_val = "sum", m_start[0] + m_start[1]

            def try_borrow(step, boundary_pos, shared_pt, ref_orient, ref_length):
                """Walk from the run's boundary outward (step=+1 for the
                trailing side, -1 for leading), through the entries list
                starting at index k+step, position boundary_pos in
                `ordered`. Any immediately-adjacent 'orig' segment whose
                length exactly matches ref_length is fully consumed and
                the walk continues past it -- such a segment is
                geometrically indistinguishable from another step of the
                very same uniform staircase (only excluded from the run
                itself by the balance trim in _find_open_chain_runs), so
                treating it as a genuine, differently-sized connecting
                trace would stop the extension one step too early. The
                walk stops and takes a final half-length borrow at the
                first segment that doesn't exactly match (a real
                connecting trace), or gives up (falling back to a
                perpendicular stub) at anything else: a via-safe run
                boundary, a branching/loop entry, a mismatched
                orientation, or insufficient remaining length.
                """
                idx, cur_pt, cur_orient, cur_len = k + step, shared_pt, ref_orient, ref_length
                pending = []  # [(position, shared_pt, new_pt, new_remaining), ...]
                while True:
                    if not (0 <= idx < n):
                        return None
                    nb = entries[idx]
                    if nb[0] != "orig" or nb[1] != boundary_pos:
                        return None
                    nb_e = group_edges[ordered[nb[1]][0]]
                    if nb_e["orient"] == cur_orient:
                        return None
                    far = nb_e["p2"] if nb_e["p1"] == cur_pt else nb_e["p1"]

                    next_idx, next_pos = idx + step, boundary_pos + step
                    next_is_orig = (0 <= next_idx < n and entries[next_idx][0] == "orig"
                                    and entries[next_idx][1] == next_pos)
                    if nb_e["length"] == cur_len and next_is_orig:
                        # Fully consume: there's more plain, potentially
                        # uniform-chain material beyond this segment, so
                        # it's not the far side of a reversal hinge
                        # meeting another run halfway (that case is
                        # handled below instead, by the ordinary
                        # half-length borrow, exactly as before).
                        pending.append((nb[1], cur_pt, far, 0))
                        cur_pt, cur_orient, cur_len = far, nb_e["orient"], nb_e["length"]
                        idx, boundary_pos = next_idx, next_pos
                        continue

                    avail = remaining.get(nb[1], nb_e["length"])
                    half = cur_len / 2
                    if avail < half:
                        return None
                    dx, dy = self._sign(far[0] - cur_pt[0]), self._sign(far[1] - cur_pt[1])
                    new_pt = (cur_pt[0] + dx * half, cur_pt[1] + dy * half)
                    on_line = ((new_pt[0] - new_pt[1] == line_val) if line_kind == "diff"
                               else (new_pt[0] + new_pt[1] == line_val))
                    if not on_line:
                        return None
                    pending.append((nb[1], cur_pt, new_pt, avail - half))
                    # The whole cascade succeeded -- commit every pending
                    # change (including any fully-consumed intermediate
                    # segments) now, all at once.
                    for pos, shared, dest, new_remaining in pending:
                        borrow_from.setdefault(pos, {})[shared] = dest
                        remaining[pos] = new_remaining
                    return new_pt

            p_diag_start = try_borrow(-1, start - 1, c_start, first_e["orient"], first_e["length"])
            need_start_stub = p_diag_start is None
            if need_start_stub:
                p_diag_start = m_start

            p_diag_end = try_borrow(1, end + 1, c_end, last_e["orient"], last_e["length"])
            need_end_stub = p_diag_end is None
            if need_end_stub:
                p_diag_end = m_end

            run_geom[k] = (p_diag_start, p_diag_end, need_start_stub, need_end_stub,
                           c_start, c_end, first_e, last_e)
            run_line[k] = line_kind

        self._snap_half_units(run_geom, run_line, borrow_from)

        # Pass 2: build a flat list of "leaves" -- every piece of
        # geometry the run-borrow resolution above actually leaves
        # behind, in order (a leaf is either an unmerged straight
        # segment, a diagonal's stub, or a run's own diagonal) -- then
        # look for ordinary 90-degree corners between adjacent
        # straight leaves and chamfer them, before emitting everything.
        leaves = self._build_leaf_list(group_edges, ordered, entries, run_geom, borrow_from)
        self._apply_corner_slant(leaves, layer, net, tcode)
        self._finalize_and_emit_leaves(leaves, layer, net, tcode)

    @staticmethod
    def _snap_half_units(run_geom, run_line, borrow_from):
        """Move every diagonal endpoint that lies on a half unit onto a
        whole unit, keeping each diagonal exactly 45 degrees and every
        piece connected and orthogonal.

        An endpoint lies on a straight segment -- the stair edge whose
        midpoint it is, or the connecting segment it was borrowed from --
        and only its coordinate along that segment can be fractional
        (the other one is the segment's own, whole, coordinate). So the
        point is moved along its own segment only: the segment stays
        horizontal or vertical, its far end is untouched, and the
        diagonal and the neighbouring piece keep sharing the moved point.
        Both endpoints of a diagonal lie on the same half-unit line
        (x+y or x-y = n + 0.5), so moving both by half a unit the same way
        lands them on one whole line: exactly 45 degrees. The way is
        half a unit down (x+y or x-y = n) unless an endpoint shared with
        an earlier diagonal (a hinge between two runs) is already fixed
        the other way.
        """
        fixed = {}                              # exact point -> whole point

        def snapped(pt, kind, shift):
            # shift -0.5 or +0.5 of the line constant x+y (sum) or x-y (diff)
            x, y = pt
            if x != int(x):
                x += shift
            elif y != int(y):
                y += shift if kind == "sum" else -shift
            return (int(x), int(y))

        for k, geom in run_geom.items():
            p_start, p_end = geom[0], geom[1]
            frac = [p for p in (p_start, p_end) if p[0] != int(p[0]) or p[1] != int(p[1])]
            if not frac:
                continue
            kind = run_line[k]
            choice = None
            for shift in (-0.5, 0.5):
                if all(fixed.get(p, snapped(p, kind, shift)) == snapped(p, kind, shift)
                       for p in frac):
                    choice = shift
                    break
            if choice is None:
                choice = -0.5                   # cannot agree with both: _diag_LT_encoding reports it
            new = {p: fixed.get(p, snapped(p, kind, choice)) for p in frac}
            fixed.update(new)
            run_geom[k] = (new.get(p_start, p_start), new.get(p_end, p_end)) + tuple(geom[2:])

        # Every whole point: the borrowed points (the ends of the
        # shortened connecting segments) follow their diagonal's ends.
        for shared in borrow_from.values():
            for key, dest in shared.items():
                # (int: a whole point computed as a float, e.g. 3315.0)
                shared[key] = fixed.get(dest, (int(dest[0]), int(dest[1])))
        for k, geom in run_geom.items():
            run_geom[k] = (tuple(int(v) for v in geom[0]), tuple(int(v) for v in geom[1])) \
                + tuple(geom[2:])

    def _build_leaf_list(self, group_edges, ordered, entries, run_geom, borrow_from):
        """Flatten the run-borrow-resolved entries into an ordered list
        of leaves, each a dict with:
          orient:    'H' | 'V' | 'diag'
          p1, p2:    endpoints as resolved by run-borrowing (unaffected
                     by corner-slanting, applied later)
          length:    leg length (H/V only; None for 'diag')
          p1_offset / p2_offset: how much corner-slanting trims inward
                     from each end, filled in by _apply_corner_slant()
                     (both start at 0)
        Consecutive leaves always share an endpoint (leaf[i]["p2"] ==
        leaf[i+1]["p1"]) by construction -- which depends on reading
        each 'orig' edge's endpoints from `ordered[pos]` (the
        walk-consistent from/to direction that _order_LH_LV_component
        already computed for it), not from the edge's own raw stored
        p1/p2. An edge's storage order and its walk direction can
        legitimately disagree -- _order_LH_LV_component walks whichever
        way the chain actually connects, regardless of which end
        happened to be recorded as p1 vs p2 -- and the rest of this
        pass assumes leaves are given in walk-consistent order
        throughout, exactly like Pass 1's own c_start/c_end already do
        for run boundaries. Using the edge's raw p1/p2 instead is a
        real, if easy to make, mistake: it silently swaps which end of
        that edge is treated as "shared with the next leaf" and which
        is treated as "this component's true outer end", so a
        corner-slant offset meant for the real shared corner lands on
        the true endpoint instead -- one that structurally should never
        be touched at all (see _apply_corner_slant's docstring) -- while
        the genuine corner never gets chamfered.
        """
        leaves = []
        for k, entry in enumerate(entries):
            if entry[0] == "orig":
                pos = entry[1]
                e = group_edges[ordered[pos][0]]
                _, p1, p2 = ordered[pos]
                for shared_pt, new_pt in borrow_from.get(pos, {}).items():
                    if p1 == shared_pt:
                        p1 = new_pt
                    if p2 == shared_pt:
                        p2 = new_pt
                if p1 == p2:
                    continue
                leaves.append({
                    "orient": e["orient"], "p1": p1, "p2": p2,
                    "length": abs(p2[0] - p1[0]) + abs(p2[1] - p1[1]),
                    "p1_offset": 0, "p2_offset": 0,
                })
            else:
                (p_diag_start, p_diag_end, need_start_stub, need_end_stub,
                 c_start, c_end, first_e, last_e) = run_geom[k]
                if need_start_stub:
                    leaves.append({
                        "orient": first_e["orient"], "p1": c_start, "p2": p_diag_start,
                        "length": (abs(p_diag_start[0] - c_start[0])
                                   + abs(p_diag_start[1] - c_start[1])),
                        "p1_offset": 0, "p2_offset": 0,
                    })
                leaves.append({
                    "orient": "diag", "p1": p_diag_start, "p2": p_diag_end,
                    "length": None, "p1_offset": 0, "p2_offset": 0,
                })
                if need_end_stub:
                    leaves.append({
                        "orient": last_e["orient"], "p1": p_diag_end, "p2": c_end,
                        "length": (abs(c_end[0] - p_diag_end[0])
                                   + abs(c_end[1] - p_diag_end[1])),
                        "p1_offset": 0, "p2_offset": 0,
                    })
        return leaves

    def clearance_safe_trim(self, tcode):
        """The largest chamfer trim, in native db units, that keeps a
        corner of trace code `tcode` at least its own clearance away from
        anything it already kept that clearance from: (2 - sqrt 2) * c +
        (1 - sqrt 2 / 2) * w, w and c from the code's *TT record (see the
        comment below CORNER_SLANT_LIMIT_EXPLICIT). None when the file
        gives no width and clearance for the code (no extra cap then)."""
        wc = self.trace_codes.get(tcode)
        if not wc:
            return None
        width, clearance = wc
        return (2 - 2 ** 0.5) * clearance + (1 - 2 ** 0.5 / 2) * width

    def _apply_corner_slant(self, leaves, layer, net, tcode=None):
        """Find ordinary 90-degree corners between adjacent straight
        (H/V) leaves and record how much to trim off each side.

        The trim is the configured limit, capped by the trace code's own
        clearance-safe trim (clearance_safe_trim) and by half of either
        adjacent leg.

        A diagonal leaf is never a corner-slant candidate on either
        side -- a run's own diagonal meets its stub (or a directly
        borrowed connecting segment) at 135 degrees, not 90, and is
        already exactly what the staircase-recovery feature intended;
        this pass only ever looks at two adjacent H/V leaves.

        Each corner's trim amount is capped to at most half of
        *either* adjacent leg's own full length, regardless of the
        configured limit -- for a long leg this never binds and the
        configured limit is what actually applies; for a leg shorter
        than twice the limit (e.g. a short "singlet" between two other
        corners in quick succession), its own available length caps it
        instead. See CORNER_SLANT_LIMIT_MIL's module comment for why
        this keeps the result within the original corner's own
        footprint regardless of which leg is doing the limiting.

        For a via, "within tolerance" alone isn't quite the right test:
        a short leg can have *both* its corners fall within a via's own
        radius even though the via genuinely terminates a trace at
        only one of them -- the other is just incidentally close
        because the leg itself is short. So instead of protecting
        every corner within a via's radius, only the single *closest*
        candidate corner is protected per via -- and that comparison
        includes the whole chain's own two true outer endpoints
        alongside the internal candidates, even though neither is ever
        itself a chamfer candidate (they're already safe by
        construction, never touched by this scan regardless of via
        proximity). Without that, a via whose real attachment point is
        one of those true endpoints would still end up "claiming" and
        protecting whatever internal candidate happens to be nearest to
        it -- even though the via was never actually going to be
        disconnected there at all. Only when an internal candidate is
        genuinely the closest point overall (closer than either true
        endpoint) does it get protected. Each via's radius is resolved
        for *this specific layer* via _via_radius_for_layer -- not a
        single value shared across layers, since *T0/*T1/*T2 can
        legitimately declare different pad sizes for the same code (see
        that method's own docstring). A corner at a pad needs no
        equivalent check for the same true-endpoint reason: a pad can
        only ever sit at one of those same two points.
        """
        limit = self.effective_corner_slant_limit_units()
        if not limit:
            return
        safe = self.clearance_safe_trim(tcode)
        if safe is not None:
            limit = min(limit, safe)
        via_points = self.via_points_by_net.get(net, ())

        candidates = [
            i for i in range(len(leaves) - 1)
            if leaves[i]["orient"] in ("H", "V")
            and leaves[i + 1]["orient"] in ("H", "V")
            and leaves[i]["orient"] != leaves[i + 1]["orient"]
        ]
        true_endpoints = []
        if leaves and leaves[0]["orient"] in ("H", "V"):
            true_endpoints.append(leaves[0]["p1"])
        if leaves and leaves[-1]["orient"] in ("H", "V"):
            true_endpoints.append(leaves[-1]["p2"])

        protected = set()
        for vx, vy, pad_code in via_points:
            radius = self._via_radius_for_layer(pad_code, layer)
            if radius <= 0:
                continue
            radius_sq = radius * radius
            best_i, best_dist_sq = None, None
            for i in candidates:
                shared = leaves[i]["p2"]  # == leaves[i+1]["p1"]
                dist_sq = (shared[0] - vx) ** 2 + (shared[1] - vy) ** 2
                if dist_sq <= radius_sq and (best_dist_sq is None or dist_sq < best_dist_sq):
                    best_i, best_dist_sq = i, dist_sq
            for ep in true_endpoints:
                dist_sq = (ep[0] - vx) ** 2 + (ep[1] - vy) ** 2
                if dist_sq <= radius_sq and (best_dist_sq is None or dist_sq < best_dist_sq):
                    best_i, best_dist_sq = None, dist_sq  # a true endpoint won; nothing to protect
            if best_i is not None:
                protected.add(best_i)

        for i in candidates:
            if i in protected:
                continue
            a, b = leaves[i], leaves[i + 1]
            # Rounded once, here -- not left as a float to be rounded
            # independently at each end later. A half-integer slant
            # (whenever the binding leg length is odd) would otherwise
            # let the two ends' shifted coordinates round in different
            # effective directions (Python's round-half-to-even depends
            # on the parity of the specific value being rounded, which
            # differs between the two sides), breaking the exact
            # |dx|=|dy| the resulting chamfer diagonal depends on by a
            # single unit. Rounding the shared slant value itself first
            # means both ends add/subtract the same integer, which is
            # exact.
            slant = round(min(limit, a["length"] / 2, b["length"] / 2))
            if slant < 1:
                continue
            a["p2_offset"] = slant
            b["p1_offset"] = slant
            self.corners_chamfered += 1

    def _finalize_and_emit_leaves(self, leaves, layer, net, tcode):
        """Resolve each leaf's final geometry after corner-slanting and
        emit everything: unmerged straight legs, run diagonals, and
        newly chamfered corners.

        Every H/V leaf's two ends are shifted independently by
        whatever _apply_corner_slant recorded there, unconditionally --
        there is deliberately no special case for a leg that ends up
        fully consumed (both ends shifted by exactly half its own
        length): shifting from p1 by an offset and shifting from p2 by
        that same offset are, for integer coordinates, exact mirror
        arithmetic (p1+d and p2-d with d=(p2-p1)/2), so they always
        agree to the last database unit with no separate handling
        needed. If a leg does collapse to zero length, p1==p2 falls out
        naturally and is simply skipped during emission.

        Adjacent chamfer diagonals that turn out collinear (this only
        ever happens when a leg between them collapsed to zero length,
        since a genuine kink can't average out to |dx|=|dy| across two
        45-degree pieces from different diagonal families) are merged
        into a single output diagonal using their own two outer
        endpoints directly.
        """
        def shift(p_from, p_to, offset):
            if offset <= 0:
                return p_from
            dx, dy = self._sign(p_to[0] - p_from[0]), self._sign(p_to[1] - p_from[1])
            return (round(p_from[0] + dx * offset), round(p_from[1] + dy * offset))

        for leaf in leaves:
            if leaf["orient"] == "diag":
                leaf["p1_side_point"] = leaf["p1"]
                leaf["p2_side_point"] = leaf["p2"]
                continue
            leaf["p1_side_point"] = shift(leaf["p1"], leaf["p2"], leaf["p1_offset"])
            leaf["p2_side_point"] = shift(leaf["p2"], leaf["p1"], leaf["p2_offset"])

        # Build the raw output-piece list: each non-degenerate leaf's
        # own geometry, plus a chamfer diagonal wherever a corner-slant
        # was actually applied (i.e. wherever the two adjacent leaves'
        # touching points no longer coincide).
        pieces = []  # ('straight', orient, p1, p2) | ('diag', p1, p2)
        for i, leaf in enumerate(leaves):
            if leaf["orient"] == "diag":
                pieces.append(("diag", leaf["p1_side_point"], leaf["p2_side_point"]))
            elif leaf["p1_side_point"] != leaf["p2_side_point"]:
                pieces.append(("straight", leaf["orient"],
                               leaf["p1_side_point"], leaf["p2_side_point"]))

            if i + 1 < len(leaves):
                p_a = leaf["p2_side_point"]
                p_b = leaves[i + 1]["p1_side_point"]
                if p_a != p_b:
                    pieces.append(("diag", p_a, p_b))

        # Merge adjacent diagonal pieces that are collinear: check the
        # two outer endpoints directly (no need to reference the shared
        # middle point at all) -- |dx|=|dy| between them holds exactly
        # when both pieces belong to the same 45-degree family, which
        # is precisely what "collinear through a shared point" means
        # here.
        merged = []
        for piece in pieces:
            if (merged and merged[-1][0] == "diag" and piece[0] == "diag"
                    and merged[-1][2] == piece[1]):
                a_start, b_end = merged[-1][1], piece[2]
                if abs(b_end[0] - a_start[0]) == abs(b_end[1] - a_start[1]):
                    merged[-1] = ("diag", a_start, b_end)
                    continue
            merged.append(piece)

        for piece in merged:
            if piece[0] == "straight":
                _, orient, p1, p2 = piece
                if orient == "H":
                    coord1, c0, c1, o = p1[1], p1[0], p2[0], 1
                else:
                    coord1, c0, c1, o = p1[0], p1[1], p2[1], 2
                self.out.append(f"*LT {layer} {coord1}")
                self.out.append(f"{c0} {c1} {net} {tcode} 0 {o};")
            else:
                _, p1, p2 = piece
                if p1 == p2:
                    continue
                coord1, c0, c1, orient = self._diag_LT_encoding(p1, p2)
                self.out.append(f"*LT {layer} {coord1}")
                self.out.append(f"{c0} {c1} {net} {tcode} 0 {orient};")

    def _find_open_chain_runs(self, group_edges, ordered):
        """For an OPEN CHAIN (a real routed trace with two electrical
        endpoints, not a closed loop) find every maximal run of edges
        that alternate H/V, all share exactly the same length (a single
        staircase is drawn at one consistent grid step), and move in
        one consistent diagonal direction -- then trims it only if it
        isn't already balanced.

        Strict alternation means a run's H and V edge counts can only
        ever be equal, or differ by exactly 1 (with the majority
        orientation sitting at both ends). An equal-count run is
        already a complete, valid 45-degree diagonal and needs no
        trimming, even if it happens to start or end on either
        orientation -- e.g. two diagonals meeting at a "peak" (a
        direction reversal) produce exactly this shape, and it's
        correct as-is. Only a genuinely unbalanced run (off by one) is
        trimmed, by dropping one edge from whichever end carries the
        majority orientation -- which always leaves the result
        starting on a horizontal segment and ending on a vertical one,
        matching Ultiboard's own fixed convention for how it draws a
        diagonal trace as a staircase (confirmed empirically: no matter
        which direction a diagonal is drawn, the first segment is
        always horizontal and the last is always vertical). The
        trimmed-off edge can never be a genuine staircase step in that
        case -- it's either a separate connecting trace segment that
        happens to share the same length, or the hinge of a direction
        reversal that belongs to neither adjacent run -- and is left
        unmerged rather than folded in.

        Returns a list of ('orig', position) / ('run', start_i, end_i)
        entries (all indices into `ordered`), covering every edge in
        `ordered` exactly once.
        """
        n = len(ordered)

        def step_sign(k):
            _, p_from, p_to = ordered[k]
            return (self._sign(p_to[0] - p_from[0]), self._sign(p_to[1] - p_from[1]))

        entries = []
        i = 0
        while i < n:
            j, run_sign = i, None
            while j + 1 < n:
                idx_a, _, _ = ordered[j]
                idx_b, _, _ = ordered[j + 1]
                if not self._is_pairable(group_edges[idx_a], group_edges[idx_b]):
                    break
                sa, sb = step_sign(j), step_sign(j + 1)
                combined = (sa[0] or sb[0], sa[1] or sb[1])
                if run_sign is None:
                    run_sign = combined
                elif combined != run_sign:
                    break
                j += 1

            start, end = i, j
            count_h = sum(1 for k in range(start, end + 1)
                          if group_edges[ordered[k][0]]["orient"] == "H")
            count_v = (end - start + 1) - count_h
            # Strict alternation means these can only ever be equal, or
            # differ by exactly 1 (with the majority orientation sitting
            # at both ends). Equal counts are already a complete,
            # correctly-balanced 45-degree run -- e.g. a run that starts
            # and ends on the SAME orientation because it's actually two
            # diagonals meeting at a "peak" (a direction reversal), which
            # is itself perfectly valid and needs no trim at all. Only
            # trim when genuinely unbalanced, and always from whichever
            # end carries the majority orientation, so the result still
            # starts on H and ends on V either way.
            if count_h > count_v:
                end -= 1
            elif count_v > count_h:
                start += 1

            if end > start:
                for k in range(i, start):
                    entries.append(("orig", k))
                entries.append(("run", start, end))
                for k in range(end + 1, j + 1):
                    entries.append(("orig", k))
                if end - start >= 3:   # 4+ edges = 2+ consecutive pairs
                    self.multi_step_run_lengths.append(
                        group_edges[ordered[start][0]]["length"])
            else:
                for k in range(i, j + 1):
                    entries.append(("orig", k))
            i = j + 1
        return entries

    def _split_at_via_waypoints(self, group_edges, ordered, start, end, net, layer):
        """A diagonal can never be allowed to run through a point where
        a via for this net sits (or close enough to it -- see
        _point_near_via's docstring for why exact coordinate matching
        alone isn't reliable) -- it would disconnect the via. Check
        every INTERNAL corner of the run (the two boundary vertices are
        excluded: they get their own connecting stub segments
        regardless, in _emit_middle_out_run) against this net's via
        centers, and split the run there if one is found, re-trimming
        each half the same way _find_open_chain_runs does (only if
        genuinely unbalanced -- see that method's docstring).

        Returns a list of ('orig', position) / ('run', start_i, end_i)
        entries covering [start, end] exactly once.
        """
        via_points = self.via_points_by_net.get(net)
        if not via_points:
            return [("run", start, end)]

        split_at = [k for k in range(start, end)
                    if self._point_near_via(ordered[k][2], via_points, layer)]
        if not split_at:
            return [("run", start, end)]

        bounds, seg_start = [], start
        for k in split_at:
            bounds.append((seg_start, k))
            seg_start = k + 1
        bounds.append((seg_start, end))

        entries = []
        for s0, e0 in bounds:
            if s0 > e0:
                continue
            s, e = s0, e0
            count_h = sum(1 for k in range(s, e + 1)
                          if group_edges[ordered[k][0]]["orient"] == "H")
            count_v = (e - s + 1) - count_h
            if count_h > count_v:
                e -= 1
            elif count_v > count_h:
                s += 1

            if e > s:
                for k in range(s0, s):
                    entries.append(("orig", k))
                entries.append(("run", s, e))
                for k in range(e + 1, e0 + 1):
                    entries.append(("orig", k))
            else:
                for k in range(s0, e0 + 1):
                    entries.append(("orig", k))
        return entries

    def _emit_LH_LV_edges(self, edges):
        groups = {}
        for idx, e in enumerate(edges):
            # Grouped by (layer, net, tcode) only -- NOT flag (the F/V
            # fixed/variable-width marker, already dropped from every
            # *LT record emitted below). Flag can legitimately change
            # partway along one continuous physical trace (confirmed:
            # a real trace whose own DDF records switch from flag 'V'
            # to flag 'F' partway along, with no other topology change
            # at that point at all) -- grouping by it as well would
            # artificially cut that trace into two disconnected
            # components right there, for no real reason, hiding a
            # perfectly good connecting segment from the borrow-cascade
            # on one side of the cut. Real connectivity is still
            # determined purely by shared endpoints, via
            # _build_LH_LV_components below, so this only removes a
            # spurious boundary, not genuine topology.
            key = (e["layer"], e["net"], e["tcode"])
            groups.setdefault(key, []).append(e)

        junctions = self._cross_code_junctions(edges)
        for (layer, net, tcode), group_edges in groups.items():
            group_junctions = junctions.get((layer, net, tcode), set())
            ends = {p for e in group_edges for p in (e["p1"], e["p2"])}
            _adjacency, components = self._build_LH_LV_components(
                group_edges, frozenset(group_junctions & ends))

            for comp in components:
                mark = len(self.out)
                self._emit_LH_LV_component(group_edges, comp, layer, net, tcode)
                # A junction inside one of this component's segments (not
                # at a vertex, so not split at) must still lie on what was
                # emitted; if a merge or chamfer took it away, the
                # component is emitted unmerged instead.
                inside = {p for p in group_junctions - ends
                          if any(self._on_edge(p, group_edges[i]) for i in comp)}
                if inside and not all(self._on_emitted(p, self.out[mark:]) for p in inside):
                    del self.out[mark:]
                    for idx in sorted(comp):
                        self._emit_orig_edge(group_edges[idx], layer)

    @staticmethod
    def _on_edge(p, e):
        (x1, y1), (x2, y2) = e["p1"], e["p2"]
        return ((x1 == x2 == p[0] and min(y1, y2) <= p[1] <= max(y1, y2))
                or (y1 == y2 == p[1] and min(x1, x2) <= p[0] <= max(x1, x2)))

    @staticmethod
    def _on_emitted(p, lines):
        """Whether point p lies on one of the *LT pieces in lines."""
        coord1 = None
        for ln in lines:
            if ln.startswith("*LT "):
                coord1 = int(ln.split()[2])
                continue
            f = ln.rstrip(";").split()
            a, b, orient = int(f[0]), int(f[1]), f[-1]
            if orient == "1":
                q1, q2 = (a, coord1), (b, coord1)
            elif orient == "2":
                q1, q2 = (coord1, a), (coord1, b)
            else:                              # 4: coord1 = x-y; 8: coord1 = x+y
                ends = []
                for v in (a, b):
                    sm, df = (v, coord1) if orient == "4" else (coord1, v)
                    ends.append(((sm + df) / 2, (sm - df) / 2))
                q1, q2 = ends
            (x1, y1), (x2, y2) = q1, q2
            if ((x2 - x1) * (p[1] - y1) == (y2 - y1) * (p[0] - x1)
                    and min(x1, x2) <= p[0] <= max(x1, x2) and min(y1, y2) <= p[1] <= max(y1, y2)):
                return True
        return False

    def _cross_code_junctions(self, edges):
        """Per (layer, net, tcode): the points where a trace of another
        trace code of the same layer and net meets it -- an end of one
        trace on an end of, or anywhere along, the other. Edges are
        merged per trace code, so without these points a merge or chamfer
        at such a point would leave the other trace's end loose."""
        by_net = {}
        for e in edges:
            by_net.setdefault((e["layer"], e["net"]), []).append(e)
        out = {}
        for (layer, net), es in by_net.items():
            if len({e["tcode"] for e in es}) < 2:
                continue
            for e in es:
                for p in (e["p1"], e["p2"]):
                    for o in es:
                        if o["tcode"] != e["tcode"] and self._on_edge(p, o):
                            out.setdefault((layer, net, e["tcode"]), set()).add(p)
                            out.setdefault((layer, net, o["tcode"]), set()).add(p)
        return out

    def _emit_LH_LV_component(self, group_edges, comp, layer, net, tcode):
        """Merge and emit one connected component of one trace code."""
        ordered, is_loop = self._order_LH_LV_component(group_edges, comp)

        if ordered is None:
            # Branching component (a junction/T): not a simple
            # staircase -- emit every edge unchanged, unmerged.
            for idx in comp:
                self._emit_orig_edge(group_edges[idx], layer)
            return

        if is_loop:
            # Closed shape outline: no real electrical endpoints
            # to preserve, so it keeps the original corner-based
            # placement (see _compress_LH_LV_chain).
            ordered = self._rotate_to_safe_seam(group_edges, ordered)
            for entry in self._compress_LH_LV_chain(group_edges, ordered):
                if entry[0] == "orig":
                    self._emit_orig_edge(group_edges[entry[1]], layer)
                else:
                    _, p_start, p_end = entry
                    coord1, c0, c1, orient = self._diag_LT_encoding(p_start, p_end)
                    self.out.append(f"*LT {layer} {coord1}")
                    self.out.append(f"{c0} {c1} {net} {tcode} 0 {orient};")
            return

        # Open chain: a real routed trace with two endpoints.
        # Flatten via-waypoint splits into the run-finding
        # entries so the borrow-from-neighbour logic in
        # _resolve_and_emit_open_chain can see every 'orig'
        # segment's true neighbours across the whole component.
        flat_entries = []
        for entry in self._find_open_chain_runs(group_edges, ordered):
            if entry[0] == "orig":
                flat_entries.append(entry)
                continue
            _, s, e = entry
            flat_entries.extend(
                self._split_at_via_waypoints(group_edges, ordered, s, e, net, layer)
            )
        self._resolve_and_emit_open_chain(
            group_edges, ordered, flat_entries, layer, net, tcode
        )

    # =========================================================
    # *V
    def handle_V(self):
        self.out.append(self.lines[self.i].strip())
        self.i += 1

        while True:
            line          = self.lines[self.i].strip()
            has_semicolon = line.endswith(";")
            if has_semicolon:
                line = line[:-1]

            parts = line.split()
            if len(parts) >= 4:
                # pad code: V2/V3 has no separate via-code range, it reuses
                # the same 0-15 pad-code table as ordinary pads. Offset by
                # 240 so the via references the duplicated high-numbered
                # twin (see handle_T), matching V4/V5's own convention of
                # reserving codes 240-255 for vias.
                parts[2] = str(int(parts[2]) + 240)
                # layerset: same bit-position offset as pads/pins (handle_S,
                # handle_C) - applies unconditionally, not just to the
                # all-layers sentinel.
                parts[3] = f"{(int(parts[3], 16) >> 12):08x}"

            line_fixed = " ".join(parts)

            if has_semicolon:
                self.out.append(line_fixed + " 0 0 0 1;")
                self.i += 1
                break
            else:
                self.out.append(line_fixed + " 0 0 0 1")
                self.i += 1

    # =========================================================
    # *X
    def handle_X(self):
        line = self.lines[self.i].rstrip("\n")
        self.i += 1
        # extract first 5 numeric fields only
        match = re.match(r'\*X\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.*)', line)
        if not match:
            self.out.append(line)
            return

        x, y, h, layer, rot, text = match.groups()
        self.out.append(
            f"*X {int(x)} {int(y)} {int(h)} {int(h)} 100 "
            f"{ROT_MAP.get(int(rot), 0)} {int(layer) - 1} {text}"
        )

    # =========================================================
    def emit_TS_and_SBOARD(self):
        self.out.append("*TS H 0 0")

        def odd(x):  return x if x % 2      else x + 1
        def even(x): return x if x % 2 == 0 else x + 1

        w, h = self.board_w, self.board_h
        seg  = [
            (odd(0),  0, even(w), 0),
            (odd(w),  0, even(w), h),
            (odd(w),  h, even(0), h),
            (odd(0),  h, even(0), 0),
        ]

        self.out.extend([
            "*SBOARD",
            "60 90 100 0 100 100",
            "0 0 0 0 0 100",
            "0.000000",
            ",".join(f"{a},{b},{c},{d}" for a, b, c, d in seg) + ";",
            ";\n;"
        ])

    # =========================================================
    def copy_block(self):
        while True:
            line = self.lines[self.i].rstrip("\n")
            self.out.append(line)
            self.i += 1
            if line.endswith(";"):
                break


# =============================================================
# Public API used by kiub.py
# =============================================================

def convert_str(source: str) -> str:
    """Convert a V2/V3 DDF string to a V4.60 DDF string.

    Parameters
    ----------
    source : str
        Full contents of a V2/V3 DDF file decoded as CP437.

    Returns
    -------
    str
        Equivalent V4.60 DDF content as a plain string.
    """
    return DDFConverter(source.splitlines(keepends=True)).convert()


def convert_str_counted(source: str) -> tuple[str, int]:
    """convert_str, plus the number of 90-degree corners chamfered."""
    conv = DDFConverter(source.splitlines(keepends=True))
    return conv.convert(), conv.corners_chamfered


def detect_staircases(source: str) -> dict:
    """Check whether a V2/V3 DDF file's *LH/*LV data actually contains
    any staircase-represented diagonal traces -- intended for a GUI (or
    other caller) to decide whether to prompt the user before running
    (or re-running) a full conversion.

    Returns a dict:
      {
        "found": bool,                  # see below for exactly what this checks
        "declared_grid_mil": float | None,  # this file's own declared
                                             # default grid step, for a
                                             # caller to show the user and
                                             # let them correct if they
                                             # know Ultiboard's routing
                                             # grid changed mid-design
                                             # without the header
                                             # reflecting it -- None if
                                             # the header declares none
        "effective_limit_mil": float,   # what will actually be used if the
                                             # user accepts declared_grid_mil
                                             # as-is: that value (or the
                                             # ceiling, if none declared),
                                             # already capped at ceiling_mil
        "ceiling_mil": float,           # STAIRCASE_CEILING_MIL itself
        "most_common_step_mil": float | None,   # see below -- a warning
        "most_common_step_count": int,          # signal only, never applied
        "chamfer_corners": int,                 # corners the default conversion chamfers
      }

    "found" runs a real conversion under the hood and checks its output
    for any diagonal *LT record, rather than trying to shortcut the real
    component/connectivity analysis: *LH/*LV records are stored sorted
    by column/row coordinate, not by trace or drawing order, so a
    genuine staircase's edges are not necessarily -- or even usually --
    adjacent in the file; a cheaper file-order-only heuristic can't
    reliably tell.

    Two things are deliberately forced for this detection pass alone
    (saving and restoring whatever the caller already had set, so this
    function has no lasting side effects):

    - Corner-slanting (chamfering ordinary 90-degree corners) is forced
      off. Chamfer diagonals and staircase-merge diagonals are written
      in the identical *LT record format, so without isolating this,
      "found" can't actually tell "this file has genuine staircase-drawn
      diagonals" apart from "chamfering ordinary corners happened to
      produce some diagonals too" -- confirmed on a real design file
      where corner-slant alone produced over a thousand diagonal
      records while staircase-merge, in complete isolation at the exact
      same limit, produced zero.

    - The staircase limit used for detection is forced to
      STAIRCASE_CEILING_MIL itself, independent of this file's own
      declared_grid_mil (below). If detection instead used the file's
      own declared grid, a genuinely wrong declared value (too small)
      would make "found" falsely report nothing -- exactly denying the
      one caller (the GUI's staircase dialog) that could let the user
      correct it. Confirmed on a real file: a declared grid of 8.3 mil
      found nothing in isolation, but the same file's genuine staircases
      turned out to need something closer to 25 mil. Detecting at the
      ceiling means "found" answers "could staircase-merge ever find
      anything here, under the most generous reasonable interpretation"
      -- separate from declared_grid_mil/effective_limit_mil, which
      still report this file's own actual values for the dialog to show
      and let the user correct.

    "most_common_step_mil" is the statistical mode of the per-edge
    length across every genuinely multi-step run found during this same
    detection pass (see DDFConverter.multi_step_run_lengths) -- i.e.
    what this file's real, unambiguous staircases actually used, as
    opposed to declared_grid_mil, which only reflects Ultiboard's
    default grid *setting* at whatever point the header was last
    written. If this comes out larger than declared_grid_mil, that's a
    concrete, file-specific sign the routing grid was likely changed
    after these staircases were drawn -- worth surfacing to the user as
    a warning, but deliberately never applied automatically: it's a
    single global statistic for the whole file, and a board can
    legitimately mix a small grid in one dense area with a larger one
    elsewhere, where blindly raising the effective limit to match would
    reintroduce the exact over-merging risk the ceiling exists to
    prevent, just somewhere else on the same board.
    """
    global CORNER_SLANT_LIMIT_MIL, CORNER_SLANT_LIMIT_EXPLICIT
    global STAIRCASE_LIMIT_MIL, STAIRCASE_LIMIT_EXPLICIT
    _saved = (CORNER_SLANT_LIMIT_MIL, CORNER_SLANT_LIMIT_EXPLICIT,
              STAIRCASE_LIMIT_MIL, STAIRCASE_LIMIT_EXPLICIT)
    try:
        CORNER_SLANT_LIMIT_MIL, CORNER_SLANT_LIMIT_EXPLICIT = 0, True
        STAIRCASE_LIMIT_MIL, STAIRCASE_LIMIT_EXPLICIT = STAIRCASE_CEILING_MIL, True
        conv = DDFConverter(source.splitlines(keepends=True))
        output = conv.convert()
        declared_grid_units = conv.declared_grid_units  # set during header
                                                          # parsing, unaffected
                                                          # by the override above
        multi_step_lengths = conv.multi_step_run_lengths
    finally:
        (CORNER_SLANT_LIMIT_MIL, CORNER_SLANT_LIMIT_EXPLICIT,
         STAIRCASE_LIMIT_MIL, STAIRCASE_LIMIT_EXPLICIT) = _saved

    found = any(
        line.rstrip().endswith((" 0 4;", " 0 8;"))
        for line in output.splitlines()
    )

    # The corners the default conversion would chamfer (staircase limit and
    # corner-slant limit as set by the caller, or their auto defaults): a
    # GUI offers the chamfer choice only when there is something to chamfer.
    chamfer_corners = convert_str_counted(source)[1]

    declared_grid_mil = (declared_grid_units / DB_UNITS_PER_MIL) if declared_grid_units else None
    effective_limit_mil = min(declared_grid_mil, STAIRCASE_CEILING_MIL) \
                           if declared_grid_mil else STAIRCASE_CEILING_MIL

    most_common_units, most_common_count = None, 0
    if multi_step_lengths:
        counts = {}
        for length in multi_step_lengths:
            counts[length] = counts.get(length, 0) + 1
        most_common_units, most_common_count = max(counts.items(), key=lambda kv: kv[1])

    return {
        "found": found,
        "declared_grid_mil": declared_grid_mil,
        "effective_limit_mil": effective_limit_mil,
        "ceiling_mil": STAIRCASE_CEILING_MIL,
        "most_common_step_mil": (most_common_units / DB_UNITS_PER_MIL)
                                 if most_common_units is not None else None,
        "most_common_step_count": most_common_count,
        "chamfer_corners": chamfer_corners,
    }


# =============================================================
# Standalone CLI
# =============================================================

def _convert_file(inp: str, outp: str) -> None:
    """Read *inp*, convert V2/V3 → V4.60, write to *outp*.

    Ultiboard requires a DDF file's very last line to be blank to open
    it correctly, so the converted content is written with exactly one
    trailing blank line -- not just a single newline terminating the
    last content line, which alone doesn't create a blank line after it.
    """
    with open(inp, "r", encoding="cp437", errors="ignore") as f:
        source = f.read()
    with open(outp, "w", encoding="cp437") as f:
        f.write(convert_str(source).rstrip() + "\n\n")


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(
        description="Convert an Ultiboard V2/V3 DDF file to V4.60 format."
    )
    _parser.add_argument(
        "infile",
        help="Input DDF file (the .DDF extension is added automatically if omitted).",
    )
    _parser.add_argument(
        "outfile",
        nargs="?",
        default=None,
        help="Output DDF file (default: <infile>_V4.DDF).",
    )
    _parser.add_argument(
        "--staircase-limit",
        type=float,
        default=None,
        metavar="MIL",
        help=(
            "Maximum length (in mil) for a single V2/V3 staircase grid "
            "step -- see STAIRCASE_LIMIT_MIL. Default: 25, or auto-adjusted "
            "from the file's declared default grid step if enough edges "
            "actually share that length."
        ),
    )
    _parser.add_argument(
        "--no-staircase-merge",
        action="store_true",
        help="Disable staircase-to-diagonal recovery entirely (default: enabled).",
    )
    _args = _parser.parse_args()

    if _args.no_staircase_merge:
        STAIRCASE_LIMIT_MIL = 0
        STAIRCASE_LIMIT_EXPLICIT = True
    elif _args.staircase_limit is not None:
        STAIRCASE_LIMIT_MIL = _args.staircase_limit
        STAIRCASE_LIMIT_EXPLICIT = True

    # Add .DDF extension if missing
    if not _args.infile.lower().endswith(".ddf"):
        _args.infile += ".DDF"

    # Validate input file
    if not os.path.exists(_args.infile):
        print(f"ERROR: File '{_args.infile}' does not exist.")
        sys.exit(1)

    # Derive output filename when not supplied
    if _args.outfile is None:
        _args.outfile = _args.infile[:-4] + "_V4.DDF"

    _convert_file(_args.infile, _args.outfile)
    print(f"Done: {_args.infile}  →  {_args.outfile}")
