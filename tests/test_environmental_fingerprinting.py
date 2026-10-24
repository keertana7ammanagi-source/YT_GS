"""Tests for environmental_fingerprinting and the read-only database history.  SYNTHETIC data only."""
import copy
import gc
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests._env_data import minimal_rows, three_regime_records
from tests._synthetic import make_record

import environmental_fingerprinting as ef
from environmental_fingerprinting import (STATUS_COMPLETE, STATUS_FAILED, STATUS_INSUFFICIENT, FingerprintConfig,
                                          analyze_environment, format_fingerprint_text)
from database_handling import TelemetryDatabase
from station_control import TELEMETRY_FIELDS


class ValidAnalysis(unittest.TestCase):
    def test_structured_result_for_valid_records(self):
        r = analyze_environment(three_regime_records(), scope_label="test scope", data_source="SYNTHETIC TEST DATA")
        self.assertEqual(r.status, STATUS_COMPLETE)
        self.assertTrue(r.fingerprint_id.startswith("ENV-"))
        self.assertEqual((r.n_records_input, r.n_observations, r.n_complete), (150, 150, 150))
        self.assertEqual((r.t_min, r.t_max), (1.0, 150.0))
        self.assertEqual(r.team_ids, ["1234"])
        self.assertEqual([s.name for s in r.feature_stats], list(ef.FEATURE_NAMES))
        self.assertTrue(all(s.coverage == 1.0 and s.used_in_clustering for s in r.feature_stats))
        self.assertTrue(r.clustering.found)
        self.assertEqual(r.clustering.k, 3)
        self.assertEqual([c.letter for c in r.clusters], ["A", "B", "C"])
        self.assertEqual(sum(c.n for c in r.clusters), 150)
        # clusters are ordered by time and match the three regimes
        self.assertEqual([(c.t_min, c.t_max) for c in r.clusters], [(1.0, 39.0), (40.0, 99.0), (100.0, 150.0)])
        self.assertEqual(r.clusters[0].states, {"LAUNCH_PAD": 39})
        self.assertTrue(r.explanation and r.limitations)
        self.assertIn(r.fingerprint_id, r.name)
        self.assertEqual(r.series.labels.count(-1), 0)

    def test_text_rows_like_the_database_are_parsed(self):
        r = analyze_environment(minimal_rows())
        self.assertEqual((r.status, r.clustering.k), (STATUS_COMPLETE, 3))

    def test_trends_and_altitude_context(self):
        r = analyze_environment(three_regime_records())
        pressure = next(t for t in r.trends if t.name == "PRESSURE")
        self.assertLess(pressure.net_change, 0)
        self.assertLess(pressure.altitude_correlation, -0.9)
        self.assertEqual([p.state_name for p in r.phases], ["LAUNCH_PAD", "ASCENT", "DESCENT"])
        self.assertNotIn("ALTITUDE", r.clustering.features_used)       # context only, never clustered

    def test_reproducible(self):
        a = analyze_environment(three_regime_records())
        b = analyze_environment(three_regime_records())
        self.assertEqual(a, b)
        self.assertEqual(a.fingerprint_id, b.fingerprint_id)

    def test_input_records_not_mutated(self):
        rows = minimal_rows() + three_regime_records(20)
        rows.append({"TIMESTAMP": "bad", "TEMP": float("nan")})
        before = copy.deepcopy(rows)
        analyze_environment(rows)
        self.assertEqual(repr(rows), repr(before))

    def test_interval_selection(self):
        r = analyze_environment(three_regime_records(), t_start=40, t_end=99)
        self.assertEqual((r.t_min, r.t_max, r.n_observations, r.n_outside_interval), (40.0, 99.0, 60, 90))
        self.assertEqual(r.interval_requested, (40, 99))


class InsufficientAndInvalid(unittest.TestCase):
    def test_insufficient_data_is_explicit(self):
        r = analyze_environment(three_regime_records(5))
        self.assertEqual(r.status, STATUS_INSUFFICIENT)
        self.assertIn("Insufficient valid data", r.message)
        self.assertEqual((r.clusters, r.trends, r.fingerprint_id), ([], [], None))
        self.assertNotIn("complete", format_fingerprint_text(r).splitlines()[0].lower())

    def test_empty_and_all_invalid_inputs(self):
        for rows in ([], [{"TIMESTAMP": "x"}], [{}] * 20, ["nope"] * 20):
            r = analyze_environment(rows)
            self.assertEqual(r.status, STATUS_INSUFFICIENT, rows[:1])

    def test_missing_nan_inf_text_bool_and_out_of_range(self):
        rows = three_regime_records()
        rows[0]["TEMP"] = float("nan")
        rows[1]["TEMP"] = float("inf")
        rows[2]["TEMP"] = "abc"
        rows[3]["TEMP"] = True
        rows[4]["TEMP"] = None
        del rows[5]["TEMP"]
        rows[6]["HUMIDITY"] = 150.0           # outside definitional 0-100 %RH
        rows[7]["LIGHT_LUX"] = -5.0           # lux cannot be negative
        rows[8]["TIMESTAMP"] = "nan"          # row dropped entirely
        rows.append("not a record")
        r = analyze_environment(rows)
        self.assertEqual(r.status, STATUS_COMPLETE)
        temp = next(s for s in r.feature_stats if s.name == "TEMP")
        self.assertEqual(temp.n_invalid, 6)          # nan, inf, text, bool, None and a missing key
        self.assertEqual(next(s for s in r.feature_stats if s.name == "HUMIDITY").n_out_of_range, 1)
        self.assertEqual(next(s for s in r.feature_stats if s.name == "LIGHT_LUX").n_out_of_range, 1)
        self.assertEqual(r.dropped.get("invalid or missing TIMESTAMP"), 1)
        self.assertEqual(r.dropped.get("record is not a mapping"), 1)
        self.assertEqual(r.n_observations, 149)       # 150 rows minus the one with an invalid TIMESTAMP
        self.assertEqual(r.n_complete, 149 - 8)       # 8 rows have an invalid/out-of-range value in a clustered feature
        self.assertLess(r.n_complete, r.n_observations)
        for v in r.series.values["TEMP"]:
            self.assertTrue(v is None or math.isfinite(v))
        self.assertTrue(any("excluded" in w for w in r.warnings))

    def test_constant_feature_is_reported_not_clustered(self):
        rows = three_regime_records()
        for r in rows:
            r["UV_INDEX"] = 3.0
        res = analyze_environment(rows)
        uv = next(s for s in res.feature_stats if s.name == "UV_INDEX")
        self.assertFalse(uv.used_in_clustering)
        self.assertIn("constant", uv.note)
        self.assertNotIn("UV_INDEX", res.clustering.features_used)
        self.assertEqual(res.status, STATUS_COMPLETE)

    def test_all_features_constant_does_not_crash_or_cluster(self):
        rows = three_regime_records(60)
        for r in rows:
            r.update(TEMP=25.0, PRESSURE=100000.0, HUMIDITY=50.0, LIGHT_LUX=500.0, UV_INDEX=2.0)
        res = analyze_environment(rows)
        self.assertEqual(res.status, STATUS_COMPLETE)
        self.assertFalse(res.clustering.attempted)
        self.assertEqual(res.clusters, [])
        self.assertIn("not attempted", res.clustering.reason)

    def test_too_few_complete_rows_does_not_force_clustering(self):
        res = analyze_environment(three_regime_records(25))
        self.assertEqual(res.status, STATUS_COMPLETE)
        self.assertFalse(res.clustering.attempted)
        self.assertIn("at least 30", res.clustering.reason)

    def test_sparse_features_use_only_what_exists(self):
        rows = three_regime_records()
        for r in rows:
            for name in ("HUMIDITY", "LIGHT_LUX", "UV_INDEX"):
                r[name] = None
        res = analyze_environment(rows)
        self.assertEqual(res.clustering.features_used, ["TEMP", "PRESSURE"])
        hum = next(s for s in res.feature_stats if s.name == "HUMIDITY")
        self.assertEqual((hum.n_valid, hum.minimum, hum.median), (0, None, None))      # nothing fabricated


class NoInventedGroups(unittest.TestCase):
    def test_random_noise_single_regime_is_not_split(self):
        rng = np.random.default_rng(5)
        rows = [{"TEAM_ID": "1", "TIMESTAMP": i, "TEMP": 25 + rng.normal(), "PRESSURE": 1e5 + 30 * rng.normal(),
                 "HUMIDITY": 50 + rng.normal(), "LIGHT_LUX": 800 + 20 * rng.normal(), "UV_INDEX": 3 + 0.1 * rng.normal()}
                for i in range(200)]
        res = analyze_environment(rows)
        self.assertFalse(res.clustering.found)
        self.assertEqual(res.clusters, [])
        self.assertIn("No distinct groups", res.clustering.reason)

    def test_smooth_ramp_is_not_reported_as_distinct_groups(self):
        rng = np.random.default_rng(3)
        rows = [{"TEAM_ID": "1", "TIMESTAMP": i, "TEMP": 30 - 8 * i / 199 + 0.1 * rng.normal(),
                 "PRESSURE": 1e5 - 6000 * i / 199 + 10 * rng.normal(), "HUMIDITY": 40 + 15 * i / 199 + 0.2 * rng.normal(),
                 "LIGHT_LUX": 800 + 14000 * i / 199, "UV_INDEX": 3 + 4 * i / 199} for i in range(200)]
        res = analyze_environment(rows)
        self.assertFalse(res.clustering.found)
        self.assertTrue(res.clustering.candidates)                      # k values were evaluated and rejected
        self.assertTrue(all(c[3] != "distinct" for c in res.clustering.candidates if c[1] is not None))

    def test_scope_warnings_for_multiple_missions(self):
        rows = three_regime_records(60)
        for r in rows[30:]:
            r["TEAM_ID"] = "9999"
        for i, r in enumerate(rows[30:]):
            r["TIMESTAMP"] = float(i + 1)                                 # clock restarts
        res = analyze_environment(rows)
        self.assertEqual(res.team_ids, ["1234", "9999"])
        self.assertTrue(any("TEAM_ID" in w for w in res.warnings))
        self.assertTrue(any("mission boundary" in w for w in res.warnings))


class FailureHandling(unittest.TestCase):
    def test_internal_error_returns_failed_result_never_success(self):
        with mock.patch.object(ef, "_analyze", side_effect=RuntimeError("boom")):
            res = analyze_environment(three_regime_records())
        self.assertEqual(res.status, STATUS_FAILED)
        self.assertIn("Analysis failed", res.message)
        self.assertIsNone(res.fingerprint_id)
        self.assertEqual(format_fingerprint_text(res).strip(), res.message)
        self.assertNotIn("complete", res.message.lower())

    def test_bad_config_rejected(self):
        with self.assertRaises(ValueError):
            FingerprintConfig(k_min=1)


class DatabaseHistory(unittest.TestCase):
    def test_history_last_id_and_read_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.addCleanup(gc.collect)
            path = Path(tmp) / "t.sqlite3"
            db = TelemetryDatabase(path)
            self.assertEqual(db.last_id(), 0)
            for i in range(1, 6):
                db.submit(make_record(i))
            db.close()
            db = TelemetryDatabase(path)
            self.assertEqual(db.last_id(), 5)
            rows = db.history(2, ("TIMESTAMP", "TEMP", "TEAM_ID"))
            self.assertEqual([r["id"] for r in rows], [3, 4, 5])
            self.assertEqual(set(rows[0]), {"id", "TIMESTAMP", "TEMP", "TEAM_ID"})
            self.assertEqual(len(db.history(0)), 5)
            self.assertEqual(db.last_id(), 5)                            # reading changed nothing
            with self.assertRaises(ValueError):
                db.history(0, ("TEMP; DROP TABLE telemetry",))
            self.assertEqual(len(db.history(0)), 5)
            db.close()
            result = analyze_environment(TelemetryDatabase(path).history(0, ef.REQUIRED_COLUMNS))
            self.assertEqual(result.n_records_input, 5)
            del result
            gc.collect()


if __name__ == "__main__":
    unittest.main(verbosity=2)
