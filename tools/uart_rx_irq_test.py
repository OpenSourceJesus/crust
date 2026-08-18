#!/usr/bin/env python3
"""Check UART receive interrupts -- the first non-timer interrupt source.

The timer is the easy interrupt: it is private to the core, needs no routing,
and every board reaches it the same way. A UART is the opposite, and that is
what makes it worth testing:

  * on virt it is **SPI 33**, a shared peripheral the GIC distributor must be
    told to route to a core;
  * on a Pi 3 it is **GPU interrupt 57**, which must pass *two* controllers --
    enabled in the legacy controller's second bank at `0x3F00B200`, then
    routed to a core by the ARM local block at `0x40000000` -- and arrives as
    an undifferentiated "GPU" bit that has to be decoded a second time.

Both are driven through the same `intc_*` seam, so the program below is
identical on both boards.

**Why input is sent only after the guest says READY.** Piping input in at
launch is unreliable, and not because of the interrupt path: `uart_init()`
clears CR to reconfigure the port, which flushes the receive FIFO. Characters
that qemu delivered before that point are simply gone. Sending after the guest
has initialised its UART removes the race, and a flaky test here would be
worse than none -- it would train a reader to rerun it rather than believe it.

    python3 tools/uart_rx_irq_test.py
"""
import os
import re
import select
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SEND = "abcXYZ123"

PROGRAM = r"""
void uart_init(void);
void uart_puts(char *s);
void uart_putdec(long v);
void uart_puthex(unsigned long v);

void irq_init(void);
void irq_enable(void);
void irq_disable(void);

void rx_start(int echo);
void rx_stop(void);
unsigned long rx_count(void);
unsigned long rx_interrupts(void);
int rx_last_char(void);

unsigned long timer_count(void);
unsigned long timer_freq(void);
unsigned long unexpected(void);
unsigned long spurious(void);
unsigned long irq_pending(void);

static void wait_ms(unsigned long ms)
{
    unsigned long f = timer_freq();
    unsigned long t0 = timer_count();
    while (timer_count() - t0 < (f / 1000) * ms) {
    }
}

void kmain(void)
{
    unsigned long before;

    uart_init();
    irq_init();
    rx_start(0);

    /* Deliberately still masked: nothing may be received yet. */
    irq_disable();
    uart_puts("READY\n");

    wait_ms(700);
    before = rx_count();
    uart_puts("MASKEDRX=");
    uart_putdec((long)before);
    uart_puts("\n");
    /* The core must be *seeing* the request even though it cannot take it.
     * Zero here would mean the interrupt never reached the core at all, and
     * the counts below would prove nothing about routing. */
    uart_puts("ISRMASKED=");
    uart_puthex(irq_pending());
    uart_puts("\n");

    irq_enable();
    wait_ms(700);

    uart_puts("RXCHARS=");
    uart_putdec((long)rx_count());
    uart_puts("\nRXIRQS=");
    uart_putdec((long)rx_interrupts());
    uart_puts("\nLAST=");
    uart_putdec((long)rx_last_char());
    uart_puts("\nSPURIOUS=");
    uart_putdec((long)spurious());
    uart_puts("\nUNEXPECTED=");
    uart_putdec((long)unexpected());
    uart_puts("\n");

    rx_stop();
    irq_disable();
    uart_puts("DONE\n");
}
"""


def run_guest(elf, machine, cpu, send, timeout=90):
    """Boot the image, wait for READY, then type `send` at the serial port."""
    cmd = ["qemu-system-aarch64", "-M", machine, "-cpu", cpu,
           "-nographic", "-nodefaults", "-serial", "mon:stdio",
           "-kernel", elf]
    # Binary pipes and chunked reads. Reading one character at a time through
    # a text wrapper truncated the output unpredictably -- the guest had
    # finished and its last lines were still in flight.
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, bufsize=0)
    out = b""
    sent = False
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            r, _, _ = select.select([p.stdout], [], [], 0.2)
            if r:
                chunk = os.read(p.stdout.fileno(), 4096)
                if not chunk:
                    break
                out += chunk
            if not sent and b"READY" in out:
                # The guest has configured its UART and enabled receive
                # interrupts at the controller, but not yet at the core.
                sent = True
                p.stdin.write(send.encode())
                p.stdin.flush()
            if b"DONE" in out:
                break
            if p.poll() is not None and not r:
                break
    finally:
        p.kill()
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    return out.decode("utf-8", "replace")
    return out


def main():
    if subprocess.run(["which", "qemu-system-aarch64"],
                      capture_output=True).returncode != 0:
        print("SKIP: qemu-system-aarch64 not installed")
        return 0

    import baremetal_arm64 as bm

    boards = [b for b in sorted(bm.BOARDS)
              if bm.BOARDS[b]["irq"] and bm.BOARDS[b]["machine"]]
    total_pass = total_fail = 0

    for board in boards:
        prof = bm.BOARDS[board]
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "rxprog.c")
            with open(src, "w") as f:
                f.write(PROGRAM)
            elf = os.path.join(d, "rx.elf")
            try:
                bm.build([src], elf, os.path.join(d, "obj"), board=board)
            except Exception as e:
                print("  FAIL  [%s] build: %s" % (board, e))
                total_fail += 1
                continue
            out = run_guest(elf, prof["machine"], prof["cpu"], SEND)

        clean = out.replace("\r", "")
        if "DONE" not in clean:
            print("  FAIL  [%s] program did not run to completion" % board)
            for line in clean.split("\n")[-6:]:
                if line.strip():
                    print("        | " + line)
            total_fail += 1
            continue

        def val(key):
            m = re.search(r"\b%s(0x[0-9a-f]+|-?\d+)" % key, clean)
            if not m:
                return None
            t = m.group(1)
            return int(t, 16) if t.startswith("0x") else int(t)

        checks = []
        checks.append(("nothing received while PSTATE.I is set",
                       val("MASKEDRX=") == 0,
                       "masked_rx=%s" % val("MASKEDRX=")))
        checks.append(("controller routes the UART to the core",
                       (val("ISRMASKED=") or 0) != 0,
                       "ISR_EL1=%s" % hex(val("ISRMASKED=") or 0)))
        got = val("RXCHARS=")
        checks.append(("every character sent is received",
                       got == len(SEND),
                       "rx=%s of %d" % (got, len(SEND))))
        checks.append(("at least one receive interrupt was taken",
                       (val("RXIRQS=") or 0) > 0,
                       "rx_irqs=%s" % val("RXIRQS=")))
        checks.append(("the last byte matches what was sent",
                       val("LAST=") == ord(SEND[-1]),
                       "last=%s want=%d" % (val("LAST="), ord(SEND[-1]))))
        checks.append(("no spurious interrupts",
                       val("SPURIOUS=") == 0,
                       "spurious=%s" % val("SPURIOUS=")))
        checks.append(("no interrupts from unexpected sources",
                       val("UNEXPECTED=") == 0,
                       "unexpected=%s" % val("UNEXPECTED=")))

        for name, ok, detail in checks:
            if ok:
                print("  PASS  [%-6s] %-42s %s" % (board, name, detail))
                total_pass += 1
            else:
                print("  FAIL  [%-6s] %-42s %s" % (board, name, detail))
                total_fail += 1

    print("\nuart receive interrupts across %d boards: %d pass, %d fail"
          % (len(boards), total_pass, total_fail))
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
