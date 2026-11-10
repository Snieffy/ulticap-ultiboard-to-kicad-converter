# Coming from KIUC or KIUB

KIUT 1.0 is KIUC (Ulticap schematics to KiCad), KIUB (Ultiboard boards to KiCad) and the KIUC
Viewer in one program. It converts everything they did; this page lists what works differently
for someone who used them. New to KIUT? The [README](../README.md) is all you need.

---

## Contents

[↑ README contents](../README.md#converting-and-viewing)

- [The programs](#the-programs)
- [A schematic and its board together](#a-schematic-and-its-board-together)
- [The command line](#the-command-line)
- [Settings](#settings)
- [Where the files go](#where-the-files-go)
- [Schematic results](#schematic-results)
- [Board results](#board-results)
- [The viewer](#the-viewer)

## The programs

| Before | Now |
|---|---|
| `kiuc_gui.py`, `kiub_gui.py` | `kiut_gui.py`: one window for schematics, boards and libraries |
| `kiuc.py`, `kiub.py` | `kiut.py` |
| `kiuc_viewer.py` (KIUC Viewer) | `kiut_viewer.py` (KIUT Viewer); also *Open in Viewer* in the window |
| `kiuc_refdes.py`, Refdes Reannotate | `kiut.py --fix-only`; in the window *Schematic → Rename designators without converting…* |
| Netlist Compare | *Compare netlists* in the window, after a conversion |
| `kiub_v2v3.py` | not needed: a conversion makes the V4.60 DDF in `<name>_corrected`; on its own: `python -m kiut.board.kiub_v2v3` |

*Help* opens the documents as pages in your browser.

[↑ Contents](#contents)

## A schematic and its board together

Choose a schematic with its DDF (the window ticks *With its board* when the DDF lies next to the
schematic, under the same name; `kiut.py X.SCH X.DDF`) and both go into **one KiCad project**:

- **one designator check** over both — a designator KiCad won't accept is renamed on the schematic
  and the board alike;
- **every footprint linked to its symbol**, as KiCad's *Update PCB from Schematic* links them; a
  board converted again keeps the unit KiCad linked it to;
- the schematic converted alone compares itself with the board already there, and the board alone
  links to the converted schematic.

[↑ Contents](#contents)

## The command line

`kiut.py` takes the input files and which settings to use — every conversion choice is a setting
now ([Settings](#settings)):

    python kiut.py <file(s)> [--settings design|kiut|default] [--yes] [-q] [--fix-only]

| KIUC / KIUB option | Now |
|---|---|
| `-f`/`--font` | `[schematic] font` or `[board] font` |
| `--v5`, `--fix-refdes`, `--allow-missing-sheets`, `--kicad-colors` | `v5_header`, `fix_designators`, `allow_missing_sheets`, `kicad_colors` |
| `--block` | `block_library = yes` (a `.BLK` always converts to a block library) |
| `--library-only` | `[board] library_only = yes` |
| `--tec FILE` | the `.TEC` given as a second input |
| `--tec-split FILE…` | `tec_split` |
| `--v2v3-no-staircase-merge`, `--v2v3-no-chamfer` | `v2v3_staircase_merge = no`, `v2v3_chamfer = no` |
| `--pad-to-mask-clearance` and the other values | the setting of the same name |
| `-v` | always on: the board's details are always shown |
| `-o`, `-n`, `--dump`, `--dump-symbols`, `--ddf` | gone: the output folder is fixed (see [Where the files go](#where-the-files-go)); the dumps are in the window's *Schematic* menu |

Several files in one run where they belong together — Ulticap libraries, Ultiboard libraries and
library DDFs — and wildcards (`kiut.py libs/*.UTLIB`) are new. Exit codes: 0 done, 1 refused,
cancelled or failed, 2 a wrong command line.

[↑ Contents](#contents)

## Settings

- **Files:** `kiut.ini` (the window's current settings, read by `kiut.py` too), each design's own
  `<name>_kiut.ini` in its output folder (written by its first conversion, used again by the next),
  and `kiut_defaults.ini`, the defaults, which comes with KIUT. `kiuc.ini` and `kiub_gui.ini` are no
  longer written: the window offers to import them once (*File → Import old settings…*), and the
  viewer copies its own part of `kiuc.ini` into `kiut_viewer.ini` once.
- **Two fonts**, one for the schematic and one for the board.
- **Changed:**
  - `height_tune` is now one multiplier per kind of text: `ref_height_tune`, `value_height_tune`,
    `field_height_tune`, `text_height_tune`, all 1.0 (KIUC: 0.5), and `label_height_tune`, 0.8;
    an old file's `height_tune` is taken for each of them;
  - `fix_designators` is on by default (KIUC: only with `--fix-refdes`);
  - `along_wire_centre_lo` and `along_wire_centre_hi` are gone (see
    [Schematic results](#schematic-results)), passed over in an old file.
- **New:** `label_keep_alignment`, `block_library`, and KiCad's label and overbar offsets for a new
  project (`label_offset_percent`, `overbar_offset_percent`).

Every setting: [SETTINGS.md](SETTINGS.md).

[↑ Contents](#contents)

## Where the files go

A design's output is where it was: `<name>_KiCad` (and `<name>_corrected`) next to its files.
**Libraries converted on their own** — Ulticap symbol and block libraries, Ultiboard libraries and
library DDFs — now go into **KIUT's library folder**, `Documents/KIUT` in your user folder
(`Symbols`, `Blocks`, `Footprints`, each with one library table), instead of next to their files.
The folder can be changed: *File → Library output folder…*. Details: the README's
[Where the files go](../README.md#where-the-files-go).

[↑ Contents](#contents)

## Schematic results

A design converted again with the defaults can differ from KIUC's conversion:

- **Text sizes:** references, values, fields and free texts at 1.0 × Ulticap's size (KIUC 0.5), net
  labels at 0.8; the bus labels KIUT adds follow the label size. A design's own settings file keeps
  the values it was converted with.
- **Net labels:** where KiCad has to put a label's point elsewhere than Ulticap's anchor, the text
  stays on the side of the point where Ulticap drew it (its alignment turned if need be);
  `label_keep_alignment = yes` keeps Ulticap's alignment instead.
- **Sheet size:** free texts count with their whole size, so a sheet of texts gets a paper that
  fits them.
- **A new KiCad project** gets label and overbar offsets that suit full-size labels.
- **Fonts:** Ulticap Stroke (`fonts/`) matches Ulticap's text.

[↑ Contents](#contents)

## Board results

The board's conversion is KIUB's. New around it: several Ultiboard libraries and library DDFs in
one run (with one technology), library DDFs into the library folder, and the links to the schematic.
The board's text settings are tuned for the Ultiboard font, Ultiboard Stroke: see the README's
[Fonts](../README.md#fonts).

[↑ Contents](#contents)

## The viewer

- **KIUT Viewer**, with its own settings file, `kiut_viewer.ini`.
- It shows exactly what a file stores and never writes one: `--v5` is gone (the KIUT window opens
  the corrected copy when there is one).
- What reading a design reports: *View → Reading messages…*, counted in the status bar.
- A design opens fitted to the window.
- *Open in KIUT…* (was *Open in KIUC GUI…*); *Help → KIUT Viewer documentation*.

Everything about it: [VIEWER.md](VIEWER.md).

[↑ Contents](#contents)
