"""stores.py -- OpStore: into locals, and into every kind of output.

A store is an assignment statement.  Into a FORWARDED local it emits nothing
(the value is remembered); into a MATERIALISED local it is a MOV into the
local's register (a component store a PAIR, notes/55 §7); into an output it is
a MOV into the output's name, with the temp's own store (the self-move) when
the value is a register.  Where each lands among the blocks is the statement
walker's rule (notes/65 §4, `Core._second_store`).
"""
import lex as _lex

from spvnames import Op, ExecutionModel, StorageClass

import sched as _sched
import opchain as _opchain
from glasmlib.common import NotEstablished, ENV, OP_NAME, ACCESS_CHAINS
from glasmlib.types import BOOL_TYPE_CODE, _components, _glasm_type_code, \
    _pointee_components, _pointee_code, _vi_is_scalar
from glasmlib.operands import _constant_operand
from glasmlib.chains import _output_chain, _output_operand, \
    _colour_output_name, _patch_level_chain, _position_chain, _buffer_chain
from glasmlib.text import _COMPONENTS, _emit, _swizzle, _swizzle_suffix, \
    _source, _position_store
from glasmlib.usage import _use_count
from glasmlib.boolean import _BOOL_REPR_CODE
from glasmlib.lower.core import _local_chain, _is_placeholder
from glasmlib import nodes

_IDENTITY = (0, 1, 2, 3)
_POSITION = ("out", "result.position")

# The values that name (a component of) something else rather than being a
# scalar their own instruction wrote.
_COMPONENT_NAMING = (Op.OpVectorShuffle, Op.OpCompositeExtract,
                     Op.OpCopyObject)
_NAMING = (Op.OpLoad,) + _COMPONENT_NAMING


def _lane_x_move(l):
    r"""`(\S+\s+)(#\d+)\.x, (\S+);$` matched: (the mnemonic with its
    blanks, the placeholder, the source), or None."""
    f = _lex.split_first(l)
    if f is None or f[0] != 0 or f[2] == f[1] or not l.startswith("#", f[2]):
        return None
    e = _lex.digit_end(l, f[2] + 1)
    if e == f[2] + 1 or not l.startswith(".x, ", e) or not l.endswith(";"):
        return None
    src = l[e + 4:-1]
    if not src or any(c in _lex.WS for c in src):
        return None
    return l[:f[2]], l[f[2]:e], src


def _is_selected_placeholder(v):
    r"""`^#\d+\.[xyzw]{1,4}$`"""
    head, dot, swz = v.partition(".")
    return bool(dot) and _lex.is_numbered(head, "#") and _lex.is_swizzle(swz)


def _modified(text):
    return text.startswith("-") or text.startswith("|")


_IMAGE_RESULT_OPS = frozenset((
    Op.OpImageSampleImplicitLod, Op.OpImageSampleExplicitLod,
    Op.OpImageFetch, Op.OpImageSampleDrefImplicitLod,
    Op.OpImageSampleProjImplicitLod, Op.OpImageSampleProjExplicitLod,
    Op.OpImageSampleDrefExplicitLod, Op.OpImageRead, Op.OpImageGather))


def _selects_an_image(module, ins):
    """Does this shuffle select from an IMAGE OP's result?

    An image op is not a node of the emit list (notes/111, and
    `tools/nodedump.py` prints `<no node record>` against every `TEX`/`TXL`
    line), so a selection of one has no node to be a selector ON: the store
    has to materialise it.  A selection of anything else does have one.
    """
    src = module.result_insn.get(ins.args()[0]) if ins.args() else None
    return src is not None and src.opcode in _IMAGE_RESULT_OPS


class StoreOps(object):

    def _arm_store(self, ins):
        if ins.opcode != Op.OpStore:
            return False
        ptr, val = ins.args()[0], ins.args()[1]
        if self._is_self_copy(ptr, val):
            return True
        self.store_no += 1
        self.stores += 1
        self._end_forwards(ptr)
        const = _constant_operand(self.module, val)
        if ptr in self.lmem_k:
            self._lmem_store(ptr, val)
            return True
        if ptr in self.reg_arrays:
            self._lmem_store(ptr, val, in_registers=True)
            return True
        lc = _local_chain(self.module, ptr, self.by_result, self.locals_)
        if (lc is not None or ptr in self.locals_) and self._stale_ldc(val):
            raise NotEstablished(
                "a block load stored to a local in a later block: where its "
                "re-load falls against the store's block split is not "
                "measured")
        if lc is not None:
            self._store_local_component(val, const, lc)
            return True
        _n0 = len(self.lines)
        _bch = (None if ptr in self.locals_ or ENV.get("G2S_NOSTB")
                else _buffer_chain(self.module, ptr, self.by_result))
        if _bch is not None and _bch[0].startswith("LDB"):
            self._store_buffer(val, const, _bch)
            return True
        if ptr in self.locals_:
            self._store_local(ptr, val, const)
            _dst = self.local_reg.get(ptr)
        else:
            self._store_output(ptr, val, const)
            _dst = None
        self._mark_store_movs(_n0, _dst)
        return True

    def _store_buffer(self, val, const, chain):
        """A STORE INTO A STORAGE BUFFER is one `STB` with the load's
        mnemonic and address (`st_a`..`st_e.comp`): `STB.U32 {5, 0, 0, 0},
        sbo_buf0[4];`, `STB.F32X4 R0, sbo_buf1[16];`, a dynamic index scaled
        and carried as a load's (`st_c`: `MUL.S`, `MOV.S`, `sbo_buf0[R0.x]`).
        The value prints bare, the register itself; a component other than
        x is gathered into a scratch `.x` first (`st_e`'s `cb.f = p.y`:
        `MOV.F R0.x, R0.y; STB.F32 R0, ..`).  No self-move follows."""
        mnem, name, off, _tid, _dyn, _bcomp = chain
        if _bcomp is not None:
            raise NotEstablished("a store into one component of a storage "
                                 "buffer vector: not measured")
        self._computation()
        smn = "STB" + mnem[3:]
        _tc = _glasm_type_code(self.module, _tid)
        _mv = self._mov_for(_tc)
        _n = _components(self.module, _tid)
        if const is not None:
            src = const
        else:
            src = self.values.get(val)
            if src is None or _modified(src) or _mv is None:
                raise NotEstablished("a storage-buffer store whose value has "
                                     "no form")
            _cs = self.comps.get(val)
            _vi = self.module.result_insn.get(val)
            _crd = self._con_read(val)
            _sel = (not ENV.get("G2S_NOSTBSELECT")
                    and self._selects_vector_component(val))
            if _n == 1 and (_crd is not None or _sel or (
                    _cs is not None and (_cs[0] != 0 or (
                        _vi is not None
                        and _vi.opcode == Op.OpCompositeExtract)))):
                # ... and ANY COMPONENT SELECT, lane x too (notes/111): a
                # load through a chain that picks a vector's component is a
                # MOV of its own (notes/104 §6) -- `cb_a.comp`'s
                # `gl_GlobalInvocationID.x` prints `MOV.U R0.x,
                # invocation.globalid; STB.U32 R0, ..`, `cb_c.comp`'s `p.x`
                # (a local) and `u.v.x` (a uniform) `MOV.F R1.x, R0;` and
                # `MOV.F R1.x, R2;`, where the scalar `gl_LocalInvocationIndex`
                # is stored bare
                # A SELECTED COMPONENT is gathered into a scratch `.x` --
                # a construct's lane from its source (`st_f.comp`'s
                # `floatBitsToUint(t).x`: `MOV.U R5.x, R0; STB.U32 R5, ..`,
                # R0 the lane's MOV, vr 10 of its own)
                _g = self._fresh()
                self.lines.append(_emit(
                    _mv, "%s.x" % _g,
                    _crd.split(".")[0] if _crd is not None
                    else _swizzle(src, _cs[0])))
                src = _g
            elif _cs is not None and tuple(_cs[:_n]) != tuple(range(_n)) \
                    and _n != 1:
                raise NotEstablished("a swizzled vector stored into a "
                                     "storage buffer: not measured")
        if _dyn is None:
            addr = "%s[%d]" % (name, off)
        else:
            _dl = _dyn if isinstance(_dyn, list) else [_dyn]
            _ld = self.ldc_at.get(val) if const is None else None
            if _ld is not None and not ENV.get("G2S_STBLDCLATE"):
                # the VALUE is lowered before the address: a block load it
                # reads is made ahead of the index's scaling (`st_g.comp`'s
                # `cb.v[k] = p`: `LDC.U32 R0.x, ..; LDC.F32X4 R1, ..; MUL.S
                # R0.x, R0, {16, ..};`)
                _tg = _sched.Tie([_ld[0]])
                _tg.seq = len(self.lines) - 0.5
                self.ties.append(_tg)
            _t, off, _np = self._dynamic_address(chain, _dl)
            addr = "%s[%s.x%s]" % (name, _t, " + %d" % off if off else "")
        self.lines.append("%s %s, %s;" % (smn, src, addr))

    def _mark_store_movs(self, n0, dst):
        """THE MOVs A WHOLE STORE MAKES are not nodes yet when the one-lane
        read pass runs (glasmlib/replicate.py): the DAG holds the store (op
        0x3a), which the pass skips, and its MOV comes later -- `ps_c.frag`'s
        `MOV.F result_color0, R2;` and the temp's `MOV.F R2, R2;` both read
        a MUL of `R0.x` by `attrib.x` at the full selector (gdb on
        f_7100069f90's visits: the MUL, then the two 0x3a stores).  The MOVs
        a store added writing the stored-to name (`dst`, a local's
        register) or an output are recorded."""
        for _k in range(n0, len(self.lines)):
            _p = _sched.parse(self.lines[_k])
            if _p is None or not _p[0].startswith("MOV"):
                continue
            _d = _p[1][0]
            if (dst is not None and _d == dst) or (
                    dst is None and not _d.startswith("#")):
                self.store_movs.append(_k)

    def _is_self_copy(self, ptr, val):
        """A COMPONENT STORED STRAIGHT BACK -- `u_xlat0.x = u_xlat0.x;`, HLSLcc
        writes these -- is no statement at all: `ss_a.frag` (the local stored
        whole in the block) and `ss_b.frag` (stored before an IF) print
        nothing for it, and the next read still takes what it took before
        (`ss_a`'s clamp reads the MUL).  The value must be a load of the SAME
        component with no store in between.  `G2S_SELFCOPY=1` stores it."""
        if ENV.get("G2S_SELFCOPY"):
            return False
        lc = _local_chain(self.module, ptr, self.by_result, self.locals_)
        _wl = self.whole_load_at.get(val)
        if (lc is None and _wl is not None and _wl == (ptr, self.store_no)
                and not ENV.get("G2S_NOWHOLESELFCOPY")):
            # A WHOLE LOCAL STORED STRAIGHT BACK -- `u_xlat3 = u_xlat3;`,
            # a load and a store of it -- prints nothing either: `ss_d.frag`
            # (the slice's `map_226cebcd`, `MIN.F R3.xy, R7, ..` straight
            # after the pair)
            return True
        _cl = self.comp_load.get(val)
        if _cl is None and not ENV.get("G2S_NOSHUFSELFCOPY"):
            # ... AND SO IS ONE THROUGH A SHUFFLE of the whole load: HLSLcc's
            # `u_xlat11.xy = u_xlat11.xy;` is a load, a `.xy` shuffle and two
            # extract stores, and `ss_c.frag` (the corpus's
            # `chr_hair_f0ad47e1` and nine more) prints nothing for them
            _cl = self._shuffle_lane_of_load(val)
        if _cl is None or _cl[1] != self.store_no:
            return False
        return lc is not None and lc == _cl[0]

    def _shuffle_lane_of_load(self, val):
        """((local, component), store_no at the load) when `val` extracts
        a lane of a same-operand shuffle of a whole local load."""
        _x = self.module.result_insn.get(val)
        if _x is None or _x.opcode != Op.OpCompositeExtract \
                or len(_x.args()) != 2:
            return None
        _sh = self.module.result_insn.get(_x.args()[0])
        if _sh is None or _sh.opcode != Op.OpVectorShuffle \
                or _sh.args()[0] != _sh.args()[1]:
            return None
        _wl = self.whole_load_at.get(_sh.args()[0])
        _sel = _sh.args()[2:]
        _k = _x.args()[1]
        if _wl is None or not 0 <= _k < len(_sel):
            return None
        return ((_wl[0], _sel[_k]), _wl[1])

    def _position_node_seq(self, val, first):
        """The `node[36]` the position's `.x` store carries when it reads a
        local's lane FORWARDED from a plain lane-x store in this block (the
        lane IS the stored value's node, `cfw_kind` "node"): that node's own
        -- `sp_a.vert` (`t.x = dot(a0, a1); gl_Position = t.xxxx;`) prints
        the DP4, `result.position.x`, `t`'s store and the self-move all at
        seq 3, pass 1's list ordering them (`tools/nodedump.py`); so does
        `sb_c.vert`'s `u_xlat3.x` (seq 45).  A merge's lane is a node of its
        own (`pt_a.vert`: DP4 23, the insert 24, `result.position.x` 25)."""
        if ENV.get("G2S_NOPOSNODESEQ"):
            return None
        _ins = self.module.result_insn.get(val)
        if _ins is not None and _ins.opcode == Op.OpVectorShuffle \
                and _ins.args()[0] == _ins.args()[1]:
            _ld, _lane = _ins.args()[0], _ins.args()[2]
        else:
            _ld, _lane = val, self.comps.get(val, _IDENTITY)[0]
        _lw = self.load_of.get(_ld)
        if _lw is None:
            return None
        _var = _lw[0]
        _fw = self.cfw.get(_var, {}).get(_lane)
        if (_fw is None or _fw[0] != self._bkey()
                or self.cfw_kind.get((_var, _lane)) != "node"
                or not first.endswith(", %s;" % _swizzle(_fw[1], _fw[2]))):
            return None
        _defs = [self.defline[_k] for _k, _v in self.values.items()
                 if _v == _fw[1] and self.defline.get(_k) is not None]
        # the instruction's own line: the first that defines the register
        # (a forwarded read of it is `defline`d where it is read)
        return min(_defs) if _defs else None

    def _splat_of_whole_load(self, val):
        """The local's register when `val` is a same-operand shuffle of a
        whole local load (`t.yyyy` as `OpVectorShuffle %l %l 1 1 1 1`)."""
        _sh = self.module.result_insn.get(val)
        if _sh is None or _sh.opcode != Op.OpVectorShuffle \
                or _sh.args()[0] != _sh.args()[1]:
            return None
        _lw = self.load_of.get(_sh.args()[0])
        return _lw[1] if _lw is not None else None

    def _end_forwards(self, ptr):
        """Any store to a local ends what a load in its block may forward,
        and a whole store ends the merge's reign."""
        self.lfwd.pop(ptr, None)
        self.cpair.pop(ptr, None)
        _spc = self.by_result.get(ptr)
        if _spc is not None and _spc.opcode in ACCESS_CHAINS:
            self.lfwd.pop(_spc.args()[0], None)

    # -- a component of a local ----------------------------------------------

    def _store_local_component(self, val, const, lc):
        self.norm_local[lc[0]] = False
        v = self.values.get(val) or const
        if v is None:
            raise NotEstablished("a local component store with no operand "
                                 "form")
        var, ci = lc
        if var in self.whole_load:
            self._store_component_pair(val, const, v, var, ci)
        else:
            self.locals_[var][ci] = (v, self.comps.get(val, _IDENTITY)[0])
        self.stores -= 1                # forwarded, or a pair: not counted

    def _open_for_component_store(self, var):
        """The walker's test for a component store; True when the store
        opened its block.

        PENDING FROM AN EARLIER BLOCK: the walker's test is on the NAME's
        record, which a component store sets as a whole store does (notes/72
        §8).  `ce_n41.vert`'s `u_xlat3.y = dot(u_xlat4, u_xlat6)` --
        `u_xlat3.x` stored in an earlier block, not read since -- has the DP4
        in one block and the pair `MOV.F R5.y, R0.x; MOV.F R5.xzw, R5;` in
        the next, with `result.position.x` (the compiler's blocks 22 and 23).
        As in the whole store, the value's temp stays behind, the DP4 folded
        into its name, and the pair reads that name.  Whatever the value: a
        temp made before stays behind, and an operand that is not a temp --
        an attribute's component, a constant, a name -- is read by the store
        statement itself, so its gather goes in the NEW block, after the cut
        (notes/79 §1)."""
        if self._second_store(var):
            return True
        if (self.lpend.get(var, self._bkey()) != self._bkey()
                and self.blk_node):
            self._flush()
            self.cuts.append(len(self.lines))
            self.stored_key[var] = self._bkey()
            return True
        return False

    def _component_operand(self, val, const, v):
        """(operand, its component, straight into the lane?, value insn).

        THE DESTINATION decides the gather (notes/69), as for every
        single-component store: `.x` straight from any source component
        (`lv_v2a.frag`: `MOV.F R0.x, fragment.attrib[0].z;`), `.y..w` through
        a scratch `.x` (`lo_parts.vert`) ... but a SCALAR VALUE goes straight
        in, as in a construct (`co_mul4`): `lv_cc.vert`'s `t.y = a + b`
        prints `MOV.F R2.y, R0.x;` with no scratch ... a scalar the value's
        OWN instruction wrote: an extract or shuffle of a vector names a
        component of it and gathers through the scratch `.x`
        (`map_0ae40bcc`'s `u_xlat1.xyz = <vec3 ADD>`: `MOV.F R1.x, R2.y;
        MOV.F R12.xzw, R12; MOV.F R12.y, R1.x;`)."""
        module = self.module
        _sc = self.comps.get(val, _IDENTITY)[0]
        _dvi = module.result_insn.get(val)
        _direct = (_is_placeholder(v) and _vi_is_scalar(module, val)
                   and _dvi is not None
                   and self._value_opcode(val) not in _NAMING
                   and not (val in self.lname and v == self.lname[val][0]))
        # ... and so is a SCALAR local's name (notes/72 §5 says a VECTOR's
        # component gathers): the ternary's temp, read whole after the ENDIF
        # -- the cut `mq_n8.frag`'s `hlslcc_movcTemp.y = (b.y) ? .. : ..`
        # prints `MOV.F R0.y, R1.x;` with no scratch
        if ((val in self.arm_names or val in self.load_of)
                and _is_placeholder(v) and _vi_is_scalar(module, val)
                and not ENV.get("G2S_NOARMDIRECT")):
            _direct = True
        # ... and so is ONE LANE OF A CONSTRUCT OF IT: the extract reads the
        # constituent, the scalar local's load -- `compute_volumefog_
        # scatter-1`'s `u_xlatu5.zw = uvec2(u_xlatu8)` (a splat of the load,
        # extracted lane by lane) prints `MOV.U R8.z, R0.x; .. MOV.U R8.w,
        # R0.x;` with no scratch.  `G2S_NOCONLOADDIRECT=1` gathers.
        _src = self._construct_lane_source(val)
        if (_src is not None and not _direct
                and (_src in self.arm_names or _src in self.load_of)
                and v == self.values.get(_src)
                and _is_placeholder(v) and _vi_is_scalar(module, _src)
                and not ENV.get("G2S_NOCONLOADDIRECT")):
            _direct = True
        _split_here = bool(self.cuts) and self.cuts[-1] == len(self.lines)
        if _split_here and const is None and self._stale_ldc(val):
            # the load again, in the block the store opened (`_reload`;
            # `chr_cloth_73643b8a`'s `u_xlat1.y = m[1].x` prints its `LDC.F32
            # R1.x, buf0[80];` after `u_xlat1.x`'s pair, notes/82)
            self._reload(val)
            v = self.values.get(val)
        if _split_here and const is None:
            # THE STORE OPENED A BLOCK: a local it reads is read there, by
            # NAME -- the `OpLoad` before the store was forwarded from the
            # old block's stores (`pk_d.vert`: `u.x = t.x` after `u = b`, t
            # stored in the block before: `MOV.F R3.x, R5;`, R5 = t)
            _nm = self._name_read(val)
            if _nm is not None:
                v, _sc = _nm
                # ... a VECTOR's component gathers, but a SCALAR local's
                # name goes straight in, as above: `pg_d.frag`'s `u_xlat2.w =
                # u_xlat10_20` (a float) in the block its store opened prints
                # `MOV.F R2.w, R0.x;` (the corpus's `map_02077bd8`)
                _direct = bool(
                    val in self.load_of and _is_placeholder(v)
                    and _vi_is_scalar(module, val)
                    and not ENV.get("G2S_NOSCALARNAMEDIRECT"))
                # ... and a read of the NAME from a later block restarts its
                # record (notes/65 §4, `_touch_by_name`), so the local's next
                # store does not open a block: `nr_a.frag`'s `u_xlat3.y =
                # u_xlat2.x` (a block of its own) reads `u_xlat2` by name,
                # `MOV.F R0.x, R5;`, and the later `u_xlat2.x = c9.x * c15`
                # shares its block and loads with `u_xlat2.z = ..` (one
                # `LDC.F32X2` for `c9`)
                self._touch_by_name(v)
        _crd = self._con_read(val) if const is None else None
        if _crd is not None:
            # A COMPONENT OF A CONSTRUCT MADE IN THIS BLOCK: the store reads
            # that component's source, a scalar in `.x` (`chr_cloth_04058fad`:
            # `u_xlat16.x = u_xlat7.x` right after `u_xlat7.xyz = vec3(..)`
            # prints `MOV.F R16.x, R3;`, R3 being what `MOV.F R7.x, R3;`
            # wrote)
            # ... into ANOTHER lane the read stays a component select of the
            # construct and gathers through a scratch `.x`, as an output
            # lane's does (`cr_f`): `cr_o.frag`'s `u_xlat1.w = u_xlat0.w`
            # prints `MOV.F R8.x, R4; .. MOV.F R9.w, R8.x;`
            # (`map_2948729f`: `MOV.F R0.x, R9; .. MOV.F R28.w, R0.x;`)
            v, _sc = _crd.split(".")[0], 0
            _direct = bool(ENV.get("G2S_CONLANEDIRECT"))
        _lc = self.ldc_canon.get(val) if const is None else None
        if (_lc is not None and _lc[1] is None and val not in self.comps
                and val not in self.node_loads
                and v == self.values.get(val) and v in self.ldc_vreg
                and self.ldc_vreg[v][0] == self._bkey()
                and _vi_is_scalar(module, val)
                and not ENV.get("G2S_NOLDCLANEFOLD")):
            # A SCALAR BLOCK LOAD IS SUBSTITUTED INTO THE STORE (notes/67 §1,
            # notes/104 §4): the insert reads the load's node, a scalar, with
            # no scratch -- the fold dumps of `lf_c.frag` (`u.w = s`: the
            # 0x47 into lane w on the 0x3b itself, the clamp on the same
            # node) and `lf_d`/`lf_g` (another reader of `s`, before or
            # after: the same) -- `MOV.F R2.w, R3.x; MIN.F R0.x, R3, ..`
            # (`chr_cloth_b1810a88`)
            _direct = True
        return v, _sc, _direct, _dvi

    def _construct_lane_source(self, val):
        """The constituent an `OpCompositeExtract` of an
        `OpCompositeConstruct` of scalars names, or None."""
        _e = self.module.result_insn.get(val)
        if (_e is None or _e.opcode != Op.OpCompositeExtract
                or len(_e.args()) != 2):
            return None
        _c = self.module.result_insn.get(_e.args()[0])
        if _c is None or _c.opcode != Op.OpCompositeConstruct:
            return None
        _parts = _c.args()
        _k = int(_e.args()[1])
        if not 0 <= _k < len(_parts) or not all(
                _vi_is_scalar(self.module, p) for p in _parts):
            return None
        return _parts[_k]

    def _component_mov(self, var):
        """THE LOCAL'S OWN TYPE names the MOVs: a bool vector's component
        store is `MOV.U R3.x, R0;` (`pb_a.frag`) -- a bool is held in its
        representation (U32), as the whole store's MOV is."""
        _ptc = _pointee_code(self.module, var)
        if _ptc == BOOL_TYPE_CODE:
            _ptc = _BOOL_REPR_CODE
        _cmv = self._mov_for(_ptc)
        if _cmv is None:
            raise NotEstablished("a component store whose MOV the image does "
                                 "not name")
        return _cmv

    def _component_gather(self, v, _sc, _direct, const, ci, _cmv):
        """The operand the lane write reads, gathering it first when it has
        to."""
        if const is not None:
            # slot 0 into lane x is the identity and prints bare
            # (`bv_n62.vert`'s `u_xlat3.x = float(0.0)`: `MOV.F R39.x, {0, 0,
            # 0, 0};`); another lane reads slot 0 through `.x` (`mb_n5`'s
            # `.w`)
            return v if ci == 0 else "%s.x" % v
        if _direct and ci != 0:
            return "%s.x" % v
        if ci != 0:
            _g = self._fresh(is_wide=True)
            self.lines.append(_emit(_cmv, "%s.x" % _g, _swizzle(v, _sc)))
            return "%s.x" % _g
        return _swizzle(v, _sc)

    def _store_component_pair(self, val, const, v, var, ci):
        _vt = _pointee_components(self.module, var)
        if _vt is None or not 2 <= _vt <= 4:
            raise NotEstablished(
                "a component store into a local that is not a vector")
        _copened = self._open_for_component_store(var)
        self.local_seen = True
        _reg = self._local_register(var)
        v, _sc, _direct, _dvi = self._component_operand(val, const, v)
        _cmv = self._component_mov(var)
        _rk = (self._retarget_node(val, "%s.x" % _reg, ldc=True)
               if ci == 0 and const is None and v == self.values.get(val)
               else None)
        if _rk is not None:
            # the load's node IS the lane (core.py `_retarget_node`): the
            # fold dumps of `ld_mx` (a matrix part's MOV) and `lx_b.frag` (a
            # scalar block load: the merge 0x57 on the 0x3b itself, `LDC.F32
            # R1.x, buf0[16];`)
            _wline = _rk
            v, _sc = _reg, 0
        else:
            _o = self._component_gather(v, _sc, _direct, const, ci, _cmv)
            _wline = len(self.lines)
            self.lines.append(_emit(_cmv, "%s.%s" % (_reg, _COMPONENTS[ci]),
                                    _o))
        # notes/55 §7: the merge's copy of the OTHER components passes their
        # value through; the scheduler must know.  Only the local's own
        # components (`lv_v2a.frag`'s vec2: `MOV.F R0.y, R0;`).  A local that
        # stores NO other component has nothing to pass through, and prints
        # the write alone (`mb_n5.vert`: `u_xlat0.w = 1.0` its only store,
        # `MOV.F R0.w, {1, 0, 0, 0}.x;` and no copy).
        _pmask = "".join(
            _COMPONENTS[k] for k in range(_vt)
            if k != ci and k in self.lstored.get(var, set(range(4))))
        # THE LANE IS THE NODE ITSELF only for a plain store into lane x: a
        # scalar lives in `.x`, so a vector defined at x alone IS the scalar,
        # and the value keeps its own store (the flush) and its second use.
        # Into another lane the store is an insert, a node of its own
        # (`sel_n.frag`: `u_xlatb0.z = a.x < a.y;` prints `MOV.U R0.z, R0.x;`
        # with no flush, where `sel_b.frag`'s `.x` prints `MOV.U R1.x, R1;`).
        _is_node = (not _pmask
                    and (ci == 0 or ENV.get("G2S_LANEFLUSH")))
        if _pmask:
            self._component_pass_through(val, var, _dvi, _wline, _copened,
                                         _cmv, _reg, _pmask, ci)
        elif _is_node and self._component_is_statement_temp(val, v, const,
                                                            _dvi):
            self._component_temp_flush(val, v, _cmv)
        self.locals_[var] = dict((i, (_reg, i)) for i in range(4))
        _fv, _fc = v, _sc
        _deep = self._deep_of(v, _sc)
        if _deep is not None and not ENV.get("G2S_NOLANEDEEP"):
            # A PLAIN COPY IS TRANSPARENT to a later reader of the lane: the
            # inserted value is the other local's register, whose own store
            # is a copy of a value, and the DAG reads that value instead --
            # as a construct's lane does (core.py `_con_read`).
            # `map_4a96b6fb-1`: `u_xlat10_25 = texture(..).x; u_xlat4.w =
            # u_xlat10_25; u_xlat4.w = clamp(u_xlat4.w, 0, 1)` prints `MOV.F
            # R10.x, R11; MOV.F R1.w, R10.x;` and `MIN.F R8.x, R11, ..` --
            # the MIN on the TEXTURE (`tools/gsum.py` nodes 121.5 / 121.7 /
            # 121.10: the lane store reads the copy, the MIN its source).
            # A copy of another local's NAME is a node of its own and the
            # read stops at it (`sw_b.frag`, `cfw_deep` is not set there).
            # `G2S_NOLANEDEEP=1`.
            _fv, _fc = _deep
        self.cfw.setdefault(var, {})[ci] = (self._bkey(), _fv, _fc)
        self.cfw_kind[(var, ci)] = ("node" if _is_node
                                    else "merge" if _pmask else "insert")
        if (_is_node and v in self.local_reg.values()
                and not ENV.get("G2S_NAMECOPYFWD")):
            # ... but a copy of ANOTHER LOCAL's component is a node of its own:
            # `sw_b.frag`'s `u_xlat4.x = u_xlat10_4.x; .. clamp(u_xlat4.x)`
            # prints `MOV.F R1.x, R4; MIN.F R2.x, R1, ..` -- the read takes
            # the copy, not the other local's name
            self.cfw_kind[(var, ci)] = "namecopy"
        self.cfw_via.pop((var, ci), None)
        if _pmask and const is None:
            self._merge_lane_via(val, var, ci)
        self.lpend[var] = self._bkey()

    def _component_pass_through(self, val, var, _dvi, _wline, _copened,
                                _cmv, _reg, _pmask, ci):
        """The pair's copy half.

        ONE `node[36]` FOR BOTH HALVES (notes/91).  `bl_257.vert`, every pair
        of the prologue:
        * the first store of the local in its block is one seq (`u_xlat5.x =
          dot(..)`: 127 twice);
        * so is a store that opened its block when the value reads the local
          (`u_xlat9.x = u_xlat9.x * 3.0 + -1.0` after `.z`: 490 twice);
        * a store that opened its block without reading it (`u_xlat5.y`
          after `.x`: 131 and 133), and an extract's store (`u_xlat2.xyz =
          <vec3>`, 185 and 187), are two.
        Tied, pass 1's list decides: the copy first."""
        self.cpair[var] = (self._bkey(), _wline, len(self.lines), _cmv)
        self.cpair_tied[var] = False
        # a component LOAD selects too, as an extract does (notes/102 §3):
        # `map_e58292f4`'s `u_xlat19.x = u_xlat20.y` is write 328,
        # pass-through 329 (`tools/nodedump.py`)
        _selects = ((_dvi is not None
                     and _dvi.opcode == Op.OpCompositeExtract)
                    or (val in self.comp_load
                        and not ENV.get("G2S_COMPLOADTIE"))
                    or (self._input_component_load(_dvi)
                        and not ENV.get("G2S_INLOADTIE")))
        # WHICH LANE decides, not whether the store opened its block
        # (notes/106, `tools/nodedump.py`): a write into lane x shares its
        # copy's seq -- `pt_i.frag`'s `u_xlat3.x = a.x * a.y`, which opened
        # its block without reading the local, 14 and 14; `nr_a.frag`'s
        # `u_xlat3.x = -u_xlat1.x`, 22 and 22 -- and a write into another
        # lane does not, opened or not: `pt_i`'s first store `u_xlat3.z`, 3
        # and 5; `pt_j`'s `u_xlat3.z`, 12 and 14.  (`bl_257`'s cases above
        # are all this.)  `G2S_PAIRTIEOLD=1` restores the opened-block test.
        if ENV.get("G2S_PAIRTIEOLD"):
            _tie = (not _copened or self._value_reads_local(val, var))
        else:
            _tie = ci == 0
        if (not _selects and _tie
                and ENV.get("G2S_PAIRTIE", "1") != "0"):
            self.ties.append([_wline, len(self.lines)])
            self.cpair_tied[var] = True
        self.passthru.append(len(self.lines))
        self.lines.append(_emit(_cmv, "%s.%s" % (_reg, _pmask), _reg))

    def _input_component_load(self, _dvi):
        """Whether `_dvi` loads ONE COMPONENT of an input vector through a
        constant-index chain (`a0.x`): a component select of the input, as
        a local's component load is -- `sp_i.vert`'s `t.x = a0.x` after
        `t.y = ..` is write 8, pass-through 10, where `sp_g`'s `t.x = a0.x *
        a1.x` (a MUL) and `sp_h`'s `t.x = 1.0` share one seq
        (`tools/nodedump.py`)."""
        if _dvi is None or _dvi.opcode != Op.OpLoad:
            return False
        _ch = self.by_result.get(_dvi.args()[0])
        if _ch is None or _ch.opcode not in ACCESS_CHAINS \
                or len(_ch.args()) != 2:
            return False
        _g = self.module.globals.get(_ch.args()[0])
        return (_g is not None and _g.operands[2] == StorageClass.Input
                and _vi_is_scalar(self.module, _dvi.result)
                and (_pointee_components(self.module, _ch.args()[0]) or 0)
                >= 2)

    def _component_is_statement_temp(self, val, v, const, _dvi):
        # a LOCAL's register is a name, not a statement's temp: `sw_b.frag`'s
        # `u_xlat4.x = u_xlat10_4.x` reads the name and flushes nothing
        return (_is_placeholder(v) and const is None
                and (v not in self.local_reg.values()
                     or ENV.get("G2S_NAMEFLUSH"))
                and self.defblk.get(val) == self.blk_no
                and self.defline.get(val) is not None
                and val not in self.load_of and val not in self.arm_names
                and val not in self.node_loads
                and _dvi is not None
                and _dvi.opcode not in _COMPONENT_NAMING
                and _vi_is_scalar(self.module, val)
                and self.cuts[-1:] != [len(self.lines) - 1]
                and not ENV.get("G2S_NOCOMPFLUSH"))

    def _component_temp_flush(self, val, v, _cmv):
        """WITH NO MERGE the component store is a plain store of the temp
        into the name, and the temp has its own store, flushed like a whole
        store's (`mc_n3.vert`: `u_xlat2.x = (-u_xlat1.x) + ..`, `u_xlat2`
        stored only at `.x`, prints `MOV.F R12.x, R13;` and later `MOV.F
        R13.x, R13;`)."""
        start = self.defline[val]
        _grp = list(range(start, len(self.lines)))
        self.ties.append(_grp)
        self.stmtpos[v] = start
        self.flush_q.append((v, _cmv, ".x", _grp, start))

    # -- a whole local ------------------------------------------------------

    def _store_local(self, ptr, val, const):
        module = self.module
        v = self.values.get(val) or const
        _lv = self.load_of.get(val)
        if self._store_partial_copy(ptr, val, _lv):
            return
        _cp = self.cpair.get(_lv[0]) if _lv is not None else None
        _mfw = None
        if (_cp is not None and _cp[0] == self._bkey() and _lv[1] == v
                and val in self.lsplit and _lv[0] != ptr
                and not ENV.get("G2S_NOMERGEFWD")):
            # `w = u` right after `u.c = ..` in one block reads the MERGE
            # (`_merge_forward`, the cut `mq_n8.frag`)
            v = _mfw = self._merge_forward(_lv[0])
            self.values[val] = v
        if v is None:
            _vd = module.result_insn.get(val)
            raise NotEstablished(
                "a local store with no operand form (the value is %s)"
                % (OP_NAME.get(_vd.opcode, _vd.opcode)
                   if _vd is not None else "not an instruction"))
        if (val in self.splats and ptr in self.whole_load
                and not ENV.get("G2S_NOSPLATNODE")):
            v = self._splat_node(val, v)
        cs = self.comps.get(val, _IDENTITY)
        _vi1 = module.result_insn.get(val)
        # A SELECTION of another value -- a shuffle, an extract, a component
        # load -- is a node of its own, which the store materialises and a
        # later read takes from the local (`g2s_trace_graph`: `sq_a.frag`'s
        # `u_xlat0 = u_xlat1.y` and `sq_b.frag`'s `.x` alike, the reader's
        # source is the store node; `sel_q.frag`'s `.xy` the same).  A
        # scalar result carries comps (0, 0, 0, 0) and is no selection.
        _selected = (_vi1 is not None and (
            _vi1.opcode in (Op.OpVectorShuffle, Op.OpCompositeExtract)
            or (_vi1.opcode == Op.OpLoad and val in self.comp_load)))
        if (ptr in self.whole_load and cs != _IDENTITY
                and _vi1 is not None and _vi1.has_result_type
                and _components(module, _vi1.result_type) in (1, 2, 3, 4)
                and not v.startswith("{")):
            # THE SELECTOR IS THE SOURCE'S, printed as any operand's against
            # the write mask (`sc_sel.frag`: `MOV.F R0.x,
            # fragment.attrib[0].y;`; `lv_v2a.frag`'s vec2 of `a.xy`: `MOV.F
            # R0.xy, fragment.attrib[0];`; `lc_a.frag`'s whole vec4 of
            # `a.yzwx`: `MOV.F R0, fragment.attrib[0].yzwx;`).
            v = _source(self.values, self.comps, val,
                        _components(module, _vi1.result_type))
            cs = _IDENTITY
        if ptr in self.whole_load and cs != _IDENTITY:
            raise NotEstablished(
                "a swizzled whole store into a materialised local")
        if ptr in self.whole_load:
            self._store_materialised(ptr, val, v, _mfw, _selected)
        else:
            self.locals_[ptr] = dict((i, (v, cs[i])) for i in range(4))
        self.stores -= 1                # forwarded, or a local: not counted

    def _splat_node(self, val, v):
        """A SPLAT STORED INTO A LOCAL IS A NODE (notes/111): the front
        end's splat is a swizzle of the scalar (`_construct_splat`), and a
        store makes it a MOV at the local's width that the local's store and
        every forwarded read then take -- `ldv_v4.vert`'s `u_xlat1 =
        vec4(m[0].w + a.x); p = u_xlat1;` prints `ADD.F32 R0.x, ..; MOV.F
        R2, R0.x; MOV.F result.attrib[1], R2; .. MOV.F R2, R2;`
        (`g2s_trace_fold`: the 0x47 with selector xxxx, seq 10, and two MOVs
        of it stored, the output's and the local's, seq 10 too).  A MATRIX
        ELEMENT's splat is its own MOV at that width: `u_xlat0 =
        vec4(m[0].y)` is `LDC.F32X2 R0.y, ..; MOV.F R1, R0.y;` (the element's
        MOV, selector yyyy), not the element's `.x` MOV splatted.
        `G2S_NOSPLATNODE=1` stores the swizzle into the local as before."""
        _mv, _ds = self._whole_store_mov(val)
        _c = self.comps.get(val, _IDENTITY)[0]
        _base = v
        _src = self.splat_src.get(val)
        if _src is not None and _src in self.node_line:
            # the element's own `.x` MOV goes: the splat is that node
            _k, _tok = self.node_line[_src]
            _m = _lane_x_move(self.lines[_k])
            if (_m is not None and _k == len(self.lines) - 1
                    and len(self._readers_of(_src)) == 1):
                self.lines[_k] = "%s%s%s, %s;" % (_m[0], _m[1], _ds, _m[2])
                self.values[val] = _m[1]
                self.comps.pop(val, None)
                self.splats.discard(val)
                self.splat_nodes.add(val)
                self.defline[val] = _k
                self.defblk[val] = self.blk_no
                return _m[1]
        _t = self._fresh()
        self.splat_nodes.add(val)
        self.defline[val] = len(self.lines)
        self.defblk[val] = self.blk_no
        self.lines.append(_emit(_mv, _t + _ds, _swizzle(_base, _c)))
        self.values[val] = _t
        self.comps.pop(val, None)
        self.splats.discard(val)
        return _t

    def _whole_store_mov(self, val):
        """THE COLOUR STORE'S SHAPE (notes/64): the MOV takes the value's
        type and its write mask from the component count (`MOV.S R0.x, ...`
        for cf_loop's int counter).  A bool is its representation by the time
        a MOV prints (f_7100030e20 retypes every bool-typed node,
        0x30e44..0x30ea4 -- notes/64 §4)."""
        module = self.module
        _vi = module.result_insn.get(val)
        _vt = (_vi.result_type if _vi is not None and _vi.has_result_type
               else None)
        if _vt is None:
            _ci = module.constants.get(val)
            _vt = _ci.result_type if _ci is not None else None
        _tc = _glasm_type_code(module, _vt) if _vt else None
        if _tc == BOOL_TYPE_CODE:
            _tc = _BOOL_REPR_CODE
        _mv = self._mov_for(_tc)
        _nc = _components(module, _vt) if _vt else None
        _ds = _opchain.dest_suffix(_nc) if _nc else None
        if _mv is None or _ds is None:
            raise NotEstablished(
                "a whole local store whose MOV or mask the image's rules do "
                "not give")
        return _mv, _ds

    def _store_materialised(self, ptr, val, v, _mfw, selected=False):
        self.local_seen = True
        _reg = self._local_register(ptr)
        _mv, _ds = self._whole_store_mov(val)
        _sec = self._second_store(ptr)
        _here = self.defblk.get(val) == self.blk_no
        # PENDING FROM AN EARLIER BLOCK -- any block, not only a marker's:
        # `ce_head4.vert`'s `u_xlat3 = <construct>` is stored in the block
        # the previous `u_xlat2` store opened, while `u_xlat3`'s last store
        # is pending from the block before; the compiler opens a block AT the
        # store (record 33 at seq 102 in a block of its own start), and the
        # statements that computed the value stay behind.  Keyed on the
        # cut-aware block (`_bkey`).
        _split = (not _sec
                  and self.lpend.get(ptr, self._bkey()) != self._bkey()
                  and self.blk_node)
        if _sec:
            # a SECOND store in the block opened a block at the store: the
            # value was made in the block before and its temp's store folded
            # into it there -- the local reads the temp's name, and there is
            # no flush (`lv_f1.frag`: `LG2.F32 R1.x, R3.x;` then, in the next
            # block, `MOV.F R0.x, R1;`)
            self.lines.append(_emit(_mv, _reg + _ds, v))
        elif _split and (_here or val in self.module.constants):
            self._store_opening_block(ptr, v, _reg, _mv, _ds)
        elif _split:
            raise NotEstablished(
                "a local store opening a block whose value was made in an "
                "earlier one")
        else:
            if (self._store_in_block(val, v, _reg, _mv, _ds, _here,
                                     merge_reader=_mfw is not None)
                    and not ENV.get("G2S_NORETARGETFWD")):
                # the load's node now writes the local: a read in the block
                # takes the register (the old value is gone) --
                # `lens_flare_v.vert`'s `u_xlatu1 = buf.v; .. float(u_xlatu1)`
                # prints `LDB.U32 R0.x, sbo_buf1[0]; .. I2F.U R2.x, R0;`
                v = _reg
        if _mfw is not None:
            # the reader is the merge's statement's
            self.merge_grp[_mfw].append(len(self.lines) - 1)
        self.lpend[ptr] = self._bkey()
        self.locals_[ptr] = dict((i, (_reg, i)) for i in range(4))
        self._record_local_forward(ptr, val, v, _reg, selected)

    def _store_partial_copy(self, ptr, val, _lv):
        """A WHOLE COPY OF A LOCAL STORED ONLY IN PART is a store of the
        components the source ever stores (`_stored_mask`, as the colour
        store's, notes/87) -- so into another local it is a PAIR: the write
        at that mask and the copy of the destination's other stored
        components.  The source is read by its NAME: a component-wise value
        has no merge node to forward (`fa_e.frag`: `u_xlat0.x = ..; u_xlat0.y
        = ..; u_xlat2 = u_xlat0;` prints the pair writing `u_xlat0` itself,
        then `MOV.F R1.zw, R1; MOV.F R1.xy, R2;`; with `u_xlat0` stored whole
        before, `fa_f.frag`, the copy is the merge's, notes/85).

        Only in the block the destination's store stays in; True when the
        store was made here.  `G2S_NOPARTIALCOPY=1` turns it off."""
        if (_lv is None or _lv[0] == ptr or ptr not in self.whole_load
                or _lv[0] not in self.local_reg
                or ENV.get("G2S_NOPARTIALCOPY")):
            return False
        _src = _lv[0]
        _n = _pointee_components(self.module, _src)
        _nd = _pointee_components(self.module, ptr)
        _st = sorted(c for c in self.lstored.get(_src, set(range(_n or 0)))
                     if _n and c < _n)
        if not _n or _nd != _n or not _st or _st == list(range(_n)):
            return False
        if (self._second_store(ptr)
                or self.lpend.get(ptr, self._bkey()) != self._bkey()):
            raise NotEstablished(
                "a whole copy of a partly stored local opening a block: not "
                "measured")
        self.local_seen = True
        _reg = self._local_register(ptr)
        _mv, _ds = self._whole_store_mov(val)
        _wmask = "".join(_COMPONENTS[c] for c in _st)
        _pmask = "".join(
            _COMPONENTS[k] for k in range(_nd)
            if k not in _st and k in self.lstored.get(ptr, set(range(4))))
        _wline = len(self.lines)
        self.lines.append(_emit(_mv, "%s.%s" % (_reg, _wmask),
                                self.local_reg[_src]))
        if _pmask:
            self.cpair[ptr] = (self._bkey(), _wline, len(self.lines), _mv)
            self.ties.append([_wline, len(self.lines)])
            self.passthru.append(len(self.lines))
            self.lines.append(_emit(_mv, "%s.%s" % (_reg, _pmask), _reg))
        self.lpend[ptr] = self._bkey()
        self.locals_[ptr] = dict((i, (_reg, i)) for i in range(4))
        for _c in range(4):
            self.cfw_kind.pop((ptr, _c), None)
        self.lfwd[ptr] = (self._bkey(), _reg, None)
        self.cfw[ptr] = dict((c, (self._bkey(), _reg, c)) for c in range(4))
        self.stores -= 1
        return True

    def _store_opening_block(self, ptr, v, _reg, _mv, _ds):
        """The temp's store `tmp = expr` stays in the block before: a value
        already in a register is FOLDED into it (one use, no modifier:
        f_7100032c30), anything else is its carrier MOV (0xf11530).  The
        local then reads the temp's name at the new block's entry -- `MOV.F
        R0, -a0; MOV.F R0, R0;` and `MUL.F32 R0, ...; MOV.F R0, R0;` in
        cf_switch.vert, with no self-move.

        A CONSTANT is no statement's temp: the store opens its block and
        writes the constant itself (`lc_b.frag`'s second loop's `u_xlatu1 =
        0u;` after the first loop: `MOV.U R0.x, {0, 0, 0, 0};`)."""
        # ... a register READ THROUGH A SELECTOR is a register too, no
        # modifier: `so_a.frag`'s second `u_xlat10_22 = texture(..).zw` opens
        # its block and prints the one `MOV.F R5.xy, R0.zwzw;` (the corpus's
        # `map_0a238124`)
        _selected = (_is_selected_placeholder(v)
                     and not ENV.get("G2S_SELCARRIER"))
        if not _is_placeholder(v) and not v.startswith("{") \
                and not _selected:
            _t = self._fresh(True)
            self.lines.append(_emit(_mv, _t + _ds, v))
            v = _t
        self._flush()
        self.cuts.append(len(self.lines))
        # the store is now in the block it opened: a second store there is a
        # second store in ONE block
        self.stored_key[ptr] = self._bkey()
        self.lines.append(_emit(_mv, _reg + _ds, v))

    def _store_in_block(self, val, v, _reg, _mv, _ds, _here,
                        merge_reader=False):
        """`merge_reader`: the store reads a MERGE (`_merge_forward`) and is
        that statement's -- the caller puts it in the merge's group, and no
        group of its own may override it.  MEASURED with `g2s_trace_sel` on
        `map_2fcbac26`: the pair's pass-through, the copy `u_xlat125 =
        u_xlat39` and the local's own store all carry `node[36]` 83, and
        `entry[68]` then puts the copy first.  `G2S_MERGEREADERGRP=1` keeps
        the store's own group."""
        if ((val in self.node_loads or val in self.ldc_at)
                and not merge_reader and v == self.values.get(val)
                and self._retarget_node(val, _reg + _ds,
                                        ldc=True) is not None):
            # the load's node writes the local (core.py `_retarget_node`):
            # `mx_g.vert`'s `u_xlat0 = m[2]` is `MOV.F R0, R0;`, `mx_h`'s
            # `u_xlat0 = m2` `LDC.F32X4 R0, buf0[32];` -- no store of its own
            # and no temp to flush.  True: the caller forwards the register.
            return True
        self.lines.append(_emit(_mv, _reg + _ds, v))
        if merge_reader and not ENV.get("G2S_MERGEREADERGRP"):
            return
        _grp = (list(range(self.defline[val], len(self.lines)))
                if _here and self.defline.get(val) is not None else None)
        _fl = self.fwd_line.get(val)
        if (_fl is not None and (not self.cuts or self.cuts[-1] <= _fl)
                and not ENV.get("G2S_NOFWDSEQ")):
            self._tie_to_forwarded_store(_fl)
            _grp = None
        if _grp is not None:
            self.ties.append(_grp)
        # A SELECTION MATERIALISED BY THE STORE: the store's line is the
        # value's FIRST node, so it is a lowering vreg and the LOCAL is its
        # name.  WHETHER is `_is_statement_temp_store`'s question, asked of
        # the value as for any other store; only WHAT IS FLUSHED differs --
        # the local, not the source (`post_monotone.frag`: the compiler's
        # vr 23 is the gather and vr 4 the name, both at `node[36]` 18).
        _vsh = self.module.result_insn.get(val)
        if (_vsh is not None and _vsh.opcode == Op.OpVectorShuffle
                and _here and self.defline.get(val) is not None
                and _is_placeholder(_reg) and _reg not in self.merged
                and self._is_statement_temp_store(val, v, selection=True)
                and _selects_an_image(self.module, _vsh)
                and not ENV.get("G2S_NOSELNAME")):
            self.stmtpos[_reg] = self.defline[val]
            self.flush_q.append((_reg, _mv, _ds, _grp, self.defline[val],
                                 _reg, "selection"))
            return
        if self._is_statement_temp_store(val, v):
            # THE TEMP'S OWN STORE, flushed when the block closes (notes/67
            # §7): after the statements and the branch's `.CC` move, in the
            # statement's position (`sc_select.frag`: seq 1, after the seq-7
            # `.CC`; `cf_loop`: after both stores).
            if _here and self.defline.get(val) is not None:
                self.stmtpos[v] = self.defline[val]
            self.flush_q.append((v, _mv, _ds, _grp,
                                 self.defline.get(val) if _here else None))

    def _tie_to_forwarded_store(self, _fl):
        """A READ FORWARDED from a local's store in this block is that
        store's node, and the store made from it carries the same
        `node[36]`, whatever the first store's is.  `lp_ca.vert`'s `j = k +
        1u; k = j;`: the ADD, both stores and the temp's flush are all seq 29
        (the ADD's, which `j`'s store took); `mq_n8.frag`'s `hlslcc_movcTemp
        = u_xlat0` after `u_xlat0.xy = fract(..)`, which opened its block, is
        23 with that store while the FRC behind the cut is 22 (notes/91).
        Pass 1's list then orders them."""
        for _t in reversed(self.ties):
            if _fl in _t:
                _t.append(len(self.lines) - 1)
                return
        self.ties.append([_fl, len(self.lines) - 1])

    def _is_statement_temp_store(self, val, v, selection=False):
        """Is the stored value a temp of THIS statement, with a store of its
        own to flush?

        Only a temp's WHOLE register: a component of something else --
        `u_xlat10_0 = texture(..).w`, `map_15393bbe` -- is not a temp of this
        statement.  A NAME stored is no temp with a store of its own to
        flush: a local's name read, or a select's result, which the arms
        store -- `u_xlat18 = (b) ? -1.0 : 1.0` prints the one `MOV.F R0.x,
        R0;` after the ENDIF, `mb_n24`."""
        _vx = self.module.result_insn.get(val)
        if ENV.get("G2S_TEMPDBG") in (v, "*"):             # diagnosis only
            import sys as _sys
            _sys.stderr.write(
                "TEMPSTORE %s val=%s op=%s load_of=%s fwd_of=%s name=%s "
                "merged=%s arm=%s comp_load=%s comp_value=%s splat=%s\n"
                % (v, val, _vx.opcode if _vx else None,
                   val in self.load_of, val in self.fwd_of,
                   v in self.local_reg.values(), v in self.merged,
                   val in self.arm_names, val in self.comp_load,
                   val in self.component_values, val in self.splat_nodes))
        return (v.startswith("#")
                and not (val in self.load_of and v == self.load_of[val][1])
                and not (val in self.fwd_of and not ENV.get("G2S_FWDFLUSH"))
                and v not in self.local_reg.values()
                and v not in self.merged
                and val not in self.arm_names
                and _is_placeholder(v)
                and (selection or not (_vx is not None and _vx.opcode
                                       in (Op.OpVectorShuffle,
                                           Op.OpCompositeExtract)))
                # a component load selects too (`sq_b.frag`: the store of
                # `u_xlat1.x` reads the MUL, and only the MUL is flushed)
                and not (val in self.comp_load
                         and not ENV.get("G2S_COMPLOADFLUSH"))
                and val not in self.component_values
                # a splat's node is the swizzle's MOV, no temp of its own
                # (`_splat_node`)
                and val not in self.splat_nodes)

    def _deep_of(self, v, sc):
        """The value behind a LOCAL'S REGISTER whose store in this block is
        a plain copy (`cfw_deep`), as `(value, component)`, or None."""
        _b = v.split(".")[0]
        for _var, _reg in self.local_reg.items():
            if _reg != _b:
                continue
            _d = self.cfw_deep.get(_var)
            if _d is None or sc not in _d or _d[sc][0] != self._bkey():
                return None
            return _d[sc][1], _d[sc][2]
        return None

    def _record_local_forward(self, ptr, val, v, _reg, selected=False):
        """A LOAD IN THIS BLOCK TAKES THE STORED VALUE: the DAG forwards a
        name's store to its reads within the block (notes/65 §3, notes/67 §4)
        -- `sc_select.frag`'s branch reads the TRUNC (R3), not `b0`'s
        register (R0).  A value that SELECTS lanes of another is a node of
        its own, materialised by the store even when its lanes lead the
        source's (`sel_q.frag`: `u_xlatb0.xy = lessThan(a.xyxx, ..).xy;`
        prints `MOV.U R1.xy, R0;` and the read takes R1)."""
        bk = self._bkey()
        # a bool local stored whole only from normalised compares holds
        # normalised bools (control.py `_normalised_view`)
        self.norm_local[ptr] = (self.norm_local.get(ptr, True)
                                and self._normalised_view(val))
        # a whole store's lanes are component selects of the value, not
        # lane stores (`sel_l.frag`: `MOV.U.CC HC.x, R0.y;`)
        for _c in range(4):
            self.cfw_kind.pop((ptr, _c), None)
        if selected and ENV.get("G2S_SELFWD"):
            selected = False
        if val not in self.component_values and not selected and (
                v.startswith("{") or (
                _lex.swizzle_suffix(v, 1, 4) is None
                and (v.startswith("#")
                     or self.comps.get(val, _IDENTITY) == _IDENTITY))):
            self.lfwd[ptr] = (bk, v, val)
            self.cfw[ptr] = dict((c, (bk, v, c)) for c in range(4))
            # A WHOLE COPY OF ANOTHER LOCAL IS TRANSPARENT PER COMPONENT:
            # the copy's reads take what the SOURCE's components were stored
            # from, not the source's register -- the reading of notes/114 §7
            # (a plain copy is transparent) on a component read.
            # `vfx_basic_p.frag`'s `u_xlat0 = movcTemp; .. u_xlat0.y *
            # u_xlat0.x` prints `MUL.F32 R2.x, R2, R7;`, the select's result
            # and the lane-x source, where we read `movcTemp` itself.
            # `G2S_NOCOPYCFW=1` keeps the source's register.
            _lo = self.load_of.get(val)
            if _lo is not None and not ENV.get("G2S_NOCOPYCFW"):
                _sf = self.cfw.get(_lo[0])
                _sreg = self.local_reg.get(_lo[0])
                if _sf:
                    for _c, _e in _sf.items():
                        if _c not in self.cfw[ptr]:
                            continue
                        if _e[0] == bk:
                            self.cfw[ptr][_c] = (bk, _e[1], _e[2])
                        elif (_sreg is not None
                              and not ENV.get("G2S_NOCOPYCFWREG")):
                            # ... and a component the source stored in an
                            # EARLIER block is read at the copy's line as
                            # the source's own register, which is what the
                            # copy forwards (notes/114 §21).  `ct_a.frag`:
                            # `tools/gsum.py` gives the MUL the same source
                            # node as the copy's lane-x line, 0x71e8.
                            self.cfw[ptr][_c] = (bk, _sreg, _c)
            # the store's own line, for a store made from a read this
            # forwards (its `node[36]`, notes/91)
            _lp = _sched.parse(self.lines[-1]) if self.lines else None
            if _lp is not None and _lp[1][0] == _reg:
                self.lfwd_line[ptr] = len(self.lines) - 1
            else:
                self.lfwd_line.pop(ptr, None)
        else:
            # a SWIZZLED value (`u_xlat10_0 = texture(..).w`) is materialised
            # by the store's MOV, and a read in the block takes that MOV --
            # the local's register (`map_15393bbe`: `MOV.F R2.x, R9.w;` then
            # `MUL.F32 R3.x, R1, R2;`).  A value that is an INPUT's operand
            # (`vertex.attrib[0]`) is forwarded as itself: the reader
            # re-reads the input (`cf_call.vert`'s `MOV.F R0,
            # vertex.attrib[0];` then the parameter's `MOV.F R1,
            # vertex.attrib[0];`).
            self.lfwd[ptr] = (bk, _reg, None)
            self.cfw[ptr] = dict((c, (bk, _reg, c)) for c in range(4))
            # ... and the SWIZZLE ITSELF is kept: a merge lane stored from a
            # component of this local reads through it (`_merge_lane_via`)
            # (`v` prints the selector: `#17.wxyz`, comps (3, 0, 1, 2))
            _cs = self.comps.get(val)
            _vb = v.split(".")[0]
            if (_is_placeholder(_vb) and _cs is not None
                    and _vb not in self.local_reg.values()
                    and v in (_vb, _vb + "." + "".join(
                        _COMPONENTS[c] for c in _cs))):
                self.cfw_deep[ptr] = dict((c, (bk, _vb, _cs[c]))
                                          for c in range(len(_cs)))
                return
        self.cfw_deep.pop(ptr, None)

    def _merge_lane_via(self, val, var, ci):
        """A MERGE LANE STORED FROM A COMPONENT OF ANOTHER LOCAL, itself
        stored whole in this block from a swizzle of a node, is read in the
        block as THAT NODE at the composed component: `tw_j.frag`'s
        `u_xlat10_4 = textureLod(..).wxyz; u_xlat4.x = u_xlat10_4.x;
        clamp(u_xlat4.x, ..)` prints `MOV.F R2.x, R4;` for the lane (the
        swizzle's MOV) but `MIN.F R1.x, R3.w, ..` for the read (the fold
        dump: the 0x8e's slot is the 0xb7 with selector 0x02010003).  A read
        of the other local's component itself takes the swizzle's MOV
        (`R4.x`), and a plain lane-x store is a node of its own (`sw_b`)."""
        if ENV.get("G2S_NOMERGEVIA"):
            return
        _cl = self.comp_load.get(val)
        if _cl is None:
            return
        (_l, _k), _sn = _cl
        _dp = self.cfw_deep.get(_l, {}).get(_k)
        _cf = self.cfw.get(_l, {}).get(_k)
        bk = self._bkey()
        if (_dp is None or _dp[0] != bk or _cf is None or _cf[0] != bk
                or _cf[1] != self.local_reg.get(_l) or _cf[2] != _k):
            return
        self.cfw_via[(var, ci)] = (self.cfw[var][ci], _dp)

    # -- outputs --------------------------------------------------------------

    def _store_output(self, ptr, val, const):
        module = self.module
        src = self.values.get(val) or const
        if src is None:
            raise NotEstablished("a stored value with no operand form")
        if (self._stale_ldc(val) and self.model != ExecutionModel.Geometry
                and (_position_chain(module, ptr) is None
                     or _position_chain(module, ptr) < 0)
                and _output_chain(module, ptr, self.model,
                                  self.by_result) is None):
            # the component stores re-load after their block split
            # (`_reload`); a whole store's element blocks are not wired
            raise NotEstablished(
                "a block load stored whole in a later block: its re-load per "
                "element block is not measured")
        _pl = _patch_level_chain(module, ptr, self.by_result)
        if _pl is not None:
            self._store_patch_level(val, src, _pl)
            return
        dst = _colour_output_name(module, ptr, self.model)
        if dst is not None:
            self._store_colour(val, src, dst)
            return
        comp = _position_chain(module, ptr)
        if comp is not None and self.model == ExecutionModel.Geometry:
            self._store_geometry_position(ptr, val, const, src, comp)
            return
        if comp is not None:
            self._store_position(val, const, src, comp)
            return
        _oc = _output_chain(module, ptr, self.model, self.by_result)
        if _oc is not None:
            self._store_output_component(val, const, src, _oc)
            return
        odst = _output_operand(module, ptr, self.model)
        if odst is not None:
            self._store_output_whole(val, const, src, odst)
            return
        if self.scalarised:
            raise NotEstablished(
                "a store to something else after a gl_Position store: the "
                "compiler interleaves them (notes/31)")
        raise NotEstablished(
            "a store whose destination is neither a fragment colour output, "
            "a stage output with a Location, nor gl_Position")

    def _refuse_interleave(self):
        """ORDER, NOT LOWERING -- and now SCHEDULED.  `if_out.vert` and
        `p04_out.vert` store gl_Position and then another output, and the
        compiler emits the second store BETWEEN the last two instructions of
        the first.  That is the block rule of notes/55 §8: a block holds one
        store per name, so the second output shares the `.w` element's block,
        and the scheduler orders the two (both exact).
        `G2S_NOINTERLEAVE=1` restores the refusal."""
        if self.scalarised and ENV.get("G2S_NOINTERLEAVE"):
            raise NotEstablished(
                "a store to something else after a gl_Position store: the "
                "compiler interleaves them (notes/31)")

    def _store_patch_level(self, val, src, _pl):
        """notes/63: one scalar patch output, written `.x` straight from its
        source (`MOV.F result.patch.tessouter[2].x, {1, 0, 0, 0};`)."""
        module = self.module
        _vi = module.result_insn.get(val)
        _tc = (_glasm_type_code(module, _vi.result_type)
               if _vi is not None and _vi.has_result_type else None)
        _mv = self._mov_for(_tc)
        if _mv is None or _components(module, _vi.result_type) != 1:
            raise NotEstablished(
                "a tessellation level stored from a value whose MOV or width "
                "the image's rules do not give")
        if src.startswith("#"):
            src = src + ".x"
        self.lines.append("%s %s.x, %s;" % (_mv, _pl, src))

    # -- colour outputs

    def _value_mov(self, val):
        """(MOV, component count, destination suffix) from the stored
        value's type.

        THE SUFFIX IS THE VALUE'S TYPE, not a constant `.F`.
        `int_umin.frag` stores a uvec4 and the compiler prints `MOV.U
        result_color0, R0;` -- the same suffix rule as every other opcode
        (notes/37), applied to the stored value's type with the MOV opcode.
        AND THE WRITE MASK: `fr_out2.frag` stores a vec2 and the compiler
        prints `MOV.F result_color0.xy, R0;` with the self-move masked to
        match."""
        module = self.module
        _ty, _n = None, None
        _vi = module.result_insn.get(val)
        if _vi is not None and _vi.has_result_type:
            _ty = _glasm_type_code(module, _vi.result_type)
            _n = _components(module, _vi.result_type)
        _mov = (_opchain.mnemonic_for_opcode(nodes.MOV, _ty)
                if _ty is not None else None)
        _ds = _opchain.dest_suffix(_n) if _n else None
        if _mov is None or _ds is None:
            raise NotEstablished(
                "an output store whose MOV suffix or write mask the image's "
                "rules do not give for this value's type")
        return _mov, _n, _ds

    def _lane_mov(self, val):
        """The MOV of ONE LANE of an output store: its suffix is the stored
        scalar's type, an instruction's or a constant's, as the whole store's
        is (`sel_r.frag` stores a uint select and uint constants into a
        uvec4's lanes: `MOV.U result_color0.w, R0.x;`, `MOV.U
        result_color0.x, {1, 0, 0, 0};`)."""
        module = self.module
        _vi = module.result_insn.get(val)
        _vt = (_vi.result_type if _vi is not None and _vi.has_result_type
               else None)
        if _vt is None:
            _ci = module.constants.get(val)
            _vt = _ci.result_type if _ci is not None else None
        _ty = _glasm_type_code(module, _vt) if _vt is not None else None
        _mov = (_opchain.mnemonic_for_opcode(nodes.MOV, _ty)
                if _ty is not None else None)
        if _mov is None:
            raise NotEstablished(
                "an output lane store whose MOV suffix the image's rules do "
                "not give for this value's type")
        return _mov

    def _close_colour_block(self, dst):
        """A SECOND STORE TO THE OUTPUT IN ONE BLOCK OPENS A BLOCK (notes/55
        §8, sched.py `_block_spans`), and the block that closes takes its
        temps' stores with it (notes/67 §7).  The cut `mq_n26.frag` stores
        `SV_Target0` twice: `u_xlatb0 = u_xlat0.x < 0.0`'s name store `MOV.U
        R8.x, R1;` prints BEFORE the second store, in the first block -- so
        the flushed lines end the old block (the boundary moves past them, as
        past a call's write, `sched._block_spans`)."""
        _opened = False
        _okey = ("out", dst)
        if (not ENV.get("G2S_NOCOLCOMPPEND") and _okey in self.stored_key
                and dst not in self.ostore_blk):
            # A COMPONENT STORE is a store to the output too (`_second_store`
            # keys it `("out", name)`): `ox_e.frag`'s `o.y = a.x; o = a * 2.0;`
            # opens a block at the whole store, the MUL in the block before
            self.ostore_blk[dst] = self.stored_key[_okey]
        if (self.ostore_blk.get(dst) == self._bkey()
                and ENV.get("G2S_SAMEBLKFLUSH")):
            _n0 = len(self.lines)
            self._flush()
            self.calls.extend(range(_n0, len(self.lines)))
        elif (dst in self.ostore_blk
              and (len(self.lines) > self._blk_start()
                   or (self.ostore_blk.get(dst) == self._bkey()
                       and not ENV.get("G2S_SAMEBLKFLUSH")))
              and not ENV.get("G2S_NOCOLOURPEND")):
            # ... and IN THE SAME BLOCK the same: the store opens a block
            # and reads its value's temp by name, no self-move (`ox_c`:
            # `o = a; o = a * 2.0;` prints the MUL first, then both stores,
            # and no `MOV.F R0, R0;`)
            # STORED IN AN EARLIER BLOCK: an output is never read, so its
            # store stays pending (notes/65 §4, the vertex outputs'
            # `_pending_output`), and this store opens a block of its own,
            # reading its value's temp by NAME -- no self-move.  `cr_m.frag`'s
            # second `c = ..`: the compiler's block 2 holds the colour store
            # alone, its source a leaf (`tools/gsum.py`).
            self._open_block()
            _opened = True
        self.ostore_blk[dst] = self._bkey()
        if not ENV.get("G2S_NOCOLCOMPPEND"):
            self.stored_key[_okey] = self._bkey()
        return _opened

    def _materialise_modified(self, val, src):
        """(the source, the line the statement's tie starts at or None).

        The negate's carrier is this statement's (notes/56: carrier, store
        and self-move share one `node[36]`).  A MODIFIED value is
        materialised into a full-mask register first (notes/47), so the store
        is really two lines plus the self-move.  ONE STATEMENT, ONE
        `node[36]` (notes/56): the compiler's stamps for `fr_mrt.frag` give
        the carrier, the store and its self-move all seq 3 and the other
        colour store seq 1, and pass 1 lists the three first.  So the three
        lines are one tie group, and the order is the scheduler's.
        `G2S_NOMATSTORE=1` restores the refusal."""
        _mat_at = None
        if (val in self.negated and self.defblk.get(val) == self.blk_no
                and self.defline.get(val) is not None):
            _mat_at = self.defline[val]
        if _modified(src):
            if ENV.get("G2S_NOMATSTORE"):
                raise NotEstablished(
                    "a modified value stored to an output: the carrier MOV "
                    "is established (notes/47) but the scheduler's order for "
                    "it among the other stores is not (notes/31)")
            _mat_at = len(self.lines)
            _mat = self._fresh()
            self.lines.append("MOV.F %s, %s;" % (_mat, src))
            src = _mat
        return src, _mat_at

    def _store_colour(self, val, src, dst):
        _opened = self._close_colour_block(dst)
        self._refuse_interleave()
        if _opened:
            # the value was made before the cut: a local's read is its
            # NAME there, as for a pending vertex output (`_pending_output`)
            if val in self.load_of:
                src = self.load_of[val][1]
            _mov, _n, _ds = self._value_mov(val)
            _sw = self.comps.get(val)
            if _sw is not None and not src.startswith("{"):
                src += _swizzle_suffix(_sw, _n)
            if _modified(src):
                raise NotEstablished(
                    "a modified value stored to a pending colour output: "
                    "where its carrier falls against the cut is not measured")
            self.lines.append("%s %s%s, %s;" % (_mov, dst, _ds, src))
            return
        src, _mat_at = self._materialise_modified(val, src)
        _mov, _n, _ds = self._value_mov(val)
        _sw = self.comps.get(val)
        if _sw is not None and not src.startswith("{"):
            src += _swizzle_suffix(_sw, _n)
        _lv = self.load_of.get(val)
        if (_lv is not None and _sw is None
                and not ENV.get("G2S_NOSTOREDMASK")):
            # a whole local's read writes the components the local ever
            # stores (`_stored_mask`)
            _ds = self._stored_mask(_lv[0]) or _ds
        _cp = self.cpair.get(_lv[0]) if _lv is not None else None
        if (_cp is not None and _cp[0] == self._bkey() and _lv[1] == src
                and val in self.lsplit and _mat_at is None
                and not ENV.get("G2S_NOMERGEFWD")):
            # THE MERGE IS THE STORED VALUE (notes/85, `_merge_forward`): the
            # output reads the merge node, no self-move (a local read), and
            # the local's own store follows it (`pb_c.frag`: `MOV.F
            # result_color0, R0;` then `MOV.F R1, R0;`, all seq 18).
            _X = self._merge_forward(_lv[0])
            self.lines.append("%s %s%s, %s;" % (_mov, dst, _ds, _X))
            self.merge_grp[_X].append(len(self.lines) - 1)
            self._flush(only=src)
            return
        self.lines.append("%s %s%s, %s;" % (_mov, dst, _ds, src))
        if (not ENV.get("G2S_NOCOLVALSEQ") and _mat_at is None
                and self.values.get(val) == src and _is_placeholder(src)
                and self.defline.get(val) is not None
                and self.defblk.get(val) == self.blk_no
                and val not in self.load_of and val not in self.fwd_of):
            # THE COLOUR STORE IS ITS VALUE'S STATEMENT: the output's MOV
            # carries the value's `node[36]` (`tools/nodedump.py`:
            # `f03_tex.frag` the TEX's 9, `int_iadd.frag` the I2F's 4,
            # `ld_rgba32f.frag` the LOADIM's 4 -- where the load's own dead
            # temp, also seq 4, is listed after the output's MOV and prints
            # after it, notes/111)
            _tg = _sched.Tie([len(self.lines) - 1])
            _tg.seq = self.defline[val]
            self.ties.append(_tg)
        _fl = self.fwd_line.get(val)
        if (_fl is not None and _mat_at is None
                and (not self.cuts or self.cuts[-1] <= _fl)
                and not ENV.get("G2S_NOCOLFWDSEQ")):
            # A READ FORWARDED FROM A LOCAL'S STORE is that store's node,
            # and the colour store made from it carries its `node[36]`
            # (`_tie_to_forwarded_store`); pass 1 then lists the output's
            # store first (`cr_i.frag`: `MOV.F result_color0, R0;` before the
            # local's `MOV.F R1, R0;`, as `pb_c.frag`'s merge)
            self._tie_to_forwarded_store(_fl)
        self._colour_self_move(val, src, _mov, _ds)
        if _mat_at is not None:
            self.ties.append(list(range(_mat_at, len(self.lines))))

    def _colour_self_move(self, val, src, _mov, _ds):
        """A COLOUR STORE FROM A REGISTER IS FOLLOWED BY A SELF-MOVE.

        `MOV.F R0, R0;` appears after every whole-vector colour store whose
        source is a register -- `f03_tex.frag`, `fr_mrt.frag`,
        `int_iadd.frag` -- and never after one whose source is an attribute
        or a constant.  PROVENANCE, MEASURED (notes/44): from the fold dump,
        `fr_in.frag` (`c = v`, an attribute) builds ONE 0x47 + ONE 0x3a, and
        `int_iadd.frag` (`c = <computed>`) TWO 0x47 + TWO 0x3a, both MOVs
        taking the same source -- so the second assignment is in the IR, not
        an artefact of the printer, and it appears exactly when the stored
        value lives in a register.

        THE TEMP'S OWN STORE RENAMES, as the vertex outputs' does (notes/74
        §2, notes/84): the instruction writes a NEW lowering vreg and the
        self-move copies it into the statement temp's name.  They share a
        register when they can, but not always: `lm_icb.frag` prints `I2F.U
        R0, R4; MOV.F result_color0, R0; MOV.F R5, R0;` -- the name, live
        out of the block, meets the dead element temps (R0..R3) that pass 1
        lists after it."""
        _sb = self.values.get(val)
        if ENV.get("G2S_COLDBG"):                          # diagnosis only
            import sys as _sys
            _sys.stderr.write(
                "COLSELF val=%s src=%s sb=%s splat=%s local=%s load=%s "
                "lname=%s defline=%s defblk=%s blk=%s arm=%s swz=%s "
                "queued=%s\n"
                % (val, src, _sb, val in self.splats,
                   _sb in self.local_reg.values(), val in self.load_of,
                   val in self.lname, self.defline.get(val),
                   self.defblk.get(val), self.blk_no, val in self.arm_names,
                   self._swizzled_local_read(val),
                   self._temp_store_queued(src)))
        if (src.startswith("#") and val not in self.splats
                and src == _sb and _is_placeholder(_sb)
                and _sb not in self.local_reg.values()
                and val not in self.load_of and val not in self.lname
                and self.defline.get(val) is not None
                and self.defblk.get(val) == self.blk_no
                and not ENV.get("G2S_NOCOLFLUSH")):
            self.stmtpos[_sb] = self.defline[val]
            self.flush_q.append((_sb, _mov, _ds, None, self.defline[val]))
            self._flush(only=_sb)
        elif (src.startswith("#") and val not in self.splats
              and not self._swizzled_local_read(val)
              # a value FORWARDED from a local's store that opened this
              # block was made before the cut: it is its temp's NAME here,
              # no self-move (`ox_b.frag`: `t.x = 0.0; t = a * 2.0; o = t;`
              # prints `MOV.F result_color0, R1;` alone, `t`'s store after)
              and not (val in self.fwd_of
                       and self.defline.get(self.fwd_of[val]) is not None
                       and self.defline[self.fwd_of[val]] < self._blk_start()
                       and not ENV.get("G2S_FWDNAMESELF"))
              and not (ENV.get("G2S_NAMESELFMOVE") is None
                       and val in self.load_of
                       and src == self.load_of[val][1])
              # A SELECT'S RESULT IS NO TEMP OF THIS STATEMENT: the ARMS
              # store it, so there is nothing to rename -- the same reading
              # `_is_statement_temp_store` makes for a local store
              # (`mb_n24.vert`).  `post_sky_dlss_mask.frag` stores the
              # `(b) ? 1.0 : 0.0` of an IF/ELSE to a colour output and the
              # compiler prints the store alone.  `G2S_ARMSELFMOVE=1`
              # restores the line.
              and not (val in self.arm_names
                       and not ENV.get("G2S_ARMSELFMOVE"))
              and not self._temp_store_queued(src)):
            self.lines.append("%s %s%s, %s;" % (_mov, src, _ds, src))

    def _temp_store_queued(self, src):
        """Does the temp `src` already have its own store queued -- the
        local's store it was forwarded from queued it (`_store_in_block`)?
        A temp has ONE store: `cr_i.frag`'s `u_xlat0 = a * 2.0; c = u_xlat0;`
        prints `MOV.F result_color0, R0; MOV.F R1, R0; MOV.F R0, R0;`, one
        self-move, which is the flush's.  `G2S_COLDUPFLUSH=1` restores the
        second."""
        if ENV.get("G2S_COLDUPFLUSH"):
            return False
        return any(_q[0] == src for _q in self.flush_q)

    # -- gl_Position

    def _store_geometry_position(self, ptr, val, const, src, comp):
        """THE GEOMETRY STAGE WRITES A SHADOW (notes/65).

        The front end's pass `f_7100f7b230` gives every output whose binding
        record has flag 0x20 and not 0x2000 -- the geometry stage's -- a
        shadow variable (`f_7100f7b3a0`, `sym[96]`), and `f_7100f7af40`
        rewrites each assignment to it: the destination becomes the shadow and
        a copy `output = shadow` is appended (0xf7b0f0..0xf7b1bc); the
        EmitVertex call takes the shadow (0xf7afcc).  The store arrives
        already split into one assignment per component (the reader's element
        loop), so `gl_Position = v` is
            S.c = v.c;  gl_Position.c = S.c;     for c = x..w
        (measured: `g2s_trace_irtree`, and `g2s_trace_irnew`'s frames name
        f_7100f7af40 as the copies' maker).

        S is loaded WHOLE by the EmitVertex call, so it is a materialised
        local (notes/53 §8): each `S.c = ...` is the pair `MOV S.<others>, S;
        MOV S.c, <v.c>`, a component other than 0 gathered into a `.x` lane
        first.  Each `gl_Position.c = S.c` is the single-component position
        store (notes/31): `.x` straight from the source, `.y..w` through a
        scratch `.x`.  Its READ of S: for `.x` the name was stored in the
        same block, so the DAG takes the stored value (`MOV result.position.x,
        R1` reads the load, measured with the node dump); `.y..w` open a block
        of their own (the walker's pending-store test, py/sched.py), so they
        read S at block entry (`MOV R1.x, R2.y`)."""
        if comp != -1 or self.model_gs_pos is not None:
            raise NotEstablished(
                "a geometry-stage gl_Position store other than one whole "
                "store: the shadow's block entry reads are measured for that "
                "shape only")
        if not self.use_regalloc or ENV.get("G2S_NOLOCALREG"):
            raise NotEstablished(
                "the geometry stage's gl_Position shadow is a materialised "
                "local, which needs the transcribed allocator")
        if const is not None or src.startswith(("-", "|", "{")):
            raise NotEstablished(
                "a geometry-stage gl_Position store of a constant or modified "
                "value: its shadow store is not measured")
        _cs = self.comps.get(val, _IDENTITY)
        _S = self._local_register(("gs-shadow", ptr))
        self.local_seen = True
        lines = self.lines
        for _c in range(4):
            _sc = _cs[_c]
            if _sc != 0:
                _g = self._fresh(is_wide=True)
                lines.append(_emit("MOV.F", "%s.x" % _g, _swizzle(src, _sc)))
                _o = "%s.x" % _g
            else:
                _o = src
            lines.append(_emit("MOV.F", "%s.%s" % (_S, _COMPONENTS[_c]), _o))
            self.passthru.append(len(lines))
            lines.append(_emit("MOV.F", "%s.%s" % (_S, "".join(
                _COMPONENTS[k] for k in range(4) if k != _c)), _S))
            if _c == 0:
                lines.append("MOV.F result.position.x, %s;" % _o)
            else:
                _scr = self._fresh()
                lines.append("MOV.F %s.x, %s.%s;"
                             % (_scr, _S, _COMPONENTS[_c]))
                lines.append("MOV.F result.position.%s, %s.x;"
                             % (_COMPONENTS[_c], _scr))
        self.model_gs_pos = _S
        self.scalarised = True

    def _store_position(self, val, const, src, comp):
        _ppend = comp < 0 and _POSITION in self.stored_key
        if (_ppend and val in self.load_of and const is None
                and self.comps.get(val, _IDENTITY) == _IDENTITY):
            # `gl_Position` STORED BEFORE, never read: the whole store opens
            # a block at itself (the walker's pending test, notes/65 §4), so
            # the local it loads is read by NAME at that block's entry, not
            # forwarded from this block's stores (`mb_n16.vert`: `u_xlat0.x =
            # dot(..); gl_Position = u_xlat0;` after the four component
            # stores prints `MOV.F result.position.x, R2;`, R2 being
            # `u_xlat0`).
            src = self.load_of[val][1]
            self.head_src.pop(val, None)
            self.lsplit.pop(val, None)
        if _ppend and self.lines and self.cuts[-1:] != [len(self.lines)]:
            # THE BLOCK OPENS AT THE STORE, explicitly: the scheduler's own
            # pending test runs per control-flow segment and forgets a store
            # made before an IF (`mb_n24.vert`: `u_xlat18 = (b) ? -1.0 : 1.0;`
            # after the ENDIF, then `gl_Position = u_xlat0` -- the store
            # prints first, `result.position.x` in the next block).
            self._open_block()
        if comp < 0:
            self.stored_key[_POSITION] = self._bkey()
            more = self._position_whole(val, const, src)
        else:
            more = self._position_component(val, const, src, comp)
        _ptie = (self._position_node_seq(val, more[0])
                 if comp < 0 and const is None and not _ppend else None)
        if _ptie is not None:
            _tg = _sched.Tie()
            _tg.seq = _ptie
            _tg.append(len(self.lines))
            self.ties.append(_tg)
        self._emit_position_lines(more, comp)
        if self.pflush is not None:
            _pnm, _pfrom = self.pflush
            self.pflush = None
            # the name is the temp's statement record: it walks where the
            # statement stood, ahead of the lowering vreg (notes/68 §4 -- the
            # compiler numbers it 1, the DP4 5, in `pc_e.vert`)
            self.stmtpos[_pnm] = _pfrom
            self.flush_q.append((_pnm, "MOV.F", ".x", None, _pfrom))
            self._flush(only=_pnm)
        self.scalarised = True

    def _emit_position_lines(self, more, comp):
        """The block ends after `.x`: the pending gl_Position store opens the
        next one.  So every temp's store still pending is flushed THERE, in
        `.x`'s block, not at the end of the body.  First read on a call's
        value -- `.x` reads it forwarded (the return name) and the temp is
        flushed before `.y` reads it by name (`cf_call.vert`: `MOV.F
        result.position.x, R1; MOV.F R0, R1; MOV.F R0.x, R0.y;`) -- and it is
        the same close of the block for any temp: `bc_sbo.vert`'s bitcast
        temp is flushed between the ADD and the I2F (notes/72).  The flushed
        store is a line of THAT block, so the boundary the walker opens for
        `.y` falls after it: an explicit cut, where the scheduler's own rule
        would put the boundary right after the `.x` store (py/sched.py)."""
        if self.flush_q and comp is not None and comp < 0:
            self.lines.append(more[0])
            _n0 = len(self.lines)
            self._flush()
            if len(self.lines) > _n0 and len(more) > 1:
                self.cuts.append(len(self.lines))
            self._extend_position(more[1:], comp,
                                  start=len(self.lines))
        else:
            self._extend_position(more, comp)

    def _extend_position(self, more, comp, start=None):
        """ONE ELEMENT PER BLOCK.  `_position_whole`'s own reading -- "each
        element's statement is in a block of its own and makes its own
        scratch node" (notes/82) -- was modelled by the separate scratch
        registers alone; the BOUNDARY was only opened after `.x`, and then
        only when a temp had to be flushed there.  The compiler opens one
        at every element: `ui_depth_alpha_v.vert`, `tools/gsum.py`, blocks
        9, 10, 11 and 12 carry `result.position.x`, `.y`, `.z` and `.w`,
        one apiece, with each element's scratch move beside its own store.
        `G2S_NOPOSELEMBLK=1` keeps them in one block."""
        if comp is None or comp >= 0 or ENV.get("G2S_NOPOSELEMBLK"):
            self.lines.extend(more)
            return
        _start = start      # where the current element's statement begins
        for _l in more:
            _isp = _l.startswith("MOV.F result.position.")
            if _isp and not _l.startswith("MOV.F result.position.x,"):
                # ... at its FIRST line, which is the scratch move, or the
                # re-load ahead of it when the value is a block load
                # (`pl_e.vert`: `LDC.F32X2 R0.y, buf0[16];` opens `.y`'s
                # block, not the `MOV.F R0.x, R0.y;` after it)
                _at = _start if _start is not None else len(self.lines)
                if _at > 0 and self.cuts[-1:] != [_at]:
                    self.cuts.append(_at)
            self.lines.append(_l)
            if _isp:
                _start = len(self.lines)

    def _position_whole(self, val, const, src):
        """`gl_Position = v`: one element per block.

        A MODIFIED value is MATERIALISED first.  notes/47: a stored value
        that is a plain (possibly swizzled) reference to an addressable
        operand is stored straight from it, and anything else goes into a
        full-mask register -- the assignment takes operand-form variant 0xac
        instead of 0xab for it.  A negate has no instruction of its own, so
        the instruction that puts it in the register is a MOV carrying the
        modifier, which is `MOV.F R0, -vertex.attrib[0];` in `un_neg.vert`.
        """
        cs = self.comps.get(val, _IDENTITY)
        if _modified(src):
            if cs != _IDENTITY:
                raise NotEstablished(
                    "a modified value stored through a swizzle: the carrier "
                    "MOV's own swizzle has not been measured")
            carrier = self._fresh()
            self.lines.append("MOV.F %s, %s;" % (carrier, src))
            src = carrier
        scratch = self._scratch_for(src)
        # ONE SCRATCH PER ELEMENT: each `.y..w` is a statement in a block of
        # its own with its own scratch node -- the compiler's vregs 6, 7, 8 in
        # `pt_h.vert`, which a single shared placeholder made interfere across
        # the blocks (notes/82).  `G2S_ONESCRATCH=1` restores the shared one.
        _scr3 = scratch
        if not ENV.get("G2S_ONESCRATCH"):
            _scr3 = [scratch, self._fresh(), self._fresh()]
        more, _n = _position_store(src, const is not None, cs, _scr3,
                                   self.head_src.get(val))
        _s3 = _scr3 if isinstance(_scr3, list) else [_scr3] * 3
        more = self._position_from_local(val, const, src, cs, more, _s3)
        return self._position_from_block_load(val, const, src, cs, more, _s3)

    def _position_from_local(self, val, const, src, cs, more, _s3):
        """The element blocks when the value is a local's read."""
        _lw = self.load_of.get(val)
        if (_lw is not None and const is None and not _modified(src)
                and _lw[1] != src and cs == _IDENTITY):
            # A WHOLE LOAD OF A MATERIALISED LOCAL stored in this block: `.x`
            # takes the stored value, `.y..w` open blocks of their own and
            # read the NAME there (`ce_head.vert`: `MOV.F result.position.x,
            # R18;` then `MOV.F R0.x, R17.y;` -- R17 being `u_xlat2`).
            more = [more[0]] + self._position_elements(
                _s3, lambda _c: "%s.%s" % (_lw[1], _c))
        _ln = self.lname.get(val)
        if (_ln is not None and const is None and not _modified(src)
                and len(set(cs)) == 1 and val not in self.head_src):
            # A SPLAT OF A LOCAL'S COMPONENT: `gl_Position = vec4(t.y)` is
            # `gl_Position = t.yyyy`, the load being the name itself, not a
            # temp.  `.x` shares the store's block and takes the stored
            # value; `.y..w` each open a block and read the NAME at its
            # entry, one scratch copy apiece (`lv_wc.vert`: `MOV.F
            # result.position.x, R0.y;` then three `MOV.F R0.x, R1.y;` -- the
            # same reading as the geometry shadow's, notes/65).
            # (component 0 prints bare, the printer's rule: `sp_f.vert`'s
            # `vec4(t.x)` reads `MOV.F R0.x, R1;`)
            _nm = _swizzle(_ln[0], _ln[1])
            more = [more[0]] + self._position_elements(_s3, lambda _c: _nm)
        _shl = self._splat_of_whole_load(val)
        if (_shl is not None and const is None and not _modified(src)
                and len(set(cs)) == 1 and val not in self.head_src
                and not ENV.get("G2S_NOSHUFSPLATPOS")):
            # THE SAME SPLAT THROUGH A SHUFFLE of the whole load
            # (`gl_Position = t.yyyy`): the swizzle of the name is the same
            # read, so `.y..w` each open a block, read the NAME, one scratch
            # copy apiece -- `sp_b.vert` prints three `MOV.F R0.x, R1.y;`,
            # `sp_c.vert` (the lane stored in an earlier block) three
            # `MOV.F R0.x, R1.z;`, `sb_c.vert` three reads of `u_xlat3`'s
            # register, not of the dot's temp
            _nm = _swizzle(_shl, cs[0])
            more = [more[0]] + self._position_elements(_s3, lambda _c: _nm)
        _sp0 =self.lsplit.get(val, {}).get(0)
        if (_sp0 is not None and const is None and cs == _IDENTITY
                and more[0].startswith("MOV.F result.position.x, ")):
            # A WHOLE LOAD OF A LOCAL WHOSE `.x` THIS BLOCK STORED: `.x`
            # shares the block and takes the stored value, as a component
            # read does (notes/69), while `.y..w` open blocks and read the
            # name.  `pt_a.vert` (`b.x = dot(a, b); gl_Position = b;`) prints
            # `MOV.F result.position.x, R0;` -- the DP4's register, not b's.
            more[0] = "MOV.F result.position.x, %s;" % _swizzle(
                _sp0[0], _sp0[1])
        return more

    def _position_elements(self, _s3, source_of):
        """`.y..w`, each through its own scratch from `source_of(letter)`."""
        out = []
        for _i, _c in enumerate(_COMPONENTS[1:], 1):
            out.append("MOV.F %s.x, %s;" % (_s3[_i - 1], source_of(_c)))
            out.append("MOV.F result.position.%s, %s.x;"
                       % (_c, _s3[_i - 1]))
        return out

    def _position_from_block_load(self, val, const, src, cs, more, _s3):
        """A BLOCK LOAD STORED WHOLE: `.y..w` are blocks of their own, and
        each substitutes the load again -- an LDC per element block
        (`pl_e.vert`), its mask the one component read (`_narrow_loads`)."""
        if not (src in self.ldc_vreg and const is None and cs == _IDENTITY
                and val not in self.head_src
                and more[0] == "MOV.F result.position.x, %s;" % src):
            return more
        _b, _mn, _ls, _nm, _of = self.ldc_vreg[src]
        more = [more[0]]
        for _i, _c in enumerate(_COMPONENTS[1:], 1):
            _new = self._fresh()
            self.ldc_vreg[_new] = (_b, _mn, _ls, _nm, _of)
            more.append("%s %s%s, %s[%d];" % (_mn, _new, _ls, _nm, _of))
            more.append("MOV.F %s.x, %s.%s;" % (_s3[_i - 1], _new, _c))
            more.append("MOV.F result.position.%s, %s.x;"
                        % (_c, _s3[_i - 1]))
        return more

    def _position_component(self, val, const, src, comp):
        """A store through a chain that already names ONE component is one
        instruction: `w1_mov.vert` writes `gl_Position.x` and gets `MOV.F
        result.position.x, vertex.attrib[0];`.  THE SAME RULE AS THE
        WHOLE-VECTOR STORE, keyed on the DESTINATION component: `.x` is
        written straight from the source, and `.y`, `.z`, `.w` go through the
        scratch register (`w2_mov.vert`).

        A COMPUTED VALUE IS THE OUTPUT-CHAIN STORE'S CASE (notes/74): the
        position component store is the same assignment statement as
        `result_color0.w = ...`.
          * A second store to `gl_Position` in a block opens a block AT the
            store; the value's temp stays behind (`_second_store`, notes/55
            §8).  The compiler's blocks for `chr_eye`: each `gl_Position.c =
            dot(..)` store sits in the block after its DP4.
          * `.x` from a scalar temp made in the block is followed by the
            temp's flush, `MOV.F R0.x, R0;` (`pc_e.vert`, as `sc_dot2.frag`,
            notes/66 §3).
          * `.y..w` from a whole scalar register go straight to the lane, no
            scratch (`pc_d.vert`: `DP4.F32 R0.x, ..; MOV.F
            result.position.y, R0.x;`, as `sc_mulw.frag`, notes/70)."""
        self._second_store(_POSITION)
        if const is None and self._stale_ldc(val):
            self._reload(val)
            src = self.values.get(val)
        sc = _swizzle(src, self.comps.get(val, _IDENTITY)[0])
        _stemp = self._is_own_scalar(val, src)
        if comp == 0:
            if (_stemp and self.defblk.get(val) == self.blk_no
                    and self.defline.get(val) is not None):
                # the temp's own store, flushed right after: the instruction
                # writes a NEW lowering vreg that the store reads, and the
                # flush copies it into the temp's name (`_flush`).
                # `chr_eye`'s block 28: DP4 row 515 -> position.x, and the
                # flush row 514 -- `MOV.F R2.x, R3;`.
                self.pflush = (src, self.defline[val])
            return ["MOV.F result.position.x, %s;" % sc]
        if _stemp:
            return ["MOV.F result.position.%s, %s.x;"
                    % (_COMPONENTS[comp], src)]
        if src.startswith("{") and val not in self.comps \
                and not ENV.get("G2S_POSCONSCRATCH"):
            # A SCALAR CONSTANT goes straight to the lane here too, as it
            # does into any other output (`sc_cstore.frag`, above): no
            # scratch, its slot-0 selector printed.  `post_popup_face_v.vert`
            # prints `MOV.F result.position.z, {0, 0, 0, 0}.x;`.
            return ["MOV.F result.position.%s, %s.x;"
                    % (_COMPONENTS[comp], src)]
        scratch = self._scratch_for(src)
        return ["MOV.F %s.x, %s;" % (scratch, sc),
                "MOV.F result.position.%s, %s.x;"
                % (_COMPONENTS[comp], scratch)]

    def _is_own_scalar(self, val, src):
        """A scalar the value's own instruction wrote: not a load, a shuffle
        or an extract, which name a component of something else (a DP4's
        result carries comps (0,0,0,0))."""
        _vi = self.module.result_insn.get(val)
        return (_is_placeholder(src)
                and set(self.comps.get(val, (0,))) == {0}
                and _vi_is_scalar(self.module, val)
                and _vi is not None
                and self._value_opcode(val) not in _NAMING)

    # -- other outputs

    def _store_output_component(self, val, const, src, _oc):
        """A STORE THROUGH A CHAIN INTO ANY OTHER LOCATION OUTPUT.

        `st3_dstswz.vert` writes `v.zw = a0.xy` and glslang scalarises it in
        the SPIR-V already -- two access chains with constant indices -- so
        each one is the same shape the `gl_Position` component store has,
        with the destination named out of the binding namespace instead:

            MOV.F R0.x, vertex.attrib[0];
            MOV.F result.attrib[0].z, R0.x;

        and the first line is dropped when the destination component is `.x`
        AND the source reads component 0, which is the same identity test as
        everywhere else (notes/41).

        A SECOND STORE TO THE OUTPUT in its block opens a block at the store
        (notes/55 §8, notes/65 §4), and the old block's pending temp stores
        are flushed first (`_second_store`).  A local the statement loads is
        then read at the new block's entry by its NAME, not the forwarded
        value (`sc_ldw.frag`: `o.w = x1` reads x1's register, vreg 3 in the
        node dump, after the flush of the MUL's temp)."""
        _obase, _ocomp = _oc
        _osec = self._second_store(("out", _obase))
        if const is None and self._stale_ldc(val):
            # the load again, in the block this store is in (`_reload`;
            # `pl_a.vert`'s `o.y`: `LDC.F32X2 R0.y`)
            self._reload(val)
            src = self.values.get(val)
        if _osec and val in self.load_of:
            src = self.load_of[val][1]
        elif _osec and const is None and self._name_read(val) is not None:
            # the new block reads the local by NAME, a component too
            # (`pk_e.vert`: `o.w = t.y` -> `MOV.F R0.x, R5.y;`, R5 = t;
            # notes/81 §2)
            _nr = self._name_read(val)
            src = _nr[0]
            self.comps[val] = (_nr[1],) * 4
            self._touch_by_name(_nr[0])
        self._refuse_interleave()
        lane = "%s.%s" % (_obase, _COMPONENTS[_ocomp])
        _mv = self._lane_mov(val)
        if src.startswith("{") and val not in self.comps:
            # A SCALAR CONSTANT goes straight to its component, its slot-0
            # selector printed wherever it is not the destination's own lane
            # (`sc_cstore.frag`: `MOV.F result_color0.y, {1, 0, 0, 0}.x;`) --
            # the gather through a scratch `.x` is an addressable operand's
            # (st3_dstswz), not a constant's.
            self.lines.append("%s %s, %s%s;"
                              % (_mv, lane, src, "" if _ocomp == 0 else ".x"))
            return
        _crd = self._con_read(val) if const is None else None
        if _crd is not None:
            # A COMPONENT OF A CONSTRUCT MADE IN THIS BLOCK: its source, a
            # scalar in `.x`, straight to the lane (`chr_hair_2ae6bf65`:
            # `MOV.F result.attrib[5].x, R4;`, R4 what `MOV.F R8.x, R4;`
            # wrote; notes/81 §3)
            # Into ANOTHER lane the read stays a component select of the
            # construct and gathers through a scratch `.x` as any selected
            # operand does (`cr_f.frag`: `c.y = u_xlat0.y` prints `MOV.F
            # R5.x, R1; MOV.F result_color0.y, R5.x;`).
            _b = _crd.split(".")[0]
            if _ocomp == 0:
                self.lines.append("%s %s, %s;" % (_mv, lane, _b))
            elif ENV.get("G2S_CONLANEDIRECT"):
                self.lines.append("%s %s, %s.x;" % (_mv, lane, _b))
            else:
                scratch = self._scratch_for(_b)
                self.lines.append("%s %s.x, %s;" % (_mv, scratch, _b))
                self.lines.append("%s %s, %s.x;" % (_mv, lane, scratch))
            return
        if _ocomp == 0:
            self._output_lane_x(val, src, _obase, _mv, opened=_osec)
        elif self._is_whole_scalar_register(val, src):
            # A WHOLE SCALAR VALUE IN A REGISTER goes straight to the lane
            # (notes/70): the store reads the temp's NAME through a component
            # select, so there is no scratch and -- the value having one use,
            # its own store -- no flush either (`nodedump` on
            # `sc_mulw.frag`: the MUL is the name's vreg).  `sc_mulw.frag`
            # prints `MUL.F32 R0.x, ...; MOV.F result_color0.w, R0.x;` and
            # `sc_selw.frag` the same after its ENDIF.
            self.lines.append("%s %s, %s.x;" % (_mv, lane, src))
        else:
            _sc = _swizzle(src, self.comps.get(val, _IDENTITY)[0])
            scratch = self._scratch_for(src)
            self.lines.append("%s %s.x, %s;" % (_mv, scratch, _sc))
            self.lines.append("%s %s, %s.x;" % (_mv, lane, scratch))

    def _touch_by_name(self, name):
        """A READ OF A LOCAL'S NAME in a later block restarts its NAME RECORD
        (notes/65 §4: an access from another block starts `[24]`/`[32]`/
        `[40]` afresh), so its store is no longer pending and the next store
        of it does not open a block.  MEASURED with `g2s_trace_blkrec` on
        `cr_l.frag`: at `u_xlat0 = vec4(..)`, after `c.xyz = u_xlat0.yzw`,
        the record shows `[24]` = the block `c.z`'s store opened, `[32]` set
        (the read) and `[40]` clear -- no block opens, and the construct's
        writes, the store and the temp's flush are one statement (seq 27).
        `G2S_NONAMETOUCH=1` leaves the store pending."""
        if ENV.get("G2S_NONAMETOUCH"):
            return
        for _var, _r in self.local_reg.items():
            if _r == name and self.lpend.get(_var, self._bkey()) \
                    != self._bkey():
                del self.lpend[_var]

    def _is_whole_scalar_register(self, val, src):
        """(a load only as a whole scalar -- a local's NAME, `sc_ldw.frag` --
        not a component of a block vector, which gathers: `pl_b.vert`) (a
        DP4's result carries comps (0, 0, 0, 0): the test is a scalar the
        value's own instruction wrote, as for the position lanes, notes/74
        §3 -- `mb_n30.vert`'s `MOV.F result.attrib[4].w, R12.x;`)"""
        _vi = self.module.result_insn.get(val)
        # A SCALAR INTERFACE OPERAND IS A WHOLE SCALAR VALUE TOO: the rule is
        # about the value, not about where it lives, so a `float` input goes
        # straight to the lane like a register does -- `post_popup_face_v`'s
        # `in_TEXCOORD0` (an `OpTypeFloat` Input) prints `MOV.F
        # result.attrib[1].z, vertex.attrib[1].x;`, while a COMPONENT of a
        # vector input still gathers through the scratch (`st3_dstswz.vert`,
        # whose `a0.xy` is a `vec4`'s).  `G2S_NOSCALARIFACE=1` keeps the
        # register-only test.
        _iface = (not _is_placeholder(src) and not src.startswith("{")
                  and _lex.swizzle_suffix(src, 1, 4) is None
                  and not ENV.get("G2S_NOSCALARIFACE"))
        return ((_is_placeholder(src) or _iface)
                and set(self.comps.get(val, (0,))) == {0}
                and _vi_is_scalar(self.module, val)
                and _vi is not None
                and _vi.opcode not in _COMPONENT_NAMING
                and (_vi.opcode != Op.OpLoad or val not in self.comps))

    def _output_lane_x(self, val, src, _obase, _mv="MOV.F", opened=False):
        """THE DESTINATION decides (notes/69): `.x` is written straight from
        any source component (`lv_v2a.frag`: `MOV.F result_color0.x,
        fragment.attrib[0].z;`), `.y..w` through a scratch `.x` -- the
        position store's rule.  `_mv` is the lane's MOV (`_lane_mov`).

        `opened`: this store OPENED A BLOCK (`_second_store`), so the scalar
        temp's own store belongs to the block before it -- flushed there,
        when the name store is the value's only use, so the instruction
        writes the name itself -- and the store reads the NAME in the new
        block.  `uo_b.frag` (`o1.w` stored earlier, then `o1.x = a + b`):
        the ADD is stored straight by a 0x3a (seq 24) and the output's store
        (seq 25) reads a 0x2b register read of the name, `MOV.U
        result_color1.x, R2;` with no self-move (`g2s_dag`).  `uo_a.frag`,
        whose `o1.x` is the first store to `o1`, keeps the self-move."""
        module = self.module
        _sc = _swizzle(src, self.comps.get(val, _IDENTITY)[0])
        _ld = self.ldc_at.get(val)
        if (_ld is not None and src == _ld[1] and val not in self.comps
                and _vi_is_scalar(module, val)
                and _use_count(module, val) == 1):
            # A LOAD IS NOT A TEMP (notes/67 §1), so a load whose only reader
            # is this store gives the store's carrier MOV a one-use source
            # with no modifier, and the MOV folds into it (f_7100032c30,
            # notes/64 §5): the load writes the output itself.
            # `ld_f1s.frag` prints `LDC.F32 result_color0.x, buf0[16];` and
            # no MOV.
            self.lines[_ld[0]] = self.lines[_ld[0]].replace(
                "%s.x," % _ld[1], "%s.x," % _obase, 1)
            return
        self.lines.append("%s %s.x, %s;" % (_mv, _obase, _sc))
        _vi = module.result_insn.get(val)
        if (_is_placeholder(src) and _vi is not None
                and _vi.opcode not in _NAMING
                and self.defblk.get(val) == self.blk_no
                and _vi.has_result_type
                and _components(module, _vi.result_type) == 1):
            if opened and not ENV.get("G2S_NOOPENEDNAME"):
                # the temp's store is the old block's and folds into the
                # instruction: the register IS the name, nothing to copy
                return
            # A SCALAR TEMP READ WHOLE (notes/66 §3): the value is the named
            # temp's, forwarded inside the block, so it has two uses -- this
            # store and the temp's own store, flushed after it -- and the
            # instruction cannot fold into the temp.  The flush is the
            # self-move: `sc_dot2.frag` prints `MOV.F result_color0.x, R0;
            # MOV.F R0.x, R0;`.  It is `_flush`'s store, as for the position
            # `.x` (notes/74): the instruction writes a new lowering vreg, the
            # name walks at its statement.
            if self.defline.get(val) is not None:
                self.stmtpos[src] = self.defline[val]
                self.flush_q.append((src, _mv, ".x", None,
                                     self.defline[val]))
                self._flush(only=src)
            else:
                self.lines.append("%s %s.x, %s;" % (_mv, src, src))

    def _store_output_whole(self, val, const, src, odst):
        """THE SAME SHAPE AS THE COLOUR STORE, and it needs the same three
        things: the MOV's suffix from the stored value's type, the
        destination's WRITE MASK from the value's component count (notes/41),
        and the self-move when the source is a register.  `wb2_add.vert`
        prints
            MOV.F result.attrib[0].xy, R0;
            MOV.F R0.xy, R0;"""
        self._refuse_interleave()
        _mov, _n, _ds = self._value_mov(val)
        _wsec = False
        if not ENV.get("G2S_NOVOUTPEND"):
            _wsec, src = self._pending_output(val, const, src, odst)
        _lv = self.load_of.get(val)
        _cp = self.cpair.get(_lv[0]) if _lv is not None else None
        if (not _wsec and _cp is not None and _cp[0] == self._bkey()
                and _lv[1] == src and val in self.lsplit
                and val not in self.comps
                and not ENV.get("G2S_NOMERGEFWD")):
            # THE MERGE IS THE STORED VALUE, as for a colour store (notes/85):
            # `mf_a.vert`'s `o = u_xlat0` right after `u_xlat0.w = dot(..)`
            # reads the pair's node, and the local's own store follows it --
            # sharing its register, `MOV.F result.attrib[0], R1; MOV.F R1,
            # R1;` (`monster_001ea2e4`: `MOV.F result.attrib[9], R5; ..
            # MOV.F R13, R5;`)
            _X = self._merge_forward(_lv[0])
            _ds = self._stored_mask(_lv[0]) or _ds
            self.lines.append("%s %s%s, %s;" % (_mov, odst, _ds, _X))
            self.merge_grp[_X].append(len(self.lines) - 1)
            # the local's own store stays QUEUED to the block's close: a
            # later read of a lane the pair did not write reads the name at
            # block entry, ahead of it (`monster_001ea2e4`: `ADD.F32 R3.x,
            # R13.y, R2;` before `MOV.F R13, R5;`)
            if ENV.get("G2S_MERGEFLUSHNOW"):
                self._flush(only=src)
            return
        # A WHOLE READ OF A PARTLY STORED LOCAL writes the components it ever
        # stores (`_stored_mask`, the colour store's rule, notes/87), and
        # when every one of them was stored in this block, from one value,
        # it reads that value (notes/69): `ld_ar.vert`'s `p = u_xlat1`,
        # `u_xlat1` stored only at `.x` just before, prints `MOV.F
        # result.attrib[1].x, R2;` -- R2 the ADD -- and the local's own store
        # after it.
        _lvw = self.load_of.get(val) if not _wsec else None
        _pm = (self._stored_mask(_lvw[0])
               if _lvw is not None and src == _lvw[1]
               and not ENV.get("G2S_NOOUTSTOREDMASK") else None)
        if _pm is not None and _pm != _ds:
            _ds = _pm
            _ls = self.lsplit.get(val, {})
            _lanes = ["xyzw".index(ch) for ch in _pm[1:]]
            if _lanes and all(_c in _ls for _c in _lanes) and len(
                    set(_ls[_c][0] for _c in _lanes)) == 1:
                src = _ls[_lanes[0]][0]
                # each lane the mask writes reads its own stored lane,
                # printed against the width the mask writes (notes/41)
                _sel = [_ls[_lanes[0]][1]] * 4
                for _c in _lanes:
                    _sel[_c] = _ls[_c][1]
                self.comps[val] = tuple(_sel)
                _n = len(_lanes)
                # the store is the stored value's node's: it carries that
                # statement's `node[36]` (`_tie_output_to_read`'s forwarded
                # case), and the local's own store follows it
                _defs = [self.defline[_k] for _k, _v in self.values.items()
                         if _v == src and self.defline.get(_k) is not None]
                if _defs and not ENV.get("G2S_NOFWDSEQ"):
                    _tg = _sched.Tie()
                    _tg.seq = max(_defs)
                    _tg.append(len(self.lines))
                    self.ties.append(_tg)
        # AND THE SOURCE'S SWIZZLE.  `st1_swz.vert` stores `a0.wzyx` and the
        # compiler prints the selector on the source, exactly as an
        # arithmetic operand does (notes/41).
        _sw = self.comps.get(val)
        if (not _wsec and _sw is None
                and (val in self.node_loads or val in self.ldc_at)
                and self._retarget_node(val, odst + _ds,
                                        ldc=True) is not None):
            # the load's node writes the output (core.py `_retarget_node`):
            # `mx_g.vert`'s `o = m[1]` is `MOV.F result.attrib[0], R1;`,
            # `mx_h`'s `o = m1` `LDC.F32X4 result.attrib[0], buf0[16];`
            return
        _crd = (self._con_read(val)
                if (not _wsec and const is None and _n == 1
                    and not ENV.get("G2S_NOWHOLECONREAD")) else None)
        if _crd is not None:
            # A SCALAR LANE OF A CONSTRUCT MADE IN THIS BLOCK reads that
            # lane's source, bare, as the lane-x output store does
            # (`chr_hair_2ae6bf65`, `_store_output_lane`): the slice's
            # `debug_lightprobe_v`'s `vs_TEXCOORD1 = u_xlat0.y` right after
            # `u_xlat0.xy = vec2(a, b)` prints `MOV.F result.attrib[2].x,
            # R4;` -- R4 what `b`'s MOV wrote (`tools/gsum.py`: node 25.11
            # reads 25.7), not the construct's `.y`.
            # `G2S_NOWHOLECONREAD=1` reads the construct's lane.
            src, _sw = _crd.split(".")[0], None
        if _sw is not None and not src.startswith("{"):
            src += _swizzle_suffix(_sw, _n)
        self._tie_output_to_read(val)
        self.lines.append("%s %s%s, %s;" % (_mov, odst, _ds, src))
        # ... but a materialised LOCAL'S NAME is no temp and has no store of
        # its own to flush: `mb_n24.vert`'s `vs_NORMAL0.xyz = u_xlat0.xyz`
        # prints `MOV.F result.attrib[1].xyz, R11;` alone.  (A LOCAL READ --
        # by name or forwarded from its store in this block -- is not this
        # statement's temp: the temp's own store belongs to the local's
        # store, already queued.  `chr_hair_0e0b7460`'s `vs_TANGENT0.xyz =
        # u_xlat8.xyz` after `u_xlat8.xyz = ..*..` has no self-move.)
        if (src.startswith("#") and val not in self.splats and not _wsec
                and self.values.get(val) not in self.local_reg.values()
                and val not in self.load_of and val not in self.lname):
            self._output_self_move(val, src, _mov, _ds, _sw)

    def _pending_output(self, val, const, src, odst):
        """A STORE TO A PENDING OUTPUT OPENS A BLOCK (the walker's test,
        notes/65 §4, as for a component output store): a second store in the
        block, or one to an output stored in an earlier block (an output is
        never read, so it stays pending).  The value's instructions stay in
        the old block with its temps' stores, and the value -- made before
        the cut -- is its temp's NAME, so the store has no self-move.
        `lp_oe.vert` (`o = a + b; t = a * b; o = t + b;`) prints the second
        ADD before the first store and `MOV.F result.attrib[0], R3;` last,
        alone (notes/91)."""
        _wsec = self._second_store(("out", odst))
        if _wsec and const is None and val in self.load_of:
            src = self.load_of[val][1]
        elif _wsec and const is None and self._name_read(val) is not None:
            raise NotEstablished(
                "a local read stored to a pending output: the name read's "
                "shape in the new block is not measured for a whole output "
                "store")
        return _wsec, src

    def _tie_output_to_read(self, val):
        """A NAME READ IS ONE INTERNED EXPRESSION (notes/75 §1): the store of
        a local read earlier in the block is made from that read's node and
        carries ITS `node[36]`.  `mb_n29.vert`'s `vs_TEXCOORD1 = u_xlatu2` is
        seq 225, between the first address MUL that read `u_xlatu2` (222) and
        its LDB (227), and prints right after the MULs.

        ... and a read FORWARDED from the local's store in this block is the
        stored value's node: the store carries that node's `node[36]`, ahead
        of the local's own store (`chr_hair_0e0b7460`: `vs_TANGENT0.xyz =
        u_xlat8.xyz` prints before `MOV.F R9.xyz, R3;`) -- the SAME
        `node[36]` as the value (213 for the MUL, the attrib store and
        `u_xlat8`'s store alike); pass 1's list breaks the tie."""
        _fr = (self._first_reader(self.values.get(val))
               if self.values.get(val) in self.local_reg.values() else None)
        if (_fr is None and val in self.fwd_of
                and self.defline.get(self.fwd_of[val]) is not None
                and not ENV.get("G2S_NOFWDSEQ")):
            _fr = self.defline[self.fwd_of[val]] - 0.5
        if _fr is not None:
            _tg = _sched.Tie()
            _tg.seq = _fr + 0.5
            _tg.append(len(self.lines))
            self.ties.append(_tg)

    def _output_self_move(self, val, src, _mov, _ds, _sw):
        """The temp's own store is `_flush`'s: a NEW lowering vreg for the
        instruction, the name at its statement (notes/74 §2) --
        `mb_n26.vert`'s tangent MUL is vreg 107, its name 63."""
        _sb = self.values.get(val)
        if (_ds == ".x" and _sw is not None and tuple(_sw) == (0, 0, 0, 0)
                and not ENV.get("G2S_SCALARSWFLUSH")):
            # a SCALAR's selector is `.x` in every lane -- no selection: the
            # dot's temp stored to a float output (`sb_c.vert`'s `on =
            # dot(n, u_xlat2.xyz)`) flushes into its own name, a vreg of its
            # own (vr 28 against the DP3's 47)
            _sw = None
        if (not ENV.get("G2S_NOOUTFLUSH")
                and val not in self.load_of and val not in self.lname
                and _is_placeholder(_sb)
                and self.defline.get(val) is not None
                and self.defblk.get(val) == self.blk_no
                and _sw is None):
            self.stmtpos[_sb] = self.defline[val]
            self.flush_q.append((_sb, _mov, _ds, None, self.defline[val]))
            self._flush(only=_sb)
        elif (_is_selected_placeholder(src)
              and not ENV.get("G2S_SELSELFMOVE")):
            # A VALUE READ THROUGH A SELECTOR HAS NO SELF-MOVE: there is no
            # temp to rename, the value IS the name read that way (the same
            # reading as `_store_opening_block`'s `_selected`), and a
            # destination cannot carry a selector anyway -- `ui_basic_v.vert`
            # stored `#122.yzww` to `result.attrib[3].xyz` and we wrote
            # `MOV.F #122.yzww.xyz, #122.yzww;`, which the compiler does not
            # print at all.  `G2S_SELSELFMOVE=1` restores it.
            pass
        elif val in self.arm_names and not ENV.get("G2S_ARMSELFMOVE"):
            # A SELECT'S RESULT IS NO TEMP OF THIS STATEMENT EITHER: the ARMS
            # store it (`_is_statement_temp_store` says the same for a local
            # store, `mb_n24.vert`), so the output store has nothing to
            # rename.  `post_sky_dlss_mask.frag` stores the `(b) ? 1.0 : 0.0`
            # of an IF/ELSE to `result_color1.x` and the compiler prints
            # only the store.  `G2S_ARMSELFMOVE=1` restores the line.
            pass
        else:
            self.lines.append("%s %s%s, %s;" % (_mov, src, _ds, src))
