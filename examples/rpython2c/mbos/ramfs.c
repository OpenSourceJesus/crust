/* ramfs.c -- the mbos ramdisk.
 *
 * The bootloader (QEMU's -kernel loader, via -initrd) places a tar archive in
 * memory and describes it in the Multiboot info structure. This file finds it,
 * walks it, and builds a table of the files inside.
 *
 * It owns the two things tarfs.rs cannot: pointer arithmetic over physical
 * memory, and the Multiboot structure layout. Every decision about what a tar
 * header *means* is asked of tarfs.rs, which rustc checks.
 *
 * Files are not copied. The table points into the module image where the
 * bootloader left it, so a 1 MiB ramdisk costs a few hundred bytes of heap
 * rather than a second megabyte. That is safe because nothing ever writes to
 * the ramdisk -- it is read-only by construction, which is also why there is
 * no write path here to get wrong.
 *
 * Multiboot1 info structure, the parts used:
 *
 *   offset  field
 *        0  flags        (bit 3 set => mods_count / mods_addr are valid)
 *       20  mods_count
 *       24  mods_addr    -> array of { mod_start, mod_end, string, reserved }
 */
#include "mbos.h"
#include "tarfs.h"          /* generated from tarfs.rs; see gen_rs.py */

#define MB_FLAG_MODS  (1u << 3)
#define TAR_BLOCK     512

struct mb_module {
    u32 mod_start;
    u32 mod_end;
    u32 string;
    u32 reserved;
};

struct ramfs_file {
    const char *name;
    const u8   *data;
    u32         size;
};

#ifndef RAMFS_MAX_FILES
#define RAMFS_MAX_FILES 64
#endif

static struct ramfs_file g_files[RAMFS_MAX_FILES];
static int  g_count = 0;
static int  g_ready = 0;

/* Names are copied out of the header block, because the block is a scratch
 * buffer that the next iteration overwrites. The data is not copied. */
#define NAME_POOL_BYTES 4096
static char g_names[NAME_POOL_BYTES];
static int  g_name_used = 0;

static TarHeader g_hdr;     /* one scratch header, reused for each entry */

static const u8 *g_base = 0;
static u32       g_bytes = 0;

/* ---- Multiboot ---------------------------------------------------------- */

/* Locate the first Multiboot module. Returns 0 when there is no ramdisk,
 * which is not an error -- mbos boots fine without one. */
static int find_module(void *mbi, const u8 **out_base, u32 *out_size) {
    u32 flags, count, addr;
    struct mb_module *mods;

    if (!mbi) return 0;

    flags = *(volatile u32 *)mbi;
    if (!(flags & MB_FLAG_MODS)) return 0;

    count = *(volatile u32 *)((u8 *)mbi + 20);
    addr  = *(volatile u32 *)((u8 *)mbi + 24);
    if (count == 0 || addr == 0) return 0;

    mods = (struct mb_module *)(u64)addr;
    if (mods[0].mod_end <= mods[0].mod_start) return 0;

    *out_base = (const u8 *)(u64)mods[0].mod_start;
    *out_size = mods[0].mod_end - mods[0].mod_start;
    return 1;
}

/* ---- tar walk ----------------------------------------------------------- */

static char *name_dup(int len) {
    char *dst;
    int i;
    if (len < 0 || g_name_used + len + 1 > NAME_POOL_BYTES) return 0;
    dst = &g_names[g_name_used];
    for (i = 0; i < len; i++) dst[i] = (char)TarHeader_name_byte(&g_hdr, i);
    dst[len] = 0;
    g_name_used += len + 1;
    return dst;
}

/* Load 512 bytes at `off` into the scratch header. Bounds are checked here
 * because this is the one place a bad offset turns into a wild read. */
static int load_header(u32 off) {
    int i;
    if ((u64)off + TAR_BLOCK > (u64)g_bytes) return 0;
    for (i = 0; i < TAR_BLOCK; i++) {
        if (!TarHeader_set_byte(&g_hdr, i, g_base[off + i])) return 0;
    }
    return 1;
}

int ramfs_init(void *mbi) {
    u32 off = 0;

    g_count = 0;
    g_name_used = 0;
    g_ready = 0;

    if (!find_module(mbi, &g_base, &g_bytes)) {
        ser_puts("[mbos] ramfs: no boot module\n");
        return 0;
    }

    while (off + TAR_BLOCK <= g_bytes) {
        int size, step, namelen;

        if (!load_header(off)) break;
        if (TarHeader_is_end(&g_hdr)) break;

        /* Both checks matter. The magic says this looks like a ustar header;
         * the checksum says it actually is one. Without the second, a walk
         * that lost alignment would read file data as a header. */
        if (!TarHeader_is_ustar(&g_hdr)) {
            ser_puts("[mbos] ramfs: not a ustar header, stopping\n");
            break;
        }
        if (!TarHeader_checksum_ok(&g_hdr)) {
            ser_puts("[mbos] ramfs: header checksum mismatch, stopping\n");
            break;
        }

        size = TarHeader_size(&g_hdr);
        step = TarHeader_next_offset(&g_hdr, size);
        if (size < 0 || step < 0) {
            ser_puts("[mbos] ramfs: malformed size field, stopping\n");
            break;
        }
        if ((u64)off + (u64)step > (u64)g_bytes) {
            ser_puts("[mbos] ramfs: entry runs past the module, stopping\n");
            break;
        }

        if (TarHeader_is_regular(&g_hdr) && g_count < RAMFS_MAX_FILES) {
            namelen = TarHeader_build_name(&g_hdr);
            if (namelen > 0) {
                char *nm = name_dup(namelen);
                if (nm) {
                    g_files[g_count].name = nm;
                    g_files[g_count].data = g_base + off + TAR_BLOCK;
                    g_files[g_count].size = (u32)size;
                    g_count++;
                }
            }
        }

        off += (u32)step;
    }

    g_ready = 1;
    ser_puts("[mbos] ramfs: ready\n");
    return g_count;
}

/* ---- lookup ------------------------------------------------------------- */

int ramfs_count(void) { return g_ready ? g_count : 0; }

const char *ramfs_name(int i) {
    if (i < 0 || i >= g_count) return 0;
    return g_files[i].name;
}

u32 ramfs_size(int i) {
    if (i < 0 || i >= g_count) return 0;
    return g_files[i].size;
}

/* Tar stores "dir/file" while a user types "file", and both should find it. */
static int name_matches(const char *entry, const char *want) {
    const char *base = entry;
    const char *p;
    if (mini_strcmp(entry, want) == 0) return 1;
    for (p = entry; *p; p++) {
        if (*p == '/') base = p + 1;
    }
    return mini_strcmp(base, want) == 0;
}

int ramfs_find(const char *name) {
    int i;
    if (!g_ready || !name) return -1;
    for (i = 0; i < g_count; i++) {
        if (name_matches(g_files[i].name, name)) return i;
    }
    return -1;
}

const u8 *ramfs_data(int i, u32 *size) {
    if (i < 0 || i >= g_count) return 0;
    if (size) *size = g_files[i].size;
    return g_files[i].data;
}

u32 ramfs_bytes(void) { return g_bytes; }
