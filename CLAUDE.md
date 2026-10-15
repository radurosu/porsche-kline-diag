# CLAUDE.md — pdiag (Porsche K-line diagnostics)

Start here every session. Then read `docs/` as needed and the latest files in `logs/`.

## What this is
Open, modern diagnostic tool for K-line era water-cooled Porsches (986/996/997.1/987.1/955,
~1997–2008). Goal: a genuinely good alternative to **Durametric** (the incumbent owner tool;
dated Windows UI, VIN licensing). Dealer tools for reference: **PIWIS** (current) and **PST2**
(original 996-era tester). Open source, GPL-3.0-or-later. Repo:
https://github.com/radurosu/porsche-kline-diag
Product/business strategy is private and lives outside the repo at `../porsche-private/STRATEGY.md`
(local only). Never copy it into this repo.

## Owner / test car
Radu Rosu. Test car: **2002 Porsche 996 Targa** (US). Engine ECU: Bosch
**ME7.8** (DME). Diagnostic bus: **K-line only** (no CAN). Radu runs commands on the car and
sends back the `logs/*.jsonl` traces; Claude can't touch hardware — design every hardware
step so one run produces enough trace data to debug from.

## Hard rules
1. **Read-only.** No services that write, code, adapt, flash or clear (no 0x14, 0x2E, 0x3B,
   0x31 routines, 0x34+ download, 0x27 security access). Clearing DTCs is excluded on
   purpose too: it erases evidence, and for a pre-purchase product it's a fraud vector.
   Revisit only with an explicit decision recorded here.
2. **Clean-room.** Decode only from our own captures/experiments. Never use leaked PIWIS
   data, Durametric binaries/tables, or other proprietary material. Record the source of
   each decoded identifier in `docs/PROTOCOL.md`.
3. **Traces are private.** `logs/` is gitignored (VINs, ECU IDs). Scrub before publishing.
4. Runs on **macOS** (Radu's machine). Python 3.11+, `uv`, pyserial. No Windows-only deps.

## Architecture (`src/pdiag/`)
- `transport.py` — K-line byte I/O on a KKL cable: echo removal, 5-baud init and fast init
  via serial BREAK, `TracingTransport` (JSONL trace), `QueueTransport` (mock base).
- `kwp2000.py` — ISO 14230 framing (encode/decode/split, checksum), `Kwp2000Client` with
  read-only services (0x10, 0x1A, 0x18, 0x21, 0x3E, 0x82), NRC handling incl. 0x78 pending.
- `obd.py` — generic OBD-II over ISO 9141-2 or KWP2000; PID decoders; mode 03 DTCs.
- `models.py` — model profiles (986/996/987.1/997.1) and VIN → model detection. DME values
  per model are placeholders.
- `mock.py` — simulated car (`--mock`, `--model` picks which). Values are **fake placeholders**,
  not real decodes. `tcu=True` adds a second ECU answering generic OBD.
- `cli.py` — `pdiag` commands (see README).
- `service.py` — `Car`: one K-line, a lock, and switching between the generic OBD session
  and the DME session (5.5 s settle on hardware). Live streaming yields to one-shot requests.
  Live reads rpm every sample and rotates the other PIDs.
- `web.py` + `static/` — `pdiag ui`: stdlib HTTP server, JSON API, SSE `/api/live`. Vanilla
  HTML/CSS/JS with no build step. The over-rev report is mock-only until decoded (501 on hardware).
Tests: `uv run pytest` (all against the mock).

## UI design (keep consistent)
Instrument, not dashboard. The one bold element is the 996-style tach with the six over-rev
ranges painted on its rim, linking live rpm to stored history. Everything else stays quiet:
hairline rules, no card grids, no shadows. Palette: pale seal-grey paper `#E3E5E0`, graphite
ink, dark gauge face, 996 orange needle `#F25C19`, amber for caution, red for serious, teal
for OK. Type: Archivo only, using its width axis (expanded headings, condensed numerals).
Copy: plain sentences, the verdict first. Units default to US (°F, mph) with a toggle.

## Status (2026-10-14)
- Phase 0 done: stack + CLI + mock + tests. **Never run on real hardware yet.**
- Web UI (`pdiag ui`) done against the mock (2026-10-13). Over-rev range boundaries and all
  mock values are placeholders.
- 2026-10-14: 987.1 support (model profiles, VIN detection, `--model`). Bug fixes before
  first hardware run: P4 timing in `write` (FTDI latency), junk before 5-baud sync,
  multi-ECU OBD replies (stale replies leaked into later requests), split ISO 9141
  messages, unbounded request loop, unsupported PIDs/NRCs killing the live session,
  sessions not reopened after P3max idle, probe waited 2.5 s < P3max, sniff splitter
  resync across chunks.
- Cable: ordered 2026-10-13, KKL 409.1 FT232RL (Amazon B0DG2NCJ4Z). Not yet tested. On arrival,
  check that it enumerates as FTDI (vendor 0x0403) and shows up as `/dev/cu.usbserial-*`.

## Next steps
1. Get the KKL FTDI cable → `pdiag ports` → `pdiag obd` (engine running). Proves cable,
   driver, BREAK-based 5-baud timing on macOS, and the generic protocol (9141 vs KWP).
2. `pdiag probe --init both` → find the DME's K-line address, init type and keybytes.
3. `pdiag id` / `pdiag dtc` against the DME; try `--session 0x81` / `0x89` if rejected.
4. **The gate:** read the over-rev counters (ranges 1–6). Best route: sniff a real
   Durametric session (`pdiag sniff`, second cable on an OBD splitter) and decode the
   request/response pairs. Fallback: `pdiag lid-scan` + correlation, or ME7 RAM logging.
5. Only after the gate: phone app + BLE dongle (ESP32 + L9637).

## Open questions
- DME K-line address on 996 ME7.8 (0x10 is a guess) and whether it wants 5-baud or fast init.
- Required diagnostic session mode for Porsche-specific reads.
- Where the over-rev counters / misfire counters live (local ID? memory read?).
- Whether macOS BREAK timing is accurate enough for fast init (25 ms ±1).
- Does the US 2002 996 speak ISO 9141-2 or KWP2000 for generic OBD?
- 987.1/997.1: same DME address and services as the 996? US MY2008: generic OBD on CAN only?
- 5-baud address parity: 0x33 sent as plain 8N1; ISO 9141 may expect 7O1 (0xB3). Try if no sync.

## Conventions
- Small modules, no frameworks. Match existing style. Hex in logs lower-case, no spaces.
- New hardware commands must go through `open_transport` so they're traced.
- Update **Status / Next steps / Open questions** here at the end of each work session,
  and add findings to `docs/PROTOCOL.md`.
