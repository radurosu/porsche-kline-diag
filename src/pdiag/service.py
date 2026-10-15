"""One car, one K-line: serialises access and owns ECU session switching.

The K-line talks to one ECU session at a time. Generic OBD (0x33) and the DME
are separate sessions; switching re-inits. On real hardware we wait for the
previous session to time out (P3max 5 s) before waking a different address.
"""

from __future__ import annotations

import threading
import time

from . import kwp2000, models, obd
from .transport import KLineError, Transport

RPM_PID = 0x0C
# Read rpm every sample and rotate through the rest, so the needle stays smooth
# on a slow bus (ISO 9141-2 manages roughly 8-10 requests/s).
ROTATING_PIDS = {0x05: "coolant_temp", 0x0E: "timing_advance", 0x11: "throttle",
                 0x04: "engine_load", 0x10: "maf", 0x0D: "speed",
                 0x06: "stft_b1", 0x07: "ltft_b1"}
PER_SAMPLE = 2
SESSION_TIMEOUT = 5.5
# ECUs drop a session after P3max (5 s) without traffic. Past this much idle
# time, assume it's gone and wake the ECU again instead of timing out.
SESSION_IDLE = 4.0
LIVE_MIN_INTERVAL = 0.09  # several browser tabs share one bus sample


class NotDecoded(Exception):
    """Feature needs Porsche-specific data we haven't decoded yet."""


class Car:
    def __init__(self, t: Transport, model: models.Model | None = None,
                 dme_addr: int | None = None, mock: bool = False,
                 port: str | None = None, label: str | None = None):
        self.t = t
        self.model = model  # None until given or detected from the VIN
        self.label = label
        self.dme_addr = dme_addr if dme_addr is not None else (model or models.DEFAULT).dme_addr
        self.mock = mock
        self.port = port
        self.lock = threading.RLock()
        self.link = None
        self.dme: kwp2000.Kwp2000Client | None = None
        self.info: dict = {}
        self._supported: set[int] = set()
        self._latest: dict = {}  # last value of every live PID, across samples
        self._sample_t = 0.0
        self._last_session_end = 0.0
        self._last_io = 0.0
        self._pending = 0  # one-shot requests waiting; live streaming yields to them
        self._plock = threading.Lock()
        self._rot = 0

    # -- sessions ----------------------------------------------------------
    def _drop(self) -> None:
        if self.dme:
            self.dme.stop()
        self.link = self.dme = None
        self._last_session_end = time.monotonic()

    def _expire(self) -> None:
        if (self.link or self.dme) and time.monotonic() - self._last_io > SESSION_IDLE:
            self.link = self.dme = None
            self._last_session_end = self._last_io

    def _settle(self) -> None:
        if not self.mock:
            wait = self._last_session_end + SESSION_TIMEOUT - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    def _obd(self):
        self._expire()
        if self.link is None:
            if self.dme:
                self._drop()
            self._settle()
            link, kb = obd.connect(self.t)
            self._supported = set(obd.supported_pids(link))
            self._latest = {}
            self.link = link
            self.info["generic_protocol"] = link.name
            self.info["generic_keybytes"] = f"{kb[0]:02X} {kb[1]:02X}"
        return self.link

    def _dme(self) -> kwp2000.Kwp2000Client:
        self._expire()
        if self.dme is None:
            if self.link:
                self._drop()
            self._settle()
            c = kwp2000.Kwp2000Client(self.t, self.dme_addr)
            kb = c.connect_5baud()
            self.dme = c
            self.info["dme_address"] = f"0x{self.dme_addr:02X}"
            self.info["dme_keybytes"] = f"{kb[0]:02X} {kb[1]:02X}"
        return self.dme

    def _guard(self, fn, background: bool = False):
        if background:
            while self._pending:
                time.sleep(0.01)
        else:
            with self._plock:
                self._pending += 1
        try:
            return self._locked(fn)
        finally:
            if not background:
                with self._plock:
                    self._pending -= 1

    def _locked(self, fn):
        with self.lock:
            try:
                return fn()
            except kwp2000.NegativeResponse:
                raise  # the ECU answered; the session is still up
            except KLineError:
                self.link = self.dme = None
                self._last_session_end = time.monotonic()
                raise
            finally:
                self._last_io = time.monotonic()

    # -- API ---------------------------------------------------------------
    def status(self) -> dict:
        m = self.model or models.DEFAULT
        return {"mode": "mock" if self.mock else "hardware", "port": self.port,
                "car": self.label or (self.model.name if self.model else "Porsche"),
                "model": self.model.code if self.model else None,
                "profile": m.profile(),
                "connected": bool(self.link or self.dme), **self.info}

    def live(self) -> dict:
        """Full snapshot: rpm fresh, rotating PIDs as last read."""
        def run():
            if time.monotonic() - self._sample_t < LIVE_MIN_INTERVAL and self._latest:
                return dict(self._latest)
            link = self._obd()
            out = {"rpm": round(obd.read_pid(link, RPM_PID)[0], 2)}
            # Only PIDs the car reports: an unsupported one gets no answer on
            # ISO 9141-2, which would look like a dead link.
            items = [(p, n) for p, n in ROTATING_PIDS.items() if p in self._supported]
            for _ in range(PER_SAMPLE if items else 0):
                pid, name = items[self._rot % len(items)]
                self._rot += 1
                out[name] = round(obd.read_pid(link, pid)[0], 2)
            self._latest.update(out)
            self._sample_t = time.monotonic()
            return dict(self._latest)
        return self._guard(run, background=True)

    def dtcs(self) -> list[dict]:
        from .mock import MOCK_DTC_TEXT
        def run():
            out = []
            for code, status in self._dme().read_dtcs():
                out.append({
                    "code": obd.dtc_str(code), "raw": f"{code:04X}",
                    "status": f"{status:02X}",
                    # ISO 14230 status: bit 7 = warning lamp, bits 6-5 = 11 present
                    "lamp": bool(status & 0x80),
                    "present": (status >> 5) & 3 == 3,
                    "text": MOCK_DTC_TEXT.get(code) if self.mock else None,
                })
            return out
        return self._guard(run)

    def ident(self) -> dict:
        def run():
            c = self._dme()
            found = {}
            for opt in (0x90, 0x9B, 0x91, 0x92, 0x94, 0x80):
                try:
                    found[f"{opt:02X}"] = c.read_ecu_id(opt).decode("ascii", "replace").strip()
                except kwp2000.NegativeResponse:
                    pass
            vin = found.get("90")
            if self.model is None and (m := models.from_vin(vin)):
                self.model = m
            return {"vin": vin, "software": found.get("9B"),
                    "raw": found, **self.status()}
        return self._guard(run)

    def over_rev(self) -> dict:
        if not self.mock:
            raise NotDecoded("Over-rev counters aren't decoded for this DME yet. "
                             "Capture a Durametric session with `pdiag sniff` to decode them.")
        from .mock import mock_over_rev
        return mock_over_rev(self.model or models.DEFAULT)
