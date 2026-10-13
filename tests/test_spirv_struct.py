"""Unit tests for the CFG structurizer (shivyc/spirv_struct.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shivyc.spirv_struct import structurize, Unstructurable


def flat(items, out):
    """All block ids placed by a tree, in order."""
    for it in items:
        if it[0] == "block":
            out.append(it[1])
        elif it[0] == "if":
            flat(it[2], out)
            flat(it[3], out)
        elif it[0] == "loop":
            flat(it[2], out)
    return out


def has_loop(items):
    for it in items:
        if it[0] == "loop":
            return True
        if it[0] == "if" and (has_loop(it[2]) or has_loop(it[3])):
            return True
    return False


def ends_terminated(items):
    if not items:
        return False
    last = items[-1]
    return last[0] in ("ret", "br") or (last[0] == "if" and not last[4])


class T(unittest.TestCase):
    def run_cfg(self, kinds, succs):
        tree = structurize(kinds, succs)
        placed = flat(tree, [])
        self.assertEqual(sorted(placed), sorted(set(placed)))   # exactly once
        return tree

    def test_straight_line(self):
        t = self.run_cfg(["fall", "ret"], [[1], []])
        self.assertEqual(t, [("block", 0), ("block", 1), ("ret", 1)])

    def test_diamond(self):
        t = self.run_cfg(["cond", "jmp", "jmp", "ret"],
                         [[1, 2], [3], [3], []])
        self.assertEqual(t[1][0], "if")
        self.assertTrue(t[1][4])
        self.assertEqual(t[-1], ("ret", 3))

    def test_if_without_else(self):
        t = self.run_cfg(["cond", "jmp", "ret"], [[1, 2], [2], []])
        self.assertEqual(flat(t, []), [0, 1, 2])

    def test_early_return_nests_the_rest(self):
        # 0: if c return; 1: rest; 2: ret ; 3: ret
        t = self.run_cfg(["cond", "ret", "jmp", "ret"],
                         [[1, 2], [], [3], []])
        self.assertEqual(t[1][0], "if")
        self.assertFalse(t[1][4])

    def test_while(self):
        t = self.run_cfg(["jmp", "cond", "jmp", "ret"],
                         [[1], [2, 3], [1], []])
        loop = t[1]
        self.assertEqual(loop[0], "loop")
        self.assertTrue(loop[3])
        self.assertEqual(t[-1], ("ret", 3))

    def test_do_while(self):
        # 0 -> 1 ; 1 body, cond -> 1 or 2
        t = self.run_cfg(["jmp", "cond", "ret"], [[1], [1, 2], []])
        self.assertEqual(t[1][0], "loop")

    def test_break_and_continue(self):
        # 1: while-test; 2: if (x) break; 4: if (y) continue; 5: body; ->1
        t = self.run_cfg(["jmp", "cond", "cond", "ret", "cond", "jmp", "jmp"],
                         [[1], [2, 3], [3, 4], [], [6, 5], [6], [1]])
        self.assertEqual(t[1][0], "loop")

    def test_nested_loops(self):
        # outer 1..6, inner 3..4
        kinds = ["jmp", "cond", "jmp", "cond", "jmp", "jmp", "ret"]
        succs = [[1], [2, 6], [3], [4, 5], [3], [1], []]
        t = self.run_cfg(kinds, succs)
        outer = t[1]
        self.assertEqual(outer[0], "loop")
        self.assertTrue(has_loop(outer[2]))

    def test_infinite_loop_has_no_exit(self):
        t = self.run_cfg(["jmp", "jmp"], [[1], [1]])
        self.assertFalse(t[1][3])

    def test_return_pad_is_absorbed_into_the_loop(self):
        # inner loop (3,4) leaves to 5, or through a block that returns
        kinds = ["jmp", "cond", "jmp", "cond", "cond", "jmp", "ret", "ret"]
        succs = [[1], [2, 7], [3], [4, 5], [3, 6], [1], [], []]
        t = self.run_cfg(kinds, succs)
        self.assertEqual(sorted(flat(t, [])), [0, 1, 2, 3, 4, 5, 6, 7])

    def test_pad_that_rejoins_is_absorbed(self):
        # while loop whose `break` goes through pad 3 to the same block as
        # the loop test's exit
        kinds = ["jmp", "cond", "cond", "jmp", "jmp", "ret"]
        succs = [[1], [2, 4], [1, 3], [4], [5], []]
        t = self.run_cfg(kinds, succs)
        self.assertTrue(t[1][3])

    def test_break_out_of_two_loops_is_refused(self):
        kinds = ["jmp", "cond", "jmp", "cond", "cond", "jmp", "ret"]
        succs = [[1], [2, 6], [3], [4, 5], [3, 6], [1], []]
        with self.assertRaises(Unstructurable):
            structurize(kinds, succs)

    def test_short_circuit_join_is_copied(self):
        # a && b: both failures go to block 3, which has two preds
        kinds = ["cond", "cond", "jmp", "jmp", "ret"]
        succs = [[3, 1], [3, 2], [4], [4], []]
        with self.assertRaises(Unstructurable):
            structurize(kinds, succs)
        t = structurize(kinds, succs, [1, 1, 1, 1, 1])
        self.assertEqual(len([i for i in flat(t, []) if i == 3]), 2)

    def test_switch_fallthrough_is_refused(self):
        # 0: if a goto 2 ; 1: A (falls into 2) ; 2: B ; 3: ret
        kinds = ["cond", "fall", "fall", "ret"]
        succs = [[2, 1], [2], [3], []]
        # this one is a plain join and is fine ...
        structurize(kinds, succs)
        # ... but entering the middle of an arm from outside is not
        kinds = ["cond", "cond", "fall", "ret", "fall"]
        succs = [[1, 4], [2, 3], [3], [], [2]]
        with self.assertRaises(Unstructurable):
            structurize(kinds, succs)

    def test_irreducible_is_refused(self):
        # two entries into the 1<->2 cycle
        kinds = ["cond", "cond", "cond", "ret"]
        succs = [[1, 2], [2, 3], [1, 3], []]
        with self.assertRaises(Unstructurable):
            structurize(kinds, succs)

    def test_bad_shapes_are_refused(self):
        with self.assertRaises(Unstructurable):
            structurize(["cond"], [[0]])
        with self.assertRaises(Unstructurable):
            structurize(["jmp"], [[5]])


if __name__ == "__main__":
    unittest.main()
