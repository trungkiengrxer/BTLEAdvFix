#!/usr/bin/env python3
"""Decode BLE advertising activity from a bluetoothd PacketLogger trace (.pklg).

Usage:
  python3 tools/pklg_adv.py <trace.pklg> [--utc-offset 7] [--from HH:MM:SS] [--to HH:MM:SS]

Prints, in local time:
  CHIP  every LE_SetAdvData payload sent to the controller, decoded into AD
        structures and Apple continuity types (05 = AirDrop, 09 = AirPlay,
        10 = NearbyInfo, ...)
  CONN  LE_CreateConn commands and their status
  FAIL  any HCI command that completed with a non-zero status
"""
import argparse
import datetime
import struct


def records(path):
    d = open(path, "rb").read()
    big = struct.unpack(">I", d[:4])[0] < 0x10000
    i = 0
    while i + 13 <= len(d):
        ln = struct.unpack(">I" if big else "<I", d[i:i + 4])[0]
        if ln < 9 or i + 4 + ln > len(d):
            break
        ts = struct.unpack(">Q" if big else "<Q", d[i + 4:i + 12])[0]
        yield (ts >> 32) + (ts & 0xFFFFFFFF) / 1e6, d[i + 12], d[i + 13:i + 4 + ln]
        i += 4 + ln


def apple_types(v):
    out, j = [], 2
    while j + 1 < len(v):
        t, ln = v[j], v[j + 1]
        body = v[j + 2:j + 2 + ln]
        out.append("%02x[%d%s]" % (t, ln, "" if len(body) == ln else " TRUNC"))
        j += 2 + ln
    return "Apple{" + " ".join(out) + "}"


def decode_adv(p):
    ln, data, i, parts = p[0], p[1:1 + p[0]], 0, []
    while i < len(data) and data[i]:
        l, t = data[i], data[i + 1]
        v = data[i + 2:i + 1 + l]
        parts.append(apple_types(v) if t == 0xFF and v[:2] == b"\x4c\x00" else "AD%02x" % t)
        i += 1 + l
    return "len=%d %s" % (ln, " ".join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--utc-offset", type=float, default=7)
    ap.add_argument("--from", dest="start", default="00:00:00")
    ap.add_argument("--to", dest="end", default="99:99:99")
    a = ap.parse_args()
    tz = datetime.timezone(datetime.timedelta(hours=a.utc_offset))
    pending, last = {}, None
    for ts, typ, d in records(a.trace):
        t = datetime.datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S.%f")[:-3]
        if not (a.start <= t <= a.end):
            continue
        if typ == 0 and len(d) >= 3:
            op, p = d[0] | d[1] << 8, d[3:3 + d[2]]
            pending[op] = t
            if op == 0x2008:
                s = decode_adv(p)
                if s != last:
                    print(t, "CHIP", s)
                    last = s
            elif op == 0x200D:
                print(t, "CONN LE_CreateConn filter=%d peerType=%d peer=%s" % (p[4], p[5], p[6:12][::-1].hex(":")))
        elif typ == 1 and len(d) >= 6 and d[0] in (0x0E, 0x0F):
            op, st = (d[3] | d[4] << 8, d[5]) if d[0] == 0x0E else (d[4] | d[5] << 8, d[2])
            if st and op != 0xFCE9:
                print(pending.get(op, t), "FAIL opcode=0x%04X status=0x%02X" % (op, st))


if __name__ == "__main__":
    main()
