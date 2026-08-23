#ifndef _SHIM_LINUX_LLIST_H
#define _SHIM_LINUX_LLIST_H
#include <linux/kernel.h>
/* Lock-free singly-linked list. drm_device embeds an llist_head by value
 * (connector_free_list), so the types must be complete.
 *
 * "Lock-free" upstream means cmpxchg; single-threaded here means plain
 * pointer writes. Same caveat as spinlock.h -- correct until there is a
 * second context, then silently wrong. */
struct llist_node { struct llist_node *next; };
struct llist_head { struct llist_node *first; };

#define LLIST_HEAD_INIT(name) { NULL }
#define LLIST_HEAD(name) struct llist_head name = LLIST_HEAD_INIT(name)

static inline void init_llist_head(struct llist_head *head) { head->first = NULL; }
static inline bool llist_empty(const struct llist_head *head) { return head->first == NULL; }

static inline bool llist_add(struct llist_node *new, struct llist_head *head)
{
    bool was_empty = (head->first == NULL);
    new->next = head->first;
    head->first = new;
    return was_empty;
}

static inline struct llist_node *llist_del_all(struct llist_head *head)
{
    struct llist_node *first = head->first;
    head->first = NULL;
    return first;
}

#define llist_entry(ptr, type, member) container_of(ptr, type, member)
#define llist_for_each_entry_safe(pos, n, node, member) \
    for (pos = NULL, n = NULL; 0; )
#endif
