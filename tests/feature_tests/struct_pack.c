// Struct packing: `#pragma pack`, `_Pragma("pack(..)")` and
// `__attribute__((packed))`. ShivyC used to ignore all three and lay every
// struct out naturally, so one source had one layout under gcc and another
// here. Every size and offset below is gcc's; each failed check sets a bit.
//
// The member accesses matter as much as the sizes: a packed member may sit
// at an address its alignment does not divide, and ShivyC reads and writes
// such a member a byte at a time, which is what keeps it correct on targets
// that trap on a misaligned word access.

#pragma pack(push, 1)
struct In { short s; int i; };
struct P { unsigned char tag; int id; double d; struct In in; long long q; };
#pragma pack(pop)

struct N { unsigned char a; int b; };                 // after pop: natural
struct __attribute__((packed)) A { char c; short s; long l; };
struct B { char c; double d; } __attribute__((packed));
_Pragma("pack(2)") struct C { char c; int i; }; _Pragma("pack()")
struct W { char c; struct P p; };                     // natural, holds packed
union U { char c; int i; } __attribute__((packed));

#pragma pack(4)
struct D { char c; double d; };                       // 4, not 8
#pragma pack()

struct P arr[3];

int read_id(struct P *p) { return p->id; }

int main() {
  int r = 0;

  if (sizeof(struct P) != 27) r |= 1;
  if (sizeof(struct N) != 8) r |= 2;
  if (sizeof(struct A) != 11) r |= 4;
  if (sizeof(struct B) != 9) r |= 8;
  if (sizeof(struct C) != 6 || _Alignof(struct C) != 2) r |= 16;
  if (sizeof(struct W) != 28 || _Alignof(struct W) != 1) r |= 32;
  if (sizeof(struct D) != 12) r |= 64;
  if (sizeof(union U) != 4 || _Alignof(union U) != 1) r |= 128;

  struct P local;
  local.tag = 7;
  local.id = 0x01020304;
  local.d = 2.5;
  local.in.s = -3;
  local.in.i = 99;
  local.q = -5;
  arr[1] = local;

  // `id` is at offset 1, so arr[1].id is at an odd address.
  unsigned char *raw = (unsigned char *)&arr[1];
  if (raw[0] != 7 || raw[1] != 4 || raw[4] != 1) r |= 256;

  struct P *p = &arr[1];
  if (p->id != 0x01020304 || read_id(p) != 0x01020304) r |= 512;
  if (p->d != 2.5 || p->q != -5) r |= 1024;
  if (p->in.s != -3 || p->in.i != 99) r |= 2048;

  p->in.i = 12345;
  arr[2].id = 41;
  arr[2].id += 1;
  if (arr[1].in.i != 12345 || arr[2].id != 42) r |= 4096;

  struct W w;
  w.p.id = 77;
  if (w.p.id != 77) r |= 8192;

  return r;
}

// Return: 0
