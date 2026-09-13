#!/usr/bin/env python3
"""Pin what a C# `class` means in csrust before any lowering exists.

Issue #25 §1: Crust has no GC. Recommended model is (b) single ownership
with (c) available per-type via `[Shared]`:

  class Node { }           // one owner; assignment does not alias
  [Shared] class Node { }  // shared_ptr; aliasing OK, cycles may leak

These tests are the contract. Implementation that lands later must keep them
green — silent value-copy of a default `class` is the failure mode
TRANSPILER.md forbids.

    python3 tools/test_csrust_semantics.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import may fail until skeleton lands; keep the file importable for CI once
# tools/cs2cpp.py exists.
try:
    import tools.cs2cpp as cs2cpp
    import tools.cpprust as cpprust
except ImportError:  # pragma: no cover
    cs2cpp = None
    cpprust = None


_NODE = """
class Node {
    public int v;
    public Node() { v = 0; }
    public void set(int x) { v = x; }
    public int get() { return v; }
}
"""

_SHARED = """
[Shared] class Node {
    public int v;
    public Node() { v = 0; }
    public void set(int x) { v = x; }
    public int get() { return v; }
}
"""


@unittest.skipUnless(cs2cpp is not None, "cs2cpp not available yet")
class TestClassAssignmentSemantics(unittest.TestCase):
    """Default class: no aliasing via assignment."""

    def lower(self, src):
        return cpprust.translate(cs2cpp.normalize(src), path="t.cs")

    def refuses(self, src, *needles):
        try:
            out = self.lower(src)
        except (cs2cpp.CsError, cpprust.CppError) as e:
            msg = getattr(e, "message", None) or (e.args[0] if e.args else str(e))
            for n in needles:
                self.assertIn(n, msg)
            return msg
        self.fail("expected refusal, got:\n%s" % out[-600:])

    def test_copy_constructing_a_class_is_refused(self):
        # C# would alias; csrust must not silently copy.
        self.refuses(
            _NODE + """
int f() {
    Node a = new Node();
    Node b = a;
    return b.get();
}
""",
            "Node",
        )

    def test_assigning_a_class_is_refused(self):
        self.refuses(
            _NODE + """
int f() {
    Node a = new Node();
    Node b = new Node();
    b = a;
    return b.get();
}
""",
            "Node",
        )

    def test_shared_class_may_alias(self):
        out = self.lower(
            _SHARED + """
int f() {
    Node a = new Node();
    Node b = a;
    b.set(7);
    return a.get();
}
"""
        )
        # shared_ptr lowering leaves a copy/alias path in the C.
        self.assertTrue(
            ("shared_ptr" in out) or ("Node_copy" in out) or ("use_count" in out)
            or ("b = a" in out) or ("shared_ptr_Node" in out),
            out[-800:],
        )
        # Method calls must go through get()+Type_method, not shared_ptr::get
        # alone as the C# get() result.
        self.assertIn("Node_set", out)
        self.assertIn("Node_get", out)


@unittest.skipUnless(cs2cpp is not None, "cs2cpp not available yet")
class TestStructIsValue(unittest.TestCase):
    """struct stays a value type; copy is fine."""

    def test_struct_copy_ok(self):
        out = cpprust.translate(
            cs2cpp.normalize("""
struct Point {
    public int x;
    public Point(int x) { this.x = x; }
}
int f() {
    Point a = new Point(1);
    Point b = a;
    return b.x;
}
"""),
            path="t.cs",
        )
        self.assertIn("return", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
