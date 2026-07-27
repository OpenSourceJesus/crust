// Data-carrying enums, lowered to a tagged union -- the same shape a C
// programmer would write by hand:
//
//     enum Msg_tag { Msg_Quit, Msg_Move, Msg_Write, Msg_Color };
//     struct Msg_Move_data { int x; int y; };
//     struct Msg { enum Msg_tag tag; union { ... } u; };
//
// `match` switches on the tag, and each arm binds its payload inside its own
// block so bindings cannot leak into a later arm.

int printf(const char *, ...);

enum Msg {
    Quit,
    Move { x: i32, y: i32 },
    Write(i32),
    Color(i32, i32, i32),
}

fn describe(m: Msg) -> i32 {
    match m {
        Msg::Quit => 0,
        Msg::Move { x, y } => x * 100 + y,
        Msg::Write(n) => n,
        Msg::Color(r, g, b) => r + g + b,
    }
}

// A field can be renamed while destructuring, and `_` discards.
fn just_x(m: Msg) -> i32 {
    match m {
        Msg::Move { x: got, y: _ } => got,
        _ => -1,
    }
}

enum Tree {
    Leaf(i32),
    Empty,
}

fn leaf_or(t: Tree, fallback: i32) -> i32 {
    match t {
        Tree::Leaf(v) => v,
        Tree::Empty => fallback,
    }
}

fn main() {
    printf("quit   = %d\n", describe(Msg::Quit));
    printf("move   = %d\n", describe(Msg::Move { x: 3, y: 7 }));
    printf("write  = %d\n", describe(Msg::Write(42)));
    printf("color  = %d\n", describe(Msg::Color(10, 20, 12)));
    printf("just_x = %d\n", just_x(Msg::Move { x: 5, y: 9 }));
    printf("leaf   = %d\n", leaf_or(Tree::Leaf(42), 0));
    printf("empty  = %d\n", leaf_or(Tree::Empty, 7));
}
