#!/usr/bin/env python3
"""Keep test_minire.py's embedded engine copy in sync with minire.py.

minipy has no module system, so test_minire.py is literally `minire.py` with a
self-test driver appended after the DRIVER_MARKER line. That duplication silently
rots the moment minire.py changes, so this script regenerates the embedded half.

  python3 sync_test_minire.py           # rewrite test_minire.py in place
  python3 sync_test_minire.py --check   # exit 1 if out of sync (for CI)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "minire.py")
TEST = os.path.join(HERE, "test_minire.py")
DRIVER_MARKER = "# ---- self-test driver (appended to a copy of minire.py) ---"


def build():
    engine = open(ENGINE, encoding="utf-8").read()
    test = open(TEST, encoding="utf-8").read()
    idx = test.find(DRIVER_MARKER)
    if idx < 0:
        sys.exit("sync_test_minire: driver marker not found in test_minire.py")
    driver = test[idx:]
    return engine.rstrip("\n") + "\n\n\n" + driver


def main():
    want = build()
    check = "--check" in sys.argv
    if check:
        if open(TEST, encoding="utf-8").read() != want:
            sys.exit("test_minire.py is out of sync with minire.py; "
                     "run: python3 tools/rpy_lib/sync_test_minire.py")
        print("test_minire.py: in sync with minire.py")
        return
    open(TEST, "w", encoding="utf-8").write(want)
    print("test_minire.py: regenerated from minire.py")


main()
