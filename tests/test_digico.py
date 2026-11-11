"""Run: python tests/test_digico.py  (fake DiGiCo and the X32 simulator, no console needed)"""
import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
import sim  # noqa: E402
from digico import DiGiCoMeters, db_byte, gr_byte  # noqa: E402
from x32 import X32  # noqa: E402


def wait(fn, t=10.0):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def osc(addr, *floats):
    pad = lambda b: b + b"\0" * (4 - len(b) % 4)
    return pad(addr.encode()) + pad(("," + "f" * len(floats)).encode()) + b"".join(struct.pack(">f", f) for f in floats)


class FakeDiGiCo:
    """Answers "<addr>/?" with a value from `values`, or with no value (like the offline software)."""

    def __init__(self, values):
        self.values, self.got = values, []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            data, src = self.sock.recvfrom(65535)
            addr = data[:data.index(b"\0")].decode()
            self.got.append(addr)
            if addr.endswith("/?"):
                a = addr[:-2]
                v = self.values.get(a)
                self.sock.sendto(osc(a, v) if v is not None else osc(a), src)


def main():
    assert db_byte(-150) == 0 and db_byte(0) == 204 and gr_byte(0) == 204 and gr_byte(20) == 153

    d = FakeDiGiCo({
        "/Console/Name": None,
        "/Input_Channels/33/Channel_Input/post_meter/left": -20.0,
        "/Input_Channels/33/Dynamics/gate_meter": 12.0,
        "/Input_Channels/34/Dynamics/GR_meter_1": 6.0,
    })
    xs = sim.X32Sim()
    x = X32("A", "127.0.0.1", port=xs.port)
    assert wait(lambda: x.connected), "x32 connect"
    m = DiGiCoMeters({"ip": "127.0.0.1", "port": d.port, "listen": 0, "start": 33})
    m.watch = lambda: (True, 1)
    x.meter_overlay = m.overlay
    x.meter_clients = 1
    assert wait(lambda: (0, "level") in m.values and (1, "comp") in m.values), "meter replies"
    x.send("/meters", "/meters/1")
    assert wait(lambda: len(x.meter_bytes) >= 96 and x.meter_bytes[0] == db_byte(-20)), "level overlay"
    b = x.meter_bytes
    assert b[32] == gr_byte(12) and b[64 + 1] == gr_byte(6), "gate / comp overlay"
    assert b[2] == 0, "unmapped data shows as no signal, not as X32 level"
    assert m.status()["info"] == "Meters live" and m.status()["raw"]["level"] == [-20.0, -20.0]
    # read only: every message to the DiGiCo is a query
    assert d.got and all(a.endswith("/?") for a in d.got), "only queries"
    # the selected channel (X32 ch 2 = index 1) is read more often than the rest
    assert wait(lambda: sum(a.startswith("/Input_Channels/40/") for a in d.got) >= 12), "polls continue"
    n_sel = sum(a.startswith("/Input_Channels/34/") for a in d.got)
    n_other = sum(a.startswith("/Input_Channels/40/") for a in d.got)
    assert n_sel > n_other, (n_sel, n_other)
    # nobody watching: no meter queries
    m.watch = lambda: (False, -1)
    time.sleep(0.3)
    before = len([a for a in d.got if "meter" in a])
    time.sleep(0.5)
    assert len([a for a in d.got if "meter" in a]) == before, "polls while nobody watches"
    m.stop()

    # offline software: meter replies with no value
    e = FakeDiGiCo({"/Console/Name": None})
    m2 = DiGiCoMeters({"ip": "127.0.0.1", "port": e.port, "listen": 0, "start": 1})
    m2.watch = lambda: (True, 0)
    e.values.update({a: None for a in m2.addr_map})
    assert wait(lambda: m2.empty > 0), "empty replies"
    assert "No meter data" in m2.status()["info"], m2.status()
    m2.stop()
    print("digico ok")


if __name__ == "__main__":
    main()
