# UNITY_PACK_SYSTEMS — animation, particles, physics subset

`unity_pack` is not Unity. These three systems are **opt-in stubs**: the
packer emits them only when a script mentions the matching API, keeps
them packed and GLES-friendly, and refuses the rest of the Unity surface.

## Animation

| Script uses | Emitted |
|-------------|---------|
| `Time.time` | `float Time_time` (advanced in `engine_tick`) |
| `Mathf.Sin` / `Mathf.Cos` | `sinf` / `cosf` wrappers (`-lm`) |
| `AnimationCurve.Evaluate(t)` | Linear keyframe sampler + default bounce curve in `data.c` |
| `transform.position = new Vector2(x, y)` | Direct `set_pos_*` |

Typical pattern: bob or lerp a packed position from `Time.time` or a
curve. No Animator state machines, no Mecanim, no skeletal skins.

## Particles

| Script uses | Emitted |
|-------------|---------|
| `ParticleSystem.Emit(x, y)` | Fixed pool (64), spawn at `(x,y)` with a short upward burst |

`ParticleSystem_Tick(dt)` integrates and retires by life.
`engine_collect_draws` appends live particles as small quads after
instance draws. No GPU particles, no modules, no trails.

## Physics (2D)

| Script uses | Emitted |
|-------------|---------|
| `Physics2D.gravity` | `Physics2D_gravity_x/y` (default `(0, -9.81)`) |
| `Time.fixedDeltaTime` | Host-pokeable float (default `1/50`) |
| `FixedUpdate` | Called once per `engine_tick` before `Update` |

Integrate velocity in `FixedUpdate` and write `transform.position` the
same way gameplay scripts already do. No colliders, no contacts, no
Rigidbody2D component graph — velocity is ordinary packed float fields
(`velX` / `velY` or expanded `Vector2`).

## Tick order

```
Time_time += Time_deltaTime
ParticleSystem_Tick(Time_deltaTime)   // if Emit used
foreach class: FixedUpdate            // if present
foreach class: Update
```

## Fixture

`examples/unity_pack/SystemsScene` — one Bouncer (animation), one Ball
(physics), one Emitter (particles). Pack and tick:

```
python3 tools/unity_pack.py examples/unity_pack/SystemsScene -o /tmp/sys
make -C /tmp/sys
```
