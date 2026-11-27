"""The unsafe behaviour the safety tests found on 2026-11-11 - one test (or a few) per issue.

KI-1 to KI-5, KI-9, KI-10 and KI-11 were fixed on 2026-11-12, KI-7 on 2026-11-16: their tests are
ordinary tests now and must pass.  They stay here, under their numbers, as the regression tests for
those fixes - each docstring says what used to happen.

Still open, by decision, and marked `xfail(strict=True)` (it fails today as expected; if it ever
passes pytest reports that as a failure, so the mark gets removed):

  KI-6   small chamber: a stopped primary pump is not noticed while NO turbo is turning.  The pump
         has no feedback unless the frequency reader is switched on, and it is not always fitted.

`pytest -rx` lists it.  docs/SAFETY_TESTS.md has the whole story.
"""
import pytest

from facility_control.automode import AutoInput, AutoStateMachine
from facility_control.model import (ERR_BACKING_LOST, ERR_CHAMBER_PRESSURE, ERR_GAUGE_FAULT, Commands,
                                    FacilityState as S, Inputs, Mode,
                                    TurboInputs)
from facility_control.units import torr_to
from tests.safety_rig import CHAMBERS, Rig
from tests.safety_scenarios import REACT_S, assert_no_new_hazard, fault_catalogue, make


def _errors(r):
    return [line for line in r.ctl.event_log if line.startswith("ERROR") or line.startswith("!!")]


# ---------------------------------------------------------------------------------------- KI-1
@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("fault", ["one_turbo_error", "all_turbos_error"])
def test_ki1_an_error_shutdown_keeps_the_cooling_while_the_turbos_spin(which, fault):
    """Was: any error in high vacuum -> Facility Off -> everything off, including the chiller, with
    the rotors still at ~100 % (the primary and the turbo valves were kept, the cooling was not).
    Now: the chiller stays on while any turbo is above the 'still spinning' speed, then goes off."""
    r = make(which, "high_vac")
    fault_catalogue(r)[fault](r)
    r.run(600.0)
    assert r.ctl.snapshot().current == S.FACILITY_OFF and _errors(r)
    assert r.any_turning() and r.st.chiller_on > 0.5, "the chiller went off with the turbos still turning"
    assert not r.monitor.found("T12"), r.monitor.report()
    assert any("Kept the chiller running" in line for line in r.ctl.event_log)
    r.run_until(lambda s: all(r.speed(t.id) < 0.5 for t in r.turbos), 3 * 3600.0, "turbos stopped")
    r.run(120.0)
    assert r.st.chiller_on < 0.5 and r.st.primary_on < 0.5, "everything goes off once they have stopped"
    assert_no_new_hazard(r)


# ---------------------------------------------------------------------------------------- KI-2
def test_ki2_small_shut_off_while_roughing_lets_the_turbo_slow_down_first():
    """Was: small chamber, Pump to Rough pressed in high vacuum (turbo coasting at ~95 %), then 'Shut
    Off' -> straight to Facility Off, chiller off at once.  Now (`shut_off_spins_down_first`, as on
    the VC100): Turbo slowing - primary, chiller and turbo valve stay on until it has slowed."""
    r = make("small", "rough_from_high_vac")
    assert r.speed("turbo1") > 80
    r.press("shut_off")
    r.run(30.0)
    s = r.ctl.snapshot()
    assert s.current == S.TURBO_SLOWING and s.commands.chiller and s.commands.primary
    r.run(600.0)
    assert not r.monitor.found("T12"), r.monitor.report()
    assert_no_new_hazard(r)


# ---------------------------------------------------------------------------------------- KI-3
def test_ki3_small_turbo_valve_is_not_reopened_onto_an_unpumped_foreline():
    """Was: chamber vented, turbo coasting, Pump to Rough (turbo isolated, bypass open, foreline at
    chamber pressure), then Vent: Venting's own rule - 'turbo valve open while the turbo spins' -
    re-opened the valve one loop after the bypass closed, foreline still at several Torr.
    Now: an opening onto a turning turbo waits until the foreline is pumped down."""
    r = make("small", "venting_coasting_atm")
    r.press("pump_to_rough")
    r.run_until(lambda s: r.st.valves["bypass"].position > 0.9, 300, "bypass open")
    r.run(5.0)
    assert r.st.p_foreline > 20.0 and r.speed("turbo1") > 50
    r.press("vent")
    r.run(300.0)
    assert not r.monitor.found("T1", "T3", "T9"), r.monitor.report()
    assert r.st.valves["turbo_valve"].position > 0.9, "the valve never came back once the foreline was pumped"
    assert any("held closed" in line and "not pumped down yet" in line for line in r.ctl.event_log)
    assert_no_new_hazard(r)


@pytest.mark.parametrize("button", ["vent", "overnight_pump", "pump_to_high_vac", "pump_to_rough"])
def test_ki3_small_restart_with_a_coasting_turbo_never_opens_onto_atmosphere(button):
    """Was: the program (or the PC) restarts while the turbo coasts at 90 % - everything off, the
    foreline backfilled to atmosphere, Auto in Facility Off.  'Vent' or 'Overnight Pump' commanded
    the turbo valve and the primary pump together: the valve opened in a second, the two-relay pump
    needs 15 s before it starts, and the exhaust of a turbo at 90 % saw several hundred Torr.
    Now: the valve waits for the pump to run and the foreline to be pumped down."""
    r = Rig("small")
    st = r.st
    st.turbos["turbo1"].speed_pct = 90.0
    st.turbos["turbo1"].p_body_torr = 1e-3
    st.p_chamber = 1e-4 if button == "overnight_pump" else 760.0
    st.p_foreline = 760.0
    r.backend.sim.turbo_spindown_s = 2400.0
    r.tick(3)
    r.press(button)
    r.run(600.0)
    assert not r.monitor.found("T1", "T3", "T9"), r.monitor.report()
    assert_no_new_hazard(r)


def test_ki3_small_restart_with_a_coasting_turbo_gets_its_backing_back_by_itself():
    """... and with nothing pressed at all, Facility Off starts the primary and the chiller for the
    coasting turbo and opens its valve once the foreline is down - never before."""
    r = Rig("small")
    st = r.st
    st.turbos["turbo1"].speed_pct = 90.0
    st.turbos["turbo1"].p_body_torr = 1e-3
    st.p_foreline = 760.0
    r.backend.sim.turbo_spindown_s = 2400.0
    opened_at = None
    for _ in range(300):
        r.tick()
        if opened_at is None and st.valves["turbo_valve"].position > 0.02:
            opened_at = st.p_foreline
    assert opened_at is not None, "the coasting turbo never got its backing"
    assert opened_at < r.cfg.thresholds.turbo_on_threshold_torr * 1.5, f"valve opened with the foreline at {opened_at:.2f} Torr"
    assert st.primary_on > 0.5 and st.chiller_on > 0.5
    assert_no_new_hazard(r)


# ---------------------------------------------------------------------------------------- KI-4
@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("situation", ["high_vac", "high_vac_running_up", "slowing_fast", "venting_coasting_atm",
                                       "overnight_spinning", "rough_from_high_vac"])
def test_ki4_a_stopped_primary_pump_isolates_the_turning_turbos(which, situation):
    """Was: the backing pump stops with turbos turning; Auto kept the turbo valves OPEN ('a spinning
    turbo keeps its valve') and commanded a pump that was not there, so the foreline backfilled
    into the turbos until one tripped - the VC100 even knew (error 5000) and still held them open.
    Now: turbo valves shut and motors off within seconds, error 5012 (or 5000), Auto stops; the
    turbos coast isolated, cooled, and get their valves back only onto a pumped foreline."""
    r = make(which, situation)
    before = len(_errors(r))
    fault_catalogue(r)["primary_pump_stops"](r)
    r.run(900.0)
    assert r.ctl.snapshot().current == S.FACILITY_OFF and _errors(r)[before:], "not noticed"
    # The valve is shut within seconds.  What the logs added (2026-11-12): the foreline is at
    # atmosphere within 6 s of the pump stopping, so at full speed some gas is inside the turbo before
    # any valve can move (T1, T14) - that part no software can prevent; it is not asserted here.
    slow = [v for v in r.monitor.found("T3", "T9", "T8") if v.duration(r.t) > REACT_S]
    assert not slow, r.monitor.report()
    st = r.st
    for t in r.turbos:
        assert st.valves[t.turbo_valve].position < 0.02, f"{t.turbo_valve} open onto the dead foreline"
        assert not st.turbos[t.id].motor
    assert_no_new_hazard(r)


def test_ki4_small_notices_a_stopped_primary_pump_through_the_foreline_gauge():
    """The small chamber's pump has no feedback, but with a turbo turning the foreline gauge tells."""
    r = make("small", "high_vac")
    fault_catalogue(r)["primary_pump_stops"](r)
    r.run(60.0)
    s = r.ctl.snapshot()
    assert s.error.status and s.error.code == ERR_BACKING_LOST, s.error
    assert "Backing lost" in s.error.source and "foreline" in s.error.source


# ---------------------------------------------------------------------------------------- KI-5
@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("fault,situation", [("big_leak", "high_vac"), ("big_leak", "high_vac_running_up"),
                                             ("air_inrush", "high_vac"), ("air_inrush", "high_vac_running_up")])
def test_ki5_a_pressure_rise_in_high_vacuum_closes_the_gates(which, fault, situation):
    """Was: the chamber comes up while the gates are open (a leak, the hand vent valve, a seal) and
    Auto stayed in Pumping to High Vac with the gates open; with the turbos still running up nothing
    tripped at all.  Now: above the exposure limit for `pressure_trip_delay_s` the gates close,
    error 5013, Auto stops; the turbos keep their backing and cooling while they slow."""
    r = make(which, situation)
    before = len(_errors(r))
    fault_catalogue(r)[fault](r)
    r.run(900.0)
    assert r.ctl.snapshot().current == S.FACILITY_OFF
    assert all(v.duration(r.t) <= REACT_S for v in r.monitor.found("T2")), r.monitor.report()
    assert all(r.st.valves[t.gate_valve].position < 0.02 for t in r.turbos)
    new = _errors(r)[before:]
    assert new, "nothing in the log about it"
    # 5013 (chamber pressure) - or 5012 where the turbo has pushed the foreline over its limit first,
    # which the small chamber's roughing line allows; either way gate AND turbo valve are shut
    assert any(f"ERROR {ERR_CHAMBER_PRESSURE}" in line or f"ERROR {ERR_BACKING_LOST}" in line for line in new), new
    assert_no_new_hazard(r)


@pytest.mark.parametrize("which", CHAMBERS)
def test_ki5_a_big_leak_is_caught_before_the_turbos_see_it(which):
    """A leak that takes a few seconds to matter: the gates are shut before the chamber is above
    what the monitor calls un-roughed - the turbos never see it at all."""
    r = make(which, "high_vac")
    fault_catalogue(r)["big_leak"](r)
    r.run(300.0)
    assert not r.monitor.found("T1", "T2"), r.monitor.report()
    assert max(t.p_body_torr for t in r.st.turbos.values()) < r.monitor.p_hot


@pytest.mark.parametrize("which", CHAMBERS)
def test_ki5_normal_pressures_do_not_trip(which):
    """A burst to half the trip level (`high_vac_pressure_trip`, 1e-1 mBar) is not a trip."""
    r = make(which, "high_vac")
    level = r.cfg.thresholds.high_vac_pressure_trip_torr
    assert level is not None and abs(torr_to("mbar", level) - 0.1) < 1e-9
    r.st.p_chamber = 0.5 * level
    r.run(120.0)
    s = r.ctl.snapshot()
    assert s.current == S.PUMPING_TO_HIGH_VAC and not s.error.status
    assert all(r.st.valves[t.gate_valve].position > 0.98 for t in r.turbos)


@pytest.mark.parametrize("which", CHAMBERS)
def test_ki5_a_moderate_leak_trips_at_the_high_vac_level(which):
    """Test engineers, 2026-11-12: normal work stays below 1e-2 mBar, so the trip is at 1e-1 mBar -
    not at the exposure limit (1 / 1.25 mBar).  A leak that would level out at a few 1e-1 mBar
    closes the gates before the chamber gets anywhere near the exposure limit."""
    r = make(which, "high_vac")
    r.backend.sim.leak_torr_l_s = 0.03 * r.backend.sim.chamber_volume_l
    r.disturbances.append("big_leak")
    peak = 0.0
    for _ in range(300):
        s = r.tick()
        if s.error.status:
            break
        peak = max(peak, r.st.p_chamber)
    else:
        raise AssertionError("no trip in five minutes")
    assert s.error.code in (ERR_CHAMBER_PRESSURE, ERR_BACKING_LOST), s.error
    if s.error.code == ERR_CHAMBER_PRESSURE:
        assert "limit 1.0e-01 mBar" in s.error.source, s.error
    else:                         # small chamber: the turbo pushed the foreline over its limit first
        assert which == "small" and "the foreline is at" in s.error.source, s.error
    assert peak < r.monitor.p_rough, f"the chamber reached {torr_to('mbar', peak):.1e} mBar before the gates closed"
    r.run(60.0)
    assert r.ctl.snapshot().current == S.FACILITY_OFF
    assert all(r.st.valves[t.gate_valve].position < 0.02 for t in r.turbos)
    assert not r.monitor.found("T1", "T2"), r.monitor.report()


@pytest.mark.parametrize("which", CHAMBERS)
def test_ki5_the_high_vac_level_does_not_trip_the_pump_down(which):
    """The gates open near the turbo-on threshold, which is ABOVE the 1e-1 mBar trip level: the level
    only counts once the chamber has pumped down below half of it.  (The simulator pumps faster than
    the real chambers and is already below the level when Pumping to High Vac starts, so the test
    holds the chamber at 3 x the level for the first 20 s of that state.)"""
    r = Rig(which)
    level = r.cfg.thresholds.high_vac_pressure_trip_torr
    r.press("pump_to_high_vac")
    held = 0
    for _ in range(4 * 3600):
        s = r.tick()
        assert not s.error.status, s.error
        if s.current == S.PUMPING_TO_HIGH_VAC:
            if held < 20:
                if held == 0:
                    r.ctl.auto.mem.hv_trip_armed = False      # as if the state had been entered above the level
                else:
                    assert not r.ctl.auto.mem.hv_trip_armed
                r.st.p_chamber = 3.0 * level
                held += 1
            elif r.ctl.auto.mem.hv_trip_armed and r.st.p_chamber < 0.01 * level:
                break
    else:
        raise AssertionError("high vacuum not reached")
    assert held == 20
    assert all(r.st.valves[t.gate_valve].position > 0.98 for t in r.turbos)
    assert not r.monitor.found("T1", "T2"), r.monitor.report()


def _hv_step(sm, cfg, wrg, prev, now):
    inp = Inputs()
    inp.pressures_torr = {g.id: 1e-3 for g in cfg.gauges}
    inp.pressures_torr[cfg.main_gauge.id] = wrg
    inp.primary_read = inp.chiller_read = True
    inp.valve_reads = dict(prev.valves)
    for t in cfg.turbos:
        ti = TurboInputs(speed_pct=100.0)
        if t.control_mode == "shimadzu_contacts":
            ti.contacts = {"rotating": True, "normal": True}
        inp.turbos[t.id] = ti
    return sm.step(AutoInput(inp, prev, S.PUMPING_TO_HIGH_VAC, S.PUMPING_TO_HIGH_VAC, 0, set(), now, False,
                             cfg.thresholds.turbo_on_threshold_torr, None, False))


def _hv_prev(cfg):
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.primary = c.primary_run = c.primary_power = c.chiller = True
    for t in cfg.turbos:
        c.valves[t.turbo_valve] = c.valves[t.gate_valve] = True
        c.turbo_motor[t.id] = True
    return c


@pytest.mark.parametrize("which", CHAMBERS)
def test_ki5_unit_the_high_vac_level_arms_below_half_and_null_means_the_exposure_limit(which):
    from tests.safety_rig import load_profile
    cfg = load_profile(which)
    level = cfg.thresholds.high_vac_pressure_trip_torr
    delay = cfg.timings.pressure_trip_delay_s
    sm = AutoStateMachine(cfg)
    exposure = sm._exposure_limit(cfg.thresholds.turbo_on_threshold_torr)
    assert level < exposure
    prev = _hv_prev(cfg)
    # just after the gates opened: 3 x the level, far longer than the delay - not armed, no trip
    for i in range(10):
        out = _hv_step(sm, cfg, 3 * level, prev, 100.0 + i)
        assert out.trip is None and not sm.mem.hv_trip_armed
    # 0.8 x the level does not arm it; 0.4 x does
    _hv_step(sm, cfg, 0.8 * level, prev, 111.0)
    assert not sm.mem.hv_trip_armed
    _hv_step(sm, cfg, 0.4 * level, prev, 112.0)
    assert sm.mem.hv_trip_armed
    # now 2 x the level: nothing for less than the delay, then the gates close with 5013
    out = _hv_step(sm, cfg, 2 * level, prev, 120.0)
    assert out.trip is None
    out = _hv_step(sm, cfg, 2 * level, prev, 120.0 + delay + 0.1)
    assert out.trip is not None and out.trip[0] == ERR_CHAMBER_PRESSURE
    assert not any(out.commands.valves[t.gate_valve] for t in cfg.turbos)
    # one sample back below resets the clock
    sm2 = AutoStateMachine(cfg)
    _hv_step(sm2, cfg, 0.1 * level, prev, 0.0)
    _hv_step(sm2, cfg, 2 * level, prev, 1.0)
    _hv_step(sm2, cfg, 0.9 * level, prev, 2.0)
    assert _hv_step(sm2, cfg, 2 * level, prev, 1.0 + delay + 0.5).trip is None
    # null: only the exposure limit trips
    cfg.thresholds.high_vac_pressure_trip_torr = None
    sm3 = AutoStateMachine(cfg)
    _hv_step(sm3, cfg, 0.1 * level, prev, 0.0)
    _hv_step(sm3, cfg, 2 * level, prev, 1.0)
    assert _hv_step(sm3, cfg, 2 * level, prev, 60.0).trip is None
    _hv_step(sm3, cfg, 1.5 * exposure, prev, 61.0)
    assert _hv_step(sm3, cfg, 1.5 * exposure, prev, 61.0 + delay + 0.1).trip is not None


# ---------------------------------------------------------------------------------------- KI-6
@pytest.mark.xfail(strict=True, reason="KI-6 (open by decision): the small chamber's primary pump has no feedback "
                                       "unless the frequency reader is on - with no turbo turning a pump that "
                                       "stops is not noticed")
def test_ki6_small_a_stopped_primary_pump_is_noticed_while_roughing():
    r = make("small", "rough_mid")
    before = len(_errors(r))
    fault_catalogue(r)["primary_pump_stops"](r)
    r.run(300.0)
    assert _errors(r)[before:], "the primary pump has been stopped for five minutes and nothing was logged"


# ---------------------------------------------------------------------------------------- KI-7
GAUGE_ROLES = ["chamber", "foreline", "turbo"]


def _break(r, role, how):
    """A gauge dies ('fails': about 0 V) or sticks ('freezes': the last voltage, to the last digit)."""
    fault_catalogue(r)[f"{role}_gauge_{how}"](r)
    cfg = r.cfg
    return {"chamber": cfg.main_gauge, "foreline": cfg.foreline_gauge, "turbo": cfg.turbo_gauge(cfg.turbos[0].id)}[role]


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("role", GAUGE_ROLES)
@pytest.mark.parametrize("how", ["fails", "freezes"])
@pytest.mark.parametrize("situation", ["rough_mid", "high_vac", "slowing_fast"])
def test_ki7_a_dead_or_frozen_gauge_warns_and_shuts_the_facility_down_safely(which, role, how, situation):
    """Was: a dead gauge was simply believed - a dead chamber gauge blinded the High Vac leak trip, a
    dead foreline gauge the backing-lost check, a dead turbo gauge the hot-start shut-off.
    Now (2026-11-16): below 1 V for 3 s, or 100 readings in a row exactly equal -> pop-up, error 5014,
    Facility Off; the turbos keep their backing and cooling while they slow."""
    r = make(which, situation)
    popups = len([m for k, m in r.dialogs.log if k in ("info", "notice")])
    g = _break(r, role, how)
    r.run(20.0 if how == "fails" else 130.0)
    s = r.ctl.snapshot()
    assert s.error.status and s.error.code == ERR_GAUGE_FAULT and g.label in s.error.source, s.error
    assert ("no signal" if how == "fails" else "frozen") in s.error.source
    assert s.current == S.FACILITY_OFF
    for kind in ("vent", "bypass", "gate"):
        for v in r.cfg.valves_of_kind(kind):
            assert r.st.valves[v.id].position < 0.02, f"{v.label} still open"
    assert not any(t.motor for t in r.st.turbos.values())
    new = [m for k, m in r.dialogs.log if k in ("info", "notice")][popups:]
    assert len(new) == 1 and g.label in new[0] and "shut down safely" in new[0], new
    assert r.ctl.inputs.pressure(g.id) != r.ctl.inputs.pressure(g.id), "the reading of a gauge in fault is not a number"
    r.run(1200.0)
    assert len([m for k, m in r.dialogs.log if k in ("info", "notice")][popups:]) == 1, "the pop-up must not repeat"
    assert_no_new_hazard(r)


@pytest.mark.parametrize("which", CHAMBERS)
def test_ki7_the_error_goes_when_the_gauge_reads_properly_again(which):
    r = make(which, "rough_mid")
    g = _break(r, "foreline", "fails")
    r.run(20.0)
    assert r.ctl.snapshot().error.code == ERR_GAUGE_FAULT
    r.backend.set_fault(f"gauge_fault_{g.id}", False)
    r.run(10.0)
    assert not r.ctl.snapshot().error.status
    assert any(f"{g.label}: reading valid again" in line for line in r.ctl.event_log)
    assert r.ctl.inputs.pressure(g.id) == r.ctl.inputs.pressure(g.id)
    assert r.ctl.snapshot().current == S.FACILITY_OFF, "nothing restarts by itself"


# ---------------------------------------------------------------------------------------- KI-9
def test_ki9_handover_of_a_high_vac_line_up_on_a_vented_chamber_is_refused_safely():
    """Was: Admin - primary on, turbo valve and gate open, turbo stopped, chamber at atmosphere -
    reads as 'Pumping to High Vac'; Auto accepted it, pumped the chamber through the stopped turbo
    and started the motor at the 5 Torr hot-start limit with the gate open.  Now: Pumping to High
    Vac sees a chamber far above the exposure limit, closes the gate and stops with error 5013;
    the operator pumps down properly from Facility Off."""
    from tests.test_safety_handover import LINEUPS, _admin, _hand_over, _set
    r = _admin("small")
    _set(r, **LINEUPS["high_vac_lineup__turbos_stopped__chamber_vented"][0])
    assert _hand_over(r) and r.ctl.mode == Mode.AUTO
    r.run(120.0)
    s = r.ctl.snapshot()
    # (whichever protection sees it first: the foreline at atmosphere behind an open turbo valve, or
    #  the chamber at atmosphere behind an open gate)
    assert s.error.status and s.error.code in (ERR_CHAMBER_PRESSURE, ERR_BACKING_LOST) and s.current == S.FACILITY_OFF
    assert r.st.valves["gate"].position < 0.02 and not r.st.turbos["turbo1"].motor
    r.run(1800.0)
    assert not r.monitor.found("T2", "T10"), r.monitor.report()
    assert_no_new_hazard(r)


# --------------------------------------------------------------------------------------- KI-10
@pytest.mark.parametrize("which", CHAMBERS)
def test_ki10_a_turbo_is_never_driven_behind_a_shut_turbo_valve(which):
    """Found 2026-11-12 by the random operator, on the path the KI-2 fix opened: turbo coasting at
    atmosphere, Pump to Rough (turbo valve isolated, bypass open), Shut Off -> Turbo slowing, then
    Pump to High Vac on that page.  Frame 7 restarts the motor there; the valve could not be
    re-opened (foreline not pumped down), so the rotor was driven against a dead volume.
    Now: the motor follows the valve (global rule 4) - it is started in Engage Turbo."""
    r = make(which, "venting_coasting_atm")
    assert r.any_turning()
    r.press("pump_to_rough")
    r.run_until(lambda s: s.current == S.PUMPING_TO_ROUGH and s.substate == 3, 600.0, "roughing")
    r.run(3.0)
    r.press("shut_off")
    r.run(1.0)
    r.press("pump_to_high_vac")
    for _ in range(40):
        r.tick()
        for t in r.turbos:
            c = r.ctl.cmds
            assert not (c.turbo_motor[t.id] and not c.valves[t.turbo_valve]), f"{t.id} driven behind its shut valve"
    r.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 6 * 3600.0, "Pumping to High Vac")
    r.run(300.0)
    assert all(r.ctl.cmds.turbo_motor[t.id] for t in r.turbos), "the turbos were started again in the end"
    assert not r.monitor.found("K3"), r.monitor.report()
    assert_no_new_hazard(r)


# --------------------------------------------------------------------------------------- KI-11
@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("cause", ["leak", "primary_pump_stops"])
def test_ki11_a_backing_lost_trip_shuts_the_gate_with_the_turbo_valve(which, cause):
    """Found 2026-11-12 on the measured plant.  A foreline over its limit means the pump behind it has
    gone - or the turbo is pushing more gas into it than the pump takes (a chamber leak; on the small
    chamber's roughing line that reaches the limit before the chamber gauge does).  The trip used to
    shut only the turbo valve and leave the gate to the error shutdown one loop later: with a leak the
    rotor kept taking gas through its open gate with nowhere to put it.  Now both go in one frame."""
    r = make(which, "high_vac")
    if cause == "leak":
        r.backend.sim.leak_torr_l_s = 0.03 * r.backend.sim.chamber_volume_l
        r.disturbances.append("big_leak")
    else:
        fault_catalogue(r)[cause](r)
    for _ in range(600):
        s = r.tick()
        # at no moment is a turbo valve shut while the gate of the same (turning) turbo is still open
        for t in r.turbos:
            if r.speed(t.id) > 5.0 and not s.commands.valves[t.turbo_valve]:
                assert not s.commands.valves[t.gate_valve], f"{t.turbo_valve} shut, {t.gate_valve} still commanded open"
                assert not s.commands.turbo_motor[t.id]
        if s.error.status:
            break
    else:
        raise AssertionError("no trip in ten minutes")
    assert s.error.code in (ERR_BACKING_LOST, ERR_CHAMBER_PRESSURE, 5000), s.error
    assert not any(s.commands.valves[t.gate_valve] for t in r.turbos), "a gate is still commanded open after the trip"
    r.run(120.0)
    assert r.ctl.snapshot().current == S.FACILITY_OFF and r.ctl.snapshot().error.status
    assert_no_new_hazard(r)
