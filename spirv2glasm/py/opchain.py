"""opchain.py -- the SPIR-V-opcode-to-GLASM-mnemonic chain, read from the image.

Nothing here is fitted to a listing.  Every table this module loads was
extracted from `subsdk0.elf` by a tool in `tools/`, and the chain is the one
notes/33..37 establish:

    SPIR-V opcode
      -> notes/body_workers.json   the worker's operator-code constant
      -> notes/lowering_map.json   the IR operation FAMILY BASE   (tools/lowermap.py)
      -> notes/ir_glasm_map.json   the GLASM opcode               (tools/iropmap.py)
      -> py/glslc/glasm.py         the mnemonic and the type suffix
                                   (tools/mkglasmdefs.py, from tools/opname_dec.py
                                   and tools/typesuffix.py)

The operand-form variant (notes/35) moves WITHIN a family and never changes the
opcode, so the mnemonic does not depend on it; it is therefore not needed here.

`mnemonic()` returns None rather than guessing whenever any link is missing --
an operator the lowering does not name, a family the emitter's table does not
name, an opcode with no mnemonic, or a type outside the suffix table.  A caller
must treat None as "not established" and refuse the shader.
"""
import json
import os

from glasmnames import Op, RoundingOp, MNEMONIC, SUFFIX, LONG_SUFFIX_OPS

_NOTES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "notes")


def _load(name):
    with open(os.path.join(_NOTES, name)) as fh:
        return json.load(fh)


_WORKERS = _load("body_workers.json")["handlers"]
_LOWER = _load("lowering_map.json")["map"]
_IRMAP = _load("ir_glasm_map.json")["simple"]
_EXTSET = _load("extset_names.json")["name_by_number"]
_NAMEID = _load("name_ids.json")["name_by_id"]
_BUILTIN = _load("builtin_opcodes.json")["opcode_by_builtin_id"]

# name -> interned id (the first id wins; the dump is id-ordered)
_ID_BY_NAME = {}
for _k, _v in _NAMEID.items():
    _ID_BY_NAME.setdefault(_v, _k)

# notes/24: `w4` is the operator code for the BINARY worker and `w3` for the
# UNARY one.  Both are read here rather than assumed from the constant's name,
# because a handler can set both (`OpVectorExtractDynamic` sets w3 and w4).
_BINARY_WORKER = "0x7100fcf930"
_UNARY_WORKER = "0x7100fcf694"

# The float test f_71000569f0 and the unsigned/signed tests f_7100056a80 /
# f_7100050dd0 are three bitmask predicates over the type code, transcribed
# from the image (each is `(t < limit) && ((mask >> t) & 1)`).
_FLOAT = 0x800801C0
_UNSIGNED = 0x0000001000015400
_SIGNED = 0x000000100001FE00


def _is_float(t):
    return t < 0x20 and (_FLOAT >> t) & 1


def _is_unsigned(t):
    return t < 0x25 and (_UNSIGNED >> t) & 1


def _is_signed(t):
    return t < 0x25 and (_SIGNED >> t) & 1


def _operator_codes():
    """SPIR-V opcode NAME -> the operator code its handler hands the lowering."""
    out = {}
    for h in _WORKERS.values():
        worker = h.get("worker")
        consts = h.get("consts", {})
        if worker == _BINARY_WORKER:
            code = consts.get("w4")
        elif worker == _UNARY_WORKER:
            code = consts.get("w3")
        else:
            continue
        if code is None:
            continue
        for name in h.get("opcodes", []):
            out[name] = int(code, 0)
    return out


OPERATOR = _operator_codes()


def family_base(spirv_name):
    """The IR operation family the lowering gives this SPIR-V opcode."""
    code = OPERATOR.get(spirv_name)
    if code is None:
        return None
    arm = _LOWER.get("%#x" % code)
    if arm is None:
        return None
    calls = arm.get("calls", [])
    # An arm that reaches more than one factory/base pair was not resolved, and
    # picking one of them would be a guess.
    if len(calls) != 1:
        return None
    base = calls[0].split(":")[1]
    if base == "None":
        return None
    return int(base, 0)


def glasm_opcode(spirv_name):
    base = family_base(spirv_name)
    if base is None:
        return None
    entry = _IRMAP.get("%#x" % base)
    if entry is None:
        return None
    return int(entry["opcode"], 0)


def suffix(opcode, type_code):
    """notes/37: an opcode in `long_suffix_opcodes` with a FLOAT type takes the
    full type name; everything else takes the two-character form."""
    if opcode in LONG_SUFFIX_OPS and _is_float(type_code):
        return SUFFIX.get(type_code)
    if _is_unsigned(type_code):
        return ".U"
    if _is_signed(type_code):
        return ".S"
    if _is_float(type_code):
        return ".F"
    return None


# f_7100069df0, a per-instruction peephole in the step that runs between the
# builder and the printer (notes/38): a node whose opcode is 0xa2 (`SUB`) is
# replaced by a clone with opcode 0x83 (`ADD`) whose SECOND SOURCE has its
# negate bit flipped --
#
#     69e0c:  if (node[8] != 0xa2) return node
#     69e14:  if (cg[384] & 0x40)  return node
#     69e44:  new[8] = 0x83
#     69e94:  new[220] = (node[216] >> 32) ^ 1      ; the negate flag
#
# so `a - b` is printed `ADD dst, a, -b`.  `cg[384] bit 6` is a profile flag
# that switches the rewrite off; it is clear for every stage measured here.
_PEEPHOLE = {Op.SUB: (Op.ADD, True)}


def rewrite(opcode):
    """(opcode, negate_second_source) after the image's peepholes."""
    return _PEEPHOLE.get(opcode, (opcode, False))


def mnemonic_for_opcode(opcode, type_code):
    """The full GLASM mnemonic for a GLASM OPCODE that is already known.

    The namer table and the type-suffix rule are the image's; this is the tail
    of `mnemonic()` split out for the cases where the opcode does not come
    from the worker table -- the image instructions, whose opcode is measured
    from the compiler's own node instead (py/glslc/glasm.py still supplies
    the name and the suffix).
    """
    if opcode is None:
        return None
    opcode, _ = rewrite(opcode)
    base = MNEMONIC.get(opcode)
    if base is None:
        return None
    suf = suffix(opcode, type_code)
    if suf is None:
        return None
    return base + suf


def mnemonic(spirv_name, type_code):
    """The full GLASM mnemonic, or None when a link in the chain is missing."""
    return mnemonic_for_opcode(glasm_opcode(spirv_name), type_code)


def _load_shape():
    """notes/extinst_shape.json -- MEASURED, by tools/extshape.py.

    The chain below names a builtin's opcode; it does not say whether the call
    becomes one instruction, is scalarised per component, or is inlined
    outright (notes/39).  That is read off the front end's own nodes instead.
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "notes", "extinst_shape.json")
    try:
        with open(path) as f:
            return json.load(f)["shape"]
    except (IOError, OSError, ValueError, KeyError):
        return {}


_SHAPE = _load_shape()


def extinst_single(number, type_code):
    """The mnemonic for a GLSL.std.450 instruction that lowers to ONE
    whole-vector GLASM instruction, or None.

    None means "not established for this builtin": either it was never
    measured, or the measurement says it is scalarised or inlined.  The
    mnemonic and the type suffix come from the image's tables, keyed by the
    opcode the measurement read out of the node.
    """
    e = _SHAPE.get(str(number))
    if not e or not e.get("single"):
        return None
    opcode = int(e["single"]["opcode"], 16)
    base = e["single"].get("mnemonic")
    if opcode in (RoundingOp.ROUNDING_6c, RoundingOp.ROUNDING_6d) and base:
        # THE ROUNDING FAMILY NAMES ITSELF FROM THE MODE, not from the namer
        # table (notes/23): opcode class 0x6c/0x6d carries a 4-bit mode in
        # node[12] which IS the mnemonic -- FLR, ROUND, CEIL or TRUNC -- which
        # is why neither namer has an entry for the opcode and why
        # `mnemonic_for_opcode` cannot spell it.  The measurement records the
        # mode (notes/extinst_shape.json) and the type suffix is the ordinary
        # one.
        suf = suffix(opcode, type_code)
        return None if suf is None else base + suf
    return mnemonic_for_opcode(opcode, type_code)


def extinst_shape(number):
    """The measured shape record, for a refusal that can say what it saw."""
    return _SHAPE.get(str(number))


def extinst_opcode(number):
    """A GLSL.std.450 number's GLASM opcode, via notes/39's chain:
    number -> cgc's name for it -> the interned name id -> f_7100f28a00."""
    name = _EXTSET.get(str(number))
    if name is None:
        return None
    bid = _ID_BY_NAME.get(name)
    if bid is None:
        return None
    entry = _BUILTIN.get(bid)
    if entry is None:
        return None
    opcode = entry["opcode"]
    # An arm that computes its opcode is reported as DYN@... and not guessed.
    if opcode.startswith("DYN"):
        return None
    # The image's own distinction: the arms that also set a MASK (w26) are the
    # ones whose result is not one whole-vector instruction -- every scalar
    # transcendental (COS, SIN, EX2, EXP, LG2, LOG, POW, RSQ) sets 0xff, and
    # the whole-vector builtins (MAX, MIN, FLR, CEIL, SSG, MAD, DDX, DDY) set
    # none.  What the mask then makes the emitter do has not been read, so a
    # masked builtin is refused rather than emitted as one instruction.
    if entry.get("mask") is not None:
        return None
    return int(opcode, 0)


def extinst_mnemonic(number, type_code):
    opcode = extinst_opcode(number)
    if opcode is None:
        return None
    base = MNEMONIC.get(opcode)
    if base is None:
        return None
    suf = suffix(opcode, type_code)
    if suf is None:
        return None
    return base + suf


# The one arm of f_7100f28a00 that COMPUTES its opcode, transcribed rather than
# left as DYN: builtin id 0x497 is `dot`, and
#
#     f28ca0:  w24 = w3 & 0xf          ; the component count
#     f28cd8:  w9  = w24 + 0x86
#     f28cdc:  cmp w24, #2
#     f28ce4:  w8  = 0x90              ; MUL
#     f28cec:  csel w27, w8, w9, cc    ; w24 < 2 ? MUL : 0x86 + w24
#
# so two components give 0x88 `DP2`, three 0x89 `DP3`, four 0x8a `DP4`.
_DOT_ID = "0x497"


def dot_opcode(components):
    if components < 2:
        return Op.MUL
    if components > 4:
        return None
    return Op.DP2 - 2 + components          # the arm's 0x86 + count


def dot_mnemonic(components, type_code):
    opcode = dot_opcode(components)
    if opcode is None:
        return None
    base = MNEMONIC.get(opcode)
    if base is None:
        return None
    suf = suffix(opcode, type_code)
    if suf is None:
        return None
    return base + suf


# The DESTINATION WRITE MASK, read out of f_7100f13410 (notes/41):
#
#     f1352c:  w8 = irnode[40]
#     f1353c:  w9 = (w8 >> 8) & 0xf          ; the component COUNT
#     f13540:  tst 0xe, w8 >> 8              ; count >= 2 ?
#     f1354c:  csinc w9, w9, wzr, ne         ; no -> 1
#     f13550:  w26 = [.rodata 0x11be1c0][w9] ; the mask
#     f13560:  -> node[48]
#
# and the table is {0, 0xff, 0xffff, 0xffffff, 0xffffffff} repeating.  notes/29
# read the printed form of each: 0xffffffff prints NO swizzle, 0xff prints `.x`.
MASK_BY_COUNT = (0x00000000, 0x000000ff, 0x0000ffff, 0x00ffffff, 0xffffffff)


def write_mask(components):
    n = components if components >= 2 else 1
    if n > 4:
        return None
    return MASK_BY_COUNT[n]


def dest_suffix(components):
    """The swizzle the destination printer puts on a mask (notes/29)."""
    m = write_mask(components)
    if m is None:
        return None
    return {0x000000ff: ".x", 0x0000ffff: ".xy",
            0x00ffffff: ".xyz", 0xffffffff: ""}.get(m)
