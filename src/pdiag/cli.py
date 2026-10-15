"""pdiag command line. Every hardware command writes a JSONL trace to logs/."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from . import kwp2000, models, obd
from .transport import KLineError, NoResponse, SerialTransport, TracingTransport

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def _parse_int(s: str) -> int:
    return int(s, 0)


def _hexlist(s: str) -> list[int]:
    return [int(x, 16) for x in s.split(",") if x]


def open_transport(args, cmd: str):
    if args.mock:
        from .mock import MockCar
        t = MockCar(generic=args.mock_generic, dynamic=cmd == "ui",
                    model=args.model or "996")
    else:
        port = args.port or os.environ.get("PDIAG_PORT") or _guess_port()
        try:
            t = SerialTransport(port)
        except OSError as e:  # serial.SerialException: missing, unplugged or busy
            sys.exit(f"cannot open {port}: {e}\n(is another pdiag, e.g. `pdiag ui`, "
                     "using the cable?)")
        print(f"port: {port}", file=sys.stderr)
    if args.no_trace or (args.mock and cmd == "ui"):
        return t
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{cmd}.jsonl"
    print(f"trace: {path}", file=sys.stderr)
    return TracingTransport(t, str(path))


def _guess_port() -> str:
    from serial.tools import list_ports
    cands = [p.device for p in list_ports.comports()
             if "usbserial" in p.device and p.device.startswith("/dev/cu.")]
    if len(cands) != 1:
        sys.exit(f"cannot pick port automatically ({cands or 'no usbserial device'}); "
                 "use --port or PDIAG_PORT")
    return cands[0]


def _dme(args, t) -> kwp2000.Kwp2000Client:
    c = kwp2000.Kwp2000Client(t, args.addr)
    kb = c.connect_fast() if args.init == "fast" else c.connect_5baud()
    print(f"connected 0x{args.addr:02X} keybytes {kb[0]:02X} {kb[1]:02X}")
    if args.session is not None:
        r = c.start_session(args.session)
        print(f"session 0x{args.session:02X}: {r.hex()}")
    return c


# -- commands ----------------------------------------------------------------
def cmd_ports(args):
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.device.startswith("/dev/cu."):
            mark = "  <- likely KKL" if "usbserial" in p.device else ""
            print(f"{p.device}  {p.description}{mark}")


def cmd_probe(args, t):
    from .service import SESSION_TIMEOUT
    inits = ["5baud", "fast"] if args.init == "both" else [args.init]
    for a in args.addrs:
        for init in inits:
            try:
                if init == "5baud":
                    kb = t.five_baud_init(a)
                else:
                    kb = kwp2000.Kwp2000Client(t, a, functional=a == obd.OBD_ADDR).connect_fast()
                print(f"0x{a:02X} {init}: keybytes {kb[0]:02X} {kb[1]:02X}"
                      + ("  (KWP2000)" if kb[1] == kwp2000.KWP_KB2 else ""))
                awake = True
            except NoResponse as e:
                print(f"0x{a:02X} {init}: {e}")
                awake = False
            except KLineError as e:
                # Something answered (a rejection, a partial handshake): the ECU
                # is awake even though the wake-up failed.
                print(f"0x{a:02X} {init}: {e}")
                awake = True
            if not args.mock:
                # An ECU that woke stays in session until P3max passes.
                time.sleep(SESSION_TIMEOUT if awake else 0.5)


def cmd_obd(args, t):
    link, kb = obd.connect(t, args.init)
    print(f"protocol: {link.name}  keybytes {kb[0]:02X} {kb[1]:02X}")
    try:
        pids = obd.supported_pids(link)
        print("supported PIDs:", " ".join(f"{p:02X}" for p in pids))
    except KLineError as e:
        pids = sorted(obd.PIDS)
        print(f"supported PIDs: unknown ({e}); trying every known PID")
    # One run should yield as much as possible: a failed PID doesn't stop the rest.
    for p in pids:
        if p in obd.PIDS:
            try:
                v, raw = obd.read_pid(link, p)
                shown = v if isinstance(v, str) else f"{v:9.2f} {obd.PIDS[p].unit}"
                print(f"  {obd.PIDS[p].name:16s} {shown}")
            except KLineError as e:
                print(f"  {obd.PIDS[p].name:16s} {e}")
    print("stored DTCs:", ", ".join(obd.read_stored_dtcs(link)) or "none")


def cmd_live(args, t):
    link, _ = obd.connect(t, args.init)
    names = [obd.PIDS[p].name if p in obd.PIDS else f"pid_{p:02x}" for p in args.pids]
    out = csv.writer(sys.stdout, lineterminator="\n")  # status texts contain commas
    out.writerow(["t", *names])
    t0 = time.perf_counter()
    n = 0
    try:
        while args.count == 0 or n < args.count:
            vals = []
            for p in args.pids:  # a PID that fails leaves its column empty
                try:
                    v = obd.read_pid(link, p)[0]
                    vals.append(v if isinstance(v, str) else f"{v:.2f}")
                except (NoResponse, kwp2000.NegativeResponse, obd.BadReply):
                    vals.append("")
            if not any(vals):
                raise KLineError("no PID answered; is the engine computer still awake?")
            out.writerow([f"{time.perf_counter() - t0:.2f}", *vals])
            sys.stdout.flush()
            n += 1
    except KeyboardInterrupt:
        pass


def cmd_id(args, t):
    c = _dme(args, t)
    for opt in args.options:
        try:
            d = c.read_ecu_id(opt)
            txt = d.decode("ascii", "replace")
            print(f"1A {opt:02X}: {d.hex()}  |{txt}|")
        except (kwp2000.NegativeResponse, NoResponse) as e:
            print(f"1A {opt:02X}: {e}")
    c.stop()


def cmd_dtc(args, t):
    c = _dme(args, t)
    dtcs = c.read_dtcs()
    for code, status in dtcs:
        print(f"{code:04X}  ({obd.dtc_str(code)})  status {status:02X}")
    print(f"{len(dtcs)} DTC(s)")
    c.stop()


def cmd_lid_scan(args, t):
    c = _dme(args, t)
    out = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-lids.jsonl"
    LOG_DIR.mkdir(exist_ok=True)
    misses = 0  # requests in a row with no usable answer
    with open(out, "w") as f:
        # No testerPresent needed: every request keeps the session alive.
        for lid in range(args.start, args.end + 1):
            try:
                d = c.read_local_id(lid)
                rec = {"lid": f"{lid:02x}", "hex": d.hex()}
                print(f"21 {lid:02X}: {d.hex()}")
                misses = 0
            except kwp2000.NegativeResponse as e:
                rec = {"lid": f"{lid:02x}", "nrc": f"{e.code:02x}"}
                misses = 0
            except KLineError as e:  # silence or garbled: note it, keep sweeping
                rec = {"lid": f"{lid:02x}", "error": str(e)}
                misses += 1
            f.write(json.dumps(rec) + "\n")
            if misses == 3:
                print(f"no answer to 3 LIDs in a row, stopping at 21 {lid:02X}. "
                      f"Rerun with --start 0x{lid - 2:02X} once the ECU answers again.")
                break
    print(f"results: {out}")
    c.stop()


def cmd_sniff(args, t):
    """Passive: never transmits. Splits traffic on inter-byte gaps."""
    LOG_DIR.mkdir(exist_ok=True)
    out = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-sniff.jsonl"
    print(f"sniffing -> {out} (Ctrl-C to stop)")
    t0 = time.perf_counter()
    rest = b""  # partial frame carried into the next chunk
    with open(out, "w") as f:
        try:
            while True:
                chunk = t.read_until_gap(gap=args.gap, first_timeout=1.0)
                if not chunk:
                    continue
                rec = {"t": round(time.perf_counter() - t0, 4), "hex": chunk.hex()}
                frames, skipped, rest = kwp2000.split_stream(rest + chunk)
                if len(rest) > 260:  # longer than any KWP frame: not a frame start
                    skipped, rest = skipped + rest, b""
                if frames:
                    rec["kwp"] = [{"tgt": fr.target, "src": fr.source,
                                   "data": fr.data.hex()} for fr in frames]
                if skipped:
                    rec["skipped"] = skipped.hex()
                f.write(json.dumps(rec) + "\n")
                print(rec["t"], chunk.hex(" "))
        except KeyboardInterrupt:
            pass


def cmd_ui(args, t):
    from .service import Car
    from .web import serve
    port = None if args.mock else getattr(getattr(t, "inner", t), "ser", None).port
    model = models.MODELS[args.model] if args.model else None
    logs = {}
    if not args.mock:  # decoded live samples and raw DME blocks, next to the byte trace
        stamp = time.strftime('%Y%m%d-%H%M%S')
        for kind in ("live", "dme"):
            path = LOG_DIR / f"{stamp}-{kind}.jsonl"
            logs[kind] = open(path, "a", buffering=1)
            print(f"{kind} log: {path}", file=sys.stderr)
    try:
        serve(Car(t, model=model, dme_addr=args.addr, mock=args.mock, port=port,
                  label=args.car, live_log=logs.get("live"), dme_log=logs.get("dme")),
              port=args.http_port, open_browser=not args.no_browser)
    finally:
        for f in logs.values():
            f.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pdiag", description=__doc__)
    ap.add_argument("--port", help="serial device, e.g. /dev/cu.usbserial-A50285BI")
    ap.add_argument("--mock", action="store_true", help="use simulated car, no hardware")
    ap.add_argument("--mock-generic", choices=["iso9141", "kwp"], default="iso9141",
                    help="generic OBD protocol the simulated car speaks")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--model", choices=sorted(models.MODELS),
                    help="car model (default: detect from the VIN; the mock simulates a 996)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list serial ports")

    p = sub.add_parser("probe", help="wake a list of addresses (5-baud and/or fast init)")
    p.add_argument("--addrs", type=_hexlist, default=[0x33, 0x10, 0x11, 0x01, 0x12])
    p.add_argument("--init", choices=["5baud", "fast", "both"], default="5baud")

    for name in ("obd", "live"):
        p = sub.add_parser(name, help="generic OBD summary" if name == "obd"
                           else "stream PIDs as CSV")
        p.add_argument("--init", choices=["auto", "5baud", "fast"], default="auto")
        if name == "live":
            p.add_argument("--pids", type=_hexlist, default=[0x0C, 0x05, 0x0E, 0x11])
            p.add_argument("--count", type=int, default=0, help="0 = until Ctrl-C")

    for name, hlp in (("id", "DME identification (0x1A)"),
                      ("dtc", "DME fault codes (0x18)"),
                      ("lid-scan", "sweep readDataByLocalIdentifier (0x21)")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--addr", type=_parse_int, default=0x11,
                       help="DME K-line address (0x11 on a 2002 996 ME7.8)")
        p.add_argument("--init", choices=["5baud", "fast"], default="5baud")
        p.add_argument("--session", type=_parse_int, default=None,
                       help="startDiagnosticSession mode, e.g. 0x81 or 0x89")
        if name == "id":
            p.add_argument("--options", type=_hexlist,
                           default=[0x80, 0x86, 0x8A, 0x90, 0x91, 0x92, 0x94, 0x9B])
        if name == "lid-scan":
            p.add_argument("--start", type=_parse_int, default=0x01)
            p.add_argument("--end", type=_parse_int, default=0xFF)

    p = sub.add_parser("ui", help="open the local web interface")
    p.add_argument("--addr", type=_parse_int, default=None,
                   help="DME K-line address (default: the model's, 0x11)")
    p.add_argument("--car", default=None, help="name shown in the header, e.g. '996 Targa'")
    p.add_argument("--http-port", type=int, default=8996)
    p.add_argument("--no-browser", action="store_true")

    p = sub.add_parser("sniff", help="passively log K-line traffic (e.g. Durametric)")
    p.add_argument("--gap", type=float, default=0.02, help="frame split gap, seconds")

    args = ap.parse_args(argv)
    if args.cmd == "ports":
        return cmd_ports(args)
    if args.cmd == "sniff" and args.mock:
        sys.exit("sniff listens to a real K-line: it needs the cable, not --mock")

    t = open_transport(args, args.cmd)
    if args.cmd == "sniff" and isinstance(getattr(t, "inner", t), SerialTransport):
        getattr(t, "inner", t).echo = False
    fn = {"probe": cmd_probe, "obd": cmd_obd, "live": cmd_live, "id": cmd_id,
          "dtc": cmd_dtc, "lid-scan": cmd_lid_scan, "sniff": cmd_sniff, "ui": cmd_ui}[args.cmd]
    try:
        fn(args, t)
    except KLineError as e:
        sys.exit(f"error: {e}")
    finally:
        t.close()


if __name__ == "__main__":
    main()
