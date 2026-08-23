#ifndef _SHIM_LINUX_KGDB_H
#define _SHIM_LINUX_KGDB_H
/* Kernel debugger. There is none, so no code is ever running in a debugger
 * master context. printk.h's WARN family calls this to decide whether it is
 * safe to take locks while reporting. */
#define in_dbg_master() (0)
#endif
