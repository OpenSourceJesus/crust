# rcrt_riscv64.s -- freestanding startup and runtime for RV64 programs
# linked by rlink, the counterpart of rcrt.s (x86-64) and rcrt_arm64.s.
#
# A program built by the self-hosted chain has no libc: ShivyCX compiles the
# C, rasm assembles it, and rlink links it, so something has to provide
# _start and the handful of runtime calls test programs need. This file talks
# to the kernel directly through `ecall`.
#
# RV64 lp64d / Linux: syscall number in a7, arguments in a0-a5, result in a0.
# The numbers are the same "generic" table AArch64 uses -- exit_group is 94,
# write 64, read 63, brk 214 -- not the x86-64 ones.
#
# At process entry sp points at argc, then argv[], then a NULL, then envp[].
# There is no return address: _start must never `ret`.
#
# gp must be established before anything can use gp-relative addressing, and
# the instruction sequence that loads it must not itself be relaxed into a
# gp-relative form -- hence the .option norelax around it.

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
.option push
.option norelax
	lla	gp, __global_pointer$
.option pop
	ld	a0, 0(sp)               # argc
	addi	a1, sp, 8               # argv
	# envp follows argv and its NULL terminator: sp + 8 + 8*(argc+1)
	addi	a2, a0, 1
	slli	a2, a2, 3
	add	a2, a1, a2
	lla	t0, environ
	sd	a2, 0(t0)
	li	s0, 0
	li	ra, 0
	call	main
	call	exit

# void exit(int status) -- SYS_exit_group(94), never returns
exit:
	li	a7, 94
	ecall
	j	exit

# long write(int fd, const void *buf, unsigned long n) -- SYS_write(64)
write:
	li	a7, 64
	ecall
	ret

# long read(int fd, void *buf, unsigned long n) -- SYS_read(63)
read:
	li	a7, 63
	ecall
	ret

# unsigned long strlen(const char *s)
strlen:
	mv	t0, a0
strlen_loop:
	lbu	t1, 0(t0)
	beqz	t1, strlen_done
	addi	t0, t0, 1
	j	strlen_loop
strlen_done:
	sub	a0, t0, a0
	ret

# int puts(const char *s) -- writes s then a newline, like the C library
puts:
	addi	sp, sp, -32
	sd	ra, 24(sp)
	sd	s1, 16(sp)
	mv	s1, a0
	call	strlen
	mv	a2, a0                  # length
	mv	a1, s1                  # buffer
	li	a0, 1                   # stdout
	call	write
	lla	a1, rcrt_nl
	li	a0, 1
	li	a2, 1
	call	write
	li	a0, 0
	ld	s1, 16(sp)
	ld	ra, 24(sp)
	addi	sp, sp, 32
	ret

# int putchar(int c)
putchar:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	lla	t0, rcrt_ch
	sb	a0, 0(t0)
	mv	a1, t0
	li	a0, 1
	li	a2, 1
	call	write
	li	a0, 0
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret

# void putint(long n) -- decimal, no newline
putint:
	addi	sp, sp, -48
	sd	ra, 40(sp)
	sd	s1, 32(sp)
	sd	s2, 24(sp)
	lla	s1, rcrt_numbuf
	addi	s1, s1, 31              # fill backwards from the end
	li	s2, 0                   # digit count
	mv	t0, a0
	li	t3, 0                   # negative flag
	bgez	t0, putint_digits
	sub	t0, zero, t0
	li	t3, 1
putint_digits:
	li	t1, 10
putint_loop:
	div	t2, t0, t1
	mul	t4, t2, t1
	sub	t4, t0, t4              # remainder
	addi	t4, t4, 48              # '0'
	sb	t4, 0(s1)
	addi	s1, s1, -1
	addi	s2, s2, 1
	mv	t0, t2
	bnez	t0, putint_loop
	beqz	t3, putint_emit
	li	t4, 45                  # '-'
	sb	t4, 0(s1)
	addi	s1, s1, -1
	addi	s2, s2, 1
putint_emit:
	addi	a1, s1, 1
	mv	a2, s2
	li	a0, 1
	call	write
	ld	s2, 24(sp)
	ld	s1, 32(sp)
	ld	ra, 40(sp)
	addi	sp, sp, 48
	ret

# void *memset(void *d, int c, unsigned long n)
memset:
	mv	t0, a0
memset_loop:
	beqz	a2, memset_done
	sb	a1, 0(t0)
	addi	t0, t0, 1
	addi	a2, a2, -1
	j	memset_loop
memset_done:
	ret

# void *memcpy(void *d, const void *s, unsigned long n)
memcpy:
	mv	t0, a0
memcpy_loop:
	beqz	a2, memcpy_done
	lbu	t1, 0(a1)
	sb	t1, 0(t0)
	addi	a1, a1, 1
	addi	t0, t0, 1
	addi	a2, a2, -1
	j	memcpy_loop
memcpy_done:
	ret

# void *sbrk(long inc) -- SYS_brk(214). brk returns the new break, so the
# first call passes 0 to discover the current one.
sbrk:
	addi	sp, sp, -32
	sd	ra, 24(sp)
	sd	s1, 16(sp)
	mv	s1, a0
	lla	t0, rcrt_brk
	ld	t1, 0(t0)
	bnez	t1, sbrk_have
	li	a0, 0
	li	a7, 214
	ecall
	lla	t0, rcrt_brk
	sd	a0, 0(t0)
	mv	t1, a0
sbrk_have:
	add	a0, t1, s1              # requested new break
	li	a7, 214
	ecall
	lla	t0, rcrt_brk
	ld	t1, 0(t0)
	sd	a0, 0(t0)
	mv	a0, t1                  # return the *old* break
	ld	s1, 16(sp)
	ld	ra, 24(sp)
	addi	sp, sp, 32
	ret

# long rsyscall1(long n, long a)
rsyscall1:
	mv	a7, a0
	mv	a0, a1
	ecall
	ret

# long rsyscall3(long n, long a, long b, long c)
rsyscall3:
	mv	a7, a0
	mv	a0, a1
	mv	a1, a2
	mv	a2, a3
	ecall
	ret

.section .data
.align 3
environ:
	.dword 0
rcrt_brk:
	.dword 0
rcrt_nl:
	.byte 10
rcrt_ch:
	.byte 0
.align 3
rcrt_numbuf:
	.zero 32
