/* Per-workgroup histogram of the low 4 bits, in workgroup memory with
   atomics, then merged into a global histogram with device-scope atomics.
   Also records popcounts, and uses cas for a running max. */
unsigned gpu_global_id(int dim);
unsigned gpu_local_id(int dim);
void gpu_barrier(void);
unsigned gpu_atomic_add(unsigned *p, unsigned v);
unsigned gpu_atomic_max(unsigned *p, unsigned v);
unsigned gpu_atomic_or(unsigned *p, unsigned v);
unsigned gpu_atomic_cas(unsigned *p, unsigned expected, unsigned desired);
unsigned gpu_atomic_load(unsigned *p);
void gpu_atomic_store(unsigned *p, unsigned v);
unsigned gpu_popcount(unsigned x);

static unsigned bins[16];
static unsigned flag;

void hist(unsigned *keys, unsigned *global, unsigned *pop, unsigned *maxout, unsigned n)
assert hist in gpu.compute(local_size=32)
assert keys in gpu.buffer(binding=0, readonly)
assert global in gpu.buffer(binding=1)
assert pop in gpu.buffer(binding=2)
assert maxout in gpu.buffer(binding=3)
assert bins in gpu.shared
assert flag in gpu.shared
{
    unsigned l = gpu_local_id(0);
    unsigned g = gpu_global_id(0);
    if (l < 16) gpu_atomic_store(&bins[l], 0);
    if (l == 0) gpu_atomic_store(&flag, 0);
    gpu_barrier();
    if (g < n) {
        unsigned k = keys[g];
        gpu_atomic_add(&bins[k & 15], 1);
        gpu_atomic_or(&flag, k);
        gpu_atomic_max(&maxout[0], k);
        pop[g] = gpu_popcount(k);
    }
    gpu_barrier();
    if (l < 16) gpu_atomic_add(&global[l], gpu_atomic_load(&bins[l]));
    if (l == 0) {
        /* claim slot 1 once per workgroup: cas 0 -> flag's low bits + 1 */
        gpu_atomic_cas(&maxout[1], 0, gpu_atomic_load(&flag) + 1);
    }
}
