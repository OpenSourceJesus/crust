#!/usr/bin/env python3
"""AArch64 differential tester for the ShivyC arm64 back end.

For each C program it: (1) compiles with ShivyC's own arm64 back end
(`python3 -m shivyc.main --target arm64 -S`), assembles + links the result with
an aarch64 GCC, and runs it under qemu-aarch64; (2) compiles the same C directly
with the aarch64 GCC as an oracle and runs that under qemu; then compares exit
codes. Programs the arm64 back end does not yet support (it says so explicitly,
rather than miscompiling) are reported as SKIP, not FAIL -- the set of SKIPs is
exactly the work remaining.

Toolchain (override via env): CROSS_CC=aarch64-linux-gnu-gcc, QEMU=qemu-aarch64.

Usage:
    python3 tools/arm64_difftest.py            # built-in Stage 2 program set
    python3 tools/arm64_difftest.py a.c b.c    # specific C files
"""
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CROSS_CC = os.environ.get("CROSS_CC", "aarch64-linux-gnu-gcc")
QEMU = os.environ.get("QEMU", "qemu-aarch64")

# Built-in Stage 2 programs: integer-literal returns the back end supports today.
STAGE2 = [
    ("ret0", "int main(){return 0;}"),
    ("ret7", "int main(){return 7;}"),
    ("ret42", "int main(){return 42;}"),
    ("ret200", "int main(){return 200;}"),
    ("ret255", "int main(){return 255;}"),
    ("two_funcs", "long f(){return 100;} int main(){return 42;}"),
]

# Stage 3: locals, add/sub/mul/div/mod, comparisons, if/while.
STAGE3 = [
    ("if_gt", "int main(){int a=7,b=3; if(a>b) return a; return b;}"),
    ("if_ge_false", "int main(){int a=3,b=8; if(a>=b) return 1; return 0;}"),
    ("if_eq", "int main(){int a=5,b=5; if(a==b) return 42; return 0;}"),
    ("if_ne", "int main(){int a=5,b=6; if(a!=b) return 9; return 0;}"),
    ("while_sum", "int main(){int x=0,i=0; while(i<10){x=x+i; i=i+1;} return x;}"),
    ("sum_1_10", "int main(){int s=0,i=1; while(i<=10){s=s+i; i=i+1;} return s;}"),
    ("factorial5", "int main(){int n=5,f=1; while(n>1){f=f*n; n=n-1;} return f;}"),
    ("sub_chain", "int main(){int a=100,b=30,c=8; return a-b-c;}"),
    ("div", "int main(){int a=84,b=2; return a/b;}"),
    ("mod", "int main(){int a=200,b=7; return a%b;}"),
    ("mul_add", "int main(){int a=6,b=7; return a*b+0;}"),
    ("nested_if", "int main(){int x=5; if(x>0){ if(x<10) return 3; } return 9;}"),
    ("countdown", "int main(){int n=10,c=0; while(n>0){c=c+2; n=n-1;} return c;}"),
]

# Stage 4: function calls and recursion (AAPCS64 args in x0-x7).
STAGE4 = [
    ("call_add", "int add(int a,int b){return a+b;} int main(){return add(40,2);}"),
    ("fib10", "int fib(int n){if(n<2)return n; return fib(n-1)+fib(n-2);}"
              " int main(){return fib(10);}"),
    ("fact5", "int fact(int n){if(n<=1)return 1; return n*fact(n-1);}"
              " int main(){return fact(5);}"),
    ("nested_calls", "int sq(int x){return x*x;} int add3(int a,int b,int c)"
                     "{return a+b+c;} int main(){return add3(sq(3),sq(4),0);}"),
    ("five_args", "int g(int a,int b,int c,int d,int e){return a+b+c+d+e;}"
                  " int main(){return g(1,2,3,4,5);}"),
    ("eight_args", "int h(int a,int b,int c,int d,int e,int f,int g,int i)"
                   "{return a+b+c+d+e+f+g+i;} int main(){return h(1,2,3,4,5,6,7,8);}"),
    # >10 live values forces the register allocator to spill the overflow.
    ("spill12", "int main(){int a=1,b=2,c=3,d=4,e=5,f=6,g=7,h=8,i=9,j=10,k=11,"
                "l=12; return a+b+c+d+e+f+g+h+i+j+k+l;}"),
]

# Stage 6: general pointers (AddrOf of a variable, load/store through a pointer).
STAGE6 = [
    ("ptr_store", "int main(){int x=5; int *p=&x; *p=10; return x;}"),
    ("ptr_load", "int main(){int x=5; int *p=&x; return *p;}"),
    ("ptr_copy", "int main(){int x=3,y=4; int *p=&x,*q=&y; *p=*q+1; return x;}"),
    ("ptr_param", "void inc(int *p){*p=*p+1;} int main(){int x=41; inc(&x);"
                  " return x;}"),
    ("double_ptr", "int main(){int x=7; int *p=&x; int **pp=&p; **pp=99;"
                   " return x;}"),
    ("swap", "void swap(int *a,int *b){int t=*a; *a=*b; *b=t;}"
             " int main(){int x=10,y=3; swap(&x,&y); return x;}"),
]

# Stage 7: arrays and indexed access (ReadRel/SetRel; constant + variable index).
STAGE7 = [
    ("arr_const", "int main(){int a[3]; a[0]=7; a[1]=8; return a[0]+a[1];}"),
    ("arr_var", "int main(){int a[5]; int i=2; a[i]=9; return a[i];}"),
    ("arr_init", "int main(){int a[3]={1,2,3}; return a[0]+a[1]+a[2];}"),
    ("char_arr", "int main(){char s[4]; s[0]=72; s[1]=105; s[2]=0;"
                 " return s[0]+s[1];}"),
    ("arr_sumsq", "int main(){int a[5]; int s=0,i=0; while(i<5){a[i]=i*i;"
                  " i=i+1;} i=0; while(i<5){s=s+a[i]; i=i+1;} return s;}"),
    ("ptr_index", "int sum(int *p,int n){int s=0,i=0; while(i<n){s=s+p[i];"
                  " i=i+1;} return s;} int main(){int a[4]; a[0]=10;a[1]=20;"
                  "a[2]=5;a[3]=7; return sum(a,4);}"),
    ("fib_array", "int fib(int n){int f[20]; f[0]=0; f[1]=1; int i=2;"
                  " while(i<=n){f[i]=f[i-1]+f[i-2]; i=i+1;} return f[n];}"
                  " int main(){return fib(10);}"),
]

# Stage 8: structs (member access, pointer-to-struct, whole-struct copy,
# arrays of structs).
STAGE8 = [
    ("struct_basic", "struct P{int x;int y;}; int main(){struct P p; p.x=3;"
                     " p.y=4; return p.x+p.y;}"),
    ("struct_ptr", "struct P{int x;int y;}; int d(struct P *p){return p->x+p->y;}"
                   " int main(){struct P p; p.x=30; p.y=12; return d(&p);}"),
    ("struct_copy", "struct R{int a;int b;int c;}; int main(){struct R r;"
                    " r.a=1;r.b=2;r.c=3; struct R s; s=r; return s.a+s.b+s.c;}"),
    ("struct_pad", "struct P{char a; int b;}; int main(){struct P p; p.a=5;"
                   " p.b=37; return p.a+p.b;}"),
    ("array_of_struct", "struct Pt{int x;int y;}; int main(){struct Pt a[3];"
                        " a[0].x=1; a[1].x=10; a[2].x=100;"
                        " return a[0].x+a[1].x+a[2].x;}"),
    ("struct_loop", "struct Pt{int x;int y;}; int main(){struct Pt a[4]; int i=0;"
                    " while(i<4){a[i].x=i; a[i].y=i*2; i=i+1;} int s=0; i=0;"
                    " while(i<4){s=s+a[i].x+a[i].y; i=i+1;} return s;}"),
]

# Stage 9: globals -- static / file-scope storage, addressed via adrp/add.
STAGE9 = [
    ("g_set", "int g; int main(){g=5; return g;}"),
    ("g_init", "int g=7; int main(){return g;}"),
    ("g_rmw", "int g=10; int main(){g=g+1; return g;}"),
    ("g_counter", "int c=0; void inc(){c=c+1;}"
                  " int main(){inc(); inc(); inc(); return c;}"),
    ("g_static", "static int s=42; int main(){return s;}"),
    ("g_two", "int a; int b=5; int main(){a=b*2; return a+b;}"),
    ("g_array", "int a[5]; int main(){int i=0; while(i<5){a[i]=i*i; i=i+1;}"
                " return a[4];}"),
    ("g_array_init", "int a[4]={10,20,30,40}; int main(){return a[0]+a[3];}"),
    ("g_array_xfn", "int a[3]; int sum(){int s=0,i=0; while(i<3){s=s+a[i];"
                    " i=i+1;} return s;} int main(){a[0]=1;a[1]=2;a[2]=3;"
                    " return sum();}"),
    ("g_ptr", "int g=99; int main(){int *p=&g; *p=7; return g;}"),
    ("g_struct", "struct P{int x;int y;}; struct P g;"
                 " int main(){g.x=8; g.y=34; return g.x+g.y;}"),
    ("g_char_array", "char m[6]={72,105,0,0,0,0}; int main(){return m[0]+m[1];}"),
]

# Stage 10: codegen polish (immediate operands, compare+branch fusion, copy
# coalescing) plus a wide-immediate materialization fix. These lock in
# correctness of the optimized paths across comparison forms and literal sizes.
STAGE10 = [
    ("all_cmp_if", "int main(){int x=5; int r=0; if(x==5)r=r+1; if(x!=3)r=r+2;"
                   " if(x<9)r=r+4; if(x>2)r=r+8; if(x<=5)r=r+16; if(x>=5)r=r+32;"
                   " return r;}"),
    ("while_ne", "int main(){int i=0,c=0; while(i!=5){c=c+i;i=i+1;} return c;}"),
    ("while_gt", "int main(){int i=10,c=0; while(i>0){c=c+1;i=i-1;} return c;}"),
    ("nested_and", "int main(){int a=3,b=7; if(a<b && b<10){ if(a>0)"
                   " return 42; } return 0;}"),
    ("or_cond", "int main(){int x=0; if(x==0 || x==9) return 5; return 1;}"),
    ("coalesce_chain", "int main(){int a=2,b=3,c=4; int r=a*b+c; r=r-1; r=r*2;"
                       " return r;}"),
    ("imm_boundary", "int main(){int x=4095; int y=5000; if(x==4095 && y==5000)"
                     " return x+y-9000; return 0;}"),
    ("unsigned_bignum", "int f(){unsigned int a=4000000000u; unsigned int b=5;"
                        " if(a>b) return 1; return 0;} int main(){return f();}"),
    ("big_neg_lit", "int main(){int x=-100000; return x+100007;}"),
    ("long_bignum", "long f(){long x=10000000000; return x/100000000;}"
                    " int main(){return f();}"),
    ("long_max", "long f(){return 9223372036854775807L;}"
                 " int main(){long x=f(); if(x>0) return 1; return 0;}"),
]

# Stage 11: bitwise/shift/unary operators, and global-address caching.
STAGE11 = [
    ("bit_and", "int main(){int a=12,b=10; return a&b;}"),
    ("bit_or", "int main(){int a=12,b=10; return a|b;}"),
    ("bit_xor", "int main(){int a=12,b=10; return a^b;}"),
    ("bit_combined", "int main(){int x=0xF0; return (x&0x0F)|(x>>4);}"),
    ("shl_imm", "int main(){int a=1; return a<<4;}"),
    ("shr_imm", "int main(){int a=256; return a>>2;}"),
    ("shr_unsigned", "unsigned f(){unsigned a=0x80000000u; return a>>30;}"
                     " int main(){return f();}"),
    ("shr_signed", "int f(){int a=-16; return a>>2;} int main(){return f()+10;}"),
    ("shl_reg", "int main(){int a=3,b=5; return a<<b;}"),
    ("shift_long", "long f(){long a=1; return a<<40;}"
                   " int main(){long x=f(); return x>>33;}"),
    ("bit_not", "int main(){int a=5; return (~a)+10;}"),
    ("neg", "int main(){int a=5; return -a+10;}"),
    ("g_accum_loop", "int g=0; int main(){int i=0; while(i<100){g=g+i; i=i+1;}"
                     " return g;}"),
    ("g_array_cached", "int a[10]; int main(){int i=0; while(i<10){a[i]=i*i;"
                       " i=i+1;} int s=0;i=0; while(i<10){s=s+a[i];i=i+1;}"
                       " return s;}"),
    ("g_addr_cached", "int g=5; int main(){int *p=&g; *p=*p+1; return g + *p;}"),
    ("g_struct_loop", "struct P{int x;int y;}; struct P g; int main(){int i=0;"
                      " while(i<5){g.x=g.x+i; g.y=g.y+1; i=i+1;}"
                      " return g.x+g.y;}"),
]

# Stage 12: floating point, multi-dimensional arrays, compound-assignment.
STAGE12 = [
    # multi-dimensional arrays (decompose into existing IL; locked in here)
    ("md_2d_const", "int main(){int a[3][4]; a[1][2]=7; return a[1][2];}"),
    ("md_2d_var", "int main(){int a[2][3]; int i=1,j=2; a[i][j]=9;"
                  " return a[i][j];}"),
    ("md_2d_fill", "int main(){int a[3][3]; int i=0; while(i<3){int j=0;"
                   " while(j<3){a[i][j]=i*3+j; j=j+1;} i=i+1;} return a[2][2];}"),
    ("md_2d_sum", "int main(){int m[2][2]; m[0][0]=1;m[0][1]=2;m[1][0]=3;"
                  "m[1][1]=4; int s=0,i=0; while(i<2){int j=0; while(j<2){"
                  "s=s+m[i][j];j=j+1;}i=i+1;} return s;}"),
    ("md_2d_global", "int g[2][3]; int main(){g[1][2]=42; return g[1][2];}"),
    # compound-assignment
    ("ca_local", "int main(){int x=5; x+=3; x*=2; return x;}"),
    ("ca_global", "int g=0; int main(){g+=7; g-=2; return g;}"),
    ("ca_array", "int main(){int a[5]; a[2]=10; a[2]+=5; return a[2];}"),
    # floating point: arithmetic
    ("f_lit_cast", "int main(){float f=1.5; return (int)f;}"),
    ("f_add", "int main(){float a=1.5,b=2.5; return (int)(a+b);}"),
    ("f_sub", "int main(){float a=10.0,b=3.0; return (int)(a-b);}"),
    ("d_mul", "int main(){double d=3.0; d=d*2.0; return (int)d;}"),
    ("f_div", "int main(){float a=10.0,b=4.0; return (int)(a/b);}"),
    ("d_div", "int main(){double d=100.0,e=7.0; return (int)(d/e);}"),
    # floating point: calls (arg/return register convention)
    ("f_call", "float add(float a,float b){return a+b;}"
               " int main(){return (int)add(1.5,2.5);}"),
    ("d_call", "double area(double r){return 3.14159*r*r;}"
               " int main(){return (int)area(2.0);}"),
    ("f_sq", "float sq(float x){return x*x;} int main(){return (int)sq(5.0);}"),
    # floating point: comparisons
    ("f_cmp_gt", "int main(){float a=2.5; if(a>2.0) return 1; return 0;}"),
    ("d_cmp_fn", "int cmp(double a,double b){if(a<b) return 1; return 0;}"
                 " int main(){return cmp(2.5,3.5);}"),
    # floating point: conversions
    ("i_to_f", "int main(){int i=5; float f=i; f=f*2.0; return (int)f;}"),
    ("f_to_d", "int main(){float f=3.14; double d=f; return (int)(d*2.0);}"),
    ("d_trunc", "int main(){double d=7.9; return (int)d;}"),
    # floating point: loops, aggregates, globals, pointers
    ("f_accum_loop", "int main(){float s=0.0; int i=0; while(i<5){s=s+1.5;"
                     " i=i+1;} return (int)s;}"),
    ("d_pow_loop", "double pw(int n){double r=1.0; int i=0; while(i<n){"
                   "r=r*2.0; i=i+1;} return r;} int main(){return (int)pw(5);}"),
    ("f_array", "int main(){float arr[3]; arr[0]=1.5; arr[1]=2.5; arr[2]=3.0;"
                " return (int)(arr[0]+arr[1]+arr[2]);}"),
    ("d_array_loop", "int main(){double a[4]; int i=0; while(i<4){a[i]=i*1.5;"
                     " i=i+1;} return (int)(a[0]+a[1]+a[2]+a[3]);}"),
    ("f_struct", "struct V{float x;float y;}; int main(){struct V v; v.x=3.0;"
                 " v.y=4.0; return (int)(v.x*v.x+v.y*v.y);}"),
    ("f_global", "float g=2.5; int main(){g=g+1.5; return (int)g;}"),
    ("f_ptr", "int main(){float f=5.5; float *p=&f; *p=*p+1.0; return (int)f;}"),
]

# Stage 13: liveness-based linear-scan allocation with caller/callee split.
# These exercise register reuse, spills, cross-call liveness (callee-saved),
# leaf/frameless functions (caller-saved homes), and the coalescing safety
# check (swaps), all validated against the gcc-arm64 oracle.
STAGE13 = [
    # leaf functions: should be frameless with zero callee-saves
    ("ra_leaf_sq", "int sq(int x){return x*x;} int main(){return sq(12);}"),
    ("ra_leaf_poly", "int f(int a,int b,int c){return a*a+b*b+c*c;}"
                     " int main(){return f(2,3,4);}"),
    ("ra_leaf_loop", "int main(){int s=0,i=0; while(i<20){s=s+i*i; i=i+1;}"
                     " return s%256;}"),
    # register pressure forcing spills
    ("ra_pressure", "int main(){int a=1,b=2,c=3,d=4,e=5,f=6,g=7,h=8,i=9,j=10,"
                    "k=11,l=12,m=13,n=14,o=15,p=16; return a+b+c+d+e+f+g+h+i+j+"
                    "k+l+m+n+o+p+a*b+c*d+e*f;}"),
    ("ra_pressure_mix", "int main(){int s=0; int a=1,b=2,c=3,d=4,e=5,f=6,g=7,"
                        "h=8,j=9,k=10,l=11,m=12; s=a*b+c*d+e*f+g*h+j*k+l*m;"
                        " int x=a+b+c+d+e+f+g+h+j+k+l+m; return s+x;}"),
    # coalescing safety: swaps and rotates must not clobber early
    ("ra_swap", "int main(){int a=3,b=7; int t=a; a=b; b=t; return a*10+b;}"),
    ("ra_fib_iter", "int main(){int a=0,b=1,i=0; while(i<10){int t=a+b; a=b;"
                    " b=t; i=i+1;} return b;}"),
    ("ra_rotate3", "int main(){int a=1,b=2,c=3; int t=a; a=b; b=c; c=t;"
                   " return a*100+b*10+c;}"),
    # cross-call liveness: values live across a call need callee-saved homes
    ("ra_cross_call", "int sq(int x){return x*x;} int main(){int a=1,b=2,c=3,"
                      "d=4,e=5,f=6,g=7,h=8,i=9,j=10,k=11,l=12; int s=sq(a);"
                      " return s+a+b+c+d+e+f+g+h+i+j+k+l;}"),
    ("ra_args_after_call", "int h(int a,int b,int c){return a*100+b*10+c;}"
                           " int main(){int p=2,q=3,r=4; int s=h(p,q,r);"
                           " return s+p+q+r;}"),
    ("ra_nested_calls", "int f(int x){return x+1;} int g(int x){return x*2;}"
                        " int main(){return f(g(f(g(5))));}"),
    ("ra_seq_calls", "int add(int a,int b){return a+b;} int main(){"
                     "int x=add(1,2); int y=add(3,4); int z=add(5,6);"
                     " return add(add(x,y),z);}"),
    # mixed int/float pressure and float loop-carry
    ("ra_mixed", "int main(){int i=0; float fs=0.0; while(i<10){fs=fs+1.5;"
                 " i=i+1;} int isum=0,k=0; while(k<5){isum=isum+k; k=k+1;}"
                 " return isum+(int)fs;}"),
    ("ra_float_swap", "int main(){float a=1.0,b=1.0; int n=0; while(n<10){"
                      "float t=a+b; a=b; b=t; n=n+1;} return (int)b;}"),
    ("ra_float_cross", "double f(double x){return x*1.5;} int main(){"
                       "double acc=1.0; int i=0; while(i<8){acc=f(acc);"
                       " i=i+1;} return (int)acc;}"),
    # recursion and deep call trees
    ("ra_tailrec", "int rec(int n,int acc){if(n==0)return acc;"
                   " return rec(n-1,acc+n);} int main(){return rec(10,0);}"),
    ("ra_call_tree", "int a(int x){return x+1;} int b(int x){return a(x)+"
                     "a(x+1);} int c(int x){return b(x)+b(x+1);}"
                     " int main(){return c(3);}"),
]


# String literals and pointer access width. A string literal lives at a
# symbol in .data and is reached like a global; neither of these back ends
# emitted that storage at all until now. The pointer cases guard the width
# of loads and stores made *through* a pointer, which a plain ldr/str gets
# wrong in a way nothing notices unless the neighbouring bytes are read.
STRINGS = [
    ("a64_str_index", "int main(){char *s=\"hi\"; return s[0];}"),
    ("a64_str_index2", "int main(){char *s=\"hello\"; return s[4];}"),
    ("a64_str_walk", "int main(){char *s=\"abc\"; int n=0;"
                    " while(*s){ n = n + *s; s = s + 1; } return n % 251;}"),
    ("a64_str_len", "int slen(char *s){int n=0; while(s[n]) n=n+1; return n;}"
                   " int main(){return slen(\"abcdefg\");}"),
    ("a64_str_two", "int main(){char *a=\"ab\"; char *b=\"cd\";"
                   " return a[0]+b[0];}"),
    ("a64_str_array", "int main(){char a[6]=\"hi\"; return a[0]+a[1];}"),
    ("a64_str_global", "char *g = \"xy\"; int main(){return g[0]+g[1];}"),
    # Store *width* through a pointer. A plain str/sd writes 4 or 8 bytes,
    # which is invisible unless something reads the bytes that follow.
    ("a64_ptr_store_narrow",
     "int main(){char b[16]; int i; for(i=0;i<16;i++) b[i]=i+1;"
     " char *p=b; *p=9; return b[0]+b[1]*10+b[2]*100;}"),
    ("a64_ptr_store_short",
     "int main(){short a[8]; int i; for(i=0;i<8;i++) a[i]=i+1;"
     " short *p=a; *p=9; return a[0]+a[1]*10+a[2]*100;}"),
    ("a64_ptr_load_narrow",
     "int main(){char b[16]; int i; for(i=0;i<16;i++) b[i]=i*7;"
     " char *p=b; p=p+2; return (*p) / 3;}"),
    ("a64_ptr_load_short",
     "int main(){short a[8]; int i; for(i=0;i<8;i++) a[i]=i*100;"
     " short *p=a; p=p+1; return (*p) / 3;}"),
]


# Indirect calls through function pointers. The address of a function is
# recorded so a *direct* call stays a `bl`/`call`, but it must also be
# materialised, because once function pointers exist the value can be stored,
# passed, reassigned or indexed -- at which point a note to the call site is
# not enough.
FUNCPTR = [
    ("a64_fp_basic", "int a(int x){return x+1;}"
                    " int main(){int (*f)(int)=a; return f(41);}"),
    ("a64_fp_reassign", "int a(int x){return x+1;} int b(int x){return x*2;}"
                       " int main(){int (*f)(int)=a; int r=f(5); f=b;"
                       " return r+f(10);}"),
    ("a64_fp_two_args", "int add(int a,int b){return a+b;}"
                       " int main(){int (*f)(int,int)=add; return f(20,22);}"),
    ("a64_fp_param", "int a(int x){return x+1;}"
                    " int apply(int (*f)(int), int v){return f(v);}"
                    " int main(){return apply(a,41);}"),
    ("a64_fp_copy", "int a(int x){return x+1;}"
                   " int main(){int (*f)(int)=a; int (*g)(int)=f;"
                   " return g(41);}"),
    ("a64_fp_table", "int a(int x){return x+1;} int b(int x){return x*2;}"
                    " int main(){int (*t[2])(int); t[0]=a; t[1]=b;"
                    " return t[0](5)+t[1](10);}"),
    ("a64_fp_float", "double d(double x){return x*2.0;}"
                    " int main(){double (*f)(double)=d; return (int)f(21.0);}"),
    ("a64_fp_void", "int g; void s(int v){g=v;}"
                   " int main(){void (*f)(int)=s; f(37); return g;}"),
    ("a64_fp_global", "int a(int x){return x+1;} int (*gf)(int);"
                     " int main(){gf=a; return gf(41);}"),
    ("a64_fp_loop", "int a(int x){return x+1;} int b(int x){return x*2;}"
                   " int main(){int (*t[2])(int); t[0]=a; t[1]=b;"
                   " int s=0,i; for(i=0;i<2;i++) s+=t[i](i+3); return s;}"),
]


# Stack arguments. Both ABIs pass the first eight of each class in registers
# and the rest on the stack. The two targets need opposite treatments: arm64
# addresses its frame off x29, so sp can move around a call for an outgoing
# area, while RV64 addresses its frame off sp, so the outgoing area has to be
# reserved *inside* the frame and everything else shifted past it.
STACKARGS = [
    ("a64_sa_nine", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                   "int j){return a+b+c+d+e+g+h+i+j;}"
                   " int main(){return f(1,2,3,4,5,6,7,8,9);}"),
    ("a64_sa_twelve", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                     "int j,int k,int l,int m)"
                     "{return a+b+c+d+e+g+h+i+j+k+l+m;}"
                     " int main(){return f(1,2,3,4,5,6,7,8,9,10,11,12);}"),
    ("a64_sa_last_only", "int f(int a,int b,int c,int d,int e,int g,int h,"
                        "int i,int j){return j;}"
                        " int main(){return f(1,2,3,4,5,6,7,8,42);}"),
    ("a64_sa_order", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                    "int j,int k){return j*10+k;}"
                    " int main(){return f(1,2,3,4,5,6,7,8,3,7);}"),
    ("a64_sa_long", "long f(long a,long b,long c,long d,long e,long g,long h,"
                   "long i,long j){return a+b+c+d+e+g+h+i+j;}"
                   " int main(){return (int)f(1,2,3,4,5,6,7,8,9);}"),
    ("a64_sa_double", "double f(double a,double b,double c,double d,double e,"
                     "double g,double h,double i,double j)"
                     "{return a+b+c+d+e+g+h+i+j;}"
                     " int main(){return (int)f(1,2,3,4,5,6,7,8,9);}"),
    ("a64_sa_float", "float f(float a,float b,float c,float d,float e,"
                    "float g,float h,float i,float j)"
                    "{return a+b+c+d+e+g+h+i+j;}"
                    " int main(){return (int)f(1,2,3,4,5,6,7,8,9);}"),
    ("a64_sa_mixed", "int f(int a,double b,int c,double d,int e,double g,"
                    "int h,double i,int j,double k)"
                    "{return a+c+e+h+j+(int)(b+d+g+i+k);}"
                    " int main(){return f(1,1.0,2,2.0,3,3.0,4,4.0,5,5.0);}"),
    ("a64_sa_nested", "int g(int a,int b,int c,int d,int e,int f,int h,int i,"
                     "int j){return a+j;}"
                     " int f2(int a,int b,int c,int d,int e,int f,int h,int i,"
                     "int j){return g(a,b,c,d,e,f,h,i,j)+j;}"
                     " int main(){return f2(1,2,3,4,5,6,7,8,9);}"),
    ("a64_sa_locals", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                     "int j){int arr[4]; int k; for(k=0;k<4;k++) arr[k]=k;"
                     " return a+j+arr[3];}"
                     " int main(){return f(1,2,3,4,5,6,7,8,9);}"),
    ("a64_sa_recursive", "int f(int n,int b,int c,int d,int e,int g,int h,"
                        "int i,int acc){if(n==0) return acc;"
                        " return f(n-1,b,c,d,e,g,h,i,acc+n);}"
                        " int main(){return f(9,0,0,0,0,0,0,0,0);}"),
]


# arm64-only gaps closed alongside stack arguments: a variable index whose
# element size is not a power of two (needs a real multiply rather than a
# shifted addressing mode), and a whole-aggregate copy where either side is a
# global (needs its address materialised rather than a frame offset).
ARM64EXTRA = [
    ("a64_idx_struct12", "struct S{int a;int b;int c;};"
     " int main(){struct S v[4]; int i;"
     " for(i=0;i<4;i++){v[i].a=i; v[i].b=i*2; v[i].c=i*3;}"
     " int s=0; for(i=0;i<4;i++) s+=v[i].a+v[i].b+v[i].c; return s;}"),
    ("a64_idx_char5", "struct T{char x[5];};"
     " int main(){struct T t[3]; int i,j;"
     " for(i=0;i<3;i++) for(j=0;j<5;j++) t[i].x[j]=i+j;"
     " return t[2].x[4];}"),
    ("a64_idx_arr3", "int main(){int a[3][3]; int i,j;"
     " for(i=0;i<3;i++) for(j=0;j<3;j++) a[i][j]=i*3+j;"
     " int s=0; for(i=0;i<3;i++) s+=a[i][2]; return s;}"),
    ("a64_agg_from_global", "struct P{int x;int y;}; struct P g={3,4};"
     " int main(){struct P a=g; return a.x*10+a.y;}"),
    ("a64_agg_to_global", "struct P{int x;int y;}; struct P g;"
     " int main(){struct P a; a.x=5; a.y=6; g=a; return g.x*10+g.y;}"),
    ("a64_agg_global_both", "struct P{int x;int y;};"
     " struct P g1={1,2}; struct P g2;"
     " int main(){g2=g1; return g2.x*10+g2.y;}"),
]


# Variadic functions. ShivyCX's own callees read every argument from one
# contiguous stack block whose base the caller hands over in a scratch
# register, rather than from AArch64's real va_list (separate general and FP
# save areas plus four offsets). That is far simpler and, since both sides are
# ours, sufficient -- it is the same model the x86-64 back end uses.
VARIADIC = [
    ("a64_va_three", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(3,10,20,12);}"),
    ("a64_va_eight", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(8,1,2,3,4,5,6,7,8);}"),
    ("a64_va_overflow", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(12,1,2,3,4,5,6,7,8,9,10,11,12);}"),
    ("a64_va_none", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;} int main(){return vs(0)+33;}"),
    ("a64_va_twice", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(2,20,22)+vs(1,0);}"),
    ("a64_va_long", "long vl(int n, ...){__builtin_va_list ap; long s=0; int i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,long);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return (int)vl(3,100L,200L,42L) % 251;}"),
    ("a64_va_named2", "int vs(int a, int n, ...){__builtin_va_list ap;"
     " int s=a,i; __builtin_va_start(ap,n);"
     " for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(5,3,10,20,7);}"),
    # Large frames: a local buffer past the immediate range of the frame
    # instructions (arm64 stp is a scaled 7-bit field, riscv addi 12 signed).
    ("a64_bigframe", "int main(){char b[4096]; int i;"
     " for(i=0;i<4096;i++) b[i]=(char)(i&7); return b[4095]+35;}"),
    ("a64_fneg", "int main(){double d=12.5; return (int)(-d + 50);}"),
]

def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _qemu_exit(binary):
    rc, _, _ = _run([QEMU, binary])
    return rc


def check_toolchain():
    missing = []
    for tool in (CROSS_CC, QEMU):
        rc, _, _ = _run([tool, "--version"])
        if rc != 0:
            missing.append(tool)
    return missing


def test_one(name, src, workdir):
    """Returns (status, detail): status in {PASS, FAIL, SKIP, ERROR}."""
    cpath = os.path.join(workdir, name + ".c")
    with open(cpath, "w") as f:
        f.write(src if src.endswith("\n") else src + "\n")

    # ShivyC arm64 -> .s
    spath = os.path.join(workdir, name + ".s")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-S", "-o", spath, "--target", "arm64"])
    blob = (out + err).lower()
    if "not implemented" in blob or "stage 2" in blob:
        return "SKIP", "arm64 back end does not support this yet"
    if rc != 0 or not os.path.exists(spath):
        return "ERROR", "shivyc arm64 failed: %s" % (err.strip()[:200])

    # assemble + link our asm
    mybin = os.path.join(workdir, name + ".my")
    rc, _, err = _run([CROSS_CC, "-static", spath, "-o", mybin])
    if rc != 0:
        return "ERROR", "assembling our asm failed: %s" % err.strip()[:200]

    # oracle: gcc-arm64 straight from C
    orabin = os.path.join(workdir, name + ".ora")
    rc, _, err = _run([CROSS_CC, "-static", cpath, "-o", orabin])
    if rc != 0:
        return "ERROR", "oracle compile failed: %s" % err.strip()[:200]

    mine = _qemu_exit(mybin)
    ora = _qemu_exit(orabin)
    if mine == ora:
        return "PASS", "exit=%d" % mine
    return "FAIL", "mine=%d oracle=%d" % (mine, ora)


def main(argv):
    missing = check_toolchain()
    if missing:
        print("missing toolchain: %s" % ", ".join(missing))
        print("install e.g.: apt install gcc-aarch64-linux-gnu qemu-user")
        return 2

    if len(argv) > 1:
        progs = []
        for path in argv[1:]:
            with open(path) as f:
                progs.append((os.path.basename(path), f.read()))
    else:
        progs = STAGE2 + STAGE3 + STAGE4 + STAGE6 + STAGE7 + STAGE8 + STAGE9 + STAGE10 + STAGE11 + STAGE12 + STAGE13 + STRINGS + FUNCPTR + STACKARGS + ARM64EXTRA + VARIADIC

    workdir = tempfile.mkdtemp(prefix="arm64diff-")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0}
    for name, src in progs:
        status, detail = test_one(name, src, workdir)
        counts[status] += 1
        print("  %-5s %-12s %s" % (status, name, detail))

    print("\narm64 difftest: %d pass, %d fail, %d skip, %d error"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"], counts["ERROR"]))
    return 1 if (counts["FAIL"] or counts["ERROR"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
