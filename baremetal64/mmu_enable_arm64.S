/* mmu_enable_arm64.S - the system-register half of turning the MMU on.
 *
 * ShivyCX has no inline assembly, so everything that touches a system
 * register lives here. mmu_arm64.c builds the tables and computes the three
 * register values; this reads them back and commits them.
 *
 * The ordering is the whole point and is not negotiable:
 *
 *   1. MAIR, TCR, TTBR0 must all be in place *before* translation is
 *      enabled -- the first translated fetch uses them.
 *   2. `isb` after each group, because system-register writes are not
 *      guaranteed visible to subsequent instructions without one. The CPU is
 *      free to have already fetched and decoded past the msr.
 *   3. The TLB is invalidated first. It is not empty at reset in any
 *      architecturally guaranteed way, and a stale entry that happens to
 *      match will be used in preference to a walk.
 *   4. `dsb` before enabling, so the page table *stores* from mmu_init have
 *      actually reached memory. They were written with the MMU off, hence
 *      uncached, but the barrier is what orders them against the table walk
 *      that is about to read them.
 *
 * Getting any of this out of order does not produce a clean failure; it
 * produces a machine that runs correctly until the first access that misses
 * the TLB.
 */

.section .text
.global mmu_enable

mmu_enable:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp

    /* Invalidate the whole EL1 TLB and wait for it. */
    dsb     ishst
    tlbi    vmalle1
    dsb     ish
    isb

    /* MAIR_EL1 */
    bl      mmu_mair
    msr     mair_el1, x0

    /* TCR_EL1 */
    bl      mmu_tcr
    msr     tcr_el1, x0

    /* TTBR0_EL1 */
    bl      mmu_ttbr0
    msr     ttbr0_el1, x0
    isb

    /* Make the page tables visible to the table walker. */
    dsb     ish
    isb

    /* SCTLR_EL1: set M (bit 0, MMU), C (bit 2, data cache) and I (bit 12,
     * instruction cache). Enabling the caches is the point of the exercise --
     * with the MMU off everything was forced to Device memory, so this is
     * where the machine actually gets fast. */
    mrs     x0, sctlr_el1
    orr     x0, x0, #0x1
    orr     x0, x0, #0x4
    orr     x0, x0, #0x1000
    msr     sctlr_el1, x0
    isb

    ldp     x29, x30, [sp], #32
    ret

/* Read SCTLR_EL1 back, so a test can prove the MMU and caches are actually
 * on rather than infer it from the machine not having crashed. */
.global read_sctlr
read_sctlr:
    mrs     x0, sctlr_el1
    ret

/* unsigned long current_el(void) -- which exception level we ended up at.
 * Useful on a board whose firmware hand-off level is not documented. */
.global current_el
current_el:
    mrs     x0, currentel
    lsr     x0, x0, #2
    ret
