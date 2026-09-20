"""glasm.py -- emit the parts of a GLASM listing whose rule is established.

The public face of the converter's output side.  Each function returns one
section of the listing, or raises NotEstablished where the rule that would
produce it has not been read out of the compiler; the work is done in
`glasmlib/` (see its package docstring for the module map).
"""
from glasmlib.common import NotEstablished
from glasmlib.header import header, module_options, option_block, \
    stage_directives, comment_header
from glasmlib.types import type_spelling
from glasmlib.varblock import semantic_lines, var_lines
from glasmlib.declare import interface_bindings, attrib_block, \
    colour_output_lines, declarations
from glasmlib.lower import lower

__all__ = [
    "NotEstablished", "header", "module_options", "option_block",
    "stage_directives", "comment_header", "type_spelling", "semantic_lines",
    "var_lines", "interface_bindings", "attrib_block", "colour_output_lines",
    "declarations", "body",
]


def body(module, entry_name="main"):
    """The instruction lines, `END`, and the trailing count comment."""
    b = lower(module, entry_name)
    return b.lines + ["END"] + b.trailer()
