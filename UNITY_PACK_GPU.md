# UNITY_PACK GPU upload path

Decisions for the next SoA / upload work (issue-style notes from
performance writeups and #34-class benches). Crust already ships
SoA-by-default packs and `engine_upload_positions`; this file records
*where we aim* so follow-ups stay scoped.

## Answers (locked for this slice)

| Question | Choice |
|---|---|
| Graphics API / shading language | **OpenGL ES 2.0+ / GLSL** — matches `examples/gles2` and the GLFW window host. Vulkan/SPIR-V and D3D/HLSL stay out until a second backend exists. |
| In-view / frustum before upload? | **Raw bulk upload first.** CPU frustum gather is a later opt-in; the preferred long-term path is GPU-driven culling (upload all positions once, cull in a compute/FS path). |
| AoS vs SoA toggle | **SoA default**; **`unity_pack.py --aos`** for compact AoS structs; **`--soa-vec4`** for `float[N][4]`. |

Note on terminology: some engine posts swap “SoA” and “AoS”. In this
repo **SoA** means separate contiguous `float` tables per field (good for
upload / SIMD); **AoS** means `struct { float x,y,z; ... } objs[N]`.

## std140 vs std430

`layout(std140)` pads each `vec3` array element to 16 bytes. A CPU table
of `float[N][3]` will **not** match a `vec3 pos[N]` UBO.

Options we support or plan:

1. **`--soa-vec4`** (this slice): store `float _Class_pos[N][4]`; `.xyz` =
   world position, `.w` = instance index (useful for picking / bitmasks).
   Matches `vec4` under std140 *and* std430.
2. **SSBO + `std430`** (GLSL stub emitted beside the pack): keep tight
   `vec3` / `float[3]` on both sides when the host can use shader storage
   buffers (ES 3.1+ / desktop GL). GLES2 window path stays attribute/VBO
   based and does not require SSBOs yet.
3. **Bit-pack later**: pack xyz into `uint32` on the CPU, unpack in GLSL
   (`>>` / `&`). Cuts upload bandwidth; separate flag when implemented.

## Frustum / “only objects in view”

Not in this slice. Order of attack:

1. Contiguous SoA upload (+ vec4 pad for UBO rules) — done / this PR.
2. SIMD-friendly CPU gather of a visibility mask (optional).
3. GPU-driven: upload full SoA once per frame; cull on GPU.

## Indices and power-of-two structs

`uint8_t` / `uint16_t` handles instead of pointers are already emitted when
the instance set is closed. Rounding packed struct sizes up to a power of
two (so `base + (i << k)` replaces `i * sizeof`) is a follow-up for SoA
packs; bitfields make exact pow2 padding fiddly and must not break
layout tests.

## Compiler / packer switch (concrete)

```
# SoA float[N][2|3] — default; stream tables on upload
python3 tools/unity_pack.py MiniScene -o /tmp/soa

# compact AoS — gather on upload
python3 tools/unity_pack.py MiniScene -o /tmp/aos --aos

# SoA float[N][4] — GPU UBO / vec4 friendly; .w = instance id
python3 tools/unity_pack.py MiniScene -o /tmp/soa4 --soa-vec4
```

Gameplay still goes through `Class_get_pos_x(i)` / setters; only storage
and `engine_upload_positions` change.

## The 2D batch path (`--gpu-batch`)

The target: every 2D sprite in **one draw call**, each sprite a 16-bit
index plus packed bytes, lit by URP 2D lights -- the renderer counterpart
of Box2D-Packed's 16-bit indices. Code: `tools/unity_pack_gpu2d.py` (pack
side), `examples/unity_pack/gles3_batch.h` (GLES 3.1 renderer); build a
viewer with it as `gles3_view.c -DBATCH`.

**Atlas.** At pack time PIL packs every sprite texture into one
power-of-two square page (shelf packing, tallest first, from 64 up to
4096), each image's edge pixels extruded into a 2-pixel border so nearest
and linear sampling never bleed into a neighbour. A pack too big for one
4096 page gets more pages, uploaded as the layers of one
`GL_TEXTURE_2D_ARRAY` -- still one bind. The pages are written beside the
pack as `atlas<N>.png`.

**Sprite table.** Each texture's rectangle in the atlas as four `uint16`
(u0, v0, u1, v1, normalized 0..65535 -- a sixteenth of a texel at 4096) and
its page. The renderer keeps it as two integer textures (`RGBA16UI`,
`R8UI`, 256 wide) the vertex shader `texelFetch`es by the sprite index.

**Instances.** `engine_collect_gpu_sprites()` packs the engine's sorted draw
list 24 bytes a sprite:

| bytes | field | |
|---|---|---|
| 8 | `float x, y` | world position |
| 4 | `half hw, hh` | half extents; a negative one flips |
| 2 | `u16 sprite` | the sprite table's index (0xFFFF: untextured, a particle) |
| 2 | `s16 rot` | rotation, 65536 a turn |
| 4 | `u8 r, g, b, a` | tint |
| 4 | `u8 layer, flags, effect, arg` | sorting layer; 1 = lit; a 2D effect and its parameter (reserved) |

The renderer draws them as instances of one four-corner triangle strip
(`glDrawArraysInstanced(GL_TRIANGLE_STRIP, 0, 4, n)`, every attribute's
divisor 1): corners, rotation and the atlas lookup are the vertex
shader's. The CPU no longer builds quads, and one texture bind serves the
frame. Held by `TestGLES3View.test_batch_frame_is_the_per_sprite_frame`:
MiniScene through the batch path is the per-sprite renderer's frame, byte
for byte (Mesa, headless), in one draw call.

**URP 2D lights.** A URP `Light2D` (read by its fields: `m_LightType`,
`m_Color`, `m_Intensity`, inner / outer radius and angle,
`m_FalloffIntensity`, `m_ApplyToSortingLayers`) is an `EngineLight2D` of
`engine_collect_lights2d()` -- at its owner's live position, its target
sorting layers a bit mask. A SpriteRenderer whose material is URP's
`Sprite-Lit-Default` is lit (`flags & 1`); the fragment shader multiplies it
by the sum of the lights that include its sorting layer (URP's default
Multiply blend style): a Global light its color x intensity, a Point
light's falling from the inner to the outer radius, a spot's (inner / outer
angle) across its cone about the light's up axis. A lit sprite no light
reaches is black, as in Unity; an unlit sprite is not touched. Up to 16
lights a frame, as uniform arrays. Held by
`test_batch_global_light_tints_the_lit_sprites`.

Approximations, for now: URP shapes a point light's falloff with a lookup
texture -- here a power curve, `t ^ (0.5 + 2 * falloffIntensity)`; Freeform
and Sprite lights, normal maps, shadows, and the other blend styles are not
read; a light's direction is its authored rotation.

**Persistent instance buffer.** The renderer keeps last frame's instances;
only the runs that changed are uploaded (`glBufferSubData`, runs bridging up
to 8 unchanged sprites merged), so the draw order stays the engine's and a
still scene uploads nothing -- the viewer reports the bytes (the test: 72 the
first frame, 0 an unchanged one).

**GLES2 fallback** (`gles2_batch.h`, `gles2_view.c -DBATCH`). No instancing,
integer textures or arrays on GLES2: the same corners the per-sprite path
computes, into one vertex buffer with a static 16-bit index buffer (16384
sprites a batch), textured from the atlas -- one `glDrawElements` per run of
sprites on one page, one for the frame with a one-page atlas. The lights are
the same shader in GLSL ES 1.00, each sprite given the mask of the lights
reaching its layer (no integer bits there); normal maps and effects are the
GLES3 path's only. Its frame is the GLES2 per-sprite frame, byte for byte
(`test_gles2_batch_frame_is_the_per_sprite_frame`). Both batch shaders keep
the per-sprite shader's `mediump` for the color (the same rounding), `highp`
where the lights' distances are.

**Normal maps.** A sprite texture's URP secondary texture `_NormalMap` (its
.meta's `spriteSheet.secondaryTextures`; a sheet slice cropped as the sprite
is) goes in a second atlas of the same layout -- flat (0.5, 0.5, 1) where a
sprite has none -- `atlas<N>_normal.png`, `engine_atlas_normal_rgba()`. For a
light with normal maps on (`m_NormalMapQuality` not Disabled), a point
light's attenuation is multiplied by N . L, L toward the light raised by
`m_NormalMapDistance`; the normal turns and flips with its sprite. Global
lights ignore it, as URP's do (`test_batch_normal_map_faces_the_light`).

**2D effects** (the `effect` byte). Per GameObject, set from a script with
`SpriteEffects2D.Set(gameObject, SpriteEffect2D.Flash, 0.8f)` / `.Clear(go)`
(`examples/unity_pack/SpriteEffects2D.cs`: put it in the Unity project -- it
compiles there and does nothing; the packer rewrites the calls to
`engine_set_sprite_effect(go, effect, amount)`, also the host's). 1 Flash
(toward white), 2 Grayscale, 3 HueShift (a turn), 4 Dissolve (that share of
texels gone), 5 Outline (its width, 1..8 texels, in the tint, inside the
sprite's bounds -- a sprite needs a transparent margin; never sampling a
neighbour in the atlas). `EngineDraw` carries its GameObject (`go`) for the
table (`test_batch_sprite_effect_flashes_the_coins`). A Godot pack takes the
same path (`godot_pack.py --gpu-batch`; see GODOT_PACK.md): its scripts call
`SpriteEffects2D.Set(this | a node reference, ..)`
(`examples/unity_pack/SpriteEffects2D.Godot.cs`), and the effect covers the
node's subtree -- `engine_set_sprite_effect` walks a static child table
(`_engine_fx_child` / `_engine_fx_sib`), a Godot node's sprite being most
often its child.

**Freeform, Parametric and Sprite lights** (GLES3 path). A Freeform light's
`m_ShapePath` (and an older Parametric light's regular polygon:
`m_ShapeLightParametricSides / Radius / AngleOffset`) becomes world-space
points -- the light's live position, its authored rotation and scale --
shared by all lights in one 64-point uniform array
(`engine_light2d_points()`; each `EngineLight2D` has its `shape_start /
shape_count`, up to 16 a light). The shader lights a texel fully inside the
polygon (a crossing test) and, outside it, falls over
`m_ShapeLightFalloffSize` by the distance to the nearest edge, with the
point lights' power curve. A Sprite light's `m_LightCookieSprite` is loaded
into the texture table, so into the atlas: the light is the cookie's rect
(its size in units, the light's scale) at the light's transform, the shader
mapping the texel into the light's frame and taking the cookie's rgb x
alpha x color x intensity. Normal maps apply to every light but Global.
Close to URP, not exact: its falloff is a lookup texture, a Freeform
light's falloff an extruded mesh.

**The GPU sort** (`gles3_batch.h` built with `GB_GPU_SORT`, ES 3.1). The
engine hands the sprites over unsorted -- in its own stable order (class,
instance), `engine_collect_gpu_sprites_stable()` -- each with a 32-bit sort
key (sorting layer << 16 | sorting order + 32768). The sprites are a shader
storage buffer, updated by the same delta upload: a moved sprite rewrites
its own 24 bytes and nothing else shifts, as the order no longer lives in
the buffer. A compute shader bitonic-sorts (key, index) pairs -- a stable
sort, so the CPU's order (layer, order, then list order) exactly -- only on
a frame whose keys changed (most keep last frame's order: no dispatch); the
vertex shader reads its sprite through `ord[gl_InstanceID]`, unpacking the
24 bytes (`unpackHalf2x16`, `unpackUnorm4x8`). A GPU without storage
buffers in the vertex stage (`GL_MAX_VERTEX_SHADER_STORAGE_BLOCKS` < 2: some
ES 3.1 mobile parts) keeps the CPU sort, saying so.

**Next.** Shadow casters (URP ShadowCaster2D); keying the stable order by
Box2D-Packed's 16-bit body index directly; culling the instances to the
camera in the same compute pass.
