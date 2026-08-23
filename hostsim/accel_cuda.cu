/* accel_cuda.cu - the accelerator, on a GPU.
 *
 * ============================ UNVERIFIED ============================
 * This file has never been compiled or run. It was written in an
 * environment with no CUDA toolkit and no GPU, from the programming
 * guide. It is here to show the shape of the integration -- where the
 * seam falls, who owns the buffers, what the firmware sees -- and not
 * as code known to work.
 *
 * Before trusting it on a Jetson:
 *
 *     python3 tools/hostsim_build.py examples/hostsim/vision_node.c \
 *         --cuda -o /tmp/vision.so
 *     python3 -c "import sys; sys.path.insert(0,'tools'); \
 *                 from hostsim import Sim; s=Sim('/tmp/vision.so'); \
 *                 print(s.accel_backend, s.accel_selftest())"
 *
 * accel_selftest() must return 0: it runs the same frames through this
 * kernel and through the plain C in accel.c and counts disagreements.
 * Everything is integer arithmetic precisely so that comparison can be
 * exact rather than a tolerance argument.
 * ====================================================================
 *
 * Build:  nvcc -O3 -Xcompiler -fPIC -DHOSTSIM_CUDA -c accel_cuda.cu
 */

#include <cuda_runtime.h>
#include <stdio.h>

extern "C" {
#include "accel.h"
}

/* The templates live in accel.c; this needs its own device copy. Uploaded
 * once, because a per-frame upload of weights would dominate the timing and
 * misrepresent what a real inference costs. */
static signed char *d_templates;
static unsigned char *d_frame;
static long *d_scores;
static int cuda_ready;
static int cuda_usable;

extern "C" void accel_build_templates_into(signed char *out);

/* One block per class, threads striding the frame, then a reduction in
 * shared memory. The frame is small, so this is latency-bound rather than
 * bandwidth-bound -- which is the honest reason a real deployment would batch
 * frames instead of running them one at a time like this. */
__global__ void infer_kernel(const signed char *templates,
                             const unsigned char *frame,
                             long *scores, int frame_len)
{
    extern __shared__ long partial[];
    int cls = blockIdx.x;
    int tid = threadIdx.x;
    long sum = 0;

    for (int i = tid; i < frame_len; i += blockDim.x) {
        sum += (long)templates[cls * frame_len + i] * (long)frame[i];
    }
    partial[tid] = sum;
    __syncthreads();

    for (int stride = blockDim.x / 2; stride > 0; stride >>= 1) {
        if (tid < stride) {
            partial[tid] += partial[tid + stride];
        }
        __syncthreads();
    }
    if (tid == 0) {
        scores[cls] = partial[0];
    }
}

static int cuda_setup(void)
{
    if (cuda_ready) {
        return cuda_usable;
    }
    cuda_ready = 1;
    cuda_usable = 0;

    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) {
        return 0;                   /* built with CUDA, running without a GPU */
    }

    accel_init();

    signed char host_templates[ACCEL_CLASSES * ACCEL_FRAME];
    accel_build_templates_into(host_templates);

    if (cudaMalloc(&d_templates, sizeof(host_templates)) != cudaSuccess ||
        cudaMalloc(&d_frame, ACCEL_FRAME) != cudaSuccess ||
        cudaMalloc(&d_scores, ACCEL_CLASSES * sizeof(long)) != cudaSuccess) {
        return 0;
    }
    if (cudaMemcpy(d_templates, host_templates, sizeof(host_templates),
                   cudaMemcpyHostToDevice) != cudaSuccess) {
        return 0;
    }
    cuda_usable = 1;
    return 1;
}

extern "C" int accel_available(void)
{
    return cuda_setup();
}

extern "C" const char *accel_backend(void)
{
    return cuda_setup() ? "cuda" : "cpu (cuda build, no device)";
}

extern "C" int accel_infer(const unsigned char *frame, long *score_out)
{
    /* Falling back rather than failing: a build made for a Jetson should
     * still run on a developer's laptop, just slower. */
    if (!cuda_setup()) {
        return accel_infer_cpu(frame, score_out);
    }

    const int threads = 256;
    long scores[ACCEL_CLASSES];

    if (cudaMemcpy(d_frame, frame, ACCEL_FRAME,
                   cudaMemcpyHostToDevice) != cudaSuccess) {
        return accel_infer_cpu(frame, score_out);
    }

    infer_kernel<<<ACCEL_CLASSES, threads, threads * sizeof(long)>>>(
        d_templates, d_frame, d_scores, ACCEL_FRAME);

    if (cudaDeviceSynchronize() != cudaSuccess ||
        cudaMemcpy(scores, d_scores, sizeof(scores),
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
        return accel_infer_cpu(frame, score_out);
    }

    long best = -1;
    int best_class = -1;
    for (int c = 0; c < ACCEL_CLASSES; c++) {
        if (scores[c] > best) {
            best = scores[c];
            best_class = c;
        }
    }
    if (score_out) {
        *score_out = best;
    }
    return best_class;
}

/* Run both paths over generated frames and count disagreements. This is the
 * check that the GPU path is actually equivalent, and the reason the
 * arithmetic is integer. */
extern "C" int accel_selftest(void)
{
    if (!cuda_setup()) {
        return 0;                   /* nothing to compare against */
    }

    unsigned char frame[ACCEL_FRAME];
    unsigned int state = 99991u;
    int mismatches = 0;

    for (int trial = 0; trial < 32; trial++) {
        for (int i = 0; i < ACCEL_FRAME; i++) {
            state = state * 1103515245u + 12345u;
            frame[i] = (unsigned char)((state >> 16) & 0xFF);
        }
        long gpu_score = 0;
        long cpu_score = 0;
        int gpu = accel_infer(frame, &gpu_score);
        int cpu = accel_infer_cpu(frame, &cpu_score);
        if (gpu != cpu || gpu_score != cpu_score) {
            mismatches++;
            fprintf(stderr,
                    "accel_selftest: trial %d gpu=(%d,%ld) cpu=(%d,%ld)\n",
                    trial, gpu, gpu_score, cpu, cpu_score);
        }
    }
    return mismatches;
}
