#!/usr/bin/env python3
"""spirv2glasm.py -- SPIR-V in, GLASM out: the Python converter's front end.

    python3 spirv2glasm.py shader.vert.spv                 # listing to stdout
    python3 spirv2glasm.py shader.vert.spv -o shader.glasm
    python3 spirv2glasm.py module.spv --entry vs_main -o out.glasm
    python3 spirv2glasm.py shader.frag.spv --form control -o out.txt
    python3 spirv2glasm.py shader.frag.spv --partial -o out.glasm
    python3 spirv2glasm.py a.frag.spv b.vert.spv -o outdir -j 2

SEVERAL INPUTS are converted in one run: `-o` then names a DIRECTORY
(created if missing), and each listing is written to `<dir>/<name>.glasm`,
`<name>` being the input's file name without `.spv`.  `-j N` converts N at a
time, in N worker processes.  Diagnostics come in input order, and the exit
status is the highest any input gave.

This drives the PYTHON converter in py/ -- the reimplementation -- not the C
oracle.  The oracle (`build/spirv2glasm`, tools/spirv2glasm.c) has its own
command line and needs the built port tree; this needs only Python 3.

WHAT IT PRODUCES.  The listing GLSLC 17.24 prints for the module compiled at
`--opt-level none` with the default `--debug-info none`, byte for byte -- the
form every saved listing in listings/ and corpus_listings/ is (notes/50).
`--form control` gives the same program as the fat control section stores it,
comment lines dropped and blank runs collapsed (tools/checklisting.py).  The
`--opt-level` / `--debug-info` options are accepted with the oracle's
spelling so a command line can be moved between the two tools, and only
`none` is accepted for either: at any other level the compiler's body is a
different shape (g0/g1/g2 add an instruction, notes/50), and no rule for it
has been read.

WHEN THE CONVERTER STOPS.  The converter never emits a line whose rule is not
established; where it reaches one it raises `NotEstablished` with the reason
(PYTHON-STATUS.md).  By default that is a refusal: nothing is written, the
section and the reason go to stderr, and the exit status is 3.  `--partial`
writes the established PREFIX instead -- every line of it the compiler's --
and still reports the refusal and exits 3, so a script cannot mistake a
prefix for a whole listing.

The stage is the execution model of the entry point `--entry` names (default
`main`), read off the module's own OpEntryPoint, exactly as the oracle does.
Compute has no rule and is refused by design (HANDOVER.md §1).

Exit status:
    0  the whole listing was written
    1  the input is not a module GLSLC accepts (bad file, bad SPIR-V, an
       opcode with no handler in the compiler's dispatch tables, no such
       entry point)
    2  bad command line
    3  the converter reached a construct whose rule is not established
       (with --partial, the prefix was written)
    4  the converter crashed -- a bug, not a gap
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "py"))
import spv                                                     # noqa: E402
import listing                                                 # noqa: E402
from spvnames import ExecutionModel                            # noqa: E402

EXIT_OK, EXIT_INPUT, EXIT_USAGE, EXIT_GAP, EXIT_BUG = 0, 1, 2, 3, 4

# The oracle's execution-model spelling (tools/spirv2glasm.c, stage_name),
# so a message names a stage the same way in both tools.
STAGE_NAME = {ExecutionModel.Vertex: "vertex",
              ExecutionModel.TessellationControl: "tess_control",
              ExecutionModel.TessellationEvaluation: "tess_evaluation",
              ExecutionModel.Geometry: "geometry",
              ExecutionModel.Fragment: "fragment",
              ExecutionModel.GLCompute: "compute"}


def _parser():
    p = argparse.ArgumentParser(
        prog="spirv2glasm.py",
        description="Convert a SPIR-V module to the GLASM listing GLSLC 17.24 "
                    "produces for it, with the Python converter.",
        epilog="Exit status: 0 whole listing, 1 input rejected, 2 usage, "
               "3 construct not established (prefix written with --partial), "
               "4 converter bug.")
    p.add_argument("inputs", nargs="+", metavar="input",
                   help="the SPIR-V module(s) (.spv), or - for stdin")
    p.add_argument("-o", "--output", default="-",
                   help="where to write the listing (default: stdout); with "
                        "several inputs, the directory to write them to")
    p.add_argument("-j", "--jobs", type=int, default=1,
                   help="with several inputs, convert this many at a time "
                        "(default 1)")
    p.add_argument("--entry", default="main",
                   help="the entry point to compile (default: main)")
    p.add_argument("--form", choices=("plain", "control"), default="plain",
                   help="plain: the listing as printed and as the debug-info "
                        "section stores it (default); control: the fat "
                        "control section's comment-stripped spelling")
    p.add_argument("--partial", action="store_true",
                   help="on a construct whose rule is not established, write "
                        "the established prefix instead of nothing (the exit "
                        "status is still 3)")
    p.add_argument("--opt-level", default="none",
                   help="accepted for parity with the oracle; only 'none'")
    p.add_argument("--debug-info", default="none",
                   help="accepted for parity with the oracle; only 'none'")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="no diagnostics on stderr, only the exit status")
    return p


def _say(msgs, fmt, *a):
    msgs.append("spirv2glasm.py: " + (fmt % a if a else fmt))


def _read_input(path):
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as fh:
        return fh.read()


def _write_output(path, text):
    if path == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    # Written whole or not at all: a half-written listing left behind by an
    # I/O error would look like a converter prefix.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _options_supported(args, msgs):
    """Only `none` is established for the two oracle-parity options."""
    for opt, val in (("--opt-level", args.opt_level),
                     ("--debug-info", args.debug_info)):
        if val != "none":
            _say(msgs, "%s %s: only 'none' is established -- the compiler's "
                 "body takes a different shape at any other level "
                 "(notes/50); use the oracle build/spirv2glasm for it",
                 opt, val)
            return False
    return True


def _load_module(path, entry, msgs):
    """(module, entry point), or None after saying why the input is
    rejected."""
    try:
        data = _read_input(path)
    except OSError as exc:
        _say(msgs, "cannot read %s: %s", path, exc.strerror or exc)
        return None
    try:
        module = spv.Module(data)
    except spv.SpvError as exc:
        _say(msgs, "%s: not a SPIR-V module GLSLC accepts: %s", path, exc)
        return None
    # GLSLC's own dispatch tables: an opcode with no handler is error 8001,
    # "SPIR-V: Invalid %s", in the compiler (spv.Module.check).
    mod_bad, body_bad = module.check()
    if mod_bad or body_bad:
        for ins, why in (mod_bad + body_bad)[:10]:
            _say(msgs, "%s: GLSLC rejects %s (opcode %d): %s",
                 path, ins.name, ins.opcode, why)
        return None
    ep = module.entry_point(entry)
    if ep is None:
        have = ", ".join(repr(e[2]) for e in module.entry_points) or "none"
        _say(msgs, "%s: no entry point %r (the module has %s)",
             path, entry, have)
        return None
    return module, ep


def convert(path, entry="main", form="plain", partial=False):
    """One module: (exit status, listing text or None, [diagnostics]).

    The text is None when nothing is to be written (a rejected input, a
    refusal without `partial`, a crash)."""
    msgs = []
    loaded = _load_module(path, entry, msgs)
    if loaded is None:
        return EXIT_INPUT, None, msgs
    module, ep = loaded
    try:
        lines, refusal = listing.build(module, entry)
    except RuntimeError as exc:
        _say(msgs, "converter bug on %s (%s stage): %s", path,
             STAGE_NAME.get(ep[0], ep[0]), exc)
        return EXIT_BUG, None, msgs
    if refusal is not None:
        _say(msgs, "%s (%s stage): not established in %s",
             path, STAGE_NAME.get(ep[0], ep[0]), refusal)
        if not partial:
            return EXIT_GAP, None, msgs
        _say(msgs, "writing the established prefix, %d lines", len(lines))
    return (EXIT_GAP if refusal is not None else EXIT_OK,
            listing.render(lines, form), msgs)


def _convert_to(job):
    """A worker's job: convert `path` and write `out`; (status, msgs)."""
    path, out, entry, form, partial = job
    status, text, msgs = convert(path, entry, form, partial)
    if text is not None:
        try:
            _write_output(out, text)
        except OSError as exc:
            _say(msgs, "cannot write %s: %s", out, exc.strerror or exc)
            return EXIT_INPUT, msgs
    return status, msgs


def _output_name(path):
    name = os.path.basename(path)
    if name.endswith(".spv"):
        name = name[:-len(".spv")]
    return name + ".glasm"


def main(argv=None):
    args = _parser().parse_args(argv)
    msgs = []

    def flush(lines):
        if not args.quiet:
            for m in lines:
                sys.stderr.write(m + "\n")
            sys.stderr.flush()
    if not _options_supported(args, msgs):
        flush(msgs)
        return EXIT_USAGE
    if len(args.inputs) == 1:
        status, msgs = _convert_to((args.inputs[0], args.output, args.entry,
                                    args.form, args.partial))
        flush(msgs)
        return status
    # SEVERAL INPUTS: `-o` is a directory, one listing per input in it
    if "-" in args.inputs or args.output == "-":
        _say(msgs, "several inputs need -o DIR, and cannot read stdin")
        flush(msgs)
        return EXIT_USAGE
    names = [_output_name(p) for p in args.inputs]
    if len(set(names)) != len(names):
        _say(msgs, "two inputs would write the same %s/<name>.glasm",
             args.output)
        flush(msgs)
        return EXIT_USAGE
    try:
        os.makedirs(args.output, exist_ok=True)
    except OSError as exc:
        _say(msgs, "cannot create %s: %s", args.output, exc.strerror or exc)
        flush(msgs)
        return EXIT_INPUT
    jobs = [(p, os.path.join(args.output, n), args.entry, args.form,
             args.partial) for p, n in zip(args.inputs, names)]
    worst = EXIT_OK
    if args.jobs > 1:
        import concurrent.futures
        with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
            results = list(pool.map(_convert_to, jobs, chunksize=4))
    else:
        results = map(_convert_to, jobs)
    for status, lines in results:
        flush(lines)
        worst = max(worst, status)
    return worst


if __name__ == "__main__":
    sys.exit(main())
