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

Pass `--aos` to keep positions in instance structs, or `--soa-vec4` for
`float[N][4]` tables (see `UNITY_PACK.md`). The scripts are bash
(`#!/usr/bin/env bash`); from fish just run the path — do not paste bash
`${...}` expansions into fish.

```
./examples/unity_pack/run_gles2_window.sh --aos
```

`run_gles2.sh` packs this scene, links `gles2_view.c`, and draws each
object as a coloured quad (surfaceless FBO → ASCII).

`run_gles2_window.sh` runs `unity_pack.py`, which links `gles2_window.c`
into `$TMPDIR/<project>/<productName>` (override the directory with
`OUT=`). The player keeps moving every frame (`Time.deltaTime` from the
frame clock). Arrow keys / WASD poke `engine_input_axis_*` for
`Input.GetAxis`. Quit via authored `Application.Quit` or the window
close control (no Escape/Q host shortcut).

SystemsScene (Pad + Ball + … → `/tmp/SystemsScene/SystemsScene`):

```
PROJECT="$(pwd)/examples/unity_pack/SystemsScene" \
  ./examples/unity_pack/run_gles2_window.sh
```
