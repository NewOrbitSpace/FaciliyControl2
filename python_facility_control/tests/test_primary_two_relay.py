"""The two-relay primary pump (hardware change 2026-09-14, small chamber).

Power (Mod3/port0/line6) and run (Mod3/port0/line7) are separate relays and the run relay only has
an effect once the pump is powered, so the controller sequences power -> gap -> run on start and
run -> gap -> power off on stop.  The pump's only feedback is its drive frequency on Mod1/ai6
(0-10 V = 0-210 Hz); "running" means that frequency is above `running_above_hz`.
"""
import pytest

from facility_control.config import CONFIG_DIR, load_config
from facility_control.model import Commands, Mode
from tests.conftest import Harness


# ---------------------------------------------------------------- profile
def test_profile_describes_two_relays_and_a_frequency(cfg):
    pc = cfg.primary
    assert pc.two_stage and pc.power_cmd == "Mod3/port0/line6" and pc.run_cmd == "Mod3/port0/line7"
    assert pc.cmd is None and not pc.has_read          # the old command line and DI read-back are gone
    assert pc.has_frequency and pc.frequency.channel == "Mod1/ai6"
    assert (pc.frequency.max_v, pc.frequency.max_hz) == (10.0, 210.0)
    assert pc.frequency.hz(10.0) == pytest.approx(210.0)
    assert pc.frequency.hz(5.0) == pytest.approx(105.0)
    assert pc.frequency.hz(0.0) == pytest.approx(0.0)
    assert cfg.timings.primary_power_to_run_gap_s > 0 and cfg.timings.primary_run_to_power_off_gap_s > 0


def test_vc100_keeps_its_single_relay_pump():
    """The shared engine must not have grown a small-chamber assumption."""
    vc = load_config(str(CONFIG_DIR / "facility_vc100.yaml"))
    assert not vc.primary.two_stage and vc.primary.cmd == "Mod4/port0/line0"
    assert vc.primary.has_read and not vc.primary.has_frequency


# ---------------------------------------------------------------- start sequence
def _demand_primary(h):
    """Admin mode: click the pump and confirm the dialog."""
    h.dialogs.answers.put(True)
    h.ctl.request_user_cmd("primary")
    return h.step()


def test_power_relay_closes_first_and_run_follows_after_the_gap(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    s = _demand_primary(h)
    # first iteration after the command: powered, but the run relay is still open
    assert s.commands.primary is True
    assert s.commands.primary_power is True and s.commands.primary_run is False
    assert s.primary_phase == "powering"
    assert h.backend.state.primary_hz == 0.0
    t0 = h.backend._sim_time
    s, _ = h.run_until(lambda s: s.commands.primary_run, 4000)
    gap = h.backend._sim_time - t0
    assert gap >= cfg.timings.primary_power_to_run_gap_s * 0.8, f"run relay closed after only {gap:.1f} s"
    assert s.commands.primary_power is True and s.primary_phase == "running"


def test_pump_only_turns_once_both_relays_are_closed(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    _demand_primary(h)
    s, _ = h.run_until(lambda s: s.primary_running, 6000)
    assert s.inputs.primary_hz > cfg.primary.frequency.running_above_hz
    assert s.commands.primary_power and s.commands.primary_run
    s, _ = h.run_until(lambda s: s.inputs.primary_hz > 200.0, 6000)
    assert s.inputs.primary_hz <= cfg.primary.frequency.max_hz


def test_run_relay_alone_does_nothing_without_power(cfg):
    """The failure the sequencing exists to prevent: closing the run relay on an unpowered pump."""
    b = Harness(cfg, mode=Mode.ADMIN).backend
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.primary = c.primary_run = True          # run asked for, power deliberately left open
    b.write(c)
    b.advance(60)
    assert b.state.primary_hz == 0.0
    assert b.read().primary_hz == 0.0


# ---------------------------------------------------------------- stop sequence
def test_stop_drops_run_first_then_power_after_the_gap(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    _demand_primary(h)
    h.run_until(lambda s: s.primary_running, 6000)
    s = _demand_primary(h)                     # click again -> stop
    assert s.commands.primary is False
    assert s.commands.primary_run is False and s.commands.primary_power is True
    assert s.primary_phase == "stopping"
    t0 = h.backend._sim_time
    s, _ = h.run_until(lambda s: not s.commands.primary_power, 4000)
    gap = h.backend._sim_time - t0
    assert gap >= cfg.timings.primary_run_to_power_off_gap_s * 0.8, f"power removed after only {gap:.1f} s"
    assert s.primary_phase == ""


def test_exit_drops_both_relays_at_once(cfg):
    """The safe state is unconditional – no sequencing on the way out."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    _demand_primary(h)
    h.run_until(lambda s: s.primary_running, 6000)
    h.ctl._shutdown()
    assert not h.ctl.cmds.primary_power and not h.ctl.cmds.primary_run
    assert not h.backend.state.primary_power and not h.backend.state.primary_run


# ---------------------------------------------------------------- spin-up error
def test_error_5000_when_commanded_but_never_spinning(cfg):
    """Run commanded, frequency stays at zero past the timeout -> error 5000 stops Auto mode."""
    from facility_control.model import ERR_PRIMARY_CONFLICT
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("primary_fault", True)          # powered and commanded, but it will not turn
    _demand_primary(h)
    s, _ = h.run_until(lambda s: s.error.status, 20000)
    assert s.error.code == ERR_PRIMARY_CONFLICT
    assert "not turning" in s.error.source
    assert not s.primary_running


def test_no_error_while_the_pump_is_still_spinning_up(cfg):
    """The timeout must be long enough that a healthy start is never flagged."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    _demand_primary(h)
    s, _ = h.run_until(lambda s: s.primary_running, 6000)
    assert not s.error.status


def test_no_error_while_the_pump_coasts_down(cfg):
    """A pump still turning after the run command was removed is normal, not a conflict."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    _demand_primary(h)
    h.run_until(lambda s: s.primary_running, 6000)
    _demand_primary(h)                                   # stop
    for _ in range(600):
        s = h.step()
        assert not s.error.status, s.error.source
        if not s.primary_running:
            break


# ---------------------------------------------------------------- derived state
def test_run_hours_count_rotation_not_the_command(cfg, tmp_path):
    """The meter must follow the frequency, so a pump that never spins accrues nothing."""
    from facility_control.runhours import RunHours
    h = Harness(cfg, mode=Mode.ADMIN, run_hours=RunHours(str(tmp_path / "rh.json")))
    h.step(2)
    h.backend.set_fault("primary_fault", True)
    _demand_primary(h)
    for _ in range(400):
        h.step()
    assert h.ctl.snapshot().primary_run_hours == 0.0      # commanded the whole time, never turned
    h.backend.set_fault("primary_fault", False)
    s, _ = h.run_until(lambda s: s.primary_run_hours > 0.0, 8000)
    assert s.primary_running


def test_csv_logs_the_relays_and_the_frequency(cfg, tmp_path):
    import csv as _csv
    from facility_control.logging_csv import CsvLogger
    h = Harness(cfg, mode=Mode.ADMIN)
    logger = CsvLogger(cfg, str(tmp_path))
    h.ctl.csv = logger
    cfg.logging.csv_period_s = 0.0
    h.step(2)
    _demand_primary(h)
    h.run_until(lambda s: s.primary_running, 6000)
    logger.close()
    path = next(iter(tmp_path.glob("facility_main_v4.4_*.csv")))
    rows = list(_csv.DictReader(open(path)))
    assert {"primary_power_cmd", "primary_run_cmd", "primary_hz", "primary_running"} <= set(rows[0])
    assert any(r["primary_running"] == "1" and float(r["primary_hz"]) > 5.0 for r in rows)
