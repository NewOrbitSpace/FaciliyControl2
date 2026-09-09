"""Render the GUI offscreen and save PNG screenshots at a few points of a simulated Auto cycle.

    QT_QPA_PLATFORM=offscreen python tools/screenshot_gui.py out_dir [config/facility_vc100.yaml]
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from facility_control.config import load_config  # noqa: E402
from facility_control.controller import AutoAnswerDialogs, Controller  # noqa: E402
from facility_control.hal.sim_backend import SimBackend  # noqa: E402
from facility_control.gui.main_window import MainWindow  # noqa: E402
from facility_control.model import Mode  # noqa: E402


def main(out_dir: str, config: str = None):
    os.makedirs(out_dir, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    cfg = load_config(config)
    cfg.simulation.time_scale = 40.0
    backend = SimBackend(cfg)
    dialogs = AutoAnswerDialogs()
    ctl = Controller(cfg, backend, dialogs=dialogs, initial_mode=Mode.AUTO)
    win = MainWindow(cfg, ctl, sim_backend=backend)
    win.resize(1440, 1000)
    win.show()
    ctl.start()

    def pump(seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    pump(1.5)
    win.grab().save(os.path.join(out_dir, "01_facility_off.png"))
    dialogs.answers.put(True)                      # "Is the manual vent valve closed?" -> Yes
    ctl.request_auto_button("pump_to_high_vac")
    pump(3.0)
    win.grab().save(os.path.join(out_dir, "02_pumping_to_rough.png"))
    # wait until high vac (bounded)
    t0 = time.time()
    while time.time() - t0 < 120:
        pump(0.5)
        s = ctl.snapshot()
        if s.current.name == "PUMPING_TO_HIGH_VAC" and all(tv.status.name == "SPEED_REACHED" for tv in s.turbos.values()):
            break
    pump(2.0)
    win.grab().save(os.path.join(out_dir, "03_high_vac.png"))
    # inject a fault -> error + safemode
    backend.set_fault("turbo_error", True)
    pump(2.0)
    win.grab().save(os.path.join(out_dir, "04_error.png"))       # shutdown in progress: loop frozen (gate 9 s + chiller 10 s + primary 0.5 s)
    t0 = time.time()
    while time.time() - t0 < 30 and (ctl.snapshot().loop_blocked or ctl.snapshot().hold_remaining_s > 0):
        pump(0.2)
    win.unit_combo.setCurrentIndex(1)
    pump(1.0)
    win.grab().save(os.path.join(out_dir, "05_mbar.png"))
    # Admin: a chiller command freezes the loop for 10 s (VI behaviour) – the panel shows the wait
    ctl.request_admin_mode()
    pump(0.5)
    ctl.request_user_cmd("chiller")
    pump(1.5)
    win.grab().save(os.path.join(out_dir, "06_settle_wait.png"))
    ctl.request_stop()
    ctl.join(20)
    print("screenshots written to", out_dir)
    for line in ctl.event_log[-30:]:
        print("  ", line)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshots", sys.argv[2] if len(sys.argv) > 2 else None)
