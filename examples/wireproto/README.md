# Dual FE/BE wire protocol

Same Crust sources compile to a **native backend** and a **wasm frontend**.
Because Crust uses `sizeof(void *) == 8` and the same SysV-style layout on both
targets, a POD struct is already a binary protocol -- no separate IDL.

This example is the thin slice for [issue #15](https://github.com/brentharts/crust/issues/15):
joint compile, shared structs, FE/BE unit test. Not in scope here: polymorphic
wasm anti-tampering, or RPython `with frontend(...):` / HTML glue generation.

## Layout = wire format

| Type | Size | Fields (offsets) |
|------|------|------------------|
| `struct WireReq` | 12 | `tag@0`, `a@4`, `b@8` |
| `struct WireRep` | 8 | `tag@0`, `result@4` |

Integers are little-endian host `int`. Do not put process pointers on the wire;
only integers and fixed arrays travel.

Stay inside the C subset documented in [CRUST.md](../../CRUST.md) /
[CPPRUST.md](../../CPPRUST.md): plain POD C structs, no C++ classes, no
bitfields, no flexible arrays.

## Build both sides

```sh
python3 tools/dual_compile.py examples/wireproto/codec.c \
    -I examples/wireproto -o /tmp/wireproto/codec
# -> /tmp/wireproto/codec  and  /tmp/wireproto/codec.wasm
```

```sh
/tmp/wireproto/codec                 # native self-check (exit 0)
node tools/wasm_run.js /tmp/wireproto/codec.wasm
```

## Joint test

```sh
make test_wireproto
# or: python3 tools/wireproto_test.py
```

Checks layout exports match, then native→wasm and wasm→native ADD / PING.
