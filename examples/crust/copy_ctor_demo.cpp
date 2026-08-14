int printf(const char *fmt, ...);
void *malloc(unsigned long);
void free(void *);

class Buf {
public:
    int *data;
    int n;
    Buf() { n = 4; data = (int *)malloc(16); data[0] = 1; printf("ctor\n"); }
    Buf(const Buf &o) {
        n = o.n;
        data = (int *)malloc(16);
        data[0] = o.data[0];
        printf("copy %d\n", data[0]);
    }
    ~Buf() { free(data); printf("dtor\n"); }
    int head() { return data[0]; }
};

int main(void) {
    Buf a;
    {
        Buf b = a;
        Buf c(b);
        printf("b=%d c=%d\n", b.head(), c.head());
    }
    printf("a=%d\n", a.head());
    return 0;
}
