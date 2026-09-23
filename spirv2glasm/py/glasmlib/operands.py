"""operands.py -- constants and interface variables as GLASM source text.

`{1, 0, 0.5, 1}`, `{2, 0, 0, 0}.x`, `vertex.attrib[0]`, `fragment.position`,
`vertex.vertexIndex`: the operand printer's spellings for what a SPIR-V id
names when it is not a register.
"""
import struct

from spvnames import Op, StorageClass, Decoration

from glasmlib.common import ACCESS_CHAINS
from glasmlib.semantics import BUILTIN_SLOT, GEOMETRY_INPUT_KIND, \
    COMPUTE_INPUT_KIND, _stage_kind
from glasmlib.text import _swizzle_suffix

# The binding kinds whose inputs the instruction printer names itself
# (py/opname.py): vertex inputs, and the two tessellation input kinds.
_VERTEX_INPUT_KIND = 0x07
_TESC_INPUT_KIND = 0x33
_TESE_INPUT_KIND = 0x35
_UINT_SIGN_BIT = 0x80000000


def _float_literal(bits):
    """A float constant the way the operand printer spells it.

    `{1, 0, 0.5, 1}` -- no trailing `.0` on a whole number, and the shortest
    representation that round-trips otherwise.  Measured on the constant
    probes; the printer's own format is `%g`-like.
    """
    v = struct.unpack("<f", struct.pack("<I", bits & 0xffffffff))[0]
    if v == int(v) and abs(v) < 1e16:
        # NEGATIVE ZERO KEEPS ITS SIGN, as `%g` prints it: the whole-number
        # path goes through an integer, which drops it.
        # `minimap_29ccc3e6.frag` stores `{-0, 0, 0, 0}`.
        if v == 0 and bits & _UINT_SIGN_BIT:
            return "-0"
        return "%d" % int(v)
    t = repr(float("%.9g" % v))
    return t[:-2] if t.endswith(".0") else t


def _signed(v, width):
    v &= (1 << width) - 1
    return v - (1 << width) if v >> (width - 1) else v


def _int_literal(t, word):
    """THE IMMEDIATE PRINTER (f_710003dc10, 0x3dd80..0x3dda8): a value with
    bit 31 set prints `0x%x` when the type is unsigned (f_7100056a80) and
    `%d` -- negative -- otherwise; every other value prints `%d`.
    `0088_ff_a.frag`: `{0xffffffff, 0, 0, 0}`; the ImmediateConstBuffer's
    `{1065353216, ..}`."""
    width, signed = t.args()[0], t.args()[1]
    if not signed and word & _UINT_SIGN_BIT and width == 32:
        return "0x%x" % word
    return "%d" % (word if not signed else _signed(word, width))


def _scalar_value(module, cid):
    """The literal of a scalar constant id, or None."""
    ins = module.constants.get(cid)
    if ins is None:
        return None
    if ins.opcode == Op.OpConstantTrue:
        return "1"
    if ins.opcode == Op.OpConstantFalse:
        return "0"
    if ins.opcode != Op.OpConstant:
        return None
    t = module.types.get(ins.result_type)
    if t is None:
        return None
    if t.opcode == Op.OpTypeFloat:
        return _float_literal(ins.args()[0])
    if t.opcode == Op.OpTypeInt:
        return _int_literal(t, ins.args()[0])
    return None


def _constant_operand(module, cid):
    """A constant id as a GLASM source operand, or None.

    A vector constant prints all four components in braces; a scalar one
    prints the same braces with the value in the first slot, which is what
    `0074_w1_mov.vert`'s `{0, 0, 0, 0}` is.
    """
    ins = module.constants.get(cid)
    if ins is None:
        return None
    if ins.opcode == Op.OpConstantComposite:
        parts = [_scalar_value(module, a) for a in ins.args()]
        if any(x is None for x in parts):
            return None
        parts = (parts + ["0", "0", "0", "0"])[:4]
        return "{%s}" % ", ".join(parts)
    v = _scalar_value(module, cid)
    return None if v is None else "{%s, 0, 0, 0}" % v


def _constant_source(module, cid, nres):
    """A constant id as a SOURCE operand, broadcast when it has to be.

    A VECTOR constant prints bare.  A SCALAR one prints the padded literal
    and, when the instruction's RESULT is wider than one component, a `.x`
    that says every component reads slot 0:

        0041_k1_addc.vert  ADD.F32 R0,   vertex.attrib[0], {1, 2, 3, 4};
        0041_k2_mulc.vert  MUL.F32 R0,   vertex.attrib[0], {2, 0, 0, 0}.x;
        0026_cf_if.vert    SGT.F32 R0.x, vertex.attrib[0], {0, 0, 0, 0};

    The third is why `nres` is a parameter and not an assumption: a
    scalar-result comparison against a scalar constant prints NO swizzle, so
    "a scalar constant always broadcasts" is wrong and cost three probes when
    it was tried.
    """
    text = _constant_operand(module, cid)
    if text is None:
        return None
    ins = module.constants.get(cid)
    if ins.opcode == Op.OpConstantComposite:
        return text
    # A scalar constant reads slot 0 for every component, i.e. the selector
    # (0,0,0,0) -- so `_swizzle_suffix` gives exactly the measured answer:
    # nothing when the result is one component wide, `.x` when it is wider.
    return text + _swizzle_suffix((0,), nres)


def _type_insn(module, tid):
    """The instruction that declares type `tid`, or None."""
    ins = module.globals.get(tid)
    if ins is None and hasattr(module, "type_insn"):
        ins = module.type_insn(tid)
    if ins is None:
        for i in module.insns:
            if i.result == tid:
                return i
    return ins


def _type_width(module, tid):
    """The total bit width of a type, or None when it is not a plain one.

    A bitcast is only a reinterpretation when nothing moves, and that is what
    this checks.  Scalars give their own width; a vector gives its component
    width times its count.  Anything else -- a pointer, a struct, an array --
    returns None and the caller refuses.
    """
    ins = _type_insn(module, tid)
    if ins is None:
        return None
    if ins.opcode in (Op.OpTypeInt, Op.OpTypeFloat):
        return int(ins.args()[0])
    if ins.opcode == Op.OpTypeVector:
        w = _type_width(module, ins.args()[0])
        return None if w is None else w * int(ins.args()[1])
    return None


def _instruction_printer_input(kind, slot):
    """A built-in input the DECLARATION printer has no arm for, named by the
    INSTRUCTION printer's own table (`opname.py`), or None.

    `binding.py` is `f_7100bd4810`, the declaration printer, and its kind-7
    arm stops at slot 0x1f (0xbd4938) -- a built-in input has no ATTRIB line.
    The instruction printer's table, indexed by the same slot (0xbd0e20), is
    `opname.py`: 66 of 66 arms decoded, with 0x3e and 0x3f coming out as
    `vertex.vertexIndex` and `vertex.instanceIndex` -- exactly the slots
    `BUILTIN_SLOT[0x07]` gives VertexIndex and InstanceIndex."""
    import opname
    if kind == _VERTEX_INPUT_KIND:
        text = opname.VERTEX_IN.get(slot)
        if text is not None and "%d" not in text:
            return text
    if kind == COMPUTE_INPUT_KIND:
        # notes/111: the compute stage's arm (0xbd0f30), `invocation.*`
        text = opname.COMPUTE_IN.get(slot)
        if text is not None and "%d" not in text:
            return text
    if kind in (_TESC_INPUT_KIND, _TESE_INPUT_KIND):
        # notes/62-63: the tessellation stages' own arms of the instruction
        # printer; a non-arrayed built-in (`gl_TessCoord`, slot 0x3c ->
        # `vertex.tesscoord`; `gl_InvocationID`, slot 0x3b ->
        # `primitive.invocation`) takes no vertex index.
        ent = (opname.PER_VERTEX_IN_35 if kind == _TESE_INPUT_KIND
               else opname.PER_VERTEX_IN_33).get(slot)
        if ent is not None and "%d" not in ent[0]:
            return ent[0]
    return None


def _block_member_operand(module, vid, idx, model):
    """`(operand, components)` for a MEMBER of an input interface BLOCK.

    A block's members each carry their own Location (notes/114 \u00a746), so
    a member IS the attribute at its own location -- the same string
    `_interface_operand` builds for a plain located variable.  Returns None
    for anything else, including a block with no per-member Location.
    """
    import binding
    if idx is None:
        return None
    ins = module.globals.get(vid)
    if ins is None or ins.operands[2] != StorageClass.Input:
        return None
    pt = module.types.get(ins.result_type)
    if pt is None or not pt.operands:
        return None
    sid = pt.operands[-1]
    st = module.types.get(sid)
    if st is None or st.opcode != Op.OpTypeStruct:
        return None
    idx = int(idx)
    if not 0 <= idx < len(st.args()):
        return None
    loc = module.member_decoration(sid, idx, Decoration.Location)
    if loc is None:
        return None
    kind = _stage_kind(model, StorageClass.Input)
    form = binding.binding_form(kind, loc[0], ".")
    if form is None or form[0] is None or form[1] is None:
        return None
    return ("%s[%d]" % (form[0], loc[0]), st.args()[idx])


def _interface_operand(module, vid, model):
    """An interface variable as a GLASM source operand, or None.

    The names are the binding namespace of notes/16 -- `fragment.attrib[0]`,
    `vertex.attrib[0]`, `fragment.position` -- and they are the same strings
    the ATTRIB line declares, which is why this defers to `binding.py` rather
    than spelling them again.
    """
    import binding
    ins = module.globals.get(vid)
    if ins is None or ins.operands[2] != StorageClass.Input:
        return None
    loc = module.decoration(vid, Decoration.Location)
    kind = _stage_kind(model, StorageClass.Input)
    if loc is not None:
        form = binding.binding_form(kind, loc[0], ".")
        if form is None or form[0] is None:
            return None
        return "%s[%d]" % (form[0], loc[0]) if form[1] is not None \
            else form[0]
    bi = module.decoration(vid, Decoration.BuiltIn)
    if bi is None:
        return None
    slot = BUILTIN_SLOT.get(kind, {}).get(bi[0])
    if slot is None:
        return None
    form = binding.binding_form(kind, slot, ".")
    if form is None:
        return _instruction_printer_input(kind, slot)
    text, base, _cull = form
    if text is None:
        return None
    return "%s[%d]" % (text, slot - base) if base is not None else text


def _per_vertex_location_operand(module, pid, model, by_result):
    """A USER-DECLARED PER-VERTEX INPUT ARRAY at a constant vertex index:
    `(vertex[k].attrib[loc], component or None)`, or None.

    `in vec4 v[]` in a tessellation or geometry stage is an ARRAY, so a read
    of `v[k]` is an access chain whose FIRST index is the vertex, not a
    component.  The printer's per-vertex arm formats it exactly as it does a
    built-in member (`opname.PER_VERTEX_IN_33`/`_35`, notes/62), with the
    location as the slot: `layout(location=0) in vec4 v[]` read at `v[1]`
    prints `vertex[1].attrib[0]`, and a component of it carries the swizzle
    (`vertex[0].attrib[0].y`).  The ATTRIB declaration is unchanged
    (`vertex_attrib[] = { vertex.attrib[0..0] }`).
    """
    import opname
    ch = by_result.get(pid)
    if ch is None or ch.opcode not in ACCESS_CHAINS:
        return None
    args = ch.args()
    if len(args) not in (2, 3):
        return None
    var = args[0]
    gins = module.globals.get(var)
    if gins is None or gins.operands[2] != StorageClass.Input:
        return None
    kind = _stage_kind(model, StorageClass.Input)
    if kind not in (GEOMETRY_INPUT_KIND, _TESC_INPUT_KIND, _TESE_INPUT_KIND):
        return None
    loc = module.decoration(var, Decoration.Location)
    if loc is None or module.block_array_depth(var) != 1:
        return None
    k = _scalar_value(module, args[1])
    if k is None:
        return None
    table = (opname.PER_VERTEX_IN_35 if kind == _TESE_INPUT_KIND
             else opname.PER_VERTEX_IN_33)
    ent = table.get(loc[0])
    if ent is None:
        return None
    fmt, sub = ent
    if fmt.count("%d") != 2:
        return None
    base = fmt % (int(k), loc[0] - (sub or 0))
    if len(args) == 2:
        return (base, None)
    c = _scalar_value(module, args[2])
    if c is None or not 0 <= int(c) < 4:
        return None
    return (base, int(c))


def _per_vertex_dynamic_location_operand(module, pid, model, by_result):
    """A PER-VERTEX USER INPUT read at a DYNAMIC vertex index, as
    (alias, index id, location), or None.

    `water_00540147.tesc` reads `vs_NORMAL0[gl_InvocationID]` and prints

        MOV.S R1.x, primitive.invocation;
        MOV.F R18.xyz, vertex_attrib[R1.x][1];

    -- the index carried into a register, then the element taken out of the
    ATTRIB declaration's alias, which for a user input is the RANGED form
    (`ATTRIB vertex_attrib[] = { vertex.attrib[0..9] };`) and so carries a
    SECOND index, the location.  `_per_vertex_operand` above takes the
    built-in members of `gl_in[]`, whose alias is a single binding and has
    no second index.  (notes/130)
    """
    import os as _os
    if _os.environ.get("G2S_NOPVDYNLOC"):
        return None
    ch = by_result.get(pid)
    if ch is None or ch.opcode not in ACCESS_CHAINS or len(ch.args()) != 2:
        return None
    args = ch.args()
    gins = module.globals.get(args[0])
    if gins is None or gins.operands[2] != StorageClass.Input:
        return None
    if _scalar_value(module, args[1]) is not None:
        return None                     # a constant index is another shape
    loc = module.decoration(args[0], Decoration.Location)
    if loc is None:
        return None
    kind = _stage_kind(model, StorageClass.Input)
    if kind not in (GEOMETRY_INPUT_KIND, _TESC_INPUT_KIND, _TESE_INPUT_KIND):
        return None
    import binding
    form = binding.binding_form(kind, loc[0], "_")
    if form is None or form[0] is None or form[1] is None:
        return None
    return (form[0], args[1], loc[0] - form[1])


def _per_vertex_operand(module, pid, model, by_result):
    """`gl_in[k].member` in the tessellation or geometry stages, or None.

    notes/62: the printer's kind-0x35 arm takes the VERTEX index from the
    operand's bits 8..15 and the member's slot from its low byte, and formats
    `PER_VERTEX_IN_35[slot]` with (vertex, slot - sub).  A constant vertex
    index into a built-in member of the arrayed block is taken for kinds
    0x33/0x35; a register index (notes/63, notes/65) for 0x30 as well.
    """
    import opname
    ch = by_result.get(pid)
    if ch is None or ch.opcode not in ACCESS_CHAINS:
        return None
    args = ch.args()
    var = args[0]
    gins = module.globals.get(var)
    if gins is None or gins.operands[2] != StorageClass.Input:
        return None
    kind = _stage_kind(model, StorageClass.Input)
    if kind not in (GEOMETRY_INPUT_KIND, _TESC_INPUT_KIND, _TESE_INPUT_KIND) \
            or len(args) != 3 or module.block_array_depth(var) != 1:
        return None
    k = _scalar_value(module, args[1])
    m = _scalar_value(module, args[2])
    if m is None:
        return None
    bi = dict(module.member_builtins(var)).get(int(m))
    if bi is None:
        return None
    slot = BUILTIN_SLOT.get(kind, {}).get(bi)
    if slot is None:
        return None
    if kind == GEOMETRY_INPUT_KIND and k is not None:
        # The geometry input kind's CONSTANT-vertex operand goes through the
        # instruction printer's kind-0x30 arm, which is not decoded; only the
        # register-indexed form below (the ATTRIB alias) is taken.
        return None
    if kind != GEOMETRY_INPUT_KIND:
        table = (opname.PER_VERTEX_IN_35 if kind == _TESE_INPUT_KIND
                 else opname.PER_VERTEX_IN_33)
        ent = table.get(slot)
        if ent is None:
            return None
    if k is None:
        # A DYNAMIC vertex index (notes/63): the operand is the ATTRIB
        # declaration's alias, indexed by a register -- `0007_ts_ctrl.tesc`
        # prints `vertex_position[R0.x]`.  Returned as (alias, index id) for
        # the load to build.
        import binding
        form = binding.binding_form(kind, slot, "_")
        if form is None or form[0] is None or form[1] is not None:
            return None
        return (form[0], args[1])
    fmt, sub = ent
    vals = (int(k), slot - (sub or 0))
    return fmt % vals[:fmt.count("%d")]
