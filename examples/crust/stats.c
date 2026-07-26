/* Slices, &str and associated constants, called from C. */
int printf(const char *, ...);

struct Stats {
    count: i32,
    total: i32,
    max: i32,
}

impl Stats {
    const EMPTY_MAX: i32 = -2147483647;

    fn of(xs: &[i32]) -> Stats {
        let mut s: Stats = Stats {
            count: xs.len() as i32,
            total: 0,
            max: Stats::EMPTY_MAX,
        };
        for i in 0..xs.len() {
            s.total += xs[i];
            if xs[i] > s.max {
                s.max = xs[i];
            }
        }
        s
    }

    fn mean(&self) -> i32 {
        if self.count == 0 { 0 } else { self.total / self.count }
    }
}

/* &str lowers to const char *, so it is directly printable from C. */
fn label() -> &str {
    "sample"
}

int main(void) {
    int data[6] = {3, 9, 4, 1, 5, 8};
    /* C builds the fat pointer the same way Crust does. */
    crust_slice_int all = (crust_slice_int){data, 6};
    Stats s = Stats_of(all);
    printf("%s: n=%d total=%d max=%d mean=%d\n",
           label(), s.count, s.total, s.max, Stats_mean(&s));
    return 0;
}
