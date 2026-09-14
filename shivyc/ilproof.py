"""Lift a function's IL into the fragment `hoare.py` proves things about.

`shivyc/proofs.py` certifies contracts, and RosettaMath's `crustos_eq.py`
proves theorems about a hand-written model of the kernel.  Between the two
there was nothing: the model had to be written by a person reading the
source, and `crustos/kernel.c` -- Rust and C -- had no Python source for
`hoare.py` to read at all.

This is the seam.  It takes a function *after* the front end has done its
work -- the IL that every Crust front end produces, C or Rust or rpython --
rebuilds the structured control flow the front end flattened, and writes it
back out as Python in `hoare.py`'s dialect: the same dialect the model
authors write by hand, so the same `read_procedure` compiles it and the same
proof helpers apply.

    from shivyc.ilproof import lift
    source = lift(il_code, symbol_table, 'sum_to')
    proc = hoare.read_procedure(source, env, ensures=[...])

What is claimed, and what is not, matters:

  * The lift is over Nat.  C `int` wraps at 2**32 and Nat does not;
    subtraction here is truncated where C's goes negative.  A proof about
    the lifted function is a proof about that model of the arithmetic, and
    it says nothing about overflow.  This is the same kind of distance as
    `SCHEME_NONE` being `len(names)` in the scheme-layer model, and the same
    remedy applies: state it, and test the model against the binary.

  * Only structured control flow is lifted.  A loop is recognised as
    `while` when it has one entry, one exit, and one back edge; an `if` is
    recognised by its diamond.  Anything else -- `goto`, `break` out of the
    middle, `switch` -- is refused with a message rather than approximated.

  * A loop needs an invariant and a variant, and C states neither.  For the
    counted idiom -- `i < n` at the head, `i = i + 1` once in the body, `n`
    unchanged -- both are derived: variant `n - i`, invariant `i <= n`, which
    is what a person would write.  `invariants`, keyed by loop variable,
    conjoins a stronger invariant onto that one, for a postcondition that
    needs more than the bound.  Any other loop shape is refused.

  * Integers only.  Pointers, arrays, structs and calls are refused.  The
    scheme layer needs lists and the kernel needs records, and neither is
    here yet; this is the arithmetic core, done carefully, so that what is
    built on it is built on something checked.

`tests/test_ilproof.py` compiles each example with the real compiler, runs
the binary, and checks the lifted model against it through the kernel's
evaluator -- the same discipline `test_crustos_model.py` applies to the
scheme layer.
"""
from shivyc.il_cmds import control, compare, math as ilmath, value


class LiftError(Exception):
    """Something in the IL has no lifting.  Says what, rather than guessing."""


# Binary operators, and how the fragment spells them.
_ARITH = {
    ilmath.Add: "+",
    ilmath.Subtr: "-",
    ilmath.Mult: "*",
    # Nat division floors and `divb n 0` is 0, which is what C leaves
    # undefined; the model says 0 where the machine says nothing.  The
    # same distance as truncated subtraction, and stated for the same
    # reason.
    ilmath.Div: "//",
    ilmath.Mod: "%",
}
_COMPARE = {
    compare.LessCmp: "<",
    compare.LessOrEqCmp: "<=",
    compare.GreaterCmp: ">",
    compare.GreaterOrEqCmp: ">=",
    compare.EqualCmp: "==",
    compare.NotEqualCmp: "!=",
}


class _Names:
    """Readable names for IL values: the C name when there is one."""

    def __init__(self, symbol_table):
        self.table = symbol_table
        self.temps = {}

    def of(self, ilvalue):
        if ilvalue.literal is not None:
            return str(ilvalue.literal.val)
        name = self.table.names.get(ilvalue)
        if name is not None:
            return name
        if ilvalue not in self.temps:
            self.temps[ilvalue] = "_t%d" % (len(self.temps) + 1)
        return self.temps[ilvalue]

    def is_named(self, ilvalue):
        return ilvalue.literal is None and ilvalue in self.table.names


def _blocks(cmds):
    """Split into basic blocks.  Each is (label or None, [cmds], terminator)."""
    blocks, current, label = [], [], None
    for cmd in cmds:
        if isinstance(cmd, control.Label):
            if current or label is not None:
                blocks.append((label, current, None))
            label, current = cmd.label, []
            continue
        if isinstance(cmd, (control.Jump, control.JumpZero,
                            control.JumpNotZero, control.Return)):
            blocks.append((label, current, cmd))
            label, current = None, []
            continue
        current.append(cmd)
    if current or label is not None:
        blocks.append((label, current, None))
    return blocks


class _Lifter:
    def __init__(self, cmds, names, invariants):
        self.blocks = _blocks(cmds)
        self.names = names
        self.invariants = invariants or {}
        self.by_label = {lab: i for i, (lab, _, _) in enumerate(self.blocks)
                         if lab is not None}
        # IL temporaries are single-assignment expression trees; inline them.
        self.exprs = {}

    # -- expressions --------------------------------------------------------

    def expr(self, ilvalue):
        if ilvalue in self.exprs:
            return self.exprs[ilvalue]
        return self.names.of(ilvalue)

    def straight(self, cmds, out):
        """Lift a run of non-control commands into statements."""
        for cmd in cmds:
            kind = type(cmd)
            if kind is value.LoadArg:
                continue                          # parameters are bound
            if kind is value.Set:
                rhs = self.expr(cmd.arg)
                if self.names.is_named(cmd.output):
                    out.append("%s = %s" % (self.names.of(cmd.output), rhs))
                else:
                    self.exprs[cmd.output] = rhs
                continue
            if kind in _ARITH:
                text = "(%s %s %s)" % (self.expr(cmd.arg1), _ARITH[kind],
                                       self.expr(cmd.arg2))
                self.record(cmd.output, text, out)
                continue
            if kind is ilmath.RBitShift:
                # On a non-negative value `x >> k` is exactly `x / 2**k`, so
                # this one is faithful rather than a model: no truncation and
                # no wrap, because a right shift cannot overflow.  The shift
                # amount has to be a literal for `2**k` to be a numeral; a
                # computed one is refused rather than approximated.
                if cmd.arg2.literal is None:
                    raise LiftError(
                        "a right shift by a computed amount has no lifting "
                        "yet: the shift must be a literal, so that the "
                        "divisor is a numeral")
                shift = int(cmd.arg2.literal.val)
                text = "(%s // %d)" % (self.expr(cmd.arg1), 2 ** shift)
                self.record(cmd.output, text, out)
                continue
            if kind in _COMPARE:
                text = "(%s %s %s)" % (self.expr(cmd.arg1), _COMPARE[kind],
                                       self.expr(cmd.arg2))
                self.record(cmd.output, text, out)
                continue
            raise LiftError("%s has no lifting yet: only integer arithmetic "
                            "and comparison are lifted" % kind.__name__)

    def record(self, ilvalue, text, out):
        if self.names.is_named(ilvalue):
            out.append("%s = %s" % (self.names.of(ilvalue), text))
        else:
            self.exprs[ilvalue] = text

    # -- control flow -------------------------------------------------------

    def lift(self):
        body = []
        self.region(0, len(self.blocks), body, indent=0, join=None)
        return body

    def region(self, start, stop, out, indent, join):
        """Lift blocks[start:stop] as one structured sequence.

        `join` is the label a Jump may target to end this region normally:
        the loop head for a loop body, the if's end label for a then-branch.
        Any other unconditional jump is a goto, break or continue, and those
        are refused rather than approximated.
        """
        pad = "    " * indent
        i = start
        while i < stop:
            label, cmds, term = self.blocks[i]
            if self.is_loop_head(i):
                i = self.loop(i, out, indent)
                continue
            lines = []
            self.straight(cmds, lines)
            out.extend(pad + line for line in lines)
            if term is None:
                i += 1
            elif isinstance(term, control.Return):
                if term.arg is None:
                    raise LiftError("a void function returns nothing there "
                                    "is a claim to make about")
                out.append(pad + "return " + self.expr(term.arg))
                i += 1
            elif isinstance(term, (control.JumpZero, control.JumpNotZero)):
                i = self.branch(i, stop, out, indent)
            elif isinstance(term, control.Jump):
                if join is not None and term.label == join and i == stop - 1:
                    return
                raise LiftError("an unconditional jump that is neither a "
                                "loop's back edge nor an if's join: goto, "
                                "break or continue, none of which is lifted")
            else:
                raise LiftError("unrecognised terminator %s"
                                % type(term).__name__)

    def is_loop_head(self, i):
        """A block is a loop head if a later block jumps back to it."""
        label = self.blocks[i][0]
        return label is not None and self.back_edge_to(i) is not None

    def back_edge_to(self, i):
        label = self.blocks[i][0]
        for j in range(i + 1, len(self.blocks)):
            term = self.blocks[j][2]
            if isinstance(term, control.Jump) and term.label == label:
                return j
        return None

    def loop(self, head, out, indent):
        r"""
            Label head            while cond:
              cond                    body
              JumpZero cond exit      step
              body              Label exit
              [Label cont]
              step
              Jump head
            Label exit
        """
        pad = "    " * indent
        label, cmds, term = self.blocks[head]
        if not isinstance(term, control.JumpZero):
            raise LiftError("a loop whose head does not test and exit is not "
                            "a while loop")
        back = self.back_edge_to(head)
        exit_at = self.by_label.get(term.label)
        if exit_at != back + 1:
            raise LiftError("a loop whose exit is not the block after its "
                            "back edge: a break or goto out of the middle")

        scratch = []
        self.straight(cmds, scratch)
        if scratch:
            raise LiftError("a loop condition that assigns a named variable "
                            "is not lifted")
        cond = self.expr(term.cond)
        cond_cmd = self.cond_of(cmds, term.cond)

        body_cmds = [c for j in range(head + 1, back + 1)
                     for c in self.blocks[j][1]]
        out.append(pad + "while %s:" % cond)
        for marker in self.annotations(cond_cmd, body_cmds):
            out.append(pad + "    " + marker)
        self.region(head + 1, back + 1, out, indent + 1, join=label)
        return back + 1

    def cond_of(self, cmds, ilvalue):
        for cmd in cmds:
            if getattr(cmd, "output", None) is ilvalue:
                return cmd
        return None

    def annotations(self, cond_cmd, body_cmds):
        """`assert invariant(...)` and `assert variant(...)` for the loop.

        C states neither.  For the counted idiom they are derived; any other
        loop needs one supplied, keyed by the loop's head label.
        """
        if isinstance(cond_cmd, compare.LessCmp):
            i, n = cond_cmd.arg1, cond_cmd.arg2
            if self.names.is_named(i) and self.counts_up(i, n, body_cmds):
                iname, nname = self.names.of(i), self.expr(n)
                extra = self.invariants.get(iname)
                inv = "%s <= %s" % (iname, nname)
                if extra:
                    inv = "%s and %s" % (inv, extra)
                return ["assert invariant(%s)" % inv,
                        "assert variant(%s - %s)" % (nname, iname)]
        raise LiftError("this loop is not the counted idiom -- `i < n` at "
                        "the head, `i = i + 1` once in the body, `n` never "
                        "assigned -- and no invariant was supplied for it")

    def counts_up(self, i, n, body_cmds):
        """Is `i` set from `i + 1` exactly once, and `n` never assigned?"""
        bumps = 0
        for cmd in body_cmds:
            outv = getattr(cmd, "output", None)
            if outv is n:
                return False
            if outv is not i:
                continue
            if not isinstance(cmd, value.Set):
                return False
            source = cmd.arg
            feeder = next((c for c in body_cmds
                           if getattr(c, "output", None) is source), None)
            if (isinstance(feeder, ilmath.Add) and feeder.arg1 is i
                    and feeder.arg2.literal is not None
                    and feeder.arg2.literal.val == 1):
                bumps += 1
            else:
                return False
        return bumps == 1

    def branch(self, i, stop, out, indent):
        r"""
            cond                       cond
            JumpZero cond else         JumpZero cond end
            then                       then
            Jump end                   Label end
            Label else
            else
            Label end
        """
        pad = "    " * indent
        label, cmds, term = self.blocks[i]
        cond = self.expr(term.cond)
        if isinstance(term, control.JumpNotZero):
            cond = "not " + cond
        else_at = self.by_label.get(term.label)
        if else_at is None or else_at <= i:
            raise LiftError("a conditional jump backwards: a loop with its "
                            "test at the bottom, which is not lifted")
        # Does the then-branch end by jumping over an else-branch?
        last = self.blocks[else_at - 1][2]
        join_at = None
        if isinstance(last, control.Jump):
            join_at = self.by_label.get(last.label)
            if join_at is None or join_at <= else_at:
                raise LiftError("a jump out of a then-branch that is not to "
                                "the if's end")
        out.append(pad + "if %s:" % cond)
        then_lines = []
        self.region(i + 1, else_at, then_lines, indent + 1,
                    join=last.label if join_at else None)
        if not then_lines:
            then_lines.append("    " * (indent + 1) + "pass")
        out.extend(then_lines)
        if join_at is None:
            return else_at
        out.append(pad + "else:")
        else_lines = []
        self.region(else_at, join_at, else_lines, indent + 1, join=None)
        if not else_lines:
            else_lines.append("    " * (indent + 1) + "pass")
        out.extend(else_lines)
        return join_at


def lift(il_code, symbol_table, function, invariants=None, ensures=None):
    """The function, as Python source in hoare.py's dialect."""
    cmds = il_code.commands[function]
    names = _Names(symbol_table)

    params = []
    for cmd in cmds:
        if isinstance(cmd, value.LoadArg):
            params.append("%s: 'Nat'" % names.of(cmd.output))
    lifter = _Lifter(cmds, names, invariants)
    body = lifter.lift()
    lines = ["def %s(%s) -> 'Nat':" % (function, ", ".join(params))]
    lines += ["    " + line for line in body]
    return "\n".join(lines) + "\n"
