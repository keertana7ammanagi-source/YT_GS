"""Optional tkintermapview adapter for live position and trajectory display."""
from __future__ import annotations

import tkinter as tk
from typing import Any


class FlightMap:
    def __init__(self, parent: Any, height: int = 360):
        self.widget = None
        self.marker = None
        self.path = None
        self._points: list[tuple[float, float]] = []
        try:
            import tkintermapview
            self.widget = tkintermapview.TkinterMapView(parent, height=height, corner_radius=8)
            self.widget.set_position(20.5937, 78.9629)
            self.widget.set_zoom(4)
            self.widget.pack(fill="both", expand=True)
        except Exception as exc:
            self.widget = tk.Label(parent, text=f"Map unavailable: {exc}", anchor="center")
            self.widget.pack(fill="both", expand=True)

    def update(self, latitude: float, longitude: float, fix: int) -> None:
        if fix <= 0 or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return
        if not hasattr(self.widget, "set_marker"):
            return
        point = (latitude, longitude)
        self._points.append(point)
        self._points = self._points[-5000:]
        self.widget.set_position(*point)
        if self.marker:
            try: self.marker.delete()
            except Exception: pass
        self.marker = self.widget.set_marker(*point, text=f"{latitude:.5f}, {longitude:.5f}")
        if self.path:
            try: self.path.delete()
            except Exception: pass
        if len(self._points) >= 2:
            self.path = self.widget.set_path(self._points)
