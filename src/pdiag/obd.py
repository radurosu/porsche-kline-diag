"""Generic OBD-II (SAE J1979) over K-line: ISO 9141-2 or ISO 14230 (KWP2000).

Both links expose request(data, settle) -> list of response payloads (one per
ECU); `settle` is how long to keep listening for more ECUs after an answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import kwp2000
from .transport import KLineError, NoResponse, Transport

OBD_ADDR = 0x33  # functional OBD address used by both 5-baud and KWP
ISO9141_HDR = b"\x48\x6B"  # every ISO 9141-2 OBD response starts with this
ISO9141_GAP = 0.022  # silence that ends a chunk (ECU messages are >= 25 ms apart)
# Fault codes are a one-shot read where a missed ECU looks like a clean car, so
# wait for slow ECUs longer than live data can afford.
DTC_SETTLE = 1.0
# When only one ECU answers PID 00 there is nobody else to wait for; P3 (55 ms
# after its last byte) still spaces the requests.
SINGLE_ECU_SETTLE = 0.03


class BadReply(KLineError):
    """The ECU answered, but not with what was asked (wrong PID, short data)."""


# -- links -----------------------------------------------------------------
class Iso9141Link:
    """ISO 9141-2: request 68 6A F1 <data> cs, response 48 6B <ecu> <data> cs.
    No length byte: a message ends where its checksum closes and the next
    header (or silence) follows."""

    name = "ISO 9141-2"

    def __init__(self, t: Transport):
        self.t = t
        self.settle = kwp2000.MULTI_GAP  # how long to wait for more ECUs
        self._last = time.perf_counter()  # created right after the handshake: wait P3

    def request(self, data: bytes, settle: float | None = None) -> list[bytes]:
        settle = self.settle if settle is None else settle
        wait = self._last + getattr(self.t, "p3", kwp2000.P3_MIN) - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        body = bytes([0x68, 0x6A, 0xF1]) + data
        self.t.drain()  # a late reply to an earlier request must not pass as this one's
        self.t.write(body + bytes([kwp2000.checksum(body)]))
        out, pend = [], b""
        first = 0.5
        # USB latency can split one message over two chunks, or run two ECUs'
        # messages into one chunk; split_9141 handles both.
        while chunk := self.t.read_until_gap(gap=ISO9141_GAP, first_timeout=first):
            # P3 runs from the ECU's last byte, which came one gap before now
            self._last = time.perf_counter() - ISO9141_GAP
            msgs, pend = split_9141(pend + chunk)
            out += msgs
            first = settle if out else 0.5
        if out:
            return out
        if pend:
            raise BadReply(f"bad ISO 9141 response: {pend.hex()}")
        raise NoResponse("no OBD response")


def split_9141(buf: bytes) -> tuple[list[bytes], bytes]:
    """Cut received bytes into ISO 9141-2 response payloads.

    Returns (payloads, rest): `rest` is a message still arriving, or bytes that
    never became one. Garbled bytes before a later valid message are dropped.
    """
    out, i = [], 0
    while i < len(buf):
        end = _msg_end(buf, i)
        if end is not None:
            out.append(buf[i + 3:end - 1])
            i = end
            continue
        j = buf.find(ISO9141_HDR, i + 1)
        while j >= 0 and _msg_end(buf, j) is None:
            j = buf.find(ISO9141_HDR, j + 1)
        if j < 0:
            break
        i = j
    return out, buf[i:]


def _msg_end(buf: bytes, i: int) -> int | None:
    """End of the shortest valid message at buf[i:] that is followed by the next
    header (or the start of one, or nothing), else None."""
    if buf[i:i + 2] != ISO9141_HDR:
        return None
    for end in range(i + 5, len(buf) + 1):
        if (kwp2000.checksum(buf[i:end - 1]) == buf[end - 1]
                and ISO9141_HDR.startswith(buf[end:end + 2])):
            return end
    return None


class KwpObdLink:
    name = "ISO 14230 (KWP2000)"

    def __init__(self, client: kwp2000.Kwp2000Client):
        self.client = client
        self.settle = kwp2000.MULTI_GAP  # how long to wait for more ECUs

    def request(self, data: bytes, settle: float | None = None) -> list[bytes]:
        return self.client.request_all(data, self.settle if settle is None else settle)


def connect(t: Transport, init: str = "auto"):
    """Wake the car's OBD ECUs. Returns (link, keybytes).

    "auto" falls back to fast init only if nothing answered the 5-baud init. A
    half-finished handshake is raised as is: fast init would hide the real
    problem, and ISO 9141-2 cars can't do fast init at all.
    """
    if init in ("auto", "5baud"):
        try:
            kb = t.five_baud_init(OBD_ADDR)
        except NoResponse:
            if init == "5baud":
                raise
        else:
            if kb[1] == kwp2000.KWP_KB2:
                c = kwp2000.Kwp2000Client(t, OBD_ADDR, functional=True)
                c.keybytes = kb
                c._last_rx = time.perf_counter()  # wait P3 before the first request
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
    size: int  # data bytes the decoder needs
    decode: Callable[[bytes], float | str]  # a number, or text for status PIDs


def _a(d): return d[0]
def _ab(d): return (d[0] << 8) | d[1]


def _monitor_status(d: bytes) -> str:
    """PID 01: fault lamp, stored codes, readiness. Monitors that aren't complete
    after a drive cycle can mean the codes were cleared recently."""
    supported = [d[1] >> i & 1 for i in range(3)] + [d[2] >> i & 1 for i in range(8)]
    incomplete = [d[1] >> (i + 4) & 1 for i in range(3)] + [d[3] >> i & 1 for i in range(8)]
    not_ready = sum(s & x for s, x in zip(supported, incomplete))
    return (f"MIL {'on' if d[0] & 0x80 else 'off'}, {d[0] & 0x7F} stored, "
            f"{not_ready} of {sum(supported)} monitors not ready")


_FUEL = {1: "open loop (warming up)", 2: "closed loop", 4: "open loop (load or decel)",
         8: "open loop (fault)", 16: "closed loop (sensor fault)"}
_AIR = {1: "upstream of catalyst", 2: "downstream of catalyst", 4: "off", 8: "on (test)"}
_OBD_STD = {1: "OBD-II (CARB)", 2: "OBD (EPA)", 3: "OBD and OBD-II", 4: "OBD-I",
            5: "not OBD compliant", 6: "EOBD", 7: "EOBD and OBD-II", 8: "EOBD and OBD",
            9: "EOBD, OBD and OBD-II", 10: "JOBD", 11: "JOBD and OBD-II",
            12: "JOBD and EOBD", 13: "JOBD, EOBD and OBD-II"}


def _fuel_system(d: bytes) -> str:
    return " / ".join(_FUEL.get(b, f"0x{b:02x}") for b in d[:2] if b) or "not reported"


def _o2_sensors(d: bytes) -> str:
    banks = [f"bank {b + 1}: " + " ".join(f"S{i + 1}" for i in range(4) if d[0] >> (4 * b + i) & 1)
             for b in (0, 1) if d[0] >> (4 * b) & 0xF]
    return ", ".join(banks) or "none"


PIDS = {p.pid: p for p in [
    Pid(0x01, "monitor_status", "", 4, _monitor_status),
    Pid(0x03, "fuel_system", "", 2, _fuel_system),
    Pid(0x04, "engine_load", "%", 1, lambda d: _a(d) * 100 / 255),
    Pid(0x05, "coolant_temp", "C", 1, lambda d: _a(d) - 40),
    Pid(0x06, "stft_b1", "%", 1, lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x07, "ltft_b1", "%", 1, lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x08, "stft_b2", "%", 1, lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x09, "ltft_b2", "%", 1, lambda d: (_a(d) - 128) * 100 / 128),
    Pid(0x0B, "map", "kPa", 1, _a),
    Pid(0x0C, "rpm", "rpm", 2, lambda d: _ab(d) / 4),
    Pid(0x0D, "speed", "km/h", 1, _a),
    Pid(0x0E, "timing_advance", "deg", 1, lambda d: _a(d) / 2 - 64),
    Pid(0x0F, "intake_temp", "C", 1, lambda d: _a(d) - 40),
    Pid(0x10, "maf", "g/s", 2, lambda d: _ab(d) / 100),
    Pid(0x11, "throttle", "%", 1, lambda d: _a(d) * 100 / 255),
    Pid(0x12, "secondary_air", "", 1, lambda d: _AIR.get(d[0], f"0x{d[0]:02x}")),
    Pid(0x13, "o2_sensors", "", 1, _o2_sensors),
    Pid(0x14, "o2_b1s1", "V", 2, lambda d: _a(d) / 200),
    Pid(0x15, "o2_b1s2", "V", 2, lambda d: _a(d) / 200),
    Pid(0x18, "o2_b2s1", "V", 2, lambda d: _a(d) / 200),
    Pid(0x19, "o2_b2s2", "V", 2, lambda d: _a(d) / 200),
    Pid(0x1C, "obd_standard", "", 1, lambda d: _OBD_STD.get(d[0], f"0x{d[0]:02x}")),
    Pid(0x1F, "run_time", "s", 2, _ab),
]}


def _no_answer(resps: list[bytes], sid: int, what: str) -> KLineError:
    """Error for replies that don't answer the request. An ISO 9141-2 rejection
    (7F <sid> <code>) becomes a NegativeResponse, as it is on KWP."""
    for r in resps:
        if len(r) >= 3 and r[0] == 0x7F and r[1] == sid:
            return kwp2000.NegativeResponse(sid, r[2])
    return BadReply(f"{what}: {' '.join(r.hex() for r in resps)}")


def supported_pids(link) -> list[int]:
    """Walk the 0x00/0x20/0x40... bitmaps, merged across every ECU that answers.

    Raises KLineError if not even the first bitmap can be read: the list is then
    unknown, which callers must not mistake for empty.
    """
    out, base = set(), 0x00
    while True:
        try:
            resps = link.request(bytes([0x01, base]))
        except KLineError:
            if base == 0x00:
                raise
            break
        bitmaps = [r for r in resps if len(r) >= 6 and r[:2] == bytes([0x41, base])]
        if not bitmaps:
            if base == 0x00:
                raise _no_answer(resps, 0x01, "no PID 00 bitmap")
            break
        if base == 0x00:
            link.ecus = len(bitmaps)  # every OBD ECU must answer PID 00
        more = False
        for resp in bitmaps:
            bits = int.from_bytes(resp[2:6], "big")
            out |= {base + i + 1 for i in range(32) if bits & (1 << (31 - i))}
            more |= bool(bits & 1)
        if not more or base >= 0xC0:
            break
        base += 0x20
    return sorted(p for p in out if p % 0x20)


def read_pid(link, pid: int) -> tuple[float | str, bytes]:
    """(value, raw data): a number, text for status PIDs, NaN if not decodable yet."""
    resps = link.request(bytes([0x01, pid]))
    answers = [r for r in resps if r[:2] == bytes([0x41, pid])]
    if not answers:
        raise _no_answer(resps, 0x01, f"no reply for PID {pid:02X}")
    p = PIDS.get(pid)
    if p is None:
        return float("nan"), answers[0][2:]
    resp = next((r for r in answers if len(r) - 2 >= p.size), None)
    if resp is None:
        raise BadReply(f"PID {pid:02X} reply too short: {answers[0].hex()}")
    return p.decode(resp[2:]), resp[2:]


# -- DTCs ------------------------------------------------------------------
def dtc_str(code: int) -> str:
    return "PCBU"[code >> 14] + f"{(code >> 12) & 3}{code & 0xFFF:03X}"


def read_stored_dtcs(link) -> list[str]:
    """Mode 03. Payload 43 then 2-byte DTCs, zero-padded."""
    codes = []
    resps = link.request(b"\x03", settle=DTC_SETTLE)
    answers = [r for r in resps if r[:1] == b"\x43"]
    if not answers:
        raise _no_answer(resps, 0x03, "no mode 03 answer")
    for resp in answers:
        body = resp[1:]
        if len(body) % 2:  # some stacks prepend a count byte
            body = body[1:]
        codes += [dtc_str((body[i] << 8) | body[i + 1])
                  for i in range(0, len(body) - 1, 2)
                  if body[i] or body[i + 1]]
    return codes
