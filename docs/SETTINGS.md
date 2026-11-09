# KIUT settings

Every conversion setting lives in a settings file; the command line takes the input files and
which settings file to use, nothing else (and `--fix-only`, the designator fix without a
conversion):

    python kiut.py <file(s)> [--settings design|kiut|default] [--yes] [-q] [--fix-only]

## Contents

[↑ README contents](../README.md#converting-and-viewing)

- [The files](#the-files)
- [Which settings a run uses](#which-settings-a-run-uses)
- [The format](#the-format)
- [[schematic]](#schematic)
- [[board]](#board)

## The files

| File | Where | Written |
|---|---|---|
| `kiut_defaults.ini` | next to `kiut.py` | comes with KIUT: the defaults -- every setting with its value and a one-line comment; change a value to change a default (without the file, or for a value it can't give, the code's own, with a note) |
| `kiut.ini` | next to `kiut.py` | by the KIUT window: the current settings (the last used) |
| `<name>_kiut.ini` | in the design's output folder, `<name>_KiCad`; one for a schematic and its board (in KIUT's library folder: Ulticap symbol libraries `Symbols/Ulticap_lib_kiut.ini`, one for every library there; a block library `Blocks/<name>_kiut.ini`; an Ultiboard library `Footprints/<name>_lib_KiCad/<name>_kiut.ini`) | by the design's first conversion; after that, only by the window's *Save current settings* |

Neither of the last two has to be there: without them, the defaults. `kiut.ini` also keeps the window's
own sections, among them `[folders]` `libraries`: KIUT's library folder, where library-only
conversions go (Documents/KIUT when not set; the window's *File → Library output folder…*; the
environment variable `KIUT_LIBRARY_FOLDER` overrides it). kiut.py reads it too. The viewer keeps its own settings in
`kiut_viewer.ini` ([VIEWER.md](VIEWER.md)); they never change a conversion.

[↑ Contents](#contents)

## Which settings a run uses

Without `--settings`: the design's own `<name>_kiut.ini` when there is one, else `kiut.ini`, else
the built-in defaults. `--settings design`, `--settings kiut` or `--settings default` uses that
one; a file that isn't there yet gives the built-in defaults, with a warning. The output's first
line says which settings were used.

[↑ Contents](#contents)

## The format

A plain text file in two sections, `[schematic]` and `[board]`, one `key = value` per line; a line
starting with `;` is a comment. A setting a file doesn't hold keeps its default, so an older file
still works -- one with a `[common]` section holding `font` (an earlier KIUT's) gives that font to
both, with a note.

- **yes / no** settings take `yes` or `no` (also `true`/`false`, `on`/`off`, `1`/`0`).
- **Numbers** are written with a point: `0.254`. A number outside its suggested range is used,
  with a note: the range is what was found to work, not a limit.
- **Empty** means "worked out from the input" where a setting says so.
- A value that can't be read keeps its default, with a warning; so does a misspelt key.

[↑ Contents](#contents)

## [schematic]

### Font

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `font` | `KiCad Font` |  | Font of every text in the schematic: "KiCad Font", KiCad's own font, or the family name of any installed font. "DejaVu Sans Mono" also draws the characters KiCad's font lacks (e.g. the DOS box-drawing characters); "Ulticap Stroke" looks as Ulticap does. A font must be installed on every computer that opens the KiCad files; DejaVu Sans Mono and Ulticap Stroke are checked on this one (KiCad Font is used, with a warning, when missing), any other name is not. |

### Schematic

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `kicad_colors` | `no` | `yes` / `no` | yes: KiCad's default colour theme instead of Ulticap's measured colour palette -- no explicit colours are written, so every item takes its colour from the KiCad theme in use. |
| `v5_header` | `no` | `yes` / `no` | yes: the .SCH version header is rewritten to V5.00 before reading, for files made with Ulticap V5.x (Windows) and saved in the old format. Written in the design's corrected copies (<name>_corrected); the originals are never changed. .SCH/.BLK only. |
| `fix_designators` | `yes` | `yes` / `no` | yes: designators that don't end in a digit (KiCad doesn't take them as annotated) are renamed in corrected copies of the schematic -- and of the design's DDF, when there is one -- in <name>_corrected, and those are converted. no: the design is converted unchanged, with a warning and a log of the designators. |
| `allow_missing_sheets` | `no` | `yes` / `no` | yes: a sub-sheet the hierarchy refers to that isn't there is left out and the rest converted. no: the conversion stops. A known cause is a double-dot or double-extension file name left by a Ulticap V5.72 re-save ("SHEET2..SCH" or "SHEET2.SCH.SCH" instead of "SHEET2.SCH"). |
| `block_library` | `no` | `yes` / `no` | yes: a .SCH (with its sub-sheets) converts to a KiCad block library, <name>.kicad_blocks in KIUT's Blocks folder, instead of a schematic project: no .kicad_pro or .kicad_wks, title symbols left out. A .BLK always converts to a block library. Not for a .UTSCH. |
| `label_keep_alignment` | `no` | `yes` / `no` | A net label sits where KiCad can connect it -- a wire end or a point on the wire -- which is not always where Ulticap anchored it. no: the text stays on the side of that point where Ulticap drew it (its justification turned if need be). yes: Ulticap's justification kept, the text then possibly on the other side of the point. anchor_wire_max_dist applies either way. |

### Schematic fine tuning

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `ref_height_tune` | `1` | 0.1 to 2 | Multiplier applied to a reference's text height (over Ulticap's own size). 1.137 matches Ulticap's character size exactly; 1 comes close without moving text off its place. |
| `value_height_tune` | `1` | 0.1 to 2 | Multiplier applied to a value's text height (a power symbol's net name too). |
| `field_height_tune` | `1` | 0.1 to 2 | Multiplier applied to the height of every other symbol field, a symbol's label text and a sheet symbol's name and file. |
| `text_height_tune` | `1` | 0.1 to 2 | Multiplier applied to plain text: free texts and annotations that aren't labels. |
| `label_height_tune` | `0.8` | 0.1 to 2 | Multiplier applied to the height of the texts KiCad attaches to a wire -- net labels, bus labels, port labels, a pin's wire label: KiCad lifts a label off its wire in proportion to its size (see LABEL_OFFSET_PERCENT), and a label can't be moved away from its wire as in Ulticap. |
| `pin_num_height_tune` | `0.5` | 0.1 to 2 | Multiplier applied to pin number text height: KiCad keeps a pin number attached to its pin. |
| `pin_name_height_tune` | `0.5` | 0.1 to 2 | Multiplier applied to pin name text height and to plain text embedded in the symbol itself (its TXT graphics): KiCad keeps a pin name attached to its pin. |
| `offset_pname` | `0.005` | 0 to 2 | Pin name offset from pin endpoint, mm. KiCad default is 1.016; a small value minimises pin name overlap on dense ICs. |
| `anchor_wire_max_dist` | `100` | 0 to 2000 | Max distance (Ulticap units) a label's anchor may sit from its own POE's wire network before giving up on projecting it onto the wire and falling back to the POE's own position instead. |

### Schematic fine tuning (Ulticap line widths)

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `thin_line_width` | `6` | 1 to 50 | Width of a thin line, in Ulticap units (real mil = this ÷ 2). Ulticap DOS's fixed thin width, built into the program — not stored in the .SCH file. Applies to symbol lines and arcs and to sheet lines from .SCH files only: .UTSCH lines keep their own width, and wires and buses use KiCad's project defaults. |
| `thick_line_width` | `30` | 1 to 100 | Width of a thick line, in Ulticap units (real mil = this ÷ 2). Ulticap DOS's fixed thick width, built into the program — not stored in the .SCH file. Applies to symbol lines and arcs and to sheet lines from .SCH files only: .UTSCH lines keep their own width, and wires and buses use KiCad's project defaults. |

### Schematic: KiCad project (new projects only)

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `label_offset_percent` | `0` | 0 to 200 (KiCad's limit) | KiCad's label offset ratio in percent (Schematic Setup > Formatting), written into a .kicad_pro KIUT creates: how far a label's text sits from its wire, as a percentage of its size (KiCad's default 15). 0 to 200 only: KiCad reads any other value back as 15, though the file keeps it. An existing project keeps its own. |
| `overbar_offset_percent` | `105` | KiCad's | KiCad's overbar offset ratio in percent (Schematic Setup > Formatting), written into a .kicad_pro KIUT creates: how far an overbar sits above its text, as a percentage of its size (KiCad's default 123); negative draws the bar under the text. An existing project keeps its own. |

[↑ Contents](#contents)

## [board]

### Font

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `font` | `KiCad Font` |  | Font of every text on the board: "KiCad Font", KiCad's own font, or the family name of any installed font; "Ultiboard Stroke" looks as Ultiboard does. The text sizes follow font_height_ratio and font_width_ratio. The board's text settings are tuned for Ultiboard Stroke: another font can draw texts larger, and on copper they can then touch traces or pads (run the design rule check). A font must be installed on every computer that opens the KiCad files. |

### Board

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `library_only` | `ask` | `ask` / `yes` / `no` | A DDF that places components but holds no nets, copper or vias may be a design or a shape library. ask: the question is asked (yes, the command line's --yes: a design); yes: converted as a library only (the library board and the footprint library, no design board); no: as a design. A DDF holding shapes only is always a library. |
| `tec_split` | *empty* |  | An Ultiboard library's technology as a multi-file set (versions before V4.60), in this order: TOP; BOTTOM; INNER; DRILLS[; TRACES] -- the top, bottom and inner pad tables, the drill table and, optionally, the trace table; the same file may serve several pad tables. File names relative to the library's folder, separated by ";". Empty: the technology is the .TEC file given with the library (V4.60 and later). |

### Board defaults

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `pad_to_mask_clearance` | `0.05` | -1 to 1 | Solder mask expansion around each pad, mm. Positive values enlarge the mask opening beyond the pad edge. Written to the .kicad_pcb. |
| `solder_mask_min_width` | `0.15` | 0 to 1 | Minimum solder mask web width between adjacent mask openings, mm. Openings closer than this are merged by KiCad's plotter. Written to the .kicad_pcb. |
| `pad_to_paste_clearance` | `0` | -1 to 1 | Solder paste absolute clearance from the pad edge, mm (negative shrinks the paste aperture, e.g. for fine-pitch parts). Written to the .kicad_pcb. |
| `pad_to_paste_clearance_ratio` | `0` | -1 to 1 | Solder paste relative clearance, as a ratio of pad size (added to the absolute clearance above). Written to the .kicad_pcb. |
| `solder_mask_to_copper_clearance` | `0` | 0 to 1 | Minimum clearance the DRC enforces between solder mask openings and copper, mm. Written to the .kicad_pro. |

### Board fine tuning (geometry)

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `font_height_ratio` | `1.208` | 1 to 1.5 | KiCad text height = Ultiboard text height ÷ this ratio. Empirically fitted so converted text visually matches Ultiboard's own rendering. |
| `font_width_ratio` | `1.186` | 1 to 1.5 | KiCad text width = Ultiboard text width × this ratio. Empirically fitted, paired with font_height_ratio. |
| `line_width` | `0.075` | 0.01 to 0.5 | Default width for lines, arcs, and circles (board outline, silk, etc.) wherever the DDF doesn't specify one of its own, mm. |
| `snap_tolerance` | `0.1` | 0 to 1 | Maximum gap, mm, between adjacent board-outline endpoints before they're snapped closed into a single continuous outline. |

### Board fine tuning (clearance)

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `default_clearance` | `0.254` | 0.05 to 0.5 | Fallback copper clearance, mm, used where the DDF doesn't specify one. Alter cautiously -- values set too low can produce DRC clearance violations elsewhere on the board. |
| `default_width` | `0.254` | 0.05 to 0.5 | Fallback copper trace width, mm, used where the DDF doesn't specify one. Alter cautiously -- affects current-carrying capacity and DRC. |
| `default_thermal_gap` | `0.254` | 0.05 to 0.5 | Fallback thermal-relief air gap (spoke-to-pad), mm, used where the DDF doesn't specify one. Alter cautiously. |
| `default_thermal_width` | `0.254` | 0.05 to 0.5 | Fallback thermal-relief spoke width, mm, used where the DDF doesn't specify one. Alter cautiously. |

### Board V2/V3

| Setting | Default | Suggested range | What it does |
|---|---|---|---|
| `v2v3_text_width_ratio` | `0.8` | 0.3 to 1.5 | DDF V2/V3 pre-conversion only: estimated text width = text height × this ratio (unlike V4/V5, V2/V3 DDFs don't store text width directly). |
| `v2v3_text_thickness_ratio` | `0.1667` | 0.05 to 0.5 | DDF V2/V3 pre-conversion only: estimated text stroke thickness = text height × this ratio. |
| `v2v3_staircase_merge` | `yes` | `yes` / `no` | DDF V2/V3 only: staircase traces (a diagonal drawn as grid steps) recovered as diagonal traces. |
| `v2v3_staircase_limit_mil` | *empty* | 0 to 25 | DDF V2/V3 only: the longest single staircase step recovered as a diagonal, mil. Empty: the file's own declared grid step. Never more than 25 mil. |
| `v2v3_chamfer` | `yes` | `yes` / `no` | DDF V2/V3 only: ordinary 90-degree trace corners chamfered (slanted). |
| `v2v3_corner_slant_limit_mil` | *empty* | 0 to 25 | DDF V2/V3 only: the most trimmed off each leg of a 90-degree corner when chamfering it, mil. Empty: as v2v3_staircase_limit_mil. |

[↑ Contents](#contents)
