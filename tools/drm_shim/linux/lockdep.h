#ifndef _SHIM_LINUX_LOCKDEP_H
#define _SHIM_LINUX_LOCKDEP_H
/* Lock-ordering assertions, compiled out.
 *
 * Upstream these expand to nothing unless CONFIG_LOCKDEP is set, so no-ops are
 * the faithful configuration rather than a shortcut -- but they must still be
 * *declared*, because with them undeclared gcc treats each call as an implicit
 * function declaration. Under the old -w oracle that passed silently; it is
 * now an error, which is how these surfaced as the single most common blocker
 * in the generic layer (six of the eight priority stragglers).
 *
 * The (void)(l) keeps the lock argument evaluated-looking so an unused-variable
 * warning does not appear where upstream has none.
 *
 * Upstream this header is reached via mutex.h and spinlock.h. Both of those
 * are autostub placeholders here, so kernel.h includes it directly instead --
 * a deviation in *routing*, not in what is defined. */
#define lockdep_assert_held(l)              do { (void)(l); } while (0)
#define lockdep_assert_held_once(l)         do { (void)(l); } while (0)
#define lockdep_assert_held_read(l)         do { (void)(l); } while (0)
#define lockdep_assert_held_write(l)        do { (void)(l); } while (0)
#define lockdep_assert_not_held(l)          do { (void)(l); } while (0)
#define lockdep_assert_none_held_once()     do { } while (0)
#define lockdep_is_held(l)                  (1)
#define lockdep_init_map(a, b, c, d)        do { } while (0)
#define lockdep_set_class(a, b)             do { } while (0)
#define lockdep_set_subclass(a, b)          do { } while (0)
#define might_lock(l)                       do { (void)(l); } while (0)
#define might_sleep()                       do { } while (0)
#define might_sleep_if(c)                   do { } while (0)

struct lock_class_key { int unused; };
#endif
