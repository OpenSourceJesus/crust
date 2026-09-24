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
Unity (and later Godot / Blender) object model, held to the same
discipline as `cs2cpp.py` / `csrust.py`: what is not in the subset is
refused with a Unity/csc-style diagnostic
(`Assets/.../File.cs(line,col): error CSxxxx: …`) at the use site.

Script bodies are still lowered by unity_pack's own translator, which is
being replaced by `cs2cpp.py` one rewrite family at a time — see
[Script lowering and the move to cs2cpp](#script-lowering-and-the-move-to-cs2cpp).
A method it cannot lower yet is reported (`warning CS8000`), not
silently emptied.

After emit, `engine.cpp` / `data.cpp` / `main.cpp` (C++ subset twins of
the `.c` files) are run through `cpprust._check_unsupported` and
`cpprust.translate`, then the translated C is compiled with
`python3 -m shivyc.main` (crust). If the hand-lowered code leaves the
crust subset or fails to compile, pack fails with `PackError` naming the
file — so you know the generated C++ and C stayed inside the same gate
`csrust` uses.

Host builds still use `gcc` via the generated `Makefile`. `make crust-check`
recompiles the `.c` files with crust (`-D CRUST_NO_POSIX_MKDIR` skips
`errno.h` / `mkdir`, which crust's include subset does not provide).

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
3. **Bitfields** for ints whose every value is known: the scene's, and
   the literals (or consts) scripts assign. `hp` that is only ever 0..7
   is `unsigned hp : 3`. A field written with anything else —
   `seen = target.hp`, `hp++`, `hp += n`, a parameter, or a write from
   another script through a handle — keeps its C# width: nothing bounds
   it, and a bitfield would truncate or wrap it silently (it did:
   `seen = target.hp` with 5 stored 1).
4. **Indices instead of pointers.** If a class has ≤256 instances
   *and the scripts never `Instantiate` / `Destroy` / `new GameObject`*,
   a reference is `uint8_t` into `_Coin_inst_array[]`. The translator
   rewrites `other.hp` to `Coin_AT(Owner_get_other(i)).hp`, the instance
   in `other`'s slot. (Documented from the start, it only works as of the
   move to cs2cpp: it used to come out `Owner_get_other(i).Owner_get_hp(i)`
   and the method was silently emptied. `TestPackedFields` runs it.)

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
python3 tools/unity_pack.py examples/unity_pack/MiniScene
# sources + player → $TMPDIR/MiniScene/MiniScene
# (Windows: MiniScene.exe). -o <dir> overrides the directory only.

python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/upack
/tmp/upack/MiniScene

python3 tools/unity_pack.py <project> --strict   # a stub is an error
```

The linked player is `gles2_window.c` when `pkg-config glfw3` succeeds,
otherwise the generated headless `main.c` (tick + print draw count).

Godot: pass a directory containing `.tscn`. Blender: a JSON dump
(`blender_pack.json`) — same packed C, different importer.

## Display via GLES2

`engine_collect_draws()` walks every authored SpriteRenderer with a project
PNG and fills an `EngineDraw` list (world xy, half-extents, XY rotation basis, tint
RGBA, tex index), then sorts by TagManager sorting layer and `m_SortingOrder`
(back-to-front). Tint alpha (`m_Color.a` on SpriteRenderer / Image) multiplies
texture alpha in the GLES hosts (`GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA`).
Windowed hosts (`gles2_window.c`) size the GLFW window from
Player Settings `defaultScreenWidth` / `defaultScreenHeight` (`Screen_width`
/ `Screen_height` in `data.c`). `fullscreenMode` 0/1 opens a primary-monitor
fullscreen window (`Screen_fullScreen`); with `defaultIsNativeResolution` the
desktop video mode is used so the window fills the display. The checked-in host
`examples/unity_pack/gles2_view.c` ticks the engine, draws each sprite as
a textured quad through the same surfaceless EGL/FBO path as
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
fills a flat `float[]` for the GPU: under SoA it `memcpy`s the tables (clang
autovec remarks showed nested element copies were not beneficial); under
AoS it gathers from struct fields. Host `Makefile` compiles `engine.c` with
`-O3 -fno-math-errno` so `sinf`/`cosf`/`sqrtf` loops can autovec. Opt-in so
size-focused packs stay AoS and you can benchmark both:

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

## Script lowering and the move to cs2cpp

unity_pack lowers script bodies with a translator of its own, written as
regex rewrites against the packed object model. It should never have had
one: `tools/cs2cpp.py` is the C# subset, and everything it does — enums,
`List<T>`, `MemoryMarshal`, declaration order, its refusals — should apply
to Unity scripts too. The translator is being moved onto cs2cpp one
rewrite family at a time, with a "packed" object model cs2cpp understands
(a class is an index into its instance array), until what remains here is
the Unity API layer: Transform, GetComponent, Input and the like.

**What has moved.** cs2cpp describes the difference between the two
object models in one place, `cs2cpp.ObjectModel`; unity_pack builds the
packed one from its plan (`_packed_model`) and hands script bodies to
cs2cpp's families before its own Unity API rewrites:

| family | cs2cpp | packed model |
|--------|--------|--------------|
| float literals `2f` | `lower_body` | `2.f` (csrust gained this too: it had none) |
| `x == null` / `!= null` | `lower_body` | `-1`, the missing-object index |
| `true` / `false` | `lower_body` | `1` / `0` |
| `this.x`, bare `this` | `lower_body` | `x`, `i` — an object is its index |
| `string` locals | `lower_local_types` | `const char *` |
| `byte[]`, `.Length`, `[i]` | `lower_byte_arrays` | `ByteArray`, `.length`, `.data[i]` |
| `"s" + x` | `lower_string_concat`, `scalar_kind` | `_str_plus_i/f/c/s(..)` |
| `List<T>`, `Dictionary<K,V>`, `SortedList` | `lower_packed_collections`, from a `PackedClass` per class | `std::vector` / `std::map`; `Add`, `Clear`, `Count`, `ContainsKey`, `Remove`; `Other.list` as `Other_list`; an instance field aliased to its slot, `&items = Owner_items[i]` |
| `Time.deltaTime`, `Application.*`, `File.*`, `Mathf.*`, `Debug.Log`, `Input.GetAxis`, … | `lower_bindings`, over this file's tables | `Time_deltaTime`, `Application_dataPath()`, … |
| statics, fields, `other.hp` | `lower_packed_fields` | `Owner_name`, `Owner_get_x(i)` / `Owner_set_x(i, v)`, `Other_AT(..).hp` |

The plan still decides everything it decided: this file describes each
class to cs2cpp as a `PackedClass` (its static and per-instance lists and
maps, and what its other fields hold), and cs2cpp does the lowering.
Collection element types are this file's (`_collection_elem_c_ty`, passed
as the model's `elem_type`) and are lossy: `double` is `float`, and
`long`, `uint` and `ulong` are `int`. They were so before the move and are
unchanged; widening them would change every packed collection. `x.field`
for another class's instance map was matched on the field's name whatever
`x` was; now an `x` visibly of another type is left alone.

**The Unity API is a table.** A UnityEngine member that is one engine name
— `Time.deltaTime`, `Application.dataPath`, `File.Exists`, `Mathf.Sin`,
`Debug.Log`, `Input.GetAxis`, `Camera.main.orthographicSize` — is a
`cs2cpp.Binding` in one of this file's tables (`_UNITY_API_CORE`, `_SCENE`,
`_LOG`, `_CONSOLE`, `_MATHF`), and `cs2cpp.lower_bindings` applies it.
Adding API is adding a row. The knowledge is this file's, the rewriting
cs2cpp's, and every entry gets the same boundaries: the one-off patterns
each got them right or wrong on their own (`Time.time` by text replace
took the front of `Time.timeScale`; `File.Exists` took the back of
`MyFile.Exists`). What is not one name — Transform, GetComponent,
`Destroy(gameObject)`, `Keyboard.current.<k>Key` — is still rewritten here.

Each moved as the same code, so the packed output did not change — the
golden check is byte-identical after every step — except that cs2cpp
matches outside strings and comments, where unity_pack's regexes did not,
and where a step fixed something, shown case by case in the golden diff:
a field write is parsed to the end of its expression (the old rewrite
closed the paren at the end of the line, wrong for two writes on one
line), handle fields are rewritten before their reads, and an int written
with a non-literal keeps its width (one corpus field, `pointsPerGem =
amount`, had been a 1-bit bitfield).
`Transform`, `GameObject` and `AudioSource` locals stay here: they are
Unity types, the API layer's, not C#'s.

**Stubs are diagnostics.** A method whose lowered body still holds C# the
translator cannot handle is emitted as an empty method. That used to
happen without a word, which changed what the program did — a
`Debug.Log` vanished, a `File` call became a no-op. Now each one is a
csc-style warning at the method, naming what was left:

```
Assets/Scripts/Menu.cs(3,19): warning CS8000: `Menu.Start` is not lowered yet
  (`Unknown.DoThing(`: Unlowered static call …); it is emitted as an empty method
```

With `pack(strict=True)` / `--strict` it is an error, and every stub is
recorded in `plan["stubs"]`. (CS8000 is csc's "not yet implemented".)
Once the move to cs2cpp is done, strict becomes the default.

Reporting them showed that the detector itself was emptying methods that
were lowered completely: it matched inside string literals (a script path
in a null-reference message, a URL) and took locals and fields of the
engine's own C types (`ByteArray`, `Vector2`, `Vector2Int`, `Matrix4x4`)
for leftover C#. Both are fixed — it matches with strings and comments
blanked, and accepts the types the engine has declared — and a stub keeps
a `SetActive` line only if that line is itself lowered (one inside a
lambda had carried the lambda into the C). `TestStubDiagnostics` pins
each.

**The gate: `tools/unity_pack_golden.py`.** Every step of the move must
leave the packed output exactly as it was, or change it on purpose and
show where:

```
python3 tools/unity_pack_golden.py check      # re-pack every case, compare
python3 tools/unity_pack_golden.py check -v   # ... with diffs
python3 tools/unity_pack_golden.py record     # after an intended change
```

The corpus is every `unity_pack.pack(..)` call `tests/test_unity_pack.py`
makes, over the small projects the tests author themselves: inputs,
options, and a sha256 of `engine.cpp` / `data.cpp` / `main.cpp`
(`tests/unity_golden/corpus.json`). A check skips cpprust + shivyc
validation, which does not change the emitted text, so it takes seconds.

**Fixtures.** `examples/unity_pack/MiniScene` is a self-authored project
(scene, metas, a generated 8×8 PNG) and is tracked whole. SystemsScene is
scripts-only in the repository; the tests that pack the whole project are
marked `needs_systems` and skip unless its scene, art and ProjectSettings
have been dropped in locally. Its golden cases go to a local file beside
the cache and are checked when present.

