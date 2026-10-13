"""Simulated 996 for development without the car.

All values here are FAKE placeholders, not decoded Porsche data. Real local
IDs / ECU ID formats must come from captures (see docs/PROTOCOL.md).
"""

from __future__ import annotations

import math
import random
import time

from . import kwp2000
from .transport import KLineError, QueueTransport

MOCK_DME_ADDR = 0x10  # placeholder; real 996 DME address unverified

# Over-rev counters as the UI expects them once decoded. Range boundaries are
# PLACEHOLDERS (not verified ME7.8 values); counts and hours are invented.
MOCK_OVER_REV = {
    "engine_hours": 3412.6,
    "ranges": [
        {"range": 1, "rpm_from": 7300, "rpm_to": 7600, "ignitions": 1284, "last_hour": 3409.1},
        {"range": 2, "rpm_from": 7600, "rpm_to": 7900, "ignitions": 412, "last_hour": 3402.7},
        {"range": 3, "rpm_from": 7900, "rpm_to": 8200, "ignitions": 3, "last_hour": 2113.5},
        {"range": 4, "rpm_from": 8200, "rpm_to": 8500, "ignitions": 0, "last_hour": None},
        {"range": 5, "rpm_from": 8500, "rpm_to": 8800, "ignitions": 0, "last_hour": None},
        {"range": 6, "rpm_from": 8800, "rpm_to": None, "ignitions": 0, "last_hour": None},
    ],
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
                 dynamic: bool = False):
        super().__init__()
        self.generic = generic
        self.dme_addr = dme_addr
        self.addr: int | None = None
        self.dynamic = dynamic  # False = fixed idle values (tests)
        self.t0 = time.monotonic()

    # -- wakeup ------------------------------------------------------------
    def five_baud_init(self, address: int) -> tuple[int, int]:
        if address == 0x33:
            self.addr = 0x33
            return (0x08, 0x08) if self.generic == "iso9141" else (0xE9, 0x8F)
        if address == self.dme_addr:
            self.addr = address
            return (0xEF, 0x8F)
        raise KLineError(f"no 0x55 sync after 5-baud 0x{address:02X} (got nothing)")

    def fast_init(self) -> None:
        self.addr = None  # target taken from StartCommunication header

    # -- requests ----------------------------------------------------------
    def write(self, data: bytes) -> None:
        if data[0] == 0x68:  # ISO 9141-2 request
            resp = self._obd(data[3:-1])
            if resp:
                body = bytes([0x48, 0x6B, self.dme_addr]) + resp
                self.push(body + bytes([kwp2000.checksum(body)]))
            return
        f = kwp2000.decode(data)
        if f.data[0] == 0x81:
            self.addr = f.target
        for resp in self._kwp(f.target, f.data):
            self.push(kwp2000.encode(resp, kwp2000.TESTER, f.target))

    def _obd(self, d: bytes) -> bytes | None:
        if d[0] == 0x01:
            pid = d[1]
            vals = self._pid_bytes()
            return bytes([0x41, pid]) + vals[pid] if pid in vals else None
        if d[0] == 0x03:
            return b"\x43\x03\x00\x04\x20\x00\x00"  # P0300, P0420
        return None

    def _pid_bytes(self) -> dict[int, bytes]:
        if not self.dynamic:
            return {
                0x00: b"\x1e\x1f\x80\x00",  # 04-07, 0C-11
                0x04: b"\x40", 0x05: b"\x80", 0x0C: (850 * 4).to_bytes(2, "big"),
                0x0D: b"\x00", 0x0E: b"\x8c", 0x0F: b"\x3c", 0x10: b"\x02\x58",
                0x11: b"\x22", 0x06: b"\x80", 0x07: b"\x82",
            }
        e = _engine(time.monotonic() - self.t0)
        pct = lambda v: bytes([round(max(0, min(100, v)) * 255 / 100)])
        trim = lambda v: bytes([round(v * 128 / 100 + 128)])
        return {
            0x00: b"\x1e\x1f\x80\x00",
            0x04: pct(e["load"]), 0x05: bytes([round(e["coolant"]) + 40]),
            0x06: trim(e["stft"]), 0x07: trim(e["ltft"]),
            0x0C: round(e["rpm"] * 4).to_bytes(2, "big"), 0x0D: bytes([round(e["speed"])]),
            0x0E: bytes([round((e["timing"] + 64) * 2)]), 0x0F: b"\x3c",
            0x10: round(e["maf"] * 100).to_bytes(2, "big"), 0x11: pct(e["throttle"]),
        }

    def _kwp(self, target: int, d: bytes) -> list[bytes]:
        sid = d[0]
        if target == 0x33:
            r = self._obd(d)
            return [r] if r else [bytes([0x7F, sid, 0x11])]
        if sid == 0x81:
            return [b"\xC1\xEF\x8F"]
        if sid == 0x10:
            return [bytes([0x50, d[1]])]
        if sid == 0x3E:
            return [b"\x7E"]
        if sid == 0x82:
            return [b"\xC2"]
        if sid == 0x1A:
            ids = {0x9B: b"MOCK-ME7.8 0261 FAKE", 0x90: b"WP0ZZZ99Z2SMOCK00"}
            if d[1] in ids:
                return [bytes([0x5A, d[1]]) + ids[d[1]]]
            return [bytes([0x7F, sid, 0x31])]
        if sid == 0x18:
            return [b"\x58\x02\x03\x00\xE0\x11\x28\x20"]
        if sid == 0x21:
            lid = d[1]
            if lid == 0xEE:  # exercises responsePending handling
                return [b"\x7F\x21\x78", b"\x61\xEE\x01\x02"]
            if lid in (0x01, 0x02, 0x10):
                return [bytes([0x61, lid]) + bytes(range(lid, lid + 6))]
            return [bytes([0x7F, sid, 0x31])]
        return [bytes([0x7F, sid, 0x11])]
