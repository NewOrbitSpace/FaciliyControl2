"""Default behaviour = the VI: the control loop freezes for every settle wait and dialog.

While a device settles nothing is read, so the command/read cross-check only happens once the
device had its full settle time (an actuation is never flagged as a conflict too early).
The harness shortens every sleep to 0.5 ms and runs the plant 200x faster, so a 10 s freeze takes
~50 ms of wall time and the plant still sees ~10 s.
"""
import threading
import time

from facility_control.controller import DEFER, AutoAnswerDialogs, Controller
from facility_control.hal.sim_backend import SimBackend
from facility_control.model import Mode
from tests.conftest import Harness


def test_default_is_blocking(cfg):
    assert cfg.timings.blocking_waits is True
    h = Harness(cfg, mode=Mode.ADMIN)
    assert h.ctl.blocking is True


def test_chiller_command_freezes_loop_for_settle_time(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    it0, sim0 = h.ctl.iteration, h.backend._sim_time
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("chiller")
    t = time.perf_counter()
    s = h.tick()                                    # decision + 10 s chiller settle, in ONE iteration
    wall = time.perf_counter() - t
    assert s.commands.chiller is True
    assert s.iteration == it0 + 1                    # no iteration ran during the wait
    assert not s.loop_blocked and s.hold_remaining_s == 0 and not h.ctl.holding
    assert wall >= 100 * 0.0005 * 0.9                # 100 sleep slices of the shortened sleep
    s = h.tick()                                     # first read after the wait: plant advanced ~10 s
    assert h.backend._sim_time - sim0 >= 8.0
    assert s.inputs.chiller_read is True and not s.error.status
    assert not h.ctl._suppress_until                 # blocking mode needs no cross-check suppression


def test_no_early_conflict_error_for_slow_valve_and_gate(cfg):
    """The gate takes several seconds to move; because the loop is frozen for 9 s the first
    cross-check after the command sees the valve in position -> no error 5010."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    for target in ("valve:gate", "valve:bypass", "primary"):
        h.dialogs.answers.put(True)
        h.ctl.request_user_cmd(target)
        s = h.tick()
        assert not s.error.status, (target, s.error)
        s = h.tick()
        assert not s.error.status, (target, s.error)
    assert s.commands.valves["gate"] and s.commands.valves["bypass"] and s.commands.primary
    assert s.inputs.valve_reads["gate"] and s.inputs.valve_reads["bypass"]
    # the two-relay pump is still sequencing (power on, run relay after the gap) – it is not yet
    # turning, and that must NOT be reported as a conflict
    assert s.primary_phase == "powering" and not s.primary_running and not s.error.status
    s, _ = h.run_until(lambda s: s.primary_running, 4000)
    assert s.commands.primary_power and s.commands.primary_run and not s.error.status


def test_dialog_freezes_loop_until_answered(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(DEFER)
    h.ctl.request_user_cmd("valve:bypass")
    th = threading.Thread(target=h.ctl.iterate, daemon=True)
    th.start()
    for _ in range(400):                             # wait until the dialog is open
        if h.dialogs.pending:
            break
        time.sleep(0.005)
    assert h.dialogs.pending
    time.sleep(0.05)
    assert th.is_alive()                             # the iteration has not finished: loop frozen
    s = h.ctl.snapshot()
    assert s.dialog_pending and s.loop_blocked and "Open Bypass Valve?" in s.dialog_message
    assert s.commands.valves["bypass"] is False
    kind, msg, handle = h.dialogs.pending[-1]
    handle.resolve(0)                                # operator clicks 'Yes'
    th.join(5.0)
    assert not th.is_alive()
    s = h.ctl.snapshot()
    assert s.commands.valves["bypass"] is True and not s.dialog_pending and not s.loop_blocked


def test_stop_request_releases_frozen_dialog(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.dialogs.answers.put(DEFER)
    h.ctl.request_user_cmd("valve:bypass")
    th = threading.Thread(target=h.ctl.iterate, daemon=True)
    th.start()
    for _ in range(400):
        if h.dialogs.pending:
            break
        time.sleep(0.005)
    h.ctl.request_stop()
    th.join(5.0)
    assert not th.is_alive()
    assert h.ctl.snapshot().commands.valves["bypass"] is False     # counted as 'No' / window closed


def test_error_ack_pulse_is_a_blocking_1s_pulse(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    seen = []
    orig = h.backend.turbo_error_ack
    h.backend.turbo_error_ack = lambda tid, level: (seen.append((tid, level)), orig(tid, level))
    h.ctl.request_turbo_error_ack("turbo1")
    t = time.perf_counter()
    s = h.tick()
    wall = time.perf_counter() - t
    assert seen == [("turbo1", True), ("turbo1", False)]           # high, wait, low – in one iteration
    assert wall >= 10 * 0.0005 * 0.9                                # 1 s = 10 slices
    assert s.turbo_error_ack_active["turbo1"] is False and not h.ctl._ack_until


def test_threaded_loop_really_pauses(cfg):
    """Real thread, real clock: after a chiller command no iteration runs for ~10 s."""
    cfg.simulation.time_scale = 1.0
    backend = SimBackend(cfg)
    dialogs = AutoAnswerDialogs()
    ctl = Controller(cfg, backend, dialogs=dialogs, initial_mode=Mode.ADMIN)
    ctl.start()
    try:
        time.sleep(0.5)
        dialogs.answers.put(True)
        ctl.request_user_cmd("chiller")
        time.sleep(0.4)
        s0 = ctl.snapshot()
        assert s0.commands.chiller and s0.loop_blocked and s0.hold_remaining_s > 8 and "Chiller" in s0.hold_reason
        time.sleep(1.0)
        s1 = ctl.snapshot()
        assert s1.iteration == s0.iteration              # frozen: no new iteration in that second
        assert s1.inputs.t == s0.inputs.t                # same (old) readings
        assert s1.hold_remaining_s < s0.hold_remaining_s  # but the countdown is published
    finally:
        ctl.request_stop()                               # releases the freeze early
        ctl.join(10)
        assert ctl.finished.is_set()
