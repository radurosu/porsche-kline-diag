# Hardware

## Cable (buy this)
FTDI-based **KKL** USB cable (VAG-COM 409.1 style, FT232RL chip), ~$20.
- macOS has a built-in FTDI driver (AppleUSBFTDI). No install needed. Device:
  `/dev/cu.usbserial-XXXX`. Use the `cu.` device, not `tty.`.
- Prefer genuine FTDI. Avoid CH340 cables: unreliable BREAK timing, and 5-baud init needs it.
- **Not** an ELM327: ELM327s hide the raw K-line, can't sniff, and cheap clones are bad
  on K-line.

## First connection
1. Plug the cable into the laptop. Run `uv run pdiag ports` and confirm a `usbserial` device.
2. Plug into the car's OBD port (996: driver footwell, left of the steering column).
3. Ignition on (engine on for live data). Run `uv run pdiag obd`.
4. Send back the newest `logs/*.jsonl`.

## Sniffing a Durametric session
- Needs a Durametric interface and a Windows laptop (theirs), plus our KKL cable.
- An **OBD Y-splitter** puts both on the same K-line. Our cable only listens: `pdiag sniff`
  disables echo checking and never transmits.
- Record what Durametric was doing at each timestamp (e.g. "opened over-rev screen").
  Notes plus the capture are what make decoding possible.

## Later: product dongle
ESP32 + L9637 (or discrete transistor) K-line transceiver, OBD connector, BLE. ~$15 BOM.
Not started.

## Fallbacks if BREAK-based init fails on macOS
- `pyftdi` bit-bang mode (needs the Apple driver detached).
- Or 5-baud only (more tolerant than fast init).
