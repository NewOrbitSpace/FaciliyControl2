"""Pfeiffer HiPace turbos (TC 400) driven over RS-485 - the facility program's side of the link.

A turbo block with `control_mode: hipace_rs485` has no DAQ lines at all: motor, standby, error
acknowledge, speed and error code all travel over the Pfeiffer Vacuum protocol
(facility_control/drivers/pfeiffer_pv.py, the same code the bring-up tool tools/hipace_rs485.py uses).

    turbos:
      - id: turbo2
        control_mode: hipace_rs485
        hipace_rs485:
          port: COM6            # several turbos may share one port (one RS-485 segment) ...
          address: 2            # ... with different [P:797] addresses; or one port per turbo

Up to four HiPace are expected on the VC100 (more work too - only the poll cycle gets longer).

Why a background thread per port.  At 9600 baud one query/reply pair takes ~40-60 ms, so reading
four pumps is ~1 s of bus time - far longer than the controller's 100 ms loop.  Each port therefore
gets a poller thread; the controller's `read()` returns the latest cached values instantly and
`write()` only records the *desired* motor / standby state.  The poller reconciles desired against
what it last wrote successfully, so a write lost to a timeout is simply sent again on the next pass
(rate-limited), and a stop never waits behind a slow poll of another pump for more than one telegram.

Speed modes (2026-10-27): Off ([P:010] off), Full speed, Standby ([P:002] on, speed [P:717]) and a
free set value ([P:707] 20..100 % with the rotation speed setting mode [P:026] on).  [P:026] is
switched off again whenever the pump is stopped, put in standby or returned to full speed, and once
at start-up, so a pump never keeps a reduced speed from an earlier session.

Write order: a STOP is always sent first - before an error acknowledge or a speed change - so a
refused or lost acknowledge can never hold the stop back.

Per pump and pass: [P:309] speed, [P:010] pumping station, [P:303] error code, [P:002] standby;
every `slow_poll_every` passes also [P:315] nominal speed, [P:023], [P:060], [P:300], temperatures,
drive current.

Failure handling (what the controller sees in TurboInputs):
  * ANY good reply counts as "alive" - a poll answer or the echo of a write (2026-10-28: write
    echoes did not count before, so a pump busy taking start commands looked silent).
  * no reply for `comm_warn_s` (default 3 s) -> `comm_warning` + `comm_silent_s`: shown on the
    panel, NOT an error - one slow pass of a busy bus is not a lost pump.
  * no reply for `comm_timeout_s` (default 20 s) -> error "no RS-485 reply ...".  The controller
    then takes just that turbo out in Auto (`isolate_on_comm_loss`) instead of stopping everything.
    The LAST KNOWN speed is kept, deliberately: the plant rules hold a spinning turbo's valve open
    and the primary pump on, and an unknown speed must be treated as "may still be spinning".
  * every pump on a port silent for `port_reset_s` (default 10 s) -> the port is closed and opened
    again and RS-485 control re-taken (a hung USB/RS-485 converter) - no program restart needed.

Poll order (2026-10-28): every pass polls the pump that has been silent longest first, writes are
sent before each poll, and the slow parameter set is spread over the passes (one pump at a time
instead of all four in the same pass), so no pump waits behind a whole bus of slow polls.
  * a control write rejected with _LOGIC (RS-485 does not have control: [P:060] != 2 or the D-Sub
    remote-priority input is active) -> error with that text, until a write succeeds again.
  * [P:303] starting with "Err" -> error; "Wrn" -> warning (shown, does not stop Auto unless
    `warning_is_error: true`).

Start-up and exit: `open()` takes RS-485 control ([P:060] = 2) on every pump unless
`take_control: false`; the controller then commands everything off, exactly as for the D-Sub
modes.  `close()` sends the final (all-off) state synchronously before the ports are released, and
puts [P:060] back to what it found only if `restore_interface_on_exit: true` (default: leave the
pumps on RS-485, stopped - the D-Sub lines are no longer driven by this program).

`read_only=True` (tools/daq_check.py): poll only - no control taken, nothing ever written.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..drivers.pfeiffer_pv import (BAUD, DEFAULT_ADDRESS, HiPace, PVBus, PVError, PVPortError, PVRejected,
                                   PVTimeout, error_kind)
from ..config import check_rs485_turbos as validate_rs485_turbos
from ..model import TurboInputs

FAST = (309, 10, 303, 2)
SLOW = (315, 23, 60, 300, 310, 326, 342, 346, 717)    # 717: standby speed (Engage waits for it)
TEMP_NAMES = {326: "electronics", 342: "bearing", 346: "motor"}

DEFAULTS = {
    "address": DEFAULT_ADDRESS,
    "baud": BAUD,
    "timeout_s": 0.3,
    "take_control": True,
    "restore_interface_on_exit": False,
    "comm_warn_s": 3.0,
    "comm_timeout_s": 20.0,
    "port_reset_s": 10.0,
    "slow_poll_every": 10,
    "standby_speed_pct": None,      # write [P:717] at start-up when set (20..100 %)
    "nominal_hz": None,             # fallback until [P:315] has been read
    "warning_is_error": False,
    "write_retry_s": 1.0,
    "usb_id": None,                 # "VID:PID" or "VID:PID:serial" of the USB/RS-485 converter (hex, as the
    #                                 event log prints it).  With it the converter is found even when
    #                                 Windows gives it another COM number than `port`.
}


def rs485_params(params: Dict) -> Dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in params.items() if v is not None or k not in DEFAULTS})
    return out


class _NoPort:
    """Stands in for a serial port that could not be opened: every query simply times out."""
    is_open = False

    def reset_input_buffer(self):
        pass

    def write(self, data):
        return len(data)

    def read_until(self, terminator=b"\r"):
        time.sleep(0.01)
        return b""

    def close(self):
        pass


@dataclass
class _Unit:
    tid: str
    label: str
    p: Dict
    pump: HiPace
    desired_motor: bool = False
    desired_standby: bool = False
    desired_speed: Optional[float] = None      # reduced-speed set value in %, None = full speed
    applied_motor: Optional[bool] = None       # None = unknown -> always write once
    applied_standby: Optional[bool] = None
    applied_speed: object = "unknown"          # last speed setting written ("unknown" -> write once)
    ack_pending: bool = False
    values: Dict[int, object] = field(default_factory=dict)
    last_ok: float = 0.0
    opened_at: float = 0.0
    polls: int = 0
    control_error: str = ""
    last_write_attempt: float = 0.0
    events: List[str] = field(default_factory=list)


class RS485Turbos:
    """All `hipace_rs485` turbos of a facility: one PVBus + poller thread per serial port."""

    def __init__(self, turbos, read_only: bool = False,
                 serial_factory: Optional[Callable[[str, int, float], object]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 port_lister: Optional[Callable[[], List[Dict]]] = None):
        self.turbos = [t for t in turbos if t.control_mode == "hipace_rs485"]
        validate_rs485_turbos(self.turbos)
        self.read_only = read_only
        self.serial_factory = serial_factory
        self.clock = clock
        self.port_lister = port_lister     # -> [{"device", "description", "vid", "pid", "serial"}] (tests)
        self.lock = threading.RLock()
        self.buses: Dict[str, PVBus] = {}
        self.units: Dict[str, _Unit] = {}
        self._by_port: Dict[str, List[_Unit]] = {}
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self.idle_s = 0.02                 # pause between poll passes
        self.port_errors: Dict[str, str] = {}
        self._port_retry: Dict[str, float] = {}
        # A port that is gone is tried again for as long as the program runs: every second for the
        # first minute (a USB converter that re-enumerates is back within seconds), then every 5 s.
        self.port_retry_s = 5.0
        self.port_retry_fast_s = 1.0
        self.port_retry_fast_for_s = 60.0
        self.repeat_log_s = 600.0          # a fault that lasts is logged again this often, not every time
        self._port_lost_at: Dict[str, float] = {}      # when a port went away (or failed to open)
        self._port_tries: Dict[str, int] = {}
        self._port_identity: Dict[str, tuple] = {}     # (vid, pid, serial) of the converter last opened OK
        self._port_events: List[str] = []              # events of a port, not of one pump
        self._said: Dict[tuple, tuple] = {}
        self._port_list_cache: tuple = (-1e9, [])

    def __bool__(self) -> bool:
        return bool(self.turbos)

    # ------------------------------------------------------------------ lifecycle
    def open(self, start_threads: bool = True) -> None:
        now = self.clock()
        for tc in self.turbos:
            p = rs485_params(tc.params)
            key = str(p["port"]).upper()
            if key not in self.buses:
                self.buses[key] = self._make_bus(p)
            pump = HiPace(self.buses[key], int(p["address"]), tc.label)
            pump.control_enabled = not self.read_only
            if p.get("nominal_hz"):
                pump.nominal_hz = int(p["nominal_hz"])
            u = _Unit(tc.id, tc.label, p, pump, opened_at=now)
            self.units[tc.id] = u
            self._by_port.setdefault(key, []).append(u)
        for units in self._by_port.values():         # spread the slow parameter set over the passes
            for i, u in enumerate(units):
                every = max(1, int(u.p["slow_poll_every"]))
                u.polls = (i * every) // max(1, len(units))
        self._last_reset: Dict[str, float] = {k: now for k in self._by_port}
        for u in self.units.values():
            self._connect(u)
        self._threads_wanted = bool(start_threads)
        if start_threads:
            for key in self._by_port:
                self._start_thread(key)

    def _start_thread(self, key: str) -> None:
        th = threading.Thread(target=self._run, args=(self._by_port[key],), name=f"rs485-{key}", daemon=True)
        th.start()
        self._threads.append(th)

    def _ensure_threads(self) -> None:
        """The pollers are the ONLY thing that talks to the pumps.  One that has ended (it should
        never - see _run) is started again; called from the controller's loop."""
        if not getattr(self, "_threads_wanted", False) or self._stop.is_set():
            return
        alive = {th.name for th in self._threads if th.is_alive()}
        self._threads = [th for th in self._threads if th.is_alive()]
        for key in self._by_port:
            if f"rs485-{key}" not in alive:
                self._pev(f"!! RS-485 poller for {key} had stopped - started again")
                self._start_thread(key)

    # ------------------------------------------------------------------ the serial port
    def _pev(self, text: str) -> None:
        with self.lock:
            self._port_events.append(text)

    def _say(self, key: str, kind: str, text: str) -> None:
        """A port event that repeats for as long as a fault lasts (a reset every 10 s, a failed
        re-open every second): logged the first time, then once every `repeat_log_s` with a count."""
        now = self.clock()
        last, hidden = self._said.get((key, kind), (None, 0))
        if last is None or now - last >= self.repeat_log_s:
            self._pev(text + (f"  [{hidden} more like this since the last line]" if hidden else ""))
            self._said[(key, kind)] = (now, 0)
        else:
            self._said[(key, kind)] = (last, hidden + 1)

    def _list_ports(self) -> List[Dict]:
        """The serial ports the PC has right now, with the USB identity of each."""
        now = self.clock()
        if now - self._port_list_cache[0] < 1.0:
            return self._port_list_cache[1]
        ports: List[Dict] = []
        try:
            if self.port_lister is not None:
                ports = [dict(x) for x in self.port_lister()]
            elif self.serial_factory is None:           # (a test's own serial objects are not real ports)
                from serial.tools import list_ports
                ports = [{"device": x.device, "description": x.description or "", "vid": x.vid, "pid": x.pid,
                          "serial": x.serial_number or ""} for x in list_ports.comports()]
        except Exception:
            ports = []
        self._port_list_cache = (now, ports)
        return ports

    @staticmethod
    def _usb_text(info: Dict) -> str:
        if info.get("vid") is None:
            return ""
        return f"USB {int(info['vid']):04X}:{int(info['pid'] or 0):04X}" + (f":{info['serial']}" if info.get("serial") else "")

    @staticmethod
    def _wanted_identity(p: Dict) -> Optional[tuple]:
        """`usb_id: "0403:6001"` / `"0403:6001:A9XYZ"` -> (vid, pid, serial or None)."""
        text = str(p.get("usb_id") or "").strip()
        if not text:
            return None
        parts = text.replace("USB", "").strip().split(":")
        try:
            return int(parts[0], 16), int(parts[1], 16), (":".join(parts[2:]).strip() or None)
        except (ValueError, IndexError):
            return None

    def _ports_hint(self) -> str:
        ports = self._list_ports()
        if not ports:
            return "" if (self.serial_factory is not None and self.port_lister is None) else \
                "  No serial port is present on this PC - is the USB/RS-485 converter plugged in?"
        shown = "; ".join(f"{x['device']} ({', '.join(t for t in (x.get('description'), self._usb_text(x)) if t)})"
                          for x in ports)
        return f"  Serial ports on this PC now: {shown}"

    def _moved_port(self, key: str, p: Dict) -> Optional[str]:
        """The configured port cannot be opened.  If the SAME USB converter - the one named by
        `usb_id`, or the one that was on this port while it worked - is present under another COM
        number, return that.  Never guesses: exactly one match, and not a port this program
        already uses."""
        want = self._wanted_identity(p) or self._port_identity.get(key)
        if want is None:
            return None
        configured = {str(rs485_params(t.params)["port"]).upper() for t in self.turbos} | {key}
        in_use = {str(b.port).upper() for k, b in self.buses.items() if k != key}
        found = []
        for x in self._list_ports():
            dev = str(x.get("device") or "")
            if x.get("vid") is None or dev.upper() in in_use or (dev.upper() in configured and dev.upper() != key):
                continue
            if (x["vid"], x["pid"]) != (want[0], want[1]):
                continue
            if want[2] is not None and (x.get("serial") or "") != want[2]:
                continue
            found.append(dev)
        found = [d for d in found if d.upper() != key] or found
        if len(found) == 1 and found[0].upper() != key:
            return found[0]
        if len(found) > 1:
            self._say(key, "ambiguous", f"!! {p['port']} cannot be opened and {len(found)} converters of the same "
                                        f"type are present ({', '.join(found)}) - cannot tell which one it is; set "
                                        f"`port:` (or `usb_id:` with its serial number) in the profile")
        return None

    def _remember_port(self, key: str, device: str) -> None:
        for x in self._list_ports():
            if str(x.get("device") or "").upper() == str(device).upper() and x.get("vid") is not None:
                self._port_identity[key] = (x["vid"], x["pid"], x.get("serial") or None)

    def _open_serial(self, p: Dict, port: Optional[str] = None):
        port = port or p["port"]
        if self.serial_factory:
            return self.serial_factory(port, int(p["baud"]), float(p["timeout_s"]))
        return PVBus(port, baud=int(p["baud"]), timeout=float(p["timeout_s"])).ser

    def _open_port(self, key: str, p: Dict):
        """-> (serial object, the port name actually opened).  Tries the configured port, then the
        same converter under another COM number (see _moved_port).  Raises if neither opens."""
        try:
            ser = self._open_serial(p)
            self._remember_port(key, p["port"])
            return ser, str(p["port"])
        except Exception as exc:
            moved = self._moved_port(key, p)
            if moved is None:
                raise
            ser = self._open_serial(p, moved)           # (raises the second error if this fails too)
            self._remember_port(key, moved)
            self._pev(
                f"!! {p['port']} cannot be opened ({exc}), but its USB/RS-485 converter is now {moved} - using "
                f"{moved}.  Change `port:` to {moved} in the profile (or set `usb_id:` so the number no longer matters)")
            return ser, moved

    def _port_error_text(self, p: Dict, exc: BaseException) -> str:
        return f"cannot open {p['port']}: {exc}.{self._ports_hint()}"

    def _make_bus(self, p: Dict) -> PVBus:
        """A port that cannot be opened (converter unplugged, port held by the bring-up tool) must not
        stop the facility program: the pumps on it report 'cannot open COMx' and the poller retries."""
        key = str(p["port"]).upper()
        device = str(p["port"])
        try:
            ser, device = self._open_port(key, p)
            self.port_errors.pop(key, None)
        except Exception as exc:
            ser = _NoPort()
            self.port_errors[key] = self._port_error_text(p, exc)
            self._port_lost_at[key] = self.clock()
        return PVBus(device, baud=int(p["baud"]), timeout=float(p["timeout_s"]), serial_obj=ser)

    def _drop_port(self, key: str, units: List[_Unit]) -> None:
        """The serial port failed under us (USB converter unplugged, reset or re-enumerated - pyserial
        reports 'WriteFile failed', 'ClearCommError failed', 'device disconnected').  Close the dead
        handle - Windows cannot give the converter its COM number back while we hold it - and let
        _retry_port open it again.  Whatever the pumps were last told is sent again after that."""
        bus = self.buses[key]
        with bus.lock:
            fault, bus.port_fault = bus.port_fault, ""
            try:
                bus.ser.close()
            except Exception:
                pass
            bus.ser = _NoPort()
        now = self.clock()
        self.port_errors[key] = f"{bus.port} stopped working ({fault})"
        self._port_lost_at[key] = now
        self._port_tries[key] = 0
        self._port_retry[key] = now - self.port_retry_s          # first new attempt on the next pass
        self._said.pop((key, "retry"), None)
        self._pev(f"!! RS-485 port {bus.port} stopped working ({fault}) - port closed; trying to "
                                 f"open it again every {self.port_retry_fast_s:g} s")
        for u in units:
            u.applied_motor = None                  # unknown from here on: the wanted state is sent again
            u.applied_standby = None
            u.applied_speed = "unknown"

    def _retry_port(self, key: str, p: Dict) -> None:
        """A port that could not be opened or has dropped out is tried again for as long as the
        program runs - never given up on."""
        if key not in self.port_errors:
            return
        now = self.clock()
        lost = self._port_lost_at.setdefault(key, now)
        every = self.port_retry_fast_s if now - lost < self.port_retry_fast_for_s else self.port_retry_s
        if now - self._port_retry.get(key, -1e9) < every:
            return
        self._port_retry[key] = now
        self._port_tries[key] = self._port_tries.get(key, 0) + 1
        try:
            ser, device = self._open_port(key, p)
        except Exception as exc:
            self.port_errors[key] = self._port_error_text(p, exc)
            if now - lost >= 5.0:                   # (a converter that re-enumerates needs a few seconds)
                self._say(key, "retry", f"{p['port']} still cannot be opened after {now - lost:.0f} s "
                                        f"({self._port_tries[key]} tries) - still trying.{self._ports_hint()}")
            return
        bus = self.buses[key]
        with bus.lock:
            bus.ser = ser
            bus.port = device
            bus.port_fault = ""
        self.port_errors.pop(key, None)
        self._pev(f"RS-485 port {device} opened again after {now - lost:.0f} s "
                                 f"({self._port_tries.get(key, 1)} tries) - taking the pumps back")
        self._port_lost_at.pop(key, None)
        self._port_tries.pop(key, None)
        self._said.pop((key, "retry"), None)
        self._last_reset[key] = now
        for u in self._by_port.get(key, []):
            u.applied_motor = None                  # send the wanted state again - the pump may have been
            u.applied_standby = None                # power-cycled, or missed a stop while the port was away
            u.applied_speed = "unknown"
            self._connect(u, quiet=True)

    def _connect(self, u: _Unit, quiet: bool = False) -> None:
        """Best effort: a pump that does not answer yet is not fatal - it shows as 'no reply'."""
        try:
            u.values[315] = u.pump.read(315)
            u.pump.nominal_hz = u.values[315] or u.pump.nominal_hz
            u.last_ok = self.clock()
            if not self.read_only and u.p["take_control"]:
                before = u.pump.read(60)
                u.pump.take_rs485_control()
                if before != 2:
                    u.events.append(f"{u.label}: RS-485 control taken ([P:060] {before} -> 2)")
            if not self.read_only and u.p.get("standby_speed_pct") is not None:
                u.pump.set_standby_speed(float(u.p["standby_speed_pct"]))
        except PVRejected as exc:
            u.control_error = f"RS-485 control refused: {exc}"
        except PVError as exc:
            if not quiet:
                u.events.append(f"{u.label}: no RS-485 reply at start-up ({exc})")

    def close(self) -> List[str]:
        """Stop the pollers, send the final state (all off after safe_state) synchronously, release."""
        self._stop.set()
        for th in self._threads:
            th.join(timeout=5.0)
        self._threads.clear()
        msgs: List[str] = []
        for u in self.units.values():
            if not self.read_only:
                for _ in range(3):                         # a stop is worth a couple of retries
                    u.last_write_attempt = 0.0
                    if self._apply(u):
                        break
                if u.applied_motor is not False and not u.desired_motor:
                    msgs.append(f"!! {u.label}: could not confirm STOP over RS-485 - check the pump")
                if u.p["restore_interface_on_exit"]:
                    msgs.extend(u.pump.safe_exit(stop=False, restore=True))
        for bus in self.buses.values():
            bus.close()
        self.buses.clear()
        return msgs

    # ------------------------------------------------------------------ controller side
    def set_commands(self, motor: Dict[str, bool], standby: Dict[str, bool],
                     speed: Optional[Dict[str, Optional[float]]] = None) -> None:
        speed = speed or {}
        with self.lock:
            for tid, u in self.units.items():
                u.desired_motor = bool(motor.get(tid, False))
                u.desired_standby = bool(standby.get(tid, False))
                sp = speed.get(tid)
                u.desired_speed = None if sp is None else float(sp)

    def error_ack(self, tid: str) -> None:
        u = self.units.get(tid)
        if u is not None and not self.read_only:
            with self.lock:
                u.ack_pending = True

    def take_events(self) -> List[str]:
        self._ensure_threads()
        out = []
        with self.lock:
            out.extend(self._port_events)
            self._port_events.clear()
            for u in self.units.values():
                out.extend(u.events)
                u.events.clear()
        return out

    def inputs(self, tid: str) -> TurboInputs:
        u = self.units[tid]
        now = self.clock()
        with self.lock:
            v = dict(u.values)
            last_ok, control_error = u.last_ok, u.control_error
            desired = u.desired_motor
        ti = TurboInputs()
        nominal = v.get(315) or u.pump.nominal_hz
        hz = v.get(309)
        if hz is not None and nominal:
            ti.speed_pct = 100.0 * float(hz) / float(nominal)
        ti.motor_read = bool(v.get(10, False))
        code = v.get(303)
        kind = error_kind(code) if code is not None else "unknown"
        ti.error = kind == "error"
        ti.warning = kind == "warning"
        ti.error_text = str(code) if code is not None else ""
        if ti.warning and u.p.get("warning_is_error"):
            ti.error = True
        ti.still_spinning = (ti.speed_pct > 5.0) if hz is not None else None
        ti.temps = {name: f"{v[n]} °C" for n, name in TEMP_NAMES.items() if v.get(n) is not None}
        sb = v.get(717) if v.get(717) is not None else u.p.get("standby_speed_pct")
        ti.standby_set_pct = float(sb) if sb is not None else None
        ti.comm_ok = True
        silent = now - (last_ok or u.opened_at)
        timeout_s = float(u.p["comm_timeout_s"])
        warn_s = min(float(u.p.get("comm_warn_s") or timeout_s), timeout_s)
        ti.comm_silent_s = silent
        if warn_s < silent <= timeout_s:
            ti.comm_warning = True
            ti.motor_read = desired            # stale read-back: no motor conflict on a slow bus
        if silent > timeout_s:
            ti.comm_ok = False
            ti.error = True
            ti.error_text = (f"no RS-485 reply from {u.pump.bus.port} address {u.pump.address} "
                             f"for {silent:.0f} s")
            ti.motor_read = desired            # unknown - don't add a motor-conflict on top
            port_err = self.port_errors.get(str(u.p["port"]).upper())
            if port_err:
                ti.error_text = port_err
        elif control_error:
            ti.error = True
            ti.error_text = control_error
        ti.rs485 = {"port": u.pump.bus.port, "address": u.pump.address,
                    "ctrl": v.get(60), "remote_prio": v.get(300), "stats": u.pump.stats.summary()}
        return ti

    # ------------------------------------------------------------------ poller
    def _run(self, units: List[_Unit]) -> None:
        """The poller of one port.  It must never end while the program runs: nothing else reads the
        pumps, re-opens the port or sends a stop.  (Until 2026-11-27 a serial-port error - a USB
        converter that dropped out for a moment - ended it for good; only a program restart
        brought the link back.)"""
        key = str(units[0].p["port"]).upper()
        while not self._stop.is_set():
            try:
                self._pass(key, units)
            except Exception as exc:                # whatever it is: say so, and carry on
                self._say(key, "crash", f"!! RS-485 poller for {key}: unexpected {type(exc).__name__}: {exc} "
                                        f"- carrying on")
            self._stop.wait(self.idle_s)

    def _pass(self, key: str, units: List[_Unit]) -> None:
        """One pass over a port: the pump silent longest is polled first; writes (a stop above all)
        go out before every poll."""
        bus = self.buses[key]
        if bus.port_fault:
            self._drop_port(key, units)
        self._retry_port(key, units[0].p)
        if key in self.port_errors:
            return                                  # no port: nothing to ask; the next pass tries it again
        self._maybe_reset_port(key, units)
        with self.lock:
            order = sorted(units, key=lambda u: u.last_ok or u.opened_at)
        for u in order:
            for w in units:
                self._apply(w)
            if self._stop.is_set():
                return
            if not bus.port_fault:
                self._poll(u)
            if bus.port_fault:                      # the port died under us: no point asking the others
                self._drop_port(key, units)
                return

    def _maybe_reset_port(self, key: str, units: List[_Unit]) -> None:
        """Every pump on this port silent for `port_reset_s`: close the port, open it again and take
        RS-485 control again - what a program restart would have done, without stopping the facility."""
        if self.read_only or key in self.port_errors:
            return                                  # a port that cannot be opened has its own retry
        now = self.clock()
        reset_s = float(units[0].p.get("port_reset_s") or 0.0)
        if reset_s <= 0:
            return
        silent = min(now - (u.last_ok or u.opened_at) for u in units)
        if silent < reset_s:
            if (key, "reset") in self._said:        # they answer again: the next silence is a new story
                del self._said[(key, "reset")]
                self._pev(f"pump(s) on {self.buses[key].port} answering again")
            return
        if now - self._last_reset.get(key, 0.0) < reset_s:
            return
        self._last_reset[key] = now
        p = units[0].p
        bus = self.buses[key]
        try:
            with bus.lock:
                try:
                    bus.ser.close()
                except Exception:
                    pass
                bus.ser = _NoPort()
                ser, device = self._open_port(key, p)
                bus.ser, bus.port, bus.port_fault = ser, device, ""
        except Exception as exc:
            self.port_errors[key] = self._port_error_text(p, exc)
            self._port_lost_at[key] = now
            self._port_tries[key] = 0
            self._say(key, "reset", f"!! {p['port']}: no pump answered for {silent:.0f} s and re-opening the port "
                                    f"failed ({exc}) - trying again every {self.port_retry_fast_s:g} s")
            return
        # one line when it starts, then one every `repeat_log_s` - not eight every ten seconds for as
        # long as the pumps' electronics are switched off
        self._say(key, "reset", f"{bus.port}: no reply from any pump for {silent:.0f} s - port closed and "
                                f"re-opened (repeated every {reset_s:g} s until a pump answers)")
        for u in units:
            u.applied_motor = None                  # unknown after a reset: send the desired state again
            u.applied_standby = None
            u.applied_speed = "unknown"
            self._connect(u, quiet=True)

    def poll_once_ordered(self) -> None:
        """One pass per port exactly as the poller thread does it (tests)."""
        for key, units in self._by_port.items():
            self._pass(key, units)

    def poll_once(self) -> None:
        """One full pass over every port (tests, or a caller without threads)."""
        for units in self._by_port.values():
            for u in units:
                self._apply(u)
                self._poll(u)

    @staticmethod
    def _effective_speed(motor: bool, standby: bool, speed: Optional[float]) -> Optional[float]:
        """A reduced set value only applies to a running pump that is not in standby."""
        return speed if (motor and not standby and speed is not None) else None

    def _apply(self, u: _Unit) -> bool:
        """Bring the pump to the desired state.  Returns True when nothing is left to write."""
        if self.read_only:
            return True
        with self.lock:
            want_motor, want_sb, ack = u.desired_motor, u.desired_standby, u.ack_pending
            want_speed = self._effective_speed(u.desired_motor, u.desired_standby, u.desired_speed)
        todo = ((want_motor != u.applied_motor) or (want_sb != u.applied_standby) or ack
                or want_speed != u.applied_speed)
        if not todo:
            return True
        now = self.clock()
        stopping = want_motor is False and u.applied_motor is not False
        if not stopping and now - u.last_write_attempt < float(u.p["write_retry_s"]):
            return False                            # rate-limit retries (a stop is never delayed)
        u.last_write_attempt = now
        try:
            if stopping:
                u.pump.stop()                       # [P:010] off - always allowed, always first
                u.applied_motor = False
            if ack:
                u.pump.ack_error()
                with self.lock:
                    u.ack_pending = False
                    u.control_error = ""
                u.events.append(f"{u.label}: error acknowledged over RS-485 ([P:009])")
            # standby and speed mode before a start, so the pump heads straight for the right speed
            if want_sb != u.applied_standby:
                if want_sb or u.applied_standby is not None or want_motor:
                    u.pump.set_standby(want_sb)
                u.applied_standby = want_sb
            if want_speed != u.applied_speed:
                u.pump.set_speed(want_speed)        # [P:707] + [P:026] = 1, or [P:026] = 0
                if want_speed is not None:
                    u.events.append(f"{u.label}: speed set to {want_speed:.0f} % over RS-485 ([P:707])")
                elif u.applied_speed not in (None, "unknown"):
                    u.events.append(f"{u.label}: speed setting mode off ([P:026] = 0)")
                u.applied_speed = want_speed
            if want_motor != u.applied_motor:
                if want_motor:
                    if u.p["take_control"]:
                        u.pump.take_rs485_control()
                    u.pump.start()                  # [P:023] on if needed, [P:010] on
                else:
                    u.pump.stop()
                u.applied_motor = want_motor
            with self.lock:
                u.control_error = ""
                u.last_ok = self.clock()            # the pump answered every write: it is alive
            return True
        except PVRejected as exc:
            with self.lock:
                u.control_error = f"RS-485 command refused: {exc}"
                u.last_ok = self.clock()            # a refusal is still an answer
        except PVError:
            pass                                    # timeout etc.: retried next pass, comm watchdog reports it
        return False

    def _poll(self, u: _Unit) -> None:
        params = list(FAST)
        if u.polls % max(1, int(u.p["slow_poll_every"])) == 0:
            params += SLOW
        u.polls += 1
        timeouts = 0
        for n in params:
            try:
                val = u.pump.read(n)
            except PVTimeout:
                timeouts += 1
                if timeouts >= 2:                   # dead link: don't spend 12 x timeout on it
                    break
                continue
            except PVPortError:
                break                               # the port itself: _pass closes it and retries
            except PVError:
                continue
            with self.lock:
                u.values[n] = val
                u.last_ok = self.clock()
            if n == 315 and val:
                u.pump.nominal_hz = int(val)
