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
Python side does not claim.  Current figures (notes/114 §78 and notes/115-130):

```
probes        (680)   exact 680   prefix-only 0   DIFFERS 0   failed 0
regression    (500)   exact 500   prefix-only 0   DIFFERS 0   failed 0
the 60 that once     exact 13   prefix-only 0   DIFFERS 47   failed 0
refused
```

Re-measured 2026-09-23: the 60 give 15,918 of 46,419 listing lines (34.3%),
the probes 59,053 of 59,053 (100%) and the 120-shader corpus sample 120,351
of 120,351 (100%).

**PREFIX-ONLY IS 0.**  That is the number this campaign was about.  It was
60 over the whole corpus when the refusal census was taken (notes/114 §78)
and it is now none: every module converts to the end.

THE TWO SETS THAT GATE EVERY CHANGE:

* `tools/probecheck.py` -- 680 probes, about a minute.  Run it FIRST after
  any edit: it covers 680 distinct rules and tells you which one broke.
* `tools/exact500b.stems` -- 500 corpus shaders drawn at random from the ones that
  were exact, about 8 minutes.  Run it before calling a cause closed.

`DIFFERS` is now the whole of the remaining work, and it is not a
regression: a module that used to stop at its declarations now emits its
body, where it meets whatever the refusal had been hiding.  Of the 60
modules that refused, 13 are byte-exact and 47 differ somewhere in the
body.

WHAT THE PROBES ARE NAMED.  Every probe carries the number of the note that
reads it -- `0104_pu_j.vert`, `0129_...` -- so a listing and the reading
behind it are one grep apart.  `0000_` means the probe predates the note
that would name it.  `listings_open/` holds the ORACLE's listing for a probe
whose rule is not implemented yet: it is evidence, not a gate, and moves
into `listings/` when the rule is read.

## The body: where it now stands

The body is EMITTED, in full, for every module in the corpus.  This section
used to say "still unemitted"; what it describes -- `tools/nodedump.py`
pairing every GLASM line with the IR node that produced it -- is now the
instrument the readings are made with rather than a map of unexplored
ground.

What it gives, and what the converter is built on:

* the node layout -- opcode and modifier at +8, destination virtual register
  at +32, write mask at +48, operand links at +64/+72, destination operand at
  +136, flags at +152 (notes/29);
* `node[36]` (the statement stamp) and `entry[68]` (pass 1's position), which
  together are the scheduler's two keys (notes/51);
* the register allocator from both ends at once: `vr3..vr7` are `R0..R4` and
  `vr10`, `vr13`, `vr16` are `R0` again, so `TEMP R0..R4` and `# 5 R-regs`
  count PHYSICAL registers where the IR has eleven virtual ones.

ITS ONE BLIND SPOT, and it is load-bearing: the `--node` tracer is not on
the printer that emits MEMORY and TEXTURE lines, so `nodedump` reports
`<no node record>` for an `LDB`, an `STB` or a `TXG`.  Anything that turns
on one of those needs `tools/p1cmp.py` (which compares the two pass-1
orders and needs no printed order) instead -- see notes/120 §1, where a
gather's opcode had to be recorded as UNREAD for exactly this reason.

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

## What is left

**DIFFERS, and nothing else.**  The `#var` block, the declarations, the
`used` column and the instruction body are all done -- that list, which this
section used to hold, is history.  Every module now converts to the end, so
the remaining work is entirely "our line is not the compiler's line".

Where the differences are, from the 60 modules that once refused (47 of them
differ) and from the corpus sweep:

* **THE REGISTER ALLOCATOR.**  Most surviving differs are a register number
  or a `TEMP R0..Rn` count, not a wrong instruction.  Ours is a linear scan
  (`glasmlib/alloc.py`); the compiler's is the transcribed one
  (`py/regalloc.py`, `py/ifg.py`) and the two do not coalesce alike.
* **THE PER-LOOP FLAG.**  `post_ssgi.frag` wants `SHORT TEMP H0, H1;` where
  we declare `H0`: nested loops get a `continue` flag EACH, and `_CFLAG_REG`
  models one per shader (notes/122).
* **THE SHARED-MEMORY BODY.**  `probes/0119_bar_a.comp` and
  `0127_atoms_a.comp` reach the end and differ by an extra MOV and a
  register count; their oracle listings are in `listings_open/`
  (notes/124).
* **`listings_open/`** holds the rest: a probe whose rule is read but not
  implemented keeps the oracle's listing there rather than in the gate.

## How to work on one

1. `tools/compare.py <listing-dir> <spv-dir> --only <stems>` to see the
   first line that differs.
2. Write the MINIMAL probe for it, compile it, take the ORACLE's listing,
   and see whether the probe reproduces the difference.  A corpus shader
   confirms a whole listing; a probe says which rule the listing was
   testing.  Twelve readings went in against corpus shaders alone in one
   session and the first probe written for one of them failed immediately
   (notes/121 §2).
3. Read the rule -- from the recompiled source (`/home/claude/work/ex/src`),
   the traces (`G2S_TRACE=1 G2S_ONLY=<gate>`), or `tools/nodedump.py` --
   and only then edit.
4. `tools/probecheck.py` (a minute), then the 500-shader set (8 minutes).

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
