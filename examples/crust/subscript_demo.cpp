#include <string>
#include <vector>
int printf(const char *fmt, ...);
int main(void) {
    std::vector<int> v;
    int i = 0;
    while (i < 5) { v.push_back(i); i = i + 1; }
    v[0] = 100;
    v[1] = v[0] + v[2];
    printf("v0=%d v1=%d v4=%d size=%d\n", v[0], v[1], v[4], v.size());

    std::string s("abc");
    s[0] = 'X';
    printf("s=%s s1=%c\n", s.c_str(), s[1]);
    return 0;
}
