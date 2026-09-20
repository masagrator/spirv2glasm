#!/usr/bin/env python3
"""mkcorpus.py -- turn the 14,706-shader GLSL corpus into SPIR-V.

WHY ANYTHING HAS TO BE DONE AT ALL.  Every shader in the corpus declares

    #extension GL_NV_separate_texture_types : require
    #extension GL_NV_gpu_shader5            : require

and glslang implements neither, so it rejects all 14,706 outright.  That is not
a property of the shaders: it is NVN's own extension pair, and what the first
of them enables -- separate `texture2D` and `sampler` objects combined at the
point of use, `textureLod(sampler2D(Tex0, Smpl0), ...)` -- is ORDINARY GLSL in
Vulkan semantics, which glslang does implement.  So the shaders are compiled
with `-V` rather than `-G`, and the only edit is dropping the two `#extension`
lines glslang does not know.

WHAT THAT CHANGES, STATED PLAINLY.  `-V` is not free: Vulkan semantics renames
a few built-ins (`gl_VertexID` -> `gl_VertexIndex`), requires descriptor sets
on every opaque uniform, and forbids default uniforms outside a block.  Where a
shader depends on one of those it fails here and is reported, rather than being
patched into something it was not.  The count of each failure reason is printed,
so the sample's shape is visible instead of implied.

The alternative -- teaching glslang the two extensions -- is a change to
glslang, not to this project, and would still produce SPIR-V that glslang wrote
rather than SPIR-V the game shipped.  Neither route yields the exact modules
NVN saw; this one yields real shaders at full scale, which is what the sample
is for.
"""
import argparse
import collections
import os
import re
import subprocess
import sys

# A directive may be spelled `#extension` or `# extension` -- the corpus uses
# both, and GLSLC accepts both.
DIRECTIVE = re.compile(r"^\s*#\s*(version|extension)\b(.*)$")

# The two extensions glslang does not implement.  Everything they enable that
# the corpus actually uses is available to glslang under another name, so they
# are replaced rather than merely dropped:
#
#   GL_NV_separate_texture_types  separate `texture2D` + `sampler` objects,
#                                 combined at the point of use.  Ordinary GLSL
#                                 under Vulkan semantics -- no extension needed,
#                                 which is why the corpus is compiled with -V.
#   GL_NV_gpu_shader5             the sized scalar types the corpus uses
#                                 (`uint32_t`, `uint64_t`).  glslang has these
#                                 under GL_EXT_shader_explicit_arithmetic_types
#                                 and GL_ARB_gpu_shader_int64.
UNKNOWN_EXT = ("GL_NV_separate_texture_types", "GL_NV_gpu_shader5")
SUBSTITUTE_EXT = ("GL_EXT_shader_explicit_arithmetic_types",
                  # FP16 SPELLED OUT.  The umbrella extension above is
                  # supposed to imply the per-width ones, and in glslang it
                  # does not always: a shader that declares `float16_t` can
                  # still be rejected with "required extension not requested"
                  # naming this one.  Both are enabled rather than one,
                  # because the umbrella is what the sized INT types come
                  # from and this is what the sized FLOAT types come from.
                  "GL_EXT_shader_explicit_arithmetic_types_float16",
                  # ...and that one covers ALU only.  A `float16_t` INSIDE a
                  # buffer block is a storage question, not an arithmetic one,
                  # and needs this as well; NV_gpu_shader5 gave the corpus
                  # both at once.
                  "GL_EXT_shader_16bit_storage",
                  "GL_ARB_gpu_shader_int64",
                  # NV_gpu_shader5 also allows IMPLICIT conversion to
                  # float16_t, which the EXT above does not; glslang's AMD
                  # spelling of half float does.
                  "GL_AMD_gpu_shader_half_float",
                  # A few shaders declare a block with scalar layout.
                  "GL_EXT_scalar_block_layout")

# The version is raised to 460 rather than kept.  Two things in the corpus need
# it and neither is a property of the shader's logic:
#
#   * `gl_CullDistance` in the `gl_PerVertex` redeclaration is core from 4.50,
#     and the corpus redeclares it at `#version 440`.  glslang does not
#     implement GL_ARB_cull_distance, so raising the version is the only route.
#   * one shader family ends its block declarations with a bare `;`, which is
#     an empty declaration -- rejected below 4.60 as "extraneous semicolon" and
#     accepted by GLSLC at any version.
#
# 4.60 adds nothing else these shaders use, so the raise is a parser
# accommodation and not a change of language.
FORCE_VERSION = "#version 460"

# Vulkan renames two vertex built-ins.  The corpus is OpenGL GLSL, so they are
# mapped back by definition rather than by editing the shader body.  This is
# NOT a no-op -- gl_InstanceIndex counts from the draw's baseInstance where
# gl_InstanceID counts from 0 -- and it is the one semantic difference this
# normalisation introduces.  It is listed in the corpus report for that reason.
VULKAN_SHIMS = ("#define gl_VertexID gl_VertexIndex",
                "#define gl_InstanceID gl_InstanceIndex")

EXT_TO_STAGE = {
    ".vert": "vert", ".frag": "frag", ".geom": "geom",
    ".tesc": "tesc", ".tese": "tese", ".comp": "comp",
}


# ---------------------------------------------------------------------------
# The BINDLESS CONSTRUCTOR, and why it is rewritten rather than left to fail.
#
# 4,366 of the 14,706 shaders build a sampler from a 64-bit NVN handle:
#
#     sampler2D  ( uint64_t ( Tex3 ) )                                 ~5,400
#     sampler2D  ( uint64_t ( DiffuseMap ) |
#                  uint64_t ( SamplerStates [ samDiffuseMap ] ) << 20 )    48
#
# glslang refuses both -- a `texture2D` is not convertible to an integer --
# and that one cause is essentially the whole of the corpus's rejections
# (121 of 122 on a random 400).  It is also not a property of the shader's
# logic: the same shaders ALREADY declare the two halves separately,
#
#     layout(binding = 3) uniform sampler   Smpl3;
#     layout(binding = 3) uniform texture2D Tex3;
#
# paired BY BINDING NUMBER, which is precisely the separate-texture-types
# model that made `-V` the right choice in the first place (see the header).
# So the handle arithmetic is replaced by the pairing it encodes:
#
#     sampler2D ( uint64_t ( Tex3 ) )   ->   sampler2D ( Tex3 , Smpl3 )
#
# WHAT THIS COSTS, STATED PLAINLY.  Where the handle OR-ed in a sampler-state
# index (the 48), that index is DROPPED -- the state it selected is not
# expressible here, and no sampler object in the shader corresponds to it.
# Where a texture has no sampler at its own binding, the first declared
# sampler of the right kind is used instead.  Both are choices about which
# sampler is bound, not about what the shader computes: the texture, the
# coordinates, the arithmetic and the control flow are untouched, so the
# SHAPE the converter is measured on is the shader's own.  A shader whose
# declarations give no usable sampler at all is left to fail rather than
# having one invented for it.
SAMPLER_CTOR = re.compile(r"\b(u?i?sampler[0-9A-Za-z]*)\s*\(\s*uint64_t\s*\(")
# `layout(binding = N) uniform <type> <name> ;`, in both of the corpus's
# spacings (`uniform texture2D Tex0 ;` and `uniform texture2D Tex0;`).
DECL = re.compile(r"layout\s*\([^)]*\bbinding\s*=\s*(\d+)[^)]*\)\s*"
                  r"uniform\s+(u?i?texture[0-9A-Za-z]*|sampler[0-9A-Za-z]*)\s+"
                  r"([A-Za-z_][A-Za-z_0-9]*)\s*;")


def _balanced(text, open_at):
    """The index just past the `)` matching the `(` at `open_at`."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _declarations(text):
    """binding -> name, for the texture objects and for the sampler objects.

    Two sampler tables, because a shadow sampler and an ordinary one are not
    interchangeable: `sampler2DShadow` must be built from a `samplerShadow`
    and every other constructor from a plain `sampler`.
    """
    tex, smp, smp_shadow = {}, {}, {}
    for m in DECL.finditer(text):
        binding, kind, name = int(m.group(1)), m.group(2), m.group(3)
        if kind.startswith("texture") or kind.startswith("utexture") \
                or kind.startswith("itexture"):
            tex[name] = binding
        elif kind == "samplerShadow":
            smp_shadow.setdefault(binding, name)
        elif kind == "sampler":
            smp.setdefault(binding, name)
    return tex, smp, smp_shadow


def depair_bindless(text):
    """Rewrite every bindless sampler constructor into an explicit pair.

    Returns the text unchanged when the shader has no such constructor, and
    raises nothing when it cannot be rewritten -- an unrewritten constructor
    is left in place so glslang reports it and the shader is counted as a
    failure with its real cause, rather than being silently dropped.
    """
    if "uint64_t" not in text:
        return text
    tex, smp, smp_shadow = _declarations(text)
    if not smp and not smp_shadow:
        return text
    out, pos = [], 0
    while True:
        m = SAMPLER_CTOR.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        ctor = m.group(1)
        open_at = text.index("(", m.end(1))
        end = _balanced(text, open_at)
        if end < 0:
            out.append(text[pos:m.end()])
            pos = m.end()
            continue
        inner = text[open_at + 1:end - 1]
        # The texture is the first identifier inside the first `uint64_t(...)`.
        t = re.search(r"uint64_t\s*\(\s*([A-Za-z_][A-Za-z_0-9]*)", inner)
        shadow = ctor.endswith("Shadow")
        table = smp_shadow if shadow else smp
        name = t.group(1) if t else None
        chosen = None
        if name is not None and table:
            b = tex.get(name)
            chosen = table.get(b)
            if chosen is None:
                # No sampler at this texture's own binding: take the first
                # declared one of the right kind.  Stated in the header as a
                # choice about which sampler is bound, not about the shader.
                chosen = table[sorted(table)[0]]
        if chosen is None:
            out.append(text[pos:end])          # leave it; glslang will say so
        else:
            out.append(text[pos:m.start()])
            out.append("%s ( %s , %s )" % (ctor, name, chosen))
        pos = end
    return "".join(out)


def normalise(path):
    """Rewrite one shader so glslang will read it, changing as little as possible.

    Three edits, each forced by something glslang requires and GLSLC does not:

      * `#version` is moved to the front.  The corpus has a whole family of
        shaders whose first directive is an `#extension` and whose `#version`
        comes after it; GLSLC accepts that order and glslang does not.
      * the two unknown extensions are replaced by the glslang spellings of
        what they provide (see UNKNOWN_EXT / SUBSTITUTE_EXT).
      * the two renamed Vulkan built-ins are mapped back (VULKAN_SHIMS).

    Everything else -- every other directive, the whole body, the comments --
    is passed through in its original order.
    """
    with open(path, "r", errors="replace") as fh:
        lines = fh.read().splitlines()

    out = []
    for line in lines:
        m = DIRECTIVE.match(line)
        if not m:
            out.append(line)
            continue
        if m.group(1) == "version":
            continue                      # replaced by FORCE_VERSION at the top
        if any(e in line for e in UNKNOWN_EXT):
            continue
        out.append(line)

    # The bindless constructors, before the head is glued on: the rewrite reads
    # the shader's own `layout(binding = ...)` declarations and they are all in
    # the body.
    out = depair_bindless("\n".join(out)).split("\n")

    head = [FORCE_VERSION]
    # `require`, not `enable`, and only on the lines THIS script injects.
    # Measured equivalent on a 900-shader sample (403 compiled either way) --
    # glslang implements all five -- so the choice is about what a failure
    # should look like: these are substitutions for extensions the shader
    # itself said `require` on, and if a future glslang drops one the corpus
    # should stop loudly rather than mis-compile quietly.
    #
    # The shaders' OWN `#extension ... : enable` lines are left exactly as
    # they are.  Several name extensions glslang does not implement
    # (`GL_EXT_gpu_shader4`, `GL_NV_bindless_texture`), where `enable` is a
    # warning and `require` is a hard error: rewriting those cost 30 shaders
    # of the same sample.
    head += ["#extension %s : require" % e for e in SUBSTITUTE_EXT]
    head += list(VULKAN_SHIMS)
    return "\n".join(head + out) + "\n"


# ---------------------------------------------------------------------------
# The IMPLICIT NARROWING to fp16, repaired from glslang's own diagnostic.
#
# A handful of shaders are written in fp16 throughout -- `f16vec3` parameters,
# `float16_t` locals -- and assign an expression that an untyped literal has
# dragged up to fp32:
#
#     float16_t pd = a / ( 0.5 * 3.14159285 ) * float16_t ( resolution ) ;
#
# `a` is fp16 and widens to fp32 (which is allowed), so the right-hand side is
# fp32 and only the ASSIGNMENT needs an fp32 -> fp16 narrowing.
# `GL_NV_gpu_shader5` permits that implicitly; the EXT explicit-arithmetic
# family forbids it, which is what the name means.  Measured, no combination
# of extensions glslang implements restores it, and no `--target-env` changes
# it -- it is a language rule, not a capability.
#
# So the narrowing is made explicit, which is what the implicit conversion did:
# compute in fp32, narrow ONCE at the end.
#
#     float16_t pd = float16_t ( a / ( ... ) * float16_t ( resolution ) ) ;
#
# Retyping the LITERAL instead (`a / float16_t ( 0.5 * 3.14159285 )`) also
# compiles and is NOT equivalent: it moves the divide into fp16 and changes
# the arithmetic rather than spelling it out.  Measured on the five forms,
# `float16_t(int)` compiles on its own, so the integer conversion is not what
# fails here and converting via `float` first would change nothing.
#
# This is driven by glslang's error line, not by a pattern in the source: the
# compiler says which line and which target type, and only that line is
# touched.  A line it cannot repair is left alone so the shader fails with its
# real cause.
NARROW = re.compile(r"ERROR: [^:]*:(\d+): '=' :\s*cannot convert from "
                    r"' temp ([A-Za-z_0-9]+)' to ' temp ([A-Za-z_0-9]+)'")
# An `=` that is an assignment, not a comparison.
ASSIGN = re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)")


def repair_narrowing(text, stderr_text):
    """Wrap the right-hand side of the assignment glslang refused.

    Returns the new text, or None when there is nothing it can repair -- in
    which case the caller must not retry.
    """
    m = NARROW.search(stderr_text)
    if not m:
        return None
    lineno, target = int(m.group(1)), m.group(3)
    # Only the sized float types.  A refusal to convert between two unrelated
    # types is a different problem and wrapping it would hide it.
    if not (target == "float16_t" or target.startswith("f16vec")):
        return None
    lines = text.split("\n")
    if not (1 <= lineno <= len(lines)):
        return None
    line = lines[lineno - 1]
    a = ASSIGN.search(line)
    if not a:
        return None
    end = line.rfind(";")
    if end < 0 or end <= a.end():
        return None
    lhs, rhs = line[:a.end()], line[a.end():end]
    if not rhs.strip():
        return None
    lines[lineno - 1] = "%s %s ( %s ) %s" % (lhs, target, rhs.strip(),
                                             line[end:])
    return "\n".join(lines)


def reason(stderr_text):
    """Collapse glslang's output to one short cause, for the histogram."""
    for line in stderr_text.splitlines():
        if "ERROR:" not in line:
            continue
        m = re.search(r"ERROR: .*?:\d+: '(.*?)' : (.*)", line)
        if m:
            return (m.group(2).strip())[:60]
        return line.strip()[:60]
    return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="folder of .vert/.frag/... shaders")
    ap.add_argument("dst", help="where to write the .spv files")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args()

    os.makedirs(a.dst, exist_ok=True)
    names = sorted(n for n in os.listdir(a.src)
                   if os.path.splitext(n)[1] in EXT_TO_STAGE)
    if a.limit:
        names = names[:a.limit]

    tmpdir = os.path.join(a.dst, ".src")
    os.makedirs(tmpdir, exist_ok=True)

    ok = 0
    why = collections.Counter()
    from concurrent.futures import ThreadPoolExecutor

    def one(name):
        stage = EXT_TO_STAGE[os.path.splitext(name)[1]]
        # glslang picks the stage from the extension, and the corpus names
        # contain '#', which it would otherwise read as part of a path.  Write
        # a normalised copy under a safe name with the right extension.
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        src = os.path.join(tmpdir, safe)
        with open(src, "w") as fh:
            fh.write(normalise(os.path.join(a.src, name)))
        out = os.path.join(a.dst, safe + ".spv")
        # A BOUNDED REPAIR LOOP.  Each pass compiles, and if glslang refused
        # exactly one implicit fp16 narrowing, that line is made explicit and
        # the shader is compiled again.  Bounded because a repair that does
        # not move the error forward would otherwise spin; anything else is
        # reported with its real cause on the first failure.
        for _ in range(8):
            p = subprocess.run(["glslangValidator", "-V", "-S", stage,
                                "-o", out, src],
                               capture_output=True, text=True)
            if p.returncode == 0 and os.path.exists(out):
                return (name, None)
            fixed = repair_narrowing(open(src).read(), p.stdout + p.stderr)
            if fixed is None:
                return (name, reason(p.stdout + p.stderr))
            with open(src, "w") as fh:
                fh.write(fixed)
        return (name, reason(p.stdout + p.stderr))

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for name, err in ex.map(one, names):
            if err is None:
                ok += 1
            else:
                why[err] += 1

    print("%d shaders, %d compiled to SPIR-V, %d rejected" % (len(names), ok, sum(why.values())))
    for r, n in why.most_common(20):
        print("  %5d  %s" % (n, r))


if __name__ == "__main__":
    main()
