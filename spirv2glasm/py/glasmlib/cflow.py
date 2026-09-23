"""cflow.py -- structured control flow flattened into one marked stream.

The lowering walks ONE instruction stream.  A structured `if`, `switch` or
loop is turned into marker pseudo-instructions (`IF`, `ELSE`, `ENDIF`,
`REP`, `LOOPIF`, `LOOPELSE`, `ENDREP`, `BREAK`, `CFLAG_*`) around the
flattened arms; anything that is not a read shape is refused.
"""
from spvnames import Op

from glasmlib.common import NotEstablished, KILLS, Op, ENV

# The merge labels of the loops being walked, innermost last: a branch to the
# top one from inside an arm is a `break`.
_LOOP_MERGES = []

# id -> True/False for the module's OpConstantTrue / OpConstantFalse, set by
# the lowering for the duration of the walk.  An `if` on one of them is folded
# before the IR ever sees it (`_const_arm`).
_CONST_BOOLS = {}

# The loops being walked, innermost last, for `continue` (notes/71): each is
# {"cont": the continue label, "depth": the `_walk` depth of its body, "used":
# whether a continue arm was taken}.  `_WALK_DEPTH` counts nested `_walk`s so
# a continue is only taken at the loop body's own level.
_LOOP_CONTS = []
_WALK_DEPTH = [0]


def _blocks(body):
    """The function's instructions split into (label, [instructions])."""
    out, cur, label = [], [], None
    for i in body:
        if i.opcode == Op.OpLabel:
            if label is not None:
                out.append((label, cur))
            label, cur = i.result, []
            continue
        if i.opcode in (Op.OpLine, Op.OpNoLine):
            continue
        cur.append(i)
    if label is not None:
        out.append((label, cur))
    return out


class _Marker(object):
    """A pseudo-instruction standing for `IF`, `ELSE`, `ENDIF` and the rest."""

    def __init__(self, kind, cond=None):
        self.opcode = -1
        self.kind = kind
        self.cond = cond
        self.result = None

    def args(self):
        return []


# A marker's opcode: no SPIR-V opcode is negative.
MARKER = -1


class _Label(object):
    """A synthetic OpLabel, so an arm can be re-walked as its own body."""

    def __init__(self, result):
        self.opcode = Op.OpLabel
        self.result = result

    def args(self):
        return []


def _is_branch_to(ins, label):
    return ins.opcode == Op.OpBranch and ins.args()[0] == label


def _arm_body(ins):
    """An arm's instructions without its terminating branch.

    A terminator that is `OpKill` (or one of its two siblings) STAYS: it is a
    real instruction, `KIL NE.x;`, not a branch.
    """
    if ins and ins[-1].opcode == Op.OpBranch:
        return ins[:-1]
    return ins


def _flatten_blocks(blocks, merge):
    """Flatten an arm, dropping the branch that takes it to the merge."""
    # The branch that leaves the arm has to go BEFORE the walk, not after:
    # the merge block is not part of the arm, so the walk would see a branch
    # to a label it does not have and call it unstructured.
    body = []
    for n, (label, ins) in enumerate(blocks):
        body.append(_Label(label))
        if n == len(blocks) - 1 and ins and _is_branch_to(ins[-1], merge):
            ins = ins[:-1]
        body.extend(ins)
    return _walk(body)


def _flatten(body):
    """One instruction stream, with structured control flow turned into
    markers.

    The shape recognised for an `if` is exactly what glslang emits:

        <block>  OpSelectionMerge merge  /  OpBranchConditional c, t, f
        t:       <block>  OpBranch merge
        f:       <block>  OpBranch merge      (absent for an if with no else)
        merge:   <block>
    """
    return _walk(body)


def _const_arm(cond):
    """The arm a constant-condition `if` is replaced by, or None if the
    condition is not a constant.

    Read (notes/71): the cgc statement simplifier `f_7100ef7d50`, run by the
    cgc->IR stage `f_7100eee650` after the break lowering, dispatches on the
    node kind (jump table 0x11bdbf6).  For an `if` (kind 1, 0xef7e9c ->
    0xef7eb4) whose condition `[24]` is a constant (kind 0x11) with every
    component equal (0xef7ee0..0xef7f08), 0xef8f68 returns `[40]` (the else)
    when the first component is zero and `[32]` (the then) otherwise
    (0xef8f84..0xef8f98) -- the other arm is dropped.  `g2s_trace_regs` at
    0xef8f84 fires on `0071_lp_wbrk.vert`'s `while (true)` condition (the static
    true constant) and never on `0071_lp_fbrk.vert`'s `i < 4`.

    Returns "then" or "else".
    """
    if cond in _CONST_BOOLS:
        return "then" if _CONST_BOOLS[cond] else "else"
    return None


def _switch_chain(term, merge, order, byid, head):
    """A structured `switch`, as the chain of IFs the front end makes of it.

    Read (notes/66):

      * the reader's OpSwitch handler (0xfdceb8 -> f_7100fd56d8) wraps the
        selector (f_7100f3b030, operator 5) and records the case literals
        and their labels (f_7100fd5800); each case target then opens with a
        case-label statement (kind 0xa), the default's with kind 0xb;
      * cgc's switch lowering (f_7100fb0a40, one call per statement of the
        switch body, IN BODY ORDER) makes, for a case label, the compare
        `selector == literal` (f_7100f3b030 operator 0x2b, 0xfb0bd4) -- ORed
        into the pending test when several labels share a body (operator
        0x32, 0xfb0bf8) -- and, when the body's first statement arrives,
        closes the previous group into an `if` (f_7100fb0c40 ->
        f_7100f3e120 kind 1, 0xfb0cdc) hung on the previous one's else;
      * a default label only sets the group's flag ([104], 0xfb0c18), and
        f_7100fb0c40 holds its body aside ([96], 0xfb0c80) to be the
        INNERMOST else, wherever the default sits in the body.

    `g2s_trace_irtree` on `0046_cf_switch.vert` shows the result: `if (k == 0)
    {..} else { if (k == 1) {..} else {default} }`, with the default block
    first in the SPIR-V.  The compare is an expression used directly as the
    condition, not a stored bool (the IF emitter's `("switch", ...)` form).

    Only the read shape is taken: one literal per target, targets distinct
    from each other and from the default, the case targets in the order of
    their literals, every arm ending with a branch to the merge (a `break`;
    a fallthrough goes through the lowering's other arms, 0xfb0b54, which are
    not read), and a default that is a block of its own.
    """
    args = term.args()
    sel, default, pairs = args[0], args[1], args[2:]
    if len(pairs) % 2 or not pairs:
        raise NotEstablished("a switch whose case list is not one word each")
    cases = [(pairs[k], pairs[k + 1]) for k in range(0, len(pairs), 2)]
    targets = [lab for _, lab in cases]
    if (default == merge or default in targets
            or len(set(targets)) != len(targets) or merge in targets):
        raise NotEstablished("a switch whose cases share a body or that has "
                             "no default of its own")
    if any(t not in byid for t in targets + [default]) or merge not in byid:
        raise NotEstablished("a switch target that is not a block")
    idx = [order.index(t) for t in targets]
    if idx != sorted(idx):
        raise NotEstablished("a switch whose case blocks are not in the "
                             "order of their literals")
    mi = order.index(merge)
    starts = sorted(idx + [order.index(default)])
    if starts[-1] >= mi or starts[0] <= head:
        raise NotEstablished("a switch whose arms are not before its merge")

    def arm(label):
        lo = order.index(label)
        hi = next((x for x in starts if x > lo), mi)
        sub = [(order[k], byid[order[k]]) for k in range(lo, hi)]
        last = sub[-1][1][-1] if sub and sub[-1][1] else None
        if last is None or not _is_branch_to(last, merge):
            raise NotEstablished("a switch arm that does not end with a "
                                 "break")
        return _flatten_blocks(sub, merge)

    out = []
    for lit, lab in cases:
        out.append(_Marker("IF", ("switch", sel, lit)))
        out.extend(arm(lab))
        out.append(_Marker("ELSE"))
    out.extend(arm(default))
    out.extend(_Marker("ENDIF") for _ in cases)
    return out


def _walk(body):
    _WALK_DEPTH[0] += 1
    try:
        return _walk_level(body)
    finally:
        _WALK_DEPTH[0] -= 1


class _Level(object):
    """One level of the walk: the blocks, the position, the output."""

    def __init__(self, body):
        blocks = _blocks(body)
        self.order = [b[0] for b in blocks]
        self.byid = dict(blocks)
        self.out = []
        self.i = 0
        # `continue` guards opened at this level, closed at its end (notes/71)
        self.guards = 0

    def blocks_between(self, lo, hi):
        return [(self.order[k], self.byid[self.order[k]])
                for k in range(lo, hi)]


def _headed_by(ins, term, opcode, merge_opcode):
    """Does the block end with `merge_opcode` then a terminator `opcode`?"""
    return (len(ins) >= 2 and term is not None and term.opcode == opcode
            and ins[-2].opcode == merge_opcode)


def _break_arm(sub):
    """A `break`: the arm leaves the innermost loop.  The compiler prints it
    as the loop exit's own conditional BRK on a constant true inside the IF
    (`0071_lp_fbrk.vert`, `0071_lp_wbrk.vert`: `IF NE.x; MOV.U.CC RC.x, {1, 0, 0, 0};
    BRK (NE.x); ENDIF;`), the same lowering as the loop condition's ELSE arm
    (notes/64 §5, notes/71)."""
    sub[-1] = (sub[-1][0], sub[-1][1][:-1] + [_Marker("BREAK")])


def _continue_arm(sub, has_else):
    """A `continue` (notes/71).  The break/continue lowering f_7100f74bc0
    walks the loop with f_7100f741c0; an arm holding a continue
    (f_7100fafb70 -> f_7100fafca0, kind 7 at 0xfafcf0) has it replaced by an
    assignment `flag = 1.0` to a flag temp of cgc type 0x2b (f_7100f5aab0 /
    f_7100f53ac0, 0xfafd04..0xfafd80) and what follows is guarded by
    `if (flag == 0)` (operator 0x2b, 0xfafc10..0xfafc70); the flag is set to
    0 at the loop body's top.  `g2s_trace_wstmt` on `0071_lp_wcont.vert` shows
    exactly that IR.

    NEITHER "ONCE PER BODY" NOR "AT THE BODY'S OWN LEVEL" is a restriction
    the compiler has (notes/122 §1): 0xfafcfc reuses the loop's flag temp if
    it already exists, and the guard is built around the statement list OF
    THE LEVEL BEING WALKED.  An ELSE arm is still refused -- what the kind-7
    handler does with one is not read.  `G2S_NOCONTDEEP=1` restores the two
    lifted restrictions."""
    top = _LOOP_CONTS[-1]
    if ENV.get("G2S_CONTDBG"):
        import sys as _s
        print("CONTDBG has_else=%r used=%r depth=%r topdepth=%r" % (
            has_else, top["used"], _WALK_DEPTH[0], top["depth"]),
            file=_s.stderr)
    # NEITHER "ONCE" NOR "AT THE BODY'S OWN LEVEL" IS IN THE COMPILER
    # (notes/122 §1).  `f_7100fafca0` at 0xfafcfc tests whether the loop's
    # flag temp ALREADY EXISTS and creates it only when it does not
    # (0xfafd04), then assigns `flag = 1.0` at 0xfafd4c-0xfafd84 -- so a
    # second continue in one body reuses the flag rather than being a new
    # shape.  And the guard is built at 0xfafc10..0xfafc70 around `*(168)`,
    # the statement list OF THE LEVEL BEING WALKED, which is what
    # `CFLAG_TEST` already does here for whatever level found the continue.
    # An ELSE arm is still refused: the kind-7 handler is reached for an arm
    # holding a continue, and what an else arm does there is not read.
    if has_else or (ENV.get("G2S_NOCONTDEEP")
                    and (top["used"] or _WALK_DEPTH[0] != top["depth"])):
        raise NotEstablished(
            "a continue in a shape not read: only one "
            "if-without-else at the loop body's level")
    top["used"] = True
    sub[-1] = (sub[-1][0], sub[-1][1][:-1] + [_Marker("CFLAG_SET")])


def _selection_arms(lv, bounds, merge, has_else):
    """Each arm's blocks, its end classified; and whether one continues.

    AN ARM MAY BE SEVERAL BLOCKS, and may itself contain a structured `if`,
    so each arm is flattened by the same walk.  The arm's LAST block must
    still end at the merge -- or leave the loop, or end the shader."""
    armblocks = []
    continues = False
    for lo, hi in bounds:
        sub = lv.blocks_between(lo, hi)
        last = sub[-1][1][-1] if sub and sub[-1][1] else None
        if last is None:
            raise NotEstablished("an empty branch arm")
        if last.opcode in KILLS:
            pass                        # the arm ends the shader
        elif (last.opcode in (Op.OpReturn, Op.OpReturnValue,
                              Op.OpUnreachable)
                and not ENV.get("G2S_NORETARM")):
            # AN ARM THAT RETURNS ends the shader too, and the compiler
            # prints it inside the IF: `debug_mask_sky.frag`'s
            # `if (b) { SV_Target0 = vec4(0.0); return; }` is
            #     IF    NE.x;
            #     MOV.F result_color0, {0, 0, 0, 0};
            #     RET   (TR);
            #     ENDIF;
            # and the body continues after the ENDIF.  That is the same
            # shape `KILLS` already takes -- an arm that does not reach the
            # merge because it leaves the shader -- and `_arm_return`
            # already emits the `RET`.  `G2S_NORETARM=1` refuses it again.
            pass
        elif _LOOP_MERGES and _is_branch_to(last, _LOOP_MERGES[-1]):
            _break_arm(sub)
        elif _LOOP_CONTS and _is_branch_to(last, _LOOP_CONTS[-1]["cont"]):
            _continue_arm(sub, has_else)
            continues = True
        elif not _is_branch_to(last, merge):
            if ENV.get("G2S_ARMDBG"):
                import sys as _s
                print("ARMDBG last=%s args=%s merge=%s loopmerges=%s "
                      "conts=%s" % (getattr(last, "opcode", None),
                                    last.args() if last is not None else None,
                                    merge, _LOOP_MERGES,
                                    [c["cont"] for c in _LOOP_CONTS]),
                      file=_s.stderr)
            raise NotEstablished(
                "a branch arm that does not join at the merge")
        armblocks.append(sub)
    return armblocks, continues


def _selection(lv, ins, term):
    """A structured `if` (OpSelectionMerge + OpBranchConditional)."""
    merge = ins[-2].args()[0]
    cond, t, f = term.args()[0], term.args()[1], term.args()[2]
    has_else = f != merge                       # else: an `if` with no else
    mi = lv.order.index(merge)
    ti = lv.order.index(t)
    bounds = ([(ti, lv.order.index(f)), (lv.order.index(f), mi)]
              if has_else else [(ti, mi)])
    if ti >= mi or any(hi <= lo for lo, hi in bounds):
        raise NotEstablished("a branch whose arms are not in order")
    armblocks, continues = _selection_arms(lv, bounds, merge, has_else)
    lv.out.extend(ins[:-2])
    kept = _const_arm(cond)
    if kept is not None and continues:
        raise NotEstablished("a continue under a constant condition")
    if kept is not None:
        # A constant condition: only the arm it selects survives, inline,
        # with no IF around it (`_const_arm`).  An `if` with no else whose
        # condition is false leaves nothing.
        if kept == "then":
            lv.out.extend(_flatten_blocks(armblocks[0], merge))
        elif has_else:
            lv.out.extend(_flatten_blocks(armblocks[1], merge))
        lv.i = mi
        return
    lv.out.append(_Marker("IF", cond))
    lv.out.extend(_flatten_blocks(armblocks[0], merge))
    if has_else:
        lv.out.append(_Marker("ELSE"))
        lv.out.extend(_flatten_blocks(armblocks[1], merge))
    lv.out.append(_Marker("ENDIF"))
    if continues:
        # the rest of the body, under `if (flag == 0)`
        lv.out.append(_Marker("CFLAG_TEST"))
        lv.guards += 1
    lv.i = mi


def _loop_shape(lv, ins, term):
    """The loop's blocks, checked against the one shape read: (condition
    block, its terminator, body blocks, continue block, merge index)."""
    merge, cont = ins[-2].args()[0], ins[-2].args()[1]
    cd = term.args()[0]
    i = lv.i
    if cd not in lv.byid or lv.order.index(cd) != i + 1:
        raise NotEstablished("a loop whose condition block does not follow "
                             "its header")
    cins = lv.byid[cd]
    ct = cins[-1] if cins else None
    if (ct is None or ct.opcode != Op.OpBranchConditional
            or ct.args()[2] != merge):
        raise NotEstablished("a loop whose condition does not exit to its "
                             "merge")
    body0, ci, mi = ct.args()[1], lv.order.index(cont), lv.order.index(merge)
    bi = lv.order.index(body0)
    if not (i + 1 < bi <= ci < mi):
        raise NotEstablished("a loop whose blocks are not in order")
    ctail = lv.byid[cont]
    if (not ctail or not _is_branch_to(ctail[-1], lv.order[i])
            or ci + 1 != mi):
        raise NotEstablished("a loop whose continue block does not branch "
                             "back to its header")
    sub = lv.blocks_between(bi, ci)
    last = sub[-1][1][-1] if sub and sub[-1][1] else None
    if last is None or not _is_branch_to(last, cont):
        raise NotEstablished(
            "a loop body that does not end at its continue block")
    return merge, cont, cins, ct, sub, ctail, mi


def _loop_body(merge, cont, sub):
    """The body flattened with the loop's break and continue targets
    pushed; returns (flattened body, whether a continue was taken)."""
    _LOOP_MERGES.append(merge)
    _LOOP_CONTS.append({"cont": cont, "depth": _WALK_DEPTH[0] + 1,
                        "used": False})
    try:
        body = _flatten_blocks(sub, cont)
        used = _LOOP_CONTS[-1]["used"]
    finally:
        _LOOP_MERGES.pop()
        _LOOP_CONTS.pop()
    return body, used


def _loop(lv, ins, term):
    """A STRUCTURED LOOP (notes/64), the shape glslang emits for `while` and
    `for`:

        H:  OpLoopMerge M C; OpBranch Cd
        Cd: <condition>; OpBranchConditional c, B, M
        B:  <body>; OpBranch C          (one or more blocks)
        C:  <continue>; OpBranch H
        M:  <after the loop>

    printed as REP / the constant test / the condition / IF / the body and
    continue / ELSE BRK / ENDIF / ENDREP."""
    merge, cont, cins, ct, sub, ctail, mi = _loop_shape(lv, ins, term)
    # The condition block's branch is the reader's `if (c) {body} else
    # break`; on a constant `c` the simplifier keeps one arm (`_const_arm`).
    # `while (true)` / `for (;;)` keep the body with no IF around it
    # (`0071_lp_wbrk.vert`).  A constant false would leave the bare `break` -- a
    # shape with no probe, so refused.
    kept = _const_arm(ct.args()[0])
    if kept == "else":
        raise NotEstablished("a loop whose condition is constant false")
    lv.out.extend(ins[:-2])
    lv.out.append(_Marker("REP"))
    lv.out.extend(cins[:-1])
    if kept is None:
        lv.out.append(_Marker("LOOPIF", ct.args()[0]))
    body, used = _loop_body(merge, cont, sub)
    if used:
        # The flag's `= 0` is the body's first IR statement
        # (`g2s_trace_wstmt`, 0071_lp_wcont.vert).  Where the continue block's own
        # code would go relative to the guard, and where the init sits when
        # the loop's condition is not folded away, have no probe: both
        # refused.
        if kept is None or ctail[:-1]:
            raise NotEstablished(
                "a continue in a loop with a condition or a continue block "
                "of its own (not read)")
        lv.out.append(_Marker("CFLAG_INIT"))
    lv.out.extend(body)
    lv.out.extend(ctail[:-1])
    if kept is None:
        lv.out.append(_Marker("LOOPELSE"))
        lv.out.append(_Marker("ENDIF"))
    lv.out.append(_Marker("ENDREP"))
    lv.i = mi


def _walk_level(body):
    lv = _Level(body)
    while lv.i < len(lv.order):
        ins = lv.byid[lv.order[lv.i]]
        term = ins[-1] if ins else None
        if _headed_by(ins, term, Op.OpBranchConditional,
                      Op.OpSelectionMerge):
            _selection(lv, ins, term)
        elif _headed_by(ins, term, Op.OpSwitch, Op.OpSelectionMerge):
            merge = ins[-2].args()[0]
            lv.out.extend(ins[:-2])
            lv.out.extend(_switch_chain(term, merge, lv.order, lv.byid,
                                        lv.i))
            lv.i = lv.order.index(merge)
        elif _headed_by(ins, term, Op.OpBranch, Op.OpLoopMerge):
            _loop(lv, ins, term)
        elif term is not None and term.opcode == Op.OpBranch:
            # A branch to the block that FOLLOWS it is a fallthrough and
            # emits nothing: glslang ends a block at every merge point even
            # when control simply carries on.  A branch anywhere else is a
            # loop or an unstructured jump and is refused.
            if (lv.i + 1 < len(lv.order)
                    and term.args()[0] == lv.order[lv.i + 1]):
                lv.out.extend(ins[:-1])
                lv.i += 1
                continue
            raise NotEstablished("an unstructured branch")
        else:
            lv.out.extend(ins)
            lv.i += 1
    lv.out.extend(_Marker("ENDIF") for _ in range(lv.guards))
    return lv.out
