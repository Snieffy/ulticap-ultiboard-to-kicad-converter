# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/netlist.py -- a KiCad netlist against Ulticap's, net by net (the KIUT window's Compare
netlists; KIUC's Netlist Compare before stage 9c)

The net names are ignored: a net is equal when its set of component pins is, e.g. {R1-1, IC2-5,
C3-2}. build_report gives the comparison as text. When KIUT's designator fix renamed designators
(ICA -> IC1A, J9A -> J9A1), the KiCad netlist has the new names and Ulticap's the old ones:
designator_renames gives the renames as a netlist sees them -- a part's, not a unit's (IC -> IC1)
-- and build_report reads Ulticap's netlist through them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

Pin = Tuple[str, str]          # (reference, pin)
PinGroup = FrozenSet[Pin]


@dataclass(frozen=True)
class NetGroup:
    name: str
    pins: PinGroup


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def pin_sort_key(pin: Pin) -> Tuple[str, int, str]:
    """Natural-ish sort: R2 before R10, then pin text."""
    ref, p = pin
    m = re.match(r"^([A-Za-z_]+)(\d+)$", ref)
    if m:
        return (m.group(1).upper(), int(m.group(2)), p)
    return (ref.upper(), 10**9, p)


def format_pin(pin: Pin) -> str:
    ref, p = pin
    return f"{ref}-{p}" if p else f"{ref}-(empty pin)"


def format_pins(pins: Iterable[Pin]) -> str:
    return ", ".join(format_pin(pin) for pin in sorted(pins, key=pin_sort_key))


def safe_stem(path: Path) -> str:
    stem = path.stem.strip() or "netlist"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") or "netlist"


def default_report_path(kicad_path: str, ulticap_path: str) -> str:
    if not kicad_path or not ulticap_path:
        return ""
    kp = Path(kicad_path)
    up = Path(ulticap_path)
    return str(kp.with_name(f"{safe_stem(kp)}__VS__{safe_stem(up)}__net_compare.txt"))


# ---------------------------------------------------------------------------
# KiCad S-expression .net parser
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp437", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1", errors="replace")


def _find_balanced_forms(text: str, head: str) -> List[str]:
    """Return complete S-expression forms starting with '(head'."""
    forms: List[str] = []
    i = 0
    needle = f"({head}"
    n = len(text)

    while True:
        start = text.find(needle, i)
        if start < 0:
            break

        # Avoid matching words that merely begin with the head.
        after = start + len(needle)
        if after < n and text[after] not in " \t\r\n()":
            i = after
            continue

        depth = 0
        in_string = False
        escaped = False
        for j in range(start, n):
            ch = text[j]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        forms.append(text[start:j + 1])
                        i = j + 1
                        break
        else:
            raise ValueError(f"Unbalanced S-expression while reading {head!r}")
    return forms


def _sexpr_string_value(form: str, key: str) -> Optional[str]:
    m = re.search(r"\(" + re.escape(key) + r"\s+\"((?:\\.|[^\"])*)\"\)", form)
    if not m:
        return None
    return bytes(m.group(1), "utf-8").decode("unicode_escape")


def parse_kicad_netlist(path: str) -> List[NetGroup]:
    text = _read_text(path)
    groups: List[NetGroup] = []

    for net_form in _find_balanced_forms(text, "net"):
        name = _sexpr_string_value(net_form, "name") or "(unnamed)"
        pins: List[Pin] = []

        for node_form in _find_balanced_forms(net_form, "node"):
            ref = _sexpr_string_value(node_form, "ref") or ""
            pin = _sexpr_string_value(node_form, "pin") or ""
            if ref:
                pins.append((ref.strip(), pin.strip()))

        groups.append(NetGroup(name=name, pins=frozenset(pins)))

    if not groups:
        raise ValueError("No KiCad '(net ...)' groups found. Is this a KiCad Eeschema .net file?")
    return groups


# ---------------------------------------------------------------------------
# Ulticap .net parser
# ---------------------------------------------------------------------------

def parse_ulticap_netlist(path: str) -> List[NetGroup]:
    text = _read_text(path)
    groups: List[NetGroup] = []
    current_name: Optional[str] = None
    current_pins: List[Pin] = []

    def flush() -> None:
        nonlocal current_name, current_pins
        if current_name is not None:
            groups.append(NetGroup(name=current_name, pins=frozenset(current_pins)))
        current_name = None
        current_pins = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("+") or line.startswith("="):
            continue

        if line.startswith("*"):
            flush()
            current_name = line[1:].strip()
            continue

        if current_name is None:
            continue

        # Ulticap usually emits comma-separated COMP-PIN entries.
        for item in line.split(","):
            item = item.strip()
            if not item:
                continue
            m = re.match(r"^([^\s,\-]+)\s*-\s*(\S*)$", item)
            if m:
                current_pins.append((m.group(1).strip(), m.group(2).strip()))
            else:
                # Keep unusual tokens visible instead of silently dropping them.
                current_pins.append((item, ""))

    flush()

    if not groups:
        raise ValueError("No Ulticap '* netname' groups found. Is this an Ulticap .net file?")
    return groups


# ---------------------------------------------------------------------------
# Comparison and report generation
# ---------------------------------------------------------------------------

def index_by_group(groups: Sequence[NetGroup]) -> Dict[PinGroup, List[NetGroup]]:
    out: Dict[PinGroup, List[NetGroup]] = {}
    for g in groups:
        out.setdefault(g.pins, []).append(g)
    return out


def group_sort_key(g: NetGroup) -> Tuple[int, str, str]:
    first_pin = format_pin(sorted(g.pins, key=pin_sort_key)[0]) if g.pins else ""
    return (len(g.pins), first_pin, g.name)


def build_report(kicad_path: str, ulticap_path: str, renames: Optional[Dict[str, str]] = None) -> str:
    """The comparison as a text report. renames: Ulticap's designators as
    the KiCad side has them ({old: new}, KIUT's designator fix): applied to
    Ulticap's nets before comparing, and listed in the report."""
    kicad = parse_kicad_netlist(kicad_path)
    ulticap = parse_ulticap_netlist(ulticap_path)
    renames = dict(renames or {})
    if renames:
        ulticap = [NetGroup(g.name, frozenset((renames.get(r, r), p) for r, p in g.pins)) for g in ulticap]

    k_by_group = index_by_group(kicad)
    u_by_group = index_by_group(ulticap)

    matched: List[Tuple[NetGroup, NetGroup]] = []
    missing_ulticap: List[NetGroup] = []
    extra_kicad: List[NetGroup] = []

    for u in sorted(ulticap, key=group_sort_key):
        candidates = k_by_group.get(u.pins, [])
        if candidates:
            matched.append((candidates[0], u))
        else:
            missing_ulticap.append(u)

    for k in sorted(kicad, key=group_sort_key):
        if k.pins not in u_by_group:
            extra_kicad.append(k)

    k_name = Path(kicad_path).name
    u_name = Path(ulticap_path).name

    lines: List[str] = []
    lines.append(f"{safe_stem(Path(kicad_path))} / {safe_stem(Path(ulticap_path))} netlist group comparison")
    lines.append("=" * 72)
    lines.append("")
    lines.append("Comparison rule: net names ignored; each net is compared as a set of (component reference, pin).")
    lines.append("")
    lines.append(f"KiCad file: {k_name}")
    lines.append(f"Ulticap file: {u_name}")
    lines.append("")
    lines.append(f"KiCad {k_name} nets: {len(kicad)}")
    lines.append(f"Ulticap {u_name} nets: {len(ulticap)}")
    lines.append(f"Matched groups: {len(matched)} / {len(ulticap)} Ulticap groups")
    lines.append(f"Ulticap groups missing in KiCad: {len(missing_ulticap)}")
    lines.append(f"Extra KiCad groups not in Ulticap: {len(extra_kicad)}")
    lines.append("")
    if renames:
        lines.append("Ulticap designators read as KIUT's designator fix renamed them: "
                     + ", ".join(f"{o} -> {n}" for o, n in sorted(renames.items())))
        lines.append("")

    if missing_ulticap:
        lines.append("Ulticap groups missing in KiCad:")
        for u in sorted(missing_ulticap, key=group_sort_key):
            lines.append(f"- Ulticap {u.name} ({len(u.pins)} pins): {format_pins(u.pins)}")
        lines.append("")

    if extra_kicad:
        lines.append("Extra KiCad-only groups:")
        for k in sorted(extra_kicad, key=group_sort_key):
            pins_text = format_pins(k.pins) if k.pins else "(empty net)"
            lines.append(f"- KiCad {k.name} ({len(k.pins)} pins): {pins_text}")
        lines.append("")

    lines.append("Matched groups:")
    for k, u in sorted(matched, key=lambda ku: group_sort_key(ku[1])):
        same_name = k.name == u.name or k.name.lstrip("/") == u.name
        name_note = "same/similar net name" if same_name else "different net name"
        pins_text = format_pins(u.pins) if u.pins else "(empty net)"
        lines.append(f"- KiCad {k.name} == Ulticap {u.name} ({len(u.pins)} pins, {name_note})")
        lines.append(f"  KiCad pins:   {pins_text}")
        lines.append(f"  Ulticap pins: {pins_text}")

    return "\n".join(lines) + "\n"


def designator_renames(root, ddf=None) -> Dict[str, str]:
    """The designators KIUT's designator fix renamed in the design whose
    root sheet (or .UTSCH) is root, as a netlist names them: {old: new} --
    a part with several units by its part's name (ICA -> IC1A: IC -> IC1),
    one of a single unit as it is (J9A -> J9A1). Empty when the design's
    corrected copy of root was not renamed by the fix (its manifest)."""
    from kiut.core.corrected import CorrectedFolder, locate
    from kiut.schematic.kiuc_refdes import CORRECTION, plan_new_names
    root = Path(root)
    folder = CorrectedFolder(locate(root), 'KIUT')
    if not folder.exists():
        return {}
    try:
        record = folder.manifest().files.get(root.name, {})
    except ValueError:
        return {}
    if CORRECTION not in record.get('corrections', []):
        return {}
    offending, new = plan_new_names(root, str(ddf) if ddf else None)
    renames: Dict[str, str] = {}
    for o in offending:
        n = new.get(o.refdes)
        if not n:
            continue
        if o.parts > 1 and o.refdes[-1:].isalpha() and n[-1:] == o.refdes[-1:]:
            renames[o.refdes[:-1]] = n[:-1]          # a unit: the part's name
        else:
            renames[o.refdes] = n
    return renames
