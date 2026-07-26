// Result<T, E> and `?`: error propagation without sentinel returns.

enum ParseError {
    Empty,
    BadDigit,
    Overflow,
}

const LIMIT: i32 = 100000;

// Parse a decimal string into an i32, reporting *why* it failed.
fn parse_i32(s: &str) -> Result<i32, ParseError> {
    let n: usize = s.len();
    if n == 0 {
        return Err(ParseError::Empty);
    }
    let mut acc: i32 = 0;
    for i in 0..n {
        let c: i32 = s[i] as i32;
        if c < 48 || c > 57 {
            return Err(ParseError::BadDigit);
        }
        acc = acc * 10 + (c - 48);
        if acc > LIMIT {
            return Err(ParseError::Overflow);
        }
    }
    Ok(acc)
}

// `?` unwraps on success and returns the error otherwise, so the happy path
// reads top to bottom with no error branches in the way.
fn sum_pair(a: &str, b: &str) -> Result<i32, ParseError> {
    let x: i32 = parse_i32(a)?;
    let y: i32 = parse_i32(b)?;
    Ok(x + y)
}

fn code(e: ParseError) -> i32 {
    match e {
        ParseError::Empty => 1,
        ParseError::BadDigit => 2,
        ParseError::Overflow => 3,
    }
}

fn main() -> i32 {
    let good: Result<i32, ParseError> = sum_pair("20", "22");
    let bad: Result<i32, ParseError> = sum_pair("20", "x2");
    let empty: Result<i32, ParseError> = sum_pair("", "1");

    let mut total: i32 = good.unwrap();
    total += code(bad.unwrap_err()) * 0;

    // `.ok()` drops the error, turning a Result into an Option
    let maybe: Option<i32> = empty.ok();
    total += maybe.unwrap_or(0);
    total
}
