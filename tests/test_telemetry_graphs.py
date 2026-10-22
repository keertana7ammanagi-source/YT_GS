"""Tests for telemetry_graphs.  SYNTHETIC data only."""
import unittest

from tests._synthetic import make_record

from telemetry_graphs import GRAPHS, GraphData


class GraphDataTests(unittest.TestCase):
    def test_exactly_the_six_requested_graphs(self):
        self.assertEqual([f for f, _ in GRAPHS], ["PRESSURE", "HUMIDITY", "TEMP", "POWER", "ALTITUDE", "VOLTAGE"])
        self.assertEqual(set(GraphData().series), {f for f, _ in GRAPHS})

    def test_light_theme_and_distinct_colours(self):
        from telemetry_graphs import BACKGROUND, FIELD_COLORS, PANEL
        self.assertEqual(set(FIELD_COLORS), {f for f, _ in GRAPHS})
        self.assertEqual(len(set(FIELD_COLORS.values())), 6)          # every graph has its own colour
        for colour in (BACKGROUND, PANEL):                            # light backgrounds
            r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
            self.assertGreater(0.299 * r + 0.587 * g + 0.114 * b, 200)

    def test_valid_record_adds_one_point_per_field(self):
        data = GraphData()
        self.assertTrue(data.add_record(make_record(5, TEMP=21.5, VOLTAGE=7.3)))
        for field, _ in GRAPHS:
            self.assertEqual(len(data.series[field]), 1)
            self.assertEqual(data.series[field][0][0], 5.0)       # x axis is TIMESTAMP
        self.assertEqual(data.series["TEMP"][0][1], 21.5)
        self.assertEqual(data.series["VOLTAGE"][0][1], 7.3)

    def test_invalid_values_are_skipped_not_plotted(self):
        data = GraphData()
        r = make_record(1)
        r["TEMP"], r["HUMIDITY"], r["PRESSURE"], r["POWER"] = float("nan"), float("inf"), "abc", True
        del r["VOLTAGE"]
        data.add_record(r)
        self.assertEqual([len(data.series[f]) for f in ("TEMP", "HUMIDITY", "PRESSURE", "POWER", "VOLTAGE")], [0] * 5)
        self.assertEqual(len(data.series["ALTITUDE"]), 1)         # valid fields still plotted

    def test_bad_timestamp_adds_nothing(self):
        data = GraphData()
        r = make_record(1)
        r["TIMESTAMP"] = float("nan")
        self.assertFalse(data.add_record(r))
        self.assertEqual(data.revision, 0)

    def test_history_is_bounded(self):
        data = GraphData(history=50)
        for i in range(1, 200):
            data.add_record(make_record(i))
        self.assertEqual(len(data.series["TEMP"]), 50)
        self.assertEqual(data.series["TEMP"][-1][0], 199.0)

    def test_record_not_modified(self):
        r = make_record(1)
        before = dict(r)
        GraphData().add_record(r)
        self.assertEqual(r, before)


class WindowTests(unittest.TestCase):
    def test_window_draws_six_lines_with_data(self):
        try:
            import tkinter as tk
            import matplotlib  # noqa: F401
            root = tk.Tk()
        except Exception as exc:  # no tkinter / no display (CI): the data tests above still run
            self.skipTest(f"GUI not available: {exc}")
        try:
            from telemetry_graphs import TelemetryGraphWindow
            data = GraphData()
            window = TelemetryGraphWindow(root, data)
            for i in range(1, 21):
                data.add_record(make_record(i, TEMP=20 + i))
            window._refresh()
            self.assertEqual(len(window.lines), 6)
            self.assertEqual(len(window.lines["TEMP"].get_xdata()), 20)
            self.assertEqual(window.lines["TEMP"].get_ydata()[-1], 40.0)
            window.close()
            window._refresh()                                    # closed window: must be a safe no-op
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
