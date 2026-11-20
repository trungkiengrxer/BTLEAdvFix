#!/usr/bin/env python3
"""Port BTLEAdvFix to the bluetoothd of one macOS build.

Usage: python3 tools/port.py <bluetoothd> <macOS build>      e.g. 25G332
       python3 tools/port.py --check <bluetoothd> <macOS build>

<bluetoothd> must be a clean copy. While the kext is active, reads of
/usr/sbin/bluetoothd return patched pages: boot with -bladvoff first, or
take the copy from another volume.

The patch sites are found by signature (?? = byte that differs between
builds), never by address. Every signature must match exactly once and all
checks below must pass, otherwise nothing is written:

  - the routine area after __eh_frame is free and on one page
  - the retargeted branch really pointed into the replaced instructions
  - no other branch or call lands inside replaced bytes

The result is BTLEAdvFix/builds/<build>.inc, which the kext compiles in.
With --check nothing is written; the existing file is compared instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portlib import Binary, Patch, PortError, assemble, branch_scan, rel32, write_build  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILDS = ROOT / "BTLEAdvFix" / "builds"
CAVE_SRC = ROOT / "docs" / "cave.s"

# Shared layout of the zero padding after __eh_frame (both kexts rely on it):
#   __eh_frame + 0x060 .. + 0x180   BTLEAdvFix routine
#   __eh_frame + 0x180 .. + 0x400   BTLERPAConnect routines
CAVE_OFFSET = 0x60
CAVE_LIMIT = 0x180

# LeBroadcaster constructor:
#   cmpb $0x0, 0xa0(%rax)    ; SupportsMultipleAdvertising
#   jne  ...                 ; -> nop nop ("Using a single advertising instance")
#   leaq ...(%rip), %rax
SINGLE_ADV = "80 B8 A0 00 00 00 00 75 ?? 48 8D 05"

# Advertisement builder, right before the first-fit copy loops:
#   movw %ax, (%r15,%r13,2)
#   movq VEC_END(%rbp), %rax      ; -> call routine
#   movq VEC_BEGIN(%rbp), %r15    ; -> nops
#   cmpq %r15, %rax
#   je   ...
CALLSITE = "66 43 89 04 6F 48 8B 85 ?? ?? ?? ?? 4C 8B BD ?? ?? ?? ?? 4C 39 F8 0F 84 ?? ?? ?? ??"

# The one branch that jumps to the second load above (rax is already loaded
# on that path). It is retargeted to the call, which redoes both loads.
#   movq 0x8(%rdx), %rcx; jmp +2; shrl %ecx; testq %rcx, %rcx
#   je   <second load>
#   leaq ...(%rbp), %rsi
BRANCH = "48 8B 4A 08 EB 02 D1 E9 48 85 C9 0F 84 ?? ?? ?? ?? 48 8D B5 ?? ?? ?? ??"


# Builds whose sharingd adds no NearbyAction while sending AirDrop. There the
# alternation of docs/cave.s can hold the wrong order for a whole send, so
# the AirDrop AD goes first, except while another session is boosted (see
# AIRDROP_FIRST in docs/cave.s).
AIRDROP_FIRST_BUILDS = {"25G72"}

# Advertisement builder, minimum requested interval over the sessions:
#   movq 0x30(%r12), %rcx; movzwl INTERVAL(%rcx), %r15d
#   cmpw %r15w, %ax; cmovbl %eax, %r15d
SESSION_INTERVAL = "49 8B 4C 24 ?? 44 0F B7 B9 ?? ?? ?? ?? 66 44 39 F8 44 0F 42 F8"


def port(binary, os_build):
    notes = []

    site = binary.find_one("single advertising instance", SINGLE_ADV)
    original = binary.bytes_at(site, 12)
    single = Patch("single advertising instance", site, original, original[:7] + b"\x90\x90" + original[9:])

    eh_addr, eh_size = binary.sections[("__TEXT", "__eh_frame")]
    eh = binary.off(eh_addr)
    text_end = binary.segments["__TEXT"][3]
    if eh_size + 8 > CAVE_OFFSET:
        raise PortError("__eh_frame is larger than expected, the routine area overlaps it")
    if eh + CAVE_LIMIT > text_end:
        raise PortError("not enough padding after __eh_frame")
    cave = eh + CAVE_OFFSET

    call = binary.find_one("call site", CALLSITE)
    vec_end, vec_begin = binary.i32(call + 8), binary.i32(call + 15)
    if vec_end != vec_begin + 8:
        raise PortError("call site: the two loads are not the begin/end of one vector")
    symbols = {"VEC_BEGIN": vec_begin, "VEC_END": vec_end, "AIRDROP_FIRST": 0}
    if os_build in AIRDROP_FIRST_BUILDS:
        hit = binary.find_one("session interval", SESSION_INTERVAL)
        symbols.update(AIRDROP_FIRST=1, INTERVAL=binary.i32(hit + 9))
        notes.append(f"AirDrop first, yields to boosted sessions; session interval at +{symbols['INTERVAL']:#x}")
    code, labels = assemble(CAVE_SRC, symbols, "_cave")
    if len(code) > CAVE_LIMIT - CAVE_OFFSET:
        raise PortError(f"routine is {len(code)} bytes, limit is {CAVE_LIMIT - CAVE_OFFSET}")
    if any(binary.bytes_at(cave, CAVE_LIMIT - CAVE_OFFSET)):
        raise PortError("routine area after __eh_frame is not free")
    routine = Patch("advertising order routine", cave, bytes(len(code)), code)
    notes.append(f"routine at 0x{binary.va(cave):x} ({len(code)} bytes), "
                 f"VEC_BEGIN={vec_begin:#x} VEC_END={vec_end:#x}")

    original = binary.bytes_at(call, 28)
    callsite = Patch("advertising order call", call, original,
                     original[:5] + b"\xE8" + rel32(call + 10, cave) + b"\x90" * 9 + original[19:])

    hits = [h for h in binary.find(BRANCH) if h + 17 + binary.i32(h + 13) == call + 12]
    if len(hits) != 1:
        raise PortError(f"branch: {len(hits)} candidates jump to the second load, expected 1")
    je = hits[0]
    original = binary.bytes_at(je, 24)
    branch = Patch("advertising order branch", je, original,
                   original[:13] + rel32(je + 17, call + 5) + original[17:])

    patches = [single, routine, callsite, branch]
    bad = branch_scan(binary, patches, [])
    for src, tgt in bad:
        print(f"branch 0x{src:x} -> 0x{tgt:x} lands inside replaced bytes")
    if bad:
        raise PortError("other code jumps into the replaced instructions")
    return patches, notes


def main():
    args = sys.argv[1:]
    check = "--check" in args
    args = [a for a in args if a != "--check"]
    if len(args) != 2:
        sys.exit(__doc__)
    path, os_build = args
    try:
        binary = Binary(path)
        patches, notes = port(binary, os_build)
        for p in patches:
            print(f"{p.name:30s} 0x{binary.va(p.offset):x}  {len(p.original):3d} bytes")
        target = BUILDS / f"{os_build}.inc"
        before = target.read_text() if target.exists() else None
        if check and before is None:
            raise PortError(f"{target.name} does not exist")
        names = write_build(BUILDS, os_build, binary, patches, notes)
        if check:
            same = target.read_text() == before
            target.write_text(before)
            print("OK: builds/%s.inc is up to date" % os_build if same else "MISMATCH with builds/%s.inc" % os_build)
            sys.exit(0 if same else 1)
        print(f"wrote builds/{os_build}.inc; builds compiled in: {', '.join(names)}")
    except PortError as e:
        sys.exit(f"NOT PORTED: {e}")


if __name__ == "__main__":
    main()
