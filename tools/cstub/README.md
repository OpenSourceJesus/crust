# cstub -- headers for compiling cpprust output

Translating a C++ file leaves its `#include` directives in place, so the
emitted C still asks for the headers the source asked for. The C ones
(`<stddef.h>`, `<string.h>`, ..) resolve normally. The C++-only ones do
not exist for a C compiler, so this directory supplies empty stand-ins:

    gcc -fsyntax-only -I tools/cstub out.c

Every file here is empty on purpose. The lowering supplies its own `std`
for what it supports and translates the uses, so no declaration from these
headers should be needed. **If adding a declaration here makes an output
compile, that is a translation defect** -- the C is relying on something
the lowering was supposed to have handled -- and the fix belongs in
`cpprust.py`, not here.

This is what makes "the output compiles" a meaningful check. Without it
every output fails on a missing `<type_traits>` and real defects are
invisible behind the noise.
