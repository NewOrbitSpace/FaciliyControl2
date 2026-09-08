"""Pressure unit handling.  All internal values are Torr (as in the VI)."""
from __future__ import annotations

import math

TORR_PER_MBAR = 0.7500616827  # 1 mbar = 0.75006 Torr
MBAR_PER_TORR = 1.0 / TORR_PER_MBAR

UNITS = ("torr", "mbar")


def torr_to(unit: str, value_torr: float) -> float:
    if unit == "torr":
        return value_torr
    if unit == "mbar":
        return value_torr * MBAR_PER_TORR
    raise ValueError(f"unknown pressure unit {unit!r}")


def to_torr(unit: str, value: float) -> float:
    if unit == "torr":
        return value
    if unit == "mbar":
        return value * TORR_PER_MBAR
    raise ValueError(f"unknown pressure unit {unit!r}")


def unit_label(unit: str) -> str:
    return {"torr": "Torr", "mbar": "mBar"}[unit]


def format_pressure(value_torr: float, unit: str, digits: int = 2) -> str:
    """LabVIEW-style scientific display, e.g. '6.28E+2'. NaN -> 'NaN'."""
    if value_torr is None or (isinstance(value_torr, float) and math.isnan(value_torr)):
        return "NaN"
    v = torr_to(unit, value_torr)
    if v == 0:
        return "0.00E+0"
    exp = int(math.floor(math.log10(abs(v))))
    mant = v / 10**exp
    return f"{mant:.{digits}f}E{exp:+d}"
