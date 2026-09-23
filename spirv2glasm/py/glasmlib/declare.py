"""declare.py -- the declaration block: STORAGE/CBUFFER, ATTRIB/OUTPUT, colours.

`f_7100bdaef0` builds the ATTRIB / OUTPUT lines from the (kind, reg) pair on
each interface symbol, and `f_7100bd4810` turns that pair into text.  notes/16
has the printer's side (py/binding.py is its transcription) and notes/17 the
assignment side -- which kind a stage's inputs and outputs get and which slot
each built-in occupies.  What follows is that algorithm, with the SPIR-V
module standing in for the symbol table.
"""
from spvnames import Op, ExecutionModel, StorageClass, BuiltIn, Decoration

from glasmlib.common import NotEstablished, ARRAY_TYPES
from glasmlib.semantics import STAGE_KIND, BUILTIN_SLOT, QUALIFIERS, \
    TESC_PATCH_KIND, TESC_PER_VERTEX_KIND, COMPUTE_INPUT_KIND, _interp_bits, \
    _cull_slot
from glasmlib.usage import _live_ids, _is_array, _element_uses, \
    _member_element_uses, _unreferenced_block
from glasmlib.blocks import HANDLE_BUFFER, BLOCK_STORAGE, _opaque_offset, \
    _block_kind

# The ATTRIB/OUTPUT loop walks the slots from this one down.
_TOP_SLOT = 147
# The bit (of the sixteen kinds `binding.KIND_BIT` numbers) whose lines carry
# interpolation qualifiers: kind 0x37, the per-vertex and fragment inputs.
_QUALIFIED_BIT = 0
_FRAGMENT_INPUT_KIND = 0x37


def _output_kind(module, model, vid, kout):
    """The binding kind of an Output variable."""
    if model == ExecutionModel.TessellationControl:
        # Tessellation control has two output kinds and the Patch decoration
        # picks between them: `gl_TessLevelInner` and `gl_TessLevelOuter`
        # are Patch and take 0xbd, everything else is per-vertex and takes
        # 0xb7 (measured on 0007_ts_ctrl.tesc).
        return (TESC_PATCH_KIND
                if module.decoration(vid, Decoration.Patch) is not None
                else TESC_PER_VERTEX_KIND)
    return kout


def _builtin_bindings(module, model, vid, kind, bits, bi, out):
    """A standalone built-in variable's slots."""
    slots = BUILTIN_SLOT.get(kind, {})
    # `gl_FragDepth` does not take the stage's output kind: it gets 0xd2 with
    # reg 0xffffffff (measured), and 0xd2 has neither an arm in
    # `f_7100bd4810` nor a colour-output arm, so it contributes no
    # declaration at all.
    if bi[0] == BuiltIn.FragDepth and model == ExecutionModel.Fragment:
        return
    if bi[0] not in slots:
        raise NotEstablished(
            "built-in %d has no established slot in kind 0x%02x"
            % (bi[0], kind))
    # A STANDALONE built-in that is an ARRAY -- `gl_TessLevelOuter`,
    # `gl_TessLevelInner` -- takes one slot per element, and only the
    # elements the shader touches, the same way a block member does.  The
    # array test matters: `gl_FragCoord` is a vector and its component access
    # chains are not slots, so indexing it would put `.y` and `.z` in the clip
    # family.
    if _is_array(module, vid):
        for e in sorted(_element_uses(module, vid)):
            out.setdefault(slots[bi[0]] + e, []).append((kind, bits))
    else:
        out.setdefault(slots[bi[0]], []).append((kind, bits))


def _block_bindings(module, vid, kind, bits, out):
    """A built-in block's member slots.

    Every member carries its own BuiltIn, and gl_PerVertex is the only one
    any probe or corpus shader has.  The classifier's gate is per SYMBOL and a
    block member is its own symbol, so the `used` test has to be per member
    too -- otherwise a vertex shader that writes only gl_Position would also
    declare its clip and cull slots."""
    slots = BUILTIN_SLOT.get(kind, {})
    used = _member_element_uses(module, vid)
    # An output block whose members carry Location instead of BuiltIn -- what
    # a translated HLSL varying struct looks like -- puts each member at its
    # own slot.
    for m, loc in module.member_locations(vid):
        if any(mm == m for mm, _e in used):
            out.setdefault(loc, []).append((kind, bits))
    members = module.member_builtins(vid)
    # The cull slots start immediately after the clip slots that are actually
    # used: `program[1328]` is that count, and the cull arm of `f_7100bd4810`
    # subtracts it (0x7100bd523c).  Measured both ways -- clip[2] fully used
    # puts cull at 0x44, clip[3] with one element used puts it at 0x43.
    n_clip = sum(1 for m, e in used
                 if any(mm == m and bb == BuiltIn.ClipDistance
                        for mm, bb in members))
    for m, b in members:
        for mm, e in sorted(used):
            if mm != m:
                continue
            if b == BuiltIn.CullDistance:
                out.setdefault(_cull_slot(kind, n_clip + e), []).append(
                    (kind, bits))
                continue
            if b not in slots:
                raise NotEstablished(
                    "built-in %d in an interface block has no established "
                    "slot in kind 0x%02x" % (b, kind))
            # Only an ARRAY member spreads over slots.  A vector member --
            # `gl_Position` -- takes one slot however many components an
            # access chain names, and expanding it declares `result.color`,
            # `result.color.secondary` and `result.color.back` for a shader
            # that writes `.xyzw`.
            if not module.member_is_array(vid, m):
                e = 0
            out.setdefault(slots[b] + e, []).append((kind, bits))


def interface_bindings(module, entry_name="main"):
    """Every (kind, slot, bits) the front end would put on this module.

    Returns a dict slot -> list of (kind, bits), which is the same information
    `f_7100bdcc20` accumulates into its 149-entry table, plus the interpolation
    bits the merge loop needs.

    The classifier's second test is `sym[13] & 1`, which is bit 8 of the flag
    word -- the SAME bit the `#var` line prints as its `used` column.  So the
    declaration block covers only the symbols a reference node was built for,
    and an interface variable the shader never touches contributes nothing.
    `_live_ids` is this converter's approximation of that bit (notes/14), so
    it is what gates the walk here too.
    """
    ep = module.entry_point(entry_name)
    model = ep[0]
    if model == ExecutionModel.GLCompute:
        # A COMPUTE program's interface is its built-in inputs, kind 0x68,
        # and the declaration printer `f_7100bd4810` (py/binding.py) has no
        # arm for that kind -- `binding_form(0x68, slot)` is empty for every
        # slot -- so they print no ATTRIB line (notes/111: `0111_cb_a.comp` reads
        # all five and prints none; `cp_a`..`cp_g` read none).  Anything
        # else live in Input/Output is refused.
        _live = _live_ids(module)
        _slots = BUILTIN_SLOT.get(COMPUTE_INPUT_KIND, {})
        for vid, ins in module.globals.items():
            if (ins.operands[2] not in (StorageClass.Input,
                                        StorageClass.Output)
                    or vid not in _live):
                continue
            bi = module.decoration(vid, Decoration.BuiltIn)
            if (ins.operands[2] != StorageClass.Input or bi is None
                    or bi[0] not in _slots):
                raise NotEstablished(
                    "a compute program's interface variable other than a "
                    "measured built-in input")
        return {}
    if model not in STAGE_KIND:
        raise NotEstablished(
            "execution model %d has no interface binding rule" % model)
    kin, kout = STAGE_KIND[model]
    live = _live_ids(module)
    out = {}
    for vid, ins in module.globals.items():
        storage = ins.operands[2]
        if storage == StorageClass.Input:
            kind = kin
        elif storage == StorageClass.Output:
            kind = _output_kind(module, model, vid, kout)
        else:
            continue
        if vid not in live:
            continue
        bits = _interp_bits(module, vid)
        loc = module.decoration(vid, Decoration.Location)
        bi = module.decoration(vid, Decoration.BuiltIn)
        if loc is not None:
            out.setdefault(loc[0], []).append((kind, bits))
        elif bi is not None:
            _builtin_bindings(module, model, vid, kind, bits, bi, out)
        else:
            _block_bindings(module, vid, kind, bits, out)
    return out


def _merge_low(kind, slots, slot, bits, base, is_cull):
    """The lowest slot a merged range starting at `slot` reaches.

    Only two arms merge.  Kind 0x07 walks down to the family base (or to the
    first slot with a different owner or size class) and kind 0x37 stops as
    soon as the interpolation changes.  Every other family arm reaches
    0x7100bd4c88 / 0x7100bd5338, which set the range's first index to ZERO
    unconditionally -- so a vertex shader with a `flat` output still prints
    one `result_attrib[] = { result.attrib[0..N] }`."""
    if is_cull:
        # `*p5` is program[1328], the number of clip elements in use, which
        # is the index of the LOWEST cull slot present.
        return min(x for x in slots if base <= x <= base + 7)
    if kind == _FRAGMENT_INPUT_KIND:
        lo = slot
        while lo > base and slots.get(lo - 1, bits) == bits:
            lo -= 1
        return lo
    return base


def _kind_lines(binding, bit, slots):
    """The ATTRIB/OUTPUT lines of one kind, from the top slot down."""
    kind = binding.BIT_KIND[bit]
    reg_extra = binding.BIT_REG[bit]
    word = "ATTRIB" if (1 << bit) & binding.INPUT_BITS else "OUTPUT"
    lines = []
    slot = _TOP_SLOT
    while slot >= 0:
        if slot not in slots:
            slot -= 1
            continue
        form = binding.binding_form(kind, reg_extra | slot, ".")
        if form is None:               # a kind with no arm: no line
            slot -= 1
            continue
        text, base, is_cull = form
        bits = slots[slot]
        # The qualifiers are emitted ONLY on bit 0 -- kind 0x37, the
        # per-vertex and fragment inputs.  `f_7100bdaef0` reaches the
        # FLAT/CENTROID/NOPERSPECTIVE/SAMPLE/PERVERTEX block through
        # `cbz w23, bdb17c`, so every other bit takes the path at 0x7100bdb19c
        # which has no qualifier code at all: a vertex shader with a `flat`
        # OUTPUT still prints a bare `OUTPUT`.
        quals = ("".join("%s " % q for q, b in QUALIFIERS if bits >> b & 1)
                 if bit == _QUALIFIED_BIT else "")
        if base is None:
            name = binding.binding_name(kind, reg_extra | slot, "_")
            lines.append("%s%s %s = %s;" % (quals, word, name, text))
            slot -= 1
            continue
        lo = _merge_low(kind, slots, slot, bits, base, is_cull)
        name = binding.binding_name(kind, reg_extra | slot, "_")
        # The suffix comes from the value the NAME call leaves in W23, and
        # only the two merge loops leave the range's start there: every other
        # arm passes 0 (0x7100bd4c88) and the cull arm explicitly selects 0
        # when `p5` is absent (0x7100bd5248).  So only kind 0x37 ever carries
        # one.
        if kind == _FRAGMENT_INPUT_KIND and lo - base >= 1:
            name += "%d" % (lo - base)
        lines.append("%s%s %s[] = { %s[%d..%d] };"
                     % (quals, word, name, text, lo - base, slot - base))
        slot = lo - 1
    return lines


def attrib_block(module, entry_name="main"):
    """`f_7100bdaef0`'s ATTRIB / OUTPUT lines.

    The loop is the compiler's: for each kind present, walk the slots from 147
    downward, and at each slot that has the kind either

      * print `name = binding;` when `f_7100bd4810` reports a single binding
        (its arms that return -1), or
      * merge downward, no further than the family's base slot and only while
        the interpolation bits stay the same, and print
        `name[] = { binding[lo..hi] };` -- the whole of the fragment stage's
        several-line split (notes/16).

    The variable name carries the range's first index when that index is >= 1,
    which is what `f_7100bd5340` appends with `%d`.
    """
    import binding

    table = interface_bindings(module, entry_name)
    # Keyed by BIT, not by kind: 0xb7 occupies two bits (8 for the write side,
    # 12 for the `gl_out[]` read side, whose reg carries 0x10000), and keying
    # by kind alone emits every 0xb7 symbol under both.
    kinds = {}
    for slot, entries in table.items():
        for kind, bits in entries:
            b = binding.KIND_BIT[kind]
            kinds.setdefault(b, {})[slot] = bits
    lines = []
    for bit in range(16):
        if bit in kinds:
            lines += _kind_lines(binding, bit, kinds[bit])
    return lines


# The colour-output registers: 0..7 plain, 8..15 secondary.
_SECONDARY = 8
_COLOUR_REGISTERS = 16


def _colour_output_registers(module):
    """The fragment output registers, in declaration order, first
    occurrence winning."""
    live = _live_ids(module)
    regs = []
    for vid, ins in module.globals.items():
        if ins.operands[2] != StorageClass.Output:
            continue
        if vid not in live:
            continue
        loc = module.decoration(vid, Decoration.Location)
        if loc is None:
            # `gl_FragDepth` and the `$kill` pseudo-output have a BuiltIn and
            # no Location, and neither takes a colour-output arm.
            if module.decoration(vid, Decoration.BuiltIn) is not None:
                continue
            raise NotEstablished("a fragment output with no Location")
        if loc[0] >= _COLOUR_REGISTERS:
            raise NotEstablished(
                "fragment output register %d: the table at 0x71011a2f4c only "
                "covers 0..15" % loc[0])
        if loc[0] not in regs:
            regs.append(loc[0])
    return regs


def colour_output_lines(module, entry_name="main"):
    """The fragment colour outputs, from the tail of `f_7100bdaef0`.

    These are NOT part of the ATTRIB/OUTPUT loop: they come from a separate
    walk of `program + 208`, they come after the TEMP block in the listing, and
    their text is chosen by a 16-entry byte table at 0x71011a2f4c indexed by
    the output's register, with base 0x7100bdb8e0:

        0      OUTPUT result_color0 = result.color;
        1-7    OUTPUT result_color%d = result.color[%d];
        8      OUTPUT result_color0_secondary = result.color.secondary;
        9-15   OUTPUT result_color%d_secondary = result.color[%d].secondary;

    each prefixed with `SHORT ` when the program has half registers
    (`program[1376]`) and the symbol's component type is 7.  A register is
    emitted at most once: the walk keeps a 16-byte `seen` array.

    The ORDER is the order of the list, and it is the module's own order of
    declaration -- `chr_cloth_0134fdf9-1.frag` declares SV_Target1..4 before
    SV_Target0 and the listing prints colour 0 last, which ascending order gets
    wrong.
    """
    ep = module.entry_point(entry_name)
    if ep[0] != ExecutionModel.Fragment:
        return []
    out = []
    for r in _colour_output_registers(module):
        if r == 0:
            out.append("OUTPUT result_color0 = result.color;")
        elif r < _SECONDARY:
            out.append("OUTPUT result_color%d = result.color[%d];" % (r, r))
        elif r == _SECONDARY:
            out.append("OUTPUT result_color0_secondary = "
                       "result.color.secondary;")
        else:
            out.append("OUTPUT result_color%d_secondary = "
                       "result.color[%d].secondary;"
                       % (r - _SECONDARY, r - _SECONDARY))
    return out


def _buffer_bindings(module):
    """(storage bindings, constant-buffer bindings) that get a line."""
    sbo, cbuf = set(), set()
    for vid, ins in module.globals.items():
        storage = ins.operands[2]
        ptr = module.types.get(ins.result_type)
        if ptr is None or ptr.opcode != Op.OpTypePointer:
            continue
        pointee = ptr.operands[2]
        binding_ = module.decoration(vid, Decoration.Binding)
        if storage == StorageClass.UniformConstant:
            if _opaque_offset(module, pointee) is not None:
                cbuf.add(HANDLE_BUFFER)
            continue
        if storage in BLOCK_STORAGE:
            if binding_ is None:
                raise NotEstablished("a block with no Binding")
            # An ARRAY of blocks becomes a GROUP, and a group prints the
            # ranged form -- `sbo_buf%d[][] = { program.storage[%d..%d] }` --
            # whose extent comes from the program's own record table
            # (notes/15).  Which bindings one covers has not been checked
            # against the compiler, so such a module is refused rather than
            # given the single-slot line.
            pt = module.types.get(pointee)
            if pt is not None and pt.opcode in ARRAY_TYPES:
                raise NotEstablished(
                    "an array of blocks: it prints the grouped, ranged "
                    "STORAGE/CBUFFER form and the range has not been checked")
            kind, _reg = _block_kind(module, vid, storage, pointee)
            if _unreferenced_block(module, vid, kind):
                continue                # no binding, no CBUFFER line
            (sbo if kind == "SBO_BUFFER" else cbuf).add(binding_[0])
    return sbo, cbuf


def _refuse_atomic_counters(module):
    """`f_7100bdabf0` runs between the comment block and the ATTRIB loop and
    is ONLY the atomic counters: eight unrolled slots reading
    `program[900 + 4*i]`, each emitting `COUNTER atomic_counter%d[] = {
    program.counter[%d] };` when the slot is >= 0.  The line is exact; which
    SPIR-V binding lands in which slot is not, and no probe or corpus shader
    has one -- so a module that does is refused rather than quietly missing
    its COUNTER lines."""
    for vid, ins in module.globals.items():
        if ins.operands[2] == StorageClass.AtomicCounter:
            raise NotEstablished(
                "an atomic counter: the COUNTER line's shape is read "
                "(f_7100bdabf0) but which of the eight program.counter slots a "
                "binding takes is not")


def declarations(module, entry_name="main"):
    """`STORAGE` / `CBUFFER`, the `ATTRIB`/`OUTPUT` block, then TEMP.

    `f_7100bdaef0` emits, in order: the profile line and OPTIONs, the stage
    directives, the `#var`/`#semantic` comments, the STORAGE and CBUFFER lines
    (`f_7100bdabf0`), the ATTRIB/OUTPUT block, the TEMP block, `lmem`, the
    IMAGE line, the fragment colour outputs and the subroutines.

        STORAGE sbo_buf%d[] = { program.storage[%d] };   one per storage block
        CBUFFER buf%d[]     = { program.buffer[%d] };    one per uniform block

    Both lists are in ascending binding order and storage comes before uniform
    (607/607 listings).  This is a SUBSET of the real rule: `f_7100d56850`
    emits a RANGED form (`sbo_buf%d[][] = { program.storage[%d..%d] }`) when
    consecutive entries of its binding table share a group, and a
    `PARAM sbo_storage_len%d[]` line when an entry asks for it (notes/15).  No
    shader in the 607 measured listings has either, so neither is emitted here.
    Buffer 14 gets a `CBUFFER` line whenever the shader has any opaque uniform,
    because that is where their handles live (notes/10).
    """
    from glasmlib.lower import lower

    sbo, cbuf = _buffer_bindings(module)
    _refuse_atomic_counters(module)
    # SHARED MEMORY comes before STORAGE (`post_tonemap_update.comp`):
    #     SHARED_MEMORY 512;
    #     SHARED shared_mem[] = { program.sharedmem };
    # 512 is the variable's size in BYTES -- 128 elements of one uint.
    # (notes/124 §2)
    from glasmlib.varblock import _shared_arrays
    _sh = _shared_arrays(module)
    out = []
    if _sh:
        out.append("SHARED_MEMORY %d;" % (4 * sum(n for _n, _v, n in _sh)))
        out.append("SHARED shared_mem[] = { program.sharedmem };")
    out += ["STORAGE sbo_buf%d[] = { program.storage[%d] };" % (b, b)
            for b in sorted(sbo)]
    out += ["CBUFFER buf%d[] = { program.buffer[%d] };" % (b, b)
            for b in sorted(cbuf)]
    out += attrib_block(module, entry_name)
    # The TEMP block and the colour outputs come after the ATTRIB lines, and
    # the TEMP counts are the register allocator's result (notes/16), so they
    # can only be emitted for a body this converter has lowered itself.  When
    # the body raises, this returns the PREFIX it always did -- which is what
    # keeps the measured declaration numbers unchanged for every shader whose
    # body is still out of reach.
    try:
        lowered = lower(module, entry_name)
    except NotEstablished:
        return out                      # the prefix, exactly as before
    out += lowered.temp_block()
    out += colour_output_lines(module, entry_name)
    return out
