#include "la.h"

int main() {
    Vec<float,16> a; Vec<float,16> b; Vec<float,16> c;
    int i = 0;
    for (i = 0; i < 16; i = i + 1) {
        a.d[i] = 1.0f; b.d[i] = 2.0f; c.d[i] = 3.0f;
    }
    Vec<float,16> s = a + b * c;        /* 1 + 6 = 7 per lane */
    Vec<float,16> t = a - b;            /* -1 */
    Vec<float,16> u = a.scaled(5.0f);   /* 5 */
    float dp = a.dot(b);                /* 32 */

    Mat<float,4,4> M; Vec<float,4> x;
    for (i = 0; i < 16; i = i + 1) { M.d[i] = 1.0f; }
    for (i = 0; i < 4; i = i + 1) { x.d[i] = 2.0f; }
    Vec<float,4> y = M * x;             /* 8 */

    return (int)(s.d[0] + t.d[0] + u.d[0] + dp + y.d[0]);
    /* 7 + (-1) + 5 + 32 + 8 = 51 */
}
