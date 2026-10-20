"""Two stacked plots sharing the time axis (VI: 'Waveform Chart 2' with 9 plots):
top = pressures on a log axis, bottom = On/Off of pumps, chiller and valves.

Unlike the LabVIEW chart the history can be explored.  The panel is used on a **touch screen**, so
the controls are large and pinch-free:

* **Box zoom** – drag a rectangle over either plot to zoom into it (pyqtgraph's RectMode).  Drag with
  box zoom off to pan.  Any manual range change stops the live auto-scroll.
* **Reset X** / **Reset Y** – reset the two axes independently: X returns to the live window, Y
  rescales to whatever traces are currently shown.
* **Show:** one checkbox per reading.  Hiding a trace rescales the remaining ones when
  *Auto-rescale on show/hide* is ticked.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, List, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..config import FacilityConfig
from ..model import Snapshot
from ..units import torr_to, unit_label

pg.setConfigOptions(antialias=False, background="w", foreground="k")

PRESSURE_COLORS = ["#1F3FCF", "#D62728", "#2CA02C", "#17BECF", "#9467BD", "#8C564B"]
STATUS_COLORS = ["#9467BD", "#E5A800", "#1F3FCF", "#D62728", "#2CA02C", "#000000", "#D62728", "#2CA02C", "#17BECF", "#FF7F0E"]

# big enough to hit with a finger
TOUCH_BUTTON = ("QPushButton { min-height: 30px; min-width: 86px; font-size: 12px; padding: 2px 10px; }"
                "QPushButton:checked { background: #C8E6C9; font-weight: bold; }")
TOUCH_CHECK = ("QCheckBox { font-size: 12px; padding: 3px 6px; }"
               "QCheckBox::indicator { width: 18px; height: 18px; }")


class HistoryBuffer:
    """Ring buffers at ~1 Hz (24 h ≈ 86 400 points per series)."""

    def __init__(self, series: List[str], maxlen: int = 86400):
        self.t: Deque[float] = deque(maxlen=maxlen)
        self.data: Dict[str, Deque[float]] = {s: deque(maxlen=maxlen) for s in series}
        self._last = 0.0

    def add(self, t: float, values: Dict[str, float], min_dt: float = 1.0) -> bool:
        if t - self._last < min_dt:
            return False
        self._last = t
        self.t.append(t)
        for s, dq in self.data.items():
            dq.append(values.get(s, float("nan")))
        return True


class PlotsWidget(QWidget):
    def __init__(self, cfg: FacilityConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.unit = cfg.pressure_unit_default
        self.pressure_series = [g.id for g in cfg.gauges]
        self.status_series: List[Tuple[str, str]] = [("primary", cfg.primary.label), ("chiller", cfg.chiller.label)]
        for t in cfg.turbos:
            self.status_series.append((f"turbo:{t.id}", t.label))
        for vid, v in cfg.valves.items():
            self.status_series.append((f"valve:{vid}", v.label))
        self.history = HistoryBuffer(self.pressure_series + [k for k, _ in self.status_series])
        self.window_s = 30 * 60

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        # ---------------- toolbar: follow / zoom / axis resets
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.follow = QCheckBox("Follow (auto-scroll)")
        self.follow.setStyleSheet(TOUCH_CHECK)
        self.follow.setChecked(True)
        self.follow.toggled.connect(self._on_follow)
        bar.addWidget(self.follow)

        self.box_zoom = QPushButton("Box zoom")
        self.box_zoom.setCheckable(True)
        self.box_zoom.setStyleSheet(TOUCH_BUTTON)
        self.box_zoom.setToolTip("Drag a rectangle on a plot to zoom into it. Off = drag to pan.")
        self.box_zoom.toggled.connect(self._on_box_zoom)
        bar.addWidget(self.box_zoom)

        self.reset_x_btn = QPushButton("Reset X")
        self.reset_x_btn.setStyleSheet(TOUCH_BUTTON)
        self.reset_x_btn.setToolTip("Time axis back to the live window")
        self.reset_x_btn.clicked.connect(self.reset_x)
        bar.addWidget(self.reset_x_btn)

        self.reset_y_btn = QPushButton("Reset Y")
        self.reset_y_btn.setStyleSheet(TOUCH_BUTTON)
        self.reset_y_btn.setToolTip("Rescale the pressure axis to the traces that are shown")
        self.reset_y_btn.clicked.connect(self.reset_y)
        bar.addWidget(self.reset_y_btn)

        self.auto_rescale = QCheckBox("Auto-rescale on show/hide")
        self.auto_rescale.setStyleSheet(TOUCH_CHECK)
        self.auto_rescale.setChecked(True)
        bar.addWidget(self.auto_rescale)

        bar.addStretch(1)
        self.window_label = QLabel("window: 30 min")
        bar.addWidget(self.window_label)
        layout.addLayout(bar)

        # ---------------- plots
        self.glw = pg.GraphicsLayoutWidget()
        layout.addWidget(self.glw, 1)
        axis1 = pg.DateAxisItem(orientation="bottom")
        self.p_top = self.glw.addPlot(row=0, col=0, axisItems={"bottom": axis1})
        self.p_top.setLogMode(x=False, y=True)
        self.p_top.showGrid(x=True, y=True, alpha=0.3)
        self.p_top.setLabel("left", f"Pressure ({unit_label(self.unit)})")
        self.p_top.addLegend(offset=(-10, 10))
        axis2 = pg.DateAxisItem(orientation="bottom")
        self.p_bot = self.glw.addPlot(row=1, col=0, axisItems={"bottom": axis2})
        self.p_bot.setXLink(self.p_top)
        self.p_bot.setYRange(-0.1, 1.1)
        self.p_bot.setLabel("left", "Status")
        self.p_bot.getAxis("left").setTicks([[(0, "Off"), (1, "On")]])
        self.p_bot.showGrid(x=True, y=False, alpha=0.3)
        self.p_bot.addLegend(offset=(-10, 10))
        self.glw.ci.layout.setRowStretchFactor(0, 5)
        self.glw.ci.layout.setRowStretchFactor(1, 3)

        # a manual pan/zoom means the operator wants to look at history -> stop following
        for p in (self.p_top, self.p_bot):
            p.getViewBox().sigRangeChangedManually.connect(self._on_manual_range)

        self.curves_top: Dict[str, pg.PlotDataItem] = {}
        for i, g in enumerate(cfg.gauges):
            self.curves_top[g.id] = self.p_top.plot(pen=pg.mkPen(PRESSURE_COLORS[i % len(PRESSURE_COLORS)], width=1.5),
                                                    name=g.plot_label or g.label)
        self.curves_bot: Dict[str, pg.PlotDataItem] = {}
        for i, (key, label) in enumerate(self.status_series):
            self.curves_bot[key] = self.p_bot.plot(pen=pg.mkPen(STATUS_COLORS[i % len(STATUS_COLORS)], width=1.5),
                                                   name=label, stepMode=None)

        # ---------------- per-trace show/hide
        self.trace_boxes: Dict[str, QCheckBox] = {}
        layout.addLayout(self._build_trace_row(
            "Pressures:", [(g.id, g.plot_label or g.label, PRESSURE_COLORS[i % len(PRESSURE_COLORS)])
                           for i, g in enumerate(cfg.gauges)]))
        layout.addLayout(self._build_trace_row(
            "Status:", [(key, label, STATUS_COLORS[i % len(STATUS_COLORS)])
                        for i, (key, label) in enumerate(self.status_series)]))

    # ------------------------------------------------------------------ build helpers
    def _build_trace_row(self, title: str, items) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(2)
        lab = QLabel(title)
        lab.setStyleSheet("font-size: 12px; font-weight: bold;")
        row.addWidget(lab)
        for key, label, color in items:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.setStyleSheet(TOUCH_CHECK + f"QCheckBox {{ color: {color}; }}")
            cb.toggled.connect(lambda on, k=key: self._on_trace_toggled(k, on))
            row.addWidget(cb)
            self.trace_boxes[key] = cb
        row.addStretch(1)
        return row

    # ------------------------------------------------------------------ controls
    def _curve(self, key: str):
        return self.curves_top.get(key) or self.curves_bot.get(key)

    def _on_trace_toggled(self, key: str, on: bool) -> None:
        curve = self._curve(key)
        if curve is not None:
            curve.setVisible(on)
        if self.auto_rescale.isChecked():
            self.reset_y()

    def _on_box_zoom(self, on: bool) -> None:
        mode = pg.ViewBox.RectMode if on else pg.ViewBox.PanMode
        for p in (self.p_top, self.p_bot):
            p.getViewBox().setMouseMode(mode)

    def _on_manual_range(self, *_args) -> None:
        if self.follow.isChecked():
            self.follow.blockSignals(True)
            self.follow.setChecked(False)
            self.follow.blockSignals(False)

    def _on_follow(self, on: bool) -> None:
        if on:
            self.reset_x()

    def reset_x(self) -> None:
        """Time axis back to the live window (and resume following)."""
        if not self.follow.isChecked():
            self.follow.blockSignals(True)
            self.follow.setChecked(True)
            self.follow.blockSignals(False)
        if self.history.t:
            t1 = self.history.t[-1]
            self.p_top.setXRange(t1 - self.window_s, t1 + 5, padding=0)

    def reset_y(self) -> None:
        """Rescale each plot's Y to the traces that are currently shown."""
        self.p_top.enableAutoRange(axis="y")
        self.p_bot.setYRange(-0.1, 1.1)

    # ------------------------------------------------------------------ data
    def set_unit(self, unit: str):
        self.unit = unit
        self.p_top.setLabel("left", f"Pressure ({unit_label(unit)})")
        self.redraw()
        if self.auto_rescale.isChecked():
            self.p_top.enableAutoRange(axis="y")

    def add_snapshot(self, snap: Snapshot):
        vals: Dict[str, float] = {}
        for g in self.cfg.gauges:
            vals[g.id] = snap.inputs.pressures_torr.get(g.id, float("nan"))
        vals["primary"] = 1.0 if snap.primary_running else 0.0
        vals["chiller"] = 1.0 if snap.inputs.chiller_read else 0.0
        for t in self.cfg.turbos:
            tv = snap.turbos.get(t.id)
            vals[f"turbo:{t.id}"] = 1.0 if (tv and tv.motor_read) else 0.0
        for vid in self.cfg.valve_ids:
            vals[f"valve:{vid}"] = 1.0 if snap.inputs.valve_reads.get(vid) else 0.0
        if self.history.add(snap.t, vals):
            self.redraw()

    def redraw(self):
        if not self.history.t:
            return
        t = np.fromiter(self.history.t, dtype=float)
        n = len(t)
        # stagger the boolean traces slightly so overlapping On/Off lines stay visible
        for i, (key, _) in enumerate(self.status_series):
            y = np.fromiter(self.history.data[key], dtype=float, count=n)
            self.curves_bot[key].setData(t, y * (1.0 - 0.02 * i) + 0.01 * i)
        for gid in self.pressure_series:
            y = np.fromiter(self.history.data[gid], dtype=float, count=n)
            y = torr_to(self.unit, y)
            y = np.where(np.isfinite(y) & (y > 0), y, np.nan)
            self.curves_top[gid].setData(t, y, connect="finite")
        if self.follow.isChecked():
            t1 = t[-1]
            self.p_top.setXRange(t1 - self.window_s, t1 + 5, padding=0)
            self.p_top.enableAutoRange(axis="y")
