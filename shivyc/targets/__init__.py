"""Compilation targets (back-end architectures).

The IL produced by il_gen is architecture-neutral; everything below it -- the
register file, the calling convention, instruction selection, and the assembler
syntax -- is target-specific. This package is the seam: a `Target` object owns
those facts so the same front end can emit x86-64 today and aarch64 / riscv64
later, selected by `--target`.

Stage 1 introduces the seam and moves the smallest, most self-contained fact --
the assembler-syntax directives that wrap the program -- behind it, leaving
behavior byte-identical on x86-64. Later stages move the register file, calling
convention, and instruction selection here too.

Self-host note: the self-hosted compiler cannot read a class attribute through
the type, so every target fact is an *instance* attribute set in __init__, and
callers always work with a target *instance* (via get_target), never the class.
"""


class Target:
    """Base class: the architecture facts a back end needs. Subclasses fill
    these in. Kept as instance attributes for self-host compatibility."""

    def __init__(self):
        self.name = "generic"
        # GNU "triple" used when handing work to an external assembler/linker or
        # a cross toolchain (e.g. clang --target=<triple>).
        self.triple = ""
        # Assembler-syntax directive lines emitted before / after the program
        # body. x86 GAS toggles Intel vs AT&T syntax; aarch64 GAS has a single
        # native syntax and emits neither.
        self.asm_syntax_prologue = []
        self.asm_syntax_epilogue = []
        # True for targets that emit a finished binary artifact directly from
        # the back end rather than assembler text for `as` + `ld`. The driver
        # in main.py branches on this instead of on the target name, so a
        # second such target costs nothing here.
        self.is_binary = False
        # Filename extension of the back end's output. Only meaningful when
        # is_binary is set; the text targets always produce ".s".
        self.output_ext = ".s"
        # Operating system the output runs on, orthogonal to the architecture:
        # "linux" (the historical default), "none" (freestanding / bare-metal
        # ELF -- the same object-level facts as linux), or "macos". Selected
        # by --os; see set_os below.
        self.os = "linux"
        # Object-file format the emitted assembler text targets. "elf" for
        # every Linux / bare-metal target; "macho" for Apple. Drives section
        # directives, symbol spelling, and address-materialization syntax.
        self.obj_format = "elf"
        # Prefix the platform C ABI puts on every C-level symbol. Mach-O
        # spells `main` as `_main`; ELF uses the bare name.
        self.sym_prefix = ""

    def set_os(self, os_name):
        """Retarget this instance at operating system `os_name` (already
        normalized by normalize_os). Only the ELF default is accepted here;
        subclasses that support another OS override this."""
        if os_name == "" or os_name == "linux" or os_name == "none":
            if os_name:
                self.os = os_name
            return
        raise ValueError("target %s does not support --os %s"
                         % (self.name, os_name))


class X86_64Target(Target):
    """x86-64 (AMD64), System V ABI, GAS Intel syntax. The original and, for
    now, the only fully-implemented back end."""

    def __init__(self):
        Target.__init__(self)
        self.name = "x86_64"
        self.triple = "x86_64-linux-gnu"
        self.asm_syntax_prologue = ["\t.intel_syntax noprefix"]
        self.asm_syntax_epilogue = ["\t.att_syntax noprefix"]


class Arm64Target(Target):
    """aarch64 / ARM64 bare-metal cross target. Instruction selection and the
    register file land in later stages; for now this carries the triple and the
    (empty) syntax directives so the seam is exercised end to end."""

    def __init__(self):
        Target.__init__(self)
        self.name = "arm64"
        self.triple = "aarch64-none-elf"
        # aarch64 GAS has one native syntax; no intel/att toggle is emitted.
        self.asm_syntax_prologue = []
        self.asm_syntax_epilogue = []

    def set_os(self, os_name):
        """arm64 additionally supports macOS on Apple Silicon (Mach-O,
        Apple's arm64 variant of AAPCS64). The architecture name stays
        "arm64" -- only the OS-facing facts change -- so everything keyed on
        the architecture (register pools, thread contracts) is unaffected."""
        if os_name == "macos":
            self.os = "macos"
            self.obj_format = "macho"
            self.sym_prefix = "_"
            self.triple = "arm64-apple-macos11"
            return
        Target.set_os(self, os_name)


class RiscV64Target(Target):
    """RV64 (riscv64) bare-metal cross target, lp64 ABI. Shares the entire
    target-neutral middle end -- IL, liveness, and the linear-scan register
    allocator -- with the other back ends; only instruction selection, the
    register file (x0..x31 / a0-a7 / s0-s11 / t0-t6), and the ABI differ.
    Brought up after aarch64 to validate that the seam makes a second ISA
    cheap: the allocator is reused verbatim and only lowering is new."""

    def __init__(self):
        Target.__init__(self)
        self.name = "riscv64"
        self.triple = "riscv64-unknown-elf"
        # RISC-V GAS has one native syntax; no intel/att toggle is emitted.
        self.asm_syntax_prologue = []
        self.asm_syntax_epilogue = []


class M68kTarget(Target):
    """Motorola 68000 / Neo-Geo bare-metal cross target (the console's main CPU;
    ngdevkit cross-compiles to it with gcc). CISC, big-endian, two register
    files (data d0-d7 / address a0-a7), two-address ALU ops, .b/.w/.l sizes, and
    a stack-based calling convention -- a deliberately different shape from the
    RISC back ends, chosen to test how far the target-neutral middle end
    stretches. The integer-core lowering reuses the shared liveness + linear-scan
    allocator unchanged; only instruction selection and the m68k ABI are new."""

    def __init__(self):
        Target.__init__(self)
        self.name = "m68k"
        self.triple = "m68k-neogeo-elf"
        # m68k GAS uses one native (Motorola/MIT) syntax; no toggle is emitted.
        self.asm_syntax_prologue = []
        self.asm_syntax_epilogue = []


class WasmTarget(Target):
    """WebAssembly (wasm32, MVP binary format).

    The first target here that is not a register machine, and so the first that
    shares none of the middle end: there are no physical registers to allocate
    (locals are unbounded and typed, and the engine's own JIT does the real
    allocation), and there is no flat instruction stream to branch around --
    control flow must be expressed as structured block/loop/br, so the IL's
    label-and-jump CFG is reconstructed into a br_table dispatch by asm_gen.

    It is also the first target with no external assembler step. shivyc/wasm.py
    emits the `.wasm` binary itself, so `as` and `ld` are never invoked; the
    is_binary flag below is what tells the driver to skip them.
    """

    def __init__(self):
        Target.__init__(self)
        self.name = "wasm"
        self.triple = "wasm32-unknown-unknown"
        # No assembler text is produced at all, so there is no syntax to select.
        self.asm_syntax_prologue = []
        self.asm_syntax_epilogue = []
        self.is_binary = True
        self.output_ext = ".wasm"


def _make_target(n):
    """Construct the architecture target for canonical-or-alias name `n`."""
    if n == "x86_64" or n == "amd64":
        return X86_64Target()
    if n == "arm64" or n == "aarch64":
        return Arm64Target()
    if n == "riscv64" or n == "rv64":
        return RiscV64Target()
    if n == "m68k" or n == "neogeo" or n == "68k":
        return M68kTarget()
    if n == "wasm" or n == "wasm32" or n == "webassembly":
        return WasmTarget()
    return X86_64Target()


# Canonical name plus accepted aliases -> constructor.
def get_target(name, os_name=""):
    """Return a fresh Target instance for `name` (default x86-64). Aliases:
    amd64->x86_64, aarch64->arm64, rv64->riscv64, neogeo/68k->m68k. An unknown
    name falls back to x86-64 so the compiler stays usable; front ends should
    validate the name explicitly.

    `os_name` (optional, any alias accepted by normalize_os) selects the
    operating system. An unsupported architecture/OS pairing falls back to the
    architecture's default OS here; the driver validates the pairing with
    is_supported_os first so the user gets a real diagnostic."""
    n = name if name else "x86_64"
    t = _make_target(n)
    osn = normalize_os(os_name)
    if osn and is_supported_os(t.name, osn):
        t.set_os(osn)
    return t


def normalize_os(os_name):
    """Canonical OS name for `os_name`, "" when unset, or the input unchanged
    (so is_known_os can reject it) when unrecognized."""
    if not os_name:
        return ""
    o = os_name.lower()
    if o == "macos" or o == "darwin" or o == "osx" or o == "macosx" \
            or o == "apple":
        return "macos"
    if o == "linux" or o == "gnu":
        return "linux"
    if o == "none" or o == "baremetal" or o == "bare-metal" \
            or o == "freestanding" or o == "elf":
        return "none"
    return os_name


def is_known_os(os_name):
    """True if `os_name` is a recognized OS or alias."""
    o = normalize_os(os_name)
    return o == "" or o == "linux" or o == "none" or o == "macos"


def is_supported_os(target_name, os_name):
    """True if the architecture `target_name` can target `os_name`.
    macOS is Apple Silicon only, so it pairs with arm64 alone."""
    o = normalize_os(os_name)
    if o == "" or o == "linux" or o == "none":
        return True
    if o == "macos":
        return target_name == "arm64" or target_name == "aarch64"
    return False


def is_known_target(name):
    """True if `name` is a recognized target or alias."""
    return name in ("x86_64", "amd64", "arm64", "aarch64", "riscv64", "rv64",
                    "m68k", "neogeo", "68k", "wasm", "wasm32", "webassembly")


DEFAULT_TARGET = "x86_64"
