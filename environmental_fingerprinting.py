"""Offline environmental fingerprinting of validated CanSat telemetry.

WHAT THIS IS: offline, unsupervised, AI-assisted *statistical* analysis (descriptive statistics, trends, and
K-means clustering with feature scaling).  It is NOT a trained model, has no labelled ground truth, and has not
been validated against real flight data.  It never names a place, weather event or atmospheric layer.

SAFETY: pure analysis on records you pass in.  No serial, network, command or file-writing code; input records
are never modified.  Heavy work belongs in a worker thread (the dashboard does that), never the serial thread.

Features (all exist in station_control.TELEMETRY_FIELDS; units follow the existing dashboard labels):
  TEMP (degC), PRESSURE (Pa), HUMIDITY (%RH), LIGHT_LUX (lux; unit taken from the field name), UV_INDEX (index).
ALTITUDE (m, firmware-reported barometric altitude) is CONTEXT ONLY - it is not clustered, because it is not an
independent measurement of the atmosphere (altitude and pressure carry overlapping information) and REL_ALTITUDE
is a ground-station-derived copy of the same quantity.  FLIGHT_SOFTWARE_STATE (firmware-reported) is context only.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

import numpy as np

from station_control import STATE_NAMES

# (field, label, unit)
FEATURES = (
    ("TEMP", "Temperature", "\u00b0C"),
    ("PRESSURE", "Pressure", "Pa"),
    ("HUMIDITY", "Relative humidity", "%RH"),
    ("LIGHT_LUX", "Light intensity", "lux"),
    ("UV_INDEX", "UV index", "index"),
)
FEATURE_NAMES = tuple(f[0] for f in FEATURES)
FEATURE_LABELS = {f[0]: f[1] for f in FEATURES}
FEATURE_UNITS = {f[0]: f[2] for f in FEATURES}
ALTITUDE_FIELD = "ALTITUDE"
STATE_FIELD = "FLIGHT_SOFTWARE_STATE"
# Columns a caller must supply (used by the dashboard when it reads the database).
REQUIRED_COLUMNS = ("TEAM_ID", "TIMESTAMP", ALTITUDE_FIELD, STATE_FIELD, *FEATURE_NAMES)

STATUS_COMPLETE = "complete"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_FAILED = "failed"

# TEAM_ID prefix written by the ground station's own Simulation Mode (SimulationWorker uses "SIM-0001").  Rows carrying
# it are treated as simulated, never as real flight telemetry.  This is an inference from that convention.
SIMULATION_TEAM_PREFIX = "SIM-"

METHOD_NAME = "Offline AI-assisted statistical analysis: descriptive statistics + K-means clustering (unsupervised)"


@dataclass(frozen=True)
class FingerprintConfig:
    min_observations: int = 10               # below this: explicit "insufficient data", no analysis
    min_samples_for_clustering: int = 30     # complete rows needed before K-means is attempted
    min_feature_coverage: float = 0.5        # features with less valid coverage are not clustered
    k_min: int = 2
    k_max: int = 4
    min_cluster_size: int = 5
    min_cluster_fraction: float = 0.05
    min_silhouette: float = 0.25             # heuristic: below this the groups are not called "distinct"
    min_gap_ratio: float = 3.0               # heuristic: gap between groups vs typical neighbour spacing
    n_init: int = 10
    max_iter: int = 100
    silhouette_max_samples: int = 800
    seed: int = 20260
    constant_rel_tol: float = 1e-9
    max_chart_points: int = 1500
    log_features: tuple[str, ...] = ("LIGHT_LUX",)   # spans orders of magnitude -> log1p before scaling
    # Definitional bounds only (not sensor limits).  None = unbounded on that side.
    bounds: Mapping[str, tuple[float | None, float | None]] = field(default_factory=lambda: {
        "TEMP": (-273.15, None), "PRESSURE": (0.0, None), "HUMIDITY": (0.0, 100.0),
        "LIGHT_LUX": (0.0, None), "UV_INDEX": (0.0, None),
    })

    def __post_init__(self):
        if self.k_min < 2 or self.k_max < self.k_min:
            raise ValueError("need 2 <= k_min <= k_max")
        if self.min_observations < 2 or self.min_samples_for_clustering < 2 * self.min_cluster_size:
            raise ValueError("min_observations/min_samples_for_clustering too small")


@dataclass
class FeatureStats:
    name: str
    label: str
    unit: str
    n_valid: int = 0
    coverage: float = 0.0
    n_invalid: int = 0
    n_out_of_range: int = 0
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    std: float | None = None
    median: float | None = None
    used_in_clustering: bool = False
    note: str = ""


@dataclass
class FeatureTrend:
    name: str
    label: str
    unit: str
    n: int
    slope_per_s: float | None
    net_change: float | None          # median(last quarter) - median(first quarter)
    altitude_correlation: float | None


@dataclass
class PhaseSummary:
    state: int
    state_name: str
    n: int
    medians: dict[str, float | None]
    altitude_median: float | None


@dataclass
class ClusterSummary:
    letter: str
    name: str
    description: str
    n: int
    share: float
    t_min: float
    t_max: float
    t_median: float
    medians: dict[str, float | None]
    ranges: dict[str, tuple[float, float] | None]
    centroid_z: dict[str, float]
    altitude: tuple[float, float, float] | None     # (min, median, max)
    states: dict[str, int]


@dataclass
class ClusteringInfo:
    attempted: bool = False
    found: bool = False
    reason: str = ""
    method: str = "K-means (own numpy implementation, k-means++ init, deterministic seed)"
    k: int = 0
    silhouette: float | None = None
    gap_ratio: float | None = None
    features_used: list[str] = field(default_factory=list)
    n_samples: int = 0
    seed: int = 0
    candidates: list[tuple[int, float | None, float | None, str]] = field(default_factory=list)   # (k, silhouette, gap ratio, note)


@dataclass
class ChartSeries:
    t: list[float] = field(default_factory=list)
    values: dict[str, list[float | None]] = field(default_factory=dict)
    altitude: list[float | None] = field(default_factory=list)
    labels: list[int] = field(default_factory=list)      # cluster index per point, -1 = unassigned


@dataclass
class FingerprintResult:
    status: str
    message: str
    fingerprint_id: str | None = None
    name: str = ""
    scope_label: str = ""
    data_source: str = ""
    team_ids: list[str] = field(default_factory=list)
    simulated_team_ids: list[str] = field(default_factory=list)
    interval_requested: tuple[float | None, float | None] = (None, None)
    n_records_input: int = 0
    n_outside_interval: int = 0
    n_observations: int = 0          # valid timestamp + >= 1 valid environmental value
    n_complete: int = 0              # rows valid for every clustered feature
    t_min: float | None = None
    t_max: float | None = None
    dropped: dict[str, int] = field(default_factory=dict)
    feature_stats: list[FeatureStats] = field(default_factory=list)
    clustering: ClusteringInfo = field(default_factory=ClusteringInfo)
    clusters: list[ClusterSummary] = field(default_factory=list)
    trends: list[FeatureTrend] = field(default_factory=list)
    phases: list[PhaseSummary] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)
    series: ChartSeries = field(default_factory=ChartSeries)
    obs_t: list[float] = field(default_factory=list)       # full-resolution, time-sorted (for alert association)
    obs_label: list[int] = field(default_factory=list)
    method: str = METHOD_NAME
    computed_at: str = field(default="", compare=False)


# ---------------------------------------------------------------------------------------------- helpers
def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value.strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _state_name(state: int) -> str:
    return STATE_NAMES[state] if 0 <= state < len(STATE_NAMES) else f"STATE {state}"


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}g}"


def failed_result(message: str, scope_label: str = "", data_source: str = "") -> FingerprintResult:
    return FingerprintResult(
        status=STATUS_FAILED, message=f"Analysis failed: {message}", scope_label=scope_label,
        data_source=data_source, computed_at=_now(),
        limitations=["No fingerprint was produced; no environmental conclusions can be drawn from this run."],
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _kmeanspp(x: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    centers = [x[int(rng.integers(len(x)))]]
    for _ in range(1, k):
        d2 = np.min(((x[:, None, :] - np.array(centers)[None, :, :]) ** 2).sum(axis=2), axis=1)
        total = d2.sum()
        if total <= 0:
            centers.append(x[int(rng.integers(len(x)))])
        else:
            centers.append(x[int(rng.choice(len(x), p=d2 / total))])
    return np.array(centers)


def _kmeans(x: np.ndarray, k: int, seed: int, n_init: int, max_iter: int) -> tuple[np.ndarray, np.ndarray, float]:
    """Deterministic Lloyd K-means with k-means++ starts.  Returns (labels, centers, inertia)."""
    rng = np.random.default_rng([seed, k])
    best: tuple[np.ndarray, np.ndarray, float] | None = None
    for _ in range(n_init):
        centers = _kmeanspp(x, k, rng)
        for _ in range(max_iter):
            d2 = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
            labels = d2.argmin(axis=1)
            new = centers.copy()
            for j in range(k):
                members = labels == j
                new[j] = x[members].mean(axis=0) if members.any() else x[int(d2.min(axis=1).argmax())]
            if np.allclose(new, centers, atol=1e-10):
                centers = new
                break
            centers = new
        d2 = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        labels = d2.argmin(axis=1)
        inertia = float(d2[np.arange(len(x)), labels].sum())
        if best is None or inertia < best[2] - 1e-12:
            best = (labels, centers, inertia)
    assert best is not None
    return best


def _separation(x: np.ndarray, labels: np.ndarray, max_samples: int, seed: int) -> tuple[float | None, float | None]:
    """(silhouette, gap_ratio) on a deterministic subsample.

    gap_ratio = 3rd-smallest cross-group nearest distance / median nearest-neighbour distance.  Silhouette alone is
    also high for a smooth continuous gradient cut in two; a gap ratio near 1 shows the groups merely touch.
    """
    n = len(x)
    if n > max_samples:
        idx = np.sort(np.random.default_rng([seed, 7919]).choice(n, max_samples, replace=False))
        x, labels = x[idx], labels[idx]
    present = np.unique(labels)
    if len(present) < 2:
        return None, None
    dist = np.sqrt(((x[:, None, :] - x[None, :, :]) ** 2).sum(axis=2))
    scores = np.zeros(len(x))
    cross = np.full(len(x), np.inf)
    for i in range(len(x)):
        other_mask = labels != labels[i]
        cross[i] = dist[i, other_mask].min()
        same = labels == labels[i]
        if same.sum() <= 1:
            continue                                         # singleton cluster -> score 0 by convention
        a = dist[i, same].sum() / (same.sum() - 1)
        b = min(dist[i, labels == other].mean() for other in present if other != labels[i])
        denom = max(a, b)
        scores[i] = 0.0 if denom == 0 else (b - a) / denom
    off_diag = dist + np.diag(np.full(len(x), np.inf))
    nn = float(np.median(off_diag.min(axis=1)))
    gap = float(np.sort(cross)[min(2, len(cross) - 1)])
    ratio = gap / nn if nn > 0 else float("inf")
    return float(scores.mean()), ratio


def _stats(values: np.ndarray) -> tuple[float, float, float, float, float]:
    std = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    return float(values.min()), float(values.max()), float(values.mean()), std, float(np.median(values))


# ---------------------------------------------------------------------------------------------- public API
def analyze_environment(records: Iterable[Mapping[str, Any]], config: FingerprintConfig | None = None,
                        t_start: float | None = None, t_end: float | None = None,
                        scope_label: str = "", data_source: str = "") -> FingerprintResult:
    """Analyse validated telemetry records.  Never raises: failures are returned as status 'failed'."""
    cfg = config or FingerprintConfig()
    try:
        return _analyze(records, cfg, t_start, t_end, scope_label, data_source)
    except Exception as exc:  # noqa: BLE001 - reported to the operator, never shown as success
        return failed_result(f"{type(exc).__name__}: {exc}", scope_label, data_source)


def _analyze(records, cfg: FingerprintConfig, t_start, t_end, scope_label, data_source) -> FingerprintResult:
    rows = records if isinstance(records, (list, tuple)) else list(records)
    names = list(FEATURE_NAMES)
    dropped: Counter[str] = Counter()
    counts = {n: {"invalid": 0, "out_of_range": 0} for n in names}
    t_list: list[float] = []
    vals: list[list[float]] = []
    alts: list[float] = []
    states: list[int] = []
    teams: set[str] = set()
    n_outside = decreases = team_changes = 0
    prev_t: float | None = None
    prev_team: str | None = None

    for row in rows:
        if not isinstance(row, Mapping):
            dropped["record is not a mapping"] += 1
            continue
        team = row.get("TEAM_ID")
        team_text = str(team).strip() if team is not None else ""
        t = _num(row.get("TIMESTAMP"))
        if t is None:
            dropped["invalid or missing TIMESTAMP"] += 1
            continue
        if team_text:
            teams.add(team_text)
            if prev_team is not None and team_text != prev_team:
                team_changes += 1
            prev_team = team_text
        if prev_t is not None and t < prev_t:
            decreases += 1
        prev_t = t
        if (t_start is not None and t < t_start) or (t_end is not None and t > t_end):
            n_outside += 1
            continue
        row_vals: list[float] = []
        any_valid = False
        for name in names:
            v = _num(row.get(name))
            if v is None:
                counts[name]["invalid"] += 1
                row_vals.append(math.nan)
                continue
            lo, hi = cfg.bounds.get(name, (None, None))
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                counts[name]["out_of_range"] += 1
                row_vals.append(math.nan)
                continue
            row_vals.append(v)
            any_valid = True
        if not any_valid:
            dropped["no valid environmental value"] += 1
            continue
        alt = _num(row.get(ALTITUDE_FIELD))
        st = _num(row.get(STATE_FIELD))
        t_list.append(t)
        vals.append(row_vals)
        alts.append(math.nan if alt is None else alt)
        states.append(int(st) if st is not None and float(st).is_integer() else -1)

    n_obs = len(t_list)
    base = FingerprintResult(
        status=STATUS_COMPLETE, message="", scope_label=scope_label, data_source=data_source,
        team_ids=sorted(teams), interval_requested=(t_start, t_end), n_records_input=len(rows),
        n_outside_interval=n_outside, n_observations=n_obs, dropped=dict(dropped), computed_at=_now(),
    )
    warnings = base.warnings
    simulated = sorted(t for t in teams if t.upper().startswith(SIMULATION_TEAM_PREFIX))
    base.simulated_team_ids = simulated
    if simulated and len(simulated) == len(teams):
        base.data_source = f"SIMULATED telemetry (Simulation Mode, TEAM_ID {', '.join(simulated)}) - not real flight data"
        warnings.append("All analysed rows come from the application's Simulation Mode (TEAM_ID starts with "
                        f"'{SIMULATION_TEAM_PREFIX}'); the results describe simulated data, not a real environment.")
    elif simulated:
        warnings.append(f"Some analysed rows come from Simulation Mode (TEAM_ID {', '.join(simulated)}) and are mixed with "
                        "other data; the results do not describe a single real flight.")
    if dropped:
        warnings.append("Records excluded: " + "; ".join(f"{v} x {k}" for k, v in dropped.items()) + ".")
    if len(teams) > 1:
        warnings.append(f"The data contain {len(teams)} different TEAM_ID values ({', '.join(sorted(teams))}); "
                        "they were analysed together - the selected scope may mix unrelated missions.")
    if decreases or team_changes:
        warnings.append(f"Possible mission boundary inside the data ({decreases} TIMESTAMP decrease(s), "
                        f"{team_changes} TEAM_ID change(s)); mission boundaries cannot be identified reliably, "
                        "so all rows in scope were analysed together.")

    if n_obs < cfg.min_observations:
        base.status = STATUS_INSUFFICIENT
        base.message = (f"Insufficient valid data: {n_obs} valid observation(s) in scope; at least "
                        f"{cfg.min_observations} are required for a fingerprint.")
        base.limitations = ["No fingerprint, clusters or trends were produced because the data are insufficient."]
        if n_obs:
            order = np.argsort(np.array(t_list), kind="stable")
            base.t_min, base.t_max = float(min(t_list)), float(max(t_list))
            base.feature_stats = _feature_stats(np.array(vals)[order], n_obs, counts, set())
        return base

    order = np.argsort(np.array(t_list), kind="stable")
    t = np.array(t_list)[order]
    feat = np.array(vals, dtype=float)[order]
    alt = np.array(alts, dtype=float)[order]
    state = np.array(states, dtype=int)[order]
    base.t_min, base.t_max = float(t[0]), float(t[-1])
    duplicates = int((np.diff(t) == 0).sum())
    if duplicates:
        warnings.append(f"{duplicates} observation(s) share a TIMESTAMP with the previous one (kept; not de-duplicated).")

    # ---- which features may be clustered
    usable: list[int] = []
    notes: dict[str, str] = {}
    for j, name in enumerate(names):
        col = feat[:, j]
        ok = col[np.isfinite(col)]
        coverage = len(ok) / n_obs
        if len(ok) < 2:
            notes[name] = "not enough valid values"
        elif coverage < cfg.min_feature_coverage:
            notes[name] = f"coverage {coverage:.0%} below {cfg.min_feature_coverage:.0%}"
        elif float(ok.max() - ok.min()) <= cfg.constant_rel_tol * max(1.0, abs(float(np.median(ok)))):
            notes[name] = "constant value (no variation to cluster)"
        else:
            usable.append(j)
    base.feature_stats = _feature_stats(feat, n_obs, counts, {names[j] for j in usable}, notes)
    for stat in base.feature_stats:
        if stat.n_valid and stat.coverage < 1.0:
            warnings.append(f"{stat.label} has valid data for {stat.coverage:.0%} of observations "
                            f"({stat.n_invalid} missing/invalid, {stat.n_out_of_range} out of range).")
        elif stat.n_valid == 0:
            warnings.append(f"{stat.label} has no valid data in scope.")

    complete = np.isfinite(feat[:, usable]).all(axis=1) if usable else np.zeros(n_obs, dtype=bool)
    base.n_complete = int(complete.sum())

    # ---- trends, phases (descriptive; do not need clustering)
    base.trends = _trends(t, feat, alt, cfg)
    base.phases = _phases(feat, alt, state)

    # ---- clustering
    labels_full = np.full(n_obs, -1, dtype=int)
    info = base.clustering
    info.seed = cfg.seed
    info.features_used = [names[j] for j in usable]
    if len(usable) < 2:
        info.reason = ("Clustering not attempted: fewer than two environmental features have enough valid, "
                       "varying data.")
    elif base.n_complete < cfg.min_samples_for_clustering:
        info.reason = (f"Clustering not attempted: {base.n_complete} complete observation(s); at least "
                       f"{cfg.min_samples_for_clustering} are required.")
    else:
        _cluster(base, cfg, t, feat, alt, state, usable, complete, labels_full)

    # ---- chart data, id, narrative
    base.obs_t = [float(v) for v in t]
    base.obs_label = [int(v) for v in labels_full]
    base.series = _series(t, feat, alt, labels_full, cfg)
    base.limitations = _limitations(base)
    base.fingerprint_id = _fingerprint_id(base)
    base.name = _fingerprint_name(base)
    base.explanation = _explain(base)
    base.message = (f"Analysis complete: {n_obs} valid observations"
                    + (f", {len(base.clusters)} environmental pattern(s) found." if base.clusters
                       else "; statistics only (no distinct patterns reported)."))
    return base


def _feature_stats(feat: np.ndarray, n_obs: int, counts, clustered: set[str], notes: dict[str, str] | None = None
                   ) -> list[FeatureStats]:
    out = []
    for j, (name, label, unit) in enumerate(FEATURES):
        col = feat[:, j]
        ok = col[np.isfinite(col)]
        stat = FeatureStats(name, label, unit, n_valid=int(len(ok)), coverage=len(ok) / n_obs if n_obs else 0.0,
                            n_invalid=counts[name]["invalid"], n_out_of_range=counts[name]["out_of_range"],
                            used_in_clustering=name in clustered, note=(notes or {}).get(name, ""))
        if len(ok):
            stat.minimum, stat.maximum, stat.mean, stat.std, stat.median = _stats(ok)
        out.append(stat)
    return out


def _trends(t, feat, alt, cfg) -> list[FeatureTrend]:
    out = []
    for j, (name, label, unit) in enumerate(FEATURES):
        ok = np.isfinite(feat[:, j])
        n = int(ok.sum())
        slope = net = corr = None
        if n >= 10 and float(t[ok].max() - t[ok].min()) > 0:
            tt, vv = t[ok], feat[ok, j]
            slope = float(np.polyfit(tt - tt.mean(), vv, 1)[0])
            q = max(1, n // 4)
            net = float(np.median(vv[-q:]) - np.median(vv[:q]))
            both = ok & np.isfinite(alt)
            if both.sum() >= 10 and np.std(feat[both, j]) > 0 and np.std(alt[both]) > 0:
                corr = float(np.corrcoef(feat[both, j], alt[both])[0, 1])
        out.append(FeatureTrend(name, label, unit, n, slope, net, corr))
    return out


def _phases(feat, alt, state) -> list[PhaseSummary]:
    out = []
    for s in sorted(set(int(v) for v in state if v >= 0)):
        mask = state == s
        medians = {}
        for j, name in enumerate(FEATURE_NAMES):
            col = feat[mask, j]
            col = col[np.isfinite(col)]
            medians[name] = float(np.median(col)) if len(col) else None
        a = alt[mask]
        a = a[np.isfinite(a)]
        out.append(PhaseSummary(s, _state_name(s), int(mask.sum()), medians, float(np.median(a)) if len(a) else None))
    return out


def _cluster(base: FingerprintResult, cfg, t, feat, alt, state, usable, complete, labels_full) -> None:
    info = base.clustering
    info.attempted = True
    idx = np.flatnonzero(complete)
    raw = feat[np.ix_(idx, usable)].copy()
    for pos, j in enumerate(usable):
        if FEATURE_NAMES[j] in cfg.log_features:
            raw[:, pos] = np.log1p(raw[:, pos])
    mean = raw.mean(axis=0)
    std = raw.std(axis=0)
    std[std == 0] = 1.0                                   # a column constant on the complete rows -> all zeros
    x = (raw - mean) / std
    n = len(x)
    min_size = max(cfg.min_cluster_size, math.ceil(cfg.min_cluster_fraction * n))
    best: tuple[float, int, np.ndarray, np.ndarray, float] | None = None       # distinct candidates only
    fallback: tuple[float, float, int] | None = None                           # best (sil, gap, k) overall
    for k in range(cfg.k_min, min(cfg.k_max, n // min_size) + 1):
        labels, centers, _ = _kmeans(x, k, cfg.seed, cfg.n_init, cfg.max_iter)
        sizes = np.bincount(labels, minlength=k)
        if sizes.min() < min_size:
            info.candidates.append((k, None, None, f"rejected: smallest group has {int(sizes.min())} < {min_size} samples"))
            continue
        sil, gap = _separation(x, labels, cfg.silhouette_max_samples, cfg.seed)
        if sil is None:
            continue
        distinct = sil >= cfg.min_silhouette and gap >= cfg.min_gap_ratio
        info.candidates.append((k, sil, gap, "distinct" if distinct else "not distinct"))
        if fallback is None or sil > fallback[0] + 1e-12:
            fallback = (sil, gap, k)
        if distinct and (best is None or sil > best[0] + 1e-12):
            best = (sil, k, labels, centers, gap)
    info.n_samples = n
    if best is None:
        if fallback is None:
            info.reason = "No valid clustering candidate (groups too small or not separable)."
        else:
            info.silhouette, info.gap_ratio = fallback[0], fallback[1]
            info.reason = (f"No distinct groups: best silhouette {fallback[0]:.2f} (k={fallback[2]}) with a group-gap ratio of "
                           f"{fallback[1]:.1f} did not meet the thresholds (silhouette >= {cfg.min_silhouette:.2f} and gap ratio "
                           f">= {cfg.min_gap_ratio:.1f}); the readings vary continuously or in one regime, so they are "
                           "described as a single pattern.")
        return
    sil, k, labels, centers, gap = best
    info.silhouette, info.gap_ratio = sil, gap
    info.found, info.k = True, k
    info.reason = (f"{k} groups selected by highest silhouette score ({sil:.2f}) among candidates that were clearly "
                   f"separated (group-gap ratio {gap:.1f}).")
    # name clusters by order of appearance (median mission time)
    t_idx = t[idx]
    rank = np.argsort([float(np.median(t_idx[labels == j])) for j in range(k)], kind="stable")
    relabel = {int(old): new for new, old in enumerate(rank)}
    new_labels = np.array([relabel[int(v)] for v in labels])
    labels_full[idx] = new_labels
    for new in range(k):
        old = int(rank[new])
        members = idx[new_labels == new]
        z = {FEATURE_NAMES[j]: float(centers[old][pos]) for pos, j in enumerate(usable)}
        medians, ranges = {}, {}
        for j, name in enumerate(FEATURE_NAMES):
            col = feat[members, j]
            col = col[np.isfinite(col)]
            medians[name] = float(np.median(col)) if len(col) else None
            ranges[name] = (float(col.min()), float(col.max())) if len(col) else None
        a = alt[members]
        a = a[np.isfinite(a)]
        altitude = (float(a.min()), float(np.median(a)), float(a.max())) if len(a) else None
        state_counts = Counter(_state_name(int(s)) for s in state[members] if s >= 0)
        letter = chr(ord("A") + new)
        desc = _describe_cluster(z, altitude)
        base.clusters.append(ClusterSummary(
            letter, f"Pattern {letter}", desc, int(len(members)), len(members) / n, float(t[members].min()),
            float(t[members].max()), float(np.median(t[members])), medians, ranges, z, altitude, dict(state_counts)))


def _describe_cluster(z: dict[str, float], altitude) -> str:
    bits = []
    for name, value in sorted(z.items(), key=lambda kv: -abs(kv[1])):
        if abs(value) >= 0.5 and len(bits) < 3:
            bits.append(f"{'higher' if value > 0 else 'lower'} {FEATURE_LABELS[name].lower()}")
    text = ", ".join(bits) if bits else "readings close to the dataset average"
    if altitude:
        text += f" (altitude {altitude[0]:.0f}-{altitude[2]:.0f} m)"
    return text


def _series(t, feat, alt, labels, cfg) -> ChartSeries:
    n = len(t)
    idx = np.arange(n) if n <= cfg.max_chart_points else np.unique(np.linspace(0, n - 1, cfg.max_chart_points).astype(int))
    def clean(a):
        return [None if not math.isfinite(v) else float(v) for v in a[idx]]
    return ChartSeries(t=[float(v) for v in t[idx]], values={name: clean(feat[:, j]) for j, name in enumerate(FEATURE_NAMES)},
                       altitude=clean(alt), labels=[int(v) for v in labels[idx]])


def _limitations(r: FingerprintResult) -> list[str]:
    out = [
        "Unsupervised statistical analysis without labelled ground truth; it has not been validated on real flight data.",
        "Patterns describe how the measurements vary within this dataset; they do not identify a location, weather "
        "event, atmospheric layer or cause.",
        "Pressure, temperature and light all change with altitude, time and the sensors' own thermal/solar exposure; "
        "this analysis cannot separate those effects.",
    ]
    if r.clustering.found:
        out.append("The number of groups is chosen by silhouette and group-gap heuristics; different k or scaling could group "
                   "the data differently.")
    if r.n_observations < 100:
        out.append(f"Only {r.n_observations} valid observations were available; results are weakly supported.")
    if not r.clustering.found:
        out.append("Clustering did not report distinct groups: " + (r.clustering.reason or "see statistics."))
    return out


def _fingerprint_id(r: FingerprintResult) -> str:
    payload = {
        "n": r.n_observations, "t": [round(r.t_min or 0, 3), round(r.t_max or 0, 3)], "f": r.clustering.features_used,
        "k": len(r.clusters),
        "m": [[round(s.median, 3) if s.median is not None else None] for s in r.feature_stats],
    }
    return "ENV-" + hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8].upper()


def _fingerprint_name(r: FingerprintResult) -> str:
    span = f"mission time {r.t_min:g}-{r.t_max:g} s"
    pattern = f"{len(r.clusters)} environmental patterns" if r.clusters else "single environmental pattern (no distinct groups)"
    return f"{r.fingerprint_id}: {pattern}, {span}"


def _strength(r: float) -> str:
    a = abs(r)
    return "strong" if a >= 0.7 else "moderate" if a >= 0.4 else "weak"


def _explain(r: FingerprintResult) -> list[str]:
    out = [f"{r.n_observations} valid observations were analysed over mission time {r.t_min:g}-{r.t_max:g} s "
           f"({r.n_complete} complete across the {len(r.clustering.features_used)} clustered feature(s))."]
    for tr in r.trends:
        if tr.net_change is None:
            continue
        direction = "rose" if tr.net_change > 0 else "fell" if tr.net_change < 0 else "did not change"
        out.append(f"{tr.label} {direction} by {abs(tr.net_change):.4g} {tr.unit} between the first and last quarter of "
                   f"the observations (linear slope {tr.slope_per_s:.3g} {tr.unit}/s).")
    correlated = [tr for tr in r.trends if tr.altitude_correlation is not None and abs(tr.altitude_correlation) >= 0.4]
    for tr in correlated:
        out.append(f"{tr.label} has a {_strength(tr.altitude_correlation)} "
                   f"{'positive' if tr.altitude_correlation > 0 else 'negative'} correlation with ALTITUDE "
                   f"(r = {tr.altitude_correlation:.2f}).")
    if correlated:
        out.append("Correlation with ALTITUDE is shown for context. If the firmware derives ALTITUDE from PRESSURE (not "
                   "confirmed in the ground-station code), the pressure-altitude correlation is not independent evidence, "
                   "and correlations only describe this dataset.")
    if r.clusters:
        out.append(f"K-means grouped the complete observations into {len(r.clusters)} patterns "
                   f"(silhouette {r.clustering.silhouette:.2f}).")
        for c in r.clusters:
            mix = ", ".join(f"{name} {count}" for name, count in sorted(c.states.items(), key=lambda kv: -kv[1])) or "unknown"
            out.append(f"{c.name}: {c.n} observations ({c.share:.0%}), mission time {c.t_min:g}-{c.t_max:g} s, "
                       f"{c.description}; reported flight states: {mix}.")
        out.append("Pattern differences are statistical groupings of the readings; any link to flight phase or altitude is "
                   "an association in this dataset, not an established cause.")
    else:
        out.append("No distinct environmental groups were reported; the readings are summarised as a single pattern.")
    return out


def format_fingerprint_text(r: FingerprintResult) -> str:
    """Plain-text rendering used by the dashboard tab (the PDF builds its own tables from the same object)."""
    if r.status == STATUS_FAILED:
        return r.message + "\n"
    lines = [r.message, ""]
    if r.status == STATUS_INSUFFICIENT:
        if r.feature_stats:
            lines.append("Valid values found so far:")
            lines += [f"  {s.label}: {s.n_valid}" for s in r.feature_stats]
        lines += [""] + [f"! {w}" for w in r.warnings]
        return "\n".join(lines) + "\n"
    lines += [f"Fingerprint: {r.name}", f"Scope: {r.scope_label or 'n/a'}", f"Data source: {r.data_source or 'n/a'}",
              f"Method: {r.method}", f"Computed at: {r.computed_at}", "",
              f"Records in scope: {r.n_records_input}  |  valid observations: {r.n_observations}  |  complete: {r.n_complete}",
              "", "ENVIRONMENTAL STATISTICS (raw units)",
              f"{'Feature':<18}{'unit':<6}{'n':>6}{'cover':>7}{'min':>11}{'median':>11}{'max':>11}  clustered"]
    for s in r.feature_stats:
        lines.append(f"{s.label:<18}{s.unit:<6}{s.n_valid:>6}{s.coverage:>7.0%}{_fmt(s.minimum):>11}{_fmt(s.median):>11}"
                     f"{_fmt(s.maximum):>11}  {'yes' if s.used_in_clustering else 'no' + (' (' + s.note + ')' if s.note else '')}")
    lines += ["", f"CLUSTERING: {r.clustering.reason}"]
    for c in r.clusters:
        lines.append(f"  {c.name}  n={c.n} ({c.share:.0%})  t={c.t_min:g}-{c.t_max:g}s  {c.description}")
        lines.append("      medians: " + ", ".join(f"{n}={_fmt(v)}" for n, v in c.medians.items() if v is not None))
    lines += ["", "TRENDS (first vs last quarter)"]
    for tr in r.trends:
        if tr.net_change is not None:
            corr = "" if tr.altitude_correlation is None else f", r(altitude)={tr.altitude_correlation:.2f}"
            lines.append(f"  {tr.label}: change {tr.net_change:+.4g} {tr.unit}{corr}")
    lines += ["", "INTERPRETATION (algorithm-generated)"] + [f"  - {e}" for e in r.explanation]
    if r.warnings:
        lines += ["", "DATA-QUALITY WARNINGS"] + [f"  ! {w}" for w in r.warnings]
    lines += ["", "LIMITATIONS"] + [f"  * {x}" for x in r.limitations]
    return "\n".join(lines) + "\n"