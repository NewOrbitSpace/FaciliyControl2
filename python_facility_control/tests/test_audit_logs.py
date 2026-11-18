"""tools/audit_logs.py: the safety rules applied to the facility's own CSV logs."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_logs  # noqa: E402
from facility_control.config import CONFIG_DIR, load_config  # noqa: E402
from facility_control.logging_csv import CsvLogger  # noqa: E402


def _cfg():
    return load_config(str(CONFIG_DIR / "facility_vc100.yaml"))


def _row(t, state="Pumping to High Vac", mode="AUTO", **kw):
    """A healthy high-vacuum row; keyword arguments override columns."""
    row = {"timestamp": f"{1_800_000_000 + t:.3f}", "iso_time": f"2026-11-05 09:{t // 60:02d}:{t % 60:02d}", "mode": mode,
           "current_state": state, "target_state": state, "wrg_mbar": "1e-6", "fore_mbar": "5e-3",
           "bypass_cmd": "0", "bypass_read": "0", "primary_cmd": "1", "chiller_cmd": "1", "error_status": "0"}
    for i in range(1, 7):
        row.update({f"gate{i}_cmd": "1", f"gate{i}_read": "1", f"turbo{i}_motor_cmd": "1", f"turbo{i}_speed_pct": "100.0",
                    f"turbo{i}_rotating": "1"})
    for i in (1, 2, 3):
        row.update({f"turbo{i}_valve_cmd": "1", f"turbo{i}_valve_read": "1", f"turbo{i}_gauge_volts": "3.2"})
    row.update({"wrg_volts": "3.0", "fore_volts": "3.2"})
    row.update({k: str(v) for k, v in kw.items()})
    return row


def _kinds(rep):
    return sorted({e.kind for e in rep.episodes})


def test_a_healthy_run_is_clean_and_the_margins_are_reported():
    rows = [_row(0, wrg_mbar=1.3e-1, fore_mbar=8e-2), _row(5, wrg_mbar=4e-2, fore_mbar=3.9e-1),
            _row(10, wrg_mbar=1e-5), _row(15, wrg_mbar=1.2e-4), _row(20)]
    rep = audit_logs.audit(rows, _cfg())
    assert not rep.episodes
    assert abs(rep.max_foreline_running_mbar - 0.39) < 1e-9          # the run-up bump
    assert abs(rep.max_chamber_high_vac_mbar - 1.2e-4) < 1e-12       # gas flow once pumped down


def test_the_5_november_incident_is_found():
    """Venting at 78 %, Pump to High Vac: bypass open, Turbo Valves 2 and 3 open, foreline at atmosphere."""
    vent = dict(state="Venting", wrg_mbar=1000, **{f"gate{i}_cmd": 0 for i in range(1, 7)}, **{f"gate{i}_read": 0 for i in range(1, 7)},
                **{f"turbo{i}_motor_cmd": 0 for i in range(1, 7)}, turbo1_rotating=0, turbo4_rotating=0,
                turbo1_valve_cmd=0, turbo1_valve_read=0)
    spd = lambda v: {f"turbo{i}_speed_pct": v for i in (2, 3, 5, 6)}             # noqa: E731
    rows = [_row(0, **vent, **spd(78.0)),
            _row(5, **{**vent, "state": "Pumping to Rough"}, **spd(12.0), bypass_cmd=1, bypass_read=1, fore_mbar=1025),
            _row(10, **{**vent, "state": "Pumping to Rough"}, **spd(5.5), bypass_cmd=1, bypass_read=1, fore_mbar=1025)]
    rep = audit_logs.audit(rows, _cfg())
    assert {"CRASH", "EXHAUST"} <= set(_kinds(rep))
    crashed = {e.what.split(":")[0] for e in rep.episodes if e.kind == "CRASH"}
    assert crashed == {"Turbo 2", "Turbo 3", "Turbo 5", "Turbo 6"}
    assert "78 % -> 12 %" in next(e.what for e in rep.episodes if e.kind == "CRASH")
    assert len(rep.serious) >= 8


def test_a_slow_coast_down_is_not_a_crash():
    rows = [_row(5 * k, state="Turbo slowing", **{f"turbo{i}_motor_cmd": 0 for i in range(1, 7)},
                 **{f"turbo{i}_speed_pct": 100.0 - 0.2 * k for i in (2, 3, 5, 6)},
                 **{f"gate{i}_cmd": 0 for i in range(1, 7)}, **{f"gate{i}_read": 0 for i in range(1, 7)}) for k in range(60)]
    assert not audit_logs.audit(rows, _cfg()).serious


def test_gates_closed_again_in_engage_is_found_but_one_stuck_gate_is_not():
    def engage(t, gates, standby=0):
        kw = {f"gate{i + 1}_cmd": g for i, g in enumerate(gates)}
        kw.update({f"gate{i + 1}_read": g for i, g in enumerate(gates)})
        kw.update({f"turbo{i}_standby_cmd": standby for i in range(1, 7)})
        return _row(t, state="Engage Turbo", wrg_mbar=6e-3, **kw)
    flip = [engage(0, "100000"), engage(10, "111100"), engage(20, "000000", 1), engage(25, "000000", 1)]
    rep = audit_logs.audit(flip, _cfg())
    assert _kinds(rep) == ["GATES"] and rep.episodes[0].rows == 2
    stuck = [engage(0, "111100"), engage(10, "111000"), engage(20, "111011")]          # gate 4 taken out of use
    assert not audit_logs.audit(stuck, _cfg()).episodes


def test_a_pressure_rise_in_high_vac_is_found_only_after_the_pump_down():
    entry = [_row(0, wrg_mbar=1.4e-1), _row(5, wrg_mbar=1.2e-1), _row(10, wrg_mbar=3e-2), _row(15, wrg_mbar=1e-4)]
    assert not audit_logs.audit(entry, _cfg()).episodes
    rep = audit_logs.audit(entry + [_row(20, wrg_mbar=2e-1), _row(25, wrg_mbar=3e-1)], _cfg())
    assert _kinds(rep) == ["LEAK"]


def test_chiller_off_with_turbos_spinning_is_information_not_an_alarm():
    rows = [_row(0, state="Facility Off", chiller_cmd=0, primary_cmd=0,
                 **{f"turbo{i}_motor_cmd": 0 for i in range(1, 7)}, **{f"turbo{i}_speed_pct": 60.0 for i in (2, 3, 5, 6)},
                 **{f"turbo{i}_valve_cmd": 0 for i in (1, 2, 3)}, **{f"turbo{i}_valve_read": 0 for i in (1, 2, 3)},
                 **{f"gate{i}_cmd": 0 for i in range(1, 7)}, **{f"gate{i}_read": 0 for i in range(1, 7)}, fore_mbar=1020)]
    rep = audit_logs.audit(rows, _cfg())
    assert _kinds(rep) == ["COOLING"] and not rep.serious


def test_it_reads_the_files_the_controller_writes(tmp_path):
    """Column names come from the same profile the logger uses; the profile is found from the file name."""
    cfg = _cfg()
    header = CsvLogger(cfg, str(tmp_path)).header()
    path = tmp_path / "facility_vc100_2026-11-05.csv"
    good = _row(0)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(",".join(header) + "\n")
        fh.write(",".join(good.get(c, "0") for c in header) + "\n")
    assert os.path.samefile(audit_logs.profile_for(str(path)), CONFIG_DIR / "facility_vc100.yaml")
    assert audit_logs.main([str(path)]) == 0
    for needed in ("wrg_mbar", "fore_mbar", "turbo2_speed_pct", "turbo2_valve_cmd", "gate2_cmd", "bypass_cmd",
                   "primary_cmd", "chiller_cmd", "turbo1_rotating", "error_status"):
        assert needed in header, needed


def test_a_gauge_with_no_signal_is_found():
    rows = [_row(0), _row(5, fore_volts=0.02), _row(10, fore_volts=0.01), _row(15, fore_volts=3.1)]
    rep = audit_logs.audit(rows, _cfg())
    assert _kinds(rep) == ["GAUGE"] and rep.episodes[0].rows == 2 and "Foreline" in rep.episodes[0].what
    assert not audit_logs.audit([_row(0, fore_volts=2.8, wrg_volts=3.0, turbo1_gauge_volts=2.7)], _cfg()).episodes
