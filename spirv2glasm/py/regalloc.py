"""regalloc.py -- GLSLC's register colouring, transcribed (notes/54).

The per-class driver `f_7100046b60` hands a list of vreg records to two
virtual methods of the allocator object, per attempt:

    list  = this->vt[160](...)    f_7100045ed0   SIMPLIFY: the visiting order
    slots = this->vt[152](...)    f_7100045530   SELECT:   first fit per record

with `this->vt[24]` = `f_7100042da0` (a record's degree) and `this->vt[32]` =
`f_7100042ed0` (take a record out of the graph) underneath.  Every function
here is one of those, instruction for instruction; the addresses in the
comments are where to re-read each step.

A record is the allocator's 224-byte vreg record reduced to the fields these
functions read and write.  The interference graph is `record[216]`, the
per-vreg adjacency list `f_7100043460` builds: one node per neighbour,
`{idx, four words (one per component of THIS vreg, a byte per component of
the other), count}` -- notes/52 sec.3e.

This module does NOT build the graph.  `tools/simpcheck.py` feeds it the
compiler's own graph (the `g2s_simp` dump) and compares every decision it
makes with the compiler's, attempt by attempt; the converter feeds it a graph
it derives from its own lines.
"""

# f_7100bdfaa8: `width(type)` -- a 0x1a-entry word table at .rodata
# 0x11a3c24 indexed by `type - 1`; anything outside 1..26 is 0.  Read out of
# the image (tools/simpcheck.py prints it again from the ELF with --tables).
WIDTH = (0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 0, 0,
         0, 0, 0, 1)

# .rodata 0x116831c: the candidate STRIDE for a record of n components
# (f_7100045530 at 0x457ec..0x45800).
STRIDE = (0, 1, 2, 4, 4, 8, 8, 8, 8)

# .rodata 0x1143d20: the SWIZZLE a placement at lane `slot & 3` gives the
# record's components (0x45e60..0x45e70).  Byte c is the lane component c
# lands in.
SWIZZLE = (0x03020100, 0x00030201, 0x00000302, 0x00000003)

# 0x1145670: the per-byte bit weights f_7100042da0 ANDs its `== 0xff` byte
# mask with before summing -- 1, 2, 4 ... 128 twice.  The two halves of the
# 16-byte vector are summed SEPARATELY (`addv` on each 8-byte half) and
# joined as `low | high << 8`, so byte `4*c + k` of the node's four words
# becomes bit `4*c + k` of a 16-bit mask: one bit per conflicting
# (component, component) pair.
_PAIR_WEIGHTS = (1, 2, 4, 8, 16, 32, 64, 128) * 2

SPILL_FLAG = 0x20000        # record[156] bit 17 -- "on the spill list L0"

import os as _os
_TRACE = bool(_os.environ.get("G2S_PICKTRACE"))


def width(type_code):
    """f_7100bdfaa8."""
    k = type_code - 1
    if 0 <= k <= 0x19:
        return WIDTH[k]
    return 0


def limit(n):
    """f_7100bdfbc4 -- the class's register limit: `(n + 3) & ~3`."""
    return (n + 3) & ~3


class Edge(object):
    """One node of `record[216]`: the neighbour and four component words."""

    __slots__ = ("idx", "words", "count")

    def __init__(self, idx, words, count=0):
        self.idx = idx
        self.words = tuple(words)
        self.count = count          # node[32], written by f_7100042da0


class Rec(object):
    """The fields of a 224-byte vreg record the colouring reads and writes."""

    __slots__ = ("idx", "type", "b12", "b13", "b14", "b15", "sym", "h22",
                 "swz", "c48", "c52", "c56", "c60", "w64", "cm", "tie",
                 "chain", "w96", "h136", "w156", "grp", "nb", "w68", "w72",
                 "w84", "cls", "l144", "w200")

    def __init__(self, idx, **kw):
        self.idx = idx
        self.type = 0
        self.b12 = self.b13 = self.b14 = self.b15 = 0
        self.sym = 1
        self.h22 = 0
        self.swz = 0x03020100
        self.c48 = self.c52 = self.c56 = self.c60 = 0
        self.w64 = -1
        self.cm = 0
        self.tie = 0
        self.chain = 0
        self.w96 = 0
        self.h136 = 0
        self.w156 = 0
        self.grp = None
        self.nb = []
        self.w68 = self.w72 = 0
        self.w84 = 0
        self.cls = 0
        self.l144 = []
        self.w200 = 0
        for k, v in kw.items():
            setattr(self, k, v)


def _ncomp(cm):
    """Components a `record[80]` mask names: bytes whose bit 0 is set.

    0x46db4..0x46dc8: `uxtl` the four bytes, `bic #0xfe` keeps bit 0 of each,
    `addv` counts them.
    """
    return sum(1 for k in range(4) if (cm >> (8 * k)) & 1)


def _group_count(rec):
    """f_7100036038: `record[22] / width(record[8])`, or 0 for width 0."""
    w = width(rec.type)
    if not w:
        return 0
    # sdiv of a signed halfword by a small positive int
    q = abs(rec.h22) // w
    return q if rec.h22 >= 0 else -q


def degree(recs, rec):
    """f_7100042da0 -- this->vt[24]: a record's weighted degree.

    For every node of the neighbour list (following `record[92]`'s chain from
    the record's own base), count the (component, component) bytes that are
    0xff, store the count in the node (`node[32]`), and add it weighted by
    the neighbour's width -- doubled when THIS record's width is 4.
    """
    wr = width(rec.type)
    shift = 1 if wr == 4 else 0
    cur = recs[rec.chain] if rec.chain else rec
    total = 0
    while True:
        for e in cur.nb:
            wn = width(recs[e.idx].type)
            bits = 0
            for c in range(4):
                for k in range(4):
                    if ((e.words[c] >> (8 * k)) & 0xff) == 0xff:
                        half = (4 * c + k) >> 3
                        bits |= _PAIR_WEIGHTS[4 * c + k] << (8 * half)
            e.count = bin(bits & 0xffff).count("1")
            total += (wn << shift) * e.count
        if not cur.chain:
            break
        cur = recs[cur.chain]
    return total


def remove(recs, lists, rec, w3, K, cg1232):
    """f_7100042ed0 -- this->vt[32]: take `rec` out of the graph.

    Every neighbour's degree drops by `w3` (halved on a chained record) times
    the pairs it shared with this one, and a neighbour on the spill list L0
    whose weighted degree has fallen below K moves to the TAIL of L2.
    """
    w20 = w3
    cur = rec
    if rec.chain:
        w20 >>= 1                               # asr w20, w20, #1
        cur = recs[rec.chain]
    L0, L2 = lists["L0"], lists["L2"]
    while True:
        for e in cur.nb:
            j = e.idx
            if recs[j].grp:                     # 0x42f84: a group's head
                j = recs[j].grp[0]
            if (recs[j].w156 >> 11) & 1:        # byte 157 bit 3
                j = recs[j].w84
            r = recs[j]
            if cg1232:
                r.c48 -= w20
                w8 = r.c48
            else:
                wr = width(r.type)
                r.c48 = r.c48 - (w20 << (1 if wr == 4 else 0)) * e.count
                w8 = r.w64 * r.c48
            if K < 1:
                continue
            if not (r.w156 & SPILL_FLAG):       # byte 158 bit 1
                continue
            if w8 >= K:
                continue
            L0.remove(r)
            r.w156 &= ~SPILL_FLAG
            L2.append(r)
        if not cur.chain:
            break
        cur = recs[cur.chain]


def simplify(recs, order, attempt, K, cls8, p376, cg1232, avoid, state):
    """f_7100045ed0 -- this->vt[160]: the visiting order.

    `order` is the driver's list (head first).  `state` carries the two
    in/out words the driver passes by pointer: `budget` (x3) and `wmask`
    (x6).  Returns the list the engine visits, head first.
    """
    if p376 != 0 and cls8 > 2:                  # 0x45f00: nothing to do
        return []
    E, A, B, C, D, avoid = _simplify_mode(attempt, cls8, state, avoid)
    F = E | (A ^ 1) | D                         # [sp+16], 0x46000..0x4600c
    lists = {"L0": [], "L1": [], "L2": []}
    for r in order:
        _seed(recs, lists, r, K, cg1232, state, E, C, D, F)
    if E and not D:
        state["budget"] = 0
    G = E | D                                   # [x29-8]
    H = G if K <= 0 else 1                      # [sp+12]
    stack = []                                  # stack[-1] is the head
    while True:
        if lists["L2"] or lists["L1"]:
            r = (lists["L2"] or lists["L1"]).pop(0)
            remove(recs, lists, r, r.h136, K, cg1232)
            _push(recs, stack, r)
            continue
        if not lists["L0"]:
            break
        best, bestv, w3 = _spill_choice(lists["L0"], G, A, B, avoid)
        if E and bestv > state["budget"]:
            state["budget"] = bestv
        lists["L0"].remove(best)
        _push(recs, stack, best)
        best.w156 &= ~SPILL_FLAG
        if H:
            remove(recs, lists, best, w3, K, cg1232)
    if K >= 1:
        state["budget"] = cls8
    return list(reversed(stack))


def _simplify_mode(attempt, cls8, state, avoid):
    """The attempt's flags, 0x45f10..0x45ffc: (E, A, B, C, D, avoid).

    E [sp+28] weights nothing (every attempt but the first); B orders the
    spill choice by `h22` first (class 8 == 1, or attempt 2 with at most one
    width in the mask); A weights by width (attempt 2, several widths); C
    weights by 999999 - c56 (attempt 3); D by c60 * 10000 and drops the
    record to avoid (attempt 4)."""
    E = 1 if attempt != 1 else 0
    A = B = C = D = 0
    if cls8 == 1:
        B = 1                                   # 0x45f34
    elif attempt == 4:
        avoid = None
        E, D = 0, 1
    elif attempt == 3:
        E, C = 0, 1
    elif attempt == 2:
        pc = bin(state["wmask"] & 0xffffffff).count("1")
        A = 1 if pc > 1 else 0
        B = 1 if pc < 2 else 0
    return E, A, B, C, D, avoid


def _set_group_w64(recs, r, value):
    """`w64` of every member of the record's group past the first."""
    if r.grp:
        n = _group_count(r)
        for k in range(1, n):
            recs[r.grp[k]].w64 = value


def _seed(recs, lists, r, K, cg1232, state, E, C, D, F):
    """One record of the driver's list into L2 (it colours whatever
    happens: fewer neighbours than K) or L0 (a spill candidate, with its
    weight in `w96`)."""
    r.h136 = width(r.type)
    r.w96 = 0
    if cg1232:
        r.w64 = 1
        _set_group_w64(recs, r, 1)
    elif r.b14:
        r.w64 = 4
    elif r.b13:
        r.w64 = 2
    else:
        r.w64 = 1
    if r.c48 * r.w64 < K:
        r.w156 &= ~SPILL_FLAG
        lists["L2"].append(r)
        return
    r.w156 |= SPILL_FLAG
    state["wmask"] |= 1 << ((r.h136 - 1) & 31)
    if E:
        v = 0
    elif C:
        v = 999999 - r.c56
    elif D:
        v = r.c60 * 10000
    else:
        v = r.c52
    r.w96 = v * (r.h136 if F == 0 else 1)
    lists["L0"].append(r)


def _push(recs, stack, r):
    """A record onto the visiting stack, marked taken."""
    stack.append(r)
    r.w96 = -1
    r.w64 = -1
    _set_group_w64(recs, r, -1)


def _spill_choice(L0, G, A, B, avoid):
    """The spill choice, 0x46598..0x46664: (the record, its weight, its
    width)."""
    best = None
    bestv = 0x7fffffff
    best22 = 9999
    w3 = 9999
    for r in L0:
        w12 = r.w96
        if G:
            w12 = (r.c48 + r.w96) * (r.h136 if A else 1)
        if B:
            take = 1 if (best22 > r.h22
                         or (best22 == r.h22 and w12 < bestv)) else 0
        else:
            take = 1 if w12 < bestv else 0
        if avoid is not None and best is not None:
            if best is avoid:
                take = 1
            if r is avoid:
                continue
        if take:
            best22 = r.h22
            bestv = w12
            w3 = r.h136
            best = r
    return best, bestv, w3


def _glob(cls12, nbits):
    """The slots NO record of the class may take (0x4560c..0x45700).

    When the class struct's word 12 is below 4, lanes `cls12`..3 of every
    register are marked: a class narrower than four components.
    """
    out = set()
    if cls12 <= 3:
        for base in range(0, nbits, 4):
            for lane in range(cls12, 4):
                out.add(base + lane)
    return out


def _mark(recs, rec, glob, nbase):
    """f_7100044e10 -- this->vt[128]: the slots this record may NOT take.

    Four bitsets, one per component of THIS record, each starting as a copy
    of `glob`; every coloured neighbour (colour >= 0 and below `nbase * 8`)
    marks the slots its conflicting components occupy -- its colour plus
    `width * lane`, the lane its component `k` was put in being byte `k` of
    its swizzle `record[24]`, and `width` slots each.
    """
    bitmaps = [set(glob) for _ in range(4)]
    base = rec
    if rec.chain:
        base = recs[rec.chain]
    for e in base.nb:
        nb = recs[e.idx]
        col = nb.w64
        if col < 0 or col >= nbase * 8:
            continue
        wn = width(nb.type)
        lanes = [(nb.swz >> (8 * k)) & 0xff for k in range(4)]
        if rec.chain:
            # 0x44f28..0x45090: the chained (64-bit pair) form, which indexes
            # the bitsets by component PAIR.  Not reached by any shader
            # measured; refused rather than approximated.
            raise NotImplementedError("chained record in f_7100044e10")
        for c in range(4):
            for k in range(4):
                if not (e.words[c] >> (8 * k)) & 0xff:
                    continue
                if wn < 1:
                    continue
                s = col + wn * lanes[k]
                for q in range(wn):
                    bitmaps[c].add(s + q)
    return bitmaps


def select(recs, order, w3, lim, cls12, p376, w5):
    """f_7100045530 -- this->vt[152]: first fit, in `order`.

    `w3` is the driver's budget word (the bitsets are `((w3 + 1) & ~1) * 8`
    slots), `lim` the class limit `f_7100bdfbc4(cg[36 + 4*class])`, `w5` the
    driver's seventh argument.  For each record the candidate starts at slot
    0 and advances by the stride for its component count until component
    i's slot `cand + i` is free in bitset i for every i; the colour is
    `cand` aligned down and the leftover lane is the swizzle.  Returns the
    highest `cand + record[22]`, or -1 for an empty list.

    The lane-permuting fallback at 0x458c0..0x45dcc (a record with `b12`
    set, `b13` clear and more than one component, when the ordinary search
    has run out of slots) is not transcribed and raises.
    """
    nbase = (w3 + 1) & ~1
    nbits = nbase * 8
    glob = _glob(cls12, nbits)
    for r in order:                             # vt[144] = f_71000454d0
        r.w64 = -1
    best = -1
    for r in order:
        bitmaps = _mark(recs, r, glob, nbase)
        two = 1 if width(r.type) == 2 else 0
        n = int(r.h22 / 2) if two else r.h22
        step = 2 if two else 1
        stride = STRIDE[r.h22] if not r.b15 else step * 8
        cand = 0
        cur_lim = lim
        found = None
        while True:
            while cand < cur_lim:
                ok = True
                for i in range(max(n, 0)):
                    s = cand + i * step
                    if (s < nbits and s in bitmaps[i]) or \
                            (two and s + 1 < nbits and (s + 1) in bitmaps[i]):
                        ok = False
                        break
                if ok:
                    found = cand
                    break
                cand += stride
            if found is not None:
                break
            if r.b12 and not r.b13 and n != 1:
                raise NotImplementedError(
                    "the lane-permuting search at 0x458c0")
            new_lim = limit(cur_lim + 4)        # 0x45dd0
            if new_lim > nbits:
                found = new_lim                 # 0x45df8: no room at all
                break
            cand = cur_lim
            cur_lim = new_lim
        best = max(best, found + r.h22)
        if w5 < 3 or p376 == 0:
            r.w64 = found & (-4 << two)
        if _TRACE:
            # diagnosis only: the same line `g2s_pick` prints for the
            # compiler, so the two pick sequences can be laid side by side
            import sys as _sys
            _sys.stderr.write("PICK rec=%d cand=%d reg=%d\n"
                              % (r.idx, found, found & (-4 << two)))
        r.swz = SWIZZLE[(found >> two) & 3]
    return best


def allocate_class(recs, order0, cls, cg36, p376, cg1232, avoid, w7):
    """f_7100046b60's attempt loop (0x46e5c..0x47014) over one class.

    `order0` is the driver's list, head first; `cls` the class struct
    `{8: .., 12: ..}`; `cg36` the class's register count.  Returns
    `(regs, used, attempts)` where `attempts` records, per attempt, the
    visiting order and the engine's result -- what `tools/simpcheck.py`
    compares with the compiler.
    """
    K = limit(cg36)
    state = {"budget": 0, "wmask": 0}
    best_regs = 99999
    best_spill = 99999
    used = 99999                                # [sp+64] starts as 0x1869f
    order = order0
    attempts = []
    regs = None
    for attempt in range(5):
        lst = simplify(recs, order, attempt, K, cls[8], p376, cg1232, avoid,
                       state)
        b = state["budget"]
        if cls[8] < (b + 3) >> 2:
            b = cls[8] * 4
        w3 = b + 1
        state["budget"] = w3
        ret = select(recs, lst, w3, K, cls[12], p376, w7)
        regs = limit(ret)
        attempts.append(([r.idx for r in lst], ret,
                         dict((r.idx, (r.w64, r.swz)) for r in lst)))
        spill = 0                               # this[16], never set here
        if regs > K or spill:
            if regs < best_regs or (regs == best_regs and spill < best_spill):
                for r in lst:
                    r.w68, r.w72 = r.swz, r.w64
                best_regs, used, best_spill = regs, ret, spill
            for r in lst:
                r.c48 = r.c52
            order = lst
            continue
        used = ret
        break
    if best_regs < regs or (best_regs == regs and best_spill <= 0):
        for r in order:
            r.w64, r.swz = r.w72, r.w68
        regs = best_regs
    return regs, used, attempts


def singles(all_recs):
    """0x46c40..0x46c68: records with `b12` clear and ONE component.

    The driver counts these over EVERY record of the program before it
    builds the list, and the count decides the cost formula below.
    """
    return sum(1 for r in all_recs if not r.b12 and _ncomp(r.cm) == 1)


def driver_cost(recs, rec, nsingles):
    """The cost `f_7100046b60` stores in `record[48]` and `record[52]`.

    0x46d90..0x46e3c: the degree from vt[24]; then, for a record whose `b12`
    is clear and whose component mask names any component, either four times
    the degree (ONE component, and at least two single-component records in
    the program) or the degree plus `width * ncomp`.
    """
    d = degree(recs, rec)
    if rec.b12:
        return d
    n = _ncomp(rec.cm)
    if n == 0:
        return d
    if n == 1 and nsingles >= 2:
        return 4 * d
    return d + width(rec.type) * n


def _sext28(v):
    v &= 0x0fffffff
    return v - (1 << 28) if v & (1 << 27) else v


def first_sweep(recs):
    """0x46bf0..0x46cdc: the driver's sweep BEFORE the graph is built.

    Returns the single-component count the cost formula reads; mutates the
    records exactly as the driver does (the colour of a record with no
    components is 0, every neighbour list is dropped, a tied record takes
    its base's width as its component count and loses `b12`).
    """
    n = len(recs)
    nsingles = 0
    for i in range(n):
        r = recs[i]
        r.c56 = r.c60 = -1
        if r.h22 == 0:
            r.w64 = 0
        r.nb = []
        if not r.b12 and _ncomp(r.cm) == 1:
            nsingles += 1
    for i in range(1, n):
        r = recs[i]
        t = _sext28(r.tie)
        if t:
            r.h22 = width(recs[t].type)
            r.b12 = 0
    return nsingles


def build_lists(recs, cls_id, nsingles):
    """0x46d00..0x46e44: the driver's three lists, each head first.

    Returns `(order, tied, precoloured)`.  Every one is built by PUSH-FRONT
    over ascending index, so each is in DESCENDING index order.
    """
    order, tied, pre = [], [], []
    for i in range(1, len(recs)):
        r = recs[i]
        if (r.tie & 0x0fffffff) and r.sym == 1:
            tied.insert(0, r)
            r.w64 = -1
            continue
        if (r.w156 >> 11) & 1:                  # byte 157 bit 3
            if r.sym == 1:
                continue
            if r.cls == cls_id:
                pre.insert(0, r)
            continue
        if r.sym != 1:
            if r.cls == cls_id:
                pre.insert(0, r)
            continue
        if r.h22 == 0:
            continue
        cost = driver_cost(recs, r, nsingles)
        r.c48 = cost
        r.c52 = cost
        order.insert(0, r)
    return order, tied, pre


def name_symbols(recs, symbase, w6, p376, tied):
    """0x471a0..0x47364: the symbol each coloured record is printed as.

    A record's symbol IS its register (notes/52 sec.2): `symbase +
    colour / 4` for the 32-bit types (`width == 1`), `w6 + colour / 8` for
    the two-slot ones when the driver was given `w6`.  Tied records take
    their base's colour plus the lane pair their delta lands in, and have
    their swizzle shifted by two lanes.  Returns the highest colour seen on
    the `prog[376]` path (0 otherwise), which the "used" word is made of.
    """
    w20 = 0
    for i in range(1, len(recs)):
        r = recs[i]
        if r.sym != 1:
            continue
        if w6:
            if width(r.type) >= 2:
                if not (r.type <= 0xc and (1 << r.type) & 0x1840):
                    continue
                if r.w64 < 0:
                    continue
                r.sym = w6 + (r.w64 >> 3)
                continue
        if not (r.type <= 0x1b and (1 << r.type) & 0xc187fc0):
            continue
        if r.w64 < 0:
            continue
        if r.type == 0x1a or p376 == 0:
            r.sym = symbase + (r.w64 >> 2)
        else:
            r.sym = r.w64 + symbase
            w20 = max(w20, r.w64)
    for r in tied:
        t = _sext28(r.tie)
        base = recs[t]
        lane = (base.swz & 0xff) + (((r.tie & 0xffffffff) >> 28) - 16
                                   if (r.tie >> 31) & 1 else
                                   (r.tie & 0xffffffff) >> 28)
        r.w64 = base.w64 + (lane >> 1) * 4
        if (lane & ~2) == 1:
            out = 0
            for k in range(4):
                b = ((r.swz >> (8 * k)) & 0xff) + 2
                out |= (b if b <= 3 else 0) << (8 * k)
            r.swz = out
        if p376 == 0 or r.type == 0x1a:
            r.sym = symbase + (r.w64 >> 2)
        else:
            r.sym = r.w64 + symbase
            w20 = max(w20, r.w64)
    return w20


def allocate(recs, cls_id, cls, cg36, p376, cg1232, avoid, symbase, w6, w7,
             graph, extra_singles=0):
    """`f_7100046b60` end to end, for the spill-free case.

    `recs` is the whole record array (index 0 included), `graph(recs)`
    builds the neighbour lists (this->vt[16]; the check tool installs the
    compiler's own, the converter its derivation).  Returns the "used" word
    the driver stores through its eighth argument.
    """
    # `extra_singles`: single-component records of the program that are not
    # in `recs` -- the LONG handles (notes/87).  The driver's count runs over
    # EVERY record (0x46c40), whatever its class.
    nsingles = first_sweep(recs) + extra_singles
    graph(recs)
    order, tied, _pre = build_lists(recs, cls_id, nsingles)
    regs, used, attempts = allocate_class(recs, order, cls, cg36, p376,
                                          cg1232, avoid, w7)
    if cls.get(29, 0):
        # vt[56] = f_71000443f8: `cls[8] >= (regs + 3) >> 2`.  When it does
        # not hold the driver inserts spill code and starts over
        # (0x47044..0x47198), which is not transcribed.
        if not cls[8] >= (regs + 3) >> 2:
            raise NotImplementedError("spill code (0x47044)")
    w20 = name_symbols(recs, symbase, w6, p376, tied)
    if cls[8] > 2 and p376 != 0:
        return w20 * 4 + 4, attempts
    return used, attempts
