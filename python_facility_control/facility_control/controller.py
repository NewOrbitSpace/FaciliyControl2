"""The facility controller – the main loop of Main_V4.4.vi.

One iteration = the VI's flat sequence:
    0 READ  → 1 STATUS/ERRORS → 2 CLEAR → 3 DECIDE (mode) → 4 COMMAND → 5 EVENT LOG
paced to `loop_period_ms` (100 ms).

Settle waits and dialogs – two behaviours, chosen with `timings.blocking_waits` in the YAML:

* **blocking (default – exactly the VI).**  After a device command the loop *freezes* for the
  settle time (1 s valve / 9 s gate / 10 s chiller / 0.5 s primary / 1 s error-ack pulse) and while
  a Yes/No dialog is open.  Nothing is read and no error is evaluated in that time, so a device is
  cross-checked only once it had its full settle time – an actuation can never be flagged as a
  conflict too early.  The GUI stays responsive (snapshots are still published) but shows the last
  readings until the wait is over.
* **non-blocking (`blocking_waits: false`).**  The loop keeps running: sensors are read, errors
  evaluated, plots/CSV updated every iteration; only the DECIDE step is *held* until the settle
  deadline passes or the dialog is answered, and a device that is still moving is not cross-checked
  until its settle time is over (the VI could not check either – it was asleep).

Runs in its own thread; the GUI (or a test) reads `snapshot()` and posts requests with the
`request_*` methods.  Dialogs go through a `DialogProvider` returning a `DialogHandle` that is
resolved later (Qt), or immediately (tests / headless).
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Protocol, Set, Tuple

from .automode import AutoInput, AutoStateMachine
from .config import FacilityConfig
from .hal.base import HardwareBackend
from .interlocks import Interlocks
from .logging_csv import CsvLogger
from .model import (AUTO_ERROR_CODE_MAX, AUTO_ERROR_CODE_MIN, ERR_BRT_DEVICE, ERR_BYPASS_CONFLICT,
                    ERR_CHILLER_CONFLICT, ERR_COMPRESSOR_LOW, ERR_DAQ, ERR_GATE_CONFLICT, ERR_PRIMARY_CONFLICT,
                    ERR_TURBO_DEVICE, ERR_TURBO_MOTOR_CONFLICT, ERR_TURBO_VALVE_CONFLICT, ERR_VENT_CONFLICT,
                    TURBO_COLORS, Commands, ErrorCluster, FacilityState, Inputs, Mode, OnOffError, Snapshot,
                    TabPage, TurboStatus, TurboView)


# ---------------------------------------------------------------------------- dialogs
class DialogHandle:
    """Result of a dialog that may be answered later.  The provider returns it at once; whether the
    controller waits for it (blocking mode) or carries on (non-blocking) is the controller's choice."""

    def __init__(self, result=None, done: bool = False):
        self._event = threading.Event()
        self.result = result
        if done:
            self._event.set()

    def resolve(self, result) -> None:
        self.result = result
        self._event.set()

    @property
    def done(self) -> bool:
        return self._event.is_set()


class DialogProvider(Protocol):
    def ask(self, kind: str, message: str, buttons: List[str]) -> DialogHandle:
        """kind: 'two' (buttons [yes, no] -> result 0/1), 'three' (result 0/1/2, 3 = window closed),
        'info' (one OK button, result ignored).  Must return immediately."""
        ...


DEFER = object()   # sentinel for AutoAnswerDialogs: leave the dialog open until the test resolves it


class AutoAnswerDialogs:
    """Non-interactive provider (tests, headless).  Answers come from a queue (default: first
    button); a queued `DEFER` returns an unresolved handle kept in `.pending`."""

    def __init__(self):
        self.answers: "queue.Queue[object]" = queue.Queue()
        self.log: List[Tuple[str, str]] = []
        self.pending: List[Tuple[str, str, DialogHandle]] = []

    def _next(self, default):
        try:
            return self.answers.get_nowait()
        except queue.Empty:
            return default

    def ask(self, kind: str, message: str, buttons: List[str]) -> DialogHandle:
        self.log.append((kind, message))
        if kind == "info":
            return DialogHandle(None, done=True)
        ans = self._next(0)
        if ans is DEFER:
            h = DialogHandle()
            self.pending.append((kind, message, h))
            return h
        if isinstance(ans, bool):          # convenience for 'two': True = yes
            ans = 0 if ans else 1
        return DialogHandle(int(ans), done=True)

    # backwards-compatible helpers used by older tests
    def two_button(self, message: str, yes: str = "Yes", no: str = "No") -> bool:
        return self.ask("two", message, [yes, no]).result == 0


# ---------------------------------------------------------------------------- requests
@dataclass
class Request:
    kind: str
    arg: object = None
    arg2: object = None


@dataclass
class PendingDialog:
    handle: DialogHandle
    apply: Callable[[object, Commands], None]   # (result, commands) -> mutate commands / controller state
    label: str


IMMEDIATE_KINDS = {"stop", "clear_error", "set_threshold", "set_engage_time", "set_unit"}
VALVE_ERROR_CODES = {"turbo": ERR_TURBO_VALVE_CONFLICT, "bypass": ERR_BYPASS_CONFLICT,
                     "vent": ERR_VENT_CONFLICT, "gate": ERR_GATE_CONFLICT}


class Controller:
    def __init__(self, cfg: FacilityConfig, backend: HardwareBackend, dialogs: Optional[DialogProvider] = None,
                 csv_logger: Optional[CsvLogger] = None, initial_mode: Optional[Mode] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.backend = backend
        self.dialogs: DialogProvider = dialogs or AutoAnswerDialogs()
        self.csv = csv_logger
        self.sleep = sleep
        self.clock = clock
        self.auto = AutoStateMachine(cfg)
        self.interlocks = Interlocks(cfg)
        self._requests: "queue.Queue[Request]" = queue.Queue()
        self._deferred: List[Request] = []          # decisions that arrived while held / dialog open
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.initial_mode = initial_mode
        # ---- shift registers -------------------------------------------------
        self.mode: Mode = initial_mode if initial_mode is not None else Mode.INITIALIZE
        self.current = FacilityState.FACILITY_OFF
        self.target = FacilityState.FACILITY_OFF
        self.substate = 0
        self.tab = TabPage.FACILITY_OFF
        self.cmds = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
        self.error = ErrorCluster()
        self.running_led = False
        self.event_log: List[str] = []
        self.skip_primary_warm = False
        self.turbo_on_threshold_torr = cfg.thresholds.turbo_on_threshold_torr
        self.turbo_engage_time: Optional[float] = None
        self.iteration = 0
        self.inputs = Inputs()
        self.last_error_seen = ""
        self._snapshot = Snapshot()
        # ---- settle waits / dialogs ------------------------------------------
        self.blocking: bool = bool(cfg.timings.blocking_waits)   # True = freeze like the VI
        self._blocked: bool = False                  # currently frozen in a settle wait / dialog
        self._waiting_label: str = ""                # dialog the frozen loop is waiting for
        self._stop_asked = threading.Event()         # lets a frozen loop give up its wait on exit
        self._hold_until: float = 0.0               # settle deadline (controller clock)
        self._hold_reason: str = ""
        # non-blocking mode only:
        self._suppress_until: Dict[str, float] = {}  # device key -> no cmd/read cross-check before this time
        self._ack_until: Dict[str, float] = {}       # turbo id -> error-ack line goes low at this time
        self._ack_active: Dict[str, bool] = {t: False for t in cfg.turbo_ids}
        self._pending: Optional[PendingDialog] = None
        self._elapsed_below = 0.0
        self._valve_errors: Dict[str, bool] = {}
        self._turbo_views: Dict[str, TurboView] = {}
        self._primary_status = OnOffError.OFF
        self._chiller_status = OnOffError.OFF
        self._last_csv = 0.0
        self.pressure_unit = cfg.pressure_unit_default
        self.finished = threading.Event()

    # ------------------------------------------------------------------ thread control
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="facility-controller", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_asked.set()                       # wakes a loop frozen in a settle wait / dialog
        self._requests.put(Request("stop"))

    def join(self, timeout: float = 30.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ------------------------------------------------------------------ requests (GUI → controller)
    def request(self, kind: str, arg=None, arg2=None) -> None:
        self._requests.put(Request(kind, arg, arg2))

    def request_clear_error(self): self.request("clear_error")
    def request_admin_mode(self): self.request("admin_mode")
    def request_manual_mode(self): self.request("manual_mode")
    def request_change_to_auto(self): self.request("change_to_auto")
    def request_auto_button(self, name: str): self.request("auto_button", name)
    def request_user_cmd(self, target: str): self.request("user_cmd", target)        # 'valve:<id>' | 'primary' | 'chiller' | 'turbo:<id>'
    def request_turbo_error_ack(self, tid: Optional[str] = None): self.request("turbo_error_ack", tid)
    def request_set_threshold(self, torr: float): self.request("set_threshold", torr)
    def request_set_engage_time(self, epoch: Optional[float]): self.request("set_engage_time", epoch)
    def request_set_unit(self, unit: str): self.request("set_unit", unit)

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    @property
    def holding(self) -> bool:
        return self.clock() < self._hold_until or self._pending is not None

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        cfg = self.cfg
        try:
            self.backend.open()
            self._log("Program started (%s)" % self.backend.describe())
            period = cfg.loop_period_ms / 1000.0
            next_t = time.monotonic()
            while not self._stop.is_set():
                self.iterate()
                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    self.sleep(delay)
                else:
                    next_t = time.monotonic()
        finally:
            self._shutdown()
            self.finished.set()

    def _shutdown(self) -> None:
        """Exit path of the VI ('Force stop on exit' = True): everything off, tasks cleared."""
        try:
            self.cmds = Commands.all_off(self.cfg.valve_ids, self.cfg.turbo_ids)
            self.backend.safe_state()
            self._log("Program stopped – safe state commanded")
        except Exception as exc:  # pragma: no cover
            self._log(f"Safe-state write failed: {exc}")
        try:
            self.backend.close()
        except Exception:
            pass
        if self.csv:
            self.csv.close()
        self._publish()

    # ------------------------------------------------------------------ one iteration
    def iterate(self) -> None:
        cfg = self.cfg
        now = self.clock()
        self.iteration += 1
        self.running_led = not self.running_led
        # ---- requests: immediate ones now, decisions queued for the DECIDE step
        for r in self._drain_requests():
            if r.kind == "stop":
                self._stop.set()
                return
            elif r.kind == "clear_error":
                self._clear_requested = True
            elif r.kind == "set_threshold":
                self.turbo_on_threshold_torr = float(r.arg)
            elif r.kind == "set_engage_time":
                self.turbo_engage_time = r.arg
            elif r.kind == "set_unit":
                self.pressure_unit = str(r.arg)
            else:
                self._deferred.append(r)
        # ---- frame 0: read (always)
        inp = self.backend.read()
        self.inputs = inp
        # ---- frame 1: status / errors (always; settling devices are not cross-checked)
        self.error = self._evaluate_errors(inp, now)
        # ---- frame 2: clear error
        if getattr(self, "_clear_requested", False):
            self._clear_requested = False
            self.error = ErrorCluster()
            self._log("Error cleared by user")
        self.sleep(cfg.status_wait_ms / 1000.0)
        # ---- error-ack pulse end
        for tid, until in list(self._ack_until.items()):
            if now >= until:
                try:
                    self.backend.turbo_error_ack(tid, False)
                except Exception:
                    pass
                self._ack_active[tid] = False
                del self._ack_until[tid]
        # ---- frame 3: decide (held while a device settles or a dialog is open)
        prev = self.cmds
        new: Optional[Commands] = None
        if self._pending is not None:
            if self._pending.handle.done:
                new = prev.copy()
                new.turbo_error_ack = {t: False for t in cfg.turbo_ids}
                pending, self._pending = self._pending, None
                pending.apply(pending.handle.result, new)
        elif now >= self._hold_until:
            self._hold_reason = ""
            reqs, self._deferred = self._deferred, []
            new = prev.copy()
            new.turbo_error_ack = {t: False for t in cfg.turbo_ids}
            for r in reqs:
                if r.kind == "admin_mode":
                    if self.mode != Mode.ADMIN:
                        self._log("Admin Control Mode Enabled")
                    self.mode = Mode.ADMIN
                elif r.kind == "manual_mode":
                    if self.mode != Mode.MANUAL:
                        self._log("Manual (interlocked) Control Mode Enabled")
                    self.mode = Mode.MANUAL
            reqs = [r for r in reqs if r.kind not in ("admin_mode", "manual_mode")]
            if self.mode == Mode.INITIALIZE:
                new = self._decide_initialize(new)
                self._deferred = reqs                    # nothing else until the mode is chosen
            elif self.mode in (Mode.ADMIN, Mode.MANUAL):
                new = self._decide_manual_or_admin(new, inp, reqs)
            elif self.mode == Mode.AUTO:
                new = self._decide_auto(new, inp, reqs)
        # a dialog answered instantly (headless / tests) is applied in the same iteration
        while self._pending is not None and self._pending.handle.done and new is not None:
            pending, self._pending = self._pending, None
            pending.apply(pending.handle.result, new)
            if self._deferred and self.mode in (Mode.ADMIN, Mode.MANUAL):
                reqs, self._deferred = self._deferred, []
                new = self._decide_manual_or_admin(new, inp, reqs)
        # ---- frame 4: command (only when a decision was made)
        if new is not None:
            self.cmds = new                          # indicators show the new command during the wait
            self._command(prev, new, now)
            self._event_log_changes(prev, new)
        # ---- frame 5: csv + publish (always)
        self._csv_row()
        self._publish()

    # ------------------------------------------------------------------ frame 1
    def _suppressed(self, key: str, now: float) -> bool:
        return now < self._suppress_until.get(key, 0.0)

    def _evaluate_errors(self, inp: Inputs, now: float) -> ErrorCluster:
        cfg = self.cfg
        err = ErrorCluster()
        if inp.daq_error:
            err = err.merge(ErrorCluster.make(ERR_DAQ, f"DAQ read failed: {inp.daq_error}"))
        # valves: XOR(previous command, read) – not while the valve is still moving
        self._valve_errors = {}
        for vid, v in cfg.valves.items():
            conflict = (bool(self.cmds.valves.get(vid, False)) != bool(inp.valve_reads.get(vid, False))
                        and not self._suppressed(f"valve:{vid}", now))
            self._valve_errors[vid] = conflict
            if conflict:
                code = VALVE_ERROR_CODES.get(v.kind, ERR_GATE_CONFLICT)
                err = err.merge(ErrorCluster.make(code, f"Conflict between {v.label} command and read!"))
        # primary: check disabled in Main_V4.4 (cross_check: false)
        prim_conf = (cfg.primary.cross_check and (bool(self.cmds.primary) != bool(inp.primary_read))
                     and not self._suppressed("primary", now))
        self._primary_status = OnOffError.ERROR if prim_conf else (OnOffError.ON if self.cmds.primary else OnOffError.OFF)
        if prim_conf:
            err = err.merge(ErrorCluster.make(ERR_PRIMARY_CONFLICT, "Conflict between Primary pump command and read!"))
        # chiller
        chil_conf = (cfg.chiller.cross_check and (bool(self.cmds.chiller) != bool(inp.chiller_read))
                     and not self._suppressed("chiller", now))
        self._chiller_status = OnOffError.ERROR if chil_conf else (OnOffError.ON if inp.chiller_read else OnOffError.OFF)
        if chil_conf:
            err = err.merge(ErrorCluster.make(ERR_CHILLER_CONFLICT, "Conflict between Chiller command and read!"))
        # turbos
        self._turbo_views = {}
        th = cfg.thresholds
        for tc in cfg.turbos:
            ti = inp.turbos.get(tc.id)
            if ti is None:
                continue
            motor_read = ti.motor_read if tc.control_mode == "hipace_rs485" else bool(self.cmds.turbo_motor.get(tc.id, False))
            standby = bool(self.cmds.turbo_standby.get(tc.id, False))
            turbo_error = bool(ti.error) and bool(self.cmds.chiller)     # "Ignore Turbo Error if chiller off"
            if motor_read:
                status = TurboStatus.STANDBY if standby else (
                    TurboStatus.SPEED_REACHED if ti.speed_pct > th.turbo_speed_reached_pct else TurboStatus.SPINNING_UP)
            else:
                status = TurboStatus.SPINNING_DOWN if ti.speed_pct > th.turbo_slow_speed_pct else TurboStatus.OFF
            if turbo_error:
                status = TurboStatus.ERROR
                if tc.control_mode == "bigred_dsub15":
                    err = err.merge(ErrorCluster.make(ERR_BRT_DEVICE, "BRT Error. See Device LEDs for more info "))
                elif tc.control_mode == "hipace_rs485":
                    err = err.merge(ErrorCluster.make(ERR_TURBO_DEVICE, f"HP2300 Error Code: {ti.error_text}"))
                else:
                    err = err.merge(ErrorCluster.make(ERR_TURBO_DEVICE, "HP2300 Error. See Device LEDs for more info "))
            if (tc.control_mode == "hipace_rs485" and ti.motor_read != bool(self.cmds.turbo_motor.get(tc.id, False))
                    and not self._suppressed(f"turbo:{tc.id}", now)):
                err = err.merge(ErrorCluster.make(ERR_TURBO_MOTOR_CONFLICT, "Conflict between Turbo Motor command and read!"))
            self._turbo_views[tc.id] = TurboView(status, TURBO_COLORS[status], ti.speed_pct, turbo_error, motor_read,
                                                 standby, tc.in_use, ti.still_spinning)
        # compressor pressure (VC100 feature, only if configured)
        if th.compressor_min_bar is not None and inp.compressor_bar is not None and inp.compressor_bar < th.compressor_min_bar:
            err = err.merge(ErrorCluster.make(ERR_COMPRESSOR_LOW, f"Compressor pressure low ({inp.compressor_bar:.1f} bar)"))
        key = f"{err.code}:{err.source}" if err.status else ""
        if key and key != self.last_error_seen:
            self._log(f"ERROR {err.code}: {err.source}")
        self.last_error_seen = key
        return err

    # ------------------------------------------------------------------ waits (the VI's Wait (ms) / dialogs)
    def _freeze(self, seconds: float, reason: str) -> None:
        """Blocking mode – the VI's `Wait (ms)` after a device command: the whole loop stops, nothing
        is read and no error is evaluated until the time is over.  Snapshots keep being published
        (same readings) so the panel can show what it is waiting for."""
        if seconds <= 0:
            return
        self._blocked, self._hold_reason = True, reason
        self._hold_until = self.clock() + seconds
        self._publish()
        remaining = seconds
        while remaining > 1e-9 and not self._stop_asked.is_set():
            step = min(0.1, remaining)
            self.sleep(step)
            remaining -= step
            self._publish()
        self._blocked, self._hold_reason, self._hold_until = False, "", 0.0

    def _wait_dialog(self, handle: DialogHandle, kind: str, label: str) -> None:
        """Blocking mode – the VI's dialog: the loop stands still until the operator answers."""
        self._blocked, self._waiting_label = True, label
        self._publish()
        while not handle.done:
            if self._stop_asked.is_set():            # program closing: count as 'window closed'
                handle.resolve(None if kind == "info" else (3 if kind == "three" else 1))
                break
            self.sleep(0.1)
            self._publish()
        self._blocked, self._waiting_label = False, ""

    def _open_dialog(self, kind: str, message: str, buttons: List[str],
                     apply: Callable[[object, Commands], None], label: str = "") -> None:
        """Ask the operator.  Blocking mode: wait here for the answer (the VI's behaviour); otherwise
        the answer is applied in the iteration in which it arrives."""
        handle = self.dialogs.ask(kind, message, buttons)
        self._pending = PendingDialog(handle, apply, label or message)
        if self.blocking and not handle.done:
            self._wait_dialog(handle, kind, label or message)

    def _info(self, message: str) -> None:
        handle = self.dialogs.ask("info", message, ["OK"])
        if self.blocking and not handle.done:           # the VI's one-button dialog blocks too
            self._wait_dialog(handle, "info", message)
        self._log(message)

    # ------------------------------------------------------------------ frame 3
    def _decide_initialize(self, new: Commands) -> Commands:
        cmds = Commands.all_off(self.cfg.valve_ids, self.cfg.turbo_ids)
        self.current = self.target = FacilityState.FACILITY_OFF
        self.substate = 0
        self.tab = TabPage.FACILITY_OFF

        def apply(result, _cmds):
            want_auto = result == 0
            self.mode = Mode.AUTO if want_auto else Mode.ADMIN
            self._log(("Auto" if want_auto else "Admin") + " Control Mode selected")

        self._open_dialog("two", "Select Control Mode", ["Auto", "Admin"], apply, "Select Control Mode")
        return cmds

    def _decide_manual_or_admin(self, new: Commands, inp: Inputs, reqs: List[Request]) -> Commands:
        cfg = self.cfg
        manual = self.mode == Mode.MANUAL
        # while in Admin the VI displays "Pumping to Rough" as current/target
        self.current = self.target = FacilityState.PUMPING_TO_ROUGH
        self.substate = 0
        for i, r in enumerate(reqs):
            if r.kind == "user_cmd":
                self._handle_user_cmd(str(r.arg), new, inp, manual)
            elif r.kind == "turbo_error_ack":
                for t in ([r.arg] if r.arg else cfg.turbo_ids):
                    new.turbo_error_ack[t] = True
            elif r.kind == "change_to_auto":
                self._change_to_auto(new)
            if self._pending is not None:                # one dialog at a time; the rest waits
                self._deferred = reqs[i + 1:] + self._deferred
                break
        return new

    def _handle_user_cmd(self, target: str, new: Commands, inp: Inputs, manual: bool) -> None:
        cfg = self.cfg
        if target.startswith("valve:"):
            vid = target.split(":", 1)[1]
            v = cfg.valves[vid]
            is_open = new.valves.get(vid, False)
            want = not is_open
            if manual:
                verdict = self.interlocks.valve(vid, want, new, inp)
                if not verdict.allowed:
                    self._info(verdict.message)
                    return

            def apply(result, cmds, vid=vid, want=want):
                if result == 0:
                    cmds.valves[vid] = want
            self._open_dialog("two", f"{'Close' if is_open else 'Open'} {v.label}?", ["Yes", "No"], apply)
        elif target == "primary":
            want = not new.primary
            if manual:
                verdict = self.interlocks.primary(want, new, inp)
                if not verdict.allowed:
                    self._info(verdict.message)
                    return

            def apply(result, cmds, want=want):
                if result == 0:
                    cmds.primary = want
            self._open_dialog("two", f"Turn {'Off' if new.primary else 'on'} Primary Pump?", ["Yes", "No"], apply)
        elif target == "chiller":
            want = not new.chiller
            if manual:
                verdict = self.interlocks.chiller(want, new, inp)
                if not verdict.allowed:
                    self._info(verdict.message)
                    return

            def apply(result, cmds, want=want):
                if result == 0:
                    cmds.chiller = want
            self._open_dialog("two", f"Turn {'Off' if new.chiller else 'on'} Chiller?", ["Yes", "No"], apply)
        elif target.startswith("turbo:"):
            tid = target.split(":", 1)[1]
            motor = new.turbo_motor.get(tid, False)
            standby = new.turbo_standby.get(tid, False)
            if not motor:
                if manual:
                    verdict = self.interlocks.turbo_motor(tid, True, new, inp)
                    if not verdict.allowed:
                        self._info(verdict.message)
                        return

                def apply(result, cmds, tid=tid):
                    if result == 0:
                        cmds.turbo_motor[tid], cmds.turbo_standby[tid] = True, False
                self._open_dialog("two", "Start Turbo Pump?", ["Yes", "No"], apply)
            elif not standby:
                def apply(result, cmds, tid=tid):
                    if result == 0:                       # Yes -> stop
                        cmds.turbo_motor[tid], cmds.turbo_standby[tid] = False, False
                    elif result == 2:                     # Activate Standby
                        cmds.turbo_motor[tid], cmds.turbo_standby[tid] = True, True
                    else:                                 # No / window closed
                        cmds.turbo_motor[tid], cmds.turbo_standby[tid] = True, False
                self._open_dialog("three", "Stop Turbo Pump?", ["Yes", "No", " Activate Standby"], apply)
            else:
                def apply(result, cmds, tid=tid):
                    if result == 0:                       # Yes, Spin Up
                        cmds.turbo_motor[tid], cmds.turbo_standby[tid] = True, False
                    elif result == 2:                     # Yes, Spin Down
                        cmds.turbo_motor[tid], cmds.turbo_standby[tid] = False, False
                    else:
                        cmds.turbo_motor[tid], cmds.turbo_standby[tid] = True, True
                self._open_dialog("three", " Exit Turbo Standby Mode?", ["Yes, Spin Up", "No", " Yes, Spin Down"], apply)

    def recognise_state(self, c: Commands) -> Optional[Tuple[FacilityState, FacilityState, str]]:
        """Admin → Auto state recognition (exactly the VI's decision tree, on commanded states)."""
        cfg = self.cfg
        turbos = cfg.turbos_in_use() or cfg.turbos
        motor = any(c.turbo_motor.get(t.id, False) for t in turbos)
        tv = any(c.valves.get(t.turbo_valve, False) for t in turbos)
        gate = any(c.valves.get(t.gate_valve, False) for t in turbos)
        bp = any(c.valves.get(v.id, False) for v in cfg.valves_of_kind("bypass"))
        vent = any(c.valves.get(v.id, False) for v in cfg.valves_of_kind("vent"))
        S = FacilityState
        if not c.primary:
            if not motor and not tv and not gate and not vent and not bp:
                return S.FACILITY_OFF, S.FACILITY_OFF, "Facility off"
            return None
        if not gate:
            if not motor:
                if not tv and not gate and not motor:
                    return S.PUMPING_TO_ROUGH, S.PUMPING_TO_HIGH_VAC, "Pumping to Rough"
                return None
            if not bp:
                if tv and not gate and not bp:
                    return S.VENTING, S.VENTING, "Venting"
                return None
            if not tv and not gate and not vent and bp:
                return S.PUMPING_TO_ROUGH, S.PUMPING_TO_HIGH_VAC, "Pumping to Rough"
            return None
        if tv and gate and not vent and not bp:
            return S.PUMPING_TO_HIGH_VAC, S.PUMPING_TO_HIGH_VAC, "Pumping to High Vac"
        return None

    def _enter_auto(self, cur: FacilityState, tgt: FacilityState, name: str, cmds: Commands) -> None:
        self.mode = Mode.AUTO
        self.current, self.target = cur, tgt
        bp = any(cmds.valves.get(v.id, False) for v in self.cfg.valves_of_kind("bypass"))
        self.substate = 2 if (cur == FacilityState.PUMPING_TO_ROUGH and bp) else 0
        self.auto.mem.below_prev = None
        self._log(f"Auto Control Mode – recognised state '{name}', target '{tgt.label}'")

    def _change_to_auto(self, new: Commands) -> None:
        rec = self.recognise_state(new)
        if rec is None:
            self._info("Target State Not Recognised! ")
            return
        cur, tgt, name = rec
        if name == "Pumping to Rough":
            def apply(result, cmds):
                if result == 0:
                    self._enter_auto(cur, FacilityState.PUMPING_TO_HIGH_VAC, name, cmds)
                elif result == 1:
                    self._enter_auto(cur, FacilityState.PUMPING_TO_ROUGH, name, cmds)
            self._open_dialog("three", f'Go to State: \n"{name}"?', ["Yes, Target High Vac", "Yes, Target Low Vac", "No"], apply)
        else:
            def apply(result, cmds):
                if result == 0:
                    self._enter_auto(cur, tgt, name, cmds)
            self._open_dialog("two", f'Go to State: \n"{name}"?', ["Yes", "No"], apply)

    def _decide_auto(self, new: Commands, inp: Inputs, reqs: List[Request]) -> Commands:
        buttons: Set[str] = {str(r.arg) for r in reqs if r.kind == "auto_button"}
        for r in reqs:
            if r.kind == "turbo_error_ack":            # port addition: Reset Turbos works in Auto too
                for t in ([r.arg] if r.arg else self.cfg.turbo_ids):
                    new.turbo_error_ack[t] = True
        error_active = self.error.status or (AUTO_ERROR_CODE_MIN <= self.error.code <= AUTO_ERROR_CODE_MAX and self.error.code != 0)
        out = self.auto.step(AutoInput(inp, self.cmds, self.current, self.target, self.substate, buttons, self.clock(),
                                       error_active, self.turbo_on_threshold_torr, self.turbo_engage_time,
                                       self.skip_primary_warm))
        for line in out.log:
            self._log(line)
        self.current, self.target, self.substate, self.tab = out.current, out.target, out.substate, out.tab
        self.skip_primary_warm = out.skip_primary_warm
        self._elapsed_below = out.elapsed_below_threshold_s
        out.commands.turbo_error_ack = new.turbo_error_ack
        return out.commands

    # ------------------------------------------------------------------ frame 4
    def _command(self, prev: Commands, new: Commands, now: float) -> None:
        cfg, tm = self.cfg, self.cfg.timings
        # turbos that are not in use never get commanded (VC100 'Turbo X in use?')
        for tc in cfg.turbos:
            if not tc.in_use:
                new.turbo_motor[tc.id] = False
                new.turbo_standby[tc.id] = False
                new.valves[tc.gate_valve] = False
                new.valves[tc.turbo_valve] = False
        try:
            self.backend.write(new)
        except Exception as exc:
            self.error = ErrorCluster.make(ERR_DAQ, f"DAQ write failed: {exc}")
            self._log(f"ERROR {ERR_DAQ}: DAQ write failed: {exc}")
        # HiPace / BRT error acknowledge: 1 s pulse on the ack line
        pulses: List[Tuple[object, float]] = [
            (tc, int(tc.params.get("error_ack_pulse_ms", 1000)) / 1000.0) for tc in cfg.turbos
            if new.turbo_error_ack.get(tc.id) and tc.control_mode in ("hipace_dsub25", "bigred_dsub15")]
        # the VI's settle waits, sequential: valves (9 s if a gate moved) + chiller + primary
        waits: List[Tuple[float, str]] = []
        changed_valves = [v for v in cfg.valve_ids if prev.valves.get(v) != new.valves.get(v)]
        gate_changed = any(cfg.valves[v].kind == "gate" for v in changed_valves)
        if changed_valves:
            waits.append(((tm.gate_settle_ms if gate_changed else tm.valve_settle_ms) / 1000.0,
                          ", ".join(cfg.valves[v].label for v in changed_valves) + " moving"))
        if prev.chiller != new.chiller:
            waits.append((tm.chiller_settle_ms / 1000.0, f"{cfg.chiller.label} {'starting' if new.chiller else 'stopping'}"))
        if prev.primary != new.primary:
            waits.append((tm.primary_settle_ms / 1000.0, f"{cfg.primary.label} {'starting' if new.primary else 'stopping'}"))

        if self.blocking:
            # ---- exactly the VI: pulse high – Wait – low; then Wait for every device that moved.
            for tc, pulse_s in pulses:
                self._log(f"{tc.label}: error acknowledge pulse")
                self._ack_active[tc.id] = True
                self._ack_write(tc.id, True)
                self._freeze(pulse_s, f"{tc.label} error acknowledge")
                self._ack_write(tc.id, False)
                self._ack_active[tc.id] = False
            for seconds, reason in waits:
                self._freeze(seconds, reason)
            return

        # ---- non-blocking: hold only the DECIDE step, keep reading; no cross-check of a moving
        #      device until the VI would have looked again
        hold_s, reasons = 0.0, []
        for tc, pulse_s in pulses:
            self._log(f"{tc.label}: error acknowledge pulse")
            self._ack_active[tc.id] = True
            self._ack_until[tc.id] = now + pulse_s
            self._ack_write(tc.id, True)
            hold_s += pulse_s
            reasons.append(f"{tc.label} error acknowledge")
        for seconds, reason in waits:
            hold_s += seconds
            reasons.append(reason)
        if hold_s > 0:
            until = now + hold_s
            self._hold_until = max(self._hold_until, until)
            self._hold_reason = "; ".join(reasons)
            for v in changed_valves:
                self._suppress_until[f"valve:{v}"] = until
            if prev.chiller != new.chiller:
                self._suppress_until["chiller"] = until
            if prev.primary != new.primary:
                self._suppress_until["primary"] = until
            for tc in cfg.turbos:
                if prev.turbo_motor.get(tc.id) != new.turbo_motor.get(tc.id):
                    self._suppress_until[f"turbo:{tc.id}"] = until

    def _ack_write(self, tid: str, state: bool) -> None:
        try:
            self.backend.turbo_error_ack(tid, state)
        except Exception as exc:
            self.error = ErrorCluster.make(ERR_DAQ, f"DAQ write failed: {exc}")
            self._log(f"ERROR {ERR_DAQ}: DAQ write failed: {exc}")

    # ------------------------------------------------------------------ frame 5
    def _event_log_changes(self, prev: Commands, new: Commands) -> None:
        cfg = self.cfg
        ts = time.strftime("%H:%M:%S")
        for vid, v in cfg.valves.items():
            if prev.valves.get(vid) != new.valves.get(vid):
                self._log(f"{'Opened' if new.valves.get(vid) else 'Closed'} {v.label} at {ts}")
        if prev.primary != new.primary:
            self._log(f"{'Started' if new.primary else 'Stopped'} {cfg.primary.label} at {ts}")
        if prev.chiller != new.chiller:
            self._log(f"{'Started' if new.chiller else 'Stopped'} {cfg.chiller.label} at {ts}")
        for tc in cfg.turbos:
            if prev.turbo_motor.get(tc.id) != new.turbo_motor.get(tc.id):
                self._log(f"{'Started' if new.turbo_motor.get(tc.id) else 'Stopped'} {tc.label} at {ts}")
            if prev.turbo_standby.get(tc.id) != new.turbo_standby.get(tc.id):
                self._log(f"{tc.label} standby {'on' if new.turbo_standby.get(tc.id) else 'off'} at {ts}")

    def _log(self, line: str) -> None:
        self.event_log.append(line)
        cap = self.cfg.logging.event_log_max_lines
        if len(self.event_log) > cap:
            del self.event_log[: len(self.event_log) - cap]

    def _csv_row(self) -> None:
        if not self.csv:
            return
        now = self.clock()
        if now - self._last_csv >= self.cfg.logging.csv_period_s:
            self._last_csv = now
            try:
                self.csv.write_row(self._build_snapshot())
            except Exception as exc:  # pragma: no cover
                self._log(f"CSV write failed: {exc}")

    # ------------------------------------------------------------------ snapshot
    def _build_snapshot(self) -> Snapshot:
        ps, cs = self._primary_status, self._chiller_status
        ptxt = {OnOffError.OFF: "is Off", OnOffError.ON: "is On", OnOffError.ERROR: "Error"}[ps]
        ctxt = {OnOffError.OFF: "Chiller is Off", OnOffError.ON: "Chiller is On", OnOffError.ERROR: "Chiller error"}[cs]
        now = self.clock()
        dialog_label = self._pending.label if self._pending is not None else self._waiting_label
        return Snapshot(
            t=now, iteration=self.iteration, running_led=self.running_led, mode=self.mode,
            current=self.current, target=self.target, substate=self.substate, tab=self.tab,
            inputs=self.inputs, commands=self.cmds.copy(), valve_errors=dict(self._valve_errors),
            primary_status=ps, primary_text=f"{self.cfg.primary.label} {ptxt}", chiller_status=cs, chiller_text=ctxt,
            turbos=dict(self._turbo_views), error=replace(self.error), event_log=list(self.event_log),
            elapsed_below_threshold_s=self._elapsed_below, turbo_on_threshold_torr=self.turbo_on_threshold_torr,
            turbo_engage_time=self.turbo_engage_time, turbo_error_ack_active=dict(self._ack_active),
            dialog_pending=bool(dialog_label), dialog_message=dialog_label,
            hardware=self.backend.describe(), skip_primary_warm=self.skip_primary_warm,
            compressor_bar=self.inputs.compressor_bar,
            hold_remaining_s=max(0.0, self._hold_until - now), hold_reason=self._hold_reason if now < self._hold_until else "",
            loop_blocked=self._blocked,
        )

    def _publish(self) -> None:
        snap = self._build_snapshot()
        with self._lock:
            self._snapshot = snap

    def _drain_requests(self) -> List[Request]:
        out: List[Request] = []
        while True:
            try:
                out.append(self._requests.get_nowait())
            except queue.Empty:
                return out
