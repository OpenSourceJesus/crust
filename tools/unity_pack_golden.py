"""Byte-for-byte gate for moving unity_pack's script lowering onto cs2cpp.

unity_pack's own C# translator is being replaced, one rewrite family at a
time, by `tools/cs2cpp.py`. Each step must leave the packed output exactly
as it was -- or change it on purpose, and then show precisely where. This
records what the packer emits for a corpus of projects and checks it again.

    python3 tools/unity_pack_golden.py record   # run the unity tests, record
    python3 tools/unity_pack_golden.py check    # re-pack every case, compare
    python3 tools/unity_pack_golden.py check -v # ... and print the diffs

The corpus is every `unity_pack.pack(..)` call `tests/test_unity_pack.py`
makes: the tests author small projects of their own, so recording them
captures the families they exercise with no fixture written twice. Each
case keeps its input files, the options, and a sha256 of `engine.cpp`,
`data.cpp` and `main.cpp` -- the text the packer emits, before cpprust and
shivyc validate it (that step does not change the text, and skipping it is
what makes a check take seconds rather than an hour). A case that failed
keeps its error message instead, and is replayed with validation on, since
that may be where it failed.

Committed: `tests/unity_golden/corpus.json`. Full outputs are cached in
`$TMPDIR/unity_golden_cache` for diffs. A project under
`examples/unity_pack/SystemsScene` -- whose scene is not in the repository
-- is recorded to `local.json` beside the cache instead, and checked when
present.
"""

import contextlib
import difflib
import hashlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools.unity_pack as unity_pack  # noqa: E402

CORPUS = os.path.join(ROOT, "tests", "unity_golden", "corpus.json")
CACHE = os.path.join(tempfile.gettempdir(), "unity_golden_cache")
LOCAL = os.path.join(CACHE, "local.json")
OUTPUTS = ("engine.cpp", "data.cpp", "main.cpp")
_LOCAL_ROOTS = (os.path.join(ROOT, "examples", "unity_pack", "SystemsScene"),)


def _snapshot(root):
    """Every file under `root`: text as str, anything else base64."""
    files = {}
    for d, _dirs, names in os.walk(root):
        for n in sorted(names):
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            data = open(p, "rb").read()
            try:
                files[rel] = data.decode("utf-8")
            except UnicodeDecodeError:
                import base64
                files[rel] = {"b64": base64.b64encode(data).decode("ascii")}
    return files


def _materialize(files, root):
    import base64
    for rel, content in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if isinstance(content, dict):
            open(p, "wb").write(base64.b64decode(content["b64"]))
        else:
            open(p, "w", encoding="utf-8", newline="").write(content)


def _normalize(text, root):
    """The output with the project's own path taken out of it."""
    return text.replace(os.path.realpath(root), "<ROOT>").replace(root, "<ROOT>")


def _outputs(outdir, root):
    out = {}
    for name in OUTPUTS:
        p = os.path.join(outdir, name)
        if os.path.isfile(p):
            out[name] = _normalize(open(p, encoding="utf-8").read(), root)
    return out


def _digest(outs):
    return dict((k, hashlib.sha256(v.encode("utf-8")).hexdigest())
                for k, v in sorted(outs.items()))


def _test_name():
    for fr in inspect.stack():
        if fr.function.startswith("test"):
            cls = fr.frame.f_locals.get("self")
            return "%s.%s" % (type(cls).__name__ if cls else "?", fr.function)
    return "?"


def _pack_case(files, opts, validate, name="project"):
    """Re-pack a recorded project; (outputs, error).

    Into a directory of the recorded project's own name: with no
    `productName` the packer names the product after its directory, so a
    fresh temp name changed `data.cpp` on every replay."""
    parent = tempfile.mkdtemp(prefix="ugold-")
    root = os.path.join(parent, name)
    os.makedirs(root)
    outdir = tempfile.mkdtemp(prefix="ugold-out-")
    real = unity_pack.validate_emitted_c
    try:
        _materialize(files, root)
        if not validate:
            unity_pack.validate_emitted_c = lambda *a, **k: None
        err = None
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                unity_pack.pack(root, outdir, **opts)
            except unity_pack.PackError as e:
                err = _normalize(e.message, root)
        return _outputs(outdir, root), err
    finally:
        unity_pack.validate_emitted_c = real
        shutil.rmtree(parent, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)


def record():
    """Run the unity tests with `pack` wrapped; write the corpus."""
    cases, local = {}, {}
    real_pack = unity_pack.pack
    counter = {}

    def recording_pack(root, outdir, *args, **kw):
        names = ("soa", "soa_vec4", "force", "strict")
        opts = dict(zip(names, args))
        opts.update(kw)
        opts.pop("force", None)
        files = _snapshot(root)
        test = _test_name()
        counter[test] = counter.get(test, 0) + 1
        cid = "%s#%d" % (test, counter[test])
        err = None
        try:
            return real_pack(root, outdir, *args, **kw)
        except unity_pack.PackError as e:
            err = _normalize(e.message, root)
            raise
        finally:
            outs = _outputs(outdir, root)
            case = {"files": files, "opts": opts, "error": err,
                    "out": _digest(outs), "name": os.path.basename(
                        os.path.normpath(root))}
            real_root = os.path.realpath(root)
            is_local = any(real_root.startswith(os.path.realpath(r))
                           for r in _LOCAL_ROOTS)
            (local if is_local else cases)[cid] = case
            _cache(cid, outs)

    unity_pack.pack = recording_pack
    # The emitted text does not depend on cpprust + shivyc validating it,
    # and skipping that is most of the recording's time. (A test that
    # swaps in its own validator still gets it: it patches over this.)
    real_validate = unity_pack.validate_emitted_c
    unity_pack.validate_emitted_c = lambda *a, **k: None
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(
            "tests.test_unity_pack")
        unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    finally:
        unity_pack.pack = real_pack
        unity_pack.validate_emitted_c = real_validate
    # Keep only what reproduces: a test may patch unity_pack for its own
    # purpose, and a case that does not replay the same on this very tree
    # would only ever report noise.
    kept = {}
    dropped = []
    for store, into in ((cases, kept), (local, None)):
        for cid, case in sorted(store.items()):
            outs, err = _pack_case(case["files"], case["opts"],
                                   validate=case["error"] is not None,
                                   name=case["name"])
            if _digest(outs) == case["out"] and err == case["error"]:
                if into is not None:
                    into[cid] = case
            else:
                dropped.append(cid)
                if into is None:
                    local.pop(cid)
    os.makedirs(os.path.dirname(CORPUS), exist_ok=True)
    with open(CORPUS, "w") as f:
        json.dump(kept, f, indent=1, sort_keys=True)
        f.write("\n")
    os.makedirs(CACHE, exist_ok=True)
    # Without the local projects (SystemsScene's scene is not in the
    # repository) their tests skip and record nothing: keep what an earlier
    # recording stored rather than wiping it. And say that the committed
    # cases those tests make after packing SystemsScene are missing too.
    have_local = any(os.path.isfile(os.path.join(r, "Assets", "Scenes",
                                                 "Systems.unity"))
                     for r in _LOCAL_ROOTS)
    if have_local:
        with open(LOCAL, "w") as f:
            json.dump(local, f, indent=1, sort_keys=True)
    else:
        print("note: SystemsScene is not present; its local cases were kept "
              "as recorded, and the tests that pack it did not run -- record "
              "with it present for a complete corpus")
    print("recorded %d cases (%d local-only), dropped %d that do not replay"
          % (len(kept), len(local), len(dropped)))
    for cid in dropped:
        print("  dropped: %s" % cid)
    return 0


def _cache(cid, outs):
    d = os.path.join(CACHE, "out", cid.replace("/", "_"))
    os.makedirs(d, exist_ok=True)
    for name, text in outs.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(text)


def check(verbose=False, only=None):
    """Re-pack every case; report any whose output changed."""
    corpus = json.load(open(CORPUS))
    if os.path.isfile(LOCAL):
        corpus.update(json.load(open(LOCAL)))
    changed = 0
    for cid, case in sorted(corpus.items()):
        if only and only not in cid:
            continue
        outs, err = _pack_case(case["files"], case["opts"],
                               validate=case["error"] is not None,
                               name=case["name"])
        got = _digest(outs)
        if got == case["out"] and err == case["error"]:
            continue
        changed += 1
        print("CHANGED %s" % cid)
        if err != case["error"]:
            print("  error was: %s\n  error now: %s" % (case["error"], err))
        for name in OUTPUTS:
            if got.get(name) == case["out"].get(name):
                continue
            print("  %s differs" % name)
            if verbose:
                old_p = os.path.join(CACHE, "out", cid.replace("/", "_"), name)
                old = open(old_p).read() if os.path.isfile(old_p) else ""
                for line in difflib.unified_diff(
                        old.splitlines(), outs.get(name, "").splitlines(),
                        "recorded/" + name, "now/" + name, n=2, lineterm=""):
                    print("    " + line)
    print("%d of %d cases unchanged" % (len(corpus) - changed, len(corpus)))
    return 1 if changed else 0


def main(argv):
    if argv[:1] == ["record"]:
        return record()
    if argv[:1] == ["check"]:
        only = None
        if "--only" in argv:
            only = argv[argv.index("--only") + 1]
        return check(verbose="-v" in argv, only=only)
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
