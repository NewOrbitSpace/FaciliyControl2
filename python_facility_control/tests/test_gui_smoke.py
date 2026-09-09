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
    s = h.step(3)                                              # +1 for the vent-valve confirmation
    assert s.current.name == "PUMPING_TO_ROUGH"
    win._poll(); app.processEvents()
    assert "Pumping to Rough" in win.state_label.text()
    # unit switch changes plot label and threshold display
    win.unit_combo.setCurrentIndex(1)
    app.processEvents()
    assert "mBar" in win.plots.p_top.getAxis("left").labelText
    assert win.threshold.value() == pytest.approx(0.2, rel=1e-3)   # 2e-1 mBar default
    img = win.grab()
    assert not img.isNull() and img.width() > 800
    win.close()


def test_vc100_window_shows_three_turbos_and_vi_buttons(app):
    from facility_control.config import CONFIG_DIR, load_config
    vcfg = load_config(str(CONFIG_DIR / "facility_vc100.yaml"))
    vcfg.simulation.time_scale = 200.0
    h = Harness(vcfg)
    win = MainWindow(vcfg, h.ctl, sim_backend=h.backend)
    win.show()
    h.step(3)
    win._poll(); app.processEvents()
    labels = [b.text() for b in win.auto_buttons]
    assert labels == ["Pump to Rough", "Pump to High Vac", "Overnight Pump", "Vent", "Shut Off once rough"]   # VI page 0
    assert "Turbo warning (contacts)" in [cb.text() for cb in win.fault_boxes.values()]
    assert len(win.schematic.cfg.turbos) == 3 and win.threshold.value() == pytest.approx(0.25, rel=1e-3)   # 0.25 mBar
    win.auto_buttons[4].click()                                # Shut Off once rough -> vent question -> Rough, target Off
    s = h.step(3)
    assert s.current.name == "PUMPING_TO_ROUGH" and s.target.name == "FACILITY_OFF"
    win._poll(); app.processEvents()
    assert [b.text() for b in win.auto_buttons][-1] == "Shut Off"                                       # VI page 1
    img = win.grab()
    assert not img.isNull()
    win.close()
