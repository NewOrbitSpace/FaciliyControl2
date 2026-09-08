"""Hardware abstraction layer.

`HardwareBackend` is the only interface the controller talks to.  Two implementations:

* `SimBackend`      – a small physical model of the facility, no hardware needed
* `NiDaqmxBackend`  – NI cDAQ via the `nidaqmx` package (import guarded)

`create_backend(config, prefer)` picks one: real DAQ when requested *and* available, else simulation.
"""
from __future__ import annotations

from typing import Optional

from ..config import FacilityConfig
from .base import HardwareBackend
from .sim_backend import SimBackend


def daqmx_available() -> bool:
    try:
        import nidaqmx  # noqa: F401
        from nidaqmx.system import System
        System.local().driver_version  # raises if the driver is missing
        return True
    except Exception:
        return False


def create_backend(config: FacilityConfig, prefer: str = "auto", read_only: bool = False) -> HardwareBackend:
    """prefer: 'sim' | 'daq' | 'auto'.  read_only (DAQ only): never write an output – bring-up / monitoring."""
    if prefer == "sim":
        return SimBackend(config)
    if prefer in ("daq", "auto") and daqmx_available():
        from .nidaqmx_backend import NiDaqmxBackend
        return NiDaqmxBackend(config, read_only=read_only)
    if prefer == "daq":
        raise RuntimeError("NI-DAQmx requested but the nidaqmx package / driver is not available")
    return SimBackend(config)


__all__ = ["HardwareBackend", "SimBackend", "create_backend", "daqmx_available"]
