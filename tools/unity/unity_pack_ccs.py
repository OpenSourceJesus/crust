"""unity_pack_ccs -- the CCSharp backend of unity_pack.

The project's scripts are C#; CCSharp (https://github.com/crustos/CCSharp) lowers them, together with its small UnityEngine
(unity-engine/*.cs), to the C++ subset and then to C.  This module does not parse or lower C#: it reads the scene with
unity_pack's scene loader, writes the scene as C# (`SceneData.cs`: `new GameObject`, `Attach`), copies the scripts, and hands
the lot to `ccs2c.py --unity-engine`.

`eligible(objects, cameras)` says whether the engine covers the project; when it does not, unity_pack refuses it.

    CCSHARP_HOME   the CCSharp checkout (default ../CCSharp beside this repo)
    DNA_HOME       DotNetAnywhere, for scripts outside the Crust subset (default ../DotNetAnywhere beside CCSharp)
"""
import math
import functools
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# Component keys the engine has no class for yet.  A scene record that has any of them set goes to the packed path.
UNSUPPORTED_KEYS = (
    "canvas", "ui_text", "ui_image", "ui_button", "particle_system", "animator",
    "audio_source", "light", "tilemap",
)
# Of those, the ones the managed engine has (scene_source builds them): a project with DotNetAnywhere may use them.
MANAGED_KEYS = ("canvas", "ui_image", "ui_button", "tilemap")
COLLIDER_KINDS = ("box", "circle", "capsule_h", "capsule_v", "polygon")
# guid -> PhysicsMaterial2D {friction, bounciness, friction_combine, bounce_combine}, set by build()
MATERIALS = {}

# Messages a script declares without `override` in Unity; the engine's MonoBehaviour has them virtual.
MESSAGES = ("Awake", "OnEnable", "Start", "Update", "LateUpdate", "FixedUpdate", "OnDisable", "OnDestroy",
            "OnCollisionEnter2D", "OnCollisionStay2D", "OnCollisionExit2D",
            "OnTriggerEnter2D", "OnTriggerStay2D", "OnTriggerExit2D")


def ccsharp_home():
    return os.environ.get("CCSHARP_HOME") or os.path.join(os.path.dirname(REPO), "CCSharp")


def project_identity(root):
    """(companyName, productName) from ProjectSettings, else Unity's defaults (DefaultCompany, the project folder's name)."""
    company, product = "DefaultCompany", os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "Player"
    path = os.path.join(root, "ProjectSettings", "ProjectSettings.asset")
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        for key in ("companyName", "productName"):
            m = re.search(r"(?m)^\s*%s:\s*(.*?)\s*$" % key, text)
            if m and m.group(1).strip("'\""):
                if key == "companyName":
                    company = m.group(1).strip("'\"")
                else:
                    product = m.group(1).strip("'\"")
    return company, product


def available():
    return os.path.exists(os.path.join(ccsharp_home(), "crust", "ccs2c.py"))


def dna_available():
    """DotNetAnywhere where CCSharp's --dna looks for it ($DNA_HOME, default ../DotNetAnywhere)."""
    home = os.environ.get("DNA_HOME") or os.path.join(os.path.dirname(ccsharp_home()), "DotNetAnywhere")
    return os.path.exists(os.path.join(home, "native", "src", "Host.h"))


def eligible(objects, cameras, prefabs=()):
    """(ok, why): ok when every record uses only what the engine covers."""
    managed = dna_available()
    for records in [objects] + [pf["objects"] for pf in prefabs]:     # a parent is looked for in the same scene or prefab
        xf_ids = {r.get("xf_id") for r in records}
        for o in records:
            for k in UNSUPPORTED_KEYS:
                if o.get(k) and not (managed and k in MANAGED_KEYS):
                    return False, "%s uses %s" % (o.get("name"), k)
            col = o.get("collider2d")
            if col and col.get("kind") not in COLLIDER_KINDS:
                return False, "%s uses a %s collider" % (o.get("name"), col.get("kind"))
            if o.get("father_id") and o["father_id"] not in xf_ids:
                return False, "%s: parent is not a plain object" % o.get("name")
            if o.get("script") and not os.path.exists(o["script"]):
                return False, "%s: script file missing" % o.get("name")
    return True, ""


def describe(script_texts):
    """Roslyn's account of the scripts: {class: {"base": name|None, "fields": {name: type}}}, from `ccs2c --describe` over the scripts and the
    engine.  The packer does not read C#: it asks."""
    import tempfile
    import json
    sys.path.insert(0, os.path.join(ccsharp_home(), "crust"))
    import ccs2c
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as work:
        for name, text in script_texts.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        out = os.path.join(work, "describe.json")
        ccs2c.to_cpp([d], unity_engine=True, describe=out, workdir=work, dna=dna_available())
        with open(out) as f:
            return json.load(f)


def declared_fields(classes, cls):
    """{name: type} of a class's public fields, inherited ones included."""
    out = {}
    while cls in classes:
        for k, v in classes[cls]["fields"].items():
            out.setdefault(k, v)
        cls = classes[cls]["base"]
    return out


# Asset GUID -> path (the packer's map, set by write_project), and the AudioClips a scene refers to: guid -> (variable, name, seconds, rate, channels)
ASSET_PATHS = {}
CLIPS = {}


def clip_expr(guid):
    """`SceneData.clipN`, the AudioClip of a sound asset, or None when the asset is not a sound file.
    ponytail: WAV is read (its length, rate, channels: stdlib `wave`); another format's clip has length 0.  No samples: the host has no audio yet."""
    path = ASSET_PATHS.get(guid)
    if not path or os.path.splitext(path)[1].lower() not in (".wav", ".ogg", ".mp3", ".aif", ".aiff", ".flac"):
        return None
    if guid not in CLIPS:
        secs, rate, ch = 0.0, 44100, 1
        if path.lower().endswith(".wav"):
            import wave
            try:
                with wave.open(path) as w:
                    rate, ch = w.getframerate(), w.getnchannels()
                    secs = w.getnframes() / float(rate or 1)
            except (wave.Error, OSError, EOFError):
                pass
        CLIPS[guid] = ("clip%d" % len(CLIPS), os.path.splitext(os.path.basename(path))[0], secs, rate, ch)
    return "SceneData." + CLIPS[guid][0]


def ref_expr(fid, ty, obj_of):
    """The object a scene reference (fileID `fid`) names, as an expression of the field's declared type `ty`; None when it is not in this scene
    or prefab (an asset), or the type is not one an object or a component can be.
    ponytail: a component is found by type on the object (GetComponent<T>), so of two of one type on one object the first is taken.  Upgrade path:
    each script record carrying its own MonoBehaviour fileID (the primary record merges them all)."""
    fid = str(fid)
    go = obj_of.get(fid) if "@" not in fid else obj_of.get("@" + fid.split("@")[-1])
    in_prefab = "@" in fid
    t = (ty or "").strip()
    if in_prefab and go is None and t.split(".")[-1] == "AudioClip":
        return clip_expr(fid.split("@")[-1])
    if not in_prefab and ("#" + fid) in obj_of and t.split(".")[-1] not in ("GameObject", "Transform"):
        return obj_of["#" + fid]            # that very script
    if go is None or not t or t.endswith("]") or t.startswith("System.") or t in ("string", "int", "float", "bool", "double", "object"):
        return None
    short = t.split(".")[-1]
    if short == "GameObject":
        return go
    if short == "Transform":
        return "%s.transform" % go
    if in_prefab and obj_of.get(":managed"):
        # the one its fileID names in the template (a prefab script has its sourceId); else (a built-in component) the first of its type
        return "(UnityEngine.Object.FindSource<%s>(%s, %sL) ?? %s.GetComponentInChildren<%s>(true))" % (t, go, int(fid.split("@")[0]), go, t)
    # (in a prefab's template the component may be on an object below its root: the first of its type there)
    return ("%s.GetComponentInChildren<%s>(true)" if in_prefab else "%s.GetComponent<%s>()") % (go, t)


def _element_type(ty):
    """`T` of `T[]` or `List<T>`, else None."""
    t = (ty or "").strip()
    if t.endswith("[]"):
        return t[:-2]
    m = re.match(r"^(?:System\.Collections\.Generic\.)?List<(.+)>$", t)
    return m.group(1) if m else None


def scene_refs(c, decl, r, obj_of):
    """`c.field = <object>;` for a script's scene references (single ones and arrays / lists of them), public fields only."""
    out = []
    for fname, fid in sorted((r.get("object_refs") or {}).items()):
        e = ref_expr(fid, decl.get(fname), obj_of) if fname in decl else None
        if e is not None:
            out.append("%s.%s = %s;" % (c, fname, e))
    for fname, fids in sorted((r.get("object_ref_arrays") or {}).items()):
        ty = decl.get(fname)
        elem = _element_type(ty)
        if elem is None:
            continue
        # an asset that is not loaded (an AudioClip, a Sprite ...) is a null slot: the array keeps its length, which scripts index by
        items = [(ref_expr(f, elem, obj_of) or "null") if str(f) != "0" else "null" for f in fids]
        if ty.strip().endswith("[]"):
            out.append("%s.%s = new %s[] { %s };" % (c, fname, elem, ", ".join(items)))
        else:
            out.append("%s.%s = new System.Collections.Generic.List<%s> { %s };" % (c, fname, elem, ", ".join(items)))
    return out


def _class_info(classes, ty):
    t = (ty or "").strip()
    return classes.get(t) or classes.get(t.split(".")[-1]) or {}


_VECTORS = {"Vector2": "xy", "Vector3": "xyz", "Vector4": "xyzw", "Vector2Int": "xy", "Vector3Int": "xyz", "Quaternion": "xyzw", "Color": "rgba"}


def scalar_values(path, decl, rec, classes):
    """`path.k = v;` for the scene's plain values of the public fields `decl` (a script's, or an embedded value's): numbers and bools, an enum
    (stored as its number), a string, and Vector2 / Vector3 / Vector2Int / Vector3Int."""
    out = []
    nums = rec.get("fields") or {}
    for k, val in sorted(nums.items()):
        ty = decl.get(k)
        if ty is None:
            continue
        lit = field_literal(ty, val)
        if lit is None and _class_info(classes, ty).get("kind") == "enum" and float(val) == int(val):
            lit = "(%s)%d" % (ty, int(val))
        if lit is not None:
            out.append("%s.%s = %s;" % (path, k, lit))
    for k, val in sorted((rec.get("str_fields") or {}).items()):
        if decl.get(k) == "string" and k not in nums:
            out.append('%s.%s = "%s";' % (path, k, _cs_string(val)))
    done = set()
    # a vector stored as its components (`location_x`, `location_y`: the record's own fields), and a Rect / RectInt embedded in the value
    for k, ty in sorted(decl.items()):
        short = (ty or "").split(".")[-1]
        comps = _VECTORS.get(short)
        if comps and k + "_" + comps[0] in nums and k + "_" + comps[1] in nums:
            vals = [nums.get(k + "_" + ch, 1.0 if ch in "aw" and short in ("Color", "Quaternion") else 0.0) for ch in comps]
            lit = ", ".join(str(int(round(float(x)))) if short.endswith("Int") else _f(x) for x in vals)
            out.append("%s.%s = new %s(%s);" % (path, k, short, lit))
            done.add(k)
        sv = (rec.get("struct_values") or {}).get(k)
        if short in ("Rect", "RectInt") and sv:
            f = sv.get("fields") or {}
            if all(c in f for c in ("x", "y", "width", "height")):
                vals = [f[c] for c in ("x", "y", "width", "height")]
                out.append("%s.%s = new %s(%s);" % (path, k, short, ", ".join(str(int(v)) if short == "RectInt" else _f(v) for v in vals)))
                done.add(k)
    for k, vals in sorted((rec.get("num_arrays") or {}).items()):
        ty = (decl.get(k) or "").strip()
        elem = _element_type(ty)
        if elem in ("float", "int", "double", "long", "short", "byte"):
            lits = ", ".join(field_literal(elem, v) for v in vals)
            out.append("%s.%s = %s;" % (path, k, ("new %s[] { %s }" if ty.endswith("[]") else "new System.Collections.Generic.List<%s> { %s }") % (elem, lits)))
    for key, n in (("vec2_fields", 2), ("vec3_fields", 3)):
        for k, v in sorted((rec.get(key) or {}).items()):
            if k in done:
                continue
            short = (decl.get(k) or "").split(".")[-1]
            if short in ("Vector2", "Vector3"):
                vals = list(v) + [0.0] * (int(short[-1]) - len(v))
                out.append("%s.%s = new %s(%s);" % (path, k, short, ", ".join(_f(x) for x in vals[:int(short[-1])])))
            elif short in ("Vector2Int", "Vector3Int"):
                vals = list(v) + [0.0] * (int(short[-4]) - len(v))
                out.append("%s.%s = new %s(%s);" % (path, k, short, ", ".join(str(int(round(x))) for x in vals[:int(short[-4])])))
    return out


_ELEMS = [0]


def embedded_arrays(path, decl, rec, classes, obj_of, managed, depth=0):
    """An array or List<T> of embedded [Serializable] values (`areaZones`): one object per element, given the serializer's defaults (on a
    managed engine) and then its scene values.  Each element is built in a block of its own, as `__eN`."""
    out = []
    for k, items in sorted((rec.get("struct_arrays") or {}).items()):
        ty = decl.get(k)
        elem = _element_type(ty)
        info = _class_info(classes, elem)
        if elem is None or info.get("kind") not in ("class", "struct") or depth > 10:
            continue
        is_list = not ty.strip().endswith("[]")
        out.append("%s.%s = %s;" % (path, k, "new System.Collections.Generic.List<%s>()" % elem if is_list else "new %s[%d]" % (elem, len(items))))
        for i, item in enumerate(items):
            e = "__e%d" % _ELEMS[0]
            _ELEMS[0] += 1
            # (a class with no parameterless constructor is made as Unity's serializer makes it: no constructor runs)
            make = "new %s()" % elem if info.get("new", True) else "(%s)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(%s))" % (elem, elem)
            body = ["%s %s = %s;" % (elem, e, make)]
            if managed and info.get("kind") == "class":
                body.append("SerializedDefaults.CreateFields(%s);" % e)
            body += nested_values(e, elem, item, classes, obj_of, depth + 1)
            body.append("%s.%s.Add(%s);" % (path, k, e) if is_list else "%s.%s[%d] = %s;" % (path, k, i, e))
            out.append("{ " + " ".join(body) + " }")
    return out


def nested_values(path, ty, sv, classes, obj_of=None, depth=0):
    """Assignments of an embedded [Serializable] value's scene values (`path.k = v;`), for the public fields of class `ty` (and theirs, nested):
    numbers, strings, Vector2 and scene references.  The class is looked up by its full name first (two classes may share a short one)."""
    cls = (ty or "").strip()
    if cls not in classes:
        cls = cls.split(".")[-1]
    if depth > 10 or cls not in classes:
        return []
    decl = declared_fields(classes, cls)
    out = []
    if obj_of is not None:
        out += scene_refs(path, decl, sv, obj_of)
    out += scalar_values(path, decl, sv, classes)
    for k, inner in sorted((sv.get("struct_values") or {}).items()):
        if k in decl:
            out += nested_values("%s.%s" % (path, k), decl[k], inner, classes, obj_of, depth + 1)
    out += embedded_arrays(path, decl, sv, classes, obj_of, obj_of is not None and obj_of.get(":managed"), depth + 1)
    return out


def field_literal(ty, val):
    """The C# literal of a scene value for a field of type `ty`, or None when the engine cannot set it (a reference, a vector ...)."""
    if ty == "float" or ty == "double":
        return _f(val)
    if ty == "int" or ty == "long" or ty == "short" or ty == "byte":
        return str(int(val))
    if ty == "bool":
        return "true" if val else "false"
    return None


# The engine classes a script may derive from that have the MonoBehaviour messages virtual (MonoBehaviour and Unity's UI), and ScriptableObject's.
MB_ROOTS = {"MonoBehaviour", "Behaviour", "UIBehaviour", "Selectable", "Button", "Toggle", "ToggleGroup", "Slider", "Scrollbar", "ScrollRect",
            "InputField", "Graphic", "MaskableGraphic", "Image", "RawImage", "Text", "TMP_Text", "TextMeshProUGUI", "TextMeshPro", "EventSystem",
            "BaseInputModule", "BaseRaycaster", "GraphicRaycaster", "LayoutGroup", "HorizontalOrVerticalLayoutGroup", "HorizontalLayoutGroup",
            "VerticalLayoutGroup", "GridLayoutGroup", "ContentSizeFitter", "LayoutElement", "AspectRatioFitter", "CanvasScaler", "Mask",
            "RectMask2D", "BaseMeshEffect", "Shadow", "Outline"}
SO_MESSAGES = ("Awake", "OnEnable", "OnDisable", "OnDestroy", "OnValidate", "Reset")


def prepare_scripts(texts):
    """{name: text} of Unity scripts as the engine's C#: `[Shared]` is CCSharp's to refuse, and a message (`void Update()`) is `public override` --
    in a class whose ancestry (followed through all the scripts) reaches MonoBehaviour or one of Unity's UI classes, and for ScriptableObject its
    own messages; a method of that name in any other class (a plain `Timer.Start()`) is left alone."""
    bases = {}
    for t in texts.values():
        for m in re.finditer(r"(?<!\w)(?:class|struct)\s+(\w+)(?:\s*<[^>{]*>)?\s*:\s*([\w.]+)", t):
            bases.setdefault(m.group(1), m.group(2).split(".")[-1])

    def root(c):
        seen = set()
        while c in bases and c not in seen:
            seen.add(c)
            c = bases[c]
        return c

    out = {}
    for name, text in texts.items():
        text = re.sub(r"^[ \t]*\[Shared\][ \t]*\r?\n", "", text, flags=re.M)
        scan = _CS_NOISE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
        spans = []                                  # (start, end, class): each class's body, to find the class a method is in
        for m in re.finditer(r"(?<!\w)(?:class|struct)\s+(\w+)[^{;]*\{", scan):
            depth, k = 0, m.end() - 1
            while k < len(scan):
                if scan[k] == "{": depth += 1
                elif scan[k] == "}":
                    depth -= 1
                    if depth == 0: break
                k += 1
            spans.append((m.end(), k, m.group(1)))

        def allowed(at):
            inner = [sp for sp in spans if sp[0] <= at < sp[1]]
            if not inner: return ()
            r = root(max(inner, key=lambda sp: sp[0])[2])
            return MESSAGES if r in MB_ROOTS else SO_MESSAGES if r == "ScriptableObject" else ()

        # `void Update(`, `private void Update(` and `public void Update(` all become `public override void Update(`
        # (`protected override void OnEnable(` -- a script deriving Unity's UI -- is public too: the engine's messages are public).  A base
        # script's `public virtual void Awake(` too: left virtual it opened a slot of its own, so its subclasses' overrides were never called.
        pat = re.compile(r"(?m)^([ \t]*)((?:(?:public|private|protected|internal|virtual|override|new|abstract|sealed)[ \t]+)*)(void[ \t]+(%s)[ \t]*\()"
                         % "|".join(MESSAGES))
        pieces, last = [], 0
        for m in pat.finditer(scan):
            if m.group(4) not in allowed(m.start()):
                continue
            mods = m.group(2).split()
            keep = "".join(k + " " for k in ("abstract", "sealed") if k in mods)
            pieces.append(text[last:m.start()])
            pieces.append(m.group(1) + "public " + keep + "override " + text[m.start(3):m.end()])
            last = m.end()
        pieces.append(text[last:])
        out[name] = "".join(pieces)
    return out


def prepare_script(text):
    """One script (its classes' ancestry read from it alone)."""
    return prepare_scripts({"_": text})["_"]


# comments, strings and directives, blanked before classes and methods are looked for
_CS_NOISE_RE = re.compile(r'//[^\n]*|/\*.*?\*/|@"(?:[^"]|"")*"|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)


def _f(x):
    x = float(x)
    if x != x:
        return "float.NaN"
    if x in (float("inf"), float("-inf")):
        return "float.PositiveInfinity" if x > 0 else "float.NegativeInfinity"
    s = repr(x)
    if "e" in s:
        s = "%.9g" % x
    return s + "f"


def _lit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _f(v)
    return None


def scene_records(objects, hierarchy=()):
    """The scene as emit_groups takes it: the object records, a record for each transform-only GameObject (an empty parent the
    loader keeps in `hierarchy` only), and each baked tilemap tile as a GameObject with a SpriteRenderer."""
    out = [o for o in objects if not o.get("tile")]
    have = {o.get("xf_id") for o in out}
    for h in hierarchy:
        if h.get("xf_id") not in have and h.get("local_pos") is not None:
            out.append(dict(h, local_scale=h.get("local_scale") or (1.0, 1.0, 1.0)))
            have.add(h["xf_id"])
    for i, t in enumerate(o for o in objects if o.get("tile")):
        sp = t["sprite"]
        half = math.atan2(sp.get("m10", 0.0), sp.get("m00", 1.0)) / 2
        out.append({"name": "Tile", "go_id": "tile%d" % i, "active": 1, "layer": sp.get("layer", 0), "sprite": sp,
                    "local_pos": (sp["x"], sp["y"], sp.get("z", 0.0)), "local_rot": (0.0, 0.0, math.sin(half), math.cos(half)),
                    "local_scale": (sp.get("scale_x", 1.0), sp.get("scale_y", 1.0), 1.0)})
    return out


def all_records(objects, prefabs=()):
    """The scene's records and every prefab's: what textures and scripts are collected over."""
    out = list(objects)
    for pf in prefabs:
        out += pf["objects"]
    return out


def textures(objects, prefabs=()):
    """The distinct sprite textures, in first-use order: [(path, w, h, rgba)]; a sprite's spriteId indexes this."""
    out, ids = [], {}
    for o in all_records(objects, prefabs):
        sp = o.get("sprite")
        if sp and sp.get("has_sprite") and sp.get("tex_path") and sp["tex_path"] not in ids:
            ids[sp["tex_path"]] = len(out)
            out.append((sp["tex_path"], sp["tex_w"], sp["tex_h"], sp["tex_rgba"]))
    return out, ids


def groups(objects):
    """[[record, co-script records ...]]: a GameObject with several scripts is one record and a `co_mb` record for each further script."""
    out, by_go = [], {}
    for o in objects:
        if o.get("co_mb") and o.get("go_id") in by_go:
            by_go[o["go_id"]].append(o)
        else:
            by_go[o.get("go_id")] = [o]
            out.append(by_go[o.get("go_id")])
    return out


def _cs_string(x):
    return x.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def prefab_name(pf):
    return "Prefab_%s" % re.sub(r"\W", "_", pf["guid"])


_JOINT_CLASSES = {0: "HingeJoint2D", 1: "DistanceJoint2D", 2: "SpringJoint2D", 3: "FixedJoint2D", 4: "SliderJoint2D", 5: "WheelJoint2D",
                  6: "FrictionJoint2D", 7: "RelativeJoint2D", 8: "TargetJoint2D"}


def _jf(v):
    return _f(v) if v == v and abs(v) < 1e30 else "0f"


def _jb(v):
    return "true" if v else "false"


# (C# field, record key, how to spell it).  Infinite thresholds (an unbreakable joint) are not sent: the engine does not break joints.
_JOINT_FIELDS = (
    ("useLimits", "use_limits", _jb), ("lower", "lower", _jf), ("upper", "upper", _jf),
    ("useMotor", "use_motor", _jb), ("motorSpeed", "motor_speed", _jf), ("maxMotor", "motor_max", _jf),
    ("distance", "distance", _jf), ("autoConfigureDistance", "auto_distance", _jb), ("maxDistanceOnly", "max_distance_only", _jb),
    ("frequency", "frequency", _jf), ("dampingRatio", "damping", _jf),
    ("angle", "angle", _jf), ("autoConfigureAngle", "auto_angle", _jb),
    ("maxForce", "max_force", _jf), ("maxTorque", "max_torque", _jf), ("correctionScale", "correction", _jf),
    ("autoConfigureOffset", "auto_offset", _jb), ("angularOffset", "offset_angle", _jf), ("autoConfigureTarget", "auto_target", _jb),
)


def _material(col, rb):
    """The PhysicsMaterial2D a collider uses: its own, else its Rigidbody2D's, else Unity's defaults (friction 0.4, bounciness 0, Average)."""
    for rec in (col, rb or {}):
        g = rec.get("material_guid")
        if g and g.lower() in MATERIALS:
            m = MATERIALS[g.lower()]
            return {"friction": m["friction"], "bounciness": m["bounciness"],
                    "friction_combine": int(m.get("friction_combine", 0)), "bounce_combine": int(m.get("bounce_combine", 0))}
    return {"friction": 0.4, "bounciness": 0.0, "friction_combine": 0, "bounce_combine": 0}


@functools.lru_cache(maxsize=None)
def script_order(script):
    """The script's Script Execution Order (its .meta `executionOrder`), 0 without one."""
    try:
        with open(script + ".meta", encoding="utf-8", errors="replace") as f:
            m = re.search(r"(?m)^\s*executionOrder:\s*(-?\d+)", f.read())
    except OSError:
        return 0
    return int(m.group(1)) if m else 0


def emit_groups(gs, classes, tex_ids, pfx, ind, pf_var, cameras=(), in_prefab=False):
    """C# that builds the GameObjects of `gs` (a scene's or a prefab's): returns (lines, {xf_id: variable}).  `pf_var` is {guid: "SceneData.pfN"},
    the template a script field that refers to a prefab is given."""
    out = []
    ui = dna_available()        # the uGUI classes are the managed engine's
    var_of = {g[0]["xf_id"]: "%so%d" % (pfx, i) for i, g in enumerate(gs) if g[0].get("xf_id") is not None}
    # every fileID on an object (it, its transform, its components) -> the object's variable: what a reference field is resolved through
    obj_of = {}
    for i, g in enumerate(gs):
        for r in g:
            for fid in [r.get("go_id"), r.get("xf_id")] + list(r.get("comp_ids") or []) + list(r.get("mb_ids") or []):
                if fid is not None:
                    obj_of.setdefault(str(fid), "%so%d" % (pfx, i))
    for guid, var in pf_var.items():        # `fid@guid`: an object of a prefab asset, found in its template
        obj_of["@" + guid] = var
    obj_of[":managed"] = ui                 # (a prefab's component is found by its fileID on a managed engine)
    for i, g in enumerate(gs):              # a script's own MonoBehaviour fileID -> that very script (of two of one type on an object)
        for k, r in enumerate(g):
            if r.get("class") and r.get("script") and r.get("own_mb_id"):
                obj_of["#" + str(r["own_mb_id"])] = "%ss%d_%d" % (pfx, i, k)
    refs = []                   # (script variable, declared fields, record): their scene references, set once every object exists
    tiles_of = {}               # tile asset guid -> the Tile made for it (one each, shared by every cell that shows it)
    for i, g in enumerate(gs):
        o = g[0]
        v = "%so%d" % (pfx, i)
        out.append('GameObject %s = new GameObject("%s");' % (v, _cs_string(o["name"])))
        p, s_ = o["local_pos"], o["local_scale"]
        out.append("%s.transform.localPosition = new Vector3(%s, %s, %s);" % (v, _f(p[0]), _f(p[1]), _f(p[2])))
        out.append("%s.transform.localScale = new Vector3(%s, %s, %s);" % (v, _f(s_[0]), _f(s_[1]), _f(s_[2])))
        q = o.get("local_rot") or (0.0, 0.0, 0.0, 1.0)
        if tuple(q) != (0.0, 0.0, 0.0, 1.0):
            out.append("%s.transform.localRotation = new Quaternion(%s, %s, %s, %s);" % (v, _f(q[0]), _f(q[1]), _f(q[2]), _f(q[3])))
        if not o.get("active", 1):
            out.append("%s.SetActive(false);" % v)
        if o.get("layer"):
            out.append("%s.layer = %d;" % (v, o["layer"]))
        if ui and o.get("rect"):
            out.append("RectTransform %srt%d = %s.MakeRect();" % (pfx, i, v))
        for k, r in enumerate(g):
            if not (r.get("class") and r.get("script")):   # (a scriptless object has a made-up class name)
                continue
            c = "%ss%d_%d" % (pfx, i, k)
            out.append("%s %s = new %s();" % (r["class"], c, r["class"]))
            decl = declared_fields(classes, r["class"])
            out += scalar_values(c, decl, r, classes)
            refs.append((c, decl, r))
            if in_prefab and ui and r.get("own_mb_id"):
                out.append("%s.sourceId = %sL;" % (c, int(r["own_mb_id"])))
            order = script_order(r["script"])
            if order:
                out.append("%s.executionOrder = %d;" % (c, order))
            out.append("%s.Attach(%s);" % (v, c))
        rb = o.get("rigidbody2d")
        if rb:
            b = "%srb%d" % (pfx, i)
            out.append("Rigidbody2D %s = new Rigidbody2D();" % b)
            out.append("%s.bodyType = (RigidbodyType2D)%d;" % (b, int(rb.get("body_type", 0))))
            out.append("%s.mass = %s; %s.gravityScale = %s; %s.drag = %s;" % (
                b, _f(rb.get("mass", 1.0)), b, _f(rb.get("gravity_scale", 1.0)), b, _f(rb.get("linear_damping", 0.0))))
            out.append("%s.velocity = new Vector2(%s, %s);" % (b, _f(rb.get("vel_x", 0.0)), _f(rb.get("vel_y", 0.0))))
            if rb.get("freeze_rot"):
                out.append("%s.freezeRotation = true;" % b)
            if rb.get("ang_vel"):
                out.append("%s.angularVelocity = %s;" % (b, _f(rb["ang_vel"])))
            out.append("%s.Attach(%s);" % (v, b))
        col = o.get("collider2d")
        if col and col.get("kind") in COLLIDER_KINDS:
            cc = "%scol%d" % (pfx, i)
            kind = col["kind"]
            cls = {"box": "BoxCollider2D", "circle": "CircleCollider2D", "polygon": "PolygonCollider2D"}.get(kind, "CapsuleCollider2D")
            out.append("%s %s = new %s();" % (cls, cc, cls))
            out.append("%s.isTrigger = %s;" % (cc, "true" if col.get("is_trigger") else "false"))
            out.append("%s.enabled = %s;" % (cc, "true" if col.get("enabled", True) else "false"))
            mat = _material(col, rb)
            out.append("%s.friction = %s; %s.bounciness = %s;" % (cc, _f(mat["friction"]), cc, _f(mat["bounciness"])))
            if mat["friction_combine"] or mat["bounce_combine"]:
                out.append("%s.frictionCombine = %d; %s.bounceCombine = %d;" % (cc, mat["friction_combine"], cc, mat["bounce_combine"]))
            ox, oy = col.get("offset_x", 0.0), col.get("offset_y", 0.0)
            if kind == "polygon":
                # the outline is cut into convex pieces here; the points carry the collider's offset
                import tools.unity.unity_pack_physics as _ph
                for tri in _ph._triangulate_paths(col.get("paths") or []):
                    for (px, py) in tri:
                        out.append("%s.AddPoint(%s, %s);" % (cc, _f(px + ox), _f(py + oy)))
                    out.append("%s.EndPiece();" % cc)
                for path in col.get("paths") or []:        # the outlines as authored, which points / GetPath give back
                    out += ["%s.AddPathPoint(%s, %s);" % (cc, _f(px), _f(py)) for (px, py) in path]
                    out.append("%s.EndPath(%d);" % (cc, len(path)))
                out.append("%s.pathOffset = new Vector2(%s, %s);" % (cc, _f(ox), _f(oy)))
            else:
                out.append("%s.offset = new Vector2(%s, %s);" % (cc, _f(ox), _f(oy)))
                if kind == "circle":
                    out.append("%s.radius = %s;" % (cc, _f(col.get("radius", 0.5))))
                else:
                    out.append("%s.size = new Vector2(%s, %s);" % (cc, _f(col.get("size_x", 1.0)), _f(col.get("size_y", 1.0))))
                    if kind == "capsule_h":
                        out.append("%s.vertical = false;" % cc)
            out.append("%s.Attach(%s);" % (v, cc))
        sp = o.get("sprite")
        if sp and sp.get("has_sprite") and not (ui and o.get("ui_image")):      # (a UI image is an Image, not a sprite in the world)
            r = "%sr%d" % (pfx, i)
            out.append("SpriteRenderer %s = new SpriteRenderer();" % r)
            out.append("%s.spriteId = %d;" % (r, tex_ids.get(sp.get("tex_path"), -1)))
            sx, sy = (abs(o["local_scale"][0]) or 1.0), (abs(o["local_scale"][1]) or 1.0)     # the record's half size has the object's scale in it
            out.append("%s.halfW = %s; %s.halfH = %s;" % (r, _f(sp.get("half_w", 0.5) / sx), r, _f(sp.get("half_h", 0.5) / sy)))
            out.append("%s.color = new Color(%s, %s, %s, %s);" % (r, _f(sp["r"]), _f(sp["g"]), _f(sp["b"]), _f(sp["a"])))
            out.append("%s.sortingOrder = %d;" % (r, sp.get("sorting_order", 0)))
            out.append("%s.sortingLayer = %d;" % (r, sp.get("sorting_layer", 0) or 0))
            out.append("%s.Attach(%s);" % (v, r))
    rb_of = {}
    for i, g in enumerate(gs):
        rec = g[0].get("rigidbody2d")
        if rec and rec.get("file_id") is not None:
            rb_of[str(rec["file_id"])] = "%srb%d" % (pfx, i)
    for i, g in enumerate(gs):
        for k, jt in enumerate(g[0].get("joints2d") or []):
            cls = _JOINT_CLASSES[jt["kind"]]
            jv = "%sj%d_%d" % (pfx, i, k)
            out.append("%s %s = new %s();" % (cls, jv, cls))
            out.append("%s.enabled = %s;" % (jv, "true" if jt.get("enabled", 1) else "false"))
            if jt.get("connected_fid") is not None and str(jt["connected_fid"]) in rb_of:
                out.append("%s.connectedBody = %s;" % (jv, rb_of[str(jt["connected_fid"])]))
            out.append("%s.enableCollision = %s;" % (jv, "true" if jt.get("collide") else "false"))
            out.append("%s.autoConfigureConnectedAnchor = %s;" % (jv, "true" if jt.get("auto_anchor", 1) else "false"))
            out.append("%s.anchor = new Vector2(%s, %s); %s.connectedAnchor = new Vector2(%s, %s);" % (
                jv, _f(jt["anchor"][0]), _f(jt["anchor"][1]), jv, _f(jt["canchor"][0]), _f(jt["canchor"][1])))
            for fld, key, conv in _JOINT_FIELDS:
                val = jt.get(key)
                if val is None:
                    continue
                out.append("%s.%s = %s;" % (jv, fld, conv(val)))
            lo, tg = jt.get("offset") or (0.0, 0.0), jt.get("target") or (0.0, 0.0)
            out.append("%s.linearOffset = new Vector2(%s, %s); %s.target = new Vector2(%s, %s);" % (jv, _jf(lo[0]), _jf(lo[1]), jv, _jf(tg[0]), _jf(tg[1])))
            out.append("%so%d.Attach(%s);" % (pfx, i, jv))
        if ui:
            out += _ui_components(g[0], "%so%d" % (pfx, i), "%sui%d" % (pfx, i))
            out += _tilemap_components(g[0], "%so%d" % (pfx, i), "%stm%d" % (pfx, i), tiles_of, pfx)
            out += _builtin_components(g[0], "%so%d" % (pfx, i), "%sbi%d" % (pfx, i))
            out += _audio_sources(g[0], "%so%d" % (pfx, i), "%sau%d" % (pfx, i))
            if g[0].get("animator_controller"):
                # ponytail: the Animator without its controller (states and clips are not loaded): Play and speed work, nothing animates.
                # Upgrade path: the controller asset's states and the clips' curves (the packed path's _build_animation_tables).
                out += ["Animator %sanim%d = new Animator();" % (pfx, i), "%so%d.Attach(%sanim%d);" % (pfx, i, pfx, i)]
    for i, g in enumerate(gs):
        if g[0].get("father_id") in var_of:
            out.append("%so%d.transform.SetParent(%s.transform, false);" % (pfx, i, var_of[g[0]["father_id"]]))
    out += _cameras(cameras, obj_of, var_of, pfx)      # before the references: a script's `camera` field is one of them
    for c, decl, r in refs:                 # every object exists now: a reference may point at one made later
        out += scene_refs(c, decl, r, obj_of)
        # after Attach, which gave the [Serializable] fields their objects (SerializedDefaults.CreateFields): their scene values go in
        for fname, sv in sorted((r.get("struct_values") or {}).items()):
            if fname in decl:
                out += nested_values("%s.%s" % (c, fname), decl[fname], sv, classes, obj_of)
        out += embedded_arrays(c, decl, r, classes, obj_of, ui)
    for i, g in enumerate(gs):              # after the parenting: an anchored position is measured from the parent's rectangle
        rect = g[0].get("rect") if ui else None
        if rect:
            rt = "%srt%d" % (pfx, i)
            for prop, key, dflt in (("anchorMin", "anchor_min", (0.5, 0.5)), ("anchorMax", "anchor_max", (0.5, 0.5)), ("pivot", "pivot", (0.5, 0.5)),
                                    ("sizeDelta", "size_delta", (0.0, 0.0)), ("anchoredPosition", "anchored_position", (0.0, 0.0))):
                x, y = rect.get(key) or dflt
                out.append("%s.%s = new Vector2(%s, %s);" % (rt, prop, _f(x), _f(y)))
    return [ind + l for l in out], var_of


def _cameras(cameras, obj_of, var_of, pfx):
    """The scene's cameras: each on its own GameObject when the scene made one (it has scripts, or children), else on a new one under its parent.
    Camera.main is the first camera tagged MainCamera (Engine.mainCamera is otherwise the last one attached)."""
    out, main = [], None
    for j, c in enumerate(cameras):
        cv = "%scam%d" % (pfx, j)
        go = obj_of.get(str(c.get("cam_file_id")))
        if go is None:
            go = "%scgo%d" % (pfx, j)
            p = c["local_pos"]
            out.append('GameObject %s = new GameObject("%s");' % (go, _cs_string(c["name"])))
            out.append("%s.transform.localPosition = new Vector3(%s, %s, %s);" % (go, _f(p[0]), _f(p[1]), _f(p[2])))
            if c.get("father_id") in var_of:
                out.append("%s.transform.SetParent(%s.transform, false);" % (go, var_of[c["father_id"]]))
        out.append("Camera %s = new Camera();" % cv)
        out.append("%s.orthographicSize = %s;" % (cv, _f(c.get("orthographic_size", 5.0))))
        out.append("%s.background = new Color(%s, %s, %s, 1f);" % (cv, _f(c.get("bg_r", 0)), _f(c.get("bg_g", 0)), _f(c.get("bg_b", 0))))
        out.append("%s.Attach(%s);" % (go, cv))
        if main is None and c.get("main"):
            main = cv
    if main is not None:
        out.append("Engine.mainCamera = %s;" % main)
    return out


def _color(c):
    return "new Color(%s, %s, %s, %s)" % (_f(c.get("r", 1.0)), _f(c.get("g", 1.0)), _f(c.get("b", 1.0)), _f(c.get("a", 1.0)))


def _tilemap_components(o, v, c, tiles_of, pfx):
    """A Grid and a Tilemap with its authored cells, for scripts to ask (cellBounds, GetTile, WorldToCell ...); the baked tile sprites are what
    is drawn.  ponytail: a Tile has no sprite (tile assets are not loaded): a script reading tile.sprite gets null."""
    out = []
    gr = o.get("grid")
    if gr:
        (cw, ch), (gw, gh) = gr["cell"], gr["gap"]
        out += ["Grid %sg = new Grid();" % c, "%sg.SetCellSize(new Vector3(%s, %s, 0f)); %sg.SetCellGap(new Vector3(%s, %s, 0f));" % (
            c, _f(cw), _f(ch), c, _f(gw), _f(gh)), "%s.Attach(%sg);" % (v, c)]
    tm = o.get("tilemap")
    if tm:
        ax, ay = tm["anchor"]
        out += ["UnityEngine.Tilemaps.Tilemap %st = new UnityEngine.Tilemaps.Tilemap();" % c, "%st.tileAnchor = new Vector3(%s, %s, 0f);" % (c, _f(ax), _f(ay)),
                "%s.Attach(%st);" % (v, c)]
        for x, y, guid in tm["cells"]:
            if guid not in tiles_of:
                tiles_of[guid] = "%stile%d" % (pfx, len(tiles_of))
                out.append("UnityEngine.Tilemaps.Tile %s = ScriptableObject.CreateInstance<UnityEngine.Tilemaps.Tile>();" % tiles_of[guid])
            out.append("%st.SetTile(new Vector3Int(%d, %d, 0), %s);" % (c, x, y, tiles_of[guid]))
    return out


# Unity's own MonoBehaviours the loader makes in their own way (or never: the record's other keys stand for them)
_BUILTIN_DONE = {"Image", "Button", "TextMeshProUGUI", "TextMeshPro", "Canvas", "Text", "Slider", "Toggle", "Scrollbar", "ScrollRect", "RawImage"}


@functools.lru_cache(maxsize=None)
def engine_classes():
    """{class name: full name} of the managed engine's classes (unity-engine-managed), from their sources: the namespace a class is declared in."""
    found = {}
    d = os.path.join(ccsharp_home(), "unity-engine-managed")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        if not f.endswith(".cs"):
            continue
        with open(os.path.join(d, f), encoding="utf-8-sig") as fh:
            text = fh.read()
        spaces = [(m.start(), m.group(1)) for m in re.finditer(r"(?m)^namespace\s+([\w.]+)", text)]
        for m in re.finditer(r"(?m)^\s*public\s+(?:sealed\s+|partial\s+)?class\s+(\w+)", text):
            ns = [n for at, n in spaces if at < m.start()]
            found.setdefault(m.group(1), (ns[-1] + "." if ns else "") + m.group(1))
    return found


def _builtin_components(o, v, c):
    """The Unity package MonoBehaviours on the object (EventSystem, an input module, a raycaster, a layout group ...) that the managed engine
    has a class of: made, so that a script's GetComponent or reference finds them.  Their settings are not read (the engine's defaults)."""
    out = []
    known = engine_classes()
    for k, (name, _fid, enabled) in enumerate(o.get("builtin_mbs") or ()):
        full = known.get(name)
        if full is None or name in _BUILTIN_DONE:
            continue
        out.append("%s %s%d = new %s();" % (full, c, k, full))
        if not enabled:
            out.append("%s%d.enabled = false;" % (c, k))
        out.append("%s.Attach(%s%d);" % (v, c, k))
    return out


def _audio_sources(o, v, c):
    """The object's AudioSources with their settings.  ponytail: no clip (audio assets are not loaded, and the host has no audio yet): a script
    that plays one plays silence.  Upgrade path: the clips decoded into the host's sounds (AudioClip.FromHost)."""
    out = []
    for k, a in enumerate(o.get("audiosources") or ()):
        a_ = "%s%d" % (c, k)
        out += ["AudioSource %s = new AudioSource();" % a_,
                "%s.playOnAwake = %s; %s.loop = %s; %s.mute = %s;" % (a_, "true" if a.get("play_on_awake", 1) else "false", a_, "true" if a.get("loop") else "false",
                                                                   a_, "true" if a.get("mute") else "false"),
                "%s.volume = %s; %s.pitch = %s;" % (a_, _f(a.get("volume", 1.0)), a_, _f(a.get("pitch", 1.0)))]
        if not a.get("enabled", 1):
            out.append("%s.enabled = false;" % a_)
        out.append("%s.Attach(%s);" % (v, a_))
    return out


def _ui_components(o, v, c):
    """The uGUI components of record `o`, on GameObject `v` (variables named from `c`).
    ponytail: geometry, colours, text and switches only: an Image has no sprite yet and a Button no onClick listeners (the packer does not
    load UI sprites or persistent calls for the engine); the upgrade path is the packed path's _bake_ui_images and its onClick table."""
    out = []
    cv = o.get("canvas")
    if cv:
        out += ["Canvas %sc = new Canvas();" % c, "%sc.renderMode = (RenderMode)%d;" % (c, cv.get("render_mode", 0)),
                "%sc.planeDistance = %s; %sc.sortingOrder = %d; %sc.overrideSorting = %s;" % (
                    c, _f(cv.get("plane_distance", 100.0)), c, cv.get("sorting_order", 0), c, "true" if cv.get("override_sorting") else "false")]
        if not cv.get("enabled", 1):
            out.append("%sc.enabled = false;" % c)
        out.append("%s.Attach(%sc);" % (v, c))
    im = o.get("ui_image")
    if im:
        out += ["UnityEngine.UI.Image %si = new UnityEngine.UI.Image();" % c, "%si.color = %s;" % (c, _color(im)),
                "%si.type = (UnityEngine.UI.Image.Type)%d; %si.preserveAspect = %s; %si.pixelsPerUnitMultiplier = %s;" % (
                    c, im.get("image_type", 0), c, "true" if im.get("preserve_aspect") else "false", c, _f(im.get("pixels_per_unit_multiplier", 1.0)))]
        if not im.get("enabled", 1):
            out.append("%si.enabled = false;" % c)
        out.append("%s.Attach(%si);" % (v, c))
    tmp = o.get("ui_tmp")
    if tmp:
        out += ["TMPro.TextMeshProUGUI %st = new TMPro.TextMeshProUGUI();" % c, '%st.text = "%s";' % (c, _cs_string(tmp.get("text") or "")),
                "%st.fontSize = %s; %st.color = %s;" % (c, _f(tmp.get("font_size", 36.0)), c, _color(tmp)), "%s.Attach(%st);" % (v, c)]
    bt = o.get("ui_button")
    if bt:
        out += ["UnityEngine.UI.Button %sb = new UnityEngine.UI.Button();" % c,
                "%sb.interactable = %s;" % (c, "false" if bt.get("interactable", 1) in (0, False) else "true")]
        if bt.get("enabled", 1) in (0, False):
            out.append("%sb.enabled = false;" % c)
        out.append("%s.Attach(%sb);" % (v, c))
    return out


def scene_source(objects, cameras, classes=None, prefabs=(), project=None):
    """SceneData.cs: the scene as code, and a Prefab subclass for each prefab.  `classes` is describe()'s account of the scripts: a scene field is set
    on the script that declares it.  `project` ({"company", "product", "scenes"}) is told to Application and SceneManager when the scripts use them
    (setting them otherwise would bring in the managed half of the engine for nothing)."""
    classes = classes or {}
    CLIPS.clear()
    tex_ids = textures(objects, prefabs)[1]
    pf_var = {pf["guid"]: "SceneData.pf%d" % i for i, pf in enumerate(prefabs)}
    out = ["using UnityEngine;"] + ["using %s;" % ns for ns in (project or {}).get("namespaces", ())] + [""]
    for pf in prefabs:                      # a prefab is code that builds it; Instantiate(template) calls it
        gs = groups(pf["objects"])
        lines, var_of = emit_groups(gs, classes, tex_ids, "", "        ", pf_var, in_prefab=True)
        roots = [i for i, g in enumerate(gs) if g[0].get("father_id") not in var_of]
        out += ["class %s : Prefab" % prefab_name(pf), "{", "    public override GameObject Create()", "    {"] + lines
        out += ["        return o%d;" % (roots[0] if roots else 0), "    }", "}", ""]
    out += ["static class SceneData", "{"]
    for i, pf in enumerate(prefabs):
        out.append("    public static GameObject pf%d;" % i)
    # Load builds the prefab templates once and the scene; Build is the scene alone, which SceneManager.LoadScene runs again (through SceneLoad)
    out += ["    public static void Load()", "    {", "        MakeClips();", "        Engine.sceneLoader = new SceneLoad();"]
    if project and project.get("managed_api"):
        out += ['        Application.companyName = "%s";' % _cs_string(project.get("company") or "DefaultCompany"),
                '        Application.productName = "%s";' % _cs_string(project.get("product") or "Player"),
                "        UnityEngine.SceneManagement.SceneManager.sceneNames = new string[] { %s };"
                % ", ".join('"%s"' % _cs_string(n) for n in (project.get("scenes") or [""]))]
    matrix = (project or {}).get("layer_matrix")
    if matrix and any(m != 0xFFFFFFFF for m in matrix):
        out += ["        Physics2D.layerMasks.Add(%d);" % (m - (1 << 32) if m >= 1 << 31 else m) for m in matrix]
    names = (project or {}).get("layer_names")
    if names and project.get("layer_api"):
        out.append("        LayerMask.layerNames = new string[] { %s };" % ", ".join('"%s"' % _cs_string(names.get(k, "")) for k in range(32)))
    # templates first: a script field may refer to one, and read its fields as Unity's scripts do (`bulletPrefab.moveSpeed`).  Each is built once,
    # inactive and out of the scene (Engine.Forget); Instantiate builds another.  Last found first: a prefab is found through the ones that refer to it.
    # ponytail: two prefabs that refer to each other -- the first built sees the other's template still null.
    for i in reversed(range(len(prefabs))):
        out += ["        pf%d = new %s().Create();" % (i, prefab_name(prefabs[i])), "        pf%d.SetActive(false);" % i,
                "        pf%d.prefab = new %s();" % (i, prefab_name(prefabs[i])), "        Engine.Forget(pf%d);" % i]
    out += ["        Build();", "    }", "", "    public static void Build()", "    {",
            "        Engine.loadingScene = true;          // (the scene's objects get Unity's negative instance IDs)"]
    lines, var_of = emit_groups(groups(objects), classes, tex_ids, "", "        ", pf_var, cameras)
    out += lines + ["        Engine.loadingScene = false;"]
    out += ["    }", ""]
    # the AudioClips the scene refers to, made once (CLIPS was filled while the objects above were written)
    out += ["    public static AudioClip %s;" % v for v, _n, _s, _r, _c in CLIPS.values()]
    out += ["    static void MakeClips()", "    {"] + ['        %s = AudioClip.FromHost("%s", -1, %s, %d, %d);' % (v, _cs_string(n), _f(secs), rate, ch)
                                                      for v, n, secs, rate, ch in CLIPS.values()] + ["    }"]
    out += ["}", "", "class SceneLoad : SceneLoader", "{", "    public override void Load(int buildIndex) { SceneData.Build(); }", "}", ""]
    return "\n".join(out)


def collect_prefabs(root, objects, assets):
    """The prefabs the scene's scripts refer to (and those they refer to): [{"guid", "name", "objects"}], records as the scene's are.  The prefab files are
    read by unity_pack's own parser."""
    import tools.unity.unity_pack as up
    guids = up._guid_map(root, asset_guids=assets)
    found, order = {}, []

    def ids(v):
        # the references of a record or of an embedded value: single ones, arrays, and those of the values nested in it
        for ref in (v.get("object_refs") or {}).values():
            yield str(ref)
        for arr in (v.get("object_ref_arrays") or {}).values():
            for ref in arr:
                yield str(ref)
        for inner in (v.get("struct_values") or {}).values():
            for ref in ids(inner):
                yield ref

    def refs_of(recs):
        for o in recs:
            for ref in ids(o):
                g = ref.split("@")[-1]
                path = assets.get(g)
                if "@" in ref and path and path.endswith(".prefab") and g not in found:
                    yield g, path

    pending = list(refs_of(objects))
    while pending:
        g, path = pending.pop(0)
        if g in found:
            continue
        # with its own PrefabInstances expanded, as a scene's are (a prefab built from another one: a variant, a nested prefab)
        parsed = up.parse_unity_yaml(up._expand_unstripped_prefab_instances(up._read(path), assets), guid_to_script=guids, asset_guids=assets)
        recs = scene_records([dict(r) for r in parsed[0]], parsed[3])
        up._attach_sprite_textures(recs, assets)
        found[g] = {"guid": g, "name": os.path.splitext(os.path.basename(path))[0], "objects": recs}
        order.append(g)
        pending += list(refs_of(recs))
    return [found[g] for g in order]


MAIN_SOURCE = """using System;
using UnityEngine;

class Program
{
    static int Main()
    {
        SceneData.Load();
        Time.deltaTime = 0.0166667f;
        int frames = %d;
        for (int i = 0; i < frames; i++) Engine.Tick();
        Console.WriteLine("ticks=" + frames + " draws=" + Engine.CountDraws());
        return 0;
    }
}
"""


def ilspycmd():
    """ILSpy's command line (decompiles a managed plugin DLL to C#): CCSharp's build/tools (`dotnet tool install ilspycmd --tool-path
    build/tools`), else on the PATH, else None."""
    local = os.path.join(ccsharp_home(), "build", "tools", "ilspycmd")
    return local if os.path.exists(local) else shutil.which("ilspycmd")


def unclash(text):
    """An obfuscated assembly decompiled: a private field may share its name with a method of its class (`private string a;` beside
    `private void a(..)`), which IL allows and C# does not.  The field is renamed (`a__f`), its uses being the name not followed by `(`, within
    its class -- a private member is not seen outside it."""
    decl = re.compile(r"(?m)^[ \t]*(?:private|internal)[ \t]+(?:static[ \t]+|readonly[ \t]+)*[\w<>\[\],. ]+?[ \t]+(\w+)[ \t]*(?:;|=)")
    for m in list(re.finditer(r"(?<!\w)(?:class|struct)\s+\w+[^{;]*\{", text))[::-1]:
        depth, k = 0, m.end() - 1
        while k < len(text):
            if text[k] == "{": depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0: break
            k += 1
        body = text[m.end():k]
        fields = set(decl.findall(body))
        methods = set(re.findall(r"(?m)^[ \t]*(?:(?:public|private|protected|internal|static|virtual|override|abstract|new)[ \t]+)*[\w<>\[\],. ]+?[ \t]+(\w+)[ \t]*\(", body))
        for name in fields & methods:
            body = re.sub(r"(?<![\w])%s\b(?!\s*[(<])" % re.escape(name), name + "__f", body)
        text = text[:m.end()] + body + text[k:]
    return text


_OBFUSCATED = re.compile(r"Dotfuscator|ObfuscatedByAttribute|SmartAssembly|ConfusedByAttribute|BabelObfuscator")


def surface_only(text):
    """The non-private surface of decompiled C#: types and their non-private members, constructors with empty bodies and every other body
    throwing NotSupportedException; private members dropped.  For an obfuscated plugin, whose private members IL lets share names and differ only
    by return type -- not C# -- and whose internals a game reaches only through its editor-time tools (2DColliderGen's outline tracer)."""
    THROW = '{ throw new System.NotSupportedException("the internals of an obfuscated plugin are not compiled"); }'

    def end_of(s, i, open_ch, close_ch):
        depth = 0
        while i < len(s):
            if s[i] == open_ch: depth += 1
            elif s[i] == close_ch:
                depth -= 1
                if depth == 0: return i
            elif s[i] == '"':
                i += 1
                while i < len(s) and s[i] != '"': i += 2 if s[i] == "\\\\" else 1
            i += 1
        return len(s) - 1

    def members(body, kind):
        out, i = [], 0
        while i < len(body):
            j = i
            while j < len(body) and body[j] not in "{;=":
                if body[j] == "(": j = end_of(body, j, "(", ")")
                elif body[j] == "[": j = end_of(body, j, "[", "]")
                j += 1
            if j >= len(body):
                break
            header = body[i:j]
            if body[j] == "=" and body[j:j + 2] == "=>":           # expression body: up to its `;`
                k = j
                while body[k] != ";":
                    k = end_of(body, k, "(", ")") if body[k] == "(" else end_of(body, k, "{", "}") if body[k] == "{" else k
                    k += 1
                out.append(member(header, "=>", "", kind))
                i = k + 1
            elif body[j] == "=":                                     # a field with an initialiser
                k = j
                while body[k] != ";":
                    k = end_of(body, k, "(", ")") if body[k] == "(" else end_of(body, k, "{", "}") if body[k] == "{" else k
                    k += 1
                out.append(member(header, "=", body[j:k], kind))
                i = k + 1
            elif body[j] == ";":
                out.append(member(header, ";", "", kind))
                i = j + 1
            else:
                k = end_of(body, j, "{", "}")
                out.append(member(header, "{", body[j + 1:k], kind))
                i = k + 1
        return "".join(m for m in out if m)

    def member(header, how, rest, kind):
        h = header.strip()
        words = re.sub(r"\[[^\]]*\]", " ", h).split()
        if not words:
            return ""
        is_type = any(w in ("class", "struct", "interface", "enum") for w in words)
        private = not any(w in ("public", "protected", "internal") for w in words) and kind != "interface"
        if private and not (is_type and kind == "top"):
            return ""
        if is_type:
            if "enum" in words:
                return "\n" + h + " {" + rest + "}\n"
            sub = "interface" if "interface" in words else "type"
            return "\n" + h + " {" + members(rest, sub) + "\n}\n"
        if kind == "interface" or how == ";":
            return "\n\t" + h + ";"
        if how == "=":
            return "\n\t" + h + (rest + ";" if "const" in words else ";")
        name_part = re.sub(r"\([^)]*\)", "", h)
        if "(" in h and how in ("{", "=>"):                        # a method, constructor or operator
            # a constructor is its name alone before `(`, once the modifiers are gone
            before = re.sub(r"\[[^\]]*\]", " ", h[:h.index("(")]).split()
            ctor = len([w for w in before if w not in ("public", "protected", "internal", "static", "extern", "unsafe", "new")]) == 1
            sig = re.sub(r"\)\s*:\s*(?:base|this)\s*\(.*$", ")", h, flags=re.S)
            return "\n\t" + sig + (" { }" if ctor else " " + THROW)
        accessors = [a for a in ("get", "set") if how == "{" and re.search(r"\b%s\b" % a, rest)] or ["get"]
        return "\n\t" + name_part + " { " + " ".join("%s %s" % (a, THROW) for a in accessors) + " }"

    usings = "".join(l + "\n" for l in text.splitlines() if l.startswith("using "))
    body = "\n".join(l for l in text.splitlines() if not l.startswith("using "))
    return usings + members(body, "top") + "\n"


def plugin_sources(dlls, outdir):
    """C# for the project's managed plugin DLLs (a Unity project may ship code compiled, as 2DColliderGen ships its outline tracer): each one
    decompiled once into outdir/plugins (again when the DLL changes), its `[assembly: ..]` attributes dropped.  A DLL that is not managed (a native
    plugin) or that cannot be decompiled is left out, with a note."""
    tool = ilspycmd()
    out = []
    if not dlls:
        return out
    if tool is None:
        sys.stderr.write("unity_pack: %d plugin DLL(s) left out: ilspycmd is not installed (dotnet tool install ilspycmd --tool-path %s)\n"
                         % (len(dlls), os.path.join(ccsharp_home(), "build", "tools")))
        return out
    cache = os.path.join(outdir, "plugins")
    os.makedirs(cache, exist_ok=True)
    for dll in dlls:
        name = os.path.splitext(os.path.basename(dll))[0]
        cs = os.path.join(cache, name + ".cs")
        if not os.path.exists(cs) or os.path.getmtime(cs) < os.path.getmtime(dll):
            work = os.path.join(cache, name + ".tmp")
            shutil.rmtree(work, ignore_errors=True)
            p = subprocess.run([tool, dll, "-o", work, "-lv", "CSharp7_3"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            made = [os.path.join(work, f) for f in os.listdir(work)] if os.path.isdir(work) else []
            if p.returncode != 0 or not made:
                sys.stderr.write("unity_pack: plugin %s left out (not a managed assembly?)\n" % os.path.basename(dll))
                shutil.rmtree(work, ignore_errors=True)
                continue
            with open(made[0], encoding="utf-8-sig") as f:
                text = re.sub(r"(?m)^\[assembly:.*\]\s*$", "", f.read())
            # ponytail: an obfuscated plugin compiles as its public surface only (its bodies throw); a game that calls into it at run time needs
            # the binary loaded instead -- DotNetAnywhere with UnityEngine and netstandard redirected to the engine and its corlib.
            text = surface_only(text) if _OBFUSCATED.search(text) else unclash(text)
            with open(cs, "w", encoding="utf-8") as f:
                f.write(text)
            shutil.rmtree(work, ignore_errors=True)
        out.append(cs)
    return out


def reachable_scripts(attached, candidates):
    """The attached scripts and every candidate script that defines a type they (transitively) name, in that order."""
    from tools.unity.unity_pack_common import _CS_NOISE
    scan = {}
    for p in list(attached) + list(candidates):
        if p not in scan:
            with open(p, encoding="utf-8-sig") as f:
                scan[p] = _CS_NOISE.sub(" ", f.read())
    defines = {}
    for p in candidates:
        for n in re.findall(r"(?<!\w)(?:class|struct|interface|enum)\s+(\w+)", scan[p]):
            defines.setdefault(n, []).append(p)
            if n.endswith("Attribute"):                 # `[MaxInstances]` names MaxInstancesAttribute
                defines.setdefault(n[:-len("Attribute")], []).append(p)
    # ponytail: a type is "named" when its identifier appears anywhere in the file, so a local or member that shares
    # a type's name pulls that script in too; upgrade path is CCSharp's --describe listing the types each file uses.
    out, seen = [], set()
    # extension methods are called without naming their class (`items.Remove(x)`), so a file that declares any is always in
    todo = list(attached) + [p for p in candidates if re.search(r"\(\s*this\s+[\w.<>\[\], ]+\s+\w+", scan[p])]
    while todo:
        p = todo.pop(0)
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        for w in set(re.findall(r"\b[A-Za-z_]\w*\b", scan[p])):
            todo += [q for q in defines.get(w, ()) if q not in seen]
    return out


def write_project(objects, cameras, outdir, frames=60, prefabs=(), scripts=(), project=None):
    """Write the C# project to outdir/cs; returns its folder.  `scripts`: the project's other scripts (the helpers, base classes
    and singletons the attached ones use)."""
    cs = os.path.join(outdir, "cs")
    shutil.rmtree(cs, ignore_errors=True)
    os.makedirs(cs)
    texts = {}
    for sp in reachable_scripts([o.get("script") for o in all_records(objects, prefabs) if o.get("script")], scripts):
        if os.path.basename(sp) not in texts:
            with open(sp, encoding="utf-8-sig") as f:
                texts[os.path.basename(sp)] = f.read()
    texts = prepare_scripts(texts)
    for name, text in texts.items():
        with open(os.path.join(cs, name), "w") as f:
            f.write(text)
    classes = describe(texts) if texts else {}
    project = dict(project or {})
    project["managed_api"] = any(re.search(r"\b(?:Application|SceneManager|PlayerPrefs)\b", t) for t in texts.values())
    project["layer_api"] = any(re.search(r"\bLayerMask\b", t) for t in texts.values())
    ASSET_PATHS.clear()
    ASSET_PATHS.update(project.get("assets") or {})
    # ponytail: every top-level namespace the scripts declare is opened, so the scene names their classes as they do; two classes of one name
    # in two of them would be ambiguous, and a class in a nested namespace is not reached.  Upgrade path: describe() reporting each class's namespace, and qualified names here.
    project["namespaces"] = sorted({ns for t in texts.values() for ns in re.findall(r"(?m)^namespace\s+([\w.]+)", t)})
    with open(os.path.join(cs, "SceneData.cs"), "w") as f:
        f.write(scene_source(objects, cameras, classes, prefabs, project))
    with open(os.path.join(cs, "Program.cs"), "w") as f:
        f.write(MAIN_SOURCE % frames)
    return cs


def build(objects, cameras, outdir, run=False, prefabs=(), materials=None, scripts=(), project=None):
    """Lower and build the project; returns the ccs2c exit code."""
    MATERIALS.clear()
    MATERIALS.update(materials or {})
    cs = write_project(objects, cameras, outdir, prefabs=prefabs, scripts=scripts, project=project)
    physics = physics_for(objects, prefabs)
    cmd = [sys.executable, os.path.join(ccsharp_home(), "crust", "ccs2c.py"), cs, "--unity-engine", "--physics=" + physics, "--name=game",
           "--convert=" + os.path.join(outdir, "cpp"), "--c", "-o", os.path.join(outdir, "game.c"),
           "--exe=" + os.path.join(outdir, "game")]
    if run:
        cmd.append("--run")
    if dna_available():
        cmd.append("--dna")     # a script outside the Crust subset runs on DotNetAnywhere; a project inside it stays all native
    rc = subprocess.call(cmd)
    if rc == 0:
        write_host_files(objects, cameras, outdir, prefabs)
        if physics == "box2d":
            write_physics_files(outdir)
    return rc


def physics_for(objects, prefabs=()):
    """"box2d" (Box2D-Packed under the engine's Physics2D) when a record has a Rigidbody2D or a collider, else "managed" (nothing to link).
    $CCS_PHYSICS=managed|box2d overrides."""
    forced = os.environ.get("CCS_PHYSICS")
    if forced:
        return forced
    for o in all_records(objects, prefabs):
        if o.get("rigidbody2d") or o.get("collider2d"):
            return "box2d"
    return "managed"


def write_physics_files(outdir):
    """What a host links with when the program uses Box2D-Packed: outdir/physics/{pb2.c, pb2.h, libbox2d.a, include/}, and physics/link.txt with the
    compiler arguments (relative to outdir)."""
    import shutil
    sys.path.insert(0, os.path.join(ccsharp_home(), "crust"))
    import ccs2c
    args = ccs2c.physics_link("box2d")                  # ["-I", shim, "-I", include, pb2.c, libbox2d.a, "-lpthread"]
    dest = os.path.join(outdir, "physics")
    os.makedirs(dest, exist_ok=True)
    shim, inc, src, lib = args[1], args[3], args[4], args[5]
    for f in (src, os.path.join(shim, "pb2.h"), lib):
        shutil.copy(f, dest)
    shutil.rmtree(os.path.join(dest, "include"), ignore_errors=True)
    shutil.copytree(inc, os.path.join(dest, "include"))
    with open(os.path.join(dest, "link.txt"), "w") as f:
        f.write("-I physics -I physics/include physics/pb2.c physics/libbox2d.a -lpthread -lm\n")


# The host's keys (examples/unity_pack/gles*_window.c UNITY_KEYS) and their Input System Key numbers.
HOST_KEYS = ([(chr(c), 15 + c - ord("a")) for c in range(ord("a"), ord("z") + 1)] + [("digit0", 50)]
             + [("digit%d" % d, 40 + d) for d in range(1, 10)]
             + [("space", 1), ("enter", 2), ("tab", 3), ("escape", 60), ("backspace", 65), ("leftShift", 51), ("rightShift", 52),
                ("leftAlt", 53), ("rightAlt", 54), ("leftCtrl", 55), ("rightCtrl", 56), ("leftArrow", 61), ("rightArrow", 62),
                ("upArrow", 63), ("downArrow", 64)])


def host_shim(objects, cameras, prefabs=()):
    """C appended to the lowered program: the engine_* functions a host (examples/unity_pack/gles*_window.c) calls, over the engine's
    Host* functions.  The program's own `main` is renamed `game_main`, so the host's `main` is the one that links."""
    texs = textures(objects, prefabs)[0]
    c = ["", "/* ---- the unity_pack host interface over the CCSharp engine ---- */", '#include "engine_draw.h"', ""]
    for i, (_p, w, h, rgba) in enumerate(texs):
        c.append("static const unsigned char _tex%d[] = {%s};" % (i, ",".join(str(b) for b in rgba)))
    c.append("static const int _tex_w[] = {%s}, _tex_h[] = {%s};" % (
        ",".join(str(t[1]) for t in texs) or "0", ",".join(str(t[2]) for t in texs) or "0"))
    c.append("static const unsigned char *const _tex_px[] = {%s};" % (",".join("_tex%d" % i for i in range(len(texs))) or "0"))
    c += [
        "int engine_texture_count(void) { return %d; }" % len(texs),
        "int engine_texture_width(int id) { return _tex_w[id]; }",
        "int engine_texture_height(int id) { return _tex_h[id]; }",
        "const unsigned char *engine_texture_rgba(int id) { return _tex_px[id]; }",
        "float Time_deltaTime; /* the host sets it each frame */",
        "extern int Screen_width, Screen_height; /* (defined below; the host may resize them) */",
        "extern float engine_pointer_x, engine_pointer_y; extern int engine_pointer_down;",
    ]
    # the host's input globals (examples/unity_pack/gles*_window.c declares them weak, these win), read into the engine's InputState each frame
    c += ["int engine_keyboard_connected, engine_pointer_buttons, engine_gamepad_connected, engine_gamepad_button[15];",
          "float engine_scroll_x, engine_scroll_y, engine_gamepad_axis[6];",
          "int %s;" % ", ".join("engine_keyboard_" + n for n, _k in HOST_KEYS)]
    c += [
        "static int _loaded;",
        "void engine_tick(void) {",
        "    if (!_loaded) { _loaded = 1; SceneData_Load(); }",
        "    UnityEngine_Engine_HostSetDeltaTime(Time_deltaTime > 0 ? Time_deltaTime : 1.0f / 60.0f);",
        "    UnityEngine_Engine_HostSetScreen(Screen_width, Screen_height);",
        "    UnityEngine_InputState_HostSetKeyboard(engine_keyboard_connected);",
    ] + ["    UnityEngine_InputState_HostSetKey(%d, engine_keyboard_%s);" % (k, n) for n, k in HOST_KEYS] + [
        "    UnityEngine_InputState_HostSetPointer(engine_pointer_x, engine_pointer_y, engine_pointer_buttons | engine_pointer_down);",
        "    UnityEngine_InputState_HostAddScroll(engine_scroll_x, engine_scroll_y);",
        "    engine_scroll_x = 0; engine_scroll_y = 0;",
        "    UnityEngine_InputState_HostSetPad(0, engine_gamepad_connected);",
        "    for (int b = 0; b < 15; b++) UnityEngine_InputState_HostSetPadButton(0, b, engine_gamepad_button[b]);",
        "    for (int a = 0; a < 6; a++) UnityEngine_InputState_HostSetPadAxis(0, a, engine_gamepad_axis[a]);",
        "    UnityEngine_Engine_Tick();",
        "}",
        "int engine_class_count(void) { return 0; }",
        "int engine_collect_draws(EngineDraw *out, int max) {",
        "    if (!_loaded) { _loaded = 1; SceneData_Load(); }",
        "    int n = UnityEngine_Engine_HostCollect();",
        "    if (n > max) n = max;",
        "    for (int i = 0; i < n; i++) {",
        "        EngineDraw *d = &out[i];",
        "        d->x = UnityEngine_Engine_HostDrawF(i, 0); d->y = UnityEngine_Engine_HostDrawF(i, 1);",
        "        d->half_w = UnityEngine_Engine_HostDrawF(i, 2); d->half_h = UnityEngine_Engine_HostDrawF(i, 3);",
        "        d->m00 = UnityEngine_Engine_HostDrawF(i, 9); d->m01 = UnityEngine_Engine_HostDrawF(i, 10);",
        "        d->m10 = UnityEngine_Engine_HostDrawF(i, 11); d->m11 = UnityEngine_Engine_HostDrawF(i, 12);",
        "        d->r = UnityEngine_Engine_HostDrawF(i, 4); d->g = UnityEngine_Engine_HostDrawF(i, 5);",
        "        d->b = UnityEngine_Engine_HostDrawF(i, 6); d->a = UnityEngine_Engine_HostDrawF(i, 7);",
        "        d->tex = UnityEngine_Engine_HostDrawI(i, 0); d->sorting_layer = UnityEngine_Engine_HostDrawI(i, 2);",
        "        d->sorting_order = UnityEngine_Engine_HostDrawI(i, 1); d->flags = 0; d->go = -1;",
        "        d->z = UnityEngine_Engine_HostDrawF(i, 8); d->layer = UnityEngine_Engine_HostDrawI(i, 3);",
        "    }",
        "    return n;",
        "}",
        "int engine_collect_cameras(EngineCamera *out, int max) {",
        "    if (!_loaded) { _loaded = 1; SceneData_Load(); }",
        "    if (max < 1 || !UnityEngine_Engine_HostHasCamera()) return 0;",
        "    EngineCamera *k = &out[0];",
        "    k->x = UnityEngine_Engine_HostCameraF(0); k->y = UnityEngine_Engine_HostCameraF(1);",
        "    k->half_h = UnityEngine_Engine_HostCameraF(2); k->aspect = 0;",
        "    k->rect_x = 0; k->rect_y = 0; k->rect_w = 1; k->rect_h = 1;",
        "    k->bg_r = UnityEngine_Engine_HostCameraF(3); k->bg_g = UnityEngine_Engine_HostCameraF(4); k->bg_b = UnityEngine_Engine_HostCameraF(5);",
        "    k->clear = 1;",
        "    return 1;",
        "}",
        "void engine_draw_camera(int k) { (void)k; }",
        "int engine_collect_tris(EngineTri *out, int max) { (void)out; (void)max; return 0; }",
        "float engine_pointer_x, engine_pointer_y; int engine_pointer_down;",
        "int Screen_width = 800, Screen_height = 600, Screen_fullScreen, Screen_fullScreenNative, Screen_maximized;",
        'const char engine_product_name[] = "Player";',
        "void engine_set_log_file(const char *p) { (void)p; }",
        "void engine_apply_argv(int argc, char **argv) { (void)argc; (void)argv; }",
        'const char *engine_console_log_path(void) { return ""; }',
        'const char *engine_data_path(void) { return ""; }',
        'const char *engine_persistent_data_path(void) { return ""; }',
        "int engine_wants_quit(void) { return UnityEngine_Engine_HostWantsQuit(); }",
        "int engine_position_floats(void) { return 0; }",
        "int engine_upload_positions(float *dst, int max) { (void)dst; (void)max; return 0; }",
        "",
    ]
    return "\n".join(c)


# The engine functions the host interface calls: (C name, namespace, class, method, DNA_Find signature).  On a managed engine (--dna put it on
# DotNetAnywhere) the lowered program has none of them, and each is a C function of that name calling the managed method instead.
HOST_CALLS = (
    ("SceneData_Load", "", "SceneData", "Load", ">v"),
    ("UnityEngine_Engine_HostSetDeltaTime", "UnityEngine", "Engine", "HostSetDeltaTime", "f>v"),
    ("UnityEngine_Engine_HostSetScreen", "UnityEngine", "Engine", "HostSetScreen", "ii>v"),
    ("UnityEngine_InputState_HostSetKeyboard", "UnityEngine", "InputState", "HostSetKeyboard", "i>v"),
    ("UnityEngine_InputState_HostSetKey", "UnityEngine", "InputState", "HostSetKey", "ii>v"),
    ("UnityEngine_InputState_HostSetPointer", "UnityEngine", "InputState", "HostSetPointer", "ffi>v"),
    ("UnityEngine_InputState_HostAddScroll", "UnityEngine", "InputState", "HostAddScroll", "ff>v"),
    ("UnityEngine_InputState_HostSetPad", "UnityEngine", "InputState", "HostSetPad", "ii>v"),
    ("UnityEngine_InputState_HostSetPadButton", "UnityEngine", "InputState", "HostSetPadButton", "iii>v"),
    ("UnityEngine_InputState_HostSetPadAxis", "UnityEngine", "InputState", "HostSetPadAxis", "iif>v"),
    ("UnityEngine_Engine_Tick", "UnityEngine", "Engine", "Tick", ">v"),
    ("UnityEngine_Engine_HostCollect", "UnityEngine", "Engine", "HostCollect", ">i"),
    ("UnityEngine_Engine_HostDrawF", "UnityEngine", "Engine", "HostDrawF", "ii>f"),
    ("UnityEngine_Engine_HostDrawI", "UnityEngine", "Engine", "HostDrawI", "ii>i"),
    ("UnityEngine_Engine_HostHasCamera", "UnityEngine", "Engine", "HostHasCamera", ">i"),
    ("UnityEngine_Engine_HostCameraF", "UnityEngine", "Engine", "HostCameraF", "i>f"),
    ("UnityEngine_Engine_HostWantsQuit", "UnityEngine", "Engine", "HostWantsQuit", ">i"),
)


def managed_host_calls(c_text):
    """C functions, named as the lowered engine's, that call the managed engine's methods through DotNetAnywhere's host API (ccs_find and
    ccs_call are the bridge glue's) -- for each one the lowered program does not define (what stayed native is called directly).
    ponytail: one DNA_Call per value, so a frame of n sprites is about 13n calls.  Upgrade path: one call filling float and int arrays ('b')."""
    out = []
    for cname, ns, cls, meth, sig in HOST_CALLS:
        if re.search(r"(?m)^[^;{}\n]*\b%s\s*\([^;{]*\)\s*\{" % cname, c_text):
            continue
        params, ret = sig.split(">")
        ctype = {"i": "int", "f": "float"}
        decl = ", ".join("%s a%d" % (ctype[p], k) for k, p in enumerate(params)) or "void"
        args = ", ".join(("DNA_Int(a%d)" if p == "i" else "DNA_Float(a%d)") % k for k, p in enumerate(params))
        rt = {"v": "void", "i": "int", "f": "float"}[ret]
        body = ["    static DNA_Method *m;", "    DNA_Value r;"]
        if params:
            body.append("    DNA_Value a[%d] = { %s };" % (len(params), args))
        body += ['    if (m == NULL) m = ccs_find("%s", "%s", "%s", "%s");' % (ns, cls, meth, sig),
                 "    ccs_call(m, %s, %d, &r);" % ("a" if params else "NULL", len(params))]
        if ret != "v":
            body.append("    return r.u.%s;" % ret)
        out += ["static %s %s(%s) {" % (rt, cname, decl)] + body + ["}"]
    return "\n".join(out) + "\n"


def write_dna_link(outdir):
    """outdir/dna/link.txt: what a host links a managed engine with (DotNetAnywhere's headers and library, copied beside the program, since the
    library is built for this program's [DllImport] table), and corlib.dll beside the player.  Nothing when the program is all native."""
    sys.path.insert(0, os.path.join(ccsharp_home(), "crust"))
    import ccs2c
    home, bdir = ccs2c.dna_prepare(False)
    ffi = os.path.exists(os.path.join(outdir, "cpp", "game.ffi.json"))
    lib = os.path.join(bdir, "libdna_ffi.a" if ffi else "libdna.a")
    d = os.path.join(outdir, "dna")
    os.makedirs(d, exist_ok=True)
    shutil.copy(lib, os.path.join(d, os.path.basename(lib)))
    shutil.copy(os.path.join(bdir, "corlib.dll"), outdir)
    with open(os.path.join(d, "link.txt"), "w") as f:
        f.write("-I %s dna/%s -lpthread\n" % (os.path.join(home, "native", "src"), os.path.basename(lib)))


def write_host_files(objects, cameras, outdir, prefabs=()):
    """outdir/engine.c (the lowered program + the host interface; its `main` is `game_main`) and outdir/engine_draw.h, the files a host
    builds with, as for the packed path."""
    sys.path.insert(0, REPO)
    import tools.unity.unity_pack as _up
    with open(os.path.join(outdir, "engine_draw.h"), "w") as f:
        f.write(_up.emit_engine_draw_h())
    with open(os.path.join(outdir, "game.c")) as f:
        c = f.read()
    glue = os.path.join(outdir, "cpp", "game.bridge.c")
    extra = ""
    if os.path.exists(glue):
        # a managed half: its glue (whose `main` is renamed with the program's) and the host calls over it
        with open(glue) as f:
            extra = "\n" + f.read() + "\n" + managed_host_calls(c)
        write_dna_link(outdir)
    with open(os.path.join(outdir, "engine.c"), "w") as f:
        f.write("#define main game_main\n" + c + extra + host_shim(objects, cameras, prefabs))
