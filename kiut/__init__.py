# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.
"""KIUT: KiCad import of Ulticap schematics and Ultiboard boards.

The code of both converters, in one package:

- kiut.schematic: KIUC, Ulticap schematics and symbol libraries -> KiCad
- kiut.board:     KIUB, Ultiboard boards and libraries -> KiCad
- kiut.viewer:    the viewer (PySide6)
- kiut.core:      code both converters share (filled in from stage 4 on)

The programs are started from the files in the KIUT folder (kiut.py, kiut_gui.py,
kiut_viewer.py), which is also where the settings files are kept: kiut.ini (the window and
kiut.py), kiut_viewer.ini (the viewer).
"""
from pathlib import Path

# The KIUT folder: the start files, and the settings files next to them
# (kiut.ini; KIUC's kiuc.ini and KIUB's kiub_gui.ini were kept there too, and the KIUT window
# imports them from there).
APP_DIR = Path(__file__).resolve().parent.parent

VERSION = '1.0'
REPOSITORY = 'https://github.com/Snieffy/ulticap-ultiboard-to-kicad-converter'
# The About boxes' credit line (the author, 2026-11-03; the README has it in full).
CREDIT = ("KIUB's original code by the author; KIUC's code, KIUB's further development and KIUT "
          "by an AI assistant (Claude, by Anthropic), from the author's test data, findings and "
          "design decisions.")
