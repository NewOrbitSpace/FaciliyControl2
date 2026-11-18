"""Fault injection: every fault the simulator can express, in every situation that matters.

For each cell the fault is injected, the facility is left alone for half an hour of plant time,
then everything is repaired, the error cleared and the operator pumps down again.  The safety
monitor watches throughout.  A cell fails if a rule is broken that is neither

  * physically unavoidable under that fault (the chiller has failed -> the turbos have no cooling), nor
  * a known issue (tests/safety_scenarios.KNOWN_ISSUES - each has its own strict-xfail test in
    test_safety_known_issues.py; SAFETY_STRICT=1 switches the tolerance off).

Separately: faults the facility has a sensor for must be NOTICED - an error within a minute.
"""
import pytest

from facility_control.model import FacilityState as S
from tests.safety_rig import CHAMBERS, Rig
from tests.safety_scenarios import (SPINNING_SITUATIONS, assert_no_new_hazard, at_least, clear_faults, fault_catalogue,
                                    make)

AFTER_FAULT_S = 1800.0 if at_least("full") else 1200.0
AFTER_REPAIR_S = 3600.0 if at_least("full") else 2400.0

SITUATIONS_FOR_FAULTS = {
    "quick": ["high_vac", "venting_coasting_atm"],
    "normal": ["high_vac", "high_vac_running_up", "slowing_fast", "rough_from_high_vac", "venting_coasting_atm",
               "overnight_spinning", "rough_mid"],
    "full": SPINNING_SITUATIONS + ["rough_mid", "rough_hold", "venting_running_atm", "off_cold", "rough_start",
                                   "overnight_cold", "vented_hold", "off_after_vent"],
}
VC100_NORMAL = ["high_vac", "slowing_fast", "venting_coasting_atm", "rough_from_high_vac"]


def _cells():
    from tests.safety_scenarios import LEVEL
    for which in CHAMBERS:
        faults = list(fault_catalogue(Rig(which)))
        sits = SITUATIONS_FOR_FAULTS[LEVEL]
        if which == "vc100" and LEVEL == "normal":
            sits = VC100_NORMAL
        if which == "vc100" and LEVEL == "quick":
            sits = ["high_vac"]
        for sit in sits:
            for fault in faults:
                yield which, sit, fault


@pytest.mark.parametrize("which,situation,fault", list(_cells()))
def test_fault_in_situation(which, situation, fault):
    r = make(which, situation, "long_coast")
    fault_catalogue(r)[fault](r)
    r.run(AFTER_FAULT_S)
    assert_no_new_hazard(r, f"{fault} injected in {situation}")
    # ---- repaired; the operator clears the error and carries on
    clear_faults(r)
    r.ctl.request_turbo_error_ack()                  # 'Reset Turbos'
    r.tick(5)
    r.ctl.request_clear_error()
    r.tick(5)
    for button in ("pump_to_high_vac", "pump_to_rough"):
        if button in r.buttons():
            r.press(button)
            break
    r.run(AFTER_REPAIR_S)
    assert_no_new_hazard(r, f"after repairing {fault} (injected in {situation})")


# ------------------------------------------------------------------------- the fault is noticed
def _errors(r):
    return [line for line in r.ctl.event_log if line.startswith("ERROR") or line.startswith("!!")]


NOTICED = [  # (fault, situation, chambers) - the facility has a signal for these
    ("all_turbos_error", "high_vac", CHAMBERS),
    ("one_turbo_error", "high_vac", CHAMBERS),
    ("one_turbo_error", "slowing_fast", CHAMBERS),
    ("chiller_stops", "high_vac", CHAMBERS),
    ("chiller_stops", "rough_for_high_vac", CHAMBERS),
    ("power_cut", "high_vac", CHAMBERS),
    ("power_cut", "rough_mid", CHAMBERS),
    ("primary_pump_stops", "high_vac", ("vc100",)),          # VC100 has a run read-back (error 5000)
    ("primary_pump_stops", "rough_mid", ("vc100",)),
    ("compressed_air_lost", "high_vac", ("vc100",)),
    ("compressed_air_lost", "rough_mid", ("vc100",)),
    ("turbo_warning", "high_vac", ("vc100",)),
    ("rs485_link_lost", "high_vac", ("vc100",)),
    # a dead gauge is noticed after 3 s (2026-11-16) ...
    ("chamber_gauge_fails", "high_vac", CHAMBERS),
    ("chamber_gauge_fails", "rough_mid", CHAMBERS),
    ("foreline_gauge_fails", "high_vac", CHAMBERS),
    ("turbo_gauge_fails", "high_vac", CHAMBERS),
]

FROZEN = [  # ... a frozen one after 100 readings: 100 s at this rig's 1 s loop, about 15 s on the facility
    ("chamber_gauge_freezes", "high_vac"), ("foreline_gauge_freezes", "high_vac"), ("turbo_gauge_freezes", "rough_mid"),
]


@pytest.mark.parametrize("fault,situation,which", [(f, s, w) for f, s, ws in NOTICED for w in ws])
def test_fault_is_noticed_within_a_minute(fault, situation, which):
    r = make(which, situation, "long_coast")
    before = len(_errors(r))
    fault_catalogue(r)[fault](r)
    r.run(60.0)
    new = _errors(r)[before:]
    assert new, f"{fault} in {situation}: nothing in the log after 60 s"


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("fault,situation", FROZEN)
def test_a_frozen_gauge_is_noticed_within_three_minutes(fault, situation, which):
    r = make(which, situation, "long_coast")
    before = len(_errors(r))
    fault_catalogue(r)[fault](r)
    r.run(180.0)
    new = _errors(r)[before:]
    assert any("5014" in line for line in new), f"{fault} in {situation}: {new}"


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("fault", ["all_turbos_error", "power_cut", "chiller_stops", "chamber_gauge_fails",
                                   "foreline_gauge_fails", "turbo_gauge_fails"])
def test_an_error_always_ends_with_everything_shut(which, fault):
    """Whatever the error, within a minute: vent, bypass and gates shut, no turbo driven."""
    r = make(which, "high_vac", "long_coast")
    fault_catalogue(r)[fault](r)
    r.run(60.0)
    s = r.ctl.snapshot()
    assert s.current == S.FACILITY_OFF and _errors(r)
    st = r.st
    for kind in ("vent", "bypass", "gate"):
        for v in r.cfg.valves_of_kind(kind):
            assert st.valves[v.id].position < 0.02, f"{v.label} still open a minute after the error"
    assert not any(t.motor for t in st.turbos.values())


@pytest.mark.parametrize("which", CHAMBERS)
def test_a_stuck_open_gate_is_never_vented_onto(which):
    """Gate stuck open with the turbo turning: whatever the operator presses next, the vent valve
    must not open onto it."""
    r = make(which, "high_vac", "long_coast")
    gate = r.cfg.valves_of_kind("gate")[0].id
    r.backend.set_fault(f"stuck_{gate}", True)
    for button in ("vent", "vent_and_shutdown", "shutdown"):
        if button in r.buttons():
            r.press(button)
            r.run(900.0)
        r.ctl.request_clear_error()
        r.tick(3)
        for b2 in ("vent", "pump_to_rough"):
            if b2 in r.buttons():
                r.press(b2)
                r.run(900.0)
    assert not r.monitor.found("T4", "K1"), r.monitor.report()
    assert_no_new_hazard(r, "gate stuck open")
