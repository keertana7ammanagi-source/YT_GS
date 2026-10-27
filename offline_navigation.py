"""Offline walking guidance from the operator's current position to a CanSat."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000
COMPASS_NAMES = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")
COMPASS_ABBR = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two latitude/longitude points (degrees)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def compass_direction(bearing: float) -> tuple[str, str]:
    """Nearest of 8 compass points for a bearing in degrees clockwise from north: ("north-east", "NE")."""
    index = int(((float(bearing) % 360) + 22.5) // 45) % 8
    return COMPASS_NAMES[index], COMPASS_ABBR[index]


def format_distance(metres: float) -> str:
    return f"{metres:.1f} m" if metres < 1000 else f"{metres / 1000:.2f} km"


def turn_text(relative: float, distance: float) -> str:
    """Plain-language turn instruction. relative = target bearing - your heading, in (-180, 180]; positive = right."""
    if distance < 1:
        return "You have reached the CanSat"
    side, amount = ("right" if relative > 0 else "left"), round(abs(relative))
    if abs(relative) <= 15:
        return "Walk straight ahead"
    if abs(relative) <= 45:
        return f"Turn slightly {side} ({amount}°), then walk"
    if abs(relative) < 150:
        return f"Turn {side} {amount}°, then walk"
    return f"Turn around (about {amount}° {side}), then walk"


def calculate_navigation_instructions(user_lat, user_lon, user_heading, cansat_lat, cansat_lon):
    """Return distance, target bearing and a relative turn instruction.

    Headings and bearings use degrees clockwise from true north. Invalid coordinates
    raise ``ValueError`` so callers can keep stale/invalid GPS data off the display.
    """
    values = tuple(float(v) for v in (user_lat, user_lon, user_heading, cansat_lat, cansat_lon))
    lat1, lon1, heading, lat2, lon2 = values
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Navigation inputs must be finite numbers")
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        raise ValueError("Latitude or longitude is out of range")
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlat, dlon = lat2r - lat1r, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    distance = 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
    y = math.sin(dlon) * math.cos(lat2r)
    x = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
    relative = (bearing - heading + 180) % 360 - 180
    if distance < 1:
        instruction = "You have reached the CanSat"
    elif abs(relative) <= 15:
        instruction = "Walk Straight"
    elif relative > 0:
        instruction = f"Turn Right by {round(abs(relative))}°"
    else:
        instruction = f"Turn Left by {round(abs(relative))}°"
    compass, compass_abbr = compass_direction(bearing)
    return {
        "distance": round(distance, 1), "bearing": round(bearing, 1), "instruction": instruction,
        # added for easier-to-follow guidance (existing keys above are unchanged)
        "compass": compass, "compass_abbr": compass_abbr, "heading": round(heading % 360, 1),
        "relative_turn": round(relative, 1), "turn_text": turn_text(relative, distance),
        "distance_text": format_distance(distance),
    }


def format_recovery_text(result: dict) -> str:
    """Three short lines for the Recovery tab."""
    if result["distance"] < 1:
        return f"{result['turn_text']}\nDistance: {result['distance_text']}"
    heading_name = compass_direction(result["heading"])[1]
    return (f"{result['turn_text']}\n"
            f"Go {result['compass']} ({result['compass_abbr']})  |  Distance: {result['distance_text']}  |  Bearing: {result['bearing']:.0f}°\n"
            f"You are facing {result['heading']:.0f}° ({heading_name})")


# ---------------------------------------------------------------------------------------------- landing point
@dataclass(frozen=True)
class LandingStatus:
    locked: bool
    coordinates: tuple[float, float] | None
    message: str
    valid_fixes: int
    spread_m: float | None = None
    low_confidence: bool = False


class LandingEstimator:
    """Locks the landing point from several valid GNSS fixes received after IMPACT instead of a single sample.

    A fix is valid when GNSS_FIX > 0, latitude/longitude are finite, in range and not exactly 0,0, and HDOP is
    positive and not above ``max_hdop``.  Once ``required_fixes`` consecutive-window valid fixes are available, their
    mean is locked if every fix lies within ``max_spread_m`` of the mean.  Otherwise the window slides (so an outlier
    or movement right after impact drops out).  After ``give_up_after`` valid fixes without settling, the latest mean
    is locked and flagged low-confidence.  All thresholds are tunable defaults, not validated limits.
    Limitation: plain mean of latitude/longitude - fine over metres, not designed for the 180th meridian.
    """

    def __init__(self, required_fixes: int = 5, max_hdop: float = 5.0, max_spread_m: float = 10.0, give_up_after: int = 30):
        if required_fixes < 2 or give_up_after < required_fixes or max_hdop <= 0 or max_spread_m <= 0:
            raise ValueError("need required_fixes >= 2, give_up_after >= required_fixes, max_hdop > 0, max_spread_m > 0")
        self.required_fixes, self.max_hdop, self.max_spread_m, self.give_up_after = required_fixes, max_hdop, max_spread_m, give_up_after
        self.reset()

    def reset(self) -> None:
        self._window: deque[tuple[float, float]] = deque(maxlen=self.required_fixes)
        self._total_valid = 0
        self._rejected = 0
        self._last_reject = ""
        self._status: LandingStatus | None = None

    @property
    def locked(self) -> bool:
        return self._status is not None

    def _reject_reason(self, fix, lat, lon, hdop) -> str | None:
        try:
            if int(fix) <= 0:
                return "no GNSS fix"
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return "unreadable fix or coordinates"
        if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return "coordinates out of range"
        if lat == 0 and lon == 0:
            return "coordinates are 0,0"
        try:
            hdop = float(hdop)
        except (TypeError, ValueError):
            return "HDOP unavailable"
        if not math.isfinite(hdop) or hdop <= 0:
            return "HDOP unavailable"
        if hdop > self.max_hdop:
            return f"HDOP {hdop:.1f} above {self.max_hdop:g}"
        return None

    def add_fix(self, fix, latitude, longitude, hdop) -> LandingStatus:
        if self._status is not None:
            return self._status                      # already locked: never moves
        reason = self._reject_reason(fix, latitude, longitude, hdop)
        if reason:
            self._rejected += 1
            self._last_reject = reason
            return LandingStatus(False, None, self._progress_message(), len(self._window))
        self._window.append((float(latitude), float(longitude)))
        self._total_valid += 1
        if len(self._window) < self.required_fixes:
            return LandingStatus(False, None, self._progress_message(), len(self._window))
        lat = sum(p[0] for p in self._window) / len(self._window)
        lon = sum(p[1] for p in self._window) / len(self._window)
        spread = max(haversine_m(lat, lon, p[0], p[1]) for p in self._window)
        if spread <= self.max_spread_m:
            self._status = LandingStatus(
                True, (lat, lon), f"IMPACT coordinates locked: {lat:.6f}, {lon:.6f}  (average of {self.required_fixes} valid GNSS fixes, spread {spread:.1f} m)",
                len(self._window), spread)
        elif self._total_valid >= self.give_up_after:
            self._status = LandingStatus(
                True, (lat, lon), f"IMPACT coordinates locked with LOW CONFIDENCE: {lat:.6f}, {lon:.6f}  (fixes were still moving by {spread:.1f} m after {self._total_valid} valid fixes)",
                len(self._window), spread, True)
        else:
            return LandingStatus(False, None, self._progress_message(f"fixes still moving by {spread:.1f} m (limit {self.max_spread_m:g} m)"),
                                 len(self._window), spread)
        return self._status

    def _progress_message(self, note: str = "") -> str:
        text = f"IMPACT received - averaging GNSS fixes for the landing point: {len(self._window)}/{self.required_fixes} valid"
        if note:
            text += f" ({note})"
        elif self._last_reject:
            text += f" (last rejected: {self._last_reject}; {self._rejected} rejected so far)"
        return text