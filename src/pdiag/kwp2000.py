"""KWP2000 (ISO 14230) framing and client — read-only services only.

Frame: [fmt][tgt][src]([len]) data... [cs]
  fmt bits 7-6: 00 no address, 10 physical, 11 functional, 01 CARB
  fmt bits 5-0: data length 1..63, or 0 -> separate length byte after addresses
  cs = sum of all preceding bytes mod 256
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .transport import KLineError, Transport

TESTER = 0xF1
P2_TIMEOUT = 1.0  # spec P2max is 50 ms; generous until real ECU timing is known
P2_PENDING = 5.0  # after NRC 0x78 (response pending)
P3_MIN = 0.055  # min gap between ECU response and next request
P1_INTERBYTE = 0.05
REQUEST_DEADLINE = 30.0  # total per request, incl. responsePending extensions
MULTI_GAP = 0.1  # after a reply, wait this long for other ECUs (functional requests)

NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported-invalidFormat",
    0x21: "busy-repeatRequest",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "responsePending",
    0x80: "serviceNotSupportedInActiveDiagnosticSession",
}

# KWP2000 keybyte KB2 is 0x8F; KB1 encodes timing/header options.
KWP_KB2 = 0x8F


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def encode(data: bytes, target: int, source: int = TESTER,
           functional: bool = False) -> bytes:
    n = len(data)
    if not 1 <= n <= 255:
        raise ValueError(f"KWP data length {n} out of range")
    fmt = 0xC0 if functional else 0x80
    hdr = bytes([fmt | n, target, source]) if n <= 63 else bytes([fmt, target, source, n])
    body = hdr + data
    return body + bytes([checksum(body)])


@dataclass
class Frame:
    target: int | None
    source: int | None
    data: bytes


def frame_size(prefix: bytes) -> int | None:
    """Total frame length given its first bytes, or None if more bytes needed."""
    if not prefix:
        return None
    fmt = prefix[0]
    addr = 2 if fmt & 0xC0 else 0
    n = fmt & 0x3F
    if n:
        return 1 + addr + n + 1
    if len(prefix) < 2 + addr:
        return None
    return 1 + addr + 1 + prefix[1 + addr] + 1


def decode(raw: bytes) -> Frame:
    size = frame_size(raw)
    if size is None or len(raw) < size:
        raise KLineError(f"truncated KWP frame: {raw.hex()}")
    raw = raw[:size]
    if checksum(raw[:-1]) != raw[-1]:
        raise KLineError(f"bad checksum: {raw.hex()}")
    fmt = raw[0]
    if fmt & 0xC0:
        tgt, src, i = raw[1], raw[2], 3
    else:
        tgt = src = None
        i = 1
    if not fmt & 0x3F:
        i += 1
    return Frame(tgt, src, raw[i:-1])


def split_stream(raw: bytes) -> tuple[list[Frame], bytes, bytes]:
    """Best-effort split of captured bytes into KWP frames (for sniffing).

    Returns (frames, skipped, rest). Bytes that don't start a valid frame (init
    sync/keybytes, noise) are skipped one at a time; `rest` is a trailing partial
    frame to prepend to the next chunk. A skipped byte can occasionally line up
    into a false frame, so the raw capture stays the ground truth.
    """
    def valid_at(j: int) -> bool:
        size = frame_size(raw[j:j + 5])
        if size is None or j + size > len(raw):
            return False
        try:
            decode(raw[j:j + size])
            return True
        except KLineError:
            return False

    out, skipped, i = [], bytearray(), 0
    while i < len(raw):
        if valid_at(i):
            size = frame_size(raw[i:i + 5])
            out.append(decode(raw[i:i + size]))
            i += size
            continue
        # Either junk or a frame still arriving. If a valid frame starts later
        # in the buffer, this byte was junk; otherwise keep the tail for later.
        if not any(valid_at(j) for j in range(i + 1, len(raw))):
            break
        skipped.append(raw[i])
        i += 1
    return out, bytes(skipped), raw[i:]


def split_frames(raw: bytes) -> list[Frame]:
    return split_stream(raw)[0]


class NoResponse(KLineError):
    pass


class NegativeResponse(KLineError):
    def __init__(self, service: int, code: int):
        self.service, self.code = service, code
        super().__init__(f"service 0x{service:02X} rejected: 0x{code:02X} "
                         f"{NRC.get(code, 'unknown')}")


def read_frame(t: Transport, timeout: float) -> Frame:
    head = t.read(1, timeout)
    if not head:
        raise NoResponse("timeout waiting for ECU response")
    while (size := frame_size(head)) is None:
        b = t.read(1, P1_INTERBYTE)
        if not b:
            raise KLineError(f"truncated header: {head.hex()}")
        head += b
    rest = t.read(size - len(head), P1_INTERBYTE * size)
    return decode(head + rest)


class Kwp2000Client:
    def __init__(self, t: Transport, target: int, source: int = TESTER,
                 functional: bool = False):
        self.t = t
        self.target = target
        self.source = source
        self.functional = functional
        self.keybytes: tuple[int, int] | None = None
        self._last_rx = 0.0

    # -- connection --------------------------------------------------------
    def connect_5baud(self) -> tuple[int, int]:
        self.keybytes = self.t.five_baud_init(self.target)
        self._last_rx = time.perf_counter()
        return self.keybytes

    def connect_fast(self) -> tuple[int, int]:
        self.t.fast_init()
        resp = self.request(b"\x81")  # StartCommunication
        if len(resp) < 3:
            raise KLineError(f"StartCommunication reply without keybytes: {resp.hex()}")
        self.keybytes = (resp[1], resp[2])
        return self.keybytes

    # -- core request/response --------------------------------------------
    def request(self, data: bytes) -> bytes:
        """Send a request, return positive response data (incl. SID+0x40)."""
        return self._exchange(data, multi=False)[0]

    def request_all(self, data: bytes) -> list[bytes]:
        """For functional requests that several ECUs answer (e.g. DME and a
        Tiptronic TCU): every positive response, in arrival order. A negative
        response from one ECU is raised only if no ECU answered positively."""
        return self._exchange(data, multi=True)

    def _exchange(self, data: bytes, multi: bool) -> list[bytes]:
        sid = data[0]
        wait = self._last_rx + getattr(self.t, "p3", P3_MIN) - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        self.t.write(encode(data, self.target, self.source, self.functional))
        out: list[bytes] = []
        nrc: NegativeResponse | None = None
        pending: set[int | None] = set()  # ECUs that sent 0x78 and owe a final reply
        timeout = P2_TIMEOUT
        deadline = time.perf_counter() + REQUEST_DEADLINE
        while True:
            if time.perf_counter() > deadline:
                raise KLineError(f"no final response to service 0x{sid:02X} "
                                 f"within {REQUEST_DEADLINE:.0f} s")
            try:
                f = read_frame(self.t, timeout)
            except NoResponse:
                if out:
                    return out
                if nrc:
                    raise nrc
                raise
            self._last_rx = time.perf_counter()
            d = f.data
            if len(d) >= 3 and d[0] == 0x7F and d[1] == sid:
                if d[2] == 0x78:
                    pending.add(f.source)
                    timeout = P2_PENDING
                    continue
                pending.discard(f.source)
                nrc = NegativeResponse(sid, d[2])
                if not multi:
                    raise nrc
            elif d and d[0] == sid + 0x40:
                pending.discard(f.source)
                out.append(d)
                if not multi:
                    return out
            else:
                continue  # frame not for this request (other ECU / stale)
            timeout = P2_PENDING if pending else MULTI_GAP

    # -- read-only services ------------------------------------------------
    def start_session(self, mode: int = 0x81) -> bytes:
        return self.request(bytes([0x10, mode]))

    def tester_present(self) -> bytes:
        return self.request(b"\x3E\x01")

    def read_ecu_id(self, option: int) -> bytes:
        return self.request(bytes([0x1A, option]))[2:]

    def read_local_id(self, lid: int) -> bytes:
        return self.request(bytes([0x21, lid]))[2:]

    def read_dtcs(self, status: int = 0x02, group: int = 0xFF00) -> list[tuple[int, int]]:
        """readDTCByStatus -> [(dtc16, status)]."""
        d = self.request(bytes([0x18, status, group >> 8, group & 0xFF]))
        if len(d) < 2 or len(d) - 2 < d[1] * 3:
            raise KLineError(f"DTC reply shorter than its count: {d.hex()}")
        count, body = d[1], d[2:]
        return [((body[i] << 8) | body[i + 1], body[i + 2])
                for i in range(0, count * 3, 3)]

    def stop(self) -> None:
        try:
            self.request(b"\x82")
        except KLineError:
            pass
