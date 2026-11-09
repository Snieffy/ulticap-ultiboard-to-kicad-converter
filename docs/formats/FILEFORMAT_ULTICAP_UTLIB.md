# Ulticap `.UTLIB` File Format

Reverse-engineered description of the symbol library files of Ulticap V5 for Windows, derived from
the analysis of real `.UTLIB` files and comparison with Ulticap's own import of the same libraries
into a design (`.UTSCH`, described in [FILEFORMAT_ULTICAP_UTSCH.md](FILEFORMAT_ULTICAP_UTSCH.md)). The DOS
version's libraries (`.LIB`) are a different format.

## Contents

[↑ README contents](../../README.md#file-formats)

1. [The file](#1-the-file)
2. [Symbol index](#2-symbol-index)
3. [Font list](#3-font-list)
4. [String pool](#4-string-pool)
5. [Class table](#5-class-table)
6. [Symbol bodies](#6-symbol-bodies)
7. [A library symbol in a design](#7-a-library-symbol-in-a-design)
8. [How KIUC converts a library](#8-how-kiuc-converts-a-library)
9. [Unknowns and forms never seen](#9-unknowns-and-forms-never-seen)

---

**Scope.** The libraries examined: one written in 1998 holding over 3,000 symbols, one exported
by Ulticap V5.72, and two test libraries exported by the same version from symbols drawn with
every drawing tool (lines in every style, polylines, rectangles, ellipses, pies and polygons,
filled and unfilled). All have the same layout; they differ only in which classes they use (§5).
Each test library was compared with the design its symbols were placed in.

**Conventions.** As in [FILEFORMAT_ULTICAP_UTSCH.md](FILEFORMAT_ULTICAP_UTSCH.md): little-endian, unaligned values;
coordinates are signed 32-bit integers in Ulticap units (1 unit = 2 mil = 0.0508 mm), y up; colours
are Windows `COLORREF`s. `STRING` is the string of FILEFORMAT_ULTICAP_UTSCH.md §3.1 (a length byte, or
`0xFF` and a `WORD`, or `0xFFFF` and a `DWORD`, then the characters, no terminator), in Windows-1252.
`POINTS` is a `WORD` count and that many pairs of `INT32` (x, y). Field names are descriptions; the
file stores none.

---

## 1. The file

A `.UTLIB` is a flat file, not a compound document like a `.UTSCH`: a fixed header, a directory of
five sections, and the sections one after the other. The symbols are stored as objects with class
tags, class markers and drawing records, as in a `.UTSCH`, but with classes of their own (§5).

### 1.1 Header

| Offset⠀⠀ | Type | Value | Meaning |
|---|---|---|---|
| 0x00 | 16 bytes | `ULTImate Library` | signature, no terminator |
| 0x10 | `WORD` | 100 | format number; also the schema of every class in the file |
| 0x12 | `BYTE` | 1 | unknown (1 in every file examined) |
| 0x13 | `DWORD` | | save time, Unix time (UTC); matches the file's date |
| 0x17 | 5 × (`DWORD` offset, `DWORD` count) | | the directory (§1.2) |
| 0x3F | `BYTE`, `BYTE` | 0, 0 | unknown (0 in every file examined) |

The header is 0x41 bytes long; the first section starts right after it.

### 1.2 Directory

The five entries, each a file offset and a count, in this order:

1. symbol index (§2)
2. string pool (§4)
3. font list (§3)
4. symbol bodies (§6)
5. class table (§5)

On disk the sections follow each other without gaps in a different order: **index, fonts,
strings, classes, bodies**. Each section starts with a `DWORD` that repeats its count. The bodies
run to the end of the file.

[↑ Contents](#contents)

---

## 2. Symbol index

`DWORD` count, then per symbol:

| Field | Type | Meaning |
|---|---|---|
| names | `DWORD` n, n × `STRING` | the gate-drawing names (§6.2): one for an ordinary symbol, one per gate type for a non-homogeneous part (`DEV/1`, `DEV/2`); some such symbols hold first a drawing named `DEV` itself, the whole package in one drawing |
| packages | `DWORD` n, n × `STRING` | a package list; may be just `-` while the package attribute (§6.4) names the package |
| designator | `STRING` | the designator template (`U?`), as in the body |
| device | `STRING` | the device keyword, as in the body (§6.4) |
| offset | `DWORD` | file offset of the symbol's body |

The index lets a program list the library and fetch one symbol without reading the others.
Offsets ascend; the names need not be sorted. Everything in the index is repeated in the body, and
where the two differ (the package list) the body is what Ulticap shows.

[↑ Contents](#contents)

---

## 3. Font list

`DWORD` count, then that many Windows `LOGFONT`s, 60 bytes each (five `INT32`, eight `BYTE`s, a
32-byte face name). Texts name a font by its position in this list (§6.3). Unlike a `.UTSCH`'s font
entries there is no usage count. The face name's bytes after its terminator are leftover memory.

Every font of the libraries examined has character set 0 (ANSI), so strings are Windows-1252.

[↑ Contents](#contents)

---

## 4. String pool

`DWORD` count, then that many `STRING`s: sorted bytewise, unique, the first one empty. Every string
of every symbol — names, keys, values, pin numbers, texts — is stored as a `DWORD` index into this
pool, not in place.

[↑ Contents](#contents)

---

## 5. Class table

`DWORD` count, then per class: `WORD` `0xFFFF`, `WORD` schema (100), `WORD` name length, the name
(ASCII). Classes are numbered from 1 in table order. In a body a class is written as `0x8000 |`
its number, both for an object's own tag and for a class marker. The table holds only the classes
the file uses.

| Library | Classes |
|---|---|
| written by V5.72 | `CBBoxNC`, `CBBox`, `CDrawObject`, `CUndoableObject`, `CPolyLine`, `CMultiPointObject`, `CLibPin`, `CPin`, `CLibText`, `CText`, `CLibAttribute`, `CAttribute`, `CRectangle`, `CTwoPointObject` |
| written in 1998 | `CBBox`, `CDrawObject`, `CUndoableObject`, `CPolyLine`, `CMultiPointObject`, `CLibPin`, `CPin`, `CLibText`, `CText`, `CLibAttribute`, `CAttribute`, `CArc` |
| test library (drawing objects) | `CBBoxNC`, `CBBox`, `CDrawObject`, `CUndoableObject`, `CRectangle`, `CTwoPointObject`, `CEllipse`, `CPie`, `CArc`, `CPolygon`, `CMultiPointObject`, `CLine`, `CPolyLine`, `CLibAttribute`, `CAttribute`, `CText` |

No object refers to another by number: each body is complete apart from the class table, the font
list and the string pool.

[↑ Contents](#contents)

---

## 6. Symbol bodies

`DWORD` count (the index's count), then the symbols. Each starts at its index offset and ends where
the next one starts, the last at the end of the file. Nothing gives an object's length, so this is
the check that a symbol was read with the right layouts.

### 6.1 Symbol

| Field | Type | Meaning |
|---|---|---|
| | `BYTE`, `BYTE` | 1, 0 in every symbol; unknown |
| in netlist | `DWORD` | 1 for a component, 0 for the sheet symbol |
| gate drawings | `DWORD` n, then n gate drawings (§6.2) | |

### 6.2 Gate drawing

| Field | Type | Meaning |
|---|---|---|
| name | `DWORD` string index | the gate drawing's name, as in the index (`GATE2`, `DEV/1`) |
| designator text | `DWORD` string index | `$`: where the designator is drawn |
| gate count | `DWORD` | the gates of the package drawn with this drawing |
| external gate swap | `DWORD` | 1 or 0 |
| objects | `DWORD` n, then n objects (§6.3) | in this order: the bounding box, graphics, pins, attributes |

### 6.3 Objects

An object is its class tag, then the data of each class of its chain, each base class introduced by
its class marker `M(...)`. The **drawing record** is `M(CDrawObject)`, pen colour `DWORD`, pen
width `DWORD`, pen style `DWORD`, `POINTS`, `M(CUndoableObject)`.

| Class⠀⠀⠀⠀⠀⠀ | Data after the tag |
|---|---|
| `CBBoxNC` | `M(CBBox)`, drawing record — 2 points: the symbol's box, minimum and maximum corner |
| `CBBox` | drawing record — the same |
| `CPolyLine` | `M(CMultiPointObject)`, drawing record — an open line |
| `CLine` | `M(CPolyLine)`, `M(CMultiPointObject)`, drawing record — 2 points: one straight segment |
| `CArc` | drawing record — 3 points: start, end, centre |
| `CRectangle` | `DWORD` filled (0/1), `DWORD` fill colour, `M(CTwoPointObject)`, drawing record — 4 corners |
| `CEllipse` | as `CRectangle`: `DWORD` filled, `DWORD` fill colour, `M(CTwoPointObject)`, drawing record — the 4 corners of the ellipse's bounding rectangle |
| `CPolygon` | `DWORD` filled, `DWORD` fill colour, `M(CMultiPointObject)`, drawing record — the corners, closed back to the first |
| `CPie` | `DWORD` filled, `DWORD` fill colour, `M(CArc)`, drawing record — 3 points as an arc's; the wedge is closed by a line from each end to the centre |
| `CLibPin` | `M(CPin)`, five `BYTE`s (below), drawing record (pen colour = pin colour; 1 point: the pin's position); pin numbers: `DWORD` n, n × `DWORD` string index, one per gate; the number label: a `CLibText`; `WORD` n, n × `CLibAttribute` |
| `CLibText` | `M(CText)`, text (below) |
| `CLibAttribute` | `M(CAttribute)`, `DWORD` shown, `DWORD` kind, `DWORD` keyword (string index), `M(CText)`, text (below) |

**Pin bytes**, in this order: internal connect, swap, orientation, pin type, style — the fields of
a `.UTSCH` shape pin (FILEFORMAT_ULTICAP_UTSCH.md §5.3) in another order, with the same codes.

**Text** (19 bytes, then a drawing record):

| Field | Type | Meaning |
|---|---|---|
| visible | `DWORD` | 0 hidden, 1 shown |
| anchor | `BYTE` | 0–8, the 3 × 3 grid of FILEFORMAT_ULTICAP_UTSCH.md §4.2 |
| orientation | `DWORD` | degrees: 0, or 90 for a vertical text |
| font | `DWORD` | position in the font list (§3) |
| string | `DWORD` | string index |
| overlines | `WORD` count, then that many `DWORD`s | always 0 |
| drawing record | | pen colour = text colour; 1 point: the anchor point |

A pin's number label is one text for all its gates; the pin numbers (one per gate) are drawn with
it. A text stores no frame (a `.UTSCH`'s zoom-dependent display box is not kept).

### 6.4 Attributes

| Kind | Keyword | Text | Visibility |
|---|---|---|---|
| 0 | the key (`UT_VALUE`, `UT_PKGTYPE`, `UT_PINNAME`, `UT_NETNAME`, a user key), or empty for a graphic text | the value | the text's own visible field ("shown" is 0) |
| 4 | the device keyword: the symbol's name (`PART\SO`) | the device it shows (`PART`) | "shown" |
| 5 | the designator template (`U?`) | `$` | "shown" |

Each gate drawing has one kind-4 and one kind-5 attribute. Keys are upper case, with rare
exceptions in mixed case. Pins carry kind-0 attributes only.

[↑ Contents](#contents)

---

## 7. A library symbol in a design

Imported into a design (or placed from the library), a symbol becomes a shape
(FILEFORMAT_ULTICAP_UTSCH.md §5) holding the same data — a check of every symbol of a library of over
3,000 against Ulticap's own import found no other difference than these:

- The shape is named after the device keyword (kind 4), not the index name: the gate drawings of
  a non-homogeneous part (`DEV/1`, `DEV/2`) become one shape `DEV` with two gate drawings, and two
  library symbols with one index name but different keywords become two shapes.
- Kind 4 becomes the gate drawing's `UT_DEVICE` attribute, with the **keyword** as its value: the
  device text is not kept. Kind 5 becomes the shape's designator template (its keyword) and the
  gate drawing's `$` text, visible as "shown" says.
- The bounding box becomes the gate drawing's four-corner frame.
- Each gate's pin number gets its own copy of the number label.
- Texts are upper-cased (`FILEFORMAT_ULTICAP_UTSCH.md` §4.3); keys stored in mixed case become upper case.
- The shape gets `UT_DEVICE`, `UT_PKGTYPE` and `UT_VALUE` attributes of its own with the value `-`.
- Where a pin has two `UT_PINNAME` attributes the import keeps the last.

[↑ Contents](#contents)

---

## 8. How KIUC converts a library

KIUC rebuilds each symbol as the shape of §7 and converts it with the code that converts a
design's shapes, so a library symbol becomes the same KiCad symbol as the same symbol in a
converted design, except where KIUC follows the library rather than the import:

- **Names:** the KiCad symbol is named after the device keyword, with `\`, `/` and `:` replaced
  by `_` (KiCad's library names cannot hold them). A second symbol with a keyword already taken
  gets `_2` (then `_3`, …) and a warning. The gate types of a non-homogeneous part become one
  KiCad symbol named after the device, with one unit per gate.
- **Whole-package drawing:** a gate drawing named exactly as the device, beside its gate types,
  is not a gate type but the whole package (Ulticap's own export numbers it `DEV/1` and the gate
  types after it). KIUC writes it as an ordinary symbol `DEV` and the gate types, joined, as `DEV_2`,
  reporting the name taken — the form of the same device in a DOS library.
- **Device:** the `UC_DEVICE` field holds the device text (`PART`), not the keyword.
- **Value:** the symbol's `VALUE` (even a `?`); without one, the device text, with the `UC_DEVICE`
  field then hidden. A non-homogeneous part has the device's name as Value.
- **Labels:** a multi-unit symbol draws its keyless graphic texts (`TXT`) as plain texts of its
  units, each gate drawing its own; a single-unit symbol keeps them as fields.
- **Case:** texts keep their case.
- **No placeholders:** no `-` device, package or value is added.
- **Misattached texts:** some symbols carry a gate-drawing label as a second pin name, or as a
  `UT_NETNAME` on the gate drawing itself. A pin's first `UT_PINNAME` names the pin; a further one,
  and a `UT_NETNAME` on the gate drawing, become plain texts at their own positions. Each is
  reported.
- **Field positions:** every field keeps its library position when the symbol is placed in KiCad
  (`do_not_autoplace`).

Sheet symbols and port symbols are converted as ordinary symbols.

[↑ Contents](#contents)

---

## 9. Unknowns and forms never seen

- Header byte 0x12 (1), header bytes 0x3F–0x40 (0, 0) and a symbol's two first bytes (1, 0):
  constant in every file examined, meaning unknown; not needed to read a library.
- Classes not listed in §6.3: none seen. KIUC stops at a symbol using one, naming the symbol and
  the class: nothing gives an object's length, so it cannot be skipped. A symbol that does not end
  where the next one starts is refused the same way.
- Strings of 255 characters or more and counts of 65,535 or more: not seen; the long forms of
  FILEFORMAT_ULTICAP_UTSCH.md §3.1 are assumed.
- Overline positions in a text: the count is always 0.

[↑ Contents](#contents)
