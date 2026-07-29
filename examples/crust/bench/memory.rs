/* Array and slice traffic: bounds-free indexing, slice iteration, and a
 * struct-of-arrays walk. Measures address generation more than arithmetic. */
int printf(const char *, ...);

struct Table { keys: [u64; 512], vals: [u64; 512] }

fn fill(t: *mut Table) {
    for i in 0..512 {
        t.keys[i] = (i * 2654435761) & 0xFFFFFFFF;
        t.vals[i] = i;
    }
}

fn probe(t: *mut Table, rounds: u64) -> u64 {
    let mut hits: u64 = 0;
    for r in 0..rounds {
        let i: u64 = r & 511;
        if t.keys[i] > 1000 { hits += t.vals[i]; }
    }
    hits
}

fn slice_sum(xs: &[u64]) -> u64 {
    let mut s: u64 = 0;
    for x in xs { s += x; }
    s
}

fn main() {
    let mut t: Table = Table { keys: [0; 512], vals: [0; 512] };
    fill(&t);
    let mut total: u64 = probe(&t, 2000000);
    for _r in 0..500 { total += slice_sum(&t.vals[0..512]); }
    printf("%lu\n", total);
}
