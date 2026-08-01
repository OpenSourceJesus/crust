// editbuf.rs -- the line editor's buffer and history ring.
//
// This is the part of the shell that is pure index arithmetic: inserting and
// deleting inside a fixed buffer, and walking a wrap-around history ring. It
// has no I/O and touches no hardware, so it is exactly the part worth handing
// to a checker. shell.c keeps the drawing and the dispatch; it calls in here
// for every mutation and then repaints from what it reads back.
//
// Two toolchains see this file, and they do different jobs:
//
//   rustc      checks it. Bounds, arithmetic, mutability, exhaustiveness.
//              Nothing rustc rejects gets as far as the kernel. See
//              `make check-rs`.
//   crust.py   compiles it, by lowering to C that gcc builds into the kernel
//              alongside the hand-written C. There is no FFI boundary and no
//              runtime: the generated `EditBuf_insert` is an ordinary C
//              function that shell.c calls directly.
//
// Written in the subset both accept, which means: no slices, no iterators, no
// Option, no traits. Flat arrays with explicit index math -- which is the
// error-prone style, and the reason having a checker on it is worth anything.

const LINE_MAX: i32 = 256;
const HIST_MAX: i32 = 16;

// ---------------------------------------------------------------------------
// The edit buffer
// ---------------------------------------------------------------------------

struct EditBuf {
    buf: [u8; 256],
    len: i32,
    pos: i32,
}

impl EditBuf {
    fn reset(&mut self) {
        self.len = 0;
        self.pos = 0;
        self.buf[0] = 0;
    }

    fn length(&self) -> i32 {
        return self.len;
    }

    fn cursor(&self) -> i32 {
        return self.pos;
    }

    // Read one byte. Out-of-range reads return 0 rather than trapping, so the
    // C side can walk the buffer without duplicating the bounds test.
    fn byte(&self, i: i32) -> u8 {
        if i < 0 || i >= self.len {
            return 0;
        }
        return self.buf[i as usize];
    }

    // Insert at the cursor, shifting the tail right. Returns false when the
    // buffer is full -- one slot is always reserved for the terminator, so a
    // full buffer still terminates.
    fn insert(&mut self, c: u8) -> bool {
        if self.len >= LINE_MAX - 1 {
            return false;
        }
        if self.pos < 0 || self.pos > self.len {
            return false;
        }
        let mut i: i32 = self.len;
        while i > self.pos {
            self.buf[i as usize] = self.buf[(i - 1) as usize];
            i -= 1;
        }
        self.buf[self.pos as usize] = c;
        self.len += 1;
        self.pos += 1;
        self.buf[self.len as usize] = 0;
        return true;
    }

    // Delete the byte at `at`, shifting the tail left.
    fn delete_at(&mut self, at: i32) -> bool {
        if at < 0 || at >= self.len {
            return false;
        }
        let mut i: i32 = at;
        while i < self.len - 1 {
            self.buf[i as usize] = self.buf[(i + 1) as usize];
            i += 1;
        }
        self.len -= 1;
        if self.pos > self.len {
            self.pos = self.len;
        }
        self.buf[self.len as usize] = 0;
        return true;
    }

    // Delete to the left of the cursor and move with it.
    fn backspace(&mut self) -> bool {
        if self.pos <= 0 {
            return false;
        }
        self.pos -= 1;
        return self.delete_at(self.pos);
    }

    fn kill(&mut self) {
        self.len = 0;
        self.pos = 0;
        self.buf[0] = 0;
    }

    // Cursor movement. Each returns whether it actually moved, so the caller
    // can skip a repaint that would change nothing.
    fn left(&mut self) -> bool {
        if self.pos <= 0 {
            return false;
        }
        self.pos -= 1;
        return true;
    }

    fn right(&mut self) -> bool {
        if self.pos >= self.len {
            return false;
        }
        self.pos += 1;
        return true;
    }

    fn home(&mut self) -> bool {
        if self.pos == 0 {
            return false;
        }
        self.pos = 0;
        return true;
    }

    fn end(&mut self) -> bool {
        if self.pos == self.len {
            return false;
        }
        self.pos = self.len;
        return true;
    }

    // Replace the whole contents, used when loading a history entry.
    fn set_from(&mut self, src: &History, slot: i32) {
        let mut i: i32 = 0;
        while i < LINE_MAX - 1 {
            let c: u8 = src.byte_at(slot, i);
            if c == 0 {
                break;
            }
            self.buf[i as usize] = c;
            i += 1;
        }
        self.buf[i as usize] = 0;
        self.len = i;
        self.pos = i;
    }
}

// ---------------------------------------------------------------------------
// The history ring
// ---------------------------------------------------------------------------
//
// Flat storage: entry `s` occupies bytes [s * LINE_MAX, (s+1) * LINE_MAX).
// `next` is the write head and wraps; `count` saturates at HIST_MAX. `back`
// counts away from the most recent entry, so 0 is the last command entered.

struct History {
    buf: [u8; 4096],
    count: i32,
    next: i32,
    pos: i32,
}

impl History {
    fn reset(&mut self) {
        self.count = 0;
        self.next = 0;
        self.pos = -1;
    }

    fn depth(&self) -> i32 {
        return self.count;
    }

    fn cursor(&self) -> i32 {
        return self.pos;
    }

    fn set_cursor(&mut self, p: i32) {
        if p < -1 {
            self.pos = -1;
        } else if p >= self.count {
            self.pos = self.count - 1;
        } else {
            self.pos = p;
        }
    }

    fn byte_at(&self, slot: i32, i: i32) -> u8 {
        if slot < 0 || slot >= HIST_MAX {
            return 0;
        }
        if i < 0 || i >= LINE_MAX {
            return 0;
        }
        return self.buf[(slot * LINE_MAX + i) as usize];
    }

    // Turn a "how many entries back" into a storage slot. Adding HIST_MAX
    // twice keeps the operand of % non-negative for any valid `back`, which
    // matters because C's % on a negative left operand is not a modulus.
    fn slot_for(&self, back: i32) -> i32 {
        if back < 0 || back >= self.count {
            return -1;
        }
        return (self.next - 1 - back + 2 * HIST_MAX) % HIST_MAX;
    }

    // True when `ed` holds exactly the most recent entry -- used to suppress a
    // duplicate rather than filling the ring with one repeated command.
    fn matches_last(&self, ed: &EditBuf) -> bool {
        if self.count <= 0 {
            return false;
        }
        let slot: i32 = self.slot_for(0);
        if slot < 0 {
            return false;
        }
        let mut i: i32 = 0;
        while i < ed.len {
            if self.byte_at(slot, i) != ed.buf[i as usize] {
                return false;
            }
            i += 1;
        }
        return self.byte_at(slot, ed.len) == 0;
    }

    fn push(&mut self, ed: &EditBuf) -> bool {
        if ed.len <= 0 {
            return false;
        }
        if self.matches_last(ed) {
            return false;
        }
        let base: i32 = self.next * LINE_MAX;
        let mut i: i32 = 0;
        while i < ed.len && i < LINE_MAX - 1 {
            self.buf[(base + i) as usize] = ed.buf[i as usize];
            i += 1;
        }
        self.buf[(base + i) as usize] = 0;
        self.next = (self.next + 1) % HIST_MAX;
        if self.count < HIST_MAX {
            self.count += 1;
        }
        self.pos = -1;
        return true;
    }

    // Step towards older entries. Returns the slot to load, or -1 at the end.
    fn older(&mut self) -> i32 {
        if self.pos + 1 >= self.count {
            return -1;
        }
        self.pos += 1;
        return self.slot_for(self.pos);
    }

    // Step towards newer entries. Returns -1 once back at the fresh line,
    // which the caller treats as "clear the buffer".
    fn newer(&mut self) -> i32 {
        if self.pos < 0 {
            return -1;
        }
        self.pos -= 1;
        if self.pos < 0 {
            return -1;
        }
        return self.slot_for(self.pos);
    }
}
