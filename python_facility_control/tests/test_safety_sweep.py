"""Every Auto button, pressed in every situation an operator can be in, on both chambers.

The engineer's report of 2026-11-11 was one cell of this table: situation "chamber vented, turbos
spinning down, turbo valves open, primary on" x button "Pump to High Vac".  Nobody had tried it.
This file tries all of them, and after each press lets the sequence run on for half an hour of
plant time with the safety monitor watching valve positions, pressures and rotor speeds.

A cell fails if the monitor sees a rule broken that is not already a known issue
(tests/safety_scenarios.KNOWN_ISSUES; SAFETY_STRICT=1 switches that tolerance off).
"""
import pytest

from tests.safety_rig import ALL_BUTTONS, CHAMBERS, Rig
from tests.safety_scenarios import (LEVEL, PLANTS, SITUATIONS, SPINNING_SITUATIONS, assert_no_new_hazard, at_least,
                                    exists_on, make)

WAIT = "(just wait)"
RUN_ON_S = 1800.0 if at_least("full") else 1200.0
VC100_NORMAL = SPINNING_SITUATIONS + ["venting_from_slowing_atm", "off_after_vent", "vented_hold"]


def _cells():
    for which in CHAMBERS:
        for sit in SITUATIONS:
            if not exists_on(sit, which):
                continue
            if not at_least("normal") and sit not in ("venting_coasting_atm", "venting_from_slowing_atm",
                                                      "slowing_fast", "rough_from_high_vac", "high_vac"):
                continue                                  # quick: the worst five
            if which == "vc100" and not at_least("full") and sit not in VC100_NORMAL:
                continue                                  # the six-turbo plant is slow to simulate
            if which == "vc100" and not at_least("normal") and sit not in ("venting_coasting_atm", "venting_from_slowing_atm"):
                continue
            for button in (WAIT,) + ALL_BUTTONS:
                yield which, sit, button


@pytest.mark.parametrize("which,situation,button", list(_cells()))
def test_button_in_situation(which, situation, button):
    r = make(which, situation, "long_coast")
    visible = r.buttons()
    if button != WAIT:
        if button not in visible and not at_least("full"):
            pytest.skip("not on this page (pressed anyway at SAFETY_LEVEL=full)")
        r.press(button)
    r.run(RUN_ON_S)
    assert_no_new_hazard(r, f"{situation} then {button}")


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("plant", [p for p in PLANTS if p != "long_coast"])
@pytest.mark.parametrize("situation", SPINNING_SITUATIONS)
def test_spinning_situations_on_other_plants(which, plant, situation):
    """The same table for the situations with a turning rotor, on a plant with the demo simulator's
    short coast-down and on one with a much stronger roughing pump: the hazards depend on what is
    still turning when the pressure arrives."""
    if not at_least("normal") or (which == "vc100" and not at_least("full")):
        pytest.skip("SAFETY_LEVEL=full" if which == "vc100" else "SAFETY_LEVEL=normal")
    first = make(which, situation, plant)
    buttons = first.buttons() or []
    for button in buttons:
        r = make(which, situation, plant)
        r.press(button)
        r.run(RUN_ON_S)
        assert_no_new_hazard(r, f"{plant} plant: {situation} then {button}")
