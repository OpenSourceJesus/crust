# UNITY_PACK_SYSTEMS — authored Unity systems only

`unity_pack` speeds up what is already in the user's Unity (or Godot)
project. It **does not invent assets**: no synthetic ParticleSystem
pools, no default AnimationCurves, no scripted UI invent, no InputAction maps.
Runtime `AddComponent<T>()` works for packed builtins (Camera, Light,
SpriteRenderer, Rigidbody/2D, Box/Circle/Sphere colliders, Animation,
Animator, AudioSource) and for authored MonoBehaviours — GetOrAdd into a
pre-sized pool (one spare slot per calling instance). AudioSource allows
multiple sources on one GO (Unity does not DisallowMultiple). Authored scene
Canvas + Image/Button/TextMeshProUGUI
are drawn; `using UnityEngine.UI` and Image/Button fields are allowed. Scripted invent
(`AddComponent<Canvas>`, `typeof(Canvas)`, `ForceUpdateCanvases`) stays refused. Scripts that need
other Unity features keep them in the authored project until the packer can
import them; calling
invent-requiring APIs today is a hard `PackError`.

Emitted `engine.cpp` / `data.cpp` / `main.cpp` (C++-subset twins) are
gated through `cpprust.translate`, then the translated C is compiled with
crust/`shivyc`. Leaving that subset or failing crust compile is a
`PackError` on the generated file. Pack writes the translated C as
`engine.c` / `main.c` (and `data.c` when not skipped for size) so the host
`Makefile` / player build compile with `gcc` (`CC=clang` for clang) —
`List` / `GetComponentsInChildren` become crust `vector_int` rather than
linking `libstdc++`. Engine objects use `-O3 -fno-math-errno` (math loops
can autovec). `make vectorize-report` compiles `engine.c` with those flags
plus `clang -Rpass-missed=loop-vectorize,slp-vectorize` so missed
auto-vectorization remarks print on stderr. `make crust-check` recompiles
the lowered `.c` files with crust. Microbenches
`tools/unity_pack_bench_upload.py` and `tools/unity_pack_bench_csharp.py`
time C under both gcc and clang when both are on PATH (`--cc` to restrict).

What *is* lowered: APIs and methods on the MonoBehaviours / scene
instances that are already placed.

## Input (host-fed)

| Script uses | Emitted |
|-------------|---------|
| `Input.GetAxis("Horizontal"\|"Vertical")` | Host floats `engine_input_axis_*` |
| `Input.GetButton("Jump")` | Host int `engine_input_button_Jump` |
| `Input.GetKey("a")` | Host table `engine_input_key[256]` |
| `Keyboard.current` | Non-NULL when `engine_keyboard_connected` |
| `Keyboard.current.<name>Key.isPressed` | Host int `engine_keyboard_<name>` |

Legacy Input Manager and Input System `Keyboard.current` (connected
device + key state). Bare `Keyboard` needs `using UnityEngine.InputSystem;`
or `UnityEngine.InputSystem.Keyboard` — no invented global alias.
`InputAction` maps and `Gamepad.current` are still refused (would invent
action maps / device graphs).

## Logging (player log)

| Script uses | Emitted |
|-------------|---------|
| `Debug.Log(msg)` / `print(msg)` | Unity **Player.log** path + newline |
| `-logFile path` / `-logFile -` | `engine_apply_argv` → file or **stdout** |
| `System.Console.WriteLine(msg)` | **stdout** (terminal), not Player.log |
| `WriteLine` / `Debug.Log` of a `GameObject` | `Object.ToString` → `name (UnityEngine.GameObject)` (missing → `"null"`) |
| `Application.dataPath` | Packed project `Assets/` absolute path (**Awake/Start only**) |
| `Application.persistentDataPath` | Unity company/product save dir (**Awake/Start only**) |
| `Application.isEditor` | Always **false** (packed player) |
| `Application.isPlaying` | Always **true** while the packed player runs |
| `#if UNITY_EDITOR` / `UNITY_ANDROID` / `UNITY_IOS` | Inactive for pack (desktop standalone defines); `#else` kept |
| `Application.productName` | Baked `ProjectSettings` `productName` (else project folder name) |
| `Application.OpenURL(url)` | `system("python3 -c \"import webbrowser; webbrowser.open('…')\"")` |
| `Application.Quit()` / `Quit(code)` | Sets flag; host polls `engine_wants_quit()` (no Escape/Q shortcut) |
| Other `Application.*` | Pack-time **CS0117** (`Application` in scope via `using UnityEngine`) |
| Other `Quaternion.*` (not Euler / identity / LookRotation / Slerp / Inverse / Angle) | Pack-time **CS0117** (`Quaternion` in scope via `using UnityEngine`) |
| `File.WriteAllText(path, text)` | `fopen` write (`"w"`); creates parent dirs when possible |
| `File.AppendAllText(path, text)` | `fopen` append (`"a"`); creates parent dirs when possible |
| `File.WriteAllBytes(path, bytes)` | `fopen` write (`"wb"`) + `fwrite`; `byte[]` → `ByteArray`; `new byte[]{…}` → static buffer helper |
| `File.ReadAllBytes(path)` | `fopen` read (`"rb"`) + chunked `fread` → malloc'd `ByteArray` (`.Length` / `[i]` lowered) |
| `File.Exists(path)` | `fopen` probe (`"rb"`) → 1 / 0 (dirs fail like .NET) |
| `File.Delete(path)` | `remove(3)`; missing path is a no-op (no throw) |
| `File.Copy(src, dest)` / `Copy(src, dest, overwrite)` | `fread`/`fwrite`; mkdir dest parent; no overwrite → no-op if dest exists |
| `File.CreateText(path)` | `fopen` `"w"` (+ mkdir) → `StreamWriter` (`FILE*`); `.WriteLine` / `.Close` |
| `File.OpenText(path)` | `fopen` `"r"` → `StreamReader` (`FILE*`); `.ReadLine` / `.Close` |
| Other `File.*` | Pack-time **CS0117** (`File` in scope via `using System.IO`) |

Default log path matches Unity standalone (from `ProjectSettings`
`companyName` / `productName`, else `DefaultCompany` / project folder):

| OS | Path |
|----|------|
| Linux | `~/.config/unity3d/<company>/<product>/Player.log` |
| macOS | `~/Library/Logs/<company>/<product>/Player.log` |
| Windows | `%USERPROFILE%\AppData\LocalLow\<company>\<product>\Player.log` |

Not the process cwd. Terminal output needs `-logFile -` or
`System.Console.WriteLine` (`using System;` or FQN). Optional `Debug.Log`
context arg ignored. `engine_console_log_path()` mirrors
`Application.consoleLogPath`. `Application.dataPath` is the project's
`Assets/` folder (Editor semantics). `Application.persistentDataPath` matches
Unity standalone (Linux `~/.config/unity3d/<company>/<product>`, macOS
`~/Library/Application Support/...`, Windows `%USERPROFILE%\AppData\LocalLow\...`).
Calling either from a MonoBehaviour field initializer / `.cctor` raises
Unity's `UnityException` / `TypeInitializationException` every frame and
**does not** run that script's `Start`/`Update` (same as Unity).
`Start` runs once before first `Update`.

## GameObject lookup

| Script uses | Emitted |
|-------------|---------|
| `GameObject.Find(name)` | Runtime `strcmp` on authored GO name table → index or **-1** |
| `Object.ToString` (via printing a Find result) | `name (UnityEngine.GameObject)`; missing → `"null"` |
| `.GetComponent<T>()` on a null GO | **NullReferenceException** with `Class.Method () (at path:line)`; method exits (Unity) |
| `.GetComponent<T>()` on a live GO | Instance index of authored/`AddComponent` `T`, or **-1** if absent |
| `FindObjectOfType<T>(includeInactive?)` | Scan live `_engine_go_T[]` (skip Destroyed; optional inactive) → first index or **-1** |
| `T.Instance` / `T.instance` | ``T_Instance()`` — cache until destroyed/missing, then ``FindObjectOfType<T>(true)`` |
| `Find(...).GetComponent<T>().field` | NRE if Find missed or component/field receiver is null |
| `transform.Find(name)` / nested `"A/B"` | Child GO via **live** parent table (seeded from authored `m_Father`; updated by `SetParent`) → index or **-1** |
| `go.transform.Find` / `Transform` local `.Find` | Same — receiver is the live GO index |

Parsed with cpprust `_match_paren` / `_match_angle` (same AST helpers
csrust uses). Find **does not** fail at pack time for unknown names —
lookup is runtime only (Unity null). Calling a method or reading a field on
that null is a `NullReferenceException` (logged with script site); `setjmp`
unwinds the current `Start`/`Update` so the player keeps running. `GetComponent<T>`
still requires `T` to be an authored packed MonoBehaviour (no invented
component types). `transform.Find` walks the **live** parent table (seeded
from authored `m_Father`, updated by `SetParent`) — not a pack-time bake.

## Animation (script motion + authored clips)

| Authored | Packed behaviour |
|----------|------------------|
| Authored `!u!111` Animation + **legacy** `.anim` | Plays authored root `m_PositionCurves` (`engine_animation_tick`) |
| Authored `!u!95` Animator + `.controller` | Default state motion **only if the clip is non-legacy** |
| `m_Legacy: 1` on `.anim` | Legacy → `Animation` only; Mecanim → `Animator` only (Unity) |
| `m_PlayAutomatically` / Animator default | Starts playing; loops when `m_LoopTime` / WrapMode Loop |
| Empty `m_PositionCurves` (sprite PPtr only) | Advances clip time; **does not** write Transform (no invent) |
| Authored `m_PPtrCurves` `attribute: m_Sprite` | Discrete hold-sample; swaps path child's SpriteRenderer tex (Idle → Graphics) |
| `Mathf.Sign` | `Mathf_Sign` (−1 / 0 / 1) |
| Extensions `Vector2.SetX` / `SetZ` + `Transform.SetWorldScale` | Live scale on referenced Transform GO; sprite half-extents × scale |
| Script field initializers (`float xSize = 1`) | Seed packed instance data when the scene omits the value |

Root position keys write absolute `localPosition` from the clip (Bob.anim
x=2 places Spinner/Wave at x=2 while y bobs). Sprite PPtr keys resolve the
curve `path` under the Animator/Animation owner (`Graphics` child of Player)
and pack any referenced PNG even if it is not an initial SpriteRenderer
sprite. Child-path **transform** curves, blend trees,
Avatar masks, Animator parameters / transitions, and skeletal skins are not
sampled yet. `AnimationCurve.Evaluate` without an authored curve asset remains
refused.

## Particles

Refused. `ParticleSystem.Emit` needs a ParticleSystem in the project;
the packer will not invent a pool.

## Lighting

| Script / scene uses | Emitted |
|---------------------|---------|
| `RenderSettings.ambientLight.{r,g,b}` | Host-pokeable ambient floats |
| Authored `!u!108` Light on a GameObject | `_Light_intensity` / `_Light_color_*` tables |

No invented light *assets*. `AddComponent<Light>()` GetOrAdds a default light
slot (intensity 1, white) into the light table.

## Camera and rendering

| Script / scene uses | Emitted |
|---------------------|---------|
| Authored `!u!20` Camera (MainCamera) | `Camera_main_pos_*` (incl. **z**), `orthographicSize`, near/far clip, background RGB |
| Camera `m_Father` under a packed body | Live: `_engine_sync_camera_main` → `parent_world + local` each tick/draw |
| `Camera.main.orthographicSize` / `.transform.position` / clip planes | Reads those globals |
| Authored `!u!212` SpriteRenderer with `m_Sprite` → **project PNG** | Texture + tinted quad in `engine_collect_draws` |
| Authored `!u!223` Canvas + uGUI Image / Button / TMP | Screen-space quad; UISprite Sliced 9-slice bake; TMP SDF bake |
| `m_SortingLayerID` / `m_SortingOrder` (+ TagManager layers) | Draws sorted back-to-front (layer index, then order) |
| `ProjectSettings` `defaultScreenWidth` / `Height` | `Screen_width` / `Screen_height` (GLFW window size) |
| `ProjectSettings` `fullscreenMode` 0/1 | `Screen_fullScreen` — GLES host uses primary monitor |
| `defaultIsNativeResolution` | `Screen_fullScreenNative` — desktop video mode size when FS |
| Authored `m_LocalRotation` on Transform | XY basis `EngineDraw.m00..m11` (ortho drop Z) |
| `transform.Rotate` (euler / `Vector3.axis * deg`, Space.Self) | Live local quat + `rot_m00..m11` in draws |
| `transform.LookAt` (Transform / `Vector3`, default up) | Live local quat = LookRotation(to−from); refreshes draw basis |
| `transform.eulerAngles` (`=` / `+=`, degrees) | Get/set live quat via Unity Euler; refreshes draw basis |
| `transform.rotation` (`=` `Quaternion.Euler` / `LookRotation` / `Slerp` / `Inverse` / `RotateTowards` / `identity` / `new`) | Set live quat (unparented ≈ world); refreshes draw basis |
| `Quaternion.Angle(a, b)` | Degrees between two rotations (`acos(|dot|)×2` in degrees) |
| `Quaternion.RotateTowards(from, to, maxDegreesDelta)` | Step toward `to` by at most `maxDegreesDelta` degrees |
| `transform.Find` (child name or `"A/B"` path) | Live parent table child lookup → GO index or **-1** |
| `transform.parent` | Live `_engine_go_parent` (seeded `m_Father`) → parent GO index or **-1** |
| `transform.SetParent` (Transform / null, optional `worldPositionStays`) | Live `_engine_go_parent` + xf parent; stays=true keeps world T; appends as last sibling |
| `transform.GetSiblingIndex` | Live `_engine_go_sib` (seeded GO order under parent; updated by `SetParent`) |
| `transform.gameObject` | Same GO index as this Transform (packed Transform ≡ GameObject) |
| `transform.worldToLocalMatrix` / `localToWorldMatrix` | Live TRS → `Matrix4x4` (same affine as TransformPoint) |
| `transform.localScale` | Allowed (CS1061 cleared); live scale tables when SetWorldScale / scale draws / matrices need them |
| `transform.localPosition` | Live packed pos tables (local under parent); Vector3 field round-trip |
| `transform.localRotation` | Live local quat (`_Class_rot_*`); set via Quaternion expr like `rotation` |
| `transform.TransformPoint` | Live local→world: `T + R*(S*p)` using current pos / rot basis / scale |
| Authored `m_Father` / PrefabInstance `m_TransformParent` | World TRS = parent ∘ local; **live** at draw/collider time |

PNG pixels are packed into `data.c` (`engine_texture_rgba`). Editing the
referenced sprite and re-packing changes the drawn texels. Tint comes from
`m_Color` (**including alpha**). GLES hosts multiply
`texture.a × EngineDraw.a` with `GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA`.
World size follows Unity:
`(texels / spritePixelsToUnits) * Transform.scale` (half-extents in
`engine_collect_draws`). `spritePixelsToUnits` is read from the PNG `.meta`
(default **100**). Sprite quads use the local XY→world XY basis from
`m_LocalRotation` (`EngineDraw.m00..m11`; orthographic drop of Z). Pure Z
spin matches the old cos/sin path; X/Y tilt foreshortens the projected
extents. Scripts that call `transform.Rotate`, `transform.LookAt`,
`transform.eulerAngles`, or `transform.rotation` keep a live local
quaternion (`_Class_rot_*`) and refresh that basis each call (`Rotate` =
Space.Self degrees; `LookAt` = LookRotation toward target, default world
up; `eulerAngles` / `rotation = Quaternion.Euler` / `LookRotation` =
absolute orientation).
Child transforms keep
**local** position when parented to another packed body; `engine_collect_draws`
(and collider centers) compose `parent_world + local` each frame so a parent
`Rigidbody2D` / scripted motion carries children (Unity hierarchy). Objects with
their own Rigidbody stay independent. UI under a Canvas uses hierarchical
RectTransform bake (Canvas root = screen).

`engine_collect_draws` sorts by TagManager `m_SortingLayers` index (from
`m_SortingLayerID`), then `m_SortingOrder` — lower draws first (behind).
`EngineDraw.sorting_layer` / `sorting_order` expose the resolved keys.
SystemsScene: BouncePad order **-10**, Stick on **Foreground**.

`Screen_width` / `Screen_height` come from Player Settings
`defaultScreenWidth` / `defaultScreenHeight` (Unity default **1024×768** if
omitted). `fullscreenMode` **0** (Exclusive) / **1** (FullScreenWindow) sets
`Screen_fullScreen`; `gles2_window.c` then creates a GLFW window on the primary
monitor (with `defaultIsNativeResolution`, the desktop video mode size — so the
window is actually fullscreen, not a decorated 1920×1080 frame). Mode **2**
sets `Screen_maximized`; mode **3** / omitted stays windowed. The window title
is `productName`. Scripts may read `Screen.width` / `Screen.height`.

Unity cameras look along **+Z** (identity rotation). `engine_collect_draws`
keeps a sprite only when
`nearClipPlane <= (object_z - Camera_main_pos_z) <= farClipPlane`.
A camera at positive **z** with sprites at **z = 0** therefore draws nothing
(objects are behind the camera). Clip planes default to Unity's **0.3 / 1000**
when the YAML omits them.

**No default visuals.** A GameObject with only a Transform / MonoBehaviour
does **not** appear in `engine_collect_draws`. Hosts clear to the authored
camera background; they do not invent class-hash coloured quads.

`Camera.main` without a scene Camera is a `PackError`.
`AddComponent<Camera>` / `AddComponent<SpriteRenderer>` GetOrAdd into pools
(SpriteRenderer without an authored sprite still does not draw).

## UI

Authored `!u!223` Canvas (Screen Space Overlay / Camera) + uGUI `Image`
or `Button` (with Image) draw via RectTransform size mapped into the
main ortho camera. Nested RectTransforms (e.g. Button → label) use the
parent pixel rect. Authored `VerticalLayoutGroup` /
`HorizontalLayoutGroup` (+ optional `LayoutElement` with min / preferred /
flexible / max) are baked into child `anchoredPosition` / `sizeDelta` / top-left
anchors before that bake (same stacking Unity's layout pass applies).
`ContentSizeFitter` (Unconstrained / MinSize / PreferredSize / Clamped) resizes
the fitter's own RectTransform from those preferred/min sizes (including a
layout group's child totals). `AspectRatioFitter` (WidthControlsHeight /
HeightControlsWidth / FitInParent / EnvelopeParent) adjusts size or stretch
anchors to enforce `m_AspectRatio`. Unity builtin UISprites (`guid` in
`unity_builtin_extra`) bake a rounded white sprite; `Image.type = Sliced`
9-slices with fixed corner borders (Simple stretches). Authored project
PNG UI sprites use `.meta` `spriteBorder` the same way. When
`TextureImporter.spriteMode` is Multiple, each Image/SpriteRenderer
`m_Sprite` fileID selects a `spriteSheet` rect (`internalID`) and only that
crop is packed — otherwise every slice would stretch the whole atlas (e.g.
Settings Menu Full appearing many times on Main Menu). Authored
`Image.preserveAspect` (Simple type) fits the sprite inside the RectTransform
instead of stretching to fill (Unity GenerateSimpleSprite). RectTransform
`localScale` on an object and its ancestors accumulates into baked UI screen
rects (e.g. a VerticalLayoutGroup scaled to 0.59 shrinks children and TMP
like Unity Canvas space). Authored
`TextMeshProUGUI` draws when `m_fontAsset` resolves (Assets or Packages /
PackageCache): SDF atlas + glyph tables bake `m_text` into a UI sprite
tinted by `m_fontColor`. Button `m_OnClick` persistent `SetActive` calls
fire on host pointer press (`engine_pointer_x/y/down`, screen space, origin
bottom-left); inactive parents hide children (`activeInHierarchy`). Authored
`m_IsActive: 0` seeds `_engine_go_active` at load (not forced on).
Stripped `PrefabInstance` roots (e.g. UI Button prefabs) are hydrated from
the source `.prefab` + modifications so TMP children and layout groups see
real RectTransforms instead of full-screen fallbacks. Authored `m_Sprite`
`objectReference` overrides on those instances supply Image sprites when the
prefab default is null. `Awake` runs once before `Start` so
authored `gameObject.SetActive(false)` (e.g. SettingsMenu) hides UI before
the first draw. Canvas sorting layer/order apply to child Images/Buttons;
TMP sorts one order above its Canvas. EventSystem / GraphicRaycaster /
legacy `UI.Text` / `GridLayoutGroup` are not imported.
`AddComponent<Canvas>` / `typeof(Canvas)` / `ForceUpdateCanvases`
remain refused (no invent); `using UnityEngine.UI` and Image fields are fine.

The GLES hosts (`gles2_window.c` / `gles2_view.c`) accept up to 512 draws and
512 textures by default (`MAX_DRAWS` / `MAX_TEX`) so large UI menus are not
silently truncated.

Asset GUIDs resolve under `Assets/`, `Packages/`, and
`Library/PackageCache/` (UPM). Only `Assets/**/*.cs` become packed
MonoBehaviours — package scripts are for reference resolution only.
`Assets/**/Editor/**/*.cs` are skipped (Unity editor-only assemblies).

## Physics (Rigidbody / Rigidbody2D + FixedUpdate)

| Script uses | Emitted |
|-------------|---------|
| Authored `!u!50` Rigidbody2D | Velocity / gravityScale / mass / **linearDamping** tables; Dynamic integrate |
| Authored `!u!54` Rigidbody | 3D velocity + `useGravity` + **drag**; integrates under `Physics.gravity` |
| `Physics2D.gravity` | `Physics2D_gravity_x/y` (default `(0, -9.81)`) |
| `Physics.gravity` | `Physics_gravity_x/y/z` (default `(0, -9.81, 0)`) |
| `GetComponent<Rigidbody2D>().velocity` / `.linearVelocity` / `.gravityScale` / `.linearDamping` | Reads/writes packed RB2D fields |
| `GetComponent<Rigidbody>().velocity` / `.linearVelocity` / `.drag` | Reads/writes packed RB fields |
| Field `Rigidbody2D rb` / `Rigidbody rb` + `.linearVelocity` / `.velocity` | Scene PPtr → packed RB index; `= ….SetX/Y/Z(…)` and `= new Vector2/3(…)` |
| `Time.fixedDeltaTime` | Host-pokeable float (default `1/50`) |
| `FixedUpdate` | Accumulator in `engine_tick`: zero or more steps of `fixedDeltaTime` per frame (Unity), then `engine_physics_fixed` each step |

Linear damping uses Box2D’s factor `clamp(1 − damping · Δt, 0, 1)` on velocity
after gravity (same as Unity Physics2D). Authored `m_LinearDamping` /
`m_LinearDrag` / `m_Drag` on 2D; `m_Drag` on 3D.

Only **authored** Rigidbody components start packed; `AddComponent<Rigidbody>` /
`AddComponent<Rigidbody2D>` GetOrAdds a Dynamic body with Unity defaults
(mass 1, gravity on / gravityScale 1).

## Colliders (BoxCollider2D / CircleCollider2D / BoxCollider / SphereCollider)

| Scene authors | Emitted |
|---------------|---------|
| Authored `!u!61` BoxCollider2D | Size/offset → half-extents; contacts in `engine_physics_collide2d` |
| Authored `!u!58` CircleCollider2D | Radius (× max scale); AABB contacts vs boxes/circles |
| Authored `!u!65` BoxCollider | Size/center → half-extents; contacts in `engine_physics_collide3d` |
| Authored `!u!135` SphereCollider | Radius (× max scale); AABB contacts |
| `m_IsTrigger: 1` | Parsed but skipped for solid resolution |
| Dynamic Rigidbody(2D) + collider | Separates along MTV; friction + bounce from materials |
| Dynamic vs dynamic (vertical) | Lower body treated as support so stacks do not drive through static floors |
| `OnCollisionEnter2D` / `Stay2D` / `Exit2D` | After `engine_physics_collide2d`; `Collision2D` handle; `ToString` → `"UnityEngine.Collision2D"` |

### Physics materials

| Asset / default | Emitted |
|-----------------|---------|
| No `m_Material` (2D) | Friction `0.4`, bounciness `0` (Unity PhysicsMaterial2D defaults) |
| No `m_Material` (3D) | Static + dynamic friction `0.6`, bounciness `0` |
| Authored `.physicsMaterial2D` | Collider then Rigidbody2D material, else defaults |
| Authored `.physicMaterial` | Collider then Rigidbody material, else defaults |
| Combine modes | Average / Multiply / Minimum / Maximum (max of the two modes) |

Static / kinematic colliders (no Dynamic RB) push Dynamic bodies. Transform-only
GOs that carry a collider (e.g. Ground) are packed as position instances.
`AddComponent<*Collider*>` GetOrAdds into a handle pool. Rotation for 2D uses an
AABB of the OBB (authored `m_LocalRotation`). No PolygonCollider2D yet.

## AddComponent

| Script uses | Emitted |
|-------------|---------|
| `gameObject.AddComponent<T>()` / `AddComponent<T>()` | `GameObject_AddComponent_T(go)` — GetOrAdd |
| Builtin `T` (Camera, Light, SpriteRenderer, RB, colliders, AudioSource) | Pre-sized pool; returns existing if already on the GO (AudioSource always adds) |
| Authored MonoBehaviour `T` | Spare instance slots (`n` + budget); mutable GO map |
| `Console.WriteLine(component)` | `T_ToString(index)` → `"name (UnityEngine.T)"` |
| `audio.Play` / `Stop` / `volume` / `loop` / `clip` | Host-observable `_AudioSource_*` tables (clip = opaque index; `null` → `-1`) |

Pool budget is one slot per instance of each class that calls `AddComponent<T>`
(so Update-loop calls reuse the same component). `AddComponent<ParticleSystem>` /
`Canvas` / etc. remain refused.

## Instantiate

| Script uses | Emitted |
|-------------|---------|
| `Instantiate(this)` | `Object_Instantiate_T(i, -1)` — new GO + cloned MB fields |
| `Instantiate(this, parent)` | `Object_Instantiate_T(i, parent_go)` — parent Transform ≡ GO index |
| Typed `T x = Instantiate(this…)` | `int x = Object_Instantiate_T(…)` |

Pool + GO spare budget is one slot per authored instance of each class that
calls `Instantiate(this…)`. Clones copy packed struct fields (and SoA pos),
name `"(Clone)"`, and wire live `_engine_go_T` / parent tables. Prefab /
position / rotation overloads stay unlowered (public helpers that still
contain them are stubbed).

## GetComponentsInChildren

| Script uses | Emitted |
|-------------|---------|
| `GetComponentsInChildren<T>()` / `(true\|false)` | `std::vector<int> = GameObject_GetComponentsInChildren_T(go, incl)` |
| `recv.GetComponentsInChildren<T>()` | Same; `recv` is this / Transform / packed MB → owner GO |
| `arr.Length` | `arr.size()` |
| `Renderer` | Aliased to authored `SpriteRenderer` GO map |

Walks live `_engine_go_parent` (self + descendants). Packed MonoBehaviours and
known builtins (`SpriteRenderer`, RB, uGUI, …) only — unknown `T` → CS0246.

## Refused (would invent assets / components)

| Script uses | Why refused |
|-------------|-------------|
| `ParticleSystem.Emit` / `AddComponent<ParticleSystem>` | Needs a ParticleSystem; packer will not invent a pool |
| `AnimationCurve.Evaluate` | Needs authored curves; packer will not invent keyframes |
| `InputAction` / `Gamepad.current` | Needs Input System assets / runtime |
| `UnityEngine.UI` using / Image·Button·Canvas fields | Allowed (authored wiring) |
| `GetComponent<Canvas\|Image\|RectTransform\|…>` | Live `_engine_go_*` maps (RectTransform ≡ GO); seeded authored |
| `GetComponent<T>` for prefab/scene MBs | Live maps; prefab instances loaded when scene scripts reference `T` |
| `AddComponent<Canvas>` / `typeof(Canvas)` / `ForceUpdateCanvases` | Refused invent — author `!u!223` in the scene |
| `List<T>` | ``std::vector`` (MB/component elems → ``int`` indices); ``Add``/``Count``; cross-class static ``Other.list`` → ``Other_list`` |
| `Dictionary<K,V>` / `SortedList<K,V>` | ``std::map`` (``Add``→``[]=``, ``Clear``/``Count``/indexer); string keys via helper |
| `Vector2` | C ``typedef struct`` + ``Vector2_make``; packed fields stay ``_x``/``_y`` |
| `T.StaticMethod` / `T.Instance` / `FindObjectOfType<T>` | ``T_StaticMethod(args)``; ``T_Instance()`` caches ``Object_FindObjectOfType_T(1)`` (live GO map scan, skips Destroyed; re-finds when null); explicit ``FindObjectOfType<T>()`` → ``Object_FindObjectOfType_T(0)`` |
| `Toggle[]` / ``.isOn`` | ``std::vector<int>`` GO idxs; ``Toggle_set/get_isOn`` |
| `HashSet` / … | BCL collections not lowered — CS0246 at the type token |
| `Camera.main` with no scene Camera | Packer will not invent a default camera |

Pack / crust / cpprust failures report as Unity/csc diagnostics
(`Assets/…(line,col): error CSxxxx: …`), not raw `engine.cpp` subset prose.

## Incremental pack

`pack()` writes `outdir/.unity_pack_stamp.json` with an input fingerprint,
sha256s of `engine.cpp` / `data.cpp` / `main.cpp`, and light per-class rows
(`name`, `n`, `size`, `idx_ty`, optional SoA dims) for the CLI summary on
early exit. Fingerprints split into **assets** (tools, ProjectSettings,
non-`.cs` Assets) and **scripts** (`Assets/**/*.cs`). A later pack of the
same project into the same outdir:

- **Early exit** when the full fingerprint matches and outputs are complete
  (no `load_project` / emit / transpile).
- **Scripts-only** when assets match but scripts differ: reuse
  `.unity_pack_scene_cache` (scenes + guid map), re-analyze `.cs`, then
  emit with per-file transpile skip.
- Otherwise re-emits, then **skips cpprust+crust** for any twin whose on-disk
  `.cpp` is byte-identical (and the lowered `.c` exists). Other outputs use
  write-if-different so mtimes stay put for `gcc`.

Fingerprint covers `tools/unity_pack.py`, `tools/cpprust.py`,
`ProjectSettings/`, and authored `Assets/` extensions (`.cs`, scenes,
prefabs, metas, common textures/audio, etc.). It does **not** walk
`Library/PackageCache` — after a UPM-only change, pass `--force`.

```
python3 tools/unity_pack.py <project> -o /tmp/out
python3 tools/unity_pack.py <project> -o /tmp/out          # stamp hit
python3 tools/unity_pack.py <project> -o /tmp/out --force  # always re-emit
```

`build_player_executable` also skips compiling/linking when `.o` / the exe
are newer than their inputs.

## Tick order

```
Time_time += Time_deltaTime   // if Time.time used
fixed_accum += min(Time_deltaTime, maximumDeltaTime≈1/3)
while fixed_accum >= Time_fixedDeltaTime:
    foreach class: FixedUpdate    // if present
    engine_physics_fixed()        // authored Rigidbody / Rigidbody2D + collide
                                  // then OnCollisionEnter/Stay/Exit2D
    fixed_accum -= Time_fixedDeltaTime
foreach class: Update
```

## Fixture

`examples/unity_pack/SystemsScene` — Bouncer / Ball / Pad (each with
authored SpriteRenderer), HeavyBall + Ground (`Ice.physicsMaterial2D`), Wave
(`Animation` + legacy `Bob.anim`), Spinner (`Animator` + `Bob.controller`,
idle — legacy clip is not Mecanim), Shade
(Light only, no invent-draw), Main Camera:

```
python3 tools/unity_pack.py examples/unity_pack/SystemsScene -o /tmp/sys
make -C /tmp/sys && /tmp/sys/game
PROJECT="$(pwd)/examples/unity_pack/Slime Jump" \
  ./examples/unity_pack/run_gles2_window.sh
```
