#!/usr/bin/env python3
"""Fast end-to-end check of the packer features being added now.

The full suite (`python3 -m unittest tests.test_unity_pack`) packs a few
hundred projects and takes most of a quarter hour. This packs three -- one
Unity project, one Godot project and, with a Box2D-Packed checkout, one
physics scene -- each holding every feature under test, builds them, runs
them, and compares what they print with what C# prints. About a minute.

    python3 tools/unity_pack_features.py            # everything
    python3 tools/unity_pack_features.py unity      # one project: unity,
                                                    # godot or box2d
    python3 tools/unity_pack_features.py -v         # show every line
    python3 tools/unity_pack_features.py --keep     # keep the temp dirs

Each check is a C# method of its own, called from `Start`, printing lines
that begin with its name. A method the packer could not lower is emitted
empty (warning CS8000) and prints nothing, so it cannot pass by accident;
the warning is shown with the failure. Adding a check is adding an entry to
UNITY_CHECKS / GODOT_CHECKS: the body, and the lines C# would print.

Needs a C compiler, a coost checkout (string storage) and, for `box2d`, a
Box2D-Packed checkout -- each found as the packer finds it (`COOST_ROOT`,
`BOX2D_PACKED_ROOT`, or beside this repository). A missing one skips its
project and says so.
"""
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.unity_pack as unity_pack  # noqa: E402


# --------------------------------------------------------------------------
# Unity checks: (name, method body, extra class members, expected lines).
# The project's Player has `public int hp;` (7 in the scene) and a handle
# `public Tag tag;` to a Tag whose `label` the scene sets to "alpha".
# --------------------------------------------------------------------------

UNITY_CHECKS = [
    ("kept_local",
     'string saved = "saved-" + hp;\n'
     'for (int k = 0; k < 20; k++) { string t = "tmp-" + k; }\n'
     'Debug.Log("kept_local:" + saved);\n',
     "", ["kept_local:saved-7"]),

    ("long_string",
     'string big = "";\n'
     'for (int k = 0; k < 300; k++) { big = big + "abc"; }\n'
     'Debug.Log("long_string:" + big);\n',
     "", ["long_string:" + "abc" * 300]),

    ("int_format",
     'string s = "n=";\n'
     's += 1000000;\n'
     's += "!";\n'
     'Debug.Log("int_format:" + s);\n'
     'Debug.Log("int_format:hp=" + hp);\n',
     "", ["int_format:n=1000000!", "int_format:hp=7"]),

    ("value_first",
     'string s = hp + " hp";\n'
     'Debug.Log("value_first:" + s);\n'
     'Debug.Log("value_first:" + (hp * 2 + "!"));\n',
     "", ["value_first:7 hp", "value_first:14!"]),

    ("self_assign",
     'string a = "x";\n'
     'string b = a;\n'
     'a = a + a;\n'
     'a = b + a + b;\n'
     'Debug.Log("self_assign:" + a + "," + b);\n',
     "", ["self_assign:xxxx,x"]),

    ("param_owned",
     'Take("in-" + hp);\n',
     '    void Take(string p) {\n'
     '        for (int k = 0; k < 20; k++) { string t = "tmp-" + k; }\n'
     '        p = p + "!";\n'
     '        Debug.Log("param_owned:" + p);\n'
     '    }\n',
     ["param_owned:in-7!"]),

    ("field_instance",
     'note = note + "-x";\n'
     'for (int k = 0; k < 20; k++) { string t = "tmp-" + k; }\n'
     'Debug.Log("field_instance:" + note);\n',
     '    public string note = "init";\n',
     ["field_instance:init-x"]),

    ("field_static",
     'last = "hp" + hp;\n'
     'last += "!";\n'
     'for (int k = 0; k < 20; k++) { string t = "tmp-" + k; }\n'
     'Debug.Log("field_static:" + last);\n',
     '    public static string last;\n',
     ["field_static:hp7!"]),

    # Restores what it changed: Unity does not order Start across classes,
    # and the Tag's own Start prints its label.
    ("field_other",
     'string old = tag.label;\n'
     'Debug.Log("field_other:" + tag.label);\n'
     'tag.label = tag.label + "-set";\n'
     'Debug.Log("field_other:" + tag.label);\n'
     'tag.label = old;\n'
     'Debug.Log("field_other:" + tag.label);\n',
     "", ["field_other:alpha", "field_other:alpha-set", "field_other:alpha"]),

    ("member_length",
     'string s = "hello";\n'
     'Debug.Log("member_length:" + s.Length);\n'
     'Debug.Log("member_length:" + "abc".Length);\n',
     "", ["member_length:5", "member_length:3"]),

    ("member_case",
     'string s = "MiXed";\n'
     'Debug.Log("member_case:" + s.ToUpper() + "," + s.ToLower());\n',
     "", ["member_case:MIXED,mixed"]),

    ("member_substring",
     'string s = "hello world";\n'
     'Debug.Log("member_substring:" + s.Substring(6));\n'
     'Debug.Log("member_substring:" + s.Substring(0, 5));\n',
     "", ["member_substring:world", "member_substring:hello"]),

    ("member_search",
     'string s = "a-b-a";\n'
     'Debug.Log("member_search:" + s.IndexOf("a") + "," + s.LastIndexOf("a")'
     ' + "," + s.IndexOf("z") + "," + s.IndexOf(\'b\') + "," + s.IndexOf("a", 1));\n'
     'if (s.Contains("-b-")) Debug.Log("member_search:contains");\n'
     'if (s.StartsWith("a-") && s.EndsWith("-a")) Debug.Log("member_search:ends");\n',
     "", ["member_search:0,4,-1,2,4", "member_search:contains",
          "member_search:ends"]),

    ("member_edit",
     'string s = "  a-b  ";\n'
     'Debug.Log("member_edit:[" + s.Trim() + "][" + s.TrimStart() + "]["'
     ' + s.TrimEnd() + "]");\n'
     'Debug.Log("member_edit:" + s.Trim().Replace("-", "+"));\n',
     "", ["member_edit:[a-b][a-b  ][  a-b]", "member_edit:a+b"]),

    ("member_compare",
     'string s = "abc";\n'
     'string e = "";\n'
     'if (s.Equals("abc")) Debug.Log("member_compare:equals");\n'
     'if (string.IsNullOrEmpty(e) && !string.IsNullOrEmpty(s))'
     ' Debug.Log("member_compare:empty");\n',
     "", ["member_compare:equals", "member_compare:empty"]),

    ("member_on_field",
     'Debug.Log("member_on_field:" + tag.label.ToUpper() + "," + tag.label.Length);\n',
     "", ["member_on_field:ALPHA,5"]),
]

#: The Tag class: its label comes from the scene, one line per instance.
UNITY_TAG_SCRIPT = (
    "using UnityEngine;\n"
    "public class Tag : MonoBehaviour {\n"
    "    public string label;\n"
    "    void Start() { Debug.Log(\"field_scene:\" + label); }\n"
    "}\n")
UNITY_TAG_EXPECT = ["field_scene:alpha", "field_scene:two words",
                    "field_scene:it's"]


# --------------------------------------------------------------------------
# Godot checks: the same features through godot_pack's reading of Godot C#.
# Player has `[Export] public int Hp` (5 in the scene) and
# `[Export] public string Title` ("hero" in the scene).
# --------------------------------------------------------------------------

GODOT_CHECKS = [
    ("g_kept_local",
     'string saved = "saved-" + Hp;\n'
     'for (int k = 0; k < 20; k++) { string t = "tmp-" + k; }\n'
     'GD.Print("g_kept_local:", saved);\n',
     "", ["g_kept_local:saved-5"]),

    ("g_field_scene",
     'GD.Print("g_field_scene:", Title);\n'
     'Title = Title + "!";\n'
     'GD.Print("g_field_scene:", Title);\n',
     "", ["g_field_scene:hero", "g_field_scene:hero!"]),

    ("g_members",
     'string s = "  Godot  ";\n'
     'GD.Print("g_members:", s.Trim().ToUpper(), ",", s.Trim().Length);\n',
     "", ["g_members:GODOT,5"]),
]


# --------------------------------------------------------------------------
# Box2D-Packed: a ball falls onto the ground; its string field records hits.
# --------------------------------------------------------------------------

BOX2D_BALL = (
    "using UnityEngine;\n"
    "public class Ball : MonoBehaviour {\n"
    "    public int enters;\n"
    "    public string status = \"falling\";\n"
    "    void Start() { Debug.Log(\"b2d_status:\" + status); }\n"
    "    void OnCollisionEnter2D(Collision2D coll) {\n"
    "        enters = enters + 1;\n"
    "        status = \"hit-\" + enters;\n"
    "        if (enters == 1) Debug.Log(\"b2d_status:\" + status);\n"
    "    }\n"
    "}\n")
BOX2D_EXPECT = ["b2d_status:falling", "b2d_status:hit-1"]


# --------------------------------------------------------------------------

def _mb(fid, go, guid, fields=""):
    return ("--- !u!114 &%d\nMonoBehaviour:\n  m_GameObject: {fileID: %d}\n"
            "  m_Script: {fileID: 11500000, guid: %s}\n%s" % (fid, go, guid,
                                                               fields))


def _go(fid, name, comps, pos=(0, 0)):
    return ("--- !u!1 &%d\nGameObject:\n  m_Name: %s\n  m_Component:\n%s"
            "--- !u!4 &%d\nTransform:\n  m_GameObject: {fileID: %d}\n"
            "  m_LocalPosition: {x: %s, y: %s, z: 0}\n"
            % (fid, name, "".join("  - component: {fileID: %d}\n" % c
                                  for c in [fid + 1] + comps),
               fid + 1, fid, pos[0], pos[1]))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def unity_project(root):
    player = ["using UnityEngine;\n",
              "public class Player : MonoBehaviour {\n",
              "    public int hp;\n",
              "    public Tag tag;\n"]
    for _n, _b, members, _e in UNITY_CHECKS:
        if members and "string " in members and "(" not in members:
            player.append(members)
    player.append("    void Start() {\n")
    for name, _b, _m, _e in UNITY_CHECKS:
        player.append("        C_%s();\n" % name)
    player.append("    }\n")
    for name, body, members, _e in UNITY_CHECKS:
        if members and "(" in members:
            player.append(members)
        player.append("    void C_%s() {\n%s    }\n" % (name, "".join(
            "        " + l + "\n" for l in body.rstrip("\n").split("\n"))))
    player.append("}\n")
    scripts = os.path.join(root, "Assets", "Scripts")
    _write(os.path.join(scripts, "Player.cs"), "".join(player))
    _write(os.path.join(scripts, "Player.cs.meta"),
           "guid: feat0000000000000000000000000001\n")
    _write(os.path.join(scripts, "Tag.cs"), UNITY_TAG_SCRIPT)
    _write(os.path.join(scripts, "Tag.cs.meta"),
           "guid: feat0000000000000000000000000002\n")
    tags = [("alpha", 30), ("'two words'", 40), ("'it''s'", 50)]
    scene = ["%YAML 1.1\n",
             _go(1, "Player", [3]),
             _mb(3, 1, "feat0000000000000000000000000001",
                 "  hp: 7\n  tag: {fileID: %d}\n" % (tags[0][1] + 2))]
    for label, fid in tags:
        scene.append(_go(fid, "Tag", [fid + 2]))
        scene.append(_mb(fid + 2, fid, "feat0000000000000000000000000002",
                         "  label: %s\n" % label))
    _write(os.path.join(root, "Assets", "Scenes", "S.unity"), "".join(scene))
    expect = []
    for _n, _b, _m, e in UNITY_CHECKS:
        expect += e
    return expect + UNITY_TAG_EXPECT


def godot_project(root):
    fixture = os.path.join(ROOT, "tests", "fixtures", "GodotMini")
    shutil.copytree(fixture, root)
    lines = ["using Godot;\n",
             "public partial class Player : Node2D\n{\n",
             "    [Export] public int Hp = 3;\n",
             "    [Export] public string Title = \"none\";\n",
             "    public override void _Ready()\n    {\n"]
    for name, _b, _m, _e in GODOT_CHECKS:
        lines.append("        C_%s();\n" % name)
    lines.append("    }\n")
    for name, body, _m, _e in GODOT_CHECKS:
        lines.append("    void C_%s()\n    {\n%s    }\n" % (name, "".join(
            "        " + l + "\n" for l in body.rstrip("\n").split("\n"))))
    lines.append("}\n")
    _write(os.path.join(root, "scripts", "Player.cs"), "".join(lines))
    main = os.path.join(root, "main.tscn")
    with open(main) as f:
        tscn = f.read()
    # The fixture's Player node sets Hp; give it a Title as well.
    tscn = re.sub(r"(?m)^(Hp = .*)$", r'\1\nTitle = "hero"', tscn, count=1)
    _write(main, tscn)
    expect = []
    for _n, _b, _m, e in GODOT_CHECKS:
        expect += e
    return expect


def box2d_project(root):
    scripts = os.path.join(root, "Assets", "Scripts")
    _write(os.path.join(scripts, "Ball.cs"), BOX2D_BALL)
    _write(os.path.join(scripts, "Ball.cs.meta"),
           "guid: feat0000000000000000000000000003\n")
    scene = (
        "%YAML 1.1\n"
        "--- !u!1 &1\nGameObject:\n  m_Name: Ground\n"
        "  m_Component:\n  - component: {fileID: 2}\n"
        "  - component: {fileID: 3}\n"
        "--- !u!4 &2\nTransform:\n  m_GameObject: {fileID: 1}\n"
        "  m_LocalPosition: {x: 0, y: -2.5, z: 0}\n"
        "--- !u!61 &3\nBoxCollider2D:\n  m_GameObject: {fileID: 1}\n"
        "  m_Enabled: 1\n  m_IsTrigger: 0\n"
        "  m_Offset: {x: 0, y: 0}\n  m_Size: {x: 20, y: 1}\n"
        "--- !u!1 &10\nGameObject:\n  m_Name: Ball\n"
        "  m_Component:\n  - component: {fileID: 11}\n"
        "  - component: {fileID: 12}\n  - component: {fileID: 13}\n"
        "  - component: {fileID: 14}\n"
        "--- !u!4 &11\nTransform:\n  m_GameObject: {fileID: 10}\n"
        "  m_LocalPosition: {x: 0, y: 2, z: 0}\n"
        "--- !u!50 &12\nRigidbody2D:\n  m_GameObject: {fileID: 10}\n"
        "  m_BodyType: 0\n  m_Mass: 1\n  m_GravityScale: 1\n"
        "  m_LinearDamping: 0\n"
        "--- !u!58 &13\nCircleCollider2D:\n  m_GameObject: {fileID: 10}\n"
        "  m_Enabled: 1\n  m_IsTrigger: 0\n"
        "  m_Offset: {x: 0, y: 0}\n  m_Radius: 0.5\n"
        + _mb(14, 10, "feat0000000000000000000000000003", "  enters: 0\n"))
    _write(os.path.join(root, "Assets", "Scenes", "S.unity"), scene)
    return BOX2D_EXPECT


# --------------------------------------------------------------------------

def run_project(kind, make, tmp, verbose):
    """Pack, build and run one project; returns (ok, report lines)."""
    root = os.path.join(tmp, "feat_" + kind)
    out = os.path.join(tmp, kind + "-out")
    expect = make(root)
    err = io.StringIO()
    t0 = time.time()
    try:
        with contextlib.redirect_stderr(err):
            plan = unity_pack.pack(root, out, force=True)
            exe = unity_pack.build_player_executable(
                out, plan.get("product_name") or "Player")
    except unity_pack.PackError as e:
        return False, ["pack failed: %s" % e.message.strip()]
    warnings = [l for l in err.getvalue().splitlines()
                if "warning CS" in l or "error CS" in l]
    run = subprocess.run([exe, "-logFile", "-"], capture_output=True,
                         text=True, cwd=out, timeout=120)
    got = [l for l in run.stdout.splitlines()
           if re.match(r"^[a-z0-9_]+:", l) and not l.startswith("ticks=")]
    report = []
    ok = run.returncode == 0
    if run.returncode != 0:
        report.append("player exited %d: %s" % (run.returncode,
                                                run.stderr.strip()[:300]))
    prefixes = sorted({e.split(":", 1)[0] for e in expect})
    for pre in prefixes:
        want = [e for e in expect if e.split(":", 1)[0] == pre]
        have = [g for g in got if g.split(":", 1)[0] == pre]
        if want == have:
            report.append("  ok    %s" % pre)
            if verbose:
                report += ["          %s" % _short(h) for h in have]
            continue
        ok = False
        report.append("  FAIL  %s" % pre)
        report.append("          want %s" % [_short(w) for w in want])
        report.append("          got  %s" % [_short(h) for h in have])
        for w in warnings:
            if "C_%s" % pre in w or ("`" + pre) in w:
                report.append("          %s" % w.strip())
    stray = [w for w in warnings
             if not any("C_%s" % p in w for p in prefixes)]
    for w in stray:
        report.append("  warn  %s" % w.strip())
    report.insert(0, "%s: %s (%.0fs)" % (
        kind, "ok" if ok else "FAILED", time.time() - t0))
    return ok, report


def _short(s, n=70):
    return s if len(s) <= n else "%s...(%d chars)" % (s[:n], len(s))


def main(argv):
    verbose = "-v" in argv
    keep = "--keep" in argv
    only = [a for a in argv if not a.startswith("-")]
    if unity_pack.find_coost_root() is None:
        sys.stderr.write("no coost checkout (COOST_ROOT, or ../coost): the "
                         "string features need one\n")
        return 2
    # The project directory names the player binary, so none is named after
    # a directory a pack writes (`box2d/` holds the physics glue).
    projects = [("unity", unity_project), ("godot", godot_project),
                ("box2d", box2d_project)]
    tmp = tempfile.mkdtemp(prefix="upack-features-")
    all_ok = True
    for kind, make in projects:
        if only and kind not in only:
            continue
        if kind == "box2d" and unity_pack.find_box2d_root() is None:
            print("box2d: skipped (no Box2D-Packed checkout: "
                  "BOX2D_PACKED_ROOT, or ../box2d)")
            continue
        ok, report = run_project(kind, make, tmp, verbose)
        all_ok = all_ok and ok
        print("\n".join(report))
    if keep:
        print("kept %s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
