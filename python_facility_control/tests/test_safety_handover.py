"""Admin -> Auto: Auto takes over whatever line-up the operator left, and must make it safe.

Admin mode has no interlocks by design, and 'Change to Auto' recognises the state from the
COMMANDS alone (the VI's decision tree) - it does not look at the plant.  So Auto can be handed a
line-up that reads as an ordinary state but sits on top of a vented chamber or a coasting turbo
(2026-10-28: Admin -> Auto with TMP2/5 coasting at ~70 % opened a turbo valve onto a foreline at
atmosphere).  Each test builds such a plant by hand, hands over, and lets the monitor watch.

The monitor is off while the operator is in Admin (what he does there is his business) and for
`GRACE_S` after the handover - the time Auto needs for one decision and one valve travel.
"""
import pytest

from facility_control.model import FacilityState as S, Mode
from tests.safety_rig import CHAMBERS, Rig
from tests.safety_scenarios import PLANTS, assert_no_new_hazard, at_least

GRACE_S = 6.0
ATM = 760.0


def _admin(which):
    r = Rig(which, mode=Mode.ADMIN, tweak=PLANTS["long_coast"])
    r.monitor.enabled = False
    r.tick(2)
    return r


def _set(r, *, primary=False, chiller=False, bypass=False, vent=False, tv=False, gate=False, motor=False,
         speed=0.0, chamber=ATM, foreline=None, body=None):
    """Put commands AND plant where the test wants them (Admin: the commands simply stand)."""
    c, st = r.ctl.cmds, r.st
    c.primary, c.chiller = primary, chiller
    if hasattr(c, "primary_run"):
        c.primary_run = c.primary_power = primary
    for v in r.cfg.valves_of_kind("bypass"):
        c.valves[v.id] = bypass
    for v in r.cfg.valves_of_kind("vent"):
        c.valves[v.id] = vent
    for t in r.turbos:
        c.valves[t.turbo_valve] = tv
        c.valves[t.gate_valve] = gate
        c.turbo_motor[t.id] = motor
        st.turbos[t.id].speed_pct = speed
        st.turbos[t.id].p_body_torr = body if body is not None else (chamber if gate else 1e-3)
    st.p_chamber = chamber
    st.p_foreline = foreline if foreline is not None else (1e-2 if primary else ATM)
    # the pump and the chiller are RUNNING in this line-up, not just being started: a foreline with a
    # stopped pump behind it is at atmosphere within seconds (logs), which would wreck the line-up
    # before Auto ever sees it
    st.primary_cmd = st.primary_run = st.primary_power = primary
    st.primary_on = 1.0 if primary else 0.0
    if primary and r.cfg.primary.has_frequency:
        fq = r.cfg.primary.frequency
        st.primary_hz = r.backend.sim.primary_run_hz if r.backend.sim.primary_run_hz is not None else fq.max_hz
    st.chiller_cmd = chiller
    st.chiller_on = 1.0 if chiller else 0.0
    r.run(20.0)                                            # valves travel, pumps come up
    for t in r.turbos:                                     # ... and the rotors are where the test put them
        st.turbos[t.id].speed_pct = speed


def _hand_over(r, expect=None):
    r.ctl.request_change_to_auto()
    r.tick(1)
    if r.ctl.mode != Mode.AUTO:
        return False
    r.came_from_admin = True
    r.run(GRACE_S)
    r.monitor.enabled = True
    if expect is not None:
        assert r.ctl.current in expect, f"recognised as {r.ctl.current.label}"
    return True


LINEUPS = {
    # name: (kwargs for _set, what it reads as)
    "all_off__turbos_coasting__chamber_vented": (dict(speed=90.0, chamber=ATM), "Facility off"),
    "all_off__turbos_coasting__chamber_evacuated": (dict(speed=90.0, chamber=1e-4, foreline=ATM), "Facility off"),
    "roughing__bypass_open__turbos_coasting__vented": (dict(primary=True, chiller=True, bypass=True, speed=90.0,
                                                            chamber=ATM, foreline=ATM), "Pumping to Rough"),
    "roughing__bypass_shut__turbos_coasting__vented": (dict(primary=True, chiller=True, speed=90.0, chamber=ATM),
                                                       "Pumping to Rough"),
    "roughing__bypass_and_vent_open": (dict(primary=True, bypass=True, vent=True, chamber=ATM, foreline=ATM),
                                       "Pumping to Rough"),
    "venting_lineup__turbos_running__chamber_vented": (dict(primary=True, chiller=True, tv=True, motor=True,
                                                            speed=100.0, chamber=ATM, foreline=6e-2, body=1e-3),
                                                       "Venting"),
    "high_vac_lineup__turbos_stopped__chamber_vented": (dict(primary=True, chiller=True, tv=True, gate=True,
                                                             speed=0.0, chamber=ATM, foreline=ATM, body=ATM),
                                                        "Pumping to High Vac"),
    "high_vac_lineup__healthy": (dict(primary=True, chiller=True, tv=True, gate=True, motor=True, speed=100.0,
                                      chamber=1e-5, body=1e-5), "Pumping to High Vac"),
}


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("lineup", list(LINEUPS))
def test_auto_makes_the_line_up_safe(which, lineup):
    if which == "vc100" and not at_least("normal") and "coasting" not in lineup:
        pytest.skip("SAFETY_LEVEL=quick")
    kwargs, _ = LINEUPS[lineup]
    r = _admin(which)
    _set(r, **kwargs)
    if not _hand_over(r):
        pytest.skip("not recognised as an Auto state - the operator stays in Admin")
    r.run(1800.0)
    assert_no_new_hazard(r, f"Admin -> Auto from '{lineup}'")


@pytest.mark.parametrize("which", CHAMBERS)
@pytest.mark.parametrize("lineup", ["all_off__turbos_coasting__chamber_vented",
                                    "all_off__turbos_coasting__chamber_evacuated",
                                    "roughing__bypass_open__turbos_coasting__vented",
                                    "roughing__bypass_shut__turbos_coasting__vented",
                                    "venting_lineup__turbos_running__chamber_vented"])
def test_then_every_button(which, lineup):
    """... and whatever he presses next from there."""
    if which == "vc100" and not at_least("normal"):
        pytest.skip("SAFETY_LEVEL=quick")
    kwargs, _ = LINEUPS[lineup]
    probe = _admin(which)
    _set(probe, **kwargs)
    if not _hand_over(probe):
        pytest.skip("not recognised as an Auto state")
    for button in probe.buttons():
        r = _admin(which)
        _set(r, **kwargs)
        _hand_over(r)
        r.press(button)
        r.run(1800.0)
        assert_no_new_hazard(r, f"Admin -> Auto from '{lineup}', then {button}")


@pytest.mark.parametrize("which", CHAMBERS)
def test_an_unrecognised_line_up_stays_in_admin(which):
    """Vent and gate open together reads as nothing Auto knows: it must refuse, not guess."""
    r = _admin(which)
    _set(r, primary=True, vent=True, gate=True, chamber=ATM)
    assert not _hand_over(r)
    assert r.ctl.mode == Mode.ADMIN
