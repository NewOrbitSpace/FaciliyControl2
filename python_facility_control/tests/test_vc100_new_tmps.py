"""VC100 extension of 2026-10-13: TMP4 (Shimadzu on its HD15 remote), TMP5/TMP6 (HiPace, RS-485), their
gate valves on the new NI-9472 in slot 5, and the shared turbo valves (TMP4/5/6 are backed through
Turbo 1/2/3's turbo valve and read Turbo 1/2/3's gauge)."""
import pytest

from conftest import Harness
from test_nidaqmx_backend import FakeTask, fake_daq  # noqa: F401  (fixture reused)
from facility_control.config import CONFIG_DIR, load_config
from facility_control.interlocks import Interlocks
from facility_control.model import Commands, FacilityState as S, Inputs, Mode, TurboInputs, TurboStatus, normalize_contacts

VC100 = str(CONFIG_DIR / "facility_vc100.yaml")


@pytest.fixture
def vcfg():
    c = load_config(VC100)
    c.simulation.time_scale = 200.0
    return c


# ---------------------------------------------------------------------------- profile / pinout
def test_new_lines_match_the_wiring(vcfg):
    g = vcfg.valves
    assert (g["gate4"].cmd, g["gate4"].read) == ("Mod5/port0/line4", "Mod2/port0/line22")
    assert (g["gate5"].cmd, g["gate5"].read) == ("Mod5/port0/line3", "Mod2/port0/line23")
    assert (g["gate6"].cmd, g["gate6"].read) == ("Mod5/port0/line5", "Mod2/port0/line24")
    t4 = vcfg.turbo("turbo4")
    p = t4.params
    assert t4.control_mode == "shimadzu_contacts" and t4.gate_valve == "gate4"
    assert (p["error_ack_do"], p["standby_do"], p["motor_do"]) == ("Mod5/port0/line0", "Mod5/port0/line1", "Mod5/port0/line2")
    assert (p["at_speed_di"], p["accel_decel_di"], p["alarm_di"], p["warning_di"]) == \
        ("Mod2/port0/line18", "Mod2/port0/line19", "Mod2/port0/line20", "Mod2/port0/line21")
    assert p["alarm_di_inverted"] is False and p["warning_di_inverted"] is False       # 24 V = alarm / warning
    for new, old in (("turbo4", "turbo1"), ("turbo5", "turbo2"), ("turbo6", "turbo3")):
        assert vcfg.turbo(new).turbo_valve == vcfg.turbo(old).turbo_valve
        assert vcfg.turbo_gauge(new).id == f"{old}_gauge"
    assert [t.id for t in vcfg.turbos_on_valve("turbo1_valve")] == ["turbo1", "turbo4"]
    assert {t.id for t in vcfg.rs485_turbos()} == {"turbo2", "turbo3", "turbo5", "turbo6"}
    from tools.daq_check import channel_inventory
    phys = [ph for _, ph, _ in channel_inventory(vcfg)]
    assert len(phys) == len(set(phys)) == 48        # no line used twice (19 DO, 23 DI, 6 AI)


# ---------------------------------------------------------------------------- HD15 contact mapping
def test_hd15_contacts_map_onto_the_canonical_six():
    # accelerating: motor on, accel/decel output active, not yet above 80 %
    c = normalize_contacts({"at_speed": False, "accel_decel": True, "alarm": False, "warning": False}, True)
    assert c["accelerating"] and not c["braking"] and c["rotating"] and not c["at_speed"]
    # at speed: > 80 %, no longer accelerating
    c = normalize_contacts({"at_speed": True, "accel_decel": False, "alarm": False, "warning": False}, True)
    assert c["at_speed"] and c["rotating"] and not c["accelerating"]
    # coasting down from full speed: braking, and NOT "speed reached" although still above 80 %
    c = normalize_contacts({"at_speed": True, "accel_decel": True, "alarm": False, "warning": False}, False)
    assert c["braking"] and not c["at_speed"] and c["rotating"]
    # stopped
    c = normalize_contacts({"at_speed": False, "accel_decel": False, "alarm": False, "warning": False}, False)
    assert not any(c[k] for k in ("rotating", "accelerating", "braking", "at_speed"))
    # Turbo 1's full set passes through unchanged
    full = {"rotating": True, "accelerating": False, "at_speed": True, "braking": False, "alarm": False, "warning": True}
    assert normalize_contacts(full, True) == full


def test_daq_backend_wires_tmp4_and_the_new_gates(vcfg, fake_daq):
    vcfg.turbos = [t for t in vcfg.turbos if t.control_mode != "hipace_rs485"]    # no serial port in CI
    b = fake_daq.NiDaqmxBackend(vcfg)
    b.open()
    t = FakeTask.registry
    assert [c.phys for c in t["turbo4_cmd"].channels] == ["cDAQ3Mod5/port0/line2", "cDAQ3Mod5/port0/line1"]
    assert t["turbo4_ack"].channels[0].phys == "cDAQ3Mod5/port0/line0"
    assert [c.phys for c in t["turbo4_read"].channels] == [f"cDAQ3Mod2/port0/line{n}" for n in (18, 20, 21, 19)]
    vc = [c.phys for c in t["valve_cmd"].channels]
    assert vc[-3:] == ["cDAQ3Mod5/port0/line4", "cDAQ3Mod5/port0/line3", "cDAQ3Mod5/port0/line5"]
    assert [c.phys for c in t["valve_read"].channels][-3:] == [f"cDAQ3Mod2/port0/line{n}" for n in (22, 23, 24)]
    # motor on, > 80 % and the accel/decel output off -> at speed; alarm/warning lines low = healthy
    cmds = Commands.all_off(vcfg.valve_ids, vcfg.turbo_ids)
    cmds.turbo_motor["turbo4"] = True
    b.write(cmds)
    assert t["turbo4_cmd"].writes[-1] == [True, False]
    FakeTask.di_values.update({"cDAQ3Mod2/port0/line18": True, "cDAQ3Mod2/port0/line19": False,
                               "cDAQ3Mod2/port0/line20": False, "cDAQ3Mod2/port0/line21": False,
                               "cDAQ3Mod2/port0/line22": True})
    inp = b.read()
    c4 = inp.turbos["turbo4"].contacts
    assert c4["at_speed"] and c4["rotating"] and not c4["alarm"] and not inp.turbos["turbo4"].error
    assert inp.valve_reads["gate4"] is True
    # 24 V on the alarm line = alarm (not inverted, unlike Turbo 1)
    FakeTask.di_values["cDAQ3Mod2/port0/line20"] = True
    assert b.read().turbos["turbo4"].error is True
    b.turbo_error_ack("turbo4", True)
    assert t["turbo4_ack"].writes[-1] is True
    b.close()


def test_tmp4_alarm_raises_5005_and_warning_shows(vcfg):
    h = Harness(vcfg, mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("turbo_warning_turbo4", True)
    s = h.step()
    assert s.turbos["turbo4"].status == TurboStatus.WARNING and s.turbos["turbo1"].status == TurboStatus.OFF
    assert s.error.code == 5005 and "Turbo 4 Warning" in s.error.source
    h.backend.set_fault("turbo_warning_turbo4", False)
    h.backend.set_fault("turbo_error_turbo4", True)
    s = h.step()
    assert s.turbos["turbo4"].status == TurboStatus.ERROR and "Turbo 4 Alarm" in s.error.source


# ---------------------------------------------------------------------------- shared turbo valves
def test_manual_mode_will_not_close_a_shared_valve_while_the_other_turbo_runs(vcfg):
    il = Interlocks(vcfg)
    cmds = Commands.all_off(vcfg.valve_ids, vcfg.turbo_ids)
    cmds.valves["turbo1_valve"] = True
    cmds.turbo_motor["turbo4"] = True                  # TMP4 runs, Turbo 1 is off
    inp = Inputs(turbos={tid: TurboInputs() for tid in vcfg.turbo_ids})
    v = il.valve("turbo1_valve", False, cmds, inp)
    assert not v.allowed and "Turbo Off" in v.message
    cmds.turbo_motor["turbo4"] = False
    inp.turbos["turbo4"].contacts = {"rotating": True}
    assert not il.valve("turbo1_valve", False, cmds, inp).allowed      # still coasting
    inp.turbos["turbo4"].contacts = {"rotating": False}
    assert il.valve("turbo1_valve", False, cmds, inp).allowed


def test_spin_down_keeps_a_shared_valve_open_for_the_turbo_still_turning(vcfg):
    h = Harness(vcfg)
    h.dialogs.answers.put(True)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 20000)
    h.run_until(lambda s: all(tv.status == TurboStatus.SPEED_REACHED for tv in s.turbos.values()), 20000)
    h.ctl.request_auto_button("shutdown")
    s = h.step(2)
    # Turbo 1 has already stopped, TMP4 (same turbo valve) is still near full speed
    for _ in range(5):
        h.backend.state.turbos["turbo1"].speed_pct = 0.0
        h.backend.state.turbos["turbo4"].speed_pct = 90.0
        s = h.step()
        assert s.commands.valves["turbo1_valve"], f"shared valve closed on a turning TMP4 ({s.current.label})"


def test_a_parked_turbo_does_not_close_the_valve_it_shares(vcfg):
    vcfg.turbo("turbo4").in_use = False
    h = Harness(vcfg, mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(0)
    h.ctl.request_user_cmd("valve:turbo1_valve")
    s = h.step(3)
    assert s.commands.valves["turbo1_valve"] and not s.commands.valves["gate4"]
