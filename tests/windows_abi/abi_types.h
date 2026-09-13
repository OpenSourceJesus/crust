/* Shared declarations for the Win64 ABI conformance programs. */
struct S1 { char c; };
struct S2 { short s; };
struct S4 { char a, b; short h; };
struct S8 { int x, y; };
struct S3 { char c[3]; };
struct S12 { int x, y, z; };
struct S24 { long long a, b, c; };
struct abi_table {
    long long (*mix)(int, double, int, double, int, double, int);
    long long (*many)(int, int, int, int, int, char, short, long long, int, long);
    double (*manyd)(float, double, float, double, float, double);
    float (*retf)(float, int);
    int (*take_sc)(signed char);
    int (*take_us)(unsigned short);
    signed char (*ret_sc)(int);
    unsigned short (*ret_us)(int);
    long (*ret_long)(long);
    struct S1 (*s1)(struct S1);
    struct S2 (*s2)(struct S2);
    struct S4 (*s4)(struct S4);
    struct S8 (*s8)(struct S8);
    struct S3 (*s3)(struct S3);
    struct S12 (*s12)(struct S12);
    struct S24 (*s24)(struct S24);
    long long (*clobber24)(struct S24);
    long long (*late)(int, int, int, int, struct S8, struct S24, struct S3);
    double (*vsum)(int, ...);
    double (*vnamed)(double, int, ...);
    struct S24 (*vret)(int, ...);
    long long (*apply)(long long (*)(int, int, int, int, int, char, short,
                                     long long, int, long), int);
};
