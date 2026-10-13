"""Generic OBD-II (SAE J1979) over K-line: ISO 9141-2 or ISO 14230 (KWP2000).

Both links expose request(data) -> list of response payloads (one per ECU).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import kwp2000
from .transport import KLineError, Transport

OBD_ADDR = 0x33  # functional OBD address used by both 5-baud and KWP


# -- links -----------------------------------------------------------------
class Iso9141Link:
    """ISO 9141-2: request 68 6A F1 <data> cs, response 48 6B <ecu> <data> cs.
    No length byte: messages are delimited by inter-byte timing (P1 <= 20 ms,
    P2 >= 25 ms between messages)."""

    name = "ISO 9141-2"

    def __init__(self, t: Transport):
        self.t = t
        self._last = 0.0

    def request(self, data: bytes) -> list[bytes]:
        wait = self._last + getattr(self.t, "p3", kwp2000.P3_MIN) - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        body = bytes([0x68, 0x6A, 0xF1]) + data
        self.t.write(body + bytes([kwp2000.checksum(body)]))
        out = []
        first = 0.5
        while msg := self.t.read_until_gap(gap=0.022, first_timeout=first):
            first = 0.1
            if len(msg) < 5 or kwp2000.checksum(msg[:-1]) != msg[-1]:
                raise KLineError(f"bad ISO 9141 response: {msg.hex()}")
            out.append(msg[3:-1])
        self._last = time.perf_counter()
        if not out:
            raise KLineError("no OBD response")
        return out


class KwpObdLink:
    name = "ISO 14230 (KWP2000)"

    def __init__(self, client: kwp2000.Kwp2000Client):
        self.client = client

    def request(self, data: bytes) -> list[bytes]:
        return [self.client.request(data)]


def connect(t: Transport, init: str = "auto"):
    """Wake the car's OBD ECUs. Returns (link, keybytes)."""
    if init in ("auto", "5baud"):
        try:
            kb = t.five_baud_init(OBD_ADDR)
        except KLineError:
            if init == "5baud":
                raise
        else:
            if kb[1] == kwp2000.KWP_KB2:
                c = kwp2000.Kwp2000Client(t, OBD_ADDR, functional=True)
                c.keybytes = kb
                return KwpObdLink(c), kb
            return Iso9141Link(t), kb  # keybytes 08 08 or 94 94
    c = kwp2000.Kwp2000Client(t, OBD_ADDR, functional=True)
    kb = c.connect_fast()
    return KwpObdLink(c), kb


# -- PIDs ------------------------------------------------------------------
@dataclass(frozen=True)
class Pid:
    pid: int
    name: str
    unit: str
    decode: Callable[[bytes], float]


def _a(d): return d[0]
def _ab(d): return (d[0] << 8) | d[1]


PIDS = {p.pid: p for p in [
    Pid(0x04, "engine_load", "%", lambda d: _a(d) * 100 / 255),
    Pid(0x05, "coolant_temp", "C", lambda d: _a(d) - 40),
    Pid(0x06, "stft_b1", "%", lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x07, "ltft_b1", "%", lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x08, "stft_b2", "%", lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x09, "ltft_b2", "%", lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x0B, "map", "kPa", _a),
    Pid(0x0C, "rpm", "rpm", lambda d: _ab(d) / 4),
    Pid(0x0D, "speed", "km/h", _a),
    Pid(0x0E, "timing_advance", "deg", lambda d: _a(d) / 2 - 64),
    Pid(0x0F, "intake_temp", "C", lambda d: _a(d) - 40),
    Pid(0x10, "maf", "g/s", lambda d: _ab(d) / 100),
    Pid(0x11, "throttle", "%", lambda d: _a(d) * 100 / 255),
    Pid(0x1F, "run_time", "s", _ab),
]}


def supported_pids(link) -> list[int]:
    """Walk the 0x00/0x20/0x40... bitmaps."""
    out, base = [], 0x00
    while True:
        try:
            resp = link.request(bytes([0x01, base]))[0]
        except KLineError:
            break
        bits = int.from_bytes(resp[2:6], "big")
        out += [base + i + 1 for i in range(32) if bits & (1 << (31 - i))]
        if not bits & 1 or base >= 0xC0:
            break
        base += 0x20
    return [p for p in out if p % 0x20]


def read_pid(link, pid: int) -> tuple[float, bytes]:
    resp = link.request(bytes([0x01, pid]))[0]
    if resp[0] != 0x41 or resp[1] != pid:
        raise KLineError(f"unexpected PID response: {resp.hex()}")
    raw = resp[2:]
    p = PIDS.get(pid)
    return (p.decode(raw) if p else float("nan")), raw


# -- DTCs ------------------------------------------------------------------
def dtc_str(code: int) -> str:
    return "PCBU"[code >> 14] + f"{(code >> 12) & 3}{code & 0xFFF:03X}"


def read_stored_dtcs(link) -> list[str]:
    """Mode 03. Payload 43 then 2-byte DTCs, zero-padded."""
    codes = []
    for resp in link.request(b"\x03"):
        body = resp[1:]
        if len(body) % 2:  # some stacks prepend a count byte
            body = body[1:]
        codes += [dtc_str((body[i] << 8) | body[i + 1])
                  for i in range(0, len(body) - 1, 2)
                  if body[i] or body[i + 1]]
    return codes
