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


def emit_groups(gs, classes, tex_ids, pfx, ind, pf_var):
    """C# that builds the GameObjects of `gs` (a scene's or a prefab's): returns (lines, {xf_id: variable}).  `pf_var` is {guid: "SceneData.pfN"},
    the template a script field that refers to a prefab is given."""
    out = []
    var_of = {g[0].get("xf_id"): "%so%d" % (pfx, i) for i, g in enumerate(gs)}
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
        for k, r in enumerate(g):
            if not (r.get("class") and r.get("script")):   # (a scriptless object has a made-up class name)
                continue
            c = "%ss%d_%d" % (pfx, i, k)
            out.append("%s %s = new %s();" % (r["class"], c, r["class"]))
            decl = declared_fields(classes, r["class"])
            for fname, val in sorted((r.get("fields") or {}).items()):
                lit = field_literal(decl.get(fname), val) if fname in decl else None
                if lit is not None:
                    out.append("%s.%s = %s;" % (c, fname, lit))
            for fname, ref in sorted((r.get("object_refs") or {}).items()):
                guid = str(ref).split("@")[-1]
                if guid in pf_var and decl.get(fname, "").endswith("GameObject"):
                    out.append("%s.%s = %s;" % (c, fname, pf_var[guid]))
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
        if sp and sp.get("has_sprite"):
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
    for i, g in enumerate(gs):
        if g[0].get("father_id") in var_of:
            out.append("%so%d.transform.parent = %s.transform;" % (pfx, i, var_of[g[0]["father_id"]]))
    return [ind + l for l in out], var_of


def scene_source(objects, cameras, classes=None, prefabs=()):
    """SceneData.cs: the scene as code, and a Prefab subclass for each prefab.  `classes` is describe()'s account of the scripts: a scene field is set
    on the script that declares it."""
    classes = classes or {}
    tex_ids = textures(objects, prefabs)[1]
    pf_var = {pf["guid"]: "SceneData.pf%d" % i for i, pf in enumerate(prefabs)}
    out = ["using UnityEngine;", ""]
    for pf in prefabs:                      # a prefab is code that builds it; Instantiate(template) calls it
        gs = groups(pf["objects"])
        lines, var_of = emit_groups(gs, classes, tex_ids, "", "        ", pf_var)
        roots = [i for i, g in enumerate(gs) if g[0].get("father_id") not in var_of]
        out += ["class %s : Prefab" % prefab_name(pf), "{", "    public override GameObject Create()", "    {"] + lines
        out += ["        return o%d;" % (roots[0] if roots else 0), "    }", "}", ""]
    out += ["static class SceneData", "{"]
    for i, pf in enumerate(prefabs):
        out.append("    public static GameObject pf%d;" % i)
    out += ["    public static void Load()", "    {"]
    for i, pf in enumerate(prefabs):        # templates first: a script field may refer to one
        out += ['        pf%d = new GameObject("%s");' % (i, _cs_string(pf["name"])), "        pf%d.SetActive(false);" % i,
                "        pf%d.prefab = new %s();" % (i, prefab_name(pf)), "        Engine.Forget(pf%d);" % i]
    lines, var_of = emit_groups(groups(objects), classes, tex_ids, "", "        ", pf_var)
    out += lines
    for j, c in enumerate(cameras):
        v = "c%d" % j
        p = c["local_pos"]
        out.append('        GameObject %s = new GameObject("%s");' % (v, _cs_string(c["name"])))
        out.append("        %s.transform.localPosition = new Vector3(%s, %s, %s);" % (v, _f(p[0]), _f(p[1]), _f(p[2])))
        out.append("        Camera %sc = new Camera();" % v)
        out.append("        %sc.orthographicSize = %s;" % (v, _f(c.get("orthographic_size", 5.0))))
        out.append("        %sc.background = new Color(%s, %s, %s, 1f);" % (v, _f(c.get("bg_r", 0)), _f(c.get("bg_g", 0)), _f(c.get("bg_b", 0))))
        out.append("        %s.Attach(%sc);" % (v, v))
    out += ["    }", "}", ""]
    return "\n".join(out)


def collect_prefabs(root, objects, assets):
    """The prefabs the scene's scripts refer to (and those they refer to): [{"guid", "name", "objects"}], records as the scene's are.  The prefab files are
    read by unity_pack's own parser."""
    import tools.unity.unity_pack as up
    guids = up._guid_map(root, asset_guids=assets)
    found, order = {}, []

    def refs_of(recs):
        for o in recs:
            for ref in (o.get("object_refs") or {}).values():
                g = str(ref).split("@")[-1]
                path = assets.get(g)
                if path and path.endswith(".prefab") and g not in found:
                    yield g, path

    pending = list(refs_of(objects))
    while pending:
        g, path = pending.pop(0)
        if g in found:
            continue
        recs = up._parsed_prefab_objects(path, guids, assets)[0]
        recs = [dict(r) for r in recs]
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
        "static int _loaded;",
        "void engine_tick(void) {",
        "    if (!_loaded) { _loaded = 1; SceneData_Load(); }",
        "    UnityEngine_Time_deltaTime = Time_deltaTime > 0 ? Time_deltaTime : 1.0f / 60.0f;",
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
        "int engine_wants_quit(void) { return 0; }",
        "int engine_position_floats(void) { return 0; }",
        "int engine_upload_positions(float *dst, int max) { (void)dst; (void)max; return 0; }",
        "",
    ]
    return "\n".join(c)


def write_host_files(objects, cameras, outdir, prefabs=()):
    """outdir/engine.c (the lowered program + the host interface; its `main` is `game_main`) and outdir/engine_draw.h, the files a host
    builds with, as for the packed path."""
    sys.path.insert(0, REPO)
    import tools.unity.unity_pack as _up
    with open(os.path.join(outdir, "engine_draw.h"), "w") as f:
        f.write(_up.emit_engine_draw_h())
    with open(os.path.join(outdir, "game.c")) as f:
        c = f.read()
    with open(os.path.join(outdir, "engine.c"), "w") as f:
        f.write("#define main game_main\n" + c + host_shim(objects, cameras, prefabs))
