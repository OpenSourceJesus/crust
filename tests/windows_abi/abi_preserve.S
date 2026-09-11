/* g_preserve(cb, arg) -- built by MinGW gcc into the reference DLL.
 *
 * Loads every register Win64 obliges a callee to preserve with a known
 * value, calls cb(arg) -- a register-hungry Crust function -- and returns a
 * bitmask of the registers that did not come back intact:
 *   1 rbx, 2 rsi, 4 rdi, 8 r12, 16 r13, 32 r14, 64 r15, 128 xmm6, 256 xmm7.
 * rsi and rdi are the interesting ones: System V makes them scratch, so a
 * Win64 back end that still treated them that way would fail here. */
    .text
    .globl g_preserve
g_preserve:
    pushq %rbx
    pushq %rsi
    pushq %rdi
    pushq %r12
    pushq %r13
    pushq %r14
    pushq %r15
    subq $64, %rsp                 /* 32 home + xmm6/xmm7 save; realigned */
    movups %xmm6, 32(%rsp)
    movups %xmm7, 48(%rsp)
    movabsq $0x1111111111111111, %rbx
    movabsq $0x2222222222222222, %rsi
    movabsq $0x3333333333333333, %rdi
    movabsq $0x4444444444444444, %r12
    movabsq $0x5555555555555555, %r13
    movabsq $0x6666666666666666, %r14
    movabsq $0x7777777777777777, %r15
    movq %rbx, %xmm6
    movq %rsi, %xmm7
    movq %rcx, %rax
    movq %rdx, %rcx
    call *%rax
    xorl %eax, %eax
    movabsq $0x1111111111111111, %rdx
    cmpq %rdx, %rbx
    je 1f
    orl $1, %eax
1:  movabsq $0x2222222222222222, %rdx
    cmpq %rdx, %rsi
    je 2f
    orl $2, %eax
2:  movabsq $0x3333333333333333, %rdx
    cmpq %rdx, %rdi
    je 3f
    orl $4, %eax
3:  movabsq $0x4444444444444444, %rdx
    cmpq %rdx, %r12
    je 4f
    orl $8, %eax
4:  movabsq $0x5555555555555555, %rdx
    cmpq %rdx, %r13
    je 5f
    orl $16, %eax
5:  movabsq $0x6666666666666666, %rdx
    cmpq %rdx, %r14
    je 6f
    orl $32, %eax
6:  movabsq $0x7777777777777777, %rdx
    cmpq %rdx, %r15
    je 7f
    orl $64, %eax
7:  movq %xmm6, %rdx
    movabsq $0x1111111111111111, %rcx
    cmpq %rcx, %rdx
    je 8f
    orl $128, %eax
8:  movq %xmm7, %rdx
    movabsq $0x2222222222222222, %rcx
    cmpq %rcx, %rdx
    je 9f
    orl $256, %eax
9:  movups 32(%rsp), %xmm6
    movups 48(%rsp), %xmm7
    addq $64, %rsp
    popq %r15
    popq %r14
    popq %r13
    popq %r12
    popq %rdi
    popq %rsi
    popq %rbx
    ret
