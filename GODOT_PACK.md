# GODOT_PACK — packed engine from a Godot 4 subset

`tools/godot_pack.py` packs a Godot 4 project into the same `engine.c` /
`data.c` a Unity project becomes ([UNITY_PACK.md](UNITY_PACK.md)). It is the
Godot front end of one packer: it produces the object list and script
analyses a Unity project produces, and everything after import — field
widths, bitfields, handles, SoA positions, 2D physics on Box2D-Packed — is
unity_pack's back end, not written twice.

```
python3 tools/godot_pack.py tests/fixtures/GodotMini -o /tmp/gm
/tmp/gm/GodotMini
ready hp=5
x=220
```

`godot_pack.py` takes unity_pack's options (`-o`, `--force`, `--strict`,
`--physics-inject`, `--box2d PATH`, `--coost PATH`); `unity_pack.py` on a directory holding
`project.godot` does the same thing. As with Unity, what is not in the
subset is refused where it is written, never dropped.

## Scenes

`.tscn`, `.tres` and `project.godot` share one reader for Godot's text
resource format (`parse_resource`): section headers and their attributes,
and typed values across lines — `Vector2(..)`, `ExtResource("..")`,
`SubResource("..")`, arrays, dictionaries, `&"StringName"`.

* The scene packed is `application/run/main_scene`, a `res://` path or a
  `uid://` (resolved through scene headers and `.uid` files); without one,
  every scene no other scene instances.
* Instanced scenes are expanded in place, with the overrides the instancing
  scene writes for their root or their children. Every instance of a
  scriptless scene is one class, named for the scene's root: six instances
  of `crate.tscn` are `Crate`, n = 6, however the nodes are named.
* Sub-resources and `.tres` files are resolved where a property uses them.
* Each node is one object. Its global transform is composed from its
  parents (2D, and `Transform3D`; `top_level` respected). Its class is the
  one its C# script declares — the class its file is named for, Godot's own
  rule — or its name when it has none.
* Values the scene sets for the script's `[Export]` members are the
  object's fields. Numbers, bools and strings are packed; a `NodePath`, a
  `Vector2` or a resource is refused at its scene line. A `string` export
  is owned storage the script can assign, and C# string members work on
  it (see *Strings* in [UNITY_PACK.md](UNITY_PACK.md)).

Global positions are baked at import; the parent chain is not yet a runtime
hierarchy, so a script moving a parent does not move its children.

## Scripts: Godot C#, read as the subset

`adapt_csharp` presents a node script to unity_pack's analyzer as the
Unity-shaped C# it already lowers:

| Godot | read as |
|-------|---------|
| `partial class P : Node2D` (any Godot base) | `class P : MonoBehaviour` |
| `_Ready` / `_EnterTree` / `_ExitTree` | `Start` / `Awake` / `OnDestroy` |
| `_Process(double delta)` / `_PhysicsProcess(..)` | `Update()` / `FixedUpdate()` with `float delta` from `Time.deltaTime` / `Time.fixedDeltaTime` |
| `[Export]` field or `{ get; set; }` auto-property | a field |
| `Position` / `GlobalPosition`, `.X` `.Y` `.Z` | `transform.localPosition` / `transform.position`, `.x` `.y` `.z` |
| `Vector2.Up` / `Down` / `Left` / `Right` / `Zero` / `One` | Godot's values: `Up` is `(0, -1)` |
| `GD.Print(a, b)` | `Console.WriteLine("" + a + b)`: Godot concatenates |
| `QueueFree()` | `Destroy(gameObject)` |

The rewrite never moves a line, so a diagnostic about a script names the
Godot file's own line, as `res://`:

```
res://scripts/Player.cs(17,38): error CS8000: `GetNode` (Godot API) is not packed yet
res://coin.tscn:6: error: GDScript is not packed yet (res://scripts/coin.gd); unity_pack reads Godot C# scripts
```

Refused where used: `GetNode` / `GetParent` / `GetTree` / `AddChild`,
signals (`[Signal]`, `EmitSignal`, `Connect`), `Input` and `_Input`,
`Rotation` / `Scale` / `Transform`, `Velocity` / `MoveAndSlide`, `Visible` /
`Modulate`, `PackedScene`, `Godot.Collections`, and `GD` members other than
`Print`. A member the script declares itself (a field named `Scale`) is its
own.

## Physics: Box2D-Packed's Godot mode

A body node takes its `CollisionShape2D` child and its `PhysicsMaterial`
into the body's Rigidbody2D / Collider2D rows, as a Unity GameObject has its
components; the shape node is not an object of its own (nor is a shape
outside a body, which does nothing in Godot).

| Godot | packed |
|-------|--------|
| `RigidBody2D` | dynamic; `freeze` with `freeze_mode` static / kinematic; `mass`, `gravity_scale`, `linear_velocity` |
| `CharacterBody2D`, `AnimatableBody2D` | kinematic, moved by its script |
| `StaticBody2D` | a static collider |
| `Area2D` | a sensor |
| `RectangleShape2D` / `CircleShape2D` | box / circle, offset and rotated as the shape node is, scaled by it |
| `PhysicsMaterial` (`physics_material_override`) | friction, bounce, `rough`, `absorbent` — Godot's defaults without one |
| `linear_damp`, `linear_damp_mode` | combine adds `physics/2d/default_linear_damp`; replace does not |
| `physics/2d/default_gravity` × `_vector` | `Physics2D_gravity` in `data.c`: 980 px/s², down |
| `physics/common/physics_ticks_per_second` | `Time_fixedDeltaTime`: 1/60 |

The world is Godot's — pixels, y down — in the packed tables, the scripts
and Box2D alike. Box2D-Packed's Godot mode
(`box2d_unity.emit_glue(..., mode="godot")`) runs it with Godot's meaning
for each table: `b2SetLengthUnitsPerMeter` scales Box2D's tolerances to
pixels (`[godot_pack] length_units_per_meter` in `project.godot`, 64 by
default); friction combines as `|min(a, b)|` and bounce as
`clamp(a + b, 0, 1)`, a rough material's friction and an absorbent one's
bounce counted negative (Godot's `godot_body_pair_2d.cpp`); and damping is
Godot's `v *= 1 - dt * d` a step, not Box2D's per-substep form. An older
Box2D-Packed checkout without the mode is an error naming the checkout.

Refused at the scene line: other shapes (capsule, segment, polygon,
`CollisionPolygon2D`), a second enabled shape on one body, one-way
collision, `collision_layer` / `collision_mask` other than 1, Area2D gravity
and damping overrides, `constant_linear_velocity`, and 3D bodies. A
`RigidBody2D` that may rotate is a warning: Box2D-Packed locks body rotation,
as for Unity, and `lock_rotation = true` says so.

## Signals

The physics signals are packed: `body_entered` and `body_exited` on a
`RigidBody2D` or an `Area2D`, and `area_entered` and `area_exited` on an
`Area2D`. They are wired either way Godot wires them:

* a scene `[connection]`, from any node to a method of any node with a
  script, including connections inside an instanced scene;
* `BodyEntered += OnBodyEntered;` in a script's `_Ready` or `_EnterTree`
  (`-=` in `_ExitTree` is accepted and does nothing more).

Godot's rules decide what is sent. A `RigidBody2D` reports contacts only
with `contact_monitor` and `max_contacts_reported`; an `Area2D` that is not
`monitoring`, or a body without a shape, sends nothing. Each of those is a
warning, and the connection is not made. An `Area2D` sees bodies (static
ones too) and other areas, as a monitorable area does in Godot.

The engine diffs Box2D-Packed's touching pairs each step, as for Unity's
`OnCollisionEnter2D`, and a generated `_godot_signal` calls each connected
handler with the other body. In the handler, that node is the other
collider's index, and three things are packed on it:

| In the handler | Packed |
|---|---|
| `body is Player`, `body is not Player` | the other object's class, or a Godot type it is (`RigidBody2D`, `PhysicsBody2D`, `Node2D` ...) |
| `body.IsInGroup("players")` | the scene's `groups=[...]` on that node |
| `body.Name` in a `GD.Print` or a string | the node's name |

Any other use of it is refused at its line. A node freed with `QueueFree()`
sends and receives no more signals; its Box2D body stays in the world, so it
still collides until the glue can remove shapes (not yet).

## Checked against real Godot

The scene reader and the physics rules were checked against Godot 4.7.2
itself, headless with `--fixed-fps 60`, running GDScript twins of the same
scenes. For Box2D-Packed's `test/godot/Bounce`: first contacts come on the
same frames, the bounce 0.5 ball rebounds 0.256 of its drop in Godot and
0.243 packed (Godot's `clamp(a + b)`; Unity's average would give 0.0625),
the ball on an absorbent pad 0.041 and 0.036, and a body with `linear_damp`
2 keeps 13.080 px/s after 60 steps in both. Godot calls `_physics_process`
before each step, so a probe there reads one step behind.

## Tests

`TestGodot` in `tests/test_unity_pack.py` packs `tests/fixtures/GodotMini`
and projects it writes itself: the resource reader, transforms and
instancing, the script adapter and its refusals, bodies and shapes, the
physics refusals, signal wiring and handler lowering and their refusals,
and — with a Box2D-Packed checkout — a C# script watching its `RigidBody2D`
fall at 980 px/s² and a pickup scene's signals. Box2D-Packed's own
`test/godot/run_godot_tests.py --crust PATH` checks the physics against
Godot's rules end to end: restitution, absorbent materials, damping, a crate
stack in pixel units, contact and area signals, a freed node's silence, and
the injected build against the standard one.

## Not yet

Sprite2D and textures, Camera2D, input actions, signals other than the
physics ones above, node references, collision layers, a runtime hierarchy,
removing a freed node's body from Box2D, GDScript.
