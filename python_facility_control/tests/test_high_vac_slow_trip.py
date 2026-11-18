"""The second, slower chamber-pressure trip in Pumping to High Vac (2026-11-17).

The first trip (2026-11-12, error 5013) is at 1e-1 mBar for 2 s.  In normal work - firing, pulling
vacuum on new lines - the chamber stays below 1e-2 mBar, and at or above that it is "dangerous for
the turbos at full speed and most likely a leak" (test engineers, 2026-11-12).  Between the two
nothing reacted: a leak that SETTLES at a few 1e-2 mBar left the turbos pumping it at full speed.

`thresholds.high_vac_pressure_slow_trip_mbar` = 1e-2 for `timings.pressure_slow_trip_delay_s` = 30 s
-> the same reaction: gates closed, error 5013, Auto stops, the turbos slow down backed and cooled.
30 s so that a gauge misreading while it changes range, or a short burst, passes.  Like the first
trip it only counts once the chamber has been below half of it (the gates open at 1.1-2.4e-1 mBar).

Against the logs: once below 1e-3 mBar the chamber never came back above 1e-3 mBar in any logged
run on either chamber - the trip would not have fired on any logged day.
"""
import pytest

from facility_control.automode import AutoStateMachine
from facility_control.model import ERR_CHAMBER_PRESSURE, FacilityState as S
from facility_control.units import torr_to
from tests.safety_rig import CHAMBERS, Rig, load_profile
from tests.test_safety_known_issues import _hv_prev, _hv_step

MBAR = 0.750062            # Torr per mBar


def _at_high_vac(which, **kw):
    r = Rig(which, **kw)
    r.to_high_vac()
    r.run(120.0)
    assert r.ctl.auto.mem.hv_slow_armed and not r.ctl.snapshot().error.status
    return r


def _hold(r, mbar, seconds):
    """Keep the chamber at `mbar` for `seconds` (a leak the turbos just balance).  Returns the
    snapshot, and after how many seconds an error appeared (None = none)."""
    tripped_after = None
    for i in range(int(seconds)):
        r.st.p_chamber = mbar * MBAR
        s = r.tick()
        if s.error.status and tripped_after is None:
            tripped_after = i + 1
            break
    return r.ctl.snapshot(), tripped_after


@pytest.mark.parametrize("which", CHAMBERS)
def test_profile_settings(which):
    cfg = load_profile(which)
    assert torr_to("mbar", cfg.thresholds.high_vac_pressure_slow_trip_torr) == pytest.approx(1e-2, rel=1e-6)
    assert cfg.timings.pressure_slow_trip_delay_s == 30.0
    assert cfg.thresholds.high_vac_pressure_slow_trip_torr < cfg.thresholds.high_vac_pressure_trip_torr


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("mbar", [2e-2, 5e-2, 9e-2])
def test_a_pressure_that_settles_in_the_gap_closes_the_gates_after_30_s(which, mbar):
    r = _at_high_vac(which)
    s, after = _hold(r, mbar, 60)
    assert after is not None, f"no trip with the chamber at {mbar:g} mBar for a minute"
    assert 29 <= after <= 33, f"tripped after {after} s"
    assert s.error.code == ERR_CHAMBER_PRESSURE and "stayed above 1.0e-02 mBar" in s.error.source, s.error
    r.run(30.0)
    s = r.ctl.snapshot()
    assert s.current in (S.FACILITY_OFF, S.TURBO_SLOWING)
    assert all(r.st.valves[t.gate_valve].position < 0.02 for t in r.turbos), "a gate is still open"
    # the turbos slow down with their backing and their cooling - nothing else changes
    assert s.commands.primary and s.commands.chiller
    assert not any(s.commands.turbo_motor.values())
    r.assert_safe()


@pytest.mark.parametrize("which", CHAMBERS)
def test_a_burst_shorter_than_30_s_is_not_a_trip(which):
    r = _at_high_vac(which)
    for _ in range(3):
        s, after = _hold(r, 5e-2, 20)
        assert after is None, s.error
        r.run_until(lambda s: r.st.p_chamber < 5e-3 * MBAR, 600.0, "pumped down again")
        r.run(5.0)
    s = r.ctl.snapshot()
    assert s.current == S.PUMPING_TO_HIGH_VAC and not s.error.status
    assert all(r.st.valves[t.gate_valve].position > 0.98 for t in r.turbos)


@pytest.mark.parametrize("which", CHAMBERS)
def test_just_below_the_level_never_trips(which):
    r = _at_high_vac(which)
    s, after = _hold(r, 8e-3, 300)
    assert after is None and s.current == S.PUMPING_TO_HIGH_VAC


@pytest.mark.parametrize("which", CHAMBERS)
def test_the_pump_down_itself_never_trips(which):
    """The gates open at 1.1-2.4e-1 mBar and the small chamber needs minutes to get below 1e-2:
    the slow trip does not count until the chamber has been below half of it."""
    r = Rig(which)
    r.press("pump_to_high_vac")
    held = 0
    for _ in range(4 * 3600):
        s = r.tick()
        assert not s.error.status, s.error
        if s.current == S.PUMPING_TO_HIGH_VAC:
            if held < 240:                            # four minutes at 3e-2 mBar on the way down
                if held == 0:
                    r.ctl.auto.mem.hv_slow_armed = False
                    r.ctl.auto.mem.hv_trip_armed = False
                assert not r.ctl.auto.mem.hv_slow_armed
                r.st.p_chamber = 3e-2 * MBAR
                held += 1
            elif r.ctl.auto.mem.hv_slow_armed and r.st.p_chamber < 1e-3 * MBAR:
                break
    else:
        raise AssertionError("high vacuum not reached")
    assert held == 240
    assert all(r.st.valves[t.gate_valve].position > 0.98 for t in r.turbos)


@pytest.mark.parametrize("which", CHAMBERS)
def test_the_fast_trip_still_comes_first_for_a_big_rise(which):
    r = _at_high_vac(which)
    s, after = _hold(r, 5e-1, 20)
    assert after is not None and after <= 5, after
    assert s.error.code == ERR_CHAMBER_PRESSURE and "limit 1.0e-01 mBar" in s.error.source, s.error


@pytest.mark.parametrize("which", CHAMBERS)
def test_null_switches_it_off(which):
    def off(cfg):
        cfg.thresholds.high_vac_pressure_slow_trip_torr = None
    r = Rig(which, tweak=off)
    r.to_high_vac()
    r.run(120.0)
    s, after = _hold(r, 5e-2, 120)
    assert after is None and s.current == S.PUMPING_TO_HIGH_VAC


# ------------------------------------------------------------------------------- the state machine alone
@pytest.mark.parametrize("which", CHAMBERS)
def test_unit_arming_timer_and_reset(which):
    cfg = load_profile(which)
    level = cfg.thresholds.high_vac_pressure_slow_trip_torr
    delay = cfg.timings.pressure_slow_trip_delay_s
    sm = AutoStateMachine(cfg)
    prev = _hv_prev(cfg)
    t = 1000.0

    def step(wrg, dt=1.0):
        nonlocal t, prev
        t += dt
        out = _hv_step(sm, cfg, wrg, prev, t)
        prev = out.commands
        return out

    # not armed: above the level for a long time, nothing
    for _ in range(int(3 * delay)):
        assert step(3.0 * level).trip is None
    assert not sm.mem.hv_slow_armed
    # 0.6 x the level does not arm it, 0.4 x does
    step(0.6 * level)
    assert not sm.mem.hv_slow_armed
    step(0.4 * level)
    assert sm.mem.hv_slow_armed
    # above for less than the delay, then a dip below: the clock starts again
    for _ in range(int(delay) - 2):
        assert step(2.0 * level).trip is None
    assert step(0.9 * level).trip is None and sm.mem.hv_slow_since is None
    for _ in range(int(delay) - 1):
        assert step(2.0 * level).trip is None
    out = step(2.0 * level)
    out = out if out.trip else step(2.0 * level)
    assert out.trip is not None and out.trip[0] == ERR_CHAMBER_PRESSURE and "stayed above" in out.trip[1]
    assert not any(out.commands.valves[tc.gate_valve] for tc in cfg.turbos if tc.in_use)
    # an unreadable chamber gauge is not a trip (that is error 5014's job) and restarts the clock
    sm2 = AutoStateMachine(cfg)
    prev = _hv_prev(cfg)
    sm2.mem.hv_slow_armed = True
    for i in range(int(3 * delay)):
        t += 1.0
        wrg = float("nan") if i % 10 == 9 else 2.0 * level
        assert _hv_step(sm2, cfg, wrg, prev, t).trip is None
