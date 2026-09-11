#ifndef _STDARG_H
#define _STDARG_H

/* ShivyC variadic support.
 *
 * Variadic functions in ShivyC receive all of their arguments on the stack
 * (see the calling-convention handling in the code generator), so a va_list
 * is just a moving pointer over those 8-byte argument slots. va_start asks the
 * compiler for the address of the first variadic argument; va_arg reads the
 * current slot and advances by one 8-byte slot. */

#ifdef __APPLE__
/* The macOS SDK declares va_list itself (sys/_types/_va_list.h, guarded by
 * _VA_LIST_T) as __darwin_va_list, which is `void *` for a compiler that is
 * not GCC or clang. The SDK ships no <stdarg.h> -- that is the compiler's --
 * so its headers include this one, and the two typedefs must agree. Either
 * spelling is an 8-byte pointer over the argument slots, which is exactly
 * Apple's arm64 va_list, so this is only a matter of matching the name. */
#ifndef _VA_LIST_T
#define _VA_LIST_T
typedef void *va_list;
#endif
/* Step through `char *`: arithmetic on the SDK's `void *` is not C. */
#define va_start(ap, last) ((ap) = (va_list)__builtin_va_start_addr())
#define va_arg(ap, type) \
    (*(type *)(((ap) = (char *)(ap) + 8), (char *)(ap) - 8))
#define va_end(ap)         ((void)((ap) = (va_list)0))
#define va_copy(dst, src)  ((dst) = (src))
#else
typedef char *va_list;

#define va_start(ap, last) ((ap) = (va_list)__builtin_va_start_addr())
#define va_arg(ap, type)   (*(type *)(((ap) = (ap) + 8), (ap) - 8))
#define va_end(ap)         ((void)((ap) = (va_list)0))
#define va_copy(dst, src)  ((dst) = (src))
#endif

#endif
