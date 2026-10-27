"""Offline GPS input for the Recovery tab: parse NMEA sentences from a USB/serial GPS receiver.

READ-ONLY: the worker only reads lines from the receiver's serial port; it never writes anything to it, never
touches the LoRa/telemetry link and needs no internet, Wi-Fi or API key.

Supported sentences (any talker prefix: GP, GN, GL, GA, BD ...): GGA (position, fix quality, satellites, HDOP) and
RMC (position, speed over ground, course over ground).  Sentences without a valid checksum are ignored.

Heading note: a GPS *course* is the direction of travel over the ground.  It is only meaningful while the receiver is
actually moving, so it is reported only when speed >= MIN_SPEED_KNOTS_FOR_COURSE (a tunable default, about 0.5 m/s).
When standing still the operator's manually entered heading stays in use.
"""
from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass

MIN_SPEED_KNOTS_FOR_COURSE = 1.0


@dataclass(frozen=True)
class GpsFix:
    latitude: float
    longitude: float
    hdop: float | None = None
    satellites: int | None = None
    speed_knots: float | None = None
    course_deg: float | None = None          # only set while moving (see module note)


def nmea_checksum_ok(sentence: str) -> bool:
    """True if '$...*HH' has a correct XOR checksum.  Sentences without a checksum are rejected."""
    sentence = sentence.strip()
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, given = sentence[1:].partition("*")
    if len(given) < 2:
        return False
    try:
        expected = int(given[:2], 16)
    except ValueError:
        return False
    actual = 0
    for char in body:
        actual ^= ord(char)
    return actual == expected


def _coordinate(value: str, hemisphere: str, degree_digits: int) -> float | None:
    """ddmm.mmmm / dddmm.mmmm + N/S/E/W -> signed decimal degrees, or None if unusable."""
    if not value or hemisphere not in ("N", "S", "E", "W") or len(value) <= degree_digits + 1:
        return None
    try:
        degrees = int(value[:degree_digits])
        minutes = float(value[degree_digits:])
    except ValueError:
        return None
    if not (0 <= minutes < 60) or not math.isfinite(minutes):
        return None
    result = degrees + minutes / 60
    if hemisphere in ("S", "W"):
        result = -result
    limit = 90 if degree_digits == 2 else 180
    return result if abs(result) <= limit else None


def _float(text: str) -> float | None:
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_nmea(line: str) -> dict | None:
    """Parse one NMEA line.  Returns a dict for GGA/RMC sentences (with ``valid`` telling if it holds a usable
    position), or None for anything else (garbage, bad checksum, other sentence types)."""
    line = line.strip()
    if not nmea_checksum_ok(line):
        return None
    fields = line[1:line.index("*")].split(",")
    kind = fields[0][2:]
    if kind == "GGA" and len(fields) >= 9:
        lat, lon = _coordinate(fields[2], fields[3], 2), _coordinate(fields[4], fields[5], 3)
        try:
            quality = int(fields[6])
        except ValueError:
            quality = 0
        sats = int(fields[7]) if fields[7].isdigit() else None
        return {"type": "GGA", "valid": quality >= 1 and lat is not None and lon is not None, "lat": lat, "lon": lon,
                "sats": sats, "hdop": _float(fields[8])}
    if kind == "RMC" and len(fields) >= 9:
        lat, lon = _coordinate(fields[3], fields[4], 2), _coordinate(fields[5], fields[6], 3)
        return {"type": "RMC", "valid": fields[2] == "A" and lat is not None and lon is not None, "lat": lat, "lon": lon,
                "speed_knots": _float(fields[7]), "course": _float(fields[8])}
    return None


class NmeaFixTracker:
    """Combines GGA and RMC sentences into GpsFix objects (position from whichever arrives, extras from the other)."""

    def __init__(self, min_speed_knots: float = MIN_SPEED_KNOTS_FOR_COURSE):
        self.min_speed_knots = min_speed_knots
        self._hdop: float | None = None
        self._sats: int | None = None
        self._speed: float | None = None
        self._course: float | None = None

    def update(self, parsed: dict | None) -> GpsFix | None:
        if not parsed or not parsed["valid"]:
            return None
        if parsed["type"] == "GGA":
            self._hdop, self._sats = parsed["hdop"], parsed["sats"]
        else:
            self._speed, self._course = parsed["speed_knots"], parsed["course"]
        moving = self._speed is not None and self._speed >= self.min_speed_knots and self._course is not None
        return GpsFix(parsed["lat"], parsed["lon"], self._hdop, self._sats, self._speed,
                      (self._course % 360) if moving else None)


class GpsReceiverWorker:
    """Reads NMEA lines from a serial GPS receiver and posts ("gps_fix", GpsFix) / ("gps_status"|"gps_error", text)."""

    def __init__(self, port: str, baudrate: int, output: queue.Queue):
        self.port, self.baudrate, self.output = port, baudrate, output
        self.stop_event = threading.Event()
        self.serial = None
        self.thread = threading.Thread(target=self._run, name="gps-receiver", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        tracker = NmeaFixTracker()
        try:
            import serial
            self.serial = serial.Serial(port=self.port, baudrate=self.baudrate, timeout=0.5)
            self.output.put(("gps_status", f"GPS receiver connected on {self.port} at {self.baudrate} bps"))
            while not self.stop_event.is_set():
                raw = self.serial.readline()                     # read-only: nothing is ever written
                if not raw:
                    continue
                fix = tracker.update(parse_nmea(raw.decode("ascii", errors="replace")))
                if fix is not None:
                    self.output.put(("gps_fix", fix))
        except Exception as exc:                                  # port missing, unplugged, permission ...
            if not self.stop_event.is_set():
                self.output.put(("gps_error", f"GPS receiver stopped: {exc}"))
        finally:
            if self.serial is not None:
                try:
                    self.serial.close()
                except Exception:
                    pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.serial is not None:
            try:
                self.serial.cancel_read()
            except Exception:
                pass
        self.thread.join(2)