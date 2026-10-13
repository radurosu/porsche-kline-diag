import pytest

from pdiag import cli, kwp2000, obd
from pdiag.mock import MockCar
from pdiag.transport import KLineError


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
    ["probe"], ["obd"], ["--mock-generic", "kwp", "obd"], ["live", "--count", "2"],
    ["id"], ["dtc"], ["lid-scan", "--end", "0x12"],
])
def test_cli_smoke(argv, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    cli.main(["--mock"] + argv)
    assert capsys.readouterr().out
    assert list(tmp_path.glob("*.jsonl"))
