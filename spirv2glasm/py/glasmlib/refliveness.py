"""refliveness.py -- notes/18's reaching-definition liveness, kept for reference.

NOTHING CALLS THIS.  The `used` gate is a mention count (usage._live_ids);
notes/18 built this walk on a misreading, and over 3,442 corpus shaders it
disagrees with the mention count on exactly one shader -- where it is the one
that is wrong (see usage.py).  It is kept because its four corrections are
each a real observation about the front end, and re-deriving them would be
expensive if a later surface (the BODY, where dead code really is dropped)
needs them.

`live_ids(module)` answers: the ids whose value a front end would still have a
reference node for.  `used` is not "the id is mentioned".  A vertex shader can
load an input, store it into a local and never read the local again, and the
compiler still prints `: 0` for that input -- which is exactly the shape
glslang emits at -O0 for an unused attribute, and exactly what a mention count
gets wrong.  What the bit records is that a symbol REFERENCE node survived
(notes/14), so this is a liveness fixpoint:

  * the operands of the side-effecting instructions are live, as is a store
    whose target is not function-local (an output, a buffer);
  * a value that is live makes its own operands live;
  * a pointer that is loaded by a live load makes its base VARIABLE live, and
    a store into a live variable makes its stored value live;
  * a call is NOT a root.  Its arguments are live only if the matching
    parameter is live inside the callee -- glslang passes every input through
    an `out`-style local into a function, and treating the call itself as a
    use marks every attribute live, which is exactly what the compiler does
    not do.  The front end inlines and then drops the reference, so following
    the parameter is the closer model.
"""
from spvnames import Op, StorageClass

from glasmlib.usage import _SIDE_EFFECT, _id_args, _pointer_base, _ptr_key

# A store is observable when its target outlives the shader -- an output, a
# buffer, an image.  `Private` is NOT one of those: a translated HLSL shader
# keeps its scratch in module-scope `Private` variables (`u_xlat5` and
# friends) and treating a write to one as observable makes every value that
# ever passes through the scratch look used, which is the `map_1465b18f.frag`
# defect.
_LOCALISH = (StorageClass.Function, StorageClass.Private)


class _Module(object):
    """The module's definitions, stores, loads and calls, collected once."""

    def __init__(self, module):
        self.module = module
        self.defs = {}
        self.stores = []
        self.loads = []
        for fn in module.functions:
            for ins in fn.insns:
                r = getattr(ins, "result", None)
                if r:
                    self.defs[r] = ins
                if ins.opcode == Op.OpStore:
                    self.stores.append(ins)
                elif ins.opcode == Op.OpLoad:
                    self.loads.append(ins)
        self.params = {fn.result: list(fn.params) for fn in module.functions}
        # Precompute every pointer's base once: `_pointer_base` walks an
        # access chain, and doing it inside the fixpoint made the walk
        # quadratic on the larger corpus shaders.
        self.store_base = {id(i): self.base(i.args()[0]) for i in self.stores}
        self.load_base = {id(i): self.base(i.args()[0]) for i in self.loads}
        self.store_key = {id(i): self.key(i.args()[0]) for i in self.stores}
        self.load_key = {id(i): self.key(i.args()[0]) for i in self.loads}

    def base(self, pid):
        return _pointer_base(self.module, pid, self.defs)

    def key(self, pid):
        return _ptr_key(self.module, pid, self.defs)

    def storage_of(self, vid):
        g = self.module.globals.get(vid)
        if g is not None:
            return g.operands[2]
        d = self.defs.get(vid)
        if d is not None and d.opcode == Op.OpVariable:
            return d.args()[0]
        return None

    def param_ids(self, f):
        return [p if isinstance(p, int) else p.operands[1]
                for p in self.params.get(f, [])]


def _callee_summaries(m):
    """Per function: the parameters it reads, and whether it does anything
    observable (itself or through a call).

    The front end INLINES, so liveness at a call site is not a property of
    the callee alone: a helper called twice, once for a value that is used
    and once for a value that is thrown away, must not make both arguments
    live.  Marking the shared parameter live and reading it back does exactly
    that, so what is used instead is a per-callee SUMMARY, applied at each
    call site under that site's own condition."""
    reads_param, self_effect = {}, {}
    for fn in m.module.functions:
        pids = set(m.param_ids(fn.result))
        rd = set()
        eff = False
        for ins in fn.insns:
            if ins.opcode == Op.OpLoad:
                b = m.base(ins.args()[0])
                if b in pids:
                    rd.add(b)
            elif ins.opcode == Op.OpStore:
                b = m.base(ins.args()[0])
                if m.storage_of(b) not in _LOCALISH and b not in pids:
                    eff = True
            elif (ins.opcode in _SIDE_EFFECT
                  and ins.opcode != Op.OpFunctionCall):
                eff = True
        reads_param[fn.result] = rd
        self_effect[fn.result] = eff
    # Side effects propagate through calls.
    grew = True
    while grew:
        grew = False
        for fn in m.module.functions:
            if self_effect.get(fn.result):
                continue
            for ins in fn.insns:
                if ins.opcode == Op.OpFunctionCall and \
                        self_effect.get(ins.args()[0]):
                    self_effect[fn.result] = True
                    grew = True
                    break
    return reads_param, self_effect


def _returns_and_calls(m):
    """{function: [returned ids]} and [(callee, args, result)].

    A function's returned value is live only when some CALL to it has a live
    result.  Treating `OpReturnValue` as a root instead makes every helper's
    inputs live, which is how an unused attribute passed to a helper whose
    result is thrown away ends up wrongly marked used."""
    returns, calls = {}, []
    for fn in m.module.functions:
        for ins in fn.insns:
            if ins.opcode == Op.OpReturnValue:
                returns.setdefault(fn.result, []).append(ins.args()[0])
            elif ins.opcode == Op.OpFunctionCall:
                a = ins.args()
                calls.append((a[0], a[1:], getattr(ins, "result", None)))
    return returns, calls


def _overlaps(a, b):
    """Two index paths name overlapping locations when either is a prefix of
    the other; an unknown (dynamic) path overlaps everything."""
    if a is None or b is None:
        return True
    return a[:len(b)] == b or b[:len(a)] == a


def _successors(blocks):
    """Each block's successor block indices, from its terminator."""
    index = {lbl: n for n, (lbl, _) in enumerate(blocks)}
    succ = []
    for _lbl, insns in blocks:
        t = insns[-1] if insns else None
        out = []
        if t is not None:
            if t.opcode == Op.OpBranch:
                out = [t.args()[0]]
            elif t.opcode == Op.OpBranchConditional:
                out = t.args()[1:3]
            elif t.opcode == Op.OpSwitch:
                a = t.args()
                out = [a[1]] + list(a[3::2])
        succ.append([index[x] for x in out if x in index])
    return succ


def _block_entry_states(m, blocks, succ):
    """The reaching stores at each block's entry: a fixpoint over the
    successor edges of `(base, path) -> {stores}` maps."""
    def step(state, ins):
        if ins.opcode != Op.OpStore:
            return
        base, path = m.store_key[id(ins)]
        for k in [k for k in state if k[0] == base
                  and (path is None or _overlaps(k[1], path))]:
            del state[k]
        state[(base, path)] = {ins}

    ins_state = [dict() for _ in blocks]
    out_state = [None] * len(blocks)
    changed = True
    while changed:
        changed = False
        for n, (_lbl, insns) in enumerate(blocks):
            st = {}
            for p, ss in enumerate(succ):
                if n in ss and out_state[p] is not None:
                    for k, v in out_state[p].items():
                        st.setdefault(k, set()).update(v)
            if st != ins_state[n]:
                ins_state[n] = st
                changed = True
            cur = {k: set(v) for k, v in st.items()}
            for ins in insns:
                step(cur, ins)
            if out_state[n] != cur:
                out_state[n] = cur
                changed = True
    return ins_state, step


def _reaching_definitions(m):
    """{id(load): stores reaching it}, {call argument: stores reaching it}.

    "Any store into a variable something reads" is too coarse: in
    `branches_depthprepass_vs_nowind_unrolledinput.vert` a local is written,
    read, and written again, and the LAST write is dead.  Straight-line order
    is too coarse the other way: the read and the writes are in different
    basic blocks.  So locations are (base, constant index path), gen/kill per
    block, and a fixpoint over the successor edges.  A store through a
    non-constant index kills everything known about its base."""
    reaching, arg_reaching = {}, {}
    for fn in m.module.functions:
        by_base = {}
        for ins in fn.insns:
            if ins.opcode == Op.OpStore:
                by_base.setdefault(m.store_base[id(ins)], []).append(ins)
        blocks = fn.blocks or [(None, fn.insns)]
        ins_state, step = _block_entry_states(m, blocks, _successors(blocks))

        def read(state, base, path):
            out = set()
            for (b, p), v in state.items():
                if b == base and _overlaps(p, path):
                    out |= v
            return out or set(by_base.get(base, []))

        for n, (_lbl, insns) in enumerate(blocks):
            cur = {k: set(v) for k, v in ins_state[n].items()}
            for ins in insns:
                if ins.opcode == Op.OpLoad:
                    b, p = m.load_key[id(ins)]
                    reaching[id(ins)] = read(cur, b, p)
                elif ins.opcode == Op.OpFunctionCall:
                    # Passing a pointer to a call READS whatever was last
                    # written through it, so an argument behaves like a load
                    # at that point: without this, the store that fills a
                    # call's `in` parameter is never reached and every
                    # argument looks dead.
                    for a in ins.args()[1:]:
                        b, p = m.key(a)
                        arg_reaching.setdefault(a, set()).update(
                            read(cur, b, p))
                step(cur, ins)
    return reaching, arg_reaching


class _Live(object):
    """The live set and its work list."""

    def __init__(self):
        self.ids = set()
        self.work = []
        self.changed = False

    def mark(self, i):
        if i and i not in self.ids:
            self.ids.add(i)
            self.work.append(i)
            self.changed = True


def _roots(m, live):
    for fn in m.module.functions:
        for ins in fn.insns:
            if ins.opcode in _SIDE_EFFECT:
                for w in _id_args(ins):
                    live.mark(w)
    for ins in m.stores:
        base = m.base(ins.args()[0])
        if m.storage_of(base) not in _LOCALISH:
            live.mark(ins.args()[0])
            live.mark(ins.args()[1])
            live.mark(base)


def _drain(m, live):
    """A live value makes its operands live -- except a call's."""
    while live.work:
        w = live.work.pop()
        ins = m.defs.get(w)
        if ins is None or ins.opcode == Op.OpFunctionCall:
            # A call's ARGUMENTS are not live just because its result is:
            # glslang passes each attribute into a helper through a local and
            # the compiler reports the attribute unused when the helper never
            # reads that parameter.  The parameter edges carry the liveness
            # in whichever direction it really flows.
            continue
        for a in _id_args(ins):
            live.mark(a)


def _through_loads(m, live, reaching):
    for ins in m.loads:
        if ins.result not in live.ids:
            continue
        sts = reaching.get(id(ins)) or ()
        if not sts:
            # Nothing wrote it in this function: the value comes from the
            # variable itself (an input, a uniform, a parameter).
            live.mark(m.load_base[id(ins)])
            continue
        for st in sts:
            for a in st.args():
                live.mark(a)


def _through_calls(m, live, calls, returns, arg_reaching, reads_param,
                   self_effect):
    """A live parameter makes the argument passed for it live, and a call
    whose result is live makes the callee's returned value live."""
    for callee, args, res in calls:
        if res in live.ids:
            for rv in returns.get(callee, ()):
                live.mark(rv)
        for a in args:
            if a in live.ids:
                for st in arg_reaching.get(a, ()):
                    for x in st.args():
                        live.mark(x)
        if res in live.ids or self_effect.get(callee):
            rd = reads_param.get(callee, set())
            for pid, a in zip(m.param_ids(callee), args):
                if pid in rd:
                    live.mark(a)


def live_ids(module):
    """The live ids, module-scope variables included (see the module doc)."""
    m = _Module(module)
    reads_param, self_effect = _callee_summaries(m)
    returns, calls = _returns_and_calls(m)
    reaching, arg_reaching = _reaching_definitions(m)
    live = _Live()
    _roots(m, live)
    live.changed = True
    while live.changed:
        live.changed = False
        _drain(m, live)
        _through_loads(m, live, reaching)
        _through_calls(m, live, calls, returns, arg_reaching, reads_param,
                       self_effect)
    return live.ids
