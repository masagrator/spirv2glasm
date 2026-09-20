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
        c = Lowering(module, entry_name).run()
        try:
            module._g2s_body = c
        except AttributeError:
            pass
    return c
