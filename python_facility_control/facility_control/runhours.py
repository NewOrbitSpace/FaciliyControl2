"""Persistent run-hour meters (e.g. 'Primary pump total operating hours').

The counter has to survive restarts, so it is kept in a small JSON file next to the CSV logs and
written back periodically and on exit.  Pass `path=None` for an in-memory meter (tests, headless).
Counting is driven by the *read-back* of the device, so it reflects the hours the pump actually ran,
not the hours it was commanded to run.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional


class RunHours:
    def __init__(self, path: Optional[str] = None, save_period_s: float = 60.0):
        self.path = Path(path) if path else None
        self.save_period_s = save_period_s
        self.seconds: Dict[str, float] = {}
        self._last_save = 0.0
        self._dirty = False
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k, v in (data.get("seconds") or {}).items():
                self.seconds[str(k)] = float(v)
        except Exception:
            pass          # a corrupt meter file must never stop the facility program

    def save(self, force: bool = False, now: float = 0.0) -> None:
        if not self.path or not self._dirty:
            return
        if not force and (now - self._last_save) < self.save_period_s:
            return
        self._last_save = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"seconds": self.seconds}, fh)
            os.replace(tmp, self.path)     # atomic – a power cut can't leave a half-written meter
            self._dirty = False
        except Exception:
            pass

    # ------------------------------------------------------------------ counting
    def add(self, key: str, seconds: float) -> None:
        if seconds <= 0:
            return
        self.seconds[key] = self.seconds.get(key, 0.0) + seconds
        self._dirty = True

    def hours(self, key: str) -> float:
        return self.seconds.get(key, 0.0) / 3600.0
