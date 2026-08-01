/* irq.c -- interrupts for mbos: 64-bit IDT, 8259 PIC remap, PIT timer.
 *
 * Adapted from baremetal64/idt64.c, which targets minikraft and pulls in its
 * console.h/string.h. mbos has neither, so this version speaks mbos.h only:
 * mini_memset for zeroing, ser_puts/con_puts for the panic path, and the
 * inline outb/inb already declared there.
 *
 * The stub table (isr0..isr31, irq0..irq15) lives in idt.S, copied verbatim
 * from baremetal64/idt64.S -- the frame layout below must match its pushes.
 *
 * What this adds over the old `for(;;) hlt` ending in main.c: the kernel can
 * now be woken by hardware. That is the difference between a renderer and
 * something that can host a shell.
 */
#include "mbos.h"

#define IDT_ENTRIES 256
#define KERNEL_CS   0x08      /* 64-bit code selector from boot64.S GDT */
#define GATE_INTR   0x8E      /* present, DPL0, 64-bit interrupt gate */

/* 8259A PIC */
#define PIC1_CMD  0x20
#define PIC1_DATA 0x21
#define PIC2_CMD  0xA0
#define PIC2_DATA 0xA1
#define PIC_EOI   0x20

/* 8253/8254 PIT */
#define PIT_CH0   0x40
#define PIT_CMD   0x43
#define PIT_HZ    1193182u

struct idt_entry {
    u16 offset_low;
    u16 selector;
    u8  ist;
    u8  type_attr;
    u16 offset_mid;
    u32 offset_high;
    u32 zero;
} __attribute__((packed));

struct idt_ptr {
    u16 limit;
    u64 base;
} __attribute__((packed));

/* Must match the push order in idt.S's SAVE_REGS. */
struct interrupt_frame64 {
    u64 rax, rbx, rcx, rdx, rsi, rdi, rbp;
    u64 r8, r9, r10, r11, r12, r13, r14, r15;
    u64 int_no, err_code;
    u64 rip, cs, rflags, rsp, ss;
};

static struct idt_entry idt[IDT_ENTRIES];
static struct idt_ptr   idtp;
static irq_handler_t    handlers[IDT_ENTRIES];

static volatile u64 g_ticks = 0;

/* Stub table from idt.S */
extern void isr0(void);  extern void isr1(void);  extern void isr2(void);
extern void isr3(void);  extern void isr4(void);  extern void isr5(void);
extern void isr6(void);  extern void isr7(void);  extern void isr8(void);
extern void isr9(void);  extern void isr10(void); extern void isr11(void);
extern void isr12(void); extern void isr13(void); extern void isr14(void);
extern void isr15(void); extern void isr16(void); extern void isr17(void);
extern void isr18(void); extern void isr19(void); extern void isr20(void);
extern void isr21(void); extern void isr22(void); extern void isr23(void);
extern void isr24(void); extern void isr25(void); extern void isr26(void);
extern void isr27(void); extern void isr28(void); extern void isr29(void);
extern void isr30(void); extern void isr31(void);
extern void irq0(void);  extern void irq1(void);  extern void irq2(void);
extern void irq3(void);  extern void irq4(void);  extern void irq5(void);
extern void irq6(void);  extern void irq7(void);  extern void irq8(void);
extern void irq9(void);  extern void irq10(void); extern void irq11(void);
extern void irq12(void); extern void irq13(void); extern void irq14(void);
extern void irq15(void);

static void set_gate(u8 num, u64 handler) {
    idt[num].offset_low  = (u16)(handler & 0xFFFF);
    idt[num].selector    = KERNEL_CS;
    idt[num].ist         = 0;
    idt[num].type_attr   = GATE_INTR;
    idt[num].offset_mid  = (u16)((handler >> 16) & 0xFFFF);
    idt[num].offset_high = (u32)((handler >> 32) & 0xFFFFFFFF);
    idt[num].zero        = 0;
}

void irq_register(u8 vec, irq_handler_t handler) {
    handlers[vec] = handler;
}

u64 irq_ticks(void) {
    return g_ticks;
}

/* ---- panic path -------------------------------------------------------- */

static const char *EXC_NAME[32] = {
    "divide error", "debug", "NMI", "breakpoint",
    "overflow", "bound range", "invalid opcode", "device not available",
    "double fault", "coprocessor overrun", "invalid TSS", "segment not present",
    "stack fault", "general protection", "page fault", "reserved",
    "x87 FP", "alignment check", "machine check", "SIMD FP",
    "virtualization", "control protection", "reserved", "reserved",
    "reserved", "reserved", "reserved", "reserved",
    "hypervisor injection", "VMM communication", "security", "reserved"
};

static void put_hex(u64 v) {
    static const char DIG[] = "0123456789abcdef";
    char buf[19];
    int i;
    buf[0] = '0'; buf[1] = 'x';
    for (i = 0; i < 16; i++) {
        buf[2 + i] = DIG[(v >> ((15 - i) * 4)) & 0xF];
    }
    buf[18] = 0;
    ser_puts(buf);
}

/* A fault with no handler is not recoverable here -- say what and where, then
 * stop. Silently returning would just re-fault forever. */
static void panic(struct interrupt_frame64 *f) {
    const char *name = (f->int_no < 32) ? EXC_NAME[f->int_no] : "unknown";
    ser_puts("\n[mbos] PANIC: ");
    ser_puts(name);
    ser_puts("\n  int_no  "); put_hex(f->int_no);
    ser_puts("\n  err     "); put_hex(f->err_code);
    ser_puts("\n  rip     "); put_hex(f->rip);
    ser_puts("\n  rsp     "); put_hex(f->rsp);
    ser_puts("\n  rflags  "); put_hex(f->rflags);
    ser_puts("\n[mbos] halted.\n");
    con_puts("\n[mbos] PANIC: ");
    con_puts(name);
    con_puts(" -- halted (see serial)\n");
    for (;;) __asm__ volatile ("cli; hlt");
}

/* ---- dispatchers called from idt.S ------------------------------------- */

void isr_handler(struct interrupt_frame64 *frame) {
    if (frame->int_no < IDT_ENTRIES && handlers[frame->int_no]) {
        handlers[frame->int_no]();
        return;
    }
    panic(frame);
}

void irq_handler(struct interrupt_frame64 *frame) {
    u8 irq = (u8)(frame->int_no - 32);

    if (frame->int_no < IDT_ENTRIES && handlers[frame->int_no]) {
        handlers[frame->int_no]();
    }

    /* End Of Interrupt: slave first (if applicable), then master. */
    if (irq >= 8) outb(PIC2_CMD, PIC_EOI);
    outb(PIC1_CMD, PIC_EOI);
}

/* ---- PIC --------------------------------------------------------------- */

/* Remap the PICs to vectors 32..47 so hardware IRQs stop colliding with the
 * CPU exception vectors, then mask everything -- callers opt in per line. */
static void pic_remap(void) {
    outb(PIC1_CMD,  0x11);          /* ICW1: init, expect ICW4 */
    outb(PIC2_CMD,  0x11);
    outb(PIC1_DATA, 0x20);          /* ICW2: master base vector 32 */
    outb(PIC2_DATA, 0x28);          /* ICW2: slave  base vector 40 */
    outb(PIC1_DATA, 0x04);          /* ICW3: slave on IRQ2 */
    outb(PIC2_DATA, 0x02);          /* ICW3: slave cascade identity */
    outb(PIC1_DATA, 0x01);          /* ICW4: 8086 mode */
    outb(PIC2_DATA, 0x01);
    outb(PIC1_DATA, 0xFF);          /* mask all */
    outb(PIC2_DATA, 0xFF);
}

void pic_enable_irq(u8 irq) {
    u16 port = (irq < 8) ? PIC1_DATA : PIC2_DATA;
    u8  bit  = (u8)(irq & 7);
    outb(port, (u8)(inb(port) & ~(1u << bit)));
    if (irq >= 8) {
        /* the cascade line must be open for any slave IRQ to arrive */
        outb(PIC1_DATA, (u8)(inb(PIC1_DATA) & ~(1u << 2)));
    }
}

/* ---- PIT --------------------------------------------------------------- */

static void on_timer(void) {
    g_ticks++;
}

void pit_init(u32 hz) {
    u32 div = PIT_HZ / hz;
    if (div == 0) div = 1;
    if (div > 0xFFFF) div = 0xFFFF;
    outb(PIT_CMD, 0x36);                    /* ch0, lo/hi, rate generator */
    outb(PIT_CH0, (u8)(div & 0xFF));
    outb(PIT_CH0, (u8)((div >> 8) & 0xFF));
}

/* ---- init -------------------------------------------------------------- */

void idt_init(void) {
    mini_memset(idt, 0, sizeof(idt));
    mini_memset(handlers, 0, sizeof(handlers));

    set_gate(0,(u64)(u64)isr0);   set_gate(1,(u64)isr1);
    set_gate(2,(u64)isr2);        set_gate(3,(u64)isr3);
    set_gate(4,(u64)isr4);        set_gate(5,(u64)isr5);
    set_gate(6,(u64)isr6);        set_gate(7,(u64)isr7);
    set_gate(8,(u64)isr8);        set_gate(9,(u64)isr9);
    set_gate(10,(u64)isr10);      set_gate(11,(u64)isr11);
    set_gate(12,(u64)isr12);      set_gate(13,(u64)isr13);
    set_gate(14,(u64)isr14);      set_gate(15,(u64)isr15);
    set_gate(16,(u64)isr16);      set_gate(17,(u64)isr17);
    set_gate(18,(u64)isr18);      set_gate(19,(u64)isr19);
    set_gate(20,(u64)isr20);      set_gate(21,(u64)isr21);
    set_gate(22,(u64)isr22);      set_gate(23,(u64)isr23);
    set_gate(24,(u64)isr24);      set_gate(25,(u64)isr25);
    set_gate(26,(u64)isr26);      set_gate(27,(u64)isr27);
    set_gate(28,(u64)isr28);      set_gate(29,(u64)isr29);
    set_gate(30,(u64)isr30);      set_gate(31,(u64)isr31);

    set_gate(32,(u64)irq0);       set_gate(33,(u64)irq1);
    set_gate(34,(u64)irq2);       set_gate(35,(u64)irq3);
    set_gate(36,(u64)irq4);       set_gate(37,(u64)irq5);
    set_gate(38,(u64)irq6);       set_gate(39,(u64)irq7);
    set_gate(40,(u64)irq8);       set_gate(41,(u64)irq9);
    set_gate(42,(u64)irq10);      set_gate(43,(u64)irq11);
    set_gate(44,(u64)irq12);      set_gate(45,(u64)irq13);
    set_gate(46,(u64)irq14);      set_gate(47,(u64)irq15);

    idtp.limit = (u16)(sizeof(idt) - 1);
    idtp.base  = (u64)(u64)&idt;
    __asm__ volatile ("lidt %0" : : "m"(idtp));
}

/* Bring up the whole interrupt path: IDT, PIC remap, timer at `hz`, keyboard.
 * Interrupts are enabled on return, so everything must be armed before sti. */
void irq_init(u32 hz) {
    idt_init();
    pic_remap();

    irq_register(32, on_timer);
    pit_init(hz);
    pic_enable_irq(0);

    kbd_init();                 /* registers vector 33, drains the controller */
    pic_enable_irq(1);

    __asm__ volatile ("sti");
}
