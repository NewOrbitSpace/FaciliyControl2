"""Facility profile loader.  A facility is fully described by one YAML file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .gauges import FORMULAS
from .units import TORR_PER_MBAR

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
class FrequencyConfig:
    """Analog speed/frequency feedback of a VFD-driven pump (small chamber: the primary pump's
    drive reports 0-10 V = 0-`max_hz`).  `running_above_hz` is what counts as 'the pump is turning'
    for the status colour, the interlocks and the operating-hour meter."""
    channel: str
    max_hz: float = 210.0
    min_v: float = 0.0
    max_v: float = 10.0
    running_above_hz: float = 5.0

    def hz(self, volts: float) -> float:
        span = self.max_v - self.min_v
        if span <= 0:
            return 0.0
        return (volts - self.min_v) / span * self.max_hz


@dataclass
class PumpConfig:
    """A pump or chiller contactor.

    Most devices are a single DO line (`cmd`) with a DI run read-back (`read`).  A pump whose
    mains power and run command go to *separate* relays gives `power_cmd` + `run_cmd` instead of
    `cmd`: the controller then sequences power -> gap -> run on start and run -> gap -> power off
    on stop (`timings.primary_power_to_run_gap_s` / `primary_run_to_power_off_gap_s`).  Such a pump
    usually has no boolean read-back at all and reports a `frequency` instead."""
    label: str
    cmd: Optional[str] = None            # single-relay pump: the one DO line
    read: Optional[str] = None           # DI run read-back; None = no boolean feedback
    cross_check: bool = True
    read_inverted: bool = False
    power_cmd: Optional[str] = None      # two-relay pump: mains power relay
    run_cmd: Optional[str] = None        # two-relay pump: start/run relay (only effective once powered)
    frequency: Optional[FrequencyConfig] = None

    @property
    def two_stage(self) -> bool:
        """True when power and run are separate relays."""
        return bool(self.power_cmd and self.run_cmd)

    @property
    def has_read(self) -> bool:
        return bool(self.read)

    @property
    def has_frequency(self) -> bool:
        return self.frequency is not None

    @property
    def power_line(self) -> Optional[str]:
        return self.power_cmd if self.two_stage else self.cmd

    @property
    def run_line(self) -> Optional[str]:
        return self.run_cmd if self.two_stage else self.cmd


@dataclass
class TurboConfig:
    id: str
    label: str
    control_mode: str               # bigred_dsub15 | hipace_dsub25 | hipace_rs485 | shimadzu_contacts
    gate_valve: str
    turbo_valve: str
    gauge: Optional[str] = None
    in_use: bool = True
    params: Dict[str, Any] = field(default_factory=dict)   # the active mode's parameter block
    all_modes: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def has_speed(self) -> bool:
        """True when the controller gets a speed signal (analog or RS485); the Shimadzu contact
        interface only reports Rotating / Accelerating / At Speed / Braking."""
        return self.control_mode != "shimadzu_contacts"

    @property
    def has_error_ack(self) -> bool:
        return bool(self.params.get("error_ack_do")) or self.control_mode == "hipace_rs485"


TURBO_MODES = ("bigred_dsub15", "hipace_dsub25", "hipace_rs485", "shimadzu_contacts")
# per-mode defaults for the error reported when the turbo signals a fault (all overridable per turbo
# with `error_code` / `error_message` in the mode block)
TURBO_ERROR_DEFAULTS = {
    "bigred_dsub15": (5007, "BRT Error. See Device LEDs for more info "),
    "hipace_dsub25": (5006, "HP2300 Error. See Device LEDs for more info "),
    "hipace_rs485": (5006, "HP2300 Error Code: {error_text}"),
    "shimadzu_contacts": (5005, "{label} Alarm"),
}


@dataclass
class Thresholds:
    # 2e-1 mBar (test engineer, 2026-09-08).  Kept in Torr internally, as in the VI.
    turbo_on_threshold_torr: float = 0.150012          # = 2e-1 mBar
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
    compressor_low_time_s: float = 0.0                 # VC100: the air pressure must be low for 5 s before the error
    # Optional separate threshold for the Engage-Turbo pressure check (x wiggle room).  None = the
    # turbo-on threshold is used for both checks, as in both VIs.
    engage_check_threshold_torr: Optional[float] = None
    # 'Turbo slowing Threshold (%)' of the VC100 VI: a turbo above this speed counts as still spinning
    # in Overnight / Venting / Turbo slowing.  None = turbo_spinning_pct (Main_V4.4 uses one value).
    turbo_slowing_threshold_pct: Optional[float] = None
    # Start-up order in Pumping to Rough (test engineer, 2026-09-08).
    # Above this chamber pressure the bypass valve is opened BEFORE the primary pump starts;
    # below it the primary pump starts first and the bypass follows after primary_to_bypass_gap_s.
    # None = always primary pump first, bypass after the gap (the VIs' own order).
    bypass_first_above_torr: Optional[float] = 37.503084   # = 5e1 mBar
    # Pressing Overnight Pump below this pressure skips roughing entirely (no primary, no bypass).
    overnight_skip_below_torr: Optional[float] = 0.150012   # = 2e-1 mBar; None = never skip

    @property
    def slowing_pct(self) -> float:
        return self.turbo_spinning_pct if self.turbo_slowing_threshold_pct is None else self.turbo_slowing_threshold_pct

    def engage_threshold(self, turbo_on_threshold_torr: float) -> float:
        return turbo_on_threshold_torr if self.engage_check_threshold_torr is None else self.engage_check_threshold_torr


# YAML keys of Thresholds that may be given in mBar instead of Torr: `<name>_mbar` replaces `<name>_torr`
_TORR_KEYS = ("turbo_on_threshold", "gauge_error_threshold", "wrg_error_threshold", "turbo_high_pressure_shutoff",
              "manual_main_pressure_max", "manual_foreline_pressure_max", "bypass_first_above",
              "overnight_skip_below", "engage_check_threshold")


@dataclass
class Timings:
    primary_warmup_s: float = 60.0
    primary_warmup_enabled: bool = False
    disengage_wait_s: float = 1.0
    vent_duration_s: float = 600.0
    # gap between starting the primary pump and opening the bypass valve, low-pressure branch
    # of Pumping to Rough (test engineer asked for "roughly 20-30 s")
    primary_to_bypass_gap_s: float = 25.0
    valve_settle_ms: int = 1000
    gate_settle_ms: int = 9000
    bypass_settle_ms: Optional[int] = None      # VC100 waits 2 s after a bypass command; None = valve_settle_ms
    chiller_settle_ms: int = 10000
    primary_settle_ms: int = 500                # wait after the primary pump is switched ON
    primary_settle_off_ms: Optional[int] = None # wait after it is switched OFF; None = same as primary_settle_ms
    # Two-relay primary pump (small chamber): power and run are separate relays.  Starting is
    # power -> wait -> run; stopping is run off -> wait -> power off.  The loop keeps running during
    # these gaps (they are a sequencing delay, not a cross-check settle window).
    primary_power_to_run_gap_s: float = 5.0
    primary_run_to_power_off_gap_s: float = 5.0
    # How long the pump may be commanded to run while its frequency stays below
    # `frequency.running_above_hz` before error 5000 is raised.  None = never raise it.
    primary_spinup_timeout_s: Optional[float] = 30.0
    # True (default, = the VI): the control loop *freezes* for the settle waits above and while a
    # dialog is open – no readings, no error checks meanwhile, so a device is only cross-checked
    # after it had its full settle time.  False: the loop keeps reading and only the decision step
    # waits (readings continue; moving devices are not cross-checked until their settle time is over).
    blocking_waits: bool = True


@dataclass
class AutoBehaviour:
    """Auto-mode rules that differ between the facilities' VIs (YAML block `auto_mode`).  Defaults =
    Main_V4.4 (small chamber); the VC100 profile switches the others on."""
    # Overnight Pump: keep the primary pump + chiller running and each spinning turbo's turbo valve
    # open until that turbo has slowed below the slowing threshold (VC100).  Main_V4.4 switches
    # everything off at once and only keeps the chiller while the turbo is above overnight_chiller_speed_pct.
    overnight_backing_while_spinning: bool = False
    # Turbo valves compared against the turbo gauges (VC100):
    #   Pumping to Rough: a turbo valve may also open when the bypass is open and the chamber is
    #                     already below that turbo's body pressure (not only after the 30 s wait)
    #   Venting / Turbo slowing: a spinning turbo's valve stays open only if it already was, or the
    #                     foreline is below the turbo body pressure (never blow gas into a turbo)
    turbo_valve_gauge_rule: bool = False
    # Standby line = motor commanded AND gate valve closed (VC100 Auto mode)
    standby_when_gate_closed: bool = False
    # 'Shut Off' pressed while a turbo still spins -> Turbo slowing (primary keeps backing) instead of
    # Facility Off straight away (VC100)
    shut_off_spins_down_first: bool = False
    # 'Shutdown Now' in Venting closes the vent valve at once and the vent stays closed while the
    # turbos slow down (VC100).  Main_V4.4 leaves the vent command as it was.
    vent_closes_on_shutdown: bool = False
    # Any button that leaves Venting (Shutdown Now / After Vent, Pump to Rough, Overnight Pump) switches
    # the turbo motors off (VC100).  Main_V4.4 leaves the motor command as it was.
    venting_buttons_stop_turbos: bool = False


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
    # VFD-driven primary pump (a facility whose primary_pump has a `frequency` block)
    primary_run_hz: Optional[float] = None   # frequency it settles at when running; None = frequency.max_hz
    primary_spinup_s: float = 8.0
    primary_spindown_s: float = 20.0


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
    compressor_scale: float = 1.0          # bar = volts * scale + offset (VC100: V/5*10 -> scale 2)
    compressor_offset: float = 0.0
    auto_mode: AutoBehaviour = field(default_factory=AutoBehaviour)
    auto_buttons: Optional[Dict[int, List[Tuple[str, str]]]] = None   # tab page -> [(label, action)], None = Main_V4.4 pages
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


# Auto-mode button actions understood by the state machine (see automode.py)
AUTO_ACTIONS = {"pump_to_rough", "pump_to_high_vac", "overnight_pump", "vent", "shut_off", "shut_off_once_rough",
                "shutdown", "shutdown_after_vent", "vent_and_shutdown"}

_REQUIRED_TURBO_PARAMS = {
    "bigred_dsub15": ("speed_ai", "error_di", "motor_do"),
    "hipace_dsub25": ("speed_ai", "error_di", "motor_do"),
    "hipace_rs485": ("port",),
    "shimadzu_contacts": ("rotating_di", "accelerating_di", "at_speed_di", "braking_di", "alarm_di", "warning_di", "motor_do"),
}


def _check_turbo_params(tid: str, mode: str, params: Dict[str, Any]) -> None:
    missing = [k for k in _REQUIRED_TURBO_PARAMS.get(mode, ()) if k not in params]
    if missing:
        raise ValueError(f"turbo {tid}: {mode} block is missing {missing}")


def _get(d: Dict[str, Any], key: str, default: Any = None) -> Any:
    return d.get(key, default) if d else default


_PUMP_KEYS = {"label", "cmd", "read", "cross_check", "read_inverted", "power_cmd", "run_cmd", "frequency"}
_FREQ_KEYS = {f for f in FrequencyConfig.__dataclass_fields__}


def _pump(block: Dict[str, Any], where: str) -> PumpConfig:
    """Build a PumpConfig from its YAML block, accepting either a single `cmd` relay or the
    `power_cmd` + `run_cmd` pair, with an optional analog `frequency` feedback."""
    unknown = set(block) - _PUMP_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    cmd, power_cmd, run_cmd = block.get("cmd"), block.get("power_cmd"), block.get("run_cmd")
    if cmd and (power_cmd or run_cmd):
        raise ValueError(f"{where}: give either `cmd` (one relay) or `power_cmd` + `run_cmd` (two relays), not both")
    if not cmd and not (power_cmd and run_cmd):
        raise ValueError(f"{where}: needs `cmd`, or both `power_cmd` and `run_cmd`")
    freq = None
    if block.get("frequency"):
        fb = dict(block["frequency"])
        unknown = set(fb) - _FREQ_KEYS
        if unknown:
            raise ValueError(f"{where}.frequency: unknown keys {sorted(unknown)}")
        if "channel" not in fb:
            raise ValueError(f"{where}.frequency: needs a `channel`")
        freq = FrequencyConfig(**fb)
    read = block.get("read")
    if block.get("cross_check", True) and not read and not freq:
        # nothing to cross-check against: a pump with neither a read-back nor a frequency
        raise ValueError(f"{where}: cross_check is on but there is no `read` line and no `frequency` feedback")
    return PumpConfig(label=block["label"], cmd=cmd, read=read,
                      cross_check=bool(block.get("cross_check", True)),
                      read_inverted=bool(block.get("read_inverted", False)),
                      power_cmd=power_cmd, run_cmd=run_cmd, frequency=freq)


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

    primary = _pump(raw["primary_pump"], "primary_pump")
    chiller = _pump(raw["chiller"], "chiller")

    turbos = []
    for t in raw.get("turbos", []):
        mode = t["control_mode"]
        modes = {k: v for k, v in t.items() if k in TURBO_MODES}
        if mode not in modes:
            raise ValueError(f"turbo {t['id']}: control_mode {mode!r} has no parameter block (known modes: {', '.join(TURBO_MODES)})")
        _check_turbo_params(t["id"], mode, modes[mode])
        if t["gate_valve"] not in valves or t["turbo_valve"] not in valves:
            raise ValueError(f"turbo {t['id']}: gate/turbo valve not defined in valves")
        turbos.append(TurboConfig(id=t["id"], label=t["label"], control_mode=mode, gate_valve=t["gate_valve"],
                                  turbo_valve=t["turbo_valve"], gauge=t.get("gauge"), in_use=bool(t.get("in_use", True)),
                                  params=modes[mode], all_modes=modes))

    def dc(cls, block):
        block = dict(block or {})
        if cls is Thresholds:                     # accept *_mbar spellings, stored in Torr internally
            for key in _TORR_KEYS:
                if f"{key}_mbar" in block:
                    if f"{key}_torr" in block:
                        raise ValueError(f"thresholds: give {key}_mbar or {key}_torr, not both")
                    v = block.pop(f"{key}_mbar")
                    block[f"{key}_torr"] = None if v is None else float(v) * TORR_PER_MBAR
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(block) - known
        if unknown:
            raise ValueError(f"{cls.__name__}: unknown keys {sorted(unknown)}")
        return cls(**block)

    comp = raw.get("compressor") or {}
    buttons = None
    if raw.get("auto_buttons"):
        buttons = {}
        for page, items in raw["auto_buttons"].items():
            lst = []
            for it in items or []:
                if isinstance(it, dict):
                    lst.append((str(it["label"]), str(it["action"])))
                else:
                    lst.append((str(it[0]), str(it[1])))
            for _lab, act in lst:
                if act not in AUTO_ACTIONS:
                    raise ValueError(f"auto_buttons page {page}: unknown action {act!r}; known: {sorted(AUTO_ACTIONS)}")
            buttons[int(page)] = lst

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
        compressor_ai=comp.get("channel"), compressor_scale=float(comp.get("scale", 1.0)),
        compressor_offset=float(comp.get("offset", 0.0)),
        auto_mode=dc(AutoBehaviour, raw.get("auto_mode")), auto_buttons=buttons, source_path=source_path,
    )
    if cfg.pressure_unit_default not in ("torr", "mbar"):
        raise ValueError("facility.pressure_unit_default must be torr or mbar")
    cfg.main_gauge  # validate presence
    return cfg
