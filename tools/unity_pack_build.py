# SPDX-License-Identifier: MIT
"""unity_pack: Building the player: the generated Makefile and the executable, including the
Box2D-Packed glue for 2D physics.

Moved out of tools/unity_pack.py unchanged; unity_pack re-exports every name
here, so `unity_pack.<name>` keeps working."""

from __future__ import annotations
import hashlib
import json
import os
import pickle
import re
import sys
import math
import copy
import tools.cs2cpp as cs2cpp  # noqa: E402

import tools.cs2cpp as cs2cpp  # noqa: E402
from tools.unity_pack_common import *  # noqa: E402,F401,F403
from tools.unity_pack_physics import *  # noqa: E402,F401,F403
from tools.unity_pack_sprites import *  # noqa: E402,F401,F403
from tools.unity_pack_ui import *  # noqa: E402,F401,F403
from tools.unity_pack_anim import *  # noqa: E402,F401,F403
from tools.unity_pack_audio import *  # noqa: E402,F401,F403

__all__ = [
    'build_player_executable',
    'emit_makefile',
    'exe_filename',
]


def emit_makefile(outdir, box2d=False, box2d_inject=False):
    """Makefile for the packed player. With *box2d*, the player links the
    Box2D-Packed glue (physics_box2d.c) and box2d/libbox2d.a, which
    build_player_executable builds with box2d_pack."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = sys.executable
    physics_objs = " physics_box2d.o" if box2d else ""
    physics_libs = " box2d/libbox2d.a -lpthread" if box2d else ""
    physics_rule = ""
    if box2d:
        physics_rule = (
            "physics_box2d.o: physics_box2d.c\n"
            "\t$(CC) -O3 -std=c17 -I box2d/box2d_src/include%s -c -o $@ $<\n"
            % (" -DB2_PACK_INJECTED=1" if box2d_inject else ""))
    # Absolute paths so `make crust-check` works from the outdir.
    head = (
        "# generated — engine.c is cpprust-lowered C (from engine.cpp subset); "
        "data.c / main.c stay C\n"
        "# CC=clang for clang builds; make vectorize-report for loop/SLP miss remarks\n"
        "CC ?= gcc\n"
        "CLANG ?= clang\n"
        "CFLAGS_ENGINE ?= -O3 -fno-math-errno\n"
        "CRUST_ROOT ?= %s\n"
        "CRUST_PY ?= %s\n"
        "CRUST = $(CRUST_PY) -m shivyc.main --no-cache\n"
        ".PHONY: all crust-check vectorize-report clean\n"
        "all: game\n"
        "engine.o: engine.c\n"
        "\t$(CC) $(CFLAGS_ENGINE) -c -o $@ $<\n"
        "data.o: data.c\n"
        "\t$(CC) -O0 -c -o $@ $<\n"
        "main.o: main.c engine_draw.h\n"
        "\t$(CC) -O2 -c -o $@ $<\n"
    ) % (repo, py)
    link = (
        "game: engine.o data.o main.o%s\n"
        "\t$(CC) -O2 -o $@ engine.o data.o main.o%s%s -lm\n"
    ) % (physics_objs, physics_objs, physics_libs)
    tail = (
        "# Clang remarks: which loops miss auto-vectorization (stderr).\n"
        "vectorize-report: engine.c\n"
        "\t$(CLANG) $(CFLAGS_ENGINE) "
        "-Rpass-missed=loop-vectorize,slp-vectorize "
        "-c -o engine.vectorize.o engine.c\n"
        "# Recompile lowered C with crust/shivyc (C++ twins gate at pack time).\n"
        "crust-check: engine.c data.c main.c engine.cpp data.cpp main.cpp\n"
        "\tcd $(CRUST_ROOT) && $(CRUST) -c -D CRUST_NO_POSIX_MKDIR "
        "-o $(CURDIR)/engine.crust.o $(CURDIR)/engine.c\n"
        "\tcd $(CRUST_ROOT) && $(CRUST) -c -D CRUST_NO_POSIX_MKDIR "
        "-o $(CURDIR)/data.crust.o $(CURDIR)/data.c\n"
        "\tcd $(CRUST_ROOT) && $(CRUST) -c "
        "-o $(CURDIR)/main.crust.o $(CURDIR)/main.c\n"
        "clean:\n"
        "\trm -f engine.o data.o main.o game engine.vectorize.o "
        "engine.crust.o data.crust.o main.crust.o%s\n"
    ) % physics_objs
    return head + physics_rule + link + tail


def exe_filename(product):
    """Player binary name: productName plus the platform executable suffix."""
    name = (product or "Player").strip() or "Player"
    name = name.replace("/", "_").replace("\\", "_").replace("\0", "_")
    if sys.platform.startswith("win"):
        for ch in '<>:"|?*':
            name = name.replace(ch, "_")
        if not name.lower().endswith(".exe"):
            name += ".exe"
    return name


def build_player_executable(outdir, product, box2d_root=None, box2d_lto=False):
    """Compile packed C sources and link a player named after productName.

    Prefers examples/unity_pack/gles3_window.c (OpenGL ES 3.1; gles2_window.c
    with UNITY_PACK_GLES2=1) when pkg-config finds glfw3.
    Otherwise links the generated headless main.c.

    ``engine.c`` is the cpprust-lowered C from the ``engine.cpp`` subset
    (``std::vector`` → crust ``vector_int``, …). ``data.c`` / ``main.c`` are
    already C. The host player builds with ``gcc`` (``CC``).

    Object files and the exe are rebuilt only when their inputs are newer
    (or missing), so an incremental pack that left ``.c`` untouched does not
    force a full recompile.
    """
    import subprocess
    cc = os.environ.get("CC") or "gcc"
    exe = os.path.join(outdir, exe_filename(product))
    engine_c = os.path.join(outdir, "engine.c")
    data_c = os.path.join(outdir, "data.c")
    engine_o = os.path.join(outdir, "engine.o")
    data_o = os.path.join(outdir, "data.o")

    def _run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise PackError(
                "player build failed (%s): %s" % (
                    " ".join(cmd[:6]), err or ("exit %d" % r.returncode)))

    def _needs_rebuild(target, *inputs):
        if not os.path.isfile(target):
            return True
        try:
            t_m = os.path.getmtime(target)
        except OSError:
            return True
        for inp in inputs:
            if not inp or not os.path.isfile(inp):
                return True
            try:
                if os.path.getmtime(inp) > t_m:
                    return True
            except OSError:
                return True
        return False

    if _needs_rebuild(engine_o, engine_c):
        _progress("compiling engine.c")
        _run([cc, "-O3", "-fno-math-errno", "-c", "-o", engine_o, engine_c])
    else:
        _progress("engine.o up to date")
    if _needs_rebuild(data_o, data_c):
        _progress("compiling data.c")
        _run([cc, "-O0", "-c", "-o", data_o, data_c])
    else:
        _progress("data.o up to date")

    # Box2D-Packed 2D physics: generated glue + the library
    physics_objs = []
    physics_libs = []
    glue_c = os.path.join(outdir, "physics_box2d.c")
    if os.path.isfile(glue_c):
        b2u = _load_box2d_unity(box2d_root)
        inject = os.path.isfile(os.path.join(outdir, "box2d_inject.json"))
        _progress("building Box2D-Packed%s" % (" (injected)" if inject else ""))
        try:
            lib, inc, defs = b2u.build_library(outdir, inject=inject, lto=box2d_lto)
        except Exception as e:  # box2d_pack.BuildError carries the compiler output
            raise PackError("Box2D-Packed build failed: %s" % e)
        glue_o = os.path.join(outdir, "physics_box2d.o")
        _run([cc, "-O3", "-std=c17", "-c", "-o", glue_o, glue_c, "-I", inc]
             + defs + (["-flto"] if box2d_lto else []))
        physics_objs = [glue_o]
        physics_libs = [lib, "-lpthread"]

    # OpenGL ES 3.1 by default -- what has SSBOs, for `--gpu-handles` --
    # and ES 2.0 for hardware without it (UNITY_PACK_GLES2=1).
    host = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "examples", "unity_pack",
        "gles2_window.c" if os.environ.get("UNITY_PACK_GLES2")
        else "gles3_window.c"))
    use_window = False
    cflags = []
    libs = []
    if os.path.isfile(host):
        try:
            chk = subprocess.run(
                ["pkg-config", "--exists", "glfw3"],
                capture_output=True)
            if chk.returncode == 0:
                cflags = subprocess.check_output(
                    ["pkg-config", "--cflags", "glfw3"], text=True).split()
                libs = subprocess.check_output(
                    ["pkg-config", "--libs", "glfw3"], text=True).split()
                use_window = True
        except (OSError, subprocess.CalledProcessError):
            use_window = False
    if use_window:
        deps = [host, engine_o, data_o] + physics_objs
        render_h = os.path.join(os.path.dirname(host), "gles3_render.h")
        if os.path.isfile(render_h):
            deps.append(render_h)
        if _needs_rebuild(exe, *deps):
            _progress("linking window player %s" % exe)
            _run([cc, "-O2", "-o", exe, host, engine_o, data_o]
                 + physics_objs + ["-I", outdir] + cflags + libs
                 + ["-lGLESv2"] + physics_libs + ["-lm"])
            # (gles3_window.c includes gles3_render.h beside it.)
        else:
            _progress("player up to date")
    else:
        main_c = os.path.join(outdir, "main.c")
        main_o = os.path.join(outdir, "main.o")
        if _needs_rebuild(main_o, main_c):
            _progress("compiling main.c")
            _run([cc, "-O2", "-c", "-o", main_o, main_c])
        else:
            _progress("main.o up to date")
        if _needs_rebuild(exe, engine_o, data_o, main_o, *physics_objs):
            _progress("linking headless player %s" % exe)
            _run([cc, "-O2", "-o", exe, engine_o, data_o, main_o]
                 + physics_objs + physics_libs + ["-lm"])
        else:
            _progress("player up to date")
    return exe
