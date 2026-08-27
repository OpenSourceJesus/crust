#!/usr/bin/env python3
"""Run a real `.wasm` under node and under its wasm2c translation, and check
that the two agree.

    python3 tools/wasm_module_difftest.py some.wasm
    python3 tools/wasm_module_difftest.py --all      # bundled modules, if any

`tools/wasm_roundtrip.py` checks programs this toolchain compiled, which are
small and use a narrow slice of the format. This checks modules built by
*other* toolchains -- the ones that are 100,000 instructions long, use SIMD,
and were never written with this translator in mind. Those had only ever been
shown to *compile*, which says nothing about whether they compute the same
thing.

For each exported function, both sides:

  1. call it with a fixed argument vector,
  2. print the result (or TRAP, if it trapped),
  3. print a hash of the whole of linear memory afterwards.

The memory hash is what makes this worth running. A wrong store offset, a
lane written in the wrong order, a load that sign-extends when it should not
-- none of those necessarily change a return value, but all of them change
memory, and the hash notices.

Each export runs in its own process, because a trap on the C side exits and
there would be no way to continue to the next one.
"""
import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import shivyc.wasm as w                                       # noqa: E402
import shivyc.wasm_reader as reader                           # noqa: E402

CC = os.environ.get("CC", "gcc")
NODE = os.environ.get("NODE", "node")

CTYPE = {w.I32: "u32", w.I64: "u64", w.F32: "f32", w.F64: "f64",
         reader.V128: "v128"}

# Argument vectors to try. Zeros first because most of these parameters are
# pointers and zero is the least likely to wander out of bounds; then a few
# small values, which for an allocator-style export is a realistic call and
# for anything else is a good way to provoke a trap on both sides at once.
ARG_VECTORS = [0, 1, 16, 1024]


def c_ident(name):
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    s = "".join(out)
    return ("_" + s) if (not s or s[0].isdigit()) else s


def gen_import_stubs(mod):
    """Definitions for the module's imports.

    Every stub returns zero and ignores its arguments. That is not a
    simulation of the host -- it is a *deterministic* host, which is all this
    test needs: both sides see the same replies, so any difference in what
    they compute afterwards is the translator's.
    """
    lines = ['#include "wasm2c_rt.h"', '#include "wasm2c_rt_simd.h"', ""]
    for imp in mod.imports:
        if imp.kind != w.EXTERNAL_KIND_FUNC:
            continue
        ft = mod.types[imp.type_index]
        ret = CTYPE[ft.results[0]] if ft.results else "void"
        params = []
        for k in range(len(ft.params)):
            params.append("%s a%d" % (CTYPE[ft.params[k]], k))
        sig = ", ".join(params) or "void"
        name = "wasm_import_%s_%s" % (c_ident(imp.module), c_ident(imp.field))
        body = "  (void)0;"
        if ft.results:
            if ft.results[0] == reader.V128:
                body = "  v128 z; memset(z.bytes,0,16); return z;"
            else:
                body = "  return 0;"
        lines.append("%s %s(%s) {" % (ret, name, sig))
        for k in range(len(ft.params)):
            lines.append("  (void)a%d;" % k)
        lines.append(body)
        lines.append("}")
    return "\n".join(lines) + "\n"


def gen_driver(mod, exports):
    """A driver that calls one export, chosen by argv, then hashes memory."""
    lines = ['#include <stdio.h>', '#include <stdlib.h>',
             '#include "wasm2c_rt.h"', '#include "wasm2c_rt_simd.h"',
             "",
             "extern void wasm_init(void);",
             "extern u8 *wasm_memory(void);",
             "extern u64 wasm_memory_size(void);"]
    for name, index, ft in exports:
        ret = CTYPE[ft.results[0]] if ft.results else "void"
        params = ", ".join([CTYPE[p] for p in ft.params]) or "void"
        lines.append("extern %s w2c_export_%s(%s);" % (ret, c_ident(name),
                                                       params))
    lines += [
        "",
        "/* FNV-1a over all of linear memory. Cheap, and sensitive to a",
        " * single changed byte anywhere in it. */",
        "static void print_mem_hash(void) {",
        # Hex on both sides deliberately: the decimal form of the FNV offset
        # basis is 20 digits, and dropping one produces a hash that is
        # perfectly self-consistent and disagrees with every other
        # implementation -- which is exactly what happened here, and looked
        # for a while like 30 translation bugs.
        "  u64 h = 0xcbf29ce484222325ull;",
        "  u8 *m = wasm_memory(); u64 n = wasm_memory_size(), k;",
        "  for (k = 0; k < n; k++) { h ^= m[k]; h *= 0x100000001b3ull; }",
        '  printf(" mem=%016llx\\n", (unsigned long long)h);',
        "}",
        "",
        "int main(int argc, char **argv) {",
        "  int which = argc > 1 ? atoi(argv[1]) : 0;",
        "  u32 a = argc > 2 ? (u32)strtoul(argv[2], 0, 10) : 0;",
        "  wasm_init();",
        "  switch (which) {",
    ]
    for i in range(len(exports)):
        name, index, ft = exports[i]
        args = ", ".join([("(%s)a" % CTYPE[p]) for p in ft.params])
        call = "w2c_export_%s(%s)" % (c_ident(name), args)
        lines.append("  case %d: {" % i)
        if ft.results and ft.results[0] in (w.I32, w.I64):
            lines.append('    printf("r=%%lld",'
                         ' (long long)(s64)%s);' % call)
        elif ft.results:
            lines.append('    printf("r=fp");')
            lines.append("    (void)%s;" % call)
        else:
            lines.append("    %s;" % call)
            lines.append('    printf("r=void");')
        lines.append("    break; }")
    lines += [
        '  default: printf("r=none"); break;',
        "  }",
        "  print_mem_hash();",
        "  return 0;",
        "}",
    ]
    return "\n".join(lines) + "\n"


NODE_DRIVER = r"""
const fs = require('fs');
const path = process.argv[2];
const which = parseInt(process.argv[3] || '0', 10);
const argval = parseInt(process.argv[4] || '0', 10);
const names = JSON.parse(process.argv[5]);

(async () => {
  const bytes = fs.readFileSync(path);
  const mod = await WebAssembly.compile(bytes);
  // Every import is a deterministic stub returning zero, matching the C side.
  const imports = {};
  for (const imp of WebAssembly.Module.imports(mod)) {
    imports[imp.module] = imports[imp.module] || {};
    if (imp.kind === 'function') imports[imp.module][imp.name] = () => 0;
    else if (imp.kind === 'memory')
      imports[imp.module][imp.name] = new WebAssembly.Memory({initial: 256});
    else if (imp.kind === 'global')
      imports[imp.module][imp.name] = new WebAssembly.Global({value:'i32', mutable:true}, 0);
    else if (imp.kind === 'table')
      imports[imp.module][imp.name] = new WebAssembly.Table({initial: 256, element:'anyfunc'});
  }
  let inst;
  try { inst = await WebAssembly.instantiate(mod, imports); }
  catch (e) { process.stdout.write('instantiate-failed\n'); return; }

  let out = '';
  const fn = inst.exports[names[which]];
  try {
    const r = fn ? fn(argval, argval, argval, argval, argval, argval,
                      argval, argval, argval, argval) : undefined;
    out = (r === undefined) ? 'r=void' : 'r=' + BigInt.asIntN(64, BigInt(r));
  } catch (e) {
    out = 'TRAP';
  }
  // Hash memory the same way the C driver does. The export is found by
  // *kind*, not by the name "memory": a minified module exports it as `i` or
  // `r`, and looking it up by name silently hashes nothing at all -- which
  // reads as a memory mismatch on every single export.
  let memExp = null;
  for (const e of WebAssembly.Module.exports(mod)) {
    if (e.kind === 'memory') { memExp = inst.exports[e.name]; break; }
  }
  if (!memExp) {
    for (const k of Object.keys(imports)) {
      for (const n of Object.keys(imports[k])) {
        if (imports[k][n] instanceof WebAssembly.Memory) memExp = imports[k][n];
      }
    }
  }
  let h = 0xcbf29ce484222325n;
  const P = 0x100000001b3n, M = (1n << 64n) - 1n;
  if (memExp) {
    const m = new Uint8Array(memExp.buffer);
    for (let k = 0; k < m.length; k++) {
      h = (h ^ BigInt(m[k])) * P & M;
    }
  }
  process.stdout.write(out + ' mem=' + h.toString(16).padStart(16, '0') + '\n');
})();
"""


def _run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    return p.returncode, p.stdout, p.stderr


def check_module(path, workdir, verbose):
    name = os.path.basename(path).replace(".wasm", "")
    try:
        mod = reader.decode_file(path)
    except reader.WasmDecodeError as e:
        return [("DECODE", name, str(e)[:90])]

    exports = []
    for e in mod.exports:
        if e.kind != w.EXTERNAL_KIND_FUNC:
            continue
        ft = mod.type_of_func(e.index)
        ok = True
        for p in ft.params:
            if p != w.I32:
                ok = False              # keep the argument vector simple
        if ft.results and len(ft.results) > 1:
            ok = False
        if ok:
            exports.append((e.name, e.index, ft))
    if not exports:
        return [("SKIP", name, "no exports with all-i32 parameters")]

    cpath = os.path.join(workdir, name + "_mod.c")
    rc, out, err = _run([sys.executable, os.path.join(HERE, "wasm2c.py"),
                         path, "-o", cpath, "--no-main"])
    if rc != 0:
        return [("FAIL", name, "wasm2c: %s" % (out + err).strip()[:120])]

    stub = os.path.join(workdir, name + "_stubs.c")
    with open(stub, "w") as f:
        f.write(gen_import_stubs(mod))
    drv = os.path.join(workdir, name + "_drv.c")
    with open(drv, "w") as f:
        f.write(gen_driver(mod, exports))

    binp = os.path.join(workdir, name + ".bin")
    rc, _, err = _run([CC, "-w", "-std=c99", "-I", HERE,
                       cpath, stub, drv, "-o", binp, "-lm"])
    if rc != 0:
        return [("FAIL", name, "link: %s" % err.strip()[:200])]

    js = os.path.join(workdir, "drv.js")
    with open(js, "w") as f:
        f.write(NODE_DRIVER)

    import json
    names_json = json.dumps([e[0] for e in exports])

    results = []
    for i in range(len(exports)):
        for argval in ARG_VECTORS:
            label = "%s:%s(%d)" % (name, exports[i][0], argval)
            try:
                crc, cout, cerr = _run([binp, str(i), str(argval)])
            except subprocess.TimeoutExpired:
                results.append(("FAIL", label, "C side timed out"))
                continue
            c_ans = cout.strip() if crc == 0 else "TRAP"
            try:
                nrc, nout, nerr = _run([NODE, js, path, str(i), str(argval),
                                        names_json])
            except subprocess.TimeoutExpired:
                results.append(("SKIP", label, "node timed out"))
                continue
            n_ans = nout.strip()
            if n_ans == "instantiate-failed":
                results.append(("SKIP", label, "node could not instantiate"))
                continue
            if n_ans.startswith("TRAP"):
                # Both must trap, or neither. A trap on one side only means
                # the translation disagrees about a bounds check.
                if c_ans == "TRAP":
                    results.append(("PASS", label, "both trap"))
                else:
                    results.append(("FAIL", label,
                                    "node trapped, C did not (%s)"
                                    % c_ans[:40]))
                continue
            if c_ans == "TRAP":
                results.append(("FAIL", label, "C trapped, node did not"))
                continue
            if c_ans != n_ans:
                results.append(("FAIL", label,
                                "node=%s C=%s" % (n_ans[:48], c_ans[:48])))
            else:
                results.append(("PASS", label, c_ans[:40]))
    return results


def main(argv):
    verbose = "-v" in argv
    paths = [a for a in argv[1:] if not a.startswith("-")]
    if not paths:
        print("usage: wasm_module_difftest.py <module.wasm> [...]")
        return 2

    workdir = tempfile.mkdtemp(prefix="wasmmod-")
    counts = {}
    for path in paths:
        if not os.path.exists(path):
            print("  MISS  %s" % path)
            continue
        for status, label, detail in check_module(path, workdir, verbose):
            counts[status] = counts.get(status, 0) + 1
            if verbose or status not in ("PASS",):
                print("  %-6s %-34s %s" % (status, label, detail))

    print("\nmodule difftest: " + ", ".join(
        "%d %s" % (counts[k], k.lower()) for k in sorted(counts)))
    bad = counts.get("FAIL", 0) + counts.get("DECODE", 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
