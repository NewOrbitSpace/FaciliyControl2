"""Situations the safety tests start from, fault catalogue and plant variants (helpers, no tests).

A *situation* is a named way of bringing a fresh rig into a state an operator can really be in:
which Auto state, how fast the turbos turn, what the chamber pressure is.  They are deliberately
written as operator actions (press, wait) so each is reachable on the real facility.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from tests.safety_rig import RULES, Rig, S, Violation

ATM = 700.0          # "the chamber is vented" (Torr)


# ------------------------------------------------------------------------------- plant variants
def plant_default(cfg) -> None:
    """The profile's own plant: for the VC100 the numbers measured from its logs (HiPace pumps coast
    ~45 min, the Shimadzu pumps brake to a stop in ~9.5 min, roughing takes ~6 min)."""


def plant_long_coast(cfg) -> None:
    """Every turbo coasts for an hour and none brakes: longer than any vent or roughing, so a rotor
    is always still turning when the pressure comes up.  (The small chamber's coast time has not
    been measured; this is the safe assumption for it.)"""
    cfg.simulation.turbo_spindown_s = 3600.0
    cfg.simulation.turbo_overrides = {}


def plant_fast_rough(cfg) -> None:
    """A stronger roughing line: the chamber is roughed while the turbos have hardly slowed."""
    plant_long_coast(cfg)
    cfg.simulation.primary_pump_speed_l_s *= 4.0
    cfg.simulation.bypass_conductance_l_s *= 4.0


PLANTS: Dict[str, Callable] = {"default": plant_default, "long_coast": plant_long_coast, "fast_rough": plant_fast_rough}


# ------------------------------------------------------------------------------- situations
def sit_off_cold(r: Rig):
    r.tick(3)


def sit_rough_start(r: Rig):
    r.tick(2); r.press("pump_to_rough"); r.run(30)


def sit_rough_mid(r: Rig):
    r.tick(2); r.press("pump_to_rough")
    r.run_until(lambda s: r.st.p_chamber < 20.0, what="20 Torr")


def sit_rough_hold(r: Rig):
    r.to_rough_hold(); r.run(60)


def sit_rough_for_high_vac(r: Rig):
    r.tick(2); r.press("pump_to_high_vac")
    r.run_until(lambda s: r.st.p_chamber < 20.0, what="20 Torr")


def sit_overnight_cold(r: Rig):
    r.to_rough_hold(); r.ctl.request_set_engage_time(r.ctl.clock() + 1e7); r.press("overnight_pump")
    r.run_until(lambda s: s.current == S.OVERNIGHT_PUMP, what="Overnight"); r.run(60)


def sit_high_vac(r: Rig):
    r.to_high_vac(); r.run(120)


def sit_high_vac_running_up(r: Rig):
    r.to_high_vac(full_speed=False); r.run(5)


def sit_slowing_fast(r: Rig):
    r.to_high_vac(); r.press("shutdown")
    r.run_until(lambda s: s.current == S.TURBO_SLOWING, what="Turbo slowing"); r.run(5)


def sit_slowing_slow(r: Rig):
    sit_slowing_fast(r)
    r.run_until(lambda s: max(r.speed(t.id) for t in r.turbos) < 70 or s.current != S.TURBO_SLOWING, what="70 %")


def sit_overnight_spinning(r: Rig):
    r.to_high_vac(); r.ctl.request_set_engage_time(r.ctl.clock() + 1e7); r.press("overnight_pump")
    r.run_until(lambda s: s.current == S.OVERNIGHT_PUMP, what="Overnight"); r.run(10)


def sit_rough_from_high_vac(r: Rig):
    """Pump to Rough pressed in high vacuum: Disengage -> Pumping to Rough, turbos coasting."""
    r.to_high_vac(); r.press("pump_to_rough")
    r.run_until(lambda s: s.current == S.PUMPING_TO_ROUGH, what="Pumping to Rough"); r.run(10)


def sit_venting_running_start(r: Rig):
    """'Vent' from high vacuum: the turbos keep running; the vent valve has just opened."""
    r.to_high_vac(); r.press("vent")
    r.run_until(lambda s: s.current == S.VENTING, what="Venting"); r.run(8)


def sit_venting_running_mid(r: Rig):
    sit_venting_running_start(r)
    r.run_until(lambda s: r.st.p_chamber > 5.0 or s.current != S.VENTING, what="5 Torr")


def sit_venting_running_atm(r: Rig):
    sit_venting_running_start(r)
    r.run_until(lambda s: r.st.p_chamber > ATM or s.current != S.VENTING, what="atmosphere")


def sit_venting_coasting_atm(r: Rig):
    """The engineer's state (2026-11-11): chamber at atmosphere, turbos spinning down, turbo valves
    open, primary backing them - reached with 'Vent and shutdown'."""
    r.to_high_vac(); r.press("vent_and_shutdown")
    r.run_until(lambda s: s.current == S.VENTING, what="Venting")
    r.run_until(lambda s: r.st.p_chamber > ATM or s.current != S.VENTING, what="atmosphere")


def sit_venting_from_slowing_atm(r: Rig):
    """The same state by the other road (VC100 only): Shutdown, then 'Vent' on the Turbo slowing page."""
    r.to_high_vac(); r.press("shutdown")
    r.run_until(lambda s: s.current == S.TURBO_SLOWING, what="Turbo slowing")
    r.run(20)
    r.press("vent")
    r.run_until(lambda s: s.current == S.VENTING, what="Venting")
    r.run_until(lambda s: r.st.p_chamber > ATM or s.current != S.VENTING, what="atmosphere")


def sit_vented_hold(r: Rig):
    """'Vent' from high vacuum, vent timer finished: holding in Venting with the vent valve shut."""
    sit_venting_running_start(r)
    r.run_until(lambda s: s.current == S.VENTING and s.substate == 2, what="vent finished"); r.run(30)


def sit_off_after_vent(r: Rig):
    """Vent and shutdown completed: Facility Off, chamber at atmosphere (turbos may still coast below
    the state machine's 'still spinning' speed)."""
    r.to_high_vac(); r.press("vent_and_shutdown")
    r.run_until(lambda s: s.current == S.VENTING, what="Venting")
    r.run_until(lambda s: s.current == S.FACILITY_OFF, 6 * 3600.0, what="Facility Off"); r.tick(3)


def sit_off_coasting_evacuated(r: Rig):
    """Shutdown completed: Facility Off the moment the turbos drop below the 'still spinning' speed -
    the chamber still evacuated, the rotors still turning slowly."""
    r.to_high_vac(); r.press("shutdown")
    r.run_until(lambda s: s.current == S.FACILITY_OFF, 4 * 3600.0, what="Facility Off"); r.tick(2)


SITUATIONS: Dict[str, Callable[[Rig], None]] = {
    "off_cold": sit_off_cold,
    "rough_start": sit_rough_start,
    "rough_mid": sit_rough_mid,
    "rough_hold": sit_rough_hold,
    "rough_for_high_vac": sit_rough_for_high_vac,
    "overnight_cold": sit_overnight_cold,
    "high_vac": sit_high_vac,
    "high_vac_running_up": sit_high_vac_running_up,
    "slowing_fast": sit_slowing_fast,
    "slowing_slow": sit_slowing_slow,
    "overnight_spinning": sit_overnight_spinning,
    "rough_from_high_vac": sit_rough_from_high_vac,
    "venting_running_start": sit_venting_running_start,
    "venting_running_mid": sit_venting_running_mid,
    "venting_running_atm": sit_venting_running_atm,
    "venting_coasting_atm": sit_venting_coasting_atm,
    "venting_from_slowing_atm": sit_venting_from_slowing_atm,
    "vented_hold": sit_vented_hold,
    "off_after_vent": sit_off_after_vent,
    "off_coasting_evacuated": sit_off_coasting_evacuated,
}

# situations in which a turbo is (or may be) turning - where plant faults matter most
SPINNING_SITUATIONS = ["high_vac", "high_vac_running_up", "slowing_fast", "overnight_spinning", "rough_from_high_vac",
                       "venting_running_mid", "venting_coasting_atm", "off_coasting_evacuated"]
# situations that only exist on one facility (the button is not on the other's page)
ONLY_ON = {"venting_from_slowing_atm": ("vc100",)}


def exists_on(situation: str, which: str) -> bool:
    return which in ONLY_ON.get(situation, (which,))


def make(which: str, situation: str, plant: str = "long_coast", seed: int = 1, **kw) -> Rig:
    r = Rig(which, seed=seed, tweak=PLANTS[plant], **kw)
    SITUATIONS[situation](r)
    return r


# ------------------------------------------------------------------------------- random operator
WAITS = (1.0, 3.0, 10.0, 30.0, 90.0, 300.0, 900.0, 2400.0)


def random_operator(r: Rig, rng, plant_hours: float, visible_only: bool = True, impatient: bool = False) -> None:
    """An operator who presses whatever is on the page, whenever: seeded, so a failure replays.

    Between presses he waits a random time - seconds (mid-sequence, mid-settle) to tens of minutes
    (states run to completion).  `impatient`: mostly short waits and double presses.
    `visible_only=False`: any Auto action at any time (a script, a remote panel, a stale click)."""
    from tests.safety_rig import ALL_BUTTONS
    end = r.t + plant_hours * 3600.0
    r.tick(2)
    while r.t < end:
        choices = r.buttons() if visible_only else list(ALL_BUTTONS)
        if choices and rng.random() < 0.9:
            r.press(rng.choice(choices))
            if impatient and rng.random() < 0.3:
                r.tick(rng.randint(0, 2))
                more = r.buttons() if visible_only else list(ALL_BUTTONS)
                if more:
                    r.press(rng.choice(more))
        waits = WAITS[:5] if impatient and rng.random() < 0.8 else WAITS
        r.run(rng.choice(waits) * rng.uniform(0.5, 1.5))
    r.run(1800.0)                                         # let whatever was started run on


# ------------------------------------------------------------------------------- faults
def _first(r: Rig, kind: str) -> str:
    return r.cfg.valves_of_kind(kind)[0].id


def fault_catalogue(r: Rig) -> Dict[str, Callable[[Rig], None]]:
    """Everything that can go wrong on this facility that the simulator can express.  Each entry is
    `inject(rig)`.  Valves stick where they are (open ones open, shut ones shut)."""
    cfg = r.cfg
    set_fault = r.backend.set_fault
    cat: Dict[str, Callable[[Rig], None]] = {
        "primary_pump_stops": lambda r: set_fault("primary_fault", True),
        "chiller_stops": lambda r: set_fault("chiller_fault", True),
        "all_turbos_error": lambda r: set_fault("turbo_error", True),
        "one_turbo_error": lambda r: set_fault(f"turbo_error_{cfg.turbos[0].id}", True),
        "power_cut": lambda r: set_fault("power_cut", True),
        # a hand vent valve left cracked open / a seal letting go: ~atmosphere in a few minutes
        "big_leak": lambda r: (setattr(r.backend.sim, "leak_torr_l_s", 0.5 * r.backend.sim.chamber_volume_l),
                               r.disturbances.append("big_leak")),
        # a viewport or door seal failing outright
        "air_inrush": lambda r: (setattr(r.st, "p_chamber", 760.0), r.disturbances.append("air_inrush")),
        "stuck_bypass": lambda r: set_fault(f"stuck_{_first(r, 'bypass')}", True),
        "stuck_vent": lambda r: set_fault(f"stuck_{_first(r, 'vent')}", True),
        "stuck_turbo_valve": lambda r: set_fault(f"stuck_{_first(r, 'turbo')}", True),
        "stuck_gate": lambda r: set_fault(f"stuck_{_first(r, 'gate')}", True),
        "slow_gate": lambda r: set_fault(f"slow_{_first(r, 'gate')}", True),
        "chamber_gauge_fails": lambda r: set_fault(f"gauge_fault_{cfg.main_gauge.id}", True),
    }
    cat["chamber_gauge_freezes"] = lambda r: set_fault(f"gauge_frozen_{cfg.main_gauge.id}", True)
    if cfg.foreline_gauge is not None:
        cat["foreline_gauge_fails"] = lambda r: set_fault(f"gauge_fault_{cfg.foreline_gauge.id}", True)
        cat["foreline_gauge_freezes"] = lambda r: set_fault(f"gauge_frozen_{cfg.foreline_gauge.id}", True)
    tg = cfg.turbo_gauge(cfg.turbos[0].id)
    if tg is not None:
        cat["turbo_gauge_fails"] = lambda r: set_fault(f"gauge_fault_{tg.id}", True)
        cat["turbo_gauge_freezes"] = lambda r: set_fault(f"gauge_frozen_{tg.id}", True)
    if cfg.has_compressor:
        cat["compressed_air_lost"] = lambda r: set_fault("compressor_low", True)
    if any(t.control_mode == "shimadzu_contacts" for t in cfg.turbos):
        cat["turbo_warning"] = lambda r: set_fault("turbo_warning", True)
    rs = [t for t in cfg.turbos if t.control_mode == "hipace_rs485"]
    if rs:
        cat["rs485_link_lost"] = lambda r: set_fault(f"rs485_lost_{rs[0].id}", True)
    return cat


def clear_faults(r: Rig) -> None:
    """Repair everything (the fault stays in the history of violations it caused)."""
    for name in list(r.st.faults):
        r.backend.set_fault(name, False)
    r.backend.sim.leak_torr_l_s = load_profile_leak(r)
    del r.disturbances[:]


def load_profile_leak(r: Rig) -> float:
    from tests.safety_rig import load_profile
    return load_profile(r.which).simulation.leak_torr_l_s


# ------------------------------------------------------------------------------- known issues
REACT_S = 15.0        # a fault may put pressure on a turning turbo for at most this long before Auto has isolated it


@dataclass
class KnownIssue:
    """Unsafe behaviour the safety tests found in the controller and that is not fixed yet.

    The broad tests (sweep, random operator, fault matrix) do not fail on a violation that matches a
    known issue's signature - otherwise they would be permanently red and useless for catching
    anything NEW.  Each issue has its own test in tests/test_safety_known_issues.py marked
    `xfail(strict=True)`: it documents the behaviour, and the day the controller is fixed it
    XPASSes, which pytest reports as a failure - the cue to delete the issue here.
    Run with SAFETY_STRICT=1 to switch the tolerance off and see every violation.

    2026-11-12: KI-1, KI-2, KI-3, KI-4, KI-5 and KI-9 are fixed and gone from this list, so none of
    their signatures is tolerated any more.  KI-6 breaks no monitor rule.  KI-7 (a dead gauge was
    believed) was fixed on 2026-11-16: a dead or frozen gauge is error 5014.  The list is empty."""
    id: str
    title: str
    match: Callable[[Violation, Rig], bool]


KNOWN_ISSUES: List[KnownIssue] = []


def _within(v: Violation, now: float, rules: Iterable[str]) -> bool:
    return v.rule in rules and v.duration(now) <= REACT_S


# What a fault makes physically unavoidable, whatever the controller does: not a failure.  The pressure
# faults are unavoidable only for as long as it takes to react (REACT_S) - a turbo valve or a gate still
# open onto the pressure after that is the controller's doing.
UNAVOIDABLE: Dict[str, Callable[[Violation, float], bool]] = {
    "chiller_fault": lambda v, now: v.rule in ("T12", "T7"),          # the cooling itself has failed
    "power_cut": lambda v, now: v.rule in ("T12", "T7", "T8", "T9"),  # everything drops; fail-closed valves shut
    # the backing pump stops: the foreline is at atmosphere within seconds (logs), before any valve
    # can move.  What got into the turbo stays there after the valves have shut (T1) and brakes the
    # rotor (T14); with the rotor braked the gas reaches the chamber through the open gate (T2).
    "primary_fault": lambda v, now: v.rule in ("T1", "T14") or _within(v, now, ("T2", "T3", "T8", "T9")),
    # the chamber comes up with the gates open; what is inside the turbo when they shut stays there
    "big_leak": lambda v, now: v.rule in ("T1", "T14") or _within(v, now, ("T2", "T3")),
    # a seal lets go: the rotors are in air before anything can react, and stay in it until they stop
    "air_inrush": lambda v, now: v.rule in ("T1", "T14") or _within(v, now, ("T2", "T3", "T8", "T9")),
}


def triage(r: Rig) -> Dict[str, List[Violation]]:
    """Sort the monitor's violations: {'new': [...], 'KI-n': [...], 'unavoidable': [...]}."""
    out: Dict[str, List[Violation]] = {"new": []}
    strict = os.environ.get("SAFETY_STRICT", "") not in ("", "0")
    for v in r.monitor.violations:
        if any(f in UNAVOIDABLE and UNAVOIDABLE[f](v, r.t) for f in v.faults):
            out.setdefault("unavoidable", []).append(v)
            continue
        ki = None if strict else next((k for k in KNOWN_ISSUES if k.match(v, r)), None)
        out.setdefault(ki.id if ki else "new", []).append(v)
    return out


def assert_no_new_hazard(r: Rig, context: str = "") -> Dict[str, List[Violation]]:
    """Fail on any violation that is neither unavoidable under the injected fault nor a known issue."""
    t = triage(r)
    if t["new"]:
        lines = [str(v) for v in t["new"][:10]]
        if len(t["new"]) > 10:
            lines.append(f"... and {len(t['new']) - 10} more")
        rules = sorted({v.rule for v in t["new"]})
        raise AssertionError(
            f"{r.which}: {len(t['new'])} safety violation(s){' – ' + context if context else ''}\n"
            + "\n".join(lines) + "\nrules broken:\n" + "\n".join(f"   {x}: {RULES[x].text}" for x in rules)
            + "\noperator history (last presses):\n" + r.history())
    return t


# ------------------------------------------------------------------------------- how much to run
# SAFETY_LEVEL=quick   a few minutes: the hazardous situations only, few seeds          (while developing)
#             normal   the default: every situation on both chambers, a modest campaign (every change)
#             full     everything x everything, long campaigns                          (before a release)
LEVEL = os.environ.get("SAFETY_LEVEL", "normal").lower()
if LEVEL not in ("quick", "normal", "full"):
    raise ValueError(f"SAFETY_LEVEL must be quick, normal or full, not {LEVEL!r}")


def at_least(level: str) -> bool:
    order = ("quick", "normal", "full")
    return order.index(LEVEL) >= order.index(level)
