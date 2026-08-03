# Run ShivyC's test suite with PyPy3 (much faster than CPython).
#
# The tests shell out to a `shivyc` executable (subprocess.run(["shivyc", ...])).
# That executable is only on PATH if the package's console-script was installed,
# which it is not in a fresh checkout.
# Rather than require `pip install`, we drop a tiny `bin/shivyc` shim that runs
# the compiler as a module under pypy3, and put it on PATH (with the repo root
# on PYTHONPATH so it imports without being installed). The shim also means the
# compiler itself runs under PyPy3.

ROOT := $(shell pwd)
export PYTHONPATH := $(ROOT)$(if $(PYTHONPATH),:$(PYTHONPATH),)
export PATH := $(ROOT)/bin:$(PATH)

# ---------------------------------------------------------------------------
# Micropython compile-testing
#
# `make install_micropython` clones (or updates) the objcore micropython fork
# and generates its qstr/module headers. The `test_micropython*` targets then
# compile-check slices of it through ShivyCX (under PyPy3) via tools/mpy_test.py.
MPY_REPO ?= https://github.com/OpenSourceJesus/micropython
MPY_DIR  ?= $(ROOT)/micropython
MPY_PORT := $(MPY_DIR)/ports/objcore
MPY_GENHDR := $(MPY_PORT)/build/genhdr/qstrdefs.generated.h

# Large external codebases compiled via the generic runner tools/bigtest.py.
# These targets are NOT part of `make test`/`make default`; they are opt-in and
# slow. bigtest skips large .c files by default (override via MAX_KB) and
# forwards extra defines/includes (DEFS / INCS) so quick experiments are easy:
#     make test_cpython DEFS='-D FOO=1' MAX_KB=128
MAX_KB ?= 64
DEFS   ?=
INCS   ?=
# Parallel compile jobs for the big-codebase targets; 0 = one job per CPU.
JOBS   ?= 0

# CPython object model, compiled against the built-in musl (--musl).
CPY_REPO ?= https://github.com/OpenSourceJesus/cpython-tinier
CPY_DIR  ?= $(ROOT)/cpython-tinier
CPY_CONFIG := $(CPY_DIR)/pyconfig.h
CPY_DEFS := -D Py_BUILD_CORE -D thread_local=_Thread_local \
            -D _Py_USE_GCC_BUILTIN_ATOMICS=1

# 2.11BSD userland, compiled with BSD's own headers.
BSD_REPO ?= https://github.com/brentharts/2.11BSD-riscv
BSD_DIR  ?= $(ROOT)/2.11BSD-riscv

default:
	chmod +x ./crust
	./crust examples/crust/shapes.c -o /tmp/shapes
	/tmp/shapes

# ---------------------------------------------------------------------------
# Crust: the C+Rust(+rpython) front end. See CRUST.md.
#
#   make test_crust       full check -- the translation-layer unit tests plus
#                         every example in examples/crust, each compiled, run
#                         and checked against its expected output/exit status.
#   make test_fast_crust  the same example runner over a subset that still
#                         covers all three front ends and both include paths.
#                         Leans on the /tmp transpile+object caches, so it is
#                         quick enough to run on every edit.
#
# Both use tools/crust_examples.py, which holds the expected output for each
# example so a silent miscompile that still exits 0 is caught.
test_crust:
	python3 -m unittest tests.test_crust -v 2>&1 | tail -5
	python3 tools/crust_examples.py -v

test_fast_crust:
	@python3 tools/crust_examples.py --fast -v

# Survey how much of a real Rust codebase Crust can compile, and what is
# blocking the rest. Point it at any Rust tree:
#     make crustos_survey REDOX=~/redox-kernel
# tools/crustos.py also has `build` and `stage` modes; it ignores Cargo
# entirely and just walks the tree for .rs files.
REDOX ?= $(ROOT)/redox-kernel
# Build and run CrustOS: the compatible subset of the real Redox kernel,
# linked with the scheme layer and scheduler in crustos/. See CRUSTOS.md.
crustos:
	python3 tools/crustos.py fetch
	python3 tools/crustos.py run



crustos_survey:
	python3 tools/crustos.py survey --blockers --verify

# Drop the cached rpython transpiles and runtime objects that a
# `#include "*.py"` builds up under /tmp.
clean_crust:
	rm -rf /tmp/crust-rpy build/crust

# Run the ENTIRE test suite via unittest discovery (still under pypy3).
test: shim
	cd tests && pypy3 -m unittest discover -s .

# Create bin/shivyc -> `pypy3 -m shivyc.main "$@"`.
shim:
	@mkdir -p bin
	@printf '#!/bin/sh\nexec pypy3 -m shivyc.main "$$@"\n' > bin/shivyc
	@chmod +x bin/shivyc

# Install the build/test toolchain (compilers, qemu, pypy3). This used to be
# `make install`; that name now installs the bootstrapped compiler (see the
# bootstrap section below).
install_deps:
	sudo apt-get update
	sudo apt-get install -y build-essential gcc gcc-multilib binutils make \
		python3 qemu-system-x86 git pypy3

clean:
	rm -rf bin build

# ---------------------------------------------------------------------------
# Self-hosting: transpile ShivyCX's own source to C and build/test/bench it.
#
# tools/selfhost.py transpiles ShivyCX modules with tools/py2c.py, compiles
# them, and links runnable test/benchmark exes. Two link backends: `host`
# (plain gcc, for pure-C modules like tokens) and `objcore` (links the
# micropython objcore core, for code that uses the dynamic mp bridge). All C
# test/bench harnesses are inlined in the script and written to /tmp at build
# time. These targets are NOT part of `make test`; they are opt-in.
#     make selfhost                 # end-to-end module tests (host backend)
#     make selfhost_objcore         # also the objcore-bridge test (needs the
#                                   #   objcore build: see install_micropython
#                                   #   + a built ports/objcore/build/py)
#     make selfhost_bench           # transpiled-code microbenchmarks
#     make selfhost_coverage        # how many modules gcc-compile (glibc)
#     make selfhost_coverage_musl   # ... against the packaged musl headers
selfhost:
	python3 tools/selfhost.py test

selfhost_objcore:
	python3 tools/selfhost.py test --objcore

selfhost_bench:
	python3 tools/selfhost.py bench

selfhost_coverage:
	python3 tools/selfhost.py coverage

# Build the self-host artifacts into a fixed /tmp directory (kept on disk) and
# run the simple per-module self-host tests against them. The build dir holds
# the transpiled+compiled module test exes (tokens, ilbase, weak_alias).
#     make selfhost_build              # -> /tmp/shivyc-selfhost
SELFHOST_DIR ?= /tmp/shivyc-selfhost
selfhost_build:
	python3 tools/selfhost.py test --build-dir $(SELFHOST_DIR)
	@echo "self-host build kept in $(SELFHOST_DIR)"

# Compile-speed benchmark: time the ShivyCX compiler vs gcc on the same input.
# Override the compiler under test with SHIVYC=... (e.g. a native binary):
#     make bench_compile_speed
#     make bench_compile_speed SHIVYC='pypy3 -m shivyc.main'
bench_compile_speed:
	python3 benchmarks/compile_speed/bench_compile_speed.py $(BENCH_ARGS)

selfhost_link:
	python3 tools/selfhost.py link

# EXPERIMENTAL: build the whole self-hosted compiler as a single native binary
# into a fixed /tmp dir. Links cleanly (0 undefined refs); full module-init
# ordering is still being worked out, so the binary is not yet a working
# compiler. Useful as a reproducible base for that work.
#     make selfhost_compiler            # -> $(SELFHOST_NATIVE_DIR)/shivyc_native
SELFHOST_NATIVE_DIR ?= /tmp/shivyc-native
selfhost_compiler:
	python3 tools/selfhost.py compiler --build-dir $(SELFHOST_NATIVE_DIR)

# ---------------------------------------------------------------------------
# Bootstrap: build the self-hosted compiler and (eventually) self-compile it.
#
# Everything lands in $(BOOTSTRAP_DIR) (kept on disk). Override to relocate:
#     make bootstrap BOOTSTRAP_DIR=/tmp/shivyc-bootstrap
#
#   make bootstrap   Stage 1. Transpile ShivyCX's own source with py2c, compile
#                    it with gcc into $(BOOTSTRAP_DIR)/shivyc_native, smoke-test
#                    that native binary, then benchmark its compile speed
#                    against gcc (the same headerless ~200-line program through
#                    both compilers).
#
#   make bootstrap2  Stage 2. Feed the compiler's own generated C back through
#                    the stage-1 native binary to produce the final
#                    $(BOOTSTRAP_DIR)/shivycx, then run the full test suite
#                    against it. A complete stage-2 self-compile is the
#                    milestone we are working toward: until the native compiler
#                    accepts every construct it emits for its own source, this
#                    reports how many modules already self-compile (a progress
#                    gauge) and the first blocker.
#
#   make install     Copy the bootstrapped compiler to $(PREFIX)/bin/shivycx
#                    (prefers the stage-2 shivycx; falls back to the stage-1
#                    native binary). Override PREFIX (default /usr/local).
BOOTSTRAP_DIR ?= /tmp/shivyc-bootstrap
PREFIX        ?= /usr/local

bootstrap:
	python3 tools/selfhost.py bootstrap --build-dir $(BOOTSTRAP_DIR)

bootstrap2:
	python3 tools/selfhost.py bootstrap2 --build-dir $(BOOTSTRAP_DIR)

install:
	@cx="$(BOOTSTRAP_DIR)/shivycx"; \
	if [ ! -x "$$cx" ]; then cx="$(BOOTSTRAP_DIR)/shivyc_native"; fi; \
	if [ ! -x "$$cx" ]; then \
	  echo "no bootstrapped compiler in $(BOOTSTRAP_DIR); run 'make bootstrap' first"; \
	  exit 1; fi; \
	echo "installing $$cx -> $(PREFIX)/bin/shivycx"; \
	install -d $(PREFIX)/bin && install -m 0755 "$$cx" $(PREFIX)/bin/shivycx

selfhost_coverage_musl:
	python3 tools/selfhost.py coverage --musl

# ---------------------------------------------------------------------------
# rpython examples
#
# Compile each rpython example straight to a native binary through
# shivyc.main's `.py` path (transpile -> C -> ShivyCX -> link) and check its
# exit code. Examples without a `main()` are libraries and are not listed here.
#     make rpython
RPY    := examples/rpython2c
RPYBIN := build/rpython

# ---------------------------------------------------------------------------
# GUI examples: pure-rpython native Wayland clients. py2c generates ALL the C
# (the Wayland runtime + scanned xdg-shell + the bundled rwayland / rpyqt
# libraries); we just transpile and link against libwayland-client.
#
#     make wayland   # the rwayland framebuffer demo  -> build/gui/wayland_app
#     make rpyqt     # the PyQt-shaped counter        -> build/gui/qt_app
#
# Requires libwayland-dev + wayland-scanner (a vendored xdg-shell.xml is used
# when /usr/share/wayland-protocols is absent). Run the binary under a Wayland
# compositor; without one it prints "failed to connect" and exits non-zero.
GUIBIN := build/gui
WL_CFLAGS ?= -O2 -w
WL_LIBS   ?= -lwayland-client

wayland:
	@mkdir -p $(GUIBIN)/wayland
	@cp -f $(RPY)/rpy_lib/xdg-shell.xml $(GUIBIN)/wayland/ 2>/dev/null || true
	python3 tools/py2c.py $(RPY)/wayland_helloworld.py --out $(GUIBIN)/wayland
	cc $(WL_CFLAGS) -I$(GUIBIN)/wayland $(GUIBIN)/wayland/*.c \
	    -o $(GUIBIN)/wayland_app $(WL_LIBS)
	@echo "built $(GUIBIN)/wayland_app  (run it under a Wayland compositor)"

rpyqt:
	@mkdir -p $(GUIBIN)/rpyqt
	@cp -f $(RPY)/rpy_lib/xdg-shell.xml $(GUIBIN)/rpyqt/ 2>/dev/null || true
	python3 tools/py2c.py $(RPY)/rpyqt_helloworld.py --out $(GUIBIN)/rpyqt
	cc $(WL_CFLAGS) -I$(GUIBIN)/rpyqt $(GUIBIN)/rpyqt/*.c \
	    -o $(GUIBIN)/qt_app $(WL_LIBS)
	@echo "built $(GUIBIN)/qt_app  (run it under a Wayland compositor)"

controls:
	@mkdir -p $(GUIBIN)/controls
	@cp -f $(RPY)/rpy_lib/xdg-shell.xml $(GUIBIN)/controls/ 2>/dev/null || true
	python3 tools/py2c.py $(RPY)/rpyqt_controls.py --out $(GUIBIN)/controls
	cc $(WL_CFLAGS) -I$(GUIBIN)/controls $(GUIBIN)/controls/*.c \
	    -o $(GUIBIN)/controls_app $(WL_LIBS)
	@echo "built $(GUIBIN)/controls_app  (run it under a Wayland compositor)"

# The rpython mini web-browser: a pure-rpython DOM renderer (json2qt.py + dom.py)
# fed by a generated page (page_data.py). The CPython helper www2json.py turns
# example.html into BOTH page.json (the canonical bundle) and page_data.py (its
# "py" form), then py2c co-compiles the three rpython files with rpyqt and emits
# the Wayland glue.  -> build/gui/minibrowser_app
MB := $(RPY)/minibrowser
minibrowser:
	@mkdir -p $(GUIBIN)/minibrowser
	@cp -f $(RPY)/rpy_lib/xdg-shell.xml $(GUIBIN)/minibrowser/ 2>/dev/null || true
	@# The page-scripting engine embeds the minipy interpreter: generate a copy
	@# with its main() stripped (json2qt provides the C entry point) and
	@# co-compile it into the browser.
	python3 $(MB)/gen_embed.py $(GUIBIN)/minibrowser >/dev/null
	python3 tools/py2c.py $(MB)/json2qt.py $(MB)/dom.py $(MB)/minijson.py \
	    $(GUIBIN)/minibrowser/interp_embed.py --out $(GUIBIN)/minibrowser
	cc $(WL_CFLAGS) -rdynamic -I$(GUIBIN)/minibrowser $(GUIBIN)/minibrowser/*.c \
	    tools/rpy_lib/mb_ffi.c \
	    -o $(GUIBIN)/minibrowser_app $(WL_LIBS) -lm
	@# Stage the runnable site + toolchain next to the binary: at runtime the
	@# browser shells out to `python3 www2json.py` (fetch) and, for scripted
	@# pages, `python3 pycompile.py` (compile <script type="python"> to .mpyc,
	@# which needs the minipy package + minidom.py).
	@cp -f $(MB)/www2json.py $(MB)/pycompile.py $(MB)/minidom.py \
	    $(MB)/jitc.py $(MB)/js2py.py $(MB)/ts2py.py $(MB)/mb_imgcache.py \
	    $(MB)/home.html $(MB)/about.html $(MB)/example.html \
	    $(MB)/pyscript.html $(MB)/pyscript2.html $(MB)/pyjit.html \
	    $(MB)/canvas.html $(MB)/jsdemo.html $(MB)/twoway.html $(MB)/ts.html \
	    $(MB)/domnative.html $(MB)/domio.html $(MB)/objptr.html \
	    $(MB)/domx.html $(MB)/img.html $(MB)/logo.png \
	    $(GUIBIN)/
	@rm -rf $(GUIBIN)/minipy && cp -r tools/minipy $(GUIBIN)/minipy
	@python3 $(MB)/www2json.py $(MB)/home.html --out $(GUIBIN) >/dev/null
	@echo "built $(GUIBIN)/minibrowser_app  (run from $(GUIBIN) under a Wayland compositor)"
	@echo "  cd $(GUIBIN) && ./minibrowser_app"
	@echo "  cd $(GUIBIN) && ./minibrowser_app --script-selftest   # run a page's python"
	@echo "  cd $(GUIBIN) && ./minibrowser_app --jit-selftest      # JIT rpython + native ctypes call"

# Live demo: build the browser and open it directly on a real web page whose
# HTML references an image -- the standard "Lenna" test image on Wikipedia. On
# fetch, www2json downloads the page, and mb_imgcache (PIL) downloads and
# converts the <img> it references into a blit-ready cache file, which the
# QImage widget composites into the window. Needs a Wayland compositor and
# working internet. Override the page with `make lenna LENNA_URL=...`.
LENNA_URL ?= https://en.wikipedia.org/wiki/Lenna
lenna: minibrowser
	@echo "opening minibrowser on $(LENNA_URL)"
	@echo "(fetches the page + its image live; needs a Wayland compositor)"
	cd $(GUIBIN) && XDG_RUNTIME_DIR=$${XDG_RUNTIME_DIR:-/run/user/$$(id -u)} \
	    ./minibrowser_app "$(LENNA_URL)"

# Rebuild the SAME browser with ShivyC (our own C compiler + assembler) instead
# of gcc, to prove the toolchain can self-compile it. gcc stays the default
# (it is faster); this target is a compatibility check. Run `make minibrowser`
# first (it generates the .c and stages the runtime files). The system cpp only
# preprocesses (the C standard separates preprocessing from compilation) with
# -P to suppress line markers -- which would otherwise split ShivyC's stdarg
# macro calls; ShivyC does all the actual compilation, optimization, and
# linking, including -rdynamic and -lwayland-client (both added to ShivyC).
# Optimization level via OPT=-O0..-O4 (default -O0).
OPT ?= -O0
minibrowser_shivyc:
	@test -d $(GUIBIN)/minibrowser || { echo "run 'make minibrowser' first"; exit 1; }
	@mkdir -p $(GUIBIN)/minibrowser_shivyc_pp
	@echo "preprocessing browser TUs with cpp -E -P ..."
	@for src in $(GUIBIN)/minibrowser/json2qt.c $(GUIBIN)/minibrowser/dom.c \
	    $(GUIBIN)/minibrowser/minijson.c $(GUIBIN)/minibrowser/interp_embed.c \
	    $(GUIBIN)/minibrowser/rpyqt.c $(GUIBIN)/minibrowser/rwayland_rt.c \
	    $(GUIBIN)/minibrowser/xdg-shell-protocol.c \
	    $(GUIBIN)/minibrowser/shivyc_rt.c tools/rpy_lib/mb_ffi.c; do \
	  cc -E -P -I$(GUIBIN)/minibrowser $$src \
	    > $(GUIBIN)/minibrowser_shivyc_pp/`basename $$src` 2>/dev/null; \
	done
	@echo "compiling + linking with ShivyC $(OPT) ..."
	python3 -m shivyc.main --no-cache $(OPT) -rdynamic \
	    $(GUIBIN)/minibrowser_shivyc_pp/*.c \
	    -o $(GUIBIN)/minibrowser_app_shivyc $(WL_LIBS) -lm
	@echo "built $(GUIBIN)/minibrowser_app_shivyc with ShivyC $(OPT)"
	@echo "  cd $(GUIBIN) && ./minibrowser_app_shivyc --canvas-selftest"

rpython:
	@mkdir -p $(RPYBIN)
	@fail=0; \
	run() { \
	  src=$$1; exp=$$2; in=$$3; out=$(RPYBIN)/`basename $$src .py`; \
	  if ! python3 -m shivyc.main --no-cache $$src -o $$out >/dev/null 2>&1; then \
	    echo "  FAIL(compile) $$src"; fail=1; return; fi; \
	  if [ -n "$$in" ]; then echo "$$in" | timeout 20 $$out >/dev/null 2>&1; \
	  else timeout 20 $$out >/dev/null 2>&1; fi; rc=$$?; \
	  if [ "$$rc" = "$$exp" ]; then echo "  ok    $$src (exit $$rc)"; \
	  else echo "  FAIL  $$src (exit $$rc, expected $$exp)"; fail=1; fi; }; \
	runm() { exp=$$1; shift; out=$(RPYBIN)/multi_`basename $$1 .py`; \
	  if ! python3 -m shivyc.main --no-cache "$$@" -o $$out >/dev/null 2>&1; then \
	    echo "  FAIL(compile) $$*"; fail=1; return; fi; \
	  timeout 20 $$out >/dev/null 2>&1; rc=$$?; \
	  if [ "$$rc" = "$$exp" ]; then echo "  ok    [multi] $$* (exit $$rc)"; \
	  else echo "  FAIL  [multi] $$* (exit $$rc, expected $$exp)"; fail=1; fi; }; \
	run $(RPY)/numpy/simd_kernels.py 55 ""; \
	run $(RPY)/numpy/simd_blas.py   186 ""; \
	run $(RPY)/numpy/ufuncs.py       49 ""; \
	run $(RPY)/numpy/fusion.py       97 ""; \
	run $(RPY)/numpy/matmul.py      239 ""; \
	run $(RPY)/nn/neural_net.py     199 ""; \
	run $(RPY)/nn/torch_mlp.py        4 ""; \
	run $(RPY)/nn/torch_mlp_f32.py    4 ""; \
	run $(RPY)/nn/quant_mlp.py       50 ""; \
	run $(RPY)/ffi/ffi_math.py       35 ""; \
	run $(RPY)/nbody/nbody.py        11 ""; \
	run $(RPY)/classes/polymorphism.py 22 ""; \
	run $(RPY)/classes/pod_vs_object.py  48 ""; \
	run $(RPY)/lists/typed_list.py       65 ""; \
	run $(RPY)/dicts/typed_dict.py       58 ""; \
	run $(RPY)/compiler/lexer_kernel.py  13 ""; \
	run $(RPY)/memory/del_demo.py    60 ""; \
	run $(RPY)/memory/autofree.py   135 ""; \
	run $(RPY)/io/simple_io.py        5 world; \
	run $(RPY)/net/socket_echo.py     5 ""; \
	run $(RPY)/mandelbrot/mandelbrot.py 70 ""; \
	run $(RPY)/sysinfo/sysinfo.py        7 ""; \
	run $(RPY)/collections/containers.py 10 ""; \
	run $(RPY)/dynattr/app.py 126 ""; \
	run $(RPY)/rtattr/app.py 48 ""; \
	run $(RPY)/crossattr/app.py 114 ""; \
	run $(RPY)/aggregates/app.py 84 ""; \
	run $(RPY)/formatting/app.py 33 ""; \
	run $(RPY)/ctorval/app.py 23 ""; \
	run $(RPY)/sets/app.py 35 ""; \
	run $(RPY)/dictops/app.py 186 ""; \
	run $(RPY)/wordfreq/app.py 93 ""; \
	run $(RPY)/untyped/app.py 41 ""; \
	run $(RPY)/promote/app.py 70 ""; \
	run $(RPY)/pgo/app.py 70 ""; \
	runm 38 $(RPY)/multifile/app.py $(RPY)/multifile/geom.py; \
	runm 44 $(RPY)/pgo_multi/app.py $(RPY)/pgo_multi/hist.py; \
	runm 45 $(RPY)/ambig/app.py $(RPY)/ambig/node_a.py $(RPY)/ambig/node_b.py; \
	runm 55 $(RPY)/fieldwrite/app.py $(RPY)/fieldwrite/lib.py; \
	if [ $$fail = 0 ]; then echo "rpython examples: all passed"; \
	else echo "rpython examples: FAILURES"; fi; exit $$fail

# ---------------------------------------------------------------------------
# Fast smoke test: two oracle programs (a single-file syntax sweep and a
# multi-file cross-module case) compiled three ways -- CPython (the oracle),
# the ShivyCX self-compiler, and the py2c->gcc transpiler -- requiring all
# three to agree. Covers most of the language subset in a few seconds, so it
# stands in for the full suite when iterating.
#     make testfast
FAST    := tests/fast
FASTBIN := build/fast
testfast:
	@mkdir -p $(FASTBIN)
	@fail=0; \
	sx_run() { out=$(FASTBIN)/$$1_sx; shift; \
	  if ! python3 -m shivyc.main --no-cache "$$@" -o $$out >/dev/null 2>&1; then echo ERR; return; fi; \
	  timeout 30 $$out >/dev/null 2>&1; echo $$?; }; \
	gcc_run() { d=$(FASTBIN)/$$1_c; rm -rf $$d; mkdir -p $$d; shift; \
	  if ! python3 tools/py2c.py "$$@" --out $$d >/dev/null 2>&1; then echo ERR; return; fi; \
	  python3 -c "import sys;sys.path.insert(0,'tools');import py2c;py2c.write_runtime('$$d')" >/dev/null 2>&1; \
	  if ! gcc -std=c99 -I$$d $$d/*.c -o $$d/bin 2>/dev/null; then echo ERR; return; fi; \
	  timeout 30 $$d/bin >/dev/null 2>&1; echo $$?; }; \
	report() { if [ "$$3" = "$$2" ] && [ "$$4" = "$$2" ]; then \
	    echo "  ok    $$1 (cpython=$$2 shivycx=$$3 gcc=$$4)"; \
	  else echo "  FAIL  $$1 (cpython=$$2 shivycx=$$3 gcc=$$4)"; fail=1; fi; }; \
	orc=`python3 $(FAST)/syntax_core.py >/dev/null 2>&1; echo $$?`; \
	sx=`sx_run syntax_core $(FAST)/syntax_core.py`; \
	cc=`gcc_run syntax_core $(FAST)/syntax_core.py`; \
	report "syntax_core (single file)" $$orc $$sx $$cc; \
	orc=`cd $(FAST)/multi && python3 main.py >/dev/null 2>&1; echo $$?`; \
	M="$(FAST)/multi/main.py $(FAST)/multi/geometry.py $(FAST)/multi/shapes.py"; \
	sx=`sx_run multi $$M`; \
	cc=`gcc_run multi $$M`; \
	report "multi (cross-module)" $$orc $$sx $$cc; \
	if [ $$fail = 0 ]; then echo "testfast: PASS"; \
	else echo "testfast: FAIL"; fi; exit $$fail

# Differential check for the minipy pipeline (rast.py parser + minipy2c.py
# transpiler + interpreter unit tests), three ways: CPython ground truth, the
# pure-Python reference VM, and the py2c-compiled native interpreter. Mirrors
# testfast but adds minipy as a third executor, per MINIPY.md section 9.
testminipy:
	@fail=0; \
	run3() { s="$$1"; \
	  c=`python3 "$$s" 2>&1 | md5sum | cut -c1-12`; \
	  r=`python3 tools/rpy.py --ref "$$s" 2>&1 | md5sum | cut -c1-12`; \
	  n=`python3 tools/rpy.py "$$s" 2>&1 | md5sum | cut -c1-12`; \
	  if [ "$$c" = "$$r" ] && [ "$$c" = "$$n" ]; then echo "  ok    $$s"; \
	  else echo "  FAIL  $$s (cpython=$$c ref=$$r native=$$n)"; fail=1; fi; }; \
	echo "-- minipy interpreter unit tests (3-way) --"; \
	for t in tools/minipy/test_*.py; do run3 "$$t"; done; \
	echo "-- parser + transpiler agreement --"; \
	if python3 tools/rpy_lib/rast_test.py    >/dev/null 2>&1; then echo "  ok    rast_test (4-way)"; else echo "  FAIL  rast_test"; fail=1; fi; \
	if python3 tools/rpy_lib/minipy2c_test.py >/dev/null 2>&1; then echo "  ok    minipy2c_test (3-way)"; else echo "  FAIL  minipy2c_test"; fail=1; fi; \
	if python3 tools/rpy_lib/minast_native_test.py >/dev/null 2>&1; then echo "  ok    minast_native_test (3-way)"; else echo "  FAIL  minast_native_test"; fail=1; fi; \
	if [ $$fail = 0 ]; then echo "testminipy: PASS"; \
	else echo "testminipy: FAIL"; fi; exit $$fail

# Native self-host regression check, three ways: compile
# tests/fast/selfhost_regressions.c with the native self-hosted binary, with
# gcc, and with the CPython oracle, and require all three exit codes to agree.
# That .c file pins the native-codegen bugs fixed while bootstrapping the
# self-host compiler (array-size deduction, unsigned / suffixed integer
# literals, unsigned comparison operand sizing, pointer compound assignment,
# function-like macros) so none of them can silently regress. Add a new case to
# the file whenever another self-hosting bug is fixed.
#
# Reuses a cached native build in $(SELFHOST_NATIVE_DIR) if present; otherwise
# builds it first (slow, a few minutes).
#     make testfast_native
NATIVE_REG := $(FAST)/selfhost_regressions.c
testfast_native: $(NATIVE_REG)
	@nat="$(SELFHOST_NATIVE_DIR)/shivyc_native"; \
	if [ ! -x "$$nat" ]; then \
	  echo "building native self-host compiler -> $(SELFHOST_NATIVE_DIR) (slow) ..."; \
	  python3 tools/selfhost.py compiler --build-dir $(SELFHOST_NATIVE_DIR) || exit 1; \
	fi; \
	mkdir -p $(FASTBIN); cp $(NATIVE_REG) $(FASTBIN)/reg.c; \
	fail=0; \
	gcc -std=c99 -O0 $(FASTBIN)/reg.c -o $(FASTBIN)/reg_gcc 2>/dev/null \
	  && $(FASTBIN)/reg_gcc >/dev/null 2>&1; gc=$$?; \
	"$$nat" $(FASTBIN)/reg.c -o $(FASTBIN)/reg_native >/dev/null 2>&1 \
	  && $(FASTBIN)/reg_native >/dev/null 2>&1; nt=$$?; \
	python3 -m shivyc.main --no-cache $(FASTBIN)/reg.c -o $(FASTBIN)/reg_oracle >/dev/null 2>&1 \
	  && $(FASTBIN)/reg_oracle >/dev/null 2>&1; oc=$$?; \
	if [ "$$nt" = "$$gc" ] && [ "$$oc" = "$$gc" ]; then \
	  echo "  ok    selfhost_regressions (gcc=$$gc native=$$nt oracle=$$oc)"; \
	else \
	  echo "  FAIL  selfhost_regressions (gcc=$$gc native=$$nt oracle=$$oc)"; fail=1; \
	fi; \
	if [ $$fail = 0 ]; then echo "testfast_native: PASS"; \
	else echo "testfast_native: FAIL"; fi; exit $$fail

# ---------------------------------------------------------------------------
# Promotion behavior-preservation check: compile a set of container-heavy
# programs with PY2C_PROMOTE_CONTAINERS=1 (auto-promote inferred containers to
# the unboxed typed form) and require the result to still match CPython. This
# guards that promotion never changes observable behavior.
#     make testpromote
testpromote:
	@mkdir -p $(FASTBIN)
	@fail=0; \
	chk() { src=$$1; cpy=`python3 $$src >/dev/null 2>&1; echo $$?`; \
	  d=$(FASTBIN)/promo_`basename $$src .py`; rm -rf $$d; mkdir -p $$d; \
	  if ! PY2C_PROMOTE_CONTAINERS=1 python3 tools/py2c.py $$src --out $$d >/dev/null 2>&1; then \
	    echo "  FAIL(transpile) $$src"; fail=1; return; fi; \
	  python3 -c "import sys;sys.path.insert(0,'tools');import py2c;py2c.write_runtime('$$d')" >/dev/null 2>&1; \
	  if ! gcc -std=c99 -I$$d $$d/*.c -o $$d/bin 2>/dev/null; then \
	    echo "  FAIL(gcc) $$src"; fail=1; return; fi; \
	  got=`timeout 30 $$d/bin >/dev/null 2>&1; echo $$?`; \
	  n=`PY2C_PROMOTE_CONTAINERS=1 python3 tools/py2c.py $$src --out $$d 2>&1 >/dev/null | grep -c promoted`; \
	  if [ "$$got" = "$$cpy" ]; then echo "  ok    $$src (==$$cpy, $$n promoted)"; \
	  else echo "  FAIL  $$src (promoted=$$got, cpython=$$cpy)"; fail=1; fi; }; \
	chk $(FAST)/syntax_core.py; \
	chk $(RPY)/promote/app.py; \
	chk $(RPY)/dictops/app.py; \
	chk $(RPY)/wordfreq/app.py; \
	chk $(RPY)/untyped/app.py; \
	if [ $$fail = 0 ]; then echo "testpromote: PASS"; \
	else echo "testpromote: FAIL"; fi; exit $$fail

# ---------------------------------------------------------------------------
# Profile-guided auto-typing behavior-preservation check: compile each program
# both boxed (default) and with -fprofile-generate (profile the run, auto-type
# inferred containers), and require the two binaries to agree. Guards that PGO
# auto-typing never changes observable behavior relative to the boxed build.
#     make testpgo
testpgo:
	@mkdir -p $(FASTBIN)
	@fail=0; \
	build() { src=$$1; flag=$$2; tag=$$3; d=$(FASTBIN)/pgo_$$tag; rm -rf $$d; mkdir -p $$d; \
	  if ! python3 tools/py2c.py $$src $$flag --out $$d >/dev/null 2>&1; then echo ERR; return; fi; \
	  python3 -c "import sys;sys.path.insert(0,'tools');import py2c;py2c.write_runtime('$$d')" >/dev/null 2>&1; \
	  if ! gcc -std=c99 -I$$d $$d/*.c -o $$d/bin 2>/dev/null; then echo ERR; return; fi; \
	  timeout 40 $$d/bin >/dev/null 2>&1; echo $$?; }; \
	chk() { src=$$1; b=`build $$src "" box`; a=`build $$src -fprofile-generate at`; \
	  n=`python3 tools/py2c.py $$src -fprofile-generate --out $(FASTBIN)/pgo_at 2>/dev/null | grep -oE '[0-9]+ profiled \+ [0-9]+ static' | head -1`; \
	  if [ "$$b" = "$$a" ] && [ "$$b" != "ERR" ]; then echo "  ok    $$src (boxed==pgo==$$b; $${n:-no} type(s))"; \
	  else echo "  FAIL  $$src (boxed=$$b pgo=$$a)"; fail=1; fi; }; \
	buildm() { flag=$$1; tag=$$2; shift 2; d=$(FASTBIN)/pgom_$$tag; rm -rf $$d; mkdir -p $$d; \
	  if ! python3 tools/py2c.py "$$@" $$flag --out $$d >/dev/null 2>&1; then echo ERR; return; fi; \
	  python3 -c "import sys;sys.path.insert(0,'tools');import py2c;py2c.write_runtime('$$d')" >/dev/null 2>&1; \
	  if ! gcc -std=c99 -I$$d $$d/*.c -o $$d/bin 2>/dev/null; then echo ERR; return; fi; \
	  timeout 40 $$d/bin >/dev/null 2>&1; echo $$?; }; \
	chkm() { exp=$$1; shift; b=`buildm "" box "$$@"`; a=`buildm -fprofile-generate at "$$@"`; \
	  if [ "$$b" = "$$a" ] && [ "$$b" != "ERR" ]; then echo "  ok    [multi] $$1 ... (boxed==pgo==$$b)"; \
	  else echo "  FAIL  [multi] $$1 ... (boxed=$$b pgo=$$a)"; fail=1; fi; }; \
	chku() { src=$$1; rm -f $(FASTBIN)/prof.json; \
	  g=`build $$src "-fprofile-generate=$(FASTBIN)/prof.json" gen`; \
	  u=`build $$src "-fprofile-use=$(FASTBIN)/prof.json" use`; \
	  if [ "$$g" = "$$u" ] && [ "$$g" != "ERR" ]; then echo "  ok    [use] $$src (generate==use==$$g)"; \
	  else echo "  FAIL  [use] $$src (generate=$$g use=$$u)"; fail=1; fi; }; \
	chk $(RPY)/pgo/app.py; \
	chk $(RPY)/promote/app.py; \
	chk $(RPY)/dictops/app.py; \
	chk $(RPY)/wordfreq/app.py; \
	chk $(RPY)/untyped/app.py; \
	chk $(FAST)/syntax_core.py; \
	chkm 44 $(RPY)/pgo_multi/app.py $(RPY)/pgo_multi/hist.py; \
	chku $(RPY)/pgo/app.py; \
	if [ $$fail = 0 ]; then echo "testpgo: PASS"; \
	else echo "testpgo: FAIL"; fi; exit $$fail

# ---------------------------------------------------------------------------
# NumPy operator-fusion check: a whole-array `out[:]=expr` store lowers to ONE
# loop with no array temporaries. Each fused kernel is checked against an
# explicit manual loop (self-validating sources return 0 on agreement), built
# through both gcc and the ShivyCX self-backend.
#     make testfuse
testfuse:
	@mkdir -p $(FASTBIN)
	@fail=0; \
	gccbuild() { src=$$1; d=$(FASTBIN)/fuse_`basename $$src .py`; rm -rf $$d; mkdir -p $$d; \
	  if ! python3 tools/py2c.py $$src --out $$d >/dev/null 2>&1; then echo ERR; return; fi; \
	  python3 -c "import sys;sys.path.insert(0,'tools');import py2c;py2c.write_runtime('$$d')" >/dev/null 2>&1; \
	  if ! gcc -std=c99 -I$$d $$d/*.c -o $$d/bin -lm 2>/dev/null; then echo ERR; return; fi; \
	  timeout 30 $$d/bin >/dev/null 2>&1; echo $$?; }; \
	sxbuild() { src=$$1; out=$(FASTBIN)/fusesx_`basename $$src .py`; \
	  if ! python3 -m shivyc.main --no-cache $$src -o $$out >/dev/null 2>&1; then echo ERR; return; fi; \
	  timeout 30 $$out >/dev/null 2>&1; echo $$?; }; \
	chk() { src=$$1; exp=$$2; g=`gccbuild $$src`; s=`sxbuild $$src`; \
	  if [ "$$g" = "$$exp" ] && [ "$$s" = "$$exp" ]; then echo "  ok    $$src (gcc==shivycx==$$exp)"; \
	  else echo "  FAIL  $$src (gcc=$$g shivycx=$$s, expected $$exp)"; fail=1; fi; }; \
	chk $(FAST)/fuse_kernels.py 0; \
	chk $(RPY)/numpy/fusion.py 97; \
	if [ $$fail = 0 ]; then echo "testfuse: PASS"; \
	else echo "testfuse: FAIL"; fi; exit $$fail

# ---------------------------------------------------------------------------
# rpy_torch mini-PyTorch check: the bundled library auto-attaches when imported,
# and a trainable MLP (forward + full backprop + SGD, all through the API, every
# elementwise kernel fused) must learn XOR (exit 4) identically under gcc and the
# ShivyCX self-backend.
#     make testtorch
testtorch:
	@mkdir -p $(FASTBIN)
	@fail=0; \
	gccbuild() { src=$$1; d=$(FASTBIN)/torch_`basename $$src .py`; rm -rf $$d; mkdir -p $$d; \
	  if ! python3 tools/py2c.py $$src --out $$d >/dev/null 2>&1; then echo ERR; return; fi; \
	  python3 -c "import sys;sys.path.insert(0,'tools');import py2c;py2c.write_runtime('$$d')" >/dev/null 2>&1; \
	  if ! gcc -std=c99 -I$$d $$d/*.c -o $$d/bin -lm 2>/dev/null; then echo ERR; return; fi; \
	  timeout 30 $$d/bin >/dev/null 2>&1; echo $$?; }; \
	sxbuild() { src=$$1; out=$(FASTBIN)/torchsx_`basename $$src .py`; \
	  if ! python3 -m shivyc.main --no-cache $$src -o $$out >/dev/null 2>&1; then echo ERR; return; fi; \
	  timeout 30 $$out >/dev/null 2>&1; echo $$?; }; \
	chk() { src=$$1; exp=$$2; g=`gccbuild $$src`; s=`sxbuild $$src`; \
	  if [ "$$g" = "$$exp" ] && [ "$$s" = "$$exp" ]; then echo "  ok    $$src (gcc==shivycx==$$exp)"; \
	  else echo "  FAIL  $$src (gcc=$$g shivycx=$$s, expected $$exp)"; fail=1; fi; }; \
	chk $(RPY)/nn/torch_mlp.py 4; \
	chk $(RPY)/nn/torch_mlp_f32.py 4; \
	chk $(RPY)/nn/quant_mlp.py 50; \
	chk $(RPY)/ffi/ffi_math.py 35; \
	if [ $$fail = 0 ]; then echo "testtorch: PASS"; \
	else echo "testtorch: FAIL"; fi; exit $$fail

# ---------------------------------------------------------------------------
# Benchmarks: whole-program SIMD vs gcc -O0/-O2, memory-safety table, etc.
#     make benchmarks
benchmarks:
	cd benchmarks && python3 run_benchmarks.py
	@echo "(also: python3 tools/crust_benchmarks.py [features|rpython|minipy|all])"

# ---------------------------------------------------------------------------
# rpython cross-runtime benchmarks: the same pure-Python programs run under
# CPython, PyPy3, py2c+gcc, and the self-hosted ShivyCX compiler, measuring
# runtime + peak memory + compile time. The harness builds shivyc_native once
# (cached in benchmarks/build_native_bench, or supply one via $SHIVYC_NATIVE).
BENCH_PLOT_DIR ?= /tmp/shivyc_benchmarks

benchmarks_rpython:
	python3 benchmarks/run_rpython_benchmarks.py

# Full report: run the harness, render the matplotlib figures (PNG+PDF), and
# typeset tools/benchmarks.tex into $(BENCH_PLOT_DIR)/benchmarks.pdf.
#
# Every step here is tolerated failing. The report is a collection of
# independent sections, and one of them being unbuildable -- a missing
# toolchain, or a harness that needs the self-hosted native compiler -- should
# cost that section rather than the whole document. `make benchmarks_rpython`
# remains strict, so a regression there is still caught by its own target.
# Fast turnaround for iterating on the compiler: the same report, built from
# reduced measurements. Roughly 3 minutes against 8 for the full target.
#
# Three things make up the difference. The crust suite skips `memory`, whose
# ShivyCX compile alone is ~115s -- over 80% of that suite -- because its
# `[u64; 512]` arrays push the register allocator's interference graph past
# 8000 nodes and the coalescing pass is quadratic in that. The rpython suite
# runs every benchmark at a fraction of its workload rather than dropping any,
# so every backend comparison stays intact. And the feature suite is left out
# entirely, being the slowest step that contributes no timing data.
#
# The figures label themselves as a quick run. Use `make benchmarks_report`
# for numbers worth quoting.
benchmarks_report_fast:
	-python3 benchmarks/run_rpython_benchmarks.py --quick
	-python3 benchmarks/run_minipy_benchmarks.py
	-python3 benchmarks/plot_rpython.py $(BENCH_PLOT_DIR)
	-python3 benchmarks/plot_minipy.py $(BENCH_PLOT_DIR)
	-python3 benchmarks/run_crust_benchmarks.py --quick
	-python3 benchmarks/plot_crust.py $(BENCH_PLOT_DIR)
	-METAMORPHIC_N=6000000 METAMORPHIC_DEPTHS="4 16 24 32 48" \
		python3 benchmarks/run_metamorphic_benchmarks.py
	-python3 benchmarks/plot_metamorphic.py $(BENCH_PLOT_DIR)
	@command -v pdflatex >/dev/null 2>&1 || { \
		echo "pdflatex not found; figures are in $(BENCH_PLOT_DIR)."; \
		exit 0; }
	cp tools/benchmarks.tex tools/crust.tex $(BENCH_PLOT_DIR)/
	cd $(BENCH_PLOT_DIR) && pdflatex -interaction=nonstopmode benchmarks.tex >/dev/null 2>&1 \
		&& pdflatex -interaction=nonstopmode benchmarks.tex >/dev/null 2>&1
	@echo "Benchmark report (QUICK): $(BENCH_PLOT_DIR)/benchmarks.pdf"

benchmarks_report:
	-python3 benchmarks/run_rpython_benchmarks.py
	-python3 benchmarks/run_minipy_benchmarks.py
	-python3 benchmarks/plot_rpython.py $(BENCH_PLOT_DIR)
	-python3 benchmarks/plot_minipy.py $(BENCH_PLOT_DIR)
# The Crust language benchmarks: same generated C, ShivyCX vs gcc, across
# both the Rust and the C++ front ends.
	-python3 benchmarks/run_crust_benchmarks.py
	-python3 benchmarks/plot_crust.py $(BENCH_PLOT_DIR)
# The ShivyCX-vs-gcc feature suite. It writes results/results.json, which
# plot_results.py turns into the two comparison figures; both steps are
# tolerated failing so a missing toolchain does not lose the rest of the
# report.
	-python3 tools/crust_benchmarks.py features
	-python3 benchmarks/plot_results.py
	-cp benchmarks/results/benchmarks.png benchmarks/results/benchmarks2.png \
		$(BENCH_PLOT_DIR)/ 2>/dev/null || true
# The metamorphic-return study: run the self-hosted rasm+rlink microbenchmarks
# and render the two-panel figure. Tolerated failing like the rest.
	-python3 benchmarks/run_metamorphic_benchmarks.py
	-python3 benchmarks/plot_metamorphic.py $(BENCH_PLOT_DIR)
	@command -v pdflatex >/dev/null 2>&1 || { \
		echo "pdflatex not found; figures are in $(BENCH_PLOT_DIR) but the PDF was not built."; \
		exit 0; }
	cp tools/benchmarks.tex tools/crust.tex $(BENCH_PLOT_DIR)/
	cd $(BENCH_PLOT_DIR) && pdflatex -interaction=nonstopmode benchmarks.tex >/dev/null 2>&1 \
		&& pdflatex -interaction=nonstopmode benchmarks.tex >/dev/null 2>&1
	@echo "Benchmark report: $(BENCH_PLOT_DIR)/benchmarks.pdf"

# ---------------------------------------------------------------------------
# Micropython targets

# Clone the objcore micropython fork (or fast-forward an existing checkout),
# then generate the headers its sources need.
install_micropython:
	@if [ -d "$(MPY_DIR)/.git" ]; then \
		echo "Updating micropython in $(MPY_DIR)"; \
		git -C "$(MPY_DIR)" pull --ff-only; \
	else \
		echo "Cloning $(MPY_REPO) into $(MPY_DIR)"; \
		git clone "$(MPY_REPO)" "$(MPY_DIR)"; \
	fi
	$(MAKE) $(MPY_GENHDR)

# Generate micropython's qstr/module headers (required to preprocess its
# sources). micropython generates these before compiling any .c, so the headers
# appear even though the port's gcc build then trips the known hal.c
# warn_unused_result error -- which is why the gcc step's failure is ignored and
# success is verified by the header's presence instead.
$(MPY_GENHDR):
	-$(MAKE) -C $(MPY_PORT)
	@test -f $(MPY_GENHDR) || { \
		echo "ERROR: could not generate micropython headers."; \
		echo "Run 'make install_micropython' first."; exit 1; }

# Compile-check every part of micropython through ShivyCX (one warm PyPy3
# process). Reports a per-file summary; exits non-zero if any file fails.
test_micropython: $(MPY_GENHDR)
	pypy3 tools/mpy_test.py all --mpy-dir $(MPY_DIR) --quiet

# Individual slices, each usable as a standalone regression gate.
test_micropython_core: $(MPY_GENHDR)
	pypy3 tools/mpy_test.py core --mpy-dir $(MPY_DIR) --quiet

test_micropython_objects: $(MPY_GENHDR)
	pypy3 tools/mpy_test.py objects --mpy-dir $(MPY_DIR) --quiet

test_micropython_modules: $(MPY_GENHDR)
	pypy3 tools/mpy_test.py modules --mpy-dir $(MPY_DIR) --quiet

test_micropython_emitters: $(MPY_GENHDR)
	pypy3 tools/mpy_test.py emitters --mpy-dir $(MPY_DIR) --quiet

test_micropython_port: $(MPY_GENHDR)
	pypy3 tools/mpy_test.py port --mpy-dir $(MPY_DIR) --quiet

# Remove the micropython checkout entirely.
clean_micropython:
	rm -rf $(MPY_DIR)

# ---------------------------------------------------------------------------
# CPython targets (minimal, single-threaded, compiled against built-in musl).
# Opt-in and slow; large files are skipped by default (see MAX_KB).

install_cpython:
	@if [ -d "$(CPY_DIR)/.git" ]; then \
		echo "Updating cpython in $(CPY_DIR)"; \
		git -C "$(CPY_DIR)" pull --ff-only; \
	else \
		echo "Cloning $(CPY_REPO) into $(CPY_DIR)"; \
		git clone --depth 1 "$(CPY_REPO)" "$(CPY_DIR)"; \
	fi
	$(MAKE) $(CPY_CONFIG)

# pyconfig.h via configure (minimal build). Threads stay on (modern CPython
# requires them); ShivyCX treats _Thread_local as a plain global, which is
# correct for a single-threaded compile-check.
$(CPY_CONFIG):
	@test -d $(CPY_DIR) || { echo "Run 'make install_cpython' first."; exit 1; }
	cd $(CPY_DIR) && ./configure --without-pymalloc --disable-test-modules >/dev/null
	@test -f $(CPY_CONFIG) || { \
		echo "ERROR: configure did not produce pyconfig.h."; \
		echo "Run 'make install_cpython' first."; exit 1; }

# Compile-check CPython's object model. test_cpython == test_cpython_objects.
test_cpython test_cpython_objects: $(CPY_CONFIG)
	pypy3 tools/bigtest.py $(CPY_DIR) 'Objects/*.c' --musl --quiet \
		--jobs $(JOBS) --max-kb $(MAX_KB) -I . -I Include -I Include/internal \
		$(CPY_DEFS) $(DEFS) $(INCS)

clean_cpython:
	rm -rf $(CPY_DIR)

# ---------------------------------------------------------------------------
# 2.11BSD targets (classic Unix userland, BSD's own headers). Opt-in and slow.

install_bsd:
	@if [ -d "$(BSD_DIR)/.git" ]; then \
		echo "Updating 2.11BSD in $(BSD_DIR)"; \
		git -C "$(BSD_DIR)" pull --ff-only; \
	else \
		echo "Cloning $(BSD_REPO) into $(BSD_DIR)"; \
		git clone --depth 1 "$(BSD_REPO)" "$(BSD_DIR)"; \
	fi
	@# Classic 2.11BSD expects <sys/...> to resolve to the sys/h tree.
	ln -sfn ../sys/h $(BSD_DIR)/include/sys
	@echo "2.11BSD ready in $(BSD_DIR)"

# Compile-check the BSD userland utilities.
test_bsd test_bsd_bin:
	@test -d $(BSD_DIR) || { echo "Run 'make install_bsd' first."; exit 1; }
	pypy3 tools/bigtest.py $(BSD_DIR) 'bin/**/*.c' --quiet \
		--jobs $(JOBS) --max-kb $(MAX_KB) -I include $(DEFS) $(INCS)

test_bsd_usrbin:
	@test -d $(BSD_DIR) || { echo "Run 'make install_bsd' first."; exit 1; }
	pypy3 tools/bigtest.py $(BSD_DIR) 'usr.bin/**/*.c' --quiet \
		--jobs $(JOBS) --max-kb $(MAX_KB) -I include $(DEFS) $(INCS)

clean_bsd:
	rm -rf $(BSD_DIR)

# ---------------------------------------------------------------------------
# Bare-metal demos
#
# Compile a freestanding ShivyCX app and link it against the inlined mini-OS
# (no libc, no CRT) via shivycx_baremetal.py. `--image` additionally wraps the
# kernel in a 64-bit Multiboot boot stub so it can boot under QEMU. Sources live
# in examples/baremetal/; outputs land in build/.
BUILD ?= build

# Build every bare-metal demo (no QEMU needed).
baremetal: baremetal-hello baremetal-kernel baremetal-irq
	@echo "bare-metal images in $(BUILD)/:"; ls -1 $(BUILD)/*.elf

# Freestanding app linked against the mini-OS console.
baremetal-hello: shim
	@mkdir -p $(BUILD)
	pypy3 shivycx_baremetal.py examples/baremetal/hello.c -o $(BUILD)/hello.elf

# Bootable 64-bit image that prints to VGA/serial.
baremetal-kernel: shim
	@mkdir -p $(BUILD)
	pypy3 shivycx_baremetal.py examples/baremetal/kernel.c -o $(BUILD)/kernel.elf --image

# Bootable image with timer + keyboard via the 64-bit IDT.
baremetal-irq: shim
	@mkdir -p $(BUILD)
	pypy3 shivycx_baremetal.py examples/baremetal/kernel_irq.c -o $(BUILD)/irq.elf --image

# gcc-build the inlined 32-bit MiniKraft baseline from minikraft.py.
minikraft: shim
	@mkdir -p $(BUILD)
	pypy3 minikraft.py --build $(BUILD)/minikraft

# Boot the timer+keyboard image under QEMU (serial to stdout).
run-irq: baremetal-irq
	qemu-system-x86_64 -kernel $(BUILD)/irq.elf -serial stdio

# minibrowser on bare-metal VGA: parse HTML -> DOM -> render into a VGA graphics
# framebuffer with a bitmap font, in a freestanding 64-bit long-mode kernel that
# boots with a plain `qemu -kernel` (Multiboot AOUT kludge, no GRUB). Can also
# fetch the page over virtio-net + ARP + UDP. See examples/rpython2c/mbos/.
MBOS := $(RPY)/mbos
mbos:
	$(MAKE) -C $(MBOS)

# Headless self-test: boot mbos, assert the rendered page text, save a shot.
mbos-test:
	$(MAKE) -C $(MBOS) test

# Networked self-test: mbos fetches the page over virtio-net + ARP + UDP from
# a host-side server (minikraft-network-stack concepts, compact polled driver).
mbos-test-net:
	$(MAKE) -C $(MBOS) test-net

# 1920x1080 graphics via a 64 MiB VGA device.
mbos-hires:
	$(MAKE) -C $(MBOS) test-hires

# rpython render path: dom/html/render generated from rpython by py2c and linked
# into the kernel in place of the hand-written C (same dom.py as minibrowser).
mbos-rpython:
	$(MAKE) -C $(MBOS) rpython
mbos-rpython-test:
	$(MAKE) -C $(MBOS) rpython-test
mbos-rpython-test-net:
	$(MAKE) -C $(MBOS) rpython-test-net

self:
	cd tools && pypy3 py2c.py

.PHONY: default test testfast testminipy testfast_native testpromote testpgo testfuse testtorch shim install install_deps clean baremetal baremetal-hello \
        bootstrap bootstrap2 \
        selfhost selfhost_objcore selfhost_bench selfhost_coverage \
        selfhost_coverage_musl selfhost_link selfhost_build selfhost_compiler \
        bench_compile_speed \
        rpython benchmarks wayland rpyqt controls minibrowser \
        baremetal-kernel baremetal-irq minikraft run-irq mbos mbos-test mbos-test-net mbos-hires mbos-rpython mbos-rpython-test mbos-rpython-test-net \
        install_micropython clean_micropython test_micropython \
        test_micropython_core test_micropython_objects \
        test_micropython_modules test_micropython_emitters test_micropython_port \
        install_cpython clean_cpython test_cpython test_cpython_objects \
        install_bsd clean_bsd test_bsd test_bsd_bin test_bsd_usrbin crustos
