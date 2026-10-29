"""Speed window for one turbo (VC100: Pfeiffer HiPace Turbo 2, 3, 5, 6 on RS-485; since 2026-10-29
also the Shimadzu TMP1 / TMP4 - Off / Full speed / Standby only, via their motor + standby lines).

Opens when the operator clicks that turbo on the diagram (Manual / Admin mode):

    Turbo 2 – Pfeiffer HiPace
    Now: running at full speed  ·  actual 100 %  ·  Speed Reached
    ( Off )  ( Full speed )  ( Standby )  ( Set speed )
    Set speed  [=========|-----------]  [ 60 %]        <- only active with 'Set speed'
                                   [ Start at 60 % ]  [ Close ]

    Off         stop the pump ([P:010] off)
    Full speed  nominal speed
    Standby     the pump's standby speed ([P:002] on, [P:717])
    Set speed   run at the chosen value ([P:707], rotation speed setting mode [P:026] on)

Nothing is sent until Apply is pressed; the Apply button always names what it will do ("Start at
60 %", "Change to 45 %", "Stop Turbo 2" ...), so it is the confirmation - no second Yes/No box.  In
Manual mode the interlocks still apply (e.g. "Please Turn Primary Pump On first").  The window stays
open so the speed can be watched; in Auto mode its controls are disabled (Auto runs the turbos at
full speed / its own standby).
"""
from __future__ import annotations

import html

from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from ..config import FacilityConfig
from ..model import Mode, Snapshot, contact_summary

MODES = ("off", "full", "standby", "set")
LABELS = {"off": "Off", "full": "Full speed", "standby": "Standby", "set": "Set speed"}
TIPS = {
    "off": "Stop the pump",
    "full": "Run at full (nominal) speed",
    "standby": "Run at the pump's standby speed ([P:717])",
    "set": "Run at the speed chosen below ([P:707], rotation speed setting mode)",
}
CHECKED = {"off": "#C9CED4", "full": "#3FB950", "standby": "#E3B341", "set": "#2F81F7"}
BTN = ("QPushButton {{ background: #F0F0F0; border: 1px solid #8A8A8A; border-radius: 4px; padding: 6px 10px;"
       " font-size: 14px; min-width: 84px; min-height: 30px; color: black; }}"
       " QPushButton:checked {{ background: {bg}; font-weight: bold; color: {fg}; border: 2px solid #3D444D; }}"
       " QPushButton:disabled {{ color: #9A9A9A; }}")


def commanded_mode(snap: Snapshot, tid: str):
    """What the controller is commanding for this turbo: ('off'|'full'|'standby'|'set', set value)."""
    c = snap.commands
    if not c.turbo_motor.get(tid, False):
        return "off", None
    if c.turbo_standby.get(tid, False):
        return "standby", None
    sp = c.speed_set(tid)
    return ("set", sp) if sp is not None else ("full", None)


class TurboSpeedDialog(QDialog):
    """Off / Full speed / Standby / Set speed for one HiPace (opened from the diagram)."""

    def __init__(self, cfg: FacilityConfig, controller, tid: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg, self.ctl, self.tid = cfg, controller, tid
        self.tc = cfg.turbo(tid)
        self.lo, self.hi = self.tc.speed_set_range
        self.settable = self.tc.speed_settable      # HiPace: + 'Set speed'; Shimadzu: Off/Full/Standby
        self.setWindowTitle(f"{self.tc.label} – speed")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._commanded = "off"
        self._commanded_set: Optional[float] = None
        self._enabled: Optional[bool] = None
        self._touched = False                 # operator picked something since the last apply

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)
        kind = "Pfeiffer HiPace (RS-485)" if self.settable else "Shimadzu"
        title = QLabel(f"{self.tc.label}  –  {kind}")
        title.setStyleSheet("QLabel { font-size: 16px; font-weight: bold; }")
        lay.addWidget(title)
        self.now = QLabel("")
        self.now.setStyleSheet("QLabel { font-size: 14px; color: #1F2328; }")
        lay.addWidget(self.now)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        lay.addWidget(line)

        row = QHBoxLayout()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, QPushButton] = {}
        for m in MODES:
            b = QPushButton(LABELS[m])
            b.setCheckable(True)
            b.setToolTip(TIPS[m])
            b.setStyleSheet(BTN.format(bg=CHECKED[m], fg="white" if m in ("full", "set") else "black"))
            b.clicked.connect(lambda _=False, m=m: self._on_pick(m))
            self.group.addButton(b)
            self.buttons[m] = b
            row.addWidget(b)
            if m == "set" and not self.settable:
                b.hide()                          # no speed set value on this pump
            if m == "standby" and not self.settable:
                b.setToolTip("Run in standby (standby output line on)")
        lay.addLayout(row)

        srow = QHBoxLayout()
        self.slider_label = QLabel("Set speed")
        self.slider_label.setStyleSheet("QLabel { font-size: 14px; }")
        srow.addWidget(self.slider_label)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(int(round(self.lo)), int(round(self.hi)))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.setMinimumWidth(240)
        self.slider.setToolTip(f"Speed set value, {self.lo:.0f}..{self.hi:.0f} % of nominal")
        srow.addWidget(self.slider, 1)
        self.spin = QSpinBox()
        self.spin.setRange(int(round(self.lo)), int(round(self.hi)))
        self.spin.setSuffix(" %")
        self.spin.setStyleSheet("QSpinBox { font-size: 15px; font-weight: bold; min-width: 76px; min-height: 28px; }")
        srow.addWidget(self.spin)
        lay.addLayout(srow)
        start = int(round(min(self.hi, max(self.lo, 75.0))))
        self.slider.setValue(start)
        self.spin.setValue(start)
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        if not self.settable:
            for w in (self.slider_label, self.slider, self.spin):
                w.hide()

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("QLabel { font-size: 12px; color: #8A4B00; }")
        lay.addWidget(self.note)

        brow = QHBoxLayout()
        brow.addStretch(1)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setStyleSheet("QPushButton { font-size: 14px; font-weight: bold; min-width: 160px;"
                                     " min-height: 34px; padding: 4px 14px; }")
        self.apply_btn.clicked.connect(self._on_apply)
        self.apply_btn.setDefault(True)
        brow.addWidget(self.apply_btn)
        close = QPushButton("Close")
        close.setStyleSheet("QPushButton { font-size: 14px; min-width: 90px; min-height: 34px; }")
        close.clicked.connect(self.close)
        brow.addWidget(close)
        lay.addLayout(brow)
        self._select("off")

    # ------------------------------------------------------------------ helpers
    def selected(self) -> str:
        for m, b in self.buttons.items():
            if b.isChecked():
                return m
        return self._commanded

    def _select(self, mode: str):
        self.buttons[mode].setChecked(True)
        self._refresh_controls()

    def _set_value(self, v: int):
        for w in (self.slider, self.spin):
            w.blockSignals(True)
            w.setValue(int(v))
            w.blockSignals(False)

    def _on_slider(self, v: int):
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self._touched = True
        self._refresh_controls()

    def _on_spin(self, v: int):
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self._touched = True
        self._refresh_controls()

    def _on_pick(self, mode: str):
        self._touched = True
        self._refresh_controls()

    def _action(self):
        """(button text, request or None) for what is selected, compared with what is commanded."""
        sel = self.selected()
        name = self.tc.label
        pct = float(self.spin.value())
        running = self._commanded != "off"
        if sel == "off":
            return (f"Stop {name}", ("off", None)) if running else ("Already stopped", None)
        if sel == "set" and pct >= 100.0:
            sel = "full"
        if sel == self._commanded and (sel != "set" or (self._commanded_set is not None
                                                        and abs(pct - self._commanded_set) < 0.5)):
            return "No change", None
        target = {"full": "full speed", "standby": "standby", "set": f"{pct:.0f} %"}[sel]
        if not running:
            text = "Start in standby" if sel == "standby" else f"Start at {target}"
        else:
            text = "Go to standby" if sel == "standby" else f"Change to {target}"
        return text, (sel, pct if sel == "set" else None)

    def _refresh_controls(self):
        on = bool(self._enabled) and self.tc.in_use
        for b in self.buttons.values():
            b.setEnabled(on)
        set_on = on and self.selected() == "set"
        for w in (self.slider, self.spin, self.slider_label):
            w.setEnabled(set_on)
        text, req = self._action()
        self.apply_btn.setText(text)
        self.apply_btn.setEnabled(on and req is not None)

    def _on_apply(self):
        text, req = self._action()
        if req is None:
            return
        mode, pct = req
        self.ctl.request_turbo_speed(self.tid, mode, pct, confirmed=True)
        self._touched = False
        self.apply_btn.setEnabled(False)          # re-enabled by the next update once applied

    # ------------------------------------------------------------------ refresh from the controller
    def update_from(self, snap: Snapshot):
        enabled = snap.mode in (Mode.ADMIN, Mode.MANUAL)
        mode, sp = commanded_mode(snap, self.tid)
        changed = (mode, sp) != (self._commanded, self._commanded_set)
        self._commanded, self._commanded_set = mode, sp
        if enabled != self._enabled:
            self._enabled = enabled
            self.note.setText("" if enabled else
                              "Auto mode runs the turbos itself (full speed / standby). "
                              "Switch to Manual or Admin mode to change this pump's speed.")
        if changed or not self._touched:          # follow the controller unless the operator is choosing
            self.buttons[mode].setChecked(True)
            if mode == "set" and sp is not None:
                self._set_value(int(round(sp)))
            self._touched = False
        tv = snap.turbos.get(self.tid)
        what = {"off": "stopped", "full": "full speed", "standby": "standby",
                "set": f"set to {sp:.0f} %" if sp is not None else "set speed"}[mode]
        if tv is not None:
            status = tv.status.label.replace("Turbo ", "")
            if tv.has_speed:
                actual = f"actual <b>{tv.speed_pct:.0f} %</b>"
            else:                                 # Shimadzu: what its status contacts say
                sig = contact_summary(tv.contacts, tv.motor_read, self.tc.params.get("at_speed_pct"), tv.standby)
                actual = "signals <b>" + html.escape(sig) + "</b>"      # '<80%' is not a tag
            self.now.setText(f"Now: <b>{what}</b>   ·   {actual}   ·   {status}")
        else:
            self.now.setText(f"Now: <b>{what}</b>")
        self._refresh_controls()
