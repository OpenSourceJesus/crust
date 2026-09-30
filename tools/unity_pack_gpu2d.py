"""unity_pack: the 2D GPU path -- one atlas, 16-bit sprites, one draw call.

`pack(.., gpu_batch=True)` (`--gpu-batch`) adds, beside the per-texture
tables every pack has:

* an atlas: PIL packs every sprite texture into one power-of-two square
  page (shelf packing, tallest first), each image's edge pixels extruded
  into its padding so neither a nearest nor a linear sample bleeds into its
  neighbour; a pack too big for one MAX_SIDE page has more, the layers of a
  texture array (still one bind). The pages are written as atlas<N>.png
  beside the pack, to look at.
* the sprite table: each texture's rectangle in the atlas as four uint16
  (u0, v0, u1, v1 normalized to 0..65535) and its page, looked up in the
  shader by the sprite's 16-bit index;
* `engine_collect_gpu_sprites()`: the engine's draw list -- sorted as
  Unity sorts it -- packed 24 bytes a sprite:

      float x, y;            world position
      half  hw, hh;          half extents (a flip: negative)
      u16   sprite;          the sprite table's index (0xFFFF: no texture)
      s16   rot;             rotation, 65536 a turn
      u8    r, g, b, a;      tint
      u8    layer;           sorting layer (the 2D lights' target layers)
      u8    flags;           GPU_SPRITE_LIT, ..
      u8    effect, arg;     a 2D effect and its parameter (0: none)

  so a renderer draws them all as instances of one quad: one
  glDrawArraysInstanced (examples/unity_pack/gles3_batch.h).
"""
import os

#: The largest atlas page (GLES3 guarantees 2048; 4096 is near universal).
MAX_SIDE = 4096
PAD = 2
#: engine flags
GPU_SPRITE_LIT = 1


def _next_pow2(n):
    k = 1
    while k < n:
        k <<= 1
    return k


def _shelf_pack(sizes, side):
    """Place (w, h) boxes (each plus 2 * PAD) on shelves of a side x side
    page, tallest first: {index: (x, y)} for those placed, in order."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0]))
    placed = {}
    x = y = shelf_h = 0
    for i in order:
        w, h = sizes[i][0] + 2 * PAD, sizes[i][1] + 2 * PAD
        if w > side or h > side:
            continue
        if x + w > side:
            y += shelf_h
            x = shelf_h = 0
        if y + h > side:
            continue
        placed[i] = (x + PAD, y + PAD)
        x += w
        shelf_h = max(shelf_h, h)
    return placed


def _paste_extruded(page, img, x, y):
    """img at (x, y), its edges extruded into the PAD border."""
    w, h = img.size
    page.paste(img, (x, y))
    for d in range(1, PAD + 1):
        page.paste(img.crop((0, 0, w, 1)), (x, y - d))
        page.paste(img.crop((0, h - 1, w, h)), (x, y + h - 1 + d))
        page.paste(page.crop((x, y - PAD, x + 1, y + h + PAD)), (x - d, y - PAD))
        page.paste(page.crop((x + w - 1, y - PAD, x + w, y + h + PAD)),
                   (x + w - 1 + d, y - PAD))


def normal_maps(textures, root):
    """Each texture's URP normal map (its .meta's secondary texture
    `_NormalMap`), as RGBA its size (a sheet slice cropped as the sprite
    is), or None."""
    import re
    from tools.unity_pack_sprites import (_load_png_rgba, _parse_sprite_sheet,
                                          _crop_rgba)
    guid_path = {}
    for dp, dns, fns in os.walk(root or "."):
        dns[:] = [d for d in dns if d not in ("Library", "Temp", "Logs", ".git")]
        for fn in fns:
            if fn.endswith(".png.meta"):
                try:
                    m = re.search(r"(?m)^guid:\s*([0-9a-fA-F]+)",
                                  open(os.path.join(dp, fn), errors="replace").read())
                except OSError:
                    continue
                if m:
                    guid_path[m.group(1).lower()] = os.path.join(dp, fn[:-5])
    out = []
    for t in textures:
        nrm = None
        try:
            meta = open((t.get("path") or "") + ".meta", errors="replace").read()
        except OSError:
            meta = ""
        m = re.search(r"-\s*name:\s*_NormalMap\s*\n\s*texture:\s*\{[^}]*guid:\s*"
                      r"([0-9a-fA-F]+)", meta)
        npath = guid_path.get(m.group(1).lower()) if m else None
        if npath:
            try:
                w, h, rgba = _load_png_rgba(npath)
                fid = int(t.get("file_id") or 0)
                if fid not in (0, 21300000):
                    e = _parse_sprite_sheet(t["path"]).get(fid)
                    if e:
                        w, h, rgba = _crop_rgba(rgba, w, h, e["x"], e["y"], e["w"], e["h"])
                nrm = (w, h, bytes(rgba))
            except Exception:
                nrm = None
        out.append(nrm)
    return out


def build_atlas(textures, outdir=None, root=None):
    """{"side", "pages": [RGBA bytes], "rects": [(page, x, y, w, h)]}."""
    from PIL import Image
    sizes = [(int(t["w"]), int(t["h"])) for t in textures]
    area = sum((w + 2 * PAD) * (h + 2 * PAD) for w, h in sizes)
    widest = max([max(w, h) + 2 * PAD for w, h in sizes] or [1])
    side = max(64, _next_pow2(max(int((area * 1.15) ** 0.5), widest)))
    side = min(side, MAX_SIDE)
    remaining = list(range(len(textures)))
    pages, rects = [], [None] * len(textures)
    while remaining:
        # the smallest power-of-two page that takes them all, up to MAX_SIDE
        while True:
            placed = _shelf_pack([sizes[i] for i in remaining], side)
            if len(placed) == len(remaining) or side >= MAX_SIDE:
                break
            side *= 2
        if not placed:
            raise ValueError("a texture is larger than the %d atlas page" % MAX_SIDE)
        page = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        for k, (x, y) in placed.items():
            i = remaining[k]
            w, h = sizes[i]
            img = Image.frombytes("RGBA", (w, h), bytes(textures[i]["rgba"]))
            # the edges extruded into the padding (bleed-free sampling)
            _paste_extruded(page, img, x, y)
            rects[i] = (len(pages), x, y, w, h)
        pages.append(page)
        remaining = [remaining[k] for k in range(len(remaining)) if k not in placed]
    # every page is the same side (a texture array's layers)
    side = max(p.size[0] for p in pages) if pages else 1
    # URP normal maps: a second atlas of the same layout (flat where a
    # sprite has none)
    normals = normal_maps(textures, root) if root else [None] * len(textures)
    normal_out = []
    if any(normals):
        npages = [Image.new("RGBA", (side, side), (128, 128, 255, 255)) for _p in pages]
        for i, nm in enumerate(normals):
            if not nm or not rects[i]:
                continue
            pg, x, y, w, h = rects[i]
            img = Image.frombytes("RGBA", (nm[0], nm[1]), nm[2])
            if img.size != (w, h):
                img = img.resize((w, h))
            _paste_extruded(npages[pg], img, x, y)
        for k, npg in enumerate(npages):
            if outdir:
                npg.save(os.path.join(outdir, "atlas%d_normal.png" % k))
            normal_out.append(npg.tobytes())
    out = []
    for k, pg in enumerate(pages):
        if pg.size[0] != side:
            big = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            big.paste(pg, (0, 0))
            pg = big
        if outdir:
            pg.save(os.path.join(outdir, "atlas%d.png" % k))
        out.append(pg.tobytes())
    return {"side": side, "pages": out, "rects": rects, "normal_pages": normal_out}


def _uv16(v, side):
    return max(0, min(65535, int(round(v * 65535.0 / side))))


def emit_data(p, plan):
    """data.c: the atlas pages and the sprite table."""
    atlas = plan.get("gpu_atlas")
    if not atlas:
        return
    side = atlas["side"]
    p("/* The 2D GPU path: the atlas (%d page(s) of %d x %d) and the sprite table */"
      % (len(atlas["pages"]), side, side))
    p("const int _engine_atlas_side = %d;" % side)
    p("const int _engine_atlas_pages = %d;" % len(atlas["pages"]))
    for k, page in enumerate(atlas["pages"]):
        p("const unsigned char _engine_atlas%d_rgba[%d] = {" % (k, len(page)))
        for off in range(0, len(page), 32):
            p("    " + ", ".join(str(b) for b in page[off:off + 32]) + ",")
        p("};")
    for k, page in enumerate(atlas.get("normal_pages") or []):
        p("const unsigned char _engine_atlas%d_normal_rgba[%d] = {" % (k, len(page)))
        for off in range(0, len(page), 32):
            p("    " + ", ".join(str(b) for b in page[off:off + 32]) + ",")
        p("};")
    rects = atlas["rects"] or [(0, 0, 0, 0, 0)]
    p("const unsigned short _engine_sprite_uv[%d] = {" % (4 * len(rects)))
    for pg, x, y, w, h in rects:
        p("    %d, %d, %d, %d," % (_uv16(x, side), _uv16(y, side),
                                  _uv16(x + w, side), _uv16(y + h, side)))
    p("};")
    p("const unsigned char _engine_sprite_page[%d] = { %s };" % (
        len(rects), ", ".join(str(r[0]) for r in rects)))
    p("const int _engine_sprite_count = %d;" % len(atlas["rects"]))


def emit_engine(p, plan):
    """engine.c: the accessors and engine_collect_gpu_sprites (after the
    draw list's EngineDraw and engine_collect_draws)."""
    atlas = plan.get("gpu_atlas")
    if not atlas:
        return
    np = len(atlas["pages"])
    p("/* ---- the 2D GPU path (tools/unity_pack_gpu2d.py) ---- */")
    p("extern const int _engine_atlas_side;")
    p("extern const int _engine_atlas_pages;")
    for k in range(np):
        p("extern const unsigned char _engine_atlas%d_rgba[];" % k)
    p("extern const unsigned short _engine_sprite_uv[];")
    p("extern const unsigned char _engine_sprite_page[];")
    p("extern const int _engine_sprite_count;")
    p("int engine_atlas_side(void) { return _engine_atlas_side; }")
    p("int engine_atlas_page_count(void) { return _engine_atlas_pages; }")
    p("const unsigned char *engine_atlas_rgba(int page) {")
    p("    switch (page) {")
    for k in range(np):
        p("    case %d: return _engine_atlas%d_rgba;" % (k, k))
    p("    default: return 0;")
    p("    }")
    p("}")
    nn = len(atlas.get("normal_pages") or [])
    for k in range(nn):
        p("extern const unsigned char _engine_atlas%d_normal_rgba[];" % k)
    p("/* URP normal maps, the atlas's layout (0: none in the pack) */")
    p("const unsigned char *engine_atlas_normal_rgba(int page) {")
    p("    switch (page) {")
    for k in range(nn):
        p("    case %d: return _engine_atlas%d_normal_rgba;" % (k, k))
    p("    default: return 0;")
    p("    }")
    p("}")
    p("int engine_sprite_count(void) { return _engine_sprite_count; }")
    p("const unsigned short *engine_sprite_uv_table(void) { return _engine_sprite_uv; }")
    p("const unsigned char *engine_sprite_page_table(void) { return _engine_sprite_page; }")
    p("/* IEEE half, round to nearest */")
    p("static unsigned short _gpu_half(float f) {")
    p("    union { float f; unsigned u; } v;")
    p("    unsigned s, e, m;")
    p("    v.f = f;")
    p("    s = (v.u >> 16) & 0x8000u;")
    p("    e = (v.u >> 23) & 0xffu;")
    p("    m = v.u & 0x7fffffu;")
    p("    if (e >= 143u) return (unsigned short)(s | 0x7bffu); /* the largest */")
    p("    if (e < 113u) return (unsigned short)s;               /* to zero */")
    p("    m = m + 0x1000u;")
    p("    if (m & 0x800000u) { m = 0; e = e + 1u; }")
    p("    return (unsigned short)(s | ((e - 112u) << 10) | (m >> 13));")
    p("}")
    p("static unsigned char _gpu_u8(float v) {")
    p("    if (!(v > 0.f)) return 0;")
    p("    if (v >= 1.f) return 255;")
    p("    return (unsigned char)(v * 255.f + 0.5f);")
    p("}")
    p("#define ENGINE_GPU_SCRATCH 8192")
    p("/* the draw list unsorted: engine_collect_gpu_sprites_stable's */")
    p("extern int _engine_draw_nosort;")
    p("static EngineDraw _engine_gpu_scratch[ENGINE_GPU_SCRATCH];")
    p("/* The draw list (sorted), packed for one instanced draw: a sprite's")
    p("   rotation and scale come from its local-to-world basis. */")
    p("int engine_collect_gpu_sprites(EngineGpuSprite *out, int max) {")
    p("    int n, k;")
    p("    if (!out || max < 1) return 0;")
    p("    /* a draw no source flagged (UI, particles) reads 0: unlit */")
    p("    memset(_engine_gpu_scratch, 0, sizeof _engine_gpu_scratch);")
    p("    n = engine_collect_draws(_engine_gpu_scratch,")
    p("                             max < ENGINE_GPU_SCRATCH ? max : ENGINE_GPU_SCRATCH);")
    p("    for (k = 0; k < n; k = k + 1) {")
    p("        const EngineDraw *d = &_engine_gpu_scratch[k];")
    p("        EngineGpuSprite *g = &out[k];")
    p("        float sx = sqrtf(d->m00 * d->m00 + d->m10 * d->m10);")
    p("        float sy = sqrtf(d->m01 * d->m01 + d->m11 * d->m11);")
    p("        float ang = atan2f(d->m10, d->m00);")
    p("        if (d->m00 * d->m11 - d->m01 * d->m10 < 0.f) sy = -sy; /* a flip */")
    p("        g->x = d->x;")
    p("        g->y = d->y;")
    p("        g->hw = _gpu_half(d->half_w * sx);")
    p("        g->hh = _gpu_half(d->half_h * sy);")
    p("        g->sprite = (unsigned short)(d->tex >= 0 ? d->tex : 0xffff);")
    p("        g->rot = (short)(int)floorf(ang * 10430.378f + 0.5f); /* 65536 / 2pi */")
    p("        g->r = _gpu_u8(d->r); g->g = _gpu_u8(d->g);")
    p("        g->b = _gpu_u8(d->b); g->a = _gpu_u8(d->a);")
    p("        g->layer = (unsigned char)d->sorting_layer;")
    p("        g->flags = (unsigned char)d->flags;")
    p("        g->effect = 0;")
    p("        g->arg = 0;")
    p("        if (d->go >= 0 && d->go < (int)sizeof _engine_go_fx) {")
    p("            g->effect = _engine_go_fx[d->go];")
    p("            g->arg = _engine_go_fx_arg[d->go];")
    p("        }")
    p("    }")
    p("    return n;")
    p("}")
    p("/* For a renderer that sorts on the GPU: the sprites in the engine's own")
    p("   stable order (class, instance -- no sort), each with its sort key:")
    p("   sorting layer << 16 | sorting order + 32768. Sorting (key, index)")
    p("   pairs reproduces the CPU order (layer, order, then list order). */")
    p("int engine_collect_gpu_sprites_stable(EngineGpuSprite *out, unsigned *keys, int max) {")
    p("    int n, k;")
    p("    _engine_draw_nosort = 1;")
    p("    n = engine_collect_gpu_sprites(out, max);")
    p("    _engine_draw_nosort = 0;")
    p("    for (k = 0; k < n && keys; k = k + 1) {")
    p("        int o = _engine_gpu_scratch[k].sorting_order + 32768;")
    p("        if (o < 0) o = 0;")
    p("        if (o > 65535) o = 65535;")
    p("        keys[k] = ((unsigned)(_engine_gpu_scratch[k].sorting_layer & 255) << 16)")
    p("                  | (unsigned)o;")
    p("    }")
    p("    return n;")
    p("}")
    p("")


#: engine_draw.h's part (the host's view of the above) -- completed below.
HEADER = """
/* ---- the 2D GPU path (a pack made with --gpu-batch) ---- */
typedef struct EngineGpuSprite {
    float x, y;               /* world position */
    unsigned short hw, hh;    /* half extents, IEEE half (negative: a flip) */
    unsigned short sprite;    /* the sprite table's index; 0xffff: no texture */
    short rot;                /* rotation, 65536 a turn */
    unsigned char r, g, b, a; /* tint */
    unsigned char layer;      /* sorting layer */
    unsigned char flags;      /* 1: lit by the 2D lights */
    unsigned char effect, arg;
} EngineGpuSprite;             /* 24 bytes */
int engine_atlas_side(void);
int engine_atlas_page_count(void);
const unsigned char *engine_atlas_rgba(int page);   /* side * side RGBA8 */
const unsigned char *engine_atlas_normal_rgba(int page); /* URP normal maps, or 0 */
int engine_sprite_count(void);
const unsigned short *engine_sprite_uv_table(void); /* u0 v0 u1 v1, 0..65535 */
const unsigned char *engine_sprite_page_table(void);
int engine_collect_gpu_sprites(EngineGpuSprite *out, int max);
/* unsorted (the engine's stable order), with each sprite's sort key */
int engine_collect_gpu_sprites_stable(EngineGpuSprite *out, unsigned *keys, int max);
/* 2D effects on a GameObject's sprites (SpriteEffects2D.Set in a script) */
void engine_set_sprite_effect(int go, int effect, float amount);
"""


# ---------------------------------------------------------------------------
# URP 2D lights (Light2D) and lit sprites
# ---------------------------------------------------------------------------

#: The URP material a lit sprite uses (Sprite-Lit-Default).
URP_SPRITE_LIT = "a97c105638bdf8b4a8650670310a4cd3"
#: Light2D.LightType: Parametric 0, Freeform 1, Sprite 2, Point 3, Global 4.
LIGHT_GLOBAL = 4
MAX_LIGHTS = 16
#: Freeform / Parametric polygon points, all lights together (the shader's
#: uniform array).
MAX_POINTS = 64
MAX_SHAPE_POINTS = 16


def parse_light2d(block):
    """A URP Light2D MonoBehaviour's YAML -> its record, or None."""
    import re
    if "m_LightType:" not in block or "m_Intensity:" not in block:
        return None

    def num(k, d):
        m = re.search(r"(?m)^\s+%s:\s*([-0-9.eE+]+)\s*$" % k, block)
        return float(m.group(1)) if m else float(d)
    col = re.search(r"m_Color:\s*\{r:\s*([^,]+),\s*g:\s*([^,]+),\s*b:\s*([^,]+),", block)
    layers = []
    lm = re.search(r"(?m)^  m_ApplyToSortingLayers:\s*\n((?:  - .*\n?)+)", block)
    if lm:
        layers = [int(v) for v in re.findall(r"-\s*(-?\d+)", lm.group(1))]
    elif re.search(r"(?m)^  m_ApplyToSortingLayers:\s*(\S+)", block):
        layers = []
    en = re.search(r"(?m)^  m_Enabled:\s*(\d)", block)
    return {
        "type": int(num("m_LightType", 3)),
        "color": tuple(float(col.group(k)) for k in (1, 2, 3)) if col else (1.0, 1.0, 1.0),
        "intensity": num("m_Intensity", 1.0),
        "inner": num("m_PointLightInnerRadius", 0.0),
        "outer": num("m_PointLightOuterRadius", 1.0),
        "inner_angle": num("m_PointLightInnerAngle", 360.0),
        "outer_angle": num("m_PointLightOuterAngle", 360.0),
        "falloff": num("m_FalloffIntensity", 0.5),
        "layers": layers,
        "enabled": int(en.group(1)) if en else 1,
        # a Freeform light's polygon (light-local), and its falloff size
        "shape_path": [(float(a), float(b)) for a, b in re.findall(
            r"-\s*\{x:\s*([-0-9.eE+]+),\s*y:\s*([-0-9.eE+]+)",
            (re.search(r"(?ms)^  m_ShapePath:\s*\n((?:  - .*\n?)+)", block) or
             re.match(r"()", "")).group(1))],
        "falloff_size": num("m_ShapeLightFalloffSize", 0.5),
        # a Parametric light (older URP): a regular polygon
        "sides": int(num("m_ShapeLightParametricSides", 6)),
        "param_radius": num("m_ShapeLightParametricRadius", 1.0),
        "param_angle": num("m_ShapeLightParametricAngleOffset", 0.0),
        # a Sprite light's cookie
        "cookie_guid": ((re.search(r"m_LightCookieSprite:\s*\{[^}]*guid:\s*([0-9a-fA-F]+)",
                                   block) or re.match(r"()", "")).group(1) or "").lower(),
        # NormalMapQuality: 0 Fast, 1 Accurate, 2 Disabled (the default)
        "normal_quality": int(num("m_NormalMapQuality", 2)),
        "normal_distance": num("m_NormalMapDistance", 3.0),
    }


def build_lights(plan, sorting_layers):
    """plan["lights2d"]: each enabled Light2D with its owner and the bit mask
    of the sorting layers (by index) it lights."""
    uid_index = {int(l.get("unique_id", 0)): k for k, l in enumerate(sorting_layers or [])}
    out = []
    for cname in sorted(plan["classes"]):
        for i, o in enumerate(plan["classes"][cname].get("instances") or []):
            for L in o.get("lights2d") or []:
                if not L.get("enabled", 1):
                    continue
                mask = 0
                for uid in L["layers"]:
                    k = uid_index.get(uid, 0 if uid == 0 else None)
                    if k is not None and k < 32:
                        mask |= 1 << k
                q = o.get("rot") or (0.0, 0.0, 0.0, 1.0)
                pos = o.get("pos") or (0.0, 0.0, 0.0)
                sc = o.get("local_scale") or o.get("scale") or (1.0, 1.0, 1.0)
                sx, sy = float(sc[0]), float(sc[1])
                shape = []
                if L["type"] == 1:
                    shape = [(x * sx, y * sy) for x, y in L["shape_path"]]
                elif L["type"] == 0:
                    import math
                    n = max(3, int(L["sides"]))
                    a0 = math.radians(L["param_angle"]) + math.pi / 2.0
                    shape = [(L["param_radius"] * math.cos(a0 + 2 * math.pi * k / n) * sx,
                              L["param_radius"] * math.sin(a0 + 2 * math.pi * k / n) * sy)
                             for k in range(n)]
                cookie, chw, chh = -1, 0.0, 0.0
                if L["type"] == 2:
                    ti = (plan.get("_cookie_tex") or {}).get(L.get("cookie_guid"))
                    if ti is not None:
                        t = plan["textures"][ti]
                        ppu = float(t.get("ppu") or 100.0)
                        cookie = int(ti)
                        chw = t["w"] / ppu * abs(sx) * 0.5
                        chh = t["h"] / ppu * abs(sy) * 0.5
                out.append(dict(L, owner_class=cname, owner_inst=i, mask=mask,
                                pos=(float(pos[0]), float(pos[1])),
                                rot_z=(2.0 * __import__("math").atan2(q[2], q[3])),
                                shape=shape[:MAX_SHAPE_POINTS], cookie=cookie,
                                cookie_half=(chw, chh)))
    plan["lights2d"] = out[:MAX_LIGHTS]


def emit_engine_lights(p, plan, class_ids, c_ident, class_has_position):
    """engine.c: engine_collect_lights2d -- each light's color * intensity,
    shape and target layers, at its owner's live position."""
    import math
    lights = plan.get("lights2d") or []
    p("/* Freeform / Parametric lights' polygons, world space, in light order:")
    p("   each light's shape_start / shape_count index these */")
    p("static float _engine_light_pts[%d];" % (2 * MAX_POINTS))
    p("static int _engine_light_npts;")
    p("const float *engine_light2d_points(int *count) {")
    p("    if (count) *count = _engine_light_npts;")
    p("    return _engine_light_pts;")
    p("}")
    p("int engine_collect_lights2d(EngineLight2D *out, int max) {")
    p("    int n = 0;")
    p("    if (!out) return 0;")
    p("    _engine_light_npts = 0;")
    for L in lights:
        cl = plan["classes"].get(L["owner_class"]) or {}
        idn = c_ident(L["owner_class"])
        live = class_has_position(cl) and not cl.get("static")
        p("    if (n < max) {")
        p("        EngineLight2D *l = &out[n];")
        if live:
            p("        l->x = %s_get_pos_x(%du); l->y = %s_get_pos_y(%du);"
              % (idn, L["owner_inst"], idn, L["owner_inst"]))
        else:
            p("        l->x = %sf; l->y = %sf;" % (repr(L["pos"][0]), repr(L["pos"][1])))
        c = L["color"]
        k = L["intensity"]
        p("        l->r = %sf; l->g = %sf; l->b = %sf;" % (
            repr(c[0] * k), repr(c[1] * k), repr(c[2] * k)))
        p("        l->type = %d;" % L["type"])
        p("        l->inner = %sf; l->outer = %sf;" % (repr(L["inner"]), repr(L["outer"])))
        # a spot's half angles, as cosines (a full circle: -1, no cone)
        ci = math.cos(math.radians(min(L["inner_angle"], 360.0) / 2.0))
        co = math.cos(math.radians(min(L["outer_angle"], 360.0) / 2.0))
        p("        l->cos_inner = %sf; l->cos_outer = %sf;" % (repr(ci), repr(co)))
        # the light's up axis, as its authored rotation turns it
        p("        l->dir_x = %sf; l->dir_y = %sf;" % (
            repr(-math.sin(L["rot_z"])), repr(math.cos(L["rot_z"]))))
        p("        l->falloff = %sf;" % repr(L["falloff"]))
        # its normal map distance (0: normal maps disabled for it)
        p("        l->normal_distance = %sf;" % repr(
            float(L["normal_distance"]) if L.get("normal_quality", 2) != 2 else 0.0))
        p("        l->layer_mask = %du;" % L["mask"])
        cr, sr = math.cos(L["rot_z"]), math.sin(L["rot_z"])
        p("        l->cos_r = %sf; l->sin_r = %sf;" % (repr(cr), repr(sr)))
        p("        l->falloff_size = %sf;" % repr(max(1e-4, float(L["falloff_size"]))))
        p("        l->cookie = %d;" % L.get("cookie", -1))
        p("        l->half_w = %sf; l->half_h = %sf;" % (
            repr(L["cookie_half"][0]), repr(L["cookie_half"][1])))
        shape = L.get("shape") or []
        p("        l->shape_start = _engine_light_npts;")
        p("        l->shape_count = 0;")
        if shape:
            p("        if (_engine_light_npts + %d <= %d) {" % (len(shape), MAX_POINTS))
            for (x, y) in shape:
                # rotated by the light's authored rotation, at its position
                p("            _engine_light_pts[2 * _engine_light_npts] = l->x + %sf;"
                  % repr(x * cr - y * sr))
                p("            _engine_light_pts[2 * _engine_light_npts + 1] = l->y + %sf;"
                  % repr(x * sr + y * cr))
                p("            _engine_light_npts = _engine_light_npts + 1;")
            p("            l->shape_count = %d;" % len(shape))
            p("        }")
        p("        n = n + 1;")
        p("    }")
    p("    (void)max;")
    p("    return n;")
    p("}")
    p("")


LIGHT_HEADER = """
/* URP 2D lights (Light2D), for the lit sprites (flags & 1) */
typedef struct EngineLight2D {
    float x, y;                  /* world position */
    float r, g, b;               /* color * intensity */
    int type;                    /* 3 point (a spot with its angles), 4 global */
    float inner, outer;          /* radii */
    float cos_inner, cos_outer;  /* a spot's half angles (-1: a full circle) */
    float dir_x, dir_y;          /* its up axis */
    float falloff;               /* m_FalloffIntensity */
    unsigned layer_mask;         /* the sorting layers (by index) it lights */
    float normal_distance;       /* the lit normal maps' light height; 0: off */
    float cos_r, sin_r;          /* its rotation */
    int shape_start, shape_count;/* Freeform / Parametric: its polygon's points */
    float falloff_size;          /* ... and how far outside it the light falls */
    int cookie;                  /* Sprite: its cookie's sprite index (-1: none) */
    float half_w, half_h;        /* ... and the cookie's half extents */
} EngineLight2D;
int engine_collect_lights2d(EngineLight2D *out, int max);
/* the Freeform lights' polygon points (x, y pairs), of the last collect */
const float *engine_light2d_points(int *count);
"""

HEADER = HEADER + LIGHT_HEADER


# ---------------------------------------------------------------------------
# 2D effects: the instance's effect byte
# ---------------------------------------------------------------------------

#: SpriteEffect2D (examples/unity_pack/SpriteEffects2D.cs) -> the effect byte.
EFFECTS = {"None": 0, "Flash": 1, "Grayscale": 2, "HueShift": 3, "Dissolve": 4,
           "Outline": 5}


def desugar_effects(text):
    """`SpriteEffects2D.Set(go, SpriteEffect2D.Flash, amount)` and
    `SpriteEffects2D.Clear(go)` -> `__sprite_fx(go, effect, amount)`, which
    the lowering makes engine_set_sprite_effect (amount 0..1 as 0..255; an
    Outline's is its width in texels). The stub class itself is left: its
    methods are empty."""
    import re
    if "SpriteEffects2D" not in text and "SpriteEffect2D" not in text:
        return text
    if re.search(r"\bstatic\s+class\s+SpriteEffects2D\b", text):
        return text
    text = re.sub(r"(?<![\w.])SpriteEffect2D\s*\.\s*(\w+)",
                  lambda m: str(EFFECTS.get(m.group(1), 0)), text)
    text = re.sub(r"(?<![\w.])SpriteEffects2D\s*\.\s*Clear\s*\(([^()]*)\)",
                  lambda m: "__sprite_fx(%s, 0, 0f)" % m.group(1).strip(), text)
    text = re.sub(r"(?<![\w.])SpriteEffects2D\s*\.\s*Set\s*\(", "__sprite_fx(", text)
    return text


def emit_effects(p, go_cap):
    """engine.c: the per-GameObject effect tables and their setter."""
    cap = max(1, int(go_cap))
    p("/* 2D effects, per GameObject: 1 flash, 2 grayscale, 3 hue shift,")
    p("   4 dissolve, 5 outline; the parameter a byte */")
    p("static unsigned char _engine_go_fx[%d], _engine_go_fx_arg[%d];" % (cap, cap))
    p("void engine_set_sprite_effect(int go, int effect, float amount) {")
    p("    int a;")
    p("    if (go < 0 || go >= %d) return;" % cap)
    p("    a = effect == 5 ? (int)(amount + 0.5f) : (int)(amount * 255.f + 0.5f);")
    p("    if (a < 0) a = 0;")
    p("    if (a > 255) a = 255;")
    p("    _engine_go_fx[go] = (unsigned char)(effect > 0 ? effect : 0);")
    p("    _engine_go_fx_arg[go] = (unsigned char)a;")
    p("}")
