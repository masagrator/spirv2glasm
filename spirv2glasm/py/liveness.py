"""liveness.py -- `f_7100043460`, the interference graph, transcribed.

This is the allocator object's vt[16] (notes/54 §10): the function that
builds every vreg record's neighbour list `record[216]` and its first-def and
last-use positions `record[56]` / `record[60]`, which py/regalloc.py then
colours.  It runs on a model of the compiler's own IR -- positions (the
instruction list, each with its block and node) and nodes with their operand
slots -- so `tools/graphcheck.py` can feed it the compiler's IR and compare
what it builds with what the compiler built.

Every step names the address it transcribes.  Paths the probes and the corpus
sample never reach are not approximated: they raise `NotImplementedError`
with the address, so a shader that needs one says so.

The live set is the compiler's nibble set at `prog + 0x3a0` (f_7100058888
TEST, f_71000588f0 NEXT, f_7100058a20 ASSIGN): one 4-bit component mask per
record index.  Masks passed around are the BYTE-per-component form
(0xff per component), as everywhere in the allocator.
"""

import regalloc


def compress(mask32):
    """f_71000519a0: bit c set iff byte c of the mask is exactly 0xff."""
    return sum(1 << c for c in range(4)
               if ((mask32 >> (8 * c)) & 0xff) == 0xff)


def expand(nib):
    """f_7100051980: the 0x1168b08 table -- nibble to byte mask."""
    return sum(0xff << (8 * c) for c in range(4) if (nib >> c) & 1)


def permute(sel, mask):
    """f_7100051890(sel, mask): byte `sel[c]` set for every byte c of mask."""
    out = 0
    for c in range(4):
        if (mask >> (8 * c)) & 0xff:
            out |= 0xff << (8 * ((sel >> (8 * c)) & 0xff))
    return out & 0xffffffff


class Node(object):
    """The fields of a GLASM DAG node the sweep reads (notes/29, notes/47)."""

    __slots__ = ("addr", "op", "n40", "n48", "n92", "f152", "slots")

    def __init__(self, addr, op, n40, n48, n92, f152, slots):
        self.addr = addr
        self.op = op
        self.n40 = n40
        self.n48 = n48
        self.n92 = n92
        self.f152 = f152
        self.slots = slots          # [(inline, node addr, sel, mask)]


class LiveSet(object):
    """The nibble set at `prog + 0x3a0`."""

    def __init__(self, n):
        self.n = n
        self.nib = {}

    def test(self, i):
        """f_7100058888: the element as a byte mask, 0 when out of range."""
        if i < 1 or i > self.n:
            return 0
        return expand(self.nib.get(i, 0))

    def assign(self, i, mask32):
        """f_7100058a20: the element BECOMES compress(mask)."""
        v = compress(mask32)
        if v:
            self.nib[i] = v
        else:
            self.nib.pop(i, None)

    def next(self, i):
        """f_71000588f0: the first element >= i with a non-zero nibble."""
        for k in sorted(self.nib):
            if k >= i and self.nib[k]:
                return k
        return -1


def vt0(node):
    """f_71000430b0 -- this->vt[0]: does the sweep look at this node."""
    if node is None:
        return False
    k = node.op - 0x58
    if 0 <= k <= 4:
        return bool((6 >> k) & 1)
    return True


# f_7100059f3c through cg->vt[464] = f_7100059f70: a byte table at .rodata
# 0x1168a3b indexed by `op - 2`; an entry of 0 returns false, 1 true, and an
# opcode outside 2..0xcb is true.  Decoded from the image (notes/54 §10);
# these are the opcodes it is FALSE for.
_VT464_FALSE = frozenset([0x2, 0x8, 0x20, 0x21, 0x25, 0x26, 0x27] +
                         list(range(0x29, 0x31)) + list(range(0x32, 0x37)) +
                         [0x5f])


def vt464(op):
    """cg->vt[464]: may an inline slot's node carry a definition."""
    return op not in _VT464_FALSE


def _sext28(v):
    v &= 0x0fffffff
    return v - (1 << 28) if v & (1 << 27) else v


def _s32(v):
    v &= 0xffffffff
    return v - (1 << 32) if v & (1 << 31) else v


def split(recs, cg1232, vreg, mask):
    """f_71000520e0: a (vreg, byte mask) as the records it lives in.

    Returns `[(record index, byte mask)]` in the order the function fills its
    two output arrays.  The forms, tested in this order:

    * CHAINED (`record[92] != 0`, 0x520ec): the value is two halves.  The low
      pair of components goes to `record[92]`, each component widened to two
      bytes (x -> bytes 0,1; y -> bytes 2,3); the high pair to the chain's
      own `record[92]`, the same way.  A half whose components are all clear
      is skipped (0x520f4 tests `mask & 0xffff`; 0x52128 tests the unsigned
      `mask >= 0x10000`).
    * TIED (`record[88] & 0x0fffffff`, 0x52138): the target
      `t = sext28(record[88])` and `off = (record[88] >>s 27) & ~1`; when
      `off >= 4`, `t` becomes `t.record[92]` and `off` drops by 4 (0x52160).
      The record returned is `t.record[92]` (0x52188); its mask is every
      component byte of `mask` that is non-zero, moved up `off` bytes inside
      a zeroed word (0x52194..0x521f4) -- bytes pushed past the word are
      written to the stack beside it and never read.
    * GROUP (`prog[768][1232]` and `record[208]`, 0x52264): one part per
      component whose byte is exactly 0xff, the record
      `record[208][record[200] + k]` with mask 0xff (0x5228c..0x523b0).
    * otherwise the record itself, mask unchanged (0x522b0).
    """
    r = recs[vreg]
    mask &= 0xffffffff
    if r.chain:
        out = []
        if mask & 0xffff:
            lo = 0xffff if mask & 0xff else 0
            hi = 0xffff if mask & 0xff00 else 0
            out.append((r.chain, lo | (hi << 16)))
        if mask >= 0x10000:
            lo = 0xffff if mask & 0xff0000 else 0
            hi = 0xffff if mask & 0xff000000 else 0
            out.append((recs[r.chain].chain, lo | (hi << 16)))
        return out
    if r.tie & 0x0fffffff:
        t = _sext28(r.tie)
        off = (_s32(r.tie) >> 27) & ~1
        if off - 4 >= 0:
            t = recs[t].chain
            off -= 4
        word = 0
        for k in range(4):
            if (mask >> (8 * k)) & 0xff and 0 <= off + k < 4:
                word |= 0xff << (8 * (off + k))
        return [(recs[t].chain, word)]
    if cg1232 and r.grp is not None:
        out = []
        for k in range(4):
            if (mask >> (8 * k)) & 0xff == 0xff:
                g = r.w200 + k
                if not 0 <= g < len(r.grp):
                    raise NotImplementedError(
                        "f_71000520e0 group index %d outside the dump" % g)
                out.append((r.grp[g], 0xff))
        return out
    return [(vreg, mask)]


def seed(recs, own, prev, last_op, last_op72, symrec, n, cg1232):
    """f_7100048820: a block's live set at its END, and its pressure.

    `own` is the block's `+0xb0` name set as `{name: nibble}`; `prev` is the
    set of the block before it on the program's block chain (`prog[184]`,
    next at +0x120), None for the first block.  `symrec[name]` is
    `prog[832][name][80]`: `cg->vt[696]` is f_710003c33c, the identity.

    0x48840: the three counter words are zeroed and the live set at
    `prog + 0x3a0` cleared to `prog[808]` = `n` members (f_71000587d0).
    0x48874: when the block's last node (`block[24]`) is op 0x1e whose
    `node[72]` is op 0x5f, the previous block's set is OR-ed INTO THIS
    BLOCK'S OWN SET (f_7100057fc0, word by word) -- a lasting change, so
    `own` is updated in place.  0x488c8: for every name in the set, ascending
    from 1 (f_71000588f0), whose record is non-zero, the name's components
    (f_7100058888) are split (f_71000520e0) and each part made live
    (f_7100058a20, which REPLACES the element); the part's class counter
    (`record[28]`) grows by `cg->vt[328](record[8])` -- the width -- times
    the components its mask names (`bic #0xfe` + `addv`, 0x489a8).

    Returns `(LiveSet, {class: count})`.
    """
    if last_op == 0x1e and last_op72 == 0x5f and prev is not None:
        for i, v in prev.items():
            own[i] = own.get(i, 0) | v
    live = LiveSet(n)
    counts = {}
    for i in sorted(own):
        nib = own[i] & 0xf
        if i < 1 or not nib:
            continue
        rec = symrec.get(i, 0)
        if not rec:
            continue
        for r, m in split(recs, cg1232, rec, expand(nib)):
            live.assign(r, m)
            rr = recs[r]
            ncomp = sum(1 for k in range(4) if (m >> (8 * k)) & 1)
            counts[rr.cls] = counts.get(rr.cls, 0) + \
                regalloc.width(rr.type) * ncomp
    return live, counts


def defs(recs, nodes, node, cg1232, flag=1):
    """f_7100052680: what a node defines, `[(record index, byte mask)]`.

    The node's own `(node[92], node[48])` -- nothing when `node[92] < 1` or
    `node[40]` is set -- then, for every INLINE slot whose node `cg->vt[464]`
    accepts, that node's own definitions.  `vt[8]` of every node class the
    compiler builds here is f_710004f150 (returns 0), so the `node[168]`
    shift at 0x528ec never applies.
    """
    if node.op == 0xd1:
        raise NotImplementedError("f_7100052680 op 0xd1 (0x526b8)")
    out = []
    if node.n92 >= 1 and node.n40 == 0:
        r = recs[node.n92]
        if r.chain and (flag & 1):
            raise NotImplementedError("f_7100052680 chained (0x527b4)")
        if (r.tie & 0x0fffffff) and (flag & 1):
            raise NotImplementedError("f_7100052680 tied (0x52810)")
        if cg1232 and r.grp:
            raise NotImplementedError("f_7100052680 group (0x528d8)")
        out.append((node.n92, node.n48 & 0xffffffff))
    elif node.n92 < 1 or node.n40:
        # 0x52764: `node[92] < 1` returns 0 at once, and so does a set
        # `node[40]` -- the slot walk below is not reached.
        return out
    for inline, sub, _sel, _mask in node.slots:
        # 0x529fc..0x52a68: an INLINE slot whose node cg->vt[464] accepts is
        # walked into, and its definitions are this node's too.
        if not inline:
            continue
        sn = nodes.get(sub)
        if sn is None or not vt464(sn.op):
            continue
        out.extend(defs(recs, nodes, sn, cg1232, flag))
    return out


def uses(recs, nodes, node, cg1232):
    """f_71000523c0: what a node reads, `[(record index, byte mask)]`.

    Per slot, in order: an INLINE slot whose node is not a 0x2b register
    read is walked into; any other slot whose node names a vreg reads the
    components its selector maps the slot's mask to (f_7100051890) --
    except for a 0x1 node, which reads the slot's mask as it stands, and a
    0x5a slot under a node whose vt[8] holds (never, for these classes).
    """
    out = []
    for inline, sub, sel, mask in node.slots:
        sn = nodes.get(sub)
        if sn is None:
            continue
        if inline and sn.op != 0x2b:
            out.extend(uses(recs, nodes, sn, cg1232))
            continue
        if sn.n92 < 1:
            continue
        if node.op == 1:
            m = mask
        else:
            m = permute(sel, mask)
        out.extend(split(recs, cg1232, sn.n92, m))
    return out


def _eligible(r, cls):
    """0x43340..0x43368: a named record takes part only if it is coloured
    and of this class."""
    if r.sym == 1:
        return True
    return r.w64 >= 0 and r.cls == cls


def _edge(owner, mask_a, other, mask_b):
    """f_7100042c90: push `(other, h12)` at the HEAD of owner's scratch list.

    Nibble c of h12 is compress(mask_b) for every byte c of mask_a that is
    non-zero.
    """
    h12 = 0
    for c in range(4):
        if (mask_a >> (8 * c)) & 0xff:
            h12 |= compress(mask_b) << (4 * c)
    owner.l144.insert(0, (other, h12 & 0xffff))


def sweep(recs, positions, nodes, seed_of, cls, cg1232=0):
    """The position loop of f_7100043460 (0x434fc..0x43c7c).

    `positions` is `[(block, node addr)]` in list order; `seed_of(block)`
    returns the live set f_7100048820 leaves for a block (a LiveSet).
    Leaves `record.l144` (the scratch list at `record[144]`), `c56`, `c60`.
    """
    for r in recs[1:]:
        r.h136 = -1                     # 0xffff, a signed halfword
        r.l144 = []
    arr = []                            # this[24], count this[20]
    live = None
    prev = None
    for pos in range(len(positions) - 1, -1, -1):
        block, naddr = positions[pos]
        if block != prev:
            if prev is not None:
                for i in arr:
                    recs[i].h136 = -1
            live, arr = _enter_block(recs, block, seed_of)
            prev = block
        node = nodes.get(naddr)
        if node is None or not vt0(node):
            continue
        dl = defs(recs, nodes, node, cg1232)
        _def_edges(recs, dl, arr, live, cls, pos)
        _kill(recs, dl, arr, live)
        ul = uses(recs, nodes, node, cg1232)
        if (node.f152 >> 3) & 1 and dl:
            # 0x439b8: this arm reads the def-mask array at the POSITION
            # index, not at the def's own -- whatever the stack holds there.
            raise NotImplementedError("f_7100043460 node[152] bit 3 arm")
        _make_live(recs, ul, arr, live, pos)


def _enter_block(recs, block, seed_of):
    """A new block: its seed live set, and the live array built from it in
    record order.  Returns (live set, live array)."""
    live = seed_of(block)
    arr = []
    i = live.next(1)
    while i != -1:
        r = recs[i]
        if r.chain and not ((r.w156 >> 11) & 1):
            raise NotImplementedError(
                "f_7100043460 chained seed (f_7100052040)")
        r.h136 = len(arr)
        arr.append(i)
        i = live.next(i + 1)
    if getattr(block, "b296", 0):
        raise NotImplementedError("f_71000430f0 (block[296])")
    return live, arr


def _def_edges(recs, dl, arr, live, cls, pos):
    """f_71000432c0 -- the edges, defs against the live set; a def's first
    position (`c56`) is the first one the backward sweep meets."""
    for d, dm in dl:
        rd = recs[d]
        if not _eligible(rd, cls):
            continue
        if rd.c56 < 0:
            rd.c56 = pos
        for m in list(arr):
            if m == d:
                continue
            rm = recs[m]
            if not _eligible(rm, cls):
                continue
            mm = live.test(m)
            if not mm:
                continue
            if d <= m:
                _edge(rm, mm, d, dm)
            else:
                _edge(rd, dm, m, mm)


def _kill(recs, dl, arr, live):
    """0x438d0..0x4398c -- kill, swap-removing a record that emptied."""
    for d, dm in dl:
        rem = live.test(d) & ~dm & 0xffffffff
        live.assign(d, rem)
        if rem:
            continue
        r = recs[d]
        if r.h136 < 0:
            continue
        k = r.h136
        arr[k] = arr[-1]
        recs[arr[k]].h136 = k
        arr.pop()
        r.h136 = -1


def _make_live(recs, ul, arr, live, pos):
    """f_7100047d60 -- the uses, each made live (ASSIGN cur | mask); then
    0x43ac4..0x43c78 -- a newly live record joins the live array, and its
    last-use position is the first one the backward sweep meets."""
    for u, um in ul:
        cur = live.test(u)
        live.assign(u, cur | um)
    for u, _um in ul:
        r = recs[u]
        if r.h136 < 0:
            r.h136 = len(arr)
            arr.append(u)
        if r.c60 < 0:
            r.c60 = pos


def finish(recs):
    """0x43c7c..0x43ff0: merge, mirror and convert the scratch lists.

    Duplicates in a record's list are merged into the LAST occurrence (the
    earliest pushed), keeping list order; then, owner by ascending index,
    every node is mirrored onto the other record's list HEAD with its 4x4
    component matrix transposed; then each list becomes `record[216]` in the
    same order, each nibble expanded to a byte mask (f_7100051980).
    """
    for r in recs[1:]:
        lst = r.l144
        acc = {}
        last = {}
        for i, (other, h) in enumerate(lst):
            acc[other] = acc.get(other, 0) | h
            last[other] = i
        r.l144 = [(other, acc[other]) for i, (other, _h) in enumerate(lst)
                  if last[other] == i]
    for r in recs[1:]:
        for other, h in list(r.l144):
            t = 0
            for c in range(4):
                for j in range(4):
                    if (h >> (4 * c + j)) & 1:
                        t |= 1 << (4 * j + c)
            recs[other].l144.insert(0, (r.idx, t))
    for r in recs[1:]:
        r.nb = [regalloc.Edge(other, [expand((h >> (4 * c)) & 0xF)
                                      for c in range(4)])
                for other, h in r.l144]


def build(recs, positions, nodes, seed_of, cls, p376=0, cls8=0, cg1232=0):
    """f_7100043460 whole: the sweep, then `finish`."""
    if p376 and cls8 > 2:               # 0x43494..0x434b8
        return
    sweep(recs, positions, nodes, seed_of, cls, cg1232)
    finish(recs)
