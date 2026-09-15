# MiniScene — fixture for `tools/unity_pack.py`

A tiny Unity-shaped project: two hand-placed coins (never spawned) and
one player that writes `transform.position` in `Update`.

```
python3 tools/unity_pack.py examples/unity_pack/MiniScene -o /tmp/upack
gcc -O3 -c /tmp/upack/engine.c
gcc -O0 -c /tmp/upack/data.c
```

Coins should pack to ≤16 bytes (2D, static float16 positions, bitfield
`hp`/`value`, `uint8_t` index). The player stays larger because it moves
(`float32` x/y).

## View with GLES2

```
./examples/unity_pack/run_gles2.sh           # surfaceless FBO → ASCII
./examples/unity_pack/run_gles2_window.sh    # real GLFW window (animated)
./examples/unity_pack/run_gles2_wasm.sh      # soft GLES under node
```

Pass `--soa` to any of them to pack contiguous position tables (see
`UNITY_PACK.md`). The scripts are bash (`#!/usr/bin/env bash`); from fish
just run the path — do not paste bash `${...}` expansions into fish.

```
./examples/unity_pack/run_gles2_window.sh --soa
```

`run_gles2.sh` packs this scene, links `gles2_view.c`, and draws each
object as a coloured quad (surfaceless FBO → ASCII).

`run_gles2_window.sh` links `gles2_window.c` against the same packed
engine and opens an OpenGL ES 2.0 window via GLFW. The player keeps
moving every frame (`Time.deltaTime` from the frame clock). Escape or Q
closes the window.
