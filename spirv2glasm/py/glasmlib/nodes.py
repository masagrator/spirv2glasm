"""nodes.py -- the GLASM node opcodes and type codes the lowering makes.

These are NOT SPIR-V numbers: they are the compiler's own, and they come from
`py/glslc/glasm.py` (through `glasmnames`), the table read out of the image
as Khronos's spirv.py is SPIR-V's.  What this module adds is the
CONVERTER'S reading of which opcode each role is: the image spells twelve
opcodes `MOV`, and the one an assignment, a carrier or a gather makes is
0x47 (`Op.MOV_47`), measured from the emit lists.
`opchain.mnemonic_for_opcode(MOV, F32)` is `MOV.F`.
"""
from glasmnames import Op, RoundingOp, Type

# node opcodes
LOAD_COPY = Op.MOV_3b           # the copy a matrix column load makes
MOV = Op.MOV_47                 # an assignment, a carrier, a gather
ADDRESS_MOV = Op.MOV_4a         # the carrier an index takes into a register
DDX = Op.DDX
DDY = Op.DDY
TRUNC_FAMILY = RoundingOp.ROUNDING_6d   # named by its mode (notes/23)
FLR = Op.FLR
I2F = Op.I2F
RCP = Op.RCP
RSQ = Op.RSQ
ADD = Op.ADD
DIV = Op.DIV                    # the one-component divide, and OpSDiv's
MAX = Op.MAX
MIN = Op.MIN
MUL = Op.MUL
SGE = Op.SGE
SGT = Op.SGT
SLT = Op.SLT
OR = Op.OR                      # any()'s chain (`0000_any_a.frag`)
BFI = Op.BFI                   # bitfieldInsert (`0108_bf_a.frag`'s DAG: 0x1b0)
TEX = Op.TEX_bc                 # the image instructions' opcodes, measured
TXL = Op.TXL_b7                 # from the emit lists (notes/49)
TXF = Op.TXF_b5
LOADIM = Op.LOADIM              # the storage-image instructions (notes/111:
STOREIM = Op.STOREIM            # 0x1b9 / 0x1ba in `g2s_trace_fold`)
UP4UB = Op.UP4UB
SHL = Op.SHL_9b                 # `SHL.U` of r11f_g11f_b10f's unpack (0x9b)
UP2H = Op.UP2H
UP2US = Op.UP2US
BFE = Op.BFE                    # bitfieldExtract, the packed formats
NOT = Op.NOT                    # `~x` (notes/114 §45: `0114_bn_a.frag`'s DAG
                                # is one 0x77 of mask 0xffffffff)

# The scalar ops f_7100060110 splits per component (0x6013c..0x60158:
# `1 << (op - 0x66) & 0x2601011`): COS EX2 LG2 RCP RSQ SIN.
SPLIT_SCALAR_OPS = (Op.COS, Op.EX2, Op.LG2, Op.RCP, Op.RSQ, Op.SIN)

# type codes (notes/37, the suffix each prints)
F32 = Type.F32                  # .F32 / .F
S32 = Type.S32                  # .S32 / .S
U32 = Type.U32                  # .U32 / .U
# .U64; the TRUNC and I2F of `step` and `sign` are typed with it, and 10, 12
# and 14 all print the short `.U` there (notes/39)
U64 = Type.U64
