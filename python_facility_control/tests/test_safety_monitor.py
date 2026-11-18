"""The safety monitor itself: every rule fires when the plant is put in the state it forbids, the
monitor is silent through a normal cycle, and a run is exactly repeatable.

Without this file the other safety tests could pass because a rule can never fire.
"""
import pytest

from facility_control.model import Mode
from tests.safety_rig import CHAMBERS, RULES, Rig, S


def _plant(which):
    """A rig whose controller is never iterated: the test moves the plant by hand."""
    r = Rig(which, mode=Mode.ADMIN)
    st = r.st
    st.p_chamber, st.p_foreline = 1e-5, 1e-3
    for t in st.turbos.values():
        t.p_body_torr = 1e-5
    r.backend.sim.turbo_spindown_s = 1e5              # rotors keep whatever speed the test gives them
    return r, st, r.turbos[0]


def _open(st, *valves):
    for v in valves:
        st.valves[v].cmd = True


def _running(st, primary=True, chiller=True):
    st.primary_cmd = st.primary_run = st.primary_power = primary
    st.primary_on = 1.0 if primary else 0.0
    st.chiller_cmd = chiller
    st.chiller_on = 1.0 if chiller else 0.0


def _bypass(r):
    return r.cfg.valves_of_kind("bypass")[0].id


def _vent(r):
    return r.cfg.valves_of_kind("vent")[0].id


def case_T1(r, st, t):
    _running(st); st.turbos[t.id].speed_pct = 80; st.turbos[t.id].p_body_torr = 50.0


def case_T2(r, st, t):
    _running(st); st.turbos[t.id].speed_pct = 80; st.p_chamber = 100.0; _open(st, t.gate_valve)


def case_T3(r, st, t):
    _running(st, primary=False); st.turbos[t.id].speed_pct = 80; st.p_foreline = 100.0; _open(st, t.turbo_valve)


def case_T4(r, st, t):
    _running(st); st.turbos[t.id].speed_pct = 80; _open(st, t.gate_valve, _vent(r))


def case_T5(r, st, t):
    _running(st); _open(st, _bypass(r), _vent(r))


def case_T6(r, st, t):
    _running(st); st.turbos[t.id].speed_pct = 80; _open(st, t.gate_valve, _bypass(r))


def case_T7(r, st, t):
    _running(st, chiller=False); st.turbos[t.id].motor = True; _open(st, t.turbo_valve)


def case_T8(r, st, t):
    _running(st); st.turbos[t.id].motor = True                      # turbo valve shut


def case_T9(r, st, t):
    _running(st, primary=False); st.turbos[t.id].speed_pct = 80; _open(st, t.turbo_valve)


def case_T10(r, st, t):
    _running(st); st.turbos[t.id].motor = True; st.turbos[t.id].p_body_torr = 50.0; _open(st, t.turbo_valve)
    st.p_foreline = 50.0; st.primary_on = 0.0; st.primary_cmd = st.primary_run = False


def case_T11(r, st, t):
    _running(st, primary=False); st.p_chamber = 1e-5; st.p_foreline = 5.0; _open(st, _bypass(r))


def case_T12(r, st, t):
    _running(st, chiller=False); st.turbos[t.id].speed_pct = 80


def case_T13(r, st, t):
    st.primary_run, st.primary_power = True, False


def case_T14(r, st, t):
    _running(st); st.turbos[t.id].speed_pct = 90; st.turbos[t.id].p_body_torr = 700.0     # air on a rotor at speed
    r.backend.sim.turbo_spindown_s = 3600.0


def case_K1(r, st, t):
    r.ctl.cmds.valves[_vent(r)] = True; r.ctl.cmds.valves[t.gate_valve] = True


def case_K2(r, st, t):
    r.ctl.cmds.valves[_bypass(r)] = True; r.ctl.cmds.valves[t.gate_valve] = True


def case_K3(r, st, t):
    r.ctl.cmds.turbo_motor[t.id] = True; r.ctl.cmds.primary = True; r.ctl.cmds.chiller = True


def case_K4(r, st, t):
    _running(st, primary=False); st.turbos[t.id].speed_pct = 80; r.ctl.cmds.valves[t.turbo_valve] = True


CASES = {name[5:]: fn for name, fn in globals().items() if name.startswith("case_")}


def test_every_rule_has_a_case_that_breaks_it():
    assert set(CASES) == set(RULES), f"rules without a self-test: {sorted(set(RULES) - set(CASES))}"


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("rule", sorted(RULES, key=lambda k: (k[0], int(k[1:]))))
def test_rule_fires(which, rule):
    r, st, t = _plant(which)
    if rule == "T13" and not r.cfg.primary.two_stage:
        pytest.skip("single-relay primary pump")
    r._advance(5.0)
    assert not r.monitor.violations, "the quiet plant is not quiet:\n" + r.monitor.report()
    CASES[rule](r, st, t)
    r._advance(RULES[rule].grace_s + 5.0)
    assert r.monitor.found(rule), f"{rule} ({RULES[rule].text}) did not fire"


@pytest.mark.parametrize("which", CHAMBERS)
def test_grace_times_are_respected(which):
    """A condition shorter than a rule's grace time is valve travel or a pump starting, not a fault."""
    r, st, t = _plant(which)
    case_T7(r, st, t)
    r._advance(RULES["T7"].grace_s - 2.0)
    assert not r.monitor.found("T7")
    _running(st)
    r._advance(30.0)
    assert not r.monitor.found("T7")


@pytest.mark.parametrize("which", CHAMBERS)
def test_a_normal_cycle_is_silent(which):
    """Pump down, run, shut down, pump down again, vent: nothing for the monitor to say."""
    r = Rig(which)
    r.to_high_vac()
    r.run(300)
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.FACILITY_OFF, what="Facility Off")
    r.press("pump_to_high_vac")
    r.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, what="Pumping to High Vac")
    r.run(120)
    r.press("vent_and_shutdown")
    r.run_until(lambda s: s.current == S.FACILITY_OFF, 6 * 3600, what="Facility Off")
    assert r.monitor.samples > 5000
    r.assert_safe()
    assert not [line for line in r.ctl.event_log if line.startswith("ERROR")]


@pytest.mark.parametrize("which", CHAMBERS)
def test_runs_are_repeatable(which):
    """Same seed, same plant, same presses -> the same run to the last digit (so a failure replays)."""
    def run():
        r = Rig(which, seed=7)
        r.to_high_vac(full_speed=False)
        r.press("vent")
        r.run(600)
        st = r.st
        return (r.t, r.ctl.iteration, st.p_chamber, st.p_foreline,
                tuple(round(t.speed_pct, 9) for t in st.turbos.values()), tuple(r.ctl.event_log[-5:]) and len(r.ctl.event_log))
    assert run() == run()
