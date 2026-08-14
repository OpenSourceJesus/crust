#include <string>
#include <vector>
int printf(const char *fmt, ...);

class Point {
public:
    int x;
    int y;
    Point() { x = 0; y = 0; }
    Point(int a) { x = a; y = a; }
    Point(int a, int b) { x = a; y = b; }
};

int main(void) {
    Point p;
    Point q(5);
    Point r(2, 3);
    printf("p=%d,%d q=%d,%d r=%d,%d\n", p.x, p.y, q.x, q.y, r.x, r.y);

    Point *h = new Point(7, 8);
    printf("heap=%d,%d\n", h->x, h->y);
    delete h;

    std::string s("hello");
    printf("s=%s len=%d\n", s.c_str(), s.size());

    std::vector<int> v(16);
    v.push_back(42);
    printf("v0=%d size=%d\n", v.get(0), v.size());
    return 0;
}
