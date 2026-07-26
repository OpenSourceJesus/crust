// enum, match, const, tuple structs and array literals, in an all-Rust file.

const LEN: usize = 5;

enum Kind {
    Digit,
    Space,
    Letter,
    Other,
}

// A tuple struct: fields are reached as `.0`, `.1`, ...
struct Counts(i32, i32, i32);

fn classify(c: i32) -> Kind {
    if c >= 48 && c <= 57 {
        Kind::Digit
    } else if c == 32 {
        Kind::Space
    } else if c >= 97 && c <= 122 {
        Kind::Letter
    } else {
        Kind::Other
    }
}

// `match` on an enum is checked for exhaustiveness: drop an arm and the
// compiler names the variant that is missing.
fn weight(k: Kind) -> i32 {
    match k {
        Kind::Digit => 1,
        Kind::Letter => 10,
        Kind::Space | Kind::Other => 0,
    }
}

fn tally(text: *const i32, n: usize) -> Counts {
    let mut digits: i32 = 0;
    let mut letters: i32 = 0;
    let mut score: i32 = 0;
    for i in 0..n {
        let k: Kind = classify(text[i]);
        score += weight(k);
        digits += if weight(k) == 1 { 1 } else { 0 };
        letters += if weight(k) == 10 { 1 } else { 0 };
    }
    Counts(digits, letters, score)
}

fn main() -> i32 {
    // "a1 b2" as codepoints
    let text: [i32; LEN] = [97, 49, 32, 98, 50];
    let c: Counts = tally(text, LEN);
    c.0 * 100 + c.1 * 10 + c.2
}
