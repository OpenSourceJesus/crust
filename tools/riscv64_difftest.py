#!/usr/bin/env python3
"""RISC-V 64 differential tester for the ShivyC riscv64 back end.

Mirrors tools/arm64_difftest.py: for each C program, compile it with ShivyC
(`--target riscv64`) to assembly, assemble + link with the RISC-V cross gcc,
run under qemu-riscv64, and compare the process exit code against the same
program compiled straight from C by gcc (the oracle). The two exit codes must
match (mod 256).

The riscv64 back end currently implements the integer core (locals, +-*/% , the
six comparisons, if/while, direct calls, recursion). Programs that use features
it does not yet lower (floats, pointers, arrays, structs, globals) make ShivyC
raise; those are reported SKIP, not FAIL -- the back end refuses rather than
miscompile.

Toolchain (override via env): CROSS_CC=riscv64-linux-gnu-gcc, QEMU=qemu-riscv64.
"""
import os
import subprocess
import sys
import tempfile

CROSS_CC = os.environ.get("CROSS_CC", "riscv64-linux-gnu-gcc")
QEMU = os.environ.get("QEMU", "qemu-riscv64")

# Integer-core corpus exercising the shared linear-scan allocator on a second
# ISA: constants, arithmetic, division/modulo, comparisons, if/while control
# flow, leaf and recursive calls, register pressure (spills), the copy-
# coalescing safety check (swaps), and cross-call liveness.
CORE = [
    ("rv_const", "int main(){return 42;}"),
    ("rv_arith", "int main(){int a=2,b=3,c=4; return a*b+c-1;}"),
    ("rv_div_mod", "int main(){int a=100,b=7; return a/b + a%b;}"),
    ("rv_neg", "int main(){int a=3,b=10; return a-b;}"),
    ("rv_cmp_all", "int main(){int a=3,b=5; int r=0;"
                   " if(a<b)r=r+1; if(b>a)r=r+10; if(a<=3)r=r+100;"
                   " if(b>=5)r=r+1000; if(a==3)r=r+10000; if(a!=b)r=r+100000;"
                   " return r%256;}"),
    ("rv_if_else", "int cls(int x){if(x<0)return 0; if(x<10)return 1;"
                   " if(x<100)return 2; return 3;}"
                   " int main(){return cls(5)+cls(50)*4+cls(500)*16;}"),
    ("rv_while", "int main(){int s=0,i=0; while(i<20){s=s+i; i=i+1;}"
                 " return s%256;}"),
    ("rv_nested_loop", "int main(){int g=0,i=0; while(i<10){int j=0;"
                       " while(j<10){g=g+1; j=j+1;} i=i+1;} return g%256;}"),
    ("rv_leaf_call", "int sq(int x){return x*x;} int main(){return sq(12);}"),
    ("rv_fib", "int fib(int n){if(n<2)return n; return fib(n-1)+fib(n-2);}"
               " int main(){return fib(11)%256;}"),
    ("rv_mutual", "int isodd(int n); int iseven(int n){if(n==0)return 1;"
                  " return isodd(n-1);} int isodd(int n){if(n==0)return 0;"
                  " return iseven(n-1);} int main(){return iseven(10);}"),
    ("rv_multi_arg", "int f(int a,int b,int c,int d){return a*1000+b*100+"
                     "c*10+d;} int main(){return f(1,2,3,4)%256;}"),
    ("rv_args_after_call", "int h(int a,int b,int c){return a+b+c;}"
                           " int main(){int p=2,q=3,r=4; int s=h(p,q,r);"
                           " return s+p+q+r;}"),
    ("rv_swap", "int main(){int a=3,b=7; int t=a; a=b; b=t;"
                " return a*10+b;}"),
    ("rv_fib_iter", "int main(){int a=0,b=1,i=0; while(i<10){int t=a+b;"
                    " a=b; b=t; i=i+1;} return b;}"),
    ("rv_pressure", "int main(){int a=1,b=2,c=3,d=4,e=5,f=6,g=7,h=8,i=9,"
                    "j=10,k=11,l=12,m=13,n=14,o=15,p=16; return (a+b+c+d+e+"
                    "f+g+h+i+j+k+l+m+n+o+p+a*b+c*d)%256;}"),
    ("rv_spills_cross_call", "int sq(int x){return x*x;} int main(){"
                             "int a=1,b=2,c=3,d=4,e=5,f=6,g=7,h=8,i=9,j=10,"
                             "k=11,l=12; int s=sq(a); return (s+a+b+c+d+e+f+"
                             "g+h+i+j+k+l)%256;}"),
    ("rv_tail_rec", "int rec(int n,int acc){if(n==0)return acc;"
                    " return rec(n-1,acc+n);} int main(){return rec(10,0);}"),
]


# Static / file-scope globals. These live at a symbol rather than in the
# frame, so every access goes through `lla` -- which is the whole reason the
# assembler needed a macro expansion that synthesises a local label. Until
# this corpus existed, no compiled C program reached that path at all.
GLOBALS = [
    ("rv_g_read", "int g = 17; int main(){return g;}"),
    ("rv_g_write", "int g = 1; int main(){g = 42; return g;}"),
    ("rv_g_rmw", "int g = 17; int main(){g = g + 8; return g;}"),
    ("rv_g_tentative", "int g; int main(){g = 9; return g + 1;}"),
    ("rv_g_many", "int a=1; int b=2; int c=3;"
                  " int main(){return a*100 + b*10 + c;}"),
    ("rv_g_loop", "int acc; int main(){int i; for(i=0;i<10;i++) acc = acc + i;"
                  " return acc;}"),
    ("rv_g_across_call", "int g = 5; int bump(int x){return x + 1;}"
                         " int main(){g = bump(g); g = bump(g); return g;}"),
    ("rv_g_func_uses", "int g = 4; int get(){return g;}"
                       " void set(int v){g = v;} int main(){set(30);"
                       " return get() + g;}"),
    ("rv_g_static", "static int s = 11; int main(){s = s * 3; return s;}"),
    ("rv_g_char", "char c = 100; int main(){c = c + 5; return c;}"),
    ("rv_g_short", "short h = 300; int main(){h = h + 44; return h % 256;}"),
    ("rv_g_uchar", "unsigned char u = 200; int main(){return u;}"),
    # Sign-extension of sub-word globals needs care: `return u` alone cannot
    # see a wrong lb-vs-lbu choice, because an exit code keeps only the low
    # byte and both readings agree there. Dividing first moves the difference
    # into that byte -- 200/2 is 100 unsigned, but -56/2 is -28 (0xE4).
    ("rv_g_uchar_div", "unsigned char u = 200; int main(){return u / 2;}"),
    # Dividing by 2 is not enough either: the wrong reading differs by a
    # multiple of 65536, and halving that still lands on the same low byte.
    # A divisor coprime with 256 is what makes the difference visible.
    #
    # `signed char` is spelled out deliberately. Plain `char` is *unsigned* on
    # the RISC-V and AArch64 psABIs but signed on x86-64, and ShivyCX treats
    # it as signed on every target -- a real divergence from the oracle, but a
    # front-end one, unrelated to how globals are loaded. Being explicit keeps
    # this case testing the load-extension choice it is meant to test rather
    # than failing for that other reason. See rv_g_plainchar_abi below.
    ("rv_g_schar_neg",
     "signed char c = -5; int main(){return (c + 305) / 3;}"),
    ("rv_g_ushort_div", "unsigned short w = 60000;"
                        " int main(){return w / 4;}"),
    ("rv_g_sshort_neg", "short w = -1000; int main(){return (w + 1300) / 3;}"),
    ("rv_g_long", "long L = 100000; int main(){L = L + 23; return L % 256;}"),
    ("rv_g_cond", "int g = 7; int main(){if (g > 5) return g * 2; return 0;}"),
    # KNOWN DIVERGENCE, not a globals bug. Plain `char` is unsigned on the
    # RISC-V and AArch64 psABIs and signed on x86-64; ShivyCX treats it as
    # signed everywhere, so this program returns 100 where gcc returns 185.
    # Recorded as XFAIL so the gap stays visible and so that fixing it (a
    # target-dependent front-end change, which would affect arm64 too) turns
    # this green rather than going unnoticed.
    ("rv_g_plainchar_abi",
     "char c = -5; int main(){return (c + 305) / 3;}", "XFAIL"),
    ("rv_g_recursive", "int depth; int rec(int n){depth = depth + 1;"
                       " if(n == 0) return depth; return rec(n-1);}"
                       " int main(){return rec(9);}"),
]


# Bitwise operations and shifts. Shifts are the interesting half: RV64 has
# separate 32- and 64-bit forms, and the right shift is arithmetic or logical
# by the *operand's* signedness, so each combination of width and signedness
# is a distinct instruction that can be picked wrongly.
BITOPS = [
    ("rv_and", "int main(){int x=0xF0,y=0x3C; return x & y;}"),
    ("rv_or", "int main(){int x=0xF0,y=0x0C; return x | y;}"),
    ("rv_xor", "int main(){int x=0xF0,y=0x3C; return (x ^ y) & 0xFF;}"),
    ("rv_and_or_xor",
     "int main(){int x=0xF0,y=0x3C; return ((x&y)|(x^y)) & 0xFF;}"),
    ("rv_not", "int main(){int x=0x0F; return (~x) & 0xFF;}"),
    ("rv_neg", "int main(){int x=40; return -x + 90;}"),
    ("rv_neg_long", "int main(){long x=40; return (int)(-x + 90);}"),
    ("rv_bit_compound",
     "int main(){int x=0xFF; x &= 0x3C; x |= 0x03; x ^= 0x01; return x;}"),
    ("rv_shl_imm", "int main(){int x=3; return x << 5;}"),
    ("rv_shr_imm", "int main(){int x=200; return x >> 2;}"),
    ("rv_shr_neg_signed",
     "int main(){int x=-64; return (x >> 2) + 100;}"),
    ("rv_shr_unsigned",
     "int main(){unsigned x=0xF0000000u; return (int)((x >> 28) & 0xFF);}"),
    ("rv_shr_neg_unsigned",
     "int main(){unsigned x=(unsigned)-64; return (int)((x >> 26) & 0xFF);}"),
    ("rv_shl_var", "int main(){int x=3,n=5; return x << n;}"),
    ("rv_shr_var", "int main(){int x=200,n=2; return x >> n;}"),
    ("rv_shr_var_signed",
     "int main(){int x=-64,n=2; return (x >> n) + 100;}"),
    ("rv_shl_long",
     "int main(){long x=3; return (int)((x << 40) >> 40);}"),
    ("rv_shr_long_signed",
     "int main(){long x=-1024; return (int)((x >> 3) + 200);}"),
    ("rv_shr_long_unsigned",
     "unsigned long g = 0xF000000000000000UL;"
     " int main(){return (int)((g >> 60) & 0xFF);}"),
    ("rv_shift_width_edge",
     "int main(){int x=1; return (x << 31) >> 31;}"),
    ("rv_bit_loop",
     "int main(){int m=0,i; for(i=0;i<8;i++) m = (m << 1) | 1; return m;}"),
    ("rv_mask_global", "int flags = 0xAA;"
                       " int main(){flags &= 0x3C; flags |= 1; return flags;}"),
]


# Integer conversions. RV64 keeps every 32-bit value sign-extended in a
# register regardless of its signedness -- that is the psABI's rule -- so a
# widening assignment from an *unsigned* int must zero-extend explicitly
# rather than move, and a narrowing one must truncate to the target width and
# re-extend by the target's signedness. Both were wrong, and neither is
# visible without a case that carries the difference past bit 7.
CONVERSIONS = [
    ("rv_cv_u32_to_long",
     "unsigned x = 2147483648u;"
     " int main(){long y = x; return (int)((y >> 32) & 0xFF);}"),
    ("rv_cv_u32_neg_to_long",
     "int main(){long L=-5; unsigned u=(unsigned)L;"
     " return (int)((unsigned long)u >> 24);}"),
    ("rv_cv_s32_to_long",
     "int main(){int x=-5; long y=x; return (int)((y >> 32) & 0xFF);}"),
    ("rv_cv_narrow_short",
     "int main(){int x=70000; short s=(short)x; return (int)(s/3);}"),
    ("rv_cv_narrow_short_neg",
     "int main(){int x=-70000; short s=(short)x; return (int)(s/3) + 100;}"),
    ("rv_cv_narrow_ushort",
     "int main(){int x=70000; unsigned short s=(unsigned short)x;"
     " return (int)(s/3);}"),
    ("rv_cv_narrow_char",
     "int main(){int x=300; char c=(char)x; return (int)c + 100;}"),
    ("rv_cv_narrow_schar_neg",
     "int main(){int x=200; signed char c=(signed char)x;"
     " return (int)(c/3) + 100;}"),
    ("rv_cv_narrow_uchar",
     "int main(){int x=300; unsigned char c=(unsigned char)x; return c;}"),
    # `return c` alone cannot tell a zero-extended unsigned char from a
    # sign-extended one: both agree in the low byte, which is all an exit
    # code keeps. Dividing moves the difference into that byte -- 200/3 is
    # 66, but -56/3 is -18 (0xEE).
    ("rv_cv_narrow_uchar_div",
     "int main(){int x=200; unsigned char c=(unsigned char)x;"
     " return c / 3;}"),
    ("rv_cv_narrow_ushort_div",
     "int main(){int x=60000; unsigned short w=(unsigned short)x;"
     " return (int)(w / 7) & 0xFF;}"),
    ("rv_cv_narrow_from_long",
     "int main(){long L=0x1234567890L; int i=(int)L; return (int)((i>>8)&0xFF);}"),
    ("rv_cv_ushort_widen",
     "unsigned short g=60000; int main(){long y=g;"
     " return (int)((y>>8)&0xFF);}"),
    ("rv_cv_uchar_widen",
     "unsigned char gb=200; int main(){long y=gb;"
     " return (int)((y>>4)&0xFF);}"),
    ("rv_cv_roundtrip",
     "int main(){unsigned u=4000000000u; long y=u; unsigned v=(unsigned)y;"
     " return (int)(v/16000000);}"),
]


# Pointers, arrays and structs. These share one mechanism: a value whose
# address is taken, or that is too big for a register, is *forced* to a frame
# slot and reached through an address rather than a register home. The
# indexed forms then compute base + chunk*count, where the base is a global
# symbol, a frame slot, or an ordinary pointer.
MEMORY = [
    ("rv_ptr_basic", "int main(){int x=9; int *p=&x; *p=*p+4; return x;}"),
    ("rv_ptr_swap", "void sw(int*a,int*b){int t=*a;*a=*b;*b=t;}"
                    " int main(){int x=3,y=8; sw(&x,&y); return x*10+y;}"),
    ("rv_ptr_arith", "int main(){int a[5]; int i; for(i=0;i<5;i++) a[i]=i+1;"
                     " int *p=a; p=p+2; return *p * 10 + *(p+1);}"),
    ("rv_ptr_to_ptr", "int main(){int x=7; int *p=&x; int **q=&p;"
                      " **q = **q + 3; return x;}"),
    ("rv_ptr_param", "int addto(int *p, int v){*p = *p + v; return *p;}"
                     " int main(){int x=10; return addto(&x, 5) + x;}"),
    ("rv_arr_local", "int main(){int a[6]; int i; for(i=0;i<6;i++) a[i]=i+1;"
                     " int s=0; for(i=0;i<6;i++) s+=a[i]; return s;}"),
    ("rv_arr_global", "int g[5];"
                      " int main(){int i; for(i=0;i<5;i++) g[i]=i*3;"
                      " return g[4];}"),
    ("rv_arr_init_global", "int g[4] = {2,4,6,8};"
                           " int main(){return g[0]+g[1]+g[2]+g[3];}"),
    ("rv_arr_const_index", "int main(){int a[4]; a[0]=1; a[1]=2; a[2]=3;"
                           " a[3]=4; return a[2]*10 + a[3];}"),
    ("rv_arr_char", "int main(){char b[8]; int i; for(i=0;i<8;i++) b[i]=i*2;"
                    " return b[5];}"),
    ("rv_arr_long", "int main(){long a[4]; int i; for(i=0;i<4;i++) a[i]=i*1000;"
                    " return (int)(a[3] / 10);}"),
    ("rv_arr_2d", "int main(){int a[3][4]; int i,j;"
                  " for(i=0;i<3;i++) for(j=0;j<4;j++) a[i][j]=i*4+j;"
                  " return a[2][3];}"),
    ("rv_arr_2d_sum", "int main(){int a[3][3]; int i,j,s=0;"
                      " for(i=0;i<3;i++) for(j=0;j<3;j++) a[i][j]=i+j;"
                      " for(i=0;i<3;i++) for(j=0;j<3;j++) s+=a[i][j];"
                      " return s;}"),
    ("rv_arr_pass", "int sum(int *a, int n){int s=0,i;"
                    " for(i=0;i<n;i++) s+=a[i]; return s;}"
                    " int main(){int a[5]; int i; for(i=0;i<5;i++) a[i]=i*2;"
                    " return sum(a,5);}"),
    ("rv_struct_basic", "struct P{int x;int y;};"
                        " int main(){struct P p; p.x=6; p.y=7;"
                        " return p.x*p.y;}"),
    ("rv_struct_ptr", "struct P{int x;int y;};"
                      " int main(){struct P p; struct P *q=&p; q->x=4;"
                      " q->y=9; return p.x + p.y;}"),
    ("rv_struct_mixed", "struct S{char c; int i; long l;};"
                        " int main(){struct S s; s.c=3; s.i=100; s.l=1000;"
                        " return s.c + s.i + (int)(s.l/10);}"),
    ("rv_struct_nested", "struct A{int x;}; struct B{struct A a; int y;};"
                         " int main(){struct B b; b.a.x=4; b.y=6;"
                         " return b.a.x * b.y;}"),
    ("rv_struct_array", "struct P{int x;int y;};"
                        " int main(){struct P a[3]; int i;"
                        " for(i=0;i<3;i++){a[i].x=i; a[i].y=i*2;}"
                        " return a[2].x*10 + a[2].y;}"),
    ("rv_struct_global", "struct P{int x;int y;} gp;"
                         " int main(){gp.x=5; gp.y=8; return gp.x*gp.y;}"),
    ("rv_struct_copy", "struct P{int x;int y;};"
                       " int main(){struct P a; a.x=3; a.y=4;"
                       " struct P b = a; return b.x*10 + b.y;}"),
    # Store *width* through a pointer. Writing 8 bytes where one was meant
    # is invisible if nothing reads the neighbouring bytes, so these check
    # that the neighbours survive. The array is padded so an over-wide store
    # stays inside it rather than corrupting an unrelated local.
    ("rv_ptr_store_narrow",
     "int main(){char b[16]; int i; for(i=0;i<16;i++) b[i]=i+1;"
     " char *p=b; *p=9; return b[0]+b[1]*10+b[2]*100;}"),
    ("rv_ptr_store_short",
     "int main(){short a[8]; int i; for(i=0;i<8;i++) a[i]=i+1;"
     " short *p=a; *p=9; return a[0]+a[1]*10+a[2]*100;}"),
    ("rv_ptr_load_narrow",
     "int main(){char b[16]; int i; for(i=0;i<16;i++) b[i]=i*7;"
     " char *p=b; p=p+2; return (*p) / 3;}"),
    ("rv_ptr_load_short",
     "int main(){short a[8]; int i; for(i=0;i<8;i++) a[i]=i*100;"
     " short *p=a; p=p+1; return (*p) / 3;}"),
    # Copy coalescing must not fold a temporary onto an address-taken
    # variable: that would give it a register home, and the pointer would
    # then read a frame slot nobody wrote. Needs a *computed* initialiser --
    # a literal one is not a coalescing candidate, so `int x = 9; &x` cannot
    # reach this path.
    ("rv_addr_coalesce",
     "int main(){int y=5; int x=y+1; int *p=&x; *p=*p+1; return x;}"),
    ("rv_addr_coalesce_call",
     "int mk(int v){return v*3;}"
     " int main(){int x=mk(4); int *p=&x; *p=*p+2; return x;}"),
    ("rv_addr_coalesce_struct",
     "struct P{int x;int y;};"
     " int main(){int t=3+4; struct P p; p.x=t; p.y=2;"
     " struct P *q=&p; q->x = q->x + 1; return p.x*10+p.y;}"),
    ("rv_addr_across_call", "int bump(int v){return v+1;}"
                            " int main(){int x=5; int *p=&x;"
                            " *p = bump(*p); *p = bump(*p); return x;}"),
]


# Floating point. lp64d gives FP its own register file and its own argument
# sequence -- fa0..fa7, counted separately from a0..a7 -- so a function's
# third parameter may arrive in a0 when the first two were doubles. Float
# literals go to .data and are loaded by address. Conversions to integer must
# round toward zero, which RISC-V spells as an explicit `rtz` operand because
# the default rounding mode is round-to-nearest.
FLOATS = [
    ("rv_f_add", "int main(){double a=1.5,b=2.25; return (int)((a+b)*4);}"),
    ("rv_f_sub", "int main(){double a=9.5,b=2.25; return (int)((a-b)*4);}"),
    ("rv_f_mul", "int main(){double d=3.5,e=2.0; return (int)(d*e+d);}"),
    ("rv_f_div", "int main(){double a=45.0,b=4.0; return (int)(a/b*4);}"),
    ("rv_f_neg", "int main(){double d=12.5; return (int)(-d + 50);}"),
    ("rv_f_chain",
     "int main(){double a=1.5,b=2.0,c=3.0; return (int)(((a+b)*c-a)/2);}"),
    ("rv_f_cmp_lt", "int main(){double a=1.5,b=2.5; if(a<b) return 33;"
                    " return 44;}"),
    ("rv_f_cmp_all",
     "int main(){double a=2.0,b=5.0; int r=0;"
     " if(a<b)r=r+1; if(b>a)r=r+2; if(a<=2.0)r=r+4; if(b>=5.0)r=r+8;"
     " if(a==2.0)r=r+16; if(a!=b)r=r+32; return r;}"),
    ("rv_f_cmp_false",
     "int main(){double a=5.0,b=2.0; int r=0;"
     " if(a<b)r=r+1; if(b>a)r=r+2; if(a<=b)r=r+4; if(b>=a)r=r+8;"
     " if(a==b)r=r+16; if(a!=b)r=r+32; return r;}"),
    ("rv_f_loop",
     "int main(){double acc=1.0; int i; for(i=0;i<6;i++) acc=acc*1.5;"
     " return (int)acc;}"),
    ("rv_f_int_to_double", "int main(){int i=7; double d=i; d=d*1.5;"
                           " return (int)d;}"),
    ("rv_f_double_to_int", "int main(){double d=9.99; return (int)d;}"),
    ("rv_f_trunc_neg", "int main(){double d=-9.99; return (int)d + 100;}"),
    ("rv_f_trunc_half", "int main(){double d=2.6; return (int)d;}"),
    ("rv_f_long_to_double",
     "int main(){long L=1000000; double d=L; return (int)(d/40000);}"),
    ("rv_f_double_to_long",
     "int main(){double d=123456.75; long L=(long)d; return (int)(L%251);}"),
    ("rv_f_unsigned_to_double",
     "int main(){unsigned u=4000000000u; double d=u;"
     " return (int)(d/20000000);}"),
    ("rv_f_double_to_unsigned",
     "int main(){double d=4000000000.0; unsigned u=(unsigned)d;"
     " return (int)(u/20000000);}"),
    ("rv_f_float_type", "int main(){float f=2.5f,g=4.0f; return (int)(f*g);}"),
    ("rv_f_float_to_double",
     "int main(){float f=1.5f; double d=f; d=d*4.0; return (int)d;}"),
    ("rv_f_double_to_float",
     "int main(){double d=9.75; float f=(float)d; return (int)(f*4.0f);}"),
    ("rv_f_float_cmp", "int main(){float a=1.5f,b=2.5f; if(a<b) return 21;"
                       " return 12;}"),
    ("rv_f_call", "double dbl(double x){return x*2.0;}"
                  " int main(){return (int)dbl(21.0);}"),
    ("rv_f_call_multi", "double sum3(double a,double b,double c)"
                        "{return a+b+c;}"
                        " int main(){return (int)sum3(1.5,2.5,3.0);}"),
    ("rv_f_call_mixed",
     "int mix(int a,double b,int c,double d){return a+c+(int)(b+d);}"
     " int main(){return mix(1,2.5,3,4.5);}"),
    ("rv_f_call_mixed2",
     "double f(double a,int b,double c,int d)"
     "{return a+c+(double)(b+d);}"
     " int main(){return (int)f(1.5,2,3.5,4);}"),
    ("rv_f_recursive",
     "double pw(double x,int n){if(n==0) return 1.0;"
     " return x*pw(x,n-1);} int main(){return (int)pw(1.5,4);}"),
    ("rv_f_cross_call",
     "double inc(double x){return x+1.0;}"
     " int main(){double a=1.0,b=10.0; a=inc(a); b=inc(b);"
     " return (int)(a*10+b);}"),
    ("rv_f_global", "double g = 1.25;"
                    " int main(){g = g*4.0; return (int)g;}"),
    ("rv_f_global_float", "float gf = 2.5f;"
                          " int main(){gf = gf*3.0f; return (int)gf;}"),
    ("rv_f_array", "int main(){double a[4]; int i;"
                   " for(i=0;i<4;i++) a[i]=i*1.5;"
                   " return (int)(a[3]*10);}"),
    ("rv_f_ptr", "int main(){double d=3.25; double *p=&d; *p = *p * 4.0;"
                 " return (int)d;}"),
    ("rv_f_struct", "struct V{double x; double y;};"
                    " int main(){struct V v; v.x=1.5; v.y=2.5;"
                    " return (int)((v.x+v.y)*4);}"),
    ("rv_f_pressure",
     "int main(){double a=1.0,b=2.0,c=3.0,d=4.0,e=5.0,f=6.0,g=7.0,h=8.0;"
     " double s=a+b+c+d+e+f+g+h; s=s*a+b*c+d*e+f*g+h;"
     " return (int)s % 251;}"),
]


# String literals and pointer access width. A string literal lives at a
# symbol in .data and is reached like a global; neither of these back ends
# emitted that storage at all until now. The pointer cases guard the width
# of loads and stores made *through* a pointer, which a plain ldr/str gets
# wrong in a way nothing notices unless the neighbouring bytes are read.
STRINGS = [
    ("rv_str_index", "int main(){char *s=\"hi\"; return s[0];}"),
    ("rv_str_index2", "int main(){char *s=\"hello\"; return s[4];}"),
    ("rv_str_walk", "int main(){char *s=\"abc\"; int n=0;"
                    " while(*s){ n = n + *s; s = s + 1; } return n % 251;}"),
    ("rv_str_len", "int slen(char *s){int n=0; while(s[n]) n=n+1; return n;}"
                   " int main(){return slen(\"abcdefg\");}"),
    ("rv_str_two", "int main(){char *a=\"ab\"; char *b=\"cd\";"
                   " return a[0]+b[0];}"),
    ("rv_str_array", "int main(){char a[6]=\"hi\"; return a[0]+a[1];}"),
    ("rv_str_global", "char *g = \"xy\"; int main(){return g[0]+g[1];}"),
    # Store *width* through a pointer. A plain str/sd writes 4 or 8 bytes,
    # which is invisible unless something reads the bytes that follow.
    ("rv_ptr_store_narrow",
     "int main(){char b[16]; int i; for(i=0;i<16;i++) b[i]=i+1;"
     " char *p=b; *p=9; return b[0]+b[1]*10+b[2]*100;}"),
    ("rv_ptr_store_short",
     "int main(){short a[8]; int i; for(i=0;i<8;i++) a[i]=i+1;"
     " short *p=a; *p=9; return a[0]+a[1]*10+a[2]*100;}"),
    ("rv_ptr_load_narrow",
     "int main(){char b[16]; int i; for(i=0;i<16;i++) b[i]=i*7;"
     " char *p=b; p=p+2; return (*p) / 3;}"),
    ("rv_ptr_load_short",
     "int main(){short a[8]; int i; for(i=0;i<8;i++) a[i]=i*100;"
     " short *p=a; p=p+1; return (*p) / 3;}"),
]


# Indirect calls through function pointers. The address of a function is
# recorded so a *direct* call stays a `bl`/`call`, but it must also be
# materialised, because once function pointers exist the value can be stored,
# passed, reassigned or indexed -- at which point a note to the call site is
# not enough.
FUNCPTR = [
    ("rv_fp_basic", "int a(int x){return x+1;}"
                    " int main(){int (*f)(int)=a; return f(41);}"),
    ("rv_fp_reassign", "int a(int x){return x+1;} int b(int x){return x*2;}"
                       " int main(){int (*f)(int)=a; int r=f(5); f=b;"
                       " return r+f(10);}"),
    ("rv_fp_two_args", "int add(int a,int b){return a+b;}"
                       " int main(){int (*f)(int,int)=add; return f(20,22);}"),
    ("rv_fp_param", "int a(int x){return x+1;}"
                    " int apply(int (*f)(int), int v){return f(v);}"
                    " int main(){return apply(a,41);}"),
    ("rv_fp_copy", "int a(int x){return x+1;}"
                   " int main(){int (*f)(int)=a; int (*g)(int)=f;"
                   " return g(41);}"),
    ("rv_fp_table", "int a(int x){return x+1;} int b(int x){return x*2;}"
                    " int main(){int (*t[2])(int); t[0]=a; t[1]=b;"
                    " return t[0](5)+t[1](10);}"),
    ("rv_fp_float", "double d(double x){return x*2.0;}"
                    " int main(){double (*f)(double)=d; return (int)f(21.0);}"),
    ("rv_fp_void", "int g; void s(int v){g=v;}"
                   " int main(){void (*f)(int)=s; f(37); return g;}"),
    ("rv_fp_global", "int a(int x){return x+1;} int (*gf)(int);"
                     " int main(){gf=a; return gf(41);}"),
    ("rv_fp_loop", "int a(int x){return x+1;} int b(int x){return x*2;}"
                   " int main(){int (*t[2])(int); t[0]=a; t[1]=b;"
                   " int s=0,i; for(i=0;i<2;i++) s+=t[i](i+3); return s;}"),
]


# Stack arguments. Both ABIs pass the first eight of each class in registers
# and the rest on the stack. The two targets need opposite treatments: arm64
# addresses its frame off x29, so sp can move around a call for an outgoing
# area, while RV64 addresses its frame off sp, so the outgoing area has to be
# reserved *inside* the frame and everything else shifted past it.
STACKARGS = [
    ("rv_sa_nine", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                   "int j){return a+b+c+d+e+g+h+i+j;}"
                   " int main(){return f(1,2,3,4,5,6,7,8,9);}"),
    ("rv_sa_twelve", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                     "int j,int k,int l,int m)"
                     "{return a+b+c+d+e+g+h+i+j+k+l+m;}"
                     " int main(){return f(1,2,3,4,5,6,7,8,9,10,11,12);}"),
    ("rv_sa_last_only", "int f(int a,int b,int c,int d,int e,int g,int h,"
                        "int i,int j){return j;}"
                        " int main(){return f(1,2,3,4,5,6,7,8,42);}"),
    ("rv_sa_order", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                    "int j,int k){return j*10+k;}"
                    " int main(){return f(1,2,3,4,5,6,7,8,3,7);}"),
    ("rv_sa_long", "long f(long a,long b,long c,long d,long e,long g,long h,"
                   "long i,long j){return a+b+c+d+e+g+h+i+j;}"
                   " int main(){return (int)f(1,2,3,4,5,6,7,8,9);}"),
    ("rv_sa_double", "double f(double a,double b,double c,double d,double e,"
                     "double g,double h,double i,double j)"
                     "{return a+b+c+d+e+g+h+i+j;}"
                     " int main(){return (int)f(1,2,3,4,5,6,7,8,9);}"),
    ("rv_sa_float", "float f(float a,float b,float c,float d,float e,"
                    "float g,float h,float i,float j)"
                    "{return a+b+c+d+e+g+h+i+j;}"
                    " int main(){return (int)f(1,2,3,4,5,6,7,8,9);}"),
    ("rv_sa_mixed", "int f(int a,double b,int c,double d,int e,double g,"
                    "int h,double i,int j,double k)"
                    "{return a+c+e+h+j+(int)(b+d+g+i+k);}"
                    " int main(){return f(1,1.0,2,2.0,3,3.0,4,4.0,5,5.0);}"),
    ("rv_sa_nested", "int g(int a,int b,int c,int d,int e,int f,int h,int i,"
                     "int j){return a+j;}"
                     " int f2(int a,int b,int c,int d,int e,int f,int h,int i,"
                     "int j){return g(a,b,c,d,e,f,h,i,j)+j;}"
                     " int main(){return f2(1,2,3,4,5,6,7,8,9);}"),
    ("rv_sa_locals", "int f(int a,int b,int c,int d,int e,int g,int h,int i,"
                     "int j){int arr[4]; int k; for(k=0;k<4;k++) arr[k]=k;"
                     " return a+j+arr[3];}"
                     " int main(){return f(1,2,3,4,5,6,7,8,9);}"),
    ("rv_sa_recursive", "int f(int n,int b,int c,int d,int e,int g,int h,"
                        "int i,int acc){if(n==0) return acc;"
                        " return f(n-1,b,c,d,e,g,h,i,acc+n);}"
                        " int main(){return f(9,0,0,0,0,0,0,0,0);}"),
]


# Variadic functions. ShivyCX's own callees read every argument from one
# contiguous stack block whose base the caller hands over in a scratch
# register, rather than from AArch64's real va_list (separate general and FP
# save areas plus four offsets). That is far simpler and, since both sides are
# ours, sufficient -- it is the same model the x86-64 back end uses.
VARIADIC = [
    ("rv_va_three", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(3,10,20,12);}"),
    ("rv_va_eight", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(8,1,2,3,4,5,6,7,8);}"),
    ("rv_va_overflow", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(12,1,2,3,4,5,6,7,8,9,10,11,12);}"),
    ("rv_va_none", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;} int main(){return vs(0)+33;}"),
    ("rv_va_twice", "int vs(int n, ...){__builtin_va_list ap; int s=0,i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(2,20,22)+vs(1,0);}"),
    ("rv_va_long", "long vl(int n, ...){__builtin_va_list ap; long s=0; int i;"
     " __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,long);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return (int)vl(3,100L,200L,42L) % 251;}"),
    ("rv_va_named2", "int vs(int a, int n, ...){__builtin_va_list ap;"
     " int s=a,i; __builtin_va_start(ap,n);"
     " for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int);"
     " __builtin_va_end(ap); return s;}"
     " int main(){return vs(5,3,10,20,7);}"),
    # Large frames: a local buffer past the immediate range of the frame
    # instructions (arm64 stp is a scaled 7-bit field, riscv addi 12 signed).
    ("rv_bigframe", "int main(){char b[4096]; int i;"
     " for(i=0;i<4096;i++) b[i]=(char)(i&7); return b[4095]+35;}"),
    ("rv_fneg", "int main(){double d=12.5; return (int)(-d + 50);}"),
]

def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


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

    spath = os.path.join(workdir, name + ".s")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-S", "-o", spath, "--target", "riscv64"])
    blob = (out + err).lower()
    # Detect the exception type, not a phrase in its message: wordings vary
    # ("only the integer core is implemented"), and matching on prose reports
    # a known gap as a hard error.
    if "NotImplementedError" in blob:
        detail = "riscv64 back end does not support this yet"
        for ln in blob.split("\n"):
            if "NotImplementedError:" in ln:
                detail = ln.split("NotImplementedError:", 1)[1].strip()
        return "SKIP", detail
    if rc != 0 or not os.path.exists(spath):
        return "ERROR", "shivyc riscv64 failed: %s" % (err.strip()[:200])

    mybin = os.path.join(workdir, name + ".my")
    rc, _, err = _run([CROSS_CC, "-static", spath, "-o", mybin])
    if rc != 0:
        return "ERROR", "assembling our asm failed: %s" % err.strip()[:200]

    orabin = os.path.join(workdir, name + ".ora")
    rc, _, err = _run([CROSS_CC, "-static", cpath, "-o", orabin])
    if rc != 0:
        return "ERROR", "oracle compile failed: %s" % err.strip()[:200]

    mine, _, _ = _run([QEMU, mybin])
    ora, _, _ = _run([QEMU, orabin])
    if mine == ora:
        return "PASS", "exit=%d" % mine
    return "FAIL", "mine=%d oracle=%d" % (mine, ora)


def main(argv):
    missing = check_toolchain()
    if missing:
        print("missing toolchain: %s" % ", ".join(missing))
        print("install e.g.: apt install gcc-riscv64-linux-gnu qemu-user")
        return 2

    if len(argv) > 1:
        progs = []
        for path in argv[1:]:
            with open(path) as f:
                progs.append((os.path.basename(path), f.read()))
    else:
        progs = (CORE + GLOBALS + BITOPS + CONVERSIONS + MEMORY
                 + FLOATS + STRINGS + FUNCPTR
                 + STACKARGS + VARIADIC)

    workdir = tempfile.mkdtemp(prefix="riscv64diff-")
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0,
              "XFAIL": 0, "XPASS": 0}
    for entry in progs:
        name = entry[0]
        src = entry[1]
        expect = entry[2] if len(entry) > 2 else ""
        status, detail = test_one(name, src, workdir)
        if expect == "XFAIL":
            # A known, documented divergence. It must still *fail* -- if it
            # starts passing, say so loudly rather than silently swallowing
            # the good news.
            if status == "FAIL":
                status = "XFAIL"
            elif status == "PASS":
                status = "XPASS"
                detail = "now matches the oracle -- remove the XFAIL marker"
        counts[status] += 1
        print("  %-5s %-20s %s" % (status, name, detail))

    print("\nriscv64 difftest: %d pass, %d fail, %d skip, %d error, "
          "%d xfail, %d xpass"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"],
             counts["ERROR"], counts["XFAIL"], counts["XPASS"]))
    # An XPASS is not a success: a documented divergence quietly disappearing
    # means the note is now wrong, so it fails the run until someone looks.
    return 1 if (counts["FAIL"] or counts["ERROR"]
                 or counts["XPASS"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
