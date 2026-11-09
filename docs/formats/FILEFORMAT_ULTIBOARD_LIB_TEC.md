# Ultiboard Library and Technology File Formats

Reverse-engineered description of Ultiboard's compiled shape libraries (`.LIB` for V4, `.L55`
for V5) and its technology files (`.TEC`), derived from the analysis of real files and comparison
with the DDF files the libraries were compiled from. The DDF format itself, to which this document
refers throughout, is described in [FILEFORMAT_ULTIBOARD_DDF.md](FILEFORMAT_ULTIBOARD_DDF.md).

## Contents

[↑ README contents](../../README.md#file-formats)

1. [Library container](#1-library-container)
2. [Technology files](#2-technology-files)
3. [Converting technology between V4 and V5 units](#3-converting-technology-between-v4-and-v5-units)
4. [How KIUB converts a library](#4-how-kiub-converts-a-library)
5. [Unknowns](#5-unknowns)

---

**Scope.** The files examined: V4.91 libraries and V5.72 libraries, each compared with the DDF
files they were compiled from, and single-file and multi-file `.TEC` sets in both V4 and V5
units.

**Conventions.** Little-endian, unaligned values. `WORD` is 16 bits unsigned, `DWORD` 32 bits.
Library coordinates and sizes are in the units of the DDF of the same version: 1/1200 inch for V4,
nanometres for V5 ([FILEFORMAT_ULTIBOARD_DDF.md](FILEFORMAT_ULTIBOARD_DDF.md), Section 10.1).

## 1. Library container

A compiled Ultiboard shape library is a binary container of shape definitions.

| Offset | Size | Contents |
|---|---|---|
| `0x00` | 13 | Signature `ULTImate-LIB` followed by a zero byte |
| `0x10` | `WORD` | Number of index slots |
| `0x12` | `WORD` | 12, the longest shape name the library holds |
| `0x14` | 22 × slots | Index |

Each 22-byte index slot:

| Offset in slot | Size | Contents |
|---|---|---|
| `0x00` | `WORD` | Nonzero when the slot is in use |
| `0x02` | `DWORD` | Offset of the shape definition in the file |
| `0x06` | `WORD` | Length of the shape definition |
| `0x08` | 1 | 0 |
| `0x09` | 13 | Shape name, zero-terminated (at most 12 characters) |

A shape definition is exactly the text of a DDF `*S` record
([FILEFORMAT_ULTIBOARD_DDF.md](FILEFORMAT_ULTIBOARD_DDF.md), Section 3) without its `*S<name>` line: the two text
descriptor lines, the thermal value and the outline, pad and arc sections, with CRLF line ends,
followed by a zero byte. A longer shape name is shortened to 12 characters when the library is
compiled.

The library stores no version and no technology: its pad lines carry pad and drill **codes**
only (FILEFORMAT_ULTIBOARD_DDF.md, Section 3.2), whose sizes live in the technology of the design the
library was made with. The units follow from the size of the values.

A slot may hold something that is not a shape definition — a damaged library was seen holding a
component record and fragments in its slots — so a reader has to check each definition before
using it.

Ulticap's DOS symbol libraries use the same `.LIB` extension with the signature `ULTImate LIB` (a
space instead of the hyphen); they hold schematic symbols, not shapes, and are converted by
KIUC, KIUT's schematic converter (see [FILEFORMAT_ULTICAP_LIB.md](FILEFORMAT_ULTICAP_LIB.md) and
[SCHEMATICS.md](../SCHEMATICS.md)).

[↑ Contents](#contents)

## 2. Technology files

Ultiboard keeps a design's technology in `.TEC` text files, in one of two layouts. Their `*V`
version line does not tell the layouts apart (versions even differ within one multi-file set);
the records present do.

**Single file** (V4.60 and later) — every table in one file:

| Record | DDF counterpart | Contents |
|---|---|---|
| `*P0`, `*P1`, `*P2` | `*T0`, `*T1`, `*T2` | Inner, top and bottom pad codes, same fields as in the DDF (FILEFORMAT_ULTIBOARD_DDF.md, Section 2) |
| `*D` | `*TD` | Drill codes; all 256 codes are listed, an unused one as 0 |
| `*T` | `*TT` | Trace codes (32) |
| `*C` | first `*TC` field | Drill tolerance |
| `*G` | — | V5 only; not needed for shapes |

**Multi-file set** (older versions) — one file per table:

- three pad files, each holding one pad table as `*P` records without a layer digit; which layer
  a pad file serves is decided only by how it is used, so one file may serve several layers;
- a drill file with the `*D` records (and `*C`);
- a trace file with the `*T` records.

A table may list fewer codes than its full range (256 pad and drill codes); a code it does not
list is undefined. The board clearance (the second `*TC` field) is a design setting that no
`.TEC` file holds.

[↑ Contents](#contents)

## 3. Converting technology between V4 and V5 units

When Ultiboard V5 converts V4 technology to nanometres (1 V4 unit = 25,400,000 / 1200 nm), it
rounds as follows (reproduced exactly on every V5 table derived from a V4 one that was compared):

| Value | Rounding |
|---|---|
| Pad `<x1>`, `<x2>`, `<clearance>`; drill diameters; trace width and clearance; drill tolerance | To nearest |
| Pad `<y>` | Down when `<y>` = `<x1>` + `<x2>`, otherwise to nearest |
| Pad `<radius>` | Half the converted `<y>` (rounded down) when 2`<radius>` = `<y>`, otherwise to nearest |

KIUB converts V5 technology to V4 units by rounding to the nearest unit, keeping the same two
relations, but never rounds a nonzero value to 0: a drill below the SMD threshold marks pads
without a hole (FILEFORMAT_ULTIBOARD_DDF.md, Section 10.4) and must keep doing so.

[↑ Contents](#contents)

## 4. How KIUB converts a library

KIUB reads the library's slots, checks every shape definition and leaves out (and names in the
log) a slot that does not hold one. It reads the technology from one single-file `.TEC` or a
multi-file set, checking each file for the role it was given, and converts it to the library's
units when they differ. Shapes and technology together then form the text of a DDF holding only
shapes: a header for the library's version, the technology as `*T` records (codes whose values
are all 0 left out, as Ultiboard leaves them out of a DDF) and the shapes in library order. That
DDF is converted as a library-only input: the library board and the footprint library, no design
board (see [BOARDS.md](../BOARDS.md), "What a conversion writes").

[↑ Contents](#contents)

## 5. Unknowns

- Header bytes `0x0D`–`0x0E` (a `WORD` that differs from file to file) and `0x0F` (0) are not
  used by KIUB; their meaning is unknown.
- `*G` in V5 `.TEC` files is not needed for shapes; its meaning is unknown.

[↑ Contents](#contents)
