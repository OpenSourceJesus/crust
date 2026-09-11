/* rlibc_win64.c -- the C99 pieces msvcrt.dll does not export.
 *
 * On Windows the C library is msvcrt.dll (see rcrt_win64.s), which predates
 * C99. Almost everything a program needs is there, but not `snprintf` and
 * `vsnprintf`: msvcrt has only `_snprintf`, which returns -1 instead of the
 * needed length on truncation and then does not NUL-terminate. Crust-lowered
 * Rust (`write!`) and a great deal of portable C rely on the C99 behaviour,
 * so these wrap msvcrt's primitives to provide it.
 *
 * Compiled by Crust itself for --os windows and linked into every Windows
 * image, like rlibc.c on the freestanding Linux path.
 *
 * Crust's va_list is a `char *` stepping through 8-byte argument slots, which
 * is exactly msvcrt's va_list, so a list can be handed straight across.
 */

typedef unsigned long long size_t;
typedef char *va_list;

int _vsnprintf(char *buf, size_t n, const char *fmt, va_list ap);
int _vscprintf(const char *fmt, va_list ap);

int vsnprintf(char *buf, size_t n, const char *fmt, va_list ap)
{
    int need = _vscprintf(fmt, ap);
    if (n > 0) {
        int wrote = _vsnprintf(buf, n, fmt, ap);
        /* On truncation msvcrt returns -1 (or exactly n) and leaves the
         * buffer unterminated; C99 always terminates within n. */
        if (wrote < 0 || (size_t)wrote >= n)
            buf[n - 1] = 0;
    }
    return need;
}

int snprintf(char *buf, size_t n, const char *fmt, ...)
{
    va_list ap;
    int r;
    ap = (va_list)__builtin_va_start_addr();
    r = vsnprintf(buf, n, fmt, ap);
    return r;
}
