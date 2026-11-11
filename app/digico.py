"""DiGiCo meter source: reads input, gate and compressor meters from a DiGiCo (SD / Quantum) over OSC.

The X32 stays the console that MixDeck shows and follows. This only replaces the X32's channel meters
(level, gate and compressor gain reduction) with the meters of the mapped DiGiCo channels. It never writes
a value to the DiGiCo: it sends read queries ("<address>/?") only.

Addresses come from the console's own iPad command list (IPAD_Q3.DOSC in the Quantum offline software).
The offline software answers these meter queries with no value, because it has no audio. The value units
on a real console are not yet known: levels are read as dBFS and gain reduction as dB. status() reports the
range of raw values seen, so that the scale can be checked on a real console.
"""
import socket
import struct
import threading
import time

from x32 import level_byte

CHANNELS = 32            # X32 input channels
POLL = 0.2               # all mapped channels, 5 times per second
POLL_SELECTED = 0.1      # the selected channel, 10 times per second
STALE = 1.0              # meters older than this show as no signal
GR_FLOOR = 0.0001


def _pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def query(addr):
    return _pad(f"{addr}/?".encode()) + b",\0\0\0"


def decode(data):
    """One OSC message -> (address, first numeric argument or None)."""
    end = data.index(b"\0")
    addr = data[:end].decode("utf-8", "replace")
    i = (end + 4) & ~3
    if i >= len(data) or data[i:i + 1] != b",":
        return addr, None
    tend = data.index(b"\0", i)
    tags = data[i + 1:tend].decode()
    j = (tend + 4) & ~3
    if tags[:1] == "f":
        return addr, struct.unpack_from(">f", data, j)[0]
    if tags[:1] == "i":
        return addr, float(struct.unpack_from(">i", data, j)[0])
    return addr, None


def db_byte(db):
    """dBFS to the X32 meter byte (-80..+20 dB)."""
    return 0 if db <= -80 else level_byte(10 ** (db / 20))


def gr_byte(gr_db):
    """dB of gain reduction to the X32 gain-factor byte (1.0 = no reduction)."""
    return level_byte(max(GR_FLOOR, 10 ** (-abs(gr_db) / 20)))


class DiGiCoMeters:
    KINDS = ("level", "gate", "comp")

    def __init__(self, cfg):
        self.cfg = cfg
        self.ip = cfg.get("ip", "")
        self.port = int(cfg.get("port") or 7001)
        self.start = max(1, int(cfg.get("start") or 1))   # DiGiCo channel for X32 channel 1
        self.stopped = False
        self.watch = lambda: (False, -1)  # set by the server: (someone watches X32 meters, X32 selected strip)
        self.values = {}         # (x32 index, kind) -> (value, time)
        self.raw = {}            # kind -> [min, max] of raw values seen
        self.last_rx = 0.0
        self.replies = 0
        self.empty = 0
        self.addr_map = {}
        for i in range(CHANNELS):
            c = f"/Input_Channels/{self.start + i}"
            self.addr_map[f"{c}/Channel_Input/post_meter/left"] = (i, "level")
            self.addr_map[f"{c}/Dynamics/gate_meter"] = (i, "gate")
            self.addr_map[f"{c}/Dynamics/GR_meter_1"] = (i, "comp")
        self.by_index = {}
        for a, (i, k) in self.addr_map.items():
            self.by_index.setdefault(i, []).append(a)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", int(cfg.get("listen", 7000))))  # the console sends to this port; 0 = any (tests)
        self.sock.settimeout(1.0)
        threading.Thread(target=self._rx, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()

    @property
    def connected(self):
        return time.time() - self.last_rx < 6

    def stop(self):
        self.stopped = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, addr):
        try:
            self.sock.sendto(query(addr), (self.ip, self.port))
        except OSError:
            pass

    def _poll(self):
        next_all = next_ping = 0.0
        while not self.stopped:
            now = time.time()
            if self.ip and now >= next_ping:
                self._send("/Console/Name")  # read only: shows that the console answers
                next_ping = now + 3
            wanted, selected = self.watch()
            if self.ip and wanted:
                if now >= next_all:
                    next_all = now + POLL
                    for a in self.addr_map:
                        self._send(a)
                elif isinstance(selected, int) and 0 <= selected < CHANNELS:
                    for a in self.by_index[selected]:
                        self._send(a)
            time.sleep(POLL_SELECTED)

    def _rx(self):
        while not self.stopped:
            try:
                data, src = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if src[0] != self.ip or data.startswith(b"#bundle"):
                continue
            try:
                addr, v = decode(data)
            except (ValueError, struct.error):
                continue
            self.last_rx = time.time()
            hit = self.addr_map.get(addr)
            if not hit:
                continue
            self.replies += 1
            if v is None:
                self.empty += 1
                continue
            i, kind = hit
            r = self.raw.setdefault(kind, [v, v])
            r[0], r[1] = min(r[0], v), max(r[1], v)
            self.values[(i, kind)] = (v, self.last_rx)

    def overlay(self, meter_bytes):
        """Put DiGiCo levels and gain reduction into the X32 meter bytes (1: 32 in, 32 gate, 32 comp)."""
        if not self.ip:
            return meter_bytes
        b = bytearray(meter_bytes)
        if len(b) < 96:
            b.extend(bytes(96 - len(b)))
        now = time.time()
        for i in range(CHANNELS):
            for k, off, conv in (("level", 0, db_byte), ("gate", 32, gr_byte), ("comp", 64, gr_byte)):
                v = self.values.get((i, k))
                b[off + i] = conv(v[0]) if v and now - v[1] < STALE else 0
        return bytes(b)

    def status(self):
        if not self.ip:
            info = "Off"
        elif not self.connected:
            info = "No reply from the DiGiCo"
        elif not self.values:
            info = "Connected. No meter data" + (" (the console sends empty meter values)" if self.empty else "")
        else:
            info = "Meters live"
        return {"connected": self.connected, "info": info, "start": self.start, "replies": self.replies,
                "empty": self.empty, "raw": {k: [round(a, 3), round(b, 3)] for k, (a, b) in self.raw.items()}}
