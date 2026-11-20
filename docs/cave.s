# Advertising routine for bluetoothd (x86_64), build independent.
#
# It replaces the two loads of the session vector in the advertisement
# builder (see tools/port.py, site "callsite") and redoes them before it
# returns. The two stack slots are the only build-specific values; port.py
# reads them from the replaced instructions and passes them in:
#
#   VEC_BEGIN   rbp displacement of the vector begin  (-0x6dc0 on 25G72/25G332)
#   VEC_END     rbp displacement of the vector end    (-0x6db8 on 25G72/25G332)
#
# vector: element stride 0x10, session pointer at element+8
# session raw data: buf = [session+0x38]; size = [buf+0x10]; bytes = buf+0x18
#
# A BCM20702 has one 31-byte advertising instance and bluetoothd fills it
# first-fit in vector order, so the first session wins. Order chosen here:
#
#   1. A session whose Apple AD ("len FF 4C 00 ...") carries a NearbyAction
#      TLV (type 0x0F, length >= 5) goes first. sharingd adds it while the
#      Mac is looking for an AirDrop receiver (NeedsCLink); iOS 27.2 only
#      answers the Mac when it sees it.
#   2. Otherwise alternate, about every 2 s on a 3 GHz TSC (bit 33):
#        - AirDrop AD ("len FF 4C 00 05") first, or
#        - the original order, where the connectable session with
#          NearbyInfo (0x10) comes first. iOS 27.2 needs NearbyInfo to
#          recognise the Mac over Rapport before it offers it as an AirDrop
#          target, and AirDrop to wake older iPhones.
#      If AirDrop already is first in the "original order" phase it is
#      swapped with the second session, so both ADs get airtime.
    .text
    .globl _cave
_cave:
    pushq   %rcx
    pushq   %rdx
    pushq   %rsi
    pushq   %rdi
    pushq   %r8
    pushq   %r9
    movq    VEC_BEGIN(%rbp), %rcx
    movq    VEC_END(%rbp), %rdx
    movq    %rcx, %rsi
1:  # pass 1: look for a NearbyAction TLV
    cmpq    %rdx, %rsi
    jae     5f
    movq    8(%rsi), %rdi
    testq   %rdi, %rdi
    je      2f
    movq    0x38(%rdi), %r8
    testq   %r8, %r8
    je      2f
    cmpq    $6, 0x10(%r8)
    jb      2f
    cmpw    $0x4cff, 0x19(%r8)
    jne     2f
    cmpb    $0, 0x1b(%r8)
    jne     2f
    movq    $4, %r9
3:  # walk the Continuity TLVs
    leaq    2(%r9), %rax
    cmpq    0x10(%r8), %rax
    ja      2f
    cmpb    $0x0f, 0x18(%r8,%r9)
    jne     4f
    cmpb    $5, 0x19(%r8,%r9)
    jae     8f
4:
    movzbq  0x19(%r8,%r9), %rax
    leaq    2(%r9,%rax), %r9
    jmp     3b
2:
    addq    $0x10, %rsi
    jmp     1b
5:  # no NearbyAction: alternate on the TSC
    rdtsc
    testl   $2, %edx
    jne     10f
    # phase 0: AirDrop must not be first; if it is, swap it with the second
    leaq    0x10(%rcx), %rsi
    cmpq    %rdx, %rsi
    jae     9f
    movq    8(%rcx), %rdi
    testq   %rdi, %rdi
    je      9f
    movq    0x38(%rdi), %r8
    testq   %r8, %r8
    je      9f
    cmpq    $5, 0x10(%r8)
    jb      9f
    cmpl    $0x05004cff, 0x19(%r8)
    jne     9f
    jmp     8f
10: # phase 1: AirDrop first
    movq    %rcx, %rsi
6:
    cmpq    %rdx, %rsi
    jae     9f
    movq    8(%rsi), %rdi
    testq   %rdi, %rdi
    je      7f
    movq    0x38(%rdi), %r8
    testq   %r8, %r8
    je      7f
    cmpq    $5, 0x10(%r8)
    jb      7f
    cmpl    $0x05004cff, 0x19(%r8)
    je      8f
7:
    addq    $0x10, %rsi
    jmp     6b
8:  # swap element rsi with the first element (rcx)
    cmpq    %rcx, %rsi
    je      9f
    movq    (%rcx), %rdi
    xchgq   %rdi, (%rsi)
    movq    %rdi, (%rcx)
    movq    8(%rcx), %rdi
    xchgq   %rdi, 8(%rsi)
    movq    %rdi, 8(%rcx)
9:
    popq    %r9
    popq    %r8
    popq    %rdi
    popq    %rsi
    popq    %rdx
    popq    %rcx
    # the two instructions replaced at the call site
    movq    VEC_END(%rbp), %rax
    movq    VEC_BEGIN(%rbp), %r15
    retq
