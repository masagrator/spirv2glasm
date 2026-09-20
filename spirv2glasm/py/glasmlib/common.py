"""common.py -- what every part of the output side shares.

`NotEstablished` is the converter's one refusal: raised where a rule has not
been read out of the compiler, so a listing is never partly guessed.

The opcode families below are groups of Khronos opcodes (`spvnames.Op`) that
this converter treats alike; each is named for what the members have in
common.
"""
import os

import spvgrammar
from spvnames import Op

# opcode -> its Khronos name, for messages and the opcode chain's lookups
OP_NAME = spvgrammar.NAME

# The environment switches (`G2S_*`) that turn a measured rule off, for
# comparison; read through one name so the modules agree on it.
ENV = os.environ


class NotEstablished(Exception):
    """Raised where the rule has not been read out of the compiler yet.

    Carries what was being emitted, so a failure names the next thing to go
    and read rather than just failing.
    """


# The bindless-handle types: an opaque uniform of one of these is a 64-bit
# handle in constant buffer 14 (notes/10).
HANDLE_TYPES = (Op.OpTypeImage, Op.OpTypeSampler, Op.OpTypeSampledImage)

ARRAY_TYPES = (Op.OpTypeArray, Op.OpTypeRuntimeArray)

SCALAR_TYPES = (Op.OpTypeBool, Op.OpTypeInt, Op.OpTypeFloat)

COMPOSITE_TYPES = (Op.OpTypeVector, Op.OpTypeMatrix)

ACCESS_CHAINS = (Op.OpAccessChain, Op.OpInBoundsAccessChain,
                 Op.OpPtrAccessChain)

BOOL_CONSTANTS = (Op.OpConstantTrue, Op.OpConstantFalse)

# The instructions that end an invocation's fragment: a discard and its two
# later spellings.
KILLS = (Op.OpKill, Op.OpTerminateInvocation, Op.OpDemoteToHelperInvocation)

# The depth-compare sampling opcodes, and their sparse counterparts.
DREF_OPS = frozenset((
    Op.OpImageSampleDrefImplicitLod, Op.OpImageSampleDrefExplicitLod,
    Op.OpImageSampleProjDrefImplicitLod, Op.OpImageSampleProjDrefExplicitLod,
    Op.OpImageDrefGather,
    Op.OpImageSparseSampleDrefImplicitLod,
    Op.OpImageSparseSampleDrefExplicitLod,
    Op.OpImageSparseSampleProjDrefImplicitLod,
    Op.OpImageSparseSampleProjDrefExplicitLod,
    Op.OpImageSparseDrefGather))
