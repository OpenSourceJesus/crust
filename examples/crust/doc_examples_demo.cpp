#include <string>
#include <vector>
int printf(const char *fmt, ...);
class Shape {
public:
    int id;
    Shape(int i) { id = i; }
    virtual ~Shape() { }
    virtual int area() { return 0; }
};
class Square : public Shape {
public:
    int side;
    Square(int i, int s) : Shape(i) { side = s; }
    ~Square() { }
    int area() { return side * side; }
};
template<typename K, typename V>
class Pair {
    K key;
    V val;
public:
    Pair(K k, V v) { key = k; val = v; }
    K first() { return key; }
};
int main(void) {
    Shape *s = new Square(1, 3);
    printf("%d\n", s->area());
    delete s;

    Pair<int, double> p(1, 2.0);
    Pair<char, int> q(65, 9);
    printf("p=%d q=%d\n", p.first(), q.first());

    std::string str("hello");
    str.append(", world");
    str[0] = 'H';
    printf("%s %d\n", str.c_str(), str.size());

    std::vector<int> v;
    v.push_back(1);
    v[0] = 42;
    printf("v0=%d\n", v[0]);

    std::ownvector<std::string> ov;
    std::string a("alpha");
    ov.push_back(a);
    a.assign("changed");
    printf("ov0=%s a=%s\n", ov[0].c_str(), a.c_str());
    return 0;
}
