"""SYNTHETIC TEST DATA ONLY.  Values are invented for unit tests; they are not real CanSat
measurements and are not engineering limits."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from station_control import TELEMETRY_FIELDS, parse_frame  # noqa: E402

_BASE = {
    "TEAM_ID": "1234", "GNSS_TIME": "12:00:00", "GNSS_SATS": "8", "FLIGHT_SOFTWARE_STATE": "2",
    "GNSS_FIX": "2", "GNSS_LATITUDE": "12.9", "GNSS_LONGITUDE": "77.6", "PRESSURE": "101300",
    "ALTITUDE": "100", "TEMP": "25", "VOLTAGE": "7.4", "ACCEL_Z": "9.8", "MAG_X": "30",
    "MAG_Y": "-10", "MAG_Z": "40", "HUMIDITY": "50", "LIGHT_LUX": "500", "UV_INDEX": "2",
    "GNSS_ALTITUDE": "100", "GNSS_HDOP": "1.2", "CURRENT": "0.2", "POWER": "1.5",
}
_NOISY_SKIP = {"TEAM_ID", "GNSS_TIME", "GNSS_SATS", "FLIGHT_SOFTWARE_STATE", "GNSS_FIX",
               "GNSS_LATITUDE", "GNSS_LONGITUDE", "TIMESTAMP", "PACKET_COUNT"}


def make_frame(i: int, state: int = 2, **overrides) -> str:
    """CSV line with exactly 37 fields; i = sample number (TIMESTAMP and PACKET_COUNT)."""
    cells = []
    for k, name in enumerate(TELEMETRY_FIELDS):
        if name == "TIMESTAMP":
            cells.append(str(i))
        elif name == "PACKET_COUNT":
            cells.append(str(i))
        elif name == "FLIGHT_SOFTWARE_STATE":
            cells.append(str(state))
        else:
            base = float(_BASE.get(name, "0")) if name not in ("TEAM_ID", "GNSS_TIME") else None
            if base is None:
                cells.append(_BASE[name])
            elif name in _NOISY_SKIP:
                cells.append(_BASE[name])
            else:
                cells.append(repr(base + 0.02 * math.sin(i * 1.3 + k)))
    for name, value in overrides.items():
        cells[TELEMETRY_FIELDS.index(name)] = str(value)
    return ",".join(cells)


def make_record(i: int, state: int = 2, **overrides) -> dict:
    """Parsed record via the real parse_frame (so types match the live pipeline)."""
    return parse_frame(make_frame(i, state, **overrides))
