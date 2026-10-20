# SPDX-License-Identifier: MIT
"""unity_pack: Physics: Rigidbody / Rigidbody2D / Collider tables, physics materials, collision
messages, and the Box2D-Packed checkout (box2d_unity.py) for 2D physics.

Moved out of tools/unity/unity_pack.py unchanged; unity_pack re-exports every name
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

from tools.unity.unity_pack_common import *  # noqa: E402,F401,F403

__all__ = [
    '_COLLISION2D_MSGS',
    '_JOINT2D_COMPONENTS',
    '_JOINT2D_BREAK_ACTIONS',
    '_JOINT2D_KINDS',
    '_build_joint2d_table',
    '_parse_joint2d',
    '_DEFAULT_MAT2D',
    '_DEFAULT_MAT3D',
    '_PHYSICS_COMPONENTS',
    '_COLLIDER2D_TYPES',
    '_build_collider2d_tables',
    '_triangulate_paths',
    '_build_collider3d_tables',
    '_build_rigidbody_tables',
    '_collision2d_arg_name',
    '_load_box2d_unity',
    '_load_physics_materials',
    '_parse_physic_material3d',
    '_parse_physics_material2d',
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

# The abstract 2D collider: a handle is its `_Collider2D_*` index, the same
# index a collision / trigger handler's parameter carries.
_COLLIDER2D_TYPES = frozenset(("Collider2D",))


# MonoBehaviour 2D collision messages (Unity Physics2D).
_TRIGGER2D_MSGS = ("OnTriggerEnter2D", "OnTriggerStay2D", "OnTriggerExit2D")

#: 2D joints Box2D-Packed builds, by `_Joint2D_kind`: revolute, distance
#: (rigid, or a rope with maxDistanceOnly), distance with a spring, weld,
#: prismatic, wheel.
_JOINT2D_KINDS = {"HingeJoint2D": 0, "DistanceJoint2D": 1, "SpringJoint2D": 2,
                  "FixedJoint2D": 3, "SliderJoint2D": 4, "WheelJoint2D": 5,
                  # Box2D-Packed's motor joint, three ways: friction (velocity
                  # control to rest), relative (a spring to an offset), target
                  # (a spring to a world point)
                  "FrictionJoint2D": 6, "RelativeJoint2D": 7, "TargetJoint2D": 8}

#: JointBreakAction2D: Ignore, CallbackOnly, Disable, Destroy (the default).
_JOINT2D_BREAK_ACTIONS = {"Ignore": 0, "CallbackOnly": 1, "Disable": 2,
                          "Destroy": 3}
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
        "frequency": num("m_Frequency", {2: 1.0, 5: 2.0, 8: 5.0}.get(k, 0.0)),
        "damping": num("m_DampingRatio", {5: 0.7, 8: 1.0}.get(k, 0.0)),
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
        "break_action": int(num("m_BreakAction", 3)),
        # friction / relative / target
        "max_force": num("m_MaxForce", {7: 10000.0, 8: 1000.0}.get(k, 0.0)),
        "max_torque": num("m_MaxTorque", {7: 10000.0}.get(k, 0.0)),
        "correction": num("m_CorrectionScale", 0.3),
        "auto_offset": int(num("m_AutoConfigureOffset", 1)),
        "offset": vec("m_LinearOffset"),
        "offset_angle": num("m_AngularOffset", 0.0),
        "auto_target": int(num("m_AutoConfigureTarget", 1)),
        "target": vec("m_Target"),
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
        or bool(plan.get("collider2d"))
        # Physics2D queries are the Box2D-Packed glue's, built with these
        or "Physics2D.query" in used_apis)


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
                    # an authored 0 is no gravity, not the default: `or`
                    # would read it as missing
                    "gravity_scale": float(r2["gravity_scale"]
                                           if r2.get("gravity_scale") is not None
                                           else 1.0),
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


def _triangulate_paths(paths):
    """PolygonCollider2D paths → triangles [((x,y),(x,y),(x,y)), ...] by ear clipping.

    ponytail: every path is solid (a path inside another is not cut out as a hole)
    and triangles stay triangles (not merged into convex polygons of up to 8, which
    would mean fewer Box2D shapes); O(n³) worst case, fine for authored outlines.
    """
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    tris = []
    for path in paths:
        pts = []
        for q in path:
            if not pts or q != pts[-1]:
                pts.append(q)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        area = sum(pts[i - 1][0] * q[1] - q[0] * pts[i - 1][1]
                   for i, q in enumerate(pts))
        if area < 0:
            pts.reverse()
        while len(pts) > 3:
            n = len(pts)
            for i in range(n):
                a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
                if cross(a, b, c) <= 0:
                    continue  # reflex or straight corner
                if any(cross(a, b, p) >= 0 and cross(b, c, p) >= 0
                       and cross(c, a, p) >= 0
                       for p in pts if p not in (a, b, c)):
                    continue  # another corner inside the ear
                tris.append((a, b, c))
                del pts[i]
                break
            else:
                break  # self-intersecting: keep what was cut
        if len(pts) == 3 and cross(*pts) > 0:
            tris.append(tuple(pts))
    return tris


#: Scripts that are a terrain chunk's collider: kind 5 in the collider table
#: (Box2D-Packed's terrain chunk, box2d_unity._with_terrain).
TERRAIN_COLLIDER_CLASSES = ("Box2DChunkCollider", "Box2DChainChunkCollider")
TERRAIN_KIND = 5
TERRAIN_DEFAULT_SHAPES = 256


def _build_collider2d_tables(plan):
    """Authored Box / Circle / CapsuleCollider2D → packed contact table."""
    cols = []
    class_ids = {n: i for i, n in enumerate(sorted(plan["classes"]))}
    # Map (class, inst) → rb2d index for dynamic flag.
    rb_of = {}
    for ri, r in enumerate(plan.get("rigidbody2d") or []):
        rb_of[(r["owner_class"], r["owner_inst"])] = ri
    # Unity: a collider sits on the Rigidbody2D of its GameObject or of the
    # nearest ancestor's (the glue offsets it from that body's origin)
    xf_rb, father = {}, {}
    for cname, cl in plan["classes"].items():
        for i, o in enumerate(cl.get("instances") or []):
            x = str(o.get("xf_id") or 0)
            if x == "0":
                continue
            father[x] = str(o.get("father_id") or 0)
            if (cname, i) in rb_of:
                xf_rb[x] = rb_of[(cname, i)]

    def ancestor_rb(o):
        x, seen = str(o.get("father_id") or 0), set()
        while x != "0" and x not in seen:
            if x in xf_rb:
                return xf_rb[x]
            seen.add(x)
            x = father.get(x, "0")
        return None
    for cname, cl in sorted(plan["classes"].items()):
        cid = class_ids[cname]
        for i, o in enumerate(cl.get("instances") or []):
            c = o.get("collider2d")
            if not c and cname in TERRAIN_COLLIDER_CLASSES:
                # destructible terrain: the chunk's shapes come from the
                # script at run time (Box2DTerrain.SetBoxes), no authored shape
                c = {"kind": "terrain", "enabled": 1}
                # the chunk's body turns with its GameObject (fixed at pack
                # time, as an authored collider's rotation is)
                q = o.get("rot") or (0.0, 0.0, 0.0, 1.0)
                qx, qy, qz, qw = q[0], q[1], q[2], q[3]
                # the planar angle of local +X (unity_pack._quat_z_rad)
                rz = math.atan2(2.0 * (qx * qy + qw * qz),
                                1.0 - 2.0 * (qy * qy + qz * qz))
                c["cos_z"], c["sin_z"] = math.cos(rz), math.sin(rz)
            if not c or not c.get("enabled", 1):
                continue
            rb_i = rb_of.get((cname, i))
            if rb_i is None:
                rb_i = xf_rb.get(str(o.get("xf_id") or 0))
            if rb_i is None:
                rb_i = ancestor_rb(o)
            body = 2  # static (no RB)
            if rb_i is not None:
                body = int((plan["rigidbody2d"][rb_i]).get("body_type") or 0)
            kind = {"box": 0, "capsule_v": 2, "capsule_h": 3,
                    "polygon": 4, "terrain": TERRAIN_KIND}.get(c.get("kind"), 1)
            if kind == 4:
                plan["physics2d_polygons"] = True
            if kind == TERRAIN_KIND:
                # a terrain chunk is a static body with no Rigidbody2D
                rb_i, body = None, 2
                plan["physics2d_terrain"] = True
                if cname == "Box2DChainChunkCollider":
                    plan["terrain2d_chains"] = True
                plan["terrain2d_max_shapes"] = max(
                    int(plan.get("terrain2d_max_shapes") or 0),
                    int(o.get("max_shapes") or TERRAIN_DEFAULT_SHAPES))
            cols.append({
                "tris": c.get("tris") or [],
                "name": o.get("name") or "obj",
                "file_id": c.get("file_id"),
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
                # ponytail: unscaled by the transform, as Unity hands Box2D
                # the scaled box and the raw radius (an assumption, unchecked
                # against a non-uniform scale)
                "edge_r": float(c.get("edge_radius") or 0.0),
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
                # Godot's collision_layer / collision_mask (godot_pack)
                "godot_layer": int(c.get("godot_layer", 1)),
                "godot_mask": int(c.get("godot_mask", 1)),
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


def find_box2d_root(box2d_root=None):
    """Box2D-Packed checkout: *box2d_root*, $BOX2D_PACKED_ROOT, or a ``box2d``
    directory beside this repository. None when there is none."""
    candidates = [box2d_root, os.environ.get("BOX2D_PACKED_ROOT"),
                  os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                      os.path.dirname(os.path.abspath(__file__))))), "box2d")]
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


def read_layer_matrix(root):
    """ProjectSettings/Physics2DSettings.asset `m_LayerCollisionMatrix`:
    32 little-endian uint32 words, word k the layers k collides with
    (`Physics2D.GetLayerCollisionMask(k)`); None without one."""
    path = os.path.join(root or "", "ProjectSettings", "Physics2DSettings.asset")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r"m_LayerCollisionMatrix:\s*([0-9a-fA-F]{256})",
                          f.read())
    except OSError:
        return None
    if not m:
        return None
    raw = bytes.fromhex(m.group(1))
    return [int.from_bytes(raw[4 * k:4 * k + 4], "little") for k in range(32)]


def has_standard_remove(texts):
    """True when the project's `LayerMask.Remove(params string[])` extension
    is the LayerMaskExtensions one: `~(~mask | FromLayerNames(..))`, with
    `FromLayerNames` OR-ing `1 << NameToLayer(name)`. Any other body is the
    project's own and is left for the lowering to refuse."""
    def body(name, sig, text):
        m = re.search(r"static\s+LayerMask\s+%s\s*\(%s\)\s*(\{[^{}]*\})"
                      % (name, sig), text)
        return m and re.sub(r"\s+", "", m.group(m.lastindex))
    for t in texts:
        rm = body("Remove", r"\s*this\s+LayerMask\s+(\w+)\s*,\s*params\s+"
                  r"string\s*\[\s*\]\s*(\w+)\s*", t)
        fl = body("FromLayerNames", r"\s*params\s+string\s*\[\s*\]\s*\w+\s*", t)
        if rm and fl and re.fullmatch(
                r"\{LayerMask(\w+)=~\w+;return~\(\1\|FromLayerNames\(\w+\)\);\}",
                rm) and re.fullmatch(
                r"\{LayerMask(\w+)=\(LayerMask\)0;foreach\(string(\w+)in\w+\)"
                r"\1\|=\(1<<LayerMask\.NameToLayer\(\2\)\);return\1;\}", fl):
            return True
    return False
