"""Pfeiffer Vacuum protocol for RS-485 (TC 110 / TC 400 / TM 700 drive electronics).

The protocol core shared by the facility program (hal/hipace_rs485_turbos.py drives the HiPace
turbos with it) and the standalone bring-up tool tools/hipace_rs485.py / hipace_rs485_gui.py:

    telegrams   build / parse / checksum, data types (DT), the parameter table (PARAMS)
    PVBus       one serial port (one RS-485 segment), several addresses, comm statistics
    HiPace      one turbo on a bus: typed read/write, snapshot, guarded control, safe exit
    validate    the bring-up check list
    SimTC400 / SimBus   a simulated TC 400 bus (port names SIM1, SIM2, SIM4)

Protocol and safety notes: see the docstring of tools/hipace_rs485.py.
Moved here unchanged from tools/hipace_rs485.py on 2026-10-09 (SIM4 added).
"""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

BAUD = 9600
DEFAULT_ADDRESS = 1
CR = "\r"

# ---------------------------------------------------------------------------- telegram layer
ACTION_QUERY = 0       # "00": data request, data "=?"
ACTION_WRITE = 10      # "10": control command / reply


def checksum(text: str) -> int:
    """Pfeiffer checksum: sum of the ASCII codes modulo 256."""
    return sum(text.encode("ascii")) % 256


def build_telegram(address: int, action: int, param: int, data: str) -> str:
    if not 0 <= address <= 999:
        raise ValueError(f"address {address} outside 0..999")
    if not 0 <= param <= 999:
        raise ValueError(f"parameter {param} outside 0..999")
    if len(data) > 99:
        raise ValueError("data longer than 99 characters")
    body = f"{address:03d}{action:02d}{param:03d}{len(data):02d}{data}"
    return f"{body}{checksum(body):03d}{CR}"


def query_telegram(address: int, param: int) -> str:
    return build_telegram(address, ACTION_QUERY, param, "=?")


class PVError(RuntimeError):
    """Any failure talking to a pump."""


class PVTimeout(PVError):
    pass


class PVChecksum(PVError):
    pass


class PVFormat(PVError):
    pass


class PVRejected(PVError):
    """The pump answered, but with NO_DEF / _RANGE / _LOGIC."""

    MEANING = {"NO_DEF": "parameter does not exist on this unit",
               "_RANGE": "value out of range",
               "_LOGIC": "logic access violation (does RS-485 have control? [P:060] = 2, [P:300] off?)"}

    def __init__(self, code: str, param: int):
        self.code, self.param = code, param
        super().__init__(f"[P:{param:03d}] rejected: {code} - {self.MEANING.get(code, 'unknown')}")


@dataclass
class Telegram:
    address: int
    action: int
    param: int
    data: str


def parse_telegram(raw: str) -> Telegram:
    text = raw.rstrip("\r\n")
    if len(text) < 13:
        raise PVFormat(f"telegram too short: {text!r}")
    body, cs = text[:-3], text[-3:]
    if not cs.isdigit():
        raise PVFormat(f"checksum field not numeric: {text!r}")
    if checksum(body) != int(cs):
        raise PVChecksum(f"checksum mismatch in {text!r} (noise, termination, baud?)")
    head = body[:10]
    if not head.isdigit():
        raise PVFormat(f"header not numeric: {text!r}")
    address, action, param, length = int(head[0:3]), int(head[3:5]), int(head[5:8]), int(head[8:10])
    data = body[10:]
    if len(data) != length:
        raise PVFormat(f"data length {len(data)} but header says {length}: {text!r}")
    return Telegram(address, action, param, data)


# ---------------------------------------------------------------------------- data types
class DT:
    BOOL_OLD = 0      # 6 chars, 111111 / 000000
    U_INT = 1         # 6 digits
    U_REAL = 2        # 6 digits, value * 100
    STRING = 4        # 6 characters
    BOOL_NEW = 6      # 1 char
    U_SHORT = 7       # 3 digits
    U_EXPO = 10       # 6 digits: 4-digit mantissa (x.xxx) + exponent + 20
    STRING16 = 11     # 16 characters


def decode(dtype: int, data: str):
    d = data
    if dtype == DT.BOOL_OLD:
        if d and set(d) <= {"1"}:
            return True
        if d and set(d) <= {"0"}:
            return False
        raise PVFormat(f"not a boolean: {d!r}")
    if dtype == DT.BOOL_NEW:
        if d in ("0", "1"):
            return d == "1"
        raise PVFormat(f"not a boolean: {d!r}")
    if dtype in (DT.U_INT, DT.U_SHORT):
        if not d.isdigit():
            raise PVFormat(f"not an integer: {d!r}")
        return int(d)
    if dtype == DT.U_REAL:
        if not d.isdigit():
            raise PVFormat(f"not a number: {d!r}")
        return int(d) / 100.0
    if dtype == DT.U_EXPO:
        if len(d) != 6 or not d.isdigit():
            raise PVFormat(f"not an exponential number: {d!r}")
        return int(d[:4]) / 1000.0 * 10.0 ** (int(d[4:]) - 20)
    return d.strip()


def encode(dtype: int, value) -> str:
    if dtype == DT.BOOL_OLD:
        return "111111" if value else "000000"
    if dtype == DT.BOOL_NEW:
        return "1" if value else "0"
    if dtype == DT.U_INT:
        v = int(value)
        if not 0 <= v <= 999999:
            raise ValueError(f"{value} outside 0..999999")
        return f"{v:06d}"
    if dtype == DT.U_SHORT:
        v = int(value)
        if not 0 <= v <= 999:
            raise ValueError(f"{value} outside 0..999")
        return f"{v:03d}"
    if dtype == DT.U_REAL:
        v = int(round(float(value) * 100))
        if not 0 <= v <= 999999:
            raise ValueError(f"{value} outside 0..9999.99")
        return f"{v:06d}"
    if dtype == DT.U_EXPO:
        x = float(value)
        if x <= 0:
            return "000000"
        e = int(math.floor(math.log10(x)))
        m = int(round(x / 10 ** e * 1000))
        if m >= 10000:
            m, e = m // 10, e + 1
        if not -20 <= e <= 79:
            raise ValueError(f"{value} not representable")
        return f"{m:04d}{e + 20:02d}"
    s = str(value)
    width = 16 if dtype == DT.STRING16 else 6
    return s[:width].ljust(width)


# ---------------------------------------------------------------------------- parameter table
@dataclass(frozen=True)
class Param:
    number: int
    name: str
    dtype: int
    unit: str = ""
    access: str = "r"           # r, rw, w
    text: str = ""


_P = [
    Param(2, "Standby", DT.BOOL_OLD, "", "rw", "Standby (reduced speed, [P:717])"),
    Param(9, "ErrorAckn", DT.BOOL_OLD, "", "w", "Error acknowledgement"),
    Param(10, "PumpgStatn", DT.BOOL_OLD, "", "rw", "Pumping station on/off (motor runs if [P:023] on)"),
    Param(23, "MotorPump", DT.BOOL_OLD, "", "rw", "Motor pump on/off"),
    Param(26, "SpdSetMode", DT.U_SHORT, "", "rw", "Rotation speed setting mode: 0 off, 1 on (speed = [P:707])"),
    Param(60, "CtrlViaInt", DT.U_SHORT, "", "rw",
          "Control via interface: 1 remote, 2 RS-485, 4 PV.can, 8 fieldbus, 16 E74, 255 unlock"),
    Param(61, "IntSelLckd", DT.BOOL_OLD, "", "rw", "Interface selection locked"),
    Param(300, "RemotePrio", DT.BOOL_OLD, "", "r", "Remote (D-Sub) priority active"),
    Param(302, "SpdSwPtAtt", DT.BOOL_OLD, "", "r", "Rotation speed switch point attained"),
    Param(303, "Errorcode", DT.STRING, "", "r", "Actual error code"),
    Param(304, "OvTempElec", DT.BOOL_OLD, "", "r", "Excess temperature electronic drive unit"),
    Param(305, "OvTempPump", DT.BOOL_OLD, "", "r", "Excess temperature pump"),
    Param(306, "SetSpdAtt", DT.BOOL_OLD, "", "r", "Set rotation speed attained"),
    Param(307, "PumpAccel", DT.BOOL_OLD, "", "r", "Pump accelerates"),
    Param(308, "SetRotSpd", DT.U_INT, "Hz", "r", "Set rotation speed"),
    Param(309, "ActualSpd", DT.U_INT, "Hz", "r", "Actual rotation speed"),
    Param(310, "DrvCurrent", DT.U_REAL, "A", "r", "Drive current"),
    Param(311, "OpHrsPump", DT.U_INT, "h", "r", "Operating hours pump"),
    Param(312, "Fw version", DT.STRING, "", "r", "Firmware version electronic drive unit"),
    Param(313, "DrvVoltage", DT.U_REAL, "V", "r", "Drive voltage"),
    Param(314, "OpHrsElec", DT.U_INT, "h", "r", "Operating hours electronic drive unit"),
    Param(315, "Nominal Spd", DT.U_INT, "Hz", "r", "Nominal rotation speed"),
    Param(316, "DrvPower", DT.U_INT, "W", "r", "Drive power"),
    Param(319, "PumpCycles", DT.U_INT, "", "r", "Pump cycles"),
    Param(326, "TempElec", DT.U_INT, "°C", "r", "Temperature electronics"),
    Param(330, "TempPmpBot", DT.U_INT, "°C", "r", "Temperature pump bottom part"),
    Param(336, "AccelDecel", DT.U_INT, "rpm/s", "r", "Acceleration / deceleration"),
    Param(342, "TempBearng", DT.U_INT, "°C", "r", "Temperature bearing"),
    Param(346, "TempMotor", DT.U_INT, "°C", "r", "Temperature motor"),
    Param(349, "ElecName", DT.STRING, "", "r", "Name of electronic drive unit"),
    Param(354, "HW Version", DT.STRING, "", "r", "Hardware version electronic drive unit"),
    *[Param(360 + i, f"ErrHist{i + 1}", DT.STRING, "", "r", f"Error code history, position {i + 1}")
      for i in range(10)],
    Param(397, "SetRotSpd", DT.U_INT, "rpm", "r", "Set rotation speed (rpm)"),
    Param(398, "ActualSpd", DT.U_INT, "rpm", "r", "Actual rotation speed (rpm)"),
    Param(399, "NominalSpd", DT.U_INT, "rpm", "r", "Nominal rotation speed (rpm)"),
    Param(700, "RUTimeSVal", DT.U_INT, "min", "rw", "Set value run-up time"),
    Param(701, "SpdSwPt1", DT.U_INT, "%", "rw", "Rotation speed switch point 1"),
    Param(707, "SpdSVal", DT.U_REAL, "%", "rw", "Set value in rotation speed setting mode"),
    Param(717, "StbySVal", DT.U_REAL, "%", "rw", "Set value rotation speed at standby"),
    Param(797, "RS485Adr", DT.U_INT, "", "rw", "RS-485 device address"),
]
PARAMS: Dict[int, Param] = {p.number: p for p in _P}

CTRL_INTERFACES = {1: "remote (D-Sub)", 2: "RS-485", 4: "PV.can", 8: "fieldbus", 16: "E74", 255: "unlocked"}
CTRL_RS485 = 2

# what a snapshot reads: every poll / every `slow_every` polls / once at connect
FAST_PARAMS = (309, 303, 10, 23, 2, 306, 307, 310, 316)
SLOW_PARAMS = (308, 315, 313, 326, 330, 342, 346, 60, 61, 300, 302, 304, 305, 717, 311, 314)
IDENT_PARAMS = (349, 312, 354, 797, 399)


def ctrl_text(value: Optional[int]) -> str:
    if value is None:
        return "?"
    if value in CTRL_INTERFACES:
        return CTRL_INTERFACES[value]
    names = [n for b, n in CTRL_INTERFACES.items() if b != 255 and value & b]
    return " + ".join(names) or str(value)


def error_kind(code: Optional[str]) -> str:
    """'ok', 'warning', 'error' or 'unknown' for a [P:303] string."""
    if code is None:
        return "unknown"
    c = code.strip().lower()
    if c in ("no err", "noerr", "000000", "") or c.startswith("no "):
        return "ok"
    if c.startswith("wrn"):
        return "warning"
    if c.startswith("err") or c.startswith("f"):
        return "error"
    return "unknown"


# ---------------------------------------------------------------------------- comm statistics
@dataclass
class CommStats:
    requests: int = 0
    ok: int = 0
    timeouts: int = 0
    checksum_errors: int = 0
    format_errors: int = 0
    rejected: int = 0
    last_ms: float = 0.0
    max_ms: float = 0.0
    sum_ms: float = 0.0
    last_ok_time: float = 0.0
    consecutive_failures: int = 0

    @property
    def avg_ms(self) -> float:
        return self.sum_ms / self.ok if self.ok else 0.0

    @property
    def failures(self) -> int:
        return self.timeouts + self.checksum_errors + self.format_errors

    def summary(self) -> str:
        return (f"{self.ok}/{self.requests} ok, {self.timeouts} timeout, {self.checksum_errors} checksum, "
                f"{self.format_errors} format, {self.rejected} rejected, "
                f"latency avg {self.avg_ms:.0f} / max {self.max_ms:.0f} ms")


# ---------------------------------------------------------------------------- bus
class PVBus:
    """One serial port / RS-485 segment.  Thread-safe: one telegram at a time."""

    def __init__(self, port: Optional[str] = None, baud: int = BAUD, timeout: float = 0.3,
                 serial_obj=None, log: Optional[Callable[[str, str], None]] = None):
        self.port = port or "?"
        if serial_obj is not None:
            self.ser = serial_obj
        elif port and port.upper().startswith("SIM"):
            self.ser = SimBus.named(port)
        else:
            import serial  # lazily, so --help and the tests work without pyserial
            self.ser = serial.Serial(port=port, baudrate=baud, bytesize=8, parity="N",
                                     stopbits=1, timeout=timeout, write_timeout=1.0)
        self.timeout = timeout
        self.lock = threading.RLock()
        self.stats: Dict[int, CommStats] = {}
        self.log = log            # log(direction, text), direction "TX" / "RX" / "--"
        self.gap_s = 0.005        # idle time between telegrams (bus turnaround)
        self.echo_detected = False

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def stats_for(self, address: int) -> CommStats:
        return self.stats.setdefault(address, CommStats())

    def _log(self, direction: str, text: str) -> None:
        if self.log:
            try:
                self.log(direction, text.rstrip("\r"))
            except Exception:
                pass

    def _readline(self, deadline: float) -> str:
        buf = b""
        while time.monotonic() < deadline:
            chunk = self.ser.read_until(b"\r") if hasattr(self.ser, "read_until") else self.ser.read(1)
            if chunk:
                buf += chunk
                if buf.endswith(b"\r"):
                    break
        return buf.decode("ascii", errors="replace")

    def transact(self, address: int, action: int, param: int, data: str) -> Telegram:
        tx = build_telegram(address, action, param, data)
        st = self.stats_for(address)
        with self.lock:
            st.requests += 1
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
            time.sleep(self.gap_s)
            self._log("TX", tx)
            t0 = time.monotonic()
            self.ser.write(tx.encode("ascii"))
            deadline = t0 + self.timeout
            rx = self._readline(deadline)
            if rx == tx and (action == ACTION_QUERY or self.echo_detected):
                # A query reply never equals the query (action 10 vs 00), so this is the converter
                # echoing our own bytes.  A write reply legitimately equals the write telegram, so
                # for writes we can only tell once a query has shown that this converter echoes.
                if not self.echo_detected:
                    self._log("--", "converter echoes transmitted telegrams - echo will be skipped")
                self.echo_detected = True
                rx = self._readline(time.monotonic() + self.timeout)
            ms = (time.monotonic() - t0) * 1000.0
        try:
            if not rx:
                raise PVTimeout(f"no reply from address {address} on {self.port} "
                                f"(port, D+/D- swapped, address, termination, baud?)")
            self._log("RX", rx)
            if not rx.endswith("\r"):
                raise PVFormat(f"incomplete reply {rx!r}")
            t = parse_telegram(rx)
            if t.address != address:
                raise PVFormat(f"reply from address {t.address}, expected {address} (address clash?)")
            if t.param != param:
                raise PVFormat(f"reply for [P:{t.param:03d}], expected [P:{param:03d}]")
        except PVTimeout:
            st.timeouts += 1
            st.consecutive_failures += 1
            raise
        except PVChecksum:
            st.checksum_errors += 1
            st.consecutive_failures += 1
            raise
        except PVFormat:
            st.format_errors += 1
            st.consecutive_failures += 1
            raise
        st.ok += 1
        st.consecutive_failures = 0
        st.last_ms, st.sum_ms, st.max_ms = ms, st.sum_ms + ms, max(st.max_ms, ms)
        st.last_ok_time = time.time()
        if t.data in PVRejected.MEANING:
            st.rejected += 1
            raise PVRejected(t.data, param)
        return t

    def query(self, address: int, param: int) -> str:
        return self.transact(address, ACTION_QUERY, param, "=?").data

    def write(self, address: int, param: int, data: str) -> str:
        return self.transact(address, ACTION_WRITE, param, data).data

    def scan(self, addresses=range(1, 33), progress: Optional[Callable[[int], None]] = None
             ) -> List[Tuple[int, str]]:
        """Query [P:349] ElecName at each address; return [(address, name)] of those that answer."""
        found = []
        old = self.timeout
        self.timeout = min(old, 0.15)
        try:
            for a in addresses:
                if progress:
                    progress(a)
                try:
                    found.append((a, decode(DT.STRING, self.query(a, 349))))
                except PVRejected as exc:
                    found.append((a, f"(answers, {exc.code})"))
                except PVError:
                    pass
        finally:
            self.timeout = old
            for a in addresses:            # a scan is not a link-quality measurement
                if a not in dict(found):
                    self.stats.pop(a, None)
        return found


# ---------------------------------------------------------------------------- the pump
@dataclass
class Snapshot:
    time: float
    values: Dict[int, object] = field(default_factory=dict)
    failed: Dict[int, str] = field(default_factory=dict)
    comm_ok: bool = True

    def get(self, param: int, default=None):
        return self.values.get(param, default)

    @property
    def speed_hz(self) -> Optional[int]:
        return self.values.get(309)

    def speed_pct(self, nominal_hz: Optional[int]) -> Optional[float]:
        s = self.speed_hz
        if s is None or not nominal_hz:
            return None
        return 100.0 * s / nominal_hz

    def status(self, nominal_hz: Optional[int], slow_pct: float = 5.0) -> str:
        """Same words as the facility program uses for the turbos."""
        if not self.comm_ok:
            return "No comms"
        kind = error_kind(self.values.get(303))
        if kind == "error":
            return "Error"
        on = self.values.get(10)
        pct = self.speed_pct(nominal_hz)
        if on:
            if self.values.get(2):
                return "Standby" if not self.values.get(307) else "Standby (accel)"
            if self.values.get(306):
                return "Normal"
            return "Accel"
        if pct is not None and pct > slow_pct:
            return "Decel"
        if kind == "warning":
            return "Warning"
        return "Off"


class ControlNotEnabled(PVError):
    pass


class HiPace:
    """One TC 400 on a PVBus."""

    def __init__(self, bus: PVBus, address: int = DEFAULT_ADDRESS, name: str = "HiPace"):
        self.bus, self.address, self.name = bus, address, name
        self.control_enabled = False
        self.ident: Dict[int, object] = {}
        self.nominal_hz: Optional[int] = None
        self.started_by_tool = False
        self.original_ctrl: Optional[int] = None    # [P:060] as found, if this tool changed it
        self._polls = 0
        self.last: Optional[Snapshot] = None

    @property
    def stats(self) -> CommStats:
        return self.bus.stats_for(self.address)

    # -- typed access
    def read(self, param: int):
        p = PARAMS.get(param)
        data = self.bus.query(self.address, param)
        return decode(p.dtype, data) if p else data

    def read_raw(self, param: int) -> str:
        return self.bus.query(self.address, param)

    def write(self, param: int, value, *, raw: bool = False, _stop: bool = False):
        if not (self.control_enabled or _stop):
            raise ControlNotEnabled("control is not enabled - nothing was written")
        p = PARAMS.get(param)
        if raw or p is None:
            data = str(value)
        else:
            if "w" not in p.access:
                raise ValueError(f"[P:{param:03d}] {p.name} is read-only")
            data = encode(p.dtype, value)
        echo = self.bus.write(self.address, param, data)
        if echo != data:
            raise PVFormat(f"[P:{param:03d}] write not echoed: sent {data!r}, got {echo!r}")
        return decode(p.dtype, echo) if (p and not raw) else echo

    # -- reading
    def identify(self) -> Dict[int, object]:
        for n in IDENT_PARAMS + (315,):
            try:
                self.ident[n] = self.read(n)
            except PVError as exc:
                self.ident[n] = None
                if isinstance(exc, PVTimeout):
                    raise
        self.nominal_hz = self.ident.get(315) or self.nominal_hz
        return self.ident

    def snapshot(self, slow_every: int = 5, full: bool = False) -> Snapshot:
        snap = Snapshot(time=time.time())
        params = list(FAST_PARAMS)
        if full or self._polls % max(1, slow_every) == 0 or self.last is None:
            params += SLOW_PARAMS
        elif self.last is not None:                   # carry the slow values forward
            for n in SLOW_PARAMS:
                if n in self.last.values:
                    snap.values[n] = self.last.values[n]
        self._polls += 1
        timeouts = 0
        for n in params:
            try:
                snap.values[n] = self.read(n)
            except PVTimeout as exc:
                snap.failed[n] = str(exc)
                timeouts += 1
                if timeouts >= 2:                     # don't spend 10 x timeout on a dead link
                    snap.comm_ok = False
                    break
            except PVError as exc:
                snap.failed[n] = str(exc)
        if 315 in snap.values and snap.values[315]:
            self.nominal_hz = snap.values[315]
        self.last = snap
        return snap

    # -- control (all guarded by control_enabled except stop)
    def take_rs485_control(self) -> None:
        current = self.read(60)
        if current == CTRL_RS485:
            return
        if self.original_ctrl is None:
            self.original_ctrl = current
        self.write(60, CTRL_RS485)

    def restore_control_interface(self) -> None:
        if self.original_ctrl is not None:
            self.write(60, self.original_ctrl, _stop=True)
            self.original_ctrl = None

    def start(self) -> None:
        """Pumping station on (and motor enabled).  The caller decides whether that is safe."""
        if not self.control_enabled:
            raise ControlNotEnabled("control is not enabled - nothing was written")
        if self.read(23) is False:
            self.write(23, True)
        self.write(10, True)
        self.started_by_tool = True

    def stop(self) -> None:
        """Pumping station off.  Allowed even with control disabled - stopping is always safe."""
        self.write(10, False, _stop=True)
        self.started_by_tool = False

    def set_standby(self, on: bool) -> None:
        self.write(2, bool(on))

    def ack_error(self) -> None:
        self.write(9, True)

    def set_speed(self, pct: Optional[float]) -> None:
        """Run at a reduced speed or back at full speed.

        pct None -> rotation speed setting mode off ([P:026] = 0): nominal speed (or the standby
        speed while [P:002] is on).  pct 20..100 -> [P:707] = pct, then [P:026] = 1.  The set value is
        written first so the pump never heads for a stale value."""
        if pct is None:
            self.write(26, 0)
            return
        if not 20.0 <= pct <= 100.0:
            raise ValueError("rotation speed set value must be 20..100 %")
        self.write(707, float(pct))
        self.write(26, 1)

    def set_standby_speed(self, pct: float) -> None:
        if not 20.0 <= pct <= 100.0:
            raise ValueError("standby speed must be 20..100 %")
        self.write(717, pct)

    def set_address(self, new: int) -> None:
        if not 1 <= new <= 255:
            raise ValueError("RS-485 address must be 1..255")
        p = PARAMS[797]
        data = encode(p.dtype, new)
        if not self.control_enabled:
            raise ControlNotEnabled("control is not enabled - nothing was written")
        try:
            echo = self.bus.write(self.address, 797, data)
        except PVTimeout:
            echo = None                                # it may already answer on the new address
        old = self.address
        self.address = new
        if self.read(797) != new:
            self.address = old
            raise PVError(f"address change not confirmed (echo {echo!r})")

    def safe_exit(self, stop: bool = True, restore: bool = True) -> List[str]:
        """Stop what this tool started and put [P:060] back.  Never raises; returns messages."""
        msgs = []
        if stop and self.started_by_tool:
            try:
                self.stop()
                msgs.append(f"{self.name}: pumping station switched off")
            except Exception as exc:
                msgs.append(f"!! {self.name}: could not stop over RS-485 ({exc}) - "
                            f"stop it from the facility / remove its mains")
        if restore and self.original_ctrl is not None:
            was = self.original_ctrl
            try:
                self.restore_control_interface()
                msgs.append(f"{self.name}: control interface restored to {ctrl_text(was)}")
            except Exception as exc:
                msgs.append(f"!! {self.name}: could not restore [P:060] = {was} ({exc})")
        return msgs


# ---------------------------------------------------------------------------- validation
@dataclass
class Check:
    name: str
    result: str          # PASS, WARN, FAIL, INFO, SKIP
    detail: str


def validate(pump: HiPace, pings: int = 50, write_test: bool = False,
             progress: Optional[Callable[[str], None]] = None) -> List[Check]:
    """The bring-up check list.  Read-only unless write_test (needs control enabled)."""
    out: List[Check] = []

    def note(c: Check):
        out.append(c)
        if progress:
            progress(f"{c.result:4s}  {c.name}: {c.detail}")

    # 1 - does anything answer?
    try:
        name = pump.read(349)
        note(Check("Link", "PASS", f"address {pump.address} answers on {pump.bus.port}: {name!r}"))
    except PVTimeout as exc:
        note(Check("Link", "FAIL", str(exc)))
        return out
    except PVError as exc:
        note(Check("Link", "WARN", f"answers, but [P:349] failed: {exc}"))
        name = None
    if name is not None and "TC" not in str(name).upper():
        note(Check("Electronics", "WARN", f"expected a TC 400, unit reports {name!r}"))

    # 2 - identity
    pump.identify()
    note(Check("Identity", "INFO",
               f"{pump.ident.get(349)}  firmware {pump.ident.get(312)}  hardware {pump.ident.get(354)}  "
               f"nominal {pump.ident.get(315)} Hz / {pump.ident.get(399)} rpm"))
    adr = pump.ident.get(797)
    if adr is None:
        note(Check("Address", "WARN", "[P:797] could not be read"))
    else:
        note(Check("Address", "PASS" if adr == pump.address else "FAIL",
                   f"[P:797] = {adr}, configured {pump.address}"))

    # 3 - every monitoring parameter
    snap = pump.snapshot(full=True)
    bad = {n: m for n, m in snap.failed.items()}
    if bad:
        note(Check("Parameters", "WARN", f"{len(snap.values)} read, failed: " +
                   ", ".join(f"[P:{n:03d}] {PARAMS[n].name}" for n in bad)))
    else:
        note(Check("Parameters", "PASS", f"all {len(snap.values)} monitoring parameters read"))

    # 4 - link quality
    before = CommStats(**vars(pump.stats))
    t0 = time.monotonic()
    for i in range(pings):
        try:
            pump.read_raw(309)
        except PVError:
            pass
    dt = time.monotonic() - t0
    st = pump.stats
    n_ok = st.ok - before.ok
    n_fail = st.failures - before.failures
    lat = (st.sum_ms - before.sum_ms) / n_ok if n_ok else 0.0
    res = "PASS" if n_fail == 0 else ("WARN" if n_fail <= max(1, pings // 100) else "FAIL")
    note(Check("Link quality", res,
               f"{n_ok}/{pings} replies ok, {st.timeouts - before.timeouts} timeouts, "
               f"{st.checksum_errors - before.checksum_errors} checksum errors, "
               f"avg {lat:.0f} ms, {pings / dt:.1f} telegrams/s"))

    # 5 - pump health
    code = snap.get(303)
    kind = error_kind(code)
    note(Check("Error code", {"ok": "PASS", "warning": "WARN", "error": "FAIL"}.get(kind, "WARN"),
               f"[P:303] = {code!r}"))
    hist = []
    for n in range(360, 363):
        try:
            hist.append(str(pump.read(n)))
        except PVError:
            pass
    if hist:
        note(Check("Error history", "INFO", "last: " + ", ".join(hist)))
    for n, label in ((304, "electronics"), (305, "pump")):
        if snap.get(n):
            note(Check("Temperature", "FAIL", f"excess temperature {label} ([P:{n}])"))
    temps = ", ".join(f"{PARAMS[n].name} {snap.get(n)} °C" for n in (326, 330, 342, 346) if snap.get(n) is not None)
    if temps:
        note(Check("Temperatures", "INFO", temps))

    # 6 - can RS-485 command it?
    ctrl = snap.get(60)
    if ctrl is None:
        note(Check("Control interface", "WARN", "[P:060] could not be read"))
    elif ctrl == CTRL_RS485 or (isinstance(ctrl, int) and ctrl != 255 and ctrl & CTRL_RS485):
        note(Check("Control interface", "PASS", f"[P:060] = {ctrl} ({ctrl_text(ctrl)})"))
    else:
        note(Check("Control interface", "WARN",
                   f"[P:060] = {ctrl} ({ctrl_text(ctrl)}) - RS-485 can monitor but not command; "
                   f"'Take RS-485 control' writes 2"))
    if snap.get(61):
        note(Check("Interface lock", "WARN", "[P:061] interface selection is locked"))
    prio = snap.get(300)
    if prio is None:
        note(Check("Remote priority", "WARN", "[P:300] could not be read"))
    else:
        note(Check("Remote priority", "FAIL" if prio else "PASS",
                   "D-Sub remote priority input ACTIVE - it overrides RS-485, remove it" if prio
                   else "D-Sub remote priority not active"))

    # 7 - write round trip (harmless: write the standby speed back with its own value)
    if not write_test:
        note(Check("Write round trip", "SKIP", "read-only validation (enable control to include it)"))
    elif not pump.control_enabled:
        note(Check("Write round trip", "SKIP", "control not enabled"))
    else:
        try:
            v = pump.read(717)
            echo = pump.write(717, v)
            note(Check("Write round trip", "PASS" if abs(echo - v) < 0.01 else "FAIL",
                       f"[P:717] StbySVal written with its own value {v:.2f} %, echoed {echo:.2f} %"))
        except PVRejected as exc:
            note(Check("Write round trip", "FAIL", str(exc)))
        except PVError as exc:
            note(Check("Write round trip", "FAIL", str(exc)))

    verdict = ("FAIL" if any(c.result == "FAIL" for c in out)
               else "WARN" if any(c.result == "WARN" for c in out) else "PASS")
    note(Check("Overall", verdict, {"PASS": "link and pump fit for RS-485 control",
                                    "WARN": "works, see warnings",
                                    "FAIL": "not fit for RS-485 control yet"}[verdict]))
    return out


# ---------------------------------------------------------------------------- simulator
class SimTC400:
    """A TC 400 + HiPace as seen over RS-485: parameters, control rules, spin up / down."""

    def __init__(self, address=1, nominal_hz=820, run_up_s=150.0, time_scale=1.0):
        self.nominal_hz = nominal_hz
        self.run_up_s = run_up_s
        self.time_scale = time_scale
        self.p: Dict[int, object] = {
            2: False, 10: False, 23: True, 26: 0, 60: 1, 61: False, 300: False, 302: False, 303: "no Err",
            304: False, 305: False, 306: False, 307: False, 308: nominal_hz, 309: 0, 310: 0.0,
            311: 12873, 312: "010300", 313: 48.0, 314: 15021, 315: nominal_hz, 316: 0, 319: 412,
            326: 31, 330: 27, 336: 0, 342: 29, 346: 30, 349: "TC400", 354: "000200",
            397: nominal_hz * 60, 398: 0, 399: nominal_hz * 60, 700: 8, 701: 80, 707: 75.0,
            717: 66.7, 797: address,
        }
        for i in range(10):
            self.p[360 + i] = ["Err002", "Wrn007", "no Err"][i % 3]
        self.speed = 0.0
        self._t = time.monotonic()

    def update(self):
        now = time.monotonic()
        dt = (now - self._t) * self.time_scale
        self._t = now
        running = self.p[10] and self.p[23] and error_kind(self.p[303]) != "error"
        target = 0.0
        if running:
            if self.p[2]:                                   # standby has priority
                target = self.nominal_hz * self.p[717] / 100.0
            elif self.p[26]:                                # rotation speed setting mode
                target = self.nominal_hz * self.p[707] / 100.0
            else:
                target = float(self.nominal_hz)
        rate = self.nominal_hz / self.run_up_s
        if self.speed < target:
            self.speed = min(target, self.speed + rate * dt)
        else:
            self.speed = max(target, self.speed - rate * 0.5 * dt)     # coasts down slower
        accel = running and self.speed < target - 0.5
        self.p[307] = accel
        self.p[306] = running and not accel and target > 0
        self.p[308] = int(target if running else 0)
        self.p[309] = int(round(self.speed))
        self.p[398] = int(round(self.speed * 60))
        self.p[397] = self.p[308] * 60
        self.p[302] = self.speed >= self.nominal_hz * self.p[701] / 100.0
        self.p[310] = round(2.6 if accel else (0.4 + 0.3 * self.speed / self.nominal_hz) if running else 0.0, 2)
        self.p[316] = int(self.p[310] * self.p[313])
        self.p[330] = 27 + int(8 * self.speed / self.nominal_hz)
        self.p[346] = 30 + int(12 * self.speed / self.nominal_hz)

    def handle(self, t: Telegram) -> str:
        self.update()
        n = t.param
        if n not in PARAMS:
            return "NO_DEF"
        par = PARAMS[n]
        if t.action == ACTION_QUERY:
            if t.data != "=?":
                return "_RANGE"
            if par.access == "w":
                return encode(par.dtype, False)
            return encode(par.dtype, self.p[n])
        if t.action != ACTION_WRITE or "w" not in par.access:
            return "_LOGIC"
        try:
            value = decode(par.dtype, t.data)
        except PVError:
            return "_RANGE"
        if len(t.data) != len(encode(par.dtype, value)):
            return "_RANGE"
        control_params = (2, 9, 10, 23, 26, 707)
        rs485_has_control = self.p[60] != 255 and int(self.p[60]) & CTRL_RS485 and not self.p[300]
        if n in control_params and not rs485_has_control:
            return "_LOGIC"
        if n == 60:
            if self.p[61] and value != 255:
                return "_LOGIC"
            if value not in CTRL_INTERFACES:
                return "_RANGE"
        if n in (707, 717) and not 20.0 <= value <= 100.0:
            return "_RANGE"
        if n == 26 and value not in (0, 1):
            return "_RANGE"
        if n == 797 and not 1 <= value <= 255:
            return "_RANGE"
        if n == 9:
            if value:
                self.p[303] = "no Err"
            return t.data
        self.p[n] = value
        return t.data


class SimBus:
    """A fake serial port carrying one or more simulated TC 400 (pyserial-like surface).

    SIM1  shared bus, pumps at addresses 1 and 2    SIM2  one pump at address 1
    SIM4  shared bus, four pumps at addresses 1..4 (VC100 room for four HiPace on one segment)
    Faults for testing: noise (probability of a corrupted reply), echo (converter echoes TX),
    dead (no replies at all).
    """
    _registry: Dict[str, "SimBus"] = {}

    def __init__(self, pumps: Dict[int, SimTC400], noise: float = 0.0, echo: bool = False,
                 dead: bool = False, delay_s: float = 0.008):
        self.pumps = pumps
        self.noise, self.echo, self.dead, self.delay_s = noise, echo, dead, delay_s
        self._out = b""
        self.is_open = True
        self.received: List[str] = []

    @classmethod
    def named(cls, name: str, time_scale: float = 10.0) -> "SimBus":
        key = name.upper()
        if key not in cls._registry or not cls._registry[key].is_open:
            if key == "SIM1":
                pumps = {1: SimTC400(1, time_scale=time_scale), 2: SimTC400(2, time_scale=time_scale)}
            elif key == "SIM4":
                pumps = {a: SimTC400(a, time_scale=time_scale) for a in (1, 2, 3, 4)}
            else:
                pumps = {1: SimTC400(1, nominal_hz=820, time_scale=time_scale)}
            cls._registry[key] = SimBus(pumps)
        return cls._registry[key]

    def reset_input_buffer(self):
        self._out = b""

    def close(self):
        self.is_open = False

    def write(self, data: bytes):
        text = data.decode("ascii")
        self.received.append(text)
        reply = b""
        if self.echo:
            reply += data
        if not self.dead:
            try:
                t = parse_telegram(text)
                pump = self.pumps.get(t.address)
                if pump is not None:
                    rdata = pump.handle(t)
                    new_adr = pump.p[797]
                    if new_adr != t.address:                    # address changed: move it
                        self.pumps[new_adr] = self.pumps.pop(t.address)
                    r = build_telegram(t.address, ACTION_WRITE, t.param, rdata)
                    if self.noise and random.random() < self.noise:
                        r = r[:-4] + ("0" if r[-4] != "0" else "1") + r[-3:]
                    reply += r.encode("ascii")
            except PVError:
                pass
        self._out += reply
        return len(data)

    def read_until(self, terminator=b"\r"):
        time.sleep(self.delay_s if self._out else 0.02)
        i = self._out.find(terminator)
        if i < 0:
            data, self._out = self._out, b""
        else:
            data, self._out = self._out[:i + 1], self._out[i + 1:]
        return data

    def read(self, n=1):
        data, self._out = self._out[:n], self._out[n:]
        return data
