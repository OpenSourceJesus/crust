#include <string>
#include <vector>
int printf(const char *fmt, ...);

static int square(int v) { return v * v; }

class Tally {
public:
    std::vector<int> nums;
    std::string label;
    Tally() { }
    void add(int v) { nums.push_back(v); }
    int total() {
        int t = 0;
        int i = 0;
        while (i < nums.size()) { t = t + nums.get(i); i = i + 1; }
        return t;
    }
};

int mapsum(std::vector<int> &v, int (*fn)(int)) {
    int t = 0;
    int i = 0;
    while (i < v.size()) { t = t + fn(v.get(i)); i = i + 1; }
    return t;
}

int main(void) {
    Tally t;
    t.label.assign("counts");
    t.add(1); t.add(2); t.add(3);
    printf("%s total=%d\n", t.label.c_str(), t.total());

    auto cube = [](int v) -> int { return v * v * v; };
    printf("squares=%d cubes=%d\n", mapsum(t.nums, square), mapsum(t.nums, cube));

    std::vector<int> copy = t.nums;
    copy.set(0, 100);
    printf("copy0=%d orig0=%d\n", copy.get(0), t.nums.get(0));
    return 0;
}
