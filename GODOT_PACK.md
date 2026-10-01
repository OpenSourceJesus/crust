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

That includes a method the script translator cannot lower yet: in a Godot
project it is an error at its line (`error CS8000`), where a Unity project
still gets a warning and an empty method (see *Stubs are diagnostics* in
[UNITY_PACK.md](UNITY_PACK.md)). A lambda, a tuple or a static call nothing
lowers used to empty its method here too, with only that warning.
`--strict` is the default; `pack(strict=False)` asks for the warning.

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

### Vector2

`Vector2` is the engine's C struct, and its arithmetic is Godot's:
`a + b`, `a - b`, `v * s`, `s * v`, `a * b` (component-wise), `v / s`,
`a / b`, `-v`, `==` / `!=` (exact, as GodotSharp's `Equals`), and `+=` `-=`
`*=` `/=` -- typed and rewritten into the engine's component-wise helpers
(`tools/unity_pack_vectors.py`), whatever the operands: locals, fields,
`Position`, a method's result, `(a - b)`. A node's own `Position` /
`GlobalPosition` is a vector too: read whole, and assigned any vector
expression.

Its methods are GodotSharp's (`Core/Vector2.cs`): `Length`,
`LengthSquared`, `Normalized` (only a zero vector stays zero), `Dot`,
`Cross`, `DistanceTo`, `DistanceSquaredTo`, `Angle`, `AngleTo`,
`DirectionTo`, `Lerp` (not clamped), `MoveToward`, `Rotated`,
`LimitLength` (1 by default), `Abs`, `IsZeroApprox`, `IsEqualApprox`
(Mathf's 1e-6); the constants `Vector2.Zero`, `One`, `Up` (0, -1), `Down`,
`Left`, `Right`. Any other `Vector2.X` is refused where it is written, and
so is a method given the wrong number of arguments.

### Strings

A C# `string` is what it is in a Unity pack (*Strings* in
[UNITY_PACK.md](UNITY_PACK.md)): a local, a field, a parameter or an
`[Export] string` owns its text as a [coost](https://github.com/crustos/coost)
`fastring`, so a pack whose scripts keep a string needs a coost checkout
(`--coost PATH`, `$COOST_ROOT`, or `coost` beside this repository).
Concatenation is typed (`"hp " + hp`), `$"..."` and `string.Format` are
read at pack time, `==` / `!=` compare the text, and the .NET members work
(`Length`, `ToUpper`, `Contains`, `Replace`, `Split`, `string.Join` ...).
And Godot's own:

| Godot | packed |
|-------|--------|
| `GD.Print(a, b, ..)` | its arguments concatenated, and a newline |
| `GD.PrintS(..)` / `GD.PrintT(..)` | separated by a space / a tab |
| `GD.PrintRaw(..)` | no newline |
| `GD.PrintErr(..)` | on stderr |
| `GD.Str(a, b, ..)` | the concatenation, a string |
| `Name` (this node's, or through a reference) | the node's name, a string |
| `StringName` | a string |
| a `[Signal]` with a `string` parameter | passed to the handler, which keeps its own copy |

Other `GD` members are refused where they are written.

### The runtime hierarchy

The parent chain is live. A 2D node under a 2D node object is that node's
child at runtime (unity_pack's transform parents: `xf_id` / `father_id`):
its position is local, its node's `position`, and its world position is
composed each frame from its parent's, through the parent's global rotation
and scale -- `global = parent_global * local`, as Godot's. So a sprite under
a moving node moves with it, scaled and turned as its parent is. Scripts do
not rotate or scale nodes, so each child's parent basis is a constant.

* `Position` is the local position; `GlobalPosition` is composed through
  the parents when read, and assigning it sets the local position that puts
  the node there (the parents' world taken away, the parent's rotation and
  scale inverted) -- on a script's own node and through a reference.
* A `RigidBody2D`, `CharacterBody2D`, `StaticBody2D`, `AnimatableBody2D` or
  `Area2D` has no parent at runtime: it is simulated in the world, as in
  Godot, where a body does not move with its parent. Its children follow it.
* A `top_level` node, and a node under a non-2D `Node`, have none either.

Scripts' `_Process` runs class by class (unity_pack's order), not in tree
order; a parent's move this frame is seen by a child that reads its
`GlobalPosition` after it.

## Sprite2D

A `Sprite2D` is its object's row in the draw list, as a Unity
SpriteRenderer is (`engine_collect_draws`), in Godot's pixels:

| Godot | packed |
|-------|--------|
| `texture`: a PNG `Texture2D`, or an `AtlasTexture` region of one | the texture table, cropped at import |
| `region_enabled` / `region_rect`, `hframes` / `vframes` with `frame` or `frame_coords` | cropped as `Sprite2D::_get_rects` does: the region, then the frame |
| `offset`, `centered` | the draw's centre, a constant offset from the node |
| `flip_h`, `flip_v` | mirrored in place, as Godot's negative rect size is |
| global rotation and scale | the draw's basis and half extents |
| `modulate` of the node and its CanvasItem parents, times its `self_modulate` | the draw's colour |
| `visible` false on it or a parent | not drawn |
| `z_index` (with `z_as_relative`), then tree order | painter's order: a z's rank among those used is the sorting layer, tree order the sorting order |

A script moving the node (`Position`) moves its draw, and a node freed with
`QueueFree()` is no longer drawn. Texture row 0 is the image's bottom in the
packed tables, as for Unity, and the draw's local y is mirrored so the image's
top is up in Godot's y-down pixels.

Refused at the scene line: a texture that is not a PNG (`.svg`, a
`GradientTexture2D` ...), a PNG the packer cannot decode (8-bit RGB / RGBA
are packed), a frame outside its texture or not whole pixels, an
AtlasTexture `margin`, a CanvasItem `material`, `show_behind_parent`,
`y_sort_enabled`, `clip_children`; and drawing nodes not packed yet --
`AnimatedSprite2D`, `Polygon2D`, `TileMap` / `TileMapLayer`,
particles, `MeshInstance2D`, `TextureRect`, `NinePatchRect` -- when they
have something to draw.

## The 2D GPU path (`--gpu-batch`)

```sh
python3 tools/godot_pack.py <project> -o out --gpu-batch
cc -DBATCH -I out -I examples/unity_pack -I examples/unity_pack/include \
   -o out/view examples/unity_pack/gles3_view.c out/engine.c out/data.c \
   -lEGL -lGLESv2 -lm
```

A Godot pack takes unity_pack's GPU path as a Unity one does
(`tools/unity_pack_gpu2d.py`, `examples/unity_pack/gles3_batch.h`): every
Sprite2D's texture -- a sprite-sheet frame, a region, an AtlasTexture, each
cropped at import -- is a rectangle of one atlas, and the frame is one
instanced draw call. Its sprites are the draw list's, in Godot's order
(z_index, then tree order), with Godot's flips, rotation, scale, modulate
and a parent's transform; its camera is the Camera2D's, y down
(`gles3_render.h`). The batch shader's frame is the per-sprite renderer's,
byte for byte.

A script sets a 2D effect -- Flash, Grayscale, HueShift, Dissolve,
Outline -- with the Godot flavour of `SpriteEffects2D`
(`examples/unity_pack/SpriteEffects2D.Godot.cs`, to put in the project so
the editor compiles it):

```csharp
SpriteEffects2D.Set(this, SpriteEffect2D.Flash, 0.8f);
SpriteEffects2D.Set(GetNode<Enemy>("../Enemy"), SpriteEffect2D.Grayscale, 1f);
SpriteEffects2D.Clear(this);
```

The node is `this` or a node reference, and the effect is drawn on its
sprites and its children's -- a Godot node's sprite is most often its child
(a body and its Sprite2D), as its modulate is theirs. Without
`--gpu-batch`, nothing draws the effects.

## Camera2D

The view the draw list is seen through is Godot's: world y points down the
screen (`int Camera_main_y_down = 1` in `data.c`; the example renderers,
`gles2_view.c` / `gles2_window.c` / `gles3_view.c` / `gles3_window.c`, flip
their view for it, and read 0 -- Unity's y up -- from a Unity pack).

* Without a Camera2D, the view is the viewport's rect from the origin,
  `display/window/size/viewport_width` x `_height` (1152 x 648 by default).
* The current Camera2D is the first enabled one in the tree, as Godot makes
  current. Its view is the viewport's size over `zoom`, anchored at the node
  (`anchor_mode` drag centre, or fixed top-left), clamped to the `limit_*`s
  (left, then right; top, then bottom -- `limit_enabled`), then moved by
  `offset`: `Camera2D::get_camera_transform`'s order.
* A camera with a script follows its own node; one without follows its
  parent -- whose world position the runtime hierarchy composes through all
  its ancestors, so it follows whichever of them moves -- keeping its offset
  from it; the limits and offset are applied each frame.
* The clear colour is `rendering/environment/defaults/default_clear_color`;
  the window is `window_width_override` / `_height_override` when set, and
  the view is letterboxed into it, as stretch aspect `keep` does.

Refused at the scene line: position or rotation smoothing, drag margins,
`custom_viewport`, a rotated camera with `ignore_rotation = false`, a zoom
that is not positive. A script cannot yet read or move the camera (`GetNode`
is refused), nor switch cameras.

## Input actions

`Input` reads the InputMap: Godot 4.4's built-in `ui_*` actions (with the
toggle deadzone, 0.5), then `project.godot`'s `[input]` (0.2 when an action
gives none), which overrides them. Each action a script names is a slot of
the engine's tables, evaluated once a tick over the keys and first joypad
the host reports (`engine_keyboard_<key>`, `engine_gamepad_button[]` /
`_axis[]`, as a Unity pack's `Keyboard.current` / `Gamepad.current`):

| Godot | packed |
|-------|--------|
| `IsActionPressed`, `IsActionJustPressed`, `IsActionJustReleased` | the action's state, and its edges since the last tick |
| `GetActionStrength`, `GetActionRawStrength` | the strongest event, as Input's action cache |
| `GetAxis(neg, pos)` | strength(pos) - strength(neg) |
| `GetVector(nx, px, ny, py[, deadzone])` | raw strengths, the four deadzones' mean unless given, a circular deadzone, length at most 1 |
| `IsKeyPressed` / `IsPhysicalKeyPressed` / `IsKeyLabelPressed(Key.X)` | the key |
| `IsJoyButtonPressed(0, JoyButton.X)`, `GetJoyAxis(0, JoyAxis.X)` | the first joypad, y down |

An event is Godot's: a key (its keycode, physical keycode or label; a Shift /
Ctrl / Alt without a location is either side; the modifiers it asks for must
be held, others may be), a joypad button, or a joypad axis
(`InputEventJoypadMotion::action_match`: pressed past the deadzone in its
direction, strength `inverse_lerp(deadzone, 1, |v|)`, raw strength `|v|`).
Keys a host reports: letters, digits, Space, Enter, Escape, Tab, Backspace,
the arrows, Shift, Ctrl, Alt. A built-in's events a host cannot report (KP
Enter, Page Up / Down, Home, End) are not in it.

Refused where written: an action the InputMap does not have, an action name
that is not a string literal, `exact_match`, a key a host does not report,
Meta / Command modifiers, a joypad device other than the first, mouse and
other events, `Input` members not above, and `_Input` / `_UnhandledInput` /
`_UnhandledKeyInput` / `_ShortcutInput` overrides.

Edges are per tick: a press between two ticks is one `IsActionJustPressed`,
and a tick that runs two physics steps shows it to both.

## Scripts: Godot C#, read as the subset

`adapt_csharp` presents a node script to unity_pack's analyzer as the
Unity-shaped C# it already lowers:

| Godot | read as |
|-------|---------|
| `partial class P : Node2D` (any Godot base) | `class P : MonoBehaviour` |
| `_Ready` / `_EnterTree` / `_ExitTree` | `Start` / `Awake` / `OnDestroy` |
| `_Process(double delta)` / `_PhysicsProcess(..)` | `Update()` / `FixedUpdate()` with `float delta` from `Time.deltaTime` / `Time.fixedDeltaTime` |
| `[Export]` field or `{ get; set; }` auto-property | a field |
| `Position` / `GlobalPosition`, `.X` `.Y` `.Z` | `transform.localPosition` / `transform.position` (local / composed through the parents; see *The runtime hierarchy*), `.x` `.y` `.z` |
| `Vector2.Up` / `Down` / `Left` / `Right` / `Zero` / `One` | Godot's values: `Up` is `(0, -1)` |
| `GD.Print(a, b)` | `Console.WriteLine("" + a + b)`: Godot concatenates |
| `QueueFree()` | `Destroy(gameObject)` |

The rewrite never moves a line, so a diagnostic about a script names the
Godot file's own line, as `res://`:

```
res://scripts/Player.cs(17,38): error CS8000: `GetNode` (Godot API) is not packed yet
res://coin.tscn:6: error: GDScript is not packed yet (res://scripts/coin.gd); unity_pack reads Godot C# scripts
```

Refused where used: `GetTree` (but for `GetTree().Root.AddChild`) / `GetChild`,
`Connect`, `Input` beyond the actions above, `_Input`,
`Rotation` / `Scale` / `Transform`, `Velocity` / `MoveAndSlide`, `Visible` /
`Modulate`, `ResourceLoader` but for a scene's load, `Godot.Collections`, and `GD`
members other than the print family, `Str` and `Load<PackedScene>`. A member the script declares itself (a field named `Scale`) is its
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

`collision_layer` and `collision_mask` are Godot's, 32 bits
(`godot_body_pair_2d.cpp`, `Area2D::collides_with`): a dynamic body is pushed
by what its mask has the layer of, and an Area2D reports what its mask has
the layer of (between two areas, each by its own mask). Box2D-Packed's Godot
mode filters the body pairs (`_with_godot_layers`: a pair collides when a
dynamic side's mask has the other's layer), and godot_pack's signal
dispatch the areas'. A Box2D contact pushes both of its bodies, so two
dynamic bodies that Godot pushes one way only -- one's mask has the other's
layer, not the reverse -- are pushed both ways: a warning at the scene line.

Refused at the scene line: other shapes (capsule, segment, polygon,
`CollisionPolygon2D`), a second enabled shape on one body, one-way
collision, Area2D gravity
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
sends and receives no more signals, and its body leaves the world: Box2D-Packed's
live gate disables the bodies of a freed node (`engine_rb2d_live` /
`engine_col2d_live`), and drops their touching pairs, from the next step --
what rested on it falls. `QueueFree()` frees the node's subtree, as
Godot's: its children first, in reverse order (their `_ExitTree` before
its), and their bodies leave the world with it.

## Timers and a script's own signals

Besides the physics signals, a `Timer`'s `timeout` and a script's own
`[Signal]`s are packed, wired either way the physics ones are (a scene
`[connection]`, or `X += Handler;` in `_Ready` / `_EnterTree` on the
script's own signal, or a Timer script's `Timeout`):

```csharp
[Signal] public delegate void HitEventHandler(int damage, float at, bool crit);
...
EmitSignal(SignalName.Hit, 3, 1.5f, true);   // or EmitSignal("Hit", ..)
EmitSignalHit(7, 2.5f, false);               // the generated form
```

Each emission calls its connections at once, in connection order, with its
arguments; a freed sender or receiver is skipped. A signal's parameters are
`int`, `float`, `bool` or `string`, and a handler takes the signal's types. A
`Timer` (a node, or one a script derives from) is Godot's: `wait_time`,
`one_shot`, `autostart`; each frame `time_left -= delta`, and below 0 it
sends `timeout` and starts over from what is left (`+= wait_time`), or stops
when one-shot. Timers tick after the scripts' `_Process` in a frame -- Godot
ticks one in tree order with the `_process` calls, and it is most often a
child or later sibling of the script it serves.

Refused where written: another parameter type (a node, a vector ..), `EmitSignal` of a signal the
script does not declare or with the wrong number of arguments, a signal
name that is not `SignalName.X` or a literal, a handler whose parameters are
not the signal's, a deferred connection, a physics-process Timer,
`ignore_time_scale`, a Timer script's own `Start` / `Stop` / `WaitTime` /
`OneShot` / `TimeLeft` / `Paused` / `IsStopped`, and other signals (Button
`pressed`, `tree_exited` ...).

## Node references

`GetNode<T>("path")`, `GetNodeOrNull<T>(..)` and `GetParent<T>()` are
resolved when the project is packed, for each node that runs the script --
two instances of `enemy.tscn` each reach their own `Cooldown` -- as
Node::get_node resolves them: relative (`Child/Grand`, `..`), absolute
(`/root/Main/Player`), or a scene unique name (`%Boss`). Each call is a
field of the script's class that the scene seeds, so the usual caching
(`_player = GetNode<Player>("../Player");` in `_Ready`) copies it, and a use
anywhere reads it. An `[Export]` node field the scene sets
(`Target = NodePath("../Player")`) is resolved the same way. The path is a
string literal; a `GetNodeOrNull` that finds nothing is null.

What the reference is follows T:

| T | the reference | through it |
|---|---|---|
| the target's script class (`Player`) | that node's object | its methods and fields; `Position` / `GlobalPosition` (a vector or `.X` / `.Y`), read and assigned (`=`, `+=`, `-=`, `*=` / `/=` by a number); `Name` |
| a Godot type the target is (`Node2D`, `Sprite2D`, `RigidBody2D` ...) | the node's packed class: its script's, or without one the node's own | the same; a variable given it takes that class |
| `Timer` | its timer | `Start()` / `Start(t)`, `Stop()`, `IsStopped()`, `TimeLeft`, `WaitTime` / `OneShot` / `Paused` (read and set) -- Godot's Timer: start sets `time_left = wait_time`, stop `-1` |

A Timer script has the Timer members on itself too, and a script's own
`Name` is its node's (renaming is not packed).

Refused where written: `GetNode` without a type, a path that is not a
literal, a path that finds no node (the message names the node the path is
from), a target that is not T, a Godot-typed reference that reaches nodes of
different classes from the nodes running the script, another member
through a reference (`_p.Rotation`), a component of another node's position
assigned (`_p.Position.X = 3`, C#'s CS1612), a Timer's `Timeout +=` on a
reference (connect it in the scene), `WaitTime +=`. A reference to a node
that is freed still names it.

## Spawning: PackedScene.Instantiate and AddChild

```csharp
[Export] public PackedScene BulletScene;            // the scene sets it
...
Bullet b = BulletScene.Instantiate<Bullet>();       // or (Bullet)s.Instantiate(),
b.Position = new Vector2(0, 8);                     //    s.Instantiate() as Bullet
AddChild(b);
```

Every scene a script can spawn -- an `[Export] PackedScene` the scene sets,
a literal `GD.Load<PackedScene>("res://..")` / `ResourceLoader.Load` (in a
field's initializer, a local, or inline:
`GD.Load<PackedScene>("res://b.tscn").Instantiate<Bullet>()`) -- is packed
as a template: its nodes, dormant from the start (not processed, drawn or
simulated). Scenes the templates spawn are templates too. A spawn is the
subset's `Instantiate` (*Instance caps* in UNITY_PACK.md): a clone of the
template's root into a free row of its class, its fields as the template
sets them, `_EnterTree` then, `_Ready` at the next frame, before its first
`_Process` -- a node added during a frame is processed from the next, as in
Godot. It is drawn as its template is, and a clone's `Name` is its
template node's.

The whole scene is spawned: each of its nodes is cloned, under the clone of
its parent, positioned and drawn as the scene has it (a sprite child, a
grandchild ..), and a reference from one of its scripts to a node of its
own scene (`GetNode<Sprite2D>("Look")`) is that node's clone. If a node's
class is full, the nodes already cloned are freed and `Instantiate`
returns null. The scene tree is live: a spawned node is its parent's child
-- `QueueFree` on the parent frees it too -- and a freed node's row, when
the next spawn takes it, leaves its old parent.

A spawned scene's root may be a physics body -- a `RigidBody2D`,
`StaticBody2D` or `Area2D` and its CollisionShape2Ds. A body's rows (its
collider, its Rigidbody2D, its Box2D body) are fixed when the project is
packed, so its class has a pool of dormant copies, as many as may be live
at once (`[MaxInstances(N)]`, else 64), each with its own body, which
Box2D-Packed's live gate keeps disabled until a spawn takes it: a spawn
takes a freed copy, its body enabled at the clone's position with the
template's velocity, never the last life's. `AddChild` makes a body's
position global -- it is simulated in the world, as in Godot, where a body
does not follow its parent -- and its children follow it. A placed Area2D
sees spawned bodies and areas, and a spawned one is seen: their
signals are the placed nodes' as ever. A spawned body's own physics
signals -- its script's `BodyEntered += OnHit`, or its scene connecting it
to itself -- are each pool copy's own.

A spawned scene's Timers are its own: pooled as a body is (their rows are
the timers' table), each spawn's Timer as the scene sets it -- `autostart`,
`wait_time`, `one_shot` -- and processed from the frame after it is added,
as Godot processes a node added during a frame. Its connections -- a
Timer's `timeout`, a script's `[Signal]`, from one of its nodes to another
-- are the spawn's own: a table per connection, filled when it is spawned,
read by the signal's dispatch. A reference from one of its scripts to its
own Timer (`GetNode<Timer>("Fuse")`) is the spawn's Timer; an absolute path
(`GetNode<Score>("/root/Main/Score")`) is the main scene's node.

`AddChild(b)` puts it under this node, `ref.AddChild(b)` under a node
reference, `GetParent().AddChild(b)` under this node's parent, and
`GetTree().Root.AddChild(b)` / `GetTree().CurrentScene.AddChild(b)` at the
top: its `Position` is local to its parent from then on, in the parent's
global rotation and scale -- and its scene's nodes in theirs -- through the
runtime hierarchy.

A spawned class keeps at most `[MaxInstances(N)]` live (the project's own
attribute, as in a Unity pack), else 64 beside its template; a freed
clone's row is the next spawn's. When they are all live, the spawn clips:
`Instantiate` returns null, as a Unity pack's does.

Refused where written: a scene spawned other than into a local (`T b =
s.Instantiate<T>()`, then `AddChild(b)`), a local of another type than the
scene's root, an `AddChild` of something else or with more arguments, a
scene set to no scene on the nodes running the script, a scene that
spawns itself, a relative path above a spawned scene's root (`..` from
it: its parent is known only when it is added -- an absolute path is
packed). Refused at the scene's line, until they are packed: a physics body
below a spawned scene's root, and a spawned body's physics signal to
another node than itself. A `SpriteEffects2D` effect set on a spawned node reaches
its own sprite, not yet its spawned children's.

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

`tools/godot_pack_test_fast.py` (`make test_godot_fast`)
covers the newer front end: Sprite2D rows, refusals and a packed draw list;
Camera2D views, limits and following, and a y-down frame through
`gles2_view.c` when EGL is there; input actions against Godot's rules; the
reader's inline objects. It skips what is not about Godot -- the emitted C is
not re-validated through cpprust + crust, and is compiled at -O0 -- so new
Godot tests belong there. `TestGodot` in `tests/test_unity_pack.py` packs `tests/fixtures/GodotMini`
and projects it writes itself: the resource reader, transforms and
instancing, the script adapter and its refusals, bodies and shapes, the
physics refusals, signal wiring and handler lowering and their refusals,
and — with a Box2D-Packed checkout — a C# script watching its `RigidBody2D`
fall at 980 px/s² and a pickup scene's signals. Box2D-Packed's own
`test/godot/run_godot_tests.py --crust PATH` checks the physics against
Godot's rules end to end: restitution, absorbent materials, damping, a crate
stack in pixel units, contact and area signals, a freed node's silence, and
the injected build against the standard one.

## Lines and curves

A **Line2D** is drawn as a Unity LineRenderer is (see *LineRenderer* in
[UNITY_PACK.md](UNITY_PACK.md)): one quad a segment, as wide as `width`
times `width_curve` at the segment's midpoint and in `gradient`'s color there
(or `default_color`), both sampled at the point's fraction of the line's
length, as Godot's LineBuilder samples them; `closed` adds the closing
segment, `z_index` sorts it, `visible` hides it. Its points are local to the
node: the node's rotation and scale are baked in at pack time and its
position is followed as it moves. Godot's joints, caps, texture and
antialiasing are not drawn -- a line is a chain of straight bands.

A **Curve** -- a Line2D's `width_curve`, or a script's `[Export] public Curve
Ramp;` -- goes into the same table as Unity's AnimationCurve. Curve::sample is
the cubic Bezier through the points with control points a third of the way
along at the stored tangents, which is exactly Unity's Hermite with
`in = left_tangent`, `out = right_tangent`; it clamps outside the points.
Scripts: `Ramp.Sample(x)`, `Ramp.PointCount`.

Refused at the scene line: a Line2D under a non-uniform scale (its width
would stretch), more than 256 points, and a gradient that interpolates
cubically or in another color space. A gradient's Constant mode is Godot's --
the last color at or before the offset, not Unity's Fixed.

## Not yet

Line2D and Curve from a script (`AddPoint`, `SetPointPosition`,
`SampleBaked`), Camera2D smoothing and drag margins, mouse input, signals other than
the physics ones, timeouts and the scripts' own, process order by tree,
a body below a spawned scene's root, a spawned body's physics signals to
another node, GDScript.
