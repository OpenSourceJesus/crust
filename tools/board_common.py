"""Shared machinery for the board driver scripts (raspi.py, jetnano.py).

Those scripts take a user's source files, compile them with everything a given
board needs, package the result, and optionally run it under qemu and hand the
run to a test script. Both boards are AArch64 Linux and differ only in CPU
model and defaults, so all of the work lives here and each script is a thin
board profile.

A *run record* is what the test-script hook receives: exit status, stdout,
stderr, and -- when `--debug` is on -- the qemu log with the CPU register dump,
already parsed into a dict. That is the piece that makes this useful for
diff-testing and for chasing miscompiles, since it can assert on the register
state at exit rather than only on a program's exit code.

Emulation note: this uses **qemu-user**, which emulates the AArch64 user-mode
ISA and the Linux syscall interface. It is not a board emulator -- there is no
device tree, no GPIO, no firmware. `-cpu` selects the board's actual core, so
instruction availability and timing-independent behaviour match, but anything
touching hardware does not. See BOARDS.md.
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class Board(object):
    """A board profile: everything raspi/jetnano differ by."""

    def __init__(self, key, name, cpu, target, qemu, notes,
                 cross_prefix="aarch64-linux-gnu-"):
        self.key = key              # short id, used in output paths
        self.name = name            # human name for messages
        self.cpu = cpu              # qemu -cpu model (the board's real core)
        self.target = target        # ShivyCX --target
        self.qemu = qemu            # qemu-user binary
        self.notes = notes          # board caveats, printed by --info
        self.cross_prefix = cross_prefix


class RunRecord(object):
    """The result of running a packaged binary, handed to a test script."""

    def __init__(self):
        self.board = ""
        self.binary = ""
        self.argv = []
        self.exit_code = 0
        self.signal = 0             # nonzero if the process died on a signal
        self.stdout = ""
        self.stderr = ""
        self.qemu_log = ""          # raw -d output, empty unless --debug
        self.registers = {}         # final register state, if --debug
        self.disasm = ""            # objdump of .text, if --debug

    def as_dict(self):
        return {
            "board": self.board, "binary": self.binary, "argv": self.argv,
            "exit_code": self.exit_code, "signal": self.signal,
            "stdout": self.stdout, "stderr": self.stderr,
            "registers": self.registers,
            "qemu_log_bytes": len(self.qemu_log),
        }


def run(cmd, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


def have(tool):
    if not tool:
        return False
    return shutil.which(tool) is not None


# ---------------------------------------------------------------------------
# Register parsing
# ---------------------------------------------------------------------------
# qemu's `-d cpu` dump prints AArch64 state as `X00=...` pairs with `PC=` and
# `SP=` mixed in. We keep the *last* block, which is the state at exit -- the
# earlier ones are every translation block along the way.
_REG_RE = re.compile(r"\b(X\d\d|PC|SP|LR|PSTATE)=([0-9a-fA-F]+)")


def parse_registers(log):
    """Final AArch64 register state from a qemu `-d cpu` log, as a dict of
    name -> int. Empty if the log has no CPU dump."""
    blocks = log.split("PC=")
    if len(blocks) < 2:
        pairs = _REG_RE.findall(log)
    else:
        pairs = _REG_RE.findall("PC=" + blocks[-1])
    regs = {}
    for name, val in pairs:
        try:
            regs[name] = int(val, 16)
        except ValueError:
            pass
    return regs


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def compile_sources(board, sources, outdir, cross, opt_level, extra):
    """Compile and link `sources` for `board`. Returns (binary path, log).

    Two modes, and the distinction is not cosmetic. By default ShivyCX's own
    assembler and linker produce the binary, so nothing outside this repository
    is involved -- which is also the only mode that works when cross-compiling
    from a non-AArch64 host without a cross toolchain, since ShivyCX would
    otherwise hand AArch64 assembly to the *host* `as`. With `--cross` the GNU
    cross toolchain assembles and links instead, which brings in the full
    glibc.
    """
    os.makedirs(outdir, exist_ok=True)
    binary = os.path.join(outdir, board.key)

    if not cross:
        env = dict(os.environ)
        env["SHIVYC_RASM"] = "1"
        env["SHIVYC_RLINK"] = "1"
        cmd = [sys.executable, "-m", "shivyc.main"]
        cmd.extend(sources)
        cmd.extend(["-o", binary, "--target", board.target])
        if opt_level:
            cmd.extend(["-O", str(opt_level)])
        cmd.extend(extra)
        rc, out, err = run(cmd, env=env, cwd=ROOT)
        log = (out + err).strip()
        if rc != 0 or not os.path.exists(binary):
            return None, log or "compiler returned %d" % rc
        os.chmod(binary, 0o755)
        return binary, log

    gcc = board.cross_prefix + "gcc"
    if not have(gcc):
        return None, ("%s not found -- install the cross toolchain, or drop "
                      "--cross to use our own assembler and linker" % gcc)
    asms = []
    logs = []
    for src in sources:
        base = os.path.splitext(os.path.basename(src))[0]
        spath = os.path.join(outdir, base + ".s")
        cmd = [sys.executable, "-m", "shivyc.main", src, "-S", "-o", spath,
               "--target", board.target]
        if opt_level:
            cmd.extend(["-O", str(opt_level)])
        cmd.extend(extra)
        rc, out, err = run(cmd, cwd=ROOT)
        logs.append((out + err).strip())
        if rc != 0 or not os.path.exists(spath):
            return None, "\n".join([x for x in logs if x]) or "compile failed"
        asms.append(spath)

    rc, out, err = run([gcc, "-static"] + asms + ["-o", binary])
    logs.append((out + err).strip())
    if rc != 0 or not os.path.exists(binary):
        return None, "\n".join([x for x in logs if x]) or "link failed"
    os.chmod(binary, 0o755)
    return binary, "\n".join([x for x in logs if x])


def package(board, binary, outdir, selfhosted):
    """Write the run helper and a manifest beside the binary, so the directory
    can be copied to the board and used directly."""
    runner = os.path.join(outdir, "run-on-board.sh")
    with open(runner, "w") as f:
        f.write("#!/bin/sh\n"
                "# Run this on the %s itself. The binary is a static AArch64\n"
                "# Linux executable: no loader, no libc, nothing to install.\n"
                "exec \"$(dirname \"$0\")/%s\" \"$@\"\n"
                % (board.name, os.path.basename(binary)))
    os.chmod(runner, 0o755)

    manifest = {
        "board": board.name,
        "cpu": board.cpu,
        "target": board.target,
        "binary": os.path.basename(binary),
        "selfhosted": bool(selfhosted),
        "size_bytes": os.path.getsize(binary),
    }
    mpath = os.path.join(outdir, "manifest.json")
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    return runner, mpath


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def run_qemu(board, binary, argv, debug, timeout=60):
    """Run `binary` under qemu-user for `board`. Returns a RunRecord."""
    rec = RunRecord()
    rec.board = board.name
    rec.binary = binary
    rec.argv = list(argv)

    cmd = [board.qemu]
    if board.cpu:
        cmd.extend(["-cpu", board.cpu])
    logpath = None
    if debug:
        logpath = binary + ".qemu.log"
        # `cpu` dumps registers before each translation block; that is verbose
        # but it is what makes the final register state available.
        cmd.extend(["-d", "cpu", "-D", logpath])
    cmd.append(binary)
    cmd.extend(argv)

    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        rc = p.returncode
        rec.stdout, rec.stderr = p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rec.exit_code = -1
        rec.stderr = "timed out after %ds" % timeout
        return rec

    if rc < 0:
        rec.signal = -rc
        rec.exit_code = rc
    else:
        rec.exit_code = rc

    if logpath and os.path.exists(logpath):
        with open(logpath, errors="replace") as f:
            rec.qemu_log = f.read()
        rec.registers = parse_registers(rec.qemu_log)
    return rec


def disassemble(binary, prefix="aarch64-linux-gnu-"):
    tool = prefix + "objdump"
    if not have(tool):
        return ""
    rc, out, _ = run([tool, "-d", binary])
    return out if rc == 0 else ""


# ---------------------------------------------------------------------------
# Test-script hook
# ---------------------------------------------------------------------------
def run_test_script(path, rec):
    """Execute a user test script against a run record.

    The script is plain Python, executed with `record` bound in its globals
    (a RunRecord). It signals a failure by raising, by defining a `check(rec)`
    that returns a false value or a string, or by setting `result` to a string.
    Anything else is a pass. Returns (ok, message).
    """
    if not os.path.exists(path):
        return False, "test script not found: %s" % path
    src = open(path).read()
    glb = {
        "record": rec,
        "registers": rec.registers,
        "stdout": rec.stdout,
        "stderr": rec.stderr,
        "exit_code": rec.exit_code,
        "qemu_log": rec.qemu_log,
        "__name__": "__board_test__",
        "__file__": os.path.abspath(path),
    }
    try:
        exec(compile(src, path, "exec"), glb)
    except AssertionError as e:
        return False, "assertion failed: %s" % (e or "(no message)")
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)

    check = glb.get("check")
    if callable(check):
        try:
            res = check(rec)
        except AssertionError as e:
            return False, "assertion failed in check(): %s" % (e or "")
        except Exception as e:
            return False, "check() raised %s: %s" % (type(e).__name__, e)
        if res is None or res is True:
            return True, "check() passed"
        if res is False:
            return False, "check() returned False"
        return False, str(res)

    res = glb.get("result")
    if isinstance(res, str) and res:
        return False, res
    return True, "script completed"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def parse_args(board, argv):
    opts = {
        "sources": [], "qemu": False, "debug": False, "cross": False,
        "test_script": None, "outdir": None, "opt": 0, "run_args": [],
        "info": False, "extra": [], "timeout": 60, "keep_going": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--qemu":
            opts["qemu"] = True
        elif a == "--debug":
            opts["debug"] = True
            opts["qemu"] = True
        elif a == "--cross":
            opts["cross"] = True
        elif a == "--selfhosted":
            pass                       # the default; accepted for clarity
        elif a == "--info":
            opts["info"] = True
        elif a.startswith("--test-script="):
            opts["test_script"] = a.split("=", 1)[1]
            opts["qemu"] = True
        elif a == "--test-script":
            opts["test_script"] = argv[i + 1]
            opts["qemu"] = True
            i += 1
        elif a.startswith("--out="):
            opts["outdir"] = a.split("=", 1)[1]
        elif a.startswith("--timeout="):
            opts["timeout"] = int(a.split("=", 1)[1])
        elif a.startswith("-O"):
            opts["opt"] = int(a[2:] or "0")
        elif a == "--":
            opts["run_args"] = argv[i + 1:]
            break
        elif a in ("-h", "--help"):
            usage(board)
            sys.exit(0)
        elif a.startswith("-"):
            opts["extra"].append(a)
        else:
            opts["sources"].append(a)
        i += 1
    return opts


def usage(board):
    print("""usage: %(prog)s [options] source.c [more.c ...] [-- prog args]

Build for the %(name)s (%(cpu)s, AArch64 Linux) and optionally run it.

  --qemu               run the result under qemu-user (%(qemu)s)
  --debug              also capture qemu's register dump (implies --qemu)
  --test-script=FILE   run FILE against the result (implies --qemu)
  --cross              assemble and link with the GNU cross toolchain
                       (brings in glibc). The default uses our own assembler,
                       linker and runtime -- no gcc, no binutils, no libc
  --out=DIR            where to write the package (default: build/%(key)s)
  --timeout=N          seconds to allow the program (default 60)
  -O<n>                optimisation level passed to the compiler
  --info               print what this board is and what is not emulated
  --                   everything after this is passed to the program

A test script is plain Python with `record` in scope (also `registers`,
`stdout`, `stderr`, `exit_code`, `qemu_log`). Fail by raising, by asserting,
or by defining check(rec) that returns a message. For example:

    def check(rec):
        if rec.exit_code != 42:
            return "expected 42, got %%d" %% rec.exit_code
        if rec.registers.get("X00", 0) & 0xff != 42:
            return "X0 did not hold the return value"
""" % {"prog": os.path.basename(sys.argv[0]), "name": board.name,
       "cpu": board.cpu, "qemu": board.qemu, "key": board.key})


def main(board, argv):
    opts = parse_args(board, argv[1:])

    if opts["info"]:
        print("%s -- %s core, AArch64 Linux" % (board.name, board.cpu))
        print("  ShivyCX target : %s" % board.target)
        print("  qemu           : %s -cpu %s (user mode)"
              % (board.qemu, board.cpu))
        for n in board.notes:
            print("  note           : %s" % n)
        return 0

    if not opts["sources"]:
        usage(board)
        return 2

    outdir = opts["outdir"] or os.path.join(ROOT, "build", board.key)
    print("== building for %s (%s) ==" % (board.name, board.cpu))
    binary, log = compile_sources(board, opts["sources"], outdir,
                                  opts["cross"], opts["opt"],
                                  opts["extra"])
    if binary is None:
        print("  FAIL  compile failed")
        for line in log.split("\n")[:20]:
            print("    " + line)
        return 1
    print("  ok    %s (%d bytes, %s)"
          % (binary, os.path.getsize(binary),
             "GNU cross toolchain" if opts["cross"]
             else "self-hosted toolchain"))

    runner, manifest = package(board, binary, outdir, not opts["cross"])
    print("  ok    packaged: %s, %s"
          % (os.path.basename(runner), os.path.basename(manifest)))

    if not opts["qemu"]:
        print("\nCopy %s to the %s and run ./run-on-board.sh"
              % (outdir, board.name))
        return 0

    if not have(board.qemu):
        print("  SKIP  %s not installed -- cannot run here" % board.qemu)
        return 0

    print("\n== running under qemu (%s, -cpu %s) =="
          % (board.qemu, board.cpu))
    rec = run_qemu(board, binary, opts["run_args"], opts["debug"],
                   opts["timeout"])
    if rec.stdout:
        for line in rec.stdout.rstrip("\n").split("\n"):
            print("  out | " + line)
    if rec.stderr.strip():
        for line in rec.stderr.rstrip("\n").split("\n")[:10]:
            print("  err | " + line)
    if rec.signal:
        print("  died on signal %d" % rec.signal)
    else:
        print("  exit %d" % rec.exit_code)
    if opts["debug"]:
        if rec.registers:
            keys = ["PC", "SP", "LR"] + ["X%02d" % n for n in range(4)]
            shown = ["%s=%#x" % (k, rec.registers[k])
                     for k in keys if k in rec.registers]
            print("  regs at exit: " + "  ".join(shown))
            print("  (%d registers captured, full log at %s.qemu.log)"
                  % (len(rec.registers), binary))
        else:
            print("  no register dump captured -- does this qemu support "
                  "-d cpu?")

    if opts["test_script"]:
        print("\n== test script: %s ==" % opts["test_script"])
        rec.disasm = disassemble(binary)
        ok, msg = run_test_script(opts["test_script"], rec)
        print("  %s  %s" % ("PASS" if ok else "FAIL", msg))
        return 0 if ok else 1

    return 0 if rec.exit_code >= 0 and not rec.signal else 1
