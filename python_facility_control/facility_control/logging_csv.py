"""Permanent logging: one CSV file per day (VC100 behaviour) with every valve/pump state,
all pressures (Torr and mBar) and the error cluster."""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import List, Optional, TextIO

from .config import FacilityConfig
from .model import Snapshot
from .units import torr_to


CONTACT_COLUMNS = ("rotating", "accelerating", "at_speed", "braking", "alarm", "warning")


class CsvLogger:
    def __init__(self, cfg: FacilityConfig, directory: Optional[str] = None):
        self.cfg = cfg
        self.dir = Path(directory or cfg.logging.csv_directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh: Optional[TextIO] = None
        self._writer = None
        self._day = ""
        self.path: Optional[Path] = None

    def header(self) -> List[str]:
        cfg = self.cfg
        cols = ["timestamp", "iso_time", "mode", "current_state", "target_state", "substate"]
        for g in cfg.gauges:
            cols += [f"{g.id}_torr", f"{g.id}_mbar", f"{g.id}_volts"]
        for ea in cfg.extra_analog:
            cols.append(f"{ea['id']}_volts")
        for vid in cfg.valve_ids:
            cols += [f"{vid}_cmd", f"{vid}_read"]
        cols += ["primary_cmd", "primary_read", "chiller_cmd", "chiller_read"]
        if cfg.primary.two_stage:
            cols += ["primary_power_cmd", "primary_run_cmd"]
        if cfg.primary.has_frequency:
            cols += ["primary_hz", "primary_running"]
        for t in cfg.turbos:
            cols += [f"{t.id}_motor_cmd", f"{t.id}_standby_cmd", f"{t.id}_speed_pct", f"{t.id}_status", f"{t.id}_error"]
            if not t.has_speed:                       # contact interface: log the six status contacts too
                cols += [f"{t.id}_{c}" for c in CONTACT_COLUMNS]
        if cfg.has_compressor:
            cols.append("compressor_bar")
        cols += ["error_status", "error_code", "error_source", "primary_run_hours"]
        return cols

    def _rotate(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day == self._day and self._fh:
            return
        self.close()
        self._day = day
        self.path = self.dir / f"facility_{self.cfg.id}_{day}.csv"
        new = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(self.header())
            self._fh.flush()

    def write_row(self, s: Snapshot) -> None:
        cfg = self.cfg
        self._rotate(s.t)
        row: List[object] = [f"{s.t:.3f}", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.t)),
                             s.mode.name, s.current.label, s.target.label, s.substate]
        for g in cfg.gauges:
            p = s.inputs.pressures_torr.get(g.id, float("nan"))
            row += [f"{p:.4e}", f"{torr_to('mbar', p):.4e}", f"{s.inputs.gauge_volts.get(g.id, float('nan')):.4f}"]
        for ea in cfg.extra_analog:
            row.append(f"{s.inputs.extra_analog.get(ea['id'], float('nan')):.4f}")
        for vid in cfg.valve_ids:
            row += [int(bool(s.commands.valves.get(vid))), int(bool(s.inputs.valve_reads.get(vid)))]
        row += [int(s.commands.primary), int(s.inputs.primary_read), int(s.commands.chiller), int(s.inputs.chiller_read)]
        if cfg.primary.two_stage:
            row += [int(bool(s.commands.primary_power)), int(bool(s.commands.primary_run))]
        if cfg.primary.has_frequency:
            row += ["" if s.primary_hz is None else f"{s.primary_hz:.1f}", int(bool(s.primary_running))]
        for t in cfg.turbos:
            tv = s.turbos.get(t.id)
            row += [int(bool(s.commands.turbo_motor.get(t.id))), int(bool(s.commands.turbo_standby.get(t.id))),
                    f"{(tv.speed_pct if tv else 0.0):.1f}", (tv.status.label if tv else ""), int(bool(tv.error)) if tv else 0]
            if not t.has_speed:
                row += [int(bool(tv.contacts.get(c))) if tv else 0 for c in CONTACT_COLUMNS]
        if cfg.has_compressor:
            row.append("" if s.compressor_bar is None else f"{s.compressor_bar:.2f}")
        row += [int(s.error.status), s.error.code, s.error.source, f"{s.primary_run_hours:.3f}"]
        assert self._writer is not None
        self._writer.writerow(row)
        self._fh.flush()  # type: ignore[union-attr]

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
            self._writer = None
