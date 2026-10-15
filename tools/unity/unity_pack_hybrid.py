# SPDX-License-Identifier: MIT
"""unity_pack --hybrid: a script method the packer cannot lower to C runs as managed code on DotNetAnywhere.

Without --hybrid a method with C# left in it after lowering becomes an empty stub and a `CS8000` warning: the method does nothing at run time.
With it, such a method keeps its own C# source, compiled (mcs) against a small managed UnityEngine (tools/unity/unity_pack_managed/UnityShim.cs)
and run by DotNetAnywhere, which is linked into the player.  The packed engine is not changed:

    native  engine.c    Spark_Tally(i)  ->  ccs_b_Spark_Tally(i)   (hybrid_glue.c: DNA_Find / DNA_Call)   ->  managed Spark.__Tally(i)
    managed Spark.hp  ->  ccs_x_Spark_get_hp(i)   (engine.c: a wrapper of the accessor the lowered code uses)   ->  the packed array

An object is its index.  The managed class is a handle to it: its fields are PROPERTIES that read and write the packed arrays through the
accessors engine.c already has (`Spark_get_hp(i)`), so state is held once, natively, and a managed method and a lowered one see the same.

What this does not do, by design: change what a pack that worked did.  A class whose managed code does not compile (a Unity member the shim
lacks, a field the packed engine stores in a form managed code cannot reach) keeps its stubs and its CS8000 warnings, with the reason added.
A field is never copied into the managed object: that would be a second copy that silently disagrees.

Pieces written beside engine.c:  hybrid_generated.cs  hybrid.managed.dll  hybrid_glue.c  hybrid.ffi.json
"""
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SHIM = os.path.join(HERE, "unity_pack_managed", "UnityShim.cs")
ASSEMBLY = "hybrid.managed.dll"

# C# type -> (managed type, FFI manifest type, DotNetAnywhere value letter, C type in the glue, constructor of the value)
SCALAR = {
    "int": ("int", "int", "i", "int", "DNA_Int"), "uint": ("uint", "uint", "i", "unsigned", "DNA_Int"),
    "short": ("short", "short", "i", "short", "DNA_Int"), "ushort": ("ushort", "ushort", "i", "unsigned short", "DNA_Int"),
    "sbyte": ("sbyte", "sbyte", "i", "signed char", "DNA_Int"), "byte": ("byte", "byte", "i", "unsigned char", "DNA_Int"),
    "bool": ("bool", "int", "i", "int", "DNA_Int"),
    "long": ("long", "long", "l", "long long", "DNA_Long"),
    "float": ("float", "float", "f", "float", "DNA_Float"), "double": ("double", "double", "d", "double", "DNA_Double"),
}
# A Vector2 crosses the boundary as its two floats; a Vector2 result comes back through a two-float slot (ccs_ret2, in hybrid_glue.c) that the
# callee sets and the caller reads, since neither the FFI nor DNA_Call returns a struct.
VEC2 = "Vector2"


def _carrier(t):
    """may a value of C# type *t* cross the boundary?"""
    return t in SCALAR or t == VEC2


def _ctype(t):
    return VEC2 if t == VEC2 else SCALAR[t][3]


def _mtype(t):
    return VEC2 if t == VEC2 else SCALAR[t][0]


def _ffi_args(params):
    out = []
    for t, _n in params:
        out += ["float", "float"] if t == VEC2 else [SCALAR[t][1]]
    return out


def _dna_letters(params):
    return "".join("ff" if t == VEC2 else SCALAR[t][2] for t, _n in params)


# what an engine accessor's C type is, in the manifest
ACCESSOR_FFI = {"unsigned": "uint", "int": "int", "float": "float", "double": "double"}
# the fields managed code can have as properties: C# type -> (getter cast, setter cast)
FIELD_TYPES = {"int", "uint", "short", "ushort", "sbyte", "byte", "bool", "float"}


# the names of the managed Transform's rotation API: a class whose managed code uses one needs the engine's rotation storage (ClassGen._rotation_storage)
ROTATION_WORDS = frozenset(("rotation", "localRotation", "eulerAngles", "localEulerAngles", "Rotate", "LookAt", "TransformDirection", "InverseTransformDirection"))


class HybridError(Exception):
    pass


# ---- the toolchain ---------------------------------------------------------------------------------------------------------------------

def dna_home():
    h = os.environ.get("DNA_HOME") or os.path.join(os.path.dirname(REPO), "DotNetAnywhere")
    return h if os.path.exists(os.path.join(h, "build.py")) else None


def build_dir():
    return os.environ.get("UNITY_PACK_DNA_BUILD") or os.path.join(REPO, "build", "dna")


def ccs_dll():
    """CC#'s compiler (CCSharp, built: `python3 build.py compiler`), which compiles the managed side with Roslyn; None when it or `dotnet` is absent.
    $CCS_DLL names it, $CCS_HOME the checkout; otherwise a CCSharp checkout beside this repository."""
    if shutil.which("dotnet") is None:
        return None
    cands = []
    if os.environ.get("CCS_DLL"):
        cands.append(os.environ["CCS_DLL"])
    home = os.environ.get("CCS_HOME") or os.path.join(os.path.dirname(REPO), "CCSharp")
    cands.append(os.path.join(home, "build", "compiler", "ccs.dll"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def compiler_name():
    """'roslyn' (CC#'s compiler) or 'mcs' (mono): what compiles the managed C#.  Roslyn when it is there; $UNITY_PACK_MANAGED_COMPILER forces one."""
    want = os.environ.get("UNITY_PACK_MANAGED_COMPILER", "").strip().lower()
    if want == "mcs":
        return "mcs" if shutil.which("mcs") else None
    if want == "roslyn":
        return "roslyn" if ccs_dll() else None
    if ccs_dll():
        return "roslyn"
    return "mcs" if shutil.which("mcs") else None


def available():
    """(True, '') or (False, why): DotNetAnywhere beside this repository (or $DNA_HOME) and a compiler for the managed C#
    (CC#'s Roslyn one beside this repository, or mono's mcs)."""
    if compiler_name() is None:
        return False, "no managed C# compiler: build CCSharp beside this repository (python3 build.py compiler; needs dotnet), or install mono-mcs"
    if dna_home() is None:
        return False, "DotNetAnywhere not found: clone it beside this repository, or set DNA_HOME"
    return True, ""


def _dna_run(args):
    home, bdir = dna_home(), build_dir()
    os.makedirs(bdir, exist_ok=True)
    p = subprocess.run([sys.executable, os.path.join(home, "build.py")] + list(args) + ["--build-dir", bdir],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise HybridError("building DotNetAnywhere failed:\n" + p.stdout.decode("utf-8", "replace")[-800:])


def prepare_corlib():
    """DotNetAnywhere's corlib.dll (the managed code is compiled against it and runs on it), built once."""
    corlib = os.path.join(build_dir(), "corlib.dll")
    if not os.path.exists(corlib):
        _dna_run(["--lib-only"])
    if not os.path.exists(corlib):
        raise HybridError("DotNetAnywhere's corlib.dll was not built (it needs mcs)")
    return corlib


def mcs(sources, out, corlib):
    p = subprocess.run(["mcs", "-nostdlib", "-unsafe", "-target:" + ("exe" if out.endswith(".exe") else "library"), "-nowarn:0169,0414,0219,0649",
                        "-r:" + corlib, "-out:" + out] + list(sources),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode == 0, p.stdout.decode("utf-8", "replace")


def roslyn(sources, out, corlib):
    """The same, with CC#'s compiler: Roslyn against DotNetAnywhere's corlib (`ccs --managed-compile`), so current C# (what mcs, which stops at
    C# 7, cannot parse) compiles."""
    p = subprocess.run(["dotnet", ccs_dll(), "--managed-compile", out, corlib] + list(sources), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode == 0, p.stdout.decode("utf-8", "replace")


def compile_managed(sources, out, corlib):
    """(ok, messages): compile the managed C# *sources* to *out* (a console program when it ends in .exe, else a library) against *corlib*
    with whichever compiler compiler_name() picks."""
    return (roslyn if compiler_name() == "roslyn" else mcs)(sources, out, corlib)


# ---- what the packer tells us about a method -------------------------------------------------------------------------------------------

def signature_ok(params, ret):
    """May a method with these parameters (cs2cpp.Param) and C# return type be called across the boundary?  Numbers and bools only, for now."""
    for prm in params:
        if not _carrier(prm.type) or getattr(prm, "modifier", None):
            return False
    r = (ret or "void").strip()
    return _carrier(r) or r == "void"


def accessors(engine, cname):
    """{field: (get C type, set C type or None)} for the scalar accessors engine.c has for *cname*: `static unsigned Spark_get_hp(unsigned i)`."""
    out = {}
    for m in re.finditer(r"^static (unsigned|int|float|double) %s_get_(\w+)\(unsigned i\)" % re.escape(cname), engine, re.M):
        out[m.group(2)] = [m.group(1), None]
    for m in re.finditer(r"^static void %s_set_(\w+)\(unsigned i, (unsigned|int|float|double) v\)" % re.escape(cname), engine, re.M):
        if m.group(1) in out:
            out[m.group(1)][1] = m.group(2)
    return {k: tuple(v) for k, v in out.items()}


def _words(text):
    return set(re.findall(r"[A-Za-z_]\w*", text))


# ---- the managed class of a script ----------------------------------------------------------------------------------------------------

def _const_literal(f):
    """the C# literal of a constant field the packer parsed (`const float TIME = 1;`), or None"""
    v, t = f.get("default"), f.get("ty")
    if t not in SCALAR or v is None or isinstance(v, (str, list, dict, tuple)):
        return None
    if t == "bool":
        return "true" if v else "false"
    if t in ("float", "double"):
        return "%s%s" % (repr(float(v)), "f" if t == "float" else "")
    if isinstance(v, float) and not float(v).is_integer():
        return None
    return str(int(v)) + ("L" if t == "long" else "")


class ClassGen(object):
    """The managed C# of one script class, and what it needs from the engine."""

    def __init__(self, plan, engine, cl, cands, siblings, used_extra=frozenset()):
        self.name = cl["name"]
        self.used_extra = set(used_extra)       # words other code uses (a peer class: what the classes that name it use of it)
        self.refs = set()                       # script classes this class's code names (their handles are managed classes too)
        self.class_names = list(plan["classes"]) if not isinstance(plan["classes"], list) else [c["name"] for c in plan["classes"]]
        self.cl = cl
        self.cands = cands
        self.siblings = [s for s in siblings if not s.get("stub")]
        self.acc = accessors(engine, self.name)
        self.engine = engine
        self.natives = {}          # export name -> (ret FFI type, [arg FFI types], C text of the wrapper, managed extern declaration)
        self.time = time_members(engine)          # the Time members the managed Time class gets (all the engine declares)
        self.problem = None
        self.helpers = []
        self.words = set()
        self.find_names = set()
        self.uses_go = False
        self.xf_fields = []
        self.uses_rb2d = False
        self.uses_spr = False
        self.go_components = []
        self.src = self._build()

    # a managed property for each field the engine has accessors for
    def _field(self, f):
        t = f["ty"]
        if t == VEC2:
            return self._vec2_field(f)
        if t == "Transform":
            return self._transform_field(f)
        if t == "Rigidbody2D":
            return self._rigidbody_field(f)
        if t == "SpriteRenderer":
            return self._spriterenderer_field(f)
        a = self.acc.get(f["name"])
        handle =t in self.class_names and a is not None and a[0] == "int" and a[1] == "int" and f["name"] in self._used
        if (t not in FIELD_TYPES and not handle) or a is None or a[1] is None:
            return None
        gt, st = a
        n = f["name"]
        get_ex, set_ex = "ccs_x_%s_get_%s" % (self.name, n), "ccs_x_%s_set_%s" % (self.name, n)
        self.natives[get_ex] = (ACCESSOR_FFI[gt], ["uint"], "%s ccs_x_%s_get_%s(unsigned i) { return %s_get_%s(i); }" % (gt, self.name, n, self.name, n))
        self.natives[set_ex] = ("void", ["uint", ACCESSOR_FFI[st]], "void ccs_x_%s_set_%s(unsigned i, %s v) { %s_set_%s(i, v); }" % (self.name, n, st, self.name, n))
        if handle:
            # a reference to another script object: its index in its class's arrays (-1: null), as a managed handle with identity
            if t != self.name:
                self.refs.add(t)
            return ("    public %s %s {\n        get { int h = HybridNative.%s(__i); return h < 0 ? null : %s.__Get((uint)h); }\n"
                    "        set { HybridNative.%s(__i, value == null ? -1 : (int)value.__i); }\n    }" % (t, n, get_ex, t, set_ex))
        if t == "float":
            return "    public float %s { get { return HybridNative.%s(__i); } set { HybridNative.%s(__i, value); } }" % (n, get_ex, set_ex)
        if t == "bool":
            return "    public bool %s { get { return HybridNative.%s(__i) != 0; } set { HybridNative.%s(__i, value ? %s : %s); } }" % (
                n, get_ex, set_ex, "1" if ACCESSOR_FFI[st] != "uint" else "1u", "0" if ACCESSOR_FFI[st] != "uint" else "0u")
        mt = SCALAR[t][0]
        sc = "(%s)" % ("uint" if ACCESSOR_FFI[st] == "uint" else "int" if ACCESSOR_FFI[st] == "int" else "float")
        return "    public %s %s { get { return (%s)HybridNative.%s(__i); } set { HybridNative.%s(__i, %svalue); } }" % (mt, n, mt, get_ex, set_ex, sc)

    def _vec2_field(self, f):
        """an embedded Vector2 field: the engine keeps it as two floats with accessors `Class_get_f_x/_y` and `Class_set_f_x/_y`"""
        n = f["name"]
        ax, ay = self.acc.get(n + "_x"), self.acc.get(n + "_y")
        if not all(a and a[0] == "float" and a[1] == "float" for a in (ax, ay)):
            return None
        for c in "xy":
            self.natives["ccs_x_%s_get_%s_%s" % (self.name, n, c)] = ("float", ["uint"], "float ccs_x_%s_get_%s_%s(unsigned i) { return %s_get_%s_%s(i); }" % (self.name, n, c, self.name, n, c))
            self.natives["ccs_x_%s_set_%s_%s" % (self.name, n, c)] = ("void", ["uint", "float"], "void ccs_x_%s_set_%s_%s(unsigned i, float v) { %s_set_%s_%s(i, v); }" % (self.name, n, c, self.name, n, c))
        g = lambda c: "HybridNative.ccs_x_%s_get_%s_%s(__i)" % (self.name, n, c)
        s = lambda c: "HybridNative.ccs_x_%s_set_%s_%s(__i, value.%s);" % (self.name, n, c, c)
        return "    public %s %s { get { return new %s(%s, %s); } set { %s %s } }" % (VEC2, n, VEC2, g("x"), g("y"), s("x"), s("y"))

    def _rigidbody_field(self, f):
        """a `Rigidbody2D` field: an int handle into the engine's Box2D-backed arrays (-1 for none), as a managed Rigidbody2D (_rigidbody2d_class)"""
        n, c = f["name"], self.name
        a = self.acc.get(n)
        if not (a and a == ("int", "int")) or not re.search(r"^static void Rigidbody2D_set_velocity\(int rb,", self.engine, re.M):
            return None
        self.uses_rb2d = True
        get_ex, set_ex = "ccs_x_%s_get_%s" % (c, n), "ccs_x_%s_set_%s" % (c, n)
        self.natives[get_ex] = ("int", ["uint"], "int %s(unsigned i) { return %s_get_%s(i); }" % (get_ex, c, n))
        self.natives[set_ex] = ("void", ["uint", "int"], "void %s(unsigned i, int v) { %s_set_%s(i, v); }" % (set_ex, c, n))
        return ("    public UnityEngine.Rigidbody2D %s {\n        get { return UnityEngine.Rigidbody2D.__Wrap(HybridNative.%s(__i)); }\n"
                "        set { HybridNative.%s(__i, value == null ? -1 : value.__rb); }\n    }" % (n, get_ex, set_ex))

    def _spriterenderer_field(self, f):
        """a `SpriteRenderer` field: the GameObject index the engine keeps for it (-1 for none), as a managed SpriteRenderer (_spriterenderer_class)
        with the one member the engine stores per renderer, `color`.  Anything else asked of it (sprite, flipX, sortingOrder, ..) is not a member
        of the managed class, so that code does not build and keeps its lowered C."""
        n, c = f["name"], self.name
        a = self.acc.get(n)
        if not (a and a == ("int", "int")) or not re.search(r"^static void SpriteRenderer_set_color\(int go,", self.engine, re.M):
            return None
        self.uses_spr = True
        get_ex = "ccs_x_%s_get_%s" % (c, n)
        self.natives[get_ex] = ("int", ["uint"], "int %s(unsigned i) { return %s_get_%s(i); }" % (get_ex, c, n))
        return ("    public UnityEngine.SpriteRenderer %s {\n        get { return UnityEngine.SpriteRenderer.__Wrap(HybridNative.%s(__i)); }\n    }" % (n, get_ex))

    def _transform_field(self, f):
        """a `Transform` field the packer resolved to an object (`_Class_f_target_class[i]` / `_target_inst[i]`: -1 for none): a read-only
        property giving a Transform of that object, whose position is the world one (`_engine_world_pos` / `_engine_set_world`, as the lowered
        code uses).  Only position: _build declines code that asks such a transform for anything else."""
        n, e, c = f["name"], self.engine, self.name
        if n not in self._used or not re.search(r"^extern const int _%s_%s_target_class\[" % (re.escape(c), re.escape(n)), e, re.M):
            return None
        if not re.search(r"^static void _engine_world_pos\(int class_id, unsigned inst,[^;{]*\) \{", e, re.M):
            return None
        self.xf_fields.append(n)
        self.natives["ccs_x_%s_%s_tc" % (c, n)] = ("int", ["uint"], "int ccs_x_%s_%s_tc(unsigned i) { return _%s_%s_target_class[i]; }" % (c, n, c, n))
        self.natives["ccs_x_%s_%s_ti" % (c, n)] = ("int", ["uint"], "int ccs_x_%s_%s_ti(unsigned i) { return (int)_%s_%s_target_inst[i]; }" % (c, n, c, n))
        return ("    public UnityEngine.Transform %s { get { int c = HybridNative.ccs_x_%s_%s_tc(__i); "
                "return c < 0 ? null : new __TargetTransform(c, HybridNative.ccs_x_%s_%s_ti(__i)); } }" % (n, c, n, c, n))

    def _rotation_storage(self):
        """Does the engine keep a rotation for this class (`_Class_rot_x/y/z/w[i]` and the basis `_Class_rot_m00..m11[i]`), and the helper that sets
        the quaternion and the basis together?  Then managed code can read and write transform.rotation through them."""
        e, n = self.engine, re.escape(self.name)
        return (all(re.search(r"^extern float _%s_rot_%s\[" % (n, a), e, re.M) for a in ("x", "y", "z", "w", "m00", "m01", "m10", "m11"))
                and re.search(r"^static void _engine_transform_set_quat\(", e, re.M) is not None)

    def _hierarchy_functions(self):
        """the engine has what `transform.parent` / `SetParent(null)` need for this class (it emits them only for code that uses them)"""
        e, n = self.engine, re.escape(self.name)
        return (re.search(r"^static int Transform_get_parent\(int go\) \{", e, re.M) is not None
                and re.search(r"^static void Transform_SetParent\(int child, int parent,", e, re.M) is not None
                and re.search(r"^static int _engine_go_of_%s\(" % n, e, re.M) is not None)

    def _transform(self):
        """position through the SoA position accessors, when the class has them.  A 2D class has no pos_z: z reads 0 and a write of it is dropped.
        rotation through the engine's rotation arrays, when the class has them (_rotation_storage)."""
        def has(n):
            return n in self.acc and self.acc[n][0] == "float" and self.acc[n][1] == "float"
        if not (has("pos_x") and has("pos_y")):
            return None
        axes = ("pos_x", "pos_y") + (("pos_z",) if has("pos_z") else ())
        for n in axes:
            self.natives["ccs_x_%s_get_%s" % (self.name, n)] = ("float", ["uint"], "float ccs_x_%s_get_%s(unsigned i) { return %s_get_%s(i); }" % (self.name, n, self.name, n))
            self.natives["ccs_x_%s_set_%s" % (self.name, n)] = ("void", ["uint", "float"], "void ccs_x_%s_set_%s(unsigned i, float v) { %s_set_%s(i, v); }" % (self.name, n, self.name, n))
        g = lambda n: "HybridNative.ccs_x_%s_get_%s(i)" % (self.name, n)
        s = lambda n, v: "HybridNative.ccs_x_%s_set_%s(i, %s);" % (self.name, n, v)
        z_get = g("pos_z") if "pos_z" in axes else "0f"
        z_set = s("pos_z", "value.z") if "pos_z" in axes else ""
        if self._rotation_storage():
            for a in "xyzw":
                self.natives["ccs_x_%s_get_rot_%s" % (self.name, a)] = (
                    "float", ["uint"], "float ccs_x_%s_get_rot_%s(unsigned i) { return _%s_rot_%s[i]; }" % (self.name, a, self.name, a))
            self.natives["ccs_x_%s_set_rot" % self.name] = (
                "void", ["uint", "float", "float", "float", "float"],
                "void ccs_x_%s_set_rot(unsigned i, float x, float y, float z, float w) { _engine_transform_set_quat("
                "&_%s_rot_x[i], &_%s_rot_y[i], &_%s_rot_z[i], &_%s_rot_w[i], &_%s_rot_m00[i], &_%s_rot_m01[i], &_%s_rot_m10[i], &_%s_rot_m11[i], x, y, z, w); }"
                % ((self.name,) + (self.name,) * 8))
            rot = ("    public override UnityEngine.Quaternion rotation {\n        get { return new UnityEngine.Quaternion(%s, %s, %s, %s); }\n"
                   "        set { HybridNative.ccs_x_%s_set_rot(i, value.x, value.y, value.z, value.w); }\n    }\n"
                   % (g("rot_x"), g("rot_y"), g("rot_z"), g("rot_w"), self.name))
        else:
            # (_build declines a class whose managed code names rotation, so this is only reached by code that does not)
            rot = ("    public override UnityEngine.Quaternion rotation {\n"
                   "        get { throw new System.NotSupportedException(\"the packed engine keeps no rotation for %s\"); }\n"
                   "        set { throw new System.NotSupportedException(\"the packed engine keeps no rotation for %s\"); }\n    }\n" % (self.name, self.name))
        local = ("    public override UnityEngine.Vector3 localPosition {\n        get { return new UnityEngine.Vector3(%s, %s, %s); }\n"
                 "        set { %s %s %s }\n    }\n") % (g("pos_x"), g("pos_y"), z_get, s("pos_x", "value.x"), s("pos_y", "value.y"), z_set)
        n, e = self.name, self.engine
        if re.search(r"^static float _engine_gx\(int c, unsigned i\) \{", e, re.M):
            # a hierarchy: position is the world one, composed through the parents by the engine's own helpers (as in the lowered code)
            cid = sorted(self.class_names).index(n)
            for a in "xyz":
                self.natives["ccs_x_%s_get_wpos_%s" % (n, a)] = ("float", ["uint"], "float ccs_x_%s_get_wpos_%s(unsigned i) { return _engine_g%s(%d, i); }" % (n, a, a, cid))
            self.natives["ccs_x_%s_set_wpos" % n] = ("void", ["uint", "float", "float", "float"],
                                                     "void ccs_x_%s_set_wpos(unsigned i, float x, float y, float z) { _engine_set_world(%d, i, x, y, z); }" % (n, cid))
            pos = ("    public override UnityEngine.Vector3 position {\n        get { return new UnityEngine.Vector3(%s); }\n"
                   "        set { HybridNative.ccs_x_%s_set_wpos(i, value.x, value.y, value.z); }\n    }\n"
                   % (", ".join("HybridNative.ccs_x_%s_get_wpos_%s(i)" % (n, a) for a in "xyz"), n))
        else:
            pos = local.replace("localPosition", "position")
        parent = ""
        if self._hierarchy_functions():
            self.natives["ccs_x_%s_has_parent" % n] = ("int", ["uint"], "int ccs_x_%s_has_parent(unsigned i) { return Transform_get_parent(_engine_go_of_%s(i)) != -1; }" % (n, n))
            self.natives["ccs_x_%s_set_parent_null" % n] = ("void", ["uint", "int"], "void ccs_x_%s_set_parent_null(unsigned i, int keep) { Transform_SetParent(_engine_go_of_%s(i), -1, keep); }" % (n, n))
            parent = ("    static readonly UnityEngine.TransformRef __parent = new UnityEngine.TransformRef();\n"
                      "    public override UnityEngine.TransformRef parent { get { return HybridNative.ccs_x_%s_has_parent(i) != 0 ? __parent : null; } }\n"
                      "    public override void SetParent(UnityEngine.TransformRef p, bool worldPositionStays)\n    {\n"
                      "        // (null is the one parent a script can name: any other TransformRef is this object's own, which leaves it where it is)\n"
                      "        if (p == null) HybridNative.ccs_x_%s_set_parent_null(i, worldPositionStays ? 1 : 0);\n    }\n" % (n, n))
        return ("public class %s_Transform : UnityEngine.Transform {\n    readonly uint i;\n    public %s_Transform(uint i) { this.i = i; }\n%s%s%s%s}\n"
                % (n, n, pos, local, parent, rot))

    def _ret2(self):
        """the natives of the two-float result slot a Vector2 result travels through"""
        self.natives["ccs_x_ret2_set"] = ("void", ["float", "float"], "void ccs_x_ret2_set(float x, float y) { ccs_ret2[0] = x; ccs_ret2[1] = y; }")
        self.natives["ccs_x_ret2_x"] = ("float", [], "float ccs_x_ret2_x(void) { return ccs_ret2[0]; }")
        self.natives["ccs_x_ret2_y"] = ("float", [], "float ccs_x_ret2_y(void) { return ccs_ret2[1]; }")

    def _build(self):
        cl, name = self.cl, self.name
        bodies = "\n".join(c["body"] for c in self.cands)
        used = _words(bodies) | self.used_extra
        self.words = set(_words(bodies))
        self._used = used
        self.refs = (used & set(self.class_names)) - {name}
        props, unreachable = [], []
        for f in cl.get("fields") or []:
            if f.get("const"):
                # a constant is its value, in the managed class too (when the packer parsed it: a number or a bool)
                lit = _const_literal(f)
                if lit is not None:
                    props.append("    public const %s %s = %s;" % (SCALAR[f["ty"]][0], f["name"], lit))
                elif f["name"] in used:
                    unreachable.append("const %s %s" % (f["ty"], f["name"]))
                continue
            if f.get("static"):
                continue
            line = self._field(f)
            if line is not None:
                props.append(line)
            elif f["name"] in used:
                unreachable.append("%s %s" % (f["ty"], f["name"]))
        if unreachable:
            self.problem = ("the method uses %s, which the packed engine stores in a form managed code cannot reach yet"
                            % ", ".join("`%s`" % u for u in unreachable))
            return ""
        if self.xf_fields and (ROTATION_WORDS | {"localPosition", "parent", "SetParent", "localScale", "lossyScale", "childCount"}) & used:
            self.problem = ("the method uses a Transform field (%s) and asks a transform for more than its position, which the managed shim "
                            "cannot give for another object yet" % ", ".join(self.xf_fields))
            return ""
        if self.xf_fields and not _has_set_world(self.engine) and re.search(r"\.\s*position\s*[-+*/]?=(?!=)", bodies):
            self.problem = "the method moves a Transform field's object, and the packed engine has no world-position setter for it"
            return ""
        transform = self._transform()
        if transform is None and "transform" in used:
            # (it would build, and then fail at run time on a null transform: say so now, and keep the lowered C)
            self.problem = "the method uses `transform`, and the packed engine keeps no position accessors for this class"
            return ""
        components = []
        for m in re.finditer(r"\bGetComponent\s*<\s*(\w+)\s*>\s*\(\s*\)", bodies):
            c = m.group(1)
            if c in components:
                continue
            if c not in self.class_names or not re.search(r"^static int GameObject_GetComponent_%s\(int go\)" % re.escape(c), self.engine, re.M) \
                    or not re.search(r"^static int _engine_go_of_%s\(" % re.escape(name), self.engine, re.M):
                self.problem = "the method calls GetComponent<%s>(), which the packed engine cannot find from this class" % c
                return ""
            components.append(c)
        for c in components:
            if c != name:
                self.refs.add(c)
            ex = "ccs_x_%s_getc_%s" % (name, c)
            self.natives[ex] = ("int", ["uint"], "int %s(unsigned i) { return GameObject_GetComponent_%s(_engine_go_of_%s(i)); }" % (ex, c, name))
        e = self.engine
        has_go_of = re.search(r"^static int _engine_go_of_%s\(" % re.escape(name), e, re.M) is not None
        if re.search(r"(?<![.\w])GetComponent\s*<", bodies) and not has_go_of:
            self.problem = "the method calls GetComponent<T>(), and the packed engine keeps no object index for this class"
            return ""
        own_words = _words(bodies)
        if "gameObject" in own_words and not has_go_of:
            self.problem = "the method uses `gameObject`, and the packed engine keeps no object index for this class"
            return ""
        has_go_prop = "gameObject" in used and has_go_of
        if has_go_prop:
            self.natives["ccs_x_%s_go" % name] = ("int", ["uint"], "int ccs_x_%s_go(unsigned i) { return _engine_go_of_%s(i); }" % (name, name))
        finds = re.findall(r"\bGameObject\s*\.\s*Find\s*\(", bodies)
        literals = re.findall(r"\bGameObject\s*\.\s*Find\s*\(\s*\"([A-Za-z0-9_ ./-]*)\"\s*\)", bodies)
        if finds and (len(finds) != len(literals) or not re.search(r"^static int GameObject_Find\(const char \*name\)", e, re.M)):
            self.problem = "the method calls GameObject.Find with a name that is not a plain string literal (or the packed engine has no Find)"
            return ""
        self.find_names = set(literals)
        self.uses_go = bool(finds) or has_go_prop or "GameObject" in own_words
        self.go_components = [c for c in dict.fromkeys(re.findall(r"\bGetComponent\s*<\s*(\w+)\s*>\s*\(\s*\)", bodies))]
        if {"parent", "SetParent"} & used and not self._hierarchy_functions():
            self.problem = "the method uses the transform's parent, which the packed engine keeps no functions for in this class"
            return ""
        if ROTATION_WORDS & used or re.search(r"\btransform\s*\.\s*(?:right|up|forward)\b", bodies):
            if not self._rotation_storage():
                # (it would build, and then fail at run time with no rotation to read: say so now, and keep the lowered C)
                self.problem = "the method uses the transform's rotation, which the packed engine keeps no storage for in this class"
                return ""
        lines = ["public class %s : UnityEngine.MonoBehaviour\n{" % name]
        lines.append("    static %s[] __objs = new %s[8];" % (name, name))
        # (an int index: DotNetAnywhere's array opcodes take a 32-bit index, and `a[uint]` is a conv.u it does not narrow)
        lines.append("    public static %s __Get(uint i)\n    {\n        int n = (int)i;\n        if (n >= __objs.Length) {\n"
                     "            %s[] bigger = new %s[n * 2 + 8];\n            for (int k = 0; k < __objs.Length; k++) bigger[k] = __objs[k];\n            __objs = bigger;\n        }\n"
                     "        %s o = __objs[n];\n        if (o == null) {\n            o = new %s();\n            o.__i = i;\n%s            __objs[n] = o;\n        }\n        return o;\n    }"
                     % (name, name, name, name, name, ("            o.transform = new %s_Transform(i);\n" % name) if transform else ""))
        lines += props
        if has_go_prop:
            lines.append("    public GameObject gameObject { get { return GameObject.__Wrap(HybridNative.ccs_x_%s_go(__i)); } }" % name)
        if components:
            arms = "".join("        if (typeName == \"%s\") { int h = HybridNative.ccs_x_%s_getc_%s(__i); return h < 0 ? null : %s.__Get((uint)h); }\n" % (c, name, c, c)
                           for c in components)
            lines.append("    public override object __component(string typeName)\n    {\n%s        return null;\n    }" % arms)
        # the lowered methods of the class, for the managed ones to call (only those a managed body names)
        managed = {c["sym"] for c in self.cands}
        for s in self.siblings:
            if s["sym"] in managed or s["name"] not in used or not s["scalar"]:
                continue
            ex = "ccs_x_%s" % s["sym"]
            st = bool(s.get("static"))
            ps = s["params"]
            cps, pre, cargs = [], [], []
            for k, (t_, _n) in enumerate(ps):
                if t_ == VEC2:
                    cps.append("float a%dx, float a%dy" % (k, k))
                    pre.append("%s a%d = {a%dx, a%dy};" % (VEC2, k, k, k))
                else:
                    cps.append("%s a%d" % (SCALAR[t_][3], k))
                cargs.append("a%d" % k)
            ffi = ([] if st else ["uint"]) + _ffi_args(ps)
            vret = s["ret"] == VEC2
            rc = "void" if vret else (SCALAR[s["ret"]][3] if s["ret"] in SCALAR else "void")
            call = "%s(%s)" % (s["sym"], ", ".join(([] if st else ["i"]) + cargs))
            head = "%s %s(%s)" % (rc, ex, ", ".join(([] if st else ["unsigned i"]) + cps) or "void")
            if vret:
                stmt = "%s r = %s; ccs_ret2[0] = r.x; ccs_ret2[1] = r.y;" % (VEC2, call)
                self._ret2()
            else:
                stmt = "%s%s;" % ("" if rc == "void" else "return ", call)
            self.natives[ex] = ("void" if rc == "void" else SCALAR[s["ret"]][1], ffi, "%s { %s%s }" % (head, " ".join(pre) + " " if pre else "", stmt))
            mparams = ", ".join("%s %s" % (_mtype(t_), n) for t_, n in ps)
            margs = ", ".join(([] if st else ["__i"]) + [("%s.x, %s.y" % (n, n)) if t_ == VEC2 else n for t_, n in ps])
            mret = VEC2 if vret else (SCALAR[s["ret"]][0] if s["ret"] in SCALAR else "void")
            if vret:
                body = ("HybridNative.%s(%s); return new %s(HybridNative.ccs_x_ret2_x(), HybridNative.ccs_x_ret2_y());" % (ex, margs, VEC2))
            else:
                body = "%sHybridNative.%s(%s);" % ("" if mret == "void" else "return ", ex, margs)
            lines.append("    public %s%s %s(%s) { %s }" % ("static " if st else "", mret, s["name"], mparams, body))
        for c in self.cands:
            st = bool(c.get("static"))
            mparams = ", ".join("%s %s" % (_mtype(t_), n) for t_, n in c["params"])
            ret = (c["ret"] or "void").strip()
            mret = _mtype(ret) if _carrier(ret) else "void"
            lines.append("    public %s%s %s(%s)\n    {\n%s\n    }" % ("static " if st else "", mret, c["name"], mparams, c["body"]))
            # the entry native code calls: named by the C symbol, which tells overloads apart (a Vector2 comes in as its two floats, goes out in ccs_ret2)
            eps = ([] if st else ["uint i"])
            for t_, n in c["params"]:
                eps += ["float %s_x, float %s_y" % (n, n)] if t_ == VEC2 else ["%s %s" % (SCALAR[t_][0], n)]
            eargs = ", ".join(("new %s(%s_x, %s_y)" % (VEC2, n, n)) if t_ == VEC2 else n for t_, n in c["params"])
            target = "%s(%s)" % (c["name"], eargs) if st else "__Get(i).%s(%s)" % (c["name"], eargs)
            if ret == VEC2:
                self._ret2()
                lines.append("    public static void __%s(%s) { %s r = %s; HybridNative.ccs_x_ret2_set(r.x, r.y); }" % (c["sym"], ", ".join(eps), VEC2, target))
            else:
                lines.append("    public static %s __%s(%s) { %s%s; }" % (mret, c["sym"], ", ".join(eps), "" if mret == "void" else "return ", target))
        lines.append("}\n")
        return (transform or "") + "\n".join(lines)


def _native_decls(natives):
    out = ["public static class HybridNative\n{"]
    for ex in sorted(natives):
        ret, args, _c = natives[ex]
        mret = {"uint": "uint", "int": "int", "float": "float", "double": "double", "long": "long", "short": "short", "ushort": "ushort",
                "sbyte": "sbyte", "byte": "byte", "void": "void"}[ret]
        params = ", ".join("%s a%d" % (a, k) for k, a in enumerate(args))
        out.append("    [System.Runtime.InteropServices.DllImport(\"ccs_native\")] public static extern %s %s(%s);" % (mret, ex, params))
    out.append("}\n")
    return "\n".join(out)


# UnityEngine.Time members that are floats the engine keeps as `extern float Time_<name>;` (each only when the engine has it)
TIME_FIELDS = ("deltaTime", "unscaledDeltaTime", "timeScale", "time", "fixedDeltaTime")


def time_members(engine):
    """The Time members (TIME_FIELDS) this engine.c declares, for the managed Time class to read."""
    return [n for n in TIME_FIELDS if re.search(r"^extern float Time_%s;" % n, engine, re.M)]


def _time_class(natives, names):
    props = []
    for n in names:
        natives["ccs_x_Time_%s" % n] = ("float", [], "float ccs_x_Time_%s(void) { return Time_%s; }" % (n, n))
        props.append("        public static float %s { get { return HybridNative.ccs_x_Time_%s(); } }" % (n, n))
    return "namespace UnityEngine\n{\n    public static class Time\n    {\n" + "\n".join(props) + "\n    }\n}\n"


def _input_class(engine, natives):
    """UnityEngine.Input over the engine's own input state, for what the engine has: GetAxis of its axes, GetButton / Down / Up of its buttons
    and GetKey(name).  A name it does not know reads 0 / false, as in the lowered code.  "" when the engine has no Input at all."""
    members = []
    keycode = ""
    if "static float Input_GetAxis(const char *name)" in engine:
        arms = []
        for n in re.findall(r"^extern float engine_input_axis_(\w+);", engine, re.M):
            ex = "ccs_x_input_axis_%s" % n
            natives[ex] = ("float", [], "float %s(void) { return Input_GetAxis(\"%s\"); }" % (ex, n))
            arms.append("            if (name == \"%s\") return HybridNative.%s();" % (n, ex))
        members.append("        public static float GetAxis(string name)\n        {\n%s\n            return 0f;\n        }" % "\n".join(arms))
    names = re.search(r"_btn_name\[\d+\] = \{([^}]*)\}", engine)
    if names and "static int Input_GetButtonDown(const char *name)" in engine:
        natives["ccs_x_input_button"] = ("int", ["int", "int"], (
            "int ccs_x_input_button(int mode, int k) { const char *n = _btn_name[k]; "
            "return mode == 0 ? Input_GetButton(n) : mode == 1 ? Input_GetButtonDown(n) : Input_GetButtonUp(n); }"))
        arms = ["            if (name == %s) return HybridNative.ccs_x_input_button(mode, %d);" % (s.strip(), k)
                for k, s in enumerate(names.group(1).split(","))]
        members.append("        static int Button(int mode, string name)\n        {\n%s\n            return 0;\n        }" % "\n".join(arms))
        for m, mode in (("GetButton", 0), ("GetButtonDown", 1), ("GetButtonUp", 2)):
            members.append("        public static bool %s(string name) { return Button(%d, name) != 0; }" % (m, mode))
    if "static int Input_GetKey(const char *name)" in engine:
        natives["ccs_x_input_key"] = ("int", ["int"], "int ccs_x_input_key(int c) { char s[2]; s[0] = (char)c; s[1] = 0; return Input_GetKey(s); }")
        members.append("        public static bool GetKey(string name)\n        {\n            if (name == null || name.Length == 0) return false;\n"
                       "            return HybridNative.ccs_x_input_key((int)name[0]) != 0;\n        }")
    if re.search(r"^extern unsigned char engine_input_key\[256\];", engine, re.M):
        # held / pressed this frame / released this frame, for a key the host sets in engine_input_key[] (a letter, a digit or space, by its
        # lower-case character): the engine keeps no previous state for keys, so a latch run at the top of every tick (apply puts it there) does
        natives["ccs_x_input_edge"] = ("void", [], (
            "static unsigned char ccs_key_prev[256], ccs_key_cur[256];\n"
            "static void ccs_keys_latch(void) { int k; for (k = 0; k < 256; k++) { ccs_key_prev[k] = ccs_key_cur[k]; ccs_key_cur[k] = engine_input_key[k] != 0; } }\n"
            "void ccs_x_input_edge(void) { }"))
        natives["ccs_x_input_key_state"] = ("int", ["int", "int"], (
            "int ccs_x_input_key_state(int c, int mode) { if (c < 0 || c > 255) return 0; return mode == 0 ? ccs_key_cur[c] : mode == 1 ? "
            "(ccs_key_cur[c] && !ccs_key_prev[c]) : (!ccs_key_cur[c] && ccs_key_prev[c]); }"))
        members.append("        static int Code(string name)\n        {\n            if (name == null || name.Length == 0) return -1;\n"
                       "            int c = (int)name[0];\n            if (c >= 65 && c <= 90) c += 32;\n            return c;\n        }")
        members.append("        public static bool GetKey(KeyCode key) { return HybridNative.ccs_x_input_key_state((int)key, 0) != 0; }")
        members.append("        public static bool GetKeyDown(KeyCode key) { return HybridNative.ccs_x_input_key_state((int)key, 1) != 0; }")
        members.append("        public static bool GetKeyUp(KeyCode key) { return HybridNative.ccs_x_input_key_state((int)key, 2) != 0; }")
        members.append("        public static bool GetKeyDown(string name) { return HybridNative.ccs_x_input_key_state(Code(name), 1) != 0; }")
        members.append("        public static bool GetKeyUp(string name) { return HybridNative.ccs_x_input_key_state(Code(name), 2) != 0; }")
        enum = ", ".join(["Space = 32"] + ["Alpha%d = %d" % (d, 48 + d) for d in range(10)] + ["%s = %d" % (chr(65 + k), 97 + k) for k in range(26)])
        keycode = "    public enum KeyCode { %s }\n" % enum
        if not any(m.startswith("        public static bool GetKey(string") for m in members):
            members.append("        public static bool GetKey(string name) { return HybridNative.ccs_x_input_key_state(Code(name), 0) != 0; }")
    if not members:
        return ""
    return "namespace UnityEngine\n{\n" + keycode + "    public static class Input\n    {\n" + "\n".join(members) + "\n    }\n}\n"


def _scene_class(engine, natives):
    """UnityEngine.Camera.main, RenderSettings.ambientLight and Physics.gravity / Physics2D.gravity over the globals the engine declares for them (`extern float Camera_main_pos_x;`,
    `RenderSettings_ambient_r`): what the lowered code reads (orthographicSize, transform.position, the clip planes, ambientLight) and, since they
    are plain globals, the writes too.  The camera's transform is its own small type (position only), so code that wants more fails to build and
    stays lowered.  "" when the engine declares neither."""
    cam = set(re.findall(r"^extern float Camera_main_(\w+);", engine, re.M))
    amb = [a for a in "rgb" if re.search(r"^extern float RenderSettings_ambient_%s;" % a, engine, re.M)]

    def prop(cls_var, ctype, name, cname, setter=True):
        g = "ccs_x_scene_get_%s" % cname
        natives[g] = ("float", [], "float %s(void) { return %s; }" % (g, cname))
        out = "        public %s %s { get { return HybridNative.%s(); }" % (ctype, name, g)
        if setter:
            s = "ccs_x_scene_set_%s" % cname
            natives[s] = ("void", ["float"], "void %s(float v) { %s = v; }" % (s, cname))
            out += " set { HybridNative.%s(value); }" % s
        return out + " }"
    out = []
    if cam:
        members = ["        static CameraTransform __t = new CameraTransform();", "        static Camera __main = new Camera();",
                   "        public static Camera main { get { return __main; } }"]
        if {"pos_x", "pos_y", "pos_z"} <= cam:
            members.append("        public CameraTransform transform { get { return __t; } }")
            tpos = []
            for a in "xyz":
                g, s = "ccs_x_scene_get_Camera_main_pos_%s" % a, "ccs_x_scene_set_Camera_main_pos_%s" % a
                natives[g] = ("float", [], "float %s(void) { return Camera_main_pos_%s; }" % (g, a))
                natives[s] = ("void", ["float"], "void %s(float v) { Camera_main_pos_%s = v; }" % (s, a))
                tpos.append((a, g, s))
            out.append("    public class CameraTransform\n    {\n        public Vector3 position\n        {\n"
                       "            get { return new Vector3(%s); }\n            set { %s }\n        }\n    }\n" % (
                           ", ".join("HybridNative.%s()" % g for _a, g, _s in tpos),
                           " ".join("HybridNative.%s(value.%s);" % (s, a) for a, _g, s in tpos)))
        else:
            out.append("    public class CameraTransform { }\n")
        for n in ("orthographicSize", "aspect"):
            if n in cam:
                members.append(prop("", "float", n, "Camera_main_" + n))
        for n in ("nearClipPlane", "farClipPlane"):
            if n in cam:
                members.append(prop("", "float", n, "Camera_main_" + n, False))
        out.append("    public class Camera\n    {\n" + "\n".join(members) + "\n    }\n")
    scr = [n for n in ("width", "height") if re.search(r"^extern int Screen_%s;" % n, engine, re.M)]
    if scr:
        members = []
        for n in scr:
            natives["ccs_x_scene_get_Screen_" + n] = ("int", [], "int ccs_x_scene_get_Screen_%s(void) { return Screen_%s; }" % (n, n))
            members.append("        public static int %s { get { return HybridNative.ccs_x_scene_get_Screen_%s(); } }" % (n, n))
        out.append("    public static class Screen\n    {\n" + "\n".join(members) + "\n    }\n")
    if amb == list("rgb"):
        gets = ", ".join("HybridNative.ccs_x_scene_get_RenderSettings_ambient_%s()" % a for a in "rgb")
        sets = " ".join("HybridNative.ccs_x_scene_set_RenderSettings_ambient_%s(value.%s);" % (a, a) for a in "rgb")
        for a in "rgb":
            natives["ccs_x_scene_get_RenderSettings_ambient_" + a] = (
                "float", [], "float ccs_x_scene_get_RenderSettings_ambient_%s(void) { return RenderSettings_ambient_%s; }" % (a, a))
            natives["ccs_x_scene_set_RenderSettings_ambient_" + a] = (
                "void", ["float"], "void ccs_x_scene_set_RenderSettings_ambient_%s(float v) { RenderSettings_ambient_%s = v; }" % (a, a))
        out.append("    public static class RenderSettings\n    {\n        public static Color ambientLight\n        {\n"
                   "            get { return new Color(%s, 1f); }\n            set { %s }\n        }\n    }\n" % (gets, sets))
    # Physics2D.gravity / Physics.gravity: the globals the engine's own physics step reads, so a write changes the simulation as in Unity
    for cls, vec, axes in (("Physics2D", "Vector2", "xy"), ("Physics", "Vector3", "xyz")):
        if not all(re.search(r"^extern float %s_gravity_%s;" % (cls, a), engine, re.M) for a in axes):
            continue
        for a in axes:
            natives["ccs_x_scene_get_%s_gravity_%s" % (cls, a)] = (
                "float", [], "float ccs_x_scene_get_%s_gravity_%s(void) { return %s_gravity_%s; }" % (cls, a, cls, a))
            natives["ccs_x_scene_set_%s_gravity_%s" % (cls, a)] = (
                "void", ["float"], "void ccs_x_scene_set_%s_gravity_%s(float v) { %s_gravity_%s = v; }" % (cls, a, cls, a))
        out.append("    public static class %s\n    {\n        public static %s gravity\n        {\n            get { return new %s(%s); }\n"
                   "            set { %s }\n        }\n    }\n" % (
                       cls, vec, vec, ", ".join("HybridNative.ccs_x_scene_get_%s_gravity_%s()" % (cls, a) for a in axes),
                       " ".join("HybridNative.ccs_x_scene_set_%s_gravity_%s(value.%s);" % (cls, a, a) for a in axes)))
    return ("namespace UnityEngine\n{\n" + "\n".join(out) + "}\n") if out else ""


RANDOM_HELPERS = ("Random_Range_i", "Random_Range_f", "Random_value", "Random_InitState")


def _random_class(natives):
    """UnityEngine.Random over the runtime helpers the packer's own lowering uses (tools/unity/unity_pack_runtime.py): Range of ints and of floats, value,
    InitState, one shared xorshift state, so managed and lowered code draw the same sequence.  apply() asks the packer to emit the helpers
    (plan["_cs_str_used"]) when managed code uses Random, whether or not lowered code did."""
    natives["ccs_x_rand_range_i"] = ("int", ["int", "int"], "int ccs_x_rand_range_i(int a, int b) { return Random_Range_i(a, b); }")
    natives["ccs_x_rand_range_f"] = ("float", ["float", "float"], "float ccs_x_rand_range_f(float a, float b) { return Random_Range_f(a, b); }")
    natives["ccs_x_rand_value"] = ("float", [], "float ccs_x_rand_value(void) { return Random_value(); }")
    natives["ccs_x_rand_init"] = ("void", ["int"], "void ccs_x_rand_init(int seed) { Random_InitState(seed); }")
    return ("namespace UnityEngine\n{\n    public static class Random\n    {\n"
            "        public static int Range(int min, int max) { return HybridNative.ccs_x_rand_range_i(min, max); }\n"
            "        public static float Range(float min, float max) { return HybridNative.ccs_x_rand_range_f(min, max); }\n"
            "        public static float value { get { return HybridNative.ccs_x_rand_value(); } }\n"
            "        public static void InitState(int seed) { HybridNative.ccs_x_rand_init(seed); }\n    }\n}\n")


def _gameobject_class(gens, engine, natives):
    """UnityEngine.GameObject over the engine's object indexes: `gameObject`, Find("literal"), activeSelf / SetActive and GetComponent<T>() for the
    script classes the managed code asks for.  A method the engine lacks is left out, so code that wants it does not build."""
    names = sorted({n for g in gens for n in g.find_names})
    comps = [c for c in dict.fromkeys(c for g in gens for c in g.go_components)
             if re.search(r"^static int GameObject_GetComponent_%s\(int go\)" % re.escape(c), engine, re.M)]
    members = []
    for k, n in enumerate(names):
        natives["ccs_x_go_find_%d" % k] = ("int", [], "int ccs_x_go_find_%d(void) { return GameObject_Find(\"%s\"); }" % (k, n))
    arms = "".join("            if (name == \"%s\") return __Wrap(HybridNative.ccs_x_go_find_%d());\n" % (n, k) for k, n in enumerate(names))
    members.append("        public static GameObject Find(string name)\n        {\n%s            return null;\n        }" % arms)
    if re.search(r"^static int GameObject_activeSelf\(int go\)", engine, re.M):
        natives["ccs_x_go_active"] = ("int", ["int"], "int ccs_x_go_active(int go) { return GameObject_activeSelf(go); }")
        members.append("        public bool activeSelf { get { return HybridNative.ccs_x_go_active(__go) != 0; } }")
    if re.search(r"^static void GameObject_SetActive\(int go, int active\)", engine, re.M):
        natives["ccs_x_go_setactive"] = ("void", ["int", "int"], "void ccs_x_go_setactive(int go, int a) { GameObject_SetActive(go, a); }")
        members.append("        public void SetActive(bool value) { HybridNative.ccs_x_go_setactive(__go, value ? 1 : 0); }")
    arms = ""
    for c in comps:
        natives["ccs_x_go_getc_%s" % c] = ("int", ["int"], "int ccs_x_go_getc_%s(int go) { return GameObject_GetComponent_%s(go); }" % (c, c))
        arms += "            if (n == \"%s\") { int h = HybridNative.ccs_x_go_getc_%s(__go); return h < 0 ? null : %s.__Get((uint)h); }\n" % (c, c, c)
    members.append("        object __comp(string n)\n        {\n%s            return null;\n        }" % arms)
    members.append("        public T GetComponent<T>() where T : class { return __comp(typeof(T).Name) as T; }")
    return ("namespace UnityEngine\n{\n    public class GameObject\n    {\n        public readonly int __go;\n        GameObject(int go) { __go = go; }\n"
            "        static GameObject[] __all = new GameObject[8];\n"
            "        public static GameObject __Wrap(int go)\n        {\n            if (go < 0) return null;\n"
            "            if (go >= __all.Length) {\n                GameObject[] bigger = new GameObject[go * 2 + 8];\n"
            "                for (int k = 0; k < __all.Length; k++) bigger[k] = __all[k];\n                __all = bigger;\n            }\n"
            "            if (__all[go] == null) __all[go] = new GameObject(go);\n            return __all[go];\n        }\n"
            + "\n".join(members) + "\n    }\n}\n")


def _rigidbody2d_class(engine, natives):
    """UnityEngine.Rigidbody2D over the engine's Rigidbody2D_* functions and arrays.  A member the engine has no function for is left out, so
    code that wants it does not build (and keeps its lowered C); the engine's own functions ignore a missing body, as the lowered code relies on."""
    has = lambda n: re.search(r"^static [a-z]+ %s\(" % re.escape(n), engine, re.M) is not None
    m = []

    def prop(name, mt, getter, setter, gc, sc, conv_in="", conv_out=""):
        # a property over Rigidbody2D_get_<x> / Rigidbody2D_set_<x>
        if not (has("Rigidbody2D_get_" + getter) and has("Rigidbody2D_set_" + setter)):
            return
        natives["ccs_x_rb_get_" + getter] = (gc, ["int"], "%s ccs_x_rb_get_%s(int rb) { return Rigidbody2D_get_%s(rb); }" % ({"float": "float", "int": "int"}[gc], getter, getter))
        natives["ccs_x_rb_set_" + setter] = ("void", ["int", sc], "void ccs_x_rb_set_%s(int rb, %s v) { Rigidbody2D_set_%s(rb, v); }" % (setter, sc, setter))
        m.append("        public %s %s { get { return %s; } set { %s; } }" % (
            mt, name, conv_out % ("HybridNative.ccs_x_rb_get_%s(__rb)" % getter), "HybridNative.ccs_x_rb_set_%s(__rb, %s)" % (setter, conv_in % "value")))
    prop("mass", "float", "mass", "mass", "float", "float", "%s", "%s")
    prop("gravityScale", "float", "gravityScale", "gravityScale", "float", "float", "%s", "%s")
    prop("drag", "float", "drag", "drag", "float", "float", "%s", "%s")
    prop("linearDamping", "float", "drag", "drag", "float", "float", "%s", "%s")
    prop("rotation", "float", "rotation", "rotation", "float", "float", "%s", "%s")
    prop("angularVelocity", "float", "angularVelocity", "angularVelocity", "float", "float", "%s", "%s")
    prop("freezeRotation", "bool", "freezeRotation", "freezeRotation", "int", "int", "(%s ? 1 : 0)", "(%s != 0)")
    prop("isKinematic", "bool", "isKinematic", "isKinematic", "int", "int", "(%s ? 1 : 0)", "(%s != 0)")
    prop("bodyType", "RigidbodyType2D", "bodyType", "bodyType", "int", "int", "(int)%s", "(RigidbodyType2D)%s")
    if has("Rigidbody2D_set_velocity") and "_Rigidbody2D_vel_x" in engine:
        natives["ccs_x_rb_vel"] = ("float", ["int", "int"], "float ccs_x_rb_vel(int rb, int axis) { if (!(rb >= 0 && _rb2d_ok(rb))) return 0.f; return axis == 0 ? _Rigidbody2D_vel_x[rb] : _Rigidbody2D_vel_y[rb]; }")
        natives["ccs_x_rb_set_vel"] = ("void", ["int", "float", "float"], "void ccs_x_rb_set_vel(int rb, float x, float y) { Rigidbody2D_set_velocity(rb, x, y); }")
        for nme in ("linearVelocity", "velocity"):
            m.append("        public Vector2 %s { get { return new Vector2(HybridNative.ccs_x_rb_vel(__rb, 0), HybridNative.ccs_x_rb_vel(__rb, 1)); }\n"
                     "            set { HybridNative.ccs_x_rb_set_vel(__rb, value.x, value.y); } }" % nme)
    if has("Rigidbody2D_position_x") and has("Rigidbody2D_set_position"):
        natives["ccs_x_rb_pos"] = ("float", ["int", "int"], "float ccs_x_rb_pos(int rb, int axis) { return axis == 0 ? Rigidbody2D_position_x(rb) : Rigidbody2D_position_y(rb); }")
        natives["ccs_x_rb_set_pos"] = ("void", ["int", "float", "float"], "void ccs_x_rb_set_pos(int rb, float x, float y) { Rigidbody2D_set_position(rb, x, y); }")
        m.append("        public Vector2 position { get { return new Vector2(HybridNative.ccs_x_rb_pos(__rb, 0), HybridNative.ccs_x_rb_pos(__rb, 1)); }\n"
                 "            set { HybridNative.ccs_x_rb_set_pos(__rb, value.x, value.y); } }")
        # (the engine moves a body by setting its position, as the lowered code does)
        m.append("        public void MovePosition(Vector2 p) { HybridNative.ccs_x_rb_set_pos(__rb, p.x, p.y); }")
    if has("Rigidbody2D_AddForce"):
        natives["ccs_x_rb_force"] = ("void", ["int", "float", "float", "int"], "void ccs_x_rb_force(int rb, float x, float y, int mode) { Rigidbody2D_AddForce(rb, x, y, mode); }")
        m.append("        public void AddForce(Vector2 f, ForceMode2D mode) { HybridNative.ccs_x_rb_force(__rb, f.x, f.y, (int)mode); }")
        m.append("        public void AddForce(Vector2 f) { HybridNative.ccs_x_rb_force(__rb, f.x, f.y, 0); }")
    if has("Rigidbody2D_AddTorque"):
        natives["ccs_x_rb_torque"] = ("void", ["int", "float", "int"], "void ccs_x_rb_torque(int rb, float t, int mode) { Rigidbody2D_AddTorque(rb, t, mode); }")
        m.append("        public void AddTorque(float t, ForceMode2D mode) { HybridNative.ccs_x_rb_torque(__rb, t, (int)mode); }")
        m.append("        public void AddTorque(float t) { HybridNative.ccs_x_rb_torque(__rb, t, 0); }")
    return ("namespace UnityEngine\n{\n    public enum ForceMode2D { Force = 0, Impulse = 1 }\n"
            "    public enum RigidbodyType2D { Dynamic = 0, Kinematic = 1, Static = 2 }\n"
            "    public class Rigidbody2D\n    {\n        public readonly int __rb;\n        Rigidbody2D(int rb) { __rb = rb; }\n"
            "        static Rigidbody2D[] __all = new Rigidbody2D[8];\n"
            "        public static Rigidbody2D __Wrap(int rb)\n        {\n            if (rb < 0) return null;\n"
            "            if (rb >= __all.Length) {\n                Rigidbody2D[] bigger = new Rigidbody2D[rb * 2 + 8];\n"
            "                for (int k = 0; k < __all.Length; k++) bigger[k] = __all[k];\n                __all = bigger;\n            }\n"
            "            if (__all[rb] == null) __all[rb] = new Rigidbody2D(rb);\n            return __all[rb];\n        }\n"
            + "\n".join(m) + "\n    }\n}\n")


def _spriterenderer_class(engine, natives):
    """UnityEngine.SpriteRenderer over the engine's `SpriteRenderer_color(go, k)` / `SpriteRenderer_set_color(go, r, g, b, a)`: the tint, the one
    thing the packed engine keeps for a renderer that a script can write.  (The engine stops with a NullReferenceException on a GameObject that
    has no renderer, as the lowered code does.)"""
    natives["ccs_x_spr_color"] = ("float", ["int", "int"], "float ccs_x_spr_color(int go, int k) { return SpriteRenderer_color(go, k); }")
    natives["ccs_x_spr_set_color"] = ("void", ["int", "float", "float", "float", "float"],
                                      "void ccs_x_spr_set_color(int go, float r, float g, float b, float a) { SpriteRenderer_set_color(go, r, g, b, a); }")
    return ("namespace UnityEngine\n{\n    public class SpriteRenderer\n    {\n        public readonly int __go;\n        SpriteRenderer(int go) { __go = go; }\n"
            "        static SpriteRenderer[] __all = new SpriteRenderer[8];\n"
            "        public static SpriteRenderer __Wrap(int go)\n        {\n            if (go < 0) return null;\n"
            "            if (go >= __all.Length) {\n                SpriteRenderer[] bigger = new SpriteRenderer[go * 2 + 8];\n"
            "                for (int k = 0; k < __all.Length; k++) bigger[k] = __all[k];\n                __all = bigger;\n            }\n"
            "            if (__all[go] == null) __all[go] = new SpriteRenderer(go);\n            return __all[go];\n        }\n"
            "        public Color color {\n"
            "            get { return new Color(HybridNative.ccs_x_spr_color(__go, 0), HybridNative.ccs_x_spr_color(__go, 1), HybridNative.ccs_x_spr_color(__go, 2), HybridNative.ccs_x_spr_color(__go, 3)); }\n"
            "            set { HybridNative.ccs_x_spr_set_color(__go, value.r, value.g, value.b, value.a); }\n        }\n    }\n}\n")


def _keyboard_class(engine, natives):
    """UnityEngine.InputSystem.Keyboard over the engine's own `Keyboard_<name>Key_isPressed()` (and wasPressed/ReleasedThisFrame) functions: the
    keys some script of the project names (the packer emits exactly those).  `Keyboard.current` is null while the host says no keyboard is
    connected, as in the lowered code."""
    keys = sorted(set(re.findall(r"^static int Keyboard_(\w+)Key_isPressed\(void\)", engine, re.M)))
    if not keys or not re.search(r"^static EngineKeyboard \*Keyboard_current\(void\)", engine, re.M):
        return None
    natives["ccs_x_kb_connected"] = ("int", [], "int ccs_x_kb_connected(void) { return Keyboard_current() != 0; }")
    arms = "".join("case %d: if (what == 0) return Keyboard_%sKey_isPressed(); if (what == 1) return Keyboard_%sKey_wasPressedThisFrame(); "
                   "if (what == 2) return Keyboard_%sKey_wasReleasedThisFrame(); return 0; " % (k, key, key, key) for k, key in enumerate(keys))
    natives["ccs_x_kb"] = ("int", ["int", "int"], "int ccs_x_kb(int k, int what) { switch (k) { %sdefault: return 0; } }" % arms)
    props = "\n".join("        public KeyControl %sKey { get { return __keys[%d]; } }" % (key, k) for k, key in enumerate(keys))
    return ("namespace UnityEngine.InputSystem\n{\n    public class KeyControl\n    {\n        readonly int k;\n        public KeyControl(int k) { this.k = k; }\n"
            "        public bool isPressed { get { return HybridNative.ccs_x_kb(k, 0) != 0; } }\n"
            "        public bool wasPressedThisFrame { get { return HybridNative.ccs_x_kb(k, 1) != 0; } }\n"
            "        public bool wasReleasedThisFrame { get { return HybridNative.ccs_x_kb(k, 2) != 0; } }\n    }\n"
            "    public class Keyboard\n    {\n        static Keyboard __one = new Keyboard();\n        static KeyControl[] __keys = new KeyControl[] { %s };\n"
            "        public static Keyboard current { get { return HybridNative.ccs_x_kb_connected() != 0 ? __one : null; } }\n%s\n    }\n}\n"
            % (", ".join("new KeyControl(%d)" % k for k in range(len(keys))), props))


def _has_set_world(engine):
    return re.search(r"^static void _engine_set_world\(int c, unsigned i, float wx, float wy, float wz\) \{", engine, re.M) is not None


def _target_transform_class(natives, engine):
    """the Transform of a Transform field's object (a class id and an instance): world position through the engine's own helpers"""
    natives["ccs_x_xf_get"] = ("float", ["int", "int", "int"],
                               "float ccs_x_xf_get(int c, int inst, int axis) { float x, y, z; _engine_world_pos(c, (unsigned)inst, &x, &y, &z, 0); return axis == 0 ? x : axis == 1 ? y : z; }")
    can_set = _has_set_world(engine)
    if can_set:
        natives["ccs_x_xf_set"] = ("void", ["int", "int", "float", "float", "float"],
                                   "void ccs_x_xf_set(int c, int inst, float x, float y, float z) { _engine_set_world(c, (unsigned)inst, x, y, z); }")
    nope = "throw new System.NotSupportedException(\"the managed shim keeps only the position of another object's transform\");"
    return ("public class __TargetTransform : UnityEngine.Transform\n{\n    readonly int c, inst;\n    public __TargetTransform(int c, int inst) { this.c = c; this.inst = inst; }\n"
            "    public override UnityEngine.Vector3 position {\n        get { return new UnityEngine.Vector3(HybridNative.ccs_x_xf_get(c, inst, 0), HybridNative.ccs_x_xf_get(c, inst, 1), HybridNative.ccs_x_xf_get(c, inst, 2)); }\n"
            "        set { %s }\n    }\n"
            "    public override UnityEngine.Vector3 localPosition { get { %s } set { %s } }\n"
            "    public override UnityEngine.Quaternion rotation { get { %s } set { %s } }\n}\n"
            % ("HybridNative.ccs_x_xf_set(c, inst, value.x, value.y, value.z);" if can_set else nope, nope, nope, nope, nope))


def managed_source(gens, time_names):
    natives = {}
    parts = ["using System;\nusing UnityEngine;\n"]
    for g in gens:
        natives.update(g.natives)
    body = []
    if time_names:
        body.append(_time_class(natives, time_names))
    if gens:
        inp = _input_class(gens[0].engine, natives)
        if inp:
            body.append(inp)
        if any("Random" in g.words for g in gens):
            body.append(_random_class(natives))
            parts[0] += "using Random = UnityEngine.Random;     // (a script with both usings has to say which, as in Unity)\n"
        if any(g.uses_go for g in gens):
            body.append(_gameobject_class(gens, gens[0].engine, natives))
        scn = _scene_class(gens[0].engine, natives)
        if scn:
            body.append(scn)
        if any("Keyboard" in g.words for g in gens):
            kb = _keyboard_class(gens[0].engine, natives)
            if kb:
                body.append(kb)
                parts[0] += "using UnityEngine.InputSystem;\n"
        if any(g.uses_rb2d for g in gens):
            body.append(_rigidbody2d_class(gens[0].engine, natives))
        if any(g.uses_spr for g in gens):
            body.append(_spriterenderer_class(gens[0].engine, natives))
        if any(g.xf_fields for g in gens):
            body.append(_target_transform_class(natives, gens[0].engine))
    body += [g.src for g in gens]
    parts.append(_native_decls(natives))
    parts += body
    return "\n".join(parts), natives


# ---- native side ---------------------------------------------------------------------------------------------------------------------------

def _kind_conv(t):
    return SCALAR[t]


def glue_c(cands_by_class, ok_names):
    """hybrid_glue.c: native -> managed.  One function for each managed method, with the C signature the engine's stub has."""
    out = ["/* unity_pack --hybrid: the C side of the native/managed bridge.  Generated. */",
           "#include <stdio.h>", "#include <stdlib.h>", "#include <string.h>", "#include <unistd.h>", '#include "Host.h"', "",
           "static DNA_Assembly *hy_asm;", "", "typedef struct Vector2 { float x; float y; } Vector2;", "float ccs_ret2[2];", "",
           "static const char *hy_dll_path(char *buf, size_t n) {",
           "\tconst char *e = getenv(\"UNITY_PACK_MANAGED_DLL\");", "\tssize_t k;", "\tif (e != NULL && *e) return e;",
           "\tk = readlink(\"/proc/self/exe\", buf, n - 1);", "\tif (k > 0) {", "\t\tchar *s;", "\t\tbuf[k] = 0;", "\t\ts = strrchr(buf, '/');",
           "\t\tif (s != NULL) {", "\t\t\tsnprintf(s + 1, n - (size_t)(s + 1 - buf), \"%s\", \"" + ASSEMBLY + "\");",
           "\t\t\tif (access(buf, R_OK) == 0) return buf;", "\t\t}", "\t}", "\treturn \"" + ASSEMBLY + "\";", "}", "",
           "static void hy_ensure(void) {", "\tchar buf[4096];", "\tconst char *path;", "\tif (hy_asm != NULL) return;", "\tpath = hy_dll_path(buf, sizeof buf);",
           "\tif (access(path, R_OK) != 0) { fprintf(stderr, \"unity_pack: cannot load the managed assembly %s (it belongs beside the player; "
           "UNITY_PACK_MANAGED_DLL names another)\\n\", path); exit(70); }",
           "\tDNA_SetCrashMode(1);", "\tDNA_SetAssemblyDirFromFile(path);", "\tDNA_Init();", "\thy_asm = DNA_Load(path);",
           "\tif (hy_asm == NULL) { fprintf(stderr, \"unity_pack: cannot load the managed assembly: %s\\n\", DNA_Error()); exit(70); }", "}", "",
           "static DNA_Method *hy_find(const char *cls, const char *name, const char *sig) {", "\tDNA_Method *m;", "\thy_ensure();",
           "\tm = DNA_Find(hy_asm, \"\", cls, name, sig);",
           "\tif (m == NULL) { fprintf(stderr, \"unity_pack: %s\\n\", DNA_Error()); exit(70); }", "\treturn m;", "}", ""]
    for cname in sorted(ok_names):
        for c in cands_by_class[cname]:
            ret = (c["ret"] or "void").strip()
            st = bool(c.get("static"))
            lead = 0 if st else 1
            n = len(c["params"]) + lead
            vret = ret == VEC2
            rc = VEC2 if vret else (SCALAR[ret][3] if ret in SCALAR else "void")
            n = lead + sum(2 if t_ == VEC2 else 1 for t_, _n in c["params"])
            sig = ("" if st else "i") + _dna_letters(c["params"]) + ">" + ("v" if vret else (SCALAR[ret][2] if ret in SCALAR else "v"))
            out.append("%s ccs_b_%s(%s) {" % (rc, c["sym"], _cparams(c)))
            out.append("\tstatic DNA_Method *m;")
            out.append("\tDNA_Value a[%d], r;" % max(n, 1))
            out.append("\tif (m == NULL) m = hy_find(\"%s\", \"__%s\", \"%s\");" % (cname, c["sym"], sig))
            if not st:
                out.append("\ta[0] = DNA_Int(i);")
            slot = lead
            for k, (t, _n) in enumerate(c["params"]):
                if t == VEC2:
                    out.append("\ta[%d] = DNA_Float(a%d.x);" % (slot, k))
                    out.append("\ta[%d] = DNA_Float(a%d.y);" % (slot + 1, k))
                    slot += 2
                else:
                    out.append("\ta[%d] = %s(a%d);" % (slot, SCALAR[t][4], k))
                    slot += 1
            out.append("\tif (DNA_Call(m, a, %d, &r) != 0) { fprintf(stderr, \"unity_pack: %%s\\n\", DNA_Error()); exit(70); }" % n)
            if vret:
                out.append("\t{ %s v; v.x = ccs_ret2[0]; v.y = ccs_ret2[1]; return v; }" % VEC2)
            elif ret in SCALAR:
                field = {"i": "i", "l": "l", "f": "f", "d": "d"}[SCALAR[ret][2]]
                out.append("\treturn (%s)r.u.%s;" % (rc, field))
            else:
                out.append("\t(void)r;")
            out.append("}")
            out.append("")
    return "\n".join(out)


def _cparams(c):
    """the C parameter list of a managed method's engine-side function: `unsigned i, int a0, ..`, or without `i` for a static (`void` when empty)"""
    ps = ([] if c.get("static") else ["unsigned i"]) + ["%s a%d" % (_ctype(t), k) for k, (t, _n) in enumerate(c["params"])]
    return ", ".join(ps) if ps else "void"


def _proto(c):
    ret = (c["ret"] or "void").strip()
    rc = _ctype(ret) if _carrier(ret) else "void"
    return "%s ccs_b_%s(%s);" % (rc, c["sym"], _cparams(c))


def ffi_manifest(natives):
    return {"c_files": [], "cflags": [], "functions": [
        {"library": "ccs_native", "entry": ex, "ret": natives[ex][0], "args": list(natives[ex][1])} for ex in sorted(natives)]}


# ---- a method's own source ----------------------------------------------------------------------------------------------------------------

def original_body(path, cname, mname, nparams):
    """The body of method *mname* (with *nparams* parameters) of class *cname*, as written in the project's file *path*, before the packer's source
    rewrites (extension calls, collections, coroutines, input actions ...) -- or None when it is not there exactly once."""
    import tools.unity.unity_pack_common as common
    import tools.cs2cpp as cs2cpp
    text = common.SOURCE_ORIGINAL.get(os.path.abspath(path)) if path else None
    if not text:
        return None
    scan = cs2cpp._blank(text)
    m = re.search(r"\bclass\s+%s\b[^{;]*\{" % re.escape(cname), scan)
    if not m:
        return None
    end, depth = None, 0
    for k in range(m.end() - 1, len(scan)):
        if scan[k] == "{":
            depth += 1
        elif scan[k] == "}":
            depth -= 1
            if depth == 0:
                end = k
                break
    if end is None:
        return None
    region = scan[m.end():end]
    hits = []
    for h in re.finditer(r"(?<![\w.])%s\s*\(([^()]*)\)\s*(?:where[^{;]*)?\{" % re.escape(mname), region):
        params = h.group(1).strip()
        if (0 if not params else params.count(",") + 1) != nparams:
            continue
        hits.append(h)
    if len(hits) != 1:
        return None
    start = m.end() + hits[0].end()
    depth = 1
    for k in range(start, end):
        if scan[k] == "{":
            depth += 1
        elif scan[k] == "}":
            depth -= 1
            if depth == 0:
                return text[start:k]
    return None


def with_original_bodies(cands):
    """the candidates with each body replaced by the method's own source where that can be found (the rest as they were)"""
    out, changed = [], False
    for c in cands:
        body = original_body(c.get("src_path"), c.get("cls") or "", c["name"], len(c["params"])) if c.get("src_path") and c.get("cls") else None
        if body is not None and body != c["body"]:
            c = dict(c, body=body)
            changed = True
        out.append(c)
    return out if changed else None


# ---- project helper classes ---------------------------------------------------------------------------------------------------------------

_TYPE_DECL = re.compile(r"\b(?:class|struct|enum|interface)\s+([A-Za-z_]\w*)")
_ENGINE_BASES = re.compile(r":\s*(?:[\w.<>, ]*,\s*)?(?:MonoBehaviour|ScriptableObject|StateMachineBehaviour|Editor|EditorWindow)\b")


_EXTENSION_DECL = re.compile(r"\bstatic\s+[\w<>\[\],.?]+(?:\s*<[^>]*>)?\s+(\w+)\s*(?:<[^>]*>)?\s*\(\s*this\s")


def project_helpers(root, script_classes, bodies):
    """The project's own C# files (plain classes, structs, enums, static utilities) that managed code names, directly or through each other,
    as {type name: path}-closed list of paths.  A file that declares a script class (the packer's own), or an Editor file, is not one:
    the engine owns the first, and the second is not part of the game."""
    files = {}
    for dp, dn, fns in os.walk(os.path.join(root, "Assets")):
        dn[:] = [d for d in dn if d != "Editor"]
        for fn in fns:
            if fn.endswith(".cs"):
                path = os.path.join(dp, fn)
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        files[path] = f.read()
                except (OSError, UnicodeDecodeError):
                    pass
    declared = {}
    for path, text in files.items():
        names = set(_TYPE_DECL.findall(text))
        if names & set(script_classes) or _ENGINE_BASES.search(text):
            continue
        for n in names:
            declared.setdefault(n, path)
    # an extension method (`v.SetX(1f)`) names no class: the file that declares one of that name is needed too
    for path, text in files.items():
        if path in declared.values():
            for m in _EXTENSION_DECL.finditer(text):
                declared.setdefault(m.group(1), path)
    need, todo = [], _words(bodies)
    seen = set()
    while todo:
        w = todo.pop()
        path = declared.get(w)
        if path is None or path in seen:
            continue
        seen.add(path)
        need.append(path)
        todo |= _words(files[path])
    return sorted(need)


# ---- the step in pack() --------------------------------------------------------------------------------------------------------------------

def apply(plan, engine, outdir, report_stub, progress=lambda m: None):
    """Replace the stubs that managed code can stand in for.  Returns the engine text.

    *plan["_hybrid_deferred"]* holds the stubs emit_engine did not report; the ones this does not resolve are reported here, as they would have been."""
    deferred = plan.pop("_hybrid_deferred", []) or []
    cands_by_class = plan.pop("_hybrid_cands", {}) or {}
    methods = plan.pop("_hybrid_methods", {}) or {}
    notes = {}

    # --managed: a selected class runs as managed C# whole.  Its methods the packer lowered fine (and the boundary can carry) join the ones it
    # could not lower, as candidates; they are swapped for the managed call only once the managed assembly has built, so a class that cannot
    # be built managed is exactly what it was without --managed.
    selected = plan.get("managed") or set()
    forced = set()
    if selected:
        for cname, sibs in methods.items():
            if selected != "*" and cname not in selected:
                continue
            fs = [dict(s, forced=True) for s in sibs if s.get("scalar") and not s.get("stub")]
            if fs:
                cands_by_class.setdefault(cname, []).extend(fs)
                forced.add(cname)
        if selected != "*":
            for cname in sorted(selected - set(methods) - set(cands_by_class)):
                progress("managed: no script class %s with a method to run managed" % cname)
    declined = plan.setdefault("managed_declined", {})

    def finish(resolved):
        for cname in sorted(forced):
            if not any(r[0] == cname for r in resolved):
                declined[cname] = notes.get(cname) or "no managed method could be built"
                progress("managed: %s stays lowered: %s" % (cname, declined[cname]))
        for site, cl, m, why in deferred:
            if (cl["name"], m["name"]) in resolved:
                plan.setdefault("hybrid_methods", []).append({"class": cl["name"], "method": m["name"]})
                continue
            extra = notes.get(cl["name"])
            report_stub(plan, site, cl, m, (why[0] + ("; hybrid: " + extra if extra else ""), why[1]))
        return engine

    if not cands_by_class:
        return finish(set())
    ok, why_not = available()
    if not ok:
        for c in cands_by_class:
            notes[c] = why_not
        return finish(set())
    try:
        corlib = prepare_corlib()
    except HybridError as e:
        for c in cands_by_class:
            notes[c] = str(e).splitlines()[0]
        return finish(set())

    os.makedirs(outdir, exist_ok=True)
    work = os.path.join(outdir, "hybrid_work")
    os.makedirs(work, exist_ok=True)
    gens, variants = {}, {}
    root = plan.get("_root")
    script_classes = set(plan["classes"] if not isinstance(plan["classes"], list) else [c["name"] for c in plan["classes"]])
    for cname, cands in sorted(cands_by_class.items()):
        cl = next(c for c in plan["classes"] if c["name"] == cname) if isinstance(plan["classes"], list) else plan["classes"][cname]
        # the methods' own source first (the packer's rewrites of it are for lowering to C), then the rewritten text the lowering took
        variants[cname] = []
        for vc in (with_original_bodies(cands), cands):
            if vc is None:
                continue
            g = ClassGen(plan, engine, cl, vc, methods.get(cname, []))
            g.helpers = project_helpers(root, script_classes, "\n".join(c["body"] for c in vc)) if root else []
            for h in g.helpers:
                with open(h, encoding="utf-8-sig") as f:
                    g.words |= _words(f.read())
            variants[cname].append(g)
        gens[cname] = variants[cname][-1]
    # the script classes a managed class names are managed classes as well (handles on their indexes, fields over their accessors, their lowered
    # methods through the bridge): "peers", built from the engine alone, with every word any managed code uses
    all_used = set()
    for gs in variants.values():
        for g in gs:
            all_used |= g.words
            all_used |= g._used
    peers = {}

    def peer(cname):
        if cname not in peers:
            cl = next(c for c in plan["classes"] if c["name"] == cname) if isinstance(plan["classes"], list) else plan["classes"][cname]
            peers[cname] = ClassGen(plan, engine, cl, [], methods.get(cname, []), all_used)
        return peers[cname]

    def needed(g):
        """the peers (names) g's code needs, with the ones those need"""
        seen, todo = set(), sorted(g.refs)
        while todo:
            c = todo.pop()
            if c in seen or c == g.name:
                continue
            seen.add(c)
            todo += sorted(peer(c).refs)
        return seen

    good = []

    def check(g, tag):
        """(ok, note): does the managed class of *g* (with the peers it needs) compile?"""
        if g.problem:
            return False, g.problem
        need = sorted(needed(g))
        bad_peer = next((c for c in need if peer(c).problem), None)
        if bad_peer:
            return False, "the managed code names %s, which cannot be a managed class: %s" % (bad_peer, peer(bad_peer).problem)
        text, _n = managed_source([g] + [peer(c) for c in need], g.time)
        src = os.path.join(work, "check_%s.cs" % tag)
        with open(src, "w") as f:
            f.write(text)
        ok, msg = compile_managed([SHIM, src] + g.helpers, os.path.join(work, "check_%s.dll" % tag), corlib)
        if ok:
            return True, None
        errs = [l for l in msg.splitlines() if "error" in l]
        return False, "the managed code does not compile: " + (re.sub(r"^.*?\):\s*", "", errs[0]) if errs else msg.strip()[:160])

    for cname in sorted(gens):
        note = None
        for g in variants[cname]:
            ok, why = check(g, cname)
            if ok:
                gens[cname] = g
                good.append(cname)
                note = None
                break
            note = note or why
        if cname not in good and len(variants[cname][0].cands) > 1:
            # the class as a whole does not build: the methods that do, one by one, still can (the rest keep their lowered C or their stub)
            cl = next(c for c in plan["classes"] if c["name"] == cname) if isinstance(plan["classes"], list) else plan["classes"][cname]
            passing = []
            for k in range(len(variants[cname][0].cands)):
                for vi, vg in enumerate(variants[cname]):
                    one = ClassGen(plan, engine, cl, [vg.cands[k]], methods.get(cname, []))
                    one.helpers = project_helpers(root, script_classes, vg.cands[k]["body"]) if root else []
                    ok, why = check(one, "%s_%d" % (cname, k))
                    if ok:
                        passing.append(vg.cands[k])
                        break
                else:
                    progress("managed: %s.%s stays lowered: %s" % (cname, variants[cname][0].cands[k]["name"], why))
            if passing:
                g = ClassGen(plan, engine, cl, passing, methods.get(cname, []))
                g.helpers = sorted({h for c in passing for h in (project_helpers(root, script_classes, c["body"]) if root else [])})
                for h in g.helpers:
                    with open(h, encoding="utf-8-sig") as f:
                        g.words |= _words(f.read())
                ok, why = check(g, cname + "_part")
                if ok:
                    gens[cname] = g
                    good.append(cname)
                    note = None
        if cname not in good:
            notes[cname] = note
        else:
            cands_by_class[cname] = gens[cname].cands
    if not good:
        shutil.rmtree(work, ignore_errors=True)
        return finish(set())

    need_all = sorted({c for cname in good for c in needed(gens[cname])} - set(good))
    text, natives = managed_source([gens[c] for c in good] + [peer(c) for c in need_all], time_members(engine))
    gen_cs = os.path.join(outdir, "hybrid_generated.cs")
    with open(gen_cs, "w") as f:
        f.write(text)
    helpers = sorted({h for c in good for h in gens[c].helpers})
    ok, msg = compile_managed([SHIM, gen_cs] + helpers, os.path.join(outdir, ASSEMBLY), corlib)
    if not ok:                                   # (each class compiled alone: this is the union; it should too)
        for c in good:
            notes[c] = "the managed code does not compile together: " + msg.strip()[:160]
        return finish(set())
    shutil.rmtree(work, ignore_errors=True)

    # the engine: calls instead of the markers, prototypes, and the wrappers managed code calls
    resolved = set()
    for cname in good:
        for c in cands_by_class[cname]:
            marker = "/* unity_pack:hybrid %s */" % c["sym"]
            args = ", ".join(([] if c.get("static") else ["i"]) + [n for _t, n in c["params"]])
            ret = (c["ret"] or "void").strip()
            call = ("return " if _carrier(ret) else "") + "ccs_b_%s(%s);" % (c["sym"], args)
            if c.get("forced"):
                swapped = _swap_body(engine, c["sym"], call)
                if swapped is None:
                    raise HybridError("internal: the function %s of %s.%s is not in engine.c" % (c["sym"], cname, c["name"]))
                engine = swapped
                plan.setdefault("managed_methods", []).append({"class": cname, "method": c["name"]})
            elif marker not in engine:
                raise HybridError("internal: the marker of %s.%s is not in engine.c" % (cname, c["name"]))
            else:
                engine = engine.replace(marker, call, 1)
            resolved.add((cname, c["name"]))
    protos = "".join(_proto(c) + "\n" for cname in good for c in cands_by_class[cname])
    anchor = "#include <stdint.h>\n"
    if "Vector2" in protos and "} Vector2;\n" in engine:
        anchor = "} Vector2;\n"             # (the prototypes name the type)
    k = engine.index(anchor) + len(anchor)
    engine = engine[:k] + "/* unity_pack --hybrid: managed methods, defined in hybrid_glue.c */\n" + (
        "extern float ccs_ret2[2];\n" if any("ccs_ret2" in natives[ex][2] for ex in natives) or "Vector2 ccs_b_" in protos else "") + protos + engine[k:]
    if "ccs_x_rand_value" in natives:
        plan.setdefault("_cs_str_used", set()).update(RANDOM_HELPERS)     # (the packer emits them after this, with what they call)
    if "ccs_x_input_edge" in natives:
        # the key latch runs first in every tick, so GetKeyDown / GetKeyUp hold for the whole frame
        engine = engine.replace("void engine_tick(void) {\n", "void engine_tick(void) {\n    ccs_keys_latch();\n", 1)
        k = engine.index("/* unity_pack --hybrid: managed methods")
        engine = engine[:k] + "static void ccs_keys_latch(void);\n" + engine[k:]
    wrappers = "\n/* unity_pack --hybrid: what managed code may call (hybrid.ffi.json) */\n" + "\n".join(natives[ex][2] for ex in sorted(natives)) + "\n"
    engine = engine.rstrip("\n") + "\n" + wrappers
    with open(os.path.join(outdir, "hybrid_glue.c"), "w") as f:
        f.write(glue_c(cands_by_class, good))
    with open(os.path.join(outdir, "hybrid.ffi.json"), "w") as f:
        json.dump(ffi_manifest(natives), f, indent=1)
    counts = {}
    for cname in good:
        for c in cands_by_class[cname]:
            counts[(cname, c["name"])] = counts.get((cname, c["name"]), 0) + 1
    progress("hybrid: %d managed method(s) in %d class(es): %s" % (
        sum(counts.values()), len(good), ", ".join("%s.%s%s" % (k[0], k[1], " x%d" % n if n > 1 else "") for k, n in sorted(counts.items()))))
    return finish(resolved)


def _swap_body(engine, sym, call):
    """engine with the body of the lowered function *sym* replaced by *call* (the C that calls its managed twin); None when it is not there.
    The packer writes a function as `static T sym(...) {` .. a line that is only `}`, its body indented."""
    m = re.search(r"^static [^\n]*?\b%s\([^\n]*\) \{\n" % re.escape(sym), engine, re.M)
    if m is None:
        return None
    end = engine.find("\n}\n", m.end() - 1)
    if end < 0:
        return None
    return engine[:m.end()] + "    " + call + engine[end:]


def clean(outdir):
    """No hybrid pieces are left behind by a pack that does not use them."""
    for name in ("hybrid_generated.cs", ASSEMBLY, "hybrid_glue.c", "hybrid.ffi.json", "hybrid_glue.o", "corlib.dll"):
        p = os.path.join(outdir, name)
        if os.path.exists(p):
            os.remove(p)


def link_inputs(outdir, cc, run, progress):
    """For build_player_executable: (objects, libraries) that put DotNetAnywhere in the player, and the managed assembly beside it."""
    glue = os.path.join(outdir, "hybrid_glue.c")
    if not os.path.isfile(glue):
        return [], []
    home, bdir = dna_home(), build_dir()
    manifest = os.path.join(outdir, "hybrid.ffi.json")
    corlib = prepare_corlib()
    progress("building DotNetAnywhere with the managed-to-native functions")
    _dna_run(["--ffi", manifest, "--lib-only", "--no-corlib"])
    obj = os.path.join(outdir, "hybrid_glue.o")
    run([cc, "-O2", "-I", os.path.join(home, "native", "src"), "-c", "-o", obj, glue])
    shutil.copy(corlib, os.path.join(outdir, "corlib.dll"))
    return [obj], [os.path.join(bdir, "libdna_ffi.a"), "-lpthread"]
