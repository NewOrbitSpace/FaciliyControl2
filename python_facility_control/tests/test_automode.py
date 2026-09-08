"""Auto-mode state machine tests against the behaviour extracted from Main_V4.4.vi."""
import pytest

from facility_control.model import FacilityState as S, Mode


def test_starts_in_facility_off_everything_off(harness):
    s = harness.step(3)
    assert s.mode == Mode.AUTO
    assert s.current == S.FACILITY_OFF
    assert not any(s.commands.valves.values())
    assert not s.commands.primary and not s.commands.chiller
    assert not any(s.commands.turbo_motor.values())


def test_pump_to_high_vac_full_sequence(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.PUMPING_TO_HIGH_VAC
    # substate 0 -> 1 -> 2 (warm-up is skipped in the VI)
    s = h.step()
    assert s.substate == 1 and s.commands.primary and s.commands.chiller
    s = h.step()
    assert s.substate == 2 and s.commands.valves["bypass"] is True
    assert s.commands.valves["turbo_valve"] is False and s.commands.valves["gate"] is False
    # pump down through the bypass; after 30 s below 0.25 Torr the turbo valve opens and Engage Turbo starts
    s, _ = h.run_until(lambda s: s.current == S.ENGAGE_TURBO, 6000)
    assert s.commands.valves["turbo_valve"] is True
    # Engage Turbo sub-sequence.  A snapshot carries the *next* substate together with the commands
    # computed by the frame that just ran, so output substate k shows frame k-1's commands.  The pressure
    # check (substate 1) may legitimately fail and fall back to Pumping to Rough a few times (the turbo
    # body dumps gas into the foreline when the turbo valve opens) – we assert on the successful pass.
    seen = []
    s, _ = h.run_until(lambda s: (seen.append((s.current, s.substate, s.commands.valves["bypass"], s.commands.valves["gate"],
                                               s.commands.turbo_motor["turbo1"])) or s.current == S.PUMPING_TO_HIGH_VAC), 6000)
    last_start = max(i for i, x in enumerate(seen) if x[0] == S.ENGAGE_TURBO and x[1] == 1)
    engage = [(sub, bp, gv, tm) for (cur, sub, bp, gv, tm) in seen[last_start:] if cur == S.ENGAGE_TURBO]
    assert [e[0] for e in engage] == [1, 2, 3, 4, 5]
    assert engage[0][1:] == (True, False, False)     # frame 0: bypass open, gate closed
    assert engage[1][1:] == (True, False, False)     # frame 1: pressure check passed
    assert engage[2][1:] == (True, False, False)     # frame 2
    assert engage[3][1:] == (False, False, False)    # frame 3: bypass closed
    assert engage[4][1:] == (False, True, False)     # frame 4: gate opened
    assert s.commands.turbo_motor["turbo1"] is True  # frame 5: motor on -> Pumping to High Vac
    assert s.commands.valves["gate"] and s.commands.valves["turbo_valve"] and not s.commands.valves["bypass"]
    assert "Pressure low enough to turn on turbo" in "\n".join(s.event_log)
    # reaches speed and high vacuum
    s, _ = h.run_until(lambda s: s.turbos["turbo1"].speed_pct > 95 and s.inputs.pressures_torr["wrg"] < 1e-4, 6000)
    assert s.turbos["turbo1"].status.label == "Turbo Speed Reached"
    assert s.error.status is False


def test_engage_turbo_backs_off_when_pressure_too_high(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, _ = h.run_until(lambda s: s.current == S.ENGAGE_TURBO and s.substate == 1, 6000)
    # make the WRG read too high right at the check
    h.backend.set_fault("gauge_fault_wrg", False)
    h.backend.state.p_chamber = 5.0
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH
    assert "Pressure NOT low enough" in "\n".join(s.event_log)


def test_pump_to_rough_only_keeps_turbo_valve_closed(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    s, _ = h.run_until(lambda s: s.elapsed_below_threshold_s > 35, 8000)
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.PUMPING_TO_ROUGH
    assert s.commands.valves["turbo_valve"] is False     # (tgt == Rough) blocks the turbo valve
    assert s.commands.valves["bypass"] is True and s.commands.primary


def test_error_in_auto_forces_facility_off_and_logs(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    s, _ = h.run_until(lambda s: s.inputs.valve_reads["bypass"], 300)      # bypass open and confirmed
    h.backend.set_fault("stuck_bypass", True)                               # it will now refuse to close
    h.ctl.request_auto_button("shut_off")
    s, _ = h.run_until(lambda s: s.error.status, 50)
    assert s.error.code == 5002 and "Bypass Valve" in s.error.source
    s = h.step(2)
    assert s.current == S.FACILITY_OFF and s.target == S.FACILITY_OFF
    assert not s.commands.primary and not s.commands.valves["bypass"]
    assert any("Shutt down due to error" in l for l in s.event_log)
    # the fault persists (valve still open): clearing re-raises the error next iteration
    h.ctl.request_clear_error()
    s = h.step(2)
    assert s.error.code == 5002
    # fix the valve, clear -> error gone, Auto usable again
    h.backend.set_fault("stuck_bypass", False)
    h.step(5)
    h.ctl.request_clear_error()
    s = h.step(3)
    assert s.error.status is False
    h.ctl.request_auto_button("pump_to_rough")
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH


def test_high_pressure_turbo_shutoff(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 6000)
    assert s.commands.turbo_motor["turbo1"]
    h.backend.state.p_chamber = 14.0                       # gas burst: Turbo Convectron rises >= 5 Torr
    s, _ = h.run_until(lambda s: s.commands.turbo_motor["turbo1"] is False, 50)
    assert s.commands.turbo_motor["turbo1"] is False
    assert any("High pressure turbo shuttoff" in l for l in s.event_log)


def test_shutdown_from_high_vac_goes_through_disengage_and_turbo_slowing(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC and s.turbos["turbo1"].speed_pct > 95, 8000)
    h.ctl.request_auto_button("shutdown")
    s = h.step()
    assert s.current == S.DISENGAGE_TURBO and s.target == S.FACILITY_OFF
    assert s.commands.turbo_motor["turbo1"] is False      # motor off the moment the button is pressed
    s = h.step()
    assert s.commands.valves["gate"] is False             # Disengage frame closes the gate
    s, _ = h.run_until(lambda s: s.current == S.TURBO_SLOWING, 300)
    s = h.step()
    assert s.commands.valves["turbo_valve"] is True and s.commands.primary and s.commands.chiller
    s, _ = h.run_until(lambda s: s.current == S.FACILITY_OFF, 6000)
    s = h.step()
    assert not s.commands.primary and not any(s.commands.valves.values())


def test_vent_from_high_vac_keeps_turbo_running_then_vents_10_minutes(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC and s.turbos["turbo1"].speed_pct > 95, 8000)
    h.ctl.request_auto_button("vent")
    s = h.step()
    assert s.current == S.DISENGAGE_TURBO and s.target == S.VENTING
    assert s.commands.turbo_motor["turbo1"] is True          # 'Vent' is not one of the motor-off buttons
    s, _ = h.run_until(lambda s: s.current == S.VENTING, 300)
    s = h.step()
    assert s.commands.valves["vent"] is True and s.commands.valves["gate"] is False
    assert s.commands.valves["turbo_valve"] is True           # turbo still spinning -> keep pumping the body
    s, _ = h.run_until(lambda s: s.inputs.pressures_torr["wrg"] > 700, 8000)
    # after 10 min the vent valve closes but we stay in Venting (target 8)
    s, _ = h.run_until(lambda s: s.commands.valves["vent"] is False, 20000)
    assert s.current == S.VENTING


def test_overnight_pump_waits_for_engage_time(harness):
    h = harness
    h.step(2)
    h.ctl.request_set_engage_time(h.ctl.clock() + 3600)
    h.ctl.request_auto_button("overnight_pump")
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.OVERNIGHT_PUMP
    s, _ = h.run_until(lambda s: s.current == S.OVERNIGHT_PUMP, 8000)
    s = h.step()
    assert not s.commands.primary and not s.commands.valves["bypass"]
    h.ctl.request_set_engage_time(h.ctl.clock() - 1)
    s = h.step(2)
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.PUMPING_TO_HIGH_VAC
    assert any("Timestamp reached" in l for l in s.event_log)
