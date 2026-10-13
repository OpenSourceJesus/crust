#!/usr/bin/env python3
"""Print the IL the front end produces for a C file, one command per line.

A debugging aid for back-end work (the SPIR-V lowering was written against
this output): runs the normal pipeline up to code generation, then dumps
`il_code.commands` instead of generating anything.

    python3 tools/il_dump.py kernel.c [function ...]
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))


def describe(v):
    if v is None:
        return "None"
    lit = getattr(v, "literal", None)
    if lit is not None:
        return "lit(%r:%s)" % (lit.val, v.ctype)
    return "v%03d:%s" % (id(v) % 1000, v.ctype)


def fmt(cmd):
    parts = []
    for k in sorted(vars(cmd)):
        if k.startswith("_") or k in ("r",):
            continue
        val = getattr(cmd, k)
        if hasattr(val, "ctype"):
            parts.append("%s=%s" % (k, describe(val)))
        elif isinstance(val, (list, tuple)) and val and \
                hasattr(val[0], "ctype"):
            parts.append("%s=[%s]" % (k, ", ".join(describe(x) for x in val)))
        elif isinstance(val, (int, str, bool)) or val is None:
            parts.append("%s=%r" % (k, val))
    return "%-14s %s" % (type(cmd).__name__, " ".join(parts))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    wanted = argv[2:]
    import shivyc.asm_gen as asm_gen
    import shivyc.main as shivyc_main

    def dump(self):
        for fn in self.il_code.commands:
            if wanted and fn not in wanted:
                continue
            print("function %s" % fn)
            for c in self.il_code.commands[fn]:
                print("  " + fmt(c))
        return None

    asm_gen.ASMGen.make_asm = dump
    sys.argv = ["shivyc", "-S", path, "-o", os.devnull]
    try:
        shivyc_main.main()
    except SystemExit:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
