"""Unit tests for anomaly_detection.  All data is SYNTHETIC (see _synthetic.py)."""
import ast
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from tests._synthetic import ROOT, make_record

from anomaly_detection import (
    KIND_DATA_QUALITY, KIND_LIMIT, KIND_STATISTICAL, SEV_CRITICAL, SEV_WARNING,
    STATUS_ANOMALY, STATUS_INSUFFICIENT, STATUS_INVALID, STATUS_MONITORING,
    AnomalyDetector, DetectorConfig, load_config,
)


def run(detector, records):
    out = []
    for r in records:
        out.extend(detector.evaluate(r))
    return out


def stat_alerts(alerts, sensor=None):
    return [a for a in alerts if a.kind == KIND_STATISTICAL and (sensor is None or a.sensor == sensor)]


class NormalAndBaseline(unittest.TestCase):
    def test_normal_steady_data_no_alerts(self):
        d = AnomalyDetector()
        alerts = run(d, (make_record(i) for i in range(1, 81)))
        self.assertEqual(alerts, [])
        self.assertEqual(d.status, STATUS_MONITORING)

    def test_insufficient_baseline_never_scores(self):
        d = AnomalyDetector()
        recs = [make_record(i) for i in range(1, 10)]          # 9 < min_samples (10)
        recs[5] = make_record(6, TEMP=500)                        # huge value, but no baseline yet
        alerts = run(d, recs)
        self.assertEqual(stat_alerts(alerts), [])
        self.assertEqual(d.status, STATUS_INSUFFICIENT)

    def test_prior_samples_only_current_not_in_own_baseline(self):
        d = AnomalyDetector(DetectorConfig(min_samples=10))
        run(d, (make_record(i) for i in range(1, 11)))           # exactly 10 prior samples
        alerts = d.evaluate(make_record(11, TEMP=60))
        self.assertEqual(len(stat_alerts(alerts, "TEMP")), 1)

    def test_sudden_outlier_flagged_with_score(self):
        d = AnomalyDetector()
        run(d, (make_record(i) for i in range(1, 41)))
        alerts = d.evaluate(make_record(41, TEMP=40))
        (a,) = stat_alerts(alerts, "TEMP")
        self.assertEqual(a.value, 40.0)
        self.assertGreaterEqual(abs(a.score), d.config.z_threshold)
        self.assertEqual(a.timestamp, 41.0)
        self.assertEqual(a.packet, 41)
        self.assertEqual(d.status, STATUS_ANOMALY)

    def test_flagged_samples_do_not_distort_baseline(self):
        d = AnomalyDetector(DetectorConfig(cooldown_samples=0))
        run(d, (make_record(i) for i in range(1, 41)))
        flagged = 0
        for n, i in enumerate(range(41, 47)):
            temp = 40 if n % 2 == 0 else 25                       # spike, normal, spike, ...
            flagged += len(stat_alerts(d.evaluate(make_record(i, TEMP=temp)), "TEMP"))
        self.assertEqual(flagged, 3)                              # every spike still flagged

    def test_near_zero_variance_is_safe(self):
        d = AnomalyDetector()
        const = [make_record(i, GYRO_X=0.0, ACCEL_Z=9.8) for i in range(1, 31)]
        for r in const:
            r["GYRO_X"] = 0.0                                     # exactly constant
        self.assertEqual(run(d, const), [])
        tiny = d.evaluate(make_record(31, GYRO_X=1e-9))
        self.assertEqual(stat_alerts(tiny, "GYRO_X"), [])
        big = d.evaluate(make_record(32, GYRO_X=50.0))
        (a,) = stat_alerts(big, "GYRO_X")
        self.assertTrue(math.isfinite(a.score))

    def test_all_zero_median_has_finite_scores(self):
        d = AnomalyDetector()
        recs = [make_record(i) for i in range(1, 31)]
        for r in recs:
            r["HALL_RPM"] = 0.0
        run(d, recs)
        r = make_record(31)
        r["HALL_RPM"] = 1.0
        alerts = d.evaluate(r)
        for a in alerts:
            self.assertTrue(a.score is None or math.isfinite(a.score))


class Limits(unittest.TestCase):
    CFG = dict(limits={"VOLTAGE": (6.0, 8.4)})                    # TEST-ONLY numbers

    def test_no_limits_by_default(self):
        d = AnomalyDetector()
        self.assertEqual(d.config.limits, {})
        alerts = run(d, [make_record(1, VOLTAGE=0.0)])
        self.assertEqual([a for a in alerts if a.kind == KIND_LIMIT], [])

    def test_limit_violation_provisional_is_warning(self):
        d = AnomalyDetector(DetectorConfig(**self.CFG))
        (a,) = [x for x in d.evaluate(make_record(1, VOLTAGE=5.0)) if x.kind == KIND_LIMIT]
        self.assertEqual((a.sensor, a.severity, a.provisional, a.value), ("VOLTAGE", SEV_WARNING, True, 5.0))
        self.assertIn("minimum", a.reason)

    def test_limit_violation_confirmed_is_critical(self):
        d = AnomalyDetector(DetectorConfig(limits_provisional=False, **self.CFG))
        (a,) = [x for x in d.evaluate(make_record(1, VOLTAGE=9.0)) if x.kind == KIND_LIMIT]
        self.assertEqual(a.severity, SEV_CRITICAL)
        self.assertIn("maximum", a.reason)

    def test_limit_violating_value_not_added_to_baseline(self):
        d = AnomalyDetector(DetectorConfig(cooldown_samples=0, **self.CFG))
        run(d, (make_record(i) for i in range(1, 21)))
        size = len(d._baselines[("VOLTAGE", None)])
        d.evaluate(make_record(21, VOLTAGE=5.0))
        self.assertEqual(len(d._baselines[("VOLTAGE", None)]), size)

    def test_bad_limits_rejected(self):
        with self.assertRaises(ValueError):
            DetectorConfig(limits={"VOLTAGE": (9.0, 6.0)})
        with self.assertRaises(ValueError):
            DetectorConfig(limits={"NOT_A_FIELD": (0, 1)})
        with self.assertRaises(ValueError):
            DetectorConfig(limits={"VOLTAGE": (float("nan"), 1)})


class InvalidData(unittest.TestCase):
    def setUp(self):
        self.d = AnomalyDetector(DetectorConfig(cooldown_samples=0))
        run(self.d, (make_record(i) for i in range(1, 31)))
        self.size = len(self.d._baselines[("TEMP", None)])

    def _check(self, bad):
        r = make_record(31)
        r["TEMP"] = bad
        alerts = self.d.evaluate(r)
        dq = [a for a in alerts if a.kind == KIND_DATA_QUALITY and a.sensor == "TEMP"]
        self.assertEqual(len(dq), 1, bad)
        self.assertIsNone(dq[0].value)
        self.assertEqual(stat_alerts(alerts, "TEMP"), [])
        self.assertEqual(len(self.d._baselines[("TEMP", None)]), self.size)   # never silently "normal"
        self.assertEqual(self.d.status, STATUS_INVALID)

    def test_nan(self): self._check(float("nan"))
    def test_inf(self): self._check(float("inf"))
    def test_neg_inf(self): self._check(float("-inf"))
    def test_none(self): self._check(None)
    def test_text(self): self._check("abc")
    def test_empty_string(self): self._check("")
    def test_bool(self): self._check(True)

    def test_missing_key(self):
        r = make_record(31)
        del r["TEMP"]
        dq = [a for a in self.d.evaluate(r) if a.sensor == "TEMP"]
        self.assertEqual(dq[0].kind, KIND_DATA_QUALITY)
        self.assertIn("missing", dq[0].reason)

    def test_non_mapping_record(self):
        (a,) = self.d.evaluate("not a record")
        self.assertEqual(a.kind, KIND_DATA_QUALITY)

    def test_rejected_frame_reported_and_counted(self):
        (a,) = self.d.note_rejected_frame("Expected 37 fields, received 36")
        self.assertEqual((a.sensor, a.kind), ("FRAME", KIND_DATA_QUALITY))
        self.assertEqual(self.d.counts()["rejected_frames"], 1)
        self.assertEqual(self.d.status, STATUS_INVALID)

    def test_duplicate_timestamp_not_scored(self):
        alerts = self.d.evaluate(make_record(31, TEMP=99))
        self.assertTrue(stat_alerts(alerts, "TEMP"))              # fresh timestamp is scored
        again = self.d.evaluate(make_record(31, TEMP=99))         # same TIMESTAMP and PACKET_COUNT
        self.assertTrue(any("duplicate TIMESTAMP" in a.reason for a in again))
        self.assertEqual(stat_alerts(again), [])

    def test_backwards_timestamp_reported(self):
        alerts = self.d.evaluate(make_record(5))
        self.assertTrue(any("backwards" in a.reason for a in alerts))


class FlightPhaseAndPersistence(unittest.TestCase):
    def test_legitimate_altitude_ramp_not_flagged(self):
        d = AnomalyDetector()
        recs = [make_record(i, state=3, ALTITUDE=100 + 10 * i, GNSS_ALTITUDE=100 + 10 * i) for i in range(1, 61)]
        self.assertEqual(stat_alerts(run(d, recs), "ALTITUDE"), [])

    def test_spike_during_ramp_still_flagged(self):
        d = AnomalyDetector()
        recs = [make_record(i, state=3, ALTITUDE=100 + 10 * i) for i in range(1, 41)]
        run(d, recs)
        alerts = d.evaluate(make_record(41, state=3, ALTITUDE=5000))
        self.assertEqual(len(stat_alerts(alerts, "ALTITUDE")), 1)

    def test_state_change_jump_is_not_a_sensor_fault(self):
        d = AnomalyDetector()
        alerts = run(d, (make_record(i, state=2, ACCEL_Z=9.8) for i in range(1, 41)))
        alerts += run(d, (make_record(i, state=3, ACCEL_Z=45.0, PRESSURE=95000) for i in range(41, 81)))
        self.assertEqual(stat_alerts(alerts, "ACCEL_Z"), [])
        self.assertEqual(stat_alerts(alerts, "PRESSURE"), [])

    def test_non_dynamic_sensor_still_flagged_at_state_change(self):
        d = AnomalyDetector()
        run(d, (make_record(i, state=2) for i in range(1, 41)))
        alerts = d.evaluate(make_record(41, state=3, VOLTAGE=0.5))
        self.assertEqual(len(stat_alerts(alerts, "VOLTAGE")), 1)

    def test_unavailable_phase_is_reported_not_invented(self):
        d = AnomalyDetector()
        r = make_record(1)
        r["FLIGHT_SOFTWARE_STATE"] = None
        alerts = d.evaluate(r)
        (a,) = [x for x in alerts if x.sensor == "FLIGHT_SOFTWARE_STATE"]
        self.assertIn("flight phase unavailable", a.reason)
        self.assertIsNone(a.state)

    def test_isolated_spike_is_single_alert_then_recovers(self):
        d = AnomalyDetector(DetectorConfig(cooldown_samples=0))
        run(d, (make_record(i) for i in range(1, 41)))
        spike = d.evaluate(make_record(41, TEMP=45))
        after = run(d, (make_record(i) for i in range(42, 62)))
        self.assertEqual(len(stat_alerts(spike, "TEMP")), 1)
        self.assertEqual(stat_alerts(after, "TEMP"), [])

    def test_sustained_shift_reported_once_then_accepted(self):
        d = AnomalyDetector(DetectorConfig(cooldown_samples=0))
        run(d, (make_record(i) for i in range(1, 41)))
        alerts = run(d, (make_record(i, TEMP=45) for i in range(41, 61)))
        temp = stat_alerts(alerts, "TEMP")
        self.assertEqual(len(temp), d.config.shift_samples)       # 4 outliers + 1 shift report
        self.assertIn("sustained level shift", temp[-1].reason)
        later = run(d, (make_record(i, TEMP=45) for i in range(61, 81)))
        self.assertEqual(stat_alerts(later, "TEMP"), [])          # documented limitation: new normal

    def test_stuck_sensor_missed_by_default_but_flatline_opt_in(self):
        stuck = [make_record(i, PRESSURE=101300.0) for i in range(1, 61)]
        for r in stuck:
            r["PRESSURE"] = 101300.0
        self.assertEqual([a for a in run(AnomalyDetector(), copy.deepcopy(stuck))
                          if a.sensor == "PRESSURE"], [])
        d = AnomalyDetector(DetectorConfig(flatline_fields=("PRESSURE",), flatline_samples=20))
        flat = [a for a in run(d, stuck) if a.sensor == "PRESSURE"]
        self.assertTrue(flat and flat[0].kind == KIND_DATA_QUALITY)


class Dedup(unittest.TestCase):
    def test_persistent_anomaly_does_not_flood(self):
        d = AnomalyDetector(DetectorConfig(cooldown_samples=20, shift_samples=100, window_size=100))
        run(d, (make_record(i) for i in range(1, 41)))
        alerts = run(d, (make_record(i, TEMP=60) for i in range(41, 66)))   # 25 anomalous samples
        temp = stat_alerts(alerts, "TEMP")
        self.assertLessEqual(len(temp), 2)
        self.assertGreater(d.suppressed, 15)
        self.assertGreater(d.history[0].repeats if d.history[0].sensor == "TEMP" else 2, 1)

    def test_distinct_sensors_alert_independently(self):
        d = AnomalyDetector()
        run(d, (make_record(i) for i in range(1, 41)))
        alerts = d.evaluate(make_record(41, TEMP=60, HUMIDITY=5))
        self.assertEqual({a.sensor for a in stat_alerts(alerts)}, {"TEMP", "HUMIDITY"})


class ConfigAndSafety(unittest.TestCase):
    def test_input_record_not_mutated(self):
        d = AnomalyDetector()
        r = make_record(1)
        before = copy.deepcopy(r)
        d.evaluate(r)
        self.assertEqual(r, before)

    def test_config_validation(self):
        for bad in (dict(min_samples=2), dict(window_size=5, min_samples=10), dict(z_threshold=0),
                    dict(z_threshold=float("nan")), dict(z_critical=1.0, z_threshold=6.0),
                    dict(min_scale_abs=0, min_scale_rel=0), dict(shift_samples=1)):
            with self.assertRaises(ValueError, msg=str(bad)):
                DetectorConfig(**bad)

    def test_load_config_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"z_threshold": 8, "limits": {"VOLTAGE": [6, 8.4]},
                                         "flatline_fields": ["PRESSURE"]}))
            cfg = load_config(path)
            self.assertEqual((cfg.z_threshold, cfg.limits["VOLTAGE"]), (8, (6.0, 8.4)))
            path.write_text(json.dumps({"bogus": 1}))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_module_has_no_communication_imports(self):
        tree = ast.parse((ROOT / "anomaly_detection.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"serial", "socket", "tkinter", "customtkinter", "subprocess",
                                     "threading", "sqlite3", "os"}, imported)


class LabelledSyntheticEvaluation(unittest.TestCase):
    """Confusion counts on SYNTHETIC labelled streams.  Says nothing about real-flight accuracy."""

    @staticmethod
    def _confusion(stream, labels, sensor="TEMP", cfg=None):
        d = AnomalyDetector(cfg or DetectorConfig(cooldown_samples=0))
        tp = fp = tn = fn = 0
        for rec, label in zip(stream, labels):
            predicted = bool(stat_alerts(d.evaluate(rec), sensor))
            tp += predicted and label
            fp += predicted and not label
            fn += (not predicted) and label
            tn += (not predicted) and not label
        return tp, fp, tn, fn

    def test_isolated_spikes(self):
        spikes = {50, 90, 130}
        stream = [make_record(i, TEMP=45 if i in spikes else 25) for i in range(1, 161)]
        labels = [i in spikes for i in range(1, 161)]
        result = self._confusion(stream, labels)
        print(f"\n[SYNTHETIC isolated spikes] TP={result[0]} FP={result[1]} TN={result[2]} FN={result[3]}")
        self.assertEqual(result, (3, 0, 157, 0))

    def test_sustained_shift_shows_documented_misses(self):
        shifted = set(range(60, 75))
        stream = [make_record(i, TEMP=45 if i in shifted else 25) for i in range(1, 121)]
        labels = [i in shifted for i in range(1, 121)]
        result = self._confusion(stream, labels)
        print(f"\n[SYNTHETIC sustained 15-sample shift] TP={result[0]} FP={result[1]} TN={result[2]} FN={result[3]}"
              " (FNs expected: new level accepted; FPs = return to old level flagged)")
        # TP=5: four outliers plus the level-shift report.  FN=10: samples after the level was accepted.
        # FP=5: after accepting 45 as the new normal, the return to 25 is itself flagged as a change
        # (labelled "normal" here).  Both effects follow from the documented baseline policy.
        self.assertEqual((result[0], result[1], result[3]), (5, 5, 10))

    def test_flight_like_ramp_has_no_false_positives(self):
        stream = [make_record(i, state=3 if i > 30 else 2, ALTITUDE=100 + (15 * (i - 30) if i > 30 else 0),
                              ACCEL_Z=9.8 if i <= 30 else 25.0) for i in range(1, 121)]
        result = self._confusion(stream, [False] * 120, sensor="ALTITUDE")
        result2 = self._confusion(stream, [False] * 120, sensor="ACCEL_Z")
        print(f"\n[SYNTHETIC flight-like] ALTITUDE FP={result[1]} | ACCEL_Z FP={result2[1]}")
        self.assertEqual((result[1], result2[1]), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
