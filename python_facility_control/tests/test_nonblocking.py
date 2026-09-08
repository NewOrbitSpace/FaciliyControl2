"""Optional non-blocking mode (`timings.blocking_waits: false`): the loop keeps reading while a
device settles or a dialog is open.  Not the default – see test_blocking.py for the VI behaviour."""
import time

import pytest

from facility_control.controller import DEFER, AutoAnswerDialogs, Controller
from facility_control.hal.sim_backend import SimBackend
from facility_control.model import Mode
from tests.conftest import Harness


@pytest.fixture
def cfg(cfg):
    cfg.timings.blocking_waits = False
    return cfg


def test_readings_continue_during_chiller_and_gate_settle(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("chiller")
    s = h.tick()                                   # decision iteration: chiller commanded
    assert s.commands.chiller and s.hold_remaining_s > 9.0 and "Chiller" in s.hold_reason
    iterations, times, ok = 0, set(), True
    while h.ctl.holding:
        s = h.tick()
        iterations += 1
        times.add(round(s.inputs.t, 3))
        # fresh readings every iteration, no conflict error while the chiller starts
        ok = ok and not s.error.status
        assert iterations < 5000
    assert iterations > 3 and len(times) > 3 and ok
    # a request made during the hold is executed right after it (like the VI's latched buttons)
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("valve:gate")
    s = h.tick()
    assert s.commands.valves["gate"] is True and s.hold_remaining_s > 8.0


def test_readings_continue_while_dialog_open_and_answer_applied_later(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(DEFER)
    h.ctl.request_user_cmd("valve:bypass")
    s = h.tick()
    assert s.dialog_pending and "Open Bypass Valve?" in s.dialog_message
    first_t = s.inputs.t
    for _ in range(5):
        s = h.tick()
    assert s.inputs.t > first_t and s.dialog_pending            # loop kept running, dialog still open
    assert s.commands.valves["bypass"] is False
    kind, msg, handle = h.dialogs.pending[-1]
    handle.resolve(0)                                            # operator clicks 'Yes'
    s = h.tick()
    assert s.commands.valves["bypass"] is True and not s.dialog_pending


def test_error_ack_pulse_is_timed_not_blocking(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.ctl.request_turbo_error_ack("turbo1")
    s = h.tick()
    assert s.turbo_error_ack_active["turbo1"] is True
    n = 0
    while h.ctl.snapshot().turbo_error_ack_active["turbo1"] and n < 2000:
        h.tick()
        n += 1
    assert 0 < n < 2000


def test_threaded_loop_keeps_period_during_settle(cfg):
    """Real thread, real clock: iterations keep coming at ~10 Hz while a 10 s chiller settle runs."""
    cfg.simulation.time_scale = 1.0
    backend = SimBackend(cfg)
    dialogs = AutoAnswerDialogs()
    ctl = Controller(cfg, backend, dialogs=dialogs, initial_mode=Mode.ADMIN)
    assert ctl.blocking is False
    ctl.start()
    try:
        time.sleep(0.5)
        dialogs.answers.put(True)
        ctl.request_user_cmd("chiller")
        time.sleep(0.3)
        s0 = ctl.snapshot()
        assert s0.commands.chiller and s0.hold_remaining_s > 8
        time.sleep(1.0)
        s1 = ctl.snapshot()
        assert s1.iteration - s0.iteration >= 7          # ~10 iterations expected in 1 s
        assert s1.inputs.t > s0.inputs.t
    finally:
        ctl.request_stop()
        ctl.join(10)
