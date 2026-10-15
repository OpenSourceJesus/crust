"""unity_pack: Tilemap + TilemapRenderer -- baked tiles, drawn as sprites.

A scene Tilemap's tiles (m_Tiles: cell, sprite, matrix and color indices into
m_TileSpriteArray / m_TileMatrixArray / m_TileColorArray) are baked at pack
time into world-space sprite draws: the cell's anchor point through its Grid
(Rectangle layout: cell * (m_CellSize + m_CellGap) + m_TileAnchor *
m_CellSize), the tile's matrix, then the Tilemap's world transform. The
sprite's pivot sits on that point, as Unity draws a tile. Color is the tile's
times the Tilemap's m_Color; sorting is the TilemapRenderer's.

ponytail: static -- the authored transforms, active state and tiles; a script
moving, toggling or editing a Tilemap (SetTile, color) does not reach it, and
a tile asset's animation is not played. Only scene Tilemaps (not one in a
prefab a script Instantiates) are drawn.
"""
import re


class TilemapError(Exception):
    """A Tilemap this pack cannot draw as Unity does."""


def _section(block, key):
    m = re.search(r"(?ms)^  %s:[ \t]*\n(.*?)(?=^  \w|\Z)" % re.escape(key), block)
    return m.group(1) if m else ""


def _entries(sec):
    return re.split(r"(?m)^  - ", sec)[1:]


def _rgba(text, default=(1.0, 1.0, 1.0, 1.0)):
    m = re.search(r"\{r:\s*([^,}]+),\s*g:\s*([^,}]+),\s*b:\s*([^,}]+),"
                  r"\s*a:\s*([^}]+)\}", text or "")
    return tuple(float(m.group(i)) for i in range(1, 5)) if m else default


def _vec(block, key, default):
    m = re.search(r"(?m)^  %s:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+)" % key, block)
    return (float(m.group(1)), float(m.group(2))) if m else default


def parse_tilemap(block):
    tiles = []
    for e in _entries(_section(block, "m_Tiles")):
        c = re.search(r"first:\s*\{x:\s*(-?\d+),\s*y:\s*(-?\d+)", e)
        idx = {k: int(v) for k, v in re.findall(
            r"m_Tile(Sprite|Matrix|Color)Index:\s*(\d+)", e)}
        if c:
            tiles.append((int(c.group(1)), int(c.group(2)), idx.get("Sprite", 0),
                          idx.get("Matrix", 0), idx.get("Color", 0)))
    sprites = []
    for e in _entries(_section(block, "m_TileSpriteArray")):
        m = re.search(r"m_Data:\s*\{fileID:\s*(-?\d+)(?:,\s*guid:\s*([0-9a-fA-F]+))?", e)
        sprites.append((int(m.group(1)), m.group(2).lower())
                       if m and m.group(2) and m.group(1) != "0" else None)
    mats = []
    for e in _entries(_section(block, "m_TileMatrixArray")):
        ev = {k: float(v) for k, v in re.findall(r"e(\d\d):\s*([-0-9.eE+]+)", e)}
        mats.append((ev.get("00", 1.0), ev.get("01", 0.0), ev.get("03", 0.0),
                     ev.get("10", 0.0), ev.get("11", 1.0), ev.get("13", 0.0)))
    colors = [_rgba(e) for e in _entries(_section(block, "m_TileColorArray"))]
    cm = re.search(r"(?m)^  m_Color:\s*(\{[^}]*\})", block)
    return {"tiles": tiles, "sprites": sprites, "mats": mats, "colors": colors,
            "color": _rgba(cm.group(1) if cm else None),
            "anchor": _vec(block, "m_TileAnchor", (0.5, 0.5))}


def parse_renderer(block):
    def num(key, default):
        m = re.search(r"(?m)^  %s:\s*(-?\d+)" % key, block)
        return int(m.group(1)) if m else default
    mat = re.search(r"(?m)^  m_Materials:\s*\n\s+- \{fileID:\s*-?\d+,\s*guid:\s*"
                    r"([0-9a-fA-F]+)", block)
    return {"enabled": num("m_Enabled", 1),
            "sorting_layer_id": num("m_SortingLayerID", 0),
            "sorting_layer_yaml": num("m_SortingLayer", 0),
            "sorting_order": num("m_SortingOrder", 0),
            # URP's Sprite-Lit-Default: lit by the 2D lights
            "lit": int(bool(mat) and mat.group(1).lower()
                       == "a97c105638bdf8b4a8650670310a4cd3")}


def parse_grid(block, file_id):
    def num(key):
        m = re.search(r"(?m)^  %s:\s*(-?\d+)" % key, block)
        return int(m.group(1)) if m else 0
    if num("m_CellLayout") or num("m_CellSwizzle"):
        raise TilemapError(
            "Grid &%s: cell layout %d / swizzle %d -- only the Rectangle "
            "layout in XYZ is drawn" % (file_id, num("m_CellLayout"),
                                        num("m_CellSwizzle")))
    return {"cell": _vec(block, "m_CellSize", (1.0, 1.0)),
            "gap": _vec(block, "m_CellGap", (0.0, 0.0))}


def _go_of(rec):
    m = re.search(r"(?m)^\s+m_GameObject:\s*\{fileID:\s*(\d+)\}", rec.get("raw") or "")
    return m.group(1) if m else None


def bake_tiles(by_id, world_trs, quat_xy_basis):
    """Each drawn tile of the scene's Tilemaps: a sprite dict (world x / y,
    the basis m00..m11 with its scale apart in scale_x / scale_y)."""
    xf_of, comp_of = {}, {}
    for rec in by_id.values():
        go = _go_of(rec)
        if go is None:
            continue
        if rec.get("kind") == "Transform":
            xf_of[go] = rec
        elif rec.get("tilemap_part"):
            comp_of.setdefault(go, {})[rec["tilemap_part"]] = rec[rec["tilemap_part"]]
    go_of_xf = {str(x.get("file_id")): g for g, x in xf_of.items()}
    out = []
    for go, comps in comp_of.items():
        tm, rd = comps.get("tilemap"), comps.get("tilemap_renderer")
        xf = xf_of.get(go)
        if not tm or not rd or not rd["enabled"] or xf is None:
            continue
        grid, active, cur, seen = None, True, str(xf.get("file_id")), set()
        while cur and cur != "0" and cur in by_id and cur not in seen:
            seen.add(cur)
            g = go_of_xf.get(cur)
            if g is not None:
                active = active and int((by_id.get(g) or {}).get("active", 1)) != 0
                grid = grid or (comp_of.get(g) or {}).get("grid")
            cur = by_id[cur].get("father_id")
        if not active:
            continue
        grid = grid or {"cell": (1.0, 1.0), "gap": (0.0, 0.0)}
        (px, py, pz), rot, scale = world_trs(xf["file_id"])
        r00, r01, r10, r11 = quat_xy_basis(*rot)
        sx, sy = float(scale[0]), float(scale[1])
        (cw, ch), (gw, gh), (ax, ay) = grid["cell"], grid["gap"], tm["anchor"]
        for x, y, si, mi, ci in tm["tiles"]:
            spr = tm["sprites"][si] if si < len(tm["sprites"]) else None
            if spr is None:
                continue
            t00, t01, t03, t10, t11, t13 = (
                tm["mats"][mi] if mi < len(tm["mats"]) else (1, 0, 0, 0, 1, 0))
            col = tm["colors"][ci] if ci < len(tm["colors"]) else (1, 1, 1, 1)
            lx = (x * (cw + gw) + ax * cw + t03) * sx
            ly = (y * (ch + gh) + ay * ch + t13) * sy
            # A = R * diag(sx, sy) * T: its columns' lengths are the scale
            a00, a01 = (r00 * sx * t00 + r01 * sy * t10, r00 * sx * t01 + r01 * sy * t11)
            a10, a11 = (r10 * sx * t00 + r11 * sy * t10, r10 * sx * t01 + r11 * sy * t11)
            n0 = (a00 * a00 + a10 * a10) ** 0.5 or 1.0
            n1 = (a01 * a01 + a11 * a11) ** 0.5 or 1.0
            out.append(dict(
                rd, enabled=1, has_sprite=True, tile=True,
                sprite_file_id=spr[0], sprite_guid=spr[1],
                x=px + r00 * lx + r01 * ly, y=py + r10 * lx + r11 * ly, z=pz,
                layer=int((by_id.get(go) or {}).get("layer") or 0),
                m00=a00 / n0, m01=a01 / n1, m10=a10 / n0, m11=a11 / n1,
                scale_x=n0, scale_y=n1,
                r=col[0] * tm["color"][0], g=col[1] * tm["color"][1],
                b=col[2] * tm["color"][2], a=col[3] * tm["color"][3]))
    return out


def _f(v):
    t = "%.9g" % float(v)
    if "." not in t and "e" not in t and "n" not in t:
        t += ".0"
    return t + "f"


def emit_collect(p, plan, multi_scene):
    """_tm_collect: the tiles in the camera's view, into the draw list."""
    tiles = plan.get("tiles") or []
    if not tiles:
        return

    def table(ty, name, vals):
        p("static const %s %s[%d] = { %s };" % (ty, name, len(tiles), ", ".join(vals)))
    # farther first: the GPU path keeps list order within a layer and order
    tiles = sorted(tiles, key=lambda t: -float(t.get("z") or 0.0))
    for k in ("x", "y", "z", "half_w", "half_h", "m00", "m01", "m10", "m11", "r", "g", "b", "a"):
        table("float", "_tm_" + k, [_f(t[k]) for t in tiles])
    table("float", "_tm_pvx", [_f(1.0 - 2.0 * float((t.get("pivot") or (0.5, 0.5))[0]))
                               for t in tiles])
    table("float", "_tm_pvy", [_f(1.0 - 2.0 * float((t.get("pivot") or (0.5, 0.5))[1]))
                               for t in tiles])
    for k in ("tex_id", "sorting_layer", "sorting_order", "lit", "layer"):
        table("int", "_tm_" + k, [str(int(t.get(k) or 0)) for t in tiles])
    if multi_scene:
        table("int", "_tm_scene", [str(int(t.get("scene") or 0)) for t in tiles])
    has_cam = bool(plan.get("camera"))
    p("static void _tm_collect(EngineDraw *out, int *n, int max) {")
    p("    int k;")
    if has_cam:
        p("    float vh = Camera_main_orthographicSize, vw, aspect = Camera_main_aspect;")
        p("    if (aspect < 1e-6f) {")
        p("        float sw = (float)Screen_width, sh = (float)Screen_height;")
        p("        aspect = (sw < 1.f ? 1.f : sw) / (sh < 1.f ? 1.f : sh);")
        p("    }")
        p("    vw = vh * aspect;")
    p("    for (k = 0; k < %d && *n < max; k = k + 1) {" % len(tiles))
    p("        EngineDraw *d = &out[*n];")
    p("        float dx = _tm_pvx[k] * _tm_half_w[k], dy = _tm_pvy[k] * _tm_half_h[k];")
    if multi_scene:
        p("        if (!_engine_scene_loaded[_tm_scene[k]]) continue;")
    p("        d->x = _tm_x[k] + _tm_m00[k] * dx + _tm_m01[k] * dy;")
    p("        d->y = _tm_y[k] + _tm_m10[k] * dx + _tm_m11[k] * dy;")
    if has_cam:
        # ponytail: a bounding square (half_w + half_h) around the quad
        p("        if (fabsf(d->x - Camera_main_pos_x) > vw + _tm_half_w[k] + _tm_half_h[k]")
        p("            || fabsf(d->y - Camera_main_pos_y) > vh + _tm_half_w[k] + _tm_half_h[k])")
        p("            continue;")
    p("        d->half_w = _tm_half_w[k]; d->half_h = _tm_half_h[k];")
    p("        d->m00 = _tm_m00[k]; d->m01 = _tm_m01[k];")
    p("        d->m10 = _tm_m10[k]; d->m11 = _tm_m11[k];")
    p("        d->r = _tm_r[k]; d->g = _tm_g[k]; d->b = _tm_b[k]; d->a = _tm_a[k];")
    p("        d->tex = _tm_tex_id[k];")
    p("        d->sorting_layer = _tm_sorting_layer[k];")
    p("        d->sorting_order = _tm_sorting_order[k];")
    p("        d->z = _tm_z[k];")
    p("        d->flags = _tm_lit[k];")
    p("        d->go = -1;")
    p("        d->layer = _tm_layer[k];")
    p("        *n = *n + 1;")
    p("    }")
    p("}")
    p("")
