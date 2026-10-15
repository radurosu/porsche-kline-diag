import http.client
import threading
import types
from http.server import ThreadingHTTPServer

import pytest

from pdiag import cli, kwp2000, models, obd, transport, web
from pdiag.mock import MockCar
from pdiag.service import STATIC_PIDS, Car, id_text

# What a real 2002 996 (ME7.8) reports in PID 00, mirrored by the mock.
REAL_996_PIDS = [0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0C, 0x0D, 0x0E, 0x0F,
                 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x18, 0x19, 0x1C]
ALL_996 = {obd.PIDS[p].name for p in REAL_996_PIDS}
from pdiag.transport import KLineError, NoResponse, QueueTransport, SerialTransport


def test_encode_decode_roundtrip():
    raw = kwp2000.encode(b"\x1A\x9B", 0x10)
    assert raw == bytes([0x82, 0x10, 0xF1, 0x1A, 0x9B, (0x82 + 0x10 + 0xF1 + 0x1A + 0x9B) & 0xFF])
    f = kwp2000.decode(raw)
    assert (f.target, f.source, f.data) == (0x10, 0xF1, b"\x1A\x9B")


def test_long_frame_uses_length_byte():
    data = bytes(range(70))
    raw = kwp2000.encode(data, 0x10)
    assert raw[0] == 0x80 and raw[3] == 70
    assert kwp2000.decode(raw).data == data


def test_bad_checksum_rejected():
    raw = bytearray(kwp2000.encode(b"\x3E\x01", 0x10))
    raw[-1] ^= 1
    with pytest.raises(KLineError):
        kwp2000.decode(bytes(raw))


def test_split_frames():
    a = kwp2000.encode(b"\x21\x01", 0x10)
    b = kwp2000.encode(b"\x61\x01\xAA", 0xF1, 0x10)
    assert [f.data for f in kwp2000.split_frames(a + b)] == [b"\x21\x01", b"\x61\x01\xAA"]


def test_dtc_str():
    assert obd.dtc_str(0x0300) == "P0300"
    assert obd.dtc_str(0x4123) == "C0123"
    assert obd.dtc_str(0x1128) == "P1128"


@pytest.mark.parametrize("generic,name", [("iso9141", "ISO 9141-2"), ("kwp", "ISO 14230 (KWP2000)")])
def test_generic_obd(generic, name):
    link, kb = obd.connect(MockCar(generic))
    assert link.name == name
    assert obd.supported_pids(link) == REAL_996_PIDS
    assert obd.read_pid(link, 0x0C)[0] == 850
    assert obd.read_pid(link, 0x05)[0] == 88
    assert obd.read_stored_dtcs(link) == ["P0300", "P0420"]


def test_dme_services():
    c = kwp2000.Kwp2000Client(MockCar(), 0x11)
    assert c.connect_5baud() == (0xEF, 0x8F)
    assert c.read_ecu_id(0x9B).startswith(b"MOCK")
    assert c.read_dtcs() == [(0x0300, 0xE0), (0x1128, 0x20)]
    assert c.read_local_id(0xEE) == b"\x01\x02"  # after responsePending
    with pytest.raises(kwp2000.NegativeResponse) as e:
        c.read_local_id(0x99)
    assert e.value.code == 0x31


def test_fast_init():
    c = kwp2000.Kwp2000Client(MockCar(), 0x11)
    assert c.connect_fast() == (0xEF, 0x8F)


def test_wrong_address_fails():
    with pytest.raises(KLineError):
        kwp2000.Kwp2000Client(MockCar(), 0x42).connect_5baud()


@pytest.mark.parametrize("argv", [
    ["probe"], ["probe", "--init", "both"], ["obd"], ["--mock-generic", "kwp", "obd"], ["live", "--count", "2"],
    ["id"], ["dtc"], ["lid-scan", "--end", "0x12"],
])
def test_cli_smoke(argv, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    cli.main(["--mock"] + argv)
    assert capsys.readouterr().out
    assert list(tmp_path.glob("*.jsonl"))


# -- models -------------------------------------------------------------------
@pytest.mark.parametrize("vin,code", [
    ("WP0AA29902STEST00", "996"), ("WP0CA29876UTEST00", "987"),
    ("WP0AB29856UTEST00", "987"), ("WP0CA2981YSTEST00", "986"),
    ("WP0AA29976STEST00", "997"), ("WP0ZZZ98Z9STEST00", None),  # 987.2: CAN era
    ("WP1ZZZ9PZ5LA00000", None), ("SHORT", None), (None, None),
])
def test_model_from_vin(vin, code):
    m = models.from_vin(vin)
    assert (m.code if m else None) == code


@pytest.mark.parametrize("code", ["996", "987"])
def test_car_detects_model_from_vin(code):
    car = Car(MockCar(model=code), mock=True)
    assert car.status()["model"] is None
    car.ident()
    st = car.status()
    assert st["model"] == code and st["car"] == models.MODELS[code].name
    assert [r["rpm_from"] for r in car.over_rev()["ranges"]] == list(st["profile"]["over_rev_from"])


def test_cli_model_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    cli.main(["--mock", "--model", "987", "id"])
    assert "WP0ZZZ98Z6U" in capsys.readouterr().out


# -- multiple ECUs / robustness -------------------------------------------------
@pytest.mark.parametrize("generic", ["iso9141", "kwp"])
def test_generic_obd_with_second_ecu(generic):
    # The TCU answers first; for unsupported PIDs it NRCs (KWP) or stays silent (9141).
    link, _ = obd.connect(MockCar(generic, tcu=True))
    assert obd.supported_pids(link) == REAL_996_PIDS
    for _ in range(3):  # no stale second reply leaks into the next request
        assert obd.read_pid(link, 0x0C)[0] == 850
        assert obd.read_pid(link, 0x05)[0] == 88
    assert obd.read_stored_dtcs(link) == ["P0300", "P0420"]


class _Chunks(QueueTransport):
    """Answers each request with the given chunks (one read_until_gap each)."""

    def __init__(self, *chunks):
        super().__init__()
        self.chunks = chunks

    def write(self, data):
        for c in self.chunks:
            self.push(c)


def _msg9141(*data, ecu=0x10):
    body = bytes([0x48, 0x6B, ecu, *data])
    return body + bytes([kwp2000.checksum(body)])


def test_iso9141_reassembles_split_message():
    msg = _msg9141(0x41, 0x0C, 0x0D, 0x48)
    t = _Chunks(msg[:4], msg[4:])
    assert obd.read_pid(obd.Iso9141Link(t), 0x0C)[0] == 850


def test_split_stream_resyncs_and_carries_rest():
    a = kwp2000.encode(b"\x21\x01", 0x10)
    b = kwp2000.encode(b"\x61\x01\xAA", 0xF1, 0x10)
    frames, skipped, rest = kwp2000.split_stream(b"\x55\xef\x8f" + a + b[:3])
    assert [f.data for f in frames] == [b"\x21\x01"]
    assert skipped == b"\x55\xef\x8f" and rest == b[:3]
    assert kwp2000.split_stream(rest + b[3:])[0][0].data == b"\x61\x01\xAA"


def test_live_survives_and_session_reopens_after_idle():
    car = Car(MockCar(), mock=True)
    for _ in range(10):
        assert car.live()["rpm"] == 850
    link = car.link
    with pytest.raises(kwp2000.NegativeResponse):
        car._locked(lambda: car._dme().read_local_id(0x99))
    assert car.dme is not None  # an NRC doesn't drop the session
    car._last_io -= 10  # ECU timed the session out
    car.live()
    assert car.link is not link


# -- serial transport (fake FTDI) ---------------------------------------------
class _NoTimeoutWrites:
    """Fake pyserial port. Assigning `timeout` after open reconfigures a real
    port (and corrupts bytes on the wire), so the fakes refuse it."""

    @property
    def timeout(self):
        return 0

    @timeout.setter
    def timeout(self, v):
        raise AssertionError("SerialTransport set ser.timeout: pyserial reconfigures the port")


class FakeSerial(_NoTimeoutWrites):
    def __init__(self, after_reset: bytes, addr: int, collide: bool = False):
        self.rx = bytearray()
        self.after_reset, self.addr, self.collide = after_reset, addr, collide
        self.break_condition = False
        self.port = "/dev/cu.fake"

    def wait(self, timeout):
        return bool(self.rx)

    def reset_input_buffer(self):
        self.rx = bytearray(self.after_reset)
        self.after_reset = b""

    def write(self, b):
        self.rx += bytes([b[0] ^ 1]) if self.collide else b
        if b[0] == 0x8F ^ 0xFF:  # ~KB2 -> ECU answers ~address
            self.rx.append(self.addr ^ 0xFF)

    def read(self, n):
        out, self.rx = bytes(self.rx[:n]), self.rx[n:]
        return out

    def close(self):
        pass


def _st(fake):
    """A SerialTransport on a fake port; select() is replaced by the fake's wait."""
    st = SerialTransport.__new__(SerialTransport)
    st.ser, st.echo, st.p4, st._wait = fake, True, 0, fake.wait
    return st


def _serial(monkeypatch, fake):
    from pdiag import transport
    monkeypatch.setattr(transport, "_sleep_until", lambda d: None)
    monkeypatch.setattr(transport.time, "sleep", lambda s: None)
    return _st(fake)


def test_read_until_gap_never_touches_port_timeout():
    fake = FakeSerial(b"", 0x10)
    fake.rx += b"\x48\x6b\x10\x41\x0c"
    assert _st(fake).read_until_gap(gap=0.022, first_timeout=0.5) == b"\x48\x6b\x10\x41\x0c"


def test_five_baud_skips_junk_before_sync(monkeypatch):
    st = _serial(monkeypatch, FakeSerial(b"\x00\x00\x55\xef\x8f", 0x10))
    assert st.five_baud_init(0x10) == (0xEF, 0x8F)


def test_write_detects_collision(monkeypatch):
    st = _serial(monkeypatch, FakeSerial(b"", 0x10))
    st.write(b"\x81\x10\xf1\x81\x03")
    st.ser.collide = True
    with pytest.raises(KLineError, match="echo mismatch at byte 0"):
        st.write(b"\x81\x10\xf1\x81\x03")


# -- review fixes ---------------------------------------------------------------
@pytest.mark.parametrize("generic", ["iso9141", "kwp"])
def test_first_obd_request_waits_p3(generic):
    link, _ = obd.connect(MockCar(generic))
    last = link._last if generic == "iso9141" else link.client._last_rx
    assert last > 0


class _Scripted(QueueTransport):
    """ECU replies that arrive after a delay; a read with a shorter timeout misses them."""

    def __init__(self, events):
        super().__init__()
        self.events = list(events)  # (delay_s, raw frame)

    def write(self, data):
        pass

    def fast_init(self):
        pass

    def read(self, n, timeout):
        if not self.rx and self.events and self.events[0][0] <= timeout:
            self.push(self.events.pop(0)[1])
        return super().read(n, timeout)

    def read_until_gap(self, gap, first_timeout):
        if not self.rx and self.events and self.events[0][0] <= first_timeout:
            self.push(self.events.pop(0)[1])
        return super().read_until_gap(gap, first_timeout)


def test_pending_ecu_not_cut_short_by_other_reply():
    dme, tcu = 0x10, 0x18
    t = _Scripted([
        (0.03, kwp2000.encode(b"\x7F\x03\x78", 0xF1, dme)),
        (0.03, kwp2000.encode(b"\x43\x04\x20", 0xF1, tcu)),
        (0.5, kwp2000.encode(b"\x43\x03\x00", 0xF1, dme)),
    ])
    link = obd.KwpObdLink(kwp2000.Kwp2000Client(t, 0x33, functional=True))
    assert sorted(obd.read_stored_dtcs(link)) == ["P0300", "P0420"]


def test_rejected_mode03_is_an_error():
    t = _Chunks(_msg9141(0x7F, 0x03, 0x22))
    with pytest.raises(kwp2000.NegativeResponse) as e:
        obd.read_stored_dtcs(obd.Iso9141Link(t))
    assert (e.value.service, e.value.code) == (0x03, 0x22)


@pytest.mark.parametrize("payload", [b"\x58\x02", b"\x58\x02\x03\x00\xE0\x11"])
def test_short_dtc_reply_is_an_error(payload):
    t = _Scripted([(0, kwp2000.encode(payload, 0xF1, 0x10))])
    with pytest.raises(KLineError, match="shorter than its count"):
        kwp2000.Kwp2000Client(t, 0x10).read_dtcs()


def test_live_clients_each_get_full_snapshot():
    car = Car(MockCar(), mock=True)
    names = ALL_996
    seen = []
    for _ in range(40):  # two tabs taking turns
        seen.append(car.live())
    assert set(seen[-1]) == names and set(seen[-2]) == names


# -- second review: supported list, multi-ECU timing, garbled frames, probe ---------
def test_live_polls_everything_when_supported_list_unreadable(monkeypatch):
    real, calls = obd.supported_pids, []

    def flaky(link):
        calls.append(1)
        if len(calls) == 1:
            raise KLineError("no PID 00 bitmap")
        return real(link)

    monkeypatch.setattr(obd, "supported_pids", flaky)
    car = Car(MockCar(), mock=True)
    car.live()
    assert len(calls) == 2  # retried once on the next sample...
    assert car._supported is not None  # ...and the retry worked
    for _ in range(25):
        snap = car.live()
    assert set(snap) == ALL_996


def test_live_polls_everything_when_bitmap_never_reads(monkeypatch):
    def broken(link):
        raise KLineError("no PID 00 bitmap")

    monkeypatch.setattr(obd, "supported_pids", broken)
    car = Car(MockCar(), mock=True)
    for _ in range(25):
        snap = car.live()
    assert car._supported is None
    assert set(snap) == ALL_996


@pytest.mark.parametrize("generic", ["iso9141", "kwp"])
def test_live_drops_silent_pid_but_keeps_session(generic):
    # Bitmap claims MAF, ECU never answers it (or the bitmap was unreadable).
    car = Car(MockCar(generic, silent={b"\x01\x10"}), mock=True)
    car.live()
    link = car.link
    for _ in range(25):
        snap = car.live()
    assert car.link is link
    assert 0x10 in car._dead_pids and "maf" not in snap
    assert set(snap) == ALL_996 - {"maf"}


def test_answer_after_quick_rejection_is_kept():
    t = _Scripted([
        (0.02, kwp2000.encode(b"\x7F\x01\x12", 0xF1, 0x18)),  # TCU: not mine
        (0.15, kwp2000.encode(b"\x41\x0C\x0D\x48", 0xF1, 0x10)),  # DME, 150 ms later
    ])
    c = kwp2000.Kwp2000Client(t, 0x33, functional=True)
    assert c.request_all(b"\x01\x0C") == [b"\x41\x0C\x0D\x48"]
    assert not t.rx and not t.events  # nothing left to leak into the next request


def test_late_reply_is_drained_not_taken_as_answer():
    t = MockCar()
    c = kwp2000.Kwp2000Client(t, 0x11)
    c.connect_5baud()
    t.push(kwp2000.encode(b"\x61\x01\xAA\xBB", 0xF1, 0x10))  # late reply to 21 01
    assert c.read_local_id(0x02) == bytes(range(2, 8))


def test_garbled_second_frame_keeps_good_answer():
    bad = bytearray(kwp2000.encode(b"\x41\x0C\x00\x00", 0xF1, 0x18))
    bad[-1] ^= 0xFF
    t = _Scripted([(0.02, kwp2000.encode(b"\x41\x0C\x0D\x48", 0xF1, 0x10)),
                   (0.03, bytes(bad))])
    c = kwp2000.Kwp2000Client(t, 0x33, functional=True)
    assert c.request_all(b"\x01\x0C") == [b"\x41\x0C\x0D\x48"]


def test_iso9141_garbled_message_does_not_hide_the_next():
    garbled = bytearray(_msg9141(0x41, 0x0C, 0x00, 0x00, ecu=0x18))
    garbled[-1] ^= 0xFF
    t = _Chunks(bytes(garbled), _msg9141(0x41, 0x0C, 0x0D, 0x48))
    assert obd.read_pid(obd.Iso9141Link(t), 0x0C)[0] == 850


def test_obd_command_survives_unknown_bitmap_and_still_reads_dtcs(capsys):
    cli.cmd_obd(types.SimpleNamespace(init="auto"), MockCar(silent={b"\x01\x00"}))
    out = capsys.readouterr().out
    assert "supported PIDs: unknown" in out
    assert "rpm" in out and "850.00" in out
    assert "stored DTCs: P0300, P0420" in out


@pytest.mark.parametrize("transport,init,wait", [
    (lambda: _Scripted([(0, kwp2000.encode(b"\x7F\x81\x10", 0xF1, 0x10))]), "fast", "full"),
    (lambda: MockCar(), "5baud", 0.5),  # 0x42: nothing answers
])
def test_probe_wait_depends_on_whether_anything_answered(monkeypatch, capsys, transport, init, wait):
    from pdiag.service import SESSION_TIMEOUT
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    addr = 0x10 if init == "fast" else 0x42
    cli.cmd_probe(types.SimpleNamespace(addrs=[addr], init=init, mock=False), transport())
    assert slept == [SESSION_TIMEOUT if wait == "full" else wait]



# -- third round: review findings 1-12 ---------------------------------------------
class _Clock:
    """Stands in for the `time` module so timing tests run instantly."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def perf_counter(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += max(s, 0.0005)


@pytest.mark.parametrize("chunks,want", [
    ([_msg9141(0x41, 0x0C, 0x0D, 0x48) + _msg9141(0x41, 0x0C, 0x00, 0x00, ecu=0x18)], 2),  # merged
    ([b"\x00\x13" + _msg9141(0x41, 0x0C, 0x0D, 0x48)], 1),  # junk before a message
    ([_msg9141(0x41, 0x0C, 0x0D, 0x48)[:4], _msg9141(0x41, 0x0C, 0x0D, 0x48)[4:]], 1),  # split
    ([_msg9141(0x41, 0x0C, 0x0D, 0x48) + b"\x48"], 1),  # stray byte after
])
def test_iso9141_split_and_merged_messages(chunks, want):
    assert len(obd.Iso9141Link(_Chunks(*chunks)).request(b"\x01\x0C")) == want
    assert obd.read_pid(obd.Iso9141Link(_Chunks(*chunks)), 0x0C)[0] == 850


def test_split_9141_keeps_an_incomplete_tail():
    m = _msg9141(0x41, 0x05, 0x80)
    assert obd.split_9141(m + m[:3]) == ([m[3:-1]], m[:3])


@pytest.mark.parametrize("make_link", [
    lambda t: obd.KwpObdLink(kwp2000.Kwp2000Client(t, 0x33, functional=True)),
    lambda t: obd.Iso9141Link(t),
])
def test_fault_codes_wait_for_a_slow_ecu(make_link):
    kwp = make_link(_Scripted([])).__class__ is obd.KwpObdLink
    tcu = (kwp2000.encode(b"\x43\x00\x00", 0xF1, 0x18) if kwp
           else _msg9141(0x43, 0, 0, 0, 0, 0, 0, ecu=0x18))
    dme = (kwp2000.encode(b"\x43\x03\x00\x04\x20", 0xF1, 0x10) if kwp
           else _msg9141(0x43, 0x03, 0x00, 0x04, 0x20, 0, 0))
    t = _Scripted([(0.03, tcu), (0.2, dme)])  # TCU: no codes, at once; DME: 200 ms later
    assert obd.read_stored_dtcs(make_link(t)) == ["P0300", "P0420"]


class _TimedEcu(_NoTimeoutWrites):
    """Fake FTDI port on a _Clock. The ECU starts its 0x55 sync `w1` after the
    stop bit; every byte reaches us `latency` after it was on the wire."""

    def __init__(self, clock, w1, latency):
        self.clock, self.w1, self.latency = clock, w1, latency
        self.break_condition = False
        self.queue, self.kb2_wire, self.w4 = [], None, None

    def reset_input_buffer(self):  # five_baud_init calls this as the stop bit starts
        if self.w1 is None:
            return
        t = self.clock.now + 0.2 + self.w1
        self.kb2_wire = t + 0.003
        self._send([(t + 0.001, 0x55), (t + 0.002, 0xEF), (self.kb2_wire, 0x8F)])

    def _send(self, items):
        self.queue = sorted(self.queue + [(t + self.latency, b) for t, b in items])

    def wait(self, timeout):  # select(): sleep until a byte is due, or time out
        if self.queue and self.queue[0][0] <= self.clock.now + timeout:
            self.clock.now = max(self.clock.now, self.queue[0][0])
            return True
        self.clock.now += timeout
        return False

    def read(self, n):  # non-blocking, like pyserial with timeout=0
        out = bytearray()
        while len(out) < n and self.queue and self.queue[0][0] <= self.clock.now:
            out.append(self.queue.pop(0)[1])
        return bytes(out)

    def write(self, b):
        now = self.clock.now
        self._send([(now + 0.001, b[0])])  # echo
        if b[0] == 0x8F ^ 0xFF:
            self.w4 = now - self.kb2_wire
            self._send([(now + 0.03, 0x10 ^ 0xFF)])  # ~address


@pytest.mark.parametrize("w1,latency", [(0.06, 0.0), (0.3, 0.0), (0.3, 0.016), (0.06, 0.016)])
def test_five_baud_timing_within_spec(monkeypatch, w1, latency):
    clock = _Clock()
    monkeypatch.setattr(transport, "time", clock)
    st = _st(_TimedEcu(clock, w1, latency))
    assert st.five_baud_init(0x10) == (0xEF, 0x8F)  # even at W1max plus USB latency
    assert 0.025 <= st.ser.w4 <= 0.050  # W4 on the wire


def test_five_baud_nothing_answers(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(transport, "time", clock)
    st = _st(_TimedEcu(clock, None, 0))
    with pytest.raises(NoResponse):
        st.five_baud_init(0x10)


def test_short_pid_reply_is_a_bad_reply():
    with pytest.raises(obd.BadReply, match="too short"):
        obd.read_pid(obd.Iso9141Link(_Chunks(_msg9141(0x41, 0x0C, 0x1A))), 0x0C)
    # a second ECU's full answer is used when the first one is short
    t = _Chunks(_msg9141(0x41, 0x0C, 0x1A, ecu=0x18) + _msg9141(0x41, 0x0C, 0x0D, 0x48))
    assert obd.read_pid(obd.Iso9141Link(t), 0x0C)[0] == 850


class _ShortMaf(MockCar):
    def _pid_bytes(self):
        return {**super()._pid_bytes(), 0x10: b"\x02"}


def test_live_skips_a_garbled_pid_and_keeps_session():
    car = Car(_ShortMaf(), mock=True)
    car.live()
    link = car.link
    for _ in range(25):
        snap = car.live()
    assert car.link is link and "maf" not in snap and 0x10 not in car._dead_pids


def _serve(car):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(car))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    return srv, http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)


def test_live_stream_reports_internal_errors_and_keeps_going(monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    car = Car(MockCar(), mock=True)
    car.live = lambda: 1 / 0  # a bug, not a K-line problem
    srv, c = _serve(car)
    try:
        c.request("GET", "/api/live")
        r = c.getresponse()
        events = [r.fp.readline() for _ in range(4)]  # two events: data + blank line each
        assert all(b'"kind": "internal"' in e for e in events[::2])
    finally:
        srv.shutdown()


def test_web_rejects_foreign_host():
    srv, c = _serve(Car(MockCar(), mock=True))
    try:
        c.request("GET", "/api/status", headers={"Host": "attacker.example"})
        assert c.getresponse().status == 403
        c.request("GET", "/api/status")  # http.client sends Host: 127.0.0.1:<port>
        assert c.getresponse().status == 200
    finally:
        srv.shutdown()


def test_lid_scan_survives_silent_lids(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    args = types.SimpleNamespace(addr=0x11, init="5baud", session=None, start=0x01, end=0x20)
    cli.cmd_lid_scan(args, MockCar(silent={b"\x21\x05"}))
    assert "21 10:" in capsys.readouterr().out  # swept past the silent LID
    cli.cmd_lid_scan(args, MockCar(silent={b"\x21\x03", b"\x21\x04", b"\x21\x05"}))
    out = capsys.readouterr().out
    assert "stopping at 21 05" in out and "--start 0x03" in out and "21 10:" not in out


def test_ident_survives_a_silent_option():
    car = Car(MockCar(silent={b"\x1a\x91"}), mock=True)
    assert car.ident()["vin"] == "WP0ZZZ99Z2SMOCK00"
    assert car.dme is not None
    every = {bytes([0x1A, o]) for o in (0x90, 0x9B, 0x91, 0x92, 0x94, 0x80)}
    with pytest.raises(NoResponse):
        Car(MockCar(silent=every), mock=True).ident()


def test_cli_id_survives_a_silent_option(capsys):
    args = types.SimpleNamespace(addr=0x11, init="5baud", session=None, options=[0x90, 0x91, 0x9B])
    cli.cmd_id(args, MockCar(silent={b"\x1a\x91"}))
    out = capsys.readouterr().out
    assert "1A 91: timeout" in out and "1A 9B:" in out


@pytest.mark.parametrize("data,text", [
    (b"WP0ZZZ99Z2SMOCK00\x00\x00", "WP0ZZZ99Z2SMOCK00"),
    (b"  0261207939\xff\xff", "0261207939"),
    (b"\x01\x02\xff", "0102ff"),
])
def test_id_text(data, text):
    assert id_text(data) == text


class _HalfAwake(_Scripted):
    def five_baud_init(self, a):
        raise KLineError("expected ~address 0xCC, got 4c")


class _Asleep(_Scripted):
    def five_baud_init(self, a):
        raise NoResponse("no 0x55 sync")


def test_connect_falls_back_to_fast_init_only_when_nothing_answered():
    with pytest.raises(KLineError, match="~address"):
        obd.connect(_HalfAwake([]))
    link, kb = obd.connect(_Asleep([(0.02, kwp2000.encode(b"\xC1\xEF\x8F", 0xF1, 0x10))]))
    assert isinstance(link, obd.KwpObdLink) and kb == (0xEF, 0x8F)


class _TimedChunks(QueueTransport):
    """One ECU answer 30 ms after each request, then silence, on a _Clock."""

    p3 = 0.055

    def __init__(self, clock):
        super().__init__()
        self.clock, self.answer = clock, None

    def write(self, data):
        self.answer = _msg9141(0x41, 0x0C, 0x0D, 0x48)

    def read_until_gap(self, gap, first_timeout):
        if self.answer:
            msg, self.answer = self.answer, None
            self.clock.now += 0.03 + gap  # P2, the message, then the gap that ends it
            return msg
        self.clock.now += first_timeout
        return b""


def test_iso9141_p3_counts_from_the_last_byte(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(obd, "time", clock)
    link = obd.Iso9141Link(_TimedChunks(clock))
    link.request(b"\x01\x0C")  # waits P3 after the handshake: expected
    clock.sleeps.clear()
    link.request(b"\x01\x0C")  # the 0.1 s wait for other ECUs already covered P3
    assert not [s for s in clock.sleeps if s > 0.001]


@pytest.mark.parametrize("vin,code", [
    ("WP0ZZZ98ZAUTEST00", None),  # MY2010: "A" is 2010, not 1980
    ("WP0ZZZ99ZVSTEST00", None),  # MY1997 "99": an air-cooled 993
    ("WP0ZZZ99ZWSTEST00", "996"),  # MY1998 996 (Europe)
    ("WP0ZZZ99Z5STEST00", "997"),  # MY2005: mostly 997
    ("WP0ZZZ98ZVUTEST00", "986"),
])
def test_model_from_vin_year_edges(vin, code):
    m = models.from_vin(vin)
    assert (m.code if m else None) == code


@pytest.mark.parametrize("c,year", [("V", 1997), ("Y", 2000), ("1", 2001), ("9", 2009), ("A", 2010)])
def test_vin_year_window(c, year):
    assert models.vin_year(f"WP0ZZZ98Z{c}UTEST00") == year


def test_cli_live_unknown_pid(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    cli.main(["--mock", "live", "--count", "1", "--pids", "0C,1D"])
    head, row = capsys.readouterr().out.splitlines()
    assert head == "t,rpm,pid_1d" and row.endswith(",850.00,")


def test_cli_bad_port_and_mock_sniff(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    with pytest.raises(SystemExit, match="cannot open /dev/cu.nothing"):
        cli.main(["--port", "/dev/cu.nothing", "--no-trace", "obd"])
    with pytest.raises(SystemExit, match="needs the cable"):
        cli.main(["--mock", "sniff"])
    assert not list(tmp_path.iterdir())  # no empty trace left behind


def test_mock_fast_init_only_where_an_ecu_is(capsys):
    cli.cmd_probe(types.SimpleNamespace(addrs=[0x33, 0x11, 0x12], init="fast", mock=True),
                  MockCar("kwp"))
    out = capsys.readouterr().out
    assert "0x33 fast: keybytes E9 8F" in out and "0x11 fast: keybytes EF 8F" in out
    assert "0x12 fast: timeout" in out


def test_dtc_request_format_matches_the_996():
    t = MockCar()
    c = kwp2000.Kwp2000Client(t, 0x11)
    c.connect_5baud()
    assert c.read_dtcs() == [(0x0300, 0xE0), (0x1128, 0x20)]  # 18 00 FF 00
    with pytest.raises(kwp2000.NegativeResponse) as e:
        c.read_dtcs(status=0x02)  # VW ME7's request: the 996 DME says NRC 12
    assert e.value.code == 0x12



# -- everything the car reports ----------------------------------------------------
@pytest.mark.parametrize("pid,data,value", [
    (0x01, "00076504", "MIL off, 0 stored, 1 of 7 monitors not ready"),
    (0x01, "82076500", "MIL on, 2 stored, 0 of 7 monitors not ready"),
    (0x03, "0200", "closed loop"),
    (0x03, "0102", "open loop (warming up) / closed loop"),
    (0x12, "04", "off"),
    (0x13, "33", "bank 1: S1 S2, bank 2: S1 S2"),
    (0x13, "03", "bank 1: S1 S2"),
    (0x14, "5a80", 0.45),
    (0x1C, "01", "OBD-II (CARB)"),
])
def test_status_and_sensor_decoders(pid, data, value):
    assert obd.PIDS[pid].decode(bytes.fromhex(data)) == value


class _Counting(MockCar):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent = []

    def write(self, data):
        self.sent.append(bytes(data[3:5]) if data[0] == 0x68 else b"")
        super().write(data)


def test_live_shows_every_reading_and_reads_statuses_once():
    t = _Counting()
    car = Car(t, mock=True)
    for _ in range(25):
        snap = car.live()
    assert set(snap) == ALL_996  # all 21 readings
    assert snap["monitor_status"].startswith("MIL off")
    for pid in STATIC_PIDS:  # statuses: once per minute, not every rotation
        assert t.sent.count(bytes([0x01, pid])) == 1


@pytest.mark.parametrize("tcu,settle", [(False, obd.SINGLE_ECU_SETTLE), (True, kwp2000.MULTI_GAP)])
def test_short_wait_only_when_one_ecu_answers(tcu, settle):
    car = Car(MockCar(tcu=tcu), mock=True)
    car.live()
    assert car.link.ecus == (2 if tcu else 1) and car.link.settle == settle


def test_live_log_has_one_line_per_sample():
    import io, json
    log = io.StringIO()
    car = Car(MockCar(), mock=True, live_log=log)
    for _ in range(3):
        car.live()
    lines = [json.loads(line) for line in log.getvalue().splitlines()]
    assert len(lines) == 3 and all("t" in r and r["rpm"] == 850 for r in lines)
    assert "obd_standard" in lines[0] and "obd_standard" not in lines[1]  # fresh values only


def test_cli_shows_text_readings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    cli.main(["--mock", "obd"])
    assert "monitor_status   MIL off, 0 stored, 1 of 7 monitors not ready" in capsys.readouterr().out
    cli.main(["--mock", "live", "--count", "1", "--pids", "0C,01"])
    row = capsys.readouterr().out.splitlines()[1]
    assert row.endswith(',850.00,"MIL off, 0 stored, 1 of 7 monitors not ready"')


def test_rpm_every_other_request_and_rotation_never_skips():
    t = _Counting()
    car = Car(t, mock=True)
    car.live()  # first sample also reads the statuses
    t.sent.clear()
    for _ in range(17):  # one full lap of the 17 rotating PIDs
        car.live()
    reqs = [r for r in t.sent if r]
    assert reqs[::2] == [b"\x01\x0c"] * 17  # rpm every other request
    rotated = [r[1] for r in reqs[1::2]]
    assert sorted(rotated) == sorted(set(rotated)) and len(rotated) == 17  # each once



def test_tabs_share_samples_and_the_bus_never_idles():
    import time as _time

    class _Slow(MockCar):  # each request takes 20 ms, like a (fast) real bus
        def write(self, data):
            _time.sleep(0.02)
            super().write(data)

    car = Car(_Slow(), mock=True)
    car.live()
    seq = car._seq
    car.live()
    assert car._seq == seq + 1  # one tab: back-to-back calls each take a fresh sample

    results = {}
    a = threading.Thread(target=lambda: results.setdefault("a", car.live()))
    b = threading.Thread(target=lambda: results.setdefault("b", car.live()))
    before = car._seq
    a.start()
    _time.sleep(0.01)  # B asks while A's sample is on the bus
    b.start()
    a.join(); b.join()
    assert car._seq == before + 1  # the car was asked once
    assert results["a"] == results["b"]  # and both tabs got that sample



# -- Porsche data blocks -------------------------------------------------------------
def test_dme_blocks_cycle_and_log():
    import io, json
    from pdiag.service import DME_LIDS
    log = io.StringIO()
    car = Car(MockCar(), mock=True, dme_log=log)
    for _ in range(len(DME_LIDS)):
        msg = car.dme_live()
    assert sorted(msg["blocks"]) == sorted(f"{x:02x}" for x in DME_LIDS)
    assert len(msg["blocks"]["09"]) == 149 * 2  # same size as the real 996 block
    lines = [json.loads(line) for line in log.getvalue().splitlines()]
    assert [r["lid"] for r in lines] == [f"{x:02x}" for x in DME_LIDS]


def test_dme_block_the_ecu_lacks_is_dropped(monkeypatch):
    from pdiag import service
    monkeypatch.setattr(service, "DME_LIDS", (0x04, 0x99))
    car = Car(MockCar(), mock=True)
    msgs = [car.dme_live() for _ in range(4)]
    assert 0x99 in car._dead_lids and msgs[-1]["new"] == "04"


def test_dme_stream_over_http():
    srv, c = _serve(Car(MockCar(), mock=True))
    try:
        c.request("GET", "/api/dme-live")
        r = c.getresponse()
        line = r.fp.readline()
        assert b'"blocks"' in line and b'"new": "01"' in line
    finally:
        srv.shutdown()
