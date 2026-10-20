"""Shared enums and data records (mirrors the type-defs and shift registers of Main_V4.4)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional


class Mode(IntEnum):
    """Facility_State.ctl"""
    INITIALIZE = 0
    ADMIN = 1
    MANUAL = 2
    AUTO = 3


class FacilityState(IntEnum):
    """Current_Facility_State.ctl"""
    FACILITY_OFF = 0
    PUMPING_TO_ROUGH = 1
    OVERNIGHT_PUMP = 2
    ENGAGE_TURBO = 3
    PUMPING_TO_HIGH_VAC = 4
    DISENGAGE_TURBO = 5
    VENT_AND_SHUTDOWN = 6
    TURBO_SLOWING = 7
    VENTING = 8

    @property
    def label(self) -> str:
        return {
            0: "Facility Off", 1: "Pumping to Rough", 2: "Overnight Pump", 3: "Engage Turbo",
            4: "Pumping to High Vac", 5: "Disengage Turbo", 6: "Vent and Shutdown",
            7: "Turbo slowing", 8: "Venting",
        }[int(self)]


class TabPage(IntEnum):
    """The VI's 'Tab Control' – which page of Auto buttons is shown."""
    FACILITY_OFF = 0
    PUMPING_TO_ROUGH = 1
    HOLDING_AT_ROUGH = 2
    OVERNIGHT_PUMP = 3
    ENGAGING_TURBO = 4
    PUMPING_TO_HIGH_VAC = 5
    CLOSING_GATE = 6
    VENTING = 7
    TURBO_SLOWING = 8


class TurboStatus(IntEnum):
    OFF = 0
    SPINNING_UP = 1
    SPEED_REACHED = 2
    SPINNING_DOWN = 3
    STANDBY = 4
    ERROR = 5
    WARNING = 6          # Shimadzu contact interface 'Warning' output (VC100)

    @property
    def label(self) -> str:
        return {
            0: "Turbo Off", 1: "Turbo Spinning Up", 2: "Turbo Speed Reached",
            3: "Turbo Spinning Down", 4: "Turbo In Standby", 5: "Turbo Error", 6: "Turbo Warning",
        }[int(self)]


class OnOffError(IntEnum):
    OFF = 0
    ON = 1
    ERROR = 2


# LabVIEW colour constants of the VI (U32 0x00RRGGBB) -> '#RRGGBB'
def lv_color(u32: int) -> str:
    return f"#{u32 & 0xFFFFFF:06X}"


COLOR_OFF = lv_color(12632256)        # grey   #C0C0C0
COLOR_ON = lv_color(6618880)          # green  #64FF00
COLOR_ERROR = lv_color(16711680)      # red    #FF0000
COLOR_SPIN_UP = lv_color(3381759)     # blue   #339AFF
COLOR_SPIN_DOWN = lv_color(10650795)  # purple #A286AB
COLOR_STANDBY = lv_color(16757266)    # yellow #FFB612
COLOR_WARNING = lv_color(16744192)    # orange #FF8C00 (VC100 shows Warning in the standby yellow; kept distinct here)

TURBO_COLORS = {
    TurboStatus.OFF: COLOR_OFF, TurboStatus.SPINNING_UP: COLOR_SPIN_UP,
    TurboStatus.SPEED_REACHED: COLOR_ON, TurboStatus.SPINNING_DOWN: COLOR_SPIN_DOWN,
    TurboStatus.STANDBY: COLOR_STANDBY, TurboStatus.ERROR: COLOR_ERROR, TurboStatus.WARNING: COLOR_WARNING,
}

# LabVIEW error codes used by the VI
ERR_PRIMARY_CONFLICT = 5000
ERR_TURBO_VALVE_CONFLICT = 5001
ERR_BYPASS_CONFLICT = 5002
ERR_VENT_CONFLICT = 5003
ERR_CHILLER_CONFLICT = 5004
ERR_TURBO_MOTOR_CONFLICT = 5005
ERR_TURBO_DEVICE = 5006
ERR_BRT_DEVICE = 5007
ERR_GATE_CONFLICT = 5010
ERR_COMPRESSOR_LOW = 5011          # compressor air low (the VC100 VI reports it as 5010 = gate code; 5011 keeps it apart)
ERR_DAQ = 5020                      # port addition: hardware/DAQ read-write failure
AUTO_ERROR_CODE_MIN, AUTO_ERROR_CODE_MAX = 5000, 5011   # Auto mode: these force Facility Off (the VIs use 5000-5010)


@dataclass
class ErrorCluster:
    """LabVIEW error cluster."""
    status: bool = False
    code: int = 0
    source: str = ""

    def merge(self, other: "ErrorCluster") -> "ErrorCluster":
        """Merge Errors: first error wins."""
        return self if self.status or not other.status else other

    @staticmethod
    def make(code: int, source: str) -> "ErrorCluster":
        return ErrorCluster(True, code, source)

    def clear(self) -> None:
        self.status, self.code, self.source = False, 0, ""


@dataclass
class TurboInputs:
    speed_pct: float = 0.0
    error: bool = False
    motor_read: bool = False       # RS485 only; D-SUB / contact modes echo the command
    still_spinning: Optional[bool] = None
    error_text: str = ""
    temps: Dict[str, str] = field(default_factory=dict)
    # Shimadzu contact interface (VC100 Turbo 1): the six status contacts, already de-inverted
    # (True = the condition is present).  Empty for turbos with a speed signal.
    contacts: Dict[str, bool] = field(default_factory=dict)

    @property
    def has_contacts(self) -> bool:
        return bool(self.contacts)

    def contact(self, name: str) -> bool:
        return bool(self.contacts.get(name, False))


@dataclass
class Inputs:
    """Everything read in frame 0 (one loop iteration)."""
    t: float = 0.0
    valve_reads: Dict[str, bool] = field(default_factory=dict)      # valve id -> open?
    primary_read: bool = False
    chiller_read: bool = False
    pressures_torr: Dict[str, float] = field(default_factory=dict)  # gauge id -> Torr
    gauge_volts: Dict[str, float] = field(default_factory=dict)
    extra_analog: Dict[str, float] = field(default_factory=dict)
    turbos: Dict[str, TurboInputs] = field(default_factory=dict)
    compressor_bar: Optional[float] = None
    primary_hz: Optional[float] = None      # drive frequency of the primary pump (None = no such sensor)
    daq_error: Optional[str] = None

    def pressure(self, gauge_id: str) -> float:
        return self.pressures_torr.get(gauge_id, math.nan)


@dataclass
class Commands:
    """Everything the controller commands (the *_Sys_Cmd shift registers).

    `primary` is the *demand*: what Auto mode, Manual mode or the operator asked for.  On a facility
    whose pump has separate power and run relays the controller expands that demand into the two
    physical lines below, with the configured gap between them; on a single-relay facility both
    simply follow `primary`.  The state machine only ever touches `primary`, so nothing in
    automode.py or a second facility's profile has to know which wiring is fitted."""
    valves: Dict[str, bool] = field(default_factory=dict)  # valve id -> open
    primary: bool = False                                  # demand ("the pump should be running")
    chiller: bool = False
    turbo_motor: Dict[str, bool] = field(default_factory=dict)
    turbo_standby: Dict[str, bool] = field(default_factory=dict)
    turbo_error_ack: Dict[str, bool] = field(default_factory=dict)
    primary_power: bool = False                            # physical relay 1 (mains power)
    primary_run: bool = False                              # physical relay 2 (start/run)

    def copy(self) -> "Commands":
        return Commands(dict(self.valves), self.primary, self.chiller,
                        dict(self.turbo_motor), dict(self.turbo_standby), dict(self.turbo_error_ack),
                        self.primary_power, self.primary_run)

    def set_primary(self, on: bool) -> "Commands":
        """Set the demand *and* both relay lines together.  The controller normally derives the
        relay lines from the demand (see Controller._sequence_primary); this is for code that talks
        to a backend directly and wants the pump simply on or off."""
        self.primary = self.primary_power = self.primary_run = bool(on)
        return self

    @staticmethod
    def all_off(valve_ids: List[str], turbo_ids: List[str]) -> "Commands":
        return Commands({v: False for v in valve_ids}, False, False,
                        {t: False for t in turbo_ids}, {t: False for t in turbo_ids},
                        {t: False for t in turbo_ids}, False, False)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Commands):
            return NotImplemented
        return (self.valves == other.valves and self.primary == other.primary
                and self.chiller == other.chiller and self.turbo_motor == other.turbo_motor
                and self.turbo_standby == other.turbo_standby)


def primary_is_running(pump, inp: "Inputs", cmds: "Commands") -> bool:
    """THE definition of "the primary pump is turning".  One place, used by the controller, the
    Manual-mode interlocks, the hour meter, the panel and the CSV, so they cannot disagree.

    In order of how much the facility actually knows:
      1. a live drive-frequency reading -> above `running_above_hz` is real evidence of rotation;
      2. a boolean run read-back        -> the VI's reed/contact feedback;
      3. no feedback at all             -> the RUN RELAY being closed.  Not the demand: on a
         two-relay pump the demand is set during the power-up gap, while the pump is definitely not
         turning yet, and on a single-relay pump the two are the same thing (the VI's behaviour).

    Rule 1 is gated on a reading actually being PRESENT, not on the profile having a frequency
    block: the operator can switch the reader off (the sensor may not be connected), and the
    backends then leave `primary_hz` at None, so the answer falls through to rule 2 or 3 instead of
    reporting a pump that never runs.

    `pump` is a PumpConfig; taken duck-typed so this module stays free of a config import.
    """
    if inp.primary_hz is not None and getattr(pump, "frequency", None) is not None:
        return inp.primary_hz > pump.frequency.running_above_hz
    if getattr(pump, "has_read", False):
        return bool(inp.primary_read)
    return bool(cmds.primary_run)


@dataclass
class TurboView:
    status: TurboStatus = TurboStatus.OFF
    color: str = COLOR_OFF
    speed_pct: float = 0.0
    error: bool = False
    motor_read: bool = False
    standby: bool = False
    in_use: bool = True
    still_spinning: Optional[bool] = None
    has_speed: bool = True          # False for the contact interface (status words only)
    contacts: Dict[str, bool] = field(default_factory=dict)


@dataclass
class Snapshot:
    """Thread-safe copy of the controller state for the GUI (one per iteration)."""
    t: float = 0.0
    iteration: int = 0
    running_led: bool = False
    mode: Mode = Mode.INITIALIZE
    current: FacilityState = FacilityState.FACILITY_OFF
    target: FacilityState = FacilityState.FACILITY_OFF
    substate: int = 0
    tab: TabPage = TabPage.FACILITY_OFF
    inputs: Inputs = field(default_factory=Inputs)
    commands: Commands = field(default_factory=Commands)
    valve_errors: Dict[str, bool] = field(default_factory=dict)
    primary_status: OnOffError = OnOffError.OFF
    primary_text: str = "Primary Pump is Off"
    chiller_status: OnOffError = OnOffError.OFF
    chiller_text: str = "Chiller is Off"
    turbos: Dict[str, TurboView] = field(default_factory=dict)
    error: ErrorCluster = field(default_factory=ErrorCluster)
    event_log: List[str] = field(default_factory=list)
    elapsed_below_threshold_s: float = 0.0
    turbo_on_threshold_torr: float = 0.25
    turbo_engage_time: Optional[float] = None
    turbo_error_ack_active: Dict[str, bool] = field(default_factory=dict)
    dialog_pending: bool = False
    dialog_message: str = ""
    hardware: str = "simulation"
    skip_primary_warm: bool = False
    compressor_bar: Optional[float] = None
    primary_hz: Optional[float] = None     # drive frequency of the primary pump (None = reader off)
    primary_running: bool = False          # derived: see model.primary_is_running
    primary_frequency_available: bool = False  # the profile has a frequency block (the reader CAN be on)
    primary_frequency_enabled: bool = False    # the reader is currently switched on
    primary_phase: str = ""                # '', 'powering', 'running', 'stopping' (two-relay pump)
    primary_phase_remaining_s: float = 0.0 # time left of the power->run / run->power-off gap
    hold_remaining_s: float = 0.0      # time left of a settle wait (blocking: loop frozen; else DECIDE held)
    hold_reason: str = ""
    loop_blocked: bool = False         # True while the loop is frozen in a settle wait / dialog (VI behaviour)
    primary_run_hours: float = 0.0     # persistent 'Primary pump total operating hours' meter
