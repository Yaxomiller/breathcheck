"""Breath sensor drivers — DiDies breathalyzer measurement cycle.

Two implementations behind one interface:

- MockAnalyzer     : simulates the same phased cycle on development machines.
- SpiBreathAnalyzer: the STM32 sensor board over SPI (doorbell protocol).

Measurement cycle, timed in the STM32 tick domain (immune to host latency):

  PURGE     pump ON (active-high), PID + alcohol AFE switched on via frame
            commands; readings streamed, not analysed
  BASELINE  per-sensor averages -> fresh-air zero
  MEASURE   subject blows; per-sample delta = x - baseline, trapezoidal
            integral over the window for BOTH sensors, reported in mV*s
            (the AL-05P datasheet specs linearity as the INTEGRAL of output);
            peak deltas tracked too
  SHUTDOWN  AFE sampling + PID off, pump off. The alcohol cell idles
            VIRTUALLY SHORTED (LP loop stays biased at 0 V) — a 2-lead fuel
            cell must be stored shorted or it polarizes and drifts.

Sources:  1 = AD7798 (PID / cannabis, ADC codes, 19.073 uV/LSB)
          2 = AD5941 (alcohol fuel cell, nA; V = I * Rtia at Rtia = 4k)
          3 = SYS 1 Hz keepalive (val bit0 = PID on, bit1 = AFE running);
              guarantees frames >= 1 Hz so on/off commands (which only ride
              on frames) can always be delivered.

The board is powered once and left on between cycles; the firmware's
zero-offset calibration runs at its boot. BRD_ON is requested as output-HIGH
in one atomic step ("high") — requesting plain "out" would drive it LOW
first and reset the STM32 on every start.

`stabilize()` is app-start priming: pump OFF, PID lamp OFF, AFE sampling ON;
waits until the alcohol baseline drift stays under SETTLE_SLOPE_NA_S, then
stops sampling (cell remains biased). Run once at boot; each cycle's own
purge handles the pump-airflow step from a settled starting point.
"""
from __future__ import annotations

import importlib
import random
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src import config

CMD_NONE = 0x00
CMD_PID_STARTUP = 0xA0
CMD_PID_SHUTDOWN = 0xA1
CMD_AFE_STARTUP = 0xB0
CMD_AFE_SHUTDOWN = 0xB1

SRC_AD7798, SRC_AD5941, SRC_SYS = 1, 2, 3

MAX_RECORDS = 20
RECORD_SIZE = 12
FRAME_MAX = 4 + MAX_RECORDS * RECORD_SIZE + 2  # 246 bytes: hdr + records + CRC16

# AD5941: V = I * Rtia -> 1 uA = 4 mV at Rtia = 4 kOhm (LPTIARTIA_4K).
# AD7798: mV per LSB (unipolar, gain 2, Vref 2.5 V).
PID_MV_PER_LSB = 2.5 / (2 * 65536) * 1000.0   # 0.019073 mV

BASELINE_SPREAD_WARN = {SRC_AD5941: 200,   # nA
                        SRC_AD7798: 300}   # codes (PID drifts during warm-up)

# progress(phase, elapsed_seconds, total_seconds);
# phase: starting|purge|baseline|measure
ProgressFn = Callable[[str, float, float], None]


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _finite(value: float) -> float:
    return float(value) if value == value and abs(value) != float("inf") else 0.0


@dataclass(frozen=True)
class ChannelResult:
    baseline: float        # raw units (AD5941 nA, AD7798 codes)
    peak: float            # raw delta above baseline
    peak_t_ms: int         # ms into the cycle at the peak
    integral_mvs: float    # trapezoidal integral of the delta, mV*s
    stable: bool = True    # baseline spread within tolerance


@dataclass(frozen=True)
class CycleResult:
    alcohol: ChannelResult    # AD5941 fuel cell
    cannabis: ChannelResult   # AD7798 PID


def _mvs(src: int, integ_raw_ms: float) -> float:
    """Convert a raw trapezoidal integral (raw-unit * ms) to mV*s."""
    if src == SRC_AD5941:
        return integ_raw_ms * config.RTIA_KOHM / 1e6          # nA*ms -> mV*s
    return integ_raw_ms * PID_MV_PER_LSB / 1000.0             # code*ms -> mV*s


class BreathAnalyzer:
    name = "base"
    startup_warnings: tuple[str, ...] = ()
    state = "ready"   # ready | stabilizing | measuring | error

    def run_cycle(self, measure_seconds: float, progress: Optional[ProgressFn] = None) -> CycleResult:
        raise NotImplementedError

    def stabilize(self) -> None:
        """App-start priming; no-op for the mock."""


class MockAnalyzer(BreathAnalyzer):
    name = "mock"

    def __init__(self, startup_warnings: tuple[str, ...] = ()) -> None:
        self.startup_warnings = startup_warnings
        self._lock = threading.Lock()

    def run_cycle(self, measure_seconds: float, progress: Optional[ProgressFn] = None) -> CycleResult:
        with self._lock:
            self.state = "measuring"
            try:
                phases = (
                    ("purge", config.PURGE_SECONDS),
                    ("baseline", config.BASELINE_SECONDS),
                    ("measure", max(1.0, measure_seconds)),
                )
                for phase, total in phases:
                    started = time.monotonic()
                    while True:
                        elapsed = time.monotonic() - started
                        if progress:
                            progress(phase, min(elapsed, total), total)
                        if elapsed >= total:
                            break
                        time.sleep(0.15)

                alcohol_mvs = random.uniform(config.MOCK_ALCOHOL_MIN, config.MOCK_ALCOHOL_MAX)
                cannabis_mvs = random.uniform(config.MOCK_CANNABIS_MIN, config.MOCK_CANNABIS_MAX)
                peak_ms = int((config.PURGE_SECONDS + config.BASELINE_SECONDS
                               + measure_seconds * random.uniform(0.3, 0.7)) * 1000)
                return CycleResult(
                    alcohol=ChannelResult(
                        baseline=round(random.uniform(300, 1500), 1),        # nA
                        peak=round(alcohol_mvs * 250, 1),                    # nA
                        peak_t_ms=peak_ms,
                        integral_mvs=round(alcohol_mvs, 3),
                    ),
                    cannabis=ChannelResult(
                        baseline=round(random.uniform(400, 800), 1),         # codes
                        peak=round(cannabis_mvs * 40, 1),                    # codes
                        peak_t_ms=peak_ms,
                        integral_mvs=round(cannabis_mvs, 3),
                    ),
                )
            finally:
                self.state = "ready"


class SpiBreathAnalyzer(BreathAnalyzer):
    name = "spi"

    def __init__(self, periphery_module: Optional[Any] = None) -> None:
        self.periphery = periphery_module or importlib.import_module("periphery")
        self._lock = threading.Lock()
        self.startup_warnings = (
            "Readings are uncalibrated integrals (mV*s) — set limits after calibration.",
        )
        self.last_stabilize: dict[str, Any] = {}
        self.stabilize_started_at: Optional[float] = None

        # Open once; the board stays powered between cycles so the STM32
        # keeps its boot-time zero-offset calibration. "high" = atomic
        # output-HIGH request (plain "out" would blip the line LOW and
        # reset the STM32).
        self.trigger = self.periphery.GPIO(config.GPIO_CHIP, config.BOARD_ENABLE_GPIO, "high")
        self.ready = self.periphery.GPIO(config.GPIO_CHIP, config.READY_GPIO, "in", edge="falling")
        self.pump = self.periphery.GPIO(config.GPIO_CHIP, config.PUMP_GPIO, "out")
        self.spi = self.periphery.SPI(config.SPI_DEVICE, config.SPI_MODE, config.SPI_SPEED_HZ)
        self.pump.write(False)

    # ---- frame layer -----------------------------------------------------

    def _exchange(self, cmd: int) -> tuple[Optional[list[tuple[int, int, int]]], Optional[str]]:
        tx = [cmd] + [0x00] * (FRAME_MAX - 1)
        rx = bytes(self.spi.transfer(tx))   # ONE full-duplex 246-byte transfer
        if rx[0] != 0xAA or rx[1] != 0x55:
            return None, f"bad header {rx[0]:02X} {rx[1]:02X}"
        if crc16_ccitt(rx[:-2]) != (rx[-2] << 8) | rx[-1]:
            return None, "bad CRC"
        records = []
        for i in range(min(rx[2], MAX_RECORDS)):
            # SensorRecord: uint32 tick(ms), uint16 src, 2 pad, int32 val (LE)
            records.append(struct.unpack_from("<IH2xi", rx, 4 + i * RECORD_SIZE))
        return records, None

    def _wait_frame(self, cmd: int = CMD_NONE) -> tuple[Optional[list[tuple[int, int, int]]], bool]:
        """Block for the next doorbell, exchange one frame carrying `cmd`.
        Returns (records, delivered). On a corrupt frame the STM32 may or may
        not have latched the command — treated as NOT delivered (commands are
        idempotent, so re-sending is safe)."""
        if self.ready.read():   # idle high: wait for a falling edge
            if not self.ready.poll(config.DOORBELL_TIMEOUT_SECONDS):
                return None, False
            self.ready.read_event()
        records, error = self._exchange(cmd)
        while not self.ready.read():   # let the STM32 deassert
            time.sleep(0.001)
        if error:
            return None, False
        return records, True

    def _send_commands(self, commands: list[int], max_tries: int = 15) -> bool:
        """Deliver each command on its own frame, retrying on frame errors.
        Each command gets its own retry budget: a slow PID start must not use
        all the attempts before the alcohol AFE start command is sent."""
        for command in commands:
            for _attempt in range(max_tries):
                _records, delivered = self._wait_frame(command)
                if delivered:
                    break
            else:
                return False
        return True

    # ---- stabilize (app-start priming) -----------------------------------

    def stabilize(self) -> None:
        with self._lock:
            self.state = "stabilizing"
            self.stabilize_started_at = time.time()
            samples: list[tuple[int, int]] = []   # (stm32_ms, nA)
            settled = False
            started = self.stabilize_started_at
            try:
                self.pump.write(False)   # priming happens in still air
                self._send_commands([CMD_PID_SHUTDOWN, CMD_AFE_STARTUP])
                last_eval = 0.0
                ok_streak = 0
                while time.time() - started < config.STABILIZE_MAX_S:
                    records, _ = self._wait_frame()
                    if records is None:
                        continue
                    for tick, source, value in records:
                        if source == SRC_AD5941:
                            samples.append((tick, value))
                    if not samples or time.time() - last_eval < 1.0:
                        continue
                    last_eval = time.time()
                    t_now = samples[-1][0]
                    window = [s for s in samples if t_now - s[0] <= config.SETTLE_WINDOW_MS]
                    if len(window) < 8 or (t_now - window[0][0]) < config.SETTLE_WINDOW_MS * 0.8:
                        continue
                    mid = (t_now + window[0][0]) / 2.0
                    first = [v for t, v in window if t <= mid]
                    second = [v for t, v in window if t > mid]
                    if not first or not second:
                        continue
                    slope = ((sum(second) / len(second)) - (sum(first) / len(first))) \
                        / (config.SETTLE_WINDOW_MS / 2000.0)   # nA/s
                    if abs(slope) <= config.SETTLE_SLOPE_NA_S:
                        ok_streak += 1
                        if ok_streak >= 2:
                            settled = True
                            break
                    else:
                        ok_streak = 0
            except Exception:
                pass
            finally:
                # Stop sampling; the LP loop keeps the cell biased (virtual short).
                try:
                    self._send_commands([CMD_AFE_SHUTDOWN])
                    self.pump.write(False)
                except Exception:
                    pass
                self.last_stabilize = {
                    "settled": settled,
                    "final_ua": round(samples[-1][1] / 1000.0, 3) if samples else None,
                    "elapsed_s": round(time.time() - started, 1),
                }
                self.stabilize_started_at = None
                self.state = "ready"

    # ---- measurement cycle ------------------------------------------------

    def run_cycle(self, measure_seconds: float, progress: Optional[ProgressFn] = None) -> CycleResult:
        with self._lock:
            self.state = "measuring"
            purge_ms = config.PURGE_SECONDS * 1000.0
            baseline_ms = config.BASELINE_SECONDS * 1000.0
            measure_ms = max(1.0, measure_seconds) * 1000.0
            total_ms = purge_ms + baseline_ms + measure_ms

            stats = {s: {"base": [], "baseline": None, "integ": 0.0,
                         "peak": 0.0, "peak_t": 0, "prev": None, "stable": True}
                     for s in (SRC_AD7798, SRC_AD5941)}
            t0: Optional[int] = None   # STM32 tick of first AD5941 sample
            try:
                if progress:
                    progress("starting", 0.0, 0.0)
                self.pump.write(True)   # active high
                if not self._send_commands([CMD_PID_STARTUP, CMD_AFE_STARTUP]):
                    raise RuntimeError("sensor board did not acknowledge startup commands")

                # Command delivery can legitimately take a few seconds while
                # the board wakes. Do not charge that time to the measurement
                # deadline or the first scan can fail after partially starting
                # the sensor, only for an immediate retry to work.
                wall_deadline = time.monotonic() + total_ms / 1000.0 + 30.0
                if progress:
                    progress("purge", 0.0, purge_ms / 1000.0)

                while True:
                    if time.monotonic() > wall_deadline:
                        raise RuntimeError("sensor board stopped responding mid-cycle")
                    records, _ = self._wait_frame()
                    if records is None:
                        continue
                    for tick, source, value in records:
                        if t0 is None and source == SRC_AD5941:
                            t0 = tick   # anchor on first alcohol sample
                        if source not in stats or t0 is None or tick < t0:
                            continue
                        dt = tick - t0
                        if dt >= total_ms:   # cycle complete
                            return self._build_result(stats)
                        channel = stats[source]
                        if dt < purge_ms:
                            phase, elapsed, total = "purge", dt / 1000.0, purge_ms / 1000.0
                        elif dt < purge_ms + baseline_ms:
                            phase = "baseline"
                            elapsed, total = (dt - purge_ms) / 1000.0, baseline_ms / 1000.0
                            channel["base"].append(value)
                        else:
                            phase = "measure"
                            elapsed, total = (dt - purge_ms - baseline_ms) / 1000.0, measure_ms / 1000.0
                            if channel["baseline"] is None and channel["base"]:
                                channel["baseline"] = sum(channel["base"]) / float(len(channel["base"]))
                                spread = max(channel["base"]) - min(channel["base"])
                                channel["stable"] = spread <= BASELINE_SPREAD_WARN[source]
                            if channel["baseline"] is not None:
                                delta = value - channel["baseline"]
                                if channel["prev"] is not None:
                                    prev_t, prev_d = channel["prev"]
                                    channel["integ"] += (delta + prev_d) / 2.0 * (tick - prev_t)
                                if delta > channel["peak"]:
                                    channel["peak"], channel["peak_t"] = delta, dt
                                channel["prev"] = (tick, delta)
                        if progress:
                            progress(phase, elapsed, total)
            finally:
                # Sensors off first (needs live frames), then pump.
                try:
                    self._send_commands([CMD_AFE_SHUTDOWN, CMD_PID_SHUTDOWN])
                except Exception:
                    pass
                try:
                    self.pump.write(False)
                except Exception:
                    pass
                self.state = "ready"

    def _build_result(self, stats: dict) -> CycleResult:
        def channel(source: int) -> ChannelResult:
            data = stats[source]
            baseline = data["baseline"] if data["baseline"] is not None else 0.0
            return ChannelResult(
                baseline=_finite(round(baseline, 1)),
                peak=_finite(round(data["peak"], 1)),
                peak_t_ms=int(data["peak_t"]),
                integral_mvs=_finite(round(_mvs(source, data["integ"]), 3)),
                stable=bool(data["stable"]),
            )
        return CycleResult(alcohol=channel(SRC_AD5941), cannabis=channel(SRC_AD7798))


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
