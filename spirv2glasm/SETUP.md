# Restoring the working setup

This file says how to rebuild, from this package, the machine state the work
was done in: the oracle, the corpus, the slice, and the checks. HANDOVER.md
explains why each piece exists. This file is the checklist.

## 0. The package layout

```
<root>/
  .github/workflows/probes.yml   CI: the probe check (§6)
  spirv2glasm/                   the project; everything else is in here
  spirv2glasm/probes.7z          the probe sources and their expected listings
                                 (probes/, listings/ and listings_open/
                                 inside, §5)
  spirv2glasm/tools.7z           tools/, all but probecheck.py
  spirv2glasm/tools/probecheck.py  the probe check, a plain file: the workflow
                                 runs it
  spirv2glasm/notes.7z           notes/
  spirv2glasm/py/data/           the measured tables the converter reads
                                 (they used to be in notes/)
  spirv2glasm/mkpackage.sh       builds the three archives and the package
```

The package stays under 100 files by carrying the three big folders as
archives. Unpack them first, in `spirv2glasm/`:

```sh
7z x -y probes.7z && 7z x -y tools.7z && 7z x -y notes.7z
```

The converter and `tools/probecheck.py` need nothing from the three
archives but `probes.7z`: the converter reads only `py/` (its tables are in
`py/data/`).

`sh mkpackage.sh <out.7z>`, run from the working tree, rebuilds the archives
from `probes/`, `listings/`, `tools/` and `notes/` and writes the package.

NOTHING FROM THE CORPUS IS SHIPPED: no corpus shader, no listing made from
one, no list of their names. The corpus side of the setup (§3, §4) is
rebuilt from the corpus itself, which is kept outside the package; the
listings the package carries are the probes' only.

The project folder is self-contained. Every tool finds the project from its
own location (`tools/..`), so the folder can live anywhere. The absolute paths
below are the ones the work was done at. Several scripts (`tools/check.sh`,
`tools/mklistings.py`, the instrument scripts) still default to them, so use
the same paths unless you set the scripts' variables (`SPIRV2GLASM`,
`LISTING_DIR`, ...).

```
/home/claude/work/proj/                   the repository root (.github/ + spirv2glasm/)
/home/claude/work/proj/spirv2glasm        this project
/home/claude/work/ex/port/glslcportv9     elf2c (the ELF -> C translator) and the original ELF
/home/claude/work/ex/src                  the PORT TREE: the generated C, the oracle build
/home/claude/work/glsl/glsl               the 14,706 raw GLSL corpus shaders (§3)
/home/claude/work/corpus_spv_full         the corpus as SPIR-V (glslang 15.1.0, §3)
/home/claude/work/corpus_spv              the 120-shader sample (§3)
/home/claude/work/corpus_listings         the oracle's listings for the sample
<scratch>/slice_spv, <scratch>/slice_lst  the 1,400-shader slice (§4)
```

No `.spv` file is shipped. Everything with a `.spv` is rebuilt from sources
(§3, §5).

## 1. The toolchain

Follow HANDOVER.md §2.1:

- GCC 15 plus binutils 2.44, unpacked from the Ubuntu plucky debs into
  `/opt/tc/root`.
- The wrappers `/opt/tc/cc15` and `/opt/tc/ar15`.
- Check that `/opt/tc/cc15 --version` says 15.

You also need:

- Python 3.11 or newer.
- `spirv-tools` (`spirv-as`, `spirv-dis`), from apt.
- `p7zip-full` (packaging).
- `qemu-user`, only for running the original ELF (HANDOVER.md §2.3).
- glslang. Two versions matter:
  - **16.6.0** builds the probes. It is the version their listings are for,
    and the version CI pins. Download the release
    `glslang-16.6.0-linux-x86_64-release.zip` from
    https://github.com/KhronosGroup/glslang/releases/tag/16.6.0 and check
    it against sha256
    `a3fc4f083b1793eb53e55fa3577ac9649ffbe0340715e30d116290fb5382393f`,
    as `.github/workflows/probes.yml` does.
  - **15.1.0** (Ubuntu's `glslang-tools`) built the corpus SPIR-V
    (`corpus_spv_full/MKCORPUS_RESULT.txt`). For the probes, 15.1.0 and
    16.6.0 give the same SPIR-V.

## 2. The oracle (the port tree)

The oracle is GLSLC 17.24's own compiler, translated from the AArch64 ELF
to C by elf2c. To build it:

1. Put elf2c and the ELF in `/home/claude/work/ex/port/glslcportv9`.
2. Generate the tree into `/home/claude/work/ex/src`:
   `./QUICKSTART.sh -j N <glslc.elf>`. It maps the image at base
   `0x7100000000` (the glslcportv9 README says why).
3. Install the glasm2sass package's `tools/apply_patch.sh` into the tree
   FIRST. That puts the printer hook in `src/fn/f_71010d76b0.c`.
4. Install this project's hooks:

   ```sh
   cd /home/claude/work/proj/spirv2glasm
   tools/apply_patch.sh /home/claude/work/ex/src          # tools/spirv2glasm.c, runtime/g2s_hook.c, Makefile target
   python3 tools/apply_hooks.py /home/claude/work/ex/src  # every trace hook in patches/HOOKS.tsv
   ```

5. Build. This is long, so run it in the background (§7):

   ```sh
   cd /home/claude/work/ex/port/glslcportv9
   ./run125.sh mk 3600 make -C /home/claude/work/ex/src CC=/opt/tc/cc15 AR=/opt/tc/ar15 -j4 spirv2glasm
   ./poll125.sh mk 110            # repeat until it prints mk=0
   ```

Check the result: `build/spirv2glasm --opt-level none probes/0011_op_mul.vert.spv`
must print a listing whose first line is a progress banner and whose rest
equals `listings/0011_op_mul.vert.glasm`.

`G2S_VREGAT=<hex addr>[,<addr>...]` narrows `g2s_dump_vregs` (and the `ifg`
dump inside it) to one of the nineteen functions it is patched onto: it
walks every record's whole neighbour list at each, which on a corpus shader
of a few thousand records is gigabytes and an hour of wall clock.

The trace hooks are enabled with `G2S_TRACE=1 G2S_ONLY=<gates>`. The gates
are listed in HANDOVER.md §3.2. The ones used most are `fold`, `sel`, `block`,
`liveset`, `ifg` and `simp`.

## 3. The corpus

The raw corpus is 14,706 GLSL shaders named `<family>#<hash>[-n].<stage>`.
The work kept it in `/home/claude/work/glsl/glsl`. `corpus/mkcorpus.py`
normalises it for glslang and compiles it:

```sh
python3 corpus/mkcorpus.py /home/claude/work/glsl/glsl /home/claude/work/corpus_spv_full --jobs 4
```

The output files are named `<family>_<hash>[-n].<stage>.spv`. Beside them
are `.src/`, the normalised GLSL each module was compiled from, and
`MKCORPUS_RESULT.txt`. With glslang 15.1.0 the current mkcorpus.py gives
14,706 shaders, 14,630 compiled and 76 rejected (72 "cannot convert a
sampler").

The normalised GLSL alone was also shipped separately as
`corpus_glsl_normalised.7z` (`glsl/<name>.<stage>` plus the result file).
Compiling those files with glslang 15.1.0 (`-V -S <stage>`) gives the same
modules.

Then the 120-shader sample and its listings.  The sample's module names and
the listings are corpus data and are NOT in the package; they are kept with
the corpus.  With the name list in hand (one `.spv` name per line):

```sh
mkdir -p /home/claude/work/corpus_spv /home/claude/work/corpus_listings
cd /home/claude/work/corpus_spv_full
cp $(cat /path/to/corpus_sample.txt) /home/claude/work/corpus_spv/
# the oracle's listings: bounded foreground runs, re-run until nothing is left
/home/claude/work/proj/spirv2glasm/tools/mklistings_run.sh \
    /home/claude/work/corpus_spv /home/claude/work/corpus_listings 110
```

## 4. The slice, and the full listing set

The slice is 1,400 corpus modules, including compute.  It was the wider
regression net while the full set of listings did not exist; it is kept
because several notes report on it, but THE REGRESSION NET IS NOW
`tools/exact500b.stems` (500 modules drawn from the exact ones) and the
measurement of record is the full 14,630.  Its name list is corpus data too
and is kept with the corpus, not in the package.

```sh
S=/path/to/scratch
mkdir -p $S/slice_spv $S/slice_lst
cd /home/claude/work/corpus_spv_full && cp $(cat /path/to/slice.txt) $S/slice_spv/
# the oracle's listings: bounded foreground runs, re-run until nothing is left
/home/claude/work/proj/spirv2glasm/tools/mklistings_run.sh $S/slice_spv $S/slice_lst 110
```

`mklistings_run.sh` explains why it runs in the foreground and resumes where
it stopped.

### The full set: what `exact500b.sh` and `fullcmp.py` read

Both gates read a directory PAIR holding all 14,630 modules and all 14,630
oracle listings -- `$S/full_spv` and `$S/full_lst` at the paths the work was
done at, which is what `tools/exact500b.sh`'s `S=` defaults to.  Build them
the same way as the slice, over the whole corpus rather than a name list:

```sh
S=/path/to/scratch
mkdir -p $S/full_spv $S/full_lst
cp /home/claude/work/corpus_spv_full/*.spv $S/full_spv/
/home/claude/work/proj/spirv2glasm/tools/mklistings_run.sh $S/full_spv $S/full_lst 110
```

That is about two hours of oracle runs, once; every sweep after it is free.
`tools/fullcmp.py` (`OUT=<tsv>`) is the sweep, in chunks of 200 appended as
it goes, so it resumes from its own output after the turn boundary kills it.

## 5. The probes

The probe sources (`probes/*.{vert,frag,geom,tesc,tese,comp}` and
`probes/*.spvasm`) and their oracle listings (`listings/*.glasm`) ship as ONE
archive, `probes.7z`.  Unpack it in the project folder, then build the
SPIR-V next to the sources (`tools/check.sh` reads `probes/*.spv`) and check
every probe:

```sh
cd /home/claude/work/proj/spirv2glasm
7z x -y probes.7z
python3 tools/probecheck.py --glslang /path/to/glslang-16.6.0/bin/glslangValidator --keep probes -j 4
```

To ship changed probes, repack the two folders (no `.spv`):

```sh
7z a -t7z -mx=9 probes.7z probes listings -xr'!*.spv'
```

`listings_open/` holds the ORACLE's listing for a probe that IS in `probes/`
but whose rule is read and not yet implemented -- evidence, not a gate.  It
ships in `probes.7z` with the other two.  Move a file from it into
`listings/` when the rule lands; never delete one to make the suite green.

`notes/pending_probes/` is the other holding pen, for a probe that is not in
the suite at all yet: source and `.glasm` together.  When one becomes exact,
move both into `probes/` and `listings/`, first checking that its name does
not collide with an existing probe.  Its own README says what is in it.

A probe is named for the note that reads it (`0119_bar_a.comp`); `0000_`
marks one older than the note that would name it.  A `.glasm` kept without
its source cannot be re-measured, so keep the pair.

## 6. The checks, and the numbers to expect

| command | what | expected now (notes/115-130) |
|---|---|---|
| `python3 tools/probecheck.py -j 2` | THE FIRST GATE: every probe from source, about a minute | `680 of 680 probes match`; failures print as `FAIL <probe>: <reason>` |
| `sh tools/exact500b.sh` | THE SECOND GATE: 500 corpus shaders drawn from the exact ones, about 8 min | `exact 500  prefix-only 0  DIFFERS 0  failed 0` |
| `sh tools/check.sh` | corpus sample + probes, whole listing (about 90 s) | corpus exact 120/120, probes 680/680 |
| `python3 tools/opcoverage.py` | every opcode the oracle accepts that the corpus never exercises, rebuilt into `opcov/` | `ACCEPTED` for 49 modules, `REJECTED` for the two that crash the compiler (`opcov/README.md`) |
| `python3 tools/mkglasmref.py --check` | GLASM-REFERENCE.md against the opcode table, the listings and the per-opcode evidence | `GLASM-REFERENCE.md is current`; exit 1 and a `DRIFT:` line per row whose verdict the listings disagree with |
| `python3 tools/covercheck.py` | the six cover modules against the full sweep's opcode set | `MISSING from the cover set (0):` -- anything else means a cover module stopped reaching an opcode |
| `MODULES=opcov/cover OUT=notes/opcode_evidence_cover.json python3 tools/opcodemap.py` | rebuild the per-opcode evidence from the six cover modules, about a minute | `135 opcodes observed over 6 modules` |
| `python3 tools/compare.py <lst> <spv> -j 2 --only stems.txt` | only the listed modules (`x.frag`, one per line); `G2S_TSV=<path>` also writes one row per module | |
| `OUT=<tsv> python3 tools/fullcmp.py` | every module classified, resumable, ~4 h | 2026-09-23: `exact 14,208  prefix-only 0  DIFFERS 422  failed 0` |
| `python3 tools/diffkind.py --tsv <tsv> <lst> <spv>` | splits the DIFFERS into RENAME / REORDER / MISSING | one line per stem and a count; the 422 have not been split yet |
| `ONLY=<stems> OUT=<tsv> python3 tools/refcensus.py` | the PREFIX census: one line per module, its refusal or empty.  Needs no oracle listing | should be empty now |

**PREFIX-ONLY MUST STAY 0**, on both gates.  That is the invariant now.

`DIFFERS` is NOT 0 any more and that is not a regression: it was 0 only
because a module that hit an unread rule stopped at its declarations instead
of emitting a body.  With the refusals closed, the body is emitted and meets
whatever the refusal had been hiding.  Both gates are at DIFFERS 0 and must
stay there; the corpus at large is not, and that is the work.

## 7. Long jobs

A single shell command gets two minutes. Anything longer is launched with
`tools/run125.sh` and polled with `tools/poll125.sh`. The originals are in
the elf2c folder, and copies ship in `tools/`. Run both from the directory
the job's log should go to:

```sh
cd $S
sh /home/claude/work/proj/spirv2glasm/tools/run125.sh sl 3000 python3 /home/claude/work/proj/spirv2glasm/tools/compare.py slice_lst slice_spv
sh /home/claude/work/proj/spirv2glasm/tools/poll125.sh sl 110   # waits up to 110 s itself, so no sleep first; repeat
```

A background job dies with the turn that launched it (see `run125.sh`'s
header), so keep polling within the same turn until it finishes.

## 8. The comparison tools

These are the tools used to confirm a rule against the oracle before it is
kept:

| tool | shows |
|---|---|
| `tools/nodedump.py x.spv` | the oracle's listing with each line's vreg and seq (`node[36]`) |
| `tools/gsum.py x.spv` | the oracle's blocks and edges: seq, t68, successor list with edge kinds |
| `tools/livecheck.py x.spv` | the oracle's live array at every position (records, seed) |
| `P2WHO=<text> tools/p2trace.py x.spv` | our pass 2, candidate by candidate |
| `G2S_P2DBG=<text> spirv2glasm.py x.spv` | our pass-2 items: seq, t68, successors, per block |
| `G2S_LIVEDBG=1 spirv2glasm.py x.spv` | our live array at every position |
| `G2S_VLINES=1 spirv2glasm.py x.spv` | our lines with placeholders, and each vreg's record and register |
| `tools/samecheck.py save\|check <out> <spv-dirs>` | the converter against a saved copy of its own output (refactors that must not change a byte) |
| `tools/lexfuzz.py [N]` | every regex-free scanner in the compiler against the pattern it replaced (notes/112) |
| `tools/recnum.py x.spv x.simp` | the compiler's record NUMBERING against ours, paired by (first def, last use, mask) -- for when the numbering itself differs (notes/114) |
| `tools/schedcheck.py [probe...]` | notes/51's PASS 1 on the compiler's own DAG. The implicit register reads come from `g2s_dag`'s `irr=`, not the fold dump -- reading only the fold lines left them empty and a node held live by one alone looked ready at seeding |
| `tools/entrydump.py x.spv` | THE BLOCK'S LIVE-ENTRY LIST, `block[80]` -- pass 1's entry order, which nothing printed before the `g2s_trace_entries` hook (0x4a3a4). The entries are the NAMES' stores, in statement order |
| `tools/degcmp.py x.spv x.lc [x.simp]` | THE TWO INTERFERENCE GRAPHS by degree -- the compiler's is in the `liveset` trace (notes/52's edge rule), so nothing has to be dumped; with the simp dump the records are paired as `recnum.py` pairs them.  This read notes/114 §26 (253 shaders) |
| `tools/ifgdump.py x.spv out` | the `ifg` trace compacted to the last rows per vreg (gigabytes to megabytes); `G2S_VREGAT=<addr>` narrows the hook's record dump to one patched site, `G2S_IFGKEEP` how many rows to keep |
| `tools/livecheck.py x.spv` | the compiler's LIVE SETS position by position (`G2S_DUMPFILE` saves/reuses the trace; it asks for `liveset,stamps,node` only, which is what makes it usable on a corpus shader) |
| `tools/waredge.py [-same] [-v] x.spv...` | candidate ANTI-DEPENDENCE orders scored against every reader in the trace, so the key is read rather than fitted (notes/114 §8) |
| `tools/p2check.py [-v] x.spv...` | OUR pass 2 on the COMPILER's blocks (its stamps, its edges) against the order it printed -- splits a scheduler bug from a lowering or allocation one (notes/114) |
| `tools/regmap.py <oracle.glasm> <ours.glasm>` | two listings that differ only in REGISTER NAMES: the renaming, and the first line where it breaks -- where the colouring diverges, with no oracle run (notes/114) |
| `G2S_TIEDBG=<text> spirv2glasm.py x.spv` | the line groups (`ties`) a line belongs to, and the construct statements |
| `G2S_TEMPDBG=#n\|* spirv2glasm.py x.spv` | why a stored value is (not) a statement temp with a flush |
| `G2S_ORACLETIMEOUT=<s>` | how long the trace tools wait for the oracle (a big corpus shader takes minutes) |

Every rule in `py/` has an environment switch that turns it off
(`G2S_NO...`). The notes name each one. Switching a rule off and re-running a
probe is how to show that the probe depends on the rule.
