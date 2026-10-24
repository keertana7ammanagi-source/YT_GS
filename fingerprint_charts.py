"""Matplotlib drawing for FingerprintResult, shared by the dashboard tab and the PDF report.

Uses only matplotlib's Figure object (no pyplot, no GUI backend), so it is safe to call from a worker thread
for the PDF and from the Tk thread for the dashboard.  Light theme to match the graph window and the report.
"""
from __future__ import annotations

import math

from environmental_fingerprinting import ALTITUDE_FIELD, FEATURE_LABELS, FEATURE_NAMES, FEATURE_UNITS, FingerprintResult

CLUSTER_COLORS = ("#1f6fd1", "#f08a00", "#2e9e44", "#d62728", "#7e3fbf")
UNASSIGNED = "#9aa3b0"
FEATURE_COLORS = {"TEMP": "#d62728", "PRESSURE": "#1f6fd1", "HUMIDITY": "#0e9aa7", "LIGHT_LUX": "#f08a00", "UV_INDEX": "#7e3fbf"}
TEXT, GRID, PANEL = "#1f2933", "#d5dae1", "#ffffff"


def _style(ax) -> None:
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=TEXT, labelsize=7)
    ax.grid(True, color=GRID, linewidth=0.5)
    for spine in ax.spines.values():
        spine.set_color(GRID)


def _label_color(label: int) -> str:
    return CLUSTER_COLORS[label % len(CLUSTER_COLORS)] if label >= 0 else UNASSIGNED


def _message(ax, text: str) -> None:
    ax.set_axis_off()
    ax.text(0.5, 0.5, text, ha="center", va="center", wrap=True, color=TEXT, fontsize=8, transform=ax.transAxes)


def _legend_handles(result: FingerprintResult):
    from matplotlib.lines import Line2D
    handles = [Line2D([0], [0], marker="o", linestyle="", color=_label_color(i), markersize=5, label=c.name)
               for i, c in enumerate(result.clusters)]
    if result.clusters and any(v < 0 for v in result.series.labels):
        handles.append(Line2D([0], [0], marker="o", linestyle="", color=UNASSIGNED, markersize=5, label="unassigned"))
    return handles


def draw_feature_timelines(fig, result: FingerprintResult) -> None:
    """One panel per measured feature (raw units) plus altitude context, points coloured by pattern."""
    s = result.series
    panels = [(n, f"{FEATURE_LABELS[n]}\n({FEATURE_UNITS[n]})", s.values.get(n, [])) for n in FEATURE_NAMES]
    panels = [p for p in panels if any(v is not None for v in p[2])]
    if any(v is not None for v in s.altitude):
        panels.append((ALTITUDE_FIELD, "Altitude\n(m, context)", s.altitude))
    fig.set_facecolor("white")
    if not panels or not s.t:
        _message(fig.add_subplot(1, 1, 1), "No plottable data")
        return
    axes = fig.subplots(len(panels), 1, sharex=True)
    axes = [axes] if len(panels) == 1 else list(axes)
    for ax, (name, label, values) in zip(axes, panels):
        _style(ax)
        pts = [(t, v, lab) for t, v, lab in zip(s.t, values, s.labels) if v is not None]
        if result.clusters:
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], c=[_label_color(p[2]) for p in pts], s=5, linewidths=0)
        else:
            color = FEATURE_COLORS.get(name, "#4a5568")
            ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, linewidth=1.2)
        ax.set_ylabel(label, color=TEXT, fontsize=7)
    axes[-1].set_xlabel("Mission TIMESTAMP (s)", color=TEXT, fontsize=8)
    handles = _legend_handles(result)
    if handles:
        axes[0].legend(handles=handles, loc="upper right", fontsize=7, ncol=len(handles), frameon=False)
    fig.align_ylabels(axes)


def draw_cluster_bars(ax, result: FingerprintResult) -> None:
    """Grouped bars of each pattern's centroid in standardised units (0 = dataset average)."""
    _style(ax)
    feats = result.clustering.features_used
    if not result.clusters or not feats:
        _message(ax, "Cluster comparison unavailable:\n" + (result.clustering.reason or "no patterns reported"))
        return
    width = 0.8 / len(result.clusters)
    for i, c in enumerate(result.clusters):
        xs = [j + (i - (len(result.clusters) - 1) / 2) * width for j in range(len(feats))]
        ax.bar(xs, [c.centroid_z.get(f, 0.0) for f in feats], width=width, color=_label_color(i), label=c.name)
    ax.axhline(0, color=TEXT, linewidth=0.6)
    ax.set_xticks(range(len(feats)))
    ax.set_xticklabels([FEATURE_LABELS[f].replace("Relative ", "").replace(" intensity", "") for f in feats], fontsize=7,
                       rotation=25, ha="right")
    ax.set_ylabel("Centroid (standardised units)", color=TEXT, fontsize=7)
    ax.set_title("Pattern comparison", color=TEXT, fontsize=8, loc="left")
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + 0.35 * (hi - lo))              # headroom so the legend never covers bars
    ax.legend(fontsize=7, frameon=False, ncol=len(result.clusters), loc="upper center")


def draw_ui_overview(fig, result: FingerprintResult) -> None:
    """Compact dashboard figure: standardised timelines, pattern membership over time, pattern comparison."""
    fig.clear()
    fig.set_facecolor("white")
    s = result.series
    gs = fig.add_gridspec(2, 2, height_ratios=[1.35, 1])
    top = fig.add_subplot(gs[0, :])
    _style(top)
    plotted = False
    for name in FEATURE_NAMES:
        pts = [(t, v) for t, v in zip(s.t, s.values.get(name, [])) if v is not None]
        if len(pts) < 2:
            continue
        vs = [p[1] for p in pts]
        mean = sum(vs) / len(vs)
        std = math.sqrt(sum((v - mean) ** 2 for v in vs) / len(vs))
        if std == 0:
            continue
        top.plot([p[0] for p in pts], [(p[1] - mean) / std for p in pts], color=FEATURE_COLORS[name], linewidth=1.1,
                 label=f"{FEATURE_LABELS[name]} ({FEATURE_UNITS[name]})")
        plotted = True
    if plotted:
        top.legend(fontsize=7, ncol=3, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.18))
    else:
        _message(top, "No varying feature to plot")
    top.set_ylabel("Standardised value (z)", color=TEXT, fontsize=7)
    top.set_title("Environmental readings over mission time (each feature standardised)", color=TEXT, fontsize=8, loc="left")
    top.set_xlabel("Mission TIMESTAMP (s)", color=TEXT, fontsize=7)
    if result.clusters:
        mid, bars = fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])
        _style(mid)
        pts = [(t, lab) for t, lab in zip(s.t, s.labels) if lab >= 0]
        mid.scatter([p[0] for p in pts], [p[1] for p in pts], c=[_label_color(p[1]) for p in pts], s=6, linewidths=0)
        mid.set_yticks(range(len(result.clusters)))
        mid.set_yticklabels([c.name.replace("Pattern ", "") for c in result.clusters], fontsize=7)
        mid.set_xlabel("Mission TIMESTAMP (s)", color=TEXT, fontsize=7)
        mid.set_title("Pattern membership over time", color=TEXT, fontsize=8, loc="left")
        draw_cluster_bars(bars, result)
    else:
        _message(fig.add_subplot(gs[1, :]), "No distinct patterns reported.\n" + (result.clustering.reason or ""))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.93, bottom=0.07, hspace=0.75, wspace=0.28)


def clear_with_message(fig, text: str) -> None:
    fig.clear()
    fig.set_facecolor("white")
    _message(fig.add_subplot(1, 1, 1), text)