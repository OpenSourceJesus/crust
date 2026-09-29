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
  empty rows;
* the tables beside the instance array -- an instance `List`, `T[]`,
  `Dictionary` or `string` field -- are as long as it (they were the
  scene's count, and a clone wrote past the end), and `Instantiate` gives
  the clone's row what Unity does: a serialized field (public, or
  `[SerializeField]`) copied from the original, any other what its
  initializer makes it, and a `Dictionary`, which Unity never serializes,
  empty.

**Awake and Start, per instance.** Every instance, authored or spawned,
gets `Awake` and `Start` once, in Unity's order: a life byte per row
records each. `Instantiate` calls the clone's `Awake` before it returns;
`Start` runs at the next tick, before that object's first `Update` -- an
`Update` loop skips a row until it has started. A destroyed object gets
neither. They used to run once per class at the first tick, for the
instances there then, so a spawned object never started.

**What a clone keeps.** `Instantiate` copies the instance struct, and then
puts back every member Unity does not serialize -- a private field without
`[SerializeField]` -- to what its initializer makes it; a serialized one
keeps the original's value. The struct copy used to hand a clone the
original's private state, a counter or a flag, so it carried on as if it
had already run. `TestSpawnLifecycle` and the fast check's `life` and
`list_cap` cases run both.

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

## Strings: owned storage, members, and coost

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
(`1000000` as `1e+06`). The same typing picks `Debug_Log_i`. A
concatenation may also start from a value (`hp + " hp"`, `hp * 2 + "!"`),
when that value stands alone -- after `(`, `,`, `=` and the like. C#
evaluates left to right, so `x + y + "z"` with integers adds first: it is
`("" + (x + y)) + "z"`, and `"a" + x + y` is `"a34"`, a concatenation
throughout. An all-integer expression is formatted as an integer.

A declaration that cannot be split -- in a `for` head, or several
declarators in one statement -- keeps its marker type (`_cs_string`), and the
method is reported as a stub rather than guessed at.

**Parameters.** A `string` parameter the body uses is copied into an owned
local on entry (`_cs_arg_<name>`, the uses renamed; the declaration shares
the body's first line, so no line moves). A caller may pass a
concatenation's result, which the callee's own concatenations would reuse
while the parameter still pointed at it.

**Fields.** An instance `string` field is not in the instance struct -- it
used to be read as a handle to a class named `string`, and never declared.
It is a table of owned strings beside the instance array, as long as it
(`static fastring Player_label[N]`), and `label` in a method is
`Player_label[i]`; `other.label`, through a handle field, local or
parameter of that class, is `Other_label[other]`. Each row is seeded at
the first tick, before any `Start`, with the value it was authored with --
the scene's (`label: hello`, `'two words'`, `"escaped\n"`), else the
field's initializer -- and again when its scene is reloaded; `Instantiate`
copies it from the original. A writable `static string` is an owned
`fastring` too (it was a `const char[]`, which could not be assigned); a
`const` one stays a literal. A Godot
`[Export] string` is the same field, its value from the `.tscn`.

**Members.** On any string -- a local, field, static, parameter, literal,
or the result of another member, so they chain (`s.Trim().ToUpper()`):

| C# | |
|----|--|
| `Length`, `IndexOf(x)`, `IndexOf(x, start)`, `LastIndexOf(x)`, `CompareTo(x)` | `int` (`x` a string or a char) |
| `Contains(x)`, `StartsWith(x)`, `EndsWith(x)`, `Equals(x)` | `bool` |
| `Substring(a)`, `Substring(a, n)`, `ToUpper()`, `ToLower()`, `Trim()`, `TrimStart()`, `TrimEnd()`, `Replace(a, b)`, `ToString()` | a string |
| `string.IsNullOrEmpty(s)`, `string.IsNullOrWhiteSpace(s)`, `string.Equals(a, b)`, `string.Compare(a, b)` | |

Each is an engine helper taking the receiver as `const char *`; one that
returns a string writes it to a scratch slot, like a concatenation. The
searching is coost's (`str::memmem`, `str::memrmem`), and case, trim and
replace go through a `fastring`. Only the helpers a pack uses are emitted.
They are ordinal -- .NET's defaults for `ToUpper`, `StartsWith` and
`CompareTo` are culture-sensitive, which for ASCII is the same -- and an
argument .NET rejects (`Substring` past the end, an empty `Replace`)
aborts with the exception's name, as an unhandled one ends the process.

**Formatting.** `$"..."`, `string.Format("..", ..)` and a number's
`ToString` are read at pack time, so the format must be a literal:

| C# | packed |
|----|--------|
| `$"hp {hp} of {max:F1}"` | `"" + "hp " + (hp) + " of " + _cs_fmt_F(max, 1)` |
| `string.Format("{0}/{1}", a, b)` | `"" + (a) + "/" + (b)` |
| `speed.ToString("F2")`, `id.ToString("D4")` | `_cs_fmt_F(speed, 2)`, `_cs_fmt_D(id, 4)` |
| `hp.ToString()` (an int or float the method knows) | `"" + (hp)` |

A hole with no spec is a concatenation operand, formatted as its type is;
`{{` and `}}` are braces. Alignment (`{0,5}`), `N0`, `X` and a format that
is not a literal are left as written, so the method is reported as a stub
rather than printed wrongly.

**`string[]`, `Split` and `Join`.** A `string[]` local is a
`std::vector<fastring>`. `s.Split(',')`, `s.Split(", ")` and
`s.Split(',', ';')` return one, keeping empty entries as .NET does;
`parts.Length` is its size, `parts[i]` an owned string (read, written, a
member receiver); `foreach (string p in parts)` -- or over
`s.Split(' ')` directly -- is an index loop with `p` an owned local, on
the same lines; `string.Join(sep, parts)` joins one. `new string[n]` and
the initializer forms are in *Runtime* below. A `string[]` field and a
`List<string>` are not packed yet.

**Null.** A fastring has no null distinct from empty, so a string that is
null reads as "": `s == null` is `string.IsNullOrEmpty(s)`, `s != null`
its negation, and `s = null` assigns "". Right wherever a program does not
tell null and "" apart.

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

`TestOwnedStrings` packs, builds and runs each case, and so does
`tools/unity_pack_features.py` (below).

## Runtime: `Mathf`, `Random`, `Parse`, `Path`, `Directory`, `File` reads, `StringBuilder`, `JsonUtility`

Common .NET and Unity static APIs are one table,
`tools/unity_pack_runtime.py`: each C# spelling maps to an engine helper,
its C (in the subset cpprust lowers) and its result type, so the typed
concatenation and `Debug.Log` format a result as they format anything else.
Only the helpers a pack uses are emitted, with the ones they call.

| C# | |
|----|--|
| `Mathf.Sqrt`, `Pow`, `Floor`, `Ceil`, `Round`, `FloorToInt`, `CeilToInt`, `RoundToInt`, `Tan`, `Asin`, `Acos`, `Atan`, `Atan2`, `Exp`, `Log` (1 or 2 args), `Log10`, `Clamp01`, `InverseLerp`, `LerpUnclamped`, `MoveTowards`, `Repeat`, `PingPong`, `DeltaAngle`, `SmoothStep`, `Approximately` | as Unity; `Round` halves to even, as .NET does |
| `Mathf.PI`, `Deg2Rad`, `Rad2Deg`, `Epsilon`, `Infinity`, `NegativeInfinity` | constants |
| `Random.Range(a, b)`, `Random.value`, `Random.InitState(seed)` | int `Range` excludes `b`, float includes it; both ints picks the int one |
| `int.Parse`, `float.Parse`, `double.Parse`, `int.TryParse(s, out n)`, `float.TryParse(s, out int f)` | an `out` declaration is hoisted before its statement |
| `Path.Combine` (2+ args), `GetFileName`, `GetExtension`, `GetFileNameWithoutExtension`, `GetDirectoryName` | `/` separators |
| `Directory.Exists`, `Directory.CreateDirectory` | |
| `File.ReadAllText`, `File.ReadAllLines` | a UTF-8 BOM is dropped; lines split on `\n`, a `\r` before it dropped |
| `Time.realtimeSinceStartup`, `Time.unscaledTime` | `Time.time` (below) |
| `s.GetHashCode()` on a string | deterministic FNV-1a |

Where the packed engine differs from .NET: `float.Parse` reads the
invariant culture (`.` decimal point); `Random` is a seeded xorshift32
(Unity seeds from the clock), so a run repeats unless the script calls
`Random.InitState`; `realtimeSinceStartup` is the engine's time, since it
has no time scale and reads no clock; a string's hash is not .NET's
(which is randomized per process anyway), so only equality of hashes
means anything. Input `Parse` rejects, a file `ReadAllText` cannot open,
or a directory `CreateDirectory` cannot make aborts with the .NET
exception's name, as an unhandled exception ends the process. The
helpers keep to what crust's own C front end has -- no `strtod` (the
decimal reader is the runtime's), no `EOF`, and nothing POSIX outside
`#ifndef CRUST_NO_POSIX_MKDIR` (without it, `Directory` falls back to
`fopen`).

**Bools print as C# prints them.** `"alive " + alive` is `alive True`
and `Debug.Log(ok)` prints `False`; the engine printed 1 and 0. A bool
next to a binary `+` can only be in a concatenation, so a bool variable,
field, or helper result there becomes `(b ? "True" : "False")`. A
conditional takes its branches' type in a concatenation, so
`"x" + (ok ? "in" : "out")` is a string, not a float.

**`StringBuilder`.** A `StringBuilder` local is a coost `fastring` it
appends to in place: `Append(x)` formats `x` as a concatenation would (an
int, float, bool or char as C# prints it) and copies only that. `AppendLine`,
`AppendFormat` (through the format lowering), `Clear`, `Replace`,
`ToString`, `Length` and chained statements (`sb.Append(a).Append(b);`, one
statement per call, in braces) are lowered; a builder passed to a method or
kept in a field is not, and the method is reported.

**`JsonUtility`.** `JsonUtility.ToJson(obj)`, `ToJson(obj, pretty)` and
`FromJsonOverwrite(json, obj)` on a packed object -- `this`, or a handle
field, local or parameter of another class -- are functions generated per
class at pack time (`_Player_ToJson(i, pretty)`), over the fields Unity
serializes (public, or `[SerializeField]`), in declaration order: `int`,
`float`, `bool`, `string`, `Vector2`, `Vector3`. The text is Unity's: a
float always has a point (`2.0`) and is the shortest that reads back as
the same float, strings are escaped, pretty output indents four spaces.
`FromJsonOverwrite` sets the fields the JSON has and leaves the rest, skips
keys it does not know (nested values included), reads `\uXXXX` as UTF-8,
and a document that is not an object is .NET's `ArgumentException`. When a
project calls it, integer fields keep their full C# width: the packer
narrows a field to what its authored values need, and a value read from
JSON is not one it can see. A class with a serialized field of another
type (a reference, a collection) is not lowered, and the method is
reported rather than writing JSON without it.

**`new string[n]`**, `new string[] { .. }`, `new[] { .. }` and `{ .. }`
make a `string[]` of that size (C#'s nulls read as ""), then write each
initializer element.

**Clock.** `Stopwatch` (`StartNew`, `new Stopwatch()`, `Start`, `Stop`,
`Reset`, `Restart`, `ElapsedMilliseconds`, `Elapsed.TotalSeconds` /
`TotalMilliseconds`, `IsRunning`) and `DateTime.Now` / `UtcNow` (`Year`,
`Month`, `Day`, `Hour`, `Minute`, `Second`, `Millisecond`, `DayOfYear`,
`ToString()` and `ToString(format)` with .NET's custom tokens `yyyy yy MM M
dd d HH H hh h mm m ss s fff ff f tt`, quoted text and `\x`) read
`clock_gettime` / `localtime_r`: the player is built with gcc. They sit
behind `#ifndef CRUST_NO_POSIX_MKDIR` like the file helpers, so the pack's
validation through crust's own C front end (no `<time.h>`) still passes;
there the clock reads 0. A stopwatch or a date is a local here: kept in a
field, passed to a method, or subtracted (a `TimeSpan`), it is not lowered,
and the method is reported. `DateTime.ToString()` is the invariant
culture's general form, `MM/dd/yyyy HH:mm:ss`, not the machine's culture.

## Static helper classes and extension methods

A `static class` has no instances, so there is nothing to pack it as;
until now any call into one (`Util.Twice(hp)`) left the calling method a
stub. They are rewritten at the source level first
(`tools/unity_pack_extensions.py`), and the analysis and the lowering read
the rewritten text (`SOURCE_OVERLAY` in `unity_pack_common`):

* a C# 14 extension block -- `extension (GameObject go) { .. }` -- becomes
  classic static members, the receiver their first parameter; an extension
  property `P` becomes a method `get_P(this T x)`;
* an extension call `x.M(a)`, `x.M<T>(a)` or `x.P` becomes the static call
  `Cls.M(x, a)`, `Cls.M<T>(x, a)`, `Cls.get_P(x)` (a method of the
  project's own classes with the same name shadows it);
* a static method whose body is one `return expr;` (or `=> expr`) is
  inlined where it is called, as before; any other one, and every generic
  one, is copied into the calling class as a private static method --
  `Util__Twice`, or `UnityExtensions__GetOrAddComponent__Badge` for each
  type argument, `T` substituted -- with its calls to its siblings, its
  class's consts and its own extension calls rewritten the same way, and
  what it calls copied too.

```csharp
public static class UnityExtensions {
    extension (GameObject go) {
        public bool IsActiveInHierarchy => go.activeInHierarchy;
        public T GetOrAddComponent<T>() where T : Component {
            T component = go.GetComponent<T>();
            if (component == null) component = go.AddComponent<T>();
            return component;
        }
    }
}
// in a MonoBehaviour:
Badge b = gameObject.GetOrAddComponent<Badge>();  // packs, adds once
```

Extension methods on Unity value types work the same way: `Vector2` is a
parameter and return type the engine has (its C struct), so

```csharp
public static Vector2 SetZ(this Vector2 v, float z) { return new Vector2(v.x, z); }
public static void Example2(this float f) { }
// a.SetZ(5f), aim.SetZ(-1f) on a packed Vector2 field, speed.Example2()
```

pack and run. The packer's own `SetX` / `SetZ` inside a
`t.SetWorldScale(..)` argument stay its own; everywhere else a project's
`SetX` / `SetY` / `SetZ` are its extension methods. `GetWorldRect`,
`SetWorldScale` and a static array's `Add` / `Remove` are always the
packer's.

Each copy goes on its own line after the class's last one, so the class's
lines -- and the diagnostics pointing at them -- stay where the author
wrote them; only another top-level type later in the same file moves. A
method that reads or writes a non-const static field of its class is not
copied (each class would get its own copy of shared state), and its calls
are left for the stub check; so is a generic call whose type arguments are
inferred rather than written (`x.M()` for `M<T>(this T x)`). An API a
helper uses counts for the classes that call it (`SOURCE_API_HINTS`).

## Methods that return values, and other objects' fields

A MonoBehaviour's methods emitted as `static void`, and one returning a
value was a stub. A method may now return `int` (and the other integer
types), `bool`, `float`, `double`, `string`, `Vector2`, a packed component
or a `GameObject` (their index), and take a `Vector2` parameter (it was
passed as an `int`, a handle); the type has a C value, and anything else --
a coroutine's `IEnumerator`, a `Vector2`, a collection -- keeps the stub.
A returned string is copied to a scratch slot (`_cs_str_ret`), so an owned
local's text is not freed under the caller. Calls are typed where they are
used: `"x" + Score()` formats an integer, `"ok " + Alive()` a bool. Static
methods are forward-declared like instance ones.

A field of a packed object reached through a local or parameter of its
class (`Badge b = ..; b.n = 9;`) reads and writes its slot
(`Badge_AT(b).n`), as a handle field's does. `go.AddComponent<T>()` on a
`GameObject` variable adds to that GameObject (it added to this one).
`gameObject.activeSelf` / `activeInHierarchy` -- this object's or a
variable's -- read the engine's active tables. A comparison or logical
expression in a concatenation prints as a bool (`"ok " + (n >= 0)` is `ok
True`), and `s[k]` on a string is a `char`.

## Collections: Stack, Queue, HashSet, List members, `T[,]`

`Stack<T>`, `Queue<T>` and `HashSet<T>` were refused; they are rewritten at
the source level (`tools/unity_pack_collections.py`, in the same overlay as
the extension methods) into the `List<T>` the packer lowers -- so locals,
instance fields, statics and every element type the list lowering has work
for them too:

| C# | as a List |
|----|-----------|
| `s.Push(x)`, `q.Enqueue(x)` | `Add` |
| `s.Peek()`, `q.Peek()` | `s[s.Count - 1]`, `q[0]` |
| `s.Pop()`, `q.Dequeue()` | hoisted before the statement: an emptiness check (.NET's `InvalidOperationException`), the element into a temporary, `RemoveAt` |
| `foreach` over a `Stack` | top first, as .NET |
| `h.Add(x)` | `if (!h.Contains(x)) h.Add(x)`; as a value, hoisted with its bool |
| `h.UnionWith(o)`, `IntersectWith`, `ExceptWith` | loops |

**`LinkedList<T>`**, a subset without nodes: `AddLast` is `Add`,
`AddFirst` is `Insert(0, ..)`, `RemoveFirst` / `RemoveLast` remove at an
end, `First.Value` / `Last.Value` read one -- each end checked, as .NET's
null `First` would throw -- and `Count`, `Clear`, `Contains`, `Remove(x)`
and `foreach` are the list's. A `LinkedListNode` (`.First` kept as a node,
`.Next`, `AddAfter`, `Find`) is left for the stub check.

A take hoisted from inside a `while` / `for` header would run once, not
each time round, and is left for the stub check; one in an `if` / `switch`
condition is hoisted before it. A `HashSet` keeps insertion order (.NET's
until an element is removed) and its `Contains` is linear; a `Dequeue`
moves the rest down.

The packed `List` had only `Add`, `Clear`, `Count`, indexing and a field's
`foreach`. It now has `RemoveAt` and `Insert` (index-checked, .NET's
`ArgumentOutOfRangeException`), `Contains`, `IndexOf` and `Remove` (a
search helper per element type), and `foreach` over a local (an index
loop). A `List<string>` is a vector of owned coost `fastring`s, as a
`string[]` is -- it could not take a literal before; a `Dictionary`'s string
keys and values are unchanged.

**Multidimensional arrays.** `T[,]` and `T[,,]` of `int`, `float`, `bool` or
`string` are a `List<T>` (row-major, as .NET lays them out) and an `int`
per dimension: `new T[a, b]` stores the dimensions and fills in
`default(T)`, `g[x, y]` is the flat index through a helper that checks each
one (`IndexOutOfRangeException`), and `GetLength(k)`, `Length`, `Rank` and
`foreach` work. A field's initializer is filled at the start of `Awake`
(one is made if the class has none); its dimension fields go after the
class's last line. An array literal (`{ {1, 2}, .. }`) or a `T[,]`
parameter is left for the stub check.

## Coroutines

An `IEnumerator` method of a MonoBehaviour that `yield`s is rewritten at
the source level (`tools/unity_pack_coroutines.py`) into a state machine
of private fields and methods of its class -- things the packer lowers
like any other:

* its parameters and locals become fields (`_co_Blink_k`), so they survive
  a `yield`; the body becomes `bool _co_Blink_step()`, which a `switch`
  enters at the resume point of its last `yield` (a `goto` into the loop
  body holding it);
* `yield return null` -- and `0`, `WaitForEndOfFrame`, `WaitForFixedUpdate`
  -- waits for the next frame; `yield return new WaitForSeconds(t)` until
  `Time.time` has moved on by `t`; `yield break` ends it. The frame is an
  `int` count per object and the deadline integer milliseconds: a class
  that does not move packs its floats as halves, and a stored time read
  back below `Time.time` resumed a null yield in the frame it yielded in;
* `StartCoroutine(Blink(3))`, `StartCoroutine("Blink")` and
  `StartCoroutine(nameof(Blink))` set the parameters and run the body to
  its first `yield` at once, as Unity does; `StopCoroutine(..)` and
  `StopAllCoroutines()` clear the state;
* each frame, after the object's `Update` -- the author's is renamed and
  called from one made to call both, so its early `return` does not skip
  them -- `_co_tick()` resumes each coroutine whose wait is over.

* `yield return StartCoroutine(Child(..))` -- or `yield return
  Child(..)` -- of another coroutine of the same class starts it (to its
  first `yield`) and waits, a field saying which, until it has ended. The
  tick runs the coroutines in declaration order, once per coroutine, so a
  parent resumes in the frame its child ends, however they are ordered.

Each object has its own fields, so each runs its own coroutines; starting
one that is already running restarts it (Unity would run a second). A
coroutine keeps running after its object is disabled. Left for the stub
check: another object's coroutine, `WaitUntil` / `WaitWhile` (a lambda), a
`yield` inside a `foreach`, a `var` whose type the rewrite cannot see, and
a `Coroutine` kept in a variable.

## `byte[]`, `Encoding`, Base64, MD5 / SHA-256

In a file that builds, converts or hashes bytes -- `Encoding`, `Convert`'s
Base64, `MD5` / `SHA256`, `BitConverter`, or a sized `new byte[n]` -- a
`byte[]` is a `List<byte>` (`tools/unity_pack_collections.py`), and the
byte APIs are runtime helpers over it:

| C# | |
|----|--|
| `new byte[n]`, `new byte[] { .. }`, `b.Length`, `b[i]`, `foreach (byte x in b)` | the list's (a `byte` local is an `int`, as the list holds it) |
| `Encoding.UTF8` / `ASCII.GetBytes(s)`, `.GetString(b)` | UTF-8 bytes and back |
| `Convert.ToBase64String(b)`, `Convert.FromBase64String(s)` | coost's `base64_encode` / `_decode`; bad input is .NET's `FormatException` |
| `MD5.Create()` (a local, or in a `using`) then `.ComputeHash(b)`; `MD5.Create().ComputeHash(b)`; `MD5.HashData(b)`; `SHA256` alike | coost's `md5digest_to` / `sha256digest_to` |
| `BitConverter.ToString(b)` | `"AB-CD-.."` |
| `b.ToString("x2")`, `"X2"`, `{0:x2}` | hex |
| `File.ReadAllBytes`, `File.WriteAllBytes` | over the list, in such a file |

coost's hash and Base64 sources are spliced into the engine only when it
calls them. A byte helper's argument that is itself a byte helper's result
is hoisted into a temporary first (a reference parameter needs an
address). A file whose bytes only go to and from `File.ReadAllBytes` /
`WriteAllBytes` keeps the packer's `ByteArray` view, as before.

## `GetType`, `typeof`, `is`, `nameof`

A packed object's class is known when packing -- `this`, or a handle
field, local or parameter of a packed class -- so these are constants:
`GetType().Name` (and `.FullName`, `.ToString()`) and `x.GetType().Name`
are the class's name, `typeof(T).Name` is `"T"`, `GetType() == typeof(T)`
is decided, `x is T` for an `x` declared a `T` is `x != null`, and
`nameof(x)` is `"x"`. A type used any other way -- reflection, a `Type`
kept in a variable -- is left as written, and the method is reported.

## Box2D-Packed: triggers and the Rigidbody2D API

2D physics is Box2D-Packed (`box2d_unity.py` in its checkout generates
`physics_box2d.c`, which steps a Box2D world over the packed tables).

**Triggers.** `OnTriggerEnter2D`, `OnTriggerStay2D` and `OnTriggerExit2D
(Collider2D other)` are sent -- Unity mode's trigger colliders were Box2D
sensors that told no one. When a script has one, the plan's
`physics2d_triggers` has the glue enable sensor events and report each
step's overlapping sensor pairs with `engine_col2d_trigger(a, b)`, apart
from the touching pairs; the engine sends Enter / Stay / Exit by comparing
them with the step before, as it does collisions. With `--physics-inject`
the sensor begin / end events are injected into Box2D-Packed, as the
contacts are.

**The other collider.** In a collision or trigger handler, the parameter
-- `Collision2D coll` or `Collider2D other`, the other collider's index --
reads its GameObject: `other.gameObject` (`_col2d_go`), and its `name`,
`tag`, `CompareTag(..)`, `GetComponent<T>()`, `SetActive(..)` and
`Destroy(other.gameObject)`; on a Collider2D those members are its own,
and mean the same. `gameObject.CompareTag(..)` / `.tag` -- this object's,
a bare `CompareTag(..)`, or a GameObject variable's -- read the authored
`m_TagString` (`_engine_go_tag`).

**Rigidbody2D.** On a Rigidbody2D field, local or
`GetComponent<Rigidbody2D>()`, as the engine's `Rigidbody2D_*` over the
tables the glue pushes before each step:

| C# | |
|----|--|
| `AddForce(F)`, `AddForce(F, ForceMode2D.Impulse)` | the velocity change Unity's step makes: F·dt/m, F/m; not on a body that is not dynamic |
| `position`, `position = V`, `MovePosition(V)` | the owner's position; a write is a teleport (Unity moves a kinematic body through space) |
| `mass`, `gravityScale`, `drag` / `linearDamping`, `bodyType`, `isKinematic` | get, set, `op=` |
| `velocity` / `linearVelocity` | as before |

The glue now pushes a changed `mass` (the shape's mass data scaled to it,
as at creation) and a changed `bodyType` (`b2Body_SetType`); it pushed
velocity, a moved position, gravity scale and damping already. A
`velocity` assigned any Vector2 expression (`Vector2.zero`, a local) is
set too.

**Rotation.** A Rigidbody2D turns, as in Unity, unless it is static or its
`m_Constraints` freeze rotation (`RigidbodyConstraints2D.FreezeRotation`);
it was locked. Its owner's class keeps a live rotation (so its sprites draw
turned), the plan's `physics2d_rotation` has the glue start each body at
its owner's authored angle and angular velocity and pull both back after
every step (`engine_rb2d_get_rot` / `set_rot`, radians), and a teleport
keeps the rotation. Scripts have, in Unity's degrees:

| C# | |
|----|--|
| `rotation`, `rotation = a`, `MoveRotation(a)` | the owner's angle (a write is pushed as a turn in place) |
| `angularVelocity` | get, set, `op=` |
| `AddTorque(t)`, `AddTorque(t, ForceMode2D.Impulse)` | applied by Box2D (`b2Body_ApplyTorque` / `ApplyAngularImpulse`), which knows the inertia |
| `freezeRotation` | get, set (the motion lock follows) |
| `transform.eulerAngles.z` | of a turning body's own Transform, [0, 360) |

With no turning body in the scene the API reads 0 and writes nothing.
Godot mode keeps its bodies' rotation locked.

**Joints.** `HingeJoint2D`, `DistanceJoint2D`, `SpringJoint2D`,
`FixedJoint2D`, `SliderJoint2D`, `WheelJoint2D`, `FrictionJoint2D`,
`RelativeJoint2D` and `TargetJoint2D` are read from the scene
(`plan["joints2d"]`, the `_Joint2D_*` tables in data.c) and built by
Box2D-Packed as revolute, distance (rigid; a rope with `maxDistanceOnly`),
distance with a spring, weld, prismatic and wheel joints, and the last
three as its motor joint: friction is velocity control to rest capped at
`maxForce` / `maxTorque`; relative is a spring to the linear and angular
offset (`autoConfigureOffset` as Unity has it), capped the same, whose
frequency is `correctionScale`'s -- Box2D v2's motor joint corrected that
fraction of the error a step, a spring of sqrt(scale) / (2 pi dt) hertz;
target is a spring (`frequency`, `dampingRatio`, `maxForce`) pulling the
anchor to a world point (`autoConfigureTarget`: where the anchor starts),
the body free to turn. They are built with the bodies -- a script's `Start`
sees them. The joint links its own body to the
connected one (a wheel joint: the chassis it is on to the wheel), or to a
static ground body at the origin when there is none; Unity's anchor and
connected anchor are the frames' points (`autoConfigureConnectedAnchor`,
`autoConfigureDistance` and a slider's `autoConfigureAngle` as Unity
configures them), and a hinge's limits and angle are relative to its pose
at creation. A GameObject with a joint and no Rigidbody2D gets the one
Unity adds (dynamic, mass 1, gravity 1).

Scripts reach a joint through a field of a joint type (set by
`GetComponent<XJoint2D>()`, or a serialized reference), a local, or
`GetComponent<XJoint2D>()` itself; `== null` is no joint, or a broken one.
Its members, pushed before each step when they change and read back after
it:

| C# | |
|----|--|
| `enabled`, `useMotor`, `useLimits`, `enableCollision`, `maxDistanceOnly`, `distance`, `frequency`, `dampingRatio`, `breakForce`, `breakTorque` | get, set, `op=` |
| `motor` (`JointMotor2D`), `limits` (`JointAngleLimits2D` / `JointTranslationLimits2D`), `suspension` (`JointSuspension2D`) | get, set; `new JointMotor2D { motorSpeed = .., maxMotorTorque = .. }`; `motor.motorSpeed`, `limits.min` read directly |
| `maxForce`, `maxTorque`, `correctionScale`, `angularOffset`, `autoConfigureOffset`, `autoConfigureTarget`, `breakAction` | get, set |
| `target`, `linearOffset` (Vector2) | get, set (a moved target moves the spring's end) |
| `jointAngle`, `jointSpeed` (degrees), `jointTranslation`, `connectedBody`, `attachedRigidbody`, `reactionForce`, `reactionTorque`, `GetReactionForce(dt)`, `GetReactionTorque(dt)` | get (the reaction is Box2D's constraint force / torque after the last step) |

Hinge and wheel motor speeds and hinge limits are in degrees, as Unity's
(Box2D clamps a hinge's limits to ±178°). **Breaking**: `breakForce` and
`breakTorque` are Box2D's force and torque thresholds; a joint past one
gets its `breakAction` (`m_BreakAction`, `JointBreakAction2D`): `Destroy`
(the default) removes it -- `GetComponent` no longer finds it -- `Disable`
removes it from the world and sets `enabled` false (enabling it again
builds it again), `CallbackOnly` keeps it, and each of them sends
`OnJointBreak2D(Joint2D)` to its GameObject's scripts; `Ignore` never
breaks.

A Rigidbody2D's authored `m_GravityScale: 0` is kept; it was read as the
default, 1.

**Queries.** `Physics2D.Raycast(origin, direction[, distance[,
layerMask]])`, `RaycastAll`, `OverlapCircle(point, radius[, layerMask])`,
`OverlapCircleAll`, `OverlapPoint(point[, layerMask])` and
`OverlapPointAll` are Box2D-Packed's (`engine_box2d_raycast[_all]` /
`_overlap_circle[_all]` / `_overlap_point`, the plan's
`physics2d_queries`), and may run before the first step (a script's
`Start`: the glue builds the world first). `RaycastAll` is nearest first,
as Unity's; the `*All` arrays (`RaycastHit2D[]`, `Collider2D[]`) are lists,
so `foreach`, `hits[i]` and `.Length` work. A
`RaycastHit2D` is the engine's struct -- `collider` (an index, -1 for
none), `point`, `normal`, `distance`, `fraction` -- `if (hit)` is a hit,
and `hit.collider`, `hit.transform` and a `Collider2D` an overlap returns
read their GameObject as a handler's parameter does (`.gameObject`,
`.name`, `.tag`, `CompareTag`, `GetComponent<T>()`); `transform.position`
as the origin is taken by its x and y. Triggers are hit, as Unity's
`queriesHitTriggers` default has it, and a ray ignores a collider it
starts inside (Unity's `queriesStartInColliders` default would hit it). The
`*NonAlloc` forms are not lowered.

**Layers.** Each collider has its GameObject's `m_Layer`
(`_Collider2D_layer`), and a query's layer mask is tested against it in the
glue's callbacks -- Box2D-Packed's filters are 16 bits, Unity has 32
layers, and contacts are left alone (the layer collision matrix is not
read). With no mask a query takes `Physics2D.DefaultRaycastLayers`: every
layer but "Ignore Raycast". At the source level, a `LayerMask` (field,
local, parameter) is an `int` -- a field's scene value is its `m_Bits` --
`mask.value` is the mask, and `LayerMask.GetMask("A", ..)` /
`NameToLayer("A")` are constants from the project's layer names
(`ProjectSettings/TagManager.asset`, Unity's built-in names without one;
an unknown name is no bit / -1). A `RaycastHit2D` is a bool wherever C#
converts it: `if (hit)`, `hit ? a : b`, `hit && ..`.

## Fast feature check: `tools/unity_pack_features.py`

The full suite packs a few hundred projects and takes most of a quarter
hour. For the features being added now there is a fast check: it packs one
Unity project, one Godot project and -- with a Box2D-Packed checkout -- one
physics scene, each holding every feature under test, builds and runs them,
and compares what they print with what C# prints. About half a minute.

```
python3 tools/unity_pack_features.py              # unity, godot, box2d
python3 tools/unity_pack_features.py godot -v     # one project, every line
python3 tools/unity_pack_features.py --keep       # keep the packed projects
python3 tools/unity_pack_features.py --asan       # players under ASan + UBSan
```

Each check is a C# method of its own, called from `Start`, printing lines
that begin with its name. A method the packer could not lower is emitted
empty (warning CS8000) and prints nothing, so it cannot pass by accident;
its warning is shown with the failure. Adding a check is adding an entry
to `UNITY_CHECKS` / `GODOT_CHECKS`: the method body and the lines C# would
print. It needs a C compiler and a coost checkout; the Box2D project is
skipped, and says so, without Box2D-Packed.

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
| `string` locals | `lower_local_types`, then unity_pack's `_own_string_locals` | a coost `fastring` each (see [Strings](#strings-owned-storage-members-and-coost)) |
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

