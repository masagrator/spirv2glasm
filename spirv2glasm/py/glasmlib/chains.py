"""chains.py -- access chains read as GLASM destinations and buffer loads.

An `OpAccessChain` names a place.  Into an OUTPUT it is a destination
(`result.attrib[0].z`, `result.position.w`, `result.patch.tessouter[1]`);
into a uniform or storage block it is a LOAD (`LDC.F32X4 R0, buf0[16];`).
"""
from spvnames import Op, ExecutionModel, StorageClass, BuiltIn, Decoration

from glasmnames import SUFFIX
from glasmlib.common import NotEstablished, ENV, ACCESS_CHAINS, \
    ARRAY_TYPES, SCALAR_TYPES
from glasmlib.types import _glasm_type_code
from glasmlib.semantics import BUILTIN_SLOT, TESC_PATCH_KIND, _stage_kind
from glasmlib.operands import _scalar_value
from glasmlib.blocks import BLOCK_STORAGE, _block_kind


def _output_operand(module, vid, model):
    """A non-fragment interface OUTPUT as a destination operand, or None.

    `result.attrib[0]` and friends: the same binding namespace the ATTRIB and
    OUTPUT declarations use (notes/16), so this asks `binding.py` rather than
    spelling the names again.
    """
    import binding
    ins = module.globals.get(vid)
    if ins is None or ins.operands[2] != StorageClass.Output:
        return None
    loc = module.decoration(vid, Decoration.Location)
    if loc is None:
        return None
    # WHICH OF TESSELLATION CONTROL'S TWO OUTPUT KINDS is the `Patch`
    # decoration, as it is for the `#var` rows (notes/117 §1).
    kind = _stage_kind(model, StorageClass.Output,
                       module.decoration(vid, Decoration.Patch) is not None)
    form = binding.binding_form(kind, loc[0], ".")
    if form is None:
        return None
    text, base, _cull = form
    if text is None:
        return None
    return "%s[%d]" % (text, loc[0]) if base is not None else text


def _colour_output_name(module, vid, model):
    """`result_colorN` for a FRAGMENT Output variable with a Location.

    THE STAGE MATTERS AND USED TO BE IGNORED.  A vertex shader's location
    output is `result.attrib[N]`, not `result_colorN` -- `_output_operand()`
    names it, out of the same binding namespace the OUTPUT declaration
    uses -- and this claimed it first, so `0041_wb2_add.vert` came out storing to
    `result_color0`.  Only the fragment stage has colour outputs.
    """
    if model != ExecutionModel.Fragment:
        return None
    loc = module.decoration(vid, Decoration.Location)
    if loc is None or \
            module.globals[vid].operands[2] != StorageClass.Output:
        return None
    return "result_color%d" % loc[0]


def _output_chain(module, pid, model, by_result):
    """(destination text, component) for a chain into a LOCATION output.

    The `gl_Position` case has its own reader (`_position_chain`); this is the
    same shape for `result.attrib[N]` and friends, and it exists because
    glslang scalarises a swizzled destination in the SPIR-V itself:
    `v.zw = a0.xy` is two `OpAccessChain` + `OpStore` pairs with constant
    indices, not one masked store (`0044_st3_dstswz.vert`).
    """
    ins = by_result.get(pid)
    if ins is None or ins.opcode not in ACCESS_CHAINS:
        return None
    args = ins.args()
    if len(args) != 2:
        return None
    base = _output_operand(module, args[0], model)
    if base is None:
        base = _colour_output_name(module, args[0], model)
    if base is None:
        return None
    idx = _scalar_value(module, args[1])
    if idx is None or not 0 <= int(idx) < 4:
        return None
    return base, int(idx)


def _is_invocation_id(module, vid):
    """Is this value a load of the `gl_InvocationID` built-in?"""
    ins = module.result_insn.get(vid)
    if ins is None or ins.opcode != Op.OpLoad:
        return False
    b = module.decoration(ins.args()[0], Decoration.BuiltIn)
    return b is not None and b[0] == BuiltIn.InvocationId


def _patch_level_chain(module, pid, by_result):
    """`gl_TessLevelOuter[k]` / `gl_TessLevelInner[k]` as its output text.

    notes/63: they are Patch outputs, kind 0xbd, and `binding.py`'s arm names
    them `result.patch.tessouter[k]` / `result.patch.tessinner[k]`.
    """
    import binding
    ch = by_result.get(pid)
    if ch is None or ch.opcode not in ACCESS_CHAINS or len(ch.args()) != 2:
        return None
    var = ch.args()[0]
    gins = module.globals.get(var)
    if gins is None or gins.operands[2] != StorageClass.Output \
            or module.decoration(var, Decoration.Patch) is None:
        return None
    b = module.decoration(var, Decoration.BuiltIn)
    k = _scalar_value(module, ch.args()[1])
    if b is None or k is None:
        return None
    slot = BUILTIN_SLOT.get(TESC_PATCH_KIND, {}).get(b[0])
    if slot is None:
        return None
    form = binding.binding_form(TESC_PATCH_KIND, slot + int(k), ".")
    if form is None or form[0] is None or form[1] is None:
        return None
    return "%s[%d]" % (form[0], slot + int(k) - form[1])


def _chain_insn(module, pid):
    """The body instruction that defines `pid`, or None."""
    for fn in module.functions:
        for i in fn.insns:
            if i.result == pid:
                return i
    return None


def _position_struct(module, var, arrayed):
    """The struct type id of the block `gl_Position` lives in, or None."""
    ptr = module.types.get(var.result_type)
    if ptr is None:
        return None
    sid = ptr.operands[2]
    if arrayed:
        at = module.types.get(sid)
        if at is None or at.opcode != Op.OpTypeArray:
            return None
        sid = at.operands[1]
    st = module.types.get(sid)
    if st is None or st.opcode != Op.OpTypeStruct:
        return None
    return sid


def _position_chain(module, pid):
    """Which component of `gl_Position` this pointer names.

    -1 for the whole vector, 0..3 for one component, None if the pointer is
    not `gl_Position` at all.

    glslang writes `gl_Position` as member 0 of the `gl_PerVertex` block, so
    the store's pointer is an `OpAccessChain` with one constant index into a
    variable whose struct member 0 carries `BuiltIn Position`.
    """
    ins = _chain_insn(module, pid)
    if ins is None or ins.opcode not in ACCESS_CHAINS:
        return None
    args = ins.args()
    var = module.globals.get(args[0]) if args else None
    arrayed = (var is not None and len(args) == 3
               and module.block_array_depth(args[0]) == 1
               and _is_invocation_id(module, args[1]))
    if arrayed:
        # notes/63: `gl_out[gl_InvocationID].gl_Position` in tessellation
        # control -- each invocation writes its own vertex, which prints as
        # plain `result.position`.
        args = [args[0], args[2]]
    if len(args) not in (2, 3):
        return None
    if var is None or var.operands[2] != StorageClass.Output:
        return None
    idx = _scalar_value(module, args[1])
    if idx is None or module.types.get(var.result_type) is None:
        return None
    sid = _position_struct(module, var, arrayed)
    if sid is None:
        return None
    b = module.member_decoration(sid, int(idx), Decoration.BuiltIn)
    if b is None or b[0] != BuiltIn.Position:
        return None
    if len(args) == 2:
        return -1                                 # the whole vector
    comp = _scalar_value(module, args[2])
    if comp is None or not 0 <= int(comp) < 4:
        return None
    return int(comp)


# A LOAD OUT OF A UNIFORM OR STORAGE BLOCK.
#
#     LDC.F32X4 R0, buf0[0];          a uniform block
#     LDB.F32X4 R0, sbo_buf0[0];      a storage block
#
# The declaration names (`buf%d`, `sbo_buf%d`) are the ones `declarations()`
# already emits, the index is a BYTE offset, and `LDC` against `LDB` is the
# operand's binding kind and not the opcode (notes/30).
#
# THE SUFFIX AND THE MASK, read (notes/70):
#   * the load node is built by `f_7100f09328(cg, 0x3b, type, count, ...)`
#     (lr 0xf093cc in `g2s_trace_setop`), and 0xf093e8..0xf0940c write
#     `node[48] = table[0x11be1c0][count]` -- the SAME count table as
#     notes/41, so a scalar load writes `.x`, a vec2 `.xy`, a vec3 `.xyz`;
#   * the mnemonic builder `f_7100bd61f4` appends the WIDTH from that mask
#     (0xbd694c / 0xbd6cec): `X4` when byte 2 or 3 (z, w) is enabled, else
#     `X2` when byte 1 (y) is, else nothing -- which is why a vec3 is
#     `LDC.F32X4 R0.xyz` (`0070_ld_v3.frag`) and a scalar `LDC.F32 R0.x`;
#   * the type part is the node type's spelling in the image's long suffix
#     table (notes/37, `type_suffix.json`) without its dot: F32, S32, U32,
#     U64.  Measured on `ld_f1/ld_i1/ld_u1.frag` and every LDC/LDB line of
#     the saved listings; the earlier `_LOAD_SUFFIX` table was fitted and
#     spelled a signed load `U32`.

def _load_suffix(module, tid, count):
    """`F32X4`-style load suffix for `count` components of type `tid`, or
    None when the image's tables do not spell it."""
    try:
        code = _glasm_type_code(module, tid)
    except NotEstablished:
        return None
    spell = SUFFIX.get(code)
    if spell is None or not spell.startswith(".") or "?" in spell:
        return None
    if count >= 3:
        width = "X4"
    elif count == 2:
        width = "X2"
    else:
        width = ""
    return spell[1:] + width


class _Walk(object):
    """Where a buffer chain's constant walk has got to."""

    def __init__(self, tid):
        self.tid = tid
        self.off = 0            # the constant byte offset so far
        self.dyn = None         # (index id, stride), or two of them
        self.comp = None        # the component of a vector member read
        self.mstride = None     # the MatrixStride of the member walked into
        self.rowmajor = False   # ... and whether it is RowMajor
        self.matrix = False     # the walk took a column of a matrix


def _dynamic_step(module, walk, a):
    """ONE DYNAMIC INDEX is allowed, and only into an array: the byte offset
    becomes `index * stride` in a register and the load indexes the buffer by
    it (`0052_if_arr.vert`).  A dynamic index into a struct has no stride to scale
    by, so it still refuses.  TWO are allowed since notes/89: a structured
    buffer's `buf[i].value[j]` scales both and adds them (`0089_sb_b.frag`), and
    `dyn` is then the list of both, outermost first.  False to refuse."""
    t = module.types.get(walk.tid)
    if t is None or t.opcode not in ARRAY_TYPES:
        return False
    if isinstance(walk.dyn, list) or (walk.dyn is not None
                                      and ENV.get("G2S_ONEDYN")):
        return False
    stride = module.decoration(walk.tid, Decoration.ArrayStride)
    if stride is None:
        return False
    walk.dyn = ((a, stride[0]) if walk.dyn is None
                else [walk.dyn, (a, stride[0])])
    walk.tid = t.args()[0]
    return True


def _constant_step(module, walk, idx):
    """A constant index: a struct member's offset or an array element's.
    False to refuse."""
    t = module.types.get(walk.tid)
    if t is None:
        return False
    if t.opcode == Op.OpTypeStruct:
        mo = module.member_decoration(walk.tid, idx, Decoration.Offset)
        if mo is None:
            return False
        _ms = module.member_decoration(walk.tid, idx, Decoration.MatrixStride)
        walk.mstride = _ms[0] if _ms is not None else None
        walk.rowmajor = module.member_decoration(
            walk.tid, idx, Decoration.RowMajor) is not None
        walk.off += mo[0]
        walk.tid = t.args()[idx]
    elif (t.opcode == Op.OpTypeMatrix and walk.mstride is not None
          and not walk.rowmajor and not ENV.get("G2S_NOMATCOLUMN")):
        # A COLUMN OF A COLUMN-MAJOR MATRIX is `MatrixStride` bytes on
        # (the member's decoration, carried through any array of matrices):
        # `0103_ld_mx.vert`'s `m[1].y`, m at 16 with stride 16, prints `LDC.F32X2
        # R0.y, buf0[32];` -- the column's load read at its component, as a
        # vector member's is (notes/104 §7).  A row-major matrix's column
        # is not contiguous and is refused.
        walk.off += idx * walk.mstride
        walk.tid = t.args()[0]
        walk.matrix = True
    elif t.opcode in ARRAY_TYPES:
        stride = module.decoration(walk.tid, Decoration.ArrayStride)
        if stride is None:
            return False
        walk.off += idx * stride[0]
        walk.tid = t.args()[0]
    else:
        return False
    return True


def _walk_chain(module, args, tid):
    """The chain's indices applied to the block type, or None to refuse."""
    walk = _Walk(tid)
    for n_, a in enumerate(args[1:], 1):
        v = _scalar_value(module, a)
        _t0 = module.types.get(walk.tid)
        if (v is not None and _t0 is not None
                and _t0.opcode == Op.OpTypeVector
                and n_ == len(args) - 1 and 0 <= int(v) < _t0.args()[1]):
            # A COMPONENT OF A VECTOR MEMBER is the member's load read
            # through that component (notes/76): `v.z` prints `LDC.F32X4
            # R0.z, buf0[16];` (`0076_pl_c.vert`), the mask narrowed to the
            # component by `_narrow_loads`.
            walk.comp = int(v)
            break
        ok = (_dynamic_step(module, walk, a) if v is None
              else _constant_step(module, walk, int(v)))
        if not ok:
            return None
    return walk


def _shared_size(module, tid):
    """The size in bytes of a Workgroup type, laid out naturally.

    A Workgroup struct carries no `Offset` and its arrays no `ArrayStride`
    -- those decorations are for BLOCKS -- so the strides come from the
    types: a 32-bit scalar is 4, a vector is its count, an array is its
    length times its element, a struct the sum of its members.  That is
    what `post_tonemap_update.comp`'s `shared_mem[R0.x + 256]` needs (its
    element is one uint, so 256 bytes is element 64) and what its
    `SHARED_MEMORY 512;` states for 128 of them.  (notes/124 §2)
    """
    t = module.types.get(tid)
    if t is None:
        return None
    if t.opcode in SCALAR_TYPES:
        return 4
    if t.opcode == Op.OpTypeVector:
        _e = _shared_size(module, t.args()[0])
        return None if _e is None else _e * t.args()[1]
    if t.opcode in ARRAY_TYPES:
        _e = _shared_size(module, t.args()[0])
        _n = module.constants.get(t.args()[1]) if len(t.args()) > 1 else None
        if _e is None or _n is None:
            return None
        return _e * int(_n.args()[-1])
    if t.opcode == Op.OpTypeStruct:
        _s = 0
        for _m in t.args():
            _e = _shared_size(module, _m)
            if _e is None:
                return None
            _s += _e
        return _s
    return None


def _walk_shared(module, args, tid):
    """`_walk_chain` for shared memory, with the natural layout above."""
    walk = _Walk(tid)
    for n_, a in enumerate(args[1:], 1):
        v = _scalar_value(module, a)
        t = module.types.get(walk.tid)
        if t is None:
            return None
        if (v is not None and t.opcode == Op.OpTypeVector
                and n_ == len(args) - 1 and 0 <= int(v) < t.args()[1]):
            walk.comp = int(v)
            break
        if t.opcode == Op.OpTypeStruct:
            if v is None:
                return None
            _off = 0
            for _m in t.args()[:int(v)]:
                _e = _shared_size(module, _m)
                if _e is None:
                    return None
                _off += _e
            walk.off += _off
            walk.tid = t.args()[int(v)]
            continue
        if t.opcode not in ARRAY_TYPES:
            return None
        _st = _shared_size(module, t.args()[0])
        if _st is None:
            return None
        if v is None:
            if isinstance(walk.dyn, list):
                return None
            walk.dyn = ((a, _st) if walk.dyn is None
                        else [walk.dyn, (a, _st)])
        else:
            walk.off += int(v) * _st
        walk.tid = t.args()[0]
    return walk


def _loaded_count(module, tid):
    """How many components one load line of type `tid` fetches, or None."""
    t = module.types.get(tid)
    if t is None:
        return None
    if t.opcode == Op.OpTypeVector:
        return t.args()[1]
    if t.opcode == Op.OpTypeMatrix:
        # A MATRIX IS LOADED A COLUMN AT A TIME, so the suffix is the
        # COLUMN's: `0000_if_mat.vert` loads `mat4 m` as four `LDC.F32X4`.  The
        # type returned is still the matrix's, which is how the caller knows
        # one line will not do.
        col = module.types.get(t.args()[0])
        if col is None or col.opcode != Op.OpTypeVector:
            return None
        return col.args()[1]
    if t.opcode in SCALAR_TYPES:
        return 1
    return None


def _buffer_chain(module, pid, insns_by_result):
    """(mnemonic, declaration name, byte offset, loaded type, dynamic
    indices, component) for a chain into a block, or None.

    The constant part of the chain folds into the byte offset; the dynamic
    part (at most two array indices) is returned for the load to scale.
    """
    ins = insns_by_result.get(pid)
    if ins is None or ins.opcode not in ACCESS_CHAINS:
        return None
    args = ins.args()
    var = module.globals.get(args[0])
    if var is None:
        return None
    storage = var.operands[2]
    # SHARED MEMORY WALKS THE SAME WAY, and prints `LDS`/`STS` against
    # `shared_mem` instead of `LDB`/`STB` against `sbo_buf<n>`:
    # `post_tonemap_update.comp` has `LDS.U32 R2.x, shared_mem[R0.x + 256];`
    # and `STS.U32 R1, shared_mem[R4.x];` -- the same address the block
    # forms build, with no binding number because there is one shared
    # region.  (notes/124 §2)
    if storage == StorageClass.Workgroup:
        ptr = module.types.get(var.result_type)
        walk = _walk_shared(module, args, ptr.operands[2])
        if walk is None:
            return None
        count = _loaded_count(module, walk.tid)
        suffix = _load_suffix(module, walk.tid, count) if count else None
        if suffix is None:
            return None
        return ("LDS.%s" % suffix, "shared_mem", walk.off, walk.tid,
                walk.dyn, walk.comp)
    if storage not in BLOCK_STORAGE:
        return None
    binding_ = module.decoration(args[0], Decoration.Binding)
    if binding_ is None:
        return None
    ptr = module.types.get(var.result_type)
    walk = _walk_chain(module, args, ptr.operands[2])
    if walk is None:
        return None
    count = _loaded_count(module, walk.tid)
    if count is None:
        return None
    suffix = _load_suffix(module, walk.tid, count)
    if suffix is None:
        return None
    kind, _reg = _block_kind(module, args[0], storage, ptr.operands[2])
    if kind == "SBO_BUFFER":
        return ("LDB.%s" % suffix, "sbo_buf%d" % binding_[0], walk.off,
                walk.tid, walk.dyn, walk.comp)
    return ("LDC.%s" % suffix, "buf%d" % binding_[0], walk.off, walk.tid,
            walk.dyn, walk.comp)


def _buffer_chain_via_matrix(module, pid, insns_by_result):
    """Does the block chain `pid` take a COLUMN OF A MATRIX (`m[1]`,
    `m[1].y`)?  The walk `_buffer_chain` folds into an offset, asked again
    for that one fact (notes/104 §7)."""
    ins = insns_by_result.get(pid)
    if ins is None or ins.opcode not in ACCESS_CHAINS:
        return False
    var = module.globals.get(ins.args()[0])
    if var is None or var.operands[2] not in BLOCK_STORAGE:
        return False
    ptr = module.types.get(var.result_type)
    walk = _walk_chain(module, ins.args(), ptr.operands[2])
    return walk is not None and walk.matrix
