"""Recovery tests: compass/turn guidance, averaged landing lock, NMEA GPS input, fast simulation, main.py hooks.
All coordinates and NMEA sentences are SYNTHETIC test data."""
import math
import queue
import sys
import time
import types
import unittest
from collections import deque
from unittest import mock

from tests._synthetic import make_record
from tests.test_pipeline_regression import _import_main_with_stubs

from gps_receiver import (GpsReceiverWorker, NmeaFixTracker, nmea_checksum_ok, parse_nmea)
from offline_navigation import (LandingEstimator, calculate_navigation_instructions, compass_direction, format_recovery_text,
                                haversine_m, turn_text)
from station_control import TELEMETRY_FIELDS, parse_frame

LAT, LON = 12.9716, 77.5946


def nmea(body: str) -> str:
    """Wrap a sentence body in $...*HH with a correct XOR checksum."""
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


GGA = nmea("GPGGA,123519,1258.296,N,07735.676,E,1,08,0.9,920.0,M,-34.0,M,,")
RMC_MOVING = nmea("GPRMC,123519,A,1258.296,N,07735.676,E,3.0,84.4,230394,,,A")
RMC_STILL = nmea("GPRMC,123519,A,1258.296,N,07735.676,E,0.2,84.4,230394,,,A")


class Guidance(unittest.TestCase):
    def test_compass_points(self):
        names = {0: "north", 44: "north-east", 90: "east", 135: "south-east", 180: "south", 225: "south-west",
                 270: "west", 315: "north-west", 359.9: "north", 22.4: "north", 22.6: "north-east", 337.4: "north-west", 337.6: "north"}
        for bearing, name in names.items():
            self.assertEqual(compass_direction(bearing)[0], name, bearing)
        self.assertEqual(compass_direction(45), ("north-east", "NE"))

    def test_turn_text(self):
        self.assertEqual(turn_text(0, 50), "Walk straight ahead")
        self.assertEqual(turn_text(-15, 50), "Walk straight ahead")
        self.assertEqual(turn_text(30, 50), "Turn slightly right (30°), then walk")
        self.assertEqual(turn_text(-100, 50), "Turn left 100°, then walk")
        self.assertIn("Turn around", turn_text(170, 50))
        self.assertEqual(turn_text(90, 0.4), "You have reached the CanSat")

    def test_existing_result_keys_unchanged_and_new_ones_added(self):
        r = calculate_navigation_instructions(LAT, LON, 90, LAT + 0.001, LON)        # target 111 m due north, facing east
        self.assertAlmostEqual(r["distance"], 111.2, delta=0.2)
        self.assertEqual((r["bearing"], r["instruction"]), (0.0, "Turn Left by 90°"))
        self.assertEqual((r["compass"], r["compass_abbr"], r["heading"], r["relative_turn"]), ("north", "N", 90.0, -90.0))
        self.assertEqual(r["distance_text"], "111.2 m")
        self.assertEqual(r["turn_text"], "Turn left 90°, then walk")

    def test_text_shown_on_screen(self):
        text = format_recovery_text(calculate_navigation_instructions(LAT, LON, 90, LAT + 0.0004, LON + 0.0004))
        # equal steps in latitude and longitude are not equal distances (longitude degrees shrink away from the equator),
        # so the true bearing is 43.8 degrees, displayed as 44
        for part in ("north-east (NE)", "Distance:", "Bearing: 44°", "You are facing 90° (E)", "Turn left"):
            self.assertIn(part, text)
        self.assertIn("km", format_recovery_text(calculate_navigation_instructions(LAT, LON, 0, LAT + 0.02, LON)))

    def test_invalid_inputs_still_rejected(self):
        for bad in ((None, LON, 0, LAT, LON), ("x", LON, 0, LAT, LON), (95, LON, 0, LAT, LON), (LAT, LON, float("nan"), LAT, LON)):
            with self.assertRaises((ValueError, TypeError)):
                calculate_navigation_instructions(*bad)


class Landing(unittest.TestCase):
    def feed(self, est, n, lat=LAT, lon=LON, fix=2, hdop=0.9):
        return [est.add_fix(fix, lat + 1e-6 * i, lon, hdop) for i in range(n)]

    def test_waits_for_several_fixes_then_locks_the_average(self):
        est = LandingEstimator()
        results = [est.add_fix(2, LAT + d, LON - d, 0.9) for d in (-2e-5, 1e-5, 0, 2e-5, -1e-5)]
        self.assertFalse(any(r.locked for r in results[:4]))
        self.assertEqual([r.valid_fixes for r in results], [1, 2, 3, 4, 5])
        last = results[-1]
        self.assertTrue(last.locked and not last.low_confidence)
        self.assertAlmostEqual(last.coordinates[0], LAT, places=6)
        self.assertAlmostEqual(last.coordinates[1], LON, places=6)
        self.assertIn("average of 5 valid GNSS fixes", last.message)
        self.assertLess(last.spread_m, 5)

    def test_invalid_fixes_are_skipped_and_explained(self):
        est = LandingEstimator()
        bad = [(0, LAT, LON, 0.9), (2, float("nan"), LON, 0.9), (2, 95.0, LON, 0.9), (2, 0.0, 0.0, 0.9), (2, LAT, LON, 9.0),
               (2, LAT, LON, 0.0), (2, LAT, LON, float("nan")), (2, LAT, LON, None), ("x", LAT, LON, 0.9), (2, None, LON, 0.9)]
        for args in bad:
            status = est.add_fix(*args)
            self.assertFalse(status.locked)
            self.assertEqual(status.valid_fixes, 0)
        self.assertIn("last rejected", status.message)
        self.assertEqual(self.feed(est, 5)[-1].locked, True)               # valid fixes afterwards still work

    def test_hdop_limit_is_configurable(self):
        strict = LandingEstimator(max_hdop=1.0)
        self.assertEqual(strict.add_fix(2, LAT, LON, 1.5).valid_fixes, 0)
        self.assertEqual(LandingEstimator(max_hdop=2.0).add_fix(2, LAT, LON, 1.5).valid_fixes, 1)

    def test_one_far_outlier_does_not_become_the_landing_point(self):
        est = LandingEstimator()
        est.add_fix(2, LAT + 0.005, LON, 0.9)                              # ~550 m away (e.g. a spurious early frame)
        results = self.feed(est, 8)
        locked = next(r for r in results if r.locked)
        self.assertAlmostEqual(locked.coordinates[0], LAT, places=5)       # the window slid past the outlier
        self.assertLess(haversine_m(*locked.coordinates, LAT, LON), 2)

    def test_unstable_position_locks_with_low_confidence_after_give_up(self):
        est = LandingEstimator(give_up_after=12)
        results = [est.add_fix(2, LAT + 0.0003 * i, LON, 0.9) for i in range(12)]      # keeps moving ~33 m per fix
        self.assertFalse(any(r.locked for r in results[:-1]))
        self.assertIn("still moving", results[6].message)
        self.assertTrue(results[-1].locked and results[-1].low_confidence)
        self.assertIn("LOW CONFIDENCE", results[-1].message)

    def test_lock_never_moves_until_reset(self):
        est = LandingEstimator()
        locked = self.feed(est, 5)[-1]
        later = est.add_fix(2, LAT + 1, LON + 1, 0.9)
        self.assertIs(later, locked)
        est.reset()
        self.assertFalse(est.locked)
        self.assertEqual(est.add_fix(2, LAT, LON, 0.9).valid_fixes, 1)

    def test_config_validation(self):
        for kwargs in (dict(required_fixes=1), dict(give_up_after=3), dict(max_hdop=0), dict(max_spread_m=-1)):
            with self.assertRaises(ValueError):
                LandingEstimator(**kwargs)


class Nmea(unittest.TestCase):
    def test_checksum(self):
        self.assertTrue(nmea_checksum_ok(GGA))
        self.assertFalse(nmea_checksum_ok(GGA.replace("0.9", "0.8")))
        self.assertFalse(nmea_checksum_ok(GGA.split("*")[0]))              # no checksum
        for junk in ("", "garbage", "$", "$*", "$GPGGA*ZZ", None.__class__.__name__):
            self.assertFalse(nmea_checksum_ok(junk))

    def test_gga_position_and_quality(self):
        g = parse_nmea(GGA)
        self.assertTrue(g["valid"])
        self.assertAlmostEqual(g["lat"], 12 + 58.296 / 60, places=6)
        self.assertAlmostEqual(g["lon"], 77 + 35.676 / 60, places=6)
        self.assertEqual((g["sats"], g["hdop"]), (8, 0.9))
        self.assertFalse(parse_nmea(nmea("GPGGA,123519,1258.296,N,07735.676,E,0,00,99.9,,M,,M,,"))["valid"])   # quality 0

    def test_southern_and_western_hemispheres(self):
        g = parse_nmea(nmea("GNGGA,000000,3351.000,S,15112.000,W,1,10,1.1,5.0,M,0.0,M,,"))
        self.assertAlmostEqual(g["lat"], -(33 + 51 / 60), places=6)
        self.assertAlmostEqual(g["lon"], -(151 + 12 / 60), places=6)

    def test_rmc_validity_and_unusable_input(self):
        self.assertTrue(parse_nmea(RMC_MOVING)["valid"])
        self.assertFalse(parse_nmea(nmea("GPRMC,123519,V,,,,,,,230394,,,N"))["valid"])
        for junk in ("$GPGLL,1,2*00", nmea("GPGGA,1"), nmea("GPGGA,1,9999.0,N,00000.0,E,1,5,1.0,0,M,0,M,,"), "\x00\xff", ""):
            result = parse_nmea(junk)
            self.assertTrue(result is None or not result["valid"], junk)

    def test_course_only_used_while_moving(self):
        tracker = NmeaFixTracker()
        tracker.update(parse_nmea(GGA))
        still = tracker.update(parse_nmea(RMC_STILL))
        self.assertIsNone(still.course_deg)
        moving = tracker.update(parse_nmea(RMC_MOVING))
        self.assertAlmostEqual(moving.course_deg, 84.4)
        self.assertEqual((moving.hdop, moving.satellites), (0.9, 8))
        self.assertIsNone(tracker.update(parse_nmea("junk")))
        self.assertIsNone(tracker.update(parse_nmea(nmea("GPRMC,1,V,,,,,,,,,,N"))))

    def test_worker_reads_only_and_posts_fixes(self):
        class FakeSerial:
            instances = []
            def __init__(self, **kwargs):
                self.kwargs, self.lines, self.closed, self.writes = kwargs, [GGA.encode() + b"\r\n", b"junk\r\n", RMC_MOVING.encode() + b"\r\n"], False, 0
                FakeSerial.instances.append(self)
            def readline(self):
                time.sleep(0.01)
                return self.lines.pop(0) if self.lines else b""
            def write(self, *a):
                self.writes += 1
                raise AssertionError("the GPS worker must never write to the receiver")
            def cancel_read(self): pass
            def close(self): self.closed = True
        out = queue.Queue()
        with mock.patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=FakeSerial)}):
            worker = GpsReceiverWorker("COM9", 9600, out)
            worker.start()
            messages = [out.get(timeout=3) for _ in range(3)]
            worker.stop()
        self.assertEqual(messages[0][0], "gps_status")
        self.assertEqual([m[0] for m in messages[1:]], ["gps_fix", "gps_fix"])
        self.assertAlmostEqual(messages[2][1].course_deg, 84.4)
        fake = FakeSerial.instances[0]
        self.assertEqual((fake.kwargs["port"], fake.kwargs["baudrate"], fake.writes, fake.closed), ("COM9", 9600, 0, True))

    def test_worker_reports_open_failure_instead_of_crashing(self):
        def broken(**kwargs):
            raise OSError("no such port")
        out = queue.Queue()
        with mock.patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=broken)}):
            worker = GpsReceiverWorker("COM9", 9600, out)
            worker.start()
            kind, text = out.get(timeout=3)
            worker.thread.join(2)
        self.assertEqual(kind, "gps_error")
        self.assertIn("no such port", text)


class RecoveryInMain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_with_stubs()

    def _station(self):
        gs = object.__new__(self.main.GroundStation)
        gs.logs = []
        gs._log = gs.logs.append
        gs.db, gs.broadcaster, gs.flight_map = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        gs.serial_worker, gs.simulation_worker, gs.gps_worker = None, None, None
        gs.gps_queue = queue.Queue()
        gs.gps_last_fix_time = None
        gs.after = mock.MagicMock()
        gs.landing_coordinates = None
        gs.landing_estimator = LandingEstimator()
        for name in ("landing_var", "recovery_var", "gps_var", "gps_button", "gps_port_menu", "gps_baud_menu", "fast_sim_switch",
                     "anomaly_status_var", "anomaly_status_label", "anomaly_counts_var", "anomaly_box"):
            setattr(gs, name, mock.MagicMock())
        gs.anomaly_detector = mock.MagicMock()
        gs.anomaly_detector.evaluate.return_value = []
        gs.graph_data = mock.MagicMock()
        gs._anomaly_error_logged = False
        gs._update_cards = mock.MagicMock()
        gs._update_navigation = mock.MagicMock()
        class Entry:
            def __init__(self): self.text = ""
            def get(self): return self.text
            def delete(self, a, b): self.text = ""
            def insert(self, i, t): self.text = t
        gs.user_lat_entry, gs.user_lon_entry, gs.heading_entry = Entry(), Entry(), Entry()
        for name, default in (("packet_count", 0), ("lost_packets", 0), ("last_packet", None), ("previous_altitude", None),
                              ("altitude_baseline", None), ("flight_started_at", None), ("last_team_id", None)):
            setattr(gs, name, default)
        gs.speed_samples = deque(maxlen=3)
        return gs

    def impact_record(self, i, **kw):
        return make_record(i, state=7, GNSS_LATITUDE=LAT + 1e-5 * math.sin(i), GNSS_LONGITUDE=LON, GNSS_HDOP=0.9, GNSS_FIX=2, **kw)

    def test_landing_locks_only_after_several_impact_frames(self):
        gs = self._station()
        for i in range(1, 5):
            gs._process_record(self.impact_record(i))
            self.assertIsNone(gs.landing_coordinates, f"locked too early after {i} frame(s)")
        self.assertIn("4/5", gs.landing_var.set.call_args.args[0])
        gs._process_record(self.impact_record(5))
        self.assertIsNotNone(gs.landing_coordinates)
        self.assertAlmostEqual(gs.landing_coordinates[0], LAT, places=4)
        gs._update_navigation.assert_called()
        self.assertEqual(gs.db.submit.call_count, 5)                        # every frame still stored and published
        self.assertEqual(gs.broadcaster.publish.call_count, 5)

    def test_frames_without_fix_do_not_lock(self):
        gs = self._station()
        for i in range(1, 8):
            gs._process_record(make_record(i, state=7, GNSS_FIX=0))
        self.assertIsNone(gs.landing_coordinates)
        self.assertIn("0/5", gs.landing_var.set.call_args.args[0])
        self.assertEqual(gs.db.submit.call_count, 7)

    def test_before_impact_the_estimator_is_not_used(self):
        gs = self._station()
        gs.landing_estimator = mock.MagicMock()
        for i in range(1, 6):
            gs._process_record(make_record(i, state=3))
        gs.landing_estimator.add_fix.assert_not_called()

    def test_estimator_failure_never_stops_telemetry_storage(self):
        gs = self._station()
        gs.landing_estimator = mock.MagicMock()
        gs.landing_estimator.add_fix.side_effect = RuntimeError("boom")
        gs._process_record(self.impact_record(1))
        gs.db.submit.assert_called_once()
        self.assertTrue(any("Landing estimator error" in line for line in gs.logs))

    def test_reset_landing_lock(self):
        gs = self._station()
        for i in range(1, 6):
            gs._process_record(self.impact_record(i))
        self.assertIsNotNone(gs.landing_coordinates)
        gs._reset_landing_lock()
        self.assertIsNone(gs.landing_coordinates)
        self.assertFalse(gs.landing_estimator.locked)

    def test_starting_a_new_simulation_clears_an_old_lock(self):
        gs = self._station()
        gs.landing_coordinates = (1.0, 2.0)
        gs.sim_switch, gs.connect_button, gs.inbox = mock.MagicMock(), mock.MagicMock(), queue.Queue()
        gs.fast_sim_switch.get.return_value = 1
        with mock.patch.object(self.main, "SimulationWorker") as worker:
            gs._start_simulation()
        self.assertIsNone(gs.landing_coordinates)
        self.assertEqual(worker.call_args.args[1], self.main.FAST_IMPACT_PACKETS)

    def test_gps_fix_fills_position_and_only_moving_course_fills_heading(self):
        gs = self._station()
        gs.heading_entry.text = "123"
        tracker = NmeaFixTracker()
        tracker.update(parse_nmea(GGA))
        gs._apply_gps_fix(tracker.update(parse_nmea(RMC_STILL)))
        self.assertEqual((gs.user_lat_entry.text, gs.heading_entry.text), (f"{12 + 58.296 / 60:.6f}", "123"))   # manual heading kept
        gs._apply_gps_fix(tracker.update(parse_nmea(RMC_MOVING)))
        self.assertEqual(gs.heading_entry.text, "84")
        self.assertIn("GPS course", gs.gps_var.set.call_args.args[0])
        gs._update_navigation.assert_not_called()                           # nothing locked yet
        gs.landing_coordinates = (LAT, LON)
        gs._apply_gps_fix(tracker.update(parse_nmea(RMC_MOVING)))
        gs._update_navigation.assert_called_once()

    def test_gps_cannot_use_the_telemetry_port(self):
        gs = self._station()
        gs.serial_worker = mock.MagicMock()
        gs.serial_worker.settings.port = "COM3"
        gs.gps_port_menu.get.return_value = "COM3"
        with mock.patch.object(self.main.messagebox, "showerror") as err, mock.patch.object(self.main, "GpsReceiverWorker") as worker:
            gs._toggle_gps()
        err.assert_called_once()
        worker.assert_not_called()
        self.assertIsNone(gs.gps_worker)

    def test_gps_connect_disconnect_and_error_handling(self):
        gs = self._station()
        gs.gps_port_menu.get.return_value, gs.gps_baud_menu.get.return_value = "COM9", "9600"
        with mock.patch.object(self.main, "GpsReceiverWorker") as worker:
            gs._toggle_gps()
            worker.assert_called_once_with("COM9", 9600, gs.gps_queue)
            worker.return_value.start.assert_called_once()
            self.assertIs(gs.gps_worker, worker.return_value)
            gs.gps_queue.put(("gps_error", "GPS receiver stopped: unplugged"))
            gs._poll_gps_queue()
            worker.return_value.stop.assert_called_once()
        self.assertIsNone(gs.gps_worker)
        self.assertIn("unplugged", gs.gps_var.set.call_args.args[0])
        self.assertTrue(gs.after.called)

    def test_stale_gps_is_flagged(self):
        gs = self._station()
        gs.gps_worker = mock.MagicMock()
        gs.gps_last_fix_time = time.monotonic() - 30
        gs._poll_gps_queue()
        self.assertIn("out of date", gs.gps_var.set.call_args.args[0])


class FastSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_with_stubs()

    def frames(self, worker, n):
        return [parse_frame(worker._build_frame(p)) for p in range(1, n + 1)]

    def test_normal_timeline_is_unchanged(self):
        worker = self.main.SimulationWorker(queue.Queue())
        states = [f["FLIGHT_SOFTWARE_STATE"] for f in self.frames(worker, 61)]
        self.assertEqual((states[0], states[8], states[9], states[58], states[59], states[60]), (2, 2, 3, 3, 7, 7))

    def test_fast_mode_reaches_impact_in_about_twelve_packets(self):
        worker = self.main.SimulationWorker(queue.Queue())
        worker.set_fast_impact(True)
        states = [f["FLIGHT_SOFTWARE_STATE"] for f in self.frames(worker, 14)]
        self.assertEqual(states.index(7) + 1, 12)
        self.assertIn(3, states)                                           # still passes through a descent state
        worker.set_fast_impact(False)
        self.assertEqual(worker.impact_after, 60)

    def test_post_impact_frames_have_small_gnss_noise_and_stay_valid(self):
        worker = self.main.SimulationWorker(queue.Queue(), impact_after=12)
        frames = self.frames(worker, 20)
        for f in frames:
            self.assertEqual(f["GNSS_FIX"], 2)
        before = {(f["GNSS_LATITUDE"], f["GNSS_LONGITUDE"]) for f in frames if f["FLIGHT_SOFTWARE_STATE"] != 7}
        after = [(f["GNSS_LATITUDE"], f["GNSS_LONGITUDE"]) for f in frames if f["FLIGHT_SOFTWARE_STATE"] == 7]
        self.assertEqual(before, {(12.9716, 77.5946)})                     # unchanged before impact
        self.assertGreater(len(set(after)), 3)
        self.assertLess(max(haversine_m(12.9716, 77.5946, a, b) for a, b in after), 3)

    def test_simulated_impact_produces_a_lock(self):
        worker = self.main.SimulationWorker(queue.Queue(), impact_after=12)
        est = LandingEstimator()
        status = None
        for p in range(1, 30):
            f = parse_frame(worker._build_frame(p))
            if f["FLIGHT_SOFTWARE_STATE"] == 7:
                status = est.add_fix(f["GNSS_FIX"], f["GNSS_LATITUDE"], f["GNSS_LONGITUDE"], f["GNSS_HDOP"])
                if status.locked:
                    locked_at = p
                    break
        self.assertTrue(status.locked)
        self.assertEqual(locked_at, 16)                                    # 12 + 4 more fixes -> about 16 s in fast mode
        self.assertLess(haversine_m(*status.coordinates, 12.9716, 77.5946), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)