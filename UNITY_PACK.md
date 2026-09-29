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

The bound is not guessed. For a scene of hand-placed objects whose C#
never spawns, the count **is** the scene count. If a script spawns, an
unannotated class's index widens to 32 bits. The top value of an index is
null (255 for a `uint8_t`, read back as -1), so a byte indexes 255
instances, and an empty scene reference is null — it used to be stored as
0, another object, and references between scripts were never resolved at
all: every one held 0. They are resolved by the referenced component's
fileID now (`TestPackedFields`).

### `[MaxInstances(N)]`: the author sets the cap

```csharp
public class MaxInstancesAttribute : System.Attribute {
    public MaxInstancesAttribute(int n) {}
}

[MaxInstances(255)]   public class Player : MonoBehaviour { … }   // uint8_t
[MaxInstances(20000)] public class BulletTypeA : MonoBehaviour { … } // uint16_t
```

The attribute class is the project's own (Unity needs it to compile the
script; the packer reads the name and ignores the class). With it:

* the index into the class is as narrow as N allows — `uint8_t` up to
  255, `uint16_t` up to 65535 — whatever else in the project spawns, and
  every field that holds one is that width (a handle is as wide as its
  *target*, not its owner);
* the instance array and the GameObject pool hold exactly N, and
  `Instantiate` returns null once N are live: the N+1st bullet is not
  fired. Clipping is the behaviour asked for, not an error;
* N counts **live** instances: a destroyed one's slot is reused (it was
  not — `Destroy` never freed anything, so a pool emptied after N spawns
  in all);
* a scene that already places more than N is an error at the attribute;
* the spare slots are zeros C fills in, so `data.c` does not list 20000
  empty rows.

`TestMaxInstances` runs a bullet that clones itself every frame (held at
N) and one that fires and is destroyed (firing for all 60 frames). A
script's component is the class named after its file, as in Unity — the
first class in the file used to be taken, so an attribute class declared
above the component became the scene object's class.

### `--gpu-handles`: handles in a GLES 3.1 SSBO

The stored references — a `Bullet`'s `owner`, a `Player`'s `last` — go
to the GPU at their packed width: four byte handles, two 16-bit handles
or one 32-bit handle per `uint`, read in the shader with
`bitfieldExtract`. A handle is as wide as its *target* class's index, so
with `[MaxInstances(10)] Bullet` and `[MaxInstances(1000)] Player` a
Bullet's `owner` is 16 bits and a Player's `last` is 8.

`--gpu-handles` (`pack(gpu_handles=True)`) adds, and changes nothing
else:

* `engine_upload_handles(uint32_t *dst, int max_words)` in the engine —
  every handle field as one stream of its class's capacity, packed from
  bit 0 and word-aligned; a slot past the live count holds the field's
  null;
* `engine_handles.h` — `ENGINE_HANDLE_WORDS`, and per stream
  `<Class>_<field>_OFF` / `_LEN` / `_BITS` / `_NULL`;
* `shaders/handles.glsl` — for inclusion after `#version 310 es`: the
  SSBO at binding 1 and an accessor per stream, returning an index into
  the target class or its `_NULL`:

```glsl
const uint Bullet_owner_NULL = 65535u;
uint Bullet_owner(uint i) { return bitfieldExtract(handles[0u + i / 2u], int((i % 2u) * 16u), 16); }
const uint Player_last_NULL = 255u;
uint Player_last(uint i) { return bitfieldExtract(handles[5u + i / 4u], int((i % 4u) * 8u), 8); }
```

`TestGpuHandles` packs that scene, decodes the words the C side writes
with `bitfieldExtract`'s definition, and — where a headless GL is
available (`moderngl` over Mesa's EGL/llvmpipe) — runs a compute shader
built from `handles.glsl` and compares every slot the GPU reads with the
scene. The default viewer is OpenGL ES 3.1 (see "Display") and binds
these handles at SSBO binding 1 every frame; the GLES2 viewer, kept for
hardware without ES 3.1, has no SSBOs.

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
python3 tools/unity_pack.py <project> --gpu-handles  # handles for a GLES 3.1 SSBO
python3 tools/unity_pack.py <project> --coost PATH   # coost checkout (string locals)
```

The linked player is `gles3_window.c` (OpenGL ES 3.1) when `pkg-config
glfw3` succeeds — `gles2_window.c` with `UNITY_PACK_GLES2=1`, for hardware
without ES 3.1 — otherwise the generated headless `main.c` (tick + print
draw count).

Godot 4: `python3 tools/godot_pack.py <project>` (or unity_pack.py on the
directory holding `project.godot`; see [GODOT_PACK.md](GODOT_PACK.md)).
Blender: a JSON dump (`blender_pack.json`) — same packed C, different
importer.

## Godot

Godot 4 projects are packed by the same back end, through
`tools/godot_pack.py` — scenes, C# node scripts, and 2D physics on
Box2D-Packed's Godot mode. See [GODOT_PACK.md](GODOT_PACK.md).

## Display: OpenGL ES 3.1 (and GLES2)

The default viewer is **OpenGL ES 3.1** — desktop GL 4.3+ drivers provide
it, and Mesa does in software — because ES 3.1 is what has shader storage
buffers: a `--gpu-handles` pack's handles are uploaded every frame to SSBO
binding 1, where `shaders/handles.glsl` reads them. `gles3_render.h` is the
renderer, shared by `gles3_window.c` (GLFW) and `gles3_view.c` (headless
EGL + FBO); crust's own `GLES3/gl31.h` declares what it uses, and
`tools/gles3_header_test.py` checks every constant and prototype there
against Khronos's header. It draws exactly what the GLES2 viewer draws:
`TestGLES3View` renders MiniScene with both and requires identical frames,
compared at 8 bits a channel (`-DFBO_FORMAT=0x8058`; the default RGBA4
target would round a small difference away). The GLES2 viewers stay, for
hardware without ES 3.1. The wasm viewer is next: WebGPU, with the handle
accessors in WGSL.

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
examples/unity_pack/run_gles3.sh              # ES 3.1 surfaceless → ASCII
examples/unity_pack/run_gles3.sh --gpu-handles out.ppm
examples/unity_pack/run_gles3_window.sh       # real GLFW / ES 3.1 window
examples/unity_pack/run_gles2.sh              # the GLES2 twins
examples/unity_pack/run_gles2_window.sh
examples/unity_pack/run_gles2_wasm.sh         # soft GLES under node
```

`engine_draw.h` is written next to `engine.c` so the viewer stays in sync
with the typedef. Wasm builds one amalgamated TU via
`tools/unity_pack_amalg_view.py` (the wasm back end does not link multiple
files). The windowed hosts need `glfw3` and a display; they are not part
of the headless test path. Building a viewer *with crust* fails today for
any scene with a Camera: crust ignores `__attribute__((weak))` on
variables, so the viewers' default camera globals collide with `data.c`'s
(the GLES2 viewer the same) — gcc builds are unaffected.

## SoA positions (default) — faster GPU uploads

Default packing puts positions in contiguous tables (SoA):

```c
float _Player_pos[N][2];   /* or [N][3] in 3D */
```

Script accessors still go through `Player_get_pos_x(i)` /
`Player_set_pos_x(i, v)`, so gameplay code is unchanged. `engine_upload_positions`
fills a flat `float[]` for the GPU: under SoA it `memcpy`s the tables (clang
autovec remarks showed nested element copies were not beneficial); under
AoS (`--aos`) it gathers from struct fields. Host `Makefile` compiles `engine.c`
with `-O3 -fno-math-errno` so `sinf`/`cosf`/`sqrtf` loops can autovec.

`--aos` keeps positions inside each instance struct for size-focused packs
or AoS gather benchmarks:

```
python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/soa
python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/aos --aos
python3 tools/unity_pack_bench_upload.py      # packed SoA vs AoS (MiniScene)
python3 tools/unity_pack_bench_csharp.py      # C SoA vs C# class AoS gather
```

`unity_pack_bench_csharp.py` times the same CPU-side upload shape the
design note describes: N objects → contiguous `float[N*3]`. C# uses an
array of heap classes (Unity-like); C SoA uses a flat `pos[N][3]` table
and `memcpy`. Needs the `dotnet` SDK for the C# leg.

Bit-packed struct fields and GLSL unpacking are a later step; this slice
is the layout + upload path only. GPU alignment (std140 / `--soa-vec4`),
SSBO stubs, and culling order of attack are in [UNITY_PACK_GPU.md](UNITY_PACK_GPU.md).

## Strings: owned locals, and coost

A C# `string` in a script body is a `const char *` in the packed engine, and
a concatenation (`"hp=" + hp`) is a typed call, `_str_plus_i(..)`, whose
result lives in a scratch slot. That is sound for a value used within its
statement -- passed to `Debug.Log`, to `File.WriteAllText`, to another
concatenation -- and was not for a value *kept*: a `string` local used to be
a `const char *` too, pointing into a slot that later concatenations reuse.

```csharp
string saved = "saved-" + hp;
for (int k = 0; k < 20; k++) { string t = "tmp-" + k; }
Debug.Log(saved);                    // printed "tmp-..." -- now "saved-7"
```

So a `string` local owns its bytes. It is a
[coost](https://github.com/crustos/coost) `fastring` -- coost is a C++
library in the subset cpprust lowers -- and every other string stays as it
was:

| C# | packed C++ |
|----|------------|
| `string s = e;` | `fastring s; s.assign_cstr(e);` |
| `s = e;` | `s.assign_cstr(e);` (`e` may read `s`) |
| `s += e;` | `s.assign_cstr(_str_plus_K(s.c_str(), (e)));` |
| any other read of `s` | `s.c_str()` |

The scratch slots grow to fit (they were 512 bytes, and a longer result was
cut short), and there are sixteen. A concatenation may start from a string
variable (`s + "x"` was pointer arithmetic), and an integer operand is
formatted as one: an `int` field, local or parameter, or a packed integer
field's accessor (`Player_get_hp(i)`), where it used to print through `%g`
(`1000000` as `1e+06`). The same typing picks `Debug_Log_i`.

A declaration that cannot be split -- in a `for` head, or several
declarators in one statement -- keeps its marker type (`_cs_string`), and the
method is reported as a stub rather than guessed at. `string` *parameters*
and fields are not owned yet: a parameter the method reassigns still points
into a slot, and a writable `string` field is not packed at all.

**Where coost comes from.** Only an engine that has a `string` local needs
coost, and it is found the way Box2D-Packed is: `--coost PATH`, `$COOST_ROOT`,
or a `coost` directory beside this repository. Without one, a pack that needs
it stops with an error saying so; upstream coost (idealvin/coost, full C++)
is refused by name. The string core -- `mem`, `fast`, `fastring` -- is
expanded on its own and spliced into `engine.cpp` after the C headers, so
`engine.cpp` stands alone and the lowered `engine.c` needs nothing from the
checkout to build. (Expanded on its own, not by giving cpprust an include
path for the engine: cpprust decides every simple `#if` in a file it splices
into, and the engine's `#ifndef CRUST_NO_POSIX_MKDIR` belongs to the
compiler.) The checkout's files are part of the pack's input fingerprint.

`TestOwnedStrings` packs, builds and runs each case.

## Animation, input, lighting, camera, physics

Opt-in lowering of Input Manager axes, `Time.time` / `Mathf.Sin`,
`RenderSettings.ambientLight`, authored Lights / Cameras /
SpriteRenderers, and `Physics2D.gravity` + `FixedUpdate` on **authored**
scene objects — see [UNITY_PACK_SYSTEMS.md](UNITY_PACK_SYSTEMS.md). The
packer does not invent ParticleSystem pools, Canvas/UI, or InputAction maps.
Authored AnimationClips / AnimatorControllers and Rigidbodies are packed.
Fixture: `examples/unity_pack/SystemsScene`.

## Source layout

`tools/unity_pack.py` is split by subsystem. Each module holds whole
functions moved out of it unchanged, and unity_pack re-exports every name,
so `unity_pack.<name>` keeps working for callers and tests. Modules import
only the ones above them in this list:

| Module | Contents |
|--------|----------|
| `unity_pack_common.py` | `PackError`, progress output, shared leaf helpers |
| `unity_pack_physics.py` | Rigidbody / Collider tables, physics materials, collision messages, the Box2D-Packed checkout |
| `unity_pack_sprites.py` | PNG decoding, sprite sheets, texture GUIDs, sprite sorting |
| `unity_pack_ui.py` | uGUI: RectTransform, Canvas / CanvasScaler, Image, Button, Toggle, Slider, Scrollbar, ScrollRect, EventTrigger, layout groups, TextMeshPro |
| `unity_pack_anim.py` | AnimationClip / AnimatorController parsing, keyframes, animation tables |
| `unity_pack_audio.py` | AudioSource tables and API rewrites |
| `unity_pack_build.py` | Makefile and player executable, including the Box2D-Packed glue |
| `godot_pack.py` | Godot 4 ([GODOT_PACK.md](GODOT_PACK.md)): the text resource reader, scenes, instancing and resources, bodies and shapes for Box2D-Packed, C# node scripts read as the subset; its own CLI |
| `unity_pack.py` | scene import, script analysis and lowering, `emit_engine`, `emit_data`, `pack`, CLI |

The split was checked by importing the old and new code side by side: every
top-level name is still present, every function's bytecode is identical, and
packed output is byte-identical. `parse_unity_yaml` (about 1,100 lines) is
still a single function.

`emit_engine` was one 6,300-line function. It is now about 1,300 lines that
call 34 section functions, `_emit_engine_*`, in emission order, none longer
than about 400 lines: `_emit_engine_debug_log`, `_emit_engine_gameobject_tables`,
`_emit_engine_ui` and its widgets (`_emit_engine_ui_buttons`, `_ui_sliders`,
...), `_emit_engine_class_groups`, `_emit_engine_colliders_2d`,
`_emit_engine_physics_fixed`, `_emit_engine_animation`, and so on. Each
section's code is unchanged; it takes the emit_engine locals it reads as
parameters and returns the few it sets for later sections. The interfaces
come from a definite-assignment analysis of emit_engine, and the split was
checked by running the whole test suite with every emit_engine call compared
against the unsplit one: same C output and same changes to the plan.

The emit tests pack `tests/fixtures/MiniScene`, a complete copy of the small
board project; `examples/unity_pack/MiniScene` keeps only its scripts.

## Script lowering and the move to cs2cpp

unity_pack lowers script bodies with a translator of its own, written as
regex rewrites against the packed object model. It should never have had
one: `tools/cs2cpp.py` is the C# subset, and everything it does — enums,
`List<T>`, `MemoryMarshal`, declaration order, its refusals — should apply
to Unity scripts too. The translator is being moved onto cs2cpp one
rewrite family at a time, with a "packed" object model cs2cpp understands
(a class is an index into its instance array), until what remains here is
the Unity API layer: Transform, GetComponent, Input and the like.

**C# structure and literal helpers.** The parts of unity_pack that read C#
itself, with nothing Unity-specific in them, live in cs2cpp as public
functions: `methods_in`, `interface_methods`, `property_names`,
`properties_as_methods`, `blank_method_bodies`, `MODIFIERS`, the overload
naming `method_c_symbol`, `method_arg_type_suffix`, `method_c_arg_names`,
`overload_method_names`, and `c_string`, `string_literal_value`,
`split_call_args`, `match_call_args`, `c_ident`. They moved unchanged, and
packed output was byte-identical before and after. unity_pack keeps the old
underscore names as aliases. `tests/test_cs2cpp_helpers.py` tests them
directly.

Parameter lists are parsed once, by `cs2cpp.parse_params`, for the C
signature (`_method_c_params`), the argument names passed on
(`method_c_arg_names`) and the overload suffix (`method_arg_type_suffix`),
so the three cannot disagree. It keeps generic arguments with commas
(`Dictionary<int, string>`), array types (`params int[] rest`, suffix
`int_array`, C type `int *`), `ref` / `out` / `in` / `params`, and default
values. A default parameter used to vanish from the C signature, and the pack
failed with `use of undeclared identifier`. `split_call_args` no longer
splits inside string, verbatim, interpolated or char literals, or inside
`{ }` and `[ ]`.

Calls that omit trailing default arguments get them filled in, for instance
and static methods: `Bump()` with `void Bump(int by = 1)` lowers to
`Tally_Bump(i, 1)`. Integer and real literals, `true` / `false` and regular
string literals are filled. `null`, enum members, constants, expressions and
named arguments (`Bump(by: 2)`) need type-aware lowering and are not filled
yet: the call stays short and the pack stops with `incorrect number of
arguments for function call`. Calls that pass every argument are unchanged.

The C# preprocessor and lexical checks are general too:
`cs2cpp.eval_pp_expr` and `cs2cpp.blank_inactive_pp_regions` take the set of
defined symbols, and unity_pack passes its player's (`UNITY_STANDALONE`,
`UNITY_STANDALONE_LINUX`; `UNITY_EDITOR` and mobile symbols undefined).
`cs2cpp.real_literal_error` reports C++-style real literals such as `0.f`
(CS1061). `_check_csharp_lex` keeps only the Unity checks: the
`transform.position += new Vector2` ambiguity (CS0034) and the File,
Application, Quaternion, Transform and refused-API checks. Old and new output
matched on 7,500 random cases.

csc-style diagnostics are `cs2cpp.cs_diag` and `cs2cpp.cs_diag_at_site`,
with a `display_path` hook. unity_pack's `_cs_diag` and `_raise_cs_at_site`
pass `_assets_rel_path`, so paths print as `Assets/...`; their output was
checked identical to the old code on 6,000 random cases.

Helpers that look general but read Unity values stay in unity_pack:
`_parse_csharp_field_init` (`Application.dataPath`, `new Vector2(...)`),
`_param_c_ty` (components as packed indices), the Unity API checks in
`_check_csharp_lex`, and `_unlowered_csharp` (Unity value constructors).

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
| `string` locals | `lower_local_types`, then unity_pack's `_own_string_locals` | a coost `fastring` each (see [Strings](#strings-owned-locals-and-coost)) |
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

**The rewrites here match code, not what it prints.** They were `re.sub`
over the body, strings and comments included:
`Debug.Log("transform.position.x moved")` printed `Player_get_pos_x(i)
moved`. Every `re.sub` in the lowering (115, in 20 functions) is now
`cs2cpp.code_sub`, the same call matched on a copy with string and comment
bodies blanked, its replacement given the original's groups — so a rewrite
that reads a literal (`GameObject.Find("Enemy")`) still reads it. The
packed output of every golden case was unchanged; `TestStubDiagnostics`
runs the player and checks the printed line. Still to convert: 27 scans
in 19 functions that walk `re.finditer` / `re.search` and splice by hand
(most are guards, some rewrite).

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
recorded in `plan["stubs"]`.

Deciding what is left is split the same way as the lowering. This file
asks the Unity questions — an `Instantiate` overload or `GetComponents<T>`
nothing lowered, `Type.instances`, a component's `.gameObject` or
`.activeSelf`, a parameter the emitter did not make a C formal — and
`cs2cpp.residual_csharp` the C# ones: array types, generic calls, lambdas,
calls, statics, member access and typed locals nothing lowered. This file
tells it only what is the engine's own C: the types it declares
(`ByteArray`, `Matrix4x4`), the value types kept as constructor calls
(`Vector2Int(..)`), and — from the model — the instance accessor
(`Other_AT(i).hp`). A stub is the OR of all of them, so the split changed
no stub; it can change only which reason a warning names first. (CS8000 is csc's "not yet implemented".)
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
(`tests/unity_golden/corpus.json`). The corpus is **not in git**: `record`
it on the code you start from, before a change, then `check` after. A check
skips cpprust + shivyc validation, which does not change the emitted text,
so it takes seconds; recording runs the unity tests, which takes minutes.

**Fixtures.** `examples/unity_pack/MiniScene` is a self-authored project
(scene, metas, a generated 8×8 PNG) and is tracked whole. SystemsScene is
scripts-only in the repository; the tests that pack the whole project are
marked `needs_systems` and skip unless its scene, art and ProjectSettings
have been dropped in locally. Its golden cases go to a local file beside
the cache and are checked when present.

