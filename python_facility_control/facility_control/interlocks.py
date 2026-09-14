"""Manual-mode interlocks.

The VI's 'Manual' frame was planned as "manual control while checking all actions against
interlocks" but never finished (the mode is unreachable in Main_V4.4).  Its dialog texts
document the intended rules; they are implemented here and used by the controller's Manual
mode.  Each check answers: may this single user action be executed now?  If not, why.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import FacilityConfig
from .model import Commands, Inputs


@dataclass
class Verdict:
    allowed: bool
    message: str = ""


class Interlocks:
    def __init__(self, cfg: FacilityConfig):
        self.cfg = cfg
        self.th = cfg.thresholds

    # ------------------------------------------------------------ helpers
    def _speed(self, inp: Inputs, tid: Optional[str] = None) -> float:
        """Speed in % – a contact-interface turbo (no speed signal) counts as 100 % while 'Rotating'."""
        def one(t):
            if t.has_contacts:
                return 100.0 if t.contact("rotating") else 0.0
            return t.speed_pct
        if tid:
            return one(inp.turbos[tid]) if tid in inp.turbos else 0.0
        return max([one(t) for t in inp.turbos.values()] or [0.0])

    def _motor(self, cmds: Commands, tid: Optional[str] = None) -> bool:
        if tid:
            return cmds.turbo_motor.get(tid, False)
        return any(cmds.turbo_motor.values())

    def _primary_running(self, cmds: Commands, inp: Inputs) -> bool:
        """Is the pump actually turning?  Frequency feedback where the facility has it (the small
        chamber since the 2026-09-14 rewiring), otherwise the boolean read-back."""
        pc = self.cfg.primary
        if pc.has_frequency:
            return inp.primary_hz is not None and inp.primary_hz > pc.frequency.running_above_hz
        return bool(inp.primary_read)

    def _valve(self, cmds: Commands, inp: Inputs, kind: str) -> bool:
        """Commanded OR read open, for any valve of that kind (conservative)."""
        for v in self.cfg.valves_of_kind(kind):
            if cmds.valves.get(v.id, False) or inp.valve_reads.get(v.id, False):
                return True
        return False

    def _turbo_stopped(self, cmds: Commands, inp: Inputs, tid: Optional[str] = None) -> Verdict:
        if self._motor(cmds, tid):
            return Verdict(False, "Please Turn Turbo Off First")
        if self._speed(inp, tid) > self.th.turbo_slow_speed_pct:
            return Verdict(False, "Please wait for Turbo to slow down first")
        return Verdict(True)

    # ------------------------------------------------------------ valves
    def valve(self, valve_id: str, want_open: bool, cmds: Commands, inp: Inputs) -> Verdict:
        v = self.cfg.valves[valve_id]
        if v.kind == "turbo":
            if want_open:
                if self._valve(cmds, inp, "vent"):
                    return Verdict(False, "Please Close Vent Valve first")
                return Verdict(True)
            return self._turbo_stopped(cmds, inp, v.turbo)          # closing while turbo runs would trap it
        if v.kind == "bypass":
            if want_open:
                if self._valve(cmds, inp, "vent"):
                    return Verdict(False, "Please Close Vent Valve first")
                return self._turbo_stopped(cmds, inp) if self._valve(cmds, inp, "gate") else Verdict(True)
            return Verdict(True)
        if v.kind == "gate":
            if want_open:
                if self._valve(cmds, inp, "vent"):
                    return Verdict(False, "Please Close Vent Valve first")
                if self._valve(cmds, inp, "bypass"):
                    return Verdict(False, "Please Close Bypass Valve first")
                return Verdict(True)
            return Verdict(True)
        if v.kind == "vent":
            if want_open:
                if cmds.primary or self._primary_running(cmds, inp):
                    return Verdict(False, "Please Stop Primary Pump First")
                if self._valve(cmds, inp, "bypass"):
                    return Verdict(False, "Please Close Bypass Valve first")
                if self._valve(cmds, inp, "turbo"):
                    return Verdict(False, "Please Close Turbo Valve First")
                if self._valve(cmds, inp, "gate"):
                    return Verdict(False, "Please Close Gate Valve First")
                return self._turbo_stopped(cmds, inp)
            return Verdict(True)
        return Verdict(True)

    # ------------------------------------------------------------ pumps
    def primary(self, want_on: bool, cmds: Commands, inp: Inputs) -> Verdict:
        if want_on:
            if self._valve(cmds, inp, "vent"):
                return Verdict(False, "Please Close Vent Valve first")
            return Verdict(True)
        return self._turbo_stopped(cmds, inp)

    def chiller(self, want_on: bool, cmds: Commands, inp: Inputs) -> Verdict:
        if want_on:
            return Verdict(True)
        return self._turbo_stopped(cmds, inp)

    def turbo_motor(self, tid: str, want_on: bool, cmds: Commands, inp: Inputs) -> Verdict:
        if not want_on:
            return Verdict(True)
        t = self.cfg.turbo(tid)
        if self._valve(cmds, inp, "vent"):
            return Verdict(False, "Please Close Vent Valve first")
        # the turbo may only start once the backing pump is really turning – with the two-relay
        # wiring that means after power, the run command and the spin-up, not just the demand
        if not (cmds.primary and self._primary_running(cmds, inp)):
            return Verdict(False, "Please Turn Primary Pump On first")
        if self._valve(cmds, inp, "bypass"):
            return Verdict(False, "Please Close Bypass Valve first")
        if not (cmds.valves.get(t.turbo_valve) and inp.valve_reads.get(t.turbo_valve)):
            return Verdict(False, "Please Open Turbo Valve first")
        if not (cmds.chiller and inp.chiller_read):
            return Verdict(False, "Please Turn Chiller On First")
        g = self.cfg.turbo_gauge(tid) or self.cfg.main_gauge
        if not (inp.pressure(g.id) < self.th.manual_main_pressure_max_torr):
            return Verdict(False, "Main Facility Pressure Too High")
        fg = self.cfg.foreline_gauge
        if fg and not (inp.pressure(fg.id) < self.th.manual_foreline_pressure_max_torr):
            return Verdict(False, "Foreline Pressure Too High")
        return Verdict(True)
