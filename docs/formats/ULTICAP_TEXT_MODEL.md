# Ulticap Text Rendering Model

## Contents

[↑ README contents](../../README.md#file-formats)

- [Purpose](#purpose)
- [1. The problem](#1-the-problem)
- [2. The actual model: box first, then always-justify-one-edge](#2-the-actual-model-box-first-then-always-justify-one-edge)
- [3. The measured box metrics](#3-the-measured-box-metrics)
- [4. Overline positioning](#4-overline-positioning)
- [5. The font-independence principle](#5-the-font-independence-principle)
- [6. Known limitations](#6-known-limitations)
- [7. Resolved: a file-converting/exporting tool needs the same model, corrected for direct-anchor justify](#7-resolved-a-file-convertingexporting-tool-needs-the-same-model-corrected-for-direct-anchor-justify)
- [8. Ulticap for Windows (`.UTSCH`)](#8-ulticap-for-windows-utsch)

## Purpose

This document explains how Ulticap positions text (pin numbers, pin names,
labels, attributes) inside its schematic files. It's a record of the
underlying *model* only — how Ulticap itself decides where a character's
ink ends up, independent of how any particular tool chooses to render it.
For notes specific to KIUC's own rendering implementation (font choice,
what approaches were tried and rejected, performance considerations), see
`ULTICAP_TEXT_MODEL_IMPLEMENTATION.md`.

Keeping these separate matters: the model below is a fact about the file
format and should stay stable across rewrites of the renderer. The
implementation notes describe one particular renderer's choices, and are
expected to change over time.

[↑ Contents](#contents)

---

## 1. The problem

Early attempts assumed Ulticap positions text the way most modern
toolkits do — anchor a point, then left/center/right-justify the text
against it. That assumption is wrong, and every symptom traced back to it
(text sitting flush against the wrong edge, inconsistent behavior between
short and long strings, alignment codes that seemed to have no visible
effect for some strings but a large one for others).

[↑ Contents](#contents)

## 2. The actual model: box first, then always-justify-one-edge

Ulticap does not align *text* to an anchor point. It positions a
**virtual bounding box** relative to the anchor, using the halign/valign
codes stored in the file (left/center/right horizontally,
bottom/center/top vertically) — and then always draws the characters
justified toward **one fixed edge of that box**, regardless of which
alignment was requested:

- Horizontally: content is always left-justified inside the box.
- Vertically: content is always bottom-justified (baseline-referenced)
  inside the box.

The requested alignment only controls where the *box* sits relative to
the anchor point; it has no effect on how content sits inside the box.
For left/bottom-aligned text this is invisible, since a left/bottom-
justified box already puts content where you'd expect. For center- and
right/top-aligned text, it produces a real, visible displacement:
content is always pulled toward the box's left/bottom edge, away from a
right/top anchor and off of true center.

This was confirmed empirically against real Ulticap files, comparing
multiple elements on the same symbol that used different anchor
coordinates and alignment codes in ways that only make sense under this
model — including one case where an anchor sat exactly at the geometric
midpoint of what it was attached to, which only renders correctly if the
box is centered on that midpoint and the content is then left-justified
inside it, not centered on it directly.

The same box-then-justify structure was independently confirmed on the
vertical axis: valign behaves exactly like halign, just using the
top/bottom margins instead of left/right, with the same "always toward
one edge" rule.

[↑ Contents](#contents)

## 3. The measured box metrics

The box's dimensions were measured directly from Ulticap at its maximum
zoom level (the DOS program runs in 640×480 graphics mode, so this is the
practical resolution ceiling). All values below are in Ulticap's native
coordinate unit (1/500 inch), measured at a reference text size of 35:

| Quantity | Value (at size_u = 35) |
|---|---|
| Reference size (`size_u` at which all values here were measured) | 35 |
| Left margin (box edge → first character's ink) | 3 units |
| Right margin (last character's ink → box edge) | 18 units |
| Top margin (box top → cap height) | 8 units |
| Bottom margin (box bottom → baseline) | 3 units |
| Descender allowance (extends below box bottom) | 6 units |
| Character advance width (monospaced) | 20 units |
| Character advance width, **'7' only** | 23 units |
| Overline decoration → text gap | 3 units |

Ulticap's native coordinate unit is 1/500 inch — i.e. 1 unit = 0.0508 mm.

Two properties make this a clean, closed-form model rather than something
needing per-string estimation:

- **Margins are a fixed overhead, independent of string length.** They
  don't scale with the number of characters — a 1-character and a
  12-character string both carry the same 3-unit left margin and 18-unit
  right margin.
- **All values scale linearly with `size_u`**, proportionally to the
  reference measurement at `size_u = 35`:

```
value_at_size = value_at_reference × (size_u / 35)
```

This gives an exact box-width formula:

```
box_width = left_margin + Σ(per-character advance) + right_margin
```

Verified against Ulticap's own reference renderings — a single-character
string's measured box width matches the formula's prediction exactly
(3 + 20 + 18 = 41 units); multi-character strings were also checked, at
somewhat lower confidence due to the 640×480 zoom ceiling limiting
measurement precision, but consistent with the same formula.

The right margin alone is functionally a fixed "post-pad" after the last
character — not a per-character letter-spacing effect. An earlier attempt
to model Ulticap's wide spacing as inflated *inter*-character spacing
(a percentage-based letter-spacing applied uniformly per character) was
tried and rejected: it scales the wrong way, since a fixed margin doesn't
grow with string length but artificial per-character spacing does — fine
for a short string, but it visibly "smears" a long label far wider than
it should be, and distorts overline decorations (which then extend into
the trailing padding rather than stopping at the last character's ink).
The fixed-margin model above avoids both problems.

[↑ Contents](#contents)

## 4. Overline positioning

Ulticap draws an overline decoration exactly **3 units above the text**
(scaling with `size_u` the same way the other margins do) — a fixed,
measured gap between the top of the text and the decoration, independent
of which characters are overlined or how many. Like the box margins in
§3, this is Ulticap's own convention and does not depend on which font is
used to draw the glyphs.

[↑ Contents](#contents)

## 5. The font-independence principle

**Nothing in this model depends on which font is used to draw the
glyphs.** Box width, box placement, and per-character advance are all
computed from the fixed formula above, using Ulticap's own measured
numbers — never from whatever font metrics a particular renderer's chosen
font happens to report.

This is easy to violate by accident, and worth stating as a hard rule
rather than a preference: any time a renderer's *positioning* math pulls
in a value from the *rendering font's* own metrics (its glyph widths, its
cap-height, its line-height), that positioning becomes font-dependent in
a way that doesn't match Ulticap's fixed convention — which uses the same
margins and advances no matter what font a particular viewer happens to
have installed. Font metrics are legitimate for one purpose only:
figuring out how to *draw* a glyph inside a slot the model already
computed, never for deciding *where* a slot is or how wide a run of
content is.

[↑ Contents](#contents)

## 6. Known limitations

- **A small residual offset can exist for some text even in real
  Ulticap**, checked directly against Ulticap at maximum zoom on real
  files: certain elements are not perfectly aligned by the model's own
  formula, even in the original software. This is not a defect in the
  model above — it's a genuine characteristic of the original renderer,
  and should be left uncorrected rather than "fixed." An attempt to
  eliminate such an offset was tried and reverted: since Ulticap's own
  rendering has the same small offset, trying to eliminate it just
  introduces a new, incorrect shift relative to the authentic appearance.
- **Vertical margins were, for a period, unverified for the "always one
  edge" behavior** independent of the horizontal case. This has since
  been confirmed directly in Ulticap: valign follows the identical
  box-then-justify rule as halign, using the top/bottom margins.
- Overline decoration position and horizontal run extent must be derived
  consistently with whatever the current text-positioning approach is;
  historically this has been a source of subtle bugs when the two drift
  out of sync (see the implementation notes for specifics).

[↑ Contents](#contents)

## 7. Resolved: a file-converting/exporting tool needs the same model, corrected for direct-anchor justify

Any tool that translates Ulticap schematics into another CAD tool's own file format faces a
structural question: does that target format have an equivalent "box positioned, then content
always-justified-one-way" model, or does it justify text directly against an anchor (the
assumption this document opens by rejecting for Ulticap itself)? KiCad uses the latter — direct-
anchor justify — so naively writing Ulticap's raw anchor coordinates into KiCad's own justify
mechanism reproduces correct positions only for left/bottom-justified content; center- and
right/top-justified content is measurably displaced, by roughly the margin amounts in §3
(scaled by size).

This is now corrected in KIUC's writer (`kiuc_writer.py`) rather than left as a rendering-only
concern. `uc_anchor_shift_u`/`uc_anchor_shift_v` (`kiuc_model.py`) compute a per-justification,
per-size anchor shift — in Ulticap units, using only the fixed §3 margins, no font metrics — that
is applied to the anchor coordinate *before* handing it to KiCad's own (direct-anchor) justify
keywords. This reproduces the same position Ulticap's virtual-box model would have placed the
content at, without needing KiCad's format to natively support a box-then-justify model at all.

The shift is applied at every one of the writer's text-anchor call sites — REFDES/VALUE, the `UC_`
fields (every attribute without a KiCad equivalent, `DEVICE` included), the name texts written as
plain text (`LABELn`, a symbol-level `UT_NETNAME`, a `LABEL`/`WIRELABEL` away from the pins),
standalone LABEL/WIRELABEL net labels, power-symbol VALUE, and sheet-box names, file names and
properties. Component-attached sites (all of these except the standalone labels) share one helper
(`_uc_text_anchor_shift`), since their hjust/vjust always come from
`ulticap_translate`, which pre-swaps them for a 180-degree rotation before the shift is ever
computed — meaning the shift itself only ever needs a 0-or-90-degree rotation. Standalone `*A
LABEL=` annotations (`_annot_label_sexp`) compute their own hjust/vjust directly from the raw
vis-byte decode, with no such pre-swap, so they apply `uc_anchor_shift_u`/`uc_anchor_shift_v`
inline with the full 0/90/180/270 rotation instead of reusing the shared helper's shortcut — see
`ULTICAP_TEXT_MODEL_IMPLEMENTATION.md` and `NAMED_STUB.md` for the detail. Either way, the anchor
shift itself can't be missed at an individual call site, since both paths trace back to the same
two underlying `uc_anchor_shift_u`/`uc_anchor_shift_v` primitives.

One text is deliberately left out: the net label KIUC adds for a pin's own net name. A KiCad net
label is anchored at its connection point, so it is placed at the pin's connection point and
turned away from the symbol, not at the stored text position; there is no anchor to shift.

It has been verified against a direct simulation of the viewer's box model across all 144
rotation × alignment combinations, with zero mismatches -- for the component-attached call sites.
A label pointing left (angle 180) has since been checked against KiCad's own rendering (see
"KiCad's vertical justify, measured" below): it lands where the model says, apart from KiCad's
own small offsets.

The two programs mean different things by a 180-degree rotation. In Ulticap, rotating an attribute
text (a net name, a symbol's text) by 180 degrees around the cursor changes its alignment instead
of turning it upside down: bottom-left becomes top-right, so the file stores a horizontal or a
vertical text with an alignment. In KiCad a label rotated by 180 degrees stays readable: it is
mirrored around its connection point, which stays the reference (bottom-left becomes
bottom-right). An angle of 180 on a label in KIUC's output is thus KIUC's own choice of the
label's direction, never a value read from the file.

Free texts (`*X`) in Ulticap for DOS are the exception: they store a rotation of 0, 90, 180 or 270
degrees, and DOS draws 180 and 270 upside down, turned around the anchor, with the alignment taken
in the text's own rotated frame. KiCad never draws schematic text upside down, so KIUC writes such
a text at 0 or 90 degrees with its alignment mirrored to the opposite corner (bottom-left becomes
top-right, centre-left becomes centre-right, centre stays centre): it then covers the same area
and reads normally. The viewer draws it upside down, as DOS does. V5.72's import keeps the
alignment but stores the text as horizontal or vertical, so in a `.UTSCH` file it sits on the
other side of its anchor.

**KiCad's vertical justify, measured.** The vertical shift (`uc_anchor_shift_v`) assumes KiCad's
vertical justify keywords are self-referential the way Ulticap's box model is (`bottom` anchors
the baseline, `top` the cap height, `center` the midpoint). Measured in KiCad 9 (an SVG export of
one letter per case, four sizes, every justify) KiCad instead aligns a box 1.17 × the text size
tall, from the cap height down to 0.17 × size below the baseline (a fixed allowance, not the real
descent: a descender reaches 0.33 × size below the baseline), and then lifts it:

| Item | `bottom`: baseline above the anchor | `top`: cap height above the anchor |
|---|---|---|
| Symbol field | 0.11 × size | −0.03 × size (just below) |
| Free text | 0.26 mm + 0.12 × size | 0.26 mm − 0.05 × size |
| Net label | 0.40 × size | 0.23 × size |

(`center` lies halfway in every case; the field row was measured in KiCad 7, which renders texts
and labels identically to KiCad 9.) So the assumption holds for symbol fields to within about
0.1 mm. Free texts, which are written at their stored anchor without Ulticap's margin shift, and
net labels, which must stay on their point of effect, land 0.1–0.6 mm away from Ulticap's
position at the default size (most for labels anchored top-right, where Ulticap's margins are
largest). These differences are accepted: too small to matter, and moving a label along its wire
could change connectivity near a crossing. See the implementation notes for the history of the
earlier, reverted attempt at the vertical shift.

[↑ Contents](#contents)

## 8. Ulticap for Windows (`.UTSCH`)

Everything above was measured on Ulticap for DOS, which draws every text in one built-in
monospaced font. Ulticap V5.71/V5.72 for Windows draws text with the installed Windows fonts
instead — by default Arial, regular, 8 points — and reading a `.SCH` file into it turns every text
into Arial regular. The box metrics of §3 do not describe how Windows draws these texts.

A `.UTSCH` file stores each text's font: its face name, weight, italic flag and height. A `.SCH`
file read into V5.72 stores a height of 1.5 times the ASCII size (a test design with sizes 10 to 100
gives heights 15 to 150; the default size 35 gives 52). Heights can also be stored negative, the
Windows way of giving the character height rather than the cell height. KIUC turns any stored
height into an ASCII size the way V5.72's own ASCII export does, as the height's absolute value
times 2/3, rounded down; size 35 read in and converted thus comes back as 34. From there a
`.UTSCH` text takes exactly the same path as a `.SCH` text: the same box model, the same anchor
shift in the writer. The font face, bold and italic are not carried over; KiCad draws every text
in its own font.

[↑ Contents](#contents)
