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


@pytest.fixture
def cfg_freq():
    """The small-chamber profile with the primary pump's drive-frequency feedback switched back on.

    The shipped profile ships that reader switched OFF - reading Mod1/ai6 put noise on the other
    analog channels (2026-09-21) - but the code behind it is intact and must keep working, so every
    frequency-dependent behaviour is tested against this fixture.  If re-enabling the block would
    break something, these tests are what catches it.
    """
    from facility_control.config import FrequencyConfig
    cfg = load_config()              # its own instance: a test may ask for `cfg` and `cfg_freq` together
    cfg.simulation.time_scale = 200.0
    cfg.primary.frequency = FrequencyConfig(channel="Mod1/ai6", min_v=0.0, max_v=10.0,
                                            max_hz=210.0, running_above_hz=5.0, enabled=True)
    cfg.primary.cross_check = True
    return cfg


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
