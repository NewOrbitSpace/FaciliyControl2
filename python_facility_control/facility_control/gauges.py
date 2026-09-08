"""Gauge conversions – copied from the formula nodes of Main_V4.4.vi.

Convectron 1 / 2:  P_Conve_Torr = (10**V*1E-4)
Ion gauge (WRG):   P_WRG_Torr   = 10**(1.667*V-11.46)
"""
from __future__ import annotations

import math
from typing import Callable, Dict

FormulaFn = Callable[[float], float]
InverseFn = Callable[[float], float]


def convectron_v_to_torr(v: float) -> float:
    return 10.0**v * 1e-4


def convectron_torr_to_v(p: float) -> float:
    p = max(p, 1e-30)
    return math.log10(p / 1e-4)


def ion_gauge_v_to_torr(v: float) -> float:
    return 10.0 ** (1.667 * v - 11.46)


def ion_gauge_torr_to_v(p: float) -> float:
    p = max(p, 1e-30)
    return (math.log10(p) + 11.46) / 1.667


# Leybold log-linear active gauges used on VC100/VC140 (reference: Leybold manuals).
# Kept here so another facility only needs to name the formula in its YAML.
def leybold_ttr91_v_to_mbar(v: float) -> float:      # Pirani TTR91: p = 10**(U-5.5) mbar
    return 10.0 ** (v - 5.5)


def leybold_ptr90_v_to_mbar(v: float) -> float:      # PTR90 wide range: p = 10**(1.667*U - 11.33) mbar
    return 10.0 ** (1.667 * v - 11.33)


def _mbar_to_torr(fn):
    return lambda v: fn(v) * 0.7500616827


FORMULAS: Dict[str, FormulaFn] = {
    "convectron": convectron_v_to_torr,
    "ion_gauge": ion_gauge_v_to_torr,
    "leybold_ttr91": _mbar_to_torr(leybold_ttr91_v_to_mbar),
    "leybold_ptr90": _mbar_to_torr(leybold_ptr90_v_to_mbar),
}

INVERSES: Dict[str, InverseFn] = {
    "convectron": convectron_torr_to_v,
    "ion_gauge": ion_gauge_torr_to_v,
    "leybold_ttr91": lambda p: math.log10(max(p, 1e-30) / 0.7500616827) + 5.5,
    "leybold_ptr90": lambda p: (math.log10(max(p, 1e-30) / 0.7500616827) + 11.33) / 1.667,
}


def v_to_torr(formula: str, v: float) -> float:
    return FORMULAS[formula](v)


def torr_to_v(formula: str, p_torr: float) -> float:
    return INVERSES[formula](p_torr)


def speed_pct_from_v(v: float) -> float:
    """D-SUB speed output: 0-10 V (HiPace) or +/-10 V (BigRed) -> % (VI: mean * 10)."""
    return v * 10.0


def speed_pct_from_rpm(rpm: float, rpm_full: float = 820.0) -> float:
    """RS485: rpm*0.016667/820*100 in the VI (rpm -> Hz -> % of 820 Hz)."""
    return rpm * 0.016667 / rpm_full * 100.0
