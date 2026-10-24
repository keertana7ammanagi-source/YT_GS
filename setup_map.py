"""Lazy Carto tile map with an offline launch-pad-centered coordinate grid."""
from __future__ import annotations

import math
import threading
import tkinter as tk
from typing import Any
from urllib.request import Request, urlopen

TILE_SERVER = "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"
TILE_PROBE_URL = TILE_SERVER.format(z=5, x=17, y=13)
LAUNCH_PAD = (20.5937, 78.9629)  # configurable reference origin for the offline local grid


class FlightMap:
    def __init__(self, parent: Any, height: int = 360):
        self.parent = parent
        self.height = height
        self.widget = None
        self.fallback = None
        self.marker = None
        self.path = None
        self.target_marker = None
        self.vector = None
        self.origin_marker = None
        self._points: list[tuple[float, float]] = []
        self.current_position: tuple[float, float] | None = None
        self.current_altitude: float | None = None
        self.target_position: tuple[float, float] | None = None
        self.origin: tuple[float, float] | None = None
        self.visible = False
        self._probing = False
        self._probe_generation = 0

    def show(self) -> None:
        """Show the view and load Carto tiles only after the operator opens it."""
        self.visible = True
        if self.widget is not None:
            self.widget.pack(fill="both", expand=True)
            self._refresh_tile_map()
            return
        if self.fallback is None:
            self._create_fallback("Checking Carto tile access…")
        else:
            self.fallback.pack(fill="both", expand=True)
            self._draw_fallback("Offline grid · retrying Carto tiles…")
        if not self._probing:
            self._probe_generation += 1
            generation = self._probe_generation
            self._probing = True
            threading.Thread(target=self._probe_tile_server, args=(generation,), name="map-tile-check", daemon=True).start()

    def hide(self) -> None:
        """Hide widgets without losing position history or initializing resources."""
        self.visible = False
        for widget in (self.widget, self.fallback):
            if widget is not None:
                try: widget.pack_forget()
                except Exception: pass

    def update(self, latitude: float, longitude: float, fix: int, altitude: float | None = None) -> None:
        """Buffer valid GNSS fixes and redraw only while the map is visible."""
        if fix <= 0 or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return
        self.current_position = (float(latitude), float(longitude))
        self.current_altitude = float(altitude) if altitude is not None else None
        if not self._points or self._points[-1] != self.current_position:
            self._points.append(self.current_position)
            self._points = self._points[-5000:]
        if self.visible:
            if self.widget is not None:
                self._refresh_tile_map()
            elif self.fallback is not None:
                self._draw_fallback()

    def set_navigation_origin(self, latitude: float | None, longitude: float | None) -> None:
        self.origin = self._valid_point(latitude, longitude)
        self._refresh_visible_map()

    def set_target_position(self, latitude: float | None, longitude: float | None, redraw: bool = True) -> None:
        self.target_position = self._valid_point(latitude, longitude)
        if redraw:
            self._refresh_visible_map()

    @staticmethod
    def _valid_point(latitude, longitude):
        if latitude is None or longitude is None:
            return None
        latitude, longitude = float(latitude), float(longitude)
        if not (math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        return latitude, longitude

    def _refresh_visible_map(self) -> None:
        if not self.visible:
            return
        if self.widget is not None:
            self._refresh_tile_map()
        elif self.fallback is not None:
            self._draw_fallback()

    def _probe_tile_server(self, generation: int) -> None:
        error = None
        try:
            request = Request(TILE_PROBE_URL, headers={"User-Agent": "AstrasenseNexusGroundStation/1.0"})
            with urlopen(request, timeout=4) as response:
                if response.status != 200 or "image" not in response.headers.get("Content-Type", ""):
                    raise OSError(f"Carto tiles returned HTTP {response.status}")
                response.read(32)
        except Exception as exc:
            error = exc
        try:
            self.parent.after(0, self._finish_tile_probe, generation, error)
        except Exception:
            self._probing = False

    def _finish_tile_probe(self, generation: int, error: Exception | None) -> None:
        self._probing = False
        if generation != self._probe_generation or not self.visible:
            return
        if error is not None:
            self._draw_fallback(f"Offline coordinate grid · Carto tiles unavailable: {error}")
            return
        try:
            import tkintermapview
            if self.fallback is not None:
                self.fallback.pack_forget()
            self.widget = tkintermapview.TkinterMapView(self.parent, height=self.height, corner_radius=8)
            self.widget.set_tile_server(TILE_SERVER, max_zoom=20)
            self.widget.set_position(*(self.current_position or LAUNCH_PAD))
            self.widget.set_zoom(16 if self.current_position else 13)
            self.widget.pack(fill="both", expand=True)
            self._refresh_tile_map()
        except Exception as exc:
            self._create_fallback(f"Offline coordinate grid · map initialization failed: {exc}")
            self._draw_fallback()

    def _create_fallback(self, message: str) -> None:
        if self.widget is not None:
            try: self.widget.pack_forget()
            except Exception: pass
        if self.fallback is None:
            self.fallback = tk.Canvas(self.parent, height=self.height, background="#101820", highlightthickness=0)
        if self.visible:
            self.fallback.pack(fill="both", expand=True)
        self._draw_fallback(message)

    def _refresh_tile_map(self) -> None:
        if self.widget is None or not self.visible:
            return
        try:
            center = self.current_position or LAUNCH_PAD
            self.widget.set_position(*center)
            if self.marker:
                try: self.marker.delete()
                except Exception: pass
            if self.current_position:
                label = f"CanSat {center[0]:.6f}, {center[1]:.6f}"
                if self.current_altitude is not None:
                    label += f" · {self.current_altitude:.1f} m"
                self.marker = self.widget.set_marker(*center, text=label)
            if self.path:
                try: self.path.delete()
                except Exception: pass
            if len(self._points) >= 2:
                self.path = self.widget.set_path(self._points)
            if self.origin_marker:
                try: self.origin_marker.delete()
                except Exception: pass
                self.origin_marker = None
            if self.target_marker:
                try: self.target_marker.delete()
                except Exception: pass
                self.target_marker = None
            if self.vector:
                try: self.vector.delete()
                except Exception: pass
                self.vector = None
            if self.origin:
                self.origin_marker = self.widget.set_marker(*self.origin, text="Recovery team")
                target = self.target_position or self.current_position
                if target:
                    self.vector = self.widget.set_path([self.origin, target])
            if self.target_position:
                self.target_marker = self.widget.set_marker(*self.target_position, text="Locked landing point")
        except Exception as exc:
            self.widget.pack_forget()
            self.widget = None
            self._create_fallback(f"Offline coordinate grid · tile map error: {exc}")

    def _draw_fallback(self, message: str = "Offline coordinate grid · centered on launch pad") -> None:
        canvas = self.fallback
        if canvas is None:
            return
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 320), max(canvas.winfo_height(), self.height)
        cx, cy = width / 2, height / 2 + 12
        canvas.create_text(12, 10, anchor="nw", fill="#d6e2ea", text=message, tags="static")
        canvas.create_text(width - 12, 10, anchor="ne", fill="#91a4b2", text="© OpenStreetMap · © CARTO", tags="static")
        for radius in (40, 80, 120, 160):
            canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, outline="#263746", tags="grid")
            canvas.create_text(cx + 4, cy-radius+2, anchor="nw", fill="#718493", text=f"{radius} m", tags="grid")
        canvas.create_line(cx, 52, cx, height-24, fill="#526777", tags="grid")
        canvas.create_line(24, cy, width-24, cy, fill="#526777", tags="grid")
        canvas.create_text(cx, 55, text="N", fill="#d6e2ea", tags="grid")
        canvas.create_text(width-30, cy-12, text="E", fill="#d6e2ea", tags="grid")
        launch_x, launch_y = self._project(LAUNCH_PAD, cx, cy, 0.5)
        canvas.create_rectangle(launch_x-5, launch_y-5, launch_x+5, launch_y+5, fill="#ffb454", outline="", tags="launch")
        canvas.create_text(launch_x+8, launch_y, anchor="w", fill="#ffb454", text="Launch pad", tags="launch")
        scale = self._grid_scale(cx, cy)
        for index, point in enumerate(self._points):
            x, y = self._project(point, cx, cy, scale)
            if index:
                px, py = self._project(self._points[index-1], cx, cy, scale)
                canvas.create_line(px, py, x, y, fill="#55c2ff", width=2, tags="track")
        if self.current_position:
            x, y = self._project(self.current_position, cx, cy, scale)
            canvas.create_oval(x-6, y-6, x+6, y+6, fill="#55c2ff", outline="", tags="track")
            canvas.create_text(x+8, y-8, anchor="w", fill="#d6e2ea", text="CanSat", tags="track")
        target = self.target_position or self.current_position
        if self.origin:
            ox, oy = self._project(self.origin, cx, cy, scale)
            canvas.create_oval(ox-5, oy-5, ox+5, oy+5, fill="#74d99f", outline="", tags="vector")
            if target:
                tx, ty = self._project(target, cx, cy, scale)
                canvas.create_line(ox, oy, tx, ty, fill="#ffb454", width=2, dash=(5, 3), tags="vector")

    @staticmethod
    def _project(point: tuple[float, float], cx: float, cy: float, scale: float) -> tuple[float, float]:
        lat, lon = point
        lat0, lon0 = LAUNCH_PAD
        east = math.radians(lon - lon0) * 6_371_000 * math.cos(math.radians(lat0))
        north = math.radians(lat - lat0) * 6_371_000
        return cx + east * scale, cy - north * scale

    def _grid_scale(self, cx: float, cy: float) -> float:
        extent = 180.0
        for point in (*self._points, *((self.current_position,) if self.current_position else ()), *((self.origin,) if self.origin else ()), *((self.target_position,) if self.target_position else ())):
            x, y = self._project(point, 0, 0, 1)
            extent = max(extent, abs(x), abs(y))
        return min((cx - 34) / extent, (cy - 52) / extent)
