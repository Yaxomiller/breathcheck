#!/usr/bin/env python3
"""Low-level sensor board probe - is the STM32 alive at all?

Bypasses every bit of app code and talks to the board directly, to separate
"my software is wrong" from "the board is not running".

    sudo systemctl stop breathcheck
    sudo .venv/bin/python radxa/sensor-check.py

It powers the board, watches the doorbell (READY) line, and if an edge
arrives does one raw SPI exchange and reports whether the frame is valid.
"""
from __future__ import annotations

import os
import sys
import time

BRD_ON = int(os.environ.get("HH_BOARD_ENABLE_GPIO", 256))
READY = int(os.environ.get("HH_READY_GPIO", 257))
PUMP = int(os.environ.get("HH_PUMP_GPIO", 271))
CHIP = os.environ.get("HH_GPIO_CHIP", "/dev/gpiochip1")
SPI_DEV = os.environ.get("HH_SPI_DEVICE", "/dev/spidev1.0")
SPI_HZ = int(os.environ.get("HH_SPI_SPEED_HZ", 500000))
FRAME_MAX = 4 + 20 * 12 + 2      # 246 bytes
WATCH_SECONDS = float(os.environ.get("WATCH_SECONDS", 12))


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def main() -> int:
    try:
        from periphery import GPIO, SPI
    except ImportError:
        print("python-periphery not installed. Use the venv:")
        print("  sudo .venv/bin/python radxa/sensor-check.py")
        return 2

    print("=" * 58)
    print(" Sensor board probe")
    print("=" * 58)
    print(f"chip {CHIP}   BRD_ON {BRD_ON}   READY {READY}   PUMP {PUMP}")
    print(f"spi  {SPI_DEV} @ {SPI_HZ} Hz")

    trigger = ready = pump = spi = None
    try:
        try:
            trigger = GPIO(CHIP, BRD_ON, "high")
            ready = GPIO(CHIP, READY, "in", edge="falling")
            pump = GPIO(CHIP, PUMP, "low")
            spi = SPI(SPI_DEV, 0, SPI_HZ)
        except Exception as exc:
            print(f"\n!! could not open the hardware: {exc}")
            print("   Something else is holding it. Check:")
            print("     systemctl is-active breathcheck attendance-kiosk")
            print("     sudo fuser -v /dev/gpiochip1 /dev/spidev1.0")
            return 3
        print("\n[1] lines opened OK (board powered, BRD_ON high)")

        # --- 2. is the doorbell line doing anything? --------------------
        print(f"\n[2] watching READY for {WATCH_SECONDS:.0f}s ...")
        levels = {True: 0, False: 0}
        edges = 0
        deadline = time.monotonic() + WATCH_SECONDS
        while time.monotonic() < deadline:
            levels[bool(ready.read())] += 1
            if ready.poll(0.05):
                ready.read_event()
                edges += 1
            time.sleep(0.005)
        total = levels[True] + levels[False] or 1
        print(f"    HIGH (idle) {levels[True] * 100 // total}%   "
              f"LOW (asserted) {levels[False] * 100 // total}%   edges: {edges}")

        if edges == 0 and levels[False] == 0:
            print("\n    READY never left idle: the board is NOT sending doorbells.")
            print("    -> STM32 not running, not powered, or READY not wired.")
        elif edges == 0 and levels[True] == 0:
            print("\n    READY is stuck asserted (always LOW).")
            print("    -> board wedged mid-frame, or the line is shorted low.")
        else:
            print("\n    Doorbells are arriving - the board is alive.")

        # --- 3. power cycle and watch the boot window -------------------
        print("\n[3] power-cycling the board and watching for boot frames ...")
        pump.write(False)
        trigger.write(False)
        time.sleep(0.5)
        trigger.write(True)
        boot_edges = 0
        deadline = time.monotonic() + 8.0
        first_edge = None
        while time.monotonic() < deadline:
            if ready.poll(0.05):
                ready.read_event()
                boot_edges += 1
                if first_edge is None:
                    first_edge = time.monotonic()
        if boot_edges:
            print(f"    {boot_edges} doorbell(s) after power-on"
                  f" (first at +{first_edge - (deadline - 8.0):.1f}s)")
        else:
            print("    no doorbells at all in the 8s after power-on")

        # --- 4. raw SPI exchange ---------------------------------------
        print("\n[4] one raw SPI exchange (expect header AA 55) ...")
        rx = bytes(spi.transfer([0x00] * FRAME_MAX))
        print(f"    first 8 bytes: {' '.join(f'{b:02X}' for b in rx[:8])}")
        if rx[0] == 0xAA and rx[1] == 0x55:
            crc_ok = crc16_ccitt(rx[:-2]) == (rx[-2] << 8) | rx[-1]
            print(f"    header OK, records={rx[2]}, CRC {'OK' if crc_ok else 'BAD'}")
        elif set(rx[:8]) == {0x00}:
            print("    all zeros: MISO is silent - board not driving the bus")
        elif set(rx[:8]) == {0xFF}:
            print("    all 0xFF: MISO idle high - board not responding")
        else:
            print("    unexpected header - board out of sync or wrong SPI mode/speed")

        print("\n" + "=" * 58)
        print(" Summary: the board is "
              + ("ALIVE" if edges or boot_edges else "NOT RESPONDING"))
        print("=" * 58)
        return 0 if (edges or boot_edges) else 1
    finally:
        for line in (pump, trigger):
            if line is not None:
                try:
                    line.write(False)
                except Exception:
                    pass
        for resource in (spi, pump, ready, trigger):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
