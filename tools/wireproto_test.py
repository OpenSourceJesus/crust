#!/usr/bin/env python3
"""FE/BE wireproto roundtrip (issue #15).

Compiles examples/wireproto/codec.c to native and wasm, then:

  1. Checks sizeof/offset exports agree on both sides.
  2. Encodes a WireReq on the native backend, feeds those bytes into the wasm
     frontend's decode + handle, and checks the WireRep.
  3. Encodes on wasm, handles on native (bytes on disk), checks the reply.

No HTML/JS glue generator and no `with frontend` syntax -- just the joint
compile + binary protocol slice the issue asks for first.
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CODEC = os.path.join(ROOT, "examples", "wireproto", "codec.c")
INCDIR = os.path.join(ROOT, "examples", "wireproto")
NODE = os.environ.get("NODE", "node")

# Host scratch in linear memory: above static data, below the shadow stack.
WASM_BUF = 4096


def _run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


def dual_compile(outdir):
    native = os.path.join(outdir, "codec")
    rc, out, err = _run([
        sys.executable, os.path.join(HERE, "dual_compile.py"),
        CODEC, "-I", INCDIR, "-o", native,
    ], cwd=ROOT)
    if rc != 0:
        raise SystemExit("dual_compile failed:\n" + out + err)
    return native, native + ".wasm"


# Native CLI driver: layout dump, encode, handle. Appended to codec.c after
# stripping codec's main so the example stays free of argv parsing.
_NATIVE_DRV = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void put_bytes(unsigned char *p, int n) {
    int i;
    for (i = 0; i < n; i++) {
        if (i) putchar(' ');
        printf("%02x", p[i]);
    }
    putchar('\n');
}

static int read_hex_line(unsigned char *buf, int cap) {
    int n = 0, v;
    while (n < cap && scanf("%x", &v) == 1) {
        buf[n++] = (unsigned char)v;
    }
    return n;
}

int main(int argc, char **argv) {
    unsigned char req[32];
    unsigned char rep[32];
    if (argc < 2) return 90;
    if (strcmp(argv[1], "layout") == 0) {
        printf("req_size %d\n", wire_req_size());
        printf("rep_size %d\n", wire_rep_size());
        printf("req_tag %d\n", wire_req_tag_off());
        printf("req_a %d\n", wire_req_a_off());
        printf("req_b %d\n", wire_req_b_off());
        printf("rep_tag %d\n", wire_rep_tag_off());
        printf("rep_result %d\n", wire_rep_result_off());
        return 0;
    }
    if (strcmp(argv[1], "encode") == 0) {
        int tag = atoi(argv[2]);
        int a = atoi(argv[3]);
        int b = atoi(argv[4]);
        wire_req_encode(req, tag, a, b);
        put_bytes(req, wire_req_size());
        return 0;
    }
    if (strcmp(argv[1], "handle") == 0) {
        int n = read_hex_line(req, 32);
        if (n < wire_req_size()) return 91;
        wire_handle(req, rep);
        put_bytes(rep, wire_rep_size());
        return 0;
    }
    return 92;
}
'''

_WASM_HOST = r'''
const fs = require('fs');
const mode = process.argv[2];
const wasmPath = process.argv[3];
let memory;

const wasi = {
  fd_write: () => 0,
  proc_exit: (c) => { throw Object.assign(new Error('exit'), { code: c }); },
  fd_read: () => 52, fd_close: () => 52, fd_seek: () => 52,
  fd_fdstat_get: () => 52, path_open: () => 52,
  environ_get: () => 52,
  environ_sizes_get: (c, s) => {
    const d = new DataView(memory.buffer);
    d.setUint32(c, 0, true); d.setUint32(s, 0, true); return 0;
  },
  args_get: () => 52,
  args_sizes_get: (a, s) => {
    const d = new DataView(memory.buffer);
    d.setUint32(a, 0, true); d.setUint32(s, 0, true); return 0;
  },
  random_get: () => 52, clock_time_get: () => 52,
};

function hexOf(ptr, n) {
  const b = new Uint8Array(memory.buffer, ptr, n);
  return Array.from(b).map(x => x.toString(16).padStart(2, '0')).join(' ');
}
function writeHex(ptr, hex) {
  const parts = hex.trim().split(/\s+/).filter(Boolean);
  const b = new Uint8Array(memory.buffer);
  for (let i = 0; i < parts.length; i++) b[ptr + i] = parseInt(parts[i], 16);
  return parts.length;
}

(async () => {
  const bytes = fs.readFileSync(wasmPath);
  const { instance } = await WebAssembly.instantiate(bytes, {
    wasi_snapshot_preview1: wasi,
    env: {},
  });
  memory = instance.exports.memory;
  const e = instance.exports;
  const REQ = %d;
  const REP = REQ + 64;
  const p = (x) => BigInt(x);

  if (mode === 'layout') {
    process.stdout.write(
      'req_size ' + e.wire_req_size() + '\n' +
      'rep_size ' + e.wire_rep_size() + '\n' +
      'req_tag ' + e.wire_req_tag_off() + '\n' +
      'req_a ' + e.wire_req_a_off() + '\n' +
      'req_b ' + e.wire_req_b_off() + '\n' +
      'rep_tag ' + e.wire_rep_tag_off() + '\n' +
      'rep_result ' + e.wire_rep_result_off() + '\n'
    );
    return;
  }
  if (mode === 'encode') {
    const tag = parseInt(process.argv[4], 10);
    const a = parseInt(process.argv[5], 10);
    const b = parseInt(process.argv[6], 10);
    e.wire_req_encode(p(REQ), tag, a, b);
    process.stdout.write(hexOf(REQ, e.wire_req_size()) + '\n');
    return;
  }
  if (mode === 'handle') {
    const hex = process.argv[4];
    writeHex(REQ, hex);
    e.wire_handle(p(REQ), p(REP));
    process.stdout.write(hexOf(REP, e.wire_rep_size()) + '\n');
    return;
  }
  process.stderr.write('unknown mode ' + mode + '\n');
  process.exit(2);
})().catch((err) => {
  process.stderr.write(String(err) + '\n');
  process.exit(1);
});
''' % WASM_BUF


def parse_layout(text):
    out = {}
    for line in text.strip().splitlines():
        k, v = line.split()
        out[k] = int(v)
    return out


def check(name, cond, detail=""):
    if cond:
        print("ok  ", name)
        return True
    print("FAIL", name, detail)
    return False


def main():
    fails = 0
    with tempfile.TemporaryDirectory(prefix="wireproto_") as tmp:
        native_codec, wasm = dual_compile(tmp)

        # Self-check both mains.
        rc, _, err = _run([native_codec])
        if not check("native codec main", rc == 0, err):
            fails += 1
        rc, _, err = _run([NODE, os.path.join(ROOT, "tools", "wasm_run.js"),
                           wasm])
        if not check("wasm codec main", rc == 0, err):
            fails += 1

        # Native CLI: codec without its main, plus argv driver.
        drv_c = os.path.join(tmp, "native_drv.c")
        with open(CODEC) as f:
            codec_src = f.read()
        codec_wo_main = codec_src.rsplit("int main(void)", 1)[0]
        with open(drv_c, "w") as f:
            f.write(codec_wo_main)
            f.write(_NATIVE_DRV)
        native_drv = os.path.join(tmp, "native_drv")
        rc, out, err = _run([
            sys.executable, "-m", "shivyc.main",
            drv_c, "-I", INCDIR, "-o", native_drv,
        ], cwd=ROOT)
        if rc != 0:
            print("FAIL native driver compile", out, err)
            return 1

        host_js = os.path.join(tmp, "host.js")
        with open(host_js, "w") as f:
            f.write(_WASM_HOST)

        rc, n_layout, err = _run([native_drv, "layout"])
        rc2, w_layout, err2 = _run([NODE, host_js, "layout", wasm])
        if rc != 0 or rc2 != 0:
            print("FAIL layout dump", err, err2)
            return 1
        nl, wl = parse_layout(n_layout), parse_layout(w_layout)
        if not check("layouts match", nl == wl, "native=%r wasm=%r" % (nl, wl)):
            fails += 1
        if not check("req is 12 bytes", nl.get("req_size") == 12):
            fails += 1
        if not check("rep is 8 bytes", nl.get("rep_size") == 8):
            fails += 1

        # Native encode -> wasm handle
        rc, req_hex, err = _run([native_drv, "encode", "3", "20", "22"])
        req_hex = req_hex.strip()
        if not check("native encode", rc == 0 and req_hex, err):
            fails += 1
        else:
            rc, rep_hex, err = _run(
                [NODE, host_js, "handle", wasm, req_hex])
            rep_hex = rep_hex.strip()
            # WIRE_SUM=4, result=42 -> little-endian ints
            expect = struct.pack("<ii", 4, 42)
            expect_hex = " ".join("%02x" % b for b in expect)
            if not check("native->wasm ADD",
                         rc == 0 and rep_hex == expect_hex,
                         "got %r want %r (%s)" % (rep_hex, expect_hex, err)):
                fails += 1

        # Wasm encode -> native handle
        rc, req_hex, err = _run(
            [NODE, host_js, "encode", wasm, "3", "100", "7"])
        req_hex = req_hex.strip()
        if not check("wasm encode", rc == 0 and req_hex, err):
            fails += 1
        else:
            rc, rep_hex, err = _run(
                [native_drv, "handle"],
                input=req_hex + "\n")
            rep_hex = rep_hex.strip()
            expect = struct.pack("<ii", 4, 107)
            expect_hex = " ".join("%02x" % b for b in expect)
            if not check("wasm->native ADD",
                         rc == 0 and rep_hex == expect_hex,
                         "got %r want %r (%s)" % (rep_hex, expect_hex, err)):
                fails += 1

        # Ping
        rc, req_hex, _ = _run([native_drv, "encode", "1", "0", "0"])
        rc, rep_hex, _ = _run(
            [NODE, host_js, "handle", wasm, req_hex.strip()])
        expect = struct.pack("<ii", 2, 0)
        expect_hex = " ".join("%02x" % b for b in expect)
        if not check("PING->PONG cross",
                     rep_hex.strip() == expect_hex,
                     rep_hex):
            fails += 1

    if fails:
        print("%d failure(s)" % fails)
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
