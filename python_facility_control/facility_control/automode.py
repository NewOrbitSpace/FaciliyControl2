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

Plant-safety rules enforced for EVERY Auto state (end of `step`, highest priority first):
  1. a turbo that is turning never sees an un-roughed chamber - not at its exhaust (bypass + turbo
     valve) and not at its inlet (gate).  Isolating a turning turbo for the time it takes to rough
     the chamber is acceptable; exposing it is not (2026-11-11);
  2. the bypass is never opened into a chamber that is below the foreline (2026-09-22);
  3. otherwise a turning turbo keeps its backing: turbo valve open and primary pump running
     (2026-09-22 / 2026-09-23).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .config import FacilityConfig, TurboConfig
from .model import Commands, FacilityState as S, Inputs, TabPage, TurboInputs, primary_is_running
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
    last_gate_open: Optional[float] = None       # time of the last gate OPEN command (gate_open_stagger_s)
    standby_wait_start: Optional[float] = None   # Engage, turbos-first: when the turbos were started
    hv_request_wrg: Optional[float] = None       # chamber pressure when high vac was asked for (Torr)
    kept_tv_logged: Set[str] = field(default_factory=set)   # 'kept the valve open' logged once per episode
    isolated_tv: Set[str] = field(default_factory=set)      # turbo valves shut by the exhaust protection


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

    def _open_gates_staggered(self, cmds: Commands, prev: Commands, now: float, log: List[str]) -> bool:
        """Open the gates of the turbos in use - all at once, or (gate_open_stagger_s > 0, VC100's
        shared compressed-air line) one gate per `gate_open_stagger_s`, in turbo order.  Gates that are
        already open stay open.  Returns True once every gate is open."""
        stagger = float(self.cfg.timings.gate_open_stagger_s or 0.0)
        if stagger <= 0:
            self._set_gates(cmds, True)
            return True
        waiting: List[str] = []
        for t in self._turbos():
            if prev.valves.get(t.gate_valve, False):
                cmds.valves[t.gate_valve] = True
            elif t.gate_valve not in waiting:
                waiting.append(t.gate_valve)
        if not waiting:
            return True
        last = self.mem.last_gate_open
        if last is None or now - last >= stagger:
            gid = waiting.pop(0)
            cmds.valves[gid] = True
            self.mem.last_gate_open = now
            total = len({t.gate_valve for t in self._turbos()})
            log.append(f"Opening {self.cfg.valves[gid].label} ({total - len(waiting)}/{total})"
                       + (f" – next gate in {stagger:g} s" if waiting else ""))
        for gid in waiting:
            cmds.valves[gid] = False
        return not waiting

    def _at_standby(self, inp: Inputs, t: TurboConfig, waited: float) -> bool:
        """Engage, turbos-first branch: has this turbo reached its standby speed?

        Speed signal (HiPace): speed within 3 % of its standby speed ([P:717] read from the pump, else
        the profile's standby_speed_pct, else the Pfeiffer default 66.7 %) - or above it.
        Shimadzu contacts (no speed): run for at least turbo_standby_min_s, no longer accelerating,
        no alarm."""
        ti = inp.turbos.get(t.id)
        if ti is None or ti.error:
            return False
        if ti.has_contacts:
            # no speed signal: running, run up for `standby_runup_s` (per turbo, else
            # timings.turbo_standby_min_s) and - if the pump has one (TMP1) - no 'accelerating' contact.
            # TMP4 only has 'running' (DI19) and '>80 %' (DI18): standby stays below 80 %, so the
            # run-up time is all there is to go by.
            runup = float(t.params.get("standby_runup_s", self.tm.turbo_standby_min_s))
            return (waited >= runup and ti.contact("rotating") and not ti.contact("accelerating")
                    and not ti.contact("alarm"))
        target = ti.standby_set_pct if ti.standby_set_pct is not None else t.params.get("standby_speed_pct", 66.7)
        return ti.speed_pct >= float(target) - 3.0

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

    # ---- exhaust / inlet protection (2026-11-11) ----------------------------------------------
    def _turning(self, inp: Inputs, cmds: Commands, tc: TurboConfig) -> bool:
        """Is this rotor moving at all (or told to)?  Deliberately the LOWEST evidence of rotation the
        facility has - `turbo_slow_speed_pct` (5 %, what Manual mode calls 'slowed down'), the
        Rotating contact, or the motor command - not the 15 % / 50 % 'still spinning' thresholds the
        states use to decide when to stop backing: a rotor at 40 % must not see atmosphere either."""
        if cmds.turbo_motor.get(tc.id, False):
            return True
        ti = inp.turbos.get(tc.id)
        if ti is None:
            return False
        if ti.has_contacts:
            return bool(ti.contact("rotating"))
        return ti.speed_pct > self.th.turbo_slow_speed_pct

    def _exposure_limit(self, turbo_on_threshold_torr: float) -> float:
        """Highest chamber pressure a turning turbo may be joined to (Torr).

        `thresholds.turbo_exposure_max_torr/_mbar` if the profile gives one; else the pressure both
        VIs accept in Engage Turbo with the bypass AND the turbo valves open: wiggle room x the
        turbo-on threshold (small chamber 5 x 2e-1 = 1 mBar, VC100 5 x 0.25 = 1.25 mBar)."""
        if self.th.turbo_exposure_max_torr is not None:
            return self.th.turbo_exposure_max_torr
        return self.th.turbo_on_wiggle_room_multiplier * self.th.engage_threshold(turbo_on_threshold_torr)

    def _chamber_roughed(self, inp: Inputs, turbo_on_threshold_torr: float) -> bool:
        """Chamber gauge readable AND at or below the exposure limit.  An unreadable gauge counts as
        NOT roughed: with no reading the only safe assumption is atmosphere."""
        wrg = self._wrg(inp)
        if math.isnan(wrg) or wrg <= self.th.wrg_error_threshold_torr:
            return False
        return wrg <= self._exposure_limit(turbo_on_threshold_torr)

    def _backing_safe(self, inp: Inputs, cmds: Commands, valve_id: str, turbo_on_threshold_torr: float) -> bool:
        """May a CLOSED turbo valve be opened again onto a turning turbo?  Only onto a foreline that
        is really being pumped: primary pump running AND the foreline gauge readable AND either
        below the turbos' normal backing pressure (`turbo_valve_foreline_ok_torr`, else the turbo-on
        threshold) or below every turbo gauge behind that valve (gas then flows OUT of the turbo -
        the VC100 VI's rule).  Same test as interlocks.backing_ready, but always on."""
        if not primary_is_running(self.cfg.primary, inp, cmds):
            return False
        fg = self.cfg.foreline_gauge
        if fg is None:
            return True
        bad = self.th.gauge_error_threshold_torr
        pf = inp.pressure(fg.id)
        if math.isnan(pf) or pf <= bad:
            return False
        ok_below = self.th.turbo_valve_foreline_ok_torr
        if ok_below is None:
            ok_below = turbo_on_threshold_torr
        if pf < ok_below:
            return True
        gauges = {g.id for g in (self.cfg.turbo_gauge(t.id) for t in self.cfg.turbos_on_valve(valve_id)) if g}
        if not gauges:
            return False
        for gid in gauges:
            pt = inp.pressure(gid)
            if math.isnan(pt) or pt <= bad or not pf < pt:
                return False
        return True

    def _gates_must_wait(self, inp: Inputs, prev: Commands, turbo_on_threshold_torr: float,
                         log: List[str]) -> bool:
        """Engage Turbo, just before a gate opens: with a turbo already turning, is the chamber
        (still) low enough?  The VIs check the pressure once, a few iterations - or, turbos-first, a
        whole run-up to standby - before the gates move; a chamber that came up in between (hand
        vent valve, leak) would otherwise be opened straight onto a turning rotor's inlet.  With
        every turbo at rest nothing changes: the gates open as in the VIs."""
        if not any(self._turning(inp, prev, t) for t in self._turbos()):
            return False
        if self._chamber_roughed(inp, turbo_on_threshold_torr):
            return False
        wrg = self._wrg(inp)
        shown = "not readable" if math.isnan(wrg) else f"at {torr_to('mbar', wrg):.1e} mBar"
        log.append(f"Pressure NOT low enough to open gate! Chamber {shown} with a turbo turning "
                   f"(limit {torr_to('mbar', self._exposure_limit(turbo_on_threshold_torr)):.1e} mBar) – "
                   f"gates stay closed, roughing again at {_ts()}")
        return True

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
        want: Dict[str, bool] = {}
        for t in self._turbos():
            spin = self._spinning(inp, t, pct)
            if gauge_rule:
                p_t = self._turbo_pressure(inp, t.id)
                spin = spin and (prev.valves.get(t.turbo_valve, False) or fore < p_t)
            want[t.turbo_valve] = want.get(t.turbo_valve, False) or spin   # shared valve: any turbo needs it
        cmds.valves.update(want)

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
        if "pump_to_high_vac" in b:
            self.mem.hv_request_wrg = wrg               # Engage decides turbos-first on this pressure
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
            want_tv: Dict[str, bool] = {}
            for t in self._turbos():
                if am.turbo_valve_gauge_rule:
                    # VC100 For-loop: TV_i := done ∨ TV_i_prev ∨ (bypass ∧ WRG < turbo gauge_i)
                    early = bypass_prev and wrg > th.gauge_error_threshold_torr and wrg < self._turbo_pressure(inp, t.id)
                    open_i = done or prev.valves.get(t.turbo_valve, False) or early
                else:
                    open_i = done or any_prev
                want_tv[t.turbo_valve] = want_tv.get(t.turbo_valve, False) or (leaving and open_i)
            cmds.valves.update(want_tv)
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
                want_tv = {}
                for t in self._turbos():
                    want_tv[t.turbo_valve] = want_tv.get(t.turbo_valve, False) or self._spinning(inp, t, slow_pct)
                cmds.valves.update(want_tv)
                cmds.primary = spinning_slow
                cmds.chiller = spinning_slow
            else:
                cmds.chiller = self._any_spinning(inp, th.overnight_chiller_speed_pct)
            time_reached = a.turbo_engage_time is not None and a.now > a.turbo_engage_time
            new_cur = S.PUMPING_TO_ROUGH if time_reached else S.OVERNIGHT_PUMP
            new_tgt = S.PUMPING_TO_HIGH_VAC if time_reached else tgt
            if time_reached:
                log.append(f"Timestamp reached, turning on pumps at {_ts()}")
                self.mem.hv_request_wrg = wrg
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
                first = th.turbo_start_before_gates_below_torr
                # decided on the chamber pressure when high vac was ASKED for: opening the turbo valves
                # (turbo bodies not yet evacuated) lifts the chamber well above it before this point
                asked = self.mem.hv_request_wrg
                p_dec = min(wrg, asked) if (asked is not None and not math.isnan(asked)) else wrg
                if self._gates_must_wait(inp, prev, a.turbo_on_threshold_torr, log):
                    bp, gv, out_sub = False, False, 0    # a turning turbo's inlet never sees it
                    new_cur = S.PUMPING_TO_ROUGH
                elif first is not None and th.wrg_error_threshold_torr < p_dec < first:
                    wrg_shown = p_dec
                    # low pressure (2026-10-28): turbos first, gates closed -> they run up to standby
                    bp, gv, out_sub = False, False, 6
                    self._set_motors(cmds, True)
                    self.mem.standby_wait_start = a.now
                    log.append(f"Chamber at {torr_to('mbar', wrg_shown):.1e} mBar (< {torr_to('mbar', first):.0e}): "
                               f"starting the turbos with the gates closed – the gates open once every turbo "
                               f"is at standby speed ({_ts()})")
                else:
                    bp, gv, out_sub = False, True, 5     # open gate(s) - one by one on a shared air line
            elif substate == 6:                          # turbos-first: wait for standby speed
                bp, gv = False, False
                self._set_motors(cmds, True)
                start = self.mem.standby_wait_start if self.mem.standby_wait_start is not None else a.now
                self.mem.standby_wait_start = start
                waited = a.now - start
                waiting = [t.label for t in self._turbos() if not self._at_standby(inp, t, waited)]
                out_sub = 6
                if not waiting:
                    out_sub = 7
                    log.append(f"Turbos at standby speed after {waited:.0f} s – opening the gate valves ({_ts()})")
                elif waited >= float(self.tm.turbo_standby_timeout_s):
                    out_sub = 7
                    log.append(f"!! {', '.join(waiting)} not at standby speed after {waited / 60:.0f} min – "
                               f"opening the gate valves anyway ({_ts()})")
            elif substate == 7:                          # turbos-first: open the gates, turbos running
                bp, gv = False, None                     # gates handled here
                self._set_motors(cmds, True)
                out_sub = 7
                if self._gates_must_wait(inp, prev, a.turbo_on_threshold_torr, log):
                    gv, out_sub = False, 0               # close any gate already open, rough again
                    self.mem.standby_wait_start = None
                    new_cur = S.PUMPING_TO_ROUGH
                elif self._open_gates_staggered(cmds, prev, a.now, log):
                    out_sub = 0
                    self.mem.standby_wait_start = None
                    self.mem.hv_request_wrg = None
                    new_cur = S.PUMPING_TO_HIGH_VAC
            else:  # 5 – start the turbo(s)
                bp, gv, out_sub = False, True, 0
                self._set_motors(cmds, True)
                self.mem.hv_request_wrg = None
                new_cur = S.PUMPING_TO_HIGH_VAC
            if bypass:
                cmds.valves[bypass] = bp
            if gv is None:
                pass
            elif gv:
                if not self._open_gates_staggered(cmds, prev, a.now, log) and out_sub == 5:
                    out_sub = 4                          # turbos start only when every gate is open
            else:
                self._set_gates(cmds, False)
            cur, tgt = new_cur, new_tgt

        # =====================================================================
        elif cur == S.PUMPING_TO_HIGH_VAC:
            tab = TabPage.PUMPING_TO_HIGH_VAC
            self._set_turbo_valves(cmds, True)
            self._open_gates_staggered(cmds, prev, a.now, log)
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

        if cur in (S.FACILITY_OFF, S.VENTING):
            self.mem.hv_request_wrg = None             # a stale request pressure never carries over
        # --- VC100: turbo standby line = motor commanded while its gate is closed - except in
        #     Pumping to High Vac, where every turbo runs at full speed (2026-10-27), even while a gate
        #     is still waiting for its turn on the shared compressed-air line
        if am.standby_when_gate_closed:
            for t in self._turbos():
                cmds.turbo_standby[t.id] = (bool(cmds.turbo_motor.get(t.id, False))
                                            and not cmds.valves.get(t.gate_valve, False)
                                            and cur != S.PUMPING_TO_HIGH_VAC)
        if cur == S.PUMPING_TO_HIGH_VAC:
            for t in self._turbos():
                cmds.turbo_standby[t.id] = False
                cmds.turbo_speed_pct[t.id] = None          # never a reduced speed in high vac

        # ================================================================= global protections
        fitted = [t for t in cfg.turbos if t.in_use]     # also a turbo Auto has just taken out (stuck
        bypass_ids = [v.id for v in cfg.valves_of_kind("bypass")]   # gate): it may still be coasting

        # --- 2. never backfill the chamber through the bypass (2026-09-22)
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

        # --- 1. a turning turbo's EXHAUST never sees an un-roughed chamber (2026-11-11)
        # With the bypass open the foreline IS the chamber.  A turbo valve that is open at that
        # moment puts whatever is in the chamber - atmosphere, after a vent - on the exhaust of its
        # turbo.  Both VIs avoided this by state logic: leaving Venting / Turbo slowing / Overnight
        # for Pumping to Rough closes the turbo valves (frames 7 and 8: TV := [F,F,F] when the next
        # state is not the same one), the command frame waits its settle time, and only then does
        # Pumping to Rough open the bypass; the valves stay shut until rough vacuum (`done`, or on
        # the VC100 once the chamber is below the turbo gauge).  The 2026-09-22 rule "a spinning
        # turbo always keeps its valve" overrode exactly that and held the valves open through the
        # roughing of a vented chamber.  The priorities are now the right way round:
        #     never expose  >  never strand.
        # While the bypass is open (or about to open, or not yet confirmed shut) and the chamber is
        # above the exposure limit (or its gauge unreadable):
        #   * every turbo valve with a turning turbo behind it is SHUT, and that turbo's motor is
        #     switched off - it coasts isolated for the time the roughing takes (accepted practice;
        #     Engage Turbo restarts it afterwards);
        #   * the bypass is HELD shut until each of those valves was commanded shut in an earlier
        #     iteration (so it had its settle wait) AND its reed contact reads shut.
        # Isolated valves re-open through rule 3 below once the foreline is pumped down again.
        roughed = self._chamber_roughed(inp, a.turbo_on_threshold_torr)
        bypass_open_or_opening = any(cmds.valves.get(b_, False) or prev.valves.get(b_, False)
                                     or inp.valve_reads.get(b_, False) for b_ in bypass_ids)
        isolate: Set[str] = set()
        if bypass_open_or_opening and not roughed:
            isolate = {t.turbo_valve for t in fitted
                       if self._turning(inp, cmds, t) or self._turning(inp, prev, t)}
        if isolate:
            not_shut_yet = sorted(tv for tv in isolate
                                  if prev.valves.get(tv, False) or inp.valve_reads.get(tv, False))
            for tv in isolate:
                cmds.valves[tv] = False
            for t in fitted:
                if t.turbo_valve in isolate:
                    cmds.turbo_motor[t.id] = False
                    cmds.turbo_standby[t.id] = False
            if not_shut_yet:
                for b_ in bypass_ids:
                    if not prev.valves.get(b_, False):
                        cmds.valves[b_] = False          # the bypass waits for the turbo valves
            new_iso = isolate - self.mem.isolated_tv
            if new_iso:
                labels = ", ".join(cfg.valves[tv].label for tv in sorted(new_iso))
                shown = "not readable" if math.isnan(wrg) else f"at {torr_to('mbar', wrg):.1e} mBar"
                log.append(f"Closed {labels} BEFORE the bypass valve – turbo still turning and the chamber is "
                           f"{shown} (limit {torr_to('mbar', self._exposure_limit(a.turbo_on_threshold_torr)):.1e} mBar): "
                           f"a turning turbo's exhaust must never see it.  The turbo coasts isolated until the "
                           f"chamber is roughed ({_ts()})")
            self.mem.isolated_tv |= isolate

        # --- 3. never strand a turning turbo - when that is safe (2026-09-22, 2026-09-23, 2026-11-11)
        # A turbo that is turning should keep a path to a working backing pump.  The VIs' own state
        # logic can close a turbo valve on a spinning turbo with no need for it - pressing 'Pump to
        # High Vac' during Turbo slowing at 1e-5 mBar closes it AND restarts the motor (Main_V4.4
        # frame 7), leaving the rotor compressing into a dead volume.  So, for every state:
        #   * a valve that is OPEN on a turning turbo stays open, unless rule 1 above says isolate;
        #   * a valve that is SHUT on a turning turbo (isolated by rule 1, or by the state) is opened
        #     again only onto a foreline that is really pumped (`_backing_safe`) - never onto a
        #     foreline at chamber pressure;
        #   * while any turbo is turning the primary keeps running.  Holding a turbo valve open only
        #     helps with a working pump behind it: with the primary OFF the open valve exposes the
        #     rotor to a foreline that backfills towards atmosphere (2026-09-23: Overnight Pump
        #     stopped the primary with the turbo at ~99 %, the turbo body reached ~27 mBar, 5007).
        needs_backing = False
        for t in fitted:
            # the same "still spinning" threshold the state machine itself uses (slowing_pct, 15 %),
            # so Facility Off is never reached with a valve this rule is holding open
            spinning = self._spinning(inp, t, slow_pct)
            if not (spinning or cmds.turbo_motor.get(t.id, False)):
                continue
            needs_backing = True
            tv = t.turbo_valve
            if tv in isolate or cmds.valves.get(tv, False):
                continue
            was_open = prev.valves.get(tv, False)
            safe = was_open or self._backing_safe(inp, cmds, tv, a.turbo_on_threshold_torr)
            if not safe and not am.turbo_valve_needs_backing:
                continue                                 # stays isolated until the foreline is pumped
            # (with `turbo_valve_needs_backing` the controller applies the very same test to every
            #  turbo-valve opening, holds it and tells the operator why - so just keep asking)
            cmds.valves[tv] = True
            who = "turbo" if len(cfg.turbos) == 1 else t.label.lower()
            if tv in self.mem.isolated_tv:
                if safe and not am.turbo_valve_needs_backing:
                    log.append(f"Re-opened {cfg.valves[tv].label} – the foreline is pumped down again, "
                               f"the {who} has its backing back at {_ts()}")
            elif not was_open and t.id not in self.mem.kept_tv_logged:
                self.mem.kept_tv_logged.add(t.id)
                log.append(f"Kept the {who} valve open - the {who} is still spinning at {_ts()}")
        for t in cfg.turbos:                         # re-arm the message once the turbo has stopped
            if t.id in self.mem.kept_tv_logged and not self._spinning(inp, t, slow_pct) \
                    and not cmds.turbo_motor.get(t.id, False):
                self.mem.kept_tv_logged.discard(t.id)
        # an isolation episode ends when the valve is open again or nothing behind it turns any more
        for tv in list(self.mem.isolated_tv):
            if tv in isolate:
                continue
            if prev.valves.get(tv, False) or not any(self._turning(inp, cmds, t) for t in cfg.turbos_on_valve(tv)):
                self.mem.isolated_tv.discard(tv)         # really open again (not merely asked for)
        if needs_backing and not cmds.primary:
            cmds.primary = True
            if not prev.primary:
                log.append(f"Kept the primary pump running - a turbo is still spinning at {_ts()}")

        # --- global protection: high-pressure turbo shut-off (turbo gauge >= 5 Torr / 5 mBar)
        for t in self._turbos():
            if cmds.turbo_motor.get(t.id, False):
                p = self._turbo_pressure(inp, t.id)
                if not (p < th.turbo_high_pressure_shutoff_torr):
                    cmds.turbo_motor[t.id] = False
                    who = "turbo" if len(cfg.turbos) == 1 else t.label.lower()
                    log.append(f"High pressure {who} shuttoff triggered at {_ts()}")

        return AutoOutput(cmds, cur, tgt, out_sub, tab, log, skip_warm, elapsed_below)
