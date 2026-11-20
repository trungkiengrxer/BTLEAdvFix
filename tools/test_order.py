#!/usr/bin/env python3
"""Run the AIRDROP_FIRST variant of docs/cave.s on fake session vectors.

Usage: python3 tools/test_order.py      (x86_64 macOS; exit code 0 = pass)

The routine is assembled with test values for the build-specific symbols,
copied to executable memory and called through a small trampoline that
points rbp at a fake stack frame holding the vector, as in bluetoothd.
"""
import ctypes
import mmap
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portlib import assemble  # noqa: E402

CAVE_SRC = Path(__file__).resolve().parent.parent / "docs" / "cave.s"
INTERVAL = 0xDE
AIRDROP = bytes.fromhex("17FF4C00051200000000000000000003DAF180F78E20BD1E00")
CONNECTABLE = bytes.fromhex("15FF4C0009081302C0A800031B5810064D1DA5C5B718")

code, _ = assemble(CAVE_SRC, {"VEC_BEGIN": -0x10, "VEC_END": -0x8, "AIRDROP_FIRST": 1,
                              "INTERVAL": INTERVAL}, "_cave")
# push rbp; push r15; mov rbp, rdi; call routine; pop r15; pop rbp; ret
# (the routine reloads r15, which is callee-saved)
trampoline = b"\x55\x41\x57\x48\x89\xFD\xE8" + struct.pack("<i", 0x10 - 11) + b"\x41\x5F\x5D\xC3"
memory = mmap.mmap(-1, 0x1000, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
memory.write(trampoline.ljust(0x10, b"\xCC") + code)
run = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(ctypes.addressof(ctypes.c_char.from_buffer(memory)))
keep = []


def session(data, interval):
    buf = ctypes.create_string_buffer(0x18 + len(data))
    struct.pack_into("<Q", buf, 0x10, len(data))
    buf[0x18:0x18 + len(data)] = data
    obj = ctypes.create_string_buffer(0x100)
    struct.pack_into("<Q", obj, 0x38, ctypes.addressof(buf))
    struct.pack_into("<H", obj, INTERVAL, interval)
    keep.extend((buf, obj))
    return ctypes.addressof(obj)


def order(*sessions):
    """Names of the sessions in vector order after the routine ran."""
    vector = ctypes.create_string_buffer(0x10 * len(sessions))
    names = {}
    for i, (name, data, interval) in enumerate(sessions):
        pointer = session(data, interval)
        names[pointer] = name
        struct.pack_into("<QQ", vector, 0x10 * i, i, pointer)
    base = ctypes.addressof(vector)
    frame = ctypes.create_string_buffer(0x10)
    struct.pack_into("<QQ", frame, 0, base, base + 0x10 * len(sessions))
    run(ctypes.addressof(frame) + 0x10)
    return [names[struct.unpack_from("<Q", vector, 0x10 * i + 8)[0]] for i in range(len(sessions))]


CA, NC = ("CA", CONNECTABLE), ("NC", AIRDROP)
cases = [
    ("idle: AirDrop first", [(*CA, 0x1B0), (*NC, 0x122)], ["NC", "CA"]),
    ("Apple Pay boost: connectable stays first", [(*CA, 0x30), (*NC, 0x122)], ["CA", "NC"]),
    ("boosted session moves to the front", [(*NC, 0x122), (*CA, 0x30)], ["CA", "NC"]),
    ("both boosted: AirDrop first", [(*CA, 0x30), (*NC, 0x30)], ["NC", "CA"]),
    ("interval 0 is not a boost", [(*CA, 0), (*NC, 0x122)], ["NC", "CA"]),
    ("no AirDrop: order kept", [(*CA, 0x122), ("X", CONNECTABLE, 0x30)], ["CA", "X"]),
    ("single AirDrop session", [(*NC, 0x122)], ["NC"]),
]
failed = 0
for name, sessions, expected in cases:
    got = order(*sessions)
    ok = got == expected
    failed += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}: {got}" + ("" if ok else f", expected {expected}"))
order()  # empty vector must not crash
sys.exit(1 if failed else 0)
