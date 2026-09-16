#!/usr/bin/env python3
"""Benchmark SoA position upload vs regular C# (Unity-style AoS gather).

Same workload on both sides: N objects, each frame pack positions into a
contiguous float buffer (the CPU side of a GPU upload).

  C#  — array of class instances with x,y,z; gather into float[]
  C AoS — array of structs; gather (Unity/engine default shape)
  C SoA — float pos[N][3] table; contiguous memcpy (unity_pack --soa)

    python3 tools/unity_pack_bench_csharp.py
    python3 tools/unity_pack_bench_csharp.py --n 50000 --iters 5000

Needs: gcc/cc, and `dotnet` (SDK) for the C# leg. Skips C# with a clear
reason if dotnet is missing.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap

_CC = shutil.which("gcc") or shutil.which("cc")
_DOTNET = shutil.which("dotnet")


CS_PROGRAM = r"""
using System;
using System.Diagnostics;

// Unity-shaped: heap objects with position fields packed into a float[].
sealed class Obj {
    public float x, y, z;
    // Extra fields so the gather is not a pure triple of floats in a tiny
    // object — closer to a MonoBehaviour that also carries gameplay state.
    public float rotX, rotY, rotZ;
    public float scaleX, scaleY, scaleZ;
    public int hp;
}

static class Bench {
    const int N = __N__;
    const int Iters = __ITERS__;
    const int Warm = 200;

    static void Main() {
        var objs = new Obj[N];
        for (int i = 0; i < N; i++) {
            objs[i] = new Obj {
                x = i * 0.01f, y = i * 0.02f, z = i * 0.03f,
                rotX = 1, rotY = 0, rotZ = 0,
                scaleX = 1, scaleY = 1, scaleZ = 1,
                hp = i & 7,
            };
        }
        var allpos = new float[N * 3];
        float sink = 0;

        for (int t = 0; t < Warm; t++) {
            Gather(objs, allpos);
            sink += Checksum(allpos);
        }

        var sw = Stopwatch.StartNew();
        for (int t = 0; t < Iters; t++) {
            Gather(objs, allpos);
            sink += Checksum(allpos);
        }
        sw.Stop();

        double ns = sw.Elapsed.TotalNanoseconds / Iters;
        Console.WriteLine(
            "lang=csharp layout=aos_class n={0} iters={1} ns_per={2:F2} sink={3}",
            N, Iters, ns, sink);
    }

    static void Gather(Obj[] objs, float[] allpos) {
        for (int i = 0; i < objs.Length; i++) {
            var o = objs[i];
            int j = i * 3;
            allpos[j] = o.x;
            allpos[j + 1] = o.y;
            allpos[j + 2] = o.z;
        }
    }

    static float Checksum(float[] allpos) {
        float s = 0;
        for (int i = 0; i < allpos.Length; i += 16)
            s += allpos[i];
        return s + allpos[allpos.Length - 1];
    }
}
"""

C_AOS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#define N __N__
#define ITERS __ITERS__
#define WARM 200

struct Obj {
    float x, y, z;
    float rotX, rotY, rotZ;
    float scaleX, scaleY, scaleZ;
    int hp;
};

static volatile float g_sink;

static void gather(const struct Obj *objs, float *allpos) {
    int i;
    for (i = 0; i < N; i++) {
        allpos[i * 3 + 0] = objs[i].x;
        allpos[i * 3 + 1] = objs[i].y;
        allpos[i * 3 + 2] = objs[i].z;
    }
}

static float checksum(const float *allpos) {
    int i;
    float s = 0.f;
    for (i = 0; i < N * 3; i += 16)
        s += allpos[i];
    return s + allpos[N * 3 - 1];
}

int main(void) {
    struct Obj *objs = (struct Obj *)malloc(sizeof(struct Obj) * (size_t)N);
    float *allpos = (float *)malloc(sizeof(float) * (size_t)N * 3u);
    int i, t;
    clock_t t0, t1;
    double secs, ns;
    if (!objs || !allpos) return 2;
    for (i = 0; i < N; i++) {
        objs[i].x = (float)i * 0.01f;
        objs[i].y = (float)i * 0.02f;
        objs[i].z = (float)i * 0.03f;
        objs[i].rotX = 1; objs[i].rotY = 0; objs[i].rotZ = 0;
        objs[i].scaleX = 1; objs[i].scaleY = 1; objs[i].scaleZ = 1;
        objs[i].hp = i & 7;
    }
    for (t = 0; t < WARM; t++) {
        gather(objs, allpos);
        g_sink += checksum(allpos);
    }
    t0 = clock();
    for (t = 0; t < ITERS; t++) {
        gather(objs, allpos);
        g_sink += checksum(allpos);
    }
    t1 = clock();
    secs = (double)(t1 - t0) / (double)CLOCKS_PER_SEC;
    ns = secs * 1e9 / (double)ITERS;
    printf("lang=c layout=aos_struct n=%d iters=%d ns_per=%.2f sink=%g\n",
           N, ITERS, ns, (double)g_sink);
    free(objs);
    free(allpos);
    return 0;
}
"""

C_SOA = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define N __N__
#define ITERS __ITERS__
#define WARM 200

/* Contiguous position table — unity_pack --soa shape. */
static float pos[N][3];
static volatile float g_sink;

static void upload(float *allpos) {
    memcpy(allpos, &pos[0][0], sizeof(float) * (size_t)N * 3u);
}

static float checksum(const float *allpos) {
    int i;
    float s = 0.f;
    for (i = 0; i < N * 3; i += 16)
        s += allpos[i];
    return s + allpos[N * 3 - 1];
}

int main(void) {
    float *allpos = (float *)malloc(sizeof(float) * (size_t)N * 3u);
    int i, t;
    clock_t t0, t1;
    double secs, ns;
    if (!allpos) return 2;
    for (i = 0; i < N; i++) {
        pos[i][0] = (float)i * 0.01f;
        pos[i][1] = (float)i * 0.02f;
        pos[i][2] = (float)i * 0.03f;
    }
    for (t = 0; t < WARM; t++) {
        upload(allpos);
        g_sink += checksum(allpos);
    }
    t0 = clock();
    for (t = 0; t < ITERS; t++) {
        upload(allpos);
        g_sink += checksum(allpos);
    }
    t1 = clock();
    secs = (double)(t1 - t0) / (double)CLOCKS_PER_SEC;
    ns = secs * 1e9 / (double)ITERS;
    printf("lang=c layout=soa_table n=%d iters=%d ns_per=%.2f sink=%g\n",
           N, ITERS, ns, (double)g_sink);
    free(allpos);
    return 0;
}
"""


def _sub(template: str, n: int, iters: int) -> str:
    return template.replace("__N__", str(n)).replace("__ITERS__", str(iters))


def _parse_ns(line: str) -> float:
    m = re.search(r"ns_per=([0-9.]+)", line)
    if not m:
        raise RuntimeError("no ns_per in: %r" % line)
    return float(m.group(1))


def run_c(src: str, n: int, iters: int, tag: str) -> str:
    if not _CC:
        raise SystemExit("need gcc/cc")
    d = tempfile.mkdtemp(prefix="soa-vs-cs-%s-" % tag)
    path = os.path.join(d, "bench.c")
    with open(path, "w") as f:
        f.write(_sub(src, n, iters))
    exe = os.path.join(d, "bench")
    subprocess.check_call(
        [_CC, "-O3", "-o", exe, path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return subprocess.check_output([exe], text=True).strip()


def run_csharp(n: int, iters: int) -> str:
    if not _DOTNET:
        raise RuntimeError("dotnet not found")
    d = tempfile.mkdtemp(prefix="soa-vs-cs-csharp-")
    with open(os.path.join(d, "Bench.csproj"), "w") as f:
        f.write(textwrap.dedent("""\
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup>
                <OutputType>Exe</OutputType>
                <TargetFramework>net10.0</TargetFramework>
                <ImplicitUsings>disable</ImplicitUsings>
                <Nullable>disable</Nullable>
                <Optimize>true</Optimize>
              </PropertyGroup>
            </Project>
            """))
    with open(os.path.join(d, "Program.cs"), "w") as f:
        f.write(_sub(CS_PROGRAM, n, iters))
    subprocess.check_call(
        [_DOTNET, "build", "-c", "Release", "--nologo", "-v", "q"],
        cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    out = subprocess.check_output(
        [_DOTNET, "run", "-c", "Release", "--no-build", "--nologo"],
        cwd=d, text=True)
    return out.strip().splitlines()[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=10000,
                    help="instance count (default 10000)")
    ap.add_argument("--iters", type=int, default=10000,
                    help="timed iterations (default 10000)")
    args = ap.parse_args()
    n, iters = args.n, args.iters
    if n < 1 or n > 65535:
        sys.stderr.write("--n must be 1..65535 (index-friendly bound)\n")
        return 2

    print("position upload bench: N=%d iters=%d" % (n, iters))
    print("(gather/copy into contiguous float[N*3] — CPU side of a GPU upload)")
    print("")

    rows = []
    line = run_c(C_AOS, n, iters, "caos")
    print(line)
    rows.append(("C AoS struct gather", _parse_ns(line)))

    line = run_c(C_SOA, n, iters, "csoa")
    print(line)
    rows.append(("C SoA table memcpy", _parse_ns(line)))

    if _DOTNET:
        try:
            line = run_csharp(n, iters)
            print(line)
            rows.append(("C# class AoS gather", _parse_ns(line)))
        except (subprocess.CalledProcessError, RuntimeError) as e:
            print("C# skipped: %s" % e, file=sys.stderr)
    else:
        print("C# skipped: dotnet not on PATH", file=sys.stderr)

    print("")
    base = next((ns for name, ns in rows if "C# class" in name), None)
    soa = next((ns for name, ns in rows if "SoA" in name), None)
    aos = next((ns for name, ns in rows if "AoS struct" in name), None)
    if base and soa and soa > 0:
        print("SoA vs C# class: %.2fx faster (%.2f ns vs %.2f ns per upload)"
              % (base / soa, soa, base))
    if aos and soa and soa > 0:
        print("SoA vs C AoS:   %.2fx faster (%.2f ns vs %.2f ns per upload)"
              % (aos / soa, soa, aos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
