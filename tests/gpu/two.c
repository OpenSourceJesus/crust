unsigned gpu_global_id(int dim);
void inc(unsigned *in, unsigned *out)
assert inc in gpu.compute(local_size=4)
assert in in gpu.buffer(binding=0, readonly)
assert out in gpu.buffer(binding=1)
{
    unsigned i = gpu_global_id(0);
    out[i] = in[i] + 1u;
}
void dbl(unsigned *in, unsigned *out)
assert dbl in gpu.compute(local_size=4)
assert in in gpu.buffer(binding=0, readonly)
assert out in gpu.buffer(binding=1)
{
    unsigned i = gpu_global_id(0);
    unsigned k;
    out[i] = in[i];
    for (k = 0; k < 1u; k++)
        out[i] = out[i] * 2u;
}
