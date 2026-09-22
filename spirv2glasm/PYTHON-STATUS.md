# The Python SPIR-V → GLASM converter: where it stands

The C `spirv2glasm` runs GLSLC's own front end and is the **oracle**.  This is
the status of the Python converter that has to produce the same text itself.

Everything here is either read out of the compiler's image or measured against
the compiler.  Where something is not established, the code says so *by
raising* rather than by emitting something plausible — see `NotEstablished` in
`py/glasm.py`.

---

## The headline

`tools/compare.py` emits what the Python side claims to know and compares it
**line for line** against the oracle's listing, stopping at the first line the
Python side does not claim.  Current figures (notes/114 and after):

```
probes       (654)        exact 654   prefix-only 0     DIFFERS 0  failed 0
corpus sample (120)       exact 120   prefix-only 0     DIFFERS 0  failed 0
slice        (1,400)      exact 962   prefix-only 438   DIFFERS 0  failed 0
                          1,093,657 of 1,848,903 listing lines (59.2%)
full corpus  (14,630)     exact 10,179  prefix-only 4,451  DIFFERS 0  failed 0
                          12,917,944 of 20,496,042 lines (63.0%)
                          -- MEASURED BEFORE SS36..SS40, so it is the last
                          full sweep and not the current state
sample       (1,500)      exact 1,461   prefix-only 14     DIFFERS 25
                          -- the current state, on the current tree
```

`DIFFERS` WAS 0 EVERYWHERE at the last full sweep, and IS NOT NOW.  That is
not a regression: SS36 to SS38 removed the three commonest REFUSALS, and a
module that used to stop after its declarations now emits its whole body --
where it meets whatever the refusal had been hiding.  On a 1,500-module
sample prefix-only fell from 71-in-200 to 14-in-1,500 and DIFFERS rose from
0 to 25, of which SS39 and SS40 have since closed 11.

The 25 are TWO families, both narrowed to a mechanism and neither guessed
at (notes/114 "Still open"): 18 modules missing one scalar copy, where a
spelling that makes all 18 instruction-exact is refuted by its own probe,
and 8 where the compiler gives two copies of one expression the same
`node[36]` stamp.  notes/114 is the running list of what was read to get
here -- forty causes, each with an off-switch and a probe -- plus the candidates that were read and DROPPED because they
contradicted an already-measured rule.  What is left in that tail is the
ALLOCATOR: `tools/p2check.py` runs our pass 2 on the compiler's own blocks
and it reproduces the compiler's order everywhere it has been tried, and
`tools/recnum.py` agrees with the compiler's record numbering, so the order
and the numbering are not what differs -- the interference graph and the
colouring are.

Compute shaders are converted too (they were refused by design earlier).
Everything below this headline is the HISTORY of how the figures got here,
kept as it was measured -- the older corpus runs are over the 10,328 modules
an earlier `corpus/mkcorpus.py` produced (corpus/README.md), with 121 probes
and a converter that refused compute.

```
probes  (121 shaders)     exact 87  prefix-only 34   DIFFERS 0  failed 0
                         3032 of 4011 listing lines reproduced (75.6%)

corpus  (120 shaders)    exact 1   prefix-only 118  DIFFERS 0  failed 1
                         13595 of 120120 listing lines reproduced (11.3%)

corpus  (14,590 full)    exact 6   prefix-only 14584  DIFFERS 0  failed 0
                         1884385 of 20474191 listing lines reproduced (9.2%)
```

The corpus figure was 1.9% and is now 10.7% because the `#var` and `#semantic`
blocks -- the largest surface in a real shader's listing -- now cover the
corpus instead of refusing most of it.  The whole-corpus sweeps:

```
tools/parvar.sh    #semantic + #var, 10,328 shaders   same 10,275  differ 0  refused 53
tools/parcheck.sh  ATTRIB/OUTPUT,    10,328 shaders   same 10,290  differ 0  refused 38
```

Refusals are compute (out of scope by the brief), `gl_GlobalInvocationID`, and
the tessellation-control `gl_out` block, whose output side has two binding
kinds and prints two symbols per variable.

`DIFFERS 0` on the corpus is new: the one long-standing disagreement
(`map_1465b18f.frag`) is fixed, and it was not what it looked like.  An ARRAY
member of a uniform block prints ONE `#var` line, for ELEMENT 0
(`hlslcc_mtx4x4view_g[0]`), and the `used` column on it is element 0's -- that
shader reads row 2.  The `failed 1` is the compute shader, which raises by
design.

The declaration block is measured separately, because `compare.py` stops at the
first unclaimed line and a shader whose `#var` block is unclaimed never reaches
it.  `tools/checkattrib.py` compares just the `ATTRIB`/`OUTPUT`/colour-output
lines against the oracle:

```
probes   (90)          same 90     differ 0  refused 0
corpus listings (517)  same 516    differ 0  refused 1   (compute)
corpus, ALL (10,328)   same 10,290 differ 0  refused 38  (compute)
```

The `used` column is bit 8 of the symbol's flag word, set when the front end
builds a symbol-REFERENCE node, and at `--opt-level none` it builds one for
every operand it reads.  So the gate IS "the id appears as an operand", with
the literal filter of `_id_args` -- an `OpExtInst`'s instruction number sits in
an operand slot and is not an id.  A liveness fixpoint used to stand here; it
was built on an inverted reading of one shader and gave the wrong answer on
exactly that shader.  notes/18 records the correction and the measurement
behind it (3,442 shaders, one disagreement, and the mention count wins it).

Read that as: on every shader, every line the converter emits is the line the
compiler emits, and it stops — cleanly, at a named gap — before the first thing
whose rule has not been read.  `DIFFERS 0` is the number that matters at this
stage; `exact 0` is the one that has to become 90 and 517.

MEASURE THE CORPUS BY LINES, NOT BY SHADERS.  `tools/census.py` weights every
refusal by the listing lines it costs, and it is what says where the work is:
429k of the corpus's 604k lines are behind control flow, 78k behind loads that
need a register and 62k behind local stores.  A refusal that closes 25 probe
shaders and 1.9% of the corpus is not progress toward the goal.

The corpus percentage is low for a reason that is not about correctness: a
corpus listing runs to hundreds or thousands of lines and the part that is
established is a fixed ~10-line header, so the same work reproduced 30.8% of a
probe and 1.1% of a real shader when only the header was established.  The one failure is a compute shader, which
raises `NotEstablished` by design.

A gap and a bug look identical in a diff and are not the same thing, which is
why `compare.py` counts them separately.

**The corpus is what makes this measurement worth anything.**  Run against the
90 probes alone the OPTION block looked complete; run against 517 real shaders
it produced **244 differences** — two options no probe in the set can produce,
because no probe has a shadow sampler or `EarlyFragmentTests`.  Probes make a
rule readable; the corpus is what says the rule is finished.

## The body: where it now stands

The body is still unemitted, and what changed this session is that it is no
longer opaque.  `runtime/g2s_hook.c` gained `g2s_trace_node`, which dumps the
IR node the two body line printers take, and `tools/nodedump.py` joins that
stream to the listing by mnemonic.  One command now pairs every GLASM
instruction of any shader with the node that produced it, which gives:

* the node layout -- opcode and modifier at +8, destination virtual register
  at +32, write mask at +48, operand links at +64/+72, destination operand at
  +136, flags at +152 (notes/29);
* confirmation of `notes/glasm_opcodes.json`, decoded from the namer long
  before: 0x93/0x47/0x18/0x0f/0x13/0x14/0x1a/0x1b/0x1c come back as `POW`,
  `MOV`, `RET`, `REP`, `ENDREP`, `BRK`, `IF`, `ELSE`, `ENDIF` against the
  lines that carry those mnemonics;
* the register allocator, visible from both ends at once: `vr3..vr7` are
  `R0..R4` and `vr10`, `vr13`, `vr16` are `R0` again, so `TEMP R0..R4` and
  `# 5 R-regs` count PHYSICAL registers where the IR has eleven virtual ones;
* two lowering shapes that hold on every probe dumped so far -- vector work is
  scalarised and then gathered through one register, and writing an output is
  always a `MOV` into a scratch register followed by a masked `MOV` out.

None of that is implemented yet.  It is the map that was missing, and it was
missing because the printers dispatch through a vtable and the static call
graph stops there.

## What is established, and on what

| part of the listing | basis |
|---|---|
| the profile line (`!!NVvp5.0` …) | one literal per profile in the image; the name-recovery map attributes five of them to functions whose only rodata reference is that literal (`glasm_EmitHeader_vp5` at 0x7100d5ab60) |
| the `#profile` name | literals in the image.  **Not** the magic with punctuation removed: tessellation control is `gp5hp` and evaluation is `gp5tp`, not `gp5tcp`/`gp5tep` |
| the `OPTION` block | the printer holds each line whole (`'OPTION NV_unroll_none;\n'` at 0x710115ff …); three are unconditional over the probes, three conditional |
| the OPTION **order** | measured.  It is not alphabetical and the three always-on options are not contiguous: `NV_gpu_program_fp64` and `NV_shader_storage_buffer` come before `NV_bindless_texture`, `ARB_draw_buffers` after it |
| the stage directives (`PRIMITIVE_IN`, `VERTICES_OUT`, `INVOCATIONS`, `TESS_MODE`, `TESS_SPACING`, `TESS_VERTEX_ORDER`, `TESS_POINT_MODE`) | every line and every token is a literal in the image; which execution mode feeds which is `f_7100fcc784`'s own jump tables, decoded in `notes/tables.json` |
| the comment header | fixed text plus the profile and entry-point names |
| the `#var` and `#semantic` blocks | the type spelling is READ (the printer's name table at 0x71014f95c8); the semantic and register columns are measured over 607 listings (4,511 in/out lines exactly predicted); the block ORDER is a linked-list walk read out of `f_7100bd30b0`, and what this reproduces is that list's contents, not the mechanism (notes/13) |
| `__defaultname_<n>` | READ: it is the variable's own SPIR-V `<id>`, format string at 0x7100fd3438, checked on 2,333 blocks |
| `gl_FragCoord`'s presence | READ-adjacent: isolated to `OriginUpperLeft` vs `OriginLowerLeft` by a probe pair whose modules differ in ONE instruction |
| `STORAGE` / `CBUFFER` / vertex `ATTRIB` | measured 607/607 and 170/170 |
| the SPIR-V reader (`py/spv.py`) | the Khronos grammar for the encoding, GLSLC's two dispatch tables for what is legal where; cross-checked against `spirv-dis` on 13 modules, 690 instructions, exact agreement on opcode and result id |

### The five conditional options, and an honest caveat

`NV_gpu_program_fp64`, `ARB_draw_buffers` and `NV_shader_storage_buffer` are
emitted on conditions that correlate perfectly over the 90 probes **in both
directions** — 5/5 sampler probes and 0/85 others, 1/1 multi-output fragment
probe and 0/11 single-output ones, 1/1 SSBO probe.  That is a measurement, not
the test the compiler applies, and `py/glasm.py` says so at the definition.
One half of the fp64 condition (`OpTypeFloat 64` / `OpTypeInt 64`) is not
exercised by any probe at all and is there on reasoning, which is flagged.

`ARB_fragment_program_shadow` and `NV_early_fragment_tests` came out of the
corpus, checked over 517 real shaders in both directions with zero mismatches:
139/517 for the first (a depth-compare sample, or an `OpTypeImage` with
`Depth = 1`) and 146/517 for the second (`ExecutionMode EarlyFragmentTests`).
The second is *read* rather than measured — mode 9 has its own arm at
0x7100fccad8.

Worth noting what the fp64 option turned out to be *about*: not `double`, but
64-bit registers — a bindless texture handle is 64 bits, and the listings show
`LONG TEMP D0;` and `LDC.U64 D0.x, buf14[0];`.

## What is left, in the order it should be done

### 1. The `#var` block — the next ~35% of the lines

    #var float4 a0 : $vin.ATTR0 : ATTR0 : -1 : 1
    #var ulong s : BUFFER[14][0] : buffer[14][0] : -1 : 1

Six fields and four of them need rules that have not been read: the type
spelling (`float4`, `ulong`), the `$vin.`/`$vout.` binding name, the register
column (`ATTR0`, `COL0[0]`, `TESSCOORD[60]`, `buffer[14][0]`), and the trailing
pair.  The ordering is not established either — a fragment probe lists outputs
before inputs, a vertex probe lists its input first.

This is the **cgc symbol-table printer**, shared with the GLSL path, not the
SPIR-V reader.  So it is read from the printer, and the SPIR-V side only has to
supply the symbols — which `f_7100fd3160` already tells us it builds with
GLSL-level names (`notes/03-variables.md`).

### 2. The declaration block — DONE except TEMP

`ATTRIB` / `OUTPUT` / `STORAGE` / `CBUFFER` and the fragment colour outputs are
now READ, not measured, and they agree with the compiler exactly:

```
probes   (90)          same 90     differ 0  refused 0
corpus listings (120)  same 119    differ 0  refused 1
corpus, ALL (10,328)   same 10,290 differ 0  refused 38
```

The full-corpus sweep is the one that matters: it found two defects the 120
listings and the 90 probes both missed -- a tessellation-control varying, and
`gl_Position` written one component at a time (notes/17) -- and after those it
is exact on every non-compute shader available.  `tools/parcheck.sh` runs it,
resumably, in one stream per core.

What made that possible, in order (notes/16, notes/17, notes/18):

* `f_7100bd4810`, the binding-name formatter, transcribed arm for arm into
  `py/binding.py` — including which arms return a single binding and which
  return a range, which is what decides `name = binding;` against
  `name[] = { binding[lo..hi] };`;
* `f_7100bd1da0`, the slot-name table, decoded completely, which also pins the
  eighteen register tables of notes/07 to the code;
* `f_7100bdcc20`, the classifier, which says the block covers exactly the
  symbols whose `used` bit is set and gives the kind→bit map;
* `f_7100bdaef0`, the emitter loop itself — descending slots, the two merge
  loops, the qualifier order, and the colour-output tail;
* the (kind, slot) a stage's interface gets, measured with a new tracer
  (`g2s_trace_sym`) that prints the pair from inside the `#var` printer so the
  two streams pair positionally (`tools/bindmap.py`).

`TEMP` is all that is left of the block and it is the register allocation:
`program[1240 + 4*class]`, with the line shapes read out of `f_7100bdcd60`.

### 3. The `used` column, which is now load-bearing

`f_7100bdcc20` gates the declaration block on the same bit the `#var` line
prints, so a wrong `used` is no longer a wrong comment — it is a wrong
`ATTRIB` range.  `_live_ids` replaced the old "mentioned somewhere" test with a
liveness fixpoint that follows stores, loads, call parameters and return values
(notes/18).  It still over-reports on one shape — a value passed to a helper
whose result is stored into a local that is read later, which the compiler
still drops — and that is a named defect with a named example.

### 4. The instruction body

The 74 body handlers in `notes/dispatch.json`.  One piece of it is now read:
the VOCABULARY.  Three chained functions name an OCG opcode -- `f_7100d57910`
(shared-memory atomics) into `f_7100d56180` (buffer atomics and some texture
forms) into `f_7100bd5734` (the GLASM printer's own, 520 entries) into
`f_710005b5fc` (the generic namer, 212 entries) -- and all four tables are
decoded into `notes/glasm_opcodes.json`, 232 opcodes with a name (notes/19).
The generic namer's default arm is worth knowing: an operation with no GLASM
spelling prints as `<<name>>` from a runtime table, so a listing containing
`<<...>>` would be the compiler admitting it had no mnemonic.

The line PRINTER has since been found, by tracing rather than grepping
(notes/21, notes/23).  `f_710005c1dc` and `f_710005c71c` own the formats --
`%-5s %s, %s;`, `%-5s BB%d (%s);`, `%-5s (TR);` and the rest -- `f_7100bd61f4`
builds the mnemonic with its modifiers, and the vtable they dispatch through is
dumped in notes/23: slot 72 the mnemonic, 136 the destination, 152 each source.
`f_710003e48c` is the source-operand grammar, nine parts around a name.

And behind that is the finding that sets the order of the remaining work:
`f_710003d0f0` names a register `vr%d`/`un%d` BEFORE allocation and out of the
allocator's own 224-byte records after it.  The body's operand names are the
register allocator's output, not the IR's -- so the body needs, in order, the
74 handlers, then the allocation, then the printer.

The observed shape of the body, from listings and NOT in the emitter:

* vector arithmetic stays vectorised (`MUL.F32 R0, a, b`, `DP4.F32 R0.x, a, b`);
* stores into interface-block members scalarise, one `MOV.F` per component
  through a temp;
* the type suffix is `.F32`, where the GLSL path writes `.F`;
* constants inline as `{2, 0, 0, 0}.x`;
* a function call is `CAL BB<n>` with argument and return values copied through
  registers;
* `sqrt` is `RSQ` then `RCP`, per component — so GLSL.std.450 lowering is its
  own layer.

**None of that is in the emitter yet**, on purpose: the observations are from
listings, and the brief is source first.  The reading order that follows the
compiler's own grouping is the handler groups — one handler covers all four
`GreaterThan` comparisons, one covers all seven multiply forms, one covers all
32 image-sampling opcodes.

## The instrument that is not being used yet

`tools/trace_patch.py` puts a call at the top of any guest function in the port
tree, and `g2s_trace` in `runtime/g2s_hook.c` prints that function's incoming
argument registers when `G2S_TRACE` is set.  The port turns every guest
function into ordinary C, so this is a breakpoint that costs one recompiled
object instead of a scripted gdb session, and it can print guest *memory* — the
id table, the symbol being built — rather than only registers.

That is the tool for question 2 and 3 above, and it now works properly --
`notes/11-tracer.md` records the two wrong versions and why the second one was
so hard to localise (the port's own SIGSEGV handler reports a fault inside the
instrument as a guest fault).  With the compiler's string-append function
instrumented, the whole listing's construction is readable, `$vout.PSIZE`
included.  Before that: instrumenting the
reader entry, the type handler, the variable handler and the GLASM printer, and
compiling one module, showed that **the module is read twice and the listing
comes from the second read** -- `glslcCompile` is a pre-specialized compile
followed by a specialized one.  `notes/05-two-passes.md` has the evidence and
what it means for the converter.  Adding the instrumentation changed nothing:
the 90-probe round trip is still `identical 90  differing 0  failed 0`.
