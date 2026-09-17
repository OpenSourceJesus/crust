# UNITY_PACK_SYSTEMS — animation and physics on *authored* objects

`unity_pack` speeds up what is already in the user's Unity (or Godot)
project. It **does not invent components**: no synthetic ParticleSystem
pools, no default AnimationCurves, no Rigidbody graph the scene never
had. Scripts that need those Unity features keep them in the authored
project until the packer can import them; calling them today is a hard
`PackError`.

What *is* lowered: APIs and methods on the MonoBehaviours / scene
instances that are already placed.

## Animation (script motion)

| Script uses | Emitted |
|-------------|---------|
| `Time.time` | `float Time_time` (advanced in `engine_tick`) |
| `Mathf.Sin` / `Mathf.Cos` | `sinf` / `cosf` wrappers (`-lm`) |
| `transform.position = new Vector2(x, y)` | Direct `set_pos_*` on packed instances |

Bob or lerp positions from `Time` / `Mathf` on objects that exist in the
scene. No Animator, Mecanim, or skeletal skins.

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
| `ParticleSystem.Emit` | Needs a ParticleSystem in the project; packer will not invent a pool |
| `AnimationCurve.Evaluate` | Needs authored curve assets; packer will not invent keyframes |

## Tick order

```
Time_time += Time_deltaTime   // if Time.time used
foreach class: FixedUpdate    // if present
foreach class: Update
```

## Fixture

`examples/unity_pack/SystemsScene` — Bouncer (`Mathf.Sin` + `Time.time`)
and Ball (`FixedUpdate` + gravity), both scene-authored MonoBehaviours:

```
python3 tools/unity_pack.py examples/unity_pack/SystemsScene -o /tmp/sys
make -C /tmp/sys && /tmp/sys/game
```
