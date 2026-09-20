"""types.py -- SPIR-V types as the compiler sees them.

Three views of a type:

* its COMPONENT COUNT, which f_7100f13410 turns into a destination write mask
  (notes/41);
* its GLASM TYPE CODE, the value `node[24]` carries and the one a mnemonic's
  suffix is read from (notes/37);
* its `#var` SPELLING (`float4`, `float4x4`, `ulong`), read from the printer's
  name table at 0x71014f95c8 (notes/06).
"""
from spvnames import Op
from glasmnames import SUFFIX

from glasmlib.common import NotEstablished, HANDLE_TYPES, ARRAY_TYPES, \
    SCALAR_TYPES, COMPOSITE_TYPES

# The printer's type code for `bool` (notes/06: the printer's type
# enumeration, the same numbering the suffix table uses -- 6 is `float` and
# prints `.F32`, 11 is `int` and prints `.S32`).
BOOL_TYPE_CODE = 0x14


def _unread(what):
    raise NotEstablished("no established #var spelling for %s" % what)


# The printer's base-type names, indexed by ITS type code (notes/06).  Only the
# ones a SPIR-V module can reach are mapped here; the table also holds `half`,
# `fixed`, `short`, `ushort`, `byte`, `ubyte`, `texture`, `sampler`, `string`.
_FLOAT_NAME = {16: "half", 32: "float", 64: "double"}
_INT_NAME = {(32, 1): "int", (32, 0): "uint",
             (16, 1): "short", (16, 0): "ushort",
             (8, 1): "byte", (8, 0): "ubyte",
             (64, 0): "ulong"}


def _scalar_name(module, tid):
    t = module.types.get(tid)
    if t is None:
        raise NotEstablished("type %%%d is not a declared type" % tid)
    if t.opcode == Op.OpTypeFloat:
        return _FLOAT_NAME.get(t.operands[1]) or \
            _unread("OpTypeFloat width %d" % t.operands[1])
    if t.opcode == Op.OpTypeInt:
        w, signed = t.operands[1], t.operands[2]
        return _INT_NAME.get((w, signed)) or \
            _unread("OpTypeInt %d signed=%d" % (w, signed))
    if t.opcode == Op.OpTypeBool:
        return "bool"
    return _unread("base type of %s" % t.name)


def _components(module, tid):
    """How many components the result writes -- the count f_7100f13410 turns
    into the destination write mask (notes/41)."""
    t = module.types.get(tid)
    if t is None:
        return None
    if t.opcode == Op.OpTypeVector:
        return t.operands[2]
    if t.opcode == Op.OpTypeMatrix:
        return None
    return 1


def _vi_is_scalar(module, vid):
    """True when SPIR-V value `vid` has a one-component type."""
    ins = module.result_insn.get(vid)
    return (ins is not None and ins.has_result_type
            and _components(module, ins.result_type) == 1)


def _signedness(module, tid):
    """`S` or `U` for an integer type (or vector of one), else None."""
    t = module.types.get(tid)
    if t is None:
        return None
    if t.opcode == Op.OpTypeVector:
        t = module.types.get(t.args()[0])
    if t is None or t.opcode != Op.OpTypeInt:
        return None
    return "S" if t.args()[1] else "U"


def _pointee(module, var):
    """The type id a variable (or pointer) points to, or None."""
    ins = module.globals.get(var) or module.result_insn.get(var)
    if ins is None or not ins.has_result_type:
        return None
    pt = module.types.get(ins.result_type)
    if pt is None or pt.opcode != Op.OpTypePointer:
        return None
    return pt.args()[1]


def _pointee_components(module, var):
    """The component count of the value a variable holds, or None."""
    tid = _pointee(module, var)
    return None if tid is None else _components(module, tid)


def _pointee_code(module, var):
    """The GLASM type code of the value a variable (or pointer) holds."""
    tid = _pointee(module, var)
    return None if tid is None else _glasm_type_code(module, tid)


# The suffix each SPIR-V scalar prints, which names its type code in the
# image's own suffix table (`glasmnames.SUFFIX`, from py/glslc/glasm.py).
_FLOAT_SUFFIX = {16: ".F16", 32: ".F32", 64: ".F64"}
_INT_SUFFIX = {(8, 1): ".S8", (8, 0): ".U8",
               (16, 1): ".S16", (16, 0): ".U16",
               (32, 1): ".S32", (32, 0): ".U32",
               (64, 1): ".S64", (64, 0): ".U64"}


def _glasm_type_code(module, tid):
    """The GLASM node's type code.  The codes are NOT invented here: the
    suffix table (`glasmnames.SUFFIX`) is the image's own, and it NAMES each code
    -- 6 prints `.F32`, 0xb prints `.S32`, 0x13 prints `.F64` and so on -- so
    a SPIR-V scalar type is matched to the code whose printed name is that
    type."""
    t = module.types.get(tid)
    while t is not None and t.opcode in COMPOSITE_TYPES:
        t = module.types.get(t.operands[1])
    if t is None:
        raise NotEstablished("type %%%d is not a declared type" % tid)
    if t.opcode == Op.OpTypeFloat:
        name = _FLOAT_SUFFIX.get(t.operands[1])
    elif t.opcode == Op.OpTypeInt:
        name = _INT_SUFFIX.get((t.operands[1], t.operands[2]))
    elif t.opcode == Op.OpTypeBool:
        return BOOL_TYPE_CODE
    else:
        name = None
    if name is None:
        raise NotEstablished("no GLASM type code for %s" % t.name)
    # the LOWEST code of that spelling (two codes print `.F16`, two `.S64`)
    for code, spelling in sorted(SUFFIX.items()):
        if spelling == name:
            return code
    raise NotEstablished("the image's suffix table has no %s" % name)


def type_spelling(module, tid):
    """`float4`, `float4x4`, `ulong` -- the type as the `#var` line spells it."""
    t = module.types.get(tid)
    if t is None:
        raise NotEstablished("type %%%d is not a declared type" % tid)
    if t.opcode == Op.OpTypeVector:
        return "%s%d" % (_scalar_name(module, t.operands[1]), t.operands[2])
    if t.opcode == Op.OpTypeMatrix:
        col = module.types.get(t.operands[1])
        if col is None or col.opcode != Op.OpTypeVector:
            raise NotEstablished("OpTypeMatrix whose column is not a vector")
        return "%s%dx%d" % (_scalar_name(module, col.operands[1]),
                            t.operands[2], col.operands[2])
    if t.opcode in HANDLE_TYPES:
        # An opaque uniform is a 64-bit BINDLESS HANDLE loaded from constant
        # buffer 14, and the listing declares it as such (notes/10).
        return "ulong"
    if t.opcode in ARRAY_TYPES:
        return type_spelling(module, t.operands[1])
    if t.opcode in SCALAR_TYPES:
        return _scalar_name(module, t.operands[0])
    return _unread(t.name)
