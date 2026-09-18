# UNITY_PACK_SYSTEMS — authored Unity systems only

`unity_pack` speeds up what is already in the user's Unity (or Godot)
project. It **does not invent assets**: no synthetic ParticleSystem
pools, no default AnimationCurves, no Canvas, no InputAction maps.
Runtime `AddComponent<T>()` works for packed builtins (Camera, Light,
SpriteRenderer, Rigidbody/2D, Box/Circle/Sphere colliders, Animation,
Animator) and for authored MonoBehaviours — GetOrAdd into a pre-sized pool
(one spare slot per calling instance). Scripts that need other Unity features
keep them in the authored project until the packer can import them; calling
invent-requiring APIs today is a hard `PackError`.

Emitted `engine.c` / `data.c` / `main.c` are also gated through
`cpprust.translate` (same subset check as `csrust`'s C++ half). Leaving
that subset is a `PackError` on the generated file.

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
`Application.consoleLogPath`. `Start` runs once before first `Update`.

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
| Authored `!u!111` Animation + **legacy** `.anim` | Plays root position curves (`engine_animation_tick`) |
| Authored `!u!95` Animator + `.controller` | Default state motion **only if the clip is non-legacy** |
| `m_Legacy: 1` on `.anim` | Legacy → `Animation` only; Mecanim → `Animator` only (Unity) |
| `m_PlayAutomatically` / Animator default | Starts playing; loops when `m_LoopTime` / WrapMode Loop |

Root position keys write absolute `localPosition` from the clip (Bob.anim
x=2 places Spinner/Wave at x=2 while y bobs). Child-path curves, blend trees,
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
| `Camera.main.orthographicSize` / `.transform.position` / clip planes | Reads those globals |
| Authored `!u!212` SpriteRenderer with `m_Sprite` → **project PNG** | Texture + tinted quad in `engine_collect_draws` |
| `m_SortingLayerID` / `m_SortingOrder` (+ TagManager layers) | Draws sorted back-to-front (layer index, then order) |
| Authored `m_LocalRotation` on Transform | Z spin via `EngineDraw.cos_z` / `sin_z` (identity if omitted) |
| Authored `m_Father` / PrefabInstance `m_TransformParent` | World TRS = parent ∘ local (baked into packed `pos` / sprite spin) |

PNG pixels are packed into `data.c` (`engine_texture_rgba`). Editing the
referenced sprite and re-packing changes the drawn texels. Tint comes from
`m_Color`. World size follows Unity:
`(texels / spritePixelsToUnits) * Transform.scale` (half-extents in
`engine_collect_draws`). `spritePixelsToUnits` is read from the PNG `.meta`
(default **100**). Sprite quads are rotated in the XY plane from
`m_LocalRotation` (quaternion → angle of local +X). Child transforms use
**world** position/rotation/scale after composing the `m_Father` chain
(PrefabInstance `m_TransformParent` is applied to stripped instance
transforms). Runtime parent motion is not re-linked yet — hierarchy is
baked at pack time.

`engine_collect_draws` sorts by TagManager `m_SortingLayers` index (from
`m_SortingLayerID`), then `m_SortingOrder` — lower draws first (behind).
`EngineDraw.sorting_layer` / `sorting_order` expose the resolved keys.
SystemsScene: BouncePad order **-10**, Stick on **Foreground**.

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

Refused invent. `UnityEngine.UI` / `Canvas` keep UI in the authored
Unity project until Canvas import lands.

## Physics (Rigidbody / Rigidbody2D + FixedUpdate)

| Script uses | Emitted |
|-------------|---------|
| Authored `!u!50` Rigidbody2D | Velocity / gravityScale / mass tables; Dynamic bodies integrate |
| Authored `!u!54` Rigidbody | 3D velocity + `useGravity`; integrates under `Physics.gravity` |
| `Physics2D.gravity` | `Physics2D_gravity_x/y` (default `(0, -9.81)`) |
| `Physics.gravity` | `Physics_gravity_x/y/z` (default `(0, -9.81, 0)`) |
| `GetComponent<Rigidbody2D>().velocity` / `.gravityScale` | Reads/writes packed RB2D fields |
| `GetComponent<Rigidbody>().velocity` | Reads/writes packed RB fields |
| `Time.fixedDeltaTime` | Host-pokeable float (default `1/50`) |
| `FixedUpdate` | Once per `engine_tick`, then `engine_physics_fixed` |

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
| `UnityEngine.UI` / `Canvas` | Needs authored UI hierarchy |
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
