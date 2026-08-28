#!/usr/bin/env python3
"""Differential test for reference types (funcref / externref).

Builds small modules that use the reference instructions, then checks that
each one gives the same answer under node and under its wasm2c translation.

Reference types are what most current Rust and wasm-bindgen output relies on,
and before this they were the decoder's hard stop. The surface is small --
`ref.null`, `ref.is_null`, `ref.func`, the table instructions, and the seven
element-segment encodings the proposal added -- but almost none of it existed
in the MVP, so almost none of it was exercised by anything already here.

The modules are hand-encoded rather than compiled, because Crust's own back
end emits none of this. That has the useful side effect of also testing the
element-segment decoder against forms no other test produces.

    python3 tools/wasm_ref_difftest.py [-v]
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import shivyc.wasm as w                                       # noqa: E402

CC = os.environ.get("CC", "gcc")
NODE = os.environ.get("NODE", "node")

# Reference-type opcodes, spelled out so the test reads as what it builds.
OP_TABLE_GET = 0x25
OP_TABLE_SET = 0x26
OP_REF_NULL = 0xD0
OP_REF_IS_NULL = 0xD1
OP_REF_FUNC = 0xD2
FC_TABLE_INIT = 12
FC_ELEM_DROP = 13
FC_TABLE_COPY = 14
FC_TABLE_GROW = 15
FC_TABLE_SIZE = 16
FC_TABLE_FILL = 17


def fc(body, sub, *operands):
    body.code.append(w.BULK_PREFIX)
    body.code.extend(w.uleb(sub))
    for o in operands:
        body.code.extend(w.uleb(o))


def ref_null(body, reftype=w.EXTERNREF):
    body.code.append(OP_REF_NULL)
    body.code.append(reftype)


def table_get(body, t):
    body.code.append(OP_TABLE_GET)
    body.code.extend(w.uleb(t))


def table_set(body, t):
    body.code.append(OP_TABLE_SET)
    body.code.extend(w.uleb(t))


def ref_func(body, index):
    body.code.append(OP_REF_FUNC)
    body.code.extend(w.uleb(index))


def mod_with_extern_table(build_body, minimum=2, maximum=8):
    """A module with one externref table and a `run` export."""
    m = w.WasmModule()
    m.needs_table = True
    m.table_elem_type = w.EXTERNREF
    m.extra_tables.append((w.EXTERNREF, minimum, maximum))
    m.declare_func("run", [], [w.I32])
    b = w.FuncBody()
    build_body(b)
    m.set_body("run", b)
    m.export_func("run")
    return w.module_bytes(m)


def case_table_size(b):
    fc(b, FC_TABLE_SIZE, 1)


def case_null_is_null(b):
    ref_null(b)
    b.code.append(OP_REF_IS_NULL)


def case_get_is_null(b):
    # An externref table starts full of nulls.
    b.const_i32(1)
    table_get(b, 1)
    b.code.append(OP_REF_IS_NULL)


def case_grow(b):
    ref_null(b)
    b.const_i32(3)
    fc(b, FC_TABLE_GROW, 1)          # returns the previous size


def case_grow_then_size(b):
    ref_null(b)
    b.const_i32(3)
    fc(b, FC_TABLE_GROW, 1)
    b.op(w.OP_DROP)
    fc(b, FC_TABLE_SIZE, 1)


def case_grow_past_max(b):
    # Growing beyond the declared maximum must fail with -1, not trap.
    ref_null(b)
    b.const_i32(100)
    fc(b, FC_TABLE_GROW, 1)


def case_fill_then_get(b):
    b.const_i32(0)
    ref_null(b)
    b.const_i32(2)
    fc(b, FC_TABLE_FILL, 1)
    b.const_i32(0)
    table_get(b, 1)
    b.code.append(OP_REF_IS_NULL)


def case_set_then_get(b):
    # Store a null and read it back: the only reference a module can make for
    # itself without a host handing it one.
    b.const_i32(0)
    ref_null(b)
    table_set(b, 1)
    b.const_i32(0)
    table_get(b, 1)
    b.code.append(OP_REF_IS_NULL)


def case_copy(b):
    b.const_i32(0)
    b.const_i32(1)
    b.const_i32(1)
    fc(b, FC_TABLE_COPY, 1, 1)
    b.const_i32(0)
    table_get(b, 1)
    b.code.append(OP_REF_IS_NULL)


def build_funcref_cases():
    """Modules whose table holds functions, exercised through call_indirect
    and ref.func."""
    out = []

    # ref.func into a table slot, then an indirect call through it.
    m = w.WasmModule()
    m.declare_func("target", [w.I32], [w.I32])
    tb = w.FuncBody()
    tb.local_get(0)
    tb.const_i32(7)
    tb.op(w.I32_BIN["mul"])
    m.set_body("target", tb)

    m.declare_func("run", [], [w.I32])
    b = w.FuncBody()
    b.const_i32(1)
    ref_func(b, m.func_index("target"))
    table_set(b, 0)
    b.const_i32(6)
    b.const_i32(1)
    b.call_indirect(m.type_index([w.I32], [w.I32]))
    m.set_body("run", b)
    m.export_func("run")
    m.needs_table = True
    m.table_entries.append("target")      # reserve room for slot 1
    out.append(("funcref_set_call", w.module_bytes(m)))

    # A null slot must trap on call, not dispatch somewhere.
    m2 = w.WasmModule()
    m2.declare_func("target", [w.I32], [w.I32])
    tb2 = w.FuncBody()
    tb2.local_get(0)
    m2.set_body("target", tb2)
    m2.declare_func("run", [], [w.I32])
    b2 = w.FuncBody()
    b2.const_i32(5)
    b2.const_i32(0)                       # slot 0 is the reserved null
    b2.call_indirect(m2.type_index([w.I32], [w.I32]))
    m2.set_body("run", b2)
    m2.export_func("run")
    m2.needs_table = True
    m2.table_entries.append("target")
    out.append(("funcref_null_traps", w.module_bytes(m2)))

    # A table slot holding a function of the *wrong shape*, called through a
    # signature it does not have. Engines check this at run time and trap;
    # generated C that only cast the pointer would make the call anyway, with
    # arguments read from wherever the ABI happened to leave them. This is
    # the case that check exists for.
    m3 = w.WasmModule()
    m3.declare_func("two", [w.I32, w.I32], [w.I32])
    tb3 = w.FuncBody()
    tb3.local_get(0)
    tb3.local_get(1)
    tb3.op(w.I32_BIN["add"])
    m3.set_body("two", tb3)
    m3.declare_func("run", [], [w.I32])
    b3 = w.FuncBody()
    b3.const_i32(5)
    b3.const_i32(1)
    b3.call_indirect(m3.type_index([w.I32], [w.I32]))
    m3.set_body("run", b3)
    m3.export_func("run")
    m3.needs_table = True
    m3.table_entries.append("two")
    out.append(("funcref_type_mismatch_traps", w.module_bytes(m3)))
    return out


EXTERN_CASES = [
    ("table_size", case_table_size),
    ("null_is_null", case_null_is_null),
    ("get_is_null", case_get_is_null),
    ("grow", case_grow),
    ("grow_then_size", case_grow_then_size),
    ("grow_past_max", case_grow_past_max),
    ("fill_then_get", case_fill_then_get),
    ("set_then_get", case_set_then_get),
    ("copy", case_copy),
]

NODE_RUNNER = r"""
const fs = require('fs');
(async () => {
  try {
    const m = await WebAssembly.compile(fs.readFileSync(process.argv[2]));
    const i = await WebAssembly.instantiate(m, {});
    process.stdout.write(String(i.exports.run() | 0));
  } catch (e) {
    process.stdout.write('TRAP');
  }
})();
"""


def _run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)
    return p.returncode, p.stdout, p.stderr


def test_one(name, blob, workdir, runner):
    wpath = os.path.join(workdir, name + ".wasm")
    with open(wpath, "wb") as f:
        f.write(blob)

    rc, out, err = _run([NODE, runner, wpath])
    if rc != 0:
        return "ERROR", "node rejected the module: %s" % err.strip()[:120]
    expected = out.strip()

    cpath = os.path.join(workdir, name + ".c")
    rc, out, err = _run([sys.executable, os.path.join(HERE, "wasm2c.py"),
                         wpath, "-o", cpath, "--no-main"])
    if rc != 0:
        return "FAIL", "wasm2c: %s" % (out + err).strip()[:160]
    with open(cpath, "a") as f:
        f.write('#include <stdio.h>\n'
                'extern u32 w2c_export_run(void);\n'
                'int main(void){ wasm_init();'
                ' printf("%d", (int)(s32)w2c_export_run()); return 0; }\n')

    binp = os.path.join(workdir, name + ".bin")
    rc, _, err = _run([CC, "-w", "-std=c99", "-I", HERE, cpath,
                       "-o", binp, "-lm"])
    if rc != 0:
        return "FAIL", "translated C did not compile: %s" % err.strip()[:200]
    rc, got, _ = _run([binp])
    got = got.strip() if rc == 0 else "TRAP"
    if got != expected:
        return "FAIL", "node=%s wasm2c=%s" % (expected, got)
    return "PASS", "= %s" % expected


def main(argv):
    verbose = "-v" in argv
    for tool in (NODE, CC):
        rc, _, _ = _run([tool, "--version"])
        if rc != 0:
            print("missing toolchain: %s" % tool)
            return 2

    workdir = tempfile.mkdtemp(prefix="wasmref-")
    runner = os.path.join(workdir, "run.js")
    with open(runner, "w") as f:
        f.write(NODE_RUNNER)

    cases = []
    for name, builder in EXTERN_CASES:
        cases.append((name, mod_with_extern_table(builder)))
    cases.extend(build_funcref_cases())

    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for name, blob in cases:
        status, detail = test_one(name, blob, workdir, runner)
        counts[status] += 1
        if verbose or status != "PASS":
            print("  %-5s %-22s %s" % (status, name, detail))

    print("\nreference-type difftest: %d pass, %d fail, %d error"
          % (counts["PASS"], counts["FAIL"], counts["ERROR"]))
    return 1 if (counts["FAIL"] or counts["ERROR"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
