#!/usr/bin/env python3
"""CrustOS ELF load + reg_class switch microbenchmark.

Builds hello_guest, optionally stamps a CRUSTOS note, times load+describe+run
under the hosted crustos binary (must already be built, or builds via crustos.py).
"""
from __future__ import print_function

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build", "crustos")


def main():
    os.makedirs(BUILD, exist_ok=True)
    # Ensure crustos + guest exist
    subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "crustos.py"),
                           "build"], cwd=ROOT)
    guest = os.path.join(BUILD, "hello_guest.so")
    hinted = os.path.join(BUILD, "hello_guest_hinted.so")
    subprocess.check_call([sys.executable,
                           os.path.join(ROOT, "tools", "crust_elf_note.py"),
                           guest, "--reg-class", "1", "-o", hinted])
    binary = os.path.join(BUILD, "crustos")
    results = []
    for label, path in [("full_default", guest), ("hint_extra", hinted)]:
        env = os.environ.copy()
        env["CRUSTOS_ELF"] = path
        best = float("inf")
        out = ""
        for _ in range(3):
            t0 = time.perf_counter()
            p = subprocess.run([binary], capture_output=True, text=True, env=env)
            best = min(best, time.perf_counter() - t0)
            out = p.stdout
            if p.returncode != 0:
                print("FAIL", label, p.stderr or out)
                return 1
        results.append((label, best, out))
        print("%s: %.4fs" % (label, best))
        for line in out.splitlines():
            if "elf " in line or "reg_class" in line:
                print(" ", line)
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
