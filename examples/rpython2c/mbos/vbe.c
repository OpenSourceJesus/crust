/* vbe.c -- Bochs-VBE (DISPI) linear-framebuffer graphics for mbos.
 *
 * QEMU's std VGA (PCI 1234:1111, present with `-vga std` or `-device VGA`)
 * exposes the Bochs display interface on I/O ports 0x1CE/0x1CF, letting a
 * kernel set a linear-framebuffer graphics mode with no BIOS/VBE real-mode
 * calls. We find the device, enable its memory BAR (BAR0 = the framebuffer),
 * program a 32-bpp mode, and hand back a pointer for the console to draw into.
 *
 * This is the mbos "graphics card driver" -- deliberately tiny: one mode, one
 * 32-bpp framebuffer, no acceleration. Higher resolutions need more VRAM than
 * the 16 MiB std default; `-device VGA,vgamem_mb=64` (as noted in the README)
 * provides it.
 */
#include "mbos.h"

static inline void outw(u16 p, u16 v){ __asm__ volatile("outw %0,%1"::"a"(v),"Nd"(p)); }
static inline u16  inw(u16 p){ u16 r; __asm__ volatile("inw %1,%0":"=a"(r):"Nd"(p)); return r; }
static inline void outl(u16 p, u32 v){ __asm__ volatile("outl %0,%1"::"a"(v),"Nd"(p)); }
static inline u32  inl(u16 p){ u32 r; __asm__ volatile("inl %1,%0":"=a"(r):"Nd"(p)); return r; }

static u32 pci_r32(u8 b, u8 d, u8 f, u8 o){
    outl(0xCF8, 0x80000000u | ((u32)b<<16) | ((u32)d<<11) | ((u32)f<<8) | (o & 0xFC));
    return inl(0xCFC);
}
static void pci_w32(u8 b, u8 d, u8 f, u8 o, u32 v){
    outl(0xCF8, 0x80000000u | ((u32)b<<16) | ((u32)d<<11) | ((u32)f<<8) | (o & 0xFC));
    outl(0xCFC, v);
}

/* DISPI registers */
#define VBE_IDX 0x1CE
#define VBE_DAT 0x1CF
#define DISPI_ID          0
#define DISPI_XRES        1
#define DISPI_YRES        2
#define DISPI_BPP         3
#define DISPI_ENABLE      4
#define DISPI_BANK        5
#define DISPI_VIRT_WIDTH  6
#define DISPI_VIRT_HEIGHT 7
#define DISPI_X_OFFSET    8
#define DISPI_Y_OFFSET    9
#define DISPI_VRAM_64K    10

#define DISPI_ENABLED   0x01
#define DISPI_GETCAPS   0x02
#define DISPI_LFB       0x40
#define DISPI_NOCLEAR   0x80

static void dispi(u16 index, u16 val){ outw(VBE_IDX, index); outw(VBE_DAT, val); }
static u16  dispi_r(u16 index){ outw(VBE_IDX, index); return inw(VBE_DAT); }

static volatile u32 *g_fb;
static u32 g_w, g_h;
static u32 g_stride;        /* pixels per scanline; NOT always == g_w      */
static u32 g_vram;          /* bytes of video memory the device reports    */
static u32 g_max_w, g_max_h;
static int g_up;

int  gfx_up(void)     { return g_up; }
u32  gfx_width(void)  { return g_w; }
u32  gfx_height(void) { return g_h; }
u32  gfx_stride(void) { return g_stride; }
u32  gfx_vram(void)   { return g_vram; }
u32  gfx_max_width(void)  { return g_max_w; }
u32  gfx_max_height(void) { return g_max_h; }

/* Largest mode this device can actually scan out, given both its maximum
 * geometry and how much video memory it has. A mode that fits the geometry
 * limits but not the memory is the interesting failure: the device accepts it
 * and then displays garbage past the point where VRAM runs out. */
int gfx_mode_fits(u32 w, u32 h) {
    u64 need;
    if (w == 0 || h == 0) return 0;
    if (g_max_w && w > g_max_w) return 0;
    if (g_max_h && h > g_max_h) return 0;
    need = (u64)w * (u64)h * 4u;
    if (g_vram && need > (u64)g_vram) return 0;
    return 1;
}

/* Ask the device for its limits. The DISPI way to do this is to set the
 * GETCAPS bit and read the geometry registers back: while that bit is set they
 * report maxima rather than the current mode. The VRAM register is a count of
 * 64 KiB units and is readable at any time. */
static void query_caps(void) {
    u16 save = dispi_r(DISPI_ENABLE);

    g_vram = (u32)dispi_r(DISPI_VRAM_64K) * 65536u;

    dispi(DISPI_ENABLE, DISPI_GETCAPS);
    g_max_w = dispi_r(DISPI_XRES);
    g_max_h = dispi_r(DISPI_YRES);
    dispi(DISPI_ENABLE, save);
}

/* Bring up a `w`x`h` 32-bpp linear framebuffer. Returns 0 on success. */
int gfx_init(u32 w, u32 h) {
    int bus, dev, found = 0; u8 fb, fd = 0;
    for (bus = 0; bus < 4 && !found; bus++) {
        for (dev = 0; dev < 32 && !found; dev++) {
            u32 id = pci_r32((u8)bus, (u8)dev, 0, 0x00);
            if ((id & 0xFFFF) == 0x1234 && (id >> 16) == 0x1111) {
                fb = (u8)bus; fd = (u8)dev; found = 1;
            }
        }
    }
    if (!found) { ser_puts("[gfx] no Bochs/std VGA device\n"); return -1; }

    /* enable memory space + bus master, read framebuffer BAR0 */
    u32 cmd = pci_r32(fb, fd, 0, 0x04);
    pci_w32(fb, fd, 0, 0x04, cmd | 0x7);
    u32 bar0 = pci_r32(fb, fd, 0, 0x10) & 0xFFFFFFF0u;
    if (!bar0) { ser_puts("[gfx] no framebuffer BAR\n"); return -1; }

    query_caps();

    /* Refuse a mode the device cannot hold rather than setting it and drawing
     * off the end of VRAM. The caller gets a diagnostic naming the limit that
     * was hit; a hi-res build that silently fell back to a smaller mode would
     * be much harder to notice. */
    if (!gfx_mode_fits(w, h)) {
        ser_puts("[gfx] requested mode exceeds the device: ");
        ser_dec(w); ser_puts("x"); ser_dec(h);
        ser_puts(" needs "); ser_dec((w * h * 4) >> 20);
        ser_puts(" MiB, device has "); ser_dec(g_vram >> 20);
        ser_puts(" MiB, max "); ser_dec(g_max_w);
        ser_puts("x"); ser_dec(g_max_h); ser_puts("\n");
        return -1;
    }

    dispi(DISPI_ENABLE, 0);
    dispi(DISPI_XRES, (u16)w);
    dispi(DISPI_YRES, (u16)h);
    dispi(DISPI_BPP, 32);
    dispi(DISPI_VIRT_WIDTH, (u16)w);    /* ask for stride == width */
    dispi(DISPI_X_OFFSET, 0);
    dispi(DISPI_Y_OFFSET, 0);
    dispi(DISPI_ENABLE, DISPI_ENABLED | DISPI_LFB);

    /* Read the geometry back rather than trusting the request: the device is
     * free to clamp. */
    g_w = dispi_r(DISPI_XRES);
    g_h = dispi_r(DISPI_YRES);
    if (g_w == 0 || g_h == 0) { ser_puts("[gfx] mode set failed\n"); return -1; }

    /* The scanline stride is the *virtual* width, which the device may round
     * up for alignment. Assuming stride == width is correct for the common
     * cases and quietly shears the image when it is not, so take what the
     * hardware reports. */
    g_stride = dispi_r(DISPI_VIRT_WIDTH);
    if (g_stride < g_w) g_stride = g_w;

    if (g_w != w || g_h != h) {
        ser_puts("[gfx] device clamped the mode to ");
        ser_dec(g_w); ser_puts("x"); ser_dec(g_h); ser_puts("\n");
    }

    g_fb = (volatile u32 *)(unsigned long)bar0;
    g_up = 1;
    ser_puts("[gfx] framebuffer up ");
    ser_dec(g_w); ser_puts("x"); ser_dec(g_h);
    ser_puts(" stride "); ser_dec(g_stride);
    ser_puts(" vram "); ser_dec(g_vram >> 20); ser_puts("MiB\n");
    return 0;
}

void gfx_pixel(u32 x, u32 y, u32 rgb) {
    if (x < g_w && y < g_h) g_fb[y * g_stride + x] = rgb;
}

void gfx_fill(u32 rgb) {
    u32 x, y;
    if (!g_up) return;
    for (y = 0; y < g_h; y++) {
        volatile u32 *row = g_fb + (u64)y * g_stride;
        for (x = 0; x < g_w; x++) row[x] = rgb;
    }
}

/* Fill an axis-aligned rectangle, clipped to the screen. Row-at-a-time so the
 * stride multiply happens once per scanline instead of once per pixel. */
void gfx_rect(u32 x0, u32 y0, u32 w, u32 h, u32 rgb) {
    u32 x, y;
    if (!g_up) return;
    if (x0 >= g_w || y0 >= g_h) return;
    if (x0 + w > g_w) w = g_w - x0;
    if (y0 + h > g_h) h = g_h - y0;
    for (y = 0; y < h; y++) {
        volatile u32 *row = g_fb + (u64)(y0 + y) * g_stride + x0;
        for (x = 0; x < w; x++) row[x] = rgb;
    }
}

/* Blit one 8x16 glyph (16 row-bytes, MSB=leftmost) at pixel (px,py). */
void gfx_glyph(const u8 *rows, u32 px, u32 py, u32 fg, u32 bg) {
    u32 ry, rx;
    for (ry = 0; ry < 16; ry++) {
        u8 bits = rows[ry];
        for (rx = 0; rx < 8; rx++)
            gfx_pixel(px + rx, py + ry, (bits & (0x80 >> rx)) ? fg : bg);
    }
}

/* Scroll the whole framebuffer up by `dy` pixels, clearing the new bottom. */
/* Scroll the visible area up by `dy` pixels, clearing the new bottom.
 *
 * This reads VRAM, which is the slowest thing the kernel does: reads cross PCI
 * uncached, so at 1920x1080 a one-line console scroll moves ~8 MiB in each
 * direction. Copying only the visible width (not the whole stride) and going
 * row by row keeps it to the minimum a straightforward implementation can do.
 * The real fix is a RAM back buffer, which is also what a game loop wants --
 * see gfx_present().
 */
void gfx_scroll(u32 dy, u32 bg) {
    u32 x, y;
    if (!g_up || dy == 0 || dy >= g_h) return;
    for (y = 0; y + dy < g_h; y++) {
        volatile u32 *dst = g_fb + (u64)y * g_stride;
        volatile u32 *src = g_fb + (u64)(y + dy) * g_stride;
        for (x = 0; x < g_w; x++) dst[x] = src[x];
    }
    for (; y < g_h; y++) {
        volatile u32 *dst = g_fb + (u64)y * g_stride;
        for (x = 0; x < g_w; x++) dst[x] = bg;
    }
}

/* Blit a caller-owned 32-bpp buffer of exactly gfx_width() x gfx_height()
 * pixels to the screen. This is the path a game draws through: build the frame
 * in RAM, hand it over once. Writes to VRAM are write-combined and fast; it is
 * reads that are not, and this does none. */
void gfx_present(const u32 *src) {
    u32 x, y;
    if (!g_up || !src) return;
    for (y = 0; y < g_h; y++) {
        volatile u32 *dst = g_fb + (u64)y * g_stride;
        const u32 *s = src + (u64)y * g_w;
        for (x = 0; x < g_w; x++) dst[x] = s[x];
    }
}
