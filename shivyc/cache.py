"""On-disk cache of parsed ASTs, to speed up recompilation.

Parsing (recursive-descent with backtracking) is the most expensive front-end
step, and it depends only on the post-preprocessing token stream. We therefore
key the cache on a hash of that token stream: identical tokens always produce
an identical AST, and the hash naturally incorporates the contents of every
included header (they have already been spliced into the token stream by the
preprocessor). Cache entries are pickled ASTs under a directory in /tmp.

The cache is best-effort: any failure to read, write, or unpickle simply falls
back to parsing from scratch, so a stale or corrupt entry can never produce a
wrong result -- at worst it is ignored.

The pickle/filesystem machinery exists only to make repeated *host* (CPython)
runs fast. It is host-only: under the ShivyCX translator
(`sys.implementation.name == 'shivyc'`) the whole cache compiles down to a
no-op -- `load_ast` always misses and `store_ast` does nothing -- because the
translated compiler sees the entire program in one invocation (multiple .c/.py
files on the command line) and so never needs a cross-run AST cache. Fencing
the pickle/os use behind that guard also keeps this module free of constructs
the C backend does not model.
"""

import sys

if sys.implementation.name != 'shivyc':
    import hashlib
    import os
    import pickle
    _CACHE_DIR = os.environ.get("SHIVYC_CACHE_DIR", "/tmp/shivyc-cache")

# Bump when the AST representation or parser changes in a way that would make
# previously-cached trees invalid.
_CACHE_VERSION = "1"


def _ensure_dir():
    if sys.implementation.name != 'shivyc':
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            return True
        except OSError:
            return False
    return False


def token_key(tokens):
    """Return a stable hex digest for a post-preprocessing token stream.

    Under translation the cache is disabled, so the key is unused; return an
    empty string (a falsy key the callers treat as "do not cache")."""
    if sys.implementation.name != 'shivyc':
        h = hashlib.sha256()
        h.update(_CACHE_VERSION.encode())
        for t in tokens:
            # kind is identified by its text_repr; content captures the spelling.
            h.update(b"\x00")
            h.update(str(getattr(t.kind, "text_repr", t.kind)).encode())
            h.update(b"\x01")
            h.update(str(getattr(t, "content", "")).encode())
            # Position, too. A cached AST carries the `Range` of every node,
            # so two streams that spell the same tokens at different lines are
            # *not* interchangeable -- replaying one for the other reports
            # every diagnostic against the wrong line. `#line` makes this
            # reachable on purpose: it changes positions and nothing else, so
            # without this the resync is silently undone by a cache hit.
            h.update(b"\x02")
            r = getattr(t, "r", None)
            if r is not None and r.start is not None:
                h.update(("%s:%d:%d" % (r.start.file, r.start.line,
                                        r.start.col)).encode())
        return h.hexdigest()
    return ""


def _path_for(key):
    if sys.implementation.name != 'shivyc':
        return os.path.join(_CACHE_DIR, key + ".ast.pkl")
    return ""


def load_ast(key):
    """Return the cached AST for `key`, or None on any miss/error."""
    if sys.implementation.name != 'shivyc':
        try:
            with open(_path_for(key), "rb") as f:
                return pickle.load(f)
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError,
                ImportError, ValueError):
            return None
    return None


def store_ast(key, ast):
    """Pickle `ast` under `key`. Best-effort; failures are ignored.

    A no-op under translation (the translated compiler does not pickle)."""
    if sys.implementation.name != 'shivyc':
        if not _ensure_dir():
            return
        tmp = _path_for(key) + ".tmp.%d" % os.getpid()
        try:
            with open(tmp, "wb") as f:
                pickle.dump(ast, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, _path_for(key))  # atomic publish
        except (OSError, pickle.PicklingError, TypeError):
            try:
                os.remove(tmp)
            except OSError:
                pass
