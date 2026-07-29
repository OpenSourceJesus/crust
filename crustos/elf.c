/* CrustOS ELF64 loader -- parse static ELF, map PT_LOAD, read Crust-ELF hints.
 *
 * Hosted: segments are copied into a malloc'd image (or identity-mapped when
 * the preferred VA is free). Guests use the CrustOS syscall ABI via
 * crustos_guest_syscall (resolved at load for ET_DYN / Crust guests), not
 * Linux `syscall`, so exit does not kill the host process.
 *
 * Crust-ELF: a PT_NOTE named "CRUSTOS" with desc[0] = reg_class
 *   0 = minimal callee-saved, 1 = +extras, 2 = full GPR, 3 = +xmm0-7
 * Also accepted: e_flags bits 8..9 as a compact reg_class (0..3).
 *
 * libc: malloc/free/memcpy/memset/printf come from the unit (schemes/runtime);
 * only declare the file I/O helpers that the runtime may not export.
 */
int open(const char *, int, ...);
int close(int);
long read(int, void *, unsigned long);
long lseek(int, long, int);
char *getenv(const char *);
int mprotect(void *, unsigned long, int);

enum {
    CRUST_PROT_READ = 1,
    CRUST_PROT_WRITE = 2,
    CRUST_PROT_EXEC = 4,
    ELF_REG_MIN = 0,
    ELF_REG_EXTRA = 1,
    ELF_REG_FULL = 2,
    ELF_REG_SIMD = 3,
};

enum {
    EI_MAG0 = 0, EI_CLASS = 4, EI_DATA = 5, EI_VERSION = 6,
    ELFCLASS64 = 2, ELFDATA2LSB = 1, EV_CURRENT = 1,
    ET_EXEC = 2, ET_DYN = 3,
    PT_LOAD = 1, PT_NOTE = 4,
    PF_X = 1, PF_W = 2, PF_R = 4,
    O_RDONLY = 0, CRUST_SEEK_SET = 0, CRUST_SEEK_END = 2,
};

struct Elf64_Ehdr {
    unsigned char e_ident[16];
    unsigned short e_type;
    unsigned short e_machine;
    unsigned int e_version;
    unsigned long e_entry;
    unsigned long e_phoff;
    unsigned long e_shoff;
    unsigned int e_flags;
    unsigned short e_ehsize;
    unsigned short e_phentsize;
    unsigned short e_phnum;
    unsigned short e_shentsize;
    unsigned short e_shnum;
    unsigned short e_shstrndx;
};

struct Elf64_Phdr {
    unsigned int p_type;
    unsigned int p_flags;
    unsigned long p_offset;
    unsigned long p_vaddr;
    unsigned long p_paddr;
    unsigned long p_filesz;
    unsigned long p_memsz;
    unsigned long p_align;
};

struct ElfImage {
    unsigned char *base;     /* malloc'd guest image (lowest vaddr mapped to 0) */
    unsigned long size;
    unsigned long load_bias; /* preferred lowest PT_LOAD vaddr */
    unsigned long entry;     /* runtime entry = e_entry - load_bias + (ulong)base for reloc */
    unsigned long entry_va;  /* original e_entry */
    int reg_class;
    int is_dyn;
    int nload;
};

static unsigned long align_up(unsigned long v, unsigned long a) {
    if (a <= 1) return v;
    return (v + a - 1) & ~(a - 1);
}

static int parse_crustos_note(const unsigned char *file, unsigned long fsz,
                              const struct Elf64_Phdr *ph, int *reg_out) {
    /* Note: namesz, descsz, type, name..., desc... (padded to 4). */
    unsigned long off = ph->p_offset;
    unsigned long end = off + ph->p_filesz;
    if (end > fsz) return -1;
    while (off + 12 <= end) {
        unsigned int namesz = *(unsigned int *)(file + off);
        unsigned int descsz = *(unsigned int *)(file + off + 4);
        unsigned int ntype = *(unsigned int *)(file + off + 8);
        unsigned long name_off = off + 12;
        unsigned long desc_off = align_up(name_off + namesz, 4);
        unsigned long next = align_up(desc_off + descsz, 4);
        if (next > end) break;
        if (namesz >= 7 && file[name_off] == 'C' && file[name_off + 1] == 'R'
            && file[name_off + 2] == 'U' && file[name_off + 3] == 'S'
            && file[name_off + 4] == 'T' && file[name_off + 5] == 'O'
            && file[name_off + 6] == 'S' && descsz >= 1) {
            int cls = (int)file[desc_off];
            if (cls < 0) cls = 0;
            if (cls > ELF_REG_SIMD) cls = ELF_REG_SIMD;
            *reg_out = cls;
            (void)ntype;
            return 0;
        }
        off = next;
    }
    return -1;
}

int elf_regs_for_class(int cls) {
    if (cls == ELF_REG_MIN) return 6;
    if (cls == ELF_REG_EXTRA) return 10;
    if (cls == ELF_REG_FULL) return 15;
    return 23; /* full + 8 xmm */
}

int elf_load_path(const char *path, struct ElfImage *out) {
    int fd;
    long fsz;
    unsigned char *file;
    struct Elf64_Ehdr *eh;
    unsigned long lo, hi;
    int i, reg_class;
    long nread;

    memset(out, 0, sizeof(*out));
    out->reg_class = ELF_REG_FULL;

    fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    fsz = lseek(fd, 0, CRUST_SEEK_END);
    if (fsz <= 0) { close(fd); return -1; }
    lseek(fd, 0, CRUST_SEEK_SET);
    file = malloc((unsigned long)fsz);
    if (!file) { close(fd); return -1; }
    nread = read(fd, file, (unsigned long)fsz);
    close(fd);
    if (nread != fsz) { free(file); return -1; }

    eh = (struct Elf64_Ehdr *)file;
    if (eh->e_ident[0] != 0x7f || eh->e_ident[1] != 'E'
        || eh->e_ident[2] != 'L' || eh->e_ident[3] != 'F'
        || eh->e_ident[EI_CLASS] != ELFCLASS64
        || eh->e_ident[EI_DATA] != ELFDATA2LSB
        || (eh->e_type != ET_EXEC && eh->e_type != ET_DYN)
        || eh->e_phoff == 0 || eh->e_phnum == 0) {
        free(file);
        return -2;
    }

    /* e_flags bits 8..9 as compact reg_class when no note. */
    reg_class = (int)((eh->e_flags >> 8) & 3);
    out->is_dyn = (eh->e_type == ET_DYN);

    lo = ~0UL;
    hi = 0;
    for (i = 0; i < (int)eh->e_phnum; i++) {
        struct Elf64_Phdr *ph = (struct Elf64_Phdr *)(file + eh->e_phoff
            + (unsigned long)i * eh->e_phentsize);
        if (ph->p_type == PT_NOTE) {
            int cls = reg_class;
            if (parse_crustos_note(file, (unsigned long)fsz, ph, &cls) == 0)
                reg_class = cls;
        }
        if (ph->p_type != PT_LOAD || ph->p_memsz == 0)
            continue;
        if (ph->p_vaddr < lo) lo = ph->p_vaddr;
        if (ph->p_vaddr + ph->p_memsz > hi)
            hi = ph->p_vaddr + ph->p_memsz;
        out->nload++;
    }
    if (out->nload == 0 || lo >= hi) {
        free(file);
        return -3;
    }

    out->load_bias = lo;
    out->size = hi - lo;
    out->base = malloc(out->size);
    if (!out->base) { free(file); return -4; }
    memset(out->base, 0, out->size);

    for (i = 0; i < (int)eh->e_phnum; i++) {
        struct Elf64_Phdr *ph = (struct Elf64_Phdr *)(file + eh->e_phoff
            + (unsigned long)i * eh->e_phentsize);
        unsigned long dst, copy;
        if (ph->p_type != PT_LOAD || ph->p_memsz == 0)
            continue;
        dst = ph->p_vaddr - lo;
        copy = ph->p_filesz;
        if (copy > ph->p_memsz) copy = ph->p_memsz;
        if (ph->p_offset + copy > (unsigned long)fsz) {
            free(out->base); free(file); out->base = 0; return -5;
        }
        memcpy(out->base + dst, file + ph->p_offset, copy);
        /* BSS already zeroed by memset of whole image. */
        (void)ph->p_flags;
    }

    out->entry_va = eh->e_entry;
    out->entry = (unsigned long)out->base + (eh->e_entry - lo);
    out->reg_class = reg_class;
    free(file);
    return 0;
}

void elf_free(struct ElfImage *img) {
    if (img && img->base) {
        free(img->base);
        img->base = 0;
    }
}

/* Hosted "run": call guest as int(*)(void) for ET_DYN/PIC images after
 * marking the image executable. ET_EXEC linked at a fixed VA is only
 * loaded/described here -- jumping into a relocated copy would fault on
 * absolute addresses. Returns guest return code, or negative on error. */
typedef int (*guest_fn_t)(void);

int elf_run_guest_fn(struct ElfImage *img) {
    guest_fn_t fn;
    unsigned long page, len;
    if (!img || !img->base || img->entry == 0)
        return -100;
    if (!img->is_dyn) {
        /* Static ET_EXEC: validate-only path under hosted CrustOS. */
        return -101;
    }
    page = (unsigned long)img->base & ~0xfffUL;
    len = img->size + ((unsigned long)img->base - page);
    len = (len + 0xfffUL) & ~0xfffUL;
    if (mprotect((void *)page, len, CRUST_PROT_READ | CRUST_PROT_WRITE | CRUST_PROT_EXEC) != 0)
        return -102;
    fn = (guest_fn_t)img->entry;
    return fn();
}

int elf_describe(struct ElfImage *img) {
    if (!img || !img->base) return -1;
    printf("  elf image    : %lu bytes, %d PT_LOAD, dyn=%d\n",
           img->size, img->nload, img->is_dyn);
    printf("  elf entry_va : 0x%lx  runtime=%p\n",
           img->entry_va, (void *)img->entry);
    printf("  elf reg_class: %d (%d regs to save)\n",
           img->reg_class, elf_regs_for_class(img->reg_class));
    return 0;
}
