/* switcher_full_arm64.S - the save-everything baseline for the register
 * partition benchmark.
 *
 * Structurally identical to the switcher ShivyCX generates from a thread
 * partition: same TCB layout, same scratch protocol, same timer_ack hook,
 * same cur/next exchange, same eret. Exactly one thing differs -- this one
 * saves and restores **all 31 general-purpose registers** rather than the
 * running thread's footprint. That is what makes the comparison mean
 * something: any difference in throughput is attributable to the save set and
 * nothing else.
 *
 * Why 31 and not 22. The generic exception path in vectors_arm64.S saves
 * twenty-two registers (x0-x19, x29, x30) and would be the obvious baseline,
 * but it is not a *correct* thread switcher: it never saves x20-x28, and
 * ShivyCX puts value homes there. Using it as the baseline would compare a
 * correct switcher against a broken one and flatter the partitioned version by
 * nine registers it was never entitled to skip. A scheduler that preempts at
 * an arbitrary instruction has to save everything it cannot prove dead, and
 * without a whole-program partition it can prove nothing -- so 31 is the
 * honest number for "no partition information".
 *
 * TCB layout, matching the generated switcher:
 *     +0  saved sp    +8  saved ELR_EL1    +16 saved SPSR_EL1
 *     +24.. x0..x30, one slot each
 */

.section .text
.global timer_dispatch_full
/* The vector slot in vectors_preempt_arm64.S branches to `timer_dispatch`.
 * Aliasing it here means the *same* vector table serves both builds, so the
 * comparison is not perturbed by a different entry path -- the only thing
 * that changes between the two images is which object supplies this symbol. */
.global timer_dispatch

timer_dispatch:
timer_dispatch_full:
    /* Same entry protocol as the generated dispatch: park two scratch
     * registers before anything else, because on exception entry every
     * register may belong to the interrupted thread. */
    sub     sp, sp, #16
    stp     x9, x10, [sp]

    adrp    x9, cur_tcb
    add     x9, x9, :lo12:cur_tcb
    ldr     x9, [x9]

    /* Save all 31. x9 and x10 are still on the stack and are recovered
     * below; everything else goes straight out. Paired stores where the
     * neighbours are both live, which is everywhere except around the
     * scratch pair. */
    stp     x0, x1, [x9, #24]
    stp     x2, x3, [x9, #40]
    stp     x4, x5, [x9, #56]
    stp     x6, x7, [x9, #72]
    str     x8, [x9, #88]
    /* x9 at #96 and x10 at #104 come from the stack. */
    stp     x11, x12, [x9, #112]
    stp     x13, x14, [x9, #128]
    stp     x15, x16, [x9, #144]
    stp     x17, x18, [x9, #160]
    stp     x19, x20, [x9, #176]
    stp     x21, x22, [x9, #192]
    stp     x23, x24, [x9, #208]
    stp     x25, x26, [x9, #224]
    stp     x27, x28, [x9, #240]
    stp     x29, x30, [x9, #256]

    ldr     x10, [sp, #0]           /* original x9 */
    str     x10, [x9, #96]
    ldr     x10, [sp, #8]           /* original x10 */
    str     x10, [x9, #104]
    add     sp, sp, #16

    mov     x10, sp
    str     x10, [x9, #0]
    mrs     x10, elr_el1
    str     x10, [x9, #8]
    mrs     x10, spsr_el1
    str     x10, [x9, #16]

    bl      timer_ack

    /* x9 is caller-saved and the call above destroyed it; cur_tcb still names
     * the outgoing thread, so re-derive. (The generated switcher had exactly
     * this bug once -- see SHIVYCX.md.) */
    adrp    x9, cur_tcb
    add     x9, x9, :lo12:cur_tcb
    ldr     x9, [x9]

    /* Exchange cur_tcb and next_tcb, parking the outgoing pointer on the
     * outgoing thread's stack because two scratch registers are one short of
     * a three-way swap. */
    sub     sp, sp, #16
    str     x9, [sp]
    adrp    x9, next_tcb
    add     x9, x9, :lo12:next_tcb
    ldr     x10, [x9]
    str     x10, [sp, #8]
    ldr     x10, [sp]
    str     x10, [x9]
    adrp    x9, cur_tcb
    add     x9, x9, :lo12:cur_tcb
    ldr     x10, [sp, #8]
    str     x10, [x9]
    add     sp, sp, #16

    /* Restore all 31 from the incoming TCB. */
    adrp    x9, cur_tcb
    add     x9, x9, :lo12:cur_tcb
    ldr     x9, [x9]

    ldr     x10, [x9, #16]
    msr     spsr_el1, x10
    ldr     x10, [x9, #8]
    msr     elr_el1, x10
    ldr     x10, [x9, #0]
    mov     sp, x10

    ldp     x0, x1, [x9, #24]
    ldp     x2, x3, [x9, #40]
    ldp     x4, x5, [x9, #56]
    ldp     x6, x7, [x9, #72]
    ldr     x8, [x9, #88]
    ldp     x11, x12, [x9, #112]
    ldp     x13, x14, [x9, #128]
    ldp     x15, x16, [x9, #144]
    ldp     x17, x18, [x9, #160]
    ldp     x19, x20, [x9, #176]
    ldp     x21, x22, [x9, #192]
    ldp     x23, x24, [x9, #208]
    ldp     x25, x26, [x9, #224]
    ldp     x27, x28, [x9, #240]
    ldp     x29, x30, [x9, #256]
    /* x10 then x9 last: after this there is no scratch left, so nothing may
     * follow but the eret. */
    ldr     x10, [x9, #104]
    ldr     x9, [x9, #96]
    eret

/* The scheduler globals the generated switcher would otherwise define. Only
 * one of the two switchers is ever linked, so these do not collide. */
    .section .data
    .balign 8
    .global cur_tcb
cur_tcb:
    .quad 0
    .global next_tcb
next_tcb:
    .quad 0
