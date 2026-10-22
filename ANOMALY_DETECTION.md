# Offline Anomaly Detection (rule-based / statistical)

**What it is:** a read-only analysis layer on the existing telemetry pipeline. It is *not* a trained
machine-learning model, it has *not* been evaluated on real flight data, and it does **not** detect all
sensor failures. It never transmits anything and never changes telemetry parsing, storage, charts or commands.

## Where it plugs in
* `anomaly_detection.py` - independent module (stdlib + `station_control.TELEMETRY_FIELDS` only).
* `main.py` - `_process_record` hands the detector an untouched copy of the validated record (before the ground
  station rescales CURRENT or overwrites POWER/REL_ALTITUDE/VERTICAL_SPEED). The parser's rejected frames are
  reported via `note_rejected_frame`. Every call is wrapped so a detector error can never affect telemetry.
* New tab **Anomaly Detection**: status, counters, and a log of alerts (timestamp, severity, kind, sensor, value,
  score, flight state, reason). Existing charts (`deploy_graph.py`) are unchanged: no alert markers in v1.

## Exact rules (defaults in `DetectorConfig`)
Monitored = every numeric field except TEAM_ID, TIMESTAMP, PACKET_COUNT, GNSS_TIME, FLIGHT_SOFTWARE_STATE, GNSS_FIX.

1. **Data quality** (kind `data_quality`): missing key, None/empty/non-numeric/bool, NaN, +/-inf -> alert, value
   excluded from baseline (never treated as normal). Duplicate TIMESTAMP or PACKET_COUNT -> alert and the sample is
   not scored. Backwards TIMESTAMP/PACKET_COUNT -> alert. Parser-rejected frames -> alert (sensor `FRAME`).
   Missing/invalid FLIGHT_SOFTWARE_STATE -> info alert "flight phase unavailable".
2. **Engineering limits** (kind `limit`): `limits = {SENSOR: (min, max)}`. **Empty by default** - this code base
   contains no verified hardware limits. Alerts are `warning` + "provisional" while `limits_provisional = true`,
   `critical` once you set it false. A limit-violating value is not added to the baseline.
3. **Rolling robust score** (kind `statistical`): per sensor, baseline = last `window_size` (30) accepted PRIOR
   samples; scoring starts after `min_samples` (10). `median`, `MAD = median(|x - median|)`,
   `scale = max(MAD, min_scale_abs, min_scale_rel * |median|)` (never zero -> no division by zero / infinite score),
   `z = 0.6745 * (x - median) / scale`. `|z| >= z_threshold` (6) -> warning; `>= z_critical` (12) -> critical.
   The current sample is scored before it can enter the baseline.
4. **Baseline protection:** a flagged sample is quarantined (never enters the baseline). Unflagged samples
   append normally. The only way a flagged level re-enters the baseline is rule 5.
5. **Persistent shift:** if `shift_samples` (5) consecutive flagged samples agree within `shift_tolerance_z` (3), a
   single "sustained level shift" alert is raised and that level becomes the new baseline (window replaced).
   **Limitation:** a sensor that jumps to a new, stable, *wrong* value is reported once and then treated as normal.
   Use limits and the optional flat-line check (`flatline_fields`, off by default) for such faults.
6. **Flight phase:** uses the firmware-reported FLIGHT_SOFTWARE_STATE only (the ground station computes no phase).
   Dynamic fields (altitude, pressure, vertical speed, GNSS altitude/speed, accel, gyro, mag, HALL_RPM) are baselined
   *per state*; on entering a state its baseline restarts and those fields are not scored for `settle_samples` (5).
   Limit and data-quality checks still apply. Non-dynamic fields (e.g. VOLTAGE, TEMP) keep one baseline and are
   still scored at transitions. If the state is unavailable the detector says so and falls back to phase-less
   baselines; it never invents a phase. Steady ramps are tolerated (median/MAD); strongly accelerating legitimate
   changes within one state can still produce false alarms - tune or list the field in `dynamic_fields`.
7. **Circular angles** ROLL, YAW, GNSS_COURSE: validity and limit checks only (no statistical score; wrap-around
   would cause false alarms).
8. **Deduplication:** one alert per (sensor, kind) per `cooldown_samples` (20); suppressed repeats are counted
   (`repeats` on the alert, "repeats suppressed" in the panel). Last `history_size` (200) alerts are kept in memory.

**Status:** `Invalid sensor data` (data-quality alert within `status_hold_samples`=10) > `Potential anomaly detected`
(limit/statistical alert within hold) > `Insufficient baseline samples` (no baseline has `min_samples` yet) > `Monitoring`.

## Tuning
Create `anomaly_config.json` next to `main.py` (optional; unknown keys or bad values are rejected and defaults used,
with a note in the Flight Log). Example (limit numbers are placeholders - use your hardware datasheet):
```json
{"z_threshold": 8, "min_samples": 15, "cooldown_samples": 30,
 "limits": {"VOLTAGE": [6.0, 8.4]}, "limits_provisional": false,
 "flatline_fields": ["PRESSURE"], "flatline_samples": 40}
```
Too many alerts -> raise `z_threshold`, `min_scale_rel`/`min_scale_abs`, `cooldown_samples`, `settle_samples`.
Missed spikes -> lower `z_threshold`. Slow drift is not detected by design (the baseline follows it).

## Running the tests
`python -m unittest discover -s tests -t .` (or `python -m pytest tests`). All test data is synthetic; the labelled
confusion counts only demonstrate the rules and say nothing about real-world accuracy.
