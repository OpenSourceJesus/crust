#!/usr/bin/env python3
"""
shivycx_baremetal.py - link a ShivyCX-compiled C application against the
minikraft mini-OS, pulling in *only* the OS pieces the application actually
needs, and producing a freestanding (no libc / no crt) binary.

How it works
------------
1. The application is compiled to an object file by ShivyCX itself
   (``python -m shivyc.main app.c -c``).
2. We read the application object's *undefined* symbols and satisfy them from
   minikraft by transitive closure: each minikraft source that defines a needed
   symbol is pulled in, and that source's own undefined symbols are then
   resolved the same way. This assembles just the parts of the OS the
   application requires -- nothing more.
3. The application object and the selected OS objects are linked with ``ld`` in
   freestanding mode.

The OS sources live entirely inside ``minikraft.py`` as embedded strings; this
module never touches the original minikraft checkout.

Architecture note
-----------------
ShivyCX emits 64-bit x86-64 objects, so the application and the OS pieces it
links against are built at 64-bit here. minikraft's *boot* path (boot.S, pvh.S,
linker.ld) is 32-bit multiboot, so producing a multiboot-bootable image still
requires resolving the 32/64 split (see notes at the bottom of this file). The
app<->OS link path below is independent of that and works today.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import minikraft as mk

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Compile flags for the OS pieces, mirrored from minikraft's build but width
# parameterised. We deliberately drop -m32 so the objects match ShivyCX output.
_BASE_CFLAGS = [
    "-Ofast",
    "-ffreestanding",
    "-fno-stack-protector",
    "-fno-pic",
    "-mno-red-zone",
    "-std=c11",
    # minikraft wraps every console_putchar/console_puts body in
    # `#ifdef ENABLE_LOGGING`. Without this the console links but does
    # nothing, so a "prints to VGA/serial" demo boots silently.
    "-DENABLE_LOGGING",
]


def _tool(name):
    for prefix in ("x86_64-linux-gnu-", "x86_64-elf-", ""):
        if shutil.which(prefix + name):
            return prefix + name
    raise RuntimeError("no %s found in PATH" % name)


class BareMetalLinker:
    """Assembles and links a user app against the minimal required slice of
    minikraft."""

    def __init__(self, bits=64, workdir=None, bare_metal=True, verbose=True):
        self.bits = bits
        self.bare_metal = bare_metal
        self.verbose = verbose
        self._owns_workdir = workdir is None
        self.workdir = workdir or tempfile.mkdtemp(prefix="shivycx_bm_")
        self.srcroot = os.path.join(self.workdir, "mk")
        self.objdir = os.path.join(self.workdir, "obj")
        os.makedirs(self.objdir, exist_ok=True)
        # symbol -> minikraft relpath that defines it
        self._provider = {}
        # minikraft relpath -> compiled object path
        self._obj = {}
        # minikraft relpath -> set of undefined symbols it still needs
        self._undef = {}
        self._pool_ready = False
        self._uncompilable = []

    # -- logging ---------------------------------------------------------
    def _log(self, *a):
        if self.verbose:
            print("[shivycx-bm]", *a)

    # -- the minikraft symbol pool --------------------------------------
    def _cflags(self):
        flags = list(_BASE_CFLAGS)
        flags.append("-m64" if self.bits == 64 else "-m32")
        return flags

    def _defs(self):
        return list(mk.BARE_METAL_DEFS if self.bare_metal else mk.DEFS)

    def _build_pool(self):
        """Compile every minikraft .c that builds at the target width and index
        the symbols each one provides. Sources that don't compile at this width
        (e.g. width-specific code) are skipped and reported."""
        if self._pool_ready:
            return
        mk.write_sources(self.srcroot)
        gcc = _tool("gcc")
        incdir = os.path.join(self.srcroot, mk.INCLUDE_DIR)
        nm = _tool("nm")

        for rel in mk.MINIKRAFT_SOURCES:
            if not rel.endswith(".c"):
                continue
            if os.path.basename(rel) in mk.EXCLUDE_FROM_BUILD:
                continue
            src = os.path.join(self.srcroot, rel)
            obj = os.path.join(self.objdir, rel.replace("/", "__")[:-2] + ".o")
            cmd = [gcc] + self._cflags() + ["-I", incdir, "-c", src, "-o", obj] + self._defs()
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0 or not os.path.exists(obj):
                self._uncompilable.append(rel)
                continue
            self._obj[rel] = obj
            self._provider.update({s: rel for s in _defined_syms(obj, nm)})
            self._undef[rel] = _undef_syms(obj, nm)

        self._pool_ready = True
        self._log("symbol pool: %d objects, %d symbols (%d sources skipped at %d-bit)"
                  % (len(self._obj), len(self._provider), len(self._uncompilable), self.bits))

    # -- app compilation via ShivyCX ------------------------------------
    def compile_app(self, app_c, app_obj=None):
        """Compile the user application with ShivyCX into an object file."""
        if self.bits != 64:
            raise RuntimeError(
                "ShivyCX emits 64-bit x86-64 only; cannot compile the app at "
                "%d-bit. Provide a prebuilt object or use bits=64." % self.bits)
        app_obj = app_obj or os.path.join(self.workdir, "app.o")
        # Let the app resolve `#include "console.h"` etc. against the OS headers.
        self._build_pool()
        inc_kernel = os.path.join(self.srcroot, "src", "kernel")
        inc_include = os.path.join(self.srcroot, mk.INCLUDE_DIR)
        # ShivyCX writes its intermediate .s/.o next to the *input* source, so
        # compile a copy inside the work dir to avoid polluting the user's tree.
        app_copy = os.path.join(self.workdir, os.path.basename(app_c))
        if os.path.abspath(app_copy) != os.path.abspath(app_c):
            shutil.copyfile(app_c, app_copy)
        # The copy above moves the source away from its own directory, which
        # silently breaks any `#include "..."` the app makes relative to
        # itself. Put the original directory back on the include path so a
        # multi-file application still resolves.
        inc_app = os.path.dirname(os.path.abspath(app_c))
        cmd = [sys.executable, "-m", "shivyc.main", app_copy,
               "-c", "-o", app_obj, "-I", inc_kernel, "-I", inc_include,
               "-I", inc_app]
        self._log("compiling app with ShivyCX:", os.path.basename(app_c))
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(app_obj):
            raise RuntimeError("ShivyCX failed:\n" + (r.stdout or "") + (r.stderr or ""))
        return app_obj

    # -- dependency closure ---------------------------------------------
    def _closure(self, seed_undef, already_provided):
        """Resolve ``seed_undef`` against the OS pool by transitive closure.

        ``already_provided`` is the set of symbols defined by objects that are
        always linked in (the app, and the boot stub for bootable images), so
        they are never treated as missing. Returns (objects, sources, missing).
        """
        self._build_pool()
        worklist = list(seed_undef)
        selected = []
        seen_sym = set()
        missing = set()
        while worklist:
            sym = worklist.pop()
            if sym in seen_sym:
                continue
            seen_sym.add(sym)
            if sym in already_provided:
                continue
            rel = self._provider.get(sym)
            if rel is None:
                missing.add(sym)
                continue
            if rel not in selected:
                selected.append(rel)
                worklist.extend(self._undef.get(rel, ()))
        objs = [self._obj[rel] for rel in selected]
        return objs, selected, missing

    def required_objects(self, app_obj):
        """Objects/sources needed to satisfy a single app object's undefined
        symbols (non-bootable freestanding link)."""
        nm = _tool("nm")
        return self._closure(_undef_syms(app_obj, nm), set())

    # -- linking ---------------------------------------------------------
    def link(self, app_obj, os_objs, out_elf, entry="_start", linker_script=None):
        """Freestanding link: no crt, no libc."""
        ld = _tool("ld")
        cmd = [ld]
        if self.bits == 32:
            cmd += ["-m", "elf_i386"]
        if linker_script:
            cmd += ["-T", linker_script]
        else:
            cmd += ["-e", entry]
        cmd += ["-o", out_elf, app_obj] + os_objs
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out_elf):
            raise RuntimeError("link failed:\n" + (r.stdout or "") + (r.stderr or ""))
        return out_elf

    # -- one-shot --------------------------------------------------------
    def build(self, app_c, out_elf, entry="_start", linker_script=None):
        """Full pipeline: compile app -> resolve OS pieces -> link."""
        app_obj = self.compile_app(app_c)
        os_objs, selected, missing = self.required_objects(app_obj)
        if missing:
            raise RuntimeError(
                "unresolved symbols (not provided by minikraft at %d-bit): %s"
                % (self.bits, ", ".join(sorted(missing))))
        self._log("app pulled in %d OS piece(s): %s"
                  % (len(selected), ", ".join(os.path.basename(s) for s in selected) or "(none)"))
        self.link(app_obj, os_objs, out_elf, entry=entry, linker_script=linker_script)
        self._log("wrote", out_elf, "(%d bytes)" % os.path.getsize(out_elf))
        return out_elf, selected

    def close(self):
        if self._owns_workdir and os.path.isdir(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)

    # Defined by kernel64.ld rather than by any translation unit.
    _LINKER_PROVIDED = ("_load_start", "_load_end", "_bss_end")

    # -- bootable image (boot64.S + kernel64.ld) ------------------------
    def _install_idt64_overrides(self):
        """Swap the 32-bit idt.c out of the symbol pool and swap in the 64-bit
        long-mode IDT (idt64.c + idt64.S) embedded in minikraft.py. This is
        what unlocks kernel_main / keyboard / timer at 64-bit."""
        if getattr(self, "_idt64_installed", False):
            return
        gcc = _tool("gcc")
        nm = _tool("nm")
        files = mk.write_baremetal64(self.srcroot)
        incdir = os.path.join(self.srcroot, mk.INCLUDE_DIR)
        kdir = os.path.join(self.srcroot, "src", "kernel")

        # Drop every symbol the 32-bit idt.c used to provide.
        for sym, rel in list(self._provider.items()):
            if os.path.basename(rel) == "idt.c":
                del self._provider[sym]
        self._obj.pop("src/kernel/idt.c", None)
        self._undef.pop("src/kernel/idt.c", None)

        # Compile idt64.c and assemble idt64.S, then register them.
        c_obj = os.path.join(self.objdir, "idt64_c.o")
        cmd = ([gcc] + self._cflags() + ["-I", kdir, "-I", incdir, "-c",
                files["src/kernel/idt64.c"], "-o", c_obj] + self._defs())
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(c_obj):
            raise RuntimeError("idt64.c compile failed:\n" + r.stderr)
        s_obj = os.path.join(self.objdir, "idt64_s.o")
        self._assemble(files["src/kernel/idt64.S"], s_obj)

        for rel, obj in (("src/kernel/idt64.c", c_obj), ("src/kernel/idt64.S", s_obj)):
            self._obj[rel] = obj
            self._undef[rel] = _undef_syms(obj, nm)
            self._provider.update({s: rel for s in _defined_syms(obj, nm)})
        self._idt64_installed = True
        self._log("installed 64-bit IDT overrides (idt64.c + idt64.S; idt.c removed)")

    def _assemble(self, src, obj):
        as_tool = _tool("as")
        flag = "--64" if self.bits == 64 else "--32"
        r = subprocess.run([as_tool, flag, "-o", obj, src],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(obj):
            raise RuntimeError("assembling %s failed:\n%s" % (src, r.stderr))
        return obj

    def build_image(self, app_c, out_elf):
        """Produce a Multiboot-loadable 64-bit kernel image: ShivyCX app +
        boot64.S + only the minikraft pieces the app needs, linked with
        kernel64.ld.

        The application must provide ``void kmain(...)`` (boot64.S calls it).
        Returns (out_elf, selected_sources).
        """
        if self.bits != 64:
            raise RuntimeError("bootable images are 64-bit; boot64.S targets long mode")
        self._build_pool()
        self._install_idt64_overrides()
        nm = _tool("nm")

        # boot stub + linker script from minikraft.py
        boot_files = mk.write_baremetal64(self.srcroot)
        boot_src = boot_files["src/boot/boot64.S"]
        linker_script = boot_files["src/kernel/kernel64.ld"]
        boot_obj = os.path.join(self.objdir, "boot64.o")
        self._assemble(boot_src, boot_obj)

        # app
        app_obj = self.compile_app(app_c)

        # closure over (app + boot) undefined symbols; app/boot define kmain,
        # _start, etc., so those are not "missing".
        always = [app_obj, boot_obj]
        # Symbols the *linker script* defines, not any object: boot64.S's
        # Multiboot AOUT-kludge header references the image layout, which only
        # exists once ld has laid it out. They are not missing, so they must
        # not be reported as such.
        provided = set(self._LINKER_PROVIDED)
        seed = set()
        for o in always:
            provided.update(_defined_syms(o, nm))
            seed.update(_undef_syms(o, nm))
        os_objs, selected, missing = self._closure(seed, provided)
        if missing:
            raise RuntimeError(
                "unresolved symbols (not provided by app/boot/minikraft at "
                "64-bit): %s" % ", ".join(sorted(missing)))
        self._log("image pulls in %d OS piece(s): %s"
                  % (len(selected),
                     ", ".join(os.path.basename(s) for s in selected) or "(none)"))

        # freestanding link with the kernel linker script (ENTRY=_start).
        ld = _tool("ld")
        cmd = [ld, "-T", linker_script, "-o", out_elf, boot_obj, app_obj] + os_objs
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out_elf):
            raise RuntimeError("link failed:\n" + (r.stdout or "") + (r.stderr or ""))
        self._log("wrote bootable image", out_elf,
                  "(%d bytes)" % os.path.getsize(out_elf))
        return out_elf, selected


# -- nm helpers ----------------------------------------------------------
def _defined_syms(obj, nm="nm"):
    out = subprocess.run([nm, "--defined-only", "-g", obj],
                         capture_output=True, text=True).stdout
    syms = []
    for line in out.splitlines():
        parts = line.split()
        # "<addr> <type> <name>"  (type is T/D/R/B/W ...)
        if len(parts) >= 3:
            syms.append(parts[-1])
    return syms


def _undef_syms(obj, nm="nm"):
    out = subprocess.run([nm, "-u", obj], capture_output=True, text=True).stdout
    syms = set()
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        # "U <name>"  or just "<name>" depending on nm; weak undefined "w" ignored
        if parts[0] in ("U",) and len(parts) >= 2:
            syms.add(parts[1])
        elif len(parts) == 1:
            syms.add(parts[0])
    return syms


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Link a ShivyCX-compiled app against minikraft (bare-metal).")
    ap.add_argument("app", help="C source file for the application")
    ap.add_argument("-o", "--output", default="a.elf", help="output ELF")
    ap.add_argument("--entry", default="_start", help="entry symbol (default _start)")
    ap.add_argument("--bits", type=int, default=64, choices=(32, 64))
    ap.add_argument("--linker-script", default=None,
                    help="optional linker script (e.g. a 64-bit kernel.ld)")
    ap.add_argument("--image", action="store_true",
                    help="produce a Multiboot-bootable image using the embedded "
                         "boot64.S + kernel64.ld (app must provide kmain())")
    ap.add_argument("--keep", action="store_true", help="keep the work dir")
    args = ap.parse_args(argv)

    bm = BareMetalLinker(bits=args.bits)
    try:
        if args.image:
            out, selected = bm.build_image(args.app, args.output)
        else:
            out, selected = bm.build(args.app, args.output, entry=args.entry,
                                     linker_script=args.linker_script)
        print("built:", out)
        print("OS pieces linked in:",
              ", ".join(os.path.basename(s) for s in selected) or "(none)")
    finally:
        if not args.keep:
            bm.close()


if __name__ == "__main__":
    main()
