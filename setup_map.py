"""Optional tkintermapview adapter for live position and trajectory display."""
from __future__ import annotations

import tkinter as tk
from typing import Any


class FlightMap:
    def __init__(self, parent: Any, height: int = 360):
        self.parent = parent
        self.height = height
        self.widget = None
        self.fallback = None
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
            # Keep position and trajectory useful when map tiles or the optional
            # tkintermapview package are unavailable (including fully offline use).
            self._enable_fallback(exc)

    def _enable_fallback(self, reason: Exception) -> None:
        if self.widget is not None:
            try: self.widget.pack_forget()
            except Exception: pass
        self.fallback = tk.Canvas(self.parent, height=self.height, background="#101820", highlightthickness=0)
        self.fallback.pack(fill="both", expand=True)
        self.fallback.create_text(14, 14, anchor="nw", fill="#d6e2ea", text=f"Offline track view · map tiles unavailable ({reason})")
        self.fallback.create_text(14, 42, anchor="nw", fill="#91a4b2", text="GNSS positions will appear as a local track")

    def update(self, latitude: float, longitude: float, fix: int) -> None:
        if fix <= 0 or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return
        if not hasattr(self.widget, "set_marker"):
            return
        point = (latitude, longitude)
        self._points.append(point)
        self._points = self._points[-5000:]
        if self.fallback is not None:
            self._draw_fallback()
            return
        try:
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
        except Exception as exc:
            self._enable_fallback(exc)
            self._draw_fallback()

    def _draw_fallback(self) -> None:
        canvas = self.fallback
        canvas.delete("track")
        width, height = max(canvas.winfo_width(), 300), max(canvas.winfo_height(), 220)
        left, top, right, bottom = 36, 78, width - 24, height - 28
        for i in range(5):
            x = left + i * (right - left) / 4
            y = top + i * (bottom - top) / 4
            canvas.create_line(x, top, x, bottom, fill="#263746", tags="track")
            canvas.create_line(left, y, right, y, fill="#263746", tags="track")
        if not self._points:
            return
        lats = [p[0] for p in self._points]
        lons = [p[1] for p in self._points]
        lat_span, lon_span = max(max(lats) - min(lats), 1e-5), max(max(lons) - min(lons), 1e-5)
        coords = []
        for lat, lon in self._points:
            coords.extend((left + (lon - min(lons)) / lon_span * (right - left), bottom - (lat - min(lats)) / lat_span * (bottom - top)))
        if len(coords) >= 4:
            canvas.create_line(*coords, fill="#55c2ff", width=3, tags="track")
        x, y = coords[-2:]
        canvas.create_oval(x - 6, y - 6, x + 6, y + 6, fill="#ffb454", outline="", tags="track")
