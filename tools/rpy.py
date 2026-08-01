"""rpy -- CPython-side helpers for rpython (py2c) code generation.

This module is **CPython-only**. It is never translated to C: py2c.py does not
compile `import rpy`; it only *recognizes* the call `rpy.json.generate_decoder(Cls)`
(and `generate_encoder`) used as an argument and, knowing `Cls`'s field layout,
emits a specialized C parser that builds the POD struct directly -- no dict, no
boxing, no Python callback.

Running the same source under plain CPython must still work and produce the same
objects, so the functions here implement faithful runtime behavior:

    import json, rpy
    class User:
        def __init__(self, name: "char*", age: "int"):
            self.name = name
            self.age = age

    hook = rpy.json.generate_decoder(User)
    u = json.loads('{"name": "ada", "age": 36}', object_hook=hook)   # -> User

The encoder side lets a plain-Python *server* emit data an rpython *client* can
read with the matching generated parser:

    enc = rpy.json.generate_encoder(User)
    json.dumps(User("ada", 36), default=enc)   # -> '{"name": "ada", "age": 36}'
"""

import inspect


def _ctor_fields(cls):
    """Ordered constructor field names of `cls` (its __init__ params minus
    self). These are the slots py2c lays out in the POD struct, in order."""
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return []
    names = list(sig.parameters)
    return names[1:] if names and names[0] == "self" else names


def _ctor_annotations(cls):
    """Map constructor field name -> its annotation (as written). Used to find
    class-typed fields (e.g. `addr: "Addr"`) for nested decoding."""
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return {}
    return {n: p.annotation for n, p in sig.parameters.items()
            if p.annotation is not inspect.Parameter.empty}


def _ann_class(cls, ann):
    """If annotation `ann` (a string like "Addr") names a class reachable from
    where `cls` is defined, return that class; else None. Lets the decoder build
    nested objects without the caller wiring them up explicitly."""
    if not isinstance(ann, str):
        return ann if isinstance(ann, type) else None
    name = ann.strip().strip("'\"").rstrip("*").strip()
    g = getattr(cls.__init__, "__globals__", {})
    obj = g.get(name)
    return obj if isinstance(obj, type) else None


def _ann_list_class(cls, ann):
    """If annotation `ann` is `list[Cls]` / `List[Cls]` naming a known class,
    return that class; else None. Mirrors the generated decoder building a list
    of nested objects."""
    if not isinstance(ann, str):
        return None
    a = ann.strip().strip("'\"")
    if "[" not in a:
        return None
    head, _, rest = a.partition("[")
    if head.strip() not in ("list", "List"):
        return None
    elem = rest.rsplit("]", 1)[0].strip()
    return _ann_class(cls, elem)


def _build(cls, dct):
    """Recursively construct `cls` from dict `dct`: a constructor field whose
    annotation names a class (value a dict) is built recursively, and a
    `list[Cls]` field (value a list) builds each element. Mirrors the generated
    C decoder, which calls the nested class's decoder inline / per array element.
    """
    args = []
    anns = _ctor_annotations(cls)
    for f in _ctor_fields(cls):
        v = dct[f]
        ann = anns.get(f)
        sub = _ann_class(cls, ann)
        if sub is not None and isinstance(v, dict):
            v = _build(sub, v)
        else:
            elem = _ann_list_class(cls, ann)
            if elem is not None and isinstance(v, list):
                v = [_build(elem, e) if isinstance(e, dict) else e for e in v]
        args.append(v)
    return cls(*args)


class _Json:
    """Namespace exposed as `rpy.json`."""

    def generate_decoder(self, cls):
        """Return a json `object_hook` that builds `cls` from a parsed dict,
        recursively constructing nested class-typed fields.

        Under CPython this is a real callable used by `json.loads`. Under py2c
        the *call* `rpy.json.generate_decoder(cls)` is intercepted: the
        translator reads `cls`'s fields and generates a C parser instead, so the
        returned function is never actually invoked in compiled code.

        Note on object_hook semantics: json calls the hook bottom-up, so a nested
        object's dict reaches this hook first. It is returned unchanged (it does
        not have the *root* class's fields), then converted when the enclosing
        object is built -- matching how the generated parser recurses top-down.
        """
        fields = _ctor_fields(cls)

        def object_hook(dct):
            for f in fields:
                if f not in dct:
                    return dct      # not a `cls`-shaped object; leave as dict
            return _build(cls, dct)

        return object_hook

    def generate_encoder(self, cls):
        """Return a json `default` function that serializes `cls` instances to a
        plain object with its constructor fields (nested class instances are
        serialized recursively by json once this default is applied). For a
        Python server feeding an rpython client."""
        def default(obj):
            fields = _ctor_fields(type(obj))
            if fields:
                return {f: getattr(obj, f) for f in fields}
            raise TypeError(
                "Object of type %s is not JSON serializable"
                % type(obj).__name__)

        return default


json = _Json()


class _Threads:
    """Namespace exposed as ``rpy.threads`` for ShivyCX's register-partitioned
    bare-metal threads.

    Under CPython the decorators are identity wrappers (they only record the
    side/core on the function for introspection) and ``start_new_thread`` spawns
    a real OS thread via ``_thread.start_new_thread`` -- so the same source runs,
    semi-faithfully, on the host.

    Under py2c the ``@rpy.threads.left(core=N)`` / ``.right(core=N)`` decorator
    is *recognized*: the translator strips it and emits the equivalent
    ``assert FN in threads.left(core=N)`` partition contract in ``main``'s header
    (guarded by ``#ifdef __SHIVYC__`` so gcc still accepts the C), and a
    ``rpy.threads.start_new_thread(fn)`` call lowers to a direct ``fn()``. The
    contract is what ShivyCX's thread-partition analysis reads to split the
    register file between the two threads (see shivyc/thread_contracts.py).
    """

    def left(self, core=0):
        def deco(fn):
            fn.__rpy_thread__ = ("left", core)
            return fn
        return deco

    def right(self, core=0):
        def deco(fn):
            fn.__rpy_thread__ = ("right", core)
            return fn
        return deco

    def start_new_thread(self, fn, args=()):
        """Spawn `fn(*args)` on a new OS thread (CPython). The translator instead
        lowers `start_new_thread(fn)` to a direct call `fn()`."""
        import _thread
        return _thread.start_new_thread(fn, tuple(args))


threads = _Threads()


# ===========================================================================
# rpy.minipy -- driver for the tiny rpython Python interpreter.
#
# `py2json_bytecode(path)` is the AOT front end: it turns a .py file into the
# *flattened, pre-optimised bytecode* JSON the interpreter consumes (NOT a raw
# AST tree). See tools/minipy/compiler.py for the format and a reference VM.
#
# The command-line driver `python3 tools/rpy.py S.py [args...]` runs S.py:
#   1. hash S.py + the project .py files it imports (md5),
#   2. reuse /tmp/<md5>.minipy.json if fresh, else compile and cache it,
#   3. execute, forwarding argv;  `-i` drops into a REPL afterwards.
#
# Today execution uses the CPython reference VM (backend="ref"); the seam marked
# below is where a per-script py2c-compiled interpreter (/tmp/<md5>.interp.bin)
# will plug in as backend="native".
# ===========================================================================
import os as _os
import sys as _sys


def _minipy_compiler():
    """Import tools/minipy.compiler regardless of how rpy.py was launched."""
    here = _os.path.dirname(_os.path.abspath(__file__))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    from minipy import compiler as C
    return C


def py2json_bytecode(path):
    """Compile `path` to the interpreter's flattened-bytecode JSON (a string)."""
    import json as _json
    C = _minipy_compiler()
    return _json.dumps(C.compile_file(path))


def _project_files(path):
    """`path` plus every sibling/subdir .py it transitively imports. Used for the
    cache key so editing any imported module rebuilds. Stdlib/3rd-party imports
    (no matching .py next to the project) are ignored on purpose."""
    import ast as _ast
    root = _os.path.dirname(_os.path.abspath(path))
    seen, queue = {}, [_os.path.abspath(path)]
    while queue:
        p = queue.pop()
        if p in seen or not _os.path.isfile(p):
            continue
        try:
            src = open(p, encoding="utf-8").read()
        except OSError:
            continue
        seen[p] = src
        try:
            tree = _ast.parse(src)
        except SyntaxError:
            continue
        mods = []
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Import):
                mods += [a.name for a in n.names]
            elif isinstance(n, _ast.ImportFrom) and n.module and n.level == 0:
                mods.append(n.module)
        for m in mods:
            cand = _os.path.join(root, m.replace(".", _os.sep) + ".py")
            if _os.path.isfile(cand):
                queue.append(_os.path.abspath(cand))
    return seen


def _cache_key(path):
    import hashlib
    files = _project_files(path)
    h = hashlib.md5()
    for p in sorted(files):
        h.update(p.encode("utf-8"))
        h.update(files[p].encode("utf-8"))
    # Fold in the minipy compiler's own source: changing the compiler changes the
    # emitted bytecode, so a source-only key would serve stale (incompatible)
    # bytecode after a compiler edit.
    try:
        cs = open(_minipy_compiler().__file__, encoding="utf-8").read()
        h.update(b"\x00compiler\x00")
        h.update(cs.encode("utf-8"))
    except OSError:
        pass
    return h.hexdigest()


def _load_or_build(path, force=False):
    """Return (program_dict, cache_path), compiling + caching on a miss."""
    import json as _json
    key = _cache_key(path)
    cache = _os.path.join("/tmp", key + ".minipy.json")
    if not force and _os.path.isfile(cache):
        with open(cache) as fh:
            return _json.load(fh), cache
    C = _minipy_compiler()
    prog = C.compile_file(path)
    # --- seam: backend="native" would AOT-generate a specialised rpython
    # interpreter here and py2c-compile it to /tmp/<key>.interp.bin. For now we
    # cache the bytecode and run it with the reference VM. ---
    with open(cache, "w") as fh:
        _json.dump(prog, fh)
    return prog, cache


def _repl(prog, vm):
    """Minimal interactive shell (`-i`). Shares *values* by name with the script
    that just ran; expression results are echoed like CPython's REPL. v0 caveat:
    functions defined in one line aren't callable from a later line yet (their
    index is program-local) -- that needs the incremental compiler."""
    import ast as _ast
    C = _minipy_compiler()
    # snapshot script globals by name
    state = {}
    for slot, nm in enumerate(prog["names"]):
        if slot < len(vm.globals) and vm.globals[slot] is not None:
            state[nm] = vm.globals[slot]
    _sys.stdout.write("minipy REPL (Ctrl-D to exit)\n")
    while True:
        try:
            line = input(">>> ")
        except EOFError:
            _sys.stdout.write("\n"); return
        if not line.strip():
            continue
        try:
            node = _ast.parse(line)
        except SyntaxError as e:
            _sys.stdout.write("SyntaxError: %s\n" % e); continue
        is_expr = len(node.body) == 1 and isinstance(node.body[0], _ast.Expr)
        src = ("__ = (%s)" % line.strip()) if is_expr else line
        try:
            p = C.compile_source(src, "<repl>")
        except C.CompileError as e:
            _sys.stdout.write("minipy: %s\n" % e); continue
        v = C.VM(p)
        for slot, nm in enumerate(p["names"]):   # seed shared state by name
            if nm in state:
                v.globals[slot] = state[nm]
        try:
            v.run()
        except Exception as e:                   # noqa: BLE001 (REPL is lenient)
            _sys.stdout.write("%s: %s\n" % (type(e).__name__, e)); continue
        for slot, nm in enumerate(p["names"]):   # read back
            if slot < len(v.globals) and v.globals[slot] is not None:
                state[nm] = v.globals[slot]
        if is_expr and "__" in state and state["__"] is not None:
            _sys.stdout.write(repr(state["__"]) + "\n")


def _ensure_native(force=False):
    """Build (once) and cache the py2c-compiled interpreter binary; return its
    path, or None if the toolchain isn't available. The v0 interpreter is
    generic, so one binary runs any script's bytecode -- it's keyed by the
    interpreter's own source, not the script. (A per-script *specialised*
    interpreter is the future optimisation; the cache key would then fold in the
    script too.)"""
    import hashlib
    import subprocess
    here = _os.path.dirname(_os.path.abspath(__file__))
    interp_src = _os.path.join(here, "minipy", "interp.py")
    if not _os.path.isfile(interp_src):
        return None
    # The binary depends on py2c.py as well as interp.py: py2c is both the
    # compiler and the source of the C runtime that gets linked in, so a change
    # to either must rebuild. Keying on interp.py alone meant a runtime fix
    # (e.g. float formatting in pystr) silently kept serving the old binary.
    _h = hashlib.md5()
    _h.update(open(interp_src, "rb").read())
    _p2c = _os.path.join(here, "py2c.py")
    if _os.path.isfile(_p2c):
        _h.update(open(_p2c, "rb").read())
    key = _h.hexdigest()[:16]
    bdir = _os.path.join("/tmp", "minipy_interp_" + key)
    binp = _os.path.join(bdir, "interp")
    if not force and _os.path.isfile(binp):
        return binp
    try:
        _os.makedirs(bdir, exist_ok=True)
        py2c = _os.path.join(here, "py2c.py")
        r = subprocess.run([_sys.executable, py2c, interp_src, "--out", bdir],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        # generate the mp-bridge-free runtime, then compile + link
        subprocess.run(
            [_sys.executable, "-c",
             "import sys;sys.path.insert(0,%r);import py2c;py2c.write_runtime(%r)"
             % (here, bdir)], capture_output=True, text=True)
        import glob
        # The interpreter's ctypes FFI builtins lower to the mb_ffi.c shim
        # (dlopen/dlsym/indirect call); drop it in so the glob links it.
        import shutil
        _mbffi = _os.path.join(here, "rpy_lib", "mb_ffi.c")
        if _os.path.isfile(_mbffi):
            shutil.copy(_mbffi, _os.path.join(bdir, "mb_ffi.c"))
        csrc = glob.glob(_os.path.join(bdir, "*.c"))
        cc = subprocess.run(["gcc", "-std=c99", "-O2", "-I", bdir] + csrc
                            + ["-o", binp], capture_output=True, text=True)
        if cc.returncode != 0 or not _os.path.isfile(binp):
            return None
        return binp
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    argv = list(_sys.argv[1:] if argv is None else argv)
    interactive = False
    rebuild = False
    backend = "native"          # default; falls back to "ref" if no toolchain
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-i":
            interactive = True
        elif a == "-B":
            rebuild = True
        elif a == "--ref":
            backend = "ref"
        elif a == "--native":
            backend = "native"
        elif a in ("-h", "--help"):
            _sys.stdout.write(
                "usage: rpy.py [-i] [-B] [--ref|--native] script.py [args...]\n"
                "  -i        interactive REPL after the script\n"
                "  -B        force rebuild (ignore /tmp caches)\n"
                "  --ref     run via the CPython reference VM\n"
                "  --native  run via the py2c-compiled interpreter (default)\n")
            return 0
        elif a.startswith("-"):
            _sys.stderr.write("rpy.py: unknown option %s\n" % a); return 2
        else:
            rest = argv[i:]
            break
        i += 1
    if not rest:
        _sys.stderr.write("rpy.py: no script given (try -h)\n"); return 2
    script, script_argv = rest[0], rest[1:]
    if not _os.path.isfile(script):
        _sys.stderr.write("rpy.py: no such file: %s\n" % script); return 2
    C = _minipy_compiler()
    prog, cache = _load_or_build(script, force=rebuild)

    # The REPL needs in-process globals, so -i always runs through the reference
    # VM (its state feeds the prompt). Plain runs default to the native binary.
    if backend == "native" and not interactive:
        binp = _ensure_native(force=rebuild)
        if binp is not None:
            import subprocess
            r = subprocess.run([binp, cache] + script_argv)
            return r.returncode
        _sys.stderr.write("rpy.py: native build unavailable, using --ref\n")

    vm = C.VM(prog)
    vm.script_argv = [script] + script_argv
    vm.run()
    if interactive:
        _repl(prog, vm)
    # An unhandled SystemExit sets exit_status; propagate it so `sys.exit(n)`
    # in the guest means the same thing as it does under CPython.
    return getattr(vm, "exit_status", 0)


if __name__ == "__main__":
    raise SystemExit(main())
