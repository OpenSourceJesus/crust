/* kbd.c -- PS/2 keyboard for mbos.
 *
 * The IRQ1 handler does the minimum: read port 0x60 (which is what releases
 * the controller), translate, push into a ring. Everything else -- echo, line
 * editing, dispatch -- happens in the foreground loop, so no console work is
 * ever done from interrupt context.
 *
 * Scancode set 1, the default translated set QEMU's i8042 emulation gives us.
 */
#include "mbos.h"

#define KBD_DATA   0x60
#define KBD_STATUS 0x64

#define RING_SZ 128             /* power of two */

/* The ring holds ints, not chars, so the extended keys can be delivered as
 * values above 0xFF rather than as an escape sequence the reader has to
 * reassemble. KEY_* are defined in mbos.h. */
static volatile int      ring[RING_SZ];
static volatile unsigned r_head = 0;    /* written by the IRQ  */
static volatile unsigned r_tail = 0;    /* read by the foreground */

static int shift_down = 0;
static int ctrl_down  = 0;
static int caps_lock  = 0;

/* Set 1 make codes 0x00..0x39, unshifted. 0 means "no character". */
static const char MAP[0x40] = {
    0,    27,  '1', '2', '3', '4', '5', '6',
    '7', '8', '9', '0', '-', '=', '\b', '\t',
    'q', 'w', 'e', 'r', 't', 'y', 'u', 'i',
    'o', 'p', '[', ']', '\n', 0,  'a', 's',
    'd', 'f', 'g', 'h', 'j', 'k', 'l', ';',
    '\'', '`', 0,  '\\', 'z', 'x', 'c', 'v',
    'b', 'n', 'm', ',', '.', '/', 0,   '*',
    0,   ' ', 0,   0,   0,   0,   0,   0
};

/* Same codes with shift held. */
static const char MAP_SHIFT[0x40] = {
    0,    27,  '!', '@', '#', '$', '%', '^',
    '&', '*', '(', ')', '_', '+', '\b', '\t',
    'Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I',
    'O', 'P', '{', '}', '\n', 0,  'A', 'S',
    'D', 'F', 'G', 'H', 'J', 'K', 'L', ':',
    '"', '~', 0,  '|', 'Z', 'X', 'C', 'V',
    'B', 'N', 'M', '<', '>', '?', 0,   '*',
    0,   ' ', 0,   0,   0,   0,   0,   0
};

static int  e0_pending = 0;     /* previous byte was the 0xE0 prefix */

static void ring_push(int c) {
    unsigned next = (r_head + 1) & (RING_SZ - 1);
    if (next == r_tail) return;         /* full: drop, never block in an IRQ */
    ring[r_head] = c;
    r_head = next;
}

/* Extended (0xE0-prefixed) make codes we care about. */
static int extended_key(u8 sc) {
    switch (sc) {
        case 0x48: return KEY_UP;
        case 0x50: return KEY_DOWN;
        case 0x4B: return KEY_LEFT;
        case 0x4D: return KEY_RIGHT;
        case 0x47: return KEY_HOME;
        case 0x4F: return KEY_END;
        case 0x53: return KEY_DELETE;
        default:   return 0;
    }
}

static void on_key(void) {
    u8 sc = inb(KBD_DATA);
    int release;
    char c;

    /* 0xE0 prefixes the extended keys: arrows, Home/End, Delete, right ctrl.
     * The prefix and its code arrive as two separate interrupts, so the flag
     * has to persist across calls. */
    if (sc == 0xE0) { e0_pending = 1; return; }

    release = (sc & 0x80) != 0;
    sc = (u8)(sc & 0x7F);

    if (e0_pending) {
        e0_pending = 0;
        if (sc == 0x1D) { ctrl_down = !release; return; }   /* right ctrl */
        if (!release) {
            int k = extended_key(sc);
            if (k) ring_push(k);
        }
        return;
    }

    /* modifiers */
    if (sc == 0x2A || sc == 0x36) { shift_down = !release; return; }
    if (sc == 0x1D)               { ctrl_down  = !release; return; }
    if (sc == 0x3A) { if (!release) caps_lock = !caps_lock; return; }

    if (release) return;
    if (sc >= 0x40) return;

    c = shift_down ? MAP_SHIFT[sc] : MAP[sc];
    if (c == 0) return;

    if (caps_lock && c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
    else if (caps_lock && shift_down && c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');

    /* Ctrl+letter -> control character, so Ctrl+C and friends are available
     * to whatever line editor sits on top of this. */
    if (ctrl_down) {
        if (c >= 'a' && c <= 'z')      c = (char)(c - 'a' + 1);
        else if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 1);
        else return;
    }

    ring_push((int)(u8)c);
}

void kbd_init(void) {
    r_head = r_tail = 0;
    shift_down = ctrl_down = caps_lock = 0;
    e0_pending = 0;

    /* Drain anything the firmware left in the output buffer, or the controller
     * will never assert IRQ1. */
    while (inb(KBD_STATUS) & 0x01) (void)inb(KBD_DATA);

    irq_register(33, on_key);       /* IRQ1 -> vector 33 */
}

int kbd_getch(void) {
    int c;
    if (r_tail == r_head) return -1;
    c = ring[r_tail];
    r_tail = (r_tail + 1) & (RING_SZ - 1);
    return c;
}

int kbd_haskey(void) {
    return r_tail != r_head;
}
