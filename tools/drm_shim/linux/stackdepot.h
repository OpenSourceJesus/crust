#ifndef _SHIM_LINUX_STACKDEPOT_H
#define _SHIM_LINUX_STACKDEPOT_H
#include <linux/kernel.h>
/* depot_stack_handle_t is a cookie identifying a saved stack trace. It is
 * KASAN/KMEMLEAK debug plumbing: dma-resv.h embeds one by value, so anything
 * reaching ww_mutex.h through that chain needs the type to be *declared*
 * even though a freestanding build never records or looks up a trace.
 *
 * That is why an autostub placeholder does not help here -- the header exists
 * but declares nothing, so the include succeeds and the field declaration
 * fails. It is the single most common first error in the generic layer.
 *
 * The functions are declared but deliberately not defined. Nothing in the
 * portable slice calls them; if something does, it fails at link with a name
 * that says what it wanted, rather than silently recording nothing. */
typedef u32 depot_stack_handle_t;

depot_stack_handle_t stack_depot_save(unsigned long *entries,
                                      unsigned int nr_entries, unsigned gfp);
unsigned int stack_depot_fetch(depot_stack_handle_t handle,
                               unsigned long **entries);
#endif
