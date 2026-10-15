# Hardware

## Cable (buy this)
FTDI-based **KKL** USB cable (VAG-COM 409.1 style, FT232RL chip), ~$20.
- macOS has a built-in FTDI driver (AppleUSBFTDI). No install needed. Device:
  `/dev/cu.usbserial-XXXX`. Use the `cu.` device, not `tty.`.
- Prefer genuine FTDI. Avoid CH340 cables: unreliable BREAK timing, and 5-baud init needs it.
- **Not** an ELM327: ELM327s hide the raw K-line, can't sniff, and cheap clones are bad
  on K-line.

## Bench check (2026-10-15, cable on the Mac, no car)
- Enumerates as FTDI `0403:6001` "FT232R USB UART" (`bcdDevice 0x0600`), bound to Apple's
  built-in DriverKit driver (`com.apple.DriverKit-AppleUSBFTDI`). Node `/dev/cu.usbserial-<serial>`.
- 10400 baud 8N1 accepted. BREAK set/clear ioctls return in 0.15–0.27 ms; a 25 ms pulse
  timed in Python lands at 25.1–25.2 ms. Wire timing still unverified (no scope).
- No echo off the car: the K-line transceiver is powered from OBD pin 16 (12 V, live
  even with the ignition off). The echo test belongs at the car, ignition off.

## At the car, ignition off (2026-10-15)
- Plug the OBD end in **before** the USB end. Plugging OBD in with USB live once left the
  FT232R refusing every port setting (`tcsetattr: Invalid argument`, even from `stty`)
  until the USB end was replugged.
- Bus silent with ignition off. Echo exact: 440/440 bytes (120 single, 20x16 bursts).
- FTDI latency timer is 16 ms (echo delay 2-17 ms, median 14.6); macOS doesn't lower it.
- A BREAK leaves one `00` in RX about 1.4 ms after the line goes high, i.e. after our
  `reset_input_buffer()`. Hence the junk skip before the 5-baud sync and `drain()`
  before every request.
- **pyserial 3.5: never assign `ser.timeout` after open.** It reconfigures the port
  (tcsetattr at 38400, then IOSSIOSPEED back to 10400), corrupting the byte on the wire:
  7 of 440 echo bytes before the fix, 0 after. `SerialTransport` waits with `select()`.

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
