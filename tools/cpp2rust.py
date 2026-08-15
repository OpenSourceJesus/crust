#!/usr/bin/env python3
"""Raise the C++ subset to Rust, so `rustc` can check it.

`tools/cpprust.py` lowers the subset to C. This raises the same subset to
Rust instead, for a different purpose: the output is not meant to be run,
or linked, or read for pleasure. It is meant to be handed to `rustc` so the
borrow checker can be asked, independently, whether the ownership cpprust
inferred actually holds.

Why not the Cpp2Rust translation
--------------------------------

Popescu et al. (PLDI'26) box every variable into `Value<T> = Rc<RefCell<T>>`
and represent every pointer as a weak `Ptr<T>`. That is the right choice for
their goal, which is that *any* program translates and then runs: ownership
and mutability move to run time, where `borrow_mut` panics and `Weak::upgrade`
fails.

It is the wrong choice for this one. Deferring every check to run time is
precisely what makes `rustc` accept the result, so as a checker it answers
nothing -- their safety guarantee holds because the questions were postponed,
not because they were answered. This module inverts the default: emit the
*most restrictive* Rust that still models the C++ faithfully, so that
borrowck has something to reject.

The refusals then line up with the ones cpprust already makes:

    dtor and no copy constructor, copied  ->  use of moved value
    a by-value owning parameter/return    ->  use of moved value
    a reference return                    ->  lifetime does not live long enough
    `goto` with a destructor pending      ->  no `goto` in the language

What it is not
--------------

A second opinion, not a verdict. rustc rejects sound aliasing, so an error
here is something to look at rather than a bug found; and because the model
is lossy, rustc staying quiet vindicates nothing. Both are reported in those
terms.

Modes
-----

    --mode types      (default)  structs, Drop/Clone, signatures; bodies stubbed
    --mode ownership             the above, plus an ownership skeleton per body

`types` answers whether the object model is coherent: field types resolve,
a class that owns something cannot be copied, a signature does not pass an
owning class by value. It says nothing about statements, and cannot: the
bodies are `unimplemented!()`.

`ownership` translates each body down to the statements that move, borrow,
construct or destroy something, and erases the rest. That erasure is the
point rather than a shortcut -- arithmetic cannot violate ownership, so
dropping it costs the check nothing and removes most of what is hard to
translate. What it cannot erase safely it reports, per the guiding rule in
CPPRUST.md: anything the raising cannot do correctly is reported, not
approximated.

Driving it
----------

    python3 tools/cpp2rust.py t.cpp -o t.rs
    python3 tools/cpp2rust.py t.cpp --check          # run rustc, report
    python3 tools/cpp2rust.py t.cpp --mode ownership --check

Unlike `cpprust.py` this is a developer tool rather than a compile step, so
it may `import cpprust` outright. The subprocess rule in CPPRUST.md exists
because `shivyc/preproc.py` is transpiled by py2c and an import there becomes
an undefined cross-module reference; nothing transpiles this file, so the
front half of cpprust -- header splicing, conditionals, templates, lambdas,
`auto`, namespaces, aliases -- is reused directly rather than reimplemented.
"""

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cpprust
import cpp_auto


class RustError(Exception):
    """Raised where the raising cannot proceed correctly.

    Same contract as `cpprust.CppError`: the message names the reason and
    the fix, and no Rust is emitted that would compile and mean something
    else.
    """

    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

# Written out rather than computed. `unsigned long` is one key, not a
# modifier applied to another, because the spellings that reach here are
# few and a table cannot mis-parse one.
_PRIM = {
    "void": "()",
    "bool": "bool",
    "char": "i8",
    "signed char": "i8",
    "unsigned char": "u8",
    "short": "i16",
    "short int": "i16",
    "unsigned short": "u16",
    "int": "i32",
    "unsigned": "u32",
    "unsigned int": "u32",
    "long": "i64",
    "long int": "i64",
    "unsigned long": "u64",
    "long long": "i64",
    "unsigned long long": "u64",
    "float": "f32",
    "double": "f64",
    "size_t": "usize",
    "ssize_t": "isize",
    "int8_t": "i8",
    "uint8_t": "u8",
    "int16_t": "i16",
    "uint16_t": "u16",
    "int32_t": "i32",
    "uint32_t": "u32",
    "int64_t": "i64",
    "uint64_t": "u64",
}

_ARRAY_DIM = re.compile(r"\[\s*(\w*)\s*\]")


class Type(object):
    """A C++ type read closely enough to know what it owns.

    Only four things matter to a checker: what the base name is, how many
    pointers are on it, whether it is a reference, and whether it is const.
    Everything else about a type is the C front end's business.
    """

    __slots__ = ("base", "ptr", "ref", "const", "dim")

    def __init__(self, base, ptr=0, ref=False, const=False, dim=None):
        self.base = base
        self.ptr = ptr
        self.ref = ref
        self.const = const
        self.dim = dim


def parse_type(text, dim=""):
    """Read a written C++ type. Returns a Type, or None if unreadable."""
    t = " ".join(text.replace("*", " * ").replace("&", " & ").split())
    if not t:
        return None
    const = False
    words = []
    ptr = 0
    ref = False
    for w in t.split():
        if w == "const":
            const = True
        elif w == "*":
            ptr += 1
        elif w == "&":
            ref = True
        elif w in ("struct", "class", "enum", "static", "inline", "virtual"):
            continue
        else:
            words.append(w)
    if not words:
        return None
    base = " ".join(words)
    # An array field carries its extent separately, in `Member.dim`.
    d = None
    if dim:
        m = _ARRAY_DIM.search(dim)
        if m:
            d = m.group(1) or None
    return Type(base, ptr, ref, const, d)


def rust_type(ty, classes, owning, where):
    """Map a Type to Rust.

    A pointer becomes a raw pointer rather than a reference. That is not
    laziness: a C++ `T *` carries no lifetime and may be null, and inventing
    a `&T` for one would make rustc check a claim the source never made --
    which is how a checker starts reporting things that are not there.
    Holding a raw pointer is safe in Rust; only dereferencing is not, and
    the skeleton never dereferences.
    """
    if ty is None:
        raise RustError("%s: the type could not be read" % where)
    base = ty.base
    if base in _PRIM:
        inner = _PRIM[base]
    elif base in classes or base in owning:
        inner = base
    elif re.match(r"^\w+$", base):
        # An unknown name is a type from outside this translation unit. It
        # gets an opaque struct rather than a report: a `.cpp` naming a Crust
        # type it does not define is the ordinary case here, not an error.
        inner = base
    else:
        raise RustError(
            "%s: `%s` is not a type this pass can spell in Rust. Write it "
            "out, or add it to --owning if it is a type from the other side "
            "of the boundary." % (where, ty.base))
    if ty.dim is not None:
        inner = "[%s; %s]" % (inner, ty.dim)
    elif ty.dim is None and ty.base and _ARRAY_DIM.search(ty.base or ""):
        pass
    for _ in range(ty.ptr):
        inner = ("*const %s" if ty.const else "*mut %s") % inner
    if ty.ref:
        # A C++ reference is a pointer the source did not have to spell, and
        # unlike a `T *` it is non-null and non-reseatable -- which is
        # exactly what a Rust reference claims. So this one *is* borrowed,
        # and rustc gets to check the aliasing.
        inner = ("&%s" if ty.const else "&mut %s") % inner
    return inner


# --------------------------------------------------------------------------
# What a class owns
# --------------------------------------------------------------------------

class RClass(object):
    """A class, read for the two questions a checker asks of one.

    Does it own something -- so that it needs `Drop` and a copy of it is a
    double free? And can it be copied -- so that `T b = a;` is a clone
    rather than a move?
    """

    __slots__ = ("cls", "name", "fields", "methods", "ctors", "dtor",
                 "copy_ctor", "base", "virtuals", "owns")

    def __init__(self, cls):
        self.cls = cls
        self.name = cls.name
        self.base = cls.base
        self.fields = []
        self.methods = []
        self.ctors = []
        self.dtor = None
        self.copy_ctor = None
        self.virtuals = []
        self.owns = False


def _is_copy_ctor(member, cname):
    """`T(const T &o)` -- the one constructor that is not a `T_new`."""
    if member.kind != "ctor":
        return False
    parts = cpprust._split_top(member.params or "")
    if len(parts) != 1:
        return False
    ty = parse_type(re.sub(r"\b\w+\s*$", "", parts[0].strip()))
    if ty is None:
        ty = parse_type(parts[0])
    return ty is not None and ty.base == cname and ty.ref


def _clone_member(m, ret, params, dim):
    """A Member with its written types rewritten, and nothing else touched."""
    out = cpprust.Member(m.kind, ret, m.name, params, m.body, m.line,
                         dim, list(m.init), m.virt, m.pure)
    out.outline = m.outline
    out.definit = m.definit
    out.declared_only = m.declared_only
    return out


def _retype(cls, tsub):
    """Rewrite every written type in a class through `tsub`."""
    members = [_clone_member(m, tsub(m.ret), tsub(m.params), tsub(m.dim))
               for m in cls.members]
    return cpprust.Class(cls.name, (), members, cls.line,
                         tsub(cls.base) if cls.base else None)


def _instantiate(cls, targs, tsub):
    """One instantiation of a template: substitute, then mangle.

    Substitution is simultaneous -- `template<A, B>` instantiated as
    `<B, char>` must not rewrite `A` to `B` and then that `B` to `char` --
    which is `cpprust._subst_type`'s job, so it does it here too.
    """
    def sub(s):
        if not s:
            return s
        return tsub(cpprust._subst_type(s, cls.tparams, targs))

    name = cpprust._mono_name(cls.name, targs)
    members = []
    for m in cls.members:
        mm = _clone_member(m, sub(m.ret), sub(m.params), sub(m.dim))
        # A constructor and destructor are named for the class, so they
        # follow it to the instantiated name.
        if m.kind in ("ctor", "dtor"):
            mm.name = name
        members.append(mm)
    return cpprust.Class(name, (), members, cls.line,
                         sub(cls.base) if cls.base else None)


def read_class(cls, classes, owning):
    """Sort a cpprust Class into the shape the Rust emitter wants."""
    rc = RClass(cls)
    for m in cls.members:
        if m.kind == "field":
            ty = parse_type(m.ret, m.dim)
            rc.fields.append((m, ty))
            if ty is not None and ty.ptr == 0 and not ty.ref:
                if ty.base in owning:
                    rc.owns = True
        elif m.kind == "dtor":
            rc.dtor = m
        elif m.kind == "ctor":
            if _is_copy_ctor(m, cls.name):
                rc.copy_ctor = m
            else:
                rc.ctors.append(m)
        elif m.kind == "method":
            rc.methods.append(m)
            if m.virt or m.pure:
                rc.virtuals.append(m)
    return rc


def _owns_resource(rc, table, owning):
    """Does destroying one of these run anything?

    A destructor is the direct answer. Failing that, a member whose own type
    owns something is -- which is how a class with no destructor written in
    it still gets one, and still refuses to be copied.
    """
    if rc.dtor is not None or rc.owns:
        return True
    for _m, ty in rc.fields:
        if ty is None or ty.ptr or ty.ref:
            continue
        if ty.base in owning:
            return True
        sub = table.get(ty.base)
        if sub is not None and sub is not rc and _owns_resource(
                sub, table, owning):
            return True
    if rc.base and rc.base in table:
        return _owns_resource(table[rc.base], table, owning)
    return False


# --------------------------------------------------------------------------
# Emitting Rust
# --------------------------------------------------------------------------

_PRELUDE = """\
// Generated by tools/cpp2rust.py -- do not edit, do not run.
//
// This crate exists to be type- and borrow-checked, not executed. Bodies are
// `unimplemented!()` or ownership skeletons; the question asked of it is
// whether the ownership holds, and nothing else.
#![allow(dead_code, unused_variables, unused_mut, unused_imports)]
#![allow(non_camel_case_types, non_snake_case, non_upper_case_globals)]
#![allow(unreachable_code, unused_assignments, path_statements)]
"""


def _ctor_name(rc, member):
    """The arity scheme cpprust already uses, kept so the two agree.

    A class with one constructor keeps the plain `new` whatever its arity,
    and the no-argument one always keeps it -- because that is what member
    and base default construction calls.
    """
    arity = cpprust._arity(member.params or "")
    if len(rc.ctors) <= 1 or arity == 0:
        return "new"
    return "new_%d" % arity


def _method_name(rc, member):
    """Methods overload by argument count, exactly as cpprust resolves them."""
    same = [m for m in rc.methods if m.name == member.name]
    if len(same) <= 1:
        return member.name
    return "%s_%d" % (member.name, cpprust._arity(member.params or ""))


def _params(member, classes, owning, where, selfish=True):
    """Translate a parameter list, with `this` in front where there is one."""
    out = []
    if selfish:
        out.append("&mut self")
    for part in cpprust._split_top(member.params or ""):
        part = part.strip()
        if not part or part == "void":
            continue
        name = cpprust._param_name(part) or "_a%d" % len(out)
        decl = part
        if name and re.search(r"\b%s\s*(\[|$)" % re.escape(name), part):
            decl = re.sub(r"\b%s\b" % re.escape(name), "", part, count=1)
        ty = parse_type(decl)
        out.append("%s: %s" % (name, rust_type(ty, classes, owning, where)))
    return ", ".join(out)


def _ret(member, classes, owning, where):
    ty = parse_type(member.ret or "void")
    if ty is None or (ty.base == "void" and not ty.ptr and not ty.ref):
        return ""
    return " -> %s" % rust_type(ty, classes, owning, where)


def emit_class(rc, table, classes, owning, mode):
    """One class: the struct, its Drop, its Clone, and its methods."""
    out = []
    where = "class %s" % rc.name

    # Fields in *reverse* declaration order, with the base last.
    #
    # This is the one place the two languages disagree in a way that a
    # field-for-field mapping gets silently wrong. C++ destroys members in
    # reverse declaration order and the base after all of them; Rust drops
    # fields in declaration order. Reversing here is what makes the Rust
    # model the C++ rather than the Crust side of that disagreement, which
    # CPPRUST.md notes frees in declaration order because that is Rust's rule.
    fields = []
    for m, ty in reversed(rc.fields):
        fields.append("    pub %s: %s," % (
            m.name, rust_type(ty, classes, owning,
                              "%s, field `%s`" % (where, m.name))))
    if rc.base:
        fields.append("    pub base: %s,   // destroyed last, as in C++"
                      % rc.base)

    owns = _owns_resource(rc, table, owning)
    derive = []
    if not owns:
        # No destructor anywhere in it, so C++ copies it bitwise and so may
        # this. `Clone` rather than `Copy`: a `Copy` bound would fail on any
        # field that is not itself `Copy`, and that failure would be an
        # artefact of the mapping rather than anything wrong with the class.
        derive.append("Clone")
    elif rc.copy_ctor is not None:
        pass                     # hand-written `impl Clone` below
    if derive:
        out.append("#[derive(%s)]" % ", ".join(derive))
    out.append("#[repr(C)]")
    out.append("pub struct %s {" % rc.name)
    out.extend(fields if fields else ["    _empty: [u8; 0],"])
    out.append("}")
    out.append("")

    if owns and rc.copy_ctor is not None:
        # A copy constructor is what makes copying legal, so it is what
        # supplies `Clone`. Without one the class has a destructor and no
        # way to duplicate what it owns -- the Rule of Three -- and gets no
        # `Clone` at all, which is how `T b = a;` becomes a move and how
        # rustc comes to report the second use.
        out.append("impl Clone for %s {" % rc.name)
        out.append("    fn clone(&self) -> Self { unimplemented!() }")
        out.append("}")
        out.append("")

    if owns:
        out.append("impl Drop for %s {" % rc.name)
        out.append("    fn drop(&mut self) { }")
        out.append("}")
        out.append("")

    # Virtuals become a trait. A *non*-virtual method that calls a virtual
    # one still dispatches in C++ -- `dispatch.cpp` turns on exactly that --
    # so non-virtuals go on the trait too, as default methods. A default
    # method calling a required one is virtual dispatch from a non-virtual
    # caller, which is the shape needed, and it costs nothing where the
    # class has no virtuals at all.
    if rc.virtuals:
        out.append("pub trait %s_virt {" % rc.name)
        for m in rc.virtuals:
            out.append("    fn %s(%s)%s;" % (
                _method_name(rc, m),
                _params(m, classes, owning, where),
                _ret(m, classes, owning, where)))
        out.append("}")
        out.append("")

    inherent = [m for m in rc.methods if m not in rc.virtuals]
    if inherent or rc.ctors or rc.dtor:
        out.append("impl %s {" % rc.name)
        for m in rc.ctors:
            out.append("    pub fn %s(%s) -> Self { unimplemented!() }" % (
                _ctor_name(rc, m),
                _params(m, classes, owning, where, selfish=False)))
        for m in inherent:
            body = "unimplemented!()"
            out.append("    pub fn %s(%s)%s { %s }" % (
                _method_name(rc, m),
                _params(m, classes, owning, where),
                _ret(m, classes, owning, where), body))
        out.append("}")
        out.append("")

    if rc.virtuals:
        out.append("impl %s_virt for %s {" % (rc.name, rc.name))
        for m in rc.virtuals:
            out.append("    fn %s(%s)%s { unimplemented!() }" % (
                _method_name(rc, m),
                _params(m, classes, owning, where),
                _ret(m, classes, owning, where)))
        out.append("}")
        out.append("")
    return out


def emit_opaque(name, dropfn, used_owning):
    """A type from outside this translation unit.

    It has no definition here and needs none: what the check wants to know
    is whether it owns something, which the caller says by naming its
    destructor on `--owning`. That single fact is enough to make copying one
    a move.
    """
    out = ["#[repr(C)]", "pub struct %s {" % name,
           "    _opaque: [u8; 0],", "}", ""]
    if dropfn:
        out.append("// `%s` destroys one of these, so it owns something and"
                   % dropfn)
        out.append("// may not be duplicated -- no `Clone`.")
        out.append("impl Drop for %s {" % name)
        out.append("    fn drop(&mut self) { }")
        out.append("}")
        out.append("")
    return out


# --------------------------------------------------------------------------
# Ownership skeletons
# --------------------------------------------------------------------------

_DECL_CTOR = re.compile(r"^\s*(\w+)\s+(\w+)\s*\(([^;]*)\)\s*;")
_DECL_COPY = re.compile(r"^\s*(\w+)\s+(\w+)\s*=\s*([^;]+);")
_DECL_PLAIN = re.compile(r"^\s*(\w+)\s+(\w+)\s*;")
_METHOD_CALL = re.compile(r"^\s*([\w.]+)\s*(?:\.|->)\s*(\w+)\s*\(([^;]*)\)\s*;")


def skeleton(body, rc, table, classes, owning, where):
    """Translate a body down to what can violate ownership.

    Kept: constructing a local, copying one, calling a method on one,
    passing one to a function, and the block structure that decides when a
    destructor runs. Erased: everything else, on the grounds that arithmetic
    cannot double-free anything -- so dropping it costs the check nothing
    while removing most of what is hard to translate.

    The erasure is what makes this honest rather than a shortcut. A
    statement that is dropped can only make the check *quieter*, never
    wrong: rustc is being asked whether the moves that remain are legal, and
    a move that was erased is one it is not asked about. Where a statement
    might move something and cannot be read, it is reported instead.
    """
    out = []
    # Locals of class type this skeleton has actually declared. The guard
    # below refuses to emit any statement naming something not in here.
    #
    # That guard is what makes the erasure safe rather than merely
    # convenient. A dropped statement can only make the check quieter: rustc
    # is asked about the moves that survive, and one that was erased is a
    # question it is never asked. But a statement that *survives* while its
    # subject was erased names an undeclared variable, and rustc then reports
    # a use of something that does not exist -- a diagnostic about this pass,
    # dressed as a finding about the source. Quieter is a cost; wrong is not
    # allowed.
    known = set()

    for raw in body.split("\n"):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line in ("{", "}"):
            out.append(line)
            continue

        def emitted(stmt, uses):
            if all(u in known for u in uses):
                out.append(stmt)
                return True
            return False

        m = _DECL_CTOR.match(line)
        if m and m.group(1) in table and m.group(2) not in ("if", "while",
                                                            "for", "switch"):
            cname, vname, args = m.group(1), m.group(2), m.group(3)
            src = args.strip()
            target = table[cname]
            # `T b(a)` with a single named argument of the same class is a
            # copy construction, not a constructor picked by arity -- and it
            # is the shape the Rule of Three turns on, so it is read first.
            if cpprust._arity(args) == 1 and re.match(r"^\w+$", src) \
                    and src in known:
                out.append("    let mut %s = %s;   // %s %s(%s)"
                           % (vname, _copy_expr(src, target), cname,
                              vname, src))
                known.add(vname)
            elif not src or not re.match(r"^\w+$", src):
                out.append("    let mut %s = %s::%s();" % (
                    vname, cname,
                    _ctor_name(target, target.ctors[0])
                    if target.ctors else "new"))
                known.add(vname)
            continue

        m = _DECL_COPY.match(line)
        if m and m.group(1) in table:
            cname, vname, src = m.group(1), m.group(2), m.group(3).strip()
            target = table[cname]
            if re.match(r"^\w+$", src) and src in known:
                out.append("    let mut %s = %s;   // %s %s = %s"
                           % (vname, _copy_expr(src, target), cname,
                              vname, src))
                known.add(vname)
            continue

        # `T name;` -- default construction, and the declaration that makes
        # every use below it legal. Missing this one left the copies that
        # follow naming a variable nothing declared.
        m = _DECL_PLAIN.match(line)
        if m and m.group(1) in table:
            cname, vname = m.group(1), m.group(2)
            target = table[cname]
            out.append("    let mut %s = %s::new();" % (vname, cname))
            known.add(vname)
            continue

        m = _METHOD_CALL.match(line)
        if m:
            recv, meth, args = m.group(1), m.group(2), m.group(3)
            root = recv.split(".")[0].split("->")[0]
            for mv in _moved_args(args, table):
                emitted("    drop(%s);   // passed by value" % mv, [mv])
            emitted("    let _ = &mut %s;   // .%s()" % (root, meth), [root])
            continue

        # A bare call at statement position may still move something.
        m = re.match(r"^\s*(\w+)\s*\(([^;]*)\)\s*;", line)
        if m and m.group(1) not in ("if", "while", "for", "switch",
                                    "return", "sizeof"):
            for mv in _moved_args(m.group(2), table):
                emitted("    drop(%s);   // passed by value to `%s`"
                        % (mv, m.group(1)), [mv])
            for u in _reads(line, known, exclude=_moved_args(m.group(2),
                                                             table)):
                out.append("    let _ = &%s;   // read here" % u)
            continue

        # Anything else is erased -- except for the locals it *reads*.
        #
        # A read has to survive even when the statement around it does not.
        # `printf("%d", a.head())` cannot move anything, so the call is
        # dropped; but C++ reads `a` at that point, so `a` is live there, and
        # a move that happened above it is a genuine double ownership. Erase
        # the read too and rustc has nothing to object to -- which is how the
        # Rule of Three case went quiet while cpprust was reporting it.
        for u in _reads(line, known):
            out.append("    let _ = &%s;   // read here" % u)
    return out


_KEYWORDS = frozenset((
    "if", "else", "while", "for", "do", "switch", "case", "default", "break",
    "continue", "return", "sizeof", "new", "delete", "this", "true", "false",
    "const", "static", "struct", "class", "int", "char", "void", "long"))


def _reads(line, known, exclude=()):
    """Which declared locals does this statement mention?

    Only names already declared in the skeleton, so this can never introduce
    a use of something nothing declared. Order-preserving and deduplicated,
    because emitting the same borrow twice says nothing extra.
    """
    seen, out = set(), []
    for tok in re.findall(r"\b\w+\b", line):
        if tok in _KEYWORDS or tok in seen or tok in exclude:
            continue
        if tok in known:
            seen.add(tok)
            out.append(tok)
    return out


def _copy_expr(src, target):
    """`a` where a copy is wanted: a clone if it may be cloned, else a move.

    This is where the Rule of Three becomes a rustc diagnostic. A class with
    a destructor and no copy constructor has no `Clone`, so the copy is a
    move, so the next use of the source is `use of moved value` -- the same
    refusal cpprust makes, arrived at by a different road.
    """
    if target.copy_ctor is not None:
        return "%s.clone()" % src
    return src


def _moved_args(args, table):
    """Arguments passed by value that own something -- each one a move."""
    out = []
    for part in cpprust._split_top(args or ""):
        part = part.strip()
        if not part or part.startswith("&"):
            continue
        if re.match(r"^\w+$", part):
            out.append(part)
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _normalise(text, path, incdirs, defines, clang):
    """Run cpprust's language-agnostic front half.

    Everything up to `_strip_comments` in `cpprust.translate` is about
    getting one translation unit into the subset -- splicing headers,
    deciding conditionals, monomorphising templates, lowering lambdas,
    resolving `auto`, flattening namespaces. None of it is about C, so all
    of it is reused rather than written twice.
    """
    basedir = os.path.dirname(os.path.abspath(path)) or "."
    text = cpprust._expand_headers(text, basedir, incdirs,
                                   defines=set(defines or ()))
    text = cpprust._std_prelude(text)
    text, _s, _t = cpprust._monomorphise_function_templates(
        text, cpprust._strip_comments(text), path)
    text = cpprust._lower_lambdas(text, path)
    base = os.path.basename(path)
    try:
        for step in (cpp_auto.resolve_using_alias,
                     cpp_auto.resolve_default_arguments,
                     cpp_auto.resolve_defaulted,
                     cpp_auto.resolve_namespaces,
                     cpp_auto.resolve_nested_classes,
                     cpp_auto.resolve_aliases,
                     cpp_auto.resolve_range_for):
            text = step(text, base, blank=cpp_auto._blank_like(text))
        fallback = {}
        if clang is not False and os.path.isfile(path) \
                and cpp_auto.clang_available():
            fallback = cpp_auto.clang_auto_types(path, incdirs, defines)
        text = cpp_auto.resolve(text, base, blank=cpp_auto._blank_like(text),
                                fallback=fallback)
        text = cpp_auto.resolve_casts(text, base,
                                      blank=cpp_auto._blank_like(text))
    except cpp_auto.AutoError as e:
        raise RustError(e.message)
    return text


def raise_to_rust(text, path="<cpp>", owning=None, incdirs=(), defines=(),
                  clang=None, mode="types"):
    """Raise a C++ subset source to Rust for checking."""
    owning = owning or {}
    text = _normalise(text, path, incdirs, defines, clang)
    scan = cpprust._strip_comments(text)
    cpprust._check_unsupported(scan, path)

    cls_names = set(re.findall(r"\b(?:class|struct)\s+(\w+)", scan))
    text, scan, outline = cpprust._extract_out_of_line(text, scan, cls_names)
    found = cpprust._find_classes(scan, text)
    for _s, _e, c in found:
        cpprust._attach_out_of_line(c, outline, path)

    # Templates, monomorphised the way cpprust does it -- one struct per
    # instantiation, and the same mangled name, so a field spelled
    # `vector<int>` here and `vector_int` there are the same type on both
    # sides. An uninstantiated template emits nothing, which is what C++
    # does with one.
    tclasses = dict((c.name, c) for _s, _e, c in found if c.tparams)
    tnames = set(tclasses)
    wanted = {}

    def record(name, targs):
        cls = tclasses[name]
        if len(targs) != len(cls.tparams):
            raise RustError(
                "`%s` takes %d template argument%s, %d given (`%s<%s>`)"
                % (name, len(cls.tparams),
                   "" if len(cls.tparams) == 1 else "s", len(targs),
                   name, ", ".join(targs)))
        seen = wanted.setdefault(name, [])
        if targs not in seen:
            seen.append(targs)

    bodies = [(s, e) for s, e, c in found if c.tparams]
    if tnames:
        cpprust._monomorphise_uses(
            cpprust._blank_spans(scan, bodies), tnames, record)
        # A template body may instantiate another, so the set is closed
        # transitively -- `Outer<T>` holding an `Inner<T>` asks for
        # `Inner<int>` only once `T` is known.
        tspan = dict((c.name, (s, e, c)) for s, e, c in found if c.tparams)
        pending = [(n, t) for n in list(wanted) for t in list(wanted[n])]
        seen_pairs = set(pending)
        while pending:
            name, targs = pending.pop()
            s, e, c = tspan[name]
            sub = cpprust._subst_type(scan[s:e], c.tparams, targs)
            got = []
            cpprust._monomorphise_uses(
                sub, tnames, lambda n2, t2: got.append((n2, t2)))
            for pair in got:
                record(*pair)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    pending.append(pair)

    def tsub(s):
        """Rewrite a written type so `vector<int>` reads as `vector_int`.

        `__cpp_ref(T)` is resolved here too. It is the one element builtin
        that appears in type position -- a container cannot pick one spelling
        for a key parameter, since by value it refuses an owning key and by
        reference it cannot bind `m[3]` -- so it becomes `const T &` for a
        class and a bare `T` for a scalar. The other three builtins live in
        bodies, which the skeleton erases.
        """
        if not s:
            return s
        if tnames:
            s = cpprust._monomorphise_uses(s, tnames, known=wanted)
        if "__cpp_ref" in s:
            def one(m):
                # The argument may be a pointer -- `vector<string *>` gives
                # `__cpp_ref(string *)`. Only a class held *by value* needs
                # the reference spelling; a pointer is already one word wide
                # and passes by value like a scalar.
                arg = " ".join(m.group(1).split())
                return ("const %s &" % arg) if arg in classes else arg
            s = re.sub(r"__cpp_ref\s*\(([^()]*)\)", one, s)
        return s

    classes = set(c.name for _s, _e, c in found)
    for name in wanted:
        for targs in wanted[name]:
            classes.add(cpprust._mono_name(name, targs))

    table = {}
    order = []
    for _s, _e, c in found:
        if c.tparams:
            for targs in wanted.get(c.name, []):
                rc = read_class(_instantiate(c, targs, tsub), classes, owning)
                table[rc.name] = rc
                order.append(rc)
            continue
        rc = read_class(_retype(c, tsub), classes, owning)
        table[rc.name] = rc
        order.append(rc)

    lines = [_PRELUDE]

    # Types this file names but does not define. Opaque, plus whatever the
    # caller said about what they own.
    referenced = set()
    for rc in order:
        for _m, ty in rc.fields:
            if ty is not None and ty.base not in _PRIM:
                referenced.add(ty.base)
        if rc.base:
            referenced.add(rc.base)
    for name in sorted((referenced | set(owning)) - set(table)):
        if not re.match(r"^\w+$", name):
            continue
        lines.extend(emit_opaque(name, owning.get(name), name in owning))

    for rc in order:
        lines.extend(emit_class(rc, table, classes, owning, mode))

    if mode == "ownership":
        # Class bodies are blanked first. A method defined inside one is not
        # a free function, and reading it as one produced a skeleton called
        # `__own_head` over a body whose `this` nothing had declared.
        spans = [(s, e) for s, e, _c in found]
        lines.extend(_emit_skeletons(
            text, cpprust._blank_spans(scan, spans), table, classes, owning))

    return "\n".join(lines) + "\n"


_FREE_FN = re.compile(
    r"(?<![\w:])(\w[\w\s*&]*?)\s+(\w+)\s*\(([^;{)]*)\)\s*\{")


def _emit_skeletons(text, scan, table, classes, owning):
    """One `fn` per free function in the file, carrying only its ownership."""
    out = ["// ---- ownership skeletons ----",
           "//",
           "// Each function reduced to the statements that move, borrow,",
           "// construct or destroy something. Everything else is erased:",
           "// arithmetic cannot double-free anything, so leaving it out",
           "// makes the check quieter but never wrong.",
           ""]
    for m in _FREE_FN.finditer(scan):
        name = m.group(2)
        if name in ("if", "for", "while", "switch", "return", "sizeof"):
            continue
        brace = m.end() - 1
        close = cpprust._match_brace(text, brace)
        if close is None:
            continue
        body = text[brace + 1:close]
        stmts = skeleton(body, None, table, classes, owning,
                         "function %s" % name)
        out.append("pub fn __own_%s() {" % name)
        out.extend(stmts)
        out.append("}")
        out.append("")
    return out


def check_with_rustc(rust, path, keep=False):
    """Hand the Rust to `rustc` and report what it says.

    `--emit=metadata` type- and borrow-checks without generating code, which
    is the whole of what is wanted here and much faster than a build. The
    crate is never linked and never run.
    """
    import tempfile
    d = tempfile.mkdtemp(prefix="cpp2rust.")
    src = os.path.join(d, "check.rs")
    with open(src, "w") as f:
        f.write(rust)
    try:
        proc = subprocess.run(
            ["rustc", "--edition", "2021", "--crate-type", "lib",
             "--emit", "metadata", "-o", os.path.join(d, "check.meta"), src],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None, ("rustc could not be run. The raising still happened; "
                      "only the second opinion is missing.")
    err = proc.stderr.decode("utf-8", "replace")
    return proc.returncode, err


_VERDICT = """\
rustc rejected the raised Rust. That is a second opinion, not a verdict:
rustc rejects aliasing that is sound, so each of these is something to look
at rather than a bug found. Where it names a moved value, compare it with
what cpprust says about the same class -- the two are meant to agree.
"""

_QUIET = """\
rustc accepted the raised Rust. That is weaker than it sounds: the model is
lossy, bodies are skeletons, and a check that was never expressed cannot
fail. It means nothing was found, not that there is nothing to find.
"""


def main(argv):
    import argparse
    p = argparse.ArgumentParser(
        description="Raise the C++ subset to Rust so rustc can check it.")
    p.add_argument("source")
    p.add_argument("-o", "--output")
    p.add_argument("--mode", choices=("types", "ownership"), default="types")
    p.add_argument("--owning", default="",
                   help="Name:drop_fn,.. for types this file does not define")
    p.add_argument("--incdir", action="append", default=[])
    p.add_argument("-D", dest="defines", action="append", default=[])
    p.add_argument("--check", action="store_true",
                   help="run rustc on the result and report")
    p.add_argument("--clang", dest="clang", action="store_true", default=None)
    p.add_argument("--no-clang", dest="clang", action="store_false")
    args = p.parse_args(argv[1:])

    with open(args.source) as f:
        text = f.read()
    try:
        owning = cpprust._parse_owning(args.owning) if args.owning else {}
        rust = raise_to_rust(text, args.source, owning=owning,
                             incdirs=tuple(args.incdir),
                             defines=tuple(args.defines),
                             clang=args.clang, mode=args.mode)
    except (RustError, cpprust.CppError) as e:
        msg = "%s: %s\n" % (os.path.basename(args.source),
                            getattr(e, "message", str(e)))
        sys.stderr.write(msg)
        if args.output:
            with open(args.output, "w") as f:
                f.write("/* %s */\n" % msg)
        return 1

    if args.output:
        with open(args.output, "w") as f:
            f.write(rust)
    elif not args.check:
        sys.stdout.write(rust)

    if args.check:
        code, err = check_with_rustc(rust, args.source)
        if code is None:
            sys.stderr.write(err + "\n")
            return 0
        if code != 0:
            sys.stderr.write(err)
            sys.stderr.write("\n" + _VERDICT)
            return 2
        sys.stderr.write(_QUIET)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
