"""Software bugs fixed after the protection review of 2026-11-27 (docs/protection_review).

Only what is a plain bug, or makes Auto obviously safer in a fault, was changed; what the review
calls a policy gap and Olgu had already decided (Admin has no interlocks, Manual/Admin have no
automatic trips, the settle waits stay as in LabVIEW, no restart recovery) was left alone - see
docs/protection_review/RESPONSE.md for every finding.

  1. A FAILED DAQ READ looked like a facility at rest.  The backend hands back what it has read up to
     the failure - often no turbo readings at all - and "no reading" counted as "not turning".  One
     failed read with the turbos at full speed: Auto (error 5020 -> Facility Off) switched the
     primary pump and the chiller OFF and shut the turbo valves in that pass, and on again in the
     next.  Now a failed read keeps the last known equipment readings.
  2. A speed that is not a usable number (not-a-number, infinite, far below zero) counted as
     "stopped" everywhere.  Now it counts as turning.
  3. Manual mode asked its interlock before showing 'Open Gate Valve?' and not again when the
     operator answered.  Now it is asked again with readings taken at that moment.
  4. 'Turbo on Threshold' accepted not-a-number / infinite / negative values (the exposure limit is
     derived from it); the profiles accepted them too.
  5. One failing output group (the valves) stopped the pump, chiller and turbo writes behind it.
  6. The controller timed its waits and trips with the time of day, which Windows can step.
  7. The RS-485 link check divided by zero on a fast PC; a stale speed reading is now flagged.
"""
import math
import time

import pytest

from conftest import Harness
from safety_rig import CHAMBERS, Rig
from test_nidaqmx_backend import FakeTask, fake_daq  # noqa: F401  (fixture)
from facility_control.automode import turbo_is_spinning
from facility_control.config import CONFIG_DIR, load_config
from facility_control.controller import AutoAnswerDialogs, Controller, DialogHandle
from facility_control.hal.base import HardwareBackend
from facility_control.model import (Commands, FacilityState as S, Inputs, Mode, TurboInputs,
                                    speed_says_turning)

ATM = 760.0
DAQ_ERROR = "DaqError: Some or all of the samples requested have not yet been acquired (-200284)"


# ============================================================================ 1. a failed DAQ read
def _fail_reads(r, n):
    """The next n reads fail the way NiDaqmxBackend.read() fails early: an error and nothing else."""
    real, left = r.ctl.backend.read, {"n": n}

    def read():
        real()                                           # (the plant moves on regardless)
        if left["n"] > 0:
            left["n"] -= 1
            return Inputs(t=0.0, daq_error=DAQ_ERROR)
        return real()
    r.ctl.backend.read = read


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("failed_reads", [1, 3, 120])
def test_a_failed_daq_read_does_not_take_backing_and_cooling_from_turbos_at_full_speed(which, failed_reads):
    r = Rig(which)
    r.to_high_vac()
    r.run(120.0)
    speeds0 = {t.id: r.st.turbos[t.id].speed_pct for t in r.turbos}
    assert all(v > 95 for v in speeds0.values())
    _fail_reads(r, failed_reads)
    s = r.tick()
    # the error is raised and Auto shuts down, as for any error ...
    assert s.error.code == 5020 and "DAQ read failed" in s.error.source
    assert s.current in (S.FACILITY_OFF, S.TURBO_SLOWING)
    assert not any(s.commands.turbo_motor.values())
    assert not any(s.commands.valves[t.gate_valve] for t in r.turbos)
    # ... but the turbos, last seen at full speed, keep their backing and their cooling
    assert s.commands.primary and s.commands.chiller, "backing / cooling dropped on a failed read"
    assert all(s.commands.valves[t.turbo_valve] for t in r.turbos), "a turbo valve was shut on a failed read"
    # ... for as long as the reads fail (the last known speed is all there is to go by)
    for _ in range(failed_reads - 1):
        s = r.tick()
        assert s.commands.primary and s.commands.chiller
        assert all(s.commands.valves[t.turbo_valve] for t in r.turbos)
    # readings back: the ordinary error shutdown carries on - backed and cooled until slowed
    for _ in range(30):
        s = r.tick()
        still = [t for t in r.turbos if r.st.turbos[t.id].speed_pct > 50.0]
        if still:
            assert s.commands.primary and s.commands.chiller, "dropped with a turbo above 50 %"
    r.assert_safe()                                        # no rotor exposed, braked by gas or left uncooled


def test_what_a_failed_read_keeps_and_what_it_does_not():
    r = Rig("vc100")
    r.to_high_vac()
    r.run(30.0)
    good = r.ctl.inputs
    assert good.turbos and good.primary_read and good.chiller_read and any(good.valve_reads.values())
    inp = r.ctl._usable_inputs(Inputs(daq_error=DAQ_ERROR))
    assert inp.daq_error == DAQ_ERROR
    assert set(inp.turbos) == set(good.turbos) and inp.turbos["turbo2"].speed_pct == good.turbos["turbo2"].speed_pct
    assert inp.primary_read and inp.chiller_read and inp.valve_reads == good.valve_reads
    # a pressure that was not read stays unknown - every rule treats that as "assume atmosphere"
    assert inp.pressures_torr == {} and math.isnan(inp.pressure(r.cfg.main_gauge.id))
    # what the failed read DID deliver is not overwritten
    part = Inputs(daq_error=DAQ_ERROR, valve_reads={"gate2": False}, turbos={"turbo2": TurboInputs(speed_pct=12.0)})
    part = r.ctl._usable_inputs(part)
    assert part.turbos["turbo2"].speed_pct == 12.0 and part.valve_reads["gate2"] is False
    # a good read is passed through untouched
    fresh = Inputs()
    assert r.ctl._usable_inputs(fresh) is fresh and not fresh.turbos


# ============================================================================ 2. an unusable speed
@pytest.mark.parametrize("speed, turning", [
    (0.0, False), (4.9, False), (5.1, True), (100.0, True),
    (-0.4, False), (-1.0, False),                 # a little below zero: noise on the +/-10 V input
    (-50.0, True),                                # far below zero: not a reading
    (math.nan, True), (math.inf, True), (-math.inf, True), (None, True), ("x", True)])
def test_a_speed_that_is_not_a_usable_number_never_says_stopped(speed, turning):
    assert speed_says_turning(speed, 5.0) is turning
    if speed is not None and not isinstance(speed, str):
        assert turbo_is_spinning(TurboInputs(speed_pct=speed), 5.0) is turning


def test_contact_turbos_still_go_by_their_rotating_contact():
    assert turbo_is_spinning(TurboInputs(speed_pct=math.nan, contacts={"rotating": True}), 5.0)
    assert not turbo_is_spinning(TurboInputs(speed_pct=math.nan, contacts={"rotating": False}), 5.0)


class Recording(HardwareBackend):
    """Hands back the Inputs the test holds; records what is written."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.input, self.writes, self.reads = Inputs(), [], 0

    def open(self): pass
    def close(self): pass
    def turbo_error_ack(self, *a): pass

    def read(self):
        self.reads += 1
        return self.input

    def write(self, cmds):
        self.writes.append(cmds.copy())


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


def manual_rig(mode=Mode.MANUAL, blocking=True, profile=None):
    cfg = load_config(profile) if profile else load_config()
    cfg.timings.blocking_waits = blocking
    backend, clock = Recording(cfg), Clock()
    ctl = Controller(cfg, backend, initial_mode=mode, dialogs=AutoAnswerDialogs(), clock=clock, sleep=clock.sleep)
    # a facility at rest under vacuum, everything read back as commanded
    inp = backend.input
    for g in cfg.gauges:
        inp.pressures_torr[g.id] = 1e-4
        inp.gauge_volts[g.id] = 5.0
    for vid in cfg.valve_ids:
        inp.valve_reads[vid] = False
    for t in cfg.turbos:
        inp.turbos[t.id] = TurboInputs(speed_pct=0.0)
    ctl.iterate()
    return cfg, backend, clock, ctl


@pytest.mark.parametrize("speed", ["missing", math.nan, math.inf, -50.0])
def test_manual_interlocks_do_not_take_an_unknown_speed_for_a_stopped_turbo(speed):
    cfg, b, clock, ctl = manual_rig()
    if speed == "missing":
        b.input.turbos.clear()
    else:
        b.input.turbos["turbo1"] = TurboInputs(speed_pct=speed)
    cmds = ctl.cmds
    assert not ctl.interlocks.chiller(False, cmds, b.input).allowed          # cooling stays
    assert not ctl.interlocks.primary(False, cmds, b.input).allowed          # backing stays
    b.input.pressures_torr[cfg.main_gauge.id] = ATM
    assert not ctl.interlocks.valve("gate", True, cmds, b.input).allowed     # no atmosphere on its inlet
    assert not ctl.interlocks.valve("vent", True, cmds, b.input).allowed
    # with a real zero reading the same actions are allowed, as before
    b.input.turbos["turbo1"] = TurboInputs(speed_pct=-0.3)
    assert ctl.interlocks.chiller(False, cmds, b.input).allowed
    assert ctl.interlocks.valve("gate", True, cmds, b.input).allowed


# ============================================================================ 3. Manual: asked again at the answer
class ChangesWhileOpen(AutoAnswerDialogs):
    """The operator answers Yes - but while the question was open, `meanwhile()` happened."""

    def __init__(self, meanwhile):
        super().__init__()
        self.meanwhile = meanwhile

    def ask(self, kind, message, buttons):
        if kind == "two":
            self.meanwhile()
        return super().ask(kind, message, buttons)


@pytest.mark.parametrize("blocking", [True, False])
def test_manual_gate_is_not_opened_if_the_chamber_came_up_while_the_question_was_open(blocking):
    cfg, b, clock, ctl = manual_rig(blocking=blocking)
    b.input.turbos["turbo1"] = TurboInputs(speed_pct=100.0)                   # rotor at full speed
    ctl.dialogs = ChangesWhileOpen(lambda: b.input.pressures_torr.__setitem__(cfg.main_gauge.id, ATM))
    ctl.request_user_cmd("valve:gate")
    for _ in range(3):
        ctl.iterate()
    assert [m for k, m in ctl.dialogs.log if k == "two"] == ["Open Gate Valve?"]
    assert not ctl.cmds.valves["gate"], "gate opened onto atmosphere with the rotor at full speed"
    assert not any(w.valves["gate"] for w in b.writes)
    refused = [m for k, m in ctl.dialogs.log if k == "info"]
    assert refused and "Main Facility Pressure Too High" in refused[-1]
    assert any("no longer allowed when the question was answered" in l for l in ctl.event_log)


def test_manual_gate_opens_as_before_when_nothing_changed():
    cfg, b, clock, ctl = manual_rig()
    b.input.turbos["turbo1"] = TurboInputs(speed_pct=100.0)
    ctl.request_user_cmd("valve:gate")
    ctl.iterate()
    assert ctl.cmds.valves["gate"] and not [m for k, m in ctl.dialogs.log if k == "info"]


def test_the_delayed_answer_of_the_real_window_is_judged_with_the_readings_of_that_moment():
    """Non-blocking mode, the answer arrives passes later (the review's reproduction)."""
    cfg, b, clock, ctl = manual_rig(blocking=False)

    class Delayed(AutoAnswerDialogs):
        def ask(self, kind, message, buttons):
            if kind != "two":
                return super().ask(kind, message, buttons)
            self.handle = DialogHandle()
            return self.handle
    ctl.dialogs = Delayed()
    b.input.turbos["turbo1"] = TurboInputs(speed_pct=100.0)
    ctl.request_user_cmd("valve:gate")
    ctl.iterate()
    assert ctl._pending is not None
    b.input.pressures_torr[cfg.main_gauge.id] = ATM
    for _ in range(5):
        ctl.iterate()
    ctl.dialogs.handle.resolve(0)
    ctl.iterate()
    assert not ctl.cmds.valves["gate"]


@pytest.mark.parametrize("target, setup, change, text", [
    # chiller off / primary off: allowed with the turbo stopped, not once it turns
    ("chiller", lambda c: setattr(c, "chiller", True), lambda b: b.input.turbos.__setitem__("turbo1", TurboInputs(speed_pct=60.0)),
     "wait for Turbo to slow down"),
    ("primary", lambda c: c.set_primary(True), lambda b: b.input.turbos.__setitem__("turbo1", TurboInputs(speed_pct=60.0)),
     "wait for Turbo to slow down"),
    # vent valve: allowed with everything shut, not once the gate reads open
    ("valve:vent", lambda c: None, lambda b: b.input.valve_reads.__setitem__("gate", True), "Close Gate Valve"),
])
def test_every_manual_question_is_judged_again_at_the_answer(target, setup, change, text):
    cfg, b, clock, ctl = manual_rig()
    setup(ctl.cmds)
    b.input.chiller_read = bool(ctl.cmds.chiller)
    before = ctl.cmds.copy()
    ctl.dialogs = ChangesWhileOpen(lambda: change(b))
    ctl.request_user_cmd(target)
    ctl.iterate()
    assert [m for k, m in ctl.dialogs.log if k == "two"], "the question must have been asked"
    assert ctl.cmds.chiller == before.chiller and ctl.cmds.primary == before.primary
    assert ctl.cmds.valves == before.valves
    assert any(text in m for k, m in ctl.dialogs.log if k == "info")


def test_manual_turbo_start_is_judged_again_at_the_answer():
    cfg, b, clock, ctl = manual_rig()
    c = ctl.cmds
    c.set_primary(True)
    c.chiller = True
    c.valves["turbo_valve"] = True
    b.input.valve_reads["turbo_valve"] = True
    b.input.chiller_read = True
    assert ctl.interlocks.turbo_motor("turbo1", True, c, b.input).allowed
    ctl.dialogs = ChangesWhileOpen(lambda: setattr(b.input, "chiller_read", False))   # the chiller trips meanwhile
    ctl.request_user_cmd("turbo:turbo1")
    ctl.iterate()
    assert not ctl.cmds.turbo_motor["turbo1"]
    assert any("Chiller" in m for k, m in ctl.dialogs.log if k == "info")


def test_admin_is_unchanged_no_interlock_before_or_after_the_question():
    cfg, b, clock, ctl = manual_rig(mode=Mode.ADMIN)
    b.input.turbos["turbo1"] = TurboInputs(speed_pct=100.0)
    b.input.pressures_torr[cfg.main_gauge.id] = ATM
    ctl.request_user_cmd("valve:gate")
    ctl.iterate()
    assert ctl.cmds.valves["gate"]                                             # by design: Admin has none
    assert "WARNING" in [m for k, m in ctl.dialogs.log if k == "two"][0]       # ... but the question says so


# ============================================================================ 4. numbers that are not numbers
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -1.0, 0.0, 5000.0, "abc", None])
def test_an_unusable_turbo_on_threshold_is_refused(value):
    cfg, b, clock, ctl = manual_rig()
    before, limit = ctl.turbo_on_threshold_torr, ctl.interlocks.exposure_limit_torr()
    ctl.request_set_threshold(value)
    ctl.iterate()
    assert ctl.turbo_on_threshold_torr == before and ctl.interlocks.exposure_limit_torr() == limit
    assert any("Turbo on Threshold not changed" in l for l in ctl.event_log)


def test_a_usable_turbo_on_threshold_is_taken_as_before():
    cfg, b, clock, ctl = manual_rig()
    ctl.request_set_threshold(0.3)
    ctl.iterate()
    assert ctl.turbo_on_threshold_torr == 0.3 and ctl.interlocks.turbo_on_threshold_torr == 0.3


@pytest.mark.parametrize("line", ["  turbo_spinning_pct: .nan ", "  turbo_spinning_pct: .inf ",
                                  "  turbo_spinning_pct: -15.0 ", "  turbo_spinning_pct: fifteen "])
@pytest.mark.parametrize("profile", ["facility_main_v4.4.yaml", "facility_vc100.yaml"])
def test_a_profile_with_an_unusable_number_does_not_load(tmp_path, profile, line):
    text = (CONFIG_DIR / profile).read_text(encoding="utf-8")
    import re
    changed, n = re.subn(r"(?m)^  turbo_spinning_pct:[^\n#]*", line, text, count=1)
    assert n == 1
    bad = tmp_path / profile
    bad.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="thresholds.turbo_spinning_pct"):
        load_config(str(bad))
    load_config(str(CONFIG_DIR / profile))                                     # the real ones load


# ============================================================================ 5. output writes
def test_a_failing_valve_write_does_not_stop_the_other_outputs_from_being_written(cfg, fake_daq):  # noqa: F811
    b = fake_daq.NiDaqmxBackend(cfg)
    b.open()
    t = FakeTask.registry
    n0 = {k: len(t[k].writes) for k in ("primary_cmd", "chiller_cmd", "turbo1_cmd")}

    def boom(*a, **k):
        raise RuntimeError("DAQmx -88710 device removed")
    t["valve_cmd"].write = boom
    off = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    with pytest.raises(RuntimeError, match="valves: RuntimeError: DAQmx -88710"):
        b.write(off)
    # the pump, the chiller and the turbo were still told
    assert all(len(t[k].writes) == n0[k] + 1 for k in n0), {k: len(t[k].writes) for k in n0}
    assert t["turbo1_cmd"].writes[-1] == [False, False] and t["chiller_cmd"].writes[-1] is False
    # two groups failing: both are named
    t["chiller_cmd"].write = boom
    with pytest.raises(RuntimeError, match="valves: .*chiller: "):
        b.write(off)


def test_a_failed_write_is_tried_again_on_the_next_pass_and_reported():
    cfg, b, clock, ctl = manual_rig(mode=Mode.AUTO)
    real, fail = b.write, {"n": 2}

    def write(cmds):
        if fail["n"] > 0:
            fail["n"] -= 1
            raise RuntimeError("output write failed - valves: DaqError")
        real(cmds)
    b.write = write
    n0 = len(b.writes)
    for _ in range(4):
        ctl.iterate()
    assert len(b.writes) - n0 == 2, "the full command set must be written again once the DAQ accepts it"
    assert any("DAQ write failed" in l for l in ctl.event_log)


# ============================================================================ 6. clocks
def test_waits_and_trips_are_timed_with_a_clock_that_windows_cannot_step():
    cfg = load_config()
    ctl = Controller(cfg, Recording(cfg))
    assert ctl.clock is time.monotonic and ctl.wall is time.time
    c = Clock()
    ctl = Controller(cfg, Recording(cfg), clock=c)                             # tests, simulator: one clock
    assert ctl.clock is c and ctl.wall is c


def test_the_turbo_engage_time_is_still_a_time_of_day(cfg):
    """Overnight Pump waits for the date and time the operator set - compared with the wall clock,
    whatever the timers' clock reads."""
    h = Harness(cfg)
    h.step(2)
    epoch = 1_800_000_000.0
    wall = {"t": epoch}
    h.ctl.wall = lambda: wall["t"]
    assert h.ctl.clock() < 1e9 or True
    h.ctl.request_set_engage_time(epoch + 3600.0)
    h.ctl.request_auto_button("overnight_pump")
    s, _ = h.run_until(lambda s: s.current == S.OVERNIGHT_PUMP, max_iter=40000)
    s = h.step(50)
    assert s.current == S.OVERNIGHT_PUMP, "must wait: the engage time is an hour away"
    wall["t"] = epoch + 3601.0
    s, _ = h.run_until(lambda s: s.current != S.OVERNIGHT_PUMP, max_iter=2000)
    assert s.target == S.PUMPING_TO_HIGH_VAC


# ============================================================================ 7. the RS-485 link
def test_link_validation_does_not_divide_by_zero_when_the_pings_take_no_measurable_time(monkeypatch):
    from facility_control.drivers import pfeiffer_pv as pv
    sim = pv.SimBus({1: pv.SimTC400(1)}, delay_s=0.0)
    bus = pv.PVBus("SIMZ", serial_obj=sim)
    bus.gap_s = 0.0
    pump = pv.HiPace(bus, 1)

    class Frozen:
        """time.monotonic that does not move (a coarse Windows timer between two calls)."""
        monotonic = staticmethod(lambda: 5.0)
        time = staticmethod(time.time)
        sleep = staticmethod(lambda s: None)
    monkeypatch.setattr(pv, "time", Frozen)
    checks = pv.validate(pump, pings=5)
    link = [c for c in checks if c.name == "Link quality"][0]
    assert "rate not measurable" in link.detail and "5/5 replies ok" in link.detail


def test_a_speed_that_is_no_longer_refreshed_is_flagged_while_the_pump_answers_everything_else(monkeypatch):
    from test_hipace_rs485_facility import _bus, _mgr
    from facility_control.drivers.pfeiffer_pv import PVTimeout
    clock = Clock()
    m = _mgr(_bus(1), clock=clock)
    u = m.units["turbo1"]
    u.p["comm_warn_s"] = 3.0
    u.p["comm_timeout_s"] = 20.0
    m.poll_once()
    old = m.inputs("turbo1")
    assert old.comm_ok and not old.comm_warning
    real = u.pump.read

    def no_speed(param):
        if param == 309:
            raise PVTimeout("injected: no reply to the speed query")
        return real(param)
    monkeypatch.setattr(u.pump, "read", no_speed)
    for _ in range(10):
        clock.sleep(1.0)
        m.poll_once()
    ti = m.inputs("turbo1")
    assert ti.speed_pct == old.speed_pct                    # the last speed is still shown ...
    assert ti.comm_warning and ti.comm_silent_s >= 9.0       # ... and marked as old
    assert ti.comm_ok and not ti.error                       # not an error: see the driver's comment
    monkeypatch.setattr(u.pump, "read", real)
    clock.sleep(1.0)
    m.poll_once()
    assert not m.inputs("turbo1").comm_warning
