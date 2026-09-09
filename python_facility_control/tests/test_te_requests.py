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
    h.step()                                     # vent-valve confirmation
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
    assert s.inputs.primary_read is True
    hours = s.primary_run_hours
    # the meter is written to disk and read back by a fresh instance
    h.ctl.run_hours.save(force=True, now=h.ctl.clock())
    assert json.loads(path.read_text())["seconds"]["primary"] > 0
    assert RunHours(str(path)).hours("primary") == pytest.approx(hours, rel=1e-6)


def test_run_hours_do_not_count_while_the_pump_is_off(cfg, tmp_path):
    h = Harness(cfg, mode=Mode.ADMIN, run_hours=RunHours(str(tmp_path / "h.json")))
    h.step(30)
    assert h.ctl.snapshot().primary_run_hours == 0.0
