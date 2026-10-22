"""Standalone telemetry charts. Connects to the primary station's JSON TCP stream."""
from __future__ import annotations

import json
import socket
import threading
import time
import tkinter as tk
import queue
from collections import deque

HOST, PORT = "127.0.0.1", 5555
CHARTS = [
    ("Altitude (m)", ("REL_ALTITUDE", "ALTITUDE")),
    ("Pressure (Pa) / Vertical speed (m/s)", ("PRESSURE", "VERTICAL_SPEED")),
    ("Temperature (°C) / Humidity (%RH)", ("TEMP", "HUMIDITY")),
    ("Voltage (V) / Current (A) / Power (W)", ("VOLTAGE", "CURRENT", "POWER")),
    ("Orientation (°)", ("ROLL", "PITCH", "YAW")),
]
COLORS = ("#55c2ff", "#ffb454", "#74d99f")


class GraphClient:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AstrasenseNexus | CanSat Telemetry Graphs")
        self.root.geometry("1050x760")
        self.data = {name: {field: deque(maxlen=300) for field in fields} for name, fields in CHARTS}
        self.incoming: queue.Queue[dict] = queue.Queue(maxsize=2000)
        self.status = tk.StringVar(value=f"Waiting for station at {HOST}:{PORT}…")
        tk.Label(root, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=4)
        self.canvas = tk.Canvas(root, bg="#171a21", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        threading.Thread(target=self._receive, daemon=True).start()
        self.root.after(500, self._draw)

    def _receive(self):
        while True:
            try:
                with socket.create_connection((HOST, PORT), timeout=5) as sock:
                    self.incoming.put({"_status": "Connected to ground station"})
                    stream = sock.makefile("r", encoding="utf-8")
                    for line in stream:
                        try:
                            record = json.loads(line)
                            self.incoming.put(record, timeout=0.1)
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
            except OSError as exc:
                try: self.incoming.put_nowait({"_status": f"Reconnecting: {exc}"})
                except queue.Full: pass
                time.sleep(1)

    def _draw(self):
        try:
            while True:
                record = self.incoming.get_nowait()
                if "_status" in record:
                    self.status.set(record["_status"])
                    continue
                timestamp = float(record.get("TIMESTAMP", time.time()))
                for name, fields in CHARTS:
                    for field in fields:
                        if field in record:
                            self.data[name][field].append((timestamp, float(record[field])))
        except queue.Empty:
            pass
        canvas = self.canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 300), max(canvas.winfo_height(), 200)
        cell_w, cell_h = width / 2, height / 3
        for index, (title, fields) in enumerate(CHARTS):
            x0, y0 = (index % 2) * cell_w, (index // 2) * cell_h
            x1, y1 = x0 + cell_w, y0 + cell_h
            canvas.create_rectangle(x0 + 5, y0 + 5, x1 - 5, y1 - 5, outline="#46505e")
            canvas.create_text(x0 + 14, y0 + 15, anchor="w", text=title, fill="white", font=("Segoe UI", 10, "bold"))
            left, top, right, bottom = x0 + 42, y0 + 38, x1 - 12, y1 - 30
            canvas.create_line(left, bottom, right, bottom, fill="#738091")
            canvas.create_line(left, top, left, bottom, fill="#738091")
            for si, field in enumerate(fields):
                values = list(self.data[title][field])
                color = COLORS[si % len(COLORS)]
                canvas.create_text(left + si * 115, y1 - 14, anchor="w", text=field, fill=color, font=("Segoe UI", 8))
                if len(values) < 2:
                    continue
                ys = [point[1] for point in values]
                low, high = min(ys), max(ys)
                span = high - low or 1.0
                coords = []
                for j, (_, value) in enumerate(values):
                    px = left + j * (right - left) / (len(values) - 1)
                    py = bottom - (value - low) * (bottom - top) / span
                    coords.extend((px, py))
                canvas.create_line(*coords, fill=color, width=2)
        self.root.after(500, self._draw)


def main():
    root = tk.Tk()
    GraphClient(root)
    root.mainloop()


if __name__ == "__main__":
    main()
