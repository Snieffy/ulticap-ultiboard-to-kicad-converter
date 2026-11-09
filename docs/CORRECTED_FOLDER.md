# The corrected folder and the KiCad project folder

How KIUT lays out a design's files, and how its two converters -- KIUC for the Ulticap
schematic, KIUB for the Ultiboard board -- share the corrections they make to its source files.
It's written for users who want to know where their files go, and for anyone changing either
converter.

## Contents

[↑ README contents](../README.md#converting-and-viewing)

- [1. Layout](#1-layout)
- [2. What goes into `<name>_corrected`](#2-what-goes-into-name_corrected)
- [3. The working set](#3-the-working-set)
- [4. `_corrected.json`](#4-_correctedjson)
- [5. Rules](#5-rules)
- [6. Logs](#6-logs)
- [7. Overwriting the KiCad output](#7-overwriting-the-kicad-output)
- [8. Project libraries, and why the design files don't use them](#8-project-libraries-and-why-the-design-files-dont-use-them)

## 1. Layout

```
<folder of the originals>\
    <name>.SCH (+ sub-sheets) | <name>.UTSCH, <name>.DDF   the originals: never written
    <name>_corrected\    corrected copies, under the originals' own names, and _corrected.json
    <name>_KiCad\        the KiCad project: KIUC's schematic, KIUB's board, one <name>.kicad_pro,
                         the project libraries <name>.kicad_sym and <name>.pretty, sym-lib-table,
                         fp-lib-table (§8)
    <name>_lib_KiCad\    a design's DDF converted as a library only
```

Library-only conversions of library files -- Ulticap symbol libraries, block libraries, Ultiboard
libraries, library DDFs -- go into KIUT's library folder instead (`Documents\KIUT` unless chosen; its `Symbols`,
`Blocks` and `Footprints`, README "Where the files go"); they have no originals' folder to keep
together, and no corrected copies.

- `<name>` is the design name: the file name (without extension) of the root sheet (`.SCH`), of the
  `.UTSCH`, or of the DDF. A schematic and a board are one design when they share this name.
- Folder names are compared ignoring letter case, as Windows does.
- The folders sit next to the originals and can't be chosen. KiCad only treats `<name>.kicad_sch`,
  `<name>.kicad_pcb` and `<name>.kicad_pro` as one project when they share one name in one folder.
  The fixed layout also lets each tool find the other's files and corrections. To convert a design
  somewhere else, copy its originals there first.
- `<name>_KiCad` and `<name>_corrected` only exist once something is written into them. A design that
  needs no corrections has no `<name>_corrected`.

[↑ Contents](#contents)

## 2. What goes into `<name>_corrected`

| Correction | Manifest name | Written by | Files |
|---|---|---|---|
| V5.x header patch, for a V5.72 file saved in the old `.SCH` format (`v5_header`, the window's *SCH V5.x header*) | `v5-header` | KIUC | `.SCH` sheets |
| Record-integrity repair (V5.72 export defects) | `record-integrity` | KIUC | `.SCH` sheets |
| Pin-label override correction (V5.72 export defect) | `pin-labels` | KIUC | `.SCH` sheets |
| Space → underscore in symbol names (Ulticap V4.91 can't open them) | `symbol-names` | KIUC | `.SCH` sheets |
| Reference designators KiCad treats as unannotated (`TP` → `TP1`) | `refdes` | KIUC (`fix_designators`, *Rename designators without converting…*, `--fix-only`) | `.SCH` sheets or the `.UTSCH`; the DDF |
| V2/V3 DDF → V4.60 | `v460` | KIUB | the DDF |

- **A complete design.** When a correction changes any sheet of a `.SCH` hierarchy, every sheet is
  copied, unchanged where nothing needed correcting. The folder then opens in Ulticap as it is, and
  the `FILE=` references between sheets are left unchanged. A DDF is copied only when a correction
  changes it.
- **Adopting the corrections.** Copy the folder's design files over the originals. They keep the
  originals' names, so no renaming is needed. `_corrected.json` and the log files are not design
  files.
- **Symbol names.** A DDF never holds a schematic symbol name: a part's DDF alias is its `VALUE`
  (upper-cased, spaces → `_`, at most 19 characters), and its shape is its package. So the
  symbol-name correction leaves the schematic–board link intact.

[↑ Contents](#contents)

## 3. The working set

- **Reading.** Every tool reads each of the design's files from `<name>_corrected` when a copy is
  there, and from the original otherwise.
- **Correcting.** Every correction reads from the working set and writes into `<name>_corrected`.
- **Order doesn't matter.** KIUB's V4.60 DDF and KIUC's reannotated DDF build on each other,
  whichever tool runs first.
- **Picking a file inside the folder.** A file picked inside `<name>_corrected` belongs to the design
  `<name>`. Its originals are in the parent folder, and its output goes to the parent's
  `<name>_KiCad`, never into the corrected folder. Its original must still be there.

[↑ Contents](#contents)

## 4. `_corrected.json`

One JSON file in the folder. It's updated by whichever tool writes to the folder, and always written
last.

```json
{
  "format": 1,
  "design": "DESIGN",
  "originals": {
    "DESIGN.SCH": {"size": 12345, "sha256": "…"},
    "DESIGN_1.SCH": {"size": 6789,  "sha256": "…"},
    "DESIGN.DDF": {"size": 23456, "sha256": "…"}
  },
  "files": {
    "DESIGN.SCH": {"sha256": "…", "corrections": ["record-integrity", "refdes"]},
    "DESIGN_1.SCH": {"sha256": "…", "corrections": []},
    "DESIGN.DDF": {"sha256": "…", "corrections": ["v460", "refdes"]}
  },
  "log": [
    {"time": "2026-10-25T12:00:00", "tool": "KIUC", "file": "DESIGN.SCH",
     "correction": "refdes", "detail": "TP -> TP1"}
  ],
  "logs": ["_refdes_log.txt"]
}
```

| Key | Meaning |
|---|---|
| `format` | `1`. A tool refuses to use a manifest of a format it doesn't know (see rule 2). |
| `design` | The design name. |
| `originals` | Each original as it was when it was first copied. The key is the path relative to the originals' folder, `/`-separated; a sub-sheet elsewhere keeps its `../`. |
| `files` | Each copy, by file name, as a tool last wrote it, with every correction it has had. A sheet copied only to keep the hierarchy complete has none. |
| `log` | Every correction written, oldest first. |
| `logs` | The tools' log files in the folder. A redo removes them along with the copies. |

[↑ Contents](#contents)

## 5. Rules

1. **Read-only originals.** If nothing can be written in the originals' folder (a CD, a read-only
   share), the conversion is refused: "Copy the design to a folder you can write to, then convert it
   there." Write access is tested by creating a file, because permissions alone don't tell.
2. **Out of date.** The copies no longer match the design when:
   - an original the manifest records has changed or is gone from disk. Every recorded original is
     checked, including ones the checking tool doesn't read itself (KIUB doesn't read a schematic
     hierarchy);
   - or the folder holds the design's sheets and a sheet isn't recorded (it's new to the hierarchy);
   - or there are copies but the manifest is missing or unreadable.

   The tool then asks: **redo from the originals** (the default) or cancel. A redo empties the
   folder of everything the manifest lists and redoes this tool's own corrections. The other tool
   redoes its own on its next run. This is judged per design, not per file, because designators are
   unique across the whole hierarchy.
3. **Edited by hand.** A copy whose content no longer matches its `files` entry has been changed
   outside the tools (for example saved in Ulticap). It's read as it is, and corrections build on it.
   It's never thrown away unasked: if the copies are also out of date, the redo question says so and
   defaults to **Cancel**.
4. **Atomic writes.** Each file is written to a temporary name in the folder and then renamed, so no
   one ever reads a half-written file. The manifest is written last.
5. **Unattended runs.** On the command line, without a terminal to answer a question, the default
   answer is taken, and the output says so.

[↑ Contents](#contents)

## 6. Logs

The designator fix (`fix_designators`, *Rename designators without converting…* in the KIUT window, `kiut.py
--fix-only`) writes a log of every designator it renames:

- **Fix applied:** `<name>_corrected\_refdes_log.txt`, next to the copies it describes. It's listed
  under `logs` in the manifest.
- **`fix_designators = no`, or nothing could be renamed:** `<name>_KiCad\<name>_refdes_log.txt`, next to
  the converted schematic, which keeps the unannotated designators.

[↑ Contents](#contents)

## 7. Overwriting the KiCad output

- **KiCad files.** A conversion that would replace existing KiCad files in `<name>_KiCad` or
  a library folder lists them and asks first -- once for a schematic and its board converted
  together. In the KIUT window that's a dialog. On the command line it's `Replace them? [y/N]`,
  which cancels when there's no answer to read; `--yes` replaces (the question shown with its
  answer).
- **The shared project file.** `<name>.kicad_pro` is never replaced, only updated.
  - It starts as KiCad 9 starts one: the part both of KiCad's editors write, and the part of the
    editor whose file is converted (the schematic editor's: `erc`, `schematic`, `sheets`; the
    board editor's: `board.design_settings`). A tool adds what is missing of these to a file that
    is already there, and never changes a value in it.
  - Then each tool sets only its own keys:
    - KIUB: its design settings in `board.design_settings` (its defaults, rules and rule
      severities), `net_settings` and `pcbnew.last_paths`.
    - KIUC: `erc.rule_severities` and `schematic.connection_grid_size` and
      `schematic.page_layout_descr_file`; it also empties `sheets`.
    - Both: `meta.filename`.

  So a schematic converted alone gets KiCad's schematic-only project, a board alone KiCad's
  board-only one, and both the same file whichever tool runs first.
- **The library tables.** `sym-lib-table` and `fp-lib-table` are never replaced, only updated: each
  tool adds or replaces its own entry and keeps every other one. A file of that name that isn't a
  table of that kind is left alone, and the log says so.
- **Board and schematic linked.** KIUB links each footprint to its symbol in the converted
  schematic in `<name>_KiCad`, as KiCad links them itself, so *Update PCB from Schematic* matches
  them without re-linking (see "Footprints linked to their symbols" in [BOARDS.md](BOARDS.md)).

[↑ Contents](#contents)

## 8. Project libraries, and why the design files don't use them

| | Library, listed in the folder's table | What the design file refers to |
|---|---|---|
| Schematic symbols (KIUC) | `<name>.kicad_sym` in `sym-lib-table`, nickname `<name>` | `Ulticap:<symbol>`, a nickname no table lists |
| Board footprints (KIUB) | `<name>.pretty` in `fp-lib-table`, nickname `<name>` | `Ultiboard:<footprint>`, a nickname no table lists |
| A symbol's Footprint field (KIUC) | — | `Ultiboard:<footprint>`, the board's footprint for that part |

- **Contents.** `<name>.kicad_sym` holds every symbol the design defines, also those no sheet places;
  `<name>.pretty` every shape the DDF defines. A design without symbols, or a DDF without shapes, gets
  no library and no table entry. Library-only conversions list each library in their folder's
  table the same way: `Symbols` its `sym-lib-table`, `Blocks` its `design-block-lib-table`,
  `Footprints` its `fp-lib-table` (and each library board's own, one folder up), `<name>_lib_KiCad`
  its `fp-lib-table`.
- **Nickname.** The design's or library's name with every character KiCad or a file name refuses
  (`\ / : * ? " < > |` and control characters) made `_`.
- **Unlinked.** Each `.kicad_sch` and `.kicad_pcb` embeds the symbols and footprints it uses, and those
  stay the reference. Designs are often never back-annotated, and shapes made in a design are often
  never exported, so a library entry of the same name can differ from what the design uses; KiCad's
  *Update from library* would then replace it without warning. The libraries are there to browse and
  to place new parts from; tying a design to them is the user's deliberate step in KiCad.
- **Footprint names.** A footprint is named after its DDF shape: the nickname rule above, and a name
  that differs from an earlier shape's only in letter case (one file on Windows and macOS) gets `_2`,
  `_3`, …, in DDF order. A V2/V3 DDF gives the same names as its V4.60 conversion.
- **The board decides the footprint.** With the design's DDF (its corrected copy when there is one),
  a part's Footprint field is the footprint the DDF places for the part's reference designator (its
  `*C` record), so *Update PCB from Schematic* finds board and schematic in agreement. A schematic
  package that differs is kept as the hidden field `UC_PKG_TYPE`, and the log lists every such part.
  A part the DDF doesn't place keeps its schematic package, named by the same rule.

[↑ Contents](#contents)
