"""control.py -- structured control flow, selects, kills, geometry emits,
calls and returns.

The flattened body carries MARKERS (cflow.py) where the structure words go:
IF / ELSE / ENDIF, REP / ENDREP, the loop's exit arm, the continue flag.
Each is a block boundary: the scheduler runs ONE BASIC BLOCK AT A TIME
(notes/31), so a computation in a later block is never hoisted above a store
in an earlier one.
"""
import re as _re
from spvnames import Op, ExecutionModel

import sched as _sched
import opchain as _opchain
from glasmlib.common import NotEstablished, ENV, KILLS
from glasmlib.types import BOOL_TYPE_CODE, _components, _glasm_type_code, \
    _pointee_code, _pointee_components
from glasmlib.operands import _constant_source
from glasmlib.text import _COMPONENTS, _emit, _swizzle, _source
from glasmlib.boolean import _BOOL_SUFFIX, _BOOL_REPR_CODE, _CFLAG_TYPE, \
    _CFLAG_REG, _bool_constant, _bool_normalise, _cc_move, _switch_test, \
    _loop_head_test
from glasmlib.cflow import MARKER

# SPIR-V `Scope` and `MemorySemantics` values the barrier arm needs.  The
# memory-class mask is the five class bits; the ordering bits (acquire,
# release, sequentially-consistent) do not change the spelling in any
# listing measured (notes/119 §1).
_SCOPE_WORKGROUP = 2
_SCOPE_INVOCATION = 4
_SEM_UNIFORM_MEMORY = 0x40
_SEM_SUBGROUP_MEMORY = 0x80
_SEM_WORKGROUP_MEMORY = 0x100
_SEM_CROSS_WORKGROUP_MEMORY = 0x200
_SEM_ATOMIC_COUNTER_MEMORY = 0x400
_SEM_IMAGE_MEMORY = 0x800
_MEM_SEMANTICS = (_SEM_UNIFORM_MEMORY | _SEM_SUBGROUP_MEMORY
                  | _SEM_WORKGROUP_MEMORY | _SEM_CROSS_WORKGROUP_MEMORY
                  | _SEM_ATOMIC_COUNTER_MEMORY | _SEM_IMAGE_MEMORY)
from glasmlib.lower.core import _local_chain, _is_placeholder
from glasmlib import nodes

_IF = "IF    NE.x;"
_RET = "RET   (TR);"
_BRK = "BRK   (NE.x);"
_CFLAG_KINDS = ("CFLAG_INIT", "CFLAG_SET")


def _parameter_mov(module, param):
    """(MOV, destination suffix) of a parameter's copy."""
    _pc = _pointee_components(module, param.result)
    _ptc = _pointee_code(module, param.result)
    _pm = (_opchain.mnemonic_for_opcode(nodes.MOV, _ptc)
           if _ptc is not None else None)
    _pd = _opchain.dest_suffix(_pc) if _pc else None
    if _pm is None or _pm.startswith("<") or _pd is None:
        raise NotEstablished("a parameter whose MOV the image does not give")
    return _pm, _pd


class ControlOps(object):

    # -- the continue flag --------------------------------------------------

    def _arm_continue_flag(self, ins):
        """The `continue` flag's two assignments (notes/71) are ordinary
        statements, not block boundaries: `flag = 0` at the loop body's top
        and `flag = 1.0` where the continue stood.  The flag is a NAME like a
        materialised local, so the walker's name-record rule applies
        (notes/65 §4): the store opens a block when the flag's store is
        pending from an earlier block and this one already holds a node --
        `0071_lp_wcont.vert`'s node dump has the arm's `flag = 1.0` (seq 31) in a
        block of its own after the ADD and the stores of `i` (seq 28)."""
        if ins.opcode != MARKER or ins.kind not in _CFLAG_KINDS:
            return False
        _hmov = self._mov_for(_CFLAG_TYPE)
        if _hmov is None:
            raise NotEstablished("the continue flag's MOV the image's chain "
                                 "does not name")
        self.wants_h = True
        if ins.kind == "CFLAG_SET":
            self._flush()
        if (self.lpend.get(_CFLAG_REG, self.blk_no) != self.blk_no
                and self.blk_node):
            self.cuts.append(len(self.lines))
        self.lines.append(_emit(_hmov, _CFLAG_REG + ".x",
                                _bool_constant(ins.kind == "CFLAG_SET")))
        self.lpend[_CFLAG_REG] = self.blk_no
        self.blk_node = True
        return True

    # -- the structure words --------------------------------------------------

    _MARKER_KINDS = {
        "MAINEND": "_marker_main_end",
        "FUNC": "_marker_function",
        "IF": "_marker_if",
        "LOOPIF": "_marker_if",
        "REP": "_marker_rep",
        "BREAK": "_marker_break",
        "CFLAG_TEST": "_marker_flag_test",
        "LOOPELSE": "_marker_loop_else",
    }

    def _arm_marker(self, ins):
        """IF / ELSE / ENDIF and the rest.  The store counter resets at every
        block boundary."""
        if ins.opcode != MARKER:
            return False
        self.stores = 0
        # the block the condition's `.CC` move is in: the one before the
        # structure word
        _bk_pre = self._bkey()
        self.blk_no += 1
        self.blk_node = False
        getattr(self, self._MARKER_KINDS.get(ins.kind, "_marker_word"))(
            ins, _bk_pre)
        # ... and only NOW: the condition's `.CC` move is in the block
        # BEFORE the structure word (`_bk_pre`), and it reads what that
        # block forwards (`0105_la_a.frag`: `MOV.U.CC RC.x, R1;`).
        self._expire_local_forwards()
        return True

    def _marker_word(self, ins, _bk_pre):
        self._flush()
        self.lines.append("%s;" % ins.kind)

    def _marker_main_end(self, ins, _bk_pre):
        """0xf0fa94..0xf0fb14: the program has other functions, so the entry
        function gets a RET of its own after its body."""
        self._flush()
        self.lines.append(_RET)

    def _marker_function(self, ins, _bk_pre):
        """f_7100f103d0 with the function's symbol: its label, then the ENTRY
        COPY of each `in` parameter (0xf10474..0xf104f0: a local of the
        function is assigned the incoming name's value), a store of the entry
        block like any other, made before the body and flushed with it."""
        _f = ins.cond
        self.cur_fn = _f
        self.lines.append("BB@%d:" % _f.result)
        for _pk, _p in enumerate(_f.params):
            _F = self.formal.get(_p.result)
            if _F is None:
                raise NotEstablished("a parameter no call passed")
            _pm, _pd = _parameter_mov(self.module, _p)
            # its own position, before the body's first statement
            # (`0068_cf_call4.vert`: seq 20 and 21 for the two, 22 for the
            # multiply)
            _g = _sched.Tie()
            _g.seq = len(self.lines) - 0.5 + 0.01 * _pk
            self.ties.append(_g)
            self.fgrp[_F] = _g
            _L = self._fresh(True)
            self.callnames.add(_L)
            self.stmtpos[_L] = len(self.lines)
            self.flush_q.append((_L, _pm, _pd, _g, None, _F))

    def _open_if(self):
        self._flush()
        self.lines.append(_IF)
        self.wants_cc = True

    def _marker_if(self, ins, _bk_pre):
        if ins.kind == "IF" and isinstance(ins.cond, tuple):
            self.lines.extend(_switch_test(self.module, self.values,
                                           self.comps, ins.cond, self._fresh))
            self._open_if()
            return
        if ins.kind == "IF" and ins.cond in self.facing_loads:
            # AN IF ON gl_FrontFacing (notes/88): the substituted expression
            # is a comparison, `facing > 0` on the int input, and its bool
            # move (an integer operand's `MOV.S t, -c`, `_bool_normalise`) has
            # the IF as its one use, so the IF's `.CC` folds into it with the
            # `HC` destination (`_switch_test`'s rule).  `0088_ff_a.frag`: `MOV.S
            # R0.x, fragment.facing; SGT.S R1.x, R0, {0, 0, 0, 0}; MOV.S.CC
            # HC.x, -R1; IF NE.x;`.
            self._facing_cc()
            self._open_if()
            return
        self._if_on_value(ins, _bk_pre)

    def _is_bool_type(self, tid):
        t = self.module.types.get(tid)
        return t is not None and t.opcode == Op.OpTypeBool

    def _is_bool_local(self, cond):
        """A STORED BOOL (`bool b = i >= n; if (b)`): the compare was
        normalised where it was made (the store's value), and the IF reads
        the variable (`g2s_trace_wstmt` on 0071_lp_wbrk.vert: the if's condition
        is the variable node, cls 12), so no move of its own."""
        _cd = self.module.result_insn.get(cond)
        return (cond in self.load_of and _cd is not None
                and _cd.has_result_type
                and self._is_bool_type(_cd.result_type))

    def _if_on_value(self, ins, _bk_pre):
        c = self.values.get(ins.cond)
        if c is None:
            raise NotEstablished("a branch on a value with no form")
        _bool_local = self._is_bool_local(ins.cond)
        if self._if_on_bool_component(ins, _bk_pre):
            return
        if ins.cond not in self.normalised and not _bool_local:
            self.lines.extend(_bool_normalise(self.module, ins.cond, c))
        self.lines.append(_cc_move(c))
        self._open_if()

    def _if_on_bool_component(self, ins, _bk_pre):
        """A COMPONENT OF A STORED BOOL VECTOR (`if (b.x)`, HLSLcc's
        `(u_xlatb6.x) ? .. : ..`, notes/85): the component select of the
        name is an instruction with one use and no modifier, so the IF's
        `.CC` folds into it and the value, used nowhere else, goes to `HC`
        (node[40] = 1, as `_switch_test`'s move).  `0085_pb_a.frag` prints
        `MOV.U.CC HC.x, R3;` -- the NAME's register, R3, not the TRUNC stored
        into it in the same block.

        ... unless the component was stored in THIS block: the read then
        takes the stored value, as every component read does (notes/69) --
        the cut `0000_mq_n18.frag`'s `u_xlatb4.x = 0.0 < t.x; if (u_xlatb4.x)`
        prints `MOV.U.CC HC.x, R3;`, R3 the TRUNC."""
        _read = self._bool_component_read(ins.cond, _bk_pre)
        if _read is None:
            return False
        _bsrc = _read[0]
        self.lines.append("MOV.%s.CC HC.x, %s;" % (_BOOL_SUFFIX, _bsrc))
        self._open_if()
        return True

    def _bool_component_read(self, cond, bkey):
        """(the operand, is it the stored NODE itself) for a read of ONE
        COMPONENT OF A STORED BOOL VECTOR, stored in block `bkey`:

        * a plain store into lane x: the stored value itself -- a second use
          of it (`0095_sel_b.frag`, `0101_sel_p.frag`);
        * a merge pair: the value stored, through a component select of its
          own (notes/69, `0101_sel_j.frag`);
        * a plain store into ANOTHER lane is an insert, and the read selects
          from the name's register (`0101_sel_n.frag`: `MOV.U.CC HC.x, R0.z;`,
          `0101_sel_m.frag`);
        * no store in `bkey`: the name's register at that component.

        None when `cond` is not such a read."""
        module = self.module
        _cd = module.result_insn.get(cond)
        _blc = (_local_chain(module, _cd.args()[0], self.by_result,
                             self.locals_)
                if _cd is not None and _cd.opcode == Op.OpLoad
                and self._is_bool_type(_cd.result_type) else None)
        if (_blc is None or _blc[0] not in self.local_reg
                or ENV.get("G2S_NOBCOMP")):
            return None
        _bcf = self.cfw.get(_blc[0], {}).get(_blc[1])
        _kind = self.cfw_kind.get(_blc)
        if (_bcf is not None and _bcf[0] == bkey
                and (_kind != "insert" or ENV.get("G2S_INSERTFWD"))
                and not ENV.get("G2S_NOBCOMPFWD")):
            return _swizzle(_bcf[1], _bcf[2]), _kind == "node"
        return _swizzle(self.local_reg[_blc[0]], _blc[1]), False

    def _marker_rep(self, ins, _bk_pre):
        self._flush()
        self.lines.append("REP.S ;")
        self.lines.extend(_loop_head_test())
        self.wants_cc = True

    def _marker_break(self, ins, _bk_pre):
        """A `break` in an arm (notes/71): the same conditional BRK on a
        constant true the loop's exit arm makes."""
        self._flush()
        self.lines.append(_cc_move(_bool_constant(True)))
        self.lines.append(_BRK)
        self.wants_cc = True

    def _marker_flag_test(self, ins, _bk_pre):
        """`if (flag == 0)` (notes/71): the flag is a temp of IR type 6,
        whose compare the image's chain names `SEQ.F`, in the SHORT register
        class -- `H0`, declared by the TEMP printer's first class
        (bdcd60(.., 2, "SHORT ", "H"), 0xbdb508).  The compare is the IF's
        condition itself, so its move into the bool representation (a float
        operand: TRUNC, `_bool_normalise`) has one use and f_7100032c30 folds
        the IF's `.CC` MOV into it, with the `HC` destination
        (`_switch_test`).  The compare's own result lands in the flag's
        register."""
        _hseq = _opchain.mnemonic("OpFOrdEqual", _CFLAG_TYPE)
        if _hseq is None or _hseq.startswith("<"):
            raise NotEstablished("the continue flag's compare the image's "
                                 "chain does not name")
        self.wants_h = True
        self._flush()
        self.lines.append(_emit(_hseq, _CFLAG_REG + ".x", _CFLAG_REG,
                                _bool_constant(False)))
        self.lines.append("TRUNC.%s.CC HC.x, %s;" % (_BOOL_SUFFIX, _CFLAG_REG))
        self.lines.append(_IF)
        self.wants_cc = True

    def _marker_loop_else(self, ins, _bk_pre):
        """The SPIR-V condition's false arm is the loop's exit: an
        unconditional `break`, which the reader lowers as a conditional BREAK
        on a constant true (notes/64 §5)."""
        self._flush()
        self.lines.append("ELSE;")
        self.lines.append(_cc_move(_bool_constant(True)))
        self.lines.append(_BRK)

    # -- selects ------------------------------------------------------------

    def _arm_select(self, ins):
        """notes/67 §5.  The reader's handler (0xfdcba0 -> f_7100fd0e54, W3 =
        2) makes the cgc conditional expression (f_7100f3e120 kind 2,
        0xfd10ac) of the three operands, and the IR the statement walker
        receives is a STATEMENT: `if (c) t = a; else t = b;` into the
        result's named temp, then the consumer reads `t` (g2s_trace_irtree on
        `0067_sc_select.frag`).  So:
          * the condition is read where the select stands -- in this block,
            so a local stored here gives its stored value;
          * each arm reads its operand at the arm's entry: a local's NAME,
            not what the header block forwarded;
          * the result is a name stored in the arms, read after the ENDIF at
            block entry (no self-move)."""
        if ins.opcode != Op.OpSelect:
            return False
        args = ins.args()
        cond, ta, fa = args[0], args[1], args[2]
        c = self.values.get(cond)
        _facing = cond in self.facing_loads
        if c is None and not _facing:
            raise NotEstablished("a select on a value with no form")
        if (_components(self.module, ins.result_type) != 1
                and not ENV.get("G2S_NOVSELECT")):
            self._vector_select(ins, cond, ta, fa)
            return True
        # A COMPONENT OF A STORED BOOL VECTOR (HLSLcc's `int(u_xlatb0.x)`):
        # the `.CC` move reads the value stored to the component in this
        # block, else the name's component, as the branch on one does.  The
        # move FOLDS into a component select of its own -- `HC`, as the
        # branch's -- except when the read is the stored temp itself, stored
        # by a PLAIN write: that temp has two uses and the move stays, into
        # `RC` (`0095_sel_a.frag`, `0095_sel_b.frag`: `MOV.U.CC RC.x, R1;`; `0101_sel_j.frag`,
        # a merge pair: `MOV.U.CC HC.x, R2;`, and after the IF, the name:
        # `MOV.U.CC HC.x, R4.y;`).
        _bcomp = (self._bool_component_read(cond, self._bkey())
                  if not _facing else None)
        _hc = False
        if _bcomp is not None:
            c, _plain = _bcomp
            _hc = not _plain and not ENV.get("G2S_SELECTRC")
        elif (cond not in self.normalised and not self._is_bool_local(cond)
                and not _facing):
            raise NotEstablished(
                "a select on a bool that is neither a normalised comparison "
                "nor a stored bool")
        _mv, _ds, _forms = self._select_arms(ins, ta, fa)
        if not ENV.get("G2S_NOSELECTTOUCH"):
            # ... and a read in a LATER block touches the local (notes/65
            # §4): its pending store no longer opens a block.  `0108_dt_v.frag`'s
            # `u_xlat5.x = b ? u_xlat21 : 0.0` reads `u_xlat21` in the THEN
            # arm, and the next `u_xlat21 = dot(..)` is stored in its block --
            # `MOV.F R0.x, R3; MIN.F ..; MOV.F R3.x, R3;`, store and flush
            # (the corpus's `chr_hair_8419670a`)
            for _a in (ta, fa):
                _lv = self.load_of.get(_a)
                _cl = self.comp_load.get(_a)
                _var = (_lv[0] if _lv is not None
                        else _cl[0][0] if _cl is not None else None)
                if _var is not None:
                    self.lpend.pop(_var, None)
        _t = self._fresh(True)
        self.stores = 0
        self.blk_no += 1
        if _facing:
            # the select's IF on gl_FrontFacing (`0088_ff_a.frag`'s
            # `gl_FrontFacing ? 0xffffffffu : 0u`): the compare and its folded
            # `.CC` move, as the branch's (`_facing_cc`)
            self._facing_cc()
        elif _hc:
            self.lines.append("MOV.%s.CC HC.x, %s;" % (_BOOL_SUFFIX, c))
        else:
            self.lines.append(_cc_move(c))
        self._open_if()
        self._expire_local_forwards()
        self.lines.append(_emit(_mv, _t + _ds, _forms[0]))
        self.blk_no += 1
        self._expire_local_forwards()
        self.lines.append("ELSE;")
        self.lines.append(_emit(_mv, _t + _ds, _forms[1]))
        self.blk_no += 1
        self.lines.append("ENDIF;")
        self._expire_local_forwards()
        self.blk_node = False
        self.node_next = False
        self.values[ins.result] = _t
        self.defblk[ins.result] = -1
        self.arm_names.add(ins.result)
        return True

    def _bool_lane_view(self, cond):
        """A SHUFFLE OF A STORED BOOL VECTOR's lanes.

        `_normalised_view` follows a shuffle to a load of a bool local, but
        only when the block stored a COMPARISON into it.  A local the source
        filled lane by lane -- `u_xlatb0.xw = lessThan(..).xw` and then
        `uvec2(u_xlatb0.xw)` -- is the same shape for this arm's purpose:
        the `.CC` move copies the lanes read and the test swizzles, which is
        what the third branch below already builds and what `sv_a`, `sv_b`
        and `sv_c` pin.  `G2S_NOBOOLLANEVIEW=1` refuses it again."""
        if ENV.get("G2S_NOBOOLLANEVIEW"):
            return False
        d = self.module.result_insn.get(cond)
        if d is None:
            return False
        if d.opcode == Op.OpVectorShuffle and d.args()[0] == d.args()[1]:
            _b = d.args()[0]
        elif (d.opcode == Op.OpLoad
              and not ENV.get("G2S_NOBOOLLOADVIEW")):
            # THE WHOLE LOAD, with no shuffle over it, is the same shape:
            # `post_outline.frag` selects on a `bvec2` local and prints the
            # `.CC` lane by lane -- `MOV.U.CC RC.x, R7; MOV.U.CC RC.y, R7;`
            # -- exactly as the shuffled form does, against the single
            # masked `MOV.U.CC RC.xy, R0;` a live comparison gets.
            # (notes/121 §1)
            _b = cond
        else:
            return False
        _bd = self.module.result_insn.get(_b)
        if (_b not in self.load_of or _bd is None
                or not _bd.has_result_type):
            return False
        # a VECTOR of bool: `_is_bool_local`'s test is for the scalar
        _t = self.module.types.get(_bd.result_type)
        if _t is not None and _t.opcode == Op.OpTypeVector:
            return self._is_bool_type(_t.operands[1])
        return self._is_bool_type(_bd.result_type)

    def _merge_lane_order(self, base, mask):
        """The lanes of a local's merge IN THE ORDER THE COMPILER LISTS THEM.

        A component store's 0x57 merge node has two slots, the STORE half
        first and the pass-through copy second (`0114_m1.frag`, `0114_sv_h.frag`: the
        block-1 merge of a `bvec4` stored `.x` then `.z` is `[.z, .x]`), and
        the per-lane condition-code moves of a vector select are created in
        that order -- each reads the half for its own lane.  Creation order
        is what pass 1 releases in, so the lanes are built stored-first even
        though the listing prints them in component order.
        `G2S_NOLANEMERGEORDER=1` keeps plain component order."""
        if ENV.get("G2S_NOLANEMERGEORDER") or len(mask) < 2:
            return mask
        _carried = None
        for k in reversed(self.passthru):
            if not 0 <= k < len(self.lines):
                continue
            _m = _re.match(r"\s*\S+\s+(\S+?)(?:\.([xyzw]+))?\s*,",
                           self.lines[k])
            if _m is None or _m.group(1) != base:
                continue
            _carried = _m.group(2) or "xyzw"
            break
        if _carried is None:
            return mask
        _stored = [c for c in mask if c not in _carried]
        if not _stored:
            return mask
        return "".join(_stored + [c for c in mask if c in _carried])

    def _normalised_view(self, cond):
        """Is the bool a normalised comparison, or components of one (a
        shuffle or extract of it)?"""
        module = self.module
        while cond not in self.normalised:
            d = module.result_insn.get(cond)
            if d is None:
                return False
            if d.opcode == Op.OpVectorShuffle and d.args()[0] == d.args()[1]:
                cond = d.args()[0]
            elif d.opcode == Op.OpCompositeExtract and len(d.args()) == 2:
                cond = d.args()[0]
            elif (d.opcode == Op.OpLoad and cond not in self.comps
                    and not ENV.get("G2S_NOBOOLLOADVIEW")):
                # A LOAD OF A BOOL LOCAL the block stored a comparison into
                # is that comparison, forwarded: `0106_cc_c.frag`'s `mix(.., ..,
                # u_xlatb1.xy)` right after `u_xlatb1.xy = lessThan(..)`
                # sets the condition code from the TRUNC's register --
                # `MOV.U.CC1 RC.xy, R7;` beside the local's own store
                # `MOV.U R14.xy, R7;`
                _v = self.values.get(cond)
                _n = [n for n in self.normalised
                      if n not in self.comps and self.values.get(n) == _v]
                _lw = self.load_of.get(cond)
                if (not _n and _v is not None and _lw is not None
                        and _lw[1] == _v and self.norm_local.get(_lw[0])
                        and not ENV.get("G2S_NONORMLOCAL")):
                    # ... and a load that reads the LOCAL'S REGISTER, stored
                    # whole from a swizzle of a comparison (a selected value
                    # is materialised by the store): the register holds the
                    # normalised bools -- `0109_bs_a.frag`'s `u_xlatb1.xyz =
                    # greaterThanEqual(..).xyz; mix(.., u_xlatb1.xyz)` sets
                    # `MOV.U.CC RC.xyz, R4;`, R4 the local
                    return True
                if _v is None or not _n:
                    return False
                cond = _n[0]
            else:
                return False
        return True

    def _bool_splat(self, cond):
        """(the operand, its component) when the condition is a SPLAT of one
        bool -- `OpCompositeConstruct b b ..` -- else None.  A normalised
        comparison is read where it is (`0100_sel_f.frag`: `MOV.U.CC RC.x, R0;`).

        A splat of a STORED BOOL'S COMPONENT (HLSLcc's `bvec2(u_xlatb0.y)`)
        is refused: `0100_sel_e.frag` sets `RC.y` from the local's register, as a
        splat reads the name (notes/72), but its listing also has no store of
        the compare's temp, where `0095_sel_b.frag`'s scalar select does -- and
        when the compiler drops that store has not been read (notes/100)."""
        module = self.module
        d = module.result_insn.get(cond)
        if (d is None or d.opcode != Op.OpCompositeConstruct
                or len(set(d.args())) != 1 or ENV.get("G2S_NOBOOLSPLAT")):
            return None
        b = d.args()[0]
        bd = module.result_insn.get(b)
        if bd is not None and bd.opcode == Op.OpLoad:
            _blc = _local_chain(module, bd.args()[0], self.by_result,
                                self.locals_)
            if _blc is not None and _blc[0] in self.local_reg:
                if ENV.get("G2S_REFUSEBOOLCOMPSPLAT"):
                    raise NotEstablished(
                        "a vector select on a splat of a stored bool's "
                        "component (refused on request)")
                # THE LOCAL'S LANE: `0100_sel_e.frag`'s fold dump has the `.CC`
                # (0x7e, mask .y) on the insert that wrote `u_xlatb0.y` and
                # the select predicated on `.y` -- `MOV.U.CC RC.y, R0;` ..
                # `(NE.y)`.  The compare has no store of its own because the
                # store into lane y is an insert (the `sel_n` rule), not
                # because anything was dropped (notes/104 §9)
                # ... and that is what the lane decides, not the splat: a
                # plain store into lane X is not an insert but the value's
                # own node, and the `.CC` reads what the lane FORWARDS --
                # the same test `_bool_component_read` makes for an IF's
                # condition.  `0114_sv_f.frag`: the compiler's `.CC` (node 0.3)
                # has the TRUNC (0.1) as its source, with the local's store
                # (0.15) reading it too.  `G2S_NOSPLATFWD=1` reads the
                # local's lane whatever the kind.
                _bcf = self.cfw.get(_blc[0], {}).get(_blc[1])
                if (_bcf is not None and _bcf[0] == self._bkey()
                        and self.cfw_kind.get(_blc) not in ("insert",
                                                            "namecopy")
                        and not ENV.get("G2S_NOSPLATFWD")):
                    return _bcf[1], _bcf[2]
                return self.local_reg[_blc[0]], _blc[1]
            if (bd.args()[0] in self.local_reg and b in self.values
                    and not ENV.get("G2S_NOBOOLSPLAT")):
                # a WHOLE bool local reads as any whole load does: the value
                # stored in this block (`0100_sel_h.frag`: `MOV.U.CC RC.x, R1;`,
                # R1 the TRUNC, and the temp's store `MOV.U R1.x, R1;`
                # stays), else the name (`0100_sel_i.frag`, stored before an IF:
                # `MOV.U.CC RC.x, R0;`)
                return self.values[b], self.comps.get(b, (0, 1, 2, 3))[0]
            return None
        if b in self.normalised and b in self.values:
            return self.values[b], self.comps.get(b, (0, 1, 2, 3))[0]
        return None

    def _bool_construct_lanes(self, cond):
        """The construct's register when the condition is a bool vector
        built by OpCompositeConstruct in this block (not a splat, which
        `_bool_splat` takes), else None.  `G2S_NOBOOLCONLANES=1` refuses."""
        module = self.module
        d = module.result_insn.get(cond)
        if (d is None or d.opcode != Op.OpCompositeConstruct
                or ENV.get("G2S_NOBOOLCONLANES")):
            return None
        _v = self.values.get(cond)
        if (not isinstance(_v, str) or not _v.startswith("#")
                or cond in self.comps
                or self.con_blk.get(cond, self._bkey()) != self._bkey()):
            return None
        return _v

    def _vector_select(self, ins, cond, ta, fa):
        """A VECTOR select is not a branch: the front end writes the result
        from the false operand, sets the condition code from the bool vector
        at the result's mask, and overwrites the components whose condition
        holds with a predicated MOV (`0095_sel_c.frag`, `mix(uvec2(3u),
        uvec2(5u), lessThan(..).xy)`):
            MOV.U R2.xy, {3, 3, 0, 0};
            MOV.U.CC RC.xy, R0;
            MOV.U R2.xy(NE), {5, 5, 0, 0};
        and `0095_sel_d.frag` the same with inputs for the two operands.  The
        condition is the compare's normalised value read through its
        selector (`R0` for `.xy` of a four-component compare)."""
        _splat = self._bool_splat(cond)
        _lanes = (self._bool_construct_lanes(cond)
                  if _splat is None and not self._normalised_view(cond)
                  else None)
        if _splat is None and _lanes is None \
                and not self._normalised_view(cond) \
                and not self._bool_lane_view(cond):
            if ENV.get("G2S_SELDBG"):
                import sys as _s
                _d = self.module.result_insn.get(cond)
                print("SELDBG cond=%r val=%r comps=%r op=%r args=%r" % (
                    cond, self.values.get(cond), self.comps.get(cond),
                    getattr(_d, "opcode", None),
                    _d.args() if _d is not None else None), file=_s.stderr)
            raise NotEstablished(
                "a vector select on a bool vector that is not a normalised "
                "comparison")
        self._computation()
        _mv, _ds, _forms = self._select_arms(ins, ta, fa, True)
        _nc = _components(self.module, ins.result_type)
        if _splat is not None:
            # ONE BOOL FOR EVERY LANE (`bvec2(b)`): the condition code is
            # set in the one component the bool lives in, and the predicate
            # tests that component (`0100_sel_f.frag`: `MOV.U.CC RC.x, R0;` ..
            # `MOV.F R2.xy(NE.x), ..;`).
            _src, _k = _splat
            _cc = "MOV.%s.CC RC.%s, %s;" % (_BOOL_SUFFIX, _COMPONENTS[_k],
                                            _src)
            _pred = "(NE.%s)" % _COMPONENTS[_k]
        elif _lanes is not None:
            # A CONSTRUCTED BOOL VECTOR sets the condition code LANE BY LANE:
            # `0100_sel_g.frag`'s fold dump has the select's (0xa8) condition a
            # merge chain of four `.CC` MOVs (0x7e), each on its own lane's
            # write of the construct -- `MOV.U.CC RC.w, R1; .. RC.z ..` --
            # and the predicate the whole mask (notes/104 §9).  The four are
            # ONE condition vreg, each writing its lane (`nodedump`: vr 9 for
            # all four): the later lanes are marked `RC$+`, which the
            # numbering reads as "the set before's vreg" (notes/106)
            _cc = "\n".join("MOV.%s.CC RC%s.%s, %s;" % (
                _BOOL_SUFFIX, "$+" if _k else "", _COMPONENTS[_k], _lanes)
                for _k in range(_nc))
            _pred = "(NE)"
        else:
            _c = _source(self.values, self.comps, cond, _nc)
            if _c is None or _c.startswith(("-", "|", "{")):
                raise NotEstablished(
                    "a vector select whose condition has no form")
            _cc = _cc_move(_c, _ds)
            _pred = "(NE)"
            # A LOCAL FILLED LANE BY LANE sets the condition code ONE LANE
            # AT A TIME EVEN WITH NO SWIZZLE ON THE SOURCE.  The swizzled
            # case below already read this; `0089_sv_bl.frag` is the same
            # shape at the identity lanes and the oracle still prints
            #     MOV.U.CC RC.x, R1;
            #     MOV.U.CC RC.y, R1;
            # against the single `MOV.U.CC RC.xy, R1;` a LIVE comparison
            # gets (`0095_sel_c.frag`).  So the split is the condition's
            # PROVENANCE -- a name read against a comparison value -- not
            # whether its text carries a selector.  (notes/121 §2)
            if ("." not in _c and not self._normalised_view(cond)
                    and self._bool_lane_view(cond)
                    and not ENV.get("G2S_NOBAREBOOLLANE")):
                _cc = "\n".join(
                    "MOV.%s.CC RC%s.%s, %s;"
                    % (_BOOL_SUFFIX, "$+" if _j else "", _c2, _c)
                    for _j, _c2 in enumerate(
                        self._merge_lane_order(_c, "xyzw"[:_nc])))
            _base, _dot, _sw = _c.rpartition(".")
            if (_dot and _sw and len(_sw) >= _nc and set(_sw) <= set("xyzw")
                    and not ENV.get("G2S_CCSWIZZLESRC")):
                _lt = _sw[:_nc]
                if _lt != "xyzw"[:_nc]:
                    # A SELECTION OF THE BOOL VECTOR'S LANES: the `.CC` move
                    # copies the lanes read, in place, and the test swizzles
                    # -- `0114_sv_a.frag`'s `mix(.., .., b.xzw)` prints `MOV.U.CC
                    # RC.xzw, R0;` and `(NE.xzww)`, `sv_b` (`b.wy`) `RC.yw`
                    # and `(NE.wyzw)`, `sv_c` (`b.zzx`) `RC.xz` and
                    # `(NE.zzxw)`: the unused lanes pad with their own
                    # component (the slice's `map_daf6b9f2` and three more)
                    _mask = "".join(c for c in "xyzw" if c in _lt)
                    if (not self._normalised_view(cond)
                            and self._bool_lane_view(cond)):
                        # A LOCAL FILLED LANE BY LANE -- one this block did
                        # NOT store a comparison into whole, which is what
                        # `_normalised_view` tests -- sets the condition
                        # code ONE LANE AT A TIME, as `_lanes` does for a
                        # constructed bool vector: `chr_cloth_11e33b37`'s
                        # `uvec2(u_xlatb0.xz)` is `MOV.U.CC RC.x, R10;` and
                        # `MOV.U.CC RC.z, R10;`, not one `RC.xz`.  A bool
                        # vector that is ONE value keeps the combined move
                        # (`sv_a`, and the corpus's `MOV.U.CC RC.xzw, R4;`
                        # beside `(NE.xzww)`).
                        _cc = "\n".join(
                            "MOV.%s.CC RC%s.%s, %s;"
                            % (_BOOL_SUFFIX, "$+" if _j else "", _c2, _base)
                            for _j, _c2 in enumerate(
                                self._merge_lane_order(_base, _mask)))
                    else:
                        _cc = "MOV.%s.CC RC.%s, %s;" % (_BOOL_SUFFIX, _mask,
                                                        _base)
                    _pred = "(NE.%s)" % (_lt + "xyzw"[_nc:])
        _t = self._fresh(True)
        self.lines.append(_emit(_mv, _t + _ds, _forms[1]))
        self.lines.extend(_cc.split("\n"))
        self.lines.append(_emit(_mv, "%s%s%s" % (_t, _ds, _pred), _forms[0]))
        self.wants_cc = True
        self.values[ins.result] = _t

    def _select_arms(self, ins, ta, fa, predicated=False):
        """The arms' MOV, its mask, and each arm's operand.

        `predicated`: the PREDICATED form (`_vector_select`), whose arms are
        in the SAME block.  An arm that loads a local then takes the value
        forwarded from that local's store in this block (notes/65 §3), as
        every other read in the block does -- `vfx_multi_color_alpha_p.frag`
        predicates the LG2s' construct itself, where we wrote the local's
        name first and predicated that.  The BRANCH form's arms are blocks of
        their own and read the NAME at the block's entry (notes/81), which is
        what `0108_dt_v.frag` prints (`MOV.F R0.x, R0;` in its THEN arm).
        `G2S_SELARMNAME=1` reads the name in both."""
        module = self.module
        _nc = _components(module, ins.result_type)
        _tc = _glasm_type_code(module, ins.result_type)
        if _tc == BOOL_TYPE_CODE:
            _tc = _BOOL_REPR_CODE
        _mv = self._mov_for(_tc)
        _ds = _opchain.dest_suffix(_nc) if _nc else None
        if _mv is None or _ds is None:
            raise NotEstablished(
                "a select whose MOV or mask the image's rules do not give")
        _forms = []
        for _x in (ta, fa):
            if (_x in self.load_of
                    and not (predicated
                             and self.lfwd.get(self.load_of[_x][0],
                                               (None,))[0] == self._bkey()
                             and not ENV.get("G2S_SELARMNAME"))):
                _f = self.load_of[_x][1]
            else:
                _f = (_source(self.values, self.comps, _x, _nc)
                      or _constant_source(module, _x, _nc))
            if _f is None:
                raise NotEstablished("a select operand with no form")
            _forms.append(_f)
        return _mv, _ds, _forms

    # -- the geometry stage ---------------------------------------------------

    def _arm_emit_vertex(self, ins):
        """The reader lowers OpEmitVertex to a call of the library routine
        named "EmitStreamVertex" (0xfdcc1c -> f_7100fd6da0, the string at
        0x1145f6e); the shadowing pass hands it the shadows (notes/65).  It
        prints as ONE whole write of each output from its shadow -- `MOV.F
        result.position, R2;` -- and the EMIT (opcode 0x22), after which the
        walker opens a block (0xf0e550).  The write is not an assignment
        statement, so the walker's block test never runs for it (`calls`)."""
        if (ins.opcode != Op.OpEmitVertex
                or self.model != ExecutionModel.Geometry):
            return False
        if self.model_gs_pos is None:
            raise NotEstablished(
                "an EmitVertex with no gl_Position written before it")
        self.calls.append(len(self.lines))
        self.lines.append("MOV.F result.position, %s;" % self.model_gs_pos)
        self.lines.append("EMIT;")
        self.stores = 0
        return True

    def _arm_end_primitive(self, ins):
        """"EndStreamPrimitive" (0xfdcc2c, string 0x114e99e): ENDPRIM (opcode
        0x23), no operands."""
        if (ins.opcode != Op.OpEndPrimitive
                or self.model != ExecutionModel.Geometry):
            return False
        self.lines.append("ENDPRIM;")
        self.stores = 0
        return True

    # -- barriers -------------------------------------------------------------

    def _arm_barrier(self, ins):
        """`OpMemoryBarrier` and `OpControlBarrier`, READ off the four compute
        listings in the corpus that carry a barrier at all (notes/119 §1).

        `post_tonemap_histogram.comp` has, in SPIR-V order, MemoryBarrier
        (1, 264), ControlBarrier (2, 2, 264), and the same pair again; its
        listing has `MEMBAR.CTA;` `BAR ;` `MEMBAR.CTA;` .. `MEMBAR.CTA;`
        `BAR ;` `MEMBAR.CTA;` -- four `MEMBAR.CTA` against two `BAR`, so the
        CONTROL barrier prints a `BAR ;` AND a `MEMBAR.CTA;` after it, and
        the MEMORY barrier prints the one line.  `post_tonemap_update.comp`
        is the same at 16 against 8.

        `particle_fog_sort.comp` separates the two `MEMBAR` spellings: its
        MemoryBarrier carries semantics 3400 (image, atomic-counter,
        workgroup and uniform memory) and prints `MEMBAR;`, where 264 is
        WorkgroupMemory alone and prints `MEMBAR.CTA;` -- `.CTA` is the
        workgroup.  Its ControlBarrier (2, 2, 264) prints `BAR ;` and a
        `MEMBAR.CTA;`, as above.

        Only those two semantics values and that one ControlBarrier form are
        measured, so anything else refuses rather than picking a spelling.
        """
        if ins.opcode not in (Op.OpMemoryBarrier, Op.OpControlBarrier):
            return False
        _a = ins.args()
        # A CONTROL BARRIER WITH NO MEMORY SEMANTICS PRINTS NOTHING.
        # `water_00540147.tesc` and `water_1f555902.tesc` carry
        # `OpControlBarrier [2, 4, 0]` -- workgroup execution, INVOCATION
        # memory scope, semantics 0 -- and neither listing has a single
        # `BAR` or `MEMBAR` line.  (notes/118 §1)
        #
        # CONFOUNDED and recorded as such: the only listings with a barrier
        # that DOES print are compute shaders with semantics 264, so stage
        # and semantics vary together and one of them could be the reason.
        # Only this exact triple is dropped; anything else still refuses.
        if (ins.opcode == Op.OpControlBarrier
                and self._barrier_const(_a[0]) == _SCOPE_WORKGROUP
                and self._barrier_const(_a[1]) == _SCOPE_INVOCATION
                and self._barrier_const(_a[2]) == 0):
            return True
        _sem = self._barrier_semantics(_a[-1])
        if ins.opcode == Op.OpMemoryBarrier:
            self.lines.append(_sem)
            self.stores = 0
            return True
        # every operand is an ID of a constant, not a literal
        if (self._barrier_const(_a[0]) != _SCOPE_WORKGROUP
                or self._barrier_const(_a[1]) != _SCOPE_WORKGROUP):
            raise NotEstablished(
                "a control barrier at a scope other than the workgroup: only "
                "the workgroup form is in a listing")
        if _sem != "MEMBAR.CTA;":
            raise NotEstablished(
                "a control barrier whose memory semantics are not the "
                "workgroup's: only that form is in a listing")
        self.lines.append("BAR ;")          # the oracle's space is its own
        self.lines.append(_sem)
        self.stores = 0
        return True

    def _barrier_const(self, sid):
        """The value of a barrier operand, which is always a constant's id."""
        _c = self.module.constants.get(sid)
        if _c is None:
            raise NotEstablished(
                "a barrier whose scope or semantics is not a constant")
        return _c.args()[-1]

    def _barrier_semantics(self, sid):
        """The `MEMBAR` spelling for a barrier's memory-semantics constant."""
        _v = self._barrier_const(sid)
        _mem = _v & _MEM_SEMANTICS
        if _mem == _SEM_WORKGROUP_MEMORY:
            return "MEMBAR.CTA;"
        if _mem:
            return "MEMBAR;"
        raise NotEstablished(
            "a barrier with no memory class in its semantics (%d): not "
            "measured" % _v)

    # -- kills ----------------------------------------------------------------

    def _arm_kill(self, ins):
        """`KIL` is predicated like a branch, so it is preceded by a move into
        the condition register -- `MOV.U.CC RC.x, {1, 0, 0, 0};` with a
        constant TRUE, because the `if` has already tested the condition
        (`0011_fr_discard.frag`)."""
        if ins.opcode not in KILLS:
            return False
        self.lines.append(_cc_move(_bool_constant(True)))
        self.lines.append("KIL   NE.x;")
        if not ENV.get("G2S_KILBARRIER"):
            # ... and the KIL ENDS its block (notes/114 SS25): the `.CC`
            # move and the `KIL` are two nodes of one block in the
            # compiler's `g2s_st`, and the next statement opens the next.
            self.cuts.append(len(self.lines))
        self.wants_cc = True
        return True

    # -- calls and returns ----------------------------------------------------

    def _arm_call(self, ins):
        """notes/68, the emitter's arm for a call of a user function
        (0xf11be8..0xf1239c):
          * each `in` parameter is assigned the argument's value
            (0xf11c30..0xf11d28) -- a read of the argument variable, so the
            value it was stored with in this block;
          * a block is opened (f_7100f0bc10, 0xf11d44), the CAL (0x1d,
            0xf122f8) goes in it, and another is opened after it (0xf1232c);
          * the call's value is a read of the function's return name
            (0xf12340..0xf12358), and the SPIR-V result is a named temp
            holding it."""
        if ins.opcode != Op.OpFunctionCall:
            return False
        _f = self.callee_by_id.get(ins.args()[0])
        _argp = ins.args()[1:]
        if _f is None or len(_argp) != len(_f.params):
            raise NotEstablished("a call whose callee is not read")
        for _p, _a in zip(_f.params, _argp):
            self._pass_argument(_p, _a)
        self._flush()
        self.lines.append("CAL   BB@%d (TR);" % _f.result)
        self.stores = 0
        self.blk_no += 2
        self._expire_local_forwards()
        self.blk_node = False
        self.node_next = False
        # A VOID CALL HAS NO RETURN NAME.  `water_00540147.tesc` prints its
        # two calls as the bare `CAL   BB8 (TR);` / `CAL   BB14 (TR);` with
        # no MOV of a return value after either -- the value steps at
        # 0xf12340..0xf12358 have nothing to read.  (notes/118 §1)
        _rt = self.module.types.get(ins.result_type)
        if _rt is not None and _rt.opcode == Op.OpTypeVoid:
            self.retreg[_f.result] = None
            return True
        _nres, _ds = self._width_suffix(ins.result_type)
        _mv = self._mov_for(_glasm_type_code(self.module, ins.result_type))
        if _mv is None or _ds is None:
            raise NotEstablished("a call whose value's MOV is not given")
        _T = self._fresh(True)
        _R = self.retreg[_f.result] = self._fresh(True)
        self.callnames.update((_T, _R))
        self.values[ins.result] = _T
        self.head_src[ins.result] = _R
        self.stmtpos[_T] = len(self.lines)
        self.flush_q.append((_T, _mv, _ds, None, None, _R))
        self.defblk[ins.result] = -1
        return True

    def _pass_argument(self, _p, _a):
        _pm, _pd = _parameter_mov(self.module, _p)
        if _a not in self.locals_ or _a not in self.local_reg:
            raise NotEstablished("a call argument that is not a stored local")
        _fw = self.lfwd.get(_a)
        _av = (_fw[1] if _fw is not None and _fw[0] == self._bkey()
               else self.local_reg[_a])
        _F = self.formal[_p.result] = self._fresh(True)
        self.callnames.add(_F)
        self.lines.append(_emit(_pm, _F + _pd, _av))

    def _arm_return_value(self, ins):
        """The return arm (0xf1473c..0xf147d4): the function's return name is
        assigned the value, then RET (0xf148a8), and a block is opened."""
        if ins.opcode != Op.OpReturnValue:
            return False
        module = self.module
        _f = self.cur_fn
        _val = ins.args()[0]
        _R = self.retreg.get(_f.result) if _f is not None else None
        if _R is None:
            raise NotEstablished("a return value outside a called function")
        _vi = module.result_insn.get(_val)
        _nres = _components(module, _vi.result_type
                            if _vi is not None and _vi.has_result_type
                            else _f.type_id)
        _v = self.values.get(_val) or _constant_source(module, _val, _nres)
        if _v is None or _nres is None:
            raise NotEstablished("a return value with no form")
        _vt = (_vi.result_type if _vi is not None and _vi.has_result_type
               else module.constants[_val].result_type)
        _mv = self._mov_for(_glasm_type_code(module, _vt))
        _ds = _opchain.dest_suffix(_nres)
        if _mv is None or _ds is None:
            raise NotEstablished("a return value's MOV is not given")
        self.lines.append(_emit(_mv, _R + _ds, _v))
        if (_is_placeholder(_v) and self.defblk.get(_val) == self.blk_no
                and self.defline.get(_val) is not None
                and _vi.opcode != Op.OpLoad):
            # the value is a named temp of this block: the return reads it
            # forwarded, and the temp's own store is flushed
            _dl = self.defline[_val]
            _grp = list(range(_dl, len(self.lines)))
            self.ties.append(_grp)
            self.stmtpos[_v] = _dl
            self.flush_q.append((_v, _mv, _ds, _grp, _dl))
        self._flush()
        self.lines.append(_RET)
        return True

    def _arm_unreachable(self, ins):
        """`OpUnreachable` TERMINATES A DEAD BLOCK and prints nothing
        (notes/114 \u00a753).  `debug_hiz-1.frag` has it as the terminator of
        the block glslang opens after an `OpReturn`, which nothing branches
        to; the compiler's listing has no line for it and no label.
        `G2S_NOUNREACHABLE=1` refuses it again."""
        if ins.opcode != Op.OpUnreachable or ENV.get("G2S_NOUNREACHABLE"):
            return False
        return True

    def _arm_return(self, ins):
        if ins.opcode != Op.OpReturn:
            return False
        self._flush()
        self.lines.append(_RET)
        return True
