"""Headless GUI smoke test: window builds, updates from snapshots, buttons post requests."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from facility_control.gui.main_window import MainWindow  # noqa: E402
from facility_control.model import Mode  # noqa: E402
from tests.conftest import Harness  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_main_window_renders_and_reacts(app, cfg):
    h = Harness(cfg)
    win = MainWindow(cfg, h.ctl, sim_backend=h.backend)
    win.show()
    h.step(3)
    win._poll()
    app.processEvents()
    assert win.mode_label.text() == "Auto Control Mode"
    assert len(win.auto_buttons) == 4                          # Facility Off page
    win.auto_buttons[1].click()                                # Pump to High Vac
    s = h.step(2)
    assert s.current.name == "PUMPING_TO_ROUGH"
    win._poll(); app.processEvents()
    assert "Pumping to Rough" in win.state_label.text()
    # unit switch changes plot label and threshold display
    win.unit_combo.setCurrentIndex(1)
    app.processEvents()
    assert "mBar" in win.plots.p_top.getAxis("left").labelText
    assert win.threshold.value() == pytest.approx(0.25 / 0.7500616827, rel=1e-3)
    img = win.grab()
    assert not img.isNull() and img.width() > 800
    win.close()
