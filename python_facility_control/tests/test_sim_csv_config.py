import csv
import os

import pytest

from facility_control.config import CONFIG_DIR, build_config, load_config
from facility_control.hal.sim_backend import SimBackend
from facility_control.logging_csv import CsvLogger
from facility_control.model import Commands, Mode
from tests.conftest import Harness


def test_config_channel_map_is_the_vi_map(cfg):
    assert cfg.phys(cfg.valves["bypass"].cmd) == "cDAQ1Mod4/port0/line1"
    assert cfg.phys(cfg.valves["gate"].read) == "cDAQ1Mod2/port0/line5"
    # two-relay primary pump (hardware change 2026-09-14)
    assert cfg.phys(cfg.primary.power_cmd) == "cDAQ1Mod3/port0/line6"
    assert cfg.phys(cfg.primary.run_cmd) == "cDAQ1Mod3/port0/line7"
    assert cfg.phys(cfg.primary.frequency.channel) == "cDAQ1Mod1/ai6"
    assert cfg.primary.frequency.max_hz == 210.0
    assert cfg.phys(cfg.main_gauge.channel) == "cDAQ1Mod1/ai5"
    t = cfg.turbo("turbo1")
    assert t.control_mode == "bigred_dsub15"
    assert cfg.phys(t.params["motor_do"]) == "cDAQ1Mod3/port0/line4"
    assert cfg.thresholds.turbo_on_threshold_torr == pytest.approx(0.150012)   # 2e-1 mBar
    assert cfg.timings.gate_settle_ms == 9000


def test_config_rejects_unknown_formula(cfg):
    import yaml
    raw = yaml.safe_load(open(os.path.join(CONFIG_DIR, "facility_main_v4.4.yaml")))
    raw["gauges"][0]["formula"] = "nonsense"
    with pytest.raises(ValueError):
        build_config(raw)


def test_three_turbo_profile_loads_and_recognises_states():
    """The config layer must accept the VC100 3-turbo facility (full checks in test_vc100.py)."""
    cfg = load_config(str(CONFIG_DIR / "facility_vc100.yaml"))
    assert len(cfg.turbos) == 3 and len(cfg.valves) == 8
    h = Harness(cfg, mode=Mode.ADMIN)
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.set_primary(True)
    for t in cfg.turbos:
        c.valves[t.turbo_valve] = True
        c.valves[t.gate_valve] = True
    assert h.ctl.recognise_state(c)[2] == "Pumping to High Vac"


def test_simulator_pumps_down_and_vents(cfg):
    b = SimBackend(cfg)
    b.open()
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.set_primary(True)
    c.chiller = True
    c.valves["bypass"] = True
    b.write(c)
    b.advance(600)
    assert b.state.p_chamber < 0.25
    c.valves["turbo_valve"] = True
    b.write(c); b.advance(60)
    c.valves["bypass"] = False
    c.valves["gate"] = True
    c.turbo_motor["turbo1"] = True
    b.write(c); b.advance(400)
    assert b.state.turbos["turbo1"].speed_pct == pytest.approx(100.0)
    assert b.state.p_chamber < 1e-4
    inp = b.read()
    assert inp.valve_reads["gate"] and not inp.valve_reads["bypass"]
    assert inp.pressures_torr["wrg"] < 1e-4
    # vent
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.valves["vent"] = True
    b.write(c); b.advance(600)
    assert b.state.p_chamber > 700


def test_simulator_faults(cfg):
    b = SimBackend(cfg); b.open()
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.valves["gate"] = True
    b.set_fault("compressor_low", True)
    b.write(c); b.advance(5)
    assert not b.read().valve_reads["gate"]          # needs compressed air to open
    b.set_fault("compressor_low", False); b.advance(5)
    assert b.read().valve_reads["gate"]
    b.set_fault("power_cut", True); b.advance(5)
    inp = b.read()
    assert not inp.valve_reads["gate"] and not inp.primary_read


def test_csv_logger_writes_header_and_rows(cfg, tmp_path):
    h = Harness(cfg)
    logger = CsvLogger(cfg, str(tmp_path))
    h.ctl.csv = logger
    cfg.logging.csv_period_s = 0.0
    h.step(3)
    logger.close()
    files = list(tmp_path.glob("facility_main_v4.4_*.csv"))
    assert len(files) == 1
    rows = list(csv.reader(open(files[0])))
    assert rows[0][:6] == ["timestamp", "iso_time", "mode", "current_state", "target_state", "substate"]
    assert "wrg_torr" in rows[0] and "wrg_mbar" in rows[0] and "bypass_cmd" in rows[0] and "turbo1_speed_pct" in rows[0]
    assert len(rows) >= 3


def test_main_v44_has_no_compressor_or_extra_analog(cfg):
    """Removed on request: the small chamber has neither the compressor sensor nor 'Com Potential'.
    Both come back purely through the YAML (extra_analog list / compressor block)."""
    from facility_control.logging_csv import CsvLogger
    assert cfg.extra_analog == [] and not cfg.has_compressor
    b = SimBackend(cfg)
    b.open()
    inp = b.read()
    assert inp.compressor_bar is None and inp.extra_analog == {}
    cols = CsvLogger(cfg, "/tmp/unused-csv-dir").header()
    assert "compressor_bar" not in cols and not any(c.startswith("com_potential") for c in cols)
    # re-adding is a config change only
    cfg.extra_analog.append({"id": "com_potential", "label": "Com Potential (V)", "channel": "Mod1/ai6"})
    cfg.compressor_ai = "Mod8/ai16"
    b2 = SimBackend(cfg)
    b2.open()
    inp2 = b2.read()
    assert inp2.compressor_bar is not None and "com_potential" in inp2.extra_analog
    cols2 = CsvLogger(cfg, "/tmp/unused-csv-dir").header()
    assert "compressor_bar" in cols2 and "com_potential_volts" in cols2
