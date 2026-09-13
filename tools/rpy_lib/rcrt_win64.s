// rcrt_win64.s -- process entry and setjmp/longjmp for 64-bit Windows.
//
// The Linux runtime (rcrt.s) talks to the kernel with `syscall`. Windows has
// no stable system-call interface; its stable interface is the DLLs. So the
// C library here is msvcrt.dll, present on every Windows since XP, and this
// file only has to do what a C runtime's startup does before `main`: fetch
// argc/argv/envp, then hand main's result to exit() so atexit handlers run
// and stdio is flushed. rlink turns the calls to __getmainargs, exit and
// __set_app_type into imports from msvcrt.dll.
//
// Microsoft x64 calling convention: arguments in rcx, rdx, r8, r9, then the
// stack; the caller reserves 32 bytes of home space at [rsp] for the callee
// and keeps rsp 16-byte aligned at each call. The entry point is itself
// entered by a call, so rsp is 8 mod 16 on arrival.

.intel_syntax noprefix

.global mainCRTStartup
.global setjmp
.global longjmp

.text

// Frame (88 bytes, which realigns rsp to 16):
//   [rsp+0]  .. [rsp+31]   home space for the callees
//   [rsp+32]               5th argument of __getmainargs
//   [rsp+40]               int argc
//   [rsp+48]               char **argv
//   [rsp+56]               char **envp
//   [rsp+64]               _startupinfo { int newmode; }
mainCRTStartup:
    sub rsp, 88
    mov ecx, 1                          // _CONSOLE_APP: errors go to stderr,
    call __set_app_type                 // not to a message box
    mov DWORD PTR [rsp+64], 0
    lea rcx, QWORD PTR [rsp+40]
    lea rdx, QWORD PTR [rsp+48]
    lea r8, QWORD PTR [rsp+56]
    xor r9d, r9d                        // no wildcard expansion of argv
    lea rax, QWORD PTR [rsp+64]
    mov QWORD PTR [rsp+32], rax
    call __getmainargs
    mov ecx, DWORD PTR [rsp+40]
    mov rdx, QWORD PTR [rsp+48]
    mov r8, QWORD PTR [rsp+56]
    call main
    mov ecx, eax
    call exit                           // runs atexit, flushes, never returns
    jmp mainCRTStartup

// int setjmp(jmp_buf b)        -- b in rcx
// void longjmp(jmp_buf b, int v)  -- b in rcx, v in edx
//
// Crust's own pair, not msvcrt's: msvcrt's longjmp unwinds frame by frame
// using each function's SEH unwind data (.pdata/.xdata), which Crust does not
// emit, so it would terminate the process. The buffer holds everything Win64
// requires a callee to preserve: rbx rbp rdi rsi r12-r15, the stack pointer as
// it will be after `ret`, the return address, and xmm6-xmm15 (with movups:
// a jmp_buf is only 8-byte aligned). 240 bytes; <setjmp.h> reserves 256.
setjmp:
    mov QWORD PTR [rcx], rbx
    mov QWORD PTR [rcx+8], rbp
    mov QWORD PTR [rcx+16], rdi
    mov QWORD PTR [rcx+24], rsi
    mov QWORD PTR [rcx+32], r12
    mov QWORD PTR [rcx+40], r13
    mov QWORD PTR [rcx+48], r14
    mov QWORD PTR [rcx+56], r15
    lea rax, QWORD PTR [rsp+8]
    mov QWORD PTR [rcx+64], rax
    mov rax, QWORD PTR [rsp]
    mov QWORD PTR [rcx+72], rax
    movups XMMWORD PTR [rcx+80], xmm6
    movups XMMWORD PTR [rcx+96], xmm7
    movups XMMWORD PTR [rcx+112], xmm8
    movups XMMWORD PTR [rcx+128], xmm9
    movups XMMWORD PTR [rcx+144], xmm10
    movups XMMWORD PTR [rcx+160], xmm11
    movups XMMWORD PTR [rcx+176], xmm12
    movups XMMWORD PTR [rcx+192], xmm13
    movups XMMWORD PTR [rcx+208], xmm14
    movups XMMWORD PTR [rcx+224], xmm15
    xor eax, eax
    ret

longjmp:
    mov rbx, QWORD PTR [rcx]
    mov rbp, QWORD PTR [rcx+8]
    mov rdi, QWORD PTR [rcx+16]
    mov rsi, QWORD PTR [rcx+24]
    mov r12, QWORD PTR [rcx+32]
    mov r13, QWORD PTR [rcx+40]
    mov r14, QWORD PTR [rcx+48]
    mov r15, QWORD PTR [rcx+56]
    movups xmm6, XMMWORD PTR [rcx+80]
    movups xmm7, XMMWORD PTR [rcx+96]
    movups xmm8, XMMWORD PTR [rcx+112]
    movups xmm9, XMMWORD PTR [rcx+128]
    movups xmm10, XMMWORD PTR [rcx+144]
    movups xmm11, XMMWORD PTR [rcx+160]
    movups xmm12, XMMWORD PTR [rcx+176]
    movups xmm13, XMMWORD PTR [rcx+192]
    movups xmm14, XMMWORD PTR [rcx+208]
    movups xmm15, XMMWORD PTR [rcx+224]
    mov rsp, QWORD PTR [rcx+64]
    mov eax, edx
    cmp eax, 0                          // longjmp(b, 0) returns 1: zero is
    jne longjmp_go                      // reserved for setjmp's first return
    mov eax, 1
longjmp_go:
    jmp QWORD PTR [rcx+72]
