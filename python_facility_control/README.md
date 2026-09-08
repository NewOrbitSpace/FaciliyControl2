# Facility Control – Python port of the LabVIEW vacuum-facility VI

Replaces `Main_V4.4.vi` (NewOrbit vacuum chamber facility control) with a Python program:
the same manual (Admin) and automatic control, the same interlocks, thresholds, timings, error
codes and colours, a front panel in the LabVIEW style, and a **simulated facility** so it runs on any
laptop without an NI cDAQ.

```
python_facility_control/
├── run_facility.py           entry point            ├── config/
├── run.bat / run.sh          one-click launchers    │   ├── facility_main_v4.4.yaml   ← the facility profile (channel map, thresholds…)
├── requirements.txt                                 │   └── facility_vc100_template.yaml (3-turbo template, unverified)
├── facility_control/         the package            ├── docs/
│   ├── controller.py         main loop (VI frames 0-5)   │   ├── MAIN_V4.4_REFERENCE.md   what the VI does, wire by wire
│   ├── automode.py           Auto state machine          │   └── Main_V4.4.semantic.lvnet  readable netlist of the VI
│   ├── interlocks.py         Manual-mode interlocks      ├── tests/                        pytest suite (44 tests)
│   ├── hal/                  nidaqmx backend + simulator └── tools/                        daq_check.py (read-only bring-up), screenshot_gui.py
│   ├── gui/                  PySide6 front panel
│   ├── logging_csv.py        daily CSV log
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

Tests: `python -m pytest -q` (≈1½ min, 44 tests; the GUI test runs offscreen).

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
* *Auto* – the state machine of the VI: Facility Off → Pumping to Rough (bypass, primary; turbo valve
  after the WRG has stayed below 0.25 Torr for 30 s) → Engage Turbo (pressure check < 1.25 Torr, close
  bypass, open gate, start turbo) → Pumping to High Vac → Disengage Turbo → Venting (10 min) / Turbo
  slowing → Facility Off; plus Overnight Pump (timed start).  Any error (5000–5010) → everything off
  ("Shutt down due to error").  Turbo forced off if its gauge reads ≥ 5 Torr.
* *Admin* – no interlocks; click a valve/pump/turbo on the diagram, confirm the Yes/No dialog.
  **Auto Mode** button = state recognition (Facility off / Pumping to Rough / Venting / Pumping to
  High Vac) with the "Go to State …?" confirmation, otherwise "Target State Not Recognised!".
* *Manual* – the mode the VI planned but never finished: manual commands checked against interlocks
  ("Please Close Vent Valve first", "Main Facility Pressure Too High", …).

**Units** – Torr ⇄ mBar switch (all thresholds stay in Torr internally, as in the VI).
**Logging** – event log on the panel + one CSV per day in `logs/` (all pressures in Torr and mBar,
every command/read, turbo speed/status, error).  **Plots** – log-pressure and On/Off traces on a
shared time axis; untick *Follow* to zoom into history.

## Adapting to another facility (VC100/VC140 style, several turbos)

Copy `config/facility_main_v4.4.yaml`, edit the channel map and add turbo branches:

```yaml
turbos:
  - id: turbo2
    label: "Turbo 2"
    control_mode: hipace_dsub25   # or bigred_dsub15
    gate_valve: gate2             # ids from the valves section
    turbo_valve: turbo2_valve
    gauge: pir_2                  # a gauge with role: turbo, turbo: turbo2
    hipace_dsub25: {speed_ai: Mod8/ai5, error_di: Mod2/port0/line16, error_di_inverted: true,
                    motor_do: Mod3/port0/line3, standby_do: Mod3/port0/line4, error_ack_do: Mod3/port0/line5}
```
Analog inputs use the terminal configuration set by `analog_input.terminal_config` (Main_V4.4 wires the
gauges **differentially** = the VI's DAQmx value 10106; options `differential | rse | nrse | pseudo_diff |
default`).  This must match the wiring — the wrong choice adds a per-channel voltage offset and the
pressures read wrong while the DAQ still "reads OK".

Optional readings that Main_V4.4's chamber does not have are added the same way: an `extra_analog`
entry (e.g. `{id: com_potential, label: "Com Potential (V)", channel: Mod1/ai6}`) shows a display-only
voltage box on the diagram and logs it to the CSV; a `compressor: {channel: Mod8/ai16}` block shows the
"Compressor Pressure (Bar)" box, logs it, adds the "Compressor air low" simulator fault and – with
`thresholds.compressor_min_bar` – raises error 5011.  Both are commented out in
`facility_main_v4.4.yaml`.

The controller, recognition, interlocks, CSV and the diagram all follow the config (one branch per
turbo; `in_use: false` parks a turbo like the VI's "Turbo X in use?" constant).  Start it with
`run.bat --config config/my_facility.yaml`.  `facility_vc100_template.yaml` shows the 3-turbo shape but
is *not* verified against the VC100 VI.

## Simulation

`hal/sim_backend.py` models chamber / turbo body / foreline pressures, valve actuation with reed
feedback, pump/chiller feedback, turbo spin-up/down and gauge voltages through the VI's own formulas.
The panel at the bottom injects faults (stuck valve, turbo error, chiller/primary fault, WRG fault, power
cut; "compressor air low" only for a facility with a `compressor:` block) and sets the plant speed-up.

## Differences from Main_V4.4 (deliberate, all optional)

* Optional non-blocking settle waits and dialogs (`blocking_waits: false`, see above) – off by default.
* CSV logging, zoomable plots, unit switch, Manual mode, config-driven facility layout.
* "Reset Turbos" (error acknowledge) also works in Auto mode.
* The VI appends "Shutt down due to error" every 100 ms while an error persists; here it is logged once.
* HiPace **RS485** control (Pfeiffer PV library) is not ported – both D-SUB modes are.

## Connecting to the real facility (bring-up)

The DAQ backend (`hal/nidaqmx_backend.py`) reproduces the VI's task layout and channel map, and is
covered by tests against a *fake* driver – it has **not yet run against the real cDAQ**.  Go in steps:

1. On the lab PC: NI-DAQmx driver installed, cDAQ visible in NI MAX as `cDAQ1` (or change
   `facility.daq_name`), then `%LOCALAPPDATA%\FacilityControl\venv\Scripts\pip install nidaqmx`.
   **Close LabVIEW** – DAQmx lets only one program own the lines.
2. `python tools\daq_check.py` – read-only: lists the devices, checks every channel of the profile
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
