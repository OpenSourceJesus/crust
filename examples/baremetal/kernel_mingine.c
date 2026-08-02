/* kernel_mingine.c -- the three-language game engine, on the metal, compiled
 * by ShivyCX.
 *
 * This is the self-hosting path exercised end to end on something that is not
 * a toy: ShivyCX takes one translation unit containing C, Rust (mingine.rs,
 * spliced by shivyc/crust.py) and rpython (mingine.py, lowered by py2c), and
 * emits a bootable 64-bit image. No gcc anywhere in the chain that produces
 * this kernel's own code.
 *
 * What it does: brings up a Bochs-VBE linear framebuffer over PCI, renders the
 * shared scene from ../crust/baremetalgames/scene.c into a RAM buffer, prints
 * its checksum to serial, and blits it to the screen scaled to fit.
 *
 * The checksum is the point. scene_host.c runs the *same* scene.c hosted and
 * prints the same four lines. If they match, the identical Rust + rpython + C
 * source produced bit-identical pixels in a process and on bare metal --
 * which is a much stronger statement than "it booted and something appeared".
 *
 * The framebuffer bring-up is a deliberate near-copy of
 * examples/rpython2c/mbos/vbe.c rather than a shared file: mbos is built by
 * gcc and this is built by ShivyCX, and keeping them separate means this file
 * also serves as a check that ShivyCX handles the PCI config cycles and port
 * I/O that a real driver needs.
 */

#include "../crust/baremetalgames/scene.c"

/* ---------------------------------------------------------------- port I/O */

static void outb_p(unsigned short port, unsigned char val) {
    __asm__ volatile ("outb %0, %1" : : "a"(val), "Nd"(port));
}
static unsigned char inb_p(unsigned short port) {
    unsigned char r;
    __asm__ volatile ("inb %1, %0" : "=a"(r) : "Nd"(port));
    return r;
}

static void outw_p(unsigned short port, unsigned short val) {
    __asm__ volatile ("outw %0, %1" : : "a"(val), "Nd"(port));
}
static unsigned short inw_p(unsigned short port) {
    unsigned short r;
    __asm__ volatile ("inw %1, %0" : "=a"(r) : "Nd"(port));
    return r;
}
static void outl_p(unsigned short port, unsigned int val) {
    __asm__ volatile ("outl %0, %1" : : "a"(val), "Nd"(port));
}
static unsigned int inl_p(unsigned short port) {
    unsigned int r;
    __asm__ volatile ("inl %1, %0" : "=a"(r) : "Nd"(port));
    return r;
}

/* ---------------------------------------------------------------- serial --
 *
 * Own COM1 routines rather than minikraft's console. Two reasons: this image
 * then depends on no OS piece at all -- every instruction in it came from
 * ShivyCX compiling this file -- and minikraft's console compiles to nothing
 * unless ENABLE_LOGGING is defined, which is a trap worth stepping around
 * rather than into.
 */
#define COM1 0x3F8

static void ser_init(void) {
    outb_p(COM1 + 1, 0x00);     /* interrupts off        */
    outb_p(COM1 + 3, 0x80);     /* DLAB on               */
    outb_p(COM1 + 0, 0x03);     /* divisor 3 => 38400    */
    outb_p(COM1 + 1, 0x00);
    outb_p(COM1 + 3, 0x03);     /* 8N1, DLAB off         */
    outb_p(COM1 + 2, 0xC7);     /* FIFO on, clear        */
    outb_p(COM1 + 4, 0x0B);     /* RTS/DSR set           */
}

static void ser_putc(char c) {
    int spin;
    spin = 100000;
    while (spin > 0) {
        if ((inb_p(COM1 + 5) & 0x20) != 0) {
            spin = 0;
        } else {
            spin = spin - 1;
        }
    }
    outb_p(COM1, (unsigned char)c);
}

static void ser_puts(const char *s) {
    while (*s != 0) {
        if (*s == 10) {
            ser_putc(13);
        }
        ser_putc(*s);
        s = s + 1;
    }
}

/* ------------------------------------------------------------------- PCI -- */

static unsigned int pci_read(int bus, int dev, int off) {
    unsigned int addr;
    addr = 0x80000000u;
    addr = addr | ((unsigned int)bus << 16);
    addr = addr | ((unsigned int)dev << 11);
    addr = addr | ((unsigned int)(off & 0xFC));
    outl_p(0xCF8, addr);
    return inl_p(0xCFC);
}

static void pci_write(int bus, int dev, int off, unsigned int val) {
    unsigned int addr;
    addr = 0x80000000u;
    addr = addr | ((unsigned int)bus << 16);
    addr = addr | ((unsigned int)dev << 11);
    addr = addr | ((unsigned int)(off & 0xFC));
    outl_p(0xCF8, addr);
    outl_p(0xCFC, val);
}

/* ----------------------------------------------------------------- DISPI -- */

#define VBE_IDX 0x1CE
#define VBE_DAT 0x1CF

static void dispi_w(unsigned short index, unsigned short val) {
    outw_p(VBE_IDX, index);
    outw_p(VBE_DAT, val);
}
static unsigned short dispi_r(unsigned short index) {
    outw_p(VBE_IDX, index);
    return inw_p(VBE_DAT);
}

static unsigned int *fb_base;
static int fb_w;
static int fb_h;
static int fb_stride;

/* Bring up a 32-bpp linear framebuffer. Returns 1 on success, 0 otherwise --
 * a kernel with no display still has serial, which is all the test needs. */
static int fb_init(int want_w, int want_h) {
    int bus;
    int dev;
    int found;
    int f_bus;
    int f_dev;
    unsigned int id;
    unsigned int cmd;
    unsigned int bar0;
    unsigned int vram;

    found = 0;
    f_bus = 0;
    f_dev = 0;
    for (bus = 0; bus < 4; bus++) {
        for (dev = 0; dev < 32; dev++) {
            if (found == 0) {
                id = pci_read(bus, dev, 0x00);
                if ((id & 0xFFFF) == 0x1234) {
                    if ((id >> 16) == 0x1111) {
                        f_bus = bus;
                        f_dev = dev;
                        found = 1;
                    }
                }
            }
        }
    }
    if (found == 0) {
        ser_puts("[mingine] no Bochs VGA device\n");
        return 0;
    }

    cmd = pci_read(f_bus, f_dev, 0x04);
    pci_write(f_bus, f_dev, 0x04, cmd | 0x7);
    bar0 = pci_read(f_bus, f_dev, 0x10) & 0xFFFFFFF0u;
    if (bar0 == 0) {
        ser_puts("[mingine] no framebuffer BAR\n");
        return 0;
    }

    /* Video memory is reported in 64 KiB units. Refuse a mode that does not
     * fit rather than scanning out past the end of it -- the same check mbos's
     * driver makes, for the same reason. */
    vram = (unsigned int)dispi_r(10) * 65536u;
    if ((unsigned int)(want_w * want_h * 4) > vram) {
        ser_puts("[mingine] mode does not fit in video memory\n");
        return 0;
    }

    dispi_w(4, 0);              /* disable while reprogramming */
    dispi_w(1, (unsigned short)want_w);
    dispi_w(2, (unsigned short)want_h);
    dispi_w(3, 32);
    dispi_w(6, (unsigned short)want_w);   /* virtual width == width */
    dispi_w(8, 0);
    dispi_w(9, 0);
    dispi_w(4, 0x41);           /* enabled | LFB */

    fb_w = (int)dispi_r(1);
    fb_h = (int)dispi_r(2);
    fb_stride = (int)dispi_r(6);
    if (fb_stride < fb_w) {
        fb_stride = fb_w;
    }
    if (fb_w == 0 || fb_h == 0) {
        ser_puts("[mingine] mode set failed\n");
        return 0;
    }

    fb_base = (unsigned int *)(unsigned long)bar0;
    return 1;
}

/* ------------------------------------------------------------------ blit -- */

/* Nearest-neighbour upscale of the scene onto the screen, centred. Integer
 * scale only, so the pixels stay square and the picture stays recognisably
 * the same image the hosted run produced. */
static void fb_blit_scaled(void) {
    int scale;
    int sx;
    int sy;
    int ox;
    int oy;
    int x;
    int y;
    int i;
    int j;
    unsigned int c;
    unsigned int *src;

    src = scene_buffer();

    scale = fb_w / scene_width();
    sy = fb_h / scene_height();
    if (sy < scale) {
        scale = sy;
    }
    if (scale < 1) {
        scale = 1;
    }

    ox = (fb_w - scene_width() * scale) / 2;
    oy = (fb_h - scene_height() * scale) / 2;

    for (y = 0; y < scene_height(); y++) {
        for (x = 0; x < scene_width(); x++) {
            c = src[y * scene_width() + x];
            for (j = 0; j < scale; j++) {
                sy = oy + y * scale + j;
                for (i = 0; i < scale; i++) {
                    sx = ox + x * scale + i;
                    if (sx >= 0 && sx < fb_w && sy >= 0 && sy < fb_h) {
                        fb_base[sy * fb_stride + sx] = c;
                    }
                }
            }
        }
    }
}

/* ----------------------------------------------------------------- report - */

/* No printf here, and the summary has to be diffable against the hosted run's
 * stdout, so the two number formats it needs are written out by hand. */
static void put_dec(int v) {
    char buf[16];
    int i;
    int neg;

    neg = 0;
    if (v < 0) {
        neg = 1;
        v = -v;
    }
    i = 15;
    buf[i] = 0;
    if (v == 0) {
        i = i - 1;
        buf[i] = '0';
    }
    while (v > 0) {
        i = i - 1;
        buf[i] = (char)('0' + (v % 10));
        v = v / 10;
    }
    if (neg != 0) {
        i = i - 1;
        buf[i] = '-';
    }
    ser_puts(&buf[i]);
}

static void put_hex8(unsigned int v) {
    char buf[9];
    int i;
    int d;

    buf[8] = 0;
    for (i = 7; i >= 0; i--) {
        d = (int)(v & 0xF);
        if (d < 10) {
            buf[i] = (char)('0' + d);
        } else {
            buf[i] = (char)('a' + (d - 10));
        }
        v = v >> 4;
    }
    ser_puts(buf);
}

/* -------------------------------------------------------------------- main */

void kmain(unsigned int magic, void *mbi) {
    unsigned int sum;
    int have_fb;

    (void)magic;
    (void)mbi;

    ser_init();
    ser_puts("[mingine] booted, compiled by ShivyCX\n");

    /* Render first, display second: the checksum must not depend on whether a
     * display was found, or a headless run and a windowed run would disagree. */
    sum = scene_render();

    ser_puts("scene ");
    put_dec(scene_width());
    ser_puts("x");
    put_dec(scene_height());
    ser_puts(" frames 24\n");

    ser_puts("ball ");
    put_dec(scene_ball_x());
    ser_puts(",");
    put_dec(scene_ball_y());
    ser_puts("\n");

    ser_puts("foe ");
    put_dec(scene_foe_x());
    ser_puts(",");
    put_dec(scene_foe_y());
    ser_puts("\n");

    ser_puts("score ");
    put_dec(scene_score());
    ser_puts("\n");

    ser_puts("pixels ");
    put_hex8(sum);
    ser_puts("\n");

    have_fb = fb_init(1024, 768);
    if (have_fb != 0) {
        fb_blit_scaled();
        ser_puts("[mingine] blitted to framebuffer\n");
    }

    ser_puts("[mingine] done.\n");
    for (;;) {
    }
}
