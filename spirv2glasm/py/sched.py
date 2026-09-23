"""sched.py -- the emission order, as the compiler computes it.

notes/51 reads GLSLC's two scheduling passes and `tools/ordercheck.py` runs
that reading against the compiler on all 113 probes.  This is the same model,
driven by the CONVERTER's own lines instead of the compiler's dumps, so the
order stops being something the converter avoids by refusing and becomes
something it computes.

The three inputs the model needs, and where each comes from here:

  * THE EDGES.  `f_710004ab80` makes an edge when a use and a live definition
    overlap in COMPONENTS of the same register (notes/51 §8).  The converter
    knows what each line writes and reads, so the same rule applies directly.
  * `node[36]`.  The creation ordinal -- the n-th node made (notes/51).  The
    converter creates its nodes in the order it lowers, so this is just the
    index of the line as it was emitted, which is the one thing a model of
    the compiler's finished graph could never recover.
  * `entry[68]`.  Derived, as pass 1 derives it: the rank of a node in the
    first pass's output, 16 per step.

A line that this cannot place is not reordered: `order()` returns None and the
caller keeps its own order, so wiring this in can refuse but cannot corrupt.
"""

import os as _os
import lex as _lex

CYCLE = 16

# `MOV.F R0.xy, -foo.zw;` -> mnemonic, destination, sources.  The converter
# writes its own lines, so this parses exactly the shapes it produces.
# (lex.py `split_line`)
_COMP = {"x": 0, "y": 1, "z": 2, "w": 3}
# The block terminators, which the emitter places itself.
_TERMINATOR = ("RET", "BRK", "CONT", "ENDIF", "ELSE", "IF", "ENDREP", "REP")


def _split(operand):
    """(name, components) for one operand, components as a 4-bit mask."""
    t = operand.strip().lstrip("-|").rstrip("|")
    t = t.split("(")[0].strip()
    # AN ANNOTATED READ MASK, `#77@1` (glasm.py: a whole read of a local
    # that stores fewer components than the reader takes, notes/87): the
    # name, and the components it really reads.  Never printed.
    if (len(t) >= 4 and t[-2] == "@" and t[-1] in "0123456789abcdef"
            and (_lex.is_numbered(t[:-2], "#")
                 or _lex.is_numbered(t[:-2], "R"))):
        return t[:-2], int(t[-1], 16)
    # The swizzle is a TRAILING run of xyzw after the LAST dot, which is what
    # separates `R0.x` from `fragment.attrib[0]` -- the latter has a dot too
    # and splitting on the first one loses the name.
    base, _, swz = t.rpartition(".")
    if not base:
        base, swz = t, ""
    base = base.strip()
    if not swz or len(swz) > 4 or any(c not in _COMP for c in swz):
        return t.strip(), 0xF
    m = 0
    for c in swz:
        m |= 1 << _COMP[c]
    return base, m


_LANEWISE = frozenset(("MOV", "ADD", "MUL", "MAD", "MIN", "MAX", "SLT", "SGE",
                       "SGT", "SLE", "SEQ", "SNE", "FLR", "FRC", "TRUNC",
                       "ROUND", "CEIL", "AND", "OR", "XOR", "SHL", "SHR",
                       "DIV", "I2F", "F2I", "ABS", "SSG", "CMP", "LRP")
                      # a derivative is lane-wise too: `map_02077bd8`'s
                      # `DDY.F32 R2.x, R10;` reads `R10.x` alone, so the
                      # local's `.yzw` pass-through after it is ready at once
                      # (g2s_sel: `p52=0` from the block's first pick)
                      + (() if _os.environ.get("G2S_DDWHOLE")
                         else ("DDX", "DDY"))
                      # so is BFI, in its insert and base (the slice's
                      # `map_21c4635d`: `BFI.S R6.x, .., R5, R3;` reads `.x`)
                      + (() if _os.environ.get("G2S_BFIWHOLE")
                         else ("BFI",)))



def _inner_pieces(operand):
    r"""`[\[(]([^\])]*)[\])]` findall: the text inside each bracket or
    paren, up to the first closing one of either kind."""
    out = []
    i, n = 0, len(operand)
    while i < n:
        if operand[i] == "[" or operand[i] == "(":
            j = i + 1
            while j < n and operand[j] != "]" and operand[j] != ")":
                j += 1
            if j == n:
                # no closer: the opener matches nothing, try the next char
                i += 1
                continue
            out.append(operand[i + 1:j])
            i = j + 1
        else:
            i += 1
    return out


def _lmem_name(nm):
    r"""`^(lmem\d+)\[.*\]$`: the array name, or None."""
    if not nm.startswith("lmem") or not nm.endswith("]") or "\n" in nm:
        return None
    e = _lex.digit_end(nm, 4)
    if e == 4 or e >= len(nm) or nm[e] != "[" or e == len(nm) - 1:
        return None
    return nm[:e]


def _is_reg(sub):
    r"""`^(#\d+|[RD]\d+)(?:\.[xyzw]{1,4})?$`"""
    head, dot, swz = sub.partition(".")
    if dot and not _lex.is_swizzle(swz):
        return False
    return (_lex.is_numbered(head, "#") or _lex.is_numbered(head, "R")
            or _lex.is_numbered(head, "D"))


def _inner_reads(operand):
    """The registers read INSIDE a bracket or a paren.

    `LDC.F32X4 #4, buf0[#3.x];` reads `#3`, and `TEX.F #1, ..., handle(D0.x)`
    reads `D0` -- neither is the operand's own head token, so `_split` alone
    drops the edge and the scheduler is then free to hoist the load above the
    address arithmetic, or the fetch above the handle it needs.
    """
    out = []
    for piece in _inner_pieces(operand):
        for sub in piece.replace("+", ",").split(","):
            sub = sub.strip()
            if _is_reg(sub):
                out.append(_split(sub))
    return out


_CC_TESTS = ("NE", "EQ", "GT", "GE", "LT", "LE")
# A BARE TEST of the condition code, `IF NE.x;` / `KIL NE1.y;`, with the
# numbering tag `$k` the lowering's condition vregs carry until they are
# coloured (glasmlib/lower/finish.py `_number_conditions`).


def _cc_test(t):
    r"""`^(NE|EQ|GT|GE|LT|LE)(1?)(\$\d+)?(\.[xyzw]+)?$` -> (one, tag,
    swizzle letters or None) or None.  `one` is "1" or ""."""
    if t[:2] not in _CC_TESTS:
        return None
    i, n = 2, len(t)
    one = ""
    if i < n and t[i] == "1":
        one, i = "1", i + 1
    tag = None
    if i < n and t[i] == "$":
        e = _lex.digit_end(t, i + 1)
        if e == i + 1:
            return None
        tag, i = t[i:e], e
    swz = None
    if i < n:
        if t[i] != "." or not _lex.is_swizzle(t[i + 1:], 1, n):
            return None
        swz = t[i + 1:]
    return one, tag, swz


def _release_tag(src):
    r"""`@r([htc]{3})\s*$` searched: the three letters, or None."""
    t = src.rstrip(_lex.WS)
    if len(t) >= 5 and t[-5:-3] == "@r":
        k = t[-3:]
        if all(c in "htc" for c in k):
            return k
    return None


def _predicated(dst):
    r"""`\((NE|..)(1?)(\$\d+)?(\.[xyzw]+)?\)\s*$` searched: the test
    inside a trailing paren, as `_cc_test` gives it, or None."""
    t = dst.rstrip(_lex.WS)
    if not t.endswith(")"):
        return None
    k = t.rfind("(")
    if k < 0:
        return None
    return _cc_test(t[k + 1:-1])
_CC_TESTERS = frozenset(("IF", "KIL"))


def _cc_name(one, tag):
    """THE CONDITION REGISTER a set writes or a test reads (notes/106):
    each `.CC` set is a vreg of its own, allocator class 1, until it is
    coloured (`C$k`); after, the register it was given -- `CC0`, or `CC1`
    for a `.CC1` set and an `NE1` test.  `RC` and `HC` are the dummy
    destinations and name nothing.  `G2S_NOCCVREG=1`: the old reading, one
    name `RC`."""
    if _os.environ.get("G2S_NOCCVREG"):
        return "RC"
    if tag:
        return "C" + tag
    return "CC1" if one else "CC0"
_TEXTURE_OPS = frozenset(("TEX", "TXL", "TXF", "TXB", "TXD", "TXP"))
# The storage-image memory, a register of its own to the edge builder.
_IMAGE_MEMORY_OPS = (() if _os.environ.get("G2S_NOIMEM")
                     else frozenset(("LOADIM", "STOREIM")))
_IMEM = "IMEM"


def _pass1_extra_defs(it):
    """Pass 1's extra writes: a storage image's handle loaded AT ITS OpLoad
    (the `@w` tag, glasmlib/lower/image.py `_image_handle`) is a statement
    of its own, and the image op after it waits on it -- `0110_si_e.comp`'s pass
    1 lists that handle's load (t68 16) between the next op's own handle
    load (0) and the LOADIM (32), which is the LOADIM releasing it; as a
    plain operand of the STORE far below it would be listed last."""
    if (it is not None and it[0] == "LDC.U64" and it[2]
            and it[2][0][0].endswith("@w")):
        return ((_IMEM, 1),)
    return ()


def _extra_defs(it):
    """What a line writes besides its destination: the image memory, for
    an image load or store."""
    if it is not None and it[0].split(".")[0] in _IMAGE_MEMORY_OPS:
        return ((_IMEM, 1),)
    return ()
# The dot products that read fewer than four components.
_DOT_WIDTH = {"DP3": 3, "DP2": 2}


def _top_level_operands(rest):
    """The comma-separated operands, commas inside brackets kept."""
    srcs = []
    depth = 0
    cur = ""
    for ch in rest:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            srcs.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        srcs.append(cur)
    return srcs


def _trailing_swizzle(operand):
    """The text after the operand's last dot (its swizzle, if it has one)."""
    return operand.strip().split("(")[0].rpartition(".")[2]


def _is_swizzle(text):
    return all(c in _COMP for c in text)


def _mask_of_letters(letters):
    mk = 0
    for c in letters:
        mk |= 1 << _COMP[c]
    return mk


def _texture_2d_mask(s, mk):
    """A 2D SAMPLE READS TWO COMPONENTS of its coordinate (notes/87): lanes x
    and y, whatever the operand's width.  Reading all four made a vec2
    coordinate's `.zw` live from the program's entry -- never written -- and
    it met every temp in the graph (`0000_mq_n22e.frag`, the corpus's
    `map_010c7104`)."""
    return _texture_2d_lanes(s, mk, (0, 1))


def _texture_2d_lanes(s, mk, lanes):
    """The coordinate lanes a 2D texture instruction reads, `lanes` of its
    four (the swizzle's letters at those places when it has four)."""
    _sw = _trailing_swizzle(s)
    if "." in s and len(_sw) == 4 and _is_swizzle(_sw):
        return _mask_of_letters("".join(_sw[k] for k in lanes))
    if mk == 0xF:
        return sum(1 << k for k in lanes)
    return mk


# A 2D TXL reads its LOD from the coordinate's `.w` as well (notes/106):
# `map_2a0f6956`'s coordinate record (vr 174, written .x .y .w) has no
# neighbour in lane z, where reading all four lanes made `.z` live from the
# block's entry and meet every temp.
_TXL_2D_LANES = (0, 1, 3)


def _lane_mask(base, dmask, s, mk):
    """The components a lane-wise source reads against its write mask.

    AN UNSWIZZLED SOURCE IS READ THROUGH THE WRITE MASK.  `MOV.F R1.yzw, R1;`
    moves y to y, z to z and w to w -- it does not read `.x` -- and that is
    what keeps it independent of the `MOV.F R1.x, ...` beside it, which is
    what `0052_lo_parts.vert`'s pair needs (their `node[36]` are 2 and 4 and the
    self-copy prints first, which only happens when pass 1 sees no edge
    between them).  The MOV family always; a dot product writes one
    component and reads all of them, and so do the texture instructions.

    The other lane-wise ops too, on a narrower write: `MUL.F32 #80.xyz, #75,
    #27;` does not read `#27.w` (`0080_mc_n14.vert`, where the stray `.w` read
    made pass 1's implicit-read owner wait on itself).

    A FOUR-LETTER SWIZZLE ON A NARROWER WRITE reads only the lanes the write
    keeps: `MUL.F32 R1.xyz, R11.zxyw, R10.yzxw;` (the printer fills the
    unused lane with its own component, notes/78 §1) reads `R11.zxy`, not
    `.w`.  Component-wise ops only."""
    if base == "MOV" and mk == 0xF and dmask:
        mk = dmask
    narrower = dmask and dmask != 0xF and base in _LANEWISE
    if narrower and mk == 0xF and not _os.environ.get("G2S_NOLANEREAD"):
        mk = dmask
    if narrower:
        _sw = _trailing_swizzle(s)
        if len(_sw) == 4 and _is_swizzle(_sw):
            mk = _mask_of_letters(_sw[lane] for lane in range(4)
                                  if dmask & (1 << lane))
    return mk


def _dot_mask(width, s):
    """A DP3 READS THREE COMPONENTS, a DP2 two: the rest of an operand is not
    live through it.  `0077_mb_n18.vert`'s `u_xlat4`, stored whole and then read
    only by `DP3.F32 .., vertex.attrib[1], #48;`, conflicts with the later
    temps in `.xyz` alone in the compiler's graph (`g2s_ifg`,
    `tools/ifgjoin.py`)."""
    _sw = _trailing_swizzle(s)
    if _sw and len(_sw) <= 4 and _is_swizzle(_sw) and "." in s:
        _sw = (_sw + _sw[-1] * 4)[:width]
    else:
        _sw = "xyzw"[:width]
    return _mask_of_letters(_sw)


def parse(line):
    """(mnemonic, (dest, mask), [(src, mask)...]) or None if not an operation.

    LOCAL MEMORY IS ONE NAME (notes/84): every element store into `lmem0[i]`
    writes `lmem0` whole and every indexed load reads it whole.  That is the
    compiler's graph for `0071_lm_icb.frag` -- the four element stores chained by
    kind-0 edges in creation order, and the load after the LAST one only
    (each store kills the one before).  AN ELEMENT STORE ALSO READS THE
    ARRAY: the elements it does not write pass through it.  The compiler's
    pass 1 for `0071_lm_icb.frag` lists the stores 0, 1, 2, 3 first (`entry[68]`
    0..48) and the element temps last, which is what the release gives only
    when each store waits on the next one as a reader waits on its
    producer."""
    m = _lex.split_line(line)
    if m is None:
        # AN OPERAND-LESS INSTRUCTION (`MEMBAR.CTA;`, `BAR ;`) is still an
        # item: it writes nothing and reads nothing, so it carries no edge
        # and the scheduler places it by its `seq` alone.  That is what the
        # listing shows -- `particle_fog_sort.comp` puts an unrelated
        # `SHR.U` BETWEEN `BAR ;` and `MEMBAR.CTA;`, so a barrier is not a
        # scheduling fence.  Without this the line does not parse and the
        # whole body is refused.  (notes/119 §2)
        _b = line.strip()
        if _b.endswith(";"):
            _b = _b[:-1].strip()
            if _b and all(c.isalnum() or c in "._" for c in _b):
                return _b, _split(""), []
        return None
    mnem, dst, rest = m
    base = mnem.split(".")[0]
    _ccv = not _os.environ.get("G2S_NOCCVREG")
    _t = _cc_test(dst.strip()) if _ccv and base in _CC_TESTERS else None
    if _t:
        # `IF NE.x;`: a READ of the condition register, not a write of `NE`
        return mnem, _split(""), [(_cc_name(_t[0], _t[1]),
                                   _mask_of_letters(_t[2])
                                   if _t[2] else 0xF)]
    srcs = _top_level_operands(rest)
    if base in _TEXTURE_OPS:
        # THE HANDLE IS RELEASED FIRST (notes/61): in `0033_fr_texlod.frag` the
        # handle load sits AFTER the coordinate's construct in pass 1's list,
        # which is the reverse of the picks -- so the image node hands its
        # handle to the release before its coordinate.
        srcs = ([x for x in srcs if "handle(" in x]
                + [x for x in srcs if "handle(" not in x])
    _rel = _release_tag(srcs[-1]) if srcs else None
    if base == "STOREIM" and len(srcs) == 4 and _rel:
        # THE RELEASE ORDER the arm worked out (glasmlib/lower/image.py
        # `_arm_image_write`): h the handle, c the coordinate, t the texel
        _pos = {"h": 0, "t": 1, "c": 2}
        srcs = [srcs[_pos[k]] for k in _rel] + [srcs[3]]
    elif (base == "STOREIM" and len(srcs) == 4
            and not _os.environ.get("G2S_STOREIMPRINTORDER")):
        # AN IMAGE STORE'S NODE takes its operands HANDLE, COORDINATE, TEXEL
        # (`g2s_trace_fold` on `st6.frag`: opnd0 the handle load, opnd1 the
        # TRUNC, opnd2 the attribute), the printer puts the texel second
        # (notes/111).  The release takes the COORDINATE, then the HANDLE,
        # then the TEXEL -- pass 1's list is the reverse of the release:
        # `st6.frag` (`tools/gsum.py`) lists the handle load at t68 0 and
        # the coordinate's TRUNC at 16, `0111_hl_b.comp` the texel's LDB at 0 and
        # the handle load at 16.
        srcs = [srcs[2], srcs[0], srcs[1], srcs[3]]
    tex2d = (base == "TEX" and srcs and srcs[-1].strip() == "2D"
             and not _os.environ.get("G2S_TEXWHOLE"))
    # A CUBE SAMPLE READS THREE COMPONENTS of its coordinate, for the same
    # reason a 2D one reads two (notes/87): reading all four leaves the
    # coordinate's `.w` live from the program's entry, never written, and it
    # then meets EVERY temp in the graph.  `tools/degcmp.py` on
    # `water_11af45ac.frag`: the coordinate of `TEX.F #2208, #2205,
    # handle(D35.x), CUBE;` has degree 2206 for us and 31 for the compiler,
    # and every other record's degree is exactly one higher than the
    # compiler's because of it.  `G2S_CUBEWHOLE=1` reads all four.
    texcube = (base == "TEX" and srcs and srcs[-1].strip() == "CUBE"
               and not _os.environ.get("G2S_CUBEWHOLE"))
    txl2d = (base == "TXL" and srcs and srcs[-1].strip() == "2D"
             and not _os.environ.get("G2S_TXLWHOLE"))
    if base == "STB":
        # A STORAGE-BUFFER STORE writes the buffer and READS its value (the
        # first operand) at the store's width -- `STB.F32X4 R0, sbo_buf1[16];`
        # reads R0 whole, `STB.U32 R0, ..` its `.x` -- and the address's
        # carrier (`st_a`..`0109_st_g.comp`)
        _vm = (0x3 if mnem.endswith("X2") else 0xF if mnem.endswith("X4")
               else 0x1)
        _ops = _top_level_operands(dst + ("," + rest if rest else ""))
        _val, _addr = _ops[0], (_ops[-1] if len(_ops) > 1 else "")
        _bn = _addr.split("[")[0].strip()
        vn, _vk = _split(_val)
        _o = [] if _val.strip().startswith("{") else [(vn, _vm)]
        # ... AND THE BUFFER ITSELF, after its value and address: the stores
        # into one buffer are a chain (`tools/gsum.py 0111_cb_a.comp`: each STB
        # has a kind-0 edge to the next), and pass 1 lists the five stores
        # in creation order, each after its gather -- the release of a store
        # hands on its value and THEN the store before it (notes/111).
        # `G2S_STBNOCHAIN=1` drops the read.
        _mem = ([] if _os.environ.get("G2S_STBNOCHAIN")
                else [(_bn, 0xF)])
        # THE ADDRESS IS HANDED TO THE RELEASE BEFORE THE VALUE: `0111_cb_b.comp`
        # (`buf.d[id.x] = id.y`) lists MUL, the value's gather, then the
        # carrier -- the release took the carrier first (notes/111)
        if _os.environ.get("G2S_STBVALUEFIRST"):
            return mnem, (_bn, 0xF), _o + _inner_reads(_addr) + _mem
        return mnem, (_bn, 0xF), _inner_reads(_addr) + _o + _mem
    d = _split(dst)
    _msuf = mnem.split(".")[1:]
    if _ccv and ("CC" in _msuf or "CC1" in _msuf):
        # A `.CC` SET writes the condition register, at its mask; `RC`/`HC`
        # are its dummy destination (a numbered one, `RC$3`, names the vreg)
        _dn = d[0]
        _dm = (_dn[2:] if _dn[:2] in ("RC", "HC") and len(_dn) > 3
               and _dn[2] == "$" and _lex.is_digits(_dn[3:]) else None)
        d = (_cc_name("CC1" in _msuf, _dm), d[1])
    out = []
    _selfread = None
    _pm = _predicated(dst)
    if _pm:
        # A PREDICATED WRITE (`MOV.U R2.xy(NE), ..;`, a vector select) reads
        # the condition code the `.CC` move before it set -- at the write's
        # lanes, or the one component a swizzled test names (`(NE.y)`) --
        # and keeps the components whose condition fails: it reads its own
        # destination.
        out.append((_cc_name(_pm[0], _pm[1]),
                    _mask_of_letters(_pm[2]) if _pm[2] else d[1]))
        # ... and its own destination LAST, after the value it writes
        # (notes/114 §20): `0114_sv_d.frag`'s node 0.5 (`tools/gsum.py`) lists
        # `srcs=0.4,0.3,0.2` -- the condition, the MUL it writes, and the
        # const its destination already held.
        if _os.environ.get("G2S_PREDSELFEARLY"):
            out.append(d)
        else:
            _selfread = d
    if _lmem_name(d[0]) is not None:
        d = (_lmem_name(d[0]), 0xF)
        out.append((d[0], 0xF))
    for s in srcs:
        if not s.strip():
            continue
        nm, mk = _split(s)
        if tex2d and "handle(" not in s and s is not srcs[-1]:
            mk = _texture_2d_mask(s, mk)
        elif texcube and "handle(" not in s and s is not srcs[-1]:
            mk = _texture_2d_lanes(s, mk, (0, 1, 2))
        elif txl2d and "handle(" not in s and s is not srcs[-1]:
            mk = _texture_2d_lanes(s, mk, _TXL_2D_LANES)
        if _lmem_name(nm) is not None:
            # THE ADDRESS IS RELEASED FIRST, as a TEX's handle is: in
            # `0071_lm_icb.frag`'s pass 1 the index carrier sits AFTER the last
            # element store (`entry[68]` 64 against 48), the reverse of the
            # picks, so the load hands its address to the release before the
            # array.
            out.extend(_inner_reads(s))
            out.append((_lmem_name(nm), 0xF))
            continue
        mk = _lane_mask(base, d[1], s, mk)
        if base in _DOT_WIDTH:
            mk = _dot_mask(_DOT_WIDTH[base], s)
        out.append((nm, mk))
        out.extend(_inner_reads(s))
    if _selfread is not None:
        out.append(_selfread)
    out.extend(_inner_reads(dst))
    if base in _IMAGE_MEMORY_OPS:
        # THE IMAGE MEMORY (notes/111): an image load or store reads and
        # writes it, a handle load reads it -- `tools/gsum.py`: `0110_si_d.comp`
        # has kind-0 edges handle load -> the next LOADIM, LOADIM -> the
        # next handle load and the next LOADIM, and none between two handle
        # loads; `0110_si_b.comp`'s first handle load has none to the STOREIM
        # (the LOADIM between wrote it).  An image op's read is its FIRST
        # release: `st5.comp`'s pass 1 lists the two stores in order, and
        # `0110_si_b.comp`'s LOADIM, both the STOREIM's texel and the memory it
        # wrote, is released at the texel as before.
        out.insert(0, (_IMEM, 1))
    elif mnem == "LDC.U64" and _IMAGE_MEMORY_OPS:
        # a handle load reads it too, after its address: `0110_si_e.comp`'s pass
        # 1 lists the LOADIM before the next handle load (t68 32, 48), the
        # load releasing it
        out.append((_IMEM, 1))
    return mnem, d, out


def _reads_merge(items, j, name, smask):
    """Does a read of `name` with mask `smask` take the MERGE at `j`?

    `j` is the pass-through half of a local's component store, and the line
    just before it is the component write (glasm.py builds the pair in that
    order).  A read that covers the WRITTEN component takes the stored value,
    which is both halves.  The walker's DAG then has the copy as a producer,
    a kind-0 edge copy -> reader.  `0073_pt_c.vert` shows this: `a.xyz = p; a.w =
    1.0; dot(a, b)` prints `MOV.F R2.xyz, R2; MOV.F R2.w, {1..}.x;` and only
    then the DP4.  A read of the OTHER components only (`0052_lo_parts.vert`'s
    `result.position.x` beside `v.w = ...`) finds no write to them in the
    block and reads the name at block entry, which is the kind-2 edge
    reader -> copy (notes/55 §7).

    Returns the component write's line, or None.  The write is beside the
    copy: before it in creation order, after it once pass 1 has put the copy
    first (the per-block pass-2 items).  It is the neighbour that writes the
    same register in exactly the components the copy leaves out."""
    p = _merge_partner(items, j)
    if p is None:
        return None
    _m, (dst, dmask), _s = items[p]
    return p if dst == name and dmask & smask else None


def _merge_partner(items, j):
    """The component write paired with the pass-through copy at `j`."""
    cp = items[j]
    if cp is None:
        return None
    _m, (cdst, cmask), _s = cp
    for k in (j - 1, j + 1):
        if 0 <= k < len(items) and items[k] is not None:
            _m, (dst, dmask), _s = items[k]
            if dst == cdst and dmask and not dmask & cmask:
                return k
    return None


def _passthru_masks(items):
    """{line: the components a whole store merely PASSES THROUGH}.

    A flush that stores a value back into the name it was built from changes
    only the lanes the value replaced.  `chr_cloth_11fc2b75.frag`:

        MOV.F #406.xyz, #3;      the value takes the name's own .xyz
        MOV.F #406.w, #405.x;    and replaces .w
        MOV.F #3, #406;          stored back -- so only .w changes

    The compiler reads `#3.xyz` from BEFORE that store and is then free to
    schedule the store last, which is the order its pass 1 produces and ours
    does not.  This is notes/114 §16 and §7 one shape further out: there the
    stored value was a whole copy of ANOTHER local, here it is built from the
    local being stored to.

    Read off the items, which already carry it -- no new channel from the
    lowering.  `G2S_NOPTMASK=1` turns it off.
    """
    if _os.environ.get("G2S_NOPTMASK"):
        return {}
    out = {}
    for i, it in enumerate(items):
        if it is None:
            continue
        _m, (dst, dmask), srcs = it
        if not dst or len(srcs) != 1 or dmask != 0xF:
            continue
        src, smask = srcs[0]
        if smask != 0xF or not _lex.is_numbered(src, "#"):
            continue
        mask = 0
        for j in range(i - 1, -1, -1):
            jt = items[j]
            if jt is None:
                continue
            _m2, (d2, m2), s2 = jt
            if d2 == src and len(s2) == 1 and s2[0][0] == dst \
                    and s2[0][1] == m2:
                mask |= m2
            elif d2 == src or d2 == dst:
                break
        if mask and mask != 0xF:
            out[i] = mask
    return out


def _reads_whole(mnem):
    """Forms that read the whole operand whatever they write (`ifg.py`)."""
    return mnem.split(".")[0].startswith(("DP", "TEX", "TXL", "TXF", "LD"))


def edges(items, passthru=frozenset(), with_made=False):
    """Def-use and anti-dependence edges, by the rule at 0x4ac2c.

    `items` is the lines in the order the converter created them.  An edge
    exists when a use and a LIVE definition of the same register overlap in
    components; the forward sweep makes the def->use edges and the backward
    sweep the anti-dependences, exactly as `f_710004b220`'s two sweeps do
    (notes/51 §8).  A definition is killed by a later full write of the same
    components, which is what "live" means here.
    """
    g = _EdgeBuilder(items, passthru)
    _ptm = _passthru_masks(items)
    # THE MEMORY ORDER IS IN PASS 2'S GRAPH TOO (notes/114 §33).  The same
    # edges `_memory_pairs` gives pass 1's release walk are in the compiler's
    # `graph2` as ordinary kind-0 successors -- on
    # `particle_fog_block_init.comp`'s first block its graph is LD0..LD7 ->
    # ST8 -> ST9 -> ST10 -> ST11 -> LD12..LD15, exactly the rule.  Without
    # them pass 2 is free to hoist a load above the stores it must follow,
    # which is what it did: two of that block's four loads came out directly
    # after the first store instead of after the fourth.
    # NO CUT HANDLING IS NEEDED: `_pass2` counts only predecessors inside
    # the block it is running over, so an edge that spans a cut is ignored.
    _mem_prev, _mem_since = None, []
    _mem_edges = {}
    if not _os.environ.get("G2S_NOMEMORDER"):
        for i, it in enumerate(items):
            if it is None:
                continue
            fam = it[0].split(".")[0]
            if fam in _MEM_LOAD:
                if _mem_prev is not None:
                    _mem_edges.setdefault(i, []).append(_mem_prev)
                _mem_since.append(i)
            elif fam in _MEM_STORE:
                if _mem_prev is not None:
                    _mem_edges.setdefault(i, []).append(_mem_prev)
                _mem_edges.setdefault(i, []).extend(_mem_since)
                _mem_prev, _mem_since = i, []
    for i, it in enumerate(items):
        if it is None:
            continue
        _mnem, (dst, dmask), srcs = it
        for name, smask in srcs:
            # A COMPONENT-WISE INSTRUCTION READS AN UNSWIZZLED SOURCE
            # THROUGH ITS WRITE MASK (notes/114 \u00a775).  `ifg.py`'s
            # `positions()` already applies this -- "a dot product and the
            # memory and texture forms read the whole operand whatever they
            # write" -- and `edges()` did not, so `MOD.U R0.x, R2, {3..}.x`
            # counted as reading all of R2 and took a write-after-read edge
            # to the `.yz` pass-through beside it, which the compiler's MOD
            # does not have.  `G2S_NOSRCNARROW=1` reads the whole operand.
            if (smask == 0xF and dmask and not _reads_whole(_mnem)
                    and not _os.environ.get("G2S_NOSRCNARROW")):
                smask = dmask
            g.read_after_write(i, name, smask)
        # AFTER THE SLOT LOOP, as 0x49a24 adds them: the reader's own kind-0
        # key, so they sort with the edges its sources made.
        for _j in _mem_edges.get(i, ()):
            g.add(_j, i, (0, i))
        for _xn, _xm in _extra_defs(it):
            g.write_after_read(i, _xn, _xm)
            g.write_after_write(i, _xn, _xm)
        if not dst:
            continue
        _dm = dmask & ~_ptm.get(i, 0)
        g.write_after_read(i, dst, _dm)
        g.write_after_write(i, dst, _dm)
    if with_made:
        return g.finish() + (g.made,)
    return g.finish()


class _EdgeBuilder(object):
    """The forward sweep's state: the live definitions and the reads since
    each, and the edges with the order they were made in."""

    def __init__(self, items, passthru):
        self.items = items
        self.passthru = passthru
        self.n = n = len(items)
        self.succ = [[] for _ in range(n)]
        self.npred = [0] * n
        self.made = {}              # (a, b) -> when the sweep made it
        self.defs = {}              # name -> [(mask, index)...] live defs
        self.reads = {}             # name -> [(mask, index)...] since that def
        self.war = {}               # (reader, name) -> [(writer, mask)...]
        # the ASSEMBLED READ, as `_live_reads` computes it (notes/114
        # \u00a743): the lanes of one destination are one read in the
        # compiler, so they are asked together whether they take the merge.
        self.grp = {}
        if not _os.environ.get("G2S_NOGRPREAD"):
            for _it in items:
                if _it is None or not _it[1][0]:
                    continue
                for _n, _sm in _it[2]:
                    _k = (_it[1][0], _n)
                    self.grp[_k] = self.grp.get(_k, 0) | _sm

    def add(self, a, b, when):
        """PUSHED AT THE HEAD.  `f_710004ab80` builds `entry[56]` by pushing,
        so a node's successor list is the reverse of the order the edges were
        made in -- and the release walks it in that order, which is what
        decides which of two newly-ready nodes the selector's scan meets
        first (`0052_co_add1.vert`: the store and the construct's first component
        both become ready on the same release).

        WHEN is the sweep's own order (notes/51 §8, 0x4b3ec / 0x4b5c0): the
        uses and the node's own destination (kinds 0 and 1) are made in the
        FORWARD sweep, node by node; the anti-dependences (kind 2) in the
        BACKWARD sweep that follows it, at the READER, last node first.  So a
        producer's kind-2 successors were all made after its kind-0 ones and
        come FIRST in its list once the list is reversed -- the geometry
        probes' `gather -> copy` (kind 2) is released before the `gather ->
        store` (kind 0) it was made after (notes/65)."""
        if a != b and b not in self.succ[a]:
            self.succ[a].append(b)
            self.made[(a, b)] = when
            self.npred[b] += 1

    def read_after_write(self, i, name, smask):
        """READ AFTER WRITE: the live definition must issue first -- unless
        it is a component store's pass-through copy the read does not take:
        the read then takes the value from BEFORE the copy (the name at block
        entry), the compiler's kind-2 edge, reader -> copy (notes/55 §7)."""
        live = self.defs.get(name, ())
        for mask, j in live:
            if not mask & smask:
                continue
            if j in self.passthru:
                _p = _reads_merge(self.items, j, name, smask | self.grp.get(
                    (self.items[i][1][0], name), 0))
                if not (_p is not None and any(k == _p for _m2, k in live)):
                    self.add(i, j, (1, self.n - i))
                    continue
            self.add(j, i, (0, i))
        self.reads.setdefault(name, []).append((smask, i))

    def write_after_read(self, i, dst, dmask):
        """WRITE AFTER READ -- the anti-dependence `f_710004ab80`'s kind 2 arm
        builds with producer and consumer swapped.  A reader of the
        components this line is about to overwrite must issue BEFORE it;
        getting this edge backwards groups every component copy together and
        separates it from the store that reads it, which is what
        `0011_op_mul.vert` showed."""
        for mask, j in self.reads.get(dst, ()):
            if mask & dmask:
                self.add(j, i, self._war_key(i, j, dst, dmask, mask))
                self.war.setdefault((j, dst), []).append((i, dmask))

    def _war_key(self, i, j, dst, dmask, rmask=0xF):
        """ONE READER'S anti-dependences are made over its SOURCES LAST
        FIRST, and for each source the LATER writer first (notes/90):
        `bv_n23.vert`'s MUL reads `R0.x` then `R35`, and its list is the `R0`
        store, then the `.yzw` copy, then the `.x` write -- pushed at the
        head, so made in the reverse order.  `G2S_WARFLAT=1` keeps the old
        key."""
        n = self.n
        if _WARFLAT:
            return (1, n - j)
        _sj = self.items[j][2]
        _s = next((k for k, (nm2, m2) in enumerate(_sj)
                   if nm2 == dst and m2 & dmask), 0)
        if _WARLATER:
            return (1, n - j, len(_sj) - 1 - _s, n - i)
        # WITHIN ONE SOURCE, BY COMPONENT: the writer of the source's
        # lowest overlapping component first (notes/110 §3).  `0109_rc_g.frag`'s
        # MUL reads `R0` whole, and the compiler's list (`tools/gsum.py`,
        # node 0.4) is `w, z, y` writes and then the `.x` gather, all kind
        # 2 -- made x, y, z, w, though the `.x` gather is the EARLIEST of
        # the four writers in the list.  `G2S_WARLATER=1` restores the
        # later-writer-first key.
        _ov = rmask & dmask
        _c = next(c for c in range(4) if _ov >> c & 1) if _ov else 4
        return (1, n - j, len(_sj) - 1 - _s, _c, n - i)

    def write_after_write(self, i, dst, dmask):
        """WRITE AFTER WRITE: two definitions of the same components keep
        their order, or the later one can be overwritten by the earlier.
        Then this line is the live definition of its components, and the
        reads of them before it are no longer outstanding."""
        for mask, j in self.defs.get(dst, ()):
            if mask & dmask:
                self.add(j, i, (0, i))
        kept = [(m & ~dmask, j) for m, j in self.defs.get(dst, ())]
        self.defs[dst] = [(m, j) for m, j in kept if m] + [(dmask, i)]
        self.reads[dst] = [(m & ~dmask, j) for m, j in self.reads.get(dst, ())
                           if m & ~dmask]

    def finish(self):
        self._war_components()
        for a in range(self.n):
            self.succ[a].sort(key=lambda b: self.made[(a, b)])
        return self.succ, self.npred

    def _war_components(self):
        """THE COMPONENT AN ANTI-DEPENDENCE IS MADE FOR IS THE ONE THIS WRITER
        IS THE LAST TO WRITE, and a writer that is the last of none is made
        LAST (`tools/waredge.py`).

        One reader's anti-dependences over one source are made component by
        component, and the writer a component belongs to is the LAST one to
        write it -- so a write that a later write covers again is not made for
        that component at all, and drops behind the writers that keep one.
        `map_77e7d345.frag`'s TRUNC reads `R2` whole: the writers are the
        LDC of `R2.x`, the `.w` and `.z` lanes and the `.xy` lane that writes
        `x` again, and the compiler's list (`tools/gsum.py`, node 18.5) is the
        LDC, `.w`, `.z`, `.xy` -- made `.xy` (which keeps `x` and `y`), `.z`,
        `.w`, and the LDC last, though the LDC's own component is the lowest
        of the four.  `probes/0110_wk_d.frag` and `probes/0109_rc_g.frag`, where no
        writer covers another, are the component order as before.
        `G2S_NOWAROWN=1` keeps each writer's own lowest component.
        """
        if _WARFLAT or _WARLATER or _NOWAROWN:
            return
        for (j, _name), group in self.war.items():
            if len(group) < 2:
                continue
            owner = {}
            for i, dmask in group:
                for c in range(4):
                    if dmask >> c & 1:
                        owner[c] = i          # the LAST writer of c keeps it
            high = {}
            for c, i in owner.items():
                high[i] = max(high.get(i, 0), c)
            for i, _dmask in group:
                key = self.made.get((j, i))
                if key is None or len(key) != 5:
                    continue
                self.made[(j, i)] = key[:3] + (high.get(i, 4),) + key[4:]


def order(lines, roots=()):
    """The order the compiler would emit these lines in, or None.

    `lines` are the converter's own, in creation order -- which is `node[36]`,
    the key the selector compares first (notes/51).  `entry[68]`, the second
    key, is derived here the way pass 1 derives it: a node's rank in the
    first pass's output, 16 to the step.

    Returns a permutation of `range(len(lines))`, or None when any line does
    not parse -- silence beats a guess, and the caller keeps its own order.
    """
    # THE TERMINATOR IS NOT SCHEDULED WITH THE REST.  notes/51 §2: the
    # emitter hands the block's terminator to the loop as the pick, skipping
    # the comparator once, so it does not compete on the keys.  Leaving it in
    # lets it drift into the middle of the block, which is what it did.
    body, _alloc, tail = _strip_terminators(lines)
    items = _parse_all(body)
    if items is None:
        return None
    succ, npred = edges(items)
    t68 = _successor_count_t68(succ)
    if t68 is None:
        return None
    out = _successor_count_pass2(succ, npred, t68)
    if out is None:
        return None
    return out + tail


def _successor_count_t68(succ):
    """PASS 1 as `order()` approximates it, `f_710004a2e0` bottom-up: the
    release walks CONSUMERS before producers, the ready list is pushed at the
    head and the scan keeps the TAIL -- the entry that has been ready longest
    (notes/49) -- and each pick is FRONT-pushed into the block's list, so
    that list is the pick order reversed.  `entry[68]` is 16 times a node's
    position in it.  None when some node is never picked."""
    n = len(succ)
    pred = [[] for _ in range(n)]
    for a in range(n):
        for b in succ[a]:
            pred[b].append(a)
    nsucc = [len(succ[a]) for a in range(n)]
    ready = []
    for i in range(n):
        if nsucc[i] == 0:
            ready.insert(0, i)
    emitted = []
    while ready:
        i = ready.pop()                     # the tail: ready longest
        emitted.insert(0, i)                # f_7100030c74 pushes at the FRONT
        for a in pred[i]:
            nsucc[a] -= 1
            if nsucc[a] == 0:
                ready.insert(0, a)
    if len(emitted) != n:
        return None
    return dict((i, r * CYCLE) for r, i in enumerate(emitted))


def _successor_count_pass2(succ, npred, t68):
    """PASS 2, `f_710004b220` with the selector `f_710004ba90`, over the
    whole body.

    The selector is a SCAN, not a sort (notes/51 §4): it replaces the running
    best when the candidate's `node[36]` is smaller OR -- failing that -- its
    `entry[68]` is smaller.  That is an OR of two tests, not a lexicographic
    pair, and the difference is visible: on `0044_fr_mrt.frag` the lexicographic
    version emits the unmodified store first and the compiler emits it last.

    The cycle clock advances one step per issue (`cg[28] = cg[24] << 4`), and
    every resource record measured is 0xffffffff, which is one issue per
    cycle; a release raises its consumer's depth to `clock + 1` (0x4b764)."""
    n = len(succ)
    count = list(npred)
    depth = dict((i, 0) for i in range(n))
    work = []
    for i in range(n):
        if count[i] == 0:
            work.insert(0, i)
    out = []
    cycle = 0
    while work and cycle < 4 * n + 256:
        clock = cycle * CYCLE
        issued = False
        while not issued:
            cands = [i for i in work if depth[i] <= clock]
            if not cands:
                break
            best = None
            for i in cands:
                if best is None or i < best or t68[i] < t68[best]:
                    best = i
            work.remove(best)
            out.append(best)
            issued = True
            for b in succ[best]:
                count[b] -= 1
                if depth[b] < clock + 1:
                    depth[b] = clock + 1
                if count[b] == 0:
                    work.insert(0, b)
        cycle += 1
    if len(out) != n:
        return None
    return out


# ---------------------------------------------------------------------------
# The two passes, driven by the converter's PRE-ALLOCATION lines.
#
# `order()` above runs on the lines after the allocator has renamed them, and
# it approximates pass 1 with a successor count.  Neither is what the compiler
# does, and both are why it is off by default:
#
#   * PASS 1 IS NOT A SUCCESSOR COUNT.  notes/51 §2.1: the emitter walks the
#     block's live entries -- ONE PER VREG -- and each entry decrements the
#     pending-use count of every node that writes that vreg; a node becomes
#     ready when the count reaches zero, the ready list is pushed at the head
#     and picked from the TAIL, and each pick releases the operands it reads.
#     The picks are then reversed (`f_7100030c74` pushes at the front), and
#     `entry[68]` is 16 times the position in that reversed list.
#   * `node[36]` IS PER VREG, NOT PER LINE.  Measured on `0053_co_mul4.vert`, whose
#     four component writes of one `vec4` all carry seq 7 while the multiplies
#     that feed them carry 1, 4, 5 and 6; on `0052_co_mix4.vert` three of the four
#     carry seq 1.  A line index gives them four DIFFERENT keys and the
#     selector then resolves them the wrong way round.  Running before the
#     allocator is what makes this available: each value still has its own
#     `#n` name there, and after the allocator several values share `R0`.
#   * A LIST IS A BLOCK (notes/51 §1), and the store's expansion into one
#     assignment per element (notes/44) puts each output component in its own.
#     Measured: `p06_swizzle`, `op_add`, `op_dot`, `co_mul4` and `co_mix4` all
#     dump one block per output component, the first holding the computation.
_OUTPUT_DST = "result"


def _is_colour(name):
    r"""`result_color\d+$` matched at the start."""
    return _lex.is_numbered(name, "result_color")


def _block_spans(items, cuts=(), names=frozenset(), passthru=frozenset(),
                 calls=frozenset()):
    """The blocks, as index lists.

    THE WALKER'S TEST, READ (notes/65).  Before an assignment statement the
    statement walker `f_7100f13ff0` (0xf142c8..0xf14308, 0xf15aa4..0xf15ae8)
    looks up the destination's NAME RECORD (`f_7100f0dff0`, the 0x68-byte
    record hung off the symbol at `sym[96]`) and opens a new block
    (`f_7100f0bc10`) when `record[40]` is set and the current block already
    holds a node.  The record is `[24]` the block of the name's last access,
    `[32]` a read made in that block and `[40]` a store made in it; an access
    from a DIFFERENT block starts the record afresh (measured with
    `g2s_trace_blkrec` on the geometry probes).  So a name's store is
    "pending" from the store until the name is next touched in a later
    block, and a statement storing a pending name opens a block:

      * a second store to a name in one block (notes/55 §8's rule, the
        special case: `gl_Position = v`'s one block per element, `w2_mov`'s
        second `gl_Position.xy`, `if_out`'s `o0` staying in the `.w` block);
      * a store to a name stored in an EARLIER block and not read since
        (the geometry stage's `gl_Position.y = shadow.y` opens a block after
        `shadow.y = ...` although `gl_Position` is not stored in it), while
        a READ in a later block clears it (`shadow.z = ...` then stays with
        `gl_Position.y`, which read `shadow` at that block's entry).

    The boundary falls right after the current block's last store line, so
    the lines that compute the new store's value -- and the names they read
    -- go with it.  `cuts` are explicit boundaries.  `calls` are writes that
    are not assignment statements (the emitVertex call's output write,
    notes/65): they never open a block and are a name of their own.
    """
    w = _BlockWalk()
    cuts = set(cuts)
    for i, it in enumerate(items):
        if i in cuts and w.cur:
            w.touch_pending()
            w.close()
        name = None
        reads = []
        if it is not None:
            if it[1][0].startswith(_OUTPUT_DST) or it[1][0] in names:
                # an output, or a materialised LOCAL -- both are front-end
                # names (notes/64)
                name = it[1][0]
            reads = [n for n, _m in it[2] if n in names]
        if i in passthru:
            w.passthru(i, reads)
        elif i in calls:
            w.call(i, reads)
        elif name is None:
            w.cur.append(i)
            w.pend.extend(reads)
        else:
            w.store(i, name, reads)
    if w.cur:
        w.out.append(w.cur)
    return [b for b in w.out if b]


class _BlockWalk(object):
    """The walker's state for `_block_spans`: the blocks so far, the current
    one, and each name's record."""

    def __init__(self):
        self.out, self.cur = [], []
        self.last_store = -1        # position in `cur` after its last store
        self.blk = 0
        self.rec = {}               # name -> [block of last access, pending]
        self.pend = []              # names read by lines after `last_store`

    def touch(self, nm, store):
        r = self.rec.get(nm)
        if r is None or r[0] != self.blk:
            r = [self.blk, False]
            self.rec[nm] = r
        if store:
            r[1] = True

    def touch_pending(self):
        for nm in self.pend:
            self.touch(nm, False)
        self.pend = []

    def close(self):
        """An explicit cut: the current block ends here."""
        self.out.append(self.cur)
        self.cur, self.last_store = [], -1
        self.blk += 1

    def passthru(self, i, reads):
        """The pass-through half of a local's component store belongs to the
        store beside it: it ends that store (the boundary moves past it) and
        reads the name in the store's block."""
        self.cur.append(i)
        self.last_store = len(self.cur)
        for nm in reads:
            self.touch(nm, False)

    def call(self, i, reads):
        """A write that is not a statement: never opens a block."""
        self.cur.append(i)
        self.pend.extend(reads)
        self.touch_pending()
        self.last_store = len(self.cur)

    def store(self, i, name, reads):
        r = self.rec.get(name)
        if r is not None and r[1] and r[0] != self.blk and name in self.pend:
            # The name was READ by a line of this block before its store:
            # that line is a statement of its own (every SPIR-V value is a
            # named temp, notes/66 §3), so the walker saw the read first and
            # it restarted the record in this block -- the store is no
            # longer pending (notes/65 §4).  `lens_flare_ghost_tex.frag`'s
            # `u_xlat1 = (-u_xlat1) * 0.5 + u_xlat2`: the compiler's block 12
            # holds `u_xlat2 = exp2(...)` AND the `u_xlat1` store, because
            # the negate read `u_xlat1` there (g2s_dfent, notes/70).
            self.touch(name, False)
            r = self.rec.get(name)
        if r is not None and r[1] and self.last_store > 0:
            self.out.append(self.cur[:self.last_store])
            self.cur = self.cur[self.last_store:]
            self.last_store = -1
            self.blk += 1
        self.pend.extend(reads)
        self.touch_pending()
        self.touch(name, True)
        self.cur.append(i)
        self.last_store = len(self.cur)


def _live_reads(items, span, groups=None, passthru=frozenset()):
    """`{line: [(def, line)...]}` -- which definition each read takes, in-block.

    The same live-definition walk `edges()` does, kept separate because pass 1
    needs the pairs and not the edge list.

    `passthru` are the lines that are the PASS-THROUGH half of a local's
    component store (`MOV #r.yzw, #r` beside `MOV #r.x, v`, notes/55 §7):
    the components they write keep their value, so they produce nothing a
    later read in the block could take.  A read that finds no in-block
    producer reads the value at BLOCK ENTRY -- the front end's 0x2b register
    read of the name.  `groups`, when given, collects those entry reads per
    placeholder in creation order: `{name: [line...]}`.
    """
    pairs = []
    defs = {}
    ptdefs = {}                     # name -> [(mask, line)] live pass-throughs
    _ptm = _passthru_masks(items)   # components a whole store passes through
    # THE ASSEMBLED READ (notes/114 \u00a743).  `_reads_merge` asks whether a
    # read covers a component the block STORED, and that is the mask of the
    # SOURCE's read, not of one lowered line.  A value assembled lane by lane
    # -- the condition-code moves of a vector select -- is ONE read in the
    # compiler: one 0x57 node over the lanes, each lane reading the merge
    # through it.  So the lanes are asked TOGETHER, by the destination they
    # assemble.  `0114_m1.frag` (`a.xzxz`, a read spanning the stored lane) reads
    # the merge; `0114_m2.frag` (`a.xxxx`, the carried lane alone) reads the name
    # at block entry and is the copy's implicit read -- both READ off the
    # compiler's own DAG, not off a listing.  `G2S_NOGRPREAD=1` asks each
    # lowered line for itself again.
    _grp = {}
    if not _os.environ.get("G2S_NOGRPREAD"):
        for _i2 in span:
            _it2 = items[_i2]
            if _it2 is None or not _it2[1][0]:
                continue
            for _n2, _sm2 in _it2[2]:
                _k2 = (_it2[1][0], _n2)
                _grp[_k2] = _grp.get(_k2, 0) | _sm2
    for i in span:
        it = items[i]
        if it is None:
            continue
        _mnem, (dst, dmask), srcs = it
        for name, smask in srcs:
            if (name == _IMEM and _mnem == "LDC.U64"
                    and _os.environ.get("G2S_LDCIMEMP2ONLY")):
                continue
            hit = 0
            for mask, j in defs.get(name, ()):
                if (name == _IMEM and _mnem == "LDC.U64"
                        and items[j][0] == "LDC.U64"):
                    # a handle load waits on the image ops before it, not
                    # on a handle loaded at its OpLoad (`0110_si_b.comp`,
                    # `0110_si_e.comp`: the op's own handle load is listed
                    # first, the earlier-loaded one second)
                    continue
                if mask & smask:
                    pairs.append((j, i))
                    hit |= mask & smask
            # a read covering a component store's WRITTEN component takes
            # the merge, pass-through half included (`_reads_merge`)
            for mask, j in ptdefs.get(name, ()):
                _p = _reads_merge(items, j, name,
                                  smask | _grp.get((dst, name), 0)) \
                    if mask & smask else None
                if _p is not None and any(k == _p for _m, k
                                          in defs.get(name, ())):
                    pairs.append((j, i))
                    hit |= mask & smask
            if (groups is not None and smask & ~hit and name.startswith("#")
                    and (passthru or not _os.environ.get("G2S_IRRPASSONLY"))):
                g = groups.setdefault(name, [])
                if i not in g:
                    g.append(i)
                # the COMPONENTS read at entry: the implicit reads are kept
                # one list per component (`_implicit_read_pairs`)
                _gm = groups.setdefault(("mask", name), {})
                _gm[i] = _gm.get(i, 0) | (smask & ~hit)
        for _xn, _xm in _extra_defs(it) + _pass1_extra_defs(it):
            defs[_xn] = [(_xm, i)]
        if not dst:
            continue
        if groups is not None and dst.startswith("#"):
            # every WRITE of the name: each walks its lanes' reader lists
            groups.setdefault(("writers", dst), []).append(i)
        dmask = dmask & ~_ptm.get(i, 0)
        kept = [(m & ~dmask, j) for m, j in defs.get(dst, ())]
        ptkept = [(m & ~dmask, j) for m, j in ptdefs.get(dst, ())]
        ptdefs[dst] = [(m, j) for m, j in ptkept if m]
        if i in passthru:
            defs[dst] = [(m, j) for m, j in kept if m]
            ptdefs[dst].append((dmask, i))
            continue
        defs[dst] = [(m, j) for m, j in kept if m] + [(dmask, i)]
    return pairs


_MEM_LOAD = frozenset(("LDB", "LDC"))
_MEM_STORE = frozenset(("STB",))


def _memory_pairs(items, span):
    """THE MEMORY ORDER (notes/114 \u00a733), the second half of `node[120]`'s
    implicit-read chain.  Buffer accesses are ordered against each other
    inside their block:

        a buffer LOAD implicitly reads the block's most recent buffer STORE;
        a buffer STORE implicitly reads the block's most recent buffer STORE
        AND every buffer load since it;
        nothing orders two loads against each other.

    That is WAW + WAR + RAW and no RAR, and the `most recent store` chain is
    why the transitive edges are absent: the compiler emits exactly these and
    no more.  READ off the compiler's own `irr=` lists, not off a listing:
    over 638 traces it reproduces 3444 of the 3446 edges between 0x3b/0x3c
    nodes, edge for edge, including every irregular case -- the first store
    of a block reading EIGHT preceding loads, a block's first store reading
    nothing, and a store reading both the previous store and the one load
    between them.

    IT IS NOT ALIAS-AWARE.  Every load in the shader that forced this out
    reads `sbo_buf0` and every store writes `sbo_buf1`, so the two can never
    collide, and the compiler orders them anyway.  It does not partition by
    the value type `node[44]` either: `0109_st_f.comp` has an `LDC.F32X4` (type 6)
    released by an `STB.U32` (type 12).

    IMAGE STORES ARE NOT IN IT: `STOREIM` is op 0x2c, not 0x3c, and carries
    no such edge in any of `hl_a`, `hl_b`, `hl_c`, `si_a`, `si_b`.  That is
    read, not assumed.

    `G2S_NOMEMORDER=1` drops the edges."""
    if _os.environ.get("G2S_NOMEMORDER"):
        return []
    pairs = []
    prev_store = None
    since = []
    for i in span:
        it = items[i]
        if it is None:
            continue
        fam = it[0].split(".")[0]
        if fam in _MEM_LOAD:
            if prev_store is not None:
                pairs.append((prev_store, i))
            since.append(i)
        elif fam in _MEM_STORE:
            if prev_store is not None:
                pairs.append((prev_store, i))
            pairs.extend((j, i) for j in since)
            prev_store = i
            since = []
    return pairs


def _implicit_read_pairs(groups, passthru, items=None):
    """THE IMPLICIT READS (0x49b88, notes/51 "The implicit reads"): a
    register read of a name at block entry is FOLDED into one reader, its
    owner -- the last one created -- and every other reader of that same
    read releases the owner after its slots, as if it read it.
    `0052_lo_parts.vert`: the merge's copy half releases `result.position.x`'s
    MOV.

    WHICH READER CARRIES THEM (notes/65): the one whose `node[120]` group is
    set, and that is the merge's COPY -- the pass-through half of the local's
    store.  It releases every other reader of the same entry read.
    `0052_lo_parts.vert` cannot tell this from "the last reader created owns it"
    (the copy precedes `result.position.x` there); the geometry probes can:
    `0046_g01.geom`'s copy (seq 35) releases the gather (seq 30) that was created
    BEFORE it (notes/51, "The implicit reads").

    The owner releases the other readers LAST CREATED FIRST: the list is
    built by pushing at its head, as `entry[56]` is (0x4acf0).  `0077_mb_n17.vert`
    has two other readers of one entry read -- the DP3s of `u_xlat0.x =
    dot(N, u_xlat3.xyz)` and `u_xlat3.x = dot(T, u_xlat3.xyz)` -- and the
    compiler's pass 1 lists them DP3(N), DP3(T), i.e. released T first
    (notes/77).

    ONE LIST PER COMPONENT, AND BOTH HALVES OF THE STORE WALK THEM (0x49a74,
    the hook's group one -- `node[120]` is zero on every node below, `irx=
    none`): the releaser takes its register row `node[92]` in `program[816]`
    and, for each component k it WRITES (`node[48+k]`, x first), releases
    every record on that row's list k.  `0105_ab_a.frag`'s copy `MOV R4.xyz, R4`
    releases `abs(u_xlat1.y)` from list y and then `abs(u_xlat1.z)` from list
    z -- the creation order, where one list newest-first would give the
    reverse and print the two carriers the wrong way round.  `0077_mb_n17.vert`'s
    copy (yzw) carries `irr=T,N,T,N` -- the DP3s read xyz, so they are on
    lists y and z and on none for w -- and the store's WRITE half (`.x`)
    carries `irr=T,N` from list x.  Neither half is on its own lists: the
    copy's `irr` names only the two carriers.  `G2S_IRRCREATION=1` restores
    the one-list reading.

    AND EVERY WRITE OF THE NAME WALKS THEM, not only a component store's two
    halves (notes/106): `0106_ow_a.frag`'s `o.w = u_xlat0; u_xlat0 = u_xlat0 *
    c7` -- the store `MOV #2.x, #17` releases the entry readers of `.x`, the
    MUL and then the output lane, newest first, and that is the compiler's
    pass-1 list (the lane at `entry[68]` 32, the MUL 48).
    `G2S_IRRPASSONLY=1` keeps the two halves only."""
    pairs = []
    _old = _os.environ.get("G2S_IRRCREATION")
    _halves = {}
    for p in passthru:
        _w = _merge_partner(items, p) if items is not None else None
        _halves[p] = (p, _w)
        if _w is not None:
            _halves[_w] = (p, _w)
    for _name, g in groups.items():
        if isinstance(_name, tuple):
            continue
        _gm = groups.get(("mask", _name), {})
        if _old:
            for p in g:
                if p in passthru and len(g) >= 2:
                    for r in reversed(g):
                        if r != p:
                            pairs.append((r, p))
            continue
        if _os.environ.get("G2S_IRRPASSONLY"):
            _wr = []
            for p in g:
                if p in passthru:
                    _wr.extend(q for q in _halves[p] if q is not None)
        else:
            _wr = groups.get(("writers", _name), [])
        for q in _wr:
            _own = _halves.get(q, (q,))
            _qmask = items[q][1][1]
            for k in range(4):
                if not _qmask & (1 << k):
                    continue
                for r in reversed(g):
                    if r in _own or not _gm.get(r, 0) & (1 << k):
                        continue
                    pairs.append((r, q))
    return pairs


def _vreg_entries(items, span):
    """The vreg entries, in the order the vregs are first written:
    (the vregs, vreg -> its lines)."""
    order_v, by_v = [], {}
    for i in span:
        it = items[i]
        if it is None:
            continue
        v = it[1][0]
        # A BUFFER STORE IS ITS OWN ENTRY (notes/114 \u00a734).  Every other
        # vreg appears in `block[80]` exactly once; the buffer symbol appears
        # ONCE PER STORE -- four times in each of
        # `particle_fog_block_init.comp`'s four-store blocks and eight times
        # in its eight-store one -- so the stores are not one entry that
        # collects them.  Keyed apart, each takes its own place in the walk,
        # which is where the compiler decrements it.
        if (not _os.environ.get("G2S_NOSTOREENTRY")
                and it[0].split(".")[0] in _MEM_STORE):
            v = ("store", i)
        if v not in by_v:
            by_v[v] = []
            order_v.append(v)
        by_v[v].append(i)
    return order_v, by_v


def _order_entries(order_v, by_v, span, band):
    """The order pass 1 walks the vreg entries in (sorted in place).

    THE BLOCK'S OWN NAME LIST FIRST (notes/68 §4): the entries are walked as
    `block[80]` lists the names the block stores -- in the order the
    statement walker stored them -- and then the lowering's vregs.
    `band[1]` gives each destination its key, (0, statement) for a name and
    (1, first write) otherwise.  A key given as `(name, line)` holds only in
    the block that line is in: a local stored whole in one block and merged
    in a later one keeps its first-write place in the first (`0085_pb_b.frag`).

    The older form of `band` maps name -> lowering vreg: A FLUSHED TEMP'S
    NAME (notes/67 §7) is numbered where its value was made -- the name's
    record precedes the lowering vreg the instruction writes
    (`0067_sc_select.frag`: the name vreg 2, the TRUNC vreg 9) -- so its entry
    takes the lowering vreg's place, ahead of it."""
    if isinstance(band, tuple) and band and band[0] == "vkey":
        _first = dict((v, by_v[v][0]) for v in order_v)
        _here = set(span)
        _scoped = dict((kv[0], kk) for kv, kk in band[1].items()
                       if isinstance(kv, tuple) and kk[0] in _here)
        order_v.sort(key=lambda v: _scoped.get(
            v, band[1].get(v, (_first[v], 1, 0))))
    elif band:
        for nm, lw in band.items():
            if nm in by_v and lw in order_v:
                order_v.remove(nm)
                order_v.insert(order_v.index(lw), nm)


def _release_walk(span, pairs, order_v, by_v):
    """notes/51 §2.1: each vreg entry decrements the pending-use count of
    every node that writes it; a node becomes ready when the count reaches
    zero, the ready list is pushed at the head and picked from the TAIL,
    and each pick releases the operands it reads.  Returns the picks, or
    None when some node is never picked."""
    pend = dict((i, 1) for i in span)          # the vreg entry's own decrement
    for j, _i in pairs:
        pend[j] += 1
    reads = {}
    for j, i in pairs:
        reads.setdefault(i, []).append(j)
    ready = []

    def _decrement(j):
        pend[j] -= 1
        if pend[j] == 0:
            ready.insert(0, j)

    for v in order_v:
        for i in by_v[v]:
            _decrement(i)
    picked = []
    seen = set()
    while ready:
        p = ready.pop()
        if p in seen:
            continue
        seen.add(p)
        picked.append(p)
        for j in reads.get(p, ()):
            _decrement(j)
    if len(picked) != len(span):
        if _os.environ.get("G2S_STUCK"):
            import sys as _sys
            _left = [i for i in span if i not in set(picked)]
            print("STUCK release walk left %d of %d: %s"
                  % (len(_left), len(span), _left[:8]), file=_sys.stderr)
            _sub = [(a, b) for a, b in pairs if a in _left and b in _left]
            print("   cycle edges: %s" % [
                (a, b, "/".join(_PAIR_SRC.get((a, b), ["?"])))
                for a, b in _sub], file=_sys.stderr)
        return None
    return picked


_PAIR_SRC = {}


def _pass1(items, span, seq, passthru=frozenset(), band=None):
    """notes/51 §2.1-§2.3, over one block.  Returns the emitted order: the
    picks reversed (`f_7100030c74` pushes at the front)."""
    groups = {}
    pairs = _live_reads(items, span, groups, passthru)
    _p_live = list(pairs)
    _p_impl = _implicit_read_pairs(groups, passthru, items)
    _p_mem = _memory_pairs(items, span)
    pairs.extend(_p_impl)
    pairs.extend(_p_mem)
    # `G2S_STUCK=1`: why the scheduler refused.  It prints the span that
    # could not be placed, which pass gave up, the lines left unpicked and
    # the CYCLE between them, each edge tagged with the list that made it
    # (live / implicit / memory).  notes/129 is what it was written for.
    if _os.environ.get("G2S_STUCK"):
        _PAIR_SRC.clear()
        for _e in _p_live:
            _PAIR_SRC.setdefault(_e, []).append("live")
        for _e in _p_impl:
            _PAIR_SRC.setdefault(_e, []).append("impl")
        for _e in _p_mem:
            _PAIR_SRC.setdefault(_e, []).append("mem")
    order_v, by_v = _vreg_entries(items, span)
    _order_entries(order_v, by_v, span, band)
    picked = _release_walk(span, pairs, order_v, by_v)
    if picked is None:
        return None
    picked.reverse()
    if _os.environ.get("G2S_P1DBG"):                   # diagnosis only
        import sys as _sys
        _sys.stderr.write("P1 entries %s\n   by_v %s\n   reads %s\n"
                          "   order %s\n   seqs %s\n   items %s\n"
                          % (order_v, dict((v, by_v[v]) for v in order_v),
                             pairs, picked,
                             [(i, seq[i] if i < len(seq) else None) for i in span],
                             [(i, items[i]) for i in span]))
    return picked


def _pass2(items, emitted, seq, t68, succ, npred):
    """notes/51 §4-§5, over one block.

    `emitted` is PASS 1'S OUTPUT, and the order matters: the initial worklist
    is the block's list walked in order with each ready entry pushed at the
    head (0x4b80c), and the block's list IS pass 1's output -- so the worklist
    is that list reversed.  The selector is a scan whose second test can
    overturn its first (§4), so its answer depends on that order; seeding from
    the creation order instead picks the other end of every tie.
    """
    span = emitted
    here = set(span)
    count = dict((i, sum(1 for j in npred[i] if j in here)) for i in span)
    depth = dict((i, 0) for i in span)
    work = []
    for i in span:
        if count[i] == 0:
            work.insert(0, i)
    out = []
    cycle = 0
    limit = 4 * len(span) + 256
    while work and cycle < limit:
        clock = cycle * CYCLE
        issued = False
        while not issued:
            cands = [i for i in work if depth[i] <= clock]
            if not cands:
                break
            best = None
            for i in cands:
                if best is None or seq[i] < seq[best] or t68[i] < t68[best]:
                    best = i
            work.remove(best)
            out.append(best)
            issued = True
            for b in succ[best]:
                if b not in here:
                    continue
                count[b] -= 1
                if depth[b] < clock + 1:
                    depth[b] = clock + 1
                if count[b] == 0:
                    work.insert(0, b)
        cycle += 1
    if len(out) != len(span):
        if _os.environ.get("G2S_STUCK"):
            import sys as _sys
            _left = [i for i in span if i not in set(out)]
            print("STUCK %d of %d unpicked; first few with their "
                  "unsatisfied preds:" % (len(_left), len(span)),
                  file=_sys.stderr)
            _done = set(out)
            for i in _left[:6]:
                print("   i=%d count=%d preds_left=%s succ=%s"
                      % (i, count[i],
                         [j for j in npred[i] if j in here and j not in _done],
                         [j for j in succ[i] if j in here]), file=_sys.stderr)
        return None
    return out


def _order_pre_flat(lines, allocated=None, cuts=(), ties=(), passthru=(),
                    stage1=False, list_edges=False, names=frozenset(),
                    calls=frozenset(), band=None):
    """The emission order for the converter's PRE-ALLOCATION lines, or None.

    `allocated` is the same lines with the placeholders replaced by the
    registers a first allocation pass handed out.  When it is given the EDGES
    are taken from it and everything else from the placeholders, which is the
    compiler's own split: `node[36]` and the release walk are per value, while
    `entry[56]` is built from nodes that already name a symbol -- so two
    values sharing a register carry a write-after-read edge between them.
    `0052_co_add1.vert` needs exactly that edge (its construct and its first gather
    are both `R1`) and `0053_co_mul4.vert`, whose construct has a register to
    itself, needs its absence; the two are otherwise identical.

    `list_edges` builds pass 2's edges per block over pass 1's LIST, which
    is what `f_710004b220` sweeps (notes/51 §8), instead of over the creation
    order.  It is only faithful with `allocated`: the edges are between
    registers, and pass 1's list is the order the registers were given in.

    `stage1` returns PASS 1's order instead -- each block's list as
    `f_710004a2e0` leaves it in `block[32]`, blocks in order -- which is the
    order the allocator's sweep runs over (notes/57).
    """
    body, alloc, tail = _strip_terminators(lines, allocated)
    items = _parse_all(body)
    if items is None:
        if _os.environ.get("G2S_STUCK"):
            import sys as _sys
            for _l in body:
                if parse(_l) is None:
                    print("STUCK unparsed line: %r" % _l, file=_sys.stderr)
        return None
    eitems = items
    if alloc is not None:
        eitems = _parse_all(alloc)
        if eitems is None:
            return None
    n = len(items)
    seq = _seq_keys(n, ties)
    passthru = frozenset(passthru)
    succ, _npred_counts = edges(eitems, passthru)
    pred = [[] for _ in range(n)]
    for a in range(n):
        for b in succ[a]:
            pred[b].append(a)
    out = []
    for span in _block_spans(items, cuts, names, passthru, frozenset(calls)):
        emitted = _pass1(items, span, seq, passthru, band)
        if emitted is None:
            if _os.environ.get("G2S_STUCK"):
                import sys as _sys
                print("STUCK pass1 failed on span of %d lines (context):"
                      % len(span), file=_sys.stderr)
                for _i in range(max(0, span[0] - 8), span[0]):
                    print("   (%d) %s" % (_i, body[_i]), file=_sys.stderr)
                for _i in span:
                    print("   [%d]%s %s"
                          % (_i, " PT" if _i in passthru else "   ", body[_i]),
                          file=_sys.stderr)
            return None
        if stage1:
            out.extend(emitted)
            continue
        t68 = dict((i, r * CYCLE) for r, i in enumerate(emitted))
        made = None
        if list_edges:
            made = _list_order_edges(eitems, emitted, passthru, succ, pred)
        _register_family_edges(items, emitted, succ, pred,
                               None if _FAMILYAPPEND else made)
        got = _pass2(items, emitted, seq, t68, succ, pred)
        if _os.environ.get("G2S_P2DBG") and list_edges and any(
                _os.environ["G2S_P2DBG"] in (body[i] or "") for i in emitted):
            _pass2_dump(body, alloc, emitted, seq, t68, succ, got)
        if got is None:
            return None
        out.extend(got)
    if len(out) != n:
        return None
    return out + tail


def _strip_terminators(lines, allocated=None):
    """(body, allocated body, terminator indices): THE TERMINATOR IS NOT
    SCHEDULED WITH THE REST (notes/51 §2)."""
    tail = []
    body = list(lines)
    alloc = list(allocated) if allocated is not None else None
    while body and _lex.keyword_at(body[-1], _TERMINATOR):
        tail.insert(0, len(body) - 1)
        body = body[:-1]
        if alloc is not None:
            alloc = alloc[:-1]
    return body, alloc, tail


def _parse_all(lines):
    """Every line parsed, or None when one does not parse."""
    items = []
    for ln in lines:
        p = parse(ln)
        if p is None:
            return None
        items.append(p)
    return items


def _seq_keys(n, ties):
    """Each line's `node[36]`.

    `node[36]` IS A SOURCE POSITION, not a counter, so a line's key is its
    own index EXCEPT where several lines come from ONE position.  Those are
    measured, one construct at a time, and `ties` names them:

      co_mul4/co_mix4   the four component writes of a construct  (seq 7/1)
      0038_int_ishl.frag     the four scalarised shifts                (seq 1)
      0038_op_div.vert       the four reciprocals AND the multiply     (seq 1)

    A local's component store is the counter-example that says this cannot
    be per vreg: `0052_lo_parts.vert` writes `v.<c>` and then self-copies the
    other three components into the SAME vreg, and the two carry seq 2 and 4
    -- distinct, and the order of the pair depends on it.

    Groups with a measured `seq` are applied last, over the plain ones."""
    seq = list(range(n))
    if not _os.environ.get("G2S_TIEORDER"):
        # A MEASURED POSITION WINS: a group whose `node[36]` is given (a
        # `Tie` with `seq`) is applied after the plain statement groups, so
        # a statement's group does not flatten the positions measured inside
        # it -- `0099_sa_b.frag`'s `txVec0 = vec4(..)` store shares the construct
        # writes' seq 4 while its gathers keep 6 and 8 (notes/99).
        ties = ([t for t in ties if getattr(t, "seq", None) is None]
                + [t for t in ties if getattr(t, "seq", None) is not None])
    for group in ties:
        g = [i for i in group if i < n]
        if not g:
            continue
        k = min(g)
        if getattr(group, "seq", None) is not None:
            k = group.seq
        for i in g:
            seq[i] = k
    return seq


def _list_order_edges(eitems, emitted, passthru, succ, pred):
    """Pass 2's edges for one block, swept over pass 1's LIST (notes/51 §8),
    replacing the block's creation-order ones.  PUSHED AT THE HEAD
    (0x4acb8): a producer's successor list is the REVERSE of the order the
    sweep made the edges in."""
    bs, _np, made = edges([eitems[i] for i in emitted],
                          frozenset(k for k, i in enumerate(emitted)
                                    if i in passthru), with_made=True)
    for i in emitted:
        succ[i] = []
        pred[i] = []
    for a, lst in enumerate(bs):
        for b in reversed(lst):
            succ[emitted[a]].append(emitted[b])
            pred[emitted[b]].append(emitted[a])
    # when each edge was made, by the block's own indices -> the global ones
    # (`_register_family_edges` places its edges among them)
    return dict(((emitted[a], emitted[b]), k) for (a, b), k in made.items())


def _chain(succ, pred, p_, i, made=None, pos=None):
    """A kind-1 edge p_ -> i, unless there is one.

    PUSHED AT THE HEAD, like every edge (`_EdgeBuilder.add`): with `made`
    (the block's edges and when the sweep made them) the edge is made in the
    forward sweep at `i`, after `i`'s reads -- key `(0, pos of i, 1)` -- and
    goes where that puts it in `p_`'s list, the latest-made first.
    `chr_skin_069bf064.vert`: `result.attrib[7]`'s list is `attrib[4].w`
    (t68 416) before `attrib[5].x` (400) -- both kind 1, after the kind-2
    edge to the local's copy (`tools/gsum.py`, node 47.21).  Without `made`
    it is appended, as before; `G2S_FAMILYAPPEND=1` forces that."""
    if p_ is None or i in succ[p_]:
        return
    pred[i].append(p_)
    if made is None:
        succ[p_].append(i)
        return
    key = (0, pos[i], 1)
    made[(p_, i)] = key
    lst = succ[p_]
    k = 0
    while k < len(lst) and made.get((p_, lst[k]), (2,)) >= key:
        k += 1
    lst.insert(k, i)


def _register_family_edges(items, emitted, succ, pred, made=None):
    """OUTPUTS THAT SHARE A REGISTER CODE ARE ONE REGISTER to the edge
    builder (notes/56): `f_710004ab80` places a fixed register by
    `record[16]` alone, and every colour output is code 207 with its index
    elsewhere, so two colour stores carry a kind-1 (write after write) edge
    in the order of the block's LIST -- pass 1's output, which is what
    `f_710004b220` sweeps.  `0044_fr_mrt.frag`: `color1` before `color0`.

    The same holds for the tessellation PATCH outputs (notes/63):
    `0007_ts_ctrl.tesc` chains `tessinner[0] -> tessouter[2] -> [1] -> [0]` with
    kind-1 edges in pass 1's list order, so the whole `result.patch` family
    is one register to the edge builder.

    EVERY VERTEX OUTPUT IS REGISTER CODE 111 (the stamps' row for
    `result.position` and `result.attrib[n]` alike), so the edge builder
    chains their writes the same way, per component: `0078_mb_n27.vert`'s block
    18 has the kind-1 edge `attrib[3].xyz` -> `attrib[2].xyz`, in pass 1's
    list order (t68 80 before 144), against creation order (seq 219 after
    213).  The compiler prints `attrib[3]` first.

    THE COLOUR FAMILY IS PER COMPONENT TOO (notes/104 §5): `0104_sw_a.frag`'s
    pass-1 list holds `result_color0` (xyzw), `result_color3.x` and
    `result_color2.w`, and the compiler's DAG (`tools/gsum.py`, row 207)
    has kind-1 edges color0 -> color3.x and color0 -> color2.w and NONE
    between `.x` and `.w`.  `G2S_COLOURWHOLE=1` chains every colour write
    whole again."""
    pos = dict((i, k) for k, i in enumerate(emitted))
    prev = {}
    last_vout = {}              # component -> the last vertex-output write
    last_col = {}               # component -> the last colour-output write
    for i in emitted:
        it = items[i]
        name = (it[1][0] if it is not None else "") or ""
        if _is_colour(name) and not _COLOUR_WHOLE:
            dm = it[1][1]
            for c in range(4):
                if dm & (1 << c):
                    _chain(succ, pred, last_col.get(c), i, made, pos)
                    last_col[c] = i
            continue
        if _is_colour(name):
            fam = "colour"
        elif name.startswith("result.patch."):
            fam = "patch"
        elif ((name.startswith("result.attrib[")
               or name == "result.position") and _VOUT_FAMILY):
            dm = it[1][1]
            for c in range(4):
                if dm & (1 << c):
                    _chain(succ, pred, last_vout.get(c), i, made, pos)
                    last_vout[c] = i
            continue
        else:
            continue
        _chain(succ, pred, prev.get(fam), i, made, pos)
        prev[fam] = i


def _pass2_dump(body, alloc, emitted, seq, t68, succ, got):
    """Diagnosis only (`G2S_P2DBG`): the block's pass-2 inputs and its
    order."""
    import sys as _sys
    for i in emitted:
        _sys.stderr.write("P2 %3d seq=%s t68=%s succ=%s  %s | %s\n" % (
            i, seq[i], t68[i], [s for s in succ[i] if s in emitted],
            body[i], (alloc[i] if alloc is not None else "")))
    _sys.stderr.write("P2 order %s\n" % got)


def _span_sizes_flat(lines, cuts=(), names=frozenset(), passthru=frozenset(),
                     calls=frozenset()):
    """The sizes of `order_pre`'s blocks, in order, or None.

    `order_pre` permutes WITHIN each block and keeps the blocks in place, so
    these sizes cut its output into the same blocks.  The trailing
    terminators are not counted (the caller gives them to the last block).
    """
    body, _alloc, _tail = _strip_terminators(lines)
    items = _parse_all(body)
    if items is None:
        return None
    return [len(s) for s in _block_spans(items, cuts, names,
                                         frozenset(passthru),
                                         frozenset(calls))]


# notes/78 §3; `G2S_NOVOUT=1` turns the vertex-output family off
_VOUT_FAMILY = not _os.environ.get("G2S_NOVOUT")
_COLOUR_WHOLE = bool(_os.environ.get("G2S_COLOURWHOLE"))
_FAMILYAPPEND = bool(_os.environ.get("G2S_FAMILYAPPEND"))
_WARFLAT = bool(_os.environ.get("G2S_WARFLAT"))
_WARLATER = bool(_os.environ.get("G2S_WARLATER"))
_NOWAROWN = bool(_os.environ.get("G2S_NOWAROWN"))


# CONTROL FLOW (notes/64).  A line that sets or tests a condition code, and
# the structure words themselves, are FIXED: the compiler's blocks end at
# them, and the scheduler never moves anything across them.  Each run of
# ordinary lines between two such lines is scheduled on its own.
_BARRIER_WORDS = (("REP", "ENDREP", "IF", "ELSE", "ENDIF", "BRK", "CONT",
                   "KIL", "EMIT", "ENDPRIM", "RET", "CAL")
                  if _os.environ.get("G2S_KILBARRIER") else
                  # A `KIL` IS A NODE OF ITS BLOCK, not a barrier of its own
                  # (notes/114 SS25): the compiler's block holds the `.CC`
                  # move and the `KIL` together and ENDS at the `KIL`, so
                  # the lowering cuts after it instead.
                  ("REP", "ENDREP", "IF", "ELSE", "ENDIF", "BRK", "CONT",
                   "EMIT", "ENDPRIM", "RET", "CAL"))


def _is_barrier(line):
    r"""`^\s*(REP|..|CAL)\b|^BB\S*:|\.CC\s` MATCHED (at the start: the
    `.CC` arm, unanchored in the text of the old pattern, was anchored by
    `re.match` all the same: it takes a line STARTING `.CC `, which the
    converter never writes -- kept as it was, the listings depend on it)."""
    if _lex.keyword_at(line, _BARRIER_WORDS):
        return True
    if line.startswith("BB"):
        i = 2
        while i < len(line) and line[i] not in _lex.WS:
            if line[i] == ":":
                return True
            i += 1
        return False
    return (line.startswith(".CC") and len(line) > 3
            and line[3] in _lex.WS)
# (a RET before the end, a CAL and a subroutine's label -- notes/68 -- end
# their blocks the same way)


def _segments(lines):
    """[(lo, hi, is_barrier)] covering the body before its trailing RETs."""
    n = len(lines)
    while n and _lex.keyword_at(lines[n - 1], ("RET",)):
        n -= 1
    out, lo = [], 0
    for i in range(n):
        if _is_barrier(lines[i]):
            if lo < i:
                out.append((lo, i, False))
            out.append((i, i + 1, True))
            lo = i + 1
    if lo < n:
        out.append((lo, n, False))
    return out, n


class Tie(list):
    """A group of lines sharing one `node[36]` whose position is not its
    first line's: `seq` says where the statement stood (notes/68 -- a
    function's entry copies come before its first statement although they
    are emitted when the block closes)."""
    seq = None


def _sub(idx, lo, hi):
    return [i - lo for i in idx if lo <= i < hi]


def order_pre(lines, allocated=None, cuts=(), ties=(), passthru=(),
              stage1=False, list_edges=False, names=frozenset(),
              calls=(), band=None):
    """`_order_pre_flat` over each run of ordinary lines between control
    lines (notes/64); the control lines stay where they are."""
    segs, n = _segments(lines)
    if not any(b for _lo, _hi, b in segs):
        return _order_pre_flat(lines, allocated, cuts, ties, passthru,
                               stage1, list_edges, names, calls, band)
    out = []
    for lo, hi, bar in segs:
        if bar:
            out.append(lo)
            continue
        sub_ties = []
        for t in ties:
            g = _sub(t, lo, hi)
            if not g:
                continue
            if getattr(t, "seq", None) is not None:
                g = Tie(g)
                g.seq = t.seq - lo
            sub_ties.append(g)
        sband = band
        if isinstance(band, tuple) and band and band[0] == "vkey":
            # the keys are line indices of the whole body
            sband = ("vkey", dict((v, (k[0] - lo,) + tuple(k[1:]))
                                  for v, k in band[1].items()))
        perm = _order_pre_flat(
            lines[lo:hi],
            allocated[lo:hi] if allocated is not None else None,
            _sub(cuts, lo, hi), sub_ties, _sub(passthru, lo, hi),
            stage1, list_edges, names, _sub(calls, lo, hi), sband)
        if perm is None:
            return None
        out.extend(lo + i for i in perm)
    return out + list(range(n, len(lines)))


def span_sizes(lines, cuts=(), names=frozenset(), passthru=(), calls=()):
    """The blocks' sizes in order, each control line a block of its own."""
    segs, n = _segments(lines)
    if not any(b for _lo, _hi, b in segs):
        return _span_sizes_flat(lines, cuts, names, passthru, calls)
    out = []
    for lo, hi, bar in segs:
        if bar:
            out.append(1)
            continue
        sz = _span_sizes_flat(lines[lo:hi], _sub(cuts, lo, hi), names,
                              _sub(passthru, lo, hi), _sub(calls, lo, hi))
        if sz is None:
            return None
        out.extend(sz)
    return out
