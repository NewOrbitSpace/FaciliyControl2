"""HiPace turbos over RS-485 inside the facility program (VC100, 2026-10-09).

Exercises hal/hipace_rs485_turbos.py against the TC 400 simulator (no hardware, no pyserial): up to
four pumps on one RS-485 segment, the desired-state writes, error / warning / comm-loss reporting,
the safe exit, read-only mode, the DAQ backend merge and a full Auto pump-down with four HiPace.
"""
import time

import pytest
import yaml

from conftest import Harness
from test_nidaqmx_backend import FakeTask, fake_daq  # noqa: F401  (fixture reused)
from facility_control.config import CONFIG_DIR, TurboConfig, build_config, check_rs485_turbos
from facility_control.drivers.pfeiffer_pv import SimBus, SimTC400, parse_telegram
from facility_control.hal.hipace_rs485_turbos import RS485Turbos
from facility_control.model import Commands, FacilityState as S, Mode, TurboStatus

VC100 = CONFIG_DIR / "facility_vc100.yaml"


def _turbos(n=4, port="BUS"):
    return [TurboConfig(id=f"turbo{a}", label=f"Turbo {a}", control_mode="hipace_rs485",
                        gate_valve="g", turbo_valve="v",
                        params={"port": port, "address": a, "timeout_s": 0.05, "comm_timeout_s": 0.5,
                                "write_retry_s": 0.0})
            for a in range(1, n + 1)]


def _bus(n=4, **faults):
    return SimBus({a: SimTC400(a, time_scale=400.0) for a in range(1, n + 1)}, delay_s=0.0, **faults)


def _mgr(sim, turbos=None, **kw):
    m = RS485Turbos(turbos or _turbos(len(sim.pumps)), serial_factory=lambda *a: sim, **kw)
    m.open(start_threads=False)
    for b in m.buses.values():
        b.gap_s = 0.0
    return m


def _writes(sim):
    return [parse_telegram(t) for t in sim.received if parse_telegram(t).action == 10]


# ---------------------------------------------------------------------------- configuration
def test_four_pumps_may_share_a_port_but_not_an_address():
    check_rs485_turbos(_turbos(4))                       # four on one segment: fine
    dup = _turbos(2)
    dup[1].params["address"] = 1
    with pytest.raises(ValueError, match="already used"):
        check_rs485_turbos(dup)
    mixed = _turbos(2)
    mixed[1].params["baud"] = 19200
    with pytest.raises(ValueError, match="baud"):
        check_rs485_turbos(mixed)


def test_vc100_profile_runs_turbos_2_and_3_over_rs485_and_keeps_the_dsub_block():
    from facility_control.config import load_config
    c = load_config(str(VC100))
    for tid in ("turbo2", "turbo3"):
        tc = c.turbo(tid)
        assert tc.control_mode == "hipace_rs485" and tc.has_error_ack
        assert "hipace_dsub25" in tc.all_modes            # one word to go back to the D-SUB lines
    assert c.turbo("turbo1").control_mode == "shimadzu_contacts"
    from tools.daq_check import channel_inventory
    phys = [p for _, p, _ in channel_inventory(c)]
    assert not any("Mod3/port0/line3" in p or "Mod8/ai5" in p or "Mod2/port0/line16" in p for p in phys)


# ---------------------------------------------------------------------------- the manager
def test_takes_control_then_starts_stops_and_standbys_each_pump_on_a_shared_bus():
    sim = _bus(4)
    m = _mgr(sim)
    assert all(p.p[60] == 2 for p in sim.pumps.values())             # [P:060] = RS-485
    assert len(m.take_events()) == 4
    m.set_commands({"turbo1": True, "turbo3": True}, {"turbo3": True})
    for _ in range(8):
        m.poll_once()
        time.sleep(0.02)
    assert [sim.pumps[a].p[10] for a in (1, 2, 3, 4)] == [True, False, True, False]
    assert sim.pumps[3].p[2] is True and sim.pumps[1].p[2] is False
    t1, t2 = m.inputs("turbo1"), m.inputs("turbo2")
    assert t1.motor_read and t1.speed_pct > 1.0 and not t1.error and t1.comm_ok
    assert not t2.motor_read and t2.speed_pct == 0.0
    assert set(t1.temps) == {"electronics", "bearing", "motor"}
    m.set_commands({}, {})                                          # safe state
    m.close()
    assert not any(p.p[10] for p in sim.pumps.values())


def test_error_code_from_p303_and_ack_over_p009():
    sim = _bus(2)
    m = _mgr(sim)
    sim.pumps[2].p[303] = "Err006"
    m.poll_once()
    ti = m.inputs("turbo2")
    assert ti.error and ti.error_text == "Err006"
    m.error_ack("turbo2")
    m.poll_once()
    assert any(w.param == 9 and w.address == 2 for w in _writes(sim))
    assert not m.inputs("turbo2").error
    sim.pumps[1].p[303] = "Wrn007"
    m.poll_once()
    ti = m.inputs("turbo1")
    assert ti.warning and not ti.error                              # shown, does not stop Auto


def test_lost_link_reports_an_error_and_keeps_the_last_known_speed():
    sim = _bus(1)
    m = _mgr(sim)
    m.set_commands({"turbo1": True}, {})
    for _ in range(6):
        m.poll_once()
        time.sleep(0.02)
    speed = m.inputs("turbo1").speed_pct
    assert speed > 1.0
    sim.dead = True
    time.sleep(0.6)
    m.poll_once()
    ti = m.inputs("turbo1")
    assert ti.error and not ti.comm_ok and "no RS-485 reply" in ti.error_text
    assert ti.speed_pct == pytest.approx(speed)          # unknown speed is never reported as 0
    assert ti.motor_read is True                         # no extra motor-conflict on top of the comm error


def test_a_lost_write_is_sent_again():
    sim = _bus(1)
    m = _mgr(sim)
    sim.dead = True
    m.set_commands({"turbo1": True}, {})
    m.poll_once()
    assert not sim.pumps[1].p[10]
    sim.dead = False
    m.poll_once()
    assert sim.pumps[1].p[10]


def test_remote_priority_makes_control_writes_fail_visibly():
    sim = _bus(1)
    sim.pumps[1].p[300] = True                           # D-Sub remote priority input active
    m = _mgr(sim)
    m.set_commands({"turbo1": True}, {})
    m.poll_once()
    ti = m.inputs("turbo1")
    assert ti.error and "refused" in ti.error_text and not sim.pumps[1].p[10]


def test_read_only_never_writes():
    sim = _bus(2)
    m = _mgr(sim, read_only=True)
    m.set_commands({"turbo1": True, "turbo2": True}, {})
    m.error_ack("turbo1")
    for _ in range(3):
        m.poll_once()
    m.close()
    assert _writes(sim) == []
    assert m.inputs("turbo1").comm_ok


def test_a_port_that_cannot_be_opened_is_an_error_not_a_crash():
    def refuse(*_a):
        raise OSError("could not open port COM9")
    m = RS485Turbos(_turbos(1, port="COM9"), serial_factory=refuse)
    m.open(start_threads=False)
    time.sleep(0.6)
    ti = m.inputs("turbo1")
    assert ti.error and "cannot open COM9" in ti.error_text
    m.close()


def test_threaded_pollers_stop_the_pumps_on_close():
    sim = _bus(4)
    m = RS485Turbos(_turbos(4), serial_factory=lambda *a: sim)
    m.open()
    m.set_commands({f"turbo{i}": True for i in range(1, 5)}, {})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(p.p[10] for p in sim.pumps.values()):
        time.sleep(0.05)
    assert all(p.p[10] for p in sim.pumps.values())
    m.set_commands({}, {})
    assert m.close() == []
    assert not any(p.p[10] for p in sim.pumps.values())


# ---------------------------------------------------------------------------- DAQ backend merge
def _raw_vc100():
    return yaml.safe_load(open(VC100, encoding="utf-8"))


def test_daq_backend_drives_rs485_turbos_without_daq_lines(fake_daq):
    raw = _raw_vc100()
    SimBus._registry.pop("SIM4", None)
    sim_adr = {"turbo2": 2, "turbo3": 3, "turbo5": 1, "turbo6": 4}      # SIM4 carries addresses 1-4
    for t in raw["turbos"]:
        if t["control_mode"] == "hipace_rs485":
            t["hipace_rs485"]["port"] = "SIM4"
            t["hipace_rs485"]["address"] = sim_adr[t["id"]]
    cfg = build_config(raw)
    b = fake_daq.NiDaqmxBackend(cfg)
    b.open()
    try:
        t = FakeTask.registry
        assert not any(k.startswith(("turbo2", "turbo3", "turbo5", "turbo6")) for k in t)   # no DO/DI/AI task at all
        assert "turbo1_cmd" in t and "turbo1_read" in t                         # Shimadzu unchanged
        assert "RS-485 SIM4" in b.describe()
        sim = SimBus._registry["SIM4"]
        cmds = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
        cmds.turbo_motor["turbo2"] = True
        b.write(cmds)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not sim.pumps[2].p[10]:
            time.sleep(0.05)
        assert sim.pumps[2].p[10] and not sim.pumps[3].p[10]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not b.read().turbos["turbo2"].motor_read:
            time.sleep(0.05)
        inp = b.read()
        assert inp.turbos["turbo2"].motor_read and inp.turbos["turbo2"].comm_ok
        b.turbo_error_ack("turbo3", True)
        b.turbo_error_ack("turbo3", False)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
                parse_telegram(x).param == 9 and parse_telegram(x).address == 3 for x in sim.received):
            time.sleep(0.05)
        assert any(parse_telegram(x).param == 9 and parse_telegram(x).address == 3 for x in list(sim.received))
        b.safe_state()
    finally:
        b.close()
    assert not sim.pumps[2].p[10]


# ---------------------------------------------------------------------------- four HiPace in Auto mode
def _four_hipace_profile():
    """The VC100 profile itself (2026-10-13): Turbos 1/4 Shimadzu, Turbos 2/3/5/6 HiPace on one RS-485 bus."""
    from facility_control.config import load_config
    cfg = load_config(str(VC100))
    cfg.simulation.time_scale = 200.0
    return cfg


def test_auto_pump_down_engages_four_hipace_and_the_shimadzu():
    cfg = _four_hipace_profile()
    assert len(cfg.rs485_turbos()) == 4
    h = Harness(cfg)
    h.dialogs.answers.put(True)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 20000)
    s, _ = h.run_until(lambda s: all(tv.status == TurboStatus.SPEED_REACHED for tv in s.turbos.values()), 20000)
    assert set(s.turbos) == {f"turbo{n}" for n in range(1, 7)}
    assert all(s.commands.valves[f"gate{n}"] for n in range(1, 7))
    assert not s.error.status


def test_rs485_turbo_error_stops_auto_with_the_pump_error_code():
    cfg = _four_hipace_profile()
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("turbo_error_turbo5", True)
    s = h.step()
    assert s.turbos["turbo5"].status == TurboStatus.ERROR
    assert s.error.code == 5007 and s.error.source == "Turbo 5 Error: Err001"


def test_motor_readback_lag_is_not_a_conflict_but_a_real_mismatch_is():
    """The poller confirms [P:010] up to ~1 s after a command: no 5005 in that window (blocking mode
    has no settle wait for a turbo), but a pump that never follows is still reported."""
    from facility_control.config import load_config
    cfg = load_config(str(VC100))
    cfg.simulation.time_scale = 200.0
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    lag = {"on": True}
    orig = h.backend.read

    def lagging_read():
        inp = orig()
        if lag["on"]:
            inp.turbos["turbo2"].motor_read = False     # pump has not confirmed yet / never will
        return inp
    h.backend.read = lagging_read
    h.dialogs.answers.put(0)                            # Start Turbo 2? Yes
    h.ctl.request_user_cmd("turbo:turbo2")
    s = h.step()
    assert s.commands.turbo_motor["turbo2"] and s.error.code != 5005
    s, _ = h.run_until(lambda s: s.error.code == 5005, 20000)     # after motor_confirm_s it is flagged
    assert "Turbo Motor" in s.error.source
