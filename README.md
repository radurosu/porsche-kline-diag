# pdiag

Open, modern diagnostics for K-line era water-cooled Porsches
(986 Boxster, 996, 997.1, 987.1, Cayenne 955 — roughly 1997–2008).
Status: **phase 0** — protocol stack + CLI, tested against a simulated car.
Read-only by design.

## Setup (macOS)

```sh
uv sync
uv run pdiag --mock ui           # web UI on http://127.0.0.1:8996, simulated car
uv run pdiag --mock obd          # same simulated car, command line
uv run pytest
```

Hardware: FTDI-based **KKL** USB cable (VAG-COM 409.1 style). macOS has a
built-in FTDI driver; the cable shows up as `/dev/cu.usbserial-XXXX`.
See [docs/HARDWARE.md](docs/HARDWARE.md).

## Commands

| Command | What |
|---|---|
| `pdiag ports` | List serial ports, flag the KKL cable |
| `pdiag probe [--addrs 33,10,11] [--init both]` | Wake each address (5-baud and/or fast init), print keybytes |
| `pdiag obd` | Generic OBD-II: protocol, supported PIDs, values, stored DTCs |
| `pdiag live [--pids 0C,05,0E]` | Stream PIDs as CSV |
| `pdiag id --addr 0x10` | DME identification (KWP 0x1A) |
| `pdiag dtc --addr 0x10` | DME fault codes (KWP 0x18) |
| `pdiag lid-scan --addr 0x10` | Sweep local identifiers (KWP 0x21) → `logs/` |
| `pdiag ui` | Web UI: live tach, over-rev report, fault codes, car info |
| `pdiag sniff` | Passive K-line capture (e.g. a Durametric session) |

Global: `--port /dev/cu.usbserial-…` (or `PDIAG_PORT`), `--model 986|996|987|997` (default: detected
from the VIN), `--mock` (+ `--mock-generic kwp`; `--model` picks the simulated car), `--no-trace`. Every hardware run writes a byte-level trace to `logs/`.

## Docs

- [CLAUDE.md](CLAUDE.md) — project context, decisions, status, next steps
- [docs/PROTOCOL.md](docs/PROTOCOL.md) — K-line / KWP2000 notes, open questions
- [docs/HARDWARE.md](docs/HARDWARE.md) — cable, wiring, sniffing setup

## Contributing

The most valuable contribution is data: a `pdiag sniff` capture of another tool reading a
car, plus notes on what that tool was showing at each moment. Decoding is clean-room only,
so please don't submit material from leaked dealer software or proprietary tool internals.
Scrub VINs from captures before sharing.

## Disclaimer

Not affiliated with, endorsed by, or connected to Dr. Ing. h.c. F. Porsche AG, Robert Bosch
GmbH, or Durametric. Brand names are used only to describe compatibility. pdiag is read-only
by design, but you use it on your own car at your own risk.

## Credits

pdiag is a project by [Eloquentix](https://eloquentix.com).

## License

[GPL-3.0-or-later](LICENSE).
