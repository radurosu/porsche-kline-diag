# Protocol notes

Facts below are from public standards (ISO 9141-2, ISO 14230, SAE J1979) unless marked.
Porsche-specific findings go in the **Findings** table with their source.

## Physical layer (K-line)
- Single wire, OBD pin 7, 12 V logic, half duplex, 10400 baud 8N1.
- The tester hears its own bytes (echo). The stack reads and verifies each echoed byte.
- 2002 996: no CAN. (L-line, pin 15, is usually unused.)

## Wakeup
**5-baud init** (ISO 9141-2 and ISO 14230):
1. Line idle high ≥ 300 ms.
2. Tester sends the address byte at 5 baud (200 ms/bit): start, 8 bits LSB first, stop.
3. ECU → `0x55` sync (W1 60–300 ms), then KB1, KB2.
4. Tester waits W4 (25–50 ms), sends `~KB2`.
5. ECU sends `~address`.

Keybytes: `08 08` / `94 94` = ISO 9141-2; KB2 `8F` = KWP2000.
Generic OBD address is `0x33`.

**Fast init** (ISO 14230 only): 25 ms low, 25 ms high, then StartCommunication `0x81`
(e.g. `81 10 F1 81 03`). Response `C1 KB1 KB2`.

Implemented via serial BREAK toggling (`transport.py`). Unverified on macOS hardware.

## KWP2000 frame
`[fmt][tgt][src]([len]) data… [cs]`
- fmt bits 7-6: `00` no address, `10` physical, `11` functional, `01` CARB.
- fmt bits 5-0: data length 1–63, or `0` → separate length byte.
- cs = sum of the preceding bytes mod 256. Tester address `F1`.
- Timing: P1 (ECU inter-byte) ≤ 20 ms, P2 (response delay) 25–50 ms,
  P3 (between requests) ≥ 55 ms, P4 (tester inter-byte) 0–20 ms, default 5 ms here.
- Positive response = SID + 0x40. Negative = `7F SID NRC`; NRC `78` = response pending.

## ISO 9141-2 OBD frame
Request `68 6A F1 <data> cs`, response `48 6B <ecu> <data> cs`. No length byte;
messages are split on timing gaps.

## Read-only services used
| SID | Name | Notes |
|---|---|---|
| 10 | startDiagnosticSession | mode 81 default; 89 seen on VAG ME7 (try if needed) |
| 1A | readEcuIdentification | options 80–9F; which ones the DME supports is unknown |
| 18 | readDTCByStatus | `18 02 FF 00` = all DTCs |
| 21 | readDataByLocalIdentifier | Porsche-specific data expected here |
| 3E | testerPresent | keepalive |
| 82 | stopCommunication | |

Excluded on purpose (write/unsafe): 14, 27, 2E, 2F, 30, 31, 34–37, 3B.

## Over-rev counters — the gate
The DME stores ignition counts and timestamps (operating hours) for 6 over-rev ranges.
Location and encoding are **unknown**. Plan:
1. Sniff Durametric reading them (`pdiag sniff`) → the exact request/response pair.
2. Fallback: `pdiag lid-scan`, correlate responses with known engine state.
3. Fallback: memory read (`0x23`, read-only) if the ECU permits it.

## Models
`src/pdiag/models.py`. Model is detected from the VIN (public layout): WMI `WP0`, positions
7-8 `98` = Boxster/Cayman, `99` = 911, position 10 = model year. 98 + ≤2004 → 986,
98 + 2005–08 → 987.1, 99 + ≤2004 → 996, 99 + 2005–08 → 997.1. MY2005 `99` is ambiguous
(996 Turbo/GT2/GT3 overlap) → `--model 996`. MY2009+ (987.2/997.2) is out of scope.
Per-model DME address, redline and over-rev boundaries are **placeholders**, identical for
all models until a capture of each says otherwise.

987.1 / 997.1 risk: US law required CAN (ISO 15765-4) for generic OBD from MY2008. A US
MY2008 car may answer generic OBD (`pdiag obd`, Live view) only on CAN, which a KKL cable
can't reach, even if its DME still talks K-line. Unverified; first 987 trace settles it.

## Findings
| Date | Item | Value | Source |
|---|---|---|---|
| — | — | — | — |
