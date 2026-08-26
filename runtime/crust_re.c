/* crust_re -- see crust_re.h. Freestanding: no malloc, no locale, no wchar. */

#include "crust_re.h"

#ifndef CRUST_RE_NO_STRING
#include <string.h>
#else
static size_t crre_strlen(const char *s)
{
    size_t n = 0;
    while (s[n]) n++;
    return n;
}
#define strlen crre_strlen
#endif

/* ---- bytecode ----------------------------------------------------------- */

enum {
    OP_CHAR = 1,   /* c                                              */
    OP_ANY,        /* any byte except '\n' (no DOTALL)               */
    OP_CLASS,      /* x = class index                                */
    OP_SPLIT,      /* try x first, then y                            */
    OP_JMP,        /* x                                              */
    OP_SAVE,       /* x = capture slot                               */
    OP_BOL, OP_EOL,
    OP_WORDB, OP_NWORDB,
    OP_MARK,       /* x = mark register: record sp                   */
    OP_PROG,       /* x = mark register, y = loop exit: if the body consumed
                    * nothing, leave the loop (keeping this iteration's
                    * captures) instead of spinning. CPython runs exactly one
                    * empty iteration and commits it, so `(a*)*` against "b"
                    * yields group 1 == (0,0), not unset. */
    OP_REP,        /* single-item repeat; see sub_* fields           */
    OP_LOOK,       /* lookaround: x = sub start, y = continue target,
                    * c bit0 = negate, c bit1 = lookbehind,
                    * sub_x = fixed width (lookbehind only)         */
    OP_LOOKEND,    /* terminates a lookaround sub-program            */
    OP_MATCH
};

typedef struct {
    unsigned char op;
    unsigned char greedy;
    unsigned char c;
    unsigned char sub_op;   /* OP_CHAR / OP_ANY / OP_CLASS, for OP_REP */
    unsigned char sub_c;
    int sub_x;
    int x, y;               /* for OP_REP: x = min, y = max (-1 = unbounded) */
} ReInst;

typedef unsigned char ClassBits[32];   /* 256-bit membership bitmap */

#define CRE_NAME_MAX 32               /* longest (?P<name>) supported */
#define CRE_MAX_GROUPS 63             /* group 0 plus 63 capturing groups */

struct crust_re {
    ReInst   *prog;
    int       nprog;
    ClassBits *classes;
    int       nclasses;
    int       ngroups;
    int       nmarks;
    long      limit;
    char    (*gname)[CRE_NAME_MAX];  /* gname[g] = name of group g, "" if none */
    int       ngname;                /* slots allocated                        */
};

/* ---- arena -------------------------------------------------------------- */

typedef struct {
    char  *base;
    size_t size, used;
    int    oom;
} Arena;

static void *arena_alloc(Arena *a, size_t n)
{
    size_t p = (a->used + 15u) & ~(size_t)15u;   /* 16-byte align */
    if (p + n > a->size) { a->oom = 1; return 0; }
    a->used = p + n;
    return a->base + p;
}

/* ---- character predicates ----------------------------------------------- */

static int cr_isword(int c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
        || (c >= '0' && c <= '9') || c == '_';
}
static int cr_isdigit(int c) { return c >= '0' && c <= '9'; }
static int cr_isspace(int c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

static void bits_set(ClassBits b, int c) { b[(unsigned char)c >> 3] |= (unsigned char)(1u << (c & 7)); }
static int  bits_get(const ClassBits b, int c) { return (b[(unsigned char)c >> 3] >> (c & 7)) & 1; }

/* Fill `b` with the members of a shorthand escape (\d \w \s \D \W \S). */
static void bits_shorthand(ClassBits b, int esc)
{
    int i, in;
    int lower = esc >= 'a' && esc <= 'z' ? esc : esc + 32;
    for (i = 0; i < 256; i++) {
        if (lower == 'd')      in = cr_isdigit(i);
        else if (lower == 'w') in = cr_isword(i);
        else                   in = cr_isspace(i);
        if (esc >= 'A' && esc <= 'Z') in = !in;   /* \D \W \S negate */
        if (in) bits_set(b, i);
    }
}

/* ---- compiler ----------------------------------------------------------- */

typedef struct {
    const char *p;         /* cursor into the pattern     */
    const char *end;
    crust_re   *re;
    Arena      *arena;
    int         maxprog;   /* capacity of re->prog        */
    int         maxclass;
    const char *err;
    unsigned char *wseen;      /* scratch for block_width */
    unsigned char *wscratch;
} Comp;

static int emit(Comp *co, int op)
{
    ReInst *in;
    if (co->re->nprog >= co->maxprog) { co->err = "pattern too complex for arena"; return -1; }
    in = &co->re->prog[co->re->nprog];
    in->op = (unsigned char)op;
    in->greedy = 1; in->c = 0;
    in->sub_op = 0; in->sub_c = 0; in->sub_x = 0;
    in->x = 0; in->y = 0;
    return co->re->nprog++;
}

static int parse_alt(Comp *co);

/* Fixed width of the block reachable from `pc`, stopping at OP_LOOKEND, or -1
 * if the block can match more than one length. Follows control flow rather
 * than scanning linearly, so alternation branches of equal width (as in
 * "(?<=ab|cd)") are accepted just as CPython accepts them. `seen` guards the
 * back-edge of a star loop, which is variable-width by construction.
 */
static int block_width(Comp *co, int pc, unsigned char *seen, int depth)
{
    const ReInst *in;
    if (depth > 64) return -1;
    if (pc < 0 || pc >= co->re->nprog) return -1;
    if (seen[pc]) return -1;              /* loop => variable width */
    seen[pc] = 1;
    in = &co->re->prog[pc];
    switch (in->op) {
    case OP_LOOKEND:
        return 0;
    case OP_CHAR: case OP_ANY: case OP_CLASS: {
        int w = block_width(co, pc + 1, seen, depth + 1);
        return w < 0 ? -1 : w + 1;
    }
    case OP_REP: {
        int w;
        if (in->y < 0 || in->x != in->y) return -1;   /* unbounded or ranged */
        w = block_width(co, pc + 1, seen, depth + 1);
        return w < 0 ? -1 : w + in->x;
    }
    case OP_JMP:
        return block_width(co, in->x, seen, depth + 1);
    case OP_SPLIT: {
        /* Both arms must reach the same width for the whole to be fixed. */
        unsigned char *copy = co->wscratch;
        int i, wa, wb;
        for (i = 0; i < co->re->nprog; i++) copy[i] = seen[i];
        wa = block_width(co, in->x, seen, depth + 1);
        wb = block_width(co, in->y, copy, depth + 1);
        return (wa >= 0 && wa == wb) ? wa : -1;
    }
    case OP_LOOK:
        return block_width(co, in->y, seen, depth + 1);   /* zero width */
    case OP_SAVE: case OP_BOL: case OP_EOL: case OP_WORDB:
    case OP_NWORDB: case OP_MARK: case OP_PROG:
        return block_width(co, pc + 1, seen, depth + 1);
    default:
        return -1;
    }
}



/* Parse an escape that denotes a single character; returns the byte, or -1 if
 * `c` is a shorthand class (handled by the caller) or an error. */
static int parse_char_escape(Comp *co, int c)
{
    switch (c) {
    case 'n': return '\n';
    case 't': return '\t';
    case 'r': return '\r';
    case 'f': return '\f';
    case 'v': return '\v';
    case 'a': return '\a';
    case '0': return '\0';
    case 'x': {
        int i, v = 0;
        for (i = 0; i < 2; i++) {
            int h;
            if (co->p >= co->end) { co->err = "incomplete \\x escape"; return -1; }
            h = *co->p++;
            if (h >= '0' && h <= '9') h -= '0';
            else if (h >= 'a' && h <= 'f') h -= 'a' - 10;
            else if (h >= 'A' && h <= 'F') h -= 'A' - 10;
            else { co->err = "bad \\x escape"; return -1; }
            v = v * 16 + h;
        }
        return v;
    }
    default:
        if (cr_isdigit(c)) { co->err = "backreferences are not supported"; return -1; }
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) {
            co->err = "unsupported escape";
            return -1;
        }
        return c;   /* escaped punctuation: \. \( \\ ... */
    }
}

/* Parse "[...]"; co->p sits just after '['. Emits an OP_CLASS. */
static int parse_class(Comp *co)
{
    int idx, negate = 0, first = 1, pc;
    ClassBits *b;

    if (co->re->nclasses >= co->maxclass) { co->err = "too many character classes"; return -1; }
    idx = co->re->nclasses++;
    b = &co->re->classes[idx];
    { int i; for (i = 0; i < 32; i++) (*b)[i] = 0; }

    if (co->p < co->end && *co->p == '^') { negate = 1; co->p++; }

    while (co->p < co->end && (*co->p != ']' || first)) {
        int lo;
        first = 0;
        if (*co->p == '\\') {
            int e;
            co->p++;
            if (co->p >= co->end) { co->err = "bad escape (end of pattern)"; return -1; }
            e = *co->p++;
            if (e == 'd' || e == 'w' || e == 's' || e == 'D' || e == 'W' || e == 'S') {
                bits_shorthand(*b, e);
                continue;
            }
            if (e == 'b') { bits_set(*b, '\b'); continue; }  /* \b is backspace here */
            lo = parse_char_escape(co, e);
            if (lo < 0) return -1;
        } else {
            lo = (unsigned char)*co->p++;
        }
        /* range? */
        if (co->p + 1 < co->end && *co->p == '-' && co->p[1] != ']') {
            int hi;
            co->p++;
            if (*co->p == '\\') {
                int e;
                co->p++;
                if (co->p >= co->end) { co->err = "bad escape (end of pattern)"; return -1; }
                e = *co->p++;
                hi = parse_char_escape(co, e);
                if (hi < 0) return -1;
            } else {
                hi = (unsigned char)*co->p++;
            }
            if (lo > hi) { co->err = "bad character range"; return -1; }
            while (lo <= hi) bits_set(*b, lo++);
        } else {
            bits_set(*b, lo);
        }
    }
    if (co->p >= co->end || *co->p != ']') { co->err = "unterminated character set"; return -1; }
    co->p++;

    if (negate) { int i; for (i = 0; i < 32; i++) (*b)[i] = (unsigned char)~(*b)[i]; }

    pc = emit(co, OP_CLASS);
    if (pc < 0) return -1;
    co->re->prog[pc].x = idx;
    return pc;
}

/* Try to read a {m,n} quantifier. Returns 1 if one was consumed (writing *lo
 * and *hi), 0 if the '{' is not a quantifier (CPython treats it as a literal
 * then), -1 on error. */
static int parse_brace(Comp *co, int *lo, int *hi)
{
    const char *save = co->p;
    int a = 0, b = -1, seen_a = 0, seen_comma = 0;

    co->p++;   /* '{' */
    while (co->p < co->end && cr_isdigit(*co->p)) { a = a * 10 + (*co->p++ - '0'); seen_a = 1; }
    if (co->p < co->end && *co->p == ',') {
        int seen_b = 0;
        seen_comma = 1;
        co->p++;
        b = 0;
        while (co->p < co->end && cr_isdigit(*co->p)) { b = b * 10 + (*co->p++ - '0'); seen_b = 1; }
        if (!seen_b) b = -1;
    }
    if (co->p >= co->end || *co->p != '}' || (!seen_a && !seen_comma)) {
        co->p = save;   /* not a quantifier -- literal '{' */
        return 0;
    }
    co->p++;
    if (!seen_comma) b = a;
    if (b >= 0 && a > b) { co->err = "min repeat greater than max repeat"; return -1; }
    if ((b < 0 ? a : b) > 1000) { co->err = "repeat count too large"; return -1; }
    *lo = a; *hi = b;
    return 1;
}

/* atom := '(' alt ')' | '[' class ']' | '.' | '\' esc | literal
 * Returns the program index the atom starts at, or -1. */
static int parse_atom(Comp *co)
{
    int c, pc;

    if (co->p >= co->end) { co->err = "unexpected end of pattern"; return -1; }
    c = (unsigned char)*co->p;

    if (c == '(') {
        int capture = 1, gnum = 0, start;
        char name[CRE_NAME_MAX];
        int named = 0;
        co->p++;
        name[0] = 0;
        if (co->p < co->end && *co->p == '?') {
            const char *q = co->p + 1;
            int negate = -1, behind = 0;
            if (q < co->end && *q == ':') { capture = 0; co->p += 2; }
            else if (q < co->end && *q == 'P') {
                if (q + 1 < co->end && q[1] == '<') {
                    int k = 0;
                    q += 2;
                    while (q < co->end && *q != '>') {
                        if (k >= CRE_NAME_MAX - 1) { co->err = "group name too long"; return -1; }
                        name[k++] = *q++;
                    }
                    if (q >= co->end) { co->err = "missing > in group name"; return -1; }
                    if (k == 0) { co->err = "missing group name"; return -1; }
                    name[k] = 0;
                    named = 1;
                    co->p = q + 1;
                } else {
                    co->err = "(?P=name) backreferences are not supported";
                    return -1;
                }
            }
            else if (q < co->end && (*q == '=' || *q == '!')) {
                negate = (*q == '!');
                co->p = q + 1;
            }
            else if (q + 1 < co->end && *q == '<' && (q[1] == '=' || q[1] == '!')) {
                negate = (q[1] == '!');
                behind = 1;
                co->p = q + 2;
            }
            else { co->err = "unsupported extension group '(?...)'"; return -1; }

            if (negate >= 0) {
                /* Lookaround: the sub-pattern is emitted inline and jumped
                 * over; OP_LOOK runs it as a nested match at the current (or,
                 * for lookbehind, the rewound) position and consumes nothing. */
                int look, sub, i, w;
                look = emit(co, OP_LOOK);
                if (look < 0) return -1;
                sub = co->re->nprog;
                if (parse_alt(co) < 0) return -1;
                if (co->p >= co->end || *co->p != ')') {
                    co->err = "missing ), unterminated subpattern"; return -1;
                }
                co->p++;
                if (emit(co, OP_LOOKEND) < 0) return -1;
                co->re->prog[look].x = sub;
                co->re->prog[look].y = co->re->nprog;
                co->re->prog[look].c = (unsigned char)((negate ? 1 : 0) | (behind ? 2 : 0));
                if (behind) {
                    for (i = 0; i < co->re->nprog; i++) co->wseen[i] = 0;
                    w = block_width(co, sub, co->wseen, 0);
                    if (w < 0) {
                        co->err = "look-behind requires fixed-width pattern";
                        return -1;
                    }
                    co->re->prog[look].sub_x = w;
                }
                return look;
            }
        }
        if (capture) {
            gnum = ++co->re->ngroups;
            if (named) {
                int k;
                if (gnum >= co->re->ngname) { co->err = "too many named groups"; return -1; }
                for (k = 0; k < CRE_NAME_MAX; k++) co->re->gname[gnum][k] = name[k];
            }
            start = emit(co, OP_SAVE);
            if (start < 0) return -1;
            co->re->prog[start].x = 2 * gnum;
        } else {
            start = co->re->nprog;
        }
        if (parse_alt(co) < 0) return -1;
        if (co->p >= co->end || *co->p != ')') { co->err = "missing ), unterminated subpattern"; return -1; }
        co->p++;
        if (capture) {
            pc = emit(co, OP_SAVE);
            if (pc < 0) return -1;
            co->re->prog[pc].x = 2 * gnum + 1;
        }
        return start;
    }
    if (c == '[') { co->p++; return parse_class(co); }
    if (c == '.') { co->p++; return emit(co, OP_ANY); }
    if (c == '\\') {
        int e;
        co->p++;
        if (co->p >= co->end) { co->err = "bad escape (end of pattern)"; return -1; }
        e = (unsigned char)*co->p++;
        if (e == 'd' || e == 'w' || e == 's' || e == 'D' || e == 'W' || e == 'S') {
            int idx;
            if (co->re->nclasses >= co->maxclass) { co->err = "too many character classes"; return -1; }
            idx = co->re->nclasses++;
            { int i; for (i = 0; i < 32; i++) co->re->classes[idx][i] = 0; }
            bits_shorthand(co->re->classes[idx], e);
            pc = emit(co, OP_CLASS);
            if (pc < 0) return -1;
            co->re->prog[pc].x = idx;
            return pc;
        }
        if (e == 'b') return emit(co, OP_WORDB);
        if (e == 'B') return emit(co, OP_NWORDB);
        if (e == 'A') return emit(co, OP_BOL);
        if (e == 'Z') return emit(co, OP_EOL);
        e = parse_char_escape(co, e);
        if (e < 0) return -1;
        pc = emit(co, OP_CHAR);
        if (pc < 0) return -1;
        co->re->prog[pc].c = (unsigned char)e;
        return pc;
    }
    if (c == '^') { co->p++; return emit(co, OP_BOL); }
    if (c == '$') { co->p++; return emit(co, OP_EOL); }
    if (c == '*' || c == '+' || c == '?') { co->err = "nothing to repeat"; return -1; }
    if (c == ')') { co->err = "unbalanced parenthesis"; return -1; }

    co->p++;
    pc = emit(co, OP_CHAR);
    if (pc < 0) return -1;
    co->re->prog[pc].c = (unsigned char)c;
    return pc;
}

/* Is [s,e) a single instruction that consumes exactly one byte? Such repeats
 * get the flat OP_REP form, which keeps recursion depth O(1) rather than
 * O(length of match) -- important for long inputs. */
static int single_consumer(Comp *co, int s, int e)
{
    if (e - s != 1) return 0;
    switch (co->re->prog[s].op) {
    case OP_CHAR: case OP_ANY: case OP_CLASS: return 1;
    default: return 0;
    }
}

/* rep := atom quant?  */
static int parse_rep(Comp *co)
{
    int s = co->re->nprog, e, lo, hi, greedy = 1, q, braced = 0;

    if (parse_atom(co) < 0) return -1;
    e = co->re->nprog;

    if (co->p >= co->end) return 0;
    q = (unsigned char)*co->p;

    if (q == '*') { lo = 0; hi = -1; co->p++; }
    else if (q == '+') { lo = 1; hi = -1; co->p++; }
    else if (q == '?') { lo = 0; hi = 1; co->p++; }
    else if (q == '{') {
        int r = parse_brace(co, &lo, &hi);
        if (r < 0) return -1;
        if (r == 0) return 0;                 /* literal '{' already emitted */
        braced = 1;
    } else {
        return 0;
    }
    if (co->p < co->end && *co->p == '?') { greedy = 0; co->p++; }
    if (co->p < co->end && (*co->p == '*' || *co->p == '+')) {
        co->err = "multiple repeat"; return -1;
    }

    /* Fast path: single-byte body. */
    if (single_consumer(co, s, e)) {
        ReInst body = co->re->prog[s];
        ReInst *r;
        int pc;
        co->re->nprog = s;
        pc = emit(co, OP_REP);
        if (pc < 0) return -1;
        r = &co->re->prog[pc];
        r->sub_op = body.op; r->sub_c = body.c; r->sub_x = body.x;
        r->x = lo; r->y = hi; r->greedy = (unsigned char)greedy;
        return 0;
    }

    /* Counted repetition of a *group* is not implemented. The expansion below
     * distributes iterations differently from CPython when the body can match
     * empty, which changes capture spans and sometimes the match length
     * itself. Rejecting is the only safe option: a subtly wrong match is
     * exactly the failure mode this engine exists to avoid. Single-character
     * {m,n} (a{2,3}, \d{1,3}) is unaffected -- it takes the OP_REP fast path
     * above, which is verified against CPython. */
    if (braced) { co->err = "counted repetition of a group is not supported"; return -1; }

    /* General path: save the body, rebuild from copies. */
    {
        ReInst saved[512];
        int blen = e - s, i, n = 0, ends[64], nends = 0;

        if (blen > (int)(sizeof saved / sizeof saved[0])) {
            co->err = "quantified group too large"; return -1;
        }
        for (i = 0; i < blen; i++) saved[i] = co->re->prog[s + i];
        co->re->nprog = s;

        /* Helper: append a fresh copy of the body at the current position. */
        #define APPEND_BODY(dst) do {                                        \
            int _j, _at = co->re->nprog, _delta;                             \
            if (_at + blen > co->maxprog) { co->err = "pattern too complex for arena"; return -1; } \
            _delta = _at - s;                                                \
            for (_j = 0; _j < blen; _j++) {                                  \
                ReInst _in = saved[_j];                                      \
                if (_in.op == OP_SPLIT) { _in.x += _delta; _in.y += _delta; }\
                else if (_in.op == OP_LOOK) { _in.x += _delta; _in.y += _delta; }\
                else if (_in.op == OP_JMP) { _in.x += _delta; }              \
                else if (_in.op == OP_PROG) { _in.y += _delta; }             \
                co->re->prog[co->re->nprog++] = _in;                         \
            }                                                                \
            (dst) = _at;                                                     \
        } while (0)

        for (i = 0; i < lo; i++) { int at; APPEND_BODY(at); (void)at; n++; }

        if (hi < 0) {
            /* unbounded tail: L: SPLIT body,out; MARK; body; PROG; JMP L */
            int L, sp, mk, at;
            L = emit(co, OP_SPLIT);
            if (L < 0) return -1;
            mk = emit(co, OP_MARK);
            if (mk < 0) return -1;
            co->re->prog[mk].x = co->re->nmarks++;
            APPEND_BODY(at); (void)at;
            sp = emit(co, OP_PROG);
            if (sp < 0) return -1;
            co->re->prog[sp].x = co->re->prog[mk].x;
            { int j = emit(co, OP_JMP); if (j < 0) return -1; co->re->prog[j].x = L; }
            if (greedy) { co->re->prog[L].x = L + 1; co->re->prog[L].y = co->re->nprog; }
            else        { co->re->prog[L].x = co->re->nprog; co->re->prog[L].y = L + 1; }
            co->re->prog[sp].y = co->re->nprog;   /* OP_PROG exits where the loop does */
        } else {
            for (i = lo; i < hi; i++) {
                int sp, at, mk, pg;
                sp = emit(co, OP_SPLIT);
                if (sp < 0) return -1;
                /* Same empty-iteration rule as the unbounded case: CPython
                 * runs at most one iteration that consumes nothing, commits
                 * its captures, and stops. Without this guard the optional
                 * copies distribute differently and group spans diverge. */
                mk = emit(co, OP_MARK);
                if (mk < 0) return -1;
                co->re->prog[mk].x = co->re->nmarks++;
                APPEND_BODY(at); (void)at;
                pg = emit(co, OP_PROG);
                if (pg < 0) return -1;
                co->re->prog[pg].x = co->re->prog[mk].x;
                if (nends >= (int)(sizeof ends / sizeof ends[0]) - 1) {
                    co->err = "repeat count too large"; return -1;
                }
                ends[nends++] = sp;
                ends[nends++] = pg;      /* PROG also branches to the end */
                if (greedy) co->re->prog[sp].x = sp + 1;
                else        co->re->prog[sp].y = sp + 1;
            }
            for (i = 0; i < nends; i++) {
                if (co->re->prog[ends[i]].op == OP_PROG) {
                    co->re->prog[ends[i]].y = co->re->nprog;
                } else if (greedy) {
                    co->re->prog[ends[i]].y = co->re->nprog;
                } else {
                    co->re->prog[ends[i]].x = co->re->nprog;
                }
            }
        }
        #undef APPEND_BODY
    }
    return 0;
}

/* cat := rep* */
static int parse_cat(Comp *co)
{
    while (co->p < co->end && *co->p != '|' && *co->p != ')') {
        if (parse_rep(co) < 0) return -1;
    }
    return 0;
}

/* alt := cat ('|' cat)*
 *
 * Each branch is parsed at the same program offset and copied aside, then the
 * whole alternation is assembled in one pass:
 *
 *     SPLIT b0, L1 ; <b0> ; JMP END
 * L1: SPLIT b1, L2 ; <b1> ; JMP END
 * L2: <b2>
 * END:
 *
 * Assembling once matters: an earlier version re-wrapped the program on every
 * '|', which shifted instructions and left previously recorded patch indices
 * pointing at the wrong instruction.
 */
#define CRE_MAX_BRANCHES 32
#define CRE_SCRATCH      512

static int parse_alt(Comp *co)
{
    int start = co->re->nprog;
    ReInst scratch[CRE_SCRATCH];
    int nscratch = 0;
    int boff[CRE_MAX_BRANCHES], blen[CRE_MAX_BRANCHES], nb = 0;
    int ends[CRE_MAX_BRANCHES], nends = 0;
    int i, j;

    if (parse_cat(co) < 0) return -1;
    if (!(co->p < co->end && *co->p == '|')) return 0;   /* single branch */

    for (;;) {
        int n = co->re->nprog - start;
        if (nb >= CRE_MAX_BRANCHES) { co->err = "too many alternation branches"; return -1; }
        if (nscratch + n > CRE_SCRATCH) { co->err = "alternation too large"; return -1; }
        boff[nb] = nscratch;
        blen[nb] = n;
        for (i = 0; i < n; i++) scratch[nscratch++] = co->re->prog[start + i];
        nb++;
        if (!(co->p < co->end && *co->p == '|')) break;
        co->p++;
        co->re->nprog = start;
        if (parse_cat(co) < 0) return -1;
    }

    co->re->nprog = start;
    for (i = 0; i < nb; i++) {
        int sp = -1, at, delta;
        if (i < nb - 1) {
            sp = emit(co, OP_SPLIT);
            if (sp < 0) return -1;
        }
        at = co->re->nprog;
        delta = at - start;
        if (at + blen[i] > co->maxprog) { co->err = "pattern too complex for arena"; return -1; }
        for (j = 0; j < blen[i]; j++) {
            ReInst in = scratch[boff[i] + j];
            /* Every instruction carrying a program offset must be relocated.
             * OP_LOOK jumps over its own inline sub-program, so a stale target
             * lets control fall into the lookaround body and reach OP_LOOKEND
             * as if it were OP_MATCH. */
            if (in.op == OP_SPLIT) { in.x += delta; in.y += delta; }
            else if (in.op == OP_LOOK) { in.x += delta; in.y += delta; }
            else if (in.op == OP_JMP) { in.x += delta; }
            else if (in.op == OP_PROG) { in.y += delta; }
            co->re->prog[co->re->nprog++] = in;
        }
        if (i < nb - 1) {
            int jp = emit(co, OP_JMP);
            if (jp < 0) return -1;
            ends[nends++] = jp;
            co->re->prog[sp].x = sp + 1;
            co->re->prog[sp].y = co->re->nprog;   /* next branch starts here */
        }
    }
    for (i = 0; i < nends; i++) co->re->prog[ends[i]].x = co->re->nprog;
    return 0;
}

/* ---- public: compile ---------------------------------------------------- */

size_t crust_re_arena_hint(const char *pat)
{
    size_t n = pat ? strlen(pat) : 0;
    /* Worst case is dominated by {m,n} expansion of groups; the compiler
     * rejects counts above 1000, and each pattern byte can yield a bounded
     * number of instructions, so scale generously and align up. */
    return sizeof(struct crust_re) + 64
         + (n + 4) * 48 * sizeof(ReInst)
         + (n + 4) * sizeof(ClassBits) + 256;
}

crust_re *crust_re_compile(const char *pat, void *arena, size_t arena_size,
                           const char **err)
{
    Arena a;
    Comp co;
    crust_re *re;
    size_t n;
    int pc;

    if (err) *err = 0;
    if (!pat || !arena) { if (err) *err = "null argument"; return 0; }

    a.base = (char *)arena; a.size = arena_size; a.used = 0; a.oom = 0;
    re = (crust_re *)arena_alloc(&a, sizeof *re);
    if (!re) { if (err) *err = "arena too small"; return 0; }
    n = strlen(pat);

    /* Split the remaining arena between program and class bitmaps. */
    {
        size_t left = a.size - a.used;
        size_t want_cls = (n + 4) * sizeof(ClassBits);
        size_t want_prog;
        size_t want_name = (size_t)(CRE_MAX_GROUPS + 1) * CRE_NAME_MAX;
        size_t want_scratch;
        if (want_cls > left / 4) want_cls = left / 4;
        want_prog = left - want_cls - want_name - 64;
        want_prog = want_prog / 3 * 2;           /* leave room for the two
                                                  * width-walk scratch arrays */
        re->prog = (ReInst *)arena_alloc(&a, want_prog);
        re->classes = (ClassBits *)arena_alloc(&a, want_cls);
        re->gname = (char (*)[CRE_NAME_MAX])arena_alloc(&a, want_name);
        co.maxprog = (int)(want_prog / sizeof(ReInst));
        want_scratch = (size_t)co.maxprog + 8;
        co.wseen = (unsigned char *)arena_alloc(&a, want_scratch);
        co.wscratch = (unsigned char *)arena_alloc(&a, want_scratch);
        if (!re->prog || !re->classes || !re->gname || !co.wseen || !co.wscratch) {
            if (err) *err = "arena too small";
            return 0;
        }
        re->ngname = CRE_MAX_GROUPS + 1;
        { int gi, gk;
          for (gi = 0; gi <= CRE_MAX_GROUPS; gi++)
              for (gk = 0; gk < CRE_NAME_MAX; gk++) re->gname[gi][gk] = 0; }
        co.maxclass = (int)(want_cls / sizeof(ClassBits));
    }
    re->nprog = 0; re->nclasses = 0; re->ngroups = 0; re->nmarks = 0;
    re->limit = 1000000L;

    co.p = pat; co.end = pat + n; co.re = re; co.arena = &a; co.err = 0;

    pc = emit(&co, OP_SAVE);
    if (pc < 0) { if (err) *err = co.err; return 0; }
    re->prog[pc].x = 0;

    if (parse_alt(&co) < 0) { if (err) *err = co.err ? co.err : "bad pattern"; return 0; }
    if (co.p != co.end) {
        if (err) *err = (*co.p == ')') ? "unbalanced parenthesis" : "trailing garbage";
        return 0;
    }
    pc = emit(&co, OP_SAVE);
    if (pc < 0) { if (err) *err = co.err; return 0; }
    re->prog[pc].x = 1;
    if (emit(&co, OP_MATCH) < 0) { if (err) *err = co.err; return 0; }

    if (a.oom) { if (err) *err = "arena too small"; return 0; }
    return re;
}

int crust_re_ngroups(const crust_re *re) { return re ? re->ngroups : 0; }

int crust_re_group_index(const crust_re *re, const char *name)
{
    int g, k;
    if (!re || !name || !re->gname) return -1;
    for (g = 1; g <= re->ngroups && g < re->ngname; g++) {
        for (k = 0; k < CRE_NAME_MAX; k++) {
            if (re->gname[g][k] != name[k]) break;
            if (name[k] == 0) return g;
        }
    }
    return -1;
}

const char *crust_re_group_name(const crust_re *re, int g)
{
    if (!re || !re->gname || g < 1 || g > re->ngroups || g >= re->ngname) return 0;
    return re->gname[g][0] ? re->gname[g] : 0;
}
void crust_re_set_limit(crust_re *re, long limit) { if (re) re->limit = limit; }

/* ---- executor ----------------------------------------------------------- */

#define MAX_SLOTS 128
#define MAX_MARKS 64
#define MAX_DEPTH 8000

typedef struct {
    const crust_re *re;
    const char *text;
    long len;
    int   slots[MAX_SLOTS];
    long  marks[MAX_MARKS];
    long  steps;
    int   depth;
    int   nslots;
} VM;

static int vm_single(const VM *vm, const ReInst *in, long sp)
{
    int ch;
    if (sp >= vm->len) return 0;
    ch = (unsigned char)vm->text[sp];
    switch (in->sub_op) {
    case OP_CHAR:  return ch == in->sub_c;
    case OP_ANY:   return ch != '\n';
    case OP_CLASS: return bits_get(vm->re->classes[in->sub_x], ch);
    }
    return 0;
}

static int vm_run(VM *vm, int pc, long sp)
{
    if (++vm->depth > MAX_DEPTH) { vm->depth--; return CRUST_RE_ELIMIT; }

    for (;;) {
        const ReInst *in;
        if (++vm->steps > vm->re->limit) { vm->depth--; return CRUST_RE_ELIMIT; }
        in = &vm->re->prog[pc];

        switch (in->op) {
        case OP_CHAR:
            if (sp >= vm->len || (unsigned char)vm->text[sp] != in->c) { vm->depth--; return 0; }
            sp++; pc++; break;

        case OP_ANY:
            if (sp >= vm->len || vm->text[sp] == '\n') { vm->depth--; return 0; }
            sp++; pc++; break;

        case OP_CLASS:
            if (sp >= vm->len || !bits_get(vm->re->classes[in->x], (unsigned char)vm->text[sp])) {
                vm->depth--; return 0;
            }
            sp++; pc++; break;

        case OP_BOL:
            if (sp != 0) { vm->depth--; return 0; }
            pc++; break;

        case OP_EOL:
            /* '$' also matches just before a trailing newline, as in CPython. */
            if (!(sp == vm->len || (sp == vm->len - 1 && vm->text[sp] == '\n'))) {
                vm->depth--; return 0;
            }
            pc++; break;

        case OP_WORDB: case OP_NWORDB: {
            int before = sp > 0 && cr_isword((unsigned char)vm->text[sp - 1]);
            int after  = sp < vm->len && cr_isword((unsigned char)vm->text[sp]);
            int atb = before != after;
            if (in->op == OP_NWORDB) atb = !atb;
            if (!atb) { vm->depth--; return 0; }
            pc++; break;
        }

        case OP_MARK:
            vm->marks[in->x] = sp;
            pc++; break;

        case OP_PROG:
            if (sp == vm->marks[in->x]) pc = in->y;   /* empty body: stop looping */
            else pc++;
            break;

        case OP_JMP:
            pc = in->x; break;

        case OP_SAVE: {
            int slot = in->x, old, r;
            if (slot >= MAX_SLOTS) { vm->depth--; return 0; }
            old = vm->slots[slot];
            vm->slots[slot] = (int)sp;
            r = vm_run(vm, pc + 1, sp);
            if (r != 0) { vm->depth--; return r; }
            vm->slots[slot] = old;                  /* undo on backtrack */
            vm->depth--; return 0;
        }

        case OP_SPLIT: {
            int r = vm_run(vm, in->x, sp);
            if (r != 0) { vm->depth--; return r; }
            pc = in->y; break;                      /* tail-iterate the second branch */
        }

        case OP_REP: {
            long count = 0, lo = in->x, hi = in->y;
            long p = sp;
            if (in->greedy) {
                while ((hi < 0 || count < hi) && vm_single(vm, in, p)) { p++; count++; }
                while (count >= lo) {
                    int r = vm_run(vm, pc + 1, p);
                    if (r != 0) { vm->depth--; return r; }
                    if (count == 0) break;
                    p--; count--;
                }
                vm->depth--; return 0;
            } else {
                while (count < lo) {
                    if (!vm_single(vm, in, p)) { vm->depth--; return 0; }
                    p++; count++;
                }
                for (;;) {
                    int r = vm_run(vm, pc + 1, p);
                    if (r != 0) { vm->depth--; return r; }
                    if (hi >= 0 && count >= hi) { vm->depth--; return 0; }
                    if (!vm_single(vm, in, p)) { vm->depth--; return 0; }
                    p++; count++;
                }
            }
        }

        case OP_LOOK: {
            int neg = in->c & 1;
            int behind = in->c & 2;
            long at = sp;
            int r;
            if (behind) {
                if (sp < in->sub_x) {
                    /* Nothing to look back at: the sub-pattern cannot match,
                     * which is failure for (?<=...) and success for (?<!...). */
                    if (!neg) { vm->depth--; return 0; }
                    pc = in->y;
                    break;
                }
                at = sp - in->sub_x;
            }
            r = vm_run(vm, in->x, at);
            if (r == CRUST_RE_ELIMIT) { vm->depth--; return r; }
            if ((r == 1) == (neg != 0)) { vm->depth--; return 0; }
            pc = in->y;      /* zero-width: position is unchanged */
            break;
        }

        case OP_LOOKEND:
            vm->depth--; return 1;      /* end of a lookaround sub-program */

        case OP_MATCH:
            vm->depth--; return 1;

        default:
            vm->depth--; return 0;
        }
    }
}

int crust_re_exec(const crust_re *re, const char *text, size_t len,
                  int anchored, int *caps, int ncaps)
{
    VM vm;
    long start;
    int i, nslots;

    if (!re || !text) return CRUST_RE_NOMATCH;
    nslots = 2 * (re->ngroups + 1);
    if (nslots > MAX_SLOTS) return CRUST_RE_NOMATCH;
    if (re->nmarks > MAX_MARKS) return CRUST_RE_NOMATCH;

    vm.re = re; vm.text = text; vm.len = (long)len; vm.nslots = nslots;
    vm.steps = 0;   /* budget spans the whole call: resetting it per start
                     * position would let a search burn limit*len steps. */

    for (start = 0; start <= (long)len; start++) {
        int r;
        for (i = 0; i < nslots; i++) vm.slots[i] = -1;
        for (i = 0; i < MAX_MARKS; i++) vm.marks[i] = -1;
        vm.depth = 0;

        r = vm_run(&vm, 0, start);
        if (r == CRUST_RE_ELIMIT) return CRUST_RE_ELIMIT;
        if (r == 1) {
            if (caps) {
                for (i = 0; i < ncaps; i++) caps[i] = (i < nslots) ? vm.slots[i] : -1;
            }
            return CRUST_RE_MATCH;
        }
        if (anchored) break;
    }
    return CRUST_RE_NOMATCH;
}
