#!/usr/bin/env python3
"""test_csrust_strings -- C# `string` in csrust, backed by coost (`--coost`).

The fixture, tests/fixtures/strings/FutileLite1.cs, is the shape of Futile's
atlas / sprite / label code: an element named by a string, looked up by name,
a label whose text is appended to and indexed. It is lowered, compiled, and
*run* (gcc, and crust's own shivyc), because C that merely compiles proves
nothing about what a string does.

    python3 -m unittest tests.test_csrust_strings
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.cpprust as cpprust                              # noqa: E402
import tools.cs2cpp as cs2cpp                                # noqa: E402
import tools.cs2cpp_strings as cs_strings                    # noqa: E402
import tools.csrust as csrust                                # noqa: E402
from tests.test_csrust import run_c, run_shivyc, needs_cc    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "strings", "FutileLite1.cs")

try:
    COOST = csrust.find_coost("auto")
except cs2cpp.CsError:
    COOST = None
needs_coost = unittest.skipIf(COOST is None, "no crust edition of coost "
                              "(COOST_ROOT, or a coost directory beside crust)")


def lower(src, path="t.cs"):
    return csrust.translate(src, path=path, coost=COOST)


def refusal(src):
    try:
        lower(src)
    except cs2cpp.CsError as e:
        return e.message
    raise AssertionError("expected a refusal, got a translation")


MAIN = '''
int main(void) {
    if (FutileLite1_Run("Monkey.png") != 0) return 1;
    if (FutileLite1_Run("Banana.png") != 3) return 2;   /* index 0, not 1 */
    return 0;
}
'''


@needs_coost
class TestFixture(unittest.TestCase):
    """Every check in FutileLite1.Run passes, and the one it should fail does."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE) as f:
            cls.c = lower(f.read(), FIXTURE)

    @needs_cc
    def test_runs_under_gcc(self):
        self.assertEqual(run_c(self.c, MAIN), 0)

    def test_runs_under_shivyc(self):
        self.assertEqual(run_shivyc(self.c, MAIN), 0)

    def test_without_coost_string_is_still_refused(self):
        with self.assertRaises(cs2cpp.CsError) as cm:
            csrust.translate("public class C { public string s; }\n", path="t.cs")
        self.assertIn("`string` is not in the C# subset yet", cm.exception.message)


SMAP_FIXTURE = os.path.join(HERE, "fixtures", "strings", "Smap.cs")
COUNT_H = os.path.join(HERE, "fixtures", "strings", "count_allocs.h")


@needs_coost
class TestStringDictionary(unittest.TestCase):
    """`Dictionary<string, V>` is `_cs_smap<V>`: a sorted array of fastrings
    searched by strcmp, with a `const char *` key."""

    @classmethod
    def setUpClass(cls):
        with open(SMAP_FIXTURE) as f:
            cls.c = lower(f.read(), SMAP_FIXTURE)

    MAIN = "int main(void) { return Entry_Go(); }"      # 0 = all twelve checks passed

    @needs_cc
    def test_runs_under_gcc(self):
        # overwrite is not a second entry; a concatenated key; ordinal case;
        # Remove reports whether a key went; growth; sorted insertion
        self.assertEqual(run_c(self.c, self.MAIN), 0)

    def test_runs_under_shivyc(self):
        self.assertEqual(run_shivyc(self.c, self.MAIN), 0)

    @needs_cc
    def test_a_map_and_its_keys_are_freed_with_their_owner(self):
        # Count live heap blocks over repeated run-and-reset cycles: it must be
        # flat. (LeakSanitizer cannot tell: arenas are static, so everything in
        # them stays reachable whether or not it was freed.)
        with open(COUNT_H) as f:
            counter = f.read()
        main = """
int main(void) {
    long l[4]; int i;
    for (i = 0; i < 4; i++) {
        if (Entry_Go() != 0) return 1;
        FAtlas__arena_reset(); FAtlasElement__arena_reset();
        l[i] = g_live;
    }
    return (l[1] == l[0] && l[2] == l[0] && l[3] == l[0]) ? 0 : 2;
}
"""
        self.assertEqual(run_c(counter + self.c, main), 0)

    def test_a_string_value_is_refused_by_name(self):
        m = refusal("using System.Collections.Generic;\npublic class C {\n"
                    "  public Dictionary<string, string> d;\n}\n")
        self.assertIn("t.cs:3:", m)
        self.assertIn("a string value is not lowered yet", m)

    def test_other_key_types_are_untouched(self):
        # an int-keyed dictionary is not a string map
        out = cs_strings.lower("using System.Collections.Generic;\npublic class C {\n"
                               "  public Dictionary<int, int> d = new Dictionary<int, int>();\n}\n")
        self.assertIn("Dictionary<int, int>", out)
        self.assertNotIn("_cs_smap", out)

    def test_a_qualified_and_a_nested_value_type(self):
        out = cs_strings.lower(
            "public class C {\n  public System.Collections.Generic.Dictionary<string, "
            "System.Collections.Generic.List<int>> d;\n}\n")
        self.assertIn("_cs_smap<System.Collections.Generic.List<int>> d", out)


@needs_coost
class TestWidth(unittest.TestCase):
    @needs_cc
    def test_a_long_is_not_truncated_in_a_concatenation(self):
        c = lower("public static class W {\n  public static int Len() {\n"
                  "    long big = 5000000000L;\n    string s = \"n=\" + big;\n"
                  "    return s.Length;\n  }\n}\n")
        # "n=5000000000" is 12 characters; truncated to an int it is not
        self.assertEqual(run_c(c, "int main(void) { return W_Len() == 12 ? 0 : 1; }"), 0)


@needs_coost
class TestLowering(unittest.TestCase):
    """What the lowered text looks like, where a regression would hide."""

    def cpp(self, body, members=""):
        src = ("public class C {\n%s\npublic static int F(string a, string b) {\n%s\n}\n}\n"
               % (members, body))
        return cs_strings.lower(src)

    def test_concat_types_each_operand(self):
        out = self.cpp('string s = "a" + 1 + 2.5f + \'c\' + true + b; return 0;')
        for h in ("_cs_cat_i", "_cs_cat_f", "_cs_cat_c", "_cs_cat_b", "_cs_cat_s"):
            self.assertIn(h, out)

    def test_numbers_before_the_first_string_add(self):
        # C#: 1 + 2 + "x" is "3x"
        self.assertIn('_cs_cat_i("", 1 + 2)', self.cpp('string s = 1 + 2 + "x"; return 0;'))

    def test_null_compares_as_empty(self):
        out = self.cpp("if (a == null) return 1; if (b != null) return 2; return 0;")
        self.assertIn("_cs_str_empty(a)", out)
        self.assertIn("!_cs_str_empty(b)", out)

    def test_char_is_a_byte(self):
        out = self.cpp("int c = 'a'; return c;")
        self.assertIn("int c = 97", out)
        self.assertNotIn("char", out)

    def test_a_kept_string_is_owned_and_a_parameter_is_borrowed(self):
        out = self.cpp("string k = a + b; return 0;")
        self.assertIn("fastring k;", out)
        self.assertIn("_cs_str a", out)          # `_cs_str` is `const char *`

    def test_line_numbers_do_not_move(self):
        src = "public class C {\n  public static int F(string a) {\n    string s = a +\n      \"x\";\n    return s.Length;\n  }\n}\n"
        out = cs_strings.lower(src)
        self.assertEqual(out.count("\n"), src.count("\n"))


@needs_coost
class TestRefusals(unittest.TestCase):
    """What is not lowered yet is refused by name and line, not guessed at."""

    def test_string_collections_are_the_next_slice(self):
        m = refusal("public class C {\n  public string[] names;\n}\n")
        self.assertIn("t.cs:2:", m)
        self.assertIn("string collection is not lowered yet", m)
        m = refusal("using System.Collections.Generic;\npublic class C {\n"
                    "  public List<string> names;\n}\n")
        self.assertIn("not lowered yet", m)

    def test_an_operand_it_cannot_type_is_refused_not_guessed(self):
        m = refusal("public class C {\n  public static int F(string a) {\n"
                    "    string s = a + Mystery();\n    return 0;\n  }\n}\n")
        self.assertIn("t.cs:3:", m)
        self.assertIn("cannot tell the type of `Mystery()`", m)

    def test_concatenating_an_object_is_refused_not_a_crash(self):
        # `"a" + obj` calls obj.ToString(); an object operand (kind O) had no
        # helper and was a KeyError out of the string pass
        m = refusal("public class P { public int n; }\npublic class C {\n"
                    "  public static int F(P p, string a) {\n    string s = a + p;\n    return 0;\n  }\n}\n")
        self.assertIn("t.cs:4:", m)
        self.assertIn("not a string or a number", m)
        self.assertNotIn("Traceback", m)

    def test_ref_string_is_refused(self):
        self.assertIn("by reference", refusal(
            "public class C {\n  public static void F(ref string a) { }\n}\n"))

    def test_const_string_and_auto_property_are_refused(self):
        self.assertIn("const string", refusal(
            "public class C {\n  public const string K = \"x\";\n}\n"))
        self.assertIn("automatic", refusal(
            "public class C {\n  public string P { get; set; }\n}\n"))


class TestSubCodeGroups(unittest.TestCase):
    """cpprust._sub_code matches in a blanked copy; the groups it hands the
    callback must be the real text, or `new T("lit")` came out as spaces."""

    def test_groups_keep_their_literals(self):
        got = cpprust._sub_code(r"f\(([^)]*)\)", lambda m: "g(%s)" % m.group(1),
                                'f("abc") f(x)')
        self.assertEqual(got, 'g("abc") g(x)')

    def test_a_literal_still_cannot_match(self):
        got = cpprust._sub_code(r"f\(", lambda m: "g(", 'p("f(") f(1)')
        self.assertEqual(got, 'p("f(") g(1)')

    def test_groups_and_group_zero(self):
        got = cpprust._sub_code(r"(a)(b)?", lambda m: "%s|%s|%s" % (
            m.group(0), m.group(1), m.groups()), 'a "q"')
        self.assertEqual(got, "a|a|('a', None) \"q\"")


if __name__ == "__main__":
    unittest.main()
