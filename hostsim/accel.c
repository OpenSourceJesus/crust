/* accel.c - the host's compute, offered to firmware as an accelerator.
 *
 * A Jetson's whole reason for existing is the GPU next to the CPU. Firmware
 * that runs an inference every frame cannot be studied under an instruction
 * emulator -- armulator would need about a day per simulated second, and it
 * models no GPU in any case. Compiled for the host, the same firmware can
 * call straight into whatever the host has: CUDA, cuDNN, BLAS, or the plain C
 * below.
 *
 * The interface is deliberately narrow -- one call that takes a frame and
 * returns a classification -- because that is the shape of the seam on a real
 * board too. There, `accel_infer` would hand a buffer to the GPU through the
 * vendor runtime; here it does the arithmetic on the host. What the firmware
 * sees either way is a function that takes a while and returns a label.
 *
 * Two implementations:
 *
 *   HOSTSIM_CUDA unset (the default)
 *       Plain C. Always builds, always tested, needs no GPU.
 *
 *   HOSTSIM_CUDA set, compiled by nvcc
 *       The same arithmetic in a kernel. See accel_cuda.cu.
 *
 * Both must agree. tools/hostsim_test.py checks the C path against known
 * values; when a GPU is present, accel_selftest() compares the two directly.
 *
 * IMPORTANT: the CUDA path has never been run. There is no GPU and no CUDA
 * toolkit in the environment this was developed in, so accel_cuda.cu is
 * written from the programming guide and compiles nowhere here. Treat it as a
 * sketch of the integration, not as working code, until someone runs it on a
 * Jetson.
 */

#include "accel.h"

/* A tiny classifier: correlate the frame against each class template and
 * report the best match. Not machine learning -- it stands in for a real
 * network at the point where the seam matters, which is the shape of the
 * call and the cost of it, not the arithmetic inside.
 *
 * Integer throughout, so the CPU and GPU paths agree bit for bit. Floating
 * point would let them differ in the last place and turn the comparison in
 * accel_selftest() into a tolerance argument. */

static int templates_ready;
static signed char templates[ACCEL_CLASSES][ACCEL_FRAME];

/* Deterministic pseudo-random templates, so a run is reproducible and no
 * weights file is needed. */
static void build_templates(void)
{
    unsigned int state = 0x1234567u;
    int c;
    int i;
    for (c = 0; c < ACCEL_CLASSES; c++) {
        for (i = 0; i < ACCEL_FRAME; i++) {
            state = state * 1103515245u + 12345u;
            templates[c][i] = (signed char)((state >> 16) & 0x7F) - 64;
        }
    }
    templates_ready = 1;
}

void accel_init(void)
{
    if (!templates_ready) {
        build_templates();
    }
}

/* Copy the templates out flat, for a backend that needs its own device copy.
 * `out` must have room for ACCEL_CLASSES * ACCEL_FRAME bytes. */
void accel_build_templates_into(signed char *out)
{
    int c;
    int i;
    accel_init();
    for (c = 0; c < ACCEL_CLASSES; c++) {
        for (i = 0; i < ACCEL_FRAME; i++) {
            out[c * ACCEL_FRAME + i] = templates[c][i];
        }
    }
}

/* The reference implementation, and the one that runs unless the build was
 * given a GPU. */
int accel_infer_cpu(const unsigned char *frame, long *score_out)
{
    long best_score = -1;
    int best_class = -1;
    int c;
    int i;

    accel_init();

    for (c = 0; c < ACCEL_CLASSES; c++) {
        long score = 0;
        for (i = 0; i < ACCEL_FRAME; i++) {
            score += (long)templates[c][i] * (long)frame[i];
        }
        if (score > best_score) {
            best_score = score;
            best_class = c;
        }
    }
    if (score_out) {
        *score_out = best_score;
    }
    return best_class;
}

#ifndef HOSTSIM_CUDA

int accel_infer(const unsigned char *frame, long *score_out)
{
    return accel_infer_cpu(frame, score_out);
}

int accel_available(void)
{
    return 0;               /* no GPU in this build */
}

const char *accel_backend(void)
{
    return "cpu";
}

int accel_selftest(void)
{
    return 0;               /* nothing to compare against */
}

#endif /* HOSTSIM_CUDA */
