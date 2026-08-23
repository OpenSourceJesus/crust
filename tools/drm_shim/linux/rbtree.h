#ifndef _SHIM_LINUX_RBTREE_H
#define _SHIM_LINUX_RBTREE_H
#include <linux/kernel.h>
/* Red-black tree node layout. DRM embeds these by value (drm_mm nodes carry
 * one), so the struct must be complete even where the tree operations
 * themselves are supplied elsewhere. */
struct rb_node {
    unsigned long __rb_parent_color;
    struct rb_node *rb_right;
    struct rb_node *rb_left;
};
struct rb_root { struct rb_node *rb_node; };
struct rb_root_cached { struct rb_root rb_root; struct rb_node *rb_leftmost; };
#define RB_ROOT ((struct rb_root){ NULL })
#define RB_ROOT_CACHED ((struct rb_root_cached){ { NULL }, NULL })
#define rb_entry(ptr, type, member) container_of(ptr, type, member)
#define rb_entry_safe(ptr, type, member) \
    ((ptr) ? rb_entry(ptr, type, member) : NULL)
#endif
