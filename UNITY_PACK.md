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
gcc -O3 -c /tmp/upack/engine.c
gcc -O0 -c /tmp/upack/data.c
gcc -O2 -o /tmp/upack/game /tmp/upack/engine.o /tmp/upack/data.o -lm
```

Godot: pass a directory containing `.tscn`. Blender: a JSON dump
(`blender_pack.json`) — same packed C, different importer.
