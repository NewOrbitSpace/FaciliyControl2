"""The HiPace (TC 400) RS-485 tool, against simulated pumps on a fake serial port.

No hardware and no pyserial needed: tools/hipace_rs485.py carries a TC 400 simulator, so framing,
checksum, data types, the control-interface rules, the safety behaviour and the validation check
list are all exercised offline.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from hipace_rs485 import (CTRL_RS485, DT, ControlNotEnabled, HiPace, PVBus, PVChecksum, PVFormat,
                          PVRejected, PVTimeout, SimBus, SimTC400, build_telegram, checksum, decode,
                          encode, error_kind, parse_telegram, query_telegram, validate)


def make(pumps=None, address=1, **faults):
    sim = SimBus(pumps or {1: SimTC400(1, time_scale=1000.0)}, delay_s=0.0, **faults)
    bus = PVBus("TEST", serial_obj=sim, timeout=0.05)
    bus.gap_s = 0.0
    return sim, bus, HiPace(bus, address, "Turbo 2")


# ---------------------------------------------------------------------------- telegrams
def test_query_telegram_matches_the_manual_example():
    assert query_telegram(1, 309) == "0010030902=?107\r"


def test_checksum_is_the_ascii_sum_modulo_256():
    assert checksum("0010030902=?") == 107
    t = build_telegram(1, 10, 309, "000633")
    assert int(t[-4:-1]) == checksum(t[:-4])


def test_reply_is_parsed():
    t = parse_telegram(build_telegram(1, 10, 309, "000633"))
    assert (t.address, t.action, t.param, t.data) == (1, 10, 309, "000633")


def test_corrupted_reply_is_rejected():
    good = build_telegram(1, 10, 309, "000633")
    with pytest.raises(PVChecksum):
        parse_telegram(good[:-5] + "4" + good[-4:])
    body = "0011030907000633"                                   # length field says 7, data is 6
    with pytest.raises(PVFormat):
        parse_telegram(f"{body}{checksum(body):03d}\r")


@pytest.mark.parametrize("dtype,value,text", [
    (DT.BOOL_OLD, True, "111111"), (DT.BOOL_OLD, False, "000000"), (DT.U_INT, 633, "000633"),
    (DT.U_REAL, 66.7, "006670"), (DT.U_SHORT, 2, "002"), (DT.BOOL_NEW, True, "1"),
])
def test_data_types_round_trip(dtype, value, text):
    assert encode(dtype, value) == text
    assert decode(dtype, text) == pytest.approx(value)


def test_exponential_type():
    assert decode(DT.U_EXPO, "100023") == pytest.approx(1000.0)
    assert decode(DT.U_EXPO, "250017") == pytest.approx(2.5e-3)
    assert decode(DT.U_EXPO, encode(DT.U_EXPO, 3.2e-7)) == pytest.approx(3.2e-7)


def test_error_codes_are_classified():
    assert error_kind("no Err") == "ok"
    assert error_kind("Err002") == "error"
    assert error_kind("Wrn007") == "warning"


# ---------------------------------------------------------------------------- bus
def test_scan_finds_both_pumps_on_a_shared_bus():
    _, bus, _ = make({1: SimTC400(1), 2: SimTC400(2)})
    assert [a for a, _ in bus.scan(range(1, 6))] == [1, 2]


def test_two_pumps_on_one_bus_are_independent():
    _, bus, t2 = make({1: SimTC400(1, time_scale=1000.0), 2: SimTC400(2, time_scale=1000.0)})
    t3 = HiPace(bus, 2, "Turbo 3")
    for p in (t2, t3):
        p.control_enabled = True
        p.take_rs485_control()
    t2.start()
    import time; time.sleep(0.05)
    assert t2.read(309) > 0 and t3.read(309) == 0


def test_no_reply_is_a_timeout_and_is_counted():
    _, bus, pump = make(dead=True)
    with pytest.raises(PVTimeout):
        pump.read(309)
    assert pump.stats.timeouts == 1 and pump.stats.ok == 0


def test_noise_is_counted_as_checksum_errors():
    _, bus, pump = make(noise=1.0)
    with pytest.raises(PVChecksum):
        pump.read(309)
    assert pump.stats.checksum_errors == 1


def test_converter_echo_is_skipped_for_queries_and_writes():
    sim, bus, pump = make(echo=True)
    assert pump.read(349) == "TC400"
    assert bus.echo_detected
    pump.control_enabled = True
    pump.take_rs485_control()                 # write reply == write telegram: must still work
    assert sim.pumps[1].p[60] == CTRL_RS485


def test_an_echoing_converter_with_a_dead_pump_does_not_fake_a_write():
    sim, bus, pump = make(echo=True)
    pump.read(349)
    sim.dead = True
    pump.control_enabled = True
    with pytest.raises(PVTimeout):
        pump.write(2, True)


def test_unknown_parameter_is_reported_as_no_def():
    _, bus, pump = make()
    with pytest.raises(PVRejected) as e:
        pump.read_raw(999)
    assert e.value.code == "NO_DEF"


# ---------------------------------------------------------------------------- control
def test_nothing_is_written_unless_control_is_enabled():
    sim, bus, pump = make()
    with pytest.raises(ControlNotEnabled):
        pump.start()
    with pytest.raises(ControlNotEnabled):
        pump.take_rs485_control()
    assert not any(t[3:5] == "10" for t in sim.received)        # no write telegram left the PC


def test_pump_rejects_commands_until_rs485_has_control():
    sim, bus, pump = make()
    pump.control_enabled = True
    with pytest.raises(PVRejected) as e:
        pump.start()                       # [P:060] is still 1 (remote D-Sub)
    assert e.value.code == "_LOGIC"
    pump.take_rs485_control()
    assert pump.read(60) == CTRL_RS485 and pump.original_ctrl == 1
    pump.start()
    assert pump.read(10) is True


def test_remote_priority_overrides_rs485():
    sim, bus, pump = make()
    sim.pumps[1].p[300] = True
    pump.control_enabled = True
    pump.take_rs485_control()
    with pytest.raises(PVRejected):
        pump.start()


def test_stop_is_always_allowed():
    sim, bus, pump = make()
    pump.control_enabled = True
    pump.take_rs485_control()
    pump.start()
    pump.control_enabled = False
    pump.stop()
    assert pump.read(10) is False


def test_safe_exit_stops_and_restores_the_interface():
    sim, bus, pump = make()
    pump.control_enabled = True
    pump.take_rs485_control()
    pump.start()
    msgs = pump.safe_exit()
    assert sim.pumps[1].p[10] is False and sim.pumps[1].p[60] == 1
    assert any("restored" in m for m in msgs)


def test_safe_exit_leaves_a_pump_it_did_not_start():
    sim, bus, pump = make()
    sim.pumps[1].p.update({60: 2, 10: True})        # running under someone else's control
    pump.control_enabled = True
    pump.safe_exit()
    assert sim.pumps[1].p[10] is True and sim.pumps[1].p[60] == 2


def test_safe_exit_never_raises_on_a_dead_link():
    sim, bus, pump = make()
    pump.control_enabled = True
    pump.take_rs485_control()
    pump.start()
    sim.dead = True
    msgs = pump.safe_exit()
    assert all(m.startswith("!!") for m in msgs) and len(msgs) == 2


def test_standby_speed_and_address_are_bounded():
    _, bus, pump = make()
    pump.control_enabled = True
    for bad in (5.0, 120.0):
        with pytest.raises(ValueError):
            pump.set_standby_speed(bad)
    with pytest.raises(ValueError):
        pump.set_address(0)


def test_address_change_moves_the_pump():
    sim, bus, pump = make()
    pump.control_enabled = True
    pump.set_address(2)
    assert pump.address == 2 and 2 in sim.pumps and 1 not in sim.pumps
    assert pump.read(797) == 2


def test_status_words():
    sim, bus, pump = make()
    pump.identify()
    assert pump.snapshot(full=True).status(pump.nominal_hz) == "Off"
    sim.pumps[1].p[303] = "Err002"
    assert pump.snapshot(full=True).status(pump.nominal_hz) == "Error"


# ---------------------------------------------------------------------------- validation
def test_validation_passes_on_a_healthy_rs485_pump():
    sim, bus, pump = make()
    sim.pumps[1].p[60] = 2
    pump.control_enabled = True
    checks = validate(pump, pings=10, write_test=True)
    by = {c.name: c.result for c in checks}
    assert by["Link"] == "PASS" and by["Link quality"] == "PASS"
    assert by["Write round trip"] == "PASS" and by["Overall"] == "PASS"


def test_validation_flags_remote_priority_and_wrong_address():
    sim, bus, pump = make()
    sim.pumps[1].p.update({300: True, 797: 1})
    by = {c.name: c.result for c in validate(pump, pings=5)}
    assert by["Remote priority"] == "FAIL" and by["Overall"] == "FAIL"


def test_validation_stops_early_when_nothing_answers():
    _, bus, pump = make(dead=True)
    checks = validate(pump, pings=5)
    assert [c.result for c in checks] == ["FAIL"]


def test_cli_refuses_writes_without_allow_control():
    from hipace_rs485 import main
    with pytest.raises(SystemExit) as e:
        main(["--port", "SIM2", "control", "--start"])
    assert e.value.code != 0


# ---------------------------------------------------------------------------- GUI smoke test
def test_gui_connects_to_the_simulator_and_leaves_the_pumps_safely(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import argparse
    import time
    from PySide6.QtWidgets import QApplication, QMessageBox
    import hipace_rs485_gui as gui
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))
    SimBus._registry.clear()
    app = QApplication.instance() or QApplication([])
    w = gui.MainWindow(argparse.Namespace(sim=True, t2_port="SIM1", t3_port="SIM1", t2_address=1,
                                          t3_address=2, interval=0.2, connect=False))
    w.connect_all()
    assert set(w.pumps) == {"Turbo 2", "Turbo 3"} and len(w.workers) == 1      # one shared bus
    w.panels["Turbo 2"].btn_start.click()                                      # control disabled
    w.chk_control.setChecked(True)
    w.panels["Turbo 2"].btn_take.click()
    w.panels["Turbo 2"].btn_start.click()
    t_end = time.time() + 3
    sim = SimBus._registry["SIM1"].pumps[1]
    while time.time() < t_end and not sim.p[10]:
        app.processEvents()
        time.sleep(0.05)
    assert sim.p[10] is True and sim.p[60] == CTRL_RS485
    assert SimBus._registry["SIM1"].pumps[2].p[10] is False                  # Turbo 3 untouched
    w.disconnect_all(ask=False)
    assert sim.p[10] is False and sim.p[60] == 1                              # stopped + restored
    w.close()
