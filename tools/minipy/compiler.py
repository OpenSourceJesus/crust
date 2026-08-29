"""minipy.compiler -- CPython-side AOT front end for the rpython interpreter.

This is the ".py -> flattened bytecode" stage. It does NOT hand the rpython
interpreter a raw AST tree (that would force the interpreter to walk a deep,
irregular structure). Instead it lowers the AST to a *flat, register-based*
instruction stream over uniform POD records, the form a fast dispatch loop wants
(the same shape TPython's `tp_step` runs). The result serialises to JSON whose
objects map one-to-one onto the rpython POD classes in `schema.py`, so
`rpy.json.generate_decoder` can unpack it straight into structs -- no dict, no
boxing.

Everything here is CPython-only (it runs inside `rpy.py`, the driver). The
`Program` it emits is the contract; `VM` below is a faithful reference executor
used to validate the bytecode against `python3` before the compiled rpython
interpreter exists, and as the differential oracle thereafter.

Scope (v0): module + top-level defs (positional params), return, assignment,
aug-assign, arithmetic (+ - * / // %), single-comparator compares, and/or, not,
unary minus, if/else, while, for-in-range, break/continue/pass, calls, and the
builtins print/len/range/int/str/float/abs/bool. Comprehensions, classes,
closures, exceptions, **kwargs etc. are deliberately out of v0 -- they slot in
later without changing the format.
"""
from __future__ import annotations
import ast
# `sys` is used only for the `sys.implementation.name` guards that fold away
# when compiler.py is itself compiled by minipy. minipy treats `import sys` as a
# recognised no-op, so this line is safe on both implementations.
import sys
# `os` is needed by _module_registry(). It must be imported at module level: the
# linker rewrites top-level imports only, and on minipy `os` resolves to
# rpy_lib/minios.py.
import os

# ---------------------------------------------------------------------------
# Opcodes. Numbers are grouped so AOT-specialised variants get their own band
# (60-89), exactly like TPython reserves a fast-op range. Every instruction is
# the uniform 4-int record (op, a, b, c); operand meaning is per-opcode below.
# ---------------------------------------------------------------------------
OPS = {
    # --- core (0-19) ---
    "NOP":          0,
    "LOAD_CONST":   1,   # reg[a] = consts[b]
    "LOAD_GLOBAL":  2,   # reg[a] = globals[b]            (b = global slot)
    "STORE_GLOBAL": 3,   # globals[b] = reg[a]
    "MOVE":         4,   # reg[a] = reg[b]
    "RETURN":       5,   # return reg[a]
    "JUMP":         6,   # pc = a
    "JUMP_IF_FALSE": 7,  # if not truthy(reg[a]): pc = b
    "JF_LT": 76, "JF_LE": 77, "JF_GT": 78,  # fused compare+branch:
    "JF_GE": 79, "JF_EQ": 80, "JF_NE": 81,  # if not(reg[a] CMP reg[c]): pc=b
    "JUMP_IF_TRUE": 82,                    # if truthy(reg[a]): pc=b
    "JT_LT": 83, "JT_LE": 84, "JT_GT": 85,  # fused compare+branch:
    "JT_GE": 86, "JT_EQ": 87, "JT_NE": 88,  # if (reg[a] CMP reg[c]): pc=b
    "CALL_FUNC": 89,                       # direct call: c = fidx*256 + nargs
    "CALL_KW":     90,   # reg[a]=reg[b](pos+kw); c=npos*256+nkw; window carries
                         # positional values, keyword values, then keyword-name strs
    "CALL_SPREAD": 91,   # reg[a]=reg[b](reg[b+1..b+c], *reg[b+c+1]); c = #fixed
    "CALL":         8,   # reg[a] = reg[b](reg[b+1..b+c]); c = argcount
    # --- containers (9-19) ---
    "BUILD_LIST":   9,   # reg[a] = [regs[b..b+c-1]]
    "BUILD_TUPLE": 10,   # reg[a] = (regs[b..b+c-1])
    "BUILD_DICT":  11,   # reg[a] = {regs[b],regs[b+1] : ...}  c pairs
    "BUILD_SET":   12,   # reg[a] = {regs[b..b+c-1]}
    "INDEX":       13,   # reg[a] = reg[b][reg[c]]
    "SETINDEX":    14,   # reg[a][reg[b]] = reg[c]
    "INDEX_INT":   54,   # typed list[int] get: reg[a]=reg[b][reg[c]], no dispatch
    "SETINDEX_INT":55,   # typed list[int] set: reg[a][reg[b]]=reg[c], no dispatch
    "ACC_ADD_GINT":56,   # fused: glob[a] += tlist(reg[b])[reg[c]] (typed accumulate)
    "ACC_MAC_GINT":57,   # fused: glob[a] += tA[k]*tB[j]; b,c pack (row*4096+idx)
    "ACC_ADD_G":   58,   # fused: glob[a] = glob[a] + reg[b] (general accumulate)
    "ACC_ADD_L":   67,   # fused: reg[a] = reg[a] + reg[b] (local accumulate)
    "ACC_SUB_L":   68,   # fused: reg[a] = reg[a] - reg[b] (local accumulate)
    "ITER_NEW":    15,   # reg[a] = iter(reg[b])
    "ITER_NEXT":   16,   # if next: reg[a]=next, pc++ ; else pc=c
    "CONTAINS":    17,   # reg[a] = (reg[c] in reg[b])
    "LIST_APPEND": 18,   # reg[a].append(reg[b])
    "SET_ADD":     19,   # reg[a].add(reg[b])
    # --- classes (50-52) ---
    "LOAD_ATTR":   50,   # reg[a] = reg[b].<consts[c]>
    "STORE_ATTR":  51,   # reg[a].<consts[c]> = reg[b]
    "LOAD_METHOD": 52,   # reg[a] = bound-method reg[b].<consts[c]>
    "CALL_METHOD": 53,   # reg[a] = reg[b].<name>(reg[b+1..]); c = name*256+nargs
    # --- exceptions (70-75) ---
    "SETUP_EXCEPT": 70,  # push handler block; a = dispatch pc
    "POP_BLOCK":    71,  # pop handler block (try body finished normally)
    "RAISE":        72,  # raise reg[a]
    "RERAISE":      73,  # re-raise the in-flight exception
    "LOAD_EXC":     74,  # reg[a] = current in-flight exception value
    "EXC_MATCH":    75,  # reg[a] = isinstance(in-flight exc, class reg[b])
    # --- generic arithmetic (20-29): reg[a] = reg[b] OP reg[c] ---
    "ADD":         20, "SUB": 21, "MUL": 22, "DIV": 23,
    "MOD":         24, "FLOORDIV": 25, "POW": 26,
    "BITOR":       27, "BITAND": 28, "BITXOR": 29,  # int bitwise / set ops
    # --- generic compare (30-39): reg[a] = reg[b] CMP reg[c] ---
    "LT":          30, "LE": 31, "GT": 32, "GE": 33, "EQ": 34, "NE": 35,
    "IS":          59,                   # reg[a] = (reg[b] is reg[c]) tag-strict
    "SHL":         36, "SHR": 37,        # reg[a] = reg[b] << / >> reg[c]
    "SLICE":       38,                   # reg[a] = reg[a][lo:hi:step], (lo,hi,
                                         # step) = reg[b],reg[b+1],reg[b+2]
    "SLICE_STORE":  39,                  # reg[a][lo:hi] = value; (lo,hi,value)
                                         # = reg[b],reg[b+1],reg[b+2]
    # --- unary (40-49): reg[a] = OP reg[b] ---
    "NEG":         40, "NOT": 41,
    # --- AOT-specialised numeric fast paths (60-89): no tag check ---
    "ADD_NN":      60, "SUB_NN": 61, "MUL_NN": 62,
    "LT_NN":       63, "LE_NN": 64, "GT_NN": 65, "GE_NN": 66,
}
OPNAME = {v: k for k, v in OPS.items()}

# Builtin ids, stored in a const of kind "builtin".
BUILTINS = {n: i for i, n in enumerate(
    ["print", "len", "range", "int", "str", "float", "abs", "bool",
     "list", "dict", "set", "tuple", "repr", "sorted", "sum", "min", "max",
     "isinstance", "enumerate", "zip", "any", "all", "ord", "chr",
     "reversed", "getattr", "hasattr", "type", "setattr", "frozenset",
     # The regex seam. These are the only way guest code reaches a regex
     # engine: rpy_lib/crustre.py (registered below as `re`) calls them, and
     # interp.py implements them with a runtime-valued re.search/re.match that
     # py2c lowers to the crust_re core. One engine for guests, py2c-compiled
     # programs, C and C++ alike -- rather than a second Python implementation
     # to keep in sync.
     "__re_search", "__re_match"])}

# Builtin exception hierarchy (name -> base name), parents before children so a
# class's base is already registered when it is.  These are materialised as real
# minipy classes (the interpreter's is_instance already walks the .base chain),
# so `raise ValueError(...)` and `except Exception:` work like user classes.
_BUILTIN_EXC = [
    ("BaseException", None),
    ("Exception", "BaseException"),
    ("ArithmeticError", "Exception"),
    ("ZeroDivisionError", "ArithmeticError"),
    ("OverflowError", "ArithmeticError"),
    ("AssertionError", "Exception"),
    ("AttributeError", "Exception"),
    ("LookupError", "Exception"),
    ("IndexError", "LookupError"),
    ("KeyError", "LookupError"),
    ("NameError", "Exception"),
    ("OSError", "Exception"),
    ("IOError", "OSError"),
    ("RuntimeError", "Exception"),
    ("NotImplementedError", "RuntimeError"),
    ("RecursionError", "RuntimeError"),
    ("StopIteration", "Exception"),
    # SystemExit derives from BaseException, not Exception, exactly as in
    # CPython -- so `except Exception:` does not swallow an interpreter exit.
    ("SystemExit", "BaseException"),
    ("TypeError", "Exception"),
    ("ValueError", "Exception"),
]
_BUILTIN_EXC_BASE = {name: base for name, base in _BUILTIN_EXC}


def _needed_builtin_excs(tree):
    """Builtin exception names actually referenced (plus their ancestor chain,
    needed for `except`-matching), excluding any the program defines itself.
    Programs that never touch exceptions register none of these."""
    user_classes = set()
    used = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            user_classes.add(n.name)
        elif isinstance(n, ast.Name):
            used.add(n.id)
        elif (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "sys" and n.attr == "exit"):
            # ex_Call rewrites `sys.exit(n)` into `raise SystemExit(n)`, but
            # that rewrite happens during codegen -- after this scan. The name
            # never appears in the source, so register it from the call site
            # or the class is missing when the RAISE executes.
            used.add("SystemExit")
    needed = set()
    for nm in used:
        cur = nm
        while cur is not None and cur in _BUILTIN_EXC_BASE and cur not in user_classes:
            needed.add(cur)
            cur = _BUILTIN_EXC_BASE[cur]
    return [nm for nm, _ in _BUILTIN_EXC if nm in needed]

# Method ids (receiver passed as arg0). Distinct id band (100+) so do_builtin can
# tell a global builtin from a bound method; invoked through the same CALL path.
METHODS = {
    "append": 100, "pop": 101, "get": 102, "keys": 103, "values": 104,
    "items": 105, "add": 106, "split": 107, "join": 108, "strip": 109,
    "startswith": 110, "endswith": 111, "find": 112, "replace": 113,
    "upper": 114, "lower": 115,
    "extend": 116, "insert": 117, "index": 118, "count": 119,
    "update": 120, "setdefault": 121, "splitlines": 122, "rstrip": 123,
    "lstrip": 124, "isdigit": 125, "isupper": 126, "islower": 127,
    "isalpha": 128, "isalnum": 129,
    # set.discard / set.remove / list.remove. Absent from this table they were
    # not lowered to a builtin method at all and silently did nothing, so
    # `numreg.discard(r)` never cleared a register and the compiler kept
    # believing stale registers held numbers -- emitting ADD_NN/SUB_NN/GT_NN
    # where the generic op was required.
    "discard": 130, "remove": 131,
}


class CompileError(Exception):
    pass


# The compiler packs free-operand hints into the high bits of `a` (bit 30 = free
# reg c, bit 29 = free reg b). The native interpreter stores each instruction as a
# packed 8-byte bit-field struct whose JSON decoder fills fields by name, so we emit
# the already-split ra/fb/fc here. `a` is kept too: the pure-Python ref VM and the
# disassembler read it (and do their own strip), so they need no changes.
_FREE_C = 1073741824        # bit 30
_FREE_B = 536870912         # bit 29


def _instr_json(op, a, b, c):
    ra = a
    fc = 0
    fb = 0
    if ra >= _FREE_C:
        fc = 1
        ra -= _FREE_C
    if ra >= _FREE_B:
        fb = 1
        ra -= _FREE_B
    return {"op": op, "a": a, "b": b, "c": c, "ra": ra, "fb": fb, "fc": fc}



# --- constant interning ----------------------------------------------------
class _Consts:
    def __init__(self):
        self.items = []          # list of (kind, payload)
        self._index = {}

    def add(self, kind, payload):
        key = (kind, payload)
        if key in self._index:
            return self._index[key]
        i = len(self.items)
        self.items.append((kind, payload))
        self._index[key] = i
        return i

    def as_json(self):
        out = []
        for kind, payload in self.items:
            # one uniform record; unused slots zeroed so the POD decoder is trivial
            rec = {"t": kind, "i": 0, "d": 0.0, "s": ""}
            if kind == "int":
                rec["i"] = payload
            elif kind == "float":
                rec["d"] = payload
            elif kind in ("str",):
                rec["s"] = payload
            elif kind == "bool":
                rec["i"] = 1 if payload else 0
            elif kind == "none":
                pass
            elif kind == "func":
                rec["i"] = payload          # func index
            elif kind == "builtin":
                rec["i"] = payload          # builtin id
            elif kind == "class":
                rec["i"] = payload          # class id
            out.append(rec)
        return out


# --- a function being compiled --------------------------------------------
class _Frame:
    def __init__(self, name, params, extra_locals, is_module):
        self.name = name
        self.is_module = is_module
        self.locals = {}                 # name -> fixed reg (functions only)
        # params occupy regs 0..nparams-1, other locals follow; module scope has
        # no fixed locals (all names are globals)
        ordered = list(params)
        for nm in (extra_locals or []):
            if nm not in self.locals and nm not in ordered:
                ordered.append(nm)
        for nm in ordered:
            self.locals[nm] = len(self.locals)
        self.nparams = len(params)
        self.vararg = -1                 # reg index of a *args param, or -1
        self.base = len(self.locals)     # temps start above locals
        self.top = self.base             # register stack pointer
        self.maxreg = self.base
        self.code = []                   # list of [op, a, b, c]
        self.numreg = set()              # regs statically known to hold a number
        self.vartype = {}                # name -> 'tintlist'/'listtint' (typed)
        self.reclaimable_locals = set()  # locals whose old value may be freed on reassign
        self.params = set(params)        # parameter names (never reclaimable: alias args)
        self.param_order = list(params)  # parameter names in positional order (kw binding)
        self.defaults = []               # per-param const index, -1 = required

    # register stack discipline
    def push(self):
        r = self.top
        self.top += 1
        if self.top > self.maxreg:
            self.maxreg = self.top
        return r

    def pop_to(self, mark):
        # forget numeric facts about freed temps
        for r in range(mark, self.top):
            self.numreg.discard(r)
        self.top = mark
        # A kept register at mark-1 (e.g. the result of an *empty* container
        # literal, whose destination was never push()ed) must be counted, or the
        # frame is allocated too few registers and BUILD_LIST/etc. overflow.
        if mark > self.maxreg:
            self.maxreg = mark

    def emit(self, op, a=0, b=0, c=0):
        self.code.append([OPS[op], a, b, c])


class Compiler:
    def __init__(self):
        self.consts = _Consts()
        self.gnames = {}                 # global name -> slot
        self.funcs = []                  # list of _Frame (entry = 0)
        self._pending = []               # (FunctionDef, frame) to compile
        self.func_names = {}             # module-level def name -> fidx (direct call)
        self.reassigned_names = set()    # names rebound by non-def statements
        self.classes = []                # list of {name, base, methods:[...]}
        self._class_id = {}              # class name -> id
        self.reclaimable_globals = set()

    # --- name/slot helpers ---
    def gslot(self, name):
        if name not in self.gnames:
            self.gnames[name] = len(self.gnames)
        return self.gnames[name]

    # ---- entry ----
    # --- escape analysis: which module globals may free their old value ---
    # A scalar global accumulator (total, loop counter, ...) discards its old
    # value on every reassignment. That value is safe to reclaim only if the
    # global never escapes: never aliased to another name, returned, stored into
    # a container/attribute/another global, built into a container, or passed to
    # a call that might retain it (print/len are known read-only exceptions).
    # Uses outside those positions -- arithmetic/comparison operands, conditions,
    # subscript indices -- only read the value and are safe. The analysis is
    # conservative (any unrecognised position is treated as an escape) and the
    # runtime _free_v gate is an independent backstop (only large ints/floats are
    # ever actually recycled), so a missed case cannot corrupt a live binding.
    # Builtins that inspect an argument without keeping a reference to it, so
    # passing a name to one does not make that name escape. This is the gate on
    # whether a global/local can have its old value reclaimed on reassignment,
    # and it was previously just print/len -- which meant a single `str(acc)`
    # anywhere in the module disqualified `acc` for the whole program and every
    # `acc = acc + 1` leaked its previous V. Each name here was checked against
    # its do_builtin implementation: all of them read the argument and return a
    # fresh scalar or a class id, none store it.
    #
    # Deliberately absent: list/tuple/dict/set/sorted/min/max/sum. They do not
    # retain the *argument* either, but they return values that alias its
    # elements, which is a different question from the one this gate answers and
    # not worth the risk here.
    _READONLY_BUILTINS = ("print", "len", "str", "repr", "int", "float",
                          "bool", "abs", "ord", "chr", "type", "isinstance",
                          "hasattr")

    @staticmethod
    def _scope_locals(fnode):
        loc = set()
        for a in fnode.args.args:
            loc.add(a.arg)
        for n in ast.walk(fnode):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        loc.add(t.id)
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        for e in t.elts:
                            if isinstance(e, ast.Name):
                                loc.add(e.id)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                loc.add(n.target.id)
            elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
                loc.add(n.target.id)
        return loc

    def _compute_reclaimable_globals(self, tree):
        module_assigned = set()

        def collect(stmts):
            for n in stmts:
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            module_assigned.add(t.id)
                        elif isinstance(t, (ast.Tuple, ast.List)):
                            for e in t.elts:
                                if isinstance(e, ast.Name):
                                    module_assigned.add(e.id)
                elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                    module_assigned.add(n.target.id)
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                        and n.value is not None:
                    module_assigned.add(n.target.id)
                elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                    module_assigned.add(n.name)         # def name; don't recurse in
                elif isinstance(n, (ast.If, ast.While, ast.For)):
                    if isinstance(n, ast.For) and isinstance(n.target, ast.Name):
                        module_assigned.add(n.target.id)
                    collect(n.body); collect(getattr(n, "orelse", []))
        collect(tree.body)
        escaped = set()
        not_fresh = set()        # globals that ever receive a non-fresh (aliasable) value

        # A reclaimable global must, at every assignment, receive a freshly built,
        # uniquely-owned value -- the result of arithmetic, or a small cached int
        # constant the free gate will never recycle. Anything that can alias an
        # existing object (another name, a container element via subscript, an
        # attribute, a call result, a container literal, a for/unpack target, or a
        # large/float/str constant that may be a shared memoised const) disqualifies
        # it, because freeing its old value on reassignment could free a value some
        # other binding still holds.
        def fresh_val(value):
            if isinstance(value, (ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp)):
                return True
            if isinstance(value, ast.Constant):
                v = value.value
                if v is None or isinstance(v, bool):
                    return True
                return isinstance(v, int) and -8 <= v <= 256
            return False

        def is_glob(name, loc):
            return name not in loc and name in module_assigned

        def mark(node, escaping, loc):
            if isinstance(node, ast.Name):
                if escaping and is_glob(node.id, loc):
                    escaped.add(node.id)
            elif isinstance(node, ast.BinOp):
                mark(node.left, False, loc); mark(node.right, False, loc)
            elif isinstance(node, ast.UnaryOp):
                mark(node.operand, False, loc)
            elif isinstance(node, ast.BoolOp):
                for v in node.values:
                    mark(v, False, loc)
            elif isinstance(node, ast.Compare):
                mark(node.left, False, loc)
                for c in node.comparators:
                    mark(c, False, loc)
            elif isinstance(node, ast.IfExp):
                mark(node.test, False, loc)
                mark(node.body, escaping, loc); mark(node.orelse, escaping, loc)
            elif isinstance(node, ast.Call):
                ro = (isinstance(node.func, ast.Name)
                      and node.func.id in self._READONLY_BUILTINS)
                if not ro:
                    mark(node.func, True, loc)       # method/func base may retain
                for a in node.args:
                    mark(a, not ro, loc)
            elif isinstance(node, ast.Subscript):
                mark(node.value, True, loc)          # used as a container
                mark(node.slice, False, loc)
            elif isinstance(node, ast.Attribute):
                mark(node.value, True, loc)          # used as an object
            elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                for e in node.elts:
                    mark(e, True, loc)
            elif isinstance(node, ast.Dict):
                for k in node.keys:
                    if k is not None:
                        mark(k, True, loc)
                for v in node.values:
                    mark(v, True, loc)
            elif isinstance(node, ast.Index):           # py<3.9 slice wrapper
                mark(node.value, False, loc)

        def visit(stmts, loc):
            for s in stmts:
                if isinstance(s, ast.Assign):
                    mark(s.value, True, loc)            # value binds to target
                    fresh = fresh_val(s.value)
                    for t in s.targets:
                        if isinstance(t, ast.Name):
                            if not fresh and is_glob(t.id, loc):
                                not_fresh.add(t.id)
                        elif isinstance(t, (ast.Tuple, ast.List)):
                            for e in t.elts:               # unpack target aliases element
                                if isinstance(e, ast.Name) and is_glob(e.id, loc):
                                    not_fresh.add(e.id)
                        elif isinstance(t, ast.Subscript):
                            mark(t.value, True, loc)
                            mark(t.slice, True, loc)       # may be a retained dict key
                        elif isinstance(t, ast.Attribute):
                            mark(t.value, True, loc)
                elif isinstance(s, ast.AnnAssign):
                    if s.value is not None:
                        mark(s.value, True, loc)
                        if isinstance(s.target, ast.Name):
                            if not fresh_val(s.value) and is_glob(s.target.id, loc):
                                not_fresh.add(s.target.id)
                        elif isinstance(s.target, ast.Subscript):
                            mark(s.target.value, True, loc); mark(s.target.slice, True, loc)
                        elif isinstance(s.target, ast.Attribute):
                            mark(s.target.value, True, loc)
                elif isinstance(s, ast.AugAssign):
                    mark(s.value, False, loc)              # g += x  is fresh (g = g + x)
                    if isinstance(s.target, ast.Subscript):
                        mark(s.target.value, True, loc); mark(s.target.slice, True, loc)
                    elif isinstance(s.target, ast.Attribute):
                        mark(s.target.value, True, loc)
                elif isinstance(s, ast.Return):
                    if s.value is not None:
                        mark(s.value, True, loc)
                elif isinstance(s, ast.Expr):
                    mark(s.value, False, loc)
                elif isinstance(s, (ast.If, ast.While)):
                    mark(s.test, False, loc)
                    visit(s.body, loc); visit(s.orelse, loc)
                elif isinstance(s, ast.For):
                    mark(s.iter, True, loc)
                    if isinstance(s.target, ast.Name) and is_glob(s.target.id, loc):
                        not_fresh.add(s.target.id)         # aliases iterated elements
                    elif isinstance(s.target, (ast.Tuple, ast.List)):
                        for e in s.target.elts:
                            if isinstance(e, ast.Name) and is_glob(e.id, loc):
                                not_fresh.add(e.id)
                    visit(s.body, loc); visit(s.orelse, loc)
                elif isinstance(s, ast.FunctionDef):
                    visit(s.body, self._scope_locals(s))
                elif isinstance(s, ast.ClassDef):
                    for item in s.body:
                        if isinstance(item, ast.FunctionDef):
                            visit(item.body, self._scope_locals(item))

        visit(tree.body, set())
        return module_assigned - escaped - not_fresh - set(BUILTINS.keys())

    def compile_module(self, tree, source_name="<module>"):
        self.reclaimable_globals = self._compute_reclaimable_globals(tree)
        self.reassigned_names = self._compute_reassigned_names(tree)
        mod = _Frame(source_name, [], [], is_module=True)
        mod.vartype = self._infer_types(tree.body)
        self.funcs.append(mod)
        # pre-bind builtins into globals so `print` etc. resolve as names
        for bname, bid in BUILTINS.items():
            ci = self.consts.add("builtin", bid)
            r = mod.push()
            mod.emit("LOAD_CONST", r, ci)
            mod.emit("STORE_GLOBAL", r, self.gslot(bname))
            mod.pop_to(mod.base)
        # bind __name__ = "__main__" for the entry module so the script-entry
        # idiom `if __name__ == "__main__":` fires (minipy links everything into
        # one program, so there is a single module namespace = the entry).
        ci = self.consts.add("str", "__main__")
        r = mod.push()
        mod.emit("LOAD_CONST", r, ci)
        mod.emit("STORE_GLOBAL", r, self.gslot("__name__"))
        mod.pop_to(mod.base)
        # materialise referenced builtin exception classes (parents first) so
        # they exist before any user `class Err(Exception)` / raise / except.
        for bname in _needed_builtin_excs(tree):
            base_name = _BUILTIN_EXC_BASE[bname]
            base = self._class_id.get(base_name, -1) if base_name else -1
            cid = len(self.classes)
            self._class_id[bname] = cid
            self.classes.append({"name": bname, "base": base, "methods": []})
            ci = self.consts.add("class", cid)
            r = mod.push()
            mod.emit("LOAD_CONST", r, ci)
            mod.emit("STORE_GLOBAL", r, self.gslot(bname))
            mod.pop_to(mod.base)
        for stmt in tree.body:
            self.stmt(mod, stmt)
        mod.emit("RETURN", self._const_reg(mod, ("none", None)))
        # compile any function bodies queued during module lowering
        while self._pending:
            node, frame = self._pending.pop(0)
            self._compile_func_body(node, frame)
        return self.to_program(source_name)

    def _const_reg(self, f, kp):
        ci = self.consts.add(*kp)
        r = f.push()
        f.emit("LOAD_CONST", r, ci)
        if kp[0] in ("int", "float", "bool"):
            f.numreg.add(r)
        return r

    # ===================== statements =====================
    def stmt(self, f, node):
        m = getattr(self, "st_" + type(node).__name__, None)
        if m is None:
            raise CompileError("unsupported statement: %s (line %s)"
                               % (type(node).__name__, getattr(node, "lineno", "?")))
        m(f, node)

    def st_Expr(self, f, node):
        r = self.expr(f, node.value)
        f.pop_to(r)                       # discard value

    def st_Pass(self, f, node):
        pass

    def st_Import(self, f, node):
        # minipy provides its supported modules (sys/os/re) directly at runtime;
        # the import statement itself is a recognized no-op. CPython-only modules
        # (e.g. pickle) are expected to be used only inside branches gated by a
        # `sys.implementation.name` check, which folds away before this matters.
        pass

    def st_ImportFrom(self, f, node):
        pass

    def st_Global(self, f, node):
        # The declared names were excluded from this frame's locals by
        # _collect_locals, so loads/stores already route to the global slot.
        # Nothing to emit at the point of declaration.
        pass

    def st_Nonlocal(self, f, node):
        pass

    def st_Assert(self, f, node):
        # v0: the test is evaluated (so any side effects occur) but a failing
        # assertion does not raise (no AssertionError type yet). Invariant-style
        # asserts that hold behave identically to CPython.
        r = self.expr(f, node.test)
        f.pop_to(r)

    def _reclaimable_locals(self, func_node, frame):
        # Per-function mirror of _compute_reclaimable_globals: a local may have its
        # old value freed on reassignment only if every assignment to it is a freshly
        # built, uniquely-owned value (arithmetic or a cached-small-int constant) and
        # it never escapes (aliased to another name, returned, stored, passed to a
        # retaining call, used as a container/attribute base, or bound by a for/unpack
        # target). Parameters are always excluded -- they alias the caller's argument.
        assigned = set(); escaped = set(); not_fresh = set()

        def fresh_val(value):
            if isinstance(value, (ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp)):
                return True
            if isinstance(value, ast.Constant):
                v = value.value
                if v is None or isinstance(v, bool):
                    return True
                if isinstance(v, int) and -8 <= v <= 256:
                    return True
            return False

        def mark(node, escaping):
            if isinstance(node, ast.Name):
                if escaping:
                    escaped.add(node.id)
            elif isinstance(node, ast.BinOp):
                mark(node.left, False); mark(node.right, False)
            elif isinstance(node, ast.UnaryOp):
                mark(node.operand, False)
            elif isinstance(node, ast.BoolOp):
                for v in node.values:
                    mark(v, False)
            elif isinstance(node, ast.Compare):
                mark(node.left, False)
                for c in node.comparators:
                    mark(c, False)
            elif isinstance(node, ast.IfExp):
                mark(node.test, False); mark(node.body, escaping); mark(node.orelse, escaping)
            elif isinstance(node, ast.Call):
                ro = isinstance(node.func, ast.Name) and node.func.id in self._READONLY_BUILTINS
                if not ro:
                    mark(node.func, True)
                for a in node.args:
                    mark(a, not ro)
                for k in node.keywords:
                    mark(k.value, not ro)
            elif isinstance(node, ast.Subscript):
                mark(node.value, True); mark(node.slice, False)   # read index: not retained
            elif isinstance(node, ast.Attribute):
                mark(node.value, True)
            elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                for e in node.elts:
                    mark(e, True)
            elif isinstance(node, ast.Dict):
                for k in node.keys:
                    if k is not None:
                        mark(k, True)
                for v in node.values:
                    mark(v, True)

        def visit(stmts):
            for s in stmts:
                if isinstance(s, ast.Assign):
                    mark(s.value, True)
                    fresh = fresh_val(s.value)
                    for t in s.targets:
                        if isinstance(t, ast.Name):
                            assigned.add(t.id)
                            if not fresh:
                                not_fresh.add(t.id)
                        elif isinstance(t, (ast.Tuple, ast.List)):
                            for e in t.elts:
                                if isinstance(e, ast.Name):
                                    assigned.add(e.id); not_fresh.add(e.id)
                        elif isinstance(t, ast.Subscript):
                            mark(t.value, True); mark(t.slice, True)   # store index may be a dict key
                        elif isinstance(t, ast.Attribute):
                            mark(t.value, True)
                elif isinstance(s, ast.AnnAssign):
                    if s.value is not None:
                        mark(s.value, True)
                        if isinstance(s.target, ast.Name):
                            assigned.add(s.target.id)
                            if not fresh_val(s.value):
                                not_fresh.add(s.target.id)
                elif isinstance(s, ast.AugAssign):
                    mark(s.value, False)
                    if isinstance(s.target, ast.Name):
                        assigned.add(s.target.id)                  # g op= x is fresh
                    elif isinstance(s.target, ast.Subscript):
                        mark(s.target.value, True); mark(s.target.slice, True)
                    elif isinstance(s.target, ast.Attribute):
                        mark(s.target.value, True)
                elif isinstance(s, ast.Return):
                    if s.value is not None:
                        mark(s.value, True)
                elif isinstance(s, ast.Expr):
                    mark(s.value, False)
                elif isinstance(s, (ast.If, ast.While)):
                    mark(s.test, False)
                    visit(s.body); visit(s.orelse)
                elif isinstance(s, ast.For):
                    mark(s.iter, True)
                    if isinstance(s.target, ast.Name):
                        assigned.add(s.target.id); not_fresh.add(s.target.id)
                    elif isinstance(s.target, (ast.Tuple, ast.List)):
                        for e in s.target.elts:
                            if isinstance(e, ast.Name):
                                assigned.add(e.id); not_fresh.add(e.id)
                    visit(s.body); visit(s.orelse)
                # nested FunctionDef/ClassDef: their bodies are a separate scope

        visit(func_node.body)
        return (assigned - escaped - not_fresh - frame.params) & set(frame.locals.keys())

    def _try_acc_local(self, f, tgt, value):
        # Fuse `loc = loc + <pure expr>` / `loc = loc - <pure expr>` into a single
        # opcode for a reclaimable local: read the local's register, combine, write
        # back, and reclaim the old value -- the local mirror of ACC_ADD_G. The fused
        # op reads the local after evaluating the rhs, so (as for globals) the rhs must
        # be call-free to preserve CPython's left-to-right order.
        if f.is_module:
            return False
        if tgt.id not in f.locals or tgt.id not in f.reclaimable_locals:
            return False
        if not (isinstance(value, ast.BinOp) and isinstance(value.op, (ast.Add, ast.Sub))):
            return False
        if not (isinstance(value.left, ast.Name) and value.left.id == tgt.id):
            return False
        rhs = value.right
        if self._has_call(rhs):
            return False
        r = self.expr(f, rhs)
        fr = 1 if self._is_fresh_arith(rhs) else 0
        opn = "ACC_ADD_L" if isinstance(value.op, ast.Add) else "ACC_SUB_L"
        f.emit(opn, f.locals[tgt.id], r, fr)
        r_num = r in f.numreg
        f.pop_to(r)
        lr = f.locals[tgt.id]
        if not (lr in f.numreg and r_num):           # result numeric only if both were
            f.numreg.discard(lr)
        return True

    def _try_acc_gint(self, f, tgt, value):
        # Fuse `g = g + tlist[idx]` into one opcode when g is a reclaimable global
        # and tlist is a typed int list. Collapses load-global + typed-index + add
        # + store-global(reclaim) -- the whole inner step of a numeric reduction --
        # into a single dispatch. Only the typed source takes this path; CPython
        # runs the same code unchanged.
        if f.is_module is False and tgt.id in f.locals:
            return False
        if tgt.id not in self.reclaimable_globals:
            return False
        if not (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add)):
            return False
        if not (isinstance(value.left, ast.Name) and value.left.id == tgt.id):
            return False
        rhs = value.right
        # g += tlist[idx]
        if isinstance(rhs, ast.Subscript) \
                and self._type_of(f, rhs.value) == "tintlist":
            ra = self.expr(f, rhs.value)
            rk = self.expr(f, rhs.slice)
            f.emit("ACC_ADD_GINT", self.gslot(tgt.id), ra, rk)
            f.pop_to(ra)
            return True
        # g += tA[i][k] * tB[k][j]  (multiply-accumulate; rows packed with indices)
        if isinstance(rhs, ast.BinOp) and isinstance(rhs.op, ast.Mult) \
                and isinstance(rhs.left, ast.Subscript) \
                and isinstance(rhs.right, ast.Subscript) \
                and self._type_of(f, rhs.left.value) == "tintlist" \
                and self._type_of(f, rhs.right.value) == "tintlist":
            rar = self.expr(f, rhs.left.value)
            rk = self.expr(f, rhs.left.slice)
            rbr = self.expr(f, rhs.right.value)
            rj = self.expr(f, rhs.right.slice)
            if rar < 4096 and rk < 4096 and rbr < 4096 and rj < 4096:
                f.emit("ACC_MAC_GINT", self.gslot(tgt.id),
                       rar * 4096 + rk, rbr * 4096 + rj)
                f.pop_to(rar)
                return True
            f.pop_to(rar)
        # g = g + <any pure expression>  (general accumulate). The fused opcode
        # reads g *after* the right-hand side is evaluated, so this is only valid
        # when the rhs cannot mutate g -- i.e. contains no calls. That covers every
        # arithmetic accumulator while preserving CPython's left-to-right order.
        if not self._has_call(rhs):
            r = self.expr(f, rhs)
            fr = 1 if self._is_fresh_arith(rhs) else 0
            f.emit("ACC_ADD_G", self.gslot(tgt.id), r, fr)
            f.pop_to(r)
            return True
        return False

    @staticmethod
    def _has_call(node):
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                return True
        return False

    def st_Assign(self, f, node):
        if len(node.targets) != 1:
            # chained `a = b = value`: evaluate the value once, store into each
            rval = self.expr(f, node.value)
            for tgt in node.targets:
                self._store_target_reg(f, tgt, rval)
            f.pop_to(rval)
            return
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name):
            if self._try_acc_gint(f, tgt, node.value):
                return
            if self._try_acc_local(f, tgt, node.value):
                return
            r = self.expr(f, node.value)
            self._store_name(f, tgt.id, r)
            f.pop_to(r)
            return
        if isinstance(tgt, ast.Attribute):
            robj = self.expr(f, tgt.value)
            rval = self.expr(f, node.value)
            cn = self.consts.add("str", tgt.attr)
            f.emit("STORE_ATTR", robj, rval, cn)
            f.pop_to(robj)
            return
        if isinstance(tgt, ast.Subscript):
            if isinstance(tgt.slice, ast.Slice):
                sl = tgt.slice
                if sl.step is not None:
                    raise CompileError("v0: step in slice assignment unsupported "
                                       "(line %s)" % node.lineno)
                rseq = self.expr(f, tgt.value)                   # seq at rseq
                if sl.lower is None:
                    rlo = self._const_reg(f, ("none", None))
                else:
                    rlo = self.expr(f, sl.lower)
                assert rlo == rseq + 1
                if sl.upper is None:
                    rhi = self._const_reg(f, ("none", None))
                else:
                    rhi = self.expr(f, sl.upper)
                assert rhi == rseq + 2
                rval = self.expr(f, node.value)                 # value at rseq+3
                assert rval == rseq + 3
                f.emit("SLICE_STORE", rseq, rseq + 1, 0)
                f.pop_to(rseq)
                return
            robj = self.expr(f, tgt.value)
            ridx = self.expr(f, tgt.slice)
            rval = self.expr(f, node.value)
            op = "SETINDEX_INT" if self._type_of(f, tgt.value) == "tintlist" else "SETINDEX"
            f.emit(op, robj, ridx, rval)
            f.pop_to(robj)
            return
        if isinstance(tgt, (ast.Tuple, ast.List)):
            # a, b = <iterable>: index the (materialised) value positionally, then
            # store each element into its target (Name / attr / subscript / nested)
            rseq = self.expr(f, node.value)
            for i, elt in enumerate(tgt.elts):
                ridx = self._const_reg(f, ("int", i))
                f.emit("INDEX", ridx, rseq, ridx)
                self._store_target_reg(f, elt, ridx)
                f.pop_to(ridx)
            f.pop_to(rseq)
            return
        raise CompileError("v0 assign: unsupported target %s (line %s)"
                           % (type(tgt).__name__, node.lineno))

    def _store_target_reg(self, f, tgt, rval):
        # store the value already in register `rval` into an assignment target;
        # supports Name, Attribute, Subscript, and nested Tuple/List unpacking.
        if isinstance(tgt, ast.Name):
            self._store_name(f, tgt.id, rval)
            return
        if isinstance(tgt, ast.Attribute):
            robj = self.expr(f, tgt.value)
            cn = self.consts.add("str", tgt.attr)
            f.emit("STORE_ATTR", robj, rval, cn)
            f.pop_to(robj)
            return
        if isinstance(tgt, ast.Subscript):
            if isinstance(tgt.slice, ast.Slice):
                raise CompileError("v0: slice assignment unsupported")
            robj = self.expr(f, tgt.value)
            ridx = self.expr(f, tgt.slice)
            op = "SETINDEX_INT" if self._type_of(f, tgt.value) == "tintlist" else "SETINDEX"
            f.emit(op, robj, ridx, rval)
            f.pop_to(robj)
            return
        if isinstance(tgt, (ast.Tuple, ast.List)):
            for i, elt in enumerate(tgt.elts):
                ridx = self._const_reg(f, ("int", i))
                f.emit("INDEX", ridx, rval, ridx)
                self._store_target_reg(f, elt, ridx)
                f.pop_to(ridx)
            return
        raise CompileError("v0 assign: unsupported target %s"
                           % type(tgt).__name__)

    def st_AnnAssign(self, f, node):
        # `x: T = v` (annotation ignored); bare `x: T` is a no-op declaration
        if node.value is None:
            return
        if isinstance(node.target, ast.Name):
            r = self.expr(f, node.value)
            self._store_name(f, node.target.id, r)
            f.pop_to(r)
            return
        if isinstance(node.target, ast.Attribute):
            robj = self.expr(f, node.target.value)
            rval = self.expr(f, node.value)
            cn = self.consts.add("str", node.target.attr)
            f.emit("STORE_ATTR", robj, rval, cn)
            f.pop_to(robj)
            return
        raise CompileError("v0 annassign: Name/Attribute target only (line %s)"
                           % node.lineno)

    def st_AugAssign(self, f, node):
        if isinstance(node.target, ast.Name):
            name = node.target.id
            rb = self._load_name(f, name)
            rc = self.expr(f, node.value)
            self._binop(f, node.op, rb, rb, rc)
            f.pop_to(rb + 1)
            self._store_name(f, name, rb)
            f.pop_to(rb)
            return
        # `obj.attr op= val` / `obj[idx] op= val`: evaluate the target object
        # (and index) exactly once, load the current value, apply the op, and
        # store back through the same object/index registers.
        if isinstance(node.target, ast.Attribute):
            robj = self.expr(f, node.target.value)
            cn = self.consts.add("str", node.target.attr)
            rcur = f.push()
            f.emit("LOAD_ATTR", rcur, robj, cn)
            rval = self.expr(f, node.value)
            self._binop(f, node.op, rcur, rcur, rval)
            f.emit("STORE_ATTR", robj, rcur, cn)
            f.pop_to(robj)
            return
        if isinstance(node.target, ast.Subscript):
            if isinstance(node.target.slice, ast.Slice):
                raise CompileError(
                    "v0: slice aug-assign unsupported (line %s)" % node.lineno)
            robj = self.expr(f, node.target.value)
            ridx = self.expr(f, node.target.slice)
            rcur = f.push()
            f.emit("INDEX", rcur, robj, ridx)
            rval = self.expr(f, node.value)
            self._binop(f, node.op, rcur, rcur, rval)
            f.emit("SETINDEX", robj, ridx, rcur)
            f.pop_to(robj)
            return
        raise CompileError(
            "v0 augassign: Name/Attribute/Subscript target only (line %s)"
            % node.lineno)

    def st_Return(self, f, node):
        if node.value is None:
            r = self._const_reg(f, ("none", None))
        else:
            r = self.expr(f, node.value)
        if self._finallys:                     # run every enclosing finally first
            self._run_pending_finallys(f, len(self._finallys))
        f.emit("RETURN", r)
        f.pop_to(r)

    def _emit_branch_false(self, f, test):
        # Emit jump(s) taken when `test` is FALSE; return the list of jump
        # instruction indices, each patched at field [2] to the false target.
        # `a and b and ...` short-circuits: any false operand jumps to the target,
        # so it lowers to one (fused) branch per operand with no boolean built.
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            jfs = []
            for v in test.values:
                jfs.extend(self._emit_branch_false(f, v))
            return jfs
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            opn = {ast.Lt: "JF_LT", ast.LtE: "JF_LE", ast.Gt: "JF_GT",
                   ast.GtE: "JF_GE", ast.Eq: "JF_EQ", ast.NotEq: "JF_NE"}.get(type(test.ops[0]))
            if opn is not None:
                ra = self.expr(f, test.left)
                rc = self.expr(f, test.comparators[0])
                jf = len(f.code); f.emit(opn, ra, 0, rc)   # a=left, b=target, c=right
                f.pop_to(ra)
                return [jf]
        rc = self.expr(f, test)
        jf = len(f.code); f.emit("JUMP_IF_FALSE", rc, 0)
        f.pop_to(rc)
        return [jf]

    @staticmethod
    def _is_simple_cmp(test):
        return (isinstance(test, ast.Compare) and len(test.ops) == 1
                and type(test.ops[0]) in (ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                                          ast.Eq, ast.NotEq))

    def _is_impl_name(self, node):
        # Matches the attribute chain `sys.implementation.name`.
        return (isinstance(node, ast.Attribute) and node.attr == "name"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "implementation"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "sys")

    def _const_impl_test(self, test):
        # Statically evaluate `sys.implementation.name ==/!= 'str'` as if the
        # running implementation is 'minipy'. Returns True/False when the test is
        # this recognized identity check, else None. This lets scripts guard
        # CPython-only code (pickle, etc.) behind
        #   if sys.implementation.name != 'minipy':
        # and have minipy drop that whole branch at compile time -- no bytecode,
        # so the branch may freely use features minipy cannot compile.
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1
                and len(test.comparators) == 1):
            return None
        left = test.left
        right = test.comparators[0]
        op = test.ops[0]
        if self._is_impl_name(left) and isinstance(right, ast.Constant) \
                and isinstance(right.value, str):
            val = right.value
        elif self._is_impl_name(right) and isinstance(left, ast.Constant) \
                and isinstance(left.value, str):
            val = left.value
        else:
            return None
        if isinstance(op, ast.Eq):
            return val == "minipy"
        if isinstance(op, ast.NotEq):
            return val != "minipy"
        return None

    def st_If(self, f, node):
        verdict = self._const_impl_test(node.test)
        if verdict is True:                    # branch taken on minipy
            for s in node.body:
                self.stmt(f, s)
            return
        if verdict is False:                   # CPython-only branch: skip entirely
            for s in node.orelse:
                self.stmt(f, s)
            return
        jfs = self._emit_branch_false(f, node.test)
        for s in node.body:
            self.stmt(f, s)
        if node.orelse:
            jend = len(f.code); f.emit("JUMP", 0)
            tgt = len(f.code)                 # false -> else
            for jf in jfs:
                f.code[jf][2] = tgt
            for s in node.orelse:
                self.stmt(f, s)
            f.code[jend][1] = len(f.code)
        else:
            tgt = len(f.code)
            for jf in jfs:
                f.code[jf][2] = tgt

    def _emit_branch_true(self, f, test, target):
        # Emit a jump taken when `test` is TRUE, to a known (backward) target.
        # Mirror of _emit_branch_false; used for the rotated-loop back-edge.
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            opn = {ast.Lt: "JT_LT", ast.LtE: "JT_LE", ast.Gt: "JT_GT",
                   ast.GtE: "JT_GE", ast.Eq: "JT_EQ", ast.NotEq: "JT_NE"}.get(type(test.ops[0]))
            if opn is not None:
                ra = self.expr(f, test.left)
                rc = self.expr(f, test.comparators[0])
                f.emit(opn, ra, target, rc)            # a=left, b=target, c=right
                f.pop_to(ra)
                return
        rc = self.expr(f, test)
        f.emit("JUMP_IF_TRUE", rc, target)
        f.pop_to(rc)

    def st_While(self, f, node):
        if self._is_simple_cmp(node.test):
            # Loop rotation: guard test at entry, body, then a copy of the test as a
            # conditional back-edge -- one fused branch per iteration, no JUMP.
            guards = self._emit_branch_false(f, node.test)   # false at entry -> end
            top = len(f.code)
            loop = _Loop(top); loop.rotated = True
            self._loops.append(loop)
            for s in node.body:
                self.stmt(f, s)
            cont = len(f.code)
            self._emit_branch_true(f, node.test, top)        # back-edge: true -> top
            end = len(f.code)
            for jf in guards:
                f.code[jf][2] = end
            self._loops.pop()
            for pc in loop.breaks:
                f.code[pc][1] = end
            for pc in loop.conts:
                f.code[pc][1] = cont
        else:
            # Compound/other test: top-test form. `and` still short-circuits into
            # one branch per operand (no boolean built); continue re-tests at top.
            top = len(f.code)
            jfs = self._emit_branch_false(f, node.test)
            loop = _Loop(top)
            self._loops.append(loop)
            for s in node.body:
                self.stmt(f, s)
            f.emit("JUMP", top)
            end = len(f.code)
            for jf in jfs:
                f.code[jf][2] = end
            self._loops.pop()
            for pc in loop.breaks:
                f.code[pc][1] = end

    def st_For(self, f, node):
        tgt = node.target
        is_range = (isinstance(node.iter, ast.Call)
                    and isinstance(node.iter.func, ast.Name)
                    and node.iter.func.id == "range")
        if is_range:
            if not isinstance(tgt, ast.Name):
                raise CompileError("v0 for-range: Name target only (line %s)" % node.lineno)
            self._for_range(f, tgt.id, node)
            return
        if not isinstance(tgt, (ast.Name, ast.Tuple, ast.List)):
            raise CompileError("v0 for: Name/tuple target only (line %s)" % node.lineno)
        # general iterable: ITER_NEW once, ITER_NEXT per turn (target = loop end)
        rit = self.expr(f, node.iter)
        f.emit("ITER_NEW", rit, rit)         # rit now holds the iterator
        f.numreg.discard(rit)
        top = len(f.code)
        rx = f.push()
        nx = len(f.code); f.emit("ITER_NEXT", rx, rit, 0)   # c patched to end
        if isinstance(tgt, ast.Name):
            self._store_name(f, tgt.id, rx)
            f.pop_to(rit + 1)
        else:                                # unpack tuple element into targets
            for i, elt in enumerate(tgt.elts):
                ridx = self._const_reg(f, ("int", i))
                f.emit("INDEX", ridx, rx, ridx)
                self._store_target_reg(f, elt, ridx)
                f.pop_to(ridx)
            f.pop_to(rit + 1)
        loop = _Loop(top); self._loops.append(loop)
        for s in node.body:
            self.stmt(f, s)
        f.emit("JUMP", top)
        end = len(f.code)
        f.code[nx][3] = end
        self._loops.pop()
        f.pop_to(rit)
        for pc in loop.breaks:
            f.code[pc][1] = end

    def _for_range(self, f, name, node):
        # `for <name> in range(...)`, lowered to a counter while-loop. Generic
        # ops here; the numeric fast-path upgrades the compare/increment.
        args = node.iter.args
        if len(args) == 1:
            start, stop, step = ast.Constant(0), args[0], ast.Constant(1)
        elif len(args) == 2:
            start, stop, step = args[0], args[1], ast.Constant(1)
        else:
            start, stop, step = args[0], args[1], args[2]
        rs = self.expr(f, start); self._store_name(f, name, rs); f.pop_to(rs)
        top = len(f.code)
        ri = self._load_name(f, name)
        rstop = self.expr(f, stop)
        f.emit("LT", ri, ri, rstop)
        self._maybe_num_cmp(f, ri)
        jf = len(f.code); f.emit("JUMP_IF_FALSE", ri, 0)
        f.pop_to(ri)
        loop = _Loop(top); self._loops.append(loop)
        for s in node.body:
            self.stmt(f, s)
        ri2 = self._load_name(f, name)
        rstep = self.expr(f, step)
        self._binop_op(f, "ADD", ri2, ri2, rstep)
        f.pop_to(ri2 + 1); self._store_name(f, name, ri2); f.pop_to(ri2)
        f.emit("JUMP", top)
        end = len(f.code); f.code[jf][2] = end
        self._loops.pop()
        for pc in loop.breaks:
            f.code[pc][1] = end

    def st_Break(self, f, node):
        if not self._loops:
            raise CompileError("break outside loop (line %s)" % node.lineno)
        nf = self._n_loop_finallys()
        if nf:
            self._run_pending_finallys(f, nf)
        pc = len(f.code); f.emit("JUMP", 0)
        self._loops[-1].breaks.append(pc)

    def st_Continue(self, f, node):
        if not self._loops:
            raise CompileError("continue outside loop (line %s)" % node.lineno)
        nf = self._n_loop_finallys()
        if nf:
            self._run_pending_finallys(f, nf)
        loop = self._loops[-1]
        if loop.rotated:                       # target (back-edge) is a forward ref
            pc = len(f.code); f.emit("JUMP", 0); loop.conts.append(pc)
        else:
            f.emit("JUMP", loop.top)

    def st_Try(self, f, node):
        if node.finalbody:
            self._compile_try_finally(f, node)
        else:
            self._compile_try_except(f, node)

    def _compile_try_finally(self, f, node):
        # try/finally with no new opcodes: an outer catch-all SETUP_EXCEPT runs
        # the finally then re-raises, the normal fall-through path runs a copy of
        # the finally, and return/break/continue inside the body run the pending
        # finallys (via self._finallys) before they exit.  try/except/finally is
        # handled by nesting the except logic inside this catch-all.
        fin = node.finalbody
        setup = len(f.code); f.emit("SETUP_EXCEPT", 0)
        self._finallys.append((fin, len(self._loops)))
        if node.handlers or node.orelse:
            inner = ast.Try(body=node.body, handlers=node.handlers,
                            orelse=node.orelse, finalbody=[])
            self._compile_try_except(f, inner)
        else:
            for s in node.body:
                self.stmt(f, s)
        f.emit("POP_BLOCK")
        self._finallys.pop()
        for s in fin:                          # normal completion
            self.stmt(f, s)
        endj = len(f.code); f.emit("JUMP", 0)
        f.code[setup][1] = len(f.code)         # exception dispatch
        for s in fin:                          # exception path: run finally...
            self.stmt(f, s)
        f.emit("RERAISE")                      # ...then propagate
        f.code[endj][1] = len(f.code)

    def _run_pending_finallys(self, f, n):
        # Inline the top `n` pending finally bodies, innermost first.  Each body
        # is compiled with itself (and any inner finallys) hidden from
        # self._finallys, so a return/break inside a finally runs only the
        # finallys still enclosing it -- and never recurses into itself.
        saved = self._finallys
        total = len(saved)
        k = total - 1
        while k >= total - n:
            self._finallys = saved[:k]
            for s in saved[k][0]:
                self.stmt(f, s)
            k = k - 1
        self._finallys = saved

    def _n_loop_finallys(self):
        # finallys nested inside the current innermost loop (those set up after
        # it was entered) -- the ones a break/continue must run before exiting.
        cur = len(self._loops)
        n = 0
        i = len(self._finallys) - 1
        while i >= 0 and self._finallys[i][1] >= cur:
            n = n + 1; i = i - 1
        return n

    def _compile_try_except(self, f, node):
        setup = len(f.code); f.emit("SETUP_EXCEPT", 0)
        for s in node.body:
            self.stmt(f, s)
        f.emit("POP_BLOCK")
        if node.orelse:
            for s in node.orelse:
                self.stmt(f, s)
        end_jumps = [len(f.code)]; f.emit("JUMP", 0)
        f.code[setup][1] = len(f.code)        # dispatch entry
        for h in node.handlers:
            jf = None
            if h.type is not None:
                rexc = f.push(); f.emit("LOAD_EXC", rexc)
                rcls = self.expr(f, h.type)
                assert rcls == rexc + 1
                f.emit("EXC_MATCH", rexc, rcls)
                f.pop_to(rexc + 1)
                jf = len(f.code); f.emit("JUMP_IF_FALSE", rexc, 0)
                f.pop_to(rexc)
            if h.name:
                re = f.push(); f.emit("LOAD_EXC", re)
                self._store_name(f, h.name, re); f.pop_to(re)
            for s in h.body:
                self.stmt(f, s)
            end_jumps.append(len(f.code)); f.emit("JUMP", 0)
            if jf is not None:
                f.code[jf][2] = len(f.code)   # type mismatch -> next handler
        f.emit("RERAISE")                     # nothing matched -> propagate
        after = len(f.code)
        for ej in end_jumps:
            f.code[ej][1] = after

    def st_Raise(self, f, node):
        if node.exc is None:
            f.emit("RERAISE")
            return
        # `raise X from Y`: minipy does not track exception chaining, so the
        # cause is accepted and ignored (the exception itself is raised as-is).
        r = self.expr(f, node.exc)
        f.emit("RAISE", r)
        f.pop_to(r)

    def st_ClassDef(self, f, node):
        if node.keywords or node.decorator_list:
            raise CompileError("v0 class: no keywords/decorators (line %s)" % node.lineno)
        base = -1
        if node.bases:
            if len(node.bases) != 1 or not isinstance(node.bases[0], ast.Name):
                raise CompileError("v0 class: single Name base only (line %s)" % node.lineno)
            base = self._class_id.get(node.bases[0].id, -1)
        cid = len(self.classes)
        self._class_id[node.name] = cid
        methods = []
        self.classes.append({"name": node.name, "base": base, "methods": methods})
        for item in node.body:
            if isinstance(item, ast.FunctionDef):
                fidx = self._compile_method(item)
                methods.append({"name": item.name, "func": fidx})
            # class-level (non-method) statements are ignored in v0
        ci = self.consts.add("class", cid)
        r = f.push(); f.emit("LOAD_CONST", r, ci)
        f.emit("STORE_GLOBAL", r, self.gslot(node.name)); f.pop_to(r)

    def _literal_const_index(self, node):
        # A unary-minus on a numeric literal (`-1`, `-2.5`) is a constant for
        # default-argument purposes: fold it to the negated literal.
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) \
                and isinstance(node.operand, ast.Constant) \
                and isinstance(node.operand.value, (int, float)) \
                and not isinstance(node.operand.value, bool):
            v = node.operand.value
            kind = "float" if isinstance(v, float) else "int"
            return self.consts.add(kind, -v)
        if not isinstance(node, ast.Constant):
            raise CompileError("v0 default: constant literal only (line %s)"
                               % getattr(node, "lineno", "?"))
        v = node.value
        if isinstance(v, bool):
            return self.consts.add("bool", v)     # bool before int (subclass)
        if isinstance(v, int):
            return self.consts.add("int", v)
        if isinstance(v, float):
            return self.consts.add("float", v)
        if isinstance(v, str):
            return self.consts.add("str", v)
        if v is None:
            return self.consts.add("none", None)
        raise CompileError("v0 default: unsupported literal %r" % (v,))

    def _build_defaults(self, node, nparams):
        # defaults apply to the trailing params; store one const index per param
        # (-1 = required). Restricting defaults to literals keeps them constant
        # (CPython evaluates defaults once at def time, which literals satisfy).
        defs = [-1] * nparams
        dlist = node.args.defaults
        start = nparams - len(dlist)
        for j, dexpr in enumerate(dlist):
            defs[start + j] = self._literal_const_index(dexpr)
        return defs

    def _compile_method(self, node):
        if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            raise CompileError("v0 method: no *args/**kwargs/kwonly (line %s)" % node.lineno)
        params = [a.arg for a in node.args.args]
        extra = _collect_locals(node)
        frame = _Frame(node.name, params, extra, is_module=False)
        frame.defaults = self._build_defaults(node, len(params))
        for arg in node.args.args:
            ann = _ann_name(arg.annotation)
            if ann in ("int", "float", "long", "double") and arg.arg in frame.locals:
                frame.numreg.add(frame.locals[arg.arg])
        idx = len(self.funcs)
        self.funcs.append(frame)
        self._pending.append((node, frame))
        return idx

    def st_FunctionDef(self, f, node):
        if not f.is_module:
            raise CompileError("v0: nested functions unsupported (line %s)" % node.lineno)
        params = [a.arg for a in node.args.args]
        if node.args.kwarg or node.args.kwonlyargs:
            raise CompileError("v0 def: no **kwargs/kwonly (line %s)" % node.lineno)
        va_index = -1
        if node.args.vararg:                     # def f(a, b, *rest): rest collects
            va_index = len(params)               # the trailing positional args
            params.append(node.args.vararg.arg)
        extra = _collect_locals(node)
        frame = _Frame(node.name, params, extra, is_module=False)
        frame.vararg = va_index
        frame.defaults = self._build_defaults(node, len(node.args.args))
        if va_index >= 0:
            frame.defaults.append(-1)            # *args param has no default
        # annotation-driven typing: int/float params start known-numeric, so
        # arithmetic on them lowers to the *_NN fast ops (py2c annotates heavily)
        for ai, arg in enumerate(node.args.args):
            ann = _ann_name(arg.annotation)
            if ann in ("int", "float", "long", "double") and arg.arg in frame.locals:
                frame.numreg.add(frame.locals[arg.arg])
        idx = len(self.funcs)
        self.funcs.append(frame)
        self.func_names[node.name] = idx
        self._pending.append((node, frame))
        # bind the function object into a global slot
        ci = self.consts.add("func", idx)
        r = f.push(); f.emit("LOAD_CONST", r, ci)
        f.emit("STORE_GLOBAL", r, self.gslot(node.name)); f.pop_to(r)

    # ---- optional type annotations: typed int lists -> specialised opcodes ----
    # `list[int]` / `list[int:N]` is a list whose elements are ints; `list[list
    # [int:N]]` is a matrix of such rows. When a value is statically known to be
    # one of these, subscripting emits INDEX_INT / SETINDEX_INT (no tag dispatch,
    # no negative-index fixup) and the loaded element is marked numeric so the
    # surrounding arithmetic takes the NN fast path. CPython ignores the
    # annotations entirely, so the typed source still runs there unchanged.
    def _ann_type(self, ann):
        if isinstance(ann, ast.Str):                    # string form: "list[int]"
            try:
                ann = ast.parse(ann.s, mode="eval").body
            except Exception:
                return None
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except Exception:
                return None
        if not (isinstance(ann, ast.Subscript)
                and isinstance(ann.value, ast.Name) and ann.value.id == "list"):
            return None
        elt = ann.slice
        if elt.__class__.__name__ == "Index":           # py<3.9 wrapper
            elt = elt.value
        if isinstance(elt, ast.Name) and elt.id == "int":
            return "tintlist"
        if isinstance(elt, ast.Slice):                   # list[int:N]
            if isinstance(elt.lower, ast.Name) and elt.lower.id == "int":
                return "tintlist"
            if self._ann_type(elt.lower) == "tintlist":  # list[list[int:n]:m]
                return "listtint"
        if isinstance(elt, ast.Subscript) and self._ann_type(elt) == "tintlist":
            return "listtint"
        return None

    def _infer_types(self, body):
        vt = {}
        appends = []      # (container_name, elem_name)
        copies = []       # (target_name, source_name)
        def scan(stmts):
            for s in stmts:
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                    t = self._ann_type(s.annotation)
                    if t:
                        vt[s.target.id] = t
                if isinstance(s, ast.Assign) and len(s.targets) == 1 \
                        and isinstance(s.targets[0], ast.Name) \
                        and isinstance(s.value, ast.Name):
                    copies.append((s.targets[0].id, s.value.id))
                if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
                    c = s.value
                    if isinstance(c.func, ast.Attribute) and c.func.attr == "append" \
                            and isinstance(c.func.value, ast.Name) and len(c.args) == 1 \
                            and isinstance(c.args[0], ast.Name):
                        appends.append((c.func.value.id, c.args[0].id))
                for fld in ("body", "orelse"):
                    sub = getattr(s, fld, None)
                    if isinstance(sub, list):
                        scan(sub)
        scan(body)
        elem = {"listtint": "tintlist"}
        changed = True
        while changed:
            changed = False
            for cont, e in appends:
                if vt.get(cont) in elem and vt.get(e) != elem[vt[cont]]:
                    vt[e] = elem[vt[cont]]; changed = True
            for tgt, src in copies:
                if src in vt and vt.get(tgt) != vt[src]:
                    vt[tgt] = vt[src]; changed = True
        return vt

    def _type_of(self, f, node):
        if isinstance(node, ast.Name):
            return f.vartype.get(node.id)
        if isinstance(node, ast.Subscript):
            base = self._type_of(f, node.value)
            if base == "listtint":
                return "tintlist"
            if base == "tintlist":
                return "int"
        return None

    def _compile_func_body(self, node, frame):
        self._loops = []
        self._finallys = []      # stack of (finalbody_stmts, loop_depth_at_setup)
        frame.vartype = self._infer_types(node.body)
        frame.reclaimable_locals = self._reclaimable_locals(node, frame)
        for s in node.body:
            self.stmt(frame, s)
        frame.emit("RETURN", self._const_reg(frame, ("none", None)))

    # ===================== expressions ====================
    # each returns the register holding the result, leaving f.top one above it
    def expr(self, f, node):
        m = getattr(self, "ex_" + type(node).__name__, None)
        if m is None:
            raise CompileError("unsupported expr: %s (line %s)"
                               % (type(node).__name__, getattr(node, "lineno", "?")))
        return m(f, node)

    def ex_Constant(self, f, node):
        v = node.value
        if isinstance(v, bool):
            return self._const_reg(f, ("bool", v))
        if isinstance(v, int):
            return self._const_reg(f, ("int", v))
        if isinstance(v, float):
            return self._const_reg(f, ("float", v))
        if isinstance(v, str):
            return self._const_reg(f, ("str", v))
        if v is None:
            return self._const_reg(f, ("none", None))
        raise CompileError("v0 const: unsupported literal %r" % (v,))

    def ex_Name(self, f, node):
        return self._load_name(f, node.id)

    @staticmethod
    def _is_fresh_arith(node):
        # A value that ex_BinOp / ex_UnaryOp(-) just allocated lives only in its
        # temporary register -- it was never loaded from a name, const, index, or
        # attribute (those alias) -- so once consumed as an operand it is dead and
        # may be reclaimed. Anything else may alias a live binding.
        if isinstance(node, ast.BinOp):
            return True
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return True
        return False

    def ex_JoinedStr(self, f, node):
        """An f-string, desugared to `str(part) + str(part) + ..`.

        Rewriting to the AST minipy already compiles keeps this to one method:
        a literal segment stays a `str` constant, and an interpolation becomes
        a `str(..)` call. Nothing new is needed in the VM.

        Format specs and conversions (`{x:>3}`, `{x!r}`) are *not* supported
        and are reported rather than silently dropped -- rendering `{x:>3}` as
        plain `str(x)` would produce quietly wrong output.
        """
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(ast.Constant(value=piece.value))
            elif isinstance(piece, ast.FormattedValue):
                if piece.format_spec is not None or piece.conversion not in (-1, None):
                    raise CompileError(
                        "f-string format specs and conversions are not "
                        "supported (line %s)" % getattr(node, "lineno", "?"))
                parts.append(ast.Call(
                    func=ast.Name(id="str", ctx=ast.Load()),
                    args=[piece.value], keywords=[]))
            else:                                        # pragma: no cover
                raise CompileError("unsupported f-string part: %s"
                                   % type(piece).__name__)
        if not parts:
            return self._const_reg(f, ("str", ""))
        joined = parts[0]
        for nxt in parts[1:]:
            joined = ast.BinOp(left=joined, op=ast.Add(), right=nxt)
        ast.fix_missing_locations(ast.Expression(body=joined))
        for sub in ast.walk(joined):
            if not hasattr(sub, "lineno"):
                sub.lineno = getattr(node, "lineno", 1)
                sub.col_offset = getattr(node, "col_offset", 0)
        return self.expr(f, joined)

    def ex_BinOp(self, f, node):
        rb = self.expr(f, node.left)
        rc = self.expr(f, node.right)
        free_b = self._is_fresh_arith(node.left)
        free_c = self._is_fresh_arith(node.right)
        self._binop(f, node.op, rb, rb, rc, free_b, free_c)
        f.pop_to(rb + 1)
        return rb

    def ex_IfExp(self, f, node):
        # ternary `body if test else orelse`; both arms land in the same reg
        rc = self.expr(f, node.test)
        jf = len(f.code); f.emit("JUMP_IF_FALSE", rc, 0)
        f.pop_to(rc)
        self.expr(f, node.body)               # -> rc
        f.pop_to(rc)                          # value persists in reg rc at runtime
        jend = len(f.code); f.emit("JUMP", 0)
        f.code[jf][2] = len(f.code)           # false -> orelse
        self.expr(f, node.orelse)             # -> rc
        f.code[jend][1] = len(f.code)
        f.numreg.discard(rc)
        return rc

    def ex_UnaryOp(self, f, node):
        r = self.expr(f, node.operand)
        if isinstance(node.op, ast.USub):
            f.emit("NEG", r, r)
        elif isinstance(node.op, ast.Not):
            f.emit("NOT", r, r); f.numreg.discard(r)
        else:
            raise CompileError("v0 unary: only - and not")
        return r

    def ex_Compare(self, f, node):
        if len(node.ops) != 1:
            # chained `a < b < c` -> `a < b and b < c`. Middle operands are
            # re-evaluated, which is fine for the side-effect-free comparison
            # subset minipy targets (names, constants, attribute reads).
            clauses = []
            left = node.left
            for op, comp in zip(node.ops, node.comparators):
                cmp1 = ast.Compare(left=left, ops=[op], comparators=[comp])
                ast.copy_location(cmp1, node)
                clauses.append(cmp1)
                left = comp
            conj = ast.BoolOp(op=ast.And(), values=clauses)
            ast.copy_location(conj, node)
            return self.expr(f, conj)
        op = node.ops[0]
        # membership: `x in seq` / `x not in seq`
        if isinstance(op, (ast.In, ast.NotIn)):
            rx = self.expr(f, node.left)
            rseq = self.expr(f, node.comparators[0])
            f.emit("CONTAINS", rx, rseq, rx)   # reg[rx] = (rx in rseq)
            f.pop_to(rx + 1)
            if isinstance(op, ast.NotIn):
                f.emit("NOT", rx, rx)
            f.numreg.discard(rx)
            return rx
        # identity: `x is y` / `x is not y` -- tag-strict, distinct from ==,
        # so `1 is True` is False (unlike `1 == True`). py2c relies on this via
        # `v is True`/`v is False` to tell a bool constant from the int 1/0.
        if isinstance(op, (ast.Is, ast.IsNot)):
            rb = self.expr(f, node.left)
            rc = self.expr(f, node.comparators[0])
            f.emit("IS", rb, rb, rc)
            f.pop_to(rb + 1)
            if isinstance(op, ast.IsNot):
                f.emit("NOT", rb, rb)
            f.numreg.discard(rb)
            return rb
        rb = self.expr(f, node.left)
        rc = self.expr(f, node.comparators[0])
        name = {ast.Lt: "LT", ast.LtE: "LE", ast.Gt: "GT", ast.GtE: "GE",
                ast.Eq: "EQ", ast.NotEq: "NE"}.get(type(op))
        if name is None:
            raise CompileError("v0 compare: unsupported op %s" % type(op).__name__)
        numeric = rb in f.numreg and rc in f.numreg
        f.emit(name + ("_NN" if numeric and name in ("LT", "LE", "GT", "GE") else ""),
               rb, rb, rc)
        f.pop_to(rb + 1)
        f.numreg.discard(rb)              # result is a bool
        return rb

    def ex_BoolOp(self, f, node):
        # short-circuit and/or; result reg holds the last evaluated operand
        r = self.expr(f, node.values[0])
        jumps = []
        for v in node.values[1:]:
            if isinstance(node.op, ast.And):
                jumps.append(len(f.code)); f.emit("JUMP_IF_FALSE", r, 0)
            else:  # Or: jump out when truthy -> emulate with NOT-test
                # if truthy, skip the rest; encode as: if not r goto next-eval
                tmp = f.push(); f.emit("NOT", tmp, r); f.pop_to(tmp)
                jumps.append(len(f.code)); f.emit("JUMP_IF_FALSE", tmp, 0)
            f.pop_to(r)                    # reuse r for next operand
            r2 = self.expr(f, v)
            assert r2 == r                 # stack discipline keeps them aligned
        end = len(f.code)
        for j in jumps:
            f.code[j][2] = end
        f.numreg.discard(r)
        return r

    def _compute_reassigned_names(self, tree):
        # Names that are ever rebound by something other than a `def` (assignment,
        # aug-assignment, annotation, for-target, unpack). A module-level function
        # whose name appears here is *not* eligible for direct CALL_FUNC, since the
        # global slot it resolves to might no longer hold the original function.
        names = set()

        def tgt(t):
            if isinstance(t, ast.Name):
                names.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for e in t.elts:
                    tgt(e)

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    tgt(t)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                tgt(node.target)
            elif isinstance(node, ast.For):
                tgt(node.target)
        return names

    def _call_kw(self, f, node):
        # keyword-argument call: lay out the register window as
        #   rfun=callee, positional values, keyword values, keyword-name strings
        # and let the VM (CALL_KW) resolve names to positions at runtime.
        for kw in node.keywords:
            if kw.arg is None:
                raise CompileError("v0 call: no **kwargs (line %s)" % node.lineno)
        npos = len(node.args)
        nkw = len(node.keywords)
        if npos >= 256 or nkw >= 256:
            raise CompileError("v0 call: too many args (line %s)" % node.lineno)
        if isinstance(node.func, ast.Attribute):
            # method keyword call `recv.meth(...)`: materialise a bound method
            # (captures the receiver as self) then resolve keywords against it.
            cn = self.consts.add("str", node.func.attr)
            rfun = f.push()
            ro = self.expr(f, node.func.value)
            assert ro == rfun + 1
            f.emit("LOAD_METHOD", rfun, ro, cn)
            f.pop_to(rfun + 1)
        else:
            rfun = self.expr(f, node.func)         # callee
        k = 0
        for a in node.args:                    # positional arg values
            ra = self.expr(f, a)
            assert ra == rfun + 1 + k
            k += 1
        for kw in node.keywords:               # keyword arg values
            rv = self.expr(f, kw.value)
            assert rv == rfun + 1 + k
            k += 1
        for kw in node.keywords:               # keyword names (as string consts)
            cn = self.consts.add("str", kw.arg)
            r = f.push()
            assert r == rfun + 1 + k
            f.emit("LOAD_CONST", r, cn)
            k += 1
        f.emit("CALL_KW", rfun, rfun, npos * 256 + nkw)
        f.pop_to(rfun + 1)
        f.numreg.discard(rfun)
        return rfun

    def _call_spread(self, f, node):
        # `f(a, *s, b, ...)`: build the full positional list at runtime by
        # concatenating list literals (runs of plain args) with the starred
        # iterables in order, then do a single spread call f(*combined). Starred
        # sources must be lists (py2c only ever spreads list results such as
        # str.split()); list concatenation composes them.
        if node.keywords:
            raise CompileError("v0 call: *args with keywords unsupported (line %s)"
                               % node.lineno)
        pieces = []
        run = []
        for a in node.args:
            if isinstance(a, ast.Starred):
                if run:
                    pieces.append(ast.List(elts=run, ctx=ast.Load()))
                    run = []
                pieces.append(a.value)
            else:
                run.append(a)
        if run:
            pieces.append(ast.List(elts=run, ctx=ast.Load()))
        combined = pieces[0]
        for p in pieces[1:]:
            combined = ast.BinOp(left=combined, op=ast.Add(), right=p)
        ast.copy_location(combined, node)
        ast.fix_missing_locations(combined)
        if isinstance(node.func, ast.Attribute):
            cn = self.consts.add("str", node.func.attr)
            rfun = f.push()
            ro = self.expr(f, node.func.value)
            assert ro == rfun + 1
            f.emit("LOAD_METHOD", rfun, ro, cn)   # materialise a bound method
            f.pop_to(rfun + 1)
        else:
            rfun = self.expr(f, node.func)
        rit = self.expr(f, combined)              # full arg list at rfun+1
        assert rit == rfun + 1
        f.emit("CALL_SPREAD", rfun, rfun, 0)      # 0 fixed; spread the whole list
        f.pop_to(rfun + 1)
        f.numreg.discard(rfun)
        return rfun

    def _sys_exit(self, f, node):
        # `sys.exit(n)` -> `raise SystemExit(n)`, which is what CPython does.
        # minipy has no `sys` module object (only the pre-bound sys.argv and
        # sys.path globals), so without this the attribute lookup failed and
        # the VM reported a bare Exception with no message. py2c.py ends with
        # `sys.exit(main(sys.argv[1:]) or 0)`, so this is the first thing that
        # stops it running at all.
        # `sys.exit()` is SystemExit() with *no* args, not SystemExit(0):
        # CPython leaves .args empty, and both mean status 0.
        args = [node.args[0]] if node.args else []
        call = ast.Call(func=ast.Name(id="SystemExit", ctx=ast.Load()),
                        args=args, keywords=[])
        ast.copy_location(call, node)
        ast.copy_location(call.func, node)
        ast.fix_missing_locations(call)
        r = self.expr(f, call)
        f.emit("RAISE", r)
        return r

    def ex_Call(self, f, node):
        if (isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sys"
                and node.func.attr == "exit"):
            return self._sys_exit(f, node)
        if any(isinstance(a, ast.Starred) for a in node.args):
            return self._call_spread(f, node)
        if node.keywords:
            return self._call_kw(f, node)
        # method call `recv.meth(args)`: callable = method-id builtin, receiver
        # is arg0, then the real args -- reuses the CALL window machinery.
        if isinstance(node.func, ast.Attribute):
            # `obj.meth(args)`: fuse the method load and the call into one
            # CALL_METHOD so no bound-method value is materialised. The receiver
            # sits at the call base, args follow; c packs the name const and
            # nargs (name*256 + nargs). Fall back to LOAD_METHOD+CALL only if
            # those don't fit (>=256 args, or a very large const pool).
            meth = node.func.attr
            cn = self.consts.add("str", meth)
            n = len(node.args)
            if n < 256 and cn < 256:
                rbase = f.top
                ro = self.expr(f, node.func.value)
                assert ro == rbase
                k = 0
                for a in node.args:
                    ra = self.expr(f, a)
                    assert ra == rbase + 1 + k
                    k += 1
                f.emit("CALL_METHOD", rbase, rbase, cn * 256 + n)
                f.pop_to(rbase + 1); f.numreg.discard(rbase)
                return rbase
            rfun = f.push()
            ro = self.expr(f, node.func.value)
            assert ro == rfun + 1
            f.emit("LOAD_METHOD", rfun, ro, cn)   # bound method captures self
            f.pop_to(rfun + 1)
            n = 0
            for a in node.args:
                ra = self.expr(f, a)
                assert ra == rfun + 1 + n
                n += 1
            f.emit("CALL", rfun, rfun, n)
            f.pop_to(rfun + 1); f.numreg.discard(rfun)
            return rfun
        # plain call
        # direct call to a module-level function: resolve its index at compile
        # time and skip both the LOAD_GLOBAL of the callee and do_call's runtime
        # type dispatch. Only when the name is a known def, is not rebound anywhere,
        # and is not shadowed by a local in this scope.
        if (isinstance(node.func, ast.Name)
                and node.func.id in self.func_names
                and node.func.id not in self.reassigned_names
                and node.func.id not in f.locals
                and self.func_names[node.func.id] < 256
                and len(node.args) < 256):
            fidx = self.func_names[node.func.id]
            rbase = f.push()                   # dst slot (no callee loaded)
            k = 0
            for a in node.args:
                ra = self.expr(f, a)
                assert ra == rbase + 1 + k
                k += 1
            f.emit("CALL_FUNC", rbase, rbase + 1, fidx * 256 + k)
            f.pop_to(rbase + 1)
            f.numreg.discard(rbase)
            return rbase
        rfun = self.expr(f, node.func)
        n = 0
        for a in node.args:
            ra = self.expr(f, a)
            assert ra == rfun + 1 + n
            n += 1
        f.emit("CALL", rfun, rfun, n)
        f.pop_to(rfun + 1)                 # result lands in rfun
        f.numreg.discard(rfun)
        return rfun

    # ---- container / subscript / comprehension expressions ----
    def ex_List(self, f, node):
        base = f.top
        for e in node.elts:
            self.expr(f, e)
        f.emit("BUILD_LIST", base, base, len(node.elts))
        f.pop_to(base + 1); f.numreg.discard(base)
        return base

    def ex_Tuple(self, f, node):
        base = f.top
        for e in node.elts:
            self.expr(f, e)
        f.emit("BUILD_TUPLE", base, base, len(node.elts))
        f.pop_to(base + 1); f.numreg.discard(base)
        return base

    def ex_Set(self, f, node):
        base = f.top
        for e in node.elts:
            self.expr(f, e)
        f.emit("BUILD_SET", base, base, len(node.elts))
        f.pop_to(base + 1); f.numreg.discard(base)
        return base

    def ex_Dict(self, f, node):
        base = f.top
        for k, v in zip(node.keys, node.values):
            self.expr(f, k)
            self.expr(f, v)
        f.emit("BUILD_DICT", base, base, len(node.keys))
        f.pop_to(base + 1); f.numreg.discard(base)
        return base

    def ex_Attribute(self, f, node):
        # `sys.argv` / `sys.path` -> pre-bound global lists the VM fills (minipy
        # has no real `sys` module object at runtime).
        if (isinstance(node.value, ast.Name) and node.value.id == "sys"
                and node.attr in ("argv", "path")):
            r = f.push()
            slot = "__argv__" if node.attr == "argv" else "__syspath__"
            f.emit("LOAD_GLOBAL", r, self.gslot(slot))
            return r
        rb = self.expr(f, node.value)
        cn = self.consts.add("str", node.attr)
        f.emit("LOAD_ATTR", rb, rb, cn)
        f.numreg.discard(rb)
        return rb

    def ex_Subscript(self, f, node):
        if isinstance(node.slice, ast.Slice):
            sl = node.slice
            rb = self.expr(f, node.value)               # seq at rb
            if sl.lower is None:
                rlo = self._const_reg(f, ("none", None))  # sentinel -> step-aware default
            else:
                rlo = self.expr(f, sl.lower)
            assert rlo == rb + 1
            if sl.upper is None:
                rhi = self._const_reg(f, ("none", None))  # sentinel -> end
            else:
                rhi = self.expr(f, sl.upper)
            assert rhi == rb + 2
            if sl.step is None:
                rst = self._const_reg(f, ("int", 1))
            else:
                rst = self.expr(f, sl.step)
            assert rst == rb + 3
            f.emit("SLICE", rb, rb + 1, 0)              # (lo,hi,step) at rb+1..rb+3
            f.pop_to(rb + 1); f.numreg.discard(rb)
            return rb
        rb = self.expr(f, node.value)
        rc = self.expr(f, node.slice)
        if self._type_of(f, node.value) == "tintlist":
            f.emit("INDEX_INT", rb, rb, rc)         # element is an int
            f.pop_to(rb + 1); f.numreg.add(rb)
            return rb
        f.emit("INDEX", rb, rb, rc)
        f.pop_to(rb + 1); f.numreg.discard(rb)
        return rb

    def ex_ListComp(self, f, node):
        return self._comp(f, node, "list")

    def ex_SetComp(self, f, node):
        return self._comp(f, node, "set")

    def ex_GeneratorExp(self, f, node):
        return self._comp(f, node, "list")   # materialised; good enough for v0

    def ex_DictComp(self, f, node):
        return self._comp(f, node, "dict")

    def _comp(self, f, node, kind):
        acc = f.push()
        if kind == "list":
            f.emit("BUILD_LIST", acc, acc, 0)
        elif kind == "set":
            f.emit("BUILD_SET", acc, acc, 0)
        else:
            f.emit("BUILD_DICT", acc, acc, 0)
        f.numreg.discard(acc)
        self._comp_gen(f, node, kind, acc, 0)
        return acc

    def _comp_gen(self, f, node, kind, acc, gi):
        gens = node.generators
        if gi >= len(gens):
            if kind == "dict":
                rk = self.expr(f, node.key)
                rv = self.expr(f, node.value)
                f.emit("SETINDEX", acc, rk, rv)
                f.pop_to(rk)
            else:
                re = self.expr(f, node.elt)
                f.emit("LIST_APPEND" if kind == "list" else "SET_ADD", acc, re)
                f.pop_to(re)
            return
        gen = gens[gi]
        tgt = gen.target
        if not isinstance(tgt, (ast.Name, ast.Tuple, ast.List)):
            raise CompileError("v0 comp: Name/tuple target only")
        rit = self.expr(f, gen.iter)
        f.emit("ITER_NEW", rit, rit); f.numreg.discard(rit)
        top = len(f.code)
        rx = f.push()
        nx = len(f.code); f.emit("ITER_NEXT", rx, rit, 0)
        if isinstance(tgt, ast.Name):
            self._store_name(f, tgt.id, rx)
            f.pop_to(rit + 1)
        else:                               # unpack tuple element into targets
            for i, elt in enumerate(tgt.elts):
                ridx = self._const_reg(f, ("int", i))
                f.emit("INDEX", ridx, rx, ridx)
                self._store_target_reg(f, elt, ridx)
                f.pop_to(ridx)
            f.pop_to(rit + 1)
        skip_jumps = []
        for cond in gen.ifs:
            rc = self.expr(f, cond)
            skip_jumps.append(len(f.code)); f.emit("JUMP_IF_FALSE", rc, 0)
            f.pop_to(rc)
        self._comp_gen(f, node, kind, acc, gi + 1)
        for sj in skip_jumps:
            f.code[sj][2] = len(f.code)     # failed if -> next iteration
        f.emit("JUMP", top)
        end = len(f.code); f.code[nx][3] = end
        f.pop_to(rit)

    # ---- shared lowering ----
    def _binop(self, f, op, dst, rb, rc, free_b=False, free_c=False):
        name = {ast.Add: "ADD", ast.Sub: "SUB", ast.Mult: "MUL",
                ast.Div: "DIV", ast.Mod: "MOD", ast.FloorDiv: "FLOORDIV",
                ast.Pow: "POW", ast.BitOr: "BITOR", ast.BitAnd: "BITAND",
                ast.BitXor: "BITXOR", ast.LShift: "SHL",
                ast.RShift: "SHR"}.get(type(op))
        if name is None:
            raise CompileError("v0 binop: unsupported %s" % type(op).__name__)
        self._binop_op(f, name, dst, rb, rc, free_b, free_c)

    # ops whose interpreter cases decode the free-operand hint bits in dst
    _FREEABLE = ("ADD", "SUB", "MUL", "DIV", "MOD", "FLOORDIV")

    def _binop_op(self, f, name, dst, rb, rc, free_b=False, free_c=False):
        numeric = rb in f.numreg and rc in f.numreg
        enc = dst
        if name in self._FREEABLE:
            if free_b:
                enc += 536870912            # bit 29 -> free reg b
            if free_c:
                enc += 1073741824           # bit 30 -> free reg c
        if numeric and name in ("ADD", "SUB", "MUL"):
            f.emit(name + "_NN", enc, rb, rc)
            f.numreg.add(dst)
        else:
            f.emit(name, enc, rb, rc)
            if numeric and name in ("FLOORDIV", "MOD", "POW", "DIV"):
                f.numreg.add(dst)
            else:
                f.numreg.discard(dst)

    def _maybe_num_cmp(self, f, r):
        f.numreg.discard(r)

    def _load_name(self, f, name):
        if not f.is_module and name in f.locals:
            src = f.locals[name]
            r = f.push(); f.emit("MOVE", r, src)
            if src in f.numreg:           # numeric fact survives the move
                f.numreg.add(r)
            return r
        r = f.push(); f.emit("LOAD_GLOBAL", r, self.gslot(name))
        return r

    def _store_name(self, f, name, r):
        if not f.is_module:
            if name not in f.locals:
                # new local: give it a fixed reg below temps is hard post-hoc;
                # v0 keeps locals discovered up-front, so treat unknown as global
                pass
            if name in f.locals:
                f.emit("MOVE", f.locals[name], r)
                if r in f.numreg:
                    f.numreg.add(f.locals[name])
                return
        renc = r + 536870912 if name in self.reclaimable_globals else r
        f.emit("STORE_GLOBAL", renc, self.gslot(name))

    # ---- serialise ----
    def to_program(self, source_name):
        funcs = []
        for fr in self.funcs:
            funcs.append({
                "name": fr.name,
                "nparams": fr.nparams,
                "nregs": fr.maxreg,
                "nlocals": fr.base,
                "code": [_instr_json(op, a, b, c)
                         for (op, a, b, c) in fr.code],
                "defaults": fr.defaults,
                "vararg": fr.vararg,
                "params": fr.param_order,
            })
        names = [None] * len(self.gnames)
        for nm, slot in self.gnames.items():
            names[slot] = nm
        return {
            "version": 1,
            "source": source_name,
            "consts": self.consts.as_json(),
            "names": names,
            "nglobals": len(self.gnames),
            "funcs": funcs,
            "classes": [
                {"cname": ci["name"], "base": ci["base"],
                 "methods": [{"mname": m["name"], "mfunc": m["func"]}
                             for m in ci["methods"]]}
                for ci in self.classes
            ],
            "entry": 0,
        }


class _Loop:
    def __init__(self, top):
        self.top = top
        self.breaks = []
        self.rotated = False
        self.conts = []


def _ann_name(node):
    """The annotation written on a param, as a bare name: `n: int` -> 'int',
    `n: "int"` -> 'int'. Returns None if absent/complex."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip().strip("'\"").rstrip("*").strip()
    return None


# Discover function locals up-front (params + names assigned in the body) so the
# single-pass compiler can map them to fixed registers. Module scope skips this.
def _collect_locals(node):
    found = []
    declared_global = set()                  # names bound by `global x` -> not locals
    for n in ast.walk(node):
        if isinstance(n, ast.Global):
            for gn in n.names:
                declared_global.add(gn)
        elif isinstance(n, ast.Nonlocal):
            for gn in n.names:
                declared_global.add(gn)
    def add(name):
        if name not in found and name not in declared_global:
            found.append(name)

    def add_target(t):                       # recurse through nested tuple/list
        if isinstance(t, ast.Name):
            add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                add_target(e)
        # Attribute / Subscript targets do not bind a local

    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                add_target(t)
        elif isinstance(n, (ast.AugAssign, ast.For)) and isinstance(getattr(n, "target", None), ast.Name):
            add(n.target.id)
        elif isinstance(n, (ast.For, ast.comprehension)) and isinstance(getattr(n, "target", None), (ast.Tuple, ast.List)):
            add_target(n.target)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            add(n.target.id)
        elif isinstance(n, ast.comprehension) and isinstance(n.target, ast.Name):
            add(n.target.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            add(n.name)                          # `except E as name:` binds a local
    return found


class _WithDesugar(ast.NodeTransformer):
    """Lower `with` to the manager protocol (PEP 343), wrapped so `__exit__`
    runs on every exit path *and* can suppress an exception:

        with A as a:        _with_N = A
            BODY        ->  a = _with_N.__enter__()
                            _hit_N = False
                            try:
                                BODY
                            except BaseException as _exc_N:
                                _hit_N = True
                                if not _with_N.__exit__(type(_exc_N), _exc_N, None):
                                    raise
                            finally:
                                if not _hit_N:
                                    _with_N.__exit__(None, None, None)

    The `_hit_N` flag is the PEP-343 trick: the finally calls `__exit__(None...)`
    only when no exception was seen, so normal completion, `return`, `break` and
    `continue` all get the clean-path `__exit__` (via the finally), while an
    exception goes through the handler -- which passes the live exception triple
    and re-raises only if `__exit__` returns falsy (so a truthy result suppresses
    it). Running before _collect_locals gives `_with_N`/`_hit_N`/`_exc_N` real
    local slots (per-call / recursion-safe). Multiple items nest left-to-right;
    nested `with`s are desugared inner-first. (Traceback arg is None -- minipy
    has no traceback objects.)"""
    def __init__(self):
        self.ctr = 0

    def visit_With(self, node):
        self.generic_visit(node)             # desugar any nested withs first
        body = node.body
        for item in reversed(list(node.items)):
            self.ctr += 1
            mgr = "_with_%d" % self.ctr
            hit = "_hit_%d" % self.ctr
            exc = "_exc_%d" % self.ctr

            def load(name):
                return ast.Name(id=name, ctx=ast.Load())

            assign_mgr = ast.Assign(targets=[ast.Name(id=mgr, ctx=ast.Store())],
                                    value=item.context_expr)
            enter = ast.Call(func=ast.Attribute(value=load(mgr), attr="__enter__",
                                                ctx=ast.Load()), args=[], keywords=[])
            if item.optional_vars is not None:
                enter_stmt = ast.Assign(targets=[item.optional_vars], value=enter)
            else:
                enter_stmt = ast.Expr(value=enter)
            hit_false = ast.Assign(targets=[ast.Name(id=hit, ctx=ast.Store())],
                                   value=ast.Constant(value=False))
            exit_exc = ast.Call(
                func=ast.Attribute(value=load(mgr), attr="__exit__", ctx=ast.Load()),
                args=[ast.Call(func=load("type"), args=[load(exc)], keywords=[]),
                      load(exc), ast.Constant(value=None)], keywords=[])
            handler = ast.ExceptHandler(
                type=load("BaseException"), name=exc,
                body=[ast.Assign(targets=[ast.Name(id=hit, ctx=ast.Store())],
                                 value=ast.Constant(value=True)),
                      ast.If(test=ast.UnaryOp(op=ast.Not(), operand=exit_exc),
                             body=[ast.Raise(exc=None, cause=None)], orelse=[])])
            exit_none = ast.Expr(value=ast.Call(
                func=ast.Attribute(value=load(mgr), attr="__exit__", ctx=ast.Load()),
                args=[ast.Constant(value=None), ast.Constant(value=None),
                      ast.Constant(value=None)], keywords=[]))
            fin = [ast.If(test=ast.UnaryOp(op=ast.Not(), operand=load(hit)),
                          body=[exit_none], orelse=[])]
            tryx = ast.Try(body=body, handlers=[handler], orelse=[], finalbody=fin)
            body = [assign_mgr, enter_stmt, hit_false, tryx]
        for s in body:
            ast.copy_location(s, node)
            ast.fix_missing_locations(s)
        return body


# ---- module linking -------------------------------------------------------
# minipy has no runtime module objects. Instead, a small set of library modules
# written in the minipy subset are *linked in at compile time*: when a program
# does `import re`, the module's source is parsed, its top-level names are given
# a `$mod$` prefix (so they cannot collide with the importing program), its body
# is prepended to the program, and every `re.member` reference in the program is
# rewritten to the corresponding prefixed name. `re.match(x)` therefore becomes
# an ordinary direct call to the linked `$re$match` function -- no code-generator
# or interpreter changes are needed. Modules not in the registry keep the
# recognised-no-op import behavior (e.g. CPython-only modules gated behind a
# `sys.implementation.name` check).
def _module_registry():
    # `import os` lives at module level (see the top of the file): the module
    # linker only rewrites top-level imports, so a function-local one leaves
    # `os` unbound when compiler.py is itself compiled by minipy.
    here = os.path.dirname(os.path.abspath(__file__))
    lib = os.path.join(here, "..", "rpy_lib")
    return {
        "re": os.path.join(lib, "crustre.py"),
        "os": os.path.join(lib, "minios.py"),
        "rast": os.path.join(lib, "rast.py"),
        "ast": os.path.join(lib, "minast.py"),
        "pathlib": os.path.join(lib, "minipathlib.py"),
    }


def _module_bound_names(body):
    names = set()
    for s in body:
        if isinstance(s, (ast.FunctionDef, ast.ClassDef)):
            names.add(s.name)
        elif isinstance(s, ast.Assign):
            for t in s.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
            names.add(s.target.id)
    return names


class _RefPrefixer(ast.NodeTransformer):
    """Rewrite references to a set of names to replacement names, skipping any
    scope where a local/param shadows the name (so a function local named `arg`
    is not mistaken for a module-global `arg`). Driven by a rename map, it serves
    both to namespace a linked module's own top-level names ($mod$name) and to
    bind `from X import name` references to the linked source ($X$name). This lets
    large modules (e.g. minast, which defines `class arg` yet also uses `arg` as a
    local) link cleanly."""
    def __init__(self, rename):
        self.rename = rename              # name -> replacement name
        self.shadowed = set()

    def _locals_of(self, node):
        loc = set(_collect_locals(node))
        for a in node.args.args:
            loc.add(a.arg)
        if node.args.vararg:
            loc.add(node.args.vararg.arg)
        if node.args.kwarg:
            loc.add(node.args.kwarg.arg)
        for a in node.args.kwonlyargs:
            loc.add(a.arg)
        return loc

    def visit_FunctionDef(self, node):
        sh = (self._locals_of(node) & set(self.rename)) - self.shadowed
        self.shadowed |= sh
        self.generic_visit(node)
        self.shadowed -= sh
        return node

    def visit_Name(self, node):
        if node.id in self.rename and node.id not in self.shadowed:
            node.id = self.rename[node.id]
        return node


class _ModuleAttrRewriter(ast.NodeTransformer):
    """Rewrite `alias.member` (of a linked module) to its prefixed global name."""
    def __init__(self, alias_members):
        self.am = alias_members

    def visit_Attribute(self, node):
        self.generic_visit(node)
        v = node.value
        if isinstance(v, ast.Name) and v.id in self.am:
            mm = self.am[v.id]
            if node.attr in mm:
                return ast.copy_location(
                    ast.Name(id=mm[node.attr], ctx=node.ctx), node)
        return node


# Library modules written in the minipy subset are linked in at compile time.
# Linking is transitive and handles both `import X` (rewriting `X.member` to the
# prefixed global) and `from X import a, b` (rewriting bare `a`/`b`), so minast --
# which does `from rast import parse_python` -- pulls in rast automatically. Each
# module body is inlined once; its own top-level names get a `$mod$` prefix so
# they cannot collide with the importing program.
def _link_modules(tree):
    reg = _module_registry()
    linked = {}                               # mod key -> {member: $mod$member}
    prepend = []

    def rewrite_body(mbody, own, prefix):
        # combined rename map: own names -> $mod$name (unless prefix is None, i.e.
        # the main program keeps its own names), plus `from X import a` -> $X$a.
        rename = {}
        if prefix is not None:
            for n in own:
                rename[n] = prefix + n
        alias_members = {}
        for s in mbody:
            if isinstance(s, ast.Import):
                for al in s.names:
                    if al.name in reg:
                        alias_members[al.asname or al.name] = link(al.name)
            elif isinstance(s, ast.ImportFrom):
                if s.module in reg:
                    link(s.module)
                    srcpref = "$" + s.module + "$"
                    for al in s.names:
                        rename[al.asname or al.name] = srcpref + al.name
        holder = ast.Module(body=mbody, type_ignores=[])
        if rename:
            _RefPrefixer(rename).visit(holder)
        if alias_members:
            _ModuleAttrRewriter(alias_members).visit(holder)
        return holder.body

    def link(mod_key):
        if mod_key in linked:
            return linked[mod_key]
        with open(reg[mod_key], encoding="utf-8") as fh:
            mtree = ast.parse(fh.read(), filename=reg[mod_key])
        prefix = "$" + mod_key + "$"
        own = _module_bound_names(mtree.body)
        member_map = {}
        for n in own:
            member_map[n] = prefix + n
        linked[mod_key] = member_map          # register before recursing (cycles)
        for s in mtree.body:                  # rename own def/class definitions
            if isinstance(s, (ast.FunctionDef, ast.ClassDef)) and s.name in own:
                s.name = prefix + s.name
        body = rewrite_body(mtree.body, own, prefix)
        prepend.extend(body)
        return member_map

    has_link = False
    for s in tree.body:
        if isinstance(s, ast.Import):
            for al in s.names:
                if al.name in reg:
                    has_link = True
        elif isinstance(s, ast.ImportFrom):
            if s.module in reg:
                has_link = True
    if not has_link:
        return tree
    tree.body = rewrite_body(tree.body, set(), None)
    tree.body = prepend + tree.body
    ast.fix_missing_locations(tree)
    return tree


def _fn_paramnames(fn):
    ps = [a.arg for a in fn.args.args]
    if fn.args.vararg:
        ps.append(fn.args.vararg.arg)
    if fn.args.kwarg:
        ps.append(fn.args.kwarg.arg)
    ps += [a.arg for a in fn.args.kwonlyargs]
    return ps


def _child_stmt_lists(stmt):
    """Statement-list bodies directly under a control-flow statement (not the
    bodies of nested def/class, which introduce their own scope)."""
    lists = []
    for field in ("body", "orelse", "finalbody"):
        v = getattr(stmt, field, None)
        if isinstance(v, list):
            lists.append(v)
    if isinstance(stmt, ast.Try):
        for h in stmt.handlers:
            lists.append(h.body)
    return lists


def _nested_funcs_of(enc):
    """(nested FunctionDef, containing list) for functions nested directly in enc,
    recursing through control-flow blocks but not into nested class/def scopes."""
    result = []

    def scan(body):
        for s in body:
            if isinstance(s, ast.FunctionDef):
                result.append((s, body))          # a nested function to lift
            elif isinstance(s, ast.ClassDef):
                pass                              # nested classes: left alone
            else:
                for sub in _child_stmt_lists(s):
                    scan(sub)
    scan(enc.body)
    return result


def _loaded_names(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            out.add(n.id)
    return out


def _direct_calls(fn):
    """Names called directly (`name(...)`) anywhere in fn's body."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.add(n.func.id)
    return out


class _CallArgPrepender(ast.NodeTransformer):
    """Rewrite `name(...)` -> `newname(capt..., ...)` for a lifted nested func."""
    def __init__(self, mapping):
        self.mapping = mapping                    # name -> (newname, [capt names])

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in self.mapping:
            newname, capt = self.mapping[node.func.id]
            node.func = ast.Name(id=newname, ctx=ast.Load())
            node.args = [ast.Name(id=c, ctx=ast.Load()) for c in capt] + node.args
        return node


def _lift_closures(tree):
    """Lift non-escaping nested functions to module level, threading captured
    enclosing locals through as leading parameters. C (py2c) and minipy's register
    VM both lack closures; this mirrors py2c's own closure conversion for the
    subset that matters here: functions only ever *called* (never used as values),
    with captured variables read or mutated-in-place but not rebound (no nonlocal).
    Nested classes are left untouched."""
    module_globals = _module_bound_names(tree.body) | set(BUILTINS.keys())
    new_top = []
    counter = [0]

    def lift_enclosing(enc):
        nested = _nested_funcs_of(enc)
        if not nested:
            return
        names = set(fn.name for fn, _ in nested)
        enc_locals = set(_fn_paramnames(enc)) | set(_collect_locals(enc))
        # base captures: enclosing locals each nested func reads
        base = {}
        bound = {}
        for fn, _ in nested:
            fnbound = set(_fn_paramnames(fn)) | set(_collect_locals(fn))
            bound[fn.name] = fnbound
            free = _loaded_names(fn) - fnbound
            base[fn.name] = (free & enc_locals) - module_globals
        # escaping / unsupported guards
        for fn, _ in nested:
            if fn.name in (_loaded_names(enc) - _direct_calls(enc)):
                raise CompileError("v0 closure: nested function %r used as a value "
                                   "(line %s)" % (fn.name, fn.lineno))
        # fixpoint: a nested func that calls a sibling must also carry its captures
        capt = dict((k, set(v)) for k, v in base.items())
        changed = True
        while changed:
            changed = False
            for fn, _ in nested:
                for callee in _direct_calls(fn):
                    if callee in names and callee != fn.name:
                        add = capt[callee] - capt[fn.name] - bound[fn.name]
                        if add:
                            capt[fn.name] |= add
                            changed = True
        # assign lifted names and build the call rewrite map
        newname = {}
        for fn, _ in nested:
            newname[fn.name] = "_clo%d_%s" % (counter[0], fn.name)
            counter[0] += 1
        mapping = {}
        for fn, _ in nested:
            mapping[fn.name] = (newname[fn.name], sorted(capt[fn.name]))
        rewriter = _CallArgPrepender(mapping)
        # produce the lifted top-level functions
        for fn, container in nested:
            caps = sorted(capt[fn.name])
            fn.name = newname_of = newname[fn.name]
            fn.args.args = [ast.arg(arg=c, annotation=None) for c in caps] + fn.args.args
            rewriter.visit(fn)                    # fix sibling/self calls inside
            new_top.append(fn)
        # remove the nested defs from their containers and fix calls in enc
        for fn, container in nested:
            container[:] = [s for s in container if s is not fn]
        rewriter.visit(enc)

    for top in list(tree.body):
        if isinstance(top, ast.FunctionDef):
            lift_enclosing(top)
        elif isinstance(top, ast.ClassDef):
            for s in list(top.body):          # methods can nest functions too
                if isinstance(s, ast.FunctionDef):
                    lift_enclosing(s)
    # Lifted functions are placed *before* the rest of the module body: a def
    # only stores its function value (the body runs later, at call time), and
    # module-level code may call into functions that reach these closures, so
    # their globals must be bound before any such call executes.
    tree.body = new_top + tree.body
    ast.fix_missing_locations(tree)
    return tree


class _NameToSelfAttr(ast.NodeTransformer):
    """Rewrite free Load references to captured names -> self._cap_<name>,
    without descending into nested scopes (their names resolve elsewhere)."""
    def __init__(self, caps):
        self.caps = caps

    def visit_FunctionDef(self, node):
        return node

    def visit_Lambda(self, node):
        return node

    def visit_ClassDef(self, node):
        return node

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id in self.caps:
            return ast.Attribute(
                value=ast.Name(id="self", ctx=ast.Load()),
                attr="_cap_" + node.id, ctx=ast.Load())
        return node


class _ClassConsRewriter(ast.NodeTransformer):
    """Rewrite `C(...)` -> `C(capvals..., ...)` at a nested class's construction
    sites, prepending the captured enclosing locals."""
    def __init__(self, cname, cap_list):
        self.cname = cname
        self.cap_list = cap_list

    def visit_FunctionDef(self, node):
        return node

    def visit_ClassDef(self, node):
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == self.cname:
            node.args = [ast.Name(id=c, ctx=ast.Load())
                         for c in self.cap_list] + node.args
        return node


def _classes_nested_in(body, out):
    """ClassDefs directly nested in body (through control-flow blocks, not into
    nested def/class scopes)."""
    for s in body:
        if isinstance(s, ast.ClassDef):
            out.append(s)
        elif isinstance(s, (ast.FunctionDef, ast.ClassDef)):
            pass
        else:
            for sub in _child_stmt_lists(s):
                _classes_nested_in(sub, out)


def _lift_nested_classes(tree):
    """Thread an enclosing function's locals into a class defined inside it.

    minipy compiles a class method as a plain function, so a method's reference
    to an enclosing local resolves as a (missing) global and reads None. Here we
    capture each such local as an instance attribute self._cap_<name>, set in a
    synthesized or extended __init__, rewrite the method bodies to read it, and
    rewrite the construction site `C(...)` to pass the current values. Captures
    are read or mutated-in-place (dict/list/set), never rebound -- matching the
    contract of _lift_closures. Runs after _lift_closures, when every function
    that may contain such a class is already at module top level."""
    module_globals = _module_bound_names(tree.body) | set(BUILTINS.keys())

    def handle(enc):
        enc_locals = set(_fn_paramnames(enc)) | set(_collect_locals(enc))
        classes = []
        _classes_nested_in(enc.body, classes)
        for cls_node in classes:
            _thread(cls_node, enc, enc_locals)

    def _thread(cls_node, enc, enc_locals):
        methods = [m for m in cls_node.body if isinstance(m, ast.FunctionDef)]
        allcaps = set()
        per = {}
        for m in methods:
            mbound = set(a.arg for a in m.args.args) | set(_collect_locals(m))
            free = _loaded_names(m) - mbound
            c = (free & enc_locals) - module_globals
            c.discard("self")
            per[m.name] = c
            allcaps |= c
        if not allcaps:
            return
        cap_list = sorted(allcaps)
        # rewrite method bodies (except __init__) to read captures off self
        for m in methods:
            if m.name == "__init__":
                continue
            rw = _NameToSelfAttr(per[m.name])
            m.body = [rw.visit(st) for st in m.body]
        # build the capture-storing statements + params
        store = []
        for c in cap_list:
            store.append(ast.Assign(
                targets=[ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()),
                    attr="_cap_" + c, ctx=ast.Store())],
                value=ast.Name(id="_arg_" + c, ctx=ast.Load())))
        cap_args = [ast.arg(arg="_arg_" + c, annotation=None) for c in cap_list]
        init = None
        for m in methods:
            if m.name == "__init__":
                init = m
        if init is None:
            init = ast.FunctionDef(
                name="__init__",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self", annotation=None)] + cap_args,
                    vararg=None, kwonlyargs=[], kw_defaults=[],
                    kwarg=None, defaults=[]),
                body=store, decorator_list=[], returns=None)
            cls_node.body.insert(0, init)
        else:
            init.args.args = init.args.args[:1] + cap_args + init.args.args[1:]
            init.body = store + init.body
        # pass the captured values at every construction site inside enc
        cons = _ClassConsRewriter(cls_node.name, cap_list)
        enc.body = [cons.visit(st) for st in enc.body]

    for top in list(tree.body):
        if isinstance(top, ast.FunctionDef):
            handle(top)
        elif isinstance(top, ast.ClassDef):
            for s in list(top.body):
                if isinstance(s, ast.FunctionDef):
                    handle(s)
    ast.fix_missing_locations(tree)
    return tree


class _StripOpenKwargs(ast.NodeTransformer):
    """Drop encoding=/errors=/newline= (and any) keyword args from open() calls.
    minipy files are byte/UTF-8 oriented, so these are no-ops; removing them
    keeps open a clean positional call (path[, mode]) on both VMs -- the native
    interp binds open as a builtin whose CALL path takes positional args only."""
    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "open" \
                and node.keywords:
            node.keywords = []
        return node


def _lower_class_attrs(tree):
    """Lower class-level attribute assignments (`X = <value>` directly in a
    class body) into per-instance initialisation: each becomes `self.X =
    <value>` prepended to the class's __init__ (synthesised if the class has
    none). minipy has no class-object storage, so a class variable is modelled
    as an ordinary instance attribute established at construction time. This is
    faithful for the read-only-constant use (lookup tables, method-name sets)
    that self-hosting relies on; it does not provide `ClassName.X` access, and
    a mutated class variable would not be shared across instances (no such use
    exists in the transpiler). Runs after _link_modules so any `mod.X` inside a
    value has already been rewritten to its linked global.
    """
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for node in classes:
        attr_stmts = []
        new_body = []
        for item in node.body:
            name = None
            value = None
            if isinstance(item, ast.Assign) and item.value is not None and \
                    len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                name = item.targets[0].id
                value = item.value
            elif isinstance(item, ast.AnnAssign) and item.value is not None and \
                    isinstance(item.target, ast.Name):
                name = item.target.id
                value = item.value
            if name is not None:
                st = ast.Assign(
                    targets=[ast.Attribute(
                        value=ast.Name(id="self", ctx=ast.Load()),
                        attr=name, ctx=ast.Store())],
                    value=value)
                attr_stmts.append(st)
                continue                      # drop the class-level assignment
            new_body.append(item)
        if not attr_stmts:
            continue
        node.body = new_body
        init = None
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                init = item
                break
        if init is None:
            init = ast.FunctionDef(
                name="__init__",
                args=ast.arguments(
                    posonlyargs=[], args=[ast.arg(arg="self")], vararg=None,
                    kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
                body=[], decorator_list=[], returns=None)
            node.body.insert(0, init)
        insert_at = 0                          # keep a leading docstring first
        if init.body and isinstance(init.body[0], ast.Expr) and \
                isinstance(init.body[0].value, ast.Constant) and \
                isinstance(init.body[0].value.value, str):
            insert_at = 1
        init.body[insert_at:insert_at] = attr_stmts
        if not init.body:
            init.body.append(ast.Pass())
    ast.fix_missing_locations(tree)
    return tree


def _bind_staticmethods(tree):
    """Give every `@staticmethod` an explicit leading `self` parameter.

    minipy compiles a class method as a plain function whose first parameter is
    the receiver, and it does not implement the descriptor protocol -- so a
    `@staticmethod` was called with the instance still prepended and every
    argument landed one position to the left. In compiler.py that showed up as
    `self._scope_locals(fnode)` binding `fnode` to the Compiler instance and
    then failing on `.args`, which is what stopped compiler.py from compiling
    itself.

    Adding the parameter rather than stripping the receiver at the call site
    keeps the change local to the definition: the call already passes the
    instance, so arity now matches and the body simply ignores it. That covers
    `self.f(x)` and `obj.f(x)`. It does *not* cover `Cls.f(x)`, which passes no
    receiver -- minipy has no class-object call path for methods at all, so that
    form was already unsupported and is unaffected by this.

    The parameter is named `_staticself` rather than `self` so a static method
    that happens to use the name `self` for something of its own is not shadowed.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for m in node.body:
            if not isinstance(m, ast.FunctionDef):
                continue
            keep = []
            is_static = 0
            for d in m.decorator_list:
                if isinstance(d, ast.Name) and d.id == "staticmethod":
                    is_static = 1
                else:
                    keep.append(d)
            if is_static == 1:
                m.decorator_list = keep
                m.args.args.insert(0, ast.arg(arg="_staticself", annotation=None))
    return tree


def compile_source(src, source_name="<module>"):
    tree = ast.parse(src, filename=source_name)
    # expose __file__ so programs that introspect their own path work (pathlib)
    fileassign = ast.Assign(
        targets=[ast.Name(id="__file__", ctx=ast.Store())],
        value=ast.Constant(value=source_name))
    fileassign.lineno = 1
    fileassign.col_offset = 0
    ast.fix_missing_locations(fileassign)
    tree.body.insert(0, fileassign)
    tree = _link_modules(tree)               # inline supported library imports
    tree = _WithDesugar().visit(tree)        # with -> manager-protocol + try/finally
    tree = _StripOpenKwargs().visit(tree)    # open(..., encoding=..) -> open(..)
    tree = _bind_staticmethods(tree)         # @staticmethod -> explicit receiver param
    tree = _lower_class_attrs(tree)          # class variables -> self.X in __init__
    tree = _lift_closures(tree)              # nested funcs -> top-level + captures
    tree = _lift_nested_classes(tree)        # nested class methods capture locals
    ast.fix_missing_locations(tree)
    c = Compiler()
    c._loops = []
    c._finallys = []         # module-level try/finally + with needs this too
    # give every top-level def its locals before lowering its body
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            extra = [n for n in _collect_locals(node)]
            node._extra_locals = extra
    prog = c.compile_module(tree, source_name)
    # patch frames so discovered locals exist as fixed regs (params already in)
    return prog


def compile_file(path):
    with open(path, encoding="utf-8") as fh:
        return compile_source(fh.read(), path)


# ===========================================================================
# Reference VM (CPython). Faithful executor of the flattened bytecode, used to
# validate the format against python3 and as the rpython interpreter's oracle.
# ===========================================================================
class VM:
    def __init__(self, prog, out=None):
        self.prog = prog
        self.consts = prog["consts"]
        self.names = prog["names"]
        self.globals = [None] * prog["nglobals"]
        self.funcs = prog["funcs"]
        import sys as _sys
        self.out = out if out is not None else _sys.stdout
        self._exc_flag = False
        self._exc_val = None

    def _const(self, i):
        c = self.consts[i]
        t = c["t"]
        if t == "int":
            return c["i"]
        if t == "float":
            return c["d"]
        if t == "str":
            return c["s"]
        if t == "bool":
            return bool(c["i"])
        if t == "none":
            return None
        if t == "func":
            return ("func", c["i"])
        if t == "builtin":
            return ("builtin", c["i"])
        if t == "class":
            return ("class", c["i"])
        raise RuntimeError("bad const %r" % (c,))

    def run(self):
        # expose the process argv to the program as `sys.argv` (compiled to a
        # read of the __argv__ global). script_argv is set by the launcher.
        argv = getattr(self, "script_argv", None)
        if argv is not None and "__argv__" in self.names:
            self.globals[self.names.index("__argv__")] = list(argv)
        # `sys.path` is a real (empty) list programs may mutate; minipy resolves
        # imports at compile time, so its runtime contents don't affect linking.
        if "__syspath__" in self.names:
            self.globals[self.names.index("__syspath__")] = []
        # Reference VM only: bind host builtins minipy doesn't provide natively
        # (open, hex, hash, ...) so the transpiler's I/O and helpers work. Names
        # minipy already provides (BUILTINS) and dunders are left alone; the
        # module body runs next and overrides any it defines itself.
        import builtins as _bi
        i = 0
        while i < len(self.names):
            nm = self.names[i]
            if (nm not in BUILTINS and not nm.startswith("__")
                    and self.globals[i] is None and hasattr(_bi, nm)):
                self.globals[i] = getattr(_bi, nm)
            i += 1
        # give the minios shim a real filesystem via its injectable host-os hook
        # (never assigned in minios, so this survives module load).
        if "_host_os" in self.names:
            import os as _realos
            self.globals[self.names.index("_host_os")] = _realos
        self.exit_status = 0
        self._call(self.prog["entry"], [])
        if self._exc_flag:
            v = self._exc_val
            # An unhandled SystemExit is how a program ends normally, not a
            # failure: report its status to the caller instead of raising.
            if isinstance(v, _Inst):
                cn0 = self.prog["classes"][v.cid].get("cname", "")
                d0 = cn0.rfind("$")
                if (cn0[d0 + 1:] if d0 >= 0 else cn0) == "SystemExit":
                    a0 = v.attrs.get("args", ())
                    code = a0[0] if a0 else 0
                    self.exit_status = code if isinstance(code, int) else (
                        0 if code is None else 1)
                    return
            # surface an unhandled exception rather than returning silently
            desc = repr(v)
            if isinstance(v, _Inst):
                cn = self.prog["classes"][v.cid].get("cname", "?")
                di = cn.rfind("$")
                short = cn[di + 1:] if di >= 0 else cn
                a = v.attrs.get("args", ())
                desc = "%s%s" % (short, tuple(a) if a else "")
            raise RuntimeError("minipy: unhandled exception: %s" % desc)

    def _call(self, fidx, args):
        fn = self.funcs[fidx]
        regs = [None] * max(fn["nregs"], fn["nparams"])
        va = fn.get("vararg", -1)
        if va >= 0:                             # *args: pack the rest into a tuple
            for i in range(min(va, len(args))):
                regs[i] = args[i]
            regs[va] = tuple(args[va:])
        else:
            for i, a in enumerate(args):
                regs[i] = a
        defs = fn.get("defaults", [])       # supply defaults for missing params
        for i in range(len(args), fn["nparams"]):
            if i == va:
                continue                        # *args already bound (possibly ())
            if i < len(defs) and defs[i] >= 0:
                regs[i] = self._const(defs[i])
        code = fn["code"]
        blocks = []
        pc = 0
        while pc < len(code):
            ins = code[pc]; op = ins["op"]; a = ins["a"]; b = ins["b"]; c = ins["c"]
            if a >= 536870912:               # strip free-operand hint bits (a no-op
                a &= 536870911               # in the GC'd reference VM; native frees)
            pc += 1
            if op == 1:    regs[a] = self._const(b)
            elif op == 2:  regs[a] = self.globals[b]
            elif op == 3:  self.globals[b] = regs[a]
            elif op == 4:  regs[a] = regs[b]
            elif op == 5:  return regs[a]
            elif op == 6:  pc = a
            elif op == 7:
                if not regs[a]: pc = b
            elif op == 76:
                if not (regs[a] <  regs[c]): pc = b
            elif op == 77:
                if not (regs[a] <= regs[c]): pc = b
            elif op == 78:
                if not (regs[a] >  regs[c]): pc = b
            elif op == 79:
                if not (regs[a] >= regs[c]): pc = b
            elif op == 80:
                if not (regs[a] == regs[c]): pc = b
            elif op == 81:
                if not (regs[a] != regs[c]): pc = b
            elif op == 82:
                if regs[a]: pc = b
            elif op == 83:
                if regs[a] <  regs[c]: pc = b
            elif op == 84:
                if regs[a] <= regs[c]: pc = b
            elif op == 85:
                if regs[a] >  regs[c]: pc = b
            elif op == 86:
                if regs[a] >= regs[c]: pc = b
            elif op == 87:
                if regs[a] == regs[c]: pc = b
            elif op == 88:
                if regs[a] != regs[c]: pc = b
            elif op == 8:
                callee = regs[b]; ca = [regs[b + 1 + k] for k in range(c)]
                regs[a] = self._invoke(callee, ca)
            elif op == 91:                      # CALL_SPREAD: f(fixed..., *iter)
                callee = regs[b]
                ca = [regs[b + 1 + k] for k in range(c)]
                ca = ca + list(regs[b + 1 + c])
                regs[a] = self._invoke(callee, ca)
            elif op == 90:                      # CALL_KW: c = npos*256 + nkw
                npos = c // 256; nkw = c % 256
                callee = regs[b]
                posvals = [regs[b + 1 + k] for k in range(npos)]
                kwvals = [regs[b + 1 + npos + k] for k in range(nkw)]
                kwnames = [regs[b + 1 + npos + nkw + k] for k in range(nkw)]
                if isinstance(callee, tuple):
                    fullargs = self._bind_kwargs(callee, posvals, kwnames, kwvals)
                    regs[a] = self._invoke(callee, fullargs)
                elif sys.implementation.name != "minipy":
                    # Host callable: real kwargs. `callee(*posvals, **kw)` is the
                    # single construct the v0 code generator rejects, and it was
                    # the only thing stopping compiler.py from compiling itself.
                    # The branch is dead on minipy -- a host callable is a
                    # CPython function object reached through the reference VM,
                    # and the reference VM is exactly what the native
                    # interpreter replaces -- so the impl guard folds it away at
                    # compile time. That is cheaper and more honest than adding a
                    # CALL_SPREAD_KW opcode that no compiled program would use.
                    kw = {}
                    for k in range(nkw):
                        kw[kwnames[k]] = kwvals[k]
                    regs[a] = callee(*posvals, **kw)
                else:
                    raise CompileError("CALL_KW: host callable %r has no minipy "
                                       "equivalent" % (callee,))
            elif op == 89:                      # CALL_FUNC (direct)
                fnum = c // 256; nargs = c % 256
                ca = [regs[b + k] for k in range(nargs)]
                regs[a] = self._call(fnum, ca)
            elif op == 9:                       # BUILD_LIST
                regs[a] = [regs[b + k] for k in range(c)]
            elif op == 10:                      # BUILD_TUPLE
                regs[a] = tuple(regs[b + k] for k in range(c))
            elif op == 11:                      # BUILD_DICT
                d = {}
                for k in range(c):
                    d[regs[b + 2 * k]] = regs[b + 2 * k + 1]
                regs[a] = d
            elif op == 12:                      # BUILD_SET
                regs[a] = set(regs[b + k] for k in range(c))
            elif op == 13:                      # INDEX
                regs[a] = regs[b][regs[c]]
            elif op == 54:                      # INDEX_INT (typed; same semantics)
                regs[a] = regs[b][regs[c]]
            elif op == 55:                      # SETINDEX_INT (typed; same semantics)
                regs[a][regs[b]] = regs[c]
            elif op == 56:                      # ACC_ADD_GINT (typed global accumulate)
                self.globals[a] = self.globals[a] + regs[b][regs[c]]
            elif op == 57:                      # ACC_MAC_GINT (typed multiply-accumulate)
                self.globals[a] = self.globals[a] + \
                    regs[b // 4096][regs[b % 4096]] * regs[c // 4096][regs[c % 4096]]
            elif op == 58:                      # ACC_ADD_G (general accumulate)
                self.globals[a] = self.globals[a] + regs[b]
            elif op == 67:                      # ACC_ADD_L (local accumulate)
                regs[a] = regs[a] + regs[b]
            elif op == 68:                      # ACC_SUB_L (local accumulate)
                regs[a] = regs[a] - regs[b]
            elif op == 14:                      # SETINDEX
                regs[a][regs[b]] = regs[c]
            elif op == 15:                      # ITER_NEW
                regs[a] = [list(regs[b]), 0]
            elif op == 16:                      # ITER_NEXT
                it = regs[b]
                if it[1] < len(it[0]):
                    regs[a] = it[0][it[1]]; it[1] += 1
                else:
                    pc = c
            elif op == 17:                      # CONTAINS
                regs[a] = regs[c] in regs[b]
            elif op == 18:                      # LIST_APPEND
                regs[a].append(regs[b])
            elif op == 19:                      # SET_ADD
                regs[a].add(regs[b])
            elif op == 50:                      # LOAD_ATTR
                obj = regs[b]; nm = self._const(c)
                if isinstance(obj, tuple) and len(obj) == 2 and nm == "__name__":
                    if obj[0] == "class":
                        cn = self.prog["classes"][obj[1]]["cname"]
                        di = cn.rfind("$")          # strip any link prefix ($ast$)
                        regs[a] = cn[di + 1:] if di >= 0 else cn
                    elif obj[0] == "builtin":
                        regs[a] = [k for k, v in BUILTINS.items()
                                   if v == obj[1]][0]
                    else:
                        regs[a] = obj.attrs[nm]
                elif isinstance(obj, _Inst):
                    regs[a] = obj.attrs[nm]
                else:
                    regs[a] = getattr(obj, nm)   # host object attribute (ref VM)
            elif op == 51:                      # STORE_ATTR
                regs[a].attrs[self._const(c)] = regs[b]
            elif op == 52:                      # LOAD_METHOD
                obj = regs[b]; name = self._const(c)
                if isinstance(obj, _Inst):
                    regs[a] = ("bound", self._lookup_method(obj.cid, name), obj)
                elif name in METHODS:            # builtin method on a container/str
                    regs[a] = ("boundb", METHODS[name], obj)
                else:
                    m = getattr(obj, name, None)  # host object method (ref VM)
                    if m is not None:
                        regs[a] = ("hostb", m)
                    else:
                        self._raise_attr_error(name)
            elif op == 53:                      # CALL_METHOD (fused load+call)
                nargs = c % 256; name = self._const(c // 256)
                recv = regs[b]
                ca = [regs[b + 1 + k] for k in range(nargs)]
                if isinstance(recv, _Inst):
                    regs[a] = self._call(
                        self._lookup_method(recv.cid, name), [recv] + ca)
                elif name in METHODS:
                    regs[a] = self._method(METHODS[name], [recv] + ca)
                else:
                    m = getattr(recv, name, None)  # host object method (ref VM)
                    if m is not None:
                        regs[a] = m(*ca)
                    else:
                        self._raise_attr_error(name)
            elif op == 20: regs[a] = regs[b] + regs[c]
            elif op == 21: regs[a] = regs[b] - regs[c]
            elif op == 22: regs[a] = regs[b] * regs[c]
            elif op == 23:
                lb = regs[b]
                if isinstance(lb, _Inst):
                    m = self._lookup_method(lb.cid, "__truediv__")
                    regs[a] = self._call(m, [lb, regs[c]])
                else:
                    regs[a] = lb / regs[c]
            elif op == 24: regs[a] = regs[b] % regs[c]
            elif op == 25: regs[a] = regs[b] // regs[c]
            elif op == 26: regs[a] = regs[b] ** regs[c]
            elif op == 27: regs[a] = regs[b] | regs[c]    # int-or / set union
            elif op == 28: regs[a] = regs[b] & regs[c]    # int-and / set inter
            elif op == 29: regs[a] = regs[b] ^ regs[c]    # int-xor / set symdiff
            elif op == 36: regs[a] = regs[b] << regs[c]
            elif op == 37: regs[a] = regs[b] >> regs[c]
            elif op == 38:                                # SLICE seq[lo:hi:step]
                seq = regs[a]; lo = regs[b]; hi = regs[b + 1]; step = regs[b + 2]
                regs[a] = seq[lo:hi:step]                 # lo/hi may be None (sentinel)
            elif op == 39:                                # SLICE_STORE seq[lo:hi]=val
                seq = regs[a]; lo = regs[b]; hi = regs[b + 1]; val = regs[b + 2]
                seq[lo:hi] = val                          # lo/hi may be None (sentinel)
            elif op == 30: regs[a] = regs[b] < regs[c]
            elif op == 31: regs[a] = regs[b] <= regs[c]
            elif op == 32: regs[a] = regs[b] > regs[c]
            elif op == 33: regs[a] = regs[b] >= regs[c]
            elif op == 34: regs[a] = regs[b] == regs[c]
            elif op == 35: regs[a] = regs[b] != regs[c]
            elif op == 59:                                # IS: tag-strict identity
                _isx = regs[b]; _isy = regs[c]
                if type(_isx) is not type(_isy):
                    regs[a] = False                       # e.g. 1 is True -> False
                elif isinstance(_isx, (int, float, str, bool)) or _isx is None:
                    regs[a] = (_isx == _isy)              # value identity for immutables
                else:
                    regs[a] = (_isx is _isy)              # object identity otherwise
            elif op == 40: regs[a] = -regs[b]
            elif op == 41: regs[a] = not regs[b]
            elif op == 60: regs[a] = regs[b] + regs[c]   # ADD_NN
            elif op == 61: regs[a] = regs[b] - regs[c]
            elif op == 62: regs[a] = regs[b] * regs[c]
            elif op == 63: regs[a] = regs[b] < regs[c]   # LT_NN
            elif op == 64: regs[a] = regs[b] <= regs[c]
            elif op == 65: regs[a] = regs[b] > regs[c]
            elif op == 66: regs[a] = regs[b] >= regs[c]
            elif op == 70:                      # SETUP_EXCEPT
                blocks.append(a)
            elif op == 71:                      # POP_BLOCK
                blocks.pop()
            elif op == 72:                      # RAISE
                v = regs[a]
                if isinstance(v, tuple) and v and v[0] == "class":
                    v = self._invoke(v, [])
                self._exc_val = v; self._exc_flag = True
            elif op == 73:                      # RERAISE
                self._exc_flag = True
            elif op == 74:                      # LOAD_EXC
                regs[a] = self._exc_val
            elif op == 75:                      # EXC_MATCH
                regs[a] = self._isinstance(self._exc_val, regs[b])
            else:
                raise RuntimeError("unknown op %d" % op)
            if self._exc_flag:                  # an exception is in flight
                if blocks:
                    pc = blocks.pop()           # jump to nearest handler
                    self._exc_flag = False
                else:
                    return None                 # propagate to caller
        return None

    def _bind_kwargs(self, callee, posvals, kwnames, kwvals):
        # resolve positional + keyword args into a complete positional list,
        # mirroring interp.bind_kwargs (self is prepended later by _invoke)
        fidx = None
        self_off = 0
        if isinstance(callee, tuple):
            if callee[0] == "func":
                fidx = callee[1]; self_off = 0
            elif callee[0] == "class":
                fidx = self._lookup_method(callee[1], "__init__"); self_off = 1
            elif callee[0] == "bound":
                fidx = callee[1]; self_off = 1
        if fidx is None or fidx < 0:
            return list(posvals) + list(kwvals)
        fn = self.funcs[fidx]
        params = fn.get("params", [])
        nvis = fn["nparams"] - self_off
        defs = fn.get("defaults", [])
        out = []
        for p in range(nvis):
            if p < len(posvals):
                out.append(posvals[p])
            else:
                found = -1
                for j in range(len(kwnames)):
                    if self_off + p < len(params) and params[self_off + p] == kwnames[j]:
                        found = j
                        break
                if found >= 0:
                    out.append(kwvals[found])
                elif self_off + p < len(defs) and defs[self_off + p] >= 0:
                    out.append(self._const(defs[self_off + p]))
                else:
                    out.append(None)
        return out

    def _invoke(self, callee, args):
        if isinstance(callee, tuple) and callee[0] == "func":
            return self._call(callee[1], args)
        if isinstance(callee, tuple) and callee[0] == "builtin":
            return self._builtin(callee[1], args)
        if isinstance(callee, tuple) and callee[0] == "class":
            cid = callee[1]
            inst = _Inst(cid, self.prog["classes"][cid]["cname"])
            init = self._lookup_method(cid, "__init__")
            if init is not None:
                self._call(init, [inst] + args)
            elif self._chain_has(cid, "BaseException"):
                inst.attrs["args"] = tuple(args)
                inst.is_exc = True
                inst.exc_keyerror = self._chain_has(cid, "KeyError")
            return inst
        if isinstance(callee, tuple) and callee[0] == "bound":
            return self._call(callee[1], [callee[2]] + args)
        if isinstance(callee, tuple) and callee[0] == "boundb":
            return self._method(callee[1], [callee[2]] + args)
        if isinstance(callee, tuple) and callee[0] == "hostb":
            return callee[1](*args)          # host bound method (already bound)
        if not isinstance(callee, tuple):
            # host builtin / function bound into a global on the reference VM
            # (e.g. open): the transpiler logic runs on minipy bytecode; only
            # the host syscall delegates, exactly as native minipy would via libc.
            return callee(*args)
        raise RuntimeError("not callable: %r" % (callee,))

    def _chain_has(self, cid, name):
        classes = self.prog["classes"]
        while cid >= 0:
            if classes[cid]["cname"] == name:
                return True
            cid = classes[cid]["base"]
        return False

    def _lookup_method(self, cid, name):
        classes = self.prog["classes"]
        while cid >= 0:
            ci = classes[cid]
            for m in ci["methods"]:
                if m["mname"] == name:
                    return m["mfunc"]
            cid = ci["base"]
        return None

    def _disp(self, x):
        # str()/print of an instance dispatches to __str__ when the class defines
        # one (falling back to the default <Class object> rendering otherwise)
        if isinstance(x, _Inst) and not getattr(x, "is_exc", False):
            m = self._lookup_method(x.cid, "__str__")
            if m is not None:
                return _pystr(self._call(m, [x]))
        return _pystr(x)

    def _raise_attr_error(self, name):
        # MINIPY_TRACE_ATTR=1 names the attribute. Worth keeping: a missing
        # attribute surfaces at top level as "unhandled exception: Exception"
        # with no message and no location, because the fallback below has to
        # use a bare Exception when the program registers no AttributeError
        # class. That diagnostic is unactionable on its own -- this is what
        # turned it into "sys.exit".
        import os as _dbgos
        import sys as _dbgsys
        if _dbgos.environ.get("MINIPY_TRACE_ATTR"):
            _dbgsys.stderr.write("[attr-error] " + repr(name) + "\n")
        # Set a catchable exception (matches `except Exception`) for a method
        # call on something that has no such method -- e.g. a method on an
        # unbound name left by an unlinked `import`. Mirrors CPython raising
        # AttributeError, so guarded `try/except` blocks skip cleanly instead of
        # crashing the interpreter with a host-level KeyError.
        classes = self.prog["classes"]
        cid = -1
        fallback = -1
        i = 0
        while i < len(classes):
            cn = classes[i].get("cname", "")
            di = cn.rfind("$")
            short = cn[di + 1:] if di >= 0 else cn
            if short == "AttributeError":
                cid = i
            elif short == "Exception":
                fallback = i
            i += 1
        use = cid if cid >= 0 else fallback
        self._exc_val = self._invoke(("class", use), []) if use >= 0 else None
        self._exc_flag = True

    def _isinstance(self, exc, classval):
        if not isinstance(exc, _Inst):
            return False
        if not (isinstance(classval, tuple) and classval and classval[0] == "class"):
            return False
        target = classval[1]
        cid = exc.cid
        classes = self.prog["classes"]
        while cid >= 0:
            if cid == target:
                return True
            cid = classes[cid]["base"]
        return False

    def _builtin(self, bid, args):
        if bid >= 100:
            return self._method(bid, args)
        name = [k for k, v in BUILTINS.items() if v == bid][0]
        if name == "print":
            self.out.write(" ".join(self._disp(x) for x in args) + "\n")
            return None
        if name == "len":   return len(args[0])
        if name == "range": return list(range(*args))
        if name == "int":   return int(args[0]) if args else 0
        if name == "str":   return self._disp(args[0]) if args else ""
        if name == "float": return float(args[0]) if args else 0.0
        if name == "abs":   return abs(args[0])
        if name == "bool":  return bool(args[0]) if args else False
        if name == "list":  return list(args[0]) if args else []
        if name == "dict":  return dict(args[0]) if args else {}
        if name == "set":   return set(args[0]) if args else set()
        if name == "frozenset": return set(args[0]) if args else set()
        if name == "tuple": return tuple(args[0]) if args else ()
        if name == "repr":  return repr(args[0])
        if name == "sorted": return sorted(args[0])
        if name == "sum":   return sum(args[0])
        if name == "min":   return min(args[0]) if len(args) == 1 else min(args)
        if name == "max":   return max(args[0]) if len(args) == 1 else max(args)
        if name == "isinstance": return self._isinst(args[0], args[1])
        if name == "enumerate": return [(i, x) for i, x in enumerate(args[0])]
        if name == "zip":   return [tuple(t) for t in zip(*args)]
        if name == "any":   return any(args[0])
        if name == "all":   return all(args[0])
        if name == "ord":   return ord(args[0])
        if name == "chr":   return chr(args[0])
        if name == "reversed": return list(reversed(args[0]))
        if name == "getattr":  return self._getattr(args)
        if name == "setattr":  return self._setattr(args)
        if name == "hasattr":
            obj = args[0]
            return (isinstance(obj, _Inst)
                    and (args[1] in obj.attrs
                         or self._lookup_method(obj.cid, args[1]) is not None))
        if name == "type":
            return self._typeof(args[0])
        if name == "__re_search" or name == "__re_match":
            # Reference VM half of the regex seam. interp.py implements these
            # with a runtime-valued re.search/re.match that py2c lowers to
            # crust_re; here the stdlib stands in, which is what makes the
            # cpython/ref/native comparison meaningful rather than circular.
            import re as _re
            pat, txt = args[0], args[1]
            m = (_re.match(pat, txt) if name == "__re_match"
                 else _re.search(pat, txt))
            if m is None:
                return None
            ng = m.re.groups
            return ([m.group(i) for i in range(ng + 1)]
                    + [x for i in range(ng + 1) for x in m.span(i)])
        raise RuntimeError("unknown builtin id %d" % bid)

    def _typeof(self, x):
        # Mirror the native interp: type(x) is the type's builtin value
        # (("builtin", id), comparable to the bare name `int`/`str`/...), or
        # ("class", cid) for a user instance.
        if isinstance(x, _Inst):           return ("class", x.cid)
        if isinstance(x, bool):            return ("builtin", BUILTINS["bool"])
        if isinstance(x, int):             return ("builtin", BUILTINS["int"])
        if isinstance(x, float):           return ("builtin", BUILTINS["float"])
        if isinstance(x, str):             return ("builtin", BUILTINS["str"])
        if isinstance(x, list):            return ("builtin", BUILTINS["list"])
        if isinstance(x, dict):            return ("builtin", BUILTINS["dict"])
        if isinstance(x, set):             return ("builtin", BUILTINS["set"])
        if isinstance(x, tuple):           return ("builtin", BUILTINS["tuple"])
        return ("builtin", -1)

    def _isinst(self, obj, spec):
        if isinstance(spec, tuple) and spec and spec[0] == "class":
            return self._isinstance(obj, spec)
        if isinstance(spec, tuple) and spec and spec[0] == "builtin":
            return self._isinst_type(obj, spec[1])
        if isinstance(spec, tuple):                  # tuple of types
            for s in spec:
                if self._isinst(obj, s):
                    return True
            return False
        return False

    def _isinst_type(self, obj, bid):
        nm = [k for k, v in BUILTINS.items() if v == bid][0]
        if nm == "int":   return isinstance(obj, int)
        if nm == "float": return isinstance(obj, float)
        if nm == "str":   return isinstance(obj, str)
        if nm == "bool":  return isinstance(obj, bool)
        if nm == "list":  return isinstance(obj, list)
        if nm == "dict":  return isinstance(obj, dict)
        if nm == "set":   return isinstance(obj, set)
        if nm == "tuple": return isinstance(obj, tuple)
        return False

    def _getattr(self, args):
        obj, attr = args[0], args[1]
        if isinstance(obj, _Inst):
            if attr in obj.attrs:
                return obj.attrs[attr]
            fidx = self._lookup_method(obj.cid, attr)
            if fidx is not None:
                return ("bound", fidx, obj)
        if len(args) >= 3:
            return args[2]
        return None

    def _setattr(self, args):
        obj = args[0]
        if isinstance(obj, _Inst):
            obj.attrs[args[1]] = args[2]
        return None

    def _method(self, mid, args):
        name = [k for k, v in METHODS.items() if v == mid][0]
        recv = args[0]; rest = args[1:]
        if name == "append": recv.append(rest[0]); return None
        if name == "pop":    return recv.pop(*rest)
        if name == "get":    return recv.get(*rest)
        if name == "keys":   return list(recv.keys())
        if name == "values": return list(recv.values())
        if name == "items":  return [list(t) for t in recv.items()]
        if name == "add":    recv.add(rest[0]); return None
        if name == "split":  return recv.split(*rest) if rest else recv.split()
        if name == "join":   return recv.join(rest[0])
        if name == "strip":  return recv.strip(*rest)
        if name == "startswith": return recv.startswith(rest[0])
        if name == "endswith":   return recv.endswith(rest[0])
        if name == "find":   return recv.find(*rest)
        if name == "replace": return recv.replace(rest[0], rest[1])
        if name == "upper":  return recv.upper()
        if name == "lower":  return recv.lower()
        if name == "extend": recv.extend(rest[0]); return None
        if name == "insert": recv.insert(rest[0], rest[1]); return None
        if name == "index":  return recv.index(*rest)
        if name == "count":  return recv.count(rest[0])
        if name == "update": recv.update(rest[0]); return None
        if name == "setdefault": return recv.setdefault(*rest)
        if name == "splitlines": return recv.splitlines()
        if name == "rstrip": return recv.rstrip(*rest)
        if name == "lstrip": return recv.lstrip(*rest)
        if name == "isdigit": return recv.isdigit()
        if name == "isupper": return recv.isupper()
        if name == "islower": return recv.islower()
        if name == "isalpha": return recv.isalpha()
        if name == "isalnum": return recv.isalnum()
        raise RuntimeError("unknown method id %d" % mid)


class _Inst:
    """A minipy instance in the reference VM: a class id plus an attribute map.
    (The native interpreter stores the same thing in its container heap.)"""
    def __init__(self, cid, name):
        self.cid = cid
        self.name = name
        self.attrs = {}


def _pystr(x):
    if x is True:  return "True"
    if x is False: return "False"
    if x is None:  return "None"
    if isinstance(x, _Inst):
        if getattr(x, "is_exc", False):
            a = x.attrs.get("args", ())
            if len(a) == 0:
                return ""
            if len(a) == 1:
                return repr(a[0]) if getattr(x, "exc_keyerror", False) else _pystr(a[0])
            return _pystr(a)
        return "<%s object>" % x.name
    return str(x)


# ---- disassembler (debug) -------------------------------------------------
def disassemble(prog):
    lines = []
    for fi, fn in enumerate(prog["funcs"]):
        lines.append("func %d %s(nparams=%d, nregs=%d)"
                     % (fi, fn["name"], fn["nparams"], fn["nregs"]))
        for pc, ins in enumerate(fn["code"]):
            lines.append("  %3d  %-12s %3d %3d %3d"
                         % (pc, OPNAME.get(ins["op"], "?%d" % ins["op"]),
                            ins["a"], ins["b"], ins["c"]))
    return "\n".join(lines)
