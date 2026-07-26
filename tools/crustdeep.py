#!/usr/bin/env python3
"""crustdeep -- what is *actually* stopping each file, and what would fix it.

`crustos.py survey` ranks the first error message per file. That is the wrong
measurement once every file has several blockers: it says which feature Crust
trips over first, not which feature would unlock anything. Three rounds of
picking the top of that ranking produced features that each came off the list
while the total barely moved.

This tool measures the other thing. For every file it scans for *all* the
constructs Crust cannot handle, then answers two questions the ranking cannot:

  * How many distinct blockers does a typical failing file have?
  * Which *set* of features, implemented together, actually unlocks files?

The second is a set-cover: a file is only unlocked when every one of its
blockers is gone, so the payoff of a feature depends entirely on what else is
already done. Greedy set-cover over the real data is a far better roadmap than
a frequency ranking.

    python3 tools/crustdeep.py ~/redox-kernel ~/redox-relibc
    python3 tools/crustdeep.py ~/redox-kernel --sample 12
    python3 tools/crustdeep.py ~/redox-kernel --near-miss

Detection is syntactic and deliberately conservative: it is looking for
evidence that a construct is present, not proving that it is. Counts are a
guide for deciding what to build, not a specification.
"""

import argparse
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import shivyc.crust as crust                                  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "tools"))
import crustos                                                # noqa: E402


def _strip(src):
    """Blank comments and string literals so they cannot create false hits."""
    return crust._blank(src)


# Each entry is (label, regex). A file "needs" a feature if the pattern hits.
# Ordered roughly by how self-contained the work would be.
FEATURES = [
    ("data-enum",
     re.compile(r"enum\s+\w+[^{]*\{[^}]*?\w+\s*[({]", re.S)),
    ("lifetimes",
     re.compile(r"&\s*'\w+|<\s*'\w+|'\w+\s*[,>]")),
    ("dyn-trait",
     re.compile(r"\bdyn\s+\w")),
    ("impl-trait-ret",
     re.compile(r"->\s*impl\s+\w")),
    ("assoc-type",
     re.compile(r"^\s*type\s+\w+\s*(=|;|:)", re.M)),
    ("where-clause",
     re.compile(r"\bwhere\s+\w+\s*:")),
    ("iterator-chain",
     re.compile(r"\.(iter|into_iter|map|filter|collect|fold|for_each|"
                r"chain|zip|enumerate|rev|any|all|find|count|sum)\s*\(")),
    ("match-binding",
     re.compile(r"=>|\bSome\s*\(\s*\w+\s*\)\s*=>|\b\w+\s*\(\s*\w+\s*\)\s*=>")),
    ("closure-capture",
     re.compile(r"\|\s*\w*\s*\|\s*[^;{]*\b(self|move)\b|move\s*\|")),
    ("module",
     re.compile(r"^\s*(pub\s+)?mod\s+\w+\s*\{", re.M)),
    ("use-import",
     re.compile(r"^\s*(pub\s+)?use\s+\w", re.M)),
    ("string-type",
     re.compile(r"\bString\b|\.to_string\s*\(|\bformat!\s*\(")),
    ("std-generic",
     re.compile(r"\b(Arc|Rc|Mutex|RwLock|HashMap|BTreeMap|BTreeSet|VecDeque|"
                r"RefCell|UnsafeCell|NonNull|MaybeUninit|Weak|Cow)\s*<")),
    ("operator-trait",
     re.compile(r"impl\s+(core::ops::|ops::|std::ops::)?"
                r"(Add|Sub|Mul|Div|Rem|Not|BitAnd|BitOr|BitXor|Shl|Shr|"
                r"Index|IndexMut|Deref|DerefMut|PartialEq|PartialOrd|Ord|Eq)"
                r"\b")),
    ("derive",
     re.compile(r"#\s*\[\s*derive\s*\(")),
    ("const-generic",
     re.compile(r"<\s*const\s+\w+\s*:")),
    ("async",
     re.compile(r"\basync\s+(fn|move|\{)|\.await\b")),
    ("inline-asm",
     re.compile(r"\b(asm|naked_asm|global_asm)\s*!")),
    ("union",
     re.compile(r"^\s*(pub\s+)?union\s+\w+\s*\{", re.M)),
    ("struct-update",
     re.compile(r"\.\.\s*\w+\s*\}")),
    ("nested-generic",
     re.compile(r"<\s*\w+\s*<")),
]


def blockers_of(src):
    """The set of unsupported constructs a source file uses."""
    scan = _strip(src)
    return {label for label, pat in FEATURES if pat.search(scan)}


def analyse(paths):
    """Classify every file and record its blocker set."""
    rows = []
    for p in paths:
        try:
            src = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        res = crustos.classify(p)
        rows.append((p, res.outcome, blockers_of(src)))
    return rows


def report(rows, top):
    failing = [r for r in rows if r[1] in (crustos.FAILED, crustos.EMPTY)]
    ok = [r for r in rows if r[1] in (crustos.TRANSLATED, crustos.PARTIAL)]
    print("%d files: %d with a lowered item, %d without"
          % (len(rows), len(ok), len(failing)))

    if not failing:
        return
    sizes = collections.Counter(len(b) for _, _, b in failing)
    print("\nblockers per failing file:")
    total = sum(sizes.values())
    running = 0
    for n in sorted(sizes):
        running += sizes[n]
        print("  %2d blocker%s  %4d files  %5.1f%%   (cumulative %5.1f%%)"
              % (n, " " if n == 1 else "s", sizes[n],
                 100.0 * sizes[n] / total, 100.0 * running / total))
    mean = sum(len(b) for _, _, b in failing) / len(failing)
    print("  mean %.1f blockers per failing file" % mean)

    print("\nfrequency (how often a feature appears at all):")
    freq = collections.Counter()
    for _, _, b in failing:
        freq.update(b)
    for label, n in freq.most_common(top):
        print("  %4d  %5.1f%%  %s" % (n, 100.0 * n / len(failing), label))

    # The number that actually matters: files blocked *only* by this feature.
    print("\nsole blocker (files this feature would unlock on its own):")
    sole = collections.Counter()
    for _, _, b in failing:
        if len(b) == 1:
            sole.update(b)
    if sole:
        for label, n in sole.most_common(top):
            print("  %4d  %s" % (n, label))
    else:
        print("  none -- no failing file is blocked by a single feature")

    # Greedy set cover: repeatedly take the feature that unlocks the most
    # files given everything already chosen.
    print("\ngreedy set cover (cumulative files unlocked):")
    remaining = [set(b) for _, _, b in failing]
    done, step = set(), 0
    while step < top:
        best, best_gain = None, 0
        for label in freq:
            if label in done:
                continue
            gain = sum(1 for b in remaining if b and b <= (done | {label}))
            if gain > best_gain:
                best, best_gain = label, gain
        if not best or best_gain == 0:
            break
        done.add(best)
        remaining = [b for b in remaining if not (b and b <= done)]
        step += 1
        print("  +%-18s unlocks %4d  (total %4d of %d)"
              % (best, best_gain, len(failing) - len(remaining), len(failing)))


def near_miss(rows, top):
    """Files that are one or two features away from working."""
    print("\nnearest misses (fewest blockers first):")
    cand = [(len(b), p, b) for p, o, b in rows
            if o in (crustos.FAILED, crustos.EMPTY) and b]
    for n, p, b in sorted(cand)[:top]:
        print("  %d  %-52s  %s"
              % (n, os.path.relpath(p)[-52:], ", ".join(sorted(b))))


def sample(rows, n):
    """Print a readable sample of failing files with their first error."""
    print("\nsample of failing files:")
    fails = [(p, b) for p, o, b in rows if o == crustos.FAILED]
    step = max(1, len(fails) // max(n, 1))
    for p, b in fails[::step][:n]:
        res = crustos.classify(p)
        msg = (res.error or "").strip().replace("\n", " ")[:70]
        print("\n  %s" % os.path.relpath(p))
        print("    first error : %s" % msg)
        print("    all blockers: %s" % (", ".join(sorted(b)) or "(none found)"))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--top", type=int, default=14)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--near-miss", action="store_true")
    args = ap.parse_args(argv)

    paths = list(crustos.iter_sources(args.roots))
    rows = analyse(paths)
    report(rows, args.top)
    if args.near_miss:
        near_miss(rows, args.top)
    if args.sample:
        sample(rows, args.sample)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
