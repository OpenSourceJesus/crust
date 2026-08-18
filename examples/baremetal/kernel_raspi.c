void uart_init(void); void uart_puts(char *s); void uart_puthex(unsigned long v);
void uart_putdec(long v);
void mmu_init(void); void mmu_enable(void); unsigned long read_sctlr(void);
void exc_expect(int n); int exc_taken(void);
unsigned long current_el(void);

void kmain(void) {
    int i;
    uart_init();
    uart_puts("\n== crust bare metal: Raspberry Pi ==\n");
    uart_puts("[el] running at EL"); uart_putdec((long)current_el()); uart_puts("\n");
    uart_puts("[compute] fib:");
    { int a=0,b=1,t; for(i=0;i<10;i=i+1){uart_puts(" ");uart_putdec((long)a);t=a+b;a=b;b=t;} }
    uart_puts("\n[except] forcing a fault\n");
    exc_expect(1);
    { volatile unsigned long *bad=(volatile unsigned long *)0xdeadbe000UL; *bad=1; }
    uart_puts("[except] recovered, faults="); uart_putdec((long)exc_taken()); uart_puts("\n");
    uart_puts("[mmu] enabling\n");
    mmu_init(); mmu_enable();
    uart_puts("[mmu] SCTLR="); uart_puthex(read_sctlr());
    uart_puts(" MMU="); uart_putdec((long)(read_sctlr()&1)); uart_puts("\n");
    { static unsigned long a[32]; unsigned long s=0;
      for(i=0;i<32;i=i+1)a[i]=(unsigned long)i;
      for(i=0;i<32;i=i+1)s=s+a[i];
      uart_puts("[mmu] sum="); uart_putdec((long)s); uart_puts(" (expect 496)\n"); }
    uart_puts("== pi ok ==\n");
}
