"""Tests for the multiple-include optimization.

A header wholly wrapped in an include guard is not lexed again once its
guard macro is defined (as GCC does). These tests pin the cases where
skipping would be *wrong* -- the guard undefined again, content outside the
guard, a top-level #else -- plus the guard detector itself.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_has_feature import _run          # noqa: E402
import shivyc.lexer as lexer                      # noqa: E402
import shivyc.preproc as preproc                  # noqa: E402


def _guard_of(text):
    toks = lexer.tokenize(text, "t.h")
    P = preproc._Preprocessor
    return P._include_guard(P._group_lines(toks))


class GuardDetectionTests(unittest.TestCase):

    def test_classic_forms(self):
        self.assertEqual(_guard_of("#ifndef G_H\n#define G_H\nint x;\n#endif\n"),
                         "G_H")
        self.assertEqual(_guard_of("/* c */\n#if !defined(G_H)\n#define G_H\n"
                                   "#endif /* G_H */\n"), "G_H")
        self.assertEqual(_guard_of("#if !defined G_H\n#define G_H\n#endif\n"),
                         "G_H")

    def test_nested_conditionals_inside_are_fine(self):
        self.assertEqual(_guard_of("#ifndef G\n#define G\n#ifdef A\n#else\n"
                                   "#endif\n#if B\n#elif C\n#endif\n#endif\n"),
                         "G")

    def test_not_a_guard(self):
        for text in [
            "int before;\n#ifndef G\n#define G\n#endif\n",       # before
            "#ifndef G\n#define G\n#endif\nint after;\n",        # after
            "#ifndef G\n#define G\n#else\nint x;\n#endif\n",     # #else
            "#ifndef G\n#define G\n#elif X\n#endif\n",           # #elif
            "#ifdef G\n#endif\n",                                # ifdef
            "#if !defined(G) && X\n#endif\n",                    # compound
            "#ifndef G\n#define G\n",                            # unterminated
            "#ifndef G\n#endif\n#ifndef H\n#endif\n",            # two blocks
            "",
        ]:
            self.assertIsNone(_guard_of(text), text)


class SkipCorrectnessTests(unittest.TestCase):
    """Each program would fail to compile, or return the wrong value, if a
    re-inclusion were skipped when it must not be."""

    def test_guarded_header_twice_is_once(self):
        rc, log = _run('#include "g.h"\n#include "g.h"\n'
                       'int main(void){return VAL;}\n',
                       files={"g.h": "#ifndef G\n#define G\n#define VAL 5\n"
                                     "int once_only;\n#endif\n"})
        self.assertEqual(rc, 5, log)

    def test_guard_undefined_again_reincludes(self):
        rc, log = _run('#include "g.h"\n#undef G\n#undef VAL\n'
                       '#include "g.h"\nint main(void){return VAL;}\n',
                       files={"g.h": "#ifndef G\n#define G\n#define VAL 6\n"
                                     "#endif\n"})
        self.assertEqual(rc, 6, log)

    def test_top_level_else_reincludes(self):
        rc, log = _run('#include "e.h"\n#include "e.h"\n'
                       'int main(void){return A + B;}\n',
                       files={"e.h": "#ifndef E\n#define E\n#define A 1\n"
                                     "#else\n#define B 2\n#endif\n"})
        self.assertEqual(rc, 3, log)

    def test_content_after_endif_reincludes(self):
        rc, log = _run('#include "t.h"\n#define SECOND\n#include "t.h"\n'
                       'int main(void){return GOT;}\n',
                       files={"t.h": "#ifndef T\n#define T\n#endif\n"
                                     "#ifdef SECOND\n#define GOT 7\n#endif\n"})
        self.assertEqual(rc, 7, log)


class SkipHappensTests(unittest.TestCase):
    """The optimization is real: a guarded header is lexed once."""

    def test_second_include_is_not_lexed(self):
        import tempfile
        d = tempfile.mkdtemp()
        h = os.path.join(d, "once.h")
        with open(h, "w") as f:
            f.write("#ifndef ONCE_H\n#define ONCE_H\nint v;\n#endif\n")
        main = os.path.join(d, "m.c")
        src = '#include "once.h"\n#include "once.h"\n#include "once.h"\n'
        lexed = []
        real = lexer.tokenize

        def counting(text, filename):
            lexed.append(filename)
            return real(text, filename)
        lexer.tokenize = counting
        try:
            preproc.process(real(src, main), main)
        finally:
            lexer.tokenize = real
        self.assertEqual(lexed.count(h), 1, lexed)


if __name__ == "__main__":
    unittest.main()
