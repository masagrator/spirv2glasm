#!/usr/bin/env python3
"""probecheck.py -- build every probe from source and check the converter.

    python3 tools/probecheck.py [--glslang glslangValidator]
                                [--spirv-as spirv-as] [--keep DIR] [-j N]

For every expected listing `listings/<name>.glasm` the probe source is
`probes/<name>` (GLSL, the stage from its extension: vert, frag, geom, tesc,
tese, comp) or `probes/<name>.spvasm` (hand-written SPIR-V assembly, for shapes
glslang does not produce).  Each is compiled to SPIR-V in a scratch directory
-- `glslangValidator -V -S <stage>` or `spirv-as --target-env vulkan1.0` --
converted with `spirv2glasm.py` (the Python converter), and the output is
compared BYTE FOR BYTE with the expected listing.

`-j N` checks N probes at a time, each in a worker PROCESS that compiles
and converts a whole batch of probes: the converter is imported once per
worker (`spirv2glasm.convert`), not started once per probe.

The expected listings are the oracle's (the C port of the compiler,
`--opt-level none`) for the SPIR-V glslang 16.6.0 (`main`; 15.1.0 gives
the same) produces from these sources.  Another glslang can produce
different SPIR-V -- an older one does not declare `gl_FragCoord`, which
drops a `#var` line -- and then the check fails on the listing, not on the
converter.

Exit status 0 when every probe matches; 1 otherwise, with a unified diff of
the first lines that differ for each failing probe.
"""
import argparse
import concurrent.futures
import difflib
import os
import shutil
import subprocess
import sys
import tempfile

import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
PROBES = os.path.join(ROOT, "probes")
LISTINGS = os.path.join(ROOT, "listings")
CONVERTER = os.path.join(ROOT, "spirv2glasm.py")
STAGES = ("vert", "frag", "geom", "tesc", "tese", "comp")


def source_of(name):
    """(path, kind) of a probe's source, or (None, None)."""
    asm = os.path.join(PROBES, name + ".spvasm")
    if os.path.exists(asm):
        return asm, "spvasm"
    glsl = os.path.join(PROBES, name)
    if os.path.exists(glsl) and name.rsplit(".", 1)[-1] in STAGES:
        return glsl, "glsl"
    return None, None


def compile_probe(name, out, args):
    src, kind = source_of(name)
    if src is None:
        return "no source for %s" % name
    if kind == "glsl":
        cmd = [args.glslang, "-V", "-S", name.rsplit(".", 1)[-1],
               "-o", out, src]
    else:
        cmd = [args.spirv_as, "--target-env", "vulkan1.0", src, "-o", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return "compile failed: %s\n%s%s" % (" ".join(cmd), r.stdout,
                                             r.stderr)
    return None


_CONVERTER = []


def _converter():
    """spirv2glasm.py as a module, imported once per worker process."""
    if not _CONVERTER:
        spec = importlib.util.spec_from_file_location("spirv2glasm",
                                                      CONVERTER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _CONVERTER.append(mod)
    return _CONVERTER[0]


def check(name, scratch, args):
    """None when the probe matches, else a message."""
    spv = os.path.join(scratch, name + ".spv")
    err = compile_probe(name, spv, args)
    if err:
        return err
    status, got, msgs = _converter().convert(spv)
    if status != 0:
        return "converter exit %d: %s" % (status, "\n".join(msgs))
    with open(os.path.join(LISTINGS, name + ".glasm"), encoding="utf-8") as f:
        want = f.read()
    if got == want:
        return None
    diff = list(difflib.unified_diff(want.splitlines(), got.splitlines(),
                                     "expected", "converter", n=2,
                                     lineterm=""))
    return "listing differs:\n" + "\n".join(diff[:40])


def _check_one(job):
    """A worker's job (picklable, for the process pool)."""
    return check(*job)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--glslang", default="glslangValidator")
    ap.add_argument("--spirv-as", dest="spirv_as", default="spirv-as")
    ap.add_argument("--keep", help="keep the built SPIR-V in this directory")
    ap.add_argument("-j", type=int, default=os.cpu_count() or 1)
    ap.add_argument("names", nargs="*", help="probe names (default: all)")
    args = ap.parse_args()
    names = args.names or sorted(n[:-len(".glasm")]
                                 for n in os.listdir(LISTINGS)
                                 if n.endswith(".glasm"))
    scratch = args.keep or tempfile.mkdtemp(prefix="probecheck-")
    os.makedirs(scratch, exist_ok=True)
    failed = []
    try:
        with concurrent.futures.ProcessPoolExecutor(args.j) as pool:
            for name, msg in zip(names, pool.map(
                    _check_one, [(n, scratch, args) for n in names],
                    chunksize=8)):
                if msg is not None:
                    failed.append(name)
                    print("FAIL %s: %s\n" % (name, msg))
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
    print("%d of %d probes match" % (len(names) - len(failed), len(names)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
