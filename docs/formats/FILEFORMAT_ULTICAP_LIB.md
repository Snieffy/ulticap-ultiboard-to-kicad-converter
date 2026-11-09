# Ulticap for DOS `.LIB` File Format

Reverse-engineered description of the symbol library files of Ulticap for DOS (versions 4.x),
derived from the analysis of real `.LIB` files and comparison with the source files they were
compiled from and with the output of `LIBDECMP`, the DOS library tool that writes a library back
as `.SCH` text. The Windows versions' libraries (`.UTLIB`) are a different format, described in
[FILEFORMAT_ULTICAP_UTLIB.md](FILEFORMAT_ULTICAP_UTLIB.md).
Ultiboard for DOS also writes `.LIB` files: shape (footprint) libraries in a different container,
told apart by the signature `ULTImate-LIB` (with a hyphen, followed by a zero byte) where an Ulticap
library has `ULTImate LIB`. They are described in
[FILEFORMAT_ULTIBOARD_LIB_TEC.md](FILEFORMAT_ULTIBOARD_LIB_TEC.md), and KIUT converts them as a board input (KIUB, its board
converter).

## Contents

[↑ README contents](../../README.md#file-formats)

1. [The file](#1-the-file)
2. [Header](#2-header)
3. [Index](#3-index)
4. [Symbol bodies](#4-symbol-bodies)
5. [String blocks](#5-string-blocks)
6. [A symbol as `.SCH` text](#6-a-symbol-as-sch-text)
7. [How KIUC converts a library](#7-how-kiuc-converts-a-library)
8. [Unknowns and forms never seen](#8-unknowns-and-forms-never-seen)

---

**Scope.** The libraries examined: an original library of 192 symbols compiled in 1996, and
libraries compiled by the DOS library compiler (version 4.83) from known sources — among them a
test library holding circles and arcs in every line type, every pin form and the text forms. Each
compiled library was compared with its source, and the original with `LIBDECMP`'s output.

**Conventions.** Little-endian, unaligned values. `INT16` is signed, `WORD` unsigned. Coordinates
are Ulticap units (1 unit = 2 mil = 0.0508 mm), y up, from the symbol's bottom-left corner, as in a
`.SCH` file ([FILEFORMAT_ULTICAP_SCH.md](FILEFORMAT_ULTICAP_SCH.md)). Strings are zero-terminated, in the DOS
character set (code page 437 with Ulticap's own glyphs, FILEFORMAT_ULTICAP_SCH.md §10.8). Field names are
descriptions; the file stores none.

---

## 1. The file

A flat binary file in four parts, in this order: the header, the index (a fixed number of
slots), the symbol bodies, and two string blocks — one of attribute tags and one of values —
shared by all symbols. Each symbol body holds the records of a `.SCH` shape definition (`*S`) in
binary form, its texts as offsets into the string blocks.

[↑ Contents](#contents)

## 2. Header

| Offset⠀⠀ | Type | Value | Meaning |
|---|---|---|---|
| 0x00 | 13 bytes | `ULTImate LIB` and a 0 | signature |
| 0x0D | `WORD` | | unknown: 0x1A32 in the original library, 0x1A6A in every library compiled for this description |
| 0x0F | `BYTE` | 0 | |
| 0x10 | `WORD` | | index slots (§3) |
| 0x12 | `WORD` | 12 | the longest symbol name |
| 0x14 | `WORD` | 34 (0x22) | offset of the index |
| 0x16 | `WORD`, `WORD` | 4, 83 | unknown; the same in every library examined |
| 0x1A | `DWORD` | | offset of the tag block (§5) |
| 0x1E | `DWORD` | | offset of the value block (§5) |

The same compiler compiling the same source writes the same bytes: the file holds no date.

[↑ Contents](#contents)

## 3. Index

At 0x22, the slots, 22 bytes each:

| Offset | Type | Meaning |
|---|---|---|
| 0 | `WORD` | 1 for a used slot, 0 for a spare one |
| 2 | `DWORD` | file offset of the symbol's body |
| 6 | `WORD` | length of the body |
| 8 | `BYTE` | 0 |
| 9 | 13 bytes | the symbol's name, zero-padded |

The used slots are sorted by name; the bodies stand in the order of the source. The libraries
examined have two spare slots each.

[↑ Contents](#contents)

## 4. Symbol bodies

A body begins right after the last slot, the next one a byte after the previous one ends (that
byte is 0). Its length is exactly what its counts give:

| Part | Type | Meaning |
|---|---|---|
| counts | 6 × `WORD` | outline pairs, pins, circles, attributes, width, height |
| outline | outline pairs × 2 `INT16` | the outline stream of the `.SCH` shape, as pairs: a group starts with the pair (1, line style), then the group's points |
| pins | pins × 6 bytes | `INT16` x, `INT16` y, `BYTE` pin form, `BYTE` rotation |
| circles | circles × 13 bytes | `INT16` centre x, `INT16` centre y, `INT16` radius, `INT16` start, `INT16` sweep (1/64 degree; 23040 = a full circle; a start may be negative, −5760 drawing as 17280), `BYTE` colour, `BYTE` thick (0/1), `BYTE` line type (0–3) |
| attributes | attributes × 16 bytes | `INT16` x, `INT16` y, `INT16` point-of-effect x, `INT16` point-of-effect y, `WORD` size, `BYTE` colour, `BYTE` visibility, `WORD` tag, `WORD` value — tag and value as offsets into the string blocks |

The line style word of an outline group packs colour, width and line type as in a `.SCH` file. The
codes of the pin form, rotation and visibility are those of FILEFORMAT_ULTICAP_SCH.md §2. Attributes keep
their order.

[↑ Contents](#contents)

## 5. String blocks

The tag block holds every attribute tag once (`REFDES`, `VALUE`, `TXT`, `#`, …), the value block
every value once, each zero-terminated; an attribute names its tag and value by their offsets from
the start of their block. The value block runs to the end of the file.

[↑ Contents](#contents)

## 6. A symbol as `.SCH` text

Written out as text, a body is the shape definition it was compiled from:

```
*S<name>
<width> <height>
<the outline pairs, comma-separated>;
<pin form>,<rotation>,<x>,<y>,        one line per pin, the last ending in ';'
<circle fields, comma-separated>,     one line per circle, the last ending in ';'
<x> <y> <poe x> <poe y> <size> <colour> <visibility> <tag>=<value>
;
```

(an empty section is a lone `;`). Compiled from such a source, a library gives the source back
exactly. `LIBDECMP` cuts an attribute value at its first space; the library holds the whole
value.

[↑ Contents](#contents)

## 7. How KIUC converts a library

KIUC writes each symbol as the shape definition of §6 and reads it with the code that reads a DOS
design, so a library symbol becomes the same KiCad symbol as the same symbol in a converted DOS
design — the DOS characters, the DOS way of drawing arcs and the attribute rules included.

- **Names:** the symbol's name, with `\`, `/` and `:` replaced by `_`.
- **Non-homogeneous parts:** a device stored as gate types (`DEV/1`, `DEV/2`) becomes one KiCad
  symbol named after the device, with one unit per gate, as in a converted design. Where the library
  also holds a symbol of the device's own name (the whole package in one drawing), the joined symbol
  gets `_2`, and the log says so.
- **Value:** the symbol's `VALUE` (even a `?`); without one, its `DEVICE`, with the `UC_DEVICE` field
  then hidden. A joined non-homogeneous device has the device's name.
- **Labels:** a multi-unit symbol draws its `TXT` labels as plain texts of its units; a single-unit
  symbol keeps them as fields.
- **Field positions:** every field keeps its library position when the symbol is placed in KiCad.

A library that cannot be read is reported with the symbol where reading stopped. A file whose first
symbol does not fit the layout above is refused as possibly not a Ulticap library.

[↑ Contents](#contents)

## 8. Unknowns and forms never seen

- The header word at 0x0D and the words at 0x16 and 0x18: meaning unknown, not needed to read a
  library.
- Libraries written by other compiler versions: not examined.

[↑ Contents](#contents)
