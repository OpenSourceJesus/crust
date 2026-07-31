/* Single inheritance and virtual dispatch, lowered to plain C.
 *
 * The layout is the whole trick. A base is the *first* member of its derived
 * class, so a `Cube *` and the `Square *` inside it and the `Shape *` inside
 * that all name the same address -- upcasting is a cast and costs nothing.
 * The vtable pointer sits first in the root of the hierarchy, so it is at
 * offset zero for every class in it, and a derived class's table begins with
 * its base's slots. That is what lets a `Shape *` call through a table that
 * actually belongs to a `Cube`.
 *
 * An override is reached through a thunk rather than a cast of the function
 * pointer: the slot's `this` is the class that first declared the method, the
 * implementation takes its own, and the thunk converts between them. Casting
 * the pointer instead would be shorter and would work on every compiler
 * anyone is likely to use, but it is undefined behaviour, and the point of
 * this file is that the generated C is C.
 *
 * `describe` is deliberately *not* virtual while the `area` it calls is: a
 * non-virtual method calling a virtual one still dispatches, which is the
 * case that separates a real vtable from a name-mangling trick.
 *
 * Rust is where the data lives; C++ supplies the dispatch. Both lower to
 * functions over a struct pointer, so neither needs a shim for the other.
 */
class Shape {
    int tag;
public:
    Shape() { tag = 1; }
    virtual int area() { return 0; }
    virtual int scaled(int k) { return area() * k; }
    int describe() { return area() + tag; }
    int gettag() { return tag; }
};

class Square : public Shape {
    int w;
public:
    Square(int n) : w(n) { }
    int area() { return w * w; }
};

/* Two levels down: `Cube` overrides again, and reaches its own base's
 * implementation by name -- the lowering gives every class a plain C
 * function, so `Square`'s version is just there to be called. */
class Cube : public Square {
public:
    Cube(int n) : Square(n) { }
    int area() { return 6 * Square_area((Square *)this); }
};

/* The caller only ever sees a `Shape *`. */
int ask_area(Shape *s) { return s->area(); }

int ask_scaled(Shape *s, int k) { return s->scaled(k); }

/* C++ syntax only means anything inside this file: the include hook lowers
 * the `.cpp` and never sees the C translation unit that pulled it in. So the
 * calls that need constructors, method syntax and dispatch live here, and the
 * `.c` driver gets a plain C entry point.
 *
 * `out` is filled in declaration order: exact-type calls, then the same two
 * objects through a `Shape *`, then a non-virtual method that calls a virtual
 * one, then an inherited virtual, then an inherited non-virtual. */
void run_dispatch(int *out) {
    Square sq(3);
    Cube cu(2);

    /* Static type is exact, so these are ordinary calls. */
    out[0] = sq.area();
    out[1] = cu.area();

    /* Static type is `Shape *`; the answer comes from the vtable. */
    out[2] = ask_area((Shape *)&sq);
    out[3] = ask_area((Shape *)&cu);

    /* A non-virtual method whose body calls a virtual one. */
    out[4] = sq.describe();
    out[5] = cu.describe();

    /* An inherited virtual that calls another virtual on the way through. */
    out[6] = ask_scaled((Shape *)&sq, 10);
    out[7] = ask_scaled((Shape *)&cu, 10);

    /* An inherited non-virtual, two levels up. */
    out[8] = cu.gettag();
}
