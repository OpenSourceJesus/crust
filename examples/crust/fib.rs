fn fib(n: i32) -> i64 {
    if n < 2 {
        n as i64
    } else {
        fib(n - 1) + fib(n - 2)
    }
}

fn main() -> i32 {
    let mut total: i64 = 0;
    for i in 0..25 {
        total += fib(i);
    }
    (total % 251) as i32
}
