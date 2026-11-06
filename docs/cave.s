# Code cave for bluetoothd 25G72 (x86_64), placed at 0x100aa23b0.
# Called from 0x1006a4351 (replaces two 7-byte loads of the session vector
# begin/end). Moves the session whose raw advertising data is an Apple
# AirDrop AD ("?? FF 4C 00 05 ...") to the front of the vector, so the
# first-fit copy loops place AirDrop into the single advertising instance.
#
# vector: [rbp-0x6dc0] = begin, [rbp-0x6db8] = end, element stride 0x10,
#         session pointer at element+8
# session raw data: buf = [session+0x38]; size = [buf+0x10]; bytes = buf+0x18
    .text
    .globl _cave
_cave:
    pushq   %rcx
    pushq   %rdx
    pushq   %rsi
    pushq   %rdi
    pushq   %r8
    movq    -0x6dc0(%rbp), %rcx
    movq    -0x6db8(%rbp), %rdx
    movq    %rcx, %rsi
1:
    cmpq    %rdx, %rsi
    jae     4f
    movq    8(%rsi), %rdi
    testq   %rdi, %rdi
    je      2f
    movq    0x38(%rdi), %r8
    testq   %r8, %r8
    je      2f
    cmpq    $5, 0x10(%r8)
    jb      2f
    cmpl    $0x05004cff, 0x19(%r8)
    je      3f
2:
    addq    $0x10, %rsi
    jmp     1b
3:
    cmpq    %rcx, %rsi
    je      4f
    movq    (%rcx), %rdi
    xchgq   %rdi, (%rsi)
    movq    %rdi, (%rcx)
    movq    8(%rcx), %rdi
    xchgq   %rdi, 8(%rsi)
    movq    %rdi, 8(%rcx)
4:
    popq    %r8
    popq    %rdi
    popq    %rsi
    popq    %rdx
    popq    %rcx
    movq    -0x6db8(%rbp), %rax
    movq    -0x6dc0(%rbp), %r15
    retq
