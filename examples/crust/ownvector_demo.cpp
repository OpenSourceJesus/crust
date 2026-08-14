#include <string>
int printf(const char *fmt, ...);

int main(void) {
    std::ownvector<std::string> v;
    std::string a("alpha");
    std::string b("beta");
    v.push_back(a);
    v.push_back(b);
    a.assign("MUTATED");
    int i = 0;
    while (i < v.size()) { printf("%d=%s\n", i, v[i].c_str()); i = i + 1; }
    printf("size=%d a=%s\n", v.size(), a.c_str());
    return 0;
}
