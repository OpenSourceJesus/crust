/* Every error class --mem-safe detects, one per function.
 *
 *     shivyc --mem-safe examples/memory/memsafe_runtime.c -o memsafe
 *     ./memsafe                    # reports 5 errors + 1 leak, exits 1
 *
 * Rebuild the identical source without the flag and the check macros collapse
 * to bare expressions, the runtime is not linked, and the program runs at full
 * speed -- straight into glibc's "free(): double free detected in tcache 2",
 * which is the diagnostic this flag exists to replace.
 *
 * The macros are written out by hand here because this is the runtime's own
 * fixture. Code lowered from the C++ subset gets them emitted by
 * `cpprust.py --mem-safe` instead.
 */
#include <stdlib.h>
#include <stdio.h>
#include "crust_memsafe.h"

static void heap_overflow(void) {
    int *a = (int *)CRUST_MS_MALLOC(4 * sizeof(int));
    int i;
    for (i = 0; i < 4; i++) CRUST_MS_WR(&a[i], int, "a[i]") = i;
    CRUST_MS_WR(&a[4], int, "a[4]") = 99;          /* one past the end */
    CRUST_MS_FREE(a);
}

static void use_after_free(void) {
    int *p = (int *)CRUST_MS_MALLOC(sizeof(int));
    CRUST_MS_WR(p, int, "*p") = 7;
    CRUST_MS_FREE(p);
    printf("%d\n", CRUST_MS_RD(p, int, "*p"));      /* UAF */
}

static void double_free(void) {
    char *s = (char *)CRUST_MS_MALLOC(8);
    CRUST_MS_FREE(s);
    CRUST_MS_FREE(s);
}

static void uninit_read(void) {
    int *p = (int *)CRUST_MS_MALLOC(2 * sizeof(int));
    CRUST_MS_WR(&p[0], int, "p[0]") = 1;
    printf("%d\n", CRUST_MS_RD(&p[1], int, "p[1]")); /* never written */
    CRUST_MS_FREE(p);
}

static void far_overrun(void) {
    char *b = (char *)CRUST_MS_MALLOC(16);
    CRUST_MS_WR(&b[900], char, "b[900]") = 'x';
    CRUST_MS_FREE(b);
}

static void leak(void) { (void)CRUST_MS_MALLOC(48); }

int main(void) {
    heap_overflow();
    use_after_free();
    double_free();
    uninit_read();
    far_overrun();
    leak();
    return 0;
}
