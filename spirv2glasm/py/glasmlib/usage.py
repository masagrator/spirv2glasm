"""usage.py -- which ids, members and elements a shader body touches.

The `#var` line's `used` column, the ATTRIB/OUTPUT block's gate and the
local-memory symbols all ask the same question of the module: is this id (or
this member, this element) mentioned by a function body.  The answers live
here.
"""
from spvnames import Op, StorageClass, Decoration

from glasmlib.common import ENV, NotEstablished, ACCESS_CHAINS, ARRAY_TYPES, \
    KILLS
from glasmlib.types import type_spelling

# Operands that are LITERALS, not ids, and so are not uses of anything.  The
# value is the first argument index from which the operands stop being ids.
# Without this, `OpExtInst %float %1 Pow %a %b` counts 26 (the GLSL.std.450
# instruction number for Pow) as a reference to `%26`, which in a vertex module
# is `gl_InstanceID` -- and a built-in the shader never mentions then looks
# used.
_LITERAL_TAIL = {
    Op.OpVectorShuffle: 2,       # the component selectors
    Op.OpCompositeExtract: 1,    # the indexes
    Op.OpCompositeInsert: 2,     # the indexes
}
_LITERAL_AT = {
    Op.OpExtInst: (1,),          # the instruction number (the set IS an id)
}


def _id_args(ins):
    """The operands of an instruction that are ids."""
    a = ins.args()
    tail = _LITERAL_TAIL.get(ins.opcode)
    if tail is not None:
        a = a[:tail]
    skip = _LITERAL_AT.get(ins.opcode)
    if skip:
        a = [x for i, x in enumerate(a) if i not in skip]
    return a


# Instructions that are live because of what they DO rather than what they
# produce: control-flow decisions, calls, barriers, discards, image writes and
# geometry emission.  A store is deliberately NOT here -- whether a store
# matters depends on whether anything ever reads what it wrote, which is the
# whole point of the walk in refliveness.py.
_SIDE_EFFECT = frozenset((
    Op.OpImageWrite,
    Op.OpEmitVertex, Op.OpEndPrimitive,
    Op.OpEmitStreamVertex, Op.OpEndStreamPrimitive,
    Op.OpControlBarrier, Op.OpMemoryBarrier,
    Op.OpAtomicLoad, Op.OpAtomicStore, Op.OpAtomicExchange,
    Op.OpAtomicCompareExchange, Op.OpAtomicCompareExchangeWeak,
    Op.OpAtomicIIncrement, Op.OpAtomicIDecrement, Op.OpAtomicIAdd,
    Op.OpAtomicISub, Op.OpAtomicSMin, Op.OpAtomicUMin, Op.OpAtomicSMax,
    Op.OpAtomicUMax, Op.OpAtomicAnd, Op.OpAtomicOr, Op.OpAtomicXor,
    Op.OpKill,
    Op.OpBranchConditional, Op.OpSwitch,
))


def _pointer_base(module, pid, defs):
    """The module- or function-scope variable a pointer id ultimately names."""
    seen = set()
    while pid in defs and pid not in seen:
        seen.add(pid)
        ins = defs[pid]
        if ins.opcode in ACCESS_CHAINS:
            pid = ins.args()[0]
            continue
        break
    return pid


# THE `used` GATE IS A MENTION COUNT, AND THE LIVENESS WALK IS NOT USED.
#
# notes/18 built `refliveness.live_ids` -- a reaching-definition fixpoint with
# per-callee summaries -- on the strength of one shader,
# `branches_depthprepass_vs_nowind_unrolledinput.vert`, which it said the
# compiler prints as `vertex.attrib[0..1]` where a mention count gives
# `vertex.attrib[0..6]`.  The oracle prints the opposite: all seven of that
# shader's attributes carry `: 1` and the range IS `0..6`.  The reading was
# inverted, and the fixpoint was built to reproduce an error.
#
# Measured before changing it, over 3,442 corpus shaders (every third one):
# the two gates give a DIFFERENT declaration block on exactly one shader, and
# it is that one -- where the mention count agrees with the oracle and the
# fixpoint does not.  Over 1,721 shaders the `#semantic` and `#var` blocks are
# identical under both.  So the fixpoint costs a fixpoint per shader and buys
# a wrong answer on the single shader where it has an opinion.
def _mentioned(module):
    """Every id some function body names as an OPERAND, literals excluded.

    This is the `used` gate.  `_referenced` did the same thing without the
    literal filter, and that is the whole reason it was ever thought wrong:
    `OpExtInst %f %glsl Pow %a %b` carries the GLSL.std.450 instruction number
    26 in an operand slot, and 26 is `gl_InstanceID` in a glslang vertex
    module, so an unfiltered count marked a built-in the shader never mentions.
    `_id_args` is the filter the liveness walk already used (notes/18).
    """
    seen = set()
    for fn in module.functions:
        for ins in fn.insns:
            seen.update(_id_args(ins))
    return seen


def _live_ids(module):
    """The ids some function body mentions -- the `used` column's basis."""
    c = getattr(module, "_g2s_live", None)
    if c is None:
        c = _mentioned(module)
        try:
            module._g2s_live = c
        except AttributeError:
            pass                      # a __slots__ Module: recompute each time
    return c


def _ptr_key(module, pid, defs):
    """A (base, constant index path) key for a pointer, or (base, None).

    The reaching-definition walk has to distinguish ELEMENTS: a translated
    shader writes three components of a local array through three access chains
    and reads them back later, and collapsing them to the base variable makes
    two of the three stores look dead.  A chain with any non-constant index
    gets `None` for the path, which the caller treats as "could be anything".
    """
    path = []
    exact = True
    while pid in defs:
        ins = defs[pid]
        if ins.opcode not in ACCESS_CHAINS:
            break
        for i in reversed(ins.args()[1:]):
            c = module.constants.get(i)
            if c is None or c.opcode != Op.OpConstant:
                exact = False
            else:
                path.append(c.args()[0])
        pid = ins.args()[0]
    return (pid, tuple(reversed(path)) if exact else None)


def _referenced(module, ids):
    """Which ids any function body mentions -- the `used` column's basis.

    The last field of a `#var` line is 1 or 0 and tracks whether the variable
    is actually touched: `gl_PointSize` reads 0 in a shader that never writes
    it and 1 in one that does.  Reachability is approximated by "the id appears
    as an operand somewhere in a function", which is what a front end that does
    no dead-code elimination at -O0 would also conclude.
    """
    seen = set()
    for fn in module.functions:
        for ins in fn.insns:
            for w in ins.operands:
                if w in ids:
                    seen.add(w)
    return seen


def _use_count(module, vid):
    """How many operand slots in the module's functions read `vid`."""
    n = 0
    in_fn = False
    for ins in module.insns:
        if ins.opcode == Op.OpFunction:
            in_fn = True
            continue
        if ins.opcode == Op.OpFunctionEnd:
            in_fn = False
            continue
        if in_fn:
            n += list(_id_args(ins)).count(vid)
    return n


def _uses_builtin(module, builtin):
    """Does the module reference a variable carrying this BuiltIn decoration?"""
    for vid in module.globals:
        b = module.decoration(vid, Decoration.BuiltIn)
        if b and b[0] == builtin:
            return bool(_referenced(module, {vid}))
    return False


def _unreferenced_block(module, vid, kind):
    """Is this uniform block never referenced by any function?

    Such a block has NO binding in the listing (`0072_ce_n29.vert`, `chr_eye`
    cut before its shadow matrix is read):

        #semantic cb_shadow.__defaultname_430 : BUFFER[-1]
        #var float4 __defaultname_430.hlslcc_mtx4x4shadowViewProj_g[0] :  :  : -1 : 0

    and no `CBUFFER` line.  Only the wholly unreferenced case is taken --
    a reference the front end later drops is `_member_uses`' known gap --
    and only for a UNIFORM block: an unreferenced storage block has not
    been seen, so it is refused."""
    for fn in module.functions:
        for ins in fn.insns:
            if vid in ins.args():
                return False
    if kind != "BUFFER":
        raise NotEstablished("an unreferenced storage block: its listing "
                             "form has not been seen")
    return True


def _constant_index(module, cid):
    """The literal behind a constant index operand, or None when it is not an
    OpConstant (a dynamic index)."""
    c = module.constants.get(cid)
    if c is None or c.opcode != Op.OpConstant:
        return None
    return c.args()[0]


def _chains_into(module, var_id):
    """Every access chain whose base is `var_id`, in body order."""
    for fn in module.functions:
        for ins in fn.insns:
            if ins.opcode not in ACCESS_CHAINS:
                continue
            args = ins.args()
            if args and args[0] == var_id:
                yield ins


def _member_uses(module, var_id):
    """Which members of a block variable the body actually touches.

    The `used` column is PER MEMBER, not per variable: a shader that writes
    only `gl_Position` gets 1 on that line and 0 on `gl_PointSize`,
    `gl_ClipDistance` and `gl_CullDistance`, all four of which belong to the
    same `gl_PerVertex` variable.  A member is reached through an access chain
    whose base is the variable and whose FIRST index is a constant -- that
    index is the member number.

    KNOWN WRONG IN ONE CASE, kept here rather than papered over.  In
    `map_1465b18f.frag` the module has a fully static access chain into member
    0 of a uniform block (`hlslcc_mtx4x4view_g`), the chain's result IS
    consumed, and the compiler still prints `: 0`.  So "referenced in the
    module" is not what the column means -- the front end has decided the
    reference does not survive, and reproducing that needs whatever analysis it
    runs, which is unread.  `tools/compare.py` reports this as the one
    `DIFFERS` over 120 corpus shaders; it is a named defect, not a silent one.
    """
    live = _live_ids(module)
    out = set()
    for ins in _chains_into(module, var_id):
        args = ins.args()
        if len(args) < 2:
            continue
        if ins.result not in live:
            # An access chain whose result nothing live consumes is not a
            # use: `map_1465b18f.frag` builds a fully static chain into a
            # matrix member, feeds it to a helper whose result is dropped,
            # and the compiler prints `: 0`.
            continue
        # An ARRAYED per-vertex block (`gl_in[]`, `gl_out[]`) is indexed
        # by the VERTEX first and the member second (notes/63); the
        # vertex index may be dynamic (`gl_in[gl_InvocationID]`).
        _mi = 2 if module.block_array_depth(var_id) == 1 else 1
        if len(args) <= _mi:
            continue
        m = _constant_index(module, args[_mi])
        if m is None:
            # A dynamic member index means this walk cannot say WHICH
            # member is touched, and the `used` column is per member.
            # Guessing "all of them" over-reports -- measured: it turned a
            # `: 0` into a `: 1` on a matrix member of a uniform block.
            raise NotEstablished(
                "an access chain into a block with a non-constant member "
                "index: the per-member `used` column cannot be decided")
        out.add(m)
    return out


def _is_array(module, var_id):
    """Is the variable's pointee an array type?"""
    ins = module.globals.get(var_id)
    if ins is None:
        return False
    ptr = module.types.get(ins.result_type)
    if ptr is None or ptr.opcode != Op.OpTypePointer:
        return False
    t = module.types.get(ptr.operands[2])
    return t is not None and t.opcode in ARRAY_TYPES


def _element_uses(module, var_id):
    """Which elements of a standalone array variable the body touches.

    `gl_TessLevelOuter` is not a block member: it is its own variable of array
    type, and each element is its own symbol with its own slot, so a shader
    that writes three of them prints `result.patch.tessouter[0..2]`.  A
    variable that is not indexed at all answers `{0}`, which is what a scalar
    built-in needs.
    """
    out = set()
    for ins in _chains_into(module, var_id):
        args = ins.args()
        if len(args) < 2:
            continue
        e = _constant_index(module, args[1])
        if e is None:
            raise NotEstablished(
                "an access chain into a built-in array with a "
                "non-constant index: which slots it occupies cannot be "
                "decided")
        out.add(e)
    return out or {0}


def _member_element_uses(module, var_id, strict=True):
    """(member, element) pairs of a block variable that the body touches.

    `gl_ClipDistance` and `gl_CullDistance` are arrays inside `gl_PerVertex`,
    and each ELEMENT is its own symbol with its own slot: a shader that
    declares `gl_ClipDistance[3]` and writes only element 0 gets
    `result.clip[0..0]`, not `[0..2]`.  So the per-member walk of
    `_member_uses` is not fine enough here and this one keeps the second index
    as well.  A member with no second index answers `(m, 0)`.
    """
    # `gl_out[]` is an array of `gl_PerVertex`, so the access chain indexes the
    # array before the member; those leading indices are skipped rather than
    # read as member numbers, and their being dynamic (the invocation id) does
    # not matter.
    skip = module.block_array_depth(var_id)
    out = set()
    for ins in _chains_into(module, var_id):
        idx = ins.args()[1 + skip:]
        if not idx:
            continue
        m = _constant_index(module, idx[0])
        if m is None:
            raise NotEstablished(
                "an access chain into a block with a non-constant member "
                "index: the per-member `used` column cannot be decided")
        e = 0
        if len(idx) > 1:
            e = _constant_index(module, idx[1])
            if e is None:
                if strict:
                    raise NotEstablished(
                        "an access chain into a block array member with a "
                        "non-constant element index: which slots the "
                        "member occupies cannot be decided")
                # A dynamic index CAN name element 0, and the `#var` line
                # for an array member IS element 0's (notes/14), so for the
                # `used` column a dynamic index counts as reaching it.
                # This over-reports in the same direction the old
                # per-member test did, and it is the difference between
                # covering the corpus and refusing two thirds of it -- but
                # for an INTERFACE block, where each element is its own
                # slot, the same guess would invent declarations, so that
                # caller keeps `strict`.
                e = 0
        out.add((m, e))
    return out


def _chain_loaded(module, vid):
    """Is anything LOADED through an access chain based on this variable?"""
    chains = set(ins.result for ins in _chains_into(module, vid))
    for fn in module.functions:
        for ins in fn.insns:
            if ins.opcode == Op.OpLoad and ins.args()[0] in chains | {vid}:
                return True
    return False


def _kill_count(module):
    """How many `OpKill`-like instructions the module has."""
    n = 0
    for fn in module.functions:
        for ins in fn.insns:
            if ins.opcode in KILLS:
                n += 1
    return n


def _has_immediate_const_buffer(module):
    """Does this module get the compiler's own `ImmediateConstBuffer`?

    A listing can carry a block the module never declared:

        #semantic ImmediateConstBuffer.11 : __LOCAL

    It is where the compiler puts a constant ARRAY -- an `OpConstantComposite`
    whose type is `OpTypeArray` -- so that it can be indexed.  Detected here by
    exactly that, which is a NECESSARY condition observed on the shaders that
    have the line and absent from those that do not; whether it is SUFFICIENT
    is unread, so this only ever causes a refusal, never an emitted line.
    """
    for ins in module.constants.values():
        if ins.opcode != Op.OpConstantComposite:
            continue
        t = module.types.get(ins.result_type)
        if t is not None and t.opcode == Op.OpTypeArray:
            return True
    return False


# LOCAL MEMORY SYMBOLS -- the `__LOCAL` semantics and the `lmem<k>` rows.
#
# An array the shader indexes but cannot keep in registers goes to local
# memory, and the listing declares it twice: `#semantic <name> : __LOCAL` at
# the END of the semantic block, after every buffer, and
# `#var <type> <name>[0] :  : lmem<k>[0] : -1 : <used>` at the end of the var
# block, after the buffer rows and before the `$kill` rows.
#
# Two kinds of symbol land there and they are spelled the same way:
#
#   * a PRIVATE array the module declares -- glslang calls them `TempArray0`,
#     `TempArray1` -- which the listing suffixes with the variable's own
#     SPIR-V <id>: `TempArray0.35`, `TempArray1.129` for ids 35 and 129, the
#     same `<name>.<id>` shape `__defaultname_<id>` uses (notes/03);
#   * `ImmediateConstBuffer`, which looked like a symbol the compiler invents
#     -- 147 corpus listings carry `ImmediateConstBuffer.11` and never any
#     other number -- and is nothing of the kind: the MODULE declares it, as a
#     Private array named `ImmediateConstBuffer` that glslang happens to give
#     <id> 11 every time.  So it is the first case, not a second one, and the
#     `_has_immediate_const_buffer` guess (a constant array is present) is not
#     needed to find it.

def _dynamically_indexed(module):
    """The variables some access chain indexes by a value (its first index is
    not a constant)."""
    from glasmlib.operands import _scalar_value
    dyn = set()
    for i in module.insns:
        if i.opcode in ACCESS_CHAINS and len(i.args()) >= 2 \
                and _scalar_value(module, i.args()[1]) is None:
            dyn.add(i.args()[0])
    return dyn


def _private_array_type(module, ins):
    """The array type of a Private global, or None when it is not one."""
    if ins.operands[2] != StorageClass.Private:
        return None
    ptr = module.types.get(ins.result_type)
    if ptr is None or ptr.opcode != Op.OpTypePointer:
        return None
    t = module.types.get(ptr.operands[2])
    if t is None or t.opcode not in ARRAY_TYPES:
        return None
    return t


def _local_arrays(module):
    """The local-memory symbols, in the order they take their `lmem` numbers.

    Each entry is (name, element type spelling, variable id or None).
    """
    out = []
    # ONLY AN ARRAY INDEXED BY A VALUE goes to local memory (notes/87): the
    # cut `0091_mq_n8.frag` stores `ImmediateConstBuffer` and never indexes it,
    # and its listing has no `__LOCAL`, no `lmem` and no element stores to
    # memory -- the array is kept in registers.
    dyn = _dynamically_indexed(module)
    for vid, ins in module.globals.items():
        t = _private_array_type(module, ins)
        if t is None:
            continue
        if vid not in dyn and not ENV.get("G2S_ALLLMEM"):
            continue
        name = module.name_of(vid)
        if name is None:
            raise NotEstablished("a private array with no OpName: its local "
                                 "memory symbol has no spelling")
        out.append(("%s.%d" % (name, vid), type_spelling(module, t.args()[0]),
                    vid))
    return out


def _lmem_arrays(module):
    """[(variable id, element count)] in `lmem<k>` order (`_local_arrays`).

    The count is the array type's length: `TEMP lmem0[4];` for `uvec4[4]`
    (`0071_lm_icb.frag`), `TEMP lmem0[3];` for `vec4[3]` (`0071_lm_icbf.frag`).
    """
    from glasmlib.operands import _scalar_value
    out = []
    for _n, _t, vid in _local_arrays(module):
        ptr = module.types.get(module.globals[vid].result_type)
        t = module.types.get(ptr.operands[2])
        n = (_scalar_value(module, t.args()[1])
             if t.opcode == Op.OpTypeArray else None)
        if n is None:
            raise NotEstablished("a local-memory array with no constant "
                                 "length: its `TEMP lmem` size is unread")
        out.append((vid, int(n)))
    return out


def _fold_identities(module):
    """The front end's identity folds (notes/89), applied to the module.

    `x + 0`, `0 + x`, `x - 0`, `x * 1` and `1 * x` on SCALARS, integer and
    float alike, build no node: the value IS the other operand.  Measured on
    `0089_ia_z.frag` (`(j >> 2) + 0`, `m * 1`, `0 + m`: no ADD or MUL, the
    stores read the SHR and the attribute) and `0089_fa_z.frag` (`a.x + 0.0`,
    `a.y * 1.0`, `1.0 * a.z`, `a.w - 0.0`: each a gather of the attribute's
    component, as a plain extract).  The corpus's structured-buffer
    addresses are `(k >> 2) + 0` everywhere.

    Every later use of a folded result is rewritten to the operand it folds
    to (`Insn.ref_positions`, the grammar's IdRef words only).  Returns the
    folded result ids, whose own instructions the lowering skips."""
    if getattr(module, "_g2s_folded", None) is not None:
        return module._g2s_folded
    folded = set()
    module._g2s_folded = folded
    if ENV.get("G2S_NOIDENT"):
        return folded
    alias = _identity_aliases(module)
    folded.update(alias)
    if not alias:
        return folded
    for ins in module.insns:
        for p in ins.ref_positions():
            v = ins.operands[p]
            if v in alias:
                ins.operands[p] = alias[v]
    return folded


_ADDS = (Op.OpIAdd, Op.OpFAdd)
_SUBS = (Op.OpISub, Op.OpFSub)
_MULS = (Op.OpIMul, Op.OpFMul)
_FLOAT_ONE_BITS = 0x3f800000
_ZERO = ((Op.OpTypeInt, 0), (Op.OpTypeFloat, 0))
_ONE = ((Op.OpTypeInt, 1), (Op.OpTypeFloat, _FLOAT_ONE_BITS))


def _const_bits(module, cid):
    """(type opcode, 32-bit word) of a 32-bit scalar OpConstant, or None."""
    c = module.constants.get(cid)
    if c is None or c.opcode != Op.OpConstant:
        return None
    t = module.types.get(c.result_type)
    if t is None or t.opcode not in (Op.OpTypeInt, Op.OpTypeFloat) \
            or t.args()[0] != 32:
        return None
    return (t.opcode, c.args()[0])


def _identity_aliases(module):
    """{folded result: the operand it IS}, chains resolved."""
    alias = {}
    for ins in module.insns:
        if ins.opcode not in _ADDS + _SUBS + _MULS \
                or not ins.has_result or len(ins.args()) != 2:
            continue
        rt = module.types.get(ins.result_type)
        if rt is None or rt.opcode not in (Op.OpTypeInt, Op.OpTypeFloat):
            continue                            # scalars only (measured)
        a, b = ins.args()
        ca, cb = _const_bits(module, a), _const_bits(module, b)
        if ins.opcode in _ADDS:
            other = b if ca in _ZERO else a if cb in _ZERO else None
        elif ins.opcode in _SUBS:
            other = a if cb in _ZERO else None
        else:
            other = b if ca in _ONE else a if cb in _ONE else None
        if other is None:
            continue
        while other in alias:
            other = alias[other]
        alias[ins.result] = other
    return alias
