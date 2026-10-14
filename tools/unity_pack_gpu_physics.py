"""unity_pack --gpu-physics: 2D physics on the gpu-physics-engine circle solver, in place of Box2D-Packed.

The engine (https://github.com/crustos/gpu-physics-engine) ships unity_glue.py, which generates
physics_gpu.c -- the same engine_box2d_* contract physics_box2d.c meets -- over runtime/gpuphys.c,
the engine's Crust-translated GPU kernels run on the CPU. This module finds the checkout, stamps the
pack with its contents, and turns a scene it cannot simulate into a PackError.
"""
import hashlib
import importlib.util
import os

GPU_PHYSICS_ENV = "GPU_PHYSICS_ROOT"
# object files of the generated gpuphys/ directory (gpuphys.c + the four translated kernel files)
OBJECTS = ["gpuphys.o", "gpk_grid.o", "gpk_collision_cell_builder.o", "gpk_prowl_world.o", "gpk_prowl_bodies.o"]


def find_root(root=None):
    """The gpu-physics-engine checkout: *root* (when a path), $GPU_PHYSICS_ROOT, or ../gpu-physics-engine."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [root if isinstance(root, str) else None, os.environ.get(GPU_PHYSICS_ENV),
             os.path.join(os.path.dirname(here), "gpu-physics-engine")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "runtime", "gpuphys.c")) and \
                os.path.isfile(os.path.join(c, "unity_glue.py")):
            return os.path.abspath(c)
    from tools.unity_pack import PackError
    raise PackError("--gpu-physics needs a gpu-physics-engine checkout with unity_glue.py "
                    "(--gpu-physics PATH, $%s, or ../gpu-physics-engine)" % GPU_PHYSICS_ENV)


def fingerprint(root=None):
    """A hash of the engine files that decide the generated code, for the pack stamp."""
    base = find_root(root)
    h = hashlib.sha256()
    files = ["unity_glue.py", "runtime/gpuphys.c", "runtime/gpuphys.h", "runtime/gpk.py"]
    kdir = os.path.join(base, "kernels")
    files += ["kernels/" + f for f in sorted(os.listdir(kdir)) if f.endswith(".rs")]
    for f in files:
        try:
            with open(os.path.join(base, f), "rb") as fh:
                h.update(f.encode() + b"\0" + fh.read())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _load_glue(base):
    import sys
    rt = os.path.join(base, "runtime")
    if rt not in sys.path:
        sys.path.insert(0, rt)
    spec = importlib.util.spec_from_file_location("gpu_unity_glue", os.path.join(base, "unity_glue.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def emit(outdir, plan, root=None):
    """Write physics_gpu.c and gpuphys/ into *outdir* for *plan*."""
    from tools.unity_pack import PackError
    base = find_root(root)
    glue = _load_glue(base)
    crust = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        return glue.emit_glue(outdir, plan, gpu_root=base, crust=crust)
    except glue.GpuPhysicsError as e:
        raise PackError(str(e))
