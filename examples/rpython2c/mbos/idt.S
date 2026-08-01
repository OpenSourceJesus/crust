/* idt.S -- 64-bit (long mode) interrupt stub table for mbos.
 *
 * Provides isr0..isr31 (CPU exceptions) and irq0..irq15 (PIC, remapped to
 * vectors 32..47), plus the common stubs that save the full 64-bit register
 * frame, call the C dispatchers, and return with iretq.
 *
 * Frame layout pushed here must match `struct interrupt_frame64` in irq.c.
 */

.code64
.section .text

/* ----- per-vector stubs ------------------------------------------------- */
.macro ISR_NOERR num
.global isr\num
isr\num:
    cli
    pushq $0            /* dummy error code */
    pushq $\num         /* interrupt number */
    jmp isr_common_stub
.endm

.macro ISR_ERR num
.global isr\num
isr\num:
    cli
    pushq $\num         /* error code already pushed by CPU */
    jmp isr_common_stub
.endm

.macro IRQ num, vec
.global irq\num
irq\num:
    cli
    pushq $0
    pushq $\vec
    jmp irq_common_stub
.endm

ISR_NOERR 0
ISR_NOERR 1
ISR_NOERR 2
ISR_NOERR 3
ISR_NOERR 4
ISR_NOERR 5
ISR_NOERR 6
ISR_NOERR 7
ISR_ERR   8
ISR_NOERR 9
ISR_ERR   10
ISR_ERR   11
ISR_ERR   12
ISR_ERR   13
ISR_ERR   14
ISR_NOERR 15
ISR_NOERR 16
ISR_ERR   17
ISR_NOERR 18
ISR_NOERR 19
ISR_NOERR 20
ISR_NOERR 21
ISR_NOERR 22
ISR_NOERR 23
ISR_NOERR 24
ISR_NOERR 25
ISR_NOERR 26
ISR_NOERR 27
ISR_NOERR 28
ISR_NOERR 29
ISR_ERR   30
ISR_NOERR 31

IRQ 0, 32
IRQ 1, 33
IRQ 2, 34
IRQ 3, 35
IRQ 4, 36
IRQ 5, 37
IRQ 6, 38
IRQ 7, 39
IRQ 8, 40
IRQ 9, 41
IRQ 10, 42
IRQ 11, 43
IRQ 12, 44
IRQ 13, 45
IRQ 14, 46
IRQ 15, 47

/* ----- save/restore + dispatch ----------------------------------------- */
/* Push order defines the in-memory frame (rax ends up at the lowest address,
 * matching interrupt_frame64's first field). */
.macro SAVE_REGS
    pushq %r15
    pushq %r14
    pushq %r13
    pushq %r12
    pushq %r11
    pushq %r10
    pushq %r9
    pushq %r8
    pushq %rbp
    pushq %rdi
    pushq %rsi
    pushq %rdx
    pushq %rcx
    pushq %rbx
    pushq %rax
.endm

.macro RESTORE_REGS
    popq %rax
    popq %rbx
    popq %rcx
    popq %rdx
    popq %rsi
    popq %rdi
    popq %rbp
    popq %r8
    popq %r9
    popq %r10
    popq %r11
    popq %r12
    popq %r13
    popq %r14
    popq %r15
.endm

.extern isr_handler
.extern irq_handler

.global isr_common_stub
isr_common_stub:
    SAVE_REGS
    movq %rsp, %rdi         /* arg0 = &interrupt_frame64 */
    movq %rsp, %rbx         /* remember frame base */
    andq $-16, %rsp         /* 16-byte align for the SysV call */
    call isr_handler
    movq %rbx, %rsp         /* restore frame base */
    RESTORE_REGS
    addq $16, %rsp          /* drop int_no + err_code */
    iretq

.global irq_common_stub
irq_common_stub:
    SAVE_REGS
    movq %rsp, %rdi
    movq %rsp, %rbx
    andq $-16, %rsp
    call irq_handler
    movq %rbx, %rsp
    RESTORE_REGS
    addq $16, %rsp
    iretq
