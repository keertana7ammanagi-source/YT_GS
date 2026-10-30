# AstrasenseNexus | CAN-7USAT Ground Station

A desktop ground station for a CanSat, built for the IN-SPACe / CAN-7USAT India Student Competition 2026. It receives
telemetry from the CanSat over a LoRa/serial link, validates and stores every packet, shows live values, graphs and a map,
and adds offline analysis tools: anomaly detection, environmental fingerprinting, recovery guidance and a PDF report.
Everything runs on the laptop, with no cloud service or API key.

> **Status and honesty note.** The anomaly detector and the environmental fingerprinting are simple rule-based /
> statistical tools. They are **not** trained machine-learning models and have **not** been validated on real flight data.
> Their alerts and patterns are prompts for a person to inspect, not verdicts. See [Limitations](#limitations).

## Features

| Area | What it does |
|---|---|
| Telemetry link | Reads CSV packets from a serial/LoRa port (default 115200 baud), checks the 37-field format, and stores valid packets in SQLite |
| Dashboard | Overview, IMU & Kinematics, and GNSS & Environment tabs with live values, plus a GNSS map |
| Graph window | A second window, opened at start-up, with six live graphs: pressure, humidity, temperature, power, altitude and voltage |
| Flight Log | The latest stored packets; numbering restarts at ID 1 each time you press **Start Telemetry** (stored data is never deleted) |
| Telecommands | Start Telemetry, Stop Telemetry, Calibration 1, Calibration 2, Save EEPROM. Refused with a "No link" message if nothing is connected |
| Recovery | Averages several valid GNSS fixes after IMPACT, then gives compass direction, distance, bearing and a turn instruction. Optional USB GPS receiver for the recovery team |
| Anomaly Detection | Offline, read-only checks for engineering-limit violations, statistical outliers and data-quality problems |
| Environmental Fingerprinting | Offline summary of temperature, pressure, humidity, light and UV readings: statistics, trends and (when the data supports it) K-means patterns |
| AI Mission Report | One button writes a PDF combining the fingerprint and the anomaly detector's results |
| Simulation Mode | Generates artificial telemetry (always marked `SIM-0001`) to test the app without hardware, including a fast IMPACT test for Recovery |
| CSV export | Exports the stored telemetry |

## Requirements

* Python 3.10 or newer
* Packages (in `requirements.txt`): customtkinter, pyserial, matplotlib, tkintermapview, Pillow, setuptools, numpy, reportlab
* A desktop with a display (the app is a Tk/CustomTkinter GUI)
* Optional: a LoRa receiver on a serial port for real telemetry; a USB GPS receiver for the Recovery tab

## Install and run

```
python -m pip install -r requirements.txt
python start_app.py
```

`start_app.py` checks the Python version and the main dependencies, then launches `main.py`. If `reportlab` is missing the
app still starts and the report button tells you how to install it.

## Quick start

**With hardware**
1. Plug in the LoRa receiver, pick its port in **Communication**, and press **Connect**.
2. Press **Start Telemetry**. Packets appear on the dashboard, in the graph window and in the Flight Log.
3. If you press a telecommand with no link, the app shows a "No link" message and does **not** start Simulation Mode by itself.

**Without hardware**
1. Switch on **Simulation Mode**, then press **Start Telemetry**.
2. Use the **Fast IMPACT test** switch in the Recovery tab to reach IMPACT in about 12 packets instead of about 60.

## Telemetry packet

Each packet is one comma-separated line with exactly **37 fields**, in this order. A line with fewer or more fields is rejected
and never stored.

```
TEAM_ID, TIMESTAMP, PACKET_COUNT, ALTITUDE, PRESSURE, TEMP, VOLTAGE, GNSS_TIME, GNSS_LATITUDE, GNSS_LONGITUDE, GNSS_ALTITUDE, GNSS_SATS, ACCEL_X, ACCEL_Y, ACCEL_Z, GYRO_X, GYRO_Y, GYRO_Z, MAG_X, MAG_Y, MAG_Z, ROLL, PITCH, YAW, LIGHT_LUX, UV_INDEX, HALL_RPM, FLIGHT_SOFTWARE_STATE, REL_ALTITUDE, VERTICAL_SPEED, GNSS_GROUND_SPEED, GNSS_COURSE, GNSS_HDOP, GNSS_FIX, HUMIDITY, CURRENT, POWER
```

Flight states reported by the firmware (`FLIGHT_SOFTWARE_STATE`): 0 BOOT → 1 TEST_MODE → 2 LAUNCH_PAD → 3 ASCENT → 4 ROCKET_DEPLOY → 5 DESCENT → 6 AEROBREAK_RELEASE → 7 IMPACT.

## Tabs

* **Overview, IMU & Kinematics, GNSS & Environment:** live cards and the map. The map uses online map tiles when they are
  reachable and falls back to an offline coordinate grid otherwise.
* **Recovery:** after IMPACT (state 7) the landing point is locked from the average of 5 valid GNSS fixes (a fix needs a GNSS fix,
  sane coordinates and HDOP of 5 or less; these thresholds are tunable defaults). Enter your latitude, longitude and heading, or connect a GPS receiver
  on its own serial port. GNSS course shows horizontal ground movement only; it does not show the CanSat's vertical falling direction.
* **Flight Log:** the most recent stored packets, numbered from 1 since you last pressed Start Telemetry.
* **Anomaly Detection:** live alerts with sensor, value, score, flight state and reason. No engineering limits are configured by
  default; add only limits you can trace to a datasheet or requirement (see [ANOMALY_DETECTION.md](ANOMALY_DETECTION.md)).
* **Environmental Fingerprinting:** press **Run / Refresh Fingerprinting** (nothing runs automatically). Choose "This session" or
  "All stored data" and an optional time interval. It needs at least 10 valid observations for statistics and 30 complete ones
  before it attempts to find patterns.

## AI Mission Report (PDF)

Press **Generate AI Mission Report (PDF)** in the sidebar, choose where to save, and wait for the confirmation. The report contains the
fingerprint, the detector's status and alerts, a combined interpretation (associations, not causes) and the methods and limitations.
It describes observations only: no safety verdict and no overall score. Alerts are kept only in the running app's memory (default 200),
so the report includes only the alerts still held at that moment, and says if older ones were discarded.

## Simulation Mode

Switch on **Simulation Mode** to feed the app artificial telemetry (no hardware needed). Simulated frames carry `TEAM_ID = SIM-0001`,
and the fingerprint and the report label such data **SIMULATED**. Press **Start Telemetry** to begin. The **Fast IMPACT test** switch
in the Recovery tab shortens the time to IMPACT from about 60 packets to about 12, so you can test Recovery quickly.
Simulated data is not real telemetry and says nothing about real flight performance.

## Data and files

| Item | Where |
|---|---|
| Telemetry database | `telemetry.sqlite3` next to `main.py` (every packet that passed validation) |
| CSV export | **Export CSV** in the sidebar |
| Anomaly settings (optional) | `anomaly_config.json` next to `main.py` (see ANOMALY_DETECTION.md) |
| PDF reports | wherever you save them |

## Project layout

| File | Purpose |
|---|---|
| `start_app.py` | Checks the environment and launches the app |
| `main.py` | The application window, tabs, workers and simulator |
| `station_control.py` | The 37-field schema, packet parser/validator, flight-state names, serial settings |
| `database_handling.py` | SQLite storage, read-only history queries, CSV export |
| `telemetry_graphs.py` | The six-graph window |
| `setup_map.py` | The GNSS map with offline fallback grid |
| `deploy_graph.py` | The older separate graph viewer (no longer launched automatically) |
| `anomaly_detection.py` | The rule-based / statistical anomaly detector |
| `environmental_fingerprinting.py`, `fingerprint_charts.py` | Environmental analysis and its charts |
| `ai_report.py` | The combined PDF report |
| `offline_navigation.py` | Compass/distance/turn guidance and the landing-point estimator |
| `gps_receiver.py` | Read-only NMEA input from a USB GPS receiver |
| `tests/` | Automated tests (synthetic data only) |
| `assets/` | Team logo |

## Run the tests

```
python -m unittest discover -s tests -t .
```

On Windows the very first run can take a while because Python loads its libraries for the first time; do not press Ctrl+C while it runs.
The tests use synthetic or simulated data only. Some GUI tests are skipped automatically when no display is available.

## Limitations

* The anomaly detector and the fingerprinting are offline decision-support tools, not trained models, and have not been validated on real flight data.
* The detector cannot find every sensor failure; for example, a sensor that jumps to a new, stable, wrong value is reported once and then treated as normal.
* No engineering limits ship with the app. Do not enter a limit unless you can cite the datasheet or requirement it came from.
* Anomaly alerts are not stored in the database; they are lost when the app closes.
* Fingerprint patterns describe how readings vary in the data; they do not identify a place, a weather event or a cause.
* The landing point is an average of GNSS fixes. It reduces noise but cannot correct a systematic GNSS error.
* Updating the ground station does not update the CanSat firmware: this app only checks that incoming packets match the 37-field format above.

## Troubleshooting

| Problem | What to try |
|---|---|
| "Missing dependencies" at launch | `python -m pip install -r requirements.txt` |
| "No link" message when pressing a telecommand | Connect the serial/LoRa port first, or switch on Simulation Mode yourself |
| Report button says reportlab is missing | `python -m pip install reportlab` |
| Fingerprinting says "Insufficient valid data" | Receive more telemetry (at least 10 valid packets, about 30 for pattern finding) and run it again |
| The GPS receiver will not connect | Choose its own port; it cannot be the LoRa telemetry port. 9600 baud is common for NMEA receivers |
| Tests fail with an `asyncio` / `base_events` error | An earlier import was interrupted; run the tests again without pressing keys |

## Documentation

* [ANOMALY_DETECTION.md](ANOMALY_DETECTION.md): detector rules and tuning