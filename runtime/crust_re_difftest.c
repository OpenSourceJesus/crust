/* Differential-test driver for crust_re.
 *
 * Reads one case per line from stdin:  <mode> <pat_hex> <text_hex>\n
 * where mode is 'm' (anchored, re.match) or 's' (leftmost, re.search).
 * Writes one result line per case:
 *   NOMATCH | LIMIT | ERR <message> | MATCH <s0> <e0> <s1> <e1> ...
 * Hex encoding keeps arbitrary bytes (newlines, NULs) out of the protocol.
 *
 * Driven by crust_re_difftest.py, which compares every line against CPython.
 */
#include "crust_re.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int unhex(const char *s, size_t n, unsigned char *out)
{
    size_t i;
    if (n % 2) return -1;
    for (i = 0; i < n; i += 2) {
        int hi, lo;
        hi = s[i];     hi = hi <= '9' ? hi - '0' : (hi | 32) - 'a' + 10;
        lo = s[i + 1]; lo = lo <= '9' ? lo - '0' : (lo | 32) - 'a' + 10;
        out[i / 2] = (unsigned char)(hi * 16 + lo);
    }
    return (int)(n / 2);
}

int main(void)
{
    static char line[1 << 20];

    while (fgets(line, sizeof line, stdin)) {
        char *mode, *ph, *th, *sp;
        static unsigned char pat[1 << 16], text[1 << 16];
        int patlen, textlen, ng, i, rc;
        void *arena;
        size_t asz;
        crust_re *re;
        const char *err = 0;
        int caps[128];

        line[strcspn(line, "\n")] = 0;
        if (!line[0]) continue;

        mode = line;
        sp = strchr(line, ' '); if (!sp) { printf("ERR protocol\n"); continue; }
        *sp = 0; ph = sp + 1;
        sp = strchr(ph, ' ');   if (!sp) { printf("ERR protocol\n"); continue; }
        *sp = 0; th = sp + 1;

        patlen = unhex(ph, strlen(ph), pat);
        textlen = unhex(th, strlen(th), text);
        if (patlen < 0 || textlen < 0) { printf("ERR protocol\n"); continue; }
        pat[patlen] = 0;

        asz = crust_re_arena_hint((const char *)pat);
        arena = malloc(asz);
        if (!arena) { printf("ERR oom\n"); continue; }

        re = crust_re_compile((const char *)pat, arena, asz, &err);
        if (!re) {
            printf("ERR %s\n", err ? err : "unknown");
            free(arena);
            fflush(stdout);
            continue;
        }
        ng = crust_re_ngroups(re);
        rc = crust_re_exec(re, (const char *)text, (size_t)textlen,
                           mode[0] == 'm', caps, 2 * (ng + 1));
        if (rc == CRUST_RE_ELIMIT) {
            printf("LIMIT\n");
        } else if (rc == CRUST_RE_NOMATCH) {
            printf("NOMATCH\n");
        } else {
            printf("MATCH");
            for (i = 0; i < 2 * (ng + 1); i++) printf(" %d", caps[i]);
            printf("\n");
        }
        free(arena);
        fflush(stdout);
    }
    return 0;
}
