unsigned gpu_global_id(int dim);
unsigned gpu_local_id(int dim);
float fabsf(float x);

static unsigned collatz(unsigned n)
{
    unsigned steps = 0;
    while (n != 1 && steps < 200) {
        if (n & 1) n = 3 * n + 1; else n = n >> 1;
        steps++;
    }
    return steps;
}

void stress(int *in, int *out, float *f, unsigned n)
assert stress in gpu.compute(local_size=(8, 2))
assert in in gpu.buffer(binding=0, readonly)
assert out in gpu.buffer(binding=1)
assert f in gpu.buffer(binding=2)
{
    unsigned x = gpu_global_id(0);
    unsigned y = gpu_global_id(1);
    unsigned i = y * 16 + x;
    long idx = (long)i;
    int acc = 0, k, j;
    if (i >= n) return;
    for (k = 0; k < 5; k++) {
        if (k == 3) continue;
        for (j = 0; j < 4; j++) {
            if (j > k) break;
            acc += in[idx] * (k + j);
        }
    }
    switch (in[idx] & 3) {
    case 0: acc += 100; break;
    case 1: acc -= 7;
    case 2: acc ^= 0x55; break;
    default: acc = -acc;
    }
    out[idx] = acc + (int)collatz(in[idx] < 0 ? -in[idx] : in[idx] + 1);
    f[idx] = fabsf((float)acc) / 3.0f - (float)(acc % 5);
    acc = (acc << 3) >> 1;
    out[idx] += acc / 7 + (int)(unsigned)acc % 11;
}
