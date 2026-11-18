"""The three things that really went wrong on the VC100, as found in its own CSV logs
(docs/PLANT_FROM_LOGS.md) - replayed on the plant model measured from those logs.

  2026-10-28 15:06  Admin -> Auto with Turbo 2 and Turbo 5 coasting at 98 %, isolated, primary off,
                    foreline at atmosphere.  Auto opened Turbo Valve 2 onto that foreline: both rotors
                    went from 98 % to 18 % in under 5 s.
  2026-11-05 09:46  Vent and Shutdown at full speed; 11 min later, turbos still at 78 %, Pump to High
                    Vac.  The bypass opened onto the vented chamber with Turbo Valves 2 and 3 open:
                    four rotors went from 78 % to 5 % in under 10 s.  No error was raised.
  2026-11-09        (three times)  Pump to High Vac with the turbos still coasting at 50-70 %.  The
                    gates started to open one by one, the coasting turbos pulled the chamber below
                    1e-2 mBar within seconds, and Auto closed the gates again and put every turbo in
                    standby.  The operators finished the pump-down by hand in Admin.

Each test fails on the code that was running that day and passes now.
"""
import pytest

from facility_control.model import Mode
from tests.safety_rig import Rig, S

HIPACE = ("turbo2", "turbo3", "turbo5", "turbo6")


def _full_speed_then_vent(minutes: float) -> Rig:
    r = Rig("vc100")                                   # the measured plant, as it is in the profile
    r.to_high_vac()
    r.run(300.0)
    r.press("vent_and_shutdown")
    r.run(60.0 * minutes)
    return r


def test_2026_11_05_pump_to_high_vac_while_venting_with_the_turbos_at_78_percent():
    r = _full_speed_then_vent(11.0)
    assert r.ctl.snapshot().current == S.VENTING and r.st.p_chamber > 700.0
    speeds = [r.speed(t) for t in HIPACE]
    assert all(70.0 < v < 85.0 for v in speeds), f"the logged speed after an 11 min vent was 71-78 %, the model gives {speeds}"
    r.press("pump_to_high_vac")
    lowest = min(speeds)
    for _ in range(3 * 3600):
        s = r.tick()
        assert not s.error.status, s.error
        if s.current == S.PUMPING_TO_HIGH_VAC and all(r.speed(t) > 95.0 for t in HIPACE):
            break
        if s.current in (S.PUMPING_TO_ROUGH, S.VENTING):
            lowest = min(lowest, *(r.speed(t) for t in HIPACE))
    else:
        raise AssertionError("high vacuum not reached again")
    # the roughing takes ~6 min: an isolated HiPace loses ~2.3 %/min - not 70 % in 10 s
    assert lowest > 50.0, f"a rotor fell to {lowest:.0f} % during the roughing"
    r.assert_safe()
    assert not r.monitor.found("T14")


def test_2026_10_28_handover_with_coasting_turbos_on_a_foreline_at_atmosphere():
    r = Rig("vc100", mode=Mode.ADMIN)
    r.monitor.enabled = False
    r.tick(2)
    st = r.st
    st.p_chamber = 1e-4
    st.p_foreline = 760.0                              # primary off: the foreline is at atmosphere
    for tid, t in st.turbos.items():
        t.p_body_torr = 1e-2
        t.speed_pct = 98.0 if tid in ("turbo2", "turbo5") else 0.0
    r.tick(3)
    r.monitor.enabled = True
    r.ctl.request_change_to_auto()
    tv2 = st.valves["turbo2_valve"]
    for _ in range(600):
        r.tick()
        if tv2.position > 0.02:
            assert st.p_foreline < r.monitor.p_rough, \
                f"Turbo Valve 2 came off its seat with the foreline at {st.p_foreline:.0f} Torr"
    assert r.ctl.mode == Mode.AUTO
    assert r.speed("turbo2") > 70.0 and r.speed("turbo5") > 70.0, "the rotors were braked"
    assert tv2.position > 0.98, "the coasting turbos got their backing back once the foreline was pumped"
    r.assert_safe()
    assert not r.monitor.found("T14")


@pytest.mark.parametrize("vent_minutes", [8.5, 11.0, 15.0])
def test_2026_11_09_gates_do_not_close_again_with_the_turbos_still_coasting(vent_minutes):
    r = _full_speed_then_vent(vent_minutes)
    r.press("pump_to_rough")                           # (the operators roughed by hand in Admin)
    r.run_until(lambda s: s.current == S.PUMPING_TO_ROUGH and s.substate == 3
                and r.st.p_chamber < r.cfg.thresholds.turbo_on_threshold_torr, 3600.0, "rough vacuum")
    assert all(r.speed(t) > 40.0 for t in HIPACE), "the test needs the turbos still coasting"
    r.press("pump_to_high_vac")
    gates = sorted({t.gate_valve for t in r.turbos})
    opened = set()
    below = False
    for _ in range(3600):
        s = r.tick()
        assert not s.error.status, s.error
        now_open = {g for g in gates if s.commands.valves[g]}
        if s.current in (S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC):
            assert opened <= now_open, f"gate(s) {sorted(opened - now_open)} closed again in {s.current.label}/{s.substate}"
            opened |= now_open
            if 0 < len(now_open) < len(gates) and r.st.p_chamber < r.cfg.thresholds.turbo_start_before_gates_below_torr:
                below = True                           # what triggered it: below the limit part-way through
        if s.current == S.PUMPING_TO_HIGH_VAC and len(now_open) == len(gates):
            break
    else:
        raise AssertionError("high vacuum not reached")
    assert below, "the chamber never fell below the turbos-first limit while the gates were opening"
    assert not any(s.commands.turbo_standby.values())
    r.assert_safe()
