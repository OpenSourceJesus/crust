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
