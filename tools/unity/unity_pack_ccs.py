"""unity_pack_ccs -- the CCSharp backend of unity_pack.

The project's scripts are C#; CCSharp (https://github.com/crustos/CCSharp) lowers them, together with its small UnityEngine
(unity-engine/*.cs), to the C++ subset and then to C.  This module does not parse or lower C#: it reads the scene with
unity_pack's scene loader, writes the scene as C# (`SceneData.cs`: `new GameObject`, `Attach`), copies the scripts, and hands
the lot to `ccs2c.py --unity-engine`.

`eligible(objects, cameras)` says whether the engine covers the project; when it does not, unity_pack falls back to the packed
(data-oriented, C-emitted) path -- see `--packed`.

    CCSHARP_HOME   the CCSharp checkout (default ../CCSharp beside this repo)
"""
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# Component keys the engine has no class for yet.  A scene record that has any of them set goes to the packed path.
UNSUPPORTED_KEYS = (
    "canvas", "rigidbody2d", "collider2d", "ui_text", "ui_image", "ui_button", "particle_system", "animator",
    "audio_source", "light", "tilemap",
)
# Messages a script declares without `override` in Unity; the engine's MonoBehaviour has them virtual.
MESSAGES = ("Awake", "Start", "Update", "LateUpdate", "OnDestroy")


def ccsharp_home():
    return os.environ.get("CCSHARP_HOME") or os.path.join(os.path.dirname(REPO), "CCSharp")


def available():
    return os.path.exists(os.path.join(ccsharp_home(), "crust", "ccs2c.py"))


def eligible(objects, cameras, prefabs=()):
    """(ok, why): ok when every record uses only what the engine covers."""
    for o in all_records(objects, prefabs):
        for k in UNSUPPORTED_KEYS:
            if o.get(k):
                return False, "%s uses %s" % (o.get("name"), k)
        if o.get("father_id") or o.get("child_ids"):
            by_xf = {r.get("xf_id"): r for r in objects}
            if o.get("father_id") and o["father_id"] not in by_xf:
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
        ccs2c.to_cpp([d], unity_engine=True, describe=out, workdir=work)
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


def prepare_script(text):
    """A Unity script as the engine's C#: `[Shared]` is CCSharp's to refuse, and the messages are `override`."""
    text = re.sub(r"^[ \t]*\[Shared\][ \t]*\r?\n", "", text, flags=re.M)
    for m in MESSAGES:
        text = re.sub(r"(public\s+)(void\s+%s\s*\()" % m, r"\1override \2", text)
    return text


def _f(x):
    s = repr(float(x))
    if "e" in s or "inf" in s or "nan" in s:
        s = "%.9g" % float(x)
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
    return x.replace("\\", "\\\\").replace('"', '\\"')


def prefab_name(pf):
    return "Prefab_%s" % re.sub(r"\W", "_", pf["guid"])


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
            if not r.get("class"):
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


def write_project(objects, cameras, outdir, frames=60, prefabs=()):
    """Write the C# project to outdir/cs; returns its folder."""
    cs = os.path.join(outdir, "cs")
    shutil.rmtree(cs, ignore_errors=True)
    os.makedirs(cs)
    seen, texts = set(), {}
    for o in all_records(objects, prefabs):
        sp = o.get("script")
        if sp and sp not in seen:
            seen.add(sp)
            with open(sp) as f:
                texts[os.path.basename(sp)] = prepare_script(f.read())
    for name, text in texts.items():
        with open(os.path.join(cs, name), "w") as f:
            f.write(text)
    classes = describe(texts) if texts else {}
    with open(os.path.join(cs, "SceneData.cs"), "w") as f:
        f.write(scene_source(objects, cameras, classes, prefabs))
    with open(os.path.join(cs, "Program.cs"), "w") as f:
        f.write(MAIN_SOURCE % frames)
    return cs


def build(objects, cameras, outdir, run=False, prefabs=()):
    """Lower and build the project; returns the ccs2c exit code."""
    cs = write_project(objects, cameras, outdir, prefabs=prefabs)
    cmd = [sys.executable, os.path.join(ccsharp_home(), "crust", "ccs2c.py"), cs, "--unity-engine", "--name=game",
           "--convert=" + os.path.join(outdir, "cpp"), "--c", "-o", os.path.join(outdir, "game.c"),
           "--exe=" + os.path.join(outdir, "game")]
    if run:
        cmd.append("--run")
    rc = subprocess.call(cmd)
    if rc == 0:
        write_host_files(objects, cameras, outdir, prefabs)
    return rc


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
