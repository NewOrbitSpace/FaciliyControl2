"""A turning turbo must never see an un-roughed chamber - not at its exhaust, not at its inlet.

Reported from the VC100 (and the small chamber, built the same way) on 2026-11-11:

    "When the turbos are spinning down the chamber is allowed to vent whilst keeping the primary pump
     backing up the slowly spinning down turbos.  In this state - chamber at atmosphere, gate valves
     closed, turbos spinning down, turbo valves open and primary on - if you then click Pump to High
     Vac (likely also Pump to Rough) the bypass valve will open, exposing the exhausts of the turbos
     to atmosphere. ...  In the previous software the turbo valves would close before the bypass
     valve is ever opened.  It was deemed acceptable to let the turbos spin down without the primary
     as a backing pump for the short time it takes the chamber to get from atmosphere to rough."

Both VIs do exactly that (Venting / Turbo slowing frames: TV := [F,F,F] whenever the next state is
a different one; Pumping to Rough keeps them shut until rough vacuum).  Commit a8f8052 (2026-09-22)
added a global rule "a spinning turbo always keeps its turbo valve", which overrode it.  The rule
is now: never expose > never strand (automode.py, global protections 1 and 3).

These tests watch the PLANT (simulator valve positions and pressures), not only the commands.
"""
import math

import pytest

from facility_control.automode import AutoInput, AutoStateMachine, SUB_ROUGH_ENTRY, SUB_ROUGH_WAIT
from facility_control.config import CONFIG_DIR, load_config
from facility_control.interlocks import Interlocks
from facility_control.model import Commands, FacilityState as S, Inputs, Mode, TurboInputs
from facility_control.units import to_torr
from tests.conftest import Harness

ATM = 760.0


# =============================================================================== plant harness
def _profile(which):
    name = "facility_vc100.yaml" if which == "vc100" else "facility_main_v4.4.yaml"
    cfg = load_config(str(CONFIG_DIR / name))
    cfg.simulation.time_scale = 400.0 if which == "vc100" else 100.0
    return cfg


def _limit(cfg):
    th = cfg.thresholds
    if th.turbo_exposure_max_torr is not None:
        return th.turbo_exposure_max_torr
    return th.turbo_on_wiggle_room_multiplier * th.engage_threshold(th.turbo_on_threshold_torr)


class Plant:
    """Harness + the physical truth of the simulator."""

    def __init__(self, which, spindown_x=1.0):
        self.cfg = _profile(which)
        # real turbos coast for 20-60 min - longer than a vent; the demo simulator's default is short
        self.cfg.simulation.turbo_spindown_s *= spindown_x
        self.h = Harness(self.cfg)
        self.st = self.h.backend.state
        self.turbos = [t for t in self.cfg.turbos if t.in_use]
        self.limit = _limit(self.cfg)
        for _ in range(8):
            self.h.dialogs.answers.put(True)             # 'Is the manual vent valve closed?' -> yes
        self.exposed = []                                # (iteration, what) - must stay empty
        self.worst_exhaust = 0.0                         # highest turbo-body pressure with the rotor turning
        self.i = 0

    def turning(self, t):
        return self.st.turbos[t.id].speed_pct > self.cfg.thresholds.turbo_slow_speed_pct

    def press(self, button):
        self.h.ctl.request_auto_button(button)

    def step(self):
        s = self.h.step()
        self.i += 1
        st = self.st
        bypass = st.valves["bypass"].position
        for t in self.turbos:
            if not self.turning(t):
                continue
            tv = st.valves[t.turbo_valve].position
            gate = st.valves[t.gate_valve].position
            # margin of 4 x the limit: valve travel and gauge lag are real, a vented chamber is 600x over
            if bypass > 0.02 and tv > 0.02 and st.p_chamber > 4 * self.limit:
                self.exposed.append((self.i, f"EXHAUST of {t.id} at {st.turbos[t.id].speed_pct:.0f} % joined to the "
                                             f"chamber at {st.p_chamber:.0f} Torr in {s.current.label}/{s.substate}"))
            if gate > 0.02 and st.p_chamber > 4 * self.limit:
                self.exposed.append((self.i, f"INLET of {t.id} at {st.turbos[t.id].speed_pct:.0f} % opened onto the "
                                             f"chamber at {st.p_chamber:.0f} Torr in {s.current.label}/{s.substate}"))
            self.worst_exhaust = max(self.worst_exhaust, st.turbos[t.id].p_body_torr)
        return s

    def run_until(self, pred, n=400000):
        for _ in range(n):
            s = self.step()
            if pred(s):
                return s
        raise AssertionError(f"condition not reached; last state {s.current.label}/{s.substate}")

    def to_high_vac(self):
        self.h.step(2)
        self.press("pump_to_high_vac")
        self.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC)
        self.run_until(lambda s: all(self.st.turbos[t.id].speed_pct > 95 for t in self.turbos))

    def vent_to_atmosphere(self, how):
        """how = 'coasting' (motors off, spinning down) or 'running' (Vent: the turbos keep running)."""
        if how == "running":
            self.press("vent")
        elif self.cfg.auto_mode.venting_buttons_stop_turbos:       # VC100: Shutdown, then Vent on the slowing page
            self.press("shutdown")
            self.run_until(lambda s: s.current == S.TURBO_SLOWING)
            self.press("vent")
        else:                                                      # small chamber
            self.press("vent_and_shutdown")
        self.run_until(lambda s: s.current == S.VENTING)
        s = self.run_until(lambda s: self.st.p_chamber > 600.0 or s.current != S.VENTING)
        # the engineer's state: chamber at atmosphere, gates closed, turbos turning, turbo valves
        # open, primary on, bypass closed
        assert s.current == S.VENTING and self.st.p_chamber > 600.0
        assert any(self.turning(t) for t in self.turbos), "the turbos stopped before the chamber vented"
        assert all(not s.commands.valves[t.gate_valve] for t in self.turbos)
        assert any(s.commands.valves[t.turbo_valve] for t in self.turbos if self.turning(t))
        assert s.commands.primary and not s.commands.valves["bypass"]
        return s


CASES = [(chamber, how, button)
         for chamber in ("small", "vc100")
         for how in ("coasting", "running")
         for button in ("pump_to_high_vac", "pump_to_rough")]


# =============================================================================== the report itself
@pytest.mark.parametrize("chamber,how,button", CASES)
def test_bypass_never_opens_onto_a_turning_turbos_exhaust(chamber, how, button):
    """The engineer's sequence, on both chambers, coasting and at speed, both buttons."""
    p = Plant(chamber, spindown_x=8.0)
    p.to_high_vac()
    p.vent_to_atmosphere(how)
    were_turning = [t for t in p.turbos if p.turning(t)]

    p.press(button)
    tv_shut_at = bypass_open_at = None
    for i in range(400):
        s = p.step()
        cmd = s.commands
        if tv_shut_at is None and not any(cmd.valves[t.turbo_valve] for t in were_turning):
            tv_shut_at = i
        if bypass_open_at is None and cmd.valves["bypass"]:
            bypass_open_at = i
            # the moment the bypass is commanded: every turning turbo is already isolated - commanded
            # shut in an EARLIER iteration (it had its settle wait) and physically shut
            for t in were_turning:
                assert not cmd.valves[t.turbo_valve], f"{t.turbo_valve} still commanded open"
                assert p.st.valves[t.turbo_valve].position < 0.02, f"{t.turbo_valve} not shut yet"
                assert not cmd.turbo_motor[t.id], f"{t.id} still driven while isolated"
            break
    assert bypass_open_at is not None, "roughing never started"
    assert tv_shut_at is not None and tv_shut_at < bypass_open_at, \
        f"turbo valves shut at {tv_shut_at}, bypass opened at {bypass_open_at}"
    assert s.current == S.PUMPING_TO_ROUGH
    assert any("BEFORE the bypass valve" in line for line in s.event_log)

    # ... and all the way through the roughing: never exposed, never tripped
    for _ in range(3000):
        s = p.step()
        assert not s.error.status, f"{s.error.code}: {s.error.source}"
    assert not p.exposed, p.exposed[:3]
    assert p.worst_exhaust < p.cfg.thresholds.turbo_high_pressure_shutoff_torr, \
        f"a turning turbo's exhaust reached {p.worst_exhaust:.1f} Torr"
    assert not any(t.error for t in p.st.turbos.values())


@pytest.mark.parametrize("chamber", ["small", "vc100"])
def test_the_facility_still_comes_back_to_high_vacuum(chamber):
    """After the isolated roughing the turbo valves re-open, the turbos restart, the gates open."""
    p = Plant(chamber, spindown_x=8.0)
    p.to_high_vac()
    p.vent_to_atmosphere("coasting")
    p.press("pump_to_high_vac")
    s = p.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC and all(
        s.commands.valves[t.gate_valve] and s.commands.turbo_motor[t.id] for t in p.turbos))
    assert all(s.commands.valves[t.turbo_valve] for t in p.turbos)
    assert not s.commands.valves["bypass"] and not s.error.status
    assert not p.exposed, p.exposed[:3]
    assert not any(t.error for t in p.st.turbos.values())


@pytest.mark.parametrize("chamber", ["small", "vc100"])
def test_an_isolated_turbo_gets_its_backing_back_once_the_foreline_is_pumped(chamber):
    """Pump to Rough keeps the turbo valves shut for good in the VIs (TV := tgt != Rough ...).  A
    turbo that is still coasting gets its backing back as soon as the chamber is roughed - never
    onto a foreline at chamber pressure."""
    p = Plant(chamber, spindown_x=30.0)
    p.to_high_vac()
    p.vent_to_atmosphere("coasting")
    p.press("pump_to_rough")
    th = p.cfg.thresholds
    ok_below = th.turbo_valve_foreline_ok_torr if th.turbo_valve_foreline_ok_torr is not None else th.turbo_on_threshold_torr
    reopened = False
    for _ in range(60000):
        s = p.step()
        for t in p.turbos:
            if p.turning(t) and s.commands.valves[t.turbo_valve] and p.st.valves["bypass"].position > 0.02:
                # with the bypass open the foreline is the chamber: the valve may only be open when
                # that is roughed
                assert p.st.p_chamber <= 4 * p.limit, f"{t.turbo_valve} open at {p.st.p_chamber:.1f} Torr"
                reopened = True
        if reopened and p.st.p_foreline < 0.5 * ok_below:
            break
    assert reopened, "the coasting turbo never got its backing back"
    # said by Auto itself, or - with auto_mode.turbo_valve_needs_backing (VC100) - by the controller's
    # opening guard, which applies the same test
    assert any("has its backing back" in line or "foreline pumped down – opening" in line
               for line in s.event_log)
    assert not p.exposed, p.exposed[:3]


def test_overnight_then_a_chamber_that_came_up(cfg=None):
    """Same shape from Overnight Pump: the spinning turbo is backed (valve open, primary on); if
    the chamber has come up meanwhile, Pump to High Vac must isolate it before roughing."""
    p = Plant("small", spindown_x=20.0)
    p.to_high_vac()
    p.h.ctl.request_set_engage_time(p.h.ctl.clock() + 1e7)
    p.press("overnight_pump")
    s = p.run_until(lambda s: s.current == S.OVERNIGHT_PUMP)
    p.step()
    assert p.turning(p.turbos[0]) and p.h.ctl.cmds.valves["turbo_valve"]
    p.st.p_chamber = 300.0                                # hand vent valve opened / leak
    p.h.step(2)
    p.press("pump_to_high_vac")
    for _ in range(2000):
        p.step()
    assert not p.exposed, p.exposed[:3]
    assert p.worst_exhaust < p.cfg.thresholds.turbo_high_pressure_shutoff_torr


# =============================================================================== what must NOT change
def test_a_cold_pump_down_is_untouched():
    """Turbos at rest: nothing to protect - no isolation message, bypass and turbo valves as before."""
    p = Plant("small")
    p.h.step(2)
    p.press("pump_to_high_vac")
    s = p.run_until(lambda s: s.commands.valves["bypass"], 4000)
    assert p.st.p_chamber > 100.0
    s = p.run_until(lambda s: s.current == S.PUMPING_TO_HIGH_VAC and s.commands.turbo_motor["turbo1"], 200000)
    assert not any("BEFORE the bypass valve" in line or "Pressure NOT low enough to open gate" in line
                   for line in s.event_log)
    assert not p.exposed


def test_high_vacuum_re_engage_keeps_the_backing():
    """The 2026-09-22 case: Shutdown, then Pump to High Vac at 1e-5 mBar.  The chamber is far below
    the exposure limit, so the turbo valve stays open and nothing is roughed."""
    p = Plant("small")
    p.to_high_vac()
    p.run_until(lambda s: p.st.p_chamber < 1e-3)
    p.press("shutdown")
    p.run_until(lambda s: s.current == S.TURBO_SLOWING)
    p.press("pump_to_high_vac")
    for _ in range(6):
        s = p.step()
        assert s.commands.valves["turbo_valve"], "isolated a turbo with nothing to protect it from"
    assert s.current in (S.ENGAGE_TURBO, S.PUMPING_TO_HIGH_VAC)
    assert not any("BEFORE the bypass valve" in line for line in s.event_log)


# =============================================================================== the rule, unit level
def _machine(which="small"):
    cfg = _profile(which)
    return cfg, AutoStateMachine(cfg)


def _inputs(cfg, wrg, fore=1e-2, body=1e-3, speed=40.0, tv_read=None, bypass_read=False):
    inp = Inputs()
    inp.pressures_torr = {g.id: body for g in cfg.gauges}
    inp.pressures_torr[cfg.main_gauge.id] = wrg
    inp.pressures_torr[cfg.foreline_gauge.id] = fore
    inp.primary_read = True
    inp.primary_hz = None
    inp.chiller_read = True
    inp.valve_reads = {v: False for v in cfg.valve_ids}
    inp.valve_reads["bypass"] = bypass_read
    for t in cfg.turbos:
        ti = TurboInputs(speed_pct=speed)
        if t.control_mode == "shimadzu_contacts":
            ti.contacts = {"rotating": speed > 0}
        inp.turbos[t.id] = ti
        if tv_read is not None:
            inp.valve_reads[t.turbo_valve] = tv_read
    return inp


def _prev(cfg, tv=True, bypass=False, primary=True):
    c = Commands.all_off(cfg.valve_ids, cfg.turbo_ids)
    c.primary = primary
    c.primary_run = primary
    c.primary_power = primary
    c.chiller = True
    for t in cfg.turbos:
        c.valves[t.turbo_valve] = tv
    c.valves["bypass"] = bypass
    return c


def _step(sm, cfg, inp, prev, cur=S.PUMPING_TO_ROUGH, tgt=S.PUMPING_TO_HIGH_VAC, sub=SUB_ROUGH_ENTRY, buttons=()):
    return sm.step(AutoInput(inp, prev, cur, tgt, sub, set(buttons), 1000.0, False,
                             cfg.thresholds.turbo_on_threshold_torr, None, False))


@pytest.mark.parametrize("which", ["small", "vc100"])
def test_unit_turbo_valves_first_then_the_bypass(which):
    cfg, sm = _machine(which)
    tvs = sorted({t.turbo_valve for t in cfg.turbos})
    # 1st iteration: valves open, chamber at atmosphere, Rough wants the bypass -> valves shut, bypass HELD
    out = _step(sm, cfg, _inputs(cfg, ATM, tv_read=True), _prev(cfg, tv=True))
    assert not out.commands.valves["bypass"]
    assert not any(out.commands.valves[tv] for tv in tvs)
    assert not any(out.commands.turbo_motor.values())
    # 2nd: commanded shut, but the reed contacts still read open -> still held
    out2 = _step(sm, cfg, _inputs(cfg, ATM, tv_read=True), out.commands, sub=out.substate)
    assert not out2.commands.valves["bypass"] and not any(out2.commands.valves[tv] for tv in tvs)
    # 3rd: read shut -> now the bypass opens, the valves stay shut
    out3 = _step(sm, cfg, _inputs(cfg, ATM, tv_read=False), out2.commands, sub=out2.substate)
    assert out3.commands.valves["bypass"] and not any(out3.commands.valves[tv] for tv in tvs)
    # ... and stay shut for as long as the chamber is not roughed, whatever the state logic asks
    out4 = _step(sm, cfg, _inputs(cfg, 50.0, fore=50.0, bypass_read=True), out3.commands, sub=SUB_ROUGH_WAIT)
    assert out4.commands.valves["bypass"] and not any(out4.commands.valves[tv] for tv in tvs)


def test_unit_an_unreadable_chamber_gauge_counts_as_atmosphere():
    cfg, sm = _machine()
    out = _step(sm, cfg, _inputs(cfg, math.nan, tv_read=True), _prev(cfg, tv=True))
    assert not out.commands.valves["bypass"] and not out.commands.valves["turbo_valve"]


def test_unit_a_roughed_chamber_isolates_nothing():
    cfg, sm = _machine()
    limit = _limit(cfg)
    out = _step(sm, cfg, _inputs(cfg, 0.8 * limit, fore=0.5 * limit, tv_read=True, bypass_read=True),
                _prev(cfg, tv=True, bypass=True), sub=SUB_ROUGH_WAIT)
    assert out.commands.valves["bypass"] and out.commands.valves["turbo_valve"]


def test_unit_a_turbo_at_rest_is_not_isolated_and_not_delayed():
    cfg, sm = _machine()
    out = _step(sm, cfg, _inputs(cfg, ATM, speed=0.0), _prev(cfg, tv=False))
    assert out.commands.valves["bypass"]                 # primary already running -> bypass at once, as before


def test_unit_slow_rotation_counts_as_turning():
    """40 % is below the VC100's 50 % 'slowing' threshold - the states stop backing there - but the
    rotor is very much turning: it must not see atmosphere either."""
    cfg, sm = _machine("vc100")
    assert cfg.thresholds.slowing_pct == 50.0
    out = _step(sm, cfg, _inputs(cfg, ATM, speed=40.0, tv_read=True), _prev(cfg, tv=True))
    assert not out.commands.valves["bypass"]
    assert not any(out.commands.valves[t.turbo_valve] for t in cfg.turbos)


def test_unit_reopen_only_onto_a_pumped_foreline():
    cfg, sm = _machine()
    th = cfg.thresholds
    shut = _prev(cfg, tv=False, bypass=True)
    # chamber roughed (below the limit) but the foreline not yet at backing pressure and above the
    # turbo body: stays isolated
    limit = _limit(cfg)
    inp = _inputs(cfg, 0.9 * limit, fore=0.9 * limit, body=1e-3, bypass_read=True)
    out = _step(sm, cfg, inp, shut, tgt=S.PUMPING_TO_ROUGH, sub=SUB_ROUGH_WAIT)
    assert not out.commands.valves["turbo_valve"]
    # foreline below the turbo-on threshold: backing restored
    inp = _inputs(cfg, 0.5 * th.turbo_on_threshold_torr, fore=0.5 * th.turbo_on_threshold_torr, bypass_read=True)
    out = _step(sm, cfg, inp, shut, tgt=S.PUMPING_TO_ROUGH, sub=SUB_ROUGH_WAIT)
    assert out.commands.valves["turbo_valve"]
    # primary not running: never
    inp.primary_read = False
    off = _prev(cfg, tv=False, bypass=True, primary=False)
    out = _step(sm, cfg, inp, off, tgt=S.PUMPING_TO_ROUGH, sub=SUB_ROUGH_WAIT)
    if cfg.primary.has_read:
        assert not out.commands.valves["turbo_valve"]


def test_exposure_limit_is_configurable():
    cfg = _profile("small")
    th = cfg.thresholds
    assert th.turbo_exposure_max_torr is None
    assert _limit(cfg) == pytest.approx(5.0 * th.turbo_on_threshold_torr)
    import yaml
    from facility_control import config as config_mod
    raw = yaml.safe_load(open(CONFIG_DIR / "facility_main_v4.4.yaml"))
    raw["thresholds"]["turbo_exposure_max_mbar"] = 0.5
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(raw, f)
    try:
        c2 = config_mod.load_config(f.name)
    finally:
        os.unlink(f.name)
    assert c2.thresholds.turbo_exposure_max_torr == pytest.approx(to_torr("mbar", 0.5))
    sm = AutoStateMachine(c2)
    assert sm._exposure_limit(c2.thresholds.turbo_on_threshold_torr) == pytest.approx(to_torr("mbar", 0.5))


# =============================================================================== the inlet
def test_gates_do_not_open_onto_turning_turbos_if_the_chamber_came_up():
    """VC100, turbos-first Engage: the pressure is checked, then the turbos run up to standby with
    the gates closed - minutes.  If the chamber comes up meanwhile the gates must stay shut."""
    p = Plant("vc100")
    p.to_high_vac()
    p.run_until(lambda s: p.st.p_chamber < 1e-3)
    p.press("shutdown")
    p.run_until(lambda s: s.current == S.TURBO_SLOWING)
    p.press("pump_to_high_vac")                           # chamber still evacuated -> straight to Engage
    p.run_until(lambda s: s.current == S.ENGAGE_TURBO and s.substate == 6)
    p.st.p_chamber = 200.0                                # the chamber is let up while they run up
    for _ in range(3000):
        s = p.step()
    assert not p.exposed, p.exposed[:3]
    assert any("Pressure NOT low enough to open gate" in line for line in s.event_log)


# =============================================================================== Manual / Admin
def _manual_case(which="small", wrg=ATM, speed=40.0, tv=True, bypass=False):
    cfg = _profile(which)
    il = Interlocks(cfg)
    inp = _inputs(cfg, wrg, speed=speed, tv_read=tv, bypass_read=bypass)
    cmds = _prev(cfg, tv=tv, bypass=bypass)
    return cfg, il, cmds, inp


def test_manual_refuses_the_bypass_onto_a_turning_turbo():
    cfg, il, cmds, inp = _manual_case()
    v = il.valve("bypass", True, cmds, inp)
    assert not v.allowed and "exhaust" in v.message
    # at rest, or roughed, or isolated: allowed
    assert il.valve("bypass", True, *_manual_case(speed=0.0)[2:]).allowed
    assert il.valve("bypass", True, *_manual_case(wrg=1e-3)[2:]).allowed
    assert il.valve("bypass", True, *_manual_case(tv=False)[2:]).allowed


def test_manual_refuses_a_turbo_valve_onto_an_open_bypass():
    cfg, il, cmds, inp = _manual_case(tv=False, bypass=True)
    v = il.valve("turbo_valve", True, cmds, inp)
    assert not v.allowed and "Bypass" in v.message


def test_manual_refuses_a_gate_onto_a_turning_turbo():
    cfg, il, cmds, inp = _manual_case()
    v = il.valve("gate", True, cmds, inp)
    assert not v.allowed and "inlet" in v.message
    assert il.valve("gate", True, *_manual_case(wrg=1e-3)[2:]).allowed


def test_admin_is_warned():
    cfg = _profile("small")
    h = Harness(cfg, mode=Mode.ADMIN)
    h.step(2)
    st = h.backend.state
    # a coasting turbo behind an open turbo valve, chamber at atmosphere
    st.turbos["turbo1"].speed_pct = 60.0
    for target in ("primary", "valve:turbo_valve"):
        h.dialogs.answers.put(True)
        h.ctl.request_user_cmd(target)
        h.step(2)
    st.turbos["turbo1"].speed_pct = 60.0
    h.step()
    h.dialogs.answers.put(False)
    h.ctl.request_user_cmd("valve:bypass")
    h.step()
    kind, question = h.dialogs.log[-1]
    assert question.startswith("Open Bypass Valve?") and "WARNING" in question and "exhaust" in question
