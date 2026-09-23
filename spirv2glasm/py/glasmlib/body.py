"""body.py -- a lowered body: its lines and what the TEMP block needs."""
import lex as _lex


def _is_label(l):
    r"""A subroutine's label, `^BB\d+:$`."""
    return l.endswith(":") and _lex.is_numbered(l[:-1], "BB")


class _Body(object):
    """A lowered body: the instruction lines and what the TEMP block needs."""

    def __init__(self, lines, regs=0, wants=(), dregs=0, lmem=()):
        self.lines = lines
        self.regs = regs
        self.wants = tuple(wants)
        self.dregs = dregs
        self.lmem = tuple(lmem)

    def temp_block(self):
        """The TEMP declarations, in the order `f_7100bdaef0` emits them.

        Surveyed over the 90 probe listings, which show exactly four shapes:
        `TEMP R*; TEMP T;`, `TEMP R*; TEMP T; TEMP RC; SHORT TEMP HC;`,
        `TEMP T;` alone, and `TEMP R*; LONG TEMP D0; TEMP T;`.  So `TEMP T;`
        is unconditional, the register list comes first when there is one, and
        the condition-code pair comes last.

        Read since (notes/71): the printer (0xbdb4f8..0xbdb558) walks the
        register classes as bdcd60(.., 2, "SHORT ", "H") when the target has
        a short class, (.., 3, "", "R"), (.., 4, "LONG ", "D"), so a SHORT
        temp (`0071_lp_wcont.vert`'s continue flag) comes before the R list.
        """
        out = []
        if "H0" in self.wants:
            out.append("SHORT TEMP %s;" % ", ".join(
                w for w in self.wants if _lex.is_numbered(w, "H")))
        if self.regs:
            out.append("TEMP %s;" % ", ".join("R%d" % i
                                              for i in range(self.regs)))
        if "D0" in self.wants:
            # one LONG register per handle loaded (`0083_ps_b.frag`: two
            # texture/sampler pairs, `LONG TEMP D0, D1, D2, D3;`)
            out.append("LONG TEMP %s;" % ", ".join(
                "D%d" % i for i in range(max(self.dregs, 1))))
        out.append("TEMP T;")
        if "CC" in self.wants:
            out.append("TEMP RC;")
            out.append("SHORT TEMP HC;")
        # LOCAL MEMORY LAST: the printer's `TEMP lmem%d[%d];` (0x1160842,
        # 0xbdb718) follows the register classes -- `0071_lm_icb.frag` prints it
        # after `TEMP T;`, `map_0b9eb2ac` after `SHORT TEMP HC;`.
        for k, n in enumerate(self.lmem):
            out.append("TEMP lmem%d[%d];" % (k, n))
        return out

    def trailer(self):
        """`# N instructions, N R-regs[, N D-regs]`.

        `, %d D-regs` is appended only when a LONG register is in play --
        `0030_f03_tex.frag` prints `# 5 instructions, 1 R-regs, 1 D-regs` and every
        probe without a sampler stops after the R count.  A subroutine's
        label is not an instruction (notes/68)."""
        t = "# %d instructions, %d R-regs" % (
            sum(1 for l in self.lines if not _is_label(l)), self.regs)
        if self.dregs:
            t += ", %d D-regs" % self.dregs
        return [t]
