/* A mini OS in three languages, each doing what it is actually good at.
 *
 *   procs.py   rpython -- list-shaped bookkeeping: build a table, filter it,
 *                         fold it. py2c lowers a typed list to a plain C
 *                         struct, so Rust can walk the result directly.
 *   this file  Rust    -- the scheduler itself: fixed layouts, tight loops,
 *              C         no allocator between the code and the machine.
 *
 * The point is that `for x in xs` over an rpython list needs no iterator
 * protocol in Crust. rpython already has one; its output is three words.
 */
#include "procs.py"

int printf(const char *, ...);

const MAX_TASKS: usize = 8;

struct Task {
    pid: i32,
    prio: i32,
    ticks: i32,
}

// Rust drives the rpython table directly: `quanta` is a `list[int]` built
// over there, walked here with no conversion and no copy.
fn seed_quanta(levels: i32, base: i32, out: *mut i32) -> i32 {
    let qs: *mut PyList<i32> = build_quanta(levels, base) as *mut PyList<i32>;
    let mut n: i32 = 0;
    for q in qs {
        out[n] = q;
        n += 1;
    }
    n
}

fn admit(tasks: *mut Task, count: i32, skip: i32, levels: i32) -> i32 {
    let pids: *mut PyList<i32> = runnable_pids(count, skip) as *mut PyList<i32>;
    let mut n: i32 = 0;
    for pid in pids {
        tasks[n].pid = pid;
        tasks[n].prio = pid % levels;
        tasks[n].ticks = 0;
        n += 1;
    }
    n
}

// The hot loop: no allocation, no boxing, ordinary C after lowering.
fn run_round(tasks: *mut Task, n: i32, quanta: *const i32) -> i32 {
    let mut switches: i32 = 0;
    for i in 0..n {
        let q: i32 = quanta[tasks[i].prio];
        tasks[i].ticks += q;
        switches += 1;
    }
    switches
}

int main(void) {
    struct Task tasks[8];
    int quanta[4];

    int levels = seed_quanta(3, 10, quanta);
    int n = admit(tasks, 8, 3, 3);

    printf("levels    = %d\n", levels);
    printf("admitted  = %d\n", n);
    printf("switches  = %d\n", run_round(tasks, n, quanta));
    printf("demand    = %d\n", total_demand(3, 10));
    printf("task0     = pid %d prio %d ticks %d\n",
           tasks[0].pid, tasks[0].prio, tasks[0].ticks);
    return 0;
}
