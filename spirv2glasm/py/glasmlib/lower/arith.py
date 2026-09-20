"""arith.py -- arithmetic: binary ops, divides, shifts, dots, conversions.

The mnemonic is DERIVED, not fitted: notes/33..37 and py/opchain.py take the
SPIR-V opcode through the worker's operator code, the lowering's family base,
the emitter's opcode table and the image's type-suffix table.  `mnemonic()`
returns None wherever a link is missing, and that refuses the shader rather
than inventing a name.  What is measured here is the SHAPE: which
instructions an operation becomes, and how their operands print.
"""
import lex as _lex

from spvnames import Op, GLSL450

import opchain as _opchain
import sched as _sched
from glasmlib.common import NotEstablished, ENV, OP_NAME, COMPOSITE_TYPES
from glasmlib.types import _components, _glasm_type_code, _signedness, \
    _vi_is_scalar
from glasmlib.operands import _scalar_value, _constant_operand, \
    _constant_source, _type_width
from glasmlib.text import _COMPONENTS, _emit, _swizzle, _source
from glasmlib.boolean import _bool_normalise, _COMPARISON_OPERATORS, \
    _BOOL_REPR_CODE
from glasmlib.lower.core import _is_placeholder
from glasmlib import nodes

_IDENTITY = (0, 1, 2, 3)
# A scalar operand read by a wider op broadcasts through `.x`: a temp, an
# input attribute, or a scalar constant forwarded from a local's store
# (`lc_a.frag`'s `u_xlat1 = 3.0; .. u_xlat0 * u_xlat1`: `MUL.F32 R0, R0,
# {3, 0, 0, 0}.x;`, the form a constant operand takes directly).
# (lex.py `is_broadcastable`)



def _lead_placeholder(text):
    """`^[-|]*(#\d+)` matched: the placeholder, or None."""
    t = text.lstrip("-|")
    e = _lex.digit_end(t, 1)
    return t[:e] if t.startswith("#") and e > 1 else None


def _writer_mnemonic(line, name):
    """`^\s*(\S+)\s+NAME(?:\.[xyzw]+)?\s*,` matched: the mnemonic, or
    None."""
    f = _lex.split_first(line)
    if f is None or f[2] == f[1] or not line.startswith(name, f[2]):
        return None
    n = len(line)
    e = f[2] + len(name)
    if e < n and line[e] == ".":
        g = e + 1
        while g < n and line[g] in _lex.XYZW:
            g += 1
        if g > e + 1:
            e = g
    while e < n and line[e] in _lex.WS:
        e += 1
    return line[f[0]:f[1]] if e < n and line[e] == "," else None


# INTEGER ARITHMETIC AND BITWISE OPS.  The mnemonics are NOT a table: the
# image's chain names every one of them, exactly as it does the float ones
# (notes/33, notes/38), and the suffix rule supplies the signedness --
#
#     OpIAdd       -> 131   ADD      OpBitwiseOr   -> 146   OR
#     OpISub       -> 162   (the SUB peephole)
#     OpIMul       -> 144   MUL      OpBitwiseXor  -> 163   XOR
#                                    OpBitwiseAnd  -> 132   AND
#
# so this set only says WHICH opcodes have been checked against a listing.
# `OpISub` is the one worth naming: the chain says opcode 0xa2 rewritten by
# the peephole of notes/38 into `ADD` with a negated second source, and the
# probe agrees -- `ADD.S R0, fragment.attrib[0], -fragment.attrib[1];`.
_INT_BINARY = frozenset((Op.OpIAdd, Op.OpISub, Op.OpIMul, Op.OpBitwiseOr,
                         Op.OpBitwiseXor, Op.OpBitwiseAnd))

# OPCODES THE CHAIN NAMES WHOSE SHAPE IS NOT ONE WHOLE-VECTOR INSTRUCTION.
# Measured, not assumed: `int_ishl_i.frag`, `int_ishr_i.frag` and
# `int_ushr_u.frag` print four scalar lines each, component `w` first; and
# OpSDiv, from `int_idiv.frag`'s emit list: four nodes of ONE opcode -- 0x87,
# which the namer spells `DIV.S` -- with destination masks 0xff000000,
# 0xff0000, 0xff00, 0xff, one vreg between them and one `node[36]` across all
# four.  An INTEGER divide is scalarised; a float divide is the RCP + MUL
# shape of `op_div.vert` instead, which is why they share an IR opcode (0x85)
# and not a lowering.
_SCALARISED = frozenset((Op.OpShiftRightLogical, Op.OpShiftRightArithmetic,
                         Op.OpShiftLeftLogical, Op.OpSDiv))

_CONVERT = {Op.OpConvertSToF: "I2F.S", Op.OpConvertUToF: "I2F.U"}
_FLOAT_TO_INT = (Op.OpConvertFToU, Op.OpConvertFToS)

# The commutative add and multiply, whose constant operand goes second.
# The binary ops the canonicaliser f_710005f810 orders (notes/102 §6): for
# ADD, AND, MUL and OR (with DP2..4, MIN, MAX, POW, MAD, DP2A elsewhere) a
# CONSTANT source goes second (0x5f890..0x5f8a0).  `cn_b.frag`: `12 & a.x`
# prints `AND.S R1.x, fragment.attrib[0], {12, 0, 0, 0};`.
_COMMUTATIVE = (Op.OpIAdd, Op.OpFAdd, Op.OpIMul, Op.OpFMul,
                Op.OpBitwiseAnd, Op.OpBitwiseOr)
_MULTIPLIES = (Op.OpIMul, Op.OpFMul)

# THE FLOAT BINARY OPERATIONS a table once gave from listings: `OpFSub` is
# `ADD.F32 R0, a, -b;`.  The table was fitted and is not emitted; an opcode
# here that the chain does not name is refused as such.
_BINARY = frozenset((Op.OpFAdd, Op.OpFSub, Op.OpFMul))

# STRUCTURED CONTROL FLOW's float comparisons: a table read off listings
# (`SGT.F32` in `cf_if.vert`, `SLT.F32` in `fr_discard.frag`).  Fitted, so a
# comparison the chain does not name is refused.
_COMPARE = frozenset((Op.OpFOrdGreaterThan, Op.OpFOrdGreaterThanEqual,
                      Op.OpFOrdLessThan, Op.OpFOrdLessThanEqual,
                      Op.OpFOrdEqual, Op.OpFOrdNotEqual))

_DERIVATIVES = (Op.OpDPdx, Op.OpDPdy, Op.OpFwidth)


def _drop_x_for_scalar(a, nres):
    """`.x` is the identity for a scalar result, never printed; a bare
    component-0 source read by a wider op broadcasts."""
    if a is not None and nres and nres > 1 and _is_placeholder(a):
        return a + ".x"
    if a is not None and nres == 1 and a.endswith(".x"):
        return a[:-2]
    return a


def _toggle_negate(a):
    """Toggling, not prefixing: the negate arm XORs the bit, so a negate of
    a negate is the operand itself."""
    return a[1:] if a.startswith("-") else "-" + a


class ArithOps(object):

    def _mnemonic_of(self, opcode, type_code):
        m = _opchain.mnemonic_for_opcode(opcode, type_code)
        return None if m is None or m.startswith("<") else m

    # -- the matrix multiply ------------------------------------------------

    def _arm_matrix_times_vector(self, ins):
        """ONE BLOCK PER COLUMN, and within a block the order is forced by
        the dependence chain: `if_mat.vert`'s stamps give four blocks, each
        holding `0x3b` (the load), `0x47` (a copy of it), `0x90` (the column
        scaled by the vector's component) and, past the first, `0x83`
        accumulating into the running sum.

        A REGISTER EACH, but NOT band temps: every one of these dies into the
        next line, and `if_mat.vert` is `2 R-regs` -- the running sum and the
        column being loaded.  A band temp is live to the end of its block and
        would take twelve."""
        if ins.opcode != Op.OpMatrixTimesVector:
            return False
        self._computation()
        args = ins.args()
        _mt = self.matrices.get(args[0])
        _vec = _source(self.values, self.comps, args[1])
        if _mt is None or _vec is None:
            raise NotEstablished(
                "a matrix multiply whose operands have no form")
        _lmn, _lnm, _loff, _nc, _cb = _mt
        _tc = _glasm_type_code(self.module, ins.result_type)
        _mv = self._mnemonic_of(nodes.MOV, _tc)
        _mm = self._mnemonic_of(nodes.MUL, _tc)
        _ma = self._mnemonic_of(nodes.ADD, _tc)
        if _mv is None or _mm is None or _ma is None:
            raise NotEstablished(
                "a matrix multiply whose opcodes the image's tables do not "
                "name for this type")
        dst = None
        for _c in range(_nc):
            if _c:
                self.cuts.append(len(self.lines))
            _l = self._fresh(is_wide=True)
            self.lines.append("%s %s, %s[%d];"
                              % (_lmn, _l, _lnm, _loff + _c * _cb))
            _m = self._fresh(is_wide=True)
            self.lines.append(_emit(_mv, _m, _l))
            _s = "%s.%s" % (_vec, _COMPONENTS[_c])
            if _c == 0:
                dst = self._fresh(is_wide=True)
                self.lines.append(_emit(_mm, dst, _s, _m))
            else:
                _p = self._fresh(is_wide=True)
                self.lines.append(_emit(_mm, _p, _s, _m))
                self.lines.append(_emit(_ma, dst, dst, _p))
        self.values[ins.result] = dst
        return True

    # -- negates ----------------------------------------------------------

    def _carrier_move(self, ins, operand, refusal):
        """The negate's carrier MOV: `MOV dst, -operand;` into a band temp,
        the value marked negated."""
        _nres = _components(self.module, ins.result_type)
        a = _drop_x_for_scalar(self._con_read(operand), _nres)
        a = a or _source(self.values, self.comps, operand, _nres)
        if a is None:
            raise NotEstablished(refusal[0])
        _tc = _glasm_type_code(self.module, ins.result_type)
        _mv = self._mnemonic_of(nodes.MOV, _tc)
        _ds = _opchain.dest_suffix(_nres) if _nres else None
        if _mv is None or _ds is None:
            raise NotEstablished(refusal[1])
        dst = self._fresh(True)
        self.lines.append(_emit(_mv, dst + _ds, _toggle_negate(a)))
        self.values[ins.result] = dst
        self.negated.add(ins.result)

    def _arm_negating_multiply(self, ins):
        """A MULTIPLY BY MINUS ONE IS A NEGATE (notes/83): the folder turns
        `x * -1` into the negate node, and that prints the negate's carrier
        MOV (`_arm_negate`), integer and float alike.  `pn_a.frag`: `k * -1`
        -> `MOV.S R0.x, -fragment.attrib[0];`, `a.x * -1.0` -> `MOV.F R2.x,
        -fragment.attrib[1];`, while `k * -2` stays a `MUL.S`.
        `map_15393bbe`'s `int(b) * int(0xffffffffu)` is one."""
        if ins.opcode not in _MULTIPLIES or len(ins.args()) != 2:
            return False
        _m1 = [_x for _x in ins.args()
               if _scalar_value(self.module, _x) in ("-1", "-1.0")]
        if not _m1 or ENV.get("G2S_NONEGMUL"):
            return False
        _other = [_x for _x in ins.args() if _x not in _m1]
        _other = _other[0] if _other else ins.args()[0]
        self._carrier_move(ins, _other, (
            "a multiply by -1 whose operand has no form",
            "a negating multiply whose MOV or mask the image's rules do not "
            "give"))
        return True

    def _arm_negate(self, ins):
        """notes/42: a negate emits NO INSTRUCTION of its own.  Its handler's
        operator code is 0x17 (notes/24), the lowering gives family base 0x21
        (notes/lowering_map.json), and the emitter's arm for 0x21/0x22 is

            f1118c:  f_7100f10a30(cg, node[48], out)   ; the OPERAND
            f1119c:  out[8] ^= 1                       ; toggle NEGATE
            f111a8:  return

        so the minus sign rides on whatever consumes the value -- the SOURCE
        OPERAND SLOT's modifier (0 none, 1 negate, 2 absolute value, notes/47).

        BUT THE VALUE IS A NAMED TEMP (notes/66 §3, notes/67 §9): the reader
        makes `t = -x` a statement, and the assignment's carrier MOV
        (0xf11530) takes the modified operand.  A carrier folds into its
        source only when the read has no modifier (f_7100032c30), so this one
        stays, and every consumer reads the MOV: `sc_negadd.frag` prints
            MOV.F R0, -fragment.attrib[0];
            ADD.F32 R1, R0, {0.5, 0.5, 0.5, 0.5};
        No `_computation` refusal: this MOV is the one the stores used to make
        themselves, and where it goes among other stores is the scheduler's
        (the store ties it, notes/56)."""
        if ins.opcode not in (Op.OpSNegate, Op.OpFNegate):
            return False
        self._carrier_move(ins, ins.args()[0], (
            "a negate whose operand has no form",
            "a negate whose carrier MOV or mask the image's rules do not "
            "give"))
        return True

    # -- the derived binary ops ----------------------------------------------

    def _derived(self, ins):
        """The chain's mnemonic for the instruction, or None.

        A comparison's suffix type is its OPERANDS' -- the emitter's arm puts
        the bool in node[44], not node[24] (notes/34, notes/40).  Returns
        (mnemonic, the result's scalar type insn)."""
        module = self.module
        op = ins.opcode
        _tid = getattr(ins, "result_type", None)
        _rt = module.types.get(_tid) if _tid is not None else None
        while _rt is not None and _rt.opcode in COMPOSITE_TYPES:
            _rt = module.types.get(_rt.operands[1])
        if _rt is not None and _rt.opcode == Op.OpTypeBool:
            _d0 = module.result_insn.get(ins.args()[0]) if ins.args() \
                else None
            _tid = (_d0.result_type if _d0 is not None
                    and getattr(_d0, "has_result_type", False) else None)
        name = OP_NAME.get(op, "")
        derived = (_opchain.mnemonic(name, _glasm_type_code(module, _tid))
                   if _tid is not None
                   and _opchain.OPERATOR.get(name) is not None else None)
        if derived is None and op == Op.OpSDiv and _tid is not None:
            # The chain's own opcode for `OpSDiv` is 0x85, which no namer
            # spells; the lowering's is 0x87, read off the emit list.
            derived = self._mnemonic_of(nodes.DIV,
                                        _glasm_type_code(module, _tid))
        return derived, _rt

    def _arm_arithmetic(self, ins):
        op = ins.opcode
        derived, _rt = self._derived(ins)
        if derived is not None and op in _SCALARISED:
            self._scalarised(ins, derived)
            return True
        if (op == Op.OpExtInst and len(ins.args()) >= 2
                and ins.args()[1] == GLSL450.FClamp):
            self._clamp(ins)
            return True
        if op in (Op.OpFDiv, Op.OpFMod):
            self._divide(ins)
            return True
        if derived is not None:
            self._binary(ins, derived, _rt)
            return True
        if op in _BINARY:
            raise NotEstablished(
                "opcode %d: the image's chain does not name it and the table "
                "that did was read off a listing (_BINARY)" % op)
        return False

    def _scalarised(self, ins, derived):
        """THE CHAIN NAMES IT AND THE SHAPE IS NOT ONE INSTRUCTION.  A shift
        is emitted ONE COMPONENT AT A TIME, highest first --

            SHL.S R0.w, fragment.attrib[0], fragment.attrib[1].w;
            SHL.S R0.z, fragment.attrib[0], fragment.attrib[1].z;
            ...

        EACH LANE'S OPERANDS (notes/87), measured on `sh_a` (scalar, a
        constant count), `sh_b` (ivec2, a vector constant) and `sh_c`
        (scalar, two attribute components):
          * operand 0 carries the lane's own mask with the value's selector:
            bare where lane c reads component c (`SHL.S R3.y,
            fragment.attrib[0], ..`), the component otherwise (`SHL.S R2.x,
            fragment.attrib[0].z, ..`);
          * operand 1 carries a single-component selector and always prints
            it: `{5, 0, 0, 0}.x`, `{3, 4, 0, 0}.y`, `fragment.attrib[0].y`.

        EMITTED LOW COMPONENT FIRST.  On `int_ishl_i.frag` the four `0x9b`
        nodes carry destination masks `0xff000000`, `0xff0000`, `0xff00`,
        `0xff` IN THAT ORDER -- all naming ONE vreg -- and all four carry
        `seq=1`, so pass 1 is what reverses them, and what this owes the
        scheduler is the CREATION order.  `G2S_NOSCHED2=1` prints this
        descending instead."""
        module = self.module
        op = ins.opcode
        self._computation()
        args = ins.args()
        if len(args) != 2:
            raise NotEstablished("a shift with %d operands" % len(args))
        _nres = _components(module, ins.result_type)
        _a0 = self.values.get(args[0])
        _b0 = self.values.get(args[1])
        _bc = _constant_operand(module, args[1]) if _b0 is None else None
        if (_a0 is None or (_b0 is None and _bc is None)
                or _a0.startswith(("-", "|", "{"))
                or (_b0 is not None and _b0.startswith(("-", "|", "{")))
                or not _nres or _nres > 4):
            raise NotEstablished(
                "opcode %d: emitted one component at a time, and this one's "
                "operands are not a value and a value or constant" % op)
        _cma = self.comps.get(args[0], _IDENTITY)
        _cmb = self.comps.get(args[1], _IDENTITY)
        _bsc = _bc is not None and _components(
            module, module.constants[args[1]].result_type) == 1

        def _lane_a(c):
            return _a0 if _cma[c] == c else "%s.%s" % (
                _a0, _COMPONENTS[_cma[c]])

        def _lane_b(c):
            if _bc is not None:
                return "%s.%s" % (_bc, _COMPONENTS[0 if _bsc else c])
            return "%s.%s" % (_b0, _COMPONENTS[_cmb[c]])
        dst = self._fresh(True)
        _tie = []
        for _c in (tuple(range(_nres - 1, -1, -1))
                   if ENV.get("G2S_NOSCHED2") else tuple(range(_nres))):
            _tie.append(len(self.lines))
            self.lines.append(_emit(derived, "%s.%s" % (dst, _COMPONENTS[_c]),
                                    _lane_a(_c), _lane_b(_c)))
        self.ties.append(_tie)
        self.values[ins.result] = dst
        if _nres == 1:
            self.comps[ins.result] = (0, 0, 0, 0)
            self.scalar.add(ins.result)

    def _binary_operands(self, ins, args, _nres):
        """The two operands, each with its selector against the result."""
        module = self.module
        _ca, _cb = self._con_read(args[0]), self._con_read(args[1])
        if _nres and _nres > 1:
            # a bare component-0 source read by a wider op broadcasts
            _ca = _ca + ".x" if _ca and _is_placeholder(_ca) else _ca
            _cb = _cb + ".x" if _cb and _is_placeholder(_cb) else _cb
        elif _nres == 1:
            # `.x` is the identity for a scalar result, never printed
            _ca = _ca[:-2] if _ca and _ca.endswith(".x") else _ca
            _cb = _cb[:-2] if _cb and _cb.endswith(".x") else _cb
        a = (_ca or _source(self.values, self.comps, args[0], _nres)
             or _constant_source(module, args[0], _nres))
        b = (_cb or _source(self.values, self.comps, args[1], _nres)
             or _constant_source(module, args[1], _nres))
        if a is None or b is None:
            raise NotEstablished("a binary op whose operand has no form")
        # A ONE-COMPONENT VALUE READ BY A WIDER OP broadcasts: its selector is
        # slot 0 for every component, printed `.x` like any swizzle against
        # the result's width (`pu_b.vert`'s `a * s`: `MUL.F32 R0,
        # vertex.attrib[0], R1.x;`, R1 the scalar LDC).  Values with a
        # component map already print it through `_source`; a plain scalar
        # register had none -- and a scalar INPUT neither (`lp_of.vert`'s
        # `t * n`, `n` a float attribute, prints `vertex.attrib[2].x`).
        if _nres and _nres > 1:
            a, b = [self._broadcast(_v, _t, _nres)
                    for _v, _t in ((args[0], a), (args[1], b))]
        return a, b

    def _reads_rsq_node(self, text):
        """Does the operand `text` read an RSQ NODE itself -- a placeholder
        whose latest write in the current block is an RSQ line?  A value
        forwarded in its block is its node; a name read in a later block is
        a register read (0x2b), and a placeholder written again after the
        RSQ is that later write's."""
        name = _lead_placeholder(text or "")
        if name is None:
            return False
        for _k in range(len(self.lines) - 1, self._blk_start() - 1, -1):
            _w = _writer_mnemonic(self.lines[_k], name)
            if _w is not None:
                return _w.split(".")[0] == "RSQ"
        return False

    def _broadcast(self, vid, text, nres):
        if (nres and nres > 1 and vid not in self.comps
                and _vi_is_scalar(self.module, vid)
                and _lex.is_broadcastable(text)):
            return text + ".x"
        return text

    def _binary(self, ins, derived, _rt):
        module = self.module
        op = ins.opcode
        self._computation()
        args = ins.args()
        if len(args) != 2:
            raise NotEstablished("a binary op with %d operands" % len(args))
        if (op in _COMMUTATIVE and args[0] in module.constants
                and args[1] not in module.constants
                and not ENV.get("G2S_NOCONSTSECOND")):
            # A CONSTANT GOES SECOND in a commutative add or multiply
            # (notes/90): `bv_n60.vert`'s `vec2(-1.0, -1.0) + vpSize_g.xy`
            # prints `ADD.F32 R2.xy, R1, {-1, -1, 0, 0};`, and the divide's
            # `vec2(1.0) / a.xy` multiplies `R0, {1, 1, 0, 0}` (`dv_c.frag`).
            args = [args[1], args[0]]
        _nres = _components(module, ins.result_type)
        a, b = self._binary_operands(ins, args, _nres)
        if (_opchain.glasm_opcode(OP_NAME.get(op, "")) == nodes.MUL
                and self._reads_rsq_node(b) and not self._reads_rsq_node(a)
                and not ENV.get("G2S_NORSQFIRST")):
            # AN RSQ GOES FIRST in a multiply: the canonicaliser's MUL arm
            # (f_710005f810, 0x5f8e4..0x5f90c) swaps the two slots
            # (0x5fa10) when source 1's node is an RSQ (0x7c) and source 0's
            # is not.  `map_9a0b11a0`'s `u_xlat4.xyz * u_xlat5.xxx`, right
            # after `u_xlat5.x = inversesqrt(..)`, enters it as (0x2b, 0x7c)
            # and a hardware watch on the slot sees the swap there: `MUL.F32
            # R22.xyz, R13.x, R19;`.  `ms_a.frag`, whose `.xxx` is a name
            # read (0x2b, 0x2b), keeps its order.
            a, b = b, a
        # notes/41: the destination's write mask is the result's component
        # count, and the printer turns it into this swizzle.
        _ds = _opchain.dest_suffix(_nres) if _nres else None
        if _ds is None:
            raise NotEstablished(
                "a binary op whose destination mask the image's rule does "
                "not give")
        # Each SPIR-V value instruction contributes ONE band temp (notes/52,
        # tools/bandsize.py), and the band size is the register count on
        # every straight-line probe.
        dst = self._fresh(True)
        # notes/38: the SUB peephole prints its second source negated.
        _opc = _opchain.glasm_opcode(OP_NAME.get(op, ""))
        _neg = "-" if _opchain.rewrite(_opc)[1] else ""
        if (_rt is not None and _rt.opcode == Op.OpTypeBool
                and _opchain.OPERATOR.get(OP_NAME.get(op, ""))
                in _COMPARISON_OPERATORS):
            # A COMPARISON WITH A BOOL RESULT is normalised where it is made:
            # f_7100030e20 appends the move into the bool representation to
            # every such compare (notes/64 §6), whatever reads it.  The
            # compare's own node is a lowering temp; the move's destination
            # is the value's (`cf_loop`: SLT vreg 9, the MOV vreg 4;
            # `sc_select.frag`: SLT R0, TRUNC R3).
            _cmp = self._fresh()
            self.lines.append(_emit(derived, _cmp + _ds, a, _neg + b))
            self.lines.extend(_bool_normalise(module, ins.result, _cmp, dst,
                                              _ds))
            self.normalised.add(ins.result)
        else:
            self.lines.append(_emit(derived, dst + _ds, a, _neg + b))
        self.values[ins.result] = dst

    # -- clamp --------------------------------------------------------------

    def _clamp(self, ins):
        """GLSL.std.450 FClamp.  MEASURED, not read off a listing: the emit
        list of `un_clamp.vert` is TWO nodes, `0x8e` then `0x8d`, both
        full-mask (notes/extinst_shape.json), and the image's namer gives
        `MIN.F` and `MAX.F` for them.  `clamp(x, lo, hi)` is `max(lo,
        min(hi, x))`, which is the only decomposition into a min then a max,
        so the operands follow from the opcodes.

        BOTH destinations are BAND temps: `tools/bandsize.py` measures
        FClamp's contribution as 2, and on every straight-line probe the band
        size is the register count (notes/52).

        THE WRITE MASK IS THE RESULT'S WIDTH (notes/41), and a CONSTANT GOES
        SECOND in the commutative MIN and MAX as in an add or multiply
        (notes/90 §2): `bl_280.vert`'s `clamp(u_xlat24, 0.0, 1.0)` prints
        `MIN.F R4.x, R3, {1, 0, 0, 0};` and `MAX.F R2.x, R4, {0, 0, 0, 0};`
        (notes/91)."""
        module = self.module
        self._computation()
        args = ins.args()[2:]
        _nres = _components(module, ins.result_type)
        forms = [_source(self.values, self.comps, a, _nres)
                 or _constant_source(module, a, _nres) for a in args]
        if len(forms) != 3 or any(f is None for f in forms):
            raise NotEstablished("a clamp whose operand has no form")
        _tc = _glasm_type_code(module, ins.result_type)
        _mn = self._mnemonic_of(nodes.MIN, _tc)
        _mx = self._mnemonic_of(nodes.MAX, _tc)
        if _mn is None or _mx is None:
            raise NotEstablished(
                "a clamp whose two opcodes the image's tables do not name for "
                "this type")
        _dsc = _opchain.dest_suffix(_nres) if _nres else None
        if _dsc is None:
            raise NotEstablished(
                "a clamp whose write mask the image's rule does not give")

        def _cs(p, q):
            if (p.startswith("{") and not q.startswith("{")
                    and not ENV.get("G2S_NOCONSTSECOND")):
                return q, p
            return p, q
        _t = self._fresh(True)
        self.lines.append(_emit(_mn, _t + _dsc, *_cs(forms[2], forms[0])))
        dst = self._fresh(True)
        self.lines.append(_emit(_mx, dst + _dsc, *_cs(forms[1], _t)))
        self.values[ins.result] = dst

    # -- divides --------------------------------------------------------------

    def _scalar_divide(self, tid, dst, a, b):
        """A ONE-COMPONENT float divide, read (notes/67):

          * the emitter gives a divide opcode 0x85 (IR family 0x41, 0xf10b28);
          * the walker callback f_710005ff20 replaces a float 0x85 by an RCP
            (0x7b) of the divisor with the divide's mask, and a MUL (0x90) of
            the dividend by it (0x5ff9c..0x60104); f_7100060110 leaves an RCP
            whose mask selects fewer than two distinct source components
            alone (0x60270), so a one-component RCP stays one node;
          * f_7100068fd0 folds MUL(x, RCP(y)) -- either slot -- into DIV
            (0x87, 0x690d8) with the MUL's other operand first and the RCP's
            operand second, when the RCP carries no flag at [52], the two
            source modifiers combine (none/negate/abs) and the profile
            accepts the sum of the two output scales (f_7100030c2c: the sum
            must be 0, which it is -- nothing here sets a scale).
        """
        tc = _glasm_type_code(self.module, tid)
        mn = _opchain.mnemonic_for_opcode(nodes.DIV, tc)
        if tc != nodes.F32 or mn is None or mn.startswith("<"):
            raise NotEstablished("a one-component divide that is not F32")
        # THE SECOND SOURCE PRINTS ITS COMPONENT, READ (notes/102 §4): the
        # line printer f_710005c1dc's case for DIVSQ, DIV, SHL and SHR
        # (jump-table entry 0xaf, code at 0x5c4f0) prints source 0 with the
        # flag clear and source 1 with it set (0x5c788: w5 = 1, w6 = 1); the
        # source printer f_710003e48c hands `w6 & 1` to the swizzle printer
        # (vt[192], f_710003e9c0), which then names the component even for
        # the identity.  `dv_d.frag`: `DIV.F32 R2.x, fragment.attrib[0],
        # R1.x;` (`map_601ecbf6`'s `x / U.s`: `R2.x`, the scalar LDC)
        if (_lex.is_broadcastable(b) and not b.startswith(("{", "-{"))
                and not ENV.get("G2S_BAREDIVISOR")):
            b = b + ".x"
        return _emit(mn, dst + ".x", a, b)

    def _divide(self, ins):
        """MEASURED from the emit list of `op_div.vert`, not from its
        listing: a divide is FIVE nodes -- four `0x7b` with destination masks
        0xff000000, 0xff0000, 0xff00, 0xff in that order, each reading ONE
        component of the divisor (`sel 0x302010<c>/0xff`), then one `0x90`
        with a full mask whose operands are the dividend and the gathered
        reciprocal.  The image's namer gives `RCP.F32` and `MUL.F32` for
        those two opcodes; neither name nor shape is read off a listing.
        The two carry DIFFERENT vregs (5 and 2) that both land on symbol 512,
        i.e. one register: the reciprocal dies at the multiply."""
        module = self.module
        op = ins.opcode
        self._computation()
        args = ins.args()
        _nres = _components(module, ins.result_type)
        a = (_source(self.values, self.comps, args[0], _nres)
             or _constant_source(module, args[0], _nres))
        b = (_source(self.values, self.comps, args[1], _nres)
             or _constant_source(module, args[1], _nres))
        if len(args) != 2 or a is None or b is None:
            raise NotEstablished("a divide whose operand has no form")
        if op == Op.OpFDiv and _nres == 1:
            # ONE COMPONENT: the reciprocal and the multiply fold into DIV
            # (notes/67).  `sc_div.frag`:
            #     DIV.F32 R0.x, fragment.attrib[0], fragment.attrib[0].y;
            dst = self._fresh(True)
            self.lines.append(self._scalar_divide(ins.result_type, dst, a, b))
            self.values[ins.result] = dst
            return
        _b0 = self.values.get(args[1])
        _bconst = _constant_operand(module, args[1]) if _b0 is None else None
        if (op == Op.OpFDiv and 2 <= _nres <= 4
                and (_b0 is not None or _bconst is not None)
                and not (_b0 or "").startswith(("-", "|", "{"))
                and not a.startswith(("-", "|"))
                and not ENV.get("G2S_DIV4ONLY")):
            self._vector_divide(ins, a, _b0, _bconst, _nres)
            return
        self._divide_four(ins, a, b, _nres)

    def _vector_divide(self, ins, a, _b0, _bconst, _nres):
        """THE GENERAL VECTOR DIVIDE (notes/90), by notes/67's reading: the
        divide is an RCP of the divisor with the divide's mask and a MUL of
        the dividend by it (f_710005ff20); f_7100060110 leaves an RCP whose
        source selects FEWER THAN TWO distinct components as one node, which
        f_7100068fd0 then folds with the MUL into DIV (`dv_b.frag`'s `a.xyz /
        b.www`: `DIV.F32 R1.xyz, fragment.attrib[0], fragment.attrib[1].w;`);
        otherwise the RCP is one node per lane, each reading its component
        (`dv_a.frag`'s `a.xy / b.zw`: `RCP.F32 R0.y, fragment.attrib[1].w;
        RCP.F32 R0.x, .. .z; MUL.F32 R1.xy, fragment.attrib[0], R0;`).  A
        CONSTANT dividend is the MUL's second operand (`dv_c.frag`'s
        `vec2(1.0) / a.xy`: `MUL.F32 R4.xy, R0, {1, 1, 0, 0};`), and a
        constant divisor's components are read like any other's (`RCP.F32
        R0.w, {2, 4, 8, 16}.w;`)."""
        args = ins.args()
        _cb = (self.comps.get(args[1], _IDENTITY) if _b0 is not None
               else _IDENTITY)
        _tc = _glasm_type_code(self.module, ins.result_type)
        _rcp = self._mnemonic_of(nodes.RCP, _tc)
        _mul = self._mnemonic_of(nodes.MUL, _tc)
        _dvm = self._mnemonic_of(nodes.DIV, _tc)
        _ds = _opchain.dest_suffix(_nres)
        if None in (_rcp, _mul, _dvm, _ds):
            raise NotEstablished(
                "a divide whose opcodes the image's tables do not name for "
                "this type")
        _bb = _b0 if _b0 is not None else _bconst
        _lanes = list(range(_nres))
        _acon = a.startswith("{")
        if len(set(_cb[c] for c in _lanes)) < 2 and _b0 is not None:
            if _acon:
                raise NotEstablished(
                    "a divide of a constant by one component: the DIV's "
                    "operand order is not measured")
            dst = self._fresh(True)
            self.lines.append(_emit(_dvm, dst + _ds, a, "%s.%s" % (
                _bb, _COMPONENTS[_cb[0]])))
            self.values[ins.result] = dst
            return
        _t = self._fresh()
        _tie = []
        _groups = {}
        for _c in _lanes:
            _groups.setdefault(_cb[_c], []).append(_c)
        if (any(len(g) > 1 for g in _groups.values())
                and not ENV.get("G2S_NORCPCSE")):
            # A DIVISOR COMPONENT READ BY SEVERAL LANES is ONE reciprocal
            # (the per-lane RCPs are value-numbered): a node of its own,
            # numbered by component after the merge, written at its first
            # lane and moved into the others with one MOV -- a component
            # read by one lane stays a lane write of the merge.  `rc_a.frag`'s
            # `a / b.xyxy`: `RCP.F32 R0.y, ..[1].y; RCP.F32 R0.x, ..[1].x;
            # MOV.F R0.yw, R0.y; MOV.F R0.xz, R0.x;` (vr 7, 6, then 5 twice);
            # `rc_c`'s `b.xyzz`: the `.z` alone apart (`tools/nodedump.py`)
            _own = {}
            for _k in sorted(_groups):
                if len(_groups[_k]) > 1:
                    _own[_k] = self._fresh()
            # the shared reciprocals first, then the merge's writes -- each
            # in component order (pass 1 prints them reversed: `rc_d`'s
            # `b.xyx` is `RCP.F32 R0.x, ..x; RCP.F32 R0.y, ..y; MOV.F R0.xz,
            # R0.x;`, `rc_e`'s `b.wxwx` the `.w` one's MOV first)
            for _k in sorted(_own):
                _ls = _groups[_k]
                _tie.append(len(self.lines))
                self.lines.append(_emit(
                    _rcp, "%s.%s" % (_own[_k], _COMPONENTS[_ls[0]]),
                    "%s.%s" % (_bb, _COMPONENTS[_k])))
            for _k in sorted(_groups):
                _ls = _groups[_k]
                _tie.append(len(self.lines))
                if _k in _own:
                    self.lines.append(_emit(
                        self._mnemonic_of(nodes.MOV, _tc),
                        "%s.%s" % (_t, "".join(_COMPONENTS[c] for c in _ls)),
                        "%s.%s" % (_own[_k], _COMPONENTS[_ls[0]])))
                else:
                    self.lines.append(_emit(
                        _rcp, "%s.%s" % (_t, _COMPONENTS[_ls[0]]),
                        "%s.%s" % (_bb, _COMPONENTS[_k])))
            _lanes = []
        for _c in _lanes:
            _tie.append(len(self.lines))
            _src = "%s.%s" % (_bb, _COMPONENTS[_cb[_c]])
            _fl = self.con_flat.get(_bb)
            if (_fl is not None and _fl[1] == self._bkey() and _b0 is not None
                    and args[1] not in self.comps
                    and _cb[_c] < len(_fl[0]) and _fl[0][_cb[_c]] is not None
                    and not ENV.get("G2S_NORCPCONLANE")):
                # A DIVISOR CONSTRUCTED IN THIS BLOCK: each lane's reciprocal
                # reads that lane's SOURCE (`map_d8394059`'s `u / vec3(r.x,
                # r.y, r.z)`: `RCP.F32 R2.z, R6.z;` .. R6 the LDC, not the
                # construct R1)
                _fb, _fc = _fl[0][_cb[_c]]
                # (a constant lane is its slot 0: `rc_g`'s `RCP.F32 R0.w,
                # {2, 0, 0, 0}.x;`)
                _src = ("%s.x" % _fc if _fb is None
                        else "%s.%s" % (_fb, _COMPONENTS[_fc]))
            self.lines.append(_emit(_rcp, "%s.%s" % (_t, _COMPONENTS[_c]),
                                    _src))
        dst = self._fresh(True)
        _tie.append(len(self.lines))
        self.ties.append(_tie)
        if _acon:
            self.lines.append(_emit(_mul, dst + _ds, _t, a))
        else:
            self.lines.append(_emit(_mul, dst + _ds, a, _t))
        self.values[ins.result] = dst

    def _divide_four(self, ins, a, b, _nres):
        """The plain four-component divide of `op_div.vert` (and the mod of
        `op_mod.vert`).  CREATION ORDER when the scheduler runs: the four
        reciprocals are one vreg, so pass 1 is what prints them highest
        component first; the multiply carries the DIVIDE's own position too
        -- all five nodes read seq 1."""
        op = ins.opcode
        if _nres != 4 or _lex.has_swizzle_suffix(a) \
                or _lex.has_swizzle_suffix(b) \
                or a.startswith(("{", "-", "|")) \
                or b.startswith(("{", "-", "|")):
            raise NotEstablished(
                "a divide whose operands are not the measured plain "
                "four-component shape")
        _tc = _glasm_type_code(self.module, ins.result_type)
        _rcp = self._mnemonic_of(nodes.RCP, _tc)
        _mul = self._mnemonic_of(nodes.MUL, _tc)
        if _rcp is None or _mul is None:
            raise NotEstablished(
                "a divide whose two opcodes the image's tables do not name "
                "for this type")
        _t = self._fresh()
        _tie = []
        for _c in ((3, 2, 1, 0) if ENV.get("G2S_NOSCHED2") else (0, 1, 2, 3)):
            _tie.append(len(self.lines))
            self.lines.append(_emit(_rcp, "%s.%s" % (_t, _COMPONENTS[_c]),
                                    "%s.%s" % (b, _COMPONENTS[_c])))
        dst = self._fresh(True)
        _tie.append(len(self.lines))
        self.ties.append(_tie)
        self.lines.append(_emit(_mul, dst, a, _t))
        if op == Op.OpFMod:
            dst = self._mod_tail(a, b, dst, _tc, _mul)
        self.values[ins.result] = dst

    def _mod_tail(self, a, b, dst, _tc, _mul):
        """`mod(x, y) = x - y * floor(x / y)`.  The divide is the same five
        nodes; what follows is measured from `op_mod.vert`: `FLR` (opcode
        0x6e, which the namer spells outright -- unlike the 0x6c/0x6d
        rounding class it needs no mode), then the multiply back by the
        divisor and a subtract written as an ADD with the operand negated
        (notes/42).  Two registers: the quotient and its floor share one, the
        product and the difference the other."""
        _flr = self._mnemonic_of(nodes.FLR, _tc)
        _add = self._mnemonic_of(nodes.ADD, _tc)
        if _flr is None or _add is None:
            raise NotEstablished(
                "a mod whose floor or add the image's tables do not name for "
                "this type")
        self.lines.append(_emit(_flr, dst, dst))
        _p = self._fresh(True)
        self.lines.append(_emit(_mul, _p, b, dst))
        self.lines.append(_emit(_add, _p, a, "-%s" % _p))
        return _p

    # -- derivatives, dots, integer ops, conversions --------------------------

    def _arm_derivative(self, ins):
        """notes/58.  Builtins 0x46b..0x46d and 0x46e..0x470 map to opcodes
        0x68 DDX and 0x69 DDY, whole-vector with no mask (notes/39).
        `fr_ddx.frag` measures them on SCALAR operands -- one instruction
        into `.x`, like the dot -- and `fwidth` as the front end
        expands it: DDX and DDY, each materialised through an absolute-value
        MOV (the carrier of notes/47), and an ADD of the two.  Each derivative
        and its carrier share one `node[36]` (seq 4/4 and 6/6 against the
        ADD's 8).  EVERY value here is a front-end NAME, stored in the block
        (the dataflow dump: names 2..8, one per scalar result), so every one
        is a band temp -- live out of the block, eight registers in all."""
        op = ins.opcode
        if op not in _DERIVATIVES:
            return False
        module = self.module
        self._computation()
        args = ins.args()
        _nres = _components(module, ins.result_type)
        _ds = _opchain.dest_suffix(_nres) if _nres else None
        _tc = _glasm_type_code(module, ins.result_type)
        # A VECTOR derivative is ONE instruction at the result's mask, its
        # operand read through its selector (`dd_a.frag`: `DDX.F32 R1.xy,
        # fragment.attrib[0];`, `dd_d.frag` a whole `DDX.F32 R0, ..`).
        a = (_source(self.values, self.comps, args[0]) if _nres == 1
             else _source(self.values, self.comps, args[0], _nres))
        if a is None or _ds is None:
            raise NotEstablished("a derivative whose operand has no form")
        _ddx = self._mnemonic_of(nodes.DDX, _tc)
        _ddy = self._mnemonic_of(nodes.DDY, _tc)
        _mv = self._mnemonic_of(nodes.MOV, _tc)
        _add = self._mnemonic_of(nodes.ADD, _tc)
        if None in (_ddx, _ddy, _mv, _add):
            raise NotEstablished(
                "a derivative the image's chain does not name for this type")
        if op != Op.OpFwidth:
            dst = self._fresh(True)
            self.lines.append(_emit(_ddx if op == Op.OpDPdx else _ddy,
                                    dst + _ds, a))
        else:
            dst = self._fwidth(a, _ds, _ddx, _ddy, _mv, _add)
            if not ENV.get("G2S_FWIDTHGROUP"):
                # THE FRONT END'S EXPANSION IS THREE STATEMENTS: `ds_b.frag`
                # (`d = fwidth(t)` stored to a local) has DDX and its carrier
                # at seq 24, DDY and its carrier 26, and the ADD 28 with the
                # store and the temp's flushes -- so the store's statement
                # starts at the ADD, not at the DDX (`dd_e.frag`: 2, 4, 6).
                self.defline[ins.result] = len(self.lines) - 1
        self.values[ins.result] = dst
        if _nres == 1:
            self.comps[ins.result] = (0, 0, 0, 0)
            self.scalar.add(ins.result)
        return True

    def _fwidth(self, a, _ds, _ddx, _ddy, _mv, _add):
        """`fwidth` as the front end expands it: DDX and DDY, each through
        an absolute-value carrier MOV sharing its `node[36]`, and their ADD
        (`fr_ddx.frag` scalar, `dd_e.frag` a vec2)."""
        _abs = []
        for _mn in (_ddx, _ddy):
            _t = self._fresh(True)
            _tie = [len(self.lines)]
            self.lines.append(_emit(_mn, _t + _ds, a))
            _m = self._fresh(True)
            _tie.append(len(self.lines))
            self.lines.append(_emit(_mv, _m + _ds, "|%s|" % _t))
            self.ties.append(_tie)
            _abs.append(_m)
        dst = self._fresh(True)
        self.lines.append(_emit(_add, dst + _ds, _abs[0], _abs[1]))
        return dst

    def _arm_dot(self, ins):
        """THE OPERANDS' OWN WIDTH decides their swizzle, not the scalar
        result's: `map_01908c43`'s `dot(u_xlat6, u_xlat5.wxyz)` prints
        `R28.wxyz`, a DP3 of `.yzw` prints `R24.yzww` (`_swizzle_suffix`'s
        fill), where the result's width printed only the first letter.

        notes/39: `dot` is builtin id 0x497, whose arm of f_7100f28a00
        COMPUTES the opcode as 0x86 + the component count (MUL below two), so
        DP2/DP3/DP4 follow from the operand type, not a listing.  A SPIR-V
        value, so the front end's named temp: stored in its block and a band
        record (notes/52 §8, notes/55 §6 -- lens_flare's `g2s_rnent` makes
        the dot's temp record 5 in block 0, ahead of every lowering temp)."""
        if ins.opcode != Op.OpDot:
            return False
        module = self.module
        self._computation()
        args = list(ins.args())
        if (args[0] in module.constants and args[1] not in module.constants
                and not ENV.get("G2S_NOCONSTSECOND")):
            # A CONSTANT GOES SECOND, as in a commutative add or multiply
            # (`dt_d.frag`'s `dot(vec2(0.5, 0.25), a.zw)`: `DP2.F32 R0.x,
            # fragment.attrib[0].zwzw, {0.5, 0.25, 0, 0};`)
            args = [args[1], args[0]]
        src = self._operand_type(args[0])
        if src is None or src.opcode != Op.OpTypeVector:
            raise NotEstablished("a dot whose operand is not a vector")
        _dw = src.operands[2]
        # a CONSTANT operand prints at its own width, padded (`dt_a.frag`:
        # `DP3.F32 R0.x, fragment.attrib[0], {0.212500006, 0.715399981,
        # 0.0720999986, 0};`)
        a, b = [_source(self.values, self.comps, x, _dw)
                or _constant_source(module, x, _dw) for x in args]
        if a is None or b is None:
            raise NotEstablished("a dot whose operand has no form")
        mnem = _opchain.dot_mnemonic(
            _dw, _glasm_type_code(module, ins.result_type))
        if mnem is None:
            raise NotEstablished("a dot the image's chain does not name")
        dst = self._fresh(True)
        self.lines.append(_emit(mnem, dst + ".x", a, b))
        self.values[ins.result] = dst
        self.comps[ins.result] = (0, 0, 0, 0)
        return True

    def _operand_type(self, vid):
        """The type instruction of a value or a constant, or None."""
        d = self.module.result_insn.get(vid) or self.module.constants.get(vid)
        if d is None or not d.has_result_type:
            return None
        return self.module.types.get(d.result_type)

    def _arm_int_binary(self, ins):
        op = ins.opcode
        if op not in _INT_BINARY:
            return False
        module = self.module
        self._computation()
        args = ins.args()
        if _signedness(module, ins.result_type) is None:
            raise NotEstablished("an integer op on a non-integer type")
        _mn = _opchain.mnemonic(OP_NAME.get(op, ""),
                                _glasm_type_code(module, ins.result_type))
        if _mn is None:
            raise NotEstablished(
                "an integer op the image's chain does not name for this type")
        _opc = _opchain.glasm_opcode(OP_NAME.get(op, ""))
        _neg = "-" if _opchain.rewrite(_opc)[1] else ""
        _nres = _components(module, ins.result_type)
        a = (_source(self.values, self.comps, args[0], _nres)
             or _constant_source(module, args[0], _nres))
        b = (_source(self.values, self.comps, args[1], _nres)
             or _constant_source(module, args[1], _nres))
        if a is None or b is None:
            raise NotEstablished("an integer op whose operand has no form")
        dst = self._fresh(True)
        self.lines.append(_emit(_mn, dst, a, _neg + b))
        self.values[ins.result] = dst
        return True

    def _arm_bitfield_insert(self, ins):
        """`bitfieldInsert(base, insert, offset, bits)` IS ONE NODE, opcode
        0x1b0 (`BFI`), whose sources are, in order, the constant pair
        `{bits, offset}`, the insert and the base (`bf_a.frag`'s DAG: node
        0x1b0 on a 0x26 constant of mask 0xffff, the insert, then the base;
        `BFI.S R1.x, {4, 4, 0, 0}, fragment.attrib[0], {0, 0, 0, 0};`).  The
        handler is the multi-operand worker f_7100fd1210, not the binary
        one, so the chain gives no opcode: it is the node's.

        Measured only for a SCALAR result with CONSTANT offset and count --
        the only forms the corpus has; anything else is refused."""
        if ins.opcode != Op.OpBitFieldInsert or ENV.get("G2S_NOBFI"):
            return False
        module = self.module
        args = ins.args()
        _nres = _components(module, ins.result_type)
        if _nres != 1 or _signedness(module, ins.result_type) is None:
            raise NotEstablished("a bitfieldInsert whose result is not one "
                                 "integer component")
        _off = _scalar_value(module, args[2])
        _cnt = _scalar_value(module, args[3])
        if _off is None or _cnt is None:
            raise NotEstablished("a bitfieldInsert whose offset or count is "
                                 "not a constant")
        _mn = self._mnemonic_of(nodes.BFI,
                                _glasm_type_code(module, ins.result_type))
        if _mn is None:
            raise NotEstablished("a bitfieldInsert the image's tables do not "
                                 "name for this type")
        self._computation()
        # a lane of a construct made in this block reads its source, as any
        # operand does (`_con_read`): the slice's `chr_hair_0dc59a05` builds
        # an ivec3 of three BFIs and inserts into its `.x` -- `BFI.S R1.x,
        # {4, 0, 0, 0}, R6, R3;`, R3 the first BFI (`bf_c.frag`)
        _cr = ((lambda x: _drop_x_for_scalar(self._con_read(x), _nres))
               if not ENV.get("G2S_NOBFICON") else (lambda x: None))
        base = (_cr(args[0]) or _source(self.values, self.comps, args[0], _nres)
                or _constant_source(module, args[0], _nres))
        insert = (_cr(args[1])
                  or _source(self.values, self.comps, args[1], _nres)
                  or _constant_source(module, args[1], _nres))
        if base is None or insert is None:
            raise NotEstablished("a bitfieldInsert whose operand has no form")
        dst = self._fresh(True)
        # the node's mask is 0xff (the DAG), `.x` in print
        self.lines.append(_emit(_mn, dst + _opchain.dest_suffix(_nres),
                                "{%s, %s, 0, 0}" % (_cnt, _off),
                                insert, base))
        self.values[ins.result] = dst
        return True

    def _arm_logical(self, ins):
        """`&&` AND `||` ON BOOLS ARE THE BITWISE NODES ON THE BOOL'S
        REPRESENTATION.  The image's chain maps OpLogicalAnd to 0x84 and
        OpLogicalOr to 0x92, the nodes OpBitwiseAnd/Or map to, and a bool is
        held in its representation type (U32, notes/64 §4: `f_710005f2d0`
        rewrites 0x84/0x92 only for a FLOAT representation).  `la_a.frag`'s
        fold dump: `u_xlatb0 && u_xlatb1` is node 0x84, type 0xc, on the two
        bools' values in operand order -- `AND.U R1.x, R3, R2;`."""
        op = ins.opcode
        if op not in (Op.OpLogicalAnd, Op.OpLogicalOr) \
                or ENV.get("G2S_NOLOGICAL"):
            return False
        module = self.module
        self._computation()
        args = ins.args()
        _mn = _opchain.mnemonic(OP_NAME.get(op, ""), _BOOL_REPR_CODE)
        if _mn is None:
            raise NotEstablished(
                "a logical op the image's chain does not name for the bool "
                "representation")
        _nres = _components(module, ins.result_type)
        a = (_source(self.values, self.comps, args[0], _nres)
             or _constant_source(module, args[0], _nres))
        b = (_source(self.values, self.comps, args[1], _nres)
             or _constant_source(module, args[1], _nres))
        if a is None or b is None:
            raise NotEstablished("a logical op whose operand has no form")
        dst = self._fresh(True)
        self.lines.append(_emit(_mn, dst if _nres != 1 else dst + ".x", a, b))
        self.values[ins.result] = dst
        return True

    def _arm_any(self, ins):
        """`any(bvecN)` IS A CHAIN OF ORs on the bool's representation, lane
        by lane: `any_a.frag` (a bvec4 compare) prints
            OR.U  R1.x, R4, R4.y;
            OR.U  R1.x, R1, R4.z;
            OR.U  R2.x, R1, R4.w;
        (`any_b`, a bvec2, the one OR; `any_c`, a bvec3, two).  The nodes are
        made OUTERMOST FIRST -- `tools/nodedump.py`: seq 6 for the last OR
        (the statement's, which the value's flushes share), 7 and 8 for the
        inner ones -- so the inner ones carry later seqs than the outer."""
        if ins.opcode != Op.OpAny or ENV.get("G2S_NOANY"):
            return False
        module = self.module
        arg = ins.args()[0]
        _t = self._operand_type(arg)
        _n = _t.operands[2] if _t is not None \
            and _t.opcode == Op.OpTypeVector else None
        base = self.values.get(arg)
        if (_n is None or base is None or not _is_placeholder(base)
                or not self._normalised_view(arg)):
            raise NotEstablished("an any() of a bool vector that is not a "
                                 "normalised comparison in a register")
        _mn = self._mnemonic_of(nodes.OR, _BOOL_REPR_CODE)
        if _mn is None:
            raise NotEstablished("an any() whose OR the image does not name")
        self._computation()
        _cc = self.comps.get(arg, _IDENTITY)

        def lane(k):
            return base if _cc[k] == 0 else "%s.%s" % (base, _COMPONENTS[_cc[k]])
        _first = len(self.lines)
        acc = lane(0)
        _lines = []
        for k in range(1, _n):
            dst = self._fresh(k == _n - 1)
            _lines.append(len(self.lines))
            self.lines.append(_emit(_mn, dst + ".x", acc, lane(k)))
            acc = dst
        # outermost first: the last OR keeps the statement's position, each
        # inner one a later one (between it and the next line)
        for _j, _li in enumerate(_lines[:-1]):
            _tg = _sched.Tie([_li])
            _tg.seq = _first + 1.0 - 1.0 / (2 + (len(_lines) - 1 - _j))
            self.ties.append(_tg)
        _tl = _sched.Tie([_lines[-1]])
        _tl.seq = _first
        self.ties.append(_tl)
        self.values[ins.result] = acc
        self.comps[ins.result] = (0, 0, 0, 0)
        return True

    def _arm_convert(self, ins):
        """A CONVERSION, and both results BAND temps (notes/52 section 8):
        `int_iadd.frag` is `ADD.S R0, ..; I2F.S R1, R0;` -- the conversion
        does not reuse its dying source's register because the band is one
        temp per front-end IR operation (`tools/bandsize.py` measures
        `int_iadd.frag`'s at 2 for its two SPIR-V values).

        The destination takes the count table's mask like every other
        operation (notes/41): `ld_i1.frag` prints `I2F.S R0.x, R0;`, the
        corpus `I2F.U R16.xyz, R0.zwyw;`.

        FLOAT TO INTEGER: the cast is a MOV with an integer destination and a
        float source, which the MOV legaliser f_7100bdefd0 (0xbf030..0xbf188)
        turns into TRUNC with modifier 4, typed by the DESTINATION -- the
        same reading `_bool_normalise` uses.  `pf_a.frag` prints `TRUNC.S R0,
        fragment.attrib[0];`, `pf_b.frag` `TRUNC.U R0, ...`."""
        op = ins.opcode
        if not (op in _CONVERT or (op in _FLOAT_TO_INT
                                   and not ENV.get("G2S_NOF2I"))):
            return False
        module = self.module
        self._computation()
        _nres = _components(module, ins.result_type)
        _ds = _opchain.dest_suffix(_nres) if _nres else None
        # a component of a construct made in this block is read from its
        # source, as every operand's (`_con_read`): `cv_a.frag`'s
        # `float(u_xlati0.y)` right after `u_xlati0 = ivec2(..)` prints
        # `I2F.S R2.x, R1;` (`monster_02d3d44e`: `I2F.S R2.x, R6;`)
        a = (_drop_x_for_scalar(self._con_read(ins.args()[0]), _nres)
             if not ENV.get("G2S_NOCONVCON") else None)
        a = a or _source(self.values, self.comps, ins.args()[0], _nres)
        if a is None or _ds is None:
            raise NotEstablished("a convert whose operand has no form")
        _cmn = _CONVERT.get(op)
        if _cmn is None:
            _sg = _signedness(module, ins.result_type)
            if _sg is None:
                raise NotEstablished("a float-to-integer convert whose result "
                                     "is not an integer")
            _cmn = "TRUNC.%s" % _sg
        dst = self._fresh(True)
        self.lines.append(_emit(_cmn, dst + _ds, a))
        self.values[ins.result] = dst
        return True

    def _arm_compare(self, ins):
        """A float comparison the chain does not name: its GLASM mnemonic was
        read off a listing, not out of the image, and fitted rules are not
        emitted."""
        if ins.opcode not in _COMPARE:
            return False
        raise NotEstablished(
            "opcode %d: its GLASM mnemonic was read off a listing, not out of "
            "the image (_COMPARE), and fitted rules are not emitted"
            % ins.opcode)

    # -- bitcasts -----------------------------------------------------------

    def _is_float_type(self, tid):
        t = self.module.types.get(tid)
        if t is not None and t.opcode == Op.OpTypeVector:
            t = self.module.types.get(t.args()[0])
        return t is not None and t.opcode == Op.OpTypeFloat

    def _arm_bitcast(self, ins):
        """A BITCAST IS AN INSTRUCTION (notes/72).  The reader's OpBitcast
        handler `f_7100fda63c` makes one of every bitcast:

          * between an integer and a float (0xfda77c..0xfda890) it calls the
            builtin `intBitsToFloat` / `uintBitsToFloat` / `floatBitsToInt` /
            `floatBitsToUint` (the names at 0x115cb6d / 0x115cb6c /
            0x114a5d2 / 0x11548ed, chosen by the source's and the result's
            base types) and stores the call into a named temp
            (0xfda8d0..0xfda948).  It prints as a MOV of the RESULT's type,
            one per component (`bc_fi.frag`: `MOV.F R0.x,
            fragment.attrib[0];`, `bc_fs` `MOV.S`, `bc_fu` `MOV.U`, `bc_ubo`
            `MOV.F R0.x, R0;`);
          * anything else goes to the generic handler (0xfda9b0: f_7100fcf420
            with 0x7c), a cast that prints as a MOV with the SOURCE's suffix,
            whole-vector (`bc_attr.vert`: `MOV.U R0, vertex.attrib[0];` for
            uvec4 -> ivec4, `bc_iu` `MOV.S R0, ...` the other way, `bc_sbo`
            `MOV.U R3.x, R1;`).
        Only a same-width reinterpretation is taken; anything else would move
        bits and is refused."""
        if ins.opcode != Op.OpBitcast:
            return False
        module = self.module
        src_id = ins.args()[0]
        _nres = _components(module, ins.result_type)
        src_def = self.by_result.get(src_id) or module.constants.get(src_id)
        src_t = src_def.operands[0] if src_def is not None else None
        if src_t is None or _type_width(module, ins.operands[0]) != \
                _type_width(module, src_t):
            raise NotEstablished(
                "a bitcast that changes width: the bits move and no "
                "instruction for it is established")
        a = (_source(self.values, self.comps, src_id, _nres)
             or _constant_source(module, src_id, _nres))
        if a is None:
            raise NotEstablished("a bitcast of a value with no form")
        _fl_src = self._is_float_type(src_t)
        _fl_dst = self._is_float_type(ins.result_type)
        if (_fl_src != _fl_dst and _nres and _nres > 1
                and not ENV.get("G2S_NOVBITCAST")):
            self._vector_bitcast(ins, src_id, _nres)
            return True
        if _fl_src != _fl_dst:
            if _nres != 1:
                raise NotEstablished(
                    "a float/integer bitcast of a vector: the builtin is "
                    "scalarised and assembled (bc_uf4.vert), not read")
            _tc = _glasm_type_code(module, ins.result_type)
        else:
            _tc = _glasm_type_code(module, src_t)
        _mv = self._mnemonic_of(nodes.MOV, _tc)
        _ds = _opchain.dest_suffix(_nres) if _nres else None
        if _mv is None or _ds is None:
            raise NotEstablished(
                "a bitcast whose MOV or mask the image's rules do not give")
        self._computation()
        dst = self._fresh(True)
        self.lines.append(_emit(_mv, dst + _ds, a))
        self.values[ins.result] = dst
        return True

    def _vector_bitcast(self, ins, src_id, _nres):
        """THE BUILTIN IS SCALARISED AND ASSEMBLED (notes/87): each component
        is a scalar bitcast -- a MOV with the RESULT's suffix into a temp's
        `.x`, as the scalar form -- and the vector is a construct of the
        four, component 0 forwarded to its reader.  `bc_uf4.vert`: `MOV.F
        R0.x, vertex.attrib[0];` .. `MOV.F R3.x, vertex.attrib[0].w;`, then
        `MOV.F R4.x, R0;` `MOV.F R4.w, R3.x;` .."""
        _tc = _glasm_type_code(self.module, ins.result_type)
        _mv = self._mnemonic_of(nodes.MOV, _tc)
        _b = self.values.get(src_id)
        if _mv is None or _b is None or _b.startswith(("-", "|", "{")):
            raise NotEstablished("a vector bitcast whose MOV or source has "
                                 "no form")
        self._computation()
        _cm = self.comps.get(src_id, _IDENTITY)
        # a whole load's components stored in this block read those stored
        # values, as an extract's do (notes/69, `lsplit`): `monster_02d3d44e`'s
        # `floatBitsToInt(u_xlat104)` after `u_xlat104.y = c.y` in its block
        # prints `MOV.S R6.x, R9.y;` for .y and reads R104 for x, z, w
        _ls = ({} if ENV.get("G2S_NOBITCASTSPLIT")
               else self.lsplit.get(src_id, {}))
        flat = []
        for _k in range(_nres):
            _t = self._fresh(True)
            if _cm[_k] in _ls:
                _sv, _sc = _ls[_cm[_k]]
                self.lines.append(_emit(_mv, _t + ".x", _swizzle(_sv, _sc)))
                flat.append((_t, 0))
                continue
            # a component of a CONSTRUCT made in this block is read from its
            # source, as any lane read of one is (`_con_read`; `cx_a.frag`'s
            # `floatBitsToInt(u_xlat0.yzw)` right after `u_xlat0 = vec4(..)`
            # prints `MOV.S R4.x, R0;`, R0 what `MOV.F R7.y, R0.x;` wrote)
            _cl = (self._con_lane(_b, _cm[_k])
                   if not ENV.get("G2S_NOBITCASTCON") else None)
            _o = _cl.split(".")[0] if _cl is not None else \
                _swizzle(_b, _cm[_k])
            self.lines.append(_emit(_mv, _t + ".x", _o))
            flat.append((_t, 0))
        dst, _text = self._assemble(flat, _mv, set(), True)
        self.head_src[ins.result] = _text
        self.values[ins.result] = dst
