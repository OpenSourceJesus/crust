#ifndef _SHIM_LINUX_SPINLOCK_TYPES_H
#define _SHIM_LINUX_SPINLOCK_TYPES_H
/* Upstream splits the lock *types* from the lock *operations* so headers that
 * only embed a lock need not pull in the whole locking API. Several DRM
 * headers include only this one, which is why spinlock_t stayed unknown in
 * three files after spinlock.h existed.
 *
 * Here the split is not worth maintaining separately -- the operations are
 * no-op macros with no dependencies -- so this simply forwards. */
#include <linux/spinlock.h>
#endif
