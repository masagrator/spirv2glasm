# Handover

Read this first (`SETUP.md` is the checklist for restoring the machine
state from the package), then `SPIRV2GLASM.md`, then `PYTHON-STATUS.md`, then
`notes/` from the highest number down.  This file is the WORKFLOW: what the
machine has to have, how the instruments are built and run, and what the next
session should do.

---

## 1.  The brief, unchanged

Build a SPIR-V → GLASM converter for GLSLC 17.24 **by reading the compiler's
own code**, never by fitting examples.

Standing constraints, all still in force:

* every function is replicated in full -- no corner cutting;
* a variable that stops being used mid-function gets a comment saying so where
  the warning is suppressed;
* **no list fitting.**  A rule is READ out of the image or the port; a listing
  may only ever CHECK a rule that is already read.  "I see you still test
  hypotheses instead of checking actual sources" is the failure mode to avoid;
* source and ELF come first, shader files second;
* test with **no optimisations** (`--opt-level none`) and only the five
  non-compute stages (compute is a different pipeline and is refused by
  design);
* GLASM must come out in the forms of **both** the debug-info section and the
  control fat section.  `--debug-info none` is NOT a blanket rule -- see §2.6
  and notes/50; it is one of the two forms, and the two differ by more than
  comments;
* verification is ONE script, `tools/check.sh`, and `DIFFERS` must stay 0;
* the goal is 100% reproduction of the WHOLE listing;
* no progress reports unless asked -- work.

### The number that matters

`tools/census.py` weights every refusal by the LISTING LINES it costs, over
the 517 saved corpus listings.  Shader counts are misleading; this is not:

```
 81202 lines   33 files  a branch arm that does not join at the merge
 22010 lines   52 files  a computation after a store (hoisted above it)
  7780 lines   12 files  a local store with no operand form
  6356 lines   15 files  opcode 86 in the body
  2891 lines    6 files  a load that is not a straight interface read
```

(re-measured after notes/70; the table below this paragraph's first version
had `a load that is not a straight interface read` second at 26566 lines --
the block-load suffix read moved those files on to the next refusal)

-- re-measured over the 120 listings in this package.  The ranking is the same
one the 517-listing run gave, at the same proportions: the top three are 96%
of the corpus's lines, and two of them ARE the register question.

Anything that is not one of the top three is not the priority, however
satisfying it is to close.  Probes are a regression net, not a scoreboard.

---

## 2.  The machine

### 2.1  GCC 15 is a hard requirement

The generated port is C23 and uses `#embed`.  GCC 13 (Ubuntu noble's default)
cannot compile it, and GCC 15 lowers `#embed` to the `.base64` assembler
directive, which needs **binutils >= 2.44** -- noble ships 2.42, so both have
to come from elsewhere.  Do not try to work around `#embed` and do not reach
for clang; that was tried and rejected.

They are installed by unpacking Ubuntu plucky debs into a private root, which
needs no root privileges and does not disturb the system compiler:

```sh
mkdir -p /opt/tc/debs /opt/tc/root && cd /opt/tc/debs
# from plucky: gcc-15, gcc-15-base, gcc-15-x86-64-linux-gnu, cpp-15,
# cpp-15-x86-64-linux-gnu, libgcc-15-dev, libisl23, libmpc3, libmpfr6,
# binutils, binutils-x86-64-linux-gnu, libbinutils
for d in *.deb; do dpkg-deb -x "$d" /opt/tc/root; done
```

and driven through two wrappers, which is what every build in this package
uses:

```sh
cat /opt/tc/cc15
#!/bin/sh
export LD_LIBRARY_PATH=/opt/tc/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
exec /opt/tc/root/usr/bin/gcc-15 -B/opt/tc/root/usr/bin/ "$@"

cat /opt/tc/ar15
#!/bin/sh
export LD_LIBRARY_PATH=/opt/tc/root/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
exec /opt/tc/root/usr/bin/ar "$@"
```

`-B` is what makes GCC 15 use THAT binutils' `as`/`ld` rather than the
system's 2.42.  Check with `/opt/tc/cc15 --version` -- it must say 15.

### 2.2  HTTPS goes through the agent proxy

Outbound HTTPS is tunnelled through a local proxy (`$HTTPS_PROXY`, e.g.
`http://127.0.0.1:40975`) that re-terminates TLS, so every tool must trust
`/root/.ccr/ca-bundle.crt`.  The usual env vars are pre-set.  When a fetch
fails:

```sh
curl -sS "$HTTPS_PROXY/__agentproxy/status"     # proxy state and last failures
```

then point the offending tool at the bundle (`--cacert`, `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, `PIP_CERT`, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO`...).
**Never disable TLS verification and never unset `HTTPS_PROXY`**; a 403/407 is
an organization policy denial, which is reported, not retried.  `apt-get` and
`pip` work through it; the web is a legitimate source of tools, not just apt.

### 2.3  QEMU is how the original ELF is run

The AArch64 image is not native here.  To compare the port against the
ORIGINAL library, build a reference binary and run it under qemu with
`qemu-ref.sh` in the port tree:

```sh
ELF=subsdk0.elf ./qemu-ref.sh test.c        # -> /tmp/ref_bin, run under qemu
```

It links against the target's real AArch64 libstdc++, names the runtime file
after the SONAME (`glslc.nss` for subsdk0) and stubs two SDK internals.  Use
it whenever a reading has to be checked against the real thing rather than
against the translation.

### 2.4  Background jobs: `run125.sh` / `poll125.sh`

**A bash command must not exceed two minutes**, so anything longer runs in the
background with the two scripts in the port tree, never with a long `sleep`:

```sh
cd /home/claude/work/ex/port/glslcportv9
./run125.sh mk 3600 make -C /home/claude/work/ex/src CC=/opt/tc/cc15 AR=/opt/tc/ar15 -j4 spirv2glasm
./poll125.sh mk 110               # prints mk=0, "mk RUNNING" or "mk DIED"
```

THE SECOND ARGUMENT IS THE JOB'S OWN TIMEOUT AND HAS NOTHING TO DO WITH THE
TWO-MINUTE LIMIT ON A BASH COMMAND -- that one is handled by polling, once per
turn, for as many turns as the job needs.  Size it for the job.  The 900 this
example used to show was sized for a one-file `g2s_hook.c` rebuild, and when
a change to `include/guest_rt.h` turned the job into a 45-minute full-port
rebuild it was copied over unchanged; the job was killed at 900 seconds twice
and relaunched by hand before anyone looked at the number.  A timeout is there
to stop a HUNG job, so make it comfortably longer than the job's honest worst
case and let `poll125.sh` report the progress.

`poll125.sh` distinguishes a finished job from a dead one, which a bare marker
file cannot: a background job is killed when the turn that launched it ends.
`run125.sh` refuses to start a second copy of a conflicting tag -- two makes
over one tree race on `build/libguest.a.tmp`.

A rebuild after touching `runtime/g2s_hook.c` is about four minutes, i.e.
three poll cycles, because that file is ONE translation unit.  A change to
`include/guest_rt.h` is a different animal: every one of the 53,733 generated
`src/fn/f_*.c` files includes it, so the whole port recompiles at about 14
files a second -- roughly 45 minutes.  Measured while adding the write watch.
Put an instrument in `g2s_hook.c` unless it genuinely has to see every store.  Batch instrument changes: one rebuild for several
hooks.

### 2.6  The three listing forms -- do not pin to one

`--opt-level none` is a constant.  `--debug-info` is NOT:

```
--debug-info none   the plain body.  This is the DEFAULT, so `--opt-level
                    none` alone reproduces every saved listing in
                    listings/ and corpus_listings/ byte for byte; passing
                    `--debug-info none` as well changes nothing.
--debug-info g0/g1  adds `#MSDB: Sloc <n> <file>:<line>` before each
                    instruction -- and ONE EXTRA INSTRUCTION.
--debug-info g2     adds `#MSDB: Inst <n> <mnemonic> {dest} <file>:<line> ...`
                    and the same extra instruction.
```

The extra instruction is the point: on `op_mul.vert` a `MOV.F R0, R0;` appears
with debug info on and is absent with it off, so **a shape measured under one
form is that form's shape**.  The brief asks for both, so:

* comparing against a saved listing -> `--opt-level none`;
* comparing against a shader's debug-info section (what `tools/verify.sh` and
  `tools/checklisting.py` do) -> `--opt-level none --debug-info g2`;
* a hook or a dump -> the form whose shape is in question, and BOTH before a
  shape becomes an emitter rule.

The instrument scripts take the option set from `$SPIRV2GLASM_OPTS` and
default to the saved-listing form.

### 2.5  Layout

```
/home/claude/work/ex/port/glslcportv9   the original ELF, run125/poll125, qemu-ref
/home/claude/work/ex/src                the PORT TREE: 53,748 generated C files
                                        src/fn/f_<guestaddr>.c, runtime/, build/
/home/claude/work/proj                  the repository root: .github/ + spirv2glasm/
/home/claude/work/proj/spirv2glasm      THIS PROJECT
/home/claude/work/corpus_spv_full       the corpus as SPIR-V: 14,630 of 14,706 (corpus/README.md)
/home/claude/work/corpus_spv            the 120-shader sample (SETUP.md §3)
/home/claude/work/corpus_listings       the oracle's listings for the sample
```

Guest address = ELF vaddr + 0x7100000000, which is how the port names files.

### 2.7  The converter's code (notes/92)

`py/glasm.py` is only the public face; the output side lives in
`py/glasmlib/` (see its package docstring for the module map) and the body
lowering in `py/glasmlib/lower/`, one mixin class per instruction family over
`Core`, whose `ARMS` tuple is the order the arms are tried in.  A new
instruction is a new `_arm_<name>` method in the family's module and a place
in `ARMS`.

Every SPIR-V number is written by its Khronos name through `py/spvnames.py`
(`Op.OpLoad`, `GLSL450.FClamp`, `StorageClass.Output`, ...), which reads the
unmodified Khronos files in `py/khronos/`.  `tools/opnumlint.py` must report
0.  The compiler's own numbers have the same kind of table (notes/93):
`py/glslc/glasm.py`, generated by `tools/mkglasmdefs.py` from what
`tools/opname_dec.py` and `tools/typesuffix.py` read out of the image, and
read through `py/glasmnames.py` (`Op.MUL`, `Op.MOV_47`, `Type.F32`).  Which
opcode plays which role (the assignment MOV is `Op.MOV_47`) is in
`glasmlib/nodes.py`.  The hex numbers left in the code are binding kinds and
slots (`glasmlib/semantics.py`, `py/binding.py`).

---

## 3.  The instruments, and how to add one

The oracle is the port itself: `build/spirv2glasm`, which runs GLSLC's own
front end.  Everything is measured by patching a `g2s_*` hook into the ONE
generated C file that matters and rebuilding just that object.

### 3.1  Patching a hook

```sh
cd /home/claude/work/proj/spirv2glasm
python3 tools/apply_hooks.py /home/claude/work/ex/src     # ALL of HOOKS.tsv
python3 tools/trace_patch.py /home/claude/work/ex/src --fold 56fa0
python3 tools/indirect_patch.py /home/claude/work/ex/src 45530 43460
```

`apply_hooks.py` is what to run after a regeneration: it installs every hook
in `patches/HOOKS.tsv`, in all three of the forms §3.1 describes, and it is
not a convenience wrapper over `trace_patch.py` -- that script's idempotence
test is `"g2s_trace" in src`, so the SECOND hook on a file that already has
one is silently skipped, and 19 addresses in the table carry two.  It also
places the two hooks the table puts at a LABEL, which this section otherwise
says to do by hand.  One entry does not install and says so:
`g2s_indirect` on `f_7100eeff94`, which has no indirect call in this tree.


* `trace_patch.py` inserts a call at the top of `f_<addr>`.  **Above the
  entry-dispatch line**, not below it: the port enters a TAIL-CALLED function
  with a non-zero `entry`, so a hook below the dispatch branch misses every
  tail-call arrival.  That bug hid whole classes of call and sent two readings
  down the wrong path (notes/11, notes/47 §5).
* `indirect_patch.py` names every indirect call a function makes, passing the
  call's RETURN ADDRESS as the site number.  Vtable slot numbers collide
  across objects; return addresses do not (notes/45, notes/48).
* For a hook in the middle of a function, patch the generated label by hand:
  the block at guest `0xf11548` is `L_7100f11548:` in
  `src/fn/f_7100f10a30.c`, and registers are `GST_I64(8 * n)` for `Xn`.

Then rebuild (§2.4) and run with the gate:

```sh
G2S_TRACE=1 G2S_ONLY=fold,node build/spirv2glasm \
    --opt-level none probes/op_mul.vert.spv 2>trace 1>/dev/null
# and the same run with --debug-info g2 when the debug-info form is the
# one in question (§2.6)
```

`G2S_TRACE` turns tracing on at all; `G2S_ONLY` is a comma-separated list of
hooks that may print.  Without it a real corpus shader produces ~2.4M lines
and times out; with it, about a second.  Always read guest memory through
`g2s_try_u64` -- it runs under a SIGSEGV handler and prints `?` instead of
killing the run, and a plain range test is not a substitute (notes/11).

### 3.2  The hooks that exist

| gate | hook | what it shows |
|---|---|---|
| `fold` | `g2s_trace_fold` on `f_7100056fa0` | EVERY node of the emit list: opcode, `node[32]` as `seq=low/high`, mask, vreg, and each source's node, modifier and swizzle.  A complete DAG dump. |
| `node` | `g2s_trace_node` on `f_710005c1dc`/`c71c` | the nodes the two line printers take, 384 bytes each, in printed order.  The ground truth for "which nodes print". |
| `emit` | `g2s_trace_emit` on `f_7100030c74` and `f_7100030cfc` | every instruction linked into a list, front-push and back-push separately. |
| `pick` | `g2s_trace_pick` / `g2s_trace_picked` on `f_710004b930` | the scheduler's candidates with all comparator keys, and the entry it kept. |
| `graph` | `g2s_dump_graph` on `f_7100045530` | the whole interference graph: per vreg its colour, chain, type, and every neighbour with four component masks. |
| `vregs` | `g2s_dump_vregs` | the allocator's 224-byte records. |
| `carrier` | `g2s_trace_carrier` at `0xf11548` | the assignment lowering's materialisation test. |
| `expr` | `g2s_trace_expr` on `f_7100f10a30` | one line per IR expression lowered, with its operation number and caller. |
| `sel` | `g2s_trace_sel` / `g2s_trace_selected` on `f_710004ba90` | the SECOND pass's candidate list at every call, with both comparator keys per entry, and what it chose.  `g2s_trace_pick`'s counterpart for the scheduler that produces the printed order. |
| `block` | `g2s_trace_block` on `f_710004a2e0` | the per-block emitter's INPUT at its entry: the block, its `block[32]` list, its terminator, and every `block[80]` entry with its node, opcode, pending-use count and mask.  This is what lets the ORDER be tested without a DAG builder. |
| `indirect` | `g2s_indirect` | `site=<return address> target=f_<addr>` for every indirect call in a patched function. |
| `wstmt` `cgtree` `cgnew` `cgif` `irloop` | notes/71 §5 | the IR statements the walker dispatches; the cgc statement tree between the pass driver's steps; every cgc node interned (with an optional backtrace); the reader's cgc ifs; the IR loop. |

Plus the older ones (`fmt`, `semtab`, `sym`, `regtab`, `vtable`, `idsets`,
`bodylist`, `bodycount`, `dag`, `alloc`, `proto`, `factory`, `opcodes`,
`opmap`, `setop`, `setmask`, `mknode`, `desc`, `irop`, and the one-shot
dumpers for builtins, names, ext sets and opnames).

### 3.3  Verification and measurement

```sh
tools/check.sh          # THE verification.  Corpus + probes, line for line,
                        # plus the refusal counts.  DIFFERS must be 0.
CENSUS=1 tools/check.sh # the same, plus the LINE-WEIGHTED census.  Off by
                        # default only because re-lowering all 517 corpus
                        # modules would push the command past two minutes.
tools/census.py         # that census on its own -- read it before choosing
                        # what to work on (section 1).
tools/extshape.py       # what shape each GLSL.std.450 builtin lowers to,
                        # measured from the front end's nodes -> writes
                        # py/data/extinst_shape.json.  Re-run it when a probe
                        # that calls one builtin alone is added.
tools/extshape.py --gaps # which builtins the CORPUS uses that nothing has
                        # measured -- i.e. which probe to write next.
tools/emitorder.py      # the listing's order against the insertion trace.
tools/schedcheck.py     # the FIRST scheduling pass, against the printed
                        # order (notes/51).  89 of 113 probes.
tools/ordercheck.py     # the PRINTED order from the read rules: the second
                        # pass's whole cycle loop over the first pass's
                        # output.  113 of 113 probes.
tools/selcheck.py       # the SECOND pass's selection, call by call.
                        # 1791 of 1791 calls.  With no arguments it reads
                        # notes/sel_op_pow.txt, so the rule can be
                        # re-checked without building the oracle.
tools/recnum.py <spv> <simp-dump>       # the compiler's record NUMBERING
                        # against ours, paired by (first def, last use, mask)
tools/waredge.py <spv>  # scores a candidate anti-dependence order against
                        # every reader in a trace, not against one listing
tools/p2check.py <spv>  # our pass 2 on the COMPILER'S own blocks: its
                        # stamps, its entry[68], its edges, against the
                        # order it printed.  Exonerates (or convicts) the
                        # selector without touching the lowering.
tools/regmap.py <a.glasm> <b.glasm>     # pairs the register tokens of two
                        # listings -- no oracle -- and reports the renaming
                        # and THE FIRST LINE WHERE IT BREAKS
tools/ifgjoin.py <spv>  # the two interference graphs, joined through pass
                        # 1's lists.  The dump goes to a FILE (G2S_KEEPDUMP=
                        # <path> keeps it); G2S_TIMEOUT for a big shader.
tools/livecheck.py <spv>                # the compiler's live sets against ours
tools/compare.py <listings> <spv>       # one directory, line for line
tools/dis.sh 0x7100045530 0x200         # disassemble by GUEST address
                                        # (ELF=... points at subsdk0.elf)
tools/nodedump.py <spv>                 # pair each body line with its node
```

`check.sh` takes about 90 seconds and fits in one command; the per-block
sweeps (`parcheck.sh`, `parvar.sh`) take an hour and are for diagnosis only.

---

## 4.  Where it stands

```
corpus  (120 listings)   exact 120 prefix-only 0    DIFFERS 0  failed 0
                         120351 of 120351 lines (100.0%)
probes  (642 listings)    exact 642 prefix-only 0    DIFFERS 0  failed 0
                         57020 of 57020 lines (100.0%)
slice   (1,400 modules)  exact 962 prefix-only 438  DIFFERS 0  failed 0
full    (14,630 modules) the sweep of notes/114: 14,000 clean, and the
                         tail is 289 DIFFERS
                         out of the 630 newest listings, nearly all of them
                         a register difference on an identical line
```

(notes/111: storage images end to end -- STOREIM, LOADIM in every measured
format, the image memory in both scheduling passes -- compute built-ins,
STB chains; `notes/pending_probes/` is empty.  The probes ship as ONE
archive, `probes.7z` (probes/ and listings/); nothing from the corpus is in
the package, see SETUP.md.)

(notes/114: the full-corpus sweep (14,630 modules, the oracle's own listings
for all of them) -- TWENTY-FOUR causes so far, each read from a trace.  The
scheduler's: the condition records' place in the R live array (§5), a static
load's node and record made AT ITS READER (§6), the component an
anti-dependence is made for -- the one its writer is the LAST to write (§8),
a handle pair's two loads made after their OR (§9), and a predicated write
reading its own destination LAST, after the value it writes (§20).  The
lowering's: a construct's lane read of a scalar stored whole (§2), a
retargeted load forwarded as the local (§3), a swizzle of lanes a merge's
pair passed through (§4), a plain copy transparent to a later lane read (§7),
`{-0, ...}` (§10), two merge chains in one block (§11), a select's result
stored to an output (§12, §18), a constant straight into its lane (§13), a
scalar interface operand as a whole register (§14), a construct statement as
a group whatever its size (§15), a copy of a local transparent PER COMPONENT
-- in the block (§16) and, through the source's register, across a block
boundary (§21), a shuffle that selects lanes of one construct (§17), a
PREDICATED select's arm taking the forwarded value where the branch form
reads the name (§19), the construct's lane-x load renamed by the
statement's flush (§22, it was being dropped as dead and the lane with it),
a NEGATED whole read of a merged local reading the merge, like the bare
and swizzled reads beside it (§23 -- the modifier is the operand slot's, not
part of the name), and a per-vertex input ARRAY whose first index is the
vertex, not a component (§24).
Every one has an off-switch named in the note and a probe of its own.

What is LEFT is the tail: of the 630 newest listings 210 are exact and 289
differ, and all but about a dozen of those 289 are THE SAME LINE WITH A
DIFFERENT REGISTER -- the allocator, not the lowering and not the order.
The other 14,000 modules sweep clean (DIFFERS 0 in all fourteen chunks).  `tools/p2check.py` runs
our pass 2 on the compiler's OWN blocks (its stamps, its `entry[68]`, its
edges) and every block of every shader tried passes, so the selector is
exonerated; `tools/recnum.py` now agrees with the compiler's record
NUMBERING on the shaders tried.  So the next read is the interference graph
and the colouring: `tools/ifgjoin.py` joins the two graphs through pass 1's
lists and streams the `ifg` dump from a file (it used to read it into a
string and be killed by the memory limit -- see `ifgcheck.Dump`), and
`tools/livecheck.py` still comes out with an empty def attribution at the
positions in question, which is the thing to fix first.

Use these rather than narrowing a rule until a listing matches:
`tools/recnum.py` (the record numbering itself), `tools/waredge.py` (scores
a candidate anti-dependence order against every reader in a trace),
`tools/p2check.py` (pass 2 on the compiler's own input), `tools/regmap.py`
(pairs the register tokens of two listings with no oracle at all and reports
the first line where the renaming breaks -- it is what split the tail into
its colouring and lowering halves).  notes/114 also records the rules that
were READ AND DROPPED, with what contradicted each; a candidate that fixes
one shader and breaks the shader an existing rule was read on is not a
rule.)

(notes/113: a trailing `ENDREP`/`ENDIF` -- inside the last block, with
the RET -- now gets its dataflow edges, `ifg._trailing_structure`; a lane
of a splat of a scalar local's load goes straight in.  The full corpus's
`compute_volumefog_scatter-1` is exact.)

(notes/112: the compiler has NO `re` any more -- `py/lex.py` and small
named matchers, each quoting the pattern it replaced; `tools/lexfuzz.py`
fuzzes them against those patterns.  Keep it that way: new text matching in
`py/` is a scanner plus a `lexfuzz.py` check, never a regex.)

(notes/110: storage-image headers, swizzled reads of a forwarded merge,
write-after-read edges by component, the scalarised op's construct as a
band temp.)

(notes/109: the whole corpus sample is exact.  Position splats of a
shuffled local, merge lanes read through a swizzle, outputs as live-array
records, second colour stores, cube LOD, bool locals, flushed names as band
records, the scalarised 2..3-lane ops ON (merge-lane reads, shared
reciprocals, construct lane-x seqs), a name read after a merge pair
(`_name_read_after_pair`, the scheduler's three refusals), and COMPUTE:
GROUP_SIZE and storage-buffer stores (`STB`).  CI: tools/probecheck.py
builds every probe, compute included, with glslang main.)

(notes/108: `bitfieldInsert` lowers (one BFI node), the one-lane read pass
f_7100069f90 is transcribed (glasmlib/replicate.py), derivatives are
lane-wise, and a dozen read/order rules the `chr_*` files needed.  Corpus
100 of 120 exact, 91,802 lines (76.4%), DIFFERS 0; probes 484/484.)

(notes/107: the slice has NO DIFFERS -- exact 343, 305,257 of 1,848,212
lines.  `map_3587d848` closed by three traced rules: each block's merge
chains are numbered first (walk 1 of `f_7100036a70`), the other temps in
creation order (walk 2), and the interference sweep walks the live ARRAY --
seeded ascending, uses appended, kills swap-removed -- not the nibble set.
Next by the census: OpBitFieldInsert (58,637 lines, 30 files), explicit-lod
/fetch non-2D coordinates (22,002, 12), OpAny (6,506, 2).)

(notes/106: the CONDITION REGISTERS are allocator class 1 -- each `.CC` set a
vreg, CC0/CC1, `IF`/`KIL` reading it; an RSQ goes first in a MUL (the
canonicaliser's MUL arm); every write of a name walks its lanes' implicit
reader lists; a 2D TXL reads x, y, w; a lane-x component store's halves share
one seq, other lanes two; a name read by an opened store restarts the name's
record.  The slice is at one DIFFERS, `map_3587d848`: the lowering temps'
record order, notes/77 §4's open item.  cc_a..d, ms_a, ow_a, on_a, on_b,
lv2_a, tl_e, nr_a, pt_i, pt_j.)

(notes/105: `_arm_logical` lowered fifteen more corpus files and all fifteen
DIFFERED; four traced rules bring it to one -- `abs` of a local component
keeps its lane and takes the merge pair's seq; the implicit reads are one
list per written component, walked by both halves of a component store; a
sampled image's `OR.S` is a name, live out of its block; a scalar temp whose
output store opens a block is stored in the old block and the store reads the
name.  The one left, `map_02774da9`, is an R2/R3 swap under investigation with
`tools/ifgjoin.py`.  ab_a, hd_f, uo_a, uo_b, kd_a, la_a, la_b.)

A wider sample lives outside the package: 1,400 corpus shaders with their
oracle listings (made with `tools/mklistings.py`).  The 26 of them that
differed before notes/101 are all exact now (notes/104); the whole slice is
re-run in the background with `tools/compare.py <lst> <spv>`.

(notes/104: the LONG handles are COLOURED -- class 4 of the same allocator,
one record per `LDC.U64`/`OR.S`, ORs first; a handle load is one node per
location per block, the OR its own; an insert reads a scalar block load's
node (traced); colour outputs chain per component in pass 2's edges; a
construct gathers every component of a vector into lanes past x (STILL
LISTING-DERIVED, to be traced); a matrix part is a fresh load and a MOV at
every read, and a store's only source node writes the destination (traced
with gdb and the fold dumps, §7); a constructor's leading run of one node is
one merged write, a load or a swizzle's MOV is lane x itself (§8); a select
on a built bool vector sets RC lane by lane, on a stored lane from the
local's lane (§9).  hd_a..e, lf_c..g, sw_a, cl_a..g, lx_a, lx_b, ld_mx,
mx_b..h, pu_f..j; cr_n and lf_f moved to notes/ub_probes (undefined reads).
Every probe is exact.  Open: `f_7100061760` itself (the constructor's
expansion) is not read line by line; its output is.)

(notes/102-103: name reads restart the record, the merge reader's seq, the
traced lane rules, the operand canonicaliser; vertex merges and construct
lanes.)

(notes/101: a read that IS the stored value -- a plain store into lane x --
against an insert or a component select (RC against HC for a select's
`.CC`); a multi-lane selection is materialised by its store; output lanes
take the value's MOV type; a construct's lane read gathers; one store per
temp, the colour store tied to the forwarded store; an abs of a merge takes
the merge's seq; a whole copy of a partly stored local is a pair; a
constant splat is a 4-slot constant.  25 probes, probes 373.)

(notes/100: vector selects on a splat of a bool -- a normalised compare's
`RC.c`/`(NE.c)`, a whole bool local read as any load; a shuffle or extract
holds no node.  A fitted temp-store rule retracted.  sel_e..sel_i.)

(notes/99: shadow samples -- the coordinate rebuilt with the reference, the
separate sampler's handles first; a construct's writes of components 1..
first, then component 0, then the gathers.  sa_a..sa_d, ta_a.)

(notes/98: a dot takes constant operands, padded, constant second.
dt_a..dt_d, probes 340.)

(notes/97: a constant store opening a block writes the constant, no
carrier; a colour store from a local's NAME read has no self-move; a whole
vec4 local takes a swizzled source; a forwarded scalar constant broadcasts
`.x`.  lc_a..lc_d, probes 336.)

(notes/96: DDX/DDY/fwidth of a vector: one instruction at the result's
mask.  dd_a..dd_e, probes 332.)

(notes/95: a vector select is `MOV t, false; MOV.U.CC RC.mask, c; MOV
t(NE), true` -- predicated writes read RC and their destination in
sched.parse; a select on a stored bool's component reads it as the branch
does, into RC.  sel_a..sel_d, probes 327.)

(notes/94: a non-constant texture lod is the coordinate construct's `.w`
written from the value; tl_a..tl_d exact, probes 323.)

(notes/93: no rule change -- the GLASM opcodes and type codes as a
table in spirv.py's form, `py/glslc/glasm.py`; the namer decode redone by
simulation, which fixed thirteen garbled ATOM names, TXG/TXP/TXD read as
TXF/TXL/TXB, and dropped ATOMS.* the namers do not spell.)

(notes/92: no rule change -- `glasm.py` split into `glasmlib/` and
`glasmlib/lower/`, the long functions of sched/regalloc/liveness/ifg/binding
split, every SPIR-V number by its Khronos name.  The kill arm's 4417 was
`OpTypeUntypedPointerKHR`; it is now `OpDemoteToHelperInvocation`.  Listings
unchanged.)

(notes/91: a component store's pair is ONE seq unless the value is an
extract or the store opened its block without reading the local; a store to
a pending vertex output opens a block, no self-move; a store from a forwarded
read joins the first store's tie; builtin operands print their selectors;
FClamp takes the result's mask and a constant second.  basic_039bd1ee exact:
corpus 56, probes 319, DIFFERS 0.)

(notes/90: the general vector divide (per-lane RCP + MUL, or DIV when the
divisor selects one component), constant second in commutative add/mul,
two-component swizzles pad with the identity, WAR order sources last first,
loop back edges in the liveness, shuffle partial lsplit.)

(notes/89: two dynamic indices in a structured-buffer address, the
identity folds (`x+0`, `x*1`, ... on scalars), the later-block dynamic load
refusal lifted (`G2S_DYNLATERSTRICT` restores), input-component local
stores forward the register.  Top refusal now: divides, 74989 lines.)

(notes/88: gl_FrontFacing as the `facing > 0` expression its reader builds,
`MOV.S.CC HC.x, -c` folded; integer immediates with bit 31 set print `0x%x`
for unsigned types (f_710003dc10).  Top refusal now: a dynamically indexed
block load read in a later block, 40969 lines.)

(notes/87: vector bitcasts, shift lanes, the corpus's dither prologue
(cuts `mq_n*`, all exact), and three allocation rules found through
`ps_c.frag`: LONG handles count as single-component records, a 2D TEX
reads two coordinate lanes, a whole local read takes the components the
local ever stores (`@m` read-mask annotation).  `notes/ub_probes/` keeps
a hand cut that reads an undefined component.)

(notes/86: OpConvertFToS/FToU -> `TRUNC.<dst sign>`, the MOV legaliser's
reading; pf_a..pf_c.  Next: vector float/integer bitcast (`bc_uf4.vert`,
30039 lines), opcode 196 operands (23279).)

(notes/85: an IF on a bool-vector component folds its `.CC` into the
component select -> `MOV.U.CC HC.x, R3;` (`G2S_NOBCOMP`); component-store
MOVs take the local's type; `o = u` right after `u.c = ..` in one block
reads the MERGE, which becomes a lowering vreg copied into the local
(`G2S_NOMERGEFWD`); scoped `(name, line)` pass-1 keys.  Probes pb_a..pb_d.)

(notes/84: constant arrays in local memory -- `lm_icb`, `lm_icbf` exact.
Element MOVs + element stores, `lmem<k>` one name in sched.py (a store
reads it, a load releases its address first), `TEMP lmem<k>[N];`, and the
colour store's self-move renames like the vertex outputs'
(`G2S_NOCOLFLUSH=1`).  The corpus ICB files now stop on bool selects and
branches that are not comparisons, and on opcode 109.)

(notes/83: separate samplers (opcode 86: `LDC.U64` pair, `OR.S`, `LONG
TEMP`), TEX band results, coordinate swizzles, forwarding of a swizzled
local store, and a multiply by -1 as a negate MOV (`G2S_NONEGMUL=1` restores
the MUL).  Next: `OpConstantComposite` local stores / immediate-constant
`lmem` arrays, probes `lm_icb`/`lm_icbf`.)

(notes/82: the "computation after a store" refusal is LIFTED;
`G2S_STORESTRICT=1` restores it.  `G2S_LIFTSTORE` no longer does anything.
The next targets are the line-weighted census's top refusals:
`OpConstantComposite` local stores (63430 lines) and opcode 86 (25082).)

(notes/81.  Under `G2S_LIFTSTORE=1` the corpus is 29212 lines, 48 exact,
4 DIFFERS.)

(notes/80.  Under `G2S_LIFTSTORE=1` the probes are 225 exact with 0
DIFFERS, and the corpus is 27454 lines, 43 exact, 9 DIFFERS, most of them a
`MUL.S R2/R3` register choice in `chr_*`.)

(notes/79.  Under `G2S_LIFTSTORE=1` the probes are 215 exact with 0
DIFFERS, and the corpus is 26392 lines, 40 exact, 12 DIFFERS.  When the 12
are clean, drop the "computation after a store" refusal and `check.sh` will
report about 22% instead of 11.4%.)

(notes/78.  Under `G2S_LIFTSTORE=1` the probes are 209 exact and 2 DIFFER
(`mb_n29`/`n30`: the lowering's statement order inside a block, §5); the
corpus is 19978 lines.  `tools/ifgjoin.py` maps compiler vregs to
placeholders through pass 1's lists and diffs the two interference graphs.)

(notes/77: the `mb_*` cuts of `map_110833a5`.  Open: `mb_n18`'s register
choice, the likely common cause of the corpus TEMP-count DIFFERS under
`G2S_LIFTSTORE`.  Tools: `tools/vrjoin.py`, `G2S_VLINES`, `G2S_WALKS`,
`G2S_PICKTRACE`.)

(notes/76: `_reload` gives each block its own LDC, `_narrow_loads` sets
each LDC's mask and suffix, and component chains into uniform vector
members now lower.  `G2S_LIFTSTORE=1` corpus: 18862 lines, 29 DIFFERS.
Most are the TEMP count in `map_*`/`chr_cloth_*`; bisect them with
`probecut`.)

(notes/75: one interned node per load location per block, same-node
constructs, extracts, scalar broadcast.

`G2S_LIFTSTORE=1` corpus: 16645 lines, exact 8, 1 DIFFERS
(`chr_cloth_1c6be086`).  Once that one is clean, the next step is to lift the
"computation after a store" refusal for real.)

(notes/74: `gl_Position` component stores of computed values.  With
`G2S_LIFTSTORE=1`:

* probes: 186 exact;
* corpus: 16396 lines, exact 3 (`chr_eye` is one of them), 6 DIFFERS.

Next: `chr_cloth_*` (a DP4's register) and `map_*` (a pass-through copy).)

(notes/73 covers four rules, each confirmed against the compiler's edges with
`tools/gsum.py`:

* merge reads, in `py/sched.py` `_reads_merge`;
* position `.x` from an in-block component store;
* every stored local is materialised;
* pending component stores open a block.

Probes `ce_n30`..`ce_n47` and `pt_a`..`pt_f`.)

(notes/72 §9–§10: `ce_n29` is exact; it was cut with `tools/probecut.py`.
`G2S_LIFTSTORE=1 tools/compare.py` measures the corpus with only the
"computation after a store" refusal lifted.  That gives 15644 lines and 7
DIFFERS: six at a `R15`/`R14` choice in the bone construct, and
`map_0ae40bcc` at a pass-through MOV.  Bisect them with `probecut`.)

(notes/72: three fixes.

* Bitcasts are lowered as the reader builds them.
* Splat stores have no self-move.
* A temp is flushed at the `gl_Position` block close.

The 3 prefix-only probes are the next targets:

* `bc_uf4`: vector float bitcast;
* `lm_icb` and `lm_icbf`: local-memory arrays.)

(notes/71: loops with `break` and `continue` are read.  A constant-condition
`if` folds before the IR (`f_7100ef7d50`), so `while (true)` works; a stored
bool can be a branch condition; `continue` becomes the `H0` flag,
`SHORT TEMP H0;`.  Line-weighted census, top refusals now:

* a local store with no operand form: 63430 lines;
* a non-straight load: 27451;
* a computation after a store: 23002;
* opcode 86: 6356.

New hooks: `wstmt`, `irloop`, `cgif`, `cgnew`, `cgtree`.  The write watch is
inert in the current port build (its `guest_rt.h` is not the instrumented
one).)

(notes/70: lens_flare_ghost_tex.frag exact; block loads' masks and width
suffix read; nine new probes.  The Python converter now has a command line,
`spirv2glasm.py`, see README.)

### Machine notes from the last rebuild

* Ubuntu plucky's GCC 15.0.1 compiler packages are gone from the mirror;
  `gcc-15_15.2.0-4ubuntu4` (questing) + binutils 2.44-3ubuntu1 from
  `archive.ubuntu.com/ubuntu/pool/main/{g/gcc-15,b/binutils}` work the same
  way under `/opt/tc`.  The rebuilt oracle reproduces all 257 saved listings
  byte for byte.
* `aarch64-linux-gnu-objdump` (for `tools/dis.sh`) is `apt-get install
  binutils-aarch64-linux-gnu`; `ELF=` must point at
  `.../glslcportv9/subsdk0.elf`.
* The glasm2sass capture hook is one line in `src/fn/f_71010d76b0.c` after
  `f_7100ef60e0(cpu, 0);` plus its prototype; the Makefile's `spirv2glasm`
  target was added by hand (no glasm2sass target to anchor the patch on).
* `apply_hooks.py` misses one entry: `g2s_trace_regs` at `0x71000690a0` has
  no label in this tree; the `0x71000690a4` entry beside it (the one
  notes/67 uses) installs.

**The transcribed allocator is on by default** (notes/54, notes/55), and
with it the materialised local (`lo_parts`, `lo_half`, `lo_over` exact).
Every stage of the allocator is checked against the compiler on its own
inputs -- see step 3 of the plan below.  The `lo_*` order needed the
compiler's IMPLICIT READS (0x49b88) modelled in `py/sched.py`'s pass 1 for a
local's merge (notes/55 §7).  The block partition is now the compiler's
rule, **one store per name per block** (notes/55 §8), which put `if_out` and
`p04_out` right and lifted the "store to something else after a
gl_Position store" refusal.  And the scheduling now runs in the
compiler's sequence -- pass 1, allocate over pass 1's lists, pass 2 with
edges on the allocated registers (notes/57; `G2S_NOALLOC1=1` for the old
one).

**Control flow now covers `if`/`else`, `while`/`for` (notes/64), the
geometry stage's EmitVertex/EndPrimitive (notes/65) and `switch` (notes/66:
cgc's switch lowering makes an IF chain whose compare folds its `.CC`, and a
local store that opens a block reads the SPIR-V value's named temp) and
subroutine calls (notes/68: CAL, parameter copies, the return name, liveness
along the call edges).**  Every probe is exact; the one-component corpus
constructs are notes/67, and `lens_flare_ghost_tex.frag` stops at a vec2
local's component store (notes/67 §9).

**The scheduler is on by default** (notes/53).  `py/sched.order_pre` runs
notes/51's two passes on the converter's own lines, BEFORE `_allocate_
components`, so the emission order of every body is computed.  Three inputs
had to be fixed for that: pass 1 is the per-vreg release of notes/51 §2.1 and
not a successor count; `node[36]` is a SOURCE POSITION, so lines measured to
share one (a construct's four component writes, the four scalarised shifts,
the divide's four reciprocals and its multiply) share a key -- which is why
the scheduler must run before the allocator renames the values; and a list is
a block, one per output-component assignment.  `G2S_NOSCHED2=1` restores the
hand-written order.  Anything the emitter used to write in listing order now
owes the scheduler CREATION order instead -- the scalarised shift and the
divide's reciprocals both flipped to ascending.

What that opened, in one stretch: `OpCompositeConstruct` from different
values (four of five new `co_*.vert` probes exact), and then the whole
TRANSCENDENTAL FAMILY -- `cos`, `sin`, `exp2`, `log2`, `rsq`, `pow` are the
same construct with four per-component instructions in front of it, all six
exact, no new rule.  `tools/probecheck.py` is the new one-probe check.

The corpus figures are over the 120 saved listings THIS package travels with.
Earlier reports quote 517 over a corpus that is not in the archive set; the
ratios are the same and `tools/check.sh` derives its banners from what is
actually on disk rather than restating a number.

THE REGISTER ALLOCATOR is what the remaining probes wait on, and notes/52 has
it mapped end to end -- the engine, the first-fit scan over component slots,
the interference graph's two representations, the backward liveness sweep and
its edge rule.  Two things in it are unread, and BOTH are now the single
blocker in front of two constructs rather than a diffuse gap:

* **the visiting order** (notes/52 §9, new).  `g2s_trace_regpick` prints
  `record[48]`, `record[80]` and `record[16]` AT THE PICK, so each of the
  driver's five attempts is measured with its own costs.  That corrects §3c:
  every SINGLE-component vreg is visited in descending cost (equivalently
  descending index) on all three probes measured, and the cost is `3 * V`,
  not `4 * V`.  What is left is where the ONE multi-component record goes --
  it is at neither its cost rank nor its index rank.  That decides whether a
  composite construct shares a register with one of its own gathers, which is
  the anti-dependence notes/53 §7 needs and cannot derive; `co_add1.vert` is
  the probe that turns on it.
* **what a block boundary makes live** -- the older question, which is what
  `ifgcheck` still measures.

These tools are oracles against the compiler itself, not against listings
(`tools/regcheck.py`, which earlier versions of this list named, is not in
the package):

    tools/livecheck.py   the liveness, position by position
    tools/ifgcheck.py    a derived interference graph vs the compiler's
    tools/edgecheck.py   the scheduler's edge lists (1476/1476)

`ifgcheck` stands at 202 of 418 vregs; the gap is the one unread rule, and
notes/52 lists the candidate rules already ruled out by measurement so they
are not re-proposed.

`DIFFERS 0` has never moved off 0: nothing emitted disagrees with the
compiler.  What is emitted is the header, the `#var`/`#semantic` blocks, the
declarations, and the body of the single-block shaders whose every value is
already an operand.

### Read and wired

The opcode chain (SPIR-V op → worker operator → lowering family → emitter
opcode → mnemonic → type suffix → write mask), the `SUB` peephole, the `-O0`
store shape, register NAMING, the option set the listings were captured under,
the negate, the image instructions' mnemonics (notes/33) and the
GLSL.std.450 builtins that lower to one whole-vector instruction
(notes/39, `py/data/extinst_shape.json`).

### The ORDER, read end to end (notes/51, and it supersedes part of notes/49)

A LIST IS A BLOCK.  `f_710003bb10` walks `program[184]` and calls
`f_710004a2e0` once per block; every node that block schedules is linked into
its own `block[32]`.  There is no loop over lists inside the emitter, which is
what notes/49 was looking for.

And there are TWO scheduling passes, not one pass and a re-link:

* `f_710004a2e0` -- seeds the ready list from `block[80]`, takes the block's
  terminator first, then among ready entries takes the list's TAIL (the
  oldest), emitting a pick only if `node[48]` is non-zero and its opcode is
  not one of the five roots.  Release passes THROUGH opcodes 1, 0x3a, 0x57 and
  0x5a -- `f_7100049780` recurses into their slots instead of decrementing
  them -- which is what puts the instructions behind a merge chain on the list;
* `f_710004b220` -- moves each block's list aside and schedules it AGAIN, with
  its own selector `f_710004ba90` (readiness filter, smallest `node[36]`,
  earliest `entry[68]`), back-pushing each selection.  Its output is the
  printed order directly.

`tools/schedcheck.py` implements the first pass and reproduces the compiler's
PICK order node for node; over the probes it gets the printed order right on
**89 of 113**, and the 24 it misses are the blocks the second pass reorders.
`tools/selcheck.py` implements the second pass's gate and comparator and
reproduces its every choice: **113 probes, 1791 selector calls, 1791 OK**.

Two things in the second pass are worth carrying into any model of it:
`f_71000476f0` gates on a CYCLE CLOCK (`cg[28]` against the first pass's
`entry[52]`) with `cg[20]` as a resource mask, and `f_710004ba90`'s comparator
is a SCAN whose second test can overturn its first -- so it must be walked,
not sorted, and the ready list's order is part of the rule.

### Read but NOT yet wired -- this is the next session's work

**The operand slots** (notes/47).  Sources are at `node + 0xa8 + 0x28 * i`,
`node[153]` counts them, the destination is printed separately.  A slot is
`{type object, (modifier << 32) | type, flag, node, swizzle/mask}` and the
modifier is 0 none, 1 negate, 2 absolute value.

**The order** (notes/49).  The comparator's second arm shows `node[32]` is
zero on every node, so both arms always take the candidate and the scan keeps
the list's TAIL: among ready nodes the scheduler takes the OLDEST.  Two
insertion passes exist -- `f_7100030c74` builds each list front-first,
`f_7100030cfc` re-links back-first -- and the second stream IS the listing.
`tools/emitorder.py` confirms it on 90 of 90 probes.  What is left is what
OPENS a list, in `f_710004a2e0`'s walk over `block[80]` (one entry per
pre-scheduling instruction, its node at `entry[32]`).

**The allocator** (notes/48).  The colour is written at `0x45e54` in
`f_7100045530` as `record[64] = found & (-4 << alignment)`; `f_7100045ed0`
only ever writes -1 and is the simplify phase.  The path is
`f_710003bc94 -> f_71000473b0 -> {f_7100046a80, f_7100046b60 -> f_7100043460
-> f_7100045530}`, with `f_7100044e10` marking the taken byte offsets per
component and `f_7100bdfaa8` giving the width (1 for every type this emits, so
`R<N> = record[64] / 4`).  `g2s_dump_graph` dumps the whole input.

### The plan

0. **The second pass's WORKLIST ORDER** -- the last input the order takes
   from the compiler.  Everything else on the thread is now read and
   measured (notes/51):

       tools/schedcheck.py  113 probes    89 OK   24 MISMATCH  (pass 1 order)
       tools/selcheck.py    113 probes  1791 calls 1791 OK  0  (pass 2 picks)
       tools/ordercheck.py  113 probes   113 OK    0 MISMATCH  (the LISTING)

   `ordercheck.py` runs the whole second pass -- cycle clock (`cg[24] << 4`,
   `f_71000477a0`), resource records (`f_71000476f0` / `f_7100047768`, one
   issue per cycle on this profile), the scan comparator, the DAG's
   dependences and the WORKLIST (a stack; `entry[64]` counts unscheduled
   predecessors and `entry[52]` is raised to `clock + 1` on every release) --
   and reproduces the printed order on 92 probes.

   **The ORDER is reproduced in full.**  Every probe's scheduled order is the
   compiler's, node for node.  Nothing in the model is fitted: the cycle
   clock, the resource records, the gate, the scan, the worklist and the edge
   order are all read (notes/51 §2-§6).

   14 probes print one or two instructions that are in NO block's `block[32]`
   at the second pass's entry -- opcode 0x26 in the loops and geometry
   shaders, 0x95 in the switch.  Pass 1 never linked them, so the scheduler
   never sees them; `ordercheck.py` reports them as `+N unscheduled` rather
   than as mismatches.  The control-flow lowering puts them in the listing by
   a route the scheduler is not on, and reading that route is its own thread.

   **What the order still TAKES from the compiler** is where the next read is:
   the dependence edges themselves.  `ordercheck.py` is handed the successor
   lists, as it is handed `entry[52]` and `entry[68]`.  What decides an edge
   exists, and whether it is kind 0, 1 or 2, is `f_710004bd60`'s caller loop
   (0x4b5c0..0x4b608) and `f_710004ab80`'s component arithmetic
   (0x4ac2c..0x4ac6c) -- a liveness question, and the same one notes/48 leaves
   open for the allocator, so reading it serves both threads.

   After that, what stays the compiler's is `entry[52]` / `entry[68]` as the
   first pass leaves them, and the DAG builder's output: `node[36]`, the block
   partition and the initial `node[88]`.
1. ~~Give `py/glasm.py` a node IR instead of emitting text directly.~~  Not
   done, and no longer the blocker it was assumed to be: `py/sched.py` parses
   the converter's own lines back into (mnemonic, dest+mask, sources+masks),
   which is the same information, and the `#n` placeholders turned out to be
   an ASSET -- they are one name per vreg, which is what `node[36]` needs and
   what a register name destroys (notes/53 §2).
2. ~~Implement the scheduler.~~  DONE and on by default (notes/53):
   `sched.order_pre` runs both passes over the converter's lines, before the
   allocator.  `G2S_NOSCHED2=1` restores the hand-written order.
3. Implement the allocator properly: liveness -> per-component interference ->
   simplify -> the visiting order of notes/52 §9 -> lowest free aligned
   colour.  **Transcribed and checked stage by stage against the compiler**
   (notes/54, notes/55), each fed the compiler's own inputs:

       front-end dataflow  py/dataflow.py   tools/dfcheck.py     probes 121/121, corpus 119/119
       per-block seed      liveness.seed    tools/seedcheck.py   78427/78427 seeds (corpus)
       graph builder       liveness.build   tools/graphcheck.py  134/134, corpus 288/288
       driver + attempts   regalloc.allocate tools/drivercheck.py 134/134, corpus 288/288
       simplify + select   regalloc          tools/simpcheck.py   254 + 1104 attempts OK

   It runs on the converter's lines (py/ifg.py) and is ON BY DEFAULT
   (`G2S_NOREGALLOC=1` falls back to `_allocate_components`).  What is still
   a MODEL on that path is the front end: which converter values are cgc
   names, how blocks are formed, and the name numbering (notes/55 §5-6).
4. Only then open up loads of locals, local stores and control flow, which is
   where the corpus's 570k lines are.  The local assembled from parts is
   now emitted by default (notes/55 §7); the corpus's leading refusals are
   loops with break/continue ("a branch arm that does not join at the
   merge") and computation after a store.

Each step is verified against the compiler's own dumps, not against listings.

---

## 5.  Rules of the road

* Never emit a line that is not established.  `NotEstablished` with a reason
  is the correct output; a plausible guess is not.  That is why `DIFFERS` is 0
  and why it must stay 0.
* When a note turns out to be wrong, CORRECT IT IN PLACE with a pointer to the
  note that supersedes it (see notes/42, 43, 44 for the shape).  Wrong notes
  cost more than missing ones.
* Measure with the options of whatever is being compared against, and say
  which form the measurement belongs to (§2.6, notes/50).  `--opt-level none`
  is constant; the debug-info level is not, and the two forms the brief asks
  for differ in the BODY, not only in comments.
* Don't reinvent a tool -- look in `tools/` first.
