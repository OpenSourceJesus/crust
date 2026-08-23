#ifndef _SHIM_LINUX_ERRNO_H
#define _SHIM_LINUX_ERRNO_H
/* The subset of the kernel's errno values the portable DRM algorithm files
 * actually return. These are the asm-generic values, which every architecture
 * Linux supports uses for this range -- so the numbers matter only for
 * agreeing with a caller that also came from this tree.
 *
 * This must be a real header rather than an autostub placeholder: the values
 * are *used*, not merely included. drm_rect.c returns -EINVAL and
 * drm_displayid.c returns -ENOENT, so an empty stub compiles the include and
 * then fails on the identifier. */
#define EPERM            1      /* operation not permitted */
#define ENOENT           2      /* no such file or directory */
#define EIO              5      /* I/O error */
#define ENXIO            6      /* no such device or address */
#define EAGAIN          11      /* try again */
#define ENOMEM          12      /* out of memory */
#define EFAULT          14      /* bad address */
#define EBUSY           16      /* device or resource busy */
#define ENODEV          19      /* no such device */
#define EINVAL          22      /* invalid argument */
#define ENOSPC          28      /* no space left on device */
#define ERANGE          34      /* result out of range */
#define ENOSYS          38      /* function not implemented */
#define ENODATA         61      /* no data available */
#define EPROTO          71      /* protocol error */
#define EOVERFLOW       75      /* value too large for defined data type */
#define ENOTSUPP       524      /* operation not supported (kernel-internal) */
#define EOPNOTSUPP      95      /* operation not supported on transport */
#define EDEADLK         35      /* resource deadlock would occur */
#define EINTR            4      /* interrupted system call */
#define ETIMEDOUT      110      /* connection timed out */
#endif
