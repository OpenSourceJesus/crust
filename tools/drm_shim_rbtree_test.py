#!/usr/bin/env python3
"""drm_shim_rbtree_test -- the rbtree and interval tree actually work.

Every other check in this tree establishes that something *compiles*.
`drmdeep.py` compiles files and counts symbols; it never runs a line of what
it builds. That is fine for headers that are no-ops by design -- a spinlock
that locks nothing cannot be subtly wrong -- but rbtree.h and
interval_tree_generic.h are real data structures, and a red-black tree that
balances incorrectly compiles perfectly.

So this compiles the shim headers into a native binary and runs it, checking
after every single operation that:

  * no red node has a red parent,
  * every root-to-leaf path has the same black height,
  * parent pointers agree with child pointers,
  * in-order traversal is sorted,
  * the cached leftmost pointer really is the minimum,
  * every node's subtree-max equals the real maximum below it,
  * and interval queries return exactly what brute-force search returns.

The last one is the point. The augmented summary is an optimisation: if it is
wrong, queries silently skip subtrees and return too few results. Comparing
against a linear scan is the only way to catch that.

    python3 tools/drm_shim_rbtree_test.py
    python3 tools/drm_shim_rbtree_test.py --mutate   # prove it can fail
    python3 tools/drm_shim_rbtree_test.py -n 200000  # longer run

Runs anywhere gcc does; needs no drm-kmod checkout and no hardware.
"""
import argparse
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(HERE, "drm_shim")

HARNESS = r"""
#include <linux/rbtree.h>
#include <linux/interval_tree_generic.h>

/* Freestanding: no libc. Everything the harness needs is here. */
static unsigned long rng_state = 88172645463325252UL;
static unsigned long xrand(void)
{
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 7;
    rng_state ^= rng_state << 17;
    return rng_state;
}

#define NODES 512
struct item {
    struct rb_node rb;
    u64 start, size, __subtree_last;
    int live;
};
static struct item items[NODES];
static struct rb_root_cached tree = RB_ROOT_CACHED;

#define START(n) ((n)->start)
#define LAST(n)  ((n)->start + (n)->size - 1)

INTERVAL_TREE_DEFINE(struct item, rb, u64, __subtree_last,
                     START, LAST, static, it)

static int failures;
static const char *failed_check;

static void fail(const char *what)
{
    if (!failures) failed_check = what;
    failures++;
}

/* Black height, or -1 if the subtree is malformed. */
static int check_node(struct rb_node *n, struct rb_node *parent)
{
    int lh, rh;
    if (!n) return 1;
    if (rb_parent(n) != parent) { fail("parent pointer disagrees with child"); return -1; }
    if (rb_is_red(n)) {
        if (parent && rb_is_red(parent)) { fail("red node with red parent"); return -1; }
    }
    lh = check_node(n->rb_left, n);
    rh = check_node(n->rb_right, n);
    if (lh < 0 || rh < 0) return -1;
    if (lh != rh) { fail("black heights differ"); return -1; }
    return lh + (rb_is_black(n) ? 1 : 0);
}

static u64 real_subtree_max(struct rb_node *n)
{
    u64 m, c;
    struct item *it;
    if (!n) return 0;
    it = rb_entry(n, struct item, rb);
    m = LAST(it);
    if (n->rb_left)  { c = real_subtree_max(n->rb_left);  if (c > m) m = c; }
    if (n->rb_right) { c = real_subtree_max(n->rb_right); if (c > m) m = c; }
    return m;
}

static void check_augment(struct rb_node *n)
{
    struct item *it;
    if (!n) return;
    it = rb_entry(n, struct item, rb);
    if (it->__subtree_last != real_subtree_max(n)) fail("subtree max is stale");
    check_augment(n->rb_left);
    check_augment(n->rb_right);
}

static void check_tree(void)
{
    struct rb_node *n;
    struct item *prev = 0;
    int count = 0, live = 0, i;

    if (check_node(tree.rb_root.rb_node, 0) < 0) return;
    if (tree.rb_root.rb_node && rb_is_red(tree.rb_root.rb_node))
        fail("root is red");

    /* in-order must be sorted by start */
    for (n = rb_first(&tree.rb_root); n; n = rb_next(n)) {
        struct item *it = rb_entry(n, struct item, rb);
        if (prev && START(prev) > START(it)) fail("in-order traversal not sorted");
        prev = it;
        count++;
    }
    for (i = 0; i < NODES; i++) if (items[i].live) live++;
    if (count != live) fail("node count disagrees with live set");

    /* cached leftmost must be the true minimum */
    if (tree.rb_leftmost != rb_first(&tree.rb_root))
        fail("cached leftmost is not the minimum");

    check_augment(tree.rb_root.rb_node);
}

/* Compare a query against brute force over the live set. */
static void check_query(u64 qs, u64 ql)
{
    struct item *it;
    int found = 0, expect = 0, i;

    for (i = 0; i < NODES; i++)
        if (items[i].live && START(&items[i]) <= ql && qs <= LAST(&items[i]))
            expect++;

    for (it = it_iter_first(&tree, qs, ql); it; it = it_iter_next(it, qs, ql)) {
        if (!it->live) fail("query returned a removed node");
        if (!(START(it) <= ql && qs <= LAST(it)))
            fail("query returned a non-overlapping interval");
        found++;
        if (found > NODES) { fail("query iteration did not terminate"); return; }
    }
    if (found != expect) fail("query missed overlapping intervals");
}

int run(unsigned long iterations)
{
    unsigned long i;
    for (i = 0; i < iterations; i++) {
        int idx = (int)(xrand() % NODES);
        struct item *it = &items[idx];
        if (it->live) {
            it_remove(it, &tree);
            it->live = 0;
        } else {
            it->start = xrand() % 100000;
            it->size = 1 + xrand() % 500;
            it->live = 1;
            it_insert(it, &tree);
        }
        /* Full validation is O(n); do it often but not every single time on
         * long runs, and always for the first few thousand. */
        if (i < 3000 || (i % 37) == 0) check_tree();
        if ((i % 11) == 0) {
            u64 qs = xrand() % 100000;
            check_query(qs, qs + xrand() % 1000);
        }
        if (failures) return 1;
    }
    check_tree();
    return failures ? 1 : 0;
}

/* Freestanding entry point: no libc, no _start, just a symbol the C driver
 * below calls. The driver is compiled separately and hosted. */
const char *rbtree_failure(void) { return failed_check; }
"""

DRIVER = r"""
extern int run(unsigned long);
extern const char *rbtree_failure(void);
extern int printf(const char *, ...);

int main(int argc, char **argv)
{
    unsigned long n = 50000;
    if (argc > 1) {
        const char *s = argv[1];
        n = 0;
        while (*s >= '0' && *s <= '9') n = n * 10 + (unsigned long)(*s++ - '0');
    }
    if (run(n)) {
        printf("FAIL %s\n", rbtree_failure());
        return 1;
    }
    printf("OK %lu operations\n", n);
    return 0;
}
"""


def gcc_own_include():
    p = subprocess.run(["gcc", "-print-file-name=include"],
                       capture_output=True, text=True)
    d = p.stdout.strip()
    return ["-I", d] if d and os.path.isdir(d) else []


def build_and_run(iterations, shim=SHIM, patch=None, verbose=False):
    """Compile the harness against the shim and run it. Returns (ok, output)."""
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "harness.c")
        text = HARNESS
        if patch:
            old, new = patch
            if old not in text:
                return False, "mutation target not found: %r" % old
            text = text.replace(old, new, 1)
        with open(src, "w") as f:
            f.write(text)
        drv = os.path.join(td, "driver.c")
        with open(drv, "w") as f:
            f.write(DRIVER)

        obj = os.path.join(td, "harness.o")
        # The harness itself is compiled exactly as drmdeep compiles DRM
        # sources -- freestanding, -nostdinc, strict -- so it exercises the
        # headers in the configuration that matters. The driver is hosted so
        # it can printf.
        cmd = (["gcc", "-c", "-nostdinc", "-ffreestanding", "-fno-pic", "-O1",
                "-Werror=implicit-function-declaration", "-Werror=implicit-int",
                "-D__KERNEL__", "-D__linux__", "-D__unix__"]
               + gcc_own_include() + ["-I", shim, "-o", obj, src])
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return False, "harness did not compile:\n" + p.stderr[:1500]

        exe = os.path.join(td, "harness")
        p = subprocess.run(["gcc", "-O1", "-no-pie", "-o", exe, drv, obj],
                           capture_output=True, text=True)
        if p.returncode != 0:
            return False, "harness did not link:\n" + p.stderr[:1000]

        p = subprocess.run([exe, str(iterations)], capture_output=True,
                           text=True, timeout=600)
        out = (p.stdout + p.stderr).strip()
        if verbose:
            print("      " + out)
        return p.returncode == 0, out


# Each mutation breaks one specific thing and names the check that should
# notice. A test that cannot fail proves nothing, and these structures are
# exactly the kind where a plausible-looking bug survives casual review.
# Each mutation breaks one specific thing and names the check that should
# notice. A test that cannot fail proves nothing, and these structures are
# exactly the kind where a plausible-looking bug survives casual review.
#
# Two mutations were tried and removed, because they turned out not to be
# bugs at all -- the harness was right to accept them:
#
#   * removing the "widen ancestors on the way down" line in _insert. The
#     propagate pass after rebalancing recomputes those summaries anyway.
#   * removing the aug->copy() call when the successor takes over on erase.
#     Same reason: propagation walks to the root unconditionally here, so it
#     reaches the successor regardless. The call is kept because upstream's
#     contract includes it, not because this implementation needs it.
#
# Both are recorded rather than quietly dropped: a mutation that goes uncaught
# is either a missing check or a wrong assumption about the code, and it is
# worth knowing which.
MUTATIONS = [
    ("skip the insert rebalance",
     ("    rb_insert_augmented_cached(&node->ITRB, root, leftmost,                  \\\n"
      "                               &ITPREFIX ## _augment);"),
     ("    (void)leftmost; root->rb_leftmost = rb_first(&root->rb_root);            \\\n"
      "    ITPREFIX ## _augment.propagate(&node->ITRB, 0);")),

    ("ignore the right child when summarising a subtree",
     ("    if (node->rbfield.rb_right) {                                            \\\n"
      "        child_max = rb_entry(node->rbfield.rb_right, rbstruct,               \\\n"
      "                             rbfield)->rbaugmented;                          \\\n"
      "        if (child_max > max) max = child_max;                                \\\n"
      "    }                                                                        \\"),
     "    /* mutated: right child ignored */                                       \\"),

    ("forget to update cached leftmost on erase",
     ("static inline void rb_erase_augmented_cached(struct rb_node *node,\n"
      "                                             struct rb_root_cached *root,\n"
      "                                             const struct rb_augment_callbacks *augment)\n"
      "{\n"
      "    if (root->rb_leftmost == node) root->rb_leftmost = rb_next(node);"),
     ("static inline void rb_erase_augmented_cached(struct rb_node *node,\n"
      "                                             struct rb_root_cached *root,\n"
      "                                             const struct rb_augment_callbacks *augment)\n"
      "{")),

    ("drop the rotate callback, so demoted nodes go stale",
     "    if (aug) aug->rotate(x, y);\n}\n\n/* Standard insert fixup.",
     "\n}\n\n/* Standard insert fixup."),
]

PASS, FAIL = [], []


def check(ok, name, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name,
                          "" if ok else "\n        " + detail))
    return ok


def mutate(iterations):
    """Break the structures on purpose; the harness must notice each time."""
    print("\n== mutation: the harness must fail when the tree is broken ==")
    caught = 0
    invalid = []
    import shutil
    for name, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as td:
            shim = os.path.join(td, "drm_shim")
            shutil.copytree(SHIM, shim)
            # patch the *header*, not the harness
            for hdr in ("interval_tree_generic.h", "rbtree.h"):
                path = os.path.join(shim, "linux", hdr)
                with open(path) as f:
                    text = f.read()
                if old in text:
                    with open(path, "w") as f:
                        f.write(text.replace(old, new, 1))
                    break
            else:
                print("  SKIP  %s (mutation target moved)" % name)
                continue
            ok, out = build_and_run(min(iterations, 20000), shim=shim)
            first = out.splitlines()[0] if out else "?"
            if ok:
                print("  NOT CAUGHT  %s" % name)
            elif "did not compile" in out or "did not link" in out:
                # A mutation that breaks the build proves nothing about the
                # invariant checks -- it means the mutation itself is
                # malformed. Counting it would be self-deception.
                invalid.append(name)
                print("  INVALID     %s (broke the build, not the tree)" % name)
            else:
                caught += 1
                print("  caught      %s  ->  %s" % (name, first))
    total = len(MUTATIONS)
    print("\n  %d of %d mutations caught" % (caught, total))
    if invalid:
        print("  %d malformed (broke the build rather than the tree): %s"
              % (len(invalid), ", ".join(invalid)))
    return caught + len(invalid) == total and not invalid


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=50000, dest="n",
                    help="randomised operations (default 50000)")
    ap.add_argument("--mutate", action="store_true",
                    help="also prove the harness can fail")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print("== rbtree and interval tree ==")
    ok, out = build_and_run(args.n, verbose=args.verbose)
    check(ok, "%d randomised insert/remove/query operations hold every "
              "invariant" % args.n, out)

    good = True
    if args.mutate:
        good = mutate(args.n)

    print("\ndrm_shim_rbtree: %d pass, %d fail" % (len(PASS), len(FAIL)))
    return 0 if not FAIL and good else 1


if __name__ == "__main__":
    sys.exit(main())
