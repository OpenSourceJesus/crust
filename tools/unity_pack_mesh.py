"""unity_pack: MeshFilter / MeshRenderer -- textured triangles.

A GameObject with a MeshFilter and a MeshRenderer draws its mesh: here, the
mesh's triangles in world space (its transform composed up its parents, as
sprites are), textured with the renderer's material's `_MainTex`, sorted with
the sprites by `m_SortingOrder`, skipped while the GameObject is not active
in the hierarchy. Hosts take them from `engine_collect_tris` beside
`engine_collect_draws`.

The mesh is read from Unity's serialised form (`--- !u!43 Mesh` in the scene,
the form a mesh made in the editor or by a script and saved takes):
`m_VertexData` -- 14 channel descriptors (position, normal, tangent, colour,
uv0..uv7, blend weight, blend indices), each a stream, offset, format and
dimension, over `_typelessdata`, the interleaved vertex bytes in hex -- and
`m_IndexBuffer`, hex 16- or 32-bit indices (`m_IndexFormat` 0 / 1). Position
and uv0 as float32 in stream 0 with triangle topology are read, which is what
generated meshes -- Unity-2D-Destruction's fragments -- use; anything else is
refused at the scene, never drawn wrong. A 2D pack: z is dropped.
"""
import re
import struct

#: Unity's vertex attribute order in m_Channels (2019+).
CH_POSITION, CH_UV0 = 0, 4
#: VertexAttributeFormat byte sizes (Float32, Float16, UNorm8, SNorm8,
#: UNorm16, SNorm16, UInt8, SInt8, UInt16, SInt16, UInt32, SInt32).
_FORMAT_SIZE = (4, 2, 1, 1, 2, 2, 1, 1, 2, 2, 4, 4)


class MeshError(Exception):
    """A mesh this pack cannot draw as Unity does."""


def _num(text, key, default=None):
    m = re.search(r"(?m)^\s*%s:\s*(-?[0-9.eE+]+)\s*$" % re.escape(key), text)
    if m is None:
        if default is None:
            raise MeshError("no `%s`" % key)
        return default
    return float(m.group(1))


def parse_mesh(block):
    """A `Mesh:` block -> {"name", "verts": [(x, y)], "uv": [(u, v)],
    "tris": [i, ..]}."""
    nm = re.search(r"(?m)^\s*m_Name:\s*(.*)$", block)
    name = nm.group(1).strip() if nm else "mesh"
    for sm in re.finditer(r"(?m)^\s*topology:\s*(\d+)", block):
        if int(sm.group(1)) != 0:
            raise MeshError("mesh `%s`: a submesh of topology %s (only triangles, "
                            "0, are drawn)" % (name, sm.group(1)))
    vd = re.search(r"(?ms)^  m_VertexData:\s*\n(.*?)(?=^  m_\w+:|\Z)", block)
    if vd is None:
        raise MeshError("mesh `%s` has no m_VertexData" % name)
    vtext = vd.group(1)
    vc = int(_num(vtext, "m_VertexCount", 0))
    chans = [tuple(int(v) for v in m.groups()) for m in re.finditer(
        r"(?m)^\s*-\s*stream:\s*(\d+)\s*\n\s*offset:\s*(\d+)\s*\n\s*format:\s*(\d+)\s*\n"
        r"\s*dimension:\s*(\d+)", vtext)]
    if len(chans) <= CH_POSITION or chans[CH_POSITION][3] < 2:
        raise MeshError("mesh `%s` has no position channel" % name)
    stride = 0
    for st, off, fmt, dim in chans:
        if dim and st == 0:
            stride = max(stride, off + _FORMAT_SIZE[fmt] * dim)
    for idx in (CH_POSITION, CH_UV0):
        if idx < len(chans) and chans[idx][3]:
            st, _off, fmt, _dim = chans[idx]
            if st != 0 or fmt != 0:
                raise MeshError("mesh `%s`: %s is not float32 in stream 0"
                                % (name, "position" if idx == CH_POSITION else "uv0"))
    hm = re.search(r"(?m)^\s*_typelessdata:\s*([0-9a-fA-F]*)\s*$", vtext)
    data = bytes.fromhex(hm.group(1)) if hm else b""
    if len(data) < vc * stride:
        raise MeshError("mesh `%s`: %d vertex bytes for %d vertices of %d"
                        % (name, len(data), vc, stride))

    def read(ch, k):
        st, off, fmt, dim = chans[ch]
        return struct.unpack_from("<%df" % dim, data, k * stride + off)
    verts = [read(CH_POSITION, k)[:2] for k in range(vc)]
    has_uv = len(chans) > CH_UV0 and chans[CH_UV0][3] >= 2
    uv = [read(CH_UV0, k)[:2] if has_uv else (0.0, 0.0) for k in range(vc)]
    im = re.search(r"(?m)^\s*m_IndexBuffer:\s*([0-9a-fA-F]*)\s*$", block)
    ib = bytes.fromhex(im.group(1)) if im else b""
    wide = int(_num(block, "m_IndexFormat", 0)) == 1
    tris = list(struct.unpack("<%d%s" % (len(ib) // (4 if wide else 2), "I" if wide else "H"), ib))
    if len(tris) % 3 or any(i >= vc for i in tris):
        raise MeshError("mesh `%s`: its index buffer is not whole triangles of its "
                        "%d vertices" % (name, vc))
    return {"name": name, "verts": verts, "uv": uv, "tris": tris}


def write_mesh(name, verts, uv, tris):
    """The `Mesh:` YAML body for 2D triangles, as Unity serialises a mesh:
    position (float3) and uv0 (float2) interleaved in stream 0, 16-bit
    indices. The inverse of parse_mesh (the cave generator uses it)."""
    data = b"".join(struct.pack("<3f2f", x, y, 0.0, u, v)
                    for (x, y), (u, v) in zip(verts, uv))
    chans = []
    for i in range(14):
        if i == CH_POSITION:
            chans.append((0, 0, 0, 3))
        elif i == CH_UV0:
            chans.append((0, 12, 0, 2))
        else:
            chans.append((0, 0, 0, 0))
    lines = ["Mesh:", "  m_Name: %s" % name, "  serializedVersion: 10", "  m_SubMeshes:",
             "  - serializedVersion: 2", "    firstByte: 0", "    indexCount: %d" % len(tris),
             "    topology: 0", "    baseVertex: 0", "    firstVertex: 0",
             "    vertexCount: %d" % len(verts), "  m_IndexFormat: 0",
             "  m_IndexBuffer: %s" % struct.pack("<%dH" % len(tris), *tris).hex(),
             "  m_VertexData:", "    serializedVersion: 3",
             "    m_VertexCount: %d" % len(verts), "    m_Channels:"]
    for st, off, fmt, dim in chans:
        lines += ["    - stream: %d" % st, "      offset: %d" % off,
                  "      format: %d" % fmt, "      dimension: %d" % dim]
    lines += ["    m_DataSize: %d" % len(data), "    _typelessdata: %s" % data.hex()]
    return "\n".join(lines) + "\n"


def material_main_tex(text):
    """A `.mat`'s `_MainTex` texture guid, or None."""
    m = re.search(r"(?ms)-\s*_MainTex:\s*\n\s*m_Texture:\s*\{[^}]*guid:\s*(\w+)", text)
    return m.group(1) if m else None


def build_table(plan, objects):
    """plan["meshes"] (each distinct mesh) and plan["mesh_draws"] (each
    object that draws one: its class row, GameObject, mesh, texture,
    colour, sorting)."""
    meshes, by_key, draws = [], {}, []
    for cname in sorted(plan["classes"]):
        for i, o in enumerate(plan["classes"][cname].get("instances") or []):
            mr = o.get("mesh_draw")
            if not mr:
                continue
            key = mr["key"]
            if key not in by_key:
                by_key[key] = len(meshes)
                meshes.append(mr["mesh"])
            draws.append({"class": cname, "inst": i, "go": o.get("go_index"),
                          "mesh": by_key[key], "tex": mr.get("tex_id", -1),
                          "order": mr.get("order", 0), "layer": 0})
    plan["meshes"] = meshes
    plan["mesh_draws"] = draws


def _f(v):
    t = "%.9g" % float(v)
    if "." not in t and "e" not in t and "n" not in t:
        t += ".0"
    return t + "f"


def emit_collect(p, plan, class_ids, c_ident, has_basis):
    """`engine_collect_tris`: every active mesh's triangles, in world space.
    Emitted always (0 triangles without meshes), so a host can call it."""
    meshes = plan.get("meshes") or []
    draws = plan.get("mesh_draws") or []
    p("/* MeshFilter / MeshRenderer: textured triangles (tools/unity_pack_mesh.py) */")
    if not draws:
        p("int engine_collect_tris(EngineTri *out, int max) { (void)out; (void)max; return 0; }")
        p("")
        return
    xy, uv, idx, vb, ib, ic = [], [], [], [], [], []
    for m in meshes:
        vb.append(len(xy) // 2)
        ib.append(len(idx))
        ic.append(len(m["tris"]))
        for (x, y), (u, v) in zip(m["verts"], m["uv"]):
            xy += [x, y]
            uv += [u, v]
        idx += m["tris"]
    p("static const float _mesh_xy[%d] = { %s };" % (len(xy), ", ".join(_f(v) for v in xy)))
    p("static const float _mesh_uv[%d] = { %s };" % (len(uv), ", ".join(_f(v) for v in uv)))
    p("static const unsigned _mesh_idx[%d] = { %s };" % (len(idx), ", ".join(str(int(i)) for i in idx)))
    for nm, vals in (("vbegin", vb), ("ibegin", ib), ("icount", ic)):
        p("static const int _mesh_%s[%d] = { %s };" % (nm, len(vals), ", ".join(map(str, vals))))
    for nm, key in (("cls", "class"), ("inst", "inst"), ("go", "go"), ("mesh", "mesh"),
                    ("tex", "tex"), ("order", "order")):
        if key == "class":
            vals = [str(class_ids[d["class"]]) for d in draws]
        else:
            vals = [str(int(d[key])) if d[key] is not None else "-1" for d in draws]
        p("static const int _md_%s[%d] = { %s };" % (nm, len(draws), ", ".join(vals)))
    p("int engine_collect_tris(EngineTri *out, int max) {")
    p("    int n = 0, k, t;")
    p("    if (!out || max < 1) return 0;")
    p("    for (k = 0; k < %d; k = k + 1) {" % len(draws))
    p("        float px, py, pz, b[4];")
    p("        int m = _md_mesh[k], v0 = _mesh_vbegin[m], i0 = _mesh_ibegin[m];")
    p("        if (_md_go[k] >= 0 && !_engine_go_active_in_hierarchy(_md_go[k])) continue;")
    p("        _engine_world_pos(_md_cls[k], (unsigned)_md_inst[k], &px, &py, &pz, 0);")
    if has_basis:
        p("        _engine_world_basis(_md_cls[k], (unsigned)_md_inst[k], b, 0);")
    else:
        p("        b[0] = 1.f; b[1] = 0.f; b[2] = 0.f; b[3] = 1.f;")
    p("        for (t = 0; t + 2 < _mesh_icount[m] && n < max; t = t + 3) {")
    p("            EngineTri *o = &out[n];")
    p("            int c;")
    p("            for (c = 0; c < 3; c = c + 1) {")
    p("                int vi = v0 + (int)_mesh_idx[i0 + t + c];")
    p("                float x = _mesh_xy[2 * vi], y = _mesh_xy[2 * vi + 1];")
    p("                o->x[c] = px + b[0] * x + b[1] * y;")
    p("                o->y[c] = py + b[2] * x + b[3] * y;")
    p("                o->u[c] = _mesh_uv[2 * vi]; o->v[c] = _mesh_uv[2 * vi + 1];")
    p("            }")
    p("            o->r = 1.f; o->g = 1.f; o->b = 1.f; o->a = 1.f;")
    p("            o->tex = _md_tex[k];")
    p("            o->sorting_layer = 0; o->sorting_order = _md_order[k];")
    p("            n = n + 1;")
    p("        }")
    p("    }")
    p("    return n;")
    p("}")
    p("")
