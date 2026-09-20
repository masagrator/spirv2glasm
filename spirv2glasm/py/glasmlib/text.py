"""text.py -- how a body line is spelled: operands, swizzles, the line itself.

The operand printer emits a swizzle one character at a time from `"xyzw"`
and omits an all-`x` one (notes/23); the line printer's format is
`%-5s %s, %s, %s;`.  Everything here is that vocabulary.
"""
from glasmlib.common import ENV

_COMPONENTS = ("x", "y", "z", "w")


def _swizzle(text, comp):
    """One component of a source operand.

    Component 0 prints with NO suffix -- `MOV.F R0.x, vertex.attrib[0];` is
    the `.x` read in `p06_swizzle.vert` -- and the rest print `.y`, `.z`,
    `.w`.  That asymmetry is the printer's: the swizzle is emitted one
    character at a time from `"xyzw"` and an all-`x` swizzle is omitted
    (notes/23).
    """
    return text if comp == 0 else "%s.%s" % (text, _COMPONENTS[comp])


def _swizzle_suffix(sel, nres):
    """The swizzle a SOURCE operand prints, given the components it reads and
    the instruction's result width.

    MEASURED, on four probes that vary exactly this:

        sw1_add  v  = a0.yzwx + a1   ADD.F32 R0,    ...[0].yzwx, ...[1];
        sw2_add  v  = a0.xxxx + a1   ADD.F32 R0,    ...[0].x,    ...[1];
        sw3_add  v2 = a0.zw + a0.xy  ADD.F32 R0.xy, ...[0].zwzw, ...[0];
        cf_if    a0.x > 0.0          SGT.F32 R0.x,  ...[0],      {0,0,0,0};

    from which three things follow, and they cover every swizzle in the probe
    set including the stores':

      * it is OMITTED when it is the identity for the RESULT's width -- which
        is why `a0.xy` into a vec2 prints bare and `a0.x` into a scalar
        comparison prints bare, while the same all-`x` selector into a vec4
        does not;
      * otherwise the selector is padded to four;
      * and an all-equal selector collapses to ONE letter (`.xxxx` prints
        `.x`).

    The general rule the printer uses is the selector against the
    destination's mask and has not been read out of the image (notes/41);
    this is what the measurements say, and anything outside them is refused
    by the caller rather than guessed.
    """
    if not sel:
        return ""
    if nres and tuple(sel[:nres]) == tuple(range(nres)):
        return ""
    # THE LANES PAST THE SELECTOR KEEP THEIR OWN COMPONENT (the identity):
    # `mb_n26.vert`'s vec3 `u_xlat0.zxy * u_xlat3.yzx` prints
    # `R11.zxyw, R10.yzxw`, where repeating would give `.zxyz`.
    # (A ONE-component selector is a scalar's, broadcast: `.x`.)
    # (notes/90: a TWO-component `.xz` prints `.xzzw` -- `bv_n40.vert`'s
    # `MUL.F32 R0.xy, R39.xzzw, R1;` -- so two components pad with the
    # identity too; `G2S_REPEAT2=1` restores the repeat.)
    if (len(sel) not in (2, 3) or len(set(sel)) == 1
            or (len(sel) == 2 and ENV.get("G2S_REPEAT2"))):
        pad = (tuple(sel) * 4)[:4]
    else:
        pad = tuple(sel[:4]) + tuple(range(len(sel), 4))
    if len(set(pad)) == 1:
        return "." + _COMPONENTS[pad[0]]
    return "." + "".join(_COMPONENTS[c] for c in pad)


def _source(values, comps, vid, nres=None):
    """A value as a source operand, with its component swizzle applied.

    `nres` is the RESULT's width, which is what decides whether the swizzle
    prints at all (`_swizzle_suffix`).  Without it only the first component is
    applied, which is what this used to do -- and what dropped `.yzwx` to `.y`
    and `.zw` to `.z` on every swizzled operand.
    """
    t = values.get(vid)
    if t is None:
        return None
    c = comps.get(vid)
    if c is None:
        return t
    if nres is None:
        return _swizzle(t, c[0])
    return t + _swizzle_suffix(c, nres)


def _emit(mnem, *operands):
    """One body line, with the mnemonic left-justified in five columns.

    The printer's format is `%-5s %s, %s, %s;` (notes/23), so a mnemonic
    shorter than five characters is PADDED -- `OR.S  R0, ...` has two spaces
    -- and a longer one simply runs on.  Writing every line through here is
    what keeps `OR` and `DP4` from silently losing a space.
    """
    return "%-5s %s;" % (mnem, ", ".join(operands))


# A store into `gl_Position` is SCALARISED, and the shape depends on what the
# source is.  Measured on the probe listings, which show exactly two forms:
#
#   source is a CONSTANT                     source is an interface operand
#   ------------------------                 ------------------------------
#   MOV.F result.position.x, {..};           MOV.F result.position.x, <src>;
#   MOV.F result.position.y, {..}.x;         MOV.F R0.x, <src>.y;
#   MOV.F result.position.z, {..}.x;         MOV.F result.position.y, R0.x;
#   MOV.F result.position.w, {..}.x;         MOV.F R0.x, <src>.z;
#                                            MOV.F result.position.z, R0.x;
#                                            MOV.F R0.x, <src>.w;
#                                            MOV.F result.position.w, R0.x;
#   0 R-regs                                 1 R-reg
#
# The first component is written straight from the source in both, and only
# the other three differ: a constant is re-read (always through `.x`), an
# operand is copied through `R0.x` first.
def _position_store(src, is_constant, comps=(0, 1, 2, 3), scratch=None,
                    head=None):
    """The instruction lines for `gl_Position = src`, and the registers used.

    `comps` is the source component each destination component takes, so a
    swizzled store -- `gl_Position = a0.wzyx` in `p06_swizzle.vert` -- is the
    same lowering with a different mapping.

    PROVENANCE (notes/44): this shape is the image's, not a listing's.  The
    assignment arm of `f_7100f10a30` (IR operation 0, 0xf114ec) materialises
    the value into a register with a full mask, expands the store into one
    assignment per element (the list loop at 0xf123fc), and each of those takes
    the single-component path at 0xf129e8, which broadcasts the read component
    into every masked byte of the SOURCE SELECTOR.  That is exactly
    `MOV.F R0.x, R0.<i>;` followed by `MOV.F <out>.<c>, R0.x;`, with component
    0 skipping the first line because it is already in `x`.

    `head` overrides the FIRST component's source.  A value built by
    `OpCompositeConstruct` forwards its first operand to this read -- the
    store node's `src=` is that operand, not the construct's vreg -- while the
    other three go through the vreg.
    """
    lines = ["MOV.F result.position.x, %s;"
             % (head if head is not None else _swizzle(src, comps[0]))]
    if is_constant:
        for c in _COMPONENTS[1:]:
            lines.append("MOV.F result.position.%s, %s.x;" % (c, src))
        return lines, None
    # The scratch is the SOURCE's own register when it has one: `p05_ubo.vert`
    # writes `MOV.F R0.x, R0.y;` -- overwriting `.x` of the very register it
    # is reading `.y` from, which is safe because each component is read
    # before the next is written.  A source with no register gets one.
    # A SCALAR source needs ONE scratch copy, not three: every destination
    # component reads the same value, so `op_dot.vert` copies `R0` into
    # `R0.x` once and then writes `.y`, `.z` and `.w` from it.
    if len(set(comps)) == 1:
        if isinstance(scratch, (list, tuple)):
            scratch = scratch[0]
        lines.append("MOV.F %s.x, %s;" % (scratch, _swizzle(src, comps[0])))
        for c in _COMPONENTS[1:]:
            lines.append("MOV.F result.position.%s, %s.x;" % (c, scratch))
        return lines, scratch
    # `scratch` may be one vreg per element: each element's statement is in
    # a block of its own and makes its own scratch node (notes/82)
    _sc = list(scratch) if isinstance(scratch, (list, tuple)) \
        else [scratch] * 3
    for i, c in enumerate(_COMPONENTS[1:], 1):
        lines.append("MOV.F %s.x, %s;" % (_sc[i - 1],
                                          _swizzle(src, comps[i])))
        lines.append("MOV.F result.position.%s, %s.x;" % (c, _sc[i - 1]))
    return lines, _sc[0]
