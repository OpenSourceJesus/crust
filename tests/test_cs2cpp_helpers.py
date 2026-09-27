"""C# structure and literal helpers in tools/cs2cpp.py.

These moved from tools/unity_pack.py unchanged (unity_pack keeps the old
underscore names as aliases). Nothing here is Unity-specific. Two known gaps
are recorded as expected failures.
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

    @unittest.expectedFailure
    def test_split_respects_string_literals(self):
        # Known gap: commas inside string literals split the argument.
        self.assertEqual(cs2cpp.split_call_args('a, "x,y", d'),
                         ["a", '"x,y"', "d"])

    def test_match_call_args(self):
        text = "foo(1, g(2), 3) + 4"
        self.assertEqual(cs2cpp.match_call_args(text, text.index("(")),
                         ("1, g(2), 3", 15))


class TestMethodSignatures(unittest.TestCase):
    def test_arg_names_skip_modifiers(self):
        self.assertEqual(cs2cpp.method_c_arg_names("int a, ref float b"),
                         ["a", "b"])

    @unittest.expectedFailure
    def test_arg_names_with_array_params(self):
        # Known gap: `params int[] rest` is not matched, so `rest` is lost.
        self.assertEqual(
            cs2cpp.method_c_arg_names("int a, params int[] rest"),
            ["a", "rest"])

    def test_overload_symbols(self):
        self.assertEqual(cs2cpp.method_arg_type_suffix("int a, float b"),
                         "int_float")
        self.assertEqual(cs2cpp.method_arg_type_suffix(""), "void")
        self.assertEqual(
            cs2cpp.method_c_symbol("Counter", "Add", "int a, int b", True),
            "Counter_Add_int_int")
        self.assertEqual(
            cs2cpp.method_c_symbol("Counter", "Add", "int a", False),
            "Counter_Add")

    def test_modifiers(self):
        self.assertEqual(cs2cpp.MODIFIERS, frozenset(
            ("public", "private", "protected", "internal", "static")))


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
