// A struct member named like a typedef does not hide the typedef.
//
// Members live in their struct's own namespace, so after `struct Q { In
// In; };` the name `In` is still the typedef: `In other;` declares one.
// ShivyC used to register member names in the enclosing ordinary scope,
// which made `In` an ordinary identifier there and that declaration a
// parse error. gcc -std=c99 -pedantic accepts all of this.
//
// C# lowers a field named after its own type (`public In In;`) to exactly
// this shape, which is how it was found.

typedef struct In { int V; } In;
struct Q { In In; int n; };

struct R { struct Q q; In In; };

int main() {
  In other;
  struct Q q;
  struct R r;

  other.V = 3;
  q.In = other;
  q.n = 4;
  r.q = q;
  r.In.V = 5;

  In last = r.In;
  return q.In.V + r.q.n + last.V - 12;
}

// Return: 0
