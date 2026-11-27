"""R01/R03/R07/R11/R12: proposed requirements; strict xfails are OPEN defects.

Pure records and a recording backend only. No real driver, port or application.

Updated 2026-11-28 after the fixes (tests/test_review_fixes.py, docs/protection_review/RESPONSE.md):
the cases that are fixed now simply pass; the strict xfails that are left are NOT fixed, each for
the reason given in its marker - either a decision already taken by Olgu, or a question put to him.
"""
import math
import random
import pytest
from facility_control.config import load_config
from facility_control.controller import Controller, AutoAnswerDialogs
from facility_control.interlocks import Interlocks
from facility_control.model import Commands, Inputs, TurboInputs, Mode, FacilityState, primary_is_running
from facility_control.hal.base import HardwareBackend
from facility_control.automode import turbo_is_spinning

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def sleep(self, dt): self.t += dt

class RecordingBackend(HardwareBackend):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.input = Inputs()
        self.writes = []
        self.reads = 0
    def open(self): pass
    def close(self): pass
    def read(self):
        self.reads += 1
        return self.input
    def write(self, commands): self.writes.append(commands.copy())
    def turbo_error_ack(self, *args): pass

def rig(mode=Mode.MANUAL):
    cfg = load_config()
    backend = RecordingBackend(cfg)
    clock = Clock()
    ctl = Controller(cfg, backend, initial_mode=mode, dialogs=AutoAnswerDialogs(),
                     clock=clock, sleep=clock.sleep)
    return cfg, backend, clock, ctl

# (-1.0 was in this list: one per cent below zero is ordinary noise on the +/-10 V speed input and is
#  a stopped turbo; a reading far below zero is what cannot be one.  FIXED 2026-11-28.)
@pytest.mark.parametrize('speed', [None, math.nan, -50.0])
def test_R07_unknown_speed_cannot_authorize_cooling_off(speed):
    cfg, b, clock, ctl = rig()
    if speed is not None:
        b.input.turbos['turbo1'] = TurboInputs(speed_pct=speed)
    assert not ctl.interlocks.chiller(False, ctl.cmds, b.input).allowed

@pytest.mark.parametrize('speed', [None, math.nan, -50.0])                # FIXED 2026-11-28
def test_R07_unknown_speed_cannot_authorize_atmospheric_exposure(speed):
    cfg, b, clock, ctl = rig()
    b.input.pressures_torr[cfg.main_gauge.id] = 760.0
    if speed is not None:
        b.input.turbos['turbo1'] = TurboInputs(speed_pct=speed)
    assert not ctl.interlocks.valve('gate', True, ctl.cmds, b.input).allowed

@pytest.mark.xfail(strict=True, reason='F01, OPEN - asked of Olgu: the small chamber\'s still-spinning contact was '
                                       'only DISPLAYED by LabVIEW; using it in Auto needs the engineers to confirm '
                                       'what it does first (a contact stuck on would keep the pumps on for ever)')
def test_R07_rotating_contact_overrides_zero_analog_speed():
    assert turbo_is_spinning(TurboInputs(speed_pct=0, still_spinning=True), 5)

@pytest.mark.xfail(strict=True, reason='F02, NOT CHANGED by decision (Olgu, 18 Nov: "we leave manual and admin '
                                       'mode alone"): Admin has no interlocks, as in LabVIEW; the question warns')
def test_R11_admin_cannot_start_unbacked_uncooled_turbo():
    cfg, b, clock, ctl = rig(Mode.ADMIN)
    ctl.request_user_cmd('turbo:turbo1')
    ctl.iterate()
    assert not ctl.cmds.turbo_motor['turbo1']

@pytest.mark.xfail(strict=True, reason='F03 = KI-6, NOT CHANGED by decision (Olgu, 18 Nov: "fine for now"): the '
                                       'small chamber has no backing-pump feedback unless the frequency reader is on')
def test_R03_missing_backing_feedback_is_not_running():
    cfg, b, clock, ctl = rig()
    ctl.cmds.set_primary(True)
    assert not primary_is_running(cfg.primary, b.input, ctl.cmds)

@pytest.mark.parametrize('value', [math.nan, math.inf, -1.0])
def test_R12_reject_invalid_runtime_threshold(value):
    cfg, b, clock, ctl = rig()
    before = ctl.turbo_on_threshold_torr
    ctl.request_set_threshold(value)
    ctl.iterate()
    assert ctl.turbo_on_threshold_torr == before

@pytest.mark.xfail(strict=True, reason='F05, NOT CHANGED: readings carry no age that the interlocks could check. '
                                       'The way an old pressure really got used - a question left open - is fixed '
                                       '(the interlock is asked again at the answer, with a fresh read)')
def test_R07_old_pressure_cannot_authorize_gate_open():
    cfg, b, clock, ctl = rig()
    b.input.t = clock() - 10000
    b.input.pressures_torr[cfg.main_gauge.id] = 1e-4
    b.input.turbos['turbo1'] = TurboInputs(speed_pct=100)
    assert not ctl.interlocks.valve('gate', True, ctl.cmds, b.input).allowed

def test_R05_configured_exposure_boundary():
    cfg, b, clock, ctl = rig()
    b.input.turbos['turbo1'] = TurboInputs(speed_pct=100)
    limit = ctl.interlocks.exposure_limit_torr()
    for p, allowed in [(math.nextafter(limit, 0), True), (limit, True),
                       (math.nextafter(limit, math.inf), False), (760, False), (math.nan, False)]:
        b.input.pressures_torr[cfg.main_gauge.id] = p
        assert ctl.interlocks.valve('gate', True, ctl.cmds, b.input).allowed == allowed

def test_R11_generated_manual_valve_sequences_and_mutation(monkeypatch):
    # State-machine exploration at the authorization layer: persistent rotor at
    # full speed, chamber at atmosphere; valve states evolve after allowed requests.
    cfg, b, clock, ctl = rig()
    b.input.turbos['turbo1'] = TurboInputs(speed_pct=100)
    b.input.pressures_torr[cfg.main_gauge.id] = 760.0
    def explore():
        rng = random.Random(615083)
        c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
        for _ in range(500):
            vid = rng.choice(cfg.valve_ids)
            want = bool(rng.getrandbits(1))
            if ctl.interlocks.valve(vid, want, c, b.input).allowed:
                c.valves[vid] = want
            assert not c.valves['gate'], 'spinning rotor exposed to atmosphere'
    explore()
    # Test-only interlock mutation: the same invariant must detect the bypass.
    from facility_control.interlocks import Verdict
    monkeypatch.setattr(ctl.interlocks, 'valve', lambda *a: Verdict(True))
    with pytest.raises(AssertionError, match='exposed'):
        explore()

def test_blocking_wait_records_no_new_acquisition():
    cfg, b, clock, ctl = rig()
    before = clock()
    ctl._freeze(9, 'test only')
    assert clock() == pytest.approx(before + 9)
    assert b.reads == 0  # Characterization of F06, NOT a passing protection requirement.

def test_R11_manual_gate_confirmation_rechecks_pressure():                 # FIXED 2026-11-28
    from facility_control.controller import DialogHandle
    cfg, b, clock, ctl = rig()
    ctl.blocking = False
    class Delayed:
        def ask(self, kind, *args):
            if kind != 'two':                         # (the refusal that now follows is an 'info' box)
                return DialogHandle(None, done=True)
            self.handle = DialogHandle()
            return self.handle
    ctl.dialogs = Delayed()
    b.input.turbos['turbo1'] = TurboInputs(speed_pct=100)
    b.input.pressures_torr[cfg.main_gauge.id] = 1e-4
    ctl.request_user_cmd('valve:gate')
    ctl.iterate()
    assert ctl._pending is not None
    b.input.pressures_torr[cfg.main_gauge.id] = 760.0
    ctl.dialogs.handle.resolve(0)
    ctl.iterate()
    assert not ctl.cmds.valves['gate'], 'gate opened onto atmosphere after delayed confirmation'

def test_R08_partial_write_is_not_atomic():
    cfg, b, clock, ctl = rig()
    physical = {'valve_written': False, 'motor_stopped': False}
    def fail_partway(cmds):
        physical['valve_written'] = True
        raise OSError('injected failure before remaining output groups')
    b.write = fail_partway
    prev = ctl.cmds.copy()
    ctl._command(prev, prev.copy(), clock())
    assert ctl.error.code == 5020
    assert physical == {'valve_written': True, 'motor_stopped': False}
    # Characterization: output error is visible, but remaining writes never happen.

def test_R09_partial_serial_success_does_not_refresh_speed(monkeypatch):   # FLAGGED since 2026-11-28
    from test_hipace_rs485_facility import _mgr, _bus
    from facility_control.drivers.pfeiffer_pv import PVTimeout
    clock = Clock()
    manager = _mgr(_bus(1), clock=clock)
    unit = manager.units['turbo1']
    manager.poll_once()
    old_speed = manager.inputs('turbo1').speed_pct
    original_read = unit.pump.read
    def missing_speed(parameter):
        if parameter == 309:
            raise PVTimeout('injected missing speed response')
        return original_read(parameter)
    monkeypatch.setattr(unit.pump, 'read', missing_speed)
    for _ in range(10):
        clock.sleep(1)
        manager.poll_once()
    value = manager.inputs('turbo1')
    assert value.speed_pct == old_speed
    # Flagged as old (shown on the card; the speed-drop trip does not judge it) - deliberately not an
    # error: on 2026-10-28 pumps answered writes while their query replies stalled, and taking a
    # healthy turbo out of service for that would be worse than showing an old number.
    assert value.comm_warning and value.comm_silent_s >= 9

@pytest.mark.parametrize('previous', list(FacilityState))
def test_R01_restart_loses_state_without_reconciliation(previous):
    cfg, b, clock, ctl = rig()
    ctl.current = previous
    ctl.cmds.chiller = ctl.cmds.primary = True
    b.input.turbos['turbo1'] = TurboInputs(speed_pct=100)
    restarted = Controller(cfg, b, clock=clock, sleep=clock.sleep)
    assert restarted.mode == Mode.INITIALIZE
    assert restarted.current == FacilityState.FACILITY_OFF
    assert not restarted.cmds.chiller
    # Characterization only: recovery state / retained services are missing.
