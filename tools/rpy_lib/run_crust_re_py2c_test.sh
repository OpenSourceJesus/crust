#!/bin/sh
# Compile a py2c test with py2c and require the binary's output to match
# CPython's. Takes the script name as $1 (default: the crust_re frontend test).
#
# These live here rather than in tools/minipy/ because they are py2c-only: they
# exercise shims (crust_re tiers, subprocess, tempfile) that the minipy guest
# interpreter has no module for, so the 3-way cpython/ref/native comparison
# that tools/minipy/test_*.py get does not apply.
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$(dirname "$here")")
src="$here/${1:-test_crust_re_py2c.py}"
[ -f "$src" ] || { echo "no such test: $src"; exit 1; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

python3 "$src" > "$tmp/cpython.out"
# py2c emits into /tmp under a name derived from the input, so give the copy a
# unique basename to avoid colliding with a concurrent run.
base="crust_re_py2c_$$"
cp "$src" "$tmp/$base.py"
python3 "$root/tools/py2c.py" "$tmp/$base.py" > "$tmp/py2c.log" 2>&1 \
    || { cat "$tmp/py2c.log"; exit 1; }
trap 'rm -rf "$tmp" "/tmp/$base.c"' EXIT
cc -std=c99 -w -I/tmp "/tmp/$base.c" /tmp/shivyc_rt.c \
   -o "$tmp/native" 2>"$tmp/cc.log" || { cat "$tmp/cc.log"; exit 1; }
"$tmp/native" > "$tmp/native.out"

if ! diff -u "$tmp/cpython.out" "$tmp/native.out"; then
    echo "py2c: cpython and native output differ for $(basename "$src")"
    exit 1
fi
cat "$tmp/cpython.out"
echo "py2c: cpython and native agree for $(basename "$src")"
