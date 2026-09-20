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
                                 (probes/ and listings/ inside, §5)
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

Check the result: `build/spirv2glasm --opt-level none probes/op_mul.vert.spv`
must print a listing whose first line is a progress banner and whose rest
equals `listings/op_mul.vert.glasm`.

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

## 4. The slice

The slice is 1,400 corpus modules, including compute, the wider regression
net the work reports on.  Its name list is corpus data too and is kept with
the corpus, not in the package.

```sh
S=/path/to/scratch
mkdir -p $S/slice_spv $S/slice_lst
cd /home/claude/work/corpus_spv_full && cp $(cat /path/to/slice.txt) $S/slice_spv/
# the oracle's listings: bounded foreground runs, re-run until nothing is left
/home/claude/work/proj/spirv2glasm/tools/mklistings_run.sh $S/slice_spv $S/slice_lst 110
```

`mklistings_run.sh` explains why it runs in the foreground and resumes where
it stopped.

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

`notes/pending_probes/` holds probes whose oracle listings are known but that
the converter does not produce yet: each source with its `.glasm`. When one
becomes exact, move it into `probes/` and `listings/`, first checking that its
name does not collide with an existing probe.

## 6. The checks, and the numbers to expect

| command | what | expected now (notes/111) |
|---|---|---|
| `sh tools/check.sh` | corpus sample + probes, whole listing (about 90 s) | corpus exact 120/120, probes 627/627, DIFFERS 0 |
| `python3 tools/probecheck.py ...` | probes from source (also what CI runs) | 627 of 627 probes match |
| `python3 tools/compare.py $S/slice_lst $S/slice_spv` | the slice (about 20 min, §7) | see PROGRESS.md for the last run; DIFFERS 0 |
| `python3 tools/compare.py <lst> <spv> -j 2 --only stems.txt` | the same, in 2 worker processes, only the listed modules (`x.frag`, one per line) | |

`DIFFERS` must stay 0 everywhere.

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
| `tools/waredge.py [-same] [-v] x.spv...` | candidate ANTI-DEPENDENCE orders scored against every reader in the trace, so the key is read rather than fitted (notes/114 §8) |
| `tools/p2check.py [-v] x.spv...` | OUR pass 2 on the COMPILER's blocks (its stamps, its edges) against the order it printed -- splits a scheduler bug from a lowering or allocation one (notes/114) |
| `G2S_TIEDBG=<text> spirv2glasm.py x.spv` | the line groups (`ties`) a line belongs to, and the construct statements |
| `G2S_TEMPDBG=#n\|* spirv2glasm.py x.spv` | why a stored value is (not) a statement temp with a flush |
| `G2S_ORACLETIMEOUT=<s>` | how long the trace tools wait for the oracle (a big corpus shader takes minutes) |

Every rule in `py/` has an environment switch that turns it off
(`G2S_NO...`). The notes name each one. Switching a rule off and re-running a
probe is how to show that the probe depends on the rule.
