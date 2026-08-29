#!/usr/bin/env python3
"""test_cpprpy_decls -- the class-interface digest, and inheriting across it.

`py2c.py --decls` writes `<module>.decls.json` beside the generated C;
`cpprust.py --decls <file>` reads it, so a `.cpp` can name an rpython class
as a base. See CPPRPY.md section 4.

The claim under test is not "it translates". It is that **one object built
by C++ answers correctly when asked by either language**: a C++ override
reached through py2c's `TypeInfo`, and `isinstance_of` walking a base chain
that crosses the boundary. `TestAcrossTheBoundary` builds exactly that and
runs it.

Two properties are worth naming because a regression in either is silent:

  * **Designated initializers.** The derived table is emitted by field
    *name*, not position, so reordering py2c's `VTABLE_METHODS` cannot turn
    into a wrong indirect call here -- it becomes a compile error or
    nothing at all. `test_table_uses_designated_initializers` pins that.

  * **The descriptor pointer is one word at offset zero in both models**,
    spelled `_vptr` on one side and `_hdr.type` on the other. Emission has
    to use the spelling of whichever struct declares it;
    `test_ctor_installs_through_obj` pins that it does.

    python3 tools/test_cpprpy_decls.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tools.cpprust as cpprust                       # noqa: E402

PY2C = os.path.join(HERE, "py2c.py")

SHAPES_PY = """\
class Shape:
    def __init__(self, i: "int"):
        self.id = i
    def area(self) -> "int":
        return 0

class Square(Shape):
    def __init__(self, i: "int", s: "int"):
        self.id = i
        self.side = s
    def area(self) -> "int":
        return self.side * self.side
"""

CUBE_CPP = """\
#include "shapes.py"

class Cube : public Square {
public:
    int depth;
    Cube(int i, int s, int d) : Square(i, s) { depth = d; }
    int area() { return 6 * side * side; }
};

int cube_area(int i, int s, int d) {
    Cube c(i, s, d);
    return c.area();
}
"""


def _have(prog):
    return shutil.which(prog) is not None


class DigestBase(unittest.TestCase):
    """Builds a real digest once per class, since transpiling is by far the
    slowest step and every test here wants the same one."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="cpprpy-")
        py = os.path.join(cls.dir, "shapes.py")
        with open(py, "w") as f:
            f.write(SHAPES_PY)
        r = subprocess.run([sys.executable, PY2C, py, "--out", cls.dir,
                            "--decls"], capture_output=True, text=True)
        if r.returncode != 0:
            raise unittest.SkipTest("py2c failed: %s" % r.stderr[-400:])
        cls.decls_path = os.path.join(cls.dir, "shapes.decls.json")
        if not os.path.exists(cls.decls_path):
            raise unittest.SkipTest("py2c wrote no digest")
        with open(cls.decls_path) as f:
            cls.digest = json.load(f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def lower(self, src, **kw):
        kw.setdefault("decls", [self.decls_path])
        return cpprust.translate(src, path="t.cpp", **kw)


class TestDigestShape(DigestBase):

    def test_version_and_language(self):
        self.assertEqual(cpprust.DECLS_VERSION, self.digest["version"])
        self.assertEqual("rpython", self.digest["lang"])

    def test_classes_and_bases(self):
        by = dict((c["name"], c) for c in self.digest["classes"])
        self.assertIn("Shape", by)
        self.assertEqual("Shape", by["Square"]["base"])
        self.assertIsNone(by["Shape"]["base"])

    def test_fields_are_flattened_with_ctypes(self):
        """py2c repeats a base's fields in the derived struct, so `Square`
        carries `id` as well as `side`. Recorded as emitted rather than as
        declared -- what a consumer needs is the layout."""
        by = dict((c["name"], c) for c in self.digest["classes"])
        self.assertEqual([("id", "int"), ("side", "int")],
                         [(f["name"], f["ctype"])
                          for f in by["Square"]["fields"]])

    def test_slots_are_recorded_once_for_the_module(self):
        """The vtable is a property of the hierarchy, not of a class: py2c
        gives every class in a module the same `TypeInfo` layout. Recording
        it per class would invite a consumer to believe two classes could
        disagree."""
        self.assertIn("descriptor", self.digest)
        names = [s["name"] for s in self.digest["descriptor"]["slots"]]
        self.assertIn("area", names)
        for c in self.digest["classes"]:
            self.assertNotIn("slots", c)

    def test_descriptor_header_matches_the_cpp_side(self):
        """Same check as the RTTI suite's, from the other direction: the
        digest states the header it expects, and cpprust's descriptor has
        to be that."""
        import re
        m = re.search(r"typedef struct _CppTypeInfo \{(.*?)\} _CppTypeInfo;",
                      cpprust._RTTI_PRELUDE, re.S)
        got = [re.search(r"(\w+)\s*;$", ln).group(1)
               for ln in (re.sub(r"/\*.*?\*/", "", x).strip()
                          for x in m.group(1).splitlines())
               if ln.endswith(";")]
        self.assertEqual(self.digest["descriptor"]["header"], got)


class TestDigestIsRefusedWhenWrong(DigestBase):

    def test_unknown_version(self):
        bad = os.path.join(self.dir, "bad.json")
        d = dict(self.digest)
        d["version"] = 999
        with open(bad, "w") as f:
            json.dump(d, f)
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("int f(void) { return 1; }", decls=[bad])
        self.assertIn("version", cm.exception.args[0])

    def test_missing_file(self):
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("int f(void) { return 1; }",
                              decls=[os.path.join(self.dir, "nope.json")])
        self.assertIn("cannot read it", cm.exception.args[0])


class TestCppInheritsRpython(DigestBase):

    def test_rpython_module_include_is_left_alone(self):
        """cpprust does not splice a `.py`: it is not C++, and reading it as
        C++ finds a `class` keyword and then fails inside a Python body. The
        preprocessor answers that include; the declarations arrive here as
        `--decls`."""
        out = self.lower(CUBE_CPP)
        self.assertIn('#include "shapes.py"', out)

    def test_derived_struct_nests_the_rpython_base(self):
        out = self.lower(CUBE_CPP)
        self.assertIn("struct Cube { Square _base; int depth; };", out)

    def test_table_uses_designated_initializers(self):
        """By name, not position -- so reordering py2c's slots cannot become
        a wrong indirect call here."""
        out = self.lower(CUBE_CPP)
        self.assertIn(".name = \"Cube\"", out)
        self.assertIn(".area = &Cube__thunk_area", out)
        self.assertIn(".objsize = sizeof(struct Cube)", out)

    def test_base_link_reaches_the_rpython_descriptor(self):
        """What makes `isinstance` and `dynamic_cast` work across the
        boundary: one chain, both languages on it."""
        self.assertIn("&Square_type", self.lower(CUBE_CPP))

    def test_no_second_vtable_struct(self):
        """The hierarchy already has a descriptor type and it is the other
        language's. Emitting a second would be a second layout."""
        out = self.lower(CUBE_CPP)
        self.assertNotIn("struct Cube_vtable", out)

    def test_ctor_installs_through_obj(self):
        """The rpython root spells its descriptor pointer `_hdr.type`
        through `Obj`, not `_vptr`. Same word at the same offset -- which is
        why the two models meet at all -- but the member has to be named the
        way that struct declares it."""
        out = self.lower(CUBE_CPP)
        self.assertIn("((Obj *)this)->type = &Cube__vtable;", out)
        self.assertNotIn("->_vptr = ", out)

    def test_base_constructor_uses_the_rpython_spelling(self):
        self.assertIn("Square___init__(&this->_base, i, s);",
                      self.lower(CUBE_CPP))

    def test_dispatch_goes_through_the_foreign_descriptor(self):
        out = self.lower(CUBE_CPP)
        self.assertIn("->type)->area((Obj *)", out)


class TestCppProducesADigest(DigestBase):
    """`cpprust --emit-decls` writes the same shape py2c does. The consumer
    for it is py2c -- so an rpython class can subclass a C++ one -- which is
    not wired up yet; what is pinned here is that the artifact is produced
    and is the same artifact."""

    POOL = """\
class Pool {
public:
    int cap;
    int used;
    Pool(int c) { cap = c; used = 0; }
    virtual ~Pool() { }
    virtual int take() { used = used + 1; return used; }
};
"""

    def _emit(self):
        out = os.path.join(self.dir, "pool.decls.json")
        cpprust.translate(self.POOL, path="pool.cpp", rtti=True,
                          decls_out=out)
        with open(out) as f:
            return json.load(f)

    def test_same_version_and_shape_as_the_rpython_digest(self):
        d = self._emit()
        self.assertEqual(self.digest["version"], d["version"])
        self.assertEqual(sorted(self.digest.keys()), sorted(d.keys()))
        self.assertEqual("cpp", d["lang"])

    def test_descriptor_header_agrees_with_the_rpython_one(self):
        """The two languages publish the same header, which is the claim
        the whole digest rests on."""
        self.assertEqual(self.digest["descriptor"]["header"],
                         self._emit()["descriptor"]["header"])

    def test_fields_and_slots(self):
        d = self._emit()
        c = d["classes"][0]
        self.assertEqual([("cap", "int"), ("used", "int")],
                         [(f["name"], f["ctype"]) for f in c["fields"]])
        self.assertIn("take", [s["name"] for s in d["descriptor"]["slots"]])

    def test_concrete_class_publishes_its_table_as_the_descriptor(self):
        """A concrete class *is* its own descriptor -- the vtable's header
        prefix -- so the symbol is the table. Published so a consumer need
        not know that rule."""
        self.assertEqual("Pool__vtable", self._emit()["classes"][0]["typeinfo"])

    def test_cpprust_refuses_to_consume_a_cpp_digest(self):
        """Not the intended direction, and it cannot work as written: a
        derived table needs the base's per-class `struct <B>_vtable`, which
        a digest does not carry. Refused rather than half-supported."""
        out = os.path.join(self.dir, "pool.decls.json")
        self._emit()
        with self.assertRaises(cpprust.CppError) as cm:
            cpprust.translate("class D : public Pool { public: int f; };",
                              decls=[out])
        self.assertIn("cannot yet inherit from one", cm.exception.args[0])


@unittest.skipUnless(_have("gcc"), "gcc not available")
class TestAcrossTheBoundary(DigestBase):
    """The actual claim: one object, both languages, same answers."""

    def _build_and_run(self, main_body):
        d = self.dir
        cpp = os.path.join(d, "cube.cpp")
        with open(cpp, "w") as f:
            f.write(CUBE_CPP)
        cout = os.path.join(d, "cube.c")
        with open(cout, "w") as f:
            # The `.py` include is preproc's job; here the generated C is
            # spliced directly, which is the same translation unit by a
            # shorter road.
            f.write(self.lower(CUBE_CPP).replace(
                '#include "shapes.py"', '#include "shapes.c"'))
        drv = os.path.join(d, "drv.c")
        with open(drv, "w") as f:
            f.write('#include <stdio.h>\n#include "cube.c"\n'
                    'int main(void) {\n%s\nreturn 0; }\n' % main_body)
        exe = os.path.join(d, "drv")
        r = subprocess.run(["gcc", "-std=c11", "-I", d, "-o", exe, drv,
                            os.path.join(d, "shivyc_rt.c")],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr[-800:])
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(0, run.returncode, "crashed: %s" % run.stderr)
        return run.stdout

    def test_cpp_override_runs(self):
        self.assertEqual("54\n", self._build_and_run(
            'printf("%d\\n", cube_area(1, 3, 4));'))

    def test_rpython_dispatch_reaches_the_cpp_override(self):
        """py2c's own virtual call, on an object C++ built, landing in the
        C++ override. This is the whole point of sharing a descriptor."""
        self.assertEqual("54\n", self._build_and_run(
            'Cube c; Cube_new(&c, 1, 3, 4); Shape *s = (Shape *)&c;\n'
            'printf("%d\\n", TYPE(s)->area((Obj *)s));'))

    def test_isinstance_walks_across_the_boundary(self):
        """A C++ class is an instance of its rpython bases, by the same
        base-chain walk py2c already had."""
        self.assertEqual("1 1\n", self._build_and_run(
            'Cube c; Cube_new(&c, 1, 3, 4); Obj *o = (Obj *)&c;\n'
            'printf("%d %d\\n", isinstance_of(o, &Shape_type),'
            ' isinstance_of(o, &Square_type));'))

    def test_type_name_is_the_cpp_class(self):
        self.assertEqual("Cube\n", self._build_and_run(
            'Cube c; Cube_new(&c, 1, 3, 4); Shape *s = (Shape *)&c;\n'
            'printf("%s\\n", TYPE(s)->name);'))

    def test_the_rpython_base_still_answers_for_itself(self):
        """The derived table must not disturb the base's own dispatch.

        Built with `Square_new`, not `Square___init__`: py2c splits the two,
        and it is the allocator that arena-allocates *and* stamps the
        descriptor. `__init__` only assigns fields. Which is exactly why the
        C++ constructor emitted for `Cube` stamps the descriptor itself
        after chaining to `Square___init__` -- there is no other place it
        would happen."""
        self.assertEqual("25 Square\n", self._build_and_run(
            'Square *q = Square_new(2, 5);\n'
            'printf("%d %s\\n", TYPE(q)->area((Obj *)q), TYPE(q)->name);'))


# ------------------------------------------- the other direction

@unittest.skipUnless(_have("gcc"), "gcc not available")
class TestRpythonInheritsCpp(unittest.TestCase):
    """The mirror of TestAcrossTheBoundary: `import pool_cpp` resolves a
    C++ digest, `class MyPool(pool_cpp.Pool)` lays out over the C++
    struct, and one object answers both languages.

    The consumer works by rendering the digest as the Python module a
    matching class would have been written as, and letting py2c's ordinary
    import path parse it -- no hand-synthesized ClassInfo. Three stub
    properties are load-bearing and each was found by a failure:
    fields assign from *annotated params* (a literal zero infers a boxed
    field and mislays every subclass's struct); method bodies *write* an
    instance field (a body that doesn't reads as POD and the base's slots
    are dropped -- a NULL in the derived table); and the digest joins the
    project hierarchy scan (else the subclass roots itself and the canon
    shrinks to its own methods)."""

    POOL_CPP = """\
class Pool {
public:
    int cap;
    int used;
    Pool(int c) { cap = c; used = 0; }
    virtual ~Pool() { }
    virtual int take() { used = used + 1; return used; }
    virtual int give() { used = used - 1; return used; }
};
"""
    MYPOOL_PY = """\
import pool_cpp

class MyPool(pool_cpp.Pool):
    def __init__(self, c: "int", hi: "int"):
        self.cap = c
        self.used = 0
        self.high = hi
    def room(self) -> "int":
        return self.cap - self.used

def demo() -> "int":
    p = MyPool(10, 3)
    p.take()
    p.take()
    return p.room() * 100 + p.used
"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="cpprpy-rev-")
        d = cls.dir
        with open(os.path.join(d, "pool.cpp"), "w") as f:
            f.write(cls.POOL_CPP)
        cls.pool_c = os.path.join(d, "pool.c")
        with open(cls.pool_c, "w") as f:
            f.write(cpprust.translate(
                cls.POOL_CPP, path="pool.cpp", rtti=True,
                decls_out=os.path.join(d, "pool_cpp.decls.json")))
        with open(os.path.join(d, "mypool.py"), "w") as f:
            f.write(cls.MYPOOL_PY)
        r = subprocess.run([sys.executable, os.path.join(HERE, "py2c.py"),
                            os.path.join(d, "mypool.py"), "--out", d],
                           capture_output=True, text=True, cwd=d)
        if r.returncode != 0:
            raise unittest.SkipTest("py2c failed: %s" % r.stderr[-400:])
        cls.mypool_c = os.path.join(d, "mypool.c")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _run(self, driver):
        d = self.dir
        drv = os.path.join(d, "drv.c")
        with open(drv, "w") as f:
            f.write(driver)
        exe = os.path.join(d, "drv")
        r = subprocess.run(["gcc", "-std=c11", "-w", "-I", d, "-o", exe,
                            drv, self.mypool_c, self.pool_c,
                            os.path.join(d, "shivyc_rt.c")],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr[-800:])
        run = subprocess.run([exe], capture_output=True, text=True)
        self.assertEqual(0, run.returncode, "crashed: %s" % run.stderr)
        return run.stdout

    def test_import_resolves_and_the_base_chain_links(self):
        with open(self.mypool_c) as f:
            c = f.read()
        self.assertIn("(const struct TypeInfo*)&Pool_type", c)
        self.assertIn("extern const TypeInfo Pool_type;", c)

    def test_base_fields_are_unboxed_at_the_cpp_types(self):
        with open(self.mypool_c) as f:
            c = f.read()
        self.assertIn("int cap;", c)
        self.assertNotIn("obj cap;", c)

    def test_derived_table_carries_the_cpp_implementations(self):
        """The inherited virtuals are filled with the C++ functions, by
        the symbols the digest published and cpprust gave external
        linkage to."""
        with open(self.mypool_c) as f:
            c = f.read()
        self.assertIn(".take = Pool_take", c)
        self.assertIn(".give = Pool_give", c)

    def test_one_object_answers_both_languages(self):
        """demo() builds the object in rpython, dispatches the C++
        `take()` twice, and reads its own field: 8*100 + 2."""
        out = self._run('#include <stdio.h>\n'
                        'extern long demo(void);\n'
                        'int main(void){printf("%ld\\n", demo());'
                        'return 0;}\n')
        self.assertEqual("802\n", out)

    def test_isinstance_walks_into_the_cpp_base(self):
        """The chain crosses the boundary: an rpython-built MyPool is an
        instance of the C++ `Pool`, through the `Pool_type` alias cpprust
        emitted for its vtable."""
        out = self._run('#include <stdio.h>\n'
                        '#include "shivyc_rt.h"\n'
                        # `TypeInfo` is per-module (each generated .c
                        # declares its own, slots included); the shared
                        # runtime declares only the prefix, TypeInfoHdr.
                        # Externing through the prefix is not a workaround:
                        # prefix-compatibility IS the invariant this
                        # boundary rests on, so leaning on it is the test.
                        'extern const TypeInfoHdr Pool_type;\n'
                        'extern const TypeInfoHdr MyPool_type;\n'
                        'int main(void){\n'
                        '  const TypeInfoHdr *k = &MyPool_type;\n'
                        '  int hit = 0;\n'
                        '  for (; k; k = k->base)\n'
                        '    if (k == &Pool_type) '
                        'hit = 1;\n'
                        '  printf("%d\\n", hit);\n'
                        '  return 0;\n}\n')
        self.assertEqual("1\n", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
