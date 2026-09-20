# Khronos files, unmodified

These files are copied verbatim from the Khronos SPIRV-Headers repository
(https://github.com/KhronosGroup/SPIRV-Headers, `include/spirv/unified1/`).
The copy comes from the Ubuntu `spirv-headers` package
1.6.1+1.4.309.0, which is the release `py/spvgrammar.py` was generated from
(`tools/mkgrammar.py`).  Keeping the same release keeps the two consistent.

* `spirv.py`: Khronos's Python binding of the SPIR-V enums (`spv['Op']`,
  `spv['StorageClass']`, ...).
* `extinst.glsl.std.450.grammar.json`: the GLSL.std.450 extended
  instruction set.  Khronos publishes no Python binding for it.

Nothing here is edited.  `py/spvnames.py` exposes the numbers as names
(`Op.OpLoad`, `GLSL450.FClamp`).
