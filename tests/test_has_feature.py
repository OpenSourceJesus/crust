"""Tests for the preprocessor feature-test operators.

`__has_include` (C23; GCC and clang for years) and clang's `__has_feature`,
`__has_extension`, `__has_attribute`, `__has_c_attribute`,
`__has_cpp_attribute`, `__has_declspec_attribute` and `__has_builtin`.

Headers written for clang -- the macOS SDK throughout -- guard their
extensions with these, and fall back to portable code when told "no". So
the operators must (a) parse inside #if, (b) answer honestly: no clang
features, attributes or builtins, and a real include-path search for
__has_include, and (c) count as defined, for `#ifdef __has_feature` and
`defined(__has_feature) && __has_feature(x)` guards.

Each program's exit code encodes its checks, so a wrong answer produces a
wrong value rather than passing silently.
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(source, files=None, include_dirs=None):
    """Compile `source` (plus `files`: relpath -> text, written beside it)
    natively and run it. Returns (exit code, compiler diagnostics)."""
    d = tempfile.mkdtemp()
    for rel, text in (files or {}).items():
        path = os.path.join(d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    c = os.path.join(d, "prog.c")
    exe = os.path.join(d, "prog")
    with open(c, "w") as f:
        f.write(source)
    cmd = [sys.executable, "-m", "shivyc.main", c, "-o", exe]
    for inc in include_dirs or []:
        cmd += ["-I", os.path.join(d, inc)]
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=d, env=env)
    if p.returncode != 0 or not os.path.exists(exe):
        return None, p.stdout + p.stderr
    return subprocess.run([exe]).returncode, p.stdout + p.stderr


class HasIncludeTests(unittest.TestCase):

    def test_angled_bundled_header_found(self):
        rc, log = _run("#if __has_include(<stdio.h>)\n"
                       "int main(void){return 7;}\n#else\n"
                       "int main(void){return 1;}\n#endif\n")
        self.assertEqual(rc, 7, log)

    def test_missing_header_is_zero_not_an_error(self):
        rc, log = _run("#if __has_include(<no_such_header_4f2a.h>)\n"
                       "int main(void){return 1;}\n#else\n"
                       "int main(void){return 7;}\n#endif\n")
        self.assertEqual(rc, 7, log)

    def test_quoted_resolves_beside_the_source(self):
        rc, log = _run('#if __has_include("local.h")\n#include "local.h"\n'
                       '#endif\n'
                       '#if __has_include("absent.h")\nint absent = 1;\n#endif\n'
                       'int main(void){return LOCAL_VALUE;}\n',
                       files={"local.h": "#define LOCAL_VALUE 9\n"})
        self.assertEqual(rc, 9, log)

    def test_angled_path_with_slash_and_dash_via_include_dir(self):
        # Outside #include the lexer splits <sub/has-dash.h> into pieces;
        # they must be rejoined into the exact name and searched on -I.
        rc, log = _run("#if __has_include(<sub/has-dash.h>)\n"
                       "#include <sub/has-dash.h>\n#endif\n"
                       "int main(void){return DASH;}\n",
                       files={"inc/sub/has-dash.h": "#define DASH 5\n"},
                       include_dirs=["inc"])
        self.assertEqual(rc, 5, log)

    def test_combined_with_other_operators(self):
        rc, log = _run("#if __has_include(<stdio.h>) && !__has_include(<nope.h>)"
                       " && (1 + __has_include(<stddef.h>)) == 2\n"
                       "int main(void){return 3;}\n#else\n"
                       "int main(void){return 1;}\n#endif\n")
        self.assertEqual(rc, 3, log)


class IncludeNextTests(unittest.TestCase):
    """`#include_next` used to be an unknown directive and silently dropped,
    so a wrapper header never reached the header it wraps."""

    WRAP = {
        "a/w.h": "#define A_SEEN 1\n"
                 "#if __has_include_next(<w.h>)\n#define A_HAS_NEXT 1\n#endif\n"
                 "#include_next <w.h>\n",
        "b/w.h": "#define B_VAL 7\n"
                 "#if __has_include_next(<w.h>)\n#define B_HAS_NEXT 1\n#endif\n",
    }

    def test_wrapper_reaches_the_wrapped_header(self):
        rc, log = _run("#include <w.h>\n"
                       "int main(void){return A_SEEN + B_VAL;}\n",
                       files=self.WRAP, include_dirs=["a", "b"])
        self.assertEqual(rc, 8, log)

    def test_has_include_next_answers_per_position(self):
        # From a/ there is a later w.h (in b/); from b/ there is none.
        rc, log = _run("#include <w.h>\n"
                       "#ifndef B_HAS_NEXT\n#define B_HAS_NEXT 0\n#endif\n"
                       "int main(void){return A_HAS_NEXT * 10 + B_HAS_NEXT;}\n",
                       files=self.WRAP, include_dirs=["a", "b"])
        self.assertEqual(rc, 10, log)

    def test_in_main_file_is_an_ordinary_include(self):
        rc, log = _run("#include_next <w.h>\nint main(void){return B_VAL;}\n",
                       files={"b/w.h": "#define B_VAL 6\n"},
                       include_dirs=["b"])
        self.assertEqual(rc, 6, log)

    def test_missing_next_is_an_error_not_silence(self):
        rc, log = _run("#include <w.h>\nint main(void){return 0;}\n",
                       files={"a/w.h": "#include_next <w.h>\n"},
                       include_dirs=["a"])
        self.assertIsNone(rc)
        self.assertIn("unable to read included file", log)


class DefinedFromExpansionTests(unittest.TestCase):
    """`defined` produced by macro expansion inside #if. C leaves it
    undefined; GCC and clang evaluate it, and the macOS SDK's pthread.h
    relies on it (`#if !_PTHREAD_SWIFT_IMPORTER_NULLABILITY_COMPAT()`)."""

    def test_function_like_macro_yielding_defined(self):
        rc, log = _run("#define HAVE_X 1\n"
                       "#define CHECK() defined(HAVE_X) && !defined(NOPE)\n"
                       "#if CHECK()\nint main(void){return 7;}\n#else\n"
                       "int main(void){return 1;}\n#endif\n")
        self.assertEqual(rc, 7, log)

    def test_operand_is_not_expanded(self):
        # ZERO is defined (as 0): `defined(ZERO)` is 1, not `defined(0)`.
        rc, log = _run("#define ZERO 0\n#define Q defined(ZERO)\n"
                       "#define R defined ZERO\n"
                       "#if Q && R\nint main(void){return 7;}\n#else\n"
                       "int main(void){return 1;}\n#endif\n")
        self.assertEqual(rc, 7, log)

    def test_undefined_operand_is_zero(self):
        # Parenthesized: expansion is textual, so `!P()` would otherwise
        # bind `!` to the first operand only (GCC agrees: that gives 1).
        rc, log = _run("#define P() (defined(NOT_THERE) || (NOT_THERE < 1))\n"
                       "#if !P()\nint main(void){return 1;}\n#else\n"
                       "int main(void){return 7;}\n#endif\n")
        self.assertEqual(rc, 7, log)


class FeatureOperatorTests(unittest.TestCase):

    def test_operators_count_as_defined(self):
        src = ""
        for i, op in enumerate(["__has_include", "__has_feature",
                                "__has_extension", "__has_attribute",
                                "__has_builtin", "__has_c_attribute",
                                "__has_cpp_attribute"]):
            src += "#ifdef %s\nint d%d = 1;\n#else\nint d%d = 0;\n#endif\n" \
                   % (op, i, i)
        src += ("#if defined(__has_feature) && defined __has_include\n"
                "int both = 1;\n#else\nint both = 0;\n#endif\n"
                "int main(void){return d0+d1+d2+d3+d4+d5+d6+both;}\n")
        rc, log = _run(src)
        self.assertEqual(rc, 8, log)

    def test_clang_features_answer_no(self):
        # The exact guard shape the macOS SDK uses; it used to be a parse
        # error ("trailing tokens") because the operator was unknown.
        rc, log = _run("#if defined(__has_feature) && __has_feature(modules)\n"
                       "int main(void){return 1;}\n"
                       "#elif __has_feature(nullability) || "
                       "__has_extension(blocks) || __has_attribute(noreturn)"
                       " || __has_builtin(__builtin_expect)"
                       " || __has_c_attribute(nodiscard)\n"
                       "int main(void){return 2;}\n#else\n"
                       "int main(void){return 7;}\n#endif\n")
        self.assertEqual(rc, 7, log)

    def test_header_fallback_idiom_still_works(self):
        # Portable headers define their own fallback when the operator is
        # missing; with it present they must not redefine it, and either
        # way the result is the same.
        rc, log = _run("#ifndef __has_attribute\n"
                       "#define __has_attribute(x) 1\n#endif\n"
                       "#if __has_attribute(unused)\n"
                       "int main(void){return 1;}\n#else\n"
                       "int main(void){return 7;}\n#endif\n")
        self.assertEqual(rc, 7, log)


if __name__ == "__main__":
    unittest.main()
