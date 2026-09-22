// LeanOS: register sizing, in Rust.
//
// The scheduler saves and restores a thread's registers by class, and sizes
// that save from this function. `crustos/kernel.c` has it in C, where the
// IL lift reaches it and Lean accepts `elf_regs_for_class cls <= 23`. This is
// the same function written as Rust would write it -- a `match`, with the
// bound stated on the function rather than in a separate proof file -- and
// it is the first function in the tree whose model is *generated* from the
// file that ships: `shivyc/rustproof.py` lifts it, contract included, and
// the kernel proves the `ensures` for every `cls`.
//
// Crust compiles it, and checks the `ensures` at runtime too; the attributes
// are Creusot's and Prusti's, so the file stays valid Rust for them.
//
//   class 0  general-purpose registers only          6
//   class 1  plus the caller-saved set              10
//   class 2  plus the callee-saved set              15
//   other    everything, the conservative default   23

#[ensures(result <= 23)]
#[ensures(result >= 6)]
pub fn elf_regs_for_class(cls: u32) -> u32 {
    match cls {
        0 => 6,
        1 => 10,
        2 => 15,
        _ => 23,
    }
}

// The class a register count calls for: the inverse, rounding up. A count
// larger than any class is the conservative class.
#[ensures(result <= 3)]
pub fn class_for_regs(n: u32) -> u32 {
    match n {
        0..=6 => 0,
        7..=10 => 1,
        11..=15 => 2,
        _ => 3,
    }
}

// A class is sized: `elf_regs_for_class` of the class `class_for_regs`
// picks is at least the count asked for, up to the largest class.
#[requires(n <= 23)]
#[ensures(result)]
pub fn class_covers(n: u32) -> bool {
    elf_regs_for_class(class_for_regs(n)) >= n
}
