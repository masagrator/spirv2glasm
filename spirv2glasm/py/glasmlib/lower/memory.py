"""memory.py -- loads, local-memory arrays, variables and access chains.

A load emits nothing when what it reads already has operand text (a local's
register, an input, a component of either); a block load is an `LDC`/`LDB`
line, one per location per block; a dynamically indexed one computes its
byte offset into an address register first.
"""
from spvnames import Op, ExecutionModel, StorageClass, BuiltIn, Decoration
from spvgrammar import STORAGECLASS

import opchain as _opchain
from glasmlib.common import NotEstablished, ENV, ACCESS_CHAINS
from glasmlib.types import _components, _glasm_type_code, \
    _pointee_components, _pointee_code, _signedness
from glasmlib.operands import _scalar_value, _constant_operand, \
    _interface_operand, _per_vertex_operand, _per_vertex_location_operand, \
    _per_vertex_dynamic_location_operand, _block_member_operand
from glasmlib.chains import _buffer_chain, _buffer_chain_via_matrix
from glasmlib.text import _emit, _source, _swizzle, _COMPONENTS
from glasmlib.lower.core import _local_chain
from glasmlib.blocks import _storage_image
from glasmlib import nodes


class MemoryOps(object):

    # -- parameters ----------------------------------------------------------

    def _arm_parameter_load(self, ins):
        """A LOAD OF A PARAMETER is a named temp (`t = v`, the callee's first
        statement in g2s_trace_irtree): its readers in the block take the
        parameter's value, and its own store is flushed at the block's end in
        the entry copy's position (both seq 18 in `0050_cf_call.vert`'s nodes:
        they store the same value)."""
        if ins.opcode != Op.OpLoad or ins.args()[0] not in self.formal:
            return False
        _F = self.formal[ins.args()[0]]
        _pm, _pd = self._parameter_mov(
            ins.args()[0], "a parameter load whose MOV the image does not "
            "give")
        self.values[ins.result] = _F
        _t13 = self._fresh(True)
        self.callnames.add(_t13)
        self.stmtpos[_t13] = len(self.lines)
        self.flush_q.append((_t13, _pm, _pd, self.fgrp.get(_F), None, _F))
        return True

    def _parameter_mov(self, pid, refusal):
        """(MOV, destination suffix) for a parameter's type, or the
        refusal."""
        _pc = _pointee_components(self.module, pid)
        _pm = self._mov_for(_pointee_code(self.module, pid))
        _pd = _opchain.dest_suffix(_pc) if _pc else None
        if _pm is None or _pd is None:
            raise NotEstablished(refusal)
        return _pm, _pd

    # -- loads ---------------------------------------------------------------

    def _arm_load(self, ins):
        if ins.opcode != Op.OpLoad:
            return False
        ptr = ins.args()[0]
        lc = _local_chain(self.module, ptr, self.by_result, self.locals_)
        if lc is not None:
            # which component, and how many stores had been made: a store of
            # it straight back is a self-copy (`_is_self_copy`)
            self.comp_load[ins.result] = (lc, self.store_no)
            self._load_local_component(ins, lc)
            return True
        _lch = self.by_result.get(ptr)
        if (_lch is not None and _lch.opcode in ACCESS_CHAINS
                and _lch.args()[0] in self.lmem_k):
            self._load_lmem_element(ins, _lch)
            return True
        if ptr in self.locals_:
            self.whole_load_at[ins.result] = (ptr, self.store_no)
            self._load_local_whole(ins, ptr)
            return True
        chain = _buffer_chain(self.module, ptr, self.by_result)
        if chain is not None and _buffer_chain_via_matrix(
                self.module, ptr, self.by_result):
            self._load_matrix_column_part(ins, chain)
            return True
        if chain is not None:
            self._load_buffer(ins, chain)
            return True
        self._load_interface(ins, ptr)
        return True

    def _load_local_component(self, ins, lc):
        var, ci = lc
        cell = self.locals_[var].get(ci) if self.locals_[var] else None
        if cell is None:
            raise NotEstablished("a load of a local component never stored to")
        self.values[ins.result], c0 = cell
        if var in self.whole_load:
            # the NAME's own form, for a reader in a later block
            self.lname[ins.result] = cell
            if self.lpend.get(var, self._bkey()) != self._bkey():
                del self.lpend[var]     # a read in a later block
            # a component read in the block that stored it takes the stored
            # value (notes/69)
            # value (notes/69) -- except a plain store into a lane other than
            # x, an insert whose node the read selects from: `0102_lf_b.frag`'s
            # `u_xlat0.z = a.x * a.y; .. u_xlat0.z` reads the store node
            # (`tools/gsum.py`: the MUL's source is node 0.1, mask z), `MUL.F32
            # R1, fragment.attrib[0], R0.z;`
            _cf = self.cfw.get(var, {}).get(ci)
            if (_cf is not None and _cf[0] == self._bkey()
                    and (self.cfw_kind.get((var, ci))
                         not in ("insert", "namecopy")
                         or ENV.get("G2S_INSERTFWD"))):
                self.values[ins.result], c0 = _cf[1], _cf[2]
                _via = self.cfw_via.get((var, ci))
                if _via is not None and _via[0] is _cf:
                    # a merge lane of another local's swizzled component
                    # reads the swizzle's node (stores.py `_merge_lane_via`)
                    self.values[ins.result], c0 = _via[1][1], _via[1][2]
        # A COPY OF ANOTHER LOCAL IS TRANSPARENT, one hop at a time: what
        # this local forwards is another local's register, and the component
        # read follows THAT local's own forward for the same component --
        # the same reading as notes/114 §7 for a lane store, on a read.
        # `vfx_basic_p.frag`'s `u_xlat0 = movcTemp; .. u_xlat0.y * u_xlat0.x`
        # prints `MUL.F32 R2.x, R2, R7;` -- what the two lanes of `movcTemp`
        # were stored from -- where we stopped at `movcTemp` itself.
        # `G2S_NOCFWCHAIN=1` stops at the first hop.
        if not ENV.get("G2S_NOCFWCHAIN"):
            _seen = set()
            while True:
                _v = self.values[ins.result]
                _nx = next((_l for _l, _r in self.local_reg.items()
                            if _r == _v and _l != var and _l not in _seen),
                           None)
                if _nx is None:
                    break
                _seen.add(_nx)
                _f = self.cfw.get(_nx, {}).get(c0)
                if _f is not None and _f[0] == self._bkey() \
                        and (_f[1], _f[2]) != (_v, c0):
                    self.values[ins.result], c0 = _f[1], _f[2]
                    continue
                # ... and when that local's own forward is its register (its
                # lanes were stored one by one), the cell the lane was
                # stored from
                _cell = (self.locals_.get(_nx) or {}).get(c0)
                if _cell is None or (_cell[0], _cell[1]) == (_v, c0):
                    break
                self.values[ins.result], c0 = _cell
        if self.values[ins.result] == self.local_reg.get(var):
            # a lane read of the NAME after a merge pair (core.py
            # `_name_read_after_pair`; `0000_cy_g.frag`'s `u3 * u2.w`)
            self._name_read_after_pair(var, {ci}, ins.result)
        if ENV.get("G2S_CFWDBG"):                          # diagnosis only
            import sys as _sys
            _sys.stderr.write(
                "CFW load %s.%d -> %s.%d cell=%s cfw=%s kind=%s reg=%s\n"
                % (var, ci, self.values[ins.result], c0, cell,
                   self.cfw.get(var, {}).get(ci),
                   self.cfw_kind.get((var, ci)),
                   self.local_reg.get(var)))
        self.comps[ins.result] = (c0,) * 4
        self.scalar.add(ins.result)

    def _load_lmem_element(self, ins, _lch):
        """AN ELEMENT OF A LOCAL-MEMORY ARRAY (notes/84).  The graph of
        `0071_lm_icb.frag`: the index's carrier (op 0x4a, typed signed int like
        every address carrier -- `MOV.S R0.x, fragment.attrib[0];`) and the
        load (op 59), which prints the element's full swizzle: `MOV.U R4,
        lmem0[R0.x].xyzw;`."""
        _var = _lch.args()[0]
        # A COMPONENT OF AN ELEMENT reads that component and writes THE SAME
        # LANE: `light_cull.comp` prints `MOV.F R0.w, lmem0[R0.x].w;` beside
        # the whole element's `MOV.F R10, lmem0[R0.x].xyzw;`, and a reader
        # that wants it in `.x` gathers it afterwards (`MOV.F R3.x, R0.w;`).
        # (notes/124 §1)
        _comp = None
        if len(_lch.args()) == 3 and not ENV.get("G2S_NOLMEMCOMP"):
            _cv = _scalar_value(self.module, _lch.args()[2])
            try:
                _comp = int(_cv)
            except (TypeError, ValueError):
                _comp = None
            if _comp is None or not 0 <= _comp < 4:
                raise NotEstablished(
                    "a local-memory element component that is not a "
                    "constant lane: not measured")
        elif len(_lch.args()) != 2:
            raise NotEstablished("a local-memory chain past the element: a "
                                 "component of an element is not measured")
        if _var not in self.lmem_elem:
            raise NotEstablished("a load of a local-memory array that was "
                                 "not stored")
        _iid = _lch.args()[1]
        if _scalar_value(self.module, _iid) is not None:
            raise NotEstablished("a constant index into local memory: not "
                                 "measured")
        # the index is read as a block index is (`_index_source`): a lane of
        # a construct made in this block reads its source -- `0108_lm_ix.frag`'s
        # `icb[u_xlati19.x]` after `u_xlati19 = ivec4(k.x * 3, ..)` prints
        # `MOV.S R2.x, R0;`, R0 the MUL (the corpus's `chr_hair_f0ad47e1`)
        _isrc = (_source(self.values, self.comps, _iid, 1)
                 if ENV.get("G2S_NOLMEMINDEXCON")
                 else self._index_source(_iid))
        _car = _opchain.mnemonic_for_opcode(nodes.ADDRESS_MOV, nodes.S32)
        if _isrc is None or _car is None:
            raise NotEstablished("a local-memory index with no form")
        _i = self._fresh()
        self.carriers.add(int(_i[1:]))
        self.lines.append(_emit(_car, "%s.x" % _i, _isrc))
        dst = self._fresh(True)
        if _comp is None:
            self.lines.append(_emit(
                self.lmem_elem[_var][0], dst,
                "lmem%d[%s.x].xyzw" % (self.lmem_k[_var], _i)))
        else:
            _c = _COMPONENTS[_comp]
            self.lines.append(_emit(
                self.lmem_elem[_var][0], "%s.%s" % (dst, _c),
                "lmem%d[%s.x].%s" % (self.lmem_k[_var], _i, _c)))
            self.comps[ins.result] = (_comp,) * 4
            self.scalar.add(ins.result)
        self.values[ins.result] = dst

    def _load_local_whole(self, ins, ptr):
        if self.lpend.get(ptr, self._bkey()) != self._bkey():
            del self.lpend[ptr]         # touched in a later block
        cells = self.locals_[ptr]
        if not cells or len(cells) != 4:
            raise NotEstablished(
                "a whole load of a local that was written a component at a "
                "time: assembling it emits masked MOVs whose ORDER is the "
                "scheduler's (notes/31)")
        texts = set(t for t, _ in cells.values())
        if len(texts) != 1 or tuple(
                cells[i][1] for i in range(4)) != (0, 1, 2, 3):
            raise NotEstablished("a whole load of a local assembled from "
                                 "parts")
        self._refuse_undefined_lanes(ins, ptr)
        self.values[ins.result] = cells[0][0]
        self.load_of[ins.result] = (ptr, cells[0][0])
        _fw = self.lfwd.get(ptr)
        if _fw is not None and _fw[0] == self._bkey():
            self._forward_local_store(ins, ptr, _fw)
            return
        _cf = self.cfw.get(ptr, {})
        _map = dict((c, _cf[c][1:]) for c in _cf if _cf[c][0] == self._bkey())
        if _map:
            # some components were stored in this block: each read takes its
            # component's stored value, the others the name (`0069_lv_v2a.frag`)
            self.lsplit[ins.result] = _map

    def _refuse_undefined_lanes(self, ins, ptr):
        """A LANE THE LOCAL NEVER STORES is undefined, and what the compiler
        prints for arithmetic on it is its internals', not a rule: `cr_n.frag`
        adds a local stored only at `.w` and prints `MOV.F R5.xyz, R4.wyzw;
        MOV.F R7, R5.xyzx;` -- lanes of another value.  A read that takes only
        the stored lanes is defined and stays: a whole store of the local
        writes the stored mask (`mq_n12`, `ld_ar`), a shuffle or an extract
        picks stored lanes.  Anything else is refused.
        `G2S_UNDEFLANES=1` lowers it anyway."""
        if ENV.get("G2S_UNDEFLANES"):
            return
        _n = _components(self.module, self.module.result_insn[
            ins.result].result_type) if ins.result in \
            self.module.result_insn else None
        _st = self.lstored.get(ptr)
        if not _n or _st is None or all(_c in _st for _c in range(_n)):
            return
        for _fn in self.module.functions:
            for _u in _fn.insns:
                if ins.result not in _u.args():
                    continue
                if _u.opcode == Op.OpStore:
                    continue
                if (_u.opcode == Op.OpVectorShuffle
                        and all(_s in _st or _s > 3 and _u.args()[1]
                                != ins.result for _s in _u.args()[2:])):
                    continue
                if (_u.opcode == Op.OpCompositeExtract
                        and _u.args()[1] in _st):
                    continue
                if (_u.opcode == Op.OpBitcast
                        and not ENV.get("G2S_NOBITCASTLANES")):
                    # A BITCAST READS THE LOCAL LANE BY LANE (notes/114
                    # \u00a744), so it is defined in the same sense an
                    # extract is: the undefined lane becomes an ordinary
                    # register read of the local's own register and
                    # everything after it is ordinary.  `0114_ul_e.frag` and
                    # `0114_ul_f.frag` (stored `.xz` and `.xyw`) print
                    # `MOV.U R3.x, R6.y;` for the lane nothing stored;
                    # `0114_ul_g.frag` computes with the result afterwards and
                    # is still byte for byte the compiler's.  ARITHMETIC on
                    # the local itself is NOT this and is still refused --
                    # `ul_a`..`ul_d` are the counter-cases.
                    # `G2S_NOBITCASTLANES=1` refuses it again.
                    continue
                raise NotEstablished(
                    "a read of a local's lane it never stores: the value is "
                    "undefined, and the compiler's lines for it are its "
                    "internals'")

    def _forward_local_store(self, ins, ptr, _fw):
        """A LOAD IN THE BLOCK OF THE LOCAL'S STORE takes the stored value,
        construct head and all: `gl_Position = u_xlat1` right after
        `u_xlat1 = vec4(..)` prints `MOV.F result.position.x, R4;` --
        component 0's source (`0072_ce_n29.vert`)."""
        self.values[ins.result] = _fw[1]
        self.fwd_blk[ins.result] = (self._bkey(), self.local_reg.get(ptr))
        if len(_fw) > 2:
            self.fwd_of[ins.result] = _fw[2]
        if ptr in self.lfwd_line:
            self.fwd_line[ins.result] = self.lfwd_line[ptr]
        if len(_fw) > 2 and _fw[2] in self.head_src:
            self.head_src[ins.result] = self.head_src[_fw[2]]
        if len(_fw) > 2 and _fw[2] in self.comps:
            self.comps[ins.result] = self.comps[_fw[2]]

    def _load_buffer(self, ins, chain):
        self._computation()
        mnem, name, off, _tid, _dyn, _bcomp = chain
        _lt = self.module.types.get(_tid)
        if (_lt is not None and _lt.opcode == Op.OpTypeMatrix
                and _dyn is None):
            self._load_matrix(ins, chain, _lt)
            return
        # The destination's write mask is the count table's for the loaded
        # type (f_7100f09328, notes/70): `LDC.F32 R0.x`.
        _lds = (_opchain.dest_suffix(_components(self.module, _tid))
                if _lt is not None and _lt.opcode != Op.OpTypeMatrix else "")
        if _lds is None:
            raise NotEstablished(
                "a block load whose component count has no mask")
        # ONE NODE PER LOCATION PER BLOCK (notes/75).  A load is not a temp
        # (notes/67 §1): the reader substitutes its expression, and
        # `f_7100f3aab0` interns expressions -- a template that matches an
        # existing node returns that node.  So three `OpLoad`s of `U.s` are
        # one expression and one DAG leaf in the block: `0075_pu_a.vert`'s
        # `vec4(s, s, s, s)` prints ONE `LDC.F32 R0.x, buf0[16];`, and
        # `0075_pu_c.vert`'s `a.x * s` and `a.y + s` read the same register.
        _dl = (_dyn if isinstance(_dyn, list)
               else [_dyn] if _dyn is not None else [])
        _lkey = (mnem, name, off, _lds, tuple(
            (self._index_source(_d[0]), _d[1]) for _d in _dl))
        # A COMPONENT OF A DYNAMICALLY INDEXED BLOCK VECTOR is the whole
        # vector's load read through that component, exactly as the STATIC
        # form does (`_load_buffer_static`): the address is the only thing
        # that differs between them (notes/123 §1).
        # `G2S_NODYNBCOMP=1` restores the refusal.
        if (_bcomp is not None and _dyn is not None
                and ENV.get("G2S_NODYNBCOMP")):
            raise NotEstablished(
                "a component of a dynamically indexed block vector: not "
                "measured")
        _lhit = self.ldc_same.get(_lkey)
        # THE INTERNING IS FOR A STATIC LOCATION.  notes/75's "one node per
        # location per block" is read from STATIC loads, and a dynamically
        # indexed one is made again: `chr_cloth_421b91dc.frag` loads
        # `sbo_buf15[i + 76]` twice in one block and the compiler prints two
        # `LDB.U32` lines with two SEPARATE address computations -- seven
        # loads there, seven `MUL.S R?.x, fragment.attrib[4], {128,..}` and
        # seven `MOV.S`, one apiece -- and its 13 dynamic `OpLoad`s become
        # 13 listing loads, so nothing is shared.  That is the same shape
        # `particle_fog_block_init.comp` already needs, where every buffer
        # access recomputes its own index.
        # `G2S_DYNLOADSHARE=1` restores the refusal this replaced.
        if (_lhit is not None and _lhit[0] == self._bkey()
                and (_dyn is None or ENV.get("G2S_DYNLOADSHARE"))):
            self._load_buffer_again(ins, _lhit, _dyn, _bcomp)
        elif _dyn is None:
            self._load_buffer_static(ins, chain, _lkey, _lds)
        else:
            self._load_buffer_dynamic(ins, chain, _lkey, _lds, _dl)

    def _load_matrix(self, ins, chain, _lt):
        """NOTHING IS EMITTED HERE.  The matrix's columns are loaded by
        whatever multiplies it, one block each (`0000_if_mat.vert` dumps four
        blocks, each `LDC`, `MOV`, `MUL` and -- past the first -- `ADD`)."""
        mnem, name, off, _tid, _dyn, _bcomp = chain
        _col = self.module.types.get(_lt.args()[0])
        _nc = _lt.args()[1]
        if _col is None or _col.opcode != Op.OpTypeVector \
                or _col.args()[1] != 4 or _nc != 4:
            raise NotEstablished(
                "a matrix load that is not 4x4: the column stride of a "
                "narrower one is the layout's, not the type's, and no probe "
                "measures it")
        self.matrices[ins.result] = (mnem, name, off, _nc, 16)

    def _load_matrix_column_part(self, ins, chain):
        """A READ OF A MATRIX MEMBER'S PART IS A LOAD AND A MOV, EVERY TIME
        (notes/104 §7, read with the traces, not the listings).

        The front end makes `m[c].k` -- and a whole column `m[c]` -- a
        MATRIX swizzle (IR cls 14, op 0x1d; a vector member's `v.k` is op
        0x1c) over the member.  `f_7100f10a30` lowers 0x1d in its case at
        0x7100f117c0 (jump-table entry 0x364): the child is lowered afresh
        (`f_7100f0f260` makes a new `LDC`, 0x3b, at every read -- gdb on
        `0104_mx_b.vert` shows two loads of `m[0].y` with the same member symbol,
        where `ld_mw`'s second read of `m.w` makes none), `f_7100f12b90`
        builds the swizzle (0x5e), and because the lowered operand is a load
        a MOV (0x47, 0x7100f11890) is made -- in `mx_b`, `mx_g` and `ld_mx`
        at every read, element or column.

        So the value is the MOV's: an element's `.x` (`MOV.F R0.x, R1.y;`),
        a column whole (`MOV.F R0, R0;`).  Its readers are any value's: the
        fold dump of `ld_mx` has the ADD reading the MOV, the insert into
        lane z reading it (`MOV.F R3.z, R0.x;`), and the merge taking it AS
        lane x (`MOV.F R3.x, R0.y;` is that MOV); `mx_g`'s stores (0x3a)
        take it as their source, so it writes the output or the local
        (`MOV.F result.attrib[0], R1;`).  A dynamic column index is not
        measured."""
        mnem, name, off, _tid, _dyn, _bcomp = chain
        module = self.module
        if _dyn is not None:
            raise NotEstablished(
                "a dynamically indexed column of a block matrix: not "
                "measured")
        if ENV.get("G2S_NOMATELEMENT"):
            raise NotEstablished("a matrix element (G2S_NOMATELEMENT)")
        self._computation()
        _lds = _opchain.dest_suffix(_components(module, _tid))
        _mov = _opchain.mnemonic_for_opcode(
            nodes.MOV, _glasm_type_code(module, ins.result_type))
        if _lds is None or _mov is None or _mov.startswith("<"):
            raise NotEstablished(
                "a matrix element whose load mask or MOV has no form")
        self.node_loads.add(ins.result)
        self.node_src[ins.result] = (mnem, _lds, name, off, _bcomp, _mov)
        if _bcomp is not None:
            self.scalar.add(ins.result)
        self._emit_matrix_part(ins.result)

    def _emit_matrix_part(self, val):
        """The load and the MOV of a matrix part read `val` (notes/104 §7),
        in the current block."""
        mnem, _lds, name, off, _bcomp, _mov = self.node_src[val]
        dst = self._fresh()
        n = len(self.lines)
        # the value is the MOV's node, a node of its own and no component of
        # the load (a construct takes it straight: `0104_mx_d.vert`'s `m[1].x` in
        # lane z is `MOV.F R2.x, R2; .. MOV.F R2.z, R2.x;`, one MOV)
        self.ldc_canon[val] = (val, None)
        self.ldc_line[val] = n
        self.ldc_vreg[dst] = (self._bkey(), mnem, _lds, name, off)
        self.ldc_lines.add(n)
        self.lines.append("%s %s%s, %s[%d];" % (mnem, dst, _lds, name, off))
        _t = self._fresh()
        self.defline[val] = n
        self.defblk[val] = self.blk_no
        self.node_bkey[val] = self._bkey()
        self.node_line[val] = (len(self.lines),
                               _t + (_lds if _bcomp is None else ".x"))
        if _bcomp is None:
            self.lines.append(_emit(_mov, _t + _lds, dst))
        else:
            self.lines.append(_emit(_mov, "%s.x" % _t, _swizzle(dst, _bcomp)))
        self.values[val] = _t

    def _node_again(self, val):
        """A MATRIX PART READ BY A STORE THAT OPENED A BLOCK is made again
        in the new block: the load is substituted into the store's
        statement (notes/67 §1), as `_reload` makes a block load again --
        `0103_ld_mx.vert` prints `u_xlat0.x = m[2].y`'s `LDC.F32X2 R0.y,
        buf0[48];` after `u_xlat0.z`'s pair, and its MOV writes the lane.
        The MOV made before the cut reads a load nothing else reads and
        writes a value nothing reads: both go (`dead_lines`)."""
        _k = self.node_line[val][0]
        self.dead_lines.add(_k)
        self._emit_matrix_part(val)

    def _load_buffer_again(self, ins, _lhit, _dyn, _bcomp):
        """The same location loaded again in its block: the same node."""
        if _dyn is not None:
            raise NotEstablished(
                "a dynamically indexed block load repeated in its block: "
                "whether the address computation is shared has not been "
                "measured")
        self.values[ins.result] = _lhit[1]
        # the NODE a construct compares: the location and, for a component
        # chain, the component read of it
        self.ldc_canon[ins.result] = (_lhit[2], _bcomp)
        if _bcomp is not None:
            self.comps[ins.result] = (_bcomp,) * 4
            self.scalar.add(ins.result)
        # two readers now: the load cannot fold into a store
        self.ldc_at.pop(_lhit[2], None)
        # ... NOR INTO A CONSTRUCT'S LANE X (notes/114 §41).  Reaching here
        # is the proof that the two loads are in ONE block, which is the
        # thing that cannot be known at the first load -- so it is recorded
        # against the FIRST load's SPIR-V id, which survives a second pass,
        # and `_load_is_lane_x` consults it next time round.
        if _dyn is None:
            self.shared_first.add(_lhit[2])
        # From here the register is free to fold again: the duplicate has
        # been reached, so no later construct is waiting for it.  That is
        # the compiler's pair -- the first construct copies out of the
        # load's register, the second writes its `.y` into that register.
        self.ldc_shared.discard(_lhit[1])

    def _load_buffer_static(self, ins, chain, _lkey, _lds):
        """NOT a band temp: `0005_p05_ubo.vert`'s load and the multiply that reads
        it are ONE vreg in the compiler, which is why they share `R0`.  Only
        the dynamically indexed form, whose address computation sits in
        between, splits them."""
        mnem, name, off, _tid, _dyn, _bcomp = chain
        dst = self._fresh()
        n = len(self.lines)
        self.ldc_same[_lkey] = (self._bkey(), dst, ins.result, n)
        self.ldc_canon[ins.result] = (ins.result, _bcomp)
        self.ldc_line[ins.result] = n
        if _bcomp is None:
            self.ldc_at[ins.result] = (n, dst)
        else:
            self.comps[ins.result] = (_bcomp,) * 4
            self.scalar.add(ins.result)
        self.ldc_vreg[dst] = (self._bkey(), mnem, _lds, name, off)
        if ins.result in self.shared_before and not ENV.get("G2S_LANESHARE"):
            # the previous pass saw this location loaded again IN THIS BLOCK
            self.ldc_shared.add(dst)
        self.ldc_lines.add(n)
        self.lines.append("%s %s%s, %s[%d];" % (mnem, dst, _lds, name, off))
        self.values[ins.result] = dst

    def _index_source(self, vid):
        """A dynamic index's operand form.  A component of a CONSTRUCT made in
        this block is read from its source, as an arithmetic operand's is
        (`_con_read`): `0103_ix_a.frag`'s `v[u_xlati0.y]` right after `u_xlati0 =
        ivec2(a.x * 3, a.y * 5)` scales `MUL.S R3.x, R1, {4, ..};`, R1 the
        second product (`monster_02d3d44e`'s `MUL.S R21.x, R8, {64, ..};`)."""
        _crd = (self._con_read(vid)
                if not ENV.get("G2S_NOINDEXCON") else None)
        if _crd is not None:
            return _crd.split(".")[0]
        _d = self.module.result_insn.get(vid)
        if (_d is not None and _d.opcode == Op.OpBitcast
                and not ENV.get("G2S_NOINDEXBITCAST")):
            # AN INDEX THROUGH A BITCAST (uint to int) is scaled from the
            # bitcast's OPERAND; the bitcast is still made -- the SPIR-V
            # value's statement -- and nothing reads it (notes/111): the
            # slice's `compute_volumefog_blur-1` indexes `buf3` by
            # `int(u_xlatu_loop_1)` and prints `MUL.S R1.x, R0, {16, ..};`,
            # R0 the counter, and `MOV.U R1.x, R0;` after the load
            _o = self.module.result_insn.get(_d.args()[0])
            if (_o is not None and _o.has_result_type
                    and _components(self.module, _o.result_type) == 1
                    and _d.args()[0] in self.values):
                return self._index_source(_d.args()[0])
        return _source(self.values, self.comps, vid, 1)

    def _index_constant_part(self, vid):
        """(the other operand, the constant) when the index is an integer
        add of a scalar constant and a value, else None."""
        _d = self.module.result_insn.get(vid)
        if _d is None or _d.opcode != Op.OpIAdd or len(_d.args()) != 2:
            return None
        if (_signedness(self.module, _d.result_type) != "S"
                and not ENV.get("G2S_FOLDUNSIGNED")):
            # only a SIGNED add folds: `0000_dx_d.frag`'s `m[k.y + 2u]` (uint)
            # keeps `ADD.U R1.x, .., {2, ..}; MUL.S R2.x, R1, {16, ..};` and
            # `+ 432` -- an unsigned add may wrap
            return None
        _a, _b = _d.args()
        for _c, _x in ((_a, _b), (_b, _a)):
            if _c in self.module.constants and _x not in self.module.constants:
                _v = _scalar_value(self.module, _c)
                try:
                    return _x, int(_v)
                except (TypeError, ValueError):
                    return None
        return None

    def _load_buffer_dynamic(self, ins, chain, _lkey, _lds, _dl):
        """A DYNAMIC INDEX.  MEASURED from the emit list of `0052_if_arr.vert`: a
        `0x90` scaling the index by the array stride, a `0x4a` carrier, and
        the load taking the register as a BYTE offset.  Both the scale and
        the carrier are typed SIGNED INT whatever the index's GLSL type is --
        a probe with a `uint` index compiles to the same `MUL.S` and `MOV.S`
        -- so the suffix comes from the ADDRESS type and not from the carrier
        node's own `n44`, which reads 12 and would give `.U`.  That
        contradiction is recorded in notes/52.

        The carrier is a node of its OWN, with its own vreg: the compiler's
        records for `0072_ce_head.vert` give the scale and the carrier consecutive
        numbers (27, 28), coalesced into one register -- one placeholder for
        both hid the carrier's interference from the graph (py/ifg.py).

        TWO INDICES (notes/89, `0089_sb_b.frag`'s `buf[i].value[j]`): each is
        scaled by its own stride -- `MUL.S R2.x, i, {580, ..}; MUL.S R1.x, j,
        {4, ..};` -- and the INNER product is added to the outer one, `ADD.S
        R1.x, R1, R2;`, before the carrier.  The INNER product is made first:
        `tools/nodedump.py probes/0089_sb_b.frag.spv` numbers `j * 4` vreg 9 and
        `i * 580` vreg 10, then the ADD 11 and the carrier 12 (the outer MUL
        still prints first: the scheduler's)."""
        mnem, name, off, _tid, _dyn, _bcomp = chain
        _t, off, _nprods = self._dynamic_address(chain, _dl)
        dst = self._fresh(True)
        self.lines.append("%s %s%s, %s[%s.x%s];"
                          % (mnem, dst, _lds, name, _t,
                             " + %d" % off if off else ""))
        self.values[ins.result] = dst
        # A COMPONENT OF THE LOADED VECTOR is a swizzle on the reads, the
        # same as the static form does (`_load_buffer_static`): the load
        # takes the whole vector and the component rides in `comps`.
        if _bcomp is not None:
            self.comps[ins.result] = (_bcomp,) * 4
            self.scalar.add(ins.result)
        self.ldc_same[_lkey] = (self._bkey(), dst, ins.result,
                                len(self.lines) - 2 - _nprods
                                - (_nprods == 2))
        self.ldc_dyn[dst] = self._bkey()

    def _dynamic_address(self, chain, _dl):
        """The scaled index and its carrier (`_load_buffer_dynamic`, and a
        buffer STORE's the same, `0111_st_c.comp`): (the carrier, the constant
        offset, how many products)."""
        mnem, name, off, _tid, _dyn, _bcomp = chain
        if len(_dl) == 1 and not ENV.get("G2S_NOINDEXFOLD"):
            # A CONSTANT ADDED TO THE INDEX GOES INTO THE OFFSET: `0108_dx_a.frag`'s
            # `m[1 + k.y]` and `m[k.y + 2]` scale `k.y` itself and load
            # `buf0[R0.x + 448]` / `+ 464` (432 + 16, + 32), the corpus's
            # `chr_skin_bdbffe42` the same.  The ADD is still made -- the
            # SPIR-V value's statement -- and nothing reads it.
            _fx = self._index_constant_part(_dl[0][0])
            if _fx is not None:
                _dl = [(_fx[0], _dl[0][1])]
                off = off + _fx[1] * _dl[0][1]
        _mul = _opchain.mnemonic_for_opcode(nodes.MUL, nodes.S32)
        _mov = _opchain.mnemonic_for_opcode(nodes.ADDRESS_MOV, nodes.S32)
        _add = _opchain.mnemonic_for_opcode(nodes.ADD, nodes.S32)
        _idxs = [self._index_source(_d[0]) for _d in _dl]
        if any(_i is None for _i in _idxs) or _mul is None \
                or _mov is None or _add is None:
            raise NotEstablished(
                "a dynamically indexed block load whose index has no form")
        _prods = []
        _pairs = list(zip(_idxs, _dl))
        _inner_first = len(_pairs) == 2 and not ENV.get("G2S_OUTERFIRST")
        if _inner_first:
            _pairs.reverse()
        _first = len(self.lines)
        for _i, _d in _pairs:
            _p = self._fresh()
            self.lines.append(_emit(_mul, "%s.x" % _p, _i,
                                    "{%d, 0, 0, 0}" % _d[1]))
            _prods.append(_p)
        if _inner_first:
            _prods.reverse()
        _t0 = _prods[0]
        _tie = [_first]
        if len(_prods) == 2:
            _t0 = self._fresh()
            _tie.append(len(self.lines))
            self.lines.append(_emit(_add, "%s.x" % _t0, _prods[1], _prods[0]))
        _t = self._fresh()
        self.carriers.add(int(_t[1:]))
        _tie.append(len(self.lines))
        self.lines.append(_emit(_mov, "%s.x" % _t, _t0))
        if not ENV.get("G2S_ADDRNOTIE"):
            # THE ADDRESS IS ONE STATEMENT: the first product made, the ADD
            # and the carrier share a `node[36]` (`tools/nodedump.py`:
            # `0111_st_c.comp`'s MUL and MOV.S seq 2 and 2, `0108_dx_a.frag`'s 1/1,
            # 9/9, 17/17, `0111_cb_b.comp`'s 3/3, and `0089_sb_b.frag`'s inner MUL,
            # ADD and carrier 4/4/4 with the outer MUL made after them, 6).
            # `cb_b` needs it: the value's gather, made before the address,
            # sits between them in pass 1's list (notes/111).
            self.ties.append(_tie)
        return _t, off, len(_prods)

    def _is_front_facing(self, gins, ptr):
        return (gins is not None and self.model == ExecutionModel.Fragment
                and (self.module.decoration(ptr, Decoration.BuiltIn)
                     or (None,))[0] == BuiltIn.FrontFacing
                and not ENV.get("G2S_NOFACING"))

    def _load_interface(self, ins, ptr):
        module = self.module
        gins = module.globals.get(ptr)
        if self._is_front_facing(gins, ptr):
            # gl_FrontFacing IS AN EXPRESSION, NOT A REGISTER (notes/88): the
            # header declares it `int ... FACE_FLAT`, and a read is `facing >
            # 0` -- `MOV.S R0.x, fragment.facing; SGT.S R1.x, R0, {0, ..};` in
            # `0088_ff_a.frag`.  A load is substituted into its reader (notes/67
            # §1), so nothing is emitted here; the reader builds the
            # expression.
            self.facing_loads.add(ins.result)
            return
        if gins is not None \
                and gins.operands[2] == StorageClass.UniformConstant:
            # THE HANDLE LOAD IS EMITTED AT THE USE, NOT AT THE LOAD.
            # `LDC.U64 D0.x, buf14[0];` sits IMMEDIATELY before the
            # instruction that samples -- after the coordinate has been
            # computed, not before it (`0033_fr_texlod.frag`) -- so the SPIR-V
            # `OpLoad` of the sampler records the variable and the image op
            # emits both lines together.
            self.samplers[ins.result] = ptr
            _pt = module.types.get(gins.result_type)
            if _pt is not None and _storage_image(module, _pt.operands[2]):
                self.pending_images.append(ins.result)
            return
        # An access chain that picks ONE COMPONENT of an interface variable
        # is not a load at all: the component becomes a swizzle on the
        # operand, which is why `0026_cf_if.vert` compares against
        # `vertex.attrib[0]` (bare, meaning `.x`) rather than loading it.
        ch = self.by_result.get(ptr)
        # ... and a PER-VERTEX INPUT ARRAY's first index is the VERTEX, not
        # a component (notes/114 SS24): `v[1]` prints `vertex[1].attrib[0]`.
        # Without this the index was taken for a component and printed as a
        # swizzle of `vertex.attrib[0]`.  `G2S_NOPVLOC=1` turns it off.
        if not ENV.get("G2S_NOPVLOC"):
            _pv = _per_vertex_location_operand(
                module, ptr, self.model, self.by_result)
            if _pv is not None:
                self.values[ins.result] = _pv[0]
                if _pv[1] is not None:
                    self.comps[ins.result] = (_pv[1],) * 4
                    self.scalar.add(ins.result)
                return
        if (ch is not None and ch.opcode in ACCESS_CHAINS
                and len(ch.args()) == 2):
            base = _interface_operand(module, ch.args()[0], self.model)
            cv = _scalar_value(module, ch.args()[1])
            if base is not None and cv is not None and 0 <= int(cv) < 4:
                self.values[ins.result] = base
                self.comps[ins.result] = (int(cv),) * 4
                self.scalar.add(ins.result)
                return
        if (ch is not None and ch.opcode in ACCESS_CHAINS
                and len(ch.args()) == 3
                and not ENV.get("G2S_NOBLOCKMEMBER")):
            # ONE COMPONENT of a block member, the three-index chain: the
            # component becomes a swizzle on the member's attribute, as it
            # does for a plain located variable just above.
            _bm = _block_member_operand(module, ch.args()[0],
                                        _scalar_value(module, ch.args()[1]),
                                        self.model)
            _cv = _scalar_value(module, ch.args()[2])
            if _bm is not None and _cv is not None and 0 <= int(_cv) < 4:
                if self.lines and (not self.cuts
                                   or self.cuts[-1] != len(self.lines)):
                    self._flush()
                    self.cuts.append(len(self.lines))
                self.values[ins.result] = _bm[0]
                self.comps[ins.result] = (int(_cv),) * 4
                self.scalar.add(ins.result)
                return
        if (ch is not None and ch.opcode in ACCESS_CHAINS
                and len(ch.args()) == 2
                and not ENV.get("G2S_NOBLOCKMEMBER")):
            _bm = _block_member_operand(module, ch.args()[0],
                                        _scalar_value(module, ch.args()[1]),
                                        self.model)
            if _bm is not None:
                self._load_block_member(ins, ptr, _bm)
                return
        src = _interface_operand(module, ptr, self.model)
        if src is None:
            src = _per_vertex_operand(module, ptr, self.model, self.by_result)
        if src is None:
            src = _per_vertex_dynamic_location_operand(
                module, ptr, self.model, self.by_result)
        if isinstance(src, tuple):
            self._load_per_vertex_dynamic(ins, src)
            return
        if src is None:
            self._refuse_load(ptr)
        self.values[ins.result] = src

    def _load_per_vertex_dynamic(self, ins, src):
        """notes/63: the index is copied into a register by the carrier MOV
        (typed signed int, as `if_arr`'s), then the element is MOVed out of
        the aliased array."""
        _alias, _iid = src[0], src[1]
        _loc = src[2] if len(src) > 2 else None
        _isrc = _source(self.values, self.comps, _iid)
        _car = _opchain.mnemonic_for_opcode(nodes.ADDRESS_MOV, nodes.S32)
        _mv = _opchain.mnemonic_for_opcode(
            nodes.MOV, _glasm_type_code(self.module, ins.result_type))
        if _isrc is None or _car is None or _mv is None \
                or _mv.startswith("<"):
            raise NotEstablished(
                "a dynamically indexed per-vertex read whose index or MOV "
                "has no form")
        self._computation()
        _i = self._fresh()
        self.carriers.add(int(_i[1:]))
        self.lines.append(_emit(_car, "%s.x" % _i, _isrc))
        dst = self._fresh()
        # the read writes the VALUE'S mask: `MOV.F R18.xyz, ..` for a vec3
        _n = _components(self.module, ins.result_type)
        _ds = (_opchain.dest_suffix(_n) or "") if (_n and _loc is not None) \
            else ""
        self.lines.append(_emit(_mv, dst + _ds,
                                "%s[%s.x]" % (_alias, _i) if _loc is None
                                else "%s[%s.x][%d]" % (_alias, _i, _loc)))
        self.values[ins.result] = dst

    def _load_block_member(self, ins, ptr, bm):
        """A MEMBER OF AN INPUT INTERFACE BLOCK is materialised COMPONENT BY
        COMPONENT into a register (notes/114 \u00a748).

        `probes/0114_ib_a.frag`'s `vec3 a` at ATTR0 does not read
        `fragment.attrib[0]` whole; the compiler fills a register lane by
        lane, with the merge halves of an ordinary local:

            MOV.F R4.yz, R4;   MOV.F R4.x, fragment.attrib[0];
            MOV.F R0.x, fragment.attrib[0].y;
            MOV.F R4.xz, R4;   MOV.F R4.y, R0.x;
            ...

        so the member is lowered as a LOCAL whose components are stored from
        the attribute, and the ACCESS CHAIN's own result id is the local's
        identity -- it is a pointer to the member's type, which is exactly
        what the local machinery asks of a variable id.
        `G2S_NOBLOCKMEMBER=1` refuses it again."""
        module = self.module
        _op, _mt = bm
        _n = _components(module, _mt)
        if not _n or _n > 4:
            raise NotEstablished("an interface block member that is not a "
                                 "vector or scalar")
        # ONE BLOCK PER COMPONENT STORE.  The compiler's `stamps` for
        # `0114_ib_b.frag` are blocks of 2, 3, 3, 3, 2, 3, 3 -- a component
        # store's pair, with its scratch where it has one -- so a member's
        # FIRST store opens a block just as its later ones do
        # (`_open_for_component_store` only cuts when the same name is
        # stored twice, which a new member never is).  Without the cut the
        # first member's last lane and the second member's first pair land
        # in ONE span and the scheduler reorders across the boundary.
        if self.lines and (not self.cuts or self.cuts[-1] != len(self.lines)):
            self._flush()
            self.cuts.append(len(self.lines))
        self.lstored.setdefault(ptr, set()).update(range(_n))
        # The component travels in `comps`, as it does for a real value:
        # the gather appends the source component itself, so handing it an
        # already-swizzled operand printed `fragment.attrib[1].y.x`.  The
        # lane's value gets an id of its own, past the module's bound, so
        # `_component_operand` reads its component and finds no defining
        # instruction -- which is what sends `.y`..`.w` through the scratch
        # `.x`, exactly as the compiler does.
        _base = getattr(self, "_bm_id", None) or (module.bound + 1)
        for _k in range(_n):
            _vid = _base + _k
            self.values[_vid] = _op
            self.comps[_vid] = (_k,) * 4
            self._store_component_pair(_vid, None, _op, ptr, _k)
        self._bm_id = _base + _n
        self.values[ins.result] = self._local_register(ptr)
        self.load_of[ins.result] = (ptr, self.values[ins.result])

    def _refuse_load(self, ptr):
        """What the load is, for the census: the chain's base variable, its
        storage class, and the index operands' kinds."""
        module = self.module
        _pc = self.by_result.get(ptr)
        chained = _pc is not None and _pc.opcode in ACCESS_CHAINS
        _base = _pc.args()[0] if chained else ptr
        _bg = module.globals.get(_base)
        _sc = ([k for k, v in STORAGECLASS.items()
                if _bg is not None and v == _bg.operands[2]]
               or ["local"])[0]
        _idx = ("".join("c" if _scalar_value(module, a) is not None else "v"
                        for a in _pc.args()[1:]) if chained else "")
        raise NotEstablished(
            "a load that is not a straight interface read: the value would "
            "need a register, and the allocator is unread "
            "[%s %s idx=%s]" % (module.name_of(_base), _sc, _idx))

    # -- local-memory arrays -----------------------------------------------

    def _lmem_store(self, var, val, in_registers=False):
        """`var = <constant array>` into local memory (notes/84).

        The compiler's graph for `0071_lm_icb.frag` (`tools/gsum.py`): the array
        value is a construct, one MOV node per element into a temp of its own
        (seq 1..4, no successors), and the assignment is one store node per
        element into `lmem<k>[i]` (op 60, seq 7, 10, 13, 16) whose source is
        the element's constant, not the temp.  Both print:
            MOV.U R3, {0, 0, 0, 4};  ...  MOV.U lmem0[3], {0, 0, 0, 4};
        An array kept in REGISTERS stores each element into a register of its
        own (`0091_mq_n8.frag`: eight `MOV.U R, {..}` and no `lmem`).
        """
        module = self.module
        cins = module.constants.get(val)
        ptr_t = module.types.get(module.globals[var].result_type)
        at = module.types.get(ptr_t.operands[2])
        if cins is None or cins.opcode != Op.OpConstantComposite:
            raise NotEstablished("a local-memory array stored from something "
                                 "other than a constant composite")
        et = at.args()[0]
        _ne = _components(module, et)
        _mv = self._mov_for(_glasm_type_code(module, et))
        if _ne != 4 or _mv is None:
            raise NotEstablished("a local-memory array whose element is not "
                                 "a four-component vector: only uvec4/vec4 "
                                 "arrays are measured")
        elems = list(cins.args())
        # A REPEATED ELEMENT IS NOT INTERNED.  `ui_bold_p.frag`'s array has
        # 34 elements of which two are the same constant (indices 0 and 19),
        # and the listing has 34 `MOV.U R..` temps and both stores --
        # `MOV.U lmem0[0], {1065353216, 0, 0, 0};` and `MOV.U lmem0[19],
        # {1065353216, 0, 0, 0};`.  One temp per element, duplicates and
        # all, which is what this already emits.  (notes/124 §1)
        # `G2S_LMEMINTERN=1` restores the refusal below, which guarded
        # nothing: the interning it worried about does not happen.
        if len(set(elems)) != len(elems) and ENV.get("G2S_LMEMINTERN"):
            raise NotEstablished("a constant array repeating an element: "
                                 "whether the construct interns the element "
                                 "nodes is not measured")
        ops = [_constant_operand(module, e) for e in elems]
        if any(o is None for o in ops):
            raise NotEstablished("a constant array element with no operand "
                                 "form")
        if var in self.lmem_elem:
            raise NotEstablished("a second store to a local-memory array")
        self._computation()
        for o in ops:
            self.lines.append(_emit(_mv, self._fresh(True), o))
        for i, o in enumerate(ops):
            self.lines.append(_emit(_mv, self._fresh(True) if in_registers
                                    else "lmem%d[%d]" % (self.lmem_k[var], i),
                                    o))
        self.lmem_elem[var] = (_mv, len(ops))

    # -- variables and chains ----------------------------------------------

    def _arm_variable(self, ins):
        """A function-local variable emits NOTHING.  The front end never
        builds a symbol for it: `@TMP_<id>` (notes/28) is the name a VALUE
        gets, and a local that is only stored to and loaded from is
        forwarded, which is the `ctx[208]` behaviour of notes/25 seen from the
        other side.  Its value is tracked in `locals_`."""
        if ins.opcode != Op.OpVariable:
            return False
        self.locals_[ins.result] = {}
        return True

    def _arm_access_chain(self, ins):
        """Resolved at the load or store that uses it."""
        return ins.opcode in ACCESS_CHAINS
