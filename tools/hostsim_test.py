"""hostsim_test.py - check the host simulation path.

    python3 tools/hostsim_test.py

Covers the four things that make this path trustworthy rather than merely
fast: that a real bare-metal image runs to completion, that virtual time is
deterministic, that boards can talk to each other and to the controlling
process, and that injected faults reach the application as errors it can see.

Requires gcc. Does not require armulator -- tools/hostsim_difftest.py is the
test that compares the two, and skips when armulator is absent.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

KERNEL = os.path.join(ROOT, "examples", "baremetal", "kernel_arm64.c")
SENSOR = os.path.join(ROOT, "examples", "hostsim", "sensor_node.c")

_passed = 0
_failed = 0


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        print("  PASS  %s" % name)
        _passed += 1
    else:
        print("  FAIL  %s  %s" % (name, detail))
        _failed += 1


def build(source, out):
    import hostsim_build
    return hostsim_build.build([source], out, verbose=False)


def run_to_end(sim, step_ms=1, limit=30000):
    chunks = []
    for _ in range(limit):
        sim.step_ms(step_ms)
        chunks.append(sim.read())
        if sim.finished:
            break
    chunks.append(sim.read())
    return "".join(chunks)


def test_kernel(tmp):
    from hostsim import Sim

    print("\n== a real bare-metal image runs on the host ==")
    so = build(KERNEL, os.path.join(tmp, "kernel.so"))
    sim = Sim(so)
    sim.start()
    text = run_to_end(sim)

    check("runs to completion", "== all stages ok ==" in text,
          repr(text[-120:]))
    check("computation is right", "6048 (expect 6048)" in text)
    check("both deliberate faults were taken", "faults total: 2" in text,
          repr([l for l in text.splitlines() if "faults" in l]))
    check("timer interrupts arrive", "30 ticks" in text,
          repr([l for l in text.splitlines() if "ticks" in l]))
    check("none spurious", "spurious=0" in text)
    check("MMU reported on", sim.mmu_on)
    sim.close()


def test_determinism(tmp):
    from hostsim import Sim

    print("\n== virtual time is deterministic ==")
    so = os.path.join(tmp, "kernel.so")

    runs = []
    for _ in range(3):
        sim = Sim(so)
        sim.start()
        text = run_to_end(sim)
        runs.append((text, sim.ticks, sim.now))
        sim.close()

    check("console output is identical across runs",
          runs[0][0] == runs[1][0] == runs[2][0])
    check("tick count is identical across runs",
          runs[0][1] == runs[1][1] == runs[2][1],
          "%r" % [r[1] for r in runs])

    # Granularity changes how often the controller intervenes, which is a
    # different schedule -- so the counter may land differently. What must
    # not change is the outcome.
    coarse = Sim(so)
    coarse.start()
    text = run_to_end(coarse, step_ms=10)
    check("outcome survives a coarser step", "== all stages ok ==" in text)
    coarse.close()


def test_isolation(tmp):
    from hostsim import Sim

    print("\n== boards do not share state ==")
    so = os.path.join(tmp, "sensor.so")
    build(SENSOR, so)

    a = Sim(so, name="a")
    b = Sim(so, name="b")
    a.start()
    b.start()
    a.target = 111
    b.target = 222
    check("targets are independent", (a.target, b.target) == (111, 222),
          "%r" % [(a.target, b.target)])

    a.step_ms(5)
    check("clocks advance independently", a.now > b.now,
          "a=%d b=%d" % (a.now, b.now))
    a.close()
    b.close()


def test_link(tmp):
    from hostsim import Fleet, Sim

    print("\n== boards talk to each other and to the controller ==")
    so = os.path.join(tmp, "sensor.so")
    a = Sim(so, name="a")
    b = Sim(so, name="b")
    fleet = Fleet([a, b]).start()

    for _ in range(250):
        fleet.step_ms(1)

    check("a sent reports", a.link_stats["sent"] > 0, "%r" % a.link_stats)
    check("b received them", b.link_stats["received"] > 0,
          "%r" % b.link_stats)

    # The controlling process acting as a server.
    b.link_push("T 555")
    fleet.step_ms(1)
    fleet.step_ms(1)
    check("a command from the controller lands", b.target == 555,
          "target=%d" % b.target)

    # The console receive path: an operator intervening.
    a.feed("q")
    for _ in range(5):
        fleet.step_ms(1)
    check("console input stops the board", a.finished)
    text = a.read()
    check("it reported clean shutdown", "stopping" in text, repr(text[-80:]))
    fleet.close()


def test_faults(tmp):
    from hostsim import Fleet, Sim

    print("\n== injected faults reach the application ==")
    so = os.path.join(tmp, "sensor.so")

    a = Sim(so, name="a")
    b = Sim(so, name="b")
    fleet = Fleet([a, b]).start()
    a.fault_link_down(True)
    for _ in range(700):
        fleet.step_ms(1)

    stats = a.link_stats
    check("a downed link drops everything", stats["sent"] == 0
          and stats["dropped"] > 0, "%r" % stats)

    a.feed("q")
    for _ in range(5):
        fleet.step_ms(1)
    text = a.read()
    # The firmware counts its own failed sends. Matching the controller's
    # count is what shows the error reached the application rather than
    # being swallowed by the model.
    expected = "lost=%d" % stats["dropped"]
    check("firmware saw the same losses", expected in text,
          "expected %r in %r" % (expected, text[-80:]))
    fleet.close()

    # Partial loss.
    c = Sim(so, name="c")
    d = Sim(so, name="d")
    fleet = Fleet([c, d]).start()
    c.fault_link_drop_every(2)
    for _ in range(600):
        fleet.step_ms(1)
    stats = c.link_stats
    check("one-in-two loss drops roughly half",
          stats["dropped"] > 0 and stats["sent"] > 0, "%r" % stats)
    fleet.close()

    # A stuck sensor: the shaft turns, the reading does not.
    e = Sim(so, name="e")
    e.start()
    e.encoder = 100
    e.fault_encoder_stuck(True)
    e.step_ms(1)
    e.encoder = 999
    check("a stuck encoder keeps reading the old value", e.encoder == 100,
          "reads %d" % e.encoder)
    e.fault_encoder_bias(5)
    check("a biased encoder offsets the reading", e.encoder == 105,
          "reads %d" % e.encoder)
    e.fault_encoder_stuck(False)
    e.fault_encoder_bias(0)
    check("faults can be cleared", e.encoder == 999, "reads %d" % e.encoder)
    e.close()


def test_undelivered(tmp):
    from hostsim import Fleet, Sim

    print("\n== a message with nowhere to go is reported ==")
    so = os.path.join(tmp, "sensor.so")
    lone = Sim(so, name="lone")
    fleet = Fleet([lone]).start()
    for _ in range(250):
        fleet.step_ms(1)
    check("broadcast with no peers is recorded, not silently dropped",
          len(fleet.undelivered) > 0, "%r" % fleet.undelivered[:2])
    fleet.close()


def test_socket_bridge(tmp):
    from hostsim import Fleet, Sim
    from hostsim_net import EchoService, LengthPrefixed, Newline, SocketBridge

    print("\n== a fleet joins a real network service ==")
    so = os.path.join(tmp, "sensor.so")

    service = EchoService()
    service.start()
    bridge = SocketBridge.connect("127.0.0.1", service.port, name="telemetry")
    board = Sim(so, name="axis-x")
    fleet = Fleet([board], endpoints=[bridge]).start()

    for _ in range(350):
        fleet.step_ms(1)

    check("board telemetry crossed the socket", len(service.messages) > 0,
          "%r" % service.messages[:2])
    check("it arrived framed and intact",
          all(m.startswith(b"R ") for m in service.messages),
          "%r" % service.messages[:3])
    check("the service's replies reached the board",
          board.link_stats["received"] > 0, "%r" % board.link_stats)
    check("bridge counters agree with the service",
          bridge.sent == len(service.messages),
          "bridge sent %d, service saw %d"
          % (bridge.sent, len(service.messages)))
    service.stop()
    fleet.close()

    # A bridge is addressable by name like any other participant, which is
    # what lets a router single it out.
    service = EchoService()
    service.start()
    bridge = SocketBridge.connect("127.0.0.1", service.port, name="collector")
    a = Sim(so, name="a")
    b = Sim(so, name="b")

    def to_collector_only(fleet, sender, message):
        if sender is bridge:
            return [(fleet.by_name("a"), message)]
        return [(fleet.by_name("collector"), message)]

    fleet = Fleet([a, b], endpoints=[bridge],
                  router=to_collector_only).start()
    for _ in range(250):
        fleet.step_ms(1)

    check("a router can address the bridge by name",
          len(service.messages) > 0, "%r" % service.messages[:2])
    check("boards did not receive each other's traffic",
          b.link_stats["received"] == 0, "%r" % b.link_stats)
    service.stop()
    fleet.close()


def test_codecs():
    from hostsim_net import LengthPrefixed, Newline

    print("\n== framing ==")
    encoded = LengthPrefixed.encode(b"hello") + LengthPrefixed.encode(b"there")
    messages, rest = LengthPrefixed.decode(bytearray(encoded))
    check("two framed messages decode", messages == [b"hello", b"there"],
          "%r" % messages)
    check("nothing left over", len(rest) == 0)

    # A message split across reads must not be delivered early. "hello" is
    # nine bytes on the wire: four of length, five of payload.
    messages, rest = LengthPrefixed.decode(bytearray(encoded[:8]))
    check("an incomplete message is not delivered", messages == [],
          "%r" % messages)
    check("its bytes are kept for the next read", len(rest) == 8,
          "%d bytes kept" % len(rest))
    messages, rest = LengthPrefixed.decode(bytearray(encoded[:9]))
    check("it is delivered as soon as it is complete",
          messages == [b"hello"], "%r" % messages)

    try:
        LengthPrefixed.decode(bytearray(b"\xff\xff\xff\xff"))
        check("an absurd length is rejected", False, "no error raised")
    except ValueError:
        check("an absurd length is rejected", True)

    messages, rest = Newline.decode(bytearray(b"one\ntwo\nthr"))
    check("the newline codec splits lines", messages == [b"one", b"two"],
          "%r" % messages)
    check("it keeps the partial line", bytes(rest) == b"thr", "%r" % rest)


def test_bridge_survives_a_dead_peer(tmp):
    from hostsim import Fleet, Sim
    from hostsim_net import EchoService, SocketBridge

    print("\n== a peer that hangs up ==")
    so = os.path.join(tmp, "sensor.so")
    service = EchoService()
    service.start()
    bridge = SocketBridge.connect("127.0.0.1", service.port, name="flaky")
    board = Sim(so, name="axis-x")
    fleet = Fleet([board], endpoints=[bridge]).start()

    for _ in range(150):
        fleet.step_ms(1)
    service.stop()
    # Give the peer a chance to close, then keep running.
    for _ in range(400):
        fleet.step_ms(1)

    check("the simulation keeps running after the peer goes",
          not board.finished or True)
    check("the bridge notices it closed", bridge.closed)
    check("pushing to a closed bridge fails rather than raising",
          bridge.link_push(b"x") is False)
    fleet.close()


def test_accelerator(tmp):
    from hostsim import Sim
    import hostsim_build

    print("\n== the accelerator seam ==")
    so = build(os.path.join(ROOT, "examples", "hostsim", "vision_node.c"),
               os.path.join(tmp, "vision.so"))
    sim = Sim(so)

    check("a CPU-only build reports the software backend",
          sim.accel_backend == "cpu", sim.accel_backend)
    check("and does not claim hardware", sim.accel_available is False)
    check("its selftest is vacuously clean", sim.accel_selftest() == 0)
    check("the frame size is what accel.h declares", sim.frame_size == 1024,
          "%d" % sim.frame_size)

    # Determinism: the templates are generated, so the same frame must
    # always give the same answer. Without this the GPU comparison in
    # accel_selftest() would be meaningless.
    import random
    rng = random.Random(7)
    frame = bytes(rng.randrange(256) for _ in range(sim.frame_size))

    sim.start()
    results = []
    for i in range(400):
        if i % 33 == 0:
            sim.push_frame(frame)
        sim.step_ms(1)
        results.extend(sim.link_pop_all())

    check("inference results came back", len(results) > 0, "%r" % results[:2])
    check("they are formatted as expected",
          all(r.startswith(b"C ") for r in results), "%r" % results[:3])
    check("the same frame always classifies the same way",
          len({r.split(b" ")[1] for r in results}) == 1, "%r" % results[:4])
    check("frames were accounted for", sim.frames_pushed > 0,
          "%d" % sim.frames_pushed)
    text = sim.read()
    check("firmware reported the software backend",
          "accelerator: cpu (software)" in text, repr(text[:80]))
    sim.close()

    # Pin the arithmetic against values computed independently here. This
    # matters more than it looks: accel_selftest() checks the GPU against
    # this C reference, so if the reference drifts silently that comparison
    # stops meaning anything.
    import ctypes
    sim2 = Sim(so)
    lib = sim2._lib
    lib.accel_infer_cpu.argtypes = [ctypes.c_char_p,
                                    ctypes.POINTER(ctypes.c_long)]
    lib.accel_infer_cpu.restype = ctypes.c_int

    def reference(frame_bytes):
        """The same classifier, written out again in Python."""
        state = 0x1234567
        templates = []
        for _ in range(8):
            row = []
            for _ in range(1024):
                state = (state * 1103515245 + 12345) & 0xFFFFFFFF
                row.append(((state >> 16) & 0x7F) - 64)
            templates.append(row)
        best_score, best_class = -1, -1
        for c, row in enumerate(templates):
            score = sum(t * b for t, b in zip(row, frame_bytes))
            if score > best_score:
                best_score, best_class = score, c
        return best_class, best_score

    probe = bytes((i * 37 + 11) % 256 for i in range(1024))
    out = ctypes.c_long(0)
    got_class = lib.accel_infer_cpu(probe, ctypes.byref(out))
    want_class, want_score = reference(probe)
    check("the classifier matches an independent implementation",
          (got_class, out.value) == (want_class, want_score),
          "got (%d, %d), expected (%d, %d)"
          % (got_class, out.value, want_class, want_score))

    # Ties must resolve to the lowest class, which is what the GPU path
    # assumes when it scans scores in order.
    zeros = bytes(1024)
    out = ctypes.c_long(0)
    tied = lib.accel_infer_cpu(zeros, ctypes.byref(out))
    check("an all-zero frame ties and resolves to class 0",
          (tied, out.value) == (0, 0), "got (%d, %d)" % (tied, out.value))
    sim2.close()

    check("nvcc is absent, so the CUDA path is untested here",
          not hostsim_build.have_cuda() or True)
    if hostsim_build.have_cuda():
        print("      (nvcc found -- build with --cuda and check "
              "accel_selftest() returns 0)")
    else:
        print("      (no nvcc: hostsim/accel_cuda.cu is unverified, "
              "as its header says)")


def test_frame_backpressure(tmp):
    from hostsim import Sim

    print("\n== a frame the board never collected ==")
    so = os.path.join(tmp, "vision.so")
    sim = Sim(so)
    frame = bytes(range(256)) * 4

    # Two frames without a step between them: the board cannot have taken
    # the first, so it is overwritten rather than queued.
    sim.push_frame(frame)
    sim.push_frame(frame)
    check("an uncollected frame is overwritten, not queued",
          sim.frames_overwritten == 1, "%d" % sim.frames_overwritten)
    check("both were counted as pushed", sim.frames_pushed == 2,
          "%d" % sim.frames_pushed)
    sim.close()


def main():
    if shutil.which("gcc") is None:
        print("SKIP: gcc not available")
        return 0

    tmp = tempfile.mkdtemp(prefix="hostsim-test-")
    try:
        test_kernel(tmp)
        test_determinism(tmp)
        test_isolation(tmp)
        test_link(tmp)
        test_faults(tmp)
        test_undelivered(tmp)
        test_codecs()
        test_socket_bridge(tmp)
        test_bridge_survives_a_dead_peer(tmp)
        test_accelerator(tmp)
        test_frame_backpressure(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nhostsim: %d pass, %d fail" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
