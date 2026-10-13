"""C language extensions for ShivyC.

This module is a source pre-pass that recognizes a few non-standard
extensions, records them as per-function metadata, and blanks them out of the
source (preserving byte offsets and newlines, so error line/column numbers are
unaffected) before ShivyC's ordinary C lexer ever sees them.

Two kinds of extension are supported, both attached to a function *definition*
in the region between the parameter list `)` and the body `{`:

1. Function specifiers, borrowing the GNU `__attribute__` spelling style:

       void f() __stackless__   { ... }   // opt in to stackless lowering
       void f() __metamorphic__ { ... }   // opt in to metamorphic returns

   These give per-function control over optimizations that would otherwise be
   whole-program flags.

2. Contract blocks, borrowing Python's `assert` syntax and parsed with the
   standard-library `ast` module (the approach prototyped in arx86.py):

       extern float calc_sum(float *ptr, unsigned int len)
       assert len(ptr) >= 64
       assert not len(ptr) % 4096
       { ... }

   Each assert states a compile-time contract about an argument. `len(p) >= N`
   and `len(p) <= N` bound an array's element count; `not len(p) % N` asserts
   the count is a multiple of N. Downstream passes use these to prove, from the
   call graph, that a loop can be vectorized with no scalar remainder.

The pre-pass deliberately leaves ordinary C untouched: a header whose
inter-`)`-and-`{` region is only whitespace is not an extended definition.
"""

# (no `re`: the small amount of scanning this module needs is done by hand in
# _find_name_parens / _extract_specifiers, so the module is self-hostable.)

# A candidate function name immediately followed by its parameter list's open
# paren. We pair the parens structurally, then look at what sits between the
# close paren and the body's `{`. Scanned by hand (no `re`) so this module is
# self-hostable.
_SPECIFIERS = {"__stackless__", "__metamorphic__"}


def _is_re_space(c):
    """Whitespace per regex `\\s`: space, tab, newline, CR, form-feed, v-tab."""
    return (c == " " or c == "\t" or c == "\n" or c == "\r"
            or c == "\f" or c == "\v")


def _is_word_char(c):
    """A `\\w` character: alphanumeric or underscore."""
    return c.isalnum() or c == "_"


def _find_name_parens(scan):
    """Every `(start, name, open_idx)` where an identifier is followed by `(`
    (optionally with whitespace between) -- replaces the
    `(?P<name>[A-Za-z_]\\w*)\\s*\\(` finditer. Matches are non-overlapping,
    leftmost, and the identifier is taken greedily, like `re.finditer`."""
    out = []
    n = len(scan)
    i = 0
    while i < n:
        c = scan[i]
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and _is_word_char(scan[j]):
                j = j + 1
            k = j
            while k < n and _is_re_space(scan[k]):
                k = k + 1
            if k < n and scan[k] == "(":
                out.append((i, scan[i:j], k))
                i = k + 1
                continue
        i = i + 1
    return out


def _extract_specifiers(region, attrs):
    """Blank every `__specifier__` token (matching `__[A-Za-z_]\\w*__`) in
    `region` space-for-space, recording each stripped name in `attrs`. Replaces
    `re.sub(r"__[A-Za-z_]\\w*__", take_specifier, region)`."""
    out = []
    n = len(region)
    i = 0
    while i < n:
        end = -1
        if i + 1 < n and region[i] == "_" and region[i + 1] == "_":
            j = i + 2
            if j < n and (region[j].isalpha() or region[j] == "_"):
                k = j + 1
                while k < n and _is_word_char(region[k]):
                    k = k + 1
                # greedy \w* then a trailing `__`: the longest match ends at
                # the rightmost `__` in the word run that still leaves >=1 char
                # after the opening `__` (so e >= j + 3).
                e = k
                while e >= j + 3:
                    if region[e - 1] == "_" and region[e - 2] == "_":
                        end = e
                        break
                    e = e - 1
        if end >= 0:
            seg = region[i:end]
            attrs.add(seg.strip("_"))
            out.append(" " * len(seg))
            i = end
        else:
            out.append(region[i])
            i = i + 1
    return "".join(out)


def _blank_preproc_directives(code):
    """Return a copy of `code` with preprocessor directive lines (and their
    backslash continuations) replaced by spaces, newlines preserved.

    A function-like macro body may use a name like `PyObject_TypeCheck(...)` on
    a continuation line of a `#define`; that is not a function definition, so
    the extension scan must not treat it as one. The `#`-line check below only
    catches the first physical line of a directive, so we blank continuations
    here.
    """
    out = list(code)
    pos = 0
    in_directive = False
    for line in code.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("#") or in_directive:
            for k in range(len(line)):
                if out[pos + k] != "\n":
                    out[pos + k] = " "
            in_directive = line.rstrip().endswith("\\")
        else:
            in_directive = False
        pos += len(line) + 1
    return "".join(out)


def _blank_comments_and_strings(code):
    """Return a copy of `code` with comment and string/char-literal contents
    replaced by spaces (newlines preserved), so byte offsets and line numbers
    are unchanged.

    The extension scan must not treat a function-name-like token that appears
    inside a comment or string literal as a real definition header -- e.g.
    `/* ... _PyDict_CheckConsistency() */` would otherwise be mistaken for a
    function whose "region" runs across unrelated code.
    """
    out = list(code)
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and code[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and nxt == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (code[i] == "*" and i + 1 < n
                                 and code[i + 1] == "/"):
                if code[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " "
                i += 2
        elif c == '"' or c == "'":
            quote = c
            i += 1
            while i < n and code[i] != quote:
                if code[i] == "\\" and i + 1 < n:
                    if code[i] != "\n":
                        out[i] = " "
                    if code[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if code[i] != "\n":
                    out[i] = " "
                i += 1
            i += 1  # skip closing quote
        else:
            i += 1
    return "".join(out)


def _match_paren(code, open_idx):
    """Return the index of the `)` matching the `(` at `open_idx`, or None."""
    depth = 0
    for i in range(open_idx, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _region_after_params(code, close_idx):
    """Scan from after `)` to the body `{`, returning (region, brace_idx).

    Returns (None, None) if a `;` (prototype / statement) or EOF is hit first.
    Parens inside the region (e.g. `len(ptr)`) are tolerated.
    """
    depth = 0
    i = close_idx + 1
    while i < len(code):
        ch = code[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            # An unmatched ')' means the matched name(...) was a call nested
            # in an enclosing parenthesized expression (e.g. `if (f(x)) {`),
            # not a function-definition header. Such a region is never an
            # extension; bail so we do not scan past the real body brace.
            if depth < 0:
                return None, None
        elif depth == 0 and ch == ";":
            return None, None
        elif depth == 0 and ch == "{":
            return code[close_idx + 1:i], i
        i += 1
    return None, None


def _looks_like_extension(region):
    """True if the region is plausibly an extension (vs. unrelated code)."""
    stripped = region.strip()
    return stripped.startswith("__") or "assert" in stripped


class ExtensionInfo:
    """Per-function extension metadata, keyed by function name."""

    def __init__(self):
        # name -> set of specifier strings (without the surrounding __)
        self.attrs = {}
        # name -> {arg_name -> {'len>=': int, 'len<=': int, 'div-by': int}}
        self.contracts = {}
        # thread function name -> {'side': 'left'|'right', 'core': int}
        # Declared via `assert FN in threads.left(core=N)` in a function header.
        self.threads = {}
        # GPU kernels (SPIRV.md): `assert FN in gpu.compute(local_size=64)`
        # -> fn -> {'local_size': (x, y, z)}
        self.gpu_compute = {}
        # `assert PARAM in gpu.buffer(binding=0, readonly)` in FN's header
        # -> fn -> {param -> {'binding': int, 'set': int,
        #                     'readonly': bool, 'writeonly': bool}}
        self.gpu_buffers = {}

    def attrs_of(self, name):
        return self.attrs.get(name, set())

    def has_attr(self, name, attr):
        return attr in self.attrs.get(name, set())

    def contracts_of(self, name):
        return self.contracts.get(name, {})

    def __bool__(self):
        return (bool(self.attrs) or bool(self.contracts)
                or bool(self.threads) or bool(self.gpu_compute)
                or bool(self.gpu_buffers))


def preprocess_extensions(code):
    """Strip extensions from `code`; return (clean_code, ExtensionInfo).

    Blanked regions are replaced space-for-space (newlines preserved) so the
    cleaned source has identical byte offsets to the original.
    """
    info = ExtensionInfo()
    chars = list(code)
    consumed_until = 0  # ignore matches inside an already-claimed region

    # Scan a copy with comments and string/char literals blanked out, so a
    # name-like token inside a comment or string is never mistaken for a
    # function definition header. Offsets are identical to `code`, so indices
    # found here apply directly to `chars`.
    scan = _blank_preproc_directives(_blank_comments_and_strings(code))

    for start, name, open_idx in _find_name_parens(scan):
        if start < consumed_until:
            continue
        # Ignore matches inside a preprocessor directive line (e.g. a
        # function-like macro definition `#define likely(x) ...`), which is
        # not a function definition with an extension region.
        line_start = scan.rfind("\n", 0, start) + 1
        if scan[line_start:start].lstrip().startswith("#"):
            continue
        close_idx = _match_paren(scan, open_idx)
        if close_idx is None:
            continue
        region, brace_idx = _region_after_params(scan, close_idx)
        if region is None or not region.strip():
            continue
        if not _looks_like_extension(region):
            continue

        gpu = {"compute": {}, "buffers": {}}
        attrs, contracts, threads = _parse_region(region, name, gpu)
        for kname, rec in gpu["compute"].items():
            info.gpu_compute[kname] = rec
        if gpu["buffers"]:
            info.gpu_buffers.setdefault(name, {}).update(gpu["buffers"])
        if attrs:
            info.attrs.setdefault(name, set()).update(attrs)
        if contracts:
            info.contracts.setdefault(name, {}).update(contracts)
        for tname, rec in threads.items():
            info.threads[tname] = rec

        # Blank the region in place (preserving newlines and byte offsets).
        for idx in range(close_idx + 1, brace_idx):
            if chars[idx] != "\n":
                chars[idx] = " "
        consumed_until = brace_idx

    return "".join(chars), info


def _parse_region(region, func_name, gpu=None):
    """Extract specifiers, contracts, and thread declarations from a header
    region. Returns (attrs, contracts, threads). GPU kernel declarations
    (`gpu.compute`, `gpu.buffer`) are recorded into the `gpu` dict when one
    is passed ({'compute': {}, 'buffers': {}}); without it they are refused,
    so no caller can silently drop one."""
    attrs = set()
    contracts = {}
    threads = {}

    # Pull out specifier tokens first; whatever remains should be asserts.
    remaining = _extract_specifiers(region, attrs)

    for line in remaining.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("assert"):
            raise ExtensionError(
                f"unexpected text in extension region of '{func_name}': "
                f"{line!r}")
        decl = _parse_gpu_assert(line, func_name)
        if decl is not None:
            if gpu is None:
                raise ExtensionError(
                    f"gpu declaration is not accepted here in "
                    f"'{func_name}': {line!r}")
            if decl[0] == "compute":
                gpu["compute"][decl[1]] = decl[2]
            else:
                gpu["buffers"][decl[1]] = decl[2]
            continue
        thread = _parse_thread_assert(line, func_name)
        if thread is not None:
            tname, rec = thread
            threads[tname] = rec
            continue
        arg, contract = _parse_assert(line, func_name)
        contracts.setdefault(arg, {}).update(contract)

    return attrs, contracts, threads


def _is_identifier(s):
    """True if `s` is a Python-style identifier (no `ast` needed)."""
    if not s:
        return False
    head = s[0]
    if not (head.isalpha() or head == "_"):
        return False
    body = s.replace("_", "")        # an all-underscore name is valid
    return body == "" or body.isalnum()


def _is_int(s):
    """True if `s` is a non-negative integer literal."""
    return s != "" and s.isdigit()


def _split_top_level(s):
    """Split on commas that are not inside parentheses."""
    parts = []
    depth = 0
    cur = ""
    for c in s:
        if c == "(":
            depth = depth + 1
        elif c == ")":
            depth = depth - 1
        if c == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur = cur + c
    if cur.strip() != "":
        parts.append(cur.strip())
    return parts


def _parse_gpu_assert(line, func_name):
    """Recognize the GPU kernel declarations (SPIRV.md section 5):

        assert FN in gpu.compute(local_size=64)
        assert FN in gpu.compute(local_size=(8, 8))
        assert PARAM in gpu.buffer(binding=0, readonly)

    Returns ('compute', FN, {'local_size': (x, y, z)}),
    ('buffer', PARAM, {...}) or None when the line is not a gpu declaration.
    Anything that *is* one but malformed is refused with the reason, in the
    same spirit as the thread declarations: a kernel the compiler cannot
    understand must not quietly become an ordinary function.
    """
    s = line.strip()
    if not s.startswith("assert"):
        return None
    body = s[len("assert"):].strip()
    in_pos = body.find(" in ")
    if in_pos < 0:
        return None
    left = body[:in_pos].strip()
    right = body[in_pos + 4:].strip()
    if not right.startswith("gpu."):
        return None
    if not _is_identifier(left):
        raise ExtensionError(
            f"gpu declaration needs a name on the left of 'in' in "
            f"'{func_name}': {line!r}")
    rest = right[len("gpu."):]
    lp = rest.find("(")
    if lp < 0 or not rest.endswith(")"):
        raise ExtensionError(
            f"gpu declaration must look like gpu.compute(...) or "
            f"gpu.buffer(...) in '{func_name}': {line!r}")
    kind = rest[:lp].strip()
    items = _split_top_level(rest[lp + 1:len(rest) - 1])

    if kind == "compute":
        size = None
        for it in items:
            eq = it.find("=")
            key = it[:eq].strip() if eq >= 0 else it
            val = it[eq + 1:].strip() if eq >= 0 else ""
            if key != "local_size" or eq < 0:
                raise ExtensionError(
                    f"gpu.compute takes only local_size=N or "
                    f"local_size=(X, Y[, Z]) in '{func_name}': {line!r}")
            dims = []
            if val.startswith("(") and val.endswith(")"):
                for d in _split_top_level(val[1:len(val) - 1]):
                    dims.append(d)
            else:
                dims.append(val)
            if len(dims) < 1 or len(dims) > 3:
                raise ExtensionError(
                    f"local_size takes one to three sizes in "
                    f"'{func_name}': {line!r}")
            nums = []
            for d in dims:
                if not _is_int(d) or int(d) < 1:
                    raise ExtensionError(
                        f"local_size must be positive integer constants in "
                        f"'{func_name}': {line!r}")
                nums.append(int(d))
            while len(nums) < 3:
                nums.append(1)
            size = (nums[0], nums[1], nums[2])
        if size is None:
            raise ExtensionError(
                f"gpu.compute needs local_size=... in '{func_name}': "
                f"{line!r}")
        return ("compute", left, {"local_size": size})

    if kind == "buffer":
        rec = {"binding": None, "set": 0, "readonly": False,
               "writeonly": False}
        for it in items:
            eq = it.find("=")
            if eq >= 0:
                key = it[:eq].strip()
                val = it[eq + 1:].strip()
                if key == "binding" or key == "set":
                    if not _is_int(val):
                        raise ExtensionError(
                            f"{key} must be an integer constant in "
                            f"'{func_name}': {line!r}")
                    rec[key] = int(val)
                    continue
            elif it == "readonly" or it == "writeonly":
                rec[it] = True
                continue
            raise ExtensionError(
                f"gpu.buffer takes binding=N, set=N, readonly, writeonly in "
                f"'{func_name}': {line!r}")
        if rec["binding"] is None:
            raise ExtensionError(
                f"gpu.buffer needs binding=N in '{func_name}': {line!r}")
        if rec["readonly"] and rec["writeonly"]:
            raise ExtensionError(
                f"a buffer cannot be both readonly and writeonly in "
                f"'{func_name}': {line!r}")
        return ("buffer", left, rec)

    raise ExtensionError(
        f"unknown gpu declaration 'gpu.{kind}' in '{func_name}' (known: "
        f"compute, buffer): {line!r}")


def _parse_thread_assert(line, func_name):
    """Recognize `assert FN in threads.left(core=N)` / `.right(core=N)`.

    Returns (FN, {'side': 'left'|'right', 'core': int}) or None if the line is
    not a thread declaration (so the caller can try the contract grammar).

    Parsed with plain string operations rather than the `ast` module, so this
    is self-hostable.
    """
    s = line.strip()
    if not s.startswith("assert"):
        return None
    body = s[len("assert"):].strip()
    in_pos = body.find(" in ")
    if in_pos < 0:
        return None
    left = body[:in_pos].strip()
    right = body[in_pos + 4:].strip()
    if not _is_identifier(left):
        return None
    if not right.startswith("threads."):
        return None
    rest = right[len("threads."):]
    lp = rest.find("(")
    if lp < 0:
        return None
    side = rest[:lp].strip()
    inner = rest[lp + 1:].strip()
    if not inner.endswith(")"):
        return None
    inner = inner[:len(inner) - 1].strip()
    if side != "left" and side != "right":
        raise ExtensionError(
            f"thread group must be 'left' or 'right' in '{func_name}': "
            f"{line!r}")
    core = 0
    if inner != "":
        eq = inner.find("=")
        key = inner[:eq].strip() if eq >= 0 else inner
        val = inner[eq + 1:].strip() if eq >= 0 else ""
        if eq < 0 or key != "core" or not _is_int(val):
            raise ExtensionError(
                f"thread declaration takes only core=<int> in '{func_name}': "
                f"{line!r}")
        core = int(val)
    return left, {"side": side, "core": core}


def _parse_assert(line, func_name):
    """Parse one `assert` contract line with plain string operations.

    Recognizes:
        assert len(x) >= N      -> ('x', {'len>=': N})
        assert len(x) <= N      -> ('x', {'len<=': N})
        assert not len(x) % N   -> ('x', {'div-by': N})
    """
    s = line.strip()
    if not s.startswith("assert"):
        raise ExtensionError(f"expected an assert in '{func_name}': {line!r}")
    body = s[len("assert"):].strip()

    # assert not len(x) % N  -> divisibility
    if body.startswith("not "):
        rest = body[4:].strip()
        pct = rest.find("%")
        if pct < 0:
            raise ExtensionError(
                f"unsupported contract in '{func_name}': {line!r}")
        arg = _len_arg(rest[:pct].strip(), func_name, line)
        n = _const_int(rest[pct + 1:].strip(), func_name, line)
        return arg, {"div-by": n}

    # assert len(x) >= N  /  assert len(x) <= N
    ge = body.find(">=")
    le = body.find("<=")
    if ge >= 0:
        arg = _len_arg(body[:ge].strip(), func_name, line)
        n = _const_int(body[ge + 2:].strip(), func_name, line)
        return arg, {"len>=": n}
    if le >= 0:
        arg = _len_arg(body[:le].strip(), func_name, line)
        n = _const_int(body[le + 2:].strip(), func_name, line)
        return arg, {"len<=": n}

    raise ExtensionError(f"unsupported contract in '{func_name}': {line!r}")


def _len_arg(s, func_name, line):
    """Require `len(name)` and return `name`."""
    s = s.strip()
    if s.startswith("len(") and s.endswith(")"):
        inner = s[4:len(s) - 1].strip()
        if _is_identifier(inner):
            return inner
    raise ExtensionError(
        f"contract must use len(arg) in '{func_name}': {line!r}")


def _const_int(s, func_name, line):
    """Require a non-negative integer constant."""
    s = s.strip()
    if _is_int(s):
        return int(s)
    raise ExtensionError(
        f"contract bound must be an integer constant in '{func_name}': "
        f"{line!r}")


class ExtensionError(Exception):
    """Raised when an extension region is malformed."""
