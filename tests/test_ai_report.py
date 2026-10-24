"""Tests for ai_report (combined PDF).  SYNTHETIC data only."""
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import ai_report
from ai_report import (AnomalySnapshot, ReportInputs, alert_associations, build_combined_interpretation, build_story,
                       generate_report, snapshot_anomaly_detector, story_text)
from anomaly_detection import KIND_STATISTICAL, SEV_WARNING, Alert, AnomalyDetector, DetectorConfig
from environmental_fingerprinting import analyze_environment, failed_result
from tests._env_data import PHASES, env_values, phase_of, three_regime_records
from tests._synthetic import make_record


def detector_with_alerts(history_size=200):
    """Real AnomalyDetector fed synthetic records: a TEMP spike, a NaN voltage and a rejected frame."""
    det = AnomalyDetector(DetectorConfig(history_size=history_size))
    records = three_regime_records()
    for i, rec in enumerate(records, start=1):
        rec = dict(rec)
        if i == 70:
            rec["TEMP"] = 60.0
        if i == 71:
            rec["VOLTAGE"] = float("nan")
        det.evaluate(rec)
    det.note_rejected_frame("Expected 37 fields, received 36")
    return det, records


def page_count(path) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", Path(path).read_bytes()))


def fake_alerts(n):
    return [Alert("TEMP", KIND_STATISTICAL, SEV_WARNING, f"synthetic alert #{i}", 40.0 + i, 7.5, float(i % 150 + 1), i, 3)
            for i in range(n)]


class ReportContent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.det, cls.records = detector_with_alerts()
        cls.fp = analyze_environment(cls.records, scope_label="test scope", data_source="SYNTHETIC TEST DATA")
        cls.inputs = ReportInputs(cls.fp, snapshot_anomaly_detector(cls.det), "1234", "SYNTHETIC TEST DATA")
        cls.text = story_text(build_story(cls.inputs))

    def test_all_required_sections_present(self):
        for heading in (ai_report.REPORT_TITLE, "1. Mission and report overview", "Executive summary", "Overall limitations",
                        "2. Environmental fingerprinting", "3. Anomaly detection", "4. Combined interpretation",
                        "5. Methodology and limitations", "Temporal associations (not causation)"):
            self.assertIn(heading, self.text, heading)

    def test_report_uses_the_actual_fingerprint_and_detector_results(self):
        self.assertIn(self.fp.fingerprint_id, self.text)
        self.assertIn("1234", self.text)
        counts = self.det.counts()
        self.assertIn(f"{counts['limit']} / {counts['statistical']} / {counts['data_quality']}", self.text)
        self.assertIn(str(self.det.suppressed), self.text)
        for alert in self.det.history:
            self.assertIn(alert.reason[:40].replace("\u00b0", "?"), self.text)
        self.assertIn("Expected 37 fields, received 36", self.text)
        self.assertIn("30 / 10", self.text)                                   # window_size / min_samples from the config
        self.assertIn("none configured", self.text)                           # no engineering limits configured
        self.assertIn("Pattern A", self.text)

    def test_states_that_alerts_live_only_in_memory(self):
        self.assertIn("running detector's memory", self.text)
        self.assertIn("not stored in the database", self.text)

    def test_no_verdict_or_overall_score(self):
        low = self.text.lower()
        self.assertIn("no safety verdict", low)
        for forbidden in ("mission success probability", "overall ai score", "the cansat is safe", "the cansat is unsafe"):
            self.assertNotIn(forbidden, low)

    def test_snapshot_is_a_read_only_copy(self):
        det, _ = detector_with_alerts()
        before = (det.status, dict(det.counts()), len(det.history), det.suppressed, [a.repeats for a in det.history])
        snap = snapshot_anomaly_detector(det)
        snap.alerts[0].repeats = 999
        snap.alerts.clear()
        self.assertEqual((det.status, dict(det.counts()), len(det.history), det.suppressed, [a.repeats for a in det.history]), before)

    def test_generating_report_does_not_change_detector(self):
        det, _ = detector_with_alerts()
        before = (det.status, dict(det.counts()), list(det.history))
        with tempfile.TemporaryDirectory() as tmp:
            generate_report(Path(tmp) / "r.pdf", ReportInputs(None, snapshot_anomaly_detector(det)))
        self.assertEqual((det.status, dict(det.counts()), list(det.history)), before)


class Honesty(unittest.TestCase):
    def test_empty_alert_history_is_stated(self):
        snap = snapshot_anomaly_detector(AnomalyDetector())
        text = story_text(build_story(ReportInputs(analyze_environment(three_regime_records()), snap)))
        self.assertIn("No alerts are held in detector memory", text)
        self.assertIn("does not prove that no anomaly", text)

    def test_insufficient_environmental_data_is_stated(self):
        fp = analyze_environment(three_regime_records(5))
        text = story_text(build_story(ReportInputs(fp, snapshot_anomaly_detector(AnomalyDetector()))))
        self.assertIn("insufficient valid data", text.lower())
        self.assertIn("No fingerprint, clusters or trends were produced", text)
        self.assertNotIn("2.4 Identified patterns", text)

    def test_failed_fingerprint_is_reported_and_anomaly_results_kept(self):
        det, _ = detector_with_alerts()
        fp = failed_result("RuntimeError: boom", "scope", "src")
        text = story_text(build_story(ReportInputs(fp, snapshot_anomaly_detector(det))))
        self.assertIn("Analysis failed: RuntimeError: boom", text)
        self.assertIn("No fingerprint results are included", text)
        self.assertIn("3.4 Alert table", text)
        self.assertIn("outlier vs rolling median", text)

    def test_unavailable_detector_is_reported_and_fingerprint_kept(self):
        fp = analyze_environment(three_regime_records())
        text = story_text(build_story(ReportInputs(fp, AnomalySnapshot(available=False, error="detector missing"))))
        self.assertIn("Anomaly detector results were not available: detector missing", text)
        self.assertIn(fp.fingerprint_id, text)

    def test_full_alert_memory_is_disclosed(self):
        det, _ = detector_with_alerts(history_size=5)
        snap = snapshot_anomaly_detector(det)
        self.assertTrue(snap.history_full)
        text = story_text(build_story(ReportInputs(None, snap)))
        self.assertIn("older alerts were discarded and are NOT included", text)

    def test_association_is_described_as_association_not_causation(self):
        fp = analyze_environment(three_regime_records())
        alerts = [Alert("TEMP", KIND_STATISTICAL, SEV_WARNING, "x", 1.0, 7.0, t, 1, 3) for t in (20.0, 70.0, 130.0, 1000.0)]
        info = alert_associations(fp, alerts)
        self.assertEqual(info["by_pattern"], {"Pattern A": 1, "Pattern B": 1, "Pattern C": 1})
        self.assertEqual(info["outside_data"], 1)
        self.assertEqual(info["transitions"], 2)
        text = "\n".join(p for _, ps in build_combined_interpretation(ReportInputs(fp, AnomalySnapshot(alerts=alerts, counts={}, config={})))
                         for p in ps)
        self.assertIn("temporal associations only", text.lower())
        self.assertIn("does not show", text)
        self.assertNotIn("caused by", text.lower())


class PdfFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.det, self.records = detector_with_alerts()
        self.fp = analyze_environment(self.records, scope_label="s", data_source="SYNTHETIC TEST DATA")

    def test_valid_nonempty_pdf_at_chosen_path(self):
        target = self.dir / "my_report.pdf"
        out = generate_report(target, ReportInputs(self.fp, snapshot_anomaly_detector(self.det), "1234"))
        data = out.read_bytes()
        self.assertEqual(out, target)
        self.assertTrue(data.startswith(b"%PDF-") and b"%%EOF" in data[-2048:])
        self.assertGreater(len(data), 20_000)                                  # includes the charts
        self.assertGreaterEqual(page_count(out), 5)
        self.assertEqual(list(self.dir.glob("*.partial")), [])

    def test_long_alert_list_paginates_and_stays_in_bounds(self):
        small = generate_report(self.dir / "small.pdf", ReportInputs(self.fp, snapshot_anomaly_detector(self.det)))
        snap = AnomalySnapshot(status="Potential anomaly detected", alerts=fake_alerts(400), counts={"statistical": 400},
                               history_capacity=500, config={"limits": {}})
        big = generate_report(self.dir / "big.pdf", ReportInputs(self.fp, snap))
        self.assertGreater(page_count(big), page_count(small) + 5)
        text = story_text(build_story(ReportInputs(self.fp, snap)))
        self.assertIn("synthetic alert #0", text)
        self.assertIn("synthetic alert #399", text)                            # nothing silently truncated
        for flow in build_story(ReportInputs(self.fp, snap)):
            if hasattr(flow, "_colWidths"):
                self.assertLessEqual(sum(flow._colWidths), _ctx_width() + 0.01)

    def test_long_reason_text_wraps_inside_cells(self):
        alert = Alert("TEMP", KIND_STATISTICAL, SEV_WARNING, "very long reason " * 80, 1.0, 9.0, 5.0, 1, 3)
        snap = AnomalySnapshot(alerts=[alert], counts={}, config={})
        out = generate_report(self.dir / "wrap.pdf", ReportInputs(None, snap))
        self.assertGreater(out.stat().st_size, 1000)

    def test_xml_like_text_in_alert_does_not_break_report(self):
        alert = Alert("TEMP<script>", KIND_STATISTICAL, SEV_WARNING, "a < b & c > d </b> \u2265 \u00b0C", 1.0, 9.0, 5.0, 1, 3)
        snap = AnomalySnapshot(alerts=[alert], counts={}, config={})
        generate_report(self.dir / "x.pdf", ReportInputs(None, snap))

    def test_both_analyses_missing_still_produces_honest_pdf(self):
        out = generate_report(self.dir / "none.pdf", ReportInputs(None, None))
        text = story_text(build_story(ReportInputs(None, None)))
        self.assertIn("was not run", text)
        self.assertGreater(out.stat().st_size, 1000)

    def test_failure_leaves_existing_file_and_no_partial(self):
        target = self.dir / "keep.pdf"
        target.write_bytes(b"ORIGINAL")
        with mock.patch.object(ai_report, "build_story", side_effect=RuntimeError("layout exploded")):
            with self.assertRaises(RuntimeError):
                generate_report(target, ReportInputs(self.fp, snapshot_anomaly_detector(self.det)))
        self.assertEqual(target.read_bytes(), b"ORIGINAL")
        self.assertEqual(list(self.dir.glob("*.partial")), [])

    def test_missing_destination_folder_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            generate_report(self.dir / "nope" / "r.pdf", ReportInputs(self.fp, None))

    def test_missing_reportlab_gives_install_hint(self):
        with mock.patch.object(ai_report, "_rl", side_effect=ImportError("reportlab is not installed. Install it with: python -m pip install reportlab")):
            with self.assertRaises(ImportError) as ctx:
                generate_report(self.dir / "r.pdf", ReportInputs(self.fp, None))
        self.assertIn("pip install reportlab", str(ctx.exception))

    def test_chart_failure_does_not_lose_the_report(self):
        inputs = ReportInputs(self.fp, snapshot_anomaly_detector(self.det))
        with mock.patch("fingerprint_charts.draw_feature_timelines", side_effect=RuntimeError("no chart")):
            out = generate_report(self.dir / "nochart.pdf", inputs)
            text = story_text(build_story(inputs))
        self.assertTrue(out.exists() and out.stat().st_size > 1000)
        self.assertIn("Feature timeline chart could not be rendered: RuntimeError: no chart", text)
        self.assertIn("3. Anomaly detection", text)                           # later sections still present


def _ctx_width() -> float:
    return ai_report._Ctx().width


if __name__ == "__main__":
    unittest.main(verbosity=2)
