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
def test_profile_describes_two_relays(cfg):
    pc = cfg.primary
    assert pc.two_stage and pc.power_cmd == "Mod3/port0/line6" and pc.run_cmd == "Mod3/port0/line7"
    assert pc.cmd is None and not pc.has_read          # the old command line and DI read-back are gone
    # the gaps the test engineer asked for on 2026-09-21
    assert cfg.timings.primary_power_to_run_gap_s == 15.0
    assert cfg.timings.primary_run_to_power_off_gap_s == 300.0


def test_frequency_conversion_when_re_enabled(cfg_freq):
    fq = cfg_freq.primary.frequency
    assert fq.channel == "Mod1/ai6" and (fq.max_v, fq.max_hz) == (10.0, 210.0)
    assert fq.hz(10.0) == pytest.approx(210.0)
    assert fq.hz(5.0) == pytest.approx(105.0)
    assert fq.hz(0.0) == pytest.approx(0.0)


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


def test_pump_only_turns_once_both_relays_are_closed(cfg_freq):
    cfg = cfg_freq
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
    assert b.state.primary_on == 0.0          # no pumping speed at all
    assert not b.read().primary_read


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
def test_error_5000_when_commanded_but_never_spinning(cfg_freq):
    """Run commanded, frequency stays at zero past the timeout -> error 5000 stops Auto mode.

    Only reachable with the frequency feedback enabled; see test_spinup_error_cannot_fire_while_suppressed
    for what the shipped profile does instead."""
    from facility_control.model import ERR_PRIMARY_CONFLICT
    cfg = cfg_freq
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("primary_fault", True)          # powered and commanded, but it will not turn
    _demand_primary(h)
    s, _ = h.run_until(lambda s: s.error.status, 20000)
    assert s.error.code == ERR_PRIMARY_CONFLICT
    assert "not turning" in s.error.source
    assert not s.primary_running


def test_no_error_while_the_pump_is_still_spinning_up(cfg_freq):
    """The timeout must be long enough that a healthy start is never flagged."""
    h = Harness(cfg_freq, mode=Mode.ADMIN)
    h.step(2)
    _demand_primary(h)
    s, _ = h.run_until(lambda s: s.primary_running, 6000)
    assert not s.error.status


def test_no_error_while_the_pump_coasts_down(cfg_freq):
    """A pump still turning after the run command was removed is normal, not a conflict."""
    h = Harness(cfg_freq, mode=Mode.ADMIN)
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
def test_run_hours_count_rotation_not_the_command(cfg_freq, tmp_path):
    """With the frequency enabled the meter follows rotation, so a pump that never spins accrues nothing."""
    from facility_control.runhours import RunHours
    h = Harness(cfg_freq, mode=Mode.ADMIN, run_hours=RunHours(str(tmp_path / "rh.json")))
    h.step(2)
    h.backend.set_fault("primary_fault", True)
    _demand_primary(h)
    for _ in range(400):
        h.step()
    assert h.ctl.snapshot().primary_run_hours == 0.0      # commanded the whole time, never turned
    h.backend.set_fault("primary_fault", False)
    s, _ = h.run_until(lambda s: s.primary_run_hours > 0.0, 8000)
    assert s.primary_running


def test_csv_logs_the_relays_and_the_frequency(cfg_freq, tmp_path):
    import csv as _csv
    from facility_control.logging_csv import CsvLogger
    cfg = cfg_freq
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


# ---------------------------------------------------------------- frequency suppressed (2026-09-21)
# Reading the drive frequency on Mod1/ai6 put noise on the other analog channels, so the block is
# commented out of the profile.  These tests pin what that means, and would fail the moment the
# channel is acquired again without the rest of the plumbing being re-enabled deliberately.
def test_frequency_reader_is_configured_but_off_by_default(cfg):
    """The sensor is described in the profile so the operator can switch it on, but it starts OFF
    (acquiring it put noise on the other analog inputs, 2026-09-21)."""
    pc = cfg.primary
    assert pc.has_frequency, "the block must stay in the profile so the panel can offer the switch"
    assert pc.frequency.enabled is False, "it must start switched off"
    assert not pc.has_read, "there is no boolean read-back either"
    assert pc.cross_check, "the spin-up check is armed for when the reader is switched on"


def test_reader_off_means_no_reading_at_all(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    s = h.step(2)
    assert s.primary_frequency_available and not s.primary_frequency_enabled
    assert s.inputs.primary_hz is None


def test_status_falls_back_to_the_command(cfg):
    """No feedback -> 'running' means 'commanded to run', as in Main_V4.4."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    assert not h.ctl.snapshot().primary_running
    _demand_primary(h)
    s, _ = h.run_until(lambda s: s.commands.primary_run, 8000)
    assert s.primary_running and s.inputs.primary_hz is None
    assert not s.error.status


def test_spinup_error_cannot_fire_while_suppressed(cfg):
    """A pump that never turns is no longer detectable - the deliberate cost of suppressing ai6."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("primary_fault", True)
    _demand_primary(h)
    for _ in range(2000):
        s = h.step()
        assert not s.error.status, s.error.source
    assert h.backend.state.primary_on == 0.0          # really is not pumping
    assert s.primary_running                           # but the panel believes it is


def test_csv_keeps_the_frequency_columns_so_a_mid_run_toggle_is_logged(cfg, tmp_path):
    """The header is written once per day-file but the reader can be switched on at any time, so the
    columns stay and simply read blank while it is off."""
    from facility_control.logging_csv import CsvLogger
    logger = CsvLogger(cfg, str(tmp_path))
    header = logger.header()
    assert {"primary_hz", "primary_running", "primary_power_cmd", "primary_run_cmd"} <= set(header)


# ---------------------------------------------------------------- one definition of "running"
# The controller and the Manual-mode interlocks once had their own fallbacks.  With no feedback
# fitted the controller said "running" from the demand (so it claimed the pump was turning during
# the 15 s power-up gap) while the interlocks said it from `primary_read`, which a feedback-less
# pump never sets - blocking "Please Turn Primary Pump On first" for ever and making the turbo
# unstartable in Manual mode.  They now share model.primary_is_running.
def test_not_running_during_the_power_up_gap(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    s = _demand_primary(h)
    assert s.commands.primary and s.primary_phase == "powering"
    assert not s.primary_running, "the run relay is still open - the pump cannot be turning"
    s, _ = h.run_until(lambda s: s.commands.primary_run, 8000)
    assert s.primary_running


def test_controller_and_interlocks_agree(cfg):
    """Whatever the panel says, the interlocks must believe the same thing."""
    from facility_control.interlocks import Interlocks
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    il = Interlocks(cfg)
    _demand_primary(h)
    for _ in range(400):
        s = h.step()
        assert il._primary_running(h.ctl.cmds, h.ctl.inputs) == s.primary_running


def test_turbo_is_startable_in_manual_with_no_pump_feedback(cfg):
    """The bug the shared definition fixes: a feedback-less pump must not veto the turbo for ever."""
    h = Harness(cfg, mode=Mode.MANUAL)
    h.step(2)
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("primary"); h.step()
    s, _ = h.run_until(lambda s: s.commands.primary_run, 8000)
    h.dialogs.answers.put(True); h.ctl.request_user_cmd("valve:turbo_valve"); h.step()
    h.ctl.request_user_cmd("turbo:turbo1"); h.step()
    # it must now be complaining about the CHILLER, i.e. it got past the primary-pump check
    assert h.dialogs.log[-1] == ("info", "Please Turn Chiller On First"), h.dialogs.log[-1]


# ---------------------------------------------------------------- the ON/OFF switch (2026-09-21)
# The operator decides whether the drive-frequency sensor is physically connected.  OFF must mean
# the channel is not acquired AT ALL - merely ignoring it would still put noise on the other
# analog inputs, which is the whole reason the switch exists.
def test_switching_the_reader_on_and_off_at_runtime(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    s = h.step(2)
    assert not s.primary_frequency_enabled and s.inputs.primary_hz is None

    h.ctl.request_set_frequency_enabled(True)
    s = h.step()
    assert s.primary_frequency_enabled and s.inputs.primary_hz is not None

    h.ctl.request_set_frequency_enabled(False)
    s = h.step()
    assert not s.primary_frequency_enabled and s.inputs.primary_hz is None


def test_running_state_follows_whichever_evidence_exists(cfg):
    """Reader on -> real rotation decides.  Reader off -> the run relay decides.  Never 'stuck off'."""
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    _demand_primary(h)
    s, _ = h.run_until(lambda s: s.commands.primary_run, 8000)
    assert s.primary_running                                    # reader off: the run relay says so

    h.ctl.request_set_frequency_enabled(True)                   # now the real speed decides
    s, _ = h.run_until(lambda s: s.inputs.primary_hz is not None, 200)
    s, _ = h.run_until(lambda s: s.primary_running, 4000)
    assert s.inputs.primary_hz > cfg.primary.frequency.running_above_hz


def test_spinup_error_only_armed_while_the_reader_is_on(cfg):
    """With the reader off a dead pump is undetectable; switching it on must arm the check."""
    from facility_control.model import ERR_PRIMARY_CONFLICT
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.backend.set_fault("primary_fault", True)                  # powered and commanded, will not turn
    _demand_primary(h)
    for _ in range(1500):
        s = h.step()
        assert not s.error.status, "reader off -> nothing to check against"
    h.ctl.request_set_frequency_enabled(True)
    s, _ = h.run_until(lambda s: s.error.status, 20000)
    assert s.error.code == ERR_PRIMARY_CONFLICT and "not turning" in s.error.source


def test_the_switch_is_logged(cfg):
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    h.ctl.request_set_frequency_enabled(True)
    h.step()
    assert any("frequency reader switched ON" in l for l in h.ctl.snapshot().event_log)
    h.ctl.request_set_frequency_enabled(False)
    h.step()
    assert any("frequency reader switched OFF" in l for l in h.ctl.snapshot().event_log)


def test_toggling_a_facility_without_the_sensor_is_a_no_op():
    """The VC100 has no frequency block - the switch must simply not exist there."""
    from facility_control.config import CONFIG_DIR, load_config
    vc = load_config(str(CONFIG_DIR / "facility_vc100.yaml"))
    assert not vc.primary.has_frequency
    h = Harness(vc, mode=Mode.ADMIN)
    s = h.step(2)
    assert not s.primary_frequency_available
    h.ctl.request_set_frequency_enabled(True)
    s = h.step()
    assert not s.primary_frequency_enabled and s.inputs.primary_hz is None
