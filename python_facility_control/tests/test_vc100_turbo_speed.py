"""Pfeiffer HiPace speed modes (VC100, 2026-10-27): Off / Full / Standby / free set value.

Driver ([P:026] rotation speed setting mode + [P:707] set value), the RS-485 poller (write order,
clean-up of the speed mode), the controller (Manual/Admin only, interlocks, Auto resets to full
speed) and the GUI panel.
"""
import os

import pytest

from conftest import Harness
from test_hipace_rs485_facility import _bus, _mgr, _writes
from facility_control.config import CONFIG_DIR, load_config
from facility_control.drivers.pfeiffer_pv import HiPace, PVBus, PVRejected, SimBus, SimTC400
from facility_control.model import Mode, TurboStatus

VC100 = CONFIG_DIR / "facility_vc100.yaml"


def _vc100():
    c = load_config(str(VC100))
    c.simulation.time_scale = 200.0
    return c


# ---------------------------------------------------------------------------- driver + simulator
def test_driver_set_speed_writes_set_value_then_mode_and_sim_follows():
    sim = SimBus({1: SimTC400(1, time_scale=400.0)}, delay_s=0.0)
    bus = PVBus("SIMX", serial_obj=sim)
    bus.gap_s = 0.0
    pump = HiPace(bus, 1)
    pump.control_enabled = True
    pump.take_rs485_control()
    pump.set_speed(60.0)
    params = [w.param for w in _writes(sim)]
    assert params[-2:] == [707, 26]                        # set value first, then the mode
    p = sim.pumps[1].p
    assert p[26] == 1 and p[707] == pytest.approx(60.0)
    pump.start()
    sim.pumps[1].update()
    assert sim.pumps[1].p[308] == int(0.6 * 820)          # the pump's set rotation speed follows [P:707]
    pump.set_standby(True)
    sim.pumps[1].update()
    assert sim.pumps[1].p[308] == int(0.667 * 820)        # standby has priority
    pump.set_standby(False)
    pump.set_speed(None)
    assert p[26] == 0
    with pytest.raises(ValueError):
        pump.set_speed(10.0)


def test_sim_refuses_out_of_range_set_value():
    sim = SimBus({1: SimTC400(1)}, delay_s=0.0)
    bus = PVBus("SIMY", serial_obj=sim)
    bus.gap_s = 0.0
    pump = HiPace(bus, 1)
    pump.control_enabled = True
    pump.take_rs485_control()
    with pytest.raises(PVRejected):
        pump.write(707, 10.0)


# ---------------------------------------------------------------------------- RS-485 poller
def test_poller_applies_set_value_before_start_and_clears_mode_on_stop():
    sim = _bus(2)
    m = _mgr(sim)
    m.poll_once()
    assert all(p.p[26] == 0 for p in sim.pumps.values())   # start-up: speed mode switched off once
    sim.received.clear()
    m.set_commands({"turbo1": True}, {}, {"turbo1": 55.0})
    m.poll_once()
    w = [(t.param, t.data) for t in _writes(sim)]
    order = [n for n, _ in w]
    assert order.index(707) < order.index(26) < order.index(10)   # speed set up before the start
    assert sim.pumps[1].p[707] == pytest.approx(55.0) and sim.pumps[1].p[26] == 1 and sim.pumps[1].p[10]
    assert any("55 %" in e for e in m.take_events())
    # change the set value while running
    m.set_commands({"turbo1": True}, {}, {"turbo1": 80.0})
    m.poll_once()
    assert sim.pumps[1].p[707] == pytest.approx(80.0)
    # standby: speed mode off, standby on
    m.set_commands({"turbo1": True}, {"turbo1": True}, {"turbo1": 80.0})
    m.poll_once()
    assert sim.pumps[1].p[26] == 0 and sim.pumps[1].p[2]
    # back to set value, then stop: [P:010] off is the FIRST write, then the mode is cleared
    m.set_commands({"turbo1": True}, {}, {"turbo1": 40.0})
    m.poll_once()
    sim.received.clear()
    m.set_commands({"turbo1": False}, {}, {})
    m.poll_once()
    order = [t.param for t in _writes(sim)]
    assert order[0] == 10 and 26 in order
    assert not sim.pumps[1].p[10] and sim.pumps[1].p[26] == 0


def test_stop_is_sent_before_a_pending_error_ack():
    sim = _bus(1)
    m = _mgr(sim)
    m.set_commands({"turbo1": True}, {})
    m.poll_once()
    sim.received.clear()
    m.error_ack("turbo1")
    m.set_commands({"turbo1": False}, {})
    m.poll_once()
    order = [t.param for t in _writes(sim)]
    assert order[0] == 10 and 9 in order


# ---------------------------------------------------------------------------- controller
def test_admin_speed_modes_full_cycle():
    h = Harness(_vc100(), mode=Mode.ADMIN)
    h.step(2)
    ctl = h.ctl
    ctl.request_user_cmd("chiller")                      # the chiller contactor powers the turbo controllers
    h.step(2)
    h.backend.set_fault("no_pressure_trip", True)         # spin at atmosphere in the simulator (speed logic only)
    ctl.request_turbo_speed("turbo2", "set", 60)
    s = h.step(2)
    assert s.commands.turbo_motor["turbo2"] and s.commands.speed_set("turbo2") == 60.0
    assert any("Start Turbo 2 at 60 % speed?" in m for _, m in h.dialogs.log)
    h.advance_plant(400)
    s = h.step(1)
    tv = s.turbos["turbo2"]
    assert tv.speed_pct == pytest.approx(60.0, abs=3.0)
    assert tv.status == TurboStatus.SPEED_REACHED and tv.speed_set_pct == 60.0 and tv.speed_settable
    # running: changes apply without a question
    n = len(h.dialogs.log)
    ctl.request_turbo_speed("turbo2", "set", 5)            # clamped to the 20 % minimum
    s = h.step(1)
    assert s.commands.speed_set("turbo2") == 20.0 and len(h.dialogs.log) == n
    s = h.step(1)
    assert s.turbos["turbo2"].status == TurboStatus.SPINNING_DOWN    # slowing to the set value
    ctl.request_turbo_speed("turbo2", "full")
    s = h.step(1)
    assert s.commands.speed_set("turbo2") is None and s.commands.turbo_motor["turbo2"]
    ctl.request_turbo_speed("turbo2", "set", 100)          # 100 % is full speed
    s = h.step(1)
    assert s.commands.speed_set("turbo2") is None
    ctl.request_turbo_speed("turbo2", "set", 70)
    h.step(1)
    ctl.request_turbo_speed("turbo2", "standby")
    s = h.step(1)
    assert s.commands.turbo_standby["turbo2"] and s.commands.speed_set("turbo2") is None
    ctl.request_turbo_speed("turbo2", "set", 70)
    h.step(1)
    ctl.request_turbo_speed("turbo2", "off")
    s = h.step(2)
    assert not s.commands.turbo_motor["turbo2"] and s.commands.speed_set("turbo2") is None
    assert any("Stop Turbo 2?" in m for _, m in h.dialogs.log)
    assert any("Turbo 2 speed set to 70 %" in line for line in s.event_log)


def test_start_answered_no_changes_nothing():
    h = Harness(_vc100(), mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(1)                               # No
    h.ctl.request_turbo_speed("turbo3", "set", 50)
    s = h.step(2)
    assert not s.commands.turbo_motor["turbo3"] and s.commands.speed_set("turbo3") is None


def _old_test_shimadzu_turbo_has_no_speed_control():
    h = Harness(_vc100(), mode=Mode.ADMIN)
    h.step(2)
    h.ctl.request_turbo_speed("turbo1", "set", 50)
    s = h.step(2)
    assert not s.commands.turbo_motor["turbo1"]
    assert any("only available for the Pfeiffer" in m for k, m in h.dialogs.log if k == "info")


def test_manual_mode_start_goes_through_the_interlocks():
    h = Harness(_vc100(), mode=Mode.MANUAL)
    h.step(2)
    h.ctl.request_turbo_speed("turbo2", "set", 50)        # primary pump off -> refused
    s = h.step(2)
    assert not s.commands.turbo_motor["turbo2"]
    assert any(k == "info" for k, _ in h.dialogs.log)


def test_auto_mode_ignores_the_panel_and_clears_set_values():
    h = Harness(_vc100(), mode=Mode.ADMIN)
    h.step(2)
    h.ctl.request_turbo_speed("turbo2", "set", 50)
    s = h.step(2)
    assert s.commands.speed_set("turbo2") == 50.0
    h.ctl.mode = Mode.AUTO
    s = h.step(2)
    assert s.commands.speed_set("turbo2") is None
    h.ctl.request_turbo_speed("turbo5", "set", 50)
    s = h.step(2)
    assert s.commands.speed_set("turbo5") is None and not s.commands.turbo_motor["turbo5"]


def test_profile_speed_range_validation():
    c = _vc100()
    for t in c.turbos:
        if t.speed_settable:
            lo, hi = t.speed_set_range
            assert 20.0 <= lo < hi <= 100.0
    assert not c.turbo("turbo1").speed_settable and c.turbo("turbo2").speed_settable


def test_confirmed_request_skips_the_question_but_not_the_interlocks():
    h = Harness(_vc100(), mode=Mode.ADMIN)
    h.step(2)
    n = len(h.dialogs.log)
    h.ctl.request_turbo_speed("turbo2", "set", 50, confirmed=True)
    s = h.step(2)
    assert s.commands.turbo_motor["turbo2"] and s.commands.speed_set("turbo2") == 50.0
    assert len(h.dialogs.log) == n                            # no Yes/No box
    h.ctl.request_turbo_speed("turbo2", "off", confirmed=True)
    s = h.step(2)
    assert not s.commands.turbo_motor["turbo2"]
    m = Harness(_vc100(), mode=Mode.MANUAL)
    m.step(2)
    m.ctl.request_turbo_speed("turbo2", "full", confirmed=True)   # primary off -> interlock refuses
    s = m.step(2)
    assert not s.commands.turbo_motor["turbo2"] and any(k == "info" for k, _ in m.dialogs.log)


# ---------------------------------------------------------------------------- GUI: speed window
def test_clicking_a_pfeiffer_turbo_opens_its_speed_window():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from facility_control.gui.main_window import MainWindow
    h = Harness(_vc100(), mode=Mode.ADMIN)
    win = MainWindow(h.cfg, h.ctl, sim_backend=h.backend)
    win.show()
    h.step(2)
    win._poll(); app.processEvents()
    n = len(h.dialogs.log)
    win._on_diagram_click("turbo:turbo2")                   # Pfeiffer -> speed window, no Yes/No box
    h.step(1)
    assert "turbo2" in win.speed_dialogs and win.speed_dialogs["turbo2"].isVisible()
    assert len(h.dialogs.log) == n and not h.ctl.snapshot().commands.turbo_motor["turbo2"]
    dlg = win.speed_dialogs["turbo2"]
    assert dlg.buttons["off"].isChecked() and not dlg.apply_btn.isEnabled()
    assert not dlg.slider.isEnabled()                       # slider only with 'Set speed'
    dlg.buttons["set"].click()
    assert dlg.slider.isEnabled()
    dlg.slider.setValue(45)
    assert dlg.spin.value() == 45 and dlg.apply_btn.text() == "Start at 45 %"
    dlg.apply_btn.click()
    h.step(2)
    win._poll(); app.processEvents()
    assert h.ctl.snapshot().commands.speed_set("turbo2") == 45.0
    assert dlg.buttons["set"].isChecked() and dlg.apply_btn.text() == "No change"
    dlg.spin.setValue(70)
    assert dlg.apply_btn.text() == "Change to 70 %"
    dlg.apply_btn.click()
    h.step(1)
    assert h.ctl.snapshot().commands.speed_set("turbo2") == 70.0
    win._poll(); app.processEvents()
    dlg.buttons["standby"].click()
    assert dlg.apply_btn.text() == "Go to standby"
    dlg.apply_btn.click()
    h.step(1)
    assert h.ctl.snapshot().commands.turbo_standby["turbo2"]
    win._poll(); app.processEvents()
    dlg.buttons["off"].click()
    assert dlg.apply_btn.text() == "Stop Turbo 2"
    dlg.apply_btn.click()
    h.step(1)
    assert not h.ctl.snapshot().commands.turbo_motor["turbo2"]
    # a Shimadzu turbo (2026-10-29) gets the same window, without 'Set speed'
    win._on_diagram_click("turbo:turbo1")
    h.step(1)
    assert "turbo1" in win.speed_dialogs and win.speed_dialogs["turbo1"].buttons["set"].isHidden()
    # Auto mode: window controls disabled
    h.ctl.mode = Mode.AUTO
    h.step(1)
    win._poll(); app.processEvents()
    assert not dlg.buttons["full"].isEnabled() and "Auto mode" in dlg.note.text()
    win.close()


# ---------------------------------------------------------------------------- Shimadzu contacts / full speed
def test_contact_summary_words():
    from facility_control.model import contact_summary
    assert contact_summary({}, False) == "Off"
    assert contact_summary({}, True) == "On"                                   # commanded, no contact active
    assert contact_summary({"accelerating": True, "rotating": True}, True, 80.0) == "Accel · <80%"
    assert contact_summary({"braking": True, "rotating": True}, False, 80.0) == "Decel · <80%"
    assert contact_summary({}, True, 80.0) == "<80%"                          # TMP4 running, below 80 %
    assert contact_summary({}, False, 80.0) == "Off"                          # TMP4 stopped
    assert contact_summary({"at_speed": True, "rotating": True}, True, 80.0) == ">80%"
    assert contact_summary({"at_speed": True, "accelerating": True}, True, 80.0) == "Accel · >80%"
    assert contact_summary({"at_speed": True}, True) == "At speed"             # TMP1: rated-speed contact
    assert contact_summary({"rotating": True}, False) == "Rotating"
    assert contact_summary({"alarm": True}, True).startswith("ALARM")


def _auto_high_vac(cfg, max_iter=30000, hv_steps=3000):
    from facility_control.model import FacilityState as S
    h = Harness(cfg)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    in_hv = []
    for _ in range(max_iter):
        s = h.step()
        if s.current == S.PUMPING_TO_HIGH_VAC:
            in_hv.append(s)
            if all(tv.status == TurboStatus.SPEED_REACHED for tv in s.turbos.values()) or len(in_hv) > hv_steps:
                break
    return h, in_hv


def test_auto_high_vac_runs_every_turbo_at_full_speed():
    c = _vc100()
    h, hv = _auto_high_vac(c)
    assert hv, "never reached Pumping to High Vac"
    for s in hv:                                             # never standby / reduced speed in high vac
        assert not any(s.commands.turbo_standby.values())
        assert all(s.commands.speed_set(t) is None for t in c.turbo_ids)
        assert all(s.commands.turbo_motor[t.id] for t in c.turbos if t.in_use)
    last = hv[-1]
    assert all(tv.status == TurboStatus.SPEED_REACHED for tv in last.turbos.values())
    assert not any(tv.not_full_speed for tv in last.turbos.values())
    assert last.turbos["turbo4"].contacts["at_speed"] and not last.turbos["turbo4"].contacts["accelerating"]


def test_turbo_not_at_full_speed_is_reported():
    c = _vc100()
    c.thresholds.turbo_full_speed_timeout_s = 60.0
    c.turbo("turbo4").params["at_speed_pct"] = 101.0          # simulated TMP4 never reports '>80 %'
    h, hv = _auto_high_vac(c, hv_steps=50)
    assert hv
    h.advance_plant(120)
    s = h.step(3)
    assert s.current.name == "PUMPING_TO_HIGH_VAC"             # Auto keeps running
    assert s.turbos["turbo4"].not_full_speed and not s.turbos["turbo2"].not_full_speed
    msg = [l for l in s.event_log if "Turbo 4 NOT at full speed" in l]
    assert len(msg) == 1 and "signals:" in msg[0] and "standby off" in msg[0]


# ---------------------------------------------------------------------------- TMP4 gate sticks (2026-10-28)
def _to_high_vac_with_stuck_gate4(max_iter=40000):
    from facility_control.model import FacilityState as S
    c = _vc100()
    h = Harness(c)
    h.backend.set_fault("stuck_gate4", True)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    for _ in range(max_iter):
        s = h.step()
        if s.current == S.PUMPING_TO_HIGH_VAC and all(
                s.turbos[t].status == TurboStatus.SPEED_REACHED for t in c.turbo_ids if t != "turbo4"):
            return h, s
        assert s.mode == Mode.AUTO and not s.error.status, f"Auto stopped: {s.error.source}"
    raise AssertionError("high vac not reached")


def test_stuck_tmp4_gate_takes_tmp4_out_and_auto_carries_on():
    h, s = _to_high_vac_with_stuck_gate4()
    assert not s.commands.valves["gate4"] and not s.commands.turbo_motor["turbo4"]
    assert all(s.commands.turbo_motor[t] for t in ("turbo1", "turbo2", "turbo3", "turbo5", "turbo6"))
    assert all(s.commands.valves[g] for g in ("gate1", "gate2", "gate3", "gate5", "gate6"))
    assert s.commands.valves["turbo1_valve"]                      # TMP1 (and a coasting TMP4) stay backed
    pops = [m for k, m in h.dialogs.log if k == "info" and "Thor's hammer" in m]
    assert len(pops) == 1 and pops[0].startswith("Unable to open TMP4 gate valve, might need Thor's hammer!")
    assert any("Thor's hammer" in l and "Auto carries on" in l for l in s.event_log)
    assert s.turbos["turbo4"].isolated and not s.turbos["turbo1"].isolated
    # stays out: no retry loop, no second popup
    s = h.step(20)
    assert not s.commands.valves["gate4"] and len([m for k, m in h.dialogs.log if "Thor" in m]) == 1


def test_clear_error_lets_auto_try_tmp4_again():
    h, s = _to_high_vac_with_stuck_gate4()
    h.backend.set_fault("stuck_gate4", False)                      # the hammer worked
    h.ctl.request_clear_error()
    for _ in range(400):
        s = h.step()
        if s.commands.valves["gate4"] and s.commands.turbo_motor["turbo4"] and s.inputs.valve_reads["gate4"]:
            break
    assert s.commands.valves["gate4"] and s.commands.turbo_motor["turbo4"] and not s.turbos["turbo4"].isolated
    assert any("Turbo 4 back in use (Clear Error)" in l for l in s.event_log)
    assert s.mode == Mode.AUTO and not s.error.status


def test_stuck_gate_still_an_error_for_turbos_without_the_option_and_in_manual():
    c = _vc100()
    h = Harness(c, mode=Mode.ADMIN)
    h.backend.set_fault("stuck_gate4", True)
    h.step(2)
    h.ctl.request_user_cmd("valve:gate4")
    s = h.step(4)
    assert s.error.status and "Gate 4" in s.error.source             # Admin/Manual: the usual conflict
    assert not s.turbos["turbo4"].isolated


# ---------------------------------------------------------------------------- RS-485 link handling (2026-10-28)
def test_write_echo_counts_as_alive_and_warning_comes_before_error():
    import time as _t
    from test_hipace_rs485_facility import _turbos
    sim = _bus(1)
    t = _turbos(1)
    t[0].params.update({"comm_warn_s": 0.15, "comm_timeout_s": 0.6})
    m = _mgr(sim, t)
    m.poll_once()
    u = m.units["turbo1"]
    u.last_ok = m.clock() - 100.0
    m.set_commands({"turbo1": True}, {})
    assert m._apply(u)
    assert m.clock() - u.last_ok < 1.0                       # the write echo proved the pump alive
    sim.dead = True
    _t.sleep(0.25)
    ti = m.inputs("turbo1")
    assert ti.comm_warning and ti.comm_ok and not ti.error and ti.comm_silent_s > 0.15
    _t.sleep(0.45)
    ti = m.inputs("turbo1")
    assert not ti.comm_ok and ti.error and "no RS-485 reply" in ti.error_text


def test_poller_asks_the_quietest_pump_first_and_spreads_slow_polls():
    from facility_control.drivers.pfeiffer_pv import parse_telegram
    sim = _bus(4)
    m = _mgr(sim)
    assert len({u.polls % 10 for u in m.units.values()}) == 4    # slow sets in different passes
    now = m.clock()
    for i, tid in enumerate(["turbo1", "turbo2", "turbo3", "turbo4"]):
        m.units[tid].last_ok = now - (1 if tid != "turbo3" else 50)
    sim.received.clear()
    m.poll_once_ordered()
    first_query = next(parse_telegram(x) for x in sim.received if parse_telegram(x).action == 0)
    assert first_query.address == 3


def test_port_is_reopened_when_every_pump_on_it_is_silent():
    import time as _t
    from test_hipace_rs485_facility import _turbos
    from facility_control.hal.hipace_rs485_turbos import RS485Turbos
    sim = _bus(2)
    opened = []

    def factory(*a):
        opened.append(a)
        return sim
    t = _turbos(2)
    for x in t:
        x.params.update({"port_reset_s": 0.2, "comm_timeout_s": 5.0})
    m = RS485Turbos(t, serial_factory=factory)
    m.open(start_threads=False)
    for b in m.buses.values():
        b.gap_s = 0.0
    m.set_commands({"turbo1": True}, {})
    m.poll_once_ordered()
    assert sim.pumps[1].p[10] and len(opened) == 1
    sim.dead = True
    _t.sleep(0.3)
    m.poll_once_ordered()
    assert len(opened) == 2 and any("re-opened" in e for e in m.take_events())
    sim.dead = False
    sim.pumps[1].p[10] = False                                  # the pump lost its state meanwhile
    m.poll_once_ordered()
    assert sim.pumps[1].p[10] and m.inputs("turbo1").comm_ok    # desired state sent again


def _vc100_high_vac():
    from facility_control.model import FacilityState as S
    c = _vc100()
    h = Harness(c)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    for _ in range(40000):
        s = h.step()
        if s.current == S.PUMPING_TO_HIGH_VAC and all(
                tv.status == TurboStatus.SPEED_REACHED for tv in s.turbos.values()):
            return h, s
    raise AssertionError("high vac not reached")


def test_lost_rs485_link_takes_out_only_that_turbo_in_auto():
    h, s = _vc100_high_vac()
    h.backend.set_fault("rs485_lost_turbo2", True)
    s = h.step(3)
    assert s.mode == Mode.AUTO and s.current.name == "PUMPING_TO_HIGH_VAC" and not s.error.status
    assert not s.commands.turbo_motor["turbo2"] and not s.commands.valves["gate2"]
    assert s.commands.valves["turbo2_valve"] and s.commands.primary          # backing kept
    assert all(s.commands.turbo_motor[t] for t in ("turbo1", "turbo3", "turbo4", "turbo5", "turbo6"))
    assert s.turbos["turbo2"].isolated and s.turbos["turbo2"].comm_lost
    pops = [m for k, m in h.dialogs.log if k == "info" and "No RS-485 reply from Turbo 2" in m]
    assert len(pops) == 1
    assert any("!! no RS-485 reply" in l and "Auto carries on" in l for l in s.event_log)
    s = h.step(10)
    assert len([m for k, m in h.dialogs.log if "No RS-485 reply" in m]) == 1      # no popup storm
    # link back + Clear Error -> Turbo 2 back in service
    h.backend.set_fault("rs485_lost_turbo2", False)
    s = h.step(1)
    assert any("Turbo 2 answering on RS-485 again" in l for l in s.event_log)
    h.ctl.request_clear_error()
    for _ in range(300):
        s = h.step()
        if s.commands.turbo_motor["turbo2"] and s.commands.valves["gate2"]:
            break
    s = h.step(1)                                               # the card follows one cycle later
    assert s.commands.turbo_motor["turbo2"] and s.commands.valves["gate2"] and not s.turbos["turbo2"].isolated


def test_lost_rs485_link_is_still_an_error_in_manual_mode():
    h = Harness(_vc100(), mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("rs485_lost_turbo3", True)
    s = h.step(2)
    assert s.error.status and "no RS-485 reply" in s.error.source and not s.turbos["turbo3"].isolated


# ---------------------------------------------------------------------------- low-pressure engage (2026-10-28)
def _rough_then_high_vac(cfg):
    from facility_control.model import FacilityState as S
    h = Harness(cfg)
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    for _ in range(3000):                                       # until it waits for the pressure
        if h.step().substate == 3:
            break
    h.advance_plant(60)                                         # roughing ends near 4e-3 mBar
    s = h.step(2)
    p_mbar = s.inputs.pressure(cfg.main_gauge.id) / 0.750061683
    h.ctl.request_auto_button("pump_to_high_vac")
    trace = []
    for _ in range(40000):
        s = h.step()
        if s.current in (S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC):
            trace.append(s)
        if s.current == S.PUMPING_TO_HIGH_VAC:
            return h, s, trace, p_mbar
    raise AssertionError("high vac not reached")


def test_below_1e2_mbar_turbos_reach_standby_before_any_gate_opens():
    c = _vc100()
    h, s, trace, p0 = _rough_then_high_vac(c)
    assert p0 < 1e-2
    gates = [t.gate_valve for t in c.turbos]
    first_gate = next(i for i, x in enumerate(trace) if any(x.commands.valves[g] for g in gates))
    first_motor = next(i for i, x in enumerate(trace) if any(x.commands.turbo_motor.values()))
    assert first_motor < first_gate                                   # turbos first ...
    before = trace[first_gate - 1]
    assert all(before.commands.turbo_motor[t] and before.commands.turbo_standby[t] for t in c.turbo_ids)
    assert all(before.turbos[t].speed_pct >= 60 for t in ("turbo2", "turbo3", "turbo5", "turbo6"))
    log = "\n".join(s.event_log)
    assert "starting the turbos with the gates closed" in log and "Turbos at standby speed" in log
    assert "not at standby speed" not in log                          # every turbo made it, no timeout
    assert not any(s.commands.turbo_standby.values())                 # high vac: full speed


def test_above_threshold_keeps_the_vi_order_gates_first():
    c = _vc100()
    c.thresholds.turbo_start_before_gates_below_torr = 1e-6           # never 'low enough'
    h, s, trace, p0 = _rough_then_high_vac(c)
    gates = [t.gate_valve for t in c.turbos]
    first_gate = next(i for i, x in enumerate(trace) if any(x.commands.valves[g] for g in gates))
    first_motor = next(i for i, x in enumerate(trace) if any(x.commands.turbo_motor.values()))
    assert first_gate < first_motor
    assert "starting the turbos with the gates closed" not in "\n".join(s.event_log)


# ---------------------------------------------------------------------------- TMP4 signals corrected (2026-10-28)
def test_tmp4_card_words_follow_running_and_above_80():
    from facility_control.model import contact_summary, normalize_contacts

    def card(run, above, motor, standby=False):
        c = normalize_contacts({"rotating": run, "at_speed": above, "alarm": False, "warning": False}, motor)
        return contact_summary(c, motor, 80.0, standby)
    assert card(False, False, False) == "Off"
    assert card(False, False, True) == "On"                       # commanded, not turning yet
    assert card(True, False, True) == "Accel · <80%"
    assert card(True, True, True) == ">80%"                        # = full speed
    assert card(True, False, True, standby=True) == "Standby · <80%"
    assert card(True, True, False) == "Decel · >80%"
    assert card(True, False, False) == "Decel · <80%"


def test_tmp4_reaches_speed_reached_in_the_simulator():
    c = _vc100()
    h = Harness(c, mode=Mode.ADMIN)
    h.step(2)
    h.ctl.request_user_cmd("chiller")
    h.step(2)
    h.backend.set_fault("no_pressure_trip", True)
    h.ctl.request_user_cmd("turbo:turbo4")                        # Shimadzu: the Yes/No start question
    s = h.step(2)
    assert s.commands.turbo_motor["turbo4"]
    assert s.turbos["turbo4"].status in (TurboStatus.SPINNING_UP, TurboStatus.OFF)
    h.advance_plant(600)
    s = h.step(1)
    assert s.turbos["turbo4"].status == TurboStatus.SPEED_REACHED
    assert s.turbos["turbo4"].contacts["rotating"] and s.turbos["turbo4"].contacts["at_speed"]
    h.dialogs.answers.put(0)                                      # 'Stop Turbo 4?' -> Yes
    h.ctl.request_user_cmd("turbo:turbo4")
    s = h.step(2)
    assert not s.commands.turbo_motor["turbo4"] and s.turbos["turbo4"].status == TurboStatus.SPINNING_DOWN


# ---------------------------------------------------------------------------- turbo valve needs backing (2026-10-28)
def _coasting_then_auto(cfg):
    """Admin: TMP2/5 run up on a pumped foreline, stop, Turbo 2 valve shut, primary off, foreline
    vented - then Auto (the event of 2026-10-28)."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    for cmd in ("chiller", "primary", "valve:turbo2_valve"):
        h.ctl.request_user_cmd(cmd)
        h.step(3)
    h.advance_plant(600)
    h.step(2)
    for tid in ("turbo2", "turbo5"):
        h.ctl.request_turbo_speed(tid, "full", confirmed=True)
        h.step(1)
    h.advance_plant(600)
    h.step(1)
    for tid in ("turbo2", "turbo5"):
        h.ctl.request_turbo_speed(tid, "off", confirmed=True)
        h.step(1)
    h.ctl.request_user_cmd("valve:turbo2_valve")
    h.step(3)
    h.ctl.request_user_cmd("primary")
    h.step(3)
    h.backend.state.p_foreline = 760.0
    s = h.step(1)
    assert s.turbos["turbo2"].speed_pct > 60 and not s.commands.valves["turbo2_valve"] and not s.commands.primary
    h.ctl.request_change_to_auto()
    return h


def test_admin_to_auto_never_opens_a_turbo_valve_onto_an_unpumped_foreline():
    c = _vc100()
    h = _coasting_then_auto(c)
    ok = c.thresholds.turbo_valve_foreline_ok_torr
    opened = None
    for i in range(60):
        s = h.step(1)
        pf = s.inputs.pressure(c.foreline_gauge.id)
        pt = s.inputs.pressure("turbo2_gauge")
        if s.commands.valves["turbo2_valve"] and opened is None:
            opened = (h.ctl.primary_running(s.inputs), pf, pt, s.turbos["turbo2"].speed_pct)
            break
        assert not s.commands.valves["turbo2_valve"]
    assert opened is not None, "turbo 2 valve never opened"
    running, pf, pt, speed = opened
    assert running and (pf < pt or pf < ok)               # primary on, foreline pumped down
    assert speed > 60                                      # no collapse from ~70 % to 15 %
    log = "\n".join(s.event_log)
    assert "Turbo 2 Valve held closed – the primary pump is not running yet" in log
    assert log.count("Kept the turbo 2 valve open") == 1     # no log spam while held


def test_strict_rule_keeps_the_valve_closed_while_the_foreline_is_above_the_turbo_gauge():
    c = _vc100()
    c.thresholds.turbo_valve_foreline_ok_torr = None
    h = _coasting_then_auto(c)
    s = h.step(40)
    assert not s.commands.valves["turbo2_valve"] and s.commands.primary
    assert any("is not below Turbo 2 Gauge" in l for l in s.event_log)
    assert sum("is not below Turbo 2 Gauge" in l for l in s.event_log) == 1


def test_manual_refuses_and_admin_warns_on_an_unbacked_turbo_valve():
    m = Harness(_vc100(), mode=Mode.MANUAL)
    m.step(2)
    m.ctl.request_user_cmd("valve:turbo2_valve")
    s = m.step(2)
    assert not s.commands.valves["turbo2_valve"]
    assert any(k == "info" and "primary pump is not running" in msg for k, msg in m.dialogs.log)
    a = Harness(_vc100(), mode=Mode.ADMIN)
    a.step(2)
    a.ctl.request_user_cmd("valve:turbo2_valve")
    s = a.step(2)
    q = [msg for k, msg in a.dialogs.log if msg.startswith("Open Turbo 2 Valve?")]
    assert q and "WARNING" in q[-1] and s.commands.valves["turbo2_valve"]        # Admin: asked, then allowed


# ---------------------------------------------------------------------------- Shimadzu standby window (2026-10-29)
def test_shimadzu_turbos_get_off_full_standby_in_admin_and_manual():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from facility_control.gui.main_window import MainWindow
    c = _vc100()
    assert c.turbo("turbo1").has_standby and c.turbo("turbo4").has_standby
    h = Harness(c, mode=Mode.ADMIN)
    win = MainWindow(h.cfg, h.ctl, sim_backend=h.backend)
    win.show()
    h.step(2)
    win._poll(); app.processEvents()
    n = len(h.dialogs.log)
    win._on_diagram_click("turbo:turbo4")                    # window, not the old Yes/No
    h.step(1)
    dlg = win.speed_dialogs["turbo4"]
    assert len(h.dialogs.log) == n and dlg.isVisible()
    assert dlg.buttons["set"].isHidden() and dlg.slider.isHidden() and not dlg.buttons["standby"].isHidden()
    dlg.buttons["standby"].click()
    assert dlg.apply_btn.text() == "Start in standby"
    dlg.apply_btn.click()
    s = h.step(2)
    assert s.commands.turbo_motor["turbo4"] and s.commands.turbo_standby["turbo4"]
    win._poll(); app.processEvents()
    assert dlg.buttons["standby"].isChecked() and "Standby" in s.turbos["turbo4"].status.label
    dlg.buttons["full"].click()
    assert dlg.apply_btn.text() == "Change to full speed"
    dlg.apply_btn.click()
    s = h.step(1)
    assert s.commands.turbo_motor["turbo4"] and not s.commands.turbo_standby["turbo4"]
    win._poll(); app.processEvents()
    dlg.buttons["standby"].click()
    dlg.apply_btn.click()                                    # running -> standby
    s = h.step(1)
    assert s.commands.turbo_standby["turbo4"]
    win._poll(); app.processEvents()
    dlg.buttons["off"].click()
    dlg.apply_btn.click()
    s = h.step(1)
    assert not s.commands.turbo_motor["turbo4"] and not s.commands.turbo_standby["turbo4"]
    # 'set' is refused for a Shimadzu even if asked directly
    h.ctl.request_turbo_speed("turbo1", "set", 50, confirmed=True)
    s = h.step(2)
    assert not s.commands.turbo_motor["turbo1"]
    win.close()
    # Manual: same window, interlocks apply (primary off -> start refused)
    m = Harness(_vc100(), mode=Mode.MANUAL)
    m.step(2)
    m.ctl.request_turbo_speed("turbo1", "standby", confirmed=True)
    s = m.step(2)
    assert not s.commands.turbo_motor["turbo1"] and any(k == "info" for k, _ in m.dialogs.log)


# ---------------------------------------------------------------------------- slow TMP4 gate (2026-10-29)
def test_slow_tmp4_gate_gets_its_30_s_before_it_counts_as_stuck():
    from facility_control.model import FacilityState as S
    c = _vc100()
    assert c.turbo("turbo4").gate_open_timeout_s == 30.0
    h = Harness(c)
    h.backend.set_fault("slow_gate4", True)                  # 20 s travel: slower than the 9 s settle
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    for _ in range(40000):
        s = h.step()
        assert not s.turbos["turbo4"].isolated, "a slow gate was treated as stuck"
        assert s.mode == Mode.AUTO and not s.error.status
        if s.current == S.PUMPING_TO_HIGH_VAC and s.inputs.valve_reads.get("gate4"):
            break
    assert s.commands.valves["gate4"] and not [m for k, m in h.dialogs.log if "Thor" in m]
    log = "\n".join(s.event_log)
    assert "Gate 4 Valve not open yet after" in log and "giving it up to 30 s" in log
    assert "Gate 4 Valve open after" in log


def test_really_stuck_tmp4_gate_is_still_taken_out_after_30_s():
    h, s = _to_high_vac_with_stuck_gate4()
    assert s.turbos["turbo4"].isolated
    log = "\n".join(s.event_log)
    assert log.index("giving it up to 30 s") < log.index("Thor's hammer")
