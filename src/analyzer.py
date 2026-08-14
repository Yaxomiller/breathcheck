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
  IDLE      PID + pump off. Alcohol AFE sampling stays on so the real board,
            whose deployed firmware has no idle SYS keepalive, continues to
            provide doorbell frames that can carry the next START command.
            The alcohol cell remains virtually shorted at 0 V.
            A background keepalive thread answers every idle doorbell within
            the 100 ms protocol window — an unanswered stream desyncs the
            STM32 SPI slave and made the FIRST scan after idle fail with
            "no frames from sensor". If no valid frame arrives for
            STREAM_DEAD_SECONDS the keepalive resets the board and restarts
            AFE sampling by itself, so START always lands on a live stream.

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

import atexit
import importlib
import logging
import math
import os
import random
import signal
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src import config

logger = logging.getLogger("breathcheck.analyzer")

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
# phase: starting|recovering|purge|baseline|measure
ProgressFn = Callable[[str, float, float], None]


def _build_crc16_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC16_TABLE = _build_crc16_table()


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, table driven.

    Every 246-byte frame is checksummed, and the keepalive checks frames
    continuously. The bit-by-bit form cost ~2000 Python iterations per frame
    and held the GIL, stalling the web server; the table costs one lookup per
    byte instead.
    """
    crc = 0xFFFF
    table = _CRC16_TABLE
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ table[((crc >> 8) ^ byte) & 0xFF]
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
    # Exhale trace, one entry per sample in the MEASURE window:
    # (ms into the blow, raw ADC value, delta above baseline, delta in mV)
    samples: tuple[tuple[int, float, float, float], ...] = ()


@dataclass(frozen=True)
class CycleResult:
    alcohol: ChannelResult    # AD5941 fuel cell
    cannabis: ChannelResult   # AD7798 PID


def _mvs(src: int, integ_raw_ms: float) -> float:
    """Convert a raw trapezoidal integral (raw-unit * ms) to mV*s."""
    if src == SRC_AD5941:
        return integ_raw_ms * config.RTIA_KOHM / 1e6          # nA*ms -> mV*s
    return integ_raw_ms * PID_MV_PER_LSB / 1000.0             # code*ms -> mV*s


def sample_mv(src: int, raw_delta: float) -> float:
    """Convert one raw sample delta to mV (V = I*Rtia for the fuel cell)."""
    if src == SRC_AD5941:
        return raw_delta * config.RTIA_KOHM / 1000.0          # nA -> mV
    return raw_delta * PID_MV_PER_LSB                         # codes -> mV


class BreathAnalyzer:
    name = "base"
    startup_warnings: tuple[str, ...] = ()
    state = "ready"   # ready | stabilizing | measuring | finishing | error
    stream_ok = True  # False while the doorbell/frame stream is dead

    def run_cycle(self, measure_seconds: float, progress: Optional[ProgressFn] = None) -> CycleResult:
        raise NotImplementedError

    def stabilize(self) -> None:
        """App-start priming; no-op for the mock."""

    def collect_samples(self, seconds: float, progress: Optional[Callable[[float, float], None]] = None,
                        store: bool = True, pump_on: bool = True) -> dict[int, list[tuple[int, float]]]:
        raise NotImplementedError

    def shutdown(self) -> None:
        """Release hardware safely on exit; no-op for the mock."""


def _mock_bell(measure_ms: float, baseline: float, peak_delta: float,
               step_ms: int = 100) -> tuple[tuple[int, float, float, float], ...]:
    """A positive bell-shaped exhale trace for development machines, shaped
    like the real PID response (rise, peak mid-blow, decay)."""
    centre = measure_ms * 0.45
    width = max(1.0, measure_ms * 0.22)
    samples = []
    for t_ms in range(0, int(measure_ms) + 1, step_ms):
        bell = math.exp(-((t_ms - centre) ** 2) / (2 * width * width))
        delta = peak_delta * bell + random.uniform(-0.4, 0.4)
        samples.append((t_ms, round(baseline + delta, 1), round(delta, 2),
                        sample_mv(SRC_AD7798, delta)))
    return tuple(samples)


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
                measure_ms = max(1.0, measure_seconds) * 1000.0
                peak_ms = int((config.PURGE_SECONDS + config.BASELINE_SECONDS
                               + measure_seconds * random.uniform(0.3, 0.7)) * 1000)
                cannabis_baseline = round(random.uniform(400, 800), 1)
                cannabis_peak = round(cannabis_mvs * 40, 1)
                return CycleResult(
                    alcohol=ChannelResult(
                        baseline=round(random.uniform(300, 1500), 1),        # nA
                        peak=round(alcohol_mvs * 250, 1),                    # nA
                        peak_t_ms=peak_ms,
                        integral_mvs=round(alcohol_mvs, 3),
                    ),
                    cannabis=ChannelResult(
                        baseline=cannabis_baseline,                          # codes
                        peak=cannabis_peak,                                  # codes
                        peak_t_ms=peak_ms,
                        integral_mvs=round(cannabis_mvs, 3),
                        samples=_mock_bell(measure_ms, cannabis_baseline, cannabis_peak),
                    ),
                )
            finally:
                self.state = "ready"


    def collect_samples(self, seconds: float, progress: Optional[Callable[[float, float], None]] = None,
                        store: bool = True, pump_on: bool = True) -> dict[int, list[tuple[int, float]]]:
        """Simulated calibration sampling: a settled baseline with light noise,
        so the procedure can be exercised without the sensor board.

        Runs in REAL time by default so the on-screen countdown is honest.
        HH_MOCK_SPEEDUP>1 compresses it for UI work only.
        """
        with self._lock:
            self.state = "measuring"
            try:
                out = {SRC_AD7798: [], SRC_AD5941: []}
                speedup = max(1.0, config.MOCK_SPEEDUP)
                step_ms, started = 100, time.monotonic()
                drift = random.uniform(-0.02, 0.02)
                for t_ms in range(0, int(seconds * 1000) + 1, step_ms):
                    if store:
                        out[SRC_AD5941].append((t_ms, 900 + drift * t_ms / 100 + random.uniform(-12, 12)))
                        out[SRC_AD7798].append((t_ms, 640 + random.uniform(-1.5, 1.5)))
                    if progress:
                        progress(min(t_ms / 1000.0, seconds), seconds)
                    target = started + (t_ms / 1000.0) / speedup
                    now = time.monotonic()
                    if target > now:
                        time.sleep(min(0.1, target - now))
                return out
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

        # Request BRD_ON atomically high, open the bus, then perform one
        # deliberate reset so every backend start begins from a known STM32
        # state. The stabilize pass below rebuilds its zero/baseline state.
        self.trigger = self.periphery.GPIO(config.GPIO_CHIP, config.BOARD_ENABLE_GPIO, "high")
        self.ready = self.periphery.GPIO(config.GPIO_CHIP, config.READY_GPIO, "in", edge="falling")
        # "low" requests the pump line as an output ALREADY driven low, in one
        # atomic step. Plain "out" leaves the initial level to the board, and
        # on this unit it comes up HIGH — which runs the pump from the moment
        # the app opens the line. Drive it low again immediately, and do it
        # before the SPI bus is opened so no slow call widens that window.
        self.pump = self.periphery.GPIO(config.GPIO_CHIP, config.PUMP_GPIO, "low")
        self.pump.write(False)
        self.spi = self.periphery.SPI(config.SPI_DEVICE, config.SPI_MODE, config.SPI_SPEED_HZ)
        self._reset_board()
        self._last_frame_at = time.monotonic()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    # ---- frame layer -----------------------------------------------------

    def _reset_board(self) -> None:
        """Reset the STM32/AFE and wait briefly for its boot sequence."""
        self.pump.write(False)
        self.trigger.write(False)
        time.sleep(config.BOARD_RESET_SECONDS)
        self.trigger.write(True)
        time.sleep(config.BOARD_BOOT_SECONDS)

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

    def _wait_frame(self, cmd: int = CMD_NONE,
                    timeout: Optional[float] = None) -> tuple[Optional[list[tuple[int, int, int]]], bool]:
        """Block for the next doorbell, exchange one frame carrying `cmd`.
        Returns (records, delivered). On a corrupt frame the STM32 may or may
        not have latched the command — treated as NOT delivered (commands are
        idempotent, so re-sending is safe)."""
        if timeout is None:
            timeout = config.DOORBELL_TIMEOUT_SECONDS
        if self.ready.read():   # idle high: wait for a falling edge
            # Drop stale queued edges first — answering a doorbell whose
            # 100 ms window has passed exchanges garbage with the STM32.
            while self.ready.poll(0):
                self.ready.read_event()
            if self.ready.read():
                if not self.ready.poll(timeout):
                    return None, False
                self.ready.read_event()
        records, error = self._exchange(cmd)
        if error is None:
            self._last_frame_at = time.monotonic()
            self.stream_ok = True
        # Let the STM32 deassert. PID lamp startup keeps the STM32 busy well
        # past a second, so a slow deassert after a VALID frame must not fail
        # the exchange — the command was already latched. The bound exists
        # only so a wedged board cannot hang the thread forever.
        deadline = time.monotonic() + config.DOORBELL_TIMEOUT_SECONDS
        while not self.ready.read():
            if time.monotonic() >= deadline:
                if error is None:
                    logger.warning("doorbell still asserted %.1fs after a valid frame",
                                   config.DOORBELL_TIMEOUT_SECONDS)
                    break
                return None, False
            time.sleep(0.001)
        if error:
            return None, False
        return records, True

    def _send_commands(self, commands: list[int], max_tries: int = 15,
                       timeout: Optional[float] = None) -> bool:
        """Deliver each command on its own frame, retrying on frame errors.
        Each command gets its own retry budget: a slow PID start must not use
        all the attempts before the alcohol AFE start command is sent."""
        for command in commands:
            for attempt in range(max_tries):
                _records, delivered = self._wait_frame(command, timeout=timeout)
                if delivered:
                    if attempt:
                        logger.info("command 0x%02X delivered on attempt %d",
                                    command, attempt + 1)
                    break
                # Retries can each burn seconds on doorbell/deassert timeouts,
                # so say something rather than going quiet for minutes.
                if attempt and attempt % 3 == 0:
                    logger.warning("command 0x%02X still unacknowledged (%d/%d)",
                                   command, attempt + 1, max_tries)
            else:
                logger.warning("command 0x%02X not delivered after %d frames",
                               command, max_tries)
                return False
        return True

    # ---- idle keepalive ---------------------------------------------------

    def _keepalive_loop(self) -> None:
        """Answer doorbells between cycles.

        The protocol obliges the host to answer every doorbell within 100 ms.
        stabilize()/run_cycle() honour that while they hold the lock; this
        thread covers the idle gaps so the frame stream never desyncs, and
        revives the board on its own if the stream goes quiet."""
        while True:
            time.sleep(0.02)
            if not self._lock.acquire(timeout=1.0):
                continue   # a cycle owns the bus and is servicing doorbells
            try:
                if self.state != "ready":
                    continue
                self._wait_frame(timeout=0.25)
                if time.monotonic() - self._last_frame_at > config.STREAM_DEAD_SECONDS:
                    self._recover_stream()
            except Exception:
                pass   # never let the keepalive die; next pass retries
            finally:
                self._lock.release()

    def _recover_stream(self) -> None:
        """Reset a silent board and restart AFE sampling in the background,
        so the next scan starts on a live doorbell instead of failing."""
        logger.warning("frame stream dead for %.0fs — resetting sensor board",
                       config.STREAM_DEAD_SECONDS)
        self._reset_board()
        self.stream_ok = self._send_commands(
            [CMD_PID_SHUTDOWN, CMD_AFE_STARTUP], max_tries=8, timeout=0.5)
        logger.warning("sensor board recovery %s",
                       "succeeded" if self.stream_ok else "FAILED")
        self._last_frame_at = time.monotonic()

    # ---- stabilize (app-start priming) -----------------------------------

    def stabilize(self) -> None:
        with self._lock:
            self.state = "stabilizing"
            self.stabilize_started_at = time.time()
            samples: list[tuple[int, int]] = []   # (stm32_ms, nA)
            settled = False
            skipped = False
            recovered = False
            error: Optional[str] = None
            started = self.stabilize_started_at
            try:
                self.pump.write(False)   # priming happens in still air
                startup_commands = [CMD_PID_SHUTDOWN, CMD_AFE_STARTUP]
                if not self._send_commands(startup_commands, max_tries=3):
                    recovered = True
                    self._reset_board()
                    if not self._send_commands(startup_commands):
                        raise RuntimeError("sensor board produced no frames after hardware reset")
                if not config.WARMUP_ENABLED:
                    # AFE sampling is now running (the doorbell stream needs
                    # it); skip the wait for the baseline drift to settle so
                    # the unit is ready to scan immediately.
                    skipped = True
                    return
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
            except Exception as exc:
                error = str(exc)
            finally:
                # Keep AFE sampling alive between tests. The deployed board
                # does not emit the documented SYS keepalive while both
                # channels are off; shutting AFE down here leaves no doorbell
                # frame on which the next START command can be delivered.
                try:
                    self.pump.write(False)
                except Exception:
                    pass
                self.last_stabilize = {
                    "settled": settled,
                    "skipped": skipped,
                    "final_ua": round(samples[-1][1] / 1000.0, 3) if samples else None,
                    "elapsed_s": round(time.time() - started, 1),
                    "hardware_reset": recovered,
                }
                if error:
                    self.last_stabilize["error"] = error
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
                         "peak": 0.0, "peak_t": 0, "prev": None, "stable": True,
                         "samples": []}
                     for s in (SRC_AD7798, SRC_AD5941)}
            t0: Optional[int] = None   # STM32 tick of first AD5941 sample
            try:
                if progress:
                    progress("starting", 0.0, 0.0)
                self.pump.write(True)   # active high
                startup_commands = [CMD_PID_STARTUP, CMD_AFE_STARTUP]
                if not self._send_commands(startup_commands, max_tries=3):
                    # Recover a board whose frame stream stopped unexpectedly.
                    logger.warning("scan startup commands undelivered — hardware reset mid-scan")
                    if progress:
                        progress("recovering", 0.0, 0.0)
                    self._reset_board()
                    self.pump.write(True)
                    if not self._send_commands(startup_commands):
                        raise RuntimeError("sensor board produced no frames after hardware reset")

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
                                # Exhale trace, timed from the start of the blow.
                                channel["samples"].append((
                                    int(dt - purge_ms - baseline_ms), float(value),
                                    float(delta), sample_mv(source, delta),
                                ))
                        if progress:
                            progress(phase, elapsed, total)
            finally:
                # Stop the pump FIRST. It is a direct GPIO write that needs no
                # board traffic, whereas the PID-off handshake below can block
                # for a long time on a slow or wedged board — leaving the pump
                # audibly running long after the test finished.
                try:
                    self.pump.write(False)
                except Exception:
                    logger.exception("could not stop the pump")
                # The test itself is over; the PID-off handshake below can
                # take seconds (lamp shutdown makes the STM32 busy). Expose
                # "finishing" so scan screens don't read it as a live test —
                # a new scan started now simply queues behind this lock.
                self.state = "finishing"
                # Shut down the PID lamp, but deliberately leave AFE sampling
                # on so its doorbell frames can carry the next PID START.
                try:
                    self._send_commands([CMD_PID_SHUTDOWN])
                except Exception:
                    pass
                self.state = "ready"

    def collect_samples(self, seconds: float, progress: Optional[Callable[[float, float], None]] = None,
                        store: bool = True, pump_on: bool = True) -> dict[int, list[tuple[int, float]]]:
        """Run the sensors for `seconds` and return the raw samples per source
        as {source: [(ms_from_start, raw_value), ...]}.

        Used by the calibration procedure, which needs arbitrary-length runs
        (a 10-minute clean, a 1-minute baseline) rather than the fixed
        purge/baseline/measure cycle. Doorbells are serviced throughout, so
        `store=False` still keeps the frame stream alive during long purges.
        """
        with self._lock:
            self.state = "measuring"
            out: dict[int, list[tuple[int, float]]] = {SRC_AD7798: [], SRC_AD5941: []}
            t0: Optional[int] = None
            # Calibration phases are wall-clock durations (a 10 minute purge
            # must be 10 real minutes), so time and report against the clock.
            # Sample timestamps still come from the STM32 tick domain, which is
            # what the integral needs; driving the countdown from those ticks
            # made it jump, since frames deliver records in batches.
            started = time.monotonic()
            no_frame_deadline = started + seconds + 60.0
            try:
                if pump_on:
                    self.pump.write(True)
                startup = [CMD_PID_STARTUP, CMD_AFE_STARTUP]
                if not self._send_commands(startup, max_tries=3):
                    logger.warning("calibration startup undelivered — hardware reset")
                    self._reset_board()
                    if pump_on:
                        self.pump.write(True)
                    if not self._send_commands(startup):
                        raise RuntimeError("sensor board produced no frames after hardware reset")
                    started = time.monotonic()   # do not charge recovery to the run
                    no_frame_deadline = started + seconds + 60.0

                while True:
                    elapsed = time.monotonic() - started
                    if elapsed >= seconds:
                        return out
                    if time.monotonic() > no_frame_deadline:
                        raise RuntimeError("sensor board stopped responding")
                    if progress:
                        progress(min(elapsed, seconds), seconds)
                    records, _ = self._wait_frame()
                    if records is None:
                        continue
                    no_frame_deadline = time.monotonic() + seconds + 60.0
                    for tick, source, value in records:
                        if source not in out:
                            continue
                        if t0 is None:
                            t0 = tick
                        if tick < t0 or not store:
                            continue
                        out[source].append((int(tick - t0), float(value)))
            finally:
                try:
                    self.pump.write(False)
                except Exception:
                    logger.exception("could not stop the pump after calibration sampling")
                self.state = "finishing"
                try:
                    self._send_commands([CMD_PID_SHUTDOWN])
                except Exception:
                    pass
                self.state = "ready"

    def shutdown(self) -> None:
        """Stop the pump and release the GPIO/SPI lines.

        Without this, stopping or restarting the backend mid-scan leaves the
        pump powered: the kernel releases the line on process exit and it
        reverts to an undriven input, so nothing holds the pump off. Drive it
        low explicitly BEFORE closing anything.

        BRD_ON is deliberately NOT driven low: the board is meant to stay
        powered so the STM32 keeps the zero-offset calibration it performs at
        its own boot. Cutting power here would cold-boot it on every service
        restart, and the next start can hit the bus before that calibration
        finishes — which surfaces as "no signal from sensor".
        """
        pump = getattr(self, "pump", None)
        if pump is not None:
            try:
                pump.write(False)
            except Exception:
                logger.warning("could not drive the pump low on shutdown")
        for name in ("pump", "trigger", "ready", "spi"):
            resource = getattr(self, name, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass

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
                samples=tuple(data["samples"]),
            )
        return CycleResult(alcohol=channel(SRC_AD5941), cannabis=channel(SRC_AD7798))


def resolve_analyzer() -> BreathAnalyzer:
    if config.ANALYZER_MODE in {"spi", "live", "hardware"}:
        try:
            analyzer = SpiBreathAnalyzer()
        except Exception as exc:
            return MockAnalyzer(
                startup_warnings=(
                    f"SPI breath board unavailable ({exc}). Using mock readings.",
                )
            )
        # Never leave the pump powered if the process exits (service stop or
        # restart, Ctrl-C, unhandled error) — the released GPIO would float.
        atexit.register(analyzer.shutdown)
        _install_signal_shutdown(analyzer)
        return analyzer
    return MockAnalyzer()


def _install_signal_shutdown(analyzer: BreathAnalyzer) -> None:
    """Stop the pump on SIGTERM/SIGINT (systemctl stop/restart, Ctrl-C).

    atexit alone is not enough: the default SIGTERM handler exits without
    running atexit callbacks. Chains to the previous handler so uvicorn still
    performs its own graceful shutdown.
    """
    def handler(signum, frame, _previous):
        try:
            analyzer.shutdown()
        finally:
            if callable(_previous):
                _previous(signum, frame)
            elif _previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, lambda s, f, p=previous: handler(s, f, p))
        except (ValueError, OSError):
            pass   # not the main thread / unsupported platform
