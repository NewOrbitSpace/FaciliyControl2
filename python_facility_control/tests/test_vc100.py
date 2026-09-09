"""Medium chamber (VC100_Facility_Control_V1.0.vi) – profile, pinout and the Auto rules that differ
from the small chamber.  Everything asserted here was read out of the VI (docs/VC100_REFERENCE.md)."""
import time

import pytest

from conftest import Harness
from test_nidaqmx_backend import FakeTask, fake_daq  # noqa: F401  (fixture reused)
from facility_control.config import CONFIG_DIR, build_config, load_config
from facility_control.model import Commands, FacilityState as S, Mode, TurboStatus
from facility_control.units import TORR_PER_MBAR

VC100 = str(CONFIG_DIR / "facility_vc100.yaml")


@pytest.fixture
def vcfg():
    c = load_config(VC100)
    c.simulation.time_scale = 200.0
    return c


@pytest.fixture
def vh(vcfg):
    return Harness(vcfg)


def _start(h, button="pump_to_high_vac"):
    h.dialogs.answers.put(True)            # "Is the manual vent valve closed?" -> Yes
    h.step(2)
    h.ctl.request_auto_button(button)


def _to_high_vac(h):
    _start(h)
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 20000)
    s, _ = h.run_until(lambda s: all(tv.status == TurboStatus.SPEED_REACHED for tv in s.turbos.values()), 20000)
    return s


# ---------------------------------------------------------------------------- profile / pinout
def test_pinout_matches_the_vi(vcfg):
    """Chassis cDAQ3; the VI's channel map, line by line."""
    cfg = vcfg
    assert cfg.daq_name == "cDAQ3" and cfg.phys("Mod8/ai0") == "cDAQ3Mod8/ai0"
    # AI (NI-9205 slot 8): task order in the VI = ai0 WRG, ai1 foreline, ai2/3/4 turbo gauges, ai7 compressor, ai5/6 speeds
    assert [g.channel for g in cfg.gauges] == ["Mod8/ai0", "Mod8/ai1", "Mod8/ai2", "Mod8/ai3", "Mod8/ai4"]
    assert cfg.compressor_ai == "Mod8/ai7" and cfg.compressor_scale == 2.0
    assert cfg.turbo("turbo2").params["speed_ai"] == "Mod8/ai5" and cfg.turbo("turbo3").params["speed_ai"] == "Mod8/ai6"
    assert cfg.ai_terminal_config == "differential"
    # DO valves slot 1 / DI reads slot 2, line0..7 = bypass, vent, turbo1-3 valves, gate1-3
    order = ["bypass", "vent", "turbo1_valve", "turbo2_valve", "turbo3_valve", "gate1", "gate2", "gate3"]
    assert cfg.valve_ids == order
    for i, vid in enumerate(order):
        assert cfg.valves[vid].cmd == f"Mod1/port0/line{i}" and cfg.valves[vid].read == f"Mod2/port0/line{i}"
    assert (cfg.primary.cmd, cfg.primary.read) == ("Mod4/port0/line0", "Mod2/port0/line8")
    assert (cfg.chiller.cmd, cfg.chiller.read) == ("Mod4/port0/line1", "Mod2/port0/line9")
    # Turbo 1 contacts: DI line10..15 Rotating, Accelerating, At Speed, Breaking, Alarm, Warning; DO line0..2
    p1 = cfg.turbo("turbo1").params
    assert [p1[k] for k in ("rotating_di", "accelerating_di", "at_speed_di", "braking_di", "alarm_di", "warning_di")] == \
        [f"Mod2/port0/line{n}" for n in range(10, 16)]
    assert (p1["motor_do"], p1["standby_do"], p1["error_ack_do"]) == ("Mod3/port0/line0", "Mod3/port0/line1", "Mod3/port0/line2")
    p2, p3 = cfg.turbo("turbo2").params, cfg.turbo("turbo3").params
    assert (p2["motor_do"], p2["standby_do"], p2["error_ack_do"]) == ("Mod3/port0/line3", "Mod3/port0/line4", "Mod3/port0/line5")
    assert (p3["motor_do"], p3["standby_do"]) == ("Mod3/port0/line6", "Mod3/port0/line7") and "error_ack_do" not in p3
    assert (p2["error_di"], p3["error_di"]) == ("Mod2/port0/line16", "Mod2/port0/line17")
    # no line is used twice
    from tools.daq_check import channel_inventory
    inv = channel_inventory(cfg)
    phys = [p for _, p, _ in inv]
    assert len(phys) == len(set(phys)) == 44        # 16 valve lines + 4 pump lines + 6 AI + 18 turbo lines


def test_constants_match_the_vi(vcfg):
    th, tm, am = vcfg.thresholds, vcfg.timings, vcfg.auto_mode
    assert th.turbo_on_threshold_torr == pytest.approx(0.25 * TORR_PER_MBAR)
    assert th.turbo_high_pressure_shutoff_torr == pytest.approx(5.0 * TORR_PER_MBAR)
    assert (th.turbo_speed_reached_pct, th.turbo_slow_speed_pct, th.turbo_spinning_pct) == (95.0, 5.0, 15.0)
    assert th.slowing_pct == 50.0 and th.turbo_on_wiggle_room_multiplier == 5.0
    assert th.compressor_min_bar == 5.0 and th.compressor_low_time_s == 5.0
    assert th.bypass_first_above_torr is None and th.overnight_skip_below_torr is None     # the VI's own order
    assert (tm.primary_to_bypass_gap_s, tm.vent_duration_s, tm.disengage_wait_s) == (20.0, 900.0, 1.0)
    assert (tm.valve_settle_ms, tm.bypass_settle_ms, tm.gate_settle_ms) == (1000, 2000, 9000)
    assert (tm.chiller_settle_ms, tm.primary_settle_ms, tm.primary_settle_off_ms) == (10000, 10000, 0)
    assert all([am.overnight_backing_while_spinning, am.turbo_valve_gauge_rule, am.standby_when_gate_closed,
                am.shut_off_spins_down_first, am.vent_closes_on_shutdown, am.venting_buttons_stop_turbos])
    assert vcfg.pressure_unit_default == "mbar"


def test_gauge_formulas_are_the_vi_formula_nodes():
    from facility_control.gauges import v_to_torr
    mbar = lambda f, v: v_to_torr(f, v) / TORR_PER_MBAR
    assert mbar("edwards_wrg_mbar", 6.8) == pytest.approx(1.0)                 # 10**((V-6.8)/0.6)
    assert mbar("edwards_wrg_mbar", 5.0) == pytest.approx(10 ** (-3))
    assert mbar("edwards_apg_mbar", 6.143) == pytest.approx(1.0)               # 10**((V-6.143)/1.286)
    assert mbar("edwards_apg_mbar", 6.143 + 1.286) == pytest.approx(10.0)


def test_mbar_keys_and_unknown_modes_are_checked():
    import yaml
    raw = yaml.safe_load(open(VC100))
    raw["thresholds"]["turbo_on_threshold_torr"] = 0.2
    with pytest.raises(ValueError, match="not both"):
        build_config(raw)
    raw = yaml.safe_load(open(VC100))
    raw["turbos"][0]["control_mode"] = "leybold_magic"
    with pytest.raises(ValueError, match="known modes"):
        build_config(raw)
    raw = yaml.safe_load(open(VC100))
    del raw["turbos"][0]["shimadzu_contacts"]["alarm_di"]
    with pytest.raises(ValueError, match="missing"):
        build_config(raw)
    raw = yaml.safe_load(open(VC100))
    raw["auto_buttons"][0].append({"label": "x", "action": "fly_away"})
    with pytest.raises(ValueError, match="unknown action"):
        build_config(raw)


# ---------------------------------------------------------------------------- Auto sequence with three turbos
def test_pump_down_engages_all_three_turbos(vh):
    s = _to_high_vac(vh)
    assert s.current == S.PUMPING_TO_HIGH_VAC and s.target == S.PUMPING_TO_HIGH_VAC
    for t in ("turbo1", "turbo2", "turbo3"):
        assert s.commands.turbo_motor[t] and s.commands.valves[f"{t}_valve"] and s.commands.valves[f"gate{t[-1]}"]
        assert not s.commands.turbo_standby[t]          # gate open -> no standby (standby_when_gate_closed)
    assert s.commands.primary and s.commands.chiller and not s.commands.valves["bypass"]
    log = "\n".join(s.event_log)
    assert "Starting the primary pump; the bypass valve opens in 20 s" in log       # VI: 20 s warm-up, then bypass
    assert log.index("Started Primary Pump") < log.index("Opened Bypass Valve")
    # the contact turbo has no speed but reports its status through the contacts
    t1 = s.turbos["turbo1"]
    assert not t1.has_speed and t1.contacts["at_speed"] and t1.contacts["rotating"] and t1.status == TurboStatus.SPEED_REACHED
    assert s.turbos["turbo2"].has_speed and s.turbos["turbo2"].speed_pct > 95


def test_rough_turbo_valves_open_early_when_chamber_below_turbo_body(vh):
    """VC100 For-loop: TV_i := 30 s done ∨ TV_i_prev ∨ (bypass ∧ WRG < turbo gauge_i)."""
    _start(vh)
    s, _ = vh.run_until(lambda s: s.commands.valves["bypass"], 5000)
    assert not any(s.commands.valves[f"turbo{i}_valve"] for i in (1, 2, 3))     # bodies still at atmosphere
    s, _ = vh.run_until(lambda s: s.commands.valves["turbo1_valve"], 5000)
    wrg = s.inputs.pressures_torr["wrg"]
    assert s.current == S.PUMPING_TO_ROUGH and s.elapsed_below_threshold_s < 30      # opened before the 30 s wait
    assert wrg < s.inputs.pressures_torr["turbo1_gauge"] or wrg < vh.cfg.thresholds.turbo_on_threshold_torr


def test_shut_off_once_rough_from_facility_off(vh):
    _start(vh, "shut_off_once_rough")
    s = vh.step(3)                        # vent-valve question (auto-answered), Facility Off frame, Rough frame
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.FACILITY_OFF and s.commands.primary
    s, _ = vh.run_until(lambda s: s.current == S.FACILITY_OFF, 20000)
    s = vh.step()                          # the Facility Off frame switches everything off
    assert not s.commands.primary and not any(s.commands.valves.values())
    assert any("Rough vacuum reached – shutting off" in l for l in s.event_log)


def test_shutdown_spins_down_with_gates_closed_and_standby_rule(vh):
    s = _to_high_vac(vh)
    vh.ctl.request_auto_button("shutdown")
    s = vh.step()
    assert s.current == S.DISENGAGE_TURBO and s.target == S.FACILITY_OFF
    assert not any(s.commands.turbo_motor.values())          # motors off in the same iteration (VI)
    s = vh.step()                                             # Disengage frame: gates close at once
    assert not any(s.commands.valves[f"gate{i}"] for i in (1, 2, 3))
    s, _ = vh.run_until(lambda s: s.current == S.TURBO_SLOWING, 500)
    s = vh.step()
    assert s.commands.primary and s.commands.chiller and not s.commands.valves["vent"]
    # 'Pump to High Vac' while slowing: motors back on with the gates closed -> Standby line set (VC100 rule)
    vh.ctl.request_auto_button("pump_to_high_vac")
    s = vh.step(2)                                            # vent-valve question, then the button
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.PUMPING_TO_HIGH_VAC
    assert all(s.commands.turbo_motor.values()) and all(s.commands.turbo_standby.values())
    s, _ = vh.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 20000)
    assert not any(s.commands.turbo_standby.values())               # gates open again


def test_turbo_slowing_ends_in_facility_off_at_slowing_threshold(vh):
    s = _to_high_vac(vh)
    vh.ctl.request_auto_button("shutdown")
    s, _ = vh.run_until(lambda s: s.current == S.TURBO_SLOWING, 500)
    s, n = vh.run_until(lambda s: s.current == S.FACILITY_OFF, 60000)
    assert s.turbos["turbo2"].speed_pct <= 50.0 and not s.turbos["turbo1"].contacts["rotating"]
    assert not any(v for k, v in s.commands.valves.items() if k.endswith("_valve"))   # closed with the last slowing frame
    s = vh.step()
    assert not s.commands.primary


def test_overnight_keeps_backing_while_turbos_spin(vh):
    s = _to_high_vac(vh)
    vh.ctl.request_auto_button("overnight_pump")
    s, _ = vh.run_until(lambda s: s.current == S.OVERNIGHT_PUMP, 500)
    s = vh.step()
    # VC100: primary + chiller stay on and the turbo valves of spinning turbos stay open
    assert s.commands.primary and s.commands.chiller
    assert s.commands.valves["turbo2_valve"] and s.commands.valves["turbo3_valve"] and s.commands.valves["turbo1_valve"]
    assert not any(s.commands.turbo_motor.values()) and not s.commands.valves["bypass"]
    s, _ = vh.run_until(lambda s: not s.commands.primary, 60000)
    assert s.current == S.OVERNIGHT_PUMP and s.turbos["turbo2"].speed_pct <= 50.0
    assert not s.commands.valves["turbo2_valve"] and not s.commands.chiller


def test_venting_shutdown_now_and_after_vent(vh):
    s = _to_high_vac(vh)
    vh.ctl.request_auto_button("vent")
    s, _ = vh.run_until(lambda s: s.current == S.VENTING, 500)
    s = vh.step()
    assert s.commands.valves["vent"] and s.target == S.VENTING and all(s.commands.turbo_motor.values())
    # 'Shutdown After Vent': target Facility Off, vent goes on, turbos are stopped
    vh.ctl.request_auto_button("shutdown_after_vent")
    s = vh.step()
    assert s.current == S.VENTING and s.target == S.FACILITY_OFF and s.commands.valves["vent"]
    assert not any(s.commands.turbo_motor.values())
    # 'Shutdown Now': vent closes at once, turbos slow down, then off
    vh.ctl.request_auto_button("shut_off")
    s = vh.step()
    assert s.current == S.TURBO_SLOWING and s.target == S.FACILITY_OFF and not s.commands.valves["vent"]
    s, _ = vh.run_until(lambda s: s.current == S.FACILITY_OFF, 60000)
    assert not s.commands.valves["vent"]


def test_vent_valve_stays_15_minutes(vh):
    _start(vh, "vent")
    s = vh.step()
    assert s.current == S.VENTING and s.target == S.VENT_AND_SHUTDOWN
    t0 = vh.backend._sim_time
    s, _ = vh.run_until(lambda s: not s.commands.valves["vent"], 20000)
    assert vh.backend._sim_time - t0 >= 900.0 * 0.95
    assert s.current == S.FACILITY_OFF


def test_vent_from_turbo_slowing_page(vh):
    s = _to_high_vac(vh)
    vh.ctl.request_auto_button("shutdown")
    s, _ = vh.run_until(lambda s: s.current == S.TURBO_SLOWING, 500)
    vh.ctl.request_auto_button("vent")
    s = vh.step()
    assert s.current == S.VENTING and s.target == S.FACILITY_OFF


# ---------------------------------------------------------------------------- status words, errors
def test_contact_turbo_alarm_and_warning(vh):
    s = _to_high_vac(vh)
    vh.backend.set_fault("turbo_warning", True)
    s = vh.step()
    assert s.turbos["turbo1"].status == TurboStatus.WARNING
    assert s.error.code == 5005 and s.error.source == "Turbo 1 Warning"          # VI: 5005 'Turbo1 Warning'
    s = vh.step(2)
    assert s.current == S.FACILITY_OFF                                           # 5000..5011 stop Auto mode
    vh.backend.set_fault("turbo_warning", False)
    vh.backend.set_fault("turbo_error", True)                                    # Alarm contact
    s = vh.step()
    assert s.turbos["turbo1"].status == TurboStatus.ERROR and s.error.source == "Turbo 1 Alarm"


def test_contact_warning_can_be_display_only(vcfg):
    vcfg.turbo("turbo1").params["warning_is_error"] = False
    h = Harness(vcfg)
    _to_high_vac(h)
    h.backend.set_fault("turbo_warning", True)
    s = h.step(2)
    assert s.turbos["turbo1"].status == TurboStatus.WARNING and s.error.code == 0 and s.current == S.PUMPING_TO_HIGH_VAC


def test_hipace_error_is_not_gated_on_the_chiller(vh):
    h = Harness(vh.cfg, mode=Mode.ADMIN)
    h.step(2)
    assert not h.ctl.cmds.chiller
    h.backend.set_fault("turbo_error_turbo2", True)
    s = h.step()
    assert s.turbos["turbo2"].status == TurboStatus.ERROR and s.turbos["turbo1"].status == TurboStatus.OFF
    assert s.error.code == 5007 and s.error.source == "Turbo 2 Error."         # VI: 5007 'Turbo 2 Error.'


def test_hipace_standby_status_needs_40_percent(vh):
    """VC100: 'In Standby' is shown only when standby is commanded AND speed > 40 %."""
    h = Harness(vh.cfg, mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(0)                       # Turn on Chiller? Yes
    h.ctl.request_user_cmd("chiller"); h.step()
    h.dialogs.answers.put(0)                       # Start Turbo 2? Yes
    h.ctl.request_user_cmd("turbo:turbo2"); s = h.step(2)
    assert s.turbos["turbo2"].status == TurboStatus.SPINNING_UP
    h.dialogs.answers.put(2)                       # Stop Turbo 2? -> Activate Standby
    h.ctl.request_user_cmd("turbo:turbo2"); s = h.step(2)
    assert s.commands.turbo_standby["turbo2"]
    if s.turbos["turbo2"].speed_pct <= 40.0:
        assert s.turbos["turbo2"].status == TurboStatus.SPINNING_UP       # speed still below 40 %
    s, _ = h.run_until(lambda s: s.turbos["turbo2"].speed_pct > 40.0, 5000)
    assert s.turbos["turbo2"].status == TurboStatus.STANDBY


def test_compressor_low_for_5s_stops_auto(vh):
    s = _to_high_vac(vh)
    vh.backend.set_fault("compressor_low", True)
    s = vh.step()
    assert s.compressor_bar < 5.0 and s.error.code == 0                    # not yet: 5 s dwell
    vh.advance_plant(0.03)                                                 # (x200 -> 6 s of plant time)
    s, _ = vh.run_until(lambda s: s.error.code != 0, 500)
    assert s.error.code == 5011 and "Compressor Pressure Below 5 Bar!" in s.error.source
    s = vh.step(2)
    assert s.current == S.FACILITY_OFF


def test_primary_conflict_is_checked_on_vc100(vh):
    h = Harness(vh.cfg, mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("primary_fault", True)
    h.dialogs.answers.put(0)
    h.ctl.request_user_cmd("primary"); s = h.step(3)
    assert s.error.code == 5000 and "Primary pump" in s.error.source


# ---------------------------------------------------------------------------- settle waits (command frame)
def test_vc100_settle_waits(vh):
    h = Harness(vh.cfg, mode=Mode.ADMIN)
    h.step(2)
    seen = []
    orig = h.ctl._freeze
    h.ctl._freeze = lambda seconds, reason: (seen.append((round(seconds, 3), reason)), orig(seconds, reason))
    h.dialogs.answers.put(0); h.ctl.request_user_cmd("primary"); h.step()
    assert seen[-1] == (10.0, "Primary Pump starting")
    h.dialogs.answers.put(0); h.ctl.request_user_cmd("valve:bypass"); h.step()
    assert seen[-1] == (2.0, "Bypass Valve moving")
    h.dialogs.answers.put(0); h.ctl.request_user_cmd("valve:vent"); h.step()
    assert seen[-1] == (1.0, "Vent Valve moving")
    h.dialogs.answers.put(0); h.ctl.request_user_cmd("valve:gate2"); h.step()
    assert seen[-1] == (9.0, "Gate 2 Valve moving")
    n = len(seen)
    h.dialogs.answers.put(0); h.ctl.request_user_cmd("primary"); h.step()          # switching OFF: no wait
    assert len(seen) == n


def test_reset_turbos_pulses_the_reset_lines(vh):
    h = Harness(vh.cfg, mode=Mode.ADMIN)
    h.step(2)
    seen = []
    orig = h.backend.turbo_error_ack
    h.backend.turbo_error_ack = lambda tid, level: (seen.append((tid, level)), orig(tid, level))
    h.ctl.request_turbo_error_ack(None)
    h.tick()
    # Turbo 1 (Shimadzu Reset) and Turbo 2 (HiPace Reset) have a line, Turbo 3 has none in the VI –
    # turbo3 gets the pulse call too (its backend has no ack task and ignores it), so no line is left out
    assert ("turbo1", True) in seen and ("turbo2", True) in seen and ("turbo1", False) in seen


def test_admin_dialogs_name_the_turbo(vh):
    h = Harness(vh.cfg, mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(1)
    h.ctl.request_user_cmd("turbo:turbo3"); h.step()
    assert h.dialogs.log[-1] == ("two", "Start Turbo 3?")


# ---------------------------------------------------------------------------- DAQ backend against the fake driver
def test_vc100_daq_tasks(vcfg, fake_daq):
    b = fake_daq.NiDaqmxBackend(vcfg)
    b.open()
    t = FakeTask.registry
    assert [c.phys for c in t["valve_cmd"].channels] == [f"cDAQ3Mod1/port0/line{i}" for i in range(8)]
    assert [c.phys for c in t["valve_read"].channels] == [f"cDAQ3Mod2/port0/line{i}" for i in range(8)]
    assert t["primary_cmd"].channels[0].phys == "cDAQ3Mod4/port0/line0" and t["primary_read"].channels[0].phys == "cDAQ3Mod2/port0/line8"
    assert t["chiller_cmd"].channels[0].phys == "cDAQ3Mod4/port0/line1" and t["chiller_read"].channels[0].phys == "cDAQ3Mod2/port0/line9"
    ai = t["analog_in"]
    assert [c.phys for c in ai.channels] == ["cDAQ3Mod8/ai0", "cDAQ3Mod8/ai1", "cDAQ3Mod8/ai2", "cDAQ3Mod8/ai3",
                                             "cDAQ3Mod8/ai4", "cDAQ3Mod8/ai7", "cDAQ3Mod8/ai5", "cDAQ3Mod8/ai6"]
    assert ai.timing.cfg == (1000.0, "finite", 25)
    from nidaqmx.constants import TerminalConfiguration
    assert all(c.kw["terminal_config"] == TerminalConfiguration.DIFF for c in ai.channels)
    assert ai.channels[6].kw["min_val"] == -10.0 and ai.channels[7].kw["max_val"] == 10.0      # speeds ±10 V
    assert ai.channels[0].kw["min_val"] == 0.0 and ai.channels[0].kw["max_val"] == 10.0        # gauges 0..10 V
    assert [c.phys for c in t["turbo1_read"].channels] == [f"cDAQ3Mod2/port0/line{i}" for i in range(10, 16)]
    assert [c.phys for c in t["turbo1_cmd"].channels] == ["cDAQ3Mod3/port0/line0", "cDAQ3Mod3/port0/line1"]
    assert t["turbo1_ack"].channels[0].phys == "cDAQ3Mod3/port0/line2"
    assert [c.phys for c in t["turbo2_cmd"].channels] == ["cDAQ3Mod3/port0/line3", "cDAQ3Mod3/port0/line4"]
    assert t["turbo2_ack"].channels[0].phys == "cDAQ3Mod3/port0/line5"
    assert [c.phys for c in t["turbo3_cmd"].channels] == ["cDAQ3Mod3/port0/line6", "cDAQ3Mod3/port0/line7"]
    assert "turbo3_ack" not in t
    assert t["turbo2_read"].channels[0].phys == "cDAQ3Mod2/port0/line16" and t["turbo3_read"].channels[0].phys == "cDAQ3Mod2/port0/line17"
    # readings: contacts de-inverted (alarm/warning NC), compressor V/5*10, mBar formulas
    from facility_control.gauges import torr_to_v
    FakeTask.di_values.update({"cDAQ3Mod2/port0/line10": True, "cDAQ3Mod2/port0/line12": True,
                               "cDAQ3Mod2/port0/line14": True, "cDAQ3Mod2/port0/line15": True,    # alarm/warning lines high = healthy
                               "cDAQ3Mod2/port0/line16": True, "cDAQ3Mod2/port0/line17": False})   # T2 healthy, T3 error
    FakeTask.ai_volts.update({"cDAQ3Mod8/ai0": 6.8, "cDAQ3Mod8/ai1": 6.143, "cDAQ3Mod8/ai7": 3.0,
                              "cDAQ3Mod8/ai5": 9.5, "cDAQ3Mod8/ai6": 0.3})
    inp = b.read()
    assert not inp.daq_error
    assert inp.pressures_torr["wrg"] / TORR_PER_MBAR == pytest.approx(1.0)
    assert inp.pressures_torr["fore"] / TORR_PER_MBAR == pytest.approx(1.0)
    assert inp.compressor_bar == pytest.approx(6.0)
    t1 = inp.turbos["turbo1"]
    assert t1.contacts == {"rotating": True, "accelerating": False, "at_speed": True, "braking": False, "alarm": False, "warning": False}
    assert t1.error is False and t1.still_spinning is True
    assert inp.turbos["turbo2"].speed_pct == pytest.approx(95.0) and inp.turbos["turbo2"].error is False
    assert inp.turbos["turbo3"].speed_pct == pytest.approx(3.0) and inp.turbos["turbo3"].error is True
    # alarm contact opens -> alarm
    FakeTask.di_values["cDAQ3Mod2/port0/line14"] = False
    assert b.read().turbos["turbo1"].contacts["alarm"] is True
    # writes: 8 valve lines, motor+standby per turbo, reset via the ack task
    cmds = Commands.all_off(vcfg.valve_ids, vcfg.turbo_ids)
    cmds.turbo_motor["turbo1"] = True
    cmds.turbo_standby["turbo1"] = True
    b.write(cmds)
    assert t["turbo1_cmd"].writes[-1] == [True, True] and t["turbo3_cmd"].writes[-1] == [False, False]
    b.turbo_error_ack("turbo1", True)
    assert t["turbo1_ack"].writes[-1] is True
    b.turbo_error_ack("turbo3", True)                # no line: silently ignored
