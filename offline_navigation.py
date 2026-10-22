"""Offline walking guidance from the operator's current position to a CanSat."""
from __future__ import annotations

import math


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
    return {"distance": round(distance, 1), "bearing": round(bearing, 1), "instruction": instruction}
