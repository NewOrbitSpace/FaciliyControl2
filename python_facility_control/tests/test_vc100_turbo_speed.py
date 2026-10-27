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


def test_shimadzu_turbo_has_no_speed_control():
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


# ---------------------------------------------------------------------------- GUI panel
def test_speed_panel_rows_and_mode_gating():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from facility_control.gui.main_window import MainWindow
    h = Harness(_vc100(), mode=Mode.AUTO)
    win = MainWindow(h.cfg, h.ctl, sim_backend=h.backend)
    win.show()
    h.step(2)
    win._poll(); app.processEvents()
    panel = win.speed_panel
    assert sorted(panel.rows) == ["turbo2", "turbo3", "turbo5", "turbo6"]
    assert not panel.rows["turbo2"].buttons["set"].isEnabled()          # Auto: disabled
    h.ctl.mode = Mode.ADMIN
    h.step(1)
    win._poll(); app.processEvents()
    row = panel.rows["turbo2"]
    assert row.buttons["set"].isEnabled() and row.buttons["off"].isChecked()
    row.slider.setValue(45)
    row.buttons["set"].click()
    h.step(2)
    win._poll(); app.processEvents()
    assert row.buttons["set"].isChecked() and h.ctl.snapshot().commands.speed_set("turbo2") == 45.0
    # slider moved while in Set -> applied after release
    row.slider.setSliderDown(True)
    row.slider.setValue(65)
    row.slider.setSliderDown(False)          # emits sliderReleased
    h.step(1)
    assert h.ctl.snapshot().commands.speed_set("turbo2") == 65.0
    row.buttons["full"].click()
    h.step(1)
    win._poll(); app.processEvents()
    assert row.buttons["full"].isChecked() and h.ctl.snapshot().commands.speed_set("turbo2") is None
    win.close()
