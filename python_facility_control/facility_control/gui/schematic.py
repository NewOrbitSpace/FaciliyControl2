"""The pumping diagram (P&ID) of the front panel, drawn from the facility config.

Layout (2026-10-13 redesign, any number of turbos):

    header      facility name | mode pill | Auto state / target | settle-wait notice
    chamber     a horizontal vacuum vessel with dished heads; the chamber pressure display sits in it,
                the vent valve on the left head, the bypass line leaves the right head
    branches    one port per turbo on the underside of the vessel: gate valve -> turbo pump -> status
                card (status word + speed bar).  Turbos backed through the SAME turbo valve (VC100:
                Turbo 1 + 4, 2 + 5, 3 + 6) stand side by side and join above that one turbo valve and
                its one backing gauge
    foreline    the roughing manifold along the bottom: foreline gauge, primary pump (with run-hour meter
                and, if fitted, drive frequency), exhaust; chiller and compressed air as utility cards

Colours keep the VI's meaning (grey = off/closed, green = on/open, red = error; turbos: blue
spinning up, purple spinning down, amber standby, orange warning), in a calmer palette.  In Admin /
Manual mode the symbols are clickable and emit `commandRequested('valve:<id>' | 'primary' |
'chiller' | 'turbo:<id>')`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF,
                           QRadialGradient)
from PySide6.QtWidgets import QWidget

from ..config import FacilityConfig
from ..model import (COLOR_ERROR, COLOR_OFF, COLOR_ON, COLOR_SPIN_DOWN, COLOR_SPIN_UP, COLOR_STANDBY,
                     COLOR_WARNING, Mode, Snapshot, contact_summary)
from ..units import format_pressure, unit_label

# ---------------------------------------------------------------------------- palette
BG = QColor("#F3F5F7")
INK = QColor("#1F2328")            # primary text
INK_2 = QColor("#5B6470")          # secondary text
LINE = QColor("#3D444D")           # symbol outlines
PIPE = QColor("#5C6670")
PIPE_HI = QColor("#8C96A0")
CARD = QColor("#FFFFFF")
CARD_EDGE = QColor("#C9D1D9")
STEEL_1, STEEL_2, STEEL_3 = QColor("#E9EDF1"), QColor("#C3CBD3"), QColor("#9AA4AE")

# the VI's state colours -> the same meaning, calmer shades
_DISPLAY = {
    COLOR_OFF: "#C9CED4", COLOR_ON: "#3FB950", COLOR_ERROR: "#D73A49", COLOR_SPIN_UP: "#2F81F7",
    COLOR_SPIN_DOWN: "#8E7CC3", COLOR_STANDBY: "#E3B341", COLOR_WARNING: "#F0883E",
}
MODE_COLORS = {Mode.AUTO: "#2F81F7", Mode.ADMIN: "#B35900", Mode.MANUAL: "#7E57C2", Mode.INITIALIZE: "#6E7781"}


def _c(vi_color: str) -> QColor:
    return QColor(_DISPLAY.get(vi_color.upper(), vi_color))


@dataclass
class Hit:
    rect: QRectF
    target: str
    tooltip: str


# ---------------------------------------------------------------------------- geometry constants
HEADER_H = 34.0
VESSEL_TOP, VESSEL_BOT = 56.0, 196.0
GATE_Y = 240.0
TURBO_Y = 304.0
TURBO_R = 25.0
CARD_Y = 336.0            # turbo status card top
CARD_H = 52.0
JOIN_Y = 402.0            # turbos of one turbo valve join here
GAUGE_Y = 436.0
TV_Y = 488.0              # turbo valve
MANIFOLD_Y = 538.0
COL_W = 106.0             # one turbo column
GROUP_GAP = 30.0          # extra space between groups
X0 = 250.0                # first turbo column
LEFT_COL = 18.0           # utility cards
READOUT_W, READOUT_H = 180.0, 50.0   # gauge readout cards (2026-10-27: larger for legibility)
VALUE_PT = 14.5           # pressure value font in the readout cards
LABEL_PT = 9.5            # valve / small labels


class SchematicWidget(QWidget):
    commandRequested = Signal(str)

    W, H = 690.0, 640.0   # virtual canvas (W grows with the number of turbos)

    def __init__(self, cfg: FacilityConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.snap: Optional[Snapshot] = None
        self.unit = cfg.pressure_unit_default
        self.hits: List[Hit] = []
        self.W = max(type(self).W, self._layout()[2] + 120.0)
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
                if self.clickable() and not h.target.startswith("info:"):   # info: tooltip only
                    self.commandRequested.emit(h.target)
                return

    def mouseMoveEvent(self, ev):
        pos = self._to_virtual(ev.position())
        tip = ""
        for h in self.hits:
            if h.rect.contains(pos):
                tip = h.tooltip if (self.clickable() or h.target.startswith("info:")) \
                    else f"{h.tooltip} (switch to Admin/Manual mode to command)"
                break
        self.setToolTip(tip)
        clickable_hit = any(h.rect.contains(pos) and not h.target.startswith("info:") for h in self.hits)
        self.setCursor(Qt.PointingHandCursor if (clickable_hit and self.clickable()) else Qt.ArrowCursor)

    def _scale(self) -> Tuple[float, float, float]:
        s = min(self.width() / self.W, self.height() / self.H)
        ox = (self.width() - self.W * s) / 2
        oy = (self.height() - self.H * s) / 2
        return s, ox, oy

    def _to_virtual(self, p: QPointF) -> QPointF:
        s, ox, oy = self._scale()
        return QPointF((p.x() - ox) / s, (p.y() - oy) / s)

    # ------------------------------------------------------------------ layout
    def _groups(self) -> List[Tuple[str, List[int]]]:
        """Turbos grouped by the turbo valve they are backed through, in order of first appearance
        (VC100: [Turbo 1, Turbo 4], [Turbo 2, Turbo 5], [Turbo 3, Turbo 6]).  An unshared turbo is a
        group of one."""
        groups: Dict[str, List[int]] = {}
        for i, tc in enumerate(self.cfg.turbos):
            groups.setdefault(tc.turbo_valve, []).append(i)
        return list(groups.items())

    def _grouped(self) -> bool:
        return any(len(ix) > 1 for _, ix in self._groups())

    def _layout(self) -> Tuple[QRectF, List[float], float]:
        """Vessel rectangle, x of every turbo column (cfg.turbos order), x of the bypass line."""
        n = len(self.cfg.turbos)
        xs = [0.0] * n
        x = X0
        prev_single = False
        for gi, (_vid, idx) in enumerate(self._groups()):
            if gi:
                x += COL_W + GROUP_GAP + (READOUT_W - 48.0 if prev_single else 0.0)   # room for a lone turbo's gauge card
            prev_single = len(idx) == 1
            for k, i in enumerate(idx):
                if k:
                    x += COL_W
                xs[i] = x
        last = max(xs) if xs else X0
        # the vessel spans every port with room for its dished heads; never narrower than the readout
        left, right = 150.0, max(last + 70.0, 640.0)
        vessel = QRectF(left, VESSEL_TOP, right - left, VESSEL_BOT - VESSEL_TOP)
        bypass_x = vessel.right() + 46.0
        return vessel, xs, bypass_x

    # ------------------------------------------------------------------ painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.fillRect(self.rect(), BG)
        s, ox, oy = self._scale()
        p.translate(ox, oy)
        p.scale(s, s)
        self.hits = []
        self._draw(p)

    # ---- primitives ---------------------------------------------------------
    def _font(self, size: float, bold: bool = False, mono: bool = False) -> QFont:
        f = QFont("Consolas" if mono else "Segoe UI")
        if not mono:
            f.setFamilies(["Segoe UI", "Arial", "Helvetica"])
        else:
            f.setFamilies(["Consolas", "DejaVu Sans Mono", "Courier New"])
        f.setPointSizeF(size)
        f.setBold(bold)
        return f

    def _text(self, p: QPainter, rect: QRectF, txt: str, size: float = 9.0, bold: bool = False,
              color: QColor = INK, align=Qt.AlignLeft | Qt.AlignVCenter, mono: bool = False):
        p.setPen(QPen(color))
        p.setFont(self._font(size, bold, mono))
        p.drawText(rect, align, txt)

    def _label(self, p: QPainter, x: float, y: float, txt: str, size: float = 9.0, bold=False,
               align=Qt.AlignLeft, color: QColor = INK):
        r = QRectF(x, y - 8, 200, 16)
        if align == Qt.AlignRight:
            r = QRectF(x - 200, y - 8, 200, 16)
        elif align == Qt.AlignHCenter:
            r = QRectF(x - 100, y - 8, 200, 16)
        self._text(p, r, txt, size, bold, color, align | Qt.AlignVCenter)

    def _card(self, p: QPainter, rect: QRectF, accent: Optional[QColor] = None, radius: float = 5.0):
        p.setPen(QPen(CARD_EDGE, 1))
        p.setBrush(QBrush(CARD))
        p.drawRoundedRect(rect, radius, radius)
        if accent is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(accent))
            path = QPainterPath()
            path.addRoundedRect(QRectF(rect.left(), rect.top(), 4, rect.height()), 2, 2)
            p.drawPath(path)

    def _readout(self, p: QPainter, rect: QRectF, caption: str, value: str, unit: str = "",
                 accent: Optional[QColor] = None):
        """Instrument card: small caption, value in a fixed-width face, unit."""
        self._card(p, rect, accent)
        pad = 8.0
        self._text(p, QRectF(rect.left() + pad, rect.top() + 2, rect.width() - 2 * pad, 16), caption, 9.0,
                   False, INK_2)
        self._text(p, QRectF(rect.left() + pad, rect.top() + 17, rect.width() - 2 * pad, rect.height() - 19),
                   f"{value} {unit}".rstrip(), VALUE_PT, True, INK, Qt.AlignLeft | Qt.AlignVCenter, mono=True)

    def _pipe(self, p: QPainter, pts: List[Tuple[float, float]], width: float = 5.0):
        path = QPainterPath(QPointF(*pts[0]))
        for pt in pts[1:]:
            path.lineTo(QPointF(*pt))
        p.setBrush(Qt.NoBrush)
        pen = QPen(PIPE, width)
        pen.setCapStyle(Qt.FlatCap)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.drawPath(path)
        pen = QPen(PIPE_HI, 1.2)                          # thin highlight: reads as a tube, not a line
        p.setPen(pen)
        p.drawPath(path)

    def _flange(self, p: QPainter, x: float, y: float, vertical: bool = True):
        p.setPen(QPen(LINE, 1))
        p.setBrush(QBrush(STEEL_2))
        if vertical:
            p.drawRect(QRectF(x - 10, y - 2.5, 20, 5))
        else:
            p.drawRect(QRectF(x - 2.5, y - 10, 5, 20))

    def _valve_color(self, vid: str) -> QColor:
        if not self.snap:
            return _c(COLOR_OFF)
        cmd = self.snap.commands.valves.get(vid, False)
        rd = self.snap.inputs.valve_reads.get(vid, False)
        if self.snap.valve_errors.get(vid) and cmd != rd:
            return _c(COLOR_ERROR)
        return _c(COLOR_ON if rd else COLOR_OFF)

    def _valve(self, p: QPainter, cx: float, cy: float, vid: str, label: str, vertical: bool = True,
               label_side: str = "right", size: float = 13.0):
        """ISA bow-tie valve with a pneumatic (dome) actuator; fill = state colour."""
        col = self._valve_color(vid)
        s = size
        p.setPen(QPen(LINE, 1.4))
        p.setBrush(QBrush(col))
        if vertical:
            p.drawPolygon(QPolygonF([QPointF(cx - s, cy - s), QPointF(cx + s, cy - s), QPointF(cx, cy)]))
            p.drawPolygon(QPolygonF([QPointF(cx - s, cy + s), QPointF(cx + s, cy + s), QPointF(cx, cy)]))
            # actuator to the side: stem + dome
            p.drawLine(QPointF(cx, cy), QPointF(cx - s - 7, cy))
            dome = QRectF(cx - s - 19, cy - 8, 12, 16)
            p.drawChord(dome, 90 * 16, 180 * 16)
            p.drawLine(dome.center() + QPointF(0, -8), dome.center() + QPointF(0, 8))
            hit = QRectF(cx - s - 20, cy - s - 2, 2 * s + 22, 2 * s + 4)
        else:
            p.drawPolygon(QPolygonF([QPointF(cx - s, cy - s), QPointF(cx - s, cy + s), QPointF(cx, cy)]))
            p.drawPolygon(QPolygonF([QPointF(cx + s, cy - s), QPointF(cx + s, cy + s), QPointF(cx, cy)]))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy - s - 7))
            dome = QRectF(cx - 8, cy - s - 19, 16, 12)
            p.drawChord(dome, 180 * 16, 180 * 16)
            p.drawLine(QPointF(dome.left(), dome.bottom()), QPointF(dome.right(), dome.bottom()))
            hit = QRectF(cx - s - 2, cy - s - 20, 2 * s + 4, 2 * s + 22)
        if label_side == "right":
            self._label(p, cx + s + 6, cy, label, LABEL_PT)
        elif label_side == "left":
            self._label(p, cx - s - 24, cy, label, LABEL_PT, align=Qt.AlignRight)
        elif label_side == "above":
            self._label(p, cx, cy - s - 28, label, LABEL_PT, align=Qt.AlignHCenter)
        elif label_side == "below":
            self._label(p, cx, cy + s + 10, label, LABEL_PT, align=Qt.AlignHCenter)
        self.hits.append(Hit(hit, f"valve:{vid}", f"{label}: click to open/close"))

    def _turbo(self, p: QPainter, cx: float, cy: float, tid: str, r: float = TURBO_R):
        """Turbomolecular pump: housing ring + rotor blades, fill = the VI's turbo status colour."""
        tv = self.snap.turbos.get(tid) if self.snap else None
        col = _c(tv.color) if tv else _c(COLOR_OFF)
        in_use = not (tv and not tv.in_use)
        if not in_use:
            col = QColor("#E1E4E8")
        pen = QPen(LINE, 1.8)
        if not in_use:
            pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        grad = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.3), r * 1.3)
        grad.setColorAt(0.0, col.lighter(125))
        grad.setColorAt(1.0, col)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), r, r)
        # rotor: six swept blades around a hub
        p.setPen(QPen(LINE, 1.2))
        p.setBrush(QBrush(QColor(255, 255, 255, 170)))
        for k in range(6):
            a = k * math.pi / 3
            blade = QPainterPath(QPointF(cx + math.cos(a) * r * 0.28, cy + math.sin(a) * r * 0.28))
            b1 = a + 0.55
            blade.quadTo(QPointF(cx + math.cos(a + 0.25) * r * 0.75, cy + math.sin(a + 0.25) * r * 0.75),
                         QPointF(cx + math.cos(b1) * r * 0.82, cy + math.sin(b1) * r * 0.82))
            blade.lineTo(QPointF(cx + math.cos(a + 0.2) * r * 0.82, cy + math.sin(a + 0.2) * r * 0.82))
            blade.quadTo(QPointF(cx + math.cos(a) * r * 0.55, cy + math.sin(a) * r * 0.55),
                         QPointF(cx + math.cos(a) * r * 0.28, cy + math.sin(a) * r * 0.28))
            p.drawPath(blade)
        p.setBrush(QBrush(STEEL_1))
        p.drawEllipse(QPointF(cx, cy), r * 0.26, r * 0.26)
        tcfg = self.cfg.turbo(tid)
        tip = ("Pfeiffer turbo: click for speed control (Off / Full speed / Standby / Set speed)"
               if tcfg.speed_settable else "Turbo pump: click to start/stop/standby")
        self.hits.append(Hit(QRectF(cx - r, cy - r, 2 * r, 2 * r), f"turbo:{tid}", tip))

    def _gauge_bubble(self, p: QPainter, cx: float, cy: float, tag: str = "PT"):
        """ISA instrument bubble (pressure transmitter)."""
        p.setPen(QPen(LINE, 1.3))
        p.setBrush(QBrush(CARD))
        p.drawEllipse(QPointF(cx, cy), 11, 11)
        p.drawLine(QPointF(cx - 11, cy), QPointF(cx + 11, cy))
        self._text(p, QRectF(cx - 11, cy - 10, 22, 10), tag, 6, True, INK, Qt.AlignCenter)

    def _primary(self, p: QPainter, cx: float, cy: float, r: float = 26):
        """Rotary / dry roughing pump: circle with the flow triangle (ISA pump symbol)."""
        col = _c(COLOR_OFF)
        if self.snap:
            st = int(self.snap.primary_status)
            col = _c({0: COLOR_OFF, 1: COLOR_ON, 2: COLOR_ERROR}[st])
            if st != 2:
                if self.snap.primary_running:
                    col = _c(COLOR_ON)
                elif self.snap.primary_phase in ("powering", "stopping"):
                    col = _c(COLOR_SPIN_UP)      # powered, not turning yet / running down
        p.setPen(QPen(LINE, 1.8))
        grad = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.3), r * 1.3)
        grad.setColorAt(0.0, col.lighter(125))
        grad.setColorAt(1.0, col)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(QBrush(QColor(255, 255, 255, 190)))
        p.drawPolygon(QPolygonF([QPointF(cx - r * 0.55, cy - r * 0.62), QPointF(cx - r * 0.55, cy + r * 0.62),
                                 QPointF(cx + r * 0.75, cy)]))
        self.hits.append(Hit(QRectF(cx - r, cy - r, 2 * r, 2 * r), "primary", "Primary pump: click to start/stop"))

    def _chiller(self, p: QPainter, rect: QRectF):
        col = _c(COLOR_OFF)
        if self.snap:
            col = _c({0: COLOR_OFF, 1: COLOR_ON, 2: COLOR_ERROR}[int(self.snap.chiller_status)])
        self._card(p, rect, col)
        icon = QRectF(rect.left() + 10, rect.top() + 8, 30, 30)
        p.setPen(QPen(LINE, 1.3))
        p.setBrush(QBrush(col))
        p.drawRoundedRect(icon, 4, 4)
        cx, cy = icon.center().x(), icon.center().y()
        p.setPen(QPen(INK, 1.4))
        for k in range(6):
            a = k * math.pi / 3
            p.drawLine(QPointF(cx, cy), QPointF(cx + math.cos(a) * 11, cy + math.sin(a) * 11))
            for sgn in (-1, 1):
                b = a + sgn * math.pi / 5
                q = QPointF(cx + math.cos(a) * 7, cy + math.sin(a) * 7)
                p.drawLine(q, q + QPointF(math.cos(b) * 4, math.sin(b) * 4))
        self._text(p, QRectF(rect.left() + 48, rect.top() + 6, rect.width() - 52, 14), self.cfg.chiller.label,
                   9.5, True)
        txt = self.snap.chiller_text if self.snap else "Chiller is Off"
        self._text(p, QRectF(rect.left() + 48, rect.top() + 22, rect.width() - 52, 16),
                   txt.replace("Chiller is ", ""), 9.0, False, INK_2)
        self.hits.append(Hit(rect, "chiller", "Chiller: click to start/stop"))

    def _pressure_text(self, gid: str) -> str:
        if not self.snap:
            return "NaN"
        return format_pressure(self.snap.inputs.pressures_torr.get(gid, float("nan")), self.unit)

    # ---- the vessel ---------------------------------------------------------
    def _vessel(self, p: QPainter, r: QRectF):
        """Horizontal cylindrical vacuum vessel with 2:1 dished heads and a steel finish."""
        head = r.height() * 0.32
        path = QPainterPath()
        path.moveTo(r.left() + head, r.top())
        path.lineTo(r.right() - head, r.top())
        path.arcTo(QRectF(r.right() - 2 * head, r.top(), 2 * head, r.height()), 90, -180)
        path.lineTo(r.left() + head, r.bottom())
        path.arcTo(QRectF(r.left(), r.top(), 2 * head, r.height()), 270, -180)
        path.closeSubpath()
        grad = QLinearGradient(QPointF(0, r.top()), QPointF(0, r.bottom()))
        grad.setColorAt(0.0, STEEL_2)
        grad.setColorAt(0.18, STEEL_1)
        grad.setColorAt(0.55, STEEL_2)
        grad.setColorAt(1.0, STEEL_3)
        p.setPen(QPen(LINE, 2))
        p.setBrush(QBrush(grad))
        p.drawPath(path)
        # head seams + stiffening rings
        p.setPen(QPen(QColor(61, 68, 77, 120), 1.2))
        for x in (r.left() + head, r.right() - head):
            p.drawLine(QPointF(x, r.top() + 1), QPointF(x, r.bottom() - 1))

    # ---- main drawing -------------------------------------------------------
    def _draw(self, p: QPainter):
        cfg, snap = self.cfg, self.snap
        unit = unit_label(self.unit)
        turbos = cfg.turbos
        vessel, xs, bypass_x = self._layout()
        groups = self._groups()
        gcx = {vid: sum(xs[i] for i in idx) / len(idx) for vid, idx in groups}
        pump_x = 120.0
        right_end = bypass_x

        # ======== pipes (under everything)
        for vid, idx in groups:
            cx = gcx[vid]
            for i in idx:
                self._pipe(p, [(xs[i], vessel.bottom()), (xs[i], JOIN_Y if len(idx) > 1 else TV_Y)])
            if len(idx) > 1:
                self._pipe(p, [(min(xs[i] for i in idx) - 2.5, JOIN_Y), (max(xs[i] for i in idx) + 2.5, JOIN_Y)])
            self._pipe(p, [(cx, JOIN_Y if len(idx) > 1 else TV_Y), (cx, MANIFOLD_Y)])
        bps = cfg.valves_of_kind("bypass")
        if bps:
            self._pipe(p, [(vessel.right() - 4, vessel.center().y()), (bypass_x, vessel.center().y()),
                           (bypass_x, MANIFOLD_Y)])
        self._pipe(p, [(pump_x, MANIFOLD_Y), (right_end + 2.5, MANIFOLD_Y)])
        self._pipe(p, [(pump_x, MANIFOLD_Y), (pump_x, MANIFOLD_Y + 34)])
        vents = cfg.valves_of_kind("vent")
        vent_y = vessel.top() + 34
        if vents:
            self._pipe(p, [(vessel.left() + 8, vent_y), (vessel.left() - 64, vent_y)])

        # ======== header
        self._header(p, max(self.W, right_end + 40))

        # ======== vessel + chamber readout
        self._vessel(p, vessel)
        for x in xs:
            self._flange(p, x, vessel.bottom() + 2)
        if bps:
            self._flange(p, vessel.right() + 2, vessel.center().y(), vertical=False)
        main = cfg.main_gauge
        ro = QRectF(vessel.center().x() - 150, vessel.top() + 18, 300, 98)
        self._card(p, ro, radius=6)
        self._text(p, QRectF(ro.left() + 12, ro.top() + 4, ro.width() - 24, 16),
                   f"CHAMBER  ·  {main.label.upper()}", 9.5, True, INK_2)
        self._text(p, QRectF(ro.left() + 12, ro.top() + 20, ro.width() - 84, 52),
                   self._pressure_text(main.id), 28, True, INK, Qt.AlignLeft | Qt.AlignVCenter, mono=True)
        self._text(p, QRectF(ro.right() - 76, ro.top() + 20, 66, 52), unit, 14, False, INK_2,
                   Qt.AlignRight | Qt.AlignVCenter)
        other = "mbar" if self.unit == "torr" else "torr"
        txt = format_pressure(snap.inputs.pressures_torr.get(main.id, float("nan")), other) if snap else "NaN"
        self._text(p, QRectF(ro.left() + 12, ro.bottom() - 24, ro.width() - 24, 20),
                   f"= {txt} {unit_label(other)}", 10.5, False, INK_2, mono=True)

        # ======== vent (left head) and bypass (right line)
        if vents:
            self._valve(p, vessel.left() - 36, vent_y, vents[0].id, vents[0].label, vertical=False, label_side="above")
            ax = vessel.left() - 64
            p.setPen(QPen(LINE, 1.4))
            p.drawLine(QPointF(ax, vent_y), QPointF(ax - 10, vent_y))
            p.drawPolyline(QPolygonF([QPointF(ax - 4, vent_y - 6), QPointF(ax - 11, vent_y), QPointF(ax - 4, vent_y + 6)]))
            self._label(p, ax - 14, vent_y, "ATM", 7.5, True, Qt.AlignRight, INK_2)
        if bps:
            self._valve(p, bypass_x, (vessel.bottom() + MANIFOLD_Y) / 2 + 20, bps[0].id, bps[0].label,
                        label_side="right")

        # ======== turbo branches
        th_reached = cfg.thresholds.turbo_speed_reached_pct
        for i, tc in enumerate(turbos):
            x = xs[i]
            gate = cfg.valves[tc.gate_valve]
            short = gate.label[:-len(" Valve")] if gate.label.endswith(" Valve") else gate.label
            self._valve(p, x, GATE_Y, gate.id, short, label_side="right")
            self.hits[-1].tooltip = f"{gate.label}: click to open/close"
            self._turbo(p, x, TURBO_Y, tc.id)
            tv = snap.turbos.get(tc.id) if snap else None
            st_txt = tv.status.label.replace("Turbo ", "") if tv else "Off"
            sp = tv.speed_set_pct if (tv and tv.motor_read) else None
            if sp is not None and tv.status.name in ("SPEED_REACHED", "SPINNING_UP", "SPINNING_DOWN"):
                st_txt = {"SPEED_REACHED": f"At {sp:.0f}% set", "SPINNING_UP": f"Up to {sp:.0f}%",
                          "SPINNING_DOWN": f"Down to {sp:.0f}%"}[tv.status.name]
            st_col = INK_2
            flagged = bool(tv and tv.not_full_speed)       # Auto high vac: not at full speed in time
            if tv and tv.comm_silent_s > 0 and not tv.comm_lost:     # RS-485: slow, not yet lost
                st_txt, st_col = f"No reply {tv.comm_silent_s:.0f} s", QColor("#B35900")
            if tv and tv.isolated:                          # taken out by the controller
                st_txt = "No reply – off" if "RS-485" in tv.isolated else "Gate stuck!"
                st_col = _c(COLOR_ERROR)
            elif tv and tv.comm_lost:
                st_txt, st_col = "No RS-485 reply", _c(COLOR_ERROR)
            if tv and not tv.in_use:
                st_txt = "Not in use"
            accent = _c(tv.color) if tv else _c(COLOR_OFF)
            card = QRectF(x - COL_W / 2 + 8, CARD_Y, COL_W - 16, CARD_H)
            self._card(p, card, accent)
            self._text(p, QRectF(card.left() + 9, card.top() + 2, card.width() - 12, 18), tc.label, 10.0, True)
            self._text(p, QRectF(card.left() + 7, card.top() + 32, card.width() - 9, 18), st_txt, 8.5, st_col != INK_2, st_col)
            bar = QRectF(card.left() + 9, card.top() + 23, card.width() - 50, 6)
            if tv is None or tv.has_speed:
                pct = max(0.0, min(100.0, tv.speed_pct if tv else 0.0))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor("#E6EAEF")))
                p.drawRoundedRect(bar, 3, 3)
                if pct > 0:
                    p.setBrush(QBrush(accent))
                    p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * pct / 100.0, bar.height()), 3, 3)
                p.setPen(QPen(QColor(61, 68, 77, 140), 1))
                mx = bar.left() + bar.width() * th_reached / 100.0
                p.drawLine(QPointF(mx, bar.top() - 2), QPointF(mx, bar.bottom() + 2))
                if sp is not None:                          # reduced-speed set value: a blue tick
                    p.setPen(QPen(QColor("#2F81F7"), 2))
                    sx = bar.left() + bar.width() * max(0.0, min(100.0, sp)) / 100.0
                    p.drawLine(QPointF(sx, bar.top() - 3), QPointF(sx, bar.bottom() + 3))
                self._text(p, QRectF(bar.right() + 2, card.top() + 17, card.right() - bar.right() - 6, 18),
                           f"{pct:.0f}%", 9.0, True, _c(COLOR_ERROR) if flagged else INK,
                           Qt.AlignRight | Qt.AlignVCenter, mono=True)
            else:
                # contact interface (Shimadzu TMP1 / TMP4): what its status contacts say right now
                sig = contact_summary(tv.contacts if tv else {}, bool(tv.motor_read) if tv else False,
                                      tc.params.get("at_speed_pct"), bool(tv.standby) if tv else False)
                p.setFont(self._font(8.0, True))
                sig = p.fontMetrics().elidedText(sig, Qt.ElideRight, int(card.width() - 12))
                self._text(p, QRectF(card.left() + 8, card.top() + 16, card.width() - 10, 16),
                           sig, 8.0, True, _c(COLOR_ERROR) if ("ALARM" in sig or flagged) else INK)
                self.hits.append(Hit(QRectF(card.left(), card.top() + 14, card.width(), 18), f"info:{tc.id}",
                                     "Status contacts: Accel = accelerating, Decel = decelerating, "
                                     + (f">{tc.params['at_speed_pct']:.0f}% = speed above that, "
                                        if tc.params.get("at_speed_pct") is not None else "At speed = rated speed, ")
                                     + "On/Off = motor command (no contact active)"))

        # ======== per turbo valve: backing gauge + turbo valve
        for vid, idx in groups:
            cx = gcx[vid]
            g = cfg.turbo_gauge(turbos[idx[0]].id)
            if g:
                self._pipe(p, [(cx, GAUGE_Y), (cx + 22, GAUGE_Y)], width=3)
                self._gauge_bubble(p, cx + 33, GAUGE_Y)
                self._readout(p, QRectF(cx + 47, GAUGE_Y - READOUT_H / 2, READOUT_W, READOUT_H), g.label,
                             self._pressure_text(g.id), unit)
            tvv = cfg.valves[vid]
            self._valve(p, cx, TV_Y, tvv.id, tvv.label, label_side="right")

        # ======== foreline: gauge, primary pump, exhaust, utilities
        fg = cfg.foreline_gauge
        if fg:
            gx = pump_x + 62
            self._pipe(p, [(gx, MANIFOLD_Y), (gx, MANIFOLD_Y - 22)], width=3)
            self._gauge_bubble(p, gx, MANIFOLD_Y - 33)
            self._readout(p, QRectF(gx - 118, MANIFOLD_Y - 108, READOUT_W, READOUT_H), fg.label,
                         self._pressure_text(fg.id), unit)
        self._primary(p, pump_x, MANIFOLD_Y + 34 + 26)
        ey = MANIFOLD_Y + 60
        p.setPen(QPen(LINE, 1.4))
        p.drawLine(QPointF(pump_x - 26, ey), QPointF(pump_x - 44, ey))
        p.drawPolyline(QPolygonF([QPointF(pump_x - 38, ey - 6), QPointF(pump_x - 45, ey), QPointF(pump_x - 38, ey + 6)]))
        self._label(p, pump_x - 48, ey, "EXH", 7.5, True, Qt.AlignRight, INK_2)
        # primary card: status, hours (+ frequency)
        pc = QRectF(pump_x + 36, MANIFOLD_Y + 14, 170, 84 if cfg.primary.has_frequency else 64)
        self._card(p, pc, _c(COLOR_ON) if (snap and snap.primary_running) else _c(COLOR_OFF))
        self._text(p, QRectF(pc.left() + 10, pc.top() + 3, pc.width() - 14, 17), cfg.primary.label, 9.5, True)
        self._text(p, QRectF(pc.left() + 10, pc.top() + 21, pc.width() - 14, 17),
                   (snap.primary_text if snap else "Primary Pump is Off").replace(cfg.primary.label + " ", "")
                   .replace("Primary Pump ", ""), 9, False, INK_2)
        hours = snap.primary_run_hours if snap else 0.0
        self._text(p, QRectF(pc.left() + 10, pc.top() + 40, pc.width() - 14, 18),
                   f"Run hours  {hours:,.2f} h", 9, False, INK, mono=True)
        measured = cfg.primary.has_frequency or cfg.primary.has_read
        self.hits.append(Hit(QRectF(pc.left(), pc.top() + 38, pc.width(), 20), "primary",
                             "Primary pump total operating hours – "
                             + ("counted from its read-back" if measured
                                else "counted from the run command (no pump feedback fitted)")
                             + ", kept across restarts"))
        if cfg.primary.has_frequency:
            hz = snap.primary_hz if snap else None
            on = snap.primary_frequency_enabled if snap else False
            self._text(p, QRectF(pc.left() + 10, pc.top() + 60, pc.width() - 14, 18),
                       "Drive  " + (f"{hz:.1f} Hz" if hz is not None else ("–" if on else "reader off")),
                       9, False, INK, mono=True)
            self.hits.append(Hit(QRectF(pc.left(), pc.top() + 58, pc.width(), 18), "primary",
                                 f"Primary pump drive frequency (0-{cfg.primary.frequency.max_hz:g} Hz)"
                                 if on else "Frequency reader is switched off – tick "
                                            "'Pump frequency reader connected' on the panel to use it"))
        # utilities column
        uy = VESSEL_BOT + 40
        self._chiller(p, QRectF(LEFT_COL, uy, 124, 50))
        uy += 58
        if snap and snap.compressor_bar is not None:
            low = cfg.thresholds.compressor_min_bar is not None and snap.compressor_bar < cfg.thresholds.compressor_min_bar
            self._readout(p, QRectF(LEFT_COL, uy, 124, READOUT_H), "Compressed air", f"{snap.compressor_bar:.1f}", "bar",
                          accent=_c(COLOR_ERROR) if low else _c(COLOR_ON))
            uy += READOUT_H + 12
        for ea in cfg.extra_analog[:1]:
            v = snap.inputs.extra_analog.get(ea["id"], float("nan")) if snap else float("nan")
            self._readout(p, QRectF(LEFT_COL, uy, 124, READOUT_H), ea["label"], f"{v:.3f}", "V")
            uy += READOUT_H + 12

    def _header(self, p: QPainter, width: float):
        snap, cfg = self.snap, self.cfg
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor("#E6EAEF")))
        p.drawRect(QRectF(0, 0, width, HEADER_H))
        p.setPen(QPen(CARD_EDGE, 1))
        p.drawLine(QPointF(0, HEADER_H), QPointF(width, HEADER_H))
        self._text(p, QRectF(14, 0, 330, HEADER_H), cfg.name or cfg.id, 11, True)
        if not snap:
            return
        mode_txt = {Mode.INITIALIZE: "INITIALISING", Mode.ADMIN: "ADMIN", Mode.MANUAL: "MANUAL · INTERLOCKED",
                    Mode.AUTO: "AUTO"}[snap.mode]
        f = self._font(8, True)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(mode_txt) + 22
        pill = QRectF(350, 8, tw, 18)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(MODE_COLORS[snap.mode])))
        p.drawRoundedRect(pill, 9, 9)
        self._text(p, pill, mode_txt, 8, True, QColor("#FFFFFF"), Qt.AlignCenter)
        x = pill.right() + 12
        if snap.mode == Mode.AUTO:
            txt = f"{snap.current.label}  →  {snap.target.label}"
            self._text(p, QRectF(x, 0, 360, HEADER_H), txt, 9, True)
            p.setFont(self._font(9, True))
            x += p.fontMetrics().horizontalAdvance(txt) + 10
            self._text(p, QRectF(x, 0, 120, HEADER_H), f"substate {snap.substate}", 8, False, INK_2)
            x += 90
        if snap.hold_remaining_s > 0 and snap.hold_reason:
            avail = max(200.0, width - x - 10)
            p.setFont(self._font(8.5, True))
            txt = p.fontMetrics().elidedText(f"{snap.hold_reason}", Qt.ElideRight, int(avail - 60))
            self._text(p, QRectF(x, 0, avail, HEADER_H), f"Waiting: {txt} – {snap.hold_remaining_s:.0f} s",
                       8.5, True, QColor("#9A6700"))
