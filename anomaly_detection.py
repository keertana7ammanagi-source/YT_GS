"""Offline, read-only anomaly detection for already-validated CanSat telemetry.

This is a RULE-BASED / STATISTICAL detector.  It is NOT a trained machine-learning
model, it has not been evaluated on real flight data, and it does not detect all
sensor failures.  See ANOMALY_DETECTION.md for the exact rules and tuning guide.

Safety properties
-----------------
* Pure analysis: imports no serial, socket, Tk or subprocess code and has no way to
  transmit anything.  It never mutates the record it is given.
* Fed by the existing pipeline only (``GroundStation._process_record``); it does not
  read the serial port and does not parse packets.
* Not thread-safe: call it from one thread (the Tk thread in this application).

Alert kinds
-----------
``limit``          a configured engineering limit was violated
``statistical``    robust rolling score (median/MAD) or a sustained level shift
``data_quality``   missing/invalid values, duplicate or backwards TIMESTAMP /
                   PACKET_COUNT, rejected frames, flat-lined sensors (if enabled)
"""
from __future__ import annotations

import json
import math
import statistics
from collections import deque
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from station_control import TELEMETRY_FIELDS

# ---- alert vocabulary -------------------------------------------------------
KIND_LIMIT = "limit"
KIND_STATISTICAL = "statistical"
KIND_DATA_QUALITY = "data_quality"
SEV_INFO, SEV_WARNING, SEV_CRITICAL = "info", "warning", "critical"

STATUS_MONITORING = "Monitoring"
STATUS_ANOMALY = "Potential anomaly detected"
STATUS_INSUFFICIENT = "Insufficient baseline samples"
STATUS_INVALID = "Invalid sensor data"

# ---- field classification (only fields confirmed in station_control) --------
# Identifiers, counters, strings and categorical codes are not "sensor readings".
NON_SENSOR_FIELDS = frozenset({
    "TEAM_ID", "TIMESTAMP", "PACKET_COUNT", "GNSS_TIME", "FLIGHT_SOFTWARE_STATE", "GNSS_FIX",
})
# Angles that wrap around: a plain median/MAD would raise false alarms at the wrap
# point, so they get validity and limit checks but no rolling statistical score.
CIRCULAR_FIELDS = frozenset({"ROLL", "YAW", "GNSS_COURSE"})
# Fields that legitimately change quickly with flight phase.  They are baselined per
# flight-software state and not scored for a few samples after a state change.
DEFAULT_DYNAMIC_FIELDS = frozenset({
    "ALTITUDE", "REL_ALTITUDE", "PRESSURE", "VERTICAL_SPEED", "GNSS_ALTITUDE",
    "GNSS_GROUND_SPEED", "ACCEL_X", "ACCEL_Y", "ACCEL_Z", "GYRO_X", "GYRO_Y", "GYRO_Z",
    "MAG_X", "MAG_Y", "MAG_Z", "HALL_RPM",
})
DEFAULT_MONITORED = tuple(f for f in TELEMETRY_FIELDS if f not in NON_SENSOR_FIELDS)

_MAD_TO_Z = 0.6745  # modified z-score constant (Iglewicz & Hoaglin)
_MISSING = object()
_UNSET = object()


@dataclass
class DetectorConfig:
    """All tunables.  Defaults are generic starting points, not validated limits."""
    window_size: int = 30            # prior valid samples kept per baseline
    min_samples: int = 10            # valid prior samples required before z-scoring
    z_threshold: float = 6.0         # |modified z| >= this -> warning
    z_critical: float = 12.0         # |modified z| >= this -> critical
    min_scale_abs: float = 1e-3      # numerical floor for the robust scale (sensor units)
    min_scale_rel: float = 0.01      # floor as a fraction of |median| (stops tiny-MAD blowups)
    shift_samples: int = 5           # consecutive flagged samples that may form a new level
    shift_tolerance_z: float = 3.0   # how tightly those samples must agree (modified-z units)
    cooldown_samples: int = 20       # min samples between repeat alerts per (sensor, kind)
    settle_samples: int = 5          # samples to skip scoring dynamic fields after a state change
    status_hold_samples: int = 10    # how long the panel keeps showing an anomaly/invalid status
    history_size: int = 200          # alerts retained in memory for diagnosis
    # Engineering limits: {"VOLTAGE": (min, max)}; either bound may be None.
    # EMPTY BY DEFAULT: no hardware limits are known to this code base.
    limits: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    limits_provisional: bool = True  # label limit alerts as provisional (lower severity)
    flatline_fields: tuple[str, ...] = ()   # opt-in stuck-value check, empty = disabled
    flatline_samples: int = 30
    flatline_tolerance: float = 0.0  # max (max-min) over the window still counted as flat
    monitored_fields: tuple[str, ...] | None = None   # None = every sensor field
    dynamic_fields: frozenset = DEFAULT_DYNAMIC_FIELDS

    def __post_init__(self) -> None:
        if self.min_samples < 3:
            raise ValueError("min_samples must be >= 3")
        if self.window_size < self.min_samples:
            raise ValueError("window_size must be >= min_samples")
        if not (math.isfinite(self.z_threshold) and self.z_threshold > 0):
            raise ValueError("z_threshold must be a positive finite number")
        if not (math.isfinite(self.z_critical) and self.z_critical >= self.z_threshold):
            raise ValueError("z_critical must be finite and >= z_threshold")
        for name in ("min_scale_abs", "min_scale_rel", "shift_tolerance_z", "flatline_tolerance"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{name} must be a non-negative finite number")
        if self.min_scale_abs <= 0 and self.min_scale_rel <= 0:
            raise ValueError("at least one scale floor must be positive (avoids division by zero)")
        for name in ("shift_samples", "cooldown_samples", "settle_samples",
                     "status_hold_samples", "history_size", "flatline_samples"):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.shift_samples < 2:
            raise ValueError("shift_samples must be >= 2")
        known = set(TELEMETRY_FIELDS)
        for group_name in ("flatline_fields", "monitored_fields"):
            group = getattr(self, group_name)
            if group is not None:
                bad = [f for f in group if f not in known]
                if bad:
                    raise ValueError(f"{group_name} contains unknown fields: {bad}")
        clean: dict[str, tuple[float | None, float | None]] = {}
        for sensor, bounds in dict(self.limits).items():
            if sensor not in known:
                raise ValueError(f"limits: unknown field {sensor!r}")
            lo, hi = bounds
            for bound in (lo, hi):
                if bound is not None and not math.isfinite(float(bound)):
                    raise ValueError(f"limits[{sensor}] must be finite or None")
            if lo is not None and hi is not None and float(lo) > float(hi):
                raise ValueError(f"limits[{sensor}]: min > max")
            clean[sensor] = (None if lo is None else float(lo), None if hi is None else float(hi))
        self.limits = clean
        self.dynamic_fields = frozenset(self.dynamic_fields)


def load_config(path: str | Path) -> DetectorConfig:
    """Build a DetectorConfig from a JSON file.  Unknown keys are an error."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config file must contain a JSON object")
    allowed = {f.name for f in fields(DetectorConfig)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown config keys: {unknown}")
    if "limits" in data:
        data["limits"] = {k: tuple(v) for k, v in dict(data["limits"]).items()}
    for key in ("flatline_fields", "monitored_fields"):
        if data.get(key) is not None:
            data[key] = tuple(data[key])
    if "dynamic_fields" in data:
        data["dynamic_fields"] = frozenset(data["dynamic_fields"])
    return DetectorConfig(**data)


@dataclass
class Alert:
    sensor: str
    kind: str
    severity: str
    reason: str
    value: float | None
    score: float | None
    timestamp: float | None      # the packet's TIMESTAMP field, when valid
    packet: int | None
    state: int | None            # firmware-reported FLIGHT_SOFTWARE_STATE, when valid
    provisional: bool = False
    repeats: int = 1             # occurrences inside the cooldown (including this one)

    def format_row(self) -> str:
        value = "--" if self.value is None else f"{self.value:.4g}"
        score = "--" if self.score is None else f"{self.score:.2f}"
        stamp = "--" if self.timestamp is None else f"{self.timestamp:g}"
        state = "--" if self.state is None else str(self.state)
        flag = " (provisional)" if self.provisional else ""
        return (f"{stamp} | {self.severity.upper()} | {self.kind}{flag} | {self.sensor} | "
                f"value {value} | score {score} | state {state} | {self.reason}")


def _as_number(value: Any) -> tuple[float | None, str | None]:
    """Return (number, None) or (None, problem).  Never coerces a bad value to a good one."""
    if value is _MISSING or value is None:
        return None, "missing value"
    if isinstance(value, bool):
        return None, "non-numeric value (bool)"
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "empty value"
        try:
            number = float(text)
        except ValueError:
            return None, f"non-numeric value {text[:20]!r}"
    else:
        return None, f"non-numeric value ({type(value).__name__})"
    if math.isnan(number):
        return None, "NaN value"
    if math.isinf(number):
        return None, "infinite value"
    return number, None


class AnomalyDetector:
    """Scores each new validated record against PRIOR samples only."""

    def __init__(self, config: DetectorConfig | None = None):
        self.config = config or DetectorConfig()
        self.history: deque[Alert] = deque(maxlen=max(1, self.config.history_size))
        self.reset()

    # ---- public API ---------------------------------------------------------
    def reset(self) -> None:
        self._tick = 0
        self._baselines: dict[tuple[str, Any], deque[float]] = {}
        self._pending: dict[tuple[str, Any], deque[float]] = {}
        self._flat: dict[str, deque[float]] = {}
        self._last_emit: dict[tuple[str, str], tuple[int, Alert]] = {}
        self._prev_state: Any = _UNSET
        self._settle = 0
        self._prev_ts: float | None = None
        self._prev_packet: float | None = None
        self._last_anomaly_tick = -10**9
        self._last_invalid_tick = -10**9
        self.suppressed = 0
        self.rejected_frames = 0
        self.samples_evaluated = 0
        self.history.clear()

    @property
    def status(self) -> str:
        hold = self.config.status_hold_samples
        if self._tick - self._last_invalid_tick <= hold:
            return STATUS_INVALID
        if self._tick - self._last_anomaly_tick <= hold:
            return STATUS_ANOMALY
        if not any(len(b) >= self.config.min_samples for b in self._baselines.values()):
            return STATUS_INSUFFICIENT
        return STATUS_MONITORING

    def counts(self) -> dict[str, int]:
        result = {KIND_LIMIT: 0, KIND_STATISTICAL: 0, KIND_DATA_QUALITY: 0}
        for alert in self.history:
            result[alert.kind] += 1
        result["suppressed"] = self.suppressed
        result["rejected_frames"] = self.rejected_frames
        return result

    def note_rejected_frame(self, reason: str) -> list[Alert]:
        """Called when the EXISTING parser rejects a frame (the detector never sees raw frames)."""
        self._tick += 1
        self.rejected_frames += 1
        alerts: list[Alert] = []
        self._emit(alerts, "FRAME", KIND_DATA_QUALITY, SEV_WARNING,
                   f"frame rejected by parser: {str(reason)[:120]} (total {self.rejected_frames})",
                   None, None, None, None, None)
        return alerts

    def evaluate(self, record: Mapping[str, Any]) -> list[Alert]:
        """Evaluate one validated record; return NEWLY emitted alerts (after cooldown)."""
        self._tick += 1
        alerts: list[Alert] = []
        if not isinstance(record, Mapping):
            self._emit(alerts, "RECORD", KIND_DATA_QUALITY, SEV_WARNING,
                       "record is not a mapping", None, None, None, None, None)
            return alerts
        self.samples_evaluated += 1
        timestamp, ts_problem = _as_number(record.get("TIMESTAMP", _MISSING))
        packet, pk_problem = _as_number(record.get("PACKET_COUNT", _MISSING))
        state = self._read_state(record, alerts, timestamp, packet)
        ctx = (timestamp, None if packet is None else int(packet), state)

        skip_scoring = False
        if ts_problem:
            self._emit(alerts, "TIMESTAMP", KIND_DATA_QUALITY, SEV_WARNING, f"TIMESTAMP: {ts_problem}",
                       None, None, *ctx)
        else:
            if self._prev_ts is not None and timestamp == self._prev_ts:
                self._emit(alerts, "TIMESTAMP", KIND_DATA_QUALITY, SEV_WARNING,
                           "duplicate TIMESTAMP; sample not scored", timestamp, None, *ctx)
                skip_scoring = True
            elif self._prev_ts is not None and timestamp < self._prev_ts:
                self._emit(alerts, "TIMESTAMP", KIND_DATA_QUALITY, SEV_WARNING,
                           f"TIMESTAMP went backwards ({self._prev_ts:g} -> {timestamp:g})",
                           timestamp, None, *ctx)
            self._prev_ts = timestamp
        if pk_problem:
            self._emit(alerts, "PACKET_COUNT", KIND_DATA_QUALITY, SEV_WARNING, f"PACKET_COUNT: {pk_problem}",
                       None, None, *ctx)
        else:
            if self._prev_packet is not None and packet == self._prev_packet:
                self._emit(alerts, "PACKET_COUNT", KIND_DATA_QUALITY, SEV_WARNING,
                           "duplicate PACKET_COUNT; sample not scored", packet, None, *ctx)
                skip_scoring = True
            elif self._prev_packet is not None and packet < self._prev_packet:
                self._emit(alerts, "PACKET_COUNT", KIND_DATA_QUALITY, SEV_WARNING,
                           f"PACKET_COUNT went backwards ({self._prev_packet:g} -> {packet:g})",
                           packet, None, *ctx)
            self._prev_packet = packet

        if not skip_scoring:
            self._handle_state_change(state)
            for sensor in (self.config.monitored_fields or DEFAULT_MONITORED):
                self._check_sensor(alerts, sensor, record.get(sensor, _MISSING), ctx)
            if self._settle > 0:
                self._settle -= 1
        return alerts

    # ---- internals ----------------------------------------------------------
    def _read_state(self, record, alerts, timestamp, packet) -> int | None:
        raw = record.get("FLIGHT_SOFTWARE_STATE", _MISSING)
        number, problem = _as_number(raw)
        if problem is None and not number.is_integer():
            problem = "non-integer state"
        if problem:
            # Phase unknown: say so, and fall back to phase-less baselines.  Phase is never invented.
            self._emit(alerts, "FLIGHT_SOFTWARE_STATE", KIND_DATA_QUALITY, SEV_INFO,
                       f"flight phase unavailable ({problem}); phase-aware handling disabled for this sample",
                       None, None, timestamp, None if packet is None else int(packet), None)
            return None
        return int(number)

    def _handle_state_change(self, state: int | None) -> None:
        previous = self._prev_state
        self._prev_state = state
        if previous is _UNSET or previous == state or state is None or previous is None:
            return
        # Entering a (possibly re-entered) phase: dynamic fields start a fresh baseline for it.
        self._settle = self.config.settle_samples
        for store in (self._baselines, self._pending):
            for key in [k for k in store if k[1] == state]:
                del store[key]

    def _check_sensor(self, alerts, sensor, raw, ctx) -> None:
        cfg = self.config
        value, problem = _as_number(raw)
        if problem:
            self._emit(alerts, sensor, KIND_DATA_QUALITY, SEV_WARNING,
                       f"{problem}; excluded from baseline", None, None, *ctx)
            return
        limit_reason = self._limit_reason(sensor, value)
        if limit_reason:
            provisional = cfg.limits_provisional
            self._emit(alerts, sensor, KIND_LIMIT, SEV_WARNING if provisional else SEV_CRITICAL,
                       limit_reason, value, None, *ctx, provisional=provisional)
            return  # a limit-violating value never enters the baseline
        if sensor in cfg.flatline_fields:
            self._flatline(alerts, sensor, value, ctx)
        if sensor in CIRCULAR_FIELDS:
            return
        state = ctx[2]
        dynamic = sensor in cfg.dynamic_fields
        if dynamic and self._settle > 0 and state is not None:
            return  # legitimate phase transition: neither scored nor added to the baseline
        key = (sensor, state if dynamic else None)
        baseline = self._baselines.setdefault(key, deque(maxlen=cfg.window_size))
        if len(baseline) < cfg.min_samples:
            baseline.append(value)
            return
        median = statistics.median(baseline)
        mad = statistics.median(abs(x - median) for x in baseline)
        scale = max(mad, cfg.min_scale_abs, cfg.min_scale_rel * abs(median))
        z = _MAD_TO_Z * (value - median) / scale
        pending = self._pending.setdefault(key, deque(maxlen=cfg.shift_samples))
        if abs(z) < cfg.z_threshold:
            pending.clear()
            baseline.append(value)      # only unflagged samples may update the baseline
            return
        pending.append(value)           # flagged: quarantined, baseline untouched
        if len(pending) == cfg.shift_samples and self._agree(pending, scale):
            new_level = statistics.median(pending)
            self._emit(alerts, sensor, KIND_STATISTICAL, SEV_WARNING,
                       f"sustained level shift: last {cfg.shift_samples} samples agree near {new_level:.4g} "
                       f"(old baseline median {median:.4g}); treated as the new normal from now on - "
                       f"this could be a real change OR a stuck/offset sensor",
                       value, z, *ctx)
            baseline.clear()
            baseline.extend(pending)
            pending.clear()
            return
        severity = SEV_CRITICAL if abs(z) >= cfg.z_critical else SEV_WARNING
        self._emit(alerts, sensor, KIND_STATISTICAL, severity,
                   f"outlier vs rolling median {median:.4g} (robust scale {scale:.3g}, "
                   f"{len(pending)} consecutive flagged)", value, z, *ctx)

    def _agree(self, samples, scale: float) -> bool:
        centre = statistics.median(samples)
        return all(_MAD_TO_Z * abs(x - centre) / scale <= self.config.shift_tolerance_z for x in samples)

    def _limit_reason(self, sensor: str, value: float) -> str | None:
        bounds = self.config.limits.get(sensor)
        if not bounds:
            return None
        lo, hi = bounds
        tag = " [provisional limit]" if self.config.limits_provisional else ""
        if lo is not None and value < lo:
            return f"below configured minimum {lo:g}{tag}"
        if hi is not None and value > hi:
            return f"above configured maximum {hi:g}{tag}"
        return None

    def _flatline(self, alerts, sensor, value, ctx) -> None:
        cfg = self.config
        window = self._flat.setdefault(sensor, deque(maxlen=max(2, cfg.flatline_samples)))
        window.append(value)
        if len(window) == window.maxlen and max(window) - min(window) <= cfg.flatline_tolerance:
            self._emit(alerts, sensor, KIND_DATA_QUALITY, SEV_WARNING,
                       f"flat-lined: no change over last {len(window)} samples", value, None, *ctx)

    def _emit(self, alerts, sensor, kind, severity, reason, value, score,
              timestamp, packet, state, provisional: bool = False) -> None:
        key = (sensor, kind)
        last = self._last_emit.get(key)
        if last is not None and self._tick - last[0] < self.config.cooldown_samples:
            last[1].repeats += 1        # suppressed from the UI, counted for diagnosis
            self.suppressed += 1
            self._mark(kind)
            return
        alert = Alert(sensor, kind, severity, reason, value, score, timestamp, packet, state, provisional)
        self._last_emit[key] = (self._tick, alert)
        self.history.append(alert)
        alerts.append(alert)
        self._mark(kind)

    def _mark(self, kind: str) -> None:
        if kind == KIND_DATA_QUALITY:
            self._last_invalid_tick = self._tick
        else:
            self._last_anomaly_tick = self._tick
