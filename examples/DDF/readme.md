# Demo conversion files
- These files can be used to test the conversion capabilities.
- Some shapes are created for this purpose only.
- Each layout uses all 32 layers.
- All files, except Ortho_V4, V2 and V3 are Ultiboard V5 DDFs.
```
All_angle.ddf      - SMD and through hole components placed on Top and Bottom layers.
                     Rotation angles: 0 to 360 degrees in steps of 15 degrees.
ML32.ddf           - Components with unusual shapes. A pad is placed on one layer
                     at a time at the Top layer, each inner layer and the Bottom layer.
                   - Traces on each layer.
                   - Blind/buried vias between each layer.
                   - Text on each layer.
Ortho.ddf          - Component and text placement using 0, 90, 180 and 270 degrees angles.
                   - Polygons on layers Top, Bottom, Inner 1 and Inner 5.
                   - All possible hatch patterns.
Ortho_V4.ddf       - Identical to Ortho.ddf, saved as Ultiboard V4 DDF.
Powerplanes.ddf    - Polygon on layer Top.
                   - Powerplanes on layers Inner 4, Inner 15 and Inner 27.
V2.ddf             - V2 DDF file
V3.ddf             - V3 DDF file

Note:
Ultiboard V5.72 does not handle V2 and V3 DDF files correctly (pad drill codes are lost).
KIUB takes care of this issue and correctly converts the files to Kicad.
For archival purposes, KIUB_V2V3.py can also be used standalone to convert V2 and V3 files to V4.6.
```
