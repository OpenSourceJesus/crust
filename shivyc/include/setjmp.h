#ifndef _SHIVYC_SETJMP_H
#define _SHIVYC_SETJMP_H

/* Minimal setjmp.h for ShivyC's bundled fallback headers.
 *
 * The runtime (shivyc_rt.h) uses setjmp/longjmp for the transpiler's
 * exception mechanism, so any C that ShivyC compiles from its own transpiled
 * output must resolve <setjmp.h>. We only need a storage type of the right
 * size plus the two prototypes; setjmp/longjmp themselves resolve to glibc at
 * link time, so jmp_buf must match glibc's ABI footprint.
 *
 * On x86-64 glibc, jmp_buf is `struct __jmp_buf_tag[1]`, whose size is 200
 * bytes (8*8 saved registers + an int + a 128-byte sigset_t, padded). A flat
 * array of 25 longs (25 * 8 = 200) is layout-compatible for allocation, which
 * is all the caller does with it before passing &env[0] to setjmp/longjmp.
 */
#ifdef _WIN64
/* On Windows the pair comes from Crust's own runtime (rcrt_win64.s), not
 * msvcrt.dll: msvcrt's longjmp unwinds through each frame's SEH unwind
 * tables, which Crust does not emit, so it would abort the process. The
 * buffer holds the ten registers Win64 preserves (rbx rbp rdi rsi r12-r15,
 * the stack pointer and the return address) and xmm6-xmm15 -- 240 bytes --
 * sized like MSVC's 256-byte jmp_buf. */
typedef long long jmp_buf[32];
#else
typedef long jmp_buf[25];
#endif

int setjmp(jmp_buf env);
void longjmp(jmp_buf env, int val);

#endif /* _SHIVYC_SETJMP_H */
