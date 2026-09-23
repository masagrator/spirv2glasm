"""boolean.py -- booleans and the condition register (notes/64).

The back end stores a `bool` in an integer type chosen by the pass driver at
0x381e8..0x3822c: U32 when the profile has capability 8, else U16 when it
has capability 6, else F16.  Every probe profile has capability 8 (measured:
`g2s_walk` shows W4 = 0xc for f_710005f2d0 and f_7100030e20), so the
representation is U32 and a bool prints with the short suffix `.U`.
"""
import opchain as _opchain
from glasmlib.common import NotEstablished, OP_NAME
from glasmlib.types import _glasm_type_code, _signedness
from glasmlib.text import _emit, _source

_BOOL_SUFFIX = "U"
_BOOL_REPR_CODE = 0xc                   # U32, the same choice

# The `continue` flag (notes/71): f_7100fafca0 makes it with the cgc type
# f_7100f5aab0(cg, 0x2b), which reaches the IR as type 6 (`g2s_trace_wstmt`
# on 0071_lp_wcont.vert: the variable node's [40] = 6) and is allocated in the
# SHORT class; one flag per loop body, the only SHORT temp any probe has.
_CFLAG_TYPE = 6
_CFLAG_REG = "H0"

# The cgc operators of the six comparisons (notes/body_workers), the ones the
# lowering sends to the SLT..SNE families (notes/34, lowering_map.json).
_COMPARISON_OPERATORS = range(0x27, 0x2d)

_UINT_SIGN_BIT = 0x80000000


def _bool_constant(value):
    """A bool constant after f_7100030e20 retypes it to the representation:
    an INTEGER representation keeps the value (0x30e60..0x30e7c converts to
    1.0 only for a float one), so true is `{1, 0, 0, 0}`."""
    return "{%d, 0, 0, 0}" % (1 if value else 0)


def _bool_normalise(module, cond, reg, dst=None, mask=".x"):
    """The move f_7100030e20 appends to a COMPARISON whose result is a bool
    (0x30f30..0x30fe8): the compare (SEQ SGE SGT SLE SLT SNE) is retyped to
    its OPERAND type and its value is moved into the bool representation --

      * a float operand type: MOV U32 <- float, which the MOV legaliser
        f_7100bdefd0 (0xbf030..0xbf188: integer destination, float source)
        turns into TRUNC with modifier 4 -> `TRUNC.U R.x, R;`
      * an integer operand type (signed OR unsigned): MOV U32 <- -value, the
        source's negate bit flipped (0x30fc4..0x30fd0).  A MOV prints its
        suffix from its SOURCE type (notes/37, bd65ec) -> `MOV.S R.x, -R;`
        or `MOV.U R.x, -R;`.

    Anything else reaching a branch (a bool variable, a logical operation) is
    not a comparison and gets no such move; it is refused rather than guessed.
    The move writes the compare's own mask: a VECTOR compare's is whole
    (`0000_mq_n4.frag`'s `greaterThanEqual(v.xyxy, -v.xyxy)`: `SGE.F32 R4, ..;
    TRUNC.U R12, R4;`).
    """
    cd = module.result_insn.get(cond)
    if (cd is None or not cd.args() or _opchain.OPERATOR.get(
            OP_NAME.get(cd.opcode, "")) not in _COMPARISON_OPERATORS):
        raise NotEstablished("a branch on a bool that is not a comparison")
    od = (module.result_insn.get(cd.args()[0])
          or module.constants.get(cd.args()[0]))
    t = od.result_type if od is not None and od.has_result_type else None
    if t is None:
        raise NotEstablished("a comparison whose operand type is unknown")
    sign = _signedness(module, t)
    dst = reg if dst is None else dst
    if sign is None:
        return ["TRUNC.%s %s%s, %s;" % (_BOOL_SUFFIX, dst, mask, reg)]
    return ["MOV.%s %s%s, -%s;" % (sign, dst, mask, reg)]


def _cc_move(src, mask=".x"):
    """The `.CC` carrier a branch gets (f_7100bdec20 / the if emitter): a
    MOV of the bool into the condition code.  f_7100032c30 folds it into its
    source only when that source is an instruction that can set CC with one
    use and no modifier; a branch on a stored bool reads a NAME (op 0x2b) and
    a constant is no instruction, so here it stays a MOV, whose own dummy
    destination is the register class 0x100 the namer f_7100bcf110 prints
    as `RC` (0xbcf1d4).  A VECTOR select's move writes the result's mask
    (`0095_sel_c.frag`: `MOV.U.CC RC.xy, R0;`)."""
    return "MOV.%s.CC RC%s, %s;" % (_BOOL_SUFFIX, mask, src)


def _switch_test(module, values, comps, cond, fresh):
    """One case's test in a switch's IF chain (notes/66): the compare
    `selector == literal` the switch lowering makes (operator 0x2b), used
    DIRECTLY as the IF's condition.

      * the compare is named by the image's chain for OpIEqual on the
        selector's type (py/opchain.py), with a bool destination: `.x`;
      * f_7100030e20 appends the move into the bool representation, an
        integer operand's being `MOV U32 <- -value` (`_bool_normalise`);
      * the IF's `.CC` MOV reads that move, which is an instruction with one
        use and no modifier on the read, so f_7100032c30 folds it: the move
        takes `.CC` and, its value having no other use, node[40] = 1 -- the
        `HC` destination (`_loop_head_test`).  A branch on a STORED bool
        reads a name instead and keeps its `MOV.U.CC RC.x` (`_cc_move`).
    """
    _, sel, lit = cond
    sd = module.result_insn.get(sel)
    t = sd.result_type if sd is not None and sd.has_result_type else None
    sign = _signedness(module, t) if t is not None else None
    if sign is None:
        raise NotEstablished("a switch selector that is not an integer")
    ty = module.types.get(t)
    if ty is None or ty.args()[0] != 32:
        raise NotEstablished("a switch selector that is not 32-bit")
    if sign == "S" and lit & _UINT_SIGN_BIT:
        lit -= 1 << 32
    a = _source(values, comps, sel, 1)
    if a is None:
        raise NotEstablished("a switch selector with no form")
    mn = _opchain.mnemonic("OpIEqual", _glasm_type_code(module, t))
    ds = _opchain.dest_suffix(1)
    if mn is None or mn.startswith("<") or ds is None:
        raise NotEstablished("a switch compare the image's chain does not "
                             "name")
    dst = fresh()
    return [_emit(mn, dst + ds, a, "{%d, 0, 0, 0}" % lit),
            "MOV.%s.CC HC.x, -%s;" % (sign, dst)]


def _loop_head_test():
    """The test at the top of every REP (notes/64 §2-4), read end to end:

      * the reader makes a structured loop's statement with NO condition
        (OpBranch in the header, 0xfdce94: f_7100f3e120(cg, 3, NULL, ...));
      * the cgc->IR lowering of statement kind 3 (f_7100f41ca0, arm 0xf41d84)
        gives a missing condition the constant TRUE: f_7100eeb300(ctx, 1) =
        IR constant op 0x12, value 1, type bool (0xf41f58);
      * the statement walker's loop arm (0xf15000..0xf15158) emits
        `if (!cond) break`: NOT (op 0x64, bool, one component) of the
        condition, and a conditional BREAK (0x15) on it;
      * f_710005f2d0 lowers the NOT to SEQ(x, 0) in the bool representation
        (0x5f61c..0x5f6a4), the constant keeps its value (above);
      * f_7100bdec20 turns the BREAK into BRK with modifier 5 (`NE`) behind a
        `.CC` MOV, and f_7100032c30 FOLDS that MOV into the SEQ: the SEQ has
        one use and no modifier, so it takes `.CC` and, its value having no
        other use, node[40] = 1 (0x32df4) -- a destination that is not a
        definition, which the printer names through vt[144] = f_710005c14c:
        always `HC`.
    """
    return ["SEQ.%s.CC HC.x, %s, %s;" % (_BOOL_SUFFIX, _bool_constant(True),
                                         _bool_constant(False)),
            "BRK   (NE.x);"]
