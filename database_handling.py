"""Single-writer SQLite store with asynchronous batching and CSV export."""
from __future__ import annotations

import csv
import queue
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from station_control import TELEMETRY_FIELDS

DB_PATH = Path(__file__).resolve().with_name("telemetry.sqlite3")
_SCHEMA = ", ".join(f'"{field}" TEXT' for field in TELEMETRY_FIELDS)


class TelemetryDatabase:
    def __init__(self, path: str | Path = DB_PATH, batch_size: int = 25, flush_seconds: float = 0.5):
        self.path = Path(path)
        self.batch_size = max(1, batch_size)
        self.flush_seconds = max(0.05, flush_seconds)
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=5000)
        self._ready = threading.Event()
        self._closed = False
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._writer, name="sqlite-writer", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Database writer failed to initialize")
        if self._error:
            raise RuntimeError(f"Database initialization failed: {self._error}")

    def _writer(self) -> None:
        conn = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, received_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), {_SCHEMA})")
            conn.execute("CREATE TABLE IF NOT EXISTS telemetry_calculated (id INTEGER PRIMARY KEY AUTOINCREMENT, received_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), packet_count INTEGER, relative_altitude REAL, vertical_speed REAL, acceleration_magnitude REAL, power REAL, current REAL)")
            conn.commit()
        except Exception as exc:
            self._error = exc
        finally:
            self._ready.set()
        if self._error or conn is None:
            return
        batch: list[dict[str, Any]] = []
        insert = "INSERT INTO telemetry (" + ",".join(f'"{f}"' for f in TELEMETRY_FIELDS) + ") VALUES (" + ",".join("?" for _ in TELEMETRY_FIELDS) + ")"
        calculated_insert = "INSERT INTO telemetry_calculated (packet_count, relative_altitude, vertical_speed, acceleration_magnitude, power, current) VALUES (?, ?, ?, ?, ?, ?)"
        stopping = False
        while not stopping:
            try:
                item = self._queue.get(timeout=self.flush_seconds)
                if item is None:
                    stopping = True
                else:
                    batch.append(item)
            except queue.Empty:
                pass
            if batch and (len(batch) >= self.batch_size or stopping or self._queue.empty()):
                try:
                    conn.executemany(insert, [tuple(str(row.get(f, "")) for f in TELEMETRY_FIELDS) for row in batch])
                    conn.executemany(calculated_insert, [(row.get("PACKET_COUNT"), row.get("REL_ALTITUDE"), row.get("VERTICAL_SPEED"), row.get("ACCEL_MAG"), row.get("POWER"), row.get("CURRENT")) for row in batch])
                    conn.commit()
                except sqlite3.Error as exc:
                    self._error = exc
                batch.clear()
        conn.close()

    def submit(self, record: dict[str, Any], timeout: float = 0.2) -> None:
        if self._closed:
            raise RuntimeError("Database is closed")
        missing = [field for field in TELEMETRY_FIELDS if field not in record]
        if missing:
            raise ValueError("Telemetry record is missing required fields: " + ", ".join(missing))
        if self._error:
            raise RuntimeError(f"Database writer failed: {self._error}")
        self._queue.put(dict(record), timeout=timeout)

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path, timeout=3) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM telemetry ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def last_id(self) -> int:
        """Highest stored row id (0 when empty).  Read-only; used to mark where this session's rows begin."""
        with closing(sqlite3.connect(self.path, timeout=3)) as conn:
            conn.execute("PRAGMA query_only=ON")
            return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM telemetry").fetchone()[0])

    def history(self, since_id: int = 0, columns: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Read-only: rows with id > since_id in insertion order (values are stored as text).

        Unlike recent() this is not capped at 1000 rows.  Only known column names are accepted.
        """
        allowed = {"id", "received_utc", *TELEMETRY_FIELDS}
        chosen = ["id", "received_utc", *TELEMETRY_FIELDS] if columns is None else ["id", *[c for c in columns if c != "id"]]
        unknown = [c for c in chosen if c not in allowed]
        if unknown:
            raise ValueError(f"Unknown telemetry columns: {unknown}")
        sql = "SELECT " + ",".join(f'"{c}"' for c in chosen) + " FROM telemetry WHERE id > ? ORDER BY id"
        with closing(sqlite3.connect(self.path, timeout=10)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, (int(since_id),))]

    def export_csv(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as conn, target.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["received_utc", *TELEMETRY_FIELDS])
            writer.writerows(conn.execute("SELECT received_utc," + ",".join(f'"{f}"' for f in TELEMETRY_FIELDS) + " FROM telemetry ORDER BY id"))
        return target

    def close(self, timeout: float = 3) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            pass
        self._thread.join(timeout)