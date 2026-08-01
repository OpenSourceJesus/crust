// tarfs.rs -- ustar header parsing for the mbos ramdisk.
//
// The ramdisk is an ordinary tar archive handed to the kernel as a Multiboot
// module. ramfs.c walks it: it owns the pointer arithmetic over physical
// memory, copies each 512-byte header into the struct below, and asks this
// file what the header says.
//
// The split is the usual one -- if it can be checked without a machine, it
// belongs here. Tar headers are fixed-offset fields holding NUL-or-space
// terminated octal, which is exactly the kind of parsing that goes wrong
// quietly: an unterminated field runs into the next one, a digit outside 0-7
// is silently accepted, a size field that overflows wraps into something
// plausible. Every one of those is a bounds or range check, and every one is
// visible from here.
//
//   rustc     checks it        -- `make check-rs`
//   crust.py  lowers it to C   -- gcc builds the result into the kernel
//
// Written in the subset both accept: flat arrays, explicit index math, no
// slices or iterators or Option.
//
// ustar layout, the fields we use:
//
//   offset  size  field
//        0   100  name
//      100     8  mode
//      124    12  size      (octal)
//      136    12  mtime     (octal)
//      148     8  checksum  (octal, header treated as spaces while computing)
//      156     1  typeflag  ('0' or NUL = regular file, '5' = directory)
//      257     6  magic     ("ustar\0" or "ustar ")
//      345   155  prefix    (long names: real name is prefix + "/" + name)

const BLOCK: i32 = 512;
const NAME_MAX: i32 = 256;

struct TarHeader {
    raw: [u8; 512],
    name: [u8; 256],
    name_len: i32,
}

impl TarHeader {
    // ramfs.c fills `raw` a byte at a time rather than by memcpy, so that the
    // one place a bad offset could be introduced is bounds-checked here.
    fn set_byte(&mut self, i: i32, v: u8) -> bool {
        if i < 0 || i >= BLOCK {
            return false;
        }
        self.raw[i as usize] = v;
        return true;
    }

    fn byte(&self, i: i32) -> u8 {
        if i < 0 || i >= BLOCK {
            return 0;
        }
        return self.raw[i as usize];
    }

    // A block of all zeroes marks the end of the archive. Tar writes two of
    // them, but one is enough to stop on.
    fn is_end(&self) -> bool {
        let mut i: i32 = 0;
        while i < BLOCK {
            if self.raw[i as usize] != 0 {
                return false;
            }
            i += 1;
        }
        return true;
    }

    fn is_ustar(&self) -> bool {
        // "ustar" at offset 257, then either NUL (POSIX) or a space (GNU).
        if self.raw[257] != 117 {
            return false; // 'u'
        }
        if self.raw[258] != 115 {
            return false; // 's'
        }
        if self.raw[259] != 116 {
            return false; // 't'
        }
        if self.raw[260] != 97 {
            return false; // 'a'
        }
        if self.raw[261] != 114 {
            return false; // 'r'
        }
        return true;
    }

    // Parse a fixed-width octal field. Returns -1 on anything malformed rather
    // than a plausible-looking wrong number: a bad size field would otherwise
    // make the walk step to a wrong offset and read garbage as the next header.
    //
    // Leading spaces and NULs are skipped, trailing spaces and NULs end the
    // field, and a digit outside 0-7 or an overflow past 2^31 is an error.
    fn octal_at(&self, off: i32, len: i32) -> i32 {
        if off < 0 || len <= 0 || off + len > BLOCK {
            return -1;
        }

        let mut i: i32 = 0;
        while i < len {
            let c: u8 = self.raw[(off + i) as usize];
            if c != 32 && c != 0 {
                break;
            }
            i += 1;
        }

        let mut value: i32 = 0;
        let mut digits: i32 = 0;
        while i < len {
            let c: u8 = self.raw[(off + i) as usize];
            if c == 32 || c == 0 {
                break;
            }
            if c < 48 || c > 55 {
                return -1;
            }
            // 2^31 / 8 -- one more shift past this overflows
            if value > 268435455 {
                return -1;
            }
            value = value * 8 + ((c - 48) as i32);
            digits += 1;
            i += 1;
        }

        if digits == 0 {
            return -1;
        }

        // Whatever follows must be padding, not more digits.
        while i < len {
            let c: u8 = self.raw[(off + i) as usize];
            if c != 32 && c != 0 {
                return -1;
            }
            i += 1;
        }
        return value;
    }

    fn size(&self) -> i32 {
        return self.octal_at(124, 12);
    }

    fn typeflag(&self) -> u8 {
        return self.raw[156];
    }

    fn is_regular(&self) -> bool {
        let t: u8 = self.raw[156];
        return t == 0 || t == 48; // NUL or '0'
    }

    // The header checksum: the sum of every byte, with the checksum field
    // itself treated as eight spaces. Verifying it is what turns "this looked
    // like a header" into "this is a header" -- without it, a walk that lost
    // alignment would read file data as a header and keep going.
    fn checksum_ok(&self) -> bool {
        let stored: i32 = self.octal_at(148, 8);
        if stored < 0 {
            return false;
        }
        let mut sum: i32 = 0;
        let mut i: i32 = 0;
        while i < BLOCK {
            if i >= 148 && i < 156 {
                sum += 32;
            } else {
                sum += self.raw[i as usize] as i32;
            }
            i += 1;
        }
        return sum == stored;
    }

    // Build the full name from prefix + name, and return its length.
    //
    // ustar splits long paths across two fields; joining them here means the
    // C side only ever sees one name. Both fields are NUL-terminated only if
    // shorter than their slot, so the length is the terminator position or the
    // slot width, whichever comes first.
    fn build_name(&mut self) -> i32 {
        let mut n: i32 = 0;

        let mut p: i32 = 0;
        while p < 155 {
            let c: u8 = self.raw[(345 + p) as usize];
            if c == 0 {
                break;
            }
            p += 1;
        }

        if p > 0 {
            let mut i: i32 = 0;
            while i < p && n < NAME_MAX - 1 {
                self.name[n as usize] = self.raw[(345 + i) as usize];
                n += 1;
                i += 1;
            }
            if n < NAME_MAX - 1 {
                self.name[n as usize] = 47; // '/'
                n += 1;
            }
        }

        let mut i: i32 = 0;
        while i < 100 && n < NAME_MAX - 1 {
            let c: u8 = self.raw[i as usize];
            if c == 0 {
                break;
            }
            self.name[n as usize] = c;
            n += 1;
            i += 1;
        }

        self.name[n as usize] = 0;
        self.name_len = n;
        return n;
    }

    fn name_byte(&self, i: i32) -> u8 {
        if i < 0 || i >= self.name_len {
            return 0;
        }
        return self.name[i as usize];
    }

    fn name_length(&self) -> i32 {
        return self.name_len;
    }

    // How far to step from this header to the next one: the header block plus
    // the file's data rounded up to a whole number of blocks.
    fn next_offset(&self, size: i32) -> i32 {
        if size < 0 {
            return -1;
        }
        let blocks: i32 = (size + BLOCK - 1) / BLOCK;
        // Guard the multiply before it happens, not after it wraps.
        if blocks > 4194303 {
            return -1;
        }
        return BLOCK + blocks * BLOCK;
    }
}
