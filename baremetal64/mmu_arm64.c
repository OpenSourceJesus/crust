/* mmu_arm64.c - turn on the AArch64 MMU with an identity mapping.
 *
 * The x86 boot path identity-maps the first 1 GiB with 2 MiB pages inside
 * boot64.S, before long mode is even entered, because x86-64 *requires*
 * paging to be on to be in long mode at all. AArch64 has the opposite
 * arrangement: it runs perfectly well with the MMU off, which is how
 * everything up to this point has worked. The cost of leaving it off is that
 * all memory is Device-nGnRnE -- strongly ordered, uncached -- so every
 * access goes to RAM, and no data cache is available.
 *
 * What the MMU needs before SCTLR_EL1.M can be set:
 *
 *   MAIR_EL1   memory attribute encodings, referenced by index from each
 *              page table entry
 *   TCR_EL1    translation control: address size, granule, cacheability
 *   TTBR0_EL1  the physical base of the level-0 table
 *
 * The mapping built here is an identity map of the gigabyte holding RAM and
 * the gigabyte holding the peripherals, in 2 MiB blocks. A 2 MiB block
 * descriptor at level 2 is the AArch64 equivalent of x86's 2 MiB PDE, and it
 * is used here for the same reason: it keeps the table count small enough to
 * build by hand at boot.
 *
 * Which gigabytes those are is board-specific, and getting it wrong is not
 * survivable -- see the note on RAM_BASE below. This originally hardcoded
 * level-1 entries 0 and 1, which is right for the virt machine and for the
 * Pi, and silently unable to map the Jetson at all: its RAM is at
 * 0x80000000, in gigabyte 2, so the image mapped neither the code it was
 * executing nor the vector table it would have faulted into.
 *
 * Compiled by ShivyCX. Everything that needs a system register lives in
 * mmu_enable_arm64.S, since ShivyCX has no inline assembly.
 */

void uart_puts(char *s);
void uart_puthex(unsigned long v);

/* From the linker script: 16 KiB of 4 KiB-aligned space for page tables. */
extern unsigned long __pgtbl_start;

/* Descriptor bits, level 0/1 table entries and level 2 block entries. */
#define DESC_VALID      0x1
/* A table descriptor is 0b11: valid *and* table. Writing 0b10 here -- the
 * table bit without the valid bit -- produces an entry the walker treats as
 * invalid, so every translation faults the instant the MMU is enabled, with
 * the fault taken at the first fetch after `msr sctlr_el1` and no way to
 * report it. Block descriptors are 0b01: valid, not table. */
#define DESC_TABLE      0x3
#define DESC_BLOCK      0x1
#define DESC_AF         (1UL << 10) /* access flag: a fault if left clear */
#define DESC_SH_INNER   (3UL << 8)  /* inner shareable */
#define DESC_AP_RW      (0UL << 6)  /* read/write at EL1, no EL0 access */

/* Attribute index into MAIR_EL1, shifted into the descriptor's AttrIndx. */
#define ATTR_IDX(n)     (((unsigned long)(n)) << 2)

#define MAIR_IDX_DEVICE 0
#define MAIR_IDX_NORMAL 1

#define BLOCK_2M        0x200000UL
#define ENTRIES         512

/* Where RAM starts, and where the device window sits inside the map.
 *
 * Mapping the UART as Normal cacheable memory would let writes sit in the
 * cache instead of reaching the device, and the console would go silent the
 * moment caching is enabled -- which is exactly the sort of failure that
 * looks like "the MMU broke everything". So the device window is described
 * explicitly rather than assumed to be "everything below RAM".
 *
 * These are per-board and come from the profile in tools/baremetal_arm64.py.
 * The defaults are the virt machine's. They must be defines rather than
 * constants because the values decide *which* level-1 entries get filled in,
 * and an image that guesses wrong does not fault in a diagnosable way -- it
 * faults on the first instruction fetch after SCTLR_EL1.M is set, with the
 * vector table itself unmapped, so there is nothing left that can report it.
 *
 *   board    RAM_BASE     PERIPH_BASE  note
 *   virt     0x40000000   0x00000000   peripherals below RAM, separate GiB
 *   raspi3   0x00000000   0x3F000000   RAM and peripherals share GiB 0
 *   raspi4   0x00000000   0xFE000000   peripherals up at GiB 3
 *   jetson   0x80000000   0x50000000   RAM in GiB 2 -- unreachable before
 *                                      this was parameterised
 */
#ifndef RAM_BASE
#define RAM_BASE        0x40000000UL
#endif

#ifndef PERIPH_BASE
#define PERIPH_BASE     0x00000000UL
#endif

#ifndef PERIPH_SIZE
#define PERIPH_SIZE     0x40000000UL
#endif

/* One level-1 entry covers 1 GiB, so this is the level-1 index of an
 * address, and the base of the gigabyte containing it. */
#define GIB             0x40000000UL
#define L1_INDEX(a)     (((unsigned long)(a)) >> 30)
#define GIB_BASE(a)     (((unsigned long)(a)) & ~(GIB - 1))

/* Filled in by mmu_init; mmu_enable_arm64.S reads them back through these
 * accessors, since it cannot see C globals by name any more cheaply. */
static unsigned long *l1_table;

unsigned long mmu_ttbr0(void)
{
    return (unsigned long)l1_table;
}

/* MAIR_EL1: two attributes, at indices 0 and 1.
 *   index 0 = 0x00 : Device-nGnRnE
 *   index 1 = 0xFF : Normal, inner/outer write-back non-transient
 */
unsigned long mmu_mair(void)
{
    return 0x00FFUL;
}

/* TCR_EL1 for a 39-bit address space with a 4 KiB granule on TTBR0 only.
 *   T0SZ  = 25    -> 2^39 bytes of TTBR0 address space
 *   TG0   = 0     -> 4 KiB granule
 *   SH0   = 3     -> inner shareable
 *   ORGN0 = IRGN0 = 1 -> write-back write-allocate cacheable table walks
 *   IPS   = 2     -> 40-bit physical addresses
 *   EPD1  = 1     -> TTBR1 disabled; nothing is mapped high
 */
unsigned long mmu_tcr(void)
{
    unsigned long t0sz = 25;
    unsigned long irgn0 = 1UL << 8;
    unsigned long orgn0 = 1UL << 10;
    unsigned long sh0 = 3UL << 12;
    unsigned long tg0 = 0UL << 14;
    unsigned long epd1 = 1UL << 23;
    unsigned long ips = 2UL << 32;
    return t0sz | irgn0 | orgn0 | sh0 | tg0 | epd1 | ips;
}

/* Build a two-level table: one level-1 table whose entries point at level-2
 * tables of 2 MiB blocks. With T0SZ=25 the walk starts at level 1, so no
 * level-0 table is needed -- a detail worth stating, because a 48-bit
 * configuration (T0SZ=16) starts at level 0 and the same code would then
 * install a table one level too shallow and fault on the first access.
 */
/* Fill one level-2 table with 512 2 MiB blocks covering the gigabyte at
 * `base`, and hang it off the level-1 entry for that gigabyte.
 *
 * The attribute is chosen per block rather than per gigabyte, because on the
 * Pi they are not separable: RAM starts at 0 and the peripherals sit at
 * 0x3F000000, so both live in gigabyte 0 and a single attribute for the
 * whole gigabyte is wrong whichever one is picked.
 */
static void map_gigabyte(unsigned long *l1, unsigned long *l2,
                         unsigned long base)
{
    unsigned long i;
    unsigned long addr;

    for (i = 0; i < ENTRIES; i = i + 1) {
        addr = base + (i * BLOCK_2M);
        if (addr >= PERIPH_BASE && addr < (PERIPH_BASE + PERIPH_SIZE)) {
            /* Device-nGnRnE: uncached, so stores reach the device. */
            l2[i] = addr | DESC_BLOCK | DESC_AF
                    | ATTR_IDX(MAIR_IDX_DEVICE);
        } else {
            l2[i] = addr | DESC_BLOCK | DESC_AF | DESC_SH_INNER | DESC_AP_RW
                    | ATTR_IDX(MAIR_IDX_NORMAL);
        }
    }
    l1[L1_INDEX(base)] = ((unsigned long)l2) | DESC_TABLE;
}

/* Build a two-level table: one level-1 table whose entries point at level-2
 * tables of 2 MiB blocks.
 *
 * Up to three gigabytes are mapped: the one holding RAM, and the ones at each
 * end of the peripheral window -- which is not always a single gigabyte. The
 * Pi 3 is the awkward case and the reason this is not simply "RAM plus one":
 * its BCM peripherals are at 0x3F000000, in gigabyte 0 alongside RAM, but the
 * ARM *local* peripherals that route the generic timer are at 0x40000000, in
 * gigabyte 1. Miss that second gigabyte and the timer registers translation
 * fault the moment the MMU comes on.
 *
 * Duplicates are dropped, so a board whose windows coincide builds fewer
 * tables. Three level-2 tables is also the ceiling the linker script allows:
 * it reserves 16 KiB, which is exactly four 4 KiB tables, one of which is the
 * level-1 table.
 */
void mmu_init(void)
{
    unsigned long *tables = &__pgtbl_start;
    unsigned long *l1 = tables;
    unsigned long ram_gib;
    unsigned long periph_gib;
    unsigned long periph_end_gib;
    unsigned long used;
    unsigned long i;

    ram_gib = GIB_BASE(RAM_BASE);
    periph_gib = GIB_BASE(PERIPH_BASE);
    periph_end_gib = GIB_BASE(PERIPH_BASE + PERIPH_SIZE - 1);

    for (i = 0; i < ENTRIES; i = i + 1) {
        l1[i] = 0;
    }

    map_gigabyte(l1, tables + ENTRIES, ram_gib);
    used = 1;

    if (periph_gib != ram_gib) {
        used = used + 1;
        map_gigabyte(l1, tables + (ENTRIES * used), periph_gib);
    }

    if (periph_end_gib != ram_gib && periph_end_gib != periph_gib) {
        used = used + 1;
        map_gigabyte(l1, tables + (ENTRIES * used), periph_end_gib);
    }

    l1_table = l1;
}

void mmu_report(void)
{
    uart_puts("  TTBR0 = ");
    uart_puthex(mmu_ttbr0());
    uart_puts("\n  MAIR  = ");
    uart_puthex(mmu_mair());
    uart_puts("\n  TCR   = ");
    uart_puthex(mmu_tcr());
    uart_puts("\n");
}
