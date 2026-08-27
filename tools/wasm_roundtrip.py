#!/usr/bin/env python3
"""Round-trip tester for the wasm back end and wasm2c together.

For each program, four things happen:

    prog.c --cc-------------------------------> oracle binary
    prog.c --shivyc --target wasm------------>  prog.wasm
    prog.wasm --tools/wasm2c.py-------------->  prog_back.c
    prog_back.c --cc------------------------->  round-trip binary

and the round-trip binary must agree with the oracle on both stdout and exit
status. That is a much stronger check than either half alone: a compiler bug
that the wasm difftest misses because the module happens to run correctly on
one engine still has to survive being read back, re-expressed as C, and
compiled by a different compiler entirely.

It also tests the two halves against each other. The encoder in
shivyc/wasm.py and the decoder in shivyc/wasm_reader.py were written from the
same specification but not from each other, so a misunderstanding on one side
shows up here as a decode failure or a wrong answer rather than passing
silently.

The corpus is imported from tools/wasm_difftest.py, so every case that file
gains is round-tripped too, with no second list to maintain.

    python3 tools/wasm_roundtrip.py            # whole corpus
    python3 tools/wasm_roundtrip.py -v         # show each case
    python3 tools/wasm_roundtrip.py prog.c     # one file
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

CC = os.environ.get("CC", "gcc")


def _run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


def corpus():
    """Every case from the difftest, as (name, wasm_source, native_source).

    The stdio cases carry a separate native source (they include <stdio.h>
    where the wasm side includes <wasi.h>); everything else uses one source
    for both.
    """
    import wasm_difftest as d
    out = []
    groups = (d.CORE + d.CONVERSIONS + d.MEMORY + d.FLOATS + d.VARIADIC
              + d.FUNCPTR + d.AGGREGATE + d.STATICADDR + d.BYVALUE)
    for entry in groups:
        out.append((entry[0], entry[1], entry[1]))
    for entry in d.STDIO:
        out.append((entry[0], entry[1], entry[3]))
    return out


# Cases whose round-trip is *expected* to differ from the native oracle,
# because the wasm semantics and the native ones genuinely differ. A
# round-trip that reproduced the native answer here would mean wasm2c had
# quietly dropped the wasm behaviour on the way through.
EXPECTED_DIVERGENT = {
    "wasm_f_overflow_conv":
        "out-of-range float->int saturates in wasm, wraps to INT_MIN natively",
}


def test_one(name, src, native_src, workdir, verbose):
    """Returns (status, detail)."""
    cpath = os.path.join(workdir, name + ".c")
    with open(cpath, "w") as f:
        f.write(src if src.endswith("\n") else src + "\n")
    npath = os.path.join(workdir, name + "_native.c")
    with open(npath, "w") as f:
        f.write(native_src if native_src.endswith("\n")
                else native_src + "\n")

    # 1. the oracle
    ora_bin = os.path.join(workdir, name + ".ora")
    rc, _, err = _run([CC, "-w", "-std=c99", npath, "-o", ora_bin, "-lm"])
    if rc != 0:
        return "ERROR", "oracle compile failed: %s" % err.strip()[:160]
    ora_rc, ora_out, _ = _run([ora_bin])

    # 2. C -> wasm
    wpath = os.path.join(workdir, name + ".wasm")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-o", wpath, "--target", "wasm"], cwd=ROOT)
    blob = out + err
    if "NotImplementedError" in blob:
        detail = "back end does not support this"
        for ln in blob.split("\n"):
            if "NotImplementedError:" in ln:
                detail = ln.split("NotImplementedError:", 1)[1].strip()
        return "SKIP", detail
    if rc != 0 or not os.path.exists(wpath):
        return "ERROR", "shivyc wasm failed: %s" % blob.strip()[:160]

    # 3. wasm -> C
    backpath = os.path.join(workdir, name + "_back.c")
    rc, out, err = _run([sys.executable, os.path.join(HERE, "wasm2c.py"),
                         wpath, "-o", backpath])
    if rc != 0:
        return "FAIL", "wasm2c failed: %s" % (out + err).strip()[:200]

    # 4. C -> native, and compare
    back_bin = os.path.join(workdir, name + ".back")
    rc, _, err = _run([CC, "-w", "-std=c99", "-I", HERE, backpath,
                       "-o", back_bin, "-lm"])
    if rc != 0:
        return "FAIL", "round-tripped C did not compile: %s" \
            % err.strip()[:200]
    rt_rc, rt_out, rt_err = _run([back_bin])

    if rt_out != ora_out:
        return "FAIL", ("stdout differs: ours=%r oracle=%r"
                        % (rt_out[:70], ora_out[:70]))
    if rt_rc != ora_rc:
        extra = (" trap: %s" % rt_err.strip()[:60]) if rt_err.strip() else ""
        return "FAIL", "exit differs: ours=%d oracle=%d%s" % (
            rt_rc, ora_rc, extra)
    return "PASS", "exit=%d%s" % (rt_rc,
                                  ", %d bytes out" % len(rt_out)
                                  if rt_out else "")


def main(argv):
    verbose = "-v" in argv
    files = [a for a in argv[1:] if not a.startswith("-")]

    rc, _, _ = _run([CC, "--version"])
    if rc != 0:
        print("missing toolchain: %s" % CC)
        return 2

    if files:
        progs = []
        for path in files:
            with open(path) as f:
                text = f.read()
            progs.append((os.path.basename(path).replace(".c", ""),
                          text, text))
    else:
        progs = corpus()

    workdir = tempfile.mkdtemp(prefix="wasmrt-")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0,
              "XFAIL": 0, "XPASS": 0}
    failures = []
    for name, src, native_src in progs:
        status, detail = test_one(name, src, native_src, workdir, verbose)
        if name in EXPECTED_DIVERGENT:
            if status == "FAIL":
                status = "XFAIL"
                detail = EXPECTED_DIVERGENT[name]
            elif status == "PASS":
                status = "XPASS"
                detail = ("now matches natively -- wasm semantics may have "
                          "been lost in translation")
        counts[status] += 1
        if verbose or status in ("FAIL", "ERROR"):
            print("  %-5s %-24s %s" % (status, name, detail))
        if status in ("FAIL", "ERROR"):
            failures.append(name)

    print("\nwasm round-trip: %d pass, %d fail, %d skip, %d error, "
          "%d expected-divergent"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"],
             counts["ERROR"], counts["XFAIL"]))
    if counts["XPASS"]:
        print("%d case(s) stopped diverging -- see EXPECTED_DIVERGENT"
              % counts["XPASS"])
    if failures:
        print("workdir kept for inspection: %s" % workdir)
    return 1 if (counts["FAIL"] or counts["ERROR"]
                 or counts["XPASS"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
