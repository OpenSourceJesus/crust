/* crust_re -- a small backtracking regex engine for Crust.
 *
 * One engine, three frontends: plain C (this header), the C++ subset
 * (crust_re.hpp), and RPython/minipy via py2c lowering. Design follows
 * tinyre's VM shape (fy, 2012-2015, zlib licence) -- CHAR/SPLIT/JMP/SAVE
 * bytecode with a backtracking executor -- but is byte-oriented rather than
 * UTF-32 and allocates only from a caller-supplied arena, so it works on the
 * baremetal targets where malloc does not exist.
 *
 * Supported: literals, '.', character classes with ranges and negation, the
 * escapes \d \w \s \D \W \S \b \B \n \t \r \f \v \xHH, anchors ^ $, capturing
 * and non-capturing groups, alternation, and the quantifiers * + ? and their
 * lazy forms -- on both single items and groups. {m,n} is supported on single
 * items only.
 *
 * Also supported: named groups `(?P<name>...)`, lookahead `(?=...)` `(?!...)`
 * of any width, and lookbehind `(?<=...)` `(?<!...)` of fixed width (the same
 * restriction CPython imposes; alternation of equal-width arms is fine).
 *
 * Not supported (compilation fails with a message rather than guessing):
 * backreferences, inline flags, and {m,n} applied to a group. That last one is a deliberate omission: the expansion distributes
 * iterations differently from CPython when the body can match empty, changing
 * capture spans and occasionally the match length, and a subtly wrong match is
 * the failure mode this engine exists to avoid.
 *
 * Matching is leftmost, backtracking, with CPython `re` semantics for greedy
 * vs lazy and for capture values (verified by a differential fuzzer).
 */
#ifndef CRUST_RE_H
#define CRUST_RE_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct crust_re crust_re;

/* Result codes from crust_re_exec. */
#define CRUST_RE_NOMATCH   0
#define CRUST_RE_MATCH     1
#define CRUST_RE_ELIMIT   (-1)   /* step budget exhausted (pathological pattern) */

/* Compile `pat` into `arena` (size `arena_size` bytes). Returns NULL on error
 * and, if `err` is non-NULL, points it at a static description of the problem.
 * The returned handle lives inside the arena: it is valid until the caller
 * reuses or frees that memory. No global state, no malloc. */
crust_re *crust_re_compile(const char *pat, void *arena, size_t arena_size,
                           const char **err);

/* A conservative arena size that is always sufficient for `pat`. Lets callers
 * size a buffer without a trial compile. */
size_t crust_re_arena_hint(const char *pat);

/* Number of capturing groups (group 0, the whole match, is not counted). */
int crust_re_ngroups(const crust_re *re);

/* Index of the group named by `(?P<name>...)`, or -1 if there is no such
 * group. Names are compared exactly; group 0 is never named. */
int crust_re_group_index(const crust_re *re, const char *name);

/* Name of group `g`, or NULL if it is unnamed or out of range. */
const char *crust_re_group_name(const crust_re *re, int g);

/* Run `re` over text[0..len). If `anchored`, only position 0 is tried
 * (re.match); otherwise the leftmost match is found (re.search).
 *
 * `caps` receives 2*(ngroups+1) byte offsets: caps[0]/caps[1] are the whole
 * match, caps[2i]/caps[2i+1] group i, and -1 marks a group that did not
 * participate. `ncaps` is the number of ints available; extra slots are set to
 * -1 and missing ones are simply not written. `caps` may be NULL.
 *
 * Returns CRUST_RE_MATCH, CRUST_RE_NOMATCH, or CRUST_RE_ELIMIT. */
int crust_re_exec(const crust_re *re, const char *text, size_t len,
                  int anchored, int *caps, int ncaps);

/* Override the default backtracking step budget (default 1000000). Guards
 * against catastrophic backtracking on patterns like (a+)+b. */
void crust_re_set_limit(crust_re *re, long limit);

#ifdef __cplusplus
}
#endif

#endif /* CRUST_RE_H */
