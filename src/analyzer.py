"""Breath sensor drivers.

Two implementations behind one interface:

- MockAnalyzer     : random-but-realistic readings for development machines.
- SpiBreathAnalyzer: the PID sensor board behind an STM32 SPI bridge.

The live board uses a doorbell/frame protocol:

- BRD_ON GPIO powers the board; the READY GPIO is a doorbell input
  (idle HIGH, falling edge = a frame is ready and must be read within 100 ms).
- Each exchange is ONE full-duplex 246-byte transfer:
  header (0xAA 0x55, record count, pad) + 20 records x 12 bytes + CRC16-CCITT.
- Record layout (little-endian): uint32 tick, uint16 source, 2 pad, int32 value.
  Sources: 1 = AD7798, 2 = AD5941.
- First-byte commands: 0x00 none, 0xA0 PID startup, 0xA1 PID shutdown.

The cannabis reading is reported as the RAW aggregated ADC value from the PID
source — no ppb conversion is applied (calibration is not available yet).
`read(seconds)` blocks for the exhale window in SPI mode.
"""
from __future__ import annotations

import importlib
import random
import struct
import time
from dataclasses import dataclass
from typing import Any, Optional

from src import config

CMD_NONE = 0x00
CMD_PID_STARTUP = 0xA0
CMD_PID_SHUTDOWN = 0xA1

MAX_RECORDS = 20
RECORD_SIZE = 12
FRAME_MAX = 4 + MAX_RECORDS * RECORD_SIZE + 2  # hdr + records + CRC16 = 246

SOURCE_NAMES = {1: "AD7798", 2: "AD5941"}


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class Reading:
    alcohol_bac: float      # blood alcohol, mg/100ml
    cannabis_raw: float     # raw PID ADC value (no conversion)


class BreathAnalyzer:
    name = "base"
    startup_warnings: tuple[str, ...] = ()

    def read(self, seconds: float) -> Reading:
        raise NotImplementedError


class MockAnalyzer(BreathAnalyzer):
    name = "mock"

    def __init__(self, startup_warnings: tuple[str, ...] = ()) -> None:
        self.startup_warnings = startup_warnings

    def read(self, seconds: float) -> Reading:
        del seconds
        alcohol = random.uniform(config.MOCK_ALCOHOL_MIN, config.MOCK_ALCOHOL_MAX)
        cannabis = random.uniform(config.MOCK_CANNABIS_MIN, config.MOCK_CANNABIS_MAX)
        return Reading(
            alcohol_bac=round(max(0.0, alcohol), 1),
            cannabis_raw=float(round(cannabis)),
        )


class SpiBreathAnalyzer(BreathAnalyzer):
    name = "spi"

    def __init__(self, periphery_module: Optional[Any] = None) -> None:
        self.periphery = periphery_module or importlib.import_module("periphery")
        warnings = []
        if config.ALCOHOL_SOURCE != "adc":
            warnings.append(
                "Alcohol readings are placeholder values until the alcohol "
                "conversion is calibrated (HH_ALCOHOL_SOURCE=adc)."
            )
        warnings.append(
            "Cannabis shows the raw PID ADC value (no ppb conversion yet)."
        )
        self.startup_warnings = tuple(warnings)

    def _exchange(self, spi: Any, cmd: int) -> tuple[Optional[list[tuple[int, int, int]]], Optional[str]]:
        tx = [cmd] + [0x00] * (FRAME_MAX - 1)
        rx = bytes(spi.transfer(tx))  # ONE full-duplex 246-byte transfer
        if rx[0] != 0xAA or rx[1] != 0x55:
            return None, f"bad header {rx[0]:02X} {rx[1]:02X}"
        if crc16_ccitt(rx[:-2]) != (rx[-2] << 8) | rx[-1]:
            return None, "bad CRC"
        records = []
        for i in range(min(rx[2], MAX_RECORDS)):
            # SensorRecord: uint32 tick, uint16 src, 2 pad, int32 val (little-endian)
            records.append(struct.unpack_from("<IH2xi", rx, 4 + i * RECORD_SIZE))
        return records, None

    def _aggregate(self, samples: list[int]) -> float:
        if config.SAMPLE_AGGREGATION == "peak":
            return float(max(samples))
        if config.SAMPLE_AGGREGATION == "last":
            return float(samples[-1])
        return sum(samples) / len(samples)

    def _alcohol_bac(self, raw_value: float) -> float:
        if config.ALCOHOL_SOURCE == "adc":
            return max(0.0, raw_value * config.ALCOHOL_SCALE + config.ALCOHOL_OFFSET)
        return round(random.uniform(0.0, 10.0), 1)

    def read(self, seconds: float) -> Reading:
        trigger = None
        ready = None
        spi = None
        samples: list[int] = []
        last_error: Optional[str] = None
        sent_startup = False
        try:
            trigger = self.periphery.GPIO(config.GPIO_CHIP, config.BOARD_ENABLE_GPIO, "out")
            ready = self.periphery.GPIO(config.GPIO_CHIP, config.READY_GPIO, "in", edge="falling")
            spi = self.periphery.SPI(config.SPI_DEVICE, config.SPI_MODE, config.SPI_SPEED_HZ)

            trigger.write(False)
            time.sleep(0.01)
            trigger.write(True)

            deadline = time.monotonic() + max(1.0, seconds)
            while time.monotonic() < deadline:
                if ready.read():  # idle high: wait for a falling edge
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if not ready.poll(min(remaining, config.DOORBELL_TIMEOUT_SECONDS)):
                        continue
                    ready.read_event()
                # must exchange within 100 ms of the edge
                records, error = self._exchange(spi, CMD_NONE if sent_startup else CMD_PID_STARTUP)
                sent_startup = True
                if error:
                    last_error = error
                else:
                    for _tick, source, value in records:
                        if config.PID_SOURCE in (0, source):
                            samples.append(value)
                # let the STM32 deassert before the next wait
                release_deadline = time.monotonic() + 0.5
                while not ready.read() and time.monotonic() < release_deadline:
                    time.sleep(0.001)

            if not samples:
                detail = f" (last frame error: {last_error})" if last_error else ""
                raise RuntimeError(f"No PID samples received from the breath board{detail}")

            aggregated = self._aggregate(samples)
            return Reading(
                alcohol_bac=round(self._alcohol_bac(aggregated), 1),
                cannabis_raw=round(aggregated, 1),
            )
        except Exception as exc:
            raise RuntimeError(f"Breath sensor read failed: {exc}") from exc
        finally:
            if spi is not None and sent_startup:
                try:
                    if ready is not None and ready.read() and ready.poll(2.0):
                        ready.read_event()
                    self._exchange(spi, CMD_PID_SHUTDOWN)
                except Exception:
                    pass
            for resource in (spi,):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            if trigger is not None:
                try:
                    trigger.write(False)
                    trigger.close()
                except Exception:
                    pass
            if ready is not None:
                try:
                    ready.close()
                except Exception:
                    pass


def resolve_analyzer() -> BreathAnalyzer:
    if config.ANALYZER_MODE in {"spi", "live", "hardware"}:
        try:
            return SpiBreathAnalyzer()
        except Exception as exc:
            return MockAnalyzer(
                startup_warnings=(
                    f"SPI breath board unavailable ({exc}). Using mock readings.",
                )
            )
    return MockAnalyzer()
