"""One car, one K-line: serialises access and owns ECU session switching.

The K-line talks to one ECU session at a time. Generic OBD (0x33; ISO 9141-2 on
a 2002 996) and the DME's own KWP2000 session (0x11) are separate sessions, even
though the same ECU answers both; switching re-inits, and on real hardware we
wait for the previous session to time out (P3max 5 s) first.

Two live streams sit on top: live() for decoded OBD readings and dme_live() for
the DME's raw Porsche data blocks. Both can log every read as JSONL.
"""

from __future__ import annotations

import json
import threading
import time
from typing import TextIO

from . import kwp2000, models, obd
from .transport import KLineError, NoResponse, Transport

RPM_PID = 0x0C
# Each sample reads rpm plus the next of every other PID the car supports, so
# the needle gets every other request: a 2002 996 manages ~4.6 requests/s while
# waiting for other ECUs, ~7 when only one answers (the mock is instant).
# Status PIDs that rarely change are read once a minute instead.
STATIC_PIDS = {0x01, 0x13, 0x1C}
STATIC_REFRESH = 60.0
# Porsche-specific data blocks (KWP 0x21 local IDs) a 2002 996 ME7.8 answered in a
# full 01-FF sweep on 2026-10-15. Not decoded yet: shown and logged raw.
DME_LIDS = (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C,
            0x14, 0xEA)
# Rejections that mean "this ECU doesn't have that PID", as opposed to busy etc.
NOT_SUPPORTED = {0x11, 0x12, 0x31}
SESSION_TIMEOUT = 5.5
# ECUs drop a session after P3max (5 s) without traffic. Past this much idle
# time, assume it's gone and wake the ECU again instead of timing out.
SESSION_IDLE = 4.0


class NotDecoded(Exception):
    """Feature needs Porsche-specific data we haven't decoded yet."""


def _num(v: float | str) -> float | str:
    return round(v, 2) if isinstance(v, float) else v


def id_text(data: bytes) -> str:
    """An identification field for display: text without its padding, or hex
    when the ECU sent binary."""
    s = data.strip(b" \x00\xff")
    if s and all(0x20 <= b < 0x7F for b in s):
        return s.decode("ascii")
    return data.hex()


class Car:
    def __init__(self, t: Transport, model: models.Model | None = None,
                 dme_addr: int | None = None, mock: bool = False,
                 port: str | None = None, label: str | None = None,
                 live_log: TextIO | None = None, dme_log: TextIO | None = None):
        self.t = t
        self.live_log = live_log  # one JSON line per live sample, fresh values only
        self.dme_log = dme_log  # one JSON line per data block read
        self.model = model  # None until given or detected from the VIN
        self.label = label
        self.dme_addr = dme_addr if dme_addr is not None else (model or models.DEFAULT).dme_addr
        self.mock = mock
        self.port = port
        self.lock = threading.RLock()
        self.link = None
        self.dme: kwp2000.Kwp2000Client | None = None
        self.info: dict = {}
        self._supported: set[int] | None = None  # None = bitmap unreadable: try every PID
        self._supported_retried = False
        self._dead_pids: set[int] = set()  # silent or rejected this session
        self._static_due = 0.0
        self._blocks: dict[str, str] = {}  # DME data blocks: lid -> hex, last read
        self._dead_lids: set[int] = set()
        self._last_lid = -1
        self._latest: dict = {}  # last value of every live PID, across samples
        self._seq = 0  # samples taken; lets tabs share one sample instead of queueing
        self._last_session_end = 0.0
        self._last_io = 0.0
        self._pending = 0  # one-shot requests waiting; live streaming yields to them
        self._plock = threading.Lock()
        self._last_pid = -1  # rotation cursor: the PID read last

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
            self._supported = self._read_supported(link)
            if getattr(link, "ecus", None) == 1:
                link.settle = obd.SINGLE_ECU_SETTLE  # nobody else to wait for
            self._supported_retried = False
            self._dead_pids = set()
            self._static_due = 0.0
            self._latest = {}
            self.link = link
            self.info["generic_protocol"] = link.name
            self.info["generic_keybytes"] = f"{kb[0]:02X} {kb[1]:02X}"
        return self.link

    @staticmethod
    def _read_supported(link) -> set[int] | None:
        try:
            return set(obd.supported_pids(link))
        except KLineError:
            return None  # the rpm read that follows tells whether the link is up

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
            except (kwp2000.NegativeResponse, obd.BadReply):
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
                "units": {p.name: p.unit for p in obd.PIDS.values()},
                "connected": bool(self.link or self.dme), **self.info}

    def live(self) -> dict:
        """Full snapshot: rpm fresh, rotating PIDs as last read. If another tab took
        a sample while this call waited for the bus, share it instead of queueing
        a second one, so every tab gets every sample and the bus never idles."""
        seen = self._seq

        def run():
            if self._seq != seen and self._latest:
                return dict(self._latest)
            link = self._obd()
            if self._supported is None and not self._supported_retried:
                self._supported_retried = True
                self._supported = self._read_supported(link)
            out = {"rpm": _num(obd.read_pid(link, RPM_PID)[0])}
            usable = [p for p in sorted(obd.PIDS) if p != RPM_PID and p not in self._dead_pids
                      and (self._supported is None or p in self._supported)]
            rotating = [p for p in usable if p not in STATIC_PIDS]
            todo = []
            if rotating:  # the next PID after the last one read, so a drop skips nothing
                self._last_pid = next((p for p in rotating if p > self._last_pid), rotating[0])
                todo.append(self._last_pid)
            if time.monotonic() >= self._static_due:
                todo += [p for p in usable if p in STATIC_PIDS]
                self._static_due = time.monotonic() + STATIC_REFRESH
            for pid in todo:
                self._read_into(out, link, pid)
            self._latest.update(out)
            self._seq += 1
            if self.live_log:
                self.live_log.write(json.dumps({"t": round(time.time(), 3), **out}) + "\n")
            return dict(self._latest)
        return self._guard(run, background=True)

    def _read_into(self, out: dict, link, pid: int) -> None:
        """Read one PID into `out`. rpm answered moments ago, so the link is up:
        silence (ISO 9141-2) or "not supported" means this ECU lacks the PID, so
        stop asking for this session. Busy or a garbled reply: try next time."""
        name = obd.PIDS[pid].name
        try:
            out[name] = _num(obd.read_pid(link, pid)[0])
        except NoResponse:
            self._drop_pid(pid, name)
        except kwp2000.NegativeResponse as e:
            if e.code in NOT_SUPPORTED:
                self._drop_pid(pid, name)
        except obd.BadReply:
            pass

    def dme_live(self) -> dict:
        """Read the next Porsche data block from the DME. Returns every block
        read so far (hex) and which one is new."""
        def run():
            c = self._dme()
            lids = [x for x in DME_LIDS if x not in self._dead_lids]
            if not lids:
                raise NotDecoded("The engine computer didn't return any data blocks.")
            lid = self._last_lid = next((x for x in lids if x > self._last_lid), lids[0])
            key = f"{lid:02x}"
            try:
                data = c.read_local_id(lid)
            except kwp2000.NegativeResponse as e:
                if e.code in NOT_SUPPORTED:  # this DME doesn't have that block
                    self._dead_lids.add(lid)
                    self._blocks.pop(key, None)
                return {"blocks": dict(self._blocks), "new": None}
            self._blocks[key] = data.hex()
            if self.dme_log:
                self.dme_log.write(json.dumps({"t": round(time.time(), 3), "lid": key,
                                               "hex": data.hex()}) + "\n")
            return {"blocks": dict(self._blocks), "new": key}
        return self._guard(run, background=True)

    def _drop_pid(self, pid: int, name: str) -> None:
        self._dead_pids.add(pid)
        self._latest.pop(name, None)

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
            found, answered, silent = {}, False, None
            for opt in (0x90, 0x9B, 0x91, 0x92, 0x94, 0x80):
                try:
                    found[f"{opt:02X}"] = id_text(c.read_ecu_id(opt))
                    answered = True
                except kwp2000.NegativeResponse:
                    answered = True
                except NoResponse as e:
                    silent = e  # some ECUs ignore options they lack; keep going
            if not answered:
                raise silent  # nothing answered at all: the session is gone
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
