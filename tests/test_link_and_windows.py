"""Regression tests: no implicit Simulation Mode when there is no link, and a single sensor-graph window.
Tk/customtkinter are stubbed (see test_pipeline_regression); SYNTHETIC data only."""
import re
import unittest
from pathlib import Path
from unittest import mock

from tests._synthetic import ROOT
from tests.test_pipeline_regression import _import_main_with_stubs


class LinkAndWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_with_stubs()

    def _station(self):
        gs = object.__new__(self.main.GroundStation)
        gs.logs = []
        gs._log = gs.logs.append
        gs.serial_worker = None
        gs.simulation_worker = None
        gs.graph_process = None
        gs.simulation_seen = False
        gs.sim_switch = mock.MagicMock()
        gs.connect_button = mock.MagicMock()
        gs.port_menu, gs.baud_menu = mock.MagicMock(), mock.MagicMock()
        gs.port_menu.get.return_value, gs.baud_menu.get.return_value = "COM3", "115200"
        gs.inbox, gs.log_queue = mock.MagicMock(), mock.MagicMock()
        return gs

    def test_command_without_link_shows_message_and_does_not_start_simulation(self):
        gs = self._station()
        gs._start_simulation = mock.MagicMock()
        with mock.patch.object(self.main.messagebox, "showwarning") as warn:
            gs._dispatch("CMD,START_TM")
        warn.assert_called_once()
        self.assertEqual(warn.call_args.args[0], "No link")
        self.assertIn("no link", warn.call_args.args[1].lower())
        gs._start_simulation.assert_not_called()
        gs.sim_switch.select.assert_not_called()
        self.assertIsNone(gs.simulation_worker)
        self.assertFalse(gs.simulation_seen)
        self.assertTrue(any(line.startswith("NO LINK") for line in gs.logs))

    def test_every_command_without_link_is_refused(self):
        gs = self._station()
        with mock.patch.object(self.main.messagebox, "showwarning") as warn:
            for command in ("CMD,START_TM", "CMD,CALIB_1", "CMD,CALIB_2", "CMD,SAVE_EEPROM"):
                gs._dispatch(command)
        self.assertEqual(warn.call_count, 4)
        self.assertIsNone(gs.simulation_worker)

    def test_command_with_serial_link_still_sent_normally(self):
        gs = self._station()
        gs.serial_worker = mock.MagicMock()
        with mock.patch.object(self.main.messagebox, "showwarning") as warn:
            gs._dispatch("CMD,START_TM")
        gs.serial_worker.send.assert_called_once_with("CMD,START_TM")
        warn.assert_not_called()

    def test_command_with_deliberate_simulation_still_goes_to_simulator(self):
        gs = self._station()
        gs.simulation_worker = mock.MagicMock()
        with mock.patch.object(self.main.messagebox, "showwarning") as warn:
            gs._dispatch("CMD,START_TM")
        gs.simulation_worker.command.assert_called_once_with("CMD,START_TM")
        warn.assert_not_called()

    def test_connecting_does_not_launch_a_second_graph_window(self):
        gs = self._station()
        with mock.patch.object(self.main, "SerialWorker") as worker, mock.patch.object(self.main.subprocess, "Popen") as popen:
            gs._toggle_connection()
        worker.return_value.start.assert_called_once()           # the link itself still comes up
        popen.assert_not_called()                                  # no deploy_graph.py window
        self.assertIsNone(gs.graph_process)

    def test_no_code_path_launches_the_second_window_automatically(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"self\._launch_graph\(\)", src)), 0)
        self.assertEqual(len(re.findall(r"subprocess\.Popen\(", src)), 1)   # only inside the (unused) _launch_graph helper
        self.assertIn("self._open_graph_window()", src)                      # the six-graph window still opens at start-up


if __name__ == "__main__":
    unittest.main(verbosity=2)