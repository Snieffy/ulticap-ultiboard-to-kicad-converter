# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/settings.py -- Reading and writing a settings file

The programs keep their settings in .ini files in the KIUT folder:
kiut.ini (the KIUT window and command line) and kiut_viewer.ini (the
viewer's own; KIUC's kiuc.ini until stage 10a). KIUC's kiuc.ini and KIUB's
kiub_gui.ini are only read: the KIUT window imports them, and the viewer
takes its sections from kiuc.ini once. Each tool owns its file's
sections, keys and values; reading and writing them is the same and is
here. Every call takes the file's path, so a caller (or a test) can point
it elsewhere at any time.

- load / save: the whole file. A file is always written whole: a change
  to one section reads the file first and keeps every other section
  (update).
- keep_case: option names keep their letter case (KIUB's kiub_gui.ini,
  whose [font_ratios] keys are font names) or are lowercased, configparser's
  default (KIUC's kiuc.ini, which has always been written that way: its
  tuning names are found in either case). A file is always read with the
  rule it is written with, or a key written in another case is lost.
- floats, boolean, kicad_exe: values read the way both tools read them,
  an unreadable value taken as missing.

Until KIUT stage 4 each tool had its own copy of each of these.
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict, Iterable, Mapping


def load(path, keep_case: bool = False) -> configparser.ConfigParser:
    """The settings file at path (an empty one when it doesn't exist or
    can't be read)."""
    cfg = configparser.ConfigParser()
    if keep_case:
        cfg.optionxform = str      # type: ignore[assignment,method-assign]
    cfg.read(path, encoding='utf-8')
    return cfg


def save(path, cfg: configparser.ConfigParser) -> None:
    """Write the whole file."""
    with open(path, 'w', encoding='utf-8') as f:
        cfg.write(f)


def update(path, section: str, values: Mapping[str, str], keep_case: bool = False) -> None:
    """Set values (strings, in their order) in section of the file at path,
    keeping every other section and key."""
    cfg = load(path, keep_case)
    if not cfg.has_section(section):
        cfg.add_section(section)
    for key, value in values.items():
        cfg.set(section, key, value)
    save(path, cfg)


def floats(cfg: configparser.ConfigParser, section: str, names: Iterable[str]) -> Dict[str, float]:
    """The values of names found in section as numbers; a name not there,
    or not a number, is left out (the caller's default stands)."""
    values: Dict[str, float] = {}
    if cfg.has_section(section):
        for name in names:
            if cfg.has_option(section, name):
                try:
                    values[name] = cfg.getfloat(section, name)
                except ValueError:
                    pass   # corrupted entry; fall back to the default
    return values


def boolean(cfg: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    """A yes/no value (yes/no, true/false, on/off, 1/0); default when it is
    missing or none of these."""
    try:
        return cfg.getboolean(section, key, fallback=default)
    except ValueError:
        return default


def kicad_exe(cfg: configparser.ConfigParser, section: str = 'kicad', key: str = 'executable') -> str:
    """The stored KiCad executable, or '' when none is stored or it is no
    longer there."""
    path = cfg.get(section, key, fallback='').strip()
    return path if path and Path(path).is_file() else ''
