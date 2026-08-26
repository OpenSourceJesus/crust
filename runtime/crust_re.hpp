/* crust_re.hpp -- the C++ subset's view of the crust_re core.
 *
 * std::regex is out of reach for this subset (it wants exceptions, locales,
 * iterators and heavy templates), so this supplies a small `cre::regex` over
 * the same C engine that backs the C and RPython frontends. There is no second
 * implementation here: every method forwards to crust_re.c, so a fix in the
 * core is a fix in all three languages at once.
 *
 * Written in the CPPRUST subset -- plain classes, a constructor and
 * destructor, methods calling methods, operator[] -- rather than special-cased
 * in the lowering, for the same reason `string` and `vector` are: if it
 * compiles, the subset's claims hold.
 *
 *   cre::regex  re("(\\w+)=(\\d+)");
 *   cre::smatch m;
 *   if (cre::regex_search(re, "port=8080", m)) {
 *       // m[0] is the whole match, m[1] the name, m[2] the value
 *   }
 *
 * Compilation errors do not throw: construction records the message and
 * `ok()` reports it, so a bad pattern is loud but does not need unwinding.
 */
#ifndef CRUST_RE_HPP
#define CRUST_RE_HPP

#include "crust_re.h"
#include <string.h>

namespace cre {

/* Maximum capture slots a match can report: group 0 plus 31 groups. */
#define CRE_MAX_CAPS 64
/* Arena carried inside each regex. Patterns larger than this fail to compile
 * rather than allocating, which keeps the class usable with no heap at all. */
#define CRE_ARENA_BYTES 16384

class smatch {
public:
    int n;                       /* number of slots filled (2 per group)   */
    int caps[CRE_MAX_CAPS];
    const char *subject;
    int subject_len;

    smatch() {
        int i;
        n = 0;
        subject = 0;
        subject_len = 0;
        for (i = 0; i < CRE_MAX_CAPS; i++) caps[i] = -1;
    }

    /* Number of groups, not counting the whole match. */
    int size() const { return n > 0 ? n / 2 : 0; }

    int start(int g) const {
        if (g < 0 || 2 * g + 1 >= n) return -1;
        return caps[2 * g];
    }
    int end(int g) const {
        if (g < 0 || 2 * g + 1 >= n) return -1;
        return caps[2 * g + 1];
    }
    int length(int g) const {
        int s = start(g);
        int e = end(g);
        if (s < 0 || e < 0) return -1;
        return e - s;
    }
    /* Did group `g` participate in the match? */
    bool matched(int g) const { return start(g) >= 0; }

    /* Copy group `g` into `out` (NUL-terminated). Returns the length, or -1
     * if the group did not participate or `out` is too small. */
    int str(int g, char *out, int outcap) const {
        int s = start(g);
        int e = end(g);
        int i;
        if (s < 0 || e < 0 || out == 0) return -1;
        if (e - s + 1 > outcap) return -1;
        for (i = s; i < e; i++) out[i - s] = subject[i];
        out[e - s] = 0;
        return e - s;
    }
};

class regex {
public:
    char arena[CRE_ARENA_BYTES];
    crust_re *re;
    const char *err;

    regex(const char *pattern) {
        err = 0;
        re = crust_re_compile(pattern, arena, CRE_ARENA_BYTES, &err);
    }

    /* Did the pattern compile? Check this before matching: a failed regex
     * never matches anything, which would otherwise look like "no match". */
    bool ok() const { return re != 0; }
    const char *error() const { return err ? err : ""; }
    int groups() const { return re ? crust_re_ngroups(re) : 0; }

    /* Bound the backtracking budget for this pattern. */
    void set_limit(long limit) { if (re) crust_re_set_limit(re, limit); }

    int exec(const char *text, int len, int anchored, smatch &m) const {
        int ncaps;
        int rc;
        if (re == 0) return CRUST_RE_NOMATCH;
        ncaps = 2 * (crust_re_ngroups(re) + 1);
        if (ncaps > CRE_MAX_CAPS) return CRUST_RE_NOMATCH;
        rc = crust_re_exec(re, text, (size_t)len, anchored, m.caps, ncaps);
        if (rc == CRUST_RE_MATCH) {
            m.n = ncaps;
            m.subject = text;
            m.subject_len = len;
        } else {
            m.n = 0;
        }
        return rc;
    }
};

/* Anchored at position 0, like re.match. */
static inline bool regex_match(const regex &re, const char *text, smatch &m) {
    return re.exec(text, (int)strlen(text), 1, m) == CRUST_RE_MATCH;
}

/* Leftmost match anywhere, like re.search. */
static inline bool regex_search(const regex &re, const char *text, smatch &m) {
    return re.exec(text, (int)strlen(text), 0, m) == CRUST_RE_MATCH;
}

/* Match forms that do not need the captures.
 *
 * These are deliberately NOT overloads of the two above. Method overloading is
 * part of the subset, but free functions lower to plain C names, so two
 * `regex_match`es collide in the generated C. Distinct names are the honest
 * spelling here. */
static inline bool regex_matches(const regex &re, const char *text) {
    smatch m;
    return regex_match(re, text, m);
}
static inline bool regex_contains(const regex &re, const char *text) {
    smatch m;
    return regex_search(re, text, m);
}

}  /* namespace cre */

#endif /* CRUST_RE_HPP */
