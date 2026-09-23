"""varblock.py -- the `#semantic` and `#var` comment blocks.

    #var float4 a0 : $vin.ATTR0 : ATTR0 : -1 : 1
    #var float gl_PointSize : $vout.PSIZE : PSIZ : -1 : 0

Six fields.  The type spelling is READ (the printer's name table at
0x71014f95c8, notes/06); the rest is MEASURED over 607 listings -- the 90
probes and 517 corpus shaders -- and each rule below says what it was checked
against.  Where a variable is a kind none of this covers, the whole block
raises rather than emitting four right lines and one invented one.

A row is `(label, type, semantic, register, used)`.
"""
from spvnames import Op, ExecutionModel, ExecutionMode, StorageClass, \
    BuiltIn, Decoration

from glasmlib.common import NotEstablished, ARRAY_TYPES, ENV
from glasmlib.types import type_spelling
from glasmlib.operands import _scalar_value
from glasmlib.semantics import BUILTIN_SEMANTIC, BUILTIN_SLOT, BUILTIN_TYPE, \
    TESC_PATCH_KIND, TESC_PER_VERTEX_KIND, _location_semantic, \
    _builtin_semantic, _builtin_register, _register
from glasmlib.usage import _live_ids, _uses_builtin, _unreferenced_block, \
    _member_uses, _member_element_uses, _chain_loaded, _kill_count, \
    _local_arrays
from glasmlib.blocks import HANDLE_BUFFER, HANDLE_SIZE, BLOCK_STORAGE, \
    _blocks_and_opaques, _opaque_offset, _storage_image, _struct_array, \
    _stride_suffix, _row_offset, _STRIDE_FLOOR

INTERFACE_STORAGE = (StorageClass.Input, StorageClass.Output)

# The stages whose inputs arrive one element per input vertex.
_PER_VERTEX_INPUT_MODELS = (ExecutionModel.TessellationControl,
                            ExecutionModel.TessellationEvaluation,
                            ExecutionModel.Geometry)


def semantic_lines(module, entry_name="main"):
    """The `#semantic` block.

    NOT a sort of its own.  `f_7100bd2090` walks the SAME linked list at
    `program + 176` that the `#var` emitter walks (notes/13), head to tail, and
    skips every symbol whose flag word fails `(flags & 6) == 4` -- that is, it
    is the `#var` sequence filtered to the symbols that have a binding.  So the
    order here is whatever order `var_lines` puts them in, and emitting it any
    other way would make the two blocks disagree on shaders where the list is
    not sorted.
    """
    if (any(ins.operands[2] == StorageClass.Workgroup
            for ins in module.globals.values()
            if ins.opcode == Op.OpVariable)
            and ENV.get("G2S_NOSHAREDMEM")):
        # SHARED MEMORY is READ and implemented (notes/124 §2): the
        # `#semantic <name> : SHARED` line, the `shared_mem[0]` rows, the
        # `SHARED_MEMORY <bytes>` declaration and the `LDS`/`STS`/`ATOMS`
        # forms.  `G2S_NOSHAREDMEM=1` restores this refusal.
        raise NotEstablished("a Workgroup (shared) variable: its #semantic "
                             "and #var rows are not read")
    blocks, opaques = _blocks_and_opaques(module)
    by_name = {}
    for n, off in opaques:
        by_name[n] = "#semantic %s : BUFFER[%d][%d]" % (n, HANDLE_BUFFER, off)
    for name, tname, kind, _reg, binding, _p, _v in blocks:
        if _unreferenced_block(module, _v, kind):
            binding = -1
        by_name[name] = "#semantic %s.%s : %s[%d]" % (tname, name, kind,
                                                      binding)
    out = ["#semantic %s : SHARED" % _n
           for _n, _v, _e in _shared_arrays(module)]
    out += [by_name[n] for n in _symbol_order(module, entry_name)
            if n in by_name]
    out.extend("#semantic %s : __LOCAL" % n
               for n, _t, _v in _local_arrays(module))
    return out


def _shared_arrays(module):
    """[(name, variable id, element count)] for the Workgroup variables.

    READ off `post_tonemap_update.comp` and `post_tonemap_histogram.comp`,
    the corpus's only shaders with shared memory (notes/124 §2):

        #semantic TGSM0 : SHARED
        #var uint TGSM0[0].value[0] :  : shared_mem[0] : -1 : 1
        SHARED_MEMORY 512;
        SHARED shared_mem[] = { program.sharedmem };

    Their `TGSM0` is `struct { uint value[1]; } [128]` -- 128 * 4 = 512
    bytes -- and the row is the SAME `name[0].<member>[0]` shape the
    storage-buffer rows already use.  Only that shape is taken.
    """
    out = []
    for vid, ins in module.globals.items():
        if (ins.opcode != Op.OpVariable
                or ins.operands[2] != StorageClass.Workgroup):
            continue
        name = module.name_of(vid)
        if name is None:
            raise NotEstablished("a shared variable with no OpName")
        out.append((name, vid, _shared_words(module, vid)))
    return out


def _shared_words(module, vid):
    """The number of 32-bit words a shared variable occupies."""
    _pt = module.types.get(module.globals[vid].result_type)
    _t = module.types.get(_pt.operands[2]) if _pt is not None else None
    _n = 1
    while _t is not None and _t.opcode in ARRAY_TYPES:
        _len = module.constants.get(_t.args()[1]) if len(_t.args()) > 1 \
            else None
        if _len is None:
            raise NotEstablished(
                "a shared array with no constant length: not measured")
        _n *= int(_len.args()[-1])
        _t = module.types.get(_t.args()[0])
    if _t is not None and _t.opcode == Op.OpTypeStruct:
        if len(_t.args()) != 1:
            raise NotEstablished(
                "a shared struct of more than one member: not measured")
        _m = module.types.get(_t.args()[0])
        while _m is not None and _m.opcode in ARRAY_TYPES:
            _len = module.constants.get(_m.args()[1])
            if _len is None:
                raise NotEstablished(
                    "a shared array with no constant length: not measured")
            _n *= int(_len.args()[-1])
            _m = module.types.get(_m.args()[0])
        _t = _m
    if _t is None or _t.opcode not in (Op.OpTypeInt, Op.OpTypeFloat):
        raise NotEstablished(
            "a shared variable whose element is not a 32-bit scalar: not "
            "measured")
    return _n


def _shared_rows(module, used):
    """The `#var` rows of the Workgroup variables."""
    out = []
    for name, vid, _n in _shared_arrays(module):
        _pt = module.types.get(module.globals[vid].result_type)
        _at = module.types.get(_pt.operands[2])
        if _at is None or _at.opcode not in ARRAY_TYPES:
            raise NotEstablished("a shared variable that is not an array")
        _st = module.types.get(_at.args()[0])
        if _st is not None and _st.opcode != Op.OpTypeStruct:
            # A BARE ARRAY -- no struct wrapper -- is `<name>[0]` against
            # `shared_mem[0]`, and it carries the COUNT SUFFIX every array
            # of more than 32 elements carries: `probes/0119_bar_a.comp`
            # (`shared uint[64]`) prints
            #     #var uint TGSM0[0] :  : shared_mem[0], 64 : -1 : 1
            # where the corpus's struct-wrapped array has no suffix,
            # because the row there is for the INNER `value[0]`, whose
            # length is 1.  Same rule, different array.  (notes/124 §3)
            out.append(("%s[0]" % name, type_spelling(module, _at.args()[0]),
                        "", "shared_mem[0]%s" % _array_count_suffix(
                            module, _at),
                        1 if vid in used else 0))
            continue
        if _st is None:
            raise NotEstablished("a shared array with no element type")
        _mname = module.member_names.get((_at.args()[0], 0))
        _mt = module.types.get(_st.args()[0])
        _label = "%s[0].%s%s" % (name, _mname,
                                 "[0]" if _mt is not None
                                 and _mt.opcode in ARRAY_TYPES else "")
        _et = _mt.args()[0] if _mt is not None \
            and _mt.opcode in ARRAY_TYPES else _st.args()[0]
        out.append((_label, type_spelling(module, _et), "",
                    "shared_mem[0]", 1 if vid in used else 0))
    return out


def _symbol_order(module, entry_name="main"):
    """The symbol names in the order the list at `program + 176` holds them.

    Reconstructed from 607 listings, not read: see notes/13.  Both `#semantic`
    and `#var` are walks of this one list, so they are produced from one order
    here rather than sorted twice.
    """
    plain, iface_blocks, buffers, opaque = [], [], [], []
    for vid, ins in module.globals.items():
        storage = ins.operands[2]
        ptr = module.types.get(ins.result_type)
        if ptr is None or ptr.opcode != Op.OpTypePointer:
            continue
        pointee = ptr.operands[2]
        name = module.name_of(vid)
        if storage == StorageClass.UniformConstant:
            if name:
                opaque.append(name)
        elif storage in BLOCK_STORAGE:
            buffers.append((name or "__defaultname_%d" % vid,
                            0 if _opaque_offset(module, pointee) is None
                            else 1))
        elif storage in INTERFACE_STORAGE:
            if module.decoration(vid, Decoration.Location) is not None:
                if name:
                    plain.append(name)
            else:
                iface_blocks.append(name or "")
    return (sorted(plain + opaque)
            + sorted(iface_blocks)
            + [n for n, _k in sorted(buffers, key=lambda b: (b[1], b[0]))])


# The register names of tessellation control's PATCH outputs (notes/07: the
# output table starts `INNER0 INNER1 OUTER0 OUTER1`) and their slots in kind
# 0xbd; the semantic is `$ppvout.` + register name + slot (notes/20).
_TESC_PATCH_REG = {BuiltIn.TessLevelInner: "INNER0",
                   BuiltIn.TessLevelOuter: "OUTER0"}
# The per-vertex block members whose two rows are measured (bindmap_*.tsv).
# The per-vertex output block's members.  Clip and cull are read off the
# `water_*.tesc` rows exactly as position and point size were (notes/114
# §86): `gl_out[0].gl_ClipDistance[0] : $vin.CLP065585 : CLP0[65585]` and
# `gl_out-out.gl_ClipDistance[0] : $vout.CLP049 : CLP0[49]`.
_TESC_PV_MEMBERS = {BuiltIn.Position: ("POSITION", "HPOS"),
                    BuiltIn.PointSize: ("PSIZE", "PSIZ"),
                    BuiltIn.ClipDistance: ("CLP0", "CLP0"),
                    BuiltIn.CullDistance: ("CUL0", "CUL0")}

# The per-vertex output block's READ side carries this bit in its register.
_TESC_READ_SIDE = 0x10000


def _tesc_patch_rows(module, vid, name, pointee, bi, used):
    """A Patch built-in array (`gl_TessLevelInner/Outer`): ONE row,
    `name[0] : $ppvout.<REG><slot> : <REG>[<slot>]` (kind 0xbd)."""
    if vid not in used:
        return ("plain", [])
    slot = BUILTIN_SLOT[TESC_PATCH_KIND][bi[0]]
    reg = _TESC_PATCH_REG[bi[0]]
    at = module.types.get(pointee)
    if name is None or at is None or at.opcode != Op.OpTypeArray:
        return None
    return ("plain", [("%s[0]" % name, type_spelling(module, at.args()[0]),
                       "$ppvout.%s%d" % (reg, slot),
                       "%s[%d]" % (reg, slot), 1)])


def _tesc_per_vertex_rows(module, vid, name, sid, st, used):
    """The per-vertex output block `gl_out[]` (kind 0xb7): TWO rows per
    member, the READ side `gl_out[0].<m> : $vin.VERTEXOUT[0].<SEM> :
    <REG>[0x10000 | slot]`, used only if the block is read, and then the
    WRITE side `gl_out-out.<m> : $vout.<SEM> : <REG>[slot]`, used if written
    -- all read rows first (the `water_*.tesc` corpus rows)."""
    read = 1 if _chain_loaded(module, vid) else 0
    # THE WRITE SIDE IS PER MEMBER, not per block: `water_00540147.tesc` has
    # `gl_out-out.gl_Position .. : 1` against `gl_PointSize`, `gl_ClipDistance`
    # and `gl_CullDistance` at 0, on one block.
    _touched = _member_uses(module, vid)
    rd, wr = [], []
    for i, mt in enumerate(st.args()):
        b = module.member_decoration(sid, i, Decoration.BuiltIn)
        mname = module.member_names.get((sid, i))
        if b is None or mname is None or b[0] not in _TESC_PV_MEMBERS:
            return None
        sem, reg = _TESC_PV_MEMBERS[b[0]]
        slot = BUILTIN_SLOT[TESC_PER_VERTEX_KIND].get(b[0])
        if slot is None:
            return None
        # AN ARRAY MEMBER APPENDS THE SLOT AND TAKES NO `VERTEXOUT[0].`
        # STEP -- the same "the step and the index are alternatives, not
        # both" that `_builtin_block_rows` already reads for `gl_in`.
        _mt = module.types.get(mt)
        _arr = _mt is not None and _mt.opcode in ARRAY_TYPES
        _label = mname + "[0]" if _arr else mname
        ty = type_spelling(module, _mt.args()[0] if _arr else mt)
        if _arr:
            _rsem = "$vin.%s%d" % (sem, _TESC_READ_SIDE | slot)
            _wsem = "$vout.%s%d" % (sem, slot)
        else:
            _rsem = "$vin.VERTEXOUT[0].%s" % sem
            _wsem = "$vout.%s" % sem
        rd.append(("%s[0].%s" % (name or "gl_out", _label), ty, _rsem,
                   "%s[%d]" % (reg, _TESC_READ_SIDE | slot), read))
        wr.append(("%s-out.%s" % (name or "gl_out", _label), ty, _wsem,
                   "%s[%d]" % (reg, slot),
                   1 if i in _touched else 0))
    return ("pv", rd + wr)


def _tesc_output_rows(module, vid, used):
    """The `#var` rows of one tessellation-control output, or None when it
    is not one of the two measured shapes (see the two helpers).
    Returns ("plain", rows) or ("pv", rows).
    """
    ins = module.globals[vid]
    name = module.name_of(vid)
    pointee = module.types[ins.result_type].operands[2]
    bi = module.decoration(vid, Decoration.BuiltIn)
    if bi is not None and module.decoration(vid, Decoration.Patch) \
            is not None and bi[0] in _TESC_PATCH_REG:
        return _tesc_patch_rows(module, vid, name, pointee, bi, used)
    at = module.types.get(pointee)
    if bi is not None or at is None or at.opcode != Op.OpTypeArray:
        return None
    sid = at.args()[0]
    st = module.types.get(sid)
    if st is None or st.opcode != Op.OpTypeStruct:
        return None
    return _tesc_per_vertex_rows(module, vid, name, sid, st, used)


class _Rows(object):
    """The `#var` rows as they are collected, by the part of the list they
    sort into."""

    def __init__(self):
        self.plain = []             # sorted by label
        self.block_members = []     # (prefix, rows), sorted by prefix
        self.pv_members = []        # the per-vertex input block, last


def _origin_fragcoord(module, ep):
    """Does the entry point's mode create the gl_FragCoord symbol?

    A fragment shader with OriginUpperLeft gets a gl_FragCoord symbol even
    when it never mentions it; with OriginLowerLeft it does not.  Isolated
    with a probe pair whose modules differ in ONE instruction --
    `OpExecutionMode %4 OriginUpperLeft` against `OriginLowerLeft`, which is
    all that separates glslang's -V output from its -G output for the same
    shader.  The listing gains or loses the line accordingly.  It is the
    reason the corpus (Vulkan flavour, 395/395 have it) and the probe set
    (OpenGL flavour, none do) disagreed, and it is consistent with the
    recovered mode table: the mode has its own arm at 0x7100fcca8c.

    The symbol the mode creates and the symbol a DECLARED `gl_FragCoord`
    creates are the same one: the front end looks the name up before making
    it, so a module that both sets OriginUpperLeft and declares the variable
    still gets ONE line.
    """
    modes = {mode for eid, mode, _ in module.execution_modes if eid == ep[1]}
    return (ep[0] == ExecutionModel.Fragment
            and ExecutionMode.OriginUpperLeft in modes)


def _fragcoord_row(module, model):
    sem = _builtin_semantic(model, StorageClass.Input, "gl_FragCoord")
    reg = _builtin_register(model, StorageClass.Input, "gl_FragCoord")
    return ("gl_FragCoord", "float4", sem, reg,
            1 if _uses_builtin(module, BuiltIn.FragCoord) else 0)


def _location_row(module, model, vid, storage, pointee, name, loc, used):
    """A user in/out variable with a Location."""
    if name is None:
        raise NotEstablished("an interface variable with no OpName")
    flat = module.decoration(vid, Decoration.Flat) is not None
    sem, reg = _location_semantic(
        model, storage, loc[0], flat,
        module.decoration(vid, Decoration.Patch) is not None)
    # A PER-VERTEX input -- the tessellation and geometry stages see each
    # user input as one element per input vertex, and glslang gives the
    # variable an array type to say so.  The listing spells that element 0:
    # the name gains `[0]` and the semantic gains a `VERTEX[0].` step, exactly
    # the way a `gl_in[0].` block member does.  Measured on the `water_*`
    # tessellation-evaluation shaders; the ELEMENT TYPE is what the type
    # column shows, not the array.
    ety = pointee
    at = module.types.get(pointee)
    label = name
    if (storage == StorageClass.Input and model in _PER_VERTEX_INPUT_MODELS
            and at is not None and at.opcode in ARRAY_TYPES):
        ety = at.args()[0]
        label = name + "[0]"
        sem = sem.replace("$vin.", "$vin.VERTEX[0].", 1)
    # A PER-VERTEX OUTPUT OF TESSELLATION CONTROL TAKES TWO ROWS, the same
    # two sides `gl_out` does (notes/117 §1).  `water_00540147.tesc`:
    #     hs_BINORMAL0[0]   : $vin.VERTEXOUT[0].ATTR3 : ATTR3[65539] : .. : 0
    #     hs_BINORMAL0-out  : $vout.ATTR3             : ATTR3[3]     : .. : 1
    # -- the read side named `[0]` with the `VERTEXOUT[0].` step and bit 16
    # set in the register, the write side `-out` with neither.  They come as
    # a PAIR per variable, where the block's rows came as all-read then
    # all-write.
    if (model == ExecutionModel.TessellationControl
            and storage == StorageClass.Output
            and module.decoration(vid, Decoration.Patch) is None
            and not ENV.get("G2S_NOTESCPVOUT")):
        _ty = type_spelling(module, ety)
        _rsem = sem.replace("$vout.", "$vin.VERTEXOUT[0].", 1)
        return [(name + "[0]", _ty, _rsem,
                 _register(TESC_PER_VERTEX_KIND, "ATTR%d" % loc[0],
                           _TESC_READ_SIDE | loc[0]),
                 1 if _chain_loaded(module, vid) else 0),
                (name + "-out", _ty, sem, reg, 1 if vid in used else 0)]
    return [(label, type_spelling(module, ety), sem, reg,
             1 if vid in used else 0)]


def _builtin_block_rows(module, model, vid, storage, pointee, st, prefix,
                        pervertex):
    """The member rows of a built-in BLOCK (`gl_PerVertex`, `gl_in[]`)."""
    touched = _member_uses(module, vid)
    members = []
    for i in range(len(st.args())):
        b = module.member_decoration(pointee, i, Decoration.BuiltIn)
        mname = module.member_names.get((pointee, i))
        if b is None or mname is None:
            raise NotEstablished(
                "a struct interface member with no BuiltIn or name")
        if mname not in BUILTIN_SEMANTIC:
            raise NotEstablished("built-in %s" % mname)
        mt = st.args()[i]
        mtype = module.types.get(mt)
        label = prefix + mname
        if mtype is not None and mtype.opcode in ARRAY_TYPES:
            label += "[0]"                  # an array built-in
        sem = _builtin_semantic(model, storage, mname)
        # The `VERTEX[0].` step and the appended slot are alternatives, not
        # both: `$vin.VERTEX[0].POSITION` but `$vin.CLP049`, on the same block
        # of the same shader.  So the step goes on only where the semantic
        # took no index.  (The index is `%d` or, in kind 0x30, `[%d][%d]`.)
        if pervertex and not (sem[-1].isdigit() or sem[-1] == "]"):
            sem = sem.replace("$vin.", "$vin.VERTEX[0].", 1)
        reg = _builtin_register(model, storage, mname)
        members.append((label, type_spelling(module, mt), sem, reg,
                        1 if i in touched else 0))
    return members


def _interface_block_rows(module, model, vid, storage, pointee, st, name):
    """The member rows of a USER interface block -- a struct whose members
    carry a Location rather than a BuiltIn (notes/114 \u00a746).

    `_builtin_block_rows` demands a BuiltIn on every member and refused
    these.  They are spelled exactly like a plain located variable, one row
    per member, with the BLOCK's name in front; and when the variable has no
    name of its own the block is called `__defaultname_<result id>` --
    `branches_shadowcast_ps_nodiscard_unrolledinput.frag`'s block is id 19
    and its rows are

        #var float3 __defaultname_19.v_v2p_vInterpolant1 : $vin.ATTR0 : ...
        #var float4 __defaultname_19.v_v2p_vUserInterpolant0 : $vin.ATTR1 : ...

    each member's ATTR being its OWN Location, and the used column the
    member uses as everywhere else.  `G2S_NOIFBLOCK=1` refuses it again."""
    if ENV.get("G2S_NOIFBLOCK"):
        raise NotEstablished("a struct interface member with no BuiltIn")
    touched = _member_uses(module, vid)
    base = name or "__defaultname_%d" % vid
    out = []
    for i in range(len(st.args())):
        mloc = module.member_decoration(pointee, i, Decoration.Location)
        mname = module.member_names.get((pointee, i))
        if mloc is None or not mname:
            raise NotEstablished(
                "a struct interface member with no BuiltIn or name")
        flat = (module.member_decoration(pointee, i, Decoration.Flat)
                is not None
                or module.decoration(vid, Decoration.Flat) is not None)
        sem, reg = _location_semantic(model, storage, mloc[0], flat)
        out.append(("%s.%s" % (base, mname),
                    type_spelling(module, st.args()[i]), sem, reg,
                    1 if i in touched else 0))
    return out


def _loose_builtin_row(module, model, vid, storage, pointee, name, used,
                       origin_fragcoord):
    """A built-in variable of its own, or None when it gets no row.

    glslang declares gl_VertexID and gl_InstanceID on every vertex module
    whether or not the shader touches them, and an UNUSED one produces no
    `#var` line at all -- measured: p01_copy's module carries both and its
    listing has neither.  A used one does, and is named by
    BUILTIN_SEMANTIC."""
    builtin = module.decoration(vid, Decoration.BuiltIn)
    forced = (origin_fragcoord and builtin is not None
              and builtin[0] == BuiltIn.FragCoord)
    if vid not in used and not forced:
        return None
    if name is None or name not in BUILTIN_SEMANTIC:
        raise NotEstablished(
            "a used built-in with no established semantic: %s "
            "(BuiltIn %s)" % (name, builtin[0] if builtin else "?"))
    sem = _builtin_semantic(model, storage, name)
    reg = _builtin_register(model, storage, name)
    return (name, BUILTIN_TYPE.get(name) or type_spelling(module, pointee),
            sem, reg, 1 if vid in used else 0)


def _interface_rows(module, model, vid, storage, pointee, name, used,
                    origin_fragcoord, rows):
    """An Input or Output global: a Location row, tessellation control's
    output rows, a built-in block's member rows, or a loose built-in."""
    loc = module.decoration(vid, Decoration.Location)
    if loc is not None:
        rows.plain.extend(_location_row(module, model, vid, storage, pointee,
                                        name, loc, used))
        return
    # TESSELLATION CONTROL'S OUTPUTS (notes/63, notes/20).
    if (model == ExecutionModel.TessellationControl
            and storage == StorageClass.Output):
        row = _tesc_output_rows(module, vid, used)
        if row is not None:
            kind_, trows = row
            if kind_ == "pv":
                rows.pv_members.append(("%s[0]." % (name or "gl_out"), trows))
            else:
                rows.plain.extend(trows)
            return
    # No Location: a built-in, or a built-in BLOCK.
    #
    # On the input side of a tessellation or geometry stage the block is
    # PER-VERTEX and glslang gives it an array type (`gl_in`), so the struct
    # is one level down and every member is spelled `gl_in[0].gl_Position`
    # with a `VERTEX[0].` step in the semantic.
    st = module.types.get(pointee)
    prefix, pervertex = "", False
    if (st is not None and st.opcode in ARRAY_TYPES
            and storage == StorageClass.Input):
        inner = module.types.get(st.args()[0])
        if inner is not None and inner.opcode == Op.OpTypeStruct:
            # Geometry prints a clip/cull member's SEMANTIC in the two-index
            # form too (`$vin.CLP0[0][49]`): `_builtin_semantic` takes it from
            # the kind switch.
            pointee, st, pervertex = st.args()[0], inner, True
            prefix = "%s[0]." % (name or "gl_in")
    if st is not None and st.opcode == Op.OpTypeStruct:
        if (not pervertex and st.args() and module.member_decoration(
                pointee, 0, Decoration.BuiltIn) is None
                and module.member_decoration(
                    pointee, 0, Decoration.Location) is not None):
            # the BLOCK's rows go with the other block members, after
            # the plain ones: the compiler's list has `gl_FragCoord` first
            # and the block's four members after it, which sorting them
            # into `rows.plain` would not give ('_' sorts before 'g')
            rows.block_members.append((
                name or "__defaultname_%d" % vid,
                _interface_block_rows(module, model, vid, storage,
                                      pointee, st, name)))
            return
        members = _builtin_block_rows(module, model, vid, storage, pointee,
                                      st, prefix, pervertex)
        (rows.pv_members if pervertex
         else rows.block_members).append((prefix, members))
        return
    row = _loose_builtin_row(module, model, vid, storage, pointee, name, used,
                             origin_fragcoord)
    if row is not None:
        rows.plain.append(row)


def _opaque_row(module, vid, pointee, name, used):
    base = _opaque_offset(module, pointee)
    binding = module.decoration(vid, Decoration.Binding)
    if base is None or binding is None or name is None:
        raise NotEstablished("an opaque uniform this does not cover")
    off = base + HANDLE_SIZE * binding[0]
    if _storage_image(module, pointee):
        # A storage image's row is `<name>.__handle` with an EMPTY semantic
        # column, and `.__handle_nosize` when its format is Unknown
        # (notes/110: `si_a`..`si_e`, and `img` in `0110_si_d.comp`, which has no
        # format qualifier).
        fmt = module.types[pointee].operands[7]
        return ("%s.%s" % (name, "__handle_nosize" if fmt == 0
                           else "__handle"), "ulong", "",
                "buffer[%d][%d]" % (HANDLE_BUFFER, off),
                1 if vid in used else 0)
    return (name, "ulong", "BUFFER[%d][%d]" % (HANDLE_BUFFER, off),
            "buffer[%d][%d]" % (HANDLE_BUFFER, off), 1 if vid in used else 0)


def _global_rows(module, model, vid, ins, used, origin_fragcoord, rows):
    storage = ins.operands[2]
    ptr = module.types.get(ins.result_type)
    if ptr is None or ptr.opcode != Op.OpTypePointer:
        raise NotEstablished("a global whose type is not a pointer")
    pointee = ptr.operands[2]
    name = module.name_of(vid)
    if storage in INTERFACE_STORAGE:
        _interface_rows(module, model, vid, storage, pointee, name, used,
                        origin_fragcoord, rows)
    elif storage == StorageClass.UniformConstant:
        rows.plain.append(_opaque_row(module, vid, pointee, name, used))
    elif storage in BLOCK_STORAGE:
        pass                        # emitted after the other rows
    elif storage == StorageClass.Private:
        # A Private global is a module-scope temporary -- glslang hoists
        # every local of the entry point into one -- and it produces NO
        # `#var` line: checked on a corpus shader with 26 of them, none of
        # which appears in its listing.
        pass
    elif storage == StorageClass.Workgroup:
        pass                        # its row comes from `_shared_rows`
    else:
        raise NotEstablished(
            "a global in storage class %d -- Input, Output, UniformConstant, "
            "Uniform and StorageBuffer are established; this is not"
            % storage)


def _array_of_struct_rows(module, name, mname, mt, reg, binding, off,
                          member_used):
    """A member that is an ARRAY OF STRUCT -- the shape every storage buffer
    in this corpus has: one member, a runtime array of a one-member struct.
    The listing does not print the member; it prints element 0 of the array,
    then element 0 of each of the inner struct's own members, as
    `block.member[0].sub[0]`, with the semantic column empty the way every
    array member's is."""
    inner = _struct_array(module, mt)
    stride = module.decoration(mt, Decoration.ArrayStride)
    rows = []
    for j, sub in enumerate(module.types[inner].args()):
        sname = module.member_names.get((inner, j))
        soff = module.member_decoration(inner, j, Decoration.Offset)
        if sname is None or soff is None:
            raise NotEstablished("a nested block member with no "
                                 "name or Offset")
        st2 = module.types.get(sub)
        slabel = "%s.%s[0].%s" % (name, mname, sname)
        ety = sub
        if st2 is not None and st2.opcode in ARRAY_TYPES:
            slabel += "[0]"
            ety = st2.args()[0]
        creg = "%s[%d][%d]" % (reg, binding, off + soff[0])
        creg += _stride_suffix(stride)
        rows.append((slabel, type_spelling(module, ety), "", creg,
                     1 if member_used else 0))
    return rows


def _matrix_suffix(module, struct_id, i):
    """A MATRIX MEMBER carries the SAME stride suffix an array member does
    -- `buffer[0][0], 4` -- and it is the same rule: the stride in bytes
    over four.  The stride is the member's `MatrixStride`, not an
    `ArrayStride`.  The semantic column is NOT emptied the way an array's is;
    `0000_if_mat.vert` keeps `BUFFER[0]` on it."""
    _ms = module.member_decoration(struct_id, i, Decoration.MatrixStride)
    if _ms is None or _ms[0] % 4:
        raise NotEstablished("a matrix block member with no MatrixStride")
    # The suffix is the stride in ELEMENTS, as it is for an array member --
    # `MatrixStride` 16 prints `, 4`.  `_stride_suffix` is not reused: its
    # floor was measured on ARRAY members and would suppress this one.
    return ", %d" % (_ms[0] // 4)


def _array_count_suffix(module, atype):
    """AN ARRAY OF MORE THAN 32 VECTORS carries its length: the symbol's
    child count, the number blocks.py's `_stride_suffix` prints for an
    array of structs (f_7100bd2370's recursion at 0x7100bd2c40), under the
    same floor.  Probes: `vec4 m[33]` / `m[40]` / `ivec4 m[33]` / `vec2
    m[40]` print `, 33` / `, 40`; `m[3]` .. `m[32]` nothing, used or not
    (`0108_dx_a.frag`, and a scratch series).  Measured on scalar and vector
    elements only: a matrix element keeps `_matrix_suffix`'s rule, and an
    element of any other kind is refused past the floor."""
    if atype.opcode != Op.OpTypeArray:
        return ""
    _n = _scalar_value(module, atype.args()[1])
    try:
        _n = int(_n)
    except (TypeError, ValueError):
        raise NotEstablished("an array member whose length is not a constant")
    if _n <= _STRIDE_FLOOR:
        return ""
    _et = module.types.get(atype.args()[0])
    if _et is None or _et.opcode not in (Op.OpTypeVector, Op.OpTypeFloat,
                                         Op.OpTypeInt):
        raise NotEstablished("an array member of more than 32 elements "
                             "that are not scalars or vectors: its count "
                             "suffix is not measured")
    return ", %d" % _n


def _struct_member_rows(module, label, struct_id, kind, reg, binding,
                        base_off, use):
    """A block member that is itself a STRUCT, expanded (notes/114 \u00a747).

    `polygon_cull.comp`'s `ConstantBuffer` holds one struct member
    `frustum_g` whose own member `planes` is an array of `float4`, and the
    compiler prints ONE row for it, the path joined with a dot and the array
    spelled `[0]`:

        #var float4 __defaultname_231.frustum_g.planes[0] :  : buffer[1][0]

    so the sub-member follows exactly the rules the top-level loop applies:
    an array takes `[0]` and an EMPTY semantic, anything else the block's
    own semantic, and the register's offset is the two offsets ADDED.  The
    `used` column is the TOP member's -- the only block measured has one
    sub-member, so a finer reading than that would be a guess.
    `G2S_NOSTRUCTMEMBER=1` refuses it again."""
    if ENV.get("G2S_NOSTRUCTMEMBER"):
        raise NotEstablished("no established #var spelling for OpTypeStruct")
    st = module.types.get(struct_id)
    out = []
    for j, jt in enumerate(st.args()):
        jname = module.member_names.get((struct_id, j))
        joff = module.member_decoration(struct_id, j, Decoration.Offset)
        if jname is None or joff is None:
            raise NotEstablished("a struct block member with no name "
                                 "or Offset")
        jtype = module.types.get(jt)
        if jtype is not None and jtype.opcode == Op.OpTypeStruct:
            out.extend(_struct_member_rows(
                module, "%s.%s" % (label, jname), jt, kind, reg, binding,
                base_off + joff[0], use))
            continue
        if jtype is not None and jtype.opcode == Op.OpTypeMatrix:
            raise NotEstablished("a matrix inside a struct block member: "
                                 "its row has not been seen")
        is_array = jtype is not None and jtype.opcode in ARRAY_TYPES
        out.append(("%s.%s%s" % (label, jname, "[0]" if is_array else ""),
                    type_spelling(module, jt),
                    "" if is_array else "%s[%d]" % (kind, binding),
                    "%s[%d][%d]" % (reg, binding, base_off + joff[0]), use))
    return out


def _block_rows(module, block):
    """One uniform or storage block's rows, in ascending byte offset."""
    name, _tname, kind, reg, binding, struct_id, vid = block
    st = module.types[struct_id]
    touched = _member_uses(module, vid)
    unref = _unreferenced_block(module, vid, kind)
    # An ARRAY member prints one line, for ELEMENT 0 -- the label is
    # `name[0]` -- and each element is its own symbol, so the `used` column
    # is element 0's, not the array's.  `map_1465b18f.frag` reads row 2 of a
    # matrix array and the compiler prints `: 0` on the `[0]` line.
    touched_el = _member_element_uses(module, vid, strict=False)
    members = []
    unref_off = {}                  # an unreferenced block's row -> offset
    for i, mt in enumerate(st.args()):
        mname = module.member_names.get((struct_id, i))
        off = module.member_decoration(struct_id, i, Decoration.Offset)
        if mname is None or off is None:
            raise NotEstablished("a block member with no name or Offset")
        mtype = module.types.get(mt)
        is_array = mtype is not None and mtype.opcode in ARRAY_TYPES
        label = "%s.%s" % (name, mname)
        sem = "%s[%d]" % (kind, binding)
        if mtype is not None and mtype.opcode == Op.OpTypeStruct:
            if unref:
                raise NotEstablished(
                    "an unreferenced block with a struct member: its rows "
                    "have not been seen")
            members.extend(_struct_member_rows(
                module, label, mt, kind, reg, binding, off[0],
                1 if i in touched else 0))
            continue
        if _struct_array(module, mt) is not None:
            if unref:
                raise NotEstablished(
                    "an unreferenced block with an array-of-struct member: "
                    "its rows have not been seen")
            members.extend(_array_of_struct_rows(
                module, name, mname, mt, reg, binding, off[0], i in touched))
            continue
        if is_array:
            # An ARRAY member prints its name with [0] and leaves the
            # semantic column EMPTY -- the correlation is exact over 37,948
            # such lines: array member <-> empty semantic, no exceptions.
            label += "[0]"
            sem = ""
        msuf = ""
        if mtype is not None and mtype.opcode == Op.OpTypeMatrix:
            msuf = _matrix_suffix(module, struct_id, i)
        elif is_array and not ENV.get("G2S_NOARRAYCOUNT"):
            msuf = _array_count_suffix(module, mtype)
        use = 1 if ((i, 0) in touched_el if is_array else i in touched) else 0
        if unref:
            # no binding: `BUFFER[-1]` on the semantic, an EMPTY register
            # column (`0072_ce_n29.vert`)
            if sem:
                sem = "%s[-1]" % kind
            members.append((label, type_spelling(module, mt), sem, "", 0))
            unref_off[label] = off[0]
            continue
        members.append((label, type_spelling(module, mt), sem,
                        "%s[%d][%d]%s" % (reg, binding, off[0], msuf), use))
    # Members go in ascending BYTE OFFSET, which is the last `[...]` of the
    # register column -- and a storage-buffer row can carry a `, <n>` stride
    # after it, so the offset is parsed off the bracket rather than off the
    # end of the string.
    return sorted(members, key=lambda r: (unref_off[r[0]] if not r[3]
                                          else _row_offset(r)))


def _buffer_rows(module):
    """Uniform and storage blocks, after everything else: sorted by instance
    name, members in ascending offset (measured, 520/520 and 2484/2484)."""
    blocks, _opaques = _blocks_and_opaques(module)
    out = []
    for block in sorted(blocks):
        out.extend(_block_rows(module, block))
    return out


def _plain_order(row):
    """The order the `#var` block puts the plain rows in.

    Alphabetical by label, EXCEPT that tessellation control's two-sided
    outputs sort as one name with the READ side first: `water_*.tesc` has
    `hs_BINORMAL0[0]` before `hs_BINORMAL0-out`, where a plain string sort
    would put `-out` first (`-` is 0x2d, `[` is 0x5b).  Stripping the side
    off the key and ranking read before write is what reproduces it, and it
    leaves every one-sided label where it was.  (notes/117 §1)
    """
    _l = row[0]
    if _l.endswith("-out"):
        return (_l[:-4], 1, _l)
    if _l.endswith("[0]"):
        return (_l[:-3], 0, _l)
    return (_l, 0, _l)


def _var_line(row):
    name, ty, sem, reg, use = row
    return "#var %s %s : %s : %s : -1 : %d" % (ty, name, sem, reg, use)


def _format_var_lines(module, rows, buffer_rows, used):
    # The SHARED rows come first, as their `#semantic` lines do
    # (`post_tonemap_update.comp`, notes/124 §2).
    out = [_var_line(r) for r in _shared_rows(module, used)]
    out += [_var_line(r) for r in sorted(rows.plain, key=_plain_order)]
    for _, members in sorted(rows.block_members):
        out.extend(_var_line(r) for r in members)
    out.extend(_var_line(r) for r in buffer_rows)
    # The PER-VERTEX input block goes last, after the uniform and storage
    # blocks -- measured on the `water_*.tese` listings, where `gl_in[0].*` is
    # the tail of the `#var` block while the stage's own `gl_PerVertex` output
    # sits with the other interface symbols.
    for _, members in sorted(rows.pv_members):
        out.extend(_var_line(r) for r in members)
    for k, (lname, lty, lvid) in enumerate(_local_arrays(module)):
        out.append("#var %s %s[0] :  : lmem%d[0] : -1 : %d"
                   % (lty, lname, k,
                      1 if lvid is None or lvid in used else 0))
    # `OpKill` gets a symbol of its own -- binding kind 0xd8, reg 0xffffffff
    # (measured) -- and its line goes LAST, after the buffer members, numbered
    # in the order the kills appear.  The type column is `<none>4`, which is
    # what `f_710000415c0` returns for a type code with no name.
    for i in range(_kill_count(module)):
        out.append("#var <none>4 $kill_%04d : $vout.$kill : $kill : -1 : 0"
                   % i)
    return out


def var_lines(module, entry_name="main"):
    """The `#var` block, or NotEstablished for a module it does not cover.

    ORDER: the printer does not sort.  `f_7100bd30b0` walks a singly-linked
    list at `program + 176` head to tail and emits one line per node
    (notes/13-var-order.md).  What the order here reproduces is the CONTENT of
    that list on 607 measured listings, not the mechanism that builds it -- a
    module whose list comes out in another order would be emitted wrongly and
    only the comparison against the oracle would catch it.
    """
    ep = module.entry_point(entry_name)
    model = ep[0]
    rows = _Rows()
    used = _live_ids(module) & set(module.globals)
    # `origin_fragcoord` stands for "the gl_FragCoord line exists whether or
    # not the shader reads it", and the declaration walk consults it instead
    # of a second append here.
    origin_fragcoord = _origin_fragcoord(module, ep)
    declared_fragcoord = any(
        (module.decoration(vid, Decoration.BuiltIn) or (None,))[0]
        == BuiltIn.FragCoord for vid in module.globals)
    if origin_fragcoord and not declared_fragcoord:
        rows.plain.append(_fragcoord_row(module, model))
    for vid, ins in module.globals.items():
        _global_rows(module, model, vid, ins, used, origin_fragcoord, rows)
    buffer_rows = _buffer_rows(module)
    if (not rows.plain and not rows.block_members and not buffer_rows
            and not rows.pv_members and not _local_arrays(module)):
        raise NotEstablished("no #var lines: nothing to emit")
    return _format_var_lines(module, rows, buffer_rows, used)
