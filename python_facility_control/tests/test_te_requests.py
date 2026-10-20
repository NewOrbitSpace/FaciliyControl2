"""Changes requested by the test engineer (2026-09-08) on top of the VI behaviour.

1. Pumping to Rough start order depends on the chamber pressure
   (>5e1 mBar: bypass valve first; <5e1 mBar: primary pump first, bypass after a 20-30 s gap).
2. Pump to Rough / High Vac / Overnight Pump first ask whether the manual vent valve is closed.
3. Overnight Pump below 2e-1 mBar skips roughing entirely.
4. 'Primary pump total operating hours' meter.
Plus the mBar-by-default / 2e-1 mBar threshold settings.
"""
import json

import pytest

from facility_control.automode import SUB_ROUGH_BYPASS_FIRST, SUB_ROUGH_PRIMARY_GAP, SUB_ROUGH_WAIT
from facility_control.controller import MANUAL_VENT_QUESTION
from facility_control.model import FacilityState as S, Mode
from facility_control.runhours import RunHours
from facility_control.units import torr_to, to_torr
from tests.conftest import Harness


# --------------------------------------------------------------------------- settings
def test_profile_defaults_are_mbar_and_2e_1_mbar(cfg):
    assert cfg.pressure_unit_default == "mbar"
    assert torr_to("mbar", cfg.thresholds.turbo_on_threshold_torr) == pytest.approx(0.2, rel=1e-3)
    assert torr_to("mbar", cfg.thresholds.bypass_first_above_torr) == pytest.approx(50.0, rel=1e-3)
    assert torr_to("mbar", cfg.thresholds.overnight_skip_below_torr) == pytest.approx(0.2, rel=1e-3)
    assert 20.0 <= cfg.timings.primary_to_bypass_gap_s <= 30.0


# --------------------------------------------------------------------------- start order
def test_high_pressure_opens_bypass_before_the_primary_pump(harness):
    """At atmosphere (>5e1 mBar) the bypass valve must open before the primary pump starts."""
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    h.step()                                     # vent-valve confirmation (auto-answered Yes)
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH
    s = h.step()                                 # entry frame
    assert s.substate == SUB_ROUGH_BYPASS_FIRST
    assert s.commands.valves["bypass"] is True and s.commands.primary is False
    s = h.step()                                 # now the pump starts, bypass stays open
    assert s.commands.primary is True and s.commands.valves["bypass"] is True
    assert s.substate == SUB_ROUGH_WAIT
    assert any("before starting the primary pump" in l for l in s.event_log)


def test_low_pressure_starts_primary_then_bypass_after_the_gap(cfg):
    """Below 5e1 mBar the primary pump runs first and the bypass follows 20-30 s later."""
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = 10.0             # 10 Torr = 13 mBar -> low-pressure branch
    h.step(2)
    h.ctl.request_auto_button("pump_to_rough")
    # no vent-valve question down here: below 5e2 mBar it is skipped (2026-09-23)
    h.step()                                     # Facility Off -> Pumping to Rough
    s = h.step()                                 # entry frame: pump on, bypass still closed
    assert s.substate == SUB_ROUGH_PRIMARY_GAP
    assert s.commands.primary is True and s.commands.valves["bypass"] is False
    t_primary = h.backend._sim_time
    # the bypass must stay shut for the gap, then open
    s, _ = h.run_until(lambda s: s.commands.valves["bypass"] is True, 4000)
    gap = h.backend._sim_time - t_primary
    assert s.substate == SUB_ROUGH_WAIT
    assert gap >= cfg.timings.primary_to_bypass_gap_s * 0.8, f"bypass opened after only {gap:.1f} s"
    assert any("after the primary pump" in l for l in s.event_log)


# --------------------------------------------------------------------------- vent valve dialog
def test_pump_buttons_ask_about_the_manual_vent_valve(harness):
    h = harness
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    s = h.step()
    assert any(MANUAL_VENT_QUESTION in msg for _, msg in h.dialogs.log)
    assert s.current == S.FACILITY_OFF           # nothing happens until the question is answered
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH       # answered Yes -> the button runs


def test_answering_no_cancels_the_pump_down(harness):
    h = harness
    h.step(2)
    h.dialogs.answers.put(1)                     # 'No'
    h.ctl.request_auto_button("pump_to_high_vac")
    s = h.step(3)
    assert s.current == S.FACILITY_OFF and not s.commands.primary
    assert not any(s.commands.valves.values())
    assert any("manual vent valve not confirmed" in l for l in s.event_log)


# --------------------------------------------------------------------------- overnight skip
def test_overnight_pump_below_2e_1_mbar_skips_roughing(cfg):
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = to_torr("mbar", 0.05)   # already well below 2e-1 mBar
    h.step(3)
    h.ctl.request_auto_button("overnight_pump")
    h.step()                                     # confirmation
    s = h.step()
    assert s.current == S.OVERNIGHT_PUMP         # straight to the hold, no roughing
    assert s.commands.primary is False and not any(s.commands.valves.values())
    assert any("without roughing" in l for l in s.event_log)


def test_overnight_pump_above_the_threshold_still_roughs(harness):
    h = harness                                  # starts at atmosphere
    h.step(2)
    h.ctl.request_auto_button("overnight_pump")
    h.step()
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH and s.target == S.OVERNIGHT_PUMP


# --------------------------------------------------------------------------- run-hour meter
def test_primary_run_hours_accumulate_and_persist(cfg, tmp_path):
    path = tmp_path / "run_hours.json"
    h = Harness(cfg, mode=Mode.ADMIN, run_hours=RunHours(str(path), save_period_s=0.0))
    h.step(2)
    assert h.ctl.snapshot().primary_run_hours == 0.0
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("primary")
    h.step()
    s, _ = h.run_until(lambda s: s.primary_run_hours > 0.02, 4000)   # >72 s of simulated running
    # this pump has no boolean read-back at all (two relays, frequency reader off), so the meter
    # follows model.primary_is_running - the run relay - rather than inputs.primary_read
    assert s.primary_running is True and s.commands.primary_run is True
    hours = s.primary_run_hours
    # the meter is written to disk and read back by a fresh instance
    h.ctl.run_hours.save(force=True, now=h.ctl.clock())
    assert json.loads(path.read_text())["seconds"]["primary"] > 0
    assert RunHours(str(path)).hours("primary") == pytest.approx(hours, rel=1e-6)


def test_run_hours_do_not_count_while_the_pump_is_off(cfg, tmp_path):
    h = Harness(cfg, mode=Mode.ADMIN, run_hours=RunHours(str(tmp_path / "h.json")))
    h.step(30)
    assert h.ctl.snapshot().primary_run_hours == 0.0


# --------------------------------------------------------------------------- start order: every button
# Restated by the test engineer on 2026-09-21: the pressure-dependent start order must apply to
# *all three* pump-down buttons, not just Pump to Rough, because all three route through
# Pumping to Rough.  One test per button per branch, so a regression names the exact case.
@pytest.mark.parametrize("button", ["pump_to_rough", "pump_to_high_vac", "overnight_pump"])
def test_bypass_opens_first_above_5e1_mbar_for_every_button(cfg, button):
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = cfg.simulation.atmosphere_torr     # 760 Torr, well above 5e1 mBar
    h.step(2)
    h.ctl.request_auto_button(button)
    h.step()                                     # manual-vent-valve confirmation (auto-answered Yes)
    s = h.step()
    assert s.current == S.PUMPING_TO_ROUGH, button
    s = h.step()                                 # entry frame decides the order
    assert s.substate == SUB_ROUGH_BYPASS_FIRST, button
    assert s.commands.valves["bypass"] is True and s.commands.primary is False, button
    s = h.step()
    assert s.commands.primary is True and s.commands.valves["bypass"] is True, button


@pytest.mark.parametrize("button", ["pump_to_rough", "pump_to_high_vac", "overnight_pump"])
def test_primary_first_then_bypass_below_5e1_mbar_for_every_button(cfg, button):
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = 10.0             # 10 Torr = 13 mBar -> below 5e1 mBar, above the skip
    h.step(2)
    h.ctl.request_auto_button(button)             # below 5e2 mBar: no vent-valve question
    h.step()
    s = h.step()                                 # entry frame: pump first, bypass still shut
    assert s.substate == SUB_ROUGH_PRIMARY_GAP, button
    assert s.commands.primary is True and s.commands.valves["bypass"] is False, button
    t0 = h.backend._sim_time
    s, _ = h.run_until(lambda s: s.commands.valves["bypass"] is True, 6000)
    gap = h.backend._sim_time - t0
    assert 20.0 <= gap <= 30.0 * 1.25, f"{button}: bypass opened after {gap:.1f} s, wanted 20-30 s"


def test_the_threshold_really_is_5e1_mbar(cfg):
    """The branch point, checked either side of 5e1 mBar rather than at atmosphere."""
    from facility_control.units import to_torr
    just_above = to_torr("mbar", 55.0)
    just_below = to_torr("mbar", 45.0)
    assert just_below < cfg.thresholds.bypass_first_above_torr < just_above
    for p_torr, want in ((just_above, SUB_ROUGH_BYPASS_FIRST), (just_below, SUB_ROUGH_PRIMARY_GAP)):
        h = Harness(cfg, mode=Mode.AUTO)
        h.backend.state.p_chamber = p_torr
        h.backend.sim.time_scale = 0.0           # hold the pressure where we put it
        h.step(2)
        h.ctl.request_auto_button("pump_to_rough")   # both points are below 5e2 mBar: no question
        h.step()
        s = h.step()
        assert s.substate == want, f"{p_torr:.3g} Torr took the wrong branch"


def test_overnight_below_2e_1_mbar_starts_neither_pump_nor_bypass(cfg):
    """Below 2e-1 mBar Overnight Pump holds straight away: no primary, no bypass, no roughing."""
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = 0.1              # 0.1 Torr = 0.13 mBar -> below the skip threshold
    h.backend.sim.time_scale = 0.0
    h.step(2)
    h.ctl.request_auto_button("overnight_pump")
    h.step()                                     # vent-valve confirmation
    s = h.step()
    assert s.current == S.OVERNIGHT_PUMP, "should skip Pumping to Rough entirely"
    assert s.commands.primary is False
    assert s.commands.valves["bypass"] is False
    assert not any(s.commands.valves.values())
    assert any("without roughing" in l for l in s.event_log)
    # and it must stay that way, not drift into roughing a few frames later
    for _ in range(50):
        s = h.step()
        assert s.commands.primary is False and s.commands.valves["bypass"] is False


# --------------------------------------------------------------------------- vent question, gated
# (2026-09-23) "Is the manual vent valve closed?" is only worth asking near atmosphere.  Below
# thresholds.vent_confirm_above_torr (5e2 mBar) the chamber is plainly already pumped down, which is
# itself proof the hand valve is shut, so the dialog is skipped.
def test_vent_question_is_asked_at_atmosphere(cfg):
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = cfg.simulation.atmosphere_torr
    h.backend.sim.time_scale = 0.0
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.step()
    assert any(m == MANUAL_VENT_QUESTION for _k, m in h.dialogs.log)


@pytest.mark.parametrize("mbar", [400.0, 1.0, 1e-3])
def test_vent_question_is_skipped_once_pumped_down(cfg, mbar):
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = to_torr("mbar", mbar)
    h.backend.sim.time_scale = 0.0
    h.step(2)
    before = len(h.dialogs.log)
    h.ctl.request_auto_button("pump_to_rough")
    s = h.step(2)
    asked = [m for _k, m in h.dialogs.log[before:] if m == MANUAL_VENT_QUESTION]
    assert not asked, f"asked about the hand vent valve at {mbar:g} mBar"
    assert s.current == S.PUMPING_TO_ROUGH, "the button must act immediately instead"


def test_vent_question_is_asked_either_side_of_the_threshold(cfg):
    for mbar, want in ((550.0, True), (450.0, False)):
        h = Harness(cfg, mode=Mode.AUTO)
        h.backend.state.p_chamber = to_torr("mbar", mbar)
        h.backend.sim.time_scale = 0.0
        h.step(2)
        h.ctl.request_auto_button("overnight_pump")
        h.step()
        asked = any(m == MANUAL_VENT_QUESTION for _k, m in h.dialogs.log)
        assert asked is want, f"at {mbar:g} mBar the question should {'appear' if want else 'not appear'}"


def test_an_unreadable_gauge_still_asks(cfg):
    """If the chamber reading cannot be trusted we know nothing, so the question comes back."""
    h = Harness(cfg, mode=Mode.AUTO)
    h.step(2)
    h.backend.set_fault("gauge_fault_wrg", True)
    h.step(2)
    before = len(h.dialogs.log)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.step()
    assert any(m == MANUAL_VENT_QUESTION for _k, m in h.dialogs.log[before:])


def test_setting_the_threshold_to_null_always_asks(cfg):
    cfg.thresholds.vent_confirm_above_torr = None
    h = Harness(cfg, mode=Mode.AUTO)
    h.backend.state.p_chamber = to_torr("mbar", 1e-3)
    h.backend.sim.time_scale = 0.0
    h.step(2)
    h.ctl.request_auto_button("pump_to_high_vac")
    h.step()
    assert any(m == MANUAL_VENT_QUESTION for _k, m in h.dialogs.log)
