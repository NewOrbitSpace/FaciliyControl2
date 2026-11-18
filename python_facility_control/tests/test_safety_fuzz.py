"""A random operator: long seeded sequences of button presses and waits, on both chambers.

The sweep (test_safety_sweep.py) covers one press from each situation.  Real incidents are two or
three presses deep - the 2026-09-22 one was Shutdown, then Pump to High Vac while the turbo was
still slowing; the 2026-11-11 one was Vent, wait for atmosphere, then Pump to High Vac.  Here an
operator presses whatever is on the page at random moments - seconds apart or tens of minutes
apart - for hours of plant time, and the safety monitor watches the plant throughout.

Half the runs start cold (Facility Off, chamber at atmosphere); the other half start from one of
the named situations of tests/safety_scenarios.py, picked by the seed - mostly the ones with a
rotor turning - so the campaign spends its time where the hazards are instead of in pump-downs.

Every run is seeded and deterministic: a failure prints the seed and the presses, and
`replay(which, plant, operator, seed, hours)` below reproduces it exactly.

Three operators:
  visible    presses only what the page shows, at any moment
  impatient  the same, mostly seconds apart, often twice
  any        any Auto action at any moment - a script, a remote panel, a stale click
"""
import pytest

from tests.safety_rig import CHAMBERS, Rig, seeded
from tests.safety_scenarios import (LEVEL, PLANTS, SITUATIONS, SPINNING_SITUATIONS, assert_no_new_hazard,
                                    random_operator)

# (seeds, plant hours per run) by level - the six-turbo plant costs about eight times the small one
CAMPAIGN = {
    "small": {"quick": (3, 1.5), "normal": (8, 2.0), "full": (60, 5.0)},
    "vc100": {"quick": (1, 1.0), "normal": (3, 1.5), "full": (25, 3.0)},
}
OPERATORS = ("visible", "impatient", "any")
STARTS = SPINNING_SITUATIONS + ["rough_mid", "rough_hold", "vented_hold", "off_after_vent", "overnight_cold"]


def replay(which, plant, operator, seed, hours):
    """One campaign run, exactly: the rig after the random operator has finished."""
    rng = seeded(1000 * seed + OPERATORS.index(operator))
    start = "off_cold" if seed % 2 == 0 else STARTS[rng.randrange(len(STARTS))]
    r = Rig(which, seed=seed, tweak=PLANTS[plant])
    SITUATIONS[start](r)
    r.start_situation = start
    random_operator(r, rng, hours, visible_only=(operator != "any"), impatient=(operator == "impatient"))
    return r


def _runs():
    for which in CHAMBERS:
        seeds, hours = CAMPAIGN[which][LEVEL]
        for plant in ("long_coast", "fast_rough") if LEVEL != "quick" else ("long_coast",):
            for operator in OPERATORS:
                for seed in range(seeds):
                    yield which, plant, operator, seed, hours


@pytest.mark.parametrize("which,plant,operator,seed,hours", list(_runs()))
def test_random_operator(which, plant, operator, seed, hours):
    r = replay(which, plant, operator, seed, hours)
    assert r.pressed, "the operator never pressed anything"
    assert_no_new_hazard(r, f"random operator '{operator}', seed {seed}, {plant} plant, {hours:g} h, "
                            f"started from '{r.start_situation}'")


@pytest.mark.parametrize("which", CHAMBERS)
def test_the_random_operator_gets_everywhere(which):
    """The campaign is only worth something if it gets a turning turbo into every state where one
    can be exposed.  (Counted over the 'visible' operator's runs of this level.)"""
    from facility_control.model import FacilityState as S
    seen, turning_in = set(), set()
    seeds, hours = CAMPAIGN[which][LEVEL]
    for seed in range(max(4, seeds)):
        rng = seeded(1000 * seed)
        start = "off_cold" if seed % 2 == 0 else STARTS[rng.randrange(len(STARTS))]
        r = Rig(which, seed=seed, tweak=PLANTS["long_coast"])
        orig = r.tick

        def tick(n=1, r=r, orig=orig):
            s = orig(n)
            seen.add(s.current)
            if r.any_turning():
                turning_in.add(s.current)
            return s
        r.tick = tick
        SITUATIONS[start](r)
        random_operator(r, rng, hours)
    want = {S.FACILITY_OFF, S.PUMPING_TO_ROUGH, S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC, S.DISENGAGE_TURBO,
            S.VENTING, S.TURBO_SLOWING}
    assert want <= seen, f"never reached: {sorted(s.label for s in want - seen)}"
    need = {S.PUMPING_TO_ROUGH, S.VENTING, S.TURBO_SLOWING}
    assert need <= turning_in, f"never had a turning turbo in: {sorted(s.label for s in need - turning_in)}"
