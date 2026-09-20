"""binding.py -- the GLASM binding namespace, transcribed from the image.

Every interface symbol the front end builds carries two numbers: `kind`, a
small enumeration, and `reg`, whose low byte is a slot and whose bit 16 is a
flag.  `f_7100bd4810` turns that pair into the text a declaration uses --
`vertex.attrib`, `result.color.back.secondary`, `primitive.out.patch.attrib` --
substituting one separator character for every `%c` in its format, which is why
the same code produces both the binding (`.`) and the variable name (`_`).

It also decides the DECLARATION'S SHAPE, and that is the part a table of names
would miss.  An arm that ends at 0x7100bd55c8 returns -1: a single binding,
printed `name = binding;`.  An arm that reaches 0x7100bd5340 or 0x7100bd4c98
returns a count and writes the range's first index through `*p5`: an array,
printed `name[] = { binding[lo..hi] };`.  The arms that do the second are
exactly the ones whose slot range is a FAMILY -- `attrib`, `texcoord`, `clip`,
`cull`, `tessinner`, `tessouter` -- and the index they report is relative to
that family's base slot, which is also the floor the merge loop stops at.  When
`*p5` is not asked for (the call that builds the variable NAME), the index is
appended to the text instead, and only when it is >= 1: that is the whole of
`fragment_attrib6` versus `fragment_attrib`.

This is a transcription of that function's dispatch, not a table fitted to
listings: the two half-word jump tables at 0x71011a1ec8 (kind 0x30..0x6f) and
0x71011a1f48 (kind 0xb7..0xf0), the per-kind slot tables at 0x71011a2026,
0x71011a1fce, 0x71011a1fbc, 0x71011a205c and 0x71011a2004, and the byte table
at 0x71011a1da4 that `f_7100bd1da0` uses for the lower-case slot names.  A
(kind, slot) pair the function leaves unnamed returns None here, exactly as it
leaves the buffer empty there, and the caller skips it.

See notes/16-binding-namespace.md.
"""


class Unread(Exception):
    """A pair whose arm exists but whose index base is not a constant."""


# f_7100bd1da0: slot -> lower-case name.  Slots with no GLASM spelling
# (PRIMITIVEID, INVOCATIONID, the warp and clock registers, DEVICEINDEX,
# VIEWINDEX) reach the default arm and print '????'.
SLOT_NAME = {0x20: "position", 0x21: "color", 0x22: "color.secondary",
             0x23: "color.back", 0x24: "color.back.secondary",
             0x2f: "fogcoord", 0x30: "pointsize", 0x39: "id",
             0x46: "secondaryposition"}
for _i in range(10):
    SLOT_NAME[0x25 + _i] = "texcoord[%d]" % _i
for _i in range(8):
    SLOT_NAME[0x31 + _i] = "clip[%d]" % _i
    SLOT_NAME[0x49 + _i] = "cull[%d]" % _i
del _i


def slot_name(slot):
    """`f_7100bd1da0`, complete."""
    if slot <= 0x1f:
        return "attrib[%d]" % slot
    return SLOT_NAME.get(slot, "????")


def _fmt(pattern, sep):
    return pattern.replace("%c", sep)


# The scalar families, shared by every kind that names them: slot -> the tail
# that follows the stage prefix.  These all return -1, so they print
# `name = binding;`.
_SCALAR = {0x20: "position", 0x21: "color", 0x22: "color%csecondary",
           0x23: "color%cback", 0x24: "color%cback%csecondary",
           0x2f: "fogcoord", 0x30: "pointsize"}


def _ranged(prefix, sep, tail, base, cull=False):
    return (_fmt(prefix + tail, sep), base, cull)


def binding_form(kind, reg, sep="."):
    """(text, base) for a pair, or None if `f_7100bd4810` names nothing.

    `base` is None for the arms that return -1 -- a single binding -- and the
    family's base slot for the arms that return a count, in which case the
    index of `slot` within the family is `slot - base` and the merge may not
    descend below `base`.  The third element marks the CULL families, whose
    first index is not 0 but `program[1328]`, the number of clip elements in
    use (0x7100bd523c), so their range starts at the lowest cull slot present
    rather than at the family base.
    """
    arm = _KIND_ARMS.get(kind)
    return arm(reg & 0xff, reg, sep) if arm is not None else None


def _single(pattern, sep):
    """A single binding: the arms that return -1."""
    return (_fmt(pattern, sep), None, False)


def _attrib_only(pattern):
    """An arm that names only the attrib family, from slot 0."""
    def arm(slot, reg, sep):
        return (_fmt(pattern, sep), 0, False) if slot <= 0x1f else None
    return arm


def _named_vertex_slots(slot, reg, sep):
    """Kind 0x30: 'vertex.%s' with the lower-case slot name, then every '.'
    in the RESULT is rewritten to `sep`, which is what turns
    `vertex.color.back` into `vertex_color_back`.  Only the slots in the
    mask at 0x7100bd4890 reach it."""
    if slot not in (0x20, 0x21, 0x22, 0x23, 0x24, 0x2f, 0x30, 0x39):
        return None
    s = "vertex." + slot_name(slot)
    return (s if sep == "." else s.replace(".", sep), None, False)


def _per_vertex_inputs(slot, reg, sep):
    """Kinds 0x33 and 0x35."""
    if slot <= 0x1f:
        return _ranged("vertex%c", sep, "attrib", 0)
    if slot in _SCALAR:
        return _single("vertex%c" + _SCALAR[slot], sep)
    if 0x25 <= slot <= 0x2e:
        return _ranged("vertex%c", sep, "texcoord", 0x25)
    if 0x31 <= slot <= 0x38:
        return _ranged("vertex%c", sep, "clip", 0x31)
    if 0x49 <= slot <= 0x50:
        return _ranged("vertex%c", sep, "cull", 0x49)
    single = {0x39: "vertex%cid", 0x3a: "primitive%cid",
              0x3b: "primitive%cinvocation",
              0x46: "vertex%csecondaryposition"}.get(slot)
    return _single(single, sep) if single is not None else None


def _view_indices(slot, reg, sep):
    """Kind 0x6b."""
    return ((_fmt("primitive%cviewindices", sep), 0x10, False)
            if (slot & ~3) == 0x10 else None)


def _vertex_outputs(slot, reg, sep):
    """Kind 0x6f names ONLY the four families -- its arm chain (0x7100bd4bac
    -> bd4cf0 -> bd4e14 -> bd521c -> the empty-buffer exit) has no position,
    colour, fogcoord or pointsize test at all, which is why a vertex shader
    writing gl_Position gets no OUTPUT line for it."""
    if slot <= 0x1f:
        return _ranged("result%c", sep, "attrib", 0)
    if 0x25 <= slot <= 0x2e:
        return _ranged("result%c", sep, "texcoord", 0x25)
    if 0x31 <= slot <= 0x38:
        return _ranged("result%c", sep, "clip", 0x31)
    if 0x42 <= slot <= 0x49:
        return _ranged("result%c", sep, "cull", 0x42, cull=True)
    return None


def _result_outputs(slot, reg, sep):
    """Kinds 0xbb and 0xc0."""
    if slot <= 0x1f:
        return _ranged("result%c", sep, "attrib", 0)
    if slot in _SCALAR:
        return _single("result%c" + _SCALAR[slot], sep)
    if 0x25 <= slot <= 0x2e:
        return _ranged("result%c", sep, "texcoord", 0x25)
    if 0x31 <= slot <= 0x38:
        return _ranged("result%c", sep, "clip", 0x31)
    if 0x40 <= slot <= 0x47:
        return _ranged("result%c", sep, "cull", 0x42, cull=True)
    if slot == 0x88 or slot == 0x8b:
        return _single("result%cshadingrate", sep)
    return None


def _patch_families(prefix, attrib):
    """The tessellation patch arms: attrib, tessinner, tessouter."""
    def arm(slot, reg, sep):
        if slot <= 0x1f:
            return _ranged(prefix, sep, attrib, 0)
        if 0x20 <= slot <= 0x21:
            return _ranged(prefix, sep, "tessinner", 0x20)
        if 0x22 <= slot <= 0x25:
            return _ranged(prefix, sep, "tessouter", 0x22)
        return None
    return arm


def _per_vertex_outputs(slot, reg, sep):
    """Kind 0xb7: one arm with two prefixes, `result%c` when bit 16 of `reg`
    is clear (0x7100bd4924) and `vertex%cout%c` when it is set
    (0x7100bd4d0c), then the SHARED tail at 0x7100bd4d20 appends the slot's
    name from the table at 0x71011a2004.  Reading only the prefix leaves
    `result.`.

    Below 0x20 the shared tail falls through its slot table into the attrib
    chain at 0x7100bd504c, so a per-vertex output with a Location prints
    `result.attrib` -- which is the `OUTPUT result_attrib[]` line a
    tessellation-control shader with varyings has."""
    pre = "vertex%cout%c" if reg & 0x10000 else "result%c"
    if slot <= 0x1f:
        return _ranged(pre, sep, "attrib", 0)
    if slot in _SCALAR:
        return _single(pre + _SCALAR[slot], sep)
    if 0x25 <= slot <= 0x2e:
        return _ranged(pre, sep, "texcoord", 0x25)
    return None


def _fragment_inputs(slot, reg, sep):
    """Kind 0x37, per-vertex or fragment.  Below 0x20 the name depends on
    bit 16 of `reg`: set for the geometry/tessellation per-vertex inputs,
    clear for the fragment stage (0x7100bd51b8, a csel between the two
    literals)."""
    if slot <= 0x1f:
        return _ranged("vertex%c" if reg & 0x10000 else "fragment%c",
                       sep, "attrib", 0)
    if 0x22 <= slot <= 0x2b:
        return _ranged("fragment%c", sep, "texcoord", 0x22)
    if slot == 0x2d:
        return _single("fragment%cposition", sep)
    if 0x2e <= slot <= 0x35:
        return _ranged("fragment%c", sep, "clip", 0x2e)
    if 0x4c <= slot <= 0x53:
        return _ranged("fragment%c", sep, "cull", 0x4c, cull=True)
    return None


# f_7100bd4810's dispatch: kind -> its arm (slot, reg, separator).
_KIND_ARMS = {
    0x07: _attrib_only("vertex%cattrib"),               # vertex inputs
    0x30: _named_vertex_slots,
    0x33: _per_vertex_inputs,
    0x35: _per_vertex_inputs,
    0x36: _attrib_only("primitive%cpatch%cattrib"),     # tess patch inputs
    0x37: _fragment_inputs,
    0x6b: _view_indices,
    0x6f: _vertex_outputs,
    0xb7: _per_vertex_outputs,
    0xbb: _result_outputs,
    0xbd: _patch_families("result%cpatch%c", "attrib"),  # tess patch outputs
    0xc0: _result_outputs,
    0xc7: _attrib_only("%cattrib"),                     # the fat outputs alias
    0xf0: _patch_families("primitive%cout%c", "patch%cattrib"),  # tess out
}


def binding_name(kind, reg, sep="."):
    """Just the text, for callers that do not care about the shape."""
    f = binding_form(kind, reg, sep)
    return None if f is None else f[0]


# f_7100bdcc20: kind -> the bit it occupies in the per-slot mask, and the
# 32-bit kind id each bit maps back to (0x71011a2fd8) with the register bits
# the emitter ORs in (0x71011a3018).
BIT_KIND = (0x37, 0x30, 0x33, 0x36, 0x35, 0x07, 0xcf, 0xc0,
            0xb7, 0xbd, 0xbb, 0x6f, 0xb7, 0xf0, 0xc7, 0x6b)
# The inverse, for a symbol whose reg carries no flag: 0xb7 answers bit 8 (the
# write side); bit 12 is reached only through a reg with 0x10000 set.
KIND_BIT = {k: i for i, k in reversed(list(enumerate(BIT_KIND)))}
BIT_REG = (0,) * 12 + (0x10000,) + (0,) * 3
INPUT_BITS = 0xb03f                          # bits 0-5, 12, 13, 15
