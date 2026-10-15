"""Simulated car (996 or 987 etc.) for development without the car.

Its shape mirrors what a real 2002 996 reported on 2026-10-15: generic OBD over
ISO 9141-2 (keybytes 08 08), the PID 00 bitmap, the DME at 0x11 (KWP2000, EF 8F)
and its data block sizes. All values are FAKE placeholders, not decoded data.
"""

from __future__ import annotations

import math
import random
import time

from . import kwp2000
from .models import DEFAULT, Model
from .transport import NoResponse, QueueTransport

MOCK_DME_ADDR = 0x11  # as on a 2002 996 (ME7.8)
MOCK_TCU_ADDR = 0x18  # placeholder for a second ECU on the bus (Tiptronic)

# Data block sizes a real 2002 996 DME returned (KWP 0x21), 2026-10-15.
MOCK_LIDS = {0x03: 1, 0x04: 35, 0x05: 35, 0x06: 103, 0x07: 63, 0x08: 70, 0x09: 149,
             0x0A: 35, 0x0B: 34, 0x0C: 67, 0x14: 4, 0xEA: 3}

# Fake VINs: right WMI / type / year positions so model detection works,
# "MOCK" in the serial so they can't be mistaken for real cars.
MOCK_VINS = {"986": "WP0ZZZ98Z1SMOCK00", "996": "WP0ZZZ99Z2SMOCK00",
             "987": "WP0ZZZ98Z6UMOCK00", "997": "WP0ZZZ99Z7SMOCK00"}

# Counts and hours are invented. Boundaries come from the model profile, which
# are PLACEHOLDERS too.
_MOCK_COUNTS = [(1284, 3409.1), (412, 3402.7), (3, 2113.5), (0, None), (0, None), (0, None)]


def mock_over_rev(model: Model = DEFAULT) -> dict:
    """Over-rev counters as the UI expects them once decoded."""
    b = model.over_rev_from
    return {
        "engine_hours": 3412.6,
        "ranges": [{"range": i + 1, "rpm_from": b[i],
                    "rpm_to": b[i + 1] if i + 1 < len(b) else None,
                    "ignitions": n, "last_hour": h}
                   for i, (n, h) in enumerate(_MOCK_COUNTS)],
    }

# Text for the mock's DTCs only. P1128 wording is approximate.
MOCK_DTC_TEXT = {
    0x0300: "Random or multiple cylinder misfire detected",
    0x0420: "Catalyst efficiency below threshold, bank 1",
    0x1128: "Mixture too lean at idle, bank 1",
}


def _engine(t: float) -> dict[str, float]:
    """A 24 s drive loop: idle, pull to the limiter, lift, cruise, idle."""
    c = t % 24
    if c < 6:
        rpm, thr = 850 + 15 * math.sin(c * 3), 0
    elif c < 10:
        rpm, thr = 850 + (c - 6) / 4 * 5900, 92
    elif c < 11:
        rpm, thr = 7150 + 120 * abs(math.sin(c * 20)), 100  # limiter bounce
    elif c < 14:
        rpm, thr = 7200 - (c - 11) / 3 * 4200, 4
    elif c < 18:
        rpm, thr = 3000 + 40 * math.sin(c), 18
    else:
        rpm, thr = 3000 - min(1, (c - 18) / 3) * 2150, 0
    rpm += random.uniform(-8, 8)
    load = min(100, 18 + thr * 0.8)
    return {
        "rpm": rpm, "throttle": thr, "load": load,
        "coolant": min(88, 62 + t * 0.4),
        "timing": 6 if rpm < 1000 else (22 if thr > 80 else 32),
        "maf": 4 + rpm / 7000 * (8 + thr * 1.9),
        "speed": 0 if c < 6 else min(140, (rpm - 850) / 45),
        "stft": random.uniform(-3, 3), "ltft": 1.6,
    }


class MockCar(QueueTransport):
    def __init__(self, generic: str = "iso9141", dme_addr: int = MOCK_DME_ADDR,
                 dynamic: bool = False, model: str = "996", tcu: bool = False,
                 silent: set[bytes] = frozenset()):
        super().__init__()
        self.generic = generic
        self.dme_addr = dme_addr
        self.addr: int | None = None
        self.dynamic = dynamic  # False = fixed idle values (tests)
        self.vin = MOCK_VINS[model].encode()
        self.tcu = tcu  # a second ECU also answers generic OBD (answers first)
        self.silent = silent  # request prefixes (e.g. b"\x01\x10") nobody answers
        self.t0 = time.monotonic()

    # -- wakeup ------------------------------------------------------------
    def five_baud_init(self, address: int) -> tuple[int, int]:
        if address == 0x33:
            self.addr = 0x33
            return (0x08, 0x08) if self.generic == "iso9141" else (0xE9, 0x8F)
        if address == self.dme_addr:
            self.addr = address
            return (0xEF, 0x8F)
        raise NoResponse(f"no 0x55 sync after 5-baud 0x{address:02X} (got nothing)")

    def fast_init(self) -> None:
        self.addr = None  # target taken from StartCommunication header

    # -- requests ----------------------------------------------------------
    def write(self, data: bytes) -> None:
        if data[0] == 0x68:  # ISO 9141-2 request
            for src, resp in self._obd_all(data[3:-1]):
                if resp[0] == 0x7F:
                    continue  # ISO 9141-2 ECUs stay silent instead
                body = bytes([0x48, 0x6B, src]) + resp
                self.push(body + bytes([kwp2000.checksum(body)]))
            return
        f = kwp2000.decode(data)
        if f.data[0] == 0x81:
            self.addr = f.target
        if f.target == 0x33 and f.data[0] == 0x81:  # fast init, generic OBD
            replies = [(self.dme_addr, b"\xC1\xE9\x8F")] if self.generic == "kwp" else []
        elif f.target == 0x33:
            replies = self._obd_all(f.data)
        elif f.target == self.dme_addr:
            replies = [(f.target, r) for r in self._kwp(f.data)]
        else:
            replies = []  # no ECU at that address
        for src, resp in replies:
            self.push(kwp2000.encode(resp, kwp2000.TESTER, src))

    def _obd_all(self, d: bytes) -> list[tuple[int, bytes]]:
        if d[:2] in self.silent:
            return []
        out = []
        if self.tcu:
            out.append((MOCK_TCU_ADDR, self._tcu_obd(d)))
        r = self._obd(d)
        out.append((self.dme_addr, r or bytes([0x7F, d[0], 0x12])))
        return out

    def _tcu_obd(self, d: bytes) -> bytes:
        if d == b"\x01\x00":
            return b"\x41\x00\x00\x08\x00\x00"  # PID 0D only
        if d == b"\x01\x0d":
            return b"\x41\x0d\x00"
        if d[0] == 0x03:
            return b"\x43" + bytes(6)
        return bytes([0x7F, d[0], 0x12])

    def _obd(self, d: bytes) -> bytes | None:
        if d[0] == 0x01:
            pid = d[1]
            vals = self._pid_bytes()
            return bytes([0x41, pid]) + vals[pid] if pid in vals else None
        if d[0] == 0x03:
            return b"\x43\x03\x00\x04\x20\x00\x00"  # P0300, P0420
        return None

    # The PID 00 bitmap a real 2002 996 (ME7.8) sends: 01 03-09 0C-15 18 19 1C.
    BITMAP = b"\xbf\x9f\xf9\x90"
    # Status PIDs (values are placeholders): MIL off, 0 codes, evap not ready;
    # closed loop; secondary air off; O2 sensors S1+S2 on both banks; OBD-II.
    STATUS = {0x01: b"\x00\x07\x65\x04", 0x03: b"\x02\x00", 0x12: b"\x04",
              0x13: b"\x33", 0x1C: b"\x01"}

    def _pid_bytes(self) -> dict[int, bytes]:
        if not self.dynamic:
            return {
                0x00: self.BITMAP, **self.STATUS,
                0x04: b"\x40", 0x05: b"\x80", 0x0C: (850 * 4).to_bytes(2, "big"),
                0x0D: b"\x00", 0x0E: b"\x8c", 0x0F: b"\x3c", 0x10: b"\x02\x58",
                0x11: b"\x22", 0x06: b"\x80", 0x07: b"\x82", 0x08: b"\x80", 0x09: b"\x81",
                0x14: b"\x5a\x80", 0x15: b"\x8c\xff", 0x18: b"\x50\x80", 0x19: b"\x8a\xff",
            }
        t = time.monotonic() - self.t0
        e = _engine(t)
        pct = lambda v: bytes([round(max(0, min(100, v)) * 255 / 100)])
        trim = lambda v: bytes([round(v * 128 / 100 + 128)])
        o2 = lambda phase: bytes([round(90 + 80 * math.sin(t * 6 + phase)), 0x80])  # front: switching
        return {
            0x00: self.BITMAP, **self.STATUS,
            0x04: pct(e["load"]), 0x05: bytes([round(e["coolant"]) + 40]),
            0x06: trim(e["stft"]), 0x07: trim(e["ltft"]),
            0x08: trim(e["stft"] * 0.8), 0x09: trim(e["ltft"] * 0.5),
            0x0C: round(e["rpm"] * 4).to_bytes(2, "big"), 0x0D: bytes([round(e["speed"])]),
            0x0E: bytes([round((e["timing"] + 64) * 2)]), 0x0F: b"\x3c",
            0x10: round(e["maf"] * 100).to_bytes(2, "big"), 0x11: pct(e["throttle"]),
            0x14: o2(0), 0x15: b"\x8c\xff", 0x18: o2(1.3), 0x19: b"\x8a\xff",
        }

    def _kwp(self, d: bytes) -> list[bytes]:
        if d[:2] in self.silent:
            return []
        sid = d[0]
        if sid == 0x81:
            return [b"\xC1\xEF\x8F"]
        if sid == 0x10:
            return [bytes([0x50, d[1]])]
        if sid == 0x3E:
            return [b"\x7E"]
        if sid == 0x82:
            return [b"\xC2"]
        if sid == 0x1A:
            ids = {0x9B: b"MOCK-ME7.8 0261 FAKE", 0x90: self.vin}
            if d[1] in ids:
                return [bytes([0x5A, d[1]]) + ids[d[1]]]
            return [bytes([0x7F, sid, 0x31])]
        if sid == 0x18:
            if d[1] != 0x00:  # like the 996 ME7.8: only status 00 is accepted
                return [bytes([0x7F, sid, 0x12])]
            return [b"\x58\x02\x03\x00\xE0\x11\x28\x20"]
        if sid == 0x21:
            lid = d[1]
            if lid == 0xEE:  # exercises responsePending handling
                return [b"\x7F\x21\x78", b"\x61\xEE\x01\x02"]
            if lid in (0x01, 0x02, 0x10):
                return [bytes([0x61, lid]) + bytes(range(lid, lid + 6))]
            if lid in MOCK_LIDS:  # same block sizes as a real 996 DME; contents fake
                body = bytearray((lid * 7 + i * 13) & 0xFF for i in range(MOCK_LIDS[lid]))
                if self.dynamic and lid in (0x04, 0x05):  # two bytes that move
                    e = _engine(time.monotonic() - self.t0)
                    body[0], body[1] = round(e["rpm"] / 40) & 0xFF, round(e["throttle"]) & 0xFF
                return [bytes([0x61, lid]) + bytes(body)]
            return [bytes([0x7F, sid, 0x31])]
        return [bytes([0x7F, sid, 0x11])]
