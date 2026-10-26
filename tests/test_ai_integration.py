"""main.py integration of fingerprinting + report: workers, queue hand-back, failure isolation.
Tk/customtkinter are stubbed (see test_pipeline_regression), so real widgets and threads' GUI side are not exercised here;
the headless smoke run in the project notes covers the real window."""
import queue
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from tests._env_data import minimal_rows, three_regime_records
from tests._synthetic import make_record
from tests.test_pipeline_regression import _import_main_with_stubs

from ai_report import AnomalySnapshot
from anomaly_detection import AnomalyDetector
from environmental_fingerprinting import STATUS_COMPLETE, STATUS_FAILED, analyze_environment, failed_result


class FakeDb:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def history(self, since_id=0, columns=None):
        self.calls.append((since_id, columns))
        if self.error:
            raise self.error
        return list(self.rows)

    def last_id(self):
        return 7


class AiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_with_stubs()

    def _station(self, db=None):
        gs = object.__new__(self.main.GroundStation)
        gs.logs = []
        gs._log = gs.logs.append
        gs.db = db or mock.MagicMock()
        gs.broadcaster, gs.flight_map, gs.serial_worker = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        gs.inbox, gs.log_queue, gs.ai_queue = queue.Queue(), queue.Queue(), queue.Queue()
        gs.after = mock.MagicMock()
        gs.anomaly_detector = AnomalyDetector()
        gs.graph_data = mock.MagicMock()
        gs._anomaly_error_logged = False
        for name in ("anomaly_status_var", "anomaly_status_label", "anomaly_counts_var", "anomaly_box", "fp_run_button",
                     "report_button", "fp_status_var", "fp_status_label", "fp_summary_var", "fp_text", "fp_scope_menu",
                     "fp_from_entry", "fp_to_entry"):
            setattr(gs, name, mock.MagicMock())
        gs.fp_figure = gs.fp_canvas = None
        gs._update_cards = mock.MagicMock()
        gs.fingerprint_result, gs._fingerprint_running, gs._report_running, gs._pending_report = None, False, False, None
        gs.session_start_id, gs.last_team_id = 7, "1234"
        for name, default in (("packet_count", 0), ("lost_packets", 0), ("last_packet", None), ("previous_altitude", None),
                              ("altitude_baseline", None), ("flight_started_at", None)):
            setattr(gs, name, default)
        gs.speed_samples = deque(maxlen=3)
        gs.landing_coordinates = None            # attributes used by the newer _process_record
        gs.landing_var = mock.MagicMock()
        gs._update_navigation = mock.MagicMock()
        return gs

    def statuses(self, gs):
        return [c.args[0] for c in gs.fp_status_var.set.call_args_list]

    # ---- fingerprint worker / UI hand-back
    def test_worker_reads_history_read_only_and_posts_complete_result(self):
        db = FakeDb(minimal_rows())
        q = queue.Queue()
        self.main.GroundStation._fingerprint_worker(db, q, 7, None, None, "scope", "source")
        kind, result = q.get_nowait()
        self.assertEqual((kind, result.status), ("fingerprint", STATUS_COMPLETE))
        self.assertEqual(db.calls[0][0], 7)
        self.assertEqual((result.scope_label, result.data_source), ("scope", "source"))

    def test_worker_failure_becomes_failed_result_not_exception(self):
        q = queue.Queue()
        self.main.GroundStation._fingerprint_worker(FakeDb(error=RuntimeError("db locked")), q, 0, None, None, "s", "d")
        kind, result = q.get_nowait()
        self.assertEqual((kind, result.status), ("fingerprint", STATUS_FAILED))
        self.assertIn("db locked", result.message)

    def test_failed_result_is_never_shown_as_success(self):
        gs = self._station()
        gs._set_fp_status = mock.MagicMock()
        gs._fingerprint_running = True
        gs._handle_ai_message(("fingerprint", failed_result("RuntimeError: x")))
        gs._set_fp_status.assert_called_once()
        self.assertEqual(gs._set_fp_status.call_args.args[0], "Analysis failed")
        self.assertNotIn("Analysis complete", [c.args[0] for c in gs._set_fp_status.call_args_list])
        self.assertFalse(gs._fingerprint_running)
        shown = gs.fp_text.insert.call_args.args[1]
        self.assertNotIn("ENVIRONMENTAL STATISTICS", shown)

    def test_complete_and_insufficient_statuses(self):
        gs = self._station()
        gs._set_fp_status = mock.MagicMock()
        gs._handle_ai_message(("fingerprint", analyze_environment(three_regime_records())))
        gs._handle_ai_message(("fingerprint", analyze_environment(three_regime_records(4))))
        self.assertEqual([c.args[0] for c in gs._set_fp_status.call_args_list], ["Analysis complete", "Insufficient valid data"])

    def test_invalid_interval_text_fails_without_starting_a_thread(self):
        gs = self._station()
        gs._set_fp_status = mock.MagicMock()
        gs.fp_from_entry.get.return_value = "abc"
        gs.fp_to_entry.get.return_value = ""
        with mock.patch.object(self.main.threading, "Thread") as thread:
            gs._run_fingerprint()
        thread.assert_not_called()
        self.assertEqual(gs._set_fp_status.call_args.args[0], "Analysis failed")
        self.assertFalse(gs._fingerprint_running)

    def test_run_starts_background_thread_with_session_scope(self):
        gs = self._station()
        gs._set_fp_status = mock.MagicMock()
        gs.fp_from_entry.get.return_value, gs.fp_to_entry.get.return_value = "10", "50"
        gs.fp_scope_menu.get.return_value = self.main.SCOPE_SESSION
        with mock.patch.object(self.main.threading, "Thread") as thread:
            gs._run_fingerprint()
        args = thread.call_args.kwargs["args"]
        self.assertEqual((args[2], args[3], args[4]), (7, 10.0, 50.0))        # since_id, t_start, t_end
        thread.return_value.start.assert_called_once()
        self.assertTrue(gs._fingerprint_running)
        self.assertEqual(gs._set_fp_status.call_args.args[0], "Analysis in progress")

    # ---- report button flow
    def test_cancelled_dialog_does_nothing(self):
        gs = self._station()
        gs._start_report = gs._run_fingerprint = mock.MagicMock()
        with mock.patch.object(self.main.filedialog, "asksaveasfilename", return_value=""):
            gs._generate_report()
        gs._start_report.assert_not_called()
        gs._run_fingerprint.assert_not_called()

    def test_report_uses_the_dashboard_result_when_available(self):
        gs = self._station()
        gs.fingerprint_result = analyze_environment(three_regime_records())
        gs._start_report = mock.MagicMock()
        with mock.patch.object(self.main.filedialog, "asksaveasfilename", return_value="/tmp/x.pdf"):
            gs._generate_report()
        job = gs._start_report.call_args.args[0]
        self.assertEqual((job["path"], job["team"]), ("/tmp/x.pdf", "1234"))
        self.assertIsInstance(job["anomaly"], AnomalySnapshot)

    def test_report_job_records_whether_simulation_was_used(self):
        for seen in (False, True):
            gs = self._station()
            gs.fingerprint_result = analyze_environment(three_regime_records())
            gs.simulation_seen = seen
            gs._start_report = mock.MagicMock()
            with mock.patch.object(self.main.filedialog, "asksaveasfilename", return_value="/tmp/x.pdf"):
                gs._generate_report()
            self.assertEqual(gs._start_report.call_args.args[0]["simulation"], seen)

    def test_report_without_result_runs_fingerprint_first_then_reports(self):
        gs = self._station()
        started = []
        def fake_run():
            gs._fingerprint_running = True
            started.append(1)
        gs._run_fingerprint = fake_run
        gs._start_report = mock.MagicMock()
        gs._set_fp_status = mock.MagicMock()
        with mock.patch.object(self.main.filedialog, "asksaveasfilename", return_value="/tmp/x.pdf"):
            gs._generate_report()
        self.assertEqual(started, [1])
        gs._start_report.assert_not_called()
        gs._handle_ai_message(("fingerprint", analyze_environment(three_regime_records())))
        gs._start_report.assert_called_once()
        self.assertIsNone(gs._pending_report)

    def test_report_cancelled_if_fingerprint_cannot_start(self):
        gs = self._station()
        gs._run_fingerprint = mock.MagicMock()          # leaves _fingerprint_running False (e.g. bad interval)
        with mock.patch.object(self.main.filedialog, "asksaveasfilename", return_value="/tmp/x.pdf"):
            gs._generate_report()
        self.assertIsNone(gs._pending_report)

    def test_report_worker_success_and_failure_messages(self):
        from ai_report import ReportInputs
        q = queue.Queue()
        inputs = ReportInputs(analyze_environment(three_regime_records()), AnomalySnapshot(alerts=[], counts={}, config={}))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "r.pdf"
            self.main.GroundStation._report_worker(q, str(target), inputs)
            self.assertEqual(q.get_nowait(), ("report_ok", str(target)))
            self.assertTrue(target.exists())                                   # success only reported after the file exists
            self.main.GroundStation._report_worker(q, str(Path(tmp) / "missing" / "r.pdf"), inputs)
        kind, message = q.get_nowait()
        self.assertEqual(kind, "report_failed")
        self.assertIn("Destination folder does not exist", message)

    def test_report_messages_show_dialogs_and_restore_button(self):
        gs = self._station()
        gs._report_running = True
        with mock.patch.object(self.main.messagebox, "showinfo") as info, mock.patch.object(self.main.messagebox, "showerror") as err:
            gs._handle_ai_message(("report_ok", "/tmp/r.pdf"))
            self.assertIn("/tmp/r.pdf", info.call_args.args[1])
            gs._report_running = True
            gs._handle_ai_message(("report_failed", "boom"))
            self.assertIn("boom", err.call_args.args[1])
        self.assertFalse(gs._report_running)
        gs.report_button.configure.assert_called_with(state="normal", text="Generate AI Mission Report (PDF)")

    # ---- telemetry keeps working
    def test_failures_do_not_stop_telemetry_or_send_commands(self):
        gs = self._station()
        with mock.patch.object(self.main.messagebox, "showerror"):
            gs._report_running = True
            gs._handle_ai_message(("report_failed", "boom"))
        gs._handle_ai_message(("fingerprint", failed_result("x")))
        gs._poll_ai_queue()
        gs.ai_queue.put(("mystery",))
        gs.ai_queue.put(("fingerprint",))                      # malformed message must not raise out of the poll loop
        gs._poll_ai_queue()
        gs.inbox.put(("frame", __import__("tests._synthetic", fromlist=["make_frame"]).make_frame(1)))
        gs._drain_queues()
        gs.db.submit.assert_called_once()
        self.assertEqual(gs.serial_worker.mock_calls, [])
        self.assertTrue(gs.after.called)                       # poll loop re-armed itself

    def test_idle_status_waits_for_telemetry(self):
        gs = self._station()
        gs._set_fp_status = mock.MagicMock()
        gs.fp_status_var.get.return_value = ""
        gs.packet_count = 3
        gs._update_fingerprint_idle_status()
        self.assertEqual(gs._set_fp_status.call_args.args[0], "Waiting for sufficient telemetry")
        gs.packet_count = 50
        gs._update_fingerprint_idle_status()
        self.assertEqual(gs._set_fp_status.call_args.args[0], "Ready: press Run / Refresh")


if __name__ == "__main__":
    unittest.main(verbosity=2)