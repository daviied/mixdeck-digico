"""Record the OSC traffic between a DiGiCo console and the DiGiCo Pad (iPad) app.

The tool sits between the two and passes every message through, so the iPad app works as usual.
It records both directions to a file, to find what the console sends for the selected channel,
the screen and the meters. It sends nothing of its own to the console, except one name query
every 5 seconds in listen-only mode (no --ipad).

Needs only Python 3.8+. Run it on a computer on the console network (the Pi or a laptop):

    python3 digico_capture.py --console 192.168.1.50 --ipad 192.168.1.60

Console: Setup > External Control. Set the iPad's entry (type DiGiCo Pad) to THIS computer's IP.
Console send port 7000, receive port 7001 (or pass --from-console and --to-console).
iPad app: console IP = THIS computer's IP. Send port 7003, receive port 7002.

Type a note and press Enter at any time (for example: select ch 5). Notes go in the file, so
each action can be found later. Press Ctrl+C to stop.
"""
import argparse
import json
import queue
import select
import socket
import struct
import sys
import threading
import time


def _pad(n):
    return (n + 3) & ~3


def _string(data, i):
    end = data.index(b"\0", i)
    return data[i:end].decode("utf-8", "replace"), _pad(end + 1)


def decode(data):
    """OSC packet -> list of (address, args). Raises ValueError if it is not OSC."""
    if data.startswith(b"#bundle\0"):
        out, i = [], 16
        while i + 4 <= len(data):
            (n,) = struct.unpack_from(">i", data, i)
            out += decode(data[i + 4:i + 4 + n])
            i += 4 + n
        return out
    if not data.startswith(b"/"):
        raise ValueError("not OSC")
    addr, i = _string(data, 0)
    if i >= len(data) or data[i:i + 1] != b",":
        return [(addr, [])]
    tags, i = _string(data, i)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack_from(">i", data, i)[0]); i += 4
        elif t == "f":
            args.append(round(struct.unpack_from(">f", data, i)[0], 6)); i += 4
        elif t in "sS":
            s, i = _string(data, i); args.append(s)
        elif t == "b":
            (n,) = struct.unpack_from(">i", data, i)
            args.append({"blob": data[i + 4:i + 4 + n].hex()}); i += _pad(4 + n)
        elif t in "hdt":
            fmt = ">d" if t == "d" else ">q"
            args.append(struct.unpack_from(fmt, data, i)[0]); i += 8
        elif t in "cmr":
            args.append({"raw32": data[i:i + 4].hex()}); i += 4
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t in "NI":
            args.append(None)
        else:
            raise ValueError(f"unknown type tag {t}")
    return [(addr, args)]


def encode(addr):
    a = addr.encode() + b"\0"
    a += b"\0" * (_pad(len(a)) - len(a))
    return a + b",\0\0\0"


def main():
    p = argparse.ArgumentParser(description="Record DiGiCo console <-> iPad OSC traffic.")
    p.add_argument("--console", required=True, help="console IP address")
    p.add_argument("--ipad", help="iPad IP address. Leave out to only listen to the console.")
    p.add_argument("--from-console", type=int, default=7000, help="port the console sends to (default 7000)")
    p.add_argument("--to-console", type=int, default=7001, help="port the console receives on (default 7001)")
    p.add_argument("--from-ipad", type=int, default=7003, help="port the iPad sends to (default 7003)")
    p.add_argument("--to-ipad", type=int, default=7002, help="port the iPad receives on (default 7002)")
    p.add_argument("--out", help="output file (default digico-capture-<time>.jsonl)")
    p.add_argument("--quiet-after", type=float, default=2.0,
                   help="print an address again only after this many seconds (default 2). The file has all.")
    a = p.parse_args()

    out_path = a.out or time.strftime("digico-capture-%Y%m%d-%H%M%S.jsonl")
    out = open(out_path, "a", encoding="utf-8")
    t0 = time.monotonic()

    sc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sc.bind(("0.0.0.0", a.from_console))
    socks = [sc]
    si = None
    if a.ipad:
        si = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        si.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        si.bind(("0.0.0.0", a.from_ipad))
        socks.append(si)

    notes = queue.Queue()

    def read_notes():
        for line in sys.stdin:
            if line.strip():
                notes.put(line.strip())

    threading.Thread(target=read_notes, daemon=True).start()

    def log(direction, data, src):
        rec = {"t": round(time.monotonic() - t0, 4), "dir": direction}
        try:
            msgs = decode(data)
        except (ValueError, struct.error, IndexError):
            rec.update(src=f"{src[0]}:{src[1]}", raw=data.hex())
            out.write(json.dumps(rec) + "\n")
            print(f"{rec['t']:9.3f} {direction:14} (not OSC, {len(data)} bytes)", flush=True)
            return
        for addr, args in msgs:
            out.write(json.dumps({**rec, "addr": addr, "args": args}) + "\n")
            last = printed.get((direction, addr), -1e9)
            if rec["t"] - last >= a.quiet_after:
                printed[(direction, addr)] = rec["t"]
                shown = json.dumps(args)
                print(f"{rec['t']:9.3f} {direction:14} {addr} {shown[:120]}", flush=True)
        counts[direction] = counts.get(direction, 0) + 1

    printed, counts = {}, {}
    console = (a.console, a.to_console)
    ipad = (a.ipad, a.to_ipad) if a.ipad else None
    print(f"Recording to {out_path}", flush=True)
    print("Relay on: console -> this:%d -> iPad:%s   iPad -> this:%s -> console:%d" % (
        a.from_console, a.to_ipad if ipad else "-", a.from_ipad if ipad else "-", a.to_console), flush=True)
    print("Type a note and press Enter to mark an action. Ctrl+C stops.\n", flush=True)
    next_ping = 0.0
    try:
        while True:
            while not notes.empty():
                n = notes.get()
                t = round(time.monotonic() - t0, 4)
                out.write(json.dumps({"t": t, "dir": "note", "note": n}) + "\n")
                out.flush()
                print(f"{t:9.3f} ---- NOTE: {n}", flush=True)
            if not ipad and time.monotonic() >= next_ping:
                sc.sendto(encode("/Console/Name/?"), console)  # read only: keeps the console talking to us
                next_ping = time.monotonic() + 5
            ready, _, _ = select.select(socks, [], [], 0.2)
            for s in ready:
                data, src = s.recvfrom(65535)
                if s is sc:
                    if src[0] != a.console:
                        continue
                    log("console>ipad", data, src)
                    if ipad:
                        si.sendto(data, ipad)
                else:
                    if src[0] != a.ipad:
                        continue
                    log("ipad>console", data, src)
                    sc.sendto(data, console)
            out.flush()
    except KeyboardInterrupt:
        pass
    finally:
        out.close()
        print(f"\nStopped. Messages: {counts}. File: {out_path}", flush=True)


if __name__ == "__main__":
    main()
