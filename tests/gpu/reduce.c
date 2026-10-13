/* Per-workgroup sum: stage into workgroup memory, then halve the active
   range each step with a barrier between steps. */
unsigned gpu_global_id(int dim);
unsigned gpu_local_id(int dim);
unsigned gpu_group_id(int dim);
void gpu_barrier(void);

static unsigned scratch[64];

void reduce(unsigned *in, unsigned *sums, unsigned n)
assert reduce in gpu.compute(local_size=64)
assert in in gpu.buffer(binding=0, readonly)
assert sums in gpu.buffer(binding=1)
assert scratch in gpu.shared
{
    unsigned l = gpu_local_id(0);
    unsigned g = gpu_global_id(0);
    unsigned v = 0;
    if (g < n) v = in[g];
    scratch[l] = v;
    gpu_barrier();
    for (unsigned s = 32; s > 0; s = s >> 1) {
        if (l < s) scratch[l] = scratch[l] + scratch[l + s];
        gpu_barrier();
    }
    if (l == 0) sums[gpu_group_id(0)] = scratch[0];
}
