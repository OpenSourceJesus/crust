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
    python3 tools/unity_pack_features.py --asan     # players under ASan +
                                                    # UBSan (not box2d)

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

    ("value_chain",
     'int x = 3;\n'
     'int y = 4;\n'
     'string s = x + y + "z";\n'
     'Debug.Log("value_chain:" + s);\n'
     'Debug.Log("value_chain:" + x + y);\n',
     "", ["value_chain:7z", "value_chain:34"]),

    ("string_null",
     'string s = null;\n'
     'if (s == null) Debug.Log("string_null:null");\n'
     's = "x";\n'
     'if (s != null) Debug.Log("string_null:" + s);\n'
     'if (tag.label != null) Debug.Log("string_null:field");\n',
     "", ["string_null:null", "string_null:x", "string_null:field"]),

    ("format",
     'float speed = 2.5f;\n'
     'Debug.Log($"format:hp {hp} speed {speed:F2} id {hp:D3} {{x}}");\n'
     'Debug.Log(string.Format("format:{0}/{1}", hp, "max"));\n'
     'Debug.Log("format:" + speed.ToString("F1") + "," + hp.ToString());\n',
     "", ["format:hp 7 speed 2.50 id 007 {x}", "format:7/max",
          "format:2.5,7"]),

    ("split_join",
     'string csv = "a,b,,c";\n'
     'string[] parts = csv.Split(\',\');\n'
     'Debug.Log("split_join:" + parts.Length + "," + parts[1] + "," + parts[3]);\n'
     'parts[0] = parts[0].ToUpper();\n'
     'Debug.Log("split_join:" + string.Join("|", parts));\n'
     'foreach (string w in "x y z".Split(\' \')) Debug.Log("split_join:" + w);\n'
     'string[] two = "k=v;x=y".Split(\'=\', \';\');\n'
     'Debug.Log("split_join:" + two.Length + two[3]);\n'
     'string[] sep = "a::b".Split("::");\n'
     'foreach (string q in sep) { Debug.Log("split_join:" + q); }\n',
     "", ["split_join:4,b,c", "split_join:A|b||c", "split_join:x",
          "split_join:y", "split_join:z", "split_join:4y", "split_join:a",
          "split_join:b"]),

    ("rt_mathf",
     'Debug.Log("rt_mathf:" + Mathf.Sqrt(16f) + "," + Mathf.Pow(2f, 10f) + ","'
     ' + Mathf.FloorToInt(2.7f) + "," + Mathf.CeilToInt(2.1f) + ","'
     ' + Mathf.RoundToInt(2.5f) + "," + Mathf.RoundToInt(3.5f));\n'
     'Debug.Log("rt_mathf:" + Mathf.Clamp01(1.5f) + "," + Mathf.InverseLerp(0f, 10f, 5f)'
     ' + "," + Mathf.Repeat(7f, 3f) + "," + Mathf.PingPong(5f, 3f) + ","'
     ' + Mathf.DeltaAngle(350f, 10f) + "," + Mathf.MoveTowards(0f, 10f, 3f));\n'
     'if (Mathf.Approximately(Mathf.PI * Mathf.Rad2Deg, 180f)) Debug.Log("rt_mathf:pi");\n'
     'if (1e30f < Mathf.Infinity) Debug.Log("rt_mathf:inf");\n',
     "", ["rt_mathf:4,1024,2,3,2,4", "rt_mathf:1,0.5,1,1,20,3",
          "rt_mathf:pi", "rt_mathf:inf"]),

    ("rt_random",
     'Random.InitState(42);\n'
     'int lo = 99; int hi = -1; bool fl = true;\n'
     'for (int k = 0; k < 1000; k++) {\n'
     '    int r = Random.Range(0, 5);\n'
     '    if (r < lo) lo = r;\n'
     '    if (r > hi) hi = r;\n'
     '    float f = Random.Range(-1f, 1f);\n'
     '    if (f < -1f || f > 1f) fl = false;\n'
     '    float v = Random.value;\n'
     '    if (v < 0f || v > 1f) fl = false;\n'
     '}\n'
     'Debug.Log("rt_random:" + lo + "," + hi + "," + (fl ? "in" : "out"));\n',
     "", ["rt_random:0,4,in"]),

    ("rt_parse",
     'int a = int.Parse(" 42 ");\n'
     'float b = float.Parse("2.5");\n'
     'int c;\n'
     'bool okc = int.TryParse("x7", out c);\n'
     'if (int.TryParse("-13", out int d)) Debug.Log("rt_parse:" + a + "," + b + "," + okc + "," + d);\n',
     "", ["rt_parse:42,2.5,False,-13"]),

    ("bool_format",
     'bool yes = true;\n'
     'bool no = !yes;\n'
     'Debug.Log("bool_format:" + yes + "," + no + "," + "abc".Contains("b"));\n'
     'Debug.Log(yes);\n',
     "", ["bool_format:True,False,True"]),

    ("rt_path",
     'string p = Path.Combine("/tmp/a", "b.txt");\n'
     'Debug.Log("rt_path:" + p + "," + Path.GetFileName(p) + "," + Path.GetExtension(p)'
     ' + "," + Path.GetFileNameWithoutExtension(p) + "," + Path.GetDirectoryName(p));\n'
     'Debug.Log("rt_path:" + Path.Combine("a", "b", "c") + "," + Path.Combine("a/", "/abs"));\n',
     "", ["rt_path:/tmp/a/b.txt,b.txt,.txt,b,/tmp/a", "rt_path:a/b/c,/abs"]),

    ("rt_files",
     'string dir = Path.Combine(Application.persistentDataPath, "rt_files/deep");\n'
     'Directory.CreateDirectory(dir);\n'
     'string f = Path.Combine(dir, "t.txt");\n'
     'File.WriteAllText(f, "one\\r\\ntwo\\n");\n'
     'string[] lines = File.ReadAllLines(f);\n'
     'Debug.Log("rt_files:" + Directory.Exists(dir) + "," + Directory.Exists(f)'
     ' + "," + lines.Length + "," + lines[0] + "," + lines[1] + ","'
     ' + File.ReadAllText(f).Length);\n',
     "", ["rt_files:True,False,2,one,two,9"]),

    ("rt_hash",
     'string a = "abc";\n'
     'string b = "ab" + "c";\n'
     'if (a.GetHashCode() == b.GetHashCode() && a.GetHashCode() != "abd".GetHashCode())'
     ' Debug.Log("rt_hash:ok");\n',
     "", ["rt_hash:ok"]),

    ("strarray_new",
     'string[] a = new string[3];\n'
     'a[0] = "x"; a[2] = "z";\n'
     'string[] b = new string[] { "p", "q" + hp };\n'
     'string[] c = { "u", "v" };\n'
     'Debug.Log("strarray_new:" + string.Join("-", a) + "," + a.Length + ","'
     ' + string.Join("", b) + "," + c[1]);\n'
     'a = new string[1];\n'
     'Debug.Log("strarray_new:" + a.Length);\n',
     "", ["strarray_new:x--z,3,pq7,v", "strarray_new:1"]),

    ("builder",
     'var sb = new System.Text.StringBuilder();\n'
     'for (int k = 0; k < 3; k++) sb.Append(k).Append(",");\n'
     'sb.Append(true).Append(\'!\').Append(1.5f);\n'
     'StringBuilder t = new StringBuilder("init");\n'
     't.AppendLine(); t.AppendLine("x"); t.AppendFormat("{0}-{1}", hp, "y");\n'
     'Debug.Log("builder:" + sb.ToString() + "|" + sb.Length);\n'
     't.Replace("\\n", "/");\n'
     'Debug.Log("builder:" + t.ToString());\n'
     'sb.Clear();\n'
     'Debug.Log("builder:" + sb.Length);\n',
     "", ["builder:0,1,2,True!1.5|14", "builder:init/x/7-y",
          "builder:0"]),

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

#: A spawned class with a List field: the list tables must be as long as the
#: instance array (scene count + spawn budget), not the scene count. The
#: list is private, so a clone's starts empty (Unity copies only serialized
#: fields); each instance fills it once and prints the same line. In
#: Update: a spawned object does not get Start yet.
UNITY_SHOT_SCRIPT = (
    "using UnityEngine;\n"
    "using System.Collections.Generic;\n"
    "public class MaxInstancesAttribute : System.Attribute {\n"
    "    public MaxInstancesAttribute(int n) {}\n"
    "}\n"
    "[MaxInstances(4)]\n"
    "public class Shot : MonoBehaviour {\n"
    "    public static int made;\n"
    "    private List<int> marks = new List<int>();\n"
    "    void Update() {\n"
    "        if (marks.Count == 0) {\n"
    "            for (int k = 0; k < 40; k++) marks.Add(k);\n"
    "            Debug.Log(\"list_cap:\" + marks.Count + \",\" + marks[39]);\n"
    "        }\n"
    "        if (made < 3) { made = made + 1; Instantiate(this); }\n"
    "    }\n"
    "}\n")
UNITY_SHOT_EXPECT = ["list_cap:40,39"] * 4

#: A spawned object gets Awake at once and Start before its first Update,
#: each exactly once, like an authored one. One authored Spawner makes three
#: clones; every instance reports its own Awake/Start/first Update order.
UNITY_LIFE_SCRIPT = (
    "using UnityEngine;\n"
    "[MaxInstances(4)]\n"
    "public class Life : MonoBehaviour {\n"
    "    public static int made;\n"
    "    private int step;\n"
    "    private int updates;\n"
    "    void Awake() { step = step * 10 + 1; }\n"
    "    void Start() { step = step * 10 + 2; }\n"
    "    void Update() {\n"
    "        updates = updates + 1;\n"
    "        if (updates == 1) Debug.Log(\"life:\" + (step * 10 + 3));\n"
    "        if (made < 3) { made = made + 1; Instantiate(this); }\n"
    "    }\n"
    "}\n")
UNITY_LIFE_EXPECT = ["life:123"] * 4

#: JsonUtility on a packed object: every serialized field kind, a private
#: field left out, pretty printing, and an overwrite that skips an unknown
#: key holding nested values.
UNITY_SAVE_SCRIPT = (
    "using UnityEngine;\n"
    "public class Save : MonoBehaviour {\n"
    "    public int hp = 3;\n"
    "    public float speed = 1.5f;\n"
    "    public bool alive = true;\n"
    "    public string title = \"a\\\"b\";\n"
    "    public Vector2 v;\n"
    "    private int secret = 9;\n"
    "    void Start() {\n"
    "        Debug.Log(\"json:\" + JsonUtility.ToJson(this));\n"
    "        Debug.Log(\"json:\" + JsonUtility.ToJson(this, true).Replace(\"\\n\", \"|\"));\n"
    "        JsonUtility.FromJsonOverwrite(\"{\\\"hp\\\": 42, \\\"extra\\\": [1, {\\\"a\\\": 2}],\"\n"
    "            + \" \\\"title\\\": \\\"n\\\\u00e9w\\\", \\\"v\\\": {\\\"x\\\": 1.5, \\\"y\\\": -2}}\", this);\n"
    "        Debug.Log(\"json:\" + JsonUtility.ToJson(this) + secret);\n"
    "    }\n"
    "}\n")
UNITY_SAVE_EXPECT = [
    'json:{"hp":3,"speed":1.5,"alive":true,"title":"a\\"b","v":{"x":0.0,"y":0.0}}',
    'json:{|    "hp": 3,|    "speed": 1.5,|    "alive": true,|    "title": "a\\"b",'
    '|    "v": {|        "x": 0.0,|        "y": 0.0|    }|}',
    'json:{"hp":42,"speed":1.5,"alive":true,"title":"n\u00e9w","v":{"x":1.5,"y":-2.0}}9',
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

    ("g_builder",
     'var sb = new System.Text.StringBuilder();\n'
     'sb.Append(Hp).Append("/").Append(Title);\n'
     'GD.Print("g_builder:", sb.ToString());\n',
     "", ["g_builder:5/hero!"]),

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
    player = ["using UnityEngine;\n", "using System.IO;\n",
              "using System.Text;\n",
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
    _write(os.path.join(scripts, "Shot.cs"), UNITY_SHOT_SCRIPT)
    _write(os.path.join(scripts, "Shot.cs.meta"),
           "guid: feat0000000000000000000000000004\n")
    _write(os.path.join(scripts, "Save.cs"), UNITY_SAVE_SCRIPT)
    _write(os.path.join(scripts, "Save.cs.meta"),
           "guid: feat0000000000000000000000000006\n")
    _write(os.path.join(scripts, "Life.cs"), UNITY_LIFE_SCRIPT)
    _write(os.path.join(scripts, "Life.cs.meta"),
           "guid: feat0000000000000000000000000005\n")
    _write(os.path.join(scripts, "Tag.cs"), UNITY_TAG_SCRIPT)
    _write(os.path.join(scripts, "Tag.cs.meta"),
           "guid: feat0000000000000000000000000002\n")
    tags = [("alpha", 30), ("'two words'", 40), ("'it''s'", 50)]
    scene = ["%YAML 1.1\n",
             _go(1, "Player", [3]),
             _mb(3, 1, "feat0000000000000000000000000001",
                 "  hp: 7\n  tag: {fileID: %d}\n" % (tags[0][1] + 2))]
    scene.append(_go(80, "Save", [82]))
    scene.append(_mb(82, 80, "feat0000000000000000000000000006"))
    scene.append(_go(70, "Life", [72]))
    scene.append(_mb(72, 70, "feat0000000000000000000000000005"))
    scene.append(_go(60, "Shot", [62]))
    scene.append(_mb(62, 60, "feat0000000000000000000000000004"))
    for label, fid in tags:
        scene.append(_go(fid, "Tag", [fid + 2]))
        scene.append(_mb(fid + 2, fid, "feat0000000000000000000000000002",
                         "  label: %s\n" % label))
    _write(os.path.join(root, "Assets", "Scenes", "S.unity"), "".join(scene))
    expect = []
    for _n, _b, _m, e in UNITY_CHECKS:
        expect += e
    return (expect + UNITY_TAG_EXPECT + UNITY_SHOT_EXPECT + UNITY_LIFE_EXPECT
            + UNITY_SAVE_EXPECT)


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

def _asan_player(out):
    """The same player, rebuilt from its C with ASan and UBSan."""
    exe = os.path.join(out, "asan_player")
    srcs = [os.path.join(out, n) for n in ("engine.c", "data.c", "main.c")]
    r = subprocess.run(["gcc", "-std=gnu11", "-g", "-w",
                        "-fsanitize=address,undefined",
                        "-fno-sanitize-recover=undefined"] + srcs
                       + ["-o", exe, "-lm"], capture_output=True, text=True)
    if r.returncode != 0:
        raise unity_pack.PackError("asan build failed: %s" % r.stderr[:400])
    return exe


def run_project(kind, make, tmp, verbose, asan=False):
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
            if asan:
                exe = _asan_player(out)
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
    if "ERROR: AddressSanitizer" in run.stderr or "runtime error" in run.stderr:
        ok = False
        report.append("sanitizer: %s" % run.stderr.strip()[:600])
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
    asan = "--asan" in argv
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
        ok, report = run_project(kind, make, tmp, verbose,
                                 asan=asan and kind != "box2d")
        all_ok = all_ok and ok
        print("\n".join(report))
    if keep:
        print("kept %s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
