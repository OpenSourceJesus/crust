# HOSTSIM.md — running firmware at native speed

There are two ways to run a bare-metal image in this tree, and they answer
different questions.

[armulator](https://github.com/crustos/armulator) executes AArch64
instructions one at a time. It knows about registers, exception levels, the
MMU and the GIC, and it is what proves an image **boots**. It manages roughly
**17,000 instructions a second** — about five orders of magnitude slower than
a Jetson.

`hostsim` compiles the application's C for the host and replaces the hardware
underneath it. It executes no ARM at all and proves nothing about code
generation. It runs the same program **about 4,000× faster**, which is what
makes simulating twenty boards driving motors and talking to each other
possible at all.

| | armulator | hostsim |
|---|---|---|
| `kernel_arm64.c`, full run | 17.0 s | **0.0043 s** |
| versus real time | ~80,000× slower | 120–1000× faster |
| 16 boards in lockstep | — | ~100,000 board-ms/sec |
| executes ARM instructions | yes | **no** |
| MMU, exception levels, ESR/FAR | yes | no |
| register-level device behaviour | yes | no |
| CUDA, numpy, sockets, matplotlib | no | yes |

Neither replaces the other. Use armulator when the question is about the
image; use hostsim when the question is about the system.
[`tools/hostsim_difftest.py`](tools/hostsim_difftest.py) exists to check they
still agree about the things both can see.

## Getting started

```sh
python3 tools/hostsim_build.py examples/baremetal/kernel_arm64.c -o /tmp/userapp.so
```

```python
import sys; sys.path.insert(0, "tools")
from hostsim import Sim

sim = Sim("/tmp/userapp.so")
sim.start()
while not sim.finished:
    sim.step_ms(1)
    print(sim.read(), end="")
```

Because this is an ordinary host process, numpy, matplotlib, sockets and CUDA
are all reachable from that loop. That is the whole point of the arrangement.

## How it works

The application is compiled by `gcc -O3` and runs on its own thread inside a
shared object. Everything it calls that would touch hardware is implemented in
[`hostsim/hostsim.c`](hostsim/hostsim.c) instead: the console, the architected
timer, the interrupt counters, the MMU flags, the motor and sensor values.

This is the same idea as Zephyr's `native_posix` or NuttX's simulator target:
keep the application, replace the hardware.

**Virtual time is driven from outside.** The application's delay loops spin on
`timer_count()`, which blocks until the controlling process grants more time
with `step()`. Nothing advances on its own. That makes runs bit-for-bit
repeatable regardless of host load, and it is what lets several boards be
stepped in lockstep — `Fleet` advances each in turn, so they share one clock.

Each `Sim` copies the shared object before loading it. `dlopen` returns the
same handle for the same path and the application's state lives in ordinary
globals, so two boards loaded from one file would otherwise share a console
and a clock.

## The seam

An application may use exactly what
[`hostsim/hostsim.h`](hostsim/hostsim.h) declares. Reaching past it — inline
assembly, a system register, a hard-coded peripheral address — will not
compile, and that is deliberate: a silent divergence between what the host
runs and what the board runs is the one failure this arrangement could
introduce that nothing else would catch.

```c
void uart_init(void);
void uart_puts(char *s);
void uart_puthex(unsigned long v);
void uart_putdec(long v);
int  uart_rx_ready(void);
int  uart_getc(void);              /* -1 when nothing has arrived */

int  link_send(const char *data, unsigned long n);   /* 0 if accepted */
long link_recv(char *out, unsigned long max);        /* -1 if nothing */

int  accel_infer(const unsigned char *frame, long *score_out);
int  sim_frame_ready(void);
const unsigned char *sim_frame_data(void);
void sim_frame_consume(void);

void mmu_init(void);
void mmu_enable(void);
void mmu_report(void);
unsigned long read_sctlr(void);

void exc_expect(int n);
int  exc_taken(void);              /* cumulative count, as on hardware */

void irq_init(void);
void irq_enable(void);
void irq_disable(void);
void timer_start(int hz);
unsigned long ticks(void);
unsigned long timer_count(void);
unsigned long timer_freq(void);
```

The seam is drawn at **values**, not registers: `sim_motor_write(duty)`, not a
PWM duty register. That is right for the system question and wrong for the
driver question. Test drivers against armulator, where the registers exist.

`link_send` returns a status because it can fail, and firmware that ignores it
loses messages exactly as it would on a real link. A simulation that cannot
drop messages will never reveal that bug.

## Many boards

```python
from hostsim import Fleet, Sim

fleet = Fleet([Sim(so, name=n) for n in ("axis-x", "axis-y", "axis-z")])
fleet.start()
for _ in range(8000):
    fleet.step_ms(1)          # all boards advance, then messages are routed
```

`Fleet.deliver()` moves messages between boards after every step, once all of
them have reached the same virtual time. A message sent during a step arrives
at the start of the next one — that one-step latency is deliberate, roughly
what a real link costs, and it stops results depending on the order boards
happen to be listed in.

Delivery defaults to broadcast. Pass `router=` for anything else:

```python
def point_to_point(fleet, sender, message):
    return [(fleet.by_name("supervisor"), message)]

fleet = Fleet(sims, router=point_to_point)
```

Messages that reach no recipient land in `fleet.undelivered` rather than
disappearing. The controlling process can act as a server itself with
`sim.link_push(...)` and `sim.link_pop()`, which is how an external service,
a socket, or a test harness joins in.

## Joining a real network

A fleet on its own is a closed world.
[`tools/hostsim_net.py`](tools/hostsim_net.py) joins it to one that is not:

```python
from hostsim_net import SocketBridge

bridge = SocketBridge.connect("telemetry.staging", 9000, name="collector")
fleet = Fleet(sims, endpoints=[bridge])
```

A bridge is just another participant in routing — it exposes the same
`link_pop_all` and `link_push` a board does, so a router addresses it the same
way and the service on the far end never learns the boards are simulated. That
is what lets a fleet drop into an existing dev-ops test unmodified.

Messages are framed with a four-byte big-endian length, since link traffic is
whole messages and a TCP stream is not. `Newline` is provided for
line-oriented peers, and `codec=` takes anything with `encode`/`decode`.
`SocketBridge.listen(port)` accepts a peer instead of dialling one, and
`EchoService` is a throwaway peer for tests.

**A bridged run is not deterministic** in the way a closed fleet is. The fleet
runs on virtual time and the socket does not, so a bridge is polled and never
blocked on: whatever arrived by the end of a step is delivered at the next
one, and a slow peer can never stall the clock. Assert on *what* was
exchanged, not on exactly when — and **drain before asserting**, or the count
is short by however many messages are still in flight.
[`examples/hostsim/vision_demo.py`](examples/hostsim/vision_demo.py) does
this, and without the drain it reports 597 of 599.

## Accelerators, and the Jetson case

A Jetson exists for the GPU beside the CPU, and firmware running an inference
every frame is exactly what an instruction emulator cannot study: armulator
would need roughly a day per simulated second, and it models no GPU to run the
inference on. Compiled for the host, the same firmware reaches whatever the
host has.

The seam is [`hostsim/accel.h`](hostsim/accel.h) — one call that takes a frame
and returns a classification, which is the shape of the seam on a real board
too:

```c
int accel_infer(const unsigned char *frame, long *score_out);
int accel_available(void);
const char *accel_backend(void);
int accel_selftest(void);
```

Two implementations. [`hostsim/accel.c`](hostsim/accel.c) is plain C: always
builds, always tested, needs no GPU.
[`hostsim/accel_cuda.cu`](hostsim/accel_cuda.cu) is the same arithmetic as a
kernel, selected with `--cuda`:

```sh
python3 tools/hostsim_build.py examples/hostsim/vision_node.c --cuda -o /tmp/vision.so
```

All the arithmetic is integer, so the two paths must agree *exactly* rather
than within a tolerance. `accel_selftest()` runs both over generated frames
and counts disagreements; it must return 0.

> **The CUDA path has never been run.** There is no GPU and no CUDA toolkit in
> the environment this was written in, so `accel_cuda.cu` compiles nowhere
> here and is a sketch of the integration rather than working code. `--cuda`
> refuses outright when `nvcc` is missing rather than falling back, because a
> silent fallback would look exactly like a GPU build that was simply slow.

Frames come from the controlling process, so they can be a recorded dataset, a
generator or a real camera, none of which has to be modelled on the board.
There is **one frame of slack, not a queue**: an uncollected frame is
overwritten as a camera DMAing into a double buffer would overwrite it, which
is why `vision_node.c` counts dropped frames rather than assuming it sees them
all.

## Injecting faults

Each of these is a nuisance to arrange on a bench and one call here.

```python
sim.fault_link_down(True)        # everything the board sends is lost
sim.fault_link_drop_every(4)     # one message in four
sim.fault_encoder_stuck(True)    # shaft turns, sensor stops reporting
sim.fault_encoder_bias(50)       # every reading offset
```

The sensor faults live in the model rather than the plant, because they are
faults in the *sensor*: a stuck encoder still reads plausibly while the shaft
turns, which is exactly what makes it hard to diagnose.
[`examples/hostsim/fleet_demo.py`](examples/hostsim/fleet_demo.py) shows both
signatures — an axis stalled against a seized bearing, still commanding
current with no motion, and an axis whose reported position diverges from its
real one while the controller keeps believing the sensor.

## Testing

```sh
python3 tools/hostsim_test.py        # 52 checks, needs only gcc
python3 tools/hostsim_difftest.py    # hostsim vs armulator, skips without it
```

The difftest runs the same image both ways and compares what both are supposed
to agree about: the computation, the fault count, the tick counts, and that
none were spurious. It does **not** compare raw console text — armulator
prints ESR and FAR for each fault and hostsim has neither, so a text
comparison would fail on differences that are the reason both exist, and
everyone would learn to ignore the test.

If it ever fails, either the host model has drifted from the driver it stands
in for, or a real behavioural change landed on only one path.

## What this does not do

- **No ARM is executed.** Code generation, instruction selection, the boot
  sequence and the exception vectors are all untested here.
- **No MMU.** `mmu_enable()` sets a flag. There is no translation, no page
  table walk, no ESR or FAR.
- **Fault injection is not exception emulation.** A deliberate store to an
  unmapped address is caught with a `SIGSEGV` handler that maps a page and
  re-executes the instruction. The application observes what it observes on
  hardware — the access faulted, `exc_taken()` counts it, execution continues
  — but there is no vector table and no exception level. An image that
  inspects `ESR_EL1` belongs under armulator.
- **No register-level device behaviour.** The drivers in `baremetal64/` are
  not used; `hostsim.c` stands in for them.
- **The CUDA accelerator is unverified.** See above. The C path is tested;
  the kernel is not.
- **Not run on physical hardware.** Same caveat as everything else here.

`examples/hostsim/motor_node.c` deliberately does *not* build for a Jetson:

```
rlink: undefined reference to: sim_encoder_read, sim_motor_write, sim_target
```

That is the correct outcome. The control logic is portable, but the motor
accessors need a real PWM and quadrature driver that does not exist in this
tree. A missing driver should fail at the link rather than be quietly
substituted.
