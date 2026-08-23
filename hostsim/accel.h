/* accel.h - the accelerator seam.
 *
 * On a Jetson this would be backed by the GPU through the vendor runtime; on
 * the host it is backed by whatever the build was given. Firmware sees the
 * same two calls either way.
 */
#ifndef HOSTSIM_ACCEL_H
#define HOSTSIM_ACCEL_H

/* Samples per frame, and how many classes the classifier knows. Small enough
 * to keep the example readable; the seam does not change with the size. */
#define ACCEL_FRAME   1024
#define ACCEL_CLASSES 8

void accel_init(void);

/* Copy the class templates out flat, for a backend that keeps its own copy
 * (a GPU, for instance). Needs room for ACCEL_CLASSES * ACCEL_FRAME bytes. */
void accel_build_templates_into(signed char *out);

/* Classify one frame. Returns the class, and writes its score through
 * `score_out` when that is not NULL. */
int accel_infer(const unsigned char *frame, long *score_out);

/* The plain C implementation, exposed so a GPU build can be checked against
 * it rather than trusted. */
int accel_infer_cpu(const unsigned char *frame, long *score_out);

/* Non-zero when a real accelerator is behind accel_infer. */
int accel_available(void);

/* "cpu", "cuda", and so on -- for firmware that wants to report what it
 * actually ran on. */
const char *accel_backend(void);

/* Compare the accelerator against the reference on generated frames.
 * Returns the number of disagreements: 0 is a pass, and 0 is also what a
 * CPU-only build returns because there is nothing to compare. */
int accel_selftest(void);

#endif
