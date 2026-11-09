# Ulticap ASCII SCH File Format

Reverse-engineered specification, derived from empirical testing against
real Ulticap files (V1.50 through V5.72) and cross-checked against the
KIUC parser/writer implementation.

## Contents

[↑ README contents](../../README.md#file-formats)

1. [File Header](#1-file-header)
2. [Shape and Component records](#2-shape-and-component-records)
3. [Wire records](#3-wire-records)
4. [Junction records — `*V`](#4-junction-records--v)
5. [Annotation records — `*A`](#5-annotation-records--a)
6. [Free-text label records — `*X`](#6-free-text-label-records--x)
7. [Bus records](#7-bus-records)
8. [Sheet hierarchy and end-of-schematic marker](#8-sheet-hierarchy-and-end-of-schematic-marker)
9. [Extended section — `*M` (NRM data)](#9-extended-section--m-nrm-data)
10. [Definitions](#10-definitions)
    - [10.1 Unit system](#101-unit-system)
    - [10.2 Rotation / mirror code](#102-rotation--mirror-code-r-07)
    - [10.3 Overline markup](#103-overline-markup)
    - [10.4 Reference and value text alignment translation](#104-reference-and-value-text-alignment-translation)
    - [10.5 PINTYPE values](#105-pintype-values)
    - [10.6 Reserved tag list](#106-reserved-tag-list)
    - [10.7 Colour palette](#107-colour-palette-015)
    - [10.8 Character set](#108-character-set)

## 1. File Header

The header consists of four elements, in fixed order, each on its own line(s):

```
*P <customer name>
<major> <minor> [<NRM>]
<x0>,<y0>,<x1>,<y1>[,<grid>];
*R <root sheet name>
```

**`*P <customer name>`**  
optional free-text customer/documentation field.  
Parsed verbatim as the remainder of the line after `*P `.

**Version line — `<major> <minor> [<NRM>]`** — space-separated.

- `<major>` / `<minor>`: integer version number.  
  KIUC has been tested and
  confirmed working against files ranging from V1.50 through V5.72.  
  KIUC branches on the major version: `is_less_than_v500 = (major < 5)`,  
  since arc encoding differs between pre-V5.00 files and V5.00+ files.
- `<NRM>` (optional 3rd token): 8-digit string, stored as-is,  
  default  `'00000000'` if absent. KIUC does not interpret this value  
  it is carried only for round-tripping/diagnostics and is not written to `.kicad_sch`.

**Bounds line — `<x0>,<y0>,<x1>,<y1>[,<grid>];`**
- Comma-separated, terminated with `;`.
- `<x0>,<y0>`: lower-left corner of the sheet, in Ulticap units
  (1 unit = 2 mil).
- `<x1>,<y1>`: upper-right corner of the sheet.
- 5th field (optional): grid spacing. Extensive testing confirms this
  field is the sheet grid value.
- Default sheet bounds if absent (KIUC fallback, not an Ulticap-format default):  
  `xmin=-3025, ymin=-2175, xmax=3021, ymax=2159`.

**`*R <root sheet name>`**  
root/project sheet name for hierarchical designs.  
Stored verbatim as the remainder of the line after `*R `.

**End of header:**  
any line not matching `*P`, the version line, the bounds line,  
or `*R` terminates header parsing and begins the body.

[↑ Contents](#contents)

## 2. Shape and Component records

### Shape definition — `*S<symbol name>`

```
*S<symbol name>
<width> <height>
1,<colour>,<x>,<y>,...,<x>,<y>,1,<colour>,<x>,<y>,...,<x>,<y>;
<pin_format>,<pin_rotation>,<rel_x>,<rel_y>,
<pin_format>,<pin_rotation>,<rel_x>,<rel_y>;
<cx>,<cy>,<r>,<rotate>,<angle>,<colour>,<thick>,<linetype>,
<cx>,<cy>,<r>,<rotate>,<angle>,<colour>,<thick>,<linetype>;
<dx> <dy> <dx_poe> <dy_poe> <size> <colour> <visibility> <tag>=<value>
;
```

(Pins, Circles/Arcs, and Attribute lines each repeat one entry per line;  
the layout above shows two example lines per section purely to  
illustrate the repeating, comma- or line-terminated pattern — see the  
per-section description below for the exact repetition and termination
rule.)

Four sections follow the width/height line, in fixed order:

1. **Outline** — comma-separated stream, terminated by `;`. Contains one or more colour-groups;  
   each group begins with the literal `1` followed by `<colour>`,  
   then an arbitrary number of `x,y` coordinate pairs forming a connected polyline.  
   `<colour>` packs three values:  
   - bits 0–3: colour index (see colour palette, Section 10.7);  
   - bit 8 (`256`): thick line — `0` = thin, `256` = thick;  
   - bits 12–13: line type `{0:solid, 4096:dash, 8192:dash-dot, 12288:dot}`.  

   E.g. `1,261,...` is a thick, solid line in colour 5. The two widths are  
   Ulticap DOS's own, fixed in the program: 6 (thin) and 30 (thick), in  
   Ulticap units — the file records only which one a line uses.  
   The stream may span multiple physical lines; only the trailing `;` ends the section.  
   Confirmed by direct testing:  
   Ulticap silently coerces a coordinate value of `1` to `0` when saving  
   (e.g. a rectangle placed at 1,1 / 1,0 / 0,1 is rewritten with those coordinates as 0),  
   so genuine outline coordinate values of `1` should not occur in practice.  
   KIUC nonetheless retains positional sentinel detection (rather than value-based)  
   as a robust additional safeguard.

2. **Pins** — one pin per line, comma-separated, each line ending `,\n`  
   except the last, which ends `;\n`. Each line has the form  
   `<pin_format>,<pin_rotation>,<rel_x>,<rel_y>`:
   - `pin_format`:  
     `{0:none, 1:INVERTED, 2:CLOCK, 3:IEC IN, 4:IEC OUT,`  
     `5:IEC INVERTED IN, 6:IEC INVERTED OUT, 7:IEC INVERTED CLOCK,`  
     `8:IEC BIDIRECTIONAL, 9:BUS, 10:CLOCK INVERTED}`
   - `pin_rotation`:  
     `{0:East(right), 1:North(up), 2:West(left), 3:South(down)}`
   - `rel_x, rel_y`: stub-end position relative to the symbol's bottom-left origin.

3. **Circles/Arcs** — one entry per line, comma-separated, each line  
   ending `,\n` except the last, which ends `;\n`. Each line has the form  
   `<cx>,<cy>,<r>,<rotate>,<angle>,<colour>,<thick>,<linetype>`:
   - `cx, cy`: centre point.
   - `r`: radius, in Ulticap units.
   - `rotate`: degrees = value/64, counter-clockwise, 0 = east.
   - `angle`: segment sweep, degrees = value/64,  
   counter-clockwise from `rotate` (23040 = full circle).
   - `thick`: thin/thick flag — `0` = thin, `1` = thick (the same fixed  
   widths as outlines, 6 and 30 Ulticap units).
   - `linetype`: `{0:solid, 1:dash, 2:dash-dot, 3:dot}`.

   Both confirmed with a DOS-drawn symbol holding a thin and a thick circle  
   in each line type. Ulticap V5.72 offers no line type for a circle (only  
   colour and width), so its own circles are always written with `0` here.

   **Known V5.72 defect — missing `,\n` separator:** some V5.72-exported  
   files occasionally have a Pins or Circles/Arcs line missing its  
   trailing `,\n` entirely (comma *and* newline together), silently  
   gluing two records' worth of fields onto one physical line with no  
   separator at all. Because each field parses as a plain integer, this  
   corrupts data with no error from Ulticap itself — confirmed to both  
   drop a trailing record and shift every field after the merge point.  
   KIUC detects this (both sections always contain a whole number of  
   fixed-size records on any well-formed line, so a line that doesn't is  
   flagged) and repairs the clear-cut cases automatically; a genuinely  
   ambiguous split is left for interactive resolution rather than guessed  
   — see "Known Ulticap V5.72 Export Defects" in  
   [SCHEMATICS.md](../SCHEMATICS.md#known-ulticap-v572-export-defects-auto-corrected).

4. **Attribute lines** — one per line, space-separated, terminated by a lone `;`.  
   Each line has the form  
   `<dx> <dy> <dx_poe> <dy_poe> <size> <colour> <visibility> <tag>=<value>`.  
   Each field is parsed independently as an integer  
   (`dx,dy,dx_poe,dy_poe,size,colour,visibility`),  
   then the remainder split on the first `=` into `<tag>` and `<value>`  
   (with overline markup `^..^` converted).  
   This attribute-line grammar is universal — the same format is reused  
   for `*C` and `*A` records. `dx_poe, dy_poe` define this attribute's  
   point-of-effect (POE) — the coordinate used to match the attribute  
   against the geometry it describes (e.g. a wire, or a symbol pin's  
   connection point); see the worked description under "PINTYPE, #,  
   LABEL / WIRELABEL" below for how POE matching is applied.

   Within a symbol's attribute block, KIUC gives special handling to:
   - `TRANSX` / `TRANSY`
     silently discarded as redundant editor artefacts.
   - `SIGNAL=<net>,<pin#>[,<pin#>...]`
      Parsed separately into a signal-pins list.  
      Not treated as a display attribute.
   - `PINTYPE`, `#` (pin number), `LABEL` / `WIRELABEL`  
      grouped by shared `(dx_poe, dy_poe)` coordinates  
      matched against each symbol pin's wire-connection point  
      (the stub endpoint on the bounding-box edge,  
      derived from `pin_rotation` and `rel_x/rel_y`).  
      Exact coordinate match is tried first.  
      If no pin's connection point coincides exactly with a POE group,  
      KIUC falls back to axis-alignment matching: a `LABEL`/`WIRELABEL`'s  
      own `(dx, dy)` display position, when not affected by the V5.72  
      export defect described below, sits exactly on its true pin's own  
      coordinate on the axis perpendicular to that pin's facing direction  
      — this is checked first, since `(dx, dy)` is never the corrupted  
      field, only `(dx_poe, dy_poe)` can be. The fallback is only applied  
      when it resolves to exactly one candidate pin; a still-ambiguous  
      case is left as-is and logged as a diagnostic rather than guessed.  
      See "Known Ulticap V5.72 Export Defects" in [SCHEMATICS.md](../SCHEMATICS.md#known-ulticap-v572-export-defects-auto-corrected)  
      for the full rationale and worked examples.
   - `WIRELABEL` is used as the pin name only when `LABEL` is absent  
     (typically on PWR pins).

   The symbol's reference origin (0,0) is its bottom-left corner.

### Component placement — `*C`

```
*C <X abs> <Y abs> <rotation> <symbol name>
<dx> <dy> <dx_poe> <dy_poe> <size> <colour> <visibility> <tag>=<value>
...
;
```

- `<X abs> <Y abs>`
  absolute placement of the symbol's origin on the sheet.
- `<rotation>`: 0–7  
  see rotation/mirror table, Section 10.2.
- `<symbol name>`: references a `*S` definition appearing earlier in the file.  
  **Known defect:** a symbol name containing a space here (and on the  
  matching `*S<symbol name>` definition) causes Ulticap V4.91 to fail  
  opening the file with a disk-read error, even though V5.72 itself, this  
  parser, and KiCad all handle the space without issue — it is purely a  
  V4.91-openability concern. KIUC replaces the spaces with underscores  
  in the design's corrected copies (`<name>_corrected`, see  
  [CORRECTED_FOLDER.md](../CORRECTED_FOLDER.md)), on the `*S` definition and  
  every `*C` placement alike, and converts those; see "Known Ulticap V5.72  
  Export Defects" in  
  [SCHEMATICS.md](../SCHEMATICS.md#known-ulticap-v572-export-defects-auto-corrected).
- Following attribute lines use the same grammar as symbol attributes  
  (space-separated, `<tag>=<value>` trailing). KIUC reads these into a  
  per-component attribute map, with direct fields extracted for  
  `REFDES`, `DEVICE`, `VALUE`, `PKG_TYPE`, `WIRELABEL`, and `FILE` (subsheet reference).
  - Placeholder values (`-`, `?`, `*`, `--`, `N/A`, empty) are normalized to "absent"  
    rather than treated as literal text.
  - `FILE=` (hierarchical subsheet reference): if absent on the `*C` instance,  
    KIUC falls back to the `FILE=` attribute on the referenced `*S` symbol definition.  
    If the resolved filename has no extension, `.SCH` is appended automatically.  
    **Known defects on V5.72 "save as SCH":**
    - When the original hierarchical design uses non-DOS-8.3 subsheet  
      filenames, the filenames V5.72 actually writes for the subsheets  
      can end up not matching the `FILE=` tags recorded in the root  
      sheet. KIUC's own hierarchy-completeness check (see SCHEMATICS.md) warns  
      that a sub-sheet file is missing; there's no reliable way to infer  
      which actual file was meant, so this needs a manual fix — edit  
      either the saved filenames or the `FILE=` tags in the root sheet  
      so they agree.
    - A re-save of a `.SCH` already opened in V5.72 sometimes adds a  
      second dot between the filename and extension, or a second `.SCH`  
      extension (e.g. `SHEET2..SCH` or `SHEET2.SCH.SCH` instead of  
      `SHEET2.SCH`) — observed so far only on sub-sheets of a hierarchical  
      design. Same symptom and same manual fix as above.
- Each component pin's absolute point-of-effect is  
  `Xpoe = X_abs + dx_poe`, `Ypoe = Y_abs + dy_poe`, using the `dx_poe/dy_poe`  
  values from the matching `*S` pin's POE attribute group.
- The pin-number tag is the literal `#` character (e.g. `# = 12`).

[↑ Contents](#contents)

## 3. Wire records

Two distinct wire record formats exist:  
`*LV` (free/all-angle lines)  
`*LT` (axis-constrained horizontal/vertical/diagonal lines).

### `*LV` — all-angle line

```
*LV <level> <startx> <starty> <endx> <endy> <l1> <l2> <l3>
```

Single-line record; no continuation, no terminating `;`.

- `<level>`: `1` = netlist layer, `2` = user layer.  
  Any other value is unrecognised.
- `<startx> <starty> <endx> <endy>`  
  absolute start/end coordinates.
- Netlist layer (`level=1`):
  - `<l1>` = net number (The same net number can appear across  
     multiple `*LV 1` records belonging to the same electrical net).
  - `<l2>` = `{0:wire, 1:bus}`.
- User layer (`level=2`):
  - `<l1>` = linetype + colour, packed:  
    high byte (`& 0xFF00`) = `{0:solid, 256:dashed, 512:dash-dot, 768:dot}`  
    low byte (`% 16`) = colour index (see colour palette, Section 10.7).
  - `<l2>` = `{0:thin, 1:thick}` — the same fixed widths as symbol  
    lines, 6 and 30 Ulticap units (Section 2). Confirmed with V5.72's  
    export of a DOS sheet holding a thin and a thick line in each line type.
- `<l3>` (always 0) is present in the record - unused by KIUC.

### `*LT` — axis-constrained line

```
*LT <level> <start0>
<start1> <end1> <l1> <l2> <l3>;
```

(One or more `<start1> <end1> <l1> <l2> <l3>` data lines follow the header line;  
the layout above shows a single line purely for brevity, see below for the exact repetition rule.)

The header line gives a fixed/anchor coordinate (`start0`); one or more  
data lines follow, each describing one segment sharing that anchor.  
Only the final data line is terminated with `;`.

- `<level>`: `0` or `1` = netlist layer, `2` = user layer.
- `<start1> <end1>`: start/end of the varying coordinate  
  (interpretation depends on `<l3>`, below).
- `<l3>` — orientation, and the resulting absolute endpoints:  
   (empirically determined)
  - `4` (horizontal): `y` fixed at `start0`; `x` runs `start1 → end1`.  
    Endpoints: `(start1, start0) → (end1, start0)`.
  - `5` (vertical): `x` fixed at `start0`; `y` runs `start1 → end1`.  
    Endpoints: `(start0, start1) → (start0, end1)`.
  - `6` (diagonal, NE/SW): endpoints computed as  
    `x1=(start0+start1)//2, y1=(start1-start0)//2`  
    `x2=(start0+end1)//2, y2=(end1-start0)//2`.  
    (`//` is integer floor division, not an average.)
  - `7` (diagonal, SE/NW): endpoints computed as  
    `x1=(start0+start1)//2, y1=(start0-start1)//2`  
    `x2=(start0+end1)//2, y2=(start0-end1)//2`.  
    (`//` is integer floor division, not an average.)
- Netlist layer (`level=0` or `1`):
  - `<l1>` = net number.
  - `<l2>` = `{0:wire, 1:bus}`.
- User layer (`level=2`):
  - `<l1>` = linetype + colour, same packing as `*LV` level 2  
    (high byte linetype, low byte colour).
  - `<l2>` = `{0:thin, 1:thick}` — the same fixed widths as symbol  
    lines, 6 and 30 Ulticap units (Section 2). Confirmed with a DOS sheet  
    holding a thin and a thick line in each line type.

[↑ Contents](#contents)

## 4. Junction records — `*V`

```
*V <X common>
<Y> <net_number> <junction_type> <type>;
```

(One or more `<Y> <net_number> <junction_type> <type>` groups follow the header line;  
the layout above shows a single group purely for brevity — see below for the exact repetition rule.)

- `<X common>`: X coordinate shared by every junction in the group.
- Each data entry is a 4-value group  
  `<Y> <net_number> <junction_type> <type>`  
  whitespace-separated (newline or space)  
  Multiple groups may appear per line or across lines.  
  Only the final group is terminated with `;`.
- `<junction_type>`: `0` = wire-to-wire (or bus-to-bus) junction  
  any other value = wire-to-bus connection (bus entry).  
  **Caveat:** files saved by Ulticap for Windows (V5.71) always  
  write `<junction_type> = 0`, regardless of the actual connection kind.  
  This means `<junction_type>` alone cannot reliably distinguish  
  wire-wire / wire-bus / bus-bus junctions on such files.  
  KIUC's net/bus construction relies on geometric matching  
  (coincident endpoints/coordinates) rather than trusting this field outright.
- `<net_number>` and `<type>` are present and correctly parsed by KIUC  
  but not currently used for net construction.  
  Nets are built from geometric matching instead.

[↑ Contents](#contents)

## 5. Annotation records — `*A`

```
*A <X abs> <Y abs> <X POE abs> <Y POE abs> <size> <colour> <visibility> <tag>=<value>
```

Single-line record, no trailing `;`.

- `<X abs> <Y abs>`: absolute anchor position of the annotation text.
- `<X POE abs> <Y POE abs>`: absolute point-of-effect.  
  This is the only way KIUC determines which object an annotation belongs to:  
  KIUC checks whether `(X POE, Y POE)` lies on a wire segment,  
  or coincides with a known bus-entry point,  
  to associate a `LABEL`-tagged annotation with the wire/net or bus entry it labels.
- `<size>`, `<colour>`, `<visibility>`
  as in the universal attribute-line grammar (Section 2).
- `<tag>=<value>`: trailing text, split on the first `=`.  
  If no `=` is present, the entire trailing text is treated as the value of an  
  implicit `LABEL` tag (overline markup converted).

[↑ Contents](#contents)

## 6. Free-text label records — `*X`

```
*X <x> <y> <size> <colour> <rotation> <align> <text>
```

Single-line record, no trailing `;`. `<text>` is the remainder of the  
line (may itself contain spaces) and has overline markup `^..^` converted.

- `<rotation>`: 1/64-degree units: `0`, `5760` (90°), `11520` (180°)  
  or `17280` (270°), counter-clockwise around the anchor. Ulticap for DOS  
  draws 180° and 270° upside down: the text reads right to left (180°) or  
  downwards (270°) and lies on the far side of the anchor, `<align>`  
  being taken in the text's own rotated frame.  
  Any other on-disk value is treated as `0`.
- `<align>`: anchor position, `0`–`8`:  
  `{0:Bottom-Left, 1:Bottom-Centre, 2:Bottom-Right,`  
  `3:Centre-Left, 4:Centre-Centre, 5:Centre-Right,`  
  `6:Top-Left, 7:Top-Centre, 8:Top-Right}`.  
   Out-of-range values are clamped into `0`–`8`.  
  `*X` labels are always visible  
  (no separate visibility byte, unlike the `*A`/attribute-line grammar).

[↑ Contents](#contents)

## 7. Bus records

There is no dedicated bus record type. A bus is simply a wire record  
(`*LV` or `*LT`, Section 3) with its wire/bus flag set to bus  
(`<l2> = 1` for the netlist layer). Bus entries (a wire connecting into  
a bus) are represented via `*V` junction records (Section 4) with  
`<junction_type> != 0`.

**Geometric recovery caveat:** because Ulticap for Windows (V5.71) always  
writes `<junction_type> = 0` regardless of actual connection kind (see  
Section 4), KIUC cannot rely on `<junction_type>` alone to detect bus  
entries on such files. KIUC performs a geometric recovery pass after  
parsing: for every junction not already flagged as a bus entry, if its  
coordinates land exactly on a bus-flagged wire segment, it is reclassified  
as a bus entry. This recovery is applied unconditionally (regardless of  
file version), since in valid Ulticap data a wire-junction point that  
geometrically coincides with a bus segment is, in practice, always a  
genuine wire-to-bus connection — there is no Ulticap construct for an  
ordinary wire/wire junction that happens to sit exactly on a bus by  
coincidence.

[↑ Contents](#contents)

## 8. Sheet hierarchy and end-of-schematic marker

**Hierarchical subsheet references** are not a distinct record type.  
They are encoded through the ordinary `*C`/`*S` attribute grammar  
(Section 2), using a `FILE=<filename>` attribute to point to the SCH  
file implementing the subsheet. Two encoding styles exist depending on  
Ulticap version:

- **V4.x style:** the symbol (`*S`, e.g. named `HIERARCH_n` in observed files)  
  carries a placeholder `FILE=?` attribute; the real filename is supplied as an  
  override on the `*C` instance attribute block.
- **V5.x style:** the symbol (e.g. `SHEETBUS_n`) has no `FILE=` of its  
  own; the `*C` instance attribute block carries `FILE=` directly.

A `*C` is a hierarchical subsheet placement whenever its resolved  
`FILE=` value (instance override, falling back to the symbol definition  
per Section 2) is present. If the resolved filename has no extension,  
`.SCH` is appended.

**Module port symbols** (the in-sheet connector that exposes a net to  
the parent sheet at a subsheet boundary) are likewise identified  
through ordinary symbol attributes, again with two encoding styles:

- **V4.x style:** the `*S` symbol's attribute block has a `PORT=` attribute (e.g. `PORT=OUT`).
- **V5.x style:** the `*S` symbol's attribute block has a `DEVICE=` attribute  
  whose value starts with `PORT_` (e.g. `DEVICE=PORT_IN`),  
  and the symbol name itself starts with `PORT_`.

**End-of-schematic marker — `**`** — a line containing exactly `**`  
terminates the schematic body. Parsing of sheet content (`*S`, `*C`,  
`*LV`, `*LT`, `*V`, `*A`, `*X`, `*R`) stops at this point. Any content  
after `**` belongs to the extended/NRM section (Section 9) and is  
governed by entirely different rules.

[↑ Contents](#contents)

## 9. Extended section — `*M` (NRM data)

Content following the `**` end-of-schematic marker, beginning with  
`*M` records, is NRM (Netlist Representation Model) data appended by a  
separate tool. KIUC does not parse, interpret, or modify this data in  
any way — it is treated as opaque and discarded entirely on read.

[↑ Contents](#contents)

## 10. Definitions

### 10.1 Unit system

Every length in the SCH format is an integer in **Ulticap units**:  
1 unit = 2 mil = 0.0508 mm. That covers the coordinates and POE positions,  
the header's sheet bounds, symbol sizes and pin positions, attribute offsets  
(`dx`, `dy`, `dx_poe`, `dy_poe`) and text sizes (`<size>`: the height of a  
character cell; KiCad measures a text's height differently, by about  
1.137 × size for the same cell -- hence KIUT's text-height multipliers).  
Not lengths: the line style codes (6 thin, 30 thick, whose widths are built  
into Ulticap), the rotation code (`R`; a `*X` rotation in 1/64 degree),  
colours, visibility and alignment bytes. A `WIDTH=` value is a real mil  
(a trace width for Ultiboard). KIUT handles all geometry in integer  
Ulticap-unit space and only converts to mm at the point of KiCad output,  
to avoid floating-point rounding error accumulating through the pipeline.

The Y-axis convention is positive-Y-up, in both ASCII and binary  
Ulticap formats.

### 10.2 Rotation / mirror code (`R`, 0–7)

Used for component rotation (`*C <rotation>`) and wherever a rotation  
code appears elsewhere in attribute-derived geometry:

| R | Angle | Mirror | R | Angle | Mirror |
|---|-------|--------|---|-------|--------|
| 0 | 0°    | no     | 4 | 0°    | yes    |
| 1 | 90°   | no     | 5 | 90°   | yes    |
| 2 | 180°  | no     | 6 | 180°  | yes    |
| 3 | 270°  | no     | 7 | 270°  | yes    |


### 10.3 Overline markup

Free text fields (annotation/label text, attribute values) may contain  
Ulticap's overline syntax: a `^` starts an overline run, a second `^`  
ends it; if no closing `^` is found, the overline continues to the end  
of the text. KIUC converts this to KiCad's `~{...}` overline syntax.  
Example: `^RESET^` → `~{RESET}`; `A^B` (no closing `^`) → `A~{B}`.

### 10.4 Reference and value text alignment translation

This section covers how the visibility byte and rotation/mirror code  
on a component's REFDES/VALUE text combine to determine the text's  
on-screen anchor, and how that anchor must be pre-compensated when  
written to `.kicad_sch` so that KiCad's own automatic property-text  
flip reproduces the original Ulticap-intended display.

**Definitions**
- `V` (visibility byte): raw byte stored in the SCH file.  
  The low 5 bits give the raw anchor code: `A = V & 31`  
  (see anchor code description below).  
  For the REFDES/VALUE pairing specifically,  
  the high bits of `V` control which of the two TAGs is shown:  
  `{0:both hidden, 64:REFDES visible, 128:VALUE visible, 192:both visible}`.  
  (Note: this two-bit tag/value scheme is specific to the REFDES/VALUE pair  
  elsewhere in the format, a standalone attribute line's `<visibility>`  
  field uses only bit 128 as a single visible/hidden flag, see Section 2.)
- `A` (raw anchor code): position in a 3×3 grid.  
  `0`–`8` = horizontal text.  
  `16`–`24` = vertical text.   
  Grid:
  row `0`=Bottom, `1`=Centre, `2`=Top.  
  column `0`=Left, `1`=Centre, `2`=Right.  
  3-letter code: `[H|V][B|C|T][L|C|R]`  
  H/V = orientation (horizontal, or vertical/90° CCW)  
  B/C/T = anchor row.  
  L/C/R = anchor column.  
  Full enumeration:  
  `{0:HBL, 1:HBC, 2:HBR, 3:HCL, 4:HCC, 5:HCR, 6:HTL, 7:HTC, 8:HTR,`  
  `16:VBL, 17:VBC, 18:VBR, 19:VCL, 20:VCC, 21:VCR, 22:VTL, 23:VTC, 24:VTR}`.

**Pipeline:** the raw anchor `A` passes through three stages before  
becoming what's actually written to `.kicad_sch`:

1. **The intended display anchor** (`ulticap_translate(R, V)`) — what  
   Ulticap means to display, derived by applying the component's  
   rotation/mirror code (`R`, Section 10.2) to the raw anchor grid  
   position:

   | R | Transform on (row, col) |
   |---|--------------------------|
   | 0, 1 | identity |
   | 2, 3 | `row = 2−row, col = 2−col` (180° flip) |
   | 4, 7 | `col = 2−col` (L↔R mirror) |
   | 5, 6 | `row = 2−row` (T↔B mirror) |

2. **The value written to `.kicad_sch`** — the pre-compensated value  
   that cancels KiCad's own automatic flip of component-property text.  
   Since every flip below is self-inverse, this stage is simply stage 1  
   passed through the same KiCad-flip table again. KiCad's automatic  
   flip, by rotation code `R` (empirically verified, 144 combinations  
   tested):

   | R | H-text flip | V-text flip |
   |---|-------------|-------------|
   | 0 | none | none |
   | 1 | none | 180° (B↔T and L↔R) |
   | 2 | 180° | 180° |
   | 3 | 180° | none |
   | 4 | L↔R | T↔B |
   | 5 | T↔B | T↔B |
   | 6 | T↔B | L↔R |
   | 7 | L↔R | L↔R |

3. **What KiCad actually renders** — applying KiCad's automatic flip to  
   stage 2 always reproduces stage 1 exactly (because the flip is  
   self-inverse), so what's displayed in KiCad always matches Ulticap's  
   original intent, regardless of rotation.

**Worked example:**  
  a component at rotation `R=4` (0°, mirrored), with a raw anchor  
  `A=0` (`HBL`, horizontal bottom-left):
1. Stage 1: `R=4` applies the L↔R mirror rule (`col = 2−col`) to `HBL` → `HBR`.  
   This is what Ulticap intends to display.
2. Stage 2: apply the same `R=4` KiCad-flip table to `HBR`  
   H-text flip for `R=4` is L↔R, so `HBR` → `HBL`.  
   `HBL` is what gets written to `.kicad_sch`.
3. Stage 3: KiCad renders `HBL` with its own automatic  
   `R=4` flip applied (L↔R again) → `HBR`, matching stage 1.

The full enumeration (every `H{r}.{a}` / `V{r}.{a}` combination worked  
through all three stages) is not reproduced here, since it is fully  
reconstructible from the two tables and worked example above; there is  
no need to maintain a third, larger, harder-to-keep-in-sync artifact.

### 10.5 PINTYPE values

Used as the value of a `PINTYPE=` attribute (Section 2) on a symbol pin:

`{PAS:passive, IN:input, OUT:output, OC:open collector, OE:open emitter,`  
` BI:bi-directional, TRI:tri-state, PWR:power, NC:no connection}`

### 10.6 Reserved tag list

The following tag names (used as `<tag>` in the universal attribute-line  
grammar, Section 2) have a defined meaning to Ulticap, as documented in  
the Ulticap user manual. This list is included primarily as a reference  
for identifying unknown tags encountered in SCH files. Any tag not listed  
here is a user-defined `<KEYWORD>=<value>` pair.

| Tag | Value | Meaning |
|---|---|---|
| `#` | pin#[,pin#...] | Physical part pin number(s) represented by symbol pins |
| `DESCR` | description | Brief description of the device function |
| `DEVICE` | part type name | Unique device type name for a symbol; non-homogeneous gates suffixed `/n` (e.g. `7423/1`) |
| `FILE` | filename | Hierarchical subsheet schematic filename (e.g. `FILE=MySchem3.SCH`) |
| `GATE1` | gate suffix | Starting REFDES gate suffix for non-homogeneous parts (e.g. `GATE1=C`) |
| `LABEL` | name(s) | Net name, symbolic pin name, bus name, or symbol name |
| `PARTS` | gate count | Number of gates of this type in the physical part (default 1) |
| `PINTYPE` | see 10.5 | Pin function; may be followed by `#`/`LABEL` lines as a multi-line block until the next `PINTYPE` |
| `PKG_TYPE` | PCB shape name | Preassigned PCB layout shape name |
| `PORT` | IN/OUT/BI/PAS | Used in place of `PINTYPE` on module port pins (subsheet boundary) |
| `POWER` | min,typ,max | Power dissipation in mW, for netlist/post-processing |
| `REFDES` | reference designator | Reference designator or template (`?` enables auto-annotation) |
| `SIGNAL` | net,pin#[,pin#...] | Net(s) assigned to symbol pins, including pins not visible on the symbol (e.g. power/ground) |
| `SPICE...` | — | SPICE simulation metadata |
| `TEXT` | free text | Placed free text |
| `UT_NETNAME` | name | Bus name |
| `UT_SHEETNETNAME` | name | Subsheet net/bus name |
| `VALUE` | electrical value | Component value (e.g. `VALUE=10K`) |
| `WIDTH` | trace width (mil) | Net trace width (e.g. `WIDTH=25`) |
| `WIRELABEL` | net name | Forces the wire attached to the identified pin to take this net name |

### 10.7 Colour palette (0–15)

The 16-entry Ulticap colour palette, screen-measured from DOS Ulticap  
CRT/EGA output (the EGA half-intensity level is 170/0xAA, not the  
theoretical 128). A colour field's effective palette index is always  
`colour & 0x0F`, which strips any flag bits Ulticap packs into higher  
bits of the same field (e.g. the linetype bits in `*LV`/`*LT` level-2  
records, Section 3).

| Index | RGB | Name |
|---|---|---|
| 0 | (0, 0, 0) | black |
| 1 | (0, 0, 142) | blue |
| 2 | (0, 170, 0) | green |
| 3 | (0, 170, 170) | cyan |
| 4 | (170, 0, 0) | red |
| 5 | (170, 0, 170) | magenta |
| 6 | (125, 85, 20) | brown |
| 7 | (170, 170, 170) | light gray |
| 8 | (40, 40, 65) | gray (dark blue-gray) |
| 9 | (32, 32, 235) | light blue |
| 10 | (0, 255, 0) | light green |
| 11 | (0, 170, 150) | light cyan |
| 12 | (170, 0, 85) | light red |
| 13 | (235, 40, 125) | light magenta |
| 14 | (227, 227, 0) | yellow |
| 15 | (227, 227, 227) | white |

### 10.8 Character set

The file's text is 8-bit. Ulticap for DOS draws it in its own font,  
which follows code page 437 (the IBM PC character set) except for eight  
codes. Observed from experiments (every code drawn on screen in V4.91):

| Code | Code page 437 | Drawn by Ulticap for DOS | Unicode |
|---|---|---|---|
| 176–178 | ░ ▒ ▓ | nothing (blank) | space |
| 219 | █ | ⊓ | U+2293 |
| 220 | ▄ | ▷ | U+25B7 |
| 221 | ▌ | ≷ | U+2277 |
| 222 | ▐ | ▫ | U+25AB |
| 223 | ▀ | ◁ | U+25C1 |

Codes 219–223 are IEC qualifying symbols, used in the symbols of  
Ulticap's IEC libraries (223 is 220 mirrored). Code 94 (`^`) is the  
overline toggle (10.3); codes 127 and 255 are editing keys in Ulticap  
and do not occur in files. KIUC reads a file as code page 437 with the  
eight codes replaced as above.

Ulticap V5.72 stores texts upper-cased, by the rules of the Windows  
character set. A file it has read in and exported again can therefore  
hold other codes than the original: 228, drawn `Σ` by Ulticap for DOS  
but a lower-case letter in the Windows set, comes back as 196. The  
original codes cannot be told from the exported ones.

[↑ Contents](#contents)
