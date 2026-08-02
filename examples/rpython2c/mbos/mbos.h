/* mbos.h -- shared declarations for the freestanding mbos kernel.
 *
 * No libc is available on bare metal, so the handful of helpers the browser
 * needs (memset/memcpy/strlen/strcmp) are provided by libmini.c. Everything
 * here is plain C89-ish so it also compiles cleanly under ShivyCX later.
 */
#ifndef MBOS_H
#define MBOS_H

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;
typedef unsigned long  u64;
typedef __SIZE_TYPE__  size_t;

/* ---- port I/O ---------------------------------------------------------- */
static inline void outb(u16 port, u8 val) {
    __asm__ volatile ("outb %0, %1" : : "a"(val), "Nd"(port));
}
static inline u8 inb(u16 port) {
    u8 r; __asm__ volatile ("inb %1, %0" : "=a"(r) : "Nd"(port)); return r;
}

/* ---- mini libc (libmini.c) -------------------------------------------- */
void  *mini_memset(void *d, int c, size_t n);
void  *mini_memcpy(void *d, const void *s, size_t n);
size_t mini_strlen(const char *s);
int    mini_strcmp(const char *a, const char *b);

/* ---- console: VGA text (0xB8000) + COM1 serial (console.c) ------------- */
/* VGA text-mode colour attributes (foreground | background<<4). */
#define VGA_BLACK      0
#define VGA_BLUE       1
#define VGA_GREEN      2
#define VGA_CYAN       3
#define VGA_RED        4
#define VGA_MAGENTA    5
#define VGA_BROWN      6
#define VGA_LGREY      7
#define VGA_DGREY      8
#define VGA_LBLUE      9
#define VGA_LGREEN     10
#define VGA_LCYAN      11
#define VGA_LRED       12
#define VGA_LMAGENTA   13
#define VGA_YELLOW     14
#define VGA_WHITE      15
#define VGA_ATTR(fg, bg) ((u8)((fg) | ((bg) << 4)))

#define VGA_COLS 80
#define VGA_ROWS 25

void con_init(void);
void con_clear(u8 attr);
void con_set_attr(u8 attr);
void con_putc(char c);           /* also mirrored to serial */
void con_puts(const char *s);
void con_newline(void);
void con_backspace(void);        /* erase the cell left of the cursor */
void con_set_col(int col);       /* move the cursor within the current row */
void con_clear_eol(void);         /* blank from the cursor to the row end */
void con_mirror(int on);         /* enable/disable the serial mirror */
int  con_col(void);              /* current cursor column (for word-wrap) */
int  con_cols(void);             /* total character columns (text 80, gfx W/8) */
int  con_rows(void);             /* total character rows */

/* Serial-only output, for the headless test harness to scrape. */
void ser_puts(const char *s);
void ser_dec(u64 v);             /* decimal, for geometry/size diagnostics */

/* ---- interrupts: 64-bit IDT + 8259 PIC + PIT (irq.c, idt.S) ----------- */
/* Not built into the 32-bit text-mode kernel -- idt.S is long mode only. */
typedef void (*irq_handler_t)(void);

void irq_init(u32 hz);           /* IDT + PIC remap + timer + kbd, then sti  */
void idt_init(void);
void irq_register(u8 vec, irq_handler_t handler);
void pic_enable_irq(u8 irq);
void pit_init(u32 hz);
u64  irq_ticks(void);            /* monotonic tick count since irq_init      */

/* ---- keyboard: PS/2, scancode set 1 (kbd.c) --------------------------- */
/* Extended keys are delivered above the ASCII range so a reader can switch on
 * the value directly instead of decoding an escape sequence. */
#define KEY_UP      0x100
#define KEY_DOWN    0x101
#define KEY_LEFT    0x102
#define KEY_RIGHT   0x103
#define KEY_HOME    0x104
#define KEY_END     0x105
#define KEY_DELETE  0x106

void kbd_init(void);
int  kbd_getch(void);            /* next char, or -1 if the ring is empty    */
int  kbd_haskey(void);

/* ---- kernel heap (alloc.c + alloc.rs) --------------------------------- */
/* Block bookkeeping lives in alloc.rs and is checked by rustc; alloc.c owns
 * the arena and the offset-to-pointer translation. Not interrupt-safe. */
void  *kmalloc(size_t n);
void  *kzalloc(size_t n);        /* kmalloc + zero */
void   kfree(void *p);           /* NULL is a no-op; a bad pointer is reported */
void   kheap_init(void);

size_t kheap_total(void);
size_t kheap_used(void);
size_t kheap_largest(void);      /* biggest free run -- fragmentation signal */
int    kheap_blocks(void);
int    kheap_failures(void);
int    kheap_verify(void);       /* 0 = consistent, else 1-based bad block */
int    kheap_block(int i, size_t *off, size_t *size, int *used);

/* ---- ramdisk (ramfs.c + tarfs.rs) ------------------------------------- */
/* A tar archive handed to the kernel as a Multiboot module. Read-only: the
 * table points into the module image rather than copying it. */
int         ramfs_init(void *mbi);   /* returns the file count */
int         ramfs_count(void);
const char *ramfs_name(int i);
u32         ramfs_size(int i);
int         ramfs_find(const char *name);   /* index, or -1 */
const u8   *ramfs_data(int i, u32 *size);
u32         ramfs_bytes(void);       /* size of the whole module */

/* ---- shell (shell.c) -------------------------------------------------- */
void shell_run(void);            /* never returns */

/* ---- graphics: Bochs-VBE linear framebuffer (vbe.c) ------------------- */
int  gfx_init(u32 w, u32 h);     /* 0 on success; sets a 32-bpp LFB mode    */
int  gfx_up(void);
u32  gfx_width(void);
u32  gfx_height(void);
u32  gfx_stride(void);           /* pixels per scanline; may exceed width   */
u32  gfx_vram(void);             /* bytes of video memory reported          */
u32  gfx_max_width(void);        /* device maximum, from the GETCAPS query  */
u32  gfx_max_height(void);
int  gfx_mode_fits(u32 w, u32 h);/* geometry AND memory both allow it       */
void gfx_pixel(u32 x, u32 y, u32 rgb);
void gfx_fill(u32 rgb);
void gfx_rect(u32 x, u32 y, u32 w, u32 h, u32 rgb);
void gfx_glyph(const u8 *rows, u32 px, u32 py, u32 fg, u32 bg);
void gfx_scroll(u32 dy, u32 bg);
void gfx_present(const u32 *src); /* blit a width*height RAM buffer         */

/* Default graphics geometry (fits the 16 MiB std-VGA default; override in the
 * Makefile with -DMBOS_GFX_W / -DMBOS_GFX_H for the hi-res target). */
#ifndef MBOS_GFX_W
#define MBOS_GFX_W 1024
#endif
#ifndef MBOS_GFX_H
#define MBOS_GFX_H 768
#endif

#endif /* MBOS_H */
