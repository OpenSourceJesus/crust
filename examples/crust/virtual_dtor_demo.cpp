int printf(const char *fmt, ...);

class Shape {
public:
    int id;
    Shape(int i) { id = i; }
    virtual ~Shape() { printf("~Shape %d\n", id); }
    virtual int area() { return 0; }
};

class Square : public Shape {
public:
    int side;
    Square(int i, int s) : Shape(i) { side = s; }
    ~Square() { printf("~Square %d\n", id); }
    int area() { return side * side; }
};

class Cube : public Square {
public:
    Cube(int i, int s) : Square(i, s) { }
    ~Cube() { printf("~Cube %d\n", id); }
    int area() { return 6 * side * side; }
};

int main(void) {
    Shape *a = new Square(1, 3);
    Shape *b = new Cube(2, 2);
    printf("areas %d %d\n", a->area(), b->area());
    delete a;
    delete b;
    Shape *c = new Shape(3);
    delete c;
    printf("done\n");
    return 0;
}
