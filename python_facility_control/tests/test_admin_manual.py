"""Admin mode (dialog-confirmed commands, state recognition) and Manual mode interlocks."""
import pytest

from facility_control.automode import SUB_ROUGH_WAIT
from facility_control.model import Commands, FacilityState as S, Mode
from tests.conftest import Harness


@pytest.fixture
def admin(cfg):
    return Harness(cfg, mode=Mode.ADMIN)


def test_admin_valve_command_asks_and_executes(admin):
    h = admin
    h.step(2)
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("valve:bypass")
    s = h.step()
    assert s.commands.valves["bypass"] is True
    assert h.dialogs.log[-1] == ("two", "Open Bypass Valve?")
    h.dialogs.answers.put(False)          # answer 'No' -> unchanged
    h.ctl.request_user_cmd("valve:bypass")
    s = h.step()
    assert s.commands.valves["bypass"] is True
    assert h.dialogs.log[-1] == ("two", "Close Bypass Valve?")


def test_admin_turbo_three_button_dialogs(admin):
    h = admin
    h.step(2)
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("turbo:turbo1")
    s = h.step()
    assert s.commands.turbo_motor["turbo1"] is True
    assert h.dialogs.log[-1] == ("two", "Start Turbo Pump?")
    h.dialogs.answers.put(2)              # 'Activate Standby'
    h.ctl.request_user_cmd("turbo:turbo1")
    s = h.step()
    assert s.commands.turbo_motor["turbo1"] and s.commands.turbo_standby["turbo1"]
    h.dialogs.answers.put(0)              # 'Yes, Spin Up'
    h.ctl.request_user_cmd("turbo:turbo1")
    s = h.step()
    assert s.commands.turbo_motor["turbo1"] and not s.commands.turbo_standby["turbo1"]
    h.dialogs.answers.put(0)              # 'Yes' (stop)
    h.ctl.request_user_cmd("turbo:turbo1")
    s = h.step()
    assert not s.commands.turbo_motor["turbo1"]


def test_state_recognition_tree(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    rec = h.ctl.recognise_state
    assert rec(c)[2] == "Facility off"
    c.valves["vent"] = True
    assert rec(c) is None
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.primary = True
    assert rec(c)[:3] == (S.PUMPING_TO_ROUGH, S.PUMPING_TO_HIGH_VAC, "Pumping to Rough")
    c.valves["bypass"] = True
    assert rec(c)[2] == "Pumping to Rough"
    c.valves["turbo_valve"] = True
    assert rec(c) is None                                          # turbo valve open with motor off -> not a state
    c.turbo_motor["turbo1"] = True
    c.valves["bypass"] = False
    assert rec(c)[:3] == (S.VENTING, S.VENTING, "Venting")
    c.valves["gate"] = True
    assert rec(c)[:3] == (S.PUMPING_TO_HIGH_VAC, S.PUMPING_TO_HIGH_VAC, "Pumping to High Vac")
    c.valves["bypass"] = True
    assert rec(c) is None


def test_change_to_auto_not_recognised_stays_admin(admin):
    h = admin
    h.step(2)
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("valve:vent")   # vent open, primary off -> not a recognised state
    h.step()
    h.ctl.request_change_to_auto()
    s = h.step()
    assert s.mode == Mode.ADMIN
    assert ("info", "Target State Not Recognised! ") in h.dialogs.log


def test_change_to_auto_from_rough_with_bypass_goes_to_the_wait_substate(admin):
    h = admin
    h.step(2)
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("primary"); h.step()
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("valve:bypass"); h.step()
    h.dialogs.answers.put(0)   # 'Yes, Target High Vac'
    h.ctl.request_change_to_auto()
    s = h.step()
    assert s.mode == Mode.AUTO
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.PUMPING_TO_HIGH_VAC
    assert s.substate == SUB_ROUGH_WAIT   # already roughing -> straight to the below-threshold wait
    # the machine carries on pumping and eventually engages the turbo
    s, _ = h.run_until(lambda s: s.current == S.ENGAGE_TURBO, 8000)


def test_admin_mode_button_always_works(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    # +1 for the manual-vent-valve confirmation, and at atmosphere the bypass opens before the pump
    h.step(5)
    assert h.ctl.snapshot().commands.primary is True
    h.ctl.request_admin_mode()
    s = h.step()
    assert s.mode == Mode.ADMIN
    assert s.commands.primary is True   # commands are kept when leaving Auto


def test_manual_mode_interlocks(cfg):
    h = Harness(cfg, mode=Mode.MANUAL)
    h.step(2)
    # turbo cannot start: primary off
    h.ctl.request_user_cmd("turbo:turbo1")
    s = h.step()
    assert not s.commands.turbo_motor["turbo1"]
    assert h.dialogs.log[-1] == ("info", "Please Turn Primary Pump On first")
    # vent may open (everything off)
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("valve:vent"); s = h.step()
    assert s.commands.valves["vent"] is True
    # primary may not start with the vent open
    h.ctl.request_user_cmd("primary"); s = h.step()
    assert not s.commands.primary and h.dialogs.log[-1] == ("info", "Please Close Vent Valve first")
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("valve:vent"); h.step()          # close vent
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("primary"); s = h.step()
    assert s.commands.primary
    # two-relay pump: the interlocks want it actually turning, so let the sequence and spin-up finish
    s, _ = h.run_until(lambda s: s.primary_running, 4000)
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("valve:turbo_valve"); s = h.step()
    assert s.commands.valves["turbo_valve"]
    h.ctl.request_user_cmd("turbo:turbo1"); s = h.step()
    assert h.dialogs.log[-1] == ("info", "Please Turn Chiller On First")
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("chiller"); s = h.step()
    assert s.commands.chiller
    # let the chiller feedback come on, then freeze the plant so injected pressures are what the controller reads
    h.backend.advance(5.0)
    h.backend.sim.time_scale = 0.0
    # turbo blocked: main (turbo convectron) pressure too high
    h.backend.state.turbos["turbo1"].p_body_torr = 0.5
    h.ctl.request_user_cmd("turbo:turbo1"); s = h.step()
    assert h.dialogs.log[-1] == ("info", "Main Facility Pressure Too High")
    # blocked: foreline too high
    h.backend.state.turbos["turbo1"].p_body_torr = 1e-3
    h.backend.state.p_foreline = 5.0
    h.ctl.request_user_cmd("turbo:turbo1"); s = h.step()
    assert h.dialogs.log[-1] == ("info", "Foreline Pressure Too High")
    # everything fine -> the normal confirmation dialog appears and the turbo starts
    h.backend.state.p_foreline = 1e-3
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("turbo:turbo1"); s = h.step()
    assert h.dialogs.log[-1] == ("two", "Start Turbo Pump?") and s.commands.turbo_motor["turbo1"]
    # vent cannot open with the primary on
    h.ctl.request_user_cmd("valve:vent"); s = h.step()
    assert h.dialogs.log[-1] == ("info", "Please Stop Primary Pump First")
    # primary cannot stop while the turbo runs
    h.ctl.request_user_cmd("primary"); s = h.step()
    assert h.dialogs.log[-1] == ("info", "Please Turn Turbo Off First") and s.commands.primary


def test_startup_dialog_offers_auto_or_manual(cfg):
    """The start-up choice is Auto / Manual, not Auto / Admin (2026-09-22).

    Admin has no interlocks, so it is not somewhere to land by default; it stays reachable from the
    panel's Admin Mode button for bring-up and hand recovery."""
    from facility_control.controller import AutoAnswerDialogs, Controller
    from facility_control.hal.sim_backend import SimBackend

    for answer, want in ((0, Mode.AUTO), (1, Mode.MANUAL)):
        b = SimBackend(cfg)
        d = AutoAnswerDialogs()
        d.answers.put(answer)
        ctl = Controller(cfg, b, dialogs=d, sleep=lambda s: None)   # no initial_mode -> INITIALIZE
        b.open()
        ctl.iterate()
        ctl.iterate()
        kinds = [m for _k, m in d.log]
        assert "Select Control Mode" in kinds
        assert ctl.mode is want, f"button {answer} should select {want.name}, got {ctl.mode.name}"
    # and Admin is still reachable afterwards
    ctl.request_admin_mode()
    ctl.iterate()
    assert ctl.mode is Mode.ADMIN
