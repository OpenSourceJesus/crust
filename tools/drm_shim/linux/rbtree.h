#ifndef _SHIM_LINUX_RBTREE_H
#define _SHIM_LINUX_RBTREE_H
/* Red-black tree.
 *
 * Unlike most of this shim, this is a real data structure rather than a
 * no-op: drm_mm is a range allocator built on an augmented rbtree, and a stub
 * cannot stand in for it. Balancing, augmentation and the cached-leftmost
 * bookkeeping all have to actually work.
 *
 * That creates a problem the rest of the shim does not have. drmdeep only
 * *compiles* files -- it never runs them -- so a subtly wrong tree would pass
 * the survey and be reported as progress. This header is therefore covered by
 * an executable test (tools/drm_shim_rbtree_test.py) that re-checks the
 * red-black invariants after every operation and compares interval queries
 * against brute-force search over tens of thousands of randomised operations.
 * It is the only part of the shim whose *behaviour* is verified rather than
 * just its syntax.
 *
 * Layout follows Linux: the parent pointer and the colour bit share a word,
 * which is why rb_node is aligned and the colour is read through accessors.
 *
 * The types are defined before kernel.h is included -- see the note in
 * list.h; rb_root is embedded by value in structures kernel.h reaches. */
struct rb_node {
    unsigned long   __rb_parent_color;
    struct rb_node *rb_right;
    struct rb_node *rb_left;
} __attribute__((aligned(sizeof(long))));

struct rb_root { struct rb_node *rb_node; };

/* The "cached" variant keeps a pointer to the leftmost node so finding the
 * minimum is O(1) rather than O(log n). drm_mm relies on it for the
 * hole-address tree. */
struct rb_root_cached {
    struct rb_root  rb_root;
    struct rb_node *rb_leftmost;
};

#include <linux/kernel.h>

#define RB_RED   0
#define RB_BLACK 1

#define RB_ROOT ((struct rb_root){ NULL })
#define RB_ROOT_CACHED ((struct rb_root_cached){ { NULL }, NULL })
#define rb_entry(ptr, type, member) container_of(ptr, type, member)
#define rb_entry_safe(ptr, type, member) \
    ((ptr) ? rb_entry(ptr, type, member) : NULL)

#define rb_parent(r)   ((struct rb_node *)((r)->__rb_parent_color & ~3UL))
#define rb_color(r)    ((r)->__rb_parent_color & 1UL)
#define rb_is_red(r)   (!rb_color(r))
#define rb_is_black(r) (rb_color(r))

#define RB_EMPTY_ROOT(root) ((root)->rb_node == NULL)
#define RB_EMPTY_NODE(node) ((node)->__rb_parent_color == (unsigned long)(node))
#define RB_CLEAR_NODE(node) ((node)->__rb_parent_color = (unsigned long)(node))

static inline void rb_set_parent(struct rb_node *n, struct rb_node *p)
{
    n->__rb_parent_color = rb_color(n) | (unsigned long)p;
}

static inline void rb_set_parent_color(struct rb_node *n, struct rb_node *p,
                                       int color)
{
    n->__rb_parent_color = (unsigned long)p | (unsigned long)color;
}

static inline void rb_set_black(struct rb_node *n)
{
    n->__rb_parent_color |= RB_BLACK;
}

static inline void rb_set_red(struct rb_node *n)
{
    n->__rb_parent_color &= ~1UL;
}

/* A missing child counts as black -- the standard convention that lets the
 * rebalancing cases treat NULL uniformly. */
static inline int rb_is_black_or_nil(struct rb_node *n)
{
    return n == NULL || rb_is_black(n);
}

static inline void rb_link_node(struct rb_node *node, struct rb_node *parent,
                                struct rb_node **rb_link)
{
    node->__rb_parent_color = (unsigned long)parent;
    node->rb_left = node->rb_right = NULL;
    *rb_link = node;
}

static inline void __rb_change_child(struct rb_node *old, struct rb_node *new_,
                                     struct rb_node *parent,
                                     struct rb_root *root)
{
    if (parent) {
        if (parent->rb_left == old) parent->rb_left = new_;
        else parent->rb_right = new_;
    } else {
        root->rb_node = new_;
    }
}

/* Rotations must tell the augmentation layer what moved.
 *
 * A rotation demotes x and promotes y. y's subtree is exactly what x's was,
 * so y inherits x's summary verbatim; x then recomputes from its new
 * children. Without this, an insert that rotates at the grandparent leaves
 * that grandparent stale -- it is no longer on the path from the inserted
 * node to the root, so a simple upward walk skips it. That bug is invisible
 * to a compile and was caught by drm_shim_rbtree_test.py. */
/* Defined here, above the rotations, because the rotation and erase paths
 * call through it -- a forward declaration is not enough to dereference. */
struct rb_augment_callbacks {
    void (*propagate)(struct rb_node *node, struct rb_node *stop);
    void (*copy)(struct rb_node *old, struct rb_node *new_);
    void (*rotate)(struct rb_node *old, struct rb_node *new_);
};

static inline void __rb_rotate_left(struct rb_node *x, struct rb_root *root,
                                    const struct rb_augment_callbacks *aug)
{
    struct rb_node *y = x->rb_right;
    struct rb_node *p = rb_parent(x);
    x->rb_right = y->rb_left;
    if (y->rb_left) rb_set_parent(y->rb_left, x);
    y->rb_left = x;
    rb_set_parent(x, y);
    rb_set_parent(y, p);
    __rb_change_child(x, y, p, root);
    if (aug) aug->rotate(x, y);
}

static inline void __rb_rotate_right(struct rb_node *x, struct rb_root *root,
                                     const struct rb_augment_callbacks *aug)
{
    struct rb_node *y = x->rb_left;
    struct rb_node *p = rb_parent(x);
    x->rb_left = y->rb_right;
    if (y->rb_right) rb_set_parent(y->rb_right, x);
    y->rb_right = x;
    rb_set_parent(x, y);
    rb_set_parent(y, p);
    __rb_change_child(x, y, p, root);
    if (aug) aug->rotate(x, y);
}

/* Standard insert fixup. The node arrives red; the loop restores "no red node
 * has a red parent", pushing the violation toward the root. */
static inline void __rb_insert_color(struct rb_node *node, struct rb_root *root,
                                     const struct rb_augment_callbacks *aug)
{
    struct rb_node *parent, *gparent, *uncle;

    rb_set_red(node);
    while ((parent = rb_parent(node)) != NULL && rb_is_red(parent)) {
        gparent = rb_parent(parent);
        if (!gparent) break;
        if (parent == gparent->rb_left) {
            uncle = gparent->rb_right;
            if (uncle && rb_is_red(uncle)) {
                rb_set_black(parent);
                rb_set_black(uncle);
                rb_set_red(gparent);
                node = gparent;
                continue;
            }
            if (node == parent->rb_right) {
                __rb_rotate_left(parent, root, aug);
                node = parent;
                parent = rb_parent(node);
            }
            rb_set_black(parent);
            rb_set_red(gparent);
            __rb_rotate_right(gparent, root, aug);
        } else {
            uncle = gparent->rb_left;
            if (uncle && rb_is_red(uncle)) {
                rb_set_black(parent);
                rb_set_black(uncle);
                rb_set_red(gparent);
                node = gparent;
                continue;
            }
            if (node == parent->rb_left) {
                __rb_rotate_right(parent, root, aug);
                node = parent;
                parent = rb_parent(node);
            }
            rb_set_black(parent);
            rb_set_red(gparent);
            __rb_rotate_left(gparent, root, aug);
        }
    }
    if (root->rb_node) rb_set_black(root->rb_node);
}

/* Erase fixup. `node` may be NULL (the removed child was a leaf), which is
 * why the parent is passed alongside it. */
static inline void __rb_erase_color(struct rb_node *node, struct rb_node *parent,
                                    struct rb_root *root,
                                    const struct rb_augment_callbacks *aug)
{
    struct rb_node *sibling;

    while (rb_is_black_or_nil(node) && node != root->rb_node && parent != NULL) {
        if (parent->rb_left == node) {
            sibling = parent->rb_right;
            if (sibling && rb_is_red(sibling)) {
                rb_set_black(sibling);
                rb_set_red(parent);
                __rb_rotate_left(parent, root, aug);
                sibling = parent->rb_right;
            }
            if (!sibling) break;
            if (rb_is_black_or_nil(sibling->rb_left)
                && rb_is_black_or_nil(sibling->rb_right)) {
                rb_set_red(sibling);
                node = parent;
                parent = rb_parent(node);
                continue;
            }
            if (rb_is_black_or_nil(sibling->rb_right)) {
                if (sibling->rb_left) rb_set_black(sibling->rb_left);
                rb_set_red(sibling);
                __rb_rotate_right(sibling, root, aug);
                sibling = parent->rb_right;
            }
            if (!sibling) break;
            rb_set_parent_color(sibling, rb_parent(sibling), (int)rb_color(parent));
            rb_set_black(parent);
            if (sibling->rb_right) rb_set_black(sibling->rb_right);
            __rb_rotate_left(parent, root, aug);
            node = root->rb_node;
            break;
        } else {
            sibling = parent->rb_left;
            if (sibling && rb_is_red(sibling)) {
                rb_set_black(sibling);
                rb_set_red(parent);
                __rb_rotate_right(parent, root, aug);
                sibling = parent->rb_left;
            }
            if (!sibling) break;
            if (rb_is_black_or_nil(sibling->rb_left)
                && rb_is_black_or_nil(sibling->rb_right)) {
                rb_set_red(sibling);
                node = parent;
                parent = rb_parent(node);
                continue;
            }
            if (rb_is_black_or_nil(sibling->rb_left)) {
                if (sibling->rb_right) rb_set_black(sibling->rb_right);
                rb_set_red(sibling);
                __rb_rotate_left(sibling, root, aug);
                sibling = parent->rb_left;
            }
            if (!sibling) break;
            rb_set_parent_color(sibling, rb_parent(sibling), (int)rb_color(parent));
            rb_set_black(parent);
            if (sibling->rb_left) rb_set_black(sibling->rb_left);
            __rb_rotate_right(parent, root, aug);
            node = root->rb_node;
            break;
        }
    }
    if (node) rb_set_black(node);
    if (root->rb_node) rb_set_black(root->rb_node);
}

/* Unlinks `node` and reports, through *rebalance_from, the lowest node whose
 * subtree changed shape. Augmented callers need that to know where to start
 * recomputing summaries. */
static inline void __rb_erase(struct rb_node *node, struct rb_root *root,
                              struct rb_node **rebalance_from,
                              const struct rb_augment_callbacks *aug)
{
    struct rb_node *child, *parent;
    int color;

    if (!node->rb_left) {
        child = node->rb_right;
        parent = rb_parent(node);
        color = (int)rb_color(node);
        __rb_change_child(node, child, parent, root);
        if (child) rb_set_parent(child, parent);
    } else if (!node->rb_right) {
        child = node->rb_left;
        parent = rb_parent(node);
        color = (int)rb_color(node);
        __rb_change_child(node, child, parent, root);
        if (child) rb_set_parent(child, parent);
    } else {
        /* Two children: swap in the in-order successor, which by
         * construction has no left child. */
        struct rb_node *succ = node->rb_right;
        while (succ->rb_left) succ = succ->rb_left;
        child = succ->rb_right;
        color = (int)rb_color(succ);
        if (rb_parent(succ) == node) {
            parent = succ;
            if (child) rb_set_parent(child, succ);
        } else {
            parent = rb_parent(succ);
            __rb_change_child(succ, child, parent, root);
            if (child) rb_set_parent(child, parent);
            succ->rb_right = node->rb_right;
            rb_set_parent(node->rb_right, succ);
        }
        succ->rb_left = node->rb_left;
        rb_set_parent(node->rb_left, succ);
        rb_set_parent_color(succ, rb_parent(node), (int)rb_color(node));
        __rb_change_child(node, succ, rb_parent(node), root);
        /* succ now covers exactly the subtree node did, so it inherits
         * node's summary. Without this the summary above succ is stale and
         * propagation -- which stops as soon as a value is unchanged --
         * halts at `parent` before ever reaching succ. */
        if (aug) aug->copy(node, succ);
    }

    if (rebalance_from) *rebalance_from = parent;
    if (color == RB_BLACK) __rb_erase_color(child, parent, root, aug);
}

static inline void rb_insert_color(struct rb_node *node, struct rb_root *root)
{
    __rb_insert_color(node, root, NULL);
}

static inline void rb_erase(struct rb_node *node, struct rb_root *root)
{
    __rb_erase(node, root, NULL, NULL);
    RB_CLEAR_NODE(node);
}

static inline struct rb_node *rb_first(const struct rb_root *root)
{
    struct rb_node *n = root->rb_node;
    if (!n) return NULL;
    while (n->rb_left) n = n->rb_left;
    return n;
}

static inline struct rb_node *rb_last(const struct rb_root *root)
{
    struct rb_node *n = root->rb_node;
    if (!n) return NULL;
    while (n->rb_right) n = n->rb_right;
    return n;
}

static inline struct rb_node *rb_next(const struct rb_node *node)
{
    struct rb_node *parent;
    if (RB_EMPTY_NODE(node)) return NULL;
    if (node->rb_right) {
        struct rb_node *n = node->rb_right;
        while (n->rb_left) n = n->rb_left;
        return n;
    }
    while ((parent = rb_parent(node)) != NULL && node == parent->rb_right)
        node = parent;
    return parent;
}

static inline struct rb_node *rb_prev(const struct rb_node *node)
{
    struct rb_node *parent;
    if (RB_EMPTY_NODE(node)) return NULL;
    if (node->rb_left) {
        struct rb_node *n = node->rb_left;
        while (n->rb_right) n = n->rb_right;
        return n;
    }
    while ((parent = rb_parent(node)) != NULL && node == parent->rb_left)
        node = parent;
    return parent;
}

#define rb_first_cached(root) ((root)->rb_leftmost)

/* ---- cached variants ---------------------------------------------------
 * `leftmost` tells insert that the new node is the new minimum. The caller
 * already knows, because it walked left the whole way down. */
static inline void rb_insert_color_cached(struct rb_node *node,
                                          struct rb_root_cached *root,
                                          bool leftmost)
{
    if (leftmost) root->rb_leftmost = node;
    rb_insert_color(node, &root->rb_root);
}

static inline void rb_erase_cached(struct rb_node *node,
                                   struct rb_root_cached *root)
{
    if (root->rb_leftmost == node) root->rb_leftmost = rb_next(node);
    rb_erase(node, &root->rb_root);
}

/* ---- augmented ---------------------------------------------------------
 * Callers attach data to each node summarising its subtree (for drm_mm, the
 * largest `last` below it). After a structural change the summaries on the
 * path to the root are stale and must be recomputed.
 *
 * Linux threads callbacks through the rotations and stops propagating as soon
 * as a value is unchanged. This does the simpler thing: rebalance first, then
 * walk from the lowest affected node to the root recomputing every summary.
 * Same O(log n), a constant factor more work, and far less to get wrong --
 * every node whose subtree changed is an ancestor of the one we start from,
 * because rotations only ever occur along that path. */
static inline void rb_insert_augmented(struct rb_node *node,
                                       struct rb_root *root,
                                       const struct rb_augment_callbacks *augment)
{
    __rb_insert_color(node, root, augment);
    augment->propagate(node, NULL);
}

static inline void rb_insert_augmented_cached(struct rb_node *node,
                                              struct rb_root_cached *root,
                                              bool leftmost,
                                              const struct rb_augment_callbacks *augment)
{
    if (leftmost) root->rb_leftmost = node;
    rb_insert_augmented(node, &root->rb_root, augment);
}

static inline void rb_erase_augmented(struct rb_node *node,
                                      struct rb_root *root,
                                      const struct rb_augment_callbacks *augment)
{
    struct rb_node *from = NULL;
    __rb_erase(node, root, &from, augment);
    if (from) augment->propagate(from, NULL);
    else if (root->rb_node) augment->propagate(root->rb_node, NULL);
    RB_CLEAR_NODE(node);
}

static inline void rb_erase_augmented_cached(struct rb_node *node,
                                             struct rb_root_cached *root,
                                             const struct rb_augment_callbacks *augment)
{
    if (root->rb_leftmost == node) root->rb_leftmost = rb_next(node);
    rb_erase_augmented(node, &root->rb_root, augment);
}

/* Generates the three augmentation callbacks.
 *
 * The contract matches upstream and is easy to get backwards: RBCOMPUTE is a
 * one-argument *accessor* returning the node's own value -- drm_mm passes
 * HOLE_SIZE(NODE), which is just NODE->hole_size. The macro computes the
 * maximum over that value and the two children's stored summaries, and writes
 * the result into the rbaugmented field itself. RBCOMPUTE never stores
 * anything.
 *
 * Upstream's propagate stops as soon as a summary is unchanged. This one does
 * not: it walks to the root unconditionally. The early exit is only sound if
 * the callbacks are threaded through the unlink exactly as Linux does it, and
 * this implementation deliberately keeps the unlink simpler. _compute_max
 * still reports whether it changed, for signature compatibility.
 *
 * Cost is a constant factor on an O(log n) walk. It was tried the other way,
 * and drm_shim_rbtree_test.py caught the stale summaries within 2000
 * operations.
 *
 * The trailing semicolon is part of the macro, matching upstream: drm_mm.c
 * invokes RB_DECLARE_CALLBACKS_MAX(...) with no terminator of its own.
 *
 * Note there are no comments *inside* the definition below. A comment line
 * without its own trailing backslash silently ends the macro, leaving the
 * rest as file-scope garbage -- gcc tolerated it, ShivyCX correctly did not,
 * and it surfaced as "unexpected token at 'rbstatic'".
 */
#define RB_DECLARE_CALLBACKS_MAX(rbstatic, rbname, rbstruct, rbfield,       \
                                 rbtype, rbaugmented, rbcompute)            \
static inline bool rbname ## _compute_max(rbstruct *node)                    \
{                                                                            \
    rbtype max = rbcompute(node), child_max;                                 \
    if (node->rbfield.rb_left) {                                             \
        child_max = rb_entry(node->rbfield.rb_left, rbstruct,                \
                             rbfield)->rbaugmented;                          \
        if (child_max > max) max = child_max;                                \
    }                                                                        \
    if (node->rbfield.rb_right) {                                            \
        child_max = rb_entry(node->rbfield.rb_right, rbstruct,               \
                             rbfield)->rbaugmented;                          \
        if (child_max > max) max = child_max;                                \
    }                                                                        \
    if (node->rbaugmented == max) return false;                              \
    node->rbaugmented = max;                                                 \
    return true;                                                             \
}                                                                            \
static inline void rbname ## _propagate(struct rb_node *rb, struct rb_node *stop) \
{                                                                            \
    while (rb != stop) {                                                     \
        rbstruct *node = rb_entry(rb, rbstruct, rbfield);                    \
        (void)rbname ## _compute_max(node);                                  \
        rb = rb_parent(&node->rbfield);                                      \
    }                                                                        \
}                                                                            \
static inline void rbname ## _copy(struct rb_node *rb_old, struct rb_node *rb_new) \
{                                                                            \
    rbstruct *old = rb_entry(rb_old, rbstruct, rbfield);                     \
    rbstruct *new_ = rb_entry(rb_new, rbstruct, rbfield);                    \
    new_->rbaugmented = old->rbaugmented;                                    \
}                                                                            \
static inline void rbname ## _rotate(struct rb_node *rb_old, struct rb_node *rb_new) \
{                                                                            \
    rbstruct *old = rb_entry(rb_old, rbstruct, rbfield);                     \
    rbstruct *new_ = rb_entry(rb_new, rbstruct, rbfield);                    \
    new_->rbaugmented = old->rbaugmented;                                    \
    (void)rbname ## _compute_max(old);                                       \
}                                                                            \
rbstatic const struct rb_augment_callbacks rbname = {                        \
    rbname ## _propagate, rbname ## _copy, rbname ## _rotate                 \
};
#endif
