// rcrt.s -- the freestanding startup and runtime for programs linked by rlink.
//
// A program built by the self-hosted chain has no libc: ShivyCX compiles the C,
// rasm assembles it, and rlink links it, so something has to provide _start and
// the handful of runtime calls test programs need. That is this file. It talks
// to the kernel directly through `syscall`, so nothing outside the repository
// is involved in producing or running the resulting binary.
//
// System V x86-64: syscall number in rax, arguments in rdi/rsi/rdx/r10/r8/r9,
// result in rax; rcx and r11 are clobbered by the syscall instruction itself.
//
// At process entry the stack holds argc, then argv[], then a NULL, then envp[].
// There is no return address: _start must never `ret`.

.intel_syntax noprefix

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
.global setjmp
.global longjmp
.global _setjmp
.global _longjmp

.text

_start:
    mov rdi, QWORD PTR [rsp]            // argc
    lea rsi, QWORD PTR [rsp+8]          // argv
    // envp follows argv and its NULL terminator: rsp + 8 + 8*(argc+1)
    lea rdx, QWORD PTR [rdi+1]
    lea rdx, QWORD PTR [rsi+rdx*8]
    mov QWORD PTR [rip+environ], rdx
    and rsp, -16                        // ABI: 16-byte aligned before a call
    call main
    mov edi, eax
    call exit

// void exit(int status) -- SYS_exit_group(231), never returns
exit:
    mov eax, 231
    syscall
    jmp exit

// long write(int fd, const void *buf, unsigned long n) -- SYS_write(1)
write:
    mov eax, 1
    syscall
    ret

// long read(int fd, void *buf, unsigned long n) -- SYS_read(0)
read:
    mov eax, 0
    syscall
    ret

// unsigned long strlen(const char *s)
strlen:
    xor eax, eax
strlen_loop:
    cmp BYTE PTR [rdi+rax], 0
    je strlen_done
    inc rax
    jmp strlen_loop
strlen_done:
    ret

// int puts(const char *s) -- writes s followed by a newline to stdout
puts:
    push rbx
    mov rbx, rdi
    call strlen
    mov rdx, rax
    mov rsi, rbx
    mov edi, 1
    call write
    lea rsi, QWORD PTR [rip+nl]
    mov edx, 1
    mov edi, 1
    call write
    pop rbx
    xor eax, eax
    ret

// int putchar(int c)
putchar:
    mov BYTE PTR [rip+chbuf], dil
    lea rsi, QWORD PTR [rip+chbuf]
    mov edx, 1
    mov edi, 1
    call write
    xor eax, eax
    ret

// void putint(long n) -- decimal, no newline
putint:
    push rbx
    push r12
    mov rax, rdi
    lea r12, QWORD PTR [rip+numbuf]
    add r12, 31
    mov BYTE PTR [r12], 0
    xor ebx, ebx                        // negative flag
    cmp rax, 0
    jge putint_pos
    mov ebx, 1
    neg rax
putint_pos:
    mov rcx, 10
putint_loop:
    cqo
    idiv rcx
    add rdx, 48
    dec r12
    mov BYTE PTR [r12], dl
    cmp rax, 0
    jne putint_loop
    cmp ebx, 0
    je putint_out
    dec r12
    mov BYTE PTR [r12], 45              // '-'
putint_out:
    mov rdi, r12
    call strlen
    mov rdx, rax
    mov rsi, r12
    mov edi, 1
    call write
    pop r12
    pop rbx
    ret

// void *memset(void *dst, int c, unsigned long n)
memset:
    mov r8, rdi
    mov rax, rdi
    xor r9d, r9d
memset_loop:
    cmp r9, rdx
    jae memset_done
    mov BYTE PTR [r8+r9], sil
    inc r9
    jmp memset_loop
memset_done:
    ret

// void *memcpy(void *dst, const void *src, unsigned long n)
memcpy:
    mov rax, rdi
    xor r9d, r9d
memcpy_loop:
    cmp r9, rdx
    jae memcpy_done
    mov r10b, BYTE PTR [rsi+r9]
    mov BYTE PTR [rdi+r9], r10b
    inc r9
    jmp memcpy_loop
memcpy_done:
    ret

// void *sbrk(long increment) -- SYS_brk(12) based
sbrk:
    push rbx
    push r12                            // r12 is callee-saved: the caller's
    mov rbx, rdi                        // value must survive this call
    mov rax, QWORD PTR [rip+brk_cur]
    cmp rax, 0
    jne sbrk_have
    xor edi, edi                        // brk(0) queries the current break
    mov eax, 12
    syscall
    mov QWORD PTR [rip+brk_cur], rax
sbrk_have:
    mov rax, QWORD PTR [rip+brk_cur]
    mov rdi, rax
    add rdi, rbx
    mov r12, rax
    mov eax, 12
    syscall
    cmp rax, r12
    jl sbrk_fail
    mov QWORD PTR [rip+brk_cur], rax
    mov rax, r12
    pop r12
    pop rbx
    ret
sbrk_fail:
    mov rax, -1
    pop r12
    pop rbx
    ret

// int setjmp(jmp_buf b) / void longjmp(jmp_buf b, int val)
// The buffer holds the six callee-saved registers, the stack pointer as it
// will be after `ret`, and the return address. longjmp restores them and jumps
// straight back, so control resumes inside setjmp's caller with the given
// value (never zero -- that is reserved for the original call).
setjmp:
_setjmp:
    mov QWORD PTR [rdi], rbx
    mov QWORD PTR [rdi+8], rbp
    mov QWORD PTR [rdi+16], r12
    mov QWORD PTR [rdi+24], r13
    mov QWORD PTR [rdi+32], r14
    mov QWORD PTR [rdi+40], r15
    lea rax, QWORD PTR [rsp+8]
    mov QWORD PTR [rdi+48], rax
    mov rax, QWORD PTR [rsp]
    mov QWORD PTR [rdi+56], rax
    xor eax, eax
    ret

longjmp:
_longjmp:
    mov rbx, QWORD PTR [rdi]
    mov rbp, QWORD PTR [rdi+8]
    mov r12, QWORD PTR [rdi+16]
    mov r13, QWORD PTR [rdi+24]
    mov r14, QWORD PTR [rdi+32]
    mov r15, QWORD PTR [rdi+40]
    mov rsp, QWORD PTR [rdi+48]
    mov eax, esi
    cmp eax, 0
    jne longjmp_go
    mov eax, 1
longjmp_go:
    jmp QWORD PTR [rdi+56]

// long rsyscall1(long n, long a)  /  long rsyscall3(long n, long a, long b, long c)
// Generic escape hatches so the C runtime can reach syscalls that do not
// deserve their own stub. The syscall number arrives in rdi, so shuffle the
// arguments down into the kernel's argument registers first.
rsyscall1:
    mov rax, rdi
    mov rdi, rsi
    syscall
    ret

rsyscall3:
    mov rax, rdi
    mov rdi, rsi
    mov rsi, rdx
    mov rdx, rcx
    syscall
    ret

.data
nl:
    .byte 10
chbuf:
    .byte 0
brk_cur:
    .quad 0
environ:
    .quad 0

.bss
numbuf:
    .zero 32
