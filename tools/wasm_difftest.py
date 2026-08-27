#!/usr/bin/env python3
"""WebAssembly differential tester for the ShivyC wasm back end.

Mirrors tools/riscv64_difftest.py: for each C program, compile it with ShivyC
(`--target wasm`), run the resulting module under node, and compare the value
returned by `main` against the exit code of the same program compiled natively
by gcc (the oracle). The two must agree mod 256, since that is all a process
exit status carries.

There is no cross-compiler and no emulator in this pipeline. ShivyC emits the
`.wasm` binary itself (shivyc/wasm.py), and node's WebAssembly implementation
both validates and runs it -- so a malformed module is caught by the engine's
validator rather than showing up as a wrong answer.

The wasm back end currently implements the integer core (locals, + - * / %, the
bitwise and shift operators, the six comparisons, if/while, direct calls,
recursion). Programs using anything that needs linear memory -- pointers,
arrays, structs, globals, string literals -- or floating point make the back end
raise, and those are reported SKIP, not FAIL: the back end refuses rather than
miscompile.

Toolchain (override via env): NODE=node, CC=gcc.
"""
import os
import subprocess
import sys
import tempfile

NODE = os.environ.get("NODE", "node")
CC = os.environ.get("CC", "gcc")

# Integer-core corpus. Deliberately parallels the riscv64 CORE list so the two
# back ends can be compared case for case, plus cases that only matter for a
# stack machine with structured control flow: deep nesting, multi-way branches,
# and loops whose exit edge is not the fallthrough.
CORE = [
    ("wasm_const", "int main(){return 42;}"),
    ("wasm_arith", "int main(){int a=2,b=3,c=4; return a*b+c-1;}"),
    ("wasm_div_mod", "int main(){int a=100,b=7; return a/b + a%b;}"),
    ("wasm_neg", "int main(){int a=3,b=10; return a-b+100;}"),
    ("wasm_unary_neg", "int main(){int a=42; int b=-a; return -b;}"),
    ("wasm_bitnot", "int main(){int a=5; return (~a) + 48;}"),
    ("wasm_cmp_all", "int main(){int a=3,b=5; int r=0;"
                     " if(a<b)r=r+1; if(b>a)r=r+10; if(a<=3)r=r+100;"
                     " if(b>=5)r=r+1000; if(a==3)r=r+10000; if(a!=b)r=r+100000;"
                     " return r%256;}"),
    ("wasm_if_else", "int cls(int x){if(x<0)return 0; if(x<10)return 1;"
                     " if(x<100)return 2; return 3;}"
                     " int main(){return cls(5)+cls(50)*4+cls(500)*16;}"),
    ("wasm_while", "int main(){int s=0,i=0; while(i<20){s=s+i; i=i+1;}"
                   " return s%256;}"),
    ("wasm_nested_loop", "int main(){int g=0,i=0; while(i<10){int j=0;"
                         " while(j<10){g=g+1; j=j+1;} i=i+1;} return g%256;}"),
    ("wasm_for", "int main(){int s=0,i; for(i=0;i<10;i++) s+=i*i;"
                 " return s%256;}"),
    ("wasm_break_continue", "int main(){int s=0,i;"
                            " for(i=0;i<20;i++){ if(i%2==0) continue;"
                            " if(i>13) break; s+=i;} return s;}"),
    ("wasm_do_while", "int main(){int i=0,s=0; do{s+=i; i++;}while(i<10);"
                      " return s;}"),
    ("wasm_leaf_call", "int sq(int x){return x*x;} int main(){return sq(12);}"),
    ("wasm_fib", "int fib(int n){if(n<2)return n; return fib(n-1)+fib(n-2);}"
                 " int main(){return fib(11)%256;}"),
    ("wasm_mutual", "int isodd(int n); int iseven(int n){if(n==0)return 1;"
                    " return isodd(n-1);} int isodd(int n){if(n==0)return 0;"
                    " return iseven(n-1);} int main(){return iseven(10)+41;}"),
    ("wasm_forward_call", "int later(int x); int main(){return later(21);}"
                          " int later(int x){return x*2;}"),
    ("wasm_multi_arg", "int f(int a,int b,int c,int d){return a*1000+b*100+"
                       "c*10+d;} int main(){return f(1,2,3,4)%256;}"),
    ("wasm_many_args", "int f(int a,int b,int c,int d,int e,int g,int h,"
                       "int i,int j,int k){return a+b+c+d+e+g+h+i+j+k;}"
                       " int main(){return f(1,2,3,4,5,6,7,8,9,10);}"),
    ("wasm_unused_param", "int f(int a,int b){return a;}"
                          " int main(){return f(42,99);}"),
    ("wasm_void_call", "int g; void nothing(int x){ x=x; }"
                       " int main(){nothing(3); return 42;}"),
    ("wasm_discard_ret", "int sq(int x){return x*x;}"
                         " int main(){sq(5); return 42;}"),
    ("wasm_swap", "int main(){int a=3,b=7; int t=a; a=b; b=t;"
                  " return a*10+b;}"),
    ("wasm_fib_iter", "int main(){int a=0,b=1,i=0; while(i<10){int t=a+b;"
                      " a=b; b=t; i=i+1;} return b;}"),
    ("wasm_pressure", "int main(){int a=1,b=2,c=3,d=4,e=5,f=6,g=7,h=8,i=9,"
                      "j=10,k=11,l=12,m=13,n=14,o=15,p=16; return (a+b+c+d+e+"
                      "f+g+h+i+j+k+l+m+n+o+p+a*b+c*d)%256;}"),
    ("wasm_deep_nest", "int main(){int r=0,i,j,k;"
                       " for(i=0;i<4;i++) for(j=0;j<4;j++) for(k=0;k<4;k++)"
                       " if((i+j+k)%3==0) r++; return r;}"),
    ("wasm_tail_rec", "int rec(int n,int acc){if(n==0)return acc;"
                      " return rec(n-1,acc+n);} int main(){return rec(10,0);}"),
    ("wasm_switch", "int f(int x){switch(x){case 1: return 10; case 2:"
                    " return 20; case 3: return 30; default: return 40;}}"
                    " int main(){return f(1)+f(2)+f(3)+f(9)%7;}"),
    ("wasm_logical", "int main(){int a=1,b=0;"
                     " return (a&&!b)*20 + (a||b)*22;}"),
    ("wasm_ternary", "int main(){int a=5; return a>3 ? 42 : 7;}"),
    ("wasm_comma_side", "int main(){int i=0,s=0; while(i<10){s+=i,i++;}"
                        " return s;}"),
]

# Width and signedness: the places a stack machine with only i32/i64 has to do
# real work, since C's char/short/unsigned distinctions have no wasm type.
CONVERSIONS = [
    ("wasm_char_trunc", "int main(){int big=300; char c=(char)big;"
                        " return (int)c + 200;}"),
    ("wasm_uchar_trunc", "int main(){int big=300; unsigned char c="
                         "(unsigned char)big; return (int)c + 198;}"),
    ("wasm_short_trunc", "int main(){int big=70000; short s=(short)big;"
                         " return ((int)s)%256;}"),
    ("wasm_signed_shift", "int main(){int a=-16; return (a>>2)+46;}"),
    ("wasm_unsigned_shift", "unsigned int u=0; int main(){unsigned int a="
                            "0xFFFFFFF0u; return (int)((a>>28))+27;}"),
    ("wasm_unsigned_div", "int main(){unsigned int a=4000000000u,b=1000000u;"
                          " return (int)(a/b)%256;}"),
    ("wasm_unsigned_cmp", "int main(){unsigned int a=4000000000u; int r=0;"
                          " if(a>100u) r=42; return r;}"),
    ("wasm_long_arith", "int main(){long a=1000000L,b=3L;"
                        " return (int)((a*b)%251);}"),
    ("wasm_long_to_int", "int main(){long a=0x1FFFFFFFFL;"
                         " return (int)(a & 0xFF) - 213;}"),
    ("wasm_int_to_long", "int main(){int a=-1; long b=(long)a;"
                         " return b==-1L ? 42 : 7;}"),
    ("wasm_shift_ops", "int main(){int a=3; return (a<<4)|(a>>1);}"),
    ("wasm_bitops", "int main(){int a=0xF0,b=0x3C;"
                    " return ((a&b)+(a|b)+(a^b))%256;}"),
]

# Linear memory: pointers, arrays, globals, structs and strings. These are the
# milestone-2 features; before it, every one of them refused.
MEMORY = [
    ("wasm_ptr_basic", "int main(){int a=42; int *p=&a; return *p;}"),
    ("wasm_ptr_write", "int main(){int a=1; int *p=&a; *p=42; return a;}"),
    ("wasm_ptr_arith", "int main(){int a[4]; int *p=a; p[0]=10; p[1]=32;"
                       " return p[0]+p[1];}"),
    ("wasm_ptr_incr", "int main(){int a[3]; int *p=a; *p=5; p++; *p=37;"
                      " return a[0]+a[1];}"),
    ("wasm_ptr_ptr", "int main(){int a=42; int *p=&a; int **q=&p;"
                     " return **q;}"),
    ("wasm_array_index", "int main(){int a[5]; int i;"
                         " for(i=0;i<5;i++) a[i]=i*i; return a[4]+a[3]+17;}"),
    ("wasm_array_sum", "int main(){int a[10]; int i,s=0;"
                       " for(i=0;i<10;i++) a[i]=i; for(i=0;i<10;i++) s+=a[i];"
                       " return s;}"),
    ("wasm_char_array", "int main(){char b[8]; int i;"
                        " for(i=0;i<8;i++) b[i]=(char)(i*3);"
                        " return b[7]+21;}"),
    ("wasm_2d_array", "int main(){int a[3][3]; int i,j,s=0;"
                      " for(i=0;i<3;i++) for(j=0;j<3;j++) a[i][j]=i*3+j;"
                      " for(i=0;i<3;i++) s+=a[i][i]; return s+30;}"),
    ("wasm_global_int", "int g=42; int main(){return g;}"),
    ("wasm_global_write", "int g; int main(){g=42; return g;}"),
    ("wasm_global_array", "int g[4]={1,2,3,4};"
                          " int main(){return g[0]+g[1]+g[2]+g[3]+32;}"),
    ("wasm_global_bss", "int g[16]; int main(){int i,s=0;"
                        " for(i=0;i<16;i++) s+=g[i]; return s+42;}"),
    ("wasm_static_local", "int f(void){static int n=0; n++; return n;}"
                          " int main(){f();f();f(); return f()+38;}"),
    ("wasm_global_across_fn", "int g=10; void bump(void){g=g+16;}"
                              " int main(){bump();bump(); return g+16;}"),
    ("wasm_struct_member", "struct S{int a; int b;};"
                           " int main(){struct S s; s.a=10; s.b=32;"
                           " return s.a+s.b;}"),
    ("wasm_struct_ptr", "struct S{int a; int b;};"
                        " int main(){struct S s; struct S *p=&s;"
                        " p->a=40; p->b=2; return p->a+p->b;}"),
    ("wasm_struct_array", "struct S{int a; int b;};"
                          " int main(){struct S s[3]; int i,t=0;"
                          " for(i=0;i<3;i++){s[i].a=i; s[i].b=i*2;}"
                          " for(i=0;i<3;i++) t+=s[i].a+s[i].b; return t+33;}"),
    ("wasm_ptr_param", "void setit(int *p, int v){*p=v;}"
                       " int main(){int a=0; setit(&a,42); return a;}"),
    ("wasm_array_param", "int sum(int *a, int n){int i,s=0;"
                         " for(i=0;i<n;i++) s+=a[i]; return s;}"
                         " int main(){int a[4]; a[0]=1;a[1]=2;a[2]=3;a[3]=36;"
                         " return sum(a,4);}"),
    ("wasm_str_index", "int main(){char *s=\"hello\"; return s[0]+s[1];}"),
    ("wasm_str_len", "int main(){char *s=\"abcd\"; int n=0;"
                     " while(s[n]) n++; return n+38;}"),
    ("wasm_recursion_frame", "int f(int n){int a[4]; a[0]=n;"
                             " if(n==0) return 0; return a[0]+f(n-1);}"
                             " int main(){return f(8)+6;}"),
    # Several functions, each needing a scratch local, with different
    # parameter counts. Scratch local indices are per function; caching them
    # across functions aimed a store at the wrong local and the module failed
    # to validate. Regression test for that.
    ("wasm_scratch_per_fn", "int one(int a,int b,int c){int x=a+b+c;"
                            " int *p=&x; *p=*p+1; return x;}"
                            " int two(void){int y=10; int *q=&y; *q=*q+5;"
                            " return y;}"
                            " int three(int a){int z=a; int *r=&z; *r=*r*2;"
                            " return z;}"
                            " int main(){return one(1,2,3)+two()+three(10)+9;}"),
    # Early returns from a framed function, many times over, plus deep
    # recursion: the shadow stack pointer must be restored on every exit path
    # or it drifts until the frame runs off the end of the stack.
    ("wasm_sp_restore", "int f(int n){int b[8]; int i;"
                        " for(i=0;i<8;i++) b[i]=i+n;"
                        " if(n%3==0) return b[0]; if(n%3==1) return b[1];"
                        " return b[2];}"
                        " int g(int n){int a[4]; a[0]=n; if(n<=0) return 0;"
                        " return a[0]+g(n-1);}"
                        " int main(){int i,s=0; for(i=0;i<20000;i++) s+=f(i);"
                        " s+=g(200); return s%256;}"),
    ("wasm_mixed_struct", "struct P{char a; int b; short c; long d;};"
                          " int t(struct P *p,int k){p->a=(char)k; p->b=k*3;"
                          " p->c=(short)(k*7); p->d=(long)k*11;"
                          " return (int)p->a+p->b+(int)p->c+(int)p->d;}"
                          " int main(){struct P s; return t(&s,5)%251;}"),
    ("wasm_alias", "int main(){int a[4]; int *p=a; int *q=&a[2];"
                   " p[0]=1;p[1]=2;q[0]=3;q[1]=36;"
                   " return a[0]+a[1]+a[2]+a[3];}"),
    ("wasm_addr_in_loop", "int main(){int s=0,i; for(i=0;i<5;i++){"
                          " int x=i*2; int *p=&x; s+=*p;} return s+22;}"),
]

# Floating point. Values and comparisons are chosen so the exit status is a
# small integer: a difftest can only observe main's low 8 bits, so every case
# reduces its result to one.
FLOATS = [
    ("wasm_f_const", "int main(){double d=12.5; return (int)d+30;}"),
    ("wasm_f_arith", "int main(){double a=1.5,b=2.25;"
                     " return (int)((a+b)*8);}"),
    ("wasm_f_div", "int main(){double a=10.0,b=4.0; return (int)(a/b*16);}"),
    ("wasm_f_sub", "int main(){double a=100.5,b=58.5; return (int)(a-b);}"),
    ("wasm_f_neg", "int main(){double d=-42.0; return (int)(-d);}"),
    ("wasm_f_negzero", "int main(){double z=0.0; double n=-z;"
                       " return (1.0/n < 0.0) ? 42 : 7;}"),
    ("wasm_float_t", "int main(){float f=1.5f,g=2.25f;"
                     " return (int)((f*g)*12);}"),
    ("wasm_f_mixed_width", "int main(){float f=1.5f; double d=2.5;"
                           " return (int)((f+d)*10)+2;}"),
    ("wasm_f_demote", "int main(){double d=1.0/3.0; float f=(float)d;"
                      " return (f==(float)(1.0/3.0)) ? 42 : 7;}"),
    ("wasm_f_promote", "int main(){float f=0.5f; double d=(double)f;"
                       " return (d==0.5) ? 42 : 7;}"),
    ("wasm_f_cmp_all", "int main(){double a=1.5,b=2.5; int r=0;"
                       " if(a<b)r+=1; if(b>a)r+=2; if(a<=1.5)r+=4;"
                       " if(b>=2.5)r+=8; if(a==1.5)r+=16; if(a!=b)r+=32;"
                       " return r+21;}"),
    ("wasm_f_int_roundtrip", "int main(){int i=-1234; double d=(double)i;"
                             " int j=(int)d; return (i==j) ? 42 : 7;}"),
    ("wasm_f_unsigned_conv", "int main(){unsigned int u=4000000000u;"
                             " double d=(double)u;"
                             " return (d>3.9e9) ? 42 : 7;}"),
    ("wasm_f_long_conv", "int main(){long l=1234567890123L;"
                         " double d=(double)l; long m=(long)d;"
                         " return (l==m) ? 42 : 7;}"),
    ("wasm_f_trunc_toward_zero", "int main(){double a=-2.75,b=2.75;"
                                 " return ((int)a)*(-10) + (int)b + 20;}"),
    ("wasm_f_char_conv", "int main(){double d=65.9; char c=(char)d;"
                         " return (int)c - 23;}"),
    ("wasm_f_array", "int main(){double a[4]; int i;"
                     " for(i=0;i<4;i++) a[i]=i*1.5;"
                     " return (int)((a[0]+a[1]+a[2]+a[3])*4)+6;}"),
    ("wasm_f_ptr", "int main(){double d=21.0; double *p=&d; *p=*p*2.0;"
                   " return (int)d;}"),
    ("wasm_f_param", "double scale(double x,double k){return x*k;}"
                     " int main(){return (int)scale(10.5,4.0);}"),
    ("wasm_f_return", "double half(double x){return x/2.0;}"
                      " int main(){return (int)half(84.0);}"),
    ("wasm_f_float_param", "float fscale(float x,float k){return x*k;}"
                           " int main(){return (int)fscale(10.5f,4.0f);}"),
    ("wasm_f_recursion", "double pow2(int n){if(n==0) return 1.0;"
                         " return 2.0*pow2(n-1);}"
                         " int main(){return (int)pow2(5)+10;}"),
    ("wasm_f_global", "double gd=2.5; float gf=1.25f;"
                      " int main(){return (int)((gd+(double)gf)*8)+12;}"),
    ("wasm_f_global_array", "double g[3]={1.5,2.5,3.0};"
                            " int main(){return (int)((g[0]+g[1]+g[2])*6)+0;}"),
    ("wasm_f_struct", "struct S{double a; float b; int c;};"
                      " int main(){struct S s; s.a=1.5; s.b=2.5f; s.c=3;"
                      " return (int)((s.a+(double)s.b)*10)+2;}"),
    ("wasm_f_loop", "int main(){double s=0.0; int i;"
                    " for(i=0;i<10;i++) s+=0.5; return (int)(s*8)+2;}"),
    ("wasm_f_nan", "int main(){double z=0.0; double nan=z/z;"
                   " int r=0; if(nan!=nan) r+=42; if(!(nan<1.0)) r+=0;"
                   " return r;}"),
    ("wasm_f_inf", "int main(){double z=0.0; double inf=1.0/z;"
                   " return (inf>1e300) ? 42 : 7;}"),
    # Documented divergence, not a bug: an out-of-range float-to-int
    # conversion is undefined in C. x86 yields INT_MIN, wasm's saturating
    # conversion yields INT_MAX. Marked XFAIL so the difference stays visible
    # and anyone who "fixes" it has to decide deliberately.
    ("wasm_f_overflow_conv", "int main(){double d=8e9; return (int)d % 251;}",
     "XFAIL"),
    ("wasm_f_ternary", "int main(){double a=1.5; return a>1.0 ? 42 : 7;}"),
]

# Variadic functions. The argument block lives in the caller's frame and its
# address travels in a wasm global -- the same shape the register back ends
# use, with a global standing in for the scratch register they have and wasm
# does not. wasm_va_nested matters most: it is safe only because the IL
# evaluates arguments into temporaries first, so two argument blocks are never
# being filled at once, which the shared global would not survive.
VARIADIC = [
    ('wasm_va_basic',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){return vs(2,20,22);}'),
    ('wasm_va_none',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){return vs(0)+42;}'),
    ('wasm_va_many',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){return vs(12,1,2,3,4,5,6,7,8,9,10,11,12);}'),
    ('wasm_va_long',
     'long vl(int n, ...){__builtin_va_list ap; long s=0; int i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,long); __builtin_va_end(ap); return s;} int main(){return (int)vl(3,100L,200L,300L)%251;}'),
    ('wasm_va_double',
     'double vd(int n, ...){__builtin_va_list ap; double s=0.0; int i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,double); __builtin_va_end(ap); return s;} int main(){return (int)(vd(3,1.5,2.5,3.0)*6);}'),
    ('wasm_va_two_named',
     'int vt(int a,int b, ...){__builtin_va_list ap; int s=a*100+b*10,i; __builtin_va_start(ap,b); for(i=0;i<2;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){return vt(1,2,3,4)%251;}'),
    ('wasm_va_nested',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){return vs(2, vs(2,5,5), vs(1,32));}'),
    ('wasm_va_twice',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){return vs(2,20,20)+vs(1,2);}'),
    ('wasm_va_recursive',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int rec(int k){if(k==0) return 0; return vs(2,k,1)+rec(k-1);} int main(){return rec(8);}'),
    ('wasm_va_with_frame',
     'int vs(int n, ...){__builtin_va_list ap; int buf[4]; int s=0,i; buf[0]=n; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s+buf[0];} int main(){return vs(2,20,20);}'),
]


# Function pointers. wasm has no code addresses, so a function pointer is an
# index into the module's function table and a call through one is
# call_indirect. Table slot 0 is deliberately left empty, which is what makes
# a call through a null pointer trap instead of dispatching somewhere.
#
# call_indirect also checks the signature at run time, so a call through a
# wrongly-typed pointer traps here where the register back ends would simply
# run -- stricter than native, and worth knowing about.
FUNCPTR = [
    ('wasm_fp_basic',
     'int sq(int x){return x*x;} int main(){int(*f)(int)=sq; return f(6)+6;}'),
    ('wasm_fp_param',
     'int sq(int x){return x*x;} int apply(int(*f)(int),int v){return f(v);} int main(){return apply(sq,6)+6;}'),
    ('wasm_fp_array',
     'int a(int x){return x+1;} int b(int x){return x*2;} int main(){int(*t[2])(int); t[0]=a; t[1]=b; return t[0](20)+t[1](10)+1;}'),
    ('wasm_fp_select',
     'int a(int x){return x+1;} int b(int x){return x*2;} int main(){int i,s=0; for(i=0;i<4;i++){int(*f)(int)=(i%2)?a:b; s+=f(i);} return s+37;}'),
    ('wasm_fp_returned',
     'int a(int x){return x+1;} int b(int x){return x*2;} int (*pick(int w))(int){return w?a:b;} int main(){return pick(0)(20)+pick(1)(1);}'),
    ('wasm_fp_struct',
     'int a(int x){return x+1;} int b(int x){return x*2;} struct O{int(*f)(int); int k;}; int main(){struct O o[2]; o[0].f=a; o[0].k=1; o[1].f=b; o[1].k=2; return o[0].f(20)+o[1].f(10)+1;}'),
    ('wasm_fp_two_args',
     'int add(int a,int b){return a+b;} int main(){int(*f)(int,int)=add; return f(20,22);}'),
    ('wasm_fp_void',
     'int g; void setg(int v){g=v;} int main(){void(*f)(int)=setg; f(42); return g;}'),
    ('wasm_fp_callback_sort',
     'int asc(int a,int b){return a-b;} void isort(int *v,int n,int(*c)(int,int)){int i,j,t; for(i=1;i<n;i++){t=v[i];j=i-1; while(j>=0&&c(v[j],t)>0){v[j+1]=v[j];j--;} v[j+1]=t;}} int main(){int v[5]; v[0]=5;v[1]=3;v[2]=9;v[3]=1;v[4]=7; isort(v,5,asc); return v[0]*10+v[4]-15;}'),
    ('wasm_fp_recursive_cb',
     'int step(int x){return x-1;} int loop(int(*f)(int),int x){while(x>0) x=f(x); return x+42;} int main(){return loop(step,10);}'),
]

# Aggregate copies: struct assignment, and aggregates moving through array
# elements, members and pointers. All lower to memory.copy, which is defined
# to handle overlapping source and destination -- as C requires.
AGGREGATE = [
    ('wasm_ag_assign',
     'struct S{int a;char b;long c;}; int main(){struct S x; x.a=10;x.b=20;x.c=12; struct S y; y=x; return y.a+(int)y.b+(int)y.c;}'),
    ('wasm_ag_from_global',
     'struct S{int a;int b;}; struct S g={40,2}; int main(){struct S z; z=g; return z.a+z.b;}'),
    ('wasm_ag_big',
     'struct B{int v[16];}; int main(){struct B a; int i; for(i=0;i<16;i++)a.v[i]=i; struct B b; b=a; return b.v[15]+27;}'),
    ('wasm_ag_into_array',
     'struct S{int a;int b;}; int main(){struct S x; x.a=40;x.b=2; struct S r[2]; r[0]=x; return r[0].a+r[0].b;}'),
    ('wasm_ag_from_array',
     'struct S{int a;int b;}; int main(){struct S r[2]; r[1].a=40; r[1].b=2; struct S y; y=r[1]; return y.a+y.b;}'),
    ('wasm_ag_through_ptr',
     'struct S{int a;int b;}; int main(){struct S x; x.a=40;x.b=2; struct S y; struct S *p=&x; struct S *q=&y; *q=*p; return y.a+y.b;}'),
    ('wasm_ag_nested',
     'struct In{int a;int b;}; struct Out{struct In i; int c;}; int main(){struct Out o; o.i.a=20;o.i.b=20;o.c=2; struct Out p; p=o; return p.i.a+p.i.b+p.c;}'),
    ('wasm_ag_self',
     'struct S{int a;int b;}; int main(){struct S x; x.a=40;x.b=2; x=x; return x.a+x.b;}'),
]

# Address constants in static initializers. There is no linker and no
# relocation here: every address is known while the module is being built, so
# a symbolic initializer resolves to a plain number in the data segment.
# Note: `int *ap = &a[2];` is deliberately absent. The front end rejects an
# address constant with an addend as a non-constant initializer, on *every*
# target -- x86-64 refuses it too, while gcc accepts it. That is a shared
# front-end limitation rather than a wasm gap, so it does not belong here.
STATICADDR = [
    ('wasm_sa_strptr',
     'static char *p = "hi"; int main(){return p[0]+2;}'),
    ('wasm_sa_extern_strptr',
     'char *q = "world"; int main(){return q[0]-77;}'),
    ('wasm_sa_str_table',
     'static char *t[3] = {"a","bb","ccc"}; int main(){int n=0,i; for(i=0;i<3;i++){int k=0; while(t[i][k]) k++; n+=k;} return n+36;}'),
    ('wasm_sa_addr_of_global',
     'int g = 5; int *gp = &g; int main(){return *gp+37;}'),
    ('wasm_sa_addr_of_array',
     'int a[4]={1,2,3,4}; int *ap = a; int main(){return ap[0]+ap[3]+37;}'),
]

# Passing and returning structs by value.
#
# There are two shapes here, and which one arrives depends on size,
# because the front end implements the SysV rule itself: a struct over 16
# bytes already reaches this back end as a hidden result pointer plus a void
# call, while 16 bytes or less is still a by-value result that wasm cannot
# express and the back end has to convert. Both bands are covered below --
# treating the first like the second passes two hidden pointers and
# misaligns every later argument.
BYVALUE = [
    ('wasm_bv_param',
     'struct S{int a; int b;}; int sum(struct S s){return s.a+s.b;} int main(){struct S x; x.a=40; x.b=2; return sum(x);}'),
    ('wasm_bv_is_copy',
     'struct S{int a; int b;}; int clob(struct S s){s.a=999; s.b=999; return 0;} int main(){struct S x; x.a=40; x.b=2; clob(x); return x.a+x.b;}'),
    ('wasm_bv_return',
     'struct S{int a; int b;}; struct S mk(int a,int b){struct S r; r.a=a; r.b=b; return r;} int main(){struct S m=mk(40,2); return m.a+m.b;}'),
    ('wasm_bv_roundtrip',
     'struct S{int a; int b;}; struct S mk(int a,int b){struct S r; r.a=a; r.b=b; return r;} int sum(struct S s){return s.a+s.b;} int main(){return sum(mk(40,2));}'),
    ('wasm_bv_two_params',
     'struct S{int a; int b;}; struct S add(struct S x,struct S y){ struct S r; r.a=x.a+y.a; r.b=x.b+y.b; return r;} int main(){struct S a; a.a=20;a.b=1; struct S b; b.a=20;b.b=1; struct S r=add(a,b); return r.a+r.b;}'),
    ('wasm_bv_nested',
     'struct S{int a; int b;}; struct S add(struct S x,struct S y){struct S r; r.a=x.a+y.a; r.b=x.b+y.b; return r;} struct S twice(struct S s){return add(s,s);} int main(){struct S x; x.a=20;x.b=1; struct S r=twice(x); return r.a+r.b;}'),
    ('wasm_bv_global_arg',
     'struct S{int a; int b;}; struct S g={20,1}; int sum(struct S s){return s.a+s.b;} int main(){return sum(g)*2;}'),
    ('wasm_bv_big_return',
     'struct B{int v[12];}; struct B mk(int b){struct B x; int i; for(i=0;i<12;i++) x.v[i]=b+i; return x;} int main(){struct B r=mk(3); return r.v[0]+r.v[11]+27;}'),
    ('wasm_bv_big_param',
     'struct B{int v[12];}; int sum(struct B b){int i,s=0; for(i=0;i<12;i++) s+=b.v[i]; return s;} int main(){struct B x; int i; for(i=0;i<12;i++) x.v[i]=i; return sum(x)-24;}'),
    ('wasm_bv_big_roundtrip',
     'struct B{int v[12];}; struct B mk(int b){struct B x; int i; for(i=0;i<12;i++) x.v[i]=b+i; return x;} int sum(struct B b){int i,s=0; for(i=0;i<12;i++) s+=b.v[i]; return s;} int main(){return sum(mk(3))%251;}'),
    ('wasm_bv_big_is_copy',
     'struct B{int v[12];}; int clob(struct B b){int i; for(i=0;i<12;i++) b.v[i]=999; return 0;} int main(){struct B x; int i; for(i=0;i<12;i++) x.v[i]=i; clob(x); return x.v[11]+31;}'),
    ('wasm_bv_mixed_params',
     'struct S{int a; int b;}; int f(int a, struct S s, int b){ return a+s.a+s.b+b;} int main(){struct S x; x.a=20;x.b=2; return f(10,x,10);}'),
    ('wasm_bv_big_mixed',
     'struct B{int v[12];}; int f(int a, struct B b, int c){int i,s=a+c; for(i=0;i<12;i++) s+=b.v[i]; return s;} int main(){struct B x; int i; for(i=0;i<12;i++) x.v[i]=i; return f(10,x,10)-44;}'),
    ('wasm_iv_indirect',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){int(*f)(int,...)=vs; return f(3,10,20,12);}'),
    ('wasm_iv_indirect_none',
     'int vs(int n, ...){__builtin_va_list ap; int s=0,i; __builtin_va_start(ap,n); for(i=0;i<n;i++) s+=__builtin_va_arg(ap,int); __builtin_va_end(ap); return s;} int main(){int(*f)(int,...)=vs; return f(0)+42;}'),
    ('wasm_bv_fp_struct',
     'struct S{int a; int b;}; int sum(struct S s){return s.a+s.b;} int main(){int(*f)(struct S)=sum; struct S x; x.a=40;x.b=2; return f(x);}'),
]

# Out of scope for this milestone. Each must SKIP -- a PASS here would mean the
# back end lowered something it has no story for, and a FAIL would mean it
# emitted a module that runs and is wrong. Listing them keeps the boundary
# under test, not just documented.
# Nothing is currently refused outright, so this list is empty. It is kept
# because it is the mechanism that catches scope drift: an entry here that
# starts passing is reported as "newly-supported" and fails the run until
# someone moves it, which is how the previous three milestones each learned
# they were done.
OUT_OF_SCOPE = []

# Programs that produce output. These go through the real WASI path: the
# module imports fd_write, the host writes to a real fd, and BOTH the stdout
# text and the exit status are compared against a natively-compiled build.
# printf is the point of the whole exercise, so it is worth testing on output
# rather than only on an exit code.
# Programs that produce output. These go through the real WASI path: the
# module imports fd_write, the host writes to a real fd, and BOTH the stdout
# text and the exit status are compared against a natively-compiled build.
# printf is the point of the whole exercise, so it deserves to be tested on
# what it prints rather than only on an exit code.
#
# The two sources differ only in the printf spelling: the wasm target has no
# variadics yet, so shivyc/include/wasi.h supplies printf1/printf2/printf3
# instead. Everything else -- the format strings, the arguments, the expected
# bytes -- is identical, which is what makes the comparison meaningful.
# Programs that produce output. These go through the real WASI path: the
# module imports fd_write, the host writes to a real fd, and BOTH the stdout
# text and the exit status are compared against a natively-compiled build.
# printf is the point of the whole exercise, so it deserves to be tested on
# what it prints rather than only on an exit code.
#
# The wasm and native sources are now *character for character identical*
# except for the header line -- one includes <wasi.h>, the other <stdio.h>.
# Before variadics landed, the wasm side had to spell printf as printf1/2/3,
# and the two sources could only be compared for equivalence rather than for
# being the same program.
STDIO = [
    ('wasm_io_puts',
     '#include <wasi.h>\nint main(void){ puts("hello"); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ puts("hello"); return 0; }'),
    ('wasm_io_putchar',
     '#include <wasi.h>\nint main(void){ int i; for(i=0;i<5;i++) putchar(65+i); putchar(10); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ int i; for(i=0;i<5;i++) putchar(65+i); putchar(10); return 0; }'),
    ('wasm_io_printf_d',
     '#include <wasi.h>\nint main(void){ printf("n=%d\\n", -12345); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("n=%d\\n", -12345); return 0; }'),
    ('wasm_io_printf_s',
     '#include <wasi.h>\nint main(void){ printf("%s=%d\\n", "answer", 42); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("%s=%d\\n", "answer", 42); return 0; }'),
    ('wasm_io_printf_x',
     '#include <wasi.h>\nint main(void){ printf("%x %X\\n", 48879, 48879); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("%x %X\\n", 48879, 48879); return 0; }'),
    ('wasm_io_printf_pad',
     '#include <wasi.h>\nint main(void){ printf("[%5d][%05d]\\n", 42, 42); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("[%5d][%05d]\\n", 42, 42); return 0; }'),
    ('wasm_io_printf_left',
     '#include <wasi.h>\nint main(void){ printf("[%-5d][%-8s][%-6x]|\\n", 42, "ab", 255); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("[%-5d][%-8s][%-6x]|\\n", 42, "ab", 255); return 0; }'),
    ('wasm_io_printf_u',
     '#include <wasi.h>\nint main(void){ printf("%u\\n", 4000000000u); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("%u\\n", 4000000000u); return 0; }'),
    ('wasm_io_printf_c',
     '#include <wasi.h>\nint main(void){ printf("%c%c\\n", 104, 105); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("%c%c\\n", 104, 105); return 0; }'),
    ('wasm_io_printf_pct',
     '#include <wasi.h>\nint main(void){ printf("100%% of %d\\n", 7); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("100%% of %d\\n", 7); return 0; }'),
    ('wasm_io_printf_neg_pad',
     '#include <wasi.h>\nint main(void){ printf("[%6d][%06d][%-6d]|\\n", -42, -42, -42); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("[%6d][%06d][%-6d]|\\n", -42, -42, -42); return 0; }'),
    ('wasm_io_printf_many',
     '#include <wasi.h>\nint main(void){ printf("%d %d %d %d %d %d %d %d\\n", 1,2,3,4,5,6,7,8); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("%d %d %d %d %d %d %d %d\\n", 1,2,3,4,5,6,7,8); return 0; }'),
    ('wasm_io_printf_mixed',
     '#include <wasi.h>\nint main(void){ printf("%s|%d|%c|%x|%ld|%u\\n", "s", -1, 90, 255, 123456789012L, 7u); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("%s|%d|%c|%x|%ld|%u\\n", "s", -1, 90, 255, 123456789012L, 7u); return 0; }'),
    ('wasm_io_loop',
     '#include <wasi.h>\nint main(void){ int i; for(i=1;i<=5;i++) printf("%d squared is %d\\n", i, i*i); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ int i; for(i=1;i<=5;i++) printf("%d squared is %d\\n", i, i*i); return 0; }'),
    ('wasm_io_long',
     '#include <wasi.h>\nint main(void){ printf("%ld\\n", 1234567890123L); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ printf("%ld\\n", 1234567890123L); return 0; }'),
    ('wasm_io_exit_code',
     '#include <wasi.h>\nint main(void){ puts("bye"); return 42; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ puts("bye"); return 42; }'),
    ('wasm_io_write',
     '#include <wasi.h>\nint main(void){ write(1, "raw\\n", 4); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ write(1, "raw\\n", 4); return 0; }'),
    ('wasm_io_str_arg',
     '#include <wasi.h>\nint main(void){ char *n = "world"; printf("hello, %s!\\n", n); return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ char *n = "world"; printf("hello, %s!\\n", n); return 0; }'),
    ('wasm_io_printf_nested',
     '#include <wasi.h>\nint main(void){ int i; for(i=0;i<3;i++){ printf("row %d:", i); int j; for(j=0;j<3;j++) printf(" %d", i*j); printf("\\n"); } return 0; }',
     "",
     '#include <stdio.h>\n#include <unistd.h>\nint main(void){ int i; for(i=0;i<3;i++){ printf("row %d:", i); int j; for(j=0;j<3;j++) printf(" %d", i*j); printf("\\n"); } return 0; }'),
]

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "wasm_run.js")


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _run_reporting(cmd):
    """Run the wasm runner in report mode.

    In this mode the program's exit status arrives as a `RESULT n` marker on
    stderr, and the runner's own exit code means only success-or-host-failure.
    The indirection exists because all 256 exit statuses are legal answers, so
    the status cannot also carry "the module was invalid".
    """
    env = dict(os.environ)
    env["WASM_RUN_REPORT"] = "1"
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def _wasm_status(text):
    """Pull the exit status out of the runner's RESULT marker."""
    for ln in text.split("\n"):
        if ln.startswith("RESULT "):
            return int(ln.split()[1]) & 0xFF
    raise ValueError("no RESULT marker in runner output")


def check_toolchain():
    missing = []
    for tool in (NODE, CC):
        rc, _, _ = _run([tool, "--version"])
        if rc != 0:
            missing.append(tool)
    return missing


def test_stdio(name, src, workdir, native_src):
    """Compile `src` for wasm and `native_src` with the host compiler, run
    both, and require the stdout text *and* the exit status to agree."""
    cpath = os.path.join(workdir, name + ".c")
    with open(cpath, "w") as f:
        f.write(src + "\n")
    npath = os.path.join(workdir, name + "_native.c")
    with open(npath, "w") as f:
        f.write(native_src + "\n")

    wpath = os.path.join(workdir, name + ".wasm")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-o", wpath, "--target", "wasm"])
    blob = out + err
    if "NotImplementedError" in blob:
        detail = "wasm back end does not support this yet"
        for ln in blob.split("\n"):
            if "NotImplementedError:" in ln:
                detail = ln.split("NotImplementedError:", 1)[1].strip()
        return "SKIP", detail
    if rc != 0 or not os.path.exists(wpath):
        return "ERROR", "shivyc wasm failed: %s" % blob.strip()[:200]

    orabin = os.path.join(workdir, name + ".ora")
    rc, _, err = _run([CC, "-w", "-std=c99", npath, "-o", orabin])
    if rc != 0:
        return "ERROR", "oracle compile failed: %s" % err.strip()[:200]

    mrc, mout, merr = _run([NODE, RUNNER, wpath])
    orc, oout, _ = _run([orabin])
    if mout != oout:
        return "FAIL", ("stdout differs: ours=%r oracle=%r"
                        % (mout[:80], oout[:80]))
    if mrc != orc:
        return "FAIL", "exit differs: ours=%d oracle=%d (stdout matched)" % (
            mrc, orc)
    return "PASS", "stdout+exit match (%d bytes)" % len(mout)


def test_one(name, src, workdir, runner):
    """Returns (status, detail): status in {PASS, FAIL, SKIP, ERROR}."""
    cpath = os.path.join(workdir, name + ".c")
    with open(cpath, "w") as f:
        f.write(src if src.endswith("\n") else src + "\n")

    wpath = os.path.join(workdir, name + ".wasm")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-o", wpath, "--target", "wasm"])
    blob = out + err
    # Match on the exception type in the untouched output. (The riscv64
    # harness lowercases before this test, so its SKIP branch can never fire
    # and every refusal is reported as an ERROR -- worth not copying.)
    if "NotImplementedError" in blob:
        detail = "wasm back end does not support this yet"
        for ln in blob.split("\n"):
            if "NotImplementedError:" in ln:
                detail = ln.split("NotImplementedError:", 1)[1].strip()
        return "SKIP", detail
    if rc != 0 or not os.path.exists(wpath):
        return "ERROR", "shivyc wasm failed: %s" % (blob.strip()[:200])

    orabin = os.path.join(workdir, name + ".ora")
    rc, _, err = _run([CC, "-w", "-std=c99", cpath, "-o", orabin])
    if rc != 0:
        return "ERROR", "oracle compile failed: %s" % err.strip()[:200]

    mine_rc, _, myerr = _run_reporting([NODE, runner, wpath])
    if mine_rc != 0 or "RESULT " not in myerr:
        # The engine rejected or trapped on our module. That is a back-end
        # bug, not a disagreement about arithmetic, so name it as one.
        return "FAIL", "wasm invalid or trapped: %s" % myerr.strip()[:160]
    mine = _wasm_status(myerr)
    ora, _, _ = _run([orabin])
    if mine == ora:
        return "PASS", "exit=%d" % mine
    return "FAIL", "mine=%d oracle=%d" % (mine, ora)


NODE_WASI_CHECK = r"""
const {WASI} = require('node:wasi');
const fs = require('fs');
const w = new WASI({version:'preview1', returnOnExit:true});
(async () => {
  const m = await WebAssembly.compile(fs.readFileSync(process.argv[2]));
  const i = await WebAssembly.instantiate(m, w.getImportObject());
  process.exitCode = w.start(i);
})();
"""


def check_real_wasi(workdir):
    """Run one module under node's *real* WASI rather than tools/wasm_run.js.

    tools/wasm_run.js is our own host, so passing against it only proves the
    module agrees with our reading of the spec. This runs the same module
    against an independent implementation: if the imports, the memory export
    or the _start contract were subtly wrong, this is what catches it.
    """
    src = ('#include <wasi.h>\n'
           'int main(void){ puts("wasi"); return 7; }\n')
    cpath = os.path.join(workdir, "realwasi.c")
    with open(cpath, "w") as f:
        f.write(src)
    wpath = os.path.join(workdir, "realwasi.wasm")
    rc, out, err = _run([sys.executable, "-m", "shivyc.main", cpath,
                         "-o", wpath, "--target", "wasm"])
    if rc != 0:
        return "ERROR", "compile failed: %s" % (out + err).strip()[:150]
    js = os.path.join(workdir, "realwasi.js")
    with open(js, "w") as f:
        f.write(NODE_WASI_CHECK)
    rc, out, err = _run([NODE, "--no-warnings", js, wpath])
    if out.strip() != "wasi":
        return "FAIL", "stdout=%r stderr=%r" % (out[:60], err.strip()[:80])
    if rc != 7:
        return "FAIL", "exit=%d, expected 7" % rc
    return "PASS", "runs under node's own WASI (stdout+exit)"


def main(argv):
    missing = check_toolchain()
    if missing:
        print("missing toolchain: %s" % ", ".join(missing))
        print("install e.g.: apt install nodejs gcc")
        return 2

    args = [a for a in argv[1:] if not a.startswith("-")]
    if args:
        progs = []
        for path in args:
            with open(path) as f:
                progs.append((os.path.basename(path), f.read()))
    else:
        progs = (CORE + CONVERSIONS + MEMORY + FLOATS + VARIADIC
                 + FUNCPTR + AGGREGATE + STATICADDR
                 + BYVALUE + STDIO
                 + OUT_OF_SCOPE)

    workdir = tempfile.mkdtemp(prefix="wasmdiff-")
    runner = RUNNER

    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "ERROR": 0,
              "XFAIL": 0, "XPASS": 0}
    expect_skip = {}
    for entry in OUT_OF_SCOPE:
        expect_skip[entry[0]] = 1

    for entry in progs:
        name = entry[0]
        src = entry[1]
        expect = entry[2] if len(entry) > 2 else ""
        if len(entry) > 3:
            status, detail = test_stdio(name, src, workdir, entry[3])
        else:
            status, detail = test_one(name, src, workdir, runner)
        if expect == "XFAIL":
            # A deliberate, documented divergence. It must still differ -- if
            # it starts matching, the note is stale and should be revisited.
            if status == "FAIL":
                status = "XFAIL"
            elif status == "PASS":
                status = "XPASS"
                detail = "now matches the oracle -- revisit the XFAIL note"
        elif name in expect_skip:
            # These are the scope boundary. A SKIP is the expected, correct
            # outcome; anything else means the back end either lowered
            # something it should have refused, or broke on it in a new way.
            if status == "SKIP":
                status = "XFAIL"
            elif status == "PASS":
                status = "XPASS"
                detail = "now supported -- move it out of OUT_OF_SCOPE"
        counts[status] += 1
        print("  %-5s %-22s %s" % (status, name, detail))

    if not args:
        status, detail = check_real_wasi(workdir)
        counts[status] = counts.get(status, 0) + 1
        print("  %-5s %-22s %s" % (status, "wasm_real_wasi_host", detail))

    print("\nwasm difftest: %d pass, %d fail, %d skip, %d error, "
          "%d refused-as-expected, %d newly-supported"
          % (counts["PASS"], counts["FAIL"], counts["SKIP"],
             counts["ERROR"], counts["XFAIL"], counts["XPASS"]))
    # An XPASS is not a failure of the compiler, but it does mean this file is
    # now lying about the scope, so it fails the run until someone updates it.
    return 1 if (counts["FAIL"] or counts["ERROR"]
                 or counts["XPASS"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
