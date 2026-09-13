# Unity-shaped math (Mathf, Vector2, Vector3, Quaternion)

Emulates the common UnityEngine value-type API on the Crust C++ subset.
Not a Unity runtime — naming and behaviour only.

```sh
python3 tools/cpprust.py examples/unity/demo.cpp -o /tmp/unity_demo.c --no-clang
gcc -w -O2 -o /tmp/unity_demo /tmp/unity_demo.c -lm
/tmp/unity_demo; echo $?   # 0 = all checks passed
```

| Type | Covered |
|------|---------|
| `Mathf` | Abs, Sign, Min/Max, Clamp/Clamp01, Lerp, InverseLerp, Approximately, Sqrt/Sin/Cos/Acos/Atan2, PI/Deg2Rad/Rad2Deg |
| `Vector2` | fields, magnitude, normalized, Dot/Distance/Lerp/Scale, `+`/`-`, `.scaled(s)` |
| `Vector3` | same + Cross, up/forward/right |
| `Quaternion` | identity, Euler (degrees), Normalize, Dot, `.Multiply(q)`, `.Rotate(v)` |

Subset notes: scalar multiply is `.scaled` (same as `examples/eigen`); quat×quat /
quat×vec are named methods because the subset does not overload `operator*`
on two different right-hand types.
