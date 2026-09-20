"""ifg.py -- the interference graph `f_7100043460` builds, from the
converter's own lines, and the colouring of notes/54 run on it.

WHAT IS READ AND WHAT IS NOT.  The sweep is `f_7100043460` (vt[16]) as
notes/54 §10 reads it: positions are visited LAST TO FIRST; at a block
boundary the live set is re-seeded from the block's annotation; at each
position the DEFS are edged against every live member (`f_71000432c0`), then
killed, then the USES are made live.  An edge is kept on the HIGHER index's
list (`f_71000432c0` at 0x43414), duplicates are merged into the earliest
(`L_7100043c7c`), the lists are mirrored with the component matrix
transposed (`L_7100043e10`) and converted to the byte form the colourer
reads (`L_7100043f04`).  All of that is transcribed here.

The ANNOTATION -- the per-block live-out set the sweep is seeded from -- is
now READ (notes/55, py/dataflow.py, checked on every probe and corpus
shader): the compiler's `f_710006e7c0` makes it over cgc NAMES as

    every name the block STORES, at the mask it stores (at --opt-level none
    every store qualifies, `cg->vt[872]`), plus ordinary per-component
    liveness over the successors.

`annotation()` applies that rule to the converter's values.  What is still
a model is which converter values ARE names -- the compiler's names are the
front end's variables and temps, which the converter approximates by its
band temps -- and the record ORDER (`order_records`): `f_7100036760`
numbers the front end's names first and `f_7100036a70` the lowering's temps
after them, and the converter approximates the first group by the band.
"""

import os
import sys

import lex as _lex
import regalloc

def _place(name):
    r"""The vreg of a placeholder `#n` (`#(\d+)` in full), or None."""
    return int(name[1:]) if _lex.is_numbered(name, "#") else None


def _mask4(tok):
    m = _lex.swizzle_suffix(tok, 1, 4)
    if m is None:
        return 0xF
    out = 0
    for c in m:
        out |= 1 << "xyzw".index(c)
    return out


def _bytes(m4):
    """A 4-bit component mask as the byte-per-component form."""
    return sum(0xff << (8 * c) for c in range(4) if (m4 >> c) & 1)


def positions(items):
    """(defs, uses) per line: `[(vreg, mask4)]` each.

    `items` are `sched.parse` results; a destination or source that is a
    placeholder `#n` names vreg n.  Anything else (an interface operand, a
    constant, a condition register) is not a temp and takes no part.
    """
    out = []
    for it in items:
        if it is None:
            out.append(([], []))
            continue
        _mnem, (dst, dmask), srcs = it
        defs = []
        m = _place(dst.strip()) if dst else None
        if m is not None:
            defs.append((m, dmask))
        uses = []
        # A component-wise instruction reads an unswizzled source through its
        # write mask; a dot product and the memory and texture forms read
        # the whole operand whatever they write.
        head = _mnem.split(".")[0]
        whole = head.startswith(("DP", "TEX", "TXL", "TXF", "LD"))
        for name, smask in srcs:
            m = _place(name.strip())
            if m is not None:
                if smask == 0xF and dmask and not whole:
                    smask = dmask
                uses.append((m, smask))
        out.append((defs, uses))
    return out


def _live_out(live_in, b, succ):
    """The union of the successors' live-in (the next block when `succ`
    says nothing about `b`)."""
    out = {}
    for s in (succ or {}).get(b, (b + 1,)):
        for v, m in live_in[s].items():
            out[v] = out.get(v, 0) | m
    return out


def if_successors(lines, spans):
    """The forward edges of structured `IF / ELSE / ENDIF` (notes/67 §8).

    The linear model -- every block flows into the next -- is right for a
    straight line and for the loops (whose carried values are live across
    the back edge anyway), but a THEN arm does not flow into its ELSE arm:
    its block ends at the ELSE, which branches to the ENDIF.  The
    compiler's dataflow (`f_710006e7c0`, py/dataflow.py) follows the real
    edges, and on `sc_select.frag` the result register of the select
    (live in the ELSE arm only) does not meet the THEN arm's operand.

    Each structure word is a block of its own (`sched.span_sizes`), so the
    IF block's successors are the THEN arm and the block after the ELSE
    (or the ENDIF), and the block before an ELSE goes to the ENDIF.
    """
    succ = _structured_edges(_structure_words(lines, spans), len(spans),
                             _trailing_structure(lines, spans))
    _call_edges(lines, spans, succ)
    return succ


def _trailing_structure(lines, spans):
    """THE STRUCTURE WORDS THAT END THE PROGRAM (`ENDREP; RET;`): the
    scheduler does not schedule the trailing terminators and the caller
    hands them to the LAST block (`sched.span_sizes`), so they are no
    blocks of their own here -- `compute_volumefog_scatter-1`'s loop is the
    whole program and its ENDREP was lost with them, and with it the back
    edge: the compiler's last block marker (`tools/livecheck.py`, pos 277)
    keeps the loop's carried values live, ours let them die in the body.
    The ENDREP / ENDIF lines at the end of a multi-line last block, the
    trailing RETs skipped, in program order.  `G2S_NOTAILSTRUCT=1` drops
    them."""
    if not spans or len(spans[-1]) < 2 or os.environ.get("G2S_NOTAILSTRUCT"):
        return ()
    out = []
    for i in reversed(spans[-1]):
        t = lines[i].split()
        w = t[0].rstrip(";") if t else ""
        if w == "RET" and not out:
            continue
        if w not in ("ENDREP", "ENDIF"):
            break
        out.append(w)
    return tuple(reversed(out))


_STRUCTURE_WORDS = ("IF", "ELSE", "ENDIF", "REP.S", "REP", "ENDREP", "BRK",
                    "CONT")


def _structure_words(lines, spans):
    """{block: its structure word} for the blocks that are one."""
    kind = {}
    for b, sp in enumerate(spans):
        if len(sp) == 1:
            w = lines[sp[0]].split()[0].rstrip(";") if lines[sp[0]].split() \
                else ""
            if w in _STRUCTURE_WORDS:
                kind[b] = w
    return kind


def _structured_edges(kind, nblocks, tail=()):
    """The IF / ELSE / ENDIF edges and the loops' back edges.

    `tail`: the structure words inside the last block, after its lines
    (`_trailing_structure`).  Each is the empty block it is in the
    compiler, placed after the last one; `nblocks` is the program's end."""
    succ, stack = {}, []
    loops = []                      # [REP block, [BRK blocks], [CONT blocks]]
    for b in range(nblocks):
        k = kind.get(b)
        if k == "IF":
            stack.append([b, None])
        elif k == "ELSE" and stack:
            stack[-1][1] = b
        elif k == "ENDIF" and stack:
            i, e = stack.pop()
            if e is None:
                succ[i] = (i + 1, b)
            else:
                succ[i] = (i + 1, e + 1)
                if e - 1 > i:
                    succ[e - 1] = (b,)
        elif k in ("REP.S", "REP") and not os.environ.get("G2S_NOLOOPEDGES"):
            loops.append([b, [], []])
        elif k == "BRK" and loops:
            loops[-1][1].append(b)
        elif k == "CONT" and loops:
            loops[-1][2].append(b)
        elif k == "ENDREP" and loops:
            # THE LOOP'S BACK EDGE (notes/90): the compiler's dataflow
            # (f_710006e7c0) follows the real edges, so a value the loop
            # reads at its head is live through the whole body -- the
            # ENDREP block goes back to the body's first block, a BRK also
            # to the block after the ENDREP, a CONT to the head.
            r, brks, conts = loops.pop()
            succ[b] = (r + 1,)
            for k2 in brks:
                succ[k2] = (k2 + 1, b + 1)
            for k2 in conts:
                succ[k2] = (k2 + 1, r + 1)
    if tail:
        _tail_edges(succ, stack, loops, tail, nblocks)
    return succ


def _tail_edges(succ, stack, loops, tail, nblocks):
    """`_structured_edges` for the trailing structure words: virtual block k
    (tail[k]) follows the last real block.  An ENDREP goes back to its
    loop's first body block, an ENDIF falls through; `after(k)` is where
    control goes past virtual block k -- the next one's successor, or the
    program's end."""
    n = len(tail)
    through = [None] * (n + 1)          # the successor of virtual block k
    through[n] = nblocks
    closes = [None] * n
    for k in range(n):                  # innermost first, as they close
        if tail[k] == "ENDREP" and loops:
            closes[k] = loops.pop()
        elif tail[k] == "ENDIF" and stack:
            closes[k] = stack.pop()
    for k in range(n - 1, -1, -1):
        through[k] = (closes[k][0] + 1 if tail[k] == "ENDREP"
                      and closes[k] is not None else through[k + 1])
    last = nblocks - 1
    succ[last] = (through[0],)
    for k in range(n):
        c = closes[k]
        if c is None:
            continue
        if tail[k] == "ENDREP":
            r, brks, conts = c
            for k2 in brks:
                succ[k2] = (k2 + 1, through[k + 1])
            for k2 in conts:
                succ[k2] = (k2 + 1, r + 1)
        else:
            # the ENDIF's block is empty: reaching it is reaching what
            # follows it
            i, e = c
            if e is None:
                succ[i] = (i + 1, through[k])
            else:
                succ[i] = (i + 1, e + 1)
                if e - 1 > i:
                    succ[e - 1] = (through[k],)


def _call_edges(lines, spans, succ):
    """CALLS (notes/68): the block holding a CAL goes to the subroutine's
    first block, the subroutine's last block (the one its RET ends) back to
    the block after the CAL, and a RET's own block goes nowhere."""
    label, cal, rets = {}, {}, []
    for b, sp in enumerate(spans):
        if not sp:
            continue
        t = lines[sp[0]].strip()
        if len(sp) == 1 and t.startswith("BB") and t.endswith(":"):
            label[t[:-1]] = b
        elif len(sp) == 1 and t.startswith("CAL"):
            cal[b] = t.split()[1]
        elif len(sp) == 1 and t.startswith("RET"):
            rets.append(b)
            succ[b] = ()
    for b, name in cal.items():
        l = label.get(name)
        if l is None:
            continue
        succ[b] = (l,)
        # the subroutine ends at the first RET block after its label, or
        # with the program (a trailing RET is not a block of its own)
        end = next((r for r in rets if r > l), len(spans))
        succ[end - 1] = (b + 1,)


def annotation(pos, spans, band, succ=None):
    """The per-block live-out the sweep starts from (notes/55).

    For each block: every band temp DEFINED in it, at the mask it is
    written with; and every temp's components that a successor block
    reads before redefining (`succ`: `if_successors`, else the next
    block).
    """
    nb = len(spans)
    # precise per-component live-in of each block, computed backward -- to
    # a FIXPOINT, since a loop's back edge (`if_successors`) points to an
    # earlier block
    live_in = [dict() for _ in range(nb + 1)]
    changed = True
    while changed:
        changed = False
        for b in range(nb - 1, -1, -1):
            live = _live_out(live_in, b, succ)
            for p in reversed(spans[b]):
                defs, uses = pos[p]
                for v, m in defs:
                    if v in live:
                        live[v] &= ~m
                        if not live[v]:
                            del live[v]
                for v, m in uses:
                    live[v] = live.get(v, 0) | m
            if live != live_in[b]:
                live_in[b] = live
                changed = True
    ann = []
    for b in range(nb):
        seed = _live_out(live_in, b, succ)
        for p in spans[b]:
            for v, m in pos[p][0]:
                if v in band:
                    # notes/55: a store is live out of its block at the mask
                    # it STORES (f_710006e7c0 0x6ee40, cg->vt[872])
                    seed[v] = seed.get(v, 0) | m
        ann.append(seed)
    return ann


def _output_defs(items, outputs):
    """Per line, the OUTPUT it writes as `[(key, mask)]`: an output is a
    record of its own (finish.py `_output_records`), numbered ahead of every
    temp, so its key is negative -- it only takes a place in the live array
    (`_sweep`), no edge and no colour.  None when there are none."""
    if not outputs:
        return None
    out = []
    for it in items:
        k = None
        if it is not None and it[1][0] in outputs:
            k = outputs[it[1][0]] - 1000000.0
        out.append([(k, it[1][1] or 0xF)] if k is not None else [])
    return out


def build(pos, spans, band, index, succ=None, odefs=None):
    """Every record's neighbour list, as `f_7100043460` leaves `record[216]`.

    `index` maps a placeholder to its record index.  Returns
    `({idx: [Edge...]}, {idx: first-def position}, {idx: last-use position},
    pressure)`.
    """
    ann = annotation(pos, spans, band, succ)
    raw, c56, c60, pressure = _sweep(pos, spans, ann, index, odefs)
    _merge_duplicates(raw)
    _mirror(raw)
    graph = {}
    for owner, lst in raw.items():
        graph[owner] = [
            regalloc.Edge(other, [_bytes((h >> (4 * c)) & 0xF)
                                  for c in range(4)])
            for other, h in lst]
    return graph, c56, c60, pressure


def _live_count(live):
    # the class's own records: an output's place in the array is not
    # counted (finish.py `_output_records`)
    return sum(bin(m).count("1") for r, m in live.items() if r >= 0)


def _interference_word(om, xm):
    """f_7100042c90: nibble c of the owner's word is the other's live
    components, for every component c of the owner's mask."""
    h12 = 0
    for c in range(4):
        if (om >> c) & 1:
            h12 |= xm << (4 * c)
    return h12


_LIVEDBG = bool(os.environ.get("G2S_LIVEDBG"))


def _sweep(pos, spans, ann, index, odefs=None):
    """The backward sweep of `f_7100043460` over every block, each seeded
    with its annotation: `({owner: [(other, word)]} pushed at the head,
    first-def positions, last-use positions, pressure)`.

    `odefs` (`_output_defs`): the OUTPUTS each line writes.  An output is a
    record of the live array too (`g2s_trace_liveset`, `monster_a608a03b`:
    the block's seed is `6 16 99 800 805 814 ..`, 6 being `result_color4`),
    live from its write to the end of its block like a name's store, at
    the mask it stores -- it makes no edge, but its kill swap-removes it,
    and that moves the array's last element (the ADD, vr 1267) to the
    front."""
    oseed = []
    for span in spans:
        _os = {}
        for p in span:
            for _k, _m in (odefs[p] if odefs else ()):
                _os[_k] = _os.get(_k, 0) | _m
        oseed.append(_os)
    raw = {}                    # idx -> [(other, h12)] pushed at the head
    c56, c60 = {}, {}
    pressure = 0
    order = [(b, p) for b, span in enumerate(spans) for p in span]
    live = {}
    # the live ARRAY `this[24]` (py/liveness.py `sweep`): the edges walk it,
    # not the nibble set.  A block starts it from its seed in ascending
    # record order (`_enter_block`, NEXT from 1); a record made live joins
    # at the end (0x43ac4); a record whose last component is killed is
    # swap-removed -- the array's last element takes its slot (0x438d0).
    # G2S_LIVEINSERT gives the old walk, the set in insertion order.
    old_walk = bool(os.environ.get("G2S_LIVEINSERT"))
    arr, at = [], {}
    cur_block = None
    for k in range(len(order) - 1, -1, -1):
        b, p = order[k]
        if b != cur_block:
            cur_block = b
            live = dict((index[v], m) for v, m in ann[b].items()
                        if v in index and m)
            live.update(oseed[b])
            arr = sorted(live)
            at = dict((r, i) for i, r in enumerate(arr))
        defs, uses = pos[p]
        pressure = max(pressure, _live_count(live))
        dl = [(index[v], dm) for v, dm in defs]
        ol = odefs[p] if odefs else []
        # f_71000432c0: every def's edges against the live array, then the
        # kills (0x438d0), as `_def_edges` / `_kill`
        for d, dm in dl:
            if d not in c56:
                c56[d] = k
            _walk = (list(live) if old_walk else list(arr))
            for m in _walk:
                mm = live.get(m, 0)
                if m == d or not mm or m < 0:
                    continue            # an output makes no edge
                if d <= m:
                    owner, other, om, xm = m, d, mm, dm
                else:
                    owner, other, om, xm = d, m, dm, mm
                raw.setdefault(owner, []).insert(
                    0, (other, _interference_word(om, xm)))
            if old_walk:
                if d in live:
                    live[d] &= ~dm
                    if not live[d]:
                        del live[d]
        if not old_walk:
            for d, dm in dl + ol:
                rem = live.get(d, 0) & ~dm
                if rem:
                    live[d] = rem
                    continue
                live.pop(d, None)
                i = at.pop(d, None)
                if i is None:
                    continue
                last = arr.pop()
                if last != d:
                    arr[i] = last
                    at[last] = i
        for v, um in uses:
            u = index[v]
            live[u] = live.get(u, 0) | um
            if u not in c60:
                c60[u] = k
        if not old_walk:
            for v, _um in uses:
                u = index[v]
                if u not in at:
                    at[u] = len(arr)
                    arr.append(u)
        pressure = max(pressure, _live_count(live))
        if _LIVEDBG:
            # the live array after this position, as `g2s_trace_liveset`
            # prints it (compare with tools/livecheck.py)
            print("LIVE pos %d defs=%s arr=%s" % (k, [d for d, _m in dl],
                                                 list(arr)), file=sys.stderr)
    return raw, c56, c60, pressure


def _merge_duplicates(raw):
    """L_7100043c7c: merge duplicates into the LAST occurrence in list order
    (the earliest pushed), keeping list order."""
    for owner, lst in raw.items():
        acc = {}
        for other, h in lst:
            acc[other] = acc.get(other, 0) | h
        last = {}
        for i, (other, _h) in enumerate(lst):
            last[other] = i
        raw[owner] = [(other, acc[other]) for i, (other, _h) in enumerate(lst)
                      if last[other] == i]


def _transpose(h):
    """The 4x4 component matrix of an interference word, transposed."""
    t = 0
    for c in range(4):
        for j in range(4):
            if (h >> (4 * c + j)) & 1:
                t |= 1 << (4 * j + c)
    return t


def _mirror(raw):
    """L_7100043e10: mirror, ascending owner, pushing at the head of the
    other record's list with the component matrix transposed."""
    for owner in sorted(raw):
        for other, h in list(raw[owner]):
            raw.setdefault(other, []).insert(0, (owner, _transpose(h)))


def walk_keys(lines, items, spans, carriers=frozenset()):
    """Each non-band vreg's place in `f_7100036a70`'s two walks (notes/77).

    The lowering's temps get their records per block, in two walks over the
    block's nodes: the first (callback `f_7100036b10`) numbers the COPIES
    (IR opcode 0x47), the second (`f_7100036cc0` -> `f_7100036e34`) every
    other node.  `mb_n18.vert`'s block 3: the construct's vreg is 55, ahead
    of the LDC, the ADD (57) and the address MULs (58..) created before it.
    A carrier (0x4a, `MOV.S` in print) is not a copy.  Returns
    `{vreg: (block, walk)}` for the first definition of each vreg."""
    out = {}
    for b, span in enumerate(spans):
        for p in span:
            it = items[p]
            if it is None:
                continue
            _mn, (dst, _dm), _srcs = it
            v = _place(dst or "")
            if v is None:
                continue
            if v in out:
                continue
            is_copy = _mn.startswith("MOV") and v not in carriers
            out[v] = (b, 0 if is_copy else 1)
    return out


def merge_chains(items, spans, band):
    """THE MERGE CHAINS each block's first numbering walk takes (notes/107):
    `{vreg: (block, key)}` for every lowering temp written by two or more
    lines with disjoint partial masks -- a construct written lane by lane,
    whose lanes the compiler merges into one 0x57 chain.  The key orders a
    block's merges as the walk meets them: it takes the block's roots LAST
    first, so the merge read last goes first (the negated line of its last
    reader)."""
    defs, blk, last = {}, {}, {}
    for b, span in enumerate(spans):
        for p in span:
            it = items[p]
            if it is None:
                continue
            _mn, (dst, dm), srcs = it
            for nm, _sm in srcs:
                m = _place(nm or "")
                if m is not None:
                    last[m] = p
            v = _place(dst or "")
            if v is not None and v not in band:
                defs.setdefault(v, []).append(dm)
                blk.setdefault(v, b)
    out = {}
    for v, masks in defs.items():
        acc, disjoint = 0, True
        for m in masks:
            if m == 0xF or acc & m:
                disjoint = False
            acc |= m
        if len(masks) >= 2 and disjoint:
            out[v] = (blk[v], -last.get(v, 0))
    return out, blk


def order_records(vregs, band, order_key=None, walks=None, merges=None):
    """Record index per placeholder: the band first, then the rest, each in
    creation order (the placeholder's own number), from index 1.

    `order_key` maps a placeholder to the number it is created AT when that
    is not its own: a temp's lowering vreg that the flush renamed (notes/72)
    was made where the value was, so it takes the value's original number --
    `ce_head.vert`'s records put the ADD the flush renamed (26) right after
    the LDC (25), not after every other temp."""
    key = order_key or {}
    first = sorted(v for v in vregs if v in band)
    if walks is not None:
        rest = sorted((v for v in vregs if v not in band),
                      key=lambda v: (walks.get(v, (0, 1)),
                                     key.get(v, v), v))
    else:
        rest = sorted((v for v in vregs if v not in band),
                      key=lambda v: (key.get(v, v), v))
    if merges is not None and walks is None:
        # EACH BLOCK'S MERGE CHAINS COME FIRST (notes/107): `f_7100036a70`
        # numbers a block's temps in two walks, and the first numbers only
        # the 0x57 merges -- `map_3587d848`'s one walk-1 number is 169, the
        # construct `vec4(..)` written lane by lane, ahead of every temp its
        # block's second walk numbers (the LDC 178, created before it).  The
        # rest keep their creation order.
        _mc, _blk = merges
        _by_blk = {}
        for v in rest:
            if v in _mc:
                _by_blk.setdefault(_mc[v][0], []).append(v)
        rest = [v for v in rest if v not in _mc]
        for b in sorted(_by_blk, reverse=True):
            at = next((i for i, w in enumerate(rest)
                       if _blk.get(w, -1) >= b), len(rest))
            rest[at:at] = sorted(_by_blk[b], key=lambda v: _mc[v])
    return dict((v, i + 1) for i, v in enumerate(first + rest))


def _records(index, pos):
    """The allocator's records: 0 the reserved one, then one per placeholder
    -- an R-class vec4 temp (type 6, class 3) with the components its
    definitions write."""
    recs = [regalloc.Rec(i) for i in range(len(index) + 1)]
    recs[0].sym = 0
    recs[0].type = 1
    for v, i in index.items():
        r = recs[i]
        r.type = 6
        r.b14 = 1
        r.h22 = 4
        r.cls = 3
        cm = 0
        for defs, _uses in pos:
            for dv, dm in defs:
                if dv == v:
                    cm |= dm
        r.cm = _bytes(cm)
    return recs


def _long_singles(items):
    """THE LONG HANDLES ARE RECORDS TOO (notes/87): every `LDC.U64 Dn.x` and
    `OR.S Dn.x` is a vreg of its own, one component wide, and the driver's
    single-component count (`regalloc.singles`, 0x46c40) runs over every
    record whatever its class -- `ps_c.frag`'s one R-class single (`t`) plus
    three handles crosses the `nsingles >= 2` line of the cost formula, and
    the colouring order with it."""
    if os.environ.get("G2S_NODSINGLES"):
        return 0
    return sum(1 for it in items
               if it is not None and _lex.is_numbered(it[1][0] or "", "D")
               and it[1][1] in (1, 2, 4, 8))




def _long_def(line):
    r"""`\s*(LDC\.U64|OR\.S)\s+D(\d+)\.x\s*,` matched: (the mnemonic,
    the handle number, the index of its `D`), or None."""
    f = _lex.split_first(line)
    if f is None or f[2] == f[1] or line[f[0]:f[1]] not in ("LDC.U64",
                                                            "OR.S"):
        return None
    d = f[2]
    if not line.startswith("D", d):
        return None
    e = _lex.digit_end(line, d + 1)
    if e == d + 1 or not line.startswith(".x", e):
        return None
    n = int(line[d + 1:e])
    e += 2
    while e < len(line) and line[e] in _lex.WS:
        e += 1
    if e < len(line) and line[e] == ",":
        return line[f[0]:f[1]], n, d
    return None


def _long_names(line):
    r"""`\bD(\d+)\b` found: [(start, number)]."""
    return [(i, int(dg)) for i, _e, dg
            in _lex.find_numbered(line, "D", word_end=True, word_start=True)]


def _long_positions(lines):
    """THE LONG CLASS'S VREGS (notes/104): every `LDC.U64 Dn.x` and every
    `OR.S Dn.x` defines a vreg of its own, and a later `Dn` reads the last
    one defined.  Returns `(pos, keys, sites)`: the `positions()` form over
    those vregs (one component, `.x`), each vreg's creation key, and per
    line the vregs its `D` tokens name, in token order.

    The key is the compiler's record order, read from the `g2s_simp` dump
    (`ps_b`, `ps_c`, `map_17745a9b`): the ORs -- the sampled images --
    before every handle load, and each group in the order the lowering
    made it (the handle number the converter gave it)."""
    cur = {}
    pos, keys, sites = [], {}, []
    nxt = 0
    for line in lines:
        m = _long_def(line)
        dest_at = m[2] if m else -1
        toks = _long_names(line)
        uses, site = [], []
        for at, n in toks:
            if at == dest_at:
                site.append(None)           # the def, filled in below
                continue
            if n not in cur:
                raise ValueError("D%d read before any definition" % n)
            uses.append((cur[n], 1))
            site.append(cur[n])
        defs = []
        if m:
            v = nxt
            nxt += 1
            keys[v] = (0 if m[0] == "OR.S" else 1, m[1], v)
            cur[m[1]] = v
            defs.append((v, 1))
            site = [v if s is None else s for s in site]
        pos.append((defs, uses))
        sites.append(site)
    return pos, keys, sites


def allocate_long(lines, spans):
    """Colour the LONG handles with the compiler's allocator, CLASS 4
    (notes/104).  `f_71000473b0` runs the same driver over class 4 after
    class 3: the `g2s_simp` dump shows K = the class's pressure
    (`cg36[4]`), `cls8` 4096, `cls12` 4, five attempts with the first of
    equal results kept, and records of type 10 (`width` 1), one component
    (`cm` 0xff), `h22` 4.  The graph is the same sweep over the same pass-1
    lines, on these vregs.

    Returns the lines with every `Dn` renamed to its colour and the D
    register count, or None when the colouring leaves the transcribed
    path."""
    pos, keys, sites = _long_positions(lines)
    if not keys:
        return list(lines), 0
    order = sorted(keys, key=lambda v: keys[v])
    index = dict((v, i + 1) for i, v in enumerate(order))
    # THE SAMPLED IMAGES ARE NAMES: the block that makes an `OR.S` handle
    # STORES it, so it is in the block's live-out seed at the mask it writes
    # (notes/55, `+0xb0`).  `hd_f.frag`'s block 2 seeds names 12 and 15 --
    # records 7 and 10, the two ORs, type 10 -- and the seed's class-4
    # counter is 2 (`g2s_seedin`/`g2s_seedout`); the handle LOADS are not
    # in it.  So an OR is live from the block's end back to its def, and
    # meets every handle made after it: the second image's sampler takes D2.
    names = (frozenset() if os.environ.get("G2S_NOORNAME")
             else frozenset(v for v in keys if keys[v][0] == 0))
    graph, c56, c60, pressure = build(pos, spans, names, index,
                                      if_successors(lines, spans))
    recs = [regalloc.Rec(i) for i in range(len(index) + 1)]
    recs[0].sym = 0
    recs[0].type = 1
    for v, i in index.items():
        r = recs[i]
        r.type = 10
        r.b14 = 1
        r.h22 = 4
        r.cls = 4
        r.cm = _bytes(1)

    def graph_fn(rs):
        for r in rs:
            r.nb = [regalloc.Edge(e.idx, e.words) for e in
                    graph.get(r.idx, [])]
            r.c56 = c56.get(r.idx, -1)
            r.c60 = c60.get(r.idx, -1)

    # `extra_singles`: the R class's single-component records count too
    # (0x46c40 runs over every record), but every record here is a single,
    # so with two or more of them the count is past the formula's
    # `nsingles >= 2` line whatever the R class adds, and one record alone
    # has no neighbour to be costed against.
    try:
        used, _att = regalloc.allocate(recs, 4, {8: 4096, 12: 4, 29: 1},
                                       pressure, 0, 0, None, 512, 0, 4096,
                                       graph_fn)
    except NotImplementedError:
        return None
    colour = {}
    for v, i in index.items():
        if recs[i].w64 < 0:
            return None
        colour[v] = recs[i].w64 // 4
    out = []
    for line, site in zip(lines, sites):
        if site:
            it = iter(site)
            line = _lex.sub_numbered(line, "D",
                                     lambda _d: "D%d" % colour[next(it)],
                                     word_end=True, word_start=True)
        out.append(line)
    return out, (used + 3) >> 2 if used >= 0 else 0




def _cond_vreg(name):
    r"""`^C\$(\d+)$`: the condition vreg's number, or None."""
    return int(name[2:]) if _lex.is_numbered(name, "C$") else None


def _cond_positions(items):
    """THE CONDITION CLASS'S VREGS (notes/106): every `.CC` set defines one
    (`sched.parse` names it `C$k` while the lines carry the numbering), and
    every predicated write and `IF`/`KIL` test reads the one it is tagged
    with.  The `positions()` form over those vregs, and each vreg's written
    components."""
    pos, cm = [], {}
    for it in items:
        defs, uses = [], []
        if it is not None:
            _mn, (dst, dm), srcs = it
            k = _cond_vreg(dst or "")
            if k is not None:
                defs.append((k, dm))
                cm[k] = cm.get(k, 0) | dm
            for nm, sm in srcs:
                m = _cond_vreg(nm or "")
                if m is not None:
                    uses.append((m, sm))
        pos.append((defs, uses))
    return pos, cm


def _one_component(m4):
    return m4 in (1, 2, 4, 8)


def _cond_singles(items):
    """The condition vregs written in ONE component: the driver's
    single-component count (0x46c40) runs over every record, whatever its
    class, so the other classes' costs count these too."""
    if os.environ.get("G2S_NOCCVREG"):
        return 0
    _pos, cm = _cond_positions(items)
    return sum(1 for m in cm.values() if _one_component(m))


def _r_singles(items):
    """The R class's single-component records (`b12` is never set on them,
    `_records`): the placeholders whose definitions write one component."""
    cm = {}
    for defs, _uses in positions(items):
        for v, m in defs:
            cm[v] = cm.get(v, 0) | m
    return sum(1 for m in cm.values() if _one_component(m))


def allocate_cond(lines, spans):
    """Colour the CONDITION REGISTERS with the compiler's allocator, CLASS 1
    (notes/106).  `f_7100046b60` runs class 1 before class 3 (`g2s_eng
    class=1`, `map_02774da9`), called with `symbase` 0x100, `w6` 0 and `w7`
    2 (gdb at its entry, `kd_a.frag`); `g2s_simp` gives `cls8` 2 -- CC0 and
    CC1 -- and `cls12` 4; the records are type 26, `h22` 4, their `cm` the
    components the set writes (`RC.xy` is 0xffff).  `tools/simpcheck.py`
    reproduces every class-1 attempt of `map_9a0b11a0` with the transcribed
    simplify/select, on the compiler's graph.  The graph is the same sweep
    over the same pass-1 lines, on these vregs.

    Returns `{k: register}` (0 or 1), or None when the colouring leaves the
    transcribed path."""
    items = [_sched_parse(l) for l in lines]
    pos, cm = _cond_positions(items)
    if not cm:
        return {}
    order = sorted(cm)
    index = dict((k, i + 1) for i, k in enumerate(order))
    graph, c56, c60, pressure = build(pos, spans, frozenset(), index,
                                      if_successors(lines, spans))
    recs = [regalloc.Rec(i) for i in range(len(index) + 1)]
    recs[0].sym = 0
    recs[0].type = 1
    for k, i in index.items():
        r = recs[i]
        r.type = 26
        r.b14 = 1
        r.h22 = 4
        r.cls = 1
        r.cm = _bytes(cm[k])

    def graph_fn(rs):
        for r in rs:
            r.nb = [regalloc.Edge(e.idx, e.words) for e in
                    graph.get(r.idx, [])]
            r.c56 = c56.get(r.idx, -1)
            r.c60 = c60.get(r.idx, -1)

    try:
        _used, _att = regalloc.allocate(
            recs, 1, {8: 2, 12: 4, 29: 1}, pressure, 0, 0, None, 256, 0, 2,
            graph_fn, extra_singles=_r_singles(items) + _long_singles(items))
    except NotImplementedError:
        return None
    colour = {}
    for k, i in index.items():
        w = recs[i].w64
        if w < 0 or w % 4:
            # a set in another LANE of a register is not transcribed
            return None
        colour[k] = w // 4
    return colour


def _sched_parse(line):
    import sched
    return sched.parse(line)


def allocate(lines, items, spans, band, order_key=None, carriers=None,
             outputs=None):
    """Colour the converter's lines with the compiler's allocator.

    Returns `{placeholder: register}` and the register count, or None when
    the colouring leaves the transcribed path.  `carriers`, when given,
    turns on the two-walk record order (`walk_keys`).
    """
    pos = positions(items)
    vregs = set()
    for defs, uses in pos:
        for v, _m in defs + uses:
            vregs.add(v)
    if not vregs:
        return {}, 0
    walks = (walk_keys(lines, items, spans, carriers)
             if carriers is not None else None)
    index = order_records(vregs, band, order_key, walks,
                          None if os.environ.get("G2S_NOMERGEFIRST")
                          else merge_chains(items, spans, band))
    graph, c56, c60, pressure = build(pos, spans, band, index,
                                      if_successors(lines, spans),
                                      _output_defs(items, outputs))
    recs = _records(index, pos)

    def graph_fn(rs):
        for r in rs:
            r.nb = [regalloc.Edge(e.idx, e.words) for e in
                    graph.get(r.idx, [])]
            r.c56 = c56.get(r.idx, -1)
            r.c60 = c60.get(r.idx, -1)

    cls = {8: 4096, 12: 4, 29: 1}
    if os.environ.get("G2S_PRESSURE_DELTA"):
        # diagnosis only: what a different class pressure would colour
        pressure += int(os.environ["G2S_PRESSURE_DELTA"])
    try:
        used, _att = regalloc.allocate(recs, 3, cls, pressure, 0, 0, None,
                                       512, 0, 4096, graph_fn,
                                       extra_singles=_long_singles(items)
                                       + _cond_singles(items))
    except NotImplementedError:
        return None
    if os.environ.get("G2S_ATTEMPTS"):
        # diagnosis only: how many colouring attempts the class took, and
        # each attempt's result word
        import sys
        sys.stderr.write("ATTEMPTS %d %s pressure=%d\n" % (
            len(_att), [a[1] for a in _att], pressure))
    assign = {}
    for v, i in index.items():
        if recs[i].w64 < 0:
            return None
        assign[v] = recs[i].w64 // 4
    regs = (used + 3) >> 2 if used >= 0 else 0
    return assign, regs
