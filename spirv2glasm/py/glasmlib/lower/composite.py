"""composite.py -- extracts, constructs and shuffles.

A component of a vector and a shuffle of one emit NOTHING: they are a
selector on the operand.  A construct of different values is one write per
component into one vreg (the gather of notes/44); a splat is the scalar's own
register read in every lane.
"""
import lex as _lex

from spvnames import Op

import sched as _sched
import opchain as _opchain
from glasmlib.common import NotEstablished, ENV
from glasmlib.types import BOOL_TYPE_CODE, _components, _glasm_type_code, \
    _pointee
from glasmlib.boolean import _BOOL_REPR_CODE
from glasmlib.usage import _constant_index
from glasmlib.common import ACCESS_CHAINS
from glasmlib.operands import _constant_operand, _constant_source
from glasmlib.lower.core import _is_placeholder
from glasmlib import nodes

_IDENTITY = (0, 1, 2, 3)
# A constant's first component: `{0.5, 0, 0, 0}` or `{0.5, 0, 0, 0}.x`.


def _first_constant(t):
    r"""`^\{([^,}]+)[^}]*\}(?:\.x)?$` matched: the first component's text,
    or None."""
    if not t.startswith("{"):
        return None
    b = t.find("}")
    if b < 0 or t[b + 1:] not in ("", ".x"):
        return None
    k = 1
    while k < b and t[k] != ",":
        k += 1
    return t[1:k] if k > 1 else None


class CompositeOps(object):

    # -- extracts -------------------------------------------------------------

    def _arm_extract(self, ins):
        if ins.opcode != Op.OpCompositeExtract:
            return False
        args = ins.args()
        if len(args) != 2:
            raise NotEstablished("a nested composite extract")
        base = self.values.get(args[0])
        if base is None and self._extract_constant(ins, args):
            return True
        _ls = self.lsplit.get(args[0])
        if _ls is not None and args[1] in _ls:
            # a component this block stored: its value (notes/69)
            self.values[ins.result], _c = _ls[args[1]]
            self.comps[ins.result] = (_c,) * 4
            return True
        if base is None:
            raise NotEstablished("an extract whose source has no form")
        idx = args[1]
        if idx > 3:
            raise NotEstablished("an extract past component 3")
        # Extracting one component of a vector emits NOTHING: it is a swizzle
        # on the operand, exactly like `OpVectorShuffle`.
        prev = self.comps.get(args[0], _IDENTITY)
        self.values[ins.result] = base
        self.comps[ins.result] = (prev[idx],) * 4
        return True

    def _extract_constant(self, ins, args):
        """A component of a CONSTANT is that scalar constant: the reader
        folds the extract (`sc_cstore.frag`: `o.xyz = vec3(1.0)` stores `{1,
        0, 0, 0}` three times)."""
        _cc0 = self.module.constants.get(args[0])
        if (_cc0 is None or _cc0.opcode != Op.OpConstantComposite
                or args[1] >= len(_cc0.args())):
            return False
        _k = _constant_operand(self.module, _cc0.args()[args[1]])
        if _k is None:
            return False
        self.values[ins.result] = _k
        return True

    # -- constructs -----------------------------------------------------------

    def _arm_construct(self, ins):
        if ins.opcode != Op.OpCompositeConstruct:
            return False
        args = ins.args()
        # the SAME NODE, not only the same id: loads of one location in one
        # block are one interned expression (`ldc_same`), so `vec4(s, s, s,
        # s)` from four `OpLoad`s is a splat (`pu_a.vert`: `MUL.F32 R1,
        # vertex.attrib[0], R0.x;`)
        if len(set(args)) != 1 and len(set(
                self.ldc_canon.get(_a, _a) for _a in args)) == 1:
            self._construct_one_node(ins, args)
        elif len(set(args)) != 1:
            self._construct_gather(ins, args)
        else:
            self._construct_splat(ins, args)
        return True

    def _construct_mov(self, ins, refusal):
        _tc = _glasm_type_code(self.module, ins.result_type)
        if _tc == BOOL_TYPE_CODE and not ENV.get("G2S_NOBOOLCONSTRUCT"):
            # A BOOL IS HELD IN ITS REPRESENTATION by the time a MOV prints:
            # `f_7100030e20` retypes every bool-typed node (notes/64 §4), so a
            # bool vector's construct writes are `MOV.U` (`sel_g.frag`)
            _tc = _BOOL_REPR_CODE
        _mov = _opchain.mnemonic_for_opcode(nodes.MOV, _tc)
        if _mov is None or _mov.startswith("<"):
            raise NotEstablished(refusal)
        return _mov

    def _construct_one_node(self, ins, args):
        """DIFFERENT OPERANDS, ONE NODE: unlike a splat of one id, the reader
        builds a real construct statement, `@TMP = vec4(U.s, U.s, U.s, U.s)`,
        and its lowering is the node's swizzle.  Readers in the block take
        that swizzle forwarded (`MUL.F32 R1, vertex.attrib[0], R0.x;`) and
        the temp's own store is flushed like any temp's: `MOV.F R0, R0.x;` in
        `pu_a.vert`, `MOV.F R1.xyz, R1.x;` in `chr_cloth_06b33827.vert`'s
        `vec3(b, b, b)`."""
        self._computation()
        _b0 = self.values.get(args[0])
        _nres = _components(self.module, ins.result_type)
        _tc = _glasm_type_code(self.module, ins.result_type)
        _mov = _opchain.mnemonic_for_opcode(nodes.MOV, _tc)
        _cds = _opchain.dest_suffix(_nres) if _nres else None
        if (_b0 is None or not _is_placeholder(_b0)
                or args[0] in self.comps or _mov is None
                or _mov.startswith("<") or _cds is None):
            raise NotEstablished(
                "a construct of one repeated load whose MOV, mask or source "
                "has no form")
        self.values[ins.result] = _b0
        self.comps[ins.result] = (0,) * 4
        _cn = self._fresh(True)
        self.stmtpos[_cn] = len(self.lines)
        # the flush carries the construct statement's `node[36]`: the
        # statement is made before the loads its operands lower to
        # (`tools/gsum.py` on `pu_a.vert`: the flush seq 1, the LDC 3, the
        # MUL 4)
        # -- and when the operands' load is an EARLIER statement's, interned
        # (one location per block), no load is made here and the statement
        # sits where it is: `on_b.frag`'s second `vec3(c18, c18, c18)` flush
        # is seq 25, before its MUL (26) and after the first statement (23),
        # where the shared LDC's line would put it with the first flush (18)
        _canon = self.ldc_canon.get(args[0], (args[0],))[0]
        _cg = _sched.Tie()
        if _canon != args[0] and not ENV.get("G2S_NOONENODESTMT"):
            _cg.seq = len(self.lines) - 0.5
        else:
            _cg.seq = self.ldc_line.get(_canon, len(self.lines)) - 0.5
        self.ties.append(_cg)
        self.flush_q.append((_cn, _mov, _cds, _cg, None, _b0 + ".x"))
        self.con_blk[ins.result] = self._bkey()

    def _construct_gather(self, ins, args):
        """ONE WRITE PER COMPONENT INTO ONE VREG.  Read from the emit list of
        `co_mix4.vert` (`vec4(a0.x, a1.y, a0.z, a1.w)`) and `co_mul4.vert`
        (four independent products): the construct makes ONE vreg whose four
        writes all carry the same `node[36]`, each writing one component
        mask, and a source component other than 0 is first copied into the
        `.x` lane of a vreg of its own -- the same gather the store's
        expansion does (notes/44).  The ORDER the four are printed in is not
        this loop's: they share a `node[36]`, so pass 1 is what reverses them
        (py/sched.py).

        COMPONENT 0 IS FORWARDED TO THE CONSUMER.  `co_mix4`'s `MOV.F
        result.position.x, vertex.attrib[0];` reads the construct's FIRST
        SOURCE and not the vreg -- the store node carries `src=` the
        attribute with `inl=1` -- while `.y`, `.z` and `.w` read the vreg
        through a `0x2b`.  `head_src` carries that one operand to the
        store."""
        self._computation()
        _nres = _components(self.module, ins.result_type)
        _mov = self._construct_mov(
            ins, "a composite whose MOV the image's chain does not name for "
            "this type")
        flat, _force = self._flatten_operands(args)
        if len(flat) != _nres:
            raise NotEstablished("a composite of %d components built from %d"
                                 % (_nres, len(flat)))
        _nodes = [(_b, _s) for _b, _s in flat if _b is not None]
        if (len(set(_nodes)) != len(_nodes)
                and ENV.get("G2S_REFUSEREPEAT")):
            # ONE NODE IN SEVERAL LANES is now read on the DAG (notes/104
            # §8): a leading run of a plain node is one merged write
            # (`_assemble`'s `_leading_run`), and every other lane -- a
            # swizzle operand in any lane, a repeat after another operand --
            # is written on its own.  `G2S_REFUSEREPEAT=1` restores the
            # refusal.
            raise NotEstablished(
                "a composite repeating one node across lanes (refused on "
                "request)")
        dst, _text = self._assemble(flat, _mov, _force, True)
        # When component 0 is a value computed in this block, the construct's
        # write of it and the store that forwards it become ready together
        # and the tie is decided by an edge the ALLOCATOR puts there --
        # `co_add1.vert` and `co_mul4.vert` carry the same keys and come out
        # in opposite orders, the difference being a write-after-write on the
        # register the construct shares with its `.y` gather.  Pass 2 builds
        # its edges on allocated registers (notes/57), so this is only
        # refused on the old sequence (`G2S_NOALLOC1=1`).
        if (_text.startswith("#") and ENV.get("G2S_NOALLOC1")
                and any(_b2 is not None and _s2 != 0 for _b2, _s2 in flat)):
            raise NotEstablished(
                "a composite whose first component is a computed value "
                "beside a gathered one: the store forwards it, and the tie "
                "with the component write is then decided by an "
                "anti-dependence on the register the construct shares with "
                "the gather")
        self.head_src[ins.result] = _text
        self.values[ins.result] = dst

    def _selects_vector_component(self, _a):
        """Is operand `_a` ONE COMPONENT OF A VECTOR -- an extract of a
        vector (or of a matrix column), or a load through an access chain
        whose last index picks a vector's component?  Such a lane is a
        component select, not a scalar node, and a construct gathers it
        through a `.x` scratch whatever the component (notes/104 §6)."""
        module = self.module
        ins = module.result_insn.get(_a)
        if ins is None:
            return False
        if ins.opcode == Op.OpVectorExtractDynamic:
            return True
        if ins.opcode == Op.OpCompositeExtract:
            _c = module.result_insn.get(ins.args()[0])
            if _c is None or not _c.has_result_type:
                return False
            return self._last_step_on_vector(_c.result_type,
                                             ins.args()[1:], literal=True)
        if ins.opcode == Op.OpLoad:
            _p = module.result_insn.get(ins.args()[0])
            if _p is None or _p.opcode not in ACCESS_CHAINS:
                return False
            _base = _pointee(module, _p.args()[0])
            if _base is None:
                return False
            return self._last_step_on_vector(_base, _p.args()[1:],
                                             literal=False)
        return False

    def _last_step_on_vector(self, tid, idx, literal):
        """Walk type `tid` through the indices `idx` (literals for an
        extract, constant ids for a chain) and say whether the LAST one
        indexes a vector."""
        module = self.module
        if not idx:
            return False
        t = module.types.get(tid)
        for k, i in enumerate(idx):
            if t is None:
                return False
            if k == len(idx) - 1:
                return t.opcode == Op.OpTypeVector
            if t.opcode == Op.OpTypeStruct:
                n = i if literal else _constant_index(module, i)
                if n is None or not 0 <= n < len(t.args()):
                    return False
                t = module.types.get(t.args()[n])
            elif t.opcode in (Op.OpTypeArray, Op.OpTypeRuntimeArray,
                              Op.OpTypeMatrix, Op.OpTypeVector):
                t = module.types.get(t.args()[0])
            else:
                return False
        return False

    def _forces_gather(self, _a):
        """A local name's component, or a COMPONENT OF A BLOCK VECTOR, is
        gathered even from `.x` (`map_110833a5`'s `vec4(uvScroll0_g.x, ..,
        uvScroll1_g.x, ..)`: `MOV.F R1.x, R1;` then `MOV.F R0.z, R1.x;`).
        The name's component read through a whole load -- an extract of the
        load or of a shuffle of it -- is the same read (`fa_b.frag`'s
        `vec4(u_xlat2.xy, u_xlat1.xy)`: `MOV.F R1.x, R1;` then `MOV.F R1.z,
        R1.x;`, R1 being `u_xlat1`)."""
        if _a in self.lname and self.values.get(_a) == self.lname[_a][0]:
            return True
        if (self._selects_vector_component(_a)
                and _a not in self.node_loads
                and not ENV.get("G2S_NOSELECTGATHER")):
            # A COMPONENT OF A VECTOR is a select, not a scalar node, into
            # any lane but x (notes/104 §6): `cl_c.frag`'s `vec4(w.x, t.x,
            # ..)` prints `MOV.F R0.x, R2; .. MOV.F R0.y, R0.x;`, `cl_d`'s
            # attribute `w.x` and `cl_e`'s `u_xlat7.w` gather as well, even
            # where the lane forwards to a scalar the block stored
            # (`monster_02d3d44e`'s `txVec0 = vec4(u_xlat7.xyw, ..)`)
            return True
        _nr = self._name_read(_a) if _a not in self.load_of else None
        if (_nr is not None and self.values.get(_a) == _nr[0]
                and not ENV.get("G2S_NOEXTNAMEGATHER")):
            return True
        return (self.ldc_canon.get(_a) or (None, None))[1] is not None

    def _flatten_operands(self, args):
        """The construct's operands, one (node, component) per lane, and the
        lanes that must be gathered."""
        module = self.module
        flat = []
        _force = set()
        for _a in args:
            if self._forces_gather(_a):
                _force.add(len(flat))
            _d = self.by_result.get(_a) or module.result_insn.get(_a)
            if _d is None or not _d.has_result_type:
                raise NotEstablished("a composite operand with no type")
            _w = _components(module, _d.result_type)
            if (_w and _w > 1 and self.values.get(_a) is not None
                    and not ENV.get("G2S_NOSELECTGATHER")):
                # every lane of a VECTOR operand is a component of it
                # (`cl_f.frag`'s `vec4(s, t, 1.0)`, t a vec2: `MOV.F R1.x,
                # R3; .. MOV.F R1.y, R1.x;`)
                _force.update(range(len(flat), len(flat) + _w))
            _b = self.values.get(_a)
            if _b is None:
                _cs = _constant_source(module, _a, 1)
                if _cs is None or _w != 1:
                    raise NotEstablished("a composite operand with no form")
                flat.append((None, _cs))
                continue
            _crd = (self._con_read(_a)
                    if _w == 1 and not ENV.get("G2S_NOCONOPFWD") else None)
            if _crd is not None:
                # A COMPONENT OF A CONSTRUCT MADE IN THIS BLOCK reads that
                # lane's source, as every other reader does (`_con_read`,
                # notes/81 §3): `cc_d.frag`'s `vec4(f.x, f.y, 0.0, 1.0)`, f
                # the bitcast built lane by lane (`MOV.F R16.y, R1.x; MOV.F
                # R16.x, R0;`), prints `MOV.F R2.x, R1; MOV.F R2.y, R2.x;
                # MOV.F R2.x, R0;` -- the gather kept, the source forwarded
                flat.append((_crd.split(".")[0], 0))
                continue
            _cm = self.comps.get(_a, _IDENTITY)
            _spl = self.lsplit.get(_a, {}) if _a not in self.load_of else {}
            for _k in range(_w):
                if _k in _spl:
                    # a component the block stored: its value (the shuffle's
                    # `lsplit`, notes/90)
                    flat.append(_spl[_k])
                    continue
                flat.append((_b, _cm[_k] if _k < len(_cm) else _k))
        return flat, _force

    def _construct_splat(self, ins, args):
        """A SPLAT emits nothing.  `vec4(dot(a, b))` is the dot's own
        register read four times, and `op_dot.vert` shows the listing never
        materialises the vector.

        THE READER MAKES THE SPLAT A SWIZZLE of the scalar's temp
        (`g2s_trace_wstmt` on `sp_mul.frag`: `o = t.xxxx`, a cls-14 swizzle
        over the variable node), and a variable read INSIDE a swizzle is not
        forwarded -- its node carries none of the forwarding marks a plain
        read has (`[56]`/`[72]` zero, against 0xf / 0xab0001 on
        `sc_min.frag`'s `o.x = t`).  So the store reads the temp's NAME, the
        value has one use, it folds into the name, and there is no self-move
        after the store: `MUL.F32 R0.x, a, b; MOV.F result_color0, R0.x;`
        (notes/72)."""
        base = self.values.get(args[0])
        c = self.comps.get(args[0], _IDENTITY)
        if base is None:
            base = self._constant_splat(ins, args[0])
            if base is not None:
                # a constant vector, read as one: no selector
                self.values[ins.result] = base
                return
        if base is None:
            raise NotEstablished("a splat whose source has no form")
        self.values[ins.result] = base
        self.comps[ins.result] = (c[0],) * 4
        self.splats.add(ins.result)
        self.splat_src[ins.result] = args[0]
        if args[0] in self.lname:
            self.lname[ins.result] = self.lname[args[0]]

    def _constant_splat(self, ins, vid):
        """A SPLAT OF A CONSTANT is the constant, widened: `mix(a, b, 0.5)`
        builds `vec4(0.5)` this way and the listing prints it as `{0.5, 0.5,
        0.5, 0.5}` in the multiply's operand slot.  BROADCAST, not a `.x`
        read: the splat's own width is the result's, where a one-component
        constant would print `{0.5, 0, 0, 0}.x`.  The literal has four slots
        like every constant's, the ones past the width 0 (`cs_a.frag`'s
        `vec3(1.0)` built in the body: `MIN.F R0.xyz, .., {1, 1, 1, 0};`)."""
        base = _constant_source(self.module, vid, 1)
        if base is None:
            return None
        _m1 = _first_constant(base)
        if _m1 is None:
            return None
        _w = _components(self.module, ins.result_type)
        if not _w or _w > 4:
            return None
        _slots = [_m1] * _w
        if not ENV.get("G2S_SPLATWIDTH"):
            _slots += ["0"] * (4 - _w)
        return "{%s}" % ", ".join(_slots)

    # -- shuffles -------------------------------------------------------------

    def _arm_shuffle(self, ins):
        if ins.opcode != Op.OpVectorShuffle:
            return False
        args = ins.args()
        a, b = args[0], args[1]
        sel = args[2:]
        if a != b:
            raise NotEstablished("a shuffle of two different vectors")
        base = self.values.get(a)
        if base is None:
            raise NotEstablished("a shuffle whose source has no form")
        if any(x > 3 for x in sel):
            raise NotEstablished("a shuffle selecting the second vector")
        _ls = self.lsplit.get(a, {})
        if sel and all(x in _ls for x in sel) and len(
                set(_ls[x][0] for x in sel)) == 1:
            # EVERY SELECTED COMPONENT WAS STORED IN THIS BLOCK: the shuffle
            # reads the stored values, as a component read does (notes/69).
            # `mc_n12.vert`'s `u_xlat8.xxx * u_xlat2.xyz`, right after
            # `u_xlat8.x = inversesqrt(..)`, prints `MUL.F32 R2.xyz, R1.x,
            # R28;` -- R1 the RSQ, not `u_xlat8`.
            self.values[ins.result] = _ls[sel[0]][0]
            self.comps[ins.result] = tuple(_ls[x][1] for x in sel)
            return True
        prev = self.comps.get(a, _IDENTITY)
        _lwa = self.load_of.get(a)
        if (_lwa is not None and base == _lwa[1] and a not in self.comps
                and not any(x in _ls for x in sel)):
            # a read of the NAME's lanes after a merge pair in the block
            # (core.py `_name_read_after_pair`)
            self._name_read_after_pair(_lwa[0], set(sel), ins.result)
        self.comps[ins.result] = tuple(prev[x] for x in sel)
        self.values[ins.result] = base
        if _ls and any(x in _ls for x in sel) \
                and not ENV.get("G2S_NOSHUFSPLIT"):
            # SOME selected components were stored in this block: a reader
            # that takes the shuffle a COMPONENT AT A TIME (a construct's
            # gather) reads those stored values and the name for the rest
            # (notes/90).  `bl_259b.vert`'s `vec4(u_xlat4.xyz, ..)` right
            # after `u_xlat4.z = ..` gathers `MOV.F R5.x, R2.z;` -- the DIV --
            # for `.z` and `R35.y`, `R35` (the name) for the others.
            self.lsplit[ins.result] = dict(
                (k, _ls[x]) for k, x in enumerate(sel) if x in _ls)
        return True
