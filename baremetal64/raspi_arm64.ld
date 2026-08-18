/* raspi_arm64.ld - link a ShivyCX app + boot_arm64.S into a bare-metal image
 * for a 64-bit Raspberry Pi (and `qemu-system-aarch64 -M raspi3b`).
 *
 * The only structural difference from virt_arm64.ld is the load address, and
 * it is not arbitrary: the Pi's firmware loads `kernel8.img` at **0x80000**
 * and jumps to its first byte. (The 32-bit kernels load at 0x8000; the extra
 * digit is the 64-bit convention and mixing them up produces an image that
 * simply never runs.) Below that sits the ATAGS/device-tree area the firmware
 * has already written, so starting lower would mean overwriting the very
 * information a real kernel would want to read.
 *
 * Everything else -- the 2 KiB-aligned vector section, the 8-aligned .bss
 * bounds the boot stub clears, the page-table and stack reservations -- is the
 * same, which is the point: the spine is board-independent and only the
 * addresses move.
 */

ENTRY(_start)

SECTIONS
{
    . = 0x80000;

    .text : {
        KEEP(*(.text.boot))     /* _start must be the first byte of the image */
        *(.text)
        *(.text.*)
    }

    /* VBAR_EL1 ignores the low 11 bits, so a misaligned table is not
     * rejected -- it is silently truncated to the aligned address below it
     * and every exception vectors into the wrong place. */
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

    . = ALIGN(8);
    __bss_start = .;
    .bss : {
        *(COMMON)
        *(.bss)
        *(.bss.*)
        . = ALIGN(8);
    }
    __bss_end = .;

    /* TTBR0_EL1 holds a physical address with the low 12 bits zero, so the
     * page tables need 4 KiB alignment. */
    . = ALIGN(4096);
    __pgtbl_start = .;
    . = . + 0x4000;
    __pgtbl_end = .;

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
