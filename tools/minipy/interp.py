"""minipy.interp -- the rpython interpreter, compiled to C by py2c.

Reads flattened-bytecode JSON (rpy.py2json_bytecode / minipy.compiler), decodes
it into POD structs via rpy.json.generate_decoder, and runs the register
dispatch loop. Runs untranslated under CPython too, so it doubles as a check on
the format and is differentially tested against the pure-Python reference VM.

This revision adds container values -- list / tuple / dict / set, subscripting,
iteration, comprehensions (lowered to loops by the compiler), membership, the
common container/string methods, and %-formatting. Containers live in a side
heap indexed from the value box, so the scalar fast path stays allocation-free.
Opcode numbers and the builtin/method tables mirror minipy/compiler.py.
"""
import re
import sys
import json
import rpy
import os


# ------------------------------------------------------------------ FFI ----
# Runtime FFI for the embedded interpreter's ctypes: a page's python can load a
# JIT-compiled <script type="rpython"> .so and call into it. These lower to the
# mb_ffi.c shim (linked into the binary; dlopen is in libc, no -ldl) via py2c's
# ctypes static bridge -- mb_dlopen/mb_dlsym resolve the .so + symbol at run
# time, mb_callNi call the resolved pointer as int(int,...). Guarded so the
# module still imports cleanly under CPython (where it is only read, never runs
# these); py2c keeps this branch (its impl name is not "cpython").
if sys.implementation.name != "cpython":
    import rpy_ctypes as _ctypes
    _ffi = _ctypes.CDLL("mb_ffi")
    _ffi.mb_dlopen.restype = _ctypes.c_long
    _ffi.mb_dlopen.argtypes = [_ctypes.c_char_p]
    _ffi.mb_dlsym.restype = _ctypes.c_long
    _ffi.mb_dlsym.argtypes = [_ctypes.c_long, _ctypes.c_char_p]
    _ffi.mb_call0i.restype = _ctypes.c_int
    _ffi.mb_call0i.argtypes = [_ctypes.c_long]
    _ffi.mb_call1i.restype = _ctypes.c_int
    _ffi.mb_call1i.argtypes = [_ctypes.c_long, _ctypes.c_int]
    _ffi.mb_call2i.restype = _ctypes.c_int
    _ffi.mb_call2i.argtypes = [_ctypes.c_long, _ctypes.c_int, _ctypes.c_int]
    _ffi.mb_call3i.restype = _ctypes.c_int
    _ffi.mb_call3i.argtypes = [_ctypes.c_long, _ctypes.c_int, _ctypes.c_int,
                               _ctypes.c_int]
    _ffi.mb_call0l.restype = _ctypes.c_int
    _ffi.mb_call0l.argtypes = [_ctypes.c_long]
    _ffi.mb_call1l.restype = _ctypes.c_int
    _ffi.mb_call1l.argtypes = [_ctypes.c_long, _ctypes.c_long]
    _ffi.mb_call2l.restype = _ctypes.c_int
    _ffi.mb_call2l.argtypes = [_ctypes.c_long, _ctypes.c_long, _ctypes.c_long]
    _ffi.mb_call3l.restype = _ctypes.c_int
    _ffi.mb_call3l.argtypes = [_ctypes.c_long, _ctypes.c_long, _ctypes.c_long,
                               _ctypes.c_long]
    _ffi.mb_call0p.restype = _ctypes.c_long
    _ffi.mb_call0p.argtypes = [_ctypes.c_long]
    _ffi.mb_call1p.restype = _ctypes.c_long
    _ffi.mb_call1p.argtypes = [_ctypes.c_long, _ctypes.c_long]
    _ffi.mb_call2p.restype = _ctypes.c_long
    _ffi.mb_call2p.argtypes = [_ctypes.c_long, _ctypes.c_long, _ctypes.c_long]
    _ffi.mb_call3p.restype = _ctypes.c_long
    _ffi.mb_call3p.argtypes = [_ctypes.c_long, _ctypes.c_long, _ctypes.c_long,
                               _ctypes.c_long]


# ====================== JSON-decoded POD structs ======================
class Const:
    def __init__(self, t: "char*", i: "long", d: "double", s: "char*"):
        self.t = t
        self.i = i
        self.d = d
        self.s = s


class Instr:
    # Fields are populated directly by the generated JSON decoder (which matches
    # JSON keys to field names), so the compiler emits ra/fb/fc already split out
    # of the encoded `a`. Stored as C bit fields: 8 bytes (two unsigned ints), half
    # the size of four plain ints, and the hot loop reads ra/fb/fc with no decode.
    def __init__(self, op: "int", fb: "int", fc: "int", ra: "int",
                 b: "int", c: "int"):
        self.op: "int(8)" = op       # opcode (<256)            -- unit 1:
        self.fb: "int(1)" = fb       # free reg b hint            8+1+1+22
        self.fc: "int(1)" = fc       # free reg c hint            = 32 bits
        self.ra: "int(22)" = ra      # real dst/src register
        self.b: "int(16)" = b        # operand b               -- unit 2:
        self.c: "int(16)" = c        # operand c                 16+16 = 32 bits


class Func:
    def __init__(self, name: "char*", nparams: "int", nregs: "int",
                 nlocals: "int", code: "list[Instr]", defaults: "list[int]",
                 vararg: "int", params: "list[str]"):
        self.name = name
        self.nparams = nparams
        self.nregs = nregs
        self.nlocals = nlocals
        self.code = code
        self.defaults = defaults
        self.vararg = vararg                 # reg index of *args param, or -1
        self.params = params                 # parameter names in positional order


class MethEnt:
    def __init__(self, mname: "char*", mfunc: "int"):
        self.mname = mname
        self.mfunc = mfunc


class ClassInfo:
    def __init__(self, cname: "char*", base: "int", methods: "list[MethEnt]"):
        self.cname = cname
        self.base = base
        self.methods = methods


class Program:
    def __init__(self, version: "int", source: "char*",
                 consts: "list[Const]", names: "list[char*]",
                 nglobals: "int", funcs: "list[Func]",
                 classes: "list[ClassInfo]", entry: "int"):
        self.version = version
        self.source = source
        self.consts = consts
        self.names = names
        self.nglobals = nglobals
        self.funcs = funcs
        self.classes = classes
        self.entry = entry


# ====================== runtime value + container heap ======================
# V.tag: 0 none, 1 int, 2 float, 3 str, 4 bool, 5 func, 6 builtin,
#        7 list, 8 dict, 9 set, 10 tuple, 11 iter.
# For containers V.iv is an index into St.heap; the scalar payload lives in
# iv/dv/sv as before so int/float/etc. need no heap allocation.
class V:
    # A tagged value. iv/dv/sv share one 8-byte slot (anonymous union): a value
    # is exactly one of int/heap-index (iv), float (dv), or string (sv), chosen
    # by `tag`, so they never need to coexist. This makes V a 16-byte
    # tag+union POD (was ~32 bytes), halving allocation size and memory traffic.
    #
    # `tag` only needs a handful of values, so it is a 2-byte short; the other
    # 2 bytes of what used to be a 4-byte tag are `jitcode`, a free per-value
    # scratch slot the interpreter uses as an inline-cache key (e.g. an object's
    # class id, so method dispatch can skip the heap deref + method scan).
    tag: "short"
    jitcode: "short"
    iv: "long"
    dv: "double"
    sv: "char*"
    _c_union_ = ("iv", "dv", "sv")

    def __init__(self, tag: "int", iv: "long"):
        self.tag = tag
        self.iv = iv


# Shared immutable singletons (V is never mutated in place), populated once by
# setup_cache() at interpreter start. Caching None/True/False and small ints
# avoids a heap V allocation on the hottest paths (comparisons, loop counters,
# default register slots).
# The range is wide because the workload says so, not out of caution. Profiling
# v_int on a self-hosted compile: 45% of the integers built fall in the old
# -8..256 window, and *every* remaining one is under 32768 -- they are bytecode
# offsets, register numbers, constant indices and string lengths, all bounded by
# program size. Extending the window to cover them turns ~3.4M heap V
# allocations into array reads. The cache itself costs one V and one list slot
# per entry, about 2 MB for the whole range, paid once at startup.
_CACHE_LO = -1024
_CACHE_HI = 65535
_cache_ready: "int" = 0
_none_v: "V" = None
_true_v: "V" = None
_false_v: "V" = None
_int_cache: "list[V]" = None
# Shared read-only empty block-stack. A function with no try/except never pushes
# a handler, so it borrows this one sentinel instead of allocating a fresh list
# per call (the dominant per-call allocation on recursion-heavy code). The first
# SETUP_EXCEPT in a frame swaps in a private list. Handler PCs are stored as
# v_int so the stack is a list[V] (uniformly boxed, unlike list[int]).
# A bound *user* method packs (instance heap index, function index) into a single
# V (tag 14) instead of allocating a heap Cont per call: iv = hidx*SHIFT + fidx.
# fidx < SHIFT (16M funcs is plenty); the instance lives on the heap already, so
# the receiver V(12, hidx) reconstructs exactly. This makes `obj.method(...)` --
# the hot path in OOP code -- allocate nothing for the bound method itself.
_METH_SHIFT = 16777216

_empty_blocks: "list[V]" = None
# Shared read-only empty dict-index. Only dicts use the buckets field; every
# other container (list/set/tuple/instance/iterator/bound method) left an empty
# list there and allocated one per container. They borrow this sentinel instead;
# a dict replaces it with its own list on first reindex.
_empty_buckets: "list[V]" = None
# A single shared empty list parked in reclaimed heap slots after their real
# items array has been freed. Sharing one avoids making the sweep an allocator
# (a fresh [] per reclaimed slot is exactly the cost the old sweep avoided by
# not freeing at all); the slot is dead, so nothing ever reads or mutates it
# before _heap_put reassigns items.
_empty_items: "list[V]" = None

# ---------------------------------------------------------------------------
# String interning
# ---------------------------------------------------------------------------
# Open-addressed table mapping short string contents to a single shared V.
# Measured on a self-hosted compile of rast.py: 5,555,888 string values are
# built and they hold 1,524 distinct contents -- 99.97% duplicates -- and 99.8%
# of them are 15 characters or shorter. Handing out one shared V per distinct
# content removes essentially all of that allocation.
#
# The table is two parallel lists rather than a dict because the interpreter has
# no dict of its own, and a py2c `dict[str, V]` would be a typed container cast
# through an obj parameter -- the struct pun that has already broken this file
# twice (the collector's mark vector, pyjoin's argument).
#
# Interned V objects are immortal by construction: _free_v only recycles tags 1
# and 2, so a string V can never reach the free list, and the char* it holds is
# arena memory that is never handed back (afree is only called for lists). So a
# shared V can be returned to any number of callers without ownership tracking.
_INTERN_BITS = 14                 # 16384 slots for ~1.5k live entries
_INTERN_MASK = 16383
_INTERN_MAX_LEN = 32              # covers 99.99% of strings built; longer ones
                                  # are rare (83 of 5.5M) and not worth hashing
_intern_v: "list[V]" = None       # slot -> shared V, or None when empty
_intern_ready = 0
_intern_count = 0
# Per-program cache of materialized constant values (filled once at startup).
_const_vs: "list[V]" = None
# Free-list of uniquely-owned, dead arithmetic temporaries (large ints / floats)
# that the compiler proved non-escaping. v_int/v_float recycle these in place
# instead of allocating, which keeps allocation-heavy loops (accumulators, float
# physics) from growing the heap. Only large ints (outside the small-int cache)
# and floats are ever placed here -- never a shared singleton, const, string, or
# container -- so the in-place mutation on reuse is safe.
_v_freelist: "list[V]" = None
_const_vs_ready: "int" = 0


# A heap cell. kind: 0 list, 1 dict (items = [k0,v0,k1,v1,...]), 2 set,
# 3 tuple, 4 iter (items = materialised elements, cursor = position).
class Cont:
    def __init__(self, kind: "int", cursor: "int", items: "list[V]",
                 buckets: "list[V]"):
        self.kind = kind
        self.cursor = cursor
        self.items = items
        self.buckets = buckets         # dict hash index (over items); else empty


class St:
    def __init__(self, prog: "Program", glob: "list[V]", heap: "list[Cont]",
                 exc_flag: "int", exc_val: "V", regpool: "list[list[V]]",
                 mcache_cls: "list[V]", mcache_fidx: "list[V]",
                 frames: "list[list[V]]", freelist: "list[V]"):
        self.prog = prog
        self.glob = glob
        self.heap = heap
        self.exc_flag = exc_flag
        self.exc_val = exc_val
        self.regpool = regpool
        # The register array of every frame *currently executing*, innermost
        # last. `regpool` is its opposite: arrays no frame is using, kept for
        # reuse. Together they account for every register array in existence.
        #
        # This exists so the whole root set is reachable from `st`. Calls
        # nest by recursion, so without it a running frame's registers live
        # only as a local of `exec_func` -- on the interpreter's own stack,
        # which is a C stack in the native build and cannot be walked. A
        # collector that could not see them would free everything the
        # running frames hold.
        #
        # Argument arrays (`callargs` and friends) are deliberately *not*
        # tracked. Every value in one was read out of the caller's registers
        # a moment earlier, so it is already reachable through the caller's
        # frame; adding them would be redundant roots, not missing ones.
        self.frames = frames
        # Heap slots the collector has reclaimed. `_heap_put` takes one from
        # here before it appends, which is what turns `st.heap` from an
        # append-only log into a table with reuse -- the whole point of the
        # collector, and the only line of it that the fast path touches.
        # Free indices are carried as `V(1, index)` and the live count is
        # `nfree`, the same shape the block stack uses -- a `list[int]`
        # field stays boxed through py2c, so popping one hands back an
        # `obj` where a `long` is needed, and the native build fails. The
        # block stack's comment ("list[int].pop is miscompiled by py2c, so
        # index by bn") is the precedent; entries above `nfree` are stale
        # and reused in place rather than popped.
        self.freelist = freelist
        self.nfree = 0
        # Collect when the heap has grown to this size. Set to a multiple of
        # the live set after each collection, so collection cost is
        # proportional to what survives rather than to what was allocated,
        # and a program holding almost nothing collects almost never.
        self.gc_next = GC_MIN_HEAP
        # Monomorphic method inline cache, indexed by method-name const id:
        # (class id seen last, resolved func idx). A CALL_METHOD whose receiver's
        # class still matches skips lookup_method entirely (no base-walk/strcmp).
        self.mcache_cls = mcache_cls
        self.mcache_fidx = mcache_fidx


def new_int_list() -> "list[int]":
    # Return the literal directly. `r = []; return r` types the local from its
    # initialiser -- a *generic* boxed list -- and the return then pointer-casts
    # it to _tlist_int*. Those are different structs ({obj* data; int len; int
    # cap;} vs {int* data; long len; long cap;}), so push/pop read data and cap
    # from the wrong offsets and realloc() an arena pointer. py2c only builds a
    # real typed list when the returned node *is* the literal (coerce_to), so
    # the local defeated it and _tlist_int_new was never called anywhere.
    return []


def new_v_list() -> "list[V]":
    r = []
    return r


def new_reg_pool() -> "list[list[V]]":
    r = []
    return r


def setup_cache() -> "int":
    global _cache_ready, _none_v, _true_v, _false_v, _int_cache, _empty_blocks
    global _empty_buckets, _empty_items, _v_freelist
    global _intern_v, _intern_ready
    _empty_blocks = new_v_list()
    _empty_buckets = new_v_list()
    _empty_items = new_v_list()
    _intern_v = new_v_list()
    ii = 0
    while ii < 16384:
        _intern_v.append(V(17, 0))          # tag 17 = empty slot sentinel
        ii = ii + 1
    _intern_ready = 1
    _v_freelist = new_v_list()
    _none_v = V(0, 0)
    _false_v = V(4, 0)
    _true_v = V(4, 1)
    c = new_v_list()
    n = _CACHE_LO
    while n <= _CACHE_HI:
        c.append(V(1, n))
        n = n + 1
    _int_cache = c
    _cache_ready = 1
    return 0


def v_none() -> "V":
    if _cache_ready != 0:
        return _none_v
    return V(0, 0)


def v_int(n: "long") -> "V":
    if _cache_ready != 0 and n >= _CACHE_LO and n <= _CACHE_HI:
        return _int_cache[n - _CACHE_LO]
    if len(_v_freelist) > 0:               # recycle a dead temp in place
        r = _v_freelist.pop()
        r.tag = 1
        r.iv = n
        return r
    return V(1, n)


def v_float(x: "double") -> "V":
    if len(_v_freelist) > 0:               # recycle a dead temp in place
        rf = _v_freelist.pop()
        rf.tag = 2
        rf.dv = x
        return rf
    r = V(2, 0)
    r.dv = x
    return r


def _free_v(v: "V"):
    # Reclaim a value the compiler proved is a dead, uniquely-owned arithmetic
    # temporary. The tag/range gate is a hard backstop: only large ints (outside
    # the shared small-int cache) and floats are ever recycled, so a singleton,
    # const, string, or container can never reach the free-list even if a hint
    # were over-applied.
    global _v_freelist
    if v.tag == 1:
        if v.iv < _CACHE_LO or v.iv > _CACHE_HI:
            _v_freelist.append(v)
    elif v.tag == 2:
        _v_freelist.append(v)


def _str_hash(s: "char*", n: "int") -> "long":
    # djb2, masked to 30 bits so the arithmetic stays in range under CPython too
    # (interp.py is ordinary Python as well as py2c input, and an unmasked
    # multiply would grow unbounded there while wrapping in C).
    h = 5381
    i = 0
    while i < n:
        h = (h * 33 + ord(s[i])) & 1073741823
        i = i + 1
    return h


def v_str(t: "char*") -> "V":
    if _intern_ready != 0:
        n = len(t)
        if n <= _INTERN_MAX_LEN:
            slot = _str_hash(t, n) & _INTERN_MASK
            probe = 0
            while probe < 8:
                cur = _intern_v[slot]
                if cur.tag == 17:              # empty slot -> install
                    r = V(3, 0)
                    r.sv = t
                    _intern_v[slot] = r
                    return r
                if _strcmp(cur.sv, t) == 0:
                    return cur                 # shared, immortal
                slot = (slot + 1) & _INTERN_MASK
                probe = probe + 1
            # 8 collisions: fall through and allocate an uninterned V rather
            # than evicting. Eviction would hand out a second V for a content
            # already shared elsewhere, which is harmless, but probing further
            # costs more than the allocation saves at this load factor.
    r = V(3, 0)
    r.sv = t
    return r


def v_bool(b: "int") -> "V":
    if _cache_ready != 0:
        if b:
            return _true_v
        return _false_v
    return V(4, 1 if b else 0)


def v_func(idx: "long") -> "V":
    return V(5, idx)


def v_builtin(bid: "long") -> "V":
    return V(6, bid)


# Precise, non-moving mark-sweep over `st.heap`.
#
# `st.heap` is a handle table: a container V holds an *index* into it, never
# an address (see `cont_of`). That is the expensive half of a collector
# already built. Enumerating the heap is a loop over indices -- no object
# headers, no walking an arena, no knowing where objects begin -- and
# nothing has to move, because freeing a slot and reusing its index changes
# no V anywhere.
#
# Off by default (`GC_ON`). A program that allocates few containers should
# not pay for a collector, and the growth trigger below is what makes that
# true even when it is on.
# Plain assignments, not annotated ones: py2c lowers `NAME = <int>` at
# module level to a C integer constant (`_METH_SHIFT` above is the
# precedent), while an annotated module global becomes a boxed `obj` -- and
# `live * GC_GROWTH` on an obj is a call into the boxed-arithmetic runtime
# that the C compiler then rightly refuses. Found by *building* the
# transpiled C, not by transpiling it; the transpile alone passed.
GC_ON = 1
GC_MIN_HEAP = 4096                # never collect below this; startup is noise
GC_GROWTH = 2                     # collect again at this multiple of live


def _gc_mark(v: "V", marks: "list[int]", work: "list[int]") -> "None":
    """Mark the heap slot `v` refers to, if it refers to one.

    The tag says whether it does. 7..12 (list, dict, set, tuple, iter,
    instance), 15 (bound builtin) and 16 (file object) carry a heap index
    directly. 14 (bound method) *packs* one -- `iv = hidx * _METH_SHIFT +
    fidx` -- so it has to be divided out; missing that would collect the
    receiver of every bound method while the binding still pointed at it. 13
    is a class id and not an index at all. Everything below 7 is an immediate.

    Tag 16 was absent here until the collector was actually switched on: a
    file object is a container like any other (`v_container(st, 16, 8, ...)`),
    so leaving it unmarked made every open file collectable while still live.
    """
    t = v.tag
    if t == 14:
        h = v.iv // _METH_SHIFT
    elif t == 15 or t == 16:
        h = v.iv
    elif t >= 7 and t <= 12:
        h = v.iv
    else:
        return
    if h < 0 or h >= len(marks):
        return
    if marks[h] != 0:
        return
    marks[h] = 1
    work.append(h)


def _gc_mark_list(arr: "list[V]", marks: "list[int]", work: "list[int]") -> "None":
    i = 0
    while i < len(arr):
        _gc_mark(arr[i], marks, work)
        i = i + 1


def _gc_mark_frame_blocks(st: "St", blocks: "list[V]", bn: "int") -> "None":
    """The per-frame block stack is *not* a root, and this documents why.

    Its entries are handler program counters -- `pc = blocks[bn].iv` -- so
    they are immediates, not heap indices, and marking them would be a
    no-op. It is called at the safepoint anyway so that if the block stack
    ever grows to carry a value (a `with` manager, say), the place that
    would have to change is already named instead of silently wrong.
    """
    return None


def gc_collect(st: "St") -> "long":
    """Mark from the roots, sweep the rest onto the free list. Returns how
    many slots were reclaimed.

    The roots are every V-array reachable from `st` *that a running program
    can still read*:

      * `st.glob`, the module globals;
      * `_const_vs`, the materialised constants (a constant tuple is a
        container like any other);
      * `st.exc_val`, the exception in flight;
      * the method caches;
      * `st.frames` -- the registers of every frame currently executing.

    `st.regpool` is deliberately NOT a root, and neither is `_v_freelist`.
    Both hold values that are dead by construction: a pooled register array
    is fully re-initialised before it is read (parameters overwrite theirs,
    other named locals are cleared to None, temps are written before read),
    so nothing ever observes a stale slot. Tracing them would keep the last
    call's garbage alive, which on allocation-heavy loops is most of the
    heap -- the conservative-root problem, and the reason those two are
    called out here rather than left to be inferred.
    """
    n = len(st.heap)
    marks = new_int_list()
    i = 0
    while i < n:
        marks.append(0)
        i = i + 1
    work = new_int_list()
    _gc_mark_list(st.glob, marks, work)
    _gc_mark_list(st.mcache_cls, marks, work)
    _gc_mark_list(st.mcache_fidx, marks, work)
    if _const_vs_ready != 0:
        _gc_mark_list(_const_vs, marks, work)
    _gc_mark(st.exc_val, marks, work)
    fi = 0
    while fi < len(st.frames):
        _gc_mark_list(st.frames[fi], marks, work)
        fi = fi + 1
    while len(work) > 0:
        h = work.pop()
        c = st.heap[h]
        _gc_mark_list(c.items, marks, work)
        _gc_mark_list(c.buckets, marks, work)
    freed = 0
    live = 0
    j = 0
    while j < n:
        if marks[j] != 0:
            live = live + 1
        else:
            c = st.heap[j]
            if c.kind >= 0:               # not already on the free list
                c.kind = -1               # -1 marks a slot as free
                c.cursor = 0
                # Hand the items array back to the arena. Reclaiming the heap
                # *slot* alone left the list itself -- struct plus backing
                # array -- allocated forever, so peak memory tracked total
                # allocations instead of the live set: a loop building and
                # dropping one list at a time still grew without bound. The
                # slot is unreachable at this point (marking starts from the
                # roots and did not reach it), so nothing can observe the
                # freed array; `items` is reassigned by the next _heap_put.
                del c.items
                c.items = _empty_items
                c.buckets = _empty_buckets
                if st.nfree < len(st.freelist):
                    _lset(st.freelist, st.nfree, V(1, j))
                else:
                    st.freelist.append(V(1, j))
                st.nfree = st.nfree + 1
                freed = freed + 1
        j = j + 1
    nxt = live * GC_GROWTH
    if nxt < GC_MIN_HEAP:
        nxt = GC_MIN_HEAP
    st.gc_next = nxt
    # Release the mark vector and work stack. They are `list[int]`, which lowers
    # to a libc-malloc'd _tlist_int rather than an arena block, so they are the
    # one thing in a collection the arena does not reclaim in bulk: without this
    # every cycle leaked a vector the size of the heap, and peak memory grew
    # with the number of collections even though the arena itself stayed flat.
    # They cannot be hoisted to module scope and reused instead -- py2c boxes a
    # `list[int]` global or field back to a generic list (see the freelist
    # comment in St) and then casts it to _tlist_int*, which is the type pun
    # that broke the collector in the first place.
    del marks
    del work
    return freed


def _heap_put(st: "St", kind: "int", items: "list[V]") -> "long":
    # No collection here. `_heap_put` is called from the middle of building
    # a value -- `instantiate` allocates an instance and then allocates
    # again to fill it -- and at that moment the half-built object is held
    # only in an interpreter local, which is not a root. Collecting here
    # freed it. The trigger lives at the dispatch loop's safepoint instead,
    # where every live value is in a register by construction.
    if st.nfree > 0:
        st.nfree = st.nfree - 1
        h = st.freelist[st.nfree].iv
        c = st.heap[h]
        c.kind = kind
        c.cursor = 0
        c.items = items
        c.buckets = _empty_buckets
        return h
    c = Cont(kind, 0, items, _empty_buckets)
    st.heap.append(c)
    return len(st.heap) - 1


def v_container(st: "St", tag: "int", kind: "int", items: "list[V]") -> "V":
    return V(tag, _heap_put(st, kind, items))


def cont_of(st: "St", v: "V") -> "Cont":
    return st.heap[v.iv]


def items_of(st: "St", v: "V") -> "list[V]":
    return st.heap[v.iv].items


# ---- coercions / display ----
def to_float(v: "V") -> "double":
    if v.tag == 2:
        return v.dv
    return float(v.iv)


def to_int(v: "V") -> "long":
    if v.tag == 2:
        return int(v.dv)
    return v.iv


def _str_to_int(s: "char*") -> "long":
    n = len(s)
    if n == 0:
        return 0
    i = 0
    sign = 1
    if s[0] == "-":
        sign = -1
        i = 1
    elif s[0] == "+":
        i = 1
    r: "long" = 0
    while i < n and ord(s[i]) >= 48 and ord(s[i]) <= 57:
        r = r * 10 + (ord(s[i]) - 48)
        i = i + 1
    return sign * r


def _inf_val() -> "double":
    big: "double" = 1e308
    return big * 10.0          # overflows to +inf in C


def _str_to_float(s: "char*") -> "double":
    # float(s) lowers to strtod() in the compiled interpreter and is CPython's
    # own converter under the reference VM, so both are correctly rounded.
    #
    # This used to parse the string by hand, scaling the exponent with a loop of
    # `p = p * 10.0`. That accumulates rounding error: 308 multiplications turn
    # 1e308 into 9.999999999999998e+307. The comment on that loop claimed the
    # values would "round-trip exactly", which is precisely what they did not
    # do -- and repr/parse round-tripping is what a self-hosted compile depends
    # on when it writes float constants into generated code.
    return float(s)


def _fmt_float(d: "double") -> "char*":
    # str() on a double is CPython's repr under the reference VM, and
    # fmt_double() in the py2c runtime natively -- both produce the shortest
    # round-tripping decimal, with inf/-inf/nan handled. So there is nothing
    # left for this function to special-case.
    #
    # It used to shortcut integral values as str(int(d)) + ".0". That was wrong
    # twice over: int(d) is a 32-bit C int in the compiled interpreter, so any
    # value past 2**31 overflowed and fell through to the old %g path (printing
    # 123456789012345.0 as 1.23457e+14), and int(-0.0) is 0, so negative zero
    # came back as "0.0".
    return str(d)


def _cls_chain_has(st: "St", cid: "int", name: "char*") -> "int":
    classes = st.prog.classes
    c = cid
    while c >= 0:
        if _strcmp(classes[c].cname, name) == 0:
            return 1
        c = classes[c].base
    return 0


def _is_exc_class(st: "St", cid: "int") -> "int":
    # an exception is any class whose base chain reaches BaseException
    return _cls_chain_has(st, cid, "BaseException")


def to_disp(st: "St", v: "V", use_repr: "int") -> "char*":
    if v.tag == 1:
        return str(v.iv)
    if v.tag == 2:
        return _fmt_float(v.dv)
    if v.tag == 3:
        if use_repr != 0:
            return "'" + v.sv + "'"
        return v.sv
    if v.tag == 4:
        if v.iv != 0:
            return "True"
        return "False"
    if v.tag == 0:
        return "None"
    if v.tag == 7 or v.tag == 10:        # list / tuple
        items = items_of(st, v)
        opn = "["
        cls = "]"
        if v.tag == 10:
            opn = "("
            cls = ")"
        out = opn
        k = 0
        while k < len(items):
            if k > 0:
                out = out + ", "
            out = out + to_disp(st, items[k], 1)
            k = k + 1
        if v.tag == 10 and len(items) == 1:
            out = out + ","
        return out + cls
    if v.tag == 9:                       # set
        items = items_of(st, v)
        if len(items) == 0:
            return "set()"
        out = "{"
        k = 0
        while k < len(items):
            if k > 0:
                out = out + ", "
            out = out + to_disp(st, items[k], 1)
            k = k + 1
        return out + "}"
    if v.tag == 8:                       # dict
        items = items_of(st, v)
        out = "{"
        k = 0
        first = 1
        while k < len(items):
            if first == 0:
                out = out + ", "
            first = 0
            out = out + to_disp(st, items[k], 1) + ": " + to_disp(st, items[k + 1], 1)
            k = k + 2
        return out + "}"
    if v.tag == 12:                      # instance
        classes = st.prog.classes
        cid = st.heap[v.iv].cursor
        if _is_exc_class(st, cid) == 1:
            items = st.heap[v.iv].items
            j = 0
            while j < len(items):
                if items[j].tag == 3 and _strcmp(items[j].sv, "args") == 0:
                    at = items[j + 1]
                    ai = items_of(st, at)
                    if len(ai) == 0:
                        return ""                # str(Exception()) == ""
                    if len(ai) == 1:
                        if _cls_chain_has(st, cid, "KeyError") == 1:
                            return to_disp(st, ai[0], 1)   # KeyError uses repr(key)
                        return to_disp(st, ai[0], 0)   # the message
                    return to_disp(st, at, 1)    # str(multi-arg) == repr(tuple)
                j = j + 2
            return ""
        ci = classes[cid]
        if use_repr == 0:
            mfx = lookup_method(st, cid, "__str__")
            if mfx >= 0:
                argl = new_v_list()
                argl.append(v)
                rv = run_func(st, mfx, argl)
                if rv.tag == 3:
                    return rv.sv
                return to_disp(st, rv, 0)
        return "<" + ci.cname + " object>"
    return "<callable>"


def truthy(st: "St", v: "V") -> "int":
    if v.tag == 0:
        return 0
    if v.tag == 1:
        return 1 if v.iv != 0 else 0
    if v.tag == 2:
        return 1 if v.dv != 0.0 else 0
    if v.tag == 3:
        return 1 if ord(v.sv[0]) != 0 else 0   # non-empty == first byte not NUL
    if v.tag == 4:
        return 1 if v.iv != 0 else 0
    if v.tag == 7 or v.tag == 9 or v.tag == 10:   # list / set / tuple: empty is falsy
        return 1 if len(items_of(st, v)) > 0 else 0
    if v.tag == 8:                                 # dict: empty is falsy
        return 1 if len(items_of(st, v)) > 0 else 0
    return 1                                        # func/builtin/iter/instance: truthy


# ---- equality / ordering (value semantics for scalars) ----
def _strcmp(a: "char*", b: "char*") -> "int":
    # Stop at the first differing byte (the cheap "prefix compare" German
    # strings get for free). Most compares -- dict keys, attr/method names,
    # grammar tokens -- differ in the first byte or two, so this exits early.
    #
    # Bounded by the shorter length rather than by a NUL. Reading `a[i]` past
    # the end and relying on the terminator is a C idiom, and it is correct
    # in the generated C -- but this same source runs on the reference VM
    # under CPython, where an index past the end raises rather than yielding
    # zero. So *every equal comparison crashed the oracle*: two equal strings
    # never differ, so the loop always ran off the end, and a prefix did the
    # same. Only strings differing before either ended worked, which is why
    # the failure hid -- most compares do differ early.
    #
    # The two `len()` calls are what the previous version was written to
    # avoid. They cost a `strlen` each in C, which is vectorised, against a
    # byte-at-a-time loop that still exits at the first difference; the early
    # exit is preserved and the oracle works.
    na = len(a)
    nb = len(b)
    n = na
    if nb < n:
        n = nb
    i = 0
    while i < n:
        ca = ord(a[i])
        cb = ord(b[i])
        if ca != cb:
            if ca < cb:
                return -1
            return 1
        i = i + 1
    if na == nb:                             # equal for the whole length
        return 0
    if na < nb:                              # a is a proper prefix of b
        return -1
    return 1


def v_eq_bool(st: "St", x: "V", y: "V") -> "int":
    if x.tag == 3 and y.tag == 3:
        return 1 if _strcmp(x.sv, y.sv) == 0 else 0
    if x.tag == 0 or y.tag == 0:
        return 1 if x.tag == y.tag else 0
    if x.tag == 7 or x.tag == 10:
        # Lists and tuples compare element-wise, like CPython. This used to be
        # heap identity for every container, which made two structurally equal
        # tuples unequal -- and therefore useless as dict keys, because v_hash
        # returns a constant for containers and relies entirely on this check to
        # separate them. compiler.py keys its constant table on (kind, payload)
        # tuples, so the dedup silently never fired and a self-hosted compile
        # emitted 899 constants where CPython emits 231.
        if y.tag != x.tag:
            return 0
        if x.iv == y.iv:
            return 1                                  # same object: cheap path
        xs = items_of(st, x)
        ys = items_of(st, y)
        if len(xs) != len(ys):
            return 0
        i = 0
        while i < len(xs):
            if v_eq_bool(st, xs[i], ys[i]) == 0:
                return 0
            i = i + 1
        return 1
    if x.tag == 8 and y.tag == 8:
        # Dicts compare by contents, not by insertion order, so this looks each
        # key up in the other dict rather than walking both item arrays in step.
        if x.iv == y.iv:
            return 1
        xs = items_of(st, x)
        ys = items_of(st, y)
        if len(xs) != len(ys):
            return 0
        ycont = cont_of(st, y)
        j = 0
        while j < len(xs):
            k = dict_lookup(st, ycont, xs[j])
            if k < 0:
                return 0
            if v_eq_bool(st, xs[j + 1], ycont.items[k + 1]) == 0:
                return 0
            j = j + 2
        return 1
    if x.tag == 9 and y.tag == 9:
        if x.iv == y.iv:
            return 1
        xs = items_of(st, x)
        if len(xs) != len(items_of(st, y)):
            return 0
        i = 0
        while i < len(xs):
            if set_has(st, y, xs[i]) == 0:
                return 0
            i = i + 1
        return 1
    if x.tag >= 7 or y.tag >= 7:
        return 1 if (x.tag == y.tag and x.iv == y.iv) else 0   # container identity
    return 1 if to_float(x) == to_float(y) else 0


def v_cmp(x: "V", y: "V") -> "int":
    if x.tag == 3 and y.tag == 3:
        return _strcmp(x.sv, y.sv)
    a = to_float(x)
    b = to_float(y)
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


# ---- arithmetic ----
def v_add(st: "St", x: "V", y: "V") -> "V":
    if x.tag == 3 and y.tag == 3:
        return v_str(x.sv + y.sv)
    if (x.tag == 7 and y.tag == 7) or (x.tag == 10 and y.tag == 10):
        merged = new_v_list()
        for e in items_of(st, x):
            merged.append(e)
        for e in items_of(st, y):
            merged.append(e)
        return v_container(st, x.tag, 0 if x.tag == 7 else 3, merged)
    if x.tag == 2 or y.tag == 2:
        return v_float(to_float(x) + to_float(y))
    return v_int(x.iv + y.iv)


def v_sub(st: "St", x: "V", y: "V") -> "V":
    if x.tag == 9 and y.tag == 9:           # set difference
        # v_bitand and v_bitxor already special-cased sets; `-` did not, so it
        # fell through to `x.iv - y.iv` and subtracted the two heap indices,
        # yielding a meaningless int. compiler.py computes its reclaimable-local
        # set as `assigned - escaped - not_fresh - params`, so under minipy that
        # was always empty and no local ever got a free hint: a self-hosted
        # compile emitted zero ACC_ADD_L fusions where CPython emits fifteen.
        out = new_v_list()
        sv = v_container(st, 9, 2, out)
        for e in items_of(st, x):
            if set_has(st, y, e) == 0:
                _set_add(st, sv, e)
        return sv
    if x.tag == 2 or y.tag == 2:
        return v_float(to_float(x) - to_float(y))
    return v_int(x.iv - y.iv)


def _floordiv_int(a: "long", b: "long") -> "long":
    q = a // b                      # truncated under py2c/C, floored under CPython
    r = a - q * b
    if r != 0 and ((r < 0) != (b < 0)):
        q = q - 1
    return q


def _mod_int(a: "long", b: "long") -> "long":
    r = a - (a // b) * b
    if r != 0 and ((r < 0) != (b < 0)):
        r = r + b
    return r


def _ffloor(x: "double") -> "double":
    t = float(int(x))               # truncate toward zero
    if t > x:
        t = t - 1.0
    return t


def v_mul(st: "St", x: "V", y: "V") -> "V":
    if x.tag == 3 and y.tag == 1:           # str * int
        out = ""
        k = 0
        while k < y.iv:
            out = out + x.sv
            k = k + 1
        return v_str(out)
    if x.tag == 1 and y.tag == 3:           # int * str
        return v_mul(st, y, x)
    if (x.tag == 7 or x.tag == 10) and y.tag == 1:   # list/tuple * int
        src = items_of(st, x)
        out2 = new_v_list()
        k = 0
        while k < y.iv:
            m = 0
            while m < len(src):
                out2.append(src[m])
                m = m + 1
            k = k + 1
        if x.tag == 10:
            return v_container(st, 10, 3, out2)
        return v_container(st, 7, 0, out2)
    if x.tag == 1 and (y.tag == 7 or y.tag == 10):   # int * list/tuple
        return v_mul(st, y, x)
    if x.tag == 2 or y.tag == 2:
        return v_float(to_float(x) * to_float(y))
    return v_int(x.iv * y.iv)


def v_div(x: "V", y: "V") -> "V":
    return v_float(to_float(x) / to_float(y))


def v_floordiv(x: "V", y: "V") -> "V":
    if x.tag == 2 or y.tag == 2:
        return v_float(_ffloor(to_float(x) / to_float(y)))
    return v_int(_floordiv_int(x.iv, y.iv))


def v_mod(st: "St", x: "V", y: "V") -> "V":
    if x.tag == 3:
        args = new_v_list()
        if y.tag == 10:
            for e in items_of(st, y):
                args.append(e)
        else:
            args.append(y)
        # str_format reads the arguments and returns a fresh string, so `args`
        # is dead here. It is an interpreter temporary that never became a heap
        # container, which means the collector cannot see it: the sweep only
        # frees the `items` of a reclaimed slot. Lists like this one were the
        # bulk of the arena -- 17M of 29M allocations in a self-hosted compile
        # were never reclaimed while the live set stayed flat at ~11k slots.
        r = v_str(str_format(st, x.sv, args))
        del args
        return r
    if x.tag == 2 or y.tag == 2:
        fa = to_float(x)
        fb = to_float(y)
        return v_float(fa - fb * _ffloor(fa / fb))
    return v_int(_mod_int(x.iv, y.iv))


def v_neg(x: "V") -> "V":
    if x.tag == 2:
        return v_float(-x.dv)
    return v_int(-x.iv)


def _pw_int(base: "long", e: "long") -> "long":
    r: "long" = 1
    k = 0
    while k < e:
        r = r * base
        k = k + 1
    return r


def _pw_flt(base: "double", e: "long") -> "double":
    r: "double" = 1.0
    k = 0
    while k < e:
        r = r * base
        k = k + 1
    return r


def v_pow(x: "V", y: "V") -> "V":
    if y.tag != 2:                          # integer exponent
        e = y.iv
        if e >= 0:
            if x.tag == 2:
                return v_float(_pw_flt(x.dv, e))
            return v_int(_pw_int(x.iv, e))
    return v_float(0.0)                      # float/negative exponent: v0 stub


def set_has(st: "St", setv: "V", item: "V") -> "int":
    items = items_of(st, setv)
    j = 0
    while j < len(items):
        if v_eq_bool(st, items[j], item) != 0:
            return 1
        j = j + 1
    return 0


def v_bitor(st: "St", x: "V", y: "V") -> "V":
    if x.tag == 9 and y.tag == 9:           # set union
        out = new_v_list()
        sv = v_container(st, 9, 2, out)
        for e in items_of(st, x):
            _set_add(st, sv, e)
        for e in items_of(st, y):
            _set_add(st, sv, e)
        return sv
    return v_int(x.iv | y.iv)


def v_bitand(st: "St", x: "V", y: "V") -> "V":
    if x.tag == 9 and y.tag == 9:           # set intersection
        out = new_v_list()
        sv = v_container(st, 9, 2, out)
        for e in items_of(st, x):
            if set_has(st, y, e) != 0:
                _set_add(st, sv, e)
        return sv
    return v_int(x.iv & y.iv)


def v_bitxor(st: "St", x: "V", y: "V") -> "V":
    if x.tag == 9 and y.tag == 9:           # set symmetric difference
        out = new_v_list()
        sv = v_container(st, 9, 2, out)
        for e in items_of(st, x):
            if set_has(st, y, e) == 0:
                _set_add(st, sv, e)
        for e in items_of(st, y):
            if set_has(st, x, e) == 0:
                _set_add(st, sv, e)
        return sv
    return v_int(x.iv ^ y.iv)


def v_slice(st: "St", seq: "V", lo_v: "V", hi_v: "V", step_v: "V") -> "V":
    if seq.tag == 3:
        n = len(seq.sv)
    elif seq.tag == 7 or seq.tag == 10:
        n = len(items_of(st, seq))
    else:
        return v_none()
    step = step_v.iv
    if step == 0:
        step = 1                            # Python errors; v0 just guards
    # CPython slice.indices(): clamp bounds, with defaults keyed off step sign.
    # An omitted bound arrives as None (tag 0).
    if step < 0:
        lower = -1
        upper = n - 1
    else:
        lower = 0
        upper = n
    if lo_v.tag == 0:
        lo = upper if step < 0 else lower
    else:
        lo = lo_v.iv
        if lo < 0:
            lo = lo + n
            if lo < lower:
                lo = lower
        elif lo > upper:
            lo = upper
    if hi_v.tag == 0:
        hi = lower if step < 0 else upper
    else:
        hi = hi_v.iv
        if hi < 0:
            hi = hi + n
            if hi < lower:
                hi = lower
        elif hi > upper:
            hi = upper
    if seq.tag == 3:
        out = ""
        k = lo
        if step > 0:
            while k < hi:
                out = out + seq.sv[k]
                k = k + step
        else:
            while k > hi:
                out = out + seq.sv[k]
                k = k + step
        return v_str(out)
    src = items_of(st, seq)
    res = new_v_list()
    k = lo
    if step > 0:
        while k < hi:
            res.append(src[k])
            k = k + step
    else:
        while k > hi:
            res.append(src[k])
            k = k + step
    if seq.tag == 10:
        return v_container(st, 10, 3, res)
    return v_container(st, 7, 0, res)


def v_slice_store(st: "St", seq: "V", lo_v: "V", hi_v: "V", value: "V") -> None:
    # `seq[lo:hi] = value` for lists (step 1). Splices value's items in place so
    # aliases of the list observe the change. Bounds clamp like CPython.
    if seq.tag != 7:
        return
    items = items_of(st, seq)
    n = len(items)
    if lo_v.tag == 0:
        lo = 0
    else:
        lo = lo_v.iv
        if lo < 0:
            lo = lo + n
        if lo < 0:
            lo = 0
        if lo > n:
            lo = n
    if hi_v.tag == 0:
        hi = n
    else:
        hi = hi_v.iv
        if hi < 0:
            hi = hi + n
        if hi < 0:
            hi = 0
        if hi > n:
            hi = n
    if hi < lo:
        hi = lo
    if value.tag == 7 or value.tag == 10:
        vitems = items_of(st, value)
    else:
        vitems = new_v_list()
    newlist = new_v_list()
    k = 0
    while k < lo:
        newlist.append(items[k])
        k = k + 1
    k = 0
    while k < len(vitems):
        newlist.append(vitems[k])
        k = k + 1
    k = hi
    while k < n:
        newlist.append(items[k])
        k = k + 1
    while len(items) > 0:
        items.pop()
    k = 0
    while k < len(newlist):
        items.append(newlist[k])
        k = k + 1


# ---- subscript / membership / iteration ----
def _norm_index(i: "long", n: "long") -> "long":
    if i < 0:
        return i + n
    return i


def v_hash(key: "V") -> "long":
    t = key.tag
    if t == 1 or t == 4:               # int / bool
        return key.iv
    if t == 0:                         # none
        return 0
    if t == 3:                         # str (djb2)
        s = key.sv
        h: "long" = 5381
        i = 0
        n = len(s)
        while i < n:
            # Mask to 57 bits each step so h*33 stays within signed long (no UB
            # signed overflow -- which gcc -O2 miscompiles). Keeps h non-negative
            # too, so the bucket index (h % cap) is well-defined.
            h = (h * 33 + ord(s[i])) & 144115188075855871
            i = i + 1
        return h
    if t == 2:                         # float (consistent with int when integral)
        return int(key.dv)
    return 0                           # other: constant; correctness via eq check


def dict_reindex(cont: "Cont") -> "int":
    items = cont.items
    n = len(items)
    cnt = n // 2
    cap = 8
    while cap * 2 < cnt * 3:           # keep load factor under 2/3
        cap = cap * 2
    buckets = new_v_list()
    empty = v_int(-1)                  # shared empty sentinel (immutable)
    i = 0
    while i < cap:
        buckets.append(empty)
        i = i + 1
    j = 0
    while j < n:
        h = v_hash(items[j])
        slot = h & (cap - 1)
        while buckets[slot].iv != -1:
            slot = (slot + 1) & (cap - 1)
        buckets[slot] = v_int(j)
        j = j + 2
    cont.buckets = buckets
    return 0


def dict_lookup(st: "St", cont: "Cont", key: "V") -> "long":
    buckets = cont.buckets
    cap = len(buckets)
    if cap == 0:
        if len(cont.items) == 0:
            return -1
        dict_reindex(cont)             # lazily build index over existing items
        buckets = cont.buckets
        cap = len(buckets)
    h = v_hash(key)
    slot = h & (cap - 1)
    probes = 0
    while probes < cap:
        ki = buckets[slot].iv
        if ki == -1:
            return -1
        if v_eq_bool(st, cont.items[ki], key) != 0:
            return ki
        slot = (slot + 1) & (cap - 1)
        probes = probes + 1
    return -1


def dict_insert(st: "St", cont: "Cont", key: "V", val: "V") -> "int":
    ki = dict_lookup(st, cont, key)
    if ki >= 0:
        cont.items[ki + 1] = val
        return 0
    cont.items.append(key)             # new key: append to ordered backing store
    cont.items.append(val)
    j = len(cont.items) - 2
    cnt = len(cont.items) // 2
    cap = len(cont.buckets)
    if cap == 0 or cap * 2 < cnt * 3:
        dict_reindex(cont)             # grow (or first build): includes new key
        return 0
    h = v_hash(key)
    slot = h & (cap - 1)
    while cont.buckets[slot].iv != -1:
        slot = (slot + 1) & (cap - 1)
    cont.buckets[slot] = v_int(j)
    return 0


def dict_find(st: "St", items: "list[V]", key: "V") -> "int":
    j = 0
    n = len(items)
    while j < n:
        if v_eq_bool(st, items[j], key) != 0:
            return j
        j = j + 2
    return -1


def v_index(st: "St", seq: "V", idx: "V") -> "V":
    if seq.tag == 3:
        i = _norm_index(idx.iv, len(seq.sv))
        return v_str(seq.sv[i])
    if seq.tag == 7 or seq.tag == 10:
        items = items_of(st, seq)
        i = _norm_index(idx.iv, len(items))
        return items[i]
    if seq.tag == 8:
        cont = st.heap[seq.iv]
        j = dict_lookup(st, cont, idx)
        if j >= 0:
            return cont.items[j + 1]
        return v_none()
    return v_none()


def v_setindex(st: "St", seq: "V", idx: "V", val: "V") -> "int":
    if seq.tag == 7:
        items = items_of(st, seq)
        i = _norm_index(idx.iv, len(items))
        items[i] = val
        return 0
    if seq.tag == 8:
        cont = st.heap[seq.iv]
        dict_insert(st, cont, idx, val)
        return 0
    return 0


def v_contains(st: "St", container: "V", item: "V") -> "V":
    if container.tag == 3:
        return v_bool(1 if item.sv in container.sv else 0)
    if container.tag == 7 or container.tag == 10 or container.tag == 9:
        items = items_of(st, container)
        j = 0
        while j < len(items):
            if v_eq_bool(st, items[j], item) != 0:
                return v_bool(1)
            j = j + 1
        return v_bool(0)
    if container.tag == 8:
        cont = st.heap[container.iv]
        return v_bool(1 if dict_lookup(st, cont, item) >= 0 else 0)
    return v_bool(0)


def materialize(st: "St", v: "V") -> "list[V]":
    out = new_v_list()
    if v.tag == 7 or v.tag == 10 or v.tag == 9:
        for e in items_of(st, v):
            out.append(e)
    elif v.tag == 8:
        items = items_of(st, v)
        k = 0
        while k < len(items):
            out.append(items[k])
            k = k + 2
    elif v.tag == 3:
        k = 0
        while k < len(v.sv):
            out.append(v_str(v.sv[k]))
            k = k + 1
    return out


def v_iter(st: "St", v: "V") -> "V":
    return v_container(st, 11, 4, materialize(st, v))


def _set_add(st: "St", setv: "V", item: "V") -> "int":
    items = items_of(st, setv)
    j = 0
    while j < len(items):
        if v_eq_bool(st, items[j], item) != 0:
            return 0
        j = j + 1
    items.append(item)
    return 0


def v_len(st: "St", v: "V") -> "long":
    if v.tag == 3:
        return len(v.sv)
    if v.tag == 8:
        return len(items_of(st, v)) // 2
    if v.tag == 7 or v.tag == 10 or v.tag == 9:
        return len(items_of(st, v))
    return 0


# ---- %-formatting ----
def _is_digit(ch: "char*") -> "int":
    o = ord(ch)
    return 1 if (o >= 48 and o <= 57) else 0


def _ffmt(x: "double", prec: "int") -> "char*":
    p = prec
    if p < 0:
        p = 6
    neg = 0
    if x < 0.0:
        neg = 1
        x = -x
    scale = 1
    k = 0
    while k < p:
        scale = scale * 10
        k = k + 1
    scaled = int(x * float(scale) + 0.5)
    ip = scaled // scale
    fp = scaled % scale
    out = str(ip)
    if p > 0:
        fs = str(fp)
        while len(fs) < p:
            fs = "0" + fs
        out = out + "." + fs
    if neg != 0:
        out = "-" + out
    return out


def _hexfmt(n: "long") -> "char*":
    if n == 0:
        return "0"
    digits = "0123456789abcdef"
    neg = 0
    m = n
    if m < 0:
        neg = 1
        m = -m
    out = ""
    while m > 0:
        out = digits[m % 16] + out
        m = m // 16
    if neg != 0:
        out = "-" + out
    return out


def _pad(piece: "char*", width: "int", left: "int", zero: "int") -> "char*":
    if len(piece) >= width:
        return piece
    padc = " "
    if zero != 0 and left == 0:
        padc = "0"
    pad = ""
    k = len(piece)
    while k < width:
        pad = pad + padc
        k = k + 1
    if left != 0:
        return piece + pad
    return pad + piece


def str_format(st: "St", fmt: "char*", args: "list[V]") -> "char*":
    # Collect the pieces and join once. Appending to `out` per character made
    # this the interpreter's single largest string allocator: 13.7M pyconcat
    # calls on a self-hosted compile, ~113 MB, because every character of every
    # literal run re-copied the whole result so far. Literal runs are now taken
    # as one slice each, and `"".join` lowers to the runtime's pyjoin, which
    # sums the lengths and fills a single allocation.
    parts = []
    i = 0
    ai = 0
    n = len(fmt)
    while i < n:
        ch = fmt[i]
        if ch != "%":
            j = i
            while j < n and fmt[j] != "%":
                j = j + 1
            parts.append(fmt[i:j])
            i = j
            continue
        i = i + 1
        if i < n and fmt[i] == "%":
            parts.append("%")
            i = i + 1
            continue
        left = 0
        zero = 0
        while i < n and (fmt[i] == "-" or fmt[i] == "0" or fmt[i] == " " or fmt[i] == "+"):
            if fmt[i] == "-":
                left = 1
            if fmt[i] == "0":
                zero = 1
            i = i + 1
        width = 0
        while i < n and _is_digit(fmt[i]) != 0:
            width = width * 10 + (ord(fmt[i]) - 48)
            i = i + 1
        prec = -1
        if i < n and fmt[i] == ".":
            i = i + 1
            prec = 0
            while i < n and _is_digit(fmt[i]) != 0:
                prec = prec * 10 + (ord(fmt[i]) - 48)
                i = i + 1
        conv = "s"
        if i < n:
            conv = fmt[i]
            i = i + 1
        arg = v_none()
        if ai < len(args):
            arg = args[ai]
        ai = ai + 1
        piece = ""
        if conv == "d" or conv == "i":
            piece = str(to_int(arg))
        elif conv == "s":
            piece = to_disp(st, arg, 0)
            if prec >= 0 and len(piece) > prec:
                piece = piece[0:prec]
        elif conv == "r":
            piece = to_disp(st, arg, 1)
        elif conv == "f":
            piece = _ffmt(to_float(arg), prec)
        elif conv == "x":
            piece = _hexfmt(to_int(arg))
        else:
            piece = to_disp(st, arg, 0)
        parts.append(_pad(piece, width, left, zero))
    return "".join(parts)


# ---- classes / instances ----
# Instance: Cont kind 5, cursor = class id, items = [attrname, attrval, ...].
# Bound user method: Cont kind 6, cursor = func idx, items = [self].
# Bound builtin method: Cont kind 7, cursor = method id, items = [self].
# V tags: OBJ 12, CLASS 13, BOUND 14, BOUNDB 15.
def lookup_method(st: "St", cid: "int", name: "char*") -> "int":
    classes = st.prog.classes
    c = cid
    while c >= 0:
        ci = classes[c]
        meths = ci.methods
        j = 0
        while j < len(meths):
            me = meths[j]
            if _strcmp(me.mname, name) == 0:
                return me.mfunc
            j = j + 1
        c = ci.base
    return -1


def mcache_lookup(st: "St", cls: "int", nameid: "int", nm: "char*") -> "int":
    # Monomorphic inline cache keyed by method-name const id: if the receiver's
    # class still matches the slot, return the cached func idx; otherwise resolve
    # via lookup_method (base-walk + strcmp scan) and refill the slot.
    ccl = _lget(st.mcache_cls, nameid)
    if ccl.iv == cls:
        cfx = _lget(st.mcache_fidx, nameid)
        return cfx.iv
    fidx = lookup_method(st, cls, nm)
    _lset(st.mcache_cls, nameid, v_int(cls))
    _lset(st.mcache_fidx, nameid, v_int(fidx))
    return fidx


def inst_get(st: "St", obj: "V", name: "char*") -> "V":
    items = items_of(st, obj)
    j = 0
    while j < len(items):
        if items[j].tag == 3 and _strcmp(items[j].sv, name) == 0:
            return items[j + 1]
        j = j + 2
    return v_none()


def inst_set(st: "St", obj: "V", name: "char*", val: "V") -> "int":
    items = items_of(st, obj)
    j = 0
    while j < len(items):
        if items[j].tag == 3 and _strcmp(items[j].sv, name) == 0:
            items[j + 1] = val
            return 0
        j = j + 2
    items.append(v_str(name))
    items.append(val)
    return 0


def instantiate(st: "St", classid: "int", args: "list[V]") -> "V":
    inst = v_container(st, 12, 5, new_v_list())
    st.heap[inst.iv].cursor = classid
    inst.jitcode = classid                  # inline-cache key: object's class id
    fidx = lookup_method(st, classid, "__init__")
    if fidx >= 0:
        callargs = new_v_list()
        callargs.append(inst)
        for a in args:
            callargs.append(a)
        run_func(st, fidx, callargs)
    elif _is_exc_class(st, classid) == 1:
        # BaseException with no user __init__: record .args so str(e) and e.args
        # behave like CPython (str == message for one arg, "" for none).
        argt = new_v_list()
        for a in args:
            argt.append(a)
        inst_set(st, inst, "args", v_container(st, 10, 3, argt))
    return inst


def raise_attr_error(st: "St") -> "int":
    # Attribute/method access on a value that has none -- e.g. a method call on
    # an unbound name left by an unlinked `import X as m` (m is None). Set a
    # catchable exception (matches `except Exception`), mirroring CPython's
    # AttributeError, so guarded try/except blocks skip cleanly instead of the
    # access silently yielding None. Mirrors the reference VM _raise_attr_error.
    classes = st.prog.classes
    cid = -1
    fallback = -1
    i = 0
    while i < len(classes):
        cn = classes[i].cname
        di = len(cn) - 1
        cut = -1
        while di >= 0:
            if cn[di] == "$":                  # strip link prefix ($mod$Name)
                cut = di
                di = -1
            else:
                di = di - 1
        short = cn
        if cut >= 0:
            short = cn[cut + 1:len(cn)]
        if _strcmp(short, "AttributeError") == 0:
            cid = i
        elif _strcmp(short, "Exception") == 0:
            fallback = i
        i = i + 1
    use = cid
    if use < 0:
        use = fallback
    if use >= 0:
        st.exc_val = instantiate(st, use, new_v_list())
    st.exc_flag = 1
    return 0



def raise_named_error(st: "St", want: "char*") -> "int":
    """Set a catchable exception of class `want`, or `Exception` if absent.

    The same shape as `raise_attr_error`, parameterised by class name so
    `NameError` and its siblings do not each need a copy. A program whose
    bytecode never mentions the class still gets a catchable `Exception`
    rather than silently continuing.
    """
    classes = st.prog.classes
    cid = -1
    fallback = -1
    i = 0
    while i < len(classes):
        cn = classes[i].cname
        di = len(cn) - 1
        cut = -1
        while di >= 0:
            if cn[di] == "$":                  # strip link prefix ($mod$Name)
                cut = di
                di = -1
            else:
                di = di - 1
        short = cn
        if cut >= 0:
            short = cn[cut + 1:len(cn)]
        if _strcmp(short, want) == 0:
            cid = i
        elif _strcmp(short, "Exception") == 0:
            fallback = i
        i = i + 1
    use = cid
    if use < 0:
        use = fallback
    if use >= 0:
        st.exc_val = instantiate(st, use, new_v_list())
    st.exc_flag = 1
    return 0

def method_id(name: "char*") -> "long":
    if name == "append":
        return 100
    if name == "pop":
        return 101
    if name == "get":
        return 102
    if name == "keys":
        return 103
    if name == "values":
        return 104
    if name == "items":
        return 105
    if name == "add":
        return 106
    if name == "split":
        return 107
    if name == "join":
        return 108
    if name == "strip":
        return 109
    if name == "startswith":
        return 110
    if name == "endswith":
        return 111
    if name == "find":
        return 112
    if name == "replace":
        return 113
    if name == "upper":
        return 114
    if name == "lower":
        return 115
    if name == "extend":
        return 116
    if name == "insert":
        return 117
    if name == "index":
        return 118
    if name == "count":
        return 119
    if name == "update":
        return 120
    if name == "setdefault":
        return 121
    if name == "splitlines":
        return 122
    if name == "rstrip":
        return 123
    if name == "lstrip":
        return 124
    if name == "isdigit":
        return 125
    if name == "isupper":
        return 126
    if name == "islower":
        return 127
    if name == "isalpha":
        return 128
    if name == "isalnum":
        return 129
    if name == "discard":
        return 130
    if name == "remove":
        return 131
    return -1


def is_instance(st: "St", exc: "V", clsv: "V") -> "int":
    if exc.tag != 12 or clsv.tag != 13:
        return 0
    target = clsv.iv
    classes = st.prog.classes
    c = st.heap[exc.iv].cursor
    while c >= 0:
        if c == target:
            return 1
        ci = classes[c]
        c = ci.base
    return 0


def inst_has(st: "St", obj: "V", name: "char*") -> "int":
    items = items_of(st, obj)
    j = 0
    while j < len(items):
        if items[j].tag == 3 and _strcmp(items[j].sv, name) == 0:
            return 1
        j = j + 2
    return 0


def _isinst_type(obj: "V", bid: "long") -> "int":
    if bid == 3:                            # int (bool counts as int)
        return 1 if (obj.tag == 1 or obj.tag == 4) else 0
    if bid == 5:
        return 1 if obj.tag == 2 else 0     # float
    if bid == 4:
        return 1 if obj.tag == 3 else 0     # str
    if bid == 7:
        return 1 if obj.tag == 4 else 0     # bool
    if bid == 8:
        return 1 if obj.tag == 7 else 0     # list
    if bid == 9:
        return 1 if obj.tag == 8 else 0     # dict
    if bid == 10:
        return 1 if obj.tag == 9 else 0     # set
    if bid == 11:
        return 1 if obj.tag == 10 else 0    # tuple
    return 0


def native_isinstance(st: "St", obj: "V", spec: "V") -> "int":
    if spec.tag == 13:                      # user class
        return is_instance(st, obj, spec)
    if spec.tag == 6:                       # type builtin (int/str/list/...)
        return _isinst_type(obj, spec.iv)
    if spec.tag == 10:                      # tuple of types
        for s in items_of(st, spec):
            if native_isinstance(st, obj, s) != 0:
                return 1
        return 0
    return 0


def _is_ws(ch: "char*") -> "int":
    o = ord(ch)
    return 1 if (o == 32 or o == 9 or o == 10 or o == 13) else 0


def _rstrip(s: "char*") -> "char*":
    e = len(s)
    while e > 0 and _is_ws(s[e - 1]) != 0:
        e = e - 1
    return s[0:e]


def _lstrip(s: "char*") -> "char*":
    i = 0
    n = len(s)
    while i < n and _is_ws(s[i]) != 0:
        i = i + 1
    return s[i:n]


def _find_sub(s: "char*", sub: "char*", start: "long") -> "long":
    n = len(s)
    m = len(sub)
    if m == 0:
        return start
    i = start
    while i + m <= n:
        j = 0
        while j < m and ord(s[i + j]) == ord(sub[j]):
            j = j + 1
        if j == m:
            return i
        i = i + 1
    return -1


def _replace_all(s: "char*", old: "char*", rep: "char*") -> "char*":
    m = len(old)
    if m == 0:
        return s
    out = ""
    i = 0
    n = len(s)
    while i < n:
        hit = 0
        if i + m <= n:
            j = 0
            while j < m and ord(s[i + j]) == ord(old[j]):
                j = j + 1
            if j == m:
                hit = 1
        if hit != 0:
            out = out + rep
            i = i + m
        else:
            out = out + s[i]
            i = i + 1
    return out


# ---- const -> value ----
def const_to_v_raw(prog: "Program", idx: "int") -> "V":
    k = prog.consts[idx]
    if k.t == "int":
        return v_int(k.i)
    if k.t == "float":
        return v_float(k.d)
    if k.t == "str":
        return v_str(k.s)
    if k.t == "bool":
        return v_bool(1 if k.i != 0 else 0)
    if k.t == "func":
        return v_func(k.i)
    if k.t == "builtin":
        return v_builtin(k.i)
    if k.t == "class":
        return V(13, k.i)
    return v_none()


def const_to_v(prog: "Program", idx: "int") -> "V":
    # Constants are immutable, so each one is materialized into a V exactly once
    # (at startup) and that shared V is returned on every load. Without this a
    # large literal in a hot loop (e.g. the `1000000` bound in a while-condition)
    # allocates a fresh V on every iteration.
    if _const_vs_ready != 0:
        return _const_vs[idx]
    return const_to_v_raw(prog, idx)


# ---- builtins (ids 0-99) and methods (ids 100+) ----
# ---- file objects (tag 16 / heap kind 8) ----
# minipy has no persistent FILE* value, and py2c loses the C FILE* type when a
# file is boxed into a generic container. So a file is modelled as a buffered
# object: reads slurp the whole file at open, writes accumulate string chunks in
# the heap cell and are flushed once on close. The actual C file I/O happens only
# inside the two transient helpers below, which py2c compiles to fopen/fread/
# fwrite/fclose. cursor: 0 = read, 1 = open-for-write, 2 = write flushed/closed.

def _read_file_native(path: "char*") -> "char*":
    return open(path).read()


def _write_file_native(path: "char*", content: "char*") -> "int":
    f = open(path, "w")
    f.write(content)
    f.close()
    return 0


def file_open(st: "St", args: "list[V]") -> "V":
    path = args[0].sv
    mode = "r"
    if len(args) > 1:
        mode = args[1].sv
    is_write = 0
    i = 0
    while i < len(mode):
        ch = mode[i]
        if ch == "w" or ch == "a":
            is_write = 1
        i = i + 1
    items = new_v_list()
    items.append(v_str(path))
    if is_write == 0:
        items.append(v_str(_read_file_native(path)))
    r = v_container(st, 16, 8, items)
    st.heap[r.iv].cursor = is_write
    return r


def file_method(st: "St", nm: "char*", args: "list[V]") -> "V":
    self_v = args[0]
    c = st.heap[self_v.iv]
    if nm == "read":
        if len(c.items) > 1:
            return c.items[1]
        return v_str("")
    if nm == "write":
        if len(args) > 1:
            c.items.append(args[1])
        return v_none()
    if nm == "close":
        if c.cursor == 1:
            # Same quadratic shape as str.join had: a file written with many
            # small write() calls re-copied the whole buffer once per chunk.
            parts = []
            k = 1
            while k < len(c.items):
                parts.append(c.items[k].sv)
                k = k + 1
            _write_file_native(c.items[0].sv, "".join(parts))
            c.cursor = 2
        return v_none()
    if nm == "__enter__":
        return self_v
    if nm == "__exit__":
        return file_method(st, "close", args)
    return v_none()


def _re_ngroups(pat: "char*") -> "long":
    """Capturing groups in `pat`, counted from the pattern text.

    Both sides of the seam agree on this number without asking the engine:
    under CPython `m` is a stdlib match object and under minipy it is the flat
    capture list crust_re returned, and neither exposes a group count the
    subset can read. Counting '(' here is the one answer both agree on.
    """
    n = 0
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "\\":
            i = i + 2
            continue
        if c == "[":                      # a '(' inside a class is literal
            i = i + 1
            while i < len(pat) and pat[i] != "]":
                if pat[i] == "\\":
                    i = i + 1
                i = i + 1
            i = i + 1
            continue
        if c == "(":
            if i + 1 < len(pat) and pat[i + 1] == "?":
                # (?P<name>...) captures; (?:...) and lookaround do not.
                if i + 3 < len(pat) and pat[i + 2] == "P" and pat[i + 3] == "<":
                    n = n + 1
            else:
                n = n + 1
        i = i + 1
    return n


def do_builtin(st: "St", bid: "long", args: "list[V]") -> "V":
    if bid >= 100:
        return do_method(st, bid, args)
    if bid == 0:               # print
        out = ""
        k = 0
        while k < len(args):
            if k > 0:
                out = out + " "
            out = out + to_disp(st, args[k], 0)
            k = k + 1
        print(out)
        return v_none()
    if bid == 1:               # len
        if len(args) > 0:
            return v_int(v_len(st, args[0]))
        return v_int(0)
    if bid == 2:               # range
        lo = 0
        hi = 0
        step = 1
        if len(args) == 1:
            hi = args[0].iv
        elif len(args) == 2:
            lo = args[0].iv
            hi = args[1].iv
        elif len(args) >= 3:
            lo = args[0].iv
            hi = args[1].iv
            step = args[2].iv
        out = new_v_list()
        i = lo
        if step > 0:
            while i < hi:
                out.append(v_int(i))
                i = i + step
        else:
            while i > hi:
                out.append(v_int(i))
                i = i + step
        return v_container(st, 7, 0, out)
    if bid == 3:               # int
        if len(args) > 0:
            if args[0].tag == 3:
                return v_int(_str_to_int(args[0].sv))
            return v_int(to_int(args[0]))
        return v_int(0)
    if bid == 4:               # str
        if len(args) > 0:
            return v_str(to_disp(st, args[0], 0))
        return v_str("")
    if bid == 5:               # float
        if len(args) > 0:
            if args[0].tag == 3:
                return v_float(_str_to_float(args[0].sv))
            return v_float(to_float(args[0]))
        return v_float(0.0)
    if bid == 6:               # abs
        if len(args) > 0:
            x = args[0]
            if x.tag == 2:
                return v_float(x.dv if x.dv >= 0.0 else -x.dv)
            return v_int(x.iv if x.iv >= 0 else -x.iv)
        return v_int(0)
    if bid == 7:               # bool
        if len(args) > 0:
            return v_bool(truthy(st, args[0]))
        return v_bool(0)
    if bid == 8:               # list
        if len(args) > 0:
            return v_container(st, 7, 0, materialize(st, args[0]))
        return v_container(st, 7, 0, new_v_list())
    if bid == 9:               # dict([pairs]) or dict()
        ddict = v_container(st, 8, 1, new_v_list())
        if len(args) > 0:
            for dpair in materialize(st, args[0]):
                dkey = v_index(st, dpair, v_int(0))
                dval = v_index(st, dpair, v_int(1))
                v_setindex(st, ddict, dkey, dval)
        return ddict
    if bid == 10:              # set
        out = new_v_list()
        sv = v_container(st, 9, 2, out)
        if len(args) > 0:
            for e in materialize(st, args[0]):
                _set_add(st, sv, e)
        return sv
    # Builtin ids are a SINGLE space shared with the compiler. 0..31 belong to
    # compiler.BUILTINS (see its list; __re_search=30, __re_match=31 are the
    # last two) and are emitted directly by generated code. Interpreter-only
    # builtins -- the ones bound by name at boot rather than called by id --
    # must therefore start at 32. `open` and `_native_exists` were handed 30 and
    # 31, so the branch below shadowed them and every file object came back
    # unopened; they now live at 47/48, above the _native_call* block.
    if bid == 30 or bid == 31:  # __re_search / __re_match
        # The regex seam. Both arguments are guest strings, so the pattern is
        # runtime-valued -- which is precisely what py2c lowers to crust_re's
        # dynamic bridge. Returns the flat capture list [whole, g1, ...] that
        # rpy_lib/crustre.py wraps, or None. Building the list here rather
        # than a match object keeps the value crossing the seam to strings.
        if len(args) < 2:
            return v_none()
        pat = args[0].sv
        txt = args[1].sv
        if bid == 31:
            m = re.match(pat, txt)
        else:
            m = re.search(pat, txt)
        if m is None:
            return v_none()
        out = new_v_list()
        ng = _re_ngroups(pat)
        gi = 0
        while gi <= ng:
            g = m.group(gi)
            if g is None:
                out.append(v_none())
            else:
                out.append(v_str(g))
            gi = gi + 1
        # Spans after the strings, matching the layout py2c builds for a match
        # (see _re_emit_build): [g0..gn, s0,e0, ..., sn,en]. Keeping the two
        # sides on one layout is what lets crustre.py read either.
        gi = 0
        while gi <= ng:
            out.append(v_int(m.start(gi)))
            out.append(v_int(m.end(gi)))
            gi = gi + 1
        return v_container(st, 7, 0, out)
    if bid == 29:              # frozenset (minipy: same as set, no immutability)
        out = new_v_list()
        sv = v_container(st, 9, 2, out)
        if len(args) > 0:
            for e in materialize(st, args[0]):
                _set_add(st, sv, e)
        return sv
    if bid == 11:              # tuple
        if len(args) > 0:
            return v_container(st, 10, 3, materialize(st, args[0]))
        return v_container(st, 10, 3, new_v_list())
    if bid == 12:              # repr
        if len(args) > 0:
            return v_str(to_disp(st, args[0], 1))
        return v_str("")
    if bid == 13:              # sorted
        if len(args) > 0:
            els = materialize(st, args[0])
            _sort(els)
            return v_container(st, 7, 0, els)
        return v_container(st, 7, 0, new_v_list())
    if bid == 14:              # sum
        acc = v_int(0)
        if len(args) > 0:
            for e in materialize(st, args[0]):
                acc = v_add(st, acc, e)
        return acc
    if bid == 15:              # min
        return _minmax(st, args, -1)
    if bid == 16:              # max
        return _minmax(st, args, 1)
    if bid == 17:              # isinstance
        if len(args) >= 2:
            return v_bool(native_isinstance(st, args[0], args[1]))
        return v_bool(0)
    if bid == 18:              # enumerate
        out = new_v_list()
        if len(args) > 0:
            els = materialize(st, args[0])
            k = 0
            while k < len(els):
                pair = new_v_list()
                pair.append(v_int(k))
                pair.append(els[k])
                out.append(v_container(st, 10, 3, pair))
                k = k + 1
        return v_container(st, 7, 0, out)
    if bid == 19:              # zip
        out = new_v_list()
        if len(args) == 2:
            a0 = materialize(st, args[0])
            a1 = materialize(st, args[1])
            m = len(a0)
            if len(a1) < m:
                m = len(a1)
            k = 0
            while k < m:
                pair = new_v_list()
                pair.append(a0[k])
                pair.append(a1[k])
                out.append(v_container(st, 10, 3, pair))
                k = k + 1
        elif len(args) == 3:
            a0 = materialize(st, args[0])
            a1 = materialize(st, args[1])
            a2 = materialize(st, args[2])
            m = len(a0)
            if len(a1) < m:
                m = len(a1)
            if len(a2) < m:
                m = len(a2)
            k = 0
            while k < m:
                pair = new_v_list()
                pair.append(a0[k])
                pair.append(a1[k])
                pair.append(a2[k])
                out.append(v_container(st, 10, 3, pair))
                k = k + 1
        return v_container(st, 7, 0, out)
    if bid == 20:              # any
        if len(args) > 0:
            for e in materialize(st, args[0]):
                if truthy(st, e) != 0:
                    return v_bool(1)
        return v_bool(0)
    if bid == 21:              # all
        if len(args) > 0:
            for e in materialize(st, args[0]):
                if truthy(st, e) == 0:
                    return v_bool(0)
        return v_bool(1)
    if bid == 22:              # ord
        if len(args) > 0:
            return v_int(ord(args[0].sv))
        return v_int(0)
    if bid == 23:              # chr
        if len(args) > 0:
            return v_str(chr(args[0].iv))
        return v_str("")
    if bid == 24:              # reversed
        out = new_v_list()
        if len(args) > 0:
            els = materialize(st, args[0])
            k = len(els) - 1
            while k >= 0:
                out.append(els[k])
                k = k - 1
        return v_container(st, 7, 0, out)
    if bid == 25:              # getattr
        if len(args) >= 2:
            obj = args[0]
            nm = args[1].sv
            if obj.tag == 12:
                if inst_has(st, obj, nm) != 0:
                    return inst_get(st, obj, nm)
                fidx = lookup_method(st, st.heap[obj.iv].cursor, nm)
                if fidx >= 0:
                    return V(14, obj.iv * _METH_SHIFT + fidx)   # packed bound
            if len(args) >= 3:
                return args[2]
        return v_none()
    if bid == 26:              # hasattr
        if len(args) >= 2:
            obj = args[0]
            nm = args[1].sv
            if obj.tag == 12:
                if inst_has(st, obj, nm) != 0:
                    return v_bool(1)
                if lookup_method(st, st.heap[obj.iv].cursor, nm) >= 0:
                    return v_bool(1)
        return v_bool(0)
    if bid == 27:              # type
        if len(args) > 0:
            v = args[0]
            t = v.tag
            if t == 12:
                return V(13, st.heap[v.iv].cursor)
            if t == 1:
                return v_builtin(3)
            if t == 2:
                return v_builtin(5)
            if t == 3:
                return v_builtin(4)
            if t == 4:
                return v_builtin(7)
            if t == 7:
                return v_builtin(8)
            if t == 8:
                return v_builtin(9)
            if t == 9:
                return v_builtin(10)
            if t == 10:
                return v_builtin(11)
        return v_builtin(-1)
    if bid == 28:              # setattr
        if len(args) >= 3:
            obj = args[0]
            if obj.tag == 12:
                inst_set(st, obj, args[1].sv, args[2])
        return v_none()
    if bid == 47:              # open -> buffered file object (tag 16)
        return file_open(st, args)
    if bid == 48:             # native os.path.exists (compiles to access())
        if len(args) > 0 and args[0].tag == 3 and os.path.exists(args[0].sv):
            return v_bool(1)
        return v_bool(0)
    if bid == 32:             # native os.makedirs (compiles to mkdir loop)
        if len(args) > 0 and args[0].tag == 3:
            os.makedirs(args[0].sv)
        return v_none()
    if bid == 33:             # _native_dlopen(path) -> handle (0 on failure)
        if len(args) > 0 and args[0].tag == 3:
            return v_int(_ffi.mb_dlopen(args[0].sv))
        return v_int(0)
    if bid == 34:             # _native_dlsym(handle, name) -> fn pointer
        if len(args) > 1 and args[0].tag == 1 and args[1].tag == 3:
            return v_int(_ffi.mb_dlsym(args[0].iv, args[1].sv))
        return v_int(0)
    if bid == 35:             # _native_call0i(fn) -> int
        if len(args) > 0 and args[0].tag == 1:
            return v_int(_ffi.mb_call0i(args[0].iv))
        return v_int(0)
    if bid == 36:             # _native_call1i(fn, a) -> int
        if len(args) > 1 and args[0].tag == 1 and args[1].tag == 1:
            return v_int(_ffi.mb_call1i(args[0].iv, args[1].iv))
        return v_int(0)
    if bid == 37:             # _native_call2i(fn, a, b) -> int
        if len(args) > 2 and args[0].tag == 1 and args[1].tag == 1 \
                and args[2].tag == 1:
            return v_int(_ffi.mb_call2i(args[0].iv, args[1].iv, args[2].iv))
        return v_int(0)
    if bid == 38:             # _native_call3i(fn, a, b, c) -> int
        if len(args) > 3 and args[0].tag == 1 and args[1].tag == 1 \
                and args[2].tag == 1 and args[3].tag == 1:
            return v_int(_ffi.mb_call3i(args[0].iv, args[1].iv, args[2].iv,
                                        args[3].iv))
        return v_int(0)
    # Pointer-aware FFI: value args passed 64-bit. *l return int, *p return a
    # pointer (long). Used when a page holds/passes a native object.
    if bid == 39:             # _native_call0l(fn) -> int
        if len(args) > 0 and args[0].tag == 1:
            return v_int(_ffi.mb_call0l(args[0].iv))
        return v_int(0)
    if bid == 40:             # _native_call1l(fn, a) -> int
        if len(args) > 1 and args[0].tag == 1 and args[1].tag == 1:
            return v_int(_ffi.mb_call1l(args[0].iv, args[1].iv))
        return v_int(0)
    if bid == 41:             # _native_call2l(fn, a, b) -> int
        if len(args) > 2 and args[0].tag == 1 and args[1].tag == 1 \
                and args[2].tag == 1:
            return v_int(_ffi.mb_call2l(args[0].iv, args[1].iv, args[2].iv))
        return v_int(0)
    if bid == 42:             # _native_call3l(fn, a, b, c) -> int
        if len(args) > 3 and args[0].tag == 1 and args[1].tag == 1 \
                and args[2].tag == 1 and args[3].tag == 1:
            return v_int(_ffi.mb_call3l(args[0].iv, args[1].iv, args[2].iv,
                                        args[3].iv))
        return v_int(0)
    if bid == 43:             # _native_call0p(fn) -> ptr
        if len(args) > 0 and args[0].tag == 1:
            return v_int(_ffi.mb_call0p(args[0].iv))
        return v_int(0)
    if bid == 44:             # _native_call1p(fn, a) -> ptr
        if len(args) > 1 and args[0].tag == 1 and args[1].tag == 1:
            return v_int(_ffi.mb_call1p(args[0].iv, args[1].iv))
        return v_int(0)
    if bid == 45:             # _native_call2p(fn, a, b) -> ptr
        if len(args) > 2 and args[0].tag == 1 and args[1].tag == 1 \
                and args[2].tag == 1:
            return v_int(_ffi.mb_call2p(args[0].iv, args[1].iv, args[2].iv))
        return v_int(0)
    if bid == 46:             # _native_call3p(fn, a, b, c) -> ptr
        if len(args) > 3 and args[0].tag == 1 and args[1].tag == 1 \
                and args[2].tag == 1 and args[3].tag == 1:
            return v_int(_ffi.mb_call3p(args[0].iv, args[1].iv, args[2].iv,
                                        args[3].iv))
        return v_int(0)
    return v_none()


def _minmax(st: "St", args: "list[V]", want: "int") -> "V":
    els = new_v_list()
    if len(args) == 1:
        els = materialize(st, args[0])
    else:
        els = args
    if len(els) == 0:
        return v_none()
    best = els[0]
    k = 1
    while k < len(els):
        c = v_cmp(els[k], best)
        if (want < 0 and c < 0) or (want > 0 and c > 0):
            best = els[k]
        k = k + 1
    return best


def _sort(els: "list[V]") -> "int":
    n = len(els)
    i = 1
    while i < n:
        key = els[i]
        j = i - 1
        while j >= 0 and v_cmp(els[j], key) > 0:
            els[j + 1] = els[j]
            j = j - 1
        els[j + 1] = key
        i = i + 1
    return 0


def do_method(st: "St", mid: "long", args: "list[V]") -> "V":
    recv = args[0]
    if mid == 100:             # append
        items_of(st, recv).append(args[1])
        return v_none()
    if mid == 101:             # pop
        items = items_of(st, recv)
        pop_n = len(items)
        if len(args) >= 2:                 # pop(i): shift left, then drop last
            pop_i = _norm_index(args[1].iv, pop_n)
            pop_saved = items[pop_i]
            pop_j = pop_i
            while pop_j < pop_n - 1:
                items[pop_j] = items[pop_j + 1]
                pop_j = pop_j + 1
            items.pop()                    # no-arg -> list_pop (removes last)
            return pop_saved
        return items.pop()                 # no-arg -> list_pop (removes last)
    if mid == 102:             # dict.get
        cont = st.heap[recv.iv]
        dj = dict_lookup(st, cont, args[1])
        if dj >= 0:
            return cont.items[dj + 1]
        if len(args) >= 3:
            return args[2]
        return v_none()
    if mid == 103:             # dict.keys
        out = new_v_list()
        items = items_of(st, recv)
        k = 0
        while k < len(items):
            out.append(items[k])
            k = k + 2
        return v_container(st, 7, 0, out)
    if mid == 104:             # dict.values
        out = new_v_list()
        items = items_of(st, recv)
        k = 1
        while k < len(items):
            out.append(items[k])
            k = k + 2
        return v_container(st, 7, 0, out)
    if mid == 105:             # dict.items -> list of [k, v]
        out = new_v_list()
        items = items_of(st, recv)
        k = 0
        while k < len(items):
            pair = new_v_list()
            pair.append(items[k])
            pair.append(items[k + 1])
            out.append(v_container(st, 10, 3, pair))
            k = k + 2
        return v_container(st, 7, 0, out)
    if mid == 106:             # set.add
        _set_add(st, recv, args[1])
        return v_none()
    if mid == 130 or mid == 131:   # set.discard(x) / set.remove(x) / list.remove(x)
        # Both were missing entirely -- not in compiler.METHODS, so the call
        # lowered to nothing and quietly did nothing. That made
        # `numreg.discard(r)` a no-op inside the compiler itself, which left
        # stale registers marked numeric and mis-specialised arithmetic.
        #
        # A set stores its elements as a flat item list scanned linearly (see
        # set_has / _set_add), so removal is a scan, a shift-down and a pop --
        # there is no bucket index to repair. A list uses the same layout, so
        # the same loop serves list.remove.
        items = items_of(st, recv)
        j = 0
        while j < len(items):
            if v_eq_bool(st, items[j], args[1]) != 0:
                k = j
                while k + 1 < len(items):
                    items[k] = items[k + 1]
                    k = k + 1
                items.pop()
                return v_none()
            j = j + 1
        if mid == 131:
            # discard() on a missing element is defined to do nothing; remove()
            # raises -- KeyError from a set, ValueError from a list, matching
            # CPython.
            if recv.tag == 9:
                raise_named_error(st, "KeyError")
            else:
                raise_named_error(st, "ValueError")
        return v_none()
    if mid == 110:             # startswith (str, or tuple/list of prefixes)
        if args[1].tag == 10 or args[1].tag == 7:
            pref = items_of(st, args[1])
            pk = 0
            while pk < len(pref):
                if pref[pk].tag == 3 and recv.sv.startswith(pref[pk].sv):
                    return v_bool(1)
                pk = pk + 1
            return v_bool(0)
        if args[1].tag == 3:
            return v_bool(1 if recv.sv.startswith(args[1].sv) else 0)
        return v_bool(0)
    if mid == 111:             # endswith (str, or tuple/list of suffixes)
        if args[1].tag == 10 or args[1].tag == 7:
            suf = items_of(st, args[1])
            sk = 0
            while sk < len(suf):
                if suf[sk].tag == 3 and recv.sv.endswith(suf[sk].sv):
                    return v_bool(1)
                sk = sk + 1
            return v_bool(0)
        if args[1].tag == 3:
            return v_bool(1 if recv.sv.endswith(args[1].sv) else 0)
        return v_bool(0)
    if mid == 107:             # str.split([sep])
        s = recv.sv
        out = new_v_list()
        if len(args) >= 2:
            sep = args[1].sv
            start: "long" = 0
            idx = _find_sub(s, sep, start)
            while idx >= 0:
                out.append(v_str(s[start:idx]))
                start = idx + len(sep)
                idx = _find_sub(s, sep, start)
            out.append(v_str(s[start:len(s)]))
        else:                              # no sep: split on whitespace runs
            cur = ""
            k = 0
            while k < len(s):
                if _is_ws(s[k]) != 0:
                    if len(cur) > 0:
                        out.append(v_str(cur)); cur = ""
                else:
                    cur = cur + s[k]
                k = k + 1
            if len(cur) > 0:
                out.append(v_str(cur))
        return v_container(st, 7, 0, out)
    if mid == 108:             # str.join(iterable)
        # Build a list of the pieces and let py2c lower `sep.join(...)` to the
        # runtime's pyjoin, which sums the lengths and fills one allocation.
        # Accumulating with `out = out + e.sv` instead made join quadratic: each
        # append copied the whole prefix, so joining n pieces allocated O(n^2)
        # bytes. On a self-hosted compile this single method was 964 MB of the
        # 1384 MB arena -- 70% of all memory the interpreter touched.
        #
        # `parts` is deliberately an unannotated list literal so it lowers to a
        # generic obj list; pyjoin indexes it through pystr(). A "list[str]"
        # annotation would make it a typed _tlist_str and the cast into pyjoin's
        # obj parameter would be the same struct pun that broke the collector.
        sep = recv.sv
        parts = []
        for e in materialize(st, args[1]):
            parts.append(e.sv)
        return v_str(sep.join(parts))
    if mid == 109:             # str.strip (whitespace, both ends)
        return v_str(_lstrip(_rstrip(recv.sv)))
    if mid == 112:             # str.find(sub)
        return v_int(_find_sub(recv.sv, args[1].sv, 0))
    if mid == 113:             # str.replace(old, new)
        return v_str(_replace_all(recv.sv, args[1].sv, args[2].sv))
    if mid == 114:             # upper
        return v_str(recv.sv.upper())
    if mid == 115:             # lower
        return v_str(recv.sv.lower())
    if mid == 116:             # list.extend
        items = items_of(st, recv)
        for e in materialize(st, args[1]):
            items.append(e)
        return v_none()
    if mid == 117:             # list.insert(i, val)
        items = items_of(st, recv)
        i = _norm_index(args[1].iv, len(items) + 1)
        items.append(v_none())             # grow by one, then shift right
        j = len(items) - 1
        while j > i:
            items[j] = items[j - 1]
            j = j - 1
        items[i] = args[2]
        return v_none()
    if mid == 118:             # index(val)
        items = items_of(st, recv)
        j = 0
        while j < len(items):
            if v_eq_bool(st, items[j], args[1]) != 0:
                return v_int(j)
            j = j + 1
        return v_int(-1)
    if mid == 119:             # count(val)
        items = items_of(st, recv)
        c = 0
        j = 0
        while j < len(items):
            if v_eq_bool(st, items[j], args[1]) != 0:
                c = c + 1
            j = j + 1
        return v_int(c)
    if mid == 120:             # dict.update(other)
        other = items_of(st, args[1])
        j = 0
        while j < len(other):
            v_setindex(st, recv, other[j], other[j + 1])
            j = j + 2
        return v_none()
    if mid == 121:             # dict.setdefault(key[, default])
        cont = st.heap[recv.iv]
        dj = dict_lookup(st, cont, args[1])
        if dj >= 0:
            return cont.items[dj + 1]
        dv = v_none()
        if len(args) >= 3:
            dv = args[2]
        v_setindex(st, recv, args[1], dv)
        return dv
    if mid == 122:             # str.splitlines
        out = new_v_list()
        s = recv.sv
        cur = ""
        k = 0
        while k < len(s):
            ch = s[k]
            if ord(ch) == 10:
                out.append(v_str(cur)); cur = ""
            elif ord(ch) == 13:
                k = k + 1
                continue
            else:
                cur = cur + ch
            k = k + 1
        if len(cur) > 0:
            out.append(v_str(cur))
        return v_container(st, 7, 0, out)
    if mid == 123:             # str.rstrip (whitespace)
        return v_str(_rstrip(recv.sv))
    if mid == 124:             # str.lstrip (whitespace)
        return v_str(_lstrip(recv.sv))
    if mid == 125:             # str.isdigit
        s = recv.sv
        if len(s) == 0:
            return v_bool(0)
        k = 0
        while k < len(s):
            o = ord(s[k])
            if o < 48 or o > 57:
                return v_bool(0)
            k = k + 1
        return v_bool(1)
    if mid == 126:             # str.isupper
        s = recv.sv
        up = 0
        lo = 0
        k = 0
        while k < len(s):
            o = ord(s[k])
            if o >= 65 and o <= 90:
                up = up + 1
            elif o >= 97 and o <= 122:
                lo = lo + 1
            k = k + 1
        return v_bool(1 if (up > 0 and lo == 0) else 0)
    if mid == 127:             # str.islower
        s = recv.sv
        up = 0
        lo = 0
        k = 0
        while k < len(s):
            o = ord(s[k])
            if o >= 65 and o <= 90:
                up = up + 1
            elif o >= 97 and o <= 122:
                lo = lo + 1
            k = k + 1
        return v_bool(1 if (lo > 0 and up == 0) else 0)
    if mid == 128:             # str.isalpha
        s = recv.sv
        if len(s) == 0:
            return v_bool(0)
        k = 0
        while k < len(s):
            o = ord(s[k])
            if not ((o >= 65 and o <= 90) or (o >= 97 and o <= 122)):
                return v_bool(0)
            k = k + 1
        return v_bool(1)
    if mid == 129:             # str.isalnum
        s = recv.sv
        if len(s) == 0:
            return v_bool(0)
        k = 0
        while k < len(s):
            o = ord(s[k])
            if not ((o >= 48 and o <= 57) or (o >= 65 and o <= 90)
                    or (o >= 97 and o <= 122)):
                return v_bool(0)
            k = k + 1
        return v_bool(1)
    return v_none()


def bind_kwargs(st: "St", callee: "V", posvals: "list[V]",
                kwnames: "list[V]", kwvals: "list[V]") -> "list[V]":
    # Resolve a mix of positional + keyword arguments into a complete positional
    # args list (excluding self, which do_call/instantiate prepends per callee
    # kind). Needs the callee's parameter names, so it resolves the target func:
    #   tag 5  plain function        -> its own params, no self
    #   tag 13 class (constructor)   -> __init__ params, param 0 is self
    #   tag 14 bound user method     -> method params, param 0 is self
    fidx = -1
    self_off = 0
    if callee.tag == 5:
        fidx = callee.iv
        self_off = 0
    elif callee.tag == 13:
        fidx = lookup_method(st, callee.iv, "__init__")
        self_off = 1
    elif callee.tag == 14:
        fidx = callee.iv % _METH_SHIFT
        self_off = 1
    if fidx < 0:
        # no parameter metadata (e.g. builtin, or class without __init__):
        # best-effort positional pass-through, keyword values appended in order
        out = new_v_list()
        i = 0
        while i < len(posvals):
            out.append(posvals[i]); i = i + 1
        i = 0
        while i < len(kwvals):
            out.append(kwvals[i]); i = i + 1
        return out
    fn = st.prog.funcs[fidx]
    nvis = fn.nparams - self_off
    out = new_v_list()
    p = 0
    while p < nvis:
        if p < len(posvals):                 # positional args fill leftmost params
            out.append(posvals[p])
        else:
            found = -1                        # else look for a matching keyword
            j = 0
            while j < len(kwnames):
                if _strcmp(fn.params[self_off + p], kwnames[j].sv) == 0:
                    found = j
                    j = len(kwnames)
                else:
                    j = j + 1
            if found >= 0:
                out.append(kwvals[found])
            else:                             # else the param's default, else None
                di = self_off + p
                if di < len(fn.defaults) and fn.defaults[di] >= 0:
                    out.append(const_to_v(st.prog, fn.defaults[di]))
                else:
                    out.append(v_none())
        p = p + 1
    return out


def do_call(st: "St", callee: "V", args: "list[V]") -> "V":
    if callee.tag == 5:
        return run_func(st, callee.iv, args)
    if callee.tag == 6:
        return do_builtin(st, callee.iv, args)
    if callee.tag == 13:                   # CLASS -> instantiate
        return instantiate(st, callee.iv, args)
    if callee.tag == 14:                   # bound user method (packed, no Cont)
        fidx = callee.iv % _METH_SHIFT
        hidx = callee.iv // _METH_SHIFT
        if len(st.regpool) > 0:            # pooled arg list (was leaked before)
            callargs = st.regpool.pop()
            while len(callargs) > 0:
                callargs.pop()
        else:
            callargs = new_v_list()
        recv12 = V(12, hidx)
        recv12.jitcode = st.heap[hidx].cursor   # keep class-id cache valid for self
        callargs.append(recv12)
        for a in args:
            callargs.append(a)
        r = run_func(st, fidx, callargs)
        st.regpool.append(callargs)
        return r
    if callee.tag == 15:                   # bound builtin method
        cont = st.heap[callee.iv]
        if len(st.regpool) > 0:
            callargs = st.regpool.pop()
            while len(callargs) > 0:
                callargs.pop()
        else:
            callargs = new_v_list()
        callargs.append(cont.items[0])
        for a in args:
            callargs.append(a)
        if cont.items[0].tag == 16:        # bound file method: name in items[1]
            r = file_method(st, cont.items[1].sv, callargs)
        else:
            r = do_builtin(st, cont.cursor, callargs)
        st.regpool.append(callargs)
        return r
    return v_none()


# ---- the dispatch loop ----
def _lget(lst, i):
    return lst[i]                            # py2c lowers calls to a raw data[i]


def _lset(lst, i, v):
    lst[i] = v                               # py2c lowers calls to a raw data[i]=


def run_func(st: "St", fidx: "long", args: "list[V]") -> "V":
    fn = st.prog.funcs[fidx]
    nr = fn.nregs
    if fn.nparams > nr:
        nr = fn.nparams
    nloc = fn.nlocals                        # named locals occupy regs 0..nloc-1;
    if nloc > nr:                            # temps (nloc..nr-1) are always written
        nloc = nr                            # before read, so need no clearing
    if len(st.regpool) > 0:                  # reuse a frame (calls are nested)
        regs = st.regpool.pop()
    else:
        regs = new_v_list()
    st.frames.append(regs)                   # live until this frame returns
    na = len(args)
    np = fn.nparams
    va = fn.vararg                           # reg of *args param, or -1
    k = 0
    while k < np:                            # parameters receive the arguments
        if va >= 0 and k == va:              # *args: collect the rest into a tuple
            rest = new_v_list()
            m = va
            while m < na:
                rest.append(args[m])
                m = m + 1
            rv = v_container(st, 10, 0, rest)
        elif k < na:
            rv = args[k]
        else:
            rv = v_none()
        if k < len(regs):
            _lset(regs, k, rv)
        else:
            regs.append(rv)
        k = k + 1
    while k < nloc:                          # other named locals: clear to None so
        if k < len(regs):                    # an unbound read is None, not stale data
            _lset(regs, k, v_none())
        else:
            regs.append(v_none())
        k = k + 1
    while len(regs) < nr:                    # grow to nr; reused temp slots are left
        regs.append(v_none())                # untouched (overwritten before any read)
    if na < np:                             # supply defaults for missing params
        defs = fn.defaults
        i = na
        while i < np:
            if i < len(defs) and defs[i] >= 0:
                _lset(regs, i, const_to_v(st.prog, defs[i]))
            i = i + 1

    code = fn.code
    n = len(code)
    blocks = _empty_blocks                   # shared empty sentinel; a private
    has_blocks = 0                           # list is allocated on first handler
    bn = 0                                  # block-stack depth (list[int].pop is
    pc = 0                                  # miscompiled by py2c, so index by bn)
    while pc < n:
        # Safepoint. Between two opcodes every live value is in a register,
        # in a global, or in `st.exc_val` -- an opcode is the unit that
        # takes values out of registers and puts results back, so nothing is
        # in flight here. That is what makes the root set complete, and it
        # is why the trigger is here rather than in the allocator.
        if GC_ON != 0 and (len(st.heap) - st.nfree) >= st.gc_next:
            if has_blocks != 0:
                _gc_mark_frame_blocks(st, blocks, bn)
            gc_collect(st)
        ins = _lget(code, pc)
        op = ins.op
        a = ins.ra                          # flags already separated at load time,
        b = ins.b                           # so a is the real register; ra mirrors
        c = ins.c                           # it for the ops that also test fb/fc
        ra = a
        fb = ins.fb
        fc = ins.fc
        if op == 1:
            _lset(regs, a, const_to_v(st.prog, b)); pc = pc + 1
        elif op == 2:
            gv = _lget(st.glob, b)
            if gv.tag == 17:                   # never assigned -> NameError
                # Globals start as a sentinel distinct from None, so reading a
                # name that was never bound raises instead of silently
                # yielding None and letting execution continue. The sentinel
                # never escapes this branch, so no other tag check sees it.
                raise_named_error(st, "NameError")
            else:
                _lset(regs, a, gv); pc = pc + 1
        elif op == 3:
            if fb == 1:                    # reclaimable global: free old value
                ov = _lget(st.glob, b)
                _lset(st.glob, b, _lget(regs, ra))
                _free_v(ov)
            else:
                _lset(st.glob, b, _lget(regs, ra))
            pc = pc + 1
        elif op == 4:
            _lset(regs, a, _lget(regs, b)); pc = pc + 1
        elif op == 5:
            rret = _lget(regs, a)
            st.frames.pop()
            st.regpool.append(regs)
            return rret
        elif op == 6:
            pc = a
        elif op == 7:
            if truthy(st, _lget(regs, a)) != 0:
                pc = pc + 1
            else:
                pc = b
        elif op == 76:                     # JF_LT: if not(a < c) -> pc = b
            if v_cmp(_lget(regs, a), _lget(regs, c)) < 0:
                pc = pc + 1
            else:
                pc = b
        elif op == 77:                     # JF_LE
            if v_cmp(_lget(regs, a), _lget(regs, c)) <= 0:
                pc = pc + 1
            else:
                pc = b
        elif op == 78:                     # JF_GT
            if v_cmp(_lget(regs, a), _lget(regs, c)) > 0:
                pc = pc + 1
            else:
                pc = b
        elif op == 79:                     # JF_GE
            if v_cmp(_lget(regs, a), _lget(regs, c)) >= 0:
                pc = pc + 1
            else:
                pc = b
        elif op == 80:                     # JF_EQ: if not(a == c) -> pc = b
            if v_eq_bool(st, _lget(regs, a), _lget(regs, c)) != 0:
                pc = pc + 1
            else:
                pc = b
        elif op == 81:                     # JF_NE: if not(a != c) -> pc = b
            if v_eq_bool(st, _lget(regs, a), _lget(regs, c)) == 0:
                pc = pc + 1
            else:
                pc = b
        elif op == 82:                     # JUMP_IF_TRUE
            if truthy(st, _lget(regs, a)) != 0:
                pc = b
            else:
                pc = pc + 1
        elif op == 83:                     # JT_LT: if (a < c) -> pc = b
            if v_cmp(_lget(regs, a), _lget(regs, c)) < 0:
                pc = b
            else:
                pc = pc + 1
        elif op == 84:                     # JT_LE
            if v_cmp(_lget(regs, a), _lget(regs, c)) <= 0:
                pc = b
            else:
                pc = pc + 1
        elif op == 85:                     # JT_GT
            if v_cmp(_lget(regs, a), _lget(regs, c)) > 0:
                pc = b
            else:
                pc = pc + 1
        elif op == 86:                     # JT_GE
            if v_cmp(_lget(regs, a), _lget(regs, c)) >= 0:
                pc = b
            else:
                pc = pc + 1
        elif op == 87:                     # JT_EQ: if (a == c) -> pc = b
            if v_eq_bool(st, _lget(regs, a), _lget(regs, c)) != 0:
                pc = b
            else:
                pc = pc + 1
        elif op == 88:                     # JT_NE: if (a != c) -> pc = b
            if v_eq_bool(st, _lget(regs, a), _lget(regs, c)) == 0:
                pc = b
            else:
                pc = pc + 1
        elif op == 8:
            callee = _lget(regs, b)
            if len(st.regpool) > 0:
                cargs = st.regpool.pop()
                while len(cargs) > 0:
                    cargs.pop()
            else:
                cargs = new_v_list()
            j = 0
            while j < c:
                cargs.append(_lget(regs, b + 1 + j))
                j = j + 1
            rcall = do_call(st, callee, cargs)
            st.regpool.append(cargs)
            _lset(regs, a, rcall); pc = pc + 1
        elif op == 91:                     # CALL_SPREAD: reg[a]=reg[b](fixed,*iter)
            callee = _lget(regs, b)
            cargs = new_v_list()
            j = 0
            while j < c:
                cargs.append(_lget(regs, b + 1 + j))
                j = j + 1
            it = _lget(regs, b + 1 + c)
            if it.tag == 7 or it.tag == 10:
                iitems = items_of(st, it)
                m = 0
                while m < len(iitems):
                    cargs.append(iitems[m])
                    m = m + 1
            elif it.tag == 3:              # spread a string into its characters
                s = it.sv
                m = 0
                while m < len(s):
                    cargs.append(v_str(s[m]))
                    m = m + 1
            rcall = do_call(st, callee, cargs)
            _lset(regs, a, rcall); pc = pc + 1
        elif op == 90:                     # CALL_KW: c = npos*256 + nkw
            # window: reg[b]=callee, [b+1..b+npos]=positional values,
            # [b+1+npos..b+npos+nkw]=keyword values,
            # [b+1+npos+nkw..b+npos+2*nkw]=keyword-name strings
            npos = c // 256
            nkw = c % 256
            callee = _lget(regs, b)
            posvals = new_v_list()
            j = 0
            while j < npos:
                posvals.append(_lget(regs, b + 1 + j)); j = j + 1
            kwvals = new_v_list()
            j = 0
            while j < nkw:
                kwvals.append(_lget(regs, b + 1 + npos + j)); j = j + 1
            kwnames = new_v_list()
            j = 0
            while j < nkw:
                kwnames.append(_lget(regs, b + 1 + npos + nkw + j)); j = j + 1
            fullargs = bind_kwargs(st, callee, posvals, kwnames, kwvals)
            rcall = do_call(st, callee, fullargs)
            _lset(regs, a, rcall); pc = pc + 1
        elif op == 89:                     # CALL_FUNC: direct call, c = fidx*256+nargs
            fnum = c // 256
            nargs = c % 256
            if len(st.regpool) > 0:
                fargs = st.regpool.pop()
                while len(fargs) > 0:
                    fargs.pop()
            else:
                fargs = new_v_list()
            j = 0
            while j < nargs:
                fargs.append(_lget(regs, b + j))
                j = j + 1
            rfc = run_func(st, fnum, fargs)
            st.regpool.append(fargs)
            _lset(regs, a, rfc); pc = pc + 1
        elif op == 9:                      # BUILD_LIST
            items = new_v_list()
            j = 0
            while j < c:
                items.append(_lget(regs, b + j)); j = j + 1
            _lset(regs, a, v_container(st, 7, 0, items)); pc = pc + 1
        elif op == 10:                     # BUILD_TUPLE
            items = new_v_list()
            j = 0
            while j < c:
                items.append(_lget(regs, b + j)); j = j + 1
            _lset(regs, a, v_container(st, 10, 3, items)); pc = pc + 1
        elif op == 11:                     # BUILD_DICT
            dv = v_container(st, 8, 1, new_v_list())
            j = 0
            while j < c:
                v_setindex(st, dv, _lget(regs, b + 2 * j), _lget(regs, b + 2 * j + 1))
                j = j + 1
            _lset(regs, a, dv); pc = pc + 1
        elif op == 12:                     # BUILD_SET
            sv = v_container(st, 9, 2, new_v_list())
            j = 0
            while j < c:
                _set_add(st, sv, _lget(regs, b + j)); j = j + 1
            _lset(regs, a, sv); pc = pc + 1
        elif op == 13:                     # INDEX
            _lset(regs, a, v_index(st, _lget(regs, b), _lget(regs, c))); pc = pc + 1
        elif op == 14:                     # SETINDEX
            v_setindex(st, _lget(regs, a), _lget(regs, b), _lget(regs, c)); pc = pc + 1
        elif op == 54:                     # INDEX_INT (typed list[int], no dispatch)
            _lset(regs, a, st.heap[_lget(regs, b).iv].items[_lget(regs, c).iv]); pc = pc + 1
        elif op == 55:                     # SETINDEX_INT (typed list[int])
            st.heap[_lget(regs, a).iv].items[_lget(regs, b).iv] = _lget(regs, c); pc = pc + 1
        elif op == 56:                     # ACC_ADD_GINT: glob[a] += tlist[b][c]
            ov = _lget(st.glob, a)
            _lset(st.glob, a, v_int(ov.iv + st.heap[_lget(regs, b).iv].items[_lget(regs, c).iv].iv))
            _free_v(ov)
            pc = pc + 1
        elif op == 57:                     # ACC_MAC_GINT: glob[a] += tA[k]*tB[j]
            rar = b // 4096
            rki = b % 4096
            rbr = c // 4096
            rji = c % 4096
            prod = st.heap[_lget(regs, rar).iv].items[_lget(regs, rki).iv].iv * st.heap[_lget(regs, rbr).iv].items[_lget(regs, rji).iv].iv
            ovm = _lget(st.glob, a)
            _lset(st.glob, a, v_int(ovm.iv + prod))
            _free_v(ovm)
            pc = pc + 1
        elif op == 58:                     # ACC_ADD_G: glob[a] = glob[a] + reg[b]
            oa = _lget(st.glob, a)
            bb = _lget(regs, b)
            _lset(st.glob, a, v_add(st, oa, bb))
            _free_v(oa)
            if c == 1:                     # rhs was a fresh arith temp -> reclaim it
                _free_v(bb)
            pc = pc + 1
        elif op == 67:                     # ACC_ADD_L: reg[a] = reg[a] + reg[b]; reclaim
            la = _lget(regs, a)
            lb = _lget(regs, b)
            _lset(regs, a, v_add(st, la, lb))
            _free_v(la)
            if c == 1:
                _free_v(lb)
            pc = pc + 1
        elif op == 68:                     # ACC_SUB_L: reg[a] = reg[a] - reg[b]; reclaim
            la = _lget(regs, a)
            lb = _lget(regs, b)
            _lset(regs, a, v_sub(st, la, lb))
            _free_v(la)
            if c == 1:
                _free_v(lb)
            pc = pc + 1
        elif op == 15:                     # ITER_NEW
            _lset(regs, a, v_iter(st, _lget(regs, b))); pc = pc + 1
        elif op == 16:                     # ITER_NEXT
            it = _lget(regs, b)
            cont = st.heap[it.iv]
            if cont.cursor < len(cont.items):
                _lset(regs, a, cont.items[cont.cursor])
                cont.cursor = cont.cursor + 1
                pc = pc + 1
            else:
                pc = c
        elif op == 17:                     # CONTAINS
            _lset(regs, a, v_contains(st, _lget(regs, b), _lget(regs, c))); pc = pc + 1
        elif op == 18:                     # LIST_APPEND
            items_of(st, _lget(regs, a)).append(_lget(regs, b)); pc = pc + 1
        elif op == 19:                     # SET_ADD
            _set_add(st, _lget(regs, a), _lget(regs, b)); pc = pc + 1
        elif op == 50:                     # LOAD_ATTR
            cs = st.prog.consts
            nm = cs[c].s
            ob = _lget(regs, b)
            if ob.tag == 13 and _strcmp(nm, "__name__") == 0:
                cn = st.prog.classes[ob.iv].cname
                di = len(cn) - 1               # strip any link prefix ($ast$Name)
                cut = -1
                while di >= 0:
                    if cn[di] == "$":
                        cut = di
                        break
                    di = di - 1
                if cut >= 0:
                    cn = cn[cut + 1:len(cn)]
                _lset(regs, a, v_str(cn)); pc = pc + 1
            elif ob.tag == 0:                  # attribute on None -> AttributeError
                raise_attr_error(st)
                _lset(regs, a, v_none()); pc = pc + 1
            else:
                _lset(regs, a, inst_get(st, ob, nm)); pc = pc + 1
        elif op == 51:                     # STORE_ATTR
            cs = st.prog.consts
            nm = cs[c].s
            inst_set(st, _lget(regs, a), nm, _lget(regs, b)); pc = pc + 1
        elif op == 52:                     # LOAD_METHOD
            cs = st.prog.consts
            nm = cs[c].s
            obj = _lget(regs, b)
            if obj.tag == 12:              # instance -> bound user method
                fidx = lookup_method(st, st.heap[obj.iv].cursor, nm)
                _lset(regs, a, V(14, obj.iv * _METH_SHIFT + fidx))   # packed, no alloc
            elif obj.tag == 0:             # method on None -> AttributeError
                raise_attr_error(st)
                _lset(regs, a, v_none())
            elif obj.tag == 16:            # file object -> bound file method
                bargs = new_v_list(); bargs.append(obj); bargs.append(v_str(nm))
                _lset(regs, a, v_container(st, 15, 7, bargs))   # name kept in items[1]
            else:                          # container/str -> bound builtin
                bargs = new_v_list(); bargs.append(obj)
                _lset(regs, a, v_container(st, 15, 7, bargs))
                st.heap[_lget(regs, a).iv].cursor = method_id(nm)
            pc = pc + 1
        elif op == 53:                     # CALL_METHOD (fused load+call)
            cs = st.prog.consts
            nargs = c % 256
            nm = cs[c // 256].s
            recv = _lget(regs, b)
            if len(st.regpool) > 0:
                callargs = st.regpool.pop()
                while len(callargs) > 0:
                    callargs.pop()
            else:
                callargs = new_v_list()
            callargs.append(recv)
            ka = 0
            while ka < nargs:
                callargs.append(_lget(regs, b + 1 + ka))
                ka = ka + 1
            if recv.tag == 12:             # instance -> user method, no binding
                fidx = mcache_lookup(st, recv.jitcode, c // 256, nm)
                _lset(regs, a, run_func(st, fidx, callargs))
            elif recv.tag == 16:           # file object -> buffered file method
                _lset(regs, a, file_method(st, nm, callargs))
            elif recv.tag == 0:            # method on None -> AttributeError
                raise_attr_error(st)
                _lset(regs, a, v_none())
            else:                          # container/str -> builtin method
                _lset(regs, a, do_builtin(st, method_id(nm), callargs))
            st.regpool.append(callargs)
            pc = pc + 1
        elif op == 20:
            ob = _lget(regs, b); oc = _lget(regs, c)
            _lset(regs, ra, v_add(st, ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 21:
            ob = _lget(regs, b); oc = _lget(regs, c)
            _lset(regs, ra, v_sub(st, ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 22:
            ob = _lget(regs, b); oc = _lget(regs, c)
            _lset(regs, ra, v_mul(st, ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 23:
            ob = _lget(regs, b); oc = _lget(regs, c)
            if ob.tag == 12:                # instance: dispatch __truediv__
                argl = new_v_list()
                argl.append(ob)
                argl.append(oc)
                _lset(regs, ra, run_func(st, lookup_method(
                    st, st.heap[ob.iv].cursor, "__truediv__"), argl))
            else:
                _lset(regs, ra, v_div(ob, oc))
                if fc == 1:
                    _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 24:
            ob = _lget(regs, b); oc = _lget(regs, c)
            _lset(regs, ra, v_mod(st, ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 25:
            ob = _lget(regs, b); oc = _lget(regs, c)
            _lset(regs, ra, v_floordiv(ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 26:
            _lset(regs, a, v_pow(_lget(regs, b), _lget(regs, c))); pc = pc + 1
        elif op == 27:
            _lset(regs, a, v_bitor(st, _lget(regs, b), _lget(regs, c))); pc = pc + 1
        elif op == 28:
            _lset(regs, a, v_bitand(st, _lget(regs, b), _lget(regs, c))); pc = pc + 1
        elif op == 29:
            _lset(regs, a, v_bitxor(st, _lget(regs, b), _lget(regs, c))); pc = pc + 1
        elif op == 36:
            _lset(regs, a, v_int(_lget(regs, b).iv << _lget(regs, c).iv)); pc = pc + 1
        elif op == 37:
            _lset(regs, a, v_int(_lget(regs, b).iv >> _lget(regs, c).iv)); pc = pc + 1
        elif op == 38:
            _lset(regs, a, v_slice(st, _lget(regs, a), _lget(regs, b), _lget(regs, b + 1), _lget(regs, b + 2))); pc = pc + 1
        elif op == 39:
            v_slice_store(st, _lget(regs, a), _lget(regs, b), _lget(regs, b + 1), _lget(regs, b + 2)); pc = pc + 1
        elif op == 30:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) < 0 else 0)); pc = pc + 1
        elif op == 31:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) <= 0 else 0)); pc = pc + 1
        elif op == 32:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) > 0 else 0)); pc = pc + 1
        elif op == 33:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) >= 0 else 0)); pc = pc + 1
        elif op == 34:
            _lset(regs, a, v_bool(v_eq_bool(st, _lget(regs, b), _lget(regs, c)))); pc = pc + 1
        elif op == 35:
            _lset(regs, a, v_bool(1 if v_eq_bool(st, _lget(regs, b), _lget(regs, c)) == 0 else 0)); pc = pc + 1
        elif op == 59:                     # IS: tag-strict identity (1 is True -> 0)
            ob = _lget(regs, b); oc = _lget(regs, c)
            if ob.tag == oc.tag and v_eq_bool(st, ob, oc) == 1:
                _lset(regs, a, v_bool(1))
            else:
                _lset(regs, a, v_bool(0))
            pc = pc + 1
        elif op == 40:
            _lset(regs, a, v_neg(_lget(regs, b))); pc = pc + 1
        elif op == 41:
            _lset(regs, a, v_bool(1 if truthy(st, _lget(regs, b)) == 0 else 0)); pc = pc + 1
        elif op == 60:
            ob = _lget(regs, b); oc = _lget(regs, c)
            if ob.tag == 1 and oc.tag == 1:
                _lset(regs, ra, v_int(ob.iv + oc.iv))
            else:
                _lset(regs, ra, v_add(st, ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 61:
            ob = _lget(regs, b); oc = _lget(regs, c)
            if ob.tag == 1 and oc.tag == 1:
                _lset(regs, ra, v_int(ob.iv - oc.iv))
            else:
                _lset(regs, ra, v_sub(st, ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 62:
            ob = _lget(regs, b); oc = _lget(regs, c)
            if ob.tag == 1 and oc.tag == 1:
                _lset(regs, ra, v_int(ob.iv * oc.iv))
            else:
                _lset(regs, ra, v_mul(st, ob, oc))
            if fc == 1:
                _free_v(oc)
            if fb == 1:
                _free_v(ob)
            pc = pc + 1
        elif op == 63:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) < 0 else 0)); pc = pc + 1
        elif op == 64:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) <= 0 else 0)); pc = pc + 1
        elif op == 65:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) > 0 else 0)); pc = pc + 1
        elif op == 66:
            _lset(regs, a, v_bool(1 if v_cmp(_lget(regs, b), _lget(regs, c)) >= 0 else 0)); pc = pc + 1
        elif op == 70:                     # SETUP_EXCEPT
            if has_blocks == 0:            # leave the shared sentinel untouched
                blocks = new_v_list()
                has_blocks = 1
            if bn < len(blocks):
                blocks[bn] = v_int(a)
            else:
                blocks.append(v_int(a))
            bn = bn + 1
            pc = pc + 1
        elif op == 71:                     # POP_BLOCK
            bn = bn - 1; pc = pc + 1
        elif op == 72:                     # RAISE
            ev = _lget(regs, a)
            if ev.tag == 13:               # raising a bare class -> instantiate
                ev = instantiate(st, ev.iv, new_v_list())
            st.exc_val = ev
            st.exc_flag = 1
        elif op == 73:                     # RERAISE
            st.exc_flag = 1
        elif op == 74:                     # LOAD_EXC
            _lset(regs, a, st.exc_val); pc = pc + 1
        elif op == 75:                     # EXC_MATCH
            _lset(regs, a, v_bool(is_instance(st, st.exc_val, _lget(regs, b)))); pc = pc + 1
        else:
            pc = pc + 1
        if st.exc_flag != 0:               # an exception is in flight
            if bn > 0:
                bn = bn - 1
                pc = blocks[bn].iv         # jump to nearest handler
                st.exc_flag = 0
            else:
                st.frames.pop()
                st.regpool.append(regs)
                return v_none()            # propagate to caller
    st.frames.pop()
    st.regpool.append(regs)
    return v_none()


def build_state(prog: "Program", sargs: "list[str]") -> "St":
    # The per-run interpreter state: materialize constants, allocate globals and
    # the method-cache slots, and bind the special names (__argv__/open/...).
    # Factored out of interp_run so an embedder can build the state once, run the
    # module, and then keep the state alive to call functions on demand.
    global _const_vs, _const_vs_ready
    setup_cache()
    _const_vs = new_v_list()               # materialize each constant once
    ci = 0
    while ci < len(prog.consts):
        _const_vs.append(const_to_v_raw(prog, ci))
        ci = ci + 1
    _const_vs_ready = 1
    glob = new_v_list()
    heap = []
    cz = Cont(0, 0, new_v_list(), new_v_list())
    heap.append(cz)                        # heap[0] reserved; anchors list[Cont]
    mcc = new_v_list()
    mcf = new_v_list()
    ncon = len(prog.consts)
    zc = 0
    while zc < ncon:
        mcc.append(v_int(-1))               # -1 == empty cache slot
        mcf.append(v_int(-1))
        zc = zc + 1
    st = St(prog, glob, heap, 0, v_none(), new_reg_pool(), mcc, mcf,
            new_reg_pool(), new_v_list())
    k = 0
    while k < prog.nglobals:
        glob.append(V(17, 0))                  # unset sentinel; see LOAD_GLOBAL
        k = k + 1
    gi = 0
    while gi < len(prog.names) and gi < len(glob):
        nm = prog.names[gi]
        if _strcmp(nm, "__argv__") == 0:
            av = new_v_list()
            av.append(v_str("minipy"))
            ai = 0
            while ai < len(sargs):
                av.append(v_str(sargs[ai]))
                ai = ai + 1
            glob[gi] = v_container(st, 7, 0, av)
        elif _strcmp(nm, "__syspath__") == 0:
            glob[gi] = v_container(st, 7, 0, new_v_list())
        elif _strcmp(nm, "_host_os") == 0:
            # rpy_lib/minios.py guards every filesystem call with
            # `if _host_os is not None`, documenting that native minipy "leaves
            # it unset (None)". But an unset global is the tag-17 sentinel, and
            # LOAD_GLOBAL turns that into a NameError -- so the guard raised
            # instead of falling through to the _native_* path. Bind it to a
            # real None so the intended fallback happens.
            glob[gi] = v_none()
        elif _strcmp(nm, "open") == 0:
            glob[gi] = v_builtin(47)            # do_builtin(47) -> file_open
        elif _strcmp(nm, "_native_exists") == 0:
            glob[gi] = v_builtin(48)            # minios exists/isfile fallback
        elif _strcmp(nm, "_native_makedirs") == 0:
            glob[gi] = v_builtin(32)            # minios makedirs fallback
        elif _strcmp(nm, "_native_dlopen") == 0:
            glob[gi] = v_builtin(33)            # FFI: dlopen a JIT'd .so
        elif _strcmp(nm, "_native_dlsym") == 0:
            glob[gi] = v_builtin(34)            # FFI: resolve a symbol
        elif _strcmp(nm, "_native_call0i") == 0:
            glob[gi] = v_builtin(35)            # FFI: call int f(void)
        elif _strcmp(nm, "_native_call1i") == 0:
            glob[gi] = v_builtin(36)            # FFI: call int f(int)
        elif _strcmp(nm, "_native_call2i") == 0:
            glob[gi] = v_builtin(37)            # FFI: call int f(int,int)
        elif _strcmp(nm, "_native_call3i") == 0:
            glob[gi] = v_builtin(38)            # FFI: call int f(int,int,int)
        elif _strcmp(nm, "_native_call0l") == 0:
            glob[gi] = v_builtin(39)            # FFI: int f(void), ptr-safe
        elif _strcmp(nm, "_native_call1l") == 0:
            glob[gi] = v_builtin(40)            # FFI: int f(long)
        elif _strcmp(nm, "_native_call2l") == 0:
            glob[gi] = v_builtin(41)            # FFI: int f(long,long)
        elif _strcmp(nm, "_native_call3l") == 0:
            glob[gi] = v_builtin(42)            # FFI: int f(long,long,long)
        elif _strcmp(nm, "_native_call0p") == 0:
            glob[gi] = v_builtin(43)            # FFI: ptr f(void)
        elif _strcmp(nm, "_native_call1p") == 0:
            glob[gi] = v_builtin(44)            # FFI: ptr f(long)
        elif _strcmp(nm, "_native_call2p") == 0:
            glob[gi] = v_builtin(45)            # FFI: ptr f(long,long)
        elif _strcmp(nm, "_native_call3p") == 0:
            glob[gi] = v_builtin(46)            # FFI: ptr f(long,long,long)
        gi = gi + 1
    return st


# ---- embedding facades ---------------------------------------------------
# An embedder (e.g. the minibrowser) co-compiles interp.py and drives it through
# these. Program/St never cross the translation-unit boundary -- they are held
# in typed module globals here -- so callers only ever pass char* / int.
_embed_st: "St" = None
_embed_prog: "Program" = None
_embed_ready: "int" = 0


def run_mpyc(path: "char*") -> "int":
    # One-shot: load a .mpyc and run its module top-level to completion.
    prog = load_mpyc(path)
    args: "list[str]" = []
    return interp_run(prog, args)


def mpy_boot(path: "char*") -> "int":
    # Load a .mpyc, build the state, and run the module top-level (which defines
    # the page's functions + globals), keeping the state for later mpy_call().
    global _embed_st, _embed_prog, _embed_ready
    prog = load_mpyc(path)
    if prog.version < 0:
        return 2
    args: "list[str]" = []
    st = build_state(prog, args)
    _embed_st = st
    _embed_prog = prog
    _embed_ready = 1
    run_func(st, prog.entry, new_v_list())
    if st.exc_flag != 0:
        return 1
    return 0


def mpy_call(name: "char*") -> "int":
    # Call a top-level function of the booted module by name, no arguments.
    # Returns 0 ok, 1 the call raised, 2 not booted, 3 name not found/callable.
    prog: "Program" = _embed_prog
    st: "St" = _embed_st
    if _embed_ready == 0:
        return 2
    gi = 0
    while gi < len(prog.names) and gi < len(st.glob):
        if _strcmp(prog.names[gi], name) == 0:
            fv = st.glob[gi]
            if fv.tag != 5:                    # not a function value
                return 3
            do_call(st, fv, new_v_list())
            if st.exc_flag != 0:
                return 1
            return 0
        gi = gi + 1
    return 3


def mpy_call_s(name: "char*") -> "char*":
    # Call a booted top-level function by name (no args) and return its string
    # result (empty string if not booted / not found / raised / non-string).
    # Used to read back serialized DOM / console / alert text from the page.
    prog: "Program" = _embed_prog
    st: "St" = _embed_st
    if _embed_ready == 0:
        return ""
    gi = 0
    while gi < len(prog.names) and gi < len(st.glob):
        if _strcmp(prog.names[gi], name) == 0:
            fv = st.glob[gi]
            if fv.tag != 5:
                return ""
            r = do_call(st, fv, new_v_list())
            if st.exc_flag != 0:
                return ""
            if r.tag == 3:
                return r.sv
            return ""
        gi = gi + 1
    return ""


def mpy_call_i(name: "char*", arg: "int") -> "int":
    # Call a booted top-level function by name with one int argument and return
    # its int result (-1 if not booted / not found / raised). Used to dispatch a
    # click by element handle: mpy_call_i("__fire", handle).
    prog: "Program" = _embed_prog
    st: "St" = _embed_st
    if _embed_ready == 0:
        return -1
    gi = 0
    while gi < len(prog.names) and gi < len(st.glob):
        if _strcmp(prog.names[gi], name) == 0:
            fv = st.glob[gi]
            if fv.tag != 5:
                return -1
            cargs = new_v_list()
            cargs.append(v_int(arg))
            r = do_call(st, fv, cargs)
            if st.exc_flag != 0:
                return -1
            if r.tag == 1:
                return r.iv
            return 0
        gi = gi + 1
    return -1


def mpy_call_is(name: "char*", i: "int", s: "char*") -> "int":
    # Call a booted top-level function by name with one int and one string
    # argument. Used for two-way binding: mpy_call_is("__set_value", handle,
    # typed_text) pushes an edited input's text back into the DOM.
    prog: "Program" = _embed_prog
    st: "St" = _embed_st
    if _embed_ready == 0:
        return -1
    gi = 0
    while gi < len(prog.names) and gi < len(st.glob):
        if _strcmp(prog.names[gi], name) == 0:
            fv = st.glob[gi]
            if fv.tag != 5:
                return -1
            cargs = new_v_list()
            cargs.append(v_int(i))
            cargs.append(v_str(s))
            r = do_call(st, fv, cargs)
            if st.exc_flag != 0:
                return -1
            if r.tag == 1:
                return r.iv
            return 0
        gi = gi + 1
    return -1


def mpy_call_i_s(name: "char*", arg: "int") -> "char*":
    # Call a booted top-level function by name with one int argument and return
    # its string result (empty if not booted/found/raised/non-string). Lets
    # native page code read DOM values back by handle (mb_dom_get_value).
    prog: "Program" = _embed_prog
    st: "St" = _embed_st
    if _embed_ready == 0:
        return ""
    gi = 0
    while gi < len(prog.names) and gi < len(st.glob):
        if _strcmp(prog.names[gi], name) == 0:
            fv = st.glob[gi]
            if fv.tag != 5:
                return ""
            cargs = new_v_list()
            cargs.append(v_int(arg))
            r = do_call(st, fv, cargs)
            if st.exc_flag != 0:
                return ""
            if r.tag == 3:
                return r.sv
            return ""
        gi = gi + 1
    return ""


def mpy_call_iss(name: "char*", i: "int", s1: "char*", s2: "char*") -> "int":
    # Call a booted top-level function by name with one int and two string
    # arguments, returning its int result. Lets native page code create a
    # labelled child element (mb_dom_create_child -> __create_child).
    prog: "Program" = _embed_prog
    st: "St" = _embed_st
    if _embed_ready == 0:
        return -1
    gi = 0
    while gi < len(prog.names) and gi < len(st.glob):
        if _strcmp(prog.names[gi], name) == 0:
            fv = st.glob[gi]
            if fv.tag != 5:
                return -1
            cargs = new_v_list()
            cargs.append(v_int(i))
            cargs.append(v_str(s1))
            cargs.append(v_str(s2))
            r = do_call(st, fv, cargs)
            if st.exc_flag != 0:
                return -1
            if r.tag == 1:
                return r.iv
            return 0
        gi = gi + 1
    return -1


def _exit_status(st: "St", ev: "V") -> "int":
    # SystemExit(n).args[0] as a process status, following CPython: an int is
    # used directly, None (or no argument) means 0, anything else means 1.
    argsv = inst_get(st, ev, "args")
    if argsv.tag != 10:
        return 0
    items = items_of(st, argsv)
    if len(items) == 0:
        return 0
    first = items[0]
    if first.tag == 1:
        return int(first.iv)
    if first.tag == 0:
        return 0
    return 1


def interp_run(prog: "Program", sargs: "list[str]") -> "int":
    st = build_state(prog, sargs)
    run_func(st, prog.entry, new_v_list())
    if st.exc_flag != 0:                        # top-level unhandled exception
        ev = st.exc_val
        desc = "exception"
        if ev.tag == 12:
            cn = st.prog.classes[st.heap[ev.iv].cursor].cname
            di = len(cn) - 1
            cut = -1
            while di >= 0:
                if cn[di] == "$":
                    cut = di
                    di = -1
                else:
                    di = di - 1
            if cut >= 0:
                cn = cn[cut + 1:len(cn)]
            desc = cn
            # An unhandled SystemExit is a normal program ending, not a
            # failure. compiler.py lowers `sys.exit(n)` to `raise
            # SystemExit(n)`, so this is the path every script that calls
            # sys.exit arrives on -- including py2c.py, whose last statement
            # is `sys.exit(main(sys.argv[1:]) or 0)`.
            if _strcmp(cn, "SystemExit") == 0:
                return _exit_status(st, ev)
        print("minipy: unhandled exception: " + desc)
        return 1
    return 0


class _MpycReader:
    """Cursor over the raw bytes of a `.mpyc` file (see tools/minipy/mpyc.py).

    The interpreter reads the file into a byte-addressable buffer and walks it
    with this reader, filling the Program POD structs directly -- no JSON
    tokenising and no per-record dict/object_hook rebuild. The stream is
    NUL-free by construction so the buffer's strlen length is exact and slicing
    is safe.
    """

    buf: "char*"
    pos: "long"

    def __init__(self, buf: "char*"):
        self.buf = buf
        self.pos = 0

    def uvarint(self) -> "long":
        shift = 0
        result = 0
        while True:
            b = ord(self.buf[self.pos])
            self.pos = self.pos + 1
            result = result | ((b & 0x7F) << shift)
            if (b & 0x80) == 0:
                return result - 1               # undo the +1 NUL-avoidance bias
            shift = shift + 7

    def svarint(self) -> "long":
        u = self.uvarint()
        if (u & 1) != 0:
            return -((u >> 1) + 1)              # zig-zag decode
        return u >> 1

    def string(self) -> "char*":
        n = self.uvarint()
        start = self.pos
        s = self.buf[start:start + n]
        self.pos = start + n
        if sys.implementation.name == "cpython":
            # On CPython the file was latin-1 decoded so ord() sees bytes; the
            # slice therefore holds raw UTF-8 bytes as latin-1 chars -- recover
            # the real text. This whole branch folds away in the compiled
            # interpreter, where `s` is already the correct byte-addressed char*.
            s = s.encode("latin-1").decode("utf-8")
        return s

    def double(self) -> "double":
        return _str_to_float(self.string())


def load_mpyc(path: "char*") -> "Program":
    """Load a `.mpyc` binary bytecode file into a Program, bypassing JSON."""
    f = open(path, "rb")
    buf = f.read()
    f.close()
    if sys.implementation.name == "cpython":
        buf = buf.decode("latin-1")
    rdr = _MpycReader(buf)
    if len(buf) < 4 or buf[0:4] != "MPYC":
        print("interp: not a valid .mpyc file (bad magic)")
        return Program(-1, "", [], [], 0, [], [], 0)   # sentinel: main() aborts
    rdr.pos = 4                                    # skip the "MPYC" magic
    version = rdr.uvarint()
    source = rdr.string()

    consts = []
    nconsts = rdr.uvarint()
    ci = 0
    while ci < nconsts:
        ct = rdr.string()
        c_i: "long" = 0
        c_d: "double" = 0.0
        c_s: "char*" = ""
        if ct == "float":
            c_d = rdr.double()
        elif ct == "str":
            c_s = rdr.string()
        elif ct == "none":
            c_i = 0
        else:
            c_i = rdr.svarint()
        consts.append(Const(ct, c_i, c_d, c_s))
        ci = ci + 1

    names = []
    nnames = rdr.uvarint()
    ni = 0
    while ni < nnames:
        names.append(rdr.string())
        ni = ni + 1

    nglobals = rdr.uvarint()

    funcs = []
    nfuncs = rdr.uvarint()
    fi = 0
    while fi < nfuncs:
        fname = rdr.string()
        nparams = rdr.uvarint()
        nregs = rdr.uvarint()
        nlocals = rdr.uvarint()
        code = []
        ncode = rdr.uvarint()
        ii = 0
        while ii < ncode:
            packed = rdr.uvarint()
            op = packed >> 2
            fb = (packed >> 1) & 1
            fc = packed & 1
            ra = rdr.uvarint()
            b = rdr.svarint()
            c = rdr.svarint()
            code.append(Instr(op, fb, fc, ra, b, c))
            ii = ii + 1
        defaults = []
        ndefaults = rdr.uvarint()
        di = 0
        while di < ndefaults:
            defaults.append(rdr.svarint())
            di = di + 1
        vararg = rdr.svarint()
        params = []
        nparamnames = rdr.uvarint()
        pi = 0
        while pi < nparamnames:
            params.append(rdr.string())
            pi = pi + 1
        funcs.append(Func(fname, nparams, nregs, nlocals, code,
                          defaults, vararg, params))
        fi = fi + 1

    classes = []
    nclasses = rdr.uvarint()
    cli = 0
    while cli < nclasses:
        cname = rdr.string()
        base = rdr.svarint()
        methods = []
        nmethods = rdr.uvarint()
        mi = 0
        while mi < nmethods:
            mname = rdr.string()
            mfunc = rdr.svarint()
            methods.append(MethEnt(mname, mfunc))
            mi = mi + 1
        classes.append(ClassInfo(cname, base, methods))
        cli = cli + 1

    entry = rdr.uvarint()
    return Program(version, source, consts, names, nglobals,
                   funcs, classes, entry)


def _ends_with(s: "char*", suffix: "char*") -> "int":
    ls = len(s)
    lsuf = len(suffix)
    if lsuf > ls:
        return 0
    return 1 if s[ls - lsuf:ls] == suffix else 0


def main() -> "int":
    if len(sys.argv) < 2:
        print("usage: interp <bytecode.json|.mpyc>")
        return 1
    path = sys.argv[1]
    if _ends_with(path, ".mpyc") != 0:
        prog = load_mpyc(path)
        if prog.version < 0:
            return 1                    # load_mpyc rejected the file
    else:
        src = open(path).read()
        hook = rpy.json.generate_decoder(Program)
        prog = json.loads(src, object_hook=hook)
    sargs = []
    ai = 2
    while ai < len(sys.argv):
        sargs.append(sys.argv[ai])
        ai = ai + 1
    return interp_run(prog, sargs)
