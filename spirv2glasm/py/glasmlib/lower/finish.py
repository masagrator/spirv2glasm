"""finish.py -- the lowered lines scheduled, allocated and printed.

THE ORDER IS THE SCHEDULER'S, and it runs BEFORE the allocator -- which is
the order the compiler works in (notes/51: the emitter schedules a block and
the allocator's sweep runs over the scheduled positions), and the only order
in which `node[36]` is available at all: it is per VREG, and after the
allocator several vregs share one register name.
"""
import lex as _lex
import sys

import sched as _sched
import ifg as _ifg
import opchain as _opchain
from glasmlib import replicate as _rep
from glasmlib.common import NotEstablished, ENV
from glasmlib.usage import _lmem_arrays
from glasmlib.alloc import _sub_vregs, _allocate, _allocate_components
from glasmlib.body import _Body

_TESTS = ("NE", "EQ", "GT", "GE", "LT", "LE")


def _ldc_line(l):
    r"""`^(LDC\.[A-Z]+\d+)(X[24])? (#\d+)(\.[xyzw]+)?, (.*)$` -> the five
    groups (None for an absent optional one), or None."""
    if not l.startswith("LDC.") or "\n" in l:
        return None
    n = len(l)
    i = 4
    while i < n and "A" <= l[i] <= "Z":
        i += 1
    if i == 4:
        return None
    e = _lex.digit_end(l, i)
    if e == i:
        return None
    g1 = l[:e]
    g2 = None
    if l.startswith("X2", e) or l.startswith("X4", e):
        g2, e = l[e:e + 2], e + 2
    if e >= n or l[e] != " " or e + 1 >= n or l[e + 1] != "#":
        return None
    f = _lex.digit_end(l, e + 2)
    if f == e + 2:
        return None
    g3 = l[e + 1:f]
    g4 = None
    if f < n and l[f] == ".":
        h = f + 1
        while h < n and l[h] in _lex.XYZW:
            h += 1
        if h == f + 1:
            return None
        g4, f = l[f:h], h
    if not l.startswith(", ", f):
        return None
    return g1, g2, g3, g4, l[f + 2:]


def _sub_labels(l, fn):
    r"""`BB@(\d+)` replaced by `fn(digits)`."""
    return _lex.sub_numbered(l, "BB@", fn) if "BB@" in l else l


def _strip_read_masks(l):
    r"""`\b(R\d+)@[0-9a-f]\b` -> `\1`: the annotated read masks dropped."""
    if "@" not in l:
        return l
    out, pos = [], 0
    for i, e, _d in _lex.find_numbered(l, "R", word_start=True):
        if (e + 1 < len(l) and l[e] == "@" and l[e + 1] in "0123456789abcdef"
                and (e + 2 == len(l) or not _lex.is_word(l[e + 2]))):
            out.append(l[pos:e])
            pos = e + 2
    out.append(l[pos:])
    return "".join(out)


def _drop_hidden_dest(l):
    r"""AN IMAGE STORE'S REGISTER (notes/111): the compiler's STOREIM node has
    one -- two MOVs read it -- but the printed line names none, so the
    internal line carries it as its first operand and the render drops it:
    `^(STOREIM\.\w+)\s+R\d+, ` -> `\1 `."""
    if not l.startswith("STOREIM."):
        return l
    n = len(l)
    i = 8
    while i < n and _lex.is_word(l[i]):
        i += 1
    if i == 8:
        return l
    j = i
    while j < n and l[j] in _lex.WS:
        j += 1
    if j == i or j >= n or l[j] != "R":
        return l
    k = _lex.digit_end(l, j + 1)
    if k == j + 1 or not l.startswith(", ", k):
        return l
    return l[:i] + " " + l[k + 2:]


def _drop_release_tags(l):
    r"""... and its release-order tag (`@rhct`, image.py `_write_release`),
    and a handle load's `@w`: `@r[htc]{3}(?=;)|(?<=\])@w(?=;)` -> ''."""
    if "@" not in l:
        return l
    out, pos = [], 0
    i = l.find("@")
    while i >= 0:
        if (l.startswith("@r", i) and i + 5 < len(l) and l[i + 5] == ";"
                and all(c in "htc" for c in l[i + 2:i + 5])):
            out.append(l[pos:i])
            pos = i + 5
        elif (i > 0 and l[i - 1] == "]" and l.startswith("@w;", i)):
            out.append(l[pos:i])
            pos = i + 2
        i = l.find("@", max(pos, i + 1))
    out.append(l[pos:])
    return "".join(out)


def _placeholder_numbers(l):
    r"""`#(\d+)` findall: the numbers' digits."""
    return [d for _i, _e, d in _lex.find_numbered(l, "#")]


def _long_tokens(text):
    r"""`\bD\d+\b` findall."""
    return ["D" + d for _i, _e, d
            in _lex.find_numbered(text, "D", word_end=True, word_start=True)]


# The condition code (notes/106): a set's mnemonic and dummy destination, a
# predicated write's test, and an `IF`/`KIL` test.
def _cc_set(line):
    r"""`^(\s*\S+?\.CC)(\s+[RH]C)((?:\.[xyzw]+)?\b)` matched: (group 1,
    group 2, end of group 2), or None.  The lazy head can only end at the
    first blank, so the first token must end in `.CC`."""
    f = _lex.split_first(line)
    if f is None:
        return None
    i, j, k = f
    if j - i < 4 or not line.endswith(".CC", 0, j):
        return None
    if k == j or k + 1 >= len(line) or line[k] not in "RH" \
            or line[k + 1] != "C":
        return None
    e = k + 2
    if e < len(line) and line[e] != "." and _lex.is_word(line[e]):
        return None
    return line[:j], line[j:e], e


def _test_at(l, i):
    """One of NE/EQ/.. at `i`, or None."""
    t = l[i:i + 2]
    return t if t in _TESTS else None


def _sub_cc_pred(line, cur):
    r"""`\((NE|..)((?:\.[xyzw]+)?)\)` -> `(\1$cur\2)`."""
    if "(" not in line:
        return line
    out, pos = [], 0
    i = line.find("(")
    n = len(line)
    while i >= 0:
        t = _test_at(line, i + 1)
        e = i + 3
        if t is not None and e < n and line[e] == ".":
            f = e + 1
            while f < n and line[f] in _lex.XYZW:
                f += 1
            if f > e + 1:
                e = f
        if t is not None and e < n and line[e] == ")":
            out.append(line[pos:i])
            out.append("(%s$%d%s)" % (t, cur, line[i + 3:e]))
            pos = e + 1
            i = line.find("(", pos)
        else:
            i = line.find("(", i + 1)
    out.append(line[pos:])
    return "".join(out)


def _sub_cc_bare(line, cur):
    r"""`^(\s*(?:IF|KIL)\s+)(NE|..)\b` -> `\1\2$cur`."""
    f = _lex.split_first(line)
    if f is None:
        return line
    i, j, k = f
    if line[i:j] not in ("IF", "KIL") or k == j:
        return line
    t = _test_at(line, k)
    if t is None or (k + 2 < len(line) and _lex.is_word(line[k + 2])):
        return line
    return "%s%s$%d%s" % (line[:k], t, cur, line[k + 2:])


def _number_conditions(lines):
    """EACH `.CC` SET IS A VREG OF ITS OWN (notes/106): `map_02774da9`'s
    node dump gives every `MOV.U.CC` its own vr (239, 241, 242, .. 290), and
    the allocator colours them as class 1 -- CC0 or CC1.  The lines are
    numbered here, in creation order: a set's dummy destination becomes
    `RC$k` (`HC$k`), and every test after it -- a predicated write's `(NE)`,
    an `IF`/`KIL` test -- reads the latest set, `NE$k`.  `sched.parse` names
    that vreg `C$k`; `_render_conditions` prints the register it is given.
    A set marked `RC$+` writes another LANE of the set before's vreg (a
    constructed bool vector, `sel_g.frag`: four sets, one vr).
    `G2S_NOCCVREG=1` leaves the lines as they are, the mark dropped."""
    if ENV.get("G2S_NOCCVREG"):
        return [l.replace("C$+", "C") for l in lines]
    out, cur, k = [], None, 0
    for line in lines:
        m = _cc_set(line)
        if m and line[m[2]:].startswith("$+") and cur is not None:
            line = "%s%s$%d%s" % (m[0], m[1], cur, line[m[2] + 2:])
        elif m and not line[m[2]:].startswith("$"):
            k += 1
            cur = k
            line = "%s%s$%d%s" % (m[0], m[1], k, line[m[2]:])
        elif cur is not None:
            line = _sub_cc_pred(line, cur)
            line = _sub_cc_bare(line, cur)
        out.append(line)
    return out


def _render_conditions(line, colour):
    """A numbered line with the condition registers `_colour_conditions`
    gave: `.CC1` and `NE1` for register 1, the plain forms for register 0,
    the numbering dropped."""
    if "$" not in line:
        return line
    # `^(\s*\S+?\.CC)(\s+[RH]C)\$(\d+)` -> `\1<1?>\2`
    m = _cc_set(line)
    if m is not None and line.startswith("$", m[2]):
        e = _lex.digit_end(line, m[2] + 1)
        if e > m[2] + 1:
            line = "%s%s%s%s" % (m[0], "1" if colour[int(line[m[2] + 1:e])]
                                 else "", m[1], line[e:])
    # `\b(NE|..)\$(\d+)` -> `\1<1?>`
    out, pos = [], 0
    i = line.find("$")
    while i >= 0:
        t = _test_at(line, i - 2) if i >= 2 else None
        e = _lex.digit_end(line, i + 1)
        if (t is not None and e > i + 1 and i - 2 >= pos
                and (i == 2 or not _lex.is_word(line[i - 3]))):
            out.append(line[pos:i - 2])
            out.append(t + ("1" if colour[int(line[i + 1:e])] else ""))
            pos = e
            i = line.find("$", e)
        else:
            i = line.find("$", i + 1)
    out.append(line[pos:])
    return "".join(out)


def _colour_conditions(lines, spans):
    """The condition registers coloured by the compiler's allocator, class 1
    (py/ifg.py `allocate_cond`), over the same order the R class is coloured
    on.  `{k: register}`, or None when that allocator declines."""
    if ENV.get("G2S_NOCCVREG"):
        return {}
    return _ifg.allocate_cond(lines, spans)


def _vkeys(lines, stmtpos):
    """Each destination's key for pass 1's entry walk (notes/68 §4).

    The compiler walks the entries as the block's NAME list gives them --
    each name where the statement walker stored it -- and the converter's
    first-write order is that list wherever a name is written by its own
    statement.  A temp's store that is flushed later (notes/67 §7), a
    function's entry copies and a call's value are written after other lines
    but belong where their statement stood, ahead of the lowering vreg that
    statement wrote.  Everything else keeps its first write IN ITS BLOCK
    (py/sched.py)."""
    return dict((d, (pos, 0, k)) for k, (d, pos) in enumerate(stmtpos.items()))


def _first_reader_in_block(lines, i, v, cutset):
    """The first line after `i` in its block that reads `v`, or None when
    `v` is rewritten or the block ends first."""
    for k in range(i + 1, len(lines)):
        if any(i < c <= k for c in cutset):
            return None
        p = _sched.parse(lines[k])
        if p is None:
            return None
        _mn, (dst, _dm), srcs = p
        if any(nm == v for nm, _sm in srcs):
            return k
        if dst == v:
            return None
    return None


def _ldc_at_reader(lines, ldc_vreg, cuts, ties, con_stmt=(),
                   con_lane_load=None):
    r"""... returns {LDC line: its CREATION key} (`_creation_order`)."""
    """A static block load's node is made AT ITS FIRST READER (notes/87).

    The reader substitutes a load into the statement that uses it (notes/67
    §1), so the LDC's node is created while that statement is lowered, just
    before the reader's own node -- not where the `OpLoad` stood.  The cut
    `mq_n14.frag`'s `u_xlat1.y = dot(mtx[1], uintBitsToFloat(ICB[i]))`: the
    element load is seq 83, the LDC of `mtx[1]` seq 90, and the LDC prints
    after the bitcast's gather, right before the DP4.  The `OpLoad` of
    `mtx[1]` comes first in the SPIR-V.  So the LDC line takes, as its
    `node[36]`, a place just ahead of its first reader in the same block."""
    tied = set()
    for t in ties:
        tied.update(t)
    cutset = sorted(cuts)
    # each line's `node[36]` as the ties make it (sched.py `_seq_keys`)
    _sk = _sched._seq_keys(len(lines), ties)
    eff = dict((k, _sk[k]) for k in range(len(lines)))
    grouped = set()
    for t in ties:
        if len([k for k in t if k < len(lines)]) > 1:
            grouped.update(t)
    # A CONSTRUCT STATEMENT IS A GROUP WHATEVER ITS SIZE (notes/114 SS15):
    # `post_glowhighpass-1.frag`'s `vec2(buf2[44], buf2[40])` writes one lane
    # with a MOV and takes the other's load as lane x, so its group has a
    # single line -- the lanes still carry the statement's `node[36]` (81),
    # and the compiler makes the two loads after it, 83 and 85, in creation
    # order (`tools/gsum.py`, block 7).  `G2S_NOCONSTMT=1` asks for two.
    if not ENV.get("G2S_NOCONSTMT"):
        for _ct, _s0 in con_stmt:
            grouped.update(k for k in _ct if k < len(lines))
    if ENV.get("G2S_TIEDBG"):
        import sys as _sys
        _n = ENV.get("G2S_TIEDBG")
        _sys.stderr.write("CON %d statements, lane loads %s\n"
                          % (len(con_stmt), sorted(con_lane_load or {})))
        for _i, _l in enumerate(lines):
            if _n in _l:
                _sys.stderr.write("LINE %d %s\n" % (_i, _l))
                for _t in ties:
                    if _i in _t:
                        _sys.stderr.write("   TIE seq=%s %s\n"
                                          % (getattr(_t, "seq", None),
                                             list(_t)))
    create = {}
    _lane = dict(con_lane_load or {})
    for i, l in enumerate(lines):
        m = _ldc_line(l)
        if m is None:
            continue
        if i in _lane and not ENV.get("G2S_NOCONSTMT"):
            # the load RETARGETED INTO THE CONSTRUCT'S LANE X: made after the
            # statement like the loads its other lanes read, in creation
            # order (the same rule as `G2S_NOLDCAFTERGROUP`'s branch)
            _ct = _lane[i]
            _e2 = min((eff.get(k, k) for k in _ct if k < len(lines)),
                      default=None)
            if _e2 is not None and i not in tied:
                _tg = _sched.Tie()
                _tg.seq = _e2 + 0.5 + 0.001 * i
                _tg.append(i)
                ties.append(_tg)
            continue
        if m[2] not in ldc_vreg:
            continue
        if i in tied:
            continue
        reader = _first_reader_in_block(lines, i, m[2], cutset)
        if reader is None:
            continue
        # THE NODE IS CREATED AT ITS READER (notes/87), and the RECORD
        # numbering meets it there (notes/107), IN THE READER'S OPERAND
        # ORDER: the loads a statement reads are made as its operands are.
        # `map_2810dda5`'s `vec4(p0, p0, p1, p1)` numbers the p0 load, the
        # lane's scratch copy of it, then the p1 load (vr 771, 772, 773;
        # `G2S_ONLY=simp`) though both OpLoads come first, and its `u_xlat5
        # = u_xlat6 + u_xlat7` numbers the two loads under that ADD by
        # their place in it (845, 846), not by their own lines.
        # `G2S_NOLDCCREATE=1` keeps each load's own line.
        _rp = _sched.parse(lines[reader])
        _k = next((x for x, (nm, _sm) in enumerate(_rp[2])
                   if nm.strip() == m[2]), 0) if _rp else 0
        if not ENV.get("G2S_NOLDCCREATE"):
            create[i] = reader - 0.5 + 0.001 * _k
        if reader == i + 1:
            continue
        tg = _sched.Tie()
        # THE SAME ORDER IS THE NODE'S `node[36]`: two loads made under one
        # reader are made as its operands are, so the second operand's load
        # is stamped after the first's and the selector keeps them that way.
        # `map_87cc6750.frag`'s `OR.S D3.x, D3, D4` of two handle halves
        # prints `buf14[344]` (its `D3`) before `buf14[1368]`, though pass 1
        # lists them the other way round.  `G2S_NOLDCOPSEQ=1` gives both the
        # reader's own key, as before.
        tg.seq = reader - 0.5
        if not ENV.get("G2S_NOLDCOPSEQ"):
            tg.seq = reader - 0.5 + 0.001 * _k
        _e = eff.get(reader, reader)
        if reader in grouped and not ENV.get("G2S_NOLDCAFTERGROUP"):
            # ... but a reader STAMPED EARLIER -- a construct's lane, whose
            # group carries the statement's `node[36]` -- was stamped before
            # its operands' loads were made: `map_2810dda5`'s `vec4(p0, p0,
            # p1, p1)` of two uniforms has its lane MOVs at seq 164 and the
            # LDCs at 166 and 168 (`tools/gsum.py`, block 20), the MUL
            # reading it at 169.  The load goes just after the group's seq,
            # in creation order.
            tg.seq = _e + 0.5 + 0.001 * i
        tg.append(i)
        ties.append(tg)
    return create


def _mask_bits(swizzle):
    mask = 0
    for c in swizzle:
        mask |= 1 << "xyzw".index(c)
    return mask


def _components_read(lines, i, v, live=0xF):
    """The components of `v` the lines after `i` read while the load's
    value is still in them -- a write kills only the components it writes
    (a constructor that takes the load as its lane x writes the other lanes
    of the same register: `pu_f.vert`, notes/104 §8); None when a reading
    line cannot be parsed."""
    used = 0
    for l2 in lines[i + 1:]:
        if v not in l2:
            continue
        p = _sched.parse(l2)
        if p is None:
            return None
        _mn, (dst, _dm), srcs = p
        for nm, sm in srcs:
            if nm == v:
                used |= sm & live
        if dst == v:
            live &= ~(_dm if _dm else 0xF)
            if not live:
                break
    return used


def _narrow_loads(lines, ldc_vreg, dead=None):
    """Each static block load writes only the components read of it.

    MEASURED (notes/76).  The LDC's destination mask is the set of
    components its block reads, and its width suffix is the smallest that
    reaches the highest of them: none for `.x`, `X2` for `.y`, `X4` for `.z`
    and `.w` (there is no `X3`; a vec3 member prints `LDC.F32X4 R.xyz`).
    `pl_e.vert` prints `LDC.F32 R0.x`, `LDC.F32X2 R0.y`, `LDC.F32X4 R0.z`,
    `LDC.F32X4 R0.w` for the four element blocks of `gl_Position = v`.
    `pl_c.vert` (`a * v.z`) prints `LDC.F32X4 R0.z`.  A load whose reads
    cannot all be parsed keeps its type's mask."""
    for i, l in enumerate(lines):
        m = _ldc_line(l)
        if m is None or m[2] not in ldc_vreg:
            continue
        v = m[2]
        tmask = _mask_bits((m[3] or ".xyzw")[1:])
        used = _components_read(lines, i, v, tmask)
        if used is None:
            continue
        used &= tmask
        if not used:
            if dead is not None:
                dead.add(i)
            continue
        top = max(k for k in range(4) if used & (1 << k))
        sfx = "" if top == 0 else ("X2" if top == 1 else "X4")
        ms = "" if used == 0xf else "." + "".join(
            "xyzw"[k] for k in range(4) if used & (1 << k))
        lines[i] = "%s%s %s%s, %s" % (m[0], sfx, v, ms, m[4])


def _flush_order(flushed):
    """The record-order key of each lowering vreg a flush renamed: the
    number of the value it was made as (py/ifg.py `order_records`)."""
    out = {}
    for nm, lw in (flushed or {}).items():
        try:
            out[int(lw[1:])] = int(nm[1:])
        except (TypeError, ValueError):
            pass
    return out


def _creation_order(lines, flushed, ldc_create=None):
    """The record-order key of every placeholder (notes/107): the line, in
    CREATION order, that first writes it -- else the first that names it.

    `f_7100036a70`'s second walk numbers a block's temps POST-ORDER, each
    node after its operands (gdb on `f_7100036cc0`, `map_3587d848`: the SLT
    228, then its TRUNC 229).  Creation order is that order; the placeholder
    numbers are not where a value's number is taken before its operands'
    (the TRUNC is the value's, taken before the SLT's).  `G2S_NUMKEY=1`
    keeps the numbers, with a flush's rename at its value's
    (`_flush_order`)."""
    if ENV.get("G2S_NUMKEY"):
        return _flush_order(flushed)
    first_def, first_seen = {}, {}
    for i, line in enumerate(lines):
        it = _sched.parse(line)
        if it is not None:
            _dn = (it[1][0] or "").strip()
            if _lex.is_numbered(_dn, "#"):
                first_def.setdefault(int(_dn[1:]),
                                     (ldc_create or {}).get(i, i))
        for _d in _placeholder_numbers(line):
            first_seen.setdefault(int(_d), i)
    out = dict(first_seen)
    out.update(first_def)
    return out


# The opcode a printed mnemonic's node carries, where the namer's table
# (opchain.MNEMONIC) has none or several: the converter's MOV lines are
# 0x47 (or the index carrier 0x4a, which every predicate here answers the
# same), a rounding op is named by its mode (0x6c / 0x6d, notes/23), and an
# LDC is the load node 0x3b (`bc_sbo.vert`'s DAG: the one unnamed op), and so
# is an LDB (`if_ssbo.vert`'s DAG: its one load is 0x3b).
_REPLICATE_OPCODES = {"MOV": (0x47, 0x4a), "TRUNC": (0x6c, 0x6d),
                      "ROUND": (0x6c, 0x6d), "LDC": (0x3b,), "LDB": (0x3b,)}


# Lane-wise readers beyond the scheduler's set: a BFI reads its insert and
# base at its own lanes (`bf_b.frag`: `BFI.S R6.x, {4, 12, 0, 0}, R0, ..`
# reads the MOV's `.x`), and a derivative its operand (`ds_a`..`ds_c`:
# `DDX.F32 R3.xy, R2.x;` over a MUL of `R1.z` by `{2, 0, 0, 0}.x`, and its
# own result read at one lane after: `MOV.F R5.xy, |R4.x|;`).  A BFE the
# same as a BFI (`ld_rgba8_snorm.frag`: `BFE.S R3.x, {8, 0, 0, 0}, R1;`
# reads the lane-x MOV bare).
_LANEWISE_TOO = frozenset(("BFI", "DDX", "DDY", "BFE"))


def _line_opcodes(base):
    got = _REPLICATE_OPCODES.get(base)
    if got is not None:
        return got
    return tuple(o for o, b in _opchain.MNEMONIC.items() if b == base)


def _slot_selectors(line, dmask):
    """[(selector, byte mask)] of a lane-wise line's slots: each operand's
    swizzle over the line's write mask."""
    m = _lex.split_line(line)
    out = []
    for s in _sched._top_level_operands(m[2]):
        t = s.strip().lstrip("-|").rstrip("|").strip()
        sw = _sched._trailing_swizzle(t) if "." in t else ""
        if not (sw and len(sw) <= 4 and _sched._is_swizzle(sw)):
            sw = ""
        out.append((_rep.selector(sw), _rep.byte_mask(dmask)))
    return out


def _replicate_reads(lines, spans, store_movs=frozenset()):
    """f_7100069f90 (glasmlib/replicate.py): a lane-wise read of a value
    that holds one number in every lane reads it at one lane.

    The slot's node is the placeholder's one defining line, in the reader's
    block and before it; a placeholder written by more than one line is
    read through its merge (op 0x57, which no predicate accepts), and one
    from another block through a name read (0x2b, likewise).  A reader that
    is not lane-wise, or a defining node the transcription does not cover,
    is refused when the rule would otherwise apply.  `G2S_NOREPLICATE=1`
    turns the pass off."""
    if ENV.get("G2S_NOREPLICATE"):
        return lines
    block = {}
    for b, sp in enumerate(spans):
        for i in sp:
            block[i] = b
    parsed = [_sched.parse(l) for l in lines]
    writers = {}
    for i, p in enumerate(parsed):
        if p is not None and _lex.is_numbered(p[1][0], "#"):
            writers.setdefault(p[1][0], []).append(i)
    out = list(lines)
    for i, p in enumerate(parsed):
        if p is None or i in store_movs:
            continue                    # a store's MOV is not a node yet
        m = _lex.split_line_at(lines[i])
        rbase = p[0].split(".")[0]
        lanewise = ((rbase in _sched._LANEWISE or rbase in _LANEWISE_TOO)
                    and "(" not in m[1])
        srcs = _sched._top_level_operands(m[2])
        changed = False
        for k, s in enumerate(srcs):
            po = _rep.parse_operand(s)
            if po is None:
                continue
            name, swz = po
            if lanewise:
                sel, mask = _rep.selector(swz), _rep.byte_mask(p[1][1])
                if _rep.one_component(sel, mask):
                    continue
            elif len(set(swz)) == 1:
                continue                # one component over any mask
            ws = writers.get(name, ())
            if (len(ws) != 1 or ws[0] >= i
                    or block.get(ws[0]) != block.get(i)):
                continue
            # D as the pass left it: its own slots were visited first
            # (the walk is post-order, f_71000539c0 via f_7100053d80)
            dline = out[ws[0]]
            d = _sched.parse(dline)
            dm = _lex.split_line(dline)
            dbase = d[0].split(".")[0]
            ops = _line_opcodes(dbase)
            if not ops:
                raise NotEstablished(
                    "a read of a %s node: its opcode, which f_7100069f90's "
                    "predicates take, is not known" % dbase)
            try:
                if "(" in dm[1] or ".CC" in d[0]:
                    raise _rep.Unmeasured("a predicated or .CC node")
                slots = _slot_selectors(dline, d[1][1])
                answers = set(_rep.replicated(o, slots) for o in ops)
            except _rep.Unmeasured as exc:
                raise NotEstablished("f_7100069f90: %s" % exc)
            if len(answers) != 1:
                raise NotEstablished(
                    "f_7100069f90: the %s node's opcodes answer differently"
                    % dbase)
            if not answers.pop():
                continue
            if not lanewise:
                raise NotEstablished(
                    "f_7100069f90 on a %s reader: its slot masks are not "
                    "measured" % rbase)
            new = _rep.rewrite_operand(s, sel, mask)
            if new is None:
                break                   # 0x6a0a8: the pass stops here
            lead = s[:len(s) - len(s.lstrip())]
            srcs[k] = lead + new
            changed = True
        if changed:
            out[i] = lines[i][:m[3]] + ",".join(srcs) + ";"
    return out


def _spans_of(span_sizes, nlines):
    """The line indices of each block, the last taking any remainder."""
    spans, k = [], 0
    for n_ in span_sizes:
        spans.append(list(range(k, k + n_)))
        k += n_
    if k < nlines:
        spans[-1].extend(range(k, nlines))
    return spans


def _render(line, assign):
    return _sub_vregs(line, lambda v: "R%d" % assign[v])


_OUTPUT_BUILTIN_TEXT = {0: "result.position", 1: "result.pointsize",
                        22: "result.depth"}


def _output_records(module, model):
    """THE OUTPUTS ARE RECORDS TOO (`g2s_trace_liveset`): each output
    variable is a vreg, numbered ahead of every temp in the module's order
    of declaration -- `bl_289.vert`'s nine outputs are vregs 1..9 in
    OpVariable order (`vs_BINORMAL0`, attrib[3], first; `gl_PerVertex` 5th),
    `monster_a608a03b`'s colours 2..6 (the `$kill` pseudo-output 1).  Their
    printed names -> a key in that order, for the live array
    (py/ifg.py `_sweep`).  A name this does not know is left out."""
    from spvnames import StorageClass, Decoration, ExecutionModel
    out = {}
    k = 0
    frag = model == ExecutionModel.Fragment
    for vid, ins in module.globals.items():
        if ins.operands[2] != StorageClass.Output:
            continue
        k += 1
        loc = module.decoration(vid, Decoration.Location)
        if loc is not None:
            _n = _pointee_array_length(module, vid)
            for i in range(_n):
                nm = ("result_color%d" % (loc[0] + i) if frag
                      else "result.attrib[%d]" % (loc[0] + i))
                out.setdefault(nm, k + i / 100.0)
            continue
        bi = module.decoration(vid, Decoration.BuiltIn)
        if bi is not None and bi[0] in _OUTPUT_BUILTIN_TEXT:
            out.setdefault(_OUTPUT_BUILTIN_TEXT[bi[0]], float(k))
            continue
        for m, b in module.member_builtins(vid):
            if b in _OUTPUT_BUILTIN_TEXT:
                out.setdefault(_OUTPUT_BUILTIN_TEXT[b], k + m / 100.0)
    return out


def _pointee_array_length(module, vid):
    """The element count of an output array (1 for anything else)."""
    from glasmlib.types import _pointee
    from spvnames import Op
    from glasmlib.operands import _scalar_value
    tid = _pointee(module, vid)
    t = module.types.get(tid) if tid is not None else None
    if t is not None and t.opcode == Op.OpTypeArray:
        n = _scalar_value(module, t.operands[2])
        if n:
            return int(n)
    return 1


def _pass1_alloc_pass2(lines, cuts, ties, passthru, span_sizes, band,
                       names=frozenset(), calls=(), flushed=None,
                       vkey=None, carriers=frozenset(), outputs=None,
                       ldc_create=None):
    """The compiler's own sequence (notes/57): PASS 1 schedules each block,
    the allocator colours sweeping pass 1's lists, and PASS 2 builds its
    edges on the ALLOCATED registers and prints.  Returns (rendered, regs)
    in the final order, or None when any stage declines."""
    _band = flushed
    if vkey is not None:
        _band = ("vkey", vkey)
    perm1 = _sched.order_pre(lines, cuts=cuts, ties=ties, passthru=passthru,
                             stage1=True, names=names, calls=calls,
                             band=_band)
    if perm1 is None or sorted(perm1) != list(range(len(lines))):
        return None
    lines1 = [lines[i] for i in perm1]
    items1 = [_sched.parse(l) for l in lines1]
    spans = _spans_of(span_sizes, len(lines1))
    _long = _colour_long(lines1, spans)
    if _long is None:
        return None
    lines1, dregs = _long
    _cc = _colour_conditions(lines1, spans)
    if _cc is None:
        return None
    walks = ENV.get("G2S_WALKS")
    ra = _ifg.allocate(lines1, items1, spans, band,
                       _creation_order(lines, flushed, ldc_create),
                       frozenset(carriers) if walks else None,
                       outputs=outputs,
                       order=(None if ENV.get("G2S_NOMCMADE") else perm1))
    if ra is None:
        return None
    assign, regs = ra
    alloc = [None] * len(lines)
    for pos, i in enumerate(perm1):
        alloc[i] = _render_conditions(_render(lines1[pos], assign), _cc)
    perm = _sched.order_pre(lines, allocated=alloc, cuts=cuts, ties=ties,
                            passthru=passthru, list_edges=True, names=names,
                            calls=calls, band=_band)
    if perm is None:
        return None
    if ENV.get("G2S_VLINES"):
        _dump_vlines(lines, lines1, items1, spans, band, flushed, carriers,
                     assign, perm, ldc_create, perm1)
    return [alloc[i] for i in perm], regs, dregs


def _colour_long(lines, spans):
    """The LONG handles coloured by the compiler's allocator, class 4
    (py/ifg.py `allocate_long`, notes/104), over the same order the R
    class is coloured on.  `(lines renamed, D count)`, or None when that
    allocator declines."""
    if ENV.get("G2S_NOLONGALLOC"):
        return list(lines), len(set(_long_tokens("\n".join(lines))))
    return _ifg.allocate_long(lines, spans)


def _dump_vlines(lines, lines1, items1, spans, band, flushed, carriers,
                 assign, perm, ldc_create=None, perm1=None):
    """Diagnosis only: the final order with each line's placeholders and
    record indices, to join against `tools/nodedump.py`'s `vr=`."""
    _idx = _ifg.order_records(
        set(int(m) for l in lines for m in _placeholder_numbers(l)),
        band, _creation_order(lines, flushed, ldc_create),
        _ifg.walk_keys(lines1, items1, spans, frozenset(carriers))
        if ENV.get("G2S_WALKS") else None,
        None if ENV.get("G2S_NOMERGEFIRST")
        else _ifg.merge_chains(items1, spans, band,
                               None if ENV.get("G2S_NOMCMADE") else perm1))
    sys.stderr.write("REGS %s\n" % sorted(
        (_idx[v], v, assign[v]) for v in assign))
    for i in perm:
        sys.stderr.write("VLINE %s\n" % lines[i])


class Finish(object):
    """`_finish()`: the lines the arms made, as the body the listing
    prints."""

    def _finish(self):
        if not self.lines or not self.lines[-1].startswith("RET"):
            raise NotEstablished("a body that does not end in OpReturn")
        self.names = (frozenset(self.local_reg.values())
                      | frozenset(self.callnames))
        if self.dead_lines:
            # superseded lines (memory.py `_node_again`) go first, so the
            # load only they read is found dead below
            self._drop_lines(set(self.dead_lines))
        _dead = set()
        _narrow_loads(self.lines, self.ldc_vreg, _dead)
        if _dead and not ENV.get("G2S_KEEPDEADLDC"):
            self._drop_lines(_dead)
        if not ENV.get("G2S_NOLDCSEQ"):
            self.ldc_create = _ldc_at_reader(self.lines, self.ldc_vreg,
                                             self.cuts, self.ties,
                                             self.con_stmt,
                                             self.con_lane_load)
        if self.callees:
            self._label_subroutines()
        self.lines = _number_conditions(self.lines)
        span_sizes = _sched.span_sizes(self.lines, self.cuts, self.names,
                                       self.passthru, self.calls)
        if span_sizes is not None:
            self.lines = _replicate_reads(
                self.lines, _spans_of(span_sizes, len(self.lines)),
                frozenset(self.store_movs))
        if ENV.get("G2S_DUMP"):
            self._dump(span_sizes)
        rendered, regs, dregs = self._schedule_and_allocate(span_sizes)
        if ENV.get("G2S_SCHED"):
            # The old single-pass model over the rendered lines, OFF BY
            # DEFAULT: its first key, `node[36]`, is the CREATION order of the
            # compiler's nodes, which the rendered lines no longer carry.
            perm = _sched.order(rendered)
            if perm is not None:
                rendered = [rendered[i] for i in perm]
        # the read-mask annotations (`#77@1`, sched.py `_split`) are not
        # printed
        rendered = [_drop_release_tags(_drop_hidden_dest(
            _strip_read_masks(l))) for l in rendered]
        return _Body(rendered, regs, self._wants(), dregs,
                     [n for _v, n in _lmem_arrays(self.module)])

    def _wants(self):
        """The registers the TEMP block declares beyond the R ones."""
        wants = []
        if self.wants_h:
            wants.append("H0")
            # more than one SHORT register (`ld_rgba16f`: `SHORT TEMP H0,
            # H1;`, image.py `_unpack_pairs`)
            for _k in range(1, getattr(self, "hregs", 1)):
                wants.append("H%d" % _k)
        if self.handles:
            wants.append("D0")
        if self.wants_cc:
            wants.append("CC")
        return wants

    def _drop_lines(self, dead):
        """A LOAD NOTHING IN ITS BLOCK READS IS NOT THERE (notes/81): the
        reader substitutes a load into the statements that use it, so a load
        used only by later blocks makes a leaf there (`_reload`) and nothing
        where the `OpLoad` stood.  `chr_cloth_1c6be086`'s `vs_TEXCOORD0.xy =
        uvScroll0_g.xy` loads the vec2 before the store opens its block, and
        the compiler prints no LDC there.

        Every line index the lowering recorded moves with its line; a
        removed line's index moves to the next line kept."""
        lines = self.lines
        _keep = [k for k in range(len(lines)) if k not in dead]
        _new = dict((_k, _n) for _n, _k in enumerate(_keep))

        def _at(k):
            while k not in _new and k < len(lines):
                k += 1
            return _new.get(k, len(_keep))
        self.lines = [lines[k] for k in _keep]
        self.cuts[:] = sorted(set(_at(k) for k in self.cuts))
        for _t in self.ties:
            _sq = getattr(_t, "seq", None)
            _t[:] = [_new[k] for k in _t if k in _new]
            if _sq is not None:
                _t.seq = _at(int(_sq)) + (_sq - int(_sq))
        self.passthru[:] = [_new[k] for k in self.passthru if k in _new]
        self.calls[:] = [_new[k] for k in self.calls if k in _new]
        self.store_movs[:] = [_new[k] for k in self.store_movs if k in _new]
        for _nm in list(self.stmtpos):
            self.stmtpos[_nm] = _at(self.stmtpos[_nm])

    def _label_subroutines(self):
        """A SUBROUTINE'S LABEL IS ITS FIRST BLOCK'S NUMBER (notes/68): the
        blocks are numbered as the walker opens them, the entry function's
        first, and the return statement at the end of the entry function
        opens one more (0xf148fc) that takes the driver's RET.  So the label
        is the number of blocks before it, a RET sharing the block it ends."""
        lines = self.lines
        _sz = _sched.span_sizes(lines, self.cuts, self.names, self.passthru,
                                self.calls)
        if _sz is None:
            raise NotEstablished("a program with calls whose blocks are not "
                                 "computed")
        _lab = {}
        _nb = 0
        _s = 0
        for _n in _sz:
            _l = lines[_s]
            if _l.startswith("BB@"):
                _lab[_l[3:-1]] = _nb
                _nb += 1
            elif _n == 1 and _l.startswith("RET"):
                if _s > 0 and lines[_s - 1].startswith("RET"):
                    _nb += 1           # the block the return opened
            else:
                _nb += 1
            _s += _n
        self.lines = [_sub_labels(l, lambda d: "BB%d" % _lab[d])
                      for l in lines]

    def _dump(self, span_sizes):
        """Diagnosis only: the converter's lines before scheduling, the
        blocks, the ties and pass 1's order."""
        for _i, _l in enumerate(self.lines):
            sys.stderr.write("%3d %s%s\n" % (
                _i, _l, "   band" if any(
                    int(_m) in self.band
                    for _m in _placeholder_numbers(_l)[:1]) else ""))
        sys.stderr.write("spans %s ties %s cuts %s\n"
                         % (span_sizes, self.ties, self.cuts))
        sys.stderr.write("pass1 %s\n" % _sched.order_pre(
            self.lines, cuts=self.cuts, ties=self.ties,
            passthru=self.passthru, stage1=True, names=self.names,
            calls=self.calls,
            band=("vkey", _vkeys(self.lines, self.stmtpos))))
        sys.stderr.write("vkey %s\n" % sorted(
            _vkeys(self.lines, self.stmtpos).items(), key=lambda kv: kv[1]))

    def _schedule_and_allocate(self, span_sizes):
        """(rendered lines, register count).

        THE COMPILER'S SEQUENCE (notes/57): pass 1, then the allocator over
        pass 1's lists, then pass 2 with its edges on the allocated
        registers.  `G2S_NOALLOC1=1` falls back to scheduling both passes
        first and allocating after."""
        if (not ENV.get("G2S_NOALLOC1") and self.use_regalloc
                and span_sizes is not None
                and not ENV.get("G2S_NOSCHED2")):
            done = _pass1_alloc_pass2(
                self.lines, self.cuts, self.ties, self.passthru, span_sizes,
                self.band, self.names, self.calls, self.flushed,
                _vkeys(self.lines, self.stmtpos), self.carriers,
                None if ENV.get("G2S_NOOUTREC")
                else _output_records(self.module, self.model),
                getattr(self, "ldc_create", None))
            if done is not None:
                return done
        lines = self._schedule_both_passes()
        return self._allocate_scheduled(lines, span_sizes)

    def _schedule_both_passes(self):
        """The fallback order: both passes, before the allocator."""
        lines = self.lines
        if ENV.get("G2S_NOSCHED2"):
            return lines
        _perm = _sched.order_pre(lines, cuts=self.cuts, ties=self.ties,
                                 passthru=self.passthru, names=self.names,
                                 calls=self.calls)
        if _perm is not None:
            return [lines[i] for i in _perm]
        if not ENV.get("G2S_UNSCHEDULED"):
            # a body the scheduler's model cannot place is REFUSED: the
            # creation order it would otherwise print is not the compiler's
            # (`mc_n14.vert` printed its first block unscheduled before its
            # pass-1 cycle was found)
            raise NotEstablished(
                "the scheduler's model does not place this body (pass 1 or "
                "pass 2 leaves nodes unpicked)")
        return lines

    def _allocate_scheduled(self, lines, span_sizes):
        """notes/54-55: the compiler's own colouring (py/regalloc.py, checked
        against it attempt by attempt) on a graph built by the sweep of
        `f_7100043460` from these lines (py/ifg.py), seeded by the block
        live-out rule read from `f_710006e7c0`.  A body it declines falls
        back to the component allocator -- unless it has a materialised
        local, which that one cannot colour."""
        _spans = (_spans_of(span_sizes, len(lines))
                  if span_sizes is not None
                  else [list(range(len(lines)))])
        _long = _colour_long(lines, _spans)
        if _long is None:
            raise NotEstablished(
                "the LONG handles' colouring leaves the transcribed path")
        lines, dregs = _long
        _cc = _colour_conditions(lines, _spans)
        if _cc is None:
            raise NotEstablished(
                "the condition registers' colouring leaves the transcribed "
                "path")
        rendered, regs, dregs = self._allocate_registers(lines, span_sizes,
                                                         dregs)
        return ([_render_conditions(l, _cc) for l in rendered], regs, dregs)

    def _allocate_registers(self, lines, span_sizes, dregs):
        """The R class, for `_allocate_scheduled`: the transcribed allocator,
        else the component one."""
        _ra = None
        if self.use_regalloc and span_sizes is not None:
            _items = [_sched.parse(l) for l in lines]
            _ra = _ifg.allocate(lines, _items,
                                _spans_of(span_sizes, len(lines)), self.band,
                                _creation_order(self.lines, self.flushed,
                                                getattr(self, "ldc_create",
                                                        None)))
        if _ra is None and self.local_reg:
            raise NotEstablished(
                "a whole load of a local assembled from parts, in a body the "
                "transcribed allocator declines: the component allocator "
                "gives the local the wrong register (notes/55 §7)")
        if _ra is not None:
            _assign, regs = _ra
            return [_render(l, _assign) for l in lines], regs, dregs
        if not ENV.get("G2S_NOBAND"):
            rendered, regs = _allocate_components(lines, self.band, self.wide)
            if rendered is None:
                raise NotEstablished("the component allocator did not "
                                     "converge")
            return rendered, regs, dregs
        rendered, regs = _allocate(lines, self.band)
        return rendered, regs, dregs
