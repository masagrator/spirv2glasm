# spirv2glasm

SPIR-V → GLASM for GLSLC 17.24, the same way `glasm2sass` is GLASM → SASS:
by running the compiler's own front end, not by modelling it.

    SPIR-V --[spirv2glasm]--> GLASM --[glasm2sass]--> .code (Maxwell SASS)

* **`SETUP.md`** — how to restore the working setup from this package: the
  package layout (the repository root holds `.github/` and this
  `spirv2glasm/` folder), the toolchain, the oracle, the corpus, the slice,
  the probes, and the numbers each check should give.
* **`HANDOVER.md`** — **read this first.**  The brief and its constraints, the
  machine (GCC 15 and binutils 2.44 and how to install them, the HTTPS proxy,
  QEMU for the original ELF, the background-job scripts), every instrument and
  how to add one, where the work stands and what comes next.
* **`PROGRESS.md`** — the last session's report.
* **`SPIRV2GLASM.md`** — what the tool is, the finding it rests on, what was
  measured, and how to build it.
* **`PYTHON-STATUS.md`** — where the Python reimplementation stands, what is
  established and on what, and what is left in the order to do it.
* **`notes/`** — what has been read out of the compiler's image, every claim
  with the address to re-read it at.
* **`tools/`** — the oracle's source, the patch that installs it, the round-trip
  verifier, and the instruments: disassembly by guest address (`dis.sh`,
  `dumpfn.py`), literal annotation (`annot.py`), jump-arm decoding
  (`armfmt.py`, `opname_dec.py`), the call tracer (`trace_patch.py`), the
  binding-pair measurement (`bindmap.py`) and the declaration-block comparison
  (`checkattrib.py`, `parcheck.sh`).  `check.sh` is THE verification, with
  `probecheck.py` for the single probe being worked on;
  `census.py` weights the refusals by listing lines, which is what says where
  the work is; `emitorder.py`, `schedcheck.py` and `selcheck.py` test the
  two scheduling passes' emission order
  against the compiler's own insertion trace; `indirect_patch.py` names the
  indirect calls a function makes.
* **`py/`** — the Python converter: the SPIR-V reader, the generated grammar,
  `spvnames.py` (SPIR-V numbers by their Khronos names, read from the
  unmodified Khronos files in `py/khronos/`), `glasmnames.py` (GLSLC's own
  opcodes and type codes by name, from the generated table
  `py/glslc/glasm.py`, notes/93), the emitter -- `glasm.py`, a
  facade over `glasmlib/` and its body lowering `glasmlib/lower/` (notes/92)
  -- the scheduler and allocator (`sched.py`, `ifg.py`, `regalloc.py`), and
  `binding.py`, the transcription of the compiler's own binding-name
  formatter.
* **`corpus/`** — the script that gets 14,630 of the 14,706 real shaders
  into SPIR-V (glslang 15.1.0), and what the other 76 run into.
* **`probes.7z`** — the probes, as one archive: `probes/` (627 shaders that
  vary one thing at a time, sources only) and `listings/` (the oracle's
  answer for every probe), what `tools/probecheck.py` and `tools/compare.py`
  measure the Python side against.  Unpack it with `7z x probes.7z`.
  Nothing from the real-shader corpus is in the package (SETUP.md §0).

## The Python converter from the command line

`spirv2glasm.py` is the front end of the Python converter: one input, one
output, no port tree needed.

```sh
python3 spirv2glasm.py shader.vert.spv -o shader.glasm      # whole listing
python3 spirv2glasm.py shader.vert.spv                      # to stdout
python3 spirv2glasm.py module.spv --entry vs_main -o out.glasm
python3 spirv2glasm.py shader.frag.spv --form control -o out.txt   # fat-control spelling
python3 spirv2glasm.py shader.frag.spv --partial -o out.glasm      # established prefix
python3 spirv2glasm.py a.frag.spv b.vert.spv -o outdir -j 2        # several at once
```

Several inputs are converted in one run: `-o` names a directory and each
listing goes to `<dir>/<name>.glasm`; `-j N` converts N at a time in worker
processes; the exit status is the highest any input gave.

It produces the `--opt-level none` / `--debug-info none` listing (the form
every saved listing is; notes/50) and refuses any other level.  Where the
converter reaches a construct whose rule is not read it writes NOTHING, names
the section and the reason on stderr and exits 3; `--partial` writes the
established prefix instead (still exit 3).  Exit 1 is an input GLSLC itself
rejects, 4 a converter bug.  The section join lives in `py/listing.py` and is
shared with `tools/compare.py`, so the CLI and the measurement cannot drift:
all 488 probes come out byte-identical to `listings/`.

## Probe check (CI)

The package carries the probe SOURCES (`probes/*.vert`, `*.frag`, `*.geom`,
`*.tesc`, `*.tese`, `*.comp`, and two hand-written `*.spvasm`) and the
expected listings (`listings/*.glasm`) in one archive, `probes.7z` -- unpack
it first (`7z x probes.7z`) -- and no SPIR-V.  `tools/probecheck.py` builds
every probe that has a listing -- `glslangValidator -V -S <stage>`, or
`spirv-as --target-env vulkan1.0` for the `.spvasm` ones -- runs
`spirv2glasm.py` on it and compares byte for byte:

```sh
python3 tools/probecheck.py                      # tools from PATH
python3 tools/probecheck.py --keep probes        # also leaves probes/*.spv,
                                                 # which tools/check.sh reads
```

`-j N` (default: the CPU count; the workflow uses 2) runs N worker
processes, each compiling and converting a batch of probes with the
converter imported once (`spirv2glasm.convert`).

The expected listings are the oracle's output for the SPIR-V **glslang
16.6.0** produces (15.1.0 gives the same); an older glslang, for one, does
not declare `gl_FragCoord`, and the header loses a `#var` line.  The GitHub
workflow `.github/workflows/probes.yml` -- at the REPOSITORY root, one level
above this folder, and run with `spirv2glasm/` as its working directory --
PINS glslang 16.6.0: it downloads
the release's prebuilt `glslang-16.6.0-linux-x86_64-release.zip`, checks
its SHA-256 and `--version`, installs Ubuntu's `spirv-tools` for
`spirv-as`, and runs the check on every push and pull request.  To move to
a newer glslang, change `GLSLANG_VERSION`, the URL and the hash, and
regenerate any listing whose SPIR-V changes.

## Quick start

```sh
# build (needs GCC 15+ / binutils >= 2.44 -- see SPIRV2GLASM.md section 5)
tools/apply_patch.sh path/to/glslc17.24.113src     # after glasm2sass's
cd glslc17.24.113src && make -r -j"$(nproc)" && make spirv2glasm glasm2sass cli

# the listing for a module
build/spirv2glasm shader.vert.spv --opt-level none --debug-info none \
                  --output-thin-gpu-binaries

# the round trip, over the probe set.  Fat binaries and --debug-info g2, so
# the compiler stores its own listing and the capture can be checked against
# it -- see tools/verify.sh's header for why thin + none is the wrong choice.
ls probes/*.spv > list.txt
BIN=<port>/build tools/verify.sh list.txt /tmp/v --opt-level none --debug-info g2
#   identical 90  differing 0  failed 0

# the same over the real corpus, in parallel
python3 corpus/mkcorpus.py /path/to/glsl /path/to/spv --jobs 4
ls /path/to/spv/*.spv > corpus.txt
BIN=<port>/build tools/parverify.sh corpus.txt /tmp/cv 4 --opt-level none --debug-info g2

# how far the Python side gets -- on probes, and on real shaders
python3 tools/compare.py listings probes
#   exact 0  prefix-only 90  DIFFERS 0  failed 0
#   966 of 3139 listing lines reproduced (30.8%)
python3 tools/compare.py corpus_listings /path/to/spv
#   exact 0  prefix-only 119  DIFFERS 0  failed 1
```
