"""Objects for the IL->ASM stage of the compiler."""

import itertools

import shivyc.asm_cmds as asm_cmds
import shivyc.spots as spots
from shivyc.spots import mangle_symbol
import shivyc.simd_pack as simd_pack
from shivyc.spots import Spot, RegSpot, MemSpot, LiteralSpot
from shivyc.il_cmds.base import ILCommand  # noqa: F401  (polymorphic interface
# dispatched on the IL commands asm_gen consumes; see command.inputs()/etc.)
from typing import List
from shivyc.il_gen import ILValue
from shivyc.ctypes import CType  # noqa: F401



def _float_to_bits(val, size):
    """If `val` is a float, return its IEEE-754 bit pattern as an integer
    suitable for a .int/.quad directive; otherwise return `val` unchanged.
    Integer initializers are Python ints and pass through untouched."""
    if isinstance(val, float):
        import struct
        if size == 4:
            return struct.unpack("<I", struct.pack("<f", val))[0]
        return struct.unpack("<Q", struct.pack("<d", val))[0]
    return val


class ASMCode:
    """Stores the ASM code generated from the IL code.

    lines (List) - Lines of ASM code recorded. The commands are stored as
    tuples in this list, where the first value is the name of the command and
    the next values are the command arguments.

    """

    def __init__(self, target=None):
        """Initialize ASMCode."""
        from shivyc.targets import get_target
        # The back-end target (architecture facts: syntax, and later the register
        # file / calling convention / instruction selection). Defaults to x86-64
        # so existing call sites and tests are unaffected.
        self.target = target if target is not None else get_target("x86_64")
        self.lines = []
        self.comm = []
        self.globals = []
        self.data = []
        self.string_literals = []
        # Attributes that ASMGen sets during emission. Declared here (rather
        # than monkey-patched onto the instance) so the transpiler lays them
        # out in the struct; ASMGen still re-initializes them per run.
        self.simd_pack_hot = False
        self.frameless = False
        self.metamorphic_funcs = set()
        self.metamorphic_current = None
        self.pack_args_enabled = False
        self.simd_pack = None
        # Binary back ends (wasm) put their finished module here instead of
        # appending assembler text to `lines`; the driver writes it straight to
        # disk. None for every text target. Declared here rather than
        # monkey-patched so the transpiler lays out the field.
        self.wasm_bytes = None

    def add(self, cmd):
        """Add a command to the code.

        cmd (ASMCommand) - Command to add

        """
        self.lines.append(cmd)

    label_num = 0

    @staticmethod
    def get_label():
        """Return a unique label string."""
        ASMCode.label_num += 1
        return f"__shivyc_label{ASMCode.label_num}"

    def add_global(self, name):
        """Add a name to the code as global.

        name (str) - The name to add.

        """
        self.globals.append(f"\t.global {mangle_symbol(name)}")

    def add_weak(self, name):
        """Mark a symbol as having weak linkage (emits `.weak name`)."""
        self.globals.append(f"\t.weak {mangle_symbol(name)}")

    def add_alias(self, name, target):
        """Emit an assembler alias making `name` resolve to `target`."""
        self.globals.append(f"\t.set {mangle_symbol(name)}, {mangle_symbol(target)}")

    def add_data(self, name, size, init):
        """Add static data to the code.

        init - the value to initialize `name` to
        """
        self.data.append(f"{mangle_symbol(name)}:")
        size_strs = {1: "byte",
                     2: "word",
                     4: "int",
                     8: "quad"}

        if init:
            self.data.append(f"\t.{size_strs[size]} {_float_to_bits(init, size)}")
        else:
            self.data.append(f"\t.zero {size}")

    def add_data_block(self, name, entries, total):
        """Emit an initialized aggregate static object.

        entries - iterable of (byte_offset, size, value) constant scalars
        total - total size in bytes; gaps and the tail are zero-filled
        """
        self.data.append(f"{mangle_symbol(name)}:")
        size_strs = {1: "byte", 2: "word", 4: "int", 8: "quad"}
        pos = 0
        for off, size, val in sorted(entries, key=lambda e: e[0]):
            if off > pos:
                self.data.append(f"\t.zero {off - pos}")
            if isinstance(val, tuple) and val and val[0] == "sym":
                _, sym, addend = val
                msym = mangle_symbol(sym)
                ref = msym if not addend else f"{msym}+{addend}"
                # A symbol reference is an address. Normally 8 bytes, but under
                # -f-pointer-compression a pointer-sized field is 4 bytes: emit
                # a 32-bit relocation (.int), valid because the whole image is
                # based in the low 4 GiB (-f-low-mem).
                self.data.append(f"\t.{size_strs.get(size, 'quad')} {ref}")
            else:
                self.data.append(
                    f"\t.{size_strs[size]} {_float_to_bits(val, size)}")
            pos = off + size
        if pos < total:
            self.data.append(f"\t.zero {total - pos}")

    def add_comm(self, name, size, local):
        """Add a common symbol to the code."""
        if local:
            self.comm.append(f"\t.local {mangle_symbol(name)}")
        self.comm.append(f"\t.comm {mangle_symbol(name)} {size}")

    def add_string_literal(self, name, chars, elem_size=1):
        """Add a string literal to the ASM code.

        elem_size - bytes per element (1 for char strings, 4 for wide/wchar_t).
        """
        from shivyc.spots import mangle_symbol
        self.string_literals.append(f"{mangle_symbol(name)}:")
        directive = {1: "byte", 2: "word", 4: "int", 8: "quad"}[elem_size]
        data = ",".join(str(char) for char in chars)
        self.string_literals.append(f"\t.{directive} {data}")

    def full_code(self):  # noqa: D202
        """Produce the full assembly code.

        return (str) - The assembly code, ready for saving to disk and
        assembling.

        """
        header = list(self.target.asm_syntax_prologue)
        header += self.comm
        if self.string_literals or self.data:
            header += ["\t.section .data"]
            header += self.data
            header += self.string_literals
            header += [""]

        header += ["\t.section .text"] + self.globals

        code = [str(line) for line in self.lines]

        footer = ["\t.section\t.note.GNU-stack,\"\",@progbits"]
        footer += list(self.target.asm_syntax_epilogue) + [""]

        return "\n".join(header + code + footer)


class NodeGraph:
    """Graph storing conflict and preference information.

    self._real_nodes - list of all real nodes in this graph
    self._all_nodes - list of all nodes in this graph, including precolored
    self.conf - dictionary mapping each node to nodes with which it
    has a conflict edge
    self._pref - dictionary mapping each node to nodes with which it
    has a preference edge

    The conflict and preference relations are symmetric. That is,
    if `n1 in self.conf[n2]`, then `n2 in self._conf[n1]` and vice versa.
    """

    def __init__(self, nodes=None):
        """Initialize NodeGraph."""
        self._real_nodes = nodes or []
        self._all_nodes = self._real_nodes[:]
        # Conflict neighbours are stored as dicts-used-as-sets ({neighbour: 1})
        # so membership and removal are O(1). The register allocator queries
        # conflict membership extremely heavily during coalescing; with lists it
        # had to rebuild a separate dict-of-sets cache on every coalesce pass
        # (O(V+E) each, thousands of times per large function), which dominated
        # the allocator's peak arena memory. Holding the graph's own conflict
        # neighbours as sets removes that cache entirely.
        self._conf = {}
        for n in self._all_nodes:
            self._conf[n] = {}
        self._pref = {n: [] for n in self._all_nodes}

    def is_node(self, n: "object"):
        """Check whether given node is in the graph."""
        return n in self._conf and n in self._pref

    def add_dummy_node(self, v: "object"):
        """Add a dummy node to graph."""
        self._all_nodes.append(v)
        self._conf[v] = {}
        self._pref[v] = []

        # Dummy nodes must mutually conflict
        for nd in self._all_nodes:
            if nd not in self._real_nodes and nd != v:
                self.add_conflict(nd, v)

    def add_conflict(self, n1: "object", n2: "object"):
        """Add a conflict edge between n1 and n2."""
        self._conf[n1][n2] = 1
        self._conf[n2][n1] = 1

    def add_pref(self, n1: "object", n2: "object"):
        """Add a preference edge between n1 and n2."""
        if n2 not in self._pref[n1]:
            self._pref[n1].append(n2)
        if n1 not in self._pref[n2]:
            self._pref[n2].append(n1)

    def pop(self, n: "object"):
        """Remove and return node n from this graph."""
        del self._conf[n]
        del self._pref[n]
        if n in self._real_nodes:
            self._real_nodes.remove(n)
        self._all_nodes.remove(n)

        for v in self._conf:
            if n in self._conf[v]:
                del self._conf[v][n]
        for v in self._pref:
            if n in self._pref[v]:
                self._pref[v].remove(n)
        return n

    def remove_node(self, n: "object"):
        """Remove and return node n from this graph.

        This is an exact duplicate of pop(). It exists under a non-builtin
        name so that callers holding the graph in a plain (un-inferred)
        parameter -- e.g. _simplify_once(self, nodes, g) -- dispatch through
        the vtable to this method. The self-hosting transpiler lowers a bare
        `.pop(x)` call to a dict pop when it cannot infer that the receiver is
        a NodeGraph; on a NodeGraph that dict pop is a silent no-op, so the
        node is never actually removed. Simplification then removes nothing and
        every low-degree node it should have eliminated is spilled instead --
        the dominant cost of register allocation on large functions.
        """
        del self._conf[n]
        del self._pref[n]
        if n in self._real_nodes:
            self._real_nodes.remove(n)
        self._all_nodes.remove(n)

        for v in self._conf:
            if n in self._conf[v]:
                del self._conf[v][n]
        for v in self._pref:
            if n in self._pref[v]:
                self._pref[v].remove(n)
        return n

    def merge(self, n1: "object", n2: "object"):
        """Merge nodes n1 and n2.

        This function merges n2 into n1. That is, it removes n2 from the
        graph and n1 gets the preference neighbors and conflict neighbors
        that n2 previously had.
        """

        # Merge conflict sets: n1 gains all of n2's conflict neighbours.
        for c in self._conf[n2]:
            self._conf[n1][c] = 1

        # Restore the symmetric invariant: every node that conflicted with
        # n2 (now folded into n1's set) records the conflict against n1.
        for c in self._conf[n1]:
            if n2 in self._conf[c]:
                del self._conf[c][n2]
            self._conf[c][n1] = 1

        # Merge preference lists
        total_pref = self._pref[n1][:]
        for p in self._pref[n2]:
            if p not in total_pref:
                total_pref.append(p)

        if n1 in total_pref: total_pref.remove(n1)
        if n2 in total_pref: total_pref.remove(n2)
        self._pref[n1] = total_pref

        # Restore symmetric invariant
        for c in self._pref[n1]:
            if n2 in self._pref[c]:
                self._pref[c].remove(n2)
            if n1 not in self._pref[c]:
                self._pref[c].append(n1)

        del self._conf[n2]
        del self._pref[n2]
        self._real_nodes.remove(n2)
        self._all_nodes.remove(n2)

    def remove_pref(self, n1: "object", n2: "object"):
        """Remove the preference edge between n1 and n2."""
        self._pref[n1].remove(n2)
        self._pref[n2].remove(n1)

    def prefs(self, n: "object"):
        """Return the list of nodes to which n has a preference edge."""
        return self._pref[n]

    def confs(self, n: "object"):
        """Return the list of nodes with which n has a conflict edge."""
        return self._conf[n]

    def nodes(self):
        """Return the real nodes currently in this graph."""
        return self._real_nodes

    def all_nodes(self):
        """Return all nodes in this graph, including pseudonodes."""
        return self._all_nodes

    def copy_node(self) -> "NodeGraph":
        """Return a deep copy of this graph, but with same ILValue objects."""
        g = NodeGraph()

        g._real_nodes = self._real_nodes[:]
        g._all_nodes = self._all_nodes[:]
        for n in self._all_nodes:
            n_conf = {}
            for k in self._conf[n]:
                n_conf[k] = 1
            g._conf[n] = n_conf
            g._pref[n] = self._pref[n][:]

        return g

    def __str__(self):  # pragma: no cover
        """Return this graph as a string for debugging purposes."""
        return ("Conf\n"
                + "\n".join(str((v, self._conf[v])) for v in self._all_nodes)
                + "\nPref\n"
                + "\n".join(str((v, self._pref[v])) for v in self._all_nodes))


class ASMGen:
    """Contains the main logic for generation of the ASM from the IL.

    il_code (ILCode) - IL code to convert to ASM.
    asm_code (ASMCode) - ASMCode object to populate with ASM.
    arguments - Arguments passed via command line.
    offset (int) - Current offset from RBP for allocating on stack

    """

    asm_code: ASMCode

    # List of registers used for allocation, sorted preferred-first
    alloc_registers = spots.registers

    # List of registers used by the get_reg function.
    all_registers = alloc_registers

    def __init__(self, il_code, symbol_table, asm_code, arguments):
        """Initialize ASMGen."""
        self.il_code = il_code
        self.symbol_table = symbol_table
        self.asm_code = asm_code
        self.arguments = arguments

        self.offset = 0

        # SIMD bit-packing of small global flags (opt-in). The layout is built
        # in _get_global_spotmap once all static globals are known -- unless a
        # whole-program layout was supplied (multi-TU build), in which case
        # every unit uses that single shared, frozen layout and the memory
        # mirror is a shared common symbol.
        wp_layout = getattr(arguments, "_simd_pack_layout", None)
        if wp_layout is not None:
            self.simd_pack = wp_layout
            self.simd_pack_enabled = True
            self._simd_pack_shared = True
        else:
            self.simd_pack = simd_pack.SimdPackLayout()
            self.simd_pack_enabled = getattr(
                arguments, "simd_pack_globals", False)
            self._simd_pack_shared = False
        # Expose to IL commands, which only receive the asm_code object.
        asm_code.simd_pack = self.simd_pack
        asm_code.simd_pack_enabled = self.simd_pack_enabled
        asm_code.simd_pack_hot = False

        # Stackless / low-overhead calls (opt-in). The IL pass annotates Call
        # commands and records per-function call-structure flags; framelessness
        # is finalized here once stack offsets are known.
        self.stackless_enabled = getattr(
            arguments, "stackless_calls", False)
        asm_code.frameless = False

        # Argument packing (opt-in via -f-pack-args). Both caller and callee
        # consult this flag and recompute the identical packing layout from the
        # function signature.
        asm_code.pack_args_enabled = getattr(arguments, "pack_args", False)

        # Metamorphic returns (opt-in via -fmetamorphic + __metamorphic__).
        asm_code.metamorphic_funcs = set()
        asm_code.metamorphic_current = None

        # -O4 near-function scratch: per-function static spill/local storage.
        self._near_active = False        # is the current function using it?
        self._near_label = None          # its scratch buffer symbol
        self._near_off = 0               # next free offset within the buffer
        self._near_size = 0              # high-water mark for this function

    def make_asm(self):
        """Generate ASM code."""
        # Multi-target dispatch. The x86-64 path below is the original, fully
        # featured back end. arm64 routes to a separate, minimal lowering so the
        # x86 path stays byte-for-byte untouched while the aarch64 back end grows
        # (Stage 2: return an integer literal; later stages add real codegen).
        if self.asm_code.target.name == "arm64":
            return self._make_asm_arm64()
        if self.asm_code.target.name == "riscv64":
            return self._make_asm_riscv64()
        if self.asm_code.target.name == "m68k":
            return self._make_asm_m68k()
        if self.asm_code.target.name == "wasm":
            return self._make_asm_wasm()

        global_spotmap = self._get_global_spotmap()

        # If anything was packed, declare the memory mirror of the SIMD reg.
        if self.simd_pack.active:
            self.simd_pack.emit_store_decl(
                self.asm_code, shared=self._simd_pack_shared)

        # Expose the metamorphic function set so Call can emit the patch+jmp
        # sequence for calls to them.
        metamorphic_funcs = getattr(self.il_code, "metamorphic_funcs", set())
        self.asm_code.metamorphic_funcs = metamorphic_funcs
        near_scratch_funcs = getattr(self.il_code, "near_scratch_funcs", set())

        for func in self.il_code.commands:
            # Thread register partitioning: restrict this function's allocatable
            # register pool to its group's budget, so left/right threads use
            # disjoint registers and the generated context switcher is minimal.
            self._apply_thread_budget(func)

            is_meta = func in metamorphic_funcs
            if is_meta:
                # Metamorphic functions return through a per-function slot that
                # the caller patches with the return address. The slot lives in
                # writable *data* -- a separate page from any code, NOT next to
                # the function body.
                #
                # The old layout put the slot in a writable+executable .mtext
                # section immediately before the entry label. That placed the
                # slot in the same cache line the CPU was fetching the function
                # from, so the caller's store into it triggered a self-modifying
                # -code machine clear on every call -- ~two orders of magnitude
                # slower than an ordinary call/ret (measured in
                # tools/metamorphic/). Off the code page the store is ordinary
                # and the return reaches call/ret parity, and the body no longer
                # needs a writable+executable section at all.
                slot = func + "__metaret"
                self.asm_code.data.append(slot + ":")
                self.asm_code.data.append("\t.quad 0")
                self.asm_code.metamorphic_current = slot
            else:
                self.asm_code.metamorphic_current = None

            # -O4 near-function scratch: route this function's locals/spills
            # into a static per-function buffer instead of the stack.
            self._near_active = func in near_scratch_funcs
            self._near_label = func + "__scratch"
            self._near_off = 0
            self._near_size = 0

            # Each function gets its own stack frame, so the rbp-relative slot
            # offset must restart at 0 here. (Locals are also removed from the
            # shared spotmap after each function -- see _make_asm.) Without this
            # reset the offset accumulated across every function in the module,
            # so functions emitted late were given frames large enough to reach
            # slots sitting atop all earlier functions' dead space -- e.g. a
            # ~200-byte function reserving ~8 KB -- which overflowed the stack
            # on deep recursion (quicksort worst case, deep Collatz, etc.).
            self.offset = 0

            # A near-scratch leaf is meant to stay frameless (spilling into its
            # static buffer); using a callee-saved register would force a
            # save/restore frame, so keep these functions on caller-saved only.
            if self._near_active:
                cs = set(spots.callee_saved_registers)
                self.alloc_registers = [r for r in self.alloc_registers
                                        if r not in cs]
                self.all_registers = [r for r in self.all_registers
                                      if r not in cs]

            self.asm_code.add(asm_cmds.AsmLabel(func))

            # Contract-proven SIMD kernels get a hand-synthesized,
            # fallback-free SSE body instead of the normal scalar codegen.
            _simd_desc = getattr(self.il_code, "simd_proven", {})
            _simd_desc = _simd_desc.get(func) \
                if isinstance(_simd_desc, dict) else None
            if _simd_desc is not None:
                import shivyc.simd_contracts as simd_contracts
                if _simd_desc.get("kind") == "reduce":
                    if _simd_desc.get("elem") == "u8":
                        simd_contracts.synth_sse2_reduce_u8(
                            self.asm_code, None)
                    else:
                        simd_contracts.synth_sse2_reduce(self.asm_code, None)
                else:
                    simd_contracts.synth_sse_elementwise(
                        self.asm_code, _simd_desc)
            else:
                # Tell IL commands whether we are inside a hot/interrupt routine
                # (controls the zero-latency register read path).
                self.asm_code.simd_pack_hot = (
                    self.simd_pack.active and simd_pack.is_hot_function(func))
                self._cur_func_is_main = (func == "main")
                self._cur_func_name = func
                cmds = self.il_code.commands[func]
                if not getattr(self.arguments, "no_peephole", False):
                    import shivyc.peephole as peephole
                    # Reset the literal-registration log, run the peephole, then
                    # give a spot only to the literals it actually introduced
                    # (e.g. an induction-variable stride). Rescanning the whole
                    # program's literals here instead was O(functions x
                    # literals) -- the dominant quadratic cost in asm generation.
                    self.il_code.new_literals = []
                    cmds = peephole.optimize(cmds, self.il_code)
                    self.il_code.commands[func] = cmds
                    for v in self.il_code.new_literals:
                        if v not in global_spotmap:
                            global_spotmap[v] = LiteralSpot(
                                self.il_code.literals[v])
                self._make_asm(cmds, global_spotmap)

            # Declare the static scratch buffer (BSS) if this function used it.
            if self._near_active and self._near_size > 0:
                size = self._near_size + (-self._near_size % 16)  # 16-align
                self.asm_code.add_comm(self._near_label, size, True)
            self._near_active = False

    def _make_asm_arm64(self):
        """AArch64 (arm64) lowering -- Stage 3.

        Walks the same target-neutral IL the x86-64 back end consumes and emits
        AArch64 with a simple, correct memory/stack-machine model: every IL value
        gets a frame slot, and each operation loads its operands into scratch
        registers (w9/w10), computes, and stores the result back. This is naive
        -O0-class codegen (no register allocation yet -- that, and a real
        register/spot model, are a later optimization), but it is enough for
        locals, add/sub/mul, comparisons, and conditional branches: real `if`
        and `while`. Unsupported IL commands raise rather than miscompile.

        The x86-64 path is untouched; this runs only under `--target arm64`.
        """
        EXTERNAL = self.symbol_table.EXTERNAL
        DEFINED = self.symbol_table.DEFINED
        for v in self.symbol_table.linkages[EXTERNAL].values():
            if self.symbol_table.def_state.get(v) == DEFINED:
                self.asm_code.add_global(self.symbol_table.names[v])
        # value -> assembler symbol, for globals (static/file-scope storage),
        # and a dedup set so each global's storage is emitted once.
        self._arm64_glob = {}
        self._arm64_gemit = {}
        self._arm64_gaddr = {}
        self._arm64_freg = {}
        self._arm64_fltlit = {}
        self._arm64_fltlit_n = 0
        self._arm64_saved_int = []
        self._arm64_saved_fp = []
        self._arm64_int_save_off = {}
        self._arm64_fp_save_off = {}
        # String-literal storage. A literal lives at a symbol in .data, not in
        # any frame, so it is emitted once here and then treated exactly like
        # a global: `char *p = "hi"` is an AddrOf of that symbol. Names are
        # generated if the front end did not already intern one, and recorded
        # so every reference uses the same label.
        self._arm64_strlit = {}
        snum = 0
        for v in self.il_code.string_literals:
            nm = self.il_code.string_literal_names.get(v)
            if nm is None:
                nm = "__arm64str%d" % snum
                self.il_code.string_literal_names[v] = nm
            snum += 1
            elem_size = v.ctype.el.size if v.ctype.is_array() else 1
            self.asm_code.add_string_literal(
                nm, self.il_code.string_literals[v], elem_size)
            self._arm64_strlit[v] = nm
            # Also record it in the global map: the per-function value scan
            # skips anything carrying a `.literal` attribute, and a string
            # literal does, so it would never be registered there.
            self._arm64_glob[v] = nm
        for func in self.il_code.commands:
            # Thread register partitioning. The x86 path applies this in
            # make_asm, but make_asm dispatches to _make_asm_arm64 *before*
            # reaching that point, so it has to be applied here too -- without
            # it the budget was computed, written to JSON, passed on the
            # command line, and then quietly ignored.
            self._apply_thread_budget(func)
            self._arm64_function(func, self.il_code.commands[func])

    def _arm64_emit_global_storage(self, v):
        """Emit `.comm`/`.data` storage for a static/file-scope global `v`
        (once), mirroring the x86 path's _get_global_spotmap."""
        name = self.symbol_table.asm_name(v)
        if name in self._arm64_gemit:
            return
        self._arm64_gemit[name] = 1
        TENTATIVE = self.symbol_table.TENTATIVE
        INTERNAL = self.symbol_table.INTERNAL
        if self.symbol_table.def_state.get(v) == TENTATIVE:
            local = (self.symbol_table.linkage_type[v] == INTERNAL)
            self.asm_code.add_comm(name, v.ctype.size, local)
        elif v in self.il_code.static_block_inits:
            entries, total = self.il_code.static_block_inits[v]
            self.asm_code.add_data_block(name, entries, total)
        else:
            init_val = self.il_code.static_inits.get(v, 0)
            self.asm_code.add_data(name, v.ctype.size, init_val)

    def _arm64_function(self, func, cmds):
        """Register-allocate, emit prologue, lower each IL command, per func.

        Allocation is deliberately simple: each distinct non-literal IL value
        gets a dedicated callee-saved home register (x19-x28), and values beyond
        the 10 available registers spill to frame slots. Callee-saved homes are
        correct across calls for free (the callee preserves x19-x28), so nothing
        needs saving around a `bl`. Used callee registers are saved at entry and
        restored before every `ret`. This is not graph-colored allocation (no
        live-range reuse yet), but it keeps hot values in registers and removes
        the per-operation load/store churn of the earlier memory model."""
        import shivyc.il_cmds.control as control
        import shivyc.il_cmds.value as value_cmds
        # Pass 1: ordered distinct non-literal values; note whether we call.
        values = []
        seen = {}
        has_call = False
        for c in cmds:
            if isinstance(c, control.Call):
                has_call = True
            for v in c.inputs():
                if v is not None and getattr(v, "literal", None) is None \
                        and v not in seen:
                    seen[v] = 1
                    values.append(v)
            for v in c.outputs():
                if v is not None and getattr(v, "literal", None) is None \
                        and v not in seen:
                    seen[v] = 1
                    values.append(v)

        # A value whose address is taken, or that is an aggregate (too big for a
        # register), must live in memory so a real address exists / it fits.
        forced = {}
        for c in cmds:
            if isinstance(c, value_cmds.AddrOf) \
                    and not c.var.ctype.is_function():
                forced[c.var] = 1
        for v in values:
            if v.ctype.is_array() or v.ctype.is_struct_union() \
                    or v.ctype.size > 8:
                forced[v] = 1

        # Static / file-scope globals are not x29-relative; they live at a
        # symbol and are addressed with adrp/add. Record them and emit storage.
        STATIC = self.symbol_table.STATIC
        EXTERNAL = self.symbol_table.EXTERNAL
        glob = {}
        for v in values:
            if self.symbol_table.storage.get(v) == STATIC:
                glob[v] = self.symbol_table.asm_name(v)
                self._arm64_glob[v] = glob[v]
                self._arm64_emit_global_storage(v)
            elif self.symbol_table.linkage_type.get(v) == EXTERNAL \
                    and not v.ctype.is_function():
                # An `extern` object declared here but defined elsewhere --
                # including one defined only by a *linker script*, which is
                # how a bare-metal image reaches `__bss_start` or a page-table
                # region. It carries EXTERNAL linkage but no storage duration
                # (nothing in this translation unit allocates it), so it
                # matched neither this test nor the DEFINED check above and
                # fell through to the local path, where AddrOf handed back an
                # x29-relative frame address. That is a *silent* miscompile:
                # the code links, runs, and reads whatever is on the stack.
                # It lives at a symbol, so record it as a global -- but emit
                # no storage, since defining it here would preempt the real
                # definition.
                glob[v] = self.symbol_table.asm_name(v)
                self._arm64_glob[v] = glob[v]

        # Count how often each global is referenced; frequently-used ones get
        # their (link-time-invariant) address cached in a register for the whole
        # function instead of recomputing adrp/add at every access.
        gaccess = {}
        for c in cmds:
            for v in c.inputs():
                if v is not None and v in glob:
                    gaccess[v] = gaccess.get(v, 0) + 1
            for v in c.outputs():
                if v is not None and v in glob:
                    gaccess[v] = gaccess.get(v, 0) + 1

        # Use/def counts drive two peephole optimizations below.
        usecount = {}
        defcount = {}
        for c in cmds:
            for v in c.inputs():
                if v is not None:
                    usecount[v] = usecount.get(v, 0) + 1
            for v in c.outputs():
                if v is not None:
                    defcount[v] = defcount.get(v, 0) + 1

        # AAPCS64 splits parameters into separate integer (x0-x7) and FP (v0-v7)
        # sequences; map each LoadArg's positional arg_num to its register index
        # within the right file. (Walks LoadArgs in order; correct when params
        # are dense, i.e. no unused param before a used one of the other class.)
        self._arm64_arggp = {}
        self._arm64_argfp = {}
        agp = 0
        afp = 0
        # Parameters past the eighth of their class arrive on the caller's
        # stack, in order. Their offsets are recorded as a byte index and
        # biased by the frame size at use.
        self._arm64_argstk = {}
        astk = 0
        for c in cmds:
            if isinstance(c, value_cmds.LoadArg):
                if c.output.ctype.is_floating():
                    if afp >= 8:
                        self._arm64_argstk[c.arg_num] = astk
                        astk += 8
                    else:
                        self._arm64_argfp[c.arg_num] = afp
                    afp += 1
                else:
                    if agp >= 8:
                        self._arm64_argstk[c.arg_num] = astk
                        astk += 8
                    else:
                        self._arm64_arggp[c.arg_num] = agp
                    agp += 1

        # Copy coalescing: a `Set(out, tmp)` whose source is a single-use, single-
        # def temporary can share `out`'s home, so the defining op writes `out`
        # directly and the copy disappears. Only safe when `out`'s prior value is
        # not needed between tmp's definition and the copy (else, e.g., a swap
        # `t=a+b; a=b; b=t` would clobber b early); checked straight-line below.
        import shivyc.il_cmds.compare as cmp_cmds
        defidx = {}
        for idx in range(len(cmds)):
            for v in cmds[idx].outputs():
                if v is not None:
                    defidx[v] = idx
        coalesce = {}                      # tmp -> out (candidates)
        for k in range(len(cmds)):
            c = cmds[k]
            if isinstance(c, value_cmds.Set):
                arg = c.arg
                out = c.output
                if getattr(arg, "literal", None) is None \
                        and usecount.get(arg, 0) == 1 \
                        and defcount.get(arg, 0) == 1 \
                        and arg not in forced and out not in forced \
                        and arg not in glob and out not in glob \
                        and not out.ctype.is_struct_union() \
                        and not out.ctype.is_array() \
                        and out.ctype.size <= 8 \
                        and out.ctype.size == arg.ctype.size \
                        and out.ctype.is_floating() == arg.ctype.is_floating() \
                        and self._il_coalesce_safe(
                            cmds, defidx.get(arg, -1), k, out):
                    coalesce[arg] = out

        # Compare+branch fusion: a comparison whose result feeds only the next
        # JumpZero/JumpNotZero becomes `cmp ; b.<cc>` (no cset/cbz). Computed
        # before allocation so the never-materialized result gets no register.
        self._arm64_fuse = {}
        skip = {}
        fused_out = {}
        n = len(cmds)
        for idx in range(n):
            c = cmds[idx]
            if isinstance(c, cmp_cmds._GeneralCmp) and idx + 1 < n:
                out = c.outputs()[0]
                cins = c.inputs()
                if usecount.get(out, 0) == 1 \
                        and not cins[0].ctype.is_floating():
                    nxt = cmds[idx + 1]
                    if isinstance(nxt, control.JumpZero) and nxt.cond is out:
                        self._arm64_fuse[idx] = (nxt.label, False)
                        skip[idx + 1] = 1
                        fused_out[out] = 1
                    elif isinstance(nxt, control.JumpNotZero) \
                            and nxt.cond is out:
                        self._arm64_fuse[idx] = (nxt.label, True)
                        skip[idx + 1] = 1
                        fused_out[out] = 1

        # ---- Liveness-based linear-scan allocation -------------------------
        # Per-index use/def lists over register-allocatable, copy-coalesced
        # values (literals, globals, address-taken/aggregate values, and
        # fused-away compare results never occupy a general register).
        uses = []
        defs = []
        for idx in range(n):
            c = cmds[idx]
            u = []
            d = []
            for v in c.inputs():
                if v is not None and getattr(v, "literal", None) is None \
                        and v not in forced and v not in glob \
                        and v not in fused_out:
                    u.append(self._il_canon(v, coalesce))
            for v in c.outputs():
                if v is not None and getattr(v, "literal", None) is None \
                        and v not in forced and v not in glob \
                        and v not in fused_out:
                    d.append(self._il_canon(v, coalesce))
            uses.append(u)
            defs.append(d)
        live_in, live_out = self._il_liveness(cmds, n, uses, defs)

        # Live interval [start, end] per value, and whether it is live across a
        # call (=> needs a callee-saved home). Both are target-neutral.
        start, end, crosses = self._il_intervals(
            cmds, n, live_in, live_out, uses, defs)

        # Argument set-up for a call writes only x0..x<gp_max-1> / v0..v<fp_max-1>,
        # and incoming parameters arrive in x0..x<agp-1> / v0..v<afp-1>; caller-
        # saved homes are placed above both so neither shuffle can clobber them.
        gp_max = 0
        fp_max = 0
        for c in cmds:
            if isinstance(c, control.Call):
                g = 0
                fcnt = 0
                for a in c.args:
                    if a.ctype.is_floating():
                        fcnt += 1
                    else:
                        g += 1
                if g > gp_max:
                    gp_max = g
                if fcnt > fp_max:
                    fp_max = fcnt
        cs = gp_max
        if agp > cs:
            cs = agp
        if cs < 1:
            cs = 1
        int_caller = []
        rr = cs
        while rr <= 7:
            int_caller.append(rr)
            rr += 1
        int_callee = []
        rr = 19
        while rr <= 28:
            int_callee.append(rr)
            rr += 1
        # Thread partitioning: restrict this function's callee-saved homes to
        # its group's budget, so the two sides cannot land on the same
        # register and the emitted switcher really is minimal.
        _budget = getattr(self, "_a64_budget", None)
        if _budget:
            int_callee = [r for r in int_callee if r in _budget]
        fp_caller = []
        rr = 18                              # v18..v31: caller-saved, never args
        while rr <= 31:
            fp_caller.append(rr)
            rr += 1
        fp_callee = []
        rr = 8
        while rr <= 15:
            fp_callee.append(rr)
            rr += 1

        # Cached global addresses live the whole function and cross calls, so they
        # claim callee-saved registers first.
        self._arm64_gaddr = {}
        busy_int = {}
        busy_fp = {}
        used_int_callee = {}
        used_fp_callee = {}
        GCACHE_CAP = 3
        for v in values:
            if v in glob and gaccess.get(v, 0) >= 2 \
                    and len(self._arm64_gaddr) < GCACHE_CAP \
                    and len(int_callee) > 2:
                r = int_callee.pop(0)
                self._arm64_gaddr[v] = r
                busy_int[r] = n
                used_int_callee[r] = 1

        # Scan representative values in interval-start order; reuse a register
        # once its previous occupant's interval has ended.
        reps = {}
        order = []
        for v in values:
            cv = self._il_canon(v, coalesce)
            if cv is None or getattr(cv, "literal", None) is not None \
                    or cv in forced or cv in glob or cv in fused_out \
                    or cv in reps:
                continue
            reps[cv] = 1
            order.append(cv)
        order.sort(key=lambda vv: start.get(vv, 0))

        reg_of, freg_of, spill = self._il_linear_scan(
            order, start, end, crosses, int_caller, int_callee,
            fp_caller, fp_callee, busy_int, busy_fp,
            used_int_callee, used_fp_callee)
        self._arm64_freg = freg_of

        # Lay out the saved-register area, then spill slots for everything left
        # in memory (spilled values, address-taken locals, aggregates).
        saved_int = []
        for r in range(19, 29):
            if r in used_int_callee:
                saved_int.append(r)
        saved_fp = []
        for r in range(8, 16):
            if r in used_fp_callee:
                saved_fp.append(r)
        off = 16
        int_save_off = {}
        for r in saved_int:
            int_save_off[r] = off
            off += 8
        fp_save_off = {}
        for r in saved_fp:
            fp_save_off[r] = off
            off += 8
        slot_of = {}
        for v in values:
            cv = self._il_canon(v, coalesce)
            if cv in reg_of or cv in freg_of or v in glob or v in fused_out:
                continue
            if cv not in slot_of:
                sz = cv.ctype.size
                if sz < 8:
                    sz = 8
                sz = sz + (-sz % 8)        # round each slot up to 8 bytes
                slot_of[cv] = off
                off += sz
            if v is not cv:
                slot_of[v] = slot_of[cv]
        # Coalesced temps share their target's home; their copy is elided.
        for arg in coalesce:
            o = self._il_canon(arg, coalesce)
            if o in reg_of:
                reg_of[arg] = reg_of[o]
            if o in freg_of:
                freg_of[arg] = freg_of[o]
            if o in slot_of:
                slot_of[arg] = slot_of[o]
        for idx in range(n):
            c = cmds[idx]
            if isinstance(c, value_cmds.Set) and c.arg in coalesce:
                skip[idx] = 1

        frame = 0
        if len(saved_int) > 0 or len(saved_fp) > 0 or len(slot_of) > 0 \
                or has_call:
            frame = off + (-off % 16)      # 16-byte align
        self._arm64_saved_int = saved_int
        self._arm64_saved_fp = saved_fp
        self._arm64_int_save_off = int_save_off
        self._arm64_fp_save_off = fp_save_off

        self.asm_code.add(asm_cmds.AsmLabel(func))
        if frame:
            # stp's pre-index offset is a *scaled 7-bit* field: +/-512 bytes
            # for a pair of 64-bit registers. A larger frame -- easily reached
            # by a function with a sizeable local buffer -- has to lower sp
            # separately. x16/x17 are ABI scratch, and at entry only the
            # argument registers and x16 (the variadic block base) are live, so
            # x17 is free here.
            if frame <= 504:
                self.asm_code.add(asm_cmds.Raw(
                    "stp\tx29, x30, [sp, #-%d]!" % frame))
            else:
                self._arm64_mov_imm("x17", frame, 8)
                self.asm_code.add(asm_cmds.Raw("sub\tsp, sp, x17"))
                self.asm_code.add(asm_cmds.Raw("stp\tx29, x30, [sp]"))
            self.asm_code.add(asm_cmds.Raw("mov\tx29, sp"))
            for r in saved_int:
                self.asm_code.add(asm_cmds.Raw(
                    "str\tx%d, [x29, #%d]" % (r, int_save_off[r])))
            for r in saved_fp:
                self.asm_code.add(asm_cmds.Raw(
                    "str\td%d, [x29, #%d]" % (r, fp_save_off[r])))
        # Load cached global addresses once (callee-saved, so they survive calls).
        for v in values:
            if v in self._arm64_gaddr:
                r = self._arm64_gaddr[v]
                name = glob[v]
                self.asm_code.add(asm_cmds.Raw("adrp\tx%d, %s" % (r, name)))
                self.asm_code.add(asm_cmds.Raw(
                    "add\tx%d, x%d, :lo12:%s" % (r, r, name)))
        addrof_name = {}
        for idx in range(n):
            if idx in skip:
                continue
            self._lower_arm64(cmds[idx], idx, func, reg_of, slot_of,
                              0, frame, addrof_name)

    def _il_coalesce_safe(self, cmds, dk, k, out):
        """True if coalescing the copy at index k into `out` is safe: tmp is
        defined at index dk, dk..k is one straight-line block, and `out` is not
        referenced in [dk, k) (so out's prior value is dead and the defining op
        may write out directly). Prevents miscompiling swaps like
        `t=a+b; a=b; b=t`."""
        import shivyc.il_cmds.control as control
        if dk < 0 or dk > k:
            return False
        j = dk
        while j < k:
            cj = cmds[j]
            if isinstance(cj, control.Label) or isinstance(cj, control.Jump) \
                    or isinstance(cj, control.JumpZero) \
                    or isinstance(cj, control.JumpNotZero) \
                    or isinstance(cj, control.Return):
                return False
            for v in cj.inputs():
                if v is out:
                    return False
            for v in cj.outputs():
                if v is out:
                    return False
            j += 1
        return True

    def _il_intervals(self, cmds, n, live_in, live_out, uses, defs):
        """Per-value conservative live interval [start, end] (min/max live index,
        safe across loop back-edges) plus a `crosses` set of values live across
        some call (live in both live_in and live_out at a Call index). All three
        are architecture-neutral and shared by every back end's allocator."""
        import shivyc.il_cmds.control as control
        start = {}
        end = {}
        crosses = {}
        for idx in range(n):
            here = []
            for v in live_in[idx]:
                here.append(v)
            for v in live_out[idx]:
                here.append(v)
            for v in defs[idx]:
                here.append(v)
            for v in uses[idx]:
                here.append(v)
            for v in here:
                if v not in start or idx < start[v]:
                    start[v] = idx
                if v not in end or idx > end[v]:
                    end[v] = idx
            if isinstance(cmds[idx], control.Call):
                for v in live_out[idx]:
                    if v in live_in[idx]:
                        crosses[v] = 1
        return start, end, crosses

    def _il_linear_scan(self, order, start, end, crosses,
                        int_caller, int_callee, fp_caller, fp_callee,
                        busy_int, busy_fp, used_int_callee, used_fp_callee):
        """Architecture-neutral linear-scan core. Assigns each value in `order`
        (sorted by interval start) a register from the supplied pools: a value
        live across a call takes a callee-saved register; a call-clean value
        prefers a caller-saved one (no save) and falls back to callee, else
        spills. The pools and ABI facts are passed in by the target back end;
        the mechanism here is shared. Returns (reg_of, freg_of, spill); the
        used_*_callee maps are updated in place to drive prologue saves."""
        reg_of = {}
        freg_of = {}
        spill = {}
        for v in order:
            s = start.get(v, 0)
            e = end.get(v, s)
            if v.ctype.is_floating():
                if v in crosses:
                    r = self._il_pick(fp_callee, busy_fp, s)
                    if r >= 0:
                        used_fp_callee[r] = 1
                else:
                    r = self._il_pick(fp_caller, busy_fp, s)
                    if r < 0:
                        r = self._il_pick(fp_callee, busy_fp, s)
                        if r >= 0:
                            used_fp_callee[r] = 1
                if r >= 0:
                    freg_of[v] = r
                    busy_fp[r] = e
                else:
                    spill[v] = 1
            else:
                if v in crosses:
                    r = self._il_pick(int_callee, busy_int, s)
                    if r >= 0:
                        used_int_callee[r] = 1
                else:
                    r = self._il_pick(int_caller, busy_int, s)
                    if r < 0:
                        r = self._il_pick(int_callee, busy_int, s)
                        if r >= 0:
                            used_int_callee[r] = 1
                if r >= 0:
                    reg_of[v] = r
                    busy_int[r] = e
                else:
                    spill[v] = 1
        return reg_of, freg_of, spill

    def _il_canon(self, v, coalesce):
        """Follow the copy-coalescing chain v -> ... so coalesced temps resolve
        to the value whose home they share."""
        seen = {}
        while v in coalesce and v not in seen:
            seen[v] = 1
            v = coalesce[v]
        return v

    def _il_pick(self, pool, busy, s):
        """First register in `pool` free at index `s` (its last occupant ended
        before s), or -1 if none is free."""
        for rr in pool:
            if busy.get(rr, -1) < s:
                return rr
        return -1

    def _il_liveness(self, cmds, n, uses, defs):
        """Backward live-variable fixpoint over the per-index `uses`/`defs`
        value lists. Returns (live_in, live_out), each a list of {value: 1}."""
        import shivyc.il_cmds.control as control
        labelidx = {}
        for i in range(n):
            c = cmds[i]
            if isinstance(c, control.Label):
                labelidx[c.label] = i
        succ = []
        for i in range(n):
            c = cmds[i]
            s = []
            if isinstance(c, control.Return):
                pass                          # no successors
            elif isinstance(c, control.Jump):
                t = labelidx.get(c.label, -1)
                if t >= 0:
                    s = [t]
            elif isinstance(c, control.JumpZero) \
                    or isinstance(c, control.JumpNotZero):
                if i + 1 < n:
                    s.append(i + 1)
                t = labelidx.get(c.label, -1)
                if t >= 0:
                    s.append(t)
            else:
                if i + 1 < n:
                    s = [i + 1]
            succ.append(s)
        live_in = []
        live_out = []
        for i in range(n):
            live_in.append({})
            live_out.append({})
        changed = True
        while changed:
            changed = False
            for i in range(n - 1, -1, -1):
                lo = {}
                for sidx in succ[i]:
                    for v in live_in[sidx]:
                        lo[v] = 1
                li = {}
                for v in lo:
                    li[v] = 1
                for v in defs[i]:
                    if v in li:
                        del li[v]
                for v in uses[i]:
                    li[v] = 1
                if len(li) != len(live_in[i]) or len(lo) != len(live_out[i]):
                    changed = True
                else:
                    for v in li:
                        if v not in live_in[i]:
                            changed = True
                            break
                    for v in lo:
                        if v not in live_out[i]:
                            changed = True
                            break
                live_in[i] = li
                live_out[i] = lo
        return live_in, live_out

    def _arm64_epilogue(self, nreg, frame):
        """Restore the callee-saved registers this function actually used, tear
        down the frame, and return."""
        for r in self._arm64_saved_int:
            self.asm_code.add(asm_cmds.Raw(
                "ldr\tx%d, [x29, #%d]" % (r, self._arm64_int_save_off[r])))
        for r in self._arm64_saved_fp:
            self.asm_code.add(asm_cmds.Raw(
                "ldr\td%d, [x29, #%d]" % (r, self._arm64_fp_save_off[r])))
        if frame:
            if frame <= 504:
                self.asm_code.add(asm_cmds.Raw(
                    "ldp\tx29, x30, [sp], #%d" % frame))
            else:
                self.asm_code.add(asm_cmds.Raw("ldp\tx29, x30, [sp]"))
                self._arm64_mov_imm("x17", frame, 8)
                self.asm_code.add(asm_cmds.Raw("add\tsp, sp, x17"))
        self.asm_code.add(asm_cmds.Raw("ret"))

    def _arm64_frn(self, regnum, value):
        """FP register name of the right width for `value` (s<n> for 4-byte
        float, d<n> for 8-byte double)."""
        if value is not None and value.ctype.size == 4:
            return "s%d" % regnum
        return "d%d" % regnum

    def _arm64_float_label(self, value):
        """Emit the data for float literal `value` once and return its label."""
        name = self._arm64_fltlit.get(value)
        if name is not None:
            return name
        import struct
        val = self.il_code.float_literals[value]
        name = "__a64flt%d" % self._arm64_fltlit_n
        if value.ctype.size == 4:
            bits = struct.unpack("<I", struct.pack("<f", val))[0]
            self.asm_code.add_data(name, 4, bits)
        else:
            bits = struct.unpack("<Q", struct.pack("<d", val))[0]
            self.asm_code.add_data(name, 8, bits)
        self._arm64_fltlit_n += 1
        self._arm64_fltlit[value] = name
        return name

    def _arm64_fload_lit(self, value, fn):
        """Load float literal `value` into FP register number <fn>. Uses
        adrp/add to form the address so the `ldr` needs no `:lo12:` relocation
        (which would require the literal to be naturally aligned)."""
        name = self._arm64_float_label(value)
        self.asm_code.add(asm_cmds.Raw("adrp\tx9, %s" % name))
        self.asm_code.add(asm_cmds.Raw("add\tx9, x9, :lo12:%s" % name))
        self.asm_code.add(asm_cmds.Raw(
            "ldr\t%s, [x9]" % self._arm64_frn(fn, value)))

    def _arm64_floatuse(self, value, scratch, slot_of):
        """Return an FP register name holding float `value`: its home (no code),
        a load from its slot/global, or a loaded literal -> v<scratch>."""
        if value in self.il_code.float_literals:
            self._arm64_fload_lit(value, scratch)
            return self._arm64_frn(scratch, value)
        r = self._arm64_freg.get(value, -1)
        if r >= 0:
            return self._arm64_frn(r, value)
        target = self._arm64_mem_addr(value, 9, slot_of)
        name = self._arm64_frn(scratch, value)
        self.asm_code.add(asm_cmds.Raw("ldr\t%s, %s" % (name, target)))
        return name

    def _arm64_fdefreg(self, value, scratch):
        """FP register to write float `value` into: home, else v<scratch>."""
        r = self._arm64_freg.get(value, -1)
        if r >= 0:
            return self._arm64_frn(r, value)
        return self._arm64_frn(scratch, value)

    def _arm64_fwb(self, value, scratch, slot_of):
        """Store FP scratch back to float `value`'s slot/global, if not a home."""
        if self._arm64_freg.get(value, -1) < 0:
            target = self._arm64_mem_addr(value, 15, slot_of)
            self.asm_code.add(asm_cmds.Raw(
                "str\t%s, %s" % (self._arm64_frn(scratch, value), target)))

    def _arm64_finto(self, value, n, slot_of):
        """Force float `value` into FP register number <n> (call args/return)."""
        name = self._arm64_frn(n, value)
        if value in self.il_code.float_literals:
            self._arm64_fload_lit(value, n)
            return
        r = self._arm64_freg.get(value, -1)
        if r >= 0:
            src = self._arm64_frn(r, value)
            if src != name:
                self.asm_code.add(asm_cmds.Raw("fmov\t%s, %s" % (name, src)))
            return
        target = self._arm64_mem_addr(value, 9, slot_of)
        self.asm_code.add(asm_cmds.Raw("ldr\t%s, %s" % (name, target)))

    def _arm64_ffrom(self, n, value, slot_of):
        """Store FP register number <n> into float `value`'s home/slot/global
        (LoadArg / call return value)."""
        src = self._arm64_frn(n, value)
        r = self._arm64_freg.get(value, -1)
        if r >= 0:
            dst = self._arm64_frn(r, value)
            if dst != src:
                self.asm_code.add(asm_cmds.Raw("fmov\t%s, %s" % (dst, src)))
        else:
            target = self._arm64_mem_addr(value, 15, slot_of)
            self.asm_code.add(asm_cmds.Raw("str\t%s, %s" % (src, target)))

    def _arm64_rn(self, regnum, value):
        """Register name of the right width for `value` (w<n> for <=4 bytes,
        x<n> otherwise)."""
        if value is not None and value.ctype.size > 4:
            return "x%d" % regnum
        return "w%d" % regnum

    def _arm64_frame_ref(self, off, areg):
        """Return a memory operand addressing `off` bytes into the frame.

        The unsigned-offset load/store forms carry a 12-bit field, so an offset
        past 4095 -- a function with a local buffer of a few KB reaches that
        easily -- has to be folded into a register first. Returns `[x29, #off]`
        when it fits and `[x<areg>]` otherwise, emitting the address
        computation.
        """
        if 0 <= off <= 4095:
            return "[x29, #%d]" % off
        a = "x%d" % areg
        self._arm64_mov_imm(a, off, 8)
        self.asm_code.add(asm_cmds.Raw("add\t%s, x29, %s" % (a, a)))
        return "[%s]" % a

    def _arm64_frame_addr_into(self, dest, off, scratch):
        """Emit `dest = x29 + off`, materialising the offset when `add`'s
        12-bit immediate cannot hold it."""
        if 0 <= off <= 4095:
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, x29, #%d" % (dest, off)))
            return
        t = "x%d" % scratch
        self._arm64_mov_imm(t, off, 8)
        self.asm_code.add(asm_cmds.Raw("add\t%s, x29, %s" % (dest, t)))

    def _arm64_mem_addr(self, value, areg, slot_of):
        """Addressing operand for a memory-resident `value`. For a local it is
        `[x29, #slot]` (no code emitted). For a global it emits adrp/add of the
        symbol into x<areg> and returns `[x<areg>]`."""
        name = self._arm64_glob.get(value)
        if name is not None:
            cr = self._arm64_gaddr.get(value, -1)
            if cr >= 0:
                return "[x%d]" % cr      # address cached in a register
            a = "x%d" % areg
            self.asm_code.add(asm_cmds.Raw("adrp\t%s, %s" % (a, name)))
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, %s, :lo12:%s" % (a, a, name)))
            return "[%s]" % a
        return self._arm64_frame_ref(slot_of[value], areg)

    def _arm64_use(self, value, scratch, reg_of, slot_of):
        """Return a register name holding `value`, emitting a load if needed:
        its home register (no code), a `mov` for a literal, or an `ldr` from its
        spill slot into scratch register <scratch>."""
        lit = getattr(value, "literal", None)
        if lit is not None:
            name = self._arm64_rn(scratch, value)
            self._arm64_mov_imm(name, lit.val, value.ctype.size)
            return name
        r = reg_of.get(value, -1)
        if r >= 0:
            return self._arm64_rn(r, value)
        target = self._arm64_mem_addr(value, scratch, slot_of)
        name = self._arm64_rn(scratch, value)
        op = self._arm64_ldr_op(value.ctype.size, self._arm64_signed(value))
        self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, name, target)))
        return name

    def _arm64_defreg(self, value, scratch, reg_of):
        """Register to write `value`'s result into: its home register, else the
        scratch register <scratch> (a writeback to its slot follows)."""
        r = reg_of.get(value, -1)
        if r >= 0:
            return self._arm64_rn(r, value)
        return self._arm64_rn(scratch, value)

    def _arm64_wb(self, value, scratch, reg_of, slot_of):
        """Store scratch <scratch> back to `value`'s home, if it is not in a
        register (spilled local or global). x15 holds a global's address so it
        does not clobber the result in <scratch>."""
        if reg_of.get(value, -1) < 0:
            target = self._arm64_mem_addr(value, 15, slot_of)
            op = self._arm64_str_op(value.ctype.size)
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, %s" % (op, self._arm64_rn(scratch, value), target)))

    def _arm64_into(self, value, n, reg_of, slot_of):
        """Force `value` into a specific register number <n> (for call args and
        the return value, which must land in w/x0-x7)."""
        name = self._arm64_rn(n, value)
        lit = getattr(value, "literal", None)
        if lit is not None:
            self._arm64_mov_imm(name, lit.val, value.ctype.size)
            return
        r = reg_of.get(value, -1)
        if r >= 0:
            src = self._arm64_rn(r, value)
            if src != name:
                self.asm_code.add(asm_cmds.Raw("mov\t%s, %s" % (name, src)))
            return
        target = self._arm64_mem_addr(value, n, slot_of)
        op = self._arm64_ldr_op(value.ctype.size, self._arm64_signed(value))
        self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, name, target)))

    def _arm64_from(self, n, value, reg_of, slot_of):
        """Store register number <n> into `value`'s home (for LoadArg and the
        call return value)."""
        src = self._arm64_rn(n, value)
        r = reg_of.get(value, -1)
        if r >= 0:
            dst = self._arm64_rn(r, value)
            if dst != src:
                self.asm_code.add(asm_cmds.Raw("mov\t%s, %s" % (dst, src)))
        else:
            target = self._arm64_mem_addr(value, 15, slot_of)
            op = self._arm64_str_op(value.ctype.size)
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, src, target)))

    def _arm64_wname(self, name):
        """The 32-bit (w) form of a register name (x12 -> w12); used when an
        instruction needs the low word of a value (e.g. an sxtw source)."""
        if name and name[0] == "x":
            return "w" + name[1:]
        return name

    def _arm64_xname(self, name):
        """The 64-bit (x) form of a register name (w12 -> x12); used for a shift
        amount of a 64-bit value (only the low bits matter, so reading the wide
        register is safe)."""
        if name and name[0] == "w":
            return "x" + name[1:]
        return name

    def _arm64_pow2_log(self, n):
        """log2(n) if n is a power of two in 1..16, else -1 (the lsl shift amount
        for scaling an array index by the element size)."""
        i = 0
        v = 1
        while i <= 4:
            if v == n:
                return i
            v = v * 2
            i += 1
        return -1

    def _arm64_signed(self, value):
        """Whether `value` needs a sign-extending sub-word load. Only 1/2-byte
        integers care; wider or non-integer types (e.g. pointers, which have no
        `signed` attribute) report False safely via the size short-circuit."""
        if value.ctype.size <= 2:
            return value.ctype.signed
        return False

    def _arm64_ldr_op(self, size, signed):
        """Load mnemonic for a `size`-byte value (sign- or zero-extending the
        sub-word forms)."""
        if size == 1:
            return "ldrsb" if signed else "ldrb"
        if size == 2:
            return "ldrsh" if signed else "ldrh"
        return "ldr"

    def _arm64_str_op(self, size):
        """Store mnemonic for a `size`-byte value."""
        if size == 1:
            return "strb"
        if size == 2:
            return "strh"
        return "str"

    def _arm64_rel_target(self, base, chunk, count, an, reg_of, slot_of):
        """Return an AArch64 memory operand `[...]` addressing base + chunk*count
        (or base + chunk when count is None), emitting any address computation
        into scratch registers x<an>.. . `base` is either array storage (its
        address is x29 + slot) or a pointer value."""
        base_is_mem = base.ctype.is_array() or base.ctype.is_struct_union()
        gname = self._arm64_glob.get(base)
        gcR = self._arm64_gaddr.get(base, -1)
        # Constant total offset? (no count -> fixed chunk byte offset; literal
        # count -> chunk*index).
        const_off = None
        if count is None:
            const_off = chunk
        else:
            lit = getattr(count, "literal", None)
            if lit is not None:
                const_off = chunk * lit.val
        if const_off is not None:
            if gcR >= 0:
                if const_off == 0:
                    return "[x%d]" % gcR
                return "[x%d, #%d]" % (gcR, const_off)
            if gname is not None:
                a = "x%d" % an
                self.asm_code.add(asm_cmds.Raw("adrp\t%s, %s" % (a, gname)))
                self.asm_code.add(asm_cmds.Raw(
                    "add\t%s, %s, :lo12:%s" % (a, a, gname)))
                if const_off == 0:
                    return "[%s]" % a
                return "[%s, #%d]" % (a, const_off)
            if base_is_mem:
                return self._arm64_frame_ref(
                    slot_of[base] + const_off, an)
            rb = self._arm64_use(base, an, reg_of, slot_of)
            if const_off == 0:
                return "[%s]" % rb
            return "[%s, #%d]" % (rb, const_off)
        # Variable index: compute the effective address into x<an>. `bsrc` is the
        # base address; for a cached global it stays in its register (xR) and is
        # not clobbered.
        addr = "x%d" % an
        if gcR >= 0:
            bsrc = "x%d" % gcR
        elif gname is not None:
            self.asm_code.add(asm_cmds.Raw("adrp\t%s, %s" % (addr, gname)))
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, %s, :lo12:%s" % (addr, addr, gname)))
            bsrc = addr
        elif base_is_mem:
            self._arm64_frame_addr_into(addr, slot_of[base], an)
            bsrc = addr
        else:
            self._arm64_into(base, an, reg_of, slot_of)   # pointer value -> x<an>
            bsrc = addr
        ci = self._arm64_use(count, an + 1, reg_of, slot_of)
        self._arm64_scale_index(an, bsrc, ci, chunk, count.ctype.size <= 4)
        return "[%s]" % addr

    def _arm64_addr_into(self, base, chunk, count, an, reg_of, slot_of):
        """Compute the effective address base + chunk*count (or base + chunk when
        count is None) into register x<an>, returning its name. Like
        _arm64_rel_target but materializing the address (for AddrRel / &a[i])."""
        addr = "x%d" % an
        gname = self._arm64_glob.get(base)
        gcR = self._arm64_gaddr.get(base, -1)
        if gcR >= 0:
            bsrc = "x%d" % gcR
        elif gname is not None:
            self.asm_code.add(asm_cmds.Raw("adrp\t%s, %s" % (addr, gname)))
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, %s, :lo12:%s" % (addr, addr, gname)))
            bsrc = addr
        elif base.ctype.is_array() or base.ctype.is_struct_union():
            self._arm64_frame_addr_into(addr, slot_of[base], an)
            bsrc = addr
        else:
            self._arm64_into(base, an, reg_of, slot_of)   # pointer value -> addr
            bsrc = addr
        if count is None:
            if chunk:
                self.asm_code.add(asm_cmds.Raw(
                    "add\t%s, %s, #%d" % (addr, bsrc, chunk)))
            elif bsrc != addr:
                self.asm_code.add(asm_cmds.Raw("mov\t%s, %s" % (addr, bsrc)))
            return addr
        lit = getattr(count, "literal", None)
        if lit is not None:
            off = chunk * lit.val
            if off:
                self.asm_code.add(asm_cmds.Raw(
                    "add\t%s, %s, #%d" % (addr, bsrc, off)))
            elif bsrc != addr:
                self.asm_code.add(asm_cmds.Raw("mov\t%s, %s" % (addr, bsrc)))
            return addr
        ci = self._arm64_use(count, an + 1, reg_of, slot_of)
        self._arm64_scale_index(an, bsrc, ci, chunk, count.ctype.size <= 4)
        return addr

    def _arm64_agg_base(self, value, an, slot_of):
        """Return (base register name, offset) addressing aggregate `value`.

        A global's address is materialised into x<an>; a frame-homed aggregate
        is already addressable off x29, so no instruction is needed."""
        gname = self._arm64_glob.get(value)
        if gname is not None:
            reg = "x%d" % an
            self.asm_code.add(asm_cmds.Raw("adrp\t%s, %s" % (reg, gname)))
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, %s, :lo12:%s" % (reg, reg, gname)))
            return reg, 0
        return "x29", slot_of[value]

    def _arm64_scale_index(self, an, bsrc, ci, chunk, narrow):
        """Emit `x<an> = bsrc + ci*chunk` for a variable index.

        Takes the destination as a register *number* and the index width as a
        flag rather than a name and an ILValue: the transpiler infers each
        parameter's C type from its uses, and a name shared with an int-typed
        parameter elsewhere makes it pick the wrong one.

        A power-of-two element size folds the scale into the addressing mode's
        shift. Anything else -- a struct of 12 or 20 bytes, say -- needs a real
        multiply, so the size goes into a scratch register and the index is
        multiplied by it. x<an+2> is free here: an+1 already holds the index
        and both are scratch, never value homes.
        """
        addr = "x%d" % an
        sh = self._arm64_pow2_log(chunk)
        if sh >= 0:
            ext = "sxtw" if narrow else "lsl"
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, %s, %s, %s #%d" % (addr, bsrc, ci, ext, sh)))
            return
        idx = "x%d" % (an + 1)
        # NOTE: unexercised. This branch replaces a NotImplementedError, but no
        # C program has been found that reaches it -- the front end decomposes
        # struct-array indexing into a multiply plus a byte-offset AddrRel with
        # chunk 1, so `chunk` is always a power of two here in practice. Kept
        # because it is correct by construction and cheaper than a raise if the
        # IL shape ever changes; not claimed as tested.
        if narrow:
            # Sign-extend the 32-bit index before multiplying: the product is
            # a 64-bit byte offset, and `mul` has no extending form.
            self.asm_code.add(asm_cmds.Raw("sxtw\t%s, %s" % (idx, ci)))
        elif idx != ci:
            self.asm_code.add(asm_cmds.Raw("mov\t%s, %s" % (idx, ci)))
        scale = "x%d" % (an + 2)
        self._arm64_mov_imm(scale, chunk, 8)
        self.asm_code.add(asm_cmds.Raw(
            "mul\t%s, %s, %s" % (idx, idx, scale)))
        self.asm_code.add(asm_cmds.Raw(
            "add\t%s, %s, %s" % (addr, bsrc, idx)))

    def _arm64_mov_imm(self, dest, val, size):
        """Materialize integer literal `val` into register `dest`. AArch64 cannot
        move an arbitrary wide immediate in one instruction, so values outside a
        single mov's range are built with movz + movk over 16-bit chunks (sign
        bits fall out of the masked shifts for negatives)."""
        if -65536 <= val <= 65535:
            self.asm_code.add(asm_cmds.Raw("mov\t%s, #%d" % (dest, val)))
            return
        self.asm_code.add(asm_cmds.Raw(
            "movz\t%s, #%d" % (dest, val & 0xffff)))
        if size <= 4:
            hi = (val >> 16) & 0xffff
            if hi != 0:
                self.asm_code.add(asm_cmds.Raw(
                    "movk\t%s, #%d, lsl #16" % (dest, hi)))
        else:
            sh = 16
            while sh < 64:
                part = (val >> sh) & 0xffff
                if part != 0:
                    self.asm_code.add(asm_cmds.Raw(
                        "movk\t%s, #%d, lsl #%d" % (dest, part, sh)))
                sh += 16

    def _arm64_imm(self, value):
        """The literal value of `value` if it fits an AArch64 add/sub/cmp 12-bit
        unsigned immediate (so it can be folded as `#imm`), else -1."""
        lit = getattr(value, "literal", None)
        if lit is not None and 0 <= lit.val <= 4095:
            return lit.val
        return -1

    def _arm64_invert_cc(self, cc):
        """The opposite AArch64 condition code (for fusing a comparison whose
        negation is the branch condition)."""
        pairs = {"eq": "ne", "ne": "eq", "lt": "ge", "ge": "lt",
                 "gt": "le", "le": "gt", "lo": "hs", "hs": "lo",
                 "hi": "ls", "ls": "hi"}
        return pairs[cc]

    def _arm64_fcmp_cc(self, cmd):
        """AArch64 condition code after `fcmp` for floating comparison `cmd`
        (ordered; unordered/NaN compares false)."""
        import shivyc.il_cmds.compare as cmp_cmds
        if isinstance(cmd, cmp_cmds.EqualCmp):
            return "eq"
        if isinstance(cmd, cmp_cmds.NotEqualCmp):
            return "ne"
        if isinstance(cmd, cmp_cmds.LessCmp):
            return "mi"
        if isinstance(cmd, cmp_cmds.GreaterCmp):
            return "gt"
        if isinstance(cmd, cmp_cmds.LessOrEqCmp):
            return "ls"
        if isinstance(cmd, cmp_cmds.GreaterOrEqCmp):
            return "ge"
        return "eq"

    def _arm64_cmp_cc(self, cmd, signed):
        """AArch64 condition code (for `cset`) implementing comparison `cmd`."""
        import shivyc.il_cmds.compare as cmp_cmds
        if isinstance(cmd, cmp_cmds.EqualCmp):
            return "eq"
        if isinstance(cmd, cmp_cmds.NotEqualCmp):
            return "ne"
        if isinstance(cmd, cmp_cmds.LessCmp):
            return "lt" if signed else "lo"
        if isinstance(cmd, cmp_cmds.GreaterCmp):
            return "gt" if signed else "hi"
        if isinstance(cmd, cmp_cmds.LessOrEqCmp):
            return "le" if signed else "ls"
        if isinstance(cmd, cmp_cmds.GreaterOrEqCmp):
            return "ge" if signed else "hs"
        return "eq"

    def _lower_arm64(self, cmd, idx, func, reg_of, slot_of, nreg, frame,
                     addrof_name):
        """Lower a single IL command to AArch64 (register-allocated)."""
        import shivyc.il_cmds.control as control
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.math as math_cmds
        import shivyc.il_cmds.compare as cmp_cmds

        if isinstance(cmd, value_cmds.LoadStructArg):
            # A struct parameter too big for a register on SysV. Here every
            # struct parameter arrives the same way -- as the address of the
            # caller's object -- so this is the aggregate LoadArg case again:
            # copy it into our own frame to make the parameter by value.
            pidx = self._wasm_argmap.get(id(cmd), 0)
            self._wasm_push_addr(cmd.output, body)
            body.local_get(pidx)
            body.const_i32(cmd.output.ctype.size)
            body.memory_copy()
            return

        if isinstance(cmd, value_cmds.VaSaveBase):
            # The caller left the base of the all-argument block in x16. That
            # register is caller-saved scratch, so it is only meaningful at
            # entry -- copy it to an ordinary home before anything else can
            # clobber it, which is exactly what the x86-64 back end does with
            # r11.
            rd = self._arm64_defreg(cmd.output, 9, reg_of)
            self.asm_code.add(asm_cmds.Raw(
                "mov\t%s, x16" % self._arm64_xname(rd)))
            self._arm64_wb(cmd.output, 9, reg_of, slot_of)
            return

        if isinstance(cmd, value_cmds.VaStartAddr):
            # Address of the first variadic argument: the block holds every
            # argument in order, so skip the named ones.
            if cmd.base is None:
                raise NotImplementedError(
                    "arm64 back end: va_start without a caller-provided "
                    "argument block is not implemented")
            rb = self._arm64_use(cmd.base, 9, reg_of, slot_of)
            rd = self._arm64_defreg(cmd.output, 10, reg_of)
            off = 8 * cmd.named_count
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, %s, #%d"
                % (self._arm64_xname(rd), self._arm64_xname(rb), off)))
            self._arm64_wb(cmd.output, 10, reg_of, slot_of)
            return

        if isinstance(cmd, value_cmds.LoadArg):
            # Parameter arrives in its AAPCS64 register (x<gp> for integers,
            # v<fp> for floats); move/spill it to its home before any call.
            if cmd.arg_num in self._arm64_argstk:
                # Past the eighth of its class, so the caller left it just
                # above our frame: the prologue's `stp x29, x30, [sp, #-frame]!`
                # moved sp down by `frame` and x29 points at the new bottom, so
                # the caller's outgoing area starts at x29 + frame.
                # A frameless function never ran the prologue, so x29 still
                # holds the *caller's* frame pointer; sp is unchanged from
                # entry and the incoming area starts right at it.
                if frame:
                    base = "x29"
                    off = frame + self._arm64_argstk[cmd.arg_num]
                else:
                    base = "sp"
                    off = self._arm64_argstk[cmd.arg_num]
                out = cmd.output
                if out.ctype.is_floating():
                    rd = self._arm64_fdefreg(out, 16)
                    self.asm_code.add(asm_cmds.Raw(
                        "ldr\t%s, [%s, #%d]" % (rd, base, off)))
                    self._arm64_fwb(out, 16, slot_of)
                else:
                    rd = self._arm64_defreg(out, 9, reg_of)
                    self.asm_code.add(asm_cmds.Raw(
                        "ldr\t%s, [%s, #%d]"
                        % (self._arm64_xname(rd), base, off)))
                    self._arm64_wb(out, 9, reg_of, slot_of)
                return
            if cmd.output.ctype.is_floating():
                self._arm64_ffrom(
                    self._arm64_argfp[cmd.arg_num], cmd.output, slot_of)
            else:
                self._arm64_from(
                    self._arm64_arggp[cmd.arg_num], cmd.output, reg_of, slot_of)
            return
        if isinstance(cmd, control.Call):
            # A statically known callee reaches us one of two ways: an
            # explicit AddrOf of the function (older IL shape), or
            # Call.direct_name, set by the stackless-calls pass, which elides
            # that AddrOf entirely. Consult both, else it is a real indirect
            # call through a function pointer.
            name = addrof_name.get(cmd.func)
            if name is None:
                name = cmd.direct_name
            # AAPCS64 passes the first eight of each class in registers and
            # the rest on the stack, in order, at [sp+0], [sp+8], ... The frame
            # is addressed off x29, so moving sp for the outgoing area cannot
            # disturb any slot.
            #
            # A *variadic* call is different: ShivyCX's own variadic callees
            # read every argument from one contiguous stack block rather than
            # from the AAPCS64 register/stack split, because that is far
            # simpler than AArch64's real va_list (which needs separate
            # general and FP save areas plus four offsets). So for a variadic
            # call every argument is written to the block, its base is handed
            # over in x16, and the first eight of each class are *also* loaded
            # into their AAPCS64 registers so the callee's named parameters
            # arrive the ordinary way. x16 (IP0) is the natural carrier: the
            # ABI makes it scratch, so it costs nothing and nothing else wants
            # it across a call boundary.
            variadic = getattr(cmd, "variadic", False)
            nstack = 0
            gp = 0
            fp = 0
            for a in cmd.args:
                if a.ctype.is_floating():
                    if fp >= 8:
                        nstack += 1
                    fp += 1
                else:
                    if gp >= 8:
                        nstack += 1
                    gp += 1
            if variadic:
                nstack = len(cmd.args)
            outgoing = ((nstack * 8) + 15) & ~15      # keep sp 16-byte aligned
            if outgoing:
                self.asm_code.add(asm_cmds.Raw(
                    "sub\tsp, sp, #%d" % outgoing))
            if variadic:
                # The all-argument block, in declaration order, eight bytes
                # each. Sub-word integers go in as full 64-bit words so the
                # callee can read any width from the low end of the slot.
                voff = 0
                for a in cmd.args:
                    if a.ctype.is_floating():
                        src = self._arm64_floatuse(a, 16, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "str\t%s, [sp, #%d]" % (src, voff)))
                    else:
                        src = self._arm64_use(a, 15, reg_of, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "str\t%s, [sp, #%d]"
                            % (self._arm64_xname(src), voff)))
                    voff += 8
                self.asm_code.add(asm_cmds.Raw("mov\tx16, sp"))
            gp = 0
            fp = 0
            soff = 0
            for a in cmd.args:
                if variadic:
                    # Registers still get the first eight of each class, for
                    # the callee's named parameters; the block above carries
                    # the rest.
                    if a.ctype.is_floating():
                        if fp < 8:
                            self._arm64_finto(a, fp, slot_of)
                        fp += 1
                    else:
                        if gp < 8:
                            self._arm64_into(a, gp, reg_of, slot_of)
                        gp += 1
                    continue
                onstack = (fp >= 8) if a.ctype.is_floating() else (gp >= 8)
                if onstack:
                    # Stage through a scratch register, then store. x15/d16 are
                    # scratch and never value homes.
                    if a.ctype.is_floating():
                        src = self._arm64_floatuse(a, 16, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "str\t%s, [sp, #%d]" % (src, soff)))
                        fp += 1
                    else:
                        src = self._arm64_use(a, 15, reg_of, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "str\t%s, [sp, #%d]"
                            % (self._arm64_xname(src), soff)))
                        gp += 1
                    soff += 8
                elif a.ctype.is_floating():
                    self._arm64_finto(a, fp, slot_of)        # arg -> v<fp>
                    fp += 1
                else:
                    self._arm64_into(a, gp, reg_of, slot_of)  # arg -> w/x<gp>
                    gp += 1
            if name is not None:
                self.asm_code.add(asm_cmds.Raw(
                    "bl\t%s" % spots.mangle_symbol(name)))
            else:
                # Indirect call through a function pointer. The target is
                # loaded *after* the arguments are staged, so that materialising
                # it cannot disturb x0-x7; x15 is scratch and never a value
                # home, so it is safe to land on here.
                ra = self._arm64_use(cmd.func, 15, reg_of, slot_of)
                self.asm_code.add(asm_cmds.Raw(
                    "blr\t%s" % self._arm64_xname(ra)))
            if outgoing:
                self.asm_code.add(asm_cmds.Raw(
                    "add\tsp, sp, #%d" % outgoing))
            if not cmd.void_return:
                if cmd.ret.ctype.is_floating():
                    self._arm64_ffrom(0, cmd.ret, slot_of)    # s0/d0 -> ret home
                else:
                    self._arm64_from(0, cmd.ret, reg_of, slot_of)  # w0/x0 -> ret
            return
        if isinstance(cmd, value_cmds.AddrOf):
            name = self.symbol_table.names.get(cmd.var)
            if name is not None and cmd.var.ctype.is_function():
                # Record the name so a Call through this value stays a direct
                # `bl`, and *also* materialise the address: once function
                # pointers exist the value may be stored, passed or reassigned,
                # and then it has to be a real address rather than a note to
                # the call site.
                addrof_name[cmd.output] = name
                sym = spots.mangle_symbol(name)
                rd = self._arm64_xname(
                    self._arm64_defreg(cmd.output, 9, reg_of))
                self.asm_code.add(asm_cmds.Raw("adrp\t%s, %s" % (rd, sym)))
                self.asm_code.add(asm_cmds.Raw(
                    "add\t%s, %s, :lo12:%s" % (rd, rd, sym)))
                self._arm64_wb(cmd.output, 9, reg_of, slot_of)
                return
            gname = self._arm64_glob.get(cmd.var)
            if gname is not None:
                # Address of a global: adrp/add of its symbol (pointers are
                # 8-byte, so rd is an x-register), or a copy from the cached
                # address register if we have one.
                rd = self._arm64_defreg(cmd.output, 9, reg_of)
                cr = self._arm64_gaddr.get(cmd.var, -1)
                if cr >= 0:
                    self.asm_code.add(asm_cmds.Raw("mov\t%s, x%d" % (rd, cr)))
                else:
                    self.asm_code.add(asm_cmds.Raw("adrp\t%s, %s" % (rd, gname)))
                    self.asm_code.add(asm_cmds.Raw(
                        "add\t%s, %s, :lo12:%s" % (rd, rd, gname)))
                self._arm64_wb(cmd.output, 9, reg_of, slot_of)
                return
            # Address of a local: x29 + its frame slot. The variable was forced
            # to memory in _arm64_function, so slot_of[var] exists.
            rd = self._arm64_defreg(cmd.output, 9, reg_of)
            self.asm_code.add(asm_cmds.Raw(
                "add\t%s, x29, #%d" % (rd, slot_of[cmd.var])))
            self._arm64_wb(cmd.output, 9, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.ReadAt):
            ra = self._arm64_use(cmd.addr, 9, reg_of, slot_of)
            if cmd.output.ctype.is_floating():
                rd = self._arm64_fdefreg(cmd.output, 16)
                self.asm_code.add(asm_cmds.Raw("ldr\t%s, [%s]" % (rd, ra)))
                self._arm64_fwb(cmd.output, 16, slot_of)
                return
            rd = self._arm64_defreg(cmd.output, 10, reg_of)
            # Width matters: a plain `ldr` reads 4 or 8 bytes, so reading
            # through a `char *` would pull in the neighbouring bytes.
            op = self._arm64_ldr_op(cmd.output.ctype.size,
                                    self._arm64_signed(cmd.output))
            self.asm_code.add(asm_cmds.Raw("%s\t%s, [%s]" % (op, rd, ra)))
            self._arm64_wb(cmd.output, 10, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.SetAt):
            ra = self._arm64_use(cmd.addr, 9, reg_of, slot_of)
            if cmd.val.ctype.is_floating():
                rv = self._arm64_floatuse(cmd.val, 16, slot_of)
                self.asm_code.add(asm_cmds.Raw("str\t%s, [%s]" % (rv, ra)))
                return
            rv = self._arm64_use(cmd.val, 10, reg_of, slot_of)
            # Likewise for stores: a plain `str` writes 4 or 8 bytes, so
            # `*charptr = c` would overwrite the bytes after it.
            op = self._arm64_str_op(cmd.val.ctype.size)
            self.asm_code.add(asm_cmds.Raw("%s\t%s, [%s]" % (op, rv, ra)))
            return
        if isinstance(cmd, value_cmds.ReadRel):
            # output = *(base + chunk*count)   (array / pointer indexed load)
            target = self._arm64_rel_target(
                cmd.base, cmd.chunk, cmd.count, 12, reg_of, slot_of)
            out = cmd.output
            if out.ctype.is_floating():
                rd = self._arm64_fdefreg(out, 16)
                self.asm_code.add(asm_cmds.Raw("ldr\t%s, %s" % (rd, target)))
                self._arm64_fwb(out, 16, slot_of)
                return
            rd = self._arm64_defreg(out, 9, reg_of)
            op = self._arm64_ldr_op(out.ctype.size, self._arm64_signed(out))
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, rd, target)))
            self._arm64_wb(out, 9, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.SetRel):
            # *(base + chunk*count) = val   (array / pointer indexed store)
            target = self._arm64_rel_target(
                cmd.base, cmd.chunk, cmd.count, 12, reg_of, slot_of)
            if cmd.val.ctype.is_floating():
                rv = self._arm64_floatuse(cmd.val, 16, slot_of)
                self.asm_code.add(asm_cmds.Raw("str\t%s, %s" % (rv, target)))
                return
            rv = self._arm64_use(cmd.val, 9, reg_of, slot_of)
            op = self._arm64_str_op(cmd.val.ctype.size)
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, rv, target)))
            return
        if isinstance(cmd, value_cmds.AddrRel):
            # output = &(base + chunk*count)   (e.g. &a[i], &arr[i] for a struct)
            self._arm64_addr_into(
                cmd.base, cmd.chunk, cmd.count, 12, reg_of, slot_of)
            self._arm64_from(12, cmd.output, reg_of, slot_of)
            return
        if isinstance(cmd, control.Label):
            self.asm_code.add(asm_cmds.AsmLabel(cmd.label))
            return
        if isinstance(cmd, control.Jump):
            self.asm_code.add(asm_cmds.Raw(
                "b\t%s" % spots.mangle_symbol(cmd.label)))
            return
        if isinstance(cmd, control.JumpZero):
            rc = self._arm64_use(cmd.cond, 9, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw(
                "cbz\t%s, %s" % (rc, spots.mangle_symbol(cmd.label))))
            return
        if isinstance(cmd, control.JumpNotZero):
            rc = self._arm64_use(cmd.cond, 9, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw(
                "cbnz\t%s, %s" % (rc, spots.mangle_symbol(cmd.label))))
            return
        if isinstance(cmd, value_cmds.Set):
            out = cmd.output
            arg = cmd.arg
            # Floating point: float<->float moves/casts and int<->float
            # conversions all live here.
            of = out.ctype.is_floating()
            af = arg.ctype.is_floating()
            if of or af:
                if of and af:
                    if out.ctype.size == arg.ctype.size:
                        src = self._arm64_floatuse(arg, 16, slot_of)
                        rd = self._arm64_fdefreg(out, 16)
                        if rd != src:
                            self.asm_code.add(asm_cmds.Raw(
                                "fmov\t%s, %s" % (rd, src)))
                        self._arm64_fwb(out, 16, slot_of)
                    else:                       # float <-> double conversion
                        src = self._arm64_floatuse(arg, 16, slot_of)
                        rd = self._arm64_fdefreg(out, 16)
                        self.asm_code.add(asm_cmds.Raw(
                            "fcvt\t%s, %s" % (rd, src)))
                        self._arm64_fwb(out, 16, slot_of)
                elif of:                        # integer -> float
                    ra = self._arm64_use(arg, 9, reg_of, slot_of)
                    rd = self._arm64_fdefreg(out, 16)
                    sg = not (arg.ctype.is_pointer()
                              or (arg.ctype.is_integral() and not arg.ctype.signed))
                    op = "scvtf" if sg else "ucvtf"
                    self.asm_code.add(asm_cmds.Raw(
                        "%s\t%s, %s" % (op, rd, ra)))
                    self._arm64_fwb(out, 16, slot_of)
                else:                           # float -> integer (truncating)
                    fa = self._arm64_floatuse(arg, 16, slot_of)
                    rd = self._arm64_defreg(out, 9, reg_of)
                    sg = not (out.ctype.is_pointer()
                              or (out.ctype.is_integral() and not out.ctype.signed))
                    op = "fcvtzs" if sg else "fcvtzu"
                    self.asm_code.add(asm_cmds.Raw(
                        "%s\t%s, %s" % (op, rd, fa)))
                    self._arm64_wb(out, 9, reg_of, slot_of)
                return
            # Whole-aggregate copy (struct/array assignment): both operands are
            # memory-homed; copy size bytes in 8/4/2/1-byte chunks via scratch.
            if out.ctype.is_struct_union() or out.ctype.is_array():
                # Either side may be a frame slot or a global. Resolve each to
                # a (base register, offset) pair first; a global's address is
                # materialised into a scratch register, a local stays relative
                # to the frame pointer.
                obase, oo = self._arm64_agg_base(out, 10, slot_of)
                abase, ao = self._arm64_agg_base(arg, 11, slot_of)
                sz = out.ctype.size
                done = 0
                while sz - done >= 8:
                    self.asm_code.add(asm_cmds.Raw(
                        "ldr\tx9, [%s, #%d]" % (abase, ao + done)))
                    self.asm_code.add(asm_cmds.Raw(
                        "str\tx9, [%s, #%d]" % (obase, oo + done)))
                    done += 8
                while sz - done >= 4:
                    self.asm_code.add(asm_cmds.Raw(
                        "ldr\tw9, [%s, #%d]" % (abase, ao + done)))
                    self.asm_code.add(asm_cmds.Raw(
                        "str\tw9, [%s, #%d]" % (obase, oo + done)))
                    done += 4
                while sz - done >= 2:
                    self.asm_code.add(asm_cmds.Raw(
                        "ldrh\tw9, [%s, #%d]" % (abase, ao + done)))
                    self.asm_code.add(asm_cmds.Raw(
                        "strh\tw9, [%s, #%d]" % (obase, oo + done)))
                    done += 2
                while sz - done >= 1:
                    self.asm_code.add(asm_cmds.Raw(
                        "ldrb\tw9, [%s, #%d]" % (abase, ao + done)))
                    self.asm_code.add(asm_cmds.Raw(
                        "strb\tw9, [%s, #%d]" % (obase, oo + done)))
                    done += 1
                return
            r = reg_of.get(out, -1)
            lit = getattr(arg, "literal", None)
            if lit is not None and r >= 0:
                self._arm64_mov_imm(self._arm64_rn(r, out), lit.val,
                                    out.ctype.size)
                return
            src = self._arm64_use(arg, 9, reg_of, slot_of)
            ds = out.ctype.size
            ss = arg.ctype.size
            widen = ds > 4 and ss <= 4    # 32-bit value into a 64-bit dest
            if r >= 0:
                if widen:
                    if arg.ctype.signed:
                        self.asm_code.add(asm_cmds.Raw(
                            "sxtw\tx%d, %s" % (r, self._arm64_wname(src))))
                    else:   # writing the w-form zero-extends into the x-reg
                        self.asm_code.add(asm_cmds.Raw(
                            "mov\tw%d, %s" % (r, self._arm64_wname(src))))
                else:
                    dst = self._arm64_rn(r, out)
                    src2 = src
                    if ds <= 4:           # narrowing/same: match dest's w width
                        src2 = self._arm64_wname(src)
                    if dst != src2:
                        self.asm_code.add(asm_cmds.Raw(
                            "mov\t%s, %s" % (dst, src2)))
            else:
                stsrc = src
                if widen:
                    if arg.ctype.signed:
                        self.asm_code.add(asm_cmds.Raw(
                            "sxtw\tx9, %s" % self._arm64_wname(src)))
                    else:
                        self.asm_code.add(asm_cmds.Raw(
                            "mov\tw9, %s" % self._arm64_wname(src)))
                    stsrc = self._arm64_rn(9, out)
                elif ds <= 4:             # store the low word for a narrow dest
                    stsrc = self._arm64_wname(src)
                target = self._arm64_mem_addr(out, 15, slot_of)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s"
                    % (self._arm64_str_op(ds), stsrc, target)))
            return
        if isinstance(cmd, math_cmds.Add) or isinstance(cmd, math_cmds.Subtr) \
                or isinstance(cmd, math_cmds.Mult):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if out.ctype.is_floating():
                fa = self._arm64_floatuse(ins[0], 16, slot_of)
                fb = self._arm64_floatuse(ins[1], 17, slot_of)
                if isinstance(cmd, math_cmds.Add):
                    fop = "fadd"
                elif isinstance(cmd, math_cmds.Subtr):
                    fop = "fsub"
                else:
                    fop = "fmul"
                rd = self._arm64_fdefreg(out, 16)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s, %s" % (fop, rd, fa, fb)))
                self._arm64_fwb(out, 16, slot_of)
                return
            if isinstance(cmd, math_cmds.Add):
                op = "add"
            elif isinstance(cmd, math_cmds.Subtr):
                op = "sub"
            else:
                op = "mul"
            # Fold a small literal operand into an `add/sub #imm` (add is
            # commutative; sub only takes the immediate on its right).
            if op == "add":
                imm = self._arm64_imm(ins[1])
                other = ins[0]
                if imm < 0:
                    imm = self._arm64_imm(ins[0])
                    other = ins[1]
                if imm >= 0:
                    ra = self._arm64_use(other, 9, reg_of, slot_of)
                    rd = self._arm64_defreg(out, 9, reg_of)
                    self.asm_code.add(asm_cmds.Raw(
                        "add\t%s, %s, #%d" % (rd, ra, imm)))
                    self._arm64_wb(out, 9, reg_of, slot_of)
                    return
            elif op == "sub":
                imm = self._arm64_imm(ins[1])
                if imm >= 0:
                    ra = self._arm64_use(ins[0], 9, reg_of, slot_of)
                    rd = self._arm64_defreg(out, 9, reg_of)
                    self.asm_code.add(asm_cmds.Raw(
                        "sub\t%s, %s, #%d" % (rd, ra, imm)))
                    self._arm64_wb(out, 9, reg_of, slot_of)
                    return
            ra = self._arm64_use(ins[0], 9, reg_of, slot_of)
            rb = self._arm64_use(ins[1], 10, reg_of, slot_of)
            rd = self._arm64_defreg(out, 9, reg_of)
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, %s, %s" % (op, rd, ra, rb)))
            self._arm64_wb(out, 9, reg_of, slot_of)
            return
        if isinstance(cmd, math_cmds.Div) or isinstance(cmd, math_cmds.Mod):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if out.ctype.is_floating():        # float division (Mod n/a for float)
                fa = self._arm64_floatuse(ins[0], 16, slot_of)
                fb = self._arm64_floatuse(ins[1], 17, slot_of)
                rd = self._arm64_fdefreg(out, 16)
                self.asm_code.add(asm_cmds.Raw(
                    "fdiv\t%s, %s, %s" % (rd, fa, fb)))
                self._arm64_fwb(out, 16, slot_of)
                return
            ra = self._arm64_use(ins[0], 9, reg_of, slot_of)
            rb = self._arm64_use(ins[1], 10, reg_of, slot_of)
            ct = out.ctype
            signed = not (ct.is_pointer() or (ct.is_integral() and not ct.signed))
            divop = "sdiv" if signed else "udiv"
            rd = self._arm64_defreg(out, 9, reg_of)
            if isinstance(cmd, math_cmds.Div):
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s, %s" % (divop, rd, ra, rb)))
            else:
                # mod: q = a / b (scratch w11);  r = a - q*b via msub
                rq = self._arm64_rn(11, out)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s, %s" % (divop, rq, ra, rb)))
                self.asm_code.add(asm_cmds.Raw(
                    "msub\t%s, %s, %s, %s" % (rd, rq, rb, ra)))
            self._arm64_wb(out, 9, reg_of, slot_of)
            return
        if isinstance(cmd, math_cmds.BitAnd) or isinstance(cmd, math_cmds.BitOr) \
                or isinstance(cmd, math_cmds.BitXor):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            ra = self._arm64_use(ins[0], 9, reg_of, slot_of)
            rb = self._arm64_use(ins[1], 10, reg_of, slot_of)
            if isinstance(cmd, math_cmds.BitAnd):
                op = "and"
            elif isinstance(cmd, math_cmds.BitOr):
                op = "orr"
            else:
                op = "eor"
            rd = self._arm64_defreg(out, 9, reg_of)
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, %s, %s" % (op, rd, ra, rb)))
            self._arm64_wb(out, 9, reg_of, slot_of)
            return
        if isinstance(cmd, math_cmds.LBitShift) \
                or isinstance(cmd, math_cmds.RBitShift):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            ra = self._arm64_use(ins[0], 9, reg_of, slot_of)
            if isinstance(cmd, math_cmds.LBitShift):
                op = "lsl"
            else:
                ct = ins[0].ctype
                sg = not (ct.is_pointer() or (ct.is_integral() and not ct.signed))
                op = "asr" if sg else "lsr"
            rd = self._arm64_defreg(out, 9, reg_of)
            lit = getattr(ins[1], "literal", None)
            if lit is not None and 0 <= lit.val < 64:
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s, #%d" % (op, rd, ra, lit.val)))
            else:
                rb = self._arm64_use(ins[1], 10, reg_of, slot_of)
                # Register-form shift takes the amount at the value's width
                # (only the low bits are used).
                if out.ctype.size > 4:
                    rb = self._arm64_xname(rb)
                else:
                    rb = self._arm64_wname(rb)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s, %s" % (op, rd, ra, rb)))
            self._arm64_wb(out, 9, reg_of, slot_of)
            return
        if isinstance(cmd, math_cmds.Not) or isinstance(cmd, math_cmds.Neg):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if out.ctype.is_floating():
                # Negating a double is `fneg`, not `neg`. Without this the
                # operand goes through the *integer* helpers, which look for a
                # frame slot that a value with an FP home does not have.
                fa = self._arm64_floatuse(ins[0], 16, slot_of)
                fd = self._arm64_fdefreg(out, 16)
                self.asm_code.add(asm_cmds.Raw("fneg\t%s, %s" % (fd, fa)))
                self._arm64_fwb(out, 16, slot_of)
                return
            ra = self._arm64_use(ins[0], 9, reg_of, slot_of)
            op = "mvn" if isinstance(cmd, math_cmds.Not) else "neg"
            rd = self._arm64_defreg(out, 9, reg_of)
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, rd, ra)))
            self._arm64_wb(out, 9, reg_of, slot_of)
            return
        if isinstance(cmd, cmp_cmds._GeneralCmp):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if ins[0].ctype.is_floating():
                fa = self._arm64_floatuse(ins[0], 16, slot_of)
                fb = self._arm64_floatuse(ins[1], 17, slot_of)
                self.asm_code.add(asm_cmds.Raw("fcmp\t%s, %s" % (fa, fb)))
                rd = self._arm64_defreg(out, 9, reg_of)
                self.asm_code.add(asm_cmds.Raw(
                    "cset\t%s, %s" % (rd, self._arm64_fcmp_cc(cmd))))
                self._arm64_wb(out, 9, reg_of, slot_of)
                return
            ra = self._arm64_use(ins[0], 9, reg_of, slot_of)
            imm = self._arm64_imm(ins[1])
            if imm >= 0:
                self.asm_code.add(asm_cmds.Raw("cmp\t%s, #%d" % (ra, imm)))
            else:
                rb = self._arm64_use(ins[1], 10, reg_of, slot_of)
                self.asm_code.add(asm_cmds.Raw("cmp\t%s, %s" % (ra, rb)))
            ct = ins[0].ctype
            signed = not (ct.is_pointer() or (ct.is_integral() and not ct.signed))
            cc = self._arm64_cmp_cc(cmd, signed)
            fz = self._arm64_fuse.get(idx)
            if fz is not None:
                # Fused with the following branch: jump directly on the (possibly
                # inverted) condition instead of materializing a 0/1 and testing.
                label = fz[0]
                on_true = fz[1]
                if not on_true:
                    cc = self._arm64_invert_cc(cc)
                self.asm_code.add(asm_cmds.Raw(
                    "b.%s\t%s" % (cc, spots.mangle_symbol(label))))
                return
            rd = self._arm64_defreg(out, 9, reg_of)
            self.asm_code.add(asm_cmds.Raw("cset\t%s, %s" % (rd, cc)))
            self._arm64_wb(out, 9, reg_of, slot_of)
            return
        if isinstance(cmd, control.Return):
            if cmd.arg is not None:
                if cmd.arg.ctype.is_floating():
                    self._arm64_finto(cmd.arg, 0, slot_of)     # retval -> s0/d0
                else:
                    self._arm64_into(cmd.arg, 0, reg_of, slot_of)  # -> w0/x0
            self._arm64_epilogue(nreg, frame)
            return
        raise NotImplementedError(
            "arm64 back end: IL command '%s' not implemented yet"
            % type(cmd).__name__)


    # ================= RISC-V 64 (rv64, lp64) back end =================
    # Brought up after aarch64 to exercise the target seam: it reuses the
    # architecture-neutral middle end verbatim -- copy-coalescing safety
    # (_il_coalesce_safe), liveness (_il_liveness), live intervals + call-cross
    # detection (_il_intervals), and the caller/callee linear-scan allocator
    # (_il_linear_scan). Only instruction selection, the register file, and the
    # ABI below are new. Scope is the integer core (locals, +-*/% , the six
    # comparisons, if/while, direct calls, recursion); unsupported IL raises
    # rather than miscompile, exactly as the aarch64 back end did at this stage.
    #
    # Register file (lp64): x0=zero, x1=ra, x2=sp; scratch t0-t2 (x5-x7) and
    # t3 (x28); argument/return a0-a7 (x10-x17); callee-saved homes s2-s11
    # (x18-x27); extra caller-saved homes t4-t6 (x29-x31). Frames are
    # sp-relative (no frame pointer); leaf functions with no spills are
    # frameless.

    def _make_asm_riscv64(self):
        """RISC-V 64 lowering. Runs only under `--target riscv64`; the x86-64
        and aarch64 paths are untouched."""
        EXTERNAL = self.symbol_table.EXTERNAL
        DEFINED = self.symbol_table.DEFINED
        for v in self.symbol_table.linkages[EXTERNAL].values():
            if self.symbol_table.def_state.get(v) == DEFINED:
                self.asm_code.add_global(self.symbol_table.names[v])
        # value -> assembler symbol, for static/file-scope globals, plus a
        # dedup map so each global's storage is emitted only once.
        self._rv_glob = {}
        self._rv_gemit = {}
        self._rv_freg = {}
        self._rv_fltlit = {}
        self._rv_fltlit_n = 0
        self._rv_saved_fp = []
        self._rv_fp_save_off = {}
        # String-literal storage. A literal lives at a symbol in .data, not in
        # any frame, so it is emitted once here and then treated exactly like
        # a global: `char *p = "hi"` is an AddrOf of that symbol. Names are
        # generated if the front end did not already intern one, and recorded
        # so every reference uses the same label.
        self._rv_strlit = {}
        snum = 0
        for v in self.il_code.string_literals:
            nm = self.il_code.string_literal_names.get(v)
            if nm is None:
                nm = "__rvstr%d" % snum
                self.il_code.string_literal_names[v] = nm
            snum += 1
            elem_size = v.ctype.el.size if v.ctype.is_array() else 1
            self.asm_code.add_string_literal(
                nm, self.il_code.string_literals[v], elem_size)
            self._rv_strlit[v] = nm
            # Also record it in the global map: the per-function value scan
            # skips anything carrying a `.literal` attribute, and a string
            # literal does, so it would never be registered there.
            self._rv_glob[v] = nm
        for func in self.il_code.commands:
            self._rv_function(func, self.il_code.commands[func])

    def _rv_emit_global_storage(self, v):
        """Emit `.comm`/`.data` storage for a static/file-scope global `v`
        (once), mirroring the arm64 and x86 paths."""
        name = self.symbol_table.asm_name(v)
        if name in self._rv_gemit:
            return
        self._rv_gemit[name] = 1
        TENTATIVE = self.symbol_table.TENTATIVE
        INTERNAL = self.symbol_table.INTERNAL
        if self.symbol_table.def_state.get(v) == TENTATIVE:
            local = (self.symbol_table.linkage_type[v] == INTERNAL)
            self.asm_code.add_comm(name, v.ctype.size, local)
        elif v in self.il_code.static_block_inits:
            entries, total = self.il_code.static_block_inits[v]
            self.asm_code.add_data_block(name, entries, total)
        else:
            init_val = self.il_code.static_inits.get(v, 0)
            self.asm_code.add_data(name, v.ctype.size, init_val)

    def _rv_signed(self, value):
        """Whether `value` needs a sign-extending sub-word load. Only 1- and
        2-byte integers care; wider or non-integer types (pointers have no
        `signed` attribute) report False safely via the size short-circuit."""
        if value.ctype.size <= 2:
            return value.ctype.signed
        return False

    def _rv_ld_op(self, size, signed):
        """Load mnemonic for a `size`-byte value. RISC-V spells the choice as
        signed-vs-unsigned per width: lb/lbu, lh/lhu, lw/lwu, ld."""
        if size == 1:
            return "lb" if signed else "lbu"
        if size == 2:
            return "lh" if signed else "lhu"
        if size <= 4:
            return "lw"
        return "ld"

    def _rv_st_op(self, size):
        """Store mnemonic for a `size`-byte value."""
        if size == 1:
            return "sb"
        if size == 2:
            return "sh"
        if size <= 4:
            return "sw"
        return "sd"

    def _rv_gaddr(self, value, areg):
        """Materialize the address of global `value` into x<areg> and return
        that register name. `lla` expands to auipc+addi with the PC-relative
        relocation pair; the assembler owns that expansion."""
        name = self._rv_glob[value]
        a = self._rv_rn(areg)
        self.asm_code.add(asm_cmds.Raw("lla\t%s, %s" % (a, name)))
        return a

    def _rv_rn(self, regnum):
        """RISC-V register name (x0..x31)."""
        return "x%d" % regnum

    def _rv_use(self, value, scratch, reg_of, slot_of):
        """Register holding `value`: its home (no code), a loaded literal, or a
        load from its spill slot into x<scratch>."""
        lit = getattr(value, "literal", None)
        if lit is not None:
            name = self._rv_rn(scratch)
            self.asm_code.add(asm_cmds.Raw("li\t%s, %s" % (name, lit.val)))
            return name
        r = reg_of.get(value, -1)
        if r >= 0:
            return self._rv_rn(r)
        name = self._rv_rn(scratch)
        if value in self._rv_glob:
            a = self._rv_gaddr(value, scratch)
            op = self._rv_ld_op(value.ctype.size, self._rv_signed(value))
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, 0(%s)" % (op, name, a)))
            return name
        op = "lw" if value.ctype.size <= 4 else "ld"
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, %d(sp)" % (op, name, slot_of[value])))
        return name

    def _rv_defreg(self, value, scratch, reg_of):
        """Register to write `value` into: its home, else x<scratch>."""
        r = reg_of.get(value, -1)
        if r >= 0:
            return self._rv_rn(r)
        return self._rv_rn(scratch)

    def _rv_wb(self, value, scratch, reg_of, slot_of):
        """Store x<scratch> back to `value`'s spill slot, if it has no home."""
        if reg_of.get(value, -1) >= 0:
            return
        if value in self._rv_glob:
            # The value is already in x<scratch>, so the address needs a
            # different register: t3 is reserved for exactly this.
            a = self._rv_gaddr(value, 28)
            op = self._rv_st_op(value.ctype.size)
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, 0(%s)" % (op, self._rv_rn(scratch), a)))
            return
        op = "sw" if value.ctype.size <= 4 else "sd"
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, %d(sp)" % (op, self._rv_rn(scratch), slot_of[value])))

    def _rv_into(self, value, n, reg_of, slot_of):
        """Force `value` into x<n> (call argument / return value)."""
        name = self._rv_rn(n)
        lit = getattr(value, "literal", None)
        if lit is not None:
            self.asm_code.add(asm_cmds.Raw("li\t%s, %s" % (name, lit.val)))
            return
        r = reg_of.get(value, -1)
        if r >= 0:
            if r != n:
                self.asm_code.add(asm_cmds.Raw(
                    "mv\t%s, %s" % (name, self._rv_rn(r))))
            return
        if value in self._rv_glob:
            a = self._rv_gaddr(value, 28)
            op = self._rv_ld_op(value.ctype.size, self._rv_signed(value))
            self.asm_code.add(asm_cmds.Raw("%s\t%s, 0(%s)" % (op, name, a)))
            return
        op = "lw" if value.ctype.size <= 4 else "ld"
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, %d(sp)" % (op, name, slot_of[value])))

    def _rv_from(self, n, value, reg_of, slot_of):
        """Store x<n> into `value`'s home (parameter unload / call result)."""
        src = self._rv_rn(n)
        r = reg_of.get(value, -1)
        if r >= 0:
            if r != n:
                self.asm_code.add(asm_cmds.Raw(
                    "mv\t%s, %s" % (self._rv_rn(r), src)))
        elif value in self._rv_glob:
            a = self._rv_gaddr(value, 28)
            op = self._rv_st_op(value.ctype.size)
            self.asm_code.add(asm_cmds.Raw("%s\t%s, 0(%s)" % (op, src, a)))
        else:
            op = "sw" if value.ctype.size <= 4 else "sd"
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, %d(sp)" % (op, src, slot_of[value])))

    def _rv_ctype_signed(self, ctype):
        """Whether `ctype` is a signed integer. Pointers and unsigned types
        are not; anything else is."""
        return not (ctype.is_pointer()
                    or (ctype.is_integral() and not ctype.signed))

    def _rv_canon(self, rd, rs, size, signed):
        """Emit code so `rd` holds `rs` truncated to `size` bytes and extended
        to the RV64 canonical register form for that type.

        RV64 keeps every 32-bit value sign-extended in a register, signed or
        not -- that is the psABI's rule, not a choice -- so a 4-byte result is
        always `addiw`. Narrower types are canonicalised by their own
        signedness, which is what makes an unsigned char zero-extended and a
        signed char sign-extended.
        """
        if size >= 8:
            if rd != rs:
                self.asm_code.add(asm_cmds.Raw("mv\t%s, %s" % (rd, rs)))
            return
        if size == 4:
            self.asm_code.add(asm_cmds.Raw("addiw\t%s, %s, 0" % (rd, rs)))
            return
        if size == 2:
            self.asm_code.add(asm_cmds.Raw("slli\t%s, %s, 48" % (rd, rs)))
            op = "srai" if signed else "srli"
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s, 48" % (op, rd, rd)))
            return
        if signed:
            self.asm_code.add(asm_cmds.Raw("slli\t%s, %s, 56" % (rd, rs)))
            self.asm_code.add(asm_cmds.Raw("srai\t%s, %s, 56" % (rd, rd)))
        else:
            self.asm_code.add(asm_cmds.Raw("andi\t%s, %s, 255" % (rd, rs)))

    def _rv_convert(self, rd, rs, out, arg):
        """Emit the conversion for `Set(out, arg)`: truncate or extend `rs`
        into `rd` so it is canonical for `out`'s type."""
        so = out.ctype.size
        sa = arg.ctype.size
        if so < sa:
            self._rv_canon(rd, rs, so, self._rv_ctype_signed(out.ctype))
            return
        if so > sa and sa == 4 and not self._rv_ctype_signed(arg.ctype):
            # Widening an *unsigned* 32-bit value. The register holds it
            # sign-extended (the canonical 32-bit form), so a plain `mv` would
            # carry that sign into the high half -- 0x80000000u would become
            # negative rather than 2147483648. Zero-extend explicitly.
            self.asm_code.add(asm_cmds.Raw("slli\t%s, %s, 32" % (rd, rs)))
            self.asm_code.add(asm_cmds.Raw("srli\t%s, %s, 32" % (rd, rd)))
            return
        # Widening from 1 or 2 bytes needs nothing: those are already stored
        # in the canonical form their own signedness dictates, which is
        # exactly the widened value. Same-size assignment is a plain move.
        if rd != rs:
            self.asm_code.add(asm_cmds.Raw("mv\t%s, %s" % (rd, rs)))

    def _rv_frn(self, regnum):
        """FP register name. Unlike AArch64 -- where s<n> and d<n> select the
        access width -- RISC-V has one name per register and the *instruction*
        carries the precision suffix, so this needs no value argument."""
        return "f%d" % regnum

    def _rv_fsuffix(self, value):
        """Instruction suffix for `value`'s precision: `.s` or `.d`."""
        return "s" if value.ctype.size == 4 else "d"

    def _rv_fld_op(self, value):
        return "flw" if value.ctype.size == 4 else "fld"

    def _rv_fst_op(self, value):
        return "fsw" if value.ctype.size == 4 else "fsd"

    def _rv_float_label(self, value):
        """Emit the data for float literal `value` once and return its label."""
        name = self._rv_fltlit.get(value)
        if name is not None:
            return name
        import struct
        val = self.il_code.float_literals[value]
        name = "__rvflt%d" % self._rv_fltlit_n
        if value.ctype.size == 4:
            bits = struct.unpack("<I", struct.pack("<f", val))[0]
            self.asm_code.add_data(name, 4, bits)
        else:
            bits = struct.unpack("<Q", struct.pack("<d", val))[0]
            self.asm_code.add_data(name, 8, bits)
        self._rv_fltlit_n += 1
        self._rv_fltlit[value] = name
        return name

    def _rv_fload_lit(self, value, fn):
        """Load float literal `value` into f<fn> via its .data label.

        The address goes in t2, deliberately not t3: t3 is where the indexed
        addressing helpers leave a computed address, and a literal operand is
        loaded *after* that address is formed. Using t3 here would overwrite
        the destination of the very store being set up, so `v.x = 1.5` would
        write into .data instead of the struct.
        """
        name = self._rv_float_label(value)
        self.asm_code.add(asm_cmds.Raw("lla\tt2, %s" % name))
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, 0(t2)" % (self._rv_fld_op(value), self._rv_frn(fn))))

    def _rv_faddr(self, value, areg, slot_of):
        """Address operand for a memory-homed float: `0(t3)` for a global,
        `<slot>(sp)` for a frame slot."""
        gname = self._rv_glob.get(value)
        if gname is not None:
            a = self._rv_rn(areg)
            self.asm_code.add(asm_cmds.Raw("lla\t%s, %s" % (a, gname)))
            return "0(%s)" % a
        return "%d(sp)" % slot_of[value]

    def _rv_fuse(self, value, scratch, slot_of):
        """Return an FP register name holding float `value`: its home (no
        code), a loaded literal, or a load from its slot/global into
        f<scratch>."""
        if value in self.il_code.float_literals:
            self._rv_fload_lit(value, scratch)
            return self._rv_frn(scratch)
        r = self._rv_freg.get(value, -1)
        if r >= 0:
            return self._rv_frn(r)
        target = self._rv_faddr(value, 28, slot_of)
        name = self._rv_frn(scratch)
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, %s" % (self._rv_fld_op(value), name, target)))
        return name

    def _rv_fdefreg(self, value, scratch):
        """FP register to write float `value` into: home, else f<scratch>."""
        r = self._rv_freg.get(value, -1)
        if r >= 0:
            return self._rv_frn(r)
        return self._rv_frn(scratch)

    def _rv_fwb(self, value, scratch, slot_of):
        """Store f<scratch> back to float `value`'s slot/global, if it has no
        register home."""
        if self._rv_freg.get(value, -1) >= 0:
            return
        target = self._rv_faddr(value, 28, slot_of)
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, %s" % (self._rv_fst_op(value),
                            self._rv_frn(scratch), target)))

    def _rv_finto(self, value, n, slot_of):
        """Force float `value` into f<n> (call argument / return value)."""
        name = self._rv_frn(n)
        if value in self.il_code.float_literals:
            self._rv_fload_lit(value, n)
            return
        r = self._rv_freg.get(value, -1)
        if r >= 0:
            src = self._rv_frn(r)
            if src != name:
                self.asm_code.add(asm_cmds.Raw(
                    "fmv.%s\t%s, %s" % (self._rv_fsuffix(value), name, src)))
            return
        target = self._rv_faddr(value, 28, slot_of)
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, %s" % (self._rv_fld_op(value), name, target)))

    def _rv_ffrom(self, n, value, slot_of):
        """Store f<n> into float `value`'s home/slot/global (parameter unload
        or call result)."""
        src = self._rv_frn(n)
        r = self._rv_freg.get(value, -1)
        if r >= 0:
            dst = self._rv_frn(r)
            if dst != src:
                self.asm_code.add(asm_cmds.Raw(
                    "fmv.%s\t%s, %s" % (self._rv_fsuffix(value), dst, src)))
            return
        target = self._rv_faddr(value, 28, slot_of)
        self.asm_code.add(asm_cmds.Raw(
            "%s\t%s, %s" % (self._rv_fst_op(value), src, target)))

    def _rv_base_addr(self, base, areg, reg_of, slot_of):
        """Emit the base address of `base` into x<areg> and return its name.

        A base is one of three things: a global (its symbol), storage that
        lives in the frame -- an array or struct, whose *address* is sp+slot
        rather than its contents -- or an ordinary pointer value, which is
        already an address.
        """
        gname = self._rv_glob.get(base)
        a = self._rv_rn(areg)
        if gname is not None:
            self.asm_code.add(asm_cmds.Raw("lla\t%s, %s" % (a, gname)))
            return a
        if base.ctype.is_array() or base.ctype.is_struct_union():
            self.asm_code.add(asm_cmds.Raw(
                "addi\t%s, sp, %d" % (a, slot_of[base])))
            return a
        return self._rv_use(base, areg, reg_of, slot_of)

    def _rv_rel_base(self, base, chunk, count, rd, reg_of, slot_of):
        """Emit `rd = base + chunk*count` (or base + chunk when count is
        None), the address form shared by ReadRel/SetRel/AddrRel."""
        const_off = None
        if count is None:
            const_off = chunk
        else:
            lit = getattr(count, "literal", None)
            if lit is not None:
                const_off = chunk * lit.val
        if const_off is not None:
            b = self._rv_base_addr(base, 28, reg_of, slot_of)
            if const_off == 0:
                if rd != b:
                    self.asm_code.add(asm_cmds.Raw("mv\t%s, %s" % (rd, b)))
            elif -2048 <= const_off <= 2047:
                self.asm_code.add(asm_cmds.Raw(
                    "addi\t%s, %s, %d" % (rd, b, const_off)))
            else:
                # Beyond addi's 12-bit reach; materialize the offset first.
                self.asm_code.add(asm_cmds.Raw("li\tt2, %d" % const_off))
                self.asm_code.add(asm_cmds.Raw(
                    "add\t%s, %s, t2" % (rd, b)))
            return
        # Variable index: scale it by the element size, then add. The index is
        # loaded before the base so that computing the base cannot clobber it.
        ri = self._rv_use(count, 7, reg_of, slot_of)
        b = self._rv_base_addr(base, 28, reg_of, slot_of)
        if chunk == 1:
            self.asm_code.add(asm_cmds.Raw("add\t%s, %s, %s" % (rd, b, ri)))
            return
        sh = -1
        c = chunk
        k = 0
        while c > 0 and k < 63:
            if c == 1:
                sh = k
                break
            if c % 2 != 0:
                break
            c = c // 2
            k += 1
        if sh > 0:
            self.asm_code.add(asm_cmds.Raw("slli\tt2, %s, %d" % (ri, sh)))
        else:
            self.asm_code.add(asm_cmds.Raw("li\tt2, %d" % chunk))
            self.asm_code.add(asm_cmds.Raw("mul\tt2, %s, t2" % ri))
        self.asm_code.add(asm_cmds.Raw("add\t%s, %s, t2" % (rd, b)))

    def _rv_rel_addr(self, base, chunk, count, areg, reg_of, slot_of):
        """Return a `0(reg)` operand addressing base + chunk*count, emitting
        the address computation into x<areg>."""
        a = self._rv_rn(areg)
        self._rv_rel_base(base, chunk, count, a, reg_of, slot_of)
        return "0(%s)" % a

    def _rv_sp_adjust(self, delta):
        """Move sp by `delta` bytes (negative to allocate).

        `addi` carries a signed 12-bit immediate, so a frame larger than 2047
        bytes -- easily reached by a function with a sizeable local buffer --
        needs the amount materialised first. t1 is used rather than t0: at
        function entry t0 holds the variadic argument-block base that
        VaSaveBase is about to read, and the prologue runs before it.
        """
        if -2048 <= delta <= 2047:
            self.asm_code.add(asm_cmds.Raw("addi\tsp, sp, %d" % delta))
            return
        self.asm_code.add(asm_cmds.Raw("li\tt1, %d" % delta))
        self.asm_code.add(asm_cmds.Raw("add\tsp, sp, t1"))

    def _rv_epilogue(self, frame, has_call):
        for r in self._rv_saved_int:
            self.asm_code.add(asm_cmds.Raw(
                "ld\t%s, %d(sp)" % (self._rv_rn(r), self._rv_int_save_off[r])))
        for r in self._rv_saved_fp:
            self.asm_code.add(asm_cmds.Raw(
                "fld\t%s, %d(sp)" % (self._rv_frn(r),
                                     self._rv_fp_save_off[r])))
        if has_call:
            self.asm_code.add(asm_cmds.Raw(
                "ld\tra, %d(sp)" % self._rv_ra_off))
        if frame:
            self._rv_sp_adjust(frame)
        self.asm_code.add(asm_cmds.Raw("ret"))

    def _rv_function(self, func, cmds):
        import shivyc.il_cmds.control as control
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.math as math_cmds
        import shivyc.il_cmds.compare as cmp_cmds
        n = len(cmds)
        # Distinct non-literal values; refuse anything outside the integer core.
        values = []
        seen = {}
        has_call = False
        STATIC = self.symbol_table.STATIC
        EXTERNAL = self.symbol_table.EXTERNAL
        for c in cmds:
            if isinstance(c, control.Call):
                has_call = True
            for v in c.inputs() + c.outputs():
                if v is None or getattr(v, "literal", None) is not None:
                    continue
                if self.symbol_table.storage.get(v) == STATIC:
                    # A static / file-scope global lives at a symbol, not in
                    # the frame, so it is deliberately kept out of `values`:
                    # it gets neither a register home nor a spill slot, and
                    # every access goes through lla + load/store.

                    if v not in self._rv_glob:
                        self._rv_glob[v] = self.symbol_table.asm_name(v)
                        self._rv_emit_global_storage(v)
                    continue
                if self.symbol_table.linkage_type.get(v) == EXTERNAL \
                        and not v.ctype.is_function():
                    # An `extern` object defined in another translation unit
                    # or by a linker script: EXTERNAL linkage, but no storage
                    # duration here, so the STATIC test above misses it. It is
                    # addressed by symbol like any other global; no storage is
                    # emitted, since the definition is elsewhere. See the
                    # matching comment in _arm64_function -- both bare-metal
                    # back ends had this bug and it was silent on both.
                    if v not in self._rv_glob:
                        self._rv_glob[v] = self.symbol_table.asm_name(v)
                    continue
                if v not in seen:
                    seen[v] = 1
                    values.append(v)

        # A value whose address is taken, or that is an aggregate (too big for
        # a register), must live in memory: the first so a real address
        # exists, the second so it fits at all.
        forced = {}
        for c in cmds:
            if isinstance(c, value_cmds.AddrOf) \
                    and not c.var.ctype.is_function():
                forced[c.var] = 1
        for v in values:
            if v.ctype.is_array() or v.ctype.is_struct_union() \
                    or v.ctype.size > 8:
                forced[v] = 1

        glob = {}
        fused_out = {}
        usecount = {}
        defcount = {}
        for c in cmds:
            for v in c.inputs():
                if v is not None:
                    usecount[v] = usecount.get(v, 0) + 1
            for v in c.outputs():
                if v is not None:
                    defcount[v] = defcount.get(v, 0) + 1
        # Parameter mapping. lp64d counts integer and floating-point
        # arguments in *separate* sequences -- a0..a7 and fa0..fa7 -- so a
        # function's third parameter may arrive in a0 if the first two were
        # doubles. Both maps are keyed by argument number.
        self._rv_arggp = {}
        self._rv_argfp = {}
        # Parameters past the eighth of their class arrive in the caller's
        # outgoing area, just above our frame.
        self._rv_argstk = {}
        agp = 0
        afp = 0
        astk = 0
        for c in cmds:
            if isinstance(c, value_cmds.LoadArg):
                if c.output.ctype.is_floating():
                    if afp >= 8:
                        self._rv_argstk[c.arg_num] = astk
                        astk += 8
                    else:
                        self._rv_argfp[c.arg_num] = afp
                    afp += 1
                else:
                    if agp >= 8:
                        self._rv_argstk[c.arg_num] = astk
                        astk += 8
                    else:
                        self._rv_arggp[c.arg_num] = agp
                    agp += 1

        # Copy coalescing (shared safety check).
        defidx = {}
        for idx in range(n):
            for v in cmds[idx].outputs():
                if v is not None:
                    defidx[v] = idx
        coalesce = {}
        skip = {}
        for k in range(n):
            c = cmds[k]
            if isinstance(c, value_cmds.Set):
                arg = c.arg
                out = c.output
                # The `forced` and aggregate tests below are defensive
                # rather than load-bearing: a forced value is already kept out
                # of `order`, so it has no register home, and coalescing onto
                # it would still write through its frame slot. Mutation
                # testing confirms removing them changes the code emitted but
                # not its behaviour. They are kept for parity with the arm64
                # path, and so the invariant survives a future change to how
                # allocation picks homes.
                if getattr(arg, "literal", None) is None \
                        and usecount.get(arg, 0) == 1 \
                        and defcount.get(arg, 0) == 1 \
                        and arg not in self._rv_glob \
                        and out not in self._rv_glob \
                        and arg not in forced and out not in forced \
                        and not out.ctype.is_struct_union() \
                        and not out.ctype.is_array() \
                        and out.ctype.size <= 8 \
                        and out.ctype.size == arg.ctype.size \
                        and out.ctype.is_floating() \
                            == arg.ctype.is_floating() \
                        and self._il_coalesce_safe(
                            cmds, defidx.get(arg, -1), k, out):
                    coalesce[arg] = out

        uses = []
        defs = []
        for idx in range(n):
            c = cmds[idx]
            u = []
            d = []
            for v in c.inputs():
                if v is not None and getattr(v, "literal", None) is None:
                    u.append(self._il_canon(v, coalesce))
            for v in c.outputs():
                if v is not None and getattr(v, "literal", None) is None:
                    d.append(self._il_canon(v, coalesce))
            uses.append(u)
            defs.append(d)
        live_in, live_out = self._il_liveness(cmds, n, uses, defs)
        start, end, crosses = self._il_intervals(
            cmds, n, live_in, live_out, uses, defs)

        # Argument set-up writes a0..a<gp_max-1>; parameters arrive in a0..
        # a<agp-1>. Caller-saved homes are placed above both.
        gp_max = 0
        fp_max = 0
        out_max = 0                   # widest outgoing stack-argument area
        for c in cmds:
            if isinstance(c, control.Call):
                g = 0
                fcnt = 0
                for a in c.args:
                    if a.ctype.is_floating():
                        fcnt += 1
                    else:
                        g += 1
                ns = 0
                if g > 8:
                    ns += g - 8
                    g = 8
                if fcnt > 8:
                    ns += fcnt - 8
                    fcnt = 8
                if getattr(c, "variadic", False):
                    # A variadic call needs room for *every* argument, not
                    # just the overflow: they all go in one block.
                    ns = len(c.args)
                if ns > out_max:
                    out_max = ns
                if g > gp_max:
                    gp_max = g
                if fcnt > fp_max:
                    fp_max = fcnt
        cs = gp_max
        if agp > cs:
            cs = agp
        if cs < 1:
            cs = 1
        int_caller = []
        a = cs
        while a <= 7:
            int_caller.append(10 + a)        # a<cs>..a7
            a += 1
        int_caller.append(29)                # t4, t5, t6: caller-saved, non-arg
        int_caller.append(30)
        int_caller.append(31)
        int_callee = []
        r = 18
        while r <= 27:                       # s2..s11
            int_callee.append(r)
            r += 1
        # Floating-point file. Caller-saved homes are the temporaries above
        # the argument registers this function actually uses: ft0..ft7 are
        # f0..f7, and fa<fs>..fa7 are f10+fs..f17. ft8/ft9 (f28/f29) join
        # them; ft10/ft11 (f30/f31) are reserved as scratch and never become
        # homes. Callee-saved homes are fs0..fs11 -- f8, f9 and f18..f27.
        fp_caller = []
        rr = 0
        while rr <= 7:                       # ft0..ft7
            fp_caller.append(rr)
            rr += 1
        fs = fp_max
        if afp > fs:
            fs = afp
        rr = fs
        while rr <= 7:                       # unused fa registers
            fp_caller.append(10 + rr)
            rr += 1
        fp_caller.append(28)                 # ft8
        fp_caller.append(29)                 # ft9
        fp_callee = [8, 9]                   # fs0, fs1
        rr = 18
        while rr <= 27:                      # fs2..fs11
            fp_callee.append(rr)
            rr += 1

        busy_int = {}
        busy_fp = {}
        used_int_callee = {}
        used_fp_callee = {}
        reps = {}
        order = []
        for v in values:
            cv = self._il_canon(v, coalesce)
            if cv in reps or cv in forced:
                continue
            reps[cv] = 1
            order.append(cv)
        order.sort(key=lambda vv: start.get(vv, 0))
        reg_of, freg_of, spill = self._il_linear_scan(
            order, start, end, crosses, int_caller, int_callee,
            fp_caller, fp_callee, busy_int, busy_fp,
            used_int_callee, used_fp_callee)
        self._rv_freg = freg_of

        # Frame: ra (if any call) + used callee saves + spills, sp-relative.
        saved_int = []
        for r in range(18, 28):
            if r in used_int_callee:
                saved_int.append(r)
        # lp64d passes arguments past the eighth of each class on the stack at
        # [sp+0], [sp+8], ... of the *caller's* frame. Unlike arm64, this frame
        # is addressed off sp, so sp cannot move around a call -- the outgoing
        # area is instead reserved at the bottom of the frame and everything
        # else shifts up past it.
        outgoing = ((out_max * 8) + 15) & ~15
        self._rv_outgoing = outgoing
        off = outgoing
        self._rv_ra_off = 0
        if has_call:
            self._rv_ra_off = off
            off += 8
        int_save_off = {}
        for r in saved_int:
            int_save_off[r] = off
            off += 8
        saved_fp = []
        for r in [8, 9] + list(range(18, 28)):
            if r in used_fp_callee:
                saved_fp.append(r)
        fp_save_off = {}
        for r in saved_fp:
            fp_save_off[r] = off
            off += 8
        slot_of = {}
        for v in values:
            cv = self._il_canon(v, coalesce)
            if cv in reg_of:
                continue
            if cv not in slot_of:
                sz = cv.ctype.size
                if sz < 8:
                    sz = 8
                sz = sz + (-sz % 8)
                slot_of[cv] = off
                off += sz
            if v is not cv:
                slot_of[v] = slot_of[cv]
        for arg in coalesce:
            o = self._il_canon(arg, coalesce)
            if o in reg_of:
                reg_of[arg] = reg_of[o]
            if o in freg_of:
                freg_of[arg] = freg_of[o]
            if o in slot_of:
                slot_of[arg] = slot_of[o]
        for idx in range(n):
            c = cmds[idx]
            if isinstance(c, value_cmds.Set) and c.arg in coalesce:
                skip[idx] = 1

        frame = 0
        if len(saved_int) > 0 or len(saved_fp) > 0 or len(slot_of) > 0 \
                or has_call:
            frame = off + (-off % 16)
        self._rv_saved_int = saved_int
        self._rv_int_save_off = int_save_off
        self._rv_saved_fp = saved_fp
        self._rv_fp_save_off = fp_save_off

        self.asm_code.add(asm_cmds.AsmLabel(func))
        if frame:
            self._rv_sp_adjust(-frame)
            if has_call:
                self.asm_code.add(asm_cmds.Raw(
                    "sd\tra, %d(sp)" % self._rv_ra_off))
            for r in saved_int:
                self.asm_code.add(asm_cmds.Raw(
                    "sd\t%s, %d(sp)" % (self._rv_rn(r), int_save_off[r])))
            for r in saved_fp:
                self.asm_code.add(asm_cmds.Raw(
                    "fsd\t%s, %d(sp)" % (self._rv_frn(r), fp_save_off[r])))
        addrof_name = {}
        for idx in range(n):
            if idx in skip:
                continue
            self._lower_riscv(cmds[idx], idx, func, reg_of, slot_of,
                              frame, has_call, addrof_name)

    def _rv_binop(self, cmd, math_cmds, size):
        w = "w" if size <= 4 else ""
        if isinstance(cmd, math_cmds.Add):
            return "add" + w
        if isinstance(cmd, math_cmds.Subtr):
            return "sub" + w
        if isinstance(cmd, math_cmds.Mult):
            return "mul" + w
        return None

    def _lower_riscv(self, cmd, idx, func, reg_of, slot_of,
                     frame, has_call, addrof_name):
        import shivyc.il_cmds.control as control
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.math as math_cmds
        import shivyc.il_cmds.compare as cmp_cmds

        if isinstance(cmd, value_cmds.Set):
            out = cmd.output
            arg = cmd.arg
            of = out.ctype.is_floating()
            af = arg.ctype.is_floating()
            if of or af:
                if of and af:
                    src = self._rv_fuse(arg, 30, slot_of)
                    fd = self._rv_fdefreg(out, 30)
                    if out.ctype.size == arg.ctype.size:
                        if fd != src:
                            self.asm_code.add(asm_cmds.Raw(
                                "fmv.%s\t%s, %s"
                                % (self._rv_fsuffix(out), fd, src)))
                    else:                       # float <-> double
                        self.asm_code.add(asm_cmds.Raw(
                            "fcvt.%s.%s\t%s, %s"
                            % (self._rv_fsuffix(out),
                               self._rv_fsuffix(arg), fd, src)))
                    self._rv_fwb(out, 30, slot_of)
                elif of:                        # integer -> float
                    ra = self._rv_use(arg, 5, reg_of, slot_of)
                    fd = self._rv_fdefreg(out, 30)
                    sg = self._rv_ctype_signed(arg.ctype)
                    isz = "l" if arg.ctype.size > 4 else "w"
                    if not sg:
                        isz = isz + "u"
                    self.asm_code.add(asm_cmds.Raw(
                        "fcvt.%s.%s\t%s, %s"
                        % (self._rv_fsuffix(out), isz, fd, ra)))
                    self._rv_fwb(out, 30, slot_of)
                else:                           # float -> integer
                    fa = self._rv_fuse(arg, 30, slot_of)
                    rd = self._rv_defreg(out, 5, reg_of)
                    sg = self._rv_ctype_signed(out.ctype)
                    isz = "l" if out.ctype.size > 4 else "w"
                    if not sg:
                        isz = isz + "u"
                    # C truncates toward zero; RISC-V's default rounding mode
                    # is dynamic (round-to-nearest), so `rtz` is required, not
                    # decoration.
                    self.asm_code.add(asm_cmds.Raw(
                        "fcvt.%s.%s\t%s, %s, rtz"
                        % (isz, self._rv_fsuffix(arg), rd, fa)))
                    self._rv_wb(out, 5, reg_of, slot_of)
                return
            lit = getattr(arg, "literal", None)
            rd = self._rv_defreg(out, 5, reg_of)
            if lit is not None:
                self.asm_code.add(asm_cmds.Raw("li\t%s, %s" % (rd, lit.val)))
            else:
                rs = self._rv_use(arg, 5, reg_of, slot_of)
                self._rv_convert(rd, rs, out, arg)
            self._rv_wb(out, 5, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.Add) or isinstance(cmd, math_cmds.Subtr) \
                or isinstance(cmd, math_cmds.Mult):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if out.ctype.is_floating():
                fa = self._rv_fuse(ins[0], 30, slot_of)
                fb = self._rv_fuse(ins[1], 31, slot_of)
                fd = self._rv_fdefreg(out, 30)
                if isinstance(cmd, math_cmds.Add):
                    fop = "fadd"
                elif isinstance(cmd, math_cmds.Subtr):
                    fop = "fsub"
                else:
                    fop = "fmul"
                self.asm_code.add(asm_cmds.Raw(
                    "%s.%s\t%s, %s, %s"
                    % (fop, self._rv_fsuffix(out), fd, fa, fb)))
                self._rv_fwb(out, 30, slot_of)
                return
            ra = self._rv_use(ins[0], 5, reg_of, slot_of)
            rb = self._rv_use(ins[1], 6, reg_of, slot_of)
            rd = self._rv_defreg(out, 5, reg_of)
            op = self._rv_binop(cmd, math_cmds, out.ctype.size)
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, %s, %s" % (op, rd, ra, rb)))
            self._rv_wb(out, 5, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.Div) or isinstance(cmd, math_cmds.Mod):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if out.ctype.is_floating():        # Mod is not valid for floats
                fa = self._rv_fuse(ins[0], 30, slot_of)
                fb = self._rv_fuse(ins[1], 31, slot_of)
                fd = self._rv_fdefreg(out, 30)
                self.asm_code.add(asm_cmds.Raw(
                    "fdiv.%s\t%s, %s, %s"
                    % (self._rv_fsuffix(out), fd, fa, fb)))
                self._rv_fwb(out, 30, slot_of)
                return
            ra = self._rv_use(ins[0], 5, reg_of, slot_of)
            rb = self._rv_use(ins[1], 6, reg_of, slot_of)
            rd = self._rv_defreg(out, 5, reg_of)
            sg = not (out.ctype.is_pointer()
                      or (out.ctype.is_integral() and not out.ctype.signed))
            w = "w" if out.ctype.size <= 4 else ""
            if isinstance(cmd, math_cmds.Div):
                base = "div" if sg else "divu"
            else:
                base = "rem" if sg else "remu"
            self.asm_code.add(asm_cmds.Raw(
                "%s%s\t%s, %s, %s" % (base, w, rd, ra, rb)))
            self._rv_wb(out, 5, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.BitAnd) or isinstance(cmd, math_cmds.BitOr) \
                or isinstance(cmd, math_cmds.BitXor):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            ra = self._rv_use(ins[0], 5, reg_of, slot_of)
            rb = self._rv_use(ins[1], 6, reg_of, slot_of)
            rd = self._rv_defreg(out, 5, reg_of)
            if isinstance(cmd, math_cmds.BitAnd):
                op = "and"
            elif isinstance(cmd, math_cmds.BitOr):
                op = "or"
            else:
                op = "xor"
            # No `w` forms exist for the logical ops, and none are needed:
            # RV64 keeps 32-bit values sign-extended in registers, and a
            # bitwise combination of two sign-extended values is itself
            # sign-extended.
            self.asm_code.add(asm_cmds.Raw(
                "%s\t%s, %s, %s" % (op, rd, ra, rb)))
            self._rv_wb(out, 5, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.LBitShift) \
                or isinstance(cmd, math_cmds.RBitShift):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            ra = self._rv_use(ins[0], 5, reg_of, slot_of)
            rd = self._rv_defreg(out, 5, reg_of)
            # Right shift is arithmetic or logical by the *operand's*
            # signedness. The 32-bit `w` forms shift within 32 bits and
            # sign-extend the result, which is what a shift of an `int` means.
            if isinstance(cmd, math_cmds.LBitShift):
                base = "sll"
            else:
                ct = ins[0].ctype
                sg = not (ct.is_pointer()
                          or (ct.is_integral() and not ct.signed))
                base = "sra" if sg else "srl"
            narrow = out.ctype.size <= 4
            lit = getattr(ins[1], "literal", None)
            limit = 32 if narrow else 64
            if lit is not None and 0 <= lit.val < limit:
                op = base + "i" + ("w" if narrow else "")
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s, %d" % (op, rd, ra, lit.val)))
            else:
                rb = self._rv_use(ins[1], 6, reg_of, slot_of)
                op = base + ("w" if narrow else "")
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s, %s" % (op, rd, ra, rb)))
            self._rv_wb(out, 5, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.Neg) or isinstance(cmd, math_cmds.Not):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if out.ctype.is_floating():
                fa = self._rv_fuse(ins[0], 30, slot_of)
                fd = self._rv_fdefreg(out, 30)
                self.asm_code.add(asm_cmds.Raw(
                    "fneg.%s\t%s, %s"
                    % (self._rv_fsuffix(out), fd, fa)))
                self._rv_fwb(out, 30, slot_of)
                return
            ra = self._rv_use(ins[0], 5, reg_of, slot_of)
            rd = self._rv_defreg(out, 5, reg_of)
            if isinstance(cmd, math_cmds.Not):
                # `not` is xori rd, rs, -1; it needs no width variant.
                op = "not"
            else:
                op = "negw" if out.ctype.size <= 4 else "neg"
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, rd, ra)))
            self._rv_wb(out, 5, reg_of, slot_of)
            return


        if isinstance(cmd, cmp_cmds._GeneralCmp):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if ins[0].ctype.is_floating():
                # RISC-V compares floats straight into an integer register,
                # so there is no flag register or conditional-set step. All
                # three are *ordered* comparisons -- NaN yields 0 -- which is
                # what C wants for <, <=, > and >=; `!=` is the negation of
                # feq, and so correctly yields 1 for NaN.
                fa = self._rv_fuse(ins[0], 30, slot_of)
                fb = self._rv_fuse(ins[1], 31, slot_of)
                rd = self._rv_defreg(out, 5, reg_of)
                sfx = self._rv_fsuffix(ins[0])
                if isinstance(cmd, cmp_cmds.EqualCmp):
                    self.asm_code.add(asm_cmds.Raw(
                        "feq.%s\t%s, %s, %s" % (sfx, rd, fa, fb)))
                elif isinstance(cmd, cmp_cmds.NotEqualCmp):
                    self.asm_code.add(asm_cmds.Raw(
                        "feq.%s\t%s, %s, %s" % (sfx, rd, fa, fb)))
                    self.asm_code.add(asm_cmds.Raw(
                        "xori\t%s, %s, 1" % (rd, rd)))
                elif isinstance(cmd, cmp_cmds.LessCmp):
                    self.asm_code.add(asm_cmds.Raw(
                        "flt.%s\t%s, %s, %s" % (sfx, rd, fa, fb)))
                elif isinstance(cmd, cmp_cmds.GreaterCmp):
                    self.asm_code.add(asm_cmds.Raw(
                        "flt.%s\t%s, %s, %s" % (sfx, rd, fb, fa)))
                elif isinstance(cmd, cmp_cmds.LessOrEqCmp):
                    self.asm_code.add(asm_cmds.Raw(
                        "fle.%s\t%s, %s, %s" % (sfx, rd, fa, fb)))
                else:                          # GreaterOrEqCmp
                    self.asm_code.add(asm_cmds.Raw(
                        "fle.%s\t%s, %s, %s" % (sfx, rd, fb, fa)))
                self._rv_wb(out, 5, reg_of, slot_of)
                return
            ra = self._rv_use(ins[0], 5, reg_of, slot_of)
            rb = self._rv_use(ins[1], 6, reg_of, slot_of)
            rd = self._rv_defreg(out, 5, reg_of)
            if isinstance(cmd, cmp_cmds.EqualCmp):
                self.asm_code.add(asm_cmds.Raw("sub\t%s, %s, %s" % (rd, ra, rb)))
                self.asm_code.add(asm_cmds.Raw("seqz\t%s, %s" % (rd, rd)))
            elif isinstance(cmd, cmp_cmds.NotEqualCmp):
                self.asm_code.add(asm_cmds.Raw("sub\t%s, %s, %s" % (rd, ra, rb)))
                self.asm_code.add(asm_cmds.Raw("snez\t%s, %s" % (rd, rd)))
            elif isinstance(cmd, cmp_cmds.LessCmp):
                self.asm_code.add(asm_cmds.Raw("slt\t%s, %s, %s" % (rd, ra, rb)))
            elif isinstance(cmd, cmp_cmds.GreaterCmp):
                self.asm_code.add(asm_cmds.Raw("slt\t%s, %s, %s" % (rd, rb, ra)))
            elif isinstance(cmd, cmp_cmds.LessOrEqCmp):
                self.asm_code.add(asm_cmds.Raw("slt\t%s, %s, %s" % (rd, rb, ra)))
                self.asm_code.add(asm_cmds.Raw("xori\t%s, %s, 1" % (rd, rd)))
            else:                              # GreaterOrEqCmp
                self.asm_code.add(asm_cmds.Raw("slt\t%s, %s, %s" % (rd, ra, rb)))
                self.asm_code.add(asm_cmds.Raw("xori\t%s, %s, 1" % (rd, rd)))
            self._rv_wb(out, 5, reg_of, slot_of)
            return

        if isinstance(cmd, control.Label):
            self.asm_code.add(asm_cmds.AsmLabel(cmd.label))
            return
        if isinstance(cmd, control.Jump):
            self.asm_code.add(asm_cmds.Raw(
                "j\t%s" % spots.mangle_symbol(cmd.label)))
            return
        if isinstance(cmd, control.JumpZero):
            rc = self._rv_use(cmd.cond, 5, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw(
                "beqz\t%s, %s" % (rc, spots.mangle_symbol(cmd.label))))
            return
        if isinstance(cmd, control.JumpNotZero):
            rc = self._rv_use(cmd.cond, 5, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw(
                "bnez\t%s, %s" % (rc, spots.mangle_symbol(cmd.label))))
            return

        if isinstance(cmd, value_cmds.LoadStructArg):
            # A struct parameter too big for a register on SysV. Here every
            # struct parameter arrives the same way -- as the address of the
            # caller's object -- so this is the aggregate LoadArg case again:
            # copy it into our own frame to make the parameter by value.
            pidx = self._wasm_argmap.get(id(cmd), 0)
            self._wasm_push_addr(cmd.output, body)
            body.local_get(pidx)
            body.const_i32(cmd.output.ctype.size)
            body.memory_copy()
            return

        if isinstance(cmd, value_cmds.VaSaveBase):
            rd = self._rv_defreg(cmd.output, 5, reg_of)
            self.asm_code.add(asm_cmds.Raw("mv\t%s, t0" % rd))
            self._rv_wb(cmd.output, 5, reg_of, slot_of)
            return

        if isinstance(cmd, value_cmds.VaStartAddr):
            if cmd.base is None:
                raise NotImplementedError(
                    "riscv64 back end: va_start without a caller-provided "
                    "argument block is not implemented")
            rb = self._rv_use(cmd.base, 5, reg_of, slot_of)
            rd = self._rv_defreg(cmd.output, 6, reg_of)
            self.asm_code.add(asm_cmds.Raw(
                "addi\t%s, %s, %d" % (rd, rb, 8 * cmd.named_count)))
            self._rv_wb(cmd.output, 6, reg_of, slot_of)
            return

        if isinstance(cmd, value_cmds.LoadArg):
            if cmd.arg_num in self._rv_argstk:
                # sp was lowered by `frame`, so the caller's outgoing area
                # begins at frame(sp).
                off = frame + self._rv_argstk[cmd.arg_num]
                out = cmd.output
                if out.ctype.is_floating():
                    fd = self._rv_fdefreg(out, 30)
                    self.asm_code.add(asm_cmds.Raw(
                        "%s\t%s, %d(sp)" % (self._rv_fld_op(out), fd, off)))
                    self._rv_fwb(out, 30, slot_of)
                else:
                    rd = self._rv_defreg(out, 5, reg_of)
                    self.asm_code.add(asm_cmds.Raw(
                        "ld\t%s, %d(sp)" % (rd, off)))
                    self._rv_wb(out, 5, reg_of, slot_of)
                return
            if cmd.output.ctype.is_floating():
                self._rv_ffrom(10 + self._rv_argfp[cmd.arg_num],
                               cmd.output, slot_of)
            else:
                self._rv_from(10 + self._rv_arggp[cmd.arg_num], cmd.output,
                              reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.AddrOf):
            name = self.symbol_table.names.get(cmd.var)
            if name is not None and cmd.var.ctype.is_function():
                # As on arm64: record the name so a direct call stays direct,
                # and materialise the address so the value is usable as one.
                addrof_name[cmd.output] = name
                rd = self._rv_defreg(cmd.output, 5, reg_of)
                self.asm_code.add(asm_cmds.Raw(
                    "lla\t%s, %s" % (rd, spots.mangle_symbol(name))))
                self._rv_wb(cmd.output, 5, reg_of, slot_of)
                return
            rd = self._rv_defreg(cmd.output, 5, reg_of)
            gname = self._rv_glob.get(cmd.var)
            if gname is not None:
                self.asm_code.add(asm_cmds.Raw("lla\t%s, %s" % (rd, gname)))
            else:
                # Address of a local: sp + its frame slot. The variable was
                # forced to memory in _rv_function, so slot_of[var] exists.
                self.asm_code.add(asm_cmds.Raw(
                    "addi\t%s, sp, %d" % (rd, slot_of[cmd.var])))
            self._rv_wb(cmd.output, 5, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.ReadAt):
            ra = self._rv_use(cmd.addr, 5, reg_of, slot_of)
            out = cmd.output
            if out.ctype.is_floating():
                fd = self._rv_fdefreg(out, 30)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, 0(%s)" % (self._rv_fld_op(out), fd, ra)))
                self._rv_fwb(out, 30, slot_of)
                return
            rd = self._rv_defreg(out, 6, reg_of)
            op = self._rv_ld_op(out.ctype.size, self._rv_signed(out))
            self.asm_code.add(asm_cmds.Raw("%s\t%s, 0(%s)" % (op, rd, ra)))
            self._rv_wb(out, 6, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.SetAt):
            ra = self._rv_use(cmd.addr, 5, reg_of, slot_of)
            if cmd.val.ctype.is_floating():
                fv = self._rv_fuse(cmd.val, 30, slot_of)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, 0(%s)" % (self._rv_fst_op(cmd.val), fv, ra)))
                return
            rv = self._rv_use(cmd.val, 6, reg_of, slot_of)
            op = self._rv_st_op(cmd.val.ctype.size)
            self.asm_code.add(asm_cmds.Raw("%s\t%s, 0(%s)" % (op, rv, ra)))
            return
        if isinstance(cmd, value_cmds.ReadRel):
            # output = *(base + chunk*count)   (array / pointer indexed load)
            addr = self._rv_rel_addr(
                cmd.base, cmd.chunk, cmd.count, 28, reg_of, slot_of)
            out = cmd.output
            if out.ctype.is_floating():
                fd = self._rv_fdefreg(out, 30)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s" % (self._rv_fld_op(out), fd, addr)))
                self._rv_fwb(out, 30, slot_of)
                return
            rd = self._rv_defreg(out, 6, reg_of)
            op = self._rv_ld_op(out.ctype.size, self._rv_signed(out))
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, rd, addr)))
            self._rv_wb(out, 6, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.SetRel):
            # *(base + chunk*count) = val      (array / pointer indexed store)
            addr = self._rv_rel_addr(
                cmd.base, cmd.chunk, cmd.count, 28, reg_of, slot_of)
            if cmd.val.ctype.is_floating():
                fv = self._rv_fuse(cmd.val, 30, slot_of)
                self.asm_code.add(asm_cmds.Raw(
                    "%s\t%s, %s" % (self._rv_fst_op(cmd.val), fv, addr)))
                return
            rv = self._rv_use(cmd.val, 6, reg_of, slot_of)
            op = self._rv_st_op(cmd.val.ctype.size)
            self.asm_code.add(asm_cmds.Raw("%s\t%s, %s" % (op, rv, addr)))
            return
        if isinstance(cmd, value_cmds.AddrRel):
            # output = &(base + chunk*count)   (e.g. &a[i])
            out = cmd.output
            rd = self._rv_defreg(out, 6, reg_of)
            self._rv_rel_base(cmd.base, cmd.chunk, cmd.count, rd,
                              reg_of, slot_of)
            self._rv_wb(out, 6, reg_of, slot_of)
            return
        if isinstance(cmd, control.Call):
            name = addrof_name.get(cmd.func)
            if name is None:
                name = cmd.direct_name          # stackless-calls pass
            # A variadic call uses the same all-argument-block convention as
            # arm64: every argument goes in one contiguous stack block whose
            # base is handed over in t0, and the first eight of each class are
            # also loaded into their lp64d registers for the callee's named
            # parameters. t0 is a temporary, so nothing else wants it across
            # the call.
            variadic = getattr(cmd, "variadic", False)
            if variadic:
                voff = self._rv_outgoing - (len(cmd.args) * 8)
                if voff < 0:
                    voff = 0
                base_off = voff
                for arg in cmd.args:
                    if arg.ctype.is_floating():
                        src = self._rv_fuse(arg, 30, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "%s\t%s, %d(sp)"
                            % (self._rv_fst_op(arg), src, voff)))
                    else:
                        src = self._rv_use(arg, 6, reg_of, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "sd\t%s, %d(sp)" % (src, voff)))
                    voff += 8
                self.asm_code.add(asm_cmds.Raw(
                    "addi\tt0, sp, %d" % base_off))
            gp = 0
            fp = 0
            soff = 0
            for arg in cmd.args:
                if variadic:
                    if arg.ctype.is_floating():
                        if fp < 8:
                            self._rv_finto(arg, 10 + fp, slot_of)
                        fp += 1
                    else:
                        if gp < 8:
                            self._rv_into(arg, 10 + gp, reg_of, slot_of)
                        gp += 1
                    continue
                onstack = (fp >= 8) if arg.ctype.is_floating() else (gp >= 8)
                if onstack:
                    # The outgoing area sits at the bottom of our own frame,
                    # so sp does not move: store straight to soff(sp).
                    if arg.ctype.is_floating():
                        src = self._rv_fuse(arg, 30, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "%s\t%s, %d(sp)"
                            % (self._rv_fst_op(arg), src, soff)))
                        fp += 1
                    else:
                        src = self._rv_use(arg, 5, reg_of, slot_of)
                        self.asm_code.add(asm_cmds.Raw(
                            "sd\t%s, %d(sp)" % (src, soff)))
                        gp += 1
                    soff += 8
                elif arg.ctype.is_floating():
                    self._rv_finto(arg, 10 + fp, slot_of)     # arg -> fa<fp>
                    fp += 1
                else:
                    self._rv_into(arg, 10 + gp, reg_of, slot_of)
                    gp += 1
            if name is not None:
                self.asm_code.add(asm_cmds.Raw(
                    "call\t%s" % spots.mangle_symbol(name)))
            else:
                # Indirect call: load the target after the arguments are
                # staged, so materialising it cannot disturb a0-a7. t3 is
                # scratch and never a value home.
                ra = self._rv_use(cmd.func, 28, reg_of, slot_of)
                self.asm_code.add(asm_cmds.Raw("jalr\t%s" % ra))
            if not cmd.void_return:
                if cmd.ret.ctype.is_floating():
                    self._rv_ffrom(10, cmd.ret, slot_of)      # fa0 -> ret home
                else:
                    self._rv_from(10, cmd.ret, reg_of, slot_of)
            return
        if isinstance(cmd, control.Return):
            if cmd.arg is not None:
                if cmd.arg.ctype.is_floating():
                    self._rv_finto(cmd.arg, 10, slot_of)      # fa0
                else:
                    self._rv_into(cmd.arg, 10, reg_of, slot_of)   # a0
            self._rv_epilogue(frame, has_call)
            return
        raise NotImplementedError(
            "riscv64 back end: IL command '%s' not implemented yet"
            % type(cmd).__name__)

    # ================= Motorola 68000 (m68k / NeoGeo) back end =================
    # The Neo-Geo's main CPU is a Motorola 68000; ngdevkit cross-compiles C to
    # m68k with gcc. This back end is the first step toward that target and is a
    # real stress test of the seam, because the 68000 is unlike every back end so
    # far: it is CISC and big-endian, has two register files (data d0-d7,
    # address a0-a7), two-address instructions (dst OP= src), .b/.w/.l operation
    # sizes, and a fully stack-based calling convention (no register arguments).
    #
    # Despite all that it reuses the architecture-neutral middle end verbatim --
    # copy-coalescing safety, liveness, live intervals, and the linear-scan
    # allocator (the _il_* methods). Only instruction selection, the register
    # file, and the m68k frame/ABI below are new. Scope is the integer core
    # (locals, + - * / %, the six comparisons, if/while, stack-argument calls,
    # recursion); unsupported IL raises rather than miscompile.
    #
    # Model: values live in data registers d2-d7 (callee-saved), spilling to
    # fp-relative frame slots; d0/d1 are the compute scratch. Each binop computes
    # in d0 and stores to the home, the simplest correct lowering of a two-address
    # CISC ISA. Frames use a6 as frame pointer via link/unlk; arguments are read
    # at 8(%fp)+4*k and pushed in reverse for calls (caller cleans the stack).
    # Note: muls.l / divsl.l are 68020+; a real 68000 (Neo-Geo) needs 16-bit
    # multiply/divide or libgcc helpers -- a later step. Validated under qemu-m68k
    # against m68k-linux-gnu-gcc, which (like aarch64-linux for bare-metal arm64)
    # is the practical oracle for the same instruction set.

    def _make_asm_m68k(self):
        """m68k (Neo-Geo main CPU) lowering. Runs only under `--target m68k`."""
        EXTERNAL = self.symbol_table.EXTERNAL
        DEFINED = self.symbol_table.DEFINED
        for v in self.symbol_table.linkages[EXTERNAL].values():
            if self.symbol_table.def_state.get(v) == DEFINED:
                self.asm_code.add_global(self.symbol_table.names[v])
        self._m68_gemit = {}                  # global symbol -> emitted once
        # String-literal storage (.byte data at each strlit symbol), mirroring
        # the x86 path so `char *p = "..."` and friends resolve. Names are
        # generated here if absent and recorded so references use the same one.
        snum = 0
        for v in self.il_code.string_literals:
            nm = self.il_code.string_literal_names.get(v)
            if nm is None:
                nm = "__strlit%d" % snum
                self.il_code.string_literal_names[v] = nm
            snum += 1
            elem_size = v.ctype.el.size if v.ctype.is_array() else 1
            self.asm_code.add_string_literal(
                nm, self.il_code.string_literals[v], elem_size)
        for func in self.il_code.commands:
            self._m68_function(func, self.il_code.commands[func])

    def _m68_src(self, value, reg_of, slot_of):
        """Source operand string for `value`: immediate, scalar global (absolute
        symbol), data register, or its fp-relative spill slot."""
        lit = getattr(value, "literal", None)
        if lit is not None:
            return "#%s" % lit.val
        g = getattr(self, "_m68_glob", {}).get(value)
        if g is not None:
            return g                           # absolute addressing on the symbol
        r = reg_of.get(value, -1)
        if r >= 0:
            return "%%d%d" % r
        return "%d(%%fp)" % slot_of[value]

    def _m68_store(self, value, from_dreg, reg_of, slot_of):
        """Store data register d<from> into `value`'s home (register, scalar
        global, or fp-relative slot)."""
        g = getattr(self, "_m68_glob", {}).get(value)
        if g is not None:
            self.asm_code.add(asm_cmds.Raw(
                "move.l %%d%d,%s" % (from_dreg, g)))
            return
        r = reg_of.get(value, -1)
        if r >= 0:
            if r != from_dreg:
                self.asm_code.add(asm_cmds.Raw(
                    "move.l %%d%d,%%d%d" % (from_dreg, r)))
        else:
            self.asm_code.add(asm_cmds.Raw(
                "move.l %%d%d,%d(%%fp)" % (from_dreg, slot_of[value])))

    def _m68_epilogue(self, use_fp):
        for r in reversed(self._m68_saved):
            self.asm_code.add(asm_cmds.Raw("move.l (%%sp)+,%%d%d" % r))
        if use_fp:
            self.asm_code.add(asm_cmds.Raw("unlk %fp"))
        self.asm_code.add(asm_cmds.Raw("rts"))

    def _m68_emit_global_storage(self, v):
        """Emit `.comm`/`.data` storage for a static/file-scope global `v` once
        (the directives are target-neutral GAS, assembled big-endian for m68k)."""
        name = self.symbol_table.asm_name(v)
        if name in self._m68_gemit:
            return
        self._m68_gemit[name] = 1
        TENTATIVE = self.symbol_table.TENTATIVE
        INTERNAL = self.symbol_table.INTERNAL
        # Scalars (int / long / pointer / char / short) are stored as a single
        # 4-byte cell so a `move.l` on the symbol reads the value correctly on
        # this big-endian target; aggregates keep their full size.
        sz = v.ctype.size
        if v.ctype.is_scalar():
            sz = 4
        if self.symbol_table.def_state.get(v) == TENTATIVE:
            local = (self.symbol_table.linkage_type[v] == INTERNAL)
            self.asm_code.add_comm(name, sz, local)
        elif v in self.il_code.static_block_inits:
            entries, total = self.il_code.static_block_inits[v]
            self._m68_emit_data_block(name, entries, total, v)
        else:
            init_val = self.il_code.static_inits.get(v, 0)
            self.asm_code.add_data(name, sz, init_val)

    def _m68_emit_data_block(self, name, entries, total, v):
        """Emit an initialized aggregate/static object for m68k. Plain numeric
        blocks reuse the shared emitter; a scalar pointer initialized to an
        address emits a 4-byte `.int` relocation (m68k pointers are 4 bytes, and
        an 8-byte `.quad` reloc is unencodable). Aggregates with pointer-valued
        initializers (8-byte field stride) are not laid out yet."""
        has_sym = any(isinstance(val, tuple) and val and val[0] == "sym"
                      for _off, _size, val in entries)
        if not has_sym:
            self.asm_code.add_data_block(name, entries, total)
            return
        if v.ctype.is_array() or v.ctype.is_struct_union():
            raise NotImplementedError(
                "m68k back end: aggregate with pointer-valued initializer is"
                " not implemented")
        from shivyc.spots import mangle_symbol
        self.asm_code.data.append("%s:" % mangle_symbol(name))
        _off, _size, val = sorted(entries, key=lambda e: e[0])[0]
        _, sym, addend = val
        msym = mangle_symbol(sym)
        ref = msym if not addend else "%s+%d" % (msym, addend)
        self.asm_code.data.append("\t.int %s" % ref)

    def _m68_base_addr(self, base, areg, reg_of, slot_of):
        """Materialize the base *address* into %a<areg>: a global aggregate's or
        local aggregate's address, or a scalar pointer's value (the address it
        holds). Returns the register name."""
        a = "%%a%d" % areg
        g = self._m68_glob.get(base)
        if g is not None:
            if base.ctype.is_array() or base.ctype.is_struct_union():
                self.asm_code.add(asm_cmds.Raw("lea %s,%s" % (g, a)))
            else:                              # global scalar pointer: its value
                self.asm_code.add(asm_cmds.Raw("move.l %s,%s" % (g, a)))
            return a
        if base.ctype.is_array() or base.ctype.is_struct_union():
            self.asm_code.add(asm_cmds.Raw(
                "lea %d(%%fp),%s" % (slot_of[base], a)))
            return a
        src = self._m68_src(base, reg_of, slot_of)
        self.asm_code.add(asm_cmds.Raw("move.l %s,%s" % (src, a)))
        return a

    def _m68_addr_into(self, base, chunk, count, areg, reg_of, slot_of):
        """Compute base_address + chunk*count into %a<areg> (count None -> just
        + chunk). Uses %d0 for a variable index, so call before loading a value
        into %d0."""
        a = self._m68_base_addr(base, areg, reg_of, slot_of)
        if count is None:
            if chunk:
                self.asm_code.add(asm_cmds.Raw("adda.l #%d,%s" % (chunk, a)))
            return a
        lit = getattr(count, "literal", None)
        if lit is not None:
            off = chunk * lit.val
            if off:
                self.asm_code.add(asm_cmds.Raw("adda.l #%d,%s" % (off, a)))
            return a
        csrc = self._m68_src(count, reg_of, slot_of)
        self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % csrc))
        if chunk != 1:
            self.asm_code.add(asm_cmds.Raw("muls.l #%d,%%d0" % chunk))
        self.asm_code.add(asm_cmds.Raw("adda.l %%d0,%s" % a))
        return a

    def _m68_load_sized(self, ea, size, signed, dreg):
        """Load `size` bytes from effective address string `ea` into %d<dreg>,
        sign- or zero-extending sub-word loads to a full 32-bit value."""
        d = "%%d%d" % dreg
        if size >= 4:
            self.asm_code.add(asm_cmds.Raw("move.l %s,%s" % (ea, d)))
        elif size == 2:
            if signed:
                self.asm_code.add(asm_cmds.Raw("move.w %s,%s" % (ea, d)))
                self.asm_code.add(asm_cmds.Raw("ext.l %s" % d))
            else:
                self.asm_code.add(asm_cmds.Raw("moveq #0,%s" % d))
                self.asm_code.add(asm_cmds.Raw("move.w %s,%s" % (ea, d)))
        else:
            if signed:
                self.asm_code.add(asm_cmds.Raw("move.b %s,%s" % (ea, d)))
                self.asm_code.add(asm_cmds.Raw("extb.l %s" % d))
            else:
                self.asm_code.add(asm_cmds.Raw("moveq #0,%s" % d))
                self.asm_code.add(asm_cmds.Raw("move.b %s,%s" % (ea, d)))

    def _m68_store_sized(self, ea, size, dreg):
        """Store the low `size` bytes of %d<dreg> to effective address `ea`."""
        d = "%%d%d" % dreg
        suf = "l" if size >= 4 else ("w" if size == 2 else "b")
        self.asm_code.add(asm_cmds.Raw("move.%s %s,%s" % (suf, d, ea)))

    def _m68_signed(self, value):
        ct = value.ctype
        if ct.is_pointer() or ct.is_array() or ct.is_struct_union():
            return False
        return bool(getattr(ct, "signed", True))

    def _m68_function(self, func, cmds):
        import shivyc.il_cmds.control as control
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.math as math_cmds
        import shivyc.il_cmds.compare as cmp_cmds
        n = len(cmds)
        # Function-call targets (AddrOf of a function) are resolved at compile
        # time via addrof_name and never occupy a register; collect them so the
        # integer-core check below does not reject their pointer type.
        funcptr = {}
        for c in cmds:
            if isinstance(c, value_cmds.AddrOf) and c.var.ctype.is_function():
                funcptr[c.output] = 1
        values = []
        seen = {}
        has_call = False
        has_arg = False
        STATIC = self.symbol_table.STATIC
        # Static / file-scope globals live at a symbol, not in the frame; record
        # each and emit its storage once. They never occupy a register.
        glob = {}
        for c in cmds:
            if isinstance(c, control.Call):
                has_call = True
            if isinstance(c, value_cmds.LoadArg):
                has_arg = True
            for v in c.inputs() + c.outputs():
                if v is None:
                    continue
                if v in self.il_code.string_literals:
                    nm = self.il_code.string_literal_names.get(v)
                    if nm is not None and v not in glob:
                        glob[v] = nm          # address is the strlit symbol
                    continue
                if getattr(v, "literal", None) is not None or v in funcptr:
                    continue
                if self.symbol_table.storage.get(v) == STATIC:
                    if v not in glob:
                        glob[v] = self.symbol_table.asm_name(v)
                        self._m68_emit_global_storage(v)
                    continue
                if v.ctype.is_floating():
                    raise NotImplementedError(
                        "m68k back end: floating point is not implemented")
                # The front end (an x86-64 compiler) sizes `long` and pointers
                # at 8 bytes, but on m68k they are 4 (matching m68k-linux and the
                # oracle), and compiler-generated index/offset `long`s fit in 32
                # bits. So every integer scalar is treated as a 4-byte value (its
                # low long). True 64-bit `long long` arithmetic is a known limit.
                if v not in seen:
                    seen[v] = 1
                    values.append(v)
        self._m68_glob = glob

        # A value whose address is taken, or that is an aggregate (array/struct,
        # too big for a register), must live in memory so a real address exists.
        forced = {}
        for c in cmds:
            if isinstance(c, value_cmds.AddrOf) \
                    and not c.var.ctype.is_function() \
                    and c.var not in glob:
                forced[c.var] = 1
        for v in values:
            if v.ctype.is_array() or v.ctype.is_struct_union():
                forced[v] = 1
        fused_out = {}
        usecount = {}
        defcount = {}
        for c in cmds:
            for v in c.inputs():
                if v is not None:
                    usecount[v] = usecount.get(v, 0) + 1
            for v in c.outputs():
                if v is not None:
                    defcount[v] = defcount.get(v, 0) + 1

        defidx = {}
        for idx in range(n):
            for v in cmds[idx].outputs():
                if v is not None:
                    defidx[v] = idx
        coalesce = {}
        skip = {}
        for k in range(n):
            c = cmds[k]
            if isinstance(c, value_cmds.Set):
                arg = c.arg
                out = c.output
                if getattr(arg, "literal", None) is None \
                        and usecount.get(arg, 0) == 1 \
                        and defcount.get(arg, 0) == 1 \
                        and out.ctype.size == arg.ctype.size \
                        and self._il_coalesce_safe(
                            cmds, defidx.get(arg, -1), k, out):
                    coalesce[arg] = out

        uses = []
        defs = []
        for idx in range(n):
            c = cmds[idx]
            u = []
            d = []
            for v in c.inputs():
                if v is not None and getattr(v, "literal", None) is None \
                        and v not in funcptr:
                    u.append(self._il_canon(v, coalesce))
            for v in c.outputs():
                if v is not None and getattr(v, "literal", None) is None \
                        and v not in funcptr:
                    d.append(self._il_canon(v, coalesce))
            uses.append(u)
            defs.append(d)
        live_in, live_out = self._il_liveness(cmds, n, uses, defs)
        start, end, crosses = self._il_intervals(
            cmds, n, live_in, live_out, uses, defs)

        # Homes are the callee-saved data registers d2-d7; d0/d1 are scratch, so
        # the caller-saved pool is empty and every value home is callee-saved.
        int_callee = [2, 3, 4, 5, 6, 7]
        busy_int = {}
        busy_fp = {}
        used_int_callee = {}
        used_fp_callee = {}
        reps = {}
        order = []
        for v in values:
            cv = self._il_canon(v, coalesce)
            if cv in reps or cv in forced or cv in glob:
                continue
            reps[cv] = 1
            order.append(cv)
        order.sort(key=lambda vv: start.get(vv, 0))
        reg_of, freg_of, spill = self._il_linear_scan(
            order, start, end, crosses, [], int_callee, [], [],
            busy_int, busy_fp, used_int_callee, used_fp_callee)

        saved = []
        for r in range(2, 8):
            if r in used_int_callee:
                saved.append(r)
        self._m68_saved = saved
        # Frame slots at fp-relative negative offsets (the link reserves them):
        # spilled scalars, address-taken locals, and aggregates -- each sized to
        # its type so an array/struct gets its whole block. Globals are excluded.
        slot_of = {}
        off = 0
        for v in values:
            cv = self._il_canon(v, coalesce)
            if cv in reg_of or cv in glob:
                continue
            if cv not in slot_of:
                if cv.ctype.is_array() or cv.ctype.is_struct_union():
                    sz = cv.ctype.size           # aggregate: whole block
                else:
                    sz = 4                       # scalar (pointers are 4 on m68k)
                sz = sz + (sz % 2)               # even alignment
                off += sz
                slot_of[cv] = -off              # base (lowest address) of slot
            if v is not cv:
                slot_of[v] = slot_of[cv]
        for arg in coalesce:
            o = self._il_canon(arg, coalesce)
            if o in reg_of:
                reg_of[arg] = reg_of[o]
            if o in slot_of:
                slot_of[arg] = slot_of[o]
        for idx in range(n):
            c = cmds[idx]
            if isinstance(c, value_cmds.Set) and c.arg in coalesce:
                skip[idx] = 1

        spillsize = off + (off % 2)          # keep the frame even
        use_fp = (len(slot_of) > 0) or has_arg or has_call

        self.asm_code.add(asm_cmds.AsmLabel(func))
        if use_fp:
            self.asm_code.add(asm_cmds.Raw("link.w %%fp,#-%d" % spillsize))
        for r in saved:
            self.asm_code.add(asm_cmds.Raw("move.l %%d%d,-(%%sp)" % r))
        addrof_name = {}
        for idx in range(n):
            if idx in skip:
                continue
            self._lower_m68k(cmds[idx], idx, func, reg_of, slot_of,
                             use_fp, addrof_name)

    def _lower_m68k(self, cmd, idx, func, reg_of, slot_of, use_fp, addrof_name):
        import shivyc.il_cmds.control as control
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.math as math_cmds
        import shivyc.il_cmds.compare as cmp_cmds

        if isinstance(cmd, value_cmds.Set):
            out = cmd.output
            src = self._m68_src(cmd.arg, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % src))
            self._m68_store(out, 0, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.Add) or isinstance(cmd, math_cmds.Subtr) \
                or isinstance(cmd, math_cmds.Mult):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            a = self._m68_src(ins[0], reg_of, slot_of)
            b = self._m68_src(ins[1], reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % a))
            if isinstance(cmd, math_cmds.Add):
                op = "add.l"
            elif isinstance(cmd, math_cmds.Subtr):
                op = "sub.l"
            else:
                op = "muls.l"
            self.asm_code.add(asm_cmds.Raw("%s %s,%%d0" % (op, b)))
            self._m68_store(out, 0, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.BitAnd) \
                or isinstance(cmd, math_cmds.BitOr) \
                or isinstance(cmd, math_cmds.BitXor):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            a = self._m68_src(ins[0], reg_of, slot_of)
            b = self._m68_src(ins[1], reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % a))
            if isinstance(cmd, math_cmds.BitAnd):
                self.asm_code.add(asm_cmds.Raw("and.l %s,%%d0" % b))
            elif isinstance(cmd, math_cmds.BitOr):
                self.asm_code.add(asm_cmds.Raw("or.l %s,%%d0" % b))
            else:                              # eor's source must be a Dn
                self.asm_code.add(asm_cmds.Raw("move.l %s,%%d1" % b))
                self.asm_code.add(asm_cmds.Raw("eor.l %d1,%d0"))
            self._m68_store(out, 0, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.LBitShift) \
                or isinstance(cmd, math_cmds.RBitShift):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            a = self._m68_src(ins[0], reg_of, slot_of)
            b = self._m68_src(ins[1], reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % a))
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d1" % b))
            if isinstance(cmd, math_cmds.LBitShift):
                op = "lsl.l"
            else:                              # arithmetic if the value is signed
                op = "asr.l" if self._m68_signed(ins[0]) else "lsr.l"
            self.asm_code.add(asm_cmds.Raw("%s %%d1,%%d0" % op))
            self._m68_store(out, 0, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.Not) or isinstance(cmd, math_cmds.Neg):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            a = self._m68_src(ins[0], reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % a))
            op = "not.l" if isinstance(cmd, math_cmds.Not) else "neg.l"
            self.asm_code.add(asm_cmds.Raw("%s %%d0" % op))
            self._m68_store(out, 0, reg_of, slot_of)
            return

        if isinstance(cmd, math_cmds.Div) or isinstance(cmd, math_cmds.Mod):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            a = self._m68_src(ins[0], reg_of, slot_of)
            b = self._m68_src(ins[1], reg_of, slot_of)
            sg = not (out.ctype.is_integral() and not out.ctype.signed)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % a))
            op = "divsl.l" if sg else "divul.l"
            # quotient -> d0, remainder -> d1
            self.asm_code.add(asm_cmds.Raw("%s %s,%%d1:%%d0" % (op, b)))
            res = 1 if isinstance(cmd, math_cmds.Mod) else 0
            self._m68_store(out, res, reg_of, slot_of)
            return

        if isinstance(cmd, cmp_cmds._GeneralCmp):
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            a = self._m68_src(ins[0], reg_of, slot_of)
            b = self._m68_src(ins[1], reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % a))
            self.asm_code.add(asm_cmds.Raw("cmp.l %s,%%d0" % b))  # d0 - b
            if isinstance(cmd, cmp_cmds.EqualCmp):
                sc = "seq"
            elif isinstance(cmd, cmp_cmds.NotEqualCmp):
                sc = "sne"
            elif isinstance(cmd, cmp_cmds.LessCmp):
                sc = "slt"
            elif isinstance(cmd, cmp_cmds.GreaterCmp):
                sc = "sgt"
            elif isinstance(cmd, cmp_cmds.LessOrEqCmp):
                sc = "sle"
            else:
                sc = "sge"
            self.asm_code.add(asm_cmds.Raw("%s %%d0" % sc))
            self.asm_code.add(asm_cmds.Raw("and.l #1,%d0"))
            self._m68_store(out, 0, reg_of, slot_of)
            return

        if isinstance(cmd, control.Label):
            self.asm_code.add(asm_cmds.AsmLabel(cmd.label))
            return
        if isinstance(cmd, control.Jump):
            self.asm_code.add(asm_cmds.Raw(
                "jra %s" % spots.mangle_symbol(cmd.label)))
            return
        if isinstance(cmd, control.JumpZero):
            src = self._m68_src(cmd.cond, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % src))
            self.asm_code.add(asm_cmds.Raw("tst.l %d0"))
            self.asm_code.add(asm_cmds.Raw(
                "jeq %s" % spots.mangle_symbol(cmd.label)))
            return
        if isinstance(cmd, control.JumpNotZero):
            src = self._m68_src(cmd.cond, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % src))
            self.asm_code.add(asm_cmds.Raw("tst.l %d0"))
            self.asm_code.add(asm_cmds.Raw(
                "jne %s" % spots.mangle_symbol(cmd.label)))
            return

        if isinstance(cmd, value_cmds.LoadArg):
            # Argument k arrives on the stack at 8(%fp)+4*k.
            off = 8 + 4 * cmd.arg_num
            self.asm_code.add(asm_cmds.Raw("move.l %d(%%fp),%%d0" % off))
            self._m68_store(cmd.output, 0, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.AddrOf):
            name = self.symbol_table.names.get(cmd.var)
            if name is not None and cmd.var.ctype.is_function():
                addrof_name[cmd.output] = name
                return
            g = self._m68_glob.get(cmd.var)
            if g is not None:                  # &global: address is the symbol
                self.asm_code.add(asm_cmds.Raw("lea %s,%%a0" % g))
            else:                              # &local: fp + frame slot
                self.asm_code.add(asm_cmds.Raw(
                    "lea %d(%%fp),%%a0" % slot_of[cmd.var]))
            self.asm_code.add(asm_cmds.Raw("move.l %a0,%d0"))
            self._m68_store(cmd.output, 0, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.ReadAt):
            # output = *addr   (addr is a pointer value)
            asrc = self._m68_src(cmd.addr, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%a0" % asrc))
            self._m68_load_sized("(%a0)", cmd.output.ctype.size,
                                 self._m68_signed(cmd.output), 0)
            self._m68_store(cmd.output, 0, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.SetAt):
            # *addr = val
            asrc = self._m68_src(cmd.addr, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%a0" % asrc))
            vsrc = self._m68_src(cmd.val, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % vsrc))
            self._m68_store_sized("(%a0)", cmd.val.ctype.size, 0)
            return
        if isinstance(cmd, value_cmds.ReadRel):
            # output = *(base + chunk*count)   (array / pointer indexed load)
            self._m68_addr_into(cmd.base, cmd.chunk, cmd.count, 0,
                                reg_of, slot_of)
            self._m68_load_sized("(%a0)", cmd.output.ctype.size,
                                 self._m68_signed(cmd.output), 0)
            self._m68_store(cmd.output, 0, reg_of, slot_of)
            return
        if isinstance(cmd, value_cmds.SetRel):
            # *(base + chunk*count) = val
            self._m68_addr_into(cmd.base, cmd.chunk, cmd.count, 0,
                                reg_of, slot_of)
            vsrc = self._m68_src(cmd.val, reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % vsrc))
            self._m68_store_sized("(%a0)", cmd.val.ctype.size, 0)
            return
        if isinstance(cmd, value_cmds.AddrRel):
            # output = &(base + chunk*count)   (e.g. &a[i])
            self._m68_addr_into(cmd.base, cmd.chunk, cmd.count, 0,
                                reg_of, slot_of)
            self.asm_code.add(asm_cmds.Raw("move.l %a0,%d0"))
            self._m68_store(cmd.output, 0, reg_of, slot_of)
            return
        if isinstance(cmd, control.Call):
            name = addrof_name.get(cmd.func)
            if name is None:
                name = cmd.direct_name          # stackless-calls pass
            if name is None:
                raise NotImplementedError(
                    "m68k back end: indirect calls are not implemented")
            i = len(cmd.args) - 1
            while i >= 0:                    # push arguments right-to-left
                src = self._m68_src(cmd.args[i], reg_of, slot_of)
                self.asm_code.add(asm_cmds.Raw("move.l %s,-(%%sp)" % src))
                i -= 1
            self.asm_code.add(asm_cmds.Raw(
                "jsr %s" % spots.mangle_symbol(name)))
            if len(cmd.args) > 0:            # caller cleans the stack
                self.asm_code.add(asm_cmds.Raw(
                    "lea (%d,%%sp),%%sp" % (4 * len(cmd.args))))
            if not cmd.void_return:
                self._m68_store(cmd.ret, 0, reg_of, slot_of)  # result in d0
            return
        if isinstance(cmd, control.Return):
            if cmd.arg is not None:
                src = self._m68_src(cmd.arg, reg_of, slot_of)
                self.asm_code.add(asm_cmds.Raw("move.l %s,%%d0" % src))
            self._m68_epilogue(use_fp)
            return
        raise NotImplementedError(
            "m68k back end: IL command '%s' not implemented yet"
            % type(cmd).__name__)

    def _apply_thread_budget(self, func):
        """Restrict `alloc_registers`/`all_registers` for `func` to its thread
        group's register budget, if one was supplied via
        `arguments._thread_alloc` ({func_name: [reg64_name, ...]}). Falls back
        to the full pool for unlisted functions.

        Always keeps at least a small scratch margin so the allocator can still
        spill via get_reg; correctness is preserved either way (out-of-budget
        pressure spills to memory rather than to another group's register).
        """
        table = getattr(self.arguments, "_thread_alloc", None)
        # AArch64 does not allocate from spots.registers at all: value homes
        # come from x19-x28 in _arm64_function. So the budget is handed over as
        # a set of register *numbers* there rather than by rewriting the x86
        # spot lists here -- setting alloc_registers would be silently ignored
        # and the partition would stay an observation rather than a guarantee.
        self._a64_budget = None
        if table:
            b = table.get(func)
            if b:
                nums = []
                for rn in b:
                    if rn[0:1] == "x" and rn[1:].isdigit():
                        n = int(rn[1:])
                        if 19 <= n <= 28:
                            nums.append(n)
                # Keep a floor so the allocator always has somewhere to put a
                # value; anything over budget spills to memory, which is
                # correct, never into the other thread's bank.
                if len(nums) >= 2:
                    self._a64_budget = nums
        if not table:
            self.alloc_registers = type(self).alloc_registers
            self.all_registers = type(self).all_registers
            return
        budget = table.get(func)
        if not budget:
            self.alloc_registers = type(self).alloc_registers
            self.all_registers = type(self).all_registers
            return
        by_name = {r.name: r for r in spots.registers}
        regs = [by_name[rn] for rn in budget if rn in by_name]
        if len(regs) < 2:  # keep a minimum so get_reg always has scratch
            return
        self.alloc_registers = regs
        self.all_registers = regs

    def _alloc_stack_slot(self, size):
        """Allocate a slot for a local/spill, returning its MemSpot.

        Normally this is an rbp-relative stack slot. For a function selected
        for -O4 near-function scratch, the slot instead lives in a static
        per-function buffer, so it never touches the stack.
        """
        if self._near_active:
            spot = MemSpot(self._near_label, self._near_off)
            self._near_off += size
            self._near_size = max(self._near_size, self._near_off)
            return spot
        # Slots are padded to eight bytes. A by-value struct of 3, 5, 6 or 7
        # bytes has no matching move width, so passing one reads it out of its
        # slot as a whole eightbyte; padding guarantees that read stays inside
        # the slot rather than picking up the neighbouring local. The bytes
        # above `size` are padding the callee never stores, so their contents
        # do not matter. Eight-byte slots also keep every local aligned.
        self.offset += size + (-size % 8)
        return MemSpot(spots.RBP, -self.offset)

    def _make_asm(self, commands, global_spotmap):
        """Generate ASM code for given command list."""

        # Get free values
        free_values = self._get_free_values(commands, global_spotmap)

        # If any variable may have its address referenced, assign it a
        # permanent memory spot if it doesn't yet have one.
        move_to_mem: List["ILValue"] = []
        for command in commands:
            refs = command.references().values()
            for line in refs:
                for v in line:
                    if v not in refs:
                        move_to_mem.append(v)

        # In addition, move all IL values of strange size to memory because
        # they won't fit in a register.
        for v in free_values:
            if v.ctype.size not in {1, 2, 4, 8}:
                move_to_mem.append(v)

        for v in free_values:
            if v.ctype.is_floating():
                move_to_mem.append(v)

        # TODO: All non-free IL values are automatically assigned distinct
        # memory spots. However, this is very inoptimal for structs.
        # Consider the following C code, where S is already declared:
        #
        #   struct S array[10];
        #   s = array[1];
        #
        # This code compiles to the following IL:
        #
        #   READAT(array, 1) -> X
        #   SET(X) -> s
        #
        # However, X is an unnecessary copy of `s` in memory. Ideally,
        # the register allocator will recognize that X is just a temporary
        # and assign X to the same memory location as s to avoid additional
        # copy operations and memory usage. This also requires that the
        # relevant IL commands check whether the two arguments are in the
        # same spot before trying to do a copy.
        # Address-taken locals must keep their normal stack layout: their
        # addresses are observable (and some programs do pointer arithmetic
        # across them), so they are never relocated to near-function scratch.
        # Track the per-function spots we fold into the shared global_spotmap
        # so they can be removed again after this function is emitted (keeping
        # the shared map globals-only and constant-size across functions).
        local_keys: List["ILValue"] = []
        for v in move_to_mem:
            if v in free_values:
                self.offset += v.ctype.size
                global_spotmap[v] = MemSpot(spots.RBP, -self.offset)
                free_values.remove(v)
                local_keys.append(v)

        # Perform liveliness analysis
        live_vars = self._get_live_vars(commands, free_values)

        # Generate conflict and preference graph
        g_bak = self._generate_graph(commands, free_values, live_vars)

        # Optimistic (Briggs) colouring. The previous allocator removed a node,
        # then rebuilt and re-ran the entire simplify/coalesce/freeze allocation
        # from a fresh graph copy and retried -- O(spills) full allocations,
        # which made assembly generation super-linear on high-register-pressure
        # functions (e.g. ~30 full restarts on a 40-live-variable function).
        # Instead, build the graph once and, whenever simplification stalls,
        # push the highest-degree "potential spill" node straight onto the
        # colouring stack and carry on in the same pass. When the node is popped
        # in _generate_spotmap it is coloured normally if any register is free
        # (optimism frequently succeeds, because its neighbours often share
        # colours) and only becomes a real stack spill otherwise.
        g = g_bak.copy_node()
        removed_nodes = []
        merged_nodes = {}

        while True:
            # Repeat simplification, coalescing, and freeze until freeze
            # does not work.
            while True:
                # Repeat simplification and coalescing until nothing
                # happens.
                while True:
                    simplified = self._simplify_all(removed_nodes, g)
                    merged = self._coalesce_all(merged_nodes, g)

                    if not simplified and not merged: break

                if not self._freeze(g):
                    break

            # If no real nodes remain, the graph is fully reduced.
            if not g.nodes():
                break

            # Otherwise optimistically remove the highest-degree node onto the
            # colouring stack and continue (removing it lowers its neighbours'
            # degrees and usually unblocks further simplification). Highest
            # degree is the spill heuristic; it is an explicit scan rather than
            # max(..., key=lambda n: len(g.confs(n))) because the self-hosting
            # transpiler drops the key= argument, which would pick an arbitrary,
            # often low-degree node.
            spill_node = None
            spill_deg = -1
            for cand in g.nodes():
                cand_deg = len(g.confs(cand))
                if cand_deg > spill_deg:
                    spill_deg = cand_deg
                    spill_node = cand
            removed_nodes.append(g.remove_node(spill_node))

        # Move any remaining nodes from graph into removed_nodes
        # This accounts for pseudonodes which cannot be removed in the
        # simplify phase.
        while g.all_nodes():
            removed_nodes.append(g.pop(g.all_nodes()[0]))

        # Pop values off the stack to generate spot assignments. A node that
        # finds no free register when it is popped is spilled to a stack slot
        # there and recorded in spilled_nodes.
        spilled_nodes = []
        spotmap = self._generate_spotmap(removed_nodes, merged_nodes, g_bak,
                                         spilled_nodes)

        # Fold this function's spots into the shared global spotmap and emit
        # against it directly. Copying the whole global spotmap into a fresh
        # per-function dict here was O(functions x globals) -- the dominant
        # quadratic in both time and peak memory of asm generation, since the
        # global spotmap holds every literal/static in the program. The folded
        # keys (regalloc results, spills, and the address-taken locals above)
        # are removed after emit so the shared map stays constant-size.
        for v in spotmap:
            global_spotmap[v] = spotmap[v]
            local_keys.append(v)

        if self.arguments.show_reg_alloc_perf:  # pragma: no cover
            total_prefs = 0
            matched_prefs = 0

            all_nodes_list = g_bak.all_nodes()
            for ia in range(len(all_nodes_list)):
                for ib in range(ia + 1, len(all_nodes_list)):
                    na = all_nodes_list[ia]
                    nb = all_nodes_list[ib]
                    if nb in g_bak.prefs(na):
                        total_prefs += 1
                        if spotmap[na] == spotmap[nb]:
                            matched_prefs += 1

            print("total prefs", total_prefs)
            print("matched prefs", matched_prefs)

            print("total ILValues", len(g_bak.nodes()))
            print("register ILValues", len(g_bak.nodes()) - len(spilled_nodes))

        # Generate assembly code. Pass the spots that belong to THIS function
        # (their keys are exactly local_keys) so frame-size and callee-saved
        # detection scan only the function's spots, not the whole program's
        # globals/literals held in the shared map.
        func_spots = [global_spotmap[v] for v in local_keys]
        self._generate_asm(commands, live_vars, global_spotmap, func_spots)

        # Remove this function's spots from the shared map so it does not grow
        # with the program (the source of the asm-gen quadratic).
        for v in local_keys:
            if v in global_spotmap:
                del global_spotmap[v]

    def _get_global_spotmap(self):
        """Generate global spotmap and add global values to ASM.

        This function generates a spotmap for variables which are not
        specific to a single function. This includes literals and variables
        with static storage duration.
        """
        global_spotmap = {}

        EXTERNAL = self.symbol_table.EXTERNAL
        DEFINED = self.symbol_table.DEFINED

        num = 0

        for value in (set(self.il_code.literals.keys())
                      | set(self.il_code.float_literals.keys())
                      | set(self.il_code.string_literals.keys())
                      | set(self.symbol_table.storage.keys())):
            num += 1
            spot = self._get_nondynamic_spot(value, num)
            if spot: global_spotmap[value] = spot

            # Detect qualifying small static globals for SIMD bit-packing.
            if (self.simd_pack_enabled
                    and isinstance(spot, MemSpot)
                    and isinstance(spot.base, str)
                    and self.symbol_table.storage.get(value)
                    == self.symbol_table.STATIC):
                self.simd_pack.consider(spot.base, value.ctype.size)

        externs = self.symbol_table.linkages[EXTERNAL].values()
        for v in externs:
            if self.symbol_table.def_state.get(v) == DEFINED:
                self.asm_code.add_global(self.symbol_table.names[v])

        return global_spotmap

    def _get_nondynamic_spot(self, v, num):
        """Get a spot for non-dynamic values.

        In particular, assigns a spot to all literals, string literals,
        variables with no storage, and variables with static storage.

        v - value to get a spot for, or None if the value goes in a dynamic
        spot like a register
        nnum - positive integer guaranteed never to be the same for two
        distinct calls to this function
        """
        EXTERNAL = self.symbol_table.EXTERNAL
        INTERNAL = self.symbol_table.INTERNAL
        TENTATIVE = self.symbol_table.TENTATIVE

        if v in self.il_code.literals:
            return LiteralSpot(self.il_code.literals[v])

        elif v in self.il_code.float_literals:
            import struct, math
            name = f"__fltlit{num}"
            val = self.il_code.float_literals[v]
            fmt = "<f" if v.ctype.size == 4 else "<d"
            try:
                raw = struct.pack(fmt, val)
            except OverflowError:
                # A finite literal whose magnitude exceeds the target type's
                # range converts to IEEE infinity (with the same sign) rather
                # than being an error.
                raw = struct.pack(fmt, math.copysign(float("inf"), val))
            if v.ctype.size == 4:
                bits = struct.unpack("<I", raw)[0]
                self.asm_code.add_data(name, 4, bits)
            else:
                bits = struct.unpack("<Q", raw)[0]
                self.asm_code.add_data(name, 8, bits)
            return MemSpot(name)

        elif v in self.il_code.string_literals:
            name = self.il_code.string_literal_names.get(v, f"__strlit{num}")
            elem_size = v.ctype.el.size if v.ctype.is_array() else 1
            self.asm_code.add_string_literal(
                name, self.il_code.string_literals[v], elem_size)
            return MemSpot(name)

        # Values with no storage can be referenced directly by name
        elif not self.symbol_table.storage.get(v, True):
            return MemSpot(self.symbol_table.names[v])

        elif self.symbol_table.storage.get(v) == self.symbol_table.STATIC:
            name = self.symbol_table.asm_name(v)

            if self.symbol_table.def_state.get(v) == TENTATIVE:
                local = (self.symbol_table.linkage_type[v] == INTERNAL)
                self.asm_code.add_comm(name, v.ctype.size, local)
            elif v in self.il_code.static_block_inits:
                entries, total = self.il_code.static_block_inits[v]
                self.asm_code.add_data_block(name, entries, total)
            else:
                init_val = self.il_code.static_inits.get(v, 0)
                self.asm_code.add_data(name, v.ctype.size, init_val)

            return MemSpot(name)

    def _get_free_values(self, commands, global_spotmap):
        """Generate list of free values.

        Returns a list of the free values, the variables which need
        allocation on the stack.
        """
        free_values = []
        for command in commands:
            for value in command.inputs() + command.outputs():
                if (value and value not in free_values
                      and value not in global_spotmap):
                    free_values.append(value)

        return free_values

    def _get_live_vars(self, commands, free_values):
        """Given a set of free ILValues, find when those ILValues are live.

        free_values - list of ILValues for which to perform liveliness analysis
        returns - array mapping command indices to a tuple where first
        element is a list of variables live coming into the command and the
        second is a list of the variables live exiting the command
        """
        # Preprocess all commands to get a mapping from labels to command
        # number.
        labels = {c.label_name(): i for i, c in enumerate(commands)
                  if c.label_name()}

        # Last iteration of live variables
        prev_live_vars = None

        # This iteration of live variables
        live_vars = [([], []) for i in range(len(commands))]

        # inputs(), outputs() and targets() depend only on the command, not on
        # the liveness state, yet the fixpoint below revisits every command on
        # every iteration. Each call rebuilds a fresh list, so recomputing them
        # in the loop allocated K x M transient lists (K = iterations to
        # converge, M = commands) -- a dominant source of asm-gen arena churn
        # and time on large functions. Compute each once, up front.
        cmd_inputs = [c.inputs() for c in commands]
        cmd_outputs = [c.outputs() for c in commands]
        cmd_targets = [c.targets() for c in commands]

        while live_vars != prev_live_vars:
            prev_live_vars = live_vars[:]

            # List of currently live variables
            cur_live = []

            # Iterate through commands in backwards order
            for i, command in list(enumerate(commands))[::-1]:
                # If current command is a jump, add the live inputs of all
                # possible targets to the current live list.
                for label in cmd_targets[i]:
                    i2 = labels[label]
                    for v in prev_live_vars[i2][0]:
                        if v not in cur_live:
                            cur_live.append(v)

                # Variables live on output from this command
                out_live = cur_live[:]

                # Add variables used in this command to current live variables
                for v in cmd_inputs[i]:
                    if v in free_values and v not in cur_live:
                        cur_live.append(v)

                # Remove variables defined in this command to live variables
                for v in cmd_outputs[i]:
                    if v in free_values:
                        if v in cur_live:
                            cur_live.remove(v)
                        else:
                            # If variable is defined in command but was not
                            # live, make it live on output from this command.

                            # TODO: Deal with this more efficiently.
                            # If the output is not live, then we don't actually
                            # need to perform this computation.
                            out_live.append(v)

                # Variables live on input from this command
                in_live = cur_live[:]

                live_vars[i] = (in_live, out_live)

        return live_vars

    def _generate_graph(self, commands, free_values, live_vars) -> "NodeGraph":
        """Generate the conflict/preference graph.

        free_values - List of ILValues to include in the graph
        live_vars - Live range information from _get_live_vars

        """
        g = NodeGraph(free_values)
        for i, command in enumerate(commands):
            # Variables active during input mutually conflict. (Explicit pair
            # loop rather than itertools.combinations, which the self-host
            # transpiler does not support -- it would silently drop every
            # conflict edge, letting simultaneously-live variables share a
            # register.)
            live_in = live_vars[i][0]
            for ia in range(len(live_in)):
                for ib in range(ia + 1, len(live_in)):
                    g.add_conflict(live_in[ia], live_in[ib])

            # Variables active during output
            live_out = live_vars[i][1]
            for ia in range(len(live_out)):
                for ib in range(ia + 1, len(live_out)):
                    g.add_conflict(live_out[ia], live_out[ib])

            # Relative conflict set of this command
            for na in command.rel_spot_conf():
                for nb in command.rel_spot_conf()[na]:
                    if na in free_values and nb in free_values:
                        g.add_conflict(na, nb)

            # Absolute conflict set of this command
            for nd in command.abs_spot_conf():
                for s in command.abs_spot_conf()[nd]:
                    if nd in free_values:
                        if s not in g.all_nodes():
                            g.add_dummy_node(s)
                        g.add_conflict(nd, s)

            # Clobber set of this command
            for s in command.clobber():
                if s not in g.all_nodes():
                    g.add_dummy_node(s)

                # Add a conflict with dummy node for every variable live
                # during both entry and exit from this command.
                for nd in live_vars[i][0]:
                    if nd in live_vars[i][1]:
                        g.add_conflict(nd, s)

            # Form preferences based on rel_spot_pref
            for v1 in command.rel_spot_pref():
                for v2 in command.rel_spot_pref()[v1]:
                    if g.is_node(v1) and g.is_node(v2):
                        g.add_pref(v1, v2)

            # Form preferences based on abs_spot_pref
            for v in command.abs_spot_pref():
                for s in command.abs_spot_pref()[v]:
                    if v in free_values:
                        if s not in g.all_nodes():
                            g.add_dummy_node(s)
                        g.add_pref(v, s)
        return g

    def _simplify_all(self, removed_nodes, g: "NodeGraph"):
        """Repeat the Simplify step until no more can be done.

        Returns False iff no simplification is done.

        removed_nodes - stack of removed nodes to which this function adds
        the nodes it removes
        """

        # Get nodes without preference edges
        no_pref = [v for v in g.nodes() if not g.prefs(v)]

        # Repeat simplification until no more nodes can be removed
        did_something = False
        while True:
            rem = self._simplify_once(no_pref, g)
            if rem:
                removed_nodes.append(rem)
                no_pref.remove(rem)
                did_something = True
            else:
                break

        return did_something

    def _simplify_once(self, nodes, g: "NodeGraph"):
        """Remove and return a node in nodes if it has low conflict degree."""
        for v in nodes:
            # If the node has low conflict degree remove it from the graph.
            # Use remove_node (not pop): see NodeGraph.remove_node -- a bare
            # g.pop(v) here is mis-lowered to a dict pop because g is an
            # un-inferred parameter, which silently fails to remove the node.
            if len(g.confs(v)) < len(self.alloc_registers):
                return g.remove_node(v)

    def _coalesce_all(self, merged_nodes, g: "NodeGraph"):
        """Repeat the coalesce step until no more can be done.

        Returns False iff no simplification is done.

        merged_nodes - Mapping from node to list of nodes. Every node in the
        list of nodes has been merged into the key node.
        """
        did_something = False
        nreg = len(self.alloc_registers)
        # The graph holds each node's conflict neighbours as a set, so the
        # coalesce step queries g.confs() directly (no separate cache to rebuild
        # each pass).
        #
        # Coalescing runs in full passes: each pass walks every node once and
        # merges it with a preference neighbour where the conservative
        # (Briggs/George) criterion allows, repeating until a whole pass merges
        # nothing. The earlier formulation restarted the scan from the first
        # node after every single merge, so M merges cost M full O(V*E) scans --
        # which, once optimistic colouring removed the spill-retry loop, became
        # the dominant time cost of register allocation on large functions. A
        # pass merges many independent pairs at once, cutting the number of
        # full scans to the few rounds needed to converge.
        while True:
            merged_any = False
            for v1 in list(g.nodes()):
                # v1 may have been merged away earlier in this same pass.
                if not g.is_node(v1):
                    continue
                merge = self._coalesce_node(g, v1, nreg)
                if merge:
                    if merge[0] not in merged_nodes:
                        merged_nodes[merge[0]] = []
                    merged_nodes[merge[0]].append(merge[1])
                    merged_any = True
                    did_something = True
            if not merged_any:
                break

        return did_something

    def _coalesce_node(self, g: "NodeGraph", v1, nreg):
        """Try to coalesce v1 with one of its preference neighbours.

        Returns the merged pair (preserved, removed) if a merge was completed,
        else None. The conservative criterion is unchanged: George's heuristic
        when one node is a precolored Spot, Briggs's combined-degree heuristic
        otherwise.
        """
        for v2 in list(g.prefs(v1)):
            # If the two nodes conflict, they can never be coalesced.
            if v2 in g.confs(v1):
                continue

            # Size of the merged conflict set (g.confs values are
            # dicts-used-as-sets, so count the union of their keys). The union
            # is symmetric, so this is independent of the spot swap below.
            v1_confs = g.confs(v1)
            total_confs = len(v1_confs)
            for x in g.confs(v2):
                if x not in v1_confs:
                    total_confs += 1

            # If one is a spot, use a special heuristic.
            # (described on section 6, page 311 of George & Appel)
            a, b = v1, v2
            if isinstance(a, Spot):
                a, b = b, a
            if isinstance(b, Spot):
                for T in g.confs(a):
                    if b in g.confs(T):
                        continue
                    if len(g.confs(T)) < nreg:
                        continue
                    break
                else:
                    # We can merge a into b.
                    g.merge(b, a)
                    return b, a

            # Otherwise, apply regular merging rules.
            elif total_confs < nreg:
                g.merge(a, b)
                return a, b
        return None

    def _freeze(self, g: "NodeGraph"):
        """Remove one preference edge.

        This function finds two nodes, preferably of low conflict degree,
        that are connected by a preference edge. Then, this preference edge
        is removed from the graph. Returns false iff nothing is done.
        """

        # Conflict degree of each node. The freeze step prefers to remove
        # preference edges between low-degree nodes. The original code obtained
        # a low-to-high *rank* via sorted(..., key=lambda nd: len(g.confs(nd)))
        # and ranked edges by that. The self-hosting transpiler drops the key=
        # argument, so under self-host that sort ordered nodes arbitrarily and
        # froze essentially random edges -- degrading coalescing and driving
        # needless spills. Rank by the conflict degree directly (smaller is
        # preferred), which captures the same intent without a keyed sort.
        deg = {}
        for nd in g.all_nodes():
            deg[nd] = len(g.confs(nd))

        # Find the preference edge whose endpoints have the lowest combined
        # degree, preferring to freeze edges between low-degree nodes. Iterate
        # preference edges directly rather than enumerating and sorting all
        # O(V^2) node pairs (which made this step cubic in the graph size and
        # dominated compile time on large functions).
        best = None
        best_key = None
        for na in g.all_nodes():
            p1 = deg[na]
            for nb in g.prefs(na):
                p2 = deg[nb]
                key = (p1 + p2, min(p1, p2), max(p1, p2))
                if best_key is None or key < best_key:
                    best_key = key
                    best = (na, nb)

        if best is not None:
            g.remove_pref(best[0], best[1])
            return True

        return False

    def _generate_spotmap(self, removed_nodes, merged_nodes, g: "NodeGraph",
                          spilled_nodes):
        """Pop values off stack to generate spot assignments.

        Nodes optimistically pushed onto the colouring stack that find no free
        register when popped are assigned a fresh stack slot (a real spill) and
        appended to spilled_nodes.
        """

        # Get a set of nodes which interfere with `node` or anything merged
        # into it. (Node variables are deliberately *not* named n/n1/n2: those
        # names are inferred as C int by the transpiler's name heuristic, which
        # would corrupt the graph-node objects passed through them.)
        def get_conflicts(node):
            # Collect conflicting nodes into a dict-used-as-a-set. A real set
            # with .add() can't be used here: get_conflicts is a nested
            # function, so the transpiler does not track `conflicts` as a
            # statically-typed set and lowers .add() to a vtable dispatch
            # (TYPE(conflicts)->add) -- but the underlying object has no add
            # slot, so the call jumps through a null pointer and segfaults.
            # Subscript assignment (conflicts[k] = 1) lowers to a plain dict
            # set and is always safe.
            conflicts = {}
            for k in g.confs(node):
                conflicts[k] = 1
            for sub in merged_nodes.get(node, []):
                for c in get_conflicts(sub):
                    conflicts[c] = 1
            return conflicts

        # Get a set of nodes which are merged into `node`
        def get_merged(node):
            # Dict-used-as-a-set, for the same reason as get_conflicts.
            merged = {}
            merged[node] = 1
            for sub in merged_nodes.get(node, []):
                for m in get_merged(sub):
                    merged[m] = 1
            return merged

        # Build up spotmap
        spotmap = {}
        i = 0
        while removed_nodes:
            i += 1

            # Allocate register to node `cur`
            cur = removed_nodes.pop()
            regs = self.alloc_registers[::-1]

            # If cur is a Spot (i.e. dummy node), immediately assign it a
            # register.
            if cur in regs:
                reg = cur
                for other in get_merged(cur):
                    spotmap[other] = reg
            else:
                # Don't chose any conflicting spots
                for other in get_conflicts(cur):
                    # If other is a physical spot
                    if other in regs:
                        regs.remove(other)
                    if other in spotmap and spotmap[other] in regs:
                        regs.remove(spotmap[other])

                if regs:
                    reg = regs.pop()
                    # Assign this register to every node merged into cur
                    for other in get_merged(cur):
                        spotmap[other] = reg
                else:
                    # Optimism failed: no register is free for this node, so it
                    # is a real spill. Give it (and everything merged into it) a
                    # stack slot.
                    slot = self._alloc_stack_slot(cur.ctype.size)
                    for other in get_merged(cur):
                        spotmap[other] = slot
                    spilled_nodes.append(cur)

        return spotmap

    def _generate_asm(self, commands, live_vars, spotmap, func_spots):
        """Generate assembly code."""

        # Map every size variant (rbx/ebx/bx/bl, r12/r12d/...) of each
        # callee-saved register back to its 64-bit RegSpot, so we can detect
        # which callee-saved registers the generated body actually touches.
        callee_saved = spots.callee_saved_registers
        name_to_reg = {}
        for reg in callee_saved:
            for variant in reg.name_variants():
                if variant:
                    name_to_reg[variant] = reg

        def used_callee_saved(cmds):
            used = []
            # Dict-used-as-a-set: this is a nested function, so the transpiler
            # does not track `seen` as a statically-typed set and would lower
            # seen.add(...) to a null vtable dispatch (segfault). Subscript
            # assignment is always safe.
            seen = {}
            for c in cmds:
                for field in (getattr(c, "dest", None), getattr(c, "source", None)):
                    reg = name_to_reg.get(field)
                    if reg is not None and reg not in seen:
                        seen[reg] = 1
                        used.append(reg)
            return used


        # This is kinda hacky...
        # Frame size is the deepest rbp-relative slot among this function's own
        # spots. Globals/literals are never rbp-relative (rbp_offset() == 0), so
        # scanning them -- the whole program's worth, once per function -- was a
        # quadratic no-op; iterate only this function's spots. (max(..., default)
        # is unavailable here, so fold manually to stay safe when empty.)
        max_offset = 0
        for spot in func_spots:
            off = spot.rbp_offset()
            if off > max_offset:
                max_offset = off

        # Decide framelessness BEFORE generating the body: a function with no
        # stack-resident locals and no non-tail call needs no rbp frame at all.
        # The Return command reads asm_code.frameless while the body is being
        # generated, so this decision must be fixed up front. It cannot depend
        # on register-scratch spilling discovered during generation -- so a
        # frameless function parks scratch in the red zone (see below), which
        # needs no frame, keeping the decision independent of scratch.
        base_offset = max_offset
        if base_offset % 16 != 0:
            base_offset += 16 - base_offset % 16
        frameless = False
        info = getattr(self.il_code, "stackless_info", {})
        fn_info = info.get(self._cur_func_name)
        # A function that allocates a callee-saved register must save/restore it
        # in a real prologue/epilogue, so it cannot be frameless.
        callee_saved_set = set(callee_saved)
        # Only this function's spots can be callee-saved registers; globals are
        # memory/literal spots. Scan func_spots, not the whole program's map.
        spotmap_callee_saved = False
        for s in func_spots:
            if s in callee_saved_set:
                spotmap_callee_saved = True
                break
        if fn_info is not None:
            frameless = (base_offset == 0
                         and fn_info.get("no_regular_call", False)
                         and not spotmap_callee_saved)
        # A function that receives arguments on the stack reads them relative
        # to rbp ([rbp+16], ...), so it must keep a real frame.
        if any(getattr(cmd, "stack_spot", None) is not None
               for cmd in commands):
            frameless = False
        self.asm_code.frameless = frameless
        # A frameless function has no place to save callee-saved registers, so
        # keep its scratch allocation on caller-saved registers only.
        self._scratch_caller_saved_only = frameless

        # Per-command register-scratch spill pool. When every allocatable
        # register holds a value live across a command, handing out a scratch
        # register requires parking one such value in memory for the duration
        # of that command. Slots are allocated lazily (only when a command
        # actually runs out of registers) and reused across commands, so a
        # function that never exhausts its registers reserves nothing. The home
        # for a scratch slot depends on the function: an -O4 near-scratch
        # function uses its static buffer; a frameless leaf uses the System V
        # red zone (128 bytes below rsp, safe for leaf functions, no frame
        # needed); any other function uses a real rbp-relative stack slot,
        # which grows the frame.
        scratch_pool = []

        def alloc_scratch():
            if self._near_active:
                return self._alloc_stack_slot(8)
            if frameless:
                return MemSpot(spots.RSP, -8 * (len(scratch_pool) + 1))
            return self._alloc_stack_slot(8)

        # Generate the body into a buffer first: scratch slots are discovered
        # during generation, but the prologue that reserves them must precede
        # the body. Redirect emitted lines into `body` for the duration.
        body = []
        saved_lines = self.asm_code.lines
        self.asm_code.lines = body
        try:
            for i, command in enumerate(commands):
                self.asm_code.add(
                    asm_cmds.Comment(type(command).__name__.upper()))

                # Registers parked in scratch slots for the duration of this
                # one command, restored right after. List of (reg, slot).
                spilled_this_cmd = []
                input_spots = set(spotmap[v] for v in command.inputs()
                                  if v in spotmap)

                def get_reg(pref=None, conf=None, _i=i, _command=command,
                            _spilled=spilled_this_cmd, _inputs=input_spots):
                    if not pref: pref = []
                    if not conf: conf = []
                    pool = self.all_registers
                    if getattr(self, "_scratch_caller_saved_only", False):
                        pool = [r for r in pool
                                if r not in set(spots.callee_saved_registers)]

                    # Bad if holding a variable live both entering and exiting
                    # this command.
                    bad_vars = set(live_vars[_i][0]) & set(live_vars[_i][1])
                    bad_spots = set(spotmap[var] for var in bad_vars)

                    # Free if it is where an output is stored.
                    for v in _command.outputs():
                        bad_spots.discard(spotmap[v])

                    # Bad if listed as a conflicting spot.
                    bad_spots |= set(conf)

                    for s in (pref + pool):
                        if isinstance(s, RegSpot) and s not in bad_spots:
                            return s

                    # No register is free: park a live-across register that
                    # this command neither reads (an input) nor is already
                    # using (conf or a prior spill) in a scratch slot, hand it
                    # out, and restore it after the command finishes.
                    already = set(r for r, _ in _spilled)
                    for s in pool:
                        if (isinstance(s, RegSpot) and s not in conf
                                and s not in _inputs and s not in already):
                            j = len(_spilled)
                            if j == len(scratch_pool):
                                scratch_pool.append(alloc_scratch())
                            slot = scratch_pool[j]
                            self.asm_code.add(asm_cmds.Mov(slot, s, 8))
                            _spilled.append((s, slot))
                            return s

                    raise NotImplementedError("spill required for get_reg")

                command.make_asm(spotmap, spotmap, get_reg, self.asm_code)

                # Restore registers parked for this command's scratch needs.
                for reg, slot in reversed(spilled_this_cmd):
                    self.asm_code.add(asm_cmds.Mov(reg, slot, 8))
        finally:
            self.asm_code.lines = saved_lines

        # Grow the frame to cover any rbp-relative stack scratch slots that
        # were allocated (red-zone and near-buffer slots return rbp_offset 0
        # and so do not affect the frame).
        for slot in scratch_pool:
            max_offset = max(max_offset, slot.rbp_offset())
        if max_offset % 16 != 0:
            max_offset += 16 - max_offset % 16

        # Callee-saved registers the body actually used must be preserved for
        # the caller: store each one's incoming value to a frame slot at entry
        # and restore it before every epilogue. (Frameless functions are kept
        # off callee-saved registers above, so saved_regs is empty there.)
        saved_slots = []
        for reg in used_callee_saved(body):
            slot = self._alloc_stack_slot(8)
            saved_slots.append((reg, slot))
            max_offset = max(max_offset, slot.rbp_offset())
        if max_offset % 16 != 0:
            max_offset += 16 - max_offset % 16

        if saved_slots:
            # Insert the restores just before each epilogue. The epilogue starts
            # with `mov rsp, rbp` (also used by tail-call teardown), so every
            # exit path -- ordinary return, tail jump, metamorphic jump -- gets
            # its registers restored.
            patched = []
            for cmd in body:
                if (getattr(cmd, "name", None) == "mov"
                        and getattr(cmd, "dest", None) == "rsp"
                        and getattr(cmd, "source", None) == "rbp"):
                    for reg, slot in reversed(saved_slots):
                        patched.append(asm_cmds.Mov(reg, slot, 8))
                patched.append(cmd)
            body[:] = patched

        if not frameless:
            # Back up rbp and move rsp
            self.asm_code.add(asm_cmds.Push(spots.RBP, None, 8))
            self.asm_code.add(asm_cmds.Mov(spots.RBP, spots.RSP, 8))

            offset_spot = LiteralSpot(str(max_offset))
            self.asm_code.add(asm_cmds.Sub(spots.RSP, offset_spot, 8))

        # Save callee-saved registers used by the body (after the frame exists).
        for reg, slot in saved_slots:
            self.asm_code.add(asm_cmds.Mov(slot, reg, 8))

        # SIMD bit-packing prologue hooks.
        if self.simd_pack.active:
            if getattr(self, "_cur_func_is_main", False):
                # Seed PACK_REG (and its mirror) from the flags' initial values.
                self.simd_pack.emit_startup_pack(self.asm_code)
            elif self.asm_code.simd_pack_hot:
                # Refresh PACK_REG from the mirror: one read covers all flags,
                # and keeps us correct despite xmm15 being caller-saved.
                self.simd_pack.emit_refresh(self.asm_code)

        # Emit the buffered body after the prologue.
        self.asm_code.lines.extend(body)

    # ======================= WebAssembly (wasm32) back end =======================
    # The first non-register target, and so the first that shares none of the
    # middle end above. Two things are structurally different from arm64 /
    # riscv64 / m68k, and both are worth stating plainly because they are why
    # none of the shared machinery is reused:
    #
    #   Registers. There are none. A wasm function has an unbounded supply of
    #   typed locals and the engine's own JIT does the real allocation, so
    #   _il_liveness / _il_intervals / _il_linear_scan are simply not called:
    #   every IL value gets its own local and that is the end of it. Nothing is
    #   ever spilled, because there is nothing to spill from.
    #
    #   Control flow. wasm has no `goto`. Branches may only exit an enclosing
    #   block or re-enter an enclosing loop, so the IL's arbitrary
    #   Label/Jump/JumpZero CFG cannot be emitted edge-for-edge. It is instead
    #   reconstructed as a dispatch loop: the function body is split into basic
    #   blocks, a `state` local holds the index of the block to run next, and a
    #   br_table at the top of a `loop` jumps to it. Every block ends by either
    #   assigning `state` and branching back to the dispatch, or returning, so
    #   control never falls from one block into the next.
    #
    #   That shape is O(1) to generate and correct for any CFG, at the cost of a
    #   dispatch per edge. A relooper that recovers the original loops and ifs
    #   is the obvious follow-up, and it is deliberately confined to
    #   _wasm_emit_body: nothing else here knows how control flow is spelled.
    #
    # Scope is the integer core, exactly as riscv64 started: locals, + - * / %,
    # the bitwise and shift operators, the six comparisons, if/while, direct
    # calls and recursion. Floats, pointers, arrays, structs, globals and string
    # literals all need linear memory and a shadow stack, which is the next
    # milestone; until then they *refuse* rather than miscompile.

    # Memory layout. Address 0 is left unmapped-in-spirit so that a null
    # dereference hits the low guard region rather than a real object; static
    # data starts above it, and the shadow stack sits above that and grows
    # down.
    WASM_NULL_GUARD = 1024
    WASM_STACK_SIZE = 1 << 18          # 256 KiB of shadow stack
    WASM_PAGE = 65536

    def _wasm_valtype(self, ctype):
        """The wasm value type for a C type, or a refusal.

        Pointers are the interesting case. wasm32 addresses are 32-bit, but
        this compiler's `sizeof(void *)` is 8 everywhere -- struct layouts,
        arrays of pointers and the rest all assume it. Rather than fork the
        type system for one target, a pointer is carried as an i64 holding a
        32-bit address and wrapped to i32 at the point of access. The high
        half is always zero, so nothing is lost.
        """
        import shivyc.wasm as wasm
        if ctype.is_floating():
            # `long double` is an alias for double in this compiler (size 8),
            # so there are only the two widths wasm has natively.
            return wasm.F32 if ctype.size == 4 else wasm.F64
        if ctype.is_array() or ctype.is_struct_union():
            # An aggregate is never a value in a local: it lives in the frame
            # or in static data, and is reached by address. Callers that need
            # a *value* type for one are asking the wrong question.
            raise NotImplementedError(
                "wasm back end: aggregate '%s' cannot be held in a register"
                % ("array" if ctype.is_array() else "struct"))
        if ctype.size > 4:
            return wasm.I64
        return wasm.I32

    def _wasm_addr_of_stack(self, body):
        """Wrap the i64 pointer on top of the stack down to an i32 address."""
        import shivyc.wasm as wasm
        body.op(wasm.OP_I32_WRAP_I64)

    def _wasm_load_op(self, ctype):
        """(opcode, align) for a load of `ctype` from linear memory."""
        import shivyc.wasm as wasm
        size = ctype.size
        if ctype.is_floating():
            return ((wasm.OP_F32_LOAD, 2) if size == 4
                    else (wasm.OP_F64_LOAD, 3))
        signed = self._wasm_signed(ctype)
        if size > 4:                    # i64 destination (long, pointer)
            return wasm.OP_I64_LOAD, 3
        if size == 4:
            return wasm.OP_I32_LOAD, 2
        if size == 2:
            return (wasm.OP_I32_LOAD16_S if signed
                    else wasm.OP_I32_LOAD16_U), 1
        return (wasm.OP_I32_LOAD8_S if signed else wasm.OP_I32_LOAD8_U), 0

    def _wasm_store_op(self, ctype):
        """(opcode, align) for a store of `ctype` to linear memory."""
        import shivyc.wasm as wasm
        size = ctype.size
        if ctype.is_floating():
            return ((wasm.OP_F32_STORE, 2) if size == 4
                    else (wasm.OP_F64_STORE, 3))
        if size > 4:
            return wasm.OP_I64_STORE, 3
        if size == 4:
            return wasm.OP_I32_STORE, 2
        if size == 2:
            return wasm.OP_I32_STORE16, 1
        return wasm.OP_I32_STORE8, 0

    def _wasm_signed(self, ctype):
        """Whether `ctype` is a signed integer. Types with no `signed`
        attribute (pointers, which are out of scope here anyway) read as
        unsigned, which is the safe direction for shifts and division."""
        return bool(getattr(ctype, "signed", False))

    def _wasm_func_sig(self, name):
        """(params, results) value types for the function called `name`.

        Taken from the symbol table's ctype rather than from the body's
        LoadArg commands: a parameter that is never read emits no LoadArg, and
        deriving the arity from the body would then silently produce a
        signature the call sites disagree with.

        Aggregates never appear as wasm value types. A struct *parameter* is
        passed as the i32 address of the caller's object, which the callee
        copies into its own frame on entry -- that copy is what makes the
        parameter by value. A struct *return* is written through a hidden
        leading i32 parameter into storage the caller allocated, and the
        function returns nothing. Both are forced by the same fact: a wasm
        value type is one of i32/i64/f32/f64, and a struct is none of them.
        """
        import shivyc.wasm as wasm
        var = self.symbol_table.linkages[self.symbol_table.EXTERNAL].get(name)
        if var is None:
            var = self.symbol_table.linkages[
                self.symbol_table.INTERNAL].get(name)
        if var is None or getattr(var, "ctype", None) is None:
            raise NotImplementedError(
                "wasm back end: no signature for function '%s'" % name)
        ctype = var.ctype

        front_sret, my_sret = self._wasm_sret_kind(ctype)
        params = []
        if front_sret:
            # The front end already turned this into a hidden pointer
            # parameter (SysV's memory-class return, for a struct over 16
            # bytes) and made the call void. Nothing to invent here -- just
            # declare the parameter it is already passing. It is an ordinary
            # C pointer, so it takes the usual i64 representation.
            params.append(wasm.I64)
        elif my_sret:
            # A struct of 16 bytes or less is returned *by value* by the front
            # end, because SysV hands it back in registers. wasm has no such
            # thing: a result is a single value type. So the same hidden
            # pointer convention is applied here instead, one size band lower.
            params.append(wasm.I32)

        # A variadic function takes no *declared* wasm parameters: every
        # argument, named and unnamed alike, arrives through the caller's
        # argument block. A wasm signature is fixed-arity, so the trailing
        # arguments have nowhere else to go, and giving the named ones a
        # separate path would mean two mechanisms where the IL already expects
        # one (LoadArg carries a base_index into the block for them).
        if not getattr(ctype, "variadic", False):
            for a in ctype.args:
                if a.is_struct_union() or a.is_array():
                    params.append(wasm.I32)
                else:
                    params.append(self._wasm_valtype(a))

        results = []
        if not ctype.ret.is_void() and not front_sret and not my_sret:
            results.append(self._wasm_valtype(ctype.ret))
        return params, results

    def _wasm_sret_kind(self, ctype):
        """(front_sret, my_sret) for a function ctype.

        An aggregate return reaches this back end in one of two shapes, and
        which one depends on its size, because the front end implements the
        SysV rule directly:

          size > 16  -- the front end has *already* rewritten the call: it
                        allocates the result, passes its address as a hidden
                        first argument, and marks the call void. There is
                        nothing to do but declare that parameter.
          size <= 16 -- SysV returns it in registers, so the front end leaves
                        it as a by-value result. wasm has no multi-register
                        return, so the same hidden-pointer trick is applied
                        here, at a size the front end did not cover.

        Getting this wrong is not subtle: treating the first case like the
        second passes *two* hidden pointers and every later argument lands one
        position off.
        """
        ret = ctype.ret
        if not (ret.is_struct_union() or ret.is_array()):
            return False, False
        if getattr(ctype, "variadic", False):
            return False, ret.is_struct_union() or ret.is_array()
        if ret.is_struct_union() and ret.size > 16:
            return True, False
        return False, True


    def _wasm_sig_info(self, name):
        """(agg_ret, sret_offset) for a function: whether it returns an
        aggregate, and how many hidden parameters precede the declared ones."""
        var = self.symbol_table.linkages[self.symbol_table.EXTERNAL].get(name)
        if var is None:
            var = self.symbol_table.linkages[
                self.symbol_table.INTERNAL].get(name)
        if var is None or getattr(var, "ctype", None) is None:
            return False, 0
        front_sret, my_sret = self._wasm_sret_kind(var.ctype)
        # `my_sret` is the only one the back end has to *do* anything about at
        # a call site; the front end's own sret pointer is already an ordinary
        # argument by the time it gets here.
        return my_sret, (1 if (front_sret or my_sret) else 0)

    # The WASI preview-1 functions this back end knows how to import. A C
    # program does not call these directly -- it calls write(), and the small
    # C runtime in shivyc/include/wasi.h calls fd_write -- but the names have
    # to be recognised here so they are imported from the right module.
    #
    # Anything not on this list is imported from "env" instead, which is the
    # convention a plain JS host expects: the program declares `int ext(int);`
    # without defining it, and the host supplies `env.ext`.
    WASI_MODULE = "wasi_snapshot_preview1"
    WASI_FUNCS = ("fd_write", "fd_read", "fd_close", "fd_seek",
                  "proc_exit", "environ_get", "environ_sizes_get",
                  "args_get", "args_sizes_get", "random_get",
                  "clock_time_get", "fd_fdstat_get", "path_open")

    def _wasm_abi_valtype(self, ctype):
        """Value type for a parameter or result **at an import boundary**.

        Inside the module a pointer is an i64 holding a 32-bit address, which
        keeps sizeof(void *) == 8 consistent with the rest of the compiler.
        That representation must not escape to the host: a WASI function's
        signature is 32-bit throughout, and declaring an import with an i64
        pointer parameter both fails to match a real WASI host and hands a
        JavaScript one a BigInt. So a pointer narrows to i32 here, and the
        call site wraps the value to match.
        """
        import shivyc.wasm as wasm
        if ctype.is_pointer():
            return wasm.I32
        return self._wasm_valtype(ctype)

    def _wasm_import_sig(self, name):
        """(params, results) for an imported function, in the wasm32 ABI."""
        var = self.symbol_table.linkages[self.symbol_table.EXTERNAL].get(name)
        if var is None:
            var = self.symbol_table.linkages[
                self.symbol_table.INTERNAL].get(name)
        if var is None or getattr(var, "ctype", None) is None:
            raise NotImplementedError(
                "wasm back end: no signature for imported function '%s'"
                % name)
        ctype = var.ctype
        params = []
        for a in ctype.args:
            params.append(self._wasm_abi_valtype(a))
        results = []
        if not ctype.ret.is_void():
            results.append(self._wasm_abi_valtype(ctype.ret))
        return params, results

    def _wasm_import_origin(self, name):
        """(module, field) an undefined function is imported from."""
        base = name
        # The C runtime spells these `__wasi_fd_write` and friends so the
        # names cannot collide with a user function called `fd_write`.
        if base.startswith("__wasi_"):
            base = base[len("__wasi_"):]
        if base in self.WASI_FUNCS:
            return self.WASI_MODULE, base
        return "env", name

    def _wasm_align(self, addr, alignment):
        """Round `addr` up to a multiple of `alignment`."""
        rem = addr % alignment
        if rem:
            return addr + (alignment - rem)
        return addr

    def _wasm_alloc_static(self, size, alignment):
        """Reserve `size` bytes of static data and return the address."""
        if alignment > 8:
            alignment = 8
        if alignment < 1:
            alignment = 1
        addr = self._wasm_align(self._wasm_next_addr, alignment)
        self._wasm_next_addr = addr + (size if size > 0 else 1)
        return addr

    def _wasm_int_bytes(self, val, size):
        """`size` bytes of `val`, little-endian -- which is what wasm linear
        memory is, on every engine, by specification."""
        v = int(val) & ((1 << (size * 8)) - 1)
        out = []
        for _ in range(size):
            out.append(v & 0xFF)
            v = v >> 8
        return out

    def _wasm_float_bytes(self, val, size):
        """`size` bytes of the IEEE-754 image of `val`, little-endian."""
        import struct
        raw = struct.pack("<f" if size == 4 else "<d", float(val))
        out = []
        for byte in raw:
            out.append(byte)
        return out

    def _wasm_init_bytes(self, val, size):
        """Byte image of a scalar static initializer, integer or floating."""
        if isinstance(val, float):
            return self._wasm_float_bytes(val, size)
        return self._wasm_int_bytes(val, size)

    def _wasm_layout_statics(self):
        """Assign addresses to every string literal and static object, then
        build the byte images of their initializers.

        Two passes, and the split is load-bearing: a static initializer may
        name another static (`static char *p = "hi";` refers to the literal's
        address), and that address has to exist before the image referring to
        it can be built. Placing everything first makes the order in which
        objects appear irrelevant.
        """
        # Symbol name -> address, so a ("sym", name, addend) initializer entry
        # can be resolved to a number. Wasm has no relocations at this level:
        # every address is known at compile time and becomes a constant.
        self._wasm_addr_by_name = {}

        # --- pass 1: assign addresses
        placed = []
        snum = 0
        for v in self.il_code.string_literals:
            nm = self.il_code.string_literal_names.get(v)
            if nm is None:
                nm = "__wasmstr%d" % snum
                self.il_code.string_literal_names[v] = nm
            snum += 1
            chars = self.il_code.string_literals[v]
            elem = v.ctype.el.size if v.ctype.is_array() else 1
            data = []
            for ch in chars:
                data = data + self._wasm_int_bytes(ch, elem)
            addr = self._wasm_alloc_static(len(data), elem if elem <= 8 else 8)
            self._wasm_addr[v] = addr
            self._wasm_addr_by_name[nm] = addr
            self._wasm_data.append((addr, data))

        # Every static in the symbol table, not merely those a command
        # mentions: an object can be referenced only from another object's
        # initializer (`int g; int *gp = &g;`), in which case it appears in no
        # IL command at all and would otherwise never be given an address.
        # The symbol table's order is declaration order, so the layout is
        # stable between runs.
        STATIC = self.symbol_table.STATIC
        seen = {}
        for v in self.symbol_table.storage:
            if v in seen:
                continue
            if self.symbol_table.storage.get(v) != STATIC:
                continue
            if getattr(v, "ctype", None) is None or v.ctype.is_function():
                continue
            if getattr(v, "literal", None) is not None:
                continue
            seen[v] = 1
            size = v.ctype.size
            align = size if size in (1, 2, 4, 8) else 8
            if v.ctype.is_array():
                esz = v.ctype.el.size
                align = esz if esz in (1, 2, 4, 8) else 8
            addr = self._wasm_alloc_static(size, align)
            self._wasm_addr[v] = addr
            nm = self.symbol_table.asm_name(v)
            if nm is not None:
                self._wasm_addr_by_name[nm] = addr
            placed.append(v)

        # --- pass 2: build initializer images
        for v in placed:
            self._wasm_place_static(v)

    def _wasm_sym_addr(self, name, addend):
        """Resolve a symbolic address constant to a number."""
        addr = self._wasm_addr_by_name.get(name)
        if addr is None:
            raise NotImplementedError(
                "wasm back end: static initializer refers to '%s', which has "
                "no address in this module" % name)
        return addr + (addend if addend else 0)

    def _wasm_place_static(self, v):
        """Build one static object's initializer image at its address."""
        addr = self._wasm_addr[v]
        size = v.ctype.size

        if v in self.il_code.static_block_inits:
            entries, total = self.il_code.static_block_inits[v]
            data = [0] * total
            for off, esize, val in entries:
                if isinstance(val, tuple) and val and val[0] == "sym":
                    # An address constant. Every address is a compile-time
                    # constant here -- there is no linker and no relocation --
                    # so this resolves to a plain number. The stored width is
                    # whatever the field is; a pointer field is 8 bytes and the
                    # high half is simply zero.
                    _, sym, addend = val
                    img = self._wasm_int_bytes(
                        self._wasm_sym_addr(sym, addend), esize)
                else:
                    img = self._wasm_init_bytes(val, esize)
                for i in range(esize):
                    if off + i < total:
                        data[off + i] = img[i]
            self._wasm_data.append((addr, data))
            return

        init = self.il_code.static_inits.get(v, 0)
        if isinstance(init, tuple) and init and init[0] == "sym":
            _, sym, addend = init
            self._wasm_data.append(
                (addr, self._wasm_int_bytes(
                    self._wasm_sym_addr(sym, addend), size)))
            return
        if init:
            # A float initializer of 0.0 is skipped by this test, which is
            # correct: its IEEE image is all-zero bytes and memory starts
            # zeroed. (-0.0 is not zero-valued in Python's `if`, so it still
            # gets a segment, as it must.)
            self._wasm_data.append((addr, self._wasm_init_bytes(init, size)))
        # An uninitialized static is zero, and linear memory starts zeroed, so
        # no data segment is needed for it at all.

    def _make_asm_wasm(self):
        """WebAssembly lowering. Runs only under `--target wasm`.

        Unlike every other back end this writes no assembler text: the module
        is built as bytes and handed to the driver on `asm_code.wasm_bytes`,
        which skips `as` and `ld` entirely (see targets.WasmTarget.is_binary).
        """
        import shivyc.wasm as wasm
        import shivyc.il_cmds.control as control

        mod = wasm.WasmModule()

        # Static data first: every global and string literal is given a fixed
        # address now, so a function body can refer to one as a constant no
        # matter which order the functions are emitted in.
        self._wasm_addr = {}            # ILValue -> absolute address
        self._wasm_data = []            # (address, bytes) to emit
        self._wasm_next_addr = self.WASM_NULL_GUARD
        self._wasm_mod = mod
        self._wasm_layout_statics()

        # The shadow stack sits above the static data. Locals whose address is
        # taken, and aggregates, live here rather than in wasm locals -- a
        # wasm local has no address, so `&x` is not expressible for one.
        stack_low = self._wasm_align(self._wasm_next_addr, 16)
        stack_top = stack_low + self.WASM_STACK_SIZE
        pages = (stack_top + self.WASM_PAGE - 1) // self.WASM_PAGE
        mod.set_memory(pages)
        self._wasm_sp = mod.add_global_i32(stack_top)
        # The base of a variadic call's argument block, handed from caller to
        # callee. This is the same trick the register back ends use -- riscv64
        # passes it in t0, arm64 in a scratch register -- except that wasm has
        # no registers, so a global stands in. It is safe for the same reason:
        # the callee's prologue copies it into a local (VaSaveBase) before it
        # can make any call of its own that would overwrite it.
        self._wasm_va_base = mod.add_global_i32(0)
        for addr, data in self._wasm_data:
            mod.add_data(addr, data)

        # Imports first: they occupy the low function indices, so every one
        # must be declared before the first defined function. That means
        # scanning for calls to undefined functions up front rather than
        # discovering them while emitting bodies.
        self._wasm_imports = {}
        for func in self.il_code.commands:
            for c in self.il_code.commands[func]:
                if not isinstance(c, control.Call):
                    continue
                nm = c.direct_name
                if nm is None or nm in self.il_code.commands:
                    continue            # indirect, or defined here
                if nm in self._wasm_imports:
                    continue
                params, results = self._wasm_import_sig(nm)
                module, field = self._wasm_import_origin(nm)
                mod.declare_import(nm, module, field, params, results)
                self._wasm_imports[nm] = 1

        # A WASI *command* module is entered at `_start`, not at `main`: the
        # host calls it with no arguments and learns the exit status from
        # proc_exit. `_start` is synthesised below, so proc_exit has to be
        # imported here, while imports can still be declared.
        self._wasm_has_main = "main" in self.il_code.commands
        if self._wasm_has_main and "__wasi_proc_exit" not in self._wasm_imports:
            mod.declare_import("__wasi_proc_exit", self.WASI_MODULE,
                               "proc_exit", [wasm.I32], [])
            self._wasm_imports["__wasi_proc_exit"] = 1

        # Reserve an index for every defined function *before* emitting any
        # body, so a call to a function defined later in the file -- or a
        # mutually recursive pair -- resolves without a second pass.
        for func in self.il_code.commands:
            params, results = self._wasm_func_sig(func)
            mod.declare_func(func, params, results)

        for func in self.il_code.commands:
            body = self._wasm_function(func, self.il_code.commands[func], mod)
            mod.set_body(func, body)

        # Export every function with external linkage that we actually define.
        # The difftest harness calls `main`; exporting the rest costs a few
        # bytes and makes a module far easier to poke at from the host.
        EXTERNAL = self.symbol_table.EXTERNAL
        DEFINED = self.symbol_table.DEFINED
        exported = {}
        for v in self.symbol_table.linkages[EXTERNAL].values():
            if self.symbol_table.def_state.get(v) != DEFINED:
                continue
            nm = self.symbol_table.names.get(v)
            if nm is None or nm in exported:
                continue
            if mod.func_index(nm) < 0:
                continue
            exported[nm] = 1
            mod.export_func(nm)

        # The host reads and writes the module's memory directly -- an iovec
        # handed to fd_write is a pointer into it -- so it must be exported.
        mod.export_memory = True

        if self._wasm_has_main:
            self._wasm_emit_start(mod)

        self.asm_code.wasm_bytes = wasm.module_bytes(mod)

    def _wasm_emit_start(self, mod):
        """Synthesise the WASI entry point.

        `_start` takes nothing and returns nothing; it calls main and hands the
        result to proc_exit. A process exit status is 8 bits, so the value is
        masked here rather than left for the host to interpret -- otherwise a
        main returning -1 would exit with 4294967295.

        main may be declared either `(void)` or `(int, char **)`; the second
        form gets a zero argc and a null argv, since nothing supplies real
        arguments yet.
        """
        import shivyc.wasm as wasm
        mod.declare_func("_start", [], [])
        body = wasm.FuncBody()

        params, results = self._wasm_func_sig("main")
        for p in params:
            # argc = 0, argv = NULL.
            body.const(p, 0)
        body.call(mod.func_index("main"))
        if not results:
            # A void main still has to produce a status for proc_exit.
            body.const_i32(0)
        else:
            if results[0] == wasm.I64:
                body.op(wasm.OP_I32_WRAP_I64)
            body.const_i32(0xFF)
            body.op(wasm.I32_BIN["and"])
        body.call(mod.func_index("__wasi_proc_exit"))
        mod.set_body("_start", body)
        mod.export_func("_start")

    # ---------------------------------------------------------- basic blocks

    def _wasm_is_terminator(self, cmd):
        """Whether `cmd` ends a basic block."""
        import shivyc.il_cmds.control as control
        return isinstance(cmd, control.Jump) \
            or isinstance(cmd, control.JumpZero) \
            or isinstance(cmd, control.JumpNotZero) \
            or isinstance(cmd, control.Return)

    def _wasm_split_blocks(self, cmds):
        """Split a command list into basic blocks.

        Returns (blocks, label_block) where `blocks` is a list of command
        lists and `label_block` maps an IL label name to the index of the
        block it starts. A block begins at a Label or immediately after a
        terminator, which is the standard partition; the only wrinkle is that
        consecutive Labels must each get their own (possibly empty) block, so
        that a jump to either one lands in the right place.
        """
        import shivyc.il_cmds.control as control

        blocks = [[]]
        label_block = {}
        for cmd in cmds:
            if isinstance(cmd, control.Label):
                # Start a fresh block unless the current one is still empty,
                # in which case reuse it -- otherwise every label would leave
                # an empty block behind and inflate the dispatch table.
                if blocks[-1]:
                    blocks.append([])
                label_block[cmd.label] = len(blocks) - 1
                continue
            blocks[-1].append(cmd)
            if self._wasm_is_terminator(cmd):
                blocks.append([])

        # A trailing empty block is harmless but pointless; drop it unless it
        # is the only block (an empty function body still needs one).
        if len(blocks) > 1 and not blocks[-1]:
            # Only safe to drop if no label points at it. If one does, keep it:
            # it is the natural landing pad for a jump to the end of a function.
            last = len(blocks) - 1
            pointed_at = False
            for lbl in label_block:
                if label_block[lbl] == last:
                    pointed_at = True
            if not pointed_at:
                blocks.pop()

        return blocks, label_block

    # ------------------------------------------------------- value plumbing

    def _wasm_local(self, value, body, locals_of, nparams):
        """The local index holding `value`, allocating one on first sight."""
        idx = locals_of.get(value, -1)
        if idx >= 0:
            return idx
        vt = self._wasm_valtype(value.ctype)
        rel = body.add_local(vt)
        idx = nparams + rel
        locals_of[value] = idx
        return idx

    def _wasm_scratch(self, body, valtype, nparams):
        """A reusable scratch local of the given type.

        Needed because a wasm store takes the address *below* the value on the
        stack, while the rest of the lowering naturally produces the value
        first. Parking the value in a scratch lets the address be pushed
        underneath it without re-ordering every caller.
        """
        # Reset per function by _wasm_function: these are *local* indices, and
        # a local index is meaningful only inside the function that declared
        # it. Caching them across functions silently aims a store at whatever
        # local happens to share the number in the next one.
        key = "i64" if valtype == 0x7E else "i32"
        idx = self._wasm_scratches.get(key, -1)
        if idx < 0:
            idx = nparams + body.add_local(valtype)
            self._wasm_scratches[key] = idx
        return idx

    def _wasm_push_addr(self, value, body):
        """Push the i32 address of an object that lives in memory: a static at
        its fixed address, or a frame slot at $fp + offset."""
        import shivyc.wasm as wasm
        addr = self._wasm_addr.get(value)
        if addr is not None:
            body.const_i32(addr)
            return
        off = self._wasm_slot.get(value)
        if off is None:
            raise NotImplementedError(
                "wasm back end: no address for a value that needs one")
        body.local_get(self._wasm_fp)
        if off:
            body.const_i32(off)
            body.op(wasm.I32_BIN["add"])

    def _wasm_in_memory(self, value):
        """Whether `value` lives in linear memory rather than a wasm local."""
        return value in self._wasm_addr or value in self._wasm_slot

    def _wasm_push(self, value, body, locals_of, nparams):
        """Push `value` onto the operand stack: an immediate if it is a
        literal, a load if it lives in memory, otherwise a read of its local."""
        lit = getattr(value, "literal", None)
        if lit is not None:
            vt = self._wasm_valtype(value.ctype)
            if value.ctype.is_floating():
                # A float literal's value lives in il_code.float_literals; the
                # FloatLiteral attached to the value is a marker, and reading
                # it as an int would silently truncate.
                body.const(vt, self.il_code.float_literals[value])
            else:
                body.const(vt, int(lit.val))
            return
        if self._wasm_in_memory(value):
            if value.ctype.is_array() or value.ctype.is_struct_union():
                # An aggregate is not a value. Copying one is handled by the
                # Set case above, which works on addresses; reaching here means
                # something wants an aggregate *in a register*, which is a
                # by-value parameter or return this back end does not lower.
                raise NotImplementedError(
                    "wasm back end: passing or returning an aggregate by "
                    "value is not implemented yet")
            self._wasm_push_addr(value, body)
            op, align = self._wasm_load_op(value.ctype)
            body.mem(op, align, 0)
            return
        body.local_get(self._wasm_local(value, body, locals_of, nparams))

    def _wasm_pop_into(self, value, body, locals_of, nparams):
        """Pop the operand stack into `value`'s home."""
        if self._wasm_in_memory(value):
            vt = self._wasm_valtype(value.ctype)
            sc = self._wasm_scratch(body, vt, nparams)
            body.local_set(sc)
            self._wasm_push_addr(value, body)
            body.local_get(sc)
            op, align = self._wasm_store_op(value.ctype)
            body.mem(op, align, 0)
            return
        body.local_set(self._wasm_local(value, body, locals_of, nparams))

    def _wasm_convert(self, body, to_ctype, from_ctype):
        """Convert the value on top of the stack from one C type to another.

        Two separate jobs hide here. Changing wasm value type (i32 <-> i64) is
        a wrap or an extend. Narrowing *within* i32 -- `(char)x`, `(short)x` --
        changes no wasm type at all but must still discard the high bits, or
        the next comparison sees a value C says cannot exist.
        """
        import shivyc.wasm as wasm
        to_vt = self._wasm_valtype(to_ctype)
        from_vt = self._wasm_valtype(from_ctype)

        to_f = to_ctype.is_floating()
        from_f = from_ctype.is_floating()

        if to_f or from_f:
            if to_f and from_f:
                # float <-> double. Same type is a no-op; otherwise demote or
                # promote. Demotion rounds, which is what C says a narrowing
                # float conversion does.
                if to_vt != from_vt:
                    body.op(wasm.OP_F32_DEMOTE_F64 if to_vt == wasm.F32
                            else wasm.OP_F64_PROMOTE_F32)
                return
            if to_f:
                # integer -> float. Named by the *source* signedness, and
                # exact for every value either integer width can hold.
                src = "i64" if from_vt == wasm.I64 else "i32"
                sg = self._wasm_signed(from_ctype)
                table = (wasm.F32_CONVERT if to_vt == wasm.F32
                         else wasm.F64_CONVERT)
                body.op(table[(src, sg)])
                return
            # float -> integer. C truncates toward zero, which is what the
            # trunc family does. The saturating form is used so that a value
            # too large for the destination clamps instead of trapping; C
            # leaves that case undefined, and a trap would take down the
            # module over an expression the program may not even use.
            src = "f32" if from_vt == wasm.F32 else "f64"
            dst = "i64" if to_vt == wasm.I64 else "i32"
            sg = self._wasm_signed(to_ctype)
            body.trunc_sat(dst, src, sg)
            # A narrow destination (char, short) still needs its high bits
            # dropped after the conversion.
            width = 4 if to_vt == wasm.I32 else 8
            if to_ctype.size < width:
                self._wasm_truncate(body, to_ctype, to_vt)
            return

        if to_vt == wasm.I32 and from_vt == wasm.I64:
            body.op(wasm.OP_I32_WRAP_I64)
            from_vt = wasm.I32
        elif to_vt == wasm.I64 and from_vt == wasm.I32:
            # The *source* type's signedness decides how to widen: widening a
            # negative int to long must sign-extend, an unsigned int must not.
            if self._wasm_signed(from_ctype):
                body.op(wasm.OP_I64_EXTEND_I32_S)
            else:
                body.op(wasm.OP_I64_EXTEND_I32_U)
            from_vt = wasm.I64

        # Narrowing to a sub-word width. The rule is just: if the C type is
        # narrower than the wasm value type now holding it, the high bits are
        # not C's to keep. Truncating a value that is already in range is a
        # semantic no-op, so this needs no cleverness about whether the
        # previous step happened to leave it canonical -- and the engine folds
        # the redundant cases anyway.
        width = 4 if to_vt == wasm.I32 else 8
        if to_ctype.size < width:
            self._wasm_truncate(body, to_ctype, to_vt)

    def _wasm_truncate(self, body, ctype, vt):
        """Reduce the top of stack to `ctype`'s width, sign- or zero-extending
        back out as C requires."""
        import shivyc.wasm as wasm
        if ctype.is_floating():
            # A float value is always exactly its type's width; there are no
            # high bits to discard.
            return
        size = ctype.size
        if size >= 8:
            return
        if vt == wasm.I32 and size >= 4:
            return
        signed = self._wasm_signed(ctype)
        if signed:
            if size == 1:
                body.op(wasm.OP_I32_EXTEND8_S if vt == wasm.I32
                        else wasm.OP_I64_EXTEND8_S)
            elif size == 2:
                body.op(wasm.OP_I32_EXTEND16_S if vt == wasm.I32
                        else wasm.OP_I64_EXTEND16_S)
            elif size == 4 and vt == wasm.I64:
                body.op(wasm.OP_I64_EXTEND32_S)
            return
        mask = (1 << (size * 8)) - 1
        body.const(vt, mask)
        body.op(wasm.I32_BIN["and"] if vt == wasm.I32 else wasm.I64_BIN["and"])

    # --------------------------------------------------------- function body

    def _wasm_function(self, func, cmds, mod):
        """Emit the body of one function and return its FuncBody."""
        import shivyc.wasm as wasm
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.control as control

        params, results = self._wasm_func_sig(func)
        nparams = len(params)
        self._wasm_agg_ret, self._wasm_sret = self._wasm_sig_info(func)

        # Map each LoadArg to the wasm parameter it reads. A map is needed
        # rather than plain arithmetic on arg_num because the front end's sret
        # pointer and the first real parameter *both* carry arg_num 0 -- the
        # register back ends tell them apart by the assigned register, which
        # wasm does not have. The sret LoadArg is always emitted first, so
        # position disambiguates them.
        self._wasm_argmap = {}
        _first = True
        _fs = (self._wasm_sret and not self._wasm_agg_ret)
        _pos = 0
        for c in cmds:
            is_la = isinstance(c, value_cmds.LoadArg)
            is_lsa = isinstance(c, value_cmds.LoadStructArg)
            if not is_la and not is_lsa:
                continue
            if _fs and _first and is_la:
                # Only the *front end's* sret emits a LoadArg for the hidden
                # pointer. When this back end synthesises the pointer instead,
                # no LoadArg reads it, so the first one here is a real
                # parameter and must not be mapped to slot 0.
                self._wasm_argmap[id(c)] = 0
                _first = False
                continue
            _first = False
            if is_la:
                # arg_num is authoritative for an ordinary parameter.
                _pos = c.arg_num
            # LoadStructArg carries no arg_num at all -- it identifies its
            # parameter by SysV register or stack slot, neither of which
            # exists here -- so its position is tracked by counting. The front
            # end emits one load per declared parameter, in order, so the
            # count and the declaration order agree.
            self._wasm_argmap[id(c)] = _pos + self._wasm_sret
            _pos += 1

        body = wasm.FuncBody()
        locals_of = {}
        self._wasm_scratches = {}

        # Which values must live in the frame rather than in a wasm local?
        # Two kinds: anything whose address is taken (a wasm local has no
        # address, so `&x` cannot be formed for one), and any aggregate (too
        # big for a value type at all). Everything else stays a local.
        import shivyc.il_cmds.value as vcmds
        STATIC = self.symbol_table.STATIC
        forced = {}
        for c in cmds:
            if isinstance(c, vcmds.AddrOf) and not c.var.ctype.is_function():
                if self.symbol_table.storage.get(c.var) != STATIC:
                    forced[c.var] = 1
        for c in cmds:
            for v in c.inputs() + c.outputs():
                if v is None or getattr(v, "literal", None) is not None:
                    continue
                if self.symbol_table.storage.get(v) == STATIC:
                    continue
                if v.ctype.is_array() or v.ctype.is_struct_union():
                    # Aggregates always live in the frame; they have no value
                    # type, so _wasm_valtype must not be asked about them.
                    forced[v] = 1
                else:
                    self._wasm_valtype(v.ctype)   # refuse floats early

        # A variadic call needs a contiguous block of 8-byte slots in *this*
        # function's frame to stage its arguments in. Reserve enough for the
        # widest such call; they cannot overlap in time, so one block serves
        # all of them.
        va_out = 0
        for c in cmds:
            if isinstance(c, control.Call) and getattr(c, "variadic", False):
                need = 8 * len(c.args)
                if need > va_out:
                    va_out = need

        # Lay the forced values out in the frame.
        self._wasm_slot = {}
        frame = 0
        for v in forced:
            size = v.ctype.size
            align = 8
            if size in (1, 2, 4):
                align = size
            frame = self._wasm_align(frame, align)
            self._wasm_slot[v] = frame
            frame += size if size > 0 else 1
        # The outgoing block sits above the forced values, at a known offset.
        self._wasm_va_out = -1
        if va_out:
            frame = self._wasm_align(frame, 8)
            self._wasm_va_out = frame
            frame += va_out
        frame = self._wasm_align(frame, 16)

        # `state` holds the index of the basic block to run next. It is the
        # only piece of state the dispatch needs, and it is an ordinary local
        # like any other.
        state_local = nparams + body.add_local(wasm.I32)

        # The frame pointer, when this function needs a frame at all. A leaf
        # that takes no addresses and holds no aggregates needs none, and pays
        # nothing: no prologue, no epilogue, no global traffic.
        self._wasm_frame = frame
        self._wasm_fp = -1
        if frame:
            self._wasm_fp = nparams + body.add_local(wasm.I32)

        blocks, label_block = self._wasm_split_blocks(cmds)

        # A LoadArg must either name a real wasm parameter or carry a base
        # into the caller's argument block. Anything else would read a local
        # that does not exist.
        for c in cmds:
            if isinstance(c, value_cmds.LoadArg):
                if c.base is None and c.arg_num >= nparams:
                    raise NotImplementedError(
                        "wasm back end: parameter %d has no home (function "
                        "declares %d)" % (c.arg_num, nparams))

        # Prologue: claim the frame by lowering the shadow stack pointer, and
        # keep its base in $fp. Emitted before the dispatch loop, so it runs
        # exactly once however control flows afterwards.
        if frame:
            body.global_get(self._wasm_sp)
            body.const_i32(frame)
            body.op(wasm.I32_BIN["sub"])
            body.op_u(wasm.OP_LOCAL_TEE, self._wasm_fp)
            body.global_set(self._wasm_sp)

        self._wasm_emit_body(body, blocks, label_block, state_local,
                             locals_of, nparams, results, mod, func)
        return body

    def _wasm_epilogue(self, body):
        """Give the frame back. Emitted at every return, since a wasm function
        can leave from any point and there is no single exit to hang it on."""
        import shivyc.wasm as wasm
        if self._wasm_frame:
            body.local_get(self._wasm_fp)
            body.const_i32(self._wasm_frame)
            body.op(wasm.I32_BIN["add"])
            body.global_set(self._wasm_sp)

    def _wasm_emit_body(self, body, blocks, label_block, state_local,
                        locals_of, nparams, results, mod, func):
        """Emit the dispatch loop and every basic block inside it.

        The shape is:

            loop $L                     ;; re-entered once per CFG edge
              block $b_{n-1}
                ...
                  block $b_0
                    local.get $state
                    br_table 0 1 .. n-1 (default n-1)
                  end                   ;; branching to $b_0 lands here
                  <block 0>
                end                     ;; ... $b_1 lands here
                <block 1>
              ...
            end
            unreachable

        Branching to depth i exits blocks b_0..b_i and resumes just past b_i's
        `end`, which is exactly where block i's code sits. Every block ends in
        a `br` back to $L or a `return`, so the fallthrough from one block's
        code into the next block's `end` never happens.
        """
        import shivyc.wasm as wasm

        n = len(blocks)

        body.loop()
        for _ in range(n):
            body.block()

        body.local_get(state_local)
        depths = []
        for i in range(n):
            depths.append(i)
        # Any index outside the table takes the default. `state` is only ever
        # written from this generator, so it cannot actually go out of range;
        # the default is required by the encoding regardless.
        body.br_table(depths, n - 1)

        for i in range(n):
            body.end()
            # Depth from here back to the enclosing loop: blocks b_0..b_i have
            # been closed, so b_{i+1}..b_{n-1} plus the loop still enclose us.
            loop_depth = n - 1 - i
            self._wasm_emit_block(body, blocks[i], i, n, loop_depth,
                                  label_block, state_local, locals_of,
                                  nparams, results, mod, func)

        body.end()                       # close the loop
        # The loop is only ever left by a `return` from inside a block, so
        # falling out of it is unreachable. Saying so keeps the body
        # well-typed without inventing a return value.
        body.op(wasm.OP_UNREACHABLE)

    def _wasm_goto(self, body, target_block, loop_depth, state_local):
        """Emit an unconditional transfer to `target_block`: set the dispatch
        state and branch back to the loop header."""
        body.const_i32(target_block)
        body.local_set(state_local)
        body.br(loop_depth)

    def _wasm_emit_block(self, body, cmds, blk_idx, nblocks, loop_depth,
                         label_block, state_local, locals_of, nparams,
                         results, mod, func):
        """Emit one basic block, terminator included."""
        import shivyc.wasm as wasm
        import shivyc.il_cmds.control as control

        for cmd in cmds:
            if self._wasm_is_terminator(cmd):
                break
            self._lower_wasm(cmd, body, locals_of, nparams, mod)

        term = None
        if cmds and self._wasm_is_terminator(cmds[-1]):
            term = cmds[-1]

        nxt = blk_idx + 1

        if term is None:
            # Fell off the end of the block. If another block follows, this is
            # a plain fallthrough edge; if not, control ran off the end of the
            # function.
            if nxt < nblocks:
                self._wasm_goto(body, nxt, loop_depth, state_local)
            elif not results:
                self._wasm_epilogue(body)
                body.op(wasm.OP_RETURN)
            else:
                # Running off the end of a value-returning function is
                # undefined in C. `unreachable` traps instead of returning
                # garbage, which is the more useful failure.
                body.op(wasm.OP_UNREACHABLE)
            return

        if isinstance(term, control.Return):
            if getattr(self, "_wasm_agg_ret", False) and term.arg is not None:
                # Write the result into the caller's destination, whose
                # address arrived as the hidden leading parameter, then return
                # nothing.
                body.local_get(0)
                self._wasm_push_addr(term.arg, body)
                body.const_i32(term.arg.ctype.size)
                body.memory_copy()
                self._wasm_epilogue(body)
                body.op(wasm.OP_RETURN)
                return
            if results and term.arg is None:
                # The front end appends an implicit valueless Return to close
                # every function, including value-returning ones -- reaching it
                # means control ran off the end, which C leaves undefined. A
                # bare `return` here would not type-check (wasm wants the
                # result on the stack), and inventing a zero would invent an
                # answer, so trap instead.
                body.op(wasm.OP_UNREACHABLE)
                return
            if term.arg is not None and results:
                self._wasm_push(term.arg, body, locals_of, nparams)
                self._wasm_convert(body, self._wasm_ret_ctype(func),
                                   term.arg.ctype)
            # Release the frame before leaving. The return value is already on
            # the stack and the epilogue is stack-neutral, so it does not
            # disturb it.
            self._wasm_epilogue(body)
            body.op(wasm.OP_RETURN)
            return

        if isinstance(term, control.Jump):
            self._wasm_goto(body, label_block[term.label], loop_depth,
                            state_local)
            return

        # Conditional. Both candidate block indices are pushed, then `select`
        # picks between them on the condition -- which keeps the `br` outside
        # any nested construct, so `loop_depth` stays valid. (An if/else here
        # would put the branch one level deeper and require adjusting it.)
        taken = label_block[term.label]
        body.const_i32(taken)
        body.const_i32(nxt)
        self._wasm_push(term.cond, body, locals_of, nparams)
        cond_vt = self._wasm_valtype(term.cond.ctype)
        if isinstance(term, control.JumpZero):
            # select keeps the first operand when the condition is non-zero,
            # so the condition to compute is `cond == 0`.
            body.op(wasm.OP_I32_EQZ if cond_vt == wasm.I32
                    else wasm.OP_I64_EQZ)
        else:                            # JumpNotZero
            if cond_vt == wasm.I64:
                # select's condition must be i32; reduce the i64 to a 0/1
                # flag by double negation rather than wrapping, which would
                # lose a set bit above bit 32.
                body.op(wasm.OP_I64_EQZ)
                body.op(wasm.OP_I32_EQZ)
        body.op(wasm.OP_SELECT)
        body.local_set(state_local)
        body.br(loop_depth)

    def _wasm_ret_ctype(self, func):
        """The declared return type of `func`, for converting a returned
        value that the front end left at a different width."""
        var = self.symbol_table.linkages[self.symbol_table.EXTERNAL].get(func)
        if var is None:
            var = self.symbol_table.linkages[
                self.symbol_table.INTERNAL].get(func)
        return var.ctype.ret

    # --------------------------------------------------- instruction selection

    def _wasm_binop_name(self, cmd, math_cmds, signed):
        """Mnemonic suffix for an arithmetic / bitwise IL command."""
        if isinstance(cmd, math_cmds.Add):
            return "add"
        if isinstance(cmd, math_cmds.Subtr):
            return "sub"
        if isinstance(cmd, math_cmds.Mult):
            return "mul"
        if isinstance(cmd, math_cmds.Div):
            return "div_s" if signed else "div_u"
        if isinstance(cmd, math_cmds.Mod):
            return "rem_s" if signed else "rem_u"
        if isinstance(cmd, math_cmds.BitAnd):
            return "and"
        if isinstance(cmd, math_cmds.BitOr):
            return "or"
        if isinstance(cmd, math_cmds.BitXor):
            return "xor"
        if isinstance(cmd, math_cmds.LBitShift):
            return "shl"
        if isinstance(cmd, math_cmds.RBitShift):
            # C's >> on a signed value is arithmetic, on an unsigned value
            # logical. wasm spells that as two different instructions.
            return "shr_s" if signed else "shr_u"
        return None

    def _wasm_cmp_name(self, cmd, cmp_cmds, signed):
        """Mnemonic suffix for a comparison IL command."""
        if isinstance(cmd, cmp_cmds.EqualCmp):
            return "eq"
        if isinstance(cmd, cmp_cmds.NotEqualCmp):
            return "ne"
        if isinstance(cmd, cmp_cmds.LessCmp):
            return "lt_s" if signed else "lt_u"
        if isinstance(cmd, cmp_cmds.GreaterCmp):
            return "gt_s" if signed else "gt_u"
        if isinstance(cmd, cmp_cmds.LessOrEqCmp):
            return "le_s" if signed else "le_u"
        if isinstance(cmd, cmp_cmds.GreaterOrEqCmp):
            return "ge_s" if signed else "ge_u"
        return None

    def _lower_wasm(self, cmd, body, locals_of, nparams, mod):
        """Lower one non-terminator IL command into `body`.

        Terminators are handled by _wasm_emit_block, which owns the dispatch
        state; everything else is a straightforward stack-machine expansion:
        push the operands, apply the operator, pop into the output's local.
        """
        import shivyc.wasm as wasm
        import shivyc.il_cmds.control as control
        import shivyc.il_cmds.value as value_cmds
        import shivyc.il_cmds.math as math_cmds
        import shivyc.il_cmds.compare as cmp_cmds

        if isinstance(cmd, value_cmds.LoadArg):
            if cmd.base is not None:
                # A named parameter of a variadic function. It lives in the
                # caller's argument block, not in a wasm parameter -- the
                # function has none -- at a fixed 8-byte slot.
                self._wasm_push(cmd.base, body, locals_of, nparams)
                self._wasm_addr_of_stack(body)
                op, align = self._wasm_load_op(cmd.output.ctype)
                # Slots are 8 bytes and little-endian, so a narrower load from
                # the slot's start reads the right bytes; the store side
                # widened the value to fill the slot.
                body.mem(op, 0, 8 * cmd.base_index)
                self._wasm_pop_into(cmd.output, body, locals_of, nparams)
                return
            pidx = self._wasm_argmap.get(id(cmd), cmd.arg_num)
            if cmd.output.ctype.is_struct_union() \
                    or cmd.output.ctype.is_array():
                # A struct parameter arrives as the address of the caller's
                # object. Copying it into our own frame here is what makes the
                # parameter by *value*: the callee may then modify it freely
                # without the caller seeing the change.
                self._wasm_push_addr(cmd.output, body)
                body.local_get(pidx)
                body.const_i32(cmd.output.ctype.size)
                body.memory_copy()
                return
            # Parameters are already locals 0..n-1; copy into the value's own
            # local so later writes to it cannot disturb the incoming argument.
            body.local_get(pidx)
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        if isinstance(cmd, value_cmds.LoadStructArg):
            # A struct parameter too big for a register on SysV. Here every
            # struct parameter arrives the same way -- as the address of the
            # caller's object -- so this is the aggregate LoadArg case again:
            # copy it into our own frame to make the parameter by value.
            pidx = self._wasm_argmap.get(id(cmd), 0)
            self._wasm_push_addr(cmd.output, body)
            body.local_get(pidx)
            body.const_i32(cmd.output.ctype.size)
            body.memory_copy()
            return

        if isinstance(cmd, value_cmds.VaSaveBase):
            # The caller left the block's address in the va-base global; take a
            # private copy before anything else can disturb it.
            body.global_get(self._wasm_va_base)
            body.op(wasm.OP_I64_EXTEND_I32_U)
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        if isinstance(cmd, value_cmds.VaStartAddr):
            # va_start: point the list just past the named parameters.
            if cmd.base is None:
                raise NotImplementedError(
                    "wasm back end: va_start without a caller-provided "
                    "argument block is not implemented")
            self._wasm_push(cmd.base, body, locals_of, nparams)
            if cmd.named_count:
                body.const_i64(8 * cmd.named_count)
                body.op(wasm.I64_BIN["add"])
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        if isinstance(cmd, value_cmds.Set):
            out = cmd.output
            if out.ctype.is_array() or out.ctype.is_struct_union():
                # Struct assignment. Both sides live in memory, so this is a
                # block copy: memory.copy takes (dest, src, len) and is
                # defined to handle overlap, which matters because assigning
                # from an overlapping source is legal C.
                if out.ctype.size != cmd.arg.ctype.size:
                    raise NotImplementedError(
                        "wasm back end: aggregate assignment between "
                        "different sizes (%d and %d)"
                        % (out.ctype.size, cmd.arg.ctype.size))
                self._wasm_push_addr(out, body)
                self._wasm_push_addr(cmd.arg, body)
                body.const_i32(out.ctype.size)
                body.memory_copy()
                return
            self._wasm_push(cmd.arg, body, locals_of, nparams)
            self._wasm_convert(body, cmd.output.ctype, cmd.arg.ctype)
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        if isinstance(cmd, math_cmds.Neg) or isinstance(cmd, math_cmds.Not):
            out = cmd.output
            vt = self._wasm_valtype(out.ctype)
            if out.ctype.is_floating():
                if isinstance(cmd, math_cmds.Not):
                    # ~ has no meaning on a floating operand; the front end
                    # should have rejected this.
                    raise NotImplementedError(
                        "wasm back end: bitwise complement of a floating "
                        "value is not a valid operation")
                self._wasm_push(cmd.arg, body, locals_of, nparams)
                self._wasm_convert(body, out.ctype, cmd.arg.ctype)
                # f*.neg flips the sign bit. Computing 0 - x instead would be
                # wrong for -0.0, which must negate to +0.0 rather than to
                # -0.0 as the subtraction would give.
                body.op(wasm.OP_F32_NEG if vt == wasm.F32
                        else wasm.OP_F64_NEG)
                self._wasm_pop_into(out, body, locals_of, nparams)
                return
            if isinstance(cmd, math_cmds.Neg):
                # wasm has no unary negate: 0 - x.
                body.const(vt, 0)
                self._wasm_push(cmd.arg, body, locals_of, nparams)
                self._wasm_convert(body, out.ctype, cmd.arg.ctype)
                body.op(wasm.I32_BIN["sub"] if vt == wasm.I32
                        else wasm.I64_BIN["sub"])
            else:
                # ...nor a bitwise complement: x ^ -1.
                self._wasm_push(cmd.arg, body, locals_of, nparams)
                self._wasm_convert(body, out.ctype, cmd.arg.ctype)
                body.const(vt, -1)
                body.op(wasm.I32_BIN["xor"] if vt == wasm.I32
                        else wasm.I64_BIN["xor"])
            self._wasm_truncate(body, out.ctype, vt)
            self._wasm_pop_into(out, body, locals_of, nparams)
            return

        name = self._wasm_binop_name(cmd, math_cmds,
                                     self._wasm_signed(
                                         cmd.outputs()[0].ctype)
                                     if cmd.outputs() else True)
        if name is not None:
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            vt = self._wasm_valtype(out.ctype)
            if out.ctype.is_floating():
                # The name was chosen using integer signedness, which a float
                # type does not have -- `/` arrives here as "div_u" because
                # _wasm_signed reads a missing `signed` attribute as unsigned.
                # Strip the suffix; a float operation has only one form.
                fname = name.split("_")[0]
                if fname not in ("add", "sub", "mul", "div"):
                    # %, the bitwise operators and the shifts have no floating
                    # form in C either.
                    raise NotImplementedError(
                        "wasm back end: '%s' is not defined on floating "
                        "operands" % fname)
                self._wasm_push(ins[0], body, locals_of, nparams)
                self._wasm_convert(body, out.ctype, ins[0].ctype)
                self._wasm_push(ins[1], body, locals_of, nparams)
                self._wasm_convert(body, out.ctype, ins[1].ctype)
                body.op(wasm.F32_BIN[fname] if vt == wasm.F32
                        else wasm.F64_BIN[fname])
                self._wasm_pop_into(out, body, locals_of, nparams)
                return
            # The IL documents that a binop's three values share a type, but
            # converting each operand to the output type anyway costs nothing
            # when that holds and keeps a shift by a differently-sized count
            # correct when it does not.
            self._wasm_push(ins[0], body, locals_of, nparams)
            self._wasm_convert(body, out.ctype, ins[0].ctype)
            self._wasm_push(ins[1], body, locals_of, nparams)
            self._wasm_convert(body, out.ctype, ins[1].ctype)
            body.op(wasm.I32_BIN[name] if vt == wasm.I32
                    else wasm.I64_BIN[name])
            # Sub-word arithmetic can carry out of the C type's width, so
            # bring the result back into range before it is stored.
            self._wasm_truncate(body, out.ctype, vt)
            self._wasm_pop_into(out, body, locals_of, nparams)
            return

        cname = self._wasm_cmp_name(cmd, cmp_cmds, True)
        if cname is not None:
            ins = cmd.inputs()
            out = cmd.outputs()[0]
            if ins[0].ctype.is_floating() or ins[1].ctype.is_floating():
                # Compare at the wider of the two float types, promoting an
                # integer operand if one side is not floating at all.
                a, b = ins[0], ins[1]
                op_ctype = a.ctype if (a.ctype.is_floating() and
                                       (not b.ctype.is_floating() or
                                        a.ctype.size >= b.ctype.size)) \
                    else b.ctype
                op_vt = self._wasm_valtype(op_ctype)
                self._wasm_push(a, body, locals_of, nparams)
                self._wasm_convert(body, op_ctype, a.ctype)
                self._wasm_push(b, body, locals_of, nparams)
                self._wasm_convert(body, op_ctype, b.ctype)
                # Strip the _s/_u suffix the integer namer added: a float
                # comparison has no signedness.
                fname = cname.split("_")[0]
                body.op(wasm.F32_CMP[fname] if op_vt == wasm.F32
                        else wasm.F64_CMP[fname])
                out_vt = self._wasm_valtype(out.ctype)
                if out_vt == wasm.I64:
                    body.op(wasm.OP_I64_EXTEND_I32_U)
                self._wasm_pop_into(out, body, locals_of, nparams)
                return
            # The comparison happens at the *operands'* width and signedness;
            # the result is always an int (i32), regardless.
            a, b = ins[0], ins[1]
            op_ctype = a.ctype if a.ctype.size >= b.ctype.size else b.ctype
            signed = self._wasm_signed(a.ctype) and self._wasm_signed(b.ctype)
            cname = self._wasm_cmp_name(cmd, cmp_cmds, signed)
            op_vt = self._wasm_valtype(op_ctype)
            self._wasm_push(a, body, locals_of, nparams)
            self._wasm_convert(body, op_ctype, a.ctype)
            self._wasm_push(b, body, locals_of, nparams)
            self._wasm_convert(body, op_ctype, b.ctype)
            body.op(wasm.I32_CMP[cname] if op_vt == wasm.I32
                    else wasm.I64_CMP[cname])
            # Comparisons yield i32 0/1; widen if the output is a long.
            out_vt = self._wasm_valtype(out.ctype)
            if out_vt == wasm.I64:
                body.op(wasm.OP_I64_EXTEND_I32_U)
            self._wasm_pop_into(out, body, locals_of, nparams)
            return

        if isinstance(cmd, control.Call):
            fname = cmd.direct_name
            if fname is None:
                self._wasm_indirect_call(cmd, body, locals_of, nparams, mod)
                return

            idx = mod.func_index(fname)
            if idx < 0:
                raise NotImplementedError(
                    "wasm back end: call to '%s', which is neither defined "
                    "here nor importable" % fname)
            is_import = fname in getattr(self, "_wasm_imports", {})
            if is_import:
                params, results = self._wasm_import_sig(fname)
            else:
                params, results = self._wasm_func_sig(fname)
            # The variadic path is taken before the arity check: a variadic
            # callee declares no wasm parameters at all, so its "arity" never
            # matches the call's argument count and never should.
            if getattr(cmd, "variadic", False):
                self._wasm_variadic_call(cmd, idx, body, locals_of, nparams)
                if cmd.void_return or not results:
                    if results:
                        body.op(wasm.OP_DROP)
                    return
                self._wasm_pop_into(cmd.ret, body, locals_of, nparams)
                return

            # `callee_agg_ret` is true only when *this* back end synthesises
            # the hidden pointer. When the front end already did (a struct
            # over 16 bytes), the pointer is simply cmd.args[0] and the call
            # needs no special treatment at all -- so `extra` counts only the
            # pointer we have to add ourselves.
            callee_agg_ret = False
            front_callee = False
            if not is_import:
                callee_agg_ret, _ = self._wasm_sig_info(fname)
                front_callee = self._wasm_front_sret(fname)
            extra = 1 if callee_agg_ret else 0
            if len(cmd.args) + extra != len(params):
                raise NotImplementedError(
                    "wasm back end: call to '%s' with %d arguments but %d "
                    "parameters" % (fname, len(cmd.args), len(params)))

            if callee_agg_ret:
                # The destination for the returned struct goes in first. It is
                # cmd.ret's own storage -- an aggregate, so it already has a
                # frame slot -- which means the callee writes the result
                # straight where it is wanted, with no extra copy.
                self._wasm_push_addr(cmd.ret, body)

            # Arguments are pushed left to right; wasm pops them into the
            # callee's locals in the same order, so no shuffling is needed.
            i = 0
            first_arg = True
            for arg in cmd.args:
                if front_callee and first_arg:
                    # The front end's hidden result pointer. It is not a
                    # declared parameter, so it has no entry in ctype.args and
                    # must not consume an index -- it is just an ordinary
                    # pointer value, passed through unchanged.
                    first_arg = False
                    self._wasm_push(arg, body, locals_of, nparams)
                    continue
                first_arg = False
                if arg.ctype.is_struct_union() or arg.ctype.is_array():
                    # Pass the address; the callee makes its own copy.
                    self._wasm_push_addr(arg, body)
                    i += 1
                    continue
                pctype = self._wasm_arg_ctype(fname, i)
                self._wasm_push(arg, body, locals_of, nparams)
                self._wasm_convert(body, pctype, arg.ctype)
                if is_import and pctype.is_pointer():
                    # Narrow the i64 pointer to the i32 address the host's
                    # 32-bit signature declares.
                    body.op(wasm.OP_I32_WRAP_I64)
                i += 1
            body.call(idx)
            if callee_agg_ret:
                # Nothing came back on the stack; the result is already in
                # cmd.ret's storage.
                return
            if cmd.void_return or not results:
                # A non-void callee whose result is discarded still leaves a
                # value on the stack, and wasm validation rejects that.
                if results:
                    body.op(wasm.OP_DROP)
                return
            if is_import and cmd.ret.ctype.is_pointer():
                # The host returned a 32-bit address; widen it back to the
                # i64 a pointer is carried in.
                body.op(wasm.OP_I64_EXTEND_I32_U)
            self._wasm_pop_into(cmd.ret, body, locals_of, nparams)
            return

        if isinstance(cmd, value_cmds.AddrOf):
            # &x. The object was forced into memory precisely so this address
            # exists; producing it is just its frame slot or static address,
            # widened to the i64 a pointer is carried in.
            if cmd.var.ctype.is_function():
                # A function pointer is its index in the module's function
                # table -- wasm has no code addresses, and an indirect call
                # names a table slot. Index 0 is left empty, so a null pointer
                # traps when called rather than dispatching somewhere.
                fname = self.symbol_table.names.get(cmd.var)
                if fname is None or mod.func_index(fname) < 0:
                    raise NotImplementedError(
                        "wasm back end: address taken of function '%s', which "
                        "is not in this module" % fname)
                body.const_i32(mod.table_index(fname))
                body.op(wasm.OP_I64_EXTEND_I32_U)
                self._wasm_pop_into(cmd.output, body, locals_of, nparams)
                return
            self._wasm_push_addr(cmd.var, body)
            body.op(wasm.OP_I64_EXTEND_I32_U)
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        if isinstance(cmd, value_cmds.ReadAt):
            if cmd.output.ctype.is_array() \
                    or cmd.output.ctype.is_struct_union():
                self._wasm_push_addr(cmd.output, body)
                self._wasm_push(cmd.addr, body, locals_of, nparams)
                self._wasm_addr_of_stack(body)
                body.const_i32(cmd.output.ctype.size)
                body.memory_copy()
                return
            # *p
            self._wasm_push(cmd.addr, body, locals_of, nparams)
            self._wasm_addr_of_stack(body)
            op, align = self._wasm_load_op(cmd.output.ctype)
            body.mem(op, align, 0)
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        if isinstance(cmd, value_cmds.SetAt):
            if cmd.val.ctype.is_array() or cmd.val.ctype.is_struct_union():
                self._wasm_push(cmd.addr, body, locals_of, nparams)
                self._wasm_addr_of_stack(body)
                self._wasm_push_addr(cmd.val, body)
                body.const_i32(cmd.val.ctype.size)
                body.memory_copy()
                return
            # *p = v. Address first, then the value: the order wasm wants.
            self._wasm_push(cmd.addr, body, locals_of, nparams)
            self._wasm_addr_of_stack(body)
            self._wasm_push(cmd.val, body, locals_of, nparams)
            op, align = self._wasm_store_op(cmd.val.ctype)
            body.mem(op, align, 0)
            return

        if isinstance(cmd, value_cmds.ReadRel):
            if cmd.output.ctype.is_array() \
                    or cmd.output.ctype.is_struct_union():
                # Reading a whole aggregate out of an array element or member:
                # a block copy into the destination's own storage. Destination
                # address first, since that is memory.copy's operand order.
                self._wasm_push_addr(cmd.output, body)
                self._wasm_rel_addr(cmd, body, locals_of, nparams)
                body.const_i32(cmd.output.ctype.size)
                body.memory_copy()
                return
            self._wasm_rel_addr(cmd, body, locals_of, nparams)
            op, align = self._wasm_load_op(cmd.output.ctype)
            body.mem(op, align, 0)
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        if isinstance(cmd, value_cmds.SetRel):
            if cmd.val.ctype.is_array() or cmd.val.ctype.is_struct_union():
                # Storing a whole aggregate into an array element or member.
                self._wasm_rel_addr(cmd, body, locals_of, nparams)
                self._wasm_push_addr(cmd.val, body)
                body.const_i32(cmd.val.ctype.size)
                body.memory_copy()
                return
            self._wasm_rel_addr(cmd, body, locals_of, nparams)
            self._wasm_push(cmd.val, body, locals_of, nparams)
            op, align = self._wasm_store_op(cmd.val.ctype)
            body.mem(op, align, 0)
            return

        if isinstance(cmd, value_cmds.AddrRel):
            self._wasm_rel_addr(cmd, body, locals_of, nparams)
            body.op(wasm.OP_I64_EXTEND_I32_U)
            self._wasm_pop_into(cmd.output, body, locals_of, nparams)
            return

        raise NotImplementedError(
            "wasm back end: IL command '%s' not implemented yet"
            % type(cmd).__name__)

    def _wasm_rel_base(self, base, body, locals_of, nparams):
        """Push the i32 base address for a relative access.

        A base is one of three things, exactly as on the register back ends: a
        static object (its fixed address), an aggregate in the frame (its
        address, not its contents), or an ordinary pointer value (already an
        address, carried as i64).
        """
        if base in self._wasm_addr:
            self._wasm_push_addr(base, body)
            return
        if base.ctype.is_array() or base.ctype.is_struct_union():
            self._wasm_push_addr(base, body)
            return
        self._wasm_push(base, body, locals_of, nparams)
        self._wasm_addr_of_stack(body)

    def _wasm_rel_addr(self, cmd, body, locals_of, nparams):
        """Push the i32 address `base + chunk*count` used by the Rel commands.

        `count` may be absent (a plain displacement, e.g. a struct member) or
        a literal (a constant index), in which case the whole offset folds
        into a single constant add rather than a runtime multiply.
        """
        import shivyc.wasm as wasm
        self._wasm_rel_base(cmd.base, body, locals_of, nparams)

        if not cmd.count:
            if cmd.chunk:
                body.const_i32(cmd.chunk)
                body.op(wasm.I32_BIN["add"])
            return

        lit = getattr(cmd.count, "literal", None)
        if lit is not None:
            off = int(lit.val) * cmd.chunk
            if off:
                body.const_i32(off)
                body.op(wasm.I32_BIN["add"])
            return

        # Runtime index. The count is a full-width integer; narrow it to the
        # i32 the address arithmetic is done in, then scale by the element
        # size.
        self._wasm_push(cmd.count, body, locals_of, nparams)
        if self._wasm_valtype(cmd.count.ctype) == wasm.I64:
            body.op(wasm.OP_I32_WRAP_I64)
        if cmd.chunk != 1:
            body.const_i32(cmd.chunk)
            body.op(wasm.I32_BIN["mul"])
        body.op(wasm.I32_BIN["add"])

    def _wasm_indirect_call(self, cmd, body, locals_of, nparams, mod):
        """A call through a function pointer.

        The pointer holds a table index rather than a code address, so this is
        `call_indirect`: arguments first, the index on top, and a signature
        that the engine checks at run time. A mismatch traps -- which is
        stricter than the register back ends, where calling through a
        wrongly-typed pointer just runs.

        The signature comes from the pointer's pointee type rather than from
        the argument values, so that an implicit conversion at the call site
        cannot silently produce a signature the callee does not have.
        """
        import shivyc.wasm as wasm
        fctype = cmd.func.ctype
        if fctype.is_pointer():
            fctype = fctype.arg
        if not fctype.is_function():
            raise NotImplementedError(
                "wasm back end: indirect call through a non-function pointer")
        variadic = getattr(fctype, "variadic", False)
        front_sret, my_sret = self._wasm_sret_kind(fctype)

        # Build the callee's signature exactly as _wasm_func_sig would for a
        # direct call, so a pointer and the function it points at agree. They
        # must: call_indirect compares the two at run time and traps if they
        # differ, so any disagreement here shows up as a trap rather than as a
        # quietly wrong call.
        params = []
        if front_sret:
            params.append(wasm.I64)
        elif my_sret:
            params.append(wasm.I32)
        if not variadic:
            for a in fctype.args:
                if a.is_struct_union() or a.is_array():
                    params.append(wasm.I32)
                else:
                    params.append(self._wasm_valtype(a))
        results = []
        if not fctype.ret.is_void() and not front_sret and not my_sret:
            results.append(self._wasm_valtype(fctype.ret))

        if variadic:
            # A variadic callee takes no declared parameters: the arguments go
            # into a block in this frame and the base travels in the va-base
            # global, exactly as for a direct variadic call. Only the dispatch
            # differs.
            self._wasm_stage_variadic_args(cmd, body, locals_of, nparams)
        else:
            extra = 1 if my_sret else 0
            if len(cmd.args) + extra != len(params):
                raise NotImplementedError(
                    "wasm back end: indirect call with %d arguments but %d "
                    "parameters" % (len(cmd.args), len(params)))
            if my_sret:
                self._wasm_push_addr(cmd.ret, body)
            i = 0
            first_arg = True
            for arg in cmd.args:
                if front_sret and first_arg:
                    first_arg = False
                    self._wasm_push(arg, body, locals_of, nparams)
                    continue
                first_arg = False
                if arg.ctype.is_struct_union() or arg.ctype.is_array():
                    self._wasm_push_addr(arg, body)
                    i += 1
                    continue
                self._wasm_push(arg, body, locals_of, nparams)
                self._wasm_convert(body, fctype.args[i], arg.ctype)
                i += 1

        # The table index goes on top, above the arguments.
        self._wasm_push(cmd.func, body, locals_of, nparams)
        self._wasm_addr_of_stack(body)
        mod.needs_table = True
        body.call_indirect(mod.type_index(params, results))

        if my_sret:
            return                       # the result is already in cmd.ret
        if cmd.void_return or not results:
            if results:
                body.op(wasm.OP_DROP)
            return
        self._wasm_pop_into(cmd.ret, body, locals_of, nparams)

    def _wasm_variadic_call(self, cmd, idx, body, locals_of, nparams):
        """Stage a variadic call's arguments and transfer the block's address.

        Every argument -- named and unnamed alike -- goes into one contiguous
        run of 8-byte slots in this function's frame, and the block's address
        is handed over in the va-base global. The callee then reads its named
        parameters out of the block by index and walks the rest with va_arg.

        Each slot is 8 bytes regardless of the argument's own width, because
        that is what va_arg's pointer arithmetic assumes. Narrow integers are
        widened and floats are promoted to double, which is what C's default
        argument promotions require of an unprototyped argument anyway.
        """
        self._wasm_stage_variadic_args(cmd, body, locals_of, nparams)
        # The callee takes no wasm parameters; everything travelled in the
        # block.
        body.call(idx)

    def _wasm_stage_variadic_args(self, cmd, body, locals_of, nparams):
        """Fill the outgoing argument block and publish its address.

        Shared by direct and indirect variadic calls -- only the dispatch
        instruction afterwards differs.
        """
        import shivyc.wasm as wasm
        if self._wasm_va_out < 0:
            raise NotImplementedError(
                "wasm back end: no outgoing block reserved for a variadic "
                "call")
        off = self._wasm_va_out
        for arg in cmd.args:
            body.local_get(self._wasm_fp)
            if arg.ctype.is_floating():
                # Default argument promotion: float becomes double.
                self._wasm_push(arg, body, locals_of, nparams)
                if self._wasm_valtype(arg.ctype) == wasm.F32:
                    body.op(wasm.OP_F64_PROMOTE_F32)
                body.mem(wasm.OP_F64_STORE, 0, off)
            else:
                # Widen to fill the slot so a later narrow load from its start
                # reads defined bytes rather than whatever was there before.
                self._wasm_push(arg, body, locals_of, nparams)
                if self._wasm_valtype(arg.ctype) == wasm.I32:
                    if self._wasm_signed(arg.ctype):
                        body.op(wasm.OP_I64_EXTEND_I32_S)
                    else:
                        body.op(wasm.OP_I64_EXTEND_I32_U)
                body.mem(wasm.OP_I64_STORE, 0, off)
            off += 8

        body.local_get(self._wasm_fp)
        if self._wasm_va_out:
            body.const_i32(self._wasm_va_out)
            body.op(wasm.I32_BIN["add"])
        body.global_set(self._wasm_va_base)

    def _wasm_front_sret(self, fname):
        """Whether the front end already rewrote calls to `fname` to pass a
        hidden result pointer as their first argument."""
        var = self.symbol_table.linkages[self.symbol_table.EXTERNAL].get(fname)
        if var is None:
            var = self.symbol_table.linkages[
                self.symbol_table.INTERNAL].get(fname)
        if var is None or getattr(var, "ctype", None) is None:
            return False
        front, _ = self._wasm_sret_kind(var.ctype)
        return front

    def _wasm_arg_ctype(self, fname, i):
        """Declared type of parameter `i` of function `fname`."""
        var = self.symbol_table.linkages[self.symbol_table.EXTERNAL].get(fname)
        if var is None:
            var = self.symbol_table.linkages[
                self.symbol_table.INTERNAL].get(fname)
        return var.ctype.args[i]
