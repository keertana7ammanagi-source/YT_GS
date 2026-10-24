"""Combined AI analysis PDF report: environmental fingerprinting + the existing anomaly detector's results.

The report is built from the SAME result objects the dashboard shows (a FingerprintResult and a snapshot of the
live AnomalyDetector); it contains no separate hard-coded analysis.  It is read-only: it never touches the
detector's state, the serial port or the database, and never sends anything.

reportlab (PDF) is imported lazily inside generate_report()/build_story(), so the ground station still starts if
reportlab is missing; only the report button then reports an install hint.
"""
from __future__ import annotations

import bisect
import dataclasses
import io
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from environmental_fingerprinting import (
    FEATURE_LABELS, FEATURE_NAMES, FEATURE_UNITS, STATUS_COMPLETE, STATUS_FAILED, STATUS_INSUFFICIENT, FingerprintResult,
)
from station_control import STATE_NAMES

REPORT_TITLE = "CanSat Environmental and Anomaly Analysis Report"
NAVY, ORANGE, LIGHT = "#003366", "#E67E22", "#eef2f7"

DETECTOR_METHOD = [
    "The existing detector is rule-based / statistical. It is not a trained machine-learning model and has not been "
    "evaluated on real flight data.",
    "Data quality: missing, non-numeric, NaN or infinite values; duplicate or backwards TIMESTAMP / PACKET_COUNT; and "
    "frames rejected by the parser raise data-quality alerts. Such values are never added to a baseline.",
    "Engineering limits: optional per-sensor minimum/maximum values. None are configured unless listed below.",
    "Rolling statistical score: each reading is scored against the median and median absolute deviation (MAD) of the "
    "previous valid samples (modified z-score; the current sample is never part of its own baseline). Flagged samples "
    "are quarantined and do not update the baseline.",
    "Persistent shifts: if several consecutive flagged samples agree on a new level, one 'sustained level shift' alert "
    "is raised and that level is then treated as the new normal.",
    "Flight phase: the firmware-reported FLIGHT_SOFTWARE_STATE is used; dynamic sensors are baselined per state and are "
    "not scored for a few samples after a state change. If the state is unavailable the detector says so.",
    "Repeated alerts for the same sensor and kind are suppressed during a cooldown and counted.",
]
DETECTOR_LIMITATIONS = [
    "A sensor that jumps to a new, stable but incorrect value is reported once and is then treated as normal; the "
    "detector does not detect all sensor failures.",
    "Slow drift is absorbed by the rolling baseline and is not flagged.",
    "Strongly accelerating but legitimate changes inside one flight state can still produce alerts.",
    "ROLL, YAW and GNSS_COURSE are wrap-around angles and receive validity/limit checks only.",
    "An alert is a flag for inspection, not a confirmed hardware failure.",
]


# ------------------------------------------------------------------------------------------------ inputs
@dataclass
class AnomalySnapshot:
    """Copy of the live AnomalyDetector's results at one moment (detector state is not modified)."""
    available: bool = True
    error: str | None = None
    status: str = ""
    taken_at: str = ""
    samples_evaluated: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    suppressed: int = 0
    rejected_frames: int = 0
    alerts: list[Any] = field(default_factory=list)
    history_capacity: int | None = None
    history_full: bool = False
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReportInputs:
    fingerprint: FingerprintResult | None
    anomaly: AnomalySnapshot | None
    team_id: str | None = None
    data_source: str = ""
    generated_at: datetime = field(default_factory=datetime.now)
    simulation_in_session: bool = False      # Simulation Mode was switched on at some point since the application started


def snapshot_anomaly_detector(detector: Any) -> AnomalySnapshot:
    """Read-only copy of the detector's alerts, status, counts and configuration."""
    cfg = detector.config
    capacity = getattr(detector.history, "maxlen", None)
    alerts = [dataclasses.replace(a) for a in detector.history]
    counts = detector.counts()
    return AnomalySnapshot(
        available=True, status=detector.status, taken_at=datetime.now().isoformat(timespec="seconds"),
        samples_evaluated=int(detector.samples_evaluated), counts=dict(counts), suppressed=int(detector.suppressed),
        rejected_frames=int(detector.rejected_frames), alerts=alerts, history_capacity=capacity,
        history_full=bool(capacity and len(alerts) >= capacity),
        config={
            "window_size": cfg.window_size, "min_samples": cfg.min_samples, "z_threshold": cfg.z_threshold,
            "z_critical": cfg.z_critical, "min_scale_abs": cfg.min_scale_abs, "min_scale_rel": cfg.min_scale_rel,
            "shift_samples": cfg.shift_samples, "shift_tolerance_z": cfg.shift_tolerance_z,
            "cooldown_samples": cfg.cooldown_samples, "settle_samples": cfg.settle_samples,
            "status_hold_samples": cfg.status_hold_samples, "history_size": cfg.history_size,
            "limits": {k: list(v) for k, v in cfg.limits.items()}, "limits_provisional": cfg.limits_provisional,
            "flatline_fields": list(cfg.flatline_fields),
        },
    )


# ------------------------------------------------------------------------------------------------ helpers
def _x(value: Any) -> str:
    """Escape for reportlab Paragraph markup and keep to the built-in font's character set."""
    return escape(str(value).encode("cp1252", "replace").decode("cp1252"))


def _g(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return str(value)


def _state_name(state: int | None) -> str:
    if state is None:
        return "n/a"
    return STATE_NAMES[state] if 0 <= state < len(STATE_NAMES) else f"STATE {state}"


def _median_dt(times: list[float]) -> float:
    diffs = [b - a for a, b in zip(times, times[1:]) if b - a > 0]
    return statistics.median(diffs) if diffs else 1.0


def _nearest_label(fp: FingerprintResult, t: float) -> tuple[int | None, float | None]:
    """Cluster index (or -1 unassigned) of the observation nearest to mission time t, or None if none is close."""
    times = fp.obs_t
    if not times:
        return None, None
    i = bisect.bisect_left(times, t)
    best = min((j for j in (i - 1, i) if 0 <= j < len(times)), key=lambda j: abs(times[j] - t))
    tol = max(3 * _median_dt(times), 1.0)
    if abs(times[best] - t) > tol:
        return None, abs(times[best] - t)
    return fp.obs_label[best], abs(times[best] - t)


def alert_associations(fp: FingerprintResult | None, alerts: list[Any]) -> dict[str, Any]:
    """Temporal association of retained alerts with fingerprint patterns (association only, never causation)."""
    out: dict[str, Any] = {"timestamped": 0, "untimed": 0, "by_pattern": {}, "unassigned": 0, "outside_data": 0,
                           "near_transition": 0, "transitions": 0}
    timed = [a for a in alerts if getattr(a, "timestamp", None) is not None]
    out["timestamped"], out["untimed"] = len(timed), len(alerts) - len(timed)
    if not timed or fp is None or fp.status != STATUS_COMPLETE or not fp.clusters:
        return out
    times = fp.obs_t
    transitions = [times[i] for i in range(1, len(times))
                   if fp.obs_label[i] >= 0 and fp.obs_label[i - 1] >= 0 and fp.obs_label[i] != fp.obs_label[i - 1]]
    out["transitions"] = len(transitions)
    tol = max(5 * _median_dt(times), 2.0)
    for a in timed:
        label, _ = _nearest_label(fp, float(a.timestamp))
        if label is None:
            out["outside_data"] += 1
        elif label < 0:
            out["unassigned"] += 1
        else:
            name = fp.clusters[label].name
            out["by_pattern"][name] = out["by_pattern"].get(name, 0) + 1
        if transitions and min(abs(float(a.timestamp) - tt) for tt in transitions) <= tol:
            out["near_transition"] += 1
    return out


def build_combined_interpretation(inputs: ReportInputs) -> list[tuple[str, list[str]]]:
    """Section 4 text, computed from the two result objects.  Associations are not causes; no score or verdict."""
    fp, an = inputs.fingerprint, inputs.anomaly
    sections: list[tuple[str, list[str]]] = []

    obs: list[str] = []
    if fp is None or fp.status == STATUS_FAILED:
        obs.append("Environmental fingerprinting did not produce a result" + (f" ({fp.message})." if fp else "."))
    elif fp.status == STATUS_INSUFFICIENT:
        obs.append("Environmental fingerprinting had insufficient valid data: " + fp.message)
    else:
        obs.append(f"Fingerprint {fp.fingerprint_id} covers {fp.n_observations} valid observations "
                   f"(mission time {_g(fp.t_min)}-{_g(fp.t_max)} s).")
        if fp.clusters:
            obs.append(f"{len(fp.clusters)} environmental patterns were reported: "
                       + "; ".join(f"{c.name} ({c.description})" for c in fp.clusters) + ".")
        else:
            obs.append("No distinct environmental patterns were reported: " + (fp.clustering.reason or "see statistics."))
    sections.append(("Observed environmental patterns", obs))

    alerts_txt: list[str] = []
    if an is None or not an.available:
        alerts_txt.append("Anomaly detector results were not available" + (f" ({an.error})." if an and an.error else "."))
    else:
        n = len(an.alerts)
        alerts_txt.append(f"The detector's in-memory history holds {n} alert(s) at report time (status: {an.status or 'n/a'}); "
                          f"{an.suppressed} repeat alert(s) were suppressed and {an.rejected_frames} frame(s) were rejected "
                          "by the parser since the application started.")
        if n:
            sensors = sorted({a.sensor for a in an.alerts})
            alerts_txt.append("Sensors/streams with alerts: " + ", ".join(sensors) + ".")
        else:
            alerts_txt.append("No alerts are held in detector memory, which is not evidence that no anomalies occurred "
                              "(alerts before the application started are not stored).")
    sections.append(("Anomaly alerts retained by the detector", alerts_txt))

    assoc_txt: list[str] = []
    info = alert_associations(fp, an.alerts if an and an.available else [])
    if info["timestamped"] and fp is not None and fp.status == STATUS_COMPLETE and fp.clusters:
        parts = [f"{count} in {name}" for name, count in info["by_pattern"].items()]
        assoc_txt.append(f"Of {info['timestamped']} timestamped alert(s), the nearest fingerprint observation was assigned to: "
                         + (", ".join(parts) if parts else "no pattern") +
                         (f"; {info['unassigned']} fell on observations not assigned to a pattern" if info["unassigned"] else "") +
                         (f"; {info['outside_data']} had no fingerprint observation close in time" if info["outside_data"] else "") + ".")
        if info["transitions"]:
            assoc_txt.append(f"{info['near_transition']} of these alert(s) occurred close in time to one of "
                             f"{info['transitions']} transition(s) between patterns.")
    elif info["timestamped"]:
        assoc_txt.append(f"{info['timestamped']} timestamped alert(s) exist, but no fingerprint patterns are available to "
                         "compare them with.")
    else:
        assoc_txt.append("No timestamped alerts are available, so no temporal comparison with the fingerprint was made.")
    if an and an.available and an.alerts:
        by_state: dict[str, int] = {}
        for a in an.alerts:
            by_state[_state_name(getattr(a, "state", None))] = by_state.get(_state_name(getattr(a, "state", None)), 0) + 1
        assoc_txt.append("Alerts by firmware-reported flight state: " + ", ".join(f"{k} {v}" for k, v in sorted(by_state.items())) + ".")
        env_hits = sorted({a.sensor for a in an.alerts if a.sensor in FEATURE_NAMES})
        if env_hits:
            assoc_txt.append("Alerts were raised on environmental sensors used in the fingerprint ("
                             + ", ".join(env_hits) + "); the detector scores those readings against a rolling baseline.")
    assoc_txt.append("These are temporal associations only. An environmental change occurring near an alert does not show "
                     "that it caused the alert (or the reverse); both can follow the same flight event.")
    sections.append(("Temporal associations (not causation)", assoc_txt))

    dq: list[str] = []
    if fp is not None:
        dq += list(fp.warnings)
    if inputs.simulation_in_session:
        dq.append("Simulation Mode was used during this application session. The anomaly detector evaluates every record "
                  "received since start-up, so its alerts may have been raised on simulated data.")
    if an and an.available:
        if an.counts.get("data_quality", 0) or an.rejected_frames:
            dq.append(f"The detector reported {an.counts.get('data_quality', 0)} data-quality alert(s) and "
                      f"{an.rejected_frames} rejected frame(s); gaps or invalid values reduce what either analysis can show.")
        if an.history_full:
            dq.append(f"Detector memory holds at most {an.history_capacity} alerts and is full: older alerts were discarded "
                      "and are not in this report.")
    sections.append(("Data-quality considerations", dq or ["No data-quality problems were recorded by either analysis."]))

    rec = ["Compare the reported patterns and alerts with the raw telemetry, sensor datasheets and post-flight reference data "
           "before drawing conclusions."]
    if an and an.available:
        if not an.config.get("limits"):
            rec.append("No engineering limits are configured in the anomaly detector; add verified hardware limits to enable "
                       "limit checks (see ANOMALY_DETECTION.md).")
        if an.counts.get("data_quality", 0) or an.rejected_frames:
            rec.append("Inspect the radio link, serial framing and sensor wiring around the logged data-quality events.")
        if an.counts.get("statistical", 0):
            rec.append("Review the raw readings around each statistical alert's timestamp to decide whether it reflects a "
                       "sensor problem or a real change.")
    if fp is not None and fp.status == STATUS_COMPLETE and not fp.clusters:
        rec.append("Collect a longer or more varied dataset (for example a complete flight) to look for distinct patterns.")
    if fp is not None and fp.status == STATUS_INSUFFICIENT:
        rec.append("Run the analysis again after more valid telemetry has been received.")
    sections.append(("Suggested further inspection", rec))
    return sections


# ------------------------------------------------------------------------------------------------ reportlab story
def _rl():
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (CondPageBreak, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as exc:
        raise ImportError("reportlab is not installed. Install it with: python -m pip install reportlab") from exc
    return locals()


class _Ctx:
    def __init__(self):
        self.rl = _rl()
        rl = self.rl
        base = rl["getSampleStyleSheet"]()["Normal"]
        PS, colors = rl["ParagraphStyle"], rl["colors"]
        self.width = rl["A4"][0] - 36 * rl["mm"]
        self.s = {
            "title": PS("t", parent=base, fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=colors.HexColor(NAVY), spaceAfter=4),
            "sub": PS("sub", parent=base, fontSize=10, leading=13, textColor=colors.HexColor("#52606d"), spaceAfter=8),
            "h1": PS("h1", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=colors.HexColor(NAVY), spaceBefore=10, spaceAfter=6),
            "h2": PS("h2", parent=base, fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=colors.HexColor(ORANGE), spaceBefore=8, spaceAfter=4),
            "body": PS("body", parent=base, fontSize=9.2, leading=12.5, spaceAfter=4),
            "bullet": PS("bullet", parent=base, fontSize=9.2, leading=12.5, leftIndent=12, bulletIndent=2, spaceAfter=2),
            "note": PS("note", parent=base, fontSize=8.3, leading=11, textColor=colors.HexColor("#52606d"), spaceAfter=4),
            "td": PS("td", parent=base, fontSize=7.8, leading=9.6),
            "th": PS("th", parent=base, fontName="Helvetica-Bold", fontSize=7.8, leading=9.6, textColor=colors.white),
        }

    def p(self, text, style="body"):
        return self.rl["Paragraph"](_x(text), self.s[style])

    def raw(self, markup, style="body"):          # caller guarantees markup is already escaped
        return self.rl["Paragraph"](markup, self.s[style])

    def bullets(self, items):
        return [self.rl["Paragraph"](_x(i), self.s["bullet"], bulletText="-") for i in items]

    def table(self, rows, widths, header=True):
        rl, colors = self.rl, self.rl["colors"]
        data = [[rl["Paragraph"](_x(c), self.s["th" if header and r == 0 else "td"]) for c in row] for r, row in enumerate(rows)]
        tbl = rl["Table"](data, colWidths=[self.width * w / sum(widths) for w in widths], repeatRows=1 if header else 0)
        style = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c5ccd6")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                 ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)))
        for r in range(1 if header else 0, len(rows)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor(LIGHT)))
        tbl.setStyle(rl["TableStyle"](style))
        return tbl

    def figure(self, draw, width_in=7.0, height_in=4.5):
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        fig = Figure(figsize=(width_in, height_in), dpi=150)
        FigureCanvasAgg(fig)
        draw(fig)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, facecolor="white")
        buf.seek(0)
        return self.rl["Image"](buf, width=self.width, height=self.width * height_in / width_in)

    def safe_figure(self, draw, label, width_in=7.0, height_in=4.5):
        try:
            return self.figure(draw, width_in, height_in)
        except Exception as exc:  # a chart problem must not lose the rest of the report
            return self.p(f"[{label} could not be rendered: {type(exc).__name__}: {exc}]", "note")


def _fp_section(c: _Ctx, fp: FingerprintResult | None) -> list:
    rl = c.rl
    out = [c.p("2. Environmental fingerprinting", "h1")]
    if fp is None:
        return out + [c.p("Environmental fingerprinting was not run; no result is available.")]
    if fp.status == STATUS_FAILED:
        return out + [c.p(fp.message), c.p("No fingerprint results are included in this report.", "note")]
    out += [c.p("2.1 Method", "h2"), c.p(fp.method + ". Features are standardised (z-scores; LIGHT_LUX is log-transformed first) "
            "so units and scales do not dominate; K-means (k-means++ starts, deterministic seed "
            f"{fp.clustering.seed}) is attempted only with enough complete observations and varying features, and groups are "
            "only reported as distinct when a silhouette threshold and a group-gap check are both met. ALTITUDE and "
            "FLIGHT_SOFTWARE_STATE are context only and are not clustered. This is not a trained neural network or a "
            "validated predictive model.")]
    scope = [["Item", "Value"],
             ["Fingerprint ID / name", f"{fp.fingerprint_id or 'n/a'} / {fp.name or 'n/a'}"],
             ["Scope", fp.scope_label or "n/a"], ["Data source", fp.data_source or "n/a"],
             ["Analysis computed at", fp.computed_at or "n/a"],
             ["Records in scope / valid observations / complete observations",
              f"{fp.n_records_input} / {fp.n_observations} / {fp.n_complete}"],
             ["Mission time range (TIMESTAMP, s)", f"{_g(fp.t_min)} to {_g(fp.t_max)}"],
             ["Clustered features", ", ".join(fp.clustering.features_used) or "none"],
             ["Requested interval (s)", f"{fp.interval_requested[0] if fp.interval_requested[0] is not None else 'start'} to "
                                       f"{fp.interval_requested[1] if fp.interval_requested[1] is not None else 'end'}"
                                       f"; {fp.n_outside_interval} record(s) outside it"],
             ["TEAM_ID value(s)", ", ".join(fp.team_ids) or "n/a"]]
    out += [c.p("2.2 Data used and coverage", "h2"), c.table(scope, [34, 66])]
    if fp.status == STATUS_INSUFFICIENT:
        out += [c.p(fp.message), c.p("No fingerprint, clusters or trends were produced.", "note")]
        out += c.bullets(fp.warnings)
        return out
    rows = [["Feature", "Unit", "Valid n", "Coverage", "Min", "Median", "Mean", "Std", "Max", "Clustered"]]
    for s in fp.feature_stats:
        rows.append([s.label, s.unit, str(s.n_valid), f"{s.coverage:.0%}", _g(s.minimum), _g(s.median), _g(s.mean), _g(s.std),
                     _g(s.maximum), "yes" if s.used_in_clustering else "no" + (f" ({s.note})" if s.note else "")])
    out += [c.p("2.3 Environmental feature summary (raw units)", "h2"), c.table(rows, [13, 6, 7, 8, 9, 9, 9, 8, 9, 22])]
    out.append(c.p("2.4 Identified patterns", "h2"))
    out.append(c.p("Clustering: " + (fp.clustering.reason or "n/a")))
    if fp.clusters:
        rows = [["Pattern", "n (share)", "Mission time (s)", "Altitude min/med/max (m)", "Flight states (reported)", "Distinguishing characteristics"]]
        for cl in fp.clusters:
            alt = "n/a" if cl.altitude is None else " / ".join(f"{v:.0f}" for v in cl.altitude)
            mix = ", ".join(f"{k} {v}" for k, v in sorted(cl.states.items(), key=lambda kv: -kv[1])) or "n/a"
            rows.append([cl.name, f"{cl.n} ({cl.share:.0%})", f"{cl.t_min:g} - {cl.t_max:g}", alt, mix, cl.description])
        out.append(c.table(rows, [9, 9, 12, 15, 20, 35]))
        feats = fp.clustering.features_used
        rows = [["Pattern (median, raw units)"] + [f"{FEATURE_LABELS[f]} ({FEATURE_UNITS[f]})" for f in feats]]
        for cl in fp.clusters:
            rows.append([cl.name] + [_g(cl.medians.get(f)) for f in feats])
        out.append(rl["KeepTogether"]([c.p("Pattern medians", "note"), c.table(rows, [20] + [80 / max(1, len(feats))] * len(feats))]))
    out.append(c.p("2.5 Trends over mission time", "h2"))
    rows = [["Feature", "Change, first to last quarter", "Linear slope (per s)", "Correlation with ALTITUDE (r)", "n"]]
    for tr in fp.trends:
        rows.append([tr.label, "n/a" if tr.net_change is None else f"{tr.net_change:+.4g} {tr.unit}",
                     "n/a" if tr.slope_per_s is None else f"{tr.slope_per_s:.3g} {tr.unit}/s",
                     "n/a" if tr.altitude_correlation is None else f"{tr.altitude_correlation:.2f}", str(tr.n)])
    out.append(c.table(rows, [20, 26, 22, 24, 8]))
    if fp.phases:
        rows = [["Flight state (firmware)", "n", "Altitude median (m)"] + [f"{FEATURE_LABELS[f]} median" for f in FEATURE_NAMES]]
        for ph in fp.phases:
            rows.append([ph.state_name, str(ph.n), _g(ph.altitude_median)] + [_g(ph.medians.get(f)) for f in FEATURE_NAMES])
        out += [c.p("2.6 Relationship to flight phase (as reported by the firmware)", "h2"), c.table(rows, [16, 6, 11] + [13.4] * 5)]
    out.append(c.p("2.7 Charts", "h2"))
    from fingerprint_charts import draw_cluster_bars, draw_feature_timelines
    out.append(c.safe_figure(lambda fig: draw_feature_timelines(fig, fp), "Feature timeline chart", 7.0, 8.0))
    if fp.clusters:
        out.append(c.safe_figure(lambda fig: draw_cluster_bars(fig.add_subplot(1, 1, 1), fp), "Pattern comparison chart", 7.0, 3.0))
    out.append(c.p("2.8 Interpretation (generated from the computed results)", "h2"))
    out += c.bullets(fp.explanation)
    out.append(c.p("2.9 Warnings and limitations", "h2"))
    out += c.bullets(fp.warnings or ["No data-quality warnings were generated."])
    out += c.bullets(fp.limitations)
    return out


def _anomaly_section(c: _Ctx, an: AnomalySnapshot | None, fp: FingerprintResult | None) -> list:
    out = [c.p("3. Anomaly detection", "h1")]
    if an is None or not an.available:
        return out + [c.p("Anomaly detector results were not available" + (f": {an.error}" if an and an.error else ".")),
                      c.p("No anomaly results are included in this report.", "note")]
    out += [c.p("3.1 Detector method and configuration", "h2")] + c.bullets(DETECTOR_METHOD)
    cfg = an.config
    limits = ", ".join(f"{k}: {v[0]} to {v[1]}" for k, v in cfg.get("limits", {}).items()) or "none configured"
    rows = [["Setting", "Value"],
            ["Rolling window / minimum samples", f"{cfg.get('window_size')} / {cfg.get('min_samples')}"],
            ["Modified z-score thresholds (warning / critical)", f"{cfg.get('z_threshold')} / {cfg.get('z_critical')}"],
            ["Robust-scale floors (absolute / relative)", f"{cfg.get('min_scale_abs')} / {cfg.get('min_scale_rel')}"],
            ["Sustained-shift samples / tolerance (z)", f"{cfg.get('shift_samples')} / {cfg.get('shift_tolerance_z')}"],
            ["Cooldown / settle samples after state change", f"{cfg.get('cooldown_samples')} / {cfg.get('settle_samples')}"],
            ["Engineering limits" + (" (provisional)" if cfg.get("limits_provisional") and cfg.get("limits") else ""), limits],
            ["Flat-line check fields", ", ".join(cfg.get("flatline_fields", [])) or "disabled"],
            ["Alert memory capacity", str(an.history_capacity if an.history_capacity is not None else cfg.get("history_size"))]]
    out.append(c.table(rows, [48, 52]))
    counts = an.counts
    rows = [["Item", "Value"], ["Detector status at snapshot", an.status or "n/a"], ["Snapshot taken at", an.taken_at or "n/a"],
            ["Records evaluated by the detector (since application start)", str(an.samples_evaluated)],
            ["Retained alerts: limit / statistical / data quality",
             f"{counts.get('limit', 0)} / {counts.get('statistical', 0)} / {counts.get('data_quality', 0)}"],
            ["Repeat alerts suppressed (cooldown)", str(an.suppressed)], ["Frames rejected by the parser", str(an.rejected_frames)]]
    out += [c.p("3.2 Status and counts", "h2"), c.table(rows, [48, 52])]
    out.append(c.p("3.3 Which alerts are in this report", "h2"))
    out.append(c.p(f"Alerts are kept only in the running detector's memory (at most {an.history_capacity or 'a fixed number of'} "
                   "alerts; they are not stored in the database and are lost when the application closes). This report "
                   f"contains the {len(an.alerts)} alert(s) retained at the snapshot"
                   + (" - the memory is full, so older alerts were discarded and are NOT included." if an.history_full
                      else ". Alerts raised before the application started are not available.")))
    out.append(c.p("3.4 Alert table", "h2"))
    if an.alerts:
        rows = [["Time (s)", "Severity", "Type", "Sensor", "Value", "Score", "Flight state", "Reason"]]
        for a in an.alerts:
            kind = a.kind + (" (provisional)" if getattr(a, "provisional", False) else "")
            reason = a.reason + (f" [x{a.repeats} in cooldown]" if getattr(a, "repeats", 1) > 1 else "")
            rows.append(["n/a" if a.timestamp is None else f"{a.timestamp:g}", a.severity, kind, a.sensor, _g(a.value, 6), _g(a.score, 3),
                         _state_name(a.state), reason])
        out.append(c.table(rows, [8, 9, 11, 12, 10, 7, 11, 32]))
        timed = [a for a in an.alerts if a.timestamp is not None]
        if timed:
            out.append(c.safe_figure(lambda fig: _draw_alert_timeline(fig, an, fp), "Alert timeline", 7.0, 3.2))
    else:
        out.append(c.p("No alerts are held in detector memory at the time of this report. This does not prove that no anomaly "
                       "occurred, because alerts from before the application started are not stored."))
    out.append(c.p("3.5 Limitations of the existing detector", "h2"))
    out += c.bullets(DETECTOR_LIMITATIONS)
    return out


def _draw_alert_timeline(fig, an: AnomalySnapshot, fp: FingerprintResult | None) -> None:
    from fingerprint_charts import CLUSTER_COLORS, GRID, TEXT, _style
    ax = fig.add_subplot(1, 1, 1)
    _style(ax)
    timed = [a for a in an.alerts if a.timestamp is not None]
    sensors = sorted({a.sensor for a in timed})
    colours = {"limit": "#d62728", "statistical": "#f08a00", "data_quality": "#1f6fd1"}
    if fp is not None and fp.status == STATUS_COMPLETE and fp.clusters:
        for i, cl in enumerate(fp.clusters):
            ax.axvspan(cl.t_min, cl.t_max, color=CLUSTER_COLORS[i % len(CLUSTER_COLORS)], alpha=0.10, linewidth=0)
            ax.text((cl.t_min + cl.t_max) / 2, len(sensors) - 0.45, cl.name, ha="center", fontsize=7, color=TEXT)
    for kind, colour in colours.items():
        pts = [(a.timestamp, sensors.index(a.sensor)) for a in timed if a.kind == kind]
        if pts:
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], color=colour, s=22, label=kind, edgecolors="white", linewidths=0.4)
    ax.set_yticks(range(len(sensors)))
    ax.set_yticklabels(sensors, fontsize=7)
    ax.set_ylim(-0.6, len(sensors) - 0.2)
    ax.set_xlabel("Mission TIMESTAMP (s); shaded = fingerprint pattern time span", color=TEXT, fontsize=8)
    ax.set_title("Retained alerts over mission time", color=TEXT, fontsize=9, loc="left")
    ax.legend(fontsize=7, frameon=False, loc="upper right")


def build_story(inputs: ReportInputs) -> list:
    """Return the reportlab flowables for the whole report (also used by tests to inspect content)."""
    c = _Ctx()
    rl = c.rl
    fp, an = inputs.fingerprint, inputs.anomaly
    team = inputs.team_id or (fp.team_ids[0] if fp and fp.team_ids else None)
    story: list = [c.p(REPORT_TITLE, "title"), c.p("Offline decision-support analysis generated by the CanSat ground station", "sub")]
    story.append(c.p("1. Mission and report overview", "h1"))
    rng = "not available"
    if fp is not None and fp.t_min is not None:
        rng = f"{fp.t_min:g} to {fp.t_max:g} s (mission TIMESTAMP)"
    n_in = "n/a" if fp is None or fp.status == STATUS_FAILED else f"{fp.n_records_input} in scope, {fp.n_observations} valid environmental observations"
    rows = [["Item", "Value"], ["Team identifier", team or "not available"],
            ["Report generated", inputs.generated_at.strftime("%Y-%m-%d %H:%M:%S")],
            ["Mission timestamp range analysed", rng], ["Telemetry records analysed", n_in],
            ["Data source and coverage", inputs.data_source or (fp.data_source if fp else "") or "not available"],
            ["Environmental fingerprint", (fp.name if fp and fp.name else (fp.message if fp else "not run"))],
            ["Anomaly detector", (f"status '{an.status}', {len(an.alerts)} retained alert(s)" if an and an.available else "not available")]]
    story.append(c.table(rows, [30, 70]))
    story.append(c.p("Executive summary", "h2"))
    summary: list[str] = []
    if fp is None or fp.status == STATUS_FAILED:
        summary.append("Environmental fingerprinting failed or was not run, so no environmental result is included.")
    elif fp.status == STATUS_INSUFFICIENT:
        summary.append("Environmental fingerprinting had insufficient valid data (" + fp.message + ").")
    else:
        summary.append(f"Environmental fingerprinting analysed {fp.n_observations} valid observations and reported "
                       + (f"{len(fp.clusters)} distinct environmental pattern(s)." if fp.clusters else
                          "no distinct patterns (the readings are summarised as a single pattern)."))
    if an is None or not an.available:
        summary.append("Anomaly detector results were not available.")
    else:
        summary.append(f"The existing rule-based anomaly detector reported status '{an.status}' with {len(an.alerts)} alert(s) "
                       f"retained in memory ({an.counts.get('limit', 0)} limit, {an.counts.get('statistical', 0)} statistical, "
                       f"{an.counts.get('data_quality', 0)} data-quality).")
    if fp is not None and fp.simulated_team_ids:
        summary.append("NOTE: the analysed telemetry includes SIMULATED rows (Simulation Mode, TEAM_ID "
                       + ", ".join(fp.simulated_team_ids) + "). Results for those rows do not describe a real environment.")
    if inputs.simulation_in_session:
        summary.append("NOTE: Simulation Mode was used in this session, so detector alerts may come from simulated data.")
    summary.append("This report describes observations and algorithm-generated interpretations only. It gives no safety verdict, "
                   "no mission-success estimate and no single overall score.")
    story += c.bullets(summary)
    story.append(c.p("Overall limitations", "h2"))
    story += c.bullets([
        "Both analyses are offline decision-support tools and have not necessarily been validated on real flight data.",
        "Anomaly alerts exist only in the running application's memory; earlier or discarded alerts are not included.",
        "Temporal associations between environmental patterns and alerts are not evidence of cause.",
        "Ground-station results depend on the telemetry actually received; this report does not verify firmware behaviour."])
    story.append(rl["PageBreak"]())
    story += _fp_section(c, fp)
    story.append(rl["PageBreak"]())
    story += _anomaly_section(c, an, fp)
    story.append(rl["CondPageBreak"](60 * rl["mm"]))
    story.append(c.p("4. Combined interpretation", "h1"))
    for heading, paragraphs in build_combined_interpretation(inputs):
        story.append(c.p(heading, "h2"))
        story += c.bullets(paragraphs)
    story.append(rl["CondPageBreak"](60 * rl["mm"]))
    story.append(c.p("5. Methodology and limitations", "h1"))
    story += c.bullets([
        "Environmental fingerprinting: per-feature statistics, trends and an unsupervised K-means grouping of standardised "
        "TEMP, PRESSURE, HUMIDITY, LIGHT_LUX and UV_INDEX; ALTITUDE and the firmware flight state are context only.",
        "Anomaly detection: the existing rule-based/statistical detector described in Section 3.1, unchanged by this report.",
        "Missing and invalid values (missing, non-numeric, NaN, infinite, or outside definitional bounds such as humidity "
        "outside 0-100 %RH) are excluded and counted, never silently replaced.",
        "Scope: the fingerprint covers the records listed in Section 2.2; the detector covers records it evaluated since the "
        "application started. The two scopes may differ.",
        "Observed facts are the telemetry values and counts in the tables; interpretations are text generated by the "
        "algorithms from those numbers and should be checked by a person.",
        "All analysis ran offline on the ground station; nothing was sent to the CanSat or to any network service."])
    return story


def story_text(story: list) -> str:
    """Plain text of a story (paragraphs and table cells) - used by tests to check report content."""
    parts: list[str] = []
    for item in story:
        if hasattr(item, "getPlainText"):
            parts.append(item.getPlainText())
        elif hasattr(item, "_cellvalues"):
            for row in item._cellvalues:
                parts.extend(cell.getPlainText() for cell in row if hasattr(cell, "getPlainText"))
    return "\n".join(parts)


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColorRGB(0.32, 0.38, 0.45)
    canvas.drawString(doc.leftMargin, 10 * 2.8346, f"{REPORT_TITLE}  |  generated {doc.generated_text}")
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 10 * 2.8346, f"Page {doc.page}")
    canvas.restoreState()


def generate_report(path: str | Path, inputs: ReportInputs) -> Path:
    """Write the PDF to `path` (via a temporary file, then an atomic replace).  Raises on any failure."""
    rl = _rl()
    target = Path(path)
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Destination folder does not exist: {target.parent}")
    partial = target.with_name(target.name + ".partial")
    try:
        story = build_story(inputs)
        doc = rl["SimpleDocTemplate"](str(partial), pagesize=rl["A4"], leftMargin=18 * rl["mm"], rightMargin=18 * rl["mm"],
                                      topMargin=16 * rl["mm"], bottomMargin=18 * rl["mm"], title=REPORT_TITLE,
                                      author="CanSat Ground Station")
        doc.generated_text = inputs.generated_at.strftime("%Y-%m-%d %H:%M:%S")
        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        with open(partial, "rb") as fh:
            head = fh.read(5)
            fh.seek(-min(2048, os.path.getsize(partial)), os.SEEK_END)
            tail = fh.read()
        if head != b"%PDF-" or b"%%EOF" not in tail or os.path.getsize(partial) < 1000:
            raise RuntimeError("Generated PDF failed validation (incomplete file)")
        os.replace(partial, target)
    except Exception:
        try:
            if partial.exists():
                partial.unlink()
        except OSError:
            pass
        raise
    return target