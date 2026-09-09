import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from facility_control.config import load_config  # noqa: E402
from facility_control.controller import AutoAnswerDialogs, Controller  # noqa: E402
from facility_control.hal.sim_backend import SimBackend  # noqa: E402
from facility_control.model import Mode  # noqa: E402


@pytest.fixture
def cfg():
    c = load_config()
    c.simulation.time_scale = 200.0
    return c


class Harness:
    """Controller + simulator with a simulated clock so tests run fast and deterministically."""

    def __init__(self, cfg, mode=Mode.AUTO, run_hours=None):
        self.cfg = cfg
        self.backend = SimBackend(cfg)
        self.dialogs = AutoAnswerDialogs()
        self.t0 = time.time()
        # the controller's timers follow the simulated clock; sleeps are shortened
        self.ctl = Controller(cfg, self.backend, dialogs=self.dialogs, initial_mode=mode, run_hours=run_hours,
                              sleep=lambda s: time.sleep(min(s, 0.0005)), clock=lambda: self.t0 + self.backend._sim_time)
        self.ctl.backend.open()

    def step(self, n=1, through_hold=True):
        """Run n *decisions*.  Blocking mode (default): one iteration = one decision, the settle
        waits happen inside `iterate()` (shortened sleeps).  Non-blocking mode: iterate through the
        hold until the controller decides again."""
        for _ in range(n):
            self.ctl.iterate()
            guard = 0
            while through_hold and self.ctl.holding and self.ctl._pending is None and guard < 100000:
                self.ctl.iterate()
                guard += 1
        return self.ctl.snapshot()

    def tick(self, n=1):
        """Plain iterations, no waiting for holds."""
        for _ in range(n):
            self.ctl.iterate()
        return self.ctl.snapshot()

    def run_until(self, pred, max_iter=20000):
        for i in range(max_iter):
            s = self.step()
            if pred(s):
                return s, i
        raise AssertionError("condition not reached; last state %s/%s" % (s.current.label, s.substate))

    def advance_plant(self, seconds):
        self.backend.advance(seconds)


@pytest.fixture
def harness(cfg):
    return Harness(cfg)
