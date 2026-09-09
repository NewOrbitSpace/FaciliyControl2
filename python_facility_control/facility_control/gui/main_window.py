"""Main window – the LabVIEW front panel re-created in Qt."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import QDateTime, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDoubleSpinBox, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
                               QSizePolicy, QSplitter, QVBoxLayout, QWidget)

from ..config import FacilityConfig
from ..controller import Controller
from ..model import Mode, Snapshot, TabPage
from ..units import UNITS, torr_to, to_torr, unit_label
from .plots import PlotsWidget
from .schematic import SchematicWidget
from .widgets import BUTTON_STYLE, LED, LV_BG, ErrorClusterWidget, ValueBox, lv_button

# Auto-mode buttons per tab page (VI 'Tab Control' pages) -> (label, normalised action)
AUTO_BUTTONS: Dict[TabPage, List[tuple]] = {
    TabPage.FACILITY_OFF: [("Pump to Rough", "pump_to_rough"), ("Pump to High Vac", "pump_to_high_vac"),
                           ("Overnight Pump", "overnight_pump"), ("Vent", "vent")],
    TabPage.PUMPING_TO_ROUGH: [("Overnight Pump", "overnight_pump"), ("Pump to Rough Only", "pump_to_rough"),
                               ("Pump to High", "pump_to_high_vac"), ("Vent", "vent"), ("Shut Off", "shut_off")],
    TabPage.HOLDING_AT_ROUGH: [],
    TabPage.OVERNIGHT_PUMP: [("Vent", "vent"), ("Pump to Rough", "pump_to_rough"), ("Pump to High Vac", "pump_to_high_vac")],
    TabPage.ENGAGING_TURBO: [],
    TabPage.PUMPING_TO_HIGH_VAC: [("Shutdown", "shutdown"), ("Pump to Rough", "pump_to_rough"), ("Overnight Pump", "overnight_pump"),
                                  ("Vent and shutdown", "vent_and_shutdown"), ("Vent", "vent")],
    TabPage.CLOSING_GATE: [],
    TabPage.VENTING: [("Overnight Pump", "overnight_pump"), ("Pump to Rough", "pump_to_rough"),
                      ("Pump to Hi vac", "pump_to_high_vac"), ("Shut Off", "shut_off")],
    TabPage.TURBO_SLOWING: [("Pump to High Vac", "pump_to_high_vac")],
}
TAB_TITLES = {TabPage.FACILITY_OFF: "Facility Off", TabPage.PUMPING_TO_ROUGH: "Pumping to Rough",
              TabPage.HOLDING_AT_ROUGH: "Holding at Rough", TabPage.OVERNIGHT_PUMP: "Overnight Pump",
              TabPage.ENGAGING_TURBO: "Engaging Turbo", TabPage.PUMPING_TO_HIGH_VAC: "Pumping to High Vac",
              TabPage.CLOSING_GATE: "Closing Gate", TabPage.VENTING: "Venting", TabPage.TURBO_SLOWING: "Turbo Slowing"}


class MainWindow(QMainWindow):
    def __init__(self, cfg: FacilityConfig, controller: Controller, sim_backend=None):
        super().__init__()
        self.cfg = cfg
        self.ctl = controller
        self.sim = sim_backend
        self.unit = cfg.pressure_unit_default
        self.setWindowTitle(f"{cfg.name} – Front Panel")
        # Auto buttons per page: the profile's `auto_buttons` block (VC100) or the Main_V4.4 pages
        self.auto_buttons_map: Dict[TabPage, List[tuple]] = dict(AUTO_BUTTONS)
        if cfg.auto_buttons:
            self.auto_buttons_map = {TabPage(page): list(items) for page, items in cfg.auto_buttons.items()}
            for page in TabPage:
                self.auto_buttons_map.setdefault(page, [])
        self.setStyleSheet(f"QMainWindow {{ background: {LV_BG}; }} QLabel {{ color: black; }} QGroupBox {{ font-weight: bold; }}")
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)
        self._last_iteration = -1
        self._closing = False
        self.resize(1440, 1040)

    # ------------------------------------------------------------------ layout
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        # ---- top: schematic | plots
        top = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.schematic = SchematicWidget(self.cfg)
        self.schematic.commandRequested.connect(self.ctl.request_user_cmd)
        lv.addWidget(self.schematic, 1)
        # row under the diagram: Running LED, STOP, Reset Turbos (as in the VC100 panel)
        row = QHBoxLayout()
        self.reset_turbos = lv_button("Reset\nTurbos", 70, 44)
        self.reset_turbos.clicked.connect(lambda: self.ctl.request_turbo_error_ack(None))
        row.addWidget(self.reset_turbos)
        row.addStretch(1)
        self.running_led = LED(20)
        row.addWidget(self.running_led)
        row.addWidget(QLabel("Running"))
        row.addSpacing(30)
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setStyleSheet(BUTTON_STYLE + "QPushButton { color: #C00000; font-weight: bold; font-size: 15px; min-width: 150px; min-height: 48px; }")
        self.stop_btn.clicked.connect(self._on_stop)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        lv.addLayout(row)
        top.addWidget(left)
        self.plots = PlotsWidget(self.cfg)
        top.addWidget(self.plots)
        top.setSizes([700, 740])
        root.addWidget(top, 7)

        # ---- bottom row: mode/auto | error cluster | event log
        bottom = QHBoxLayout()
        bottom.addWidget(self._build_mode_panel(), 2)
        errbox = QVBoxLayout()
        self.error_widget = ErrorClusterWidget()
        errbox.addWidget(self.error_widget)
        self.clear_error = lv_button("Clear Error", 110, 40)
        self.clear_error.clicked.connect(self.ctl.request_clear_error)
        errbox.addWidget(self.clear_error, alignment=Qt.AlignCenter)
        bottom.addLayout(errbox, 1)
        logbox = QVBoxLayout()
        logbox.addWidget(QLabel("Event log (temporary – cleared when the program stops)"))
        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setStyleSheet("background: #DCDCDC; border: 1px solid #909090; font-size: 12px;")
        self.event_log.setMinimumHeight(120)
        self.event_log.setMaximumHeight(220)
        logbox.addWidget(self.event_log, 1)
        tl = QHBoxLayout()
        tl.addWidget(QLabel("Current Time/date"))
        self.time_label = ValueBox("", width=180)
        tl.addWidget(self.time_label)
        tl.addStretch(1)
        self.hw_label = QLabel("")
        tl.addWidget(self.hw_label)
        logbox.addLayout(tl)
        bottom.addLayout(logbox, 2)
        root.addLayout(bottom, 0)
        if self.sim is not None:
            root.addWidget(self._build_sim_panel())

    def _build_mode_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.Panel)
        panel.setFrameShadow(QFrame.Raised)
        grid = QGridLayout(panel)
        grid.setContentsMargins(8, 6, 8, 6)
        # control level
        self.mode_label = QLabel("Initialising…")
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        self.mode_label.setFont(f)
        grid.addWidget(self.mode_label, 0, 0, 1, 3)
        self.hold_label = QLabel("")
        self.hold_label.setStyleSheet("QLabel { color: #8A4B00; font-weight: bold; }")
        self.hold_label.setMinimumHeight(18)
        grid.addWidget(self.hold_label, 7, 0, 1, 3)
        self.auto_mode_btn = lv_button("Auto Mode", 110)
        self.auto_mode_btn.setToolTip("Change to Auto – the current valve/pump configuration must match a known Auto state")
        self.auto_mode_btn.clicked.connect(self.ctl.request_change_to_auto)
        self.admin_mode_btn = lv_button("Admin Mode", 110)
        self.admin_mode_btn.setToolTip("Admin: full manual control, no interlocks (experienced users only)")
        self.admin_mode_btn.clicked.connect(self.ctl.request_admin_mode)
        self.manual_mode_btn = lv_button("Manual Mode", 110)
        self.manual_mode_btn.setToolTip("Manual: manual control with interlock checks")
        self.manual_mode_btn.clicked.connect(self.ctl.request_manual_mode)
        grid.addWidget(self.auto_mode_btn, 1, 0)
        grid.addWidget(self.admin_mode_btn, 1, 1)
        grid.addWidget(self.manual_mode_btn, 1, 2)
        # units + threshold + engage time
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Units"))
        self.unit_combo = QComboBox()
        for u in UNITS:
            self.unit_combo.addItem(unit_label(u), u)
        self.unit_combo.setCurrentIndex(UNITS.index(self.unit))
        self.unit_combo.currentIndexChanged.connect(self._on_unit)
        opts.addWidget(self.unit_combo)
        opts.addSpacing(10)
        self.threshold_label = QLabel(f"Turbo on Threshold ({unit_label(self.unit).lower()})")
        opts.addWidget(self.threshold_label)
        self.threshold = QDoubleSpinBox()
        self.threshold.setDecimals(4)
        self.threshold.setRange(1e-4, 1000.0)
        self.threshold.setSingleStep(0.01)
        self.threshold.setValue(torr_to(self.unit, self.cfg.thresholds.turbo_on_threshold_torr))
        self.threshold.valueChanged.connect(self._on_threshold)
        opts.addWidget(self.threshold)
        opts.addStretch(1)
        grid.addLayout(opts, 2, 0, 1, 3)
        eng = QHBoxLayout()
        eng.addWidget(QLabel("Turbo Engage time (Overnight Pump)"))
        self.engage_time = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600 * 8))
        self.engage_time.setDisplayFormat("dd/MM/yyyy HH:mm")
        self.engage_time.setCalendarPopup(True)
        self.engage_time.dateTimeChanged.connect(self._on_engage_time)
        eng.addWidget(self.engage_time)
        eng.addSpacing(16)
        # maintenance meter: total hours the primary pump has actually run (persists across restarts)
        eng.addWidget(QLabel("Primary pump total operating hours"))
        self.primary_hours_box = ValueBox("0.00", width=90)
        self.primary_hours_box.setToolTip("Total hours the primary pump has run, counted from its "
                                          "read-back and kept in the log folder across restarts")
        eng.addWidget(self.primary_hours_box)
        eng.addStretch(1)
        grid.addLayout(eng, 3, 0, 1, 3)
        self._on_engage_time(self.engage_time.dateTime())
        # state display + auto buttons (tab page)
        self.state_label = QLabel("")
        grid.addWidget(self.state_label, 4, 0, 1, 3)
        self.auto_group = QGroupBox("Auto commands")
        self.auto_layout = QHBoxLayout(self.auto_group)
        self.auto_buttons: List[QPushButton] = []
        self._current_tab: Optional[TabPage] = None
        grid.addWidget(self.auto_group, 5, 0, 1, 3)
        self.elapsed_label = QLabel("")
        grid.addWidget(self.elapsed_label, 6, 0, 1, 3)
        return panel

    def _build_sim_panel(self) -> QWidget:
        box = QGroupBox("Simulation (no DAQ connected) – fault injection")
        lay = QHBoxLayout(box)
        self.fault_boxes: Dict[str, QCheckBox] = {}
        faults = [("turbo_error", "Turbo error"), ("chiller_fault", "Chiller fault"), ("primary_fault", "Primary fault")]
        if any(t.control_mode == "shimadzu_contacts" for t in self.cfg.turbos):
            faults.insert(1, ("turbo_warning", "Turbo warning (contacts)"))
        if self.cfg.has_compressor:                      # only facilities with the air-pressure sensor
            faults.append(("compressor_low", "Compressor air low"))
        faults.append(("power_cut", "Power cut"))
        for vid, v in self.cfg.valves.items():
            faults.append((f"stuck_{vid}", f"{v.label} stuck"))
        faults.append((f"gauge_fault_{self.cfg.main_gauge.id}", "WRG fault"))
        for key, label in faults:
            cb = QCheckBox(label)
            cb.toggled.connect(lambda on, k=key: self.sim.set_fault(k, on))
            lay.addWidget(cb)
            self.fault_boxes[key] = cb
        lay.addStretch(1)
        lay.addWidget(QLabel("Plant time scale"))
        self.time_scale = QDoubleSpinBox()
        self.time_scale.setRange(0.1, 200.0)
        self.time_scale.setValue(self.cfg.simulation.time_scale)
        self.time_scale.valueChanged.connect(lambda v: setattr(self.sim.sim, "time_scale", float(v)))
        lay.addWidget(self.time_scale)
        return box

    # ------------------------------------------------------------------ slots
    def _on_unit(self, idx: int):
        self.unit = self.unit_combo.itemData(idx)
        self.plots.set_unit(self.unit)
        self.threshold_label.setText(f"Turbo on Threshold ({unit_label(self.unit).lower()})")
        self.threshold.blockSignals(True)
        self.threshold.setValue(torr_to(self.unit, self.ctl.snapshot().turbo_on_threshold_torr))
        self.threshold.blockSignals(False)
        self.ctl.request_set_unit(self.unit)
        self.plots.redraw()

    def _on_threshold(self, value: float):
        self.ctl.request_set_threshold(to_torr(self.unit, value))

    def _on_engage_time(self, qdt: QDateTime):
        self.ctl.request_set_engage_time(qdt.toSecsSinceEpoch())

    def _on_stop(self):
        if QMessageBox.question(self, "Stop", "Stop the facility control?\nAll valves close and all pumps switch off (safemode) before exit.",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self._closing = True
        self.stop_btn.setEnabled(False)
        self.ctl.request_stop()
        QTimer.singleShot(200, self._wait_stop)

    def _wait_stop(self):
        if self.ctl.finished.is_set():
            self.close()
        else:
            QTimer.singleShot(200, self._wait_stop)

    def closeEvent(self, ev):
        if not self.ctl.finished.is_set():
            self.ctl.request_stop()
            self.ctl.join(15.0)
        ev.accept()

    # ------------------------------------------------------------------ refresh
    def _poll(self):
        snap = self.ctl.snapshot()
        self.time_label.setText(datetime.now().strftime("%H:%M:%S       %d/%m/%Y"))
        new_iteration = snap.iteration != self._last_iteration
        self._last_iteration = snap.iteration
        self.schematic.set_snapshot(snap, self.unit)      # always: units/settle status may change between iterations
        if new_iteration:
            self.plots.add_snapshot(snap)
        self.running_led.setOn(snap.running_led)
        self.error_widget.set_error(snap.error)
        self.hw_label.setText(f"Hardware: {snap.hardware}")
        # event log
        txt = "\n".join(snap.event_log[-400:])
        if txt != self.event_log.toPlainText():
            self.event_log.setPlainText(txt)
            self.event_log.verticalScrollBar().setValue(self.event_log.verticalScrollBar().maximum())
        # mode
        mode_txt = {Mode.INITIALIZE: "Select Control Mode…", Mode.ADMIN: "Admin Control Mode",
                    Mode.MANUAL: "Manual Control Mode (interlocked)", Mode.AUTO: "Auto Control Mode"}[snap.mode]
        self.mode_label.setText(mode_txt)
        if snap.dialog_pending:
            self.hold_label.setText("waiting for your answer: " + snap.dialog_message.replace("\n", " ")
                                    + ("   (control loop paused)" if snap.loop_blocked else ""))
        elif snap.hold_remaining_s > 0:
            note = "control loop paused – readings resume after the wait" if snap.loop_blocked else "readings continue"
            self.hold_label.setText(f"{snap.hold_reason} – {snap.hold_remaining_s:.0f} s ({note})")
        else:
            self.hold_label.setText("")
        self.auto_mode_btn.setEnabled(snap.mode in (Mode.ADMIN, Mode.MANUAL))
        self.admin_mode_btn.setEnabled(snap.mode != Mode.ADMIN)
        self.manual_mode_btn.setEnabled(snap.mode != Mode.MANUAL)
        if snap.mode == Mode.AUTO:
            self.state_label.setText(f"Current Facility State: <b>{snap.current.label}</b>   Target: <b>{snap.target.label}</b>   "
                                     f"Page: {TAB_TITLES[snap.tab]}   substate {snap.substate}")
            self._show_auto_buttons(snap.tab)
        else:
            self.state_label.setText("Click a valve / pump / turbo on the diagram to command it." if snap.mode != Mode.INITIALIZE else "")
            self._show_auto_buttons(None)
        if snap.elapsed_below_threshold_s > 0:
            self.elapsed_label.setText(f"Elapsed Time below threshold: {snap.elapsed_below_threshold_s:.0f} s  "
                                       f"(needs {self.cfg.thresholds.min_time_below_threshold_s:.0f} s)")
        elif snap.mode == Mode.AUTO and snap.current.name == "OVERNIGHT_PUMP" and snap.turbo_engage_time:
            self.elapsed_label.setText("Waiting for Turbo Engage time: " + datetime.fromtimestamp(snap.turbo_engage_time).strftime("%d/%m/%Y %H:%M"))
        else:
            self.elapsed_label.setText("")
        self.reset_turbos.setText("Reset\nTurbos" if not any(snap.turbo_error_ack_active.values()) else "Ack…")
        self.primary_hours_box.setText(f"{snap.primary_run_hours:.2f}")

    def _show_auto_buttons(self, tab: Optional[TabPage]):
        if tab == self._current_tab:
            return
        self._current_tab = tab
        while self.auto_layout.count():
            item = self.auto_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.auto_buttons = []
        if tab is None:
            self.auto_group.setTitle("Auto commands (Auto mode only)")
            return
        self.auto_group.setTitle(f"Auto commands – {TAB_TITLES[tab]}")
        for label, action in self.auto_buttons_map.get(tab, []):
            b = lv_button(label, 90, 34)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            b.clicked.connect(lambda _=False, a=action: self.ctl.request_auto_button(a))
            self.auto_layout.addWidget(b)
            self.auto_buttons.append(b)
        if not self.auto_buttons_map.get(tab):
            lbl = QLabel("(sequence running – no commands on this page)")
            self.auto_layout.addWidget(lbl)
            self.auto_buttons.append(lbl)  # type: ignore[arg-type]
        self.auto_layout.addStretch(1)
