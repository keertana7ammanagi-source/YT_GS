"""CanSat Ground Station: asynchronous serial, persistence, map and graph bridge."""
from __future__ import annotations

import json
import math
import queue
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from anomaly_detection import STATUS_ANOMALY, STATUS_INSUFFICIENT, STATUS_INVALID, STATUS_MONITORING, AnomalyDetector, DetectorConfig, load_config
from ai_report import AnomalySnapshot, ReportInputs, generate_report, snapshot_anomaly_detector
from database_handling import TelemetryDatabase
from environmental_fingerprinting import REQUIRED_COLUMNS, FingerprintResult, analyze_environment, failed_result, format_fingerprint_text
from environmental_fingerprinting import STATUS_COMPLETE as FP_COMPLETE, STATUS_INSUFFICIENT as FP_INSUFFICIENT  # not the anomaly STATUS_* names
from setup_map import FlightMap
from station_control import STATE_NAMES, SerialSettings, TELEMETRY_FIELDS, available_ports, parse_frame, timestamp_to_seconds
from telemetry_graphs import GraphData, TelemetryGraphWindow
from gps_receiver import GpsReceiverWorker
from offline_navigation import LandingEstimator, calculate_navigation_instructions, format_recovery_text

APP_DIR = Path(__file__).resolve().parent
LOGO_PATH = APP_DIR / "assets" / "team_logo.jpeg"
if not LOGO_PATH.is_file():
    LOGO_PATH = APP_DIR / "team_logo.jpeg"
GRAPH_SCRIPT = APP_DIR / "deploy_graph.py"
NORMAL_IMPACT_PACKETS, FAST_IMPACT_PACKETS = 60, 12      # Simulation Mode: packets (1 per second) before IMPACT
GPS_BAUD_RATES = (4800, 9600, 19200, 38400, 57600, 115200)
GPS_STALE_SECONDS = 5.0
FLIGHT_LOG_HEADER = "ID | IST | PACKET | STATE | ALTITUDE | REL_ALT | LATITUDE | LONGITUDE | VOLTAGE | POWER\n"
BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
GRAPH_HOST, GRAPH_PORT = "127.0.0.1", 5555
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
ANOMALY_CONFIG_PATH = APP_DIR / "anomaly_config.json"  # optional; see ANOMALY_DETECTION.md
ANOMALY_STATUS_COLORS = {STATUS_MONITORING: "#2fa572", STATUS_ANOMALY: "#ff9f1a", STATUS_INSUFFICIENT: "#9aa6b2", STATUS_INVALID: "#e5484d"}
ANOMALY_MAX_LINES = 500
SCOPE_SESSION, SCOPE_ALL = "This session", "All stored data"
FP_STATUS_COLORS = {"Waiting for sufficient telemetry": "#9aa6b2", "Ready: press Run / Refresh": "#9aa6b2", "Analysis in progress": "#ff9f1a",
                    "Analysis complete": "#2fa572", "Insufficient valid data": "#e5b800", "Analysis failed": "#e5484d"}


class SocketBroadcaster:
    """Non-blocking local TCP JSON server with bounded per-client output queues."""
    def __init__(self, host: str = GRAPH_HOST, port: int = GRAPH_PORT):
        self.host, self.port = host, port
        self.clients: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.server: socket.socket | None = None
        self.thread = threading.Thread(target=self._serve, name="telemetry-socket", daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(4)
            server.settimeout(0.5)
            self.server = server
        except OSError as exc:
            print(f"Graph socket unavailable on {self.host}:{self.port}: {exc}", file=sys.stderr)
            return
        while not self.stop_event.is_set():
            try:
                client, _ = server.accept()
                client.setblocking(False)
                with self.lock:
                    self.clients.add(client)
            except socket.timeout:
                continue
            except OSError:
                break
        server.close()

    def publish(self, payload: dict):
        data = (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        with self.lock:
            clients = tuple(self.clients)
        dead = []
        for client in clients:
            try:
                client.sendall(data)
            except (BlockingIOError, InterruptedError):
                # A slow display may miss a sample; serial acquisition stays unblocked.
                continue
            except OSError:
                dead.append(client)
        if dead:
            with self.lock:
                for client in dead:
                    self.clients.discard(client)
                    try: client.close()
                    except OSError: pass

    def close(self):
        self.stop_event.set()
        if self.server:
            try: self.server.close()
            except OSError: pass
        with self.lock:
            clients, self.clients = tuple(self.clients), set()
        for client in clients:
            try: client.close()
            except OSError: pass
        self.thread.join(1)


class SerialWorker:
    def __init__(self, settings: SerialSettings, output: queue.Queue, log: queue.Queue):
        self.settings, self.output, self.log = settings, output, log
        self.stop_event = threading.Event()
        self.serial = None
        self.write_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            import serial
            self.serial = serial.Serial(
                port=self.settings.port, baudrate=self.settings.baudrate,
                bytesize=self.settings.bytesize, parity=self.settings.parity,
                stopbits=self.settings.stopbits, timeout=self.settings.timeout,
                write_timeout=self.settings.write_timeout, xonxoff=self.settings.xonxoff,
                rtscts=self.settings.rtscts, dsrdtr=self.settings.dsrdtr,
            )
            try: self.serial.set_buffer_size(rx_size=self.settings.read_buffer_size)
            except (AttributeError, OSError): pass
            self.log.put(f"Connected to {self.settings.port} at {self.settings.baudrate} bps")
            while not self.stop_event.is_set():
                try:
                    line = self.serial.readline()
                    if line:
                        self.output.put(("frame", line.decode("ascii", errors="replace").strip()), timeout=0.1)
                except (serial.SerialException, OSError) as exc:
                    self.output.put(("error", f"Serial connection lost: {exc}"))
                    break
        except Exception as exc:
            self.output.put(("error", f"Could not open serial port: {exc}"))
        finally:
            if self.serial:
                try: self.serial.close()
                except Exception: pass
            self.output.put(("disconnected", "Serial reader stopped"))

    def send(self, command: str) -> None:
        if not self.serial or not self.serial.is_open:
            raise RuntimeError("Serial port is not connected")
        with self.write_lock:
            self.serial.write((command + "\r\n").encode("ascii"))
            self.serial.flush()

    def stop(self):
        self.stop_event.set()
        if self.serial:
            try: self.serial.cancel_read()
            except Exception: pass
            try: self.serial.close()
            except Exception: pass
        self.thread.join(2)


class SimulationWorker:
    """Generate schema-valid synthetic frames locally; never transmits over RF."""
    def __init__(self, output: queue.Queue, impact_after: int = NORMAL_IMPACT_PACKETS):
        self.output = output
        self.impact_after = impact_after
        self.stop_event = threading.Event()
        self.transmitting = threading.Event()
        self.thread = threading.Thread(target=self._run, name="telemetry-simulation", daemon=True)

    def set_fast_impact(self, enabled: bool) -> None:
        """Fast test mode: IMPACT after ~12 packets instead of ~60 (takes effect immediately)."""
        self.impact_after = FAST_IMPACT_PACKETS if enabled else NORMAL_IMPACT_PACKETS

    def start(self):
        self.thread.start()

    def command(self, command: str):
        if command == "CMD,START_TM": self.transmitting.set()
        elif command == "CMD,STOP_TM": self.transmitting.clear()

    def _run(self):
        packet = 0
        while not self.stop_event.wait(1.0):
            if not self.transmitting.is_set():
                continue
            packet += 1
            self.output.put(("frame", self._build_frame(packet)))

    def _build_frame(self, packet: int) -> str:
        impact_after = self.impact_after
        state = 7 if packet >= impact_after else (3 if packet >= (10 if impact_after >= 30 else 4) else 2)
        # after IMPACT the receiver sits on the ground: a metre or two of GNSS noise (deterministic, ~1-2 m)
        jitter_lat = 1.2e-5 * math.sin(packet * 1.9) if state == 7 else 0.0
        jitter_lon = 1.2e-5 * math.cos(packet * 2.7) if state == 7 else 0.0
        values = {
            "TEAM_ID": "SIM-0001", "TIMESTAMP": f"{time.time():.3f}", "PACKET_COUNT": str(packet),
            "ALTITUDE": f"{100 + min(packet, impact_after) * 2:.2f}", "PRESSURE": "100800", "TEMP": "22.5",
            "VOLTAGE": "7.4", "GNSS_TIME": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "GNSS_LATITUDE": f"{12.97160 + jitter_lat:.6f}", "GNSS_LONGITUDE": f"{77.59460 + jitter_lon:.6f}", "GNSS_ALTITUDE": "920",
            "GNSS_SATS": "9", "ACCEL_X": "0.1", "ACCEL_Y": "0.2", "ACCEL_Z": "9.7",
            "GYRO_X": "0.01", "GYRO_Y": "0.02", "GYRO_Z": "0.01", "MAG_X": "20", "MAG_Y": "5", "MAG_Z": "40",
            "ROLL": "1.2", "PITCH": "-0.8", "YAW": "42", "LIGHT_LUX": "350", "UV_INDEX": "2.1",
            "HALL_RPM": "0", "FLIGHT_SOFTWARE_STATE": str(state), "REL_ALTITUDE": "0", "VERTICAL_SPEED": "0",
            "GNSS_GROUND_SPEED": "0", "GNSS_COURSE": "42", "GNSS_HDOP": "0.9", "GNSS_FIX": "2",
            "HUMIDITY": "45", "CURRENT": "320", "POWER": "2.368",
        }
        return ",".join(values[field] for field in TELEMETRY_FIELDS)

    def stop(self):
        self.stop_event.set()
        self.thread.join(2)


class GroundStation(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AstrasenseNexus | CanSat Ground Station")
        self.geometry("1440x900")
        self.minsize(1100, 720)
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        self.started_at = time.monotonic()
        self.flight_started_at: float | None = None
        self.db = TelemetryDatabase()
        self.broadcaster = SocketBroadcaster()
        self.serial_worker: SerialWorker | None = None
        self.simulation_worker: SimulationWorker | None = None
        self.landing_coordinates: tuple[float, float] | None = None
        self.landing_estimator = LandingEstimator()      # averages several valid GNSS fixes after IMPACT
        self.gps_queue: queue.Queue = queue.Queue()
        self.gps_worker: GpsReceiverWorker | None = None
        self.gps_last_fix_time: float | None = None
        self.graph_process: subprocess.Popen | None = None
        self.inbox: queue.Queue = queue.Queue(maxsize=2000)
        self.log_queue: queue.Queue = queue.Queue(maxsize=1000)
        self.last_packet: int | None = None
        self.last_team_id: str | None = None
        self.packet_count = 0
        self.lost_packets = 0
        self.altitude_baseline: float | None = None
        self.previous_altitude: tuple[float, float] | None = None
        self.speed_samples: deque[float] = deque(maxlen=3)
        self.metrics: dict[str, tk.StringVar] = {}
        self.field_vars: dict[str, tk.StringVar] = {}
        self._anomaly_error_logged = False
        self.graph_data = GraphData()
        self.graph_window: TelemetryGraphWindow | None = None
        self._startup_notes: list[str] = []
        self.ai_queue: queue.Queue = queue.Queue()      # worker threads -> Tk thread (polled with after())
        self.fingerprint_result: FingerprintResult | None = None
        self._fingerprint_running = False
        self._report_running = False
        self._pending_report: dict | None = None
        self.simulation_seen = False                    # set when Simulation Mode is switched on
        self.flight_log_start_id = 0                    # Flight Log shows rows after this database id, numbered from 1
        self.session_start_id = self._read_session_start_id()
        self.anomaly_detector = self._create_anomaly_detector()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_queues)
        self.after(250, self._tick_clocks)
        self.after(1000, self._refresh_ports)
        self.after(1500, self._refresh_table)
        self.after(300, self._poll_ai_queue)
        self.after(500, self._poll_gps_queue)

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        logo = None
        if LOGO_PATH.is_file():
            try:
                from PIL import Image
                with Image.open(LOGO_PATH) as source:
                    logo_image = source.convert("RGB")
                logo = ctk.CTkImage(light_image=logo_image, dark_image=logo_image, size=(48, 48))
                self.logo_image = logo
            except Exception as exc:
                self._startup_notes.append(f"Team logo unavailable: {exc}")
        ctk.CTkLabel(header, text="AstrasenseNexus  |  CAN-7USAT Ground Station", image=logo, compound="left", font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, padx=18, pady=8, sticky="w")
        self.uptime_var = tk.StringVar(value="Software 00:00:00")
        self.flight_var = tk.StringVar(value="Flight --:--:--")
        self.clock_var = tk.StringVar(value="IST --:--:--")
        for col, var in enumerate((self.uptime_var, self.flight_var, self.clock_var), 1):
            ctk.CTkLabel(header, textvariable=var).grid(row=0, column=col, padx=9)
        self.mode_switch = ctk.CTkSwitch(header, text="Light", command=lambda: ctk.set_appearance_mode("Light" if self.mode_switch.get() else "Dark"))
        self.mode_switch.grid(row=0, column=4, padx=10)
        self.scale_menu = ctk.CTkOptionMenu(header, values=["100%", "125%", "150%"], command=self._set_scale, width=90)
        self.scale_menu.set("100%")
        self.scale_menu.grid(row=0, column=5, padx=(0, 14))

        sidebar = ctk.CTkFrame(self, width=270)
        sidebar.grid(row=1, column=0, sticky="nsew", padx=(10, 5), pady=10)
        sidebar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(sidebar, text="Communication", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, padx=12, pady=(12, 4), sticky="w")
        self.port_menu = ctk.CTkOptionMenu(sidebar, values=available_ports() or ["No ports"], width=220)
        self.port_menu.grid(row=1, column=0, padx=12, pady=5, sticky="ew")
        self.baud_menu = ctk.CTkOptionMenu(sidebar, values=[str(v) for v in BAUD_RATES], width=220)
        self.baud_menu.set("115200")
        self.baud_menu.grid(row=2, column=0, padx=12, pady=5, sticky="ew")
        self.connect_button = ctk.CTkButton(sidebar, text="Connect", command=self._toggle_connection)
        self.connect_button.grid(row=3, column=0, padx=12, pady=8, sticky="ew")
        ctk.CTkLabel(sidebar, text="Telecommands", font=ctk.CTkFont(size=16, weight="bold")).grid(row=4, column=0, padx=12, pady=(16, 4), sticky="w")
        for row, (label, command) in enumerate((("Start Telemetry", "CMD,START_TM"), ("Stop Telemetry", "CMD,STOP_TM"), ("Calibration 1", "CMD,CALIB_1"), ("Calibration 2", "CMD,CALIB_2"), ("Save EEPROM", "CMD,WRITE_EEPROM")), 5):
            ctk.CTkButton(sidebar, text=label, command=lambda cmd=command: self._dispatch(cmd)).grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        self.sim_switch = ctk.CTkSwitch(sidebar, text="Simulation Mode", command=self._toggle_simulation)
        self.sim_switch.grid(row=10, column=0, padx=12, pady=(12, 4), sticky="w")
        ctk.CTkButton(sidebar, text="Export CSV", command=self._export_csv).grid(row=11, column=0, padx=12, pady=4, sticky="ew")
        ctk.CTkLabel(sidebar, text="AI Analysis / Reports", font=ctk.CTkFont(size=16, weight="bold")).grid(row=12, column=0, padx=12, pady=(12, 4), sticky="w")
        self.report_button = ctk.CTkButton(sidebar, text="Generate AI Mission Report (PDF)", command=self._generate_report, fg_color="#E67E22", hover_color="#CF6D17", text_color="white")
        self.report_button.grid(row=13, column=0, padx=12, pady=4, sticky="ew")
        ctk.CTkLabel(sidebar, text="Live Log", font=ctk.CTkFont(size=16, weight="bold")).grid(row=14, column=0, padx=12, pady=(12, 4), sticky="w")
        self.log_box = ctk.CTkTextbox(sidebar, height=170, wrap="word")
        self.log_box.grid(row=15, column=0, padx=12, pady=(0, 12), sticky="nsew")
        sidebar.grid_rowconfigure(15, weight=1)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=1, column=1, sticky="nsew", padx=(5, 10), pady=10)
        overview = self.tabs.add("Overview")
        imu = self.tabs.add("IMU & Kinematics")
        nav = self.tabs.add("GNSS & Environment")
        self.nav_tab = nav
        recovery = self.tabs.add("Recovery")
        logtab = self.tabs.add("Flight Log")
        anomaly_tab = self.tabs.add("Anomaly Detection")
        fingerprint_tab = self.tabs.add("Environmental Fingerprinting")
        self._make_card_grid(overview, [
            ("Flight State", "STATE"), ("Packets / Lost", "PACKETS"), ("Loss", "LOSS"),
            ("Relative Altitude (m)", "REL_ALTITUDE"), ("Barometric Altitude (m)", "ALTITUDE"), ("Vertical Speed (m/s)", "VERTICAL_SPEED"),
            ("Pressure (Pa)", "PRESSURE"), ("Temperature (°C)", "TEMP"), ("Voltage (V)", "VOLTAGE"), ("Current (A)", "CURRENT"), ("Power (W)", "POWER"),
        ], columns=3)
        self._make_card_grid(imu, [(name.replace("_", " "), name) for name in ("ACCEL_X", "ACCEL_Y", "ACCEL_Z", "ACCEL_MAG", "GYRO_X", "GYRO_Y", "GYRO_Z", "MAG_X", "MAG_Y", "MAG_Z", "ROLL", "PITCH", "YAW", "HALL_RPM")], columns=3)
        self._make_card_grid(nav, [(name.replace("_", " "), name) for name in ("GNSS_FIX", "GNSS_SATS", "GNSS_HDOP", "GNSS_GROUND_SPEED", "GNSS_COURSE", "GNSS_LATITUDE", "GNSS_LONGITUDE", "GNSS_ALTITUDE", "LIGHT_LUX", "UV_INDEX", "HUMIDITY")], columns=3)
        self.map_button = ctk.CTkButton(nav, text="Open Map", command=self._toggle_map)
        self.map_button.grid(row=4, column=0, columnspan=3, padx=8, pady=(8, 2), sticky="ew")
        map_frame = ctk.CTkFrame(nav, height=300)
        map_frame.grid(row=5, column=0, columnspan=3, padx=8, pady=(2, 8), sticky="nsew")
        map_frame.grid_propagate(False)
        map_frame.grid_remove()
        nav.grid_rowconfigure(5, weight=0)
        nav.grid_columnconfigure((0, 1, 2), weight=1)
        self.map_frame = map_frame
        self.map_attribution = ctk.CTkLabel(map_frame, text="Map tiles © OpenStreetMap contributors · © CARTO", anchor="e", font=ctk.CTkFont(size=10))
        self.map_attribution.pack(side="bottom", fill="x", padx=8, pady=(0, 3))
        self.map_widget_frame = ctk.CTkFrame(map_frame, fg_color="transparent")
        self.map_widget_frame.pack(fill="both", expand=True)
        self.flight_map = FlightMap(self.map_widget_frame, height=270)
        self._build_recovery_tab(recovery)
        self.table = ctk.CTkTextbox(logtab, wrap="none")
        self.table.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_anomaly_tab(anomaly_tab)
        self._build_fingerprint_tab(fingerprint_tab)
        for note in self._startup_notes:
            self._log(note)
        self._open_graph_window()

    def _toggle_map(self):
        """Expand/collapse the map panel; tile resources start on the first open."""
        if getattr(self, "_map_animation_id", None) is not None:
            try: self.after_cancel(self._map_animation_id)
            except Exception: pass
            self._map_animation_id = None
        if getattr(self, "map_visible", False):
            self.map_visible = False
            self.map_button.configure(text="Open Map")
            self.nav_tab.grid_rowconfigure(5, weight=0)
            self._animate_map(int(self.map_frame.cget("height")), 0, closing=True)
        else:
            self.map_visible = True
            self.map_button.configure(text="Close Map")
            self.map_frame.grid()
            self.map_frame.configure(height=1)
            self.nav_tab.grid_rowconfigure(5, weight=1)
            self.flight_map.show()
            self._animate_map(1, 300, closing=False)

    def _animate_map(self, current: int, target: int, closing: bool):
        step = 24 if target > current else -24
        next_height = current + step
        if (step > 0 and next_height >= target) or (step < 0 and next_height <= target):
            next_height = target
        self.map_frame.configure(height=max(1, next_height))
        if next_height != target:
            self._map_animation_id = self.after(12, self._animate_map, next_height, target, closing)
        elif closing:
            self.flight_map.hide()
            self.map_frame.grid_remove()
            self._map_animation_id = None
        else:
            self._map_animation_id = None

    def _make_card_grid(self, parent, entries, columns=3):
        for col in range(columns): parent.grid_columnconfigure(col, weight=1)
        for index, (label, key) in enumerate(entries):
            card = ctk.CTkFrame(parent)
            row, col = divmod(index, columns)
            card.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            ctk.CTkLabel(card, text=label, text_color=("#52606d", "#9aa6b2")).pack(anchor="w", padx=10, pady=(7, 0))
            var = tk.StringVar(value="--")
            self.metrics[key] = var
            ctk.CTkLabel(card, textvariable=var, font=ctk.CTkFont(size=19, weight="bold")).pack(anchor="w", padx=10, pady=(1, 8))

    def _set_scale(self, value: str):
        ctk.set_widget_scaling(float(value.strip("%")) / 100.0)

    def _refresh_ports(self):
        if self.serial_worker is None:
            ports = available_ports() or ["No ports"]
            current = self.port_menu.get()
            self.port_menu.configure(values=ports)
            self.port_menu.set(current if current in ports else ports[0])
        self.after(3000, self._refresh_ports)

    def _toggle_connection(self):
        if self.simulation_worker:
            messagebox.showinfo("Simulation active", "Disable Simulation Mode before connecting to a serial port.")
            return
        if self.serial_worker:
            self.serial_worker.stop()
            self.serial_worker = None
            self.connect_button.configure(text="Connect")
            self._log("Disconnected by operator")
            return
        port = self.port_menu.get()
        if not port or port == "No ports":
            messagebox.showerror("Serial port", "Select an available COM port first.")
            return
        settings = SerialSettings(port=port, baudrate=int(self.baud_menu.get()))
        self.serial_worker = SerialWorker(settings, self.inbox, self.log_queue)
        self.serial_worker.start()
        self.connect_button.configure(text="Disconnect")
        # The six-graph window (telemetry_graphs.py) opens at start-up. The separate deploy_graph.py window is no longer
        # launched on connect, so only one sensor-graph window exists. (_launch_graph is kept but unused.)

    def _launch_graph(self):
        if self.graph_process and self.graph_process.poll() is None:
            return
        try:
            self.graph_process = subprocess.Popen([sys.executable, str(GRAPH_SCRIPT)], cwd=APP_DIR)
            self._log("Dynamic graph window launched")
        except OSError as exc:
            self._log(f"Could not launch graph window: {exc}")

    def _dispatch(self, command: str):
        if self.simulation_worker:
            self.simulation_worker.command(command)
            self._log(f"SIM > {command}")
            self._after_command_sent(command)
            return
        if not self.serial_worker:
            # No link: Simulation Mode is never switched on implicitly. The operator must connect the
            # LoRa/serial port, or turn the Simulation Mode switch on deliberately.
            self._log(f"NO LINK: '{command}' was not sent (no serial connection, Simulation Mode is off)")
            messagebox.showwarning(
                "No link",
                "There is no link to the CanSat.\n\nConnect the LoRa / serial port first (or switch on Simulation Mode "
                "yourself if you want local test data).\n\nThe command was not sent.")
            return
        try:
            self.serial_worker.send(command)
            self._log(f"TX > {command}")
            self._after_command_sent(command)
            if command == "CMD,CALIB_1":
                latest = getattr(self, "_latest_altitude", None)
                if latest is not None:
                    self.altitude_baseline = latest
        except Exception as exc:
            self._log(f"Command failed: {exc}")

    def _after_command_sent(self, command: str):
        """Start Telemetry begins a new Flight Log numbering (display only; stored telemetry is never deleted)."""
        if command != "CMD,START_TM":
            return
        try:
            self.flight_log_start_id = self.db.last_id()
        except Exception as exc:
            self._log(f"Flight Log numbering not restarted: {exc}")
            return
        self.table.delete("1.0", "end")
        self.table.insert("end", FLIGHT_LOG_HEADER)
        self._log("Flight Log restarted from ID 1")

    def _toggle_simulation(self):
        if self.sim_switch.get():
            self._start_simulation()
        elif self.simulation_worker:
            self.simulation_worker.stop()
            self.simulation_worker = None
            self.connect_button.configure(state="normal")
            self._log("Simulation disabled")

    def _start_simulation(self):
        if self.serial_worker:
            self.sim_switch.deselect()
            self._log("Disable serial connection before simulation")
            return
        if self.simulation_worker:
            return
        fast = bool(getattr(self, "fast_sim_switch", None) and self.fast_sim_switch.get())
        self.simulation_worker = SimulationWorker(self.inbox, FAST_IMPACT_PACKETS if fast else NORMAL_IMPACT_PACKETS)
        self.simulation_seen = True
        self._reset_landing_lock()          # a new simulated run must not reuse an earlier landing lock
        self.simulation_worker.start()
        self.connect_button.configure(state="disabled")
        self._log("Simulation enabled. Use Start Telemetry to generate local frames.")

    def _build_recovery_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(parent, text="CanSat recovery guidance", font=ctk.CTkFont(size=22, weight="bold")).grid(row=0, column=0, padx=16, pady=(18, 8), sticky="w")
        self.landing_var = tk.StringVar(value="Landing coordinates are not locked until IMPACT (state 7).")
        ctk.CTkLabel(parent, textvariable=self.landing_var, wraplength=800, justify="left").grid(row=1, column=0, padx=16, pady=8, sticky="w")
        tools = ctk.CTkFrame(parent, fg_color="transparent")
        tools.grid(row=2, column=0, padx=16, pady=(0, 4), sticky="ew")
        ctk.CTkButton(tools, text="Reset landing lock", width=150, command=self._reset_landing_lock).pack(side="left", padx=(0, 12))
        self.fast_sim_switch = ctk.CTkSwitch(tools, text="Fast IMPACT test (Simulation Mode: ~12 s instead of ~60 s)", command=self._on_fast_sim_toggle)
        self.fast_sim_switch.pack(side="left")
        inputs = ctk.CTkFrame(parent)
        inputs.grid(row=3, column=0, padx=16, pady=8, sticky="ew")
        self.user_lat_entry = ctk.CTkEntry(inputs, placeholder_text="Your latitude")
        self.user_lon_entry = ctk.CTkEntry(inputs, placeholder_text="Your longitude")
        self.heading_entry = ctk.CTkEntry(inputs, placeholder_text="Heading ° (clockwise from north)")
        for i, entry in enumerate((self.user_lat_entry, self.user_lon_entry, self.heading_entry)):
            entry.grid(row=0, column=i, padx=6, pady=8, sticky="ew")
            inputs.grid_columnconfigure(i, weight=1)
        ctk.CTkButton(inputs, text="Get Directions", command=self._update_navigation).grid(row=1, column=0, columnspan=3, padx=6, pady=(0, 8), sticky="ew")
        gps = ctk.CTkFrame(parent)
        gps.grid(row=4, column=0, padx=16, pady=4, sticky="ew")
        ctk.CTkLabel(gps, text="Recovery-team GPS receiver (optional, read-only)").grid(row=0, column=0, columnspan=3, padx=8, pady=(6, 0), sticky="w")
        self.gps_port_menu = ctk.CTkOptionMenu(gps, values=available_ports() or ["No ports"], width=180)
        self.gps_port_menu.grid(row=1, column=0, padx=8, pady=6)
        self.gps_baud_menu = ctk.CTkOptionMenu(gps, values=[str(v) for v in GPS_BAUD_RATES], width=100)
        self.gps_baud_menu.set("9600")
        self.gps_baud_menu.grid(row=1, column=1, padx=4, pady=6)
        self.gps_button = ctk.CTkButton(gps, text="Connect GPS", width=130, command=self._toggle_gps)
        self.gps_button.grid(row=1, column=2, padx=8, pady=6)
        self.gps_var = tk.StringVar(value="GPS not connected - enter your position and heading manually.")
        ctk.CTkLabel(gps, textvariable=self.gps_var, wraplength=760, justify="left").grid(row=2, column=0, columnspan=3, padx=8, pady=(0, 8), sticky="w")
        self.recovery_var = tk.StringVar(value="Enter your position and heading after landing coordinates are available.")
        ctk.CTkLabel(parent, textvariable=self.recovery_var, font=ctk.CTkFont(size=19, weight="bold"), wraplength=800, justify="left").grid(row=5, column=0, padx=16, pady=12, sticky="w")
        ctk.CTkLabel(parent, text="The landing point is the average of several valid GNSS fixes after IMPACT. GNSS course shows horizontal ground movement only; it does not "
                                  "show the CanSat's vertical falling direction. A GPS receiver's course is used as your heading only while you are moving.",
                     wraplength=800, justify="left", text_color=("#52606d", "#9aa6b2")).grid(row=6, column=0, padx=16, pady=(0, 12), sticky="w")

    def _update_navigation(self):
        if not self.landing_coordinates:
            self.recovery_var.set("Waiting for locked IMPACT coordinates.")
            return
        try:
            result = calculate_navigation_instructions(self.user_lat_entry.get(), self.user_lon_entry.get(), self.heading_entry.get(), *self.landing_coordinates)
            self.recovery_var.set(format_recovery_text(result))
            self.flight_map.set_navigation_origin(float(self.user_lat_entry.get()), float(self.user_lon_entry.get()))
            self.flight_map.set_target_position(*self.landing_coordinates)
        except (ValueError, TypeError) as exc:
            self.recovery_var.set(f"Check your latitude, longitude, and heading: {exc}")
            self.flight_map.set_navigation_origin(None, None)

    def _collect_impact_fix(self, record: dict):
        """Feed one post-IMPACT frame to the landing estimator; never lets an error disturb telemetry handling."""
        try:
            status = self.landing_estimator.add_fix(record["GNSS_FIX"], record["GNSS_LATITUDE"], record["GNSS_LONGITUDE"], record["GNSS_HDOP"])
            self.landing_var.set(status.message)
            if status.locked:
                self.landing_coordinates = status.coordinates
                self._log(status.message)
                self._update_navigation()
        except Exception as exc:
            self._log(f"Landing estimator error (telemetry unaffected): {exc}")

    def _reset_landing_lock(self):
        self.landing_estimator.reset()
        self.landing_coordinates = None
        self.landing_var.set("Landing coordinates are not locked until IMPACT (state 7).")
        self.recovery_var.set("Enter your position and heading after landing coordinates are available.")
        try:
            self.flight_map.set_navigation_origin(None, None)
        except Exception:
            pass

    def _on_fast_sim_toggle(self):
        if self.simulation_worker:
            self.simulation_worker.set_fast_impact(bool(self.fast_sim_switch.get()))
        self._log("Fast IMPACT test " + ("on (Simulation Mode only)" if self.fast_sim_switch.get() else "off"))

    # ---- optional recovery-team GPS receiver (offline NMEA, read-only)
    def _toggle_gps(self):
        if self.gps_worker:
            self._stop_gps("GPS disconnected - enter your position and heading manually.")
            return
        port = self.gps_port_menu.get()
        if port in ("", "No ports"):
            messagebox.showerror("GPS receiver", "No serial port selected for the GPS receiver.")
            return
        if self.serial_worker and self.serial_worker.settings.port == port:
            messagebox.showerror("GPS receiver", f"{port} is the telemetry (LoRa) link. Choose the GPS receiver's own port.")
            return
        self.gps_worker = GpsReceiverWorker(port, int(self.gps_baud_menu.get()), self.gps_queue)
        self.gps_worker.start()
        self.gps_last_fix_time = None
        self.gps_button.configure(text="Disconnect GPS")
        self.gps_var.set(f"Opening GPS receiver on {port} - waiting for a valid fix...")

    def _stop_gps(self, message: str):
        worker, self.gps_worker = self.gps_worker, None
        if worker:
            worker.stop()
        self.gps_last_fix_time = None
        self.gps_button.configure(text="Connect GPS")
        self.gps_var.set(message)

    @staticmethod
    def _set_entry(entry, text: str):
        entry.delete(0, "end")
        entry.insert(0, text)

    def _apply_gps_fix(self, fix):
        self._set_entry(self.user_lat_entry, f"{fix.latitude:.6f}")
        self._set_entry(self.user_lon_entry, f"{fix.longitude:.6f}")
        if fix.course_deg is not None:
            self._set_entry(self.heading_entry, f"{fix.course_deg:.0f}")
            heading = f"heading {fix.course_deg:.0f}° from GPS course (you are moving)"
        else:
            heading = "heading unchanged (GPS course is only used while moving) - keep your manual heading"
        extras = "".join((f" | HDOP {fix.hdop:.1f}" if fix.hdop is not None else "", f" | {fix.satellites} satellites" if fix.satellites is not None else ""))
        self.gps_last_fix_time = time.monotonic()
        self.gps_var.set(f"GPS fix: {fix.latitude:.6f}, {fix.longitude:.6f}{extras}\n{heading}")
        if self.landing_coordinates:
            self._update_navigation()

    def _poll_gps_queue(self):
        try:
            while True:
                try:
                    kind, payload = self.gps_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "gps_fix" and self.gps_worker:
                    self._apply_gps_fix(payload)
                elif kind == "gps_status":
                    self._log(payload)
                elif kind == "gps_error":
                    self._log(payload)
                    self._stop_gps(payload + " - enter your position and heading manually.")
            if self.gps_worker and self.gps_last_fix_time is not None and time.monotonic() - self.gps_last_fix_time > GPS_STALE_SECONDS:
                self.gps_var.set(f"No new GPS fix for {time.monotonic() - self.gps_last_fix_time:.0f} s - the position shown is out of date.")
        except Exception as exc:
            try: self._log(f"GPS handling error (telemetry unaffected): {exc}")
            except Exception: pass
        try:
            self.after(500, self._poll_gps_queue)
        except Exception:
            pass

    def _process_record(self, record: dict):
        raw_record = dict(record)  # untouched wire values for the read-only anomaly detector
        try:
            self.last_team_id = str(record["TEAM_ID"])
            raw_current = float(record["CURRENT"])
            current = raw_current / 1000.0 if abs(raw_current) > 1000 else raw_current
            record["CURRENT"] = current
            record["POWER"] = float(record["VOLTAGE"]) * current
            if self.altitude_baseline is None:
                self.altitude_baseline = float(record["ALTITUDE"])
            record["REL_ALTITUDE"] = float(record["ALTITUDE"]) - self.altitude_baseline
            timestamp = timestamp_to_seconds(record["TIMESTAMP"])
            record["TIMESTAMP"] = timestamp
            altitude = float(record["ALTITUDE"])
            if self.previous_altitude:
                old_time, old_alt = self.previous_altitude
                delta_t = timestamp - old_time
                if delta_t > 0:
                    self.speed_samples.append((altitude - old_alt) / delta_t)
            self.previous_altitude = (timestamp, altitude)
            if self.speed_samples:
                record["VERTICAL_SPEED"] = sum(self.speed_samples) / len(self.speed_samples)
            record["ACCEL_MAG"] = math.sqrt(sum(float(record[f"ACCEL_{axis}"]) ** 2 for axis in "XYZ"))
            packet = int(record["PACKET_COUNT"])
            self.packet_count += 1
            if self.last_packet is not None:
                delta = packet - self.last_packet
                if delta > 1:
                    missed = delta - 1
                    self.lost_packets += missed
                    print(f"PACKET JUMP: {self.last_packet} -> {packet}; missing {missed}", file=sys.stderr, flush=True)
                    self._log(f"PACKET JUMP: {self.last_packet} -> {packet}; missing {missed}")
            self.last_packet = packet
            if self.flight_started_at is None and int(record["FLIGHT_SOFTWARE_STATE"]) >= 2:
                self.flight_started_at = time.monotonic()
            if int(record["FLIGHT_SOFTWARE_STATE"]) == 7 and self.landing_coordinates is None:
                self._collect_impact_fix(record)
            fix = int(record["GNSS_FIX"])
            if fix > 0:
                target = self.landing_coordinates or (float(record["GNSS_LATITUDE"]), float(record["GNSS_LONGITUDE"]))
                self.flight_map.set_target_position(*target, redraw=False)
            self.db.submit(record)
            self.broadcaster.publish(record)
            self._run_anomaly_detection(raw_record)
            self._feed_graphs(record)
            self._update_cards(record)
            self.flight_map.update(float(record["GNSS_LATITUDE"]), float(record["GNSS_LONGITUDE"]), fix, float(record["GNSS_ALTITUDE"]))
            self._latest_altitude = altitude
        except Exception as exc:
            self._log(f"Frame processing error: {exc}")

    # ---- environmental fingerprinting + combined AI report (offline and read-only: never transmits) ----
    def _read_session_start_id(self) -> int:
        try:
            return self.db.last_id()
        except Exception as exc:
            self._startup_notes.append(f"Could not read database position ({exc}); 'This session' will include all stored rows.")
            return 0

    def _build_fingerprint_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)
        controls = ctk.CTkFrame(parent)
        controls.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        self.fp_run_button = ctk.CTkButton(controls, text="Run / Refresh Fingerprinting", command=self._run_fingerprint)
        self.fp_run_button.pack(side="left", padx=(10, 8), pady=8)
        self.fp_scope_menu = ctk.CTkOptionMenu(controls, values=[SCOPE_SESSION, SCOPE_ALL], width=150)
        self.fp_scope_menu.set(SCOPE_SESSION)
        self.fp_scope_menu.pack(side="left", padx=4)
        ctk.CTkLabel(controls, text="From t (s)").pack(side="left", padx=(12, 2))
        self.fp_from_entry = ctk.CTkEntry(controls, width=70, placeholder_text="start")
        self.fp_from_entry.pack(side="left")
        ctk.CTkLabel(controls, text="To t (s)").pack(side="left", padx=(8, 2))
        self.fp_to_entry = ctk.CTkEntry(controls, width=70, placeholder_text="end")
        self.fp_to_entry.pack(side="left")
        self.fp_status_var = tk.StringVar(value="Waiting for sufficient telemetry")
        self.fp_status_label = ctk.CTkLabel(parent, textvariable=self.fp_status_var, anchor="w", font=ctk.CTkFont(size=18, weight="bold"), text_color=FP_STATUS_COLORS["Waiting for sufficient telemetry"])
        self.fp_status_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 0))
        self.fp_summary_var = tk.StringVar(value="Offline, unsupervised statistical analysis (K-means) of validated telemetry. Not a trained model; not validated on real flight data.")
        ctk.CTkLabel(parent, textvariable=self.fp_summary_var, anchor="w", justify="left", wraplength=1000, text_color=("#52606d", "#9aa6b2")).grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 4))
        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 8))
        body.grid_columnconfigure((0, 1), weight=1, uniform="fp")
        body.grid_rowconfigure(0, weight=1)
        self.fp_text = ctk.CTkTextbox(body, wrap="word", font=ctk.CTkFont(family="Courier New", size=12))
        self.fp_text.grid(row=0, column=0, sticky="nsew", padx=(2, 4))
        self.fp_text.insert("end", "Press 'Run / Refresh Fingerprinting' once enough telemetry has been received.\n")
        self.fp_figure = self.fp_canvas = None
        chart_frame = ctk.CTkFrame(body)
        chart_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 2))
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
            from fingerprint_charts import clear_with_message
            self.fp_figure = Figure(figsize=(6, 5), dpi=100, facecolor="white")
            clear_with_message(self.fp_figure, "Charts appear after the analysis runs")
            self.fp_canvas = FigureCanvasTkAgg(self.fp_figure, master=chart_frame)
            self.fp_canvas.get_tk_widget().pack(fill="both", expand=True)
        except Exception as exc:
            ctk.CTkLabel(chart_frame, text=f"Charts unavailable: {exc}").pack(padx=10, pady=10)

    @staticmethod
    def _parse_optional_float(text: str) -> float | None:
        text = (text or "").strip()
        if not text:
            return None
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("not finite")
        return value

    def _set_fp_status(self, status: str, summary: str | None = None):
        self.fp_status_var.set(status)
        self.fp_status_label.configure(text_color=FP_STATUS_COLORS.get(status, "#9aa6b2"))
        if summary is not None:
            self.fp_summary_var.set(summary)

    def _update_fingerprint_idle_status(self):
        if self._fingerprint_running or self.fingerprint_result is not None:
            return
        wanted = "Ready: press Run / Refresh" if self.packet_count >= 10 else "Waiting for sufficient telemetry"
        if self.fp_status_var.get() != wanted:
            self._set_fp_status(wanted)

    def _run_fingerprint(self):
        """Run the offline analysis in a worker thread; the result returns through self.ai_queue."""
        if self._fingerprint_running:
            return
        try:
            t_start = self._parse_optional_float(self.fp_from_entry.get())
            t_end = self._parse_optional_float(self.fp_to_entry.get())
            if t_start is not None and t_end is not None and t_start > t_end:
                raise ValueError("start after end")
        except ValueError:
            self._set_fp_status("Analysis failed", "The From/To interval must be empty or numbers (mission TIMESTAMP in seconds), with From <= To.")
            return
        session = self.fp_scope_menu.get() == SCOPE_SESSION
        since_id = self.session_start_id if session else 0
        scope = (SCOPE_SESSION + " (rows stored since the application started)") if session else (SCOPE_ALL + " (may contain several flights)")
        source = ("Live serial telemetry received in this application session (validated frames stored in the telemetry database)" if session
                  else "All rows in the telemetry database; the origin of rows stored in earlier runs is not recorded")
        self._fingerprint_running = True
        self.fp_run_button.configure(state="disabled")
        self._set_fp_status("Analysis in progress", "Reading stored telemetry and analysing it in the background...")
        threading.Thread(target=self._fingerprint_worker, name="fingerprint", daemon=True,
                         args=(self.db, self.ai_queue, since_id, t_start, t_end, scope, source)).start()

    @staticmethod
    def _fingerprint_worker(db, out_queue, since_id, t_start, t_end, scope, source):
        try:
            rows = db.history(since_id, REQUIRED_COLUMNS)
            result = analyze_environment(rows, None, t_start, t_end, scope, source)
        except Exception as exc:  # never raise into the thread machinery; report as a failed analysis
            result = failed_result(f"{type(exc).__name__}: {exc}", scope, source)
        out_queue.put(("fingerprint", result))

    def _poll_ai_queue(self):
        try:
            while True:
                try:
                    message = self.ai_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_ai_message(message)
            self._update_fingerprint_idle_status()
        except Exception as exc:
            try: self._log(f"AI analysis UI error (telemetry unaffected): {exc}")
            except Exception: pass
        try:
            self.after(250, self._poll_ai_queue)
        except Exception:
            pass

    def _handle_ai_message(self, message):
        kind = message[0]
        if kind == "fingerprint":
            self._fingerprint_running = False
            self.fp_run_button.configure(state="normal")
            self.fingerprint_result = message[1]
            self._show_fingerprint(message[1])
            if self._pending_report is not None:
                job, self._pending_report = self._pending_report, None
                self._start_report(job)
        elif kind == "report_ok":
            self._report_running = False
            self.report_button.configure(state="normal", text="Generate AI Mission Report (PDF)")
            self._log(f"AI report saved: {message[1]}")
            messagebox.showinfo("AI Mission Report", f"Report saved to:\n{message[1]}")
        elif kind == "report_failed":
            self._report_running = False
            self.report_button.configure(state="normal", text="Generate AI Mission Report (PDF)")
            self._log(f"AI report failed: {message[1]}")
            messagebox.showerror("AI Mission Report", f"The report could not be created:\n{message[1]}")

    def _show_fingerprint(self, result: FingerprintResult):
        """Display a result; a failed analysis is never presented as a success."""
        if result.status == FP_COMPLETE:
            coverage = ", ".join(f"{s.label} {s.coverage:.0%}" for s in result.feature_stats)
            self._set_fp_status("Analysis complete", f"{result.name}  |  {result.n_observations} observations ({result.n_complete} complete)  |  coverage: {coverage}")
        elif result.status == FP_INSUFFICIENT:
            self._set_fp_status("Insufficient valid data", result.message)
        else:
            self._set_fp_status("Analysis failed", result.message)
        self._log(f"Fingerprinting: {result.message}")
        self.fp_text.delete("1.0", "end")
        self.fp_text.insert("end", format_fingerprint_text(result))
        if self.fp_figure is not None:
            try:
                from fingerprint_charts import clear_with_message, draw_ui_overview
                if result.status == FP_COMPLETE:
                    draw_ui_overview(self.fp_figure, result)
                else:
                    clear_with_message(self.fp_figure, result.message)
                self.fp_canvas.draw_idle()
            except Exception as exc:
                self._log(f"Fingerprint chart error (results are still shown as text): {exc}")

    def _generate_report(self):
        """Ask where to save, then build the combined PDF in a worker thread from the dashboard's own results."""
        if self._report_running or self._pending_report is not None:
            self._log("An AI report is already being generated")
            return
        team = self.last_team_id or "UNKNOWN"
        suggested = f"CanSat_AI_Report_{team}_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        destination = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=suggested, filetypes=[("PDF files", "*.pdf")])
        if not destination:
            return
        try:
            snapshot = snapshot_anomaly_detector(self.anomaly_detector)
        except Exception as exc:
            snapshot = AnomalySnapshot(available=False, error=f"{type(exc).__name__}: {exc}")
        job = {"path": destination, "anomaly": snapshot, "team": self.last_team_id,
               "simulation": bool(getattr(self, "simulation_seen", False))}
        if self._fingerprint_running:
            self._pending_report = job
            self._log("Report will be created when the running fingerprint analysis finishes")
        elif self.fingerprint_result is None:
            self._pending_report = job
            self._run_fingerprint()                   # same code path as the dashboard button
            if not self._fingerprint_running:         # e.g. invalid interval text: do not wait forever
                self._pending_report = None
                self._log("Report cancelled: fingerprint analysis could not start")
        else:
            self._start_report(job)

    def _start_report(self, job: dict):
        inputs = ReportInputs(fingerprint=self.fingerprint_result, anomaly=job["anomaly"], team_id=job["team"],
                              data_source=self.fingerprint_result.data_source if self.fingerprint_result else "",
                              simulation_in_session=bool(job.get("simulation")))
        self._report_running = True
        self.report_button.configure(state="disabled", text="Generating report...")
        self._log(f"Generating AI report: {job['path']}")
        threading.Thread(target=self._report_worker, name="ai-report", daemon=True, args=(self.ai_queue, job["path"], inputs)).start()

    @staticmethod
    def _report_worker(out_queue, path, inputs):
        try:
            written = generate_report(path, inputs)
            out_queue.put(("report_ok", str(written)))
        except Exception as exc:  # includes ImportError with the reportlab install hint
            out_queue.put(("report_failed", f"{type(exc).__name__}: {exc}" if not isinstance(exc, ImportError) else str(exc)))

    # ---- six-graph telemetry window (read-only view of already-validated records) ----
    def _open_graph_window(self):
        try:
            self.graph_window = TelemetryGraphWindow(self, self.graph_data)
        except Exception as exc:  # the station must start even if the graph window cannot
            self.graph_window = None
            self._log(f"Telemetry graph window not opened: {exc}")

    def _feed_graphs(self, record: dict):
        try:
            self.graph_data.add_record(record)
        except Exception as exc:
            self._log(f"Graph data error (display only, telemetry unaffected): {exc}")

    # ---- anomaly detection (analysis only: never transmits, never alters telemetry) ----
    def _create_anomaly_detector(self) -> AnomalyDetector:
        config = DetectorConfig()
        if ANOMALY_CONFIG_PATH.exists():
            try:
                config = load_config(ANOMALY_CONFIG_PATH)
                self._startup_notes.append(f"Anomaly config loaded: {ANOMALY_CONFIG_PATH.name}")
            except Exception as exc:
                self._startup_notes.append(f"Anomaly config ignored, defaults used: {exc}")
        return AnomalyDetector(config)

    def _build_anomaly_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)
        top = ctk.CTkFrame(parent)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        self.anomaly_status_var = tk.StringVar(value=self.anomaly_detector.status)
        self.anomaly_status_label = ctk.CTkLabel(top, textvariable=self.anomaly_status_var, font=ctk.CTkFont(size=20, weight="bold"), text_color=ANOMALY_STATUS_COLORS[self.anomaly_detector.status])
        self.anomaly_status_label.pack(side="left", padx=12, pady=8)
        self.anomaly_counts_var = tk.StringVar(value="")
        ctk.CTkLabel(top, textvariable=self.anomaly_counts_var).pack(side="right", padx=12)
        limits = self.anomaly_detector.config.limits
        limit_note = f"{len(limits)} engineering limit(s) configured" + (" (provisional)" if self.anomaly_detector.config.limits_provisional else "") if limits else "no engineering limits configured"
        ctk.CTkLabel(parent, anchor="w", justify="left", wraplength=1000, text_color=("#52606d", "#9aa6b2"),
                     text="Rule-based / statistical, offline and read-only (not a trained model; cannot detect every sensor failure; "
                          f"a sensor stuck at a new stable value can be missed). Flight phase = firmware FLIGHT_SOFTWARE_STATE. {limit_note}."
                     ).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.anomaly_box = ctk.CTkTextbox(parent, wrap="none")
        self.anomaly_box.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.anomaly_box.insert("end", "TIMESTAMP | SEVERITY | KIND | SENSOR | VALUE | SCORE | STATE | REASON\n")
        self._refresh_anomaly_panel([])

    def _refresh_anomaly_panel(self, alerts):
        detector = self.anomaly_detector
        status = detector.status
        self.anomaly_status_var.set(status)
        self.anomaly_status_label.configure(text_color=ANOMALY_STATUS_COLORS.get(status, "#9aa6b2"))
        counts = detector.counts()
        self.anomaly_counts_var.set(f"limit {counts['limit']} | statistical {counts['statistical']} | data quality {counts['data_quality']} | repeats suppressed {counts['suppressed']} | rejected frames {counts['rejected_frames']}")
        for alert in alerts:
            self.anomaly_box.insert("end", alert.format_row() + "\n")
        if alerts:
            self.anomaly_box.see("end")
            try:
                if int(self.anomaly_box.index("end-1c").split(".")[0]) > ANOMALY_MAX_LINES:
                    self.anomaly_box.delete("2.0", "102.0")
            except Exception: pass

    def _run_anomaly_detection(self, raw_record: dict):
        try:
            self._refresh_anomaly_panel(self.anomaly_detector.evaluate(raw_record))
        except Exception as exc:  # analysis must never disturb telemetry handling
            if not self._anomaly_error_logged:
                self._anomaly_error_logged = True
                self._log(f"Anomaly detector error (analysis only, telemetry unaffected): {exc}")

    def _note_rejected_frame(self, reason: str):
        try:
            self._refresh_anomaly_panel(self.anomaly_detector.note_rejected_frame(reason))
        except Exception: pass

    def _update_cards(self, record):
        state = int(record["FLIGHT_SOFTWARE_STATE"])
        values = dict(record)
        values["STATE"] = STATE_NAMES[state]
        values["PACKETS"] = f"{self.packet_count} / {self.lost_packets}"
        values["LOSS"] = f"{100 * self.lost_packets / max(1, self.packet_count + self.lost_packets):.2f}%"
        values["GNSS_FIX"] = ("No Fix", "2D Fix", "3D Fix")[int(record["GNSS_FIX"])]
        values["ACCEL_MAG"] = record["ACCEL_MAG"]
        for key, value in values.items():
            var = self.metrics.get(key)
            if var:
                if isinstance(value, float):
                    var.set(f"{value:.3f}")
                else:
                    var.set(str(value))
        self._log(f"RX #{record['PACKET_COUNT']} | {STATE_NAMES[state]} | alt {record['ALTITUDE']:.1f} m")

    def _drain_queues(self):
        try:
            while True:
                kind, payload = self.inbox.get_nowait()
                if kind == "frame":
                    try:
                        record = parse_frame(payload)
                        self._process_record(record)
                    except ValueError as exc:
                        self._log(f"FRAME ERROR: {exc} | {payload[:100]}")
                        self._note_rejected_frame(str(exc))
                elif kind == "error":
                    self._log(payload)
                    if self.serial_worker:
                        self.serial_worker = None
                        self.connect_button.configure(text="Connect")
                else:
                    self._log(payload)
        except queue.Empty:
            pass
        try:
            while True:
                self._log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._drain_queues)

    def _refresh_table(self):
        try:
            start = self.flight_log_start_id
            rows = [row for row in self.db.recent(100) if row["id"] > start]
            self.table.delete("1.0", "end")
            self.table.insert("end", FLIGHT_LOG_HEADER)
            for row in rows:
                state = int(row["FLIGHT_SOFTWARE_STATE"])
                received = datetime.fromisoformat(row["received_utc"].replace("Z", "+00:00"))
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
                received_ist = received.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
                self.table.insert("end", f"{row['id'] - start} | {received_ist} | {row['PACKET_COUNT']} | {STATE_NAMES[state]} | {row['ALTITUDE']} | {row['REL_ALTITUDE']} | {row['GNSS_LATITUDE']} | {row['GNSS_LONGITUDE']} | {row['VOLTAGE']} | {row['POWER']}\n")
        except Exception as exc:
            self._log(f"Database view error: {exc}")
        self.after(2000, self._refresh_table)

    def _export_csv(self):
        team = self.last_team_id or "UNKNOWN"
        suggested = f"Flight_{team}.csv"
        destination = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=suggested, filetypes=[("CSV files", "*.csv")])
        if not destination:
            return
        def worker():
            try:
                path = self.db.export_csv(destination)
                self.log_queue.put(f"CSV exported: {path}")
            except Exception as exc:
                self.log_queue.put(f"CSV export failed: {exc}")
        threading.Thread(target=worker, name="csv-export", daemon=True).start()

    def _tick_clocks(self):
        def fmt(seconds):
            seconds = int(max(0, seconds))
            return f"{seconds // 3600:02}:{(seconds // 60) % 60:02}:{seconds % 60:02}"
        self.uptime_var.set("Software " + fmt(time.monotonic() - self.started_at))
        self.flight_var.set("Flight " + (fmt(time.monotonic() - self.flight_started_at) if self.flight_started_at else "--:--:--"))
        self.clock_var.set("IST " + datetime.now(timezone.utc).astimezone(IST).strftime("%H:%M:%S"))
        self.after(250, self._tick_clocks)

    def _log(self, message: str):
        stamp = datetime.now(IST).strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{stamp}] {message}\n")
        self.log_box.see("end")
        try:
            if int(self.log_box.index("end-1c").split(".")[0]) > 800:
                self.log_box.delete("1.0", "100.0")
        except Exception: pass

    def _close(self):
        if self.serial_worker:
            self.serial_worker.stop()
        if self.simulation_worker:
            self.simulation_worker.stop()
        if self.gps_worker:
            self.gps_worker.stop()
        if self.graph_process and self.graph_process.poll() is None:
            self.graph_process.terminate()
        self.broadcaster.close()
        self.db.close()
        self.destroy()


def main():
    app = GroundStation()
    app.mainloop()


if __name__ == "__main__":
    main()

    