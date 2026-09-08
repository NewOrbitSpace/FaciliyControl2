"""Auto-mode state machine – a faithful port of the 'Auto' frame of Main_V4.4.vi.

Every state below corresponds to one frame of the VI's `Current Facility State` case
structure; comments quote the VI where the behaviour is non-obvious.  The code is written
against a *list* of turbos so that a facility profile with several turbo branches can reuse
it; with one turbo it collapses to exactly the VI's logic.

Button names (normalised from the VI's duplicated controls):
    pump_to_rough, pump_to_high_vac, overnight_pump, vent, shut_off, shutdown, vent_and_shutdown
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .config import FacilityConfig
from .model import Commands, FacilityState as S, Inputs, TabPage


# ---------------------------------------------------------------------------- timers
class ElapsedTimer:
    """LabVIEW 'Elapsed Time' express VI."""

    def __init__(self, target_s: float, auto_reset: bool = False):
        self.target_s = target_s
        self.auto_reset = auto_reset
        self.start: Optional[float] = None

    def update(self, now: float, reset: bool = False) -> tuple[float, bool]:
        if reset or self.start is None:
            self.start = now
        elapsed = now - self.start
        done = elapsed >= self.target_s
        if done and self.auto_reset:
            self.start = now
        return elapsed, done

    def reset(self, now: float) -> None:
        self.start = now


@dataclass
class AutoMemory:
    """Persistent objects of the Auto frame (express-VI timers, edge detectors)."""
    warmup: ElapsedTimer = field(default_factory=lambda: ElapsedTimer(60.0))
    below_threshold: ElapsedTimer = field(default_factory=lambda: ElapsedTimer(30.0))
    below_prev: Optional[bool] = None
    disengage: ElapsedTimer = field(default_factory=lambda: ElapsedTimer(1.0, auto_reset=True))
    vent: ElapsedTimer = field(default_factory=lambda: ElapsedTimer(600.0, auto_reset=True))
    last_error_log: str = ""


@dataclass
class AutoInput:
    inputs: Inputs
    prev: Commands
    current: S
    target: S
    substate: int
    buttons: Set[str]
    now: float
    error_active: bool                 # error? OR 5000 <= code <= 5010
    turbo_on_threshold_torr: float
    turbo_engage_time: Optional[float]  # epoch seconds ('Turbo Engage time')
    skip_primary_warm: bool


@dataclass
class AutoOutput:
    commands: Commands
    current: S
    target: S
    substate: int
    tab: TabPage
    log: List[str] = field(default_factory=list)
    skip_primary_warm: bool = False
    elapsed_below_threshold_s: float = 0.0


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class AutoStateMachine:
    def __init__(self, cfg: FacilityConfig, mem: Optional[AutoMemory] = None):
        self.cfg = cfg
        self.th = cfg.thresholds
        self.tm = cfg.timings
        self.mem = mem or AutoMemory()
        self.mem.warmup.target_s = self.tm.primary_warmup_s
        self.mem.below_threshold.target_s = self.th.min_time_below_threshold_s
        self.mem.disengage.target_s = self.tm.disengage_wait_s
        self.mem.vent.target_s = self.tm.vent_duration_s

    # ------------------------------------------------------------- helpers
    def _turbos(self):
        return self.cfg.turbos_in_use()

    def _max_speed(self, inp: Inputs) -> float:
        speeds = [inp.turbos[t.id].speed_pct for t in self.cfg.turbos if t.id in inp.turbos]
        return max(speeds) if speeds else 0.0

    def _wrg(self, inp: Inputs) -> float:
        return inp.pressure(self.cfg.main_gauge.id)

    def _foreline(self, inp: Inputs) -> float:
        g = self.cfg.foreline_gauge
        return inp.pressure(g.id) if g else self._wrg(inp)

    def _turbo_pressure(self, inp: Inputs, tid: str) -> float:
        g = self.cfg.turbo_gauge(tid)
        return inp.pressure(g.id) if g else self._foreline(inp)

    def _set_turbo_valves(self, cmds: Commands, value: bool) -> None:
        for t in self._turbos():
            cmds.valves[t.turbo_valve] = value

    def _set_gates(self, cmds: Commands, value: bool) -> None:
        for t in self._turbos():
            cmds.valves[t.gate_valve] = value

    def _set_motors(self, cmds: Commands, value: bool) -> None:
        for t in self._turbos():
            cmds.turbo_motor[t.id] = value

    def _bypass_id(self) -> Optional[str]:
        v = self.cfg.valves_of_kind("bypass")
        return v[0].id if v else None

    def _vent_id(self) -> Optional[str]:
        v = self.cfg.valves_of_kind("vent")
        return v[0].id if v else None

    def _any_turbo_valve_prev(self, prev: Commands) -> bool:
        return any(prev.valves.get(t.turbo_valve, False) for t in self._turbos())

    # ------------------------------------------------------------- main entry
    def step(self, a: AutoInput) -> AutoOutput:
        cfg, th = self.cfg, self.th
        inp, prev, b = a.inputs, a.prev, a.buttons
        log: List[str] = []
        cur, tgt = a.current, a.target

        # --- error override: "Shutt down due to error at ..." -> Facility Off
        if a.error_active:
            cur, tgt = S.FACILITY_OFF, S.FACILITY_OFF
            line = "Shutt down due to error"
            if self.mem.last_error_log != line:      # (VI repeats this every iteration; we log once)
                log.append(f"{line} at {_ts()}")
                self.mem.last_error_log = line
        else:
            self.mem.last_error_log = ""

        cmds = prev.copy()                 # every state redefines what it needs
        cmds.turbo_error_ack = {t: False for t in cfg.turbo_ids}
        bypass, vent = self._bypass_id(), self._vent_id()
        speed = self._max_speed(inp)
        wrg = self._wrg(inp)
        substate = a.substate
        out_sub = 0
        tab = TabPage.FACILITY_OFF
        skip_warm = a.skip_primary_warm
        elapsed_below = 0.0

        def all_off():
            for v in cmds.valves:
                cmds.valves[v] = False
            cmds.primary = False
            cmds.chiller = False
            for t in cfg.turbo_ids:
                cmds.turbo_motor[t] = False
                cmds.turbo_standby[t] = False

        # =====================================================================
        if cur == S.FACILITY_OFF:
            all_off()
            tab = TabPage.FACILITY_OFF
            new_cur, new_tgt = S.FACILITY_OFF, tgt
            if "pump_to_rough" in b:
                new_tgt = S.PUMPING_TO_ROUGH
            if "pump_to_high_vac" in b:
                new_tgt = S.PUMPING_TO_HIGH_VAC
            if "overnight_pump" in b:
                new_tgt = S.OVERNIGHT_PUMP
            if "vent" in b:
                new_tgt = S.VENT_AND_SHUTDOWN
            if b & {"pump_to_rough", "pump_to_high_vac", "overnight_pump"}:
                new_cur = S.PUMPING_TO_ROUGH
            if "vent" in b:
                new_cur = S.VENTING
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.PUMPING_TO_ROUGH:
            tab = TabPage.PUMPING_TO_ROUGH
            # button target overrides (Select chain)
            tgt_mid = tgt
            if "overnight_pump" in b:
                tgt_mid = S.OVERNIGHT_PUMP
            if "pump_to_rough_only" in b or "pump_to_rough" in b:
                tgt_mid = S.PUMPING_TO_ROUGH
            if "pump_to_high_vac" in b:
                tgt_mid = S.PUMPING_TO_HIGH_VAC
            new_tgt = S.VENTING if "vent" in b else tgt_mid

            primary_already_on = inp.primary_read
            done = False
            # --- substates
            if substate == 0:
                skip_warm = primary_already_on
                bypass_flag = primary_already_on
                next_sub = 1
                self.mem.warmup.reset(a.now)
            elif substate == 1:
                _, warm_done = self.mem.warmup.update(a.now)
                # VI: Compound OR (time elapsed, TRUE, primary already on) -> always TRUE
                if not self.tm.primary_warmup_enabled:
                    warm_done = True
                warm_done = warm_done or skip_warm
                next_sub = 2 if warm_done else 1
                bypass_flag = True
            else:  # substate 2 – wait for the WRG to sit below the threshold for 30 s
                below = (wrg < a.turbo_on_threshold_torr) and (wrg > th.gauge_error_threshold_torr)
                if below:
                    changed = (self.mem.below_prev is not True)
                    elapsed_below, done = self.mem.below_threshold.update(a.now, reset=changed)
                else:
                    elapsed_below, done = 0.0, False
                self.mem.below_prev = below
                bypass_flag = True
                skip_warm = False
                next_sub = 2

            # commands
            tv_open = (tgt_mid != S.PUMPING_TO_ROUGH) and (self._any_turbo_valve_prev(prev) or done)
            self._set_turbo_valves(cmds, tv_open)
            if bypass:
                cmds.valves[bypass] = False if "vent" in b else bypass_flag
            if vent:
                cmds.valves[vent] = False
            self._set_gates(cmds, False)
            cmds.primary = True
            cmds.chiller = (tgt == S.PUMPING_TO_HIGH_VAC) or (speed > th.turbo_spinning_pct) or (substate == 0)
            # transitions (use the target *after* the error override, as the VI does)
            new_cur = S.PUMPING_TO_ROUGH
            if tgt == S.OVERNIGHT_PUMP and done:
                new_cur = S.OVERNIGHT_PUMP
            if tgt == S.PUMPING_TO_HIGH_VAC and done:
                new_cur = S.ENGAGE_TURBO
            if "shut_off" in b:
                new_cur = S.FACILITY_OFF
            if "vent" in b:
                new_cur = S.VENTING
            if new_cur != S.PUMPING_TO_ROUGH:
                out_sub = 0
                skip_warm = False
            else:
                out_sub = next_sub
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.OVERNIGHT_PUMP:
            tab = TabPage.OVERNIGHT_PUMP
            all_off()
            cmds.chiller = speed > th.overnight_chiller_speed_pct
            time_reached = a.turbo_engage_time is not None and a.now > a.turbo_engage_time
            new_cur = S.PUMPING_TO_ROUGH if time_reached else S.OVERNIGHT_PUMP
            new_tgt = S.PUMPING_TO_HIGH_VAC if time_reached else tgt
            if time_reached:
                log.append(f"Timestamp reached, turning on pumps at {_ts()}")
            if "vent" in b:
                new_cur, new_tgt = S.VENTING, S.VENTING
            if "pump_to_rough" in b:
                new_cur, new_tgt = S.PUMPING_TO_ROUGH, S.PUMPING_TO_ROUGH
            if "pump_to_high_vac" in b:
                new_cur, new_tgt = S.PUMPING_TO_ROUGH, S.PUMPING_TO_HIGH_VAC
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.ENGAGE_TURBO:
            tab = TabPage.ENGAGING_TURBO
            self._set_turbo_valves(cmds, True)
            if vent:
                cmds.valves[vent] = False
            cmds.primary = True
            cmds.chiller = True
            new_cur, new_tgt = S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC
            if substate == 0:
                bp, gv, out_sub = True, False, 1
            elif substate == 1:
                limit = th.turbo_on_wiggle_room_multiplier * a.turbo_on_threshold_torr
                ok = (wrg < limit) and (wrg > th.wrg_error_threshold_torr) and (self._foreline(inp) < limit)
                what = "open gate" if speed > th.turbo_engage_msg_speed_pct else "turn on turbo"
                if ok:
                    log.append(f"Pressure low enough to {what}")
                    bp, gv, out_sub = True, False, 2
                else:
                    log.append(f"Pressure NOT low enough to {what}!")
                    bp, gv, out_sub = True, False, 0
                    new_cur = S.PUMPING_TO_ROUGH
            elif substate == 2:
                bp, gv, out_sub = True, False, 3
            elif substate == 3:
                bp, gv, out_sub = False, False, 4        # close bypass
            elif substate == 4:
                bp, gv, out_sub = False, True, 5         # open gate(s)
            else:  # 5 – start the turbo(s)
                bp, gv, out_sub = False, True, 0
                self._set_motors(cmds, True)
                new_cur = S.PUMPING_TO_HIGH_VAC
            if bypass:
                cmds.valves[bypass] = bp
            self._set_gates(cmds, gv)
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.PUMPING_TO_HIGH_VAC:
            tab = TabPage.PUMPING_TO_HIGH_VAC
            self._set_turbo_valves(cmds, True)
            self._set_gates(cmds, True)
            if bypass:
                cmds.valves[bypass] = False
            if vent:
                cmds.valves[vent] = False
            cmds.primary = True
            cmds.chiller = True
            leave = bool(b & {"shutdown", "pump_to_rough", "overnight_pump", "vent_and_shutdown"})
            self._set_motors(cmds, not leave)          # motor off the moment a leave button is pressed
            new_cur, new_tgt = S.PUMPING_TO_HIGH_VAC, tgt
            if "shutdown" in b:
                new_cur, new_tgt = S.DISENGAGE_TURBO, S.FACILITY_OFF
            if "pump_to_rough" in b:
                new_cur, new_tgt = S.DISENGAGE_TURBO, S.PUMPING_TO_ROUGH
            if "overnight_pump" in b:
                new_cur, new_tgt = S.DISENGAGE_TURBO, S.OVERNIGHT_PUMP
            if "vent_and_shutdown" in b:
                new_cur, new_tgt = S.DISENGAGE_TURBO, S.VENT_AND_SHUTDOWN
            if "vent" in b:
                new_cur, new_tgt = S.DISENGAGE_TURBO, S.VENTING   # turbo keeps running
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.DISENGAGE_TURBO:
            tab = TabPage.CLOSING_GATE
            self._set_gates(cmds, False)             # gate closes immediately
            if vent:
                cmds.valves[vent] = False
            cmds.primary = True
            cmds.chiller = True
            new_cur, new_tgt = S.DISENGAGE_TURBO, tgt
            if substate == 0:
                self._set_turbo_valves(cmds, True)
                if bypass:
                    cmds.valves[bypass] = False
                self.mem.disengage.reset(a.now)
                out_sub = 1
            elif substate == 1:
                self._set_turbo_valves(cmds, True)
                if bypass:
                    cmds.valves[bypass] = False
                _, done = self.mem.disengage.update(a.now)
                out_sub = 2 if done else 1
            else:
                self._set_turbo_valves(cmds, tgt == S.VENTING)     # keep turbo running (isolated) only when venting
                if bypass:
                    cmds.valves[bypass] = (tgt == S.PUMPING_TO_ROUGH)
                if tgt == S.PUMPING_TO_ROUGH:
                    new_cur = S.PUMPING_TO_ROUGH
                elif tgt == S.OVERNIGHT_PUMP:
                    new_cur = S.OVERNIGHT_PUMP
                elif tgt in (S.VENTING, S.VENT_AND_SHUTDOWN):
                    new_cur = S.VENTING
                else:
                    new_cur = S.TURBO_SLOWING
                out_sub = 0
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.VENTING:
            tab = TabPage.VENTING
            if bypass:
                cmds.valves[bypass] = False
            self._set_gates(cmds, False)
            spinning = speed > th.turbo_spinning_pct
            # 10-minute vent timer
            if substate == 0:
                self.mem.vent.reset(a.now)
                venting_active, next_sub = True, 1
            elif substate == 1:
                _, done = self.mem.vent.update(a.now)
                venting_active, next_sub = True, (2 if done else 1)
            else:
                venting_active, next_sub = False, 2
            if vent:
                cmds.valves[vent] = venting_active or (tgt == S.VENT_AND_SHUTDOWN)
            cmds.primary = spinning
            cmds.chiller = spinning
            # new target
            new_tgt = S.FACILITY_OFF if tgt in (S.FACILITY_OFF, S.VENT_AND_SHUTDOWN) else tgt
            if "overnight_pump" in b:
                new_tgt = S.OVERNIGHT_PUMP
            if "pump_to_rough" in b:
                new_tgt = S.PUMPING_TO_ROUGH
            if "pump_to_high_vac" in b:
                new_tgt = S.PUMPING_TO_HIGH_VAC
            # new current
            if tgt in (S.FACILITY_OFF, S.VENT_AND_SHUTDOWN):
                new_cur = S.TURBO_SLOWING if spinning else S.FACILITY_OFF
            else:
                new_cur = S.VENTING
            if venting_active:
                new_cur = S.VENTING
            if "overnight_pump" in b:
                new_cur = S.PUMPING_TO_ROUGH
            if "shut_off" in b:
                new_cur = S.TURBO_SLOWING if spinning else S.FACILITY_OFF
            if "pump_to_rough" in b or "pump_to_high_vac" in b:
                new_cur = S.PUMPING_TO_ROUGH
            staying = new_cur == S.VENTING
            self._set_turbo_valves(cmds, spinning if staying else False)
            out_sub = next_sub if staying else 0
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.TURBO_SLOWING:
            tab = TabPage.TURBO_SLOWING
            back = "pump_to_high_vac" in b
            self._set_turbo_valves(cmds, not back)
            if bypass:
                cmds.valves[bypass] = False
            if vent:
                cmds.valves[vent] = False if back else prev.valves.get(vent, False)
            self._set_gates(cmds, False)
            cmds.primary = True
            cmds.chiller = True
            self._set_motors(cmds, back)
            if back:
                cur, tgt = S.PUMPING_TO_ROUGH, S.PUMPING_TO_HIGH_VAC
            else:
                cur, tgt = (S.TURBO_SLOWING if speed > th.turbo_spinning_pct else S.FACILITY_OFF), S.FACILITY_OFF

        else:  # VENT_AND_SHUTDOWN is only a target; treat like Facility Off
            all_off()
            cur = S.FACILITY_OFF

        # --- global protection: high-pressure turbo shut-off (Turbo Convectron >= 5 Torr)
        for t in self._turbos():
            if cmds.turbo_motor.get(t.id, False):
                p = self._turbo_pressure(inp, t.id)
                if not (p < th.turbo_high_pressure_shutoff_torr):
                    cmds.turbo_motor[t.id] = False
                    log.append(f"High pressure turbo shuttoff triggered at {_ts()}")

        return AutoOutput(cmds, cur, tgt, out_sub, tab, log, skip_warm, elapsed_below)
