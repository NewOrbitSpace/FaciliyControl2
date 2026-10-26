"""VC100 changes of 2026-10-26.

1. VC100 has no hand vent valve: Pump to Rough / High Vac / Overnight must not ask
   "Is the manual vent valve closed?" (the small chamber still asks).
2. The six gate valves share one compressed-air line: they are opened one at a time, at least
   `gate_open_stagger_s` (5 s) apart, in every mode; Auto starts the turbos only once every gate is
   open; closing is never delayed.
"""
import pytest

from conftest import Harness
from facility_control.config import CONFIG_DIR, load_config
from facility_control.controller import MANUAL_VENT_QUESTION
from facility_control.model import FacilityState as S, Mode

VC100 = str(CONFIG_DIR / "facility_vc100.yaml")
GATES = [f"gate{i}" for i in range(1, 7)]


@pytest.fixture
def vcfg():
    c = load_config(VC100)
    c.simulation.time_scale = 200.0
    return c


def test_profile_settings(vcfg):
    assert vcfg.auto_mode.ask_manual_vent_valve is False
    assert vcfg.timings.gate_open_stagger_s == 5.0
    small = load_config()
    assert small.auto_mode.ask_manual_vent_valve is True        # small chamber unchanged
    assert small.timings.gate_open_stagger_s == 0.0


@pytest.mark.parametrize("button", ["pump_to_rough", "pump_to_high_vac", "overnight_pump"])
def test_vc100_pump_buttons_do_not_ask_about_a_manual_vent_valve(vcfg, button):
    h = Harness(vcfg)
    h.step(2)
    h.ctl.request_auto_button(button)
    h.step(3)
    assert not any(m == MANUAL_VENT_QUESTION for _k, m in h.dialogs.log)
    assert h.ctl.target != S.FACILITY_OFF or button == "overnight_pump"   # the button was acted on


def test_small_chamber_still_asks(cfg):
    h = Harness(cfg)
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    h.step(2)
    assert any(m == MANUAL_VENT_QUESTION for _k, m in h.dialogs.log)


def _gate_open_times(h, until, max_iter=40000):
    """Step until `until(snapshot)`; return {gate: time its OPEN command first appeared} and the
    time each turbo motor was first commanded on."""
    opened, motors = {}, {}
    for _ in range(max_iter):
        s = h.step()
        t = h.ctl.clock()
        for g in GATES:
            if s.commands.valves.get(g) and g not in opened:
                opened[g] = t
        for tid, on in s.commands.turbo_motor.items():
            if on and tid not in motors:
                motors[tid] = t
        if until(s):
            return s, opened, motors
    raise AssertionError("condition not reached")


@pytest.mark.parametrize("blocking", [True, False])
def test_auto_engage_opens_gates_one_by_one_then_starts_turbos(vcfg, blocking):
    vcfg.timings.blocking_waits = blocking
    h = Harness(vcfg)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, opened, motors = _gate_open_times(h, lambda s: s.current == S.PUMPING_TO_HIGH_VAC)
    assert list(opened) == GATES, "gates open in turbo order, one at a time"
    times = [opened[g] for g in GATES]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 5.0 - 1e-6, gaps
    assert all(s.commands.valves[g] for g in GATES)
    # every turbo is started only after the last gate has been commanded open
    assert motors and min(motors.values()) >= max(times)
    assert any("Opening Gate 6 Valve (6/6)" in line for line in s.event_log)


def test_closing_is_not_staggered(vcfg):
    h = Harness(vcfg)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 40000)
    h.ctl.request_auto_button("shutdown")
    h.step()
    s = h.step()
    assert not any(s.commands.valves[g] for g in GATES)          # all six close together


def test_manual_mode_refuses_a_second_gate_within_5_s(vcfg):
    # with the profile's 9 s gate settle hold a second click can't come within 5 s anyway; shorten the
    # hold so the refusal itself is exercised
    vcfg.timings.blocking_waits = False
    vcfg.timings.gate_settle_ms = 500
    h = Harness(vcfg, mode=Mode.ADMIN)
    h.step(2)
    h.ctl.request_user_cmd("valve:gate1")
    s = h.step()
    assert s.commands.valves["gate1"]
    t1 = h.ctl.clock()
    h.ctl.request_user_cmd("valve:gate2")
    s = h.step()
    assert h.ctl.clock() - t1 < 5.0
    assert not s.commands.valves["gate2"]
    assert any(k == "info" and "compressed-air line" in m for k, m in h.dialogs.log)
    # after the stagger time the same click goes through
    while h.ctl.clock() - t1 < 5.0:
        h.tick()
    h.ctl.request_user_cmd("valve:gate2")
    s = h.step()
    assert s.commands.valves["gate2"]
    # closing is immediate, even right after an opening
    h.ctl.request_user_cmd("valve:gate1")
    s = h.step()
    assert not s.commands.valves["gate1"]


def test_guard_holds_two_gates_opened_in_one_decision(vcfg):
    """Safety net in the controller: whatever a mode asks for, at most one gate opens per 5 s."""
    h = Harness(vcfg, mode=Mode.ADMIN)
    h.step(2)
    prev = h.ctl.cmds.copy()
    new = prev.copy()
    for g in GATES:
        new.valves[g] = True
    out = h.ctl._stagger_gate_openings(prev, new, h.ctl.clock())
    assert [g for g in GATES if out.valves[g]] == ["gate1"]
