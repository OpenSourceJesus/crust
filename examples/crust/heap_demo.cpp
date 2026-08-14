int printf(const char *fmt, ...);

class Node {
    int v;
public:
    Node(int x) { v = x; printf("ctor %d\n", v); }
    ~Node() { printf("dtor %d\n", v); }
    int get() { return v; }
    void set(int x) { v = x; }
};

class Owner {
    Node *held;
public:
    Owner() { held = new Node(1); }
    ~Owner() { delete held; }
    Node *node() { return held; }
};

int main(void) {
    Node *a = new Node(10);
    printf("a=%d\n", a->get());
    a->set(11);
    printf("a=%d\n", a->get());
    delete a;

    {
        Owner o;
        Node *inner = o.node();
        printf("owned=%d\n", inner->get());
    }

    Node *b = new Node(20);
    if (b) delete b; else printf("null\n");
    printf("done\n");
    return 0;
}
