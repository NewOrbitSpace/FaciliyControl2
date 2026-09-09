"""The pumping diagram (P&ID) of the LabVIEW front panel, drawn from the facility config.

Grey = off/closed, green = on/open, red = error; turbos use the VI's six status colours.
In Admin / Manual mode the symbols are clickable (the VI overlays its *_User_Cmd buttons
on the diagram) and emit `commandRequested('valve:<id>' | 'primary' | 'chiller' | 'turbo:<id>')`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..config import FacilityConfig
from ..model import COLOR_ERROR, COLOR_OFF, COLOR_ON, Mode, Snapshot
from ..units import format_pressure, unit_label

GRID = QColor("#E4E4E4")
PIPE = QColor("#505050")
BOX_BG = QColor("#DADADA")
TEXT = QColor("#000000")


@dataclass
class Hit:
    rect: QRectF
    target: str
    tooltip: str


class SchematicWidget(QWidget):
    commandRequested = Signal(str)

    W, H = 690.0, 640.0   # virtual canvas

    def __init__(self, cfg: FacilityConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.snap: Optional[Snapshot] = None
        self.unit = cfg.pressure_unit_default
        self.hits: List[Hit] = []
        self.setMinimumSize(600, 560)
        self.setMouseTracking(True)
        self.setAutoFillBackground(True)

    # ------------------------------------------------------------------ API
    def set_snapshot(self, snap: Snapshot, unit: str):
        self.snap = snap
        self.unit = unit
        self.update()

    def clickable(self) -> bool:
        return self.snap is not None and self.snap.mode in (Mode.ADMIN, Mode.MANUAL)

    # ------------------------------------------------------------------ events
    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        pos = self._to_virtual(ev.position())
        for h in self.hits:
            if h.rect.contains(pos):
                if self.clickable():
                    self.commandRequested.emit(h.target)
                return

    def mouseMoveEvent(self, ev):
        pos = self._to_virtual(ev.position())
        tip = ""
        for h in self.hits:
            if h.rect.contains(pos):
                tip = h.tooltip if self.clickable() else f"{h.tooltip} (switch to Admin/Manual mode to command)"
                break
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor if (tip and self.clickable()) else Qt.ArrowCursor)

    def _scale(self) -> Tuple[float, float, float]:
        s = min(self.width() / self.W, self.height() / self.H)
        ox = (self.width() - self.W * s) / 2
        oy = (self.height() - self.H * s) / 2
        return s, ox, oy

    def _to_virtual(self, p: QPointF) -> QPointF:
        s, ox, oy = self._scale()
        return QPointF((p.x() - ox) / s, (p.y() - oy) / s)

    # ------------------------------------------------------------------ painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.fillRect(self.rect(), QColor("#F4F4F4"))
        # grid like the LabVIEW front panel
        p.setPen(QPen(GRID, 1))
        step = 12
        for x in range(0, self.width(), step):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), step):
            p.drawLine(0, y, self.width(), y)
        s, ox, oy = self._scale()
        p.translate(ox, oy)
        p.scale(s, s)
        self.hits = []
        self._draw(p)

    # ---- drawing helpers ----------------------------------------------------
    def _font(self, size: float, bold: bool = False) -> QFont:
        f = QFont("Segoe UI")
        f.setPointSizeF(size)
        f.setBold(bold)
        return f

    def _box(self, p: QPainter, rect: QRectF, lines: List[Tuple[str, float, bool]], bg: QColor = BOX_BG, align=Qt.AlignCenter):
        p.setPen(QPen(QColor("#8A8A8A"), 1))
        p.setBrush(QBrush(bg))
        p.drawRect(rect)
        p.setPen(QPen(TEXT))
        n = len(lines)
        h = rect.height() / max(n, 1)
        for i, (txt, size, bold) in enumerate(lines):
            p.setFont(self._font(size, bold))
            r = QRectF(rect.x() + 3, rect.y() + i * h, rect.width() - 6, h)
            p.drawText(r, align | Qt.AlignVCenter, txt)

    def _label(self, p: QPainter, x: float, y: float, txt: str, size: float = 9.5, bold=False, align=Qt.AlignLeft):
        p.setPen(QPen(TEXT))
        p.setFont(self._font(size, bold))
        r = QRectF(x, y - 8, 260, 16)
        if align == Qt.AlignRight:
            r = QRectF(x - 260, y - 8, 260, 16)
        elif align == Qt.AlignHCenter:
            r = QRectF(x - 130, y - 8, 260, 16)
        p.drawText(r, align | Qt.AlignVCenter, txt)

    def _pipe(self, p: QPainter, pts: List[Tuple[float, float]]):
        p.setPen(QPen(PIPE, 3))
        for a, b in zip(pts, pts[1:]):
            p.drawLine(QPointF(*a), QPointF(*b))

    def _valve_color(self, vid: str) -> QColor:
        if not self.snap:
            return QColor(COLOR_OFF)
        cmd = self.snap.commands.valves.get(vid, False)
        rd = self.snap.inputs.valve_reads.get(vid, False)
        if self.snap.valve_errors.get(vid) and cmd != rd:
            return QColor(COLOR_ERROR)
        return QColor(COLOR_ON if rd else COLOR_OFF)

    def _pneumatic_valve(self, p: QPainter, cx: float, cy: float, vid: str, label: str, label_dx: float = 22,
                         label_below: bool = False):
        """Vertical pneumatic valve: square body with an actuator triangle on top (VC100 panel style)."""
        col = self._valve_color(vid)
        body = QRectF(cx - 17, cy - 17, 34, 34)
        p.setPen(QPen(QColor("#303030"), 1.5))
        p.setBrush(QBrush(col))
        p.drawRect(body)
        tri = QPolygonF([QPointF(cx - 13, cy - 17), QPointF(cx + 13, cy - 17), QPointF(cx, cy - 30)])
        p.drawPolygon(tri)
        p.drawLine(QPointF(cx, cy - 30), QPointF(cx, cy - 36))
        p.drawLine(QPointF(cx - 6, cy - 36), QPointF(cx + 6, cy - 36))
        if label_below:
            self._label(p, cx, cy + 27, label, 9.5, False, Qt.AlignHCenter)
        else:
            self._label(p, cx + label_dx, cy - 2, label, 10)
        self.hits.append(Hit(QRectF(cx - 20, cy - 38, 40, 58), f"valve:{vid}", f"{label}: click to open/close"))

    def _horizontal_valve(self, p: QPainter, cx: float, cy: float, vid: str, label: str):
        col = self._valve_color(vid)
        p.setPen(QPen(QColor("#303030"), 1.5))
        p.setBrush(QBrush(col))
        p.drawRect(QRectF(cx - 16, cy - 12, 32, 24))
        tri = QPolygonF([QPointF(cx - 16, cy - 8), QPointF(cx - 16, cy + 8), QPointF(cx - 28, cy)])
        p.drawPolygon(tri)
        p.drawLine(QPointF(cx - 28, cy), QPointF(cx - 36, cy))
        p.drawLine(QPointF(cx - 36, cy - 6), QPointF(cx - 36, cy + 6))
        self._label(p, cx - 34, cy - 24, label, 10)
        self.hits.append(Hit(QRectF(cx - 38, cy - 14, 56, 28), f"valve:{vid}", f"{label}: click to open/close"))

    def _turbo(self, p: QPainter, cx: float, cy: float, tid: str, r: float = 40):
        tv = self.snap.turbos.get(tid) if self.snap else None
        col = QColor(tv.color if tv else COLOR_OFF)
        if tv and not tv.in_use:
            col = QColor("#B0B0B0")
        p.setPen(QPen(QColor("#303030"), 2))
        p.setBrush(QBrush(col))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(QBrush(QColor("#F4F4F4")))
        p.drawEllipse(QPointF(cx, cy), r * 0.4, r * 0.4)
        # rotor blades hint
        p.setPen(QPen(QColor("#303030"), 1))
        for k in range(8):
            a = k * math.pi / 4
            p.drawLine(QPointF(cx + math.cos(a) * r * 0.45, cy + math.sin(a) * r * 0.45),
                       QPointF(cx + math.cos(a) * r * 0.9, cy + math.sin(a) * r * 0.9))
        self.hits.append(Hit(QRectF(cx - r, cy - r, 2 * r, 2 * r), f"turbo:{tid}", "Turbo pump: click to start/stop/standby"))

    def _primary(self, p: QPainter, cx: float, cy: float, r: float = 38):
        col = QColor(COLOR_OFF)
        if self.snap:
            st = self.snap.primary_status
            col = QColor({0: COLOR_OFF, 1: COLOR_ON, 2: COLOR_ERROR}[int(st)])
            if self.snap.inputs.primary_read and int(st) != 2:
                col = QColor(COLOR_ON)
        p.setPen(QPen(QColor("#303030"), 2))
        p.setBrush(QBrush(col))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor("#303030"), 3))
        for dy in (-10, 4):
            path = QPainterPath(QPointF(cx - 14, cy + dy - 6))
            path.lineTo(QPointF(cx, cy + dy + 6))
            path.lineTo(QPointF(cx + 14, cy + dy - 6))
            p.drawPath(path)
        self.hits.append(Hit(QRectF(cx - r, cy - r, 2 * r, 2 * r), "primary", "Primary pump: click to start/stop"))

    def _chiller(self, p: QPainter, x: float, y: float, w: float = 76, h: float = 76):
        col = QColor(COLOR_OFF)
        if self.snap:
            col = QColor({0: COLOR_OFF, 1: COLOR_ON, 2: COLOR_ERROR}[int(self.snap.chiller_status)])
        p.setPen(QPen(QColor("#303030"), 2))
        p.setBrush(QBrush(col))
        p.drawRect(QRectF(x, y, w, h))
        p.setPen(QPen(QColor("#202020"), 2))
        cx, cy = x + w / 2, y + h / 2
        for k in range(6):
            a = k * math.pi / 3
            ex, ey = cx + math.cos(a) * 24, cy + math.sin(a) * 24
            p.drawLine(QPointF(cx, cy), QPointF(ex, ey))
            for sgn in (-1, 1):
                b = a + sgn * math.pi / 5
                p.drawLine(QPointF(cx + math.cos(a) * 15, cy + math.sin(a) * 15),
                           QPointF(cx + math.cos(a) * 15 + math.cos(b) * 7, cy + math.sin(a) * 15 + math.sin(b) * 7))
        self.hits.append(Hit(QRectF(x, y, w, h), "chiller", "Chiller: click to start/stop"))

    def _pressure_text(self, gid: str) -> str:
        if not self.snap:
            return "NaN"
        return format_pressure(self.snap.inputs.pressures_torr.get(gid, float("nan")), self.unit)

    # ---- the diagram ---------------------------------------------------------
    def _draw(self, p: QPainter):
        cfg, snap = self.cfg, self.snap
        unit = unit_label(self.unit)
        turbos = cfg.turbos
        n = max(1, len(turbos))
        # geometry
        ch_rect = QRectF(90, 20, 560, 190)           # chamber ellipse
        manifold_y = 560                               # foreline manifold
        spacing = 150 if n <= 2 else 165
        xs = [ch_rect.center().x() - 40 + (i - (n - 1) / 2) * spacing for i in range(n)]
        if n == 1:
            xs = [330.0]
        bypass_x = ch_rect.right() + 14 if n > 1 else ch_rect.right() - 80
        # ---- pipes first (under everything)
        for x in xs:
            self._pipe(p, [(x, ch_rect.bottom() - 15), (x, manifold_y)])
        self._pipe(p, [(xs[0] - 60 if n > 1 else 175, manifold_y), (bypass_x, manifold_y)])
        self._pipe(p, [(bypass_x, ch_rect.center().y() + (30 if n == 1 else 0)), (bypass_x, manifold_y)])
        self._pipe(p, [(185, manifold_y), (185, 600)])   # down to the primary pump
        self._pipe(p, [(ch_rect.left() + 12, ch_rect.center().y() - 40), (52, ch_rect.center().y() - 40)])  # vent stub
        # ---- chamber
        p.setPen(QPen(QColor("#303030"), 2.5))
        p.setBrush(QBrush(QColor("#CFCFCF")))
        p.drawEllipse(ch_rect)
        main = cfg.main_gauge
        self._label(p, ch_rect.center().x(), ch_rect.top() + 48, f"{main.label} Pressure", 10, False, Qt.AlignHCenter)
        big = QRectF(ch_rect.center().x() - 95, ch_rect.top() + 60, 190, 62)
        self._box(p, big, [(self._pressure_text(main.id), 26, False)])
        self._label(p, big.right() + 6, big.center().y(), unit, 10)
        other = "mbar" if self.unit == "torr" else "torr"
        small = QRectF(ch_rect.right() - 190, ch_rect.bottom() - 62, 80, 22)
        self._label(p, small.left(), small.top() - 10, f"WRG ({unit_label(other)})", 9)
        txt = format_pressure(snap.inputs.pressures_torr.get(main.id, float("nan")), other) if snap else "NaN"
        self._box(p, small, [(txt, 9, False)])
        # ---- vent valve (top left)
        vents = cfg.valves_of_kind("vent")
        if vents:
            self._horizontal_valve(p, 52, ch_rect.center().y() - 40, vents[0].id, vents[0].label)
        # ---- left column: compressor / com potential + chiller
        y0 = 250
        if snap and snap.compressor_bar is not None:
            self._label(p, 22, y0, "Compressor", 10)
            self._label(p, 22, y0 + 15, "Pressure (Bar)", 10)
            self._box(p, QRectF(40, y0 + 25, 46, 22), [(f"{snap.compressor_bar:.0f}" if snap.compressor_bar > 10 else f"{snap.compressor_bar:.1f}", 10, False)])
        for ea in cfg.extra_analog[:1]:
            v = snap.inputs.extra_analog.get(ea["id"], float("nan")) if snap else float("nan")
            self._label(p, 22, y0 + 65, ea["label"], 9.5)
            self._box(p, QRectF(30, y0 + 75, 66, 22), [(f"{v:.3f}", 10, False)])
        self._chiller(p, 18, 372)
        cbox = QRectF(4, 452, 116, 30)
        ctext = snap.chiller_text if snap else "Chiller is Off"
        self._box(p, cbox, [(ctext, 10, False)])
        # ---- turbo branches (one turbo: the Main_V4.4 layout; several: compact columns, VC100 style)
        for i, tc in enumerate(turbos):
            x = xs[i]
            gate = cfg.valves[tc.gate_valve]
            tvv = cfg.valves[tc.turbo_valve]
            tv = snap.turbos.get(tc.id) if snap else None
            speed_txt = "–" if (tv and not tv.has_speed) else f"{(tv.speed_pct if tv else 0):.0f}"
            st_txt = tv.status.label if tv else "Turbo Off"
            if tv and not tv.in_use:
                st_txt = "Turbo not in use"
            g = cfg.turbo_gauge(tc.id)
            if n == 1:
                self._pneumatic_valve(p, x, 320, gate.id, gate.label)
                self._turbo(p, x, 410, tc.id)
                self._label(p, x + 44, 352, "Speed (%):", 9.5)
                self._box(p, QRectF(x + 44, 360, 40, 20), [(speed_txt, 10, False)])
                self._box(p, QRectF(x + 44, 392, 150, 28), [(st_txt, 9.5, False)])
                if g:
                    self._box(p, QRectF(x + 40, 440, 116, 40), [(g.label, 9, False), (f"{self._pressure_text(g.id)} {unit}", 9, False)])
                    self._pipe(p, [(x + 12, 460), (x + 40, 460)])
                self._pneumatic_valve(p, x, 520, tvv.id, tvv.label)
            else:
                self._pneumatic_valve(p, x, 300, gate.id, gate.label, label_dx=20)
                self._turbo(p, x, 372, tc.id, r=30)
                self._label(p, x + 34, 358, tc.label, 9, True)
                self._label(p, x + 34, 374, "Speed (%):", 8.5)
                self._box(p, QRectF(x + 92, 365, 34, 18), [(speed_txt, 9, False)])
                self._box(p, QRectF(x - 66, 410, 132, 22), [(st_txt.replace("Turbo ", ""), 9, False)])
                if g:
                    self._box(p, QRectF(x - 58, 438, 116, 36), [(g.label, 8.5, False), (f"{self._pressure_text(g.id)} {unit}", 9, False)])
                self._pneumatic_valve(p, x, 520, tvv.id, tvv.label, label_dx=20)
        # ---- bypass valve (right)
        bps = cfg.valves_of_kind("bypass")
        if bps:
            self._pneumatic_valve(p, bypass_x, 410, bps[0].id, bps[0].label, label_dx=22, label_below=(n > 1))
        # ---- foreline gauge + primary
        fg = cfg.foreline_gauge
        if fg:
            self._box(p, QRectF(6, 536, 150, 40), [(fg.label, 9.5, False), (f"{self._pressure_text(fg.id)} {unit}", 9.5, False)])
        self._primary(p, 185, 600)
        pbox = QRectF(2, 596, 142, 30)
        self._box(p, pbox, [(snap.primary_text if snap else "Primary Pump is Off", 10, False)])
        # ---- state / mode badge (VI: Current/Target Facility State rings)
        if snap:
            badge = QRectF(ch_rect.left() - 60, 220, 250, 20)
            mode_txt = {Mode.INITIALIZE: "Initialising", Mode.ADMIN: "Admin Control Mode", Mode.MANUAL: "Manual Control Mode (interlocked)",
                        Mode.AUTO: "Auto Control Mode"}[snap.mode]
            p.setPen(QPen(TEXT))
            p.setFont(self._font(9.5, True))
            p.drawText(QRectF(280, 215, 400, 18), Qt.AlignLeft | Qt.AlignVCenter, mode_txt)
            if snap.mode == Mode.AUTO:
                p.setFont(self._font(8.5, False))
                p.drawText(QRectF(280, 232, 410, 18), Qt.AlignLeft | Qt.AlignVCenter,
                           f"State: {snap.current.label}  →  Target: {snap.target.label}   (substate {snap.substate})")
            if snap.hold_remaining_s > 0 and snap.hold_reason:
                p.setPen(QPen(QColor("#8A4B00")))
                p.setFont(self._font(8.5, True))
                p.drawText(QRectF(280, 249, 410, 18), Qt.AlignLeft | Qt.AlignVCenter,
                           f"{snap.hold_reason} – {snap.hold_remaining_s:.0f} s")
