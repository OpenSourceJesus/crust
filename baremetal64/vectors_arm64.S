/* vectors_arm64.S - the AArch64 exception vector table.
 *
 * AArch64 has nothing like x86's IDT. There is no table of 256 descriptors
 * indexed by vector number; there is one 2 KiB block of *code*, divided into
 * 16 slots of 128 bytes each, and the CPU branches to the slot itself rather
 * than to an address read out of it. VBAR_EL1 holds the base.
 *
 * The 16 slots are four groups of four:
 *
 *     group          taken from
 *     ------------   ---------------------------------------------
 *     0x000..0x1FF   current EL, using SP_EL0
 *     0x200..0x3FF   current EL, using SP_ELx      <- kernel faults land here
 *     0x400..0x5FF   lower EL, AArch64
 *     0x600..0x7FF   lower EL, AArch32
 *
 * and within each group the four slots are Synchronous, IRQ, FIQ, SError.
 * Since we run at EL1h with SP_EL1 selected, a kernel bug (bad address,
 * undefined instruction) arrives in the *second* group.
 *
 * ---------------------------------------------------------------------------
 * Why each slot holds only four instructions
 *
 * 128 bytes is 32 instructions, and that is a hard ceiling, not a guideline.
 * The first version of this file inlined the whole register save into every
 * slot: 33 instructions. One instruction over. `.balign 128` did not complain
 * -- it simply rounded each entry up to the *next* 128-byte boundary, so the
 * slots ended up 256 bytes apart. Eight of the sixteen then sat outside the
 * table entirely, and the CPU, which computes its target as VBAR + a fixed
 * offset, branched into the middle of the wrong handler. A synchronous EL1h
 * fault (slot 4, offset 0x200) landed on what this file thought was slot 2
 * and was reported as an FIQ.
 *
 * Nothing about that failure points at the size of a macro. So the slots now
 * do the minimum -- claim a frame, save one register pair, note which slot
 * this is, and branch out -- and the real work lives once in exc_common.
 * tools/vectors_size_test.py checks the resulting spacing is exactly 128.
 */

.section .vectors
.global vectors_arm64
.balign 2048

/* Four instructions. x0/x1 are saved here because \kind has to go somewhere
 * and the branch itself needs no register. Everything else is exc_common's
 * job.
 */
.macro EXC_ENTRY kind
    sub     sp, sp, #256
    stp     x0, x1, [sp, #0]
    mov     x0, #\kind
    b       exc_common
.endm

vectors_arm64:

    /* ---- current EL with SP_EL0 ---------------------------------- */
    .balign 128
    EXC_ENTRY 0
    .balign 128
    EXC_ENTRY 1
    .balign 128
    EXC_ENTRY 2
    .balign 128
    EXC_ENTRY 3

    /* ---- current EL with SP_ELx (where kernel faults arrive) ------ */
    .balign 128
    EXC_ENTRY 4
    .balign 128
    EXC_ENTRY 5
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
vectors_end:

/* ---------------------------------------------------------------------------
 * The common path, in ordinary .text -- no size limit applies here.
 *
 * On entry: sp has already been lowered by 256, x0/x1 are saved at [sp, #0],
 * x0 holds the slot number, and every other register still holds whatever the
 * interrupted code left in it. x30 in particular is still the interrupted
 * value, because the slots reach here with `b` and not `bl`.
 */
.section .text
.global exc_common

exc_common:
    stp     x2, x3, [sp, #16]
    stp     x4, x5, [sp, #32]
    stp     x6, x7, [sp, #48]
    stp     x8, x9, [sp, #64]
    stp     x10, x11, [sp, #80]
    stp     x12, x13, [sp, #96]
    stp     x14, x15, [sp, #112]
    stp     x16, x17, [sp, #128]
    stp     x18, x19, [sp, #144]
    stp     x29, x30, [sp, #160]

    /* x0 is already the slot number; add the three registers that describe
     * the fault and call the C handler. */
    mrs     x1, esr_el1
    mrs     x2, far_el1
    mrs     x3, elr_el1
    bl      exc_handler

    /* exc_handler returns 0 to resume at the faulting instruction, or 1 to
     * step over it. Without this the "expected fault" path would re-execute
     * the same faulting instruction forever, since eret returns to ELR_EL1
     * unchanged.
     *
     * Done branchlessly: ELR += 4 * retval. Every AArch64 instruction is
     * exactly four bytes, so "the next instruction" is a fixed offset -- on a
     * variable-length ISA this would need the instruction decoded.
     */
    mrs     x1, elr_el1
    add     x1, x1, x0, lsl #2
    msr     elr_el1, x1

    ldp     x2, x3, [sp, #16]
    ldp     x4, x5, [sp, #32]
    ldp     x6, x7, [sp, #48]
    ldp     x8, x9, [sp, #64]
    ldp     x10, x11, [sp, #80]
    ldp     x12, x13, [sp, #96]
    ldp     x14, x15, [sp, #112]
    ldp     x16, x17, [sp, #128]
    ldp     x18, x19, [sp, #144]
    ldp     x29, x30, [sp, #160]
    ldp     x0, x1, [sp, #0]
    add     sp, sp, #256
    eret
