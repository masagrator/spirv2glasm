"""lower -- the entry point's body, lowered to GLASM lines.

One class per instruction family, each a mixin over `Core` (core.py), which
holds the state and runs the arms in `Core.ARMS` order:

  memory.py     loads, parameters, variables, access chains
  stores.py     stores to locals and to stage outputs
  arith.py      arithmetic, divides, shifts, dots, conversions, bitcasts
  image.py      sampled images and texture instructions
  composite.py  extracts, constructs, shuffles
  extinst.py    GLSL.std.450 builtins
  control.py    structured control flow, selects, kills, calls, returns
  finish.py     scheduling, allocation and the finished body
"""
from glasmlib.lower.core import Core
from glasmlib.lower.memory import MemoryOps
from glasmlib.lower.stores import StoreOps
from glasmlib.lower.arith import ArithOps
from glasmlib.lower.image import ImageOps
from glasmlib.lower.composite import CompositeOps
from glasmlib.lower.extinst import ExtInstOps
from glasmlib.lower.control import ControlOps
from glasmlib.lower.finish import Finish


class Lowering(MemoryOps, StoreOps, ArithOps, ImageOps, CompositeOps,
               ExtInstOps, ControlOps, Finish, Core):
    """The whole lowering: `Lowering(module, entry).run()` is the body."""


def lower(module, entry_name="main"):
    """Lower the entry point's body, or raise NotEstablished.

    Memoised on the module: `declarations` needs the register count for the
    TEMP block and `body` needs the lines, and lowering twice would be both
    slower and a chance for the two to disagree.
    """
    c = getattr(module, "_g2s_body", None)
    if c is None:
        # TWO PASSES, and only when the first one finds the case (notes/114
        # §41).  A static load that is loaded AGAIN IN ITS OWN BLOCK keeps
        # its register and the construct that read it first copies out of
        # it.  Whether the repeat lands in the same block cannot be known
        # at the first load -- a store between them opens a new block and
        # then the compiler simply loads twice -- so the first pass finds
        # out and the second acts on it.  The lowering is deterministic, so
        # the answer carries; it is recorded against the SPIR-V id of the
        # first load, which a second pass numbers the same way, and not
        # against a register name, which it does not.
        _first = Lowering(module, entry_name)
        c = _first.run()
        if _first.shared_first:
            c = Lowering(module, entry_name,
                         shared_before=_first.shared_first).run()
        try:
            module._g2s_body = c
        except AttributeError:
            pass
    return c
