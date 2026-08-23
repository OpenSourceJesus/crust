#ifndef _SHIM_LINUX_SPINLOCK_H
#define _SHIM_LINUX_SPINLOCK_H
struct spinlock { int unused; };
typedef struct spinlock spinlock_t;
/* The type is defined BEFORE kernel.h is included, and that ordering is
 * load-bearing. kernel.h pulls in the headers that embed this type by value
 * (workqueue.h, wait.h). If a source includes this header first, kernel.h
 * re-enters it, finds the guard already set, and gets an empty file -- so the
 * type has to already exist by then. Defining it above the include makes this
 * header safe as an entry point as well as when reached from kernel.h. */
#include <linux/kernel.h>
#include <linux/lockdep.h>
/* Spinlocks, as no-ops.
 *
 * GPU.md already records the honest version of this: no-op locking is correct
 * for a single-threaded kernel with no interrupts, and wrong the moment
 * either changes. Anything built on this inherits that assumption, and it is
 * the assumption most likely to be silently violated later -- a second core
 * or a single interrupt handler turns every one of these into a missing
 * critical section with no diagnostic anywhere.
 *
 * spinlock_t must be a complete type rather than a forward declaration:
 * drm_device and drm_file embed one by value, so five of the eight priority
 * stragglers failed with "unknown type name 'spinlock_t'" until this existed.
 * The int is there to give it a size; nothing reads it.
 *
 * The irqsave variants take `flags` by name and must still assign to it, or
 * callers get an unused-variable warning where upstream has none. */
/* Both spellings are used: DRM writes `raw_spinlock_t` in some places and
 * `struct raw_spinlock` in others (drm_mode_config::panic_lock), so the
 * struct must be named rather than anonymous. */
struct raw_spinlock { int unused; };
typedef struct raw_spinlock raw_spinlock_t;
typedef struct { int unused; } rwlock_t;

#define spin_lock_init(l)          do { (void)(l); } while (0)
#define spin_lock(l)               do { (void)(l); } while (0)
#define spin_unlock(l)             do { (void)(l); } while (0)
#define spin_lock_bh(l)            do { (void)(l); } while (0)
#define spin_unlock_bh(l)          do { (void)(l); } while (0)
#define spin_lock_irq(l)           do { (void)(l); } while (0)
#define spin_unlock_irq(l)         do { (void)(l); } while (0)
#define spin_lock_irqsave(l, f)    do { (void)(l); (f) = 0; } while (0)
#define spin_unlock_irqrestore(l, f) do { (void)(l); (void)(f); } while (0)
#define spin_trylock(l)            (1)
#define spin_is_locked(l)          (0)

#define raw_spin_lock_init(l)      do { (void)(l); } while (0)
#define raw_spin_lock(l)           do { (void)(l); } while (0)
#define raw_spin_unlock(l)         do { (void)(l); } while (0)

#define rwlock_init(l)             do { (void)(l); } while (0)
#define read_lock(l)               do { (void)(l); } while (0)
#define read_unlock(l)             do { (void)(l); } while (0)
#define write_lock(l)              do { (void)(l); } while (0)
#define write_unlock(l)            do { (void)(l); } while (0)

/* No interrupts here, so they are never disabled. printk.h's WARN path asks
 * before deciding whether it may sleep. */
#define irqs_disabled()            (0)
#define local_irq_save(f)          do { (f) = 0; } while (0)
#define local_irq_restore(f)       do { (void)(f); } while (0)
#define local_irq_disable()        do { } while (0)
#define local_irq_enable()         do { } while (0)

#define DEFINE_SPINLOCK(name)      spinlock_t name
#define __SPIN_LOCK_UNLOCKED(n)    { 0 }

/* Plain mutexes, same reasoning. struct mutex is also declared in ww_mutex.h,
 * which DRM includes on the atomic-modeset path; both spellings have to agree
 * because some translation units see one and some see both. */
#define mutex_init(m)              do { (void)(m); } while (0)
#define mutex_lock(m)              do { (void)(m); } while (0)
#define mutex_unlock(m)            do { (void)(m); } while (0)
#define mutex_trylock(m)           (1)
#define mutex_is_locked(m)         (0)
#define mutex_destroy(m)           do { (void)(m); } while (0)
#endif
