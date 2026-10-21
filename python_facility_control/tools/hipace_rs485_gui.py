"""HiPace RS-485 test panel - Turbos 2 and 3 of the VC100 (medium) facility.

Independent of the facility control program.  Connects to one or two Pfeiffer HiPace / TC 400 over
RS-485 (one shared bus with two addresses, or one COM port each), monitors them live, runs the
bring-up check list, and - only with "Enable control" switched on - takes RS-485 control and
starts / stops / standbys / acknowledges.  All protocol work is in hipace_rs485.py.

    python tools\\hipace_rs485_gui.py                 # pick ports in the window
    python tools\\hipace_rs485_gui.py --sim           # both turbos on the simulator (SIM1, addr 1 + 2)
    tools\\hipace_rs485.bat                            # same, with the facility's Python environment

Read the SAFETY note at the top of hipace_rs485.py before enabling control.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import queue
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hipace_rs485 as hp  # noqa: E402

from PySide6.QtCore import QThread, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,  # noqa: E402
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

try:
    import pyqtgraph as pg
except Exception:                       # the panel works without the plot
    pg = None

PUMP_NAMES = ("Turbo 2", "Turbo 3")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

STATUS_COLOURS = {"Normal": "#1e8e3e", "Accel": "#c77700", "Standby": "#c77700",
                  "Standby (accel)": "#c77700", "Decel": "#c77700", "Warning": "#c77700",
                  "Error": "#c5221f", "No comms": "#c5221f", "Off": "#5f6368"}
RESULT_COLOURS = {"PASS": "#1e8e3e", "WARN": "#c77700", "FAIL": "#c5221f", "INFO": "#1a73e8",
                  "SKIP": "#5f6368"}


# ---------------------------------------------------------------------------- worker
class BusWorker(QThread):
    """Owns one PVBus; polls its pumps and runs queued commands between polls."""

    snapshot = Signal(str, object)          # pump name, hp.Snapshot
    identified = Signal(str, object)
    traffic = Signal(str)
    message = Signal(str, str)              # level (info / warn / error), text
    validation = Signal(str, str, str)      # pump name, result, line

    def __init__(self, bus: hp.PVBus, pumps: List[hp.HiPace], interval_s: float):
        super().__init__()
        self.bus, self.pumps, self.interval_s = bus, pumps, interval_s
        self.jobs: "queue.Queue" = queue.Queue()
        self._stop = False
        self.paused = False
        self.log_polling = False
        self._in_job = False
        bus.log = self._on_traffic

    def _on_traffic(self, direction: str, text: str) -> None:
        if self.log_polling or self._in_job or direction == "--":
            self.traffic.emit(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} "
                              f"{self.bus.port:6s} {direction} {text}")

    def submit(self, pump: hp.HiPace, label: str, fn) -> None:
        self.jobs.put((pump, label, fn))

    def stop(self) -> None:
        self._stop = True

    def _run_jobs(self) -> None:
        while True:
            try:
                pump, label, fn = self.jobs.get_nowait()
            except queue.Empty:
                return
            self._in_job = True
            try:
                result = fn(pump)
                self.message.emit("info", f"{pump.name}: {label}" + (f" - {result}" if result else ""))
            except hp.ControlNotEnabled as exc:
                self.message.emit("warn", f"{pump.name}: {label} - {exc}")
            except Exception as exc:
                self.message.emit("error", f"{pump.name}: {label} FAILED - {exc}")
            finally:
                self._in_job = False

    def run(self) -> None:
        for p in self.pumps:
            try:
                ident = p.identify()
                self.identified.emit(p.name, dict(ident))
            except Exception as exc:
                self.message.emit("error", f"{p.name}: no identification - {exc}")
        while not self._stop:
            t0 = time.monotonic()
            self._run_jobs()
            if not self.paused:
                for p in self.pumps:
                    if self._stop:
                        break
                    try:
                        if not p.ident.get(349):
                            p.identify()
                            self.identified.emit(p.name, dict(p.ident))
                    except Exception:
                        pass
                    snap = p.snapshot()
                    self.snapshot.emit(p.name, snap)
                    self._run_jobs()          # a STOP pressed mid-poll goes out at once
            while not self._stop and time.monotonic() - t0 < self.interval_s:
                if not self.jobs.empty():
                    break
                time.sleep(0.02)


# ---------------------------------------------------------------------------- pump panel
class PumpPanel(QGroupBox):
    def __init__(self, name: str, window: "MainWindow"):
        super().__init__(name)
        self.name, self.win = name, window
        self.pump: Optional[hp.HiPace] = None
        v = QVBoxLayout(self)

        self.status = QLabel("not connected")
        f = QFont()
        f.setPointSize(16)
        f.setBold(True)
        self.status.setFont(f)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setMinimumHeight(40)
        self._set_status("not connected", "#5f6368")
        v.addWidget(self.status)

        self.speed = QLabel("—")
        fs = QFont()
        fs.setPointSize(13)
        self.speed.setFont(fs)
        self.speed.setAlignment(Qt.AlignCenter)
        v.addWidget(self.speed)

        g = QGridLayout()
        self.fields: Dict[str, QLabel] = {}
        rows = [("ident", "Unit"), ("address", "Address"), ("ctrl", "Control via"), ("prio", "Remote priority"),
                ("flags", "Station / motor / standby"), ("error", "Error code"), ("drive", "Drive"),
                ("temps", "Temp elec / bottom / bearing / motor"), ("standby", "Standby speed"),
                ("hours", "Hours pump / electronics"), ("link", "Link")]
        for i, (key, label) in enumerate(rows):
            g.addWidget(QLabel(label + ":"), i, 0)
            val = QLabel("—")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            g.addWidget(val, i, 1)
            self.fields[key] = val
        g.setColumnStretch(1, 1)
        v.addLayout(g)

        b = QGridLayout()
        self.btn_take = QPushButton("Take RS-485 control")
        self.btn_take.setToolTip("Write [P:060] CtrlViaInt = 2.  The value found is restored on exit.")
        self.btn_start = QPushButton("Start (pumping station ON)")
        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setStyleSheet("QPushButton{background:#c5221f;color:white;font-weight:bold}")
        self.btn_stop.setToolTip("Pumping station OFF - always allowed, even with control disabled")
        self.btn_sby_on = QPushButton("Standby ON")
        self.btn_sby_off = QPushButton("Standby OFF")
        self.btn_ack = QPushButton("Acknowledge error")
        self.btn_validate = QPushButton("Run validation")
        b.addWidget(self.btn_take, 0, 0, 1, 2)
        b.addWidget(self.btn_start, 1, 0)
        b.addWidget(self.btn_stop, 1, 1)
        b.addWidget(self.btn_sby_on, 2, 0)
        b.addWidget(self.btn_sby_off, 2, 1)
        b.addWidget(self.btn_ack, 3, 0)
        b.addWidget(self.btn_validate, 3, 1)
        v.addLayout(b)
        v.addStretch(1)

        self.btn_take.clicked.connect(lambda: self._job("take RS-485 control", self._take))
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(lambda: self._job("STOP", lambda p: p.stop()))
        self.btn_sby_on.clicked.connect(lambda: self._job("standby ON", lambda p: p.set_standby(True)))
        self.btn_sby_off.clicked.connect(lambda: self._job("standby OFF", lambda p: p.set_standby(False)))
        self.btn_ack.clicked.connect(lambda: self._job("error acknowledge", lambda p: p.ack_error()))
        self.btn_validate.clicked.connect(lambda: self.win.run_validation(self.name))
        self.update_buttons()

    @staticmethod
    def _take(p: hp.HiPace) -> str:
        p.take_rs485_control()
        was = p.original_ctrl
        return "[P:060] = 2 (RS-485)" + (f", was {hp.ctrl_text(was)}" if was is not None else ", already set")

    def _set_status(self, text: str, colour: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"QLabel{{background:{colour};color:white;border-radius:6px;padding:4px}}")

    def _job(self, label: str, fn) -> None:
        self.win.submit(self.name, label, fn)

    def _start(self) -> None:
        r = QMessageBox.warning(
            self, f"Start {self.name}?",
            f"{self.name} will spin up.\n\nConfirm before pressing Yes:\n"
            "  • the backing (primary) pump is running,\n"
            "  • the foreline / turbo gauge is below the hot-start limit (facility: 5 mBar),\n"
            "  • the turbo valve and gate are in the state you intend,\n"
            "  • the facility program is not commanding this turbo.\n\n"
            "Start it?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r == QMessageBox.Yes:
            self._job("START (pumping station ON)", lambda p: p.start())

    def update_buttons(self) -> None:
        connected = self.pump is not None
        control = connected and self.win.control_enabled
        for btn in (self.btn_take, self.btn_start, self.btn_sby_on, self.btn_sby_off, self.btn_ack):
            btn.setEnabled(control)
        self.btn_stop.setEnabled(connected)
        self.btn_validate.setEnabled(connected)

    def attach(self, pump: Optional[hp.HiPace]) -> None:
        self.pump = pump
        if pump is None:
            self._set_status("not connected", "#5f6368")
            self.speed.setText("—")
            for f in self.fields.values():
                f.setText("—")
        else:
            self._set_status("connecting…", "#5f6368")
            self.fields["address"].setText(f"{pump.address} on {pump.bus.port}")
        self.update_buttons()

    def show_ident(self, ident: dict) -> None:
        self.fields["ident"].setText(f"{ident.get(349) or '?'}   fw {ident.get(312) or '?'}   "
                                     f"hw {ident.get(354) or '?'}   nominal {ident.get(315) or '?'} Hz")
        adr = ident.get(797)
        if self.pump is not None:
            mismatch = adr is not None and adr != self.pump.address
            self.fields["address"].setText(f"{self.pump.address} on {self.pump.bus.port}"
                                           + (f"   !! [P:797] reads {adr}" if mismatch else
                                              f"   ([P:797] = {adr})" if adr is not None else ""))

    def show_snapshot(self, s: hp.Snapshot) -> None:
        p = self.pump
        if p is None:
            return
        word = s.status(p.nominal_hz)
        self._set_status(word, STATUS_COLOURS.get(word, "#5f6368"))
        pct = s.speed_pct(p.nominal_hz)
        hz = s.get(309)
        self.speed.setText("—" if hz is None else
                           f"{hz} Hz   {'' if pct is None else f'{pct:.1f} %'}   "
                           f"(set {s.get(308, '?')} Hz)")

        def yn(v):
            return "?" if v is None else ("ON" if v else "off")
        ctrl = s.get(60)
        ctrl_ok = ctrl is not None and ctrl != 255 and int(ctrl) & hp.CTRL_RS485
        self.fields["ctrl"].setText(f"{hp.ctrl_text(ctrl)}" + ("" if ctrl is None else f"  ([P:060] = {ctrl})")
                                    + ("" if ctrl_ok or ctrl is None else "  – monitor only")
                                    + ("  locked" if s.get(61) else ""))
        self.fields["ctrl"].setStyleSheet("" if ctrl_ok or ctrl is None else "color:#c77700")
        prio = s.get(300)
        self.fields["prio"].setText("?" if prio is None else ("ACTIVE – overrides RS-485" if prio else "off"))
        self.fields["prio"].setStyleSheet("color:#c5221f;font-weight:bold" if prio else "")
        self.fields["flags"].setText(f"{yn(s.get(10))} / {yn(s.get(23))} / {yn(s.get(2))}"
                                     + ("   accelerating" if s.get(307) else "")
                                     + ("   at set speed" if s.get(306) else ""))
        err = s.get(303)
        kind = hp.error_kind(err)
        self.fields["error"].setText(str(err) if err is not None else "?")
        self.fields["error"].setStyleSheet({"error": "color:#c5221f;font-weight:bold",
                                            "warning": "color:#c77700"}.get(kind, ""))
        self.fields["drive"].setText(f"{s.get(310, '?')} A   {s.get(313, '?')} V   {s.get(316, '?')} W")
        hot = s.get(304) or s.get(305)
        self.fields["temps"].setText(" / ".join(str(s.get(n, "?")) for n in (326, 330, 342, 346)) + " °C"
                                     + ("   !! EXCESS TEMPERATURE" if hot else ""))
        self.fields["temps"].setStyleSheet("color:#c5221f" if hot else "")
        self.fields["standby"].setText(f"{s.get(717, '?')} %")
        self.fields["hours"].setText(f"{s.get(311, '?')} h / {s.get(314, '?')} h")
        st = p.stats
        lost = st.consecutive_failures >= 2 or not s.comm_ok
        self.fields["link"].setText(("LOST – " if lost else "OK – ") + st.summary())
        self.fields["link"].setStyleSheet("color:#c5221f;font-weight:bold" if lost else "")


# ---------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    param_values = Signal(object)           # {param: value}, emitted from the worker threads
    address_changed = Signal(str, int)

    def __init__(self, args):
        super().__init__()
        self.setWindowTitle("HiPace RS-485 test panel – VC100 Turbos 2 and 3")
        self.resize(1250, 900)
        self.control_enabled = False
        self.workers: Dict[str, BusWorker] = {}           # port -> worker
        self.pumps: Dict[str, hp.HiPace] = {}             # name -> pump
        self.worker_of: Dict[str, BusWorker] = {}         # name -> worker
        self.t0 = time.time()
        self.history: Dict[str, List] = {n: [[], []] for n in PUMP_NAMES}
        self.csv_file = None
        self.csv_writer = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # -- connection
        conn = QGroupBox("Connection  (9600 8N1 – Pfeiffer Vacuum protocol)")
        cg = QGridLayout(conn)
        cg.addWidget(QLabel("Use"), 0, 0)
        cg.addWidget(QLabel("Serial port"), 0, 1)
        cg.addWidget(QLabel("RS-485 address [P:797]"), 0, 2)
        self.row_use: Dict[str, QCheckBox] = {}
        self.row_port: Dict[str, QComboBox] = {}
        self.row_addr: Dict[str, QSpinBox] = {}
        defaults = {"Turbo 2": (args.t2_port, args.t2_address), "Turbo 3": (args.t3_port, args.t3_address)}
        for i, name in enumerate(PUMP_NAMES, start=1):
            use = QCheckBox(name)
            use.setChecked(True)
            port = QComboBox()
            port.setEditable(True)
            port.setMinimumWidth(320)
            addr = QSpinBox()
            addr.setRange(1, 255)
            addr.setValue(defaults[name][1])
            self.row_use[name], self.row_port[name], self.row_addr[name] = use, port, addr
            scan = QPushButton("Scan this port")
            scan.clicked.connect(lambda _=False, n=name: self.scan(n))
            cg.addWidget(use, i, 0)
            cg.addWidget(port, i, 1)
            cg.addWidget(addr, i, 2)
            cg.addWidget(scan, i, 3)
        self.refresh_ports(defaults)
        self.btn_refresh = QPushButton("Refresh ports")
        self.btn_refresh.clicked.connect(lambda: self.refresh_ports())
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setStyleSheet("QPushButton{font-weight:bold}")
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.2, 10.0)
        self.interval.setSingleStep(0.5)
        self.interval.setValue(args.interval)
        self.interval.setSuffix(" s poll")
        self.chk_csv = QCheckBox("Record CSV (logs/)")
        cg.addWidget(self.btn_refresh, 1, 4)
        cg.addWidget(self.btn_connect, 2, 4)
        cg.addWidget(self.interval, 1, 5)
        cg.addWidget(self.chk_csv, 2, 5)
        hint = QLabel("Same port + different addresses = shared bus (one pump must be moved off factory "
                      "address 1 first).  Different ports = one bus each.  SIM1 / SIM2 = simulator.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#5f6368")
        cg.addWidget(hint, 3, 0, 1, 6)
        root.addWidget(conn)

        # -- control bar
        bar = QHBoxLayout()
        self.chk_control = QCheckBox("Enable control (writes to the pumps)")
        self.chk_control.setStyleSheet("QCheckBox{font-weight:bold;color:#c5221f}")
        self.chk_control.toggled.connect(self.set_control)
        self.btn_stop_all = QPushButton("STOP ALL TURBOS")
        self.btn_stop_all.setMinimumHeight(36)
        self.btn_stop_all.setStyleSheet("QPushButton{background:#c5221f;color:white;font-weight:bold;"
                                        "font-size:14px;padding:0 24px}")
        self.btn_stop_all.clicked.connect(self.stop_all)
        bar.addWidget(self.chk_control)
        bar.addStretch(1)
        bar.addWidget(self.btn_stop_all)
        root.addLayout(bar)

        # -- pump panels
        panels = QHBoxLayout()
        self.panels = {n: PumpPanel(n, self) for n in PUMP_NAMES}
        for p in self.panels.values():
            panels.addWidget(p)
        root.addLayout(panels, 3)

        # -- tabs
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 2)
        self._build_plot_tab()
        self._build_validation_tab()
        self._build_param_tab()
        self._build_traffic_tab()
        self._build_events_tab()

        self.param_values.connect(lambda d: [self._show_param(n, v) for n, v in d.items()])
        self.address_changed.connect(lambda n, a: self.row_addr[n].setValue(a))
        self.statusBar().showMessage("Not connected.  Read the safety note in hipace_rs485.py before enabling control.")
        self.update_controls()
        if args.connect:
            QTimer.singleShot(200, self.toggle_connect)

    # ------------------------------------------------------------------ tabs
    def _build_plot_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        if pg is None:
            lay.addWidget(QLabel("pyqtgraph not installed – no plot"))
            self.plot = None
        else:
            pg.setConfigOptions(antialias=True)
            self.plot = pg.PlotWidget()
            self.plot.setBackground("w")
            self.plot.showGrid(x=True, y=True, alpha=0.25)
            self.plot.setLabel("left", "Rotation speed", units="%")
            self.plot.setLabel("bottom", "Time since connect", units="s")
            self.plot.setYRange(0, 105)
            self.plot.addLegend()
            self.curves = {"Turbo 2": self.plot.plot(name="Turbo 2", pen=pg.mkPen("#1a73e8", width=2)),
                           "Turbo 3": self.plot.plot(name="Turbo 3", pen=pg.mkPen("#e8710a", width=2))}
            lay.addWidget(self.plot)
        self.tabs.addTab(w, "Speed")

    def _build_validation_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.val_pings = QSpinBox()
        self.val_pings.setRange(10, 2000)
        self.val_pings.setValue(100)
        self.val_pings.setPrefix("link test: ")
        self.val_pings.setSuffix(" telegrams")
        self.val_write = QCheckBox("include harmless write round trip ([P:717] written with its own value)")
        btn_both = QPushButton("Validate all connected")
        btn_both.clicked.connect(lambda: [self.run_validation(n) for n in self.pumps])
        btn_clear = QPushButton("Clear")
        top.addWidget(self.val_pings)
        top.addWidget(self.val_write)
        top.addStretch(1)
        top.addWidget(btn_both)
        top.addWidget(btn_clear)
        lay.addLayout(top)
        self.val_table = QTableWidget(0, 4)
        self.val_table.setHorizontalHeaderLabels(["Pump", "Result", "Check", "Detail"])
        self.val_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.val_table.verticalHeader().setVisible(False)
        btn_clear.clicked.connect(lambda: self.val_table.setRowCount(0))
        lay.addWidget(self.val_table)
        self.tabs.addTab(w, "Validation")

    def _build_param_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.par_pump = QComboBox()
        self.par_pump.addItems(PUMP_NAMES)
        self.par_num = QSpinBox()
        self.par_num.setRange(0, 999)
        self.par_num.setValue(309)
        self.par_num.setPrefix("[P:")
        self.par_num.setSuffix("]")
        btn_read = QPushButton("Read")
        btn_read.clicked.connect(self.param_read)
        self.par_value = QLineEdit()
        self.par_value.setPlaceholderText("value (typed, e.g. 66.7, 1, 0) – or raw data with 'raw:' prefix")
        self.btn_par_write = QPushButton("Write")
        self.btn_par_write.clicked.connect(self.param_write)
        btn_all = QPushButton("Read all known")
        btn_all.clicked.connect(self.param_read_all)
        self.btn_addr = QPushButton("Change RS-485 address…")
        self.btn_addr.clicked.connect(self.change_address)
        for x in (QLabel("Pump"), self.par_pump, self.par_num, btn_read, self.par_value, self.btn_par_write,
                  btn_all, self.btn_addr):
            row.addWidget(x)
        lay.addLayout(row)
        self.par_table = QTableWidget(0, 6)
        self.par_table.setHorizontalHeaderLabels(["Param", "Name", "Value", "Unit", "Access", "Description"])
        self.par_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.par_table.verticalHeader().setVisible(False)
        self.par_table.setRowCount(len(hp.PARAMS))
        self.par_rows = {}
        for i, (n, p) in enumerate(hp.PARAMS.items()):
            self.par_rows[n] = i
            for col, text in enumerate((f"{n:03d}", p.name, "", p.unit, p.access, p.text)):
                self.par_table.setItem(i, col, QTableWidgetItem(text))
        self.par_table.cellClicked.connect(lambda r, _c: self.par_num.setValue(int(self.par_table.item(r, 0).text())))
        lay.addWidget(self.par_table)
        self.tabs.addTab(w, "Parameters")

    def _build_traffic_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.chk_poll_traffic = QCheckBox("show polling traffic (otherwise only commands)")
        self.chk_poll_traffic.toggled.connect(self._set_poll_traffic)
        btn_save = QPushButton("Save…")
        btn_clear = QPushButton("Clear")
        row.addWidget(self.chk_poll_traffic)
        row.addStretch(1)
        row.addWidget(btn_save)
        row.addWidget(btn_clear)
        lay.addLayout(row)
        self.traffic = QPlainTextEdit()
        self.traffic.setReadOnly(True)
        self.traffic.setMaximumBlockCount(20000)
        self.traffic.setFont(QFont("Consolas", 9))
        lay.addWidget(self.traffic)
        btn_clear.clicked.connect(self.traffic.clear)
        btn_save.clicked.connect(lambda: self._save_text(self.traffic, "hipace_rs485_traffic"))
        self.tabs.addTab(w, "Traffic")

    def _build_events_tab(self):
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumBlockCount(5000)
        self.tabs.addTab(self.events, "Events")

    def _save_text(self, widget: QPlainTextEdit, stem: str):
        LOG_DIR.mkdir(exist_ok=True)
        default = str(LOG_DIR / f"{stem}_{_dt.datetime.now():%Y-%m-%d_%H%M%S}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Save", default, "Text (*.txt)")
        if path:
            Path(path).write_text(widget.toPlainText(), encoding="utf-8")

    # ------------------------------------------------------------------ ports
    def refresh_ports(self, defaults=None):
        ports = hp.list_ports()
        for name in PUMP_NAMES:
            combo = self.row_port[name]
            current = (defaults[name][0] if defaults and defaults[name][0] else combo.currentText())
            combo.clear()
            for dev, desc in ports:
                combo.addItem(f"{dev}  –  {desc}" if desc else dev, dev)
            if current:
                idx = combo.findData(current)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.setEditText(current)

    def port_of(self, name: str) -> str:
        combo = self.row_port[name]
        i = combo.currentIndex()
        if i >= 0 and combo.currentText() == combo.itemText(i):
            return combo.itemData(i)
        return combo.currentText().split()[0] if combo.currentText().strip() else ""

    def scan(self, name: str):
        if self.workers:
            QMessageBox.information(self, "Scan", "Disconnect first – the port is in use.")
            return
        port = self.port_of(name)
        n, ok = QInputDialog.getInt(self, "Scan", f"Scan {port} for addresses 1 … N:", 16, 1, 255)
        if not ok:
            return
        self.statusBar().showMessage(f"scanning {port} …")
        QApplication.processEvents()
        try:
            bus = hp.PVBus(port, timeout=0.15)
        except Exception as exc:
            QMessageBox.critical(self, "Scan", f"cannot open {port}: {exc}")
            return
        try:
            found = bus.scan(range(1, n + 1),
                             progress=lambda a: (self.statusBar().showMessage(f"scanning {port}: address {a}"),
                                                 QApplication.processEvents()))
        finally:
            bus.close()
        if found:
            text = "\n".join(f"address {a}: {nm}" for a, nm in found)
            if len(found) == 1 and found[0][0] == 1:
                text += "\n\nOnly the factory address 1 answers.  For a shared bus, change one pump's " \
                        "address (Parameters tab) with only that pump connected."
        else:
            text = "Nothing answered.\n\nCheck: correct COM port, pump powered, D+/D- not swapped, " \
                   "termination, cable on the RS-485 connector of the TC 400."
        self.log_event("info", f"scan {port}: " + (", ".join(f"{a}={nm}" for a, nm in found) or "nothing"))
        self.statusBar().showMessage(f"scan of {port} done")
        QMessageBox.information(self, f"Scan {port}", text)

    # ------------------------------------------------------------------ connect
    def toggle_connect(self):
        if self.workers:
            self.disconnect_all(ask=True)
        else:
            self.connect_all()

    def connect_all(self):
        wanted = [(n, self.port_of(n), self.row_addr[n].value()) for n in PUMP_NAMES if self.row_use[n].isChecked()]
        if not wanted:
            return
        seen = {}
        for n, port, addr in wanted:
            if (port.upper(), addr) in seen:
                QMessageBox.critical(self, "Connect", f"{n} and {seen[(port.upper(), addr)]} are both set to "
                                     f"address {addr} on {port}.  On a shared bus every pump needs its own address.")
                return
            seen[(port.upper(), addr)] = n
        by_port: Dict[str, List] = {}
        for n, port, addr in wanted:
            by_port.setdefault(port.upper() if port.upper().startswith("SIM") else port, []).append((n, addr))
        for port, members in by_port.items():
            try:
                bus = hp.PVBus(port, timeout=0.3)
            except Exception as exc:
                self.log_event("error", f"cannot open {port}: {exc}")
                QMessageBox.critical(self, "Connect", f"cannot open {port}:\n{exc}\n\n"
                                     "Is the facility program or another tool using it?")
                continue
            pumps = []
            for n, addr in members:
                p = hp.HiPace(bus, addr, n)
                p.control_enabled = self.control_enabled
                self.pumps[n] = p
                pumps.append(p)
                self.panels[n].attach(p)
            w = BusWorker(bus, pumps, self.interval.value())
            w.log_polling = self.chk_poll_traffic.isChecked()
            w.snapshot.connect(self.on_snapshot)
            w.identified.connect(lambda n, ident: self.panels[n].show_ident(ident))
            w.traffic.connect(self.traffic.appendPlainText)
            w.message.connect(self.log_event)
            w.validation.connect(self.add_validation_row)
            self.workers[port] = w
            for p in pumps:
                self.worker_of[p.name] = w
            w.start()
            self.log_event("info", f"connected {port}: " + ", ".join(f"{n} @ {a}" for n, a in members))
        if self.workers:
            self.t0 = time.time()
            self.history = {n: [[], []] for n in PUMP_NAMES}
            if self.chk_csv.isChecked():
                self._open_csv()
        self.update_controls()

    def disconnect_all(self, ask: bool) -> bool:
        """Stop polling and leave the pumps safely.  Returns False if the user cancelled."""
        stop, restore = True, True
        touched = [p for p in self.pumps.values() if p.started_by_tool or p.original_ctrl is not None]
        if ask and touched:
            lines = []
            for p in touched:
                if p.started_by_tool:
                    lines.append(f"  • {p.name} was started by this tool")
                if p.original_ctrl is not None:
                    lines.append(f"  • {p.name}: [P:060] was changed from {hp.ctrl_text(p.original_ctrl)} to RS-485")
            box = QMessageBox(QMessageBox.Question, "Leave the pumps safely",
                              "\n".join(lines) + "\n\nStop the pumps this tool started and restore the "
                              "control interface it found?", parent=self)
            yes = box.addButton("Stop + restore (recommended)", QMessageBox.AcceptRole)
            keep = box.addButton("Leave everything as it is", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(yes)
            box.exec()
            if box.clickedButton() is keep:
                stop = restore = False
            elif box.clickedButton() is not yes:
                return False
        for w in self.workers.values():
            w.stop()
        for w in self.workers.values():
            w.wait(5000)
        for p in self.pumps.values():
            for m in p.safe_exit(stop=stop, restore=restore):
                self.log_event("error" if m.startswith("!!") else "info", m)
        for w in self.workers.values():
            w.bus.close()
        self.workers.clear()
        self.worker_of.clear()
        self.pumps.clear()
        for panel in self.panels.values():
            panel.attach(None)
        self._close_csv()
        self.update_controls()
        return True

    def update_controls(self):
        connected = bool(self.workers)
        self.btn_connect.setText("Disconnect" if connected else "Connect")
        for n in PUMP_NAMES:
            for w in (self.row_use[n], self.row_port[n], self.row_addr[n]):
                w.setEnabled(not connected)
        self.btn_refresh.setEnabled(not connected)
        self.interval.setEnabled(not connected)
        self.chk_csv.setEnabled(not connected)
        self.btn_stop_all.setEnabled(connected)
        self.btn_par_write.setEnabled(connected and self.control_enabled)
        self.btn_addr.setEnabled(connected and self.control_enabled)
        for p in self.panels.values():
            p.update_buttons()
        if connected:
            self.statusBar().showMessage(("CONTROL ENABLED – " if self.control_enabled else "monitoring (read-only) – ")
                                         + ", ".join(f"{p.name} @ {p.address} on {p.bus.port}" for p in self.pumps.values()))

    # ------------------------------------------------------------------ control
    def set_control(self, on: bool):
        if on and not self.control_enabled:
            r = QMessageBox.warning(
                self, "Enable control?",
                "This allows writes to the turbos: taking RS-485 control ([P:060] = 2), start, standby, "
                "error acknowledge and parameter writes.\n\n"
                "While RS-485 has control, the facility's D-Sub Motor / Standby lines no longer command "
                "these pumps.  Use STOP / STOP ALL or the pump's mains to stop them.\n\n"
                "Make sure the facility program is not running these turbos.  Enable control?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                self.chk_control.blockSignals(True)
                self.chk_control.setChecked(False)
                self.chk_control.blockSignals(False)
                return
        self.control_enabled = on
        for p in self.pumps.values():
            p.control_enabled = on
        self.log_event("warn" if on else "info", "control ENABLED" if on else "control disabled (read-only)")
        self.update_controls()

    def submit(self, name: str, label: str, fn):
        p, w = self.pumps.get(name), self.worker_of.get(name)
        if p is None or w is None:
            return
        w.submit(p, label, fn)

    def stop_all(self):
        for name in self.pumps:
            self.submit(name, "STOP ALL", lambda p: p.stop())

    # ------------------------------------------------------------------ data
    def on_snapshot(self, name: str, snap: hp.Snapshot):
        self.panels[name].show_snapshot(snap)
        p = self.pumps.get(name)
        if p is None:
            return
        pct = snap.speed_pct(p.nominal_hz)
        if pct is not None and self.plot is not None:
            xs, ys = self.history[name]
            xs.append(snap.time - self.t0)
            ys.append(pct)
            if len(xs) > 20000:
                del xs[:5000], ys[:5000]
            self.curves[name].setData(xs, ys)
        if self.csv_writer is not None:
            st = p.stats
            self.csv_writer.writerow([
                _dt.datetime.fromtimestamp(snap.time).isoformat(timespec="milliseconds"), name, p.address,
                snap.status(p.nominal_hz), snap.get(309), "" if pct is None else f"{pct:.1f}",
                snap.get(310), snap.get(316), snap.get(326), snap.get(330), snap.get(342), snap.get(346),
                snap.get(303), snap.get(10), snap.get(2), snap.get(60), snap.get(300),
                st.ok, st.timeouts, st.checksum_errors, f"{st.last_ms:.0f}"])
            self.csv_file.flush()

    def _open_csv(self):
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"hipace_rs485_{_dt.date.today():%Y-%m-%d}.csv"
        new = not path.exists()
        self.csv_file = open(path, "a", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        if new:
            self.csv_writer.writerow(["time", "pump", "address", "status", "speed_hz", "speed_pct", "current_a",
                                      "power_w", "temp_elec_c", "temp_bottom_c", "temp_bearing_c", "temp_motor_c",
                                      "error_code", "pumping_station", "standby", "ctrl_via_int", "remote_prio",
                                      "replies_ok", "timeouts", "checksum_errors", "latency_ms"])
        self.log_event("info", f"recording to {path}")

    def _close_csv(self):
        if self.csv_file:
            self.csv_file.close()
        self.csv_file = self.csv_writer = None

    def log_event(self, level: str, text: str):
        line = f"{time.strftime('%H:%M:%S')}  {level.upper():5s}  {text}"
        self.events.appendPlainText(line)
        self.statusBar().showMessage(text, 8000)
        if level == "error":
            self.tabs.setCurrentWidget(self.events)

    # ------------------------------------------------------------------ validation
    def run_validation(self, name: str):
        pings, write_test = self.val_pings.value(), self.val_write.isChecked()
        w = self.worker_of.get(name)
        if w is None:
            return
        self.tabs.setCurrentIndex(1)

        def job(p: hp.HiPace):
            def progress(line: str):
                res = line[:4].strip()
                w.validation.emit(p.name, res, line[6:])
            checks = hp.validate(p, pings=pings, write_test=write_test, progress=progress)
            return f"validation {checks[-1].result}"
        w.submit(self.pumps[name], "validation", job)

    def add_validation_row(self, name: str, result: str, line: str):
        check, _, detail = line.partition(": ")
        r = self.val_table.rowCount()
        self.val_table.insertRow(r)
        for col, text in enumerate((name, result, check, detail)):
            item = QTableWidgetItem(text)
            if col == 1:
                item.setForeground(QColor("white"))
                item.setBackground(QColor(RESULT_COLOURS.get(result, "#5f6368")))
                item.setTextAlignment(Qt.AlignCenter)
            if check == "Overall":
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.val_table.setItem(r, col, item)
        self.val_table.resizeColumnsToContents()
        self.val_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.val_table.scrollToBottom()

    # ------------------------------------------------------------------ parameters
    def _par_pump(self) -> Optional[hp.HiPace]:
        p = self.pumps.get(self.par_pump.currentText())
        if p is None:
            QMessageBox.information(self, "Parameters", f"{self.par_pump.currentText()} is not connected.")
        return p

    def _show_param(self, n: int, value):
        if n in self.par_rows:
            self.par_table.item(self.par_rows[n], 2).setText(str(value))

    def param_read(self):
        p = self._par_pump()
        if p is None:
            return
        n = self.par_num.value()

        def job(pump):
            raw = pump.read_raw(n)
            par = hp.PARAMS.get(n)
            val = hp.decode(par.dtype, raw) if par else raw
            self.param_values.emit({n: val})
            return f"[P:{n:03d}] {par.name if par else ''} = {val!r} {par.unit if par else ''}  (raw {raw!r})"
        self.submit(p.name, f"read [P:{n:03d}]", job)

    def param_read_all(self):
        p = self._par_pump()
        if p is None:
            return
        def job(pump):
            results = {}
            for n, par in hp.PARAMS.items():
                if par.access == "w":
                    continue
                try:
                    results[n] = pump.read(n)
                except hp.PVError as exc:
                    results[n] = f"-- {exc.code if isinstance(exc, hp.PVRejected) else type(exc).__name__}"
            self.param_values.emit(results)
            return f"{len(results)} parameters read"
        self.submit(p.name, "read all parameters", job)

    def param_write(self):
        p = self._par_pump()
        if p is None:
            return
        n = self.par_num.value()
        text = self.par_value.text().strip()
        if not text:
            return
        par = hp.PARAMS.get(n)
        raw = text.startswith("raw:") or par is None
        if raw:
            value = text[4:] if text.startswith("raw:") else text
        elif par.dtype in (hp.DT.BOOL_OLD, hp.DT.BOOL_NEW):
            value = text.lower() in ("1", "on", "true", "yes")
        else:
            try:
                value = float(text) if par.dtype in (hp.DT.U_REAL, hp.DT.U_EXPO) else int(text)
            except ValueError:
                QMessageBox.warning(self, "Write", f"{text!r} is not a number")
                return
        if n in (10, 23) and value:
            QMessageBox.information(self, "Write", "Use the Start button – it asks for the start conditions.")
            return
        if n == 797:
            QMessageBox.information(self, "Write", "Use 'Change RS-485 address…' for [P:797].")
            return
        label = f"[P:{n:03d}] {par.name if par else ''}"
        r = QMessageBox.question(self, "Write parameter", f"Write {value!r} to {label} on {p.name}?")
        if r != QMessageBox.Yes:
            return
        self.submit(p.name, f"write {label} = {value!r}", lambda pump: f"echo {pump.write(n, value, raw=raw)!r}")

    def change_address(self):
        p = self._par_pump()
        if p is None:
            return
        new, ok = QInputDialog.getInt(
            self, "Change RS-485 address",
            f"{p.name} is at address {p.address}.\n\nOnly this pump may be connected to the bus while its "
            f"address is changed (two pumps answering on the same address garble each other).\n\n"
            f"New address [P:797]:", 2 if p.address == 1 else p.address, 1, 255)
        if not ok or new == p.address:
            return

        def job(pump):
            pump.set_address(new)
            self.address_changed.emit(pump.name, new)
            return f"address is now {new} (set it in the Connection row next time)"
        self.submit(p.name, f"change address {p.address} -> {new}", job)

    def _set_poll_traffic(self, on: bool):
        for w in self.workers.values():
            w.log_polling = on

    # ------------------------------------------------------------------ exit
    def closeEvent(self, ev):
        if self.workers and not self.disconnect_all(ask=True):
            ev.ignore()
            return
        ev.accept()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="both turbos on the simulator (SIM1, addresses 1 and 2)")
    ap.add_argument("--t2-port", default=None)
    ap.add_argument("--t3-port", default=None)
    ap.add_argument("--t2-address", type=int, default=1)
    ap.add_argument("--t3-address", type=int, default=2)
    ap.add_argument("--interval", type=float, default=1.0, help="poll interval, s")
    ap.add_argument("--connect", action="store_true", help="connect at start-up")
    args = ap.parse_args(argv)
    if args.sim:
        args.t2_port = args.t2_port or "SIM1"
        args.t3_port = args.t3_port or "SIM1"
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(args)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
