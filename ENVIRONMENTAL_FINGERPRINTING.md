# Environmental Fingerprinting and the combined AI report

**What it is:** offline, unsupervised, AI-assisted *statistical* analysis (descriptive statistics, trends and K-means
clustering on standardised features). It is **not** a trained model, has **not** been validated on real flight data,
and never names a place, weather event or atmospheric layer. Read-only: no commands, no network, no changes to
telemetry, the database or the anomaly detector.

## Data and features
* Source: validated frames already stored by the existing pipeline (SQLite `telemetry` table, read through the new
  read-only `TelemetryDatabase.history()` / `last_id()`; `recent()` is capped at 1000 rows).
* Clustered features: TEMP (degC), PRESSURE (Pa), HUMIDITY (%RH), LIGHT_LUX (lux), UV_INDEX (index). Units follow the
  existing dashboard labels (LIGHT_LUX's unit comes from the field name). The values are the station's final stored
  values (CURRENT/POWER/REL_ALTITUDE are ground-computed but are not used here).
* Context only (never clustered): ALTITUDE (firmware barometric altitude; overlaps with pressure) and
  FLIGHT_SOFTWARE_STATE (as reported by the firmware). TEAM_ID, TIMESTAMP, PACKET_COUNT, GNSS_* are not features.
* Scope: **This session** (rows stored since the application started) or **All stored data** (may contain several
  flights; the database has no mission column). Optional From/To mission-TIMESTAMP interval. Warnings are shown when
  TEAM_ID changes or TIMESTAMP decreases inside the data.

## Algorithm (`environmental_fingerprinting.py`)
1. Parse values; drop non-numeric/NaN/inf/missing values and values outside *definitional* bounds (humidity outside
   0-100 %RH, negative lux/UV, pressure below 0, temperature below absolute zero). They are counted, never replaced.
2. Per-feature statistics (min/median/mean/std/coverage), trend (first vs last quarter, linear slope), correlation with
   ALTITUDE, and per-flight-state medians.
3. Features with <50 % coverage or no variation are not clustered (reported with the reason).
4. Standardise (LIGHT_LUX is log-transformed first), then K-means (own numpy code, k-means++ starts, 10 restarts,
   deterministic seed) for k = 2..4 on rows that are complete for all clustered features.
5. A grouping is reported only if every group has >= max(5, 5 %) of the samples, silhouette >= 0.25 **and** the gap
   between groups is >= 3x the typical neighbour spacing (the gap check stops smooth gradients, which K-means happily
   cuts in two, from being called "distinct environments"). Otherwise: "no distinct groups / single pattern".
6. Needs >= 10 valid observations for any result and >= 30 complete observations before clustering is attempted.
All thresholds are in `FingerprintConfig`. Same data gives the same result (fingerprint ID `ENV-xxxxxxxx` is a hash of the result).

## Dashboard
Tab **Environmental Fingerprinting**: *Run / Refresh* (nothing runs automatically), scope menu, optional interval,
status (Waiting for sufficient telemetry / Analysis in progress / Analysis complete / Insufficient valid data /
Analysis failed), text results and charts. Analysis runs in a worker thread; the result returns through a queue
polled with `after()`. A failed analysis is never shown as a success.

## Combined PDF (`ai_report.py`)
Sidebar button **Generate AI Mission Report (PDF)** -> save dialog -> background build -> success dialog only after
the file is written (temp file + atomic replace). It uses the *same* `FingerprintResult` shown in the tab (running the
analysis first if none exists) and a copy of the live `AnomalyDetector`'s status, counts, configuration and alerts.
Sections: overview, fingerprinting, anomaly detection, combined interpretation (associations, not causes), methodology.
**Anomaly alerts exist only in the detector's in-memory history (default 200) and are not stored in the database**; the
report says how many were retained and when the memory was full. If one analysis fails, the report states that and
still includes the other. Needs `reportlab` (`python -m pip install reportlab`); without it the app still runs and the
button reports the install hint.

## Limitations
No ground truth; patterns may reflect altitude, time, sensor heating or sun exposure and these cannot be separated;
`ALTITUDE` may itself be derived from `PRESSURE` by the firmware (not confirmed in this code base); results from
earlier runs in "All stored data" have unknown origin. The firmware's transmitted packets were not checked here.

## Tests
`python -m unittest discover -s tests -t .` (or `python -m pytest tests`). Test data is synthetic.
