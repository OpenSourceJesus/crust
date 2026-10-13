unsigned gpu_global_id(int dim);
float sqrtf(float x);
static int clampi(int v, int lo, int hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}
void rowsum(float *m, int *out, unsigned cols)
assert rowsum in gpu.compute(local_size=4)
assert m in gpu.buffer(binding=0, readonly)
assert out in gpu.buffer(binding=1)
{
    unsigned r = gpu_global_id(0);
    float s = 0.0f;
    unsigned j;
    for (j = 0; j < cols; j++)
        s += m[r * cols + j];
    out[r] = clampi((int)sqrtf(s), 1, 5);
}
