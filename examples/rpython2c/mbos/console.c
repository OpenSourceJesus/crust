/* console.c -- the mbos console: draws characters either into a Bochs-VBE
 * graphics framebuffer (via vbe.c, using the 8x16 bitmap font) or into the
 * legacy VGA text buffer at 0xB8000, and mirrors every glyph to COM1 serial.
 *
 * The DOM renderers (render.c and the rpython render.py) only ever call the
 * con_* API -- putc / newline / set_attr / col -- so switching from an 80x25
 * text grid to a 1024x768 graphics surface changed nothing above this file:
 * the same traversal now lays out proportionally more text and paints it with
 * a real font. Colour attributes (the 4-bit VGA palette) map to RGB in gfx
 * mode and to the hardware attribute byte in text mode.
 */
#include "mbos.h"
#include "font8x16.h"

#define VGA_MEM ((volatile u16 *)0xB8000)
#define COM1    0x3F8

static int s_row = 0;
static int s_col = 0;
static u8  s_attr = 0;
static int s_cols = VGA_COLS;
static int s_rows = VGA_ROWS;
static int s_gfx = 0;
static int s_mirror = 1;    /* mirror con_putc to serial? */

/* 16-colour VGA palette -> RGB, for graphics mode. */
static const u32 PALETTE[16] = {
    0x000000, 0x0000AA, 0x00AA00, 0x00AAAA,
    0xAA0000, 0xAA00AA, 0xAA5500, 0xAAAAAA,
    0x555555, 0x5555FF, 0x55FF55, 0x55FFFF,
    0xFF5555, 0xFF55FF, 0xFFFF55, 0xFFFFFF,
};

/* ---- serial ------------------------------------------------------------ */
static void serial_init(void) {
    outb(COM1 + 1, 0x00); outb(COM1 + 3, 0x80); outb(COM1 + 0, 0x03);
    outb(COM1 + 1, 0x00); outb(COM1 + 3, 0x03); outb(COM1 + 2, 0xC7);
    outb(COM1 + 4, 0x0B);
}
static void serial_putc(char c) {
    int spin = 100000;
    while (spin-- > 0 && (inb(COM1 + 5) & 0x20) == 0) { }
    outb(COM1, (u8)c);
}
void ser_puts(const char *s) {
    while (*s) { if (*s == '\n') serial_putc('\r'); serial_putc(*s++); }
}

/* Decimal to serial. There is no printf on bare metal, and the diagnostics
 * that matter most -- geometry, sizes, counts -- are all numbers. */
void ser_dec(u64 v) {
    char buf[24];
    int i = 23;
    buf[i] = 0;
    if (v == 0) buf[--i] = '0';
    while (v > 0) { buf[--i] = (char)('0' + (v % 10)); v /= 10; }
    ser_puts(&buf[i]);
}

/* ---- attribute helpers ------------------------------------------------- */
static u32 fg_rgb(void) { return PALETTE[s_attr & 0x0F]; }
static u32 bg_rgb(void) { return PALETTE[(s_attr >> 4) & 0x0F]; }

/* ---- glyph / cell drawing --------------------------------------------- */
static const u8 *glyph_rows(char c) {
    unsigned uc = (unsigned char)c;
    if (uc < FONT_FIRST || uc > FONT_LAST) uc = ' ';
    return FONT8X16[uc - FONT_FIRST];
}

static void draw_cell(int col, int row, char c) {
    if (s_gfx) {
        gfx_glyph(glyph_rows(c), (u32)col * FONT_W, (u32)row * FONT_H,
                  fg_rgb(), bg_rgb());
    } else {
        VGA_MEM[row * s_cols + col] = (u16)((u8)c | (s_attr << 8));
    }
}

/* ---- text-mode hardware cursor ---------------------------------------- */
static void move_cursor(void) {
    if (s_gfx) return;
    u16 pos = (u16)(s_row * s_cols + s_col);
    outb(0x3D4, 14); outb(0x3D5, (u8)(pos >> 8));
    outb(0x3D4, 15); outb(0x3D5, (u8)(pos & 0xFF));
}

static void scroll_if_needed(void) {
    if (s_row < s_rows) return;
    if (s_gfx) {
        gfx_scroll(FONT_H, bg_rgb());
    } else {
        int i;
        for (i = 0; i < (s_rows - 1) * s_cols; i++) VGA_MEM[i] = VGA_MEM[i + s_cols];
        u16 blank = (u16)(' ' | (s_attr << 8));
        for (i = (s_rows - 1) * s_cols; i < s_rows * s_cols; i++) VGA_MEM[i] = blank;
    }
    s_row = s_rows - 1;
}

/* ---- public API -------------------------------------------------------- */
void con_init(void) {
    serial_init();
    s_attr = VGA_ATTR(VGA_LGREY, VGA_BLACK);
    if (gfx_init(MBOS_GFX_W, MBOS_GFX_H) == 0) {
        s_gfx = 1;
        s_cols = (int)(gfx_width() / FONT_W);
        s_rows = (int)(gfx_height() / FONT_H);
        ser_puts("[con] graphics console\n");
    } else {
        s_gfx = 0; s_cols = VGA_COLS; s_rows = VGA_ROWS;
        ser_puts("[con] text console\n");
    }
    con_clear(s_attr);
}

void con_clear(u8 attr) {
    s_attr = attr;
    if (s_gfx) {
        gfx_fill(bg_rgb());
    } else {
        u16 cell = (u16)(' ' | (attr << 8));
        int i;
        for (i = 0; i < s_cols * s_rows; i++) VGA_MEM[i] = cell;
    }
    s_row = 0; s_col = 0;
    move_cursor();
}

void con_set_attr(u8 attr) { s_attr = attr; }
int  con_col(void)  { return s_col; }
int  con_cols(void) { return s_cols; }
int  con_rows(void) { return s_rows; }

void con_newline(void) {
    s_col = 0; s_row++;
    scroll_if_needed();
    ser_puts("\n");
    move_cursor();
}

/* Serial mirroring is on by default so the headless tests see everything the
 * screen shows. The line editor turns it off while repainting: a redraw is a
 * screen operation, and echoing every intermediate state of the line to serial
 * turns the log into noise. */
void con_mirror(int on) { s_mirror = on; }

void con_putc(char c) {
    if (c == '\n') { con_newline(); return; }
    draw_cell(s_col, s_row, c);
    if (s_mirror) { int spin = 100000; while (spin-- > 0 && (inb(COM1 + 5) & 0x20) == 0) {} outb(COM1, (u8)c); }
    s_col++;
    if (s_col >= s_cols) con_newline();
    else move_cursor();
}

void con_puts(const char *s) { while (*s) con_putc(*s++); }

/* ---- line-editing primitives ------------------------------------------
 *
 * The renderers only ever append, so the console never needed to move the
 * cursor backwards or erase. A line editor does: after an insert or a delete
 * in the middle of the line it repaints the tail, which means rewinding to a
 * known column and clearing whatever the previous, longer line left behind.
 */
void con_set_col(int col) {
    if (col < 0) col = 0;
    if (col > s_cols - 1) col = s_cols - 1;
    s_col = col;
    move_cursor();
}

void con_clear_eol(void) {
    int c;
    for (c = s_col; c < s_cols; c++) draw_cell(c, s_row, ' ');
    move_cursor();
}

/* Erase the character to the left of the cursor. */
void con_backspace(void) {
    if (s_col == 0) return;
    s_col--;
    draw_cell(s_col, s_row, ' ');
    move_cursor();
    ser_puts("\b \b");
}
