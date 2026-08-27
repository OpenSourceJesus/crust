/* Minimal WASI preview-1 runtime for the wasm back end.
 *
 *     #include <wasi.h>
 *
 * gives a program running under a WASI host enough of a C library to say
 * something: write(), putchar(), puts(), and a printf covering the
 * conversions that actually come up (%d %i %u %x %c %s %ld %lu %%, with an
 * optional field width and zero-padding).
 *
 * This is a header-only runtime rather than a separate object because the
 * wasm target compiles one translation unit at a time -- there is no linker
 * in the path, so everything a module needs has to arrive through the
 * preprocessor. Everything here is `static` for that reason: including it in
 * a program that already defines one of these names must not collide.
 *
 * The only host interface used is fd_write, which takes a vector of
 * (pointer, length) pairs in the module's own linear memory. That memory is
 * exported automatically by the back end, so the host can reach the buffers
 * these functions hand it.
 */
#ifndef _WASI_H
#define _WASI_H

/* The raw preview-1 imports. Spelled __wasi_* so a program is free to define
 * its own function called fd_write; the back end strips the prefix when it
 * works out which WASI module to import from. */
int __wasi_fd_write(int fd, const void *iovs, int iovs_len, int *nwritten);
void __wasi_proc_exit(int code);

/* An iovec as WASI defines it: a pointer and a length, both 32-bit, in the
 * module's memory. The pointer is stored as an int rather than a real pointer
 * because wasm32 addresses are 32 bits while this compiler's pointers are 8
 * bytes -- a struct of two ints has exactly the layout the host expects. */
struct __wasi_iovec { int buf; int buf_len; };

#define WASI_STDOUT 1
#define WASI_STDERR 2

static int __wasi_write_fd(int fd, const char *s, int n)
{
    struct __wasi_iovec iov;
    int written;
    int rc;
    /* Casting the pointer to an int truncates it to the 32-bit address the
     * host wants, which is exactly the representation wasm32 uses. */
    iov.buf = (int)(long)s;
    iov.buf_len = n;
    written = 0;
    rc = __wasi_fd_write(fd, &iov, 1, &written);
    if (rc != 0) return -1;
    return written;
}

static int write(int fd, const char *buf, int n)
{
    return __wasi_write_fd(fd, buf, n);
}

static int putchar(int c)
{
    char ch;
    ch = (char)c;
    if (__wasi_write_fd(WASI_STDOUT, &ch, 1) < 0) return -1;
    return c;
}

static int __wasi_puts_raw(const char *s)
{
    int n;
    n = 0;
    while (s[n]) n++;
    return __wasi_write_fd(WASI_STDOUT, s, n);
}

static int puts(const char *s)
{
    if (__wasi_puts_raw(s) < 0) return -1;
    if (putchar('\n') < 0) return -1;
    return 0;
}

/* Render `val` in `base` into `buf`, returning its length. Digits come out
 * least-significant first and are reversed in place, which avoids needing a
 * second buffer or a recursive helper. */
static int __wasi_utoa(unsigned long val, int base, int upper, char *buf)
{
    const char *lower_digits = "0123456789abcdef";
    const char *upper_digits = "0123456789ABCDEF";
    const char *digits;
    int n;
    int i;
    int j;
    char t;

    digits = upper ? upper_digits : lower_digits;
    n = 0;
    if (val == 0) {
        buf[0] = '0';
        return 1;
    }
    while (val > 0) {
        buf[n] = digits[(int)(val % (unsigned long)base)];
        val = val / (unsigned long)base;
        n++;
    }
    i = 0;
    j = n - 1;
    while (i < j) {
        t = buf[i]; buf[i] = buf[j]; buf[j] = t;
        i++;
        j--;
    }
    return n;
}

/* Padding before the value (right-justified, the default). */
static void __wasi_pad(int width, int len, int zero)
{
    while (width > len) {
        putchar(zero ? '0' : ' ');
        width--;
    }
}

/* Padding after the value, for the `-` flag. Always spaces: zero-padding on
 * the right would change the number's value, so C ignores `0` when `-` is
 * given. */
static void __wasi_padr(int width, int len)
{
    while (width > len) {
        putchar(' ');
        width--;
    }
}

/* printf, taking a real `...` argument list.
 *
 * Conversions: %d %i %u %x %X %c %s %ld %lu %lx %%, each with an optional
 * field width and the `0` and `-` flags. Arguments are pulled with va_arg as the
 * format string calls for them, so there is no limit on how many there are. */
static int printf(const char *fmt, ...)
{
    __builtin_va_list ap;
    char numbuf[32];
    int argi;
    int i;
    int out;
    int width;
    int zero;
    int left;
    int lng;
    int len;
    int k;
    long arg;
    char c;
    const char *sp;

    __builtin_va_start(ap, fmt);
    i = 0;
    out = 0;
    argi = 0;
    while (fmt[i]) {
        c = fmt[i];
        if (c != '%') {
            putchar((int)c);
            out++;
            i++;
            continue;
        }
        i++;
        if (fmt[i] == '%') { putchar('%'); out++; i++; continue; }

        /* Flags. `-` (left-justify) overrides `0`, as C requires. */
        zero = 0;
        left = 0;
        while (fmt[i] == '0' || fmt[i] == '-') {
            if (fmt[i] == '0') zero = 1; else left = 1;
            i++;
        }
        if (left) zero = 0;
        width = 0;
        while (fmt[i] >= '0' && fmt[i] <= '9') {
            width = width * 10 + (fmt[i] - '0');
            i++;
        }
        lng = 0;
        while (fmt[i] == 'l') { lng = 1; i++; }

        /* Pull the argument for this conversion. A `long` slot is read for
         * every case: the variadic slots are 8 bytes wide regardless, and the
         * per-conversion code below narrows as the specifier requires. */
        arg = __builtin_va_arg(ap, long);
        argi++;

        c = fmt[i];
        i++;
        if (c == 'd' || c == 'i') {
            long v;
            v = lng ? arg : (long)(int)arg;
            if (v < 0) {
                len = __wasi_utoa((unsigned long)(-v), 10, 0, numbuf);
                /* The sign's placement depends on the padding character. With
                 * zero padding it belongs before the zeros ("-00042"); with
                 * spaces it belongs after them ("   -42"), so that the number
                 * stays right-aligned. Emitting it in one fixed position gets
                 * one of the two cases wrong. */
                if (zero) {
                    putchar('-');
                    out++;
                    __wasi_pad(width - 1, len, 1);
                } else if (!left) {
                    __wasi_pad(width, len + 1, 0);
                    putchar('-');
                    out++;
                } else {
                    putchar('-');
                    out++;
                }
                k = 0;
                while (k < len) { putchar((int)numbuf[k]); k++; }
                out = out + len;
                if (left) __wasi_padr(width, len + 1);
                
            } else {
                len = __wasi_utoa((unsigned long)v, 10, 0, numbuf);
                if (!left) __wasi_pad(width, len, zero);
                k = 0;
                while (k < len) { putchar((int)numbuf[k]); k++; }
                out = out + len;
                if (left) __wasi_padr(width, len);
            }
        } else if (c == 'u' || c == 'x' || c == 'X') {
            unsigned long v;
            int base;
            v = lng ? (unsigned long)arg : (unsigned long)(unsigned int)arg;
            base = (c == 'u') ? 10 : 16;
            len = __wasi_utoa(v, base, c == 'X', numbuf);
            if (!left) __wasi_pad(width, len, zero);
            k = 0;
            while (k < len) { putchar((int)numbuf[k]); k++; }
            out = out + len;
            if (left) __wasi_padr(width, len);
        } else if (c == 'c') {
            if (!left) __wasi_pad(width, 1, 0);
            putchar((int)arg);
            out++;
            if (left) __wasi_padr(width, 1);
        } else if (c == 's') {
            sp = (const char *)arg;
            if (!sp) sp = "(null)";
            len = 0;
            while (sp[len]) len++;
            if (!left) __wasi_pad(width, len, 0);
            k = 0;
            while (k < len) { putchar((int)sp[k]); k++; }
            out = out + len;
            if (left) __wasi_padr(width, len);
        } else {
            /* An unrecognised conversion is echoed rather than swallowed, so
             * a typo in a format string is visible in the output. */
            putchar('%');
            putchar((int)c);
            out = out + 2;
        }
    }
    __builtin_va_end(ap);
    return out;
}

/* The printfN macros this header used to require, kept as thin aliases so
 * code written against the pre-variadic version still compiles. New code
 * should just call printf. */
#define printf0(f)          printf(f)
#define printf1(f, a)       printf((f), (a))
#define printf2(f, a, b)    printf((f), (a), (b))
#define printf3(f, a, b, c) printf((f), (a), (b), (c))

static void exit(int code) { __wasi_proc_exit(code); }

#endif /* _WASI_H */
