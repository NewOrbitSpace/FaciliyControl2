"""Two stacked plots sharing the time axis (VI: 'Waveform Chart 2' with 9 plots):
top = pressures on a log axis, bottom = On/Off of pumps, chiller and valves.
Unlike the LabVIEW chart the history can be zoomed/panned ('Follow' unticked)."""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, List, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..config import FacilityConfig
from ..model import Snapshot
from ..units import torr_to, unit_label

pg.setConfigOptions(antialias=False, background="w", foreground="k")

PRESSURE_COLORS = ["#1F3FCF", "#D62728", "#2CA02C", "#17BECF", "#9467BD", "#8C564B"]
STATUS_COLORS = ["#9467BD", "#E5A800", "#1F3FCF", "#D62728", "#2CA02C", "#000000", "#D62728", "#2CA02C", "#17BECF", "#FF7F0E"]


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.follow = QCheckBox("Follow (auto-scroll)")
        self.follow.setChecked(True)
        self.window_label = QLabel("window: 30 min")
        top.addWidget(self.follow)
        top.addStretch(1)
        top.addWidget(self.window_label)
        layout.addLayout(top)

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

        self.curves_top: Dict[str, pg.PlotDataItem] = {}
        for i, g in enumerate(cfg.gauges):
            self.curves_top[g.id] = self.p_top.plot(pen=pg.mkPen(PRESSURE_COLORS[i % len(PRESSURE_COLORS)], width=1.5), name=g.plot_label or g.label)
        self.curves_bot: Dict[str, pg.PlotDataItem] = {}
        for i, (key, label) in enumerate(self.status_series):
            self.curves_bot[key] = self.p_bot.plot(pen=pg.mkPen(STATUS_COLORS[i % len(STATUS_COLORS)], width=1.5), name=label, stepMode=None)
        self.window_s = 30 * 60

    def set_unit(self, unit: str):
        self.unit = unit
        self.p_top.setLabel("left", f"Pressure ({unit_label(unit)})")

    def add_snapshot(self, snap: Snapshot):
        vals: Dict[str, float] = {}
        for g in self.cfg.gauges:
            vals[g.id] = snap.inputs.pressures_torr.get(g.id, float("nan"))
        vals["primary"] = 1.0 if snap.inputs.primary_read else 0.0
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
