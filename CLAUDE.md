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
- `transport.py` — K-line byte I/O on a KKL cable: echo check, 5-baud and fast init via serial
  BREAK, `drain()` of late bytes, `TracingTransport` (JSONL trace), `QueueTransport` (mock
  base). The pyserial timeout is set once (assigning it reconfigures the UART); waits use select().
- `kwp2000.py` — ISO 14230 framing (encode/decode/split, checksum), `Kwp2000Client` with
  read-only services (0x10, 0x1A, 0x18, 0x21, 0x3E, 0x82), NRC handling incl. 0x78 pending,
  multi-ECU collection for functional requests.
- `obd.py` — generic OBD-II over ISO 9141-2 or KWP2000; decoders for every PID a 2002 996
  reports (incl. readiness and status texts); mode 03 DTCs.
- `models.py` — model profiles (986/996/987.1/997.1) and VIN → model detection. DME address
  0x11 is verified on the 996; the other DME values are placeholders.
- `mock.py` — simulated car shaped like the real 996 (PID bitmap, keybytes, addresses, block
  sizes); values are **fake placeholders**. `tcu=True` adds a second ECU, `silent=` drops replies.
- `cli.py` — `pdiag` commands (see README).
- `service.py` — `Car`: one K-line, a lock, session switching between generic OBD (0x33) and
  the DME (0x11) with a 5.5 s settle. `live()`: rpm every other request plus a rotation of
  every supported PID (statuses once a minute), shared by all tabs. `dme_live()`: the DME's
  data blocks, round-robin. Both log JSONL.
- `web.py` + `static/` — `pdiag ui`: stdlib HTTP server, JSON API, SSE `/api/live` and
  `/api/dme-live`, Host check. Vanilla HTML/CSS/JS with no build step. The over-rev report is
  mock-only until decoded (501 on hardware).
Tests: `uv run pytest` (all against the mock).

## UI design (keep consistent)
Instrument, not dashboard. The one bold element is the 996-style tach with the six over-rev
ranges painted on its rim, linking live rpm to stored history. Everything else stays quiet:
hairline rules, no card grids, no shadows. Palette: pale seal-grey paper `#E3E5E0`, graphite
ink, dark gauge face, 996 orange needle `#F25C19`, amber for caution, red for serious, teal
for OK. Type: Archivo only, using its width axis (expanded headings, condensed numerals).
Copy: plain sentences, the verdict first. Units default to US (°F, mph) with a toggle.
Live: the needle eases between readings; under it, a quiet list of everything the engine
computer reports, whose rows never jump. Porsche data: raw DME blocks as hexdump rows, static
bytes dim, bytes that moved in ink, the latest change amber.

## Status (2026-10-15)
- Phase 0 plus three review rounds done (details in git history).
- **First car session 2026-10-15 (2002 996 Targa, US, ME7.8): it works.** Generic OBD over
  ISO 9141-2 (0x33), 21 live readings; DME over KWP2000 at 0x11: identification, fault codes
  (`18 00 FF 00`), 14 data blocks. Live UI on the car: ~7 requests/s, needle ~3.5 updates/s.
  Facts in docs/PROTOCOL.md (Findings), cable notes in docs/HARDWARE.md.
- Fixed on the car: pyserial's `timeout` setter reconfigured the UART on every read and
  corrupted bytes.
- Lessons: confirm the dash lights and check-engine lamp are on before a run (accessory
  position gives a silent car). Plug OBD first, then USB, or the FT232R can wedge
  (`tcsetattr: Invalid argument`) until USB is replugged.
- Over-rev counters: not decoded.

## Next steps
1. **The gate:** decode the over-rev counters (ranges 1–6). Best route: sniff a Durametric
   session (`pdiag sniff`, our cable on an OBD Y-splitter) with notes of what it shows when.
   Fallbacks: LID 09 (149 bytes, starts with six equal bytes — an unverified lead), LID 10
   (general reject: try `--session 0x81` / `0x89`), memory read (0x23, read-only).
2. Decode the live bytes in LIDs 04/05 by correlating `logs/*-dme.jsonl` with throttle and
   rpm changes (one variable at a time).
3. Only after the gate: phone app + BLE dongle (ESP32 + L9637).

## Open questions
- Where the over-rev and misfire counters live (a LID? memory read?).
- What session LID 10 wants; what identification option 9B (16 digits) means.
- DTC status semantics on ME7.8 (no stored codes to test with yet).
- 987.1/997.1: same DME address and services as the 996? US MY2008: generic OBD on CAN only?
- The page's live stream closed itself once when the cable came back; no repeat since.

## Conventions
- Small modules, no frameworks. Match existing style. Hex in logs lower-case, no spaces.
- New hardware commands must go through `open_transport` so they're traced.
- Update **Status / Next steps / Open questions** here at the end of each work session,
  and add findings to `docs/PROTOCOL.md`.
