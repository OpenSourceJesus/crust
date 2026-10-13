/* crust_gpu.h -- the same kernel source on the CPU.
 *
 * Under `--target spirv` the gpu_* names are compiler builtins and this header
 * is not needed.  On any other target (or gcc/clang) this header supplies them
 * as sequential state, and gpu_launch() runs a kernel once per invocation, so
 * one source file can be differential-tested against the GPU module.
 */
#ifndef CRUST_GPU_H
#define CRUST_GPU_H
#ifndef __spirv__
static unsigned gpu__gid[3], gpu__lid[3], gpu__grp[3], gpu__ngrp[3];
static unsigned gpu_global_id(int d)  { return gpu__gid[d]; }
static unsigned gpu_local_id(int d)   { return gpu__lid[d]; }
static unsigned gpu_group_id(int d)   { return gpu__grp[d]; }
static unsigned gpu_num_groups(int d) { return gpu__ngrp[d]; }

/* Run body() for every invocation of a (gx,gy,gz) grid of (lx,ly,lz) groups. */
#define GPU_LAUNCH(gx, gy, gz, lx, ly, lz, CALL) do {                      \
    unsigned a_, b_, c_, i_, j_, k_;                                        \
    gpu__ngrp[0] = (gx); gpu__ngrp[1] = (gy); gpu__ngrp[2] = (gz);          \
    for (c_ = 0; c_ < (gz); c_++) for (b_ = 0; b_ < (gy); b_++)             \
    for (a_ = 0; a_ < (gx); a_++)                                           \
    for (k_ = 0; k_ < (lz); k_++) for (j_ = 0; j_ < (ly); j_++)             \
    for (i_ = 0; i_ < (lx); i_++) {                                         \
        gpu__grp[0] = a_; gpu__grp[1] = b_; gpu__grp[2] = c_;               \
        gpu__lid[0] = i_; gpu__lid[1] = j_; gpu__lid[2] = k_;               \
        gpu__gid[0] = a_ * (lx) + i_; gpu__gid[1] = b_ * (ly) + j_;         \
        gpu__gid[2] = c_ * (lz) + k_;                                       \
        CALL;                                                               \
    } } while (0)
#endif
#endif
