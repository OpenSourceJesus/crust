# UNITY_PACK_SYSTEMS — authored Unity systems only

`unity_pack` speeds up what is already in the user's Unity (or Godot)
project. It **does not invent components**: no synthetic ParticleSystem
pools, no default AnimationCurves, no Rigidbody graph, no Canvas, no
InputAction maps, no `AddComponent<Light>`. Scripts that need those Unity
features keep them in the authored project until the packer can import
them; calling invent-requiring APIs today is a hard `PackError`.

What *is* lowered: APIs and methods on the MonoBehaviours / scene
instances that are already placed.

## Input (Input Manager, host-fed)

| Script uses | Emitted |
|-------------|---------|
| `Input.GetAxis("Horizontal"\|"Vertical")` | Host floats `engine_input_axis_*` |
| `Input.GetButton("Jump")` | Host int `engine_input_button_Jump` |
| `Input.GetKey("a")` | Host table `engine_input_key[256]` |

Legacy Input Manager only. New Input System `InputAction` /
`Keyboard.current` / `Gamepad.current` are refused (would invent action
maps / device graphs).

## Animation (script motion)

| Script uses | Emitted |
|-------------|---------|
| `Time.time` | `float Time_time` (advanced in `engine_tick`) |
| `Mathf.Sin` / `Mathf.Cos` | `sinf` / `cosf` wrappers (`-lm`) |
| `transform.position = new Vector2(x, y)` | Direct `set_pos_*` on packed instances |

Bob or lerp positions from `Time` / `Mathf` on objects that exist in the
scene. No Animator, Mecanim, or skeletal skins.

## Particles

Refused. `ParticleSystem.Emit` needs a ParticleSystem in the project;
the packer will not invent a pool.

## Lighting

| Script / scene uses | Emitted |
|---------------------|---------|
| `RenderSettings.ambientLight.{r,g,b}` | Host-pokeable ambient floats |
| Authored `!u!108` Light on a GameObject | `_Light_intensity` / `_Light_color_*` tables |

No invented lights. `AddComponent<Light>` is a `PackError`.

## UI

Refused invent. `UnityEngine.UI` / `Canvas` keep UI in the authored
Unity project until Canvas import lands.

## Physics (2D globals + FixedUpdate)

| Script uses | Emitted |
|-------------|---------|
| `Physics2D.gravity` | `Physics2D_gravity_x/y` (Unity default `(0, -9.81)`) |
| `Time.fixedDeltaTime` | Host-pokeable float (default `1/50`) |
| `FixedUpdate` | Once per `engine_tick`, before `Update` |

Velocity stays ordinary packed float fields on the user's script
(`velX` / `velY`). No colliders, contacts, or invented Rigidbody2D
components.

## Refused (would invent assets / components)

| Script uses | Why refused |
|-------------|-------------|
| `ParticleSystem.Emit` | Needs a ParticleSystem; packer will not invent a pool |
| `AnimationCurve.Evaluate` | Needs authored curves; packer will not invent keyframes |
| `InputAction` / `Keyboard.current` | Needs Input System assets / runtime |
| `UnityEngine.UI` / `Canvas` | Needs authored UI hierarchy |
| `AddComponent<Light>` | Light must already be on a scene GameObject |

## Tick order

```
Time_time += Time_deltaTime   // if Time.time used
foreach class: FixedUpdate    // if present
foreach class: Update
```

## Fixture

`examples/unity_pack/SystemsScene` — Bouncer (Sin + Time), Ball
(FixedUpdate + gravity), Pad (GetAxis), Shade (ambient + authored Light):

```
python3 tools/unity_pack.py examples/unity_pack/SystemsScene -o /tmp/sys
make -C /tmp/sys && /tmp/sys/game
```
