"""spv.py -- read a SPIR-V module the way GLSLC 17.24 reads it.

This is a parser, not a converter.  It stops exactly where the compiler's own
reader stops and it makes the same distinctions the compiler makes, because the
converter above it has to make the compiler's decisions and cannot do that on
top of a parse that has already smoothed something over.

Three places where "the way GLSLC reads it" is narrower than the SPIR-V spec,
each read out of the image rather than assumed:

  * THE MAGIC IS NOT BYTE-SWAPPED.  f_7100fcbfa0 compares word 0 against
    0x07230203 with no swap and reports "SPIR-V: Invalid magic number"
    otherwise, so a big-endian module is rejected rather than handled.
  * THE SIZE IS IN BYTES.  f_7100fcc16c advances the cursor (ctx+112) by 4 per
    word and compares it against ctx+32, which is where GLSLC receives
    GLSLCinput.spirvModuleSizes[i].
  * AN INSTRUCTION IS (word0 & 0xffff, word0 >> 16).  A word count of 0 would
    not terminate; the compiler walks off the end into error 8003 instead, so
    this reports it as an error at the instruction rather than looping.

Which opcodes are accepted where is not decided here -- spvgrammar.py carries
the compiler's two dispatch tables and Module.check() reports what they say.
"""

import struct

# STORAGECLASS was used by an earlier `check()` that classified globals; the
# reader no longer interprets storage classes at all, so it is not imported.
from spvgrammar import (OPS, NAME, MODULE_HANDLER, BODY_HANDLER,
                        DECORATION, EXECUTIONMODEL, MAGIC as KHRONOS_MAGIC)
from spvnames import Op

# Khronos's number, not a copy of it: `spvgrammar.MAGIC` is generated from
# `spirv.py`'s `MagicNumber` by tools/mkgrammar.py, and 0xfcbfe4 compares
# word 0 against exactly that.  Kept as a module attribute because callers
# import `spv.MAGIC`.
MAGIC = KHRONOS_MAGIC

# Opcodes this parser must know to find its way around a module.  They are
# structural, not semantic: where a function starts and ends, which
# instruction carries a result, which carries a name.  The numbers are
# Khronos's (`spvnames.Op`); these aliases are kept because other modules
# import them.
OP_NAME = Op.OpName
OP_MEMBER_NAME = Op.OpMemberName
OP_ENTRY_POINT = Op.OpEntryPoint
OP_EXECUTION_MODE = Op.OpExecutionMode
OP_EXECUTION_MODE_ID = Op.OpExecutionModeId
OP_CAPABILITY = Op.OpCapability
OP_EXT_INST_IMPORT = Op.OpExtInstImport
OP_MEMORY_MODEL = Op.OpMemoryModel
OP_DECORATE = Op.OpDecorate
OP_MEMBER_DECORATE = Op.OpMemberDecorate
OP_DECORATION_GROUP = Op.OpDecorationGroup
OP_GROUP_DECORATE = Op.OpGroupDecorate
OP_GROUP_MEMBER_DECORATE = Op.OpGroupMemberDecorate
OP_VARIABLE = Op.OpVariable
OP_FUNCTION = Op.OpFunction
OP_FUNCTION_PARAMETER = Op.OpFunctionParameter
OP_FUNCTION_END = Op.OpFunctionEnd
OP_LABEL = Op.OpLabel
# the type declarations are the one contiguous range of the core grammar
OP_TYPE_FIRST, OP_TYPE_LAST = Op.OpTypeVoid, Op.OpTypeForwardPointer
# ... and the ones outside it that the module scope also files as types.
# (OpNamedBarrierInitialize is not a type; it has always been filed here, and
# no GLSL source produces it.)
OP_TYPES_OUTSIDE_RANGE = (Op.OpTypePipeStorage, Op.OpTypeNamedBarrier,
                          Op.OpNamedBarrierInitialize,
                          Op.OpTypeCooperativeMatrixKHR)
OP_CONSTANTS = (Op.OpConstantTrue, Op.OpConstantFalse, Op.OpConstant,
                Op.OpConstantComposite, Op.OpConstantSampler,
                Op.OpConstantNull, Op.OpSpecConstantTrue,
                Op.OpSpecConstantFalse, Op.OpSpecConstant,
                Op.OpSpecConstantComposite, Op.OpSpecConstantOp)
OP_ARRAY_TYPES = (Op.OpTypeArray, Op.OpTypeRuntimeArray)


class SpvError(Exception):
    pass


class Insn(object):
    """One instruction: its opcode, its word count, and its operand words.

    `operands` excludes the opcode word, matching what f_7100fcc16c hands its
    handlers -- there, operands[0] is the instruction's first operand.
    """

    __slots__ = ("index", "offset", "opcode", "wordcount", "operands")

    def __init__(self, index, offset, opcode, wordcount, operands):
        self.index = index
        self.offset = offset
        self.opcode = opcode
        self.wordcount = wordcount
        self.operands = operands

    @property
    def name(self):
        return NAME.get(self.opcode, "Op?%d" % self.opcode)

    # -- the result-id / result-type conventions, from the grammar ---------
    @property
    def has_result_type(self):
        ops = OPS.get(self.opcode)
        return bool(ops and ops[3] and ops[3][0]["kind"] == "IdResultType")

    @property
    def has_result(self):
        ops = OPS.get(self.opcode)
        if not ops or not ops[3]:
            return False
        return any(o["kind"] == "IdResult" for o in ops[3][:2])

    @property
    def result_type(self):
        return self.operands[0] if self.has_result_type else None

    @property
    def result(self):
        if not self.has_result:
            return None
        return self.operands[1] if self.has_result_type else self.operands[0]

    def args(self):
        """The operands after the result-type and result ids."""
        n = (1 if self.has_result_type else 0) + (1 if self.has_result else 0)
        return self.operands[n:]

    def ref_positions(self):
        """The operand indexes that hold IdRef words, from the grammar.

        Single-word kinds are walked one word each, a trailing `*` IdRef
        takes every remaining word, and a pair kind takes two with the
        second an IdRef.  At any kind whose width the grammar does not fix
        (a string, a context-dependent number, an optional operand) the walk
        STOPS: the positions after it are not claimed, so nothing past it is
        ever rewritten as an id."""
        ops = OPS.get(self.opcode)
        out = []
        if not ops or not ops[3]:
            return out
        i = 0
        n = len(self.operands)
        one_word = ("IdRef", "IdResult", "IdResultType", "IdScope",
                    "IdMemorySemantics", "LiteralInteger",
                    "LiteralExtInstInteger")
        for o in ops[3]:
            k, q = o["kind"], o["quantifier"]
            if q == "*":
                if k == "IdRef":
                    out.extend(range(i, n))
                elif k == "PairLiteralIntegerIdRef":
                    out.extend(range(i + 1, n, 2))
                elif k == "PairIdRefLiteralInteger":
                    out.extend(range(i, n, 2))
                elif k == "PairIdRefIdRef":
                    out.extend(range(i, n))
                return out
            if q or k not in one_word:
                return out
            if i >= n:
                return out
            if k == "IdRef":
                out.append(i)
            i += 1
        return out

    def string_at(self, i):
        """The NUL-terminated, word-padded UTF-8 literal starting at operand i."""
        raw = b"".join(struct.pack("<I", w) for w in self.operands[i:])
        z = raw.find(b"\0")
        if z < 0:
            raise SpvError("unterminated string in %s" % self.name)
        return raw[:z].decode("utf-8", "replace")

    def string_words(self, i):
        """How many operand words the literal starting at i occupies."""
        n = 0
        for w in self.operands[i:]:
            n += 1
            if (w & 0xFF) == 0 or (w >> 8 & 0xFF) == 0 or \
               (w >> 16 & 0xFF) == 0 or (w >> 24 & 0xFF) == 0:
                break
        return n

    def __repr__(self):
        return "%-28s %s" % (self.name, " ".join("%d" % w for w in self.operands))


class Function(object):
    __slots__ = ("insn", "result", "type_id", "control", "params", "blocks", "insns")

    def __init__(self, insn):
        self.insn = insn
        self.result = insn.operands[1]
        self.control = insn.operands[2]
        self.type_id = insn.operands[3]
        self.params = []
        self.blocks = []          # [(label_id, [Insn, ...]), ...]
        self.insns = []           # every body instruction, in module order

    def __repr__(self):
        return "<Function %%%d, %d block(s)>" % (self.result, len(self.blocks))


class Module(object):
    """A parsed SPIR-V module, with the tables GLSLC builds beside it.

    The id-keyed tables mirror the reader's own id table (ctx+104, 104 bytes per
    id): one slot per <id>, filled in as the declaration for it goes past.
    """

    def __init__(self, data):
        if len(data) < 20:
            raise SpvError("module is %d bytes; the header alone is 20" % len(data))
        if len(data) & 3:
            raise SpvError("module is %d bytes, not a whole number of words" % len(data))
        w = struct.unpack("<%dI" % (len(data) // 4), data)
        if w[0] != MAGIC:
            sw = struct.unpack(">I", struct.pack("<I", w[0]))[0]
            if sw == MAGIC:
                raise SpvError(
                    "big-endian module: GLSLC compares the magic word without "
                    "swapping (f_7100fcbfa0) and rejects it")
            raise SpvError("bad magic 0x%08x" % w[0])
        self.words = w
        self.version = w[1]
        self.generator = w[2]
        self.bound = w[3]
        self.schema = w[4]

        self.insns = []
        self.capabilities = []
        self.extensions = []
        self.ext_inst_imports = {}         # id -> name
        self.memory_model = None           # (addressing, memory)
        self.entry_points = []             # (model, id, name, [interface ids])
        self.execution_modes = []          # (entry id, mode, [literals])
        self.names = {}                    # id -> str
        self.member_names = {}             # (id, member) -> str
        self.decorations = {}              # id -> [(Decoration, [literals])]
        self.member_decorations = {}       # (id, member) -> [(Decoration, [lits])]
        self.types = {}                    # id -> Insn (OpType*)
        self.constants = {}                # id -> Insn (OpConstant* / OpSpecConstant*)
        self.globals = {}                  # id -> Insn (module-scope OpVariable)
        self.undefs = {}                   # id -> Insn
        self.functions = []
        self.result_insn = {}              # id -> the Insn that defines it
        # Deferred until every group's own decorations have been seen; SPIR-V
        # does not require OpDecorationGroup to precede its OpGroupDecorate.
        self._group_decorate = []
        self._group_member_decorate = []

        self._parse()
        self._resolve_decoration_groups()

    # -- parsing ----------------------------------------------------------
    def _parse(self):
        w = self.words
        n = len(w)
        i = 5
        index = 0
        cur_fn = None
        cur_block = None
        while i < n:
            word0 = w[i]
            opcode = word0 & 0xFFFF
            wc = word0 >> 16
            if wc == 0:
                raise SpvError("word count 0 at word %d (%s); the module cannot "
                               "be walked past it" % (i, NAME.get(opcode, opcode)))
            if i + wc > n:
                raise SpvError("instruction at word %d claims %d words but only "
                               "%d remain -- error 8003, \"Unexpected end of "
                               "SPIR-V module\"" % (i, wc, n - i))
            index += 1
            ins = Insn(index, i, opcode, wc, list(w[i + 1:i + wc]))
            self.insns.append(ins)
            if ins.has_result:
                self.result_insn[ins.result] = ins

            if cur_fn is None:
                self._module_scope(ins)
                if opcode == OP_FUNCTION:
                    cur_fn = Function(ins)
                    self.functions.append(cur_fn)
                    cur_block = None
            else:
                if opcode == OP_FUNCTION_END:
                    cur_fn = None
                    cur_block = None
                elif opcode == OP_FUNCTION_PARAMETER:
                    cur_fn.params.append(ins)
                else:
                    cur_fn.insns.append(ins)
                    if opcode == OP_LABEL:
                        cur_block = (ins.operands[0], [])
                        cur_fn.blocks.append(cur_block)
                    elif cur_block is not None:
                        cur_block[1].append(ins)
            i += wc

    def _module_scope(self, ins):
        op = ins.opcode
        if op == OP_CAPABILITY:
            self.capabilities.append(ins.operands[0])
        elif op == Op.OpExtension:
            self.extensions.append(ins.string_at(0))
        elif op == OP_EXT_INST_IMPORT:
            self.ext_inst_imports[ins.operands[0]] = ins.string_at(1)
        elif op == OP_MEMORY_MODEL:
            self.memory_model = (ins.operands[0], ins.operands[1])
        elif op == OP_ENTRY_POINT:
            name = ins.string_at(2)
            nw = ins.string_words(2)
            self.entry_points.append((ins.operands[0], ins.operands[1], name,
                                      list(ins.operands[2 + nw:])))
        elif op in (OP_EXECUTION_MODE, OP_EXECUTION_MODE_ID):
            self.execution_modes.append((ins.operands[0], ins.operands[1],
                                         list(ins.operands[2:])))
        elif op == OP_NAME:
            self.names[ins.operands[0]] = ins.string_at(1)
        elif op == OP_MEMBER_NAME:
            self.member_names[(ins.operands[0], ins.operands[1])] = ins.string_at(2)
        elif op == OP_DECORATE:
            self.decorations.setdefault(ins.operands[0], []).append(
                (ins.operands[1], list(ins.operands[2:])))
        elif op == OP_MEMBER_DECORATE:
            self.member_decorations.setdefault(
                (ins.operands[0], ins.operands[1]), []).append(
                (ins.operands[2], list(ins.operands[3:])))
        elif op == OP_DECORATION_GROUP:
            self.decorations.setdefault(ins.operands[0], [])
        elif op == OP_GROUP_DECORATE:
            self._group_decorate.append(ins)
        elif op == OP_GROUP_MEMBER_DECORATE:
            self._group_member_decorate.append(ins)
        elif (OP_TYPE_FIRST <= op <= OP_TYPE_LAST
              or op in OP_TYPES_OUTSIDE_RANGE):
            self.types[ins.operands[0]] = ins
        elif op in OP_CONSTANTS:
            self.constants[ins.result] = ins
        elif op == OP_VARIABLE:
            self.globals[ins.result] = ins
        elif op == Op.OpUndef:
            self.undefs[ins.result] = ins

    def _resolve_decoration_groups(self):
        """OpGroupDecorate copies a group's decorations onto its targets.

        Done here rather than left to the caller because GLSLC does it in the
        reader (0x7100fdd740 / 0x7100fdd794) and everything downstream of the
        reader sees only the result.
        """
        for ins in self._group_decorate:
            group = ins.operands[0]
            for target in ins.operands[1:]:
                for d in self.decorations.get(group, []):
                    self.decorations.setdefault(target, []).append(d)
        for ins in self._group_member_decorate:
            group = ins.operands[0]
            rest = ins.operands[1:]
            for k in range(0, len(rest) - 1, 2):
                target, member = rest[k], rest[k + 1]
                for d in self.decorations.get(group, []):
                    self.member_decorations.setdefault(
                        (target, member), []).append(d)
        self._group_decorate = []
        self._group_member_decorate = []

    # -- queries ----------------------------------------------------------
    def decoration(self, id_, which):
        """The literals of decoration `which` on `id_`, or None."""
        for d, lits in self.decorations.get(id_, []):
            if d == which:
                return lits
        return None

    def member_decoration(self, id_, member, which):
        for d, lits in self.member_decorations.get((id_, member), []):
            if d == which:
                return lits
        return None

    def member_builtins(self, var_id):
        """(member index, BuiltIn) for each member of an interface block.

        An interface BLOCK -- `gl_PerVertex` is the one every stage has --
        carries no Location and no BuiltIn of its own: each member is decorated
        instead, so a caller that only looks at the variable sees nothing.  The
        pointee of the variable's pointer type is followed through an array, so
        `gl_in[]` and a plain `out gl_PerVertex` both answer.
        """
        tid = self._block_struct(var_id)
        if tid is None:
            return []
        t = self.types[tid]
        out = []
        for m in range(len(t.operands) - 1):
            b = self.member_decoration(tid, m, DECORATION["BuiltIn"])
            if b is not None:
                out.append((m, b[0]))
        return out

    def _pointee_past_arrays(self, var_id):
        """`(type id, type insn, array depth)` behind a global's pointer.

        The pointee is followed through every array level.  The type insn
        is None when the walk meets an undeclared type or a cycle; the whole
        answer is None when the variable or its pointer type is missing.
        """
        ins = self.globals.get(var_id)
        if ins is None:
            return None
        ptr = self.types.get(ins.result_type)
        if ptr is None or ptr.opcode != Op.OpTypePointer:
            return None
        tid = ptr.operands[2]
        depth = 0
        seen = set()
        while True:
            t = self.types.get(tid)
            if t is None or tid in seen:
                return tid, None, depth
            seen.add(tid)
            if t.opcode not in OP_ARRAY_TYPES:
                return tid, t, depth
            depth += 1
            tid = t.operands[1]

    def _block_struct(self, var_id):
        """The struct type id behind an interface-block variable, or None."""
        w = self._pointee_past_arrays(var_id)
        if w is None or w[1] is None or w[1].opcode != Op.OpTypeStruct:
            return None
        return w[0]

    def block_array_depth(self, var_id):
        """How many array levels sit between the pointer and the struct.

        `gl_out[]` in a tessellation-control shader is an ARRAY of
        `gl_PerVertex`, so an access chain into it indexes the array first and
        the member second; a plain `out gl_PerVertex` has no such level.  A
        walk that assumes the first index is the member number reads the
        invocation id as a member number and, when it is dynamic, refuses a
        shader it could have handled.
        """
        w = self._pointee_past_arrays(var_id)
        return 0 if w is None else w[2]

    def member_is_array(self, var_id, member):
        """Is that member of an interface block an array type?

        `gl_ClipDistance` is and takes one binding slot per element;
        `gl_Position` is a vector and takes ONE slot however many of its
        components an access chain names.  Without the distinction a shader
        that writes `gl_Position.xyzw` component-wise declares four slots.
        """
        tid = self._block_struct(var_id)
        if tid is None:
            return False
        t = self.types[tid]
        if member >= len(t.operands) - 1:
            return False
        mt = self.types.get(t.operands[1 + member])
        return mt is not None and mt.opcode in OP_ARRAY_TYPES

    def member_locations(self, var_id):
        """(member index, Location) for each member of an interface block.

        An output block whose members carry Location rather than BuiltIn --
        what an HLSL-to-SPIR-V translation produces for a varying struct -- has
        no decoration on the variable at all, so a caller that only reads the
        variable's own Location sees nothing and emits no declaration for it.
        """
        tid = self._block_struct(var_id)
        if tid is None:
            return []
        t = self.types[tid]
        out = []
        for m in range(len(t.operands) - 1):
            loc = self.member_decoration(tid, m, DECORATION["Location"])
            if loc is not None:
                out.append((m, loc[0]))
        return out

    def member_array_length(self, var_id, member):
        """The array length of one member of an interface block, or None.

        `gl_ClipDistance` inside `gl_PerVertex` is an array, and it occupies
        one binding slot per element, so the block walk needs its length.
        Returns None when the member is not an array or the length is not a
        literal constant.
        """
        tid = self._block_struct(var_id)
        if tid is None:
            return None
        t = self.types[tid]
        if member >= len(t.operands) - 1:
            return None
        mt = self.types.get(t.operands[1 + member])
        if mt is None or mt.opcode != Op.OpTypeArray:
            return None
        c = self.constants.get(mt.operands[2])
        if c is None or c.opcode != Op.OpConstant:
            return None
        return c.operands[2]

    def name_of(self, id_):
        return self.names.get(id_)

    def entry_point(self, want="main"):
        for model, id_, name, iface in self.entry_points:
            if name == want:
                return (model, id_, name, iface)
        return None

    def check(self):
        """What GLSLC's own dispatch tables say about this module.

        Returns (module_unhandled, body_unhandled) as lists of (Insn, reason).
        An opcode with no handler in the relevant table reaches error 8001,
        "SPIR-V: Invalid %s", so this is a prediction of a real refusal rather
        than an opinion about the module.
        """
        mod_bad, body_bad = [], []
        in_fn = False
        for ins in self.insns:
            op = ins.opcode
            if op == OP_FUNCTION:
                in_fn = True
                continue
            if op == OP_FUNCTION_END:
                in_fn = False
                continue
            if in_fn:
                # The declaration pass (f_7100fdd0d0) accepts OpLabel, OpLine
                # and OpNoLine itself; everything else in a body goes through
                # the body table.
                if op in (OP_LABEL, Op.OpLine, Op.OpNoLine,
                          OP_FUNCTION_PARAMETER):
                    continue
                if op not in BODY_HANDLER:
                    body_bad.append((ins, "no body handler"))
            else:
                if op not in MODULE_HANDLER:
                    mod_bad.append((ins, "no module handler"))
        return mod_bad, body_bad


def load(path):
    with open(path, "rb") as fh:
        return Module(fh.read())


if __name__ == "__main__":
    import sys
    m = load(sys.argv[1])
    print("version %d.%d  generator 0x%08x  bound %d  %d instructions"
          % (m.version >> 16 & 0xFF, m.version >> 8 & 0xFF, m.generator,
             m.bound, len(m.insns)))
    for model, id_, name, iface in m.entry_points:
        print("entry point %-14s %%%d %r  interface %s"
              % (list(EXECUTIONMODEL)[list(EXECUTIONMODEL.values()).index(model)]
                 if model in EXECUTIONMODEL.values() else model,
                 id_, name, iface))
    print("types %d  constants %d  globals %d  functions %d"
          % (len(m.types), len(m.constants), len(m.globals), len(m.functions)))
    mb, bb = m.check()
    for ins, why in mb + bb:
        print("UNHANDLED %s: %s" % (why, ins))
