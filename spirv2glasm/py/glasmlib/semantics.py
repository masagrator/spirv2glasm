"""semantics.py -- binding kinds, built-in slots, and `#var` semantics.

The compiler gives every interface symbol a (kind, register) pair: the KIND
is the stage and direction (notes/17), the register a slot within it.  The
`#var` line prints two views of that pair -- the semantic (`$vout.POSITION`)
and the register (`HPOS[32]`) -- and the ATTRIB/OUTPUT block prints a third
(py/binding.py).  The binding kinds (0x07, 0x6f, ...) are the compiler's own
numbering, written in hex as the notes read them.
"""
from spvnames import ExecutionModel, StorageClass, BuiltIn, Decoration

from glasmlib.common import NotEstablished

# gl_PerVertex and its friends: the built-in blocks whose members keep their
# DECLARATION order instead of sorting (measured, 607/607).
_BLOCK_MEMBERS = ("gl_Position", "gl_PointSize", "gl_ClipDistance",
                  "gl_CullDistance", "gl_TessLevelInner", "gl_TessLevelOuter")

# Built-in -> (semantic, register).  notes/12-builtin-semantics.md; the table
# there is the same data with the counts.  Only the stage-independent ones are
# here; anything else raises, because a wrong semantic is worse than a refusal.
BUILTIN_SEMANTIC = {
    "gl_Position":      ("$vout.POSITION", "HPOS"),
    "gl_PointSize":     ("$vout.PSIZE",    "PSIZ"),
    "gl_ClipDistance":  ("$vout.CLP0",     "CLP0"),
    "gl_CullDistance":  ("$vout.CUL0",     "CUL0"),
    "gl_FragCoord":     ("$vin.WPOS",      "WPOS"),
    "gl_FrontFacing":   ("$vin.FACE_FLAT", "SSA"),
    "gl_TessCoord":     ("$vin.TESSCOORD", "TESSCOORD"),
    "gl_InvocationID":  ("$vin.THREAD_ID", "INVOCATIONID"),
    "gl_InstanceIndex": ("$vin.INSTANCEINDEX", "INSTANCEIDX"),
    # the compute stage's inputs (notes/111, `cb_a.comp`: all five read)
    "gl_NumWorkGroups":        ("$vin.GBLSIZE", "GBLSIZE"),
    "gl_WorkGroupID":          ("$vin.CTAID",   "CTAID"),
    "gl_LocalInvocationID":    ("$vin.LCLID",   "LCLID"),
    "gl_GlobalInvocationID":   ("$vin.GBLID",   "GBLID"),
    "gl_LocalInvocationIndex": ("$vin.LCLIDX",  "LCLIDX"),
}

# The SPIR-V `BuiltIn` behind each of those names, so the register column can
# find the built-in's SLOT in `BUILTIN_SLOT` (which is keyed by binding kind
# and BuiltIn, not by name).  (VertexId and InstanceId are NOT the same
# numbers as VertexIndex and InstanceIndex.)
BUILTIN_NUMBER = {
    "gl_Position": BuiltIn.Position, "gl_PointSize": BuiltIn.PointSize,
    "gl_ClipDistance": BuiltIn.ClipDistance,
    "gl_CullDistance": BuiltIn.CullDistance,
    "gl_InvocationID": BuiltIn.InvocationId,
    "gl_TessCoord": BuiltIn.TessCoord, "gl_FragCoord": BuiltIn.FragCoord,
    "gl_FrontFacing": BuiltIn.FrontFacing,
    "gl_InstanceIndex": BuiltIn.InstanceIndex,
    "gl_NumWorkGroups": BuiltIn.NumWorkgroups,
    "gl_WorkGroupID": BuiltIn.WorkgroupId,
    "gl_LocalInvocationID": BuiltIn.LocalInvocationId,
    "gl_GlobalInvocationID": BuiltIn.GlobalInvocationId,
    "gl_LocalInvocationIndex": BuiltIn.LocalInvocationIndex,
}

# THE REGISTER COLUMN CARRIES AN INDEX FOR EVERY KIND BUT THREE.
#
# notes/20 read the SEMANTIC column's printer and found it singles out exactly
# `0x07`, `0x6f` and `0x37` -- the vertex input, vertex output and fragment
# input kinds -- for a form of their own.  The register column splits on the
# same three: those kinds print the register's bare NAME (`ATTR3`, `HPOS`,
# `WPOS`, `SSA`) and every other kind appends the slot (`ATTR3[3]`,
# `HPOS[32]`, `PSIZ[48]`, `TESSCOORD[60]`, `COL0[0]`).
#
# Measured against the corpus: it is what turns the five `water_*.tese`
# disagreements (`ATTR3` where the compiler prints `ATTR3[3]`) into agreement,
# and it leaves every vertex and fragment line unchanged, which is why the
# three-kind exception is not a guess fitted to one stage.
# READ, not fitted: `f_7100041080`'s kind switch (0x7100411e8..0x71000411244)
# sends kinds 0x07, 0x37, 0x63, 0x6f and 0xc0 to 0x71000411304, which sets the
# index to -1 -- "a negative index drops the number entirely" -- and every
# other kind to 0x71000411328, which keeps `sym[144]`.  0x30 is the one
# two-index kind.  So the exempt set is five kinds, not the three the corpus
# happened to show, and geometry OUTPUT (0xc0) is exempt while geometry INPUT
# (0x30) is not.
UNINDEXED_KIND = (0x07, 0x37, 0x63, 0x6f, 0xc0)

# The geometry stage's input kind, the one kind with two indices.
GEOMETRY_INPUT_KIND = 0x30

# A built-in whose GLASM symbol does not have the SPIR-V type.  `gl_FrontFacing`
# is `bool` in SPIR-V and the listing calls it `int`: the front end gives the
# symbol an integer type because the register (`SSA`) holds one.  Measured on
# every corpus fragment shader that reads it; nothing else in the corpus
# disagrees with its SPIR-V type.
BUILTIN_TYPE = {"gl_FrontFacing": "int"}

# notes/17.  The index is the execution model; the value is
# (input kind, output kind).  Tessellation control has two output kinds and is
# handled separately, and compute has no interface of this shape at all.
STAGE_KIND = {
    ExecutionModel.Vertex: (0x07, 0x6f),
    # tessellation control's outputs: 0xb7 per-vertex, 0xbd per-patch
    ExecutionModel.TessellationControl: (0x33, None),
    ExecutionModel.TessellationEvaluation: (0x35, 0xbb),
    ExecutionModel.Geometry: (0x30, 0xc0),
    ExecutionModel.Fragment: (0x37, 0xcf),
    # compute: built-in inputs only, kind 0x68 (`g2s_trace_sym` on
    # `cb_a.comp`: bindkind148=0x68 for all five), and no outputs
    ExecutionModel.GLCompute: (0x68, None),
}

# The compute stage's input kind (notes/111).
COMPUTE_INPUT_KIND = 0x68

# Tessellation control's two output kinds.
TESC_PER_VERTEX_KIND = 0xb7
TESC_PATCH_KIND = 0xbd

# The built-in slots, per notes/17 -- keyed by KIND, because the numbering is
# not the same in every kind.  `gl_CullDistance` is the case that forces this:
# 0x42 in the vertex output kind (1,178 corpus shaders, no disagreement) and
# 0x49 in the geometry input kind.  A kind/built-in pair that is absent here is
# refused rather than guessed, which is why the geometry OUTPUT cull slot is
# missing: one probe gives 0x43 and one observation is not a rule.
BUILTIN_SLOT = {
    0x07: {BuiltIn.VertexIndex: 0x3e, BuiltIn.InstanceIndex: 0x3f},
    0x6f: {BuiltIn.Position: 0x20, BuiltIn.PointSize: 0x30,          # vertex
           BuiltIn.ClipDistance: 0x31, BuiltIn.CullDistance: 0x42},  # outputs
    0x37: {BuiltIn.FragCoord: 0x2d, BuiltIn.FrontFacing: 0x36},     # frag in
    0x30: {BuiltIn.Position: 0x20, BuiltIn.PointSize: 0x30,          # geometry
           BuiltIn.ClipDistance: 0x31, BuiltIn.CullDistance: 0x49},  # inputs
    0xc0: {BuiltIn.Position: 0x20, BuiltIn.PointSize: 0x30,          # geometry
           BuiltIn.ClipDistance: 0x31},                              # outputs
    0x33: {BuiltIn.Position: 0x20, BuiltIn.InvocationId: 0x3b},     # tesc in
    # The tessellation-evaluation input kind's per-vertex block, read off the
    # register column: `HPOS[32]`, `PSIZ[48]`, `CLP0[49]`, `TESSCOORD[60]`.
    0x35: {BuiltIn.Position: 0x20, BuiltIn.PointSize: 0x30,
           BuiltIn.ClipDistance: 0x31, BuiltIn.TessCoord: 0x3c},
    # Tessellation evaluation outputs.  The slots are the same numbers the
    # vertex output kind uses (0x20 position, 0x30 point size, 0x31 clip),
    # read off the register column the oracle prints for this kind --
    # `HPOS[32]`, `PSIZ[48]`, `CLP0[49]`.  gl_CullDistance is left out: its
    # slot moves with the clip count (CULL_BASE below) and one measurement
    # of `CUL0[64]` does not separate the base from the count.
    0xbb: {BuiltIn.Position: 0x20, BuiltIn.PointSize: 0x30,
           BuiltIn.ClipDistance: 0x31},
    0xbd: {BuiltIn.TessLevelInner: 0x20, BuiltIn.TessLevelOuter: 0x22},
    0xb7: {BuiltIn.Position: 0x20},                         # per-vertex out
    # compute inputs: `g2s_trace_sym`'s reg144 on `cb_a.comp` -- 0
    # NumWorkGroups, 1 WorkGroupID, 2 LocalInvocationID, 3
    # GlobalInvocationID, 4 LocalInvocationIndex -- which are the slots the
    # instruction printer's kind-0x68 arm names `invocation.groupcount`,
    # `.groupid`, `.localid`, `.globalid`, `.localindex` (opname.COMPUTE_IN)
    0x68: {BuiltIn.NumWorkgroups: 0, BuiltIn.WorkgroupId: 1,
           BuiltIn.LocalInvocationId: 2, BuiltIn.GlobalInvocationId: 3,
           BuiltIn.LocalInvocationIndex: 4},
}

# Uniform and storage blocks take a kind of their own -- 0x170 + binding for a
# uniform buffer and 0x1c0 + binding for a storage buffer -- with the member's
# BYTE OFFSET as the register rather than a slot.  Neither range has an arm in
# `f_7100bd4810`, so neither produces an ATTRIB or OUTPUT line; they are here
# because the `#var` register column (`BUFFER[14][328]`) is that same pair.
# The cull families start at these slots, and the index the listing prints is
# relative to `program[1328]` -- the number of clip elements in use -- so the
# slot of `gl_CullDistance[e]` is `CULL_BASE + used_clip_count + e`.
# 0xbb and 0x35 are read off the `#var` register column, which prints the slot
# outright for every kind but three (UNINDEXED_KIND above): the five
# `water_*.tese` shaders put `CLP0[49]` and `CUL0[64]` on the OUTPUT side and
# `CLP0[49]` / `CUL0[74]` on the input side, with the clip array UNUSED, so the
# `+ used clip count` term is zero and the bases are 0x40 and 0x4a outright.
# 0xbb was 0x42 here before, copied from the vertex output kind rather than
# measured; nothing in the corpus had exercised it.
CULL_BASE = {0x6f: 0x42, 0xc0: 0x42, 0xbb: 0x40, 0x35: 0x4a,
             0x30: 0x49, 0x37: 0x4c}

UBO_KIND_BASE = 0x170
SSBO_KIND_BASE = 0x1c0

# sym[12] bits 11, 14, 17, 19 and sym[16] bit 16, the five the merge loop and
# the qualifier printer look at, in the order `f_7100bdaef0` emits them.
QUALIFIERS = (("FLAT", 11), ("CENTROID", 14), ("NOPERSPECTIVE", 17),
              ("SAMPLE", 19), ("PERVERTEX", 16))
DECOR_BIT = {Decoration.Flat: 11, Decoration.Centroid: 14,
             Decoration.NoPerspective: 17, Decoration.Sample: 19}


def _register(kind, name, slot):
    """The `#var` register column for a symbol with this binding pair.

    `f_7100041080` chooses the bracket by KIND (0x411e8, notes/20): kind
    0x30 -- the geometry stage's inputs -- takes `%s%s[%d][%d]` with the
    register word's byte 1 (the vertex) then byte 0 (the slot), 0x41314;
    the kinds of UNINDEXED_KIND take no index; every other kind prints the
    whole word, 0x41328.
    """
    if kind in UNINDEXED_KIND or slot is None:
        return name
    if kind == GEOMETRY_INPUT_KIND:
        return "%s[%d][%d]" % (name, (slot >> 8) & 0xff, slot & 0xff)
    return "%s[%d]" % (name, slot)


def _location_semantic(model, storage, loc, flat):
    """A user in/out variable's semantic and register, from its Location.

    Checked against 4,511 such lines over the corpus with zero disagreement:

        input            $vin.ATTR<loc>[_FLAT]   ATTR<loc>
        output, non-frag $vout.ATTR<loc>[_FLAT]  ATTR<loc>
        output, fragment $vout.COL<loc>          COL<loc>[<loc>]
    """
    suffix = "_FLAT" if flat else ""
    kind = _stage_kind(model, storage)
    if storage == StorageClass.Input:
        return ("$vin.ATTR%d%s" % (loc, suffix),
                _register(kind, "ATTR%d" % loc, loc))
    if storage == StorageClass.Output:
        if model == ExecutionModel.Fragment:
            return "$vout.COL%d" % loc, _register(kind, "COL%d" % loc, loc)
        return ("$vout.ATTR%d%s" % (loc, suffix),
                _register(kind, "ATTR%d" % loc, loc))
    raise NotEstablished("storage class %d has no established semantic"
                         % storage)


def _stage_kind(model, storage):
    """The binding KIND an interface variable of this stage and direction has.

    notes/17.  Tessellation control's output side is the one that has two
    (`0xb7` per-vertex and `0xbd` per-patch) and is refused rather than
    guessed, which is what the `None` in STAGE_KIND stands for.
    """
    pair = STAGE_KIND.get(model)
    if pair is None:
        raise NotEstablished("execution model %d has no established binding "
                             "kinds" % model)
    kind = pair[0] if storage == StorageClass.Input else pair[1]
    if kind is None:
        raise NotEstablished("this stage's outputs have two binding kinds "
                             "(per-vertex and per-patch) and which one a "
                             "variable gets is unread")
    return kind


def _builtin_semantic(model, storage, name, clip_used=0):
    """The semantic column of a built-in.

    The semantic and the register column are two different name tables --
    `POSITION` against `HPOS`, `PSIZE` against `PSIZ` -- but the same kind
    switch decides whether an index is appended.  Where it is, only the two
    RANGED families show it: measured over the tessellation-evaluation corpus,
    `$vout.CLP049` and `$vin.CUL074` carry the slot and `$vout.POSITION`,
    `$vout.PSIZE`, `$vin.TESSCOORD` and `$vin.VERTEX[0].ATTR3` do not.
    """
    # The `$vin.`/`$vout.` prefix is the symbol's DIRECTION (notes/20: bit 4
    # and bit 5 of `sym[12]`), not a property of the built-in, so a built-in
    # that appears on both sides -- `gl_Position` is `$vout.POSITION` in the
    # block a stage writes and `$vin.VERTEX[0].POSITION` in the `gl_in` block
    # it reads -- takes the prefix from the storage class here.
    sem = BUILTIN_SEMANTIC[name][0]
    want = "$vin." if storage == StorageClass.Input else "$vout."
    sem = want + sem.split(".", 1)[1]
    kind = _stage_kind(model, storage)
    if kind in UNINDEXED_KIND or BUILTIN_NUMBER.get(name) not in (
            BuiltIn.ClipDistance, BuiltIn.CullDistance):
        return sem
    slot = _clip_cull_slot(model, storage, name, clip_used)
    if kind == GEOMETRY_INPUT_KIND:
        # THE SAME KIND SWITCH as the register column (`f_7100041080`,
        # 0x411e8): kind 0x30 takes `%s%s[%d][%d]` (0x41410, the format at
        # 0x114bc53) with the register word's byte 1, the vertex (0 for the
        # `gl_in[0]` row), then byte 0, the slot -- `$vin.CLP0[0][49]`,
        # `$vin.CUL0[0][73]` (g01.geom, notes/65); the other indexed kinds
        # take `%s%s%d` (0x41368, 0x1166651) -- `$vin.CLP049`.
        return "%s[%d][%d]" % (sem, 0, slot)
    return "%s%d" % (sem, slot)


def _cull_slot(kind, clip_used):
    base = CULL_BASE.get(kind)
    if base is None:
        raise NotEstablished("the cull slot base for kind 0x%02x" % kind)
    return base + clip_used


def _clip_cull_slot(model, storage, name, clip_used):
    """The slot of gl_ClipDistance[0] or gl_CullDistance[0] in this kind."""
    kind = _stage_kind(model, storage)
    if BUILTIN_NUMBER[name] == BuiltIn.CullDistance:
        return _cull_slot(kind, clip_used)
    slot = BUILTIN_SLOT.get(kind, {}).get(BuiltIn.ClipDistance)
    if slot is None:
        raise NotEstablished("the clip slot base for kind 0x%02x" % kind)
    return slot


def _builtin_register(model, storage, name, clip_used=0):
    """The register column of a built-in, with the slot its kind gives it."""
    reg = BUILTIN_SEMANTIC[name][1]
    kind = _stage_kind(model, storage)
    if kind in UNINDEXED_KIND:
        return reg
    num = BUILTIN_NUMBER.get(name)
    if num == BuiltIn.CullDistance:
        return _register(kind, reg, _cull_slot(kind, clip_used))
    slot = BUILTIN_SLOT.get(kind, {}).get(num)
    if slot is None:
        raise NotEstablished(
            "built-in %s has no established slot in binding kind 0x%x, and "
            "this kind prints the slot in the register column" % (name, kind))
    return _register(kind, reg, slot)


def _interp_bits(module, vid):
    """The interpolation bits of one interface variable.

    Only the four the compiler merges on; PERVERTEX lives in a different word
    and no probe or corpus shader carries it, so a module that asks for it is
    refused rather than emitted without the qualifier.
    """
    bits = 0
    for d, b in DECOR_BIT.items():
        if module.decoration(vid, d) is not None:
            bits |= 1 << b
    return bits
