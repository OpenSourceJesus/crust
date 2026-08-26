#!/bin/sh
# Build runtime/crust_re_cpp_test.cpp two ways and require identical output:
# once with the host C++ compiler, once lowered to C by tools/cpprust.py. The
# second is the one that matters -- it is what Crust actually does with the
# header, and it caught a free-function overload the host compiler accepted.
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

g++ -std=c++11 -Wall -I"$here" "$here/crust_re_cpp_test.cpp" "$here/crust_re.c" \
    -o "$tmp/host" 2>"$tmp/gpp.log" || { cat "$tmp/gpp.log"; exit 1; }
"$tmp/host" > "$tmp/host.out"

python3 "$root/tools/cpprust.py" "$here/crust_re_cpp_test.cpp" \
    -o "$tmp/lowered.c" --incdir "$here"
cc -std=c99 -Wall -I"$here" "$tmp/lowered.c" "$here/crust_re.c" \
    -o "$tmp/lowered" 2>"$tmp/cc.log" || { cat "$tmp/cc.log"; exit 1; }
"$tmp/lowered" > "$tmp/lowered.out"

if ! diff -u "$tmp/host.out" "$tmp/lowered.out"; then
    echo "crust_re C++ frontend: host and cpprust output differ"
    exit 1
fi
cat "$tmp/host.out"
echo "crust_re C++ frontend: host and cpprust agree"
