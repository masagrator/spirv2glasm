# Progress report

**PREFIX-ONLY IS 0.**  Every module of the 14,630-shader corpus converts to
the end.  The refusal census (notes/114 §78) found 60 that stopped early, in
21 distinct causes; notes/115..130 close every one.

```
probes      (680)     exact 680     prefix-only 0  DIFFERS   0  failed 0
regression  (500)     exact 500     prefix-only 0  DIFFERS   0  failed 0
FULL CORPUS (14,630)  exact 14,208  prefix-only 0  DIFFERS 422  failed 0
the 60 that once      exact 13      prefix-only 0  DIFFERS  47  failed 0
   refused
```

The full-corpus row is the sweep that finished 2026-09-23 (`tools/fullcmp.py`,
resumable; the tsv is in the scratchpad).  **97.1% of the corpus is byte-exact
and nothing stops early or fails.**  The 422 are the whole of the remaining
work and have not yet been split by kind -- `tools/diffkind.py --tsv` is the
tool for that, and the census of 164 of them said 137 were the `map_*` family
in the RENAME class, which is the register allocator's visiting order.

Re-measured 2026-09-23: the 60 give 15,918 of 46,419 listing lines (34.3%),
the probes 59,053 of 59,053 (100%) and the 120-shader corpus sample 120,351
of 120,351 (100%).

What remains is DIFFERS -- our line against the compiler's -- and most of it
is the register allocator.  `PYTHON-STATUS.md` has the breakdown; the
sections below are the history, newest first.

## The reference became one row per opcode, and the vocabulary got measured

No converter code changed here -- the rule is differs-to-0 before new
capability -- but the reference the converter is designed against did.

**Attribution.**  `f_7100bd74f0` prints the texture and memory lines itself
and carried only the return-address hook, so every `TEX`, `LDC` and `ATOMB`
line joined as `<no node record>` and no texture opcode had any evidence at
all.  With `g2s_trace_node` on it too, `tools/opcodemap.py` files every
printed line under the `node[8]` that printed it.  GLASM-REFERENCE.md is now
ONE ROW PER OPCODE NUMBER with a `why this number` column (notes/134).

What that column can now say, from measurement rather than from the spec:

* the second texture block is the OFFSET forms (`textureOffset` -> `0x19f`,
  `texture` -> `0xbc`), and TXD is the exception that keeps one number;
* `0xb2`/`0xb6`/`0x1a7`/`0x1a9` are the THREE-SOURCE forms, where a
  cube-array coordinate fills four lanes and the bias, LOD or compare becomes
  its own operand;
* `.LODCLAMP` is its own number (`0x1ab`, `0x1ac`, `0x1ad`, `0x1ae`, `0x205`),
  reached through `GL_ARB_sparse_texture_clamp`;
* `0x1ec..0x1f3` are `ATOMS.{ADD,MIN,MAX,AND,OR,XOR,EXCH,CSWAP}` -- eight
  opcodes the namer does not name and the printer does;
* `SHL`/`SHR` disagree with the IR map and the disagreement is RECORDED, not
  resolved.

**Six modules instead of 866.**  `opcov/cover/cover.{vert,tesc,tese,geom,
frag,comp}.spvasm` print every opcode number the full sweep printed plus
twelve more; `tools/covercheck.py` prints the shortfall (0).  Rebuilding the
evidence is a minute, not half an hour.

**The spec-sourced claims got tested** (`opcov/vocab/RESULTS.md`, one row per
idea INCLUDING the negatives).  Sixteen texture target keywords replace the
guessed list; the condition codes the compiler actually prints are `TR`,
`NE`, `NE1`, `NONRESIDENT` and -- only in the footprint lowering -- the
`SINGLELOD` its own assembler rejects.  `QSWZ0..3/X/Y`, `FSIB`/`FSIE` and
`CALI` (a whole subroutine sub-language) were read through the GLSL front
end, which accepts shaders the SPIR-V one refuses.  `ATOM.*`, `LDCB`,
`ATTRRD`/`ATTRWR`, `MATCH.*`, `PK4B`, `LRP`, `RFL`, `NRM` and `SSG` were
tried and do NOT come out -- the compiler open-codes them.  **Named only**
fell from 109 rows to 86.

**Two more crashers and a compiler bug.**  A storage-buffer atomic in the
VERTEX stage segfaults where a plain store from the same stage is refused
cleanly; `OpAtomicLoad` through a `Block`+`Uniform` pointer segfaults where
`spirv-val` accepts it.  And the footprint family emits nine well-formed
instructions that the compiler's OWN ASSEMBLER refuses (`unknown opcode
modifier`) -- an emitter that outruns its assembler, like the `AND.F` case.

## The last cause: the merge pass-through register (notes/129)

The final prefix-only shaders did not refuse for want of a rule.  The
scheduler could not PLACE their bodies: three edges that are each right on
their own closed a cycle round the merge pair.

    (130,131) RAW     131 reads the merge
    (131,134) WAR     131 reads #53 at entry, 134 writes #53.x
    (134,130) kind-2  134 reads #194.w at entry, 130 writes #194.xyw

FOUR ATTEMPTS AT THE VREG IDENTITY WERE ALL REFUTED, at 436, 605, 432 and
639 of 679 probes; every one is removed from the tree and written up in
notes/129 §3 and §4, because a refuted attempt rules something out.  The
lesson is in HANDOVER §5: **when four attempts in a row regress the probes,
the model is right and the TRIGGER is wrong.**

The fix is two guards in `_name_read_after_pair`, the function that already
decides whether to split the merge's vreg and was declining to:

* A PASSED-THROUGH LANE IS NOT A WRITTEN ONE.  The pass-through half of a
  component store keeps the entry value, so a line in `self.passthru` must
  not count as a store of that component when the trigger asks whether the
  name was written.
* The anti-dependence test has to follow the access chain: a store through a
  component chain writes the CHAIN'S LOCAL, and the test was looking at the
  chain's result instead, so it never matched the reader that creates the
  obligation.

Neither changes a byte of any listing that was already right: 680/680
probes, 500/500 on the regression set, and prefix-only 0 over the whole
corpus.  notes/130 was written for the mechanism the code cited and no note
held: per-vertex reads and writes at a DYNAMIC index.

Two things about the lexer belong with this, because they cost more than the
rule did: `BAR ;` did not LEX, and five shaders were blocked behind a
refusal message that named the SCHEDULER.  A refusal message must name ONE
condition, and when it names a mechanism, check that it IS the mechanism --
three separate causes this campaign hid behind a message that named the
wrong thing (HANDOVER §5).

## The documentation pass that followed

Every figure in every file was re-derived rather than re-typed, and what did
not survive is recorded where it was wrong rather than deleted:

* `FLAGS.md` is generated (`tools/mkflags.py`).  Its site scan matched on
  the `ENV.get` call and so MISSED every switch whose call is split over two
  lines -- `G2S_NOMERGENEG` was absent from the registry entirely.  It now
  matches quoted flag names: 296 switches, 177 with no prose at their site.
* `FLAGS.md` gained a last table: the switches the NOTES still name that the
  code no longer has, and which `NO` form replaced each.  A note is a dated
  record and keeps the switch it was measured with, so `G2S_LOCALREG=1` in
  notes/53 is `G2S_NOLOCALREG=1` inverted today.
* Twenty-three `notes/N §M` citations pointed at sections that do not exist
  -- `notes/54 §9` and `§10` from five places, when notes/54 ends at §8 and
  the reading is notes/52 §7 -- and notes/114 had TWO sections numbered 79.
  All of them now resolve; the check is mechanical and worth re-running.
* HANDOVER's "the number that matters" ranked the refusal causes by listing
  lines.  That table is now EMPTY, because prefix-only is 0.  The METHOD
  survives -- weight by lines, not by files -- and it moves to DIFFERS.
* `notes/pending_probes/` held a `.glasm` whose source was not kept, so it
  could never be re-measured.  It is labelled and a README says what the
  folder is for; the rule is that the pair travels together.

THE CENSUS THAT MADE THIS POSSIBLE, and the lesson in it: the prefix-only
list in use before notes/114 §78 recorded 1,420 modules under 11 causes,
1,223 of them under ONE string.  Every one of those had already closed
itself as a side effect of other work, and the string turned out to name
three different conditions in two functions.  Work was being aimed at the
8-shader tail of a list whose head did not exist.  A census is evidence with
a date; re-take it before choosing what to do next.

Seventeen probes now isolate them, one per rule back to the anti-dependence
component (`0114_wr_a.frag`, `0114_hp_a.frag`, `0114_lz_a.frag`, `0114_mc_a.frag`, `0114_sm_a.frag`,
`0114_pc_k.vert`, `0114_sf_a.frag`, `0114_cs_b.frag`, `0114_sl_a.frag`, `0114_sv_e.frag`,
`0114_sv_d.frag`, `0114_ct_a.frag`, `0114_cg_a.frag`, `0114_mn_a.frag`, `0114_pv_a.tese`, `0114_cu_d.frag`, `0114_im_sel.frag`).  §25 has no probe and the note says why: with the rule on and
off every shader prints the same, so there is nothing for a probe to
discriminate on -- the evidence is the compiler's block list.
`probes/0114_cc_a.frag` reduces the largest of the three families still
open to 40 lines (it was in `notes/pending_probes/` when this was written
and is in the gate now; what is left there is an OLDER spelling's listing,
`notes/pending_probes/README.md`).  Every probe was checked to FAIL with its rule turned off; the
ones that did not were rewritten until they did.

`tools/degcmp.py` (new) is what read §26, and it is the instrument the
allocator work needs.  The compiler's interference graph does not have to be
dumped at all: notes/52's edge rule means the `liveset` trace CONTAINS it,
and that trace is cheap once `livecheck.py` stops asking for the `vregs`
gate (nine seconds on a 5,300-line shader instead of not finishing).  On the
shader that had been the example of the colouring problem it said, in one
line, what no listing could: every one of our 2,380 records had a degree
exactly ONE higher than the compiler's, and the extra neighbour was always
the same record -- a CUBE sample's coordinate, degree 2206 for us against 31
for the compiler.  `tools/ifgdump.py` (new) compacts the `ifg` trace from
gigabytes to megabytes, and `G2S_VREGAT` in the hook restricts the record
dump to one patched site; neither turned out to be needed for §26, and both
are kept because the sites they expose are the ones a coloured graph would
have to come from.

Three tools read these rather than fitting them.  `tools/recnum.py` pairs
the two sides' records by (first def, last use, mask) rather than by number,
so a numbering difference is visible as itself -- it is what read the load
numbering and the merge-chain key (which agrees with the compiler on all 40
sampled shaders, where the old key missed four).  `tools/waredge.py` scores
candidate anti-dependence orders against every reader in a trace, which
rejected the two keys that fitted one listing and cost probes.
`tools/gsum.py` supplied the `node[36]` stamps behind the handle pair.

THE LAST 630 LISTINGS, generated last, had never been compared: 310 of them
differed, in register numbering and in where a flush or self-move sits.  The
last ten causes closed 285 of those, so it is 474 exact and 25 DIFFERS
now -- 92.0% of the tail's lines against 39.7% -- and §26 alone (a CUBE
sample reads THREE components of its coordinate) accounts for 253 of them -- the
allocator.  The ones written up in notes/114 with their evidence include one
whose obvious fix makes the right record but prints it in the wrong place,
and one that fixes a tail shader's record numbering exactly and breaks the
shader the numbering rule was READ on -- both left open rather than
guessed.

`tools/p2check.py` (new) says where the rest of that work is: it runs our
pass 2 on the COMPILER's own blocks -- its stamps, its `entry[68]`, its
edges -- against the order it printed them in.  Every block of every shader
tried so far comes out identical (1,082 of 1,082 in one of the differing
ones), so the selector is right and what is left is LOWERING and ALLOCATION:
a record the compiler makes and we do not, or a register we colour
differently, which then adds an anti-dependence pass 2 obeys.

And `tools/regmap.py` (new) splits the tail in two without running the
oracle at all: it walks a listing pair, pairs the register tokens of every
line whose shape matches, and reports the renaming and the first line where
it breaks.  Over 40 of the differing shaders, 20 are a clean renaming until
one line -- the COLOURING's, everything before agreeing register for
register -- and 20 diverge in the program itself, which is the LOWERING's.
The colouring half is two shapes (`DIV.F32 R?.xy` and `MOV.F R?.x,
fragment.position`), and reading it needs the compiler's live set at that
position against ours.

Probes 656/656, corpus sample 120/120, slice exact 962 / DIFFERS 0.  The
full-corpus regression sweep over all 14,630 modules is clean outside the
tail: fourteen chunks of a thousand, DIFFERS 0 in every one.

**Package: 75 files.**  `tools/` (but `tools/probecheck.py`) and `notes/`
ship as `tools.7z` and `notes.7z` (`mkpackage.sh`).  The converter's tables
moved from `notes/` to `py/data/`, and `--form control`'s `control_form` from
`tools/checklisting.py` into `py/lex.py`, without its regex, so the converter
needs nothing outside `py/`.  The docstrings quoting the old patterns are
raw strings (Python 3.12+ warned).

**notes/113: the full corpus's `compute_volumefog_scatter-1` is exact.**
Two causes:
* A program that ends with a loop (`ENDREP; RET`) lost the loop's back
  edge, because the trailing terminators sit inside the last block
  (`ifg._trailing_structure` now reads them).
* A lane of a splat of a scalar local's load was gathered through a scratch
  copy; it now goes straight in.

With both fixes our graph equals the compiler's (`G2S_ONLY=simp`) record for
record.  On 29 full-corpus shaders that end in a loop or an if, the numbers
go from exact 5 / DIFFERS 1 to exact 6 / DIFFERS 0.  Probes 627/627, corpus
120/120 and the slice (exact 962, DIFFERS 0) are unchanged.

**notes/112: no regular expressions in the compiler.**  Every pattern in
`spirv2glasm.py` and `py/` was replaced by a hand-written scanner (`py/lex.py`
plus small named matchers per module), ready for a C/C++ port.  The output
is byte-identical on all 627 probes, the 120-shader corpus sample and the
1400-shader slice (`tools/samecheck.py`), and `tools/lexfuzz.py` checks each
scanner against the pattern it replaced.

**notes/111: corpus 120 of 120, probes 627 of 627, DIFFERS 0; no pending
probes.**  Storage images end to end: `STOREIM` (the store is a value, two
MOVs read it), `LOADIM` in every measured format including the packed ones
(BFE, UP4UB, UP2H/UP2US, rgb10a2, r11f_g11f_b10f, rgba8_snorm), the image
memory as a register of its own in both passes, pending image handles,
texel-buffer fetches; compute built-ins (`invocation.*`, kind 0x68); STB
chains and gathers; splats stored into locals; indices through a bitcast.
The 1,400-shader slice: exact 962, prefix-only 438, DIFFERS 0, failed 0,
1,093,657 of 1,848,903 lines (59.2%).  (The full corpus's
`compute_volumefog_scatter-1`, open here, is exact since notes/113.)

**notes/110: corpus 120 of 120, probes 578 of 578, DIFFERS 0; the old
pending probes all exact.**  Storage images get their own handle window (256 + 8 x binding,
`#var name.__handle[_nosize]`); a swizzled read of a lane a forwarded merge
wrote reads the merge; one reader's write-after-read edges are made by
component; a scalarised sin/cos/exp2..'s construct is the value's band temp.
The 1,400-shader slice: exact 961, prefix-only 439, DIFFERS 0 (was 960 / 3).

**notes/109: corpus 120 of 120 exact (100.0% of lines), probes 563 of 563,
DIFFERS 0.**  Position splats, merge lanes through a swizzle, outputs in the
live array, second colour stores, cube LOD, bool locals, flushed names, the
scalarised 2..3-lane ops by default, a name read after a merge pair, and
compute (GROUP_SIZE, STB).  The 1,400-shader slice is being re-run.


**Latest (notes/105, notes/106): the condition registers are an allocator
class; the corpus and slice DIFFERS that new lowering exposed.**  Each `.CC`
set is a vreg coloured by class 1 (CC0/CC1); an RSQ goes first in a MUL;
every write of a name walks its lanes' implicit reader lists; a 2D TXL reads
x, y, w; a sampled image's OR is a name; a lane-x component store's halves
share one seq.  Probes 458 of 458 exact, corpus exact 71 (was 56), 37348
lines, DIFFERS 0.  The 1,400-shader slice: exact 342 (was 228), 305065
lines, DIFFERS 1 (`map_3587d848`, the lowering temps' record order,
notes/106 §9 -- open).

**Before that (notes/101): which read is the stored value itself, and partial
copies.**  Driven by a 1,400-shader corpus slice: every fix is a probe
isolating it (sel_k..sel_r, cr_a..cr_i, fa_a..fa_f, cs_a -- 25 probes).
Probes 373 of 377 exact, corpus 56, DIFFERS 0.  The slice's two DIFFERS
introduced by notes/95's selects are gone; 25 older ones remain (notes/101
§9).

**Before that (notes/100): vector selects on a bool splat; a retracted
rule.**  sel_e..sel_i.  A temp-store rule narrowed until probes passed was
fitting and is removed; `0100_sel_e` stays refused.

**Before that (notes/99): shadow samplers.**  sa_a..sa_d, ta_a.

**Before that (notes/98): a dot with a constant operand.**  dt_a..dt_d exact;
probes 340 of 342, corpus 56, DIFFERS 0.

**Before that (notes/97): constant stores opening a block, name reads after
a loop, swizzled whole vec4 local stores.**  lc_a..lc_d.

**Before that (notes/96): derivatives of vectors.**  dd_a..dd_e.

**Before that (notes/95): vector selects (predicated MOVs) and selects on a
stored bool's component.**  sel_a..sel_d.

**Before that (notes/94): a texture lod that is a value.**  tl_a..tl_d.

**Latest (notes/93): the GLASM numbering as a table.**  `py/glslc/glasm.py`
(generated by `tools/mkglasmdefs.py`) is to GLSLC's opcodes and type codes
what Khronos's `spirv.py` is to SPIR-V's, read through `py/glasmnames.py`;
no GLASM opcode is a literal in the code any more.  `tools/opname_dec.py`
now simulates the namer chain and fixed thirteen garbled names and six
misread texture opcodes (none used by the converter).  Listings unchanged.

**Before that (notes/92): the code restructured, no rule change.**  `glasm.py`
is now a facade over `py/glasmlib/` (one module per concern) and
`py/glasmlib/lower/` (one mixin per instruction family); the long functions
of the scheduler, allocator and binding namespace are split; every SPIR-V
number is written by its Khronos name (`py/spvnames.py` over the unmodified
Khronos files in `py/khronos/`).  One latent bug found by the names: the kill
arm's 4417 is `OpTypeUntypedPointerKHR`, now `OpDemoteToHelperInvocation`.
Listings unchanged: probes 319 of 321 exact, corpus 56 (30865 lines),
DIFFERS 0.

**Before that (notes/91): pair seqs, pending vertex outputs, clamp.**
basic_039bd1ee.vert exact.  Probes 319 of 321 exact, corpus 56 (30865
lines), DIFFERS 0.

**Before that (notes/90): vector divides, loop back edges, orders.**
Probes 294 of 296, corpus 55.

**Before that (notes/89): structured-buffer addresses and identity folds.**
Probes 279 of 281 exact, corpus 55, DIFFERS 0.

**Before that (notes/88): gl_FrontFacing and hex immediates.**  `0088_ff_a` exact;
probes 267 of 269, corpus 55, DIFFERS 0.

**Before that (notes/87): vector bitcasts, shifts, the dither prologue.**
Probes 266 of 268 exact, corpus 55 exact (25.0% of lines), DIFFERS 0 in
both.  The allocation divergence the texture caused was three rules
(LONG handles in the single count, 2D TEX coordinate lanes, stored-component
reads).

**Before that (notes/86): float-to-integer conversions.**  `TRUNC.S`/`TRUNC.U`,
the MOV legaliser's reading.  pf_a..pf_c exact; probes 243 of 246, DIFFERS 0.

**Before that (notes/85): branches on a bool-vector component.**  HLSLcc's
`(u_xlatb6.x) ? .. : ..` branches now lower: `MOV.U.CC HC.x, R3;`.  A
component store's MOV takes the local's type.  An output store of a local
merged in the same block reads the merge, which becomes a temp copied into
the local, as the compiler's node dump shows.  Probes pb_a..pb_d are exact.
check.sh: probes 240 of 243, DIFFERS 0; corpus DIFFERS 0.

**Before that (notes/84): constant arrays in local memory.**  `0071_lm_icb` and
`0071_lm_icbf` are exact.  Each element gets a MOV into a temp and a store into
`lmem0[i]`.  The indexed load is a carrier plus `MOV.U R4, lmem0[R0.x].xyzw`.
The scheduler treats `lmem<k>` as one name.  A colour store's self-move is
renamed through `_flush`.  Probes: 236 of 239 exact, DIFFERS 0.  Corpus:
DIFFERS 0.

**Before that (notes/83): separate samplers and multiply by -1.**  Opcode 86
(`OpSampledImage`) lowers: an `LDC.U64` for each of the two handles, one
`OR.S` joining them, and `LONG TEMP` for the D registers.  Also: TEX results
are band values, texture coordinates print their swizzle, a swizzled local
store forwards its register, and `x * -1` becomes a negate MOV.  `check.sh`:
corpus 29569 lines, 53 files exact, DIFFERS 0; probes 234 of 239 exact,
DIFFERS 0.

**Before that (notes/82): the "computation after a store" refusal is LIFTED.**
`check.sh` now gives corpus 29521 of 120120 lines (24.6%, was 11.4%), 52
files exact (was 2), DIFFERS 0; probes 231 of 236 exact, DIFFERS 0.  The
last steps:

* one scratch per position element;
* dot operand swizzles;
* construct components into output lanes;
* no flush or self-move for local reads;
* re-loads after a component store's block split.

**Before that (notes/81):** three fixes:

* dead loads are dropped;
* a store that opens a block reads locals by name;
* a construct's components are forwarded into component stores.

`G2S_LIFTSTORE=1` corpus: 29212 lines (24.3%), 48 exact, 4 DIFFERS.
`check.sh`: probes 207 exact, DIFFERS 0; corpus DIFFERS 0.

**Before that (notes/80):** the `mc_*` cuts of `map_01908c43` are exact.  The
changes:

* a no-merge component store flushes its temp;
* a construct's component 0 is forwarded;
* shuffles read stored components;
* lane-wise sources read the write's lanes;
* an unplaceable body is refused.

`G2S_LIFTSTORE=1` corpus: 27454 lines (22.9%), 43 exact, 9 DIFFERS.
`check.sh`: probes 206 exact, DIFFERS 0; corpus DIFFERS 0.

**Before that (notes/79):** `map_110833a5` is exact under `G2S_LIFTSTORE=1`.
The changes:

* outputs stay pending across blocks;
* component stores to pending locals of any value open a block;
* a name read's store carries the read's seq;
* components of block vectors are gathered.

Under `G2S_LIFTSTORE=1` the corpus reaches 26392 lines (22.0%), 40 files are
exact and 12 DIFFER.  `check.sh`: probes 196 exact, DIFFERS 0; corpus
DIFFERS 0.

**Before that (notes/78):** fixes:

* DP3/DP2 read masks;
* local names and select results are not temps;
* all vertex outputs are one register to the edge builder;
* the vec3 swizzle fill;
* output stores flush through `_flush`.

`0077_mb_n18`..`0078_mb_n28` are exact.  New tool: `tools/ifgjoin.py`.  `check.sh`:
probes 192 exact, DIFFERS 0; corpus DIFFERS 0.  Under `G2S_LIFTSTORE`,
probes are 209 exact.

**Before that (notes/77):** `map_110833a5` bisected into `mb_*` probes.  Fixes:

* no empty pass-through;
* a whole `gl_Position` store after component stores reads the local by
  name;
* the implicit-read release order is last-first.

Still open: the temps' record order and allocation in `0077_mb_n18`
(`tools/vrjoin.py`, `G2S_WALKS`, `G2S_PICKTRACE` are the tools).
`check.sh`: probes 192 exact, DIFFERS 0; corpus DIFFERS 0.

**Before that (notes/76):** loads are re-lowered in every block that reads them,
and each LDC's mask and width are what its block reads.  Component chains
into uniform vector members now lower.  `check.sh`: probes 190 exact,
DIFFERS 0; corpus DIFFERS 0.

With `G2S_LIFTSTORE=1` the corpus reaches 18862 lines and exposes 29
DIFFERS, mostly the register count.

**Before that (notes/75):** repeated loads of one uniform location in a block
are one node, because the compiler interns expressions.  Related fixes:

* a construct of such loads is a real temp, with its own flush;
* an extract of a vector gathers through the scratch `.x`;
* a scalar register read by a wider op prints `.x`.

With `G2S_LIFTSTORE=1` the corpus has 8 exact files, 1 DIFFERS and 16645
lines.  `check.sh`: probes 188 exact, DIFFERS 0; corpus DIFFERS 0.

**Before that (notes/74):** `gl_Position` component stores of computed values
follow the output-chain rules.

* A second store opens a block at the store.
* `.x` from a scalar temp is followed by the temp's flush, which writes a
  renamed lowering vreg.
* `.y..w` from a scalar register go straight to the lane.

With `G2S_LIFTSTORE=1`, `chr_eye_13b81913.vert` is now EXACT (corpus 16396
lines, 6 DIFFERS left).  `check.sh`: probes 184 exact, DIFFERS 0; corpus
DIFFERS 0.

**Before that (notes/73):** bisecting `chr_eye` with `probecut`.  Each rule was
isolated in a small probe (`0073_pt_a`..`0073_pt_f`) and confirmed against the
compiler's own blocks and edges with the new `tools/gsum.py`.

* **A read covering a component store's written component** takes the
  merge, so the pass-through copy is its producer (kind 0).  A read of the
  other components still reads the name at block entry (kind 2).
* **`gl_Position = b` after `b.x = ...` in the same block.**  `.x` takes
  the stored value.
* **Stored locals are materialised even if never loaded.**
* **Pending component stores.**  A component store to a name pending from an
  earlier block opens a block at the store, as a whole store does.
* **Check run.** Probes: 180 exact, 3 prefix-only, DIFFERS 0 (9691 of 9749
  lines).  Corpus: 13667 lines, DIFFERS 0.

**Before that (notes/72):**

* **Bitcasts.** A bitcast is now lowered as the compiler's MOV:
  * integer <-> float takes the result's suffix;
  * int <-> uint takes the source's suffix.

  It was wrongly forwarded before, and no probe caught it.
* **Splat stores.** A store of `vec4(scalar)` reads the name and has no
  self-move.
* **Flush at the block close.** A temp still pending when a whole
  `gl_Position` store closes `.x`'s block is flushed inside that block
  (`0072_bc_sbo`).
* **Loaded locals.** Every stored local that is loaded is materialised,
  whole or by component (the "stays forwarded" rule was wrong).
* **`0072_ce_head.vert` exact.** It is the head of a corpus shader.  Two fixes
  got it there: flush-renamed temps take their original record position,
  and the dynamic-index carrier has its own vreg.
* **Construct components in their own block.** A one-component read of a
  construct in the same block reads that component's source
  (`0072_ce_head2`/`0072_ce_head3`).
* **Pending stores across any block.** A local store pending from any
  earlier block (not only an IF block) opens a block at the store
  (`0072_ce_head4`).
* **`0072_ce_n29.vert` exact** (the first 29 statements of `chr_eye`, cut with
  the new `tools/probecut.py`).  It needed three things:
  * an unreferenced uniform block prints `BUFFER[-1]` and no CBUFFER line;
  * a whole load forwarded through a local keeps its head source;
  * a fix to the `load_of` position rule.
* **`G2S_LIFTSTORE=1`.** A measurement-only switch that lifts just the
  "computation after a store" refusal.  With it the corpus reproduces 15644
  lines (13.0%) and 7 files DIFFER, which are the next bisection targets.
* **Check run.** Probes: 166 exact, 3 prefix-only, DIFFERS 0.  Corpus:
  DIFFERS 0.

**Before that (notes/71):**

* **Loops with `break` and `continue` are read.**
* **`while (true)`.** The compiler folds an `if` on a constant condition
  before the IR (`f_7100ef7d50`, 0xef8f68), so `while (true)` has no IF
  around its body.
* **Stored bools.** A branch on a stored bool takes only its `.CC` MOV.
* **`continue`.** It is the lowering `f_7100f74bc0`'s flag: `flag = 0` at the
  body's top, `flag = 1.0` in the arm, and `if (flag == 0)` around the rest.
  The flag lives in `H0` of the SHORT class, printed first
  (`SHORT TEMP H0;`).
* **Check run.** `tools/check.sh` gives probes 149/149 exact (4925 lines),
  DIFFERS 0, and the corpus unchanged at 13667 lines, DIFFERS 0.  The branch
  refusal (formerly 81202 lines) is gone from the census.
* **Top refusal now.** The line-weighted census's top refusal is "a local
  store with no operand form" (63430 lines, 30 files).

Earlier, `tools/check.sh`, one run:

```
corpus  (120 saved listings)   exact 1   prefix-only 117  DIFFERS 1  failed 1
                               13612 of 120120 lines (11.3%)
        DIFFERS 1 is lens_flare_ghost_tex.frag, which now lowers to its end
        and first differs at line 38 (a register) -- notes/69; being worked.
probes  (137 saved listings)   exact 137 prefix-only 0    DIFFERS 0  failed 0
                               4478 of 4478 lines (100.0%)
```

**Latest: the register allocator is transcribed, checked and on**
(notes/54, notes/55).  Each stage is checked against the compiler fed its own
inputs -- the front end's block dataflow (`tools/dfcheck.py`, every probe and
corpus shader), the per-block seed (`tools/seedcheck.py`, 78427 seeds), the
interference graph (`tools/graphcheck.py`), the driver and its attempts
(`tools/drivercheck.py`) and simplify/select (`tools/simpcheck.py`).  The
band the old model measured turned out to be a rule: at `--opt-level none`
every store is live out of its block at the mask it stores.  With the
allocator on, the local assembled from parts is emitted by default and
`0052_lo_parts`, `0053_lo_half` and `0053_lo_over` are exact; their order needed the
compiler's implicit reads (0x49b88) in the scheduler's first pass.  And the
block partition is read off the compiler's per-name `block[80]` lists: a
block holds one store per name, which puts a second output's store into the
last `gl_Position` element's block (`0031_if_out`, `0031_p04_out` exact).  Then
`0044_fr_mrt.frag` (notes/56): the colour outputs share register code 207, so the
edge builder orders their stores like writes to one register.  Then
`0052_co_add1.vert` (notes/57): the converter now follows the compiler's
sequence -- pass 1, the allocator over pass 1's lists, pass 2 with its edges
on the allocated registers, swept over pass 1's list and pushed at the head.
Then `0051_fr_ddx.frag` (notes/58): scalar `dFdx`/`dFdy`/`fwidth`, with fwidth as
the front end's `abs(DDX) + abs(DDY)` and every result a stored name.
Then `0052_op_cross.vert` (notes/59): two swizzled MULs and an ADD.  Then
`0033_un_sqrt.vert` (notes/60): inversesqrt's family shape, a reciprocal per
component, and a multiply by one.  Then `0033_fr_texlod` and `0033_fr_texfetch`
(notes/61): the coordinate built as a construct with the lod in `.w`, and
the image node releasing its handle before its coordinate.  Then all six
`ts_eval_*.tese` (notes/62): the instruction printer's kind-0x35 arm decoded
into `opname.PER_VERTEX_IN_35` names `vertex[0].position` and
`vertex.tesscoord`.  Then `0007_ts_ctrl.tesc` (notes/63): InvocationId, a
dynamically indexed per-vertex read, the arrayed output store, the patch
levels and their `#var` rows.  Then structured LOOPS (notes/64): `0046_cf_while`,
`0029_cf_loop`, `0064_p09_loop` -- REP/IF/ELSE-BRK/ENDREP, a control-flow-aware
scheduler, and a local counted as a name in the block rule.
The loop head's constant test (`SEQ.U.CC HC.x ...; BRK`) and the branch
condition's normalisation (`TRUNC.U` / `MOV.S x, -x`) are now READ, not
measured (notes/64 §2-§6): the missing loop condition becomes `true` in the
cgc->IR lowering, `if (!cond) break` is lowered through the bool
representation passes, and `HC` against `RC` is the `.CC` fold's
not-a-definition flag.  Unsigned-compare branches are no longer refused.
Then the nine `gs_*.geom` probes (notes/65): the geometry stage writes every
output through a SHADOW the front end's pass `f_7100f7b230` creates
(`S.c = v.c; gl_Position.c = S.c` per component, EmitVertex taking `S`), the
register-indexed `gl_in[i]` read, EMIT/ENDPRIM, and three reads that changed
`py/sched.py`: the walker's block test (a name's store stays pending until
the name is touched in a later block), the merge copy as the carrier of the
implicit read, and the anti-dependences made in a backward sweep after the
uses.  `0046_g01.geom` (the same body) needed the geometry clip/cull semantic
column, `$vin.CLP0[0][49]`, from the same kind switch (notes/65 §6).
Then `0046_cf_switch.vert` (notes/66): cgc's switch lowering (`f_7100fb0a40`)
turns the switch into an IF chain -- `selector == literal` per case in body
order, the default's body held back as the innermost else -- whose compare
is used directly, so the normalising `MOV.S` takes the folded `.CC`
(`MOV.S.CC HC.x, -R0;`).  The arms showed that every SPIR-V value is a
named temp: a local store that opens a block (its store pending from the
header) reads the temp's name, with the temp's own store left behind and no
self-move.
Then ten `sc_*.frag` probes of the corpus's one-component constructs
(notes/67): the one-component divide folds to `DIV` (the MUL(RCP) peephole
`f_7100068fd0`), a one-component scalar op is one instruction
(`f_7100060110`), a comparison is normalised where it is made, a bool
stores as `MOV.U`, a load takes a value stored in its block, OpSelect is an
IF/ELSE into a temp, a temp's own store is flushed at block end into its
own name, liveness follows the IF/ELSE edges, and a negate is a carrier
MOV.  `lens_flare_ghost_tex.frag` now stops at its vec2 local's component
store (notes/67 §9).
Then `0050_cf_call.vert` and three more call probes (notes/68): subroutines
through CAL, the caller's copy into each `in` parameter, the callee's entry
copy of it, the return name, the driver's extra RET, the label as the
subroutine's first block number, liveness along the call edges, and pass
1's entry walk in the block's name-list order.  Every probe is exact.

`DIFFERS 0` is still 0.  **The scheduler is now on by default** (notes/53):
`py/sched.py` runs notes/51's two passes on the converter's own lines, BEFORE
the allocator, and the order of every body is computed rather than written out
by hand.  Three things fell out of it, in this order:

* `OpCompositeConstruct` from different values -- the corpus's leading
  refusal.  Five new `co_*.vert` probes read it; four are exact.
* the TRANSCENDENTAL FAMILY.  `cos`, `sin`, `exp2`, `log2`, `rsq` and `pow`
  are the same construct: four per-component instructions into four vregs,
  then the construct's four writes with component 0 forwarded to the store.
  All six exact, and the arm reads no rule the construct had not already
  established.
* the local assembled from parts, whose SHAPE is read and emitted
  (`G2S_LOCALREG=1`, instruction counts exact on three new `lo_*.vert`
  probes) and which stays off on one unread question -- notes/53 §8.

Two more followed once the order stopped being the obstacle: `normalize`
(`v * inversesqrt(dot(v, v))`) and `mix` (`x + (y - x) * a`).  Their opcodes
and masks are measured -- `0x8a`+`0x7c`+`0x90`, `0x83` x2 + `0x90` -- and what
feeds what is the function's own definition, not a choice.  `mix` is ONE band
temp written three times, which is `tools/bandsize.py`'s 1 against
`normalize`'s 3.  Both exact.

Then `step` and `sign`, which were waiting on one thing: opcode `0x6d` has no
entry in any namer -- the rounding family takes its mnemonic from a 4-bit MODE
in `node[12]` (notes/23) -- and `tools/extshape.py` recorded that mode only
for shapes that are ONE instruction.  It records it for every `0x6c`/`0x6d`
node now, so `TRUNC` spells itself, and both fell out:
`step(edge, x) = x >= edge` truncated, one band temp written three times;
`sign(v) = (v > 0) - (v < 0)`, two band temps with the subtract reusing the
negative side's.

And `mod`, which is the divide plus three lines: `x - y * floor(x / y)`.
`FLR` is opcode `0x6e`, which the namer spells outright -- it is not the
`0x6c`/`0x6d` rounding class and needs no mode -- and the subtract is an `ADD`
with the operand negated (notes/42).

And the INTEGER divide, which is the scalarised-shift shape: four nodes of
one opcode, one vreg, one `node[36]` between them.  The opcode is the thing
that made it look harder than it is -- the chain gives `OpSDiv` 0x85, which no
namer spells, while the LOWERING uses 0x87, which spells `DIV.S`.  A float
divide and an integer divide share an IR opcode and not a lowering.

probes went 69 -> 87 exact.  The one
`failed` is the compute shader, out of scope by the brief.  The corpus figures
are over the 120 listings this package travels with, not the 517 of the last
report -- `check.sh`'s banners derive their counts now rather than stating a
number from a tree that is not here.

What this session did is the thing HANDOVER §4 named as the next work and
could not start: **the ORDER is now read end to end**, one open question at a
time, and the part of it that was read is measured against the compiler rather
than against listings.

## The machine was rebuilt from the archives

Nothing was measurable at the start: the port tree was pristine, the oracle
did not exist and the toolchain was not installed.  Restored, in this order,
and all of it re-runnable:

* GCC 15.0.1 and binutils 2.44 unpacked from Ubuntu plucky debs into
  `/opt/tc/root`, driven through `/opt/tc/cc15` and `/opt/tc/ar15` (HANDOVER
  §2.1).  `#embed` verified before anything was built on it;
* the glasm2sass capture hook re-installed in `src/fn/f_71010d76b0.c` -- one
  call to `g2s_after_glasm_print` above `L_71010d7f8c`.  That package is not
  in this archive set, so the hook was re-derived from the call site
  `apply_patch.sh` documents rather than assumed;
* `tools/apply_patch.sh`, plus the two Makefile variables its patch could not
  place (this tree has no `glasm2sass` target for the hunk to anchor on);
* **`tools/apply_hooks.py` (new)** -- installs EVERY hook in `patches/HOOKS.tsv`
  rather than the subset `trace_patch.py` knows.  It was needed, not
  convenient: `trace_patch.py`'s idempotence test is `"g2s_trace" in src`, so
  the SECOND hook on a file that already carries one is silently skipped, and
  19 addresses in the table carry two.  It also handles the two hooks the
  table places at a LABEL, which HANDOVER §3.1 says to do by hand.  196 of 199
  hooks install clean; the one real miss is recorded below;
* the library: 53,748 objects, about 50 minutes on two cores, then the oracle.
  `build/spirv2glasm --opt-level none probes/0011_op_mul.vert.spv` is byte-identical
  to `listings/0011_op_mul.vert.glasm`, which is the check that the rebuilt machine
  is the same machine;
* the corpus: the 120 shaders that have a saved listing, through
  `corpus/mkcorpus.py` with glslang 15.1.0 -- 120 of 120, 0 rejected.

## What opens a list: answered, and it was the wrong question

notes/49 ended on "what OPENS a list, in `f_710004a2e0`'s walk over
`block[80]`".  Read instruction by instruction, `f_710004a2e0` has no loop
over lists at all: **a list IS a block.**  The function is called once per
block by `f_710003bb10`'s walk over `program[184]`, and every node it
schedules is linked into that one block's `block[32]`.

Measured, with a new `g2s_trace_block` hook that dumps the emitter's input at
its entry -- `0031_if_out.vert`:

```
g2s_blk block=0x17cb29f8 list=0x17cb8ca0 own=0x17cb6750
g2s_blk block=0x17cb2ee0 list=0x17cb8cb0 own=0x17cb6808
g2s_blk block=0x17cb43f8 list=0x17cb8cc0 own=0x17cb68c0
g2s_blk block=0x17cb4878 list=0x17cb8cd0 own=0x17cb6978 term=0x17cb4e28
g2s_blk block=0x17cb4f98 list=0x17cb8ce0 own=0x17cb6a30
```

-- the four list addresses notes/49 saw are four blocks, and the `list` field
IS `block[32]`.  notes/51 has the whole loop transcribed: the seeding walk over
`block[80]`, the terminator taken first, the write-mask test that decides
whether a pick is linked at all, and the block's own node closing the list.

## Release passes through four opcodes

`f_7100049780` opens with the same opcode bitmap as the seeding loop, applied
to the OPERAND, and the arm it selects is a recursion: a node whose opcode is
1, 0x3a, 0x57 or 0x5a is never decremented, never pushed and never printed --
the release goes into its slots instead, following only those whose component
mask overlaps.

`0018_op_pow.vert` is where it shows.  Its four component `MOV`s sit behind a
three-node `0x57` merge chain, and the compiler's first ready list -- dumped
with `g2s_trace_pick` -- holds the four `MOV`s in the chain's own depth-first
order and not one node of the chain.  Modelling the store as releasing the
chain's head puts the chain on the list and gets the whole block wrong.

## The comparator, confirmed from the code and from outside

notes/49 read `f_710004b930` as degenerating -- `node[32]` is zero on every
node, so both arms end in TAKE and the scan keeps the list's TAIL.  Re-read
here instruction by instruction, that is right, and `g2s_trace_pick` confirms
it on every pick of `0018_op_pow.vert`: the entry kept is the tail each time.  The
list being pushed at the head, the tail is the oldest ready entry.

## `tools/schedcheck.py`, rewritten and run

The old one implemented notes/31's superseded comparator (a `max` over
`node[32]`, `node[36]` and a stamp) over one undivided list -- a check for a
rule that has since been read differently, which is worse than no check.  It
is replaced by the model above, with the DAG builder's decisions taken from
the compiler through `g2s_trace_block` so the ORDER is tested on its own.

```
113 probes:  89 OK   24 MISMATCH
```

and on `0018_op_pow.vert` the PICK order it produces is the compiler's own, node
for node, against `g2s_trace_pick` / `g2s_trace_picked`.

## The 24, and the reading that explains them

notes/49 said the second insertion wave "re-links the finished lists
back-first".  That is wrong, and `0018_op_pow.vert` is the counterexample: wave 2
is not wave 1 reversed, and the difference moves a line.

`f_710004b220` is a **second schedule**.  Per block it moves `block[32]` aside
with `f_7100030e08` (read in full: a two-word list move that empties the
source), builds a scheduler object, and then loops -- selecting an entry with
`x20->vt[56]` and back-pushing its node -- so its output IS the printed order,
with no reversal in it.  The first pass's order reaches it through
`entry[68]`, the scheduling time both passes write.

The selector is **`f_710004ba90`**, named with `indirect_patch.py`
(`site=0x710004b668`).  Unlike the first pass's comparator it does not
degenerate: it filters candidates through a readiness predicate, keeps the
SMALLEST `node[36]`, and breaks ties on the EARLIEST `entry[68]`.

That left one thing unexplained -- four `POW`s with `node[36]` 3, 4, 5, 6
print 3, 6, 5, 4, which smallest-first cannot produce -- and a second
instrument settled it rather than leaving it as a hypothesis.
`g2s_trace_sel` / `g2s_trace_selected` (new) dump the candidate list at every
call to the selector, and they show two things: the calls ALTERNATE, every
other one selecting nothing from an unchanged list, so this is a cycle-based
schedule; and the keys are right while the FILTER is what staggers the
candidates.  The predicate is `cg->vt[16]` = **`f_71000476f0`**, and it was read too:
thirty instructions that admit an entry when a CYCLE CLOCK (`cg[28]`) has
reached the depth the first pass wrote (`entry[52]`) and a resource record of
its own does not conflict with `cg[20]`.  Dumping the clock and the depth
beside the keys shows the alternation for what it is -- `cg[20]` is
0xffffffff on exactly the calls that select nothing, and the clock advances by
16 every second call.

And with the gate understood, one thing was still wrong, which is the finding
worth keeping: **`f_710004ba90`'s two tests are not a lexicographic key.**
Its scan takes a candidate when `node[36]` is smaller OR, failing that, when
`entry[68]` is smaller -- so a worse `node[36]` can win, and the winner
depends on the ready list's ORDER.  Walked as a scan it reproduces the
compiler's choice on every call measured; sorted on either key it reproduces
none of them.  That is the same shape notes/49 hit in the first pass, and it
is why a model of this has to scan rather than sort.

`tools/selcheck.py` (new) is the check, and it is the strongest number this
session produced:

```
113 probes   1791 selector calls   1791 OK   0 MISMATCH
```

-- the gate and the scan reproduce the compiler's choice on every call of
every probe, the 24 the first pass alone does not explain included.

So the order is now read end to end -- DAG, first pass, depth, clock, resource
mask, comparator, insertion -- and the only inputs still taken from the
compiler rather than derived are the DAG builder's and the clock's advance.

The 24 mismatches are exactly the shapes with several instructions in one
block: the scalarised GLSL.std.450 builtins (`pow`, `sqrt`, `rsq`, `sin`,
`cos`, `exp2`, `log2`, `cross`), the geometry shaders, and the loop, switch
and while probes.

## Corrections made in place

* **notes/49** -- a banner above "There are two insertion passes" pointing at
  notes/51: the second wave is a second SCHEDULE, not a re-link;
* **`tools/check.sh`** -- the banners said 517 and 90 saved listings where the
  tree holds 120 and 113.  They derive their counts now.

## One hook that does not install, stated rather than hidden

`patches/HOOKS.tsv` asks for `g2s_indirect` on `f_7100eeff94` and that
function has no indirect call in this tree, so `apply_hooks.py` reports
`no indirect call` and installs nothing.  Everything else in the table is in.

## What is next

1. **Dump the second pass's worklist** at each `f_710004ba90` call -- that is
   one hook, and it settles whether the readiness filter or a misread key
   explains the `POW` order.  With it, the order is reproducible end to end
   and `schedcheck` should go to 113.
2. Then the allocator (notes/48), against `g2s_dump_graph`, with
   `0048_un_clamp.vert` as the smallest case that a last-use scan gets wrong.
3. Only then the node IR in `py/glasm.py`, the scheduler and the allocator on
   it, and after those the loads of locals, local stores and control flow --
   which is where 115k of the corpus's 120k lines are (`tools/census.py`).

## The corpus, at full scale

The 120 saved listings this package travels with were a sample.  All 14,706
shaders now go through, and the whole listing is measured against the
compiler's own answer for every one of them.

**GLSL -> SPIR-V: 10,328 -> 14,630 of 14,706 (99.5%).**  `corpus/mkcorpus.py`
gained two edits, each driven by glslang's own diagnostic rather than by a
pattern guessed at in the source:

* **the bindless constructor.**  `sampler2D(uint64_t(Tex3))` becomes
  `sampler2D(Tex3, Smpl3)`, paired BY BINDING NUMBER -- which is the
  separate-texture-types model the shaders already declare, and the reason
  `-V` was the right choice to begin with.  That one cause was 121 of 122
  failures on a random 400.  What it costs is stated in the code: the 48
  shaders whose handle OR-ed in a sampler-state index lose that index, and a
  texture with no sampler at its own binding takes the first of the right
  kind.  Shadow samplers stay distinct from plain ones;
* **the implicit fp16 narrowing.**  A handful of shaders are fp16 throughout
  and assign an expression an untyped literal has dragged up to fp32.
  `GL_NV_gpu_shader5` allowed that implicitly and the EXT explicit-arithmetic
  family forbids it by design.  Measured: no extension combination glslang
  implements restores it and no `--target-env` changes it, and
  `float16_t(int)` compiles on its own -- so the integer conversion is not
  what fails.  The narrowing is made explicit on the assignment glslang names,
  which is what the implicit conversion did: compute in fp32, narrow once.

Plus `GL_EXT_shader_16bit_storage` (the arithmetic-types extension is ALU-only
and a `float16_t` in a buffer block needs it separately), and `: require` on
the injected lines only -- measured equivalent to `enable`, but the shaders'
OWN directives must stay `enable`, since several name extensions glslang does
not implement where `require` is a hard error.

The 76 that remain: 72 bindless shaders that declare no `uniform sampler` to
pair with, 3 `GL_NV_bindless_texture`, 1 std140 alignment.

**SPIR-V -> GLASM, all 14,590 non-compute shaders:**

```
exact 6   prefix-only 14584   DIFFERS 0   failed 0
1,884,385 of 20,474,191 listing lines reproduced (9.2%)
```

`DIFFERS 0` now holds across 14,590 real shaders and 20.5 million lines --
a 122x increase in the sample it has been held to.

The listings are SAVED (`corpus_listings_full/`, 538 MB), so every later
comparison costs no oracle runs.  `tools/mklistings.py` writes them and
`tools/mklistings_run.sh` drives it; `tools/parcheck.sh` is parameterised by
`$CHECKER` rather than copied, because the driver -- the slicing, the
one-stream-per-core scheduling, the `.done` files that make an interrupted
sweep resumable -- is the hard part.  Two things were verified before the
sweep was trusted: the generator's output is byte-identical to the package's
own saved listings (40 files, `cmp`, 0 differ), and the sweep is run SHUFFLED
with a fixed seed, because a partial sweep in directory order measures
whichever shader families sort first.  That was not hypothetical: the first
1,763 taken in directory order gave 7.0% where the random ones gave 9.7%.

**The line-weighted census, over the real corpus:**

```
13045274 lines  5310 files  a branch arm that does not join at the merge
 3699747 lines  4855 files  a load that is not a straight interface read
 2743911 lines  2686 files  a local store with no operand form
  339838 lines   733 files  opcode 86 in the body
  339642 lines   541 files  a clamp (the register, not the lowering)
```

The top three are 96.3% of the corpus's lines and the ranking is the one the
120-listing sample gave, so the priority HANDOVER sets is confirmed at scale.

## The order, joined

`tools/ordercheck.py` (new) runs the SECOND pass end to end over the first
pass's output and compares with the listing's order -- the check the order is
actually for.  Getting there needed three more readings (notes/51 §5-§6):

* **the cycle.**  `f_71000477a0` is `cg[28] = cg[24] << 4` with the masks
  cleared -- the clock is the cycle times sixteen, and `cg[8]` picks the
  direction, so pass 1 counts DOWN and pass 2 counts UP.  That is why pass 1's
  `entry[68]` stamps come out 0, 16, 32 ... in list order;
* **the resource is a RECORD**, not the node and not its opcode.  The gate
  returns the first record of the entry's chain whose bits do not overlap the
  cycle's mask, and the claim ORs THAT record's bits in.  Measured, every
  entry owns exactly one record with bits `0xffffffff`, so this profile issues
  one instruction per cycle;
* **the dependences.**  The second pass runs top-down, so the DAG's edges
  apply, with the transparent opcodes skipped through as in §3.

```
tools/ordercheck.py   113 probes   113 OK   0 MISMATCH
```

The worklist was then read too (notes/51 §6): `f_710004b220` pushes onto it in
two places and both push AT THE HEAD, so it is a stack and the scan sees the
most recently released candidate first.  `entry[64]` counts unscheduled
predecessors -- the top-down mirror of the first pass's `node[88]` -- and
`entry[52]` turns out not to be a static number the first pass hands over: it
is RAISED to at least `clock + 1` every time a predecessor issues, which is
exactly the 1, 17, 33, 49 measured against clocks 0, 16, 32, 48.  That took
the model from 82 to 92.

The fitted edge order is gone.  `f_710004ab80` (0x4acb8..0x4acf0) builds one
edge per operand and pushes it at the HEAD of the producer's `entry[56]`, and
`f_710004bd60` drives that over the operand slots last-to-first; replacing the
guess with the reading left the numbers unchanged, which is what says the
guess had been right rather than that it did not matter.

Reading it turned up the reason the remaining 21 fail, and it is not the
order: **`f_710004ab80`'s `kind == 2` arm SWAPS producer and consumer**
(0x4acd0), so the graph the second pass schedules is not the DAG but the DAG
plus anti-dependences, and `f_710004b220` asks for exactly that (`w3 = 2`,
0x4b5e8).  `g2s_trace_push` (new, on the decrement and both push sites) shows
`0033_un_sqrt.vert`'s first `0x47` node carrying `npred = 4` where the DAG gives it
one -- which is why the model releases it at clock 0 and the compiler at 128.

A wrong reading was corrected on the way, and it is the kind that is easy to
keep: `g2s_trace_stamps` sits on `f_710004b220`'s ENTRY, and the dependence
edges are built inside that function.  Its `succ=` and `npred=` fields are
therefore stale, not the second pass's input -- `npred=1` there against
`npred=4` a moment later is the same entry.  `p52` and `t68` are sound.  The
hook says so now; before it did, the stale lists looked complete enough to
reason from and sent the edge-order reading down a false trail twice.

A wrong reading was caught and corrected in place on the way: §5 first read
the claim's fourth argument as the NODE and concluded the opcode word was the
mask.  It is the gate's RETURN VALUE, set two instructions later.  The values
do not give it away -- an opcode and a record field are both small integers --
and it only shows when a model built on it issues two instructions in a cycle
the compiler separated.

The 31 that remain need one thing, and it is named: the ORDER the second
pass's worklist is in.  The comparator is a scan whose second test can
overturn its first, so its answer depends on that order, and `selcheck.py` --
given the worklist as the compiler had it -- gets all 1,791 selections right.

## The order, closed

Feeding the model the compiler's own dependence graph -- `g2s_trace_graph`
(new) at 0x4b598, after a block's edges are built -- took `ordercheck.py` from
92 to **113 of 113**.  Every probe's scheduled order is the compiler's, node
for node.

The graph is not the DAG, and that was the whole gap: `f_710004ab80`'s
`kind == 2` arm swaps producer and consumer, so it carries anti-dependences,
and `0033_un_sqrt.vert`'s first `0x47` node has four predecessors where the DAG
gives it one.  A model built on the DAG released it at clock 0; the compiler
releases it at 128.

14 probes print one or two instructions that no block's list holds at the
second pass's entry -- opcode 0x26 in the loops and geometry shaders, 0x95 in
the switch.  The scheduler never sees them, so they are reported as
`+N unscheduled` rather than counted as order mismatches, which would measure
something else.

Two parsing traps are recorded in notes/51 because both gave plausible wrong
answers rather than errors: the graph hook fires once per block and omits the
block being scheduled, so firings run together and silently double a block's
nodes, and block order taken from first appearance puts the FIRST block last.
Both went away by taking only a node -> successors map from that hook and the
block structure from `g2s_trace_stamps`.

What the order still takes from the compiler is the edges themselves, and that
is the next read: `f_710004bd60`'s caller loop and `f_710004ab80`'s component
arithmetic decide when an edge exists and of which kind.  It is a liveness
question -- the same one notes/48 leaves open for the allocator.

## The differing slice, closed (notes/104)

The 26 corpus shaders of the 1,400-shader slice that differed are all exact
(16598 of 16598 lines).  The last three were read with the traces, not
fitted: the D registers are the allocator's class 4 (`g2s_simp` shows the
driver run with `class=4`, K the handles' pressure); `0104_lf_c` reproduced a
lane's scratch MOV folding into a one-use reload; `0104_sw_a` had pass 1 and the
selector exact and one pass-2 edge too many (colour outputs chain per
component, `gsum`'s row-207 edges); `cl_a..g` measured which construct lanes
gather.  Probes 418 of 424, corpus 56, DIFFERS 0.

## Rules re-read from the code instead of the listings (notes/104 §4, §6, §7)

Two of the rules above were first taken from listings, and one of them was
justified with the wrong function (the `.CC` fold of notes/64 §5).  They
are now confirmed on the compiler's IR and DAG (`irtree`, `fold`, gdb):

* an insert reads a scalar block load's node (`0104_lf_c`, `0104_lf_d`, and `0104_lf_g`,
  whose earlier second reader the first version got wrong);
* a constructor operand that is a vector swizzle (IR op 0x1c) gets a MOV
  of its own -- `f_7100f13850` (notes/44) -- and a scalar goes straight in;
* a matrix part (`m[c].k`, `m[c]`; IR op 0x1d) is a fresh LDC and a MOV at
  every read, made by `f_7100f10a30`'s case at 0x7100f117c0; `0103_ld_mx` is no
  longer refused;
* a store's only source node writes the destination: a matrix MOV (lane x,
  whole local, whole output) and a block load (`0104_mx_h`, `0104_lx_b`).

`0103_ld_mx`, `mx_b..h`, `0104_lx_a`, `0104_lx_b`, `0104_lf_g` new; probes 429 of 434, corpus
56, DIFFERS 0.  Still to read at the code: the pass that makes the
constructor's MOVs after the construct's writes (their seqs 6..8 follow
the writes' 4).

## The record numbering and the sweep's live array (notes/107)

The slice's last DIFFERS, `map_3587d848`, is exact.  Traced with gdb on
`f_7100036a70`'s two walks: a block's merge chains (temps written by two or
more partial-mask defs) are numbered first, the other temps in creation
order.  Read from `py/liveness.py`'s transcript of `f_7100043460`: a def's
edges walk the live ARRAY (seed ascending, uses appended at the end, a
killed record swap-removed), not the nibble set -- the sorted walk broke two
corpus files, the array walk breaks none.  Probes 458/458, corpus 71 exact,
slice 343 exact, DIFFERS 0 everywhere.
