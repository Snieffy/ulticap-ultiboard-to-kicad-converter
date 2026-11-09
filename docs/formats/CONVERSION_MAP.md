# Conversion Map: What KIUC Carries Over

This table lists what an Ulticap design holds and what becomes of each item in KiCad 9. Two
columns show where the item lives in each of Ulticap's own formats: the ASCII `.SCH` file (record
or tag, see [FILEFORMAT_ULTICAP_SCH.md](FILEFORMAT_ULTICAP_SCH.md)) and the binary V5.71/V5.72 `.UTSCH` file
(see [FILEFORMAT_ULTICAP_UTSCH.md](FILEFORMAT_ULTICAP_UTSCH.md)). A dash
means the format has no such item; "not identified" means it may exist in the binary file but
has not been located.

The KiCad column uses three words consistently:
- **converted**: KiCad has its own equivalent, and the item becomes that;
- **kept as `UC_` field**: KiCad has no equivalent, so the value is kept as a symbol field named
  `UC_` followed by the Ulticap tag, shown or hidden as in Ulticap (see "Ulticap attributes in
  KiCad" in [SCHEMATICS.md](../SCHEMATICS.md));
- **not carried over**: the item is left out, with the reason.

Nothing in the "not carried over" rows affects the netlist.

Where a design's source files need correcting first (designators KiCad treats as unannotated, V5.72
export defects, symbol names Ulticap V4.91 can't open), KIUC corrects copies of them in
`<name>_corrected` and converts those; the rows below describe the result. See
[CORRECTED_FOLDER.md](../CORRECTED_FOLDER.md).

## Contents

[↑ README contents](../../README.md#file-formats)

- [Sheet and file](#sheet-and-file)
- [Hierarchy](#hierarchy)
- [Symbols and their attributes](#symbols-and-their-attributes)
- [Pins](#pins)
- [Wiring and nets](#wiring-and-nets)
- [Drawing and text](#drawing-and-text)
- [Symbol libraries](#symbol-libraries)

## Sheet and file

| Ulticap item | `.SCH` | `.UTSCH` | KiCad |
|---|---|---|---|
| Sheet size | bounds line in the header | sheet rectangle | converted: paper size; the drawing keeps its position on the sheet |
| Grid | 5th field of the bounds line | sheet field, 25 in every file (V5.72's fixed snap grid) | used to place the drawing on KiCad's grid; not stored |
| Customer name | `*P` | not identified | not carried over (no KiCad equivalent) |
| Format version, NRM number | version line | class schema numbers | not carried over (describe the file, not the design) |
| Root sheet name | `*R` | stored sheet name | used to find the root of a hierarchy; the KiCad root is named after the input file |
| Netlist data after the end marker | `**` then `*M` | — | not carried over (a separate tool's data) |
| View position, zoom | — | sheet fields | not carried over (editor state) |
| Undo history | — | undo store | not carried over (editor state) |
| Internal net and symbol IDs | — | stored per net and symbol | not carried over (KiCad numbers its own) |

[↑ Contents](#contents)

## Hierarchy

| Ulticap item | `.SCH` | `.UTSCH` | KiCad |
|---|---|---|---|
| Subsheet box | `*C` with `FILE=` | sheet frame part | converted: sheet symbol, always a rectangle |
| Subsheet file name | `FILE=` | `UT_SHEETNAME` | converted: Sheetfile (`.kicad_sch`) |
| Subsheet name as shown | `FILE=` text | `UT_SHEETNAME` text, at its stored position | converted: Sheetname |
| Subsheet box pin | pin of the box symbol | pin of the frame part | converted: sheet pin |
| Module port | `PORT=` or a `PORT_…` symbol | port symbol | converted: hierarchical label |
| Other attributes on a subsheet box | `*C` / `*S` attributes | attributes | kept as `UC_` field |

[↑ Contents](#contents)

## Symbols and their attributes

| Ulticap item | `.SCH` | `.UTSCH` | KiCad |
|---|---|---|---|
| Symbol name | `*S` name, repeated on each `*C` | the shape's name | converted: the library symbol's name, `\` `/` `:` replaced by `_` (a name taken twice gets `_2`); in `.SCH` a space becomes `_` too, corrected in the copies first (`74 00` → `74_00`). A placed symbol refers to `Ulticap:<name>`, a library no table lists: the schematic keeps its embedded symbols (README, "The project libraries") |
| Symbol definitions | every `*S` of every sheet, placed or not | the placed parts' shapes | converted: the project library `<name>.kicad_sym`, every definition once; one name defined differently in two `.SCH` sheets: the later one as `<name>_2` |
| Symbol drawing | `*S` lines, arcs, circles, rectangles, polygons | shape objects | converted: library symbol graphics |
| Placement, rotation, mirror | `*C` position and code `R` | part position and orientation | converted |
| Reference designator | `REFDES` | the part's designator text | converted: Reference (a gate letter becomes the unit). One without a trailing digit (`TP`) is unannotated in KiCad: converted unchanged, or numbered (`TP1`) by the designator fix (`fix_designators = yes`, the default) |
| Value | `VALUE` | `UT_VALUE` | converted: Value. A symbol definition (library symbol, or a design's stored copy) without `VALUE` gets its `DEVICE` as Value, its `UC_DEVICE` field hidden |
| PCB shape | `PKG_TYPE` | `UT_PKGTYPE` | converted: Footprint, `Ultiboard:<footprint>` named as KIUB names the board's footprints; with the design's DDF, the shape the board places for the part's reference designator, the schematic's own package then kept as the hidden `UC_PKG_TYPE` (README, "Footprints and the board"); a placeholder (`-`, `?`) or a power symbol: no footprint |
| Gate count | `PARTS` | sub-shape gate count | converted: units of a multi-unit symbol, unplaced gates included |
| Part with dissimilar gates (non-homogeneous, `<device>/<n>`) | one `*S` per gate type (`7423/1`, `7423/2`; `GATE1`) | one shape with one gate drawing per gate type | converted: one KiCad symbol per device, one unit per gate (gate letter A = unit 1), each unit with its gate type's drawing and pins; hidden power pins common to all units; placed gates become units of their package (`U2B` → `U2`, unit 2). `.SCH`: only `DEVICE=<device>/<n>` marks a gate type; a letter-ending designator on another `/` symbol gets a warning (V5.72's import would split it) |
| Hidden power pins | `SIGNAL=<net>,<pins>` | hidden pins with a net name | converted: hidden power pins joined to the net by name |
| Library part name | `DEVICE` | `UT_DEVICE` | kept as `UC_DEVICE` (Ulticap shows it where no value is shown, and so does KiCad) |
| Swappable pins | `PINSWAP` | `PINSWAP` | kept as `UC_PINSWAP` |
| SpiceAge and SPICE data | `SPICEAGE…`, `SPICE…` | same keys | kept as `UC_` fields, untranslated |
| SPICE data per pin | `SPICE#`, `SPICEAGEPIN` on pins | same keys on pins | kept as one hidden field per symbol, `<pin>:<value>` (`UC_SPICE#`, `UC_SPICEAGEPIN`) |
| Title-block entries | attributes of the title-block symbol | same | kept as `UC_` fields, and also converted into KiCad's title block |
| User attributes, `TEXT`, `TXT` | `<KEYWORD>=<value>` | attributes | kept as `UC_` field; a text drawn inside a single-unit symbol (`TXT`) too, so each placed part shows or hides it; a multi-unit symbol's shown `TXT` labels: converted, plain texts of its units, each gate's at its own position |
| Attribute defaults of the symbol definition | `*S` attributes | the shape's attributes | converted: Reference prefix, Value and Footprint of the library symbol; every other attribute kept as a `UC_` field of the library symbol, at the definition's position and visibility |
| Name text on a symbol | `LABELn`, a symbol-level `UT_NETNAME`, a `LABEL`/`WIRELABEL` away from the pins | same keys | converted: plain text at the same place; a hidden one is not carried over (it names no net) |
| Editor leftovers | `TRANSX`, `TRANSY` (old files) | — | not carried over |
| Empty attribute with value and tag hidden | any tag | any key | not carried over (nothing to show) |
| Copy of a symbol text added by V5.72's import of an old design | an extra `*C` entry equal to the `*S` one | an extra attribute equal to the shape's | not carried over (the original design has only the part's own entry; see [SCHEMATICS.md](../SCHEMATICS.md#known-ulticap-v572-export-defects-auto-corrected), "Template copies added by V5.72's import") |
| Schematic variant shown | — | the visible document variant | converted: the parts with that variant's values, as Ulticap shows them (see "Schematic variants" in [SCHEMATICS.md](../SCHEMATICS.md)) |
| Values in the other variants | — | the parts' other symbol variants | kept as hidden `UC_<tag>_<symbol variant>_<document variant>` fields, only where they differ from the shown variant |
| Part left out of a variant | — | no symbol variant of the part in that variant | left out of the shown variant: kept, marked do-not-populate, not in BOM and not on board, with a `UC_VARIANT_<document variant>` field "not fitted"; left out of another variant: that field only |

[↑ Contents](#contents)

## Pins

| Ulticap item | `.SCH` | `.UTSCH` | KiCad |
|---|---|---|---|
| Pin number | `#` | pin label | converted |
| Pin name | `LABEL` on a pin | `UT_PINNAME` | converted, at KiCad's fixed position |
| Pin name shown per pin | visibility per pin | visibility per pin | converted per symbol only: KiCad shows all of a symbol's pin names or none |
| Pin type | `PINTYPE` / `PORT` | pin type | converted: electrical type |
| Clock mark, inversion bubble, IEC marks | pin format code (the V5.72 export leaves some out) | pin flags | converted: pin shape |
| A pin's own net name | `WIRELABEL` on the pin | pin-level `UT_NETNAME` | converted: net label at the pin; on a single-pin symbol, see [NAMED_STUB.md](NAMED_STUB.md) |

[↑ Contents](#contents)

## Wiring and nets

| Ulticap item | `.SCH` | `.UTSCH` | KiCad |
|---|---|---|---|
| Wire | `*LV` / `*LT`, wire layer | wire | converted |
| Bus | `*LV` / `*LT`, bus layer | bus | converted; bus names in KiCad's vector or group form |
| Junction | `*V` | connection | converted |
| Wire-to-bus connection | `*V` on a bus | connection on a bus | converted: bus entry |
| Net name on a wire or bus | `*A` `LABEL` with its point on the wire | `UT_NETNAME` on the wire | converted: net or bus label |
| Automatic net names (`$$…`) | — (netlist only) | stored net names | not carried over (KiCad names unnamed nets itself) |
| Local power nets | `SIGNAL=` | hidden pins | converted as global nets (KiCad power nets are always global) |

[↑ Contents](#contents)

## Drawing and text

| Ulticap item | `.SCH` | `.UTSCH` | KiCad |
|---|---|---|---|
| Free text | `*X` | `TEXT` attribute or text object | converted: text |
| Free text at 180° or 270° | `*X` rotation `11520` / `17280` (Ulticap for DOS draws it upside down) | — (V5.72's import stores it horizontal or vertical, on the other side of its anchor) | converted: readable text at 0° or 90° covering the same area (KiCad never draws schematic text upside down) |
| Text size | size field | font height | converted |
| Text colour | 16-colour palette | full RGB | converted |
| Characters | 8-bit, Ulticap for DOS's own font: code page 437 except codes 176–178 (blank) and 219–223 (IEC symbols), see [FILEFORMAT_ULTICAP_SCH.md](FILEFORMAT_ULTICAP_SCH.md) §10.8 | 8-bit, Windows character set; V5.72 stores texts upper-cased, which changes some codes of a design read from a DOS file | converted: the characters Ulticap draws, as Unicode (see "Text characters and fonts" in [SCHEMATICS.md](../SCHEMATICS.md#text-characters-and-fonts)) |
| Font, bold, italic | — (one font) | Windows font and style | not carried over: one font for every text, KiCad's own or the one chosen as the schematic's font (`font`; [SCHEMATICS.md](../SCHEMATICS.md), "Text characters and fonts"); regular |
| Overline | `^…^` | `\^…\^` | converted: KiCad overbar |
| Sheet lines and rectangles | `*LV` / `*LT`, drawing layer | sheet line, rectangle | converted, with line style and colour |
| Sheet arcs and circles | — (the V5.72 export leaves them out) | sheet arc | converted |
| Line widths | thin or thick only | pen width | converted |
| Fills | — | fill colour | converted |
| Ellipses, pie shapes | — (exported as circles and arcs) | ellipse, pie | converted: a round ellipse as a circle, any other as four Bézier curves (filled: with a fill shape without outline beneath them); a pie as an arc, filled as a pie slice, and a line for its two radii |
| Embedded objects | — | OLE items | not carried over |

The losses of Ulticap V5.72's own "save as SCH" export, and the defects KIUC repairs in it, are
listed in [SCHEMATICS.md](../SCHEMATICS.md) under "V5.72 graphical data loss", "Known Ulticap V5.72 Export
Defects (Auto-Corrected)" and "Further losses in the export". Converting the `.UTSCH` file avoids
them.

[↑ Contents](#contents)

## Symbol libraries

A symbol library — `.UTLIB` (Windows versions, see [FILEFORMAT_ULTICAP_UTLIB.md](FILEFORMAT_ULTICAP_UTLIB.md)) or
`.LIB` (DOS version, see [FILEFORMAT_ULTICAP_LIB.md](FILEFORMAT_ULTICAP_LIB.md)) — holds the same data for each
symbol as a design holds for it once used, and its symbols convert as in the tables above. What
differs:

| Ulticap item | `.UTLIB` | `.LIB` | KiCad |
|---|---|---|---|
| Library | one file | one file | converted: one `.kicad_sym` of the same name, with a log |
| Symbol name | the device keyword (`PART\SO`) | the symbol's name | converted: the symbol's name, `\` `/` `:` replaced by `_` (`PART_SO`); a name taken twice gets `_2` |
| Device | the text of the device attribute (`PART`) | `DEVICE=` attribute | kept as `UC_DEVICE` field (Ulticap's own import of a `.UTLIB` keeps the keyword instead) |
| Gate types of a non-homogeneous part | one symbol, a gate drawing per type | one symbol per type (`DEV/1`, `DEV/2`) | converted: one symbol, a unit per gate; named `DEV_2` where a `.LIB` also holds a symbol `DEV`, or a `.UTLIB` symbol also a whole-package drawing `DEV` (written as the symbol `DEV`) |
| Text case | as entered | as entered | converted as stored (V5.72 upper-cases texts when importing a library or placing from it) |
| Characters | Windows character set | the DOS font's | converted, as for a design of that version |
| Symbol index, package list | index section | index | not carried over (the symbols themselves say the same, and decide) |
| Library save time | header | — | not carried over (describes the file) |

[↑ Contents](#contents)
