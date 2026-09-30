/* The 2D GPU path on GLES2 hardware (gles2_view.c -DBATCH, a --gpu-batch
 * pack). GLES2 has no instancing, integer textures or texture arrays, so
 * the quads are the CPU's -- the same corners the per-sprite path computes
 * -- in one vertex buffer with 16-bit indices, textured from the atlas (one
 * page one texture): one glDrawElements per run of sprites on one page, one
 * for the frame when the atlas is one page. The 2D lights are the GLES3
 * batch shader's, in GLSL ES 1.00: the CPU gives each sprite the mask of the
 * lights that reach its sorting layer (no integer bits in the shader).
 * Global and Point (spot) lights only: Freeform, Parametric and Sprite
 * lights, normal maps and the effects are the GLES3 path's.
 *
 * Needs gles2_view.c's camera (world_to_ndc_x / _y) and compile_stage.
 */
#ifndef GLES2_BATCH_H
#define GLES2_BATCH_H

#define GB2_MAX_SPRITES 16384          /* 4 vertices each: 16-bit indices */
#define GB2_STRIDE 12                  /* ndc xy, rgba, uv, world xy, lit, mask */
#define GB2_MAX_LIGHTS 16
#define GB2_MAX_PAGES 8

static const char *GB2_VERT_SRC =
    "attribute vec2 a_pos;\n"
    "attribute vec4 a_color;\n"
    "attribute vec2 a_uv;\n"
    "attribute vec2 a_world;\n"
    "attribute vec2 a_light;   /* lit, the lights' mask */\n"
    "varying vec4 v_color;\n"
    "varying vec2 v_uv;\n"
    "varying vec2 v_world;\n"
    "varying vec2 v_light;\n"
    "void main() {\n"
    "    v_color = a_color;\n"
    "    v_uv = a_uv;\n"
    "    v_world = a_world;\n"
    "    v_light = a_light;\n"
    "    gl_Position = vec4(a_pos, 0.0, 1.0);\n"
    "}\n";

static const char *GB2_FRAG_SRC =
    /* mediump, as the per-sprite shader (the same rounding of the tint);
       highp where the lights' distances are */
    "precision mediump float;\n"
    "varying vec4 v_color;\n"
    "varying vec2 v_uv;\n"
    "varying highp vec2 v_world;\n"
    "varying highp vec2 v_light;\n"
    "uniform sampler2D u_tex;\n"
    "uniform float u_textured;\n"
    "uniform int u_l_n;\n"
    "uniform highp vec4 u_l_a[16];\n"
    "uniform highp vec4 u_l_b[16];\n"
    "uniform highp vec4 u_l_c[16];\n"
    "uniform highp vec4 u_l_d[16];\n"
    "highp vec3 light2d() {\n"
    "    highp vec3 sum = vec3(0.0);\n"
    "    for (int i = 0; i < 16; i++) {\n"
    "        if (i >= u_l_n) break;\n"
    "        if (mod(floor(v_light.y / exp2(float(i)) + 0.001), 2.0) < 0.5) continue;\n"
    "        if (u_l_a[i].z > 3.5) { sum += u_l_b[i].rgb; continue; }\n"
    /* Freeform, Parametric and Sprite lights: the GLES3 path's only */
    "        if (u_l_a[i].z < 2.5) continue;\n"
    "        highp vec2 to = v_world - u_l_a[i].xy;\n"
    "        highp float d = length(to);\n"
    "        highp float t = clamp((u_l_c[i].x - d) / max(u_l_c[i].x - u_l_b[i].w, 1e-4), 0.0, 1.0);\n"
    "        highp float att = pow(t, 0.5 + 2.0 * u_l_a[i].w);\n"
    "        if (u_l_c[i].z > -0.999) {\n"
    "            highp float c = d > 1e-5 ? dot(to / d, vec2(u_l_c[i].w, u_l_d[i].x)) : 1.0;\n"
    "            att *= clamp((c - u_l_c[i].z) / max(u_l_c[i].y - u_l_c[i].z, 1e-4), 0.0, 1.0);\n"
    "        }\n"
    "        sum += u_l_b[i].rgb * att;\n"
    "    }\n"
    "    return sum;\n"
    "}\n"
    "void main() {\n"
    "    vec4 t = u_textured > 0.5 ? texture2D(u_tex, v_uv) : vec4(1.0);\n"
    "    vec3 rgb = t.rgb * v_color.rgb;\n"
    "    if (v_light.x > 0.5) rgb *= light2d();\n"
    "    gl_FragColor = vec4(rgb, t.a * v_color.a);\n"
    "}\n";

static GLuint gb2_prog, gb2_vbo, gb2_ibo, gb2_page_tex[GB2_MAX_PAGES], gb2_white;
static GLint gb2_u_tex, gb2_u_textured, gb2_u_l_n, gb2_u_l_a, gb2_u_l_b, gb2_u_l_c,
    gb2_u_l_d;
static int gb2_pages;
static GLfloat gb2_verts[GB2_MAX_SPRITES * 4 * GB2_STRIDE];
static GLushort gb2_index[GB2_MAX_SPRITES * 6];
static int gb2_draw_calls;

static int gb2_init(void)
{
    GLuint vs, fs;
    GLint ok = 0;
    int k, side;
    static const unsigned char white[4] = { 255, 255, 255, 255 };
    vs = compile_stage(GL_VERTEX_SHADER, GB2_VERT_SRC, "batch vertex");
    fs = compile_stage(GL_FRAGMENT_SHADER, GB2_FRAG_SRC, "batch fragment");
    if (!vs || !fs)
        return 0;
    gb2_prog = glCreateProgram();
    glAttachShader(gb2_prog, vs);
    glAttachShader(gb2_prog, fs);
    glBindAttribLocation(gb2_prog, 0, "a_pos");
    glBindAttribLocation(gb2_prog, 1, "a_color");
    glBindAttribLocation(gb2_prog, 2, "a_uv");
    glBindAttribLocation(gb2_prog, 3, "a_world");
    glBindAttribLocation(gb2_prog, 4, "a_light");
    glLinkProgram(gb2_prog);
    glGetProgramiv(gb2_prog, GL_LINK_STATUS, &ok);
    if (!ok)
        return 0;
    gb2_u_tex = glGetUniformLocation(gb2_prog, "u_tex");
    gb2_u_textured = glGetUniformLocation(gb2_prog, "u_textured");
    gb2_u_l_n = glGetUniformLocation(gb2_prog, "u_l_n");
    gb2_u_l_a = glGetUniformLocation(gb2_prog, "u_l_a");
    gb2_u_l_b = glGetUniformLocation(gb2_prog, "u_l_b");
    gb2_u_l_c = glGetUniformLocation(gb2_prog, "u_l_c");
    gb2_u_l_d = glGetUniformLocation(gb2_prog, "u_l_d");
    side = engine_atlas_side();
    gb2_pages = engine_atlas_page_count();
    if (gb2_pages > GB2_MAX_PAGES)
        gb2_pages = GB2_MAX_PAGES;
    glGenTextures(gb2_pages, gb2_page_tex);
    for (k = 0; k < gb2_pages; k = k + 1) {
        glBindTexture(GL_TEXTURE_2D, gb2_page_tex[k]);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, side, side, 0, GL_RGBA,
                     GL_UNSIGNED_BYTE, engine_atlas_rgba(k));
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    }
    glGenTextures(1, &gb2_white);
    glBindTexture(GL_TEXTURE_2D, gb2_white);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 1, 1, 0, GL_RGBA, GL_UNSIGNED_BYTE, white);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    /* the index buffer never changes: two triangles a quad */
    for (k = 0; k < GB2_MAX_SPRITES; k = k + 1) {
        GLushort b = (GLushort)(4 * k);
        gb2_index[6 * k + 0] = b;
        gb2_index[6 * k + 1] = (GLushort)(b + 1);
        gb2_index[6 * k + 2] = (GLushort)(b + 2);
        gb2_index[6 * k + 3] = (GLushort)(b + 1);
        gb2_index[6 * k + 4] = (GLushort)(b + 3);
        gb2_index[6 * k + 5] = (GLushort)(b + 2);
    }
    glGenBuffers(1, &gb2_vbo);
    glGenBuffers(1, &gb2_ibo);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, gb2_ibo);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, (GLsizeiptr)sizeof gb2_index, gb2_index,
                 GL_STATIC_DRAW);
    return 1;
}

/* The page a draw samples: -1 untextured. */
static int gb2_page_of(const EngineDraw *d)
{
    if (d->tex < 0 || d->tex >= engine_sprite_count())
        return -1;
    return engine_sprite_page_table()[d->tex];
}

/* The draw list, as quads in one buffer: a draw call per run of one page. */
static int gb2_draw(const EngineDraw *draws, int ndraw)
{
    const unsigned short *uv = engine_sprite_uv_table();
    EngineLight2D ls[GB2_MAX_LIGHTS];
    GLfloat la[64], lb[64], lc[64], ld[64];
    int ln = engine_collect_lights2d(ls, GB2_MAX_LIGHTS), k, i, n = 0, run = 0, run_page = -2;
    GLsizei stride = (GLsizei)(GB2_STRIDE * sizeof(GLfloat));
    for (k = 0; k < ln; k = k + 1) {
        la[4 * k] = ls[k].x; la[4 * k + 1] = ls[k].y;
        la[4 * k + 2] = (float)ls[k].type; la[4 * k + 3] = ls[k].falloff;
        lb[4 * k] = ls[k].r; lb[4 * k + 1] = ls[k].g; lb[4 * k + 2] = ls[k].b;
        lb[4 * k + 3] = ls[k].inner;
        lc[4 * k] = ls[k].outer; lc[4 * k + 1] = ls[k].cos_inner;
        lc[4 * k + 2] = ls[k].cos_outer; lc[4 * k + 3] = ls[k].dir_x;
        ld[4 * k] = ls[k].dir_y; ld[4 * k + 1] = 0.f; ld[4 * k + 2] = 0.f;
        ld[4 * k + 3] = 0.f;
    }
    glUseProgram(gb2_prog);
    glUniform1i(gb2_u_tex, 0);
    glUniform1i(gb2_u_l_n, ln);
    if (ln > 0) {
        glUniform4fv(gb2_u_l_a, ln, la);
        glUniform4fv(gb2_u_l_b, ln, lb);
        glUniform4fv(gb2_u_l_c, ln, lc);
        glUniform4fv(gb2_u_l_d, ln, ld);
    }
    glBindBuffer(GL_ARRAY_BUFFER, gb2_vbo);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, gb2_ibo);
    for (k = 0; k < 5; k = k + 1)
        glEnableVertexAttribArray((GLuint)k);
    gb2_draw_calls = 0;
    for (i = 0; i <= ndraw; i = i + 1) {
        int page = i < ndraw ? gb2_page_of(&draws[i]) : -3;
        if (i < ndraw && draws[i].tex != -2 && page == -1)
            continue;   /* no texture of the atlas's, and not a particle */
        if ((page != run_page || run == GB2_MAX_SPRITES) && run > 0) {
            /* flush the run: one draw call */
            glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(run * 4 * stride), gb2_verts,
                         GL_STREAM_DRAW);
            glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride, (const void *)0);
            glVertexAttribPointer(1, 4, GL_FLOAT, GL_FALSE, stride,
                                  (const void *)(2 * sizeof(GLfloat)));
            glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride,
                                  (const void *)(6 * sizeof(GLfloat)));
            glVertexAttribPointer(3, 2, GL_FLOAT, GL_FALSE, stride,
                                  (const void *)(8 * sizeof(GLfloat)));
            glVertexAttribPointer(4, 2, GL_FLOAT, GL_FALSE, stride,
                                  (const void *)(10 * sizeof(GLfloat)));
            glActiveTexture(GL_TEXTURE0);
            glBindTexture(GL_TEXTURE_2D, run_page >= 0 ? gb2_page_tex[run_page] : gb2_white);
            glUniform1f(gb2_u_textured, 1.0f);
            glDrawElements(GL_TRIANGLES, run * 6, GL_UNSIGNED_SHORT, 0);
            gb2_draw_calls = gb2_draw_calls + 1;
            run = 0;
        }
        if (i == ndraw)
            break;
        run_page = page;
        {
            const EngineDraw *d = &draws[i];
            float hw = d->half_w, hh = d->half_h;
            float lx[4] = {-hw, hw, -hw, hw};
            float ly[4] = {-hh, -hh, hh, hh};
            float cu[4] = {0.f, 1.f, 0.f, 1.f};
            float cv[4] = {0.f, 0.f, 1.f, 1.f};
            float u0 = 0.f, v0 = 0.f, u1 = 1.f, v1 = 1.f, mask = 0.f;
            int c, L;
            if (page >= 0) {
                u0 = (float)uv[4 * d->tex + 0] / 65535.0f;
                v0 = (float)uv[4 * d->tex + 1] / 65535.0f;
                u1 = (float)uv[4 * d->tex + 2] / 65535.0f;
                v1 = (float)uv[4 * d->tex + 3] / 65535.0f;
            }
            for (L = 0; L < ln; L = L + 1)
                if ((ls[L].layer_mask >> (d->sorting_layer & 31)) & 1u)
                    mask = mask + (float)(1 << L);
            for (c = 0; c < 4; c = c + 1) {
                GLfloat *v = &gb2_verts[(run * 4 + c) * GB2_STRIDE];
                float wx = d->x + d->m00 * lx[c] + d->m01 * ly[c];
                float wy = d->y + d->m10 * lx[c] + d->m11 * ly[c];
                v[0] = world_to_ndc_x(wx);
                v[1] = world_to_ndc_y(wy);
                v[2] = d->r; v[3] = d->g; v[4] = d->b; v[5] = d->a;
                v[6] = u0 + (u1 - u0) * cu[c];
                v[7] = v0 + (v1 - v0) * cv[c];
                v[8] = wx; v[9] = wy;
                v[10] = (d->flags & 1) ? 1.f : 0.f;
                v[11] = mask;
            }
            run = run + 1;
            n = n + 1;
        }
    }
    return n;
}

#endif
