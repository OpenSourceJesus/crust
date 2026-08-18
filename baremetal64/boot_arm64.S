/* boot_arm64.S - AArch64 bare-metal entry for ShivyCX.
 *
 * This is the AArch64 counterpart to boot64.S. Nothing carries over from it:
 * there is no Multiboot header (that is a PC firmware convention), no
 * protected-to-long-mode transition (AArch64 boots into a 64-bit state
 * already), and no GDT (AArch64 has no segmentation). What replaces all of
 * that is an exception-level drop, which x86 has no analogue of.
 *
 * On `qemu-system-aarch64 -M virt -kernel`, an ELF image is loaded at its
 * link address and entered at EL2 with the MMU and caches off, on every core
 * at once. So the first two jobs are to park the secondary cores and to get
 * down to EL1, where ordinary kernel code runs.
 *
 * The application provides:  void kmain(void);
 *
 * Assembled by our own `rasm` -- every instruction here is in the AArch64
 * subset it encodes, which is why the sequence avoids `ldr xN, =sym` literal
 * pools in favour of adrp/add.
 */

.section .text.boot
.global _start

_start:
    /* ---- park every core but the first -----------------------------
     * -M virt starts all four cores at _start simultaneously. MPIDR_EL1's
     * low bits are the CPU id; anything nonzero goes to sleep. Without this
     * four cores race through the same .bss clear and the same kmain.
     */
    mrs     x0, mpidr_el1
    and     x0, x0, #3
    cbnz    x0, park

    /* ---- descend to EL1 from wherever we were entered ----------------
     * CurrentEL holds the level in bits [3:2]. Which level a bare-metal
     * image is handed varies by board and by firmware, and there is no way
     * to know ahead of time:
     *
     *   qemu -M virt        EL2
     *   qemu -M raspi3b     EL3 or EL2 depending on the qemu version
     *   real Pi firmware    EL2, via armstub
     *   U-Boot (Jetson)     EL2, usually
     *
     * So all three are handled, and EL3 falls through into the EL2 path
     * rather than duplicating it. Assuming EL2 and finding EL3 is not a
     * clean failure: the `eret` would use ELR_EL2 while the CPU returns
     * through ELR_EL3, and the core branches to whatever ELR_EL3 held at
     * reset.
     */
    mrs     x0, currentel
    lsr     x0, x0, #2
    cmp     x0, #1
    b.eq    at_el1
    cmp     x0, #3
    b.ne    at_el2

    /* ---- EL3: configure the secure monitor and drop to EL2 -----------
     * SCR_EL3: NS (bit 0) so lower levels are non-secure, SMD (7) to disable
     * secure monitor calls we have no handler for, HCE (8) so HVC is
     * enabled at EL2, and RW (10) so EL2 is AArch64. Leaving RW clear drops
     * into a 32-bit EL2 that decodes these instructions as garbage.
     */
    mov     x0, #0x581
    msr     scr_el3, x0

    /* Return to EL2h with interrupts masked. */
    mov     x0, #0x3c9
    msr     spsr_el3, x0
    adr     x0, at_el2
    msr     elr_el3, x0
    eret

at_el2:
    /* HCR_EL2.RW (bit 31): EL1 is AArch64, not AArch32. Clearing it by
     * accident lands the eret in a 32-bit state that decodes our
     * instructions as garbage. */
    mrs     x0, hcr_el2
    orr     x0, x0, #0x80000000
    msr     hcr_el2, x0

    /* SPSR_EL2 = 0x3C5: return to EL1h (dedicated SP_EL1), with D/A/I/F all
     * masked. Interrupts stay off until the vector table is installed. */
    mov     x0, #0x3c5
    msr     spsr_el2, x0
    adr     x0, at_el1
    msr     elr_el2, x0
    eret

at_el1:
    /* ---- allow FP/SIMD at EL1 ---------------------------------------
     * CPACR_EL1.FPEN = 0b11. ShivyCX's back end has a floating-point
     * register file and will emit `d`/`s` registers for any FP code, which
     * trap to EL1 as an unhandled exception while FPEN is 0 -- and the trap
     * happens at the *first FP instruction*, arbitrarily far from here.
     */
    mov     x0, #0x300000
    msr     cpacr_el1, x0
    isb

    /* ---- install the exception vectors ------------------------------
     * Before this point any fault is fatal in the worst way: VBAR_EL1 holds
     * whatever it was reset to, so the CPU branches into unmapped memory and
     * takes a second fault doing it. Installing the table early means the
     * rest of boot can report its own failures.
     */
    adrp    x0, vectors_arm64
    add     x0, x0, :lo12:vectors_arm64
    msr     vbar_el1, x0
    isb

    /* ---- stack ------------------------------------------------------ */
    adrp    x0, __stack_top
    add     x0, x0, :lo12:__stack_top
    mov     sp, x0

    /* ---- clear .bss --------------------------------------------------
     * Nothing has zeroed it for us; on a hosted target the kernel's ELF
     * loader does this. Bounds come from the linker script and are aligned
     * to 8 there, so a doubleword loop cannot overrun.
     */
    adrp    x0, __bss_start
    add     x0, x0, :lo12:__bss_start
    adrp    x1, __bss_end
    add     x1, x1, :lo12:__bss_end
bss_loop:
    cmp     x0, x1
    b.hs    bss_done
    str     xzr, [x0]
    add     x0, x0, #8
    b       bss_loop
bss_done:

    /* ---- into C ------------------------------------------------------ */
    bl      kmain

    /* kmain returning is not an error, but there is no OS to return *to*,
     * so fall through into the park loop. */
park:
    wfi
    b       park
