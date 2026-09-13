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
