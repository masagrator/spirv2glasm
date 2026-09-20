"""listing.py -- the whole listing, section by section, stopping at the gap.

The converter's output side (`glasm.py`) is a set of section emitters, each of
which either returns its lines or raises `NotEstablished` where its rule has
not been read.  Two callers need them joined in the listing's own order and
cut at the first refusal: `tools/compare.py`, which measures how far the
converter gets, and `spirv2glasm.py`, the command-line front end.  This is
that join, written once so the two cannot drift apart.

The order is the printer's: the header (profile line, OPTION block, stage
directives, comment header), the `#semantic` block, the `#var` block, the
declarations (ATTRIB/OUTPUT/CBUFFER/STORAGE/TEMP ...), and the body with
`END` and the trailing count comment.

THE FORM.  What this produces is the PLAIN form -- the text the compiler
prints at `--opt-level none` with the default `--debug-info none`, which is
what every saved listing in `listings/` and `corpus_listings/` is (notes/50).
The debug-info forms (g0/g1/g2) are not only annotated: they carry an extra
instruction the plain form does not, and no rule for either is read, so they
are not produced here.  `control_form` gives the fat control section's
spelling of a listing, the relation `tools/checklisting.py` established.
"""
import os
import sys

import glasm

# `control_form` is lex.py's, and tools/checklisting.py (where the relation
# to the fat control section was established) imports the same one.
from lex import control_form                                    # noqa: E402

# (section name, emitter), in the order the printer writes them.
SECTIONS = (
    ("header", glasm.header),
    ("#semantic", glasm.semantic_lines),
    ("#var", glasm.var_lines),
    ("declarations", glasm.declarations),
    ("body", glasm.body),
)


class Refusal(object):
    """Where the converter stopped: the section and the NotEstablished."""

    def __init__(self, section, error):
        self.section = section
        self.error = error

    def __str__(self):
        return "%s: %s" % (self.section, self.error)


def build(module, entry_name="main"):
    """Every line the converter is prepared to claim, in listing order.

    Returns `(lines, refusal)`.  `refusal` is None when the whole listing was
    produced, and otherwise a `Refusal` naming the section whose rule is not
    established -- `lines` is then the established prefix, every line of which
    is the compiler's.

    A gap is signalled by `NotEstablished` and nothing else.  Any other
    exception is the converter crashing, and is re-raised as a RuntimeError
    naming the section, because treating it as a clean stop would hide a bug
    behind an honest-looking prefix.
    """
    lines = []
    for section, step in SECTIONS:
        try:
            part = step(module, entry_name)
        except glasm.NotEstablished as exc:
            return lines, Refusal(section, exc)
        except Exception as exc:                                # noqa: BLE001
            raise RuntimeError("%s raised %s: %s"
                               % (step.__name__, type(exc).__name__, exc))
        lines.extend(part)
    return lines, None


def render(lines, form="plain"):
    """The listing as text in one of the two stored forms.

    plain    the captured text: what the debug-info section stores for a
             `--debug-info none` compile and what `build/spirv2glasm`
             prints.  One line per element, newline-terminated.
    control  the fat control section's spelling: `#` comment lines dropped
             and runs of blanks collapsed to one (tools/checklisting.py).
    """
    text = "\n".join(lines)
    if form == "plain":
        return text + "\n" if lines else ""
    if form == "control":
        text = control_form(text)
        return text + "\n" if text else ""
    raise ValueError("unknown form %r (plain or control)" % form)
