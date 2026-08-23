#ifndef _SHIM_LINUX_LIST_H
#define _SHIM_LINUX_LIST_H
/* The kernel's intrusive circular doubly-linked list. DRM threads everything
 * onto these -- modes on a connector, nodes in an allocator -- so this is the
 * one shim header that has to be a real implementation rather than a stub. */
struct list_head { struct list_head *next, *prev; };
/* The type is defined BEFORE kernel.h is included, and that ordering is
 * load-bearing. kernel.h pulls in the headers that embed this type by value
 * (workqueue.h, wait.h). If a source includes this header first, kernel.h
 * re-enters it, finds the guard already set, and gets an empty file -- so the
 * type has to already exist by then. Defining it above the include makes this
 * header safe as an entry point as well as when reached from kernel.h. */
#include <linux/kernel.h>

#define LIST_HEAD_INIT(name) { &(name), &(name) }
#define LIST_HEAD(name) struct list_head name = LIST_HEAD_INIT(name)

static inline void INIT_LIST_HEAD(struct list_head *l)
{
    l->next = l;
    l->prev = l;
}

static inline void __list_add(struct list_head *n, struct list_head *prev,
                              struct list_head *next)
{
    next->prev = n;
    n->next = next;
    n->prev = prev;
    prev->next = n;
}

static inline void list_add(struct list_head *n, struct list_head *head)
{
    __list_add(n, head, head->next);
}

static inline void list_add_tail(struct list_head *n, struct list_head *head)
{
    __list_add(n, head->prev, head);
}

static inline void list_del(struct list_head *e)
{
    e->prev->next = e->next;
    e->next->prev = e->prev;
    e->next = e;
    e->prev = e;
}

#define list_del_init(e) list_del(e)

static inline int list_empty(const struct list_head *head)
{
    return head->next == head;
}

static inline void list_move_tail(struct list_head *e, struct list_head *head)
{
    list_del(e);
    list_add_tail(e, head);
}

#define list_entry(ptr, type, member) container_of(ptr, type, member)
#define list_first_entry(ptr, type, member) list_entry((ptr)->next, type, member)
#define list_last_entry(ptr, type, member) list_entry((ptr)->prev, type, member)
#define list_next_entry(pos, member) \
    list_entry((pos)->member.next, typeof(*(pos)), member)
#define list_prev_entry(pos, member) \
    list_entry((pos)->member.prev, typeof(*(pos)), member)
#define list_first_entry_or_null(ptr, type, member) \
    (list_empty(ptr) ? NULL : list_first_entry(ptr, type, member))

#define list_for_each(pos, head) \
    for (pos = (head)->next; pos != (head); pos = pos->next)
#define list_for_each_safe(pos, n, head) \
    for (pos = (head)->next, n = pos->next; pos != (head); \
         pos = n, n = pos->next)
#define list_for_each_entry(pos, head, member) \
    for (pos = list_first_entry(head, typeof(*pos), member); \
         &pos->member != (head); pos = list_next_entry(pos, member))
#define list_for_each_entry_safe(pos, n, head, member) \
    for (pos = list_first_entry(head, typeof(*pos), member), \
         n = list_next_entry(pos, member); \
         &pos->member != (head); pos = n, n = list_next_entry(n, member))
#define list_for_each_entry_reverse(pos, head, member) \
    for (pos = list_last_entry(head, typeof(*pos), member); \
         &pos->member != (head); pos = list_prev_entry(pos, member))
/* The reverse-safe form. drm_buddy.c frees blocks while walking backwards,
 * which is exactly the case the plain reverse iterator cannot survive: it
 * reads pos->member.prev after pos has been freed. */
#define list_for_each_entry_safe_reverse(pos, n, head, member) \
    for (pos = list_last_entry(head, typeof(*pos), member), \
         n = list_prev_entry(pos, member); \
         &pos->member != (head); pos = n, n = list_prev_entry(n, member))
#define list_for_each_entry_continue(pos, head, member) \
    for (pos = list_next_entry(pos, member); \
         &pos->member != (head); pos = list_next_entry(pos, member))
#define list_for_each_entry_from(pos, head, member) \
    for (; &pos->member != (head); pos = list_next_entry(pos, member))

static inline bool list_is_singular(const struct list_head *head)
{
    return head->next != head && head->next == head->prev;
}

static inline bool list_is_first(const struct list_head *e, const struct list_head *h)
{
    return e->prev == h;
}

static inline bool list_is_last(const struct list_head *e, const struct list_head *h)
{
    return e->next == h;
}

/* Splice one list onto another. drm_buddy.c uses the tail form to return a
 * batch of freed blocks in one operation. Both leave the source head stale,
 * exactly as upstream -- callers are expected to reinitialise it. */
static inline void list_splice(struct list_head *list, struct list_head *head)
{
    if (list->next == list) return;
    list->next->prev = head;
    list->prev->next = head->next;
    head->next->prev = list->prev;
    head->next = list->next;
}

static inline void list_splice_tail(struct list_head *list, struct list_head *head)
{
    if (list->next == list) return;
    list->prev->next = head;
    list->next->prev = head->prev;
    head->prev->next = list->next;
    head->prev = list->prev;
}

static inline void list_splice_init(struct list_head *list, struct list_head *head)
{
    list_splice(list, head);
    INIT_LIST_HEAD(list);
}

static inline void list_splice_tail_init(struct list_head *list, struct list_head *head)
{
    list_splice_tail(list, head);
    INIT_LIST_HEAD(list);
}

/* singly-linked hlist, used by a few helpers */
struct hlist_node { struct hlist_node *next, **pprev; };
struct hlist_head { struct hlist_node *first; };
#endif
