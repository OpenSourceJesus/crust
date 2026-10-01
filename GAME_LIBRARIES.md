# Game libraries: a standard base for the packers

unity_pack and godot_pack turn a Unity or Godot project into C. Today they do
it by *generating an engine*: every Unity or Godot API a project uses is
emulated by C that the packer writes for that project -- the transform tables,
the GameObject tables, physics glue, input, UI, strings, coroutines. That code
lives in the packers as Python that prints C, and there is a lot of it: about
46,000 lines across `tools/unity_pack*.py` and `tools/godot_pack.py`
(`unity_pack.py` alone is about 27,000). Every new API is more of it, and each
one is written twice -- once as the C it should become, once as the Python that
prints that C for each shape of project.

This document is the plan to change that: a **standard base of libraries**,
written in C#, ported to crust's C# subset, that the packers *target* instead
of re-creating. And because crust sees the whole program -- every line of the
libraries, every user script, every scene and asset -- it can optimise across
all of it, in ways an engine that loads arbitrary code at runtime cannot.

Status markers below: **exists** (in crust or a fork today), **in progress**
(started, with tests), **planned**.

## The libraries

Each is a fork under [crustos](https://github.com/crustos), changed only in
ways that keep it plain C# that Unity compiles as before: its README has a
"Crust port" section with the conventions, status and plan.

| library | what it gives a game | status |
|---|---|---|
| [Box2D-Packed](https://github.com/crustos/box2d) | 2D physics: bodies, shapes, joints, contacts -- already the backend of unity_pack's and godot_pack's Rigidbody2D | **exists** |
| [coost](https://github.com/crustos/coost) | the runtime underneath: strings (`fastring`), JSON, files, TCP / HTTP | **exists** |
| [Futile](https://github.com/crustos/Futile) | a code-centric 2D scene graph and batched sprite renderer: nodes, containers, sprites, labels, atlases, touch | **planned** (3 of 66 scripts translate) |
| [Unity-2D-Destruction](https://github.com/crustos/Unity-2D-Destruction) | fracturing sprites into physics fragments (Delaunay / Voronoi, Clipper) | **in progress** (9 runtime files translate; tested in `tools/unity_pack_test_fast.py`) |
| [DTerrain](https://github.com/crustos/DTerrain) | destructible, paintable bitmap terrain with colliders | **planned** (8 of 28 scripts translate) |

Together they cover the things small 2D games are made of: drawing, a scene
graph, physics, terrain, destruction, text, assets, the network.

## What changes for the packers

Today a packer is a compiler *and* an engine author. With a base of libraries
it becomes mostly a compiler:

```
  user project (scenes, prefabs, scripts, assets)
        |
  unity_pack / godot_pack:   read scenes and assets; lower the user's scripts;
        |                    generate the *glue* -- which library objects a
        |                    scene creates, with which data
        v
  base libraries (C# subset -> C):  Futile, DTerrain, Unity-2D-Destruction, ..
        |
  runtime (C):  Box2D-Packed, coost, the GLES2 / GLES3 / wasm hosts
        v
  a C compiler
```

* **Less generated code.** A sprite is a library sprite, not a row in a
  generated table with generated accessors. A collider is a Box2D-Packed shape.
  A label is a library label. The packer emits *data* -- this scene has these
  sprites with these positions and textures -- and calls into code that is
  written once, in C#, readable and testable on its own.
* **The libraries are translated, not hand-written.** Their C comes from crust's
  C# subset, the same path as any C# code. Fixing a library bug means fixing
  C#; improving the translator improves every library.
* **The subset grows from real code, not a wish list.** Each library is a
  corpus that tells crust what to lower next: the forks have already driven
  arena classes (`[MaxInstances(N)]`), static initialisers, `ref` / `out`,
  `#if !CRUST`, and fixes to `List` lowering -- each checked against Mono.
* **The libraries meet the subset half way.** A fork may change its source
  where the change is small, keeps Unity's behaviour, and keeps it plain C#:
  `[MaxInstances(N)]` on graph classes, debug text behind `#if !CRUST`,
  `List.FindAll(delegate ..)` written as the loop it stands for. Where a change
  would make the library worse for Unity users, crust grows instead.

The packers keep what only they can do: reading `.unity` / `.tscn` scenes,
prefabs and assets; lowering the user's own scripts; and refusing, with a C#
diagnostic at the line, what cannot be packed. What shrinks is the engine they
generate around those scripts.

## Why C, and why it is faster

The C that comes out is not interpreted, garbage-collected or loaded at
runtime:

* **No GC.** Objects have one owner and die at scope exit, or come from an arena
  released in bulk (`[MaxInstances(N)]`): no collector pauses, no write
  barriers, no allocation in a frame unless the program asks for it.
* **Static sizes.** A scene's object counts are known when the game is packed,
  and `[MaxInstances(N)]` bounds the rest: tables are arrays sized once, not
  lists that grow.
* **Layouts chosen for the data.** Value types are values; unity_pack already
  packs an integer field into as few bits as its authored range needs, and
  keeps positions in arrays of their own.
* **Whole-program compilation.** The libraries, the glue and the user's scripts
  are one C program: the C compiler inlines across all of it, which a runtime
  that loads assemblies separately cannot do.

## Whole-program optimisation

The larger win is what crust can *prove*. A Unity player loads arbitrary code
and must assume any of it might read anything at any time. crust has the whole
program in view -- every library line, every user script, every scene and
asset -- so it can see what is actually read and written, and specialise.

### Example: an object no script reads

Take an object whose transform is animated (an AnimationClip, or a parent it
follows) and which no script ever reads -- a decorative flag waving on a pole,
a background layer bobbing. On a CPU engine its animation curves are evaluated,
its local transform composed with its parent chain, and its world matrix
written, every frame, so that someone *could* read it.

If crust can prove nobody does, none of that has to happen on the CPU. The
curves and the parent chain go to the GPU instead: the vertex shader evaluates
the keyframes at the frame's time and composes the hierarchy, from data
uploaded once. The CPU does nothing for that object per frame.

The proof is the hard part, and it must be conservative. An object qualifies
only if **every** way of observing its transform is accounted for:

* no script reads its `transform` (position, rotation, scale, matrices,
  `TransformPoint`, ..) or those of any of its descendants;
* no script reaches it indirectly in a way that could: no `GameObject.Find` /
  `FindObjectOfType` / `GetComponent` result that could be it, no field holding a
  reference to it, no `SendMessage` with a name not known at pack time;
* nothing physical depends on it: no collider or rigidbody on it or below it
  (Box2D-Packed needs those transforms on the CPU);
* its animation is data the shader can evaluate (keyframed curves, not a script
  setting values per frame);
* nothing in the libraries reads it either (a camera following it, a culling
  pass, ..).

Anything that might break one of these -- reflection, a lookup by a name
computed at runtime, a reference crust cannot trace -- keeps the object on the
CPU. The fallback is always the ordinary, correct path; the optimisation only
removes work crust can prove is unobserved.

### Marking a tree for the GPU, and the cave benchmark

Proving every condition above automatically is the end goal; to start, the
user marks a tree: **`GpuHierarchy`**, an empty MonoBehaviour on a root, says
"the animation and transforms under here may be computed on the GPU -- no
script reads them". It is an ordinary component, so the project still opens in
Unity, and crust can still check the simple cases (a marked tree that a script
*does* read is refused, with the line that reads it). **planned**

`tools/gen_cave_scene.py` generates the benchmark: a Unity project in
`/tmp/cave` -- worms made of chains of spheres (each segment the child of the
one before, depth 40 by default), animated by legacy clips whose euler curves
address every segment by its child path; bats with flapping wings; water
falling from holes into a pool under blue Light2Ds; red and yellow Light2Ds
at a big hole top left; every worm and bat root marked `GpuHierarchy`. Sizes
are flags (`--worms`, `--segments`, `--bats`, `--holes`). **exists**

The same cave, from the same scene model, for the other two tools: `--godot`
writes a Godot 4 project to `/tmp/godot_cave_scene` (Node2D chains, Sprite2Ds,
an AnimationPlayer a root whose tracks address each segment's `rotation` by
node path, PointLight2Ds, CPUParticles2D drips, a Camera2D; pixels and y down:
positions are `(x, -y) * 64`, rotations and curve keys negated, in radians),
and `--blender` writes `/tmp/blender_cave_scene/build_cave.py`, a bpy script
that builds it (empties for the hierarchy, UV spheres for the bodies, keyed
z rotations looping by a Cycles modifier, coloured point lights, a particle
emitter a hole) and saves `cave.blend` when a `blender` binary is on the
PATH. In Godot the mark is a C# `GpuHierarchy` on the roots; in Blender a
`GpuHierarchy` custom property. **exists** (godot_pack does not yet pack
AnimationPlayer, PointLight2D or CPUParticles2D.)

It packs (1,111 objects, depth 41, 595 draws a frame), and its first result is
a gap on the CPU side: the worms' curves are not played. Curve paths resolve
through packed objects only, and a segment that has nothing but a Transform
(its sphere is a child, so segment scales do not compound down the chain) is
not packed -- the path stops at `S1`. That was silent; unity_pack now warns,
naming the curve. So the order is:

1. **The CPU baseline** -- **exists.** Transform-only objects inside an
   animated tree are packed, so all 520 curves play; and a child's world
   position now composes its parents' rotation and scale (Unity's
   parent * T R S) -- it was their positions summed, so a worm stayed a
   straight line however its curves turned it, and any rotated or scaled
   parent left its children misplaced. World-position writes invert the same
   composition. Measured headless (`-O2`, one core): 0.03 ms a frame for the
   curves, 1.6 ms a frame to compose world positions for 595 sprites. That
   composition is naive -- each sprite walks its chain to the root, and each
   step re-derives its parent's basis from the root, so a depth-40 chain costs
   O(depth^2) -- so the comparison has three sides: this, a CPU pass that
   composes each node once a frame in parent-before-child order, and the GPU.
2. **The GPU path** for marked trees: the tree flattened in parent-before-child
   order (parent index, local position / rotation / scale, curve key ranges),
   uploaded once; each frame a compute shader evaluates the curves at the
   frame's time and composes world transforms level by level, and the sprites
   draw from them. The CPU does nothing for the tree.
3. **Compare**: the same scene with the mark honoured and ignored, the same
   transforms (to a tolerance), and the time each takes.

### Handing a tree back: `Release()`, and destruction

A marked tree's transforms live on the GPU, so code that is about to *read*
them must first hand the tree back. `GpuHierarchy.Release()` does that:
crust evaluates the tree once on the CPU, from the same curves at the same
time -- the animation is data, so the result is the same without reading
anything back from the GPU, and there is no stall -- and the tree stays on
the CPU from then on. In Unity, `Release()` does nothing. **planned** (the
contract exists; crust's side waits for the GPU path)

Destruction is the case that needs it. In
[Unity-2D-Destruction](https://github.com/crustos/Unity-2D-Destruction),
`GpuHierarchy` lives in the library (`Scripts/GpuHierarchy.cs`, beside
`MaxInstancesAttribute`), and `Explodable.explode()` calls `Release()` on a
`GpuHierarchy` in its parents before fracturing reads the source's position,
rotation and scale. The fragments then become Box2D bodies the CPU simulates;
what the GPU can still take is drawing them -- their count is bounded, so
their transforms go up as one fixed-size array a frame and draw in one batch.
**exists** (the library side)

`tools/gen_cave_scene.py --destructible` generates that benchmark: the cave
with Unity-2D-Destruction's runtime library copied in (the fork cloned beside
crust, or `UNITY_2D_DESTRUCTION`), each GPU-marked bat explodable
(Rigidbody2D, BoxCollider2D, Explodable with runtime Voronoi fracturing), and
a `Quake` script that explodes one every half second -- an animated
GPU tree handed back to the CPU mid-flight, then shattered. **exists**.
unity_pack refuses it today at `Explodable.cs`'s `MeshFilter`: runtime
fragments are textured meshes, which unity_pack does not pack yet. That is
the next step for this benchmark: `MeshFilter` / `MeshRenderer` as textured
triangles in the engine's draw batch.

### More of the same kind

The same view gives other optimisations, each guarded the same way:

* **Never written, never moved.** An object no script, animation or physics
  body moves is static: its world transform is computed once, at pack time, and
  its sprites can go into a static vertex buffer drawn without per-frame work.
* **Bounded counts.** A `List` that only ever holds the objects of one scene, or
  a class with `[MaxInstances(N)]`, becomes a fixed array: no growth, no
  reallocation, no bounds beyond N.
* **One implementation.** An interface with a single implementing class in the
  whole program calls it directly: no virtual dispatch.
* **Unused features compiled out.** unity_pack already emits only the APIs a
  project uses; with the libraries, the same goes for library features no game
  code reaches.
* **Physics shaped for the solver.** A convex collider of up to 8 vertices (a
  Voronoi fragment, a terrain chunk's merged rectangles) becomes one Box2D shape
  rather than many triangles; static terrain becomes chain shapes.
* **Assets resolved at pack time.** An atlas's JSON is parsed when the game is
  packed; an element looked up by a literal name becomes an index.

## Correctness first

Every one of these must be safe, and the rule is the one the packers already
follow: **refuse rather than miscompile**. An optimisation applies only where
crust can prove it does not change what the program does; otherwise the
ordinary path is used. And what crust cannot translate faithfully is refused
with a C# diagnostic at the line -- never emptied, never approximated
silently.

The translations are checked against the real thing: the C# subset against
Mono (`tests/test_csrust.py`), the packers against Unity's and Godot's
documented semantics (`tools/unity_pack_test_fast.py`,
`tools/godot_pack_test_fast.py`). An optimisation adds a test that packs a
project with and without it and compares what the game does.

## Plan

1. **One unit.** Translate a library -- many files -- as one compilation unit.
   This is the shared blocker across all three library ports: a base class, an
   interface or a `ref` argument that crosses files. **planned**
2. **The libraries to green.** Each fork's README has its blockers in order;
   work through them, with each fork's runtime scripts checked by a crust test
   (as `TestUnity2DDestruction` does now). **in progress**
3. **Glue instead of engine.** A first packer feature retargeted at a library --
   a scene's sprites drawn through Futile's batched renderer, a Rigidbody2D
   fragment through Unity-2D-Destruction -- with the generated code it replaces
   removed. **planned**
4. **The read / write analysis.** For each object: which scripts and library
   code can read or write its transform, components and fields. The foundation
   for every optimisation above; on its own it already answers "is this object
   static?". **planned**
5. **The first whole-program optimisations**: static objects baked at pack time,
   then unobserved animation and hierarchy on the GPU, each behind a test that
   compares the game with and without it. **planned**
