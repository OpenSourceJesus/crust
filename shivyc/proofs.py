"""One reading of a contract, checked by a proof kernel when asked.

`extensions.py` parses a contract into `{'len>=': 64, 'div-by': 4}` and hands
it downstream, where each pass decides for itself what the dict means.  Two
passes read it two ways:

    contracts._violates    returns on the first key it finds
    simd_contracts._satisfies   conjoins every key

For `{'len>=': 64, 'div-by': 4}` -- the contract in `SIMD_CONTRACTS.md` -- a
length of 70 clears `len>=`, so `_violates` never looks at the `div-by` and the
call compiles, while `simd_contracts` correctly refuses to prove it and keeps
the scalar tail.  One contract, two answers, and the one that reports errors
was the lenient one.

So the reading lives here, once, and both passes ask it.  A contract holds when
*every* clause holds; a clause nobody can read is an error rather than
something to skip past.

Beyond agreeing with itself, the reading can be *checked*.  RosettaMath's
`lean4.py` is a Calculus of Constructions kernel, and `crustproof.py` turns a
contract into a term of that calculus: `{'len>=': 64, 'div-by': 4}` becomes
`andb (dvdb 4 n) (leb 64 n)`, which at a known length reduces to `true` or
`false` under the kernel's own evaluator, and when it reduces to `true` a proof
term is built and type-checked.  That is off by default and costs nothing when
off -- see `certified()` for why, and for what it costs when on.
"""

import os

BOUNDS = ("len>=", "len<=", "div-by")

# Reading a bound: does this length satisfy this clause?
_CLAUSES = {
    "len>=": lambda k, n: n >= k,
    "len<=": lambda k, n: n <= k,
    "div-by": lambda k, n: (n % k) == 0,
}


class UnknownBound(Exception):
    """A contract clause with no reading.

    Raised rather than skipped.  A clause quietly dropped is a contract that
    quietly does not hold, which is the failure this module exists to remove.
    """


def failing(length, bounds):
    """Every clause this length breaks, in the order they are written."""
    unknown = set(bounds) - set(_CLAUSES)
    if unknown:
        raise UnknownBound(
            "no reading for contract clause(s) %s; this compiler knows %s"
            % (", ".join(sorted(unknown)), ", ".join(BOUNDS)))
    return [key for key in BOUNDS
            if key in bounds and not _CLAUSES[key](bounds[key], length)]


def satisfies(length, bounds):
    """Does a known length meet the whole contract?"""
    return not failing(length, bounds)


def violates(length, bounds):
    """Does it break any clause of the contract?"""
    return bool(failing(length, bounds))


def clause_text(arg_name, key, bounds):
    """The clause as it was written in the source, for a diagnostic."""
    if key == "len<=":
        return "len(%s) <= %d" % (arg_name, bounds[key]), "is too large"
    if key == "len>=":
        return "len(%s) >= %d" % (arg_name, bounds[key]), "is too small"
    if key == "div-by":
        return ("not len(%s) %% %d" % (arg_name, bounds[key]),
                "has a length that violates the contract")
    raise UnknownBound("no text for contract clause %r" % (key,))


# ------------------------------------------------------------ certification

_KERNEL = None
_TRIED = False

#: Above this length the kernel is not consulted.  Its numerals are unary, so
#: settling a contract at length n walks n of them, and the cost grows with the
#: length rather than with the size of the number: about a second at 64 and
#: half a minute at 1024.  That is fine for a proof and wrong for a compiler,
#: so the budget keeps it to lengths where the answer arrives promptly.  The
#: reading above does not change either way; only whether it comes with a proof.
MAX_CERTIFIED = int(os.environ.get("CRUST_PROOF_MAX", "128"))


def _kernel():
    """RosettaMath's proof bridge, if it is available and wanted."""
    global _KERNEL, _TRIED
    if _TRIED:
        return _KERNEL
    _TRIED = True
    if os.environ.get("CRUST_PROOFS", "0") not in ("1", "true", "yes"):
        return None
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.environ.get("ROSETTAMATH_DIR"),
                 os.path.join(here, "..", "..", "RosettaMath"),
                 os.path.expanduser("~/RosettaMath")):
        if path and os.path.isfile(os.path.join(path, "crustproof.py")):
            full = os.path.abspath(path)
            if full not in sys.path:
                sys.path.insert(0, full)
            try:
                import crustproof
                _KERNEL = crustproof
            except ImportError:
                _KERNEL = None
            break
    return _KERNEL


def certified(length, bounds):
    """A kernel-checked certificate for this contract, or None.

    None means the kernel was not asked -- because certification is off, or
    RosettaMath is not present, or the length is past `MAX_CERTIFIED`.  It
    never means the contract failed: that is what `satisfies` is for.  When a
    certificate does come back its verdict is checked against the reading
    above, and a disagreement is raised rather than resolved, since exactly one
    of the two would then be wrong and this module cannot tell which.
    """
    kernel = _kernel()
    if kernel is None or length > MAX_CERTIFIED:
        return None
    if any(bounds.get(key, 0) > MAX_CERTIFIED for key in bounds):
        return None
    try:
        certificate = kernel.check(length, bounds)
    except Exception:                    # a bridge fault is not a compile error
        return None
    if certificate.holds != satisfies(length, bounds):
        raise AssertionError(
            "the proof kernel and this compiler disagree about %r at length "
            "%d: kernel says %s. One of them is wrong and it is not safe to "
            "guess which." % (bounds, length, certificate.holds))
    return certificate


def backend():
    """What settled the last question: for a report line."""
    return "lean4.py" if _kernel() is not None else "built-in"
