"""A pressure gauge that is dead or frozen: error 5014, warning, safe shutdown (2026-11-16).

  dead    output below `thresholds.gauge_dead_below_volts` (1 V) for `timings.gauge_fault_delay_s` (3 s).
          A dead or unplugged gauge sends about 0 V; in the facility logs of 2026-10-23 .. 11-11 no
          gauge on either chamber ever read below 2.7 V.
  frozen  `timings.gauge_frozen_readings` (100) readings in a row exactly equal.  The logs (voltages
          to 0.1 mV, 5 s apart) show a healthy gauge repeating its previous value 5-13 % of the time
          and never more than 10 rows running - except clipped at the top of the input range.

What the facility then does is tested, state by state, in test_safety_known_issues.py (KI-7).
"""
import math

import pytest

from facility_control.model import ERR_GAUGE_FAULT, FacilityState as S
from tests.safety_rig import CHAMBERS, Rig


def _code(r):
    s = r.ctl.snapshot()
    return s.error.code if s.error.status else 0


def _volts(r, gid):
    return r.ctl.inputs.gauge_volts[gid]


# ------------------------------------------------------------------------------------------ dead
@pytest.mark.parametrize("which", CHAMBERS)
def test_a_dropout_shorter_than_the_delay_is_not_a_fault(which):
    r = Rig(which)
    r.tick(5)
    gid = r.cfg.main_gauge.id
    assert r.cfg.thresholds.gauge_dead_below_volts == 1.0 and r.cfg.timings.gauge_fault_delay_s == 3.0
    r.backend.set_fault(f"gauge_fault_{gid}", True)
    r.tick(2)                                         # seen low at t and t+1 s: 1 s so far
    assert _volts(r, gid) < 1.0 and _code(r) == 0
    r.backend.set_fault(f"gauge_fault_{gid}", False)
    r.tick(20)
    assert _code(r) == 0 and not [m for k, m in r.dialogs.log if k in ("info", "notice")]
    # ... and the clock starts again from nothing the next time
    r.backend.set_fault(f"gauge_fault_{gid}", True)
    r.tick(2)
    assert _code(r) == 0
    r.tick(3)
    assert _code(r) == ERR_GAUGE_FAULT


@pytest.mark.parametrize("which", CHAMBERS)
def test_dead_is_declared_after_the_delay_and_names_the_gauge(which):
    r = Rig(which)
    r.tick(5)
    g = r.cfg.foreline_gauge
    r.backend.set_fault(f"gauge_fault_{g.id}", True)
    first = None
    for i in range(1, 10):
        r.tick()
        if _code(r) == ERR_GAUGE_FAULT:
            first = i
            break
    assert first == 4, f"declared after {first} readings 1 s apart (3 s must have passed since the first low one)"
    src = r.ctl.snapshot().error.source
    assert g.label in src and "no signal" in src and "above 1 V" in src
    assert math.isnan(r.ctl.inputs.pressure(g.id))
    assert any(line.startswith("!!") and g.label in line for line in r.ctl.event_log)


# ---------------------------------------------------------------------------------------- frozen
@pytest.mark.parametrize("which", CHAMBERS)
def test_frozen_needs_exactly_the_configured_number_of_equal_readings(which):
    r = Rig(which)
    r.to_rough_hold()                                 # every gauge mid-range, nothing clipped
    g = r.cfg.main_gauge
    n = r.cfg.timings.gauge_frozen_readings
    assert n == 100
    r.backend.set_fault(f"gauge_frozen_{g.id}", True)
    held = _volts(r, g.id)
    r.tick(n - 2)                                     # with the one already read: n - 1 equal readings
    assert _volts(r, g.id) == held and r.ctl._gauge_same[g.id] == n - 1 and _code(r) == 0
    r.tick(1)
    assert r.ctl._gauge_same[g.id] == n and _code(r) == ERR_GAUGE_FAULT
    src = r.ctl.snapshot().error.source
    assert g.label in src and "frozen" in src and f"{n} readings" in src
    r.run(30.0)
    assert r.ctl.snapshot().current == S.FACILITY_OFF


@pytest.mark.parametrize("which", CHAMBERS)
def test_one_different_reading_starts_the_count_again(which):
    r = Rig(which)
    r.to_rough_hold()
    g = r.cfg.main_gauge
    r.backend.set_fault(f"gauge_frozen_{g.id}", True)
    r.tick(90)
    r.backend.set_fault(f"gauge_frozen_{g.id}", False)
    r.tick(1)                                         # one live reading
    r.backend.set_fault(f"gauge_frozen_{g.id}", True)
    r.tick(90)
    assert _code(r) == 0, "two runs of 90 are not one run of 100"


@pytest.mark.parametrize("which", CHAMBERS)
def test_a_healthy_gauge_never_repeats_for_long(which):
    """Hours of idling and a full pump-down on live gauges: the longest run of equal readings stays
    tiny against the 100 it takes."""
    r = Rig(which)
    longest = 0
    r.press("pump_to_high_vac")
    for _ in range(4000):
        s = r.tick()
        assert not s.error.status, s.error
        longest = max(longest, max(r.ctl._gauge_same.values()))
    assert s.current == S.PUMPING_TO_HIGH_VAC
    clipped = [g.id for g in r.cfg.gauges if g.id in r.ctl._gauge_same]
    assert clipped and longest < 100


def test_a_reading_clipped_at_the_top_of_the_input_range_is_not_frozen():
    """VC100, everything off: the foreline gauge is at atmosphere and its signal clips at the top of
    the 0..10 V input - exactly 10.0000 V, reading after reading (as in the log of 2026-10-26)."""
    r = Rig("vc100")
    g = r.cfg.foreline_gauge
    for _ in range(200):                              # (the model's signal sits right at the clip: wait for one)
        r.tick()
        if _volts(r, g.id) == r.cfg.ai_max_v == 10.0:
            break
    else:
        raise AssertionError("the foreline gauge never clipped")
    r.backend.set_fault(f"gauge_frozen_{g.id}", True)     # ... and hold it there
    r.tick(400)
    assert _volts(r, g.id) == 10.0 and r.ctl._gauge_same[g.id] > 300
    assert _code(r) == 0


# ------------------------------------------------------------------------------- which gauges, switches
def test_the_gauge_of_a_turbo_that_is_not_in_use_is_not_watched():
    def tweak(cfg):
        for t in cfg.turbos:
            if cfg.turbo_gauge(t.id) is not None and cfg.turbo_gauge(t.id).id == "turbo3_gauge":
                t.in_use = False                      # Turbo 3 and Turbo 6 share that gauge
    r = Rig("vc100", tweak=tweak)
    r.tick(3)
    r.backend.set_fault("gauge_fault_turbo3_gauge", True)
    r.run(60.0)
    assert _code(r) == 0


@pytest.mark.parametrize("which", CHAMBERS)
def test_each_rule_has_its_own_off_switch(which):
    def no_dead(cfg):
        cfg.thresholds.gauge_dead_below_volts = None
    r = Rig(which, tweak=no_dead)
    r.tick(5)
    r.backend.set_fault(f"gauge_fault_{r.cfg.main_gauge.id}", True)
    r.tick(60)                                        # (dead reads noise around 0 V: not frozen either)
    assert _code(r) == 0

    def no_frozen(cfg):
        cfg.timings.gauge_frozen_readings = 0
    r = Rig(which, tweak=no_frozen)
    r.to_rough_hold()
    r.backend.set_fault(f"gauge_frozen_{r.cfg.main_gauge.id}", True)
    r.tick(300)
    assert _code(r) == 0


@pytest.mark.parametrize("which", CHAMBERS)
def test_one_gauge_can_have_its_own_floor(which):
    """`gauges[].dead_below_volts`: 0 = this gauge is never judged by its voltage."""
    def tweak(cfg):
        cfg.main_gauge.dead_below_volts = 0.0
    r = Rig(which, tweak=tweak)
    r.tick(5)
    r.backend.set_fault(f"gauge_fault_{r.cfg.main_gauge.id}", True)
    r.tick(30)
    assert _code(r) == 0
    r.backend.set_fault(f"gauge_fault_{r.cfg.foreline_gauge.id}", True)
    r.tick(6)
    assert _code(r) == ERR_GAUGE_FAULT and r.cfg.foreline_gauge.label in r.ctl.snapshot().error.source


def test_the_profile_keys_load():
    from facility_control.config import build_config, CONFIG_DIR
    import yaml
    raw = yaml.safe_load(open(CONFIG_DIR / "facility_vc100.yaml", encoding="utf-8"))
    assert raw["thresholds"]["gauge_dead_below_volts"] == 1.0
    assert raw["timings"]["gauge_fault_delay_s"] == 3.0 and raw["timings"]["gauge_frozen_readings"] == 100
    raw["gauges"][1]["dead_below_volts"] = 0.5
    cfg = build_config(raw)
    assert cfg.gauges[1].dead_below_volts == 0.5 and cfg.gauges[0].dead_below_volts is None
