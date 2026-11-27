"""Manual-mode interlocks.

The VI's 'Manual' frame was planned as "manual control while checking all actions against
interlocks" but never finished (the mode is unreachable in Main_V4.4).  Its dialog texts
document the intended rules; they are implemented here and used by the controller's Manual
mode.  Each check answers: may this single user action be executed now?  If not, why.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import math

from .config import FacilityConfig
from .model import Commands, Inputs, primary_is_running
from .units import torr_to


@dataclass
class Verdict:
    allowed: bool
    message: str = ""


class Interlocks:
    def __init__(self, cfg: FacilityConfig):
        self.cfg = cfg
        self.th = cfg.thresholds
        # the operator can change the turbo-on threshold at run time; the controller keeps this in step
        self.turbo_on_threshold_torr = cfg.thresholds.turbo_on_threshold_torr

    # ------------------------------------------------------------ helpers
    def _speed(self, inp: Inputs, tid: Optional[str] = None) -> float:
        """Speed in % – a contact-interface turbo (no speed signal) counts as 100 % while 'Rotating'.

        What is not known is never "stopped" (2026-11-28): a turbo in use that has no reading at all
        (a failed read), or whose speed is not a usable number (not-a-number, infinite, far below
        zero), counts as 100 %.  Every "wait for the turbo to slow down first" and every exposure
        check goes through here."""
        slow = self.th.turbo_slow_speed_pct

        def one(t):
            if t.has_contacts:
                return 100.0 if t.contact("rotating") else 0.0
            sp = t.speed_pct
            usable = isinstance(sp, (int, float)) and sp == sp and not math.isinf(sp) and sp >= -slow
            return float(sp) if usable else 100.0

        def known(turbo_id: str) -> float:
            if turbo_id in inp.turbos:
                return one(inp.turbos[turbo_id])
            tc = next((t for t in self.cfg.turbos if t.id == turbo_id), None)
            return 100.0 if (tc is not None and tc.in_use) else 0.0

        if tid:
            return known(tid)
        ids = list(dict.fromkeys([t.id for t in self.cfg.turbos] + list(inp.turbos)))
        return max([known(i) for i in ids] or [0.0])

    def _motor(self, cmds: Commands, tid: Optional[str] = None) -> bool:
        if tid:
            return cmds.turbo_motor.get(tid, False)
        return any(cmds.turbo_motor.values())

    def _primary_running(self, cmds: Commands, inp: Inputs) -> bool:
        """Is the pump actually turning?  Shared with the controller – see model.primary_is_running.

        This must never fall back to the boolean read-back alone: a pump with no feedback fitted
        reports `primary_read` False for ever, which would block "Please Turn Primary Pump On first"
        permanently and make the turbo unstartable in Manual mode.
        """
        return primary_is_running(self.cfg.primary, inp, cmds)

    def _valve(self, cmds: Commands, inp: Inputs, kind: str) -> bool:
        """Commanded OR read open, for any valve of that kind (conservative)."""
        for v in self.cfg.valves_of_kind(kind):
            if cmds.valves.get(v.id, False) or inp.valve_reads.get(v.id, False):
                return True
        return False

    def backing_ready(self, valve_id: str, cmds: Commands, inp: Inputs) -> Verdict:
        """May this turbo valve open?  Primary pump running AND foreline gauge below the valve's turbo
        gauge (an unreadable gauge = no).  Always allowed when `auto_mode.turbo_valve_needs_backing`
        is off."""
        cfg = self.cfg
        if not cfg.auto_mode.turbo_valve_needs_backing:
            return Verdict(True)
        label = cfg.valves[valve_id].label
        if not self._primary_running(cmds, inp):
            return Verdict(False, f"{label} held closed – the primary pump is not running yet")
        fg = cfg.foreline_gauge
        if fg is None:
            return Verdict(True)
        bad = self.th.gauge_error_threshold_torr
        pf = inp.pressure(fg.id)
        if math.isnan(pf) or pf <= bad:
            return Verdict(False, f"{label} held closed – {fg.label} reading not valid")
        ok_below = self.th.turbo_valve_foreline_ok_torr
        if ok_below is not None and pf < ok_below:
            return Verdict(True)                         # normal running backing pressure
        seen = set()
        for t in cfg.turbos_on_valve(valve_id):
            g = cfg.turbo_gauge(t.id)
            if g is None or g.id in seen:
                continue
            seen.add(g.id)
            pt = inp.pressure(g.id)
            if math.isnan(pt) or pt <= bad:
                return Verdict(False, f"{label} held closed – {g.label} reading not valid")
            if not pf < pt:
                return Verdict(False, f"{label} held closed – {fg.label} {torr_to('mbar', pf):.1e} mBar is not "
                                      f"below {g.label} {torr_to('mbar', pt):.1e} mBar")
        return Verdict(True)

    # ------------------------------------------------------------ exposure (2026-11-11)
    # A turbo that is turning must never be joined to an un-roughed chamber: not at its exhaust
    # (bypass open + its turbo valve open) and not at its inlet (its gate).  Auto mode enforces this
    # itself (automode.py, global protection 1); these are the same tests for single user actions.
    def exposure_limit_torr(self) -> float:
        if self.th.turbo_exposure_max_torr is not None:
            return self.th.turbo_exposure_max_torr
        return self.th.turbo_on_wiggle_room_multiplier * self.th.engage_threshold(self.turbo_on_threshold_torr)

    def _chamber_roughed(self, inp: Inputs) -> bool:
        p = inp.pressure(self.cfg.main_gauge.id)
        if math.isnan(p) or p <= self.th.wrg_error_threshold_torr:
            return False                                  # no reading = assume atmosphere
        return p <= self.exposure_limit_torr()

    def _turning(self, cmds: Commands, inp: Inputs, tid: str) -> bool:
        return self._motor(cmds, tid) or self._speed(inp, tid) > self.th.turbo_slow_speed_pct

    def exposure(self, valve_id: str, cmds: Commands, inp: Inputs) -> Verdict:
        """Would OPENING this valve join a turning turbo to an un-roughed chamber?"""
        cfg = self.cfg
        if self._chamber_roughed(inp):
            return Verdict(True)
        v = cfg.valves[valve_id]
        p = inp.pressure(cfg.main_gauge.id)
        shown = ("not readable" if math.isnan(p) or p <= self.th.wrg_error_threshold_torr
                 else f"at {torr_to('mbar', p):.1e} mBar")
        why = f"the chamber is {shown} (limit {torr_to('mbar', self.exposure_limit_torr()):.1e} mBar)"

        def is_open(vid: str) -> bool:
            return bool(cmds.valves.get(vid, False) or inp.valve_reads.get(vid, False))

        if v.kind == "bypass":
            for tv in cfg.valves_of_kind("turbo"):
                if not is_open(tv.id):
                    continue
                for t in cfg.turbos_on_valve(tv.id):
                    if self._turning(cmds, inp, t.id):
                        return Verdict(False, f"{t.label} is still turning with {tv.label} open and {why} – "
                                              f"its exhaust must never see that.  Wait for the turbo to stop, "
                                              f"or let Auto rough the chamber (it isolates the turbo first)")
        elif v.kind == "turbo":
            if self._valve(cmds, inp, "bypass"):
                for t in cfg.turbos_on_valve(valve_id):
                    if self._turning(cmds, inp, t.id):
                        return Verdict(False, f"Please Close Bypass Valve first – {t.label} is still turning and "
                                              f"{why}: its exhaust must never see that")
        elif v.kind == "gate":
            for t in cfg.turbos:
                if t.gate_valve == valve_id and self._turning(cmds, inp, t.id):
                    return Verdict(False, f"Main Facility Pressure Too High – {t.label} is still turning and "
                                          f"{why}: its inlet must never see that")
        return Verdict(True)

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
                verdict = self.exposure(valve_id, cmds, inp)
                if not verdict.allowed:
                    return verdict
                return self.backing_ready(valve_id, cmds, inp)
            # closing while a turbo runs would trap it - every turbo backed through this valve
            # (VC100: TMP4/5/6 share Turbo 1/2/3's valve)
            for t in (self.cfg.turbos_on_valve(valve_id) or [None]):
                verdict = self._turbo_stopped(cmds, inp, t.id if t else v.turbo)
                if not verdict.allowed:
                    return verdict
            return Verdict(True)
        if v.kind == "bypass":
            if want_open:
                if self._valve(cmds, inp, "vent"):
                    return Verdict(False, "Please Close Vent Valve first")
                if self._valve(cmds, inp, "gate"):
                    verdict = self._turbo_stopped(cmds, inp)
                    if not verdict.allowed:
                        return verdict
                return self.exposure(valve_id, cmds, inp)
            return Verdict(True)
        if v.kind == "gate":
            if want_open:
                if self._valve(cmds, inp, "vent"):
                    return Verdict(False, "Please Close Vent Valve first")
                if self._valve(cmds, inp, "bypass"):
                    return Verdict(False, "Please Close Bypass Valve first")
                return self.exposure(valve_id, cmds, inp)
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
