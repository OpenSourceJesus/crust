"""Struct packing: `#pragma pack`, `_Pragma("pack(..)")`, `__attribute__((packed))`.

ShivyC used to lay every struct out naturally, whatever the source asked
for: `#pragma pack` was an ignored directive and `packed` an attribute the
generic stripper in `weak_alias` threw away. gcc honours both, so one C file
produced two struct layouts depending on the compiler -- and nothing said so.
A struct serialised to bytes, or shared with gcc-built code, then differed
silently. That is the failure this pass removes.

It runs on the token stream after preprocessing and before the parser, in
each of the three token pipelines (compile, call graph, memory safety),
directly before `weak_alias.extract_aliases` -- the last point at which a
`packed` attribute is still in the stream, since that pass strips it. It:

  * tracks the `#pragma pack` state, which the preprocessor hands over as a
    `__shivyc_pragma_pack ( .. )` marker because a directive has no tokens of
    its own once the preprocessor is done; `_Pragma("pack(..)")` is read the
    same way;
  * reads `__attribute__((packed))` after `struct`/`union` or after the
    closing `}` of a definition;
  * records the resulting maximum alignment on the `struct`/`union` keyword
    token (`Token.pack`, 0 meaning natural), where the parser picks it up.

Every helper here is `_pk_`-prefixed. py2c module-qualifies a top-level
function name that more than one shivyc module defines, so a second `_ident`
renamed `weak_alias._ident` in the self-hosted build and broke its harness.

Semantics follow gcc's: `pack(N)` caps every member's alignment at N, and
the struct's own alignment with it; `packed` is `pack(1)` for that one type.
"""

import shivyc.lexer as lexer
import shivyc.token_kinds as token_kinds
from shivyc.errors import error_collector, CompilerError

#: The identifier the preprocessor emits in place of a `#pragma pack` line.
PACK_MARKER = "__shivyc_pragma_pack"

_PK_VALID = (1, 2, 4, 8, 16, 32, 64, 128)


def _pk_spell(t: "Token"):
    if t.rep:
        return t.rep
    if isinstance(t.content, str):
        return t.content
    return str(t.kind)


def _pk_ident(t: "Token"):
    return t.content if t.kind is token_kinds.identifier else None


def _pk_is_record_kw(t: "Token"):
    return t.kind is token_kinds.struct_kw or t.kind is token_kinds.union_kw


def _pk_close_paren(tokens, i):
    """Index of the `)` matching the `(` at `i`, or -1."""
    if i >= len(tokens) or _pk_spell(tokens[i]) != "(":
        return -1
    depth = 0
    j = i
    while j < len(tokens):
        s = _pk_spell(tokens[j])
        if s == "(":
            depth += 1
        elif s == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _pk_attr_end(tokens, i):
    """If `tokens[i]` starts `__attribute__((..))`, the index past it, else -1."""
    name = _pk_ident(tokens[i]) if i < len(tokens) else None
    if name != "__attribute__" and name != "__attribute":
        return -1
    j = _pk_close_paren(tokens, i + 1)
    if j < 0:
        return -1
    return j + 1


def _pk_attr_is_packed(tokens, start, end):
    k = start
    while k < end:
        nm = _pk_ident(tokens[k])
        if nm is not None and nm.strip("_") == "packed":
            return True
        k += 1
    return False


def _pk_number(t: "Token"):
    if t.kind is not token_kinds.number:
        return -1
    s = _pk_spell(t)
    if s.isdigit():
        return int(s)
    return -1


def _pk_pack_op(args, current, stack, r):
    """Apply one `pack(..)` argument list; return the new current value.

    The forms gcc and MSVC accept: `pack()` (natural), `pack(N)`,
    `pack(push)`, `pack(push, N)`, `pack(push, name[, N])`, `pack(pop)`,
    `pack(pop, name)` and `pack(show)`. Anything else is an error rather
    than a no-op -- a no-op is the bug this pass exists to remove.
    """
    parts = []
    cur = []
    for t in args:
        if _pk_spell(t) == ",":
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    parts.append(cur)
    if len(parts) == 1 and not parts[0]:
        return 0
    words = []
    for p in parts:
        if len(p) != 1:
            error_collector.add(CompilerError(
                "cannot read this `#pragma pack` argument", r))
            return current
        words.append(p[0])
    head = _pk_ident(words[0])
    if head == "show":
        return current
    if head == "push":
        stack.append(current)
        value = current
        for w in words[1:]:
            n = _pk_number(w)
            if n >= 0:
                value = n
        return _pk_checked(value, current, r)
    if head == "pop":
        if stack:
            return stack.pop()
        return 0
    n = _pk_number(words[0]) if len(words) == 1 else -1
    if n < 0:
        error_collector.add(CompilerError(
            "cannot read this `#pragma pack`: expected a number, `push` or "
            "`pop`", r))
        return current
    return _pk_checked(n, current, r)


def _pk_checked(value, current, r):
    if value != 0 and value not in _PK_VALID:
        error_collector.add(CompilerError(
            "`#pragma pack(%d)`: the value must be a power of two" % value,
            r))
        return current
    return value


def _pk_pragma_text(t: "Token"):
    """The text of a `_Pragma` string operand, unescaped."""
    s = _pk_spell(t)
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _pk_keyword_before_brace(result, close_idx):
    """Index in `result` of the struct/union keyword whose body ends at
    `close_idx` (a `}`), or -1."""
    depth = 0
    j = close_idx
    while j >= 0:
        s = _pk_spell(result[j])
        if s == "}":
            depth += 1
        elif s == "{":
            depth -= 1
            if depth == 0:
                break
        j -= 1
    if j < 0:
        return -1
    j -= 1
    while j >= 0:
        t = result[j]
        if _pk_is_record_kw(t):
            return j
        if t.kind is token_kinds.identifier or _pk_spell(t) in ("(", ")"):
            # The tag, or an attribute written between keyword and body.
            j -= 1
            continue
        return -1
    return -1


def apply_packing(tokens: "list[Token]"):
    """Tag struct/union keywords with their packing; drop pragma markers."""
    result = []
    stack = []
    current = 0
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        name = _pk_ident(t)
        if name == PACK_MARKER:
            j = _pk_close_paren(tokens, i + 1)
            if j < 0:
                error_collector.add(CompilerError(
                    "cannot read this `#pragma pack`", t.r))
                i += 1
                continue
            current = _pk_pack_op(tokens[i + 2:j], current, stack, t.r)
            i = j + 1
            continue
        if name == "_Pragma" and i + 3 < n and _pk_spell(tokens[i + 1]) == "(" \
                and tokens[i + 2].kind is token_kinds.string \
                and _pk_spell(tokens[i + 3]) == ")":
            # C99's operator form, which a macro can expand to. Only `pack`
            # means anything here; the rest are dropped as `#pragma` is.
            text = _pk_pragma_text(tokens[i + 2]).strip()
            if text.startswith("pack"):
                sub = lexer.tokenize(text, "<_Pragma>")
                j = _pk_close_paren(sub, 1)
                if j < 0:
                    error_collector.add(CompilerError(
                        "cannot read this `_Pragma(\"pack..\")`", t.r))
                else:
                    current = _pk_pack_op(sub[2:j], current, stack, t.r)
            i += 4
            continue
        if _pk_is_record_kw(t):
            if current:
                t.pack = current
            k = i + 1
            end = _pk_attr_end(tokens, k)
            while end >= 0:
                if _pk_attr_is_packed(tokens, k, end):
                    t.pack = 1
                k = end
                end = _pk_attr_end(tokens, k)
        elif _pk_spell(t) == "}" and i + 1 < n:
            end = _pk_attr_end(tokens, i + 1)
            if end >= 0 and _pk_attr_is_packed(tokens, i + 1, end):
                result.append(t)
                kw = _pk_keyword_before_brace(result, len(result) - 1)
                if kw >= 0:
                    result[kw].pack = 1
                i += 1
                continue
        result.append(t)
        i += 1
    return result
