from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional

from ..config import FacilityConfig
from ..model import Commands, Inputs


class HardwareBackend(ABC):
    """What the controller needs from the hardware, nothing more.

    The VI keeps 'command' and 'read' data streams completely separate (redundancy);
    the same holds here: `write()` never influences what `read()` returns except
    through the real (or simulated) plant.
    """

    name: str = "abstract"

    def __init__(self, config: FacilityConfig):
        self.config = config

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def read(self) -> Inputs:
        """Frame 0 of the VI: read every DI/AI and convert gauges to Torr."""

    @abstractmethod
    def write(self, cmds: Commands) -> None:
        """Frame 4 of the VI: write every DO (valves, primary, chiller, turbo motor/standby)."""

    @abstractmethod
    def turbo_error_ack(self, turbo_id: str, level: bool) -> None:
        """Set the error-acknowledge line (HiPace D-SUB) – the controller pulses it."""

    def safe_state(self) -> None:
        """Everything off / closed – used at exit and on safemode."""
        cfg = self.config
        self.write(Commands.all_off(cfg.valve_ids, cfg.turbo_ids))

    @abstractmethod
    def close(self) -> None: ...

    # optional hooks ---------------------------------------------------------
    def set_frequency_enabled(self, on: bool) -> None:
        """Switch the primary pump's drive-frequency reader on or off while the program runs.

        The operator decides whether the sensor is physically connected.  Off must mean the channel
        is not acquired at all - not merely ignored - because acquiring it is what puts noise on the
        other analog inputs.  A backend that does not have the channel ignores this.
        """
        pass

    def set_fault(self, name: str, value) -> None:  # simulation only
        pass

    def faults(self) -> Dict[str, object]:
        return {}

    def describe(self) -> str:
        return self.name
