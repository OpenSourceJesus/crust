int printf(const char *fmt, ...);

class Shape {
public:
    int id;
    Shape(int i) { id = i; }
    virtual ~Shape() { printf("~Shape %d\n", id); }
    virtual int area() { return 1; }
    virtual Shape *twin() { return this; }
};

class Square : public Shape {
public:
    int side;
    Square(int i, int s) : Shape(i) { side = s; }
    ~Square() { printf("~Square %d\n", id); }
    int area() { return side * side; }
    Shape *twin() { return this; }
};

class Factory {
    int next;
public:
    Factory() { next = 1; }
    Shape *make(int s) { Shape *p = new Square(next, s); next = next + 1; return p; }
};

int main(void) {
    Factory f;
    Shape *x = f.make(3);
    printf("a=%d\n", x->area());
    delete x;
    Shape *y = f.make(4);
    printf("b=%d\n", y->twin()->area());
    delete y;
    Shape *keep = f.make(5);
    printf("c=%d\n", keep->twin()->area());
    delete keep;
    printf("done\n");
    return 0;
}
