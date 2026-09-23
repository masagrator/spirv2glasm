# Handover

Read this first (`SETUP.md` is the checklist for restoring the machine
state from the package), then `SPIRV2GLASM.md`, then `PYTHON-STATUS.md`, then
`notes/` -- **`notes/README.md` indexes all 130 by mechanism**, which is now
the way in: 115..130 are topic files (115..129 split out of 114), so "read from the
highest number down" no longer describes the layout.  This file is the WORKFLOW: what the
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

HISTORY, AND WHY IT IS STILL HERE.  `tools/census.py` weights every refusal
by the LISTING LINES it costs, because shader counts are misleading: one
refusal in a 3,000-line listing costs more than fifty in short ones.  Over
the 517 saved corpus listings it gave

```
 81202 lines   33 files  a branch arm that does not join at the merge
 22010 lines   52 files  a computation after a store (hoisted above it)
  7780 lines   12 files  a local store with no operand form
  6356 lines   15 files  opcode 86 in the body
  2891 lines    6 files  a load that is not a straight interface read
```

(re-measured after notes/70; the table's first version had `a load that is
not a straight interface read` second at 26566 lines -- the block-load
suffix read moved those files on to the next refusal), and the 120
listings in this package reproduced the same ranking at the same
proportions.

**THAT TABLE IS NOW EMPTY.**  Prefix-only is 0 (§4): no module refuses, so
`census.py` has nothing to weight and the causes above are all closed.  The
method is what survives -- weight by LINES, not by files -- and it now
belongs to DIFFERS: `fullcmp.py` gives lines matched and lines total per
module, so the same weighting is one `awk` away, and that is the ranking to
take before choosing what to work on next.

The rule the table taught still holds against the new ranking: work the
few causes that carry the lines, and leave the rest however satisfying it
is to close one.  Probes are a regression net, not a scoreboard.

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

It needs `gcc-aarch64-linux-gnu` and `g++-aarch64-linux-gnu`, which are NOT
installed by default here -- only the aarch64 binutils are, so the script
fails at the compile step until `apt-get install` has run.

`tools/qtest.c` is the ready-made subject: it drives `glslcCompile` with a
SPIR-V module and prints whether it returned, with no hook and no listing
(`g2s_hook.c` cannot be inserted into the untranslated library).  That is
enough to answer "does the ORIGINAL fault here too", which is how notes/131
established that seven opcodes crash GLSLC itself and not the port:

```sh
INC=/home/claude/work/ex/src/include ELF=subsdk0.elf ./qemu-ref.sh tools/qtest.c
LD_LIBRARY_PATH=/tmp/run qemu-aarch64 -L /usr/aarch64-linux-gnu \
    -E LD_LIBRARY_PATH=/tmp/run /tmp/ref_bin <module>.spv 1
```

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

The extra instruction is the point: on `0011_op_mul.vert` a `MOV.F R0, R0;` appears
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
    --opt-level none probes/0011_op_mul.vert.spv 2>trace 1>/dev/null
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
tools/degcmp.py <spv> <livecheck-out> [simp-dump]
                        # THE TWO INTERFERENCE GRAPHS BY DEGREE.  The
                        # compiler's graph does not have to be dumped:
                        # notes/52's edge rule means the `liveset` trace
                        # contains it.  With the simp dump it pairs the
                        # records as recnum does.  This read notes/114 §26.
tools/ifgdump.py <spv> <out>            # the `ifg` trace compacted (only
                        # the last rows per vreg): gigabytes -> megabytes.
                        # G2S_VREGAT=<addr> in the hook narrows the record
                        # dump to one patched site.
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

### 3.4  Reading the GLASM opcode off a line, and the GLSL front end

Two instruments were added with the reference rewrite (notes/134):

* `tools/opcodemap.py` files every printed line under the `node[8]` that
  printed it, via `tools/nodedump.py`.  This needs `g2s_trace_node` on
  `f_7100bd74f0` AS WELL AS on the two line printers -- the texture and
  memory lines are printed there and never reach `f_710005c1dc`, so without
  it no `TEX`, `LDC` or `ATOMB` line has any opcode evidence.  The hook is in
  the tree; if the port is regenerated, put it back.
  `MODULES=opcov/cover OUT=notes/opcode_evidence_cover.json` restricts the
  walk to the six cover modules, which is the fast path.
* `tools/glasmfromglsl.py` pulls the listing out of the `.nvn` that `glslc`
  writes for a GLSL source.  The GLSL front end accepts shaders the SPIR-V
  one refuses (fragment interlock, subroutines), so it is the only way to see
  `FSIB`/`FSIE` and `CALI`; and when the backend fails, `glslc` prints the
  listing it could not assemble, which is how the footprint family was read.
  NOT `--output-assembly`: that is SASS, and the GLASM sits at the same
  offset in the `.nvn` with or without it.

## 4.  Where it stands

```
probes      (680)     exact 680     prefix-only 0  DIFFERS   0  failed 0
regression  (500)     exact 500     prefix-only 0  DIFFERS   0  failed 0
FULL CORPUS (14,630)  exact 14,208  prefix-only 0  DIFFERS 422  failed 0
the 60 that once      exact 13      prefix-only 0  DIFFERS  47  failed 0
   refused
```

**THE NEXT PIECE OF WORK IS THE 422.**  The full sweep finished 2026-09-23
and they are the entire remainder: nothing is prefix-only and nothing fails.
Split them first --

```sh
S=<scratchpad>
python3 tools/diffkind.py --tsv $S/fullcmp.tsv $S/full_lst $S/full_spv
```

-- which prints RENAME / REORDER / MISSING per stem and a count at the end.
The census of 164 of them said 137 were the `map_*` family in the RENAME
class: same instructions, same TEMP budget, a permuted register assignment,
i.e. the allocator's visiting order.  Fix by cause, re-run the two gates, and
do not start a new capability until this is 0.

Re-measured 2026-09-23: the 60 give 15,918 of 46,419 listing lines (34.3%),
the probes 59,053 of 59,053 (100%) and the 120-shader corpus sample 120,351
of 120,351 (100%).

**PREFIX-ONLY IS 0.**  Every module in the corpus converts to the end.  The
refusal census (notes/114 §78) found 60 modules that stopped early, in 21
distinct causes; notes/115..130 close all of them.  What is left is DIFFERS
-- our line against the compiler's -- and most of it is the register
allocator.  `PYTHON-STATUS.md` has the breakdown.

### The two gates

* `python3 tools/probecheck.py -j 2` -- 680 probes, about a minute.  It
  prints `FAIL <probe>: <reason>` per failure, so run it FIRST and read its
  output; do NOT loop `spirv2glasm.py` over probes by hand, which is what it
  is for and ten times slower.
* `sh tools/exact500b.sh` -- 500 corpus shaders drawn from the ones that were
  exact, about 8 minutes.  Run before calling a cause closed.

### Probes and listings

* A probe is named for the note that reads it: `0104_pu_j.vert`,
  `0119_bar_a.comp`.  `0000_` means it predates the note that would name it.
  Rename with the note, not after it.
* `listings/` is the GATE: every listing there must match byte for byte.
* `listings_open/` is EVIDENCE: the oracle's listing for a probe whose rule
  is read but not implemented.  It moves into `listings/` when the rule
  lands.  Do not delete an oracle listing to make a suite green.
* Take a probe's listing with `spirv2glasm --opt-level none <spv> | sed
  '1{/bytes of SPIR-V/d}'` -- the oracle prints a banner line on stdout that
  no listing in `listings/` has.

### Two census tools, and which answers what

* `refcensus.py` (`ONLY=`, `OUT=`) -- one line per module, the last stderr
  line or empty.  A refusal is exactly what makes a listing prefix-only, so
  this is the PREFIX census and needs no oracle listing.  It says nothing
  about exact against differs.
* `fullcmp.py` (`OUT=`) -- one row per module: stem, kind, lines matched,
  lines total, in chunks of 200, appended as it goes.  This is the one to
  run when the question is "which modules differ".
* Both are RESUMABLE from their output file, which matters: a background
  job is killed at the TURN boundary, not only when its shell exits.  Relaunch
  and it picks up.  `sleep 570` in the foreground is allowed and is the
  cheapest way to wait on a long one.

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
* Don't reinvent a tool -- look in `tools/` first.  `FLAGS.md` is the same
  point for the ~300 `G2S_*` switches: every rule has one, and turning the
  newest off one at a time is how a regression is bisected without editing
  code.  177 of them have NO prose at their site, which is a real gap --
  when you touch one, write the sentence.  A switch typed out of a note may
  not exist any more: FLAGS.md's last table lists the ones the notes name
  and the code does not, and says which took over (`G2S_LOCALREG=1` in a
  note is `G2S_NOLOCALREG=1` inverted today).  And don't reinvent a
  FUNCTION either: grep for the name before adding one.  A
  `_per_vertex_location_operand` was added this session that silently
  shadowed an existing function of exactly that name (and reused its env
  flag), and cost three wrong reverts before the collision was found.
* A refusal message must name ONE condition.  The corpus's largest
  prefix-only bucket was a single string raised for three different
  conditions in two functions, which made 1,223 modules unreadable as a
  cause (notes/114 §78).  Two more followed the same pattern: "the
  scheduler's model does not place this body" for five modules whose real
  fault was that `BAR ;` does not LEX (notes/119 §2), and "an image op with
  extra operands ... the handle load lands inside that construction" for
  five whose real fault was a one-word `offset()` operand (notes/120 §2).
  When a message names a mechanism, check that it IS the mechanism.
* A census is evidence WITH A DATE.  Re-take it before choosing what to work
  on.  A stale one sent this session at the 8-shader tail of a list whose
  1,223-shader head had already closed itself (notes/114 §78).
* When four attempts in a row regress the probes, the model is probably
  right and the TRIGGER is wrong.  notes/129 is the worked example: four
  attempts at the vreg identity cost 40-240 probes each, and the fix was two
  guards in the function that decides whether to split the vreg at all.
