"""extinst.py -- GLSL.std.450 extended instructions.

notes/39: number -> cgc's name -> interned id -> f_7100f28a00's opcode ->
mnemonic, every step a table read out of the image or dumped whole from the
running compiler.

WHAT THE OPCODE DOES NOT SAY IS THE SHAPE, and that is measured rather than
assumed (tools/extshape.py, py/data/extinst_shape.json).  `g2s_trace_fold`
dumps the nodes the front end builds for a probe that calls exactly one
builtin, so the front end itself says whether the call became ONE
whole-vector instruction (one node, mask 0xffffffff), was scalarised (four
nodes, `.x` masks -- the transcendentals) or was inlined outright (several
unrelated opcodes -- `sign`).  The mnemonic and the suffix still come from
the image's tables, keyed by the measured opcode.  The measurement and
notes/39's chain agree wherever the chain answers at all: Floor 110 = 0x6e,
Ceil 101 = 0x65, FMin 142 = 0x8e, FMax 141 = 0x8d.

FClamp is lowered by the arithmetic arm (arith.py): both its results are
band temps (notes/52 section 8).

GLSL.std.450 Sqrt on a vector is lowered here as the measured thirteen-node
shape of `0033_un_sqrt.vert` (notes/60).
"""
import lex as _lex

from spvnames import GLSL450, Op

import sched as _sched
import opchain as _opchain
from glasmlib.common import NotEstablished, ENV
from glasmlib.types import _components, _glasm_type_code, _vi_is_scalar
from glasmlib.operands import _constant_source
from glasmlib.text import _COMPONENTS, _emit, _source
from glasmlib import nodes

_IDENTITY = (0, 1, 2, 3)
_MODIFIED = ("-", "|")
_ZERO4 = "{0, 0, 0, 0}"
# The one-instruction builtins whose constant operand prints second: the MIN
# and MAX nodes (0x8e / 0x8d), whatever their type, are in the canonicaliser
# f_710005f810's set (notes/102 §6; the builtin lowering writes the slots in
# argument order, `0102_mm_a.frag`, and that pass swaps them).
_CONSTANT_SECOND = (GLSL450.FMin, GLSL450.FMax, GLSL450.SMin, GLSL450.SMax,
                    GLSL450.UMin, GLSL450.UMax)
_ONE = "{1, 0, 0, 0}"

# The whole-vector shapes whose wiring is the function's own definition.
_DEFINED_SHAPES = (GLSL450.Normalize, GLSL450.FMix, GLSL450.Step,
                   GLSL450.FSign, GLSL450.Length, GLSL450.Distance)
# The rounding class the `step` and `sign` TRUNC belongs to (notes/23).
_ROUNDING = "0x6d"


def _is_scalar_op_shape(sh):
    """The measured shape of a scalarised builtin: ONE opcode four times,
    each with mask `0xff`."""
    if sh is None or len(sh["nodes"]) != 1:
        return False
    rec = next(iter(sh["nodes"].values()))
    return rec["count"] == 4 and rec["masks"] == ["0xff"]


def _unmeasured_shape(which, sh):
    if sh is None:
        return NotEstablished(
            "GLSL.std.450 %d: its shape has not been measured "
            "(tools/extshape.py has no probe that calls it alone)" % which)
    if (len(sh["nodes"]) == 1
            and next(iter(sh["nodes"].values()))["count"] == 1):
        # ONE instruction whose mnemonic the image's tables do not give.
        # 0x6c used to land here -- it is the default `<<name>>` arm in all
        # three namers -- until notes/23's rounding MODE was wired in
        # (notes/39); anything left is a class whose naming has not been
        # read.
        return NotEstablished(
            "GLSL.std.450 %d: one whole-vector instruction (opcode %s) whose "
            "mnemonic none of the image's tables spells"
            % (which, next(iter(sh["nodes"]))))
    return NotEstablished(
        "GLSL.std.450 %d: measured as %s, which is not one whole-vector "
        "instruction (py/data/extinst_shape.json)"
        % (which, " ".join("%s x%d" % (o, v["count"])
                           for o, v in sorted(sh["nodes"].items()))
           or "no instruction of its own"))


def _named(opcode, tc):
    m = _opchain.mnemonic_for_opcode(opcode, tc)
    return None if m is None or m.startswith("<") else m


class ExtInstOps(object):

    def _arm_extinst(self, ins):
        if ins.opcode != Op.OpExtInst:
            return False
        module = self.module
        self._computation()
        args = ins.args()
        which = args[1]
        ops = args[2:]
        _nres = _components(module, ins.result_type)
        forms = [self.values.get(a) or _constant_source(module, a, _nres)
                 for a in ops]
        if any(f is None for f in forms):
            raise NotEstablished("an ext-inst operand with no form")
        if which == GLSL450.FAbs and len(forms) == 1:
            self._absolute(ins, ops, forms)
            return True
        _single = _opchain.extinst_single(
            which, _glasm_type_code(module, ins.result_type))
        if _single is not None and len(forms) in (1, 2):
            self._single(ins, _single, forms)
            return True
        _sh = _opchain.extinst_shape(which)
        for _shape in (self._one_component_scalar_op, self._four_lane_scalar,
                       self._defined_shape, self._sqrt, self._cross):
            if _shape(ins, which, forms, _sh):
                return True
        raise _unmeasured_shape(which, _sh)

    # -- FAbs -------------------------------------------------------------

    def _absolute(self, ins, ops, forms):
        """FAbs BUILDS NO NODE AT ALL -- tools/extshape.py measures it as
        having no instruction of its own -- because it is the operand slot's
        MODIFIER 2, the same field the negate uses with 1 (notes/47).

        ... BUT THE VALUE IS A NAMED TEMP, exactly as the negate's (notes/66
        §3, notes/67 §9, notes/87): `t = abs(x)` is a statement whose carrier
        MOV takes the modified operand, and a carrier folds into its source
        only when the read has no modifier -- so every reader reads the MOV.
        The cut `0000_mq_n4.frag`'s `fract(abs(v.xy))` prints `MOV.F R16.xy, |R3|;
        FRC.F32 R17.xy, R16;`, and no listing, probe or corpus, has `|..|` on
        anything but a MOV."""
        module = self.module
        if forms[0].startswith(_MODIFIED):
            # The modifier is ONE field, so a value cannot carry two.  What
            # the front end does with `abs(-x)` or `-abs(x)` has not been
            # measured, and guessing would put a wrong character in the
            # listing.
            raise NotEstablished(
                "an absolute value of an already modified operand: the "
                "slot's modifier is one field (notes/47) and which value it "
                "takes here is not measured")
        if ENV.get("G2S_NOABSMOV"):
            self.values[ins.result] = "|%s|" % forms[0]
            if ops[0] in self.comps:
                self.comps[ins.result] = self.comps[ops[0]]
            return
        _nres = _components(module, ins.result_type)
        _mv = _named(nodes.MOV, _glasm_type_code(module, ins.result_type))
        _ds = _opchain.dest_suffix(_nres) if _nres else None
        if _mv is None or _ds is None:
            raise NotEstablished("an absolute value whose carrier MOV or mask "
                                 "the image does not give")
        self._computation()
        # THE CARRIER CARRIES ITS SOURCE NODE'S `node[36]`: the modifier is
        # set on the operand slot of the node it reads, and `0000_mq_n4.frag`'s
        # `MOV.F R16.xy, |R3|;` is seq 16, the MUL's -- the read of
        # `u_xlat0` forwarded to the MUL stored into it in this block
        # (`tools/nodedump.py`)
        _orig = self.fwd_of.get(ops[0], ops[0])
        _mseq = self._merge_node_line(ops[0])
        if _mseq is not None:
            # the node read is a MERGE PAIR made in this block: the carrier
            # is stamped with the pair's statement (`0101_fa_b.frag`:
            # `u_xlat0.xy = ..` then, after another statement, `abs(
            # u_xlat0.xy)` -- `MOV.F R3.xy, |R2|;` is seq 11, the pair's
            # pass-through, ahead of the statement between)
            # ... ONE `node[36]` with the pair: when the pair's halves share
            # one (the tie that holds the pass-through), the carrier joins
            # that tie (`0108_pw_a.frag`: pass-through, carrier and lane write all
            # seq 14)
            _pg = (None if ENV.get("G2S_ABSPAIRSEQ") else
                   next((_t for _t in self.ties if _mseq in _t
                         and getattr(_t, "seq", None) is None
                         and len(_t) > 1), None))
            if _pg is not None:
                _pg.append(len(self.lines))
            else:
                _tg = _sched.Tie()
                _tg.seq = _mseq
                _tg.append(len(self.lines))
                self.ties.append(_tg)
        elif (self.defline.get(_orig) is not None
                and self.defblk.get(_orig) == self.blk_no
                and not ENV.get("G2S_NOABSSEQ")):
            _tg = _sched.Tie()
            _tg.seq = self.defline[_orig]
            _nm = self._name_read_key(ops[0])
            if _nm is not None and not ENV.get("G2S_NONAMEREADSEQ"):
                # A READ OF A NAME is ONE node per block, made at the block's
                # first read of it: `0108_ac_a.frag`'s `abs(u_xlat3.x)` in two
                # statements of one block are both seq 16 -- the first
                # statement's -- ahead of that statement's MUL (18); the
                # third, after `u_xlat31`'s second store opened a block, is
                # 27 (`tools/nodedump.py`; the corpus's `chr_hair_f0ad47e1`)
                _tg.seq = self.name_read_seq.setdefault(_nm, _tg.seq)
            _tg.append(len(self.lines))
            self.ties.append(_tg)
        # THE OPERAND IS PRINTED WITH ITS SWIZZLE, as every other arm prints
        # its sources (`_source`): `forms` is built from `self.values` alone,
        # so a value whose lane lives in `self.comps` -- a load of a stored
        # local component, `abs(u_xlat1.z)` in `0105_ab_a.frag` -- lost it and
        # printed `|R4|` for the oracle's `|R4.z|`
        _opnd = (_source(self.values, self.comps, ops[0], _nres)
                 if ops[0] in self.values and not ENV.get("G2S_NOABSCOMP")
                 else forms[0])
        dst = self._fresh(True)
        self.lines.append(_emit(_mv, dst + _ds, "|%s|" % _opnd))
        self.values[ins.result] = dst
        self.negated.add(ins.result)

    def _name_read_key(self, vid):
        """(block, the local's register) when `vid` is a component load
        that reads the local's NAME (its register, not a forwarded value),
        else None."""
        _cl = self.comp_load.get(vid)
        if _cl is not None:
            _var = _cl[0][0]
        else:
            # A WHOLE LOAD READS THE SAME NAME.  The rule above was measured
            # on a COMPONENT load (`0108_ac_a.frag`'s `abs(u_xlat3.x)`), but what
            # it is about is the READ OF THE NAME, not the lane: the corpus's
            # `chr_hair_e9f8d2b0.frag` has four `abs()` of one local's whole
            # load in one block, and the compiler's `stamps` gives the first
            # two ONE `node[36]` (1657) where ours took each line's own --
            # which is the pick the block differs on (SS76).
            # `G2S_NOWHOLENAMEREAD=1` off.
            if ENV.get("G2S_NOWHOLENAMEREAD"):
                return None
            _d = self.module.result_insn.get(vid)
            if _d is None or _d.opcode != Op.OpLoad:
                return None
            _var = _d.args()[0]
        _reg = self.local_reg.get(_var)
        if _reg is None or self.values.get(vid) != _reg:
            return None
        return (self._bkey(), _reg)

    def _merge_node_line(self, vid):
        """The pass-through line of the MERGE PAIR `vid` reads, or None.

        `vid` must read the merged local's node itself: its whole load, or a
        shuffle of that load taking the lanes in place (`u_xlat0.xy` after
        `u_xlat0.xy = ..`).  A swizzle that moves lanes is a node of its own
        and is not measured here.  `G2S_NOABSMERGESEQ=1` turns it off."""
        if ENV.get("G2S_NOABSMERGESEQ"):
            return None
        _ld = vid
        _d = self.module.result_insn.get(vid)
        if (_d is not None and _d.opcode == Op.OpVectorShuffle
                and _d.args()[0] == _d.args()[1]):
            _sel = list(_d.args()[2:])
            _lvs = self.load_of.get(_d.args()[0])
            if (len(set(_sel)) == 1 and _lvs is not None
                    and not ENV.get("G2S_NOABSSPLATSEQ")
                    and self.cfw_kind.get((_lvs[0], _sel[0])) == "merge"
                    and self.cfw.get(_lvs[0], {}).get(
                        _sel[0], (None,))[0] == self._bkey()):
                # A SPLAT OF A LANE A PAIR STORED IN THIS BLOCK reads the
                # pair's node, as a forwarded component read does: the
                # slice's `chr_skin_cb42fbab`, `u_xlat6.x = cos(..);
                # abs(u_xlat6.xx)` prints `MOV.F R20.yzw, R20; COS ..;
                # MOV.F R4.xy, |R3.x|; MOV.F R20.x, R3;`
                _cp = self.cpair.get(_lvs[0])
                if _cp is not None and _cp[0] == self._bkey():
                    return _cp[2]
            if _sel != list(range(len(_sel))):
                return None
            _ld = _d.args()[0]
        _lv = self.load_of.get(_ld)
        _cl = self.comp_load.get(_ld)
        if (_lv is None and _cl is not None
                and not ENV.get("G2S_NOABSCOMPSEQ")
                and self.values.get(_ld) == self.local_reg.get(_cl[0][0])):
            # A COMPONENT READ OF THE MERGED LOCAL is a select on the pair's
            # node too: `0105_ab_a.frag`'s `abs(u_xlat1.y)` and `abs(u_xlat1.z)`
            # after the lane stores both print seq 21, the last pair's
            # pass-through `MOV.F R4.xyz, R4;` (`tools/nodedump.py`).  A read
            # forwarded to the stored value (`cfw`) reads another node.
            _lv = (_cl[0][0], None)
        elif (_lv is None and _cl is not None
                and not ENV.get("G2S_NOABSFWDSEQ")
                and self.cfw_kind.get(tuple(_cl[0])) == "merge"
                and self.cfw.get(_cl[0][0], {}).get(_cl[0][1], (None,))[0]
                == self._bkey()):
            # ... AND SO IS A READ FORWARDED FROM A PAIR'S LANE: `0108_pw_a.frag`'s
            # `u_xlat7.x = u_xlat19.x * u_xlat7.x; .. log2(abs(u_xlat7.x))`
            # prints `MOV.F R2.x, |R1|;` -- the MUL's register -- at seq 14,
            # the pair's (`MOV.F R4.yzw, R4;` and `MOV.F R4.x, R1;`), not the
            # MUL's 12 (`tools/nodedump.py`; the corpus's `chr_cloth_a503f755`
            # and eight more).  A lane stored as the node itself (lane x, no
            # other lane stored) is still the value's.
            _lv = (_cl[0][0], None)
        if _lv is None:
            return None
        _cp = self.cpair.get(_lv[0])
        if _cp is None or _cp[0] != self._bkey():
            return None
        return _cp[2]

    # -- one whole-vector instruction ---------------------------------------

    def _single(self, ins, _single, forms):
        """THE WRITE MASK IS THE RESULT'S COMPONENT COUNT (notes/41), as for
        every other instruction: `0067_sc_min.frag` prints `MIN.F R0.x,
        fragment.attrib[0], {1, 0, 0, 0};` for a scalar min.  A vec4 gives
        the empty suffix.

        EACH OPERAND WITH ITS SELECTOR, as a binary op's: the raw form drops
        a swizzle -- `0000_bl_277.vert`'s `max(u_xlat1.xyw, vec3(0.0))` prints
        `MAX.F R1.xyz, R36.xyww, {0, 0, 0, 0};` -- and a one-component value
        read by a wider op broadcasts through `.x`."""
        module = self.module
        _nres = _components(module, ins.result_type)
        _dsx = _opchain.dest_suffix(_nres)
        if _dsx is None:
            raise NotEstablished(
                "a builtin whose write mask the image's rule does not give")
        _sforms = []
        for _a, _f in zip(ins.args()[2:], forms):
            _t = (_source(self.values, self.comps, _a, _nres)
                  or _constant_source(module, _a, _nres) or _f)
            if (_nres and _nres > 1 and _a not in self.comps
                    and _vi_is_scalar(module, _a)
                    and _lex.is_broadcastable(_t, braces=False)):
                _t = _t + ".x"
            _sforms.append(_t)
        if ENV.get("G2S_EXTRAWFORMS"):
            _sforms = forms
        elif (ins.args()[1] in _CONSTANT_SECOND and len(_sforms) == 2
              and _sforms[0].startswith("{")
              and not _sforms[1].startswith("{")
              and not ENV.get("G2S_NOCONSTSECOND")):
            # A CONSTANT GOES SECOND in the commutative MIN and MAX, as in the
            # clamp's pair (notes/90 §2): `0102_mm_a.frag`'s `max(1.0, a.x)`
            # prints `MAX.F R1.x, fragment.attrib[0], {1, 0, 0, 0};`, a vec2
            # `max(vec2(..), a.zw)` the same
            _sforms.reverse()
        dst = self._fresh(True)
        self.lines.append("%s %s%s, %s;" % (_single, dst, _dsx,
                                            ", ".join(_sforms)))
        self.values[ins.result] = dst

    # -- scalarised builtins ------------------------------------------------

    def _one_component_scalar_op(self, ins, which, forms, _sh):
        """ONE COMPONENT OF A SCALAR-OP BUILTIN (notes/67).  The
        per-component expansion is f_7100060110's: it splits a scalar op
        (COS EX2 LG2 RCP RSQ SIN, `nodes.SPLIT_SCALAR_OPS`) only when its mask
        selects two or more distinct source components (0x60270), so here the
        call is ONE instruction and there is nothing to assemble.  Its operand
        prints its component letter, as every scalar op's does (notes/53
        §10):
            EX2.F32 R0.x, fragment.attrib[0].x;
        Sqrt is the family's `1 / rsqrt(x)` (notes/60), whose divide is the
        one-component DIV (`_scalar_divide`):
            RSQ.F32 R0.x, fragment.attrib[0].x;
            DIV.F32 R1.x, {1, 0, 0, 0}, R0.x;
        """
        module = self.module
        _sqrt = which == GLSL450.Sqrt
        if _components(module, ins.result_type) != 1 or not (
                _sqrt or _is_scalar_op_shape(_sh)):
            return False
        _ops = ins.args()[2:]
        if len(_ops) != len(forms) or len(forms) not in (1, 2) \
                or (_sqrt and len(forms) != 1) \
                or any(f.startswith("{") or _lex.has_swizzle_suffix(f)
                       for f in forms):
            raise NotEstablished(
                "GLSL.std.450 %d on one component: an operand form not "
                "measured" % which)
        _args1 = ["%s.%s" % (f, _COMPONENTS[self.comps.get(a, _IDENTITY)[0]])
                  for f, a in zip(forms, _ops)]
        _tc = _glasm_type_code(module, ins.result_type)
        _op1 = nodes.RSQ if _sqrt else int(next(iter(_sh["nodes"])), 0)
        if _op1 not in nodes.SPLIT_SCALAR_OPS:
            # POW (0x93) is not in the set, and its shape is someone else's
            raise NotEstablished(
                "GLSL.std.450 %d on one component: opcode %#x is not one "
                "f_7100060110 splits" % (which, _op1))
        _mn = _named(_op1, _tc)
        if _mn is None:
            raise NotEstablished(
                "GLSL.std.450 %d: the image's tables do not name opcode %#x "
                "for this type" % (which, _op1))
        dst = self._fresh(True)
        self.lines.append(_emit(_mn, "%s.x" % dst, *_args1))
        if _sqrt:
            _t = dst
            dst = self._fresh(True)
            self.lines.append(self._scalar_divide(
                ins.result_type, dst, _ONE, "%s.x" % _t))
        self.values[ins.result] = dst
        return True

    def _merge_lane_source(self, a, c):
        """(operand, component) a scalarised op's lane `c` reads when operand
        `a` is a whole read (or a same-operand shuffle of one) of a local
        whose LAST store in this block is a merge pair writing that lane:
        the lane is the pair's written value itself, the other lanes the
        pass-through -- the local's register.  `map_16f1036a-1`'s
        `u_xlat10.x = dot(..); u_xlat10.y = dot(..); sin(u_xlat10.xy)` prints
        `SIN.F32 R2.x, R0.x;` (lane y: the second DP2) and `SIN.F32 R1.x,
        R10.x;` (lane x: the name); the three `sin` vertex files' lane z reads
        the ADD, `SIN.F32 R3.x, R2.z;`, ahead of the pair's own gather.  None
        otherwise.  `G2S_NOMERGELANE=1` turns it off."""
        if ENV.get("G2S_NOMERGELANE"):
            return None
        module = self.module
        _i = module.result_insn.get(a)
        base, comp = a, self.comps.get(a, (0, 1, 2, 3))[c]
        if (_i is not None and _i.opcode == Op.OpVectorShuffle
                and _i.args()[0] == _i.args()[1]):
            base = _i.args()[0]
            _sel = _i.args()[2:]
            if c >= len(_sel):
                return None
            comp = _sel[c]
        _lw = self.load_of.get(base)
        if _lw is None:
            return None
        var = _lw[0]
        bk = self._bkey()
        _cp = self.cpair.get(var)
        if _cp is None or _cp[0] != bk:
            return None
        _wp = _sched.parse(self.lines[_cp[1]])
        if _wp is None or not (_wp[1][1] >> comp) & 1:
            return None
        _f = self.cfw.get(var, {}).get(comp)
        if _f is None or _f[0] != bk or not _f[1].startswith("#"):
            return None
        return _f[1], _f[2]

    def _four_lane_scalar(self, ins, which, forms, _sh):
        """SCALARISED, THEN ASSEMBLED.  `un_cos`, `un_sin`, `un_exp2`,
        `un_log2`, `un_rsq` and `op_pow` each measure as ONE opcode four times
        with mask `0xff` -- one instruction per component, each into a vreg
        of its OWN -- followed by the composite construct's four writes.  The
        `0x47`s are not this arm's to emit: `_assemble` is the construct,
        read from `0053_co_mul4.vert`, and the store then forwards component 0
        exactly as it does there.

        The ORDER is the scheduler's: the four carry ONE `node[36]`, so they
        are created ASCENDING and pass 1 reverses them.  An operand that
        already carries a swizzle would need the two selectors composed,
        which has not been measured, so only the plain shape is emitted."""
        module = self.module
        _n = _components(module, ins.result_type)
        # FEWER THAN FOUR LANES, and operands read through a selector, are
        # the same shape, one instruction per lane at the lane's component:
        # `0109_sn_b.frag`'s `sin(u_xlat2.xyz)` prints `SIN.F32 R0.x, R3.x; ..
        # R3.y; .. R3.z;` and the three-lane construct, `0109_sn_c.frag`'s
        # `sin(a.zw)` `SIN.F32 .., fragment.attrib[0].z` / `.w`
        # THE DEFAULT since notes/109 §5: `sn_b`'s copy register was the
        # flushed name's band record, and the corpus's four `sin` files
        # needed the merge-lane read (`_merge_lane_source`) and the
        # divisor's shared reciprocal (arith.py).  The construct is the
        # value's BAND temp (notes/110 §4); `G2S_NOSCALARWIDE=1` refuses
        _wide = (not ENV.get("G2S_NOSCALARWIDE") and _n is not None
                 and 2 <= _n <= 4)
        if not (_sh is not None and len(_sh["nodes"]) == 1
                and (_n == 4 or _wide)
                and not any(f.startswith(("{",) + _MODIFIED)
                            or (_lex.has_swizzle_suffix(f) and not _wide)
                            for f in forms)
                and _is_scalar_op_shape(_sh)):
            return False
        if _wide and any(_lex.has_swizzle_suffix(f) for f in forms):
            return False                # a selector in the text: not measured
        _op1 = next(iter(_sh["nodes"]))
        _tc = _glasm_type_code(module, ins.result_type)
        _mn = _named(int(_op1, 0), _tc)
        _mv = _named(nodes.MOV, _tc)
        if _mn is None or _mv is None:
            raise NotEstablished(
                "GLSL.std.450 %d: the image's tables do not name opcode %s "
                "for this type" % (which, _op1))
        _parts, _tie = [], []
        _ids = ins.args()[2:]
        for _c in range(_n):
            _t = self._fresh(True)
            _tie.append(len(self.lines))
            _ops = []
            for f, _a in zip(forms, _ids):
                _ml = self._merge_lane_source(_a, _c)
                if _ml is not None:
                    _ops.append("%s.%s" % (_ml[0], _COMPONENTS[_ml[1]]))
                else:
                    _ops.append("%s.%s" % (f, _COMPONENTS[
                        self.comps.get(_a, (0, 1, 2, 3))[_c]]))
            self.lines.append(_emit(_mn, "%s.x" % _t, *_ops))
            _parts.append((_t, 0))
        if ENV.get("G2S_SCALARTIE"):
            # the old reading, one `node[36]` for all lanes; `tools/nodedump`
            # on `sn_a`, `sn_b` gives each lane its own (2, 3, 4, 5)
            self.ties.append(_tie)
        elif not ENV.get("G2S_SCALARGROUP"):
            # ... and the value's statement -- the construct, its store and
            # flushes -- starts at the construct (`0109_sn_b.frag`: the SINs 4, 5,
            # 6, the construct's writes and `t`'s stores 7), as `fwidth`'s
            # does at its ADD
            self.defline[ins.result] = len(self.lines)
        # THE CONSTRUCT IS THE VALUE'S BAND TEMP (notes/110 §4): the
        # compiler numbers it with the front end's names, after the lanes
        # it assembles and before the next value's -- `0109_sn_c.frag`'s
        # `vec4(sin(a.zw), cos(a.yx))` has the liveset seed `1 .. 8` with
        # the SIN pair record 4 (after the SINs' 2, 3) and the COS pair 7
        # (after 5, 6), both live out of the block.  `sy_a`, `sy_d` need it.
        # `G2S_SCALARCONNOBAND=1` makes it a lowering temp again.
        dst, _text = self._assemble(
            _parts, _mv, is_band=not ENV.get("G2S_SCALARCONNOBAND"))
        self.head_src[ins.result] = _text
        self.values[ins.result] = dst
        return True

    # -- shapes wired by the function's definition ----------------------------

    def _defined_shape(self, ins, which, forms, _sh):
        """TWO WHOLE-VECTOR SHAPES WHOSE WIRING IS THE FUNCTION'S OWN
        DEFINITION.  The opcodes and their masks are measured; what feeds
        what is not a choice:

          normalize(v) = v * inversesqrt(dot(v, v))
              0x8a x1 mask 0xff, 0x7c x1 mask 0xff, 0x90 x1 full
          mix(x, y, a)  = x + (y - x) * a
              0x83 x2 full, 0x90 x1 full

        Each takes a REGISTER of its own -- `un_normalize` uses R0, R1, R2
        for the three -- so every temp is a band temp."""
        module = self.module
        _scalar_result = which in (GLSL450.Length, GLSL450.Distance)
        if not (which in _DEFINED_SHAPES and _sh is not None
                and (_scalar_result
                     or _components(module, ins.result_type) == 4)
                and not any(f.startswith(_MODIFIED)
                            or _lex.has_swizzle_suffix(f) for f in forms)):
            return False
        _tc = _glasm_type_code(module, ins.result_type)
        _mul = _opchain.mnemonic_for_opcode(nodes.MUL, _tc)
        _add = _opchain.mnemonic_for_opcode(nodes.ADD, _tc)
        _rsq = _opchain.mnemonic_for_opcode(nodes.RSQ, _tc)
        _dp = _opchain.dot_mnemonic(4, _tc)
        if any(m is None or m.startswith("<")
               for m in (_mul, _add, _rsq, _dp)):
            raise NotEstablished(
                "GLSL.std.450 %d: the image's tables do not name one of its "
                "opcodes for this type" % which)
        if which == GLSL450.Length and len(forms) == 1:
            self._length(ins, forms[0], _tc, _rsq)
            return True
        if which == GLSL450.Distance and len(forms) == 2:
            self._distance(ins, forms, _tc, _add, _rsq)
            return True
        if which == GLSL450.Normalize and len(forms) == 1:
            self._normalize(ins, forms[0], _dp, _rsq, _mul)
            return True
        _cmp = self._compare_family(which, _sh, _tc)
        if which == GLSL450.Step and len(forms) == 2:
            self._step(ins, forms, _cmp)
            return True
        if which == GLSL450.FSign and len(forms) == 1:
            self._sign(ins, forms[0], _cmp, _add)
            return True
        if which == GLSL450.FMix and len(forms) == 3:
            self._mix(ins, forms, _add, _mul)
            return True
        return False

    def _length(self, ins, v, _tc, _rsq, _sub=None):
        """`length(v)` IS INLINED as a dot, a reciprocal square root and a
        RECIPROCAL (notes/114 \u00a749).  `extshape.py` measures 0x89 (`DP3`),
        0x7c (`RSQ`) and 0x87 (`DIV`), each mask `.x`, and `probes/0108_ds_b.vert`
        prints them:

            DP3.F32 R0.x, vertex.attrib[0], vertex.attrib[0];
            RSQ.F32 R1.x, R0.x;
            DIV.F32 R2.x, {1, 0, 0, 0}, R1.x;

        so the square root is `1 / rsqrt(x)`, not a `SQRT` -- the same
        inlining `normalize` gets, with the reciprocal in place of the
        multiply.  The dot is the OPERAND's width, not the result's, which is
        one component."""
        module = self.module
        _w = self._length_width(ins)
        _dp = _opchain.dot_mnemonic(_w, _tc)
        _div = _opchain.mnemonic_for_opcode(nodes.DIV, _tc)
        if any(m is None or m.startswith("<") for m in (_dp, _div)):
            raise NotEstablished("a length the image's tables do not name "
                                 "for this type")
        _t1 = self._fresh(True)
        self.lines.append(_emit(_dp, "%s.x" % _t1, v, v))
        _t2 = self._fresh(True)
        self.lines.append(_emit(_rsq, "%s.x" % _t2, "%s.x" % _t1))
        dst = self._fresh(True)
        self.lines.append(_emit(_div, "%s.x" % dst, "{1, 0, 0, 0}",
                                "%s.x" % _t2))
        self.values[ins.result] = dst
        self.comps[ins.result] = (0, 0, 0, 0)

    def _distance(self, ins, forms, _tc, _add, _rsq):
        """`distance(a, b)` IS `length(b - a)` (notes/114 \u00a749).
        `extshape.py` measures exactly `length`'s three nodes plus one 0x83
        (`ADD`) of the OPERAND's width, and `probes/0108_ds_a.vert` prints the
        subtraction with the FIRST operand negated:

            ADD.F32 R4.xyz, vertex.attrib[1], -vertex.attrib[0];
        """
        _w = self._length_width(ins)
        _t0 = self._fresh(True)
        self.lines.append(_emit(_add, _t0 + _opchain.dest_suffix(_w),
                                forms[1], "-" + forms[0]))
        self._length(ins, _t0, _tc, _rsq)

    def _length_width(self, ins):
        """The width of `length`/`distance`'s OPERAND -- the dot's width.
        The result is one component, so the result type cannot give it."""
        module = self.module
        _a = ins.args()[2]
        _d = module.result_insn.get(_a) or module.constants.get(_a)
        _w = _components(module, _d.result_type) if (
            _d is not None and _d.has_result_type) else None
        if not _w or not 2 <= _w <= 4:
            raise NotEstablished("a length whose operand is not a vector")
        return _w

    def _normalize(self, ins, v, _dp, _rsq, _mul):
        _t1 = self._fresh(True)
        self.lines.append(_emit(_dp, "%s.x" % _t1, v, v))
        _t2 = self._fresh(True)
        self.lines.append(_emit(_rsq, "%s.x" % _t2, "%s.x" % _t1))
        dst = self._fresh(True)
        self.lines.append(_emit(_mul, dst, "%s.x" % _t2, v))
        self.values[ins.result] = dst

    def _compare_family(self, which, _sh, _tc):
        """THE ROUNDING FAMILY NAMES ITSELF FROM ITS MODE.  `extshape.py`
        records that mode for EVERY 0x6c/0x6d node, so `step` and `sign` can
        spell their TRUNC the same way a single-instruction shape does
        (notes/23).  The type code for the TRUNC/I2F pair is an UNSIGNED one
        -- the listing prints `.U` -- and 10, 12 and 14 all spell it that
        way, so which of them the front end used does not reach the text.

        Returns (trunc, i2f, sge, sgt, slt); the trunc is None outside `step`
        and `sign`, which are the only readers."""
        _trn = (_sh["nodes"].get(_ROUNDING) or {}).get("mnemonic")
        _suf = _opchain.suffix(nodes.TRUNC_FAMILY, nodes.U64)
        _i2f = _opchain.mnemonic_for_opcode(nodes.I2F, nodes.U64)
        _sge = _opchain.mnemonic_for_opcode(nodes.SGE, _tc)
        _sgt = _opchain.mnemonic_for_opcode(nodes.SGT, _tc)
        _slt = _opchain.mnemonic_for_opcode(nodes.SLT, _tc)
        if which in (GLSL450.Step, GLSL450.FSign):
            if not _trn or None in (_suf, _i2f, _sge, _sgt, _slt):
                raise NotEstablished(
                    "GLSL.std.450 %d: its rounding mode or one of its "
                    "opcodes has no spelling" % which)
            _trn = _trn + _suf
        return _trn, _i2f, _sge, _sgt, _slt

    def _step(self, ins, forms, _cmp):
        """step(edge, x) = x >= edge, truncated to 0 or 1.  ONE band temp,
        written three times (`0031_op_step.vert` is `1 R-regs`)."""
        _trn, _i2f, _sge = _cmp[0], _cmp[1], _cmp[2]
        dst = self._fresh(True)
        self.lines.append(_emit(_sge, dst, forms[1], forms[0]))
        self.lines.append(_emit(_trn, dst, dst))
        self.lines.append(_emit(_i2f, dst, dst))
        self.values[ins.result] = dst

    def _sign(self, ins, v, _cmp, _add):
        """sign(v) = (v > 0) - (v < 0), each side compared, truncated and
        converted.  TWO band temps, and the subtract reuses the NEGATIVE
        side's (`0039_un_sign.vert` is `2 R-regs` and its `ADD` writes `R1`).

        THE POSITIVE SIDE TAKES THE LOWER REGISTER: the listing gives it `R0`
        and the negative side `R1`, so its vreg is the earlier one even
        though the negative side's instruction is emitted first."""
        _trn, _i2f, _sgt, _slt = _cmp[0], _cmp[1], _cmp[3], _cmp[4]
        _pos = self._fresh(True)
        _neg = self._fresh(True)
        for _mn, _t in ((_slt, _neg), (_sgt, _pos)):
            self.lines.append(_emit(_mn, _t, v, _ZERO4))
            self.lines.append(_emit(_trn, _t, _t))
            self.lines.append(_emit(_i2f, _t, _t))
        self.lines.append(_emit(_add, _neg, _pos, "-%s" % _neg))
        self.values[ins.result] = _neg

    def _mix(self, ins, forms, _add, _mul):
        """ONE band temp, written three times: `0114_op_mix.vert` is `1 R-regs`,
        and `tools/bandsize.py` measures `normalize` at 3 against this
        shape's 1.  So the three instructions are one vreg, not three."""
        dst = self._fresh(True)
        self.lines.append(_emit(_add, dst, forms[1], "-%s" % forms[0]))
        self.lines.append(_emit(_mul, dst, dst, forms[2]))
        self.lines.append(_emit(_add, dst, forms[0], dst))
        self.values[ins.result] = dst

    # -- sqrt and cross -------------------------------------------------------

    def _sqrt(self, ins, which, forms, _sh):
        """SQRT (notes/60), from `0033_un_sqrt.vert`'s nodes: the transcendental
        family's shape for inversesqrt, then a reciprocal per component and a
        multiply by one --
          RSQ t_c.x, a.c        x4  names, seq 2,3,4,5
          MOV v.c, t_c          x4  the construct, a name, seq 6
          RCP r.c, t_c.x        x4  ONE lowering vreg, seq 7
          MUL m, r, {1}.x       x1  a name, seq 7 with the RCPs
        The RCPs read the RSQ results directly; the construct is a stored
        name, so it is emitted although nothing reads it.

        WIDTH 2 IS THE SAME SHAPE, read off `compute_skydome-1.comp` rather
        than assumed from the vec4 one (notes/126 §1) -- two RSQs, a
        two-lane construct, two RCPs into ONE vreg, and the multiply by one
        under the narrow mask:

            RSQ.F32 R2.x, R0.y;   RSQ.F32 R0.x, R8.x;
            MOV.F R12.y, R2.x;    MOV.F R12.x, R0;
            RCP.F32 R3.y, R2.x;   RCP.F32 R3.x, R0.x;
            MUL.F32 R11.xy, R3, {1, 0, 0, 0}.x;

        WIDTH 3 IS THE SAME AGAIN, read off `post_taa_resolve.frag`:

            RSQ.F32 R0.x, R4.x;  RSQ.F32 R1.x, R4.y;  RSQ.F32 R2.x, R4.z;
            RCP.F32 R6.z, R2.x;  RCP.F32 R6.y, R1.x;  RCP.F32 R6.x, R0.x;
            MUL.F32 R7.xyz, R6, {1, 0, 0, 0}.x;

        Width 1 has an arm of its own (`RSQ` then a one-component `DIV`),
        so every width is now read -- each where it was SEEN, which is the
        point `0114_mx_v3.vert` made for `mix`: a narrower width is not
        always the wide shape with a smaller mask (notes/114 §50)."""
        if which != GLSL450.Sqrt:
            return False
        module = self.module
        _tc = _glasm_type_code(module, ins.result_type)
        _rsq = _opchain.mnemonic_for_opcode(nodes.RSQ, _tc)
        _rcp = _opchain.mnemonic_for_opcode(nodes.RCP, _tc)
        _mul = _opchain.mnemonic_for_opcode(nodes.MUL, _tc)
        _mv = _opchain.mnemonic_for_opcode(nodes.MOV, _tc)
        _n = _components(module, ins.result_type)
        _ds = _opchain.dest_suffix(_n) if _n else None
        if (len(forms) != 1
                or _n not in (2, 3, 4) or _ds is None
                or forms[0].startswith(("{",) + _MODIFIED)
                or _lex.has_swizzle_suffix(forms[0])
                or any(m is None or m.startswith("<")
                       for m in (_rsq, _rcp, _mul, _mv))):
            if ENV.get("G2S_SQDBG"):
                import sys as _s
                print("SQDBG n=%r ds=%r forms=%r swz=%r comps=%r mns=%r" % (
                    _n, _ds, forms,
                    _lex.has_swizzle_suffix(forms[0]) if forms else None,
                    self.comps.get(ins.args()[2]),
                    [_rsq, _rcp, _mul, _mv]), file=_s.stderr)
            raise NotEstablished(
                "GLSL.std.450 31 (sqrt) on an operand other than a plain "
                "vec2, vec3 or vec4: not measured")
        # EACH LANE READS ITS OWN SOURCE, through the same two steps every
        # other scalarised op in this file uses (`_scalar_wide`): the merge
        # pair's written value where there is one, else the operand's
        # component map.  `compute_skydome-1.comp`'s two RSQs read `R0.y`
        # and `R8.x` -- two different registers for one vec2 operand, which
        # is that mechanism and not a swizzle of one name.
        _ids = ins.args()[2:]
        _ts = []
        for _c in range(_n):
            _t = self._fresh(True)
            _ml = self._merge_lane_source(_ids[0], _c)
            if _ml is not None:
                _src = "%s.%s" % (_ml[0], _COMPONENTS[_ml[1]])
            else:
                _src = "%s.%s" % (forms[0], _COMPONENTS[
                    self.comps.get(_ids[0], _IDENTITY)[_c]])
            self.lines.append(_emit(_rsq, "%s.x" % _t, _src))
            _ts.append(_t)
        _v, _h = self._assemble([(_t, 0) for _t in _ts], _mv)
        # the construct is a stored NAME (record 6, before the MUL's 7), so
        # it is numbered with the band
        self.band.add(int(_v[1:]))
        _r = self._fresh(is_wide=True)
        _tie = []
        for _c in range(_n):
            _tie.append(len(self.lines))
            self.lines.append(_emit(_rcp, "%s.%s" % (_r, _COMPONENTS[_c]),
                                    "%s.x" % _ts[_c]))
        dst = self._fresh(True)
        _tie.append(len(self.lines))
        self.lines.append(_emit(_mul, dst + _ds, _r, _ONE + ".x"))
        self.ties.append(_tie)
        self.values[ins.result] = dst
        return True

    def _cross(self, ins, which, forms, _sh):
        """CROSS (notes/59), from `0052_op_cross.vert`'s nodes:
          MUL t.xyz, a.yzxw, b.zxyw      seq 2, a lowering temp
          MUL u.xyz, a.zxyw, b.yzxw      seq 5, a lowering temp
          ADD r.xyz, t, -u               seq 1, a stored NAME
        The ADD is CREATED first, but only the relative order of the two
        MULs reaches the scheduler (the ADD is never ready before them), so
        the lines are emitted in dependence order.  Only the measured operand
        form is taken: an addressable vec3 read as `.xyz`, whose fourth
        selector prints as `w`."""
        if which != GLSL450.Cross:
            return False
        module = self.module
        _ops = ins.args()[2:]
        _tc = _glasm_type_code(module, ins.result_type)
        _mul = _opchain.mnemonic_for_opcode(nodes.MUL, _tc)
        _add = _opchain.mnemonic_for_opcode(nodes.ADD, _tc)
        if (len(_ops) != 2 or len(forms) != 2
                or _components(module, ins.result_type) != 3
                or any(self.comps.get(a) != (0, 1, 2) for a in _ops)
                or any(f.startswith(("{", "-", "|", "#"))
                       or _lex.has_swizzle_suffix(f) for f in forms)
                or None in (_mul, _add)
                or _mul.startswith("<") or _add.startswith("<")):
            raise NotEstablished(
                "GLSL.std.450 68 (cross) on operands other than an "
                "addressable vec3 read as .xyz: not measured")
        _a, _b = forms
        _t = self._fresh()
        self.lines.append(_emit(_mul, "%s.xyz" % _t, _a + ".yzxw",
                                _b + ".zxyw"))
        _u = self._fresh()
        self.lines.append(_emit(_mul, "%s.xyz" % _u, _a + ".zxyw",
                                _b + ".yzxw"))
        dst = self._fresh(True)
        self.lines.append(_emit(_add, "%s.xyz" % dst, _t, "-%s" % _u))
        self.values[ins.result] = dst
        return True
