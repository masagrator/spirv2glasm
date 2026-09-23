"""blocks.py -- uniform and storage blocks, and opaque uniforms.

An opaque uniform (a texture, a sampler) is a bindless handle in constant
buffer 14; a block is a `BUFFER` or `SBO_BUFFER` symbol whose members are
rows of the `#var` block, keyed by byte offset.
"""
from spvnames import Op, StorageClass, Decoration

from glasmlib.common import NotEstablished, ARRAY_TYPES

# The bindless-handle windows in constant buffer 14 (notes/10).  Measured on
# probes that vary only the binding, in both directions.
HANDLE_BASE = {Op.OpTypeSampledImage: 0,    # a combined sampler
               Op.OpTypeImage: 328,         # a separate texture
               Op.OpTypeSampler: 1352}      # a separate sampler

# The constant buffer the handles live in, and the size of one handle.
HANDLE_BUFFER = 14
HANDLE_SIZE = 8

BLOCK_STORAGE = (StorageClass.Uniform, StorageClass.StorageBuffer)


# A STORAGE IMAGE (OpTypeImage with Sampled 2: `image2D`, `uimageBuffer`)
# has a window of its own at 256 (notes/110): `0110_si_a.comp` binding 0 ->
# `buffer[14][256]`, `0110_si_b.comp` bindings 1 and 3 -> 264 and 280,
# `0110_si_c.frag` binding 2 -> 272, `0110_si_e.comp` an unused binding 4 -> 288 and a
# `uimageBuffer` at binding 2 -> 272.  A sampled `texture2D` (Sampled 1)
# stays at 328.
STORAGE_IMAGE_BASE = 256


def _storage_image(module, pointee):
    """True for an OpTypeImage whose Sampled operand is 2."""
    t = module.types.get(pointee)
    return (t is not None and t.opcode == Op.OpTypeImage
            and t.operands[6] == 2)


def _opaque_offset(module, pointee):
    t = module.types.get(pointee)
    if t is None or t.opcode not in HANDLE_BASE:
        return None
    if t.opcode == Op.OpTypeImage:
        if t.operands[6] == 2:
            return STORAGE_IMAGE_BASE
        if t.operands[6] != 1:
            raise NotEstablished("an OpTypeImage whose Sampled operand is "
                                 "neither 1 nor 2")
    return HANDLE_BASE[t.opcode]


def _block_kind(module, vid, storage, struct_id):
    """`BUFFER` (a uniform block) or `SBO_BUFFER` (a storage block).

    A `buffer` block written for OpenGL-flavoured SPIR-V is a Uniform variable
    whose struct carries BufferBlock; the Vulkan spelling is a StorageBuffer
    variable.  Both reach this compiler (notes/03), so both are tested.
    """
    if storage == StorageClass.StorageBuffer:
        return "SBO_BUFFER", "sbo_buffer"
    if module.decoration(struct_id, Decoration.BufferBlock) is not None:
        return "SBO_BUFFER", "sbo_buffer"
    return "BUFFER", "buffer"


def _global_pointee(module, ins):
    """The pointee type id of a global, or None when its type is not a
    pointer."""
    ptr = module.types.get(ins.result_type)
    if ptr is None or ptr.opcode != Op.OpTypePointer:
        return None
    return ptr.operands[2]


def _opaque_entry(module, vid, pointee, name):
    base = _opaque_offset(module, pointee)
    if base is None:
        raise NotEstablished("a UniformConstant that is not an image, "
                             "sampler or sampled image")
    binding = module.decoration(vid, Decoration.Binding)
    if binding is None or name is None:
        raise NotEstablished("an opaque uniform with no Binding or name")
    return (name, base + HANDLE_SIZE * binding[0])


def _block_entry(module, vid, storage, pointee, name):
    st = module.types.get(pointee)
    if st is None or st.opcode != Op.OpTypeStruct:
        raise NotEstablished("a uniform whose type is not a struct")
    if not name:
        # An anonymous block is named `__defaultname_<n>` -- and <n> is the
        # variable's own SPIR-V <id>, not an internal counter.  (The literal
        # is at 0x7100fd3438, inside the variable handler.)  Verified on 2,333
        # blocks across the corpus, zero wrong; both the #semantic and #var
        # blocks are SORTED by this name, so getting it right is not
        # cosmetic.
        name = "__defaultname_%d" % vid
    binding = module.decoration(vid, Decoration.Binding)
    if binding is None:
        raise NotEstablished("a uniform block with no Binding")
    kind, reg = _block_kind(module, vid, storage, pointee)
    tname = module.name_of(pointee)
    if tname is None:
        raise NotEstablished("a uniform block whose struct has no name")
    return (name, tname, kind, reg, binding[0], pointee, vid)


def _blocks_and_opaques(module):
    """Uniform/storage blocks and opaque uniforms, keyed by symbol name.

    Blocks are `(name, struct name, kind, register word, binding, struct id,
    variable id)`, opaques `(name, handle offset)`.
    """
    blocks, opaques = [], []
    for vid, ins in module.globals.items():
        storage = ins.operands[2]
        pointee = _global_pointee(module, ins)
        if pointee is None:
            continue
        name = module.name_of(vid)
        if storage == StorageClass.UniformConstant:
            opaques.append(_opaque_entry(module, vid, pointee, name))
        elif storage in BLOCK_STORAGE:
            blocks.append(_block_entry(module, vid, storage, pointee, name))
    return blocks, opaques


def _struct_array(module, tid):
    """If this type is an array of struct, the struct's id; else None."""
    t = module.types.get(tid)
    if t is None or t.opcode not in ARRAY_TYPES:
        return None
    inner = module.types.get(t.args()[0])
    return (t.args()[0] if inner is not None
            and inner.opcode == Op.OpTypeStruct else None)


# THE `, <n>` SUFFIX ON A STORAGE-BUFFER ROW.
#
# `f_7100bd2370` prints it from its FIFTH argument with the format `, %d`
# (0x71011630c3, at 0x7100bd2b68), and the only caller that passes a non-zero
# one is the member recursion at 0x7100bd2c40:
#
#     bd2c40:  ldr  x8, [x21, #152]        ; the parent symbol's child count
#     bd2c44:  cmp  x8, #0x1
#     bd2c48:  csel w4, w8, wzr, gt        ; pass it only when it is > 1
#
# so the number is a CHILD COUNT.  What that count is in terms of the module is
# measured, not read: over the corpus it is the array's `ArrayStride` divided
# by four -- 576 -> 144, 580 -> 145, 176 -> 44 -- and it is absent for 128
# (32) and 112 (28).  Every value the corpus prints is above 32 and every one
# it omits is at or below it, so the threshold is written as 32 here and is the
# part of this rule to distrust first if a row ever disagrees.
_STRIDE_FLOOR = 32


def _stride_suffix(stride):
    if stride is None:
        return ""
    n, rem = divmod(stride[0], 4)
    if rem or n <= _STRIDE_FLOOR:
        return ""
    return ", %d" % n


def _row_offset(row):
    """The byte offset in a buffer row's register column."""
    reg = row[3].split(",", 1)[0]
    return int(reg.rsplit("[", 1)[1].rstrip("]"))
