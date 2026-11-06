#!/usr/bin/env python3
"""Check BTLEAdvFix patches against the installed bluetoothd.

Usage: python3 tools/check_patterns.py [/usr/sbin/bluetoothd]

Checks:
  1. every pattern matches exactly once (or is already patched in page cache)
  2. the inserted call lands on the routine, and the retargeted je lands on the call
  3. no branch/call anywhere in __text lands inside replaced bytes
     (other than at the start of a replaced instruction run), which is the bug
     that broke the first v0.2 build
Exit code 0 = all checks passed.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "BTLEAdvFix" / "kern_start.cpp"
BIN = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/sbin/bluetoothd")
SLICE = 16384          # x86_64 slice offset in the fat bluetoothd (25G72)
VMBASE = 0x100000000
PATCHES = ("singleAdv", "airdropCave", "airdropCallsite", "airdropBranch")


def arrays(text):
    out = {}
    for name, body in re.findall(r"static const uint8_t (\w+)\[\] \{(.*?)\};", text, re.S):
        out[name] = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", body))
    return out


def va(off):
    return off - SLICE + VMBASE


def main():
    a = arrays(re.sub(r"//[^\n]*", "", SRC.read_text()))
    data = BIN.read_bytes()
    patched = bytearray(data)
    ok, changed = True, []
    for base in PATCHES:
        orig, repl = a[base + "Original"], a[base + "Replacement"]
        count = data.count(orig)
        # A running BTLEAdvFix patches the shared page cache, so reads of the
        # file can already return the replacement bytes.
        if count == 0 and data.count(repl) == 1:
            print(f"{base:16s} len={len(orig):3d} already patched in page cache")
            i = data.find(repl)
        else:
            print(f"{base:16s} len={len(orig):3d} occurrences={count}")
            if count != 1 or len(orig) != len(repl):
                ok = False
                continue
            i = data.find(orig)
            patched[i:i + len(orig)] = repl
        changed += [va(i + k) for k in range(len(orig)) if orig[k] != repl[k]]

    if not ok:
        print("MISMATCH: do not use this kext with this bluetoothd")
        return 1

    def pos(base):
        i = data.find(a[base + "Original"])
        return i if i >= 0 else data.find(a[base + "Replacement"])

    cs = pos("airdropCallsite") + 5
    call_target = cs + 5 + int.from_bytes(patched[cs + 1:cs + 5], "little", signed=True)
    routine = pos("airdropCave") + 0x60
    br = pos("airdropBranch") + 11
    br_target = br + 6 + int.from_bytes(patched[br + 2:br + 6], "little", signed=True)
    print(f"call -> 0x{va(call_target):x} (routine 0x{va(routine):x})")
    print(f"je   -> 0x{va(br_target):x} (call 0x{va(cs):x})")
    ok = call_target == routine and patched[routine] == 0x51 and br_target == cs

    # Branches into replaced bytes: any j*/call target inside a changed byte
    # run, except the first byte of the run (an instruction start we kept
    # semantically equivalent).
    # Disassemble the patched copy so retargeted branches are seen as patched.
    with tempfile.NamedTemporaryFile(suffix=".bluetoothd") as tmp:
        tmp.write(patched)
        tmp.flush()
        asm = subprocess.run(["otool", "-arch", "x86_64", "-tV", tmp.name],
                             capture_output=True, text=True).stdout
    changed_set = set(changed)
    run_starts = {x for x in changed if x - 1 not in changed_set}
    bad = []
    for line in asm.splitlines():
        m = re.match(r"([0-9a-f]{16})\t(j\w+|callq)\t0x([0-9a-f]+)$", line)
        if m:
            src, tgt = int(m.group(1), 16), int(m.group(3), 16)
            if tgt in changed_set and tgt not in run_starts and src not in changed_set:
                bad.append((src, tgt))
    for s, t in bad:
        print(f"branch 0x{s:x} -> 0x{t:x} lands inside replaced bytes")
    ok = ok and not bad

    print("OK" if ok else "MISMATCH: do not use this kext with this bluetoothd")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
