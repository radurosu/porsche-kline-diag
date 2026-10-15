"""K-line physical layer: byte I/O over a KKL (FTDI) cable plus wakeup patterns.

The K-line is a single half-duplex wire. Consequences handled here:
- Every byte we send is echoed back to us; we read and verify it (collision check).
- Wakeup patterns (5-baud init, fast init) are produced by toggling the serial
  BREAK condition, which pulls the line low. 5-baud = 200 ms per bit, fast init
  = 25 ms low + 25 ms high. On macOS, break timing via the Apple FTDI driver is
  ms-accurate, which is fine for 5-baud and borderline for fast init (+/-1 ms
  spec). If fast init proves flaky, fall back to 5-baud or pyftdi bit-bang.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Protocol

BAUD = 10400
IDLE_BEFORE_INIT = 0.3  # W5 / Tidle: line must be idle high >= 300 ms
P4_INTERBYTE = 0.005  # tester inter-byte gap; some ME7 ECUs dislike 0


class KLineError(Exception):
    pass


class Transport(Protocol):
    def five_baud_init(self, address: int) -> tuple[int, int]: ...
    def fast_init(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def read(self, n: int, timeout: float) -> bytes: ...
    def read_until_gap(self, gap: float, first_timeout: float) -> bytes: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


def _sleep_until(deadline: float) -> None:
    while (rem := deadline - time.perf_counter()) > 0:
        time.sleep(rem - 0.002 if rem > 0.003 else 0)


class SerialTransport:
    p3 = 0.055  # min gap between ECU response and next request (ISO 14230 P3)

    def __init__(self, port: str, baud: int = BAUD, echo: bool = True,
                 p4: float = P4_INTERBYTE):
        import serial

        self.ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                                 timeout=0.1)
        self.echo = echo
        self.p4 = p4

    def five_baud_init(self, address: int) -> tuple[int, int]:
        """Send `address` at 5 baud, return the ECU's two keybytes.

        Sequence: 0x55 sync, KB1, KB2 from ECU -> tester sends ~KB2 ->
        ECU replies ~address.
        """
        s = self.ser
        s.break_condition = False
        time.sleep(IDLE_BEFORE_INIT)
        # start bit, 8 data bits LSB first (address as given; pass a parity-
        # adjusted value if an ECU insists on 7O1)
        bits = [0] + [(address >> i) & 1 for i in range(8)]
        t = time.perf_counter()
        for bit in bits:
            s.break_condition = bit == 0
            t += 0.2
            _sleep_until(t)
        s.break_condition = False  # stop bit; line high, ECU may answer during it
        # The low phases put 0x00/framing junk into RX. Some of it can still be
        # in the FTDI chip (latency timer) when we reset, so skip junk until 0x55.
        s.reset_input_buffer()

        junk = bytearray()
        deadline = time.perf_counter() + 0.2 + 0.3  # stop bit + W1 max
        while (rem := deadline - time.perf_counter()) > 0:
            b = self.read(1, rem)
            if b == b"\x55":
                break
            junk += b
        else:
            raise KLineError(f"no 0x55 sync after 5-baud 0x{address:02X} "
                             f"(got {junk.hex() or 'nothing'})")
        kb = self.read(2, 0.1)
        if len(kb) != 2:
            raise KLineError(f"keybytes incomplete: {kb.hex()}")
        time.sleep(0.03)  # W4
        self.write(bytes([kb[1] ^ 0xFF]))
        inv = self.read(1, 0.1)
        if inv != bytes([address ^ 0xFF]):
            raise KLineError(f"expected ~address 0x{address ^ 0xFF:02X}, "
                             f"got {inv.hex() or 'nothing'}")
        return kb[0], kb[1]

    def fast_init(self) -> None:
        """25 ms low / 25 ms high wakeup. Caller sends StartCommunication next."""
        s = self.ser
        s.break_condition = False
        time.sleep(IDLE_BEFORE_INIT)
        t = time.perf_counter()
        s.break_condition = True
        _sleep_until(t + 0.025)
        s.break_condition = False
        s.reset_input_buffer()
        _sleep_until(t + 0.050)

    def write(self, data: bytes) -> None:
        # Send the whole frame paced by P4, then check the echo. Waiting for each
        # echo byte before sending the next adds the FTDI latency timer (often
        # 16 ms) to every gap and breaks P4max (20 ms).
        for i, b in enumerate(data):
            self.ser.write(bytes([b]))
            if self.p4 and i < len(data) - 1:
                time.sleep(self.p4)
        if self.echo:
            e = self.read(len(data), 0.1 + len(data) * 0.002)
            if e != data:
                i = next((i for i, (a, b) in enumerate(zip(data, e)) if a != b), len(e))
                raise KLineError(f"echo mismatch at byte {i}: sent {data.hex()}, "
                                 f"got {e.hex() or 'nothing'} (collision?)")

    def read(self, n: int, timeout: float) -> bytes:
        buf = bytearray()
        deadline = time.perf_counter() + timeout
        while len(buf) < n and (rem := deadline - time.perf_counter()) > 0:
            self.ser.timeout = rem
            buf += self.ser.read(n - len(buf))
        return bytes(buf)

    def read_until_gap(self, gap: float, first_timeout: float) -> bytes:
        first = self.read(1, first_timeout)
        if not first:
            return b""
        buf = bytearray(first)
        self.ser.timeout = gap
        while b := self.ser.read(1):
            buf += b
        return bytes(buf)

    def flush(self) -> None:
        self.ser.reset_input_buffer()

    def close(self) -> None:
        self.ser.close()


class TracingTransport:
    """Wraps a transport and logs every operation as JSONL.

    Traces are the main debugging artifact: run on the car, send the file back.
    """

    def __init__(self, inner: Transport, path: str):
        self.inner = inner
        self.f = open(path, "a", buffering=1)
        self.t0 = time.perf_counter()
        self.p3 = getattr(inner, "p3", 0.055)

    def _log(self, op: str, **kw) -> None:
        rec = {"t": round(time.perf_counter() - self.t0, 4), "op": op, **kw}
        self.f.write(json.dumps(rec) + "\n")

    def five_baud_init(self, address: int) -> tuple[int, int]:
        self._log("5baud", addr=f"{address:02x}")
        try:
            kb = self.inner.five_baud_init(address)
        except KLineError as e:
            self._log("error", msg=str(e))
            raise
        self._log("keybytes", hex=f"{kb[0]:02x}{kb[1]:02x}")
        return kb

    def fast_init(self) -> None:
        self._log("fastinit")
        self.inner.fast_init()

    def write(self, data: bytes) -> None:
        self._log("tx", hex=data.hex())
        try:
            self.inner.write(data)
        except KLineError as e:
            self._log("error", msg=str(e))
            raise

    def read(self, n: int, timeout: float) -> bytes:
        d = self.inner.read(n, timeout)
        self._log("rx", hex=d.hex(), want=n)
        return d

    def read_until_gap(self, gap: float, first_timeout: float) -> bytes:
        d = self.inner.read_until_gap(gap, first_timeout)
        self._log("rx", hex=d.hex())
        return d

    def flush(self) -> None:
        self.inner.flush()

    def close(self) -> None:
        self.inner.close()
        self.f.close()


class QueueTransport:
    """In-memory transport base for the mock ECU. Each queued chunk is one
    ECU message; read() spans chunks, read_until_gap() returns one chunk."""

    p3 = 0.0  # no bus timing to respect

    def __init__(self):
        self.rx: deque[bytes] = deque()

    def push(self, data: bytes) -> None:
        if data:
            self.rx.append(data)

    def read(self, n: int, timeout: float) -> bytes:
        buf = bytearray()
        while len(buf) < n and self.rx:
            chunk = self.rx.popleft()
            take = n - len(buf)
            buf += chunk[:take]
            if len(chunk) > take:
                self.rx.appendleft(chunk[take:])
        return bytes(buf)

    def read_until_gap(self, gap: float, first_timeout: float) -> bytes:
        return self.rx.popleft() if self.rx else b""

    def flush(self) -> None:
        self.rx.clear()

    def close(self) -> None:
        pass
