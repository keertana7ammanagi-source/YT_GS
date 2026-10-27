"""Flight Log numbering restarts from 1 on Start Telemetry (display only).  Tk is stubbed; SYNTHETIC data only."""
import unittest
from unittest import mock

from tests.test_pipeline_regression import _import_main_with_stubs


def db_row(i):
    return {"id": i, "received_utc": "2026-01-01T00:00:00Z", "PACKET_COUNT": str(i), "FLIGHT_SOFTWARE_STATE": "2",
            "ALTITUDE": "100", "REL_ALTITUDE": "0", "GNSS_LATITUDE": "12.9", "GNSS_LONGITUDE": "77.6", "VOLTAGE": "7.4", "POWER": "1.5"}


class FlightLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_with_stubs()

    def _station(self, last_id=41):
        gs = object.__new__(self.main.GroundStation)
        gs.logs = []
        gs._log = gs.logs.append
        gs.db = mock.MagicMock()
        gs.db.last_id.return_value = last_id
        gs.table = mock.MagicMock()
        gs.after = mock.MagicMock()
        gs.flight_log_start_id = 0
        gs.serial_worker, gs.simulation_worker = None, None
        return gs

    def shown_ids(self, gs):
        lines = [c.args[1] for c in gs.table.insert.call_args_list]
        return [int(line.split(" | ")[0]) for line in lines[1:]]            # lines[0] is the header

    def test_table_numbers_rows_from_one_after_start_marker(self):
        gs = self._station()
        gs.flight_log_start_id = 6
        gs.db.recent.return_value = [db_row(i) for i in range(3, 10)]       # database ids 3..9
        gs._refresh_table()
        self.assertEqual(self.shown_ids(gs), [1, 2, 3])                     # only rows after id 6, renumbered from 1
        self.assertTrue(gs.table.insert.call_args_list[0].args[1].startswith("ID | UTC"))

    def test_default_shows_original_database_ids(self):
        gs = self._station()
        gs.db.recent.return_value = [db_row(i) for i in (101, 102)]
        gs._refresh_table()
        self.assertEqual(self.shown_ids(gs), [101, 102])

    def test_start_telemetry_over_serial_restarts_numbering_and_clears_table(self):
        gs = self._station(last_id=41)
        gs.serial_worker = mock.MagicMock()
        gs._dispatch("CMD,START_TM")
        gs.serial_worker.send.assert_called_once_with("CMD,START_TM")
        self.assertEqual(gs.flight_log_start_id, 41)
        gs.table.delete.assert_called_once()
        self.assertTrue(any("restarted from ID 1" in line for line in gs.logs))

    def test_start_telemetry_in_deliberate_simulation_restarts_numbering(self):
        gs = self._station(last_id=7)
        gs.simulation_worker = mock.MagicMock()
        gs._dispatch("CMD,START_TM")
        self.assertEqual(gs.flight_log_start_id, 7)

    def test_other_commands_do_not_restart_numbering(self):
        gs = self._station()
        gs.serial_worker = mock.MagicMock()
        for command in ("CMD,STOP_TM", "CMD,CALIB_1", "CMD,CALIB_2", "CMD,WRITE_EEPROM"):
            gs._dispatch(command)
        self.assertEqual(gs.flight_log_start_id, 0)
        gs.table.delete.assert_not_called()

    def test_refused_or_failed_start_does_not_restart_numbering(self):
        gs = self._station()                                                 # no link at all
        with mock.patch.object(self.main.messagebox, "showwarning"):
            gs._dispatch("CMD,START_TM")
        self.assertEqual(gs.flight_log_start_id, 0)
        gs.serial_worker = mock.MagicMock()
        gs.serial_worker.send.side_effect = OSError("port closed")
        gs._dispatch("CMD,START_TM")
        self.assertEqual(gs.flight_log_start_id, 0)

    def test_nothing_is_deleted_from_the_database(self):
        gs = self._station()
        gs.serial_worker = mock.MagicMock()
        gs._dispatch("CMD,START_TM")
        self.assertEqual({c[0] for c in gs.db.method_calls}, {"last_id"})   # read-only: no delete/reset call

    def test_marker_read_failure_keeps_old_numbering(self):
        gs = self._station()
        gs.flight_log_start_id = 12
        gs.db.last_id.side_effect = RuntimeError("db busy")
        gs.serial_worker = mock.MagicMock()
        gs._dispatch("CMD,START_TM")
        self.assertEqual(gs.flight_log_start_id, 12)
        gs.serial_worker.send.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)