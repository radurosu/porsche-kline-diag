import pytest

from pdiag import cli, kwp2000, models, obd
from pdiag.mock import MockCar
from pdiag.service import ROTATING_PIDS, Car
from pdiag.transport import KLineError, QueueTransport, SerialTransport


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
    assert obd.supported_pids(link) == [4, 5, 6, 7, 0xC, 0xD, 0xE, 0xF, 0x10, 0x11]
    assert obd.read_pid(link, 0x0C)[0] == 850
    assert obd.read_pid(link, 0x05)[0] == 88
    assert obd.read_stored_dtcs(link) == ["P0300", "P0420"]


def test_dme_services():
    c = kwp2000.Kwp2000Client(MockCar(), 0x10)
    assert c.connect_5baud() == (0xEF, 0x8F)
    assert c.read_ecu_id(0x9B).startswith(b"MOCK")
    assert c.read_dtcs() == [(0x0300, 0xE0), (0x1128, 0x20)]
    assert c.read_local_id(0xEE) == b"\x01\x02"  # after responsePending
    with pytest.raises(kwp2000.NegativeResponse) as e:
        c.read_local_id(0x99)
    assert e.value.code == 0x31


def test_fast_init():
    c = kwp2000.Kwp2000Client(MockCar(), 0x10)
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
    assert obd.supported_pids(link) == [4, 5, 6, 7, 0xC, 0xD, 0xE, 0xF, 0x10, 0x11]
    for _ in range(3):  # no stale second reply leaks into the next request
        assert obd.read_pid(link, 0x0C)[0] == 850
        assert obd.read_pid(link, 0x05)[0] == 88
    assert obd.read_stored_dtcs(link) == ["P0300", "P0420"]


class _Chunks(QueueTransport):
    def write(self, data):
        pass


def test_iso9141_reassembles_split_message():
    t = _Chunks()
    body = bytes([0x48, 0x6B, 0x10, 0x41, 0x0C, 0x0D, 0x48])
    msg = body + bytes([kwp2000.checksum(body)])
    t.push(msg[:4])
    t.push(msg[4:])
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
    car._sample_t = 0  # past the shared-sample window
    car.live()
    assert car.link is not link


# -- serial transport (fake FTDI) ---------------------------------------------
class FakeSerial:
    def __init__(self, after_reset: bytes, addr: int, collide: bool = False):
        self.rx = bytearray()
        self.after_reset, self.addr, self.collide = after_reset, addr, collide
        self.break_condition = False
        self.timeout = 0.1
        self.port = "/dev/cu.fake"

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


def _serial(monkeypatch, fake):
    from pdiag import transport
    monkeypatch.setattr(transport, "_sleep_until", lambda d: None)
    monkeypatch.setattr(transport.time, "sleep", lambda s: None)
    st = SerialTransport.__new__(SerialTransport)
    st.ser, st.echo, st.p4 = fake, True, 0
    return st


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

    def read(self, n, timeout):
        if not self.rx and self.events and self.events[0][0] <= timeout:
            self.push(self.events.pop(0)[1])
        return super().read(n, timeout)


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
    t = _Chunks()
    body = bytes([0x48, 0x6B, 0x10, 0x7F, 0x03, 0x22])
    t.push(body + bytes([kwp2000.checksum(body)]))
    with pytest.raises(KLineError, match="mode 03 rejected"):
        obd.read_stored_dtcs(obd.Iso9141Link(t))


@pytest.mark.parametrize("payload", [b"\x58\x02", b"\x58\x02\x03\x00\xE0\x11"])
def test_short_dtc_reply_is_an_error(payload):
    t = _Scripted([(0, kwp2000.encode(payload, 0xF1, 0x10))])
    with pytest.raises(KLineError, match="shorter than its count"):
        kwp2000.Kwp2000Client(t, 0x10).read_dtcs()


def test_live_clients_each_get_full_snapshot():
    car = Car(MockCar(), mock=True)
    names = set(ROTATING_PIDS.values()) | {"rpm"}
    seen = []
    for _ in range(12):  # two tabs taking turns
        car._sample_t = 0
        seen.append(car.live())
    assert set(seen[-1]) == names and set(seen[-2]) == names
