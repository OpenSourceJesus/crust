# SPDX-License-Identifier: MIT
"""unity_pack: Physics: Rigidbody / Rigidbody2D / Collider tables, physics materials, collision
messages, and the Box2D-Packed checkout (box2d_unity.py) for 2D physics.

Moved out of tools/unity_pack.py unchanged; unity_pack re-exports every name
here, so `unity_pack.<name>` keeps working."""

from __future__ import annotations
import hashlib
import json
import os
import pickle
import re
import sys
import math
import copy
import tools.cs2cpp as cs2cpp  # noqa: E402

import tools.cs2cpp as cs2cpp  # noqa: E402
from tools.unity_pack_common import *  # noqa: E402,F401,F403

__all__ = [
    '_COLLISION2D_MSGS',
    '_JOINT2D_COMPONENTS',
    '_JOINT2D_KINDS',
    '_build_joint2d_table',
    '_parse_joint2d',
    '_DEFAULT_MAT2D',
    '_DEFAULT_MAT3D',
    '_PHYSICS_COMPONENTS',
    '_build_collider2d_tables',
    '_build_collider3d_tables',
    '_build_rigidbody_tables',
    '_collision2d_arg_name',
    '_load_box2d_unity',
    '_load_physics_materials',
    '_parse_physic_material3d',
    '_parse_physics_material2d',
    '_rewrite_rigidbody_assigns',
    '_want_rb2d_tables',
    '_want_rb3d_tables',
    '_wrap_log_collision2d_tostring',
    'find_box2d_root',
]


# Unity defaults when Collider/Rigidbody m_Material is {fileID: 0}.
_DEFAULT_MAT2D = {
    "friction": 0.4,
    "bounciness": 0.0,
    "friction_combine": 0,  # Average
    "bounce_combine": 0,
}


_DEFAULT_MAT3D = {
    "dynamic_friction": 0.6,
    "static_friction": 0.6,
    "bounciness": 0.0,
    "friction_combine": 0,
    "bounce_combine": 0,
}


def _parse_physics_material2d(text):
    fr = re.search(r"(?m)^\s+friction:\s*([0-9.eE+-]+)", text)
    bn = re.search(r"(?m)^\s+bounciness:\s*([0-9.eE+-]+)", text)
    fc = re.search(r"(?m)^\s+m_FrictionCombine:\s*(\d+)", text)
    bc = re.search(r"(?m)^\s+m_BounceCombine:\s*(\d+)", text)
    return {
        "friction": float(fr.group(1)) if fr else 0.4,
        "bounciness": float(bn.group(1)) if bn else 0.0,
        "friction_combine": int(fc.group(1)) if fc else 0,
        "bounce_combine": int(bc.group(1)) if bc else 0,
    }


def _parse_physic_material3d(text):
    df = re.search(r"(?m)^\s+dynamicFriction:\s*([0-9.eE+-]+)", text)
    sf = re.search(r"(?m)^\s+staticFriction:\s*([0-9.eE+-]+)", text)
    bn = re.search(r"(?m)^\s+bounciness:\s*([0-9.eE+-]+)", text)
    fc = re.search(r"(?m)^\s+frictionCombine:\s*(\d+)", text)
    bc = re.search(r"(?m)^\s+bounceCombine:\s*(\d+)", text)
    return {
        "dynamic_friction": float(df.group(1)) if df else 0.6,
        "static_friction": float(sf.group(1)) if sf else 0.6,
        "bounciness": float(bn.group(1)) if bn else 0.0,
        "friction_combine": int(fc.group(1)) if fc else 0,
        "bounce_combine": int(bc.group(1)) if bc else 0,
    }


def _load_physics_materials(asset_guids):
    """guid → PhysicsMaterial2D / PhysicMaterial from project assets."""
    mats2d = {}
    mats3d = {}
    for guid, path in (asset_guids or {}).items():
        low = path.lower()
        try:
            if low.endswith(".physicsmaterial2d"):
                mats2d[guid.lower()] = _parse_physics_material2d(_read(path))
            elif low.endswith(".physicmaterial"):
                mats3d[guid.lower()] = _parse_physic_material3d(_read(path))
        except (IOError, OSError):
            continue
    return mats2d, mats3d


def _wrap_log_collision2d_tostring(text, param):
    """Console/Debug of a Collision2D param → Collision2D_ToString(handle)."""
    if not param:
        return text
    out = []
    i = 0
    while True:
        m = re.search(r"(?:Console_WriteLine|Debug_Log)\s*\(", text[i:])
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:i + m.start()])
        call = m.group(0)
        callee = re.match(r"(Console_WriteLine|Debug_Log)", call).group(1)
        start = i + m.end()
        depth = 1
        j = start
        while j < len(text) and depth:
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            out.append(text[i + m.start():])
            break
        args = text[start:j].strip()
        if args == param:
            args = "Collision2D_ToString(%s)" % param
        out.append("%s(%s)" % (callee, args))
        i = j + 1
    return "".join(out)


_PHYSICS_COMPONENTS = frozenset(("Rigidbody2D", "Rigidbody"))


# MonoBehaviour 2D collision messages (Unity Physics2D).
_TRIGGER2D_MSGS = ("OnTriggerEnter2D", "OnTriggerStay2D", "OnTriggerExit2D")

#: 2D joints Box2D-Packed builds, by `_Joint2D_kind`: revolute, distance
#: (rigid, or a rope with maxDistanceOnly), distance with a spring, weld,
#: prismatic, wheel.
_JOINT2D_KINDS = {"HingeJoint2D": 0, "DistanceJoint2D": 1, "SpringJoint2D": 2,
                  "FixedJoint2D": 3, "SliderJoint2D": 4, "WheelJoint2D": 5}
#: Script types a joint handle may have (the bases take any kind).
_JOINT2D_COMPONENTS = frozenset(_JOINT2D_KINDS) | {"Joint2D", "AnchoredJoint2D"}


def _parse_joint2d(kind, block):
    """A joint component's YAML -> its record (Unity's defaults for what the
    YAML leaves out; `Infinity` for an unbreakable joint)."""
    def num(name, default):
        m = re.search(r"(?m)^\s+%s:\s*(-?Infinity|[-0-9.eE+]+)\s*$" % name, block)
        if not m:
            return default
        v = m.group(1)
        return float("inf") if v == "Infinity" else (
            float("-inf") if v == "-Infinity" else float(v))

    def vec(name):
        m = re.search(r"%s:\s*\{x:\s*([^,}]+),\s*y:\s*([^}]+)\}" % name, block)
        return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
    cm = re.search(r"m_ConnectedRigidBody:\s*\{fileID:\s*(-?\d+)", block)
    k = _JOINT2D_KINDS[kind]
    return {
        "kind": k,
        "enabled": int(num("m_Enabled", 1)),
        "collide": int(num("m_EnableCollision", 0)),
        "connected_fid": cm.group(1) if cm and cm.group(1) != "0" else None,
        "anchor": vec("m_Anchor"),
        "canchor": vec("m_ConnectedAnchor"),
        "auto_anchor": int(num("m_AutoConfigureConnectedAnchor", 1)),
        "auto_distance": int(num("m_AutoConfigureDistance", 1)),
        "distance": num("m_Distance", 1.0),
        "max_distance_only": int(num("m_MaxDistanceOnly", 0)),
        "frequency": num("m_Frequency", {2: 1.0, 5: 2.0}.get(k, 0.0)),
        "damping": num("m_DampingRatio", {5: 0.7}.get(k, 0.0)),
        "use_motor": int(num("m_UseMotor", 0)),
        "use_limits": int(num("m_UseLimits", 0)),
        "motor_speed": num("m_MotorSpeed", 0.0),
        "motor_max": num("m_MaximumMotorForce", 10000.0),
        "lower": num("m_LowerAngle", num("m_LowerTranslation", 0.0)),
        "upper": num("m_UpperAngle", num("m_UpperTranslation", 0.0)),
        # slider: the axis (degrees); wheel: the suspension's (default up)
        "angle": num("m_Angle", 90.0 if k == 5 else 0.0),
        "auto_angle": int(num("m_AutoConfigureAngle", 0)),
        "break_force": num("m_BreakForce", float("inf")),
        "break_torque": num("m_BreakTorque", float("inf")),
    }


def _build_joint2d_table(plan):
    """plan["joints2d"]: each authored joint whose GameObject has a
    Rigidbody2D (Unity adds one; here it is skipped, with a warning), with
    its own and connected body's indices, its owner (for OnJointBreak2D) and
    GameObject; plan["joint2d_by_file_id"] for serialized references."""
    rb_of = {(r["owner_class"], r["owner_inst"]): k
             for k, r in enumerate(plan.get("rigidbody2d") or [])}
    by_fid = plan.get("rb2d_by_file_id") or {}
    joints, jfid = [], {}
    for cname in sorted(plan["classes"]):
        for i, o in enumerate(plan["classes"][cname].get("instances") or []):
            for j in o.get("joints2d") or []:
                own = rb_of.get((cname, i))
                if own is None:
                    sys.stderr.write(
                        "unity_pack: warning: %s on %r has no Rigidbody2D "
                        "(skipped)\n" % ({v: k for k, v in _JOINT2D_KINDS.items()}
                                         [j["kind"]], o.get("name")))
                    continue
                other = by_fid.get(str(j.get("connected_fid"))) \
                    if j.get("connected_fid") else None
                rec = dict(j, owner_class=cname, owner_inst=i,
                           go_index=o.get("go_index"), rb_a=own,
                           rb_b=-1 if other is None else int(other))
                if j.get("file_id") is not None:
                    jfid[str(j["file_id"])] = len(joints)
                joints.append(rec)
    plan["joints2d"] = joints
    plan["joint2d_by_file_id"] = jfid
    plan["physics2d_joints"] = bool(joints)


_COLLISION2D_MSGS = (
    "OnCollisionEnter2D",
    "OnCollisionStay2D",
    "OnCollisionExit2D",
    # OnTrigger*2D(Collider2D other) take the other collider the same way.
    "OnTriggerEnter2D",
    "OnTriggerStay2D",
    "OnTriggerExit2D",
)


def _collision2d_arg_name(args):
    """Param name from `OnCollisionEnter2D(Collision2D coll)`, or None."""
    if not args:
        return None
    m = re.match(
        r"(?:UnityEngine\.)?(?:Collision2D|Collider2D)\s+(\w+)\s*$",
        args.strip())
    return m.group(1) if m else None


def _want_rb2d_tables(plan, used_apis=None, getcomponent_types=None):
    """True when engine/data must emit Rigidbody2D packed tables.

    Authored bodies, AddComponent, GetComponent, and Collider2D collide all
    touch ``_Rigidbody2D_*``. Collide alone is enough: resolution reads mass
    and velocity even when every collider's rb index is -1.
    """
    used_apis = used_apis or set()
    gct = set(getcomponent_types or ())
    gct |= set(plan.get("getcomponent_types") or [])
    add_types = set(plan.get("addcomponent_types") or [])
    return (
        bool(plan.get("rigidbody2d"))
        or "Rigidbody2D" in gct
        or "Rigidbody2D" in used_apis
        or "Rigidbody2D" in add_types
        or bool(plan.get("collider2d")))


def _want_rb3d_tables(plan, used_apis=None, getcomponent_types=None):
    """True when engine/data must emit Rigidbody (3D) packed tables."""
    used_apis = used_apis or set()
    gct = set(getcomponent_types or ())
    gct |= set(plan.get("getcomponent_types") or [])
    add_types = set(plan.get("addcomponent_types") or [])
    return (
        bool(plan.get("rigidbody"))
        or "Rigidbody" in gct
        or "Rigidbody" in used_apis
        or "Rigidbody" in add_types
        or bool(plan.get("collider3d")))


def _build_rigidbody_tables(plan):
    """Authored Rigidbody2D / Rigidbody → packed tables linked to MB instances."""
    rb2d = []
    rb3d = []
    go_rb2d = {}  # go_index -> rb2d index
    go_rb3d = {}
    rb2d_by_file_id = {}
    rb3d_by_file_id = {}
    for cname, cl in sorted(plan["classes"].items()):
        for i, o in enumerate(cl.get("instances") or []):
            n = o.get("name") or "obj"
            gi = o.get("go_index")
            r2 = o.get("rigidbody2d")
            if r2:
                if gi is not None:
                    go_rb2d[int(gi)] = len(rb2d)
                fid = r2.get("file_id")
                if fid is not None and str(fid) != "0":
                    rb2d_by_file_id[str(fid)] = len(rb2d)
                rb2d.append({
                    "name": n,
                    "go_index": gi,
                    "owner_class": cname,
                    "owner_inst": i,
                    "file_id": fid,
                    "body_type": int(r2.get("body_type") or 0),
                    "mass": float(r2.get("mass") or 1.0),
                    "gravity_scale": float(r2.get("gravity_scale") or 1.0),
                    "linear_damping": float(r2.get("linear_damping") or 0.0),
                    "vel_x": float(r2.get("vel_x") or 0.0),
                    "vel_y": float(r2.get("vel_y") or 0.0),
                    "freeze_rot": bool(r2.get("freeze_rot")),
                    "ang_vel": float(r2.get("ang_vel") or 0.0),
                })
            r3 = o.get("rigidbody")
            if r3:
                if gi is not None:
                    go_rb3d[int(gi)] = len(rb3d)
                fid = r3.get("file_id")
                if fid is not None and str(fid) != "0":
                    rb3d_by_file_id[str(fid)] = len(rb3d)
                rb3d.append({
                    "name": n,
                    "go_index": gi,
                    "owner_class": cname,
                    "owner_inst": i,
                    "file_id": fid,
                    "mass": float(r3.get("mass") or 1.0),
                    "use_gravity": int(r3.get("use_gravity")
                                       if r3.get("use_gravity") is not None
                                       else 1),
                    "drag": float(r3.get("drag") or 0.0),
                    "vel_x": float(r3.get("vel_x") or 0.0),
                    "vel_y": float(r3.get("vel_y") or 0.0),
                    "vel_z": float(r3.get("vel_z") or 0.0),
                })
    return (rb2d, rb3d, go_rb2d, go_rb3d, rb2d_by_file_id, rb3d_by_file_id)


def _build_collider2d_tables(plan):
    """Authored Box / Circle / CapsuleCollider2D → packed contact table."""
    cols = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    # Map (class, inst) → rb2d index for dynamic flag.
    rb_of = {}
    for ri, r in enumerate(plan.get("rigidbody2d") or []):
        rb_of[(r["owner_class"], r["owner_inst"])] = ri
    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            c = o.get("collider2d")
            if not c or not c.get("enabled", 1):
                continue
            rb_i = rb_of.get((cname, i))
            body = 2  # static (no RB)
            if rb_i is not None:
                body = int((plan["rigidbody2d"][rb_i]).get("body_type") or 0)
            kind = {"box": 0, "capsule_v": 2, "capsule_h": 3}.get(
                c.get("kind"), 1)
            cols.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "rb2d": rb_i if rb_i is not None else -1,
                "body_type": body,  # 0 dynamic, 1 kinematic, 2 static
                "kind": kind,
                "is_trigger": int(c.get("is_trigger") or 0),
                "ox": float(c.get("ox") or 0.0),
                "oy": float(c.get("oy") or 0.0),
                "hw": float(c.get("hw") or 0.5),
                "hh": float(c.get("hh") or 0.5),
                "cos_z": float(c.get("cos_z") or 1.0),
                "sin_z": float(c.get("sin_z") or 0.0),
                "friction": float(c.get("friction")
                                  if c.get("friction") is not None
                                  else _DEFAULT_MAT2D["friction"]),
                "bounciness": float(c.get("bounciness")
                                    if c.get("bounciness") is not None
                                    else 0.0),
                "friction_combine": int(c.get("friction_combine") or 0),
                "bounce_combine": int(c.get("bounce_combine") or 0),
            })
    return cols


def _build_collider3d_tables(plan):
    """Authored BoxCollider / SphereCollider → packed contact table."""
    cols = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    rb_of = {}
    for ri, r in enumerate(plan.get("rigidbody") or []):
        rb_of[(r["owner_class"], r["owner_inst"])] = ri
    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            c = o.get("collider3d")
            if not c or not c.get("enabled", 1):
                continue
            rb_i = rb_of.get((cname, i))
            body = 0 if rb_i is not None else 2  # dynamic if RB else static
            kind = 0 if c.get("kind") == "box" else 1
            cols.append({
                "name": o.get("name") or "obj",
                "owner_class": cname,
                "owner_class_id": cid,
                "owner_inst": i,
                "rb3d": rb_i if rb_i is not None else -1,
                "body_type": body,
                "kind": kind,
                "is_trigger": int(c.get("is_trigger") or 0),
                "ox": float(c.get("ox") or 0.0),
                "oy": float(c.get("oy") or 0.0),
                "oz": float(c.get("oz") or 0.0),
                "hw": float(c.get("hw") or 0.5),
                "hh": float(c.get("hh") or 0.5),
                "hd": float(c.get("hd") or 0.5),
                "dynamic_friction": float(
                    c.get("dynamic_friction")
                    if c.get("dynamic_friction") is not None
                    else _DEFAULT_MAT3D["dynamic_friction"]),
                "static_friction": float(
                    c.get("static_friction")
                    if c.get("static_friction") is not None
                    else _DEFAULT_MAT3D["static_friction"]),
                "bounciness": float(c.get("bounciness")
                                    if c.get("bounciness") is not None
                                    else 0.0),
                "friction_combine": int(c.get("friction_combine") or 0),
                "bounce_combine": int(c.get("bounce_combine") or 0),
            })
    return cols


def _rewrite_rigidbody_assigns(text, plan, this_class):
    """Lower Rigidbody(2D).linearVelocity / .velocity assigns.

    Supports:
      GetComponent<Rigidbody2D>().linearVelocity = new Vector2(x, y);
      rb.linearVelocity = rb.linearVelocity.SetX(expr);
      rb.linearVelocity = new Vector2(x, y);
    and Rigidbody / SetY / SetZ / velocity aliases.
    """
    this_idn = _c_ident(this_class)
    go_this = "_engine_go_of_%s(i)" % this_idn
    cl = (plan.get("classes") or {}).get(this_class) or {}
    rb2d_fields = [
        f["name"] for f in (cl.get("fields") or [])
        if f.get("ty") == "Rigidbody2D"]
    rb3d_fields = [
        f["name"] for f in (cl.get("fields") or [])
        if f.get("ty") == "Rigidbody"]

    def repl_2d_new(m):
        args = _split_call_args(m.group(1))
        if len(args) < 2:
            return m.group(0)
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody2D(%s); "
            "if (_up_rb >= 0) { _Rigidbody2D_vel_x[_up_rb] = (%s); "
            "_Rigidbody2D_vel_y[_up_rb] = (%s); } }"
            % (go_this, args[0], args[1])
        )

    def repl_3d_new(m):
        args = _split_call_args(m.group(1))
        if len(args) < 3:
            return m.group(0)
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody(%s); "
            "if (_up_rb >= 0) { _Rigidbody_vel_x[_up_rb] = (%s); "
            "_Rigidbody_vel_y[_up_rb] = (%s); "
            "_Rigidbody_vel_z[_up_rb] = (%s); } }"
            % (go_this, args[0], args[1], args[2])
        )

    text = cs2cpp.code_sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"new\s+Vector2\s*\((.*?)\)\s*;",
        repl_2d_new, text, flags=re.S)
    text = cs2cpp.code_sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"new\s+Vector3\s*\((.*?)\)\s*;",
        repl_3d_new, text, flags=re.S)

    def repl_2d_setx(m):
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody2D(%s); "
            "if (_up_rb >= 0) { _Rigidbody2D_vel_x[_up_rb] = (%s); } }"
            % (go_this, m.group(1).strip())
        )

    def repl_2d_sety(m):
        return (
            "{ int _up_rb = GameObject_GetComponent_Rigidbody2D(%s); "
            "if (_up_rb >= 0) { _Rigidbody2D_vel_y[_up_rb] = (%s); } }"
            % (go_this, m.group(1).strip())
        )

    text = cs2cpp.code_sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*\.\s*SetX\s*\((.*?)\)\s*;",
        repl_2d_setx, text, flags=re.S)
    text = cs2cpp.code_sub(
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
        r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody2D\s*>"
        r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*\.\s*SetY\s*\((.*?)\)\s*;",
        repl_2d_sety, text, flags=re.S)

    def repl_3d_set(axis):
        def _repl(m):
            return (
                "{ int _up_rb = GameObject_GetComponent_Rigidbody(%s); "
                "if (_up_rb >= 0) { _Rigidbody_vel_%s[_up_rb] = (%s); } }"
                % (go_this, axis, m.group(1).strip())
            )
        return _repl

    for axis in ("X", "Y", "Z"):
        text = cs2cpp.code_sub(
            r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody\s*>"
            r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"(?:this\s*\.\s*)?GetComponent\s*<\s*(?:UnityEngine\.)?Rigidbody\s*>"
            r"\s*\(\s*\)\s*\.\s*(?:linearVelocity|velocity)\s*\.\s*Set%s\s*\((.*?)\)\s*;"
            % axis,
            repl_3d_set(axis.lower()), text, flags=re.S)

    # Field-based: rb.linearVelocity = rb.linearVelocity.SetX(expr);
    for fname in rb2d_fields:
        get_rb = "(int)%s_get_%s(i)" % (this_idn, fname)

        def _set_xy(x_expr, y_expr, gr=get_rb):
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody2D_vel_x[_up_rb] = (%s); "
                "_Rigidbody2D_vel_y[_up_rb] = (%s); } }"
                % (gr, x_expr, y_expr)
            )

        def repl_setx(m, fn=fname, gr=get_rb):
            # Keep y; set x from SetX arg.
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody2D_vel_x[_up_rb] = (%s); } }"
                % (gr, m.group(1).strip())
            )

        def repl_sety(m, fn=fname, gr=get_rb):
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody2D_vel_y[_up_rb] = (%s); } }"
                % (gr, m.group(1).strip())
            )

        def repl_new2(m, gr=get_rb):
            args = _split_call_args(m.group(1))
            if len(args) < 2:
                return m.group(0)
            return _set_xy(args[0], args[1], gr)

        text = cs2cpp.code_sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"(?:this\s*\.\s*)?%s\s*\.\s*(?:linearVelocity|velocity)\s*"
            r"\.\s*SetX\s*\((.*?)\)\s*;"
            % (re.escape(fname), re.escape(fname)),
            repl_setx, text, flags=re.S)
        text = cs2cpp.code_sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"(?:this\s*\.\s*)?%s\s*\.\s*(?:linearVelocity|velocity)\s*"
            r"\.\s*SetY\s*\((.*?)\)\s*;"
            % (re.escape(fname), re.escape(fname)),
            repl_sety, text, flags=re.S)
        text = cs2cpp.code_sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"new\s+Vector2\s*\((.*?)\)\s*;" % re.escape(fname),
            repl_new2, text, flags=re.S)

    for fname in rb3d_fields:
        get_rb = "(int)%s_get_%s(i)" % (this_idn, fname)

        def _axis_set(axis, m, gr=get_rb):
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody_vel_%s[_up_rb] = (%s); } }"
                % (gr, axis, m.group(1).strip())
            )

        def repl_new3(m, gr=get_rb):
            args = _split_call_args(m.group(1))
            if len(args) < 3:
                return m.group(0)
            return (
                "{ int _up_rb = %s; if (_up_rb >= 0) { "
                "_Rigidbody_vel_x[_up_rb] = (%s); "
                "_Rigidbody_vel_y[_up_rb] = (%s); "
                "_Rigidbody_vel_z[_up_rb] = (%s); } }"
                % (gr, args[0], args[1], args[2])
            )

        for axis in ("X", "Y", "Z"):
            text = cs2cpp.code_sub(
                r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
                r"(?:this\s*\.\s*)?%s\s*\.\s*(?:linearVelocity|velocity)\s*"
                r"\.\s*Set%s\s*\((.*?)\)\s*;"
                % (re.escape(fname), re.escape(fname), axis),
                lambda m, ax=axis.lower(): _axis_set(ax, m),
                text, flags=re.S)
        text = cs2cpp.code_sub(
            r"(?<![_\w])%s\s*\.\s*(?:linearVelocity|velocity)\s*=\s*"
            r"new\s+Vector3\s*\((.*?)\)\s*;" % re.escape(fname),
            repl_new3, text, flags=re.S)

    # Reads: rb.linearVelocity.x / rb.linearVelocity (a Vector2 / Vector3).
    def _vel_read(gr, table, axis):
        return ("({ int _up_rb = %s; _up_rb < 0 ? 0.f : %s_vel_%s[_up_rb]; })"
                % (gr, table, axis))

    for fields, table, axes, vec in (
            (rb2d_fields, "_Rigidbody2D", "xy", "Vector2"),
            (rb3d_fields, "_Rigidbody", "xyz", "Vector3")):
        for fname in fields:
            gr = "(int)%s_get_%s(i)" % (this_idn, fname)
            if vec == "Vector2":
                # rb.linearVelocity += v / -= v
                text = cs2cpp.code_sub(
                    r"(?<![\w.])(?:this\s*\.\s*)?%s\s*\.\s*"
                    r"(?:linearVelocity|velocity)\s*([+-])=\s*([^;]+);"
                    % re.escape(fname),
                    lambda m, g=gr, t=table: (
                        "{ int _up_rb = %s; if (_up_rb >= 0) { "
                        "Vector2 _up_d = (%s); "
                        "%s_vel_x[_up_rb] %s= _up_d.x; "
                        "%s_vel_y[_up_rb] %s= _up_d.y; } }" % (
                            g, m.group(2).strip(), t, m.group(1), t,
                            m.group(1))),
                    text)
            text = cs2cpp.code_sub(
                r"(?<![\w.])(?:this\s*\.\s*)?%s\s*\.\s*"
                r"(?:linearVelocity|velocity)\s*\.\s*([%s])\b"
                r"(?!\s*[-+*/]?=[^=])"
                % (re.escape(fname), axes),
                lambda m, g=gr, t=table: _vel_read(g, t, m.group(1)),
                text)
            text = cs2cpp.code_sub(
                r"(?<![\w.])(?:this\s*\.\s*)?%s\s*\.\s*"
                r"(?:linearVelocity|velocity)\b"
                r"(?!\s*(?:[-+*/]?=[^=]|\.))"
                % re.escape(fname),
                lambda m, g=gr, t=table, a=axes, v=vec: "%s_make(%s)" % (
                    v, ", ".join(_vel_read(g, t, ax) for ax in a)),
                text)
    return text


def find_box2d_root(box2d_root=None):
    """Box2D-Packed checkout: *box2d_root*, $BOX2D_PACKED_ROOT, or a ``box2d``
    directory beside this repository. None when there is none."""
    candidates = [box2d_root, os.environ.get("BOX2D_PACKED_ROOT"),
                  os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                      os.path.abspath(__file__)))), "box2d")]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "box2d_unity.py")):
            return os.path.abspath(c)
    return None


def _load_box2d_unity(box2d_root=None):
    """Import box2d_unity.py from a Box2D-Packed checkout."""
    root = find_box2d_root(box2d_root)
    if not root:
        raise PackError(
            "2D physics (Rigidbody2D / Collider2D) uses Box2D-Packed: pass "
            "--box2d PATH, set BOX2D_PACKED_ROOT, or clone "
            "https://github.com/crustos/box2d beside this repository")
    if not os.path.isfile(os.path.join(root, "box2d_unity.py")):
        raise PackError("no box2d_unity.py in %s (Box2D-Packed checkout?)" % root)
    if root not in sys.path:
        sys.path.insert(0, root)
    import box2d_unity
    return box2d_unity



# ---------------------------------------------------------------------------
# Layers: LayerMask, and the *All queries' arrays, at the source level
# ---------------------------------------------------------------------------

#: Unity's built-in layer names, where a project's TagManager has none.
_BUILTIN_LAYERS = {0: "Default", 1: "TransparentFX", 2: "Ignore Raycast",
                   4: "Water", 5: "UI"}


def read_layer_names(root):
    """ProjectSettings/TagManager.asset `layers:` -> {index: name}."""
    names = dict(_BUILTIN_LAYERS)
    path = os.path.join(root or "", "ProjectSettings", "TagManager.asset")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return names
    m = re.search(r"(?m)^  layers:\s*\n((?:  - .*\n?)+)", text)
    if not m:
        return names
    for k, line in enumerate(m.group(1).splitlines()[:32]):
        nm = line[4:].strip()
        if nm:
            names[k] = nm
    return names


def desugar_layers(text, layer_names):
    """LayerMask as the int it is: `LayerMask` declarations are `int`,
    `mask.value` is `mask`, and `LayerMask.GetMask("A", ..)` /
    `NameToLayer("A")` are constants from the project's layer names (an
    unknown name is -1 / no bit, as in Unity). The `*All` queries' arrays --
    `RaycastHit2D[] hits = Physics2D.RaycastAll(..)` -- are lists, so
    `foreach` and `hits[i]` are the list's; `.Length` is `.Count`."""
    import tools.cs2cpp as cs2cpp
    if "LayerMask" not in text and "All(" not in text:
        return text
    by_name = {v: k for k, v in layer_names.items()}

    def lits(args):
        return re.findall(r'"((?:[^"\\]|\\.)*)"', args)
    text = re.sub(
        r"(?<![\w.])(?:UnityEngine\s*\.\s*)?LayerMask\s*\.\s*GetMask\s*\(([^()]*)\)",
        lambda m: str(sum(1 << by_name[n] for n in set(lits(m.group(1)))
                          if n in by_name)), text)
    text = re.sub(
        r"(?<![\w.])(?:UnityEngine\s*\.\s*)?LayerMask\s*\.\s*NameToLayer\s*\(\s*"
        r'"((?:[^"\\]|\\.)*)"\s*\)',
        lambda m: str(by_name.get(m.group(1), -1)), text)
    scan = cs2cpp._blank(text)
    masks = set(re.findall(r"(?<![\w.])(?:UnityEngine\s*\.\s*)?LayerMask\s+(\w+)",
                           scan))
    text = cs2cpp.code_sub(r"(?<![\w.])(?:UnityEngine\s*\.\s*)?LayerMask(?=\s+\w)",
                           "int", text)
    for n in sorted(masks):
        text = cs2cpp.code_sub(r"(?<![\w.])(%s)\s*\.\s*value\b" % re.escape(n),
                               lambda m: m.group(1), text)
    arrays = set()
    for m in re.finditer(r"(?<![\w.])(RaycastHit2D|Collider2D)\s*\[\s*\]\s+(\w+)"
                         r"(?=\s*=\s*(?:UnityEngine\s*\.\s*)?Physics2D\s*\.\s*"
                         r"\w+All\s*\()", cs2cpp._blank(text)):
        arrays.add(m.group(2))
    text = cs2cpp.code_sub(
        r"(?<![\w.])(RaycastHit2D|Collider2D)\s*\[\s*\](\s+\w+\s*=\s*"
        r"(?:UnityEngine\s*\.\s*)?Physics2D\s*\.\s*\w+All\s*\()",
        lambda m: "List<%s>%s" % (m.group(1), m.group(2)), text)
    for n in sorted(arrays):
        text = cs2cpp.code_sub(r"(?<![\w.])(%s)\s*\.\s*Length\b" % re.escape(n),
                               lambda m: "%s.Count" % m.group(1), text)
    return text
