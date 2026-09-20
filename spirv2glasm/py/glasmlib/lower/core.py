"""core.py -- the lowering's state, its shared helpers, and its main loop.

The body is lowered one SPIR-V instruction at a time into placeholder lines
(`#n` for a value, the compiler's `node[36]` order being the order lines are
made in).  The arms that lower each instruction family are the mixins of
lower/__init__.py; what they share lives here:

* the STATE -- the values' operand text, the locals' registers and pending
  stores, the block keys, the ties and cuts the scheduler needs;
* the HELPERS every family uses -- a fresh placeholder, the block key, the
  temp flush, the walker's second-store test, the name and construct reads;
* the LOOP, which runs the arms in their fixed order until one takes the
  instruction.
"""
import lex as _lex

from spvnames import Op, StorageClass

import opchain as _opchain
import sched as _sched
from glasmlib.common import NotEstablished, ENV, OP_NAME, ACCESS_CHAINS, \
    BOOL_CONSTANTS
from glasmlib.types import _components, _pointee_components
from glasmlib.operands import _scalar_value
from glasmlib.text import _COMPONENTS, _emit, _swizzle
from glasmlib.usage import _fold_identities, _lmem_arrays, _use_count
from glasmlib.boolean import _bool_constant
from glasmlib.cflow import _CONST_BOOLS, _Marker, MARKER, _flatten
from glasmlib import nodes

# The lines that end a block for `_blk_start`: control flow, and a `.CC`
# move.
_CONTROL_WORDS = ("IF", "ELSE", "ENDIF", "REP", "ENDREP", "BRK", "CONT",
                  "BB")


def _is_control_line(l):
    r"""`^\s*(IF|ELSE|ENDIF|REP|ENDREP|BRK|CONT|BB)` -- a PREFIX, no word
    boundary after it."""
    return l.lstrip(_lex.WS).startswith(_CONTROL_WORDS)


def _no_word_or_bracket_after(text, e):
    r"""`(?![\w\[])`"""
    return (e if e == len(text) or not (_lex.is_word(text[e])
                                        or text[e] == "[") else -1)


def _whole_read_tail(text, e):
    r"""`(?![\d.])`"""
    return (e if e == len(text) or (text[e] not in _lex.DIGITS
                                    and text[e] != ".") else -1)


def _swizzle_read_tail(text, e):
    r"""`(\.[xyzw]{1,4})(?![\w\[])`: the end past the swizzle, or -1."""
    if e >= len(text) or text[e] != ".":
        return -1
    f = e + 1
    while f < len(text) and text[f] in _lex.XYZW:
        f += 1
    if not 1 <= f - e - 1 <= 4:
        return -1
    return _no_word_or_bracket_after(text, f)

# The instructions that make no node of their own, for the block's "holds a
# node" test.
_NO_NODE = (Op.OpLoad, Op.OpAccessChain, Op.OpInBoundsAccessChain,
            Op.OpVariable, MARKER)
# ... nor do a shuffle and an extract: they are a selector on their operand
# (composite.py).  `G2S_SWZNODE=1` counts them, as before notes/100.
_SELECTORS = (Op.OpVectorShuffle, Op.OpCompositeExtract)


def _local_chain(module, pid, by_result, locals_):
    """(variable, component) for a chain that names one component of a local.

    A translated shader writes its scratch a component at a time --
    `u_xlat0.x = ...` -- which is 6,523 of the corpus sample's stores.  The
    front end forwards those (notes/25), so tracking them per component here
    emits nothing at all; the component is just remembered.
    """
    ch = by_result.get(pid)
    if ch is None or ch.opcode not in ACCESS_CHAINS:
        return None
    if ch.args()[0] not in locals_ or len(ch.args()) != 2:
        return None
    v = _scalar_value(module, ch.args()[1])
    if v is None or not 0 <= int(v) < 4:
        return None
    return ch.args()[0], int(v)


def _token_dest(l, tok):
    r"""`(\S+\s+)(TOK)((?:\.[xyzw]+)?),` matched: (the start of TOK, the
    index of the comma), or None."""
    f = _lex.split_first(l)
    if (f is None or f[0] != 0 or f[2] == f[1]
            or not l.startswith(tok, f[2])):
        return None
    e = f[2] + len(tok)
    c = e
    if c < len(l) and l[c] == ".":
        c += 1
        while c < len(l) and l[c] in _lex.XYZW:
            c += 1
        if c == e + 1:
            c = e
    if c >= len(l) or l[c] != ",":
        return None
    return f[2], c


def _is_placeholder(text):
    return _lex.is_numbered(text or "", "#")


class Core(object):
    """The state and the helpers; `run()` lowers the body."""

    # The arms, in the order the loop tries them.  Each takes the
    # instruction and returns True when it lowered it; the first that does
    # ends the instruction.  An arm may also refuse (NotEstablished).
    ARMS = (
        "_arm_parameter_load", "_arm_load", "_arm_store",
        "_arm_matrix_times_vector", "_arm_negating_multiply",
        "_arm_arithmetic",
        "_arm_sampled_image", "_arm_image", "_arm_image_op",
        "_arm_image_write", "_arm_image_read",
        "_arm_extract", "_arm_construct",
        "_arm_derivative", "_arm_dot", "_arm_int_binary",
        "_arm_bitfield_insert", "_arm_any", "_arm_logical",
        "_arm_convert",
        "_arm_compare",
        "_arm_continue_flag", "_arm_marker",
        "_arm_select", "_arm_shuffle", "_arm_negate", "_arm_extinst",
        "_arm_emit_vertex", "_arm_end_primitive", "_arm_kill",
        "_arm_call", "_arm_return_value", "_arm_return",
        "_arm_variable", "_arm_access_chain", "_arm_bitcast",
    )

    def __init__(self, module, entry_name="main"):
        self.module = module
        ep = module.entry_point(entry_name)
        self.model = ep[0]
        self._init_program(ep)
        self._init_values()
        self._init_locals()
        self._init_blocks()
        self._init_loads()
        self._init_calls()

    # -- initialisation --------------------------------------------------

    def _init_program(self, ep):
        """The entry function flattened, and the functions it calls.

        Structured control flow is flattened into markers (cflow.py).

        SUBROUTINES (notes/68).  The driver (0xf0fa7c..0xf0fb14) compiles the
        entry function, appends a RET to it when the program has other
        functions, and then compiles each of them (f_7100f103d0 with the
        function's symbol); the caller reaches one through CAL.  Only the
        read shape is taken: every other function is called exactly once,
        from a straight-line entry function, and is itself one block with no
        call of its own."""
        module = self.module
        fns = [f for f in module.functions]
        entry = [f for f in fns if f.result == ep[1]]
        if len(entry) != 1:
            raise NotEstablished("no function for the entry point")
        entry = entry[0]
        _CONST_BOOLS.clear()
        for _cid, _cins in module.constants.items():
            if _cins.opcode in BOOL_CONSTANTS:
                _CONST_BOOLS[_cid] = _cins.opcode == Op.OpConstantTrue
        self.folded_ids = _fold_identities(module)
        insns = _flatten(entry.insns)
        callees = [f for f in fns if f is not entry]
        if callees:
            _calls = [i for i in insns if i.opcode == Op.OpFunctionCall]
            _called = [i.args()[0] for i in _calls]
            if (sorted(_called) != sorted(f.result for f in callees)
                    or any(isinstance(i, _Marker) for i in insns)):
                raise NotEstablished(
                    "a call shape not read: every function called once from a "
                    "straight-line entry")
            _order = dict((fid, k) for k, fid in enumerate(_called))
            callees.sort(key=lambda f: _order[f.result])
            insns = insns + [_Marker("MAINEND")]
            for f in callees:
                _fl = _flatten(f.insns)
                if (any(isinstance(i, _Marker)
                        or i.opcode == Op.OpFunctionCall for i in _fl)
                        or len(f.blocks) != 1):
                    raise NotEstablished(
                        "a called function that is not one block without "
                        "calls")
                insns = insns + [_Marker("FUNC", f)] + _fl
        self.insns = insns
        self.callees = callees
        self.callee_by_id = dict((f.result, f) for f in callees)

    def _init_values(self):
        module = self.module
        self.values = {}                # id -> source operand text
        self.comps = {}                 # value id -> its component mapping
        self.scalar = set()             # value ids that are one component
        self.samplers = {}              # value id -> the sampler variable
        self.splats = set()             # a swizzle of the temp's name
        self.splat_src = {}             # a splat -> the scalar it repeats
        self.splat_nodes = set()        # splats made a MOV by a local store
        self.pending_images = []        # storage-image loads not yet used
        self.lname = {}                 # a local component load -> (reg, c)
        self.con_src = {}               # construct vreg -> ({c: src}, block)
        self.con_flat = {}              # construct vreg -> (entries, block)
        # values that are ONE COMPONENT of a wider register write (a shadow
        # sample's result): stored like an extract of one (notes/99)
        self.component_values = set()
        self.head_src = {}              # a construct's component-0 source
        self.matrices = {}              # a matrix load -> its column loads
        self.negated = set()            # values that are a carrier MOV
        self.normalised = set()         # bool compares already normalised
        self.norm_local = {}            # bool local -> stored only from
                                        # normalised compares (True/False)
        self.arm_names = set()          # select results: names arms store
        self.facing_loads = set()       # loads of gl_FrontFacing (notes/88)
        self.counter = 0                # the last placeholder number
        self.band = set()               # band temps (notes/52)
        self.wide = set()               # placeholders a register wide
        self.by_result = {}
        for fn_ in module.functions:
            for i_ in fn_.insns:
                if i_.result is not None:
                    self.by_result[i_.result] = i_
        self.lines = []
        self.stores = 0
        self.store_no = 0               # every OpStore lowered so far
        self.cpair_tied = {}            # local -> its pair's halves share a
                                        # `node[36]` (notes/91)
        self.comp_load = {}             # component load -> ((local, c),
                                        # store_no at the load)
        self.name_read_seq = {}         # (block, name) -> its read's seq
        self.whole_load_at = {}         # whole local load -> (local,
                                        # store_no at the load)
        self.handles = 0               # LONG registers loaded
        self.wants_cc = False
        self.wants_h = False            # the continue flag's SHORT register
        self.scalarised = False         # has a gl_Position store been made?

    def _init_locals(self):
        """Which locals are MATERIALISED, and the locals' own state.

        A LOCAL THAT IS LOADED WHOLE IS MATERIALISED, not forwarded.
        `lo_parts.vert` (`vec4 v; v.x=..; v.y=..; v.z=..; v.w=..;
        gl_Position=v;`) gives the local a register of its own and writes it
        a component at a time, and each component write is a PAIR: the write
        itself, and a self-copy of the OTHER components (`MOV.F R1.yzw, R1;`).
        A local that is only ever read one component at a time stays
        forwarded: that is 6,523 of the corpus sample's stores and emits
        nothing.  ON BY DEFAULT (notes/55 §7): WHICH REGISTER the local gets
        is the transcribed allocator's (py/regalloc.py + py/ifg.py), so this
        path runs only when that one does.  `G2S_NOLOCALREG=1` (or
        `G2S_NOREGALLOC=1`) restores the refusal."""
        module = self.module
        self.use_regalloc = not ENV.get("G2S_NOREGALLOC")
        self.whole_load = set()
        self.lstored = {}               # local -> the components ever stored
        if self.use_regalloc and not ENV.get("G2S_NOLOCALREG"):
            for fn_ in module.functions:
                for i_ in fn_.insns:
                    self._scan_local_use(i_)
        # Function-local AND module-scope `Private` variables are both
        # forwarded.  A translated HLSL shader keeps its scratch in
        # module-scope `Private` globals (`u_xlat0` and friends) and they
        # produce no `#var` line and no symbol (notes/18), so they are locals
        # by another name.
        self.locals_ = {}
        self.local_reg = {}
        self.local_seen = False
        # LOCAL-MEMORY ARRAYS (notes/84): variable -> its `lmem<k>` number
        self.lmem_k = dict((v, k) for k, (v, _n)
                           in enumerate(_lmem_arrays(module)))
        self.lmem_elem = {}             # variable -> (element MOV, count)
        # PRIVATE ARRAYS NEVER INDEXED BY A VALUE live in registers (notes/87)
        self.reg_arrays = set()
        for _gid, _gins in module.globals.items():
            if (_gins.operands[2] != StorageClass.Private
                    or _gid in self.lmem_k):
                continue
            _pt = module.types.get(_gins.result_type)
            _at = module.types.get(_pt.operands[2]) if _pt is not None \
                else None
            if _at is not None and _at.opcode == Op.OpTypeArray:
                self.reg_arrays.add(_gid)
        for gid, gins in module.globals.items():
            if gins.operands[2] == StorageClass.Private:
                self.locals_[gid] = {}
        # a local's last component-store merge: var -> (block key, write
        # line, pass-through line, MOV) -- an output store of the local in the
        # same block takes the merge as its value (notes/85)
        self.cpair = {}
        self.merge_grp = {}             # merge vreg -> its statement's tie
        self.merged = set()             # the merge vregs `_merge_forward` made
        # merge vreg -> (the local's register, the lanes its pair wrote)
        self.merge_lanes = {}
        self.lfwd = {}                  # a local's forwardable store
        self.lfwd_line = {}             # local -> the line of that store
        self.load_of = {}               # a whole local load -> (var, reg)
        self.fwd_of = {}                # a forwarded local load -> value
        self.fwd_line = {}              # a forwarded load -> its store's line
        self.cfw = {}                   # a local's per-component forwards
        self.cfw_kind = {}              # (local, component) -> how the lane
                                        # was stored: "node" (plain, lane
                                        # x), "insert" (plain, another lane)
                                        # or "merge" (a pair)
        self.cfw_deep = {}              # a local stored whole from a SWIZZLE
                                        # of a node: {c: (block, node, sel)}
        self.cfw_via = {}               # (local, c) -> (its cfw entry, the
                                        # deep source a merge lane reads)
        self.lsplit = {}                # a load -> {component: forwarded}
        self.lpend = {}                 # a local's pending store's block
        self.stored_key = {}            # the block a name was last stored in
        self.ostore_blk = {}            # a colour output -> its store's block

    def _scan_local_use(self, i_):
        by_result = self.by_result
        if i_.opcode == Op.OpFunctionCall:
            # a call reads its argument variables by NAME (the caller's
            # in-copy, 0xf11c30..0xf11d28)
            self.whole_load.update(i_.args()[1:])
        if i_.opcode == Op.OpLoad:
            self.whole_load.add(i_.args()[0])
            # A LOAD THROUGH A CHAIN reads the local too: its stores are
            # materialised exactly as for a whole load.  `lv_wc.vert` (whole
            # store, `.y` read) prints `MOV.F R1, R0; MOV.F R0, R0;` and
            # reads `R1.y`; `lv_cc.vert` (component stores and reads) prints
            # each component store as the pair of notes/55 §7 (notes/72 §5).
            _pc = by_result.get(i_.args()[0])
            if _pc is not None and _pc.opcode in ACCESS_CHAINS:
                self.whole_load.add(_pc.args()[0])
        if i_.opcode == Op.OpStore:
            # which components of a local are ever stored: the pass-through
            # half of a component store copies THOSE, not every component of
            # the type (`lv_cc.vert`, a vec4 written at `.x` and `.y` only:
            # `MOV.F R2.y, R2;` beside the `.x` store).  A whole store holds
            # them all.
            _pc = by_result.get(i_.args()[0])
            chained = _pc is not None and _pc.opcode in ACCESS_CHAINS
            if chained and len(_pc.args()) == 2:
                _k = _scalar_value(self.module, _pc.args()[1])
                if _k is not None:
                    self.lstored.setdefault(_pc.args()[0], set()).add(int(_k))
            elif not chained:
                self.lstored.setdefault(i_.args()[0], set()).update(range(4))
            # A STORED LOCAL IS MATERIALISED EVEN WHEN NOTHING LOADS IT.  The
            # store is an assignment statement to the name, whatever reads it
            # later: `pt_e.vert` (`vec4 c = q * 2.0;`, never read) and
            # `pt_f.vert` (the same as a private) print `MOV.F R1, R0; MOV.F
            # R0, R0;`, and `pt_d.vert` a private stored and never read among
            # other stores (notes/73 §3).  "Loaded" was never a condition the
            # compiler tests.
            self.whole_load.add(_pc.args()[0] if chained else i_.args()[0])

    def _init_blocks(self):
        """THE NAME RECORD FOR A MATERIALISED LOCAL (notes/65 §4, notes/66):
        the statement walker opens a block before a store to a name whose
        store is pending from an EARLIER block (not touched since) when the
        current block already holds a node.  Every SPIR-V value is a named
        temp in the reader's IR (`tmp = expr; s = tmp`, g2s_trace_irtree), so
        the value the local takes is then read through the temp's NAME, and
        the temp's own store stays in the block before.  `blk_no` counts the
        marker blocks, `blk_node` says whether the current one holds a
        statement, `lpend` is a local's pending store and `defblk` a value's
        block."""
        self.cuts = []                  # lines that OPEN a block (sched.py)
        self.ties = []                  # line groups sharing one `node[36]`
        self.con_stmt = []              # (a construct statement's group, s0)
        self.con_lane_load = {}         # a lane-x load's line -> that group
        self.passthru = []              # a local store's pass-through half
        self.calls = []                 # writes that are not statements
        self.vsplit = 0                 # blocks opened by a second store
        self.blk_no = 0
        self.blk_node = False
        self.node_next = False
        self.defblk = {}                # a value -> the block it was made in
        self.defline = {}               # the first line a value's lowering made
        self.flush_q = []               # temps' stores pending to block end
        self.flushed = {}               # a flushed temp's name -> its value
        self.store_movs = []            # MOV lines a whole store makes
        self.stmtpos = {}               # a flushed name -> its statement line
        self.pflush = None              # a position `.x` temp to flush after
        self.model_gs_pos = None        # the geometry stage's gl_Position

    def _init_loads(self):
        self.ldc_same = {}              # a block load's location -> its node
        self.handle_same = {}           # a handle's buf14 offset -> (block, D)
        self.node_loads = set()         # loads whose value is a node's (MOV)
        self.node_line = {}             # such a load -> (its MOV line, dest)
        self.node_src = {}              # ... -> how to make it again
        self.node_bkey = {}             # ... -> the block it was made in
        self.dead_lines = set()         # lines made and then superseded
        self.ldc_canon = {}             # a repeated load -> the first one's id
        self.con_blk = {}               # a same-node construct -> its block
        self.ldc_line = {}              # a static block load -> its LDC line
        self.ldc_vreg = {}              # a static LDC's vreg -> block, form
        self.ldc_lines = set()          # the static LDC lines
        self.ldc_dyn = {}               # a dynamic load's vreg -> its block
        self.ldc_at = {}                # a block load -> (its line, its dst)
        self.carriers = set()           # vregs an address carrier writes

    def _init_calls(self):
        self.formal = {}                # a parameter id -> its register
        self.retreg = {}                # a function id -> its return register
        self.fgrp = {}                  # a formal register -> its entry tie
        self.cur_fn = None
        self.callnames = set()          # the call's front-end names

    # -- placeholders and blocks -----------------------------------------

    def _fresh(self, is_band=False, is_wide=False):
        self.counter += 1
        if is_band:
            self.band.add(self.counter)
        if is_band or is_wide:
            self.wide.add(self.counter)
        return "#%d" % self.counter

    NODE_VALUE = -1                     # not a SPIR-V opcode

    def _value_opcode(self, val):
        """The opcode that decides how a value is READ: its instruction's,
        except for a load whose value is a node the lowering made -- a
        matrix part's MOV (notes/104 §7) -- which is read as that node,
        not as a name or a component select (`NODE_VALUE`)."""
        if val in self.node_loads:
            return self.NODE_VALUE
        _vi = self.module.result_insn.get(val)
        return _vi.opcode if _vi is not None else None

    def _retarget_node(self, val, dest, ldc=False):
        """A LOAD IS SUBSTITUTED INTO THE STATEMENT THAT STORES IT (notes/67
        §1), so a load whose value is a node's -- a matrix part's MOV -- is
        the store's source node, and the node writes the destination: the
        fold dump of `ld_mx.vert` has the merge take the MOV as lane x
        (`MOV.F R3.x, R0.y;`), and `mx_g`'s stores (0x3a) take it as their
        source (`MOV.F result.attrib[0], R1;`, `MOV.F R0, R0;`).  Where a
        computed value is stored instead, it is a statement temp of its own
        and the lane reads that temp by name (`lx_a.frag`: `MUL.F32 R0.x,
        ..; .. MOV.F R1.x, R0;`, a 0x3a to the temp and a 0x2b read).

        A STATIC BLOCK LOAD stored whole is the same: `mx_h.vert`'s fold dump
        has the store (0x3a) on the LDC itself, and `o = m1` prints
        `LDC.F32X4 result.attrib[0], buf0[16];`, `u_xlat0 = m2` `LDC.F32X4
        R0, buf0[32];`.  The load keeps its own write mask: `dest`'s mask
        is replaced by the one the LDC line carries.  The callers that were
        measured ask for it (`ldc=True`): the whole stores (`mx_h`) and lane
        x (`lx_b.frag`: the merge on the LDC, `LDC.F32 R1.x, buf0[16];`).

        The index of the line that now writes `dest`, or None: only the
        store may read the load, and no block may have opened since the
        node was made."""
        if val in self.node_line:
            _k, _tok = self.node_line[val]
            _keep_mask = False
        elif (ldc and val in self.ldc_at
              and not ENV.get("G2S_NOLDCRETARGET")
              and self.values.get(val) == self.ldc_at[val][1]):
            _k, _tok = self.ldc_at[val]
            _keep_mask = True
        else:
            return None
        if (ENV.get("G2S_NONODERETARGET")
                or _use_count(self.module, val) != 1
                or (self.cuts and self.cuts[-1] > _k)):
            return None
        _l = self.lines[_k]
        _got = _token_dest(_l, _tok)
        if _got is None:
            return None
        _d0, _c = _got
        _mask_txt = _l[_d0 + len(_tok):_c]
        if _keep_mask:
            _sw = _lex.swizzle_suffix(dest)
            dest = (dest[:len(dest) - len(_sw) - 1] if _sw is not None
                    else dest) + _mask_txt
        elif _mask_txt:
            return None                 # the MOV's token carries its mask
        self.lines[_k] = _l[:_d0] + dest + _l[_c:]
        return _k

    def _bkey(self):
        """The current block: the marker block, the cuts, the second-store
        splits."""
        return (self.blk_no, len(self.cuts), self.vsplit)

    def _blk_start(self):
        """The first line of the CURRENT block: after the last cut or
        control-flow line."""
        lines = self.lines
        _st = self.cuts[-1] if self.cuts else 0
        for _k in range(len(lines) - 1, -1, -1):
            if _k < _st:
                break
            if _is_control_line(lines[_k]) or ".CC " in lines[_k]:
                _st = _k + 1
                break
        return _st

    def _first_reader(self, reg):
        """The first line of the CURRENT block that reads `reg` (a name), or
        None."""
        for _k in range(self._blk_start(), len(self.lines)):
            # `,.*REG(?!\d)`: the name read after the first comma
            _l = self.lines[_k]
            _c = _l.find(",")
            if _c >= 0 and _lex.find_name(_l, reg, _c + 1) is not None:
                return _k
        return None

    def _computation(self):
        """A computation after a store -- LIFTED (notes/82).

        The ORDER this used to refuse about is the scheduler's, and
        `py/sched.py` computes it.  The refusal kept out the constructs that
        were not read yet (notes/55 §8): `chr_eye_13b81913.vert` first
        DIFFERED from its first body line.  With notes/72..82 read, lifting it
        gives 0 DIFFERS on the 232 probes and the 120-shader corpus sample,
        and 52 corpus files exact instead of 2.  So it is off.
        `G2S_STORESTRICT=1` restores the refusal, for comparison.
        """
        if self.stores and ENV.get("G2S_STORESTRICT"):
            raise NotEstablished(
                "a computation after a store: the compiler hoists it above "
                "the store (notes/31), and reproducing that needs the "
                "scheduler")

    def _flush(self, only=None):
        """The temps' pending stores, now.

        The temp's store copies the VALUE's node into the temp's NAME: the
        instruction wrote a lowering vreg that its in-block readers share
        (the value has two uses, so it cannot fold into the name -- `cf_loop`:
        ADD vreg 11, `s` vreg 2, the flush vreg 5; `sc_select.frag`: TRUNC
        vreg 9, `b0` vreg 3, the flush vreg 2).  The name keeps its number,
        the lowering vreg is new.

        `only` flushes the one entry whose NAME is given, now: a reader that
        takes the NAME rather than the forwarded value (a lane store,
        notes/70) must come after the name's definition in the lines."""
        lines = self.lines
        todo = [e for e in self.flush_q if only is None or e[0] == only]
        for _ent in todo:
            self.flush_q.remove(_ent)
        for _ent in todo:
            _nm, _fmv, _fds, _fg, _from = _ent[:5]
            _src = _ent[5] if len(_ent) > 5 else _nm
            if _from is not None:
                _lw = self._fresh()
                for _k in range(_from, len(lines)):
                    lines[_k] = _lex.sub_name(lines[_k], _nm,
                                              lambda _i, _e: _lw)
                _src = _lw
                self.flushed[_nm] = _lw
                if (_is_placeholder(_nm) and not ENV.get("G2S_NOFLUSHBAND")):
                    # the NAME is a statement temp's, a band record whatever
                    # made the value: `sn_b.frag`'s `sin(u_xlat2.xyz)` is a
                    # construct (not a band temp), and its flushed name is
                    # vr 7, live out of the block with the SINs' (the seed
                    # `2 3 4 5 6 7 8`, `g2s_trace_liveset`)
                    self.band.add(int(_nm[1:]))
            if _fg is not None:
                _fg.append(len(lines))
            # the temp's store (op 0x3a): its MOV is made after
            # f_7100069f90 runs (stores.py `_mark_store_movs`)
            self.store_movs.append(len(lines))
            lines.append(_emit(_fmv, _nm + _fds, _src))

    def _second_store(self, var):
        """A name stored again in the block it was stored in opens a block
        (notes/55 §8, notes/65 §4): the reads after it forward from the new
        block's stores only.  The block opens at the STORE: its right side
        was evaluated before the walker's test and stays behind
        (`lv_v2b.frag`: the DP2 in the block before `u.x = dot(u, u)`, the
        RSQ and DIV of `u.x = sqrt(u.x)` in the block of the store before
        it), and so do the temps' stores the old block flushes."""
        split = self.stored_key.get(var) == self._bkey()
        if (not split and isinstance(var, tuple) and var[0] == "out"
                and var in self.stored_key
                and len(self.lines) > self._blk_start()
                and not ENV.get("G2S_NOOUTPEND")):
            # AN OUTPUT IS NEVER READ, so its store stays PENDING from any
            # earlier block (notes/72 §8), and a later store to it opens a
            # block at the store whenever the current block holds a node.
            # `mb_n30.vert`'s `vs_TEXCOORD0.w = dot(u_xlat1, u_xlat0)`, after
            # `vs_TEXCOORD0.xyz` blocks earlier: the DP4 stays in the block
            # before, the store opens the next (the compiler's blocks 18 and
            # 19).  The scheduler's own test would do this too, but it runs
            # per control-flow segment and puts the DP4 after the boundary.
            split = True
        if split:
            self._open_block()
        self.stored_key[var] = self._bkey()
        return split

    def _open_block(self):
        """Close the current block with its temps' stores and open one."""
        self._flush()
        self.cuts.append(len(self.lines))
        self.vsplit += 1

    def _local_register(self, var):
        if var not in self.local_reg:
            self.local_reg[var] = self._fresh(True)
        return self.local_reg[var]

    # -- reads -------------------------------------------------------------

    def _name_read(self, vid):
        """(a local's NAME, the component read) when `vid` is a whole load
        of a materialised local or one component of such a load; else
        None.  What a statement in a NEW block reads (notes/81)."""
        if vid in self.load_of:
            return self.load_of[vid][1], 0
        if vid in self.lname:           # a load through a component chain
            return self.lname[vid][0], self.lname[vid][1]
        _i = self.module.result_insn.get(vid)
        if (_i is None or _i.opcode != Op.OpCompositeExtract
                or len(_i.args()) != 2):
            return None
        _base, _k = _i.args()[0], int(_i.args()[1])
        if _base in self.load_of:
            return self.load_of[_base][1], _k
        # ONE COMPONENT OF A SHUFFLE OF THE LOAD is the same component read,
        # through the shuffle's selector (`cr_b.frag`: `c.xyz = u_xlat0.xyz`
        # -- a whole load, a shuffle, three extracts -- reads `R5.y`, `R5.z`
        # in the blocks its second and third stores open)
        _s = self.module.result_insn.get(_base)
        if (_s is not None and _s.opcode == Op.OpVectorShuffle
                and _s.args()[0] == _s.args()[1]
                and _s.args()[0] in self.load_of
                and _k < len(_s.args()) - 2
                and _s.args()[2 + _k] <= 3
                and not ENV.get("G2S_NOSHUFNAME")):
            return self.load_of[_s.args()[0]][1], int(_s.args()[2 + _k])
        return None

    def _con_read(self, vid):
        """A read of ONE component of a construct made in this block, all
        lanes: the DAG takes that component's write, a plain copy, and reads
        its source instead (`ce_head3.vert`: `u_xlat2 * u_xlat3.yyyy` prints
        `MUL.F32 R9, R18, R5.x;` -- R5.x being what `MOV.F R8.y, R5.x;`
        wrote -- the same forwarding component 0's `head` gets).  None when
        that is not the shape.

        Component 0's write copies its source whole (`head`), the others from
        a `.x` lane; a read of either takes that source (`mc_n3.vert`'s
        `-u_xlat1.x` prints `MOV.F R11.x, -R4;`, R4 being what
        `MOV.F R14.x, R4;` wrote)."""
        v = self.values.get(vid)
        c = self.comps.get(vid)
        if not c or len(set(c)) != 1:
            return None
        return self._con_lane(v, c[0])

    def _con_lane(self, v, c):
        """The source construct `v`'s component `c` was written from, when
        `v` is a construct made in this block and that source is a temp's
        `.x`; else None (`_con_read`)."""
        if v not in self.con_src:
            return None
        srcs, key = self.con_src[v]
        if key != self._bkey() or c not in srcs:
            return None
        t = srcs[c]
        return t if _lex.is_numbered(
            t[:-2] if t.endswith(".x") else t, "#") else None

    def _swizzled_local_read(self, val):
        """A SWIZZLE OF A LOCAL'S READ (notes/87): the reader makes it a
        swizzle over the variable node, and a variable read inside a swizzle
        is not forwarded (the splat's rule, notes/72) -- so the store reads
        the NAME, the value has one use and folds, and there is no
        self-move: the cut `mq_n2.frag`'s `SV_Target0 = u_xlat0.xyxy` prints
        `MOV.F result_color0, R3.xyxy;` alone."""
        _d = self.module.result_insn.get(val)
        if _d is None or _d.opcode != Op.OpVectorShuffle:
            return False
        return all(_s in self.load_of or _s in self.lname
                   for _s in _d.args()[:2])

    def _stale_ldc(self, val):
        """Is the value a static block load's register from an EARLIER
        block?"""
        if val in self.node_loads:
            # a matrix part: its load and MOV are the block's (notes/104 §7)
            return self.node_bkey.get(val) != self._bkey()
        _v = self.values.get(val)
        _r = self.ldc_vreg.get(_v) if isinstance(_v, str) else None
        return _r is not None and _r[0] != self._bkey()

    def _reload(self, val):
        """A LOAD IS SUBSTITUTED INTO EVERY STATEMENT THAT READS IT (notes/67
        §1, notes/76), and the DAG is per block: a statement in a later
        block gets a leaf, and an LDC, of its own.  `pl_e.vert`
        (`gl_Position = v`, v a uniform vec4) prints an LDC in each element's
        block: `LDC.F32 R0.x`, `LDC.F32X2 R0.y`, `LDC.F32X4 R0.z`,
        `LDC.F32X4 R0.w`.  The masks are `_narrow_loads`'s."""
        if not self._stale_ldc(val):
            return
        if val in self.node_loads:
            if not ENV.get("G2S_NONODEAGAIN"):
                self._node_again(val)
            return
        _old = self.values[val]
        _b, _mn, _ls, _nm, _of = self.ldc_vreg[_old]
        _new = self._fresh()
        self.ldc_vreg[_new] = (self._bkey(), _mn, _ls, _nm, _of)
        _n = len(self.lines)
        self.ldc_lines.add(_n)
        self.lines.append("%s %s%s, %s[%d];" % (_mn, _new, _ls, _nm, _of))
        for _k, _vv in list(self.values.items()):
            if _vv == _old:
                self.values[_k] = _new
        if not ENV.get("G2S_NORELOADKEY"):
            # the reload IS the location's node in this block: a later read
            # of it here takes the same node (notes/75), another component
            # too -- `ld_ar.vert`'s `u_xlat1.x = -a.w + m[0].w` after
            # `u_xlat0.y = m[0].y` opened the block prints ONE `LDC.F32X4
            # R0.yw, buf0[16];` (`map_2948729f`: node 2.33, made at the
            # store's seq 13, read again at seq 59)
            for _key, _hit in list(self.ldc_same.items()):
                if _hit[1] == _old:
                    self.ldc_same[_key] = (self._bkey(), _new, _hit[2], _n)
            # ... and the node a one-use reader folds into (`ldc_at`,
            # `_output_lane_x`, `_component_operand`): `lf_c.frag`'s
            # `u.w = s` opens its block, reloads `s` there, and the lane's
            # scratch MOV folds into the reload (notes/104 §4)
            for _k, _at in list(self.ldc_at.items()):
                if _at[1] == _old:
                    self.ldc_at[_k] = (_n, _new)

    # -- locals --------------------------------------------------------------

    def _stored_mask(self, var):
        """The write mask of a whole copy of a local: the components it EVER
        stores (`lstored`, notes/72 §5), not its type's.  The cut
        `mq_n12.frag`'s `SV_Target0 = u_xlat1`, a vec4 stored only at `.x`
        and `.y`, prints `MOV.F result_color0.xy, R0;` and the local's own
        store `MOV.F R15.xy, R0;` (notes/87)."""
        _n = _pointee_components(self.module, var)
        if not _n:
            return None
        _st = sorted(c for c in self.lstored.get(var, set(range(_n)))
                     if c < _n)
        if not _st:
            return None
        if _st == list(range(_n)):
            return _opchain.dest_suffix(_n)
        return "." + "".join(_COMPONENTS[c] for c in _st)

    def _value_reads_local(self, val, var):
        """Whether the statement computing `val` loads the local `var` (the
        whole local or a component of it).  The walk follows `val`'s operand
        ids and stops at each load: a statement's SSA values never cross a
        statement boundary except through a local's load and store."""
        module = self.module
        seen = set()
        todo = [val]
        while todo:
            v = todo.pop()
            if v in seen:
                continue
            seen.add(v)
            i = module.result_insn.get(v)
            if i is None:
                continue
            if i.opcode == Op.OpLoad:
                p = i.args()[0]
                pc = self.by_result.get(p)
                if p == var or (pc is not None
                                and pc.opcode in ACCESS_CHAINS
                                and pc.args()[0] == var):
                    return True
                continue
            for k in i.ref_positions():
                a = i.operands[k]
                if a != v and a in module.result_insn:
                    todo.append(a)
        return False

    def _name_read_after_pair(self, var, lanes, vid=None):
        """A READ OF THE NAME at lanes the block's last merge pair did not
        write keeps the name's OLD value.  Its reader must then go ahead of
        the pair's in-place write -- and when that reader also depends on a
        read of the MERGE made since the pair, it cannot: the pair's merge
        goes to a vreg of its own, every whole read since takes it, and the
        local's own store copies it back at the block's close
        (`_merge_forward`'s shape).  `cy_d.frag`'s `u2 = a * 2.0; u2.x = ..;
        u3 = -c + u2; o = u3 * u2.wwww;` prints `MOV.F R5.yzw, R0; MOV.F
        R5.x, R2;`, the ADD reading R5, the MUL `R0.w`, and `MOV.F R0, R5;`
        last (vr 13 against the local's 3).  In place (the reader goes
        first): `cy_e` (whole reads only), `cy_f` (no merge read), `cy_h`
        (the lane read before the whole read), `nr_a` (`u_xlat3.yz`'s DP2
        does not read the merge, the `.xy` DP2 does), `map_0c88a1ef` (the
        whole read `.xyz` reads only written lanes), `chr_eye_d37183c4`
        (lane gathers).  The three corpus files the scheduler refused
        (`map_01d7bd89`, `map_035dc146`, `map_0c88a1ef`) are this.
        `G2S_NONAMEPAIR=1` turns it off."""
        if (ENV.get("G2S_NONAMEPAIR") or var not in self.local_reg
                or vid is None):
            return
        _cp = self.cpair.get(var)
        if _cp is None or _cp[0] != self._bkey():
            return
        _wp = _sched.parse(self.lines[_cp[1]])
        if _wp is None:
            return
        # the lanes this block's component stores wrote (the pairs', not a
        # whole store's)
        _bk = self._bkey()
        _wm = _wp[1][1]
        for _c, _f in self.cfw.get(var, {}).items():
            if _f[0] == _bk and self.cfw_kind.get((var, _c)) is not None:
                _wm |= 1 << _c
        if any((_wm >> c) & 1 for c in lanes):
            return
        _reg = self.local_reg[var]
        _after = max(_cp[1], _cp[2])
        # the lines since the pair that read the merge (a written lane)
        _defs = {}
        _mreads = set()
        for _k in range(_after + 1, len(self.lines)):
            _p = _sched.parse(self.lines[_k])
            if _p is None:
                continue
            _defs.setdefault(_p[1][0], []).append(_k)
            if any(_nm == _reg and _sm & _wm for _nm, _sm in _p[2]):
                _mreads.add(_k)
        if not _mreads:
            return

        def _depends(ph, seen):
            # does placeholder `ph` come from a merge read, in the lines?
            for _k in _defs.get(ph, ()):
                if _k in _mreads:
                    return True
                if _k in seen:
                    continue
                seen.add(_k)
                for _nm, _sm in _sched.parse(self.lines[_k])[2]:
                    if _nm.startswith("#") and _nm != _reg \
                            and _depends(_nm, seen):
                        return True
            return False
        # the lane read's READERS, and what else they read
        for _u in self._readers_of(vid):
            for _a in _u.args():
                if _a == vid or not isinstance(_a, int):
                    continue
                _ph = self._pending_value(_a)
                if _ph is not None and _depends(_ph, set()):
                    self._merge_forward(var)
                    return

    def _readers_of(self, vid):
        """The body's instructions that take `vid` as an operand."""
        if not hasattr(self, "_reader_map"):
            self._reader_map = {}
            for _i in self.insns:
                if not hasattr(_i, "args"):
                    continue
                try:
                    _args = _i.args()
                except Exception:
                    continue
                for _a in set(_args):
                    if isinstance(_a, int):
                        self._reader_map.setdefault(_a, []).append(_i)
        return self._reader_map.get(vid, [])

    def _pending_value(self, vid):
        """The placeholder a value has or WILL have: a load of a local not
        yet lowered takes the value the local forwards in this block."""
        _v = self.values.get(vid)
        if _v is not None:
            return _v.split(".")[0]
        _i = self.module.result_insn.get(vid)
        if _i is not None and _i.opcode == Op.OpLoad:
            _fw = self.lfwd.get(_i.args()[0])
            if _fw is not None and _fw[0] == self._bkey():
                return str(_fw[1]).split(".")[0]
        return None

    def _merge_forward(self, var):
        """THE MERGE IS THE STORED VALUE (notes/85).

        A local whose component was stored in this block, read WHOLE by a
        store in the same block: the reader takes the merge node itself,
        which then has two uses -- the local's store and the reader's -- and
        cannot fold into the name.  The pair writes a lowering vreg, and the
        local's own store copies it (queued, flushed with the block's temps).
        `tools/nodedump.py probes/pb_c.frag.spv`: `MOV.F R0.yzw, R1; MOV.F
        R0.x, R0;` vr 9, `MOV.F result_color0, R0;`, `MOV.F R1, R0;` vr 2
        (the local), all seq 18.  `o = u * 2.0` (`pb_d`) reads the NAME and
        the pair writes the local itself.  The cut `mq_n8.frag`'s `u_xlat0 =
        hlslcc_movcTemp` after `hlslcc_movcTemp.y = ..` is the same, with a
        local as the reader: `MOV.F R14.xy, R0;` .. `MOV.F R0.xy, R0;`.

        Returns the merge vreg; the caller appends its reader to
        `merge_grp[vreg]`."""
        _cp = self.cpair.pop(var)
        _reg = self.local_reg[var]
        _lds = self._stored_mask(var)
        if _lds is None:
            raise NotEstablished("a merged local whose store mask the "
                                 "image does not give")
        _X = self._fresh()
        for _k in (_cp[1], _cp[2]):
            # the destination only: the pass-through half still reads the
            # name
            self.lines[_k] = _lex.sub_name(self.lines[_k], _reg,
                                           lambda _i, _e: _X, count=1)
        if not ENV.get("G2S_NOMERGEEARLY"):
            # A WHOLE READ OF THE LOCAL made since the pair, in its block,
            # reads the merge too: `mf_c.vert`'s `p.x = dot(b, u_xlat0)`,
            # before `o = u_xlat0`, reads the same source as the output store
            # and the local's own store (`tools/gsum.py`: nodes 1.2, 1.3,
            # 1.6), with kind-0 edges from both halves
            # A SWIZZLED READ THAT TAKES A LANE THE PAIR WROTE reads the
            # merge as well: the slice's `map_5bc1f41b` stores `u_xlat1.yzw`
            # one lane at a time (the last, `.w`, is the pair) and then
            # `-u_xlat3 + u_xlat1.yzwx`, and the compiler prints `ADD.F32
            # R5, R2, R4.yzwx;` -- the merge's register, R4 -- with the
            # local's `MOV.F R11, R4;` after it.  A swizzle of only lanes
            # the pair passed through keeps the name (the pair's lanes are
            # the ones whose value changed).  `G2S_NOMERGESWZ=1` turns it
            # off.
            _wpp = _sched.parse(self.lines[_cp[1]])
            _wmask = _wpp[1][1] if _wpp is not None else 0
            for _k in range(max(_cp[1], _cp[2]) + 1, len(self.lines)):
                _head, _sep, _srcs = self.lines[_k].partition(",")
                if _sep:
                    # `(?<=[ ,])REG(?![\d.])` -> the merge
                    _srcs = _lex.sub_name(_srcs, _reg, lambda _i, _e: _X,
                                          prev=" ,", tail=_whole_read_tail)
                    if not ENV.get("G2S_NOMERGESWZ") and _wmask:
                        _pl = _sched.parse(self.lines[_k])

                        def _sub(i, e, _pl=_pl, _t=_srcs):
                            _o = _t[i:e]
                            if _pl is None:
                                return _o
                            _b = _pl[0].split(".")[0]
                            _mk = _sched._lane_mask(
                                _b, _pl[1][1], _o, _sched._split(_o)[1])
                            if _b in _sched._DOT_WIDTH:
                                _mk = _sched._dot_mask(
                                    _sched._DOT_WIDTH[_b], _o)
                            return (_X + _o[len(_reg):] if _mk & _wmask
                                    else _o)
                        # `(?<=[ ,\-|])REG(\.[xyzw]{1,4})(?![\w\[])`
                        _srcs = _lex.sub_name(_srcs, _reg, _sub,
                                              prev=" ,-|",
                                              tail=_swizzle_read_tail)
                    self.lines[_k] = _head + _sep + _srcs
        if _cp[2] in self.passthru:
            self.passthru.remove(_cp[2])
        if (self.cpair_tied.get(var, True)
                or ENV.get("G2S_MERGEONESEQ")):
            _grp = [_cp[1], _cp[2]]
        else:
            # THE HALVES HAVE TWO SEQS (notes/91), and the merge's readers
            # take the PASS-THROUGH's: `mf_a.vert` (the pair opened its
            # block, the value does not read the local) -- write seq 7,
            # pass-through 9, and the output store and the local's own store
            # 9 too (`tools/gsum.py`)
            _grp = _sched.Tie([_cp[2]])
            _grp.seq = _cp[2]
        self.ties.append(_grp)
        self.merge_grp[_X] = _grp
        self.merged.add(_X)
        _wp = _sched.parse(self.lines[_cp[1]])
        self.merge_lanes[_X] = (_reg, _wp[1][1] if _wp is not None else 0xF)
        # the local is a NAME this block stores, at the merge's statement:
        # pass 1 walks its entry ahead of the reader's (the compiler's list:
        # pair, reader, the local's store)
        self.stmtpos[(_reg, _cp[1])] = _cp[1]
        self.flush_q.append((_reg, _cp[3], _lds, _grp, None, _X))
        if not ENV.get("G2S_NOMERGELFWD"):
            # a LATER read of the local in the block takes the merge too:
            # `mf_a.vert`'s `dot(b, u_xlat0)` after `o = u_xlat0` reads the
            # same value as the output store and the local's store
            # (`tools/gsum.py`: all three read one source)
            self.lfwd[var] = (self._bkey(), _X)
        return _X

    # -- shared shapes -------------------------------------------------------

    def _assemble(self, flat, _mov, force=(), is_band=False,
                  gather_all=False):
        """The composite-construct shape: ONE vreg, one write per component.

        `flat` is one `(base, component)` per component, or `(None, text)`
        for a constant.  Read from `co_mix4.vert` / `co_mul4.vert` (notes/53
        sec.6); returned as `(vreg, head)` where `head` is component 0's
        operand, which the consumer reads INSTEAD of the vreg.

        `force` are the components gathered through a `.x` scratch even when
        their source component is 0: a component of a local's NAME
        (`lv_cc.vert`: `t.x` into `.y` is `MOV.F R1.x, R2; MOV.F R1.y,
        R1.x;`), as a vector attribute's `.y` is (`co_mix4`), where a scalar
        value goes straight in (`co_mul4`'s MULs).

        `is_band`: the vreg is an OpCompositeConstruct's VALUE, which is one
        of the band temps every SPIR-V value instruction contributes
        (notes/52) -- it matters once the value is stored and its temp's
        store is flushed: the flushed name then lives out of the block like
        every other one (`ce_head.vert`).

        `gather_all`: EVERY component past the first goes through a `.x`
        scratch, a constant too -- the coordinate a shadow sample rebuilds
        (`sa_b.frag`: `MOV.F R2.x, {2, 0, 0, 0};` .. `MOV.F R5.z, R2.x;`).

        The entries are kept (`con_flat`), so a later reader that rebuilds the
        vector from its components can take the construct's own sources.
        """
        lines = self.lines
        _run = self._leading_run(flat, force, gather_all)
        _dst = self._load_is_lane_x(flat, force, gather_all, _run)
        _lane0_is_load = _dst is not None
        if _dst is None:
            _dst = self._fresh(is_band=is_band, is_wide=True)
        else:
            # the construct is the band temp, and the load shares it: in
            # `pu_f.vert` the construct is vreg 2, ahead of the MUL's 3, and
            # the load prints in its register (`LDC.F32 R0.x`)
            _n = int(_dst[1:])
            if is_band:
                self.band.add(_n)
            self.wide.add(_n)
        _s0 = len(lines)
        _tie = []
        _gathers = []
        _head = None
        _csrc = {}
        if _lane0_is_load:
            # the load IS lane x: no write of it (`_load_is_lane_x`)
            _head = _dst
            _csrc[0] = _dst
        if _run >= 2:
            # A LEADING RUN OF ONE PLAIN NODE (notes/104 §8): the fold dumps
            # of `pu_d`, `pu_g` and `pu_j` start the constructor's merge
            # chain with a MOV of the node into `.x` and one MOV into the
            # run's lanes from it (`MOV.F R0.x, R0; MOV.F R0.xy, R0.x;`) --
            # the second is the operand legaliser's (`f_71000311e0`)
            _b0, _sc0 = flat[0]
            _t = self._fresh(is_wide=True)
            _tie.append(len(lines))
            lines.append(_emit(_mov, "%s.x" % _t, _swizzle(_b0, _sc0)))
            _tie.append(len(lines))
            lines.append(_emit(_mov, "%s.%s" % (_dst,
                                                "".join(_COMPONENTS[:_run])),
                               "%s.x" % _t))
            _head = "%s.x" % _t
            for _c in range(_run):
                _csrc[_c] = _head
        for _c, _ent in enumerate(flat):
            if (_ent is None or _c < _run   # not written, or in the run
                    or (_c == 0 and _lane0_is_load)):
                continue
            _b, _sc = _ent
            if _b is None and gather_all and _c != 0:
                _o = self._fresh(is_wide=True)
                _gathers.append(len(lines))
                lines.append(_emit(_mov, "%s.x" % _o, _sc))
                _text = "%s.x" % _o
            elif _b is None:
                # A CONSTANT INTO LANE X PRINTS BARE (notes/90 §5), in a
                # construct too: `wk_c.frag`'s `a / vec4(2.0, b.z, a.w,
                # b.y)` prints `MOV.F R0.x, {2, 0, 0, 0};`.  No lane-x MOV
                # of a constant in the probes, the corpus or the slice
                # listings (22,295 lines) has the `.x`.
                _text = _sc if _c == 0 else "%s.x" % _sc
            elif _c == 0 and not ENV.get("G2S_LANE0GATHER"):
                # LANE X IS WRITTEN STRAIGHT from its source, a component
                # select too: `pu_h.vert`'s fold dump has the swizzle's MOV
                # (`v.y`) as the merge's lane x -- `MOV.F R0.x, R1.y;`, no
                # gather first (notes/104 §8)
                _text = _swizzle(_b, _sc)
            else:
                _o = _b
                if _sc != 0 or (_c != 0 and (_c in force or gather_all)):
                    _o = self._fresh(is_wide=True)
                    _gathers.append(len(lines))
                    lines.append(_emit(_mov, "%s.x" % _o, _swizzle(_b, _sc)))
                _text = _o if _c == 0 else "%s.x" % _o
            _tie.append(len(lines))
            lines.append(_emit(_mov, "%s.%s" % (_dst, _COMPONENTS[_c]),
                               _text))
            if _c == 0:
                _head = _text
            _csrc[_c] = _text
        # A PLAIN NODE IN LANE X is written with the other lanes, one seq:
        # `cz_a.frag`'s `vec4(n1, n2, n3, n4)` of four MULs and `cz_b`'s
        # `vec4(n1, a.y, a.z, a.w)` are all one seq (10; 4), where `cz_c`'s
        # `vec4(a.x, n2, ..)` -- lane x a component select -- is 9 against 8
        # (`tools/nodedump.py`); `sb_c.vert`'s bitcast construct 44 x4
        _node0 = (flat and flat[0] is not None and flat[0][0] is not None
                  and _is_placeholder(flat[0][0]) and flat[0][1] == 0
                  and flat[0][0] not in self.local_reg.values()
                  and 0 not in force
                  and not ENV.get("G2S_NODE0SEQ"))
        if not ENV.get("G2S_CONSTRUCTTIE"):
            _ct = self._construct_seqs(
                _s0, _tie, _gathers, flat,
                run=_run >= 2 or _lane0_is_load or _node0)
            # THE STATEMENT'S OWN GROUP, for the loads its operands lower to
            # (notes/114 SS6, finish.py `_ldc_at_reader`): its lanes carry
            # this `node[36]`, and a load read by one of them is made after
            # it -- whether the statement writes four lanes or one.  The
            # lane-x load retargeted into the construct's register
            # (`_load_is_lane_x`) is one of those loads, not a lane.
            if _ct is not None:
                self.con_stmt.append((_ct, _s0))
                if _lane0_is_load:
                    # `ldc_at` is {value: (its line, its vreg)}; lane x is
                    # named by the VREG here
                    _ll = next((_n for _n, _d in self.ldc_at.values()
                                if _d == flat[0][0]), None)
                    if _ll is not None:
                        self.con_lane_load[_ll] = _ct
        else:
            self.ties.append(_tie)
        self.con_src[_dst] = (_csrc, self._bkey())
        self.con_flat[_dst] = (list(flat), self._bkey())
        return _dst, _head

    def _load_is_lane_x(self, flat, force, gather_all, run):
        """The construct's register when lane x's operand is a static
        scalar BLOCK LOAD of this block, or None.  A load is substituted into
        its reader (notes/67 §1), so the merge takes the load itself as lane
        x -- `pu_f.vert`'s fold dump: the first 0x57 on the 0x3b, and the
        later insert of the same `s` reads it: `LDC.F32 R0.x, buf0[16]; ..
        MOV.F R0.z, R0.x;` -- as a local's lane x does (`lx_b.frag`).  A
        computed value is a temp and gets its lane MOV (`mx_d`'s `MOV.F R2.x,
        R1;`).  `G2S_NOLOADLANE=1` off."""
        if (ENV.get("G2S_NOLOADLANE") or run or gather_all or not flat
                or flat[0] is None or flat[0][0] is None
                or flat[0][1] != 0 or 0 in force):
            return None
        _b0 = flat[0][0]
        _r = self.ldc_vreg.get(_b0)
        if _r is None or _r[0] != self._bkey() or _r[2] not in ("", ".x"):
            return None
        if _b0 in self.ldc_dyn:
            return None
        return _b0

    def _leading_run(self, flat, force, gather_all):
        """How many lanes from x repeat operand 0, when operand 0 is a plain
        node -- not a constant, not a component select (a swizzle operand
        is a MOV of its own in every lane: `pu_i`'s `vec4(v.x, v.x, ..)`,
        `pl_f`'s `vec4(u, u)`) -- or 0.  Only a run FROM LANE X collapses:
        `pu_f`'s `vec4(s, v.y, s, 1)` and `pu_h`'s `vec4(v.y, s, s, 1)`
        insert each `s` on its own (notes/104 §8).  `G2S_NORUN=1` off."""
        if (ENV.get("G2S_NORUN") or gather_all or not flat
                or flat[0] is None or flat[0][0] is None
                or flat[0][1] != 0 or 0 in force):
            return 0
        _k = 1
        while (_k < len(flat) and flat[_k] == flat[0]
               and _k not in force):
            _k += 1
        return _k if _k >= 2 else 0

    def _construct_seqs(self, s0, writes, gathers, flat, run=False):
        """Returns the statement's own group (`_rest`)."""
        """A CONSTRUCT'S `node[36]`s (`tools/nodedump.py` on `sa_a`..`sa_d`
        and `sa_b.frag`'s `txVec0 = vec4(u_xlat0.xy, 2.0, u_xlat0.z)`): the
        writes of components 1.. are made first, together; then the write of
        component 0; then the gathers, in component order -- `sa_a.frag`'s
        rebuilt coordinate seq 1 (w, z, y), 3 (x), 4, 5, 6 (the `.y`, `.z`,
        `.w` gathers); `txVec0` 4, 5, 6, 8.  The lines are still emitted
        source first; the order is the scheduler's.  (Before notes/99 every
        write, component 0's too, was one group at the first write's line and
        a gather was at its own line; `G2S_CONSTRUCTTIE=1` restores that.)

        `run`: component 0 is a LEADING RUN (`_leading_run`), whose two MOVs
        carry the writes' own seq -- `pu_j.vert`'s fold dump: the run's `.x`
        and `.xy` MOVs, the insert and the constant lane all seq 4 -- so
        nothing is singled out as the component-0 write."""
        _w0 = (writes[0] if flat and flat[0] is not None and not run
               else None)
        _rest = _sched.Tie([w for w in writes if w != _w0])
        _rest.seq = s0
        self.ties.append(_rest)
        if _w0 is not None:
            _t0 = _sched.Tie([_w0])
            _t0.seq = s0 + 0.1
            self.ties.append(_t0)
        for _k, _g in enumerate(gathers):
            _tg = _sched.Tie([_g])
            _tg.seq = s0 + 0.2 + 0.01 * _k
            self.ties.append(_tg)
        return _rest

    def _facing_cc(self):
        """`facing > 0` as a branch's condition (notes/88): the int input's
        MOV, the SGT against 0, and the bool move the branch's `.CC` folds
        into (an integer operand's `MOV.S t, -c`, one use, `HC`)."""
        _fmv = _opchain.mnemonic_for_opcode(nodes.MOV, nodes.S32)
        _fgt = _opchain.mnemonic("OpSGreaterThan", nodes.S32)
        if (_fmv is None or _fgt is None or _fmv.startswith("<")
                or _fgt.startswith("<")):
            raise NotEstablished("the facing compare's MOV or SGT the image "
                                 "does not name")
        _f0 = self._fresh()
        self.lines.append(_emit(_fmv, _f0 + ".x", "fragment.facing"))
        _f1 = self._fresh()
        self.lines.append(_emit(_fgt, _f1 + ".x", _f0, _bool_constant(False)))
        self.lines.append("%s.CC HC.x, -%s;" % (_fmv, _f1))

    def _mov_for(self, type_code):
        """The MOV mnemonic for a type code, or None."""
        mv = (_opchain.mnemonic_for_opcode(nodes.MOV, type_code)
              if type_code is not None else None)
        return None if mv is None or mv.startswith("<") else mv

    def _width_suffix(self, tid):
        """(component count, destination suffix) of a result type."""
        n = _components(self.module, tid)
        return n, (_opchain.dest_suffix(n) if n else None)

    def _scratch_for(self, src):
        """The `.x` scratch a store gathers a component through: a fresh
        placeholder -- or, under `G2S_NOBAND=1` (the old allocator), the
        source's own register when it has one."""
        if not ENV.get("G2S_NOBAND"):
            return self._fresh()
        return src if src.startswith("#") else self._fresh()

    # -- the loop ------------------------------------------------------------

    def _prelude(self, ins, op):
        """What every instruction does before its arm: re-load a stale
        block load, refuse the unmeasured cross-block reads, and keep the
        block's node test and the value's block and line."""
        if op != Op.OpStore and self.ldc_vreg:
            for _a in ins.args():
                self._reload(_a)
        if self.ldc_dyn and any(
                self.ldc_dyn.get(self.values.get(_a), self._bkey())
                != self._bkey() for _a in ins.args()
                if isinstance(self.values.get(_a), str)) \
                and ENV.get("G2S_DYNLATERSTRICT"):
            raise NotEstablished(
                "a dynamically indexed block load read in a later block: "
                "its re-lowering there (index and address) is not measured "
                "[%s %%%s]" % (OP_NAME.get(op, op),
                               getattr(ins, "result", "-")))
        if self.con_blk and any(
                self.con_blk.get(_a, self._bkey()) != self._bkey()
                for _a in ins.args()):
            # a same-node construct's value is forwarded as the node's
            # swizzle; a read in a later block takes the temp's NAME, which
            # is not modelled (notes/75)
            raise NotEstablished(
                "a construct of one repeated load read in a later block")
        if self.node_next:
            self.blk_node = True
            self.node_next = False
        _no_node = _NO_NODE if ENV.get("G2S_SWZNODE") else \
            _NO_NODE + _SELECTORS
        if op not in _no_node and (
                op == Op.OpStore or getattr(ins, "result", None) is not None):
            self.node_next = True
        if getattr(ins, "result", None) is not None:
            self.defblk[ins.result] = self.blk_no
            self.defline[ins.result] = len(self.lines)

    def _lower_insn(self, ins):
        op = ins.opcode
        if self.folded_ids and getattr(ins, "result", None) in self.folded_ids:
            return                      # an identity fold: no node (notes/89)
        self._prelude(ins, op)
        if self.node_loads and op != Op.OpStore:
            self._node_after_reader(ins)
        for arm in self.ARMS:
            if getattr(self, arm)(ins):
                return
        raise NotEstablished(
            "opcode %d in the body: only a straight store is lowered" % op)

    def _node_after_reader(self, ins):
        """A MATRIX PART'S MOV IS MADE AFTER THE NODE THAT READS IT: the load
        is lowered as that node's operand (notes/67 §1).  `tools/nodedump.py`
        on `mx_d.vert`: the MUL is seq 1 and the MOV it reads 5, the ADD 7
        and its MOV 10, the construct's writes 11 and `m[1].x`'s MOV 14.  So
        the MOV's `node[36]` follows the reader's first line, the parts in
        operand order.  `G2S_NONODESEQ=1` leaves the MOV at its own line."""
        if ENV.get("G2S_NONODESEQ"):
            return
        _k = 0
        for _a in ins.args():
            if (_a not in self.node_loads
                    or self.node_bkey.get(_a) != self._bkey()):
                continue
            _t = _sched.Tie([self.node_line[_a][0]])
            _t.seq = len(self.lines) + 0.15 + 0.001 * _k
            self.ties.append(_t)
            _k += 1

    def run(self):
        for ins in self.insns:
            self._lower_insn(ins)
        return self._finish()
