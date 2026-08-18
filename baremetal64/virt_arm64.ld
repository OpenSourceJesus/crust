/* virt_arm64.ld - link a ShivyCX app + boot_arm64.S into a bare-metal image
 * for `qemu-system-aarch64 -M virt`.
 *
 * The virt machine puts RAM at 0x40000000. The conventional load address is
 * 64 KiB above that: firmware and the device tree blob occupy the bottom of
 * RAM, so starting at the very base risks having the image overwritten
 * before it ever runs.
 *
 * Unlike kernel64.ld there is no Multiboot header to keep inside the first
 * 8 KiB, so .text.boot leads purely to keep _start at the image's entry
 * address, which is what -kernel jumps to.
 */

ENTRY(_start)

SECTIONS
{
    . = 0x40080000;

    .text : {
        KEEP(*(.text.boot))     /* _start must come first */
        *(.text)
        *(.text.*)
    }

    /* The vector table must be 2 KiB aligned -- VBAR_EL1 ignores the low 11
     * bits, so a misaligned table is not rejected, it is silently truncated
     * to the aligned address below it and every exception vectors into the
     * wrong place. Its own .balign enforces this; the section alignment here
     * keeps the two consistent. */
    .vectors ALIGN(2048) : {
        KEEP(*(.vectors))
    }

    .rodata ALIGN(8) : {
        *(.rodata)
        *(.rodata.*)
    }

    .data ALIGN(8) : {
        *(.data)
        *(.data.*)
    }

    /* Bounds are 8-aligned so boot_arm64.S can clear this with a doubleword
     * loop without a tail case. */
    . = ALIGN(8);
    __bss_start = .;
    .bss : {
        *(COMMON)
        *(.bss)
        *(.bss.*)
        . = ALIGN(8);
    }
    __bss_end = .;

    /* Page tables need 4 KiB alignment (TTBR0_EL1 holds a physical address
     * with the low 12 bits zero), so the MMU stage allocates them here
     * rather than in .bss. */
    . = ALIGN(4096);
    __pgtbl_start = .;
    . = . + 0x4000;             /* 4 tables: L0, L1, and two spare */
    __pgtbl_end = .;

    /* 64 KiB of stack, growing down from __stack_top. */
    . = ALIGN(16);
    __stack_bottom = .;
    . = . + 0x10000;
    __stack_top = .;

    __image_end = .;

    /DISCARD/ : {
        *(.comment)
        *(.note.*)
        *(.eh_frame)
    }
}
