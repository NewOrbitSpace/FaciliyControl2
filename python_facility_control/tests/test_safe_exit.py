"""Closing the program: the facility is shut down safely FIRST (2026-11-17).

The VI ('Force stop on exit' = True), and this port until now, wrote everything off in one step when
the program closed - gates, turbo valves, primary pump, chiller, turbo motors - whatever the turbos
were doing.  At full speed that drops the cooling (and, where the chiller contactor feeds them, the
turbo controllers' supply) with the rotors at 100 %.

Now:
  * STOP and the window's [x] ask for `Controller.request_safe_exit()`: Auto mode, Facility Off, with
    every plant-safety rule of the state machine - exactly what an error does.  Gates shut, motors
    off; a turning turbo keeps its turbo valve, the primary pump and the chiller until it has slowed
    down.  The loop stops by itself when EVERYTHING is off.  Until then the program cannot be
    closed, only the shutdown cancelled.
  * every other way out (`request_stop`, a crash in the loop) closes the valves and stops the
    motors first, waits the valve travel, then stops the primary pump - and leaves the chiller
    running if a turbo is still turning.
"""
import threading
import time

import pytest

from facility_control.config import CONFIG_DIR, load_config
from facility_control.controller import DialogHandle
from facility_control.model import FacilityState as S, Mode
from tests.conftest import Harness
from tests.safety_rig import CHAMBERS, PROFILES, Rig
from tests.safety_scenarios import (PLANTS, SITUATIONS, SPINNING_SITUATIONS, assert_no_new_hazard, at_least,
                                    exists_on, make)
from tests.test_safety_handover import GRACE_S, LINEUPS, _admin, _set

ATM = 760.0


def _everything_off(c) -> bool:
    return not (any(c.valves.values()) or c.primary or c.primary_power or c.primary_run or c.chiller
                or any(c.turbo_motor.values()) or any(c.turbo_standby.values()))


def _run_exit(r, max_s=5 * 3600.0, grace_s=0.0):
    """Ask to close and run until the loop stops itself.  Returns the plant seconds it took."""
    t0 = r.t
    r.ctl.request_safe_exit()
    if grace_s:
        r.tick(1)
        r.run(grace_s)
        r.monitor.enabled = True
    while not r.ctl._stop.is_set():
        r.tick()
        assert r.t - t0 < max_s, (f"still not closed after {max_s / 3600:.0f} h: {r.ctl.snapshot().exit_note}; "
                                  f"commands {r.ctl.cmds}")
    took = r.t - t0
    assert _everything_off(r.ctl.cmds), f"the loop stopped with something still on: {r.ctl.cmds}"
    th = r.cfg.thresholds
    cool = min(th.slowing_pct, th.turbo_spinning_pct)
    fast = {t.id: round(r.speed(t.id)) for t in r.turbos if r.speed(t.id) > cool + 1.0}
    assert not fast, f"the program closed with turbos still above {cool:.0f} %: {fast}"
    r.ctl._shutdown()                                   # what run() does next
    assert _everything_off(r.ctl.cmds)
    r._advance(60.0)                                    # ... and nothing happens to the plant afterwards
    return took


# ------------------------------------------------------------------------------- from every Auto situation
def _cells():
    for which in CHAMBERS:
        for sit in SITUATIONS:
            if not exists_on(sit, which):
                continue
            if not at_least("normal") and sit not in SPINNING_SITUATIONS:
                continue
            yield which, sit


@pytest.mark.parametrize("which,situation", list(_cells()))
def test_closing_from_any_auto_situation_is_safe(which, situation):
    r = make(which, situation, "long_coast")
    spinning = r.any_turning()
    took = _run_exit(r)
    s = r.ctl.snapshot()
    assert s.mode == Mode.AUTO and s.current == S.FACILITY_OFF
    assert all(r.st.valves[v].position < 0.02 for v in r.st.valves), {v: x.position for v, x in r.st.valves.items()}
    if not spinning:
        assert took < 900.0, f"nothing was turning and it still took {took:.0f} s"
    assert_no_new_hazard(r, f"program closed in {situation}")
    assert any("Program asked to close" in line for line in r.ctl.event_log)
    assert any("Facility shut down – closing the program" in line for line in r.ctl.event_log)


@pytest.mark.parametrize("which", CHAMBERS)
def test_at_full_speed_the_turbos_keep_backing_and_cooling_until_they_have_slowed_down(which):
    r = Rig(which)                                      # the measured plant
    r.to_high_vac()
    r.run(60.0)
    t0 = r.t
    r.ctl.request_safe_exit()
    r.tick(1)
    r.run(20.0)
    s = r.ctl.snapshot()
    assert s.exiting and "waiting for the turbos to slow down" in s.exit_note
    c = s.commands
    assert not any(c.valves[t.gate_valve] for t in r.turbos) and not any(c.turbo_motor.values())
    assert c.primary and c.chiller and all(c.valves[t.turbo_valve] for t in r.turbos)
    th = r.cfg.thresholds
    while not r.ctl._stop.is_set():
        s = r.tick()
        for t in r.turbos:
            sp = r.speed(t.id)
            if sp > th.slowing_pct + 1.0:
                assert s.commands.primary and s.commands.valves[t.turbo_valve], f"{t.id} at {sp:.0f} % lost its backing"
            if sp > min(th.slowing_pct, th.turbo_spinning_pct) + 1.0:
                assert s.commands.chiller, f"{t.id} at {sp:.0f} % lost its cooling"
        assert r.t - t0 < 4 * 3600.0
    assert r.t - t0 > 60.0                               # it really waited
    assert _everything_off(r.ctl.cmds)
    r.assert_safe()


# ------------------------------------------------------------------------------- from Manual / Admin
@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("lineup", list(LINEUPS) + ["unrecognised__vent_and_gate_open"])
def test_closing_from_an_admin_line_up_is_safe(which, lineup):
    """Admin has no interlocks, so the line-up can be anything.  Closing takes it over as it is."""
    if which == "vc100" and not at_least("normal") and "coasting" not in lineup:
        pytest.skip("SAFETY_LEVEL=quick")
    r = _admin(which)
    if lineup.startswith("unrecognised"):
        _set(r, primary=True, vent=True, gate=True, chamber=ATM)
    else:
        _set(r, **LINEUPS[lineup][0])
    _run_exit(r, grace_s=GRACE_S)
    assert r.ctl.mode == Mode.AUTO and r.ctl.current == S.FACILITY_OFF
    assert any("mode left for the shutdown" in line for line in r.ctl.event_log)
    assert_no_new_hazard(r, f"program closed in Admin, '{lineup}'")


@pytest.mark.parametrize("which", CHAMBERS)
def test_closing_from_manual_mode(which):
    r = Rig(which, mode=Mode.MANUAL, tweak=PLANTS["long_coast"])
    r.monitor.enabled = False
    r.tick(2)
    _set(r, primary=True, chiller=True, tv=True, gate=True, motor=True, speed=100.0, chamber=1e-5, body=1e-5)
    _run_exit(r, grace_s=GRACE_S)
    assert r.ctl.mode == Mode.AUTO
    assert_no_new_hazard(r, "program closed in Manual at full speed")


# ------------------------------------------------------------------------------- while it is closing
@pytest.mark.parametrize("which", CHAMBERS)
def test_buttons_are_ignored_while_closing(which):
    r = make(which, "high_vac", "long_coast")
    r.ctl.request_safe_exit()
    r.run(30.0)
    for button in ("pump_to_high_vac", "vent", "pump_to_rough"):
        r.press(button)
        r.ctl.request_admin_mode()
        s = r.run(20.0)
        assert s.exiting and s.mode == Mode.AUTO and s.current == S.FACILITY_OFF
        assert not any(s.commands.turbo_motor.values())
        assert not any(s.commands.valves[v.id] for v in r.cfg.valves_of_kind("vent") + r.cfg.valves_of_kind("gate"))


@pytest.mark.parametrize("which", CHAMBERS)
def test_closing_can_be_cancelled_and_the_program_carries_on(which):
    r = make(which, "high_vac", "long_coast")
    r.ctl.request_safe_exit()
    r.run(60.0)
    assert r.ctl.snapshot().exiting
    r.ctl.request_cancel_exit()
    s = r.run(10.0)
    assert not s.exiting and s.exit_note == "" and not r.ctl._stop.is_set() and not r.ctl._exit_asked.is_set()
    assert s.mode == Mode.AUTO and s.current == S.FACILITY_OFF
    assert any("Closing cancelled" in line for line in r.ctl.event_log)
    r.press("pump_to_high_vac")                         # ... and Auto works as before
    r.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, what="Pumping to High Vac again")
    r.run(600.0)
    r.assert_safe()


@pytest.mark.parametrize("which", CHAMBERS)
def test_closing_with_an_error_active(which):
    """A dead chamber gauge (error 5014) is already shutting the facility down: closing still ends."""
    r = make(which, "high_vac", "long_coast")
    r.backend.set_fault(f"gauge_fault_{r.cfg.main_gauge.id}", True)
    r.run(10.0)
    assert r.ctl.snapshot().error.status
    _run_exit(r)
    assert_no_new_hazard(r, "program closed with a gauge fault active")


@pytest.mark.parametrize("which", CHAMBERS)
def test_closing_with_nothing_on_is_immediate(which):
    r = Rig(which)
    r.tick(3)
    assert _run_exit(r) < 5.0


# ------------------------------------------------------------------------------- every other way out
@pytest.mark.parametrize("which", CHAMBERS)
def test_a_stop_at_full_speed_closes_the_valves_before_the_pump_stops_and_keeps_the_cooling(which):
    """`request_stop`, or the loop dying: no waiting for the turbos, but in the right order."""
    r = Rig(which)
    r.to_high_vac()
    r.run(60.0)
    tvs = [v.id for v in r.cfg.valves_of_kind("turbo")]
    order = []
    write = r.backend.write

    def spy(cmds):
        order.append((any(cmds.valves[v] for v in tvs), bool(cmds.primary or cmds.primary_run), bool(cmds.chiller),
                      r.t))
        return write(cmds)
    r.backend.write = spy
    r.ctl._shutdown()
    assert [(tv, prim, chil) for tv, prim, chil, _ in order] == [(False, True, True), (False, False, True)], order
    assert order[1][3] - order[0][3] >= r.cfg.timings.valve_settle_ms / 1000.0 - 1e-6     # the valve travel was waited
    assert r.ctl.cmds.chiller and not r.ctl.cmds.primary and not any(r.ctl.cmds.valves.values())
    assert not any(r.ctl.cmds.turbo_motor.values())
    r._advance(120.0)
    assert r.st.chiller_on > 0.5, "the cooling was dropped with the turbos turning"
    assert not r.monitor.found("T1", "T3", "T9", "T12", "T14"), r.monitor.report()
    assert any("EXCEPT the chiller" in line for line in r.ctl.event_log)


@pytest.mark.parametrize("which", CHAMBERS)
def test_a_stop_with_nothing_turning_switches_everything_off_as_before(which):
    r = Rig(which)
    r.to_rough_hold()
    writes = []
    write = r.backend.write
    r.backend.write = lambda c: (writes.append(c.copy()), write(c))[1]
    r.ctl._shutdown()
    assert len(writes) == 1 and _everything_off(writes[0])
    assert r.ctl.event_log[-1].endswith("Program stopped – safe state commanded")


def test_the_old_exit_dropped_the_cooling_at_full_speed():
    """What `backend.safe_state()` alone - the exit until 2026-11-17 - does on the same plant."""
    r = Rig("vc100")
    r.to_high_vac()
    r.run(60.0)
    r.backend.safe_state()
    r._advance(60.0)
    assert r.monitor.found("T12"), "expected: chiller off with the rotors near 100 %"


# ------------------------------------------------------------------------------- with the real loop thread
class _NeverAnswers:
    """Dialogs that stay open (nobody at the panel)."""

    def __init__(self):
        self.asked = []

    def ask(self, kind, message, buttons):
        self.asked.append(message)
        return DialogHandle()


def _threaded(which, dialogs=None, mode=Mode.AUTO):
    cfg = load_config(str(CONFIG_DIR / PROFILES[which]))
    cfg.simulation.time_scale = 200.0
    h = Harness(cfg, mode=mode)
    if dialogs is not None:
        h.ctl.dialogs = dialogs
    return h


def _wait(pred, seconds=20.0):
    end = time.time() + seconds
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("which", CHAMBERS)
def test_the_loop_thread_ends_by_itself_once_everything_is_off(which):
    h = _threaded(which)
    h.ctl.start()
    try:
        assert _wait(lambda: h.ctl.snapshot().iteration > 3)
        h.ctl.request_auto_button("pump_to_rough")
        assert _wait(lambda: h.ctl.snapshot().commands.primary, 30.0)
        h.ctl.request_safe_exit()
        assert h.ctl.finished.wait(60.0), h.ctl.snapshot().exit_note
        assert _everything_off(h.ctl.cmds)
        log = h.ctl.event_log
        assert any("Program asked to close" in x for x in log) and log[-1].endswith("safe state commanded")
    finally:
        h.ctl.request_stop()
        h.ctl.join(10.0)


def test_closing_while_the_loop_waits_for_an_answer():
    """Blocking mode: the loop stands still in the 'Select Control Mode' dialog.  Asking to close
    counts as closing that dialog - the loop carries on and ends."""
    dialogs = _NeverAnswers()
    h = _threaded("small", dialogs=dialogs, mode=None)
    h.ctl.mode = Mode.INITIALIZE
    assert h.ctl.blocking
    h.ctl.start()
    try:
        assert _wait(lambda: bool(dialogs.asked)), "the start-up question was never asked"
        assert not h.ctl.finished.is_set()
        h.ctl.request_safe_exit()
        assert h.ctl.finished.wait(20.0)
        assert _everything_off(h.ctl.cmds)
    finally:
        h.ctl.request_stop()
        h.ctl.join(10.0)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_crash_in_the_loop_still_ends_in_the_staged_stop():
    cfg = load_config(str(CONFIG_DIR / PROFILES["small"]))
    cfg.simulation.time_scale = 200.0
    h = Harness(cfg)
    h.step(3)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC and h.backend.state.turbos["turbo1"].speed_pct > 90.0)
    writes = []
    write = h.backend.write
    h.backend.write = lambda c: (writes.append(c.copy()), write(c))[1]
    read, calls = h.backend.read, [0]

    def boom():
        calls[0] += 1
        if calls[0] > 3:
            raise RuntimeError("simulated crash in the control loop")
        return read()
    h.backend.read = boom
    hook, threading.excepthook = threading.excepthook, lambda args: None      # the traceback is expected
    try:
        h.ctl.start()
        assert h.ctl.finished.wait(20.0)
    finally:
        threading.excepthook = hook
    assert len(writes) >= 2
    first, last = writes[-2], writes[-1]
    assert not any(first.valves.values()) and first.primary and first.chiller
    assert not any(last.valves.values()) and not last.primary and last.chiller
    assert any("EXCEPT the chiller" in line for line in h.ctl.event_log)


# ------------------------------------------------------------------------------- the front panel
class _Alive:
    def is_alive(self):
        return True


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _window(app, which="small"):
    from facility_control.gui.main_window import MainWindow
    h = _threaded(which)
    win = MainWindow(h.cfg, h.ctl, sim_backend=h.backend)
    win.show()
    h.step(3)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC and h.backend.state.turbos["turbo1"].speed_pct > 90.0)
    h.ctl._thread = _Alive()                            # as far as the window can tell, the loop is running
    win._poll()
    app.processEvents()
    return h, win


def _dispose(app, h, win):
    h.ctl._thread = None
    win._timer.stop()
    win.close()
    win.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("how", ["x", "stop"])
def test_the_window_does_not_close_until_the_shutdown_is_done(app, how):
    h, win = _window(app)
    asked = []
    win._ask_close = lambda snap: (asked.append(win._something_on(snap)), True)[1]
    if how == "x":
        assert win.close() is False                     # refused: the shutdown has only just been asked for
    else:
        win.stop_btn.click()
    app.processEvents()
    assert asked == [True] and win.isVisible() and win._closing and not win.stop_btn.isEnabled()
    assert h.ctl._exit_asked.is_set()
    h.step(3)
    win._poll()
    app.processEvents()
    assert h.ctl.snapshot().exiting and win.isVisible()
    assert "Closing the program" in win.elapsed_label.text()
    # asked again while it runs: 'keep waiting' changes nothing
    win._ask_cancel_close = lambda snap: False
    assert win.close() is False and win.isVisible() and h.ctl._exiting
    # ... the turbo slows down, everything goes off, the loop ends
    for _ in range(20000):
        h.step()
        if h.ctl._stop.is_set():
            break
    else:
        raise AssertionError("the shutdown never finished: " + h.ctl.snapshot().exit_note)
    assert win.isVisible()
    h.ctl._shutdown()
    h.ctl.finished.set()
    win._poll()                                         # the panel notices and closes itself
    app.processEvents()
    assert not win.isVisible()
    _dispose(app, h, win)


def test_answering_no_leaves_everything_as_it_is(app):
    h, win = _window(app)
    win._ask_close = lambda snap: False
    assert win.close() is False
    h.step(3)
    s = h.ctl.snapshot()
    assert win.isVisible() and not win._closing and not s.exiting and s.current == S.PUMPING_TO_HIGH_VAC
    assert any(s.commands.turbo_motor.values())
    _dispose(app, h, win)


def test_the_shutdown_can_be_cancelled_from_the_panel(app):
    h, win = _window(app)
    win._ask_close = lambda snap: True
    win.close()
    h.step(3)
    assert h.ctl.snapshot().exiting
    win._ask_cancel_close = lambda snap: True
    assert win.close() is False
    h.step(3)
    win._poll()
    assert not h.ctl.snapshot().exiting and not win._closing and win.stop_btn.isEnabled() and win.isVisible()
    _dispose(app, h, win)


def test_without_a_running_loop_the_window_just_closes(app):
    """Tests and tools that build the window around a controller they step by hand."""
    h, win = _window(app)
    h.ctl._thread = None
    win._ask_close = lambda snap: pytest.fail("nothing to ask: no loop is running")
    assert win.close() is True and not win.isVisible()
    _dispose(app, h, win)
