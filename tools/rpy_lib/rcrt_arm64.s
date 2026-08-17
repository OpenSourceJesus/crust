// rcrt_arm64.s -- freestanding startup and runtime for AArch64 programs
// linked by rlink, the counterpart of rcrt.s on x86-64.
//
// A program built by the self-hosted chain has no libc: ShivyCX compiles the
// C, rasm assembles it, and rlink links it, so something has to provide
// _start and the handful of runtime calls test programs need. This file talks
// to the kernel directly through `svc #0`, so nothing outside the repository
// is involved in producing or running the resulting binary.
//
// AAPCS64 / Linux: syscall number in x8, arguments in x0-x5, result in x0.
// The syscall numbers differ from x86-64 -- AArch64 uses the "generic" table,
// so exit_group is 94, not 231, and there is no SYS_open (only openat).
//
// At process entry sp points at argc, then argv[], then a NULL, then envp[].
// There is no return address: _start must never `ret`.

.global _start
.global exit
.global write
.global read
.global puts
.global putchar
.global putint
.global strlen
.global memset
.global memcpy
.global sbrk
.global environ
.global rsyscall1
.global rsyscall3

.text

_start:
    ldr     x0, [sp]                    // argc
    add     x1, sp, #8                  // argv
    // envp follows argv and its NULL terminator: sp + 8 + 8*(argc+1)
    add     x2, x0, #1
    add     x2, x1, x2, lsl #3
    adrp    x3, environ
    add     x3, x3, :lo12:environ
    str     x2, [x3]
    // The ABI wants a 16-byte aligned sp before a call; the kernel already
    // guarantees it here, but re-align defensively since we adjusted nothing.
    mov     x29, #0
    mov     x30, #0
    bl      main
    bl      exit

// void exit(int status) -- SYS_exit_group(94), never returns
exit:
    mov     x8, #94
    svc     #0
    b       exit

// long write(int fd, const void *buf, unsigned long n) -- SYS_write(64)
write:
    mov     x8, #64
    svc     #0
    ret

// long read(int fd, void *buf, unsigned long n) -- SYS_read(63)
read:
    mov     x8, #63
    svc     #0
    ret

// unsigned long strlen(const char *s)
strlen:
    mov     x1, x0
strlen_loop:
    ldrb    w2, [x1]
    cbz     w2, strlen_done
    add     x1, x1, #1
    b       strlen_loop
strlen_done:
    sub     x0, x1, x0
    ret

// int puts(const char *s) -- writes s then a newline, like the C library
puts:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    str     x19, [x29, #16]
    mov     x19, x0
    bl      strlen
    mov     x2, x0                      // length
    mov     x1, x19                     // buffer
    mov     x0, #1                      // stdout
    bl      write
    adrp    x1, rcrt_nl
    add     x1, x1, :lo12:rcrt_nl
    mov     x0, #1
    mov     x2, #1
    bl      write
    mov     w0, #0
    ldr     x19, [x29, #16]
    ldp     x29, x30, [sp], #32
    ret

// int putchar(int c)
putchar:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    adrp    x1, rcrt_ch
    add     x1, x1, :lo12:rcrt_ch
    strb    w0, [x1]
    mov     x0, #1
    mov     x2, #1
    bl      write
    mov     w0, #0
    ldp     x29, x30, [sp], #32
    ret

// void putint(long n) -- decimal, no newline
putint:
    stp     x29, x30, [sp, #-48]!
    mov     x29, sp
    str     x19, [x29, #16]
    str     x20, [x29, #24]
    adrp    x19, rcrt_numbuf
    add     x19, x19, :lo12:rcrt_numbuf
    add     x19, x19, #31               // fill backwards from the end
    mov     x20, #0                     // digit count
    mov     x3, x0
    cmp     x3, #0
    b.ge    putint_pos
    neg     x3, x3
    mov     w4, #1                      // negative flag
    b       putint_digits
putint_pos:
    mov     w4, #0
putint_digits:
    mov     x5, #10
putint_loop:
    udiv    x6, x3, x5
    msub    x7, x6, x5, x3              // remainder = x3 - x6*10
    add     w7, w7, #48                 // '0'
    strb    w7, [x19]
    sub     x19, x19, #1
    add     x20, x20, #1
    mov     x3, x6
    cbnz    x3, putint_loop
    cbz     w4, putint_emit
    mov     w7, #45                     // '-'
    strb    w7, [x19]
    sub     x19, x19, #1
    add     x20, x20, #1
putint_emit:
    add     x1, x19, #1
    mov     x2, x20
    mov     x0, #1
    bl      write
    ldr     x19, [x29, #16]
    ldr     x20, [x29, #24]
    ldp     x29, x30, [sp], #48
    ret

// void *memset(void *d, int c, unsigned long n)
memset:
    mov     x3, x0
memset_loop:
    cbz     x2, memset_done
    strb    w1, [x3]
    add     x3, x3, #1
    sub     x2, x2, #1
    b       memset_loop
memset_done:
    ret

// void *memcpy(void *d, const void *s, unsigned long n)
memcpy:
    mov     x3, x0
memcpy_loop:
    cbz     x2, memcpy_done
    ldrb    w4, [x1]
    strb    w4, [x3]
    add     x1, x1, #1
    add     x3, x3, #1
    sub     x2, x2, #1
    b       memcpy_loop
memcpy_done:
    ret

// void *sbrk(long inc) -- SYS_brk(214). brk returns the new break, so the
// first call passes 0 to discover the current one.
sbrk:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    str     x19, [x29, #16]
    mov     x19, x0
    adrp    x1, rcrt_brk
    add     x1, x1, :lo12:rcrt_brk
    ldr     x2, [x1]
    cbnz    x2, sbrk_have
    mov     x0, #0
    mov     x8, #214
    svc     #0
    adrp    x1, rcrt_brk
    add     x1, x1, :lo12:rcrt_brk
    str     x0, [x1]
    mov     x2, x0
sbrk_have:
    add     x0, x2, x19                 // requested new break
    mov     x8, #214
    svc     #0
    adrp    x1, rcrt_brk
    add     x1, x1, :lo12:rcrt_brk
    ldr     x2, [x1]
    str     x0, [x1]
    mov     x0, x2                      // return the *old* break
    ldr     x19, [x29, #16]
    ldp     x29, x30, [sp], #32
    ret

// long rsyscall1(long n, long a)
rsyscall1:
    mov     x8, x0
    mov     x0, x1
    svc     #0
    ret

// long rsyscall3(long n, long a, long b, long c)
rsyscall3:
    mov     x8, x0
    mov     x0, x1
    mov     x1, x2
    mov     x2, x3
    svc     #0
    ret

.section .data
.align 3
environ:
    .quad 0
rcrt_brk:
    .quad 0
rcrt_nl:
    .byte 10
rcrt_ch:
    .byte 0
.align 3
rcrt_numbuf:
    .zero 32
