"""SYNTHETIC environmental test data (three distinct regimes).  Not real flight data."""
from __future__ import annotations

import math

from tests._synthetic import make_record

# phase -> (TEMP, PRESSURE, HUMIDITY, LIGHT_LUX, UV_INDEX, ALTITUDE, firmware state)
PHASES = {0: (30, 100000, 40, 800, 3, 100, 2), 1: (22, 94000, 55, 15000, 7, 450, 3), 2: (26, 98000, 48, 6000, 5, 200, 5)}


def env_values(i: int, phase: int) -> dict:
    j, k = math.sin(i * 1.7), math.cos(i * 2.3)
    t, p, h, lux, uv, alt, _ = PHASES[phase]
    return dict(TEMP=t + 0.2 * j, PRESSURE=p + 30 * k, HUMIDITY=h + 0.5 * j, LIGHT_LUX=lux * (1 + 0.02 * k),
                UV_INDEX=uv + 0.05 * j, ALTITUDE=alt + 2 * j)


def phase_of(i: int) -> int:
    return 0 if i < 40 else 1 if i < 100 else 2


def three_regime_records(n: int = 150) -> list[dict]:
    """Full parsed records (all 37 fields) with environmental values from three synthetic regimes."""
    out = []
    for i in range(1, n + 1):
        phase = phase_of(i)
        rec = make_record(i, state=PHASES[phase][6])
        rec.update(env_values(i, phase))
        out.append(rec)
    return out


def minimal_rows(n: int = 150) -> list[dict]:
    """DB-style rows: text values, only the columns the fingerprint needs."""
    rows = []
    for i in range(1, n + 1):
        phase = phase_of(i)
        row = {"TEAM_ID": "1234", "TIMESTAMP": str(i), "FLIGHT_SOFTWARE_STATE": str(PHASES[phase][6])}
        row.update({k: repr(v) for k, v in env_values(i, phase).items()})
        rows.append(row)
    return rows
