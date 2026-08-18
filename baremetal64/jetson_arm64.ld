/* jetson_arm64.ld - link a ShivyCX app + boot_arm64.S into a bare-metal image
 * for the NVIDIA Jetson Nano (Tegra X1) and its relatives.
 *
 * The Jetson does not boot an image the way a Pi does. There is no firmware
 * that loads `kernel8.img` at a fixed address; it comes up through cboot into
 * U-Boot, which loads an arm64 `Image` and jumps to it with `booti`. DRAM
 * starts at 0x80000000, and the arm64 Linux convention -- which U-Boot
 * follows -- is to place the image 2 MiB in, at **0x80080000**, leaving room
 * below for the device tree and initrd U-Boot has already written there.
 *
 * So the load address is a U-Boot convention rather than a hardware
 * constraint: `booti <addr>` will happily run an image elsewhere, but
 * 0x80080000 is what the default boot scripts use and what a real
 * `Image`-format kernel expects.
 *
 * Everything else is identical to the other boards. That is the point of
 * having done virt and the Pi first: a third board is a load address, a UART
 * base, and nothing else.
 */
ENTRY(_start)

SECTIONS
{
    . = 0x80080000;

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
