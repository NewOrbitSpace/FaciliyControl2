"""Safety test rig: a deterministic plant + a monitor that watches the PLANT, not the commands.

Why a separate rig
------------------
`tests/conftest.Harness` couples simulated time to the wall clock (the plant advances by however
long the test process happened to take, times `time_scale`).  That is fine for functional tests but
not for safety tests: a settle wait is then as long as the CPU is slow, results differ from run to
run, and a failure cannot be replayed.  Here the plant advances by exactly the time the controller
waits (`sleep(s)` -> `advance(s)`), plus a fixed loop period per iteration.  Same seed, same run.

What the monitor checks
-----------------------
Rules about the physical state of the facility - valve POSITIONS, pressures, rotor speeds, what is
really running - sampled every plant step, including inside the controller's settle waits (the
plant keeps moving while the loop is frozen).  They are independent of how the controller is
written, so they hold for any code path; that is the point.  Sources: the engineer's rule
(2026-11-11: "the turbos must never see atmosphere when spinning, either at inlet or exhaust"), the
two VIs' own protections, and manufacturer guidance:

  * Pfeiffer / Leybold application notes: a turbo has a maximum backing pressure (2-3 hPa for pure
    turbo stages, tens of hPa for compound pumps); above it the rotor overheats from gas friction.
    A sudden air inrush on a spinning rotor lifts it ("helicopter effect") and can crash it.
  * Shimadzu TMP instruction manual: evacuate before starting (< 200 Pa); on a rapid pressure rise
    close the fore-vacuum valve / main valve at once; close the fore-vacuum valve BEFORE the backing
    pump stops (a dry pump does not stop back-flow); vent only once the rotor has stopped; never
    remove power while it rotates; no cooling water -> the pump must not run.

Every rule has an id (T.. plant level, K.. command level), a one-line statement and a grace time:
how long the condition may last before it counts (valve travel, pump spin-up).  A violation
records the plant time, the Auto state and the numbers.
"""
from __future__ import annotations

import math
import os
import random
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from facility_control.config import CONFIG_DIR, FacilityConfig, load_config  # noqa: E402
from facility_control.controller import AutoAnswerDialogs, Controller  # noqa: E402
from facility_control.hal.sim_backend import SimBackend  # noqa: E402
from facility_control.model import FacilityState as S, Mode, TabPage  # noqa: E402

PROFILES = {"small": "facility_main_v4.4.yaml", "vc100": "facility_vc100.yaml"}
CHAMBERS = tuple(PROFILES)

# Auto buttons per page as the operator sees them (gui.main_window.AUTO_BUTTONS, without Qt)
_MAIN_V44_BUTTONS: Dict[TabPage, List[str]] = {
    TabPage.FACILITY_OFF: ["pump_to_rough", "pump_to_high_vac", "overnight_pump", "vent"],
    TabPage.PUMPING_TO_ROUGH: ["overnight_pump", "pump_to_rough", "pump_to_high_vac", "vent", "shut_off"],
    TabPage.OVERNIGHT_PUMP: ["vent", "pump_to_rough", "pump_to_high_vac"],
    TabPage.PUMPING_TO_HIGH_VAC: ["shutdown", "pump_to_rough", "overnight_pump", "vent_and_shutdown", "vent"],
    TabPage.VENTING: ["overnight_pump", "pump_to_rough", "pump_to_high_vac", "shut_off"],
    TabPage.TURBO_SLOWING: ["pump_to_high_vac"],
}
ALL_BUTTONS = ("pump_to_rough", "pump_to_high_vac", "overnight_pump", "vent", "shut_off", "shut_off_once_rough",
               "shutdown", "shutdown_after_vent", "vent_and_shutdown")


def load_profile(which: str) -> FacilityConfig:
    cfg = load_config(str(CONFIG_DIR / PROFILES[which]))
    cfg.simulation.time_scale = 1.0
    # No help from the turbo controllers: in both real incidents (VC100, 2026-10-28 and 2026-11-05)
    # the HiPace pumps were crash-braked by air and reported a warning, not an error - nothing
    # stopped Auto.  The safety tests assume that of every pump.
    cfg.simulation.turbo_trips_on_pressure = False
    return cfg


class DeterministicSim(SimBackend):
    """SimBackend without the wall clock: `read()` never advances the plant, only `advance()` does."""

    _reading = False

    def read(self):
        self._reading = True
        try:
            return super().read()
        finally:
            self._reading = False

    def advance(self, wall_dt: float) -> None:
        if self._reading:
            return
        super().advance(wall_dt)


# ====================================================================================== monitor
@dataclass
class Violation:
    """One episode of a rule being broken: from the moment it counted until the condition cleared."""
    rule: str
    t: float                    # plant time it began to count (after the rule's grace time)
    state: str                  # Auto state / substate (or the mode) at that moment
    detail: str
    subject: str = ""
    error: int = 0              # controller error code active at that moment (0 = none)
    faults: Tuple[str, ...] = ()        # plant faults injected at that moment
    last_press: str = ""        # the operator's last button and where he pressed it
    recent: Tuple[str, ...] = ()        # his last few presses, oldest first
    end: Optional[float] = None         # plant time the condition cleared (None = still broken)
    peak: float = 0.0           # worst value of the rule's own quantity during the episode (Torr, where it has one)
    bypass_open: bool = False   # the bypass valve was off its seat at that moment
    since_error_s: float = 1e9  # plant seconds since the controller last had an error active

    def duration(self, now: float) -> float:
        return (self.end if self.end is not None else now) - self.t

    def __str__(self) -> str:
        tail = f"  for {self.end - self.t:.0f} s" if self.end is not None else "  (never cleared)"
        if self.peak:
            tail += f", peak {self.peak:.1f} Torr"
        ctx = "".join([f"  error {self.error}" if self.error else "",
                       f"  faults {','.join(self.faults)}" if self.faults else "",
                       f"  after '{self.last_press}'" if self.last_press else ""])
        return f"[{self.rule}] t={self.t:8.1f}s  {self.state:<24} {self.detail}{tail}{ctx}"


@dataclass
class Rule:
    id: str
    text: str
    grace_s: float = 0.0


RULES: Dict[str, Rule] = {r.id: r for r in [
    # ---- a turning turbo and pressure
    Rule("T1", "a turning turbo never has more than the high-pressure limit inside it"),
    Rule("T2", "a turning turbo's INLET (open gate) never sees an un-roughed chamber"),
    Rule("T3", "a turning turbo's EXHAUST (open turbo valve) never sees an un-roughed foreline"),
    Rule("T4", "the vent valve is never open together with the gate of a turning turbo"),
    Rule("T5", "the vent valve and the bypass valve are never open together (longer than one valve travel)", grace_s=2.0),
    Rule("T6", "the bypass and a gate are never open together while that turbo turns (inlet shorted to exhaust)"),
    # ---- a turbo and its services
    Rule("T7", "a turbo is never driven without cooling", grace_s=15.0),
    Rule("T8", "a turbo is never driven without a backing path (turbo valve open, primary running)", grace_s=20.0),
    # (1 s: on the VC100 the foreline is at atmosphere within 6 s of the primary stopping - logs)
    Rule("T9", "a turbo valve is never open on a turning turbo with the primary pump stopped", grace_s=1.0),
    Rule("T10", "a turbo motor is never on with more than the hot-start limit inside the turbo", grace_s=3.0),
    Rule("T12", "the cooling (and with it the turbo controllers' supply) is never dropped while a turbo is still "
                "above the profile's 'still spinning' speed", grace_s=15.0),
    # ---- the symptom itself, whatever the cause: what both real incidents look like in the CSV
    # (VC100 2026-11-05: 78 % -> 5 % in under 10 s; 2026-10-28: 98 % -> 18 % in under 5 s)
    Rule("T14", "a rotor is never crash-braked by gas (more than 20 % of full speed lost in 5 s)"),
    # ---- the chamber and the primary
    Rule("T11", "the bypass is never open from a higher-pressure foreline into an evacuated chamber"),
    Rule("T13", "the primary pump's run relay is never closed without its power relay"),
    # ---- commands (hold whatever the hardware does)
    Rule("K1", "the vent valve is never commanded open together with a gate or the bypass"),
    Rule("K2", "the bypass and a gate are never commanded open together"),
    Rule("K3", "a turbo is never commanded on without chiller, primary pump and its turbo valve commanded on"),
    Rule("K4", "a turbo valve is never commanded open on a turning turbo with the primary pump commanded off"),
]}

TURNING_PCT = 5.0          # the facility's own "has slowed down" speed (thresholds.turbo_slow_speed_pct)
CRASH_BRAKE_PCT = 20.0     # T14: % of full speed lost within 5 s (a braking Shimadzu loses ~1 % in 5 s)
OPEN = 0.02                # a valve that has moved this far off its seat passes gas


class SafetyMonitor:
    def __init__(self, rig: "Rig"):
        self.rig = rig
        self.violations: List[Violation] = []
        self._since: Dict[Tuple[str, str], float] = {}      # (rule, subject) -> plant time the condition began
        self._open: Dict[Tuple[str, str], Violation] = {}    # episodes still running
        self.samples = 0
        self.enabled = True
        self._before_bypass = (False, 760.0, 760.0)          # (bypass was shut, chamber, foreline) at the last sample
        self._speeds: Dict[str, List[Tuple[float, float]]] = {}   # turbo -> [(t, speed)] over the last 5 s (T14)
        cfg = rig.cfg
        th = cfg.thresholds
        self.turning_pct = th.turbo_slow_speed_pct
        self.p_hot = th.turbo_high_pressure_shutoff_torr                 # 5 Torr / 5 mBar: the VIs' hot-start limit
        lim = th.turbo_exposure_max_torr if getattr(th, "turbo_exposure_max_torr", None) is not None else \
            th.turbo_on_wiggle_room_multiplier * th.engage_threshold(th.turbo_on_threshold_torr)
        self.p_rough = 4.0 * lim                                         # 4 x the limit: gauge lag and valve travel
        self.still_spinning_pct = min(th.slowing_pct, th.turbo_spinning_pct)

    # -------------------------------------------------------------------------------- bookkeeping
    def _flag(self, rule: str, subject: str, bad: bool, detail: Callable[[], str], value: float = 0.0) -> None:
        key = (rule, subject)
        t = self.rig.t
        if not bad:
            self._since.pop(key, None)
            v = self._open.pop(key, None)
            if v is not None:
                v.end = t
            return
        start = self._since.setdefault(key, t)
        if t - start >= RULES[rule].grace_s and key not in self._open:
            rig, c = self.rig, self.rig.ctl
            state = f"{c.current.label}/{c.substate}" if c.mode == Mode.AUTO else c.mode.name
            press = f"{rig.pressed[-1][2]} in {rig.pressed[-1][1]}" if rig.pressed else ""
            v = Violation(rule, t, state, f"{subject}: {detail()}", subject,
                          error=int(c.error.code) if c.error.status else 0,
                          faults=tuple(sorted(k for k, on in rig.st.faults.items() if on)) + tuple(rig.disturbances),
                          last_press=press, since_error_s=t - rig.last_error_t,
                          bypass_open=any(rig.st.valves[b.id].position > OPEN for b in rig.cfg.valves_of_kind("bypass")),
                          recent=tuple(f"{b} in {where}" for _, where, b in rig.pressed[-5:]))
            self._open[key] = v
            self.violations.append(v)
        if key in self._open:
            self._open[key].peak = max(self._open[key].peak, value)

    # -------------------------------------------------------------------------------- the rules
    def sample(self) -> None:
        if not self.enabled:
            return
        self.samples += 1
        rig, cfg = self.rig, self.rig.cfg
        st = rig.st
        pos = {vid: v.position for vid, v in st.valves.items()}
        bypass = max((pos[v.id] for v in cfg.valves_of_kind("bypass")), default=0.0)
        vent = max((pos[v.id] for v in cfg.valves_of_kind("vent")), default=0.0)
        primary_on = st.primary_on > 0.5
        chiller_on = st.chiller_on > 0.5
        pc, pf = st.p_chamber, st.p_foreline
        any_spinning = False
        for tc in cfg.turbos:
            t = st.turbos[tc.id]
            turning = t.speed_pct > self.turning_pct
            gate, tv = pos.get(tc.gate_valve, 0.0), pos.get(tc.turbo_valve, 0.0)
            who = tc.id
            spd = f"{t.speed_pct:.0f} %"
            any_spinning = any_spinning or t.speed_pct > self.still_spinning_pct
            self._flag("T1", who, turning and t.p_body_torr > self.p_hot,
                       lambda: f"{t.p_body_torr:.1f} Torr inside the turbo at {spd}", t.p_body_torr)
            self._flag("T2", who, turning and gate > OPEN and pc > self.p_rough,
                       lambda: f"gate {gate:.0%} open onto the chamber at {pc:.1f} Torr, rotor at {spd}", pc)
            self._flag("T3", who, turning and tv > OPEN and pf > self.p_rough,
                       lambda: f"turbo valve {tv:.0%} open onto the foreline at {pf:.1f} Torr, rotor at {spd}", pf)
            self._flag("T4", who, turning and vent > OPEN and gate > OPEN,
                       lambda: f"vent {vent:.0%} and gate {gate:.0%} open, rotor at {spd}")
            self._flag("T6", who, turning and bypass > OPEN and gate > OPEN,
                       lambda: f"bypass {bypass:.0%} and gate {gate:.0%} open, rotor at {spd}")
            self._flag("T7", who, t.motor and not chiller_on,
                       lambda: f"motor on, chiller off, rotor at {spd}")
            self._flag("T8", who, t.motor and (tv < 0.98 or not primary_on),
                       lambda: f"motor on with turbo valve {tv:.0%} open, primary {'running' if primary_on else 'STOPPED'}, rotor at {spd}")
            self._flag("T9", who, turning and tv > OPEN and st.primary_on < 0.05,
                       lambda: f"turbo valve {tv:.0%} open, primary stopped, foreline {pf:.2f} Torr, rotor at {spd}")
            self._flag("T10", who, t.motor and t.p_body_torr > self.p_hot,
                       lambda: f"motor on with {t.p_body_torr:.1f} Torr inside the turbo")
            hist = self._speeds.setdefault(tc.id, [])
            hist.append((rig.t, t.speed_pct))
            while hist and rig.t - hist[0][0] > 5.0:
                hist.pop(0)
            lost = max(v for _, v in hist) - t.speed_pct
            self._flag("T14", who, lost > CRASH_BRAKE_PCT,
                       lambda: f"lost {lost:.0f} % of full speed in 5 s, now at {spd}, "
                               f"{t.p_body_torr:.1f} Torr inside the turbo", lost)
        self._flag("T5", "valves", vent > OPEN and bypass > OPEN,
                   lambda: f"vent {vent:.0%} and bypass {bypass:.0%} open, chamber {pc:.1f} Torr")
        # T11 is about the moment the bypass comes off its seat: judged on the pressures just BEFORE
        # (afterwards the gas has already gone into the chamber and the two have equalised)
        was_shut, pc0, pf0 = self._before_bypass
        self._flag("T11", "bypass", was_shut and bypass > OPEN and pc0 < 1e-2 and pf0 > 3.0 * pc0 and pf0 > 2e-2,
                   lambda: f"bypass opened with the chamber at {pc0:.1e} Torr and the foreline at {pf0:.1e} Torr")
        self._before_bypass = (bypass <= OPEN, pc, pf)
        self._flag("T12", "chiller", any_spinning and not chiller_on,
                   lambda: "chiller off with " + ", ".join(
                       f"{tc.id} at {st.turbos[tc.id].speed_pct:.0f} %" for tc in cfg.turbos
                       if st.turbos[tc.id].speed_pct > self.still_spinning_pct))
        if cfg.primary.two_stage:
            self._flag("T13", "primary", st.primary_run and not st.primary_power,
                       lambda: "run relay closed, power relay open")
        # ---- command level
        c = rig.ctl.cmds
        gates_cmd = [v.id for v in cfg.valves_of_kind("gate") if c.valves.get(v.id, False)]
        bypass_cmd = any(c.valves.get(v.id, False) for v in cfg.valves_of_kind("bypass"))
        vent_cmd = any(c.valves.get(v.id, False) for v in cfg.valves_of_kind("vent"))
        self._flag("K1", "commands", vent_cmd and (bool(gates_cmd) or bypass_cmd),
                   lambda: f"vent commanded with {'bypass ' if bypass_cmd else ''}{' '.join(gates_cmd)}")
        self._flag("K2", "commands", bypass_cmd and bool(gates_cmd),
                   lambda: f"bypass commanded with {' '.join(gates_cmd)}")
        for tc in cfg.turbos:
            if not tc.active:
                continue
            m = c.turbo_motor.get(tc.id, False)
            self._flag("K3", tc.id, m and not (c.chiller and c.primary and c.valves.get(tc.turbo_valve, False)),
                       lambda: f"motor commanded; chiller {c.chiller}, primary {c.primary}, "
                               f"turbo valve {c.valves.get(tc.turbo_valve, False)}")
        tv_cmd = [v.id for v in cfg.valves_of_kind("turbo") if c.valves.get(v.id, False)
                  and any(st.turbos[t.id].speed_pct > self.turning_pct for t in cfg.turbos_on_valve(v.id))]
        self._flag("K4", "commands", bool(tv_cmd) and not c.primary,
                   lambda: f"{' '.join(tv_cmd)} commanded open, primary commanded off")

    # -------------------------------------------------------------------------------- reporting
    def found(self, *rules: str) -> List[Violation]:
        return [v for v in self.violations if not rules or v.rule in rules]

    def report(self, limit: int = 12) -> str:
        lines = [str(v) for v in self.violations[:limit]]
        if len(self.violations) > limit:
            lines.append(f"... and {len(self.violations) - limit} more")
        rules = sorted({v.rule for v in self.violations})
        legend = [f"   {r}: {RULES[r].text}" for r in rules]
        return "\n".join(lines + ["rules broken:"] + legend)


# ====================================================================================== rig
class Rig:
    """One facility (profile + deterministic plant + controller in Auto) with the safety monitor on."""

    def __init__(self, which: str, *, loop_dt: float = 1.0, seed: int = 1, mode: Mode = Mode.AUTO,
                 tweak: Optional[Callable[[FacilityConfig], None]] = None):
        self.which = which
        self.cfg = load_profile(which)
        if tweak:
            tweak(self.cfg)
        self.loop_dt = loop_dt
        self.backend = DeterministicSim(self.cfg, seed=seed)
        self.st = self.backend.state
        self.dialogs = AutoAnswerDialogs()                    # default answer: first button ('Yes')
        self.t0 = 1_800_000_000.0
        self.monitor: Optional[SafetyMonitor] = None
        self.ctl = Controller(self.cfg, self.backend, dialogs=self.dialogs, initial_mode=mode,
                              sleep=self._sleep, clock=lambda: self.t0 + self.backend._sim_time)
        self.ctl.backend.open()
        self.monitor = SafetyMonitor(self)
        self.turbos = [t for t in self.cfg.turbos if t.in_use]
        self.pressed: List[Tuple[float, str, str]] = []       # (t, state, button) - to replay a failure
        self.disturbances: List[str] = []                     # plant disturbances that are not sim 'faults' (leak, inrush)
        self.came_from_admin = False                          # Auto was handed this plant from Admin mode
        self.last_error_t = -1e9                              # plant time the controller last had an error active

    # ---- time
    @property
    def t(self) -> float:
        return self.backend._sim_time

    def _advance(self, seconds: float) -> None:
        left = seconds
        while left > 1e-9:
            h = min(0.25, left)
            self.backend.advance(h)
            left -= h
            if self.monitor:
                self.monitor.sample()

    def _sleep(self, seconds: float) -> None:                 # the controller's settle waits: exact plant time
        self._advance(seconds)

    def tick(self, n: int = 1):
        for _ in range(n):
            self.ctl.iterate()
            if self.ctl.error.status:
                self.last_error_t = self.t
            self._advance(self.loop_dt)
        return self.ctl.snapshot()

    def run(self, seconds: float):
        end = self.t + seconds
        s = self.ctl.snapshot()
        while self.t < end:
            s = self.tick()
        return s

    def run_until(self, pred: Callable[[object], bool], max_s: float = 3 * 3600.0, what: str = "condition"):
        end = self.t + max_s
        while self.t < end:
            s = self.tick()
            if pred(s):
                return s
        s = self.ctl.snapshot()
        raise AssertionError(f"{what} not reached within {max_s:.0f} s of plant time; "
                             f"state {s.current.label}/{s.substate}, error {s.error.code if s.error.status else '-'}")

    # ---- operator
    def buttons(self) -> List[str]:
        """The Auto buttons on the page the operator is looking at."""
        s = self.ctl.snapshot()
        if self.cfg.auto_buttons:
            return [act for _, act in self.cfg.auto_buttons.get(int(s.tab.value), [])]
        return list(_MAIN_V44_BUTTONS.get(s.tab, []))

    def press(self, button: str):
        s = self.ctl.snapshot()
        self.pressed.append((self.t, f"{s.current.label}/{s.substate}", button))
        self.ctl.request_auto_button(button)

    def speed(self, tid: str) -> float:
        return self.st.turbos[tid].speed_pct

    def any_turning(self) -> bool:
        return any(self.st.turbos[t.id].speed_pct > TURNING_PCT for t in self.turbos)

    # ---- canned routes into the states the tests start from
    def to_rough_hold(self):
        self.tick(2)
        self.press("pump_to_rough")
        return self.run_until(lambda s: s.current == S.PUMPING_TO_ROUGH and s.substate == 3
                              and self.st.p_chamber < self.cfg.thresholds.turbo_on_threshold_torr, what="rough vacuum")

    def to_high_vac(self, full_speed: bool = True):
        self.tick(2)
        self.press("pump_to_high_vac")
        self.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, what="Pumping to High Vac")
        if full_speed:
            self.run_until(lambda s: all(self.speed(t.id) > 95 for t in self.turbos), 3600, "turbos at full speed")
        return self.ctl.snapshot()

    def history(self) -> str:
        return "\n".join(f"   t={t:8.1f}s  {state:<24} pressed {b}" for t, state, b in self.pressed[-12:])

    def assert_safe(self, allow: Iterable[str] = (), context: str = "") -> None:
        allow = set(allow)
        bad = [v for v in self.monitor.violations if v.rule not in allow]
        if bad:
            keep = self.monitor.violations
            self.monitor.violations = bad
            try:
                text = self.monitor.report()
            finally:
                self.monitor.violations = keep
            raise AssertionError(f"{self.which}: {len(bad)} safety violation(s){' – ' + context if context else ''}\n"
                                 f"{text}\noperator history:\n{self.history()}")


def seeded(seed: int) -> random.Random:
    return random.Random(seed)
