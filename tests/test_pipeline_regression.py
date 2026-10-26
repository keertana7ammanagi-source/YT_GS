"""Regression tests: existing parser, database/CSV export, command path, and the main.py hook.
SYNTHETIC data only.  main.py is imported with stubbed tkinter/customtkinter, so the real GUI,
serial port and live UI updates are NOT exercised here (manual check required)."""
import csv
import gc
import py_compile
import queue
import re
import sys
import tempfile
import types
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from tests._synthetic import ROOT, make_frame, make_record

from anomaly_detection import AnomalyDetector
from database_handling import TelemetryDatabase
from station_control import TELEMETRY_FIELDS, parse_frame


class ParserRegression(unittest.TestCase):
    def test_schema_is_37_unique_fields(self):
        self.assertEqual((len(TELEMETRY_FIELDS), len(set(TELEMETRY_FIELDS))), (37, 37))
        self.assertEqual((TELEMETRY_FIELDS[0], TELEMETRY_FIELDS[27], TELEMETRY_FIELDS[-1]),
                         ("TEAM_ID", "FLIGHT_SOFTWARE_STATE", "POWER"))

    def test_valid_frame_parses(self):
        r = parse_frame(make_frame(7, state=3))
        self.assertEqual((r["PACKET_COUNT"], r["FLIGHT_SOFTWARE_STATE"]), (7, 3))
        self.assertEqual(list(r)[:2], ["TEAM_ID", "TIMESTAMP"])

    def test_36_and_38_fields_rejected(self):
        line = make_frame(1)
        for bad in (",".join(line.split(",")[:-1]), line + ",1"):
            with self.assertRaises(ValueError):
                parse_frame(bad)


class DatabaseRegression(unittest.TestCase):
    def test_submit_recent_and_csv_export_alignment(self):
        # Windows keeps the .sqlite3 file locked while any connection is open.  The existing database
        # code uses `with sqlite3.connect(...)` (commits, does not close), so force a GC before cleanup.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.addCleanup(gc.collect)
            db = TelemetryDatabase(Path(tmp) / "t.sqlite3")
            for i in (1, 2, 3):
                db.submit(make_record(i))
            db.close()
            db = TelemetryDatabase(Path(tmp) / "t.sqlite3")
            rows = db.recent(10)
            self.assertEqual(len(rows), 3)
            out = Path(tmp) / "out.csv"
            db.export_csv(out)
            db.close()
            with out.open(newline="", encoding="utf-8-sig") as fh:  # existing export writes a BOM
                table = list(csv.reader(fh))
            self.assertEqual(table[0], ["received_utc", *TELEMETRY_FIELDS])
            self.assertEqual(len(table), 4)
            self.assertTrue(all(len(row) == 38 for row in table))
            self.assertEqual(table[1][1 + TELEMETRY_FIELDS.index("TEAM_ID")], "1234")
            del db, rows
            gc.collect()


def _import_main_with_stubs():
    class _Base:
        def __init__(self, *a, **k): pass
    tk = types.ModuleType("tkinter")
    tk.StringVar = type("StringVar", (), {"__init__": lambda s, value="": setattr(s, "v", value),
                                          "set": lambda s, v: setattr(s, "v", v), "get": lambda s: s.v})
    tk.filedialog, tk.messagebox = mock.MagicMock(), mock.MagicMock()
    for name in ("Canvas", "Tk", "Toplevel", "Frame", "Label", "Text", "Scrollbar", "BooleanVar", "IntVar"):
        setattr(tk, name, _Base)
    ctk = mock.MagicMock()
    ctk.CTk = _Base
    stubs = {"tkinter": tk, "tkinter.filedialog": tk.filedialog, "tkinter.messagebox": tk.messagebox,
             "customtkinter": ctk, "tkintermapview": mock.MagicMock(), "serial": mock.MagicMock(),
             "serial.tools": mock.MagicMock(), "serial.tools.list_ports": mock.MagicMock()}
    sys.modules.pop("main", None)
    with mock.patch.dict(sys.modules, stubs):
        import main
    return main


class MainHookRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_with_stubs()

    def _station(self):
        m = self.main
        gs = object.__new__(m.GroundStation)
        gs.logs = []
        gs._log = gs.logs.append
        gs.db, gs.broadcaster, gs.flight_map = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        gs.serial_worker = mock.MagicMock()
        gs.inbox, gs.log_queue = queue.Queue(), queue.Queue()
        gs.after = mock.MagicMock()
        gs.anomaly_detector = AnomalyDetector()
        gs.graph_data = mock.MagicMock()
        gs._anomaly_error_logged = False
        for name in ("anomaly_status_var", "anomaly_status_label", "anomaly_counts_var", "anomaly_box"):
            setattr(gs, name, mock.MagicMock())
        gs._update_cards = mock.MagicMock()
        for name, default in (("packet_count", 0), ("lost_packets", 0), ("last_packet", None),
                              ("last_team_id", None), ("previous_altitude", None),
                              ("altitude_baseline", None), ("flight_started_at", None)):
            setattr(gs, name, default)
        gs.speed_samples = deque(maxlen=3)
        gs.landing_coordinates = None            # attributes used by the newer _process_record
        gs.landing_var = mock.MagicMock()
        gs._update_navigation = mock.MagicMock()
        return gs

    def test_main_compiles(self):
        py_compile.compile(str(ROOT / "main.py"), doraise=True)

    def test_existing_flow_unchanged_and_detector_sees_raw_values(self):
        gs = self._station()
        seen = []
        real = gs.anomaly_detector.evaluate
        gs.anomaly_detector.evaluate = lambda rec: (seen.append(dict(rec)), real(rec))[1]
        gs._process_record(make_record(1, CURRENT=1500))
        gs.db.submit.assert_called_once()
        gs.broadcaster.publish.assert_called_once()
        gs._update_cards.assert_called_once()
        self.assertEqual(gs.db.submit.call_args[0][0]["CURRENT"], 1.5)   # existing mA->A rescale intact
        self.assertEqual(seen[0]["CURRENT"], 1500.0)                       # detector saw the wire value
        self.assertEqual(gs.anomaly_detector.samples_evaluated, 1)

    def test_graph_feed_receives_final_record_and_cannot_break_telemetry(self):
        gs = self._station()
        gs._process_record(make_record(1, VOLTAGE=7.0, CURRENT=2.0))
        fed = gs.graph_data.add_record.call_args[0][0]
        self.assertAlmostEqual(fed["POWER"], 14.0)               # station's final (ground-computed) value
        gs.graph_data.add_record.side_effect = RuntimeError("boom")
        gs._process_record(make_record(2))
        self.assertEqual(gs.db.submit.call_count, 2)
        self.assertEqual(gs._update_cards.call_count, 2)

    def test_detector_failure_cannot_break_telemetry(self):
        gs = self._station()
        gs.anomaly_detector.evaluate = mock.MagicMock(side_effect=RuntimeError("boom"))
        gs._process_record(make_record(1))
        gs._process_record(make_record(2))
        self.assertEqual(gs.db.submit.call_count, 2)
        self.assertEqual(gs._update_cards.call_count, 2)
        self.assertEqual(sum("Anomaly detector error" in line for line in gs.logs), 1)  # logged once

    def test_rejected_frame_logged_as_before_and_counted(self):
        gs = self._station()
        gs.inbox.put(("frame", "1,2,3"))
        gs._drain_queues()
        self.assertTrue(any(line.startswith("FRAME ERROR") for line in gs.logs))
        self.assertEqual(gs.anomaly_detector.rejected_frames, 1)
        gs.db.submit.assert_not_called()

    def test_detector_path_never_transmits(self):
        gs = self._station()
        for i in range(1, 30):
            gs._process_record(make_record(i, TEMP=90 if i == 25 else 25))
        self.assertEqual(gs.serial_worker.mock_calls, [])

    def test_only_dispatch_sends_commands(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"serial_worker\.send\(", src)), 1)
        det = (ROOT / "anomaly_detection.py").read_text(encoding="utf-8")
        self.assertNotIn(".send(", det)
        self.assertNotIn(".write(", det)


if __name__ == "__main__":
    unittest.main(verbosity=2)
