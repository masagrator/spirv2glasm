"""dataflow.py -- `f_710006e7c0`, the front end's block liveness over NAMES.

What the allocator's seed (`f_7100048820`, py/liveness.py:seed) reads is a
block's `+0xb0` set: per cgc NAME (an index into `prog[832]`, numbered by
f_710006f340), the components live out of the block.  notes/55 §3-4 reads how
it is made; this module is that reading, in the three sets the function keeps
per block:

  `+0x80` KILL   every block[80] entry with a node: `compress(node[48])`
                 (0x6ee50)
  `+0x68` GEN    every block[72] entry whose node is op 0x2b, `node[48]`,
                 in a block whose `block[40]` is non-zero (0x6ef60..0x6efa8)
  `+0xb0` OUT    starts as every block[80] entry with a node whose
                 `entry[76]` is set OR that `cg->vt[872]` accepts, at
                 `entry[72]` (0x6ee74..0x6ee4c); then the solver
                 `f_710006f980` grows it to the fixpoint of

                     out(B) |= (out(S) & ~kill(S)) | gen(S)

                 over B's successors S -- `block[272]` and `block[280]`, or
                 the `block[312]` list (f_710005aa20 chooses).

`cg->vt[872]` is f_7100bdff14: `cg[8][236] == 1` (f_710002ead0) and, for a
store (op 0x3a), the stored-to object's `[44] != 0x19`.  At `--opt-level
none` the first half holds, so EVERY store is live out of its block at the
mask it stores -- the band notes/52 measured.  The `[44] == 0x19` exception
is read but no probe or corpus shader reaches it; `init_out` takes it as an
input (`store_kept`) rather than guess.

The solver runs region by region (`prog[920]`, indexed by `block[60]`: the
FUNCTIONS) and folds a callee's summary sets into its call blocks
(0x6fd6c..0x6fdc4).  That fold is not transcribed: a block whose sets come
from a callee is not described by its own entries, and `solve` has no way to
see one -- tools/dfcheck.py reports such blocks from the GEN/KILL check.
"""


def compress(mask32):
    """f_71000519a0 as f_71000589b4 applies it: bit c iff byte c is 0xff."""
    return sum(1 << c for c in range(4)
               if ((mask32 >> (8 * c)) & 0xff) == 0xff)


class Entry(object):
    """A block[72] / block[80] entry: `entry[28]` name, `entry[72]` mask,
    `entry[76]` flag, and its node's opcode and `node[48]` (None: no node)."""

    __slots__ = ("name", "mask", "f76", "op", "nmask")

    def __init__(self, name, mask, f76, op=None, nmask=0):
        self.name = name
        self.mask = mask
        self.f76 = f76
        self.op = op
        self.nmask = nmask


class Block(object):
    __slots__ = ("addr", "w40", "succ", "l72", "l80")

    def __init__(self, addr, w40, succ, l72, l80):
        self.addr = addr
        self.w40 = w40
        self.succ = succ            # successor addresses, in solver order
        self.l72 = l72
        self.l80 = l80


def _or(d, n, v):
    if v:
        d[n] = d.get(n, 0) | v


def kill(b):
    out = {}
    for e in b.l80:
        if e.op is not None:
            _or(out, e.name, compress(e.nmask))
    return out


def gen(b):
    out = {}
    if not b.w40:
        return out
    for e in b.l72:
        if e.op == 0x2b:
            _or(out, e.name, compress(e.nmask))
    return out


def init_out(b, store_kept=lambda e: True):
    """The OUT set before the solver.  `store_kept(e)` is `cg->vt[872]` for
    a store entry; the default is `--opt-level none` with no `[44] == 0x19`
    target (the only case measured)."""
    out = {}
    for e in b.l80:
        if e.op is None:
            continue
        if e.f76 or (e.op == 0x3a and store_kept(e)):
            _or(out, e.name, compress(e.mask))
    return out


def solve(blocks, store_kept=lambda e: True):
    """Every block's `+0xb0`, `{addr: {name: nibble}}`.  `blocks` in chain
    order; the fixpoint does not depend on the visiting order."""
    by = dict((b.addr, b) for b in blocks)
    gens = dict((b.addr, gen(b)) for b in blocks)
    kills = dict((b.addr, kill(b)) for b in blocks)
    out = dict((b.addr, init_out(b, store_kept)) for b in blocks)
    changed = True
    while changed:
        changed = False
        for b in reversed(blocks):
            ob = out[b.addr]
            for s in b.succ:
                if s not in by:
                    continue
                os_, gs, ks = out[s], gens[s], kills[s]
                for n in set(os_) | set(gs):
                    v = (os_.get(n, 0) & ~ks.get(n, 0)) | gs.get(n, 0)
                    if v & ~ob.get(n, 0):
                        ob[n] = ob.get(n, 0) | v
                        changed = True
    return out
