# spirv2glasm — SPIR-V → GLASM, exactly

`spirv2glasm` takes a SPIR-V module and produces the `!!NVvp5.0`-style
NV_gpu_program5 listing GLSLC 17.24 compiles it to — the same text the compiler
stores in a shader's debug-info section and, comment-stripped, in the fat
control section, and the same text `glasm2sass` turns into SASS.

It is not a model of the front end. It **is** the front end.

Together with `glasm2sass` it closes the pipeline:

    SPIR-V --[spirv2glasm]--> GLASM text --[glasm2sass]--> .code (Maxwell SASS)

---

## 1. The finding this rests on

GLSLC does not have a second compiler for SPIR-V. It has **one** front end —
NVIDIA's `cgc`, which also compiles GLSL — and that front end can be driven
from a SPIR-V module instead of from text. The choice is a flag inside cgc
(`-ispirv`, visible in the option-name pool at 0x710100cf5e0), and both arms
converge on the same IR and the same GLASM printer:

    GLSL   --.
              >-- cgc IR --[f_7100ef60e0]--> GLASM text --[asmp]--> OCG --> SASS
    SPIR-V --'

The handover into that shared pipeline is upstream of the printer. In the
compile driver `f_7100ef56a0`:

    ef59e0:  ldr  x0, [x8, #8]      ; x8 = [cgc+2864]; x0 = the SPIR-V reader
    ef59e4:  bl   fdd964            ; spv_ReadModule(ctx, cgc, entryPointName)

So `runtime/g2s_hook.c` — written for the GLSL path by the glasm2sass package,
and sitting on the GLASM printer's return — captures a SPIR-V compile's listing
with no change at all. That is the whole mechanism: **this tool performs an
ordinary compile and reports what went past the hook.** Nothing is substituted,
so the sections it writes are also a valid reference, not a doctored one.

### Why the stock CLI cannot do it

`tools/glslc_cli.c` leaves `GLSLCinput.spirvModuleSizes` and
`.spirvEntryPointNames` NULL, because nothing on the GLSL path reads them. The
library reads them unconditionally once the language field says SPIR-V. In
`f_7100018280` the two paths separate on one test:

    cmp  x27, #0x40                 ; x27 = optionFlags & 0x1e0 (the language)
    b.ne <GLSL: copy sources[i] as a C string>
    ldp  x8, x9, [x19, #192]        ; x8 = spirvEntryPointNames
                                    ; x9 = spirvModuleSizes
    ldr  w9, [x9, x22, lsl #2]      ; <-- faults when the array is NULL

which is exactly the SIGSEGV a `.spv` input takes through the stock CLI. The
same block shows the other two arrays are optional: a NULL
`spirvEntryPointNames`, or a NULL entry in it, falls back to the literal at
0x1154e8a, which is `"main"`, and a NULL `spirvSpecInfo` entry is stored as 0.

**The module size is in bytes.** The reader's cursor (`SpvCtx+112`) is a byte
offset — `f_7100fcc16c` advances it by 4 per word — and it is compared against
`SpvCtx+32`, which is where the value from `spirvModuleSizes` arrives.
`--size-words` re-runs a module under the other reading; it exists so the claim
can be retested, not to be used.

---

## 2. What was measured

Every number below was produced by running the tool.

`tools/verify.sh` is the round trip, in three processes because the port's
output is not independent of what was compiled before it in the same process.

**The flags are part of the test, and the first version of this got them
wrong.**  A `--output-thin-gpu-binaries --debug-info none` build stores NO
listing anywhere in its output, so a run under those flags has nothing to check
the captured text against and can only compare the SASS.  That checks the back
end; it says nothing about whether the listing the hook captured is the
compiler's own listing, which is the whole claim this tool makes.  So the
verification runs **fat** (no `--output-thin-gpu-binaries`) and with
`--debug-info g2`, which makes the compiler store its own listing in both of
the forms it has:

1. `spirv2glasm <m.spv> -o d` -- an ordinary compile, nothing substituted.
   Writes the CAPTURED listing and the reference container.
2. `checklisting.py` -- three comparisons:
   * **debug**: the debug-info section's listing must be **byte-identical** to
     the captured text.  This is the compiler stating that what went past the
     hook is what it produced.
   * **control**: the fat control section's listing must equal the captured
     text with the `#` comment lines removed and runs of whitespace collapsed
     to one space.  (That relation was established, not assumed -- see below.)
   * **code**: the `.code` rebuilt from the listing by `glasm2sass` must equal
     the `.code` the compiler produced from the module.
3. `glasm2sass <m.glasm>` -- the rebuild that check 3 consumes.

### The two stored forms, and how they differ

The brief names both: the listing the debug-info section stores verbatim, and
the comment-stripped one in a fat control section.  They are the same program,
and the exact relation was measured on `basic#039bd1ee.vert` -- a real corpus
shader whose listing is 2,599 lines:

| form | size | content |
|---|---|---|
| debug-info | 122,233 bytes, 2,599 lines | the captured text, byte for byte |
| fat control | 24,523 bytes, 1,162 lines | the same, `#` lines dropped and whitespace runs collapsed |

Dropping the comment lines alone leaves 1,162 lines -- the right count -- and
the only remaining difference is the mnemonic column's padding: `IF    NE.x;`
against `IF NE.x;`.  Collapsing runs of whitespace makes the two identical over
the whole listing.

| sample | flags | result |
|---|---|---|
| the 90 probes of the time: vertex, fragment, geometry (every input x output primitive), tess control, tess evaluation (every mode x spacing), integer ops, textures, control flow, UBO, SSBO | `--opt-level none --debug-info g2` (fat) | **90 / 90, all three checks** |
| the same 90, SASS check only | `--opt-level none --debug-info none --output-thin-gpu-binaries` | **90 / 90 identical** |
| **10,328 shaders from the 14,706-shader corpus** (what an older `mkcorpus.py` compiled) | `--opt-level none --debug-info g2` (fat) | **10,328 / 10,328, all three checks** (`corpus/RESULTS.md`) |

### The corpus, and what it took to use it

The 14,706-shader corpus is the sample that matters, and out of the box glslang
rejects every one of them.  `corpus/mkcorpus.py` gets 14,630 through (glslang
15.1.0); its header
comment states every edit and what each is forced by.  In short: `#version` is
moved to the front (a whole family in the corpus puts an `#extension` before
it, which GLSLC accepts and glslang does not), the two NVN-only extensions are
replaced by the glslang spellings of what they provide, and the shaders are
compiled with **Vulkan** semantics because what
`GL_NV_separate_texture_types` gives them -- separate `texture2D` and `sampler`
objects combined at the point of use -- is ordinary GLSL there.

Most of the corpus uses **bindless texture handles**
(`sampler2D(uint64_t(x))`), and glslang refuses to emit SPIR-V for those at
all -- `'GL_ARB_bindless_texture' : not allowed when using generating SPIR-V
codes`.  That is a limit of glslang, not of this tool: `spirv2glasm` compiles
bindless SPIR-V perfectly well when something else produces it, and the
listings show it (`LDC.U64 D0.x, buf14[0]`, `TEX.F R0, ..., handle(D0.x),
2D`).  `mkcorpus.py` rewrites the constructor to `sampler2D(Tex, Smpl)`,
pairing the texture with the sampler declared at its binding, and makes an
fp16 narrowing explicit that glslang will not do implicitly.  An older
version, without those two edits, got 10,328 through.  The 76 that still do
not convert: 72 bindless shaders with no sampler to pair with, 3 using
`GL_NV_bindless_texture`, 1 with a std140 alignment error
(`corpus/README.md`).

`probes/mkprobes.py` remains, and is complementary rather than a substitute: a
probe whose only difference from its neighbour is `vec4` against `vec3`
isolates one decision, which is what makes a *rule* readable, while the corpus
is what says the rules hold at scale.

---

## 3. What the SPIR-V front end does differently from the GLSL one

This matters, because the obvious assumption — "both arms build the same IR, so
the same program gives the same listing" — is **false**, and a converter built
on it would be wrong everywhere.

The same program, same flags, GLSL on the left and SPIR-V on the right:

| GLSL front end | SPIR-V front end |
|---|---|
| `MOV.F result.position, vertex.attrib[0];` | four scalar `MOV.F`, one per component, through a temp |
| 1 instruction, 0 R-regs | 8 instructions, 1 R-reg |
| `.F` type suffix | `.F32` type suffix |

The declaration block above the code — the `#var` lines, `ATTRIB`, `CBUFFER`,
the `OPTION`s and the header — **is** the same, which is consistent with the
two arms sharing the symbol table (§4) and differing in how they build
expressions.

Arithmetic stays vectorised on the SPIR-V path (`MUL.F32 R0, a, b`,
`DP4.F32 R0.x, a, b`); it is stores into interface-block members that
scalarise. And there is no coalescing at all: every SPIR-V result id that needs
one gets its own `TEMP`, numbered in order of first definition, which is the
signature of a direct SSA-to-temp mapping.

---

## 4. What has been read out of the image

`notes/` holds the recovered structure, each claim with the address to re-read
it at. In brief:

* **Both opcode dispatch tables**, decoded: 49 opcodes accepted at module
  scope, 224 in a function body, grouped exactly as the compiler groups them
  (`notes/dispatch.json`, `notes/01-frontend-structure.md`).
* **The reader context and the id table** — 104 bytes per `<id>`, and what four
  of its fields hold.
* **The cgc scalar type codes** the type handler passes, with the rule that the
  unsigned type is the signed one plus 1 (`notes/02-types.md`).
* **Storage classes**: `OpTypePointer` accepts a different set from
  `OpVariable` — `Image` passes the first and is refused by the second
  (`notes/03-variables.md`).
* **The decoration table**, split into acted-on, warned-and-ignored, and
  refused (`notes/04-decorations.md`).
* **The execution-mode tables**, including which modes are silently ignored.
* That the SPIR-V arm **reconstructs GLSL-level names** — its string pool holds
  `gl_PerVertex`, `gl_in`, `gl_out`, `gl_WorkGroupSize`, and the invented
  `__defaultname_%d`, `@TMP_%d`, `@TMPARR_%d` — which is why the declaration
  block comes out the same as the GLSL path's.

---

## 5. Building

```sh
tools/apply_patch.sh path/to/glslc17.24.113src     # after glasm2sass's
cd glslc17.24.113src
make -r -j"$(nproc)"      # the library: 53,748 objects
make spirv2glasm          # seconds
make glasm2sass cli       # for verify.sh and for references
```

The tree's toolchain requirement is unchanged and this package does not lower
it: **GCC 15+ with binutils ≥ 2.44, or clang 19+**, because `src/data_ro.c`
pulls the 3.2 MB read-only image in with C23 `#embed` (HANDOVER §30.9, §31.7).
Ubuntu 24.04 ships GCC 13 and binutils 2.42, neither of which is enough; what
this package was built and measured with is

    gcc-15 (Ubuntu 15-20250404-0ubuntu1) 15.0.1, binutils 2.44

unpacked from the Ubuntu plucky packages into a prefix and pointed at its own
assembler with `-B`. Do **not** work around `#embed`: §30.9 removed the C-array
and `.incbin` spellings on purpose.

---

## 6. Using it

```sh
# the listing, to stdout
spirv2glasm shader.vert.spv --opt-level none --debug-info none \
            --output-thin-gpu-binaries

# the listing and the reference container
spirv2glasm shader.vert.spv -o out --opt-level none

# a named entry point
spirv2glasm module.spv --entry vs_main -o out
```

**The flags must match the ones the listing is to be used under.** The
optimisation and debug-info levels both change what the front end emits, so a
listing captured under one flag set and assembled under another will differ,
correctly.

**The stage** is read out of the module's own `OpEntryPoint` — the execution
model of the entry point named by `--entry` (default `main`) — so it cannot be
set wrong by accident. `--stage` overrides it.

---

## 7. Re-verifying

```sh
ls probes/*.spv > list.txt
tools/verify.sh list.txt /tmp/v --opt-level none --debug-info none \
                --output-thin-gpu-binaries
```

Prints `identical N  differing N  failed N` and exits non-zero if anything
differed.

---

## 8. Limits, stated plainly

* **Compute is out of scope for now**, on instruction: the brief was the five
  non-compute stages, and the compute path is visibly different (a separate
  `!!NVcp5.0` profile, `GROUP_SIZE`, shared memory). No compute probe is in the
  90.
* **Specialization constants are not exercised.** `GLSLCinput.spirvSpecInfo` is
  passed as NULL and `GLSLCspirvSpecializationInfo` is an empty struct in
  `glslcinterface.h` ("todo: reverse from nnSdk, type never used"), so
  `OpSpecConstant` is only reached with its default value.
* **PushConstant is not exercised.** glslang allows `push_constant` only for
  Vulkan and the modules here are the OpenGL flavour; probing that storage
  class needs a hand-written module through `spirv-as`.
* **Only little-endian modules.** `f_7100fcbfa0` compares the magic word
  without swapping, so a big-endian module is rejected by the compiler; the
  tool says so itself rather than letting that message stand for both cases.
* **One compile per process, on purpose.** Do not batch modules inside it.

---

## 9. The instrument

`tools/trace_patch.py` puts a call to `g2s_trace` at the top of any guest
function in the port tree; `g2s_trace` (in `runtime/g2s_hook.c`) prints that
function's incoming argument registers when `G2S_TRACE` is set in the
environment, and returns immediately otherwise.

The port turns every guest function into ordinary C, so this is a breakpoint
that costs one recompiled object and a relink instead of a scripted gdb
session — and unlike a register dump it can print the guest's own memory, which
is where the answers to the remaining questions are. `src/fn/*.c` is generated,
so the script is idempotent and re-runnable after a regeneration, exactly like
`apply_patch.sh`.

It has already paid for itself once: `notes/05-two-passes.md`.
