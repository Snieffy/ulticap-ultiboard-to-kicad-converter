# Ulticap Text Rendering — Implementation Notes

This document covers implementation choices specific to KIUC's own
renderer(s) — font selection, rendering approach, and dead ends. Unlike
`ULTICAP_TEXT_MODEL.md`, everything here is expected to change as the
implementation evolves; none of it is a fact about the Ulticap file
format itself.

## Contents

[↑ README contents](../../README.md#file-formats)

- [Font selection](#font-selection)
- [Rendering approach: whole-string natural rendering](#rendering-approach-whole-string-natural-rendering)
- [What was tried and rejected](#what-was-tried-and-rejected)
- [Writer implementation: anchor-shift for direct-anchor justify targets](#writer-implementation-anchor-shift-for-direct-anchor-justify-targets)
- [Text sizes](#text-sizes)
- [Performance notes](#performance-notes)

## Font selection

**ISOCTEUR** was chosen as the rendering font — an ISO 3098 / DIN 6776
engineering-lettering font, structurally similar in style to Ulticap's
own DOS-era font (both are "technical drawing" style monospaced fonts
with generous natural spacing, distinct from ordinary programming
monospace fonts like Consolas or DejaVu Sans Mono, which were tried first
and don't match).

ISOCTEUR is used **only to draw the glyphs** — per the font-independence
principle in the model document, it must never feed into position
calculations. If ISOCTEUR isn't installed on a machine opening the
output (or running a live viewer), the renderer silently substitutes a
fallback font; visual appearance may differ, but positioning is
unaffected as long as the font-independence principle is actually
respected in the code.

[↑ Contents](#contents)

## Rendering approach: whole-string natural rendering

Current approach: draw each string as a single unit, letting the font
handle its own natural inter-character spacing, rather than positioning
each character individually at a fixed cursor offset.

- **Left-aligned text** needs only a start position — computed purely
  from the model (anchor + left_margin, scaled), independent of the
  string's rendered width. No font-metric dependency.
- **Right/center-aligned text** needs to know the string's total
  rendered width in order to compute where the box (and hence the start
  position) sits. Using the model's fixed per-character advance sum for
  this — rather than the font's actual rendered width — causes the text's
  far edge to drift from where the model says it should land, by an
  amount that scales with the mismatch between the font's natural
  character width and the model's fixed advance, and therefore grows
  with text size. The fix: keep `left_margin`/`right_margin` exactly as
  the model defines them (genuine fixed, font-independent offsets — no
  reason to touch them), and replace only the model's `content_width`
  term with the actual measured width of the text as it will really be
  rendered (e.g. `QFontMetrics`/`QFontMetricsF` on the real run(s),
  summed). Box width for anchor math becomes
  `left_margin + measured_content_width + right_margin`, used identically
  to before for `box_left` under all three `halign` cases. This is a
  narrow, deliberate exception to "never use font metrics for
  positioning": it's used only to find out how wide something we're
  *already committed to rendering at natural width* actually turned out
  to be, not to decide where any margin or anchor lives.
  - **Qt path**: the draw loop already computes per-run widths via
    `fm.horizontalAdvance()`. Measure them *before* computing `box_left`
    rather than after, and reuse the same numbers for the actual draw
    loop — no duplicate work. Because the same engine that measures also
    renders, this can be made *exact* for the live viewer: no gap between
    what was measured and what gets drawn.
  - **SVG path**: no measurement currently happens there at all; add a
    local (lazily-imported, so the module still works without PySide6 for
    headless/CLI use) `QFontMetricsF` measurement purely for this anchor
    calculation. The actual drawn characters still flow naturally via
    sibling `<tspan>`s as before; only the anchor math gets the real
    measurement.
  - **Validated (2026-07-09)**: implemented and tested — right/center
    alignment no longer drifts with scale in either the Qt viewer or
    exported SVG. SVG in particular renders correctly in practice, despite
    the theoretical caveat below.
  - **Remaining caveat for SVG, worth understanding even though it hasn't
    caused a problem in practice**: this measurement happens in Python via
    Qt's font engine at export time, but the SVG is *rendered* later by
    whatever opens it (a browser, Inkscape, etc.) — a different rendering
    engine. This is a deeper issue than "the right font might not be
    installed": even when the same font (ISOCTEUR) is genuinely present in
    both places, different font engines can have different hinting/
    subpixel/rounding behavior and aren't guaranteed to report identical
    advance widths for nominally the same font at the same size. So SVG
    right/center positioning is a *close approximation*, not a
    from-first-principles exact match the way the Qt viewer's is — bounded
    and normally small, but structurally different from the Qt path's
    guarantee. This is still unconditionally better than the old
    model-based width, which drifted by an amount that grew with scale
    regardless of font availability; it just means "font-independent" is
    no longer quite the right way to describe SVG's positioning the way
    §4 of the model document originally implied for renderers generally —
    the live Qt viewer is font-independent in that stronger sense, exported
    SVG is font-*insensitive-in-practice* rather than exact.
- Overline run boundaries (which substring is overlined) are found by
  measuring the rendered width of the text before the overlined run,
  using the same font-metric approach — not by summing fixed
  per-character advances.

[↑ Contents](#contents)

## What was tried and rejected

- **Per-character fixed-position cursor stepping** (position each
  character at `previous_x + model_advance`, draw at the font's natural
  width) — this is effectively the original, naive implementation. It
  causes visible overlap whenever a glyph's natural rendered width
  exceeds the fixed advance slot it's stepped by (e.g. a wide capital
  letter in some fonts), since nothing reconciles the two.
- **Per-character font stretching** (`QFont.setStretch()` in Qt,
  `textLength`/`lengthAdjust="spacingAndGlyphs"` in SVG, one stretch
  factor computed per character to force its rendered width to exactly
  match the model's fixed advance) — this does prevent overlap, and was
  verified empirically to do so correctly across a wide range of scale
  factors when tested statically. However, it introduces its own
  fragility: it was later found to break down at certain zoom levels in
  live use (character spacing collapsing toward zero above a certain
  zoom threshold), and adds real per-character computational cost (a
  font-metrics query and a stretch-percentage calculation per character,
  every repaint). Replaced by whole-string natural rendering.
- **Qt/SVG native alignment primitives** (`AlignRight`, `AlignHCenter`,
  SVG `text-anchor`) — wrong model entirely; Ulticap doesn't justify text
  against the anchor at all, it justifies a box (see the model document).
- **Percentage-based artificial letter-spacing** (inflating inter-
  character gaps to mimic Ulticap's wide font) — works passably for short
  strings but smears long text and breaks overline decorations; see the
  model document §3 for why the fixed-margin model was used instead.
- **Font-metric-derived box sizing for margins/advances** (using
  whichever font's own `horizontalAdvance()` to size the box itself,
  rather than the fixed Ulticap values) — makes the result depend on
  which font happens to be installed, rather than matching Ulticap's own
  fixed convention. This is different from the narrow, deliberate
  exception described above for right/center total-width measurement:
  this rejected approach would have used font metrics for the *margins*
  themselves, which must stay fixed regardless of rendering font.
- **Bearing compensation for a rendering font's own glyph metrics** —
  attempted to correct a small residual offset noted in the model
  document's limitations section, made things worse since that offset is
  authentic to Ulticap itself, not a rendering bug.
- **First attempt at reusing this model, unmodified, inside a file-exporting/converting
  tool** to translate Ulticap anchors into a target format's own
  direct-anchor justify semantics — attempted once, caused a real
  positioning regression in exported output (properties shifting
  significantly from their expected position), and was fully reverted.
  Root cause was never conclusively isolated before the revert. A
  corrected second attempt, informed by that failure, later succeeded and
  shipped — see "Writer implementation" below.
- **A related, separately-scoped feature**: forcing pin numbers and net
  labels to sit a fixed small distance from their electrical connection
  point, regardless of the anchor the file specifies — attempted several
  times, abandoned. This is unrelated to text positioning *within* a box
  (which this document and the model document both cover), and more
  about whether a tool should ever override a file's own authored anchor
  position at all. It kept conflicting with real files where the
  file-specified distance is itself meaningful/authentic (e.g. a symbol
  deliberately demonstrating several different alignment codes), not an
  arbitrary gap to be corrected. If revisited, treat it as an
  unconditional, uniform, tightly-bounded nudge (same fixed shift applied
  to every case, clamped to a small maximum) rather than any form of
  "snap to a target distance," which collapses meaningful variation
  between different real cases.

[↑ Contents](#contents)

## Writer implementation: anchor-shift for direct-anchor justify targets

KiCad justifies text directly against an anchor (no virtual-box model of
its own — see the model document §7). The writer (`kiuc_writer.py`)
corrects for this by shifting the anchor coordinate itself before handing
it to KiCad's justify keywords, rather than trying to give KiCad a box
model it doesn't have.

`uc_anchor_shift_u`/`uc_anchor_shift_v` (`kiuc_model.py`) compute this
shift from the fixed §3 margins only — no font metrics, and (for
horizontal 'center') no dependence on the actual text content, since
content width cancels out of the box-model formula. One shared helper
(`_uc_text_anchor_shift`) combines this with rotation handling for every
component-attached text-anchor site — REFDES/VALUE, the `UC_` fields
(`DEVICE` among them), the name texts written as plain text, power-symbol
VALUE, and sheet-box names, file names and properties — since their hjust/vjust always arrive already pre-swapped
for a 180-degree rotation via `ulticap_translate`, so the shared helper
only ever needs a 0-or-90-degree local rotation (the writer's
viewer-matching readability convention — `% 180` — already folds
180-degree-different placements into a semantic left/right or top/bottom
swap rather than a literal upside-down rotation, before the shift ever
sees them).

Standalone `*A LABEL=` net labels (`_annot_label_sexp`) apply the same
two primitives but not through the shared helper, and not with its
mod-180 shortcut: these labels compute their own hjust/vjust directly
from the raw vis-byte decode (rotation fixed at 0), then possibly flip
them again via their own POE-projection/crossing-safety logic (see
`NAMED_STUB.md`) — entirely independent of the label's final angle, with
no `ulticap_translate` pre-swap involved anywhere in the chain. So a
genuinely 180-degree-rotated standalone label's local shift needs the
FULL rotation (0, 90, 180, or 270) applied directly, not the mod-180
reduction that's only valid when the caller has already pre-swapped for
180 upstream. Reasoned through and internally consistent (the sign works
out correctly once the coordinate transform's own Y-flip is accounted
for), but not yet checked against a live Ulticap rendering of a
180-degree-rotated standalone label the way the component-attached sites
were — worth a visual spot-check when one is convenient to compare.

Verified against a direct simulation of the viewer's box model across all
144 rotation × alignment combinations, with zero mismatches — for the
shared helper (`_uc_text_anchor_shift`), i.e. the component-attached
sites. Notably, the old (combine-then-rotate) structure — the shape the
first, reverted attempt above took — mismatched on 54 of 72 `is_v=False`
cases alone, every rotation except 0 and 1; this is exactly why testing
only at those two rotations never revealed the problem the first time
round.

The vertical shift assumes that KiCad's vertical justify keywords are
self-referential the way Ulticap's box model is (`bottom` = KiCad's own
rendered baseline, `top` = its own rendered cap-height top, `center` = the
midpoint between them). Measured since in KiCad 9 (ULTICAP_TEXT_MODEL.md §7,
"KiCad's vertical justify, measured"): KiCad aligns a box 1.17 × size tall
(cap height to 0.17 × size below the baseline) and lifts it. For symbol
fields, the texts this shift is applied to, the assumption holds to within
about 0.1 mm; free texts and net labels, which get no shift, land
0.1–0.6 mm from Ulticap's position. No correction is applied: the
differences are too small to matter.

The net label written for a pin's own net name is the one text that
skips the shift on purpose: it is placed at the pin's connection point,
where a KiCad label attaches, turned away from the symbol (angle 0, 90,
180 or 270 from the direction of the stored text), justified left or
right and bottom.

Ulticap never stores a 180-degree text: rotating a text by 180 degrees
in Ulticap changes its alignment (bottom-left becomes top-right), while
KiCad keeps a 180-degree label readable by mirroring it around its
connection point (bottom-left becomes bottom-right). So every angle 180
in the output is the writer's own choice of direction (see the model
document §7).

[↑ Contents](#contents)

## Text sizes

The writer turns an Ulticap size into a KiCad font height as
`size × 0.0508 mm × multiplier`, with a multiplier per kind of text:
`REF_HEIGHT_TUNE` (1.0) for references, `VALUE_HEIGHT_TUNE` (1.0) for
values, `FIELD_HEIGHT_TUNE` (1.0) for the other fields, a symbol's label
text and a sheet symbol's name and file, `TEXT_HEIGHT_TUNE` (1.0) for
free texts, `LABEL_HEIGHT_TUNE` (0.8) for net labels, bus labels, port
labels and a pin's wire label, `PIN_NUM_HEIGHT_TUNE` (0.5) for pin
numbers, `PIN_NAME_HEIGHT_TUNE` (0.5) for pin names and for plain text
drawn in a symbol. Ulticap's character cell measures 1.137 × size in
KiCad's terms (an exact match at 1.137). A text KIUT adds (a bus label)
or whose size the file doesn't give (a sheet symbol's file name) takes
Ulticap's default size, 35, at its multiplier. They are user settings (the schematic's fine tuning: *Advanced…* in the KIUT window,
`docs/SETTINGS.md`). A `.UTSCH`
text's stored font height is first turned into an Ulticap size (model
document §8) and then goes through the same formula; its font face, bold
and italic are not used.

[↑ Contents](#contents)

## Performance notes

Whole-string rendering is cheaper than per-character stretching: one
font-metrics call per string (for right/center-aligned cases only) plus
the normal per-string draw call, rather than a stretch-percentage
computation and font mutation per character. Overline decoration adds one
extra line-draw call per contiguous overlined run (not per character) —
negligible even for schematics with many overlined signals.

[↑ Contents](#contents)
