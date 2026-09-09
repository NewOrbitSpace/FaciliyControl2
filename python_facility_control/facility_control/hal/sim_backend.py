"""Simulated facility – lets the whole program run with no NI-DAQ attached.

The plant is deliberately simple but physically sensible:

* three gas nodes – chamber (WRG), turbo body (turbo convectron), foreline (foreline convectron)
* valves with actuation delay and reed-switch feedback (open only when fully open)
* primary pump / chiller with delayed run feedback
* turbo with spin-up / spin-down dynamics; effective pumping speed ~ (speed %)^2
* gauge voltages produced with the *inverse* of the VI's formula nodes (+ a little noise)
* fault injection (stuck valve, turbo error/warning, chiller/primary fault, compressor low, gauge fault, power cut)
* Shimadzu contact interface (VC100 Turbo 1): the six status contacts are derived from the modelled speed

Time advances with wall-clock time multiplied by `simulation.time_scale` from the YAML.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..config import FacilityConfig
from ..gauges import torr_to_v, v_to_torr
from ..model import Commands, Inputs, TurboInputs
from .base import HardwareBackend


@dataclass
class _Valve:
    cmd: bool = False
    position: float = 0.0          # 0 closed .. 1 open
    stuck: bool = False


@dataclass
class _Turbo:
    motor: bool = False
    standby: bool = False
    speed_pct: float = 0.0
    error: bool = False
    p_body_torr: float = 760.0     # gas node between gate and turbo valve


@dataclass
class SimState:
    p_chamber: float = 760.0
    p_foreline: float = 760.0
    primary_cmd: bool = False
    primary_on: float = 0.0        # 0..1 ramp (run feedback when > 0.5)
    chiller_cmd: bool = False
    chiller_on: float = 0.0
    valves: Dict[str, _Valve] = field(default_factory=dict)
    turbos: Dict[str, _Turbo] = field(default_factory=dict)
    compressor_bar: float = 6.3
    faults: Dict[str, object] = field(default_factory=dict)


def _solve(M, rhs):
    """Small dense linear solve with partial pivoting (n <= 8)."""
    n = len(rhs)
    M = [row[:] for row in M]
    b = rhs[:]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-300:
            continue
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
            b[col], b[piv] = b[piv], b[col]
        for r in range(col + 1, n):
            f = M[r][col] / M[col][col]
            if f:
                for c in range(col, n):
                    M[r][c] -= f * M[col][c]
                b[r] -= f * b[col]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = b[r] - sum(M[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / M[r][r] if abs(M[r][r]) > 1e-300 else 0.0
    return x


class SimBackend(HardwareBackend):
    name = "simulation"

    def __init__(self, config: FacilityConfig, seed: int = 1):
        super().__init__(config)
        self.sim = config.simulation
        self.rng = random.Random(seed)
        self.state = SimState()
        self._last_t: Optional[float] = None
        self._sim_time = 0.0
        for vid in config.valve_ids:
            self.state.valves[vid] = _Valve()
        for tid in config.turbo_ids:
            self.state.turbos[tid] = _Turbo(p_body_torr=self.sim.atmosphere_torr)
        self.state.p_chamber = self.sim.atmosphere_torr
        self.state.p_foreline = self.sim.atmosphere_torr
        self.state.compressor_bar = self.sim.compressor_bar
        self.noise = 0.004  # volts

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        self._last_t = time.monotonic()

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return f"simulation (time x{self.sim.time_scale:g})"

    # ------------------------------------------------------------------ faults
    def set_fault(self, name: str, value) -> None:
        self.state.faults[name] = value
        if name == "compressor_low":
            self.state.compressor_bar = 2.0 if value else self.sim.compressor_bar
        if name == "power_cut" and value:
            # everything drops: DO can't hold, fail-closed valves shut, pumps stop
            for v in self.state.valves.values():
                v.cmd = False
            self.state.primary_cmd = False
            self.state.chiller_cmd = False
            for t in self.state.turbos.values():
                t.motor = False

    def faults(self) -> Dict[str, object]:
        return dict(self.state.faults)

    # ------------------------------------------------------------------ commands
    def write(self, cmds: Commands) -> None:
        st = self.state
        if st.faults.get("power_cut"):
            return
        for vid, want in cmds.valves.items():
            st.valves[vid].cmd = bool(want)
        st.primary_cmd = bool(cmds.primary)
        st.chiller_cmd = bool(cmds.chiller)
        for tid, t in st.turbos.items():
            t.motor = bool(cmds.turbo_motor.get(tid, False))
            t.standby = bool(cmds.turbo_standby.get(tid, False))

    def turbo_error_ack(self, turbo_id: str, level: bool) -> None:
        """Error-acknowledge / 'Reset' line: clears a tripped turbo unless the fault is still injected."""
        f = self.state.faults
        if level and self.state.turbos[turbo_id].error and not (f.get("turbo_error") or f.get(f"turbo_error_{turbo_id}")):
            self.state.turbos[turbo_id].error = False
        if level and self.state.faults.get("turbo_error") == "latched":
            self.state.faults["turbo_error"] = False
            self.state.turbos[turbo_id].error = False

    # ------------------------------------------------------------------ plant model
    def _step(self, dt: float) -> None:
        st, sim, cfg = self.state, self.sim, self.config
        # valves: actuate towards command (pneumatic: bypass/gate need compressed air to move)
        air_ok = st.compressor_bar > 4.0
        for vid, v in st.valves.items():
            vc = cfg.valves[vid]
            target = 1.0 if v.cmd else 0.0
            if v.stuck or st.faults.get(f"stuck_{vid}"):
                continue
            if not air_ok and not (vc.fail_closed and target == 0.0):
                continue  # needs air to move (fail-closed valves still spring shut)
            rate = dt / max(sim.valve_actuation_s, 1e-3)
            v.position += max(-rate, min(rate, target - v.position))
            v.position = min(1.0, max(0.0, v.position))
        # pumps
        prim_target = 1.0 if (st.primary_cmd and not st.faults.get("primary_fault")) else 0.0
        st.primary_on += max(-dt / 1.0, min(dt / 1.5, prim_target - st.primary_on))
        st.primary_on = min(1.0, max(0.0, st.primary_on))
        chil_target = 1.0 if (st.chiller_cmd and not st.faults.get("chiller_fault")) else 0.0
        st.chiller_on += max(-dt / 0.5, min(dt / 0.5, chil_target - st.chiller_on))
        st.chiller_on = min(1.0, max(0.0, st.chiller_on))
        # turbos
        for tid, t in st.turbos.items():
            tcfg = cfg.turbo(tid)
            fault = st.faults.get("turbo_error") or st.faults.get(f"turbo_error_{tid}")   # all turbos / one turbo
            if fault:
                t.error = True
            powered = st.chiller_on > 0.5  # chiller contactor also powers the turbo controllers
            if t.motor and powered and not t.error:
                target = float(tcfg.params.get("standby_speed_pct", 66.0)) if t.standby else 100.0
                if t.speed_pct < target:
                    t.speed_pct = min(target, t.speed_pct + 100.0 * dt / sim.turbo_spinup_s)
                else:
                    t.speed_pct = max(target, t.speed_pct - 100.0 * dt / sim.turbo_spindown_s)
            else:
                # spin-down: faster at high pressure (gas friction)
                extra = 1.0 + min(20.0, t.p_body_torr / 5.0)
                t.speed_pct = max(0.0, t.speed_pct - 100.0 * dt * extra / sim.turbo_spindown_s)
            # overspeed at high pressure -> the real turbo would trip; emulate an error above 20 Torr at >50 %
            if t.speed_pct > 50 and t.p_body_torr > 20.0 and not st.faults.get("no_pressure_trip"):
                t.error = True

        # gas flows (Torr*l/s) – backward-Euler on the node vector [P_c, P_t(1..n), P_f] -----
        # V dP/dt = -A P + b   ->   (V/dt + A) P_new = (V/dt) P + b     (unconditionally stable)
        V_c, V_f, V_t = sim.chamber_volume_l, sim.foreline_volume_l, 3.0
        P_atm = sim.atmosphere_torr
        pos = {vid: v.position for vid, v in st.valves.items()}
        tids = list(st.turbos.keys())
        n = 2 + len(tids)
        A = [[0.0] * n for _ in range(n)]
        bvec = [0.0] * n
        Vd = [V_c / dt] + [V_t / dt] * len(tids) + [V_f / dt]
        iC, iF = 0, n - 1

        def cond(vid: str, c_open: float) -> float:
            return c_open * pos.get(vid, 0.0)

        # conductances (l/s) – order-of-magnitude values tuned for a sensible demo
        C_bypass = cond("bypass", 6.0)
        C_vent = cond("vent", 4.0)
        S_prim = sim.primary_pump_speed_l_s * st.primary_on
        P_ult_prim = 3e-3
        Q_leak = sim.leak_torr_l_s
        Q_outgas = sim.outgassing_torr_l_s * (1.0 if st.p_chamber < 1.0 else 0.0)
        # chamber
        A[iC][iC] += C_bypass + C_vent
        A[iC][iF] -= C_bypass
        bvec[iC] += C_vent * P_atm + Q_leak + Q_outgas
        # foreline
        A[iF][iF] += C_bypass + S_prim
        A[iF][iC] -= C_bypass
        bvec[iF] += S_prim * P_ult_prim
        if st.primary_on < 0.05:               # primary off: foreline back-fills slowly
            A[iF][iF] += 0.02
            bvec[iF] += 0.02 * P_atm
        for k, tid in enumerate(tids):
            t = st.turbos[tid]
            tcfg = cfg.turbo(tid)
            i = 1 + k
            C_gate = cond(tcfg.gate_valve, 60.0)
            C_tv = cond(tcfg.turbo_valve, 8.0)
            S_turbo = sim.turbo_pump_speed_l_s * (t.speed_pct / 100.0) ** 2 * (1.0 if pos.get(tcfg.turbo_valve, 0) > 0.5 else 0.0)
            S_turbo *= 1.0 / (1.0 + (t.p_body_torr / 0.05) ** 2)      # compression collapses at high inlet pressure
            if t.p_body_torr > 1e-9:
                S_turbo = min(S_turbo, 2.0 / t.p_body_torr)            # max throughput ~2 Torr*l/s
            A[iC][iC] += C_gate
            A[iC][i] -= C_gate
            A[i][iC] -= C_gate
            A[i][i] += C_gate + C_tv + S_turbo
            A[i][iF] -= C_tv
            A[iF][i] -= C_tv + S_turbo                                   # turbo exhaust into the foreline
            A[iF][iF] += C_tv
            bvec[i] += 5e-8 * 1.0                                        # tiny outgassing floor in the body
        # assemble and solve (V/dt + A) x = V/dt * x_old + b  (Gaussian elimination, n <= ~6)
        M = [[A[r][c] + (Vd[r] if r == c else 0.0) for c in range(n)] for r in range(n)]
        x_old = [st.p_chamber] + [st.turbos[t].p_body_torr for t in tids] + [st.p_foreline]
        rhs = [Vd[r] * x_old[r] + bvec[r] for r in range(n)]
        x = _solve(M, rhs)
        st.p_chamber = min(P_atm, max(sim.chamber_ultimate_torr, x[iC]))
        for k, tid in enumerate(tids):
            st.turbos[tid].p_body_torr = min(P_atm, max(5e-8, x[1 + k]))
        st.p_foreline = min(P_atm, max(1e-4, x[iF]))

    def advance(self, wall_dt: float) -> None:
        """Advance the plant by wall_dt seconds (scaled), with sub-stepping for stability."""
        total = wall_dt * self.sim.time_scale
        if total <= 0:
            return
        n = max(1, int(math.ceil(total / 0.2)))
        h = total / n
        for _ in range(n):
            self._step(h)
        self._sim_time += total

    # ------------------------------------------------------------------ reads
    def read(self) -> Inputs:
        now = time.monotonic()
        if self._last_t is None:
            self._last_t = now
        self.advance(min(now - self._last_t, 5.0))
        self._last_t = now
        st, cfg = self.state, self.config
        inp = Inputs(t=time.time())
        power = not st.faults.get("power_cut")
        for vid, v in st.valves.items():
            inp.valve_reads[vid] = bool(v.position > 0.95) and power
        inp.primary_read = (st.primary_on > 0.5) and power
        inp.chiller_read = (st.chiller_on > 0.5) and power
        # gauges
        for g in cfg.gauges:
            if g.role == "main_chamber":
                p = st.p_chamber
            elif g.role == "foreline":
                p = st.p_foreline
            elif g.role == "turbo" and g.turbo in st.turbos:
                p = st.turbos[g.turbo].p_body_torr
            else:
                p = st.p_chamber
            if st.faults.get(f"gauge_fault_{g.id}") or not power:
                v = 0.0 if g.formula == "convectron" else -1.0
                inp.gauge_volts[g.id] = v
                inp.pressures_torr[g.id] = float("nan") if not power else v_to_torr(g.formula, v)
                continue
            v = torr_to_v(g.formula, p) + self.rng.gauss(0.0, self.noise)
            v = min(10.0, max(0.0, v))
            inp.gauge_volts[g.id] = v
            inp.pressures_torr[g.id] = v_to_torr(g.formula, v)
        for ea in cfg.extra_analog:
            inp.extra_analog[ea["id"]] = 0.5 + self.rng.gauss(0.0, 0.01)
        for tid, t in st.turbos.items():
            tcfg = cfg.turbo(tid)
            if tcfg.control_mode == "shimadzu_contacts":
                # contact interface: no speed signal, six status contacts derived from the modelled speed
                # (already de-inverted: True = condition present)
                spd = t.speed_pct if power else 0.0
                th = cfg.thresholds
                contacts = {
                    "rotating": spd > 1.0,
                    "accelerating": bool(t.motor and power and not t.error and spd < th.turbo_speed_reached_pct),
                    "at_speed": spd >= th.turbo_speed_reached_pct,
                    "braking": bool((not t.motor or t.error) and spd > th.turbo_slow_speed_pct),
                    "alarm": bool(t.error) if power else False,
                    "warning": bool(st.faults.get("turbo_warning")) if power else False,
                }
                ti = TurboInputs(speed_pct=0.0, error=contacts["alarm"], motor_read=bool(t.motor and power),
                                 still_spinning=contacts["rotating"], contacts=contacts)
            else:
                ti = TurboInputs(speed_pct=t.speed_pct if power else 0.0, error=bool(t.error) if power else False,
                                 motor_read=bool(t.motor and power), still_spinning=bool(t.speed_pct > 5.0))
            inp.turbos[tid] = ti
        # the plant always has an air supply; a *reading* exists only if the facility has the sensor
        inp.compressor_bar = st.compressor_bar if cfg.has_compressor else None
        return inp
