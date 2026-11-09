# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.

"""
kiut/core/folders.py -- Where a library-only conversion goes (KIUT stage 10e)

A library-only conversion goes into KIUT's library folder, by default Documents/KIUT in the user's
folder, its subfolders created when needed. They hold the libraries side by side, ready to be copied
to KiCad or added to its library tables:

  Symbols/     Ulticap symbol libraries (.UTLIB, .LIB): <name>.kicad_sym and <name>.log, one
               sym-lib-table, Ulticap_lib_kiut.ini
  Blocks/      block libraries (a .BLK, or a .SCH with block_library = yes): <name>.kicad_blocks and
               its settings copy <name>_kiut.ini, one design-block-lib-table
  Footprints/  Ultiboard libraries (.LIB, .L55) and library DDFs (shapes only, or components without
               nets converted as a library; stage 10f): <name>.pretty, one fp-lib-table, and each library's
               overview board in <name>_lib_KiCad (.kicad_pcb, .kicad_pro, log, settings copy)

A design stays next to its files, and so does what a design makes: its own symbol and footprint
libraries, and a design's DDF converted as a library only.

The library folder: the environment variable KIUT_LIBRARY_FOLDER when set (one run, a script), else
kiut.ini's [folders] libraries (the window's File -> Library output folder...), else the default.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional

ENV = 'KIUT_LIBRARY_FOLDER'
SECTION, KEY = 'folders', 'libraries'
SYMBOLS, BLOCKS, FOOTPRINTS = 'Symbols', 'Blocks', 'Footprints'
SUBFOLDERS = {'symbols': SYMBOLS, 'blocks': BLOCKS, 'footprints': FOOTPRINTS}
INI = None              # the KIUT window's kiut.ini when it isn't kiut_settings.SETTINGS_FILE


def default_root() -> Path:
    """Documents/KIUT in the user's folder."""
    return Path.home() / 'Documents' / 'KIUT'


def stored(ini=None) -> str:
    """kiut.ini's [folders] libraries ('' when not set)."""
    if ini is None:
        from kiut.core import kiut_settings
        ini = INI or kiut_settings.SETTINGS_FILE
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(str(ini), encoding='utf-8')
    except (OSError, configparser.Error, UnicodeDecodeError):
        return ''
    return cfg.get(SECTION, KEY, fallback='').strip()


def library_root(ini=None) -> Optional[Path]:
    """The library folder: KIUT_LIBRARY_FOLDER, else kiut.ini's, else the
    default. (None, only as KIUT's tests set it: next to each input, the
    converters' own default.)"""
    text = os.environ.get(ENV, '').strip() or stored(ini)
    return Path(os.path.expanduser(text)) if text else default_root()


def library_folder(kind: str, ini=None) -> Optional[Path]:
    """The subfolder of one kind ('symbols', 'blocks', 'footprints'); None:
    next to the input."""
    root = library_root(ini)
    return None if root is None else root / SUBFOLDERS[kind]


def writable(folder) -> bool:
    """Whether folder can be written to, or made: its nearest existing
    parent tried."""
    from kiut.core.corrected import is_writable
    p = Path(folder)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p.is_dir() and is_writable(p)


READ_ONLY = ('KIUT\'s library folder cannot be written to:\n{folder}\n\nChoose another one: the '
             'window\'s File -> Library output folder..., or kiut.ini\'s [folders] libraries.')
