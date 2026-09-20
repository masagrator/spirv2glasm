"""replicate.py -- f_7100069f90, a read of a REPLICATED value takes one lane.

A per-node pass of the step between the builder and the printer (called from
f_7100053d80 under f_7100054130 / f_710003a500, beside the SUB peephole
f_7100069df0 of notes/38).  Found with a watchpoint on `ns_e.frag`'s ADD
slot 0 selector (`node + 200`): 0x03020100 -> 0 there.  For every node but a
store (op 0x3a), for every slot i whose inline flag (`slot + 16`) is clear:

    sel, mask = slot[32], slot[36]
    if sel & mask is already ONE component over the mask     (0x6a020..0x6a040)
        (== 0, or mask & 0x01010101 / 0x02020202 / 0x03030303):   next slot
    D = slot[24]                                             (0x6a050)
    if not cg->vt[464](D): next slot                         (0x6a058)
    if not cg->vt[536](D):                                   (0x6a070)
        if not cg->vt[416](D): next slot                     (0x6a0c4)
        if any slot of D is not one component over its mask: next slot
    c = the first lane whose mask byte is 0xff (0x6a07c..0x6a0a8); none ->
        the pass RETURNS, leaving the node's later slots as they are
    sel = byte c of sel, in all four bytes                   (0x69fe8 ld1r)

So a reader of a value every lane of which is the same number -- a MOV of
`-u.z` into `.xy`, a MUL of `R1.z` by `{2, 0, 0, 0}.x`, a scalar op like RCP
-- reads it at one lane: `ADD.F32 R3.xy, R2.x, {1, 1, 0, 0};` (`ns_a`..`ns_e`,
and the corpus's `map_092e5464`, `chr_hair_f0ad47e1`).

The predicates, read out of the image (cg vtable at `prog[768]`, gdb):

* vt[464] = f_7100059f70, py/liveness.py `vt464`;
* vt[536] = f_7100bddb2c: a byte table at 0x11a3452 for `op - 0x3d` in
  0..0x1ef, falling back to f_7100056864 (table 0x11686fe, `op - 3` in
  0..0xda; outside it, TRUE).  TRUE for the ops below; for the image ops it
  asks cg->vt[528] (f_7100056730), which is not transcribed.
* vt[416] = f_7100bdf838: table 0x11a3716, falling back to f_71000568b0
  (table 0x11687d9, `op - 0x44` in 0..0x8c; outside it, FALSE); op 0xa8 asks
  f_7100056910, not transcribed.

`tools/` has no reader for these tables: they were decoded with the
scratchpad's `jt.py` over `tools/elfread.py`.
"""
import lex as _lex

from liveness import vt464

# cg->vt[536] TRUE (0x56898 and 0xbddb70 arms): the scalar ops whose result
# is one number in every lane.
VT536_TRUE = frozenset([
    0x06, 0x09, 0x0b, 0x12, 0x28, 0x31, 0x49, 0x61, 0x62, 0x66, 0x6a, 0x72,
    0x7a, 0x7b, 0x7c, 0x7f, 0x81, 0x88, 0x89, 0x8a, 0x8b, 0x93, 0xa9, 0xaa,
    0xba, 0xbe, 0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xd4, 0xd5, 0xd6, 0xd7, 0xd8,
    0xd9, 0xda, 0xdb, 0xdc, 0x226])
# cg->vt[536] asks cg->vt[528] (0xbddb60 and 0x568a0 arms): the image ops.
VT536_VT528 = frozenset(
    [0xb1, 0xb2, 0xb3, 0xb4, 0xb5, 0xb6, 0xb7, 0xb8, 0xb9, 0xbb, 0xbc, 0xbd,
     0xbf] + list(range(0x19b, 0x1af)) + [0x205] + list(range(0x214, 0x21b)))
# cg->vt[416] TRUE (0xbdf86c and 0x568dc arms): the lane-wise ops.
VT416_TRUE = frozenset([
    0x44, 0x47, 0x48, 0x4a, 0x4b, 0x4c, 0x4d, 0x4e, 0x52, 0x53, 0x54, 0x55,
    0x56, 0x5e, 0x63, 0x64, 0x65, 0x68, 0x69, 0x6c, 0x6d, 0x6e, 0x6f, 0x70,
    0x71, 0x76, 0x77, 0x7e, 0x82, 0x83, 0x84, 0x85, 0x8d, 0x8e, 0x8f, 0x90,
    0x91, 0x92, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9a, 0x9c, 0x9e, 0x9f, 0xa0,
    0xa1, 0xa2, 0xa3, 0xa4, 0xa7, 0xab, 0xac, 0xad, 0xae, 0xaf, 0xb0, 0xcc,
    0xcf, 0xd0, 0x1b1, 0x1b2, 0x1b3, 0x1b4, 0x1b5, 0x1bd, 0x1be, 0x1bf,
    0x1c0, 0x1f9, 0x1fe, 0x1ff, 0x200, 0x201, 0x202, 0x203, 0x204, 0x21b])
# cg->vt[416] asks f_7100056910 (0x568e8 arm).
VT416_CALL = frozenset([0xa8])

_COMP = "xyzw"


def _operand(t):
    """`^(-?)(\|?)(#\d+)(?:\.([xyzw]{1,4}))?(\|?)$` -> (neg, abs, name,
    swizzle or None, abs2), or None."""
    i = 0
    neg = abs1 = abs2 = ""
    if t.startswith("-"):
        neg, i = "-", 1
    if t.startswith("|", i):
        abs1, i = "|", i + 1
    if not t.startswith("#", i):
        return None
    e = _lex.digit_end(t, i + 1)
    if e == i + 1:
        return None
    name = t[i:e]
    rest = t[e:]
    if rest.endswith("|"):
        abs2, rest = "|", rest[:-1]
    swz = None
    if rest:
        if rest[0] != "." or not _lex.is_swizzle(rest[1:]):
            return None
        swz = rest[1:]
    return neg, abs1, name, swz, abs2


class Unmeasured(Exception):
    """A case the transcription reaches but does not cover."""


def one_component(sel, mask):
    """0x6a020..0x6a040: does the selector read ONE component over the
    mask -- `sel & mask` is 0 or the mask's bytes all 1, 2 or 3."""
    s = sel & mask
    return (s == 0 or s == mask & 0x01010101 or s == mask & 0x02020202
            or s == mask & 0x03030303)


def selector(swz):
    """The slot selector word of a printed swizzle: byte c the component
    lane c reads (a short swizzle repeats its last letter, the printer's
    padding read backwards; none is the identity)."""
    if not swz:
        return 0x03020100
    word = 0
    for c in range(4):
        word |= _COMP.index(swz[c] if c < len(swz) else swz[-1]) << (8 * c)
    return word


def byte_mask(lanes):
    """A 4-bit lane mask as the byte mask the nodes carry."""
    return sum(0xff << (8 * c) for c in range(4) if (lanes >> c) & 1)


def replicated(op, slots):
    """cg->vt[464], vt[536], vt[416] and the slot test on the node D a read
    takes (0x6a044..0x6a118): does every lane of D hold one number?
    `slots` is `[(sel, mask)]` for D's slots."""
    if not vt464(op):
        return False
    if op in VT536_TRUE:
        return True
    if op in VT536_VT528:
        # f_7100056730: TRUE only when the image node's slot 0 is op 0xcb
        # (the handle) AND the texture object cg->vt[512] returns has a
        # replicated channel word at [154] (0, or four equal nibbles other
        # than 0x3210).  gdb on that word (scratchpad `v512.py`): 0x3210,
        # the identity, for every image the probes and corpus build --
        # 2D, LOD, array, SHADOW2D, SHADOWARRAY2D (`fr_texlod`, `sa_a`,
        # `sa_c`, 21 corpus files).  So FALSE.
        return False
    if op in VT416_CALL:
        raise Unmeasured("f_7100056910 on op %#x" % op)
    if op not in VT416_TRUE:
        return False
    return all(one_component(s, m) for s, m in slots)


def rewrite_operand(text, sel, mask):
    """0x6a07c..0x6a0b0 and 0x69fe4: the operand reading lane c of its
    selector in every lane, c the first lane of the mask; None when the
    mask names no lane (the pass then stops for this node)."""
    m = _operand(text.strip())
    lane = next((c for c in range(4) if (mask >> (8 * c)) & 0xff == 0xff),
                None)
    if lane is None:
        return None
    comp = _COMP[(sel >> (8 * lane)) & 0xff]
    return "%s%s%s.%s%s" % (m[0], m[1], m[2], comp, m[4])


def parse_operand(text):
    """(name, swizzle) of a placeholder read, or None for any other
    operand (an attribute, a constant, an output: an inline slot)."""
    m = _operand(text.strip())
    if m is None:
        return None
    return m[2], m[3] or ""
