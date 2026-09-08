"""Facility profile loader.  A facility is fully described by one YAML file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .gauges import FORMULAS

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_PROFILE = "facility_main_v4.4.yaml"


@dataclass
class GaugeConfig:
    id: str
    label: str
    channel: str
    formula: str
    role: str = "other"            # main_chamber | foreline | turbo | other
    plot_label: str = ""
    turbo: Optional[str] = None
    unit_native: str = "torr"


@dataclass
class ValveConfig:
    id: str
    label: str
    cmd: str
    read: str
    kind: str                       # turbo | bypass | vent | gate | other
    turbo: Optional[str] = None
    fail_closed: bool = True


@dataclass
class PumpConfig:
    label: str
    cmd: str
    read: str
    cross_check: bool = True
    read_inverted: bool = False


@dataclass
class TurboConfig:
    id: str
    label: str
    control_mode: str               # bigred_dsub15 | hipace_dsub25 | hipace_rs485
    gate_valve: str
    turbo_valve: str
    gauge: Optional[str] = None
    in_use: bool = True
    params: Dict[str, Any] = field(default_factory=dict)   # the active mode's parameter block
    all_modes: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class Thresholds:
    turbo_on_threshold_torr: float = 0.25
    turbo_on_wiggle_room_multiplier: float = 5.0
    min_time_below_threshold_s: float = 30.0
    gauge_error_threshold_torr: float = 1e-8
    wrg_error_threshold_torr: float = 1e-9
    turbo_high_pressure_shutoff_torr: float = 5.0
    turbo_speed_reached_pct: float = 95.0
    turbo_slow_speed_pct: float = 5.0
    turbo_spinning_pct: float = 15.0
    turbo_engage_msg_speed_pct: float = 2.0
    overnight_chiller_speed_pct: float = 10.0
    manual_main_pressure_max_torr: float = 0.01
    manual_foreline_pressure_max_torr: float = 1.6
    compressor_min_bar: Optional[float] = None


@dataclass
class Timings:
    primary_warmup_s: float = 60.0
    primary_warmup_enabled: bool = False
    disengage_wait_s: float = 1.0
    vent_duration_s: float = 600.0
    valve_settle_ms: int = 1000
    gate_settle_ms: int = 9000
    chiller_settle_ms: int = 10000
    primary_settle_ms: int = 500
    # True (default, = the VI): the control loop *freezes* for the settle waits above and while a
    # dialog is open – no readings, no error checks meanwhile, so a device is only cross-checked
    # after it had its full settle time.  False: the loop keeps reading and only the decision step
    # waits (readings continue; moving devices are not cross-checked until their settle time is over).
    blocking_waits: bool = True


@dataclass
class LoggingConfig:
    csv_enabled: bool = True
    csv_directory: str = "logs"
    csv_period_s: float = 5.0
    event_log_max_lines: int = 2000


@dataclass
class SimulationConfig:
    time_scale: float = 1.0
    chamber_volume_l: float = 200.0
    foreline_volume_l: float = 5.0
    primary_pump_speed_l_s: float = 12.0
    turbo_pump_speed_l_s: float = 700.0
    turbo_spinup_s: float = 60.0
    turbo_spindown_s: float = 120.0
    valve_actuation_s: float = 0.6
    atmosphere_torr: float = 760.0
    chamber_ultimate_torr: float = 2e-7
    leak_torr_l_s: float = 1e-4
    outgassing_torr_l_s: float = 2e-5
    compressor_bar: float = 6.3


@dataclass
class FacilityConfig:
    id: str
    name: str
    daq_name: str
    loop_period_ms: int
    status_wait_ms: int
    pressure_unit_default: str
    ai_sample_rate_hz: float
    ai_samples_per_channel: int
    ai_min_v: float
    ai_max_v: float
    ai_terminal_config: str            # differential | rse | nrse | default | pseudo_diff (VI: differential)
    gauges: List[GaugeConfig]
    extra_analog: List[Dict[str, str]]
    valves: Dict[str, ValveConfig]
    primary: PumpConfig
    chiller: PumpConfig
    turbos: List[TurboConfig]
    thresholds: Thresholds
    timings: Timings
    logging: LoggingConfig
    simulation: SimulationConfig
    compressor_ai: Optional[str] = None
    source_path: str = ""

    # ---- helpers -----------------------------------------------------------
    @property
    def has_compressor(self) -> bool:
        """A `compressor:` block in the YAML fits the compressed-air sensor (panel box, CSV column,
        sim fault, error 5011).  Main_V4.4 has none."""
        return bool(self.compressor_ai)

    @property
    def valve_ids(self) -> List[str]:
        return list(self.valves.keys())

    @property
    def turbo_ids(self) -> List[str]:
        return [t.id for t in self.turbos]

    def turbo(self, tid: str) -> TurboConfig:
        return next(t for t in self.turbos if t.id == tid)

    def turbos_in_use(self) -> List[TurboConfig]:
        return [t for t in self.turbos if t.in_use]

    def gauge(self, gid: str) -> GaugeConfig:
        return next(g for g in self.gauges if g.id == gid)

    def gauge_by_role(self, role: str) -> Optional[GaugeConfig]:
        return next((g for g in self.gauges if g.role == role), None)

    @property
    def main_gauge(self) -> GaugeConfig:
        g = self.gauge_by_role("main_chamber")
        if g is None:
            raise ValueError("facility config needs a gauge with role main_chamber")
        return g

    @property
    def foreline_gauge(self) -> Optional[GaugeConfig]:
        return self.gauge_by_role("foreline")

    def turbo_gauge(self, tid: str) -> Optional[GaugeConfig]:
        return next((g for g in self.gauges if g.role == "turbo" and g.turbo == tid), None)

    def valves_of_kind(self, kind: str) -> List[ValveConfig]:
        return [v for v in self.valves.values() if v.kind == kind]

    def phys(self, channel: str) -> str:
        """Physical channel name: 'Mod4/port0/line0' -> 'cDAQ1Mod4/port0/line0' (VI: Concatenate Strings)."""
        if channel.startswith("cDAQ") or "/" not in channel:
            return channel
        return f"{self.daq_name}{channel}"


def _get(d: Dict[str, Any], key: str, default: Any = None) -> Any:
    return d.get(key, default) if d else default


def load_config(path: Optional[str] = None) -> FacilityConfig:
    if path is None:
        path = os.environ.get("FACILITY_CONFIG", str(CONFIG_DIR / DEFAULT_PROFILE))
    p = Path(path)
    if not p.exists() and (CONFIG_DIR / path).exists():
        p = CONFIG_DIR / path
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return build_config(raw, str(p))


def build_config(raw: Dict[str, Any], source_path: str = "") -> FacilityConfig:
    fac = raw["facility"]
    ai = raw.get("analog_input", {})

    gauges = [GaugeConfig(id=g["id"], label=g["label"], channel=g["channel"], formula=g["formula"],
                          role=g.get("role", "other"), plot_label=g.get("plot_label", g["label"]),
                          turbo=g.get("turbo"), unit_native=g.get("unit_native", "torr"))
              for g in raw.get("gauges", [])]
    for g in gauges:
        if g.formula not in FORMULAS:
            raise ValueError(f"gauge {g.id}: unknown formula {g.formula!r}; known: {sorted(FORMULAS)}")

    valves = {vid: ValveConfig(id=vid, label=v["label"], cmd=v["cmd"], read=v["read"], kind=v.get("kind", "other"),
                               turbo=v.get("turbo"), fail_closed=bool(v.get("fail_closed", True)))
              for vid, v in raw["valves"].items()}

    pp = raw["primary_pump"]
    ch = raw["chiller"]
    primary = PumpConfig(pp["label"], pp["cmd"], pp["read"], bool(pp.get("cross_check", True)), bool(pp.get("read_inverted", False)))
    chiller = PumpConfig(ch["label"], ch["cmd"], ch["read"], bool(ch.get("cross_check", True)), bool(ch.get("read_inverted", False)))

    turbos = []
    for t in raw.get("turbos", []):
        mode = t["control_mode"]
        modes = {k: v for k, v in t.items() if k in ("bigred_dsub15", "hipace_dsub25", "hipace_rs485")}
        if mode not in modes:
            raise ValueError(f"turbo {t['id']}: control_mode {mode!r} has no parameter block")
        if t["gate_valve"] not in valves or t["turbo_valve"] not in valves:
            raise ValueError(f"turbo {t['id']}: gate/turbo valve not defined in valves")
        turbos.append(TurboConfig(id=t["id"], label=t["label"], control_mode=mode, gate_valve=t["gate_valve"],
                                  turbo_valve=t["turbo_valve"], gauge=t.get("gauge"), in_use=bool(t.get("in_use", True)),
                                  params=modes[mode], all_modes=modes))

    def dc(cls, block):
        block = block or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(block) - known
        if unknown:
            raise ValueError(f"{cls.__name__}: unknown keys {sorted(unknown)}")
        return cls(**block)

    cfg = FacilityConfig(
        id=fac["id"], name=fac.get("name", fac["id"]), daq_name=fac.get("daq_name", "cDAQ1"),
        loop_period_ms=int(fac.get("loop_period_ms", 100)), status_wait_ms=int(fac.get("status_wait_ms", 10)),
        pressure_unit_default=fac.get("pressure_unit_default", "torr"),
        ai_sample_rate_hz=float(ai.get("sample_rate_hz", 1000.0)), ai_samples_per_channel=int(ai.get("samples_per_channel", 200)),
        ai_min_v=float(ai.get("min_v", 0.0)), ai_max_v=float(ai.get("max_v", 10.0)),
        ai_terminal_config=str(ai.get("terminal_config", "differential")).lower().replace("-", "_"),
        gauges=gauges, extra_analog=raw.get("extra_analog", []) or [], valves=valves, primary=primary, chiller=chiller,
        turbos=turbos, thresholds=dc(Thresholds, raw.get("thresholds")), timings=dc(Timings, raw.get("timings")),
        logging=dc(LoggingConfig, raw.get("logging")), simulation=dc(SimulationConfig, raw.get("simulation")),
        compressor_ai=(raw.get("compressor") or {}).get("channel"), source_path=source_path,
    )
    if cfg.pressure_unit_default not in ("torr", "mbar"):
        raise ValueError("facility.pressure_unit_default must be torr or mbar")
    cfg.main_gauge  # validate presence
    return cfg
