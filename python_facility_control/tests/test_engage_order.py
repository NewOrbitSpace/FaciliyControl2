"""Engage Turbo on the VC100: gates first or turbos first is decided ONCE, and the wait is shown.

Reported 2026-11-11 (VC100): "when pumping to high vacuum occasionally the normal procedure will
happen but the gate valves are not open and the turbos are put into standby.  There are no errors or
an indication something has happened ... I have to go into admin mode, open all the gate valves and
set all the turbos to top speed, before moving back into auto mode."

That state is the turbos-first order added on 2026-10-28 (chamber below
`turbo_start_before_gates_below_mbar`: start every turbo behind its closed gate - which is standby
on the VC100 - wait for standby speed, then open the gates).  Two things were wrong with it:

  * the choice between the two orders was re-made on EVERY pass of the gate step.  In the normal
    gates-first order the gates open one at a time (shared air line), so that step runs for a
    minute or more while the chamber keeps falling; a reading that crossed the limit part-way made
    Auto close the gates it had just opened and start all six turbos in standby behind them;
  * while waiting - up to `turbo_standby_timeout_s`, 10 min - the panel said only "substate 6".
"""
import re

import pytest

from facility_control.automode import AutoInput, AutoStateMachine
from facility_control.config import CONFIG_DIR, load_config
from facility_control.model import Commands, FacilityState as S, Inputs, TurboInputs
from facility_control.units import to_torr
from tests.conftest import Harness


def _vc100(time_scale=400.0):
    c = load_config(str(CONFIG_DIR / "facility_vc100.yaml"))
    c.simulation.time_scale = time_scale
    return c


def _gates(cfg):
    return sorted({t.gate_valve for t in cfg.turbos if t.in_use})


# ------------------------------------------------------------------------- the part-way flip
def test_gates_never_close_again_when_the_chamber_crosses_the_limit_part_way():
    cfg = _vc100()
    limit = cfg.thresholds.turbo_start_before_gates_below_torr
    assert limit is not None and cfg.timings.gate_open_stagger_s > 0
    h = Harness(cfg)
    st = h.backend.state
    gates = _gates(cfg)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")            # from atmosphere: the VIs' order, gates first
    dipped = False
    opened = set()
    for _ in range(300000):
        s = h.step()
        now_open = {g for g in gates if s.commands.valves[g]}
        if s.current in (S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC):
            assert opened <= now_open, f"gate(s) {sorted(opened - now_open)} closed again in {s.current.label}/{s.substate}"
            opened |= now_open
            if s.current == S.ENGAGE_TURBO:
                # gates first means: no turbo is started (let alone put in standby) behind a shut gate
                for t in cfg.turbos:
                    assert not (s.commands.turbo_standby[t.id] and not s.commands.valves[t.gate_valve]), \
                        f"{t.id} in standby behind a closed gate"
        if not dipped and s.current == S.ENGAGE_TURBO and s.substate == 4 and len(now_open) == 3:
            st.p_chamber = 0.8 * limit                       # the reading crosses the limit, 3 of 6 gates open
            dipped = True
        if s.current == S.PUMPING_TO_HIGH_VAC and len(now_open) == len(gates) and all(s.commands.turbo_motor.values()):
            break
    else:
        raise AssertionError("high vacuum not reached")
    assert dipped, "the test never got to dip the pressure"
    assert not any("starting the turbos with the gates closed" in line for line in s.event_log)
    assert not any(s.commands.turbo_standby.values()) and not s.error.status
    assert h.ctl.auto.mem.engage_order is None               # decided afresh next time


def _unit(cfg, sm, wrg, prev, sub=4):
    inp = Inputs()
    inp.pressures_torr = {g.id: 1e-2 for g in cfg.gauges}
    inp.pressures_torr[cfg.main_gauge.id] = wrg
    inp.primary_read = inp.chiller_read = True
    inp.valve_reads = dict(prev.valves)
    for t in cfg.turbos:
        ti = TurboInputs(speed_pct=0.0)
        if t.control_mode == "shimadzu_contacts":
            ti.contacts = {"rotating": False}
        inp.turbos[t.id] = ti
    return sm.step(AutoInput(inp, prev, S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC, sub, set(), 1000.0, False,
                             cfg.thresholds.turbo_on_threshold_torr, None, False))


def _engage_prev(cfg):
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.primary = c.primary_run = c.primary_power = c.chiller = True
    for t in cfg.turbos:
        c.valves[t.turbo_valve] = True
    return c


def test_unit_the_order_is_latched_until_engage_is_left():
    cfg = _vc100()
    cfg.timings.gate_open_stagger_s = 0.0                    # one pass opens every gate: simpler to follow
    limit = cfg.thresholds.turbo_start_before_gates_below_torr
    # ---- gates first, then the reading falls below the limit before the turbos start
    sm = AutoStateMachine(cfg)
    out = _unit(cfg, sm, 20 * limit, _engage_prev(cfg))
    assert sm.mem.engage_order == "gates_first" and all(out.commands.valves[g] for g in _gates(cfg))
    cfg.timings.gate_open_stagger_s = 5.0                    # back on the shared air line: the step repeats
    half = out.commands.copy()
    for g in _gates(cfg)[3:]:
        half.valves[g] = False
    out2 = _unit(cfg, sm, 0.5 * limit, half)
    assert sm.mem.engage_order == "gates_first"
    assert all(out2.commands.valves[g] for g in _gates(cfg)[:3]), "an open gate was closed again"
    # no turbo is started behind a closed gate, none is put in standby (2026-11-13: a turbo whose own
    # gate is open may already have been started - `start_turbo_with_its_gate`)
    for t in cfg.turbos:
        if not out2.commands.valves[t.gate_valve]:
            assert not out2.commands.turbo_motor[t.id], f"{t.id} started behind its closed gate"
    assert not any(out2.commands.turbo_standby.values())
    # ---- turbos first when it is low on the FIRST pass (the 2026-10-28 behaviour, unchanged)
    sm = AutoStateMachine(cfg)
    out = _unit(cfg, sm, 0.5 * limit, _engage_prev(cfg))
    assert sm.mem.engage_order == "turbos_first" and out.substate == 6
    assert all(out.commands.turbo_motor.values()) and not any(out.commands.valves[g] for g in _gates(cfg))
    # ---- and the decision does not outlive Engage Turbo
    sm.step(AutoInput(Inputs(), Commands.all_off(cfg.valve_ids, cfg.turbo_ids), S.FACILITY_OFF, S.FACILITY_OFF,
                      0, set(), 2000.0, False, cfg.thresholds.turbo_on_threshold_torr, None, False))
    assert sm.mem.engage_order is None


def test_null_threshold_is_always_the_vi_order():
    cfg = _vc100()
    cfg.thresholds.turbo_start_before_gates_below_torr = None
    sm = AutoStateMachine(cfg)
    out = _unit(cfg, sm, to_torr("mbar", 1e-4), _engage_prev(cfg))
    assert sm.mem.engage_order == "gates_first" and not any(out.commands.turbo_motor.values())


# ------------------------------------------------------------------------- the wait is visible
NOTE = re.compile(r"Waiting for turbos to reach standby speed \((\d) of 6, (\d\d):(\d\d)\)")


def test_the_standby_wait_is_shown_with_count_and_time():
    cfg = _vc100(200.0)
    h = Harness(cfg)
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    for _ in range(3000):
        if h.step().substate == 3:
            break
    h.advance_plant(60)                                       # roughed to a few 1e-3 mBar
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    waits, opening, seen_count = [], [], []
    for _ in range(60000):
        s = h.step()
        if s.current == S.ENGAGE_TURBO and s.substate in (6, 7):
            m = NOTE.search(s.auto_note)
            if m:
                waits.append(int(m.group(2)) * 60 + int(m.group(3)))
                seen_count.append(int(m.group(1)))
                assert not any(s.commands.valves[g] for g in _gates(cfg))
            elif s.auto_note.startswith("Opening the gate valves one at a time"):
                opening.append(s.auto_note)
        if s.current == S.PUMPING_TO_HIGH_VAC:
            break
    else:
        raise AssertionError("high vacuum not reached")
    assert waits, "nothing on the panel while the turbos ran up behind closed gates"
    assert waits == sorted(waits) and waits[-1] > waits[0], "the clock on the panel did not run"
    assert seen_count == sorted(seen_count) and seen_count[0] < 6, "the count did not start below 6 and climb"
    assert opening and "of 6" in opening[0] and "leaves standby when its gate opens" in opening[0]
    assert s.auto_note == ""                                  # nothing to wait for in high vac
    assert h.ctl.snapshot().auto_note == ""


def test_the_note_names_the_turbos_still_running_up():
    cfg = _vc100()
    sm = AutoStateMachine(cfg)
    limit = cfg.thresholds.turbo_start_before_gates_below_torr
    out = _unit(cfg, sm, 0.5 * limit, _engage_prev(cfg))                 # -> substate 6, turbos started
    out = _unit(cfg, sm, 0.5 * limit, out.commands, sub=6)               # nothing turning yet
    assert out.note.startswith("Waiting for turbos to reach standby speed (0 of 6, 00:00)")
    assert "10:00" in out.note and "Turbo 1" in out.note and "Still running up" in out.note


def test_the_panel_shows_the_note():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from facility_control.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    cfg = _vc100()
    h = Harness(cfg)
    win = MainWindow(cfg, h.ctl, sim_backend=h.backend)
    h.step(2)
    h.ctl._auto_note = "Waiting for turbos to reach standby speed (4 of 6, 03:12) – gates stay closed until then"
    h.ctl._publish()
    win._poll()
    app.processEvents()
    assert "4 of 6, 03:12" in win.elapsed_label.text()
    win.close()
