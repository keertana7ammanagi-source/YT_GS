"""Serial configuration and robust line-oriented telemetry parsing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math

TELEMETRY_FIELDS = (
    "TEAM_ID", "TIMESTAMP", "PACKET_COUNT", "ALTITUDE", "PRESSURE", "TEMP",
    "VOLTAGE", "GNSS_TIME", "GNSS_LATITUDE", "GNSS_LONGITUDE", "GNSS_ALTITUDE",
    "GNSS_SATS", "ACCEL_X", "ACCEL_Y", "ACCEL_Z", "GYRO_X", "GYRO_Y", "GYRO_Z",
    "MAG_X", "MAG_Y", "MAG_Z", "ROLL", "PITCH", "YAW", "LIGHT_LUX", "UV_INDEX",
    "HALL_RPM", "FLIGHT_SOFTWARE_STATE", "REL_ALTITUDE", "VERTICAL_SPEED",
    "GNSS_GROUND_SPEED", "GNSS_COURSE", "GNSS_HDOP", "GNSS_FIX", "HUMIDITY",
    "CURRENT", "POWER",
)
STATE_NAMES = ("BOOT", "TEST_MODE", "LAUNCH_PAD", "ASCENT", "ROCKET_DEPLOY", "DESCENT", "AEROBREAK_RELEASE", "IMPACT")
STRING_FIELDS = {"TEAM_ID", "GNSS_TIME"}
INTEGER_FIELDS = {"PACKET_COUNT", "GNSS_SATS", "FLIGHT_SOFTWARE_STATE", "GNSS_FIX"}


@dataclass(frozen=True)
class SerialSettings:
    port: str = ""
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    timeout: float = 0.25
    write_timeout: float = 1.0
    read_buffer_size: int = 4096
    xonxoff: bool = False
    rtscts: bool = False
    dsrdtr: bool = False


def available_ports() -> list[str]:
    try:
        from serial.tools import list_ports
        return [item.device for item in list_ports.comports()]
    except Exception:
        return []


def parse_frame(line: str) -> dict[str, Any]:
    """Parse exactly 37 comma-separated fields and reject malformed frames."""
    cells = [part.strip() for part in line.strip().lstrip("\ufeff").split(",")]
    if len(cells) != len(TELEMETRY_FIELDS):
        raise ValueError(f"Expected {len(TELEMETRY_FIELDS)} fields, received {len(cells)}")
    record: dict[str, Any] = {}
    for field, raw in zip(TELEMETRY_FIELDS, cells):
        if not raw:
            raise ValueError(f"{field} is empty")
        try:
            if field in STRING_FIELDS:
                record[field] = raw
            elif field in INTEGER_FIELDS:
                number = float(raw)
                if not number.is_integer():
                    raise ValueError("not an integer")
                record[field] = int(number)
            else:
                record[field] = float(raw)
                if not math.isfinite(record[field]):
                    raise ValueError("not finite")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid {field}: {raw!r}") from exc
    if not record["TEAM_ID"] or any(c in record["TEAM_ID"] for c in "/\\:*?\"<>|\r\n"):
        raise ValueError("TEAM_ID is not a safe filename identifier")
    if not 0 <= record["FLIGHT_SOFTWARE_STATE"] < len(STATE_NAMES):
        raise ValueError("FLIGHT_SOFTWARE_STATE must be 0..7")
    if not 0 <= record["GNSS_FIX"] <= 2:
        raise ValueError("GNSS_FIX must be 0, 1, or 2")
    if not -90 <= record["GNSS_LATITUDE"] <= 90 or not -180 <= record["GNSS_LONGITUDE"] <= 180:
        raise ValueError("GNSS coordinates are out of range")
    return record
