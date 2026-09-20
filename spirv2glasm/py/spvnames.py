"""spvnames.py -- SPIR-V numbers by their Khronos names.

The numbers come from Khronos's own files in `py/khronos/`, used unmodified:

* `Op.<name>`: the opcodes, from `spirv.py`'s `spv['Op']` (`Op.OpLoad` is
  61);
* `GLSL450.<name>`: the GLSL.std.450 extended instructions, from
  `extinst.glsl.std.450.grammar.json` (`GLSL450.FClamp` is 43);
* `Enum.<Kind>.<name>`: every other enumeration in `spv`
  (`Enum.StorageClass.Output`).

An unknown name is an AttributeError at import time, not a silent number.
"""
import json
import os
import runpy
from types import SimpleNamespace

_HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "khronos")

# `spirv.py` is a module that defines one dict, `spv`; run it rather than
# import it, so the Khronos file needs no package layout of ours.
_SPV = runpy.run_path(os.path.join(_HERE, "spirv.py"))["spv"]

Op = SimpleNamespace(**_SPV["Op"])

with open(os.path.join(_HERE, "extinst.glsl.std.450.grammar.json")) as _f:
    GLSL450 = SimpleNamespace(**dict(
        (_i["opname"], _i["opcode"])
        for _i in json.load(_f)["instructions"]))

Enum = SimpleNamespace(**dict(
    (_k, SimpleNamespace(**_v)) for _k, _v in _SPV.items()
    if isinstance(_v, dict) and _k != "Op"))

# The enumerations this converter reads most, under their Khronos names.
ExecutionModel = Enum.ExecutionModel
ExecutionMode = Enum.ExecutionMode
BuiltIn = Enum.BuiltIn
Dim = Enum.Dim
StorageClass = Enum.StorageClass
Decoration = Enum.Decoration
ImageOperands = Enum.ImageOperandsMask
ImageFormat = Enum.ImageFormat

# The opcode families this converter treats alike.
ACCESS_CHAINS = (Op.OpAccessChain, Op.OpInBoundsAccessChain,
                 Op.OpPtrAccessChain)
