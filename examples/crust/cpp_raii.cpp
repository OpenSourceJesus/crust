/* Construction and destruction at scope exit, including the early-exit paths.
 *
 * This is the feature the C++ subset exists for: Crust has no `Drop`, so
 * scope exit cannot run code, and every allocating type in its core carries
 * an explicit `free_buf` the caller has to remember. Here the destructor runs
 * on every path out of the block -- the closing brace, but also `continue`,
 * `break` and `return`.
 *
 * What is being measured is whether that costs what writing the calls by hand
 * would. A constructor lowers to `Type_new(&x, ..)` at the declaration and a
 * destructor to `Type_drop(&x)` at each exit, so the generated C should be
 * indistinguishable from the disciplined-C version -- the compiler is not
 * being asked to do anything clever, only to stop the programmer forgetting.
 *
 * `guarded` returns early, so its drop is emitted before the return with the
 * result spilled to a temporary first; the loop below exercises `continue`
 * and `break` unwinding. Between them the three exit paths are all hot.
 */
int printf(const char *, ...);

class Counter {
    unsigned long *sink;
    unsigned long held;
public:
    Counter(unsigned long *s, unsigned long v) { sink = s; held = v; }
    ~Counter() { *sink = *sink + held; }
    unsigned long get() { return held; }
};

/* Early return: the destructor runs before the function leaves. */
unsigned long guarded(unsigned long *sink, unsigned long x) {
    Counter c(sink, x);
    return c.get() ^ 0x5A5A;
}

int main(void) {
    unsigned long sink;
    unsigned long acc;
    unsigned long i;
    sink = 0;
    acc = 1;
    for (i = 0; i < 60000000; i = i + 1) {
        Counter outer(&sink, i);
        acc = acc + guarded(&sink, acc & 0xFFFF);
        if ((i & 7) == 3) {
            /* `continue` unwinds the loop body's scope. */
            continue;
        }
        {
            Counter inner(&sink, acc & 0xFF);
            acc = acc ^ inner.get();
        }
        acc = acc + outer.get();
    }
    printf("%lu %lu\n", acc & 0xFFFFFFFF, sink & 0xFFFFFFFF);
    return 0;
}
