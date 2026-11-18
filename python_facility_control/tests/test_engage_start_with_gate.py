"""Engage Turbo on the VC100: each turbo starts as soon as its OWN gate is open (2026-11-13).

Asked 2026-11-13: "it opens each gate valve one by one with a few seconds interval then turns on the
turbos.  Might be quicker to turn on the turbos then open the gates."

Starting the turbos FIRST was measured on the plant model fitted to the facility's logs and is not
quicker: the chamber sits at 1.7e-1 mBar while six turbos run up to standby behind closed gates, and
is at 1e-3 mBar almost three minutes LATER.  It also opens the gates onto rotors at ~70 % with the
chamber still at 1e-1 mBar - the logs show what that does (28 Oct, Admin: a gate opened onto a turbo
at 100 % with 2.4e-1 mBar in the chamber put 11 mBar on its exhaust and cost it 8 % of its speed).

What does save time without changing what any turbo sees: do not make the first turbos wait for the
last gate.  Each turbo still starts from rest with its gate open - exactly as before.
"""
import pytest

from facility_control.model import FacilityState as S
from facility_control.units import torr_to
from tests.safety_rig import Rig, load_profile
from tests.safety_scenarios import assert_no_new_hazard


def _off(cfg):
    cfg.auto_mode.start_turbo_with_its_gate = False


def _pump_down(tweak=None):
    """Pump to High Vac from atmosphere.  Returns the rig and, per turbo, what the plant looked like
    when its motor was first commanded, plus the times (from the first gate command) at which the
    chamber passed 1e-3 mBar and every turbo was at full speed."""
    r = Rig("vc100", tweak=tweak)
    r.press("pump_to_high_vac")
    started, t0, t_vac, t_full = {}, None, None, None
    for _ in range(4000):
        # the plant as the controller saw it when it decided (a tick ends after the settle waits of
        # whatever it commanded, by which time a started rotor is already turning)
        before = {t.id: (r.speed(t.id), r.st.valves[t.gate_valve].position, r.st.valves[t.turbo_valve].position)
                  for t in r.turbos}
        s = r.tick()
        assert not s.error.status, s.error
        if t0 is None and any(s.commands.valves[t.gate_valve] for t in r.turbos):
            t0 = r.t
        for t in r.turbos:
            if s.commands.turbo_motor[t.id] and t.id not in started:
                started[t.id] = dict(t=r.t - t0, state=s.current, gate_cmd=s.commands.valves[t.gate_valve],
                                     gate_pos=before[t.id][1], tv_pos=before[t.id][2], speed=before[t.id][0],
                                     gates_open=sum(1 for x in r.turbos if s.commands.valves[x.gate_valve]),
                                     chamber_mbar=torr_to("mbar", r.st.p_chamber))
            assert not s.commands.turbo_standby[t.id], f"{t.id} in standby"
            if s.commands.turbo_motor[t.id] and s.current == S.ENGAGE_TURBO:
                assert s.commands.valves[t.gate_valve], f"{t.id} driven behind a closed gate"
        if t0 is not None and t_vac is None and torr_to("mbar", r.st.p_chamber) < 1e-3:
            t_vac = r.t - t0
        if t0 is not None and t_full is None and all(r.speed(t.id) >= 99.0 for t in r.turbos):
            t_full = r.t - t0
        if t_vac is not None and t_full is not None:
            break
    else:
        raise AssertionError("pump-down not finished")
    return r, started, t_vac, t_full


def test_the_profiles():
    assert load_profile("vc100").auto_mode.start_turbo_with_its_gate is True
    assert load_profile("small").auto_mode.start_turbo_with_its_gate is False      # one gate: nothing to gain


def test_each_turbo_starts_from_rest_with_its_own_gate_open():
    r, started, _, _ = _pump_down()
    assert set(started) == {t.id for t in r.turbos}
    order = [tid for tid, _ in sorted(started.items(), key=lambda kv: kv[1]["t"])]
    assert order == [t.id for t in r.turbos], f"turbos started in the order {order}"
    for tid, x in started.items():
        assert x["gate_cmd"] and x["gate_pos"] > 0.98, f"{tid} started with its gate {x['gate_pos']:.0%} open"
        assert x["tv_pos"] > 0.98, f"{tid} started with its turbo valve {x['tv_pos']:.0%} open"
        assert x["speed"] < 1.0, f"{tid} was already turning ({x['speed']:.0f} %) when it was started"
        assert x["chamber_mbar"] < 0.3, f"{tid} started with the chamber at {x['chamber_mbar']:.2f} mBar"
    in_engage = [tid for tid, x in started.items() if x["state"] == S.ENGAGE_TURBO and x["gates_open"] < len(r.turbos)]
    assert len(in_engage) >= 4, f"only {in_engage} were started before the last gate"
    r.run(600.0)
    r.assert_safe()


def test_with_the_switch_off_every_turbo_waits_for_the_last_gate():
    r, started, _, _ = _pump_down(_off)
    for tid, x in started.items():
        assert x["gates_open"] == len(r.turbos), f"{tid} started with {x['gates_open']} gates open"
    assert max(x["t"] for x in started.values()) - min(x["t"] for x in started.values()) < 2.0
    r.assert_safe()


def test_it_is_quicker_and_no_gate_opens_onto_a_turning_turbo():
    _, _, vac_new, full_new = _pump_down()
    _, _, vac_old, full_old = _pump_down(_off)
    assert vac_new < vac_old - 15.0, f"1e-3 mBar after {vac_new:.0f} s with the switch, {vac_old:.0f} s without"
    assert full_new < full_old - 10.0, f"all at full speed after {full_new:.0f} s with the switch, {full_old:.0f} s without"


def test_turbos_first_at_every_pressure_is_slower_to_vacuum():
    """The alternative that was asked about, on the same plant: start the turbos, then open the gates."""
    def turbos_first(cfg):
        cfg.auto_mode.start_turbo_with_its_gate = False
        cfg.thresholds.turbo_start_before_gates_below_torr = 10.0
    r = Rig("vc100", tweak=turbos_first)
    r.press("pump_to_high_vac")
    t0 = t_vac = None
    onto = []                                   # (speed, chamber mBar) of each rotor when its gate came off its seat
    seen = set()
    for _ in range(4000):
        s = r.tick()
        if t0 is None and s.current == S.ENGAGE_TURBO and any(s.commands.turbo_motor.values()):
            t0 = r.t
        for t in r.turbos:
            if t.id not in seen and r.st.valves[t.gate_valve].position > 0.02:
                seen.add(t.id)
                onto.append((r.speed(t.id), torr_to("mbar", r.st.p_chamber)))
        if t0 is not None and torr_to("mbar", r.st.p_chamber) < 1e-3:
            t_vac = r.t - t0
            break
    _, _, vac_gates_first, _ = _pump_down(_off)
    assert t_vac > vac_gates_first + 60.0, f"turbos first: {t_vac:.0f} s to 1e-3 mBar, gates first: {vac_gates_first:.0f} s"
    assert max(speed for speed, _ in onto) > 50.0 and onto[0][1] > 0.05, onto   # a rotor above 50 % met 1e-1 mBar


@pytest.mark.parametrize("gate", ["gate2", "gate5"])
def test_a_gate_that_does_not_open_leaves_its_turbo_stopped(gate):
    r = Rig("vc100")
    r.backend.set_fault(f"stuck_{gate}", True)
    r.press("pump_to_high_vac")
    turbo = next(t for t in r.turbos if t.gate_valve == gate)
    for _ in range(3000):
        s = r.tick()
        assert not s.commands.turbo_motor[turbo.id], f"{turbo.id} started behind {gate}, which never opened"
        if s.error.status:
            break
    else:
        raise AssertionError("the stuck gate was never reported")
    r.run(600.0)
    assert r.ctl.snapshot().current == S.FACILITY_OFF
    assert r.speed(turbo.id) < 1.0
    assert_no_new_hazard(r)


def test_below_the_turbos_first_limit_nothing_changes():
    """Chamber already below 1e-2 mBar: the 2026-10-28 order (turbos to standby, then gates) as before."""
    r = Rig("vc100")
    r.to_high_vac()
    r.run(300.0)
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.FACILITY_OFF, 3 * 3600.0, "Facility Off")
    r.run_until(lambda s: not r.any_turning(), 3 * 3600.0, "turbos stopped")
    assert r.st.p_chamber < r.cfg.thresholds.turbo_start_before_gates_below_torr
    r.press("pump_to_high_vac")
    seen6 = False
    for _ in range(3000):
        s = r.tick()
        assert not s.error.status, s.error
        if s.current == S.ENGAGE_TURBO and s.substate == 6:
            seen6 = True
            assert not any(s.commands.valves[t.gate_valve] for t in r.turbos)
        if s.current == S.PUMPING_TO_HIGH_VAC:
            break
    assert seen6, "the turbos-first branch was not taken"
    r.assert_safe()
