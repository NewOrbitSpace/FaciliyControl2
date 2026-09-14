"""Exercise the NI-DAQmx backend against a *fake* nidaqmx module.

There is no cDAQ in CI, so this checks everything on the Python side: task/channel creation from the
profile (the VI's channel map), the shapes written to the DO tasks, the DI/AI result decoding, the
gauge conversion, the finite-AI start/stop handling and the read-only bring-up mode.  The real driver
behaviour (line polarity, timing) still has to be confirmed on the facility with tools/daq_check.py.
"""
import importlib
import sys
import types

import pytest

from facility_control.model import Commands


# ---------------------------------------------------------------------------- fake nidaqmx
class _Chan:
    def __init__(self, phys, name, **kw):
        self.phys, self.name, self.kw = phys, name, kw
        self.di_invert_lines = False


class _ChanCollection:
    def __init__(self, task, kind):
        self.task, self.kind = task, kind

    def add_do_chan(self, phys, name_to_assign_to_lines="", line_grouping=None):
        self.task.channels.append(_Chan(phys, name_to_assign_to_lines, grouping=line_grouping))
        self.task.kind = "do"
        return self.task.channels[-1]

    def add_di_chan(self, phys, name_to_assign_to_lines="", line_grouping=None):
        self.task.channels.append(_Chan(phys, name_to_assign_to_lines, grouping=line_grouping))
        self.task.kind = "di"
        return self.task.channels[-1]

    def add_ai_voltage_chan(self, phys, name_to_assign_to_channel="", terminal_config=None, min_val=0.0, max_val=10.0):
        self.task.channels.append(_Chan(phys, name_to_assign_to_channel, terminal_config=terminal_config,
                                        min_val=min_val, max_val=max_val))
        self.task.kind = "ai"
        return self.task.channels[-1]


class _Timing:
    def __init__(self):
        self.cfg = None

    def cfg_samp_clk_timing(self, rate, sample_mode=None, samps_per_chan=None):
        self.cfg = (rate, sample_mode, samps_per_chan)


class FakeTask:
    registry = {}
    di_values = {}          # phys -> bool
    ai_volts = {}           # phys -> volts

    def __init__(self, name=""):
        self.name = name
        self.channels = []
        self.kind = None
        self.do_channels = _ChanCollection(self, "do")
        self.di_channels = _ChanCollection(self, "di")
        self.ai_channels = _ChanCollection(self, "ai")
        self.timing = _Timing()
        self.writes = []
        self.stopped = 0
        self.closed = False
        FakeTask.registry[name] = self

    def write(self, data, auto_start=False):
        assert auto_start, "DO writes must auto-start"
        n = len(self.channels)
        if n > 1:
            assert isinstance(data, list) and len(data) == n, (self.name, data)
        else:
            assert isinstance(data, bool), (self.name, data)
        self.writes.append(data)

    def read(self, number_of_samples_per_channel=None, timeout=10.0):
        if self.kind == "di":
            vals = [bool(FakeTask.di_values.get(c.phys, False)) for c in self.channels]
            return vals if len(vals) > 1 else vals[0]
        if self.kind == "ai":
            assert number_of_samples_per_channel, "finite AI read must ask for the samples"
            rows = [[FakeTask.ai_volts.get(c.phys, 0.0)] * number_of_samples_per_channel for c in self.channels]
            return rows if len(rows) > 1 else rows[0]
        raise AssertionError("read on a DO task")

    def stop(self):
        self.stopped += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_daq(monkeypatch):
    FakeTask.registry.clear()
    FakeTask.di_values.clear()
    FakeTask.ai_volts.clear()
    mod = types.ModuleType("nidaqmx")
    mod.Task = FakeTask
    consts = types.ModuleType("nidaqmx.constants")
    consts.AcquisitionType = types.SimpleNamespace(FINITE="finite")
    consts.LineGrouping = types.SimpleNamespace(CHAN_PER_LINE="chan_per_line")
    consts.TerminalConfiguration = types.SimpleNamespace(DIFF="diff", RSE="rse", NRSE="nrse",
                                                         DEFAULT="default", PSEUDO_DIFF="pseudo_diff")
    mod.constants = consts
    monkeypatch.setitem(sys.modules, "nidaqmx", mod)
    monkeypatch.setitem(sys.modules, "nidaqmx.constants", consts)
    sys.modules.pop("facility_control.hal.nidaqmx_backend", None)
    backend_mod = importlib.import_module("facility_control.hal.nidaqmx_backend")
    yield backend_mod
    sys.modules.pop("facility_control.hal.nidaqmx_backend", None)


# ---------------------------------------------------------------------------- tests
def test_tasks_follow_the_vi_channel_map(cfg, fake_daq):
    b = fake_daq.NiDaqmxBackend(cfg)
    b.open()
    t = FakeTask.registry
    assert [c.phys for c in t["valve_cmd"].channels] == ["cDAQ1Mod4/port0/line0", "cDAQ1Mod4/port0/line1",
                                                        "cDAQ1Mod4/port0/line2", "cDAQ1Mod4/port0/line3"]
    assert [c.phys for c in t["valve_read"].channels] == ["cDAQ1Mod2/port0/line2", "cDAQ1Mod2/port0/line3",
                                                         "cDAQ1Mod2/port0/line4", "cDAQ1Mod2/port0/line5"]
    # two-relay primary pump: one DO task carrying [power, run]; no DI read-back any more
    assert [c.phys for c in t["primary_cmd"].channels] == ["cDAQ1Mod3/port0/line6", "cDAQ1Mod3/port0/line7"]
    assert "primary_read" not in t
    assert t["chiller_cmd"].channels[0].phys == "cDAQ1Mod3/port0/line1"
    assert t["chiller_read"].channels[0].phys == "cDAQ1Mod2/port0/line1"
    ai = t["analog_in"]
    # The main task carries ONLY the gauges.  The turbo speed and the pump frequency each get their
    # own single-channel task, as Main_V4.4.vi does - sharing one multiplexed task made the turbo
    # speed return the previous channel's voltage (the Turbo Convectron, ~69 % at atmosphere).
    assert [c.phys for c in ai.channels] == ["cDAQ1Mod1/ai5", "cDAQ1Mod1/ai2", "cDAQ1Mod1/ai1"]
    assert [c.phys for c in t["turbo1_speed_ai"].channels] == ["cDAQ1Mod1/ai4"]
    assert [c.phys for c in t["primary_hz_ai"].channels] == ["cDAQ1Mod1/ai6"]
    # the BigRed speed input is bipolar, the gauges and the drive output are not
    assert t["turbo1_speed_ai"].channels[0].kw["min_val"] == -10.0
    assert t["primary_hz_ai"].channels[0].kw["min_val"] == 0.0
    assert ai.timing.cfg == (1000.0, "finite", 200)
    assert all(c.kw["min_val"] == 0.0 and c.kw["max_val"] == 10.0 for c in ai.channels)   # gauges 0-10 V
    assert t["turbo1_speed_ai"].channels[0].kw["max_val"] == 10.0                         # BRT speed ±10 V
    # every AI channel must use the VI's terminal configuration (DIFFERENTIAL, value 10106) – not RSE
    from nidaqmx.constants import TerminalConfiguration
    assert all(c.kw["terminal_config"] == TerminalConfiguration.DIFF for c in ai.channels), \
        [c.kw["terminal_config"] for c in ai.channels]
    assert [c.phys for c in t["turbo1_cmd"].channels] == ["cDAQ1Mod3/port0/line4", "cDAQ1Mod3/port0/line5"]
    assert [c.phys for c in t["turbo1_read"].channels] == ["cDAQ1Mod2/port0/line7", "cDAQ1Mod2/port0/line8"]
    assert "turbo1_ack" not in t                      # BigRed has no error-ack line
    # open() ends with the safe state: everything off, one write per DO task
    assert t["valve_cmd"].writes == [[False, False, False, False]]
    assert t["primary_cmd"].writes == [[False, False]] and t["chiller_cmd"].writes == [False]
    assert t["turbo1_cmd"].writes == [[False, False]]


def test_write_and_read_decoding(cfg, fake_daq):
    b = fake_daq.NiDaqmxBackend(cfg)
    b.open()
    cmds = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    cmds.valves["bypass"] = True
    cmds.set_primary(True)
    cmds.turbo_motor["turbo1"] = True
    b.write(cmds)
    t = FakeTask.registry
    assert t["valve_cmd"].writes[-1] == [False, True, False, False]
    assert t["primary_cmd"].writes[-1] == [True, True] and t["chiller_cmd"].writes[-1] is False
    assert t["turbo1_cmd"].writes[-1] == [True, False]
    # plant answers: bypass open, WRG at 1e-3 Torr, foreline 0.5 Torr, turbo 45 % speed, error line high,
    # and the pump's drive reporting 105 Hz (= 5 V of the 0-10 V / 0-210 Hz range)
    FakeTask.di_values.update({"cDAQ1Mod2/port0/line3": True, "cDAQ1Mod2/port0/line7": True})
    from facility_control.gauges import torr_to_v
    FakeTask.ai_volts.update({"cDAQ1Mod1/ai5": torr_to_v("ion_gauge", 1e-3), "cDAQ1Mod1/ai2": torr_to_v("convectron", 0.5),
                              "cDAQ1Mod1/ai1": torr_to_v("convectron", 1e-3), "cDAQ1Mod1/ai4": 4.5,
                              "cDAQ1Mod1/ai6": 5.0})
    inp = b.read()
    assert not inp.daq_error
    assert inp.valve_reads == {"turbo_valve": False, "bypass": True, "vent": False, "gate": False}
    assert inp.primary_hz == pytest.approx(105.0)
    assert inp.primary_read is True and inp.chiller_read is False   # derived from the frequency
    assert inp.pressures_torr["wrg"] == pytest.approx(1e-3, rel=1e-6)
    assert inp.pressures_torr["conv2"] == pytest.approx(0.5, rel=1e-6)
    ti = inp.turbos["turbo1"]
    assert ti.speed_pct == pytest.approx(45.0) and ti.error is True and ti.motor_read is True   # D-SUB: motor = echoed command
    assert t["analog_in"].stopped == 1                 # finite task stopped after every read


def test_daq_error_is_reported_not_raised(cfg, fake_daq):
    b = fake_daq.NiDaqmxBackend(cfg)
    b.open()

    def boom(*a, **k):
        raise RuntimeError("DAQmx -50103 resource reserved")
    FakeTask.registry["analog_in"].read = boom
    inp = b.read()
    assert "resource reserved" in inp.daq_error


def test_read_only_mode_creates_no_output_task(cfg, fake_daq):
    b = fake_daq.NiDaqmxBackend(cfg, read_only=True)
    b.open()
    kinds = {name: task.kind for name, task in FakeTask.registry.items()}
    assert "do" not in kinds.values(), kinds
    assert {"valve_read", "chiller_read", "analog_in", "turbo1_read"} <= set(kinds)
    b.write(Commands.all_off(cfg.valve_ids, cfg.turbo_ids))      # no-op, must not fail
    b.safe_state()
    b.turbo_error_ack("turbo1", True)
    assert all(t.writes == [] for t in FakeTask.registry.values())
    assert "READ-ONLY" in b.describe()
    inp = b.read()
    assert not inp.daq_error and "wrg" in inp.pressures_torr
    b.close()
    assert all(t.closed for t in FakeTask.registry.values())


def test_daq_check_channel_inventory_matches_profile(cfg):
    from tools.daq_check import channel_inventory
    inv = channel_inventory(cfg)
    phys = {p for _, p, _ in inv}
    assert {"cDAQ1Mod4/port0/line0", "cDAQ1Mod2/port0/line5", "cDAQ1Mod1/ai5", "cDAQ1Mod3/port0/line4",
            "cDAQ1Mod2/port0/line8", "cDAQ1Mod1/ai4",
            # two-relay primary pump + its frequency feedback (hardware change 2026-09-14)
            "cDAQ1Mod3/port0/line6", "cDAQ1Mod3/port0/line7", "cDAQ1Mod1/ai6"} <= phys
    # the retired single command line and DI run read-back are gone from the map
    assert "cDAQ1Mod3/port0/line0" not in phys and "cDAQ1Mod2/port0/line0" not in phys
    assert len(inv) == 21 and len(phys) == 21          # no duplicate lines in the map


def test_every_analog_channel_maps_to_its_own_reading(cfg, fake_daq):
    """Distinct voltage on every AI channel -> each named reading must pick up its OWN channel.

    Guards the scan-order -> name mapping: inserting the primary-pump frequency channel into the
    task must not shift the turbo speed (or any gauge) onto a neighbour's sample.
    """
    from facility_control.gauges import v_to_torr
    b = fake_daq.NiDaqmxBackend(cfg)
    b.open()
    volts = {"cDAQ1Mod1/ai5": 1.0,    # wrg
             "cDAQ1Mod1/ai2": 2.0,    # conv2 (foreline)
             "cDAQ1Mod1/ai1": 3.0,    # conv1 (turbo)
             "cDAQ1Mod1/ai6": 7.0,    # primary pump frequency
             "cDAQ1Mod1/ai4": 0.2}    # turbo speed
    FakeTask.ai_volts.update(volts)
    inp = b.read()
    assert not inp.daq_error
    assert inp.gauge_volts["wrg"] == pytest.approx(1.0)
    assert inp.gauge_volts["conv2"] == pytest.approx(2.0)
    assert inp.gauge_volts["conv1"] == pytest.approx(3.0)
    assert inp.pressures_torr["wrg"] == pytest.approx(v_to_torr("ion_gauge", 1.0))
    # 7 V on ai6 is the pump, NOT the turbo; 0.2 V on ai4 is the turbo, NOT the pump
    assert inp.primary_hz == pytest.approx(7.0 / 10.0 * 210.0)
    assert inp.turbos["turbo1"].speed_pct == pytest.approx(2.0)
    assert inp.turbos["turbo1"].speed_pct != pytest.approx(70.0)   # would mean it read ai6


def test_turbo_speed_is_not_contaminated_by_the_gauge_before_it(cfg, fake_daq):
    """Regression for the phantom turbo speed seen on the facility (2026-09-14).

    The turbo speed shared the gauge task, so the multiplexed ADC returned the residue of the
    channel scanned before it: the Turbo Convectron at atmosphere (~6.88 V) showed up as ~69 %
    turbo speed, and once the pump-frequency channel was inserted it tracked that instead
    (0-10 V = 0-100 %).  Main_V4.4.vi reads the speed in its own task and showed a clean 0.
    """
    from facility_control.gauges import torr_to_v
    b = fake_daq.NiDaqmxBackend(cfg)
    b.open()
    # chamber and turbo section at atmosphere, pump at full frequency, turbo genuinely stopped
    FakeTask.ai_volts.update({
        "cDAQ1Mod1/ai5": torr_to_v("ion_gauge", 760.0),
        "cDAQ1Mod1/ai2": torr_to_v("convectron", 760.0),
        "cDAQ1Mod1/ai1": torr_to_v("convectron", 760.0),   # Turbo Convectron ~6.88 V
        "cDAQ1Mod1/ai6": 10.0,                              # pump frequency at 210 Hz
        "cDAQ1Mod1/ai4": 0.0,                               # turbo speed output: stopped
    })
    inp = b.read()
    assert inp.turbos["turbo1"].speed_pct == pytest.approx(0.0)
    assert inp.primary_hz == pytest.approx(210.0)
    # the two values the facility actually displayed while the turbo was stopped
    assert inp.turbos["turbo1"].speed_pct != pytest.approx(68.8, abs=1.0)   # the convectron's voltage
    assert inp.turbos["turbo1"].speed_pct != pytest.approx(100.0)           # the pump frequency


def test_speed_task_honours_the_per_turbo_sample_rate(fake_daq):
    """A dedicated task can finally use the VI's 4000 Hz x 2000 samples for the HP700 speed."""
    from facility_control.config import CONFIG_DIR, load_config
    vc = load_config(str(CONFIG_DIR / "facility_vc100.yaml"))
    b = fake_daq.NiDaqmxBackend(vc)
    b.open()
    t = FakeTask.registry
    speed_tasks = {k: v for k, v in t.items() if k.endswith("_speed_ai")}
    assert speed_tasks, "the VC100's HiPace turbos have speed channels"
    for name, task in speed_tasks.items():
        assert len(task.channels) == 1, f"{name} must stay a single-channel task"
        rate, mode, samps = task.timing.cfg
        tid = name[: -len("_speed_ai")]
        p = vc.turbo(tid).params
        assert rate == pytest.approx(float(p.get("speed_sample_rate_hz", vc.ai_sample_rate_hz)))
        assert samps == int(p.get("speed_samples", vc.ai_samples_per_channel))
