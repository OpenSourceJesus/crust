"""fleet_demo.py - three boards, one plant model, one plot.

    python3 tools/hostsim_build.py examples/hostsim/motor_node.c -o /tmp/motor.so
    python3 examples/hostsim/fleet_demo.py

Each board runs the same firmware from examples/hostsim/motor_node.c, the
same source that tools/baremetal_arm64.py builds for a Jetson or a Pi. Here it
is compiled for the host, so three of them advance in lockstep on a shared
virtual clock while this process plays the part of the physical world:
integrating each motor, adding sensor noise, and injecting the sort of fault
that is tedious to arrange on a bench.

This is the case instruction-level emulation cannot reach. Ten simulated
seconds per board is around 14 billion instructions on real silicon; armulator
manages roughly 17,000 a second. The same run finishes here in well under a
second, which is what makes a parameter sweep or a fault-injection campaign
worth setting up at all.

What it is not: a check that the firmware's ARM code is correct. Nothing here
executes an ARM instruction. Run tools/hostsim_difftest.py for that, and
tools/jetson_armulator.py when the question is about the image itself.
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tools"))

from hostsim import Fleet, Sim  # noqa: E402

SO = "/tmp/motor.so"
DURATION_MS = 8000
STEP_MS = 1


class Plant:
    """A motor and its load, as seen from outside the board.

    Deliberately crude -- first-order velocity response, viscous friction,
    integer encoder counts. The point is that it lives here, in Python, where
    it can be changed between runs without recompiling anything.
    """

    def __init__(self, inertia=1.0, friction=1.0, seed=0):
        self.position = 0.0
        self.velocity = 0.0
        self.inertia = inertia
        self.friction = friction
        self.stuck = False
        self._seed = seed

    def advance(self, duty, dt):
        if self.stuck:
            self.velocity = 0.0
            return
        accel = duty / self.inertia - self.friction * self.velocity
        self.velocity += accel * dt
        self.position += self.velocity * dt


def main():
    # Always rebuild: a stale shared object from an older backend is a
    # confusing failure (an undefined symbol from ctypes, far from the cause).
    import hostsim_build
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "motor_node.c")
    hostsim_build.build([source], SO, verbose=False)

    names = ["axis-x", "axis-y", "axis-z"]
    targets = [1000, -600, 2500]

    sims = [Sim(SO, name=n) for n in names]
    # Slightly different mechanics per axis, so one controller tuning does
    # not suit all three -- which is the usual situation.
    plants = [Plant(inertia=1.0 + 0.4 * i, friction=1.0 + 0.25 * i, seed=i)
              for i in range(len(sims))]
    fleet = Fleet(sims).start()

    for sim, target in zip(sims, targets):
        sim.target = target

    history = {n: {"t": [], "pos": [], "reported": [], "duty": []}
               for n in names}
    dt = STEP_MS / 1000.0

    for elapsed in range(0, DURATION_MS, STEP_MS):
        # Two faults, at different times, of kinds that are a nuisance to
        # arrange on a bench and one call each here.
        if elapsed == DURATION_MS // 2:
            plants[1].stuck = True
            print("[fleet] t=%dms  axis-y bearing seized" % elapsed)
        if elapsed == DURATION_MS * 3 // 4:
            # The shaft keeps turning; the sensor stops reporting it. This is
            # the more dangerous of the two, because the controller believes
            # it and keeps driving.
            sims[2].fault_encoder_stuck(True)
            print("[fleet] t=%dms  axis-z encoder seized (shaft still turning)"
                  % elapsed)

        fleet.step_ms(STEP_MS)

        for sim, plant, name in zip(sims, plants, names):
            plant.advance(sim.motor_duty, dt)
            sim.encoder = int(plant.position)   # the true shaft position
            sim.read()                          # drain the console
            history[name]["t"].append(elapsed)
            history[name]["pos"].append(plant.position)
            history[name]["reported"].append(sim.encoder)  # what it believes
            history[name]["duty"].append(sim.motor_duty)

    print("\n%-8s %10s %10s %10s" % ("board", "target", "final", "duty"))
    for sim, name, target in zip(sims, names, targets):
        h = history[name]
        print("%-8s %10d %10.1f %10d"
              % (name, target, h["pos"][-1], h["duty"][-1]))

    # A stalled axis has a signature worth alarming on: the controller is
    # still commanding current, and nothing is moving. The integral clamp in
    # the firmware stops it winding all the way to saturation, so the duty
    # settles at a steady non-zero value instead -- which is exactly the sort
    # of detail that is easier to discover in simulation than on a bench.
    print()
    for name in names:
        h = history[name]
        recent = h["pos"][-500:]
        moved = max(recent) - min(recent)
        duty = h["duty"][-1]
        if moved < 1.0 and abs(duty) > 50:
            print("[fleet] %s STALLED: duty=%d held for 500ms with %.2f "
                  "counts of motion" % (name, duty, moved))

        # The divergence a stuck sensor causes: the board's idea of where it
        # is, against where it actually is. Nothing on the board can see this
        # -- only the simulation can, which is the reason to have one.
        drift = abs(h["pos"][-1] - h["reported"][-1])
        if drift > 10.0:
            print("[fleet] %s SENSOR DIVERGENCE: believes %d, actually %.0f "
                  "(%.0f counts adrift)"
                  % (name, h["reported"][-1], h["pos"][-1], drift))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed; skipping the plot)")
        fleet.close()
        return 0

    figure, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    for name, target in zip(names, targets):
        h = history[name]
        line, = top.plot(h["t"], h["pos"],
                         label="%s (target %d)" % (name, target))
        # Where the board thinks it is, when that differs from the truth.
        if max(abs(p - r) for p, r in zip(h["pos"], h["reported"])) > 10:
            top.plot(h["t"], h["reported"], linestyle="--", linewidth=1,
                     color=line.get_color(),
                     label="%s as reported" % name)
        bottom.plot(h["t"], h["duty"], label=name)
    top.axvline(DURATION_MS // 2, color="grey", linestyle=":", linewidth=1)
    top.set_ylabel("position (counts)")
    top.legend(loc="lower right", fontsize=8)
    top.set_title("three boards, one virtual clock, plant model in numpy")
    bottom.axvline(DURATION_MS // 2, color="grey", linestyle=":", linewidth=1)
    bottom.set_ylabel("duty")
    bottom.set_xlabel("simulated time (ms)")
    bottom.legend(loc="lower right", fontsize=8)
    figure.tight_layout()

    out = "/tmp/fleet.png"
    figure.savefig(out, dpi=110)
    print("\n[fleet] plot written to %s" % out)

    fleet.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
