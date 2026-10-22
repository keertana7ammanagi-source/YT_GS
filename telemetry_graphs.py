"""Live graphs of six telemetry fields, shown in a second window that opens with the station.

Graphs (only these): PRESSURE, HUMIDITY, TEMP, POWER, ALTITUDE, VOLTAGE, each plotted against TIMESTAMP.
Read-only: the window is fed the records the station has ALREADY parsed and validated; it reads no serial
port, parses no packets, writes nothing and sends nothing.  Values shown are the station's final values
(so POWER is the ground-computed VOLTAGE x CURRENT, exactly as on the dashboard).

GraphData holds no GUI code so it can be tested headless; tkinter/matplotlib are imported lazily by the window.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Mapping

GRAPHS = (
    ("PRESSURE", "Pressure (Pa)"),
    ("HUMIDITY", "Humidity (%RH)"),
    ("TEMP", "Temperature (\u00b0C)"),
    ("POWER", "Power (W)"),
    ("ALTITUDE", "Altitude (m)"),
    ("VOLTAGE", "Voltage (V)"),
)
HISTORY = 600          # points kept per graph (bounded, like the rest of the application)
REFRESH_MS = 1000      # redraw at most once per second, and only when new data arrived
# Light theme.  One distinct, saturated colour per graph (readable on white).
BACKGROUND, PANEL, TEXT, GRID = "#f4f6f9", "#ffffff", "#1f2933", "#d5dae1"
FIELD_COLORS = {
    "PRESSURE": "#1f6fd1",   # blue
    "HUMIDITY": "#0e9aa7",   # teal
    "TEMP": "#d62728",       # red
    "POWER": "#f08a00",      # orange
    "ALTITUDE": "#7e3fbf",   # purple
    "VOLTAGE": "#2e9e44",    # green
}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class GraphData:
    """Bounded (timestamp, value) history for each plotted field.  Invalid values are skipped, never plotted."""

    def __init__(self, history: int = HISTORY):
        self.series: dict[str, deque[tuple[float, float]]] = {field: deque(maxlen=history) for field, _ in GRAPHS}
        self.revision = 0

    def add_record(self, record: Mapping[str, Any]) -> bool:
        stamp = _finite(record.get("TIMESTAMP"))
        if stamp is None:
            return False
        added = False
        for field, _ in GRAPHS:
            value = _finite(record.get(field))
            if value is not None:
                self.series[field].append((stamp, value))
                added = True
        if added:
            self.revision += 1
        return added


class TelemetryGraphWindow:
    """Second window with a 3x2 grid: one graph per field in GRAPHS."""

    def __init__(self, master, data: GraphData):
        import tkinter as tk
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        self.data = data
        self._closed = False
        self._drawn_revision = -1
        self.top = tk.Toplevel(master)
        self.top.title("CanSat Telemetry Graphs")
        self.top.geometry("1100x720")
        self.top.configure(bg=BACKGROUND)
        figure = Figure(figsize=(11, 7), dpi=100, facecolor=BACKGROUND, layout="constrained")
        self.lines = {}
        for index, (field, title) in enumerate(GRAPHS):
            axes = figure.add_subplot(3, 2, index + 1)
            axes.set_facecolor(PANEL)
            color = FIELD_COLORS[field]
            axes.set_title(title, color=color, fontsize=10, fontweight="bold", loc="left")
            axes.set_xlabel("TIMESTAMP", color=TEXT, fontsize=8)
            axes.tick_params(colors=TEXT, labelsize=8)
            axes.grid(True, color=GRID, linewidth=0.6)
            for spine in axes.spines.values():
                spine.set_color(GRID)
            (self.lines[field],) = axes.plot([], [], color=color, linewidth=1.8)
        self.figure = figure
        self.canvas = FigureCanvasTkAgg(figure, master=self.top)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.after(REFRESH_MS, self._refresh)

    def _refresh(self):
        if self._closed:
            return
        try:
            if self.data.revision != self._drawn_revision:
                self._drawn_revision = self.data.revision
                for field, _ in GRAPHS:
                    points = list(self.data.series[field])
                    line = self.lines[field]
                    line.set_data([p[0] for p in points], [p[1] for p in points])
                    line.axes.relim()
                    line.axes.autoscale_view()
                self.canvas.draw_idle()
        finally:
            self.top.after(REFRESH_MS, self._refresh)

    def close(self):
        """Closing this window never affects the station; data collection simply stops being drawn."""
        self._closed = True
        try:
            self.top.destroy()
        except Exception:
            pass
