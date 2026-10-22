"""CanSat Ground Station: asynchronous serial, persistence, map and graph bridge."""
from __future__ import annotations

from email import header
import json
import math
import queue
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from PIL import Image
from anomaly_detection import STATUS_ANOMALY, STATUS_INSUFFICIENT, STATUS_INVALID, STATUS_MONITORING, AnomalyDetector, DetectorConfig, load_config
from database_handling import TelemetryDatabase
from setup_map import FlightMap
from station_control import STATE_NAMES, SerialSettings, TELEMETRY_FIELDS, available_ports, parse_frame
from telemetry_graphs import GraphData, TelemetryGraphWindow

APP_DIR = Path(__file__).resolve().parent
GRAPH_SCRIPT = APP_DIR / "deploy_graph.py"
BAUD_RATES = (9600, 19200, 38400, 57600, 115200)
GRAPH_HOST, GRAPH_PORT = "127.0.0.1", 5555
ANOMALY_CONFIG_PATH = APP_DIR / "anomaly_config.json"  # optional; see ANOMALY_DETECTION.md
ANOMALY_STATUS_COLORS = {STATUS_MONITORING: "#2fa572", STATUS_ANOMALY: "#ff9f1a", STATUS_INSUFFICIENT: "#9aa6b2", STATUS_INVALID: "#e5484d"}
ANOMALY_MAX_LINES = 500


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


class GroundStation(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("CanGii | CanSat Ground Station")
        self.geometry("1440x900")
        self.minsize(1100, 720)
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        self.started_at = time.monotonic()
        self.flight_started_at: float | None = None
        self.db = TelemetryDatabase()
        self.broadcaster = SocketBroadcaster()
        self.serial_worker: SerialWorker | None = None
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
        self.anomaly_detector = self._create_anomaly_detector()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._drain_queues)
        self.after(250, self._tick_clocks)
        self.after(1000, self._refresh_ports)
        self.after(1500, self._refresh_table)

    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        
        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")

# Header layout
        header.grid_columnconfigure(0, weight=0)
        header.grid_columnconfigure(1, weight=1)

# Load team logo
        logo_path = APP_DIR / "assets" / "team_logo.jpeg"

        if logo_path.is_file():
            pil_logo = Image.open(logo_path).convert("RGBA")

            self.logo_image = ctk.CTkImage(
                light_image=pil_logo,
                dark_image=pil_logo,
                size=(44, 44)
    )

            ctk.CTkLabel(
                header,
                image=self.logo_image,
                text=""
            ).grid(
                row=0, column=0,
                padx=(18, 8), pady=8,
                sticky="w"
            )
        else:
            self.logo_image = None
            print(f"Team logo not found: {logo_path}")

# Team name and ground station title
        ctk.CTkLabel(
            header,
            text="ASTRASENSE NEXUS | CAN-7-USAT Ground Station",
            font=ctk.CTkFont(size=20, weight="bold")
        ).grid(
            row=0, column=1,
            padx=8, pady=12,
            sticky="w"
        )


# Status clocks
        self.uptime_var = tk.StringVar(value="Software 00:00:00")
        self.flight_var = tk.StringVar(value="Flight --:--:--")
        self.clock_var = tk.StringVar(value="UTC --:--:--")

        for col, var in enumerate(
            (self.uptime_var, self.flight_var, self.clock_var), 2
):
            ctk.CTkLabel(
                header,
                textvariable=var
            ).grid(row=0, column=col, padx=9)

# Appearance switch
        self.mode_switch = ctk.CTkSwitch(
            header,
            text="Light",
            command=lambda: ctk.set_appearance_mode(
                "Light" if self.mode_switch.get() else "Dark"
    )
)
        self.mode_switch.grid(row=0, column=5, padx=10)

# Scaling menu
        self.scale_menu = ctk.CTkOptionMenu(
            header,
            values=["100%", "125%", "150%"],
            command=self._set_scale,
            width=90
)
        self.scale_menu.set("100%")
        self.scale_menu.grid(row=0, column=6, padx=(0, 14))


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
        for row, (label, command) in enumerate((("Start Telemetry", "CMD,START_TM"), ("Calibration 1", "CMD,CALIB_1"), ("Calibration 2", "CMD,CALIB_2"), ("Save EEPROM", "CMD,WRITE_EEPROM")), 5):
            ctk.CTkButton(sidebar, text=label, command=lambda cmd=command: self._dispatch(cmd)).grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        ctk.CTkButton(sidebar, text="Export CSV", command=self._export_csv).grid(row=9, column=0, padx=12, pady=(12, 4), sticky="ew")
        ctk.CTkLabel(sidebar, text="Live Log", font=ctk.CTkFont(size=16, weight="bold")).grid(row=10, column=0, padx=12, pady=(12, 4), sticky="w")
        self.log_box = ctk.CTkTextbox(sidebar, height=270, wrap="word")
        self.log_box.grid(row=11, column=0, padx=12, pady=(0, 12), sticky="nsew")
        sidebar.grid_rowconfigure(11, weight=1)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=1, column=1, sticky="nsew", padx=(5, 10), pady=10)
        overview = self.tabs.add("Overview")
        imu = self.tabs.add("IMU & Kinematics")
        nav = self.tabs.add("GNSS & Environment")
        logtab = self.tabs.add("Flight Log")
        anomaly_tab = self.tabs.add("Anomaly Detection")
        self._make_card_grid(overview, [
            ("Flight State", "STATE"), ("Packets / Lost", "PACKETS"), ("Loss", "LOSS"),
            ("Relative Altitude (m)", "REL_ALTITUDE"), ("Barometric Altitude (m)", "ALTITUDE"), ("Vertical Speed (m/s)", "VERTICAL_SPEED"),
            ("Pressure (Pa)", "PRESSURE"), ("Temperature (°C)", "TEMP"), ("Voltage (V)", "VOLTAGE"), ("Current (A)", "CURRENT"), ("Power (W)", "POWER"),
        ], columns=3)
        self._make_card_grid(imu, [(name.replace("_", " "), name) for name in ("ACCEL_X", "ACCEL_Y", "ACCEL_Z", "ACCEL_MAG", "GYRO_X", "GYRO_Y", "GYRO_Z", "MAG_X", "MAG_Y", "MAG_Z", "ROLL", "PITCH", "YAW", "HALL_RPM")], columns=3)
        self._make_card_grid(nav, [(name.replace("_", " "), name) for name in ("GNSS_FIX", "GNSS_SATS", "GNSS_HDOP", "GNSS_GROUND_SPEED", "GNSS_COURSE", "GNSS_LATITUDE", "GNSS_LONGITUDE", "GNSS_ALTITUDE", "LIGHT_LUX", "UV_INDEX", "HUMIDITY")], columns=3)
        map_frame = ctk.CTkFrame(nav)
        map_frame.grid(row=4, column=0, columnspan=3, padx=8, pady=8, sticky="nsew")
        nav.grid_rowconfigure(4, weight=1)
        nav.grid_columnconfigure((0, 1, 2), weight=1)
        self.flight_map = FlightMap(map_frame, height=300)
        self.table = ctk.CTkTextbox(logtab, wrap="none")
        self.table.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_anomaly_tab(anomaly_tab)
        for note in self._startup_notes:
            self._log(note)
        self._open_graph_window()

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
        self._launch_graph()

    def _launch_graph(self):
        if self.graph_process and self.graph_process.poll() is None:
            return
        try:
            self.graph_process = subprocess.Popen([sys.executable, str(GRAPH_SCRIPT)], cwd=APP_DIR)
            self._log("Dynamic graph window launched")
        except OSError as exc:
            self._log(f"Could not launch graph window: {exc}")

    def _dispatch(self, command: str):
        if not self.serial_worker:
            self._log(f"Command not sent; serial disconnected: {command}")
            return
        try:
            self.serial_worker.send(command)
            self._log(f"TX > {command}")
            if command == "CMD,CALIB_1":
                latest = getattr(self, "_latest_altitude", None)
                if latest is not None:
                    self.altitude_baseline = latest
        except Exception as exc:
            self._log(f"Command failed: {exc}")

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
            timestamp = float(record["TIMESTAMP"])
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
            self.db.submit(record)
            self.broadcaster.publish(record)
            self._run_anomaly_detection(raw_record)
            self._feed_graphs(record)
            self._update_cards(record)
            self.flight_map.update(float(record["GNSS_LATITUDE"]), float(record["GNSS_LONGITUDE"]), int(record["GNSS_FIX"]))
            self._latest_altitude = altitude
        except Exception as exc:
            self._log(f"Frame processing error: {exc}")

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
            rows = self.db.recent(100)
            self.table.delete("1.0", "end")
            self.table.insert("end", "ID | UTC | PACKET | STATE | ALTITUDE | REL_ALT | LATITUDE | LONGITUDE | VOLTAGE | POWER\n")
            for row in rows:
                state = int(row["FLIGHT_SOFTWARE_STATE"])
                self.table.insert("end", f"{row['id']} | {row['received_utc']} | {row['PACKET_COUNT']} | {STATE_NAMES[state]} | {row['ALTITUDE']} | {row['REL_ALTITUDE']} | {row['GNSS_LATITUDE']} | {row['GNSS_LONGITUDE']} | {row['VOLTAGE']} | {row['POWER']}\n")
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
        self.clock_var.set("UTC " + datetime.now(timezone.utc).strftime("%H:%M:%S"))
        self.after(250, self._tick_clocks)

    def _log(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{stamp}] {message}\n")
        self.log_box.see("end")
        try:
            if int(self.log_box.index("end-1c").split(".")[0]) > 800:
                self.log_box.delete("1.0", "100.0")
        except Exception: pass

    def _close(self):
        if self.serial_worker:
            self.serial_worker.stop()
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
