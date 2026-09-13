"""Objects for the lexing phase of the compiler.

The lexing phase takes the entire contents of a raw input file and
generates a flat list of tokens present in that input file.

"""

import shivyc.token_kinds as token_kinds
from shivyc.errors import CompilerError, Position, Range, error_collector
from shivyc.tokens import Token
from shivyc.token_kinds import symbol_kinds, keyword_kinds


# Scratch-arena primitives. Under self-hosting these are provided directly by
# the transpiler runtime (see arena_mark / arena_release in py2c's RUNTIME_C);
# they are listed in py2c's RUNTIME_INTRINSICS so these Python `def`s are not
# transpiled and calls lower to the runtime functions. Under CPython (the
# reference compiler) memory is managed by the host, so they are no-ops.
def arena_mark():
    """Snapshot the runtime arena bump pointer (no-op under CPython)."""
    return 0


def arena_release(mark):
    """Rewind the runtime arena to `mark` (no-op under CPython)."""
    return None


class Tagged:
    """Class representing tagged characters.

    c (char) - the character that is tagged
    p (Position) - position of the tagged character
    r (Range) - a length-one range for the character
    """

    def __init__(self, c, p):
        """Initialize object."""
        self.c = c
        self.p = p
        self.r = Range(p, p)


def tokenize(code, filename) -> "list[Token]":
    """Convert given code into a flat list of Tokens.

    lines - List of list of Tagged objects, where each embedded list is a
    separate line in the input program.
    return - List of Token objects.
    """
    # Store tokens as they are generated
    tokens = []

    lines = split_to_tagged_lines(code, filename)
    join_extended_lines(lines)

    in_comment = False
    for logical_line, line in enumerate(lines):
        try:
            line_tokens, in_comment = tokenize_line(line, in_comment)
            for t in line_tokens:
                # Physical line numbers (in t.r) are kept for diagnostics, but
                # backslash-newline continuations have already been spliced, so
                # tag the logical line for the preprocessor's directive
                # grouping.
                t.logical_line = logical_line
                # Append in place. Using `tokens += line_tokens` here built a
                # brand-new concatenated list on every source line, recopying
                # all tokens seen so far -- O(lines^2) time and arena memory,
                # the dominant peak-memory cost of lexing large files.
                tokens.append(t)
        except CompilerError as e:
            error_collector.add(e)

    return tokens


def split_to_tagged_lines(text, filename):
    """Split the input text into tagged lines.

    No newline escaping or other preprocessing is done by this function.

    text (str) - Input file contents as a string.
    filename (str) - Input file name.
    return - Tagged lines. List of list of Tagged objects, where each second
    order list is a separate line in the input progam. No newline characters.
    """
    lines = text.splitlines()
    tagged_lines = []
    for line_num, line in enumerate(lines):
        tagged_line = []
        for col, char in enumerate(line):
            p = Position(filename, line_num + 1, col + 1, line)
            tagged_line.append(Tagged(char, p))
        tagged_lines.append(tagged_line)

    return tagged_lines


def join_extended_lines(lines: list):
    """Join together any lines which end in an escaped newline.

    This function modifies the given lines object in place.

    lines - List of list of Tagged objects, where each embedded list is a
    separate line in the input program.
    """
    # TODO: GCC supports \ followed by whitespace. Should ShivyC do this too?

    i = 0
    while i < len(lines):
        if lines[i] and lines[i][-1].c == "\\":
            # There is a next line to collapse into this one
            if i + 1 < len(lines):
                del lines[i][-1]  # remove trailing backslash
                lines[i] += lines[i + 1]  # concatenate with next line
                del lines[i + 1]  # remove next line

                # Decrement i, so this line is checked for a new trailing
                # backslash.
                i -= 1

            # There is no next line to collapse into this one
            else:
                # TODO: print warning?
                del lines[i][-1]  # remove trailing backslash

        i += 1


def tokenize_line(line: list, in_comment):
    """Tokenize the given single line.

    line - List of Tagged objects.
    in_comment - Whether the first character in this line is part of a
    C-style comment body.
    return - List of Token objects, and boolean indicating whether the next
    character is part of a comment body.
    """
    tokens = []

    # line[chunk_start:chunk_end] is the section of the line currently
    # being considered for conversion into a token; this string will be
    # called the 'chunk'. Everything before the chunk has already been
    # tokenized, and everything after has not yet been examined
    chunk_start = 0
    chunk_end = 0

    # Flag that is set True if the line begins with `#` and `include`,
    # perhaps with comments and whitespace in between.
    include_line = False
    # Flag that is set True if the line is an include directive and the
    # filename has been seen and succesfully parsed.
    seen_filename = False
    # Flag that is set True for a computed include (`#include MACRO`), whose
    # operand is neither "FILENAME" nor <FILENAME>. Such an operand is left to
    # tokenize as ordinary tokens; the preprocessor macro-expands it and
    # re-reads the resulting spelling (C11 6.10.2p4). Sticky so that re-matching
    # `#include` while the operand is still mid-chunk does not re-arm the
    # include-filename path.
    computed_include = False

    while chunk_end < len(line):
        # match_symbol_kind_at scans every symbol spelling character-by-
        # character against the line, boxing a transient one-character string
        # for each comparison. Across a whole file that is millions of tiny,
        # immediately-dead allocations -- the single largest source of lexer
        # arena use. Nothing it allocates escapes (it returns a pre-existing
        # token-kind or None), so bracket both calls in a scratch scope: the
        # boxed characters are reclaimed the instant the matcher returns.
        _scratch = arena_mark()
        symbol_kind = match_symbol_kind_at(line, chunk_end)
        next_symbol_kind = match_symbol_kind_at(line, chunk_end + 1)
        arena_release(_scratch)

        # Comment delimiters must be recognized from the raw characters, not
        # from matched symbol kinds: match_symbol_kind_at is greedy, so e.g.
        # the `*=` after `/` in `/*=...` would hide the `*` and the `/*` would
        # not be seen as a comment start (likewise `/=` in `//=...`).
        cur_c = line[chunk_end].c
        next_c = line[chunk_end + 1].c if chunk_end + 1 < len(line) else ""

        # Set include_line flag True as soon as a `#include` is detected.
        if match_include_command(tokens) and not computed_include:
            include_line = True

        # At the first operand character of an include directive, decide whether
        # it is a literal "FILENAME"/<FILENAME> or a computed include. If the
        # operand does not begin with `"` or `<` (and is not whitespace or a
        # comment), treat the line as a computed include: stop the
        # include-filename path so the operand tokenizes normally.
        if (include_line and not seen_filename
                and chunk_start == chunk_end
                and not cur_c.isspace()
                and not (cur_c == "/" and next_c in ("*", "/"))
                and cur_c not in ('"', "<")):
            include_line = False
            computed_include = True

        if in_comment:
            # If next characters end the comment...
            if cur_c == "*" and next_c == "/":
                in_comment = False
                chunk_start = chunk_end + 2
                chunk_end = chunk_start
            # Otherwise, just skip one character.
            else:
                chunk_start = chunk_end + 1
                chunk_end = chunk_start

        # If next characters start a comment, process previous chunk and set
        # in_comment to true.
        elif cur_c == "/" and next_c == "*":
            add_chunk(line[chunk_start:chunk_end], tokens)
            in_comment = True

        # If next two characters are //, we skip the rest of this line.
        elif cur_c == "/" and next_c == "/":
            break

        # Skip spaces and process previous chunk.
        elif line[chunk_end].c.isspace():
            add_chunk(line[chunk_start:chunk_end], tokens)
            chunk_start = chunk_end + 1
            chunk_end = chunk_start

        # If this is an include line, and not a comment or whitespace,
        # expect the line to match an include filename.
        elif include_line:

            # If the filename has already been seen, there should be no more
            # tokens.
            if seen_filename:
                descrip = "extra tokens at end of include directive"
                raise CompilerError(descrip, line[chunk_end].r)

            filename, end = read_include_filename(line, chunk_end)
            tokens.append(Token(token_kinds.include_file, filename, "",
                                Range(line[chunk_end].p, line[end].p)))

            chunk_start = end + 1
            chunk_end = chunk_start
            seen_filename = True

        # If next character is a quote, we read the whole string as a token.
        elif symbol_kind in {token_kinds.dquote, token_kinds.squote}:
            if symbol_kind == token_kinds.dquote:
                quote_str = '"'
                kind = token_kinds.string
                add_null = True
            else:
                quote_str = "'"
                kind = token_kinds.char_string
                add_null = False

            # A pending chunk immediately before the quote may be a literal
            # prefix. `L` marks a wide (wchar_t) string/char literal.
            prefix = chunk_to_str(line[chunk_start:chunk_end])
            wide = prefix == "L"

            chars, end = read_string(line, chunk_end + 1, quote_str, add_null)
            rep = chunk_to_str(line[chunk_end:end + 1])
            r = Range(line[chunk_end].p, line[end].p)

            if kind == token_kinds.char_string and len(chars) == 0:
                err = "empty character constant"
                error_collector.add(CompilerError(err, r))
            # A character constant with more than one character is valid C
            # (C11 6.4.4.4p10) with an implementation-defined integer value;
            # the bytes are packed (see the parser). Not an error. This also
            # keeps lexing tolerant of `'...'` appearing in the text of a
            # directive inside a skipped conditional group (e.g. CPython's
            # `# error C 'size_t' size should be ...`), which the lexer scans
            # before the preprocessor discards the inactive group.

            tok = Token(kind, chars, rep, r)
            tok.wide = wide
            tokens.append(tok)

            chunk_start = end + 1
            chunk_end = chunk_start

        # If next character is another symbol, add previous chunk and then
        # add the symbol.
        elif symbol_kind and _continues_number(line, chunk_start,
                                                chunk_end):
            chunk_end += 1

        elif symbol_kind:
            symbol_start_index = chunk_end
            symbol_end_index = chunk_end + len(symbol_kind.text_repr) - 1

            r = Range(line[symbol_start_index].p, line[symbol_end_index].p)
            symbol_token = Token(symbol_kind, "", "", r)

            add_chunk(line[chunk_start:chunk_end], tokens)
            tokens.append(symbol_token)

            chunk_start = chunk_end + len(symbol_kind.text_repr)
            chunk_end = chunk_start

        # Include another character in the chunk.
        else:
            chunk_end += 1

    # Flush out anything that is left in the chunk to the output
    add_chunk(line[chunk_start:chunk_end], tokens)

    # Catch a `#include` on a line by itself.
    if (include_line or match_include_command(tokens)) and not seen_filename:
        read_include_filename(line, chunk_end)

    return tokens, in_comment


def chunk_to_str(chunk: list):
    """Convert the given chunk to a string.

    chunk - list of Tagged characters.
    return - string representation of the list of Tagged characters
    """
    return "".join(c.c for c in chunk)


def _continues_number(line: list, chunk_start, chunk_end):
    """Whether the symbol at chunk_end continues a floating constant."""
    chunk = "".join(c.c for c in line[chunk_start:chunk_end])
    ch = line[chunk_end].c
    if ch == ".":
        if len(chunk) > 0 and chunk[0].isdigit():
            return True
        if (len(chunk) == 0 and chunk_end + 1 < len(line)
                and line[chunk_end + 1].c.isdigit()):
            return True
        return False
    if ch in "+-":
        return len(chunk) > 0 and chunk[0].isdigit() and chunk[-1] in "eEpP" and (
            chunk[-1] in "pP" or not chunk.lower().startswith("0x"))
    return False


# Every character that begins some punctuator. Built on first use, once
# token_kinds has registered them all. Called twice per input character, the
# full longest-first scan below dominated compile time on large headers (80%
# of a macOS SDK <stdio.h> build), though most characters -- letters, digits,
# spaces -- can begin no punctuator at all. They now return at once; every
# other character still gets the original scan, so results are unchanged.
_symbol_first_chars = ""


def _symbol_first():
    global _symbol_first_chars
    if not _symbol_first_chars:
        chars = ""
        for kind in symbol_kinds:
            c = kind.text_repr[0]
            if c not in chars:
                chars = chars + c
        _symbol_first_chars = chars
    return _symbol_first_chars


def match_symbol_kind_at(content: list, start):
    """Return the longest matching symbol token kind.

    content - List of Tagged objects in which to search for match.
    start (int) - Index, inclusive, at which to start searching for a match.
    returns (TokenType or None) - Symbol token found, or None if no token
    is found.

    """
    if start >= len(content) or content[start].c not in _symbol_first():
        return None
    for symbol_kind in symbol_kinds:
        # Match each character of the symbol's spelling against the line.
        # An out-of-range index means the line ends before the symbol could
        # match, so this candidate fails. (The original relied on catching
        # IndexError here; an explicit bounds check is equivalent and keeps the
        # logic free of exception-as-control-flow.)
        matched = True
        for i, c in enumerate(symbol_kind.text_repr):
            if start + i >= len(content) or content[start + i].c != c:
                matched = False
                break
        if matched:
            return symbol_kind

    return None


def match_include_command(tokens):
    """Check if end of `tokens` is a `#include` (or `#include_next`)
    directive: both take a "FILENAME" / <FILENAME> operand."""
    return (len(tokens) == 2
            and tokens[-2].kind == token_kinds.pound
            and tokens[-1].kind == token_kinds.identifier
            and (tokens[-1].content == "include"
                 or tokens[-1].content == "include_next"))


def read_string(line: list, start, delim, null):
    """Return a lexed string list in input characters.

    Also returns the index of the string end quote.

    line[start] should be the first character after the opening quote of the
    string to be lexed. This function continues reading characters until
    an unescaped closing quote is reached. The length returned is the
    number of input characters that were read, not the length of the
    string. The latter is the length of the lexed string list.

    The lexed string is a list of integers, where each integer is the
    ASCII value (between 0 and 128) of the corresponding character in
    the string. The returned lexed string includes a null-terminator.

    line - List of Tagged objects for each character in the line.
    start - Index at which to start reading the string.
    delim - Delimiter with which the string ends, like `"` or `'`
    null - Whether to add a null-terminator to the returned character list
    """
    i = start
    chars = []

    escapes = {"'": 39,
               '"': 34,
               "?": 63,
               "\\": 92,
               "a": 7,
               "b": 8,
               "f": 12,
               "n": 10,
               "r": 13,
               "t": 9,
               "v": 11}
    octdigits = "01234567"
    hexdigits = "0123456789abcdefABCDEF"

    while True:
        if i >= len(line):
            descrip = "missing terminating quote"
            raise CompilerError(descrip, line[start - 1].r)
        elif line[i].c == delim:
            if null: chars.append(0)
            return chars, i
        elif (i + 1 < len(line)
              and line[i].c == "\\"
              and line[i + 1].c in escapes):
            chars.append(escapes[line[i + 1].c])
            i += 2
        elif (i + 1 < len(line)
              and line[i].c == "\\"
              and line[i + 1].c in octdigits):
            octal = line[i + 1].c
            i += 2
            while (i < len(line)
                   and len(octal) < 3
                   and line[i].c in octdigits):
                octal += line[i].c
                i += 1
            chars.append(int(octal, 8))
        elif (i + 2 < len(line)
              and line[i].c == "\\"
              and line[i + 1].c == "x"
              and line[i + 2].c in hexdigits):
            hexa = line[i + 2].c
            i += 3
            while i < len(line) and line[i].c in hexdigits:
                hexa += line[i].c
                i += 1
            chars.append(int(hexa, 16))
        else:
            chars.append(ord(line[i].c))
            i += 1


def read_include_filename(line: list, start):
    """Read a filename that follows a #include directive.

    Expects line[start] to be one of `<` or `"`, then reads characters until a
    matching symbol is reached. Then, returns as a string the characters
    read including the initial and final symbol markers. The index returned
    is that of the closing token in the filename.
    """
    if start < len(line) and line[start].c == '"':
        end = '"'
    elif start < len(line) and line[start].c == "<":
        end = ">"
    else:
        descrip = "expected \"FILENAME\" or <FILENAME> after include directive"
        if start < len(line):
            char = line[start]
        else:
            char = line[-1]

        raise CompilerError(descrip, char.r)

    i = start + 1
    try:
        while line[i].c != end:
            i += 1
    except IndexError:
        descrip = "missing terminating character for include filename"
        raise CompilerError(descrip, line[start].r)

    return chunk_to_str(line[start:i + 1]), i


def add_chunk(chunk: list, tokens):
    """Convert chunk into a token if possible and add to tokens.

    If chunk is non-empty but cannot be made into a token, this function
    records a compiler error. We don't need to check for symbol kind tokens
    here because they are converted before they are shifted into the chunk.

    chunk - Chunk to convert into a token, as list of Tagged characters.
    tokens (List[Token]) - List of the tokens thusfar parsed.

    """
    if chunk:
        range = Range(chunk[0].p, chunk[-1].p)

        keyword_kind = match_keyword_kind(chunk)
        if keyword_kind:
            tokens.append(Token(keyword_kind, "", "", range))
            return

        number_string = match_number_string(chunk)
        if number_string:
            tokens.append(Token(token_kinds.number, number_string, "", range))
            return

        identifier_name = match_identifier_name(chunk)
        if identifier_name:
            tokens.append(Token(
                token_kinds.identifier, identifier_name, "", range))
            return

        # No keyword/number/identifier matched. Rather than failing now, emit
        # an `unrecognized` token carrying the text. The preprocessor discards
        # it in dead branches and renders it in #error messages; if it reaches
        # the parser in live code, the parser reports it then.
        tokens.append(Token(
            token_kinds.unrecognized, chunk_to_str(chunk), "", range))


def match_keyword_kind(token_repr):
    """Find the longest keyword token kind with representation token_repr.

    token_repr - Token representation to match exactly, as list of Tagged
    characters.
    returns (TokenKind, or None) - Keyword token kind that matched.

    """
    token_str = chunk_to_str(token_repr)
    for keyword_kind in keyword_kinds:
        if keyword_kind.text_repr == token_str:
            return keyword_kind
    return None


def match_number_string(token_repr):
    """Return a string that represents the given constant number.

    Recognizes C integer constants: decimal, hexadecimal (0x), binary (0b),
    and octal (leading 0), each with optional unsigned/long suffixes
    (any combination of u/U and l/L). The original spelling is returned so the
    value can be parsed downstream.

    token_repr - List of Tagged characters.
    returns (str, or None) - String representation of the number.

    """
    token_str = chunk_to_str(token_repr)
    if _match_float_const(token_str):
        return token_str
    if _match_int_const(token_str):
        return token_str
    return None


# The constant matchers below are hand-written character-class scanners rather
# than `re` patterns, so the lexer transpiles to C. Each is a full-string match
# and was verified identical to its original regex over a large case battery.
#   float: 0[xX](hex*.hex+|hex+.?)[pP][+-]?dig+ | (dig*.dig+|dig+.)([eE][+-]?dig+)? | dig+[eE][+-]?dig+   then [fFlL]?
#   int:   (0[xX]hex+ | 0[bB][01]+ | dig+) [uUlL]*
#   ident: [_A-Za-z][_A-Za-z0-9]*


def _is_dig(c):
    return '0' <= c <= '9'


def _is_alpha(c):
    return ('a' <= c <= 'z') or ('A' <= c <= 'Z')


def _is_hex(c):
    return _is_dig(c) or ('a' <= c <= 'f') or ('A' <= c <= 'F')


def _scan_digits(s, i):
    start = i
    while i < len(s) and _is_dig(s[i]):
        i += 1
    return i, i > start


def _scan_hex(s, i):
    start = i
    while i < len(s) and _is_hex(s[i]):
        i += 1
    return i, i > start


def _scan_exp(s, i, echars):
    """Match [echars][+-]?digit+ at position i. Returns (new_i, matched)."""
    if i < len(s) and s[i] in echars:
        j = i + 1
        if j < len(s) and (s[j] == '+' or s[j] == '-'):
            j += 1
        j, ok = _scan_digits(s, j)
        if ok:
            return j, True
    return i, False


def _match_float_const(s):
    n = len(s)
    # alt A: hex float  0[xX](hex*.hex+ | hex+.?) [pP][+-]?dig+
    if n >= 2 and s[0] == '0' and (s[1] == 'x' or s[1] == 'X'):
        j = 2
        ok_mant = False
        k, _ = _scan_hex(s, j)
        if k < n and s[k] == '.':
            k2, has = _scan_hex(s, k + 1)
            if has:
                j = k2
                ok_mant = True
        if not ok_mant:
            k, has = _scan_hex(s, j)
            if has:
                if k < n and s[k] == '.':
                    k += 1
                j = k
                ok_mant = True
        if ok_mant:
            j2, has = _scan_exp(s, j, "pP")
            if has:
                j = j2
                if j < n and s[j] in "fFlL":
                    j += 1
                if j == n:
                    return True
    # alt B: (dig*.dig+ | dig+.) ([eE][+-]?dig+)?
    j = 0
    ok_mant = False
    k, _ = _scan_digits(s, 0)
    if k < n and s[k] == '.':
        k2, has = _scan_digits(s, k + 1)
        if has:
            j = k2
            ok_mant = True
    if not ok_mant:
        k, has = _scan_digits(s, 0)
        if has and k < n and s[k] == '.':
            j = k + 1
            ok_mant = True
    if ok_mant:
        j, _h = _scan_exp(s, j, "eE")
        if j < n and s[j] in "fFlL":
            j += 1
        if j == n:
            return True
    # alt C: dig+ [eE][+-]?dig+
    k, has = _scan_digits(s, 0)
    if has:
        j2, hase = _scan_exp(s, k, "eE")
        if hase:
            j = j2
            if j < n and s[j] in "fFlL":
                j += 1
            if j == n:
                return True
    return False


def is_float_constant(spelling):
    """Return whether `spelling` is a floating (not integer) constant."""
    return _match_float_const(spelling)


def _match_int_const(s):
    n = len(s)
    i = 0
    if n == 0:
        return False
    if s[0] == '0' and n > 1 and (s[1] == 'x' or s[1] == 'X'):
        i = 2
        i, ok = _scan_hex(s, i)
        if not ok:
            return False
    elif s[0] == '0' and n > 1 and (s[1] == 'b' or s[1] == 'B'):
        i = 2
        start = i
        while i < n and (s[i] == '0' or s[i] == '1'):
            i += 1
        if i == start:
            return False
    else:
        i, ok = _scan_digits(s, i)
        if not ok:
            return False
    while i < n and s[i] in "uUlL":
        i += 1
    return i == n


def match_identifier_name(token_repr):
    """Return a string that represents the name of an identifier.

    token_repr - List of Tagged characters.
    returns (str, or None) - String name of the identifier.

    """
    token_str = chunk_to_str(token_repr)
    if _match_identifier(token_str):
        return token_str
    else:
        return None


def _match_identifier(s):
    if len(s) == 0:
        return False
    if not (s[0] == '_' or _is_alpha(s[0])):
        return False
    i = 1
    while i < len(s):
        c = s[i]
        if not (c == '_' or _is_alpha(c) or _is_dig(c)):
            return False
        i += 1
    return True
