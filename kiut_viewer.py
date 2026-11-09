# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.
"""The KIUT viewer: python kiut_viewer.py [<file.SCH | file.BLK | file.UTSCH>]

Ulticap schematics (.SCH, .UTSCH) shown as Ulticap draws them; needs PySide6. Starts
kiut.viewer.kiut_viewer from the KIUT folder."""
import os
import runpy
import sys

if __name__ == '__main__':
    # This folder, also when Python leaves it off the path (python -I, -P).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    runpy.run_module('kiut.viewer.kiut_viewer', run_name='__main__', alter_sys=True)
