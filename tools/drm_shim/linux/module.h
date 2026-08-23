#ifndef _SHIM_LINUX_MODULE_H
#define _SHIM_LINUX_MODULE_H
#include <linux/kernel.h>
/* Module metadata and init/exit registration. Nothing here is a loadable
 * module -- the code is linked in directly -- so the registration macros
 * discard the function reference entirely.
 *
 * module_init/module_exit must expand to a *declaration*, not nothing: they
 * appear at file scope where a bare semicolon is invalid, and expanding to
 * nothing leaves gcc reading the following token as a declaration with an
 * implicit int. That is how drm_buddy.c reported
 * "type defaults to 'int' in declaration of 'module_init'". */
#define module_init(fn) static void *__module_init_##fn = (void *)(fn)
#define module_exit(fn) static void *__module_exit_##fn = (void *)(fn)
#define MODULE_AUTHOR(s)
#define MODULE_DESCRIPTION(s)
#define MODULE_LICENSE(s)
#define MODULE_FIRMWARE(s)
#define MODULE_PARM_DESC(p, s)
#define module_param(n, t, p)
#define module_param_named(n, v, t, p)
#define EXPORT_SYMBOL_NS_GPL(s, n)
#define THIS_MODULE ((struct module *)0)
struct module;
#endif
