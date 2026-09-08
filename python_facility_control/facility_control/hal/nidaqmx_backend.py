"""NI cDAQ backend – reproduces the DAQmx task layout of Main_V4.4.vi with the `nidaqmx` package.

Tasks (as in the VI's "Setup of Main Loop"):
  DO  valves            one channel per line (line grouping = one channel for each line)
  DO  primary, chiller  single line each
  DI  valve reads       one channel per line
  DI  primary, chiller  single line each
  AI  gauges + extras + turbo speed(s)   finite acquisition, `rate` x `samples per channel`, mean per channel
  per turbo: DO motor (+ standby | + error-ack), DI error (+ still spinning)

`read_only=True` creates the input tasks only – nothing is ever written (no DO task is even
reserved), for bring-up checks with `tools/daq_check.py` while the facility is in any state.

Only imported when nidaqmx is installed (see hal/__init__.py).
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

import numpy as np

from ..config import FacilityConfig
from ..gauges import speed_pct_from_v, v_to_torr
from ..model import Commands, Inputs, TurboInputs
from .base import HardwareBackend

import nidaqmx  # type: ignore
from nidaqmx.constants import AcquisitionType, LineGrouping, TerminalConfiguration  # type: ignore

# AI terminal configuration by name (YAML `analog_input.terminal_config`).  Main_V4.4 uses
# DIFFERENTIAL (VI value 10106) – reading a differentially-wired gauge as RSE adds a per-channel
# voltage offset (wrong pressures).  Match the VI unless a facility is wired single-ended.
_TERMINAL_CONFIG = {
    "differential": TerminalConfiguration.DIFF, "diff": TerminalConfiguration.DIFF,
    "rse": TerminalConfiguration.RSE, "nrse": TerminalConfiguration.NRSE,
    "default": TerminalConfiguration.DEFAULT, "pseudo_diff": TerminalConfiguration.PSEUDO_DIFF,
    "pseudodifferential": TerminalConfiguration.PSEUDO_DIFF,
}


class NiDaqmxBackend(HardwareBackend):
    name = "nidaqmx"

    def __init__(self, config: FacilityConfig, read_only: bool = False):
        super().__init__(config)
        self.read_only = read_only
        self.tasks: Dict[str, "nidaqmx.Task"] = {}
        self.ai_channels: List[str] = []       # order of channels in the AI task
        self._last_written: Optional[Commands] = None

    # ------------------------------------------------------------------ setup
    def open(self) -> None:
        self._open_inputs()
        if not self.read_only:
            self._open_outputs()
            self.safe_state()

    def _open_outputs(self) -> None:
        cfg = self.config
        # --- DO valves (4 lines, one channel per line, written as a boolean array)
        t = nidaqmx.Task("valve_cmd")
        for vid, v in cfg.valves.items():
            t.do_channels.add_do_chan(cfg.phys(v.cmd), name_to_assign_to_lines=f"{vid}_cmd",
                                      line_grouping=LineGrouping.CHAN_PER_LINE)
        self.tasks["valve_cmd"] = t
        # --- DO primary / chiller
        for key, pump in (("primary_cmd", cfg.primary), ("chiller_cmd", cfg.chiller)):
            t = nidaqmx.Task(key)
            t.do_channels.add_do_chan(cfg.phys(pump.cmd), name_to_assign_to_lines=key, line_grouping=LineGrouping.CHAN_PER_LINE)
            self.tasks[key] = t
        # --- turbo DO (motor + standby, error acknowledge)
        for tc in cfg.turbos:
            p = tc.params
            t = nidaqmx.Task(f"{tc.id}_cmd")
            t.do_channels.add_do_chan(cfg.phys(p["motor_do"]), name_to_assign_to_lines=f"{tc.id}_motor", line_grouping=LineGrouping.CHAN_PER_LINE)
            if "standby_do" in p:
                t.do_channels.add_do_chan(cfg.phys(p["standby_do"]), name_to_assign_to_lines=f"{tc.id}_standby", line_grouping=LineGrouping.CHAN_PER_LINE)
            self.tasks[f"{tc.id}_cmd"] = t
            if "error_ack_do" in p:
                t = nidaqmx.Task(f"{tc.id}_ack")
                t.do_channels.add_do_chan(cfg.phys(p["error_ack_do"]), name_to_assign_to_lines=f"{tc.id}_ack", line_grouping=LineGrouping.CHAN_PER_LINE)
                self.tasks[f"{tc.id}_ack"] = t

    def _open_inputs(self) -> None:
        cfg = self.config
        # --- DI valve reads
        t = nidaqmx.Task("valve_read")
        for vid, v in cfg.valves.items():
            t.di_channels.add_di_chan(cfg.phys(v.read), name_to_assign_to_lines=f"{vid}_read", line_grouping=LineGrouping.CHAN_PER_LINE)
        self.tasks["valve_read"] = t
        for key, pump in (("primary_read", cfg.primary), ("chiller_read", cfg.chiller)):
            t = nidaqmx.Task(key)
            ch = t.di_channels.add_di_chan(cfg.phys(pump.read), name_to_assign_to_lines=key, line_grouping=LineGrouping.CHAN_PER_LINE)
            if pump.read_inverted:
                ch.di_invert_lines = True
            self.tasks[key] = t
        # --- AI (gauges, extras, turbo speeds, compressor)
        term = _TERMINAL_CONFIG.get(cfg.ai_terminal_config)
        if term is None:
            raise ValueError(f"unknown analog_input.terminal_config '{cfg.ai_terminal_config}' "
                             f"(use one of: {', '.join(sorted(_TERMINAL_CONFIG))})")
        ai = nidaqmx.Task("analog_in")
        self.ai_channels = []
        for g in cfg.gauges:
            ai.ai_channels.add_ai_voltage_chan(cfg.phys(g.channel), name_to_assign_to_channel=g.id,
                                               terminal_config=term, min_val=cfg.ai_min_v, max_val=cfg.ai_max_v)
            self.ai_channels.append(g.id)
        for ea in cfg.extra_analog:
            ai.ai_channels.add_ai_voltage_chan(cfg.phys(ea["channel"]), name_to_assign_to_channel=ea["id"],
                                               terminal_config=term, min_val=cfg.ai_min_v, max_val=cfg.ai_max_v)
            self.ai_channels.append(ea["id"])
        if cfg.compressor_ai:
            ai.ai_channels.add_ai_voltage_chan(cfg.phys(cfg.compressor_ai), name_to_assign_to_channel="compressor",
                                               terminal_config=term, min_val=cfg.ai_min_v, max_val=cfg.ai_max_v)
            self.ai_channels.append("compressor")
        for tc in cfg.turbos:
            p = tc.params
            if "speed_ai" in p:
                ai.ai_channels.add_ai_voltage_chan(cfg.phys(p["speed_ai"]), name_to_assign_to_channel=f"{tc.id}_speed",
                                                   terminal_config=term,
                                                   min_val=float(p.get("speed_ai_min_v", 0.0)), max_val=float(p.get("speed_ai_max_v", 10.0)))
                self.ai_channels.append(f"{tc.id}_speed")
        ai.timing.cfg_samp_clk_timing(cfg.ai_sample_rate_hz, sample_mode=AcquisitionType.FINITE, samps_per_chan=cfg.ai_samples_per_channel)
        self.tasks["analog_in"] = ai
        # --- turbo DI (error, still spinning)
        for tc in cfg.turbos:
            p = tc.params
            if tc.control_mode == "hipace_rs485":
                raise NotImplementedError("HiPace RS485 control (Pfeiffer PV library) is not ported; use a D-SUB mode")
            t = nidaqmx.Task(f"{tc.id}_read")
            t.di_channels.add_di_chan(cfg.phys(p["error_di"]), name_to_assign_to_lines=f"{tc.id}_error", line_grouping=LineGrouping.CHAN_PER_LINE)
            if "still_spinning_di" in p:
                t.di_channels.add_di_chan(cfg.phys(p["still_spinning_di"]), name_to_assign_to_lines=f"{tc.id}_spinning", line_grouping=LineGrouping.CHAN_PER_LINE)
            self.tasks[f"{tc.id}_read"] = t

    def close(self) -> None:
        for t in self.tasks.values():
            try:
                t.stop()
            except Exception:
                pass
            try:
                t.close()
            except Exception:
                pass
        self.tasks.clear()

    def describe(self) -> str:
        return f"NI-DAQmx ({self.config.daq_name})" + (" READ-ONLY" if self.read_only else "")

    # ------------------------------------------------------------------ I/O
    def write(self, cmds: Commands) -> None:
        cfg = self.config
        if self.read_only:
            return
        self.tasks["valve_cmd"].write([bool(cmds.valves.get(v, False)) for v in cfg.valve_ids], auto_start=True)
        self.tasks["primary_cmd"].write(bool(cmds.primary), auto_start=True)
        self.tasks["chiller_cmd"].write(bool(cmds.chiller), auto_start=True)
        for tc in cfg.turbos:
            vals = [bool(cmds.turbo_motor.get(tc.id, False))]
            if "standby_do" in tc.params:
                vals.append(bool(cmds.turbo_standby.get(tc.id, False)))
            task = self.tasks[f"{tc.id}_cmd"]
            task.write(vals if len(vals) > 1 else vals[0], auto_start=True)
        self._last_written = cmds.copy()

    def turbo_error_ack(self, turbo_id: str, level: bool) -> None:
        if self.read_only:
            return
        task = self.tasks.get(f"{turbo_id}_ack")
        if task is not None:
            task.write(bool(level), auto_start=True)

    def read(self) -> Inputs:
        cfg = self.config
        inp = Inputs(t=time.time())
        try:
            vr = self.tasks["valve_read"].read()
            if not isinstance(vr, list):
                vr = [vr]
            for vid, val in zip(cfg.valve_ids, vr):
                inp.valve_reads[vid] = bool(val)
            inp.primary_read = bool(self.tasks["primary_read"].read())
            inp.chiller_read = bool(self.tasks["chiller_read"].read())
            ai = self.tasks["analog_in"]
            # finite acquisition: DAQmx Read auto-starts the task and stops it after the last sample
            # (the VI reads the same way, without an explicit Start Task); stop() is a harmless safeguard
            try:
                data = ai.read(number_of_samples_per_channel=cfg.ai_samples_per_channel,
                               timeout=max(2.0, 4.0 * cfg.ai_samples_per_channel / cfg.ai_sample_rate_hz))
            finally:
                try:
                    ai.stop()
                except Exception:
                    pass
            arr = np.asarray(data, dtype=float)
            if arr.ndim == 1:
                arr = arr[None, :]
            means = arr.mean(axis=1)
            volts = dict(zip(self.ai_channels, means.tolist()))
            for g in cfg.gauges:
                v = volts[g.id]
                inp.gauge_volts[g.id] = v
                inp.pressures_torr[g.id] = v_to_torr(g.formula, v)
            for ea in cfg.extra_analog:
                inp.extra_analog[ea["id"]] = volts[ea["id"]]
            if cfg.compressor_ai:
                inp.compressor_bar = volts["compressor"]  # calibrate in config if a real sensor is fitted
            for tc in cfg.turbos:
                p = tc.params
                ti = TurboInputs()
                if f"{tc.id}_speed" in volts:
                    ti.speed_pct = speed_pct_from_v(volts[f"{tc.id}_speed"])
                dr = self.tasks[f"{tc.id}_read"].read()
                if not isinstance(dr, list):
                    dr = [dr]
                err = bool(dr[0])
                if p.get("error_di_inverted", False):
                    err = not err          # HiPace: 24 V = healthy
                ti.error = err
                if "still_spinning_di" in p and len(dr) > 1:
                    ti.still_spinning = bool(dr[1])
                # D-SUB modes have no motor feedback: the VI echoes the command (Turbo_Motor_Sys_Cmd)
                ti.motor_read = bool(self._last_written.turbo_motor.get(tc.id, False)) if self._last_written else False
                inp.turbos[tc.id] = ti
        except Exception as exc:  # DAQmx error -> surfaces in the error cluster (code 5020)
            inp.daq_error = f"{type(exc).__name__}: {exc}"
        return inp
