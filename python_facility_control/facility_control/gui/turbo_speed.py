"""Speed panel for the Pfeiffer HiPace turbos on RS-485 (VC100: Turbo 2, 3, 5, 6).

One row per pump:  name | Off | Full | Standby | Set | slider (set value, % of nominal) | set | actual

    Off       stop the pump ([P:010] off)                        - asks first
    Full      nominal speed                                      - asks first if the pump is stopped
    Standby   the pump's standby speed ([P:002] on, [P:717])     - asks first if the pump is stopped
    Set       run at the slider value ([P:707], [P:026] on)      - asks first if the pump is stopped

While a pump runs in 'Set', moving the slider changes its speed when the slider is released (or
shortly after a keyboard change), so the RS-485 link is not flooded with writes.  The buttons always
show what the controller is *commanding*, so a start that is still waiting for 'Yes' – or one the
interlocks refused – snaps back.  The panel only works in Manual / Admin mode; Auto runs the turbos
itself (at full speed / its own standby).
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QGridLayout, QGroupBox, QLabel, QPushButton, QSizePolicy, QSlider,
                               QWidget)

from ..config import FacilityConfig
from ..model import Mode, Snapshot

MODES = ("off", "full", "standby", "set")
LABELS = {"off": "Off", "full": "Full", "standby": "Standby", "set": "Set"}
TIPS = {
    "off": "Stop the pump",
    "full": "Run at full (nominal) speed",
    "standby": "Run at the pump's standby speed ([P:717])",
    "set": "Run at the speed chosen with the slider ([P:707], rotation speed setting mode)",
}
CHECKED = {"off": "#C9CED4", "full": "#3FB950", "standby": "#E3B341", "set": "#2F81F7"}
BTN = ("QPushButton {{ background: #F0F0F0; border: 1px solid #8A8A8A; border-radius: 3px; padding: 3px 6px;"
       " font-size: 13px; min-width: 44px; min-height: 24px; color: black; }}"
       " QPushButton:checked {{ background: {bg}; font-weight: bold; color: {fg}; }}"
       " QPushButton:disabled {{ color: #9A9A9A; }}"
       " QPushButton:checked:disabled {{ color: #5A5A5A; }}")


class _Row:
    def __init__(self, tid: str, label: str, lo: float, hi: float):
        self.tid, self.label, self.lo, self.hi = tid, label, lo, hi
        self.name = QLabel(label)
        self.name.setStyleSheet("QLabel { font-size: 13px; font-weight: bold; }")
        self.group = QButtonGroup()
        self.group.setExclusive(True)
        self.buttons: Dict[str, QPushButton] = {}
        for m in MODES:
            b = QPushButton(LABELS[m])
            b.setCheckable(True)
            b.setToolTip(TIPS[m])
            b.setStyleSheet(BTN.format(bg=CHECKED[m], fg="white" if m in ("full", "set") else "black"))
            if m == "standby":
                b.setStyleSheet(b.styleSheet() + " QPushButton { min-width: 66px; }")
            self.group.addButton(b)
            self.buttons[m] = b
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(round(lo)), int(round(hi)))
        self.slider.setValue(int(round(min(hi, max(lo, 75.0)))))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.setMinimumWidth(110)
        self.slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.slider.setToolTip(f"Speed set value, {lo:.0f}..{hi:.0f} % of nominal")
        self.value = QLabel("")
        self.value.setMinimumWidth(48)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.value.setStyleSheet("QLabel { font-size: 13px; font-weight: bold; font-family: Consolas, monospace; }")
        self.actual = QLabel("")
        self.actual.setMinimumWidth(104)
        self.actual.setStyleSheet("QLabel { font-size: 13px; color: #333333; font-family: Consolas, monospace; }")
        self.actual.setToolTip("Actual speed read from the pump ([P:309] / nominal)")
        self.commanded: str = "off"
        self.commanded_set: Optional[float] = None
        self.debounce = QTimer()
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(600)
        self._show_value()

    def _show_value(self):
        self.value.setText(f"{self.slider.value():d}%")


class TurboSpeedPanel(QGroupBox):
    """Off / Full / Standby / Set-speed control for every speed-settable turbo of the facility."""

    def __init__(self, cfg: FacilityConfig, controller, parent: Optional[QWidget] = None):
        super().__init__("Pfeiffer turbo speed", parent)
        self.cfg = cfg
        self.ctl = controller
        self.rows: Dict[str, _Row] = {}
        self._enabled: Optional[bool] = None
        self.setStyleSheet("QGroupBox { font-weight: bold; font-size: 13px; }")
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(3)
        for r, tc in enumerate(t for t in cfg.turbos if t.speed_settable):
            lo, hi = tc.speed_set_range
            row = _Row(tc.id, tc.label, lo, hi)
            grid.addWidget(row.name, r, 0)
            for c, m in enumerate(MODES):
                grid.addWidget(row.buttons[m], r, 1 + c)
                row.buttons[m].clicked.connect(lambda _=False, tid=tc.id, m=m: self._on_mode(tid, m))
            grid.addWidget(row.slider, r, 5)
            grid.addWidget(row.value, r, 6)
            grid.addWidget(row.actual, r, 7)
            row.slider.valueChanged.connect(lambda _v, tid=tc.id: self._on_slider_value(tid))
            row.slider.sliderReleased.connect(lambda tid=tc.id: self._apply_slider(tid))
            row.debounce.timeout.connect(lambda tid=tc.id: self._apply_slider(tid))
            if not tc.in_use:
                row.name.setText(f"{tc.label} (not in use)")
            self.rows[tc.id] = row
        grid.setColumnStretch(5, 1)

    def __bool__(self) -> bool:          # a QGroupBox is truthy anyway; this makes "no HiPace" explicit
        return bool(self.rows)

    # ------------------------------------------------------------------ user actions
    def _on_mode(self, tid: str, mode: str):
        row = self.rows[tid]
        pct = float(row.slider.value()) if mode == "set" else None
        self.ctl.request_turbo_speed(tid, mode, pct)
        self._check(row, row.commanded)           # show the command, not the click, until it is applied

    def _on_slider_value(self, tid: str):
        row = self.rows[tid]
        row._show_value()
        if row.commanded == "set" and not row.slider.isSliderDown():
            row.debounce.start()                  # keyboard / wheel / click on the groove

    def _apply_slider(self, tid: str):
        row = self.rows[tid]
        row.debounce.stop()
        if row.commanded != "set":
            return                                # just preselecting the value for 'Set'
        pct = float(row.slider.value())
        if row.commanded_set is not None and abs(pct - row.commanded_set) < 0.5:
            return
        self.ctl.request_turbo_speed(tid, "set", pct)

    # ------------------------------------------------------------------ refresh from the controller
    @staticmethod
    def _check(row: _Row, mode: str):
        b = row.buttons[mode]
        if not b.isChecked():
            b.setChecked(True)

    def update_from(self, snap: Snapshot):
        enabled = snap.mode in (Mode.ADMIN, Mode.MANUAL)
        if enabled != self._enabled:
            self._enabled = enabled
            self.setTitle("Pfeiffer turbo speed" if enabled
                          else "Pfeiffer turbo speed  (Manual / Admin mode only – Auto runs at full speed)")
        cmds = snap.commands
        for tid, row in self.rows.items():
            tc = self.cfg.turbo(tid)
            motor = bool(cmds.turbo_motor.get(tid, False))
            standby = bool(cmds.turbo_standby.get(tid, False))
            sp = cmds.speed_set(tid)
            mode = "off" if not motor else "standby" if standby else "set" if sp is not None else "full"
            row.commanded, row.commanded_set = mode, sp
            self._check(row, mode)
            on = enabled and tc.in_use
            for b in row.buttons.values():
                b.setEnabled(on)
            row.slider.setEnabled(on)
            if mode == "set" and sp is not None and not row.slider.isSliderDown() and not row.debounce.isActive():
                if row.slider.value() != int(round(sp)):
                    row.slider.blockSignals(True)
                    row.slider.setValue(int(round(sp)))
                    row.slider.blockSignals(False)
                    row._show_value()
            tv = snap.turbos.get(tid)
            if tv is None:
                row.actual.setText("")
            else:
                row.actual.setText(f"actual {tv.speed_pct:3.0f}%")
