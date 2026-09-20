# The corpus result

```
identical 10328  differing 0  failed 0
```

Every one of the 10,328 SPIR-V modules an older `mkcorpus.py` built from the
14,706-shader GLSL corpus (the current one builds 14,630, README.md), through the full round trip, with **all three checks**:

| check | what it compares | result |
|---|---|---|
| **debug** | the captured listing against the one the compiler stored VERBATIM in the debug-info section | byte-identical, 10,328/10,328 |
| **control** | the captured listing against the comment-stripped one in the fat control section | matches, 10,328/10,328 |
| **code** | the `.code` rebuilt from the listing by `glasm2sass` against the `.code` the compiler produced from the module | byte-identical, 10,328/10,328 |

Flags: `--opt-level none --debug-info g2`, **fat** binaries.  Those are what
make the compiler store its own listing in both forms; under
`--output-thin-gpu-binaries --debug-info none` it stores neither and only the
third check is possible.

Reproduce:

```sh
python3 corpus/mkcorpus.py /path/to/glsl /path/to/spv --jobs 4
ls /path/to/spv/*.spv > corpus.txt
BIN=<port>/build tools/parverify.sh corpus.txt /tmp/cv 4 --opt-level none --debug-info g2
```

About four CPU-hours on two cores; `parverify.sh` splits the list and adds the
counts up.  Successful working directories are removed as it goes (`KEEP=1`
keeps them) because the run writes gigabytes of intermediates and the modules
that agreed have nothing left to say.

## What this does and does not establish

**Does.**  That the listing `spirv2glasm` reports is the compiler's own
listing, on ten thousand real shaders and not on probes — the compiler itself
says so, twice, in the two forms it stores.  And that the listing is the whole
program at the handover: the back end reaches the same SASS from the listing
alone as it did from the module.

**Does not.**  Anything about the Python converter.  That is measured
separately by `tools/compare.py` against the oracle's listings, and
`PYTHON-STATUS.md` reports where it stands.  Running the corpus proves the
oracle; it is the ruler, not the thing being measured.
