# Ultiboard ASCII DDF File Format

Reverse-engineered specification, derived from empirical testing against
real Ultiboard DDF files (V2.x through V5.x) and cross-checked against the
KIUB parser/writer implementation. Primary research source: *Ultiboard
32bit DOS and Windows95 — Reference Manual — Appendix A — File Formats
(1997-08-15)*; this document additionally records everything KIUB's own
empirical testing has confirmed, corrected, or found undocumented in that
manual.

Ultiboard's compiled shape libraries (`.LIB`, `.L55`) and technology files
(`.TEC`) are described in [FILEFORMAT_ULTIBOARD_LIB_TEC.md](FILEFORMAT_ULTIBOARD_LIB_TEC.md).

## Contents

[↑ README contents](../../README.md#file-formats)

1. [File Header](#1-file-header)
2. [Technology data — `*T` records](#2-technology-data--t-records)
3. [Shape definitions — `*S`](#3-shape-definitions--sshape-name)
4. [Netlist records — `*N`](#4-netlist-records--n)
5. [Component placement — `*C`](#5-component-placement--c)
6. [Copper subrecords — `*L`](#6-copper-subrecords--l)
7. [Vias — `*V`](#7-vias--v)
8. [Text — `*X`](#8-text--x)
9. [DDF V2.x / V3.x legacy format](#9-ddf-v2x--v3x-legacy-format)
10. [Definitions](#10-definitions)
    - [10.1 Unit system](#101-unit-system)
    - [10.2 Rotation encoding](#102-rotation-encoding)
    - [10.3 Layer numbering and the pad/via layerset bitmask](#103-layer-numbering-and-the-padvia-layerset-bitmask)
    - [10.4 Drill codes and the "SMD trick"](#104-drill-codes-and-the-smd-trick)
    - [10.5 Font encoding and overline markup](#105-font-encoding-and-overline-markup)
    - [10.6 Board outline reconstruction](#106-board-outline-reconstruction)
    - [10.7 Known errata in the 1997 reference manual](#107-known-errata-in-the-1997-reference-manual)

## 1. File Header

The header is positional — every field is read by line/field position, not
by tag — and has this fixed layout:

```
*P <customer name>
<major> <minor>
<x0>, <y0>, <x1>, <y1>, <grid>, <swap level>[, <routing layers>], <max layers>;
<layer lamination sequence>
<reference point x>, <reference point y>
<router options> <user settings...>
<layer direction flags>
<power-plane net numbers, 6 lines>
```

**`*P <customer name>`**
Free-text field, e.g. `*P PCB`. KIUB does not parse the remainder of this
line for content; it only uses the following line to detect the DDF
version (see below).

**Version line — `<major> <minor>`** — space-separated.

- `<major>`: DDF major version, `2`–`5`. KIUB branches its entire unit
  system on this value (Section 10.1). V2.x/V3.x files are transparently
  pre-converted to V4.60 in memory before the rest of the parser ever sees
  them (Section 9); everything below describes the V4/V5 wire format.
- `<minor>`: minor revision (e.g. `50`, `60`, `80`). Not otherwise
  interpreted by KIUB, other than V4.60 having one behavioural difference
  noted under Component placement (Section 5).

**Bounds line — `<x0>, <y0>, <x1>, <y1>, <grid>, <swap level>[, <routing layers>], <max layers>;`**
- Comma-separated, terminated with `;`.
- `<x0>, <y0>`: one corner of the board outline extents, in DDF database
  units (Section 10.1). Used only to pick the smallest KiCad paper size
  ("A5" .. "A0") that fits the board, and to compute a centring offset —
  KIUB does not use these values as authoritative board-outline
  coordinates (the actual outline geometry comes from the `*SBOARD` shape,
  Section 3).
- `<x1>, <y1>`: the opposite corner.
- `<grid>`: default grid step, in the file's native unit system (Section 10.1). Read but not used by KIUB.
- `<swap level>`: read but not used.
- `<routing layers>` (optional): present in some V4 files (confirmed on
  `Ortho_V4.ddf`, an 8-field bounds line), absent in others (confirmed on
  every sampled V5 file, a 7-field bounds line). Read but not used — KIUB
  identifies this field only by its absence changing the field count, and
  always takes the **last** field as `<max layers>` regardless of whether
  this field is present.
- `<max layers>`: number of copper layers on the board, always even, 2–32.
  This is the field that matters: KIUB builds a layer bitmask
  `(2**maxLayers) - 1` from it and derives the full ordered KiCad copper
  layer stack from that mask (Section 10.3).

**Layer lamination sequence line** — a bracket/pipe/plus string such as
`(|+|+|+|+|+|+|+|+|+|+|)` (16-layer) or
`((|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|)+(|))`
(32-layer). Per the reference manual (page 7007): `(` = start layer
(Top), `)` = end layer (Bottom), `|` = a physical PCB layer, `+` = the
insulator between two adjacent layers. The string encodes which
layer-to-layer via spans are physically possible for the chosen lamination
— e.g. grouping `(|)` around a layer pair permits vias terminating on
that pair without drilling through the whole stack (blind/buried vias).
KIUB reads and discards this line; it does not derive blind/buried-via
legality from it, instead inferring each via's real span directly from
that via's own layerset bitmask (Section 7).

Worked examples for an 8-layer board (reference manual page 7007), Top
layer 1, Bottom layer 8:

```
T  1 2 3   4 5 6  B
(  | + |   +  | + )
Vias possible: Top ↔ Bottom only.

T  1 2 3    4 5 6   B
(( | + |) + (| + |))
Vias possible: Top ↔ Bottom, Top ↔ In3, In4 ↔ Bottom.

T  1   2 3   4 5   6  B
((|) + (|) + (|) + (|))
Vias possible: Top ↔ Bottom, Top ↔ In1, In2 ↔ In3, In4 ↔ In5, In6 ↔ Bottom.
```

**Reference point line — `<x>, <y>`**
The user-defined Reference Point (`R`), stored as an offset from the Board
Origin (`X`). In Ultiboard, all other coordinates in the file are relative
to the Board Origin, not the Reference Point — the Reference Point exists
purely for on-screen display/edit purposes in Ultiboard itself and carries
no geometric meaning for the converted file. Read but not used by KIUB.

**Router options / user settings line** — one line, space-separated
integers (e.g. `0 0 0 15 30 1`). Read but not used.

**Layer direction flags line** — one line of 32 space-separated values
(e.g. `2 1 2 1 2 1 …`), one pair of "odd = top view / even = bottom view"
direction flags per copper-layer-pair, per the reference manual's layer
numbering convention (see Section 10.3, "Ultiboard native layer numbers").
Read but not used.

**Power-plane net numbers — 6 lines**
Always exactly 6 lines regardless of `<max layers>`, each holding
space-separated 16-bit values (`65535` = "no power plane on this layer"),
32 values total across the 6 lines (the last line holds only 2). Position
`i` in the flattened 32-value sequence corresponds to `layersCu[i]` — the
same `<Top> <Bot> <In2> <In1> <In4> <In3> … <In30> <In29>` ordering used
throughout KIUB (Section 10.3) — **not** DDF-native layer-number order.
Any position whose value is not `65535` is emitted as a solid copper zone
covering the full board-outline extents on that layer, using
`net = value + 1` (KiCad net 0 must stay unassigned).

End of header: the next line beginning with `*T` starts the technology
data block (Section 2).

[↑ Contents](#contents)

## 2. Technology data — `*T` records

`*T` records are dispatched by their sub-code, the character immediately
following `*T`. In practice they always appear in the order listed in
the table below (`*TP`, then `*TT`, `*TC`, `*TD`, `*T0`/`*T1`/`*T2`,
`*TS`) — KIUB's dispatch is sub-code-driven and does not itself require
this order, but every sampled file follows it:

| Sub-code | Meaning |
|---|---|
| `*TP <hex>` | Default padset bit pattern. Not used. |
| `*TT <code>, <width>, <clearance>` | Trace-code table entry. |
| `*TC <drill tolerance> <board clearance>` | Board-level clearance. |
| `*TD <code>, <diameter>` | Drill-code table entry. |
| `*T0 …` | Pad definitions, **Inner** layers. |
| `*T1 …` | Pad definitions, **Front**/Top layer. |
| `*T2 …` | Pad definitions, **Back**/Bottom layer. |
| `*TS <direction> <top size> <bottom size>` | Wave-solder direction. Not used. |

In some V4 files the `*TS` line is followed by a lone carriage return, so
the next record line starts with it (`*TS H 0 0` CR LF CR `*S…`): a reader
that splits on line feeds must strip carriage returns at both ends of a
line, or it misses the first shape record.

**`*TT <code>, <width>, <clearance>`**
Defines trace code `<code>` (0–31): default trace width and clearance for
that code, in DDF units. Referenced by `<trace_code>` fields throughout
`*L`, `*N`, and elsewhere.

**`*TC <drill tolerance> <board clearance>`**
Space-separated (not comma-separated, unlike most other `*T` records).
`<board clearance>` is stored as-is and used for two purposes: the
copper-to-copper clearance on generated power-plane zones (Section 1),
and the `.kicad_pro` `min_copper_edge_clearance` DRC constraint. It is
*not* the fallback used when an individual trace code's own clearance is
missing — that fallback is the separate, user-tunable `default_clearance`
setting (Section 10 fine-tuning defaults). If `<board clearance>` itself
is absent from the line, KIUB falls back to that same `default_clearance`
value. `<drill tolerance>` is read but not used.

**`*TD <code>, <diameter>`**
Defines drill code `<code>` (0–255) → hole diameter. Codes 0–239 are pad
drill codes; codes 240–255 are via drill codes (Section 7). A diameter
below the SMD-trick threshold (Section 10.4) is stored as the sentinel
`-1` for pad codes; via codes always keep their literal diameter (to allow
microvias). **Erratum:** the 1997 reference manual documents this field as
a *radius*; empirical testing confirms it is actually the *diameter*.

**`*T0` / `*T1` / `*T2 <code>, <x1>, <x2>, <y>, <radius>, <clearance>, <h.ap>, <v.ap>, <h.th.ap>, <v.th.ap>;`**
Comma-separated pad geometry for pad code `<code>` (0–255), one record per
sub-code per code — i.e. up to three independent pad definitions per code,
one for the Inner layer set, one for Front, one for Back. Field meaning:
- `<x1>, <x2>`: half-widths either side of the drill centre. Equal for
  centric (round/symmetric) pads; unequal for offset pads (e.g. teardrop
  or half-moon shapes used on some connector footprints), where KIUB
  derives a pad-to-hole X offset from `(x2 − x1) / 2`.
  Total copper width = `x1 + x2`.
- `<y>`: pad height (also doubles as the effective "pad diameter" input
  for round-pad codes, since KIUB always treats these as
  `roundrect` pads — see below).
- `<radius>`: corner rounding radius. KIUB derives a `roundrect_rratio`
  from `radius / min(x1+x2, y)`; a radius of `y/2` (or `(x1+x2)/2`,
  whichever is smaller) therefore yields a fully round pad.
- `<clearance>`: pad-specific copper clearance. A value of `0` is
  replaced with a fixed NPTH clearance fallback (0.15 mm) — Ultiboard
  apparently allows an explicit-zero clearance field to mean
  "use the board default", and KIUB substitutes a safe non-zero value
  rather than propagating a literal zero DRC clearance.
- `<h.ap>, <v.ap>, <h.th.ap>, <v.th.ap>`: aperture fields (Gerber
  photoplotter aperture selection, a DOS-era concept). Not used.

  The sub-code character (`0`/`1`/`2`) determines which of KIUB's three
  internal pad tables the record is stored in; KIUB **swaps DDF layers 0
  and 1** when storing (`*T0`→KIUB inner table, `*T1`→KIUB front table)
  so that its internal table order matches KiCad's own
  Front/Inner/Back convention, rather than the DDF's own Inner/Front/Back
  ordering.

  When a pad's `<y>` field is `0` but a real drill diameter exists for
  that code (not the `-1` sentinel of Section 10.4, which marks pads
  without a hole), KIUB treats it as an NPTH (non-plated) hole: the pad size is set
  equal to the drill diameter, since KiCad requires pad size ≥ drill size
  even for non-conducting holes.

  Once the technology block has been read — at the first record that is
  not a `*T` record — KIUB does a fix-up pass over all three pad tables:
  any drill code (0–239 only; via codes 240–255 are deliberately
  excluded) that has a defined diameter but no corresponding pad-size
  entry is filled in with a round pad exactly matching the drill diameter
  and the fallback NPTH clearance. This covers drill-only codes used
  purely for unpadded mounting/mechanical holes, which KiCad would
  otherwise report as holes without a pad.

  Ultiboard leaves every all-zero entry out when it writes a DDF, so a
  file need not hold every code: the tables may stop short of code 255
  (whenever via code 255 is undefined), and older files hold only a short
  range of codes. Codes absent from the file are treated as zero.

  A pad whose code has no size in the tables for a layer it is placed on
  is written with size 0 (KiCad pins such pads to 1 µm when it loads the
  file); KIUB reports this once per shape, naming the pad codes involved.

[↑ Contents](#contents)

## 3. Shape definitions — `*S<shape name>`

```
*S<shape name>
<ref_x> <ref_y> <ref_height> <ref_rotation> <ref_width> <ref_thickness>
<alias_x> <alias_y> <alias_height> <alias_rotation> <alias_width> <alias_thickness>
<Rth_junc_board>
<outline line-segment stream, comma-separated, terminated by ';'>
<pad descriptor lines, comma-separated, one per line, terminated by ';'>
<outline arc/circle lines, comma-separated, one per line, terminated by ';'>
;
```

The shape name is everything after `*S` on the header line with no
separator (e.g. `*SDIP8`, `*SBOARD`). A shape whose name ends in `.BAK`
(an Ultiboard-internal backup copy) is entirely skipped — its body lines
are passed over (they do not start with `*`) and discarded.

After the arcs, V4.80 and later write a fourth `;`-terminated section,
empty in every file examined (a single library shape carries a second
empty one); V4.60 files end the shape after the arcs. KIUB reads the
outline, pads and arcs and ignores whatever follows them up to the next
record.

**Reference / alias text descriptor lines** — space-separated, same six
fields in both lines: `<x> <y> <height> <rotation> <width> <thickness>`.
These describe how the shape's REFDES (reference) and alias
(part-name/library-reference) text are rendered when the shape itself is
used as a component footprint's floating `${shapename}` label (KIUB emits
only the reference descriptor's geometry as a `fp_text user` field; the
alias descriptor is read but not separately emitted at the shape level —
per-component REFDES/VALUE text is instead driven by the fields on the
`*C` record itself, Section 5). `<rotation>` is in DDF rotation units
(Section 10.2). `<thickness>` is a multiplier: stroke thickness =
`thickness × height / 1000` (this divisor is fixed by the DDF font
encoding itself, not a KIUB choice).

**`<Rth_junc_board>`** — one line, a single float (e.g. `0.000000`):
thermal resistance junction-to-board, a component thermal-modelling value
carried from Ultiboard's simulation features. Not used by KIUB (no KiCad
equivalent).

### 3.1 Outline line-segment stream

A flat, comma-separated stream of coordinate pairs, terminated by a
trailing `;` (the stream may span multiple physical lines; only the `;`
ends the section — an empty section is just a bare `;` line). Each `(x,
y)` pair encodes, via the parity of `x`, whether it starts a new
disconnected line segment or continues the previous one:

- If `x` is **even**: this pair is the continuation endpoint of a line
  segment that runs from the previous pair to this one — draw a line
  between them.
- If `x` is **odd**: this pair marks the *start point* of a new,
  disconnected segment (the coordinate's real X value is `x − 1`, i.e.
  odd-ness itself is the marker bit, not a meaningful unit — this is why
  outline coordinates of exactly `1` cannot occur in genuine Ultiboard
  files; Ultiboard's own save routine coerces a literal `1` to `0` before
  writing).
- If two consecutive pairs both have odd `x` (two segment starts with no
  intervening endpoint), the **first** of the two is discarded — this is
  a known artefact of the DDF outline encoding (see the Errata note in
  Section 10.7), not an intentional zero-length segment.

KIUB expands this stream into a plain list of `(x1, y1, x2, y2)` line
segments (four DDF-unit integers per segment) before further processing.
For non-`BOARD` shapes, each segment becomes an `fp_line` on
`{fp_side}.SilkS`. For `BOARD`, the raw segment list is instead handed to
the board-outline reconstruction pass (Section 10.6) rather than written
directly, since Ultiboard board outlines routinely contain both the
actual closed board edge and unrelated internal divider/partition lines
in the same stream.

### 3.2 Pad descriptor lines

One pad per line, comma-separated, each line ending `,` except the last,
which ends `;`:

```
<pad code>,<rotation>,<layerset hex>,<rel_x>,<rel_y>,<pin name>
```

- `<pad code>`: index (0–255) into the `*T0`/`*T1`/`*T2` pad tables
  (Section 2).
- `<rotation>`: DDF rotation units (Section 10.2).
- `<layerset hex>`: 8-hex-digit bitmask, same encoding as via layersets
  (Section 10.3). Which physical copper layers this pad instance appears
  on.
- `<rel_x>, <rel_y>`: pad centre position relative to the shape's own
  origin, in DDF units.
- `<pin name>`: pin/pad name string, verbatim (may be numeric, e.g. `1`,
  or a symbolic pin name inherited from the schematic side of the design,
  e.g. `A`, `K`, `GND`).

### 3.3 Outline arc/circle lines

One entry per line, comma-separated, each line ending `,` except the
last, which ends `;`:

```
<cx>,<cy>,<radius>,<start_angle>,<span_angle>
```

- `<cx>, <cy>`: centre point, DDF units.
- `<radius>`: DDF units.
- `<start_angle>`: degrees × 64, range roughly −360..+360; normalised to
  0–360 by `(360 + start_angle) % 360` before use.
- `<span_angle>`: degrees × 64, always positive. `<span_angle> == 23040`
  (360°×64) marks a full circle rather than an open arc — emitted as a
  `gr_circle`/`fp_circle` (closed, no open endpoints) instead of a
  `gr_arc`/`fp_arc`.

Arc angle convention: KiCad measures angles counter-clockwise from the
positive-X axis. Ultiboard's arc midpoint is derived by *negating* the
mid-angle (`ub_mid = -(ub_start + span/2)`), which always yields a
value ≤ 0 — this asymmetry between how start/end angles and the midpoint
angle are computed is an empirically-confirmed property of the format,
not a simplification.

### 3.4 The `BOARD` shape and board outline reconstruction

`*SBOARD` is not visually a component footprint — it is the single shape
that supplies the physical board edge. It is parsed with exactly the same
four-section grammar as any other shape (reference/alias text
descriptors, `Rth_junc_board`, outline lines, pads — normally empty for
`BOARD` — and arcs), but its outline/arc data is routed to the
snap-and-reconstruct pass described in Section 10.6 instead of being
emitted directly as `fp_line`/`fp_arc` entries, and its pad section is
ignored. See Section 10.6 for why this reconstruction step exists and
exactly how it separates the true closed board edge from any internal
divider lines sharing the same outline stream.

[↑ Contents](#contents)

## 4. Netlist records — `*N`

```
*N "<net name> <trace code> <xlo> <xhi> <ylo> <yhi> <xsum> <ysum> <pincount>;
```

Space-separated after the leading `*N `, terminated by `;`. Net records
appear once per net, assigning sequential net numbers in file order
(the first `*N` record encountered becomes KiCad net 1, etc. — KiCad net
0 is always reserved for the empty/unconnected net).

- `<net name>`: the net name token. Observed samples show only a leading
  `"` with no matching trailing quote (e.g. `"BOBO`) — KIUB strips any
  literal `"` characters found in this field but does not require a
  matched pair. `'`, `"`, and `\` are all sanitised out of the stored
  name (translated to `/` for `'` and `\`, removed entirely for `"`) since
  they are illegal in KiCad net-name strings. An empty name after
  stripping is replaced with a generated placeholder `SB$<n>`.
- `<trace code>`: the DDF trace code (Section 2, `*TT`) assigned to this
  net; used later to build one KiCad netclass per trace code. `0` means
  "no explicit routing-width constraint" and is not mapped to a netclass.
- `<xlo> <xhi> <ylo> <yhi>`: bounding box of the net's routed geometry.
  Not used.
- `<xsum> <ysum>`: coordinate sums (likely used internally by Ultiboard
  for centroid/ratsnest calculations). Not used.
- `<pincount>`: number of pins on this net. Not used.

[↑ Contents](#contents)

## 5. Component placement — `*C`

```
*C <refdes> /<alias> <shape name>
<x>,<y>,<rotation>,<name_x>,<name_y>,<name_rot>,<name_w>,<name_h>,<name_thick>,<alias_x>,<alias_y>,<alias_rot>,<alias_w>,<alias_h>,<alias_thick>
<x-force> <y-force> <temp case> <temp junc> <power> <Rth_junc_board> 0
<netnr> <layerset hex> <netnr> <layerset hex> ...
;
```

**Header line** — space-separated: `<refdes>` (reference designator, e.g.
`R1`), `/<alias>` (a slash-prefixed device/value alias — KIUB strips the
leading `/`), and `<shape name>` referencing an `*S` definition that must
appear earlier in the file.

**Position line** — comma-separated:
- `<x>, <y>`: absolute placement, DDF units, of the shape's own origin.
- `<rotation>`: DDF rotation units (Section 10.2). A **negative** value
  (equivalently: rotation° normalised < 0 or bit pattern reads negative)
  indicates the component is placed on the **Bottom** layer — DDF encodes
  layer side and rotation angle in the same field rather than as separate
  values. All geometry belonging to a bottom-side component (pads,
  outline, silkscreen, text) has its X coordinate mirrored when written to
  KiCad, and Front/Back copper layer references are swapped.
- `<name_x/y/rot/w/h/thick>`: REFDES text placement, same six-field
  layout as the shape's own reference text descriptor (Section 3), but
  here `x/y` are given in *absolute* board coordinates rather than
  shape-relative ones, and `rot` is *added* to the component's own
  rotation rather than being independent of it.
- `<alias_x/y/rot/w/h/thick>`: VALUE (alias) text placement, same
  convention as the REFDES fields above.
- **Erratum:** the 1997 reference manual documents the width/height pair
  in both the shape's own text descriptors and this component-placement
  line as `<height>, <width>`; empirical testing confirms the true field
  order is `<width>, <height>`.

**Force/thermal line** — space-separated:
`<x-force vector> <y-force vector> <temp case> <temp junc> <power>
<Rth_junc_board> 0` — thermal/mechanical simulation data carried from
Ultiboard. Not used.

**Pin/net lines** — one or more lines, each holding pairs of
space-separated tokens `<netnr> <layerset hex>` repeated for every pin on
the shape, in the same pin order as the shape's own pad descriptor list.
KIUB reads only the `<netnr>` value from each pair (every other
whitespace-separated token) — the per-pin layerset hex duplicated here is
not used, since the authoritative pad layerset already lives on the
shape's own pad descriptor.

The pin/net block is terminated one of two ways, and KIUB must detect
both:
- **V4.80 and later:** a bare `;` line explicitly ends the block.
- **V4.60:** no terminator at all — the next `*`-prefixed record simply
  begins immediately. KIUB detects this by pushing the unexpected `*`
  line back into the main dispatch loop rather than consuming it as pin
  data.

[↑ Contents](#contents)

## 6. Copper subrecords — `*L`

All `*L` records share the `*L<sub-code>` dispatch pattern (`T`, `V`,
`A`, `P`):

### 6.1 `*LT` — Orthogonal/45° traces

```
*LT <layer> <coord1>
<coord2> <coord3> <netnr> <trace_code> <trace_type> <orientation>
...
<coord2> <coord3> <netnr> <trace_code> <trace_type> <orientation>;
```

Header: `<layer>` is a 1-based DDF-native layer bit index (Section 10.3);
`<coord1>` is the single coordinate shared by every following segment on
this line's orientation (see below). Each data line represents one trace
segment on that same layer:
- `<coord2>, <coord3>`: the segment's second coordinate pair,
  interpretation depending on `<orientation>`.
- `<netnr>`: net number (mapped through `netnr+1`, with the sentinel
  `65535` → KiCad net 0).
- `<trace_code>`: index into the `*TT` trace-width/clearance table.
- `<trace_type>`: `0` = fixed, `128` = variable — an autorouter hint, per
  the Ultiboard user guide: a `FIXED` trace is left untouched by the
  autorouter's via-reduction pass; a `VARIABLE` trace may be rerouted by
  the autorouter to reduce or eliminate vias. It does not affect trace
  width. Read but not distinguished by KIUB (the segment's copper width
  always comes from the `<trace_code>` lookup, regardless of this flag).
- `<orientation>`: `1` = horizontal, `2` = vertical, `4` = north-east
  45° diagonal, `8` = south-east 45° diagonal. For horizontal/vertical,
  `<coord1>` is the fixed Y/X respectively and `<coord2>/<coord3>` are
  the segment's two endpoints along the free axis. For the two diagonal
  cases, `<coord1>` is one axis-aligned reference coordinate and the true
  endpoints are derived by splitting the difference between `<coord1>`
  and each of `<coord2>`/`<coord3>` in half (a 45°-diagonal-from-two-
  axis-aligned-references encoding).

### 6.2 `*LV` — Arbitrary-angle vector traces (V4/V5 only)

```
*LV <layer> <x1> <y1> <x2> <y2> <netnr> <trace_code> <trace_type>
```

A single-line record (no repeated body, unlike `*LT`) for a trace segment
at any angle, given directly as absolute start/end coordinates. Same
field semantics as `*LT`'s per-segment fields.

This record type is specific to V4/V5. `*LV` means something different
in V2/V3 (the Vertical orthogonal trace record — see Section 9); no
V2/V3 record maps onto this section.

### 6.3 `*LA` — Arc trace

```
*LA <layer> <cx> <cy> <radius> <start_angle> <span_angle> <netnr> <trace_code> <trace_type>
```

Same angle encoding as shape outline arcs (Section 3.3). `<trace_code> ==
65535` is a sentinel meaning "no trace-width entry" (observed on
zero-width construction/reference arcs) — KIUB substitutes a
near-zero-but-nonzero width (`0.000001` mm) in this case, since writing a
literal zero width causes KiCad to silently rewrite the arc to a default
0.1 mm width on load, which would misrepresent the source data.

### 6.4 `*LP` — Polygon fill zone

```
*LP <layer> <netnr> <hatch_pattern> <hatch_dist> <trace_code> <clearance> <flag1> <flag2>
<outline x,y pairs, space-separated>[:|;]
[<pre-rendered hatch-fill segment coordinates> ...;]
```

Header, space-separated:
- `<layer>`: 1-based DDF-native layer bit index.
- `<netnr>`: net number for the fill/pour.
- `<hatch_pattern>`: Ultiboard supports 7 hatch styles (solid, `---`,
  `|||`, `+++`, forward-slant, back-slant, `XXX`); KiCad's zone fill only
  supports 3 (solid, `+++`/cross-hatch, and an angled single-direction
  hatch). Codes `3` (0°) and `12` (45°) map to KiCad's angled hatch mode
  (`(mode hatch)`, angle = `(pattern − 3) × 5`); every other pattern code
  falls back to solid fill.
- `<hatch_dist>`: Ultiboard stores this as the hatch line **centre-to-
  centre** spacing; KiCad's `hatch_gap` wants the *gap* between hatch
  lines, so KIUB converts via `hatch_dist − trace_width`.
- `<trace_code>`: trace code supplying the fill's line width.
- `<clearance>`: fill-to-copper clearance, also reused as the thermal-
  relief air gap.
- `<flag1>, <flag2>`: read but not used.

**Body — confirmed single-line-only outline read, with a documented
quirk:** the outline point list is a single line of space-separated `x,
y` pairs. KIUB reads exactly **one** such line per `*LP` record and stops
as soon as that line contains a `:` or a `;` anywhere in it. In every
sampled real-world polygon, this terminator line is immediately followed
by one or more **further** lines of coordinates — confirmed by direct
inspection to be Ultiboard's own **pre-rendered hatch-fill vector
segments** (the literal hatch lines Ultiboard itself computed and cached
in the file), not additional outline points. Because these lines do not
begin with `*`, KIUB's top-level dispatch loop silently skips them as
non-record lines rather than raising an error, and they play no further
part in the conversion. This is a deliberate simplification, not an
oversight: KIUB only needs the polygon *outline* — KiCad recomputes its
own zone fill from scratch (`Edit → Fill All Zones`, KiCad's own hatch
pattern rendering does not need Ultiboard's cached fill geometry, and in
fact cannot represent all of Ultiboard's hatch styles anyway, per the
`<hatch_pattern>` note above).

A terminator of `:` (rather than `;`) on the outline line signals that
hatch-fill data follows; a bare `;` signals no fill data follows (the
outline line is the entire record body). Either way, KIUB's behaviour is
identical: read one line, then stop.

[↑ Contents](#contents)

## 7. Vias — `*V`

```
*V <x>
<y> <netnr> <pad_code> <layerset hex> <rot> <shift> <via_index> <glue_flag>
...
<y> <netnr> <pad_code> <layerset hex> <rot> <shift> <via_index> <glue_flag>;
```

Header: `<x>` is the X coordinate shared by every via record on this
line (mirroring `*LT`'s "shared coordinate" header pattern). Each data
line is one via:
- `<y>`: the via's Y coordinate (paired with the header's shared `<x>`).
- `<netnr>`: net number (`netnr+1`, `65535`→0).
- `<pad_code>`: index into the pad tables (Section 2), always ≥ 240 for a
  genuine via code by Ultiboard convention, though KIUB does not enforce
  this — it simply looks up whatever code is given.
- `<layerset hex>`: 8-hex-digit bitmask (Section 10.3) giving the set of
  copper layers this via physically spans. If the mask includes both
  Front and Back, KIUB emits a full through-via; otherwise it emits a
  `blind` via spanning only the outermost two layers actually present in
  the mask (i.e. any span not touching both outer layers is treated as
  blind/buried, using only the two extreme layers of the mask — the
  reference manual's layer-lamination diagrams in Section 1 show how
  Ultiboard restricts which such spans are physically drillable, though
  KIUB does not itself validate legality against the lamination string).
- `<rot>`: pad rotation, relevant only if the via's pad code (Section 2)
  defines a non-round pad shape. KiCad's via model is always round, so
  KIUB discards this field regardless — see Section 10.7.
- `<shift>`: a user/autorouter routing option that nudges the via
  slightly off the routing grid, used to fit vias more densely than the
  grid spacing would otherwise allow. Not related to via pad shape. Not
  used by KIUB.
- `<via_index>`: sequence index. Not used.
- `<glue_flag>`: purpose not documented in the reference manual. A via is
  not a placeable/orientable component, so despite the field's superficial
  resemblance to the glue-dot flags seen elsewhere in pick-and-place
  contexts, that reading is speculative and not confirmed. Not used.

Per-layer annular ring: KIUB looks up the via pad code's `Ysize` in all
three of its internal pad tables (front/inner/back) and only emits a
KiCad `padstack` block (differing per-layer annular sizes) when the inner
or back size differs from the front size — otherwise a single uniform
`size` is used, matching ordinary (non-padstack) KiCad vias.

Via codes lacking a drill or a pad: Ultiboard accepts a via whose code
has a pad but no drill, or a drill but no pad, and connects the via's
layers either way. KiCad connects layers only through a hole, needs the
via larger than its drill, and replaces a zero drill with 0.3 mm when it
loads the file. KIUB therefore writes:

| Via code has | KiCad via |
|---|---|
| drill and pad | as defined |
| pad, no drill | drill 0.001 mm — the layers stay connected; the hole is not meant to be drilled, which the manufacturer must be told |
| drill, no pad | pad = drill + 0.01 mm (the minimum ring of padless PTH holes); the board's minimum via annular width allows for it |
| neither | not written |

Each such via is reported with its position and code. The default via
size and drill of the generated `.kicad_pro` come only from via codes
holding both a drill and a pad.

[↑ Contents](#contents)

## 8. Text — `*X`

```
*X <x> <y> <height> <width> <thickness> <rotation> <layer> <text string>
```

Space-separated header fields, followed by the text string as the literal
remainder of the line (extracted as raw bytes before any character-set
decoding, so multi-byte or high-CP437-range characters survive intact —
see Section 10.5).

- `<x>, <y>`: absolute position, DDF units.
- `<height>`: DDF units; converted via an empirically-fitted ratio
  (`height ÷ 1.208`) chosen so the rendered KiCad text visually matches
  Ultiboard's own on-screen rendering. The ratio was fitted against the
  **DejaVu Sans Mono** font specifically (the recommended substitute font,
  Section 10.5) — KiCad's own default font (NewStroke) closely matches
  Ultiboard's proportions on its own, so this correction factor mainly
  matters when DejaVu Sans Mono is selected. **Ultiboard Stroke** (Section
  10.5) uses its own separate ratio (`height ÷ 1.0`) instead of this
  default, since it was built to match Ultiboard's native glyphs directly.
- `<width>`: DDF units; similarly scaled (`width × 1.186`).
- `<thickness>`: multiplier, same convention as shape text
  (`thickness × height ÷ 1000`).
- `<rotation>`: DDF rotation units (Section 10.2). A negative value means
  the text is mirrored (placed as if viewed from the back of the board);
  KIUB negates the rotation again when mirroring so the text reads
  correctly in KiCad's own mirror convention.
- `<layer>`: `0` means silkscreen — positive rotation → `F.SilkS`,
  negative rotation → `B.SilkS`. Any other value `n` is a 1-based
  DDF-native copper layer bit index (Section 10.3); if `n` is even and
  positive, or the rotation was negative, the text is additionally
  mirrored (even DDF-native layer numbers are Ultiboard's own
  "bottom view" convention, matching the layer-numbering scheme
  documented in Section 10.3).

[↑ Contents](#contents)

## 9. DDF V2.x / V3.x legacy format

V2.x and V3.x DDF files use a materially different, older wire format —
narrower/differently-ordered fields, no separate text-width storage, a
differently bit-mapped pad/via layerset field, and different record
framing for the header, shapes, components, and traces. Rather than
maintaining a second parallel parser, KIUB pre-converts any V2/V3 file to
an in-memory V4.60 equivalent (via `kiub_v2v3.py`) before handing it to
the main V4/V5 parser described in Sections 1–8. Each subsection below
gives the native V2/V3 record structure alongside what it becomes after
conversion, plus the confirmed conversion specifics.

### 9.1 Header

Native V2/V3 header:

```
*P <customer name>
<major> <minor>
<width>, <height>, <grid>, <field4>;
```

The V2/V3 header shares only its first two lines (`*P <name>` and the
`<major> <minor>` version line) with V4/V5 — the version line's own
content is discarded by the pre-converter (both fields, replaced
unconditionally with the literal `4 60`) and used only upstream, by
`open_ddf` (Section 1), to decide whether pre-conversion is needed at
all. Everything else about the V2/V3 header is different:

- **The bounds line is not a set of outline corner coordinates.** Where
  V4/V5 has `<x0>, <y0>, <x1>, <y1>, <grid>, <swap level>[, <routing
  layers>], <max layers>;`, V2/V3 has the much shorter, differently-scoped
  `<width>, <height>, <grid>, <field4>;` line shown above (e.g.
  `4724, 3060, 60, 0;`). Only the first two fields are read by the
  pre-converter, and they are a plain board **width** and **height** (in
  the shared database-unit system, Section 10.1) — not a pair of corner
  coordinates. Per the Ultiboard reference manual, the third field is the
  same `<grid>` field as V4/V5's own bounds line (Section 1) — in the
  file's native unit system (Section 10.1), the same as every other
  coordinate/dimension field in the file. It is not itself read by the
  pre-converter's board-geometry handling, but it is read separately, as
  this file's declared default routing grid step (Section 9.11 covers
  what that's used for and why it isn't necessarily reliable as a stand-in
  for what any specific already-drawn feature actually used). `<field4>`
  remains unconfirmed (values seen: `0` and `1` across two sample boards,
  plausibly a `<swap level>`-equivalent flag, matching V4/V5's field
  ordering immediately after `<grid>`) — but is not read by the
  pre-converter either way.
- **No layer-lamination string, no reference-point line, and no router-
  options/layer-direction-flags lines exist in the V2/V3 source at
  all.** The pre-converter fabricates fixed placeholder content for each
  of these — a constant 16-physical-layer lamination string
  `(|+|+|+|+|+|+|+|+|+|+|)`, a reference point of `0, 0`, a router-
  options line of `240 0 0 15 30 1`, and a layer-direction-flags line of
  32 repeats of `1 2` — none of which is derived from the source file.
  This is safe only because KIUB itself never reads any of these fields
  back out (Section 1 marks every one of them "read but not used").
  `<max layers>` is the one exception worth calling out separately: the
  pre-converter hardcodes it to **`22`**, and per the Ultiboard reference
  manual, 22 layers is indeed the documented maximum the V2/V3 format
  supports — so, unlike the other fabricated header fields, this value is
  very likely an accurate representation of the format's real layer
  ceiling rather than an arbitrary placeholder, even though no single
  V2/V3 board necessarily uses all 22.
- **The 6-line power-plane block is always emitted as all-`65535`**
  (no power planes), since V2/V3 has no equivalent data to carry over.

### 9.2 Shapes — single, 4-field text descriptor; no arc section

Native V2/V3 shape:

```
*S<shape name>
<x> <y> <height> <rotation>
<outline line-segment stream, comma-separated, terminated by ';'>
<pad descriptor lines, comma-separated, one per line, terminated by ';'>
```

A V2/V3 shape header is followed by exactly **one** text-descriptor line,
not two, and that line has only **4** space-separated fields —
`<x> <y> <height> <rotation>` — rather than V4/V5's 6-field
`<x> <y> <height> <rotation> <width> <thickness>` (Section 3). There is
no separate alias-text descriptor line at all. During conversion, this
single line is used to synthesize *both* of V4/V5's descriptor lines
(reference and alias are given identical geometry), with `<width>`
estimated from `<height>` and `<thickness>` fixed to `100`
(Section 9.9 below). If a shape's stored `<x>,<y>` is `(0, 0)`, the
pre-converter additionally recentres it to the midpoint of the shape's
own outline bounding box — `(0, 0)` in V2/V3 apparently serves as a
"use the shape's geometric centre" sentinel rather than a literal origin
placement.

**A V2/V3 shape body has only two sections — outline, then pads — and
no arc/circle section at all.** Confirmed both from the pre-converter's
own parsing (which only ever splits a shape's body into an "outline"
part and a "pads" part, toggling once at the first `;`) and directly
from sample data (e.g. `*SRES12`'s body is exactly one outline line
ending in `;` followed by two pad lines ending in `;`, with the next
shape's `*S` header immediately following — no third arcs section
appears anywhere). To satisfy V4/V5's three-section shape grammar
(Section 3), the pre-converter appends an empty arcs section (a bare
`;`) after the real/converted pads section — meaning **no V2/V3 shape
can ever carry a genuine arc or circle outline primitive**; any curved
footprint edges in a V2/V3 design are necessarily approximated with
straight outline segments, or via a pad's own rounded-corner radius
(Section 2), rather than a standalone arc.

`*SNO_SHP` — a specially-named "no shape" placeholder used by V2/V3 for
components without a meaningful physical footprint — is a V2/V3-only
construct with no V4/V5 equivalent (absent from every sampled V4/V5
file, and never referenced by `kiub.py` itself). It does carry real,
if minimal, outline/pad geometry in the V2/V3 source (a small
quadrilateral outline plus a handful of zero-size pads at drill code
`0`), but the pre-converter discards that geometry entirely and
substitutes a fixed, built-in flag-shaped marker shape instead — every
`*SNO_SHP` instance in the output is visually identical regardless of
what the source file actually stored for it.

### 9.3 Components — no shape name on the header, no force/thermal line

Native V2/V3 component:

```
*C <refdes> /<alias>
<shape_id>,<x>,<y>,<rotation>,<text_offset_x>,<text_offset_y>,<f1>,<f2>,<f3>
<netnr> <layerset hex> <netnr> <layerset hex> ...
```

**The header line carries no shape-name field at all** — unlike V4/V5's
`*C <refdes> /<alias> <shape name>` (Section 5), V2/V3's header stops
after `<refdes> /<alias>` (confirmed sample: `*C L5 /0.15UH`). The
component's shape is instead identified purely through `<shape_id>`, the
first field of the position line: a 0-based index into the shapes
already declared earlier in the file, in file order — not a name
reference. The pre-converter resolves this index against its own running
list of parsed shapes and appends the resulting shape name directly onto
the V4/V5 header line it emits, so the shape-name field only ever exists
in the *converted* output, never in the V2/V3 source.

The position line's remaining fields, comma-separated (confirmed sample:
`0,3720,900,0,0,0,0,65535,65535`): `<x>, <y>` and `<rotation>` are the
placement (rotation using the V2/V3 0–7 code, Section 10.2), and
`<text_offset_x>, <text_offset_y>` are added to the shape's own stored
text position to get the component instance's absolute REFDES text
position — these two offset fields are stored as **unsigned 16-bit
values needing two's-complement correction** (any raw value above 32768
has 65536 subtracted from it before use), unlike V4/V5's already-signed,
wider-range position fields. `<f1>`–`<f3>` are present but never read.

Critically, **V2/V3 has no separate force/thermal/power-simulation line
at all** — the position line is immediately followed by the pin/net
lines. V4/V5's format requires a third line here (Section 5); the
pre-converter synthesizes a fixed placeholder `0,0,0,0,0,0,0` line to
satisfy that grammar, since no such data exists in the V2/V3 source to
carry over.

**REFDES/VALUE text rotation is always emitted as `0°`, independent of
the shape's own rotation.** The pre-converter computes the emitted
text-rotation field as `ROT_MAP.get(self.shapes[shape_id]['Rot'], 0)` —
looking up the shape's *already-converted* rotation value (not a raw
0–7 code) against `ROT_MAP`'s 0–7 keys, which never matches for any
rotation other than 0°, so the lookup's default (`0`) is what actually
gets used in every case. This field is **added to the component's own
placement rotation** by `kiub.py`'s own `*C` handler (Section 5) — it is
not an absolute/independent angle — so emitting `0` here doesn't freeze
the text in a fixed landscape orientation; it means the text simply
**rotates in lock-step with the component itself**, carrying no
additional per-shape rotation offset on top of that. This is intentional
rather than a defect.

### 9.4 Pad/via layerset bit-position offset

V2/V3's per-pad/per-pin/per-via layerset field is the same 8-hex-digit
width as V4/V5's (Section 10.3) — it is not a narrower field — but its
bit-to-physical-layer assignment is offset from V4/V5's own convention,
and the pre-converter must remap between the two:

- **Pad descriptors (in `*S` shapes) and component pin/net lines (in
  `*C` records)** have every layerset value right-shifted by 12 bits
  unconditionally (`value >> 12`) before being re-emitted as a V4/V5
  field. This is confirmed correct down to the individual bit, not just
  for the "all layers" sentinel: a dedicated test board built with one
  pad per physical layer (`Top`, `Bottom`, `Inner1`…`Inner18` — the
  maximum a V3 board allowed to be created in the Ultiboard UI at test
  time) and opened directly in Ultiboard V5/V5.72 shows each source
  layer landing on its expected V4/V5 counterpart — including each
  inner-layer pair coming out swapped in exactly the same order V4/V5
  itself uses (e.g. the pad drawn on `Inner1` reads back as `Inner2`,
  `Inner3` reads back as `Inner4`, and so on) — matching the `>> 12`
  shift bit-for-bit across all 20 layers tested. This 20-layer test
  ceiling is a separate figure from the `<max layers>` value of `22`
  documented in the reference manual and hardcoded by the pre-converter
  (Section 9.1) — the test simply didn't probe layers 19–20 (`In19`,
  `In20`) specifically, not that they're confirmed unsupported.
- **Via records (`*V`) use the same layerset packing as pads** —
  Ultiboard doesn't distinguish via layersets from pad layersets by bit
  layout, only by which record they appear in, so the same `>> 12` shift
  applies unconditionally to every via layerset value, exactly as it
  does for pads/pins above.
- Every V4/V5 via record also gains four trailing fields
  (` 0 0 0 1`) appended by the pre-converter — the V2/V3 source via line
  has no equivalent of V4/V5's `<via_index>`/`<glue_flag>` fields
  (Section 7), so fixed placeholder values are used.

### 9.5 `*T` records — drill/pad tables

Native V2/V3 technology records:

```
*TP <hex>
*TT <code>, <width>, <clearance>
*TC <board clearance>
*TD <code>, <diameter>
*T0 <code>, <x1>, <x2>, <y>, <radius>, <clearance>
*T1 <code>, <x1>, <x2>, <y>, <radius>, <clearance>
*T2 <code>, <x1>, <x2>, <y>, <radius>, <clearance>
```

- **`*TP`** is unconditionally normalized to a fixed `*TP ffffffff` on
  conversion, discarding whatever value the V2/V3 source actually held
  (confirmed sample source value: `*TP fffff000` — a different value
  entirely from the fixed constant that replaces it).
- **`*TT`** has the same 3-field structure as V4/V5's (Section 2) and is
  passed through unchanged. V2/V3 only ever defines trace codes 0–15,
  though — when the pre-converter sees a `*TC` line, it additionally
  backfills trace codes 16–31 with a fixed default entry
  (`*TT <code>, 0, 30`) for every code in that range, since V4/V5's own
  trace-code table (Section 2) spans 0–31 and nothing in the V2/V3
  source ever defines the upper half of it. This backfill isn't just
  cosmetic: `kiub_v2v3.py` can be run standalone (outside `kiub.py`
  entirely) to produce a genuine V4.60 DDF file, and that file needs to
  be correctly readable if opened directly in real Ultiboard V5 —
  without the backfill, any reference to trace codes 16–31 in such a
  file would be undefined there.
- **`*TC`** has only **one** field in V2/V3 — a bare `<board clearance>`,
  no leading `<drill tolerance>` (confirmed sample: `*TC 2`) — and is
  passed through to the output completely unmodified, still as a
  single-field line. Since V4/V5's own `*TC` handler (Section 2) treats
  a missing second field as "no board clearance given" and falls back to
  the `default_clearance` setting, this means **every V2/V3 board's
  board-level clearance is, in effect, always the `default_clearance`
  fallback** — no V2/V3 source file can supply a genuine per-board
  clearance value through this field.
- **`*TD`** drill-code diameters in V2/V3 are stored in
  **deci-millimetres (0.1 mm units)** — not the shared 1/1200-inch
  database-unit system that every other V2/V3 coordinate field uses
  (Section 10.1). The pre-converter applies an explicit
  `value × 1200 / 254` conversion (deci-mm → 1/1200-inch database units;
  `254` being `25.4 mm/inch × 10`, i.e. the deci-mm-per-inch constant)
  before re-emitting the value. This is confirmed both by the conversion
  factor itself and by the sample data — raw V2/V3 `*TD` values of
  `6, 9, 11, 13, 15, 18` (V2.DDF) and `4, 8, 10, 15, 35, 55` (V3.DDF)
  only make sense as plausible drill diameters (0.6 mm, 0.9 mm, 1.1 mm …
  5.5 mm) under a 0.1 mm-per-unit reading; read as database units they
  would imply hole diameters on the order of a few hundredths of a
  millimetre, which is not physically sensible. This unit quirk is
  specific to `*TD`'s diameter field — `*T0`/`*T1`/`*T2` pad-geometry
  fields use the ordinary shared database-unit system like everything
  else.
- **`*T0`/`*T1`/`*T2`** have only **5** comma-fields after the code —
  `<x1>, <x2>, <y>, <radius>, <clearance>` (confirmed sample:
  `*T0 0, 24, 24, 48, 24, 15`) — with none of V4/V5's four aperture
  fields (`<h.ap>, <v.ap>, <h.th.ap>, <v.th.ap>`, Section 2) present at
  all. The pre-converter appends four `0` placeholders to pad the record
  out to V4/V5's 9-field shape, since those aperture fields are unused by
  KIUB in either version anyway.

**No separate via-code list (`*TD`/`*T0`/`*T1`/`*T2` alike).** Unlike
V4/V5, where drill codes 240–255 are conventionally reserved for vias and
0–239 for pads (Section 2), V2/V3 has only a single 0–15 drill/pad-table
numbering space shared by pads and vias alike, and a `*V` record's
`<pad_code>` field (Section 7) references that same low-numbered table
directly — confirmed by sample V2/V3 via records whose `<pad_code>`
field reads `0` (`*V 1020` / `2760 29 0 fffff000;`), i.e. an ordinary
low pad code, not a value anywhere near 240.

To bridge this into V4/V5's own two-range convention, the pre-converter
duplicates *every one* of the four `*T` sub-record types —
`*TD`, `*T0`, `*T1`, and `*T2` alike — for codes 0–15 up to 240–255 as
well, so every low code that might be used by a via has a corresponding
high-numbered twin present in the output file. As with the `*TT`
backfill above, this duplication is functionally necessary rather than
merely cosmetic: since `kiub_v2v3.py`'s output can be opened directly in
real Ultiboard V5 (not just consumed by `kiub.py`), any via drill/pad
lookup that Ultiboard itself resolves against the 240–255 range needs a
matching entry there. A converted `*V` record's `<pad_code>` field is
correspondingly offset by `+240` as well, so it points at the
newly-duplicated high-numbered twin rather than the original low code —
matching V4/V5's own convention (Section 2) that a `*V` record's pad
code is expected to be ≥ 240, which matters for the same standalone-use
reason: a via edited directly in Ultiboard after conversion needs its
pad code to resolve idiomatically, not merely validly.

### 9.6 `*V` records — no `<rot>`/`<shift>`/`<via_index>`/`<glue_flag>` fields

Native V2/V3 via record:

```
*V <x>
<y> <netnr> <pad_code> <layerset hex>
...
<y> <netnr> <pad_code> <layerset hex>;
```

(Confirmed sample: `*V 120` / `540 171 0 fffff000;`.)

A V2/V3 `*V` data line has only **4** fields — `<y> <netnr> <pad_code>
<layerset hex>` — compared to V4/V5's 8 (Section 7): no `<rot>`,
`<shift>`, `<via_index>`, or `<glue_flag>` at all. The first four fields
that *are* present share the same meaning as their V4/V5 counterparts
(Section 7); pad code offsetting (`+240`) follows the same rule described
in Section 9.5 above.

The missing `<shift>` field matters for one specific reason: Ultiboard
allows a 4 quadrant via shift (depending on the via size, up to 11.67mil),
evidently to fit vias more densely than the routing grid would otherwise
allow (V4/V5's own `<shift>` field, Section 7, appears to record this same
adjustment directly, though KIUB does not read it there either). V2/V3
has no field recording this at all, so a V2/V3 file's via position simply
*is* wherever this nudge left it, with nothing in the record indicating
how far off-grid it landed or why.

This is relevant to KIUB specifically because of how it recovers V2/V3
diagonal traces and chamfered corners (Section 9.11/9.12 below): both
work from the coordinates of adjacent `*LH`/`*LV` trace segments, and a
nudged via's position can differ from where those adjacent segments'
own endpoints would otherwise imply it should be. Empirically, across a
range of via pad sizes, the nudge distance (identical along both axes)
was found to follow `min(0.35 × pad_diameter_mil, 11.67 mil)` — i.e. it
scales with the via's own pad size up to a fixed ceiling, not with drill
size or trace width. Pad diameter is looked up per the physical layer in
question (Front/Back/Inner can each declare a different diameter for the
same pad code — Section 9.5) so that this stays correct even where those
diameters actually differ. Both the staircase-recovery and chamfering
features use this to keep a safe distance from any via for the net in
question before modifying a corner near one, rather than relying on the
corner's own coordinates matching the via's recorded position exactly.

### 9.7 Text records — different field layout

Native V2/V3 text record:

```
*X <x> <y> <height> <layer> <rotation> <text>
```

A V2/V3 `*X` text record has only **5** numeric fields, in a different
order from V4/V5's 7-field record (Section 8) — note `<layer>` precedes
`<rotation>` here, the reverse of V4/V5's `... <rotation> <layer> ...`
ordering, and there is no `<width>` or `<thickness>` field at all (both
are synthesized: width copied from height, thickness fixed to `100`).
`<layer>` is also offset by one from V4/V5's convention — the
pre-converter subtracts 1 from the V2/V3 value before emitting it, so
V2/V3 layer `1` becomes V4/V5 layer `0` (silkscreen, Section 8).

### 9.8 Rotation encoding differs entirely

V2/V3 use a plain `0`–`7` code — side and angle share one small code, not
a signed degrees×64 value — rather than mapping simply to
`code × 90°`. See the full table in Section 10.2: the top-side codes
(`0`–`3`) run their angle backwards (0°, 270°, 180°, 90°) while the
bottom-side codes (`4`–`7`) run forwards (0°, 90°, 180°, 270°). The
pre-converter maps this through a fixed lookup table into the equivalent
V4/V5 degrees×64 encoding (including the sign convention for
bottom-layer placement, Section 10.2). This code is used identically for
shape text rotation, component placement rotation, and text-record
rotation.

### 9.9 No text width/thickness storage anywhere

Consistent with Sections 9.2 and 9.7 above: nowhere in the V2/V3 format
is a text width or stroke thickness stored — only height. Both are
estimated from height using fixed, user-tunable ratios (`width ≈ height
× 0.8`, `thickness ≈ height × 0.1667` by default). This is a genuine
information gap in the V2/V3 format, not a parsing simplification —
there is no lossless way to recover a V2/V3 file's original text
width/thickness, since Ultiboard itself only ever derived it from height
at render time.

### 9.10 Known bug avoided by pre-converting rather than round-tripping through Ultiboard

Ultiboard V5.72 itself is confirmed (by direct testing) to mis-handle
V2/V3 pad drill codes when loading a V2/V3 file directly — KIUB's own
pre-converter avoids this bug entirely by working from the raw V2/V3
source itself rather than relying on Ultiboard to open and re-save the
file as V4/V5 first.

### 9.11 `*LH`/`*LV` (orthogonal traces) — see Section 6.1

Native V2/V3 orthogonal trace record:

```
*LH <layer> <coord1>
<coord2> <coord3> <netnr> <trace_code><F|V>
...
<coord2> <coord3> <netnr> <trace_code><F|V>;
```

(`*LV` shares the identical structure — only the record tag differs.)
Confirmed sample: `*LV 1 2340` / `1500 1620 30 3F;`. Note the trailing
`F` (Fixed) or `V` (Variable) suffix is concatenated directly onto the
trace-code number with no separating space, unlike V4/V5's own separate
numeric `<trace_type>` field (Section 6.1).

In V2/V3, `*LH` and `*LV` are the **Horizontal** and **Vertical** trace
records respectively — both share the same header layout as V4/V5's
`*LT` (`<layer> <coord1>`), and each data line ends with that trailing
`F`/`V` suffix denoting Fixed or Variable (the V2/V3 equivalent of
V4/V5's numeric `<trace_type>` code, Section 6.1). The pre-converter
strips that suffix, appends the equivalent numeric `<trace_type>` (`0`)
and `<orientation>` code (`1` for `*LH`, `2` for `*LV`) as separate
space-separated fields, and emits the result as a single, unified `*LT`
record — folding V2/V3's separate H/V record types into V4/V5's one
orthogonal-trace record type.

**Note the naming collision:** V2/V3's `*LV` (Vertical trace) and
V4/V5's `*LV` (arbitrary-angle Vector trace, Section 6.2) share the same
two-character tag but mean entirely different things — V2/V3 has no
record equivalent to V4/V5's `*LV`/vector trace at all. Section 6.2
above describes V4/V5's `*LV` only; it does not apply to V2/V3 source
files.

**`*LT` (Section 6.1) is not implemented in V2/V3 at all** — there is no
diagonal/45° trace record in the V2/V3 DDF data. A diagonal trace drawn
in V2/V3 is stored in the DDF itself only as a "staircase" of ordinary
`*LH`/`*LV` orthogonal segments approximating the diagonal.

The pre-converter reconstructs the diagonal itself. `*LH`/`*LV` records
for the whole file are buffered rather than processed one at a time —
they are stored sorted by column/row coordinate, not by trace or drawing
order, so a single physical trace's segments are not generally adjacent
in the file — and reassembled into connected polylines (open, two-ended
traces) or loops (closed shape/pad/via outlines) via shared endpoints.

For an open trace, two adjacent segments (one horizontal, one vertical)
are treated as one 45° step whenever they are exactly equal in length.
A single such pair is enough to be recovered as a diagonal on its own,
subject to a length cap: an isolated pair of equal-length perpendicular
legs is geometrically indistinguishable from an ordinary right-angle
routing corner that merely happens to have two equal-length legs, so
without a cap the two are conflated and an ordinary corner is
misidentified as a one-step staircase. The cap is this file's own
declared default grid step (Section 9.1), always capped again at a
fixed 25 mil ceiling regardless of that declared value or of an explicit
override — real staircases are drawn along the routing grid, and on a
densely populated board that grid step already reflects the board's own
copper clearance, so a genuine staircase step is not expected to exceed
it; the 25 mil ceiling exists as a backstop independent of any
particular file's grid. A run of two or more consecutive such pairs
needs no separate justification beyond the length cap — coincidental
equal-length matches repeating consistently across several consecutive
corners are vanishingly unlikely from ordinary routing.

A recovered diagonal is placed through the midpoints of its first and
last unit segments, rather than through the staircase's own corner
vertices — this point is always exactly the same regardless of which
side of the staircase the original corners fell on, so it is correct
without needing to determine that side at all. Where a recovered run
meets a connecting segment beyond its own ends, that connecting segment
is shortened to meet the diagonal directly rather than leaving a
separate stub; if the connecting segment is itself short enough to be
fully consumed this way from both its own ends, it disappears entirely
and the diagonal continues straight through, exactly as if the
connecting segment's own length had also been part of the staircase.

V2/V3 coordinates are whole database units (Ultiboard's grids are whole
numbers of units: 12.5 mil = 15, 0.83 mil = 1), but with an odd step
length these midpoints, and the half-step shortening of a connecting
segment, fall on half units. Such a diagonal is moved half a unit, its
two ends each along the segment it lies on: it stays exactly 45° and
connected, every other segment stays horizontal or vertical, and no
corner of the original trace moves.

A recovered run is never allowed to pass through a point where a via for
its net sits (Section 9.6) — it is split there instead, so the via
keeps a real, unmodified segment connecting to it on both sides.

Segments are merged per trace code. Where traces of different trace
codes of the same net and layer meet — one trace's end on a corner or
end of the other, or anywhere along one of its segments — that point is
kept as it is: a trace is split there, so no staircase merge or corner
chamfer runs through it, and a trace on whose segment the other trace
ends is left unmerged if a merge or chamfer would take that point away.

Closed loops (shape, pad, and via outlines) are merged the same way at
their own natural corners; the length cap does not apply there, since a
loop's corners carry no equivalent length-based ambiguity.

The maximum recovered step length can be set independently of the
declared grid (e.g. a manual correction, if the grid was changed after
the traces being converted were drawn) or disabled entirely, in which
case every `*LH`/`*LV` run is emitted unmerged, exactly as before this
feature existed.

### 9.12 Corner-slanting (chamfering) ordinary 90° trace corners

Independently of staircase recovery above, the pre-converter can also
replace an ordinary 90° corner between two straight trace segments (not
originating from a recovered staircase) with a 45° chamfer — this
applies regardless of whether the file contains any staircase-drawn
diagonals at all.

Each corner's chamfer length is capped to at most half of *either*
adjacent leg's own full length, regardless of the configured limit: a
chamfer only ever consumes material from the two legs already meeting at
that corner, so the result can never extend beyond the original
corner's own footprint, and no separate clearance check against nearby
copper is needed as a result — if the original 90° corner was clear of
other copper, a chamfer that stays within its own footprint is too. The
configured limit itself defaults to tracking staircase recovery's own
effective value (Section 9.11, itself already capped at the same fixed
25 mil ceiling), and can be set independently of it, but either way only
binds when both legs are long enough not to be limited by their own
length first — a leg shorter than twice the configured limit is capped
by its own available length instead, so a short leg between two other
corners in quick succession still gets a proportionally smaller chamfer
at each end rather than an oversized one or none at all. If both ends of
such a leg end up trimming exactly half its own length, it is fully
consumed and the two resulting chamfer diagonals — one from each
corner — are merged into a single diagonal segment.

A corner is left unmodified if a via for its net (Section 9.6) sits
close enough to it that chamfering would risk moving the trace path away
from the via's actual connection point — specifically, the single
closest such via-adjacent point (which may be one of the trace's own two
true end points, e.g. where it terminates at a pad, rather than an
internal corner) is protected, not every corner merely within some fixed
distance of a via, since a short leg can otherwise have both its own
corners fall within that distance even though the via genuinely
terminates the trace at only one of them.

This feature and staircase recovery can each be disabled independently
of the other.

### 9.13 Synthetic `*TS` and `*SBOARD`

Neither a `*TS` (wave-solder direction, Section 2) nor a board-outline
shape record exists anywhere in the V2/V3 source. The pre-converter
fabricates both from scratch, immediately before processing the first
real `*S` shape record:

```
*TS H 0 0
*SBOARD
60 90 100 0 100 100
0 0 0 0 0 100
0.000000
<synthetic rectangle outline, (0,0)-(width,height)>;
;
;
```

`*TS` is emitted as a fixed placeholder (`H 0 0`) regardless of anything
in the source, since V2/V3 has no wave-solder-direction data at all
(Section 2 already notes this field is read but not used by KIUB, so the
placeholder has no downstream effect either way). `*SBOARD`'s outline is
a plain rectangle from `(0, 0)` to `(width, height)` — the V2/V3 header's
own board-width/height fields (Section 9.1) — since V2/V3 does not store
the board outline as its own named shape the way V4/V5 does. The
even/odd segment-start encoding (Section 3.1) is applied identically to
this generated outline so it parses correctly through the ordinary V4/V5
outline-stream logic; the trailing pair of bare `;` lines are the same
empty pads/arcs sections described in Section 9.2 (a synthetic `BOARD`
shape has neither).

[↑ Contents](#contents)

## 10. Definitions

### 10.1 Unit system

| DDF major version | Unit |
|---|---|
| 2, 3, 4 | Database units — 1/1200 inch (converted `value / 1.2 × 0.0254` → mm) |
| 5 | Nanometres (converted `value / 1,000,000` → mm) |

The Y-axis convention is positive-Y-**up** in the DDF file; KiCad uses
positive-Y-**down**, so every Y coordinate is negated during conversion
(this negation is applied individually throughout Sections 1–8 and is not
repeated field-by-field above).

Rounding is deferred throughout the pipeline: intermediate values are
kept at full floating-point precision and rounded exactly once, at final
output, to avoid compounding floating-point noise from combining several
already-rounded values (offsets, ratios, etc.).

### 10.2 Rotation encoding

**V4/V5:** a signed integer, degrees × 64, counter-clockwise, `0` = east.
A negative value additionally signals "this is a Bottom-layer placement"
wherever it appears on a `*C` position line (Section 5) or `*X` text
record (Section 8) — side and angle are packed into the same signed
field rather than given as separate values.

**V2/V3:** a small unsigned code `0`–`7`. The pre-converter's own lookup
table (`ROT_MAP`) gives the authoritative mapping — note the angle
progression is **not** a simple `code × 90°` on both halves; the top-side
codes run backwards (0°, 270°, 180°, 90°) while the bottom-side codes run
forwards (0°, 90°, 180°, 270°):

| Code | Raw V4/V5 value | Angle | Side |
|---|---|---|---|
| 0 | `0` | 0° | Top |
| 1 | `17280` | 270° | Top |
| 2 | `11520` | 180° | Top |
| 3 | `5760` | 90° | Top |
| 4 | `-23040` | 0° | Bottom |
| 5 | `-5760` | 90° | Bottom |
| 6 | `-11520` | 180° | Bottom |
| 7 | `-17280` | 270° | Bottom |

converted to the V4/V5 signed degrees×64 encoding by the V2/V3
pre-converter before the rest of the parser sees it (the sign of the raw
value is what actually signals Bottom placement, per Section 1; the
angle shown is each value's magnitude, mod 360°).

### 10.3 Layer numbering and the pad/via layerset bitmask

Ultiboard's **native** layer numbering (used in the `*LT`/`*LA`/`*LP`/`*X`
`<layer>` header field, which is a 1-based bit index, and in the header's
layer-direction-flags line) runs: odd numbers are the "top view" of a
layer pair, even numbers are the "bottom view", with layer 1/2 = Top/
Bottom and all higher pairs being inner layer pairs, e.g. for an 8-layer
board: `1(Top) 4 3 6 5 8 7 2(Bottom)`.

The **pad/via layerset field** (the 8-hex-digit value seen in pad
descriptors, `*C` pin lines, and `*V` records) is a *different*,
independent bitmask — one bit per physical copper layer, additive for
multi-layer pads:

| Ultiboard layer | Layerset bit |
|---|---|
| Top | `00000001h` |
| Bottom | `00000002h` |
| Inner 1 | `00000008h` |
| Inner 2 | `00000004h` |
| Inner 3 | `00000020h` |
| Inner 4 | `00000010h` |
| Inner 5 | `00000080h` |
| Inner 6 | `00000040h` |
| Inner 7 | `00000200h` |
| Inner 8 | `00000100h` |
| Inner 9 | `00000800h` |
| Inner 10 | `00000400h` |
| Inner 11 | `00002000h` |
| Inner 12 | `00001000h` |
| Inner 13 | `00008000h` |
| Inner 14 | `00004000h` |
| Inner 15 | `00020000h` |
| Inner 16 | `00010000h` |
| Inner 17 | `00080000h` |
| Inner 18 | `00040000h` |
| Inner 19 | `00200000h` |
| Inner 20 | `00100000h` |
| Inner 21 | `00800000h` |
| Inner 22 | `00400000h` |
| Inner 23 | `02000000h` |
| Inner 24 | `01000000h` |
| Inner 25 | `08000000h` |
| Inner 26 | `04000000h` |
| Inner 27 | `20000000h` |
| Inner 28 | `10000000h` |
| Inner 29 | `80000000h` |
| Inner 30 | `40000000h` |

The KiCad-side ordinal mapping KIUB uses throughout (its `layersCu`
table, positionally aligned with the bit order above — Top, Bottom, In2,
In1, In4, In3, … In30, In29) assigns canonical KiCad 6+ ordinals:
`F.Cu=0`, `In1.Cu..In30.Cu = 1..30`, `B.Cu=31`.

For any given field, KIUB masks the raw layerset hex against
`(2**maxLayers) - 1` (the board's own active-layer mask derived from the
header's `<max layers>` field, Section 1) before resolving bits to
layers, so stray high bits on boards with fewer than 32 layers are
correctly ignored.

### 10.4 Drill codes and the "SMD trick"

A pad drill diameter below 0.05 mm is treated specially and stored as the
sentinel value `-1` rather than a literal near-zero diameter. This
represents an intentional Ultiboard workaround, not a rounding
threshold: Ultiboard's DOS-era placement engine will not allow two SMD
pads from opposite layers to occupy the *exact* same X/Y coordinate (it
proposes to nudge one by a single database unit instead). To defeat this,
designers sometimes build a two-pad component (one pad per side, same
location) with a microscopic, never-actually-drilled hole purely to
satisfy Ultiboard's own placement-uniqueness rule, expecting the PCB
manufacturer to disregard it. The `-1` sentinel tells KIUB's component
builder to treat both pads as ordinary SMD pads placed at the same
location on `F.Cu` and `B.Cu` respectively, and to exclude them from the
solder paste mask, rather than generating a spurious real drill hole.
This special case applies only to pad drill codes (0–239); via drill
codes (240–255) always keep their literal diameter, since legitimately
tiny via drills (microvias) are a normal, intentional design feature.

### 10.5 Font encoding and overline markup

Ultiboard's PCB character set closely resembles CP437/CP850. KIUB
extracts each `*X` text record's payload as raw bytes (before any
higher-level line decoding) specifically to preserve high-range CP437
byte values, then maps each byte through a fixed CP437/CP850 → Unicode
table tuned to align with the DejaVu Sans Mono glyph set.

Names — of shapes, components (reference and alias) and nets — are CP437
text as well and may hold high-range bytes too (e.g. `0xF8`, `°`, in a
shape name such as `LED_45°`). KIUB decodes them as plain CP437, so
the KiCad texts hold the character itself; double quotes and backslashes
are escaped as KiCad's strings require.

Two substitute fonts give accurate visual reproduction, since KiCad's own
default NewStroke font renders noticeably wider than Ultiboard's native
PCB font:

- **Ultiboard Stroke** (`UltiboardStroke-Regular.ttf`) — built directly
  from the original Ultiboard font's own glyph shapes, so it is the
  closest visual match available. `font_height_ratio`/`font_width_ratio`
  of `1.0`/`1.4` apply (Section 10 fine-tuning defaults).
- **DejaVu Sans Mono** — a widely-installed system font whose glyph
  widths were empirically matched to Ultiboard's own; `1.208`/`1.186`
  apply. Requires the font to actually be installed, or KiCad silently
  reverts to its own default font.

Ultiboard text may contain an overline run delimited by `^`: the first
`^` starts the run, a second `^` ends it; an unmatched trailing `^`
extends the overline to the end of the string. KIUB converts this to
KiCad's `~{...}` overline markup — identical convention and conversion to
the one used in the companion Ulticap SCH format (see `FILEFORMAT_ULTICAP_SCH.md`
Section 10.3).

### 10.6 Board outline reconstruction

Because Ultiboard has a limited number of usable layers, board designs
routinely place internal divider/partition lines (e.g. separating two
logical board sections, or marking a scored break line) on the *same*
outline data stream as the true board edge — there is no separate
"internal line" layer distinct from the outline itself. Naively emitting
every line/arc in the `*SBOARD` outline stream straight to `Edge.Cuts`
therefore produces a malformed (non-closed, self-intersecting) outline
that KiCad's DRC and 3D viewer both reject.

For example, a separation line (`"`) down the middle of a board plus a
cutout — all sharing the same `*SBOARD` outline stream as the true board
edge:

```
                  +--------------+--------+
                  |              "        |
                  |              "  +--+  |
                  |         (X)  "  |  |  |
                  |              "  +--+  |
                  |              "        |
                 (R)-------------+--------+
```

`(X)` = Board Origin (all DDF coordinates are relative to this point);
`(R)` = the user-defined Reference Point, an offset from `(X)` used only
for on-screen display/edit purposes in Ultiboard itself (Section 1) —
neither carries geometric meaning for the outline reconstruction below.

KIUB reconstructs the true closed contour in three passes:

1. **Snap:** any two open endpoints (line/arc start or end points; full
   circles have none) closer together than a configurable tolerance
   (default 0.1 mm) are moved to their shared midpoint. This closes tiny
   gaps introduced by the DDF unit conversion's own rounding.
2. **Degree-pruning:** every snapped endpoint's incidence degree is
   counted (how many segment-ends land on it). In a genuinely closed
   contour every vertex has degree exactly 2. Any segment touching a
   vertex whose degree ≠ 2 is iteratively removed and its neighbours'
   degrees decremented, repeating until stable. What survives is the true
   closed contour; what's removed is everything else — floating internal
   lines (both endpoints degree 1), lines that touch the contour at a
   vertex without being part of it (creating a degree-3 branch point),
   and lines that cross the contour at a non-vertex point (which, after
   snapping, also produces a degree-3+ branch point at the crossing).
3. **Write:** contour lines/arcs → `Edge.Cuts`; everything pruned away →
   `F.Fab` (visible for reference, no DRC impact). Circles always go to
   `Edge.Cuts` (closed by construction, they can never be pruned).

If, after this process, the outline on `Edge.Cuts` is still not fully
closed in KiCad's own view (residual rounding beyond the snap tolerance,
or a genuinely malformed source outline), KiCad's own
**Edit → Shape Modification → Heal Shapes** command on the selected
Edge.Cuts elements will usually close the remaining gap.

### 10.7 Known errata in the 1997 reference manual

Confirmed by direct empirical testing against real DDF files, in addition
to the field-order and radius/diameter errata already called out inline
in Sections 2 and 5:

- **Shape outline lines cannot contain two consecutive segment-start
  points.** If two adjacent `(x, y)` pairs in the outline stream both
  have odd `x` (both marked as segment starts), the manual's own encoding
  description does not account for this case; empirically, real files
  never produce a meaningful geometry from it, and the correct handling
  (confirmed against the KIUB implementation) is to discard the first of
  the two pairs and continue from the second. This is documented in
  Section 3.1 above as part of the outline-stream grammar itself, since
  it is a structural rule of the format, not merely an implementation
  workaround.
- **Non-round vias are not reproduced.** Ultiboard's via pad table
  (Section 2) can define a non-round pad shape for a via code, and the
  per-via `<rot>` field (Section 7) orients that shape. KiCad natively
  supports only round shapes for standard vias — a non-round or
  slotted/oval via can only be represented in KiCad via a custom
  footprint workaround, which is out of scope for a `.kicad_pcb` via
  record and is not implemented by this converter. Every via is therefore
  converted to a round pad using the via pad table's `Ysize` as its
  effective diameter, and `<rot>` is discarded.

[↑ Contents](#contents)
