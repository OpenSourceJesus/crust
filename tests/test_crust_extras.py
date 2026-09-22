"""Further Crust front-end tests, kept apart from `test_crust.py` for size.

`test_crust.py` had grown past 5000 lines; new feature work lands here. The
compile-and-run helpers are shared with it rather than copied, so both files
build and run programs exactly the same way.
"""

import os
import subprocess
import tempfile
import unittest

import shivyc.crust as crust
from tests.test_crust import _run, _COUNTED, _ROOT


def _rs(source):
    """Compile and run an all-Rust program; return its exit status."""
    return _run(source, suffix=".rs")


def _rs_stderr(source):
    """Compile and run an all-Rust program; return (exit status, stderr)."""
    workdir = tempfile.mkdtemp()
    src = os.path.join(workdir, "prog.rs")
    out = os.path.join(workdir, "prog")
    with open(src, "w") as f:
        f.write(source)
    proc = subprocess.run(
        ["python3", "-m", "shivyc.main", src, "-o", out],
        cwd=_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    run = subprocess.run([out], capture_output=True, text=True, timeout=30)
    return run.returncode, run.stderr


class TestMatchExpression(unittest.TestCase):
    """`match` in value position: `let y = match x { .. };`."""

    def test_integer_match_as_a_value(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let x: i32 = 3;
    let y: i32 = match x { 1 => 10, 3 => 40, _ => 0 };
    y + 2
}
"""), 42)

    def test_type_is_inferred_from_the_arms(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let x: i32 = 2;
    let y = match x { 1 => 1, _ => 42 };
    y
}
"""), 42)

    def test_match_inside_a_larger_expression(self):
        self.assertEqual(_rs("""
fn f(x: i32) -> i32 { 2 * match x { 0 => 20, _ => 1 } + 2 }
fn main() -> i32 { f(0) }
"""), 42)

    def test_block_arms_run_statements_before_their_value(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let x: i32 = 5;
    let y: i32 = match x {
        5 => {
            let a: i32 = 20;
            let b: i32 = 22;
            a + b
        }
        _ => 0,
    };
    y
}
"""), 42)

    def test_nested_if_and_match_in_an_arm(self):
        self.assertEqual(_rs("""
fn pick(a: i32, b: i32) -> i32 {
    match a {
        0 => if b > 0 { 40 } else { 1 },
        _ => match b { 0 => 2, _ => 3 },
    }
}
fn main() -> i32 {
    let v: i32 = match 1 { _ => pick(0, 1) };
    v + pick(9, 0)
}
"""), 42)

    def test_enum_match_as_a_value(self):
        self.assertEqual(_rs("""
enum Op { Add, Mul }
fn apply(op: Op, a: i32, b: i32) -> i32 {
    let r: i32 = match op { Op::Add => a + b, Op::Mul => a * b };
    r
}
fn main() -> i32 { apply(Op::Mul, 5, 8) + apply(Op::Add, 1, 1) }
"""), 42)

    def test_data_enum_match_as_a_value(self):
        self.assertEqual(_rs("""
enum Shape { Sq(i32), Rect { w: i32, h: i32 }, Empty }
fn main() -> i32 {
    let s: Shape = Shape::Rect { w: 4, h: 10 };
    let a: i32 = match s {
        Shape::Sq(n) => n * n,
        Shape::Rect { w, h } => w * h,
        Shape::Empty => 0,
    };
    a + 2
}
"""), 42)

    def test_arms_resolve_none_and_ok_from_the_target(self):
        self.assertEqual(_rs("""
fn half(x: i32) -> Option<i32> {
    let r: Option<i32> = match x % 2 { 0 => Some(x / 2), _ => None };
    r
}
fn main() -> i32 {
    if half(3).is_some() { return 1; }
    half(84).unwrap()
}
"""), 42)

    def test_outer_hoisted_work_stays_in_scope(self):
        # The match text becomes a braced block; the `?` temporary hoisted
        # before it must not be swallowed into that block's scope.
        self.assertEqual(_rs("""
fn g(x: i32) -> Option<i32> { Some(x) }
fn f(k: i32) -> Option<i32> {
    let v: i32 = g(40)? + match k { 0 => 2, _ => 0 };
    Some(v)
}
fn main() -> i32 { f(0).unwrap() }
"""), 42)

    def test_scrutinee_is_evaluated_once(self):
        c = crust.translate("""
fn k() -> i32 { 1 }
fn f() -> i32 { match k() { 1 => 2, _ => 3 } }
fn g() -> Option<i32> { Some(1) }
fn h() -> i32 { match g() { Some(v) => v, None => 0 } }
""")
        self.assertEqual(c.count("k()"), 1)
        self.assertEqual(c.count("g()"), 1)

    def test_unannotated_none_only_match_is_an_error(self):
        with self.assertRaises(crust.CrustError):
            crust.translate("fn f(x: i32) { let y = match x { _ => None }; }")
        # ...while the annotated form is fine.
        crust.translate("fn f(x: i32) { let y: Option<i32> = "
                        "match x { _ => None }; }")


class TestMatchPatterns(unittest.TestCase):
    """Patterns beyond constant labels: the if/else-chain lowering."""

    def test_option(self):
        self.assertEqual(_rs("""
fn get(k: i32) -> Option<i32> { if k > 0 { Some(k) } else { None } }
fn main() -> i32 {
    let mut t: i32 = 0;
    match get(40) { Some(v) => t += v, None => t += 100 }
    match get(-1) { Some(v) => t += 100, None => t += 2 }
    t
}
"""), 42)

    def test_result(self):
        self.assertEqual(_rs("""
fn check(x: i32) -> Result<i32, i32> { if x >= 0 { Ok(x) } else { Err(-x) } }
fn score(x: i32) -> i32 {
    match check(x) {
        Ok(v) => v,
        Err(e) => e * 10,
    }
}
fn main() -> i32 { score(12) + score(-3) }
"""), 42)

    def test_nested_payload_patterns(self):
        self.assertEqual(_rs("""
fn f(o: Option<i32>) -> i32 {
    match o {
        Some(0) => 1,
        Some(1..=9) => 10,
        Some(n) => n,
        None => 0,
    }
}
fn main() -> i32 { f(Some(0)) + f(Some(5)) + f(Some(31)) + f(None) }
"""), 42)

    def test_guards(self):
        self.assertEqual(_rs("""
fn classify(x: i32) -> i32 {
    match x {
        n if n < 0 => 1,
        n if n % 2 == 0 => 2,
        _ => 3,
    }
}
fn main() -> i32 { classify(-5) * 10 + classify(4) * 10 + classify(7) * 4 }
"""), 42)

    def test_guard_falls_through_to_later_arms(self):
        self.assertEqual(_rs("""
fn f(o: Option<i32>) -> i32 {
    match o {
        Some(v) if v > 100 => 1,
        Some(v) => v,
        None => 0,
    }
}
fn main() -> i32 { f(Some(41)) + f(Some(500)) }
"""), 42)

    def test_ranges(self):
        self.assertEqual(_rs("""
fn bucket(x: i32) -> i32 {
    match x {
        ..=0 => 0,
        1..=9 => 1,
        10..100 => 2,
        _ => 3,
    }
}
fn main() -> i32 {
    bucket(-4) + bucket(3) * 2 + bucket(50) * 4 + bucket(1000) * 10 + 2
}
"""), 42)

    def test_char_ranges(self):
        self.assertEqual(_rs("""
fn kind(c: char) -> i32 {
    match c {
        'a'..='z' => 1,
        '0'..='9' => 2,
        _ => 3,
    }
}
fn main() -> i32 { kind('q') * 10 + kind('7') * 10 + kind('!') * 4 }
"""), 42)

    def test_top_level_binding_is_a_catch_all(self):
        self.assertEqual(_rs("""
fn f(x: i32) -> i32 { match x { 0 => 1, other => other + 1 } }
fn main() -> i32 { f(41) }
"""), 42)

    def test_at_binding(self):
        self.assertEqual(_rs("""
fn f(x: i32) -> i32 { match x { n @ 1..=50 => n + 1, _ => 0 } }
fn main() -> i32 { f(41) }
"""), 42)

    def test_tuple_scrutinee(self):
        self.assertEqual(_rs("""
fn f(a: i32, b: bool) -> i32 {
    match (a, b) {
        (0, true) => 1,
        (0, false) => 2,
        (n, true) => n,
        (_, false) => 0,
    }
}
fn main() -> i32 { f(0, true) + f(0, false) + f(39, true) + f(7, false) }
"""), 42)

    def test_bool_is_exhaustive_without_a_wildcard(self):
        self.assertEqual(_rs("""
fn f(b: bool) -> i32 { match b { true => 40, false => 2 } }
fn main() -> i32 { f(true) + f(false) }
"""), 42)

    def test_match_on_a_reference_to_an_enum(self):
        self.assertEqual(_rs("""
enum Shape { Sq(i32), Empty }
impl Shape {
    fn area(&self) -> i32 {
        match self {
            Shape::Sq(n) => n * n,
            Shape::Empty => 0,
        }
    }
    fn is_empty(&self) -> bool { matches!(self, Shape::Empty) }
}
fn main() -> i32 {
    let s: Shape = Shape::Sq(6);
    let e: Shape = Shape::Empty;
    if !e.is_empty() { return 1; }
    s.area() + e.area() + 6
}
"""), 42)

    def test_self_variant_paths(self):
        self.assertEqual(_rs("""
enum Dir { Up, Down }
impl Dir {
    fn sign(&self) -> i32 { match self { Self::Up => 1, Self::Down => -1 } }
}
fn main() -> i32 { let d: Dir = Dir::Down; 43 + d.sign() }
"""), 42)

    def test_simple_matches_still_lower_to_a_switch(self):
        c = crust.translate("""
enum C { R, G }
fn f(c: C) -> i32 { match c { C::R => 1, C::G => 2 } }
fn g(x: i32) -> i32 { match x { 1 | 2 => 3, _ => 4 } }
""")
        self.assertEqual(c.count("switch ("), 2)


class TestMatchExhaustiveness(unittest.TestCase):

    def test_missing_option_arm_is_reported(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f(o: Option<i32>) -> i32 { match o { Some(v) => v } }
""")
        self.assertIn("None", str(cm.exception))

    def test_guarded_arm_does_not_count_as_coverage(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f(o: Option<i32>) -> i32 {
    match o { Some(v) if v > 0 => v, None => 0 }
}
""")
        self.assertIn("non-exhaustive", str(cm.exception))
        self.assertIn("Some", str(cm.exception))

    def test_refutable_payload_does_not_cover_its_variant(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f(r: Result<i32, i32>) -> i32 { match r { Ok(0) => 1, Err(_) => 2 } }
""")
        self.assertIn("non-exhaustive", str(cm.exception))
        self.assertIn("Ok", str(cm.exception))

    def test_missing_data_enum_variant_is_reported(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
enum E { A(i32), B, C }
fn f(e: E) -> i32 { match e { E::A(n) if n > 0 => n, E::B => 0 } }
""")
        self.assertIn("`A`", str(cm.exception))
        self.assertIn("`C`", str(cm.exception))

    def test_unprovable_integer_match_traps_instead_of_falling_off(self):
        # Crust cannot prove range coverage, so the chain ends in `abort()`
        # rather than leaving the value unassigned.
        c = crust.translate("""
fn f(x: u8) -> i32 { match x { 0..=127 => 1, 128..=255 => 2 } }
""")
        self.assertIn("abort()", c)


class TestMatchArmControlFlow(unittest.TestCase):

    def test_return_break_continue_as_arm_bodies(self):
        self.assertEqual(_rs("""
fn first_even(xs: &[i32]) -> i32 {
    let mut found: i32 = -1;
    let mut i: usize = 0;
    while i < xs.len() {
        let x: i32 = xs[i];
        i += 1;
        match x % 2 {
            0 => { found = x; break; }
            _ => continue,
        }
    }
    found
}
fn f(o: Option<i32>) -> i32 {
    let v: i32 = match o { Some(v) => v, None => return 2 };
    v * 2
}
fn main() -> i32 {
    // 8 after 20: a `break` that only leaves the `switch` finds 8.
    let a: [i32; 5] = [3, 5, 20, 8, 7];
    first_even(&a[..]) + f(None) * 5 + f(Some(6))     // 20 + 10 + 12
}
"""), 42)

    def test_panicking_arm_in_a_value_match(self):
        self.assertEqual(_rs("""
struct P { x: i32 }
fn get(k: i32) -> P {
    match k {
        1 => P { x: 42 },
        _ => panic!("no such key"),
    }
}
fn main() -> i32 { get(1).x }
"""), 42)

    def test_early_return_from_a_value_match_drops_locals(self):
        self.assertEqual(_rs(_COUNTED + """
fn f(o: Option<i32>) -> i32 {
    let a: G = g(1);
    let v: i32 = match o { Some(v) => v, None => return 0 };
    v + a.id
}
fn main() -> i32 {
    let r: i32 = f(None) + f(Some(40));
    if live() != 0 { return 1; }
    r + 1
}
"""), 42)

    def test_arm_block_locals_are_dropped(self):
        self.assertEqual(_rs(_COUNTED + """
fn f(o: Option<i32>) -> i32 {
    match o {
        Some(v) => { let a: G = g(v); a.id }
        None => 0,
    }
}
fn main() -> i32 {
    let r: i32 = f(Some(42)) + f(None);
    if live() != 0 { return 1; }
    r
}
"""), 42)


class TestBlockExpressionScope(unittest.TestCase):
    """A block expression must not capture the statement's earlier work."""

    def test_try_before_a_block_expression(self):
        self.assertEqual(_rs("""
fn g(x: i32) -> Option<i32> { Some(x) }
fn f() -> Option<i32> {
    let v: i32 = g(40)? + { let a: i32 = 1; a + 1 };
    Some(v)
}
fn main() -> i32 { f().unwrap() }
"""), 42)


class TestTailPosition(unittest.TestCase):
    """Only the last statement of a block is its value.

    `match`/`if` statements were handed the function's tail flag wherever
    they stood, so a mid-body one *returned*: this program exited 1.
    """

    def test_mid_body_match_and_if_do_not_return(self):
        self.assertEqual(_rs("""
fn f(x: i32) -> i32 {
    let mut t: i32 = 0;
    match x { 1 => t += 1, _ => t += 2 }
    if x > 0 { t += 10 }
    { t += 1 }
    t + 30
}
fn main() -> i32 { f(1) }
"""), 42)

    def test_trailing_match_and_if_still_return(self):
        self.assertEqual(_rs("""
fn a(x: i32) -> i32 { match x { 1 => 40, _ => 0 } }
fn b(x: i32) -> i32 { if x > 0 { 2 } else { 0 } }
fn main() -> i32 { a(1) + b(1) }
"""), 42)

    def test_else_chain_is_one_statement(self):
        self.assertEqual(_rs("""
fn f(x: i32) -> i32 { if x == 0 { 1 } else if x == 1 { 42 } else { 3 } }
fn main() -> i32 { f(1) }
"""), 42)


class TestBlockExpressions(unittest.TestCase):
    """Blocks in value position, lowered through the same tail machinery."""

    def test_try_inside_a_block_runs_once(self):
        self.assertEqual(_rs("""
static mut CALLS: i32 = 0;
fn g(x: i32) -> Option<i32> { unsafe { CALLS += 1; } Some(x) }
fn f() -> Option<i32> {
    let v: i32 = { g(1)?; 40 };
    Some(v)
}
fn main() -> i32 { let v: i32 = f().unwrap(); v + unsafe { CALLS } * 2 }
"""), 42)

    def test_move_in_a_block_statement_is_not_a_false_use_after_move(self):
        self.assertEqual(_rs("""
fn take(v: Vec<i32>) -> usize { v.len() }
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(1);
    let n: usize = { take(v); 41 };
    n as i32 + 1
}
"""), 42)

    def test_block_locals_are_dropped_and_a_moved_out_local_is_kept(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    let n: i32 = { let a: G = g(40); a.id };
    let b: G = { let c: G = g(2); c };
    if live() != 1 { return 1; }
    n + b.id
}
"""), 42)

    def test_if_and_match_as_a_block_tail(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let x: i32 = 3;
    let a: i32 = { let y: i32 = x * 2; if y > 5 { 40 } else { 0 } };
    let b: i32 = { match x { 3 => 2, _ => 0 } };
    a + b
}
"""), 42)

    def test_wider_arm_type_wins(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let big: i64 = 4294967296;
    let k: i32 = 0;
    let v = match k { 1 => 1, _ => big };
    (v / 4294967296) as i32 + 41
}
"""), 42)


class TestBreakInsideSwitch(unittest.TestCase):
    """A constant-arm `match` is a C `switch`, where `break` means the switch.

    A Rust `break` in such an arm used to leave only the switch, and the loop
    carried on: this program exited 78.
    """

    def test_break_in_a_switch_arm_leaves_the_loop(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut i: i32 = 0;
    loop {
        i += 1;
        match i { 5 => { break; } _ => {} }
        if i > 40 { break; }
    }
    i + 37
}
"""), 42)

    def test_break_in_a_data_enum_switch_arm(self):
        self.assertEqual(_rs("""
enum Ev { Tick, Stop(i32) }
fn ev(i: i32) -> Ev { if i == 3 { Ev::Stop(39) } else { Ev::Tick } }
fn main() -> i32 {
    let mut i: i32 = 0;
    let mut code: i32 = 0;
    while i < 100 {
        i += 1;
        match ev(i) { Ev::Stop(c) => { code = c; break; } Ev::Tick => continue }
    }
    code + i
}
"""), 42)

    def test_a_loop_inside_a_switch_arm_breaks_itself(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut t: i32 = 0;
    match 1 {
        1 => { let mut k: i32 = 0; while true { k += 1; if k == 42 { break; } } t = k; }
        _ => {}
    }
    t
}
"""), 42)


class TestLoopValues(unittest.TestCase):
    """`loop` as an expression, with `break value`."""

    def test_break_with_a_value(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut i: i32 = 0;
    let r: i32 = loop { i += 1; if i == 21 { break i * 2; } };
    r
}
"""), 42)

    def test_loop_as_the_function_tail(self):
        self.assertEqual(_rs("""
fn find(xs: &[i32], want: i32) -> i32 {
    let mut i: usize = 0;
    loop {
        if i == xs.len() { break -1; }
        if xs[i] == want { break i as i32; }
        i += 1;
    }
}
fn main() -> i32 {
    let a: [i32; 4] = [5, 6, 7, 8];
    find(&a[..], 7) * 20 + find(&a[..], 9) + 3
}
"""), 42)

    def test_break_value_type_comes_from_the_target(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut i: i32 = 0;
    let o: Option<i32> = loop {
        i += 1;
        if i > 100 { break None; }
        if i == 42 { break Some(i); }
    };
    o.unwrap()
}
"""), 42)

    def test_loop_inside_a_larger_expression(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut n: i32 = 0;
    let v: i32 = 2 + loop { n += 1; if n == 40 { break n; } };
    v
}
"""), 42)

    def test_break_value_drops_the_loop_body_locals(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    let mut i: i32 = 0;
    let r: i32 = loop {
        let a: G = g(i + 1);
        i += 1;
        if i == 3 { break a.id * 14; }
    };
    if live() != 0 { return 1; }
    r
}
"""), 42)

    def test_break_can_move_an_owning_value_out(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut i: i32 = 0;
    let v: Vec<i32> = loop {
        let mut w: Vec<i32> = Vec::<i32>::new();
        w.push(i);
        i += 1;
        if i == 42 { break w; }
    };
    v.get(0) + 1
}
"""), 42)

    def test_break_value_outside_loop_is_rejected(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f() { while true { break 1; } }")
        self.assertIn("only allowed in `loop`", str(cm.exception))

    def test_diverging_loop_as_a_tail(self):
        c = crust.translate("""
fn serve(n: i32) -> i32 { loop { if n > 0 { return n; } } }
""")
        self.assertIn("while (1)", c)


class TestLoopLabels(unittest.TestCase):

    def test_labeled_break_leaves_the_outer_loop(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut found: i32 = 0;
    'outer: for i in 0..10 {
        for j in 0..10 {
            if i * j == 42 { found = i * 10 + j; break 'outer; }
        }
    }
    found - 25
}
"""), 42)

    def test_labeled_continue_runs_the_outer_increment(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut t: i32 = 0;
    'rows: for i in 0..6 {
        for j in 0..6 {
            if j > i { continue 'rows; }
            t += 1;
        }
    }
    t + 21
}
"""), 42)

    def test_labeled_break_with_a_value(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut i: i32 = 0;
    let r: i32 = 'search: loop {
        i += 1;
        let mut j: i32 = 0;
        while j < 10 {
            if i * j == 42 { break 'search i + j; }
            j += 1;
        }
    };
    r * 3 + 3                  // i = 6, j = 7
}
"""), 42)

    def test_labeled_break_drops_every_frame_it_leaves(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    'outer: loop {
        let a: G = g(1);
        loop {
            let b: G = g(2);
            break 'outer;
        }
    }
    if live() != 0 { return 1; }
    42
}
"""), 42)

    def test_while_let_and_for_each_take_labels(self):
        self.assertEqual(_rs("""
fn next(k: i32) -> Option<i32> { if k < 10 { Some(k + 1) } else { None } }
fn main() -> i32 {
    let a: [i32; 3] = [1, 2, 3];
    let mut t: i32 = 0;
    let mut k: i32 = 0;
    'w: while let Some(n) = next(k) {
        k = n;
        'f: for x in a {
            if x == 2 { continue 'w; }
            t += x;
        }
    }
    t + 32
}
"""), 42)

    def test_lifetimes_are_still_dropped(self):
        c = crust.translate("""
struct S<'a> { p: &'a i32 }
fn f<'a, 'b: 'a>(x: &'a i32, y: &'b i32) -> &'a i32 { x }
""")
        self.assertIn("int *f(int *x, int *y)", c)

    def test_unknown_label_is_reported(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f() { loop { break 'nowhere; } }")
        self.assertIn("nowhere", str(cm.exception))

    def test_break_outside_a_loop_is_reported(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f() { break; }")
        self.assertIn("outside", str(cm.exception))


class TestOptionMethods(unittest.TestCase):

    def test_map_and_friends_with_capturing_closures(self):
        self.assertEqual(_rs("""
fn get(k: i32) -> Option<i32> { if k > 0 { Some(k) } else { None } }
fn main() -> i32 {
    let off: i32 = 10;
    let a: Option<i32> = get(5).map(|x| x + off);
    let b: Option<i32> = get(-1).map(|x| x + off);
    let c: i32 = get(3).map_or(0, |x| x * off);
    let d: i32 = get(-3).map_or(7, |x| x * off);
    if b.is_some() { return 1; }
    a.unwrap() + c - d - 8               // 15 + 30 - 7 - 8
}
"""), 30)

    def test_closure_body_runs_only_on_its_branch(self):
        self.assertEqual(_rs("""
static mut CALLS: i32 = 0;
fn slow(v: i32) -> i32 { unsafe { CALLS += 1; } v }
fn main() -> i32 {
    let s: Option<i32> = Some(40);
    let n: Option<i32> = None;
    let a: i32 = s.unwrap_or_else(|| slow(100));
    let b: i32 = n.unwrap_or_else(|| slow(1));
    let c: i32 = unsafe { CALLS };
    a + b + c                            // 40 + 1 + 1
}
"""), 42)

    def test_and_then_filter_and_is_some_and(self):
        self.assertEqual(_rs("""
fn half(x: i32) -> Option<i32> { if x % 2 == 0 { Some(x / 2) } else { None } }
fn main() -> i32 {
    let a: Option<i32> = Some(168).and_then(half).and_then(|x| half(x));
    let b: Option<i32> = Some(3).and_then(half);
    let c: Option<i32> = Some(9).filter(|x| *x > 5);
    let d: Option<i32> = Some(2).filter(|&x| x > 5);
    let mut t: i32 = a.unwrap();
    if b.is_some() { t += 100; }
    if !c.is_some_and(|v| v == 9) { t += 100; }
    if d.is_some() { t += 100; }
    t
}
"""), 42)

    def test_or_ok_or_and_defaults(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let n: Option<i32> = None;
    let a: i32 = n.or(Some(20)).unwrap();
    let b: i32 = n.or_else(|| Some(10)).unwrap();
    let r: Result<i32, i32> = n.ok_or(5);
    let s: Result<i32, i32> = Some(3).ok_or_else(|| 9);
    let z: i32 = n.unwrap_or_default();
    a + b + r.unwrap_err() + s.unwrap() + z + 4
}
"""), 42)

    def test_take_replace_and_get_or_insert(self):
        self.assertEqual(_rs("""
struct Slot { v: Option<i32> }
impl Slot {
    fn grab(&mut self) -> Option<i32> { self.v.take() }
}
fn main() -> i32 {
    let mut s: Slot = Slot { v: Some(30) };
    let a: i32 = s.grab().unwrap();
    if s.v.is_some() { return 1; }
    let mut o: Option<i32> = Some(1);
    let old: Option<i32> = o.replace(5);
    let mut e: Option<i32> = None;
    let p: *mut i32 = e.get_or_insert(6);
    unsafe { *p += 1; }
    a + old.unwrap() + o.unwrap() + e.unwrap() - 1    // 30 + 1 + 5 + 7 - 1
}
"""), 42)

    def test_as_ref_copied_and_flatten(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let o: Option<i32> = Some(40);
    let r: Option<*const i32> = o.as_ref();
    let c: Option<i32> = r.copied();
    let nested: Option<Option<i32>> = Some(Some(2));
    c.unwrap() + nested.flatten().unwrap()
}
"""), 42)

    def test_method_on_a_reference_to_an_option(self):
        self.assertEqual(_rs("""
fn bump(o: &mut Option<i32>) -> i32 {
    let v: i32 = o.take().unwrap_or(0);
    v + 2
}
fn main() -> i32 {
    let mut o: Option<i32> = Some(40);
    let a: i32 = bump(&mut o);
    if o.is_some() { return 1; }
    a
}
"""), 42)

    def test_named_function_arguments(self):
        self.assertEqual(_rs("""
fn inc(x: i32) -> i32 { x + 1 }
fn main() -> i32 { Some(41).map(inc).unwrap() }
"""), 42)

    def test_chained_on_temporaries_evaluate_once(self):
        c = crust.translate("""
fn g() -> Option<i32> { Some(1) }
fn f() -> i32 { g().map(|x| x + 1).unwrap_or(0) }
""")
        self.assertEqual(c.count("g()"), 1)

    def test_return_inside_an_inlined_closure_is_rejected(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f(o: Option<i32>) -> i32 { o.map(|x| { return x; }).unwrap_or(0) }
""")
        self.assertIn("inlined", str(cm.exception))

    def test_expect_prints_its_message(self):
        rc, err = _rs_stderr("""
fn main() -> i32 {
    let o: Option<i32> = None;
    o.expect("the slot was empty")
}
""")
        self.assertNotEqual(rc, 0)
        self.assertIn("the slot was empty", err)

    def test_panic_prints_its_message(self):
        rc, err = _rs_stderr("""
fn main() { let k: i32 = 7; panic!("bad key {}", k); }
""")
        self.assertNotEqual(rc, 0)
        self.assertIn("bad key 7", err)


class TestResultMethods(unittest.TestCase):

    BASE = """
fn parse(x: i32) -> Result<i32, i32> { if x >= 0 { Ok(x) } else { Err(-x) } }
"""

    def test_map_map_err_and_then(self):
        self.assertEqual(_rs(self.BASE + """
fn main() -> i32 {
    let k: i32 = 3;
    let a: Result<i32, i32> = parse(10).map(|v| v * k);
    let b: Result<i32, i32> = parse(-4).map_err(|e| e + k);
    let c: Result<i32, i32> = parse(5).and_then(|v| parse(v - 10));
    a.unwrap() + b.unwrap_err() + c.unwrap_err() - 12    // 30 + 7 + 5 - 12
}
"""), 30)

    def test_or_else_unwrap_or_else_and_map_or(self):
        self.assertEqual(_rs(self.BASE + """
fn main() -> i32 {
    let a: i32 = parse(-6).unwrap_or_else(|e| e * 5);
    let b: Result<i32, i32> = parse(-2).or_else(|e| parse(e));
    let c: i32 = parse(4).map_or(0, |v| v + 1);
    let d: i32 = parse(-4).map_or_else(|e| e, |v| v * 100);
    a + b.unwrap() + c + d + 1           // 30 + 2 + 5 + 4 + 1
}
"""), 42)

    def test_err_is_ok_and_expect_err(self):
        self.assertEqual(_rs(self.BASE + """
fn main() -> i32 {
    let e: Option<i32> = parse(-40).err();
    let n: Option<i32> = parse(1).err();
    let mut t: i32 = e.unwrap();
    if n.is_some() { t += 100; }
    if parse(2).is_ok_and(|v| v == 2) { t += 1; }
    if parse(-2).is_err_and(|v| v == 2) { t += 1; }
    t + parse(0).expect("ok") + parse(-0).unwrap_or_default()
}
"""), 42)

    def test_unit_ok_type(self):
        self.assertEqual(_rs("""
fn check(x: i32) -> Result<(), i32> { if x > 0 { Ok(()) } else { Err(x) } }
fn main() -> i32 {
    let a: Result<i32, i32> = check(1).map(|_| 40);
    let b: i32 = check(-2).map_or(0, |_| 1);
    let c: i32 = check(3).map_or(0, |_| 2);
    a.unwrap() - b + c                   // 40 - 0 + 2
}
"""), 42)


class TestPointerMangling(unittest.TestCase):
    """A pointer type argument must not share a name with its pointee.

    `_mangle` dropped the `*`, so `Option<*mut i32>` and `Option<i32>` were
    one struct, `crust_option_int`.
    """

    def test_option_of_pointer_is_its_own_type(self):
        c = crust.translate(
            "fn f(a: Option<*mut i32>, b: Option<i32>) -> i32 { 0 }")
        self.assertIn("int f(crust_option_int_p, crust_option_int)", c)

    def test_vec_of_pointers_and_of_values_coexist(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut x: i32 = 40;
    let mut ps: Vec<*mut i32> = Vec::<*mut i32>::new();
    let mut vs: Vec<i32> = Vec::<i32>::new();
    ps.push(&mut x);
    vs.push(2);
    let p: *mut i32 = ps.get(0);
    unsafe { *p + vs.get(0) }
}
"""), 42)


class TestUnitOk(unittest.TestCase):

    def test_ok_unit_value(self):
        self.assertEqual(_rs("""
fn check(x: i32) -> Result<(), i32> {
    if x < 0 { return Err(x); }
    Ok(())
}
fn run() -> Result<(), i32> {
    check(1)?;
    check(-42)?;
    Ok(())
}
fn main() -> i32 { let r: Result<(), i32> = run(); -r.unwrap_err() }
"""), 42)


class TestForRanges(unittest.TestCase):

    def test_rev_skip_take_step_by(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut a: i32 = 0;
    for i in (0..5).rev() { a = a * 10 + i; }             // 43210
    let mut b: i32 = 0;
    for i in (0..10).step_by(3) { b += i; }               // 0+3+6+9 = 18
    let mut c: i32 = 0;
    for i in (1..=10).skip(2).take(3) { c += i; }         // 3+4+5 = 12
    let mut d: i32 = 0;
    for i in (0..10).step_by(4).rev() { d = d * 10 + i; } // 840
    if a != 43210 { return 1; }
    if d != 840 { return 2; }
    b + c + 12
}
"""), 42)

    def test_enumerate_on_a_range(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut t: i32 = 0;
    for (k, v) in (10..13).enumerate() { t += (k as i32) * v; }   // 0+11+24
    t + 7
}
"""), 42)

    def test_empty_and_inclusive_edges(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut t: i32 = 0;
    for i in (5..5).rev() { t += 100; }
    for i in (5..3).rev() { t += 100; }
    for i in (3..=3).rev() { t += i; }
    for i in (0..3).skip(10) { t += 100; }
    t + 39
}
"""), 42)

    def test_bounds_are_evaluated_once(self):
        self.assertEqual(_rs("""
static mut CALLS: i32 = 0;
fn n() -> i32 { unsafe { CALLS += 1; } 4 }
fn main() -> i32 {
    let mut t: i32 = 0;
    for i in (0..n()).rev().step_by(1) { t += i; }        // 6
    t + unsafe { CALLS } * 35 + 1
}
"""), 42)

    def test_plain_range_output_is_unchanged(self):
        c = crust.translate("fn f() { for i in 0..10 { } }")
        self.assertIn("for (int i = 0; i < 10; i++)", c)


class TestForCollections(unittest.TestCase):

    def test_vec_by_reference_and_iter(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(10); v.push(20); v.push(5);
    let mut t: i32 = 0;
    for x in &v { t += *x; }
    for x in v.iter() { t += x; }
    for &x in v.iter() { t -= x; }
    t + 7                                  // 35 + 35 - 35 + 7
}
"""), 42)

    def test_iter_mut_writes_through(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(1); v.push(2); v.push(3);
    for x in v.iter_mut() { *x *= 7; }
    for x in &mut v { *x += 0; }
    v.get(0) + v.get(1) + v.get(2)         // 7 + 14 + 21
}
"""), 42)

    def test_enumerate_rev_and_slices(self):
        self.assertEqual(_rs("""
fn weigh(xs: &[i32]) -> i32 {
    let mut t: i32 = 0;
    for (i, &x) in xs.iter().enumerate() { t += (i as i32) * x; }
    t
}
fn main() -> i32 {
    let a: [i32; 4] = [5, 6, 7, 8];
    let mut first: i32 = -1;
    for x in a.iter().rev() { if first < 0 { first = *x; } }
    weigh(&a[..]) - first + 6              // 0+6+14+24 - 8 + 6
}
"""), 42)

    def test_vec_through_a_pointer(self):
        self.assertEqual(_rs("""
fn total(v: &Vec<i32>) -> i32 {
    let mut t: i32 = 0;
    for x in v.iter() { t += x; }
    for x in v { t += x; }
    t
}
fn bump(v: &mut Vec<i32>) { for x in v.iter_mut() { *x += 1; } }
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(10); v.push(9);
    bump(&mut v);
    total(&v)                              // [11, 10], summed twice
}
"""), 42)

    def test_consuming_a_vec_moves_it_and_drops_each_element(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    let mut v: Vec<G> = Vec::<G>::new();
    v.push(g(20));
    v.push(g(22));
    let mut t: i32 = 0;
    for x in v { t += x.id; }
    if live() != 0 { return 1; }
    t
}
"""), 42)

    def test_use_after_consuming_loop_is_rejected(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f() -> usize {
    let v: Vec<i32> = Vec::<i32>::new();
    for x in v { }
    v.len()
}
""")
        self.assertIn("moved", str(cm.exception))

    def test_iter_does_not_consume(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(40);
    for x in v.iter() { }
    for x in &v { }
    v.len() as i32 + 41
}
"""), 42)

    def test_temporary_vec_is_freed_after_the_loop(self):
        c = crust.translate("""
fn make() -> Vec<i32> { let mut v: Vec<i32> = Vec::<i32>::new(); v.push(1); v }
fn f() -> i32 { let mut t: i32 = 0; for x in make() { t += x; } t }
""")
        body = c[c.index("int f("):]
        self.assertIn("Vec_int_free_buf(&", body)

    def test_str_bytes(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let s: &str = "a*b";
    let mut n: i32 = 0;
    for b in s.bytes() { if b == 42 { n += 1; } }
    for (i, b) in s.bytes().enumerate() { if b == 98 { n += i as i32; } }
    n + 39                                 // 1 + 2 + 39
}
"""), 42)

    def test_step_by_zero_panics(self):
        rc, _err = _rs_stderr("""
fn main() { let k: usize = 0; for i in (0..3).step_by(k) { } }
""")
        self.assertNotEqual(rc, 0)

    def test_break_and_continue_inside_adapted_loops(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut t: i32 = 0;
    'o: for i in (0..10).rev() {
        for (k, j) in (0..10).enumerate() {
            if j > i { continue 'o; }
            if i == 2 { break 'o; }
            t += 1;
        }
    }
    t                                      // 10+9+8+7+6+5+4
}
"""), 49)


class TestIteratorOutsideFor(unittest.TestCase):

    def test_unsupported_iterator_method_is_named(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f(v: &Vec<i32>) -> i32 { v.iter().peekable().count() as i32 }
""")
        self.assertIn("peekable", str(cm.exception))

    def test_unconsumed_iterator_is_an_error(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f(v: &Vec<i32>) { let it = v.iter(); }")
        self.assertIn("never consumed", str(cm.exception))


class TestIteratorConsumers(unittest.TestCase):

    def test_sum_product_count(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(3); v.push(4); v.push(5);
    let s: i32 = v.iter().sum();                           // 12
    let p: i32 = v.iter().product();                       // 60
    let c: usize = v.iter().filter(|x| **x > 3).count();   // 2
    let q: i32 = (1..=4).map(|x| x * x).sum();             // 30
    s + p + c as i32 + q - 62
}
"""), 42)

    def test_predicates_and_selectors(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let a: [i32; 5] = [7, 2, 9, 4, 6];
    let mut t: i32 = 0;
    if a.iter().any(|x| *x == 9) { t += 1; }                   // 1
    if a.iter().all(|x| *x > 0) { t += 1; }                    // 2
    if a.iter().any(|&x| x > 100) { t += 100; }
    t += a.iter().find(|x| **x % 2 == 0).copied().unwrap();    // 4
    t += a.iter().position(|x| *x == 4).unwrap() as i32;       // 7
    t += *a.iter().max().unwrap();                             // 16
    t += a.iter().min().copied().unwrap_or(0);                 // 18
    t += a.iter().fold(0, |acc, x| acc + x);                   // 46
    t += *a.iter().last().unwrap();                            // 52
    t += a.iter().nth(1).copied().unwrap();                    // 54
    t - 12
}
"""), 42)

    def test_collect_into_a_vec(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut v: Vec<i32> = Vec::<i32>::new();
    v.push(1); v.push(2); v.push(3); v.push(4);
    let sq: Vec<i32> = v.iter().map(|x| x * x).filter(|x| *x > 1).collect();
    let r: Vec<i32> = (0..5).rev().collect();
    let n: usize = sq.len();                                   // [4, 9, 16]
    sq.get(2) + r.get(0) * 5 + n as i32 + 3                    // 16 + 20 + 3 + 3
}
"""), 42)

    def test_zip_and_enumerate(self):
        self.assertEqual(_rs("""
fn dot(a: &[i32], b: &[i32]) -> i32 {
    a.iter().zip(b.iter()).map(|(x, y)| x * y).sum()
}
fn main() -> i32 {
    let a: [i32; 3] = [1, 2, 3];
    let b: [i32; 4] = [4, 5, 6, 100];
    let d: i32 = dot(&a[..], &b[..]);                          // 4+10+18 = 32
    let mut t: i32 = 0;
    for (x, y) in a.iter().rev().zip(b.iter()) { t += x * y; } // 12+10+6 = 28
    let e: usize = a.iter().enumerate()
        .filter(|(i, x)| *i + (**x as usize) > 2).count();    // 2
    d + t - 20 + e as i32
}
"""), 42)

    def test_stages_are_lazy(self):
        self.assertEqual(_rs("""
static mut CALLS: i32 = 0;
fn f(x: i32) -> i32 { unsafe { CALLS += 1; } x }
fn main() -> i32 {
    let s: i32 = (0..100).map(|x| f(x)).take(3).sum();         // 3, 3 calls
    let c1: i32 = unsafe { CALLS };
    let first: Option<i32> = (5..100).map(|x| f(x)).find(|x| *x > 6);
    let c2: i32 = unsafe { CALLS };                            // 6
    s + c1 * 10 + first.unwrap() + c2 - 4                      // 3+30+7+6-4
}
"""), 42)

    def test_stage_adaptors(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let v: [i32; 8] = [1, 2, 3, 4, 5, 6, 7, 8];
    let a: i32 = v.iter().filter(|x| **x % 2 == 0).skip(1).take(2).sum();  // 10
    let b: i32 = v.iter().take_while(|x| **x < 4).sum();                   // 6
    let c: i32 = v.iter().skip_while(|x| **x < 6).sum();                   // 21
    let d: i32 = v.iter()
        .filter_map(|x| if *x > 6 { Some(x * 2) } else { None }).sum();   // 30
    let e: i32 = v.iter().map(|x| x * 1).step_by(3).sum();                 // 12
    a + b + c + d + e - 37
}
"""), 42)

    def test_for_loop_over_a_staged_chain(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let v: [i32; 6] = [5, 1, 8, 3, 9, 2];
    let mut t: i32 = 0;
    for x in v.iter().filter(|x| **x > 2).map(|x| x * 2) { t += x; }  // 50
    for (i, x) in v.iter().enumerate().skip(4) { t += i as i32 * x; }  // 46
    t - 54
}
"""), 42)

    def test_by_key_selectors(self):
        self.assertEqual(_rs("""
struct P { x: i32, y: i32 }
fn main() -> i32 {
    let mut v: Vec<P> = Vec::<P>::new();
    v.push(P { x: 1, y: 9 }); v.push(P { x: 7, y: 2 }); v.push(P { x: 4, y: 30 });
    let a: i32 = v.iter().max_by_key(|p| p.y).map(|p| p.x).unwrap();  // 4
    let b: i32 = v.iter().min_by_key(|p| p.y).map(|p| p.x).unwrap();  // 7
    a + b + v.iter().map(|p| p.x).max().unwrap() + 24                 // +7
}
"""), 42)

    def test_for_each_captures_and_temporary_source(self):
        self.assertEqual(_rs("""
fn make() -> Vec<i32> {
    let mut v: Vec<i32> = Vec::<i32>::new(); v.push(20); v.push(22); v
}
fn main() -> i32 {
    let mut t: i32 = 0;
    make().iter().for_each(|x| t += x);
    t
}
"""), 42)
        c = crust.translate("""
fn make() -> Vec<i32> { let mut v: Vec<i32> = Vec::<i32>::new(); v }
fn f() -> usize { make().iter().count() }
""")
        body = c[c.index("unsigned long f("):]
        self.assertIn("Vec_int_free_buf(&", body)

    def test_into_iter_consumes(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f() -> usize {
    let v: Vec<i32> = Vec::<i32>::new();
    let w: Vec<i32> = v.into_iter().map(|x| x + 1).collect();
    v.len()
}
""")
        self.assertIn("moved", str(cm.exception))

    def test_block_closure_bodies(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let base: i32 = 1;
    (0..4).map(|x| { let y: i32 = x * 3; y + base }).sum::<i32>() + 20  // 1+4+7+10
}
"""), 42)


class TestIteratorEdges(unittest.TestCase):

    def test_break_and_continue_in_a_staged_for(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let v: [i32; 8] = [1, 2, 3, 4, 5, 6, 7, 8];
    let mut t: i32 = 0;
    'o: for i in 0..3 {
        for x in v.iter().filter(|x| **x % 2 == 1).map(|x| x * 10) {
            if x == 30 { continue; }
            if x == 70 { continue 'o; }
            if i == 2 { break 'o; }
            t += x;                       // 10 + 50, twice
        }
    }
    t - 78
}
"""), 42)

    def test_consumer_nested_in_a_closure(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let a: [i32; 3] = [1, 2, 3];
    let b: [i32; 3] = [4, 5, 6];
    let rows: [&[i32]; 2] = [&a[..], &b[..]];
    let best: i32 = rows.iter().map(|r| r.iter().sum::<i32>()).max().unwrap();
    best + rows.iter().map(|r| r.len() as i32).sum::<i32>() + 21  // 15 + 6 + 21
}
"""), 42)

    def test_range_contains(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let x: i32 = 7;
    let mut t: i32 = 0;
    if (1..10).contains(&x) { t += 40; }
    if (1..7).contains(&x) { t += 100; }
    if (1..=7).contains(&x) { t += 2; }
    t
}
"""), 42)

    def test_each_value_is_computed_once_per_element(self):
        c = crust.translate("""
fn sq(x: i32) -> i32 { x * x }
fn f(v: &[i32]) -> i32 { v.iter().map(|x| sq(*x)).filter(|y| *y > 4).sum() }
""")
        self.assertEqual(c.count("sq((") + c.count("sq(x"), 1)


class TestArrayOfSlices(unittest.TestCase):
    """`rows[0]` on `[&[i32]; N]` indexes the array, not a slice.

    The element type was checked before the array-ness, so this compiled to
    `rows.ptr[0]`.
    """

    def test_indexing_an_array_of_slices(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let a: [i32; 2] = [40, 1];
    let b: [i32; 3] = [1, 1, 2];
    let rows: [&[i32]; 2] = [&a[..], &b[..]];
    let r: &[i32] = rows[1];
    rows[0][0] + r[2]
}
"""), 42)


class TestContainerElementDrops(unittest.TestCase):
    """A `Vec`, `VecDeque` or `Box` drops what it holds when it is freed.

    It used to free only its buffer, so every owning element leaked.
    """

    def test_vec_drops_its_elements(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    {
        let mut v: Vec<G> = Vec::<G>::new();
        v.push(g(1)); v.push(g(2)); v.push(g(3));
        if live() != 3 { return 1; }
    }
    if live() != 0 { return 2; }
    42
}
"""), 42)

    def test_set_clear_and_pop(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    let mut v: Vec<G> = Vec::<G>::new();
    v.push(g(1)); v.push(g(2)); v.push(g(3));
    v.set(0, g(9));                    // the old element is dropped
    if live() != 3 { return 1; }
    {
        let p: G = v.pop();            // owned, dropped at this block's end
    }
    if live() != 2 { return 2; }
    v.clear();
    if live() != 0 { return 3; }
    v.push(g(4));
    41 + live()
}
"""), 42)

    def test_nested_vec_struct_field_box_and_deque(self):
        self.assertEqual(_rs(_COUNTED + """
struct H { items: Vec<G> }
fn main() -> i32 {
    {
        let mut outer: Vec<Vec<G>> = Vec::<Vec<G>>::new();
        let mut a: Vec<G> = Vec::<G>::new();
        a.push(g(1)); a.push(g(2));
        outer.push(a);
        let mut h: H = H { items: Vec::<G>::new() };
        h.items.push(g(3));
        let b: Box<G> = Box::<G>::new(g(4));
        let mut d: VecDeque<G> = VecDeque::<G>::new();
        d.push_back(g(5)); d.push_front(g(6));
        if live() != 6 { return 1; }
    }
    if live() != 0 { return 2; }
    42
}
"""), 42)

    def test_get_and_iter_are_borrows(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    let mut v: Vec<G> = Vec::<G>::new();
    v.push(g(40)); v.push(g(2));
    let mut t: i32 = 0;
    {
        let a: G = v.get(0);
        let b: G = v.last();
        let c: G = a;
        t = c.id + b.id;
    }
    if live() != 2 { return 1; }
    for x in v.iter() { t += 0 * x.id; }
    for x in &v { t += 0 * x.id; }
    if live() != 2 { return 2; }
    t
}
"""), 42)

    def test_consuming_loop_with_break_drops_each_element_once(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    let mut t: i32 = 0;
    {
        let mut v: Vec<G> = Vec::<G>::new();
        v.push(g(10)); v.push(g(20)); v.push(g(30));
        for x in v { t += x.id; if x.id == 20 { break; } }
    }
    if live() != 0 { return 1; }
    t + 12
}
"""), 42)

    def test_cloned_and_string_clone_deep_copy(self):
        # A bitwise copy here would be freed twice, which glibc aborts on.
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut v: Vec<String> = Vec::<String>::new();
    v.push(format!("ab{}", 1));
    v.push(format!("c"));
    let w: Vec<String> = v.iter().cloned().collect();
    let s: String = v.get(0).clone();
    let mut n: usize = 0;
    for x in v.iter().cloned() { n += x.len(); }        // 4
    s.len() as i32 * 10 + w.len() as i32 * 4 + n as i32 // 30 + 8 + 4
}
"""), 42)

    def test_moving_a_borrow_is_rejected(self):
        cases = {
            "by value": "fn f(v: &Vec<G>) -> i32 { take(v.get(0)) }",
            "via a local": "fn f(v: &Vec<G>) -> i32 "
                           "{ let a: G = v.get(0); take(a) }",
            "loop binding": "fn f(v: &Vec<G>) { for x in v.iter() "
                            "{ take(x); } }",
            "return": "fn f(v: &Vec<G>) -> G { v.get(0) }",
            "push": "fn f(v: &mut Vec<G>, w: &Vec<G>) { v.push(w.get(0)); }",
            "field": "struct H { g: G }\n"
                     "fn f(v: &Vec<G>) -> i32 { let h: H = H { g: v.get(0) }; 0 }",
            "collect": "fn f(v: &Vec<G>) { let w: Vec<G> = "
                       "v.iter().collect(); }",
        }
        for what, body in cases.items():
            with self.assertRaises(crust.CrustError, msg=what) as cm:
                crust.translate(_COUNTED + "fn take(x: G) -> i32 { x.id }\n"
                                + body)
            self.assertIn("borrow", str(cm.exception), what)

    def test_plain_elements_need_no_drop_calls(self):
        c = crust.translate("""
fn f() -> usize { let mut v: Vec<i32> = Vec::<i32>::new(); v.push(1); v.len() }
""")
        body = c[c.index("Vec_int_drop_elems(Vec_int *self) {"):]
        self.assertIn("(void)self;", body.split("}")[0])


def _rs_out(source):
    """Compile and run an all-Rust program; return (exit status, stdout)."""
    workdir = tempfile.mkdtemp()
    src = os.path.join(workdir, "prog.rs")
    out = os.path.join(workdir, "prog")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)
    proc = subprocess.run(
        ["python3", "-m", "shivyc.main", src, "-o", out],
        cwd=_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    run = subprocess.run([out], capture_output=True, timeout=30)
    return run.returncode, run.stdout.decode("utf-8")


class TestChars(unittest.TestCase):
    """`chars()` decodes UTF-8; `char` is a type of its own."""

    def test_chars_counts_code_points_not_bytes(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let s: &str = "h\\u{e9}llo \\u{1F600}!";      // 8 chars, 12 bytes
    let n: usize = s.chars().count();
    (s.len() as i32) * 2 + n as i32 + 10            // 24 + 8 + 10
}
"""), 42)

    def test_decoded_values_forwards_and_backwards(self):
        # One character of each UTF-8 width: 1, 2, 3 and 4 bytes.
        self.assertEqual(_rs("""
fn main() -> i32 {
    let s: &str = "a\\u{e9}\\u{20ac}\\u{1F600}";
    let mut t: u32 = 0;
    for c in s.chars() { t = t * 7 + (c as u32) % 1000; }
    let mut r: u32 = 0;
    for c in s.chars().rev() { r = r * 7 + (c as u32) % 1000; }
    if t != 47748 { return 1; }
    if r != 195180 { return 2; }
    42
}
"""), 42)

    def test_rev_char_indices_and_adaptors(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let s: &str = "x\\u{e9}yz";
    let first_back: char = s.chars().rev().nth(0).unwrap();          // 'z'
    let e_at: usize = s.char_indices()
        .find(|(i, c)| *c == '\\u{e9}').unwrap().0;                   // byte 1
    let skipped: u32 = s.chars().skip(1).take(2).map(|c| c as u32).sum();
    (first_back as i32) - 122 + e_at as i32 * 41 + (skipped as i32 - 354) + 1
}
"""), 42)

    def test_char_methods(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut t: i32 = 0;
    for c in "a1 Z\\t\\u{a0}".chars() {
        if c.is_ascii_digit() { t += 1; }
        if c.is_ascii_alphabetic() { t += 10; }
        if c.is_whitespace() { t += 100; }                 // U+00A0 counts
        if c.is_ascii_uppercase() { t += 1000; }
    }
    let d: u32 = '7'.to_digit(10).unwrap() + 'f'.to_digit(16).unwrap();  // 22
    let up: char = 'q'.to_ascii_uppercase();
    if 'x'.to_digit(10).is_some() { return 1; }
    if up != 'Q' { return 2; }
    if '\\u{e9}'.len_utf8() != 2 { return 3; }
    t - 1300 + d as i32 - 1                    // 1321 - 1300 + 22 - 1
}
"""), 42)

    def test_collect_chars_into_a_string_and_push(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let s: &str = "ab\\u{e9}";
    let r: String = s.chars().rev().collect();
    let mut u: String = String::new();
    u.push('\\u{20ac}');
    u.push('x');
    let upper: String = "hey".chars().map(|c| c.to_ascii_uppercase()).collect();
    if !upper.eq_str("HEY") { return 1; }
    if r.chars().nth(0).unwrap() != '\\u{e9}' { return 2; }
    (r.len() + u.len()) as i32 * 5 + 2         // (4 + 4) * 5 + 2
}
"""), 42)

    def test_println_prints_chars_as_characters(self):
        rc, out = _rs_out("""
fn main() {
    let c: char = '\\u{e9}';
    let n: u32 = 65;
    println!("{}{} {} {:?}", 'a', c, n, 'z');
}
""")
        self.assertEqual(out, "a\u00e9 65 'z'\n")

    def test_char_and_u32_are_different_types(self):
        c = crust.translate("""
fn f(a: Vec<char>, b: Vec<u32>) -> usize { a.len() + b.len() }
""")
        self.assertIn("Vec_crust_char", c)
        self.assertIn("Vec_unsigned_int", c)

    def test_non_ascii_classification_is_refused_not_guessed(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f(c: char) -> bool { c.is_alphabetic() }")
        self.assertIn("is_ascii_alphabetic", str(cm.exception))


class TestChain(unittest.TestCase):

    def test_chain_in_expressions_and_for(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let a: [i32; 3] = [1, 2, 3];
    let b: [i32; 2] = [10, 20];
    let s: i32 = a.iter().chain(b.iter()).sum();                 // 36
    let mut order: i32 = 0;
    for x in a.iter().chain(b.iter()).rev() { order = order * 3 + x; }
    let m: i32 = (0..2).chain(5..7).map(|x| x * x).sum();        // 62
    if order != 20 * 81 + 10 * 27 + 3 * 9 + 2 * 3 + 1 { return 1; }
    s + m - 56
}
"""), 42)

    def test_chain_with_skip_take_across_the_seam(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let a: [i32; 3] = [1, 2, 3];
    let b: [i32; 3] = [10, 20, 30];
    let v: Vec<i32> = a.iter().chain(b.iter().rev()).skip(2).take(3).copied()
        .collect();                                              // 3, 30, 20
    let three: i32 = a.iter().chain(b.iter()).chain(a.iter()).count() as i32;
    v.get(0) + v.get(1) - v.get(2) + three + 20                  // 3+30-20+9+20
}
"""), 42)

    def test_chain_needs_one_element_type(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn f(a: &[i32], b: &[f64]) -> usize { a.iter().chain(b.iter()).count() }
""")
        self.assertIn("same type", str(cm.exception))


class TestCapturingClosures(unittest.TestCase):
    """Closures that capture and are stored, passed, and called later."""

    def test_capture_by_reference(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let off: i32 = 10;
    let add = |x: i32| x + off;
    add(5) + add(17)                        // 15 + 27
}
"""), 42)

    def test_mutating_capture_is_seen_outside(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut count: i32 = 40;
    let mut inc = || count += 1;
    inc();
    inc();
    count
}
"""), 42)

    def test_move_captures_a_copy(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let mut k: i32 = 1;
    let f = move |x: i32| x + k;
    k = 100;
    f(41) + k - 100
}
"""), 42)

    def test_move_of_an_owning_value_is_dropped_once(self):
        self.assertEqual(_rs(_COUNTED + """
fn main() -> i32 {
    let mut r: i32 = 0;
    {
        let a: G = g(40);
        let f = move || a.id + 2;
        r = f() + f() - 42;
        if live() != 1 { return 1; }
    }
    if live() != 0 { return 2; }
    r
}
"""), 42)

    def test_use_after_move_into_a_closure_is_rejected(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate(_COUNTED + """
fn f() -> i32 { let a: G = g(1); let h = move || a.id; a.id }
""")
        self.assertIn("moved", str(cm.exception))

    def test_moving_a_capture_out_of_the_body_is_rejected(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate(_COUNTED + """
fn take(x: G) -> i32 { x.id }
fn f() -> i32 { let a: G = g(1); let h = || take(a); h() }
""")
        self.assertIn("borrow", str(cm.exception))

    def test_generic_bounds_where_and_impl_fn(self):
        self.assertEqual(_rs("""
fn apply<F: Fn(i32) -> i32>(f: F, x: i32) -> i32 { f(x) }
fn twice(f: impl Fn(i32) -> i32, x: i32) -> i32 { f(f(x)) }
fn run_twice<F>(f: &mut F) where F: FnMut() { f(); f(); }
fn main() -> i32 {
    let base: i32 = 3;
    let add = |x: i32| x + base;
    let mut n: i32 = 0;
    let mut bump = || n += 5;
    run_twice(&mut bump);
    apply(add, 10) + twice(add, 3) + n + 10    // 13 + 9 + 10 + 10
}
"""), 42)

    def test_stored_closures_feed_iterator_and_option_methods(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let v: [i32; 5] = [1, 2, 3, 4, 5];
    let lim: i32 = 2;
    let big = |x: &i32| *x > lim;
    let dbl = |x: i32| x * 2;
    let n: usize = v.iter().filter(big).count();              // 3
    let s: i32 = v.iter().copied().map(dbl).sum();            // 30
    let o: i32 = Some(4).map(dbl).unwrap();                   // 8
    n as i32 + s + o + 1
}
"""), 42)

    def test_closures_calling_closures_and_nested_capture(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let a: i32 = 2;
    let f = |x: i32| x * a;
    let g = |x: i32| f(x) + 1;
    let h = |x: i32| { let inner = |y: i32| y + a; inner(g(x)) };
    h(19)                                   // (19*2 + 1) + 2
}
"""), 41)

    def test_block_body_with_return_and_try(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let limit: i32 = 100;
    let check = |x: i32| -> Result<i32, i32> {
        if x > limit { return Err(x); }
        Ok(x * 2)
    };
    let twice_ok = |x: i32| -> Result<i32, i32> { let y: i32 = check(x)?; Ok(y + 1) };
    let bad: Result<i32, i32> = twice_ok(500);
    if bad.is_ok() { return 1; }
    twice_ok(20).unwrap()                   // 20*2 + 1
}
"""), 41)

    def test_immediately_invoked_and_method_capturing_self(self):
        self.assertEqual(_rs("""
struct P { x: i32 }
impl P {
    fn scaled(&self, k: i32) -> i32 { let f = |d: i32| self.x * d; f(k) }
}
fn main() -> i32 {
    let base: i32 = 1;
    let p: P = P { x: 8 };
    (|x: i32| x + base)(1) + p.scaled(5)    // 2 + 40
}
"""), 42)

    def test_array_capture(self):
        self.assertEqual(_rs("""
fn main() -> i32 {
    let a: [i32; 3] = [10, 20, 12];
    let at = |i: usize| a[i];
    let total = || { let mut t: i32 = 0; for x in a { t += x; } t };
    at(0) + at(2) + total() - 42            // 22 + 42 - 42
}
"""), 22)

    def test_non_capturing_closures_are_still_plain_functions(self):
        c = crust.translate("fn f() -> i32 { let g = |x: i32| x + 1; g(1) }")
        self.assertIn("crust_fn_int_int g = _crust_closure", c)

    def test_returning_impl_fn_is_reported(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("""
fn make(k: i32) -> impl Fn(i32) -> i32 { move |x: i32| x + k }
""")
        self.assertIn("impl", str(cm.exception))


class TestContracts(unittest.TestCase):
    """`#[requires]`/`#[ensures]`/`#[invariant]`/`#[variant]`, Creusot- and
    Prusti-style, checked at runtime."""

    def assertViolates(self, source, *fragments):
        rc, err = _rs_stderr(source)
        self.assertNotEqual(rc, 0, "the contract was not checked")
        self.assertIn("contract violated", err)
        for frag in fragments:
            self.assertIn(frag, err)

    def test_requires_holds_and_fails(self):
        src = """
use prusti_contracts::*;
#[requires(x > 0)]
#[requires(x < 100)]
fn half(x: u32) -> u32 { x / 2 }
fn main() -> i32 { half(ARG) as i32 }
"""
        self.assertEqual(_rs(src.replace("ARG", "84")), 42)
        self.assertViolates(src.replace("ARG", "0"), "requires", "x > 0",
                            "half")
        self.assertViolates(src.replace("ARG", "200"), "x < 100")

    def test_ensures_checks_every_return_path(self):
        src = """
#[ensures(result <= 23)]
fn regs(c: u32) -> u32 {
    if c == 9 { return BAD; }
    match c { 0 => 6, 1 => 10, 2 => 15, _ => 23 }
}
fn main() -> i32 {
    (regs(0) + regs(1) + regs(2) + regs(7) + regs(9)) as i32 - 40
}
"""
        self.assertEqual(_rs(src.replace("BAD", "0")), 14)
        self.assertViolates(src.replace("BAD", "99"), "ensures",
                            "result <= 23", "regs")

    def test_ensures_after_a_try_early_return(self):
        src = """
fn half(x: i32) -> Result<i32, i32> { if x % 2 == 0 { Ok(x / 2) } else { Err(x) } }
#[ensures(result.is_ok() || LIMIT)]
fn f(x: i32) -> Result<i32, i32> {
    let h: i32 = half(x)?;
    Ok(h)
}
fn main() -> i32 { let r: Result<i32, i32> = f(3); if r.is_ok() { 1 } else { 42 } }
"""
        self.assertEqual(_rs(src.replace("LIMIT", "x < 10")), 42)
        self.assertViolates(src.replace("LIMIT", "x > 10"), "ensures")

    def test_old_and_a_void_function(self):
        src = """
#[ensures(*v == old(*v) + STEP)]
fn inc(v: &mut u32) { *v += 1; }
fn main() -> i32 { let mut a: u32 = 41; inc(&mut a); a as i32 }
"""
        self.assertEqual(_rs(src.replace("STEP", "1")), 42)
        self.assertViolates(src.replace("STEP", "2"), "old(*v)")

    def test_implication(self):
        src = """
#[requires(flag ==> x > 5)]
fn f(flag: bool, x: u32) -> u32 { x }
fn main() -> i32 { (f(false, 0) + f(true, X)) as i32 }
"""
        self.assertEqual(_rs(src.replace("X", "42")), 42)
        self.assertViolates(src.replace("X", "3"), "flag ==> x > 5")

    def test_methods_see_self(self):
        src = """
struct Counter { n: u32, cap: u32 }
impl Counter {
    #[requires(self.n < self.cap)]
    #[ensures(self.n == old(self.n) + 1)]
    fn bump(&mut self) { self.n += 1; }
}
fn main() -> i32 {
    let mut c: Counter = Counter { n: 40, cap: CAP };
    c.bump(); c.bump();
    c.n as i32
}
"""
        self.assertEqual(_rs(src.replace("CAP", "50")), 42)
        self.assertViolates(src.replace("CAP", "41"), "self.n < self.cap")

    def test_loop_invariant_and_variant(self):
        src = """
fn sum_to(n: u32) -> u32 {
    let mut i: u32 = 0;
    let mut s: u32 = 0;
    #[invariant(i <= n)]
    #[variant(n - i)]
    while i < n { s += i; i += STEP; }
    s
}
fn main() -> i32 { sum_to(9) as i32 + 6 }
"""
        self.assertEqual(_rs(src.replace("STEP", "1")), 42)
        # A step of 0 never decreases the variant.
        self.assertViolates(src.replace("STEP", "0"), "variant", "n - i")

    def test_invariant_on_for_loop_and_while_let(self):
        src = """
fn next(k: u32) -> Option<u32> { if k < 5 { Some(k + 1) } else { None } }
fn main() -> i32 {
    let mut t: u32 = 0;
    #[invariant(t <= 100)]
    for i in 0..LIM { t += i; }
    let mut k: u32 = 0;
    #[invariant(k <= 5)]
    while let Some(n) = next(k) { k = n; }
    (t + k) as i32
}
"""
        self.assertEqual(_rs(src.replace("LIM", "9")), 41)
        self.assertViolates(src.replace("LIM", "20"), "invariant", "t <= 100")

    def test_invariant_is_checked_at_every_head_of_a_while(self):
        # Including the head whose test fails: `i <= 2` breaks when i is 3.
        self.assertViolates("""
fn main() -> i32 {
    let mut i: u32 = 0;
    #[invariant(i <= 2)]
    while i < 3 { i += 1; }
    i as i32
}
""", "i <= 2")

    def test_prusti_body_invariant(self):
        src = """
fn main() -> i32 {
    let mut i: i32 = 0;
    while i < 42 {
        body_invariant!(i < LIM);
        i += 1;
    }
    i
}
"""
        self.assertEqual(_rs(src.replace("LIM", "42")), 42)
        self.assertViolates(src.replace("LIM", "10"), "i < 10")

    def test_marker_attributes_and_statement_attributes(self):
        self.assertEqual(_rs("""
use creusot_contracts::*;
#[pure]
#[trusted]
fn sq(x: u32) -> u32 { x * x }
#[logic]
fn cube(x: u32) -> u32 { x * x * x }
fn main() -> i32 {
    #[allow(unused_variables)]
    let unused: i32 = 0;
    (sq(3) + cube(3) + 6) as i32
}
"""), 42)

    def test_quantified_clauses_are_kept_but_not_run(self):
        c = crust.translate("""
#[requires(forall(|i: usize| i < xs.len() ==> xs[i] > 0))]
fn f(xs: &[u32]) -> u32 { 0 }
""")
        self.assertNotIn("contract violated", c)

    def test_contracts_can_be_turned_off_but_are_still_parsed(self):
        src = "#[requires(x > 0)]\nfn f(x: u32) -> u32 { x }\n"
        os.environ["CRUST_CONTRACTS"] = "0"
        try:
            self.assertNotIn("contract violated", crust.translate(src))
            # A malformed clause is still an error with checks off.
            with self.assertRaises(crust.CrustError):
                crust.translate("#[requires(x >)]\n"
                                "fn f(x: u32) -> u32 { x }\n")
        finally:
            del os.environ["CRUST_CONTRACTS"]
        self.assertIn("contract violated", crust.translate(src))

    def test_misplaced_loop_contract_is_reported(self):
        with self.assertRaises(crust.CrustError) as cm:
            crust.translate("fn f() { #[invariant(true)] let x: i32 = 1; }")
        self.assertIn("loop", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
