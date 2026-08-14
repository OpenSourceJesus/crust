#include <string>
#include <vector>
int printf(const char *fmt, ...);

int main(void) {
    std::vector<std::string*> v;
    std::string *a = new std::string("alpha");
    std::string *b = new std::string("beta");
    v.push_back(a);
    v.push_back(b);
    int i = 0;
    while (i < v.size()) { printf("%d=%s\n", i, v[i]->c_str()); i = i + 1; }
    i = 0;
    while (i < v.size()) { std::string *p = v[i]; delete p; i = i + 1; }
    return 0;
}
