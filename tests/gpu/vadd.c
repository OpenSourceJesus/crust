unsigned gpu_global_id(int dim);
void vadd(float *a, float *b, float *c, unsigned n)
assert vadd in gpu.compute(local_size=64)
assert a in gpu.buffer(binding=0, readonly)
assert b in gpu.buffer(binding=1, readonly)
assert c in gpu.buffer(binding=2, writeonly)
{
    unsigned i = gpu_global_id(0);
    if (i < n) c[i] = a[i] + b[i];
}
