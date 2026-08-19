/* vectors_preempt_arm64.S - vector table for the register-partitioned
 * preemptive scheduler.
 *
 * Identical to vectors_arm64.S except for one slot. The EL1h IRQ entry
 * (VBAR_EL1 + 0x280) branches to `timer_dispatch` -- the switcher ShivyCX
 * generated from the thread partition -- instead of the generic exc_common
 * path.
 *
 * That is the whole point of the exercise. exc_common saves a fixed twenty-two
 * registers on every tick because it cannot know what the interrupted code was
 * using. The generated entry saves only the running thread's footprint,
 * because the compiler computed it from the call graph.
 *
 * Every other slot still goes to exc_common: a data abort or an undefined
 * instruction is a fault to report, not a thread switch, and the specialized
 * path would be wrong for it.
 *
 * The 128-byte slot limit still applies, and the branch is one instruction, so
 * there is plenty of room. tools/vectors_size_test.py checks the geometry of
 * whichever table is linked.
 */

.section .vectors
.global vectors_preempt_arm64
.balign 2048

/* Generic slot: claim a frame, save one pair, note the slot number, and hand
 * off. Four instructions -- see vectors_arm64.S for why they must stay
 * within 128 bytes. */
.macro EXC_ENTRY kind
    sub     sp, sp, #256
    stp     x0, x1, [sp, #0]
    mov     x0, #\kind
    b       exc_common
.endm

vectors_preempt_arm64:

    /* ---- current EL with SP_EL0 ---------------------------------- */
    .balign 128
    EXC_ENTRY 0
    .balign 128
    EXC_ENTRY 1
    .balign 128
    EXC_ENTRY 2
    .balign 128
    EXC_ENTRY 3

    /* ---- current EL with SP_ELx: where a kernel-mode tick arrives -- */
    .balign 128
    EXC_ENTRY 4                     /* synchronous: still a fault */
    .balign 128
    /* IRQ. No frame is claimed and no register is saved here:
     * timer_dispatch does that itself, and doing it twice would save the
     * wrong values. */
    b       timer_dispatch
    .balign 128
    EXC_ENTRY 6
    .balign 128
    EXC_ENTRY 7

    /* ---- lower EL, AArch64 --------------------------------------- */
    .balign 128
    EXC_ENTRY 8
    .balign 128
    EXC_ENTRY 9
    .balign 128
    EXC_ENTRY 10
    .balign 128
    EXC_ENTRY 11

    /* ---- lower EL, AArch32 --------------------------------------- */
    .balign 128
    EXC_ENTRY 12
    .balign 128
    EXC_ENTRY 13
    .balign 128
    EXC_ENTRY 14
    .balign 128
    EXC_ENTRY 15

    .balign 128
vectors_preempt_end:

/* Install this table. Called from C once the TCBs are set up; VBAR_EL1
 * ignores the low 11 bits, so the .balign 2048 above is load-bearing. */
.section .text
.global install_preempt_vectors
install_preempt_vectors:
    adrp    x0, vectors_preempt_arm64
    add     x0, x0, :lo12:vectors_preempt_arm64
    msr     vbar_el1, x0
    isb
    ret

/* Start a thread by eret-ing into it, rather than returning normally. Used to
 * enter the first thread from kmain: the scheduler is symmetric after that,
 * every switch being an eret from the timer ISR.
 *
 *   void thread_launch(void *tcb);
 *
 * Reads sp, ELR and SPSR from the TCB exactly as the ISR's restore path does,
 * so the first entry and every later resume take the same route.
 */
.global thread_launch
thread_launch:
    ldr     x9, [x0, #16]
    msr     spsr_el1, x9
    ldr     x9, [x0, #8]
    msr     elr_el1, x9
    ldr     x9, [x0, #0]
    mov     sp, x9
    eret
