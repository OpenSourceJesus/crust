// A mini-kernel sketch in the Crust subset, using the bundled core.
//
// This is the shape of code `tools/crustos.py` is aiming at: OS-ish Rust with
// generic containers, compiled straight to native code with no cargo, no
// toolchain pin and no std.

int printf(const char *, ...);

const MAX_PRIO: i32 = 3;

enum State {
    Ready,
    Running,
    Blocked,
}

struct Task {
    pid: i32,
    prio: i32,
    ticks: i32,
    state: State,
}

impl Task {
    fn new(pid: i32, prio: i32) -> Task {
        Task { pid: pid, prio: prio, ticks: 0, state: State::Ready }
    }

    fn tick(&mut self) {
        self.ticks += 1;
    }

    fn weight(&self) -> i32 {
        (MAX_PRIO - self.prio) + 1
    }
}

// A generic ring buffer, instantiated per element type.
struct Ring<T> {
    items: Vec<T>,
    head: usize,
}

impl<T> Ring<T> {
    fn new() -> Ring<T> {
        Ring { items: Vec::<T>::new(), head: 0 }
    }

    fn push(&mut self, value: T) {
        self.items.push(value);
    }

    fn len(&self) -> usize {
        self.items.len()
    }

    fn at(&self, i: usize) -> T {
        self.items.get(i)
    }
}

// Round-robin over the run queue, weighted by priority.
fn schedule(q: *mut Ring<Task>, rounds: i32) -> i32 {
    let mut switches: i32 = 0;
    for _r in 0..rounds {
        for i in 0..q.len() {
            let mut t: Task = q.at(i);
            let w: i32 = t.weight();
            for _k in 0..w {
                t.tick();
            }
            switches += 1;
        }
    }
    switches
}

fn describe(s: State) -> &str {
    match s {
        State::Ready => "ready",
        State::Running => "running",
        State::Blocked => "blocked",
    }
}

fn main() {
    let mut q: Ring<Task> = Ring::<Task>::new();
    q.push(Task::new(1, 0));
    q.push(Task::new(2, 2));
    q.push(Task::new(3, 1));

    printf("tasks     = %lu\n", q.len());
    printf("switches  = %d\n", schedule(&q, 4));
    printf("task0 pid = %d (%s)\n", q.at(0).pid, describe(q.at(0).state));
    printf("weight(2) = %d\n", q.at(1).weight());

    // A Box of a plain value, and a second Ring instantiation.
    let mut counter: Box<i32> = Box::<i32>::new(0);
    counter.set(counter.get() + 41);
    let mut ids: Ring<i32> = Ring::<i32>::new();
    ids.push(counter.get() + 1);
    printf("boxed     = %d\n", ids.at(0));
    counter.free_box();
}
