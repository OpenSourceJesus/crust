/* rlibc.c -- the freestanding C runtime for programs linked by rlink.
 *
 * rcrt.s provides _start and the raw syscall wrappers. This file provides the
 * C-level libc surface that real programs -- CrustOS in particular -- expect:
 * formatted output, the allocator, environment lookup, and the file/memory
 * syscalls. It is compiled by ShivyCX, assembled by rasm and linked by rlink,
 * so it adds no external dependency: the whole runtime is built by the same
 * self-hosted chain as the program using it.
 *
 * Deliberately small. printf understands the conversions CrustOS actually
 * uses; the allocator is a bump allocator over sbrk with no reuse. Both are
 * honest about what they are rather than pretending to be a general libc.
 */

#include <stdarg.h>

/* from rcrt.s */
long write(int fd, const void *buf, unsigned long n);
long read(int fd, void *buf, unsigned long n);
void exit(int status);
void *sbrk(long increment);
unsigned long strlen(const char *s);
void *memcpy(void *d, const void *s, unsigned long n);
void *memset(void *d, int c, unsigned long n);
long rsyscall1(long n, long a);
long rsyscall3(long n, long a, long b, long c);

/* set by _start */
extern char **environ;

/* ---------------------------------------------------------------- output */

#define OUTBUF 4096

/* A sink is either a file descriptor (fd >= 0) or a caller's buffer. Keeping
 * one shape lets printf and snprintf share all the formatting code. */
struct sink {
    int fd;
    char *buf;
    unsigned long cap;
    unsigned long len;   /* bytes the caller asked for, even if truncated */
    char acc[OUTBUF];
    unsigned long acc_len;
};

static void sink_flush(struct sink *s)
{
    if (s->fd >= 0 && s->acc_len > 0) {
        write(s->fd, s->acc, s->acc_len);
        s->acc_len = 0;
    }
}

static void sink_putc(struct sink *s, int c)
{
    if (s->fd >= 0) {
        if (s->acc_len >= OUTBUF)
            sink_flush(s);
        s->acc[s->acc_len] = (char)c;
        s->acc_len = s->acc_len + 1;
    } else {
        if (s->buf != 0 && s->len + 1 < s->cap)
            s->buf[s->len] = (char)c;
    }
    s->len = s->len + 1;
}

static void sink_puts(struct sink *s, const char *p)
{
    while (*p != 0) {
        sink_putc(s, (int)*p);
        p = p + 1;
    }
}

/* unsigned integer in an arbitrary base, with optional zero padding */
static void sink_num(struct sink *s, unsigned long v, int base, int upper,
                     int width, int zero)
{
    char tmp[32];
    const char *digits;
    int n;
    int i;

    digits = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    n = 0;
    if (v == 0) {
        tmp[0] = '0';
        n = 1;
    }
    while (v > 0) {
        tmp[n] = digits[v % (unsigned long)base];
        v = v / (unsigned long)base;
        n = n + 1;
    }
    i = n;
    while (i < width) {
        sink_putc(s, zero ? '0' : ' ');
        i = i + 1;
    }
    i = n - 1;
    while (i >= 0) {
        sink_putc(s, (int)tmp[i]);
        i = i - 1;
    }
}

static void sink_signed(struct sink *s, long v, int width, int zero)
{
    if (v < 0) {
        sink_putc(s, '-');
        sink_num(s, (unsigned long)(-v), 10, 0, width - 1, zero);
    } else {
        sink_num(s, (unsigned long)v, 10, 0, width, zero);
    }
}

/* The formatting core. Conversions: d i u x X o c s p %, with an optional
 * field width, '0' padding, and the l / ll / z length modifiers. */
static void format(struct sink *s, const char *fmt, va_list ap)
{
    const char *p;
    int width;
    int zero;
    int lng;
    char c;

    p = fmt;
    while (*p != 0) {
        if (*p != '%') {
            sink_putc(s, (int)*p);
            p = p + 1;
            continue;
        }
        p = p + 1;
        if (*p == '%') {
            sink_putc(s, '%');
            p = p + 1;
            continue;
        }
        zero = 0;
        width = 0;
        lng = 0;
        while (*p == '0' || *p == '-' || *p == '+' || *p == ' ') {
            if (*p == '0')
                zero = 1;
            p = p + 1;
        }
        while (*p >= '0' && *p <= '9') {
            width = width * 10 + (int)(*p - '0');
            p = p + 1;
        }
        while (*p == 'l' || *p == 'z' || *p == 'h') {
            if (*p == 'l' || *p == 'z')
                lng = 1;
            p = p + 1;
        }
        c = *p;
        if (c == 0)
            break;
        p = p + 1;
        if (c == 'd' || c == 'i') {
            if (lng)
                sink_signed(s, va_arg(ap, long), width, zero);
            else
                sink_signed(s, (long)va_arg(ap, int), width, zero);
        } else if (c == 'u') {
            if (lng)
                sink_num(s, va_arg(ap, unsigned long), 10, 0, width, zero);
            else
                sink_num(s, (unsigned long)va_arg(ap, unsigned int), 10, 0,
                         width, zero);
        } else if (c == 'x' || c == 'X') {
            if (lng)
                sink_num(s, va_arg(ap, unsigned long), 16, c == 'X', width,
                         zero);
            else
                sink_num(s, (unsigned long)va_arg(ap, unsigned int), 16,
                         c == 'X', width, zero);
        } else if (c == 'o') {
            sink_num(s, va_arg(ap, unsigned long), 8, 0, width, zero);
        } else if (c == 'c') {
            sink_putc(s, va_arg(ap, int));
        } else if (c == 's') {
            char *str;
            str = va_arg(ap, char *);
            sink_puts(s, str == 0 ? "(null)" : str);
        } else if (c == 'p') {
            sink_puts(s, "0x");
            sink_num(s, (unsigned long)va_arg(ap, void *), 16, 0, 0, 0);
        } else {
            sink_putc(s, '%');
            sink_putc(s, (int)c);
        }
    }
}

int printf(const char *fmt, ...)
{
    struct sink s;
    va_list ap;

    s.fd = 1;
    s.buf = 0;
    s.cap = 0;
    s.len = 0;
    s.acc_len = 0;
    va_start(ap, fmt);
    format(&s, fmt, ap);
    va_end(ap);
    sink_flush(&s);
    return (int)s.len;
}

int fprintf(void *stream, const char *fmt, ...)
{
    struct sink s;
    va_list ap;

    /* only the two standard streams exist here; anything else goes to stderr */
    s.fd = (stream == (void *)1) ? 1 : 2;
    s.buf = 0;
    s.cap = 0;
    s.len = 0;
    s.acc_len = 0;
    va_start(ap, fmt);
    format(&s, fmt, ap);
    va_end(ap);
    sink_flush(&s);
    return (int)s.len;
}

int snprintf(char *buf, unsigned long cap, const char *fmt, ...)
{
    struct sink s;
    va_list ap;

    s.fd = -1;
    s.buf = buf;
    s.cap = cap;
    s.len = 0;
    s.acc_len = 0;
    va_start(ap, fmt);
    format(&s, fmt, ap);
    va_end(ap);
    if (buf != 0 && cap > 0) {
        unsigned long end;
        end = s.len < cap - 1 ? s.len : cap - 1;
        buf[end] = 0;
    }
    return (int)s.len;
}

int sprintf(char *buf, const char *fmt, ...)
{
    struct sink s;
    va_list ap;

    s.fd = -1;
    s.buf = buf;
    s.cap = 0x7FFFFFFF;
    s.len = 0;
    s.acc_len = 0;
    va_start(ap, fmt);
    format(&s, fmt, ap);
    va_end(ap);
    if (buf != 0)
        buf[s.len] = 0;
    return (int)s.len;
}

int puts_stdout(const char *s)
{
    write(1, s, strlen(s));
    write(1, "\n", 1);
    return 0;
}

int fflush(void *stream)
{
    /* output is unbuffered across calls, so there is nothing to flush */
    return 0;
}

int fputs(const char *s, void *stream)
{
    int fd;
    fd = (stream == (void *)1) ? 1 : 2;
    write(fd, s, strlen(s));
    return 0;
}

/* ------------------------------------------------------------- allocator */

/* A bump allocator over sbrk. realloc always copies; free is a no-op. Fine for
 * a kernel model that allocates structures and keeps them. */

struct blk {
    unsigned long size;
};

void *malloc(unsigned long n)
{
    struct blk *b;
    unsigned long total;

    total = n + 16;
    total = (total + 15) & ~15UL;
    b = (struct blk *)sbrk((long)total);
    if ((long)b == -1)
        return 0;
    b->size = n;
    return (void *)((char *)b + 16);
}

void free(void *p)
{
    /* bump allocator: memory is returned at exit */
}

void *calloc(unsigned long count, unsigned long size)
{
    void *p;
    unsigned long n;

    n = count * size;
    p = malloc(n);
    if (p != 0)
        memset(p, 0, n);
    return p;
}

void *realloc(void *p, unsigned long n)
{
    struct blk *b;
    void *q;

    if (p == 0)
        return malloc(n);
    b = (struct blk *)((char *)p - 16);
    if (b->size >= n)
        return p;
    q = malloc(n);
    if (q != 0)
        memcpy(q, p, b->size);
    return q;
}

/* ----------------------------------------------------------- environment */

char **environ;

char *getenv(const char *name)
{
    char **e;
    unsigned long n;

    if (environ == 0)
        return 0;
    n = strlen(name);
    e = environ;
    while (*e != 0) {
        char *s;
        unsigned long i;
        int match;

        s = *e;
        match = 1;
        i = 0;
        while (i < n) {
            if (s[i] != name[i]) {
                match = 0;
                i = n;
            } else {
                i = i + 1;
            }
        }
        if (match && s[n] == '=')
            return s + n + 1;
        e = e + 1;
    }
    return 0;
}

/* --------------------------------------------------------------- syscalls */

int open(const char *path, int flags, int mode)
{
    /* SYS_open is absent on some ports; openat(AT_FDCWD) is the portable one */
    return (int)rsyscall3(2, (long)path, (long)flags, (long)mode);
}

int close(int fd)
{
    return (int)rsyscall1(3, (long)fd);
}

long lseek(int fd, long off, int whence)
{
    return rsyscall3(8, (long)fd, off, (long)whence);
}

int mprotect(void *addr, unsigned long len, int prot)
{
    return (int)rsyscall3(10, (long)addr, (long)len, (long)prot);
}

void abort(void)
{
    write(2, "abort\n", 6);
    exit(134);
}

/* ------------------------------------------------------------- utilities */

int strcmp(const char *a, const char *b)
{
    while (*a != 0 && *a == *b) {
        a = a + 1;
        b = b + 1;
    }
    return (int)((unsigned char)*a) - (int)((unsigned char)*b);
}

char *strcpy(char *d, const char *s)
{
    char *r;
    r = d;
    while (*s != 0) {
        *d = *s;
        d = d + 1;
        s = s + 1;
    }
    *d = 0;
    return r;
}


/* ------------------------------------------------- streams, ctype, string */

/* The stdio streams are opaque handles here; fprintf only reads them to pick
 * a file descriptor, so the numeric fd values double as the handles. */
void *stdin = (void *)0;
void *stdout = (void *)1;
void *stderr = (void *)2;

int isdigit(int c) { return c >= '0' && c <= '9'; }
int isalpha(int c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z'); }
int isalnum(int c) { return isalpha(c) || isdigit(c); }
int isspace(int c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'
                            || c == '\v' || c == '\f'; }
int isupper(int c) { return c >= 'A' && c <= 'Z'; }
int islower(int c) { return c >= 'a' && c <= 'z'; }
int isprint(int c) { return c >= 32 && c < 127; }
int tolower(int c) { return isupper(c) ? c + 32 : c; }
int toupper(int c) { return islower(c) ? c - 32 : c; }

int memcmp(const void *a, const void *b, unsigned long n)
{
    const unsigned char *p = (const unsigned char *)a;
    const unsigned char *q = (const unsigned char *)b;
    unsigned long i = 0;
    while (i < n) {
        if (p[i] != q[i])
            return (int)p[i] - (int)q[i];
        i = i + 1;
    }
    return 0;
}

char *strchr(const char *s, int c)
{
    while (*s != 0) {
        if (*s == (char)c)
            return (char *)s;
        s = s + 1;
    }
    return c == 0 ? (char *)s : 0;
}

char *strrchr(const char *s, int c)
{
    const char *last = 0;
    while (*s != 0) {
        if (*s == (char)c)
            last = s;
        s = s + 1;
    }
    return (char *)last;
}

char *strstr(const char *hay, const char *needle)
{
    unsigned long n = strlen(needle);
    if (n == 0)
        return (char *)hay;
    while (*hay != 0) {
        if (memcmp(hay, needle, n) == 0)
            return (char *)hay;
        hay = hay + 1;
    }
    return 0;
}

int strncmp(const char *a, const char *b, unsigned long n)
{
    unsigned long i = 0;
    while (i < n) {
        if (a[i] != b[i] || a[i] == 0)
            return (int)((unsigned char)a[i]) - (int)((unsigned char)b[i]);
        i = i + 1;
    }
    return 0;
}

/* ------------------------------------------------------------ conversion */

long strtol(const char *s, char **end, int base)
{
    long v = 0;
    int neg = 0;
    while (isspace((int)*s))
        s = s + 1;
    if (*s == '-') { neg = 1; s = s + 1; }
    else if (*s == '+') { s = s + 1; }
    if (base == 0) {
        if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) { base = 16; s = s + 2; }
        else if (s[0] == '0') { base = 8; }
        else { base = 10; }
    } else if (base == 16 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s = s + 2;
    }
    while (*s != 0) {
        int d;
        if (isdigit((int)*s)) d = (int)(*s - '0');
        else if (*s >= 'a' && *s <= 'z') d = (int)(*s - 'a') + 10;
        else if (*s >= 'A' && *s <= 'Z') d = (int)(*s - 'A') + 10;
        else break;
        if (d >= base) break;
        v = v * (long)base + (long)d;
        s = s + 1;
    }
    if (end != 0)
        *end = (char *)s;
    return neg ? -v : v;
}

int atoi(const char *s) { return (int)strtol(s, 0, 10); }

double strtod(const char *s, char **end)
{
    double v = 0.0;
    double frac = 0.1;
    int neg = 0;
    while (isspace((int)*s))
        s = s + 1;
    if (*s == '-') { neg = 1; s = s + 1; }
    else if (*s == '+') { s = s + 1; }
    while (isdigit((int)*s)) {
        v = v * 10.0 + (double)(int)(*s - '0');
        s = s + 1;
    }
    if (*s == '.') {
        s = s + 1;
        while (isdigit((int)*s)) {
            v = v + frac * (double)(int)(*s - '0');
            frac = frac * 0.1;
            s = s + 1;
        }
    }
    if (end != 0)
        *end = (char *)s;
    return neg ? -v : v;
}

/* ------------------------------------------------------------------ sort */

/* Insertion sort: O(n^2), but the call sites here sort short scheme and
 * context tables, and a small correct sort beats a large subtly wrong one. */
void qsort(void *base, unsigned long n, unsigned long size,
           int (*cmp)(const void *, const void *))
{
    char *a = (char *)base;
    char tmp[256];
    unsigned long i;
    if (size > 256)
        return;
    i = 1;
    while (i < n) {
        unsigned long j = i;
        memcpy(tmp, a + i * size, size);
        while (j > 0 && cmp(a + (j - 1) * size, tmp) > 0) {
            memcpy(a + j * size, a + (j - 1) * size, size);
            j = j - 1;
        }
        memcpy(a + j * size, tmp, size);
        i = i + 1;
    }
}
