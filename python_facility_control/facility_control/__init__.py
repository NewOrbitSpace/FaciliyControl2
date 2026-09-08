"""facility_control – Python port of the NewOrbit LabVIEW vacuum-facility controller.

Layout
------
config.py        facility profile (YAML) -> dataclasses, validated
model.py         enums / dataclasses shared by controller, HAL and GUI
units.py         Torr <-> mBar
gauges.py        gauge voltage -> pressure formulas (from the VI formula nodes)
hal/             hardware abstraction: nidaqmx backend + simulated plant
automode.py      Auto-mode state machine (exact port of Main_V4.4)
interlocks.py    Manual-mode interlocks (the VI's unfinished "Manual" intent)
controller.py    the main loop (read -> status -> clear -> decide -> command -> log)
logging_csv.py   daily CSV + event log
gui/             PySide6 front panel in the LabVIEW style
"""

__version__ = "0.1.0"
