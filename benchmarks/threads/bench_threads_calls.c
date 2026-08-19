/* bench_threads_calls.c - thread partition under real register pressure.
 *
 * bench_threads.c deliberately has call-free bodies, "so the register
 * partition fully controls each thread's footprint". On x86-64 that works,
 * because ShivyCX allocates value homes from the caller-saved pool there and a
 * leaf function uses them freely.
 *
 * On AArch64 it has the *opposite* effect, and the reason is worth stating
 * because it decides what a meaningful benchmark looks like on this target:
 *
 *   - ShivyCX's AArch64 allocator gives a value a callee-saved home (x19-x28)
 *     only when it must survive a call. Everything else goes to caller-saved
 *     scratch (x0-x18).
 *   - A cooperative switch is a *call*, so caller-saved registers are already
 *     dead across it by the ABI -- the compiler has spilled anything live.
 *     Only the callee-saved set needs saving, which is what the switcher does.
 *   - Therefore a call-free leaf function has a nearly empty callee-saved
 *     footprint, and the partition has almost nothing to partition. Measured:
 *     five live values per side still produced a one-register footprint, and
 *     constraining the budget from 3 registers to 1 cost nothing at all --
 *     same instruction count, same single spill.
 *
 * So this file gives each thread a call. Now the live values must sit in
 * callee-saved registers across `sink()`, the footprint grows to something the
 * split has to work at, and the numbers mean something.
 *
 * Expected on AArch64: ten callee-saved homes, two threads wanting eight each,
 * so they *cannot* be made disjoint. The partition degrades gracefully -- the
 * switcher saves the outgoing footprint and restores the incoming one, which
 * is correct however they overlap, just larger. Compare with
 * bench_threads.c, where the footprints do become disjoint.
 */

int l0, l1, l2, l3, l4;
int r0, r1, r2, r3, r4;

int sink(int v);

void foo(void)
{
    int a = l0 + 1;
    int b = l1 + 2;
    int c = l2 + 3;
    int d = l3 + 4;
    int e = l4 + 5;
    /* Each call forces every other live value into a callee-saved home. */
    a = sink(a);
    b = sink(b);
    c = sink(c);
    d = sink(d);
    e = sink(e);
    l0 = a + b;
    l1 = b + c;
    l2 = c + d;
    l3 = d + e;
    l4 = e + a;
}

void bar(void)
{
    int v = r0 * 2;
    int w = r1 * 3;
    int x = r2 * 4;
    int y = r3 * 5;
    int z = r4 * 6;
    v = sink(v);
    w = sink(w);
    x = sink(x);
    y = sink(y);
    z = sink(z);
    r0 = v + w;
    r1 = w + x;
    r2 = x + y;
    r3 = y + z;
    r4 = z + v;
}

/* Reachable from both threads, so it lands in the shared set and gets the
 * intersection of the two budgets -- safe to call from either side. */
int sink(int v)
{
    return v + 1;
}

int main()
assert foo in threads.left( core=0 )
assert bar in threads.right( core=0 )
{
    foo();
    bar();
    return 0;
}
