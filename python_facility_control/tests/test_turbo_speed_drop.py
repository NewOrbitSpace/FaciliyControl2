"""Turbo speed-drop trip: error 5015, every turbo isolated (2026-11-17).

A turbo that loses speed FAST has gas on it - whatever the gauges say.  Both real incidents on the
VC100 looked exactly like that in the log and nothing reacted (the HiPace pumps reported a
warning, not an error):

    2026-10-28 15:06   Turbo 2  96.7 % -> 18.0 %,  Turbo 5  97.8 % -> 18.4 %          (one 5 s log row)
    2026-11-05 09:46   Turbo 2  71.0 -> 10.7,  Turbo 6  77.8 -> 12.6,  Turbo 5  76.7 -> 31.6,
                       Turbo 3  77.7 -> 51.8 -> 4.6                                   (5 s rows)

Two rules, in per cent of FULL speed, each needing the fall to still be there 1 s later:

    fast    5 % within 1 s   (the test engineer's figure, 2026-11-17: "so it reacts a bit quicker")
    crash   30 % within 5 s  (always on)

The fast rule is suspended for 15 s after anything that is EXPECTED to slow a rotor down - its
motor switched off, standby switched, its gate commanded open onto it, the chiller switched off -
and nothing at all is judged for two minutes after the small chamber's motor is switched on or off.
What the logs (18 + 2 days) say about those:

    HiPace, motor switched off                    at most 0.6 % in the first 5 s, 2.1 % in 30 s
    gate opened onto a rotor above 15 % (Auto)    at most 2.8 % in 5 s, 7.0 % in 30 s (1.5e-1 mBar)
    gate opened by hand onto one at 100 % (Admin) 5.1 % in 5 s (0.8 mBar)
    small chamber, speed signal after a start     96 % -> 17 % in a minute, 7.4 % in 5 s
    Shimadzu braking to a stop (no speed signal)  1 % in 5 s

and that nothing else, on any logged day, lost 5 % even within 10 s.

Reaction: EVERY gate and turbo valve shut, motors off, the cooling kept on while they slow down,
until 'Clear Error'.  Manual / Admin: a line in the log, no action.
"""
import pytest

from facility_control.model import ERR_TURBO_SPEED_DROP, FacilityState as S, Mode
from tests.safety_rig import CHAMBERS, Rig
from tests.safety_scenarios import PLANTS

SHUT = 0.02


def _code(r):
    s = r.ctl.snapshot()
    return s.error.code if s.error.status else 0


def _watched(r):
    """The turbos that have a speed reading (the Shimadzu pumps on the VC100 have none)."""
    return [t for t in r.cfg.turbos if t.in_use and t.has_speed]


def _play(r, tid, speeds, until_trip=False):
    """Make the rotor follow `speeds`, one value per second (the plant, so every reading sees it).
    Returns the number of readings it took to trip (0 = no trip)."""
    for i, v in enumerate(speeds):
        r.st.turbos[tid].speed_pct = float(v)
        r.tick()
        if until_trip and r.ctl._speed_trip is not None:
            return i + 1
    return len(speeds) if r.ctl._speed_trip is not None else 0


def _ramp(a, b, seconds):
    return [a + (b - a) * (i + 1) / seconds for i in range(int(seconds))]


def _all_shut(r):
    gates = [r.st.valves[t.gate_valve].position for t in r.turbos]
    tvs = [r.st.valves[v.id].position for v in r.cfg.valves_of_kind("turbo")]
    return max(gates + tvs) < SHUT


def _glide(r, tid, target, hold=8):
    """Bring the rotor to `target` slowly (3 % a second - under both rules) and let it sit there."""
    cur = r.speed(tid)
    steps = int(abs(cur - target) / 3.0) + 1
    assert not _play(r, tid, _ramp(cur, target, steps) + [target] * hold), "the approach itself tripped"


def _reports(r):
    """The '!! ... is losing speed fast' lines (not the 'ERROR 5015: ...' line that follows the first)."""
    return [x for x in r.ctl.event_log if "losing speed fast" in x and "ERROR" not in x]


def _at_high_vac(which, **kw):
    r = Rig(which, **kw)
    r.to_high_vac()
    r.run(150.0)                                      # past every blanking / expected-slow-down window
    assert _code(r) == 0 and r.ctl.snapshot().current == S.PUMPING_TO_HIGH_VAC
    return r


# ------------------------------------------------------------------------------- the settings
@pytest.mark.parametrize("which", CHAMBERS)
def test_profile_settings(which):
    r = Rig(which)
    th, tm = r.cfg.thresholds, r.cfg.timings
    assert th.turbo_speed_drop_pct == 5 and tm.turbo_speed_drop_window_s == 1.0          # the engineer's figure
    assert th.turbo_speed_crash_pct == 30 and tm.turbo_speed_crash_window_s == 5.0
    assert tm.turbo_speed_drop_confirm_s == 1.0 and tm.turbo_speed_drop_expected_s == 15.0
    assert tm.turbo_speed_drop_blank_after_switch_s == (120.0 if which == "small" else 0.0)
    assert [t.id for t in _watched(r)] == (["turbo1"] if which == "small" else ["turbo2", "turbo3", "turbo5", "turbo6"])


# ------------------------------------------------------------------------------- it fires on the incidents
INCIDENTS = {   # the logged speeds, 5 s apart; in between the rotor is taken to have fallen steadily
    "2026-10-28 Turbo 2": [96.7, 18.0],
    "2026-10-28 Turbo 5": [97.8, 18.4],
    "2026-11-05 Turbo 2": [71.0, 10.7],
    "2026-11-05 Turbo 3": [77.7, 51.8, 4.6],
    "2026-11-05 Turbo 5": [76.7, 31.6],
    "2026-11-05 Turbo 6": [77.8, 12.6],
}


@pytest.mark.parametrize("name", sorted(INCIDENTS))
def test_every_logged_incident_trace_trips_within_three_seconds(name):
    """Coasting with the motors off for minutes, as on both days - nothing is suspended any more."""
    r = _at_high_vac("vc100", tweak=PLANTS["long_coast"])
    rows = INCIDENTS[name]
    tid = "turbo" + name[-1]
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.TURBO_SLOWING, what="Turbo slowing")
    r.run(120.0)
    _glide(r, tid, rows[0])
    assert _code(r) == 0
    fall = []
    for a, b in zip(rows, rows[1:]):
        fall += _ramp(a, b, 5)
    took = _play(r, tid, fall + [rows[-1]] * 3, until_trip=True)
    assert 0 < took <= 3, f"{name}: tripped after {took} s (0 = not at all)"
    assert _code(r) == ERR_TURBO_SPEED_DROP, r.ctl.error
    assert r.cfg.turbo(tid).label in r.ctl.error.source and "losing speed fast" in r.ctl.error.source


# ------------------------------------------------------------------------------- the reaction
@pytest.mark.parametrize("which", CHAMBERS)
def test_a_collapse_isolates_every_turbo_and_keeps_the_cooling(which):
    r = _at_high_vac(which)
    victim = _watched(r)[-1].id
    took = _play(r, victim, _ramp(100.0, 10.0, 6), until_trip=True)
    assert 0 < took <= 2, f"tripped {took} s after the fall began (0 = not at all)"
    s = r.ctl.snapshot()
    assert s.error.code == ERR_TURBO_SPEED_DROP and s.current == S.FACILITY_OFF
    c = s.commands
    assert not any(c.valves.values()), c.valves                   # every gate, every turbo valve, bypass, vent
    assert not any(c.turbo_motor.values()) and not any(c.turbo_standby.values())
    assert c.chiller, "the cooling must stay on while the rotors slow down"
    info = [m for k, m in r.dialogs.log if k == "info"]
    assert len(info) == 1 and "5015" in info[0] and "Clear Error" in info[0]
    r.run(15.0)
    assert _all_shut(r), "a gate or turbo valve is still off its seat 15 s after the trip"
    # ---- it STAYS isolated while the error is there, however good the foreline looks
    others = [t for t in r.turbos if t.id != victim]
    for _ in range(180):
        s = r.tick()
        assert s.error.code == ERR_TURBO_SPEED_DROP
        assert not any(s.commands.valves.values()) and not any(s.commands.turbo_motor.values())
        if any(r.speed(t.id) > 20.0 for t in others):
            assert s.commands.chiller
    assert _all_shut(r)
    assert not r.monitor.found("T7", "T9", "T12", "K3", "K4"), r.monitor.report()
    assert len(_reports(r)) == 1, _reports(r)                      # one line, not one per second of the fall
    # ---- 'Clear Error': the error goes; nothing restarts, gates stay shut
    r.ctl.request_clear_error()
    s = r.run(30.0)
    assert not s.error.status and s.current == S.FACILITY_OFF
    assert not any(s.commands.turbo_motor.values())
    assert all(r.st.valves[t.gate_valve].position < SHUT for t in r.turbos)
    assert any("Every turbo isolated" in line for line in r.ctl.event_log)


def test_one_turbo_falling_isolates_the_others_too():
    """They share one foreline, and both incidents hit more than one pump."""
    r = _at_high_vac("vc100")
    assert _play(r, "turbo3", _ramp(100.0, 20.0, 5), until_trip=True)
    r.run(15.0)
    assert _all_shut(r)
    for t in r.turbos:
        assert not r.st.turbos[t.id].motor, f"{t.id} is still driven"
    assert min(r.speed(t.id) for t in r.turbos if t.id != "turbo3") > 80.0      # the others just coast


@pytest.mark.parametrize("which", CHAMBERS)
def test_it_works_while_coasting_with_the_motors_off(which):
    """Turbo slowing, two and a half minutes after the stop - where both incidents started."""
    r = _at_high_vac(which, tweak=PLANTS["long_coast"])
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.TURBO_SLOWING, what="Turbo slowing")
    r.run(150.0)
    victim = _watched(r)[0].id
    start = r.speed(victim)
    assert start > 60.0
    assert 0 < _play(r, victim, _ramp(start, 5.0, 5), until_trip=True) <= 3
    assert _code(r) == ERR_TURBO_SPEED_DROP
    r.run(15.0)
    assert _all_shut(r)


# ------------------------------------------------------------------------------- the two rules
@pytest.mark.parametrize("which", CHAMBERS)
def test_fast_rule_four_percent_a_second_is_not_a_trip_seven_is(which):
    r = _at_high_vac(which)
    tid = _watched(r)[0].id
    assert not _play(r, tid, _ramp(100.0, 80.0, 5) + [80.0] * 5)          # 4 %/s, 20 % in 5 s: under both rules
    _play(r, tid, [100.0] * 20)
    assert r.ctl._speed_trip is None and not _reports(r)
    assert 0 < _play(r, tid, _ramp(100.0, 79.0, 3) + [79.0] * 2, until_trip=True) <= 3       # 7 %/s


@pytest.mark.parametrize("which", CHAMBERS)
def test_the_fast_rule_does_not_judge_a_rotor_that_has_all_but_stopped(which):
    """Below the profile's 'still spinning' speed (15 %) a fall of 5 % is not a crash."""
    r = _at_high_vac(which)
    tid = _watched(r)[0].id
    r.monitor.enabled = False
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.TURBO_SLOWING, what="Turbo slowing")
    r.run(150.0)
    _glide(r, tid, 14.0)
    assert not _play(r, tid, [8.0, 3.0, 0.0, 0.0, 0.0])
    assert not _reports(r)


@pytest.mark.parametrize("which", CHAMBERS)
def test_one_bad_reading_is_not_a_trip(which):
    """A reading of 0 that is back to normal a second later: a glitch, not a rotor."""
    r = _at_high_vac(which)
    tid = _watched(r)[0].id
    for _ in range(3):
        assert not _play(r, tid, [100.0, 100.0, 0.0, 100.0, 100.0, 100.0])
    assert _code(r) == 0 and not _reports(r)
    # ... but a reading that STAYS at 0 is (a rotor that has stopped dead, or its signal gone)
    assert _play(r, tid, [0.0, 0.0, 0.0])


@pytest.mark.parametrize("which", CHAMBERS)
def test_after_a_standstill_of_the_loop_only_the_crash_rule_counts(which):
    """With `blocking_waits` nothing is read during a settle wait (9 s after a gate moves).  The
    fast rule does not compare across it; the crash rule does, up to 15 s."""
    r = _at_high_vac(which)
    tid = _watched(r)[0].id
    r.monitor.enabled = False
    r.st.turbos[tid].speed_pct = 85.0                  # 15 % lost while the loop stood still for 9 s
    r._advance(9.0)
    assert not _play(r, tid, [85.0] * 4)               # not 5 %-in-1-s, not 30 %-in-5-s
    _play(r, tid, [100.0] * 20)
    r.st.turbos[tid].speed_pct = 40.0                  # 60 % lost during the same kind of wait
    r._advance(9.0)
    assert 0 < _play(r, tid, [40.0] * 3, until_trip=True) <= 2
    assert _code(r) == ERR_TURBO_SPEED_DROP


# ------------------------------------------------------------------------------- expected slow-downs
@pytest.mark.parametrize("which", CHAMBERS)
def test_a_complete_normal_cycle_never_trips(which):
    """Pump down, full speed, shutdown, pump down again with the turbos still coasting (the 9 Nov
    sequence: gates open onto turning rotors), 'Vent' with the turbos running (VC100: they drop
    into standby), vent and shutdown - every speed change the facility makes by itself."""
    r = Rig(which)
    r.to_high_vac()
    r.run(120.0)
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.TURBO_SLOWING, what="Turbo slowing")
    r.run(600.0)
    r.press("pump_to_high_vac")
    r.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, what="Pumping to High Vac again")
    r.run(600.0)
    r.press("vent")
    r.run_until(lambda s: s.current == S.VENTING, what="Venting")
    r.run(1800.0)
    r.press("pump_to_high_vac")
    r.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 3 * 3600.0, "Pumping to High Vac a third time")
    r.run(300.0)
    r.press("vent_and_shutdown")
    r.run_until(lambda s: s.current == S.FACILITY_OFF, 6 * 3600.0, "Facility Off")
    assert r.ctl._speed_trip is None and not _reports(r)
    assert _code(r) == 0
    r.assert_safe()


NORMAL = {   # the fastest falls in the logs that are NOT a crash, as (from, to, seconds) - with NOTHING suspended
    "small chamber speed signal after a start (7.4 % in 5 s)": (100.0, 21.0, 54),
    "gate opened by hand onto a turbo at full speed (5 % in 5 s)": (100.0, 80.0, 20),
    "three times the fastest fall after a gate opens in Auto (8.4 % in 5 s)": (100.0, 50.0, 30),
    "Shimadzu-like braking (1 % in 5 s)": (100.0, 88.0, 60),
}


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("name", sorted(NORMAL))
def test_the_fastest_normal_falls_do_not_trip_even_unannounced(which, name):
    a, b, seconds = NORMAL[name]
    r = _at_high_vac(which)
    tid = _watched(r)[0].id
    now = r.ctl.clock()
    assert r.ctl._speed_blank_until.get(tid, 0.0) <= now and r.ctl._speed_expect_until.get(tid, 0.0) <= now
    assert not _play(r, tid, _ramp(a, b, seconds) + [b] * 5), name
    assert _code(r) != ERR_TURBO_SPEED_DROP and not _reports(r)


def test_motor_switched_off_a_braking_pump_is_expected():
    """The engineer's case: a pump that decelerates actively when it is switched off.  7 % a second
    for 3 s right after the stop is not a trip; the very same fall two minutes later is.
    (`blocking_waits: false`, so that the loop reads every second through the settle waits.)"""
    def tweak(cfg):
        PLANTS["long_coast"](cfg)
        cfg.timings.blocking_waits = False
    r = _at_high_vac("vc100", tweak=tweak)
    r.monitor.enabled = False
    r.press("shutdown")
    r.run_until(lambda s: not any(s.commands.turbo_motor.values()), what="motors off")
    r.tick()
    assert r.ctl._speed_expect_until["turbo2"] > r.ctl.clock() + 10.0
    assert not _play(r, "turbo2", _ramp(100.0, 79.0, 3) + [79.0] * 4)
    assert not _reports(r) and _code(r) == 0
    r.run(120.0)
    start = r.speed("turbo2")
    assert 0 < _play(r, "turbo2", _ramp(start, start - 21.0, 3) + [start - 21.0] * 2, until_trip=True) <= 3


def test_motor_switched_off_a_real_crash_right_after_is_still_caught():
    """... and the crash rule is never suspended: 98 % -> 18 % in 5 s, two seconds after the stop."""
    r = _at_high_vac("vc100")
    r.press("shutdown")
    r.run_until(lambda s: not any(s.commands.turbo_motor.values()), what="motors off")
    r.tick(2)
    assert not r.ctl._speed_blank_until                    # VC100: nothing is blanked outright
    assert r.ctl._speed_expect_until["turbo5"] > r.ctl.clock()
    assert 0 < _play(r, "turbo5", _ramp(98.0, 18.0, 5) + [18.0] * 2, until_trip=True) <= 4
    assert _code(r) == ERR_TURBO_SPEED_DROP


@pytest.mark.parametrize("suspend", [True, False])
def test_a_gate_opening_onto_a_turning_rotor_is_expected(suspend):
    """9 Nov: Pump to High Vac with the turbos still coasting - the gates open onto turning rotors
    and each loses a few per cent.  Here: 8 % in the second after its gate opens.  No trip, and the
    pump-down finishes.  (Run with the loop reading through the settle waits, `blocking_waits:
    false`: with the VI's blocking waits the loop stands still for 9 s after every gate command and
    the fast rule cannot compare across that anyway.)  `suspend=False` is the control: with the
    15 s switched off the same dip trips."""
    def tweak(cfg):
        PLANTS["long_coast"](cfg)
        cfg.timings.blocking_waits = False
        if not suspend:
            cfg.timings.turbo_speed_drop_expected_s = 0.0
    r = _at_high_vac("vc100", tweak=tweak)
    assert not r.ctl.blocking
    r.monitor.enabled = False
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.TURBO_SLOWING, what="Turbo slowing")
    r.run(600.0)
    r.press("pump_to_high_vac")
    gate = r.cfg.turbo("turbo2").gate_valve
    r.run_until(lambda s: s.commands.valves[gate], what="Gate 2 commanded open")
    r.tick(2)                                          # the gas arrives a moment after the command
    sp = r.speed("turbo2")
    assert sp > 30.0
    tripped = _play(r, "turbo2", [sp - 8.0] * 3)
    if not suspend:
        assert tripped and _code(r) == ERR_TURBO_SPEED_DROP
        return
    assert r.ctl._speed_expect_until["turbo2"] > r.ctl.clock()
    assert not tripped and not _reports(r) and _code(r) == 0
    r.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, what="Pumping to High Vac")
    assert r.ctl._speed_trip is None


def test_dropping_into_standby_is_expected():
    """VC100, 'Vent' from high vacuum: the turbos keep running with their gates closed, which puts
    them in standby (50 / 66 %).  7 % a second right after is not a trip."""
    r = _at_high_vac("vc100")
    r.monitor.enabled = False
    r.press("vent")
    r.run_until(lambda s: s.commands.turbo_standby["turbo3"], what="Turbo 3 in standby")
    r.tick()
    assert r.ctl._speed_expect_until["turbo3"] > r.ctl.clock() + 10.0
    assert not _play(r, "turbo3", _ramp(100.0, 79.0, 3) + [79.0] * 4)
    assert not _reports(r) and _code(r) == 0


def test_the_chiller_switching_off_takes_the_speed_reading_with_it():
    """Small chamber: at the end of a shutdown the chiller goes off with the turbo at 15 %.  Where
    its contactor feeds the turbo controller the speed reading then drops to nothing - no trip."""
    r = _at_high_vac("small")
    r.press("shutdown")
    r.run_until(lambda s: s.current == S.FACILITY_OFF and not s.commands.chiller, 3 * 3600.0, "chiller off")
    assert not _play(r, "turbo1", [0.0] * 10)
    assert not _reports(r) and _code(r) == 0


def test_small_chamber_start_up_is_blanked():
    """Its speed signal reads 96 % the moment the motor starts and falls to 17 % within a minute.
    Even three times as fast as logged, nothing is judged in the first two minutes after a start."""
    r = Rig("small")
    r.tick(2)
    r.press("pump_to_high_vac")
    r.run_until(lambda s: s.commands.turbo_motor["turbo1"], what="turbo start")
    r.tick()                                           # the reading after the command
    assert r.ctl._speed_blank_until["turbo1"] == pytest.approx(r.ctl.clock() + 119.0, abs=2.5)
    assert not _play(r, "turbo1", _ramp(96.0, 17.0, 18) + [17.0] * 20)
    assert _code(r) == 0 and not _reports(r)
    r.run(120.0)
    assert r.ctl.clock() > r.ctl._speed_blank_until["turbo1"]
    _play(r, "turbo1", [90.0] * 8)
    assert _play(r, "turbo1", _ramp(90.0, 10.0, 5) + [10.0] * 3)


def test_small_chamber_is_also_blanked_after_a_stop():
    """What its speed signal does when the motor is switched off has not been logged.  Until it
    has, the two minutes after a stop are not judged either - and after them the trip is back."""
    r = _at_high_vac("small")
    r.monitor.enabled = False
    r.press("shutdown")
    r.run_until(lambda s: not s.commands.turbo_motor["turbo1"], what="motor off")
    r.tick()
    assert r.ctl._speed_blank_until["turbo1"] == pytest.approx(r.ctl.clock() + 119.0, abs=12.0)
    assert not _play(r, "turbo1", _ramp(95.0, 0.0, 5) + [0.0] * 20)      # the signal simply dropping to zero
    assert _code(r) == 0
    while r.ctl.clock() <= r.ctl._speed_blank_until["turbo1"]:
        _play(r, "turbo1", [80.0])
    _play(r, "turbo1", [80.0] * 8)
    assert _play(r, "turbo1", _ramp(80.0, 5.0, 5) + [5.0] * 3)


# ------------------------------------------------------------------------------- readings that cannot be judged
def test_a_pump_that_is_not_answering_is_not_judged():
    """An RS-485 pump that stops answering keeps its last speed; whatever arrives while the link is
    reported down is not evidence about the rotor."""
    r = _at_high_vac("vc100")
    r.backend.set_fault("rs485_lost_turbo2", True)
    r.tick(3)
    assert not _play(r, "turbo2", _ramp(100.0, 5.0, 5) + [5.0] * 5)
    assert r.ctl._speed_trip is None


def test_the_shimadzu_pumps_cannot_be_watched():
    """The big turbos: contacts only, no speed to compare - and they brake when switched off."""
    r = _at_high_vac("vc100")
    assert not _play(r, "turbo1", _ramp(100.0, 5.0, 5) + [5.0] * 5)
    assert r.ctl._speed_trip is None


@pytest.mark.parametrize("which", CHAMBERS)
def test_null_switches_it_off_and_shows_what_happened_before(which):
    """With both rules off - the behaviour until 2026-11-17, and on 5 Nov: a rotor falls from full
    speed to 5 % with every gauge reading normally, and Auto carries on in Pumping to High Vac with
    the gates open and no error."""
    def off(cfg):
        cfg.thresholds.turbo_speed_drop_pct = None
        cfg.thresholds.turbo_speed_crash_pct = None
    r = _at_high_vac(which, tweak=off)
    r.monitor.enabled = False
    assert not _play(r, _watched(r)[0].id, _ramp(100.0, 5.0, 5) + [5.0] * 5)
    s = r.ctl.snapshot()
    assert not s.error.status and s.current == S.PUMPING_TO_HIGH_VAC
    assert all(s.commands.valves[t.gate_valve] for t in r.turbos)


@pytest.mark.parametrize("which", CHAMBERS)
def test_the_crash_rule_alone_is_the_coarse_trip(which):
    """`turbo_speed_drop_pct: null` leaves 30 % in 5 s: 4 s to trip instead of 2, and 20 % in 3 s passes."""
    def coarse(cfg):
        cfg.thresholds.turbo_speed_drop_pct = None
    r = _at_high_vac(which, tweak=coarse)
    tid = _watched(r)[0].id
    assert not _play(r, tid, _ramp(100.0, 79.0, 3) + [79.0] * 6)
    _play(r, tid, [100.0] * 20)
    took = _play(r, tid, _ramp(100.0, 10.0, 6), until_trip=True)
    assert 2 < took <= 4, took


# ------------------------------------------------------------------------------- Manual / Admin
@pytest.mark.parametrize("mode", [Mode.ADMIN, Mode.MANUAL])
@pytest.mark.parametrize("which", CHAMBERS)
def test_manual_and_admin_only_log_it(which, mode):
    r = Rig(which, mode=mode)
    r.monitor.enabled = False
    r.tick(3)
    tid = _watched(r)[0].id
    before = r.ctl.cmds.copy()
    _play(r, tid, [100.0] * 8)
    assert not _play(r, tid, _ramp(100.0, 10.0, 5) + [10.0] * 4)
    assert r.ctl._speed_trip is None and _code(r) != ERR_TURBO_SPEED_DROP
    lines = _reports(r)
    assert len(lines) == 1 and f"No automatic action in {mode.name.title()} mode" in lines[0], lines
    assert r.ctl.cmds == before
