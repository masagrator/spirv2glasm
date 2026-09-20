"""header.py -- the listing's head: profile, OPTIONs, stage directives.

This is the converter's output side, and it is deliberately partial.  Each
section is here only if the rule behind it was read out of the compiler
(the printer's own literal lines, in notes/printer_strings.tsv, plus the code
that chooses between them) or measured against the oracle over the probe set.

WHAT THE PRINTER ACTUALLY HOLDS.  The listing is not assembled from fragments.
The printer keeps whole lines as literals in .rodata, with printf conversions
where a number or name goes:

    'OPTION NV_unroll_none;\\n'          0x7101160ff  (whole line)
    'OUTPUT result_color%d = result.color[%d];\\n'
    'STORAGE sbo_buf%d[] = { program.storage[%d] };\\n'
    'TEMP lmem%d[%d];\\n'
    '# %d instructions, %d R-regs'

so the emitter's job is to choose lines and fill conversions, not to invent
syntax.  tools/printerstrings.py re-extracts that vocabulary from the image.
"""
from spvnames import Op, ExecutionModel, ExecutionMode, StorageClass, \
    Decoration

from glasmlib.common import NotEstablished, HANDLE_TYPES, DREF_OPS, ENV

# ---------------------------------------------------------------------------
# The profile line
#
# One literal per profile, all present in the image; the name-recovery map
# attributes five of them to functions whose only rodata reference is the
# literal (glasm_EmitHeader_vp5 at 0x7100d5ab60 -> "!!NVvp5.0", and so on).
# The stage comes from the entry point's execution model, which is also what
# spirv2glasm.c uses to pick the NVNshaderStage.
# ---------------------------------------------------------------------------
# The `#profile` name is NOT the magic with the punctuation removed, which is
# what an eye would fill in: tessellation control is `gp5hp` and tessellation
# evaluation is `gp5tp`, not `gp5tcp`/`gp5tep`.  Both spellings are literals in
# the image (0x710115eeb7 and 0x710114fa8b) and both are what the oracle emits.
PROFILE = {
    ExecutionModel.Vertex:                 ("!!NVvp5.0",  "gp5vp"),
    ExecutionModel.TessellationControl:    ("!!NVtcp5.0", "gp5hp"),
    ExecutionModel.TessellationEvaluation: ("!!NVtep5.0", "gp5tp"),
    ExecutionModel.Geometry:               ("!!NVgp5.0",  "gp5gp"),
    ExecutionModel.Fragment:               ("!!NVfp5.0",  "gp5fp"),
    ExecutionModel.GLCompute:              ("!!NVcp5.0",  "gp5cp"),
}


# ---------------------------------------------------------------------------
# The OPTION block
#
# Measured over the 90 probes: three options are on every listing the SPIR-V
# front end produces, in this order, and three more appear conditionally.
#
#   OPTION NV_unroll_none;        90/90
#   OPTION NV_internal;           90/90
#   OPTION NV_bindless_texture;   90/90
#   OPTION NV_gpu_program_fp64;    5/90
#   OPTION ARB_draw_buffers;       1/90
#   OPTION NV_shader_storage_buffer; 1/90
#
# The three unconditional ones are emitted here.  The conditions behind the
# other three are NOT established -- the probe set shows WHICH shaders carry
# them, not the test the compiler applies -- so they are requested explicitly
# by the caller and the caller has to have a reason.  `NV_unroll_none` is
# almost certainly the --unroll-control option rather than a property of the
# module (the image holds 'OPTION NV_unroll_all;' beside it), which is why it
# is listed under flags rather than under the module.
# ---------------------------------------------------------------------------
ALWAYS_OPTIONS = ("NV_unroll_none", "NV_internal", "NV_bindless_texture")

# The order options appear in, from the probe listings.  Where two options both
# occur, the earlier one in this tuple came first in every listing that had
# both.  Note that it is NOT alphabetical and NOT "conditional ones last":
# NV_gpu_program_fp64 and NV_shader_storage_buffer come BEFORE
# NV_bindless_texture and ARB_draw_buffers comes after it, so the three
# always-on options are not a contiguous block.
OPTION_ORDER = ("NV_unroll_none", "NV_internal", "NV_gpu_program_fp64",
                "NV_shader_storage_buffer", "NV_bindless_texture",
                "ARB_draw_buffers", "ARB_fragment_program_shadow",
                "NV_early_fragment_tests",
                # after NV_bindless_texture in the corpus's four compute
                # shaders that query an image's size (notes/111); its place
                # against the three above is not measured (`module_options`
                # refuses the combinations)
                "ARB_shader_image_size")


# The three conditional options.  Each condition below is MEASURED over the 90
# probes and not read out of the compiler, which is why it says so here: the
# probe set shows a perfect correlation in both directions, but a correlation
# over 90 shaders is not the test the compiler applies.
#
#   NV_gpu_program_fp64       on all 5 probes that declare a sampler and on
#                             none of the other 85.  A bindless texture handle
#                             is 64 bits -- the listings show `LONG TEMP D0;`
#                             and `LDC.U64 D0.x, buf14[0];` -- so the option is
#                             about 64-bit registers, not about `double`.  The
#                             `OpTypeFloat 64` / `OpTypeInt 64` half of the
#                             condition below is NOT exercised by any probe.
#   ARB_draw_buffers          on the one probe with two colour outputs, and on
#                             none of the 11 fragment probes with one.
#   NV_shader_storage_buffer  on the one probe with an SSBO.
#
# Two more came out of the CORPUS rather than the probes -- the probe set has
# no shadow sampler and no EarlyFragmentTests, so it could not have shown them.
# Both were then checked over 517 real shaders in both directions, with zero
# mismatches:
#
#   ARB_fragment_program_shadow  139/517, exactly the shaders that either use a
#                             depth-compare sample (OpImageSampleDref* /
#                             OpImageDrefGather and their sparse forms) or
#                             declare an OpTypeImage with Depth = 1.
#   NV_early_fragment_tests   146/517, exactly the shaders whose entry point
#                             carries ExecutionMode EarlyFragmentTests.
#                             This one IS read rather than measured: the mode
#                             has its own arm at 0x7100fccad8
#                             (notes/tables.json).

def _uses_64bit_or_sampler(module):
    # A HANDLE THE BODY LOADS, not a declared type: the cut `mq_n8.frag`
    # declares `Tex0`/`Smpl0` and never samples, and its listing has no
    # `NV_gpu_program_fp64` (notes/87).  The handles are the 64-bit values.
    _handle_types = set(t for t, ins in module.types.items()
                        if ins.opcode in HANDLE_TYPES)
    if any(i.opcode == Op.OpLoad and getattr(i, "has_result_type", False)
           and i.result_type in _handle_types for i in module.insns):
        return True
    for ins in module.types.values():
        if ins.opcode in (Op.OpTypeInt, Op.OpTypeFloat) \
                and ins.operands[1] == 64:
            return True
    return False


def _uses_shadow(module):
    if any(i.opcode in DREF_OPS for i in module.insns):
        return True
    # OpTypeImage: result, sampled type, Dim, Depth, Arrayed, MS, Sampled, Format
    return any(i.opcode == Op.OpTypeImage and i.operands[3] == 1
               for i in module.types.values())


def _early_fragment_tests(module, entry_name):
    ep = module.entry_point(entry_name)
    return any(eid == ep[1] and mode == ExecutionMode.EarlyFragmentTests
               for eid, mode, _ in module.execution_modes)


def _uses_storage_buffer(module):
    for id_, ins in module.globals.items():
        if ins.operands[2] == StorageClass.StorageBuffer:
            return True
        # A `buffer` block written for OpenGL-flavoured SPIR-V is a Uniform
        # variable whose struct type carries BufferBlock, not a StorageBuffer
        # variable.  Both spellings are accepted by the compiler
        # (notes/03-variables.md), so both are tested here.
        if ins.operands[2] == StorageClass.Uniform:
            ptr = module.types.get(ins.result_type)
            if ptr is not None and ptr.opcode == Op.OpTypePointer:
                if module.decoration(ptr.operands[2],
                                     Decoration.BufferBlock) is not None:
                    return True
    return False


def _colour_output_count(module, entry_name):
    """How many COLOUR outputs the fragment stage has.

    This is `f_7100038ca0` (notes/22): a walk of the symbol list counting the
    leaves whose binding kind is in the colour range, and it decides
    `OPTION ARB_draw_buffers` -- emitted when the count is two or more.

    `gl_FragDepth` is not one of them.  It is an output, it is in the entry
    point's interface, and it takes binding kind 0xd2 rather than a colour kind
    (notes/17), so counting every Output made `post_init_small_vfx.frag` -- one
    colour target plus a depth write -- emit an option the compiler does not.
    A BuiltIn output is excluded here for that reason; a Location output is
    counted.
    """
    ep = module.entry_point(entry_name)
    n = 0
    for id_ in ep[3]:
        ins = module.globals.get(id_)
        if ins is None or ins.operands[2] != StorageClass.Output:
            continue
        if module.decoration(id_, Decoration.Location) is None:
            continue                       # gl_FragDepth, $kill: not colours
        n += 1
    return n


def module_options(module, entry_name="main"):
    """The conditional options this module asks for.  See the note above."""
    out = []
    if _uses_64bit_or_sampler(module):
        out.append("NV_gpu_program_fp64")
    if module.entry_point(entry_name)[0] == ExecutionModel.Fragment and \
            _colour_output_count(module, entry_name) > 1:
        out.append("ARB_draw_buffers")
    if _uses_storage_buffer(module):
        out.append("NV_shader_storage_buffer")
    _compute = module.entry_point(entry_name)[0] == ExecutionModel.GLCompute
    if _uses_shadow(module) and not (_compute
                                     and not ENV.get("G2S_CSSHADOWOPT")):
        # NOT IN A COMPUTE PROGRAM (notes/111): the corpus's
        # `compute_volumefog_injection` samples with a depth compare
        # (OpImageSampleDrefExplicitLod, a Depth-1 image) and its listing
        # has no ARB_fragment_program_shadow
        out.append("ARB_fragment_program_shadow")
    if _early_fragment_tests(module, entry_name):
        out.append("NV_early_fragment_tests")
    if any(i.opcode == Op.OpImageQuerySize for i in module.insns):
        # `imageSize()` (notes/111): the corpus's `compute_atmosphere_scatter`
        # and `compute_skydome` print `OPTION ARB_shader_image_size;` after
        # NV_bindless_texture
        if not _compute or len(out) and set(out) - {
                "NV_gpu_program_fp64", "NV_shader_storage_buffer"}:
            raise NotEstablished("ARB_shader_image_size beside another "
                                 "late option, or outside compute: its "
                                 "place is not measured")
        out.append("ARB_shader_image_size")
    return out


def option_block(extra=()):
    """The OPTION lines, in the order the printer emits them."""
    want = set(ALWAYS_OPTIONS) | set(extra)
    unknown = want - set(OPTION_ORDER)
    if unknown:
        raise NotEstablished(
            "no established position in the OPTION block for %s; "
            "notes/printer_strings.tsv lists every option line the printer "
            "holds, but not the order it emits them in"
            % ", ".join(sorted(unknown)))
    return ["OPTION %s;" % o for o in OPTION_ORDER if o in want]


# ---------------------------------------------------------------------------
# The stage-directive block
#
# Between the OPTION lines and the comment header, a geometry, tessellation or
# compute program states its layout.  Every line and every token below is a
# literal in the image -- the lines are in notes/printer_strings.tsv and the
# tokens were located by searching .rodata for each spelling:
#
#     'PRIMITIVE_IN %s;\n'      0x710114c1b6     'TESS_MODE %s;\n'     0x7101 15c8f6
#     'PRIMITIVE_OUT %s;\n'     0x710114f35a     'TESS_SPACING %s;\n'  0x710115a7ff
#     'VERTICES_OUT %d;\n'      0x710115a838     'TESS_VERTEX_ORDER %s;\n' 0x7101156705
#     'INVOCATIONS %d;\n'       0x7101159a0c     'TESS_POINT_MODE;\n'  0x710115e963
#     'GROUP_SIZE %d;\n'        0x7101152755
#
# and the tokens POINTS, LINES, LINES_ADJACENCY, TRIANGLES,
# TRIANGLES_ADJACENCY, QUADS, ISOLINES, LINE_STRIP, TRIANGLE_STRIP, EQUAL,
# FRACTIONAL_EVEN, FRACTIONAL_ODD, CW, CCW are each a NUL-terminated string of
# their own in .rodata.
#
# Which SPIR-V execution mode feeds which directive is the map in
# notes/tables.json: `f_7100fcc784` gives every one of these modes an arm of
# its own -- the tessellation set, the geometry set and LocalSize.

# SpvExecutionMode -> the token the printer writes for it.
PRIMITIVE_IN = {ExecutionMode.InputPoints: "POINTS",
                ExecutionMode.InputLines: "LINES",
                ExecutionMode.InputLinesAdjacency: "LINES_ADJACENCY",
                ExecutionMode.Triangles: "TRIANGLES",
                ExecutionMode.InputTrianglesAdjacency: "TRIANGLES_ADJACENCY"}
PRIMITIVE_OUT = {ExecutionMode.OutputPoints: "POINTS",
                 ExecutionMode.OutputLineStrip: "LINE_STRIP",
                 ExecutionMode.OutputTriangleStrip: "TRIANGLE_STRIP"}
TESS_MODE = {ExecutionMode.Triangles: "TRIANGLES",
             ExecutionMode.Quads: "QUADS",
             ExecutionMode.Isolines: "ISOLINES"}
TESS_SPACING = {ExecutionMode.SpacingEqual: "EQUAL",
                ExecutionMode.SpacingFractionalEven: "FRACTIONAL_EVEN",
                ExecutionMode.SpacingFractionalOdd: "FRACTIONAL_ODD"}
TESS_VERTEX_ORDER = {ExecutionMode.VertexOrderCw: "CW",
                     ExecutionMode.VertexOrderCcw: "CCW"}

# Defaults, measured on the probes rather than read: a geometry program with no
# Invocations mode still prints `INVOCATIONS 1;`, and a tessellation-evaluation
# program with no VertexOrder mode still prints `TESS_VERTEX_ORDER CCW;`.  They
# are marked as measured because the arm that supplies them has not been read.
DEFAULT_INVOCATIONS = 1              # measured: 9/9 geometry probes
DEFAULT_VERTEX_ORDER = "CCW"         # measured: 6/6 tess-evaluation probes


def _tokens(modes, table, form):
    return [form % tok for m, tok in table.items() if m in modes]


def _geometry_directives(modes):
    out = _tokens(modes, PRIMITIVE_IN, "PRIMITIVE_IN %s;")
    out += _tokens(modes, PRIMITIVE_OUT, "PRIMITIVE_OUT %s;")
    if ExecutionMode.OutputVertices in modes:
        out.append("VERTICES_OUT %d;" % modes[ExecutionMode.OutputVertices][0])
    out.append("INVOCATIONS %d;" % (modes[ExecutionMode.Invocations][0]
                                    if ExecutionMode.Invocations in modes
                                    else DEFAULT_INVOCATIONS))
    return out


def _tess_evaluation_directives(modes):
    out = _tokens(modes, TESS_MODE, "TESS_MODE %s;")
    out += _tokens(modes, TESS_SPACING, "TESS_SPACING %s;")
    order = DEFAULT_VERTEX_ORDER
    for m, tok in TESS_VERTEX_ORDER.items():
        if m in modes:
            order = tok
    out.append("TESS_VERTEX_ORDER %s;" % order)
    if ExecutionMode.PointMode in modes:
        out.append("TESS_POINT_MODE;")
    return out


def stage_directives(module, entry_name="main"):
    """The layout block for geometry, tessellation and compute programs."""
    ep = module.entry_point(entry_name)
    modes = {m: lits for eid, m, lits in module.execution_modes if eid == ep[1]}
    model = ep[0]
    if model == ExecutionModel.Geometry:
        return _geometry_directives(modes)
    if model == ExecutionModel.TessellationControl:
        if ExecutionMode.OutputVertices in modes:
            return ["VERTICES_OUT %d;"
                    % modes[ExecutionMode.OutputVertices][0]]
        return []
    if model == ExecutionModel.TessellationEvaluation:
        return _tess_evaluation_directives(modes)
    if model == ExecutionModel.GLCompute:
        # `GROUP_SIZE %d;` with the LocalSize dimensions, the trailing 1s
        # dropped (x always printed): measured on `cp_a`..`cp_g` -- (1,1,1)
        # `GROUP_SIZE 1;`, (8,8,1) `8 8`, (1,4,1) `1 4`, (8,1,1) `8`,
        # (4,1,2) `4 1 2`, (1,1,2) `1 1 2`
        if ExecutionMode.LocalSize not in modes:
            raise NotEstablished(
                "a compute program with no LocalSize mode: the GROUP_SIZE "
                "line for a specialised size is not measured")
        dims = list(modes[ExecutionMode.LocalSize][:3])
        while len(dims) > 1 and dims[-1] == 1:
            dims.pop()
        return ["GROUP_SIZE %s;" % " ".join("%d" % d for d in dims)]
    return []


# ---------------------------------------------------------------------------
# The comment header
#
# Fixed text plus the profile and the entry point name.  The cgc version line
# is the front end's own build stamp and is the same on every listing this
# compiler produces; it is a property of the binary, not of the module.
# ---------------------------------------------------------------------------
CGC_VERSION_LINE = "# cgc version 3.4.0001, build date Jun 10 2024"
CGC_VERSION_LINE2 = "#version 3.4.0.1 COP Build Date Jun 10 2024"


def comment_header(profile_name, program_name):
    return [
        CGC_VERSION_LINE,
        "# command line args: ",
        "#vendor NVIDIA Corporation",
        CGC_VERSION_LINE2,
        "#profile %s" % profile_name,
        "#program %s" % program_name,
    ]


def header(module, entry_name="main"):
    """Everything above the `#var` block, which is what is established."""
    ep = module.entry_point(entry_name)
    if ep is None:
        raise NotEstablished("no entry point %r in this module" % entry_name)
    model = ep[0]
    if model not in PROFILE:
        raise NotEstablished(
            "execution model %d has no GLASM profile in this compiler" % model)
    magic, profile_name = PROFILE[model]
    return ([magic] + option_block(module_options(module, entry_name))
            + stage_directives(module, entry_name)
            + comment_header(profile_name, entry_name))
