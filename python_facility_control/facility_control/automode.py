"""Auto-mode state machine – a faithful port of the 'Auto' frame of the facility VIs.

Every state below corresponds to one frame of the VI's `Current Facility State` case
structure; comments quote the VI where the behaviour is non-obvious.  The code is written
against a *list* of turbos so that a facility profile with several turbo branches reuses it;
with one turbo it collapses to exactly Main_V4.4's logic, with three (VC100) to the medium
chamber's VI.  Where the two VIs differ the behaviour is selected by the profile's `auto_mode`
block (`AutoBehaviour` in config.py) – nothing is hard-coded per facility.

Button names (normalised from the VIs' duplicated controls):
    pump_to_rough, pump_to_high_vac, overnight_pump, vent, shut_off, shut_off_once_rough,
    shutdown, shutdown_after_vent, vent_and_shutdown

Deviation from the VIs, requested by the test engineer (2026-09-08) – the start order of
Pumping to Rough can depend on the chamber pressure, and Overnight Pump can skip roughing:
  * chamber above `thresholds.bypass_first_above_torr`: open the bypass valve FIRST, then start the
    primary pump (set the threshold to null for the VIs' own order: primary first, bypass after
    `timings.primary_to_bypass_gap_s` – 20 s in the VC100 VI, 60 s but disabled in Main_V4.4);
  * Overnight Pump pressed below `thresholds.overnight_skip_below_torr` goes straight to the
    overnight hold without starting the primary pump or opening the bypass (null = never).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .config import FacilityConfig, TurboConfig
from .model import Commands, FacilityState as S, Inputs, TabPage, TurboInputs
from .units import torr_to


# --------------------------------------------------------------- Pumping to Rough substates
# (renamed/renumbered 2026-09-08 – the start order now depends on the chamber pressure)
SUB_ROUGH_ENTRY = 0          # decide the start order from the chamber pressure
SUB_ROUGH_BYPASS_FIRST = 1   # high pressure: bypass already open, now start the primary pump
SUB_ROUGH_PRIMARY_GAP = 2    # low pressure: primary running, waiting the gap before the bypass opens
SUB_ROUGH_WAIT = 3           # primary + bypass on: wait for the chamber to sit below the threshold


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
    bypass_gap: ElapsedTimer = field(default_factory=lambda: ElapsedTimer(25.0))
    below_threshold: ElapsedTimer = field(default_factory=lambda: ElapsedTimer(30.0))
    below_prev: Optional[bool] = None
    disengage: ElapsedTimer = field(default_factory=lambda: ElapsedTimer(1.0, auto_reset=True))
    last_bypass_block: object = None             # log 'bypass held shut' once per state, not per loop
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
    error_active: bool                 # error? OR 5000 <= code <= 5011
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


def turbo_is_spinning(ti: Optional[TurboInputs], pct: float) -> bool:
    """'Still spinning' for one turbo: speed above `pct`, or – for the contact interface, which has
    no speed signal – the 'Rotating' contact (VC100: `Turbo 1 Rotating Read`)."""
    if ti is None:
        return False
    if ti.has_contacts:
        return ti.contact("rotating")
    return ti.speed_pct > pct


class AutoStateMachine:
    def __init__(self, cfg: FacilityConfig, mem: Optional[AutoMemory] = None):
        self.cfg = cfg
        self.th = cfg.thresholds
        self.tm = cfg.timings
        self.am = cfg.auto_mode
        self.mem = mem or AutoMemory()
        self.mem.warmup.target_s = self.tm.primary_warmup_s
        self.mem.bypass_gap.target_s = self.tm.primary_to_bypass_gap_s
        self.mem.below_threshold.target_s = self.th.min_time_below_threshold_s
        self.mem.disengage.target_s = self.tm.disengage_wait_s
        self.mem.vent.target_s = self.tm.vent_duration_s

    # ------------------------------------------------------------- helpers
    def _turbos(self) -> List[TurboConfig]:
        return self.cfg.turbos_in_use()

    def _spinning(self, inp: Inputs, tc: TurboConfig, pct: float) -> bool:
        return turbo_is_spinning(inp.turbos.get(tc.id), pct)

    def _any_spinning(self, inp: Inputs, pct: float) -> bool:
        """The VIs OR the spinning test over every turbo (VC100: Turbo1.Rotating ∨ T2 % ∨ T3 %)."""
        return any(turbo_is_spinning(ti, pct) for ti in inp.turbos.values())

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

    def _already_evacuated(self, inp: Inputs, turbo_on_threshold_torr: float) -> bool:
        """Is the chamber already below the turbo-on threshold (and the gauge believable)?

        Used to decide that a 'Pump to High Vac' pressed while the turbo is still spinning down does
        not need roughing at all - the chamber never lost its vacuum, so starting the primary and
        opening the bypass would only push foreline gas back into it."""
        wrg = self._wrg(inp)
        if math.isnan(wrg):
            return False
        return self.th.gauge_error_threshold_torr < wrg < turbo_on_threshold_torr

    def _any_turbo_valve_prev(self, prev: Commands) -> bool:
        return any(prev.valves.get(t.turbo_valve, False) for t in self._turbos())

    def _spinning_turbo_valves(self, cmds: Commands, inp: Inputs, prev: Commands, pct: float,
                               gauge_rule: bool) -> None:
        """Turbo valves while turbos spin down (Overnight / Venting / Turbo slowing).

        Main_V4.4 (one turbo): TV := spinning.  VC100 (per turbo): TV_i := spinning_i ∧ (TV_i_prev ∨
        foreline < turbo-body pressure_i) – a valve that is closed is only opened if the foreline is
        below the turbo body, so no gas is pushed into a spinning turbo (`turbo_valve_gauge_rule`).
        """
        fore = self._foreline(inp)
        for t in self._turbos():
            spin = self._spinning(inp, t, pct)
            if gauge_rule:
                p_t = self._turbo_pressure(inp, t.id)
                spin = spin and (prev.valves.get(t.turbo_valve, False) or fore < p_t)
            cmds.valves[t.turbo_valve] = spin

    # ------------------------------------------------------------- main entry
    def step(self, a: AutoInput) -> AutoOutput:
        cfg, th, am = self.cfg, self.th, self.am
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
        spinning15 = self._any_spinning(inp, th.turbo_spinning_pct)      # 15 % / Rotating (Rough, Engage)
        slow_pct = th.slowing_pct                                          # 'Turbo slowing Threshold (%)'
        spinning_slow = self._any_spinning(inp, slow_pct)                 # Overnight / Venting / Turbo slowing
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

        def off_or_slowing(spinning: bool) -> S:
            return S.TURBO_SLOWING if spinning else S.FACILITY_OFF

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
            if "shut_off_once_rough" in b:            # VC100: rough the chamber once, then switch off
                new_tgt = S.FACILITY_OFF
            if "vent" in b:
                new_tgt = S.VENT_AND_SHUTDOWN
            if b & {"pump_to_rough", "pump_to_high_vac", "overnight_pump", "shut_off_once_rough"}:
                new_cur = S.PUMPING_TO_ROUGH
            # test engineer: Overnight Pump with the chamber already below the skip threshold needs no
            # roughing at all – hold straight away, without the primary pump or the bypass valve
            if ("overnight_pump" in b and th.overnight_skip_below_torr is not None
                    and wrg > th.wrg_error_threshold_torr and wrg < th.overnight_skip_below_torr):
                new_cur = S.OVERNIGHT_PUMP
                log.append(f"Chamber already below {torr_to('mbar', th.overnight_skip_below_torr):.1f} mBar"
                           f" – holding for Overnight Pump without roughing at {_ts()}")
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
            if b & {"shut_off", "shut_off_once_rough"}:   # VC100 buttons: target Facility Off
                tgt_mid = S.FACILITY_OFF
            new_tgt = S.VENTING if "vent" in b else tgt_mid

            primary_already_on = inp.primary_read
            done = False
            # --- substates.  The start order depends on the chamber pressure (test engineer):
            #     above bypass_first_above_torr -> bypass valve first, then the primary pump
            #     below it (or rule disabled)   -> primary pump first, bypass after the gap (the VIs)
            if substate == SUB_ROUGH_ENTRY:
                self.mem.warmup.reset(a.now)
                self.mem.bypass_gap.reset(a.now)
                wrg_valid = wrg > th.wrg_error_threshold_torr
                if primary_already_on:
                    # already roughing (e.g. recognised from Admin) – nothing to sequence
                    skip_warm = True
                    primary_flag, bypass_flag = True, True
                    next_sub = SUB_ROUGH_WAIT
                elif th.bypass_first_above_torr is not None and wrg_valid and wrg > th.bypass_first_above_torr:
                    primary_flag, bypass_flag = False, True
                    next_sub = SUB_ROUGH_BYPASS_FIRST
                    log.append(f"Chamber above {torr_to('mbar', th.bypass_first_above_torr):.0f} mBar – "
                               f"opening the bypass valve before starting the primary pump at {_ts()}")
                else:
                    primary_flag, bypass_flag = True, False
                    next_sub = SUB_ROUGH_PRIMARY_GAP
                    log.append(f"Starting the primary pump; the bypass valve opens in "
                               f"{self.tm.primary_to_bypass_gap_s:.0f} s at {_ts()}")
            elif substate == SUB_ROUGH_BYPASS_FIRST:
                # the bypass valve was commanded open in the previous frame (and has settled)
                primary_flag, bypass_flag = True, True
                next_sub = SUB_ROUGH_WAIT
            elif substate == SUB_ROUGH_PRIMARY_GAP:
                gap_elapsed, gap_done = self.mem.bypass_gap.update(a.now)
                primary_flag = True
                bypass_flag = gap_done
                next_sub = SUB_ROUGH_WAIT if gap_done else SUB_ROUGH_PRIMARY_GAP
                if gap_done:
                    log.append(f"Opened the bypass valve {gap_elapsed:.0f} s after the primary pump at {_ts()}")
            else:  # SUB_ROUGH_WAIT – wait for the WRG to sit below the threshold for 30 s
                below = (wrg < a.turbo_on_threshold_torr) and (wrg > th.gauge_error_threshold_torr)
                if below:
                    changed = (self.mem.below_prev is not True)
                    elapsed_below, done = self.mem.below_threshold.update(a.now, reset=changed)
                else:
                    elapsed_below, done = 0.0, False
                self.mem.below_prev = below
                primary_flag, bypass_flag = True, True
                skip_warm = False
                next_sub = SUB_ROUGH_WAIT

            # commands
            # turbo valves: TV := (tgt ≠ Rough) ∧ (TV_prev ∨ done)  [Main_V4.4]
            # VC100 adds per turbo: ∨ (bypass open ∧ chamber pressure < turbo-body pressure_i)
            leaving = tgt_mid != S.PUMPING_TO_ROUGH
            any_prev = self._any_turbo_valve_prev(prev)
            bypass_prev = bool(bypass and prev.valves.get(bypass, False))
            for t in self._turbos():
                if am.turbo_valve_gauge_rule:
                    # VC100 For-loop: TV_i := done ∨ TV_i_prev ∨ (bypass ∧ WRG < turbo gauge_i)
                    early = bypass_prev and wrg > th.gauge_error_threshold_torr and wrg < self._turbo_pressure(inp, t.id)
                    open_i = done or prev.valves.get(t.turbo_valve, False) or early
                else:
                    open_i = done or any_prev
                cmds.valves[t.turbo_valve] = leaving and open_i
            if bypass:
                cmds.valves[bypass] = False if "vent" in b else bypass_flag
            if vent:
                cmds.valves[vent] = False
            self._set_gates(cmds, False)
            cmds.primary = primary_flag
            cmds.chiller = ((tgt == S.PUMPING_TO_HIGH_VAC) or spinning15 or (substate == SUB_ROUGH_ENTRY))
            # transitions (use the target *after* the error override, as the VI does)
            new_cur = S.PUMPING_TO_ROUGH
            if tgt == S.OVERNIGHT_PUMP and done:
                new_cur = S.OVERNIGHT_PUMP
            if tgt == S.PUMPING_TO_HIGH_VAC and done:
                new_cur = S.ENGAGE_TURBO
            if tgt == S.FACILITY_OFF and done:          # VC100 'Shut Off once rough'
                new_cur = off_or_slowing(spinning15)
                log.append(f"Rough vacuum reached – shutting off as requested at {_ts()}")
            if "shut_off" in b:
                new_cur = off_or_slowing(spinning15) if am.shut_off_spins_down_first else S.FACILITY_OFF
            if "vent" in b:
                new_cur = S.VENTING
            if new_cur != S.PUMPING_TO_ROUGH:
                out_sub = SUB_ROUGH_ENTRY
                skip_warm = False
            else:
                out_sub = next_sub
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.OVERNIGHT_PUMP:
            tab = TabPage.OVERNIGHT_PUMP
            all_off()
            if am.overnight_backing_while_spinning:
                # VC100: while any turbo is still above the slowing threshold keep the primary pump and
                # chiller running and each spinning turbo's valve open; everything else is off
                for t in self._turbos():
                    cmds.valves[t.turbo_valve] = self._spinning(inp, t, slow_pct)
                cmds.primary = spinning_slow
                cmds.chiller = spinning_slow
            else:
                cmds.chiller = self._any_spinning(inp, th.overnight_chiller_speed_pct)
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
            if new_cur != S.OVERNIGHT_PUMP:             # VI: turbo valves only while the state stays
                self._set_turbo_valves(cmds, False)
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
                limit = th.turbo_on_wiggle_room_multiplier * th.engage_threshold(a.turbo_on_threshold_torr)
                ok = (wrg < limit) and (wrg > th.wrg_error_threshold_torr) and (self._foreline(inp) < limit)
                what = "open gate" if self._any_spinning(inp, th.turbo_engage_msg_speed_pct) else "turn on turbo"
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
            spinning = spinning_slow
            # vent timer (10 min Main_V4.4, 15 min VC100 – timings.vent_duration_s)
            if substate == 0:
                self.mem.vent.reset(a.now)
                venting_active, next_sub = True, 1
            elif substate == 1:
                _, done = self.mem.vent.update(a.now)
                venting_active, next_sub = True, (2 if done else 1)
            else:
                venting_active, next_sub = False, 2
            stop_now = "shut_off" in b                    # 'Shut Off' (Main_V4.4) / 'Shutdown Now' (VC100)
            if vent:
                cmds.valves[vent] = venting_active or (tgt == S.VENT_AND_SHUTDOWN)
                if am.vent_closes_on_shutdown and (stop_now or b & {"pump_to_rough", "pump_to_high_vac", "overnight_pump"}):
                    cmds.valves[vent] = False            # VC100: a button that leaves Venting closes the vent at once
            cmds.primary = spinning
            cmds.chiller = spinning
            if am.venting_buttons_stop_turbos and b & {"shut_off", "shutdown_after_vent", "pump_to_rough", "overnight_pump"}:
                self._set_motors(cmds, False)             # VC100 frame 8: motors := 0 on these buttons
            # new target
            new_tgt = S.FACILITY_OFF if tgt in (S.FACILITY_OFF, S.VENT_AND_SHUTDOWN) else tgt
            if "overnight_pump" in b:
                new_tgt = S.OVERNIGHT_PUMP
            if "pump_to_rough" in b:
                new_tgt = S.PUMPING_TO_ROUGH
            if "pump_to_high_vac" in b:
                new_tgt = S.PUMPING_TO_HIGH_VAC
            if stop_now or "shutdown_after_vent" in b:   # VC100 'Shutdown After Vent': finish venting, then off
                new_tgt = S.FACILITY_OFF
            # new current
            if tgt in (S.FACILITY_OFF, S.VENT_AND_SHUTDOWN):
                new_cur = off_or_slowing(spinning)
            else:
                new_cur = S.VENTING
            if venting_active:
                new_cur = S.VENTING
            if "overnight_pump" in b:
                new_cur = S.PUMPING_TO_ROUGH
            if stop_now:
                new_cur = off_or_slowing(spinning)
            if "pump_to_rough" in b or "pump_to_high_vac" in b:
                new_cur = S.PUMPING_TO_ROUGH
            # ...unless the chamber never lost its vacuum (2026-09-22, same rule as Turbo slowing)
            if "pump_to_high_vac" in b and self._already_evacuated(inp, a.turbo_on_threshold_torr):
                new_cur, new_tgt = S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC
                log.append(f"Chamber already at {torr_to('mbar', wrg):.1e} mBar – "
                           f"re-engaging the turbo without roughing at {_ts()}")
            staying = new_cur == S.VENTING
            if staying:
                self._spinning_turbo_valves(cmds, inp, prev, slow_pct, am.turbo_valve_gauge_rule)
            else:
                self._set_turbo_valves(cmds, False)
            out_sub = next_sub if staying else 0
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.TURBO_SLOWING:
            tab = TabPage.TURBO_SLOWING
            back = "pump_to_high_vac" in b
            to_vent = "vent" in b and not back            # VC100 'Vent' on the Turbo slowing page
            # already evacuated?  Then there is nothing to rough - go straight back to Engage Turbo
            # (2026-09-22; the VIs always routed this button through Pumping to Rough, which starts
            # the primary and opens the bypass even at high vacuum)
            back_direct = back and self._already_evacuated(inp, a.turbo_on_threshold_torr)
            if bypass:
                cmds.valves[bypass] = False
            if vent:
                if am.vent_closes_on_shutdown:
                    cmds.valves[vent] = False
                else:
                    cmds.valves[vent] = False if back else prev.valves.get(vent, False)
            self._set_gates(cmds, False)
            cmds.primary = True
            cmds.chiller = True
            self._set_motors(cmds, back)
            if back_direct:
                # chamber is already below the turbo-on threshold: keep the turbo valve open, spin
                # the turbo back up and re-open the gate through Engage Turbo.  No roughing, no bypass.
                self._set_turbo_valves(cmds, True)
                cur, tgt = S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC
                out_sub = 0
                log.append(f"Chamber already at {torr_to('mbar', wrg):.1e} mBar – "
                           f"re-engaging the turbo without roughing at {_ts()}")
            elif back:
                self._set_turbo_valves(cmds, False)
                cur, tgt = S.PUMPING_TO_ROUGH, S.PUMPING_TO_HIGH_VAC
            elif to_vent:
                self._set_turbo_valves(cmds, False)
                cur, tgt = S.VENTING, S.FACILITY_OFF
            else:
                cur, tgt = off_or_slowing(spinning_slow), S.FACILITY_OFF
                if am.turbo_valve_gauge_rule:
                    # VC100: TV_i := spinning_i ∧ (TV_i_prev ∨ foreline < turbo gauge_i); all closed once off
                    self._spinning_turbo_valves(cmds, inp, prev, slow_pct, True)
                    if cur == S.FACILITY_OFF:
                        self._set_turbo_valves(cmds, False)
                else:
                    self._set_turbo_valves(cmds, True)   # Main_V4.4: TV on until Facility Off runs

        else:  # VENT_AND_SHUTDOWN is only a target; treat like Facility Off
            all_off()
            cur = S.FACILITY_OFF

        # --- VC100: turbo standby line = motor commanded while its gate is closed
        if am.standby_when_gate_closed:
            for t in self._turbos():
                cmds.turbo_standby[t.id] = bool(cmds.turbo_motor.get(t.id, False)) and not cmds.valves.get(t.gate_valve, False)

        # --- global protection: never strand a spinning turbo (2026-09-22)
        # A turbo that is turning must keep a path to the backing pump.  The VIs' own state logic can
        # close a turbo valve on a spinning turbo - pressing 'Pump to High Vac' during Turbo slowing
        # closes it AND restarts the motor (Main_V4.4 frame 7: TURBO_VALVE_CMD := NOT(button),
        # TURBO_MOTOR_CMD := button), leaving the rotor compressing into a dead volume with the gate
        # shut too.  Leaving Venting has the same shape.  Rather than patch each button, the rule is
        # enforced here for every state: if the turbo is spinning or commanded to run, its valve
        # stays open.  Manual mode has always refused this (interlocks.Interlocks.valve, "closing
        # while turbo runs would trap it"); Auto now refuses it as well.
        for t in self._turbos():
            if cmds.valves.get(t.turbo_valve, False):
                continue
            # the same "still spinning" threshold the state machine itself uses (slowing_pct, 15 %),
            # so Facility Off is never reached with a valve this rule is holding open
            spinning = self._spinning(inp, t, slow_pct)
            if spinning or cmds.turbo_motor.get(t.id, False):
                cmds.valves[t.turbo_valve] = True
                who = "turbo" if len(cfg.turbos) == 1 else t.label.lower()
                if not prev.valves.get(t.turbo_valve, False):
                    log.append(f"Kept the {who} valve open - the {who} is still spinning at {_ts()}")

        # --- global protection: never backfill the chamber through the bypass (2026-09-22)
        # The bypass joins the chamber to the foreline.  Opening it when the chamber is already
        # BELOW the foreline pushes foreline gas back into the chamber instead of pumping it out -
        # seen at 1e-4 mBar after a 'Pump to High Vac' pressed during Turbo slowing, which routed
        # into Pumping to Rough and started a roughing cycle the chamber did not need.  Engage Turbo
        # keeps the bypass open in its first substates for the same reason it was open during
        # roughing, so this only ever blocks OPENING it: a bypass that is already open is never
        # forced shut, and a normal pump-down from atmosphere is untouched.
        for v in cfg.valves_of_kind("bypass"):
            if not cmds.valves.get(v.id, False) or prev.valves.get(v.id, False):
                self.mem.last_bypass_block = None
                continue
            fore = self._foreline(inp)
            if (not math.isnan(fore) and not math.isnan(wrg)
                    and wrg > th.gauge_error_threshold_torr and wrg < fore):
                cmds.valves[v.id] = False
                if self.mem.last_bypass_block != cur:
                    self.mem.last_bypass_block = cur
                    log.append(f"{v.label} held shut – chamber {torr_to('mbar', wrg):.1e} mBar is below "
                               f"the foreline {torr_to('mbar', fore):.1e} mBar at {_ts()}")
            else:
                self.mem.last_bypass_block = None

        # --- global protection: high-pressure turbo shut-off (turbo gauge >= 5 Torr / 5 mBar)
        for t in self._turbos():
            if cmds.turbo_motor.get(t.id, False):
                p = self._turbo_pressure(inp, t.id)
                if not (p < th.turbo_high_pressure_shutoff_torr):
                    cmds.turbo_motor[t.id] = False
                    who = "turbo" if len(cfg.turbos) == 1 else t.label.lower()
                    log.append(f"High pressure {who} shuttoff triggered at {_ts()}")

        return AutoOutput(cmds, cur, tgt, out_sub, tab, log, skip_warm, elapsed_below)
