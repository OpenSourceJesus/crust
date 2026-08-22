"""Test how baremetal_arm64.py handles --board versus --machine.

These two flags look interchangeable and are not. ``--board`` picks a profile
and decides what the image is *built* as -- linker script, load address,
console driver, interrupt controller, memory map. ``--machine`` only names the
qemu machine the finished image is handed to.

Confusing them used to fail badly: ``--machine raspi3`` was accepted silently,
left the profile at virt, and died at link time with

    undefined reference to: intc_raw_source, intc_raw_timer_control

which points at the interrupt controller when the mistake was the flag. Worse,
``raspi3`` is not even a qemu machine name -- qemu's is ``raspi3b`` -- so the
run could never have worked either way.

    python3 tools/board_machine_test.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(HERE, "baremetal_arm64.py")
APP = os.path.join(ROOT, "examples", "baremetal", "kernel_arm64.c")


def run(*args):
    """Run the tool and return (returncode, combined output)."""
    proc = subprocess.run([sys.executable, TOOL] + list(args),
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def check(name, condition, detail=""):
    if condition:
        print("  PASS  %s" % name)
        return 1, 0
    print("  FAIL  %s %s" % (name, detail))
    return 0, 1


def main():
    passed = failed = 0

    print("\n== board names rejected as qemu machines ==")

    # The exact mistake that motivated this: raspi3 is a board, raspi3b is
    # the machine.
    rc, out = run(APP, "--run", "--machine", "raspi3")
    p, f = check("--machine raspi3 exits nonzero", rc != 0, "rc=%d" % rc)
    passed, failed = passed + p, failed + f
    p, f = check("--machine raspi3 says it is a board name",
                 "is a board name" in out, repr(out[:120]))
    passed, failed = passed + p, failed + f
    p, f = check("--machine raspi3 names the real qemu machine",
                 "raspi3b" in out, repr(out[:120]))
    passed, failed = passed + p, failed + f
    p, f = check("--machine raspi3 does not reach the linker",
                 "intc_raw_source" not in out, "still fails at link")
    passed, failed = passed + p, failed + f

    # Boards with no qemu machine at all get the other half of the message.
    for board in ("jetson", "raspi4"):
        rc, out = run(APP, "--machine", board)
        p, f = check("--machine %s exits nonzero" % board, rc != 0,
                     "rc=%d" % rc)
        passed, failed = passed + p, failed + f
        p, f = check("--machine %s explains there is no qemu machine" % board,
                     "No qemu machine" in out, repr(out[:160]))
        passed, failed = passed + p, failed + f

    print("\n== valid combinations still work ==")

    # virt is both a board and its own qemu machine, so this one is genuinely
    # unambiguous and must not be rejected.
    rc, out = run(APP, "--machine", "virt", "-o",
                  os.path.join(ROOT, "build", "arm64bm", "cli.elf"))
    p, f = check("--machine virt is accepted", rc == 0, "rc=%d, %r" % (rc, out[-160:]))
    passed, failed = passed + p, failed + f

    rc, out = run(APP, "--board", "virt", "-o",
                  os.path.join(ROOT, "build", "arm64bm", "cli.elf"))
    p, f = check("--board virt is accepted", rc == 0, "rc=%d" % rc)
    passed, failed = passed + p, failed + f

    rc, out = run("--boards")
    p, f = check("--boards lists every profile",
                 rc == 0 and all(b in out
                                 for b in ("virt", "raspi3", "raspi4",
                                           "jetson")),
                 repr(out[:160]))
    passed, failed = passed + p, failed + f

    print("\n== unknown values are reported ==")

    rc, out = run(APP, "--board", "nosuchboard")
    p, f = check("unknown board exits nonzero", rc != 0, "rc=%d" % rc)
    passed, failed = passed + p, failed + f
    p, f = check("unknown board lists the known ones",
                 "known:" in out, repr(out[:120]))
    passed, failed = passed + p, failed + f

    print("\n== missing flag values are reported, not raised ==")

    for flag in ("--machine", "--board", "--cpu", "-o", "-D", "--vectors",
                 "--extra-asm"):
        rc, out = run(APP, flag)
        ok = rc != 0 and "needs a value" in out and "Traceback" not in out
        p, f = check("%s with no value" % flag, ok,
                     repr(out[-160:]))
        passed, failed = passed + p, failed + f

    print("\nboard/machine CLI: %d pass, %d fail" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
