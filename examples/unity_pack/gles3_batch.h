/* The 2D GPU path: every sprite in one draw call (GLES 3.1).
 *
 * For a pack made with --gpu-batch (tools/unity_pack_gpu2d.py):
 *
 *   atlas     the pages are the layers of one GL_TEXTURE_2D_ARRAY; URP normal
 *             maps a second array of the same layout
 *   table     each sprite's atlas rectangle (RGBA16UI) and page (R8UI),
 *             256 wide, fetched by its 16-bit index
 *   sprites   the engine's EngineGpuSprite (24 bytes each), drawn as
 *             instances of one four-corner strip: one glDrawArraysInstanced
 *   lights    URP 2D lights as uniform arrays: Global, Point (a spot with
 *             its angles), Freeform / Parametric (a polygon and its falloff)
 *             and Sprite (a cookie from the atlas); normal maps; up to 16
 *   effects   the instance's effect byte: flash, grayscale, hue shift,
 *             dissolve, outline
 *
 * Two ways to order the sprites:
 *
 *   default   the engine sorts (engine_collect_gpu_sprites); the instances
 *             are vertex attributes, only the changed runs uploaded
 *   GB_GPU_SORT  the engine hands them over unsorted, in its own stable
 *             order (engine_collect_gpu_sprites_stable), each with its sort
 *             key: the sprites are a shader storage buffer (still only the
 *             changed runs uploaded -- a moved body rewrites its own 24
 *             bytes, nothing shifts), a compute shader bitonic-sorts the
 *             (key, index) pairs -- only when a key changed -- and the vertex
 *             shader reads its sprite through the sorted index. A GPU that
 *             cannot read storage buffers in the vertex stage
 *             (GL_MAX_VERTEX_SHADER_STORAGE_BLOCKS 0, some ES 3.1 parts)
 *             falls back to the default.
 *
 * The camera, letterbox and clear are gles3_render.h's (the per-sprite
 * renderer): the two draw the same frame.
 */
#ifndef GLES3_BATCH_H
#define GLES3_BATCH_H

#include "gles3_render.h"
#include <stddef.h>
#include <string.h>

#ifndef GB_MAX_SPRITES
#define GB_MAX_SPRITES 8192
#endif
#define GB_MAX_LIGHTS 16
#define GB_MAX_POINTS 64
#define GB_SPRITE_WORDS 6          /* sizeof(EngineGpuSprite) / 4 */
#ifndef GL_MAX_VERTEX_SHADER_STORAGE_BLOCKS
#define GL_MAX_VERTEX_SHADER_STORAGE_BLOCKS 0x90D6
#endif

/* ---- shaders -------------------------------------------------------- */

/* The sprite's fields: vertex attributes, or (GPU_SORT) read from the
 * storage buffer through the sorted order. */
static const char *GB_VERT_SRC =
    "precision highp float;\n"
    "precision highp int;\n"
    "#ifdef GPU_SORT\n"
    "layout(std430, binding = 2) readonly buffer Sprites { uint sw[]; };\n"
    "layout(std430, binding = 3) readonly buffer Order { uvec2 ord[]; };\n"
    "#else\n"
    "layout(location = 0) in vec2 a_pos;\n"
    "layout(location = 1) in vec2 a_half;\n"
    "layout(location = 2) in uint a_sprite;\n"
    "layout(location = 3) in int a_rot;\n"
    "layout(location = 4) in vec4 a_color;\n"
    "layout(location = 5) in uvec4 a_attr;\n"
    "#endif\n"
    "uniform vec4 u_view;              /* left, bottom, right, top */\n"
    "uniform highp usampler2D u_rects; /* u0 v0 u1 v1, 0..65535 */\n"
    "uniform highp usampler2D u_pages;\n"
    "out vec4 v_color;\n"
    "out vec2 v_uv;\n"
    "flat out float v_page;\n"
    "flat out int v_textured;\n"
    "out vec2 v_world;\n"
    "flat out uint v_layer;\n"
    "flat out uint v_lit;\n"
    "flat out vec2 v_rot;\n"
    "flat out vec2 v_flip;\n"
    "flat out uvec2 v_fx;\n"
    "flat out vec4 v_rect;\n"
    "void main() {\n"
    "#ifdef GPU_SORT\n"
    "    uint b = ord[gl_InstanceID].y * 6u;\n"
    "    vec2 a_pos = vec2(uintBitsToFloat(sw[b]), uintBitsToFloat(sw[b + 1u]));\n"
    "    vec2 a_half = unpackHalf2x16(sw[b + 2u]);\n"
    "    uint a_sprite = sw[b + 3u] & 65535u;\n"
    "    int a_rot = int(sw[b + 3u]) >> 16;\n"
    "    vec4 a_color = unpackUnorm4x8(sw[b + 4u]);\n"
    "    uint w5 = sw[b + 5u];\n"
    "    uvec4 a_attr = uvec4(w5 & 255u, (w5 >> 8) & 255u, (w5 >> 16) & 255u, w5 >> 24);\n"
    "#endif\n"
    "    vec2 c = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));\n"
    "    vec2 local = (c * 2.0 - 1.0) * a_half;\n"
    "    float ang = float(a_rot) * (6.28318530718 / 65536.0);\n"
    "    float cs = cos(ang), sn = sin(ang);\n"
    "    vec2 w = a_pos + vec2(cs * local.x - sn * local.y, sn * local.x + cs * local.y);\n"
    "    gl_Position = vec4(2.0 * (w - u_view.xy) / (u_view.zw - u_view.xy) - 1.0, 0.0, 1.0);\n"
    "    v_color = a_color;\n"
    "    v_world = w;\n"
    "    v_layer = a_attr.x;\n"
    "    v_lit = a_attr.y & 1u;\n"
    "    v_fx = a_attr.zw;\n"
    "    v_rot = vec2(cs, sn);\n"
    "    v_flip = sign(a_half);\n"
    "    v_textured = a_sprite == 65535u ? 0 : 1;\n"
    "    ivec2 at = ivec2(int(a_sprite & 255u), int(a_sprite >> 8));\n"
    "    vec4 r = vec4(texelFetch(u_rects, at, 0)) / 65535.0;\n"
    "    v_uv = mix(r.xy, r.zw, c);\n"
    "    v_rect = r;\n"
    "    v_page = float(texelFetch(u_pages, at, 0).r);\n"
    "}\n";

/* URP 2D lighting for a lit sprite (Sprite-Lit-Default): the sum of the
 * lights that include its sorting layer multiplies its color (the default
 * Multiply blend style); a lit sprite no light reaches is black, as in
 * Unity. Per light:
 *   Global      its color
 *   Point       full inside the inner radius, falling to the outer (the
 *               falloff intensity shaping the curve); a spot also across
 *               its cone about the light's up axis
 *   Freeform /  full inside its polygon, falling over the falloff size by
 *   Parametric  the distance to the polygon's nearest edge
 *   Sprite      its cookie, a sprite of the atlas, over the cookie's rect
 * A light with normal maps on (not Global) is scaled by N . L. */
static const char *GB_FRAG_SRC =
    "precision mediump float;\n"
    "in vec4 v_color;\n"
    "in vec2 v_uv;\n"
    "flat in float v_page;\n"
    "flat in int v_textured;\n"
    "in highp vec2 v_world;\n"
    "flat in uint v_layer;\n"
    "flat in uint v_lit;\n"
    "flat in highp vec2 v_rot;\n"
    "flat in highp vec2 v_flip;\n"
    "flat in uvec2 v_fx;\n"
    "flat in highp vec4 v_rect;\n"
    "uniform mediump sampler2DArray u_atlas;\n"
    "uniform mediump sampler2DArray u_normals;\n"
    "uniform highp usampler2D u_rects;\n"
    "uniform highp usampler2D u_pages;\n"
    "uniform int u_has_normals;\n"
    "uniform highp float u_side;\n"
    "uniform int u_l_n;\n"
    "uniform highp vec4 u_l_a[16];  /* x, y, type, falloff intensity */\n"
    "uniform highp vec4 u_l_b[16];  /* r, g, b (color * intensity), inner radius */\n"
    "uniform highp vec4 u_l_c[16];  /* outer radius, cos inner, cos outer, dir x */\n"
    "uniform highp vec4 u_l_d[16];  /* dir y, layer mask (bits), normal distance, - */\n"
    "uniform highp vec4 u_l_e[16];  /* shape start, count, falloff size, cookie */\n"
    "uniform highp vec4 u_l_f[16];  /* cookie half w, half h, cos, sin */\n"
    "uniform highp vec2 u_pts[64];  /* the polygons' points, world space */\n"
    "layout(location = 0) out vec4 frag;\n"
    "highp float falloff(highp float t, highp float k) {\n"
    "    return pow(clamp(t, 0.0, 1.0), 0.5 + 2.0 * k);\n"
    "}\n"
    /* a polygon: 1 inside, else falling over the falloff size */
    "highp float shape_att(int i) {\n"
    "    int s = int(u_l_e[i].x), n = int(u_l_e[i].y);\n"
    "    bool inside = false;\n"
    "    highp float dmin = 1e9;\n"
    "    for (int k = 0; k < 16; k++) {\n"
    "        if (k >= n) break;\n"
    "        highp vec2 a = u_pts[s + k];\n"
    "        highp vec2 b = u_pts[s + (k + 1 == n ? 0 : k + 1)];\n"
    "        if (((a.y > v_world.y) != (b.y > v_world.y)) &&\n"
    "            v_world.x < (b.x - a.x) * (v_world.y - a.y) / (b.y - a.y) + a.x)\n"
    "            inside = !inside;\n"
    "        highp vec2 ab = b - a;\n"
    "        highp float h = clamp(dot(v_world - a, ab) / max(dot(ab, ab), 1e-8), 0.0, 1.0);\n"
    "        dmin = min(dmin, length(v_world - a - ab * h));\n"
    "    }\n"
    "    if (inside) return 1.0;\n"
    "    return falloff(1.0 - dmin / u_l_e[i].z, u_l_a[i].w);\n"
    "}\n"
    /* a cookie: the light's sprite over its rect (world -> the light's frame) */
    "highp vec3 cookie(int i) {\n"
    "    highp vec2 d = v_world - u_l_a[i].xy;\n"
    "    highp vec2 l = vec2(u_l_f[i].z * d.x + u_l_f[i].w * d.y,\n"
    "                        -u_l_f[i].w * d.x + u_l_f[i].z * d.y) / u_l_f[i].xy;\n"
    "    if (abs(l.x) > 1.0 || abs(l.y) > 1.0) return vec3(0.0);\n"
    "    uint id = uint(u_l_e[i].w);\n"
    "    ivec2 at = ivec2(int(id & 255u), int(id >> 8));\n"
    "    highp vec4 r = vec4(texelFetch(u_rects, at, 0)) / 65535.0;\n"
    "    highp float pg = float(texelFetch(u_pages, at, 0).r);\n"
    "    vec4 t = texture(u_atlas, vec3(mix(r.xy, r.zw, l * 0.5 + 0.5), pg));\n"
    "    return t.rgb * t.a;\n"
    "}\n"
    "highp vec3 light2d(highp vec3 n) {\n"
    "    highp vec3 sum = vec3(0.0);\n"
    "    for (int i = 0; i < 16; i++) {\n"
    "        if (i >= u_l_n) break;\n"
    "        uint mask = floatBitsToUint(u_l_d[i].y);\n"
    "        if (((mask >> v_layer) & 1u) == 0u) continue;\n"
    "        int type = int(u_l_a[i].z + 0.5);\n"
    "        if (type == 4) { sum += u_l_b[i].rgb; continue; }\n"
    "        highp vec3 c = u_l_b[i].rgb;\n"
    "        if (type == 3) {\n"
    "            highp vec2 to = v_world - u_l_a[i].xy;\n"
    "            highp float d = length(to);\n"
    "            highp float span = max(u_l_c[i].x - u_l_b[i].w, 1e-4);\n"
    "            c *= falloff((u_l_c[i].x - d) / span, u_l_a[i].w);\n"
    "            if (u_l_c[i].z > -0.999) {\n"
    "                highp float ca = d > 1e-5 ? dot(to / d, vec2(u_l_c[i].w, u_l_d[i].x)) : 1.0;\n"
    "                c *= clamp((ca - u_l_c[i].z) / max(u_l_c[i].y - u_l_c[i].z, 1e-4), 0.0, 1.0);\n"
    "            }\n"
    "        } else if (type == 2) {\n"
    "            c *= u_l_e[i].w >= 0.0 ? cookie(i) : vec3(0.0);\n"
    "        } else {\n"
    "            c *= shape_att(i);\n"
    "        }\n"
    "        if (u_l_d[i].z > 0.0) {   /* normal mapped: N . L, the light raised */\n"
    "            highp vec3 L = normalize(vec3(u_l_a[i].xy - v_world, u_l_d[i].z));\n"
    "            c *= max(dot(n, L), 0.0);\n"
    "        }\n"
    "        sum += c;\n"
    "    }\n"
    "    return sum;\n"
    "}\n"
    /* effects: 1 flash, 2 grayscale, 3 hue shift, 4 dissolve, 5 outline */
    "void main() {\n"
    "    vec4 t = v_textured != 0 ? texture(u_atlas, vec3(v_uv, v_page)) : vec4(1.0);\n"
    "    float amt = float(v_fx.y) / 255.0;\n"
    "    if (v_fx.x == 4u) {\n"
    "        highp vec2 cell = floor(v_uv * u_side);\n"
    "        highp float h = fract(sin(dot(cell, vec2(12.9898, 78.233))) * 43758.5453);\n"
    "        if (h < amt) discard;\n"
    "    }\n"
    "    if (v_fx.x == 5u && v_textured != 0 && t.a < 0.5) {\n"
    "        highp float px = 1.0 / u_side;\n"
    "        float near = 0.0;\n"
    "        for (int k = 1; k <= 8; k++) {\n"
    "            if (uint(k) > v_fx.y) break;\n"
    "            for (int dir = 0; dir < 4; dir++) {\n"
    "                highp vec2 o = dir == 0 ? vec2(px, 0.0) : dir == 1 ? vec2(-px, 0.0)\n"
    "                    : dir == 2 ? vec2(0.0, px) : vec2(0.0, -px);\n"
    "                highp vec2 q = clamp(v_uv + o * float(k), v_rect.xy, v_rect.zw);\n"
    "                near = max(near, texture(u_atlas, vec3(q, v_page)).a);\n"
    "            }\n"
    "        }\n"
    "        if (near > 0.5) { frag = v_color; return; }\n"
    "    }\n"
    "    vec3 rgb = t.rgb * v_color.rgb;\n"
    "    if (v_lit != 0u) {\n"
    "        highp vec3 n = vec3(0.0, 0.0, 1.0);\n"
    "        if (u_has_normals != 0 && v_textured != 0) {\n"
    "            highp vec3 m = texture(u_normals, vec3(v_uv, v_page)).xyz * 2.0 - 1.0;\n"
    "            m.xy *= v_flip;\n"
    "            n = normalize(vec3(v_rot.x * m.x - v_rot.y * m.y,\n"
    "                               v_rot.y * m.x + v_rot.x * m.y, m.z));\n"
    "        }\n"
    "        rgb *= light2d(n);\n"
    "    }\n"
    "    if (v_fx.x == 1u) rgb = mix(rgb, vec3(1.0), amt);\n"
    "    if (v_fx.x == 2u) rgb = mix(rgb, vec3(dot(rgb, vec3(0.299, 0.587, 0.114))), amt);\n"
    "    if (v_fx.x == 3u) {\n"
    "        highp float a = amt * 6.28318530718;\n"
    "        highp vec3 k = vec3(0.57735);\n"
    "        rgb = rgb * cos(a) + cross(k, rgb) * sin(a) + k * dot(k, rgb) * (1.0 - cos(a));\n"
    "    }\n"
    "    frag = vec4(rgb, t.a * v_color.a);\n"
    "}\n";

/* The GPU sort: one bitonic step over the (key, index) pairs, ascending.
 * `init` fills the pairs from the keys (padding sorts last). */
static const char *GB_SORT_SRC =
    "#version 310 es\n"
    "layout(local_size_x = 256) in;\n"
    "layout(std430, binding = 3) buffer Order { uvec2 ord[]; };\n"
    "layout(std430, binding = 4) readonly buffer Keys { uint keys[]; };\n"
    "uniform int u_init, u_n, u_count, u_j, u_k;\n"
    "void main() {\n"
    "    uint i = gl_GlobalInvocationID.x;\n"
    "    if (i >= uint(u_n)) return;\n"
    "    if (u_init != 0) {\n"
    "        ord[i] = uvec2(i < uint(u_count) ? keys[i] : 0xffffffffu, i);\n"
    "        return;\n"
    "    }\n"
    "    uint l = i ^ uint(u_j);\n"
    "    if (l <= i) return;\n"
    "    uvec2 a = ord[i], b = ord[l];\n"
    "    bool a_after = a.x > b.x || (a.x == b.x && a.y > b.y);\n"
    "    bool up = (i & uint(u_k)) == 0u;\n"
    "    if (a_after == up) { ord[i] = b; ord[l] = a; }\n"
    "}\n";

/* ---- state ----------------------------------------------------------- */

static GLuint gb_prog, gb_vao, gb_vbo, gb_atlas, gb_rects, gb_pages, gb_normals;
static GLint gb_u_view, gb_u_rects, gb_u_pages, gb_u_atlas, gb_u_normals,
    gb_u_has_normals, gb_u_side;
static GLint gb_u_l_n, gb_u_l[6], gb_u_pts;
static int gb_has_normals;
static EngineGpuSprite gb_sprites[GB_MAX_SPRITES];
static EngineGpuSprite gb_shadow[GB_MAX_SPRITES];
static int gb_shadow_n = -1;
static long gb_uploaded_bytes;
static int gb_draw_calls;
/* the GPU sort */
static int gb_gpu_sort;
static GLuint gb_sort_prog, gb_order, gb_keys;
static GLint gb_s_init, gb_s_n, gb_s_count, gb_s_j, gb_s_k;
static unsigned gb_key[GB_MAX_SPRITES], gb_key_shadow[GB_MAX_SPRITES];
static int gb_key_n = -1;
static int gb_sort_passes;   /* this frame's compute dispatches (0: order kept) */

/* ---- setup ----------------------------------------------------------- */

static GLuint gb_link(const char *vsrc, const char *fsrc, int gpu_sort)
{
    static char vbuf[16384], fbuf[16384];
    GLuint vs, fs, prog;
    GLint ok = 0;
    snprintf(vbuf, sizeof vbuf, "#version 310 es\n%s%s", gpu_sort ? "#define GPU_SORT\n" : "",
             vsrc);
    snprintf(fbuf, sizeof fbuf, "#version 310 es\n%s", fsrc);
    vs = g3_compile_stage(GL_VERTEX_SHADER, vbuf, "batch vertex");
    fs = g3_compile_stage(GL_FRAGMENT_SHADER, fbuf, "batch fragment");
    if (!vs || !fs)
        return 0;
    prog = glCreateProgram();
    glAttachShader(prog, vs);
    glAttachShader(prog, fs);
    glLinkProgram(prog);
    glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[1024];
        glGetProgramInfoLog(prog, sizeof log, 0, log);
        printf("batch link failed: %s\n", log);
        return 0;
    }
    return prog;
}

static void gb_upload_table(void)
{
    int n = engine_sprite_count(), rows, k;
    static unsigned short rect[4 * 65536];
    static unsigned char page[65536];
    const unsigned short *uv = engine_sprite_uv_table();
    const unsigned char *pg = engine_sprite_page_table();
    if (n < 1)
        n = 1;
    rows = (n + 255) / 256;
    for (k = 0; k < rows * 256; k = k + 1) {
        int src = k < engine_sprite_count() ? k : 0;
        rect[4 * k + 0] = uv ? uv[4 * src + 0] : 0;
        rect[4 * k + 1] = uv ? uv[4 * src + 1] : 0;
        rect[4 * k + 2] = uv ? uv[4 * src + 2] : 0;
        rect[4 * k + 3] = uv ? uv[4 * src + 3] : 0;
        page[k] = pg ? pg[src] : 0;
    }
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glGenTextures(1, &gb_rects);
    glBindTexture(GL_TEXTURE_2D, gb_rects);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA16UI, 256, rows, 0, GL_RGBA_INTEGER,
                 GL_UNSIGNED_SHORT, rect);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glGenTextures(1, &gb_pages);
    glBindTexture(GL_TEXTURE_2D, gb_pages);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R8UI, 256, rows, 0, GL_RED_INTEGER,
                 GL_UNSIGNED_BYTE, page);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
}

static GLuint gb_upload_array(const unsigned char *(*page_rgba)(int), int side, int pages)
{
    GLuint tex;
    int k;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D_ARRAY, tex);
    glTexStorage3D(GL_TEXTURE_2D_ARRAY, 1, GL_RGBA8, side, side, pages);
    for (k = 0; k < pages; k = k + 1)
        glTexSubImage3D(GL_TEXTURE_2D_ARRAY, 0, 0, 0, k, side, side, 1, GL_RGBA,
                        GL_UNSIGNED_BYTE, page_rgba(k));
    glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D_ARRAY, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    return tex;
}

static int gb_init_sort(void)
{
    GLint nblocks = 0, ok = 0;
    GLuint cs;
    glGetIntegerv(GL_MAX_VERTEX_SHADER_STORAGE_BLOCKS, &nblocks);
    if (nblocks < 2) {
        printf("gpu sort: the vertex stage has %d storage blocks -- CPU sort\n", (int)nblocks);
        return 0;
    }
    cs = g3_compile_stage(GL_COMPUTE_SHADER, GB_SORT_SRC, "batch sort");
    if (!cs)
        return 0;
    gb_sort_prog = glCreateProgram();
    glAttachShader(gb_sort_prog, cs);
    glLinkProgram(gb_sort_prog);
    glGetProgramiv(gb_sort_prog, GL_LINK_STATUS, &ok);
    if (!ok)
        return 0;
    gb_s_init = glGetUniformLocation(gb_sort_prog, "u_init");
    gb_s_n = glGetUniformLocation(gb_sort_prog, "u_n");
    gb_s_count = glGetUniformLocation(gb_sort_prog, "u_count");
    gb_s_j = glGetUniformLocation(gb_sort_prog, "u_j");
    gb_s_k = glGetUniformLocation(gb_sort_prog, "u_k");
    glGenBuffers(1, &gb_order);
    glBindBuffer(GL_SHADER_STORAGE_BUFFER, gb_order);
    glBufferData(GL_SHADER_STORAGE_BUFFER, (GLsizeiptr)(GB_MAX_SPRITES * 8), 0, GL_DYNAMIC_DRAW);
    glGenBuffers(1, &gb_keys);
    glBindBuffer(GL_SHADER_STORAGE_BUFFER, gb_keys);
    glBufferData(GL_SHADER_STORAGE_BUFFER, (GLsizeiptr)(GB_MAX_SPRITES * 4), 0, GL_DYNAMIC_DRAW);
    return 1;
}

static int gb_init(void)
{
    const char *names[6] = { "u_l_a", "u_l_b", "u_l_c", "u_l_d", "u_l_e", "u_l_f" };
    int side = engine_atlas_side(), pages = engine_atlas_page_count(), k;
#ifdef GB_GPU_SORT
    gb_gpu_sort = gb_init_sort();
#endif
    gb_prog = gb_link(GB_VERT_SRC, GB_FRAG_SRC, gb_gpu_sort);
    if (!gb_prog)
        return 0;
    gb_u_view = glGetUniformLocation(gb_prog, "u_view");
    gb_u_rects = glGetUniformLocation(gb_prog, "u_rects");
    gb_u_pages = glGetUniformLocation(gb_prog, "u_pages");
    gb_u_atlas = glGetUniformLocation(gb_prog, "u_atlas");
    gb_u_normals = glGetUniformLocation(gb_prog, "u_normals");
    gb_u_has_normals = glGetUniformLocation(gb_prog, "u_has_normals");
    gb_u_side = glGetUniformLocation(gb_prog, "u_side");
    gb_u_l_n = glGetUniformLocation(gb_prog, "u_l_n");
    for (k = 0; k < 6; k = k + 1)
        gb_u_l[k] = glGetUniformLocation(gb_prog, names[k]);
    gb_u_pts = glGetUniformLocation(gb_prog, "u_pts");
    if (side > 0 && pages > 0) {
        gb_atlas = gb_upload_array(engine_atlas_rgba, side, pages);
        gb_has_normals = engine_atlas_normal_rgba(0) != 0;
        if (gb_has_normals)
            gb_normals = gb_upload_array(engine_atlas_normal_rgba, side, pages);
    }
    gb_upload_table();
    glGenVertexArrays(1, &gb_vao);
    glBindVertexArray(gb_vao);
    glGenBuffers(1, &gb_vbo);
    if (gb_gpu_sort) {
        /* the sprites: a storage buffer, in the engine's stable order */
        glBindBuffer(GL_SHADER_STORAGE_BUFFER, gb_vbo);
        glBufferData(GL_SHADER_STORAGE_BUFFER, (GLsizeiptr)sizeof gb_sprites, 0,
                     GL_DYNAMIC_DRAW);
        return 1;
    }
    glBindBuffer(GL_ARRAY_BUFFER, gb_vbo);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)sizeof gb_sprites, 0, GL_STREAM_DRAW);
#define GB_OFF(f) ((const void *)offsetof(EngineGpuSprite, f))
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, sizeof(EngineGpuSprite), GB_OFF(x));
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 2, GL_HALF_FLOAT, GL_FALSE, sizeof(EngineGpuSprite), GB_OFF(hw));
    glEnableVertexAttribArray(2);
    glVertexAttribIPointer(2, 1, GL_UNSIGNED_SHORT, sizeof(EngineGpuSprite), GB_OFF(sprite));
    glEnableVertexAttribArray(3);
    glVertexAttribIPointer(3, 1, GL_SHORT, sizeof(EngineGpuSprite), GB_OFF(rot));
    glEnableVertexAttribArray(4);
    glVertexAttribPointer(4, 4, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(EngineGpuSprite), GB_OFF(r));
    glEnableVertexAttribArray(5);
    glVertexAttribIPointer(5, 4, GL_UNSIGNED_BYTE, sizeof(EngineGpuSprite), GB_OFF(layer));
#undef GB_OFF
    for (k = 0; k < 6; k = k + 1)
        glVertexAttribDivisor((GLuint)k, 1);
    return 1;
}

/* ---- per frame ------------------------------------------------------- */

#define GB_GAP 8   /* unchanged sprites a run may bridge (one upload) */

/* Only the runs that changed since the last frame go up. */
static void gb_upload_changed(GLenum target, int n)
{
    int k = 0, first, last, gap;
    const size_t sz = sizeof(EngineGpuSprite);
    gb_uploaded_bytes = 0;
    while (k < n) {
        if (k < gb_shadow_n && memcmp(&gb_sprites[k], &gb_shadow[k], sz) == 0) {
            k = k + 1;
            continue;
        }
        first = last = k;
        gap = 0;
        for (k = k + 1; k < n && gap <= GB_GAP; k = k + 1) {
            if (k < gb_shadow_n && memcmp(&gb_sprites[k], &gb_shadow[k], sz) == 0)
                gap = gap + 1;
            else {
                last = k;
                gap = 0;
            }
        }
        glBufferSubData(target, (GLintptr)(first * (int)sz),
                        (GLsizeiptr)((last - first + 1) * (int)sz), &gb_sprites[first]);
        memcpy(&gb_shadow[first], &gb_sprites[first], (size_t)(last - first + 1) * sz);
        gb_uploaded_bytes = gb_uploaded_bytes + (long)((last - first + 1) * (int)sz);
        k = last + 1;
    }
    gb_shadow_n = n;
}

/* The GPU sort of the (key, index) pairs -- only when a key (or the count)
 * changed; otherwise last frame's order stands. */
static void gb_sort(int n)
{
    int size = 1, j, k, groups;
    gb_sort_passes = 0;
    if (n == gb_key_n && memcmp(gb_key, gb_key_shadow, (size_t)n * sizeof(unsigned)) == 0)
        return;
    memcpy(gb_key_shadow, gb_key, (size_t)n * sizeof(unsigned));
    gb_key_n = n;
    while (size < n)
        size = size * 2;
    groups = (size + 255) / 256;
    glBindBuffer(GL_SHADER_STORAGE_BUFFER, gb_keys);
    glBufferSubData(GL_SHADER_STORAGE_BUFFER, 0, (GLsizeiptr)(n * 4), gb_key);
    glUseProgram(gb_sort_prog);
    glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 3, gb_order);
    glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 4, gb_keys);
    glUniform1i(gb_s_n, size);
    glUniform1i(gb_s_count, n);
    glUniform1i(gb_s_init, 1);
    glDispatchCompute((GLuint)groups, 1, 1);
    glMemoryBarrier(GL_SHADER_STORAGE_BARRIER_BIT);
    gb_sort_passes = 1;
    glUniform1i(gb_s_init, 0);
    for (k = 2; k <= size; k = k * 2)
        for (j = k / 2; j > 0; j = j / 2) {
            glUniform1i(gb_s_k, k);
            glUniform1i(gb_s_j, j);
            glDispatchCompute((GLuint)groups, 1, 1);
            glMemoryBarrier(GL_SHADER_STORAGE_BARRIER_BIT);
            gb_sort_passes = gb_sort_passes + 1;
        }
}

static void gb_upload_lights(void)
{
    EngineLight2D ls[GB_MAX_LIGHTS];
    GLfloat v[6][4 * GB_MAX_LIGHTS];
    int ln = engine_collect_lights2d(ls, GB_MAX_LIGHTS), k, npts = 0;
    const float *pts = engine_light2d_points(&npts);
    for (k = 0; k < ln; k = k + 1) {
        union { unsigned u; float f; } m;
        const EngineLight2D *l = &ls[k];
        m.u = l->layer_mask;
        v[0][4 * k] = l->x; v[0][4 * k + 1] = l->y;
        v[0][4 * k + 2] = (float)l->type; v[0][4 * k + 3] = l->falloff;
        v[1][4 * k] = l->r; v[1][4 * k + 1] = l->g; v[1][4 * k + 2] = l->b;
        v[1][4 * k + 3] = l->inner;
        v[2][4 * k] = l->outer; v[2][4 * k + 1] = l->cos_inner;
        v[2][4 * k + 2] = l->cos_outer; v[2][4 * k + 3] = l->dir_x;
        v[3][4 * k] = l->dir_y; v[3][4 * k + 1] = m.f;
        v[3][4 * k + 2] = l->normal_distance; v[3][4 * k + 3] = 0.f;
        v[4][4 * k] = (float)l->shape_start; v[4][4 * k + 1] = (float)l->shape_count;
        v[4][4 * k + 2] = l->falloff_size; v[4][4 * k + 3] = (float)l->cookie;
        v[5][4 * k] = l->half_w > 1e-6f ? l->half_w : 1e-6f;
        v[5][4 * k + 1] = l->half_h > 1e-6f ? l->half_h : 1e-6f;
        v[5][4 * k + 2] = l->cos_r; v[5][4 * k + 3] = l->sin_r;
    }
    glUniform1i(gb_u_l_n, ln);
    if (ln > 0)
        for (k = 0; k < 6; k = k + 1)
            glUniform4fv(gb_u_l[k], ln, v[k]);
    if (npts > GB_MAX_POINTS)
        npts = GB_MAX_POINTS;
    if (npts > 0)
        glUniform2fv(gb_u_pts, npts, pts);
}

/* The frame: gles3_render.h's camera, letterbox and clear, then every
 * sprite in one instanced draw. Returns how many sprites it drew. */
static int gb_draw(int width, int height)
{
    int n, vx, vy, vw, vh;
    float aspect;
    if (gb_gpu_sort)
        n = engine_collect_gpu_sprites_stable(gb_sprites, gb_key, GB_MAX_SPRITES);
    else
        n = engine_collect_gpu_sprites(gb_sprites, GB_MAX_SPRITES);
    aspect = Camera_main_aspect;
    if (aspect < 1e-6f)
        aspect = height > 0 ? (float)width / (float)height : 1.0f;
    g3_refresh_camera_bounds(aspect);
    g3_handle_view_size(width, height);
    vx = (int)(Camera_main_rect_x * (float)width + 0.5f);
    vy = (int)(Camera_main_rect_y * (float)height + 0.5f);
    vw = (int)(Camera_main_rect_w * (float)width + 0.5f);
    vh = (int)(Camera_main_rect_h * (float)height + 0.5f);
    if (vw < 1)
        vw = 1;
    if (vh < 1)
        vh = 1;
    if (gb_gpu_sort && n > 0)
        gb_sort(n);
    glViewport(0, 0, width, height);
    glClearColor(0.f, 0.f, 0.f, 1.f);
    glClear(GL_COLOR_BUFFER_BIT);
    glViewport(vx, vy, vw, vh);
    glClearColor(Camera_main_background_r, Camera_main_background_g,
                 Camera_main_background_b, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glUseProgram(gb_prog);
    glUniform4f(gb_u_view, g3_left, g3_bottom, g3_right, g3_top);
    gb_upload_lights();
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D_ARRAY, gb_atlas);
    glUniform1i(gb_u_atlas, 0);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, gb_rects);
    glUniform1i(gb_u_rects, 1);
    glActiveTexture(GL_TEXTURE2);
    glBindTexture(GL_TEXTURE_2D, gb_pages);
    glUniform1i(gb_u_pages, 2);
    glActiveTexture(GL_TEXTURE3);
    glBindTexture(GL_TEXTURE_2D_ARRAY, gb_has_normals ? gb_normals : gb_atlas);
    glUniform1i(gb_u_normals, 3);
    glUniform1i(gb_u_has_normals, gb_has_normals);
    glUniform1f(gb_u_side, (float)engine_atlas_side());
    glActiveTexture(GL_TEXTURE0);
    glBindVertexArray(gb_vao);
    gb_draw_calls = 0;
    if (n > 0) {
        if (gb_gpu_sort) {
            glBindBuffer(GL_SHADER_STORAGE_BUFFER, gb_vbo);
            gb_upload_changed(GL_SHADER_STORAGE_BUFFER, n);
            glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 2, gb_vbo);
            glBindBufferBase(GL_SHADER_STORAGE_BUFFER, 3, gb_order);
        } else {
            glBindBuffer(GL_ARRAY_BUFFER, gb_vbo);
            gb_upload_changed(GL_ARRAY_BUFFER, n);
        }
        glDrawArraysInstanced(GL_TRIANGLE_STRIP, 0, 4, n);
        gb_draw_calls = 1;
    }
    return n;
}

#endif
