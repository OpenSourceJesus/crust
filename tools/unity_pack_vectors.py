"""Vector2 arithmetic in lowered method bodies.

C has no operators on structs, so `a + b`, `v * 2.f`, `-v`, `v += w` on the
engine's `Vector2` (a C struct, see unity_pack's _emit_vector2_struct) come
out of the lowering as C that does not compile. This pass types each
expression -- a `Vector2` local or parameter, `Vector2_make(..)`, a helper or
method that returns one, a member of one (a float) -- and rewrites every
operator with a Vector2 operand into the engine's component-wise helpers:

    a + b   -> Vector2_add(a, b)       a - b  -> Vector2_sub(a, b)
    v * s   -> Vector2_scale(v, s)     s * v  -> Vector2_scale(v, s)
    a * b   -> Vector2_mulv(a, b)      (component-wise, as Godot's)
    v / s   -> Vector2_div(v, s)       a / b  -> Vector2_divv(a, b)
    -v      -> Vector2_neg(v)
    a == b  -> Vector2_eq(a, b) (Unity: approximate) or
               Vector2_eq_exact(a, b) (Godot: exact); != is its negation
    v += w  -> v = Vector2_add(v, w)   (and -=, *=, /=)

Everything without a Vector2 operand is left as it was, character for
character: the pass only touches what C would reject. An expression it
cannot parse is left as it was too, for the C compiler (and crust) to
report.
"""

from __future__ import annotations

import re

__all__ = ["lower_vector2_ops", "VECTOR2_HELPERS_C", "GODOT_VECTOR2_HELPERS_C",
           "GODOT_VECTOR2_FUNCS"]

#: The engine's Vector2 operator helpers (emitted with the struct).
VECTOR2_HELPERS_C = r"""static Vector2 Vector2_add(Vector2 a, Vector2 b) {
    return Vector2_make(a.x + b.x, a.y + b.y);
}
static Vector2 Vector2_sub(Vector2 a, Vector2 b) {
    return Vector2_make(a.x - b.x, a.y - b.y);
}
static Vector2 Vector2_scale(Vector2 v, float s) {
    return Vector2_make(v.x * s, v.y * s);
}
static Vector2 Vector2_mulv(Vector2 a, Vector2 b) {
    return Vector2_make(a.x * b.x, a.y * b.y);
}
static Vector2 Vector2_div(Vector2 v, float s) {
    return Vector2_make(v.x / s, v.y / s);
}
static Vector2 Vector2_divv(Vector2 a, Vector2 b) {
    return Vector2_make(a.x / b.x, a.y / b.y);
}
static Vector2 Vector2_neg(Vector2 v) {
    return Vector2_make(0.f - v.x, 0.f - v.y);
}
/* Godot ==: exact, component by component. */
static int Vector2_eq_exact(Vector2 a, Vector2 b) {
    return a.x == b.x && a.y == b.y;
}"""

#: Godot's Vector2 methods (GodotSharp's Core/Vector2.cs and Mathf.cs),
#: for a Godot pack: godot_pack lowers `v.Normalized()` to
#: `GodotVec_Normalized(v)`.
GODOT_VECTOR2_HELPERS_C = r"""static float GodotVec_Length(Vector2 v) {
    return sqrtf(v.x * v.x + v.y * v.y);
}
static float GodotVec_LengthSquared(Vector2 v) {
    return v.x * v.x + v.y * v.y;
}
/* Normalize: only an exactly zero vector stays zero */
static Vector2 GodotVec_Normalized(Vector2 v) {
    float lsq = v.x * v.x + v.y * v.y;
    float l;
    if (lsq == 0.f) return Vector2_make(0.f, 0.f);
    l = sqrtf(lsq);
    return Vector2_make(v.x / l, v.y / l);
}
static float GodotVec_Dot(Vector2 a, Vector2 b) {
    return a.x * b.x + a.y * b.y;
}
static float GodotVec_Cross(Vector2 a, Vector2 b) {
    return a.x * b.y - a.y * b.x;
}
static float GodotVec_DistanceTo(Vector2 a, Vector2 b) {
    return sqrtf((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y));
}
static float GodotVec_DistanceSquaredTo(Vector2 a, Vector2 b) {
    return (a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y);
}
static float GodotVec_Angle(Vector2 v) {
    return atan2f(v.y, v.x);
}
static float GodotVec_AngleTo(Vector2 a, Vector2 b) {
    return atan2f(GodotVec_Cross(a, b), GodotVec_Dot(a, b));
}
static Vector2 GodotVec_DirectionTo(Vector2 a, Vector2 b) {
    return GodotVec_Normalized(Vector2_make(b.x - a.x, b.y - a.y));
}
/* Lerp: not clamped */
static Vector2 GodotVec_Lerp(Vector2 a, Vector2 b, float w) {
    return Vector2_make(a.x + (b.x - a.x) * w, a.y + (b.y - a.y) * w);
}
static Vector2 GodotVec_MoveToward(Vector2 v, Vector2 to, float delta) {
    float dx = to.x - v.x, dy = to.y - v.y;
    float len = sqrtf(dx * dx + dy * dy);
    if (len <= delta || len < 1e-06f) return to;
    return Vector2_make(v.x + dx / len * delta, v.y + dy / len * delta);
}
static Vector2 GodotVec_Rotated(Vector2 v, float a) {
    float s = sinf(a), c = cosf(a);
    return Vector2_make(v.x * c - v.y * s, v.x * s + v.y * c);
}
static Vector2 GodotVec_LimitLength(Vector2 v, float len) {
    float l = sqrtf(v.x * v.x + v.y * v.y);
    if (l > 0.f && len < l) return Vector2_make(v.x / l * len, v.y / l * len);
    return v;
}
static Vector2 GodotVec_Abs(Vector2 v) {
    return Vector2_make(fabsf(v.x), fabsf(v.y));
}
/* Mathf.IsZeroApprox / IsEqualApprox (float): Epsilon 1e-06 */
static int GodotVec_IsZeroApprox(Vector2 v) {
    return fabsf(v.x) < 1e-06f && fabsf(v.y) < 1e-06f;
}
static int _godot_eq_approx(float a, float b) {
    float tol;
    if (a == b) return 1;
    tol = 1e-06f * fabsf(a);
    if (tol < 1e-06f) tol = 1e-06f;
    return fabsf(a - b) < tol;
}
static int GodotVec_IsEqualApprox(Vector2 a, Vector2 b) {
    return _godot_eq_approx(a.x, b.x) && _godot_eq_approx(a.y, b.y);
}"""

#: The Godot helpers that return a Vector2 (typed by the operator pass).
GODOT_VECTOR2_FUNCS = frozenset((
    "GodotVec_Normalized", "GodotVec_DirectionTo", "GodotVec_Lerp",
    "GodotVec_MoveToward", "GodotVec_Rotated", "GodotVec_LimitLength",
    "GodotVec_Abs"))

#: Engine helpers that return a Vector2.
_VEC_FUNCS = frozenset((
    "Vector2_make", "Vector2_normalized", "Vector2_Lerp", "Vector2_add",
    "Vector2_sub", "Vector2_scale", "Vector2_mulv", "Vector2_div",
    "Vector2_divv", "Vector2_neg", "GodotInput_Vector",
    "GodotVec_Normalized", "GodotVec_DirectionTo", "GodotVec_Lerp",
    "GodotVec_MoveToward", "GodotVec_Rotated", "GodotVec_LimitLength",
    "GodotVec_Abs",
    "RectTransform_get_anchoredPosition"))

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?[fFuUlL]*)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<id>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)
  | (?P<op><<=|>>=|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^]=|
          [-+*/%<>=!~?:&|^.,;(){}\[\]])
""", re.VERBOSE)


def _tokens(text):
    out = []
    k = 0
    while k < len(text):
        m = _TOKEN_RE.match(text, k)
        if not m:
            return None
        if m.lastgroup != "ws":
            out.append((m.lastgroup, m.group(), m.start(), m.end()))
        k = m.end()
    return out


class _Fail(Exception):
    pass


class _Node(object):
    """An expression: its span in the text, its type ('v' a Vector2, 's'
    anything else), and -- when it had to change -- its new text."""
    __slots__ = ("start", "end", "ty", "new")

    def __init__(self, start, end, ty, new=None):
        self.start, self.end, self.ty, self.new = start, end, ty, new


_BINARY = {  # precedence (C's), higher binds tighter
    "*": 13, "/": 13, "%": 13, "+": 12, "-": 12, "<<": 11, ">>": 11,
    "<": 10, ">": 10, "<=": 10, ">=": 10, "==": 9, "!=": 9, "&": 8,
    "^": 7, "|": 6, "&&": 5, "||": 4}
_ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


class _Parser(object):
    """C expressions, enough of them to type Vector2 operands: primary
    (a name, number, string, call, `(e)`, cast), postfix (`.m`, `->m`,
    `[i]`, `(args)`, `++` `--`), unary, binary by precedence, `?:`, and
    assignment."""

    def __init__(self, text, toks, env, funcs, exact):
        self.text, self.toks, self.env, self.funcs = text, toks, env, funcs
        self.exact = exact
        self.k = 0
        self.changed = False

    def peek(self, off=0):
        k = self.k + off
        return self.toks[k] if k < len(self.toks) else (None, None, None,
                                                           None)

    def src(self, node):
        if node.new is not None:
            return node.new
        return self.text[node.start:node.end]

    def expr(self, stop=()):
        """An assignment expression (the comma operator is not needed)."""
        left = self.ternary(stop)
        kind, tok, _s, _e = self.peek()
        if kind == "op" and tok in _ASSIGN and tok not in stop:
            self.k += 1
            right = self.expr(stop)
            if left.ty == "v" and tok in ("+=", "-=", "*=", "/="):
                op = tok[0]
                call = self._binop(op, left, right)
                if call is None:
                    raise _Fail()
                self.changed = True
                return _Node(left.start, right.end, "v", "%s = %s" % (
                    self.src(left), call))
            if left.new is not None or right.new is not None:
                return _Node(left.start, right.end, left.ty, "%s %s %s" % (
                    self.src(left), tok, self.src(right)))
            return _Node(left.start, right.end, left.ty)
        return left

    def ternary(self, stop):
        cond = self.binary(0, stop)
        kind, tok, _s, _e = self.peek()
        if kind == "op" and tok == "?" and "?" not in stop:
            self.k += 1
            a = self.expr(stop + (":",))
            kind, tok, _s, _e = self.peek()
            if tok != ":":
                raise _Fail()
            self.k += 1
            b = self.ternary(stop)
            ty = "v" if "v" in (a.ty, b.ty) else "s"
            node = _Node(cond.start, b.end, ty)
            if any(n.new is not None for n in (cond, a, b)):
                node.new = "%s ? %s : %s" % (self.src(cond), self.src(a),
                                             self.src(b))
            return node
        return cond

    def _binop(self, op, a, b):
        """The helper call for `a op b` with a Vector2 operand, else None."""
        va, vb = a.ty == "v", b.ty == "v"
        sa, sb = self.src(a), self.src(b)
        if op == "+" and va and vb:
            return "Vector2_add(%s, %s)" % (sa, sb)
        if op == "-" and va and vb:
            return "Vector2_sub(%s, %s)" % (sa, sb)
        if op == "*":
            if va and vb:
                return "Vector2_mulv(%s, %s)" % (sa, sb)
            if va:
                return "Vector2_scale(%s, %s)" % (sa, sb)
            if vb:
                return "Vector2_scale(%s, %s)" % (sb, sa)
        if op == "/":
            if va and vb:
                return "Vector2_divv(%s, %s)" % (sa, sb)
            if va:
                return "Vector2_div(%s, %s)" % (sa, sb)
        if op in ("==", "!=") and va and vb:
            call = "%s(%s, %s)" % (
                "Vector2_eq_exact" if self.exact else "Vector2_eq", sa, sb)
            return call if op == "==" else "!" + call
        return None

    def binary(self, min_prec, stop):
        left = self.unary(stop)
        while True:
            kind, tok, _s, _e = self.peek()
            if kind != "op" or tok not in _BINARY or tok in stop:
                return left
            prec = _BINARY[tok]
            if prec < min_prec:
                return left
            self.k += 1
            right = self.binary(prec + 1, stop)
            if "v" in (left.ty, right.ty):
                call = self._binop(tok, left, right)
                if call is None:
                    raise _Fail()     # an operator Vector2 does not have
                self.changed = True
                ty = "s" if tok in ("==", "!=") else "v"
                left = _Node(left.start, right.end, ty, call)
            elif left.new is not None or right.new is not None:
                left = _Node(left.start, right.end, "s", "%s %s %s" % (
                    self.src(left), tok, self.src(right)))
            else:
                left = _Node(left.start, right.end, "s")

    def unary(self, stop):
        kind, tok, s, _e = self.peek()
        if kind == "op" and tok in ("-", "+", "!", "~", "*", "&", "++", "--"):
            self.k += 1
            operand = self.unary(stop)
            if operand.ty == "v" and tok == "-":
                self.changed = True
                return _Node(s, operand.end, "v",
                             "Vector2_neg(%s)" % self.src(operand))
            if operand.ty == "v" and tok == "+":
                return _Node(s, operand.end, "v", self.src(operand))
            node = _Node(s, operand.end, "s")
            if operand.new is not None:
                node.new = tok + self.src(operand)
            return node
        if kind == "op" and tok == "(":
            # a cast `(float)x` / `(Vector2)v`, or a parenthesized expression
            k2, t2, _s2, _e2 = self.peek(1)
            k3, t3, _s3, _e3 = self.peek(2)
            if k2 == "id" and t3 == ")" and (
                    t2 in ("int", "float", "double", "unsigned", "char",
                           "Vector2", "long", "short", "bool")):
                self.k += 3
                operand = self.unary(stop)
                ty = "v" if t2 == "Vector2" else "s"
                node = _Node(s, operand.end, ty)
                if operand.new is not None:
                    node.new = "(%s)%s" % (t2, self.src(operand))
                return node
        return self.postfix(stop)

    def postfix(self, stop):
        node = self.primary(stop)
        while True:
            kind, tok, _s, e = self.peek()
            if kind != "op":
                return node
            if tok in (".", "->"):
                k2, t2, _s2, e2 = self.peek(1)
                if k2 != "id":
                    raise _Fail()
                self.k += 2
                # a Vector2's member is a float
                new = None if node.new is None else "%s%s%s" % (
                    self.src(node), tok, t2)
                node = _Node(node.start, e2, "s", new)
            elif tok == "[":
                self.k += 1
                idx = self.expr(("]",))
                kind, tok, _s, e = self.peek()
                if tok != "]":
                    raise _Fail()
                self.k += 1
                new = None
                if node.new is not None or idx.new is not None:
                    new = "%s[%s]" % (self.src(node), self.src(idx))
                node = _Node(node.start, e, "s", new)
            elif tok == "(":
                name = self.text[node.start:node.end]
                args, end = self.call_args()
                ty = "v" if name in self.funcs else "s"
                new = None
                if node.new is not None or any(a.new is not None
                                               for a in args):
                    new = "%s(%s)" % (self.src(node), ", ".join(
                        self.src(a) for a in args))
                node = _Node(node.start, end, ty, new)
            elif tok in ("++", "--"):
                self.k += 1
                node = _Node(node.start, e, node.ty,
                             None if node.new is None
                             else self.src(node) + tok)
            else:
                return node

    def call_args(self):
        """`(a, b, ..)` from the `(`: (argument nodes, end offset)."""
        self.k += 1
        args = []
        kind, tok, _s, e = self.peek()
        if tok == ")":
            self.k += 1
            return args, e
        while True:
            args.append(self.expr((",", ")")))
            kind, tok, _s, e = self.peek()
            if tok == ",":
                self.k += 1
                continue
            if tok == ")":
                self.k += 1
                return args, e
            raise _Fail()

    def primary(self, stop):
        kind, tok, s, e = self.peek()
        if kind in ("num", "str"):
            self.k += 1
            return _Node(s, e, "s")
        if kind == "id":
            self.k += 1
            return _Node(s, e, "v" if tok in self.env else "s")
        if kind == "op" and tok == "(":
            self.k += 1
            inner = self.expr((")",))
            kind, tok, _s, e = self.peek()
            if tok != ")":
                raise _Fail()
            self.k += 1
            return _Node(s, e, inner.ty, None if inner.new is None
                         else "(%s)" % self.src(inner))
        raise _Fail()


def _has_vector_operand(toks, env, funcs):
    """Whether some operator here may touch a Vector2 (a cheap prefilter)."""
    names = {t[1] for t in toks if t[0] == "id"}
    return bool(names & (env | funcs))


def _rewrite_segment(text, env, funcs, exact):
    """One expression segment, rewritten if it has Vector2 operators; else
    the same text."""
    toks = _tokens(text)
    if not toks or not _has_vector_operand(toks, env, funcs):
        return text
    p = _Parser(text, toks, env, funcs, exact)
    try:
        node = p.expr()
        if p.k != len(toks):
            return text
    except (_Fail, IndexError, RecursionError):
        return text
    if not p.changed or node.new is None:
        return text
    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    return text[:lead] + node.new + text[len(text) - trail:]


_DECL_RE = re.compile(r"(?<![\w.])Vector2\s+(\w+)\s*(?=[=;,)])")


def _segments(text):
    """Split a method body into (is_expression, text) pieces: an expression
    is what lies between statement punctuation -- `;` `{` `}` -- and the
    heads of `if (..)` / `while (..)` / `for (..;..;..)` / `return` /
    `switch`, and a declaration's initializer."""
    out = []
    k = 0
    n = len(text)
    buf = []

    def flush_expr(s):
        if s:
            out.append((True, s))

    while k < n:
        c = text[k]
        if c in "\"'":
            q = k + 1
            while q < n and text[q] != c:
                q += 2 if text[q] == "\\" else 1
            buf.append(text[k:q + 1])
            k = q + 1
            continue
        if c in ";{}":
            _emit_statement("".join(buf), out)
            buf = []
            out.append((False, c))
            k += 1
            continue
        buf.append(c)
        k += 1
    _emit_statement("".join(buf), out)
    return out


_HEAD_RE = re.compile(r"^(\s*)(?:(else)\s+)?(if|while|switch|for)\s*\(")
_RETURN_RE = re.compile(r"^(\s*(?:else\s+)?)(return)\b")
_CASE_RE = re.compile(r"^\s*(?:case\b[^:]*|default\s*):")
_DECLN_RE = re.compile(
    r"^(\s*(?:(?:static|const|unsigned|signed|struct)\s+)*[A-Za-z_][\w:<>]*"
    r"(?:\s*\*+\s*|\s+)[A-Za-z_]\w*(?:\s*\[[^\]]*\])?\s*=)(?!=)")


def _emit_statement(stmt, out):
    """A statement's text as expression / non-expression pieces."""
    if not stmt.strip():
        if stmt:
            out.append((False, stmt))
        return
    m = _CASE_RE.match(stmt)
    if m:
        out.append((False, stmt[:m.end()]))
        _emit_statement(stmt[m.end():], out)
        return
    m = _HEAD_RE.match(stmt)
    if m:
        # the head's parenthesized part, then what follows (a statement
        # without braces, or nothing)
        open_at = m.end() - 1
        depth = 0
        k = open_at
        while k < len(stmt):
            if stmt[k] == "(":
                depth += 1
            elif stmt[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k >= len(stmt):
            out.append((False, stmt))
            return
        out.append((False, stmt[:open_at + 1]))
        inner = stmt[open_at + 1:k]
        if m.group(3) == "for":
            # a for's head is split by ;, which the caller already split on
            # -- only its last piece reaches here; treat it as expressions
            for part in re.split(r"(,)", inner):
                out.append((part != ",", part))
        else:
            out.append((True, inner))
        out.append((False, ")"))
        _emit_statement(stmt[k + 1:], out)
        return
    m = _RETURN_RE.match(stmt)
    if m:
        out.append((False, stmt[:m.end()]))
        out.append((True, stmt[m.end():]))
        return
    m = _DECLN_RE.match(stmt)
    if m:
        out.append((False, stmt[:m.end()]))
        out.append((True, stmt[m.end():]))
        return
    if re.match(r"^\s*else\b", stmt):
        m = re.match(r"^\s*else\b", stmt)
        out.append((False, stmt[:m.end()]))
        _emit_statement(stmt[m.end():], out)
        return
    out.append((True, stmt))


def lower_vector2_ops(text, params="", funcs=(), exact=False):
    """*text*, a lowered method body, with its Vector2 operators as the
    engine's helpers. *params*: the method's C parameter list (a Vector2
    parameter is typed); *funcs*: more functions that return a Vector2 (the
    project's methods); *exact*: Godot's exact `==`, else Unity's
    approximate one."""
    env = set(_DECL_RE.findall(text)) | set(_DECL_RE.findall(params or ""))
    fns = set(_VEC_FUNCS) | set(funcs)
    if not env and not (set(re.findall(r"\b\w+\b", text)) & fns):
        return text
    # a `for (a; b; c)` head: its pieces are split by the statement splitter
    # at the `;`s -- rejoin nothing, each is an expression or a declaration
    pieces = _segments(text)
    return "".join(_rewrite_segment(s, env, fns, exact) if is_expr else s
                   for is_expr, s in pieces)
