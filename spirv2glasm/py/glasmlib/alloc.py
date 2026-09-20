"""alloc.py -- the straight-line register allocations (before regalloc.py).

The compiler's allocator is `f_710003b310` (2,124 lines, notes/31), and its
transcription is py/regalloc.py + py/ifg.py.  What is here is the older
reimplementation of what a straight-line block can show, kept as the
fallback the lowering uses when the transcribed allocator declines a body:
a value occupies a register from its definition until its LAST USE, and a
definition takes the lowest register free at that point -- so a result may
reuse the register of a source that dies feeding it.  `p05_ubo.vert` is the
case that forces it:

    LDC.F32X4 R0, buf0[0];              R0 defined
    MUL.F32   R0, R0, vertex.attrib[0]; R0 dies here, so the result retakes it
"""
import lex as _lex


def _vregs(line):
    """The placeholders `#n` of a line, `#(\d+)\b`: [(start, end, n)]."""
    return [(i, e, int(d)) for i, e, d
            in _lex.find_numbered(line, "#", word_end=True)]


def _sub_vregs(line, fn):
    """`#(\d+)\b` replaced by `fn(n)`."""
    return _lex.sub_numbered(line, "#", lambda d: fn(int(d)), word_end=True)


def _first_field(l):
    """`^\\s*\\S+\\s+`: (end of the first token, end of the blanks after
    it), or None."""
    f = _lex.split_first(l)
    if f is None or f[2] == f[1]:
        return None
    return f[1], f[2]


def _line_dst(l):
    """`^\s*\S+\s+([^,;]+?)\s*[,;]` matched: the destination, or None."""
    f = _first_field(l)
    if f is None:
        return None
    j, k = f
    e = k
    n = len(l)
    while e < n and l[e] != "," and l[e] != ";":
        e += 1
    if e == n:
        return None
    if e == k:
        # the destination takes back one blank, when there are two
        return l[k - 1] if k - j >= 2 else None
    return l[k:e].rstrip(_lex.WS)


def _is_output_store(l):
    """A line that stores to a program OUTPUT, which is where a block ends:
    `\s*\S+\s+(result[\w.\[\]]*)\s*,` matched."""
    f = _first_field(l)
    if f is None or not l.startswith("result", f[1]):
        return False
    n = len(l)
    e = f[1] + 6
    while e < n and (_lex.is_word(l[e]) or l[e] in ".[]"):
        e += 1
    while e < n and l[e] in _lex.WS:
        e += 1
    return e < n and l[e] == ","

_SLOT_LIMIT = 256


def _mask_of(tok):
    """The component mask a destination or source token names."""
    m = _lex.swizzle_suffix(tok)
    if m is None:
        return 0xf
    return sum(1 << "xyzw".index(c) for c in m)


def _rename(lines, assign):
    """The lines with each placeholder replaced by the register it was given."""
    return [_lex.sub_numbered(
        l, "#", lambda d: "R%d" % (assign[int(d)] // 4)
        if int(d) in assign else "#" + d, word_end=True) for l in lines]


def _operands(lines):
    """Per line: the destination (vreg, mask) or None, and the source
    (vreg, mask) list."""
    dst, srcs = [], []
    for l in lines:
        m = _line_dst(l)
        d = None
        if m and "#" in m:
            d = (int(m.split("#")[1].split(".")[0]), _mask_of(m))
        dst.append(d)
        body = l.split(",", 1)[1] if "," in l else ""
        got = []
        for i, e, dg in _lex.find_numbered(body, "#"):
            # `#(\d+)(\.[xyzw]+)?`: the token with its swizzle, if any
            if e < len(body) and body[e] == ".":
                f = e + 1
                while f < len(body) and body[f] in _lex.XYZW:
                    f += 1
                if f > e + 1:
                    e = f
            got.append((int(dg), _mask_of(body[i:e])))
        srcs.append(got)
    return dst, srcs


def _component_ranges(lines, dst, srcs, band):
    """First and last line of every (vreg, component); a BAND temp's range
    runs to the end of its block."""
    first, last = {}, {}
    for i in range(len(lines)):
        for v, mk in ([dst[i]] if dst[i] else []) + srcs[i]:
            for c in range(4):
                if mk >> c & 1:
                    first.setdefault((v, c), i)
                    last[(v, c)] = i
    ends = [i for i, l in enumerate(lines) if _is_output_store(l)]
    for (v, c) in list(last):
        if v not in band:
            continue
        for e in ends:
            if e >= last[(v, c)]:
                last[(v, c)] = e
                break
    return first, last


def _slot_fits(v, slot, first, last, taken):
    for c in range(4):
        if (v, c) not in last:
            continue
        s = slot + c
        for (ov, oc), os_ in taken.items():
            if os_ != s:
                continue
            # A definition landing on another value's LAST USE is a touch,
            # not an overlap -- the convention notes/52 measured (inclusive
            # overlap scored 55/430 against this one's 202/418).  It is what
            # lets `op_div.vert`'s multiply retake the register its
            # reciprocals die in.
            if last[(v, c)] <= first[(ov, oc)] \
                    or last[(ov, oc)] <= first[(v, c)]:
                continue
            return False
    return True


def _allocate_components(lines, band=(), wide=()):
    """The compiler's allocation, per COMPONENT SLOT (notes/52).

    Three facts, all read rather than fitted:

      * interference is PER COMPONENT -- an edge records, for each component of
        one value, which components of the other it conflicts with, so two
        values can share a register in different components (section 3e);
      * a BAND temp is live to the end of its block, because the live set is
        re-seeded per block from the block's annotation (section 7);
      * the search is FIRST FIT over component slots, four to a register, with
        the candidate advancing by `pow2ceil(ncomponents)` (section 3d).

    `tools/ifgcheck.py` checks the first against the compiler's own graph and
    agrees on every straight-line probe.
    """
    dst, srcs = _operands(lines)
    first, last = _component_ranges(lines, dst, srcs, band)
    width = {}
    for d in dst:
        if d:
            v, mk = d
            width[v] = max(width.get(v, 0), bin(mk).count("1"))
    # Registers are handed out in CREATION order, which is the placeholder's
    # own number -- `un_sqrt.vert` creates its four `RSQ` temps in ascending
    # component order and emits them in descending order, and the compiler
    # gives component c register c.
    taken = {}
    assign = {}
    for v in sorted(set(v for v, _ in last)):
        # A BAND temp occupies a whole register.  `un_sqrt.vert`'s four `RSQ`
        # results are written `.x` only and still take `R0`..`R3`, one each:
        # the vreg is four components wide whatever the node's write mask is,
        # which is also why the band size is the register count.  `wide` is
        # the stride set and `band` the live-range one.  They are the same for
        # a temp seeded live at every block, but a composite construct's vreg
        # and the gathers that feed it take a REGISTER each (their symbols are
        # consecutive, 512, 513, 514) without being live past their last read
        # -- which is exactly how `co_mix4.vert` puts the construct and its
        # first gather both in `R0`, one in `.x` before the other writes it.
        n = 4 if (v in band or v in wide) else width.get(v, 4)
        stride = 1 if n <= 1 else (2 if n == 2 else 4)
        slot = 0
        while not _slot_fits(v, slot, first, last, taken):
            slot += stride
            if slot > _SLOT_LIMIT:
                return None, 0
        for c in range(4):
            if (v, c) in last:
                taken[(v, c)] = slot + c
        assign[v] = slot
    used = (max(assign.values()) // 4 + 1) if assign else 0
    out = [_sub_vregs(l, lambda v: "R%d" % (assign[v] // 4)) for l in lines]
    _allocate_components.last_assign = assign
    return out, used


def _allocate(lines, band=()):
    """Replace `#n` value placeholders with `R%d`, and count the registers.

    `band` is the set of placeholders that are BAND temps (notes/52): the
    front end seeds them live at the top of every block, so they are live to
    the end of the program, mutually interfere, and take `R0`, `R1`, ... in
    creation order.  Everything else is an expansion's intermediate and
    coalesces into whichever register is free.  On the 86 straight-line
    probes the band size IS the listing's register count.

    A value is DEFINED at the first line it appears on and DEAD after the
    last.  At each line the values that die there are freed first, then the
    line's new value takes the lowest free register -- which is what lets a
    result retake a dying source's register (`p05_ubo.vert`) and what lets a
    condition's register be reused inside the arms of an `IF` (`cf_if.vert`).
    """
    first, last = {}, {}
    for i, l in enumerate(lines):
        for _i, _e, v in _vregs(l):
            first.setdefault(v, i)
            last[v] = i
    # A BAND temp is live to the end of ITS BLOCK, not of the program.  The
    # compiler re-seeds the live set per block from that block's own
    # annotation (notes/52), and `un_clamp.vert` shows the difference: its MIN
    # is in block 1's annotation only, so the scratch register of the later
    # component stores reuses `R0` even though the MIN is a band temp.  A
    # block here ends at a store to a program OUTPUT, which is what the `0x8`
    # markers delimit in the emit list.
    _ends = [i for i, l in enumerate(lines) if _is_output_store(l)]
    for v in band:
        if v not in last:
            continue
        for e in _ends:
            if e >= last[v]:
                last[v] = e
                break
    assign, free, used = {}, [], 0
    out = []
    for i, l in enumerate(lines):
        toks = []
        for _i, _e, v in _vregs(l):
            if v not in toks:
                toks.append(v)
        for v in list(assign):                  # free the ones already dead
            # A band temp's range was extended to the end of its block, and
            # the block's last line need not mention it, so releasing only the
            # values that APPEAR here would never release it at all.
            if last[v] < i and assign[v] not in free:
                free.append(assign[v])
        for v in toks:                          # free the ones dying here
            if last[v] == i and v in assign and first[v] != i:
                free.append(assign[v])
        free.sort()
        for v in toks:                          # then define the new one
            if v not in assign:
                if free:
                    assign[v] = free.pop(0)
                else:
                    assign[v] = used
                    used += 1
                if last[v] == i:
                    free.append(assign[v])
                    free.sort()
        out.append(_sub_vregs(l, lambda v: "R%d" % assign[v]))
    return out, used
