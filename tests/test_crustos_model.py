"""The proved model against the shipped kernel.

RosettaMath's `crustos_eq.py` proves six theorems about a Python model of
CrustOS -- `tick`, `sched`, `scheme_of`, `accepted` -- and exports them to Lean
4, which checks them independently.  Those proofs are about the model.  Nothing
in either repository checks that the model describes `crustos/schemes.py`, and
a theorem about a paraphrase is a theorem about a paraphrase.

So this runs both.  The model side is not re-typed here: `build()` hands back
the same decorated procedures that were compiled to the Calculus of
Constructions and proved, and the comparison evaluates *those terms* through
the kernel's own evaluator.  A copy of the model written out again in this file
would be a third transcription, and then there would be two gaps instead of
one.

The two agreed on nothing in particular when this was written: `scheme_of`
routed a bare "sys" to the sys: scheme in the model and to nothing in the
kernel, because the model had no counterpart to the `if idx <= 0` guard.  That
is fixed.  EXPECTED_DIVERGENCES records any gap that is known and not yet
closed, and a test asserts each one still diverges, so a fix cannot land
silently.

Skipped when RosettaMath is absent, for the same reason `shivyc/proofs.py`
degrades rather than failing: no kernel is a supported configuration.  Run
`make install_proofs` to get one.
"""
import ast
import inspect
import os
import sys
import textwrap
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The scheme layer under test, imported from the tree rather than a copy.
sys.path.insert(0, os.path.join(ROOT, "crustos"))
import schemes  # noqa: E402


def _rosettamath():
    """Where crustos_eq.py lives, by the same search proofs.py uses."""
    for path in (os.environ.get("ROSETTAMATH_DIR"),
                 os.path.join(ROOT, "..", "RosettaMath"),
                 os.path.expanduser("~/RosettaMath")):
        if path and os.path.isfile(os.path.join(path, "crustos_eq.py")):
            return os.path.abspath(path)
    return None


ROSETTAMATH = _rosettamath()

# The model is a fold over lists of unary numerals, so normalising a call on a
# seven-name table goes thousands of frames deep.  crustos_eq.py raises both
# limits the same way for the same reason.
STACK_BYTES = 512 * 1024 * 1024
RECURSION = 300000


def _in_big_stack(work):
    """Run `work` on a thread with a stack the evaluator fits in."""
    out, err = [], []

    def target():
        sys.setrecursionlimit(RECURSION)
        try:
            out.append(work())
        except BaseException as exc:            # carried across, not swallowed
            err.append(exc)

    threading.stack_size(STACK_BYTES)
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if err:
        raise err[0]
    return out[0]


# Built once: build() runs every proof in the supplement and takes about a
# second, which is too long to pay per test method.
_MODEL = {}


def _load_model():
    if _MODEL:
        return _MODEL
    sys.path.insert(0, ROSETTAMATH)

    def work():
        import crustos_eq
        import hoare
        import lean4
        env, _facts, model = crustos_eq.build()
        return env, model, hoare, lean4

    env, model, hoare, lean4 = _in_big_stack(work)
    _MODEL.update(env=env, model=model, hoare=hoare, lean4=lean4)
    return _MODEL


def _nat(term, env, lean4):
    """Read a normalised Nat back as a Python int, or None if it is not one."""
    count = 0
    current = lean4.normalize(term, env)
    while isinstance(current, lean4.App):
        head, args = lean4.spine(current)
        if not (isinstance(head, lean4.Var) and head.name == "succ"):
            return None
        count += 1
        current = lean4.normalize(args[0], env)
    if isinstance(current, lean4.Var) and current.name == "zero":
        return count
    return None


def _nat_list(term, env, lean4):
    """Read a normalised List Nat back as a list of ints."""
    out = []
    current = lean4.normalize(term, env)
    while True:
        head, args = lean4.spine(current)
        if not isinstance(head, lean4.Var):
            return None
        if head.name == "nil":
            return out
        if head.name != "cons":
            return None
        out.append(_nat(args[1], env, lean4))
        current = lean4.normalize(args[2], env)


# The model has no negative numbers -- it is over Nat -- so "no scheme" is
# encoded as len(names) where the kernel returns SCHEME_NONE.  Decoding here
# rather than in the comparison keeps the two sentinels from being conflated by
# accident: a model result of len(names) means exactly SCHEME_NONE and nothing
# else.
def _decode_scheme(value, table_size):
    if value is None:
        return None
    return schemes.SCHEME_NONE if value == table_size else value


# URLs chosen to exercise the parts the two implementations treat differently:
# a present and absent separator, a leading separator, a name that is and is
# not registered, and an empty string.
CORPUS = [
    "sys:/context",
    "memory:/free",
    "gpu:0",
    "debug:",
    "nope:/x",
    ":sys",
    "",
    "sys",             # no separator, but names a registered scheme
    "gpu",
    "nope",
    "sys:extra:colons",
    "SYS:/context",    # case matters
]

# Where the two are known to disagree, with why.  A test asserts that each
# entry still diverges, so closing one turns this file red and the fix has to
# say so rather than passing unnoticed.
#
# Empty as of the separator-guard fix in crustos_eq.py.  It was:
#
#   "sys": no separator, kernel guards with `if idx <= 0`, model had no guard
#   "gpu": the same
#
# which is the whole shape of that bug: without the guard the model took the
# entire URL as the scheme name, so it went wrong exactly when that whole
# string was in the table.  "nope" has no separator either and agreed anyway,
# because an unregistered name misses the table by both routes -- agreement by
# luck, which is why it sits in CORPUS and never belonged here.
EXPECTED_DIVERGENCES = {}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestSchemeOfAgrees(unittest.TestCase):
    """`scheme_of`, as shipped and as proved."""

    @classmethod
    def setUpClass(cls):
        loaded = _load_model()
        cls.env = loaded["env"]
        cls.hoare = loaded["hoare"]
        cls.lean4 = loaded["lean4"]
        cls.proc = loaded["model"]["scheme_of"].lean_procedure
        cls.names = schemes._NAMES

    def model_scheme_of(self, url):
        hoare, lean4, env = self.hoare, self.lean4, self.env
        term = hoare.app(self.proc.fn_term,
                         hoare.texts(self.names), hoare.text(url))
        raw = _in_big_stack(lambda: _nat(term, env, lean4))
        return _decode_scheme(raw, len(self.names))

    def test_model_evaluates_to_a_number(self):
        """Every URL in the corpus normalises to a readable Nat.

        A None here is not a disagreement, it is the harness failing to read
        the kernel's answer, and the two must not be reported as the same
        thing.
        """
        for url in CORPUS:
            with self.subTest(url=url):
                self.assertIsNotNone(self.model_scheme_of(url),
                                     "model did not normalise to a numeral")

    def test_agrees_where_it_is_supposed_to(self):
        for url in CORPUS:
            if url in EXPECTED_DIVERGENCES:
                continue
            with self.subTest(url=url):
                self.assertEqual(
                    self.model_scheme_of(url), schemes.scheme_of(url),
                    "the proved model and crustos/schemes.py disagree about "
                    "%r. One of them is wrong and it is not safe to guess "
                    "which." % url)

    def test_known_divergences_still_diverge(self):
        """The recorded gaps, so that closing one cannot pass unnoticed."""
        for url, why in EXPECTED_DIVERGENCES.items():
            with self.subTest(url=url):
                self.assertNotEqual(
                    self.model_scheme_of(url), schemes.scheme_of(url),
                    "%r now agrees (%s) -- good: remove it from "
                    "EXPECTED_DIVERGENCES." % (url, why))


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestAcceptedAgrees(unittest.TestCase):
    """`accepted`: the indices of URLs that name a registered scheme.

    This is the function `accepted_bounded` is a theorem about, so a gap here
    is a gap under the one proof most likely to be quoted.
    """

    BATCHES = [
        "sys:/context,nope:/x,gpu:0",
        "nope:/x",
        "sys:/a,memory:/b",
        "",
        "sys:/a,,gpu:1",
    ]

    @classmethod
    def setUpClass(cls):
        loaded = _load_model()
        cls.env = loaded["env"]
        cls.hoare = loaded["hoare"]
        cls.lean4 = loaded["lean4"]
        cls.proc = loaded["model"]["accepted"].lean_procedure
        cls.names = schemes._NAMES

    def model_accepted(self, urls):
        hoare, lean4, env = self.hoare, self.lean4, self.env
        term = hoare.app(self.proc.fn_term,
                         hoare.texts(self.names), hoare.text(urls))
        return _in_big_stack(lambda: _nat_list(term, env, lean4))

    def test_bound_holds_of_the_shipped_code(self):
        """`accepted_bounded` says len(result) <= len(split(urls, ',')).

        Proved of the model; asserted here of the code, which is where the
        claim would actually be relied on.
        """
        for urls in self.BATCHES:
            with self.subTest(urls=urls):
                self.assertLessEqual(len(schemes.accepted(urls)),
                                     len(urls.split(",")))

    def test_agrees_with_the_model(self):
        for urls in self.BATCHES:
            if any(part in EXPECTED_DIVERGENCES for part in urls.split(",")):
                continue
            with self.subTest(urls=urls):
                self.assertEqual(
                    self.model_accepted(urls), schemes.accepted(urls),
                    "the proved model and crustos/schemes.py disagree about "
                    "the batch %r" % urls)


def _skeleton(body):
    """Control-flow shape: statement kinds and nesting, with names dropped.

    Behavioural agreement over a corpus says the two compute the same thing on
    the cases someone thought of.  This says something narrower and harder to
    fake: that they are the same function written twice.  A corpus can miss an
    input; a shape cannot miss a branch.

    Docstrings go, and so do `assert invariant(...)` / `assert variant(...)`,
    which are addressed to the prover rather than the machine and have no
    counterpart in the kernel.  `i += 1` and `i = i + 1` are one thing.
    """
    out = []
    for stmt in body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        if isinstance(stmt, ast.Assert):
            continue
        if isinstance(stmt, ast.If):
            out.append(("If", tuple(_skeleton(stmt.body)),
                        tuple(_skeleton(stmt.orelse))))
        elif isinstance(stmt, (ast.While, ast.For)):
            out.append((type(stmt).__name__, tuple(_skeleton(stmt.body))))
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            out.append("Assign")
        else:
            out.append(type(stmt).__name__)
    return out


def _body_of(source):
    return ast.parse(textwrap.dedent(source)).body[0].body


# Structural gaps that remain, with the reason each one is still open.  Same
# rule as EXPECTED_DIVERGENCES: a test asserts each still differs, so closing
# one cannot pass unnoticed.
#
# Empty as of desugar_for and desugar_append in hoare.py.  It was:
#
#   "accepted": the kernel writes `for url in urls.split(',')` and
#               `out.append(i)`; hoare.py had neither
#
# and closing it took no change to the model's claim, only to what the
# lowering would read: a `for` over a list is now the `while` it always was,
# with the variant supplied, and `.append` is `snoc` written the way Python
# writes it.
EXPECTED_SHAPE_GAPS = {}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestModelShape(unittest.TestCase):
    """The model and the kernel as text, not as behaviour."""

    @classmethod
    def setUpClass(cls):
        _load_model()
        sys.path.insert(0, ROSETTAMATH)
        import crustos_eq
        cls.sources = crustos_eq.SOURCES

    def shapes(self, name):
        real = getattr(schemes, name)
        return (_skeleton(_body_of(inspect.getsource(real))),
                _skeleton(_body_of(self.sources[name])))

    def test_modelled_functions_are_the_same_function_twice(self):
        for name in sorted(MODELLED - set(EXPECTED_SHAPE_GAPS)):
            with self.subTest(name=name):
                real, model = self.shapes(name)
                self.assertEqual(real, model,
                                 "%s no longer has the shape of the shipped "
                                 "function; the model has drifted back into "
                                 "being a paraphrase" % name)

    def test_known_shape_gaps_are_still_open(self):
        for name, why in EXPECTED_SHAPE_GAPS.items():
            with self.subTest(name=name):
                real, model = self.shapes(name)
                self.assertNotEqual(
                    real, model,
                    "%s now matches (%s) -- good: remove it from "
                    "EXPECTED_SHAPE_GAPS." % (name, why))


# Everything the shipped scheme layer exports, and whether the proved model
# says anything about it.  A new function in crustos/schemes.py lands here as
# a failure rather than as silence, which is the only way an unmodelled
# addition gets noticed.
MODELLED = {"scheme_of", "accepted"}

UNMODELLED = {
    "scheme_count": "returns a constant; nothing to state",
    "scheme_name": "indexes the table; a bounds fact, not a routing one",
    "path_of": "returns the tail of a URL, and the model has no claim about "
               "paths yet",
    "route_all": "one scheme id per URL rather than the accepted indices; "
                 "close relative of `accepted`, not yet modelled",
    "describe": "formats a string with %, which hoare.py has no lowering for",
}


@unittest.skipUnless(ROSETTAMATH, "RosettaMath not found; run 'make install_proofs'")
class TestModelCoverage(unittest.TestCase):
    """How much of the scheme layer the proofs reach.

    `kernel.c` is deliberately absent from all of this.  It is Rust and C, so
    there is no source for hoare.py to read, and the modelled `Context`
    (`current`, `nthreads`, `ticks`) has no counterpart in the kernel's
    (`pid`, `prio`, `ticks`, `frames`, `state`, `entry`, `stack`, `reg_class`,
    `switches`) -- its nearest real analogue is `used <= 16` on `Kernel`.  The
    `tick` and `sched` theorems are therefore about a model of nothing that
    ships, and that is worth writing down rather than leaving to be inferred
    from the absence of a test.
    """

    def test_every_exported_function_is_accounted_for(self):
        exported = {name for name, value in vars(schemes).items()
                    if callable(value) and not name.startswith("_")
                    and getattr(value, "__module__", None) == schemes.__name__}
        unaccounted = exported - MODELLED - set(UNMODELLED)
        self.assertEqual(
            unaccounted, set(),
            "crustos/schemes.py exports %s, which is neither modelled nor "
            "listed in UNMODELLED. Add a model or record why there is not "
            "one." % sorted(unaccounted))

    def test_the_modelled_set_is_real(self):
        for name in MODELLED:
            with self.subTest(name=name):
                self.assertTrue(hasattr(schemes, name),
                                "%s is claimed as modelled but the kernel no "
                                "longer exports it" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
