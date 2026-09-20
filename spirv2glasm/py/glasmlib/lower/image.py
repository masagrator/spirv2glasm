"""image.py -- sampled images, their images, and the texture instructions.

The texture instruction is the one the handle feeds:

    LDC.U64 D0.x, buf14[<opaque base + 8 * binding>];
    TEX.F   R0, fragment.attrib[0], handle(D0.x), 2D;

The `D0` register is why the TEMP block grows a `LONG TEMP D0;` line, and the
dimension keyword is the image type's `Dim`.  `LDC`/`LDB` come from the
operand's binding kind rather than the opcode (notes/30), which is why the
mnemonic here is spelled out rather than looked up.
"""
from spvnames import Op, Dim, Decoration, ImageOperands, ImageFormat

import opchain as _opchain
import sched as _sched
from glasmlib.common import NotEstablished, ENV
from glasmlib.types import _components, _glasm_type_code
from glasmlib.operands import _constant_source
from glasmlib.blocks import _opaque_offset
from glasmlib.text import _emit, _source, _swizzle
from glasmlib import nodes

_IMAGE_DIM = {Dim.Dim1D: "1D", Dim.Dim2D: "2D", Dim.Dim3D: "3D",
              Dim.Cube: "CUBE",
              # a texel buffer, `texelFetch(samplerBuffer, i)` (notes/111,
              # `si_e.comp`: `TXF.F R3, {1, 0, 0, 0}, handle(D1.x), BUFFER;`)
              Dim.Buffer: "BUFFER"}

# THE IMAGE OPS' GLASM OPCODES, MEASURED -- not their mnemonics read off a
# listing.  `g2s_trace_fold` dumps every node of the emit list with its
# opcode (notes/49), and the namer spells them for the result's type.
_IMAGE_OP = {
    Op.OpImageSampleImplicitLod: nodes.TEX,
    Op.OpImageSampleExplicitLod: nodes.TXL,
    Op.OpImageFetch: nodes.TXF,
    Op.OpImageSampleDrefImplicitLod: nodes.TEX,     # `sa_a.frag`: TEX.F
}
# A depth comparison: the reference rides in the coordinate (notes/99).
_DREF_OPS = (Op.OpImageSampleDrefImplicitLod,)

# THE TARGET KEYWORDS MEASURED for arrayed and shadow images, `SHADOW`,
# `ARRAY`, then the dimension: `ta_a.frag` `ARRAY2D`, `sa_c.frag`
# `SHADOW2D`, `sa_a.frag` `SHADOWARRAY2D`.  Any other combination is refused
# rather than spelled by analogy.
_MEASURED_KEYWORDS = frozenset(("ARRAY2D", "SHADOW2D", "SHADOWARRAY2D"))
# `TXL` and `TXF` take the lod packed into the coordinate's `.w` (notes/61).
_LOD_OPS = (Op.OpImageSampleExplicitLod, Op.OpImageFetch)

# The separate sampler's handle is ORed into the texture's (notes/83).
_HANDLE_OR = "OR.S"


def _image_dim(module, var_id, shadow=False):
    """The GLASM target keyword for a sampler variable, or None.

    An ARRAYED image (OpTypeImage's Arrayed operand) prints `ARRAY` before
    its dimension, and a depth comparison `SHADOW` before that -- the
    comparison is the OPERATION's (a separate texture sampled through a
    comparison sampler has a Depth 0 image type), so the caller says."""
    ptr = module.types.get(module.globals[var_id].result_type)
    if ptr is None:
        return None
    t = module.types.get(ptr.operands[2])
    if t is not None and t.opcode == Op.OpTypeSampledImage:
        t = module.types.get(t.args()[0])
    if t is None or t.opcode != Op.OpTypeImage:
        return None
    dim = _IMAGE_DIM.get(t.args()[1])
    arrayed = t.args()[3] == 1
    if dim is None or not (arrayed or shadow):
        return dim
    keyword = ("SHADOW" if shadow else "") + ("ARRAY" if arrayed else "") + dim
    return keyword if keyword in _MEASURED_KEYWORDS else None


def _handle_offset(module, var, refusal):
    """The constant-buffer offset of an opaque variable's handle."""
    pt = module.types.get(module.globals[var].result_type)
    base = _opaque_offset(module, pt.operands[2])
    binding = module.decoration(var, Decoration.Binding)
    if base is None or binding is None:
        raise NotEstablished(refusal)
    return base + 8 * binding[0]


# A STORAGE IMAGE's target keyword (notes/111), measured for each
# dimension: `st3.comp` (image1D) `1D`, `si_a.comp` `2D`, `st2.comp` `3D`,
# `st7.comp` (image2DArray) `ARRAY2D`, `st8.comp` (uimageBuffer) `BUFFER`.
# The lanes of the coordinate each reads are the dimension's.
_STORAGE_DIM = {(Dim.Dim1D, 0): ("1D", 0x1), (Dim.Dim2D, 0): ("2D", 0x3),
                (Dim.Dim3D, 0): ("3D", 0x7), (Dim.Dim2D, 1): ("ARRAY2D", 0x7),
                (Dim.Buffer, 0): ("BUFFER", 0x1)}

# The glasm type code that prints `.U32` (glasmnames.SUFFIX).
_U32_CODE = 12

# AN IMAGE LOAD'S MEMORY FORM, by the image's format: the suffix the LOADIM
# prints and how many components it loads (notes/111, one probe each:
# `ld_rgba32f` `LOADIM.F32X4`, `ld_rg32f` `.F32X2`, `ld_r32f` `.F32`,
# `ld_rgba32ui` `.U32X4`, `ld_r32ui` `.U32`, `ld_rgba32i` `.S32X4`, `ld_r32i`
# `.S32`).  The packed formats (rgba8, the 16-bit ones, r11f_g11f_b10f,
# rgb10_a2) unpack through BFE/UP4UB/UP2H chains of their own and are refused.
_LOAD_FORM = {ImageFormat.Rgba32f: (".F32X4", 4, 6),
              ImageFormat.Rg32f: (".F32X2", 2, 6),
              ImageFormat.R32f: (".F32", 1, 6),
              ImageFormat.Rgba32ui: (".U32X4", 4, 12),
              ImageFormat.R32ui: (".U32", 1, 12),
              ImageFormat.Rgba32i: (".S32X4", 4, 11),
              ImageFormat.R32i: (".S32", 1, 11),
              # PACKED: one U32 loaded, then unpacked (`_UNPACK`)
              ImageFormat.Rgba8: (".U32", 1, 12),
              ImageFormat.Rgba8ui: (".U32", 1, 12),
              ImageFormat.Rgba8i: (".S32", 1, 11),
              ImageFormat.Rgba16ui: (".U32X2", 2, 12),
              ImageFormat.Rgba16f: (".U32X2", 2, 12),
              ImageFormat.Rg16f: (".U32", 1, 12),
              ImageFormat.R16f: (".U16", 1, 12),
              ImageFormat.Rgba16: (".U32X2", 2, 12),
              ImageFormat.Rgb10A2: (".U32", 1, 12),
              ImageFormat.R11fG11fB10f: (".U32", 1, 12),
              ImageFormat.Rgba8Snorm: (".U32", 1, 12)}

# The unpack a packed format's load is followed by, as statements of their
# own (`g2s_trace_fold` on `ld_rgba8.frag`, notes/111).
_UNPACK = {ImageFormat.Rgba8: "rgba8", ImageFormat.Rgba8ui: "bfe8",
           ImageFormat.Rgba8i: "bfe8", ImageFormat.Rgba16ui: "bfe16",
           ImageFormat.Rgba16f: "half", ImageFormat.Rg16f: "half",
           ImageFormat.R16f: "half", ImageFormat.Rgba16: "unorm16",
           ImageFormat.Rgb10A2: "rgb10a2",
           ImageFormat.R11fG11fB10f: "r11g11b10",
           ImageFormat.Rgba8Snorm: "snorm8"}


def _storage_dim(module, var_id):
    """(keyword, coordinate lane mask) of a storage image variable, or
    None."""
    ptr = module.types.get(module.globals[var_id].result_type)
    t = module.types.get(ptr.operands[2]) if ptr is not None else None
    if t is None or t.opcode != Op.OpTypeImage or t.operands[6] != 2:
        return None
    if t.operands[5] != 0:
        return None                     # multisampled: not measured
    return _STORAGE_DIM.get((t.operands[2], t.operands[4]))


class ImageOps(object):

    def _arm_image_write(self, ins):
        """`imageStore` (notes/111): the handle, then

            STOREIM.<t> handle(D0.x), <texel>, <coordinate>, <keyword>;

        `.F`/`.U`/`.S` by the texel's type (`si_a`, `st1`, `st2`), the texel
        and the coordinate as any source prints (a constant whole and padded:
        `{1, 2, 0, 0}`).  THE STORE IS A VALUE: the compiler's node (opcode
        0x1ba, `g2s_trace_fold`) has a register, and two MOVs read it, each
        stored into a temp of its own -- the whole value at the texel's type
        and `.x` as `.U` (`si_a.comp`: `MOV.U R0.x, R1; MOV.F R0, R1;`, R1
        never written; `st2.comp` `MOV.S R0, R1`).  The printed line has no
        destination; the internal one carries the store's vreg first, and the
        body's final render drops it (finish.py)."""
        if ins.opcode != Op.OpImageWrite:
            return False
        if ENV.get("G2S_NOIMAGEWRITE"):
            raise NotEstablished("an image store (G2S_NOIMAGEWRITE)")
        module = self.module
        args = ins.args()
        if len(args) != 3:
            raise NotEstablished("an image store with image operands: "
                                 "not measured")
        img, coord_id, texel_id = args
        var = self.samplers.get(img)
        if var is None or isinstance(var, tuple):
            raise NotEstablished("an image store whose image is not a "
                                 "plain uniform")
        _dk = _storage_dim(module, var)
        if _dk is None:
            raise NotEstablished("a storage image whose dimension is not "
                                 "measured")
        dim, _cmask = _dk
        _tt = module.result_insn.get(texel_id)
        _ttype = (_tt.result_type if _tt is not None and _tt.has_result_type
                  else module.constants[texel_id].result_type
                  if texel_id in module.constants else None)
        if _ttype is None:
            raise NotEstablished("an image store texel with no type")
        _tc = _glasm_type_code(module, _ttype)
        _mn = _opchain.mnemonic_for_opcode(nodes.STOREIM, _tc)
        _mv = _opchain.mnemonic_for_opcode(nodes.MOV, _tc)
        _mvu = _opchain.mnemonic_for_opcode(nodes.MOV, _U32_CODE)
        if None in (_mn, _mv, _mvu):
            raise NotEstablished("an image store the tables do not name")
        self._computation()
        texel = self._image_operand(texel_id, 4)
        coord = self._image_operand(coord_id, None, _cmask)
        d = self._image_handle(img, var)
        _s = self._fresh()
        self.lines.append(_emit(_mn, _s, "handle(%s.x)" % d, texel, coord,
                                dim + self._write_release(coord_id,
                                                          texel_id)))
        _a = self._fresh()
        _b = self._fresh()
        self.lines.append(_emit(_mv, _a, _s))
        self.lines.append(_emit(_mvu, "%s.x" % _b, _s))
        return True

    def _arm_image_read(self, ins):
        """`imageLoad` (notes/111):

            LOADIM<form> L, <coordinate>, handle(D0.x), <keyword>;

        with the coordinate BEFORE the handle, the form by the image's format
        (`_LOAD_FORM`).  The statement (`g2s_trace_fold`, one `node[36]`):
        the load L, a MOV of L stored into a temp of its own (dead: `ld_r32f`
        `MOV.F R2, R0;`, `si_b` `MOV.F R0, R1;`) at the MEMORY type (`MOV.U`
        for `.U32`), and L stored into a second.  A format narrower than the
        texel vector is then widened by a construct, the next statement: the
        loaded lanes, `0` and `1` for the rest (`ld_r32f`: `MOV.F R1.w, {1,
        ..}.x; MOV.F R1.z, {0, ..}.x; MOV.F R1.y, {0, ..}.x; .. MOV.F R1.x,
        R0;` -- lane y of `rg32f` gathered, `MOV.F R1.x, R0.y; MOV.F R1.y,
        R1.x;`)."""
        if ins.opcode != Op.OpImageRead:
            return False
        if ENV.get("G2S_NOIMAGEREAD"):
            raise NotEstablished("an image load (G2S_NOIMAGEREAD)")
        module = self.module
        args = ins.args()
        if len(args) != 2:
            raise NotEstablished("an image load with image operands: "
                                 "not measured")
        img, coord_id = args
        var = self.samplers.get(img)
        if var is None or isinstance(var, tuple):
            raise NotEstablished("an image load whose image is not a "
                                 "plain uniform")
        _dk = _storage_dim(module, var)
        if _dk is None:
            raise NotEstablished("a storage image whose dimension is not "
                                 "measured")
        dim, _cmask = _dk
        ptr = module.types.get(module.globals[var].result_type)
        _it = module.types.get(ptr.operands[2])
        _form = _LOAD_FORM.get(_it.operands[7])
        if _form is None:
            raise NotEstablished("an image load of a format whose unpack is "
                                 "not measured")
        _suf, _nload, _mcode = _form
        _tc = _glasm_type_code(module, ins.result_type)
        _n = _components(module, ins.result_type)
        _mvm = _opchain.mnemonic_for_opcode(nodes.MOV, _mcode)
        _mvt = _opchain.mnemonic_for_opcode(nodes.MOV, _tc)
        _unpack = _UNPACK.get(_it.operands[7])
        if (_mvm is None or _mvt is None or _n != 4
                or (_tc != _mcode and _unpack is None)):
            raise NotEstablished("an image load whose texel type is not the "
                                 "format's: not measured")
        self._computation()
        coord = self._image_operand(coord_id, None, _cmask)
        d = self._image_handle(img, var)
        _l = self._fresh(True)
        _tie = [len(self.lines)]
        if not ENV.get("G2S_LOADIMSTMT"):
            # a store of the value takes the load's `node[36]` (`ld_rgba32f`:
            # the output's MOV, the dead temp's and the flush all seq 4, the
            # LOADIM's)
            self.defline[ins.result] = len(self.lines)
        self.lines.append(_emit("LOADIM" + _suf, _l, coord,
                                "handle(%s.x)" % d, dim))
        _t1 = self._fresh()
        if not ENV.get("G2S_LOADIMTEMPLATE"):
            # ... and a NAME the statement stores (its 0x3a), walked by pass
            # 1 among the block's names at the load's statement: `hl_c.comp`
            # lists it after the local's store and the value's flush, all
            # three MOVs of the load seq 4 (`tools/gsum.py`, 0.7 .. 0.9)
            self.stmtpos[_t1] = _tie[0]
        _tie.append(len(self.lines))
        if not ENV.get("G2S_LOADIMMOVEARLY"):
            # THE TEMP'S MOV IS A STORE'S, made after the rest of the
            # statement (`_mark_store_movs`): `g2s_trace_liveset` numbers it
            # last -- `ld_r32f`: the output 1, the load 2 (live out), the
            # construct's name 3 and its writes 4, the handle 5, this MOV 6;
            # `ld_rgba32f`: the load's flushed name 2, the load 4, this 5
            self.store_movs.append(len(self.lines))
        self.lines.append(_emit(_mvm, _t1, _l))
        self.ties.append(_tie)
        if _unpack == "rgba8":
            self._unpack_rgba8(ins, _l, _mvm, _mvt)
            return True
        if _unpack == "bfe8":
            self._unpack_fields(ins, _l, _mcode, _mvt,
                                [(8, 0, 0), (8, 8, 0), (8, 16, 0),
                                 (8, 24, 0)])
            return True
        if _unpack == "bfe16":
            # `ld_rgba16ui`: two words, two 16-bit fields each, `BFE.U
            # R0.x, {16, 0, 0, 0}, R4; .. BFE.U R3.x, {16, 16, 0, 0}, R4.y;`
            self._unpack_fields(ins, _l, _mcode, _mvt,
                                [(16, 0, 0), (16, 16, 0), (16, 0, 1),
                                 (16, 16, 1)])
            return True
        if _unpack == "rgb10a2":
            self._unpack_rgb10a2(ins, _l)
            return True
        if _unpack == "r11g11b10":
            self._unpack_r11g11b10(ins, _l)
            return True
        if _unpack == "snorm8":
            self._unpack_snorm8(ins, _l)
            return True
        if _unpack in ("half", "unorm16"):
            self._unpack_pairs(ins, _l, _nload, _mvm, _mvt,
                               _unpack == "half",
                               _it.operands[7] == ImageFormat.R16f)
            return True
        if _nload == _n:
            self.values[ins.result] = _l
            return True
        flat = ([(_l, _c) for _c in range(_nload)]
                + [(None, "{0, 0, 0, 0}")] * (3 - _nload)
                + [(None, "{1, 0, 0, 0}")])
        self.defline[ins.result] = len(self.lines)
        dst, _text = self._assemble(flat, _mvt, is_band=True)
        self.values[ins.result] = dst
        return True

    def _unpack_fields(self, ins, _l, _code, _mv, fields):
        """AN INTEGER FORMAT UNPACKS BY BITFIELD EXTRACTS, one statement
        each (`g2s_trace_fold` on `ld_rgba8ui.frag`: a BFE, op 0x1af, of
        the loaded word per lane, seq 6, 8, 10, 12, each stored), then the
        vector built of them, one statement (seq 13): `BFE.U R0.x, {8, 0,
        0, 0}, R4; .. BFE.U R3.x, {8, 24, 0, 0}, R4;  MOV.U R5.w, R3.x; ..
        MOV.U R5.x, R0;` -- `.S` for a signed format (`ld_rgba8i`).
        `fields`: (width, offset, the loaded word's lane) per lane."""
        _bfe = _opchain.mnemonic_for_opcode(nodes.BFE, _code)
        if _bfe is None:
            raise NotEstablished("a BFE the tables do not name")
        _parts = []
        for _w, _o, _c in fields:
            _b = self._fresh(True)
            self.lines.append(_emit(_bfe, "%s.x" % _b,
                                    "{%d, %d, 0, 0}" % (_w, _o),
                                    _swizzle(_l, _c)))
            _parts.append((_b, 0))
        self.defline[ins.result] = len(self.lines)
        dst, _text = self._assemble(_parts, _mv, is_band=True)
        self.values[ins.result] = dst

    def _unpack_snorm8(self, ins, _l):
        """RGBA8_SNORM (`g2s_trace_fold` / `tools/nodedump.py` on
        `ld_rgba8_snorm.frag`): the word's lane x into a temp (`MOV.U R1.x,
        R4;`, seq 5, stored); per lane a signed BFE of it (seq 7, 11, 18,
        25, stored) and its store into ONE int vector, a local's component
        store pair each -- the copy `MOV.S R0.yzw, R0;` and the write
        `MOV.S R0.x, R3;` (both seq 9: lane x ties), then `.y` 13/15, `.z`
        20/22, `.w` 27/29 (write before copy); then the float conversion
        (`I2F.S R1, R0;`, seq 31) divided by 127 (`DIV.F32`, seq 30,
        numbered before the conversion it reads, stored), `MIN.F` by 1 (34,
        stored) and `MAX.F` by -1 (36), the value."""
        _code = {k: _opchain.mnemonic_for_opcode(v, c) for k, v, c in (
            ("movu", nodes.MOV, 12), ("movs", nodes.MOV, 11),
            ("bfe", nodes.BFE, 11), ("i2f", nodes.I2F, 11),
            ("div", nodes.DIV, 6), ("min", nodes.MIN, 6),
            ("max", nodes.MAX, 6))}
        if None in _code.values():
            raise NotEstablished("an unpack the tables do not name")
        # the temps are NAMES, each live out of its own block
        # (`g2s_trace_liveset`: the records load 2, lane 3, BFE .x 4, the
        # vector 5, BFE .y 6 .. the DIV 9 and MIN 10 in the band; the block
        # 0 seed `2 3 4 5 6`, the last block's `1 5 9 10 11`)
        _p = self._fresh(True)
        self.lines.append(_emit(_code["movu"], "%s.x" % _p, _l))
        _t = self._fresh(True)
        for _k in range(4):
            _b = self._fresh(True)
            self.lines.append(_emit(_code["bfe"], "%s.x" % _b,
                                    "{8, %d, 0, 0}" % (8 * _k), _p))
            _cc = "xyzw"[_k]
            if _k and not ENV.get("G2S_SNORMONEBLOCK"):
                # THE VECTOR STORED AGAIN OPENS A BLOCK at the store, as a
                # local's second store does (notes/55 §8): `tools/gsum.py`
                # has blocks 0 (.. the `.x` pair, the `.y` BFE), 1 (the `.y`
                # pair, the `.z` BFE), 2, 3 (the `.w` pair and the rest)
                self._open_block()
            _w = len(self.lines)
            self.lines.append(_emit(_code["movs"], "%s.%s" % (_t, _cc),
                                    _b if _k == 0 else "%s.x" % _b))
            if _k == 0:
                self.ties.append([_w, len(self.lines)])
            self.passthru.append(len(self.lines))
            self.lines.append(_emit(
                _code["movs"], "%s.%s" % (_t, "xyzw".replace(_cc, "")),
                _t))
        _f = self._fresh()
        _ti = _sched.Tie([len(self.lines)])
        self.lines.append(_emit(_code["i2f"], _f, _t))
        _d = self._fresh(True)
        _ti.seq = len(self.lines) + 0.5
        self.ties.append(_ti)
        self.lines.append(_emit(_code["div"], _d, _f, "{127, 0, 0, 0}.x"))
        _mn = self._fresh(True)
        self.lines.append(_emit(_code["min"], _mn, _d, "{1, 1, 1, 1}"))
        _v = self._fresh()
        self.defline[ins.result] = len(self.lines)
        self.lines.append(_emit(_code["max"], _v, _mn,
                                "{-1, -1, -1, -1}"))
        self.values[ins.result] = _v

    def _unpack_r11g11b10(self, ins, _l):
        """R11F_G11F_B10F (`g2s_trace_fold` / `tools/nodedump.py` on
        `ld_r11f_g11f_b10f.frag`): per field, its BFE (seq 6, 11, 16,
        stored), the shift into a float's exponent position (`SHL.U R5.x,
        R0, {17, 0, 0, 0}.x;`, seq 7, 12, 17, not stored) and that as a float
        (`MOV.F R5.x, R5;`, seq 9, 14, 19, stored); then one statement, the
        three as a vector (seq 21) times 2^112 (`MUL.F32 R9.xyz, R7,
        {5.19229686e+33, 0, 0, 0}.x;`, seq 20 -- the MUL numbered before the
        vector it reads); then the value, the product's lanes and `1` (seq
        23, lanes y and z gathered)."""
        _bfe = _opchain.mnemonic_for_opcode(nodes.BFE, 12)
        _shl = _opchain.mnemonic_for_opcode(nodes.SHL, 12)
        _mul = _opchain.mnemonic_for_opcode(nodes.MUL, 6)
        _mov = _opchain.mnemonic_for_opcode(nodes.MOV, 6)
        if None in (_bfe, _shl, _mul, _mov):
            raise NotEstablished("an unpack the tables do not name")
        _f = []
        for _w, _o, _sh in ((11, 0, 17), (11, 11, 17), (10, 22, 18)):
            _b = self._fresh(True)
            self.lines.append(_emit(_bfe, "%s.x" % _b,
                                    "{%d, %d, 0, 0}" % (_w, _o), _l))
            _s = self._fresh()
            self.lines.append(_emit(_shl, "%s.x" % _s, _b,
                                    "{%d, 0, 0, 0}.x" % _sh))
            _ff = self._fresh(True)
            self.lines.append(_emit(_mov, "%s.x" % _ff, _s))
            _f.append(_ff)
        _s0 = len(self.lines)
        _c, _h = self._assemble([(_x, 0) for _x in _f], _mov)
        _m = self._fresh(True)
        _tm = _sched.Tie([len(self.lines)])
        _tm.seq = _s0 - 0.5
        self.ties.append(_tm)
        self.lines.append(_emit(_mul, "%s.xyz" % _m, _c,
                                "{%s, 0, 0, 0}.x" % ("%.9g" % 2.0 ** 112)))
        self.defline[ins.result] = len(self.lines)
        dst, _text = self._assemble([(_m, 0), (_m, 1), (_m, 2),
                                     (None, "{1, 0, 0, 0}")], _mov,
                                    is_band=True)
        self.values[ins.result] = dst

    def _unpack_rgb10a2(self, ins, _l):
        """RGB10_A2 (`g2s_trace_fold` / `tools/nodedump.py` on
        `ld_rgb10_a2.frag`): the four fields by BFE, one statement each
        (seq 6 .. 12, stored); then ONE statement, the fields as floats
        divided by their maxima -- the reciprocals of `{1023, 1023, 1023,
        3}` one per lane into one register and the MUL (seq 13), the
        vector's writes of lanes y, z, w (14), lane x written by its own
        I2F (15) and the other I2Fs into temps of their own (16, 17, 18):
            RCP.F32 R8.w, {1023, 1023, 1023, 3}.w; ..
            I2F.U R4.x, R1; .. MOV.F R4.y, R4.x; .. I2F.U R4.x, R0;
            MUL.F32 R4, R4, R8;"""
        _bfe = _opchain.mnemonic_for_opcode(nodes.BFE, 12)
        _i2f = _opchain.mnemonic_for_opcode(nodes.I2F, 12)
        _rcp = _opchain.mnemonic_for_opcode(nodes.RCP, 6)
        _mul = _opchain.mnemonic_for_opcode(nodes.MUL, 6)
        _mov = _opchain.mnemonic_for_opcode(nodes.MOV, 6)
        if None in (_bfe, _i2f, _rcp, _mul, _mov):
            raise NotEstablished("an unpack the tables do not name")
        _b = []
        for _w, _o in ((10, 0), (10, 10), (10, 20), (2, 30)):
            _f = self._fresh(True)
            self.lines.append(_emit(_bfe, "%s.x" % _f,
                                    "{%d, %d, 0, 0}" % (_w, _o), _l))
            _b.append(_f)
        _s0 = len(self.lines)
        self.defline[ins.result] = _s0
        _c = self._fresh()
        _r = self._fresh()
        _k = "{1023, 1023, 1023, 3}"
        _stmt = _sched.Tie([])
        _stmt.seq = _s0
        for _cc in "xyzw":
            _stmt.append(len(self.lines))
            self.lines.append(_emit(_rcp, "%s.%s" % (_r, _cc),
                                    "%s.%s" % (_k, _cc)))
        _t = []
        for _j in (1, 2, 3):
            _t.append(self._fresh())
        _writes = _sched.Tie([])
        _writes.seq = _s0 + 0.1
        _x = _sched.Tie([])
        _x.seq = _s0 + 0.2
        _gs = []
        _x.append(len(self.lines))
        self.lines.append(_emit(_i2f, "%s.x" % _c, _b[0]))
        for _j, _tt in zip((1, 2, 3), _t):
            _g = _sched.Tie([len(self.lines)])
            _g.seq = _s0 + 0.2 + 0.01 * _j
            _gs.append(_g)
            self.lines.append(_emit(_i2f, "%s.x" % _tt, _b[_j]))
            _writes.append(len(self.lines))
            self.lines.append(_emit(_mov, "%s.%s" % (_c, "xyzw"[_j]),
                                    "%s.x" % _tt))
        _m = self._fresh(True)
        _stmt.append(len(self.lines))
        self.lines.append(_emit(_mul, _m, _c, _r))
        self.ties.extend([_stmt, _writes, _x] + _gs)
        self.values[ins.result] = _m

    def _unpack_pairs(self, ins, _l, _words, _mvu, _mvf, half, cvt16):
        """THE 16-BIT FORMATS UNPACK A WORD INTO TWO LANES (`g2s_trace_fold`
        on `ld_rgba16f`, `ld_rg16f`, `ld_r16f`, `ld_rgba16`): per loaded
        word, its lane into a temp (`MOV.U R1.x, R2;`, not stored; the U16
        load of `r16f` converts instead, `CVT.S32.U16 R0.x, R1;`), that as a
        float (`MOV.F R1.x, R1;`, stored) and the unpack of it (stored) --
        `UP2H.F H0.xy, R1.x;` into a SHORT register for a half format, one
        per word (`H0`, `H1`), `UP2US.F R6.xy, R1.x;` for unorm16 -- the
        words one after the other (seq 5 6 7, then 8 9 10).  Then the
        vector: the unpacked lanes, `0` and `1` for the rest, every lane
        past x gathered through a `.x` scratch (`MOV.F R4.x, H1; .. MOV.F
        R3.z, R4.x;`), one statement.  One image load per program is
        measured for the SHORT registers."""
        if half and self.wants_h and not ENV.get("G2S_MANYSHORT"):
            raise NotEstablished("a second SHORT register: its allocation "
                                 "is not measured")
        _up = _opchain.mnemonic_for_opcode(
            nodes.UP2H if half else nodes.UP2US, 6)
        _lanes = []
        for _k in range(_words):
            _a = self._fresh()
            if cvt16:
                self.lines.append(_emit("CVT.S32.U16", "%s.x" % _a, _l))
            else:
                self.lines.append(_emit(_mvu, "%s.x" % _a,
                                        _swizzle(_l, _k)))
            _b = self._fresh(True)
            self.lines.append(_emit(_mvf, "%s.x" % _b, _a))
            if half:
                _u = "H%d" % _k
                self.wants_h = True
                self.hregs = max(getattr(self, "hregs", 0), _k + 1)
            else:
                _u = self._fresh(True)
            self.lines.append(_emit(_up, "%s.xy" % _u, "%s.x" % _b))
            _lanes += [(_u, 0), (_u, 1)]
        _nl = 1 if cvt16 else 2 * _words
        flat = (_lanes[:_nl] + [(None, "{0, 0, 0, 0}")] * (3 - _nl)
                + ([(None, "{1, 0, 0, 0}")] if _nl < 4 else []))
        self.defline[ins.result] = len(self.lines)
        dst, _text = self._assemble(flat, _mvf, force=set(range(1, 4)),
                                    is_band=True)
        self.values[ins.result] = dst

    def _unpack_rgba8(self, ins, _l, _mvu, _mvf):
        """RGBA8 (unorm) UNPACKS AS FOUR STATEMENTS after the load's
        (`g2s_trace_fold` on `ld_rgba8.frag`, `tools/nodedump.py`): the
        word's lane x into a temp, `MOV.U R0.x, R1;` (seq 5, not stored);
        that as a float, `MOV.F R0.x, R0;` (seq 6, stored); the unpack into
        a SHORT register, `UP4UB.F H0, R0.x;` (seq 7, stored); and the
        vector, `MOV.F R2, H0;` (seq 8) -- the value, which a store then
        takes.  One SHORT register per program is measured (`H0`)."""
        if self.wants_h and not ENV.get("G2S_MANYSHORT"):
            raise NotEstablished("a second SHORT register: its allocation "
                                 "is not measured")
        _a = self._fresh()
        self.lines.append(_emit(_mvu, "%s.x" % _a, _l))
        _b = self._fresh(True)
        self.lines.append(_emit(_mvf, "%s.x" % _b, _a))
        self.wants_h = True
        self.lines.append(_emit(_opchain.mnemonic_for_opcode(
            nodes.UP4UB, 6), "H0", "%s.x" % _b))
        _v = self._fresh(True)
        self.defline[ins.result] = len(self.lines)
        self.lines.append(_emit(_mvf, _v, "H0"))
        self.values[ins.result] = _v

    def _image_handle(self, img, var):
        """A storage image op's handle.  THE OTHER STORAGE-IMAGE LOADS STILL
        PENDING are loaded first, each at its own OpLoad's place (notes/111):
        an image op is a memory access, and a load of an image made before it
        is not carried past it -- `si_b.comp`'s `OpLoad img; OpLoad src;
        imageRead(src); imageWrite(img)` makes `img`'s handle at seq 2,
        stored into a temp (`g2s_trace_fold`), and prints it D0 against
        `src`'s D1, where a texture sample between (`hl_a.frag`) leaves the
        handle to the store (seq 11).  `G2S_NOIMGPENDING=1` loads every
        handle at its use."""
        if not ENV.get("G2S_NOIMGPENDING"):
            for _r in list(self.pending_images):
                if _r == img:
                    continue
                _v = self.samplers.get(_r)
                if _v is not None and not isinstance(_v, tuple):
                    self._handle_load(_handle_offset(
                        self.module, _v, "a sampler with no Binding"),
                        statement=True)
        if img in self.pending_images:
            self.pending_images.remove(img)
        return self._load_handles(var, None)

    def _write_release(self, coord_id, texel_id):
        """THE ORDER PASS 1 RELEASES AN IMAGE STORE'S OPERANDS IN, as a
        `@r` tag on the target keyword (sched.py reads it, finish.py drops
        it): ascending `node[36]`.  The handle is loaded AT THE STORE, so a
        value an earlier statement made (stored in its temp) comes before
        it, in statement order, and a load substituted into the store's
        operands after it, coordinate then texel (the node's operand order)
        -- `g2s_trace_fold`: `st6.frag`'s TRUNC seq 1, the handle 4;
        `hl_a.frag`'s TEX 9, the handle 11; `hl_b.comp`'s handle 2, the
        texel's LDB 5 (pass 1 lists the reverse, `tools/gsum.py`).
        `G2S_STOREIMFIXED=1` drops the tag."""
        if ENV.get("G2S_STOREIMFIXED"):
            return ""
        _made, _subst = [], []
        for _k, _vid in (("c", coord_id), ("t", texel_id)):
            _vi = self.module.result_insn.get(_vid)
            _v = self.values.get(_vid) or ""
            if (_vi is not None and _vi.opcode != Op.OpLoad
                    and _v.startswith("#")
                    and self.defline.get(_vid) is not None):
                _made.append((self.defline[_vid], _k))
            else:
                _subst.append(_k)
        return "@r" + "".join([k for _l, k in sorted(_made)] + ["h"]
                              + _subst)

    def _image_operand(self, vid, nres, lanes=None):
        """A storage image op's texel or coordinate as a source: a constant
        whole (a scalar one without a selector, `st3.comp`'s `{5, 0, 0,
        0}`), anything else as its value prints, read at `lanes` when the
        operand is a register."""
        module = self.module
        if vid in module.constants:
            _k = _constant_source(module, vid, 1)
            if _k is None:
                raise NotEstablished("an image operand constant with no "
                                     "form")
            return _k
        _vi = module.result_insn.get(vid)
        _w = (_components(module, _vi.result_type)
              if _vi is not None and _vi.has_result_type else None)
        src = _source(self.values, self.comps, vid, nres or _w)
        if src is None or src.startswith(("-", "|")):
            raise NotEstablished("an image operand with no form")
        if (lanes is not None and vid not in self.comps
                and src.startswith(("#", "R"))):
            src = "%s@%x" % (src, lanes)
        return src

    def _arm_sampled_image(self, ins):
        """A SEPARATE TEXTURE AND SAMPLER (Vulkan GLSL's `sampler2D(T, S)`):
        the pair is recorded and the image op loads BOTH handles and ORs them
        into one (`ps_a.frag`: `LDC.U64 D1.x, buf14[1352]; LDC.U64 D0.x,
        buf14[328]; OR.S D0.x, D0, D1;` then `TEX.F .., handle(D0.x),
        2D;`)."""
        if ins.opcode != Op.OpSampledImage:
            return False
        _ti, _si = ins.args()[0], ins.args()[1]
        if (_ti in self.samplers and _si in self.samplers
                and not isinstance(self.samplers[_ti], tuple)
                and not isinstance(self.samplers[_si], tuple)):
            self.samplers[ins.result] = (self.samplers[_ti],
                                         self.samplers[_si])
            return True
        raise NotEstablished(
            "a sampled image whose texture or sampler is not a plain uniform")

    def _arm_image(self, ins):
        """The image of a sampled image is the same handle: `texelFetch`
        reads it through OpImage, and nothing is emitted."""
        if ins.opcode != Op.OpImage or ins.args()[0] not in self.samplers:
            return False
        self.samplers[ins.result] = self.samplers[ins.args()[0]]
        return True

    def _arm_image_op(self, ins):
        op = ins.opcode
        if op not in _IMAGE_OP:
            return False
        module = self.module
        _mn = _opchain.mnemonic_for_opcode(
            _IMAGE_OP[op], _glasm_type_code(module, ins.result_type))
        if _mn is None:
            raise NotEstablished(
                "an image op whose mnemonic the image's tables do not give "
                "for this result type")
        self._computation()
        if op in _DREF_OPS:
            self._shadow_sample(ins, _mn)
            return True
        args = ins.args()
        _lod = self._explicit_lod(op, args)
        self._buffer_fetch = (op == Op.OpImageFetch and _lod is None
                              and args[0] in self.samplers
                              and not isinstance(self.samplers[args[0]],
                                                 tuple)
                              and _image_dim(module, self.samplers[args[0]])
                              == "BUFFER")
        coord = self._coordinate(args, _lod)
        if coord is None or args[0] not in self.samplers:
            raise NotEstablished("an image op whose operands have no form")
        var = self.samplers[args[0]]
        _svar = None
        if isinstance(var, tuple):
            var, _svar = var            # a separate texture and sampler
        dim = _image_dim(module, var)
        if dim is None:
            raise NotEstablished("an image type with no dimension keyword")
        d = self._load_handles(var, _svar)
        if _lod is not None:
            coord = self._lod_coordinate(args, coord, dim, _lod)
        # A SPIR-V value, so a named temp stored in its block: a band record,
        # live to the block's end (`ps_b.frag`: the ADD of two samples
        # interferes with both in the compiler's graph)
        dst = (self._fresh(True) if not ENV.get("G2S_TEXNOBAND")
               else self._fresh())
        self.lines.append("%s %s, %s, handle(%s.x), %s;"
                          % (_mn, dst, coord, d, dim))
        self.values[ins.result] = dst
        return True

    def _sampler_of(self, sid):
        """(texture variable, separate sampler variable or None)."""
        if sid not in self.samplers:
            raise NotEstablished("an image op whose operands have no form")
        var = self.samplers[sid]
        return var if isinstance(var, tuple) else (var, None)

    def _shadow_sample(self, ins, _mn):
        """A DEPTH COMPARISON (`texture(sampler2DShadow, vec3)`,
        HLSLcc's `texture(sampler2DArrayShadow(T, S), txVec0)`).  The
        reference is the coordinate's last component in SPIR-V
        (`OpCompositeExtract coord n-1`), and the compiler REBUILDS the
        coordinate as a construct -- x, y, (the layer,) the reference --
        every component past the first through a `.x` scratch, a constant
        too (`_assemble`'s `gather_all`), from the coordinate's own sources
        when it is a construct of this block.  Then one TEX with the
        `SHADOW` keyword, whose result is read at `.x`:
            MOV.F R2.x, fragment.attrib[0].w;  ..  MOV.F R0.x, fragment.attrib[0];
            LDC.U64 D0.x, buf14[0];
            TEX.F R1, R0, handle(D0.x), SHADOWARRAY2D;
            MOV.F result_color0, R1.x;          (`sa_a.frag`)"""
        module = self.module
        args = ins.args()
        if len(args) != 3:
            raise NotEstablished(
                "a shadow sample with image operands: not measured")
        coord, dref = args[1], args[2]
        var, _svar = self._sampler_of(args[0])
        dim = _image_dim(module, var, shadow=True)
        if dim is None:
            raise NotEstablished(
                "a shadow sample whose target keyword is not measured")
        n = 4 if "ARRAY" in dim else 3
        _dd = module.result_insn.get(dref)
        if (_dd is None or _dd.opcode != Op.OpCompositeExtract
                or list(_dd.args()) != [coord, n - 1]):
            raise NotEstablished(
                "a shadow reference that is not the coordinate's last "
                "component: not measured")
        _cd = module.result_insn.get(coord)
        _cmov = (_opchain.mnemonic_for_opcode(
            nodes.MOV, _glasm_type_code(module, _cd.result_type))
            if _cd is not None and _cd.has_result_type else None)
        if _cmov is None or _cmov.startswith("<"):
            raise NotEstablished(
                "a coordinate MOV the image's tables do not name")
        # the rebuilt coordinate is a BAND temp, live to the block's end:
        # the TEX's result does not take its register (`sa_a.frag`: `TEX.F
        # R1, R0, ..`), as the texture result is one (`ps_b.frag`)
        # A SEPARATE SAMPLER's handles and their OR are the sampled image's
        # expression, made before the coordinate is rebuilt (`sa_b.frag`: the
        # OR is seq 9, the rebuild 14..18); a combined sampler's handle load
        # is no node of the list and follows the rebuild (`sa_a.frag`).
        d = self._load_handles(var, _svar) if _svar is not None else None
        coord_reg, _h = self._assemble(self._coordinate_lanes(coord, n),
                                       _cmov, is_band=True,
                                       gather_all=True)
        if d is None:
            d = self._load_handles(var, _svar)
        dst = self._fresh(True)
        if not ENV.get("G2S_SHADOWSTMT"):
            # the value's own line is the TEX: a store of it takes the TEX's
            # `node[36]`, not the rebuilt coordinate's (`sa_b.frag`)
            self.defline[ins.result] = len(self.lines)
        self.lines.append("%s %s, %s, handle(%s.x), %s;"
                          % (_mn, dst, coord_reg, d, dim))
        self.values[ins.result] = dst
        self.comps[ins.result] = (0, 0, 0, 0)
        self.scalar.add(ins.result)
        self.component_values.add(ins.result)

    def _coordinate_lanes(self, coord, n):
        """The first `n` components of a coordinate as `_assemble` entries:
        a construct of this block gives its own sources (`sa_b.frag`'s
        `txVec0 = vec4(u_xlat0.xy, 2.0, u_xlat0.z)`: `R1`, `R1.y`, the
        constant, `R1.z`), anything else its register or input at each
        selected component."""
        base = self.values.get(coord)
        if base is None or base.startswith(("-", "|", "{")):
            raise NotEstablished("a coordinate with no form")
        cm = self.comps.get(coord, (0, 1, 2, 3))
        _cf = self.con_flat.get(base)
        if _cf is not None and _cf[1] == self._bkey():
            lanes = [_cf[0][cm[i]] if cm[i] < len(_cf[0]) else None
                     for i in range(n)]
            if any(e is None for e in lanes):
                raise NotEstablished(
                    "a coordinate construct with an unwritten component")
            return lanes
        return [(base, cm[i]) for i in range(n)]

    def _explicit_lod(self, op, args):
        """notes/61: `textureLod` / `texelFetch` with the Lod operand alone.
        The lod goes in the coordinate's `.w`, so the coordinate is BUILT in
        a register -- the construct shape, `.z` left unwritten -- and the
        handle load is created before it (seq 2 against the construct's
        3/5)."""
        self._lod_id = None
        if len(args) == 4 and op in _LOD_OPS and args[2] == ImageOperands.Lod:
            self._lod_id = args[3]
            return self._lod_lane(args[3])
        if len(args) != 2:
            raise NotEstablished(
                "an image op with extra operands: the coordinate is built in "
                "a register and the handle load lands inside that "
                "construction (notes/31)")
        return None

    def _lod_lane(self, lod):
        """The lod as the construct's `.w` entry (`_assemble`'s form).

        A CONSTANT is its one-component text (`MOV.F R0.w, {1, 0, 0, 0}.x;`,
        `fr_texlod.frag`).  A VALUE is written like any other component of
        the construct, from its register or input at its selected component:
        `tl_a.frag` (an input) `MOV.F R0.w, fragment.attrib[1].x;`, `tl_b`
        (a product) `MOV.F R1.w, R0.x;`, `tl_c` (a uniform member) `MOV.F
        R0.w, R1.x;` after its LDC, `tl_d` (a private local, the corpus's
        HLSLcc shape) `MOV.F R0.w, R1.x;` from the local's register."""
        _k = _constant_source(self.module, lod, 1)
        if _k is not None:
            return (None, _k)
        _v = self.values.get(lod)
        if _v is None or _v.startswith(("-", "|", "{")):
            raise NotEstablished("an explicit lod whose value has no form")
        return (_v, self.comps.get(lod, (0, 1, 2, 3))[0])

    def _coordinate(self, args, _lod):
        module = self.module
        coord = self.values.get(args[1])
        if (coord is None and _lod is None and args[1] in module.constants
                and getattr(self, "_buffer_fetch", False)):
            # A BUFFER FETCH's constant texel index prints padded, no
            # selector (`si_e.comp`: `TXF.F R3, {1, 0, 0, 0}, handle(D1.x),
            # BUFFER;`)
            return _constant_source(module, args[1], 1)
        if coord is not None and args[1] in self.comps and _lod is None:
            # the coordinate's selector, against its own width
            # (`map_15393bbe`: `texture(.., u_xlat0.zw)` prints `TEX.F R9,
            # R6.zwzw, ..`)
            _cdi = module.result_insn.get(args[1])
            _cw = (_components(module, _cdi.result_type)
                   if _cdi is not None and _cdi.has_result_type else None)
            coord = _source(self.values, self.comps, args[1], _cw)
        _lvc = self.load_of.get(args[1])
        if (coord is not None and _lvc is not None and coord == _lvc[1]
                and args[1] not in self.comps
                and not ENV.get("G2S_NOSTOREDREAD")):
            # A LOCAL READ WHOLE takes the components the local EVER stores
            # (`_stored_mask`, notes/87): the hand cut `mq_n22c.frag` samples
            # `u_xlat2.xy` with only `.x` ever written, and the compiler's
            # graph has no `.y` live from the entry.  The read mask rides on
            # the operand as `@m` (sched.py `_split`), stripped when the body
            # is printed.
            _st = self.lstored.get(_lvc[0])
            if _st is not None and not {0, 1} <= set(_st):
                _m = sum(1 << c for c in _st if c < 2)
                if _m:
                    coord = "%s@%x" % (coord, _m)
        return coord

    def _next_handle(self):
        d = "D%d" % self.handles
        self.handles += 1
        return d

    def _load_handles(self, var, _svar):
        """The texture's handle, and a separate sampler's ORed into it
        (notes/83: `OR.S D0.x, D0, D1;`).

        A HANDLE LOAD IS ONE NODE PER LOCATION PER BLOCK, as every block
        load is (notes/75, notes/104): `hd_c.frag` samples `sampler2D(T, S)`
        twice and prints the two LDCs once and the OR twice (`OR.S D0.x,
        D1, D2; OR.S D1.x, D1, D2;`) -- the OR is each `OpSampledImage`'s
        own node, so it defines a D of its own rather than overwriting the
        texture's.  The D numbers made here only name the values; the class
        4 colouring (py/ifg.py `allocate_long`) picks the registers."""
        module = self.module
        _off = _handle_offset(module, var, "a sampler with no Binding")
        d = self._handle_load(_off)
        if _svar is not None:
            _soff = _handle_offset(module, _svar,
                                   "a separate sampler with no Binding")
            _ds = self._handle_load(_soff)
            _do = self._next_handle()
            self.lines.append(_emit(_HANDLE_OR, "%s.x" % _do, d, _ds))
            return _do
        return d

    def _handle_load(self, off, statement=False):
        """`LDC.U64 Dn.x, buf14[off];`, or the D this block already loaded
        from `off`.  `statement`: a storage image's handle loaded at its
        OpLoad (`_image_handle`), tagged `@w` for pass 1 (sched.py
        `_pass1_extra_defs`; the render drops it)."""
        _hit = self.handle_same.get(off)
        if (_hit is not None and _hit[0] == self._bkey()
                and not ENV.get("G2S_NOHANDLESAME")):
            return _hit[1]
        d = self._next_handle()
        self.lines.append("LDC.U64 %s.x, buf14[%d]%s;"
                          % (d, off, "@w" if statement
                             and not ENV.get("G2S_NOHANDLESTMT") else ""))
        self.handle_same[off] = (self._bkey(), d)
        return d

    def _lod_coordinate(self, args, coord, dim, _lod):
        """The coordinate built in a register with the lod in `.w`."""
        module = self.module
        _cd = module.result_insn.get(args[1])
        # A 3D COORDINATE is the same construct with a third lane: `t3_a.frag`
        # (`textureLod(sampler3D, u_xlat0, 0.0)`, `u_xlat0` a vec3) prints
        # `MOV.F R0.w, {0,..}.x; MOV.F R0.x, R1.y; MOV.F R2.x, R1.z; .. MOV.F
        # R0.z, R2.x; MOV.F R0.y, R0.x; .. MOV.F R0.x, R1;` then `TXL.F R0,
        # R0, handle(D0.x), 3D;` -- `t3_b` with a stored lane and an input
        # ... and a CUBE direction is the same three lanes (`cu_a.frag`:
        # `MOV.F R0.w, {0,..}.x; MOV.F R0.z, R1.x; .. TXL.F R0, R0,
        # handle(D0.x), CUBE;`)
        _n = ({"2D": 2, "3D": 3, "CUBE": 3}.get(dim)
              if not ENV.get("G2S_LOD2DONLY") else {"2D": 2}.get(dim))
        if dim == "CUBE" and ENV.get("G2S_NOCUBELOD"):
            _n = None
        if (_n is None or _cd is None or not _cd.has_result_type
                or _components(module, _cd.result_type) != _n
                or coord.startswith(("{", "-", "|"))):
            raise NotEstablished(
                "an explicit-lod or fetch coordinate other than a 2D or 3D "
                "one read from a register or attribute: not measured")
        _cmov = _opchain.mnemonic_for_opcode(
            nodes.MOV, _glasm_type_code(module, _cd.result_type))
        if _cmov is None or _cmov.startswith("<"):
            raise NotEstablished(
                "a coordinate MOV the image's tables do not name")
        _cc = self.comps.get(args[1], (0, 1, 2, 3))
        _lanes = [(coord, _cc[_k]) for _k in range(_n)]
        _spl = ({} if ENV.get("G2S_NOLODSPLIT")
                else self.lsplit.get(args[1], {}))
        for _k in range(_n):
            if _k in _spl:
                # A LANE STORED IN THIS BLOCK is read from its stored value,
                # as a construct's gather reads it (`_flatten_operands`,
                # notes/90): `tl_e.frag`'s `textureLod(.., u_xlat3.xy, 0.0)`
                # right after `u_xlat3.y = m.y` (a block of its own after
                # `u_xlat3.x = m.x`) builds the coordinate's `.y` from `m.y`
                # -- `MOV.F R0.x, R0.y; .. MOV.F R1.y, R0.x;` -- and `.x` from
                # the local, `MOV.F R1.x, R2;`
                _lanes[_k] = _spl[_k]
        # THE LOD IS A COMPONENT OF THE CONSTRUCT like any other: a component
        # of a VECTOR is a select (notes/104 §6, `_forces_gather`) and is
        # gathered through a `.x` scratch -- `pg_b.frag`'s `textureLod(..,
        # u_xlat6.x)` right after `u_xlat6.x = max(..)` stored into the vec4
        # prints `MOV.F R2.x, R1; .. MOV.F R2.w, R2.x;` (the corpus's
        # `chr_cloth_a503f755`), where a scalar local's value goes straight in
        # (`tl_d.frag`: `MOV.F R0.w, R1.x;`)
        _lid = getattr(self, "_lod_id", None)
        _force = (set([3]) if _lid is not None
                  and self._selects_vector_component(_lid)
                  and _lid not in self.node_loads
                  and not ENV.get("G2S_NOLODGATHER") else set())
        _sk = set(k for k in _spl if k >= 1)
        if _sk and not ENV.get("G2S_NOSPLITGATHER"):
            # A STORED LANE is a component of the local's vector, a select
            # into a lane other than x, and is gathered even when it is a
            # constant: `pg_c.frag`'s `u_xlat7.y = 0.5; textureLod(..,
            # u_xlat7.xy, 0.0)` prints `MOV.F R2.x, {0.5, 0, 0, 0};` .. `MOV.F
            # R2.y, R2.x;` (the corpus's `chr_cloth_5482bf36` and three more),
            # as the construct after it gathers the same lane
            _force = set(_force) | _sk
        coord, _h = self._assemble(
            _lanes + [None] * (3 - _n) + [_lod], _cmov, _force)
        return coord
