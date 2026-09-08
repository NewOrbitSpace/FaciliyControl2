"""Entry point: `python run_facility.py [options]`."""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

from .config import load_config
from .controller import AutoAnswerDialogs, Controller
from .hal import SimBackend, create_backend, daqmx_available
from .logging_csv import CsvLogger
from .model import Mode


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="NewOrbit vacuum facility control (Python port of the LabVIEW VI)")
    ap.add_argument("--config", "-c", default=None, help="facility YAML (default: config/facility_main_v4.4.yaml)")
    hw = ap.add_mutually_exclusive_group()
    hw.add_argument("--sim", action="store_true", help="force the simulated plant (default when no NI-DAQmx is found)")
    hw.add_argument("--daq", action="store_true", help="require the real NI cDAQ (error if nidaqmx is missing)")
    ap.add_argument("--units", choices=["torr", "mbar"], default=None, help="pressure unit shown at start-up")
    ap.add_argument("--time-scale", type=float, default=None, help="simulation speed-up factor")
    ap.add_argument("--mode", choices=["ask", "auto", "admin", "manual"], default="ask",
                    help="initial control mode ('ask' shows the Select Control Mode dialog like the VI)")
    wt = ap.add_mutually_exclusive_group()
    wt.add_argument("--blocking-waits", dest="blocking", action="store_true", default=None,
                    help="freeze the loop for settle waits and dialogs like the VI (default: timings.blocking_waits in the YAML)")
    wt.add_argument("--non-blocking", dest="blocking", action="store_false",
                    help="keep reading during settle waits and dialogs; only the decision step waits")
    ap.add_argument("--no-csv", action="store_true", help="disable daily CSV logging")
    ap.add_argument("--log-dir", default=None, help="CSV log directory (default: ./logs)")
    ap.add_argument("--headless", type=float, default=None, metavar="SECONDS",
                    help="run without a GUI for N seconds (smoke test / service mode)")
    return ap.parse_args(argv)


def build(args):
    cfg = load_config(args.config)
    if args.units:
        cfg.pressure_unit_default = args.units
    if args.time_scale:
        cfg.simulation.time_scale = args.time_scale
    if args.blocking is not None:
        cfg.timings.blocking_waits = args.blocking
    backend = create_backend(cfg, "sim" if args.sim else ("daq" if args.daq else "auto"))
    csv = None
    if cfg.logging.csv_enabled and not args.no_csv:
        csv = CsvLogger(cfg, args.log_dir)
    initial = {"ask": None, "auto": Mode.AUTO, "admin": Mode.ADMIN, "manual": Mode.MANUAL}[args.mode]
    return cfg, backend, csv, initial


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.headless is not None:
        cfg, backend, csv, initial = build(args)
        dialogs = AutoAnswerDialogs()          # headless: every question is answered with its first button
        ctl = Controller(cfg, backend, dialogs=dialogs, csv_logger=csv, initial_mode=initial or Mode.AUTO)
        ctl.start()
        t_end = time.time() + args.headless
        try:
            while time.time() < t_end and not ctl.finished.is_set():
                time.sleep(0.5)
                s = ctl.snapshot()
                print(f"[{s.iteration:6d}] {s.mode.name:6s} {s.current.label:20s} err={s.error.code} "
                      + " ".join(f"{g.id}={s.inputs.pressures_torr.get(g.id, float('nan')):.2e}" for g in cfg.gauges))
        finally:
            ctl.request_stop()
            ctl.join()
        return 0

    # GUI path.  IMPORTANT: load Qt and pyqtgraph and create the QApplication *before* the NI-DAQmx
    # library is loaded.  build() imports nidaqmx for --daq, and on Windows loading the NI library
    # before Qt/pyqtgraph makes a Qt native module crash on import (access violation 0xC0000005 –
    # no window, no Python traceback).  Qt first, NI last fixes it.
    try:
        from PySide6.QtWidgets import QApplication
        from .gui.dialogs import QtDialogProvider
        from .gui.main_window import MainWindow          # this pulls in pyqtgraph
    except ImportError as exc:
        print(f"\nGUI libraries missing ({exc}).\nInstall them into this Python environment with:\n"
              f"    python -m pip install -r requirements.txt\n"
              f"(on Windows run.bat does this in %LOCALAPPDATA%\\FacilityControl\\venv – see README).", file=sys.stderr)
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")

    cfg, backend, csv, initial = build(args)               # nidaqmx is imported here, after Qt is loaded
    dialogs = QtDialogProvider(modal_info=cfg.timings.blocking_waits)
    ctl = Controller(cfg, backend, dialogs=dialogs, csv_logger=csv, initial_mode=initial)
    win = MainWindow(cfg, ctl, sim_backend=backend if isinstance(backend, SimBackend) else None)
    dialogs._parent = win
    win.show()
    ctl.start()
    rc = app.exec()
    ctl.request_stop()                     # also releases a loop frozen in a wait / dialog
    dialogs.close_all()
    ctl.join(15.0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
