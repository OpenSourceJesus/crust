"""rasm_macro.py -- gas macro expansion for rasm.

A source-to-source pass that runs over the line list before the assembler sees
it. `.macro` / `.endm` definitions are collected and removed; invocations are
replaced by the substituted body. Everything else passes through untouched,
including line count -- each definition line and each `.endm` becomes an empty
line, so a diagnostic's line number still points at the right place in the
original file.

Supported, which is the subset real `.S` files use:

    .macro NAME arg, arg2=default
        <body, referring to \\arg and \\arg2>
    .endm

    NAME val, val2              positional invocation
    NAME arg2=x, arg=y          keyword invocation
    NAME                        all-default invocation

    \\arg    parameter substitution
    \\()     an empty separator, for gluing a parameter to following text
    \\@      a counter, unique per expansion -- for labels inside a macro body
    .rept N / .endr             repeat a block N times

Macros may call other macros; recursion is bounded by MAX_DEPTH so a macro that
invokes itself fails loudly instead of hanging the assembler.

Why this exists: the mbos kernel's interrupt stub table (`idt.S`) is built from
`.macro` templates -- forty-eight nearly identical stubs -- and without this
pass rasm rejects it with `unsupported mnemonic: ISR_NOERR`. A self-hosted
build has to assemble every `.S` in the tree, so this was on the critical path.
"""

MAX_DEPTH = 32
MAX_REPT = 65536


class MacroError(Exception):
    pass


class Macro(object):
    def __init__(self, name, params, defaults, body):
        self.name = name
        self.params = params        # list of parameter names, in order
        self.defaults = defaults    # same length; "" when no default
        self.body = body            # list of raw body lines


def _strip_comment(line):
    """Drop a line comment for the purpose of *recognising* a directive.

    The return value is only used for matching, never emitted, so it does not
    matter that this is cruder than the assembler's own comment handling.
    """
    for marker in ("//", "#", ";"):
        idx = line.find(marker)
        if idx >= 0:
            line = line[:idx]
    return line


def _first_word(line):
    s = _strip_comment(line).strip()
    if s == "":
        return ""
    i = 0
    while i < len(s) and s[i] not in " \t":
        i += 1
    return s[:i]


def _rest_after_word(line):
    s = _strip_comment(line).strip()
    i = 0
    while i < len(s) and s[i] not in " \t":
        i += 1
    return s[i:].strip()


def _split_args(text):
    """Split a comma-separated argument list, respecting quotes and brackets.

    gas argument lists are not expressions, but they can contain commas inside
    strings and inside `(...)`, and splitting naively on every comma turns one
    argument into two.
    """
    out = []
    cur = ""
    depth = 0
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote != "":
            cur += c
            if c == "\\" and i + 1 < len(text):
                cur += text[i + 1]
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == '"' or c == "'":
            quote = c
            cur += c
            i += 1
            continue
        if c == "(" or c == "[":
            depth += 1
        elif c == ")" or c == "]":
            if depth > 0:
                depth -= 1
        if c == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    if cur.strip() != "" or len(out) > 0:
        out.append(cur.strip())
    return out


def _parse_params(text):
    """Parse a `.macro` parameter list into (names, defaults).

    gas accepts `.macro m a, b` and `.macro m a b` (space separated), and a
    parameter may carry `=default`. `:req` is accepted and treated as "no
    default", which is what it means for our purposes.
    """
    if text == "":
        return [], []
    # normalise space separation into commas so one splitter handles both
    if text.find(",") < 0:
        parts = []
        for piece in text.split():
            parts.append(piece)
    else:
        parts = _split_args(text)

    names = []
    defaults = []
    for p in parts:
        p = p.strip()
        if p == "":
            continue
        if p.endswith(":req"):
            p = p[:-4].strip()
        eq = p.find("=")
        if eq >= 0:
            names.append(p[:eq].strip())
            defaults.append(p[eq + 1:].strip())
        else:
            names.append(p)
            defaults.append("")
    return names, defaults


def _substitute(line, binds, counter):
    r"""Replace \param, \(), and \@ in one body line.

    Parameter names are matched longest-first so that `\ab` binds to a
    parameter named `ab` rather than to `a` followed by a literal `b`.
    """
    names = []
    for k in binds:
        names.append(k)
    # longest first
    i = 0
    while i < len(names):
        j = i + 1
        while j < len(names):
            if len(names[j]) > len(names[i]):
                tmp = names[i]
                names[i] = names[j]
                names[j] = tmp
            j += 1
        i += 1

    out = ""
    i = 0
    n = len(line)
    while i < n:
        if line[i] != "\\":
            out += line[i]
            i += 1
            continue
        # a backslash: try the special forms, then parameters
        if line[i + 1:i + 3] == "()":
            i += 3
            continue
        if line[i + 1:i + 2] == "@":
            out += str(counter)
            i += 2
            continue
        matched = False
        k = 0
        while k < len(names):
            name = names[k]
            if line[i + 1:i + 1 + len(name)] == name:
                out += binds[name]
                i += 1 + len(name)
                matched = True
                break
            k += 1
        if matched:
            continue
        out += line[i]
        i += 1
    return out


def _bind(macro, args):
    """Match an invocation's arguments to the macro's parameters."""
    binds = {}
    i = 0
    while i < len(macro.params):
        binds[macro.params[i]] = macro.defaults[i]
        i += 1

    positional = 0
    i = 0
    while i < len(args):
        a = args[i]
        eq = a.find("=")
        # `a=b` is a keyword argument only when the left side names a
        # parameter; otherwise it is a value that happens to contain '='.
        if eq > 0 and a[:eq].strip() in binds:
            binds[a[:eq].strip()] = a[eq + 1:].strip()
        else:
            if positional >= len(macro.params):
                raise MacroError(
                    "too many arguments to macro '%s': expected %d"
                    % (macro.name, len(macro.params)))
            binds[macro.params[positional]] = a
            positional += 1
        i += 1
    return binds


class _Expander(object):
    def __init__(self):
        self.macros = {}
        self.counter = 0

    def collect(self, lines):
        """Strip `.macro` blocks out of `lines`, recording each definition.

        Definition lines are replaced by empty lines rather than deleted, so
        line numbers in the output still match the input.
        """
        out = []
        i = 0
        n = len(lines)
        while i < n:
            word = _first_word(lines[i])
            if word != ".macro":
                out.append(lines[i])
                i += 1
                continue

            header = _rest_after_word(lines[i])
            if header == "":
                raise MacroError("line %d: .macro with no name" % (i + 1))
            # name is the first token; the rest is the parameter list
            sp = 0
            while sp < len(header) and header[sp] not in " \t,":
                sp += 1
            name = header[:sp]
            params, defaults = _parse_params(header[sp:].strip())

            body = []
            out.append("")          # the .macro line itself
            i += 1
            depth = 1
            while i < n:
                w = _first_word(lines[i])
                if w == ".macro":
                    depth += 1
                elif w == ".endm":
                    depth -= 1
                    if depth == 0:
                        out.append("")      # the .endm line
                        i += 1
                        break
                body.append(lines[i])
                out.append("")
                i += 1
            else:
                raise MacroError("unterminated .macro '%s'" % name)

            if depth != 0:
                raise MacroError("unterminated .macro '%s'" % name)
            self.macros[name] = Macro(name, params, defaults, body)
        return out

    def expand(self, lines, depth):
        if depth > MAX_DEPTH:
            raise MacroError(
                "macro expansion nested deeper than %d levels -- "
                "a macro probably invokes itself" % MAX_DEPTH)

        out = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            word = _first_word(line)

            if word == ".rept":
                count_text = _rest_after_word(line)
                block, i = self._take_block(lines, i + 1, ".rept", ".endr")
                count = self._rept_count(count_text)
                k = 0
                while k < count:
                    out.extend(self.expand(block, depth + 1))
                    k += 1
                continue

            if word == ".endm" or word == ".endr":
                raise MacroError("line %d: %s without a matching opener"
                                 % (i + 1, word))

            if word in self.macros:
                macro = self.macros[word]
                args = _split_args(_rest_after_word(line))
                binds = _bind(macro, args)
                self.counter += 1
                counter = self.counter

                body = []
                j = 0
                while j < len(macro.body):
                    body.append(_substitute(macro.body[j], binds, counter))
                    j += 1
                out.extend(self.expand(body, depth + 1))
                i += 1
                continue

            out.append(line)
            i += 1
        return out

    def _take_block(self, lines, start, opener, closer):
        block = []
        i = start
        depth = 1
        while i < len(lines):
            w = _first_word(lines[i])
            if w == opener:
                depth += 1
            elif w == closer:
                depth -= 1
                if depth == 0:
                    return block, i + 1
            block.append(lines[i])
            i += 1
        raise MacroError("unterminated %s" % opener)

    def _rept_count(self, text):
        t = text.strip()
        if t == "":
            raise MacroError(".rept with no count")
        try:
            if t[:2] == "0x" or t[:2] == "0X":
                count = int(t[2:], 16)
            else:
                count = int(t, 10)
        except ValueError:
            raise MacroError(".rept count is not a literal integer: %s" % t)
        if count < 0 or count > MAX_REPT:
            raise MacroError(".rept count out of range: %d" % count)
        return count


def has_macros(text):
    """Cheap check so callers can skip the pass entirely on ordinary files."""
    return text.find(".macro") >= 0 or text.find(".rept") >= 0


def expand(text):
    """Expand every macro in `text` and return the resulting source."""
    lines = text.split("\n")
    exp = _Expander()
    lines = exp.collect(lines)
    lines = exp.expand(lines, 0)
    return "\n".join(lines)
