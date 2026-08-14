/* C++11 surface syntax: `auto`, range-`for`, namespaces, smart pointers.
 *
 * None of these change what the subset can express -- they are spellings.
 * Each is rewritten into something the lowering already handled, before any
 * pass that reads types runs, because everything downstream reads types by
 * how they are written:
 *
 *   namespaces   flattened to `N_x`, the same thing Crust does with Rust
 *                paths, and for the same reason: C has one namespace
 *   range-`for`  becomes the index loop it stands for
 *   `auto`       becomes the type that was written somewhere nearby
 *
 * The smart pointers are not rewritten at all -- they are written *in this
 * subset* and supplied when named, like `string` and `vector`. Every feature
 * they need is one the subset already claims, so if they compile the claim
 * holds.
 */
#include <memory>
#include <vector>

int released = 0;

class Sample {
public:
    int v;
    Sample() { v = 0; }
    ~Sample() { released = released + 1; }
};

namespace stats {

    /* A namespace member. Flattened to `stats_total`, and the class below
     * to `stats_Bag` -- but `Bag::items` stays `items`, because a member is
     * not a namespace name. */
    class Bag {
    public:
        std::vector<int> items;
        void add(int n) { items.push_back(n); }
        int size() { return items.size(); }
        int &operator[](int i) { return items[i]; }
    };

    int total(Bag *b) {
        int sum = 0;
        /* Range-`for` over a class with `size()` and `operator[]`. The
         * by-value form declares a copy, so `x` is an ordinary `int`. */
        for (auto x : *b) { sum = sum + x; }
        return sum;
    }

    void scale(Bag *b, int by) {
        /* The reference form aliases the element, so writing through it
         * writes to the container -- which is what a reference means. */
        for (auto &x : *b) { x = x * by; }
    }
}

int collect(void) {
    stats::Bag b;
    b.add(1);
    b.add(2);
    b.add(3);
    stats::scale(&b, 10);
    /* `auto` from a function whose return type is written. */
    auto sum = stats::total(&b);
    return sum;
}

int owned(void) {
    /* `unique_ptr` declares no copy constructor, so the Rule of Three
     * refusal the subset already makes *is* its move-only semantics. */
    std::unique_ptr<Sample> u(new Sample());
    u.get()->v = 4;

    /* `shared_ptr` refcounts: the copy constructor increments, the
     * destructor decrements and releases at zero. */
    std::shared_ptr<Sample> a(new Sample());
    long inner = 0;
    {
        std::shared_ptr<Sample> b(a);
        inner = b.use_count();
    }
    /* Both `Sample`s are released on the way out of this function, and the
     * element destructor really runs -- a plain `delete` inside a template
     * would free without calling it. */
    return u.get()->v * 100 + (int)inner * 10 + (int)a.use_count();
}
