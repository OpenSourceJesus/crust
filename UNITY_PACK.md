# UNITY_PACK — packed engine from a Unity (or Godot) subset

`tools/unity_pack.py` reads a project, looks at **which Unity C# API
the scripts actually call** and **how many objects the scene places**,
then emits two C files:

| file | compile | contents |
|------|---------|----------|
| `engine.c` | `gcc -O3` | packed structs, used API only, script methods |
| `data.c` | `gcc -O0` | scene instance arrays (big constants, little code) |

The split is the point: hot loops stay in `engine.c` where `-O3` pays;
scene tables in `data.c` are just bytes, and `-O0` is faster to compile
and does not fight the optimiser over initialisers.

This is not Unity. It is a **subset** of C# plus a **subset** of the
Unity (and later Godot / Blender) object model, lowered through the
same discipline as `cs2cpp.py` / `csrust.py`: what is not in the
subset is refused with a reason.

After emit, `engine.c` / `data.c` / `main.c` are run through
`cpprust._check_unsupported` and `cpprust.translate` (output discarded).
If the hand-lowered C leaves the crust subset, pack fails with
`PackError` naming the file — so you know the generated C stayed
inside the same gate `csrust` uses for its C++ half.

## How a 16-byte object happens

Unity's `MonoBehaviour` + `Transform` + `GameObject` is hundreds of
bytes before the first gameplay field. The packer never emits that
object. It emits **only members the scripts and the scene use**, then
shrinks those:

1. **Drop `z`** when the project is 2D (no `Vector3.z`, no
   `Quaternion`, every placed position has `z == 0`).
2. **`float16` for static background.** A sprite that is never written
   in `Update` / never spawned does not need `float32`. Stored as
   `uint16_t` bits; widened on read.
3. **Bitfields** for ints whose scene values (and script literals)
   fit. `hp` that is only ever 0..7 is `unsigned hp : 3`.
4. **Indices instead of pointers.** If a class has ≤256 instances
   *and the scripts never `Instantiate` / `Destroy` / `new GameObject`*,
   a reference is `uint8_t` into `_Coin_inst_array[]`. The translator
   rewrites `other.hp` to `_Coin_inst_array[other].hp`.

A hand-placed 2D coin with `hp` and a `Vector2` position is typically
**8–16 bytes**, not a Unity object header.

The bound “≤256” is not guessed. For a scene of hand-placed objects
whose C# never spawns, the count **is** the scene count. If a script
spawns, the index widens (or the packer refuses a 8-bit handle).

## Function grouping

Methods are grouped by the class they use most. At the top of each
group sits the initialised instance array. Every global is still
**forward-declared at the top of `engine.c`** so `data.c` can define
the storage and any group can see any array.

## Shaders (the hard part)

Object models transplant. Shaders do not: Unity HLSL, Godot shading
language, and Blender OSM are different, and WASM vs native (GL /
Metal / D3D) are different again.

The packer therefore emits a **per-platform shader compiler stub**
(`shader_compiler_linux.c`, `_apple.c`, `_windows.c`, `_wasm.c`) for
a **tiny IR**: `position`, `color`, `uv`. That is the subset. A
follow-up editor (not this file) is where an artist finalises look
per platform. See the comments in the generated compilers.

## CLI

```
python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/upack
make -C /tmp/upack          # builds game (engine + data + headless main)
/tmp/upack/game
```

`main.c` is a tiny generated host (tick + print draw count). Replace it
with `gles2_view.c` / `gles2_window.c` for display.

Godot: pass a directory containing `.tscn`. Blender: a JSON dump
(`blender_pack.json`) — same packed C, different importer.

## Display via GLES2

`engine_collect_draws()` walks every instance with a packed position and
fills an `EngineDraw` list (world xy, half-extents, RGB). Colours are a
stable hash of the class name. The checked-in host
`examples/unity_pack/gles2_view.c` ticks the engine, draws each sprite as
a coloured quad through the same surfaceless EGL/FBO path as
`examples/gles2/triangle.c`, then prints ASCII (and optional PPM).

```
examples/unity_pack/run_gles2.sh              # native surfaceless → ASCII
examples/unity_pack/run_gles2.sh out.ppm
examples/unity_pack/run_gles2_window.sh       # real GLFW / GLES window
examples/unity_pack/run_gles2_wasm.sh         # soft GLES under node
```

`engine_draw.h` is written next to `engine.c` so the viewer stays in sync
with the typedef. Wasm builds one amalgamated TU via
`tools/unity_pack_amalg_view.py` (the wasm back end does not link multiple
files). The windowed host (`gles2_window.c`) needs `glfw3` and a display;
it is not part of the headless test path.

## SoA positions (`--soa`) — faster GPU uploads

Default packing keeps positions inside each instance struct (AoS). That
matches a compact object, but a frame that uploads every position to the
GPU must *gather* `pos_x`/`pos_y` out of each struct — the same pattern
Unity-style engines use.

`--soa` moves positions into contiguous tables:

```c
float _Player_pos[N][2];   /* or [N][3] in 3D */
```

Script accessors still go through `Player_get_pos_x(i)` /
`Player_set_pos_x(i, v)`, so gameplay code is unchanged. `engine_upload_positions`
fills a flat `float[]` for the GPU: under SoA it streams the tables; under
AoS it gathers. Opt-in so size-focused packs stay AoS and you can
benchmark both:

```
python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/aos
python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/soa --soa
python3 tools/unity_pack_bench_upload.py      # packed AoS vs SoA (MiniScene)
python3 tools/unity_pack_bench_csharp.py      # C SoA vs C# class AoS gather
```

`unity_pack_bench_csharp.py` times the same CPU-side upload shape the
design note describes: N objects → contiguous `float[N*3]`. C# uses an
array of heap classes (Unity-like); C SoA uses a flat `pos[N][3]` table
and `memcpy`. Needs the `dotnet` SDK for the C# leg.

Bit-packed struct fields and GLSL unpacking are a later step; this slice
is the layout + upload path only. GPU alignment (std140 / `--soa-vec4`),
SSBO stubs, and culling order of attack are in [UNITY_PACK_GPU.md](UNITY_PACK_GPU.md).

## Animation, input, lighting, camera, physics

Opt-in lowering of Input Manager axes, `Time.time` / `Mathf.Sin`,
`RenderSettings.ambientLight`, authored Lights / Cameras /
SpriteRenderers, and `Physics2D.gravity` + `FixedUpdate` on **authored**
scene objects — see [UNITY_PACK_SYSTEMS.md](UNITY_PACK_SYSTEMS.md). The
packer does not invent ParticleSystem pools, Canvas/UI, or InputAction maps.
Authored AnimationClips / AnimatorControllers and Rigidbodies are packed.
Fixture: `examples/unity_pack/SystemsScene`.
