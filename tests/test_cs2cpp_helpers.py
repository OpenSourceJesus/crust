"""C# structure and literal helpers in tools/cs2cpp.py.

These moved from tools/unity_pack.py (unity_pack keeps the old underscore
names as aliases). Nothing here is Unity-specific.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.cs2cpp as cs2cpp  # noqa: E402
import tools.unity_pack as unity_pack  # noqa: E402


class TestLiterals(unittest.TestCase):
    def test_c_string_escapes(self):
        self.assertEqual(cs2cpp.c_string('a"b\\c\n'), '"a\\"b\\\\c\\n"')

    def test_string_literal_value(self):
        self.assertEqual(cs2cpp.string_literal_value('"hi \\"x\\""'), 'hi "x"')

    def test_c_ident(self):
        self.assertEqual(cs2cpp.c_ident("Game.Player-1"), "Game_Player_1")


class TestCallArgs(unittest.TestCase):
    def test_split_respects_parentheses(self):
        self.assertEqual(cs2cpp.split_call_args("a, f(b, c), d"),
                         ["a", "f(b, c)", "d"])

    def test_split_respects_string_literals(self):
        self.assertEqual(cs2cpp.split_call_args('a, "x,y", d'),
                         ["a", '"x,y"', "d"])
        self.assertEqual(cs2cpp.split_call_args('"a\\",b", c'),
                         ['"a\\",b"', "c"])

    def test_split_respects_other_literals_and_braces(self):
        self.assertEqual(cs2cpp.split_call_args("'a', ',', b"),
                         ["'a'", "','", "b"])
        self.assertEqual(cs2cpp.split_call_args('@"q"",""z", d'),
                         ['@"q"",""z"', "d"])
        self.assertEqual(cs2cpp.split_call_args('$"{a},{b}", d'),
                         ['$"{a},{b}"', "d"])
        self.assertEqual(cs2cpp.split_call_args("new int[] { 1, 2 }, x"),
                         ["new int[] { 1, 2 }", "x"])

    def test_match_call_args(self):
        text = "foo(1, g(2), 3) + 4"
        self.assertEqual(cs2cpp.match_call_args(text, text.index("(")),
                         ("1, g(2), 3", 15))


class TestMethodSignatures(unittest.TestCase):
    def test_arg_names_skip_modifiers(self):
        self.assertEqual(cs2cpp.method_c_arg_names("int a, ref float b"),
                         ["a", "b"])

    def test_arg_names_with_array_params(self):
        self.assertEqual(
            cs2cpp.method_c_arg_names("int a, params int[] rest"),
            ["a", "rest"])

    def test_parse_params(self):
        ps = cs2cpp.parse_params(
            'Dictionary<int, string> d, string s = "a,b", out Vector2 v, '
            "params float[,] grid")
        self.assertEqual(
            [(p.modifier, p.type, p.name, p.default) for p in ps],
            [(None, "Dictionary<int,string>", "d", None),
             (None, "string", "s", '"a,b"'),
             ("out", "Vector2", "v", None),
             ("params", "float[,]", "grid", None)])

    def test_names_params_and_suffix_agree(self):
        # unity_pack passes method_c_arg_names to a function declared with
        # _method_c_params: the two must list the same parameters.
        for args in ("int a, params int[] rest", "int a = 5, bool b = true",
                     "Dictionary<int, string> d, float f", ""):
            names = cs2cpp.method_c_arg_names(args)
            params = unity_pack._method_c_params(args)
            self.assertEqual(
                [p.split()[-1] for p in params.split(", ")] if params else [],
                names)

    def test_overload_symbols(self):
        self.assertEqual(cs2cpp.method_arg_type_suffix("int a, float b"),
                         "int_float")
        self.assertEqual(cs2cpp.method_arg_type_suffix(""), "void")
        self.assertEqual(
            cs2cpp.method_arg_type_suffix("Dictionary<int, string> d, float f"),
            "Dictionary_int_string_float")
        # Arrays stay distinct from scalars, so overloads cannot collide
        self.assertEqual(cs2cpp.method_arg_type_suffix("int a, int[] b"),
                         "int_int_array")
        self.assertNotEqual(cs2cpp.method_arg_type_suffix("int a, int[] b"),
                            cs2cpp.method_arg_type_suffix("int a, int b"))
        self.assertEqual(
            cs2cpp.method_c_symbol("Counter", "Add", "int a, int b", True),
            "Counter_Add_int_int")
        self.assertEqual(
            cs2cpp.method_c_symbol("Counter", "Add", "int a", False),
            "Counter_Add")

    def test_modifiers(self):
        self.assertEqual(cs2cpp.MODIFIERS, frozenset(
            ("public", "private", "protected", "internal", "static")))


class TestDiagnostics(unittest.TestCase):
    TEXT = "class A {\n  void F() {\n    bad();\n  }\n}\n"

    def test_line_col(self):
        self.assertEqual(cs2cpp.line_col(self.TEXT, self.TEXT.index("bad")),
                         (3, 5))
        self.assertEqual(cs2cpp.line_col("", 0), (1, 1))

    def test_cs_diag_default_path(self):
        i = self.TEXT.index("bad")
        self.assertEqual(
            cs2cpp.cs_diag("/proj/Assets/Scripts/A.cs", self.TEXT, i,
                           "CS0103", "nope"),
            "A.cs(3,5): error CS0103: nope")
        self.assertEqual(cs2cpp.cs_diag("", "", 0, "CS1", "m", "warning"),
                         "<cs>(1,1): warning CS1: m")

    def test_cs_diag_display_path(self):
        self.assertEqual(
            cs2cpp.cs_diag("a/b.cs", "x", 0, "CS1", "m",
                           display_path=lambda p: "[" + p + "]"),
            "[a/b.cs](1,1): error CS1: m")

    def test_cs_diag_at_site(self):
        body_abs = self.TEXT.index("{\n    bad")
        site = {"path": "/p/X.cs", "file_text": self.TEXT,
                "body_abs": body_abs}
        self.assertEqual(cs2cpp.cs_diag_at_site(site, 6, "CS0", "m"),
                         "X.cs(3,5): error CS0: m")
        self.assertEqual(cs2cpp.cs_diag_at_site({"path": "/p/X.cs"}, 6,
                                                "CS0", "m"),
                         "X.cs(1,1): error CS0: m")

    def test_unity_pack_prints_assets_paths(self):
        i = self.TEXT.index("bad")
        self.assertEqual(
            unity_pack._cs_diag("/proj/Assets/Scripts/A.cs", self.TEXT, i,
                                "CS0103", "nope"),
            "Assets/Scripts/A.cs(3,5): error CS0103: nope")


class TestUnityPackAliases(unittest.TestCase):
    def test_old_names_are_the_moved_functions(self):
        for old, new in (("_methods_in", "methods_in"),
                         ("_c_ident", "c_ident"),
                         ("_split_call_args", "split_call_args"),
                         ("_MODIFIERS", "MODIFIERS"),
                         ("_blank_method_bodies", "blank_method_bodies")):
            self.assertIs(getattr(unity_pack, old), getattr(cs2cpp, new))


if __name__ == "__main__":
    unittest.main(verbosity=2)
