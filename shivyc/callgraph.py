"""Whole-program (cross-translation-unit) call-graph analysis.

ShivyC compiles each translation unit independently, so any analysis that
walks the call graph (e.g. the metamorphic-reentrancy and -O4 near-scratch
safety checks) only ever sees one TU at a time. This module parses every input
TU -- reusing the on-disk AST cache so repeated TUs are cheap -- lowers each to
IL, and merges the per-TU call graphs into a single whole-program graph.

The graph is intentionally derived from the IL (the same `Call.direct_name`
edges the existing single-TU analyses use), so it is exactly the cross-TU
generalization of those analyses. It is also independent of the normal
compile pipeline: building it never changes code generation, so it cannot
affect the output of an ordinary single-file compile.
"""

import re
import sys

import shivyc.il_cmds.control as control_cmds
import shivyc.il_cmds.value as value_cmds


_BIT_RE = re.compile(r"_(\d+)bit$")


class WholeProgramGraph:
    """Merged call graph across translation units.

    defined  - set of function names with a definition somewhere in the program
    edges    - {caller_name -> set(callee_names)} (direct calls only)
    addr_taken - set of function names whose address is taken anywhere
    tu_of    - {function_name -> source file that defines it}
    """

    def __init__(self):
        self.defined = set()
        self.edges = {}
        self.addr_taken = set()
        self.tu_of = {}
        self.addr_taken_vars = set()   # global vars whose address escapes
        self.flag_globals = {}         # external flag global name -> bits
        self.inlinable = {}            # function name -> inline.InlineBody

    def add_tu(self, filename, il_code, symbol_table):
        """Merge one translation unit's IL into the whole-program graph."""
        names_by_val = {v: n for v, n in symbol_table.names.items()}
        for fn, cmds in il_code.commands.items():
            self.defined.add(fn)
            self.tu_of.setdefault(fn, filename)
            callees = self.edges.setdefault(fn, set())
            for cmd in cmds:
                if isinstance(cmd, control_cmds.Call) and cmd.direct_name:
                    callees.add(cmd.direct_name)
                # AddrOf of a function => its address is taken (possible
                # indirect re-entry). AddrOf of a variable => that global's
                # address escapes, so it cannot be safely cached in a register.
                if isinstance(cmd, value_cmds.AddrOf):
                    v = getattr(cmd, "var", None)
                    target = names_by_val.get(v)
                    if target:
                        ct = getattr(v, "ctype", None)
                        if ct is not None and ct.is_function():
                            self.addr_taken.add(target)
                        else:
                            self.addr_taken_vars.add(target)
        self._collect_flag_globals(symbol_table)
        self._capture_inlinable(il_code, symbol_table)

    def _capture_inlinable(self, il_code, symbol_table):
        """Record any small, pure, straight-line leaf functions for inlining."""
        import shivyc.inline as inline
        for fn, cmds in il_code.commands.items():
            if fn in self.inlinable:
                continue
            body = inline.capture(cmds, symbol_table)
            if body is not None:
                self.inlinable[fn] = body

    def _collect_flag_globals(self, symbol_table):
        """Record externally-linked 1-byte ``*_Nbit`` flag globals.

        Only external-linkage flags are eligible for *whole-program* packing:
        their byte is visible from every unit (so the layout can be shared and
        the value seeded from one place), whereas a static flag is local to its
        own unit and is left to the per-TU mechanism.
        """
        EXT = symbol_table.EXTERNAL
        for v, name in symbol_table.names.items():
            if symbol_table.storage.get(v) != symbol_table.STATIC:
                continue
            if symbol_table.linkage_type.get(v) != EXT:
                continue
            ct = getattr(v, "ctype", None)
            if ct is None or ct.is_function() or ct.size != 1:
                continue
            m = _BIT_RE.search(name)
            if m and 1 <= int(m.group(1)) <= 8:
                self.flag_globals[name] = int(m.group(1))

    def simd_pack_layout(self):
        """Build a deterministic, frozen whole-program SIMD-pack layout.

        Flags are assigned bit positions in sorted-name order, so every
        translation unit that consults this layout agrees on the bit positions
        without having to communicate. A flag whose address is taken anywhere
        is excluded, since pointer accesses would bypass the register cache.
        """
        import shivyc.simd_pack as simd_pack
        layout = simd_pack.SimdPackLayout()
        for name in sorted(self.flag_globals):
            if name in self.addr_taken_vars:
                continue
            layout.consider(name, 1)
        layout.frozen = True
        return layout

    def reaches_self(self, name):
        """Whether `name` can reach itself through direct calls (recursion)."""
        seen, stack = set(), list(self.edges.get(name, ()))
        while stack:
            cur = stack.pop()
            if cur == name:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.edges.get(cur, ()))
        return False

    def undefined_calls(self):
        """Return {caller -> set(callees with no definition in the program)}.

        Callees that are never defined in any TU are external symbols (libc,
        assembly stubs, or genuinely missing). This is informational.
        """
        out = {}
        for caller, callees in self.edges.items():
            missing = {c for c in callees if c not in self.defined}
            if missing:
                out[caller] = missing
        return out

    def summary(self):
        """A short human-readable summary of the whole-program graph."""
        total_edges = sum(len(c) for c in self.edges.values())
        lines = [
            f"functions defined: {len(self.defined)}",
            f"call edges:        {total_edges}",
            f"address-taken:     {len(self.addr_taken)}",
        ]
        return "\n".join(lines)


def build_program_graph(files, args):
    """Build a WholeProgramGraph over the given .c files.

    Reuses the AST cache for parsing and applies direct-call folding so call
    edges are visible. Returns (graph, ok) where ok is False if any file
    failed the front end. This performs its own front-end pass and never
    affects normal code generation.
    """
    import shivyc.lexer as lexer
    import shivyc.preproc as preproc
    import shivyc.weak_alias as weak_alias
    import shivyc.pack as pack
    import shivyc.cache as cache
    import shivyc.stackless as stackless
    import shivyc.main as main_mod
    from shivyc.errors import error_collector
    from shivyc.parser.parser import parse
    from shivyc.tree.general_nodes import Root
    from shivyc.il_gen import ILCode, SymbolTable, Context

    graph = WholeProgramGraph()
    ok = True
    for file in files:
        if not file.endswith(".c"):
            continue
        try:
            with open(file) as f:
                code = f.read()
        except OSError:
            ok = False
            continue

        import shivyc.extensions as extensions
        try:
            code, _ = extensions.preprocess_extensions(code)
        except extensions.ExtensionError:
            ok = False
            continue

        error_collector.clear()
        tokens = preproc.process(lexer.tokenize(code, file), file)
        # Before extract_aliases, which strips `packed` (see shivyc.pack).
        tokens = pack.apply_packing(tokens)
        tokens, _ = weak_alias.extract_aliases(tokens)
        tokens = main_mod._concat_adjacent_strings(tokens)

        key = cache.token_key(tokens)
        if sys.implementation.name != 'shivyc':
            ast = cache.load_ast(key)
            if ast is None:
                ast = parse(tokens)
                if ast is not None and error_collector.ok():
                    cache.store_ast(key, ast)
        else:
            # The on-disk AST cache is host-only (it pickles); the self-hosted
            # compiler always parses. Coming straight from parse() keeps `ast`
            # concretely typed, so `ast.make_il(...)` is a vtable dispatch rather
            # than an unresolved bare call.
            ast = parse(tokens)
        if ast is None:
            ok = False
            continue

        il, st = ILCode(), SymbolTable()
        try:
            ast.make_il(il, st, Context())
        except Exception:
            ok = False
            continue
        if not error_collector.ok():
            ok = False

        for fn in list(il.commands):
            il.commands[fn] = stackless._apply_direct_calls(il.commands[fn], st)
        graph.add_tu(file, il, st)

    return graph, ok
