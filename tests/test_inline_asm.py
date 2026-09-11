"""Tests for the Minikraft-scoped inline-assembly subset.

This is not general extended asm; it covers exactly what the Minikraft
unikernel uses: bare side-effect templates (mfence/hlt/sti/empty barrier),
port I/O with `a`/`=a` and `Nd` constraints, and a single `m` memory operand
(lidt). Bare templates are run directly; the privileged port-I/O and lidt
forms cannot run in user space, so those tests assert that compilation,
assembly, and linking succeed and that the emitted assembly is correct.
"""

import os
import subprocess
import tempfile
import unittest

import shivyc.main
from shivyc.errors import error_collector


class _Args:
    show_reg_alloc_perf = False
    variables_on_stack = False
    simd_pack_globals = False
    stackless_calls = False
    metamorphic = False
    opt_level = 0

    def __init__(self, files, output_name):
        self.files = files
        self.output_name = output_name


def _build(source):
    """Compile+assemble+link; return (exit_code, asm_text)."""
    workdir = tempfile.mkdtemp()
    c_path = os.path.join(workdir, "prog.c")
    out_path = os.path.join(workdir, "prog")
    with open(c_path, "w") as f:
        f.write(source)
    args = _Args([c_path], [out_path])
    shivyc.main.get_arguments = lambda: args
    error_collector.show = lambda: True
    error_collector.clear()
    rc = shivyc.main.main()
    assert rc == 0, "compilation failed"
    asm = open(os.path.join(workdir, "prog.s")).read()
    return subprocess.run([out_path]).returncode, asm


class TestBareAsm(unittest.TestCase):
    def test_mfence_runs(self):
        rc, _ = _build(
            "int main(void){ asm volatile(\"mfence\" ::: \"memory\"); "
            "return 7; }")
        self.assertEqual(rc, 7)

    def test_empty_barrier_emits_nothing(self):
        rc, asm = _build(
            "int main(void){ asm volatile(\"\" ::: \"memory\"); return 8; }")
        self.assertEqual(rc, 8)
        # An empty template must not emit a stray instruction or the per-asm
        # AT&T wrap. The file footer is now spelled `.att_syntax prefix` too
        # (FreeBSD's assembler rejects `noprefix`), so look at the body only
        # rather than the whole file.
        body = asm[asm.index("main:"):asm.index(".note.GNU-stack")]
        self.assertNotIn(".att_syntax", body)

    def test_plain_nop(self):
        rc, _ = _build(
            "int f(void){ asm volatile(\"nop\"); return 9; }"
            "int main(void){ return f(); }")
        self.assertEqual(rc, 9)


class TestPortIO(unittest.TestCase):
    def test_inb_outb_codegen(self):
        # Cannot execute privileged in/out in user space; check it builds and
        # the operands are substituted into correct, sized AT&T registers.
        rc, asm = _build(
            "unsigned char inb(unsigned short port){\n"
            "  unsigned char value;\n"
            "  asm volatile(\"inb %1, %0\" : \"=a\"(value) : \"Nd\"(port));\n"
            "  return value; }\n"
            "void outb(unsigned short port, unsigned char value){\n"
            "  asm volatile(\"outb %0, %1\" : : \"a\"(value), \"Nd\"(port)); }\n"
            "int main(void){ return 0; }")
        self.assertEqual(rc, 0)
        self.assertIn("inb %dx, %al", asm)
        self.assertIn("outb %al, %dx", asm)

    def test_inw_word_sized(self):
        rc, asm = _build(
            "unsigned short inw(unsigned short port){\n"
            "  unsigned short value;\n"
            "  asm volatile(\"inw %1, %0\" : \"=a\"(value) : \"Nd\"(port));\n"
            "  return value; }\n"
            "int main(void){ return 0; }")
        self.assertEqual(rc, 0)
        self.assertIn("inw %dx, %ax", asm)


class TestMemoryOperand(unittest.TestCase):
    def test_lidt_m_operand(self):
        rc, asm = _build(
            "struct idtptr { unsigned short limit; unsigned long base; };\n"
            "struct idtptr idtp;\n"
            "void load_idt(void){"
            " asm volatile(\"lidt %0\" : : \"m\"(idtp)); }\n"
            "int main(void){ return 0; }")
        self.assertEqual(rc, 0)
        # The `m` operand's address is staged into some register, which the
        # lidt then dereferences. (Which register is allocator-chosen.)
        import re
        m = re.search(r"lidt \(%(\w+)\)", asm)
        self.assertIsNotNone(m, asm)
        reg = m.group(1)
        self.assertIn("lea {}, [idtp]".format(reg), asm)


class TestVoidParamDefinition(unittest.TestCase):
    def test_void_param_in_definition(self):
        rc, _ = _build(
            "int f(void){ return 42; } int main(void){ return f(); }")
        self.assertEqual(rc, 42)



class TestSyscallConstraints(unittest.TestCase):
    """Extended-asm constraint mapping + GCC register-asm bindings, as used by
    musl's syscall wrappers."""

    def test_distinct_input_registers(self):
        # D/S/d must map to rdi/rsi/rdx (a prior bug collapsed them all to
        # rdx). out = a - b + c with a=10,b=3,c=5 -> 12, only if each operand
        # lands in its own register.
        rc, asm = _build(
            "long f(long a, long b, long c){ long out; "
            "__asm__ volatile(\"mov %1,%0; sub %2,%0; add %3,%0\" "
            ": \"=a\"(out) : \"D\"(a), \"S\"(b), \"d\"(c)); return out; }"
            "int main(void){ return (int)f(10, 3, 5); }")
        self.assertEqual(rc, 12)
        self.assertIn("rdi", asm)
        self.assertIn("rsi", asm)

    def test_register_asm_binding_r10_r8_r9(self):
        # `register long rN __asm__("rN")` pins a value to a specific register,
        # which musl uses for syscall args 4/5/6. out = r10+r8+r9 = 27.
        rc, _ = _build(
            "long f(void){ register long r10 __asm__(\"r10\") = 10; "
            "register long r8 __asm__(\"r8\") = 8; "
            "register long r9 __asm__(\"r9\") = 9; long out; "
            "__asm__ volatile(\"mov %1,%0; add %2,%0; add %3,%0\" "
            ": \"=a\"(out) : \"r\"(r10), \"r\"(r8), \"r\"(r9)); return out; }"
            "int main(void){ return (int)f(); }")
        self.assertEqual(rc, 27)

    def test_register_keyword_plain(self):
        rc, _ = _build("int main(void){ register int x = 5; return x; }")
        self.assertEqual(rc, 5)

    def test_c99_static_array_parameter(self):
        # `T name[static N]` parameter hint must parse (and be ignored).
        rc, _ = _build(
            "void g(char b[static 8], int n){ b[0] = (char)n; } "
            "int main(void){ char a[8]; g(a, 42); return a[0]; }")
        self.assertEqual(rc, 42)


class TestExtendedAsmOutputConstraints(unittest.TestCase):
    """=m (memory output), =r (register output), and "N" (matching in/out)
    constraints, as used by musl's atomics. Each mirrors an atomic_arch.h op."""

    def test_a_cas_hit_writes_memory(self):
        # cmpxchg with =a + =m: on match, the new value is stored to memory.
        rc, _ = _build(
            'static inline int a_cas(volatile int*p,int t,int s){'
            '__asm__ __volatile__("lock ; cmpxchg %3, %1"'
            ':"=a"(t),"=m"(*p):"a"(t),"r"(s):"memory");return t;}'
            'int main(void){int x=10;int old=a_cas(&x,10,20);'
            'return (old==10 && x==20)?55:1;}')
        self.assertEqual(rc, 55)

    def test_a_swap_matching_constraint(self):
        # xchg with =r + =m and "0" matching in/out operand.
        rc, _ = _build(
            'static inline int a_swap(volatile int*p,int v){'
            '__asm__ __volatile__("xchg %0, %1"'
            ':"=r"(v),"=m"(*p):"0"(v):"memory");return v;}'
            'int main(void){int y=5;int old=a_swap(&y,8);'
            'return (old==5 && y==8)?77:1;}')
        self.assertEqual(rc, 77)

    def test_a_fetch_add_matching(self):
        rc, _ = _build(
            'static inline int a_fadd(volatile int*p,int v){'
            '__asm__ __volatile__("lock ; xadd %0, %1"'
            ':"=r"(v),"=m"(*p):"0"(v):"memory");return v;}'
            'int main(void){int z=100;int old=a_fadd(&z,7);'
            'return (old==100 && z==107)?88:1;}')
        self.assertEqual(rc, 88)

    def test_a_store_memory_output(self):
        rc, _ = _build(
            'static inline void a_store(volatile int*p,int x){'
            '__asm__ __volatile__("mov %1, %0 ; lock ; orl $0,(%%rsp)"'
            ':"=m"(*p):"r"(x):"memory");}'
            'int main(void){int v=0;a_store(&v,42);return v;}')
        self.assertEqual(rc, 42)

    def test_a_inc_dual_memory_operands(self):
        # =m output and m input naming the same location (two memory operands).
        rc, _ = _build(
            'static inline void a_inc(volatile int*p){'
            '__asm__ __volatile__("lock ; incl %0"'
            ':"=m"(*p):"m"(*p):"memory");}'
            'int main(void){int c=41;a_inc(&c);return c;}')
        self.assertEqual(rc, 42)

    def test_a_ctz_register_output(self):
        rc, _ = _build(
            'typedef unsigned long u64;'
            'static inline int a_ctz(u64 x){'
            '__asm__("bsf %1,%0":"=r"(x):"r"(x));return x;}'
            'int main(void){return a_ctz(0x100);}')  # 8
        self.assertEqual(rc, 8)


if __name__ == "__main__":
    unittest.main()
