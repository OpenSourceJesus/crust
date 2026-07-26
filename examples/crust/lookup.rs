// Option<T>, if let and while let: fallible lookup without sentinel values.

struct Entry {
    key: i32,
    value: i32,
}

impl Entry {
    fn new(key: i32, value: i32) -> Entry {
        Entry { key: key, value: value }
    }
}

// Returning Option<i32> instead of a magic -1 makes "not found" a type the
// caller cannot forget to check.
fn find(entries: &[Entry], key: i32) -> Option<i32> {
    for i in 0..entries.len() {
        if entries[i].key == key {
            return Some(entries[i].value);
        }
    }
    None
}

static mut CURSOR: usize = 0;

fn drain(entries: &[Entry]) -> Option<i32> {
    if CURSOR >= entries.len() {
        return None;
    }
    let v: i32 = entries[CURSOR].value;
    CURSOR += 1;
    Some(v)
}

fn main() -> i32 {
    let mut table: [Entry; 3] = [
        Entry::new(1, 10),
        Entry::new(2, 20),
        Entry::new(3, 12),
    ];
    let entries: &[Entry] = &table[..];
    let mut total: i32 = 0;

    // if let binds the payload only on the Some branch
    if let Some(v) = find(entries, 2) {
        total += v;
    } else {
        total += 100;
    }

    // a miss takes the else branch, and unwrap_or supplies a default
    if let Some(v) = find(entries, 99) {
        total += v;
    }
    total += find(entries, 99).unwrap_or(0);

    // while let loops until the producer returns None
    while let Some(v) = drain(entries) {
        total += v;
    }
    total
}
