/* shell.c -- the mbos shell.
 *
 * Two pieces that stay separate on purpose:
 *
 *   the line editor  -- owns a buffer, a cursor, and a history ring, and turns
 *                       keystrokes into a finished line. It knows nothing about
 *                       what any command means.
 *   the dispatcher   -- splits a finished line into argv and looks it up in a
 *                       table of {name, handler, help}.
 *
 * The split matters for what comes later. When step 5 lands minipy, running a
 * script is one more row in CMDS, and reading a line for a REPL is one more
 * caller of shell_readline() -- neither needs the other to change.
 *
 * There is no allocator yet, so everything here is a fixed-size static: the
 * line buffer, the argv array, the history ring. Step 3 replaces the sizing
 * decisions with real allocation.
 */
#include "mbos.h"
#include "editbuf.h"      /* generated from editbuf.rs; see gen_rs.py */

/* The prompt names the toolchain the system is built with, not the kernel
 * image, because that is the thing being bootstrapped here. */
#define SHELL_PROMPT "crust> "

#define LINE_MAX  256          /* must match LINE_MAX in editbuf.rs */
#define ARGV_MAX  16

/* ---- output helpers ---------------------------------------------------
 * No printf on bare metal, and no allocator to build one on top of, so the
 * shell gets the two conversions it actually needs.
 */
static void put_u64(u64 v) {
    char buf[24];
    int i = 23;
    buf[i] = 0;
    if (v == 0) buf[--i] = '0';
    while (v > 0) { buf[--i] = (char)('0' + (v % 10)); v /= 10; }
    con_puts(&buf[i]);
}

static void put_hex64(u64 v) {
    static const char DIG[] = "0123456789abcdef";
    char buf[19];
    int i;
    buf[0] = '0'; buf[1] = 'x';
    for (i = 0; i < 16; i++) buf[2 + i] = DIG[(v >> ((15 - i) * 4)) & 0xF];
    buf[18] = 0;
    con_puts(buf);
}

/* ---- command table ----------------------------------------------------- */

typedef int (*cmd_fn)(int argc, char **argv);

struct command {
    const char *name;
    cmd_fn      fn;
    const char *help;
};

static const struct command CMDS[];      /* defined after the handlers */

static int cmd_help(int argc, char **argv) {
    int i;
    (void)argc; (void)argv;
    for (i = 0; CMDS[i].name; i++) {
        int pad = 10 - (int)mini_strlen(CMDS[i].name);
        con_puts("  ");
        con_puts(CMDS[i].name);
        while (pad-- > 0) con_putc(' ');
        con_puts(CMDS[i].help);
        con_putc('\n');
    }
    return 0;
}

static int cmd_echo(int argc, char **argv) {
    int i;
    for (i = 1; i < argc; i++) {
        if (i > 1) con_putc(' ');
        con_puts(argv[i]);
    }
    con_putc('\n');
    return 0;
}

static int cmd_clear(int argc, char **argv) {
    (void)argc; (void)argv;
    con_clear(VGA_ATTR(VGA_LGREY, VGA_BLACK));
    return 0;
}

static int cmd_ticks(int argc, char **argv) {
    (void)argc; (void)argv;
    put_u64(irq_ticks());
    con_putc('\n');
    return 0;
}

/* The tick counter is the only clock we have; at a known 100 Hz it is also a
 * uptime counter. Printed as s.cc rather than pulling in any float support. */
static int cmd_uptime(int argc, char **argv) {
    u64 t = irq_ticks();
    (void)argc; (void)argv;
    put_u64(t / 100);
    con_putc('.');
    con_putc((char)('0' + (t / 10) % 10));
    con_putc((char)('0' + t % 10));
    con_puts("s\n");
    return 0;
}

static int cmd_ver(int argc, char **argv) {
    (void)argc; (void)argv;
    con_puts("mbos -- freestanding x86-64, no libc, no host OS\n");
    con_puts("console ");
    put_u64((u64)con_cols());
    con_putc('x');
    put_u64((u64)con_rows());
    con_puts(gfx_up() ? " (vbe graphics)\n" : " (vga text)\n");
    return 0;
}

/* Reading physical memory is the closest thing to a debugger we have, and it
 * is how the next few steps get checked: peek the arena, peek a module the
 * bootloader placed, peek the framebuffer. */
static int parse_u64(const char *s, u64 *out) {
    u64 v = 0;
    int any = 0;
    int base = 10;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) { base = 16; s += 2; }
    while (*s) {
        int d;
        if (*s >= '0' && *s <= '9')      d = *s - '0';
        else if (*s >= 'a' && *s <= 'f') d = *s - 'a' + 10;
        else if (*s >= 'A' && *s <= 'F') d = *s - 'A' + 10;
        else return 0;
        if (d >= base) return 0;
        v = v * (u64)base + (u64)d;
        any = 1;
        s++;
    }
    if (!any) return 0;
    *out = v;
    return 1;
}

static int cmd_peek(int argc, char **argv) {
    u64 addr, n = 64, i;
    if (argc < 2) { con_puts("usage: peek <addr> [count]\n"); return 1; }
    if (!parse_u64(argv[1], &addr)) { con_puts("bad address\n"); return 1; }
    if (argc >= 3 && !parse_u64(argv[2], &n)) { con_puts("bad count\n"); return 1; }
    if (n > 256) n = 256;

    for (i = 0; i < n; i += 16) {
        u64 j;
        put_hex64(addr + i);
        con_puts("  ");
        for (j = 0; j < 16 && i + j < n; j++) {
            static const char DIG[] = "0123456789abcdef";
            u8 b = *(volatile u8 *)(addr + i + j);
            con_putc(DIG[b >> 4]);
            con_putc(DIG[b & 0xF]);
            con_putc(' ');
        }
        con_putc('\n');
    }
    return 0;
}

/* Triple-fault on purpose: with no ACPI and no keyboard-controller reset
 * wired up, loading a null IDT and raising an interrupt is the shortest
 * reliable way to make QEMU restart. */
static int cmd_reboot(int argc, char **argv) {
    struct { u16 limit; u64 base; } __attribute__((packed)) null_idt = { 0, 0 };
    (void)argc; (void)argv;
    con_puts("rebooting...\n");
    ser_puts("[mbos] reboot requested\n");
    __asm__ volatile ("cli");
    __asm__ volatile ("lidt %0" : : "m"(null_idt));
    __asm__ volatile ("int3");
    for (;;) __asm__ volatile ("hlt");
    return 0;                   /* unreachable; keeps -Wreturn-type quiet */
}

/* `mem` with no argument summarises; `mem map` lists every block; `mem check`
 * runs the covering-invariant audit from alloc.rs. Being able to inspect the
 * heap from the shell is what keeps step 5 debuggable -- an interpreter that
 * leaks will show up here as a block count that only climbs. */
static int cmd_mem(int argc, char **argv) {
    if (argc >= 2 && mini_strcmp(argv[1], "map") == 0) {
        int i, n = kheap_blocks();
        for (i = 0; i < n; i++) {
            size_t off, size; int used;
            if (!kheap_block(i, &off, &size, &used)) break;
            con_puts("  ");
            put_hex64((u64)off);
            con_puts("  ");
            put_u64((u64)size);
            con_puts(used ? "  used\n" : "  free\n");
        }
        return 0;
    }

    if (argc >= 2 && mini_strcmp(argv[1], "check") == 0) {
        int bad = kheap_verify();
        if (bad == 0) {
            con_puts("heap consistent: blocks cover the arena in order\n");
        } else {
            con_puts("heap INCONSISTENT at block ");
            put_u64((u64)(bad - 1));
            con_putc('\n');
            ser_puts("[mbos] heap inconsistent\n");
        }
        return bad == 0 ? 0 : 1;
    }

    con_puts("used    "); put_u64((u64)kheap_used());
    con_puts(" / ");      put_u64((u64)kheap_total());
    con_puts("\nlargest "); put_u64((u64)kheap_largest());
    con_puts("\nblocks  "); put_u64((u64)kheap_blocks());
    con_puts("\nfailed  "); put_u64((u64)kheap_failures());
    con_putc('\n');
    return 0;
}

/* Exercise the allocator from the shell: alloc a run of blocks, free every
 * other one, verify, then free the rest and verify that everything coalesced
 * back to a single block. This is the interactive twin of test_alloc.py. */
static int cmd_memtest(int argc, char **argv) {
    void *p[16];
    int i, bad;
    (void)argc; (void)argv;

    for (i = 0; i < 16; i++) {
        p[i] = kmalloc((size_t)(64 + i * 16));
        if (!p[i]) { con_puts("alloc failed\n"); return 1; }
    }
    for (i = 0; i < 16; i += 2) { kfree(p[i]); p[i] = 0; }

    bad = kheap_verify();
    if (bad) { con_puts("inconsistent after partial free\n"); return 1; }

    for (i = 1; i < 16; i += 2) { kfree(p[i]); p[i] = 0; }

    bad = kheap_verify();
    if (bad) { con_puts("inconsistent after full free\n"); return 1; }

    if (kheap_blocks() != 1 || kheap_used() != 0) {
        con_puts("did not coalesce back to one free block\n");
        ser_puts("[mbos] memtest FAIL\n");
        return 1;
    }
    /* Negative half: an allocator that accepts a double free is worse than one
     * that fails outright, because it merges two live blocks and the damage
     * surfaces somewhere unrelated. Both rejections below print a line to
     * serial, which is the expected output, not a failure. */
    {
        void *q = kmalloc(128);
        int blocks_before;
        if (!q) { con_puts("alloc failed\n"); return 1; }
        kfree(q);
        blocks_before = kheap_blocks();
        kfree(q);                       /* double free: must be refused */
        if (kheap_blocks() != blocks_before || kheap_verify()) {
            con_puts("double free was not rejected cleanly\n");
            ser_puts("[mbos] memtest FAIL double-free\n");
            return 1;
        }
        kfree((void *)0x1000);          /* outside the arena: must be refused */
        if (kheap_verify()) {
            con_puts("bad pointer corrupted the heap\n");
            ser_puts("[mbos] memtest FAIL bad-pointer\n");
            return 1;
        }
        /* Put the heap back to empty so the summary after this is comparable
         * to the one before it. */
        if (kheap_blocks() != 1 || kheap_used() != 0) {
            con_puts("heap not empty after rejection tests\n");
            return 1;
        }
    }

    con_puts("memtest ok: 16 allocs, interleaved frees, fully coalesced\n");
    con_puts("           double free and bad pointer both rejected\n");
    ser_puts("[mbos] memtest ok\n");
    return 0;
}

/* ---- graphics ---------------------------------------------------------- */

/* What the display device actually reports, as opposed to what the build asked
 * for. The two differ more often at hi-res than anywhere else: a mode can fit
 * the geometry limits and still not fit in video memory, and the scanline
 * stride is not always the width. */
static int cmd_gfx(int argc, char **argv) {
    if (!gfx_up()) {
        con_puts("no framebuffer (vga text mode)\n");
        return 1;
    }

    if (argc >= 3 && mini_strcmp(argv[1], "fits") == 0) {
        con_puts("usage: gfx fits <w> <h>\n");
        return 1;
    }

    con_puts("mode    ");
    put_u64((u64)gfx_width());  con_putc('x');
    put_u64((u64)gfx_height()); con_puts(" 32bpp\n");

    con_puts("stride  ");
    put_u64((u64)gfx_stride());
    con_puts(gfx_stride() == gfx_width() ? " px (== width)\n"
                                         : " px (padded)\n");

    con_puts("max     ");
    put_u64((u64)gfx_max_width());  con_putc('x');
    put_u64((u64)gfx_max_height()); con_putc('\n');

    con_puts("vram    ");
    put_u64((u64)(gfx_vram() >> 20)); con_puts(" MiB\n");

    con_puts("frame   ");
    put_u64(((u64)gfx_width() * gfx_height() * 4) >> 20);
    con_puts(" MiB\n");

    ser_puts("[mbos] gfx ");
    ser_dec(gfx_width()); ser_puts("x"); ser_dec(gfx_height());
    ser_puts(" stride "); ser_dec(gfx_stride());
    ser_puts(" vram "); ser_dec(gfx_vram());
    ser_puts(" max "); ser_dec(gfx_max_width());
    ser_puts("x"); ser_dec(gfx_max_height());
    ser_puts("\n");
    return 0;
}

/* Draw straight to the framebuffer: colour bars, a gradient, and a set of
 * one-pixel markers at the four corners. The corners are the useful part --
 * if the stride is wrong the right-hand markers walk diagonally down the
 * screen instead of sitting on the edge, which is visible at a glance and is
 * exactly the bug that assuming stride == width would cause. */
static int cmd_gfxtest(int argc, char **argv) {
    u32 w = gfx_width(), h = gfx_height();
    u32 i, x, y;
    static const u32 BARS[8] = {
        0xFFFFFF, 0xFFFF00, 0x00FFFF, 0x00FF00,
        0xFF00FF, 0xFF0000, 0x0000FF, 0x202020
    };
    (void)argc; (void)argv;

    if (!gfx_up()) { con_puts("no framebuffer\n"); return 1; }

    for (i = 0; i < 8; i++)
        gfx_rect(i * (w / 8), 0, w / 8, h / 2, BARS[i]);

    for (y = h / 2; y < h; y++) {
        u32 v = ((y - h / 2) * 255) / (h - h / 2);
        gfx_rect(0, y, w, 1, (v << 16) | (v << 8) | v);
    }

    for (x = 0; x < 16; x++) {
        gfx_pixel(x, 0, 0xFF0000);
        gfx_pixel(w - 1 - x, 0, 0x00FF00);
        gfx_pixel(x, h - 1, 0x0000FF);
        gfx_pixel(w - 1 - x, h - 1, 0xFFFF00);
    }

    ser_puts("[mbos] gfxtest drew ");
    ser_dec(w); ser_puts("x"); ser_dec(h); ser_puts("\n");
    return 0;
}

/* Run the three-language engine and show the result.
 *
 * The checksum printed here is the same number examples/baremetal/
 * kernel_mingine.c reports when ShivyCX builds that scene as its own kernel,
 * and the same one the hosted build prints. Three compilers' worth of paths
 * through the same Rust + rpython + C, one 32-bit answer. */
static int cmd_demo(int argc, char **argv) {
    unsigned int sum;
    (void)argc; (void)argv;

    con_puts("rendering (rust geometry + rpython rules + c blitting)...\n");
    sum = mingine_render();
    mingine_present();

    con_puts("scene   ");
    put_u64((u64)mingine_width()); con_putc('x');
    put_u64((u64)mingine_height()); con_putc('\n');
    con_puts("ball    ");
    put_u64((u64)mingine_ball_x()); con_putc(',');
    put_u64((u64)mingine_ball_y()); con_putc('\n');
    con_puts("foe     ");
    put_u64((u64)mingine_foe_x()); con_putc(',');
    put_u64((u64)mingine_foe_y()); con_putc('\n');
    con_puts("score   ");
    put_u64((u64)mingine_score()); con_putc('\n');
    con_puts("pixels  ");
    put_hex64((u64)sum); con_putc('\n');

    ser_puts("[mbos] demo pixels ");
    ser_dec((u64)sum);
    ser_puts(" ball ");
    ser_dec((u64)mingine_ball_x()); ser_puts(",");
    ser_dec((u64)mingine_ball_y());
    ser_puts(" foe ");
    ser_dec((u64)mingine_foe_x()); ser_puts(",");
    ser_dec((u64)mingine_foe_y());
    ser_puts(" score ");
    ser_dec((u64)mingine_score());
    ser_puts("\n");
    return 0;
}

/* ---- ramdisk commands -------------------------------------------------- */

static int cmd_ls(int argc, char **argv) {
    int i, n = ramfs_count();
    (void)argc; (void)argv;

    if (n == 0) {
        con_puts("no ramdisk mounted (boot with -initrd)\n");
        return 1;
    }
    for (i = 0; i < n; i++) {
        int pad = 24 - (int)mini_strlen(ramfs_name(i));
        con_puts("  ");
        con_puts(ramfs_name(i));
        while (pad-- > 0) con_putc(' ');
        put_u64((u64)ramfs_size(i));
        con_putc('\n');
    }
    con_puts("  ");
    put_u64((u64)n);
    con_puts(" file(s), module ");
    put_u64((u64)ramfs_bytes());
    con_puts(" bytes\n");
    ser_puts("[mbos] ls listed files\n");
    return 0;
}

static int cmd_cat(int argc, char **argv) {
    int idx;
    u32 size, i;
    const u8 *data;

    if (argc < 2) { con_puts("usage: cat <file>\n"); return 1; }

    idx = ramfs_find(argv[1]);
    if (idx < 0) {
        con_puts(argv[1]);
        con_puts(": no such file\n");
        ser_puts("[mbos] cat: not found\n");
        return 1;
    }

    data = ramfs_data(idx, &size);
    if (!data) { con_puts("unreadable\n"); return 1; }

    for (i = 0; i < size; i++) {
        char c = (char)data[i];
        /* Printable and newline only -- a stray control byte from a binary
         * file would otherwise walk the cursor around the screen. */
        if (c == '\n' || (c >= ' ' && c < 127)) con_putc(c);
        else if (c == '\t') con_puts("    ");
    }
    if (size > 0 && data[size - 1] != '\n') con_putc('\n');
    return 0;
}

static const struct command CMDS[] = {
    { "help",   cmd_help,   "list commands" },
    { "echo",   cmd_echo,   "print arguments" },
    { "clear",  cmd_clear,  "clear the screen" },
    { "ticks",  cmd_ticks,  "raw timer tick count" },
    { "uptime", cmd_uptime, "seconds since boot" },
    { "ver",    cmd_ver,    "kernel and console info" },
    { "demo",   cmd_demo,   "run the mingine scene on the framebuffer" },
    { "gfx",    cmd_gfx,    "display mode, stride, vram" },
    { "gfxtest",cmd_gfxtest,"draw test bars to the framebuffer" },
    { "ls",     cmd_ls,     "list files on the ramdisk" },
    { "cat",    cmd_cat,    "print a ramdisk file: cat <name>" },
    { "mem",    cmd_mem,    "heap summary; 'mem map', 'mem check'" },
    { "memtest",cmd_memtest,"alloc/free torture, then verify" },
    { "peek",   cmd_peek,   "hexdump memory: peek <addr> [n]" },
    { "reboot", cmd_reboot, "restart the machine" },
    { 0, 0, 0 }
};

/* ---- line editor -------------------------------------------------------
 *
 * The buffer and the history ring live in editbuf.rs and are checked by rustc;
 * everything below is the half that has to touch hardware -- reading keys and
 * repainting the row -- which is why it stays in C. The division is simply
 * whether a function can be checked without a machine: if it can, it belongs
 * on the Rust side.
 */

static EditBuf ed;
static History hist;
static int     ed_home = 0;       /* console column where the input starts */

/* Repaint from ed_home to the end of the line, then park the cursor. Simpler
 * than tracking which cells changed, and at 128 columns the cost is invisible. */
static void ed_redraw(void) {
    int i, n = EditBuf_length(&ed);
    con_mirror(0);              /* screen-only: see con_mirror() in console.c */
    con_set_col(ed_home);
    con_clear_eol();
    con_set_col(ed_home);
    for (i = 0; i < n; i++) con_putc((char)EditBuf_byte(&ed, i));
    con_set_col(ed_home + EditBuf_cursor(&ed));
    con_mirror(1);
}

static void ed_sync_cursor(void) {
    con_set_col(ed_home + EditBuf_cursor(&ed));
}

/* Copy the line out for the dispatcher, which tokenizes in place and so needs
 * storage it may write to. */
static char line_out[LINE_MAX];

static char *ed_take(void) {
    int i, n = EditBuf_length(&ed);
    if (n > LINE_MAX - 1) n = LINE_MAX - 1;
    for (i = 0; i < n; i++) line_out[i] = (char)EditBuf_byte(&ed, i);
    line_out[n] = 0;
    return line_out;
}

static void hist_step(int slot) {
    if (slot < 0) EditBuf_kill(&ed);
    else          EditBuf_set_from(&ed, &hist, slot);
    ed_redraw();
}

/* Block until Enter, then hand back the completed line. */
static char *shell_readline(void) {
    EditBuf_reset(&ed);
    History_set_cursor(&hist, -1);
    ed_home = con_col();

    for (;;) {
        int c;

        if (!kbd_haskey()) {
            __asm__ volatile ("hlt");    /* park until an interrupt arrives */
            continue;
        }
        c = kbd_getch();
        if (c < 0) continue;

        switch (c) {
        case '\n':
            con_set_col(ed_home + EditBuf_length(&ed));
            con_mirror(0);
            con_putc('\n');
            con_mirror(1);
            /* One clean copy of the finished line, instead of the dozens of
             * partial repaints the editor produced getting there. */
            ser_puts(ed_take());
            ser_puts("\n");
            return line_out;

        case '\b':
            if (EditBuf_backspace(&ed)) ed_redraw();
            break;

        case KEY_DELETE:
            if (EditBuf_delete_at(&ed, EditBuf_cursor(&ed))) ed_redraw();
            break;

        case KEY_LEFT:
            if (EditBuf_left(&ed))  ed_sync_cursor();
            break;

        case KEY_RIGHT:
            if (EditBuf_right(&ed)) ed_sync_cursor();
            break;

        case KEY_HOME:
        case 1:     /* Ctrl+A */
            if (EditBuf_home(&ed))  ed_sync_cursor();
            break;

        case KEY_END:
        case 5:     /* Ctrl+E */
            if (EditBuf_end(&ed))   ed_sync_cursor();
            break;

        case KEY_UP:
            { int slot = History_older(&hist); if (slot >= 0) hist_step(slot); }
            break;

        case KEY_DOWN:
            if (History_cursor(&hist) >= 0) hist_step(History_newer(&hist));
            break;

        case 21:    /* Ctrl+U -- kill the line */
            EditBuf_kill(&ed);
            ed_redraw();
            break;

        case 3:     /* Ctrl+C -- abandon this line */
            con_puts("^C\n");
            ser_puts("\n");
            EditBuf_kill(&ed);
            line_out[0] = 0;
            return line_out;

        default:
            /* No line wrapping yet: refuse rather than corrupt the row. */
            if (c >= ' ' && c < 127 &&
                ed_home + EditBuf_length(&ed) < con_cols() - 1) {
                if (EditBuf_insert(&ed, (u8)c)) ed_redraw();
            }
            break;
        }
    }
}

/* ---- dispatch ---------------------------------------------------------- */

static char *argv_buf[ARGV_MAX];

/* Split in place on runs of spaces and tabs. No quoting yet -- when a command
 * needs an argument containing a space, that is the moment to add it. */
static int tokenize(char *line, char **argv, int max) {
    int argc = 0;
    char *p = line;
    while (*p && argc < max) {
        while (*p == ' ' || *p == '\t') *p++ = 0;
        if (*p == 0) break;
        argv[argc++] = p;
        while (*p && *p != ' ' && *p != '\t') p++;
    }
    return argc;
}

static void shell_exec(char *line) {
    int argc = tokenize(line, argv_buf, ARGV_MAX);
    int i;

    if (argc == 0) return;

    for (i = 0; CMDS[i].name; i++) {
        if (mini_strcmp(CMDS[i].name, argv_buf[0]) == 0) {
            /* Mirror to serial so the headless tests can see what ran. */
            ser_puts("[mbos] exec ");
            ser_puts(argv_buf[0]);
            ser_puts("\n");
            CMDS[i].fn(argc, argv_buf);
            return;
        }
    }

    con_puts(argv_buf[0]);
    con_puts(": not found (try 'help')\n");
    ser_puts("[mbos] not found: ");
    ser_puts(argv_buf[0]);
    ser_puts("\n");
}

void shell_run(void) {
    kheap_init();
    EditBuf_reset(&ed);
    History_reset(&hist);
    con_puts("\ncrust shell -- 'help' for commands\n");
    ser_puts("[mbos] shell ready\n");

    for (;;) {
        char *line;
        con_puts("\n" SHELL_PROMPT);
        line = shell_readline();
        History_push(&hist, &ed);   /* dedup of a repeated command is in Rust */
        shell_exec(line);
    }
}
