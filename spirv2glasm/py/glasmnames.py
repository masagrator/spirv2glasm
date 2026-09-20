"""glasmnames.py -- GLSLC's GLASM numbers by name, as spvnames.py is SPIR-V's.

The numbers come from `py/glslc/glasm.py`, the table tools/mkglasmdefs.py
writes from what the tools read out of the image:

* `Op.<name>`: the OCG opcodes the namer chain spells (`Op.MUL` is 0x90;
  a spelling several opcodes share carries the number, `Op.MOV_47`);
* `RoundingOp.<name>`: the two the printer spells from the rounding mode;
* `RoundingMode.<mnemonic>`: that mode (`RoundingMode.TRUNC` is 4);
* `Type.<name>`: the IR type codes (`Type.F32` is 6);
* `MNEMONIC[opcode]`, `SUFFIX[type code]`, `LONG_SUFFIX_OPS`: the spellings,
  keyed by number, for the printer's rules (py/opchain.py).

An unknown name is an AttributeError at import time, not a silent number.
"""
import os
import runpy
from types import SimpleNamespace

_TABLE = runpy.run_path(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "glslc", "glasm.py"))["glasm"]

Op = SimpleNamespace(**_TABLE["Op"])
RoundingOp = SimpleNamespace(**_TABLE["RoundingOp"])
RoundingMode = SimpleNamespace(**dict(_TABLE["RoundingMode"]))
Type = SimpleNamespace(**_TABLE["Type"])

MNEMONIC = dict((_TABLE["Op"][k], v) for k, v in _TABLE["Mnemonic"].items())
SUFFIX = dict((_TABLE["Type"][k], v) for k, v in _TABLE["TypeSuffix"].items())
LONG_SUFFIX_OPS = frozenset(_TABLE["LongSuffixOps"])
ROUNDING_MNEMONIC = dict((v, k) for k, v in _TABLE["RoundingMode"].items())
