/* C++11 spellings on the C++ side, Rust on the other, one translation unit.
 *
 * The point of the pairing: `auto`, range-`for`, namespaces and the smart
 * pointers are all resolved before the lowering runs, so what reaches Crust
 * and ShivyCX is the same plain C every other example produces. Nothing here
 * is marshalled, and a Rust `fn` calls into the C++ directly.
 */
#include "cpp11.cpp"

int printf(const char *, ...);

/* Rust reducing what the C++ produced. */
fn describe(sum: i32, owned_code: i32) -> i32 {
    let mut parts: Vec<i32> = Vec::<i32>::new();
    parts.push(sum);
    parts.push(owned_code);
    let mut acc: i32 = 0;
    while parts.len() > 0 {
        acc += parts.pop();
    }
    acc
}

int main(void) {
    int sum = collect();
    int code = owned();

    printf("sum      = %d\n", sum);
    printf("owned    = %d\n", code);
    printf("released = %d\n", released);
    printf("combined = %d\n", describe(sum, code));
    return 42;
}
