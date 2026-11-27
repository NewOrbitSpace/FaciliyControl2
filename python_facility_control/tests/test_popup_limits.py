"""Pop-ups must never flood the screen or lock the operator out of the panel (2026-11-27).

What happened (VC100, 25 Nov).  The serial port of the RS-485 converter was not there.  In Auto at
Facility Off the program took each silent HiPace turbo out of service (log line, pop-up), and a
second rule - "at Facility Off, give taken-out turbos back so the next pump-down tries them" - gave
it back in the SAME pass without looking whether it answered.  The next pass, a tenth of a second
later, took it out again: four pop-ups and eight log lines ten times a second, without end.  The
pop-ups were modal, so the panel underneath - the Manual Mode button included - could not be used.

What holds now, for this fault and for any other:
  1. the cause: a turbo that is still silent is not given back (Facility Off, Clear Error);
  2. one pop-up per serial port for a lost link, however many pumps hang on it;
  3. every pop-up that does not hold the loop goes through Controller._notify:
       - one box per subject - not again while its last box is open, nor within
         `logging.popup_repeat_s` (30 s);
       - at most `logging.popup_max_open` (5) boxes open at once; the rest is in the event log
         only, and ONE extra box says so;
  4. such boxes are never modal (gui/dialogs.py): STOP, Clear Error and the mode buttons work with
     any number of them open; the window itself refuses more than 8 as a second limit;
  5. a line that repeats in the event log is counted, not repeated (`logging.log_flood_lines`).
"""
import os

import pytest

from conftest import Harness
from facility_control.config import CONFIG_DIR, load_config
from facility_control.controller import AutoAnswerDialogs, DialogHandle
from facility_control.model import FacilityState as S, Mode

VC100 = CONFIG_DIR / "facility_vc100.yaml"
HIPACE = ("turbo2", "turbo3", "turbo5", "turbo6")


class Screen(AutoAnswerDialogs):
    """Like the real window: a pop-up that does not hold the loop stays open until OK is pressed."""

    def __init__(self):
        super().__init__()
        self.open = []                                    # [(kind, message, handle)]

    def ask(self, kind, message, buttons):
        if kind == "notice" or (kind == "info" and getattr(self, "hold_info", False)):
            self.log.append((kind, message))
            h = DialogHandle()
            self.open.append((kind, message, h))
            return h
        return super().ask(kind, message, buttons)

    def ok(self, n=None):
        """Press OK on the first n open boxes (all by default)."""
        for _kind, _msg, h in self.open[: len(self.open) if n is None else n]:
            h.resolve(None)
        self.open = [x for x in self.open if not x[2].done]

    def notices(self, text=""):
        return [m for k, m in self.log if k == "notice" and text in m]


def vc100(mode=Mode.AUTO, screen=True, **logging):
    cfg = load_config(str(VC100))
    cfg.simulation.time_scale = 200.0
    for k, v in logging.items():
        setattr(cfg.logging, k, v)
    h = Harness(cfg, mode=mode)
    if screen:
        h.dialogs = h.ctl.dialogs = Screen()
    h.step(2)
    return h


def link(h, lost, turbos=HIPACE):
    for t in turbos:
        h.backend.set_fault(f"rs485_lost_{t}", lost)


def skip(h, seconds):
    """Let time pass for the controller without running the plant."""
    h.t0 += seconds


def lines(s, text):
    return [l for l in s.event_log if text in l]


# ---------------------------------------------------------------------------- 1, 2: the fault itself
def test_silent_turbos_at_facility_off_give_one_popup_and_one_log_line_not_one_per_pass():
    """The photo of 25 Nov: Auto, Facility Off, no HiPace answers."""
    h = vc100()
    link(h, True)
    s = h.tick(3000)                                      # five minutes of loop passes
    assert s.mode == Mode.AUTO and s.current == S.FACILITY_OFF and not s.error.status
    pops = h.dialogs.notices()
    assert len(pops) == 1, f"{len(pops)} pop-ups"
    assert pops[0].startswith("No RS-485 reply from Turbo 2, Turbo 3, Turbo 5 and Turbo 6 (SIM addresses 2, 3, 5, 6)")
    assert "keeps trying to reconnect" in pops[0] and "Clear Error" in pops[0]
    assert len(lines(s, "No RS-485 reply from")) == 1
    assert not lines(s, "back in use"), "a silent turbo must not be given back"
    assert all(s.turbos[t].isolated and s.turbos[t].comm_lost for t in HIPACE)
    assert len(s.event_log) < 40, len(s.event_log)
    assert not any(s.commands.turbo_motor.values()) and not any(s.commands.valves.values())


def test_when_the_link_is_back_at_facility_off_the_turbos_are_given_back_and_used():
    h = vc100()
    link(h, True)
    h.tick(200)
    link(h, False)
    s = h.tick(5)
    assert len(lines(s, "answering on RS-485 again")) == 4 and len(lines(s, "back in use (Facility Off")) == 4
    assert not lines(s, "press 'Clear Error'"), "at Facility Off there is nothing to clear"
    assert not any(s.turbos[t].isolated for t in HIPACE)
    h.ctl.request_auto_button("pump_to_high_vac")
    s, _ = h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, max_iter=40000)
    assert all(s.commands.turbo_motor[t] and s.commands.valves[h.cfg.turbo(t).gate_valve] for t in HIPACE)
    assert len(h.dialogs.notices()) == 1


def test_clear_error_does_not_give_back_a_turbo_that_is_still_silent():
    h = vc100()
    link(h, True)
    h.tick(50)
    for _ in range(5):
        h.ctl.request_clear_error()
        s = h.tick(20)
    assert all(s.turbos[t].isolated for t in HIPACE)
    assert len(lines(s, "still not answering on RS-485")) == 4 * 5
    assert not lines(s, "back in use") and len(h.dialogs.notices()) == 1


def test_one_silent_pump_among_four_is_one_popup_and_the_others_carry_on():
    h = vc100()
    link(h, True, ["turbo5"])
    s = h.tick(500)
    pops = h.dialogs.notices()
    assert len(pops) == 1 and pops[0].startswith("No RS-485 reply from Turbo 5 (SIM address 5)")
    assert "Its turbo valve" in pops[0]
    assert s.turbos["turbo5"].isolated and not any(s.turbos[t].isolated for t in ("turbo2", "turbo3", "turbo6"))


def test_a_link_that_comes_and_goes_cannot_raise_more_than_one_popup_per_repeat_time():
    """Worst case: the fault flips every third pass and the operator presses OK every time."""
    h = vc100()
    t0 = h.ctl.clock()
    for i in range(400):
        link(h, i % 2 == 0)
        h.tick(3)
        h.dialogs.ok()
    elapsed = h.ctl.clock() - t0
    allowed = elapsed / h.cfg.logging.popup_repeat_s + 1
    assert 1 <= len(h.dialogs.notices()) <= allowed, (len(h.dialogs.notices()), elapsed)
    assert h.ctl.popups_hidden > 0


def test_a_gauge_that_keeps_dying_and_coming_back_does_not_pile_up_popups():
    h = vc100()
    h.ctl.request_auto_button("pump_to_rough")
    h.step(5)
    t0 = h.ctl.clock()
    raised = 0
    for i in range(60):
        h.backend.set_fault("gauge_fault_wrg", i % 2 == 0)
        for _ in range(400):
            h.tick()
            if len(h.dialogs.open) > raised:
                raised = len(h.dialogs.open)
    elapsed = h.ctl.clock() - t0
    pops = h.dialogs.notices("no signal")
    assert 1 <= len(pops) <= 1 + 0, "the first box was never closed, so no second one may appear"
    assert len(h.dialogs.open) == 1 and elapsed > 60


# ---------------------------------------------------------------------------- the panel stays usable
def test_manual_mode_can_be_entered_with_the_link_dead_and_popups_open():
    h = vc100()
    link(h, True)
    h.tick(100)
    assert len(h.dialogs.open) == 1                        # nobody has pressed OK
    h.ctl.request_manual_mode()
    s = h.tick(3)
    assert s.mode == Mode.MANUAL
    before = len(h.dialogs.log)
    s = h.tick(500)
    assert len(h.dialogs.log) == before, "no new pop-ups in Manual"
    assert s.error.status and "RS-485" in s.error.source  # there it is the plain turbo error it always was
    h.ctl.request_admin_mode()
    assert h.tick(3).mode == Mode.ADMIN


# ---------------------------------------------------------------------------- 3: the general limits
def test_at_most_five_popups_open_whatever_happens_and_one_box_says_there_is_more():
    h = vc100()
    ctl, scr = h.ctl, h.dialogs
    for i in range(40):                                    # forty different things go wrong at once
        ctl._notify(f"fault{i}", f"Thing {i} has failed")
    assert len(scr.open) == 6, [m for _, m, _ in scr.open]
    assert [m for _, m, _ in scr.open][:5] == [f"Thing {i} has failed" for i in range(5)]
    assert "more messages than the 5 pop-ups" in scr.open[5][1] and "event log" in scr.open[5][1]
    assert ctl.popups_hidden == 35
    assert len([l for l in ctl.event_log if "Pop-up limit reached" in l]) == 1
    # the same forty again, a thousand times: nothing new
    for _ in range(1000):
        for i in range(40):
            ctl._notify(f"fault{i}", f"Thing {i} has failed")
    assert len(scr.open) == 6 and len(scr.log) == 6
    # the operator closes two: there is room for two new subjects
    scr.ok(2)
    assert ctl._notify("fault20", "Thing 20 has failed") and ctl._notify("fault21", "Thing 21 has failed")
    assert not ctl._notify("fault22", "Thing 22 has failed")
    assert len([x for x in scr.open if "Thing" in x[1]]) == 5


def test_the_same_subject_is_not_raised_again_while_open_nor_within_the_repeat_time():
    h = vc100()
    ctl, scr = h.ctl, h.dialogs
    assert ctl._notify("gauge:wrg", "Chamber gauge dead")
    assert not ctl._notify("gauge:wrg", "Chamber gauge dead") and len(scr.open) == 1
    skip(h, 3600.0)
    assert not ctl._notify("gauge:wrg", "Chamber gauge dead"), "still open: not again, however long ago"
    scr.ok()
    assert ctl._notify("gauge:wrg", "Chamber gauge dead"), "closed and long ago: the operator is told again"
    scr.ok()
    assert not ctl._notify("gauge:wrg", "Chamber gauge dead"), "closed a moment ago: not yet"
    skip(h, h.cfg.logging.popup_repeat_s + 1.0)
    assert ctl._notify("gauge:wrg", "Chamber gauge dead")


def test_the_limit_is_a_setting():
    h = vc100(popup_max_open=2)
    for i in range(10):
        h.ctl._notify(f"k{i}", f"message {i}")
    assert len(h.dialogs.open) == 3                        # two, and the box that says there is more
    h = vc100(popup_max_open=0)                            # 0 = no limit on the number
    for i in range(10):
        h.ctl._notify(f"k{i}", f"message {i}")
    assert len(h.dialogs.open) == 10


def test_answers_to_operator_clicks_follow_the_same_limits_when_the_loop_does_not_wait_for_them():
    cfg = load_config(str(VC100))
    cfg.simulation.time_scale = 200.0
    cfg.timings.blocking_waits = False
    h = Harness(cfg, mode=Mode.MANUAL)
    scr = h.dialogs = h.ctl.dialogs = Screen()
    scr.hold_info = True
    h.step(2)
    for _ in range(200):
        h.ctl._info("Please Turn Chiller On First")
    assert len(scr.open) == 1
    scr.ok()
    h.ctl._info("Please Turn Chiller On First")           # pressed again after closing: shown again at once
    assert len(scr.open) == 1 and len(scr.log) == 2
    for i in range(50):
        h.ctl._info(f"refusal number {i}")
    assert len(scr.open) <= 6


# ---------------------------------------------------------------------------- 5: the event log
def test_a_log_line_that_repeats_is_counted_not_repeated():
    h = vc100()
    ctl = h.ctl
    n0 = len(ctl.event_log)
    for i in range(5000):
        ctl._log(f"!! cannot open COM3 (try {i}, 12:38:{i % 60:02d})")
    new = ctl.event_log[n0:]
    assert len(new) == 21, len(new)                        # 20, and the 21st says that it is repeating
    assert "repeating" in new[-1]
    ctl._log("Opened Bypass Valve at 12:39:01")            # anything else is not affected
    assert ctl.event_log[-1] == "Opened Bypass Valve at 12:39:01"
    # it goes on for a minute: one line per window, with the number that was not shown
    n1 = len(ctl.event_log)
    for _ in range(6):
        skip(h, h.cfg.logging.log_flood_window_s + 0.5)
        for i in range(1000):
            ctl._log(f"!! cannot open COM3 (try {i}, 12:39:{i % 60:02d})")
    more = ctl.event_log[n1:]
    assert len(more) == 6 and all("more times and was not repeated" in l for l in more), more
    # it stops; later the same line is an ordinary line again
    skip(h, 120.0)
    ctl._log("!! cannot open COM3 (try 1, 13:00:00)")
    assert ctl.event_log[-1] == "!! cannot open COM3 (try 1, 13:00:00)"


def test_ordinary_work_is_never_cut_from_the_log():
    """A whole pump-down, a vent and a shut-off: every line is there."""
    h = vc100()
    h.ctl.request_auto_button("pump_to_high_vac")
    h.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC, max_iter=40000)
    h.ctl.request_auto_button("shutdown")
    s, _ = h.run_until(lambda s: s.current in (S.FACILITY_OFF, S.TURBO_SLOWING), max_iter=40000)
    assert not [l for l in s.event_log if "not repeated" in l or "repeating" in l]
    assert h.ctl.popups_hidden == 0


# ---------------------------------------------------------------------------- 4: the window
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _window(app, modal_info=True):
    from PySide6.QtTest import QTest
    from facility_control.gui.dialogs import QtDialogProvider
    from facility_control.gui.main_window import MainWindow
    cfg = load_config(str(VC100))
    cfg.simulation.time_scale = 200.0
    h = Harness(cfg)
    win = MainWindow(cfg, h.ctl, sim_backend=h.backend)
    prov = QtDialogProvider(parent=win, modal_info=modal_info)
    win.show()
    h.step(3)
    win._poll()
    QTest.qWait(50)
    pressed = []
    win.manual_mode_btn.clicked.connect(lambda: pressed.append("manual"))
    return h, win, prov, pressed


def _dispose(app, win, prov):
    from PySide6.QtTest import QTest
    prov.close_all()
    win._timer.stop()
    QTest.qWait(20)
    win.hide()
    win.deleteLater()
    QTest.qWait(20)


def test_popups_that_do_not_hold_the_loop_never_block_the_panel(app):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    h, win, prov, pressed = _window(app)
    try:
        handles = [prov.ask("notice", f"No RS-485 reply from Turbo {i}", ["OK"]) for i in range(5)]
        QTest.qWait(80)
        assert len(prov._open_notices) == 5
        assert all(not b.isModal() and b.windowModality() == Qt.NonModal for b in prov._open_notices)
        # Qt's own word for "something is holding the other windows": nothing is
        assert app.activeModalWidget() is None and QGuiApplication.modalWindow() is None
        QTest.mouseClick(win.manual_mode_btn, Qt.LeftButton)
        QTest.qWait(30)
        assert pressed == ["manual"], "the Manual Mode button must work with pop-ups open"
        spots = {(b.x(), b.y()) for b in prov._open_notices}
        assert len(spots) == 5, "the boxes must not sit exactly on top of each other"
        # closing one resolves its handle and frees its place
        prov._open_notices[0].close()
        QTest.qWait(30)
        assert handles[0].done and len(prov._open_notices) == 4
    finally:
        _dispose(app, win, prov)


def test_a_question_is_still_modal_like_the_labview_dialog(app):
    """(Also shows that the test above can tell a held panel from a free one: here Qt reports a
    modal window, there it reports none.)"""
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    h, win, prov, pressed = _window(app)
    try:
        handle = prov.ask("two", "Is the manual vent valve closed?", ["Yes", "No"])
        QTest.qWait(80)
        assert app.activeModalWidget() is not None and QGuiApplication.modalWindow() is not None
        assert app.activeModalWidget().isModal()
        app.activeModalWidget().buttons()[1].click()         # "No"
        QTest.qWait(30)
        assert handle.done and handle.result == 1
        assert QGuiApplication.modalWindow() is None
        # blocking mode: the answer to an operator click ('info') is waited for, so it is modal too
        handle = prov.ask("info", "Please Turn Chiller On First", ["OK"])
        QTest.qWait(80)
        assert app.activeModalWidget() is not None and QGuiApplication.modalWindow() is not None
        app.activeModalWidget().close()
        QTest.qWait(30)
        assert handle.done
    finally:
        _dispose(app, win, prov)


def test_the_window_itself_refuses_more_than_eight_such_boxes(app):
    from PySide6.QtTest import QTest
    h, win, prov, pressed = _window(app)
    try:
        handles = [prov.ask("notice", f"message {i}", ["OK"]) for i in range(300)]
        QTest.qWait(150)
        assert len(prov._open_notices) == 8 and prov.dropped == 292
        assert sum(1 for x in handles if x.done) == 292     # nobody is left waiting for a box that never came
    finally:
        _dispose(app, win, prov)


def test_in_non_blocking_mode_the_answer_to_a_click_does_not_block_the_panel_either(app):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    h, win, prov, pressed = _window(app, modal_info=False)
    try:
        prov.ask("info", "Please Turn Chiller On First", ["OK"])
        QTest.qWait(80)
        assert app.activeModalWidget() is None and QGuiApplication.modalWindow() is None
        QTest.mouseClick(win.manual_mode_btn, Qt.LeftButton)
        QTest.qWait(30)
        assert pressed == ["manual"]
    finally:
        _dispose(app, win, prov)
