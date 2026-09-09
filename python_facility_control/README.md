# Facility Control – Python port of the LabVIEW vacuum-facility VIs

Replaces the NewOrbit facility-control VIs with one Python program driven by a *facility profile*:
`Main_V4.4.vi` (small chamber, one turbo) and `VC100_Facility_Control_V1.0.vi` (medium chamber, three
turbos) – the same Admin and Auto control, the same interlocks, thresholds, timings, error codes and
colours, a front panel in the LabVIEW style, and a **simulated facility** so it runs on any laptop
without an NI cDAQ.  The chamber is chosen with `--config`:

```
run.bat                                        # small chamber  (config/facility_main_v4.4.yaml)
run.bat --config config/facility_vc100.yaml    # medium chamber (VC100, chassis cDAQ3)
```

```
python_facility_control/
├── run_facility.py           entry point            ├── config/
├── run.bat / run.sh          one-click launchers    │   ├── facility_main_v4.4.yaml   ← small chamber profile (channel map, thresholds…)
├── requirements.txt                                 │   └── facility_vc100.yaml       ← medium chamber profile (3 turbos, cDAQ3)
├── facility_control/         the package            ├── docs/
│   ├── controller.py         main loop (VI frames 0-5)   │   ├── MAIN_V4.4_REFERENCE.md   what the small-chamber VI does, wire by wire
│   ├── automode.py           Auto state machine          │   ├── VC100_REFERENCE.md       the medium-chamber VI: pinout, constants, differences
│   ├── interlocks.py         Manual-mode interlocks      │   └── Main_V4.4.semantic.lvnet  readable netlist of Main_V4.4
│   ├── hal/                  nidaqmx backend + simulator ├── tests/                        pytest suite (76 tests)
│   ├── gui/                  PySide6 front panel         └── tools/                        daq_check.py (read-only bring-up), screenshot_gui.py
│   ├── logging_csv.py        daily CSV log + runhours.py (pump hour meter)
│   └── config.py, model.py, gauges.py, units.py
```

## Running

Windows (lab desktop or laptop):
```
run.bat                        # creates .venv on first use, then starts the GUI
run.bat --sim --time-scale 20  # simulated plant running 20x faster (nice for a demo)
run.bat --daq                  # insist on the real cDAQ (needs NI-DAQmx + `pip install nidaqmx`)
run.bat --units mbar           # start with mBar
run.bat --mode admin           # skip the "Select Control Mode" dialog
run.bat --non-blocking         # keep reading during settle waits (experimental, see below)
run.bat --headless 60          # no GUI, print the state for 60 s (service / smoke test)
```
Anything else: `python run_facility.py --help`.  Without `--sim/--daq` the program uses the real
DAQ if `nidaqmx` and the NI driver are present, otherwise the simulator (shown bottom-right).

`run.bat` keeps the Python environment in `%LOCALAPPDATA%\FacilityControl\venv` (a short path outside
Documents/OneDrive).  Reason: PySide6 ships files with very long internal paths, and a `.venv` inside a
deep project folder hits Windows' 260-character path limit ("No such file or directory … .cpp.obj")
and leaves a half-installed environment.  If you ever see that error: delete the environment folder
and run again, set `FACILITY_VENV=C:\fc-venv` for an even shorter path, or enable long paths in
Windows (Settings → System → For developers → *Enable Win32 long paths*).  The project folder itself
can live anywhere.  For the real cDAQ install the driver package into that environment once:
`%LOCALAPPDATA%\FacilityControl\venv\Scripts\pip install nidaqmx`.

Tests: `python -m pytest -q` (≈3 min, 76 tests – 23 of them for the VC100 profile; the GUI test runs offscreen).

## What the program does (short version – full detail in `docs/MAIN_V4.4_REFERENCE.md`)

Every 100 ms: read all inputs → cross-check every valve/pump command against its read-back
(errors 5001–5010, turbo device errors 5006/5007) → optionally clear the error → decide → write
outputs → append "Opened/Closed … at hh:mm:ss" event lines.  Running LED blinks each loop.

**Settle waits – exactly like the VI (default).** The VI *sleeps* after every device command (1 s
valve, 9 s gate, 10 s chiller, 0.5 s primary, 1 s error-ack pulse) and while a Yes/No dialog is open –
nothing is read and no error is checked in that time, so a device is only cross-checked once it had
its full settle time (an actuation is never flagged as a conflict too early).  The port does the same:
the control loop freezes, the panel keeps repainting and shows what it is waiting for ("Chiller
starting – 8 s (control loop paused – readings resume after the wait)"), readings, plots and the
running LED stand still meanwhile, and buttons pressed during the wait are executed right after it.

*Optional:* `timings.blocking_waits: false` in the YAML (or `run.bat --non-blocking`) keeps the loop
reading during those waits – sensors, error monitoring, plots and CSV continue at 10 Hz, only the
*decision* step waits, and a device that is still moving is not cross-checked until its settle time
is over.  The sequencing of commands is identical in both modes; the difference is only whether the
readings freeze.  Kept for later – the default follows the VI.

**Modes**
* *Auto* – the state machine of the VI, plus the test engineer's start-order rules (below):
  Facility Off → Pumping to Rough (bypass, primary; turbo valve
  after the WRG has stayed below the turbo-on threshold for 30 s) → Engage Turbo (pressure check < 1.25 Torr, close
  bypass, open gate, start turbo) → Pumping to High Vac → Disengage Turbo → Venting (10 min) / Turbo
  slowing → Facility Off; plus Overnight Pump (timed start).  Any error (5000–5010) → everything off
  ("Shutt down due to error").  Turbo forced off if its gauge reads ≥ 5 Torr.
* *Admin* – no interlocks; click a valve/pump/turbo on the diagram, confirm the Yes/No dialog.
  **Auto Mode** button = state recognition (Facility off / Pumping to Rough / Venting / Pumping to
  High Vac) with the "Go to State …?" confirmation, otherwise "Target State Not Recognised!".
* *Manual* – the mode the VI planned but never finished: manual commands checked against interlocks
  ("Please Close Vent Valve first", "Main Facility Pressure Too High", …).

**Units** – **mBar by default** on this facility, Torr ⇄ mBar switch on the panel (all thresholds stay
in Torr internally, as in the VI).  The turbo-on threshold defaults to **2e-1 mBar**.
**Logging** – event log on the panel + one CSV per day in `logs/` (all pressures in Torr and mBar,
every command/read, turbo speed/status, error).  **Plots** – log-pressure and On/Off traces on a
shared time axis.  The panel is used on a **touch screen**, so the plots have finger-sized controls:
*Box zoom* (drag a rectangle to zoom in), separate **Reset X** (back to the live window) and
**Reset Y** (rescale to the traces shown) buttons, and a checkbox per reading to show/hide it –
with *Auto-rescale on show/hide* ticked, hiding a trace rescales the rest.  Any manual pan/zoom
switches *Follow* off automatically.

## The medium chamber (VC100) and adapting to another facility

`config/facility_vc100.yaml` is the medium chamber: chassis **cDAQ3**, three turbo branches (Turbo 1 =
Shimadzu **contact interface** – six status contacts, Motor/Standby/Reset outputs, no speed signal;
Turbos 2 and 3 = Pfeiffer HiPace D-SUB with ±10 V speed and an error line), Edwards WRG/APG gauges in
mBar, the compressed-air sensor (error when < 5 bar for 5 s) and the VI's own Auto rules (20 s primary
warm-up before the bypass, 15 min vent, turbo valves compared with the turbo gauges, primary and
chiller keep backing spinning turbos in Overnight, standby while a gate is closed, "Shut Off once
rough", "Shutdown Now / After Vent").  `docs/VC100_REFERENCE.md` lists every line of the pinout, every
constant and every difference to the small chamber, with the VI node it comes from.

Everything that differs between the two chambers is a profile setting, so a third facility is a copy
of the nearest YAML: channel map, gauge formulas (`convectron`, `ion_gauge`, `edwards_wrg_mbar`,
`edwards_apg_mbar`, `leybold_*`), turbo blocks (`bigred_dsub15 | hipace_dsub25 | shimadzu_contacts`,
each with its lines, error code/message, `error_requires_chiller`, `standby_speed_min_pct`),
`thresholds` (Torr or `_mbar` keys), `timings` (settle waits, gap, vent time), the `auto_mode` rule
switches and the `auto_buttons` per tab page.  Analog inputs use `analog_input.terminal_config` (both
VIs wire the gauges **differentially** = DAQmx 10106); the wrong choice adds a per-channel offset and
the pressures read wrong while the DAQ still "reads OK".

Optional readings are blocks too: `extra_analog` entries show a display-only voltage box and CSV column;
a `compressor:` block (channel, `scale`, `offset` → bar) shows the "Compressor Pressure (Bar)" box, adds
the "Compressor air low" simulator fault and, with `thresholds.compressor_min_bar`, raises error 5011.
The controller, recognition, interlocks, CSV and the diagram follow the config (one branch per turbo;
`in_use: false` parks a turbo like the VIs' "Turbo X in use?" constant).

## Simulation

`hal/sim_backend.py` models chamber / turbo body / foreline pressures, valve actuation with reed
feedback, pump/chiller feedback, turbo spin-up/down and gauge voltages through the VI's own formulas.
The panel at the bottom injects faults (stuck valve, turbo error, chiller/primary fault, WRG fault, power
cut; "compressor air low" only for a facility with a `compressor:` block) and sets the plant speed-up.

## Changes requested by the test engineer (2026-09-08)

These deviate from the VI on purpose and are all driven by the facility profile:

* **Pumping to Rough start order depends on the chamber pressure.**  Above
  `thresholds.bypass_first_above_torr` (**5e1 mBar**) the bypass valve is opened **first** and the
  primary pump starts after it; below it the **primary pump starts first** and the bypass follows
  after `timings.primary_to_bypass_gap_s` (**25 s**, the requested 20-30 s).  The VI always started
  the pump immediately.
* **"Is the manual vent valve closed?"** – Pump to Rough / Pump to High Vac / Overnight Pump ask this
  first and only run on *Yes* (the manual vent valve is a hand valve, not on the DAQ).
* **Overnight Pump below `thresholds.overnight_skip_below_torr` (2e-1 mBar)** skips roughing entirely:
  no primary pump, no bypass, straight into the overnight hold.
* **Primary pump total operating hours** – a maintenance meter on the panel, counted from the pump's
  read-back and kept in `logs/run_hours_<facility>.json` so it survives restarts (also a CSV column).

## Differences from Main_V4.4 (deliberate, all optional)

* Optional non-blocking settle waits and dialogs (`blocking_waits: false`, see above) – off by default.
* CSV logging, zoomable plots, unit switch, Manual mode, config-driven facility layout.
* "Reset Turbos" (error acknowledge) also works in Auto mode.
* The VI appends "Shutt down due to error" every 100 ms while an error persists; here it is logged once.
* HiPace **RS485** control (Pfeiffer PV library) is not ported – both D-SUB modes and the contact interface are.
* Auto mode stops on error codes 5000–5011 (the VIs: 5000–5010; 5011 is the compressor error, which the
  VC100 VI reports under the gate code 5010).

## Connecting to the real facility (bring-up)

The DAQ backend (`hal/nidaqmx_backend.py`) reproduces the VI's task layout and channel map, and is
covered by tests against a *fake* driver – it has **not yet run against the real cDAQ**.  Go in steps:

1. On the lab PC: NI-DAQmx driver installed, cDAQ visible in NI MAX under the profile's
   `facility.daq_name` (`cDAQ1` small chamber, `cDAQ3` medium chamber), then
   `%LOCALAPPDATA%\FacilityControl\venv\Scripts\pip install nidaqmx`.
   **Close LabVIEW** – DAQmx lets only one program own the lines.
2. `python tools\daq_check.py` (add `--config config\facility_vc100.yaml` for the medium chamber) – read-only: lists the devices, checks every channel of the profile
   exists on its module, then prints live gauge / valve / pump / turbo readings for 20 s.  Nothing is
   written, the facility stays as it is.  Compare with the LabVIEW panel (pressures, reed states).
3. With the facility in a safe state (pumps off, valves closed, vented) start `run.bat --daq --mode admin`.
   Like the VI's Initialize frame, start-up and exit command **everything off**.  Check the reads on
   the diagram, then actuate one device at a time in Admin (vent valve first) and watch for conflict
   errors 5001–5010 (wrong polarity or reed wiring shows up here), then primary, chiller, turbo.
4. Only then use Auto mode.  `run.bat --daq` without `--mode` is the normal way to start afterwards.

## Making it a single file for the lab PC (later)

`pip install pyinstaller` then `pyinstaller --onefile --name FacilityControl --add-data "config;config" run_facility.py`
produces `dist\FacilityControl.exe`; copy it with the `config` folder.  The NI-DAQmx driver still
has to be installed on that PC.
