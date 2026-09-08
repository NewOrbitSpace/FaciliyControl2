"""Small LabVIEW-look widgets: LED, status box, error cluster, grey 3-D buttons."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QTextEdit, QVBoxLayout, QWidget)

from ..model import ErrorCluster

LV_BG = "#F0F0F0"
LV_PANEL = "#D9D9D9"
LV_GRID = "#E3E3E3"
LV_TEXT = "#000000"
LV_INDICATOR_BG = "#DCDCDC"
LV_FONT = "Segoe UI, Arial, Helvetica, sans-serif"

BUTTON_STYLE = """
QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #FDFDFD, stop:1 #CFCFCF);
              border: 1px solid #7A7A7A; border-radius: 3px; padding: 4px 10px; color: black; font-size: 13px; }
QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #FFFFFF, stop:1 #DDDDDD); }
QPushButton:pressed { background: #B8B8B8; }
QPushButton:disabled { color: #8A8A8A; background: #E6E6E6; }
"""


class LED(QWidget):
    def __init__(self, diameter: int = 18, on_color: str = "#00C800", off_color: str = "#3C6E3C", parent=None):
        super().__init__(parent)
        self._on = False
        self._d = diameter
        self.on_color, self.off_color = on_color, off_color
        self.setFixedSize(diameter + 4, diameter + 4)

    def setOn(self, on: bool):
        if on != self._on:
            self._on = on
            self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#404040"), 1))
        p.setBrush(QBrush(QColor(self.on_color if self._on else self.off_color)))
        p.drawEllipse(2, 2, self._d, self._d)


class StatusBox(QLabel):
    """Grey LabVIEW string indicator with a coloured background (Primary/Chiller/Turbo status)."""

    def __init__(self, text: str = "", parent=None, width: int = 150):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(width)
        self.setFrameShape(QFrame.Panel)
        self.setFrameShadow(QFrame.Sunken)
        self.setColor("#C0C0C0")

    def setColor(self, color: str):
        self.setStyleSheet(f"QLabel {{ background: {color}; color: black; border: 1px solid #808080; padding: 3px; font-size: 13px; }}")


class ValueBox(QLabel):
    def __init__(self, text: str = "", parent=None, width: int = 90, big: bool = False):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(width)
        size = 34 if big else 13
        self.setStyleSheet(f"QLabel {{ background: {LV_INDICATOR_BG}; color: black; border: 1px solid #909090; padding: 2px 6px; font-size: {size}px; }}")


class ErrorClusterWidget(QFrame):
    """The LabVIEW error cluster: code / status LED / source."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Panel)
        self.setFrameShadow(QFrame.Raised)
        self.setStyleSheet(f"QFrame {{ background: {LV_BG}; }} QLabel {{ font-size: 12px; }}")
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 4, 8, 6)
        grid.addWidget(QLabel("code"), 0, 0)
        grid.addWidget(QLabel("status"), 0, 1)
        self.code = QLineEdit("0")
        self.code.setReadOnly(True)
        self.code.setStyleSheet(f"background: {LV_INDICATOR_BG}; border: 1px solid #909090;")
        grid.addWidget(self.code, 1, 0)
        self.led = LED(16, on_color="#FF0000", off_color="#00C800")
        grid.addWidget(self.led, 1, 1, alignment=Qt.AlignCenter)
        grid.addWidget(QLabel("source"), 2, 0, 1, 2)
        self.source = QTextEdit()
        self.source.setReadOnly(True)
        self.source.setMinimumHeight(70)
        self.source.setMaximumHeight(110)
        self.source.setStyleSheet(f"background: {LV_INDICATOR_BG}; border: 1px solid #909090; font-size: 12px;")
        grid.addWidget(self.source, 3, 0, 1, 2)

    def set_error(self, err: ErrorCluster):
        self.code.setText(str(err.code))
        self.led.setOn(err.status)
        txt = err.source if err.status else ""
        if self.source.toPlainText() != txt:
            self.source.setPlainText(txt)


def lv_button(text: str, width: int | None = None, height: int = 30) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(BUTTON_STYLE)
    b.setMinimumHeight(height)
    if width:
        b.setMinimumWidth(width)
    return b
