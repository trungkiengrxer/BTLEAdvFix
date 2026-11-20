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
#
# The phase is chosen only when bluetoothd restarts advertising, which can be
# minutes apart, so an AirDrop send can run entirely in phase 0. An iPhone on
# iOS 15 then never sees the AirDrop AD and never wakes up.
#
# AIRDROP_FIRST (set per macOS build by tools/port.py): when 1, rules 1 and 2
# are replaced by:
#
#   - the AirDrop AD goes first, as in 0.2.1, because on macOS 26.6 sharingd
#     adds no NearbyAction while sending and the AirDrop AD is the only thing
#     that wakes an older iPhone;
#   - except while another session is boosted (interval 1..BOOST, 30 ms) and
#     the AirDrop session is not: then that session goes first. sharingd
#     boosts the connectable session for about 5 s when Apple Pay looks for
#     the iPhone (NearbyInfo, ApplePayLegacy), for Handoff and when its data
#     changes; each boost start and end restarts advertising, so the order is
#     chosen again at both edges.
#
#   INTERVAL    offset of the session's requested advertising interval
#               (uint16, 0.625 ms units; 0xde on 25G72), found by port.py
    .set    BOOST, 0x30
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
.if AIRDROP_FIRST
11: # find the AirDrop session; without one the order is kept
    cmpq    %rdx, %rsi
    jae     9f
    movq    8(%rsi), %rdi
    testq   %rdi, %rdi
    je      12f
    movq    0x38(%rdi), %r8
    testq   %r8, %r8
    je      12f
    cmpq    $5, 0x10(%r8)
    jb      12f
    cmpl    $0x05004cff, 0x19(%r8)
    je      13f
12:
    addq    $0x10, %rsi
    jmp     11b
13: # AirDrop at rsi. If it is boosted itself it goes first
    movzwl  INTERVAL(%rdi), %eax
    decl    %eax
    cmpl    $BOOST - 1, %eax
    jbe     8f
    movq    %rcx, %r9
14: # otherwise the first boosted session, if any, goes first
    cmpq    %rdx, %r9
    jae     8f
    movq    8(%r9), %rdi
    testq   %rdi, %rdi
    je      15f
    movzwl  INTERVAL(%rdi), %eax
    decl    %eax
    cmpl    $BOOST - 1, %eax
    ja      15f
    movq    %r9, %rsi
    jmp     8f
15:
    addq    $0x10, %r9
    jmp     14b
.else
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
.endif
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
