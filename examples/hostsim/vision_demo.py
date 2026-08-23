"""vision_demo.py - a fleet of inference nodes reporting to a real service.

    python3 examples/hostsim/vision_demo.py

Three Jetson-style nodes each run a classifier on every frame and report the
result. The reports leave over a **real TCP socket** to a collector process
that has no idea the boards are simulated -- which is the point: the collector
could just as well be the telemetry service from a staging environment, and
the fleet can be dropped into an existing dev-ops test without either side
being modified.

Halfway through, one node's link is cut. The firmware notices, because
`link_send` returns a status, and reports its own loss count at the end. The
collector sees the reports stop. Both numbers are checked against each other
here, which is the sort of assertion a real integration test would make.

Scale, for context. Each node runs inference at 30 frames a second for eight
simulated seconds: about 240 inferences per node, and the whole run finishes
in around a second of wall clock. Under armulator the same thing is roughly a
day per simulated second, and armulator models no GPU to run the inference on.

The accelerator here is the plain C in hostsim/accel.c. With a CUDA toolkit
and a GPU, `--cuda` swaps in hostsim/accel_cuda.cu instead and nothing else
changes -- though that path has never been run; see the warning at the top of
that file.
"""

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import hostsim_build                                    # noqa: E402
from hostsim import Fleet, Sim                          # noqa: E402
from hostsim_net import EchoService, SocketBridge       # noqa: E402

SO = "/tmp/vision.so"
DURATION_MS = 8000
FRAME_INTERVAL_MS = 33          # ~30 fps
CUT_AT_MS = DURATION_MS // 2


def main():
    hostsim_build.build([os.path.join(HERE, "vision_node.c")], SO,
                        verbose=False)

    # Stand in for the telemetry service. Swap this for a real host and port
    # and nothing else in the file changes.
    collector = EchoService(prefix=b"ok:")
    collector.start()
    bridge = SocketBridge.connect("127.0.0.1", collector.port,
                                  name="collector")

    names = ["cam-north", "cam-south", "cam-east"]
    sims = [Sim(SO, name=n) for n in names]

    # Everything a board sends goes to the collector; replies go nowhere,
    # so boards never see each other's traffic.
    def to_collector(fleet, sender, message):
        if sender is bridge:
            return None
        return [(bridge, message)]

    fleet = Fleet(sims, endpoints=[bridge], router=to_collector).start()

    print("[vision] accelerator: %s (%s)"
          % (sims[0].accel_backend,
             "hardware" if sims[0].accel_available else "software"))
    if sims[0].accel_available:
        mismatches = sims[0].accel_selftest()
        print("[vision] accelerator selftest: %d mismatches" % mismatches)

    rng = random.Random(11)
    frame_size = sims[0].frame_size
    # A distinct scene per camera, so the classifications differ.
    scenes = [bytes(rng.randrange(256) for _ in range(frame_size))
              for _ in names]

    cut = False
    for elapsed in range(0, DURATION_MS, 1):
        if elapsed % FRAME_INTERVAL_MS == 0:
            for sim, scene in zip(sims, scenes):
                sim.push_frame(scene)

        if elapsed == CUT_AT_MS and not cut:
            cut = True
            sims[1].fault_link_down(True)
            print("[vision] t=%dms  cam-south link cut" % elapsed)

        fleet.step_ms(1)
        for sim in sims:
            sim.read()

    # Drain before asserting. When the loop ends there are still reports in
    # the bridge's buffer and bytes in the socket, so a count taken here
    # would be short by however many were in flight -- which is a real
    # integration bug, not a simulation artefact, and worth doing properly.
    import time
    claimed = sum(s.link_stats["sent"] for s in sims)
    for _ in range(200):
        fleet.step_ms(1)
        if len(collector.messages) >= claimed:
            break
        time.sleep(0.002)

    print("\n%-11s %8s %8s %9s %9s"
          % ("board", "frames", "dropped", "sent", "lost"))
    for sim in sims:
        stats = sim.link_stats
        print("%-11s %8d %8d %9d %9d"
              % (sim.name, sim.frames_pushed, sim.frames_overwritten,
                 stats["sent"], stats["dropped"]))

    print("\n[vision] collector received %d reports over TCP"
          % len(collector.messages))

    # The assertion an integration test would make: what the collector saw
    # equals what the boards believe they successfully sent.
    claimed = sum(s.link_stats["sent"] for s in sims)
    print("[vision] boards claim %d sent; collector saw %d -- %s"
          % (claimed, len(collector.messages),
             "agree" if claimed == len(collector.messages)
             else "DISAGREE"))

    silent = sims[1]
    print("[vision] %s lost %d reports to the cut link, and knows it"
          % (silent.name, silent.link_stats["dropped"]))

    collector.stop()
    fleet.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
