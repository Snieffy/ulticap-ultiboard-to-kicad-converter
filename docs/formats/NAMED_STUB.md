**Named-Stub Power Symbol Pattern The Ulticap pattern.**

Ulticap allows a passive single-pin symbol to act as a placeholder for a per-instance net connection.

Example records: `*S` referenced by `*C`
```
*SGND_LINK    
100 100
1,5,24,24,50,50,24,74,24,24,1,5,
24,50,0,50;
0,0,50,50;
;
100 50 100 50 35 4 130 #=1
125 50 100 50 35 11 3 PINTYPE=PAS
50 -75 50 50 35 12 4 PKG_TYPE=LINK
0 50 100 50 35 14 133 WIRELABEL=?
50 125 50 50 35 14 132 REFDES=LNK?
50 -25 50 50 35 14 4 VALUE=LINK
50 -125 50 50 35 14 4 DEVICE=GND_LINK
;

*C -2000 -400 4 GND_LINK
-25 50 100 50 35 14 133 WIRELABEL=CGND
200 75 50 50 35 14 132 REFDES=LNK1
;

*C -2000 -550 4 GND_LINK
-25 50 100 50 35 14 133 WIRELABEL=GND
200 75 50 50 35 14 132 REFDES=LNK2
;
```
The `*S` symbol definition declares a single pin with a generic `PINTYPE` (e.g. `PAS` ) and a `WIRELABEL` attribute whose default value is the literal placeholder `?` .\
Each placed instance ( `*C` ) then overrides that `WIRELABEL` with the actual net name it connects to — for example, two instances of `GND_LINK` might carry `WIRELABEL=CGND` and `WIRELABEL=GND` respectively.

In Ulticap's model, a visible `WIRELABEL` and an invisible `SIGNAL` declaration are treated identically:\
both bind the pin to a net purely by name, with no requirement for a continuous drawn wire back to the rest of that net.
This lets a real, placeable component (complete with reference designator, footprint, and BOM line — `GND_LINK` typically represents a physical 0-ohm link or solder jumper) sit at one schematic location while still being a full member of a net that's defined and drawn elsewhere across the design. 

**The KiCad gap.** KiCad has no equivalent of "a visible pin name implies net membership."\
A visible pin only joins a net through an actual drawn wire reaching it.
The one mechanism KiCad does provide for name-based, wire-independent net membership is the _invisible_ power-input pin:\
when a pin's electrical type is `power_in` and its visibility is hidden, KiCad automatically merges it into a global net carrying the same name as the pin — but, critically, this auto-merge applies _only_ to hidden pins.\
A visible `power_in` pin behaves like any other ordinary pin and must be wired in to participate in a net.

**How the converter mimics it.**\
For a symbol matching this pattern (single pin, `*S WIRELABEL` default of `?` ), the converter renders the pin as `power_in` , hidden, at its true `*S` - derived location — on-grid, not relocated, since KiCad permits drawing a real wire directly to a hidden pin at its exact position.\
This gets both properties Ulticap's model relies on at once:\
the pin still accepts a genuine wire connection exactly where Ulticap placed it, and because the pin is hidden, KiCad's name-based auto-merge folds it into the broader net (CGND, GND, or whatever the `*C WIRELABEL` override specifies)
without needing that net to be physically wired all the way back.\
The component itself — reference designator, footprint, value — is left completely untouched by this and continues through the normal, non-power placement path, so it still appears in the BOM and can still be placed on the PCB like any other part.

**The text label, and why it needs special treatment.**\
Because the pin is now hidden, its name — the actual string that drives the net-merge — is no longer visible anywhere on the symbol.
Without some visible cue, there's nothing on the schematic telling a viewer what net this pin actually belongs to.\
To address that, the converter adds a separate, ordinary graphic text element to the symbol body, positioned independently of the pin (using the `*C WIRELABEL` attribute's own placement and sizing, not the pin's location), displaying the net name.\
This text is purely cosmetic: it is not the pin name, has no electrical effect, and KiCad has no internal link between a graphic text string and a pin's name field.\
**If someone edits this text expecting to rename the net, nothing will actually change — the real, hidden pin name (and therefore the net it belongs to) stays exactly as it was.**\
To make that limitation hard to miss, the text is rendered in red and wrapped in asterisks ( `*CGND*` )\
A deliberate visual flag that this string is a representation of something hidden elsewhere, not a live, editable net name in its own right. 

---

**Free-Standing Label Positioning (`*A LABEL=`, `_annot_label_sexp`) The Ulticap pattern.**

Every `*A LABEL=` annotation carries two coordinates, and Ulticap's DOS editor lets a user move them independently: the anchor ( `X` , `Y` ) is where the visible text sits, and the POE ( `X POE` , `Y POE` ) is the actual object-association point — the coordinate that determines which wire or bus the label names. A user can drag a label's text anywhere for readability (right next to other labels in a tidy column, tucked out of the way of a symbol, or simply far off to one side) while its POE stays exactly where it was originally placed, on the wire it truly belongs to. The two are frequently identical, but a real, sometimes large, gap between them is normal and not a sign of a misplaced or stale annotation.

**The KiCad gap.** A KiCad `(label ...)` has only one coordinate, and that coordinate is both where the text renders *and* how KiCad decides which net it belongs to (whichever wire passes exactly through that point). There is no way to keep Ulticap's visual position and its true electrical position as two separate things — the converter has to collapse them into one, and where it lands matters both for correctness (which net does KiCad actually think this is) and for legibility (does the text end up somewhere sensible).

**How the converter resolves it.** The guiding rule: the label's own anchor is almost always the better position to use, since it is exactly where the user placed the text and — being Ulticap's own visual intent — it already reads correctly there. The POE is only ever used as a fallback, and only when the anchor can't safely stand in for it. Four cases, checked in order:

- POE sits at a bus-entry junction, or free-floats with no wire or bus underneath it at all (the POE was never really attached to anything — Ulticap allows this, and there is nothing to project onto): use the anchor, alignment untouched. A free-floating POE is often found near a sheet edge, so falling back to it risks placing the label outside the sheet or overlapping a symbol; the anchor, wherever the user actually put it, is the only sensible choice.
- POE sits on a wire or bus: find every segment belonging to that same electrical network (tracing through shared endpoints and junctions, so a bent or T-shaped run is treated as one net, not several) and project the anchor onto the nearest point of it — preferring a segment whose orientation (horizontal/vertical) matches the label's own text orientation, so a horizontal label doesn't accidentally snap onto a vertical wire that happens to sit closer at an inner corner. If that projected point is close enough (within a tunable distance limit), use it: this keeps the label sitting at the anchor's own position along the wire, with the wire's own line providing the coordinate KiCad needs for connectivity, and only the axis *perpendicular* to the wire may need a small justify flip (see below).
- If the projected point is too far from the anchor (past the tunable limit — the anchor has been dragged somewhere with no nearby wire in its own net at all, or well beyond the end of its wire), fall back to POE directly; the alignment is adjusted as below.
- If the projected point would coincide with a *different*, unrelated network — two wires (or a wire and a bus) can cross at a point with no junction dot, meaning they are not actually connected there even though the point satisfies an on-segment test for both — using it would make KiCad treat the label as bridging two nets that were never meant to touch. Fall back to POE directly for position (always safe, being the label's true attachment point); the alignment is adjusted as below.

**Alignment adjustment.** Two independent axes, treated differently, because they fail in different ways:

*Perpendicular to the wire* (the axis the wire does *not* run along): may flip only when the label's own justify on that axis was already `center` and the offset clears a real, measured margin (the same per-size box-model constants used elsewhere in the writer, not a separate guess) — a small offset barely straddles the wire either way, not worth disturbing, and a label already justified to one side already reads correctly regardless of the offset's size. This applies wherever the label ends up on its wire or bus.

*Along the text's reading axis* (horizontal for horizontal text, vertical for vertical text): nothing changes as long as the label sits at its anchor's own position on that axis — the text then starts exactly where the user placed it, with its original alignment. Where the label's point has to lie elsewhere on that axis — the anchor lies beyond the end of its wire, so the projection stops at the wire's end, or the label falls back to its POE — the text is put on the side of that point Ulticap drew it on: from the anchor, the original alignment and the label's real measured width (the same per-character metrics used for text-box sizing elsewhere), KIUT works out where the middle of Ulticap's text lies; a middle to the left of the point makes the text right-justified, to the right left-justified (below and above for vertical text), and a middle within a quarter of the text's width of the point keeps the label centred if it was centred, otherwise as it is. A user who moved a left-justified label's text left of its connection point, so that the text runs up to that point, gets a right-justified KiCad label ending at it. Across 80 test designs this changes 72 of 1670 labels, each closer to its Ulticap position, and leaves every label that sits at its anchor as it was. With `label_keep_alignment = yes` the original alignment is kept instead, the label's point unchanged — the text may then lie on the other side of that point.

(Two earlier rules decided this axis by the ratio of the anchor-to-POE distance to the text width instead: the very first version always placed a label at its POE and applied the ratio to every label and to both axes, with a flat per-character width guess — which also centred texts across their own wire; the later `along_wire_centre_lo` / `along_wire_centre_hi` settings kept the ratio for the crossing fallback only. The side rule replaces both: it never moves the label's point, and never changes a label at its anchor.)

The KiCad angle only ever needs recomputing when whichever axis just flipped also happens to be the label's own text-reading axis (the horizontal axis for horizontal text, the vertical axis for vertical text), which is always the case for the reading-axis adjustment above.

**One more step, applied after all of the above.** Whatever position and justify the four cases settle on is still an anchor Ulticap's own box model would render differently from KiCad's direct-anchor justify (see `ULTICAP_TEXT_MODEL.md` §7) — so the same per-justification, per-size shift used everywhere else in the writer is applied here too, using whichever justify was ultimately chosen. Standalone labels compute their own justify independently of `ulticap_translate` (no component rotation is involved), so unlike the shared component-attached helper — which only ever needs a 0-or-90-degree rotation, because its inputs already arrive pre-swapped for a 180-degree case — this one applies the label's actual final angle in full (0, 90, 180, or 270) to the local shift before adding it to the position. See `ULTICAP_TEXT_MODEL_IMPLEMENTATION.md`'s Writer implementation section for the detail and its current verification status.

[↑ README contents](../../README.md#file-formats)
