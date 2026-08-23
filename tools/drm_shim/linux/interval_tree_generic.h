#ifndef _SHIM_LINUX_INTERVAL_TREE_GENERIC_H
#define _SHIM_LINUX_INTERVAL_TREE_GENERIC_H
#include <linux/rbtree.h>
/* INTERVAL_TREE_DEFINE generates an interval tree over an existing struct.
 *
 * The tree is ordered by interval start. Each node additionally carries the
 * maximum `last` value anywhere in its subtree; that summary is what makes
 * overlap queries O(log n + k) instead of O(n), because a subtree whose
 * maximum `last` is below the query start cannot contain any match and can be
 * skipped whole.
 *
 * drm_mm instantiates this over drm_mm_node to answer "which allocations
 * overlap this address range". It was the last structural blocker in the
 * priority set: the macro was an empty autostub, so gcc parsed the
 * invocation as a declaration and reported `unknown type name 'rb'`.
 *
 * ITSTART/ITLAST are accessors, not fields, because callers compute the
 * interval from other members -- drm_mm's LAST is start + size - 1.
 *
 * Correctness here is not established by the file compiling. See
 * tools/drm_shim_rbtree_test.py, which checks every query against brute-force
 * search. */
#define INTERVAL_TREE_DEFINE(ITSTRUCT, ITRB, ITTYPE, ITSUBTREE,             \
                             ITSTART, ITLAST, ITSTATIC, ITPREFIX)           \
                                                                             \
/* The subtree summary is the maximum `last` below the node. ITLAST is the
 * accessor RB_DECLARE_CALLBACKS_MAX needs -- it returns this node's own end,
 * and the callback macro folds in the children's stored summaries. */        \
RB_DECLARE_CALLBACKS_MAX(static, ITPREFIX ## _augment, ITSTRUCT, ITRB,       \
                         ITTYPE, ITSUBTREE, ITLAST)                          \
                                                                             \
ITSTATIC void ITPREFIX ## _insert(ITSTRUCT *node, struct rb_root_cached *root) \
{                                                                            \
    struct rb_node **link = &root->rb_root.rb_node, *rb_parent_ = NULL;      \
    ITTYPE start = ITSTART(node), last = ITLAST(node);                       \
    ITSTRUCT *parent;                                                        \
    bool leftmost = true;                                                    \
                                                                             \
    node->ITSUBTREE = last;                                                  \
    while (*link) {                                                          \
        rb_parent_ = *link;                                                  \
        parent = rb_entry(rb_parent_, ITSTRUCT, ITRB);                       \
        /* Widen ancestors on the way down; the propagate pass after         \
         * rebalancing fixes anything rotations disturb. */                  \
        if (parent->ITSUBTREE < last) parent->ITSUBTREE = last;              \
        if (start < ITSTART(parent)) {                                       \
            link = &parent->ITRB.rb_left;                                    \
        } else {                                                             \
            link = &parent->ITRB.rb_right;                                   \
            leftmost = false;                                                \
        }                                                                    \
    }                                                                        \
                                                                             \
    rb_link_node(&node->ITRB, rb_parent_, link);                             \
    rb_insert_augmented_cached(&node->ITRB, root, leftmost,                  \
                               &ITPREFIX ## _augment);                       \
}                                                                            \
                                                                             \
ITSTATIC void ITPREFIX ## _remove(ITSTRUCT *node, struct rb_root_cached *root) \
{                                                                            \
    rb_erase_augmented_cached(&node->ITRB, root, &ITPREFIX ## _augment);     \
}                                                                            \
                                                                             \
/* Leftmost node in this subtree whose interval ends at or after `start`.    \
 * NULL when the whole subtree is too far left. */                          \
static ITSTRUCT *ITPREFIX ## _subtree_search(ITSTRUCT *node, ITTYPE start,   \
                                             ITTYPE last)                    \
{                                                                            \
    while (true) {                                                           \
        if (node->ITRB.rb_left) {                                            \
            ITSTRUCT *left = rb_entry(node->ITRB.rb_left, ITSTRUCT, ITRB);   \
            if (start <= left->ITSUBTREE) {                                  \
                node = left;                                                 \
                continue;                                                    \
            }                                                                \
        }                                                                    \
        if (ITSTART(node) <= last) {                                         \
            if (start <= ITLAST(node)) return node;                          \
            if (node->ITRB.rb_right) {                                       \
                node = rb_entry(node->ITRB.rb_right, ITSTRUCT, ITRB);        \
                if (start <= node->ITSUBTREE) continue;                      \
            }                                                                \
        }                                                                    \
        return NULL;                                                         \
    }                                                                        \
}                                                                            \
                                                                             \
ITSTATIC ITSTRUCT *ITPREFIX ## _iter_first(struct rb_root_cached *root,      \
                                           ITTYPE start, ITTYPE last)        \
{                                                                            \
    ITSTRUCT *node, *leftmost;                                               \
                                                                             \
    if (!root->rb_root.rb_node) return NULL;                                 \
    node = rb_entry(root->rb_root.rb_node, ITSTRUCT, ITRB);                  \
    if (node->ITSUBTREE < start) return NULL;                                \
                                                                             \
    leftmost = rb_entry(root->rb_leftmost, ITSTRUCT, ITRB);                  \
    if (ITSTART(leftmost) > last) return NULL;                               \
                                                                             \
    return ITPREFIX ## _subtree_search(node, start, last);                   \
}                                                                            \
                                                                             \
ITSTATIC ITSTRUCT *ITPREFIX ## _iter_next(ITSTRUCT *node, ITTYPE start,      \
                                          ITTYPE last)                       \
{                                                                            \
    struct rb_node *rb = node->ITRB.rb_right, *prev;                         \
                                                                             \
    while (true) {                                                           \
        /* A right subtree that reaches far enough may hold the next match. */ \
        if (rb) {                                                            \
            ITSTRUCT *right = rb_entry(rb, ITSTRUCT, ITRB);                  \
            if (start <= right->ITSUBTREE)                                   \
                return ITPREFIX ## _subtree_search(right, start, last);      \
        }                                                                    \
        /* Otherwise climb until we step up from a left child; that parent   \
         * is the next interval in start order. */                           \
        do {                                                                 \
            rb = rb_parent(&node->ITRB);                                     \
            if (!rb) return NULL;                                            \
            prev = &node->ITRB;                                              \
            node = rb_entry(rb, ITSTRUCT, ITRB);                             \
            rb = node->ITRB.rb_right;                                        \
        } while (prev == rb);                                                \
                                                                             \
        if (ITSTART(node) > last) return NULL;                               \
        if (start <= ITLAST(node)) return node;                              \
    }                                                                        \
}
#endif
