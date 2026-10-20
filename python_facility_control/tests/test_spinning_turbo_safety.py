"""Auto-mode plant-safety invariants, from a real incident on the small chamber (2026-09-22).

The facility was at ~2e-5 mBar in Pumping to High Vac.  The operator pressed Shutdown (gate closed,
turbo spinning down as expected), then pressed Pump to High Vac while it was still slowing.  The VI's
own logic - faithfully ported - did this:

    Main_V4.4.vi, frame "Turbo slowing":
        AUTO.TURBO_VALVE_CMD = Select(f="True",  sel=Pump to High Vac 2, t="False")   -> valve CLOSED
        AUTO.TURBO_MOTOR_CMD = Select(f="False", sel=Pump to High Vac 2, t="True")    -> motor ON
        AUTO.GATE_CMD        = "False"
        AUTO.CURRENT_STATE   = 1 (Pumping to Rough)

so the turbo re-accelerated to full speed with its gate AND its turbo valve shut - compressing into
a dead volume - and the Pumping to Rough state then started the primary and opened the bypass into a
chamber at 1e-4 mBar, pushing foreline gas back into it.  The operator had to switch to Admin to
save the hardware.

Three rules now prevent that, and they are enforced globally rather than per button, because the
same shapes exist on other paths (leaving Venting, and on the VC100 profile).
"""
import pytest

from facility_control.config import CONFIG_DIR, load_config
from facility_control.model import FacilityState as S, Mode
from facility_control.units import torr_to
from tests.conftest import Harness


def _to_high_vac(h):
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 40000)
    h.run_until(lambda s: h.ctl._turbo_views["turbo1"].speed_pct > 99, 100000)
    return h.run_until(lambda s: h.ctl.inputs.pressures_torr["wrg"] < 1e-3 * 0.75, 300000)[0]


# --------------------------------------------------------------------------- the incident itself
def test_the_2026_09_22_incident_end_to_end(cfg):
    h = Harness(cfg, mode=Mode.AUTO)
    _to_high_vac(h)

    h.ctl.request_auto_button("shutdown")
    h.step(2)
    s, _ = h.run_until(lambda s: s.current == S.TURBO_SLOWING, 40000)
    assert h.ctl._turbo_views["turbo1"].speed_pct > 20, "need a genuinely spinning turbo for this test"
    assert s.commands.valves["turbo_valve"], "the slowing turbo must keep its backing path"

    h.ctl.request_auto_button("pump_to_high_vac")
    s = h.step(2)
    # 1. it must NOT strand the spinning turbo
    assert s.commands.valves["turbo_valve"] is True, "a spinning turbo was isolated from its backing"
    assert s.commands.turbo_motor["turbo1"] is True
    # 2. it must NOT go roughing a chamber that never lost its vacuum
    assert s.current == S.ENGAGE_TURBO and s.target == S.PUMPING_TO_HIGH_VAC
    assert any("re-engaging the turbo without roughing" in l for l in s.event_log)

    # 3. and the bypass must never open while the chamber is below the foreline
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 40000)
    assert s.commands.valves["gate"] and s.commands.valves["turbo_valve"]
    assert not s.commands.valves["bypass"]


# --------------------------------------------------------------------------- rule 1
def test_a_spinning_turbo_never_loses_its_turbo_valve(cfg):
    """Swept over the whole spin-down: at no point may the valve be shut while the rotor turns."""
    h = Harness(cfg, mode=Mode.AUTO)
    _to_high_vac(h)
    h.ctl.request_auto_button("shutdown")
    h.step(2)
    checked_spinning = False
    for _ in range(40000):
        s = h.step()
        spd = h.ctl._turbo_views["turbo1"].speed_pct
        if spd > cfg.thresholds.slowing_pct:
            checked_spinning = True
            assert s.commands.valves["turbo_valve"], \
                f"turbo valve closed at {spd:.0f} % in state {s.current.label}"
        if s.current == S.FACILITY_OFF and spd <= cfg.thresholds.turbo_slow_speed_pct:
            break
    assert checked_spinning
    assert not s.commands.valves["turbo_valve"], "once stopped, everything closes as before"


def test_pressing_high_vac_out_of_venting_also_keeps_the_valve(cfg):
    """The same shape exists on the Venting page - the global rule must cover it too."""
    h = Harness(cfg, mode=Mode.AUTO)
    _to_high_vac(h)
    h.ctl.request_auto_button("vent")
    s, _ = h.run_until(lambda s: s.current == S.VENTING, 40000)
    assert h.ctl._turbo_views["turbo1"].speed_pct > cfg.thresholds.slowing_pct
    h.ctl.request_auto_button("pump_to_high_vac")
    s = h.step(2)
    assert s.commands.valves["turbo_valve"], "a spinning turbo was isolated on the way out of Venting"


# --------------------------------------------------------------------------- rule 3
def test_bypass_never_opens_into_a_chamber_below_the_foreline(cfg):
    h = Harness(cfg, mode=Mode.AUTO)
    _to_high_vac(h)
    h.ctl.request_auto_button("shutdown")
    h.step(2)
    h.run_until(lambda s: s.current == S.TURBO_SLOWING, 40000)
    h.ctl.request_auto_button("pump_to_high_vac")
    for _ in range(20000):
        s = h.step()
        if s.commands.valves["bypass"]:
            wrg = h.ctl.inputs.pressures_torr["wrg"]
            fore = h.ctl.inputs.pressures_torr["conv2"]
            assert wrg >= fore, (f"bypass opened with the chamber at {torr_to('mbar', wrg):.1e} mBar "
                                 f"below the foreline at {torr_to('mbar', fore):.1e} mBar")
        if s.current == S.PUMPING_TO_HIGH_VAC:
            break


def test_a_normal_pump_down_from_atmosphere_is_unaffected(cfg):
    """The guards must not disturb the ordinary case: at atmosphere the bypass opens as always."""
    h = Harness(cfg, mode=Mode.AUTO)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, _ = h.run_until(lambda s: s.commands.valves["bypass"], 4000)
    assert h.ctl.inputs.pressures_torr["wrg"] > 1.0          # still near atmosphere
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 100000)
    assert s.commands.valves["gate"] and s.commands.turbo_motor["turbo1"]


def test_an_already_open_bypass_is_never_forced_shut(cfg):
    """The rule blocks OPENING only - roughing must not be interrupted as the chamber passes the foreline."""
    h = Harness(cfg, mode=Mode.AUTO)
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    s, _ = h.run_until(lambda s: s.commands.valves["bypass"], 4000)
    opened_then_shut = False
    for _ in range(20000):
        prev = s.commands.valves["bypass"]
        s = h.step()
        if prev and not s.commands.valves["bypass"] and s.current == S.PUMPING_TO_ROUGH:
            opened_then_shut = True
            break
    assert not opened_then_shut, "the guard must not slam a bypass that is already doing its job"


# --------------------------------------------------------------------------- the other chamber
def test_the_same_rules_hold_on_the_vc100():
    vc = load_config(str(CONFIG_DIR / "facility_vc100.yaml"))
    vc.simulation.time_scale = 400.0
    h = Harness(vc, mode=Mode.AUTO)
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, 60000)
    # NB turbo1 is the Shimadzu contact interface: it reports no speed at all, so "spinning" has to
    # come from the state machine's own test (the Rotating contact), not from speed_pct.
    from facility_control.automode import turbo_is_spinning
    def all_spinning(_s):
        return all(turbo_is_spinning(h.ctl.inputs.turbos.get(t.id), vc.thresholds.slowing_pct)
                   for t in vc.turbos)
    h.run_until(all_spinning, 200000)
    h.ctl.request_auto_button("shutdown")
    h.step(2)
    s, _ = h.run_until(lambda s: s.current == S.TURBO_SLOWING, 40000)
    from facility_control.automode import turbo_is_spinning
    spinning = [t for t in vc.turbos
                if turbo_is_spinning(h.ctl.inputs.turbos.get(t.id), vc.thresholds.slowing_pct)]
    assert spinning, "at least one turbo must still be turning for this test to mean anything"
    h.ctl.request_auto_button("pump_to_high_vac")
    s = h.step(2)
    for t in spinning:
        assert s.commands.valves[t.turbo_valve], f"{t.label} was isolated while spinning"


# --------------------------------------------------------------------------- 2026-09-23 incident
# Overnight Pump was pressed from high vacuum.  Main_V4.4's Overnight frame runs all_off(), so the
# primary pump stopped while the turbo was still at ~99 %.  The turbo valve rule added on 2026-09-22
# then held that valve OPEN onto a foreline with nothing pumping it, so the foreline backfilled
# towards atmosphere and dragged the turbo body up with it (~27 mBar) until the drive tripped
# (error 5007).  Holding a turbo valve open is only protective if something is actually backing it:
# the rule now keeps the primary running too, and the profile enables the VC100's overnight-backing
# behaviour so the state expresses the intent itself.
def test_overnight_keeps_backing_until_the_turbo_has_slowed(cfg):
    h = Harness(cfg, mode=Mode.AUTO)
    _to_high_vac(h)
    h.ctl.request_set_engage_time(h.ctl.clock() + 1e6)      # far out: we only care about the spin-down
    h.ctl.request_auto_button("overnight_pump")
    h.step(3)
    h.run_until(lambda s: s.current == S.OVERNIGHT_PUMP, 40000)
    h.backend.sim.time_scale = 25.0                          # slow the plant so the spin-down is real

    saw_spinning = False
    for _ in range(60000):
        s = h.step()
        spd = h.ctl._turbo_views["turbo1"].speed_pct
        fore = h.ctl.inputs.pressures_torr["conv2"]
        body = h.ctl.inputs.pressures_torr["conv1"]
        if spd > cfg.thresholds.slowing_pct:
            saw_spinning = True
            assert s.commands.primary, f"primary stopped with the turbo still at {spd:.0f} %"
            assert s.commands.valves["turbo_valve"], f"turbo isolated at {spd:.0f} %"
            assert fore < 1.0, f"foreline backfilled to {torr_to('mbar', fore):.1e} mBar at {spd:.0f} %"
            assert body < 1.0, f"turbo body reached {torr_to('mbar', body):.1e} mBar at {spd:.0f} %"
        assert not s.error.status, f"{s.error.code}: {s.error.source}"
        if spd <= cfg.thresholds.turbo_slow_speed_pct:
            break
    assert saw_spinning
    # ...and once it really has stopped, Overnight is a cold hold: everything off
    s, _ = h.run_until(lambda s: not s.commands.primary, 20000)
    assert not any(s.commands.valves.values()) and not s.commands.chiller


def test_a_turbo_valve_is_never_held_open_onto_a_dead_foreline(cfg):
    """The half-rule that caused the 5007 trip: open valve + primary off is worse than isolating."""
    h = Harness(cfg, mode=Mode.AUTO)
    _to_high_vac(h)
    h.ctl.request_set_engage_time(h.ctl.clock() + 1e6)
    h.ctl.request_auto_button("overnight_pump")
    h.step(3)
    h.backend.sim.time_scale = 25.0
    for _ in range(60000):
        s = h.step()
        if s.commands.valves["turbo_valve"]:
            assert s.commands.primary, \
                "a turbo valve was held open with no backing pump running"
        if h.ctl._turbo_views["turbo1"].speed_pct <= cfg.thresholds.turbo_slow_speed_pct:
            break


def test_overnight_then_timestamp_restarts_cleanly(cfg):
    """The second half of the report: when the engage time arrives the facility must come back up
    without ever exposing the turbo to a backfilled foreline."""
    h = Harness(cfg, mode=Mode.AUTO)
    _to_high_vac(h)
    h.ctl.request_set_engage_time(h.ctl.clock() + 400.0)
    h.ctl.request_auto_button("overnight_pump")
    h.step(3)
    s, _ = h.run_until(lambda s: s.current != S.OVERNIGHT_PUMP, 200000)
    for _ in range(200000):
        s = h.step()
        body = h.ctl.inputs.pressures_torr["conv1"]
        spd = h.ctl._turbo_views["turbo1"].speed_pct
        if spd > cfg.thresholds.slowing_pct:
            assert body < cfg.thresholds.turbo_high_pressure_shutoff_torr, \
                f"turbo at {spd:.0f} % saw {torr_to('mbar', body):.1e} mBar"
        assert not s.error.status, f"{s.error.code}: {s.error.source}"
        if s.current == S.PUMPING_TO_HIGH_VAC and spd > 95:
            break
    assert s.commands.valves["gate"] and s.commands.valves["turbo_valve"]
