# UNITY_PACK_SYSTEMS — authored Unity systems only

`unity_pack` speeds up what is already in the user's Unity (or Godot)
project. It **does not invent assets**: no synthetic ParticleSystem
pools, no default AnimationCurves, no scripted UI invent, no InputAction maps.
Runtime `AddComponent<T>()` works for packed builtins (Camera, Light,
SpriteRenderer, Rigidbody/2D, Box/Circle/Sphere colliders, Animation,
Animator) and for authored MonoBehaviours — GetOrAdd into a pre-sized pool
(one spare slot per calling instance). Authored scene Canvas + Image/Button/TextMeshProUGUI
are drawn; scripted `UnityEngine.UI` stays refused. Scripts that need
other Unity features keep them in the authored project until the packer can
import them; calling
invent-requiring APIs today is a hard `PackError`.

Emitted `engine.cpp` / `data.cpp` / `main.cpp` (C++-subset twins) are
gated through `cpprust.translate`, then the translated C is compiled with
crust/`shivyc`. Leaving that subset or failing crust compile is a
`PackError` on the generated file. Host `Makefile` still uses `gcc`;
`make crust-check` recompiles the `.c` files with crust.

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
| `Application.dataPath` | Packed project `Assets/` absolute path |
| `Application.persistentDataPath` | Unity company/product save dir (same layout as Player.log’s parent) |
| Other `Application.*` | Pack-time **CS0117** (`Application` in scope via `using UnityEngine`) |
| `File.WriteAllText(path, text)` | `fopen` write (`"w"`); creates parent dirs when possible |
| `File.AppendAllText(path, text)` | `fopen` append (`"a"`); creates parent dirs when possible |
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
`Start` runs once before first `Update`.

## GameObject lookup

| Script uses | Emitted |
|-------------|---------|
| `GameObject.Find(name)` | Runtime `strcmp` on authored GO name table → index or **-1** |
| `Object.ToString` (via printing a Find result) | `name (UnityEngine.GameObject)`; missing → `"null"` |
| `.GetComponent<T>()` | Instance index of authored `T` on that GO, or **-1** |
| `Find(...).GetComponent<T>().field` | Runtime Find + GetComponent; missing → default `0` / `0.f` |

Parsed with cpprust `_match_paren` / `_match_angle` (same AST helpers
csrust uses). Find **does not** fail at pack time for unknown names —
lookup is runtime only (Unity null). `GetComponent<T>` still requires `T`
to be an authored packed MonoBehaviour (no invented component types).

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
| Authored `!u!223` Canvas + uGUI Image / Button / TMP | Screen-space quad; builtin UISprite → white tint; TMP SDF bake |
| `m_SortingLayerID` / `m_SortingOrder` (+ TagManager layers) | Draws sorted back-to-front (layer index, then order) |
| `ProjectSettings` `defaultScreenWidth` / `Height` | `Screen_width` / `Screen_height` (GLFW window size) |
| `ProjectSettings` `fullscreenMode` 0/1 | `Screen_fullScreen` — GLES host uses primary monitor |
| `defaultIsNativeResolution` | `Screen_fullScreenNative` — desktop video mode size when FS |
| Authored `m_LocalRotation` on Transform | Z spin via `EngineDraw.cos_z` / `sin_z` (identity if omitted) |
| Authored `m_Father` / PrefabInstance `m_TransformParent` | World TRS = parent ∘ local; **live** at draw/collider time |

PNG pixels are packed into `data.c` (`engine_texture_rgba`). Editing the
referenced sprite and re-packing changes the drawn texels. Tint comes from
`m_Color` (**including alpha**). GLES hosts multiply
`texture.a × EngineDraw.a` with `GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA`.
World size follows Unity:
`(texels / spritePixelsToUnits) * Transform.scale` (half-extents in
`engine_collect_draws`). `spritePixelsToUnits` is read from the PNG `.meta`
(default **100**). Sprite quads are rotated in the XY plane from
`m_LocalRotation` (quaternion → angle of local +X). Child transforms keep
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
parent pixel rect. Unity builtin UISprites (`guid` in
`unity_builtin_extra`) become a 1×1 white texel tinted by `m_Color`.
Authored `TextMeshProUGUI` draws when `m_fontAsset` resolves (Assets or
Packages / PackageCache): SDF atlas + glyph tables bake `m_text` into a
UI sprite tinted by `m_fontColor`. Button `m_OnClick` persistent
`SetActive` calls fire on host pointer press (`engine_pointer_x/y/down`,
screen space, origin bottom-left); inactive parents hide children
(`activeInHierarchy`). Canvas sorting layer/order apply to child
Images/Buttons; TMP sorts one order above its Canvas. EventSystem /
GraphicRaycaster / legacy `UI.Text` are not imported. Scripted
`UnityEngine.UI` / `AddComponent<Canvas>` remain refused (no invent).

Asset GUIDs resolve under `Assets/`, `Packages/`, and
`Library/PackageCache/` (UPM). Only `Assets/**/*.cs` become packed
MonoBehaviours — package scripts are for reference resolution only.

## Physics (Rigidbody / Rigidbody2D + FixedUpdate)

| Script uses | Emitted |
|-------------|---------|
| Authored `!u!50` Rigidbody2D | Velocity / gravityScale / mass / **linearDamping** tables; Dynamic integrate |
| Authored `!u!54` Rigidbody | 3D velocity + `useGravity` + **drag**; integrates under `Physics.gravity` |
| `Physics2D.gravity` | `Physics2D_gravity_x/y` (default `(0, -9.81)`) |
| `Physics.gravity` | `Physics_gravity_x/y/z` (default `(0, -9.81, 0)`) |
| `GetComponent<Rigidbody2D>().velocity` / `.gravityScale` / `.linearDamping` | Reads/writes packed RB2D fields |
| `GetComponent<Rigidbody>().velocity` / `.drag` | Reads/writes packed RB fields |
| `Time.fixedDeltaTime` | Host-pokeable float (default `1/50`) |
| `FixedUpdate` | Once per `engine_tick`, then `engine_physics_fixed` |

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
| Builtin `T` (Camera, Light, SpriteRenderer, RB, colliders) | Pre-sized pool; returns existing if already on the GO |
| Authored MonoBehaviour `T` | Spare instance slots (`n` + budget); mutable GO map |
| `Console.WriteLine(component)` | `T_ToString(index)` → `"name (UnityEngine.T)"` |

Pool budget is one slot per instance of each class that calls `AddComponent<T>`
(so Update-loop calls reuse the same component). `AddComponent<ParticleSystem>` /
`Canvas` / etc. remain refused.

## Refused (would invent assets / components)

| Script uses | Why refused |
|-------------|-------------|
| `ParticleSystem.Emit` / `AddComponent<ParticleSystem>` | Needs a ParticleSystem; packer will not invent a pool |
| `AnimationCurve.Evaluate` | Needs authored curves; packer will not invent keyframes |
| `InputAction` / `Gamepad.current` | Needs Input System assets / runtime |
| `UnityEngine.UI` / `AddComponent<Canvas>` | Author Canvas+Image in the scene; no script invent |
| `Camera.main` with no scene Camera | Packer will not invent a default camera |

## Tick order

```
Time_time += Time_deltaTime   // if Time.time used
foreach class: FixedUpdate    // if present
engine_physics_fixed()        // authored Rigidbody / Rigidbody2D + collide
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
SCENE="$(pwd)/examples/unity_pack/SystemsScene" \
  ./examples/unity_pack/run_gles2_window.sh
```
