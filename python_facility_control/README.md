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
run.bat --mode admin           # skip the "Select Control Mode" dialog (Auto | Manual | Admin)
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
  Not offered at start-up (the dialog asks Auto or Manual); reached from the panel's Admin Mode
  button when you need it for bring-up or to recover the facility by hand.
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

## Primary pump: two relays (small chamber, 2026-09-14; revised 2026-09-21)

> **The drive-frequency reader has an ON/OFF switch, and ships OFF (2026-09-21).**  Acquiring the
> drive output on `Mod1/ai6` put noise on the other analog readings, so the operator decides whether
> the sensor is connected: tick **"Pump frequency reader connected"** on the panel.  The switch is
> live – the channel has its own single-channel DAQ task, so it is opened and closed without ever
> touching the gauge task – and `frequency.enabled` in the profile sets the startup default.
>
> *Off* means the channel is **not acquired at all** (not merely ignored – that is the whole point),
> so the pump has no feedback: "Primary Pump is On" means the run relay is closed, **error 5000
> cannot fire** so a pump that fails to start is not detected, and the hour meter counts commanded
> hours.  That is the same position `Main_V4.4.vi` was always in.  *On* restores real rotation
> sensing, the spin-up check and a measured hour meter.
>
> **Current gaps: 15 s** from power to run, **5 minutes** from run off to power off.


The small chamber's primary pump is no longer one command line with a reed read-back.  Mains power
and the run command now go to **two separate relays**, and the pump reports its **drive frequency**
on an analog input:

| Signal | Channel | Note |
|---|---|---|
| Power relay | `Mod3/port0/line6` | mains power to the pump |
| Run relay | `Mod3/port0/line7` | start/run – no effect until the pump is powered |
| Drive frequency | `Mod1/ai6` | 0-10 V = 0-210 Hz (this input used to carry 'Com Potential') |

The old command line `Mod3/port0/line0` and the DI run read-back `Mod2/port0/line0` are retired.

Because the run relay does nothing on an unpowered pump, the controller **sequences** the two:

* start: power on → `timings.primary_power_to_run_gap_s` (**15 s**) → run on
* stop: run off → `timings.primary_run_to_power_off_gap_s` (**5 min**) → power off

The pump therefore keeps its mains power for five minutes after it is told to stop, while it coasts
down.  (2026-11-17: STOP and closing the window now run the normal shutdown, so they wait this gap
too.  Only a crash of the loop or `request_stop` still drops **both** relays at once.)

The control loop keeps reading during those gaps – they sequence two relays, they are not
cross-check settle windows – and the sequence also advances while a dialog is open.

Auto mode, Manual mode and the operator still deal with a single pump: the state machine only ever
sets the *demand* (`Commands.primary`) and the controller expands it into the two lines, so
`automode.py` and the VC100 profile are untouched.  On the diagram the pump is still one click.

**What "running" means.**  There is one definition, `model.primary_is_running`, shared by the
controller, the Manual-mode interlocks, the hour meter, the panel and the CSV so they cannot
disagree: the drive frequency above `running_above_hz` **when a reading is actually present**, else
the boolean run read-back, else **the run relay being closed**.  Note it keys off the reading, not
off the profile having a frequency block – that is what lets the reader be switched off mid-run
without the pump appearing to stop.  The last of those is deliberately not
"the demand": the demand is set during the power-up gap while the pump is definitely not turning.
While the pump is mid-sequence the panel says "Primary Pump powering up (15 s)" / "powering down
(300 s)" and the symbol is blue.

The operating-hour meter is shown in two places – beside the pump on the diagram ("Total run hours")
and in the mode panel – and persists in `logs/run_hours_<facility>.json` across restarts.

**New fault indication.**  `cross_check` is now meaningful on this chamber: if the run relay is
closed and the frequency stays below the running threshold for `timings.primary_spinup_timeout_s`
(**30 s**), the controller raises **error 5000** – "Primary Pump commanded to run but not turning
(0.0 Hz)" – which stops Auto mode like any other 5000-series error.  Main_V4.4 hard-wired this check
to False, so 5000 could never fire before.  A pump *coasting down* after the run command was removed
is normal and is never flagged.  The simulator's "Primary fault" injection reproduces the failure.

Any facility can use either wiring: give the pump block `cmd` (one relay, as the VC100 does) or
`power_cmd` + `run_cmd`, with an optional `frequency:` block.  The CSV gains `primary_power_cmd`,
`primary_run_cmd`, `primary_hz` and `primary_running` columns when they apply.

## Analog tasks: one per fast/high-impedance signal (fixed 2026-09-14)

Gauges share the main AI task; **each turbo speed channel and the pump frequency get their own
single-channel task**, as the VIs do.  This is not cosmetic: one multiplexed ADC serves a task's
channels in turn, and a signal that does not settle in the convert window returns the *previous*
channel's voltage.  With the turbo speed sharing the gauge task the facility showed a phantom ~69 %
turbo speed with the turbo stopped (the Turbo Convectron's 6.88 V at atmosphere), and after the pump
frequency was added ahead of it the phantom tracked the pump linearly to 100 %.  The LabVIEW panel
read 0 on the same wiring — see `docs/MAIN_V4.4_REFERENCE.md` section 6c.  A side benefit: the
per-turbo `speed_sample_rate_hz` / `speed_samples` settings finally take effect.

If you add another fast or high-impedance analog signal, give it its own task the same way rather
than appending it to the gauge scan.

## Auto-mode plant-safety rules (2026-09-22) — deliberate deviation from the VIs

An incident on the small chamber: at ~2e-5 mBar in Pumping to High Vac, **Shutdown** was pressed
(gate closed, turbo spinning down, as expected), then **Pump to High Vac** while it was still
slowing.  The VI's own logic — faithfully ported — restarted the motor, **closed the turbo valve**
and left the gate shut, so the rotor re-accelerated to full speed compressing into a dead volume;
it then routed into Pumping to Rough, which started the primary and **opened the bypass** into a
chamber at 1e-4 mBar, pushing foreline gas back in.  The operator had to switch to Admin.

Straight from `Main_V4.4.vi`, frame *Turbo slowing* (`docs/Main_V4.4.semantic.lvnet`):

```
AUTO.TURBO_VALVE_CMD = Select(f="True",  sel=Pump to High Vac 2, t="False")   -> valve CLOSED
AUTO.TURBO_MOTOR_CMD = Select(f="False", sel=Pump to High Vac 2, t="True")    -> motor ON
AUTO.GATE_CMD        = "False"
AUTO.CURRENT_STATE   = 1 (Pumping to Rough)
```

So this was a latent LabVIEW bug, not one the port introduced — and the same shape exists on the way
out of *Venting* and on the VC100 profile.  Three rules now prevent it.  Two are enforced **globally**
at the end of `AutoStateMachine.step()`, next to the existing high-pressure turbo shut-off, rather
than per button, because the hazard is a property of the plant and not of any one state:

* *(amended 2026-11-11 – see "A turning turbo never sees an un-roughed chamber" below: this rule
  now yields whenever keeping the valve open would put the chamber on the turbo's exhaust.)*
  **A spinning turbo keeps its valve open *and* the primary running.**  If a turbo is turning (above
  `turbo_slowing_threshold_pct`, the same test the state machine uses) or is commanded to run, its
  valve is held open and the primary pump is held on, whatever the state logic asked for.  Manual
  mode always refused to close the valve (`interlocks.py`, "closing while turbo runs would trap it");
  Auto now refuses it too.  **Both halves matter**: an open turbo valve with the primary stopped is
  worse than an isolated turbo, because the foreline backfills towards atmosphere and takes the
  turbo body with it — that is how the 2026-09-23 trip below happened.  Note this also keeps the
  primary running through an error shutdown until the turbo has slowed.
* **The bypass never opens into a chamber below the foreline.**  Opening it there backfills the
  chamber instead of pumping it.  This blocks *opening* only — a bypass that is already open is
  never forced shut, so an ordinary pump-down from atmosphere is untouched.
* **Pump to High Vac during Turbo slowing / Venting skips roughing when the chamber is still
  evacuated** (below the turbo-on threshold): it goes straight to Engage Turbo, keeping the turbo
  valve open and re-opening the gate, instead of running a roughing cycle the chamber does not need.

`tests/test_spinning_turbo_safety.py` replays the incident end to end and sweeps the whole spin-down
asserting the valve is never shut on a turning rotor, on both chamber profiles.

## A turning turbo never sees an un-roughed chamber (2026-11-11)

Reported from the VC100 and the small chamber: with the turbos spinning down (or still running after
**Vent**), the chamber at atmosphere, gates closed, turbo valves open and the primary backing them,
pressing **Pump to High Vac** or **Pump to Rough** opened the **bypass with the turbo valves still
open** — the exhaust of a turning turbo joined to a chamber at atmosphere.

*Cause.*  Not the VIs: both close the turbo valves first.  VC100 VI, turbo-valve tunnel of the Auto
case structure (`chambers/medium/analysis/auto_frames.txt`): frame 8 *Venting* and frame 7 *Turbo
slowing* output `[F,F,F]` whenever the next state is a different one; the command frame then waits
its settle time, and *Pumping to Rough* opens the bypass afterwards and keeps the valves shut until
rough vacuum.  Main_V4.4 has the same shape (`TV := speed > 15 %` "only while cur stays 8";
`TV := (tgt ≠ 1) ∧ (TV_prev ∨ done)`).  The port reproduced that until commit **a8f8052
(2026-09-22)**, whose global rule "a spinning turbo keeps its valve open" has no pressure condition
and re-opened the valves in the very iteration the state logic closed them.  Its bypass guard only
covers the opposite case (chamber *below* the foreline).

*Rule.*  **Never expose comes before never strand.**  Enforced at the end of
`AutoStateMachine.step()` for every state:

1. While the bypass is open, about to open, or not yet read shut, and the chamber is above the
   **exposure limit** (or its gauge is unreadable): every turbo valve with a *turning* turbo behind
   it is shut and that turbo's motor is switched off — it coasts isolated for the time the roughing
   takes — and the **bypass is held shut** until each of those valves was commanded shut in an
   earlier iteration (so it had its settle wait) *and* its reed contact reads shut.
2. The bypass never opens into a chamber below the foreline (unchanged).
3. Otherwise a turning turbo keeps its backing (the 2026-09-22 / 09-23 rule): a valve that is open
   stays open, the primary keeps running, and a valve that is *shut* is opened again only onto a
   foreline that is really pumped — primary running and the foreline below
   `turbo_valve_foreline_ok` (else the turbo-on threshold) or below the turbo gauge.

| | |
|---|---|
| *turning* | above `turbo_slow_speed_pct` (5 %), or the Rotating contact, or the motor commanded — deliberately lower than the 15 % / 50 % "still spinning" thresholds the states use |
| *exposure limit* | `thresholds.turbo_exposure_max_mbar`; default = wiggle room × turbo-on threshold, the pressure both VIs accept in Engage Turbo with bypass and turbo valves open (small chamber 1 mBar, VC100 1.25 mBar) |

Also, on the inlet side: in **Engage Turbo**, with a turbo already turning, the chamber pressure is
checked again immediately before a gate opens (the VIs check once, a few iterations earlier — or,
on the VC100's turbos-first path, a whole run-up to standby earlier); if it came up, the gates stay
shut and the chamber is roughed again.  **Manual** mode refuses to open the bypass, a turbo valve or
a gate when that would join a turning turbo to an un-roughed chamber; **Admin** has no interlocks by
design but the confirmation question carries a WARNING.

`tests/test_turbo_exposure.py` replays the report on both profiles (both buttons, coasting and at
speed) against the simulated plant — valve positions and pressures, not just commands.

## Safety tests for Auto mode (2026-11-11)

`python -m pytest tests -k safety` drives both facility profiles through every Auto button in every
situation, hours of random operator sequences, every fault the simulator can express and Admin -> Auto
hand-overs, on a deterministic plant, with a monitor that watches valve positions, pressures and
rotor speeds for 17 safety rules (a turning turbo never sees an un-roughed chamber at inlet or
exhaust, never runs without backing or cooling, the vent never opens onto an open gate, ...).
Run against the code from before the 2026-11-11 fix, the sweep fails in 9 cells and the random
operator in 11 of 24 runs.  The rules, how to run them (`SAFETY_LEVEL=quick|normal|full`), how to
replay a failure and what they found are in `docs/SAFETY_TESTS.md`.

## Protections added after the safety tests (2026-11-12)

The safety tests found eight unsafe behaviours, and a ninth while the fixes were being checked (table
in `docs/SAFETY_TESTS.md`).  Six are fixed in the engine, one by a profile switch, the dead pressure
gauge since 2026-11-16 (below), and one stays open by decision (small chamber: a stopped primary
pump with no turbo turning):

* **Cooling follows the turbos.**  While any turbo is above the "still spinning" speed the chiller
  stays on, in every state - Facility Off after an error included (it used to stop at once).
* **A turbo valve only opens onto a turning turbo when the foreline is pumped** (primary running and
  foreline below `turbo_valve_foreline_ok` / the turbo-on threshold, or below the turbo gauge).  Covers
  a program restart with turbos still coasting.
* **Backing lost -> isolate, error 5012.**  Primary pump not running, or foreline above
  `turbo_foreline_max_mbar` (3 mBar on both chambers) with the bypass shut, while a turbo turns
  behind an open turbo valve: the valve and the gate shut, the motor stops, Auto stops.  This is also how the small chamber notices a dead
  primary pump without the frequency reader.
* **Pressure rise in Pumping to High Vac -> gates closed, error 5013.**  Chamber above
  `high_vac_pressure_trip_mbar` (1e-1 mBar on both chambers; normal work stays below 1e-2 mBar) for `pressure_trip_delay_s`.  The level counts once the chamber has pumped down below
  half of it after the gates opened; until then the exposure limit (1 / 1.25 mBar) is the trip level.
  Auto stops; the turbos coast down with the primary pump and the chiller on.
* Small chamber: `shut_off_spins_down_first: true` ("Shut Off" while roughing goes through Turbo
  slowing).
* **A turbo is never driven behind a shut turbo valve.**  Whatever the other rules decide about the
  valve, the motor follows it (found by the random-operator test while checking the fixes above).
* **A backing-lost trip shuts the gate with the turbo valve**, in one command frame (found on the
  plant measured from the logs: a chamber leak can push the foreline over its limit first).

New error codes: 5012 backing lost, 5013 chamber pressure rose in high vac, 5014 gauge dead or frozen (2026-11-16, below).
**The thresholds were set on the simulator** - `docs/SAFETY_TESTS.md` lists what to check on each
facility before relying on them.

## What the facility logs showed (2026-11-12)

The CSV logs of 23 Oct - 11 Nov were read row by row (`docs/PLANT_FROM_LOGS.md`).

* They contain the three things that went wrong on the VC100 - 28 Oct (Turbo 2 and 5 from 98 % to
  18 % in 5 s), 5 Nov (four HiPace turbos from 78 % to 5 % in 10 s) and 9 Nov (gates closed again,
  turbos left in standby, three times).  All three are fixed; `tests/test_safety_incidents.py`
  replays each one.
* The simulator now uses the measured plant: roughing, venting, run-up and coast-down times, a
  foreline that is at atmosphere within seconds of the primary stopping, a rotor that air stops in
  seconds, turbo controllers that only warn.  Each profile's `simulation:` block marks those numbers
  `[log]`.
* The new trip levels are clear of everything logged: foreline at most 3.9e-1 mBar with the turbos
  running (trip 3 mBar), chamber at most 1.2e-4 mBar once pumped down (trip 1e-1 mBar).

`python tools/audit_logs.py logs/facility_vc100_*.csv` runs the safety rules over any log and lists
what it finds (exit code 1 if a turbo was exposed or crash-braked).

## Starting the facilities (test engineers)

Double-click, in the repository root:

* `Run_Small_Chamber.bat` - small chamber on its real hardware (cDAQ1)
* `Run_Medium_Chamber_VC100.bat` - VC100 on its real hardware (cDAQ3 + RS-485 turbos)

Neither ever starts the simulator: without the NI-DAQmx driver they stop with a message.  They check
that LabVIEW is closed and ask once before starting.

## Speed-drop trip, slow chamber-pressure trip, safe program exit (2026-11-17)

Three additions.  Neither VI had any of them.  All settings are in the profiles.

### 1. Turbo speed-drop trip - error 5015

**In plain words:** the program watches the speed of every turbo that reports one.  A turbo never
slows down quickly by itself - a HiPace that is switched off loses 2 % a *minute*.  If a turbo
loses **5 % of its full speed within one second**, and it is still down a second later, something
has hit the rotor with gas, whatever the gauges say.  The program then closes **every gate valve and
every turbo valve**, stops **every turbo motor**, keeps the chiller running, shows a message and
raises **error 5015**.  It takes about two seconds from the start of the fall.  The turbos stay
shut off from both sides until someone presses *Clear Error*.

Why: on 28 Oct two turbos went from 98 % to 18 % in 5 s, on 5 Nov four went from 78 % to 5 % in
under 10 s.  The pumps reported a warning, not an error, so nothing reacted.  The causes of both are
fixed; this is for the next cause nobody has thought of.  It reacts to the fall itself, so it is
too late for the first seconds - it stops the gas, stops the motors driving against it, and tells
the operator.

**It does not fire when a slow-down is expected.**  For 15 s after the program itself does one of
these to a turbo, the 5 %-in-1-s rule is switched off for that turbo:

| the program ... | what the logs show the rotor does then |
|---|---|
| switches its motor off (Shutdown, Vent and shutdown, any error) | a HiPace loses at most 0.6 % in the first 5 s and 2.1 % in 30 s - it coasts, it does not brake |
| puts it in standby, or takes it out (VC100: "Vent" with the turbos running) | not in the logs from full speed - to be watched the first time it is done on the facility |
| opens its gate onto it while it turns (Pump to High Vac with the turbos still coasting) | at most 2.8 % in 5 s, 7 % in 30 s, with 1.5e-1 mBar in the chamber |
| switches the chiller off (end of a shutdown, turbos below 15 %) | where the chiller contactor feeds the turbo controller the speed reading goes with it |

During those 15 s a second, coarse rule still counts: **30 % of full speed within 5 s**.  None of
the above comes near it, and both incidents exceed it.  It is always on.

Also:

* The big Shimadzu turbos (VC100 Turbo 1 and 4), which brake actively when switched off (1 % in
  5 s), report no speed - they cannot trip it.  Their own alarm contact already stops Auto.
* Small chamber: its speed signal reads 96 % the moment the motor starts and falls to 17 % in a
  minute, and what it does at a stop has not been logged.  Nothing at all is judged for 120 s after
  its motor is switched on or off (`turbo_speed_drop_blank_after_switch_s`).
* Below the "still spinning" speed (15 %) the 5 % rule does not apply.
* One bad reading is not a trip: the fall must still be there 1 s later.  A speed signal that goes
  to zero and STAYS there is (a broken signal wire on the small chamber therefore stops the run;
  an RS-485 pump that stops answering keeps its last speed and is not judged).
* All turbos are isolated, not only the one that fell: they share one foreline, and both incidents
  hit more than one.  After *Clear Error* a turbo valve re-opens only onto a foreline that is pumped.
* Manual and Admin mode: one line in the log ("... No automatic action in Admin mode").
* While the loop stands still in a settle wait (up to 9 s after a gate moves, as in the VI) nothing
  is read.  The 5 % rule does not compare across such a wait; the 30 % rule does.

What it would have done on the logged days (rows are 5 s apart, so "5 % in 1 s" can only be
bounded): nothing normal lost 5 % even within **10 s**.  The only falls that large are

| what | speed lost | trips? |
|---|---|---|
| 28 Oct, Turbo 2 and 5 | about 80 % in 5 s (16 % a second) | yes |
| 5 Nov, Turbo 2, 5, 6 | 45-65 % in 5 s (9-13 % a second) | yes |
| 5 Nov, Turbo 3 | 26 % in 5 s (5.2 % a second), then 47 % | yes |
| small chamber, speed signal after every motor start | 7.4 % in 5 s (1.5 % a second) | no (and blanked) |
| VC100, gate opened BY HAND in Admin onto a turbo at full speed, 0.8 mBar in the chamber (28 Oct, twice) | 5.1 % and 5.6 % in 5-10 s | no (and Admin only logs) |

Settings: `thresholds.turbo_speed_drop_pct` (5) within `timings.turbo_speed_drop_window_s` (1);
`thresholds.turbo_speed_crash_pct` (30) within `timings.turbo_speed_crash_window_s` (5);
`timings.turbo_speed_drop_confirm_s` (1), `turbo_speed_drop_expected_s` (15),
`turbo_speed_drop_blank_after_switch_s` (small 120, VC100 0).  `null` switches a rule off.
The first version of this trip (the same day) was the 30 % rule alone; 5 % in 1 s is the test
engineer's figure.

Tests: `tests/test_turbo_speed_drop.py`.

### 2. A second, slower chamber-pressure trip in Pumping to High Vac - error 5013

The trip of 2026-11-12 closes the gates when the chamber is above 1e-1 mBar for 2 s.  In normal
work the chamber stays below 1e-2 mBar, and nothing reacted in between: a leak that *settles* at a
few 1e-2 mBar left the turbos pumping it at full speed.

Now: chamber above `thresholds.high_vac_pressure_slow_trip_mbar` (**1e-2 mBar**) for
`timings.pressure_slow_trip_delay_s` (**30 s**) -> the same reaction: gates closed, error 5013, Auto
stops, the turbos slow down with primary pump and chiller on.  30 s so that a gauge misreading while
it changes range, or a short burst, passes.  Like the first trip it only counts once the chamber
has been below half of it (the gates open at 1.1-2.4e-1 mBar; the small chamber needs minutes to get
below 1e-2).  `null` = off.

Against the logs: once below 1e-3 mBar the chamber never came back above 1e-3 mBar in any logged
run, on either chamber - a factor of ten under the level, before the 30 s.
Tests: `tests/test_high_vac_slow_trip.py`.

### 3. Closing the program shuts the facility down first

The VI ('Force stop on exit'), and this port until now, switched everything off in ONE step when
the program closed - gates, turbo valves, primary pump, chiller, turbo motors - whatever the turbos
were doing, and without asking when the window was closed with [x].  At full speed that drops the
cooling with the rotors at 100 %.  (On the simulated plant the turbo valves do shut before the
foreline has come up, so no gas reaches the turbos; the cooling is what is lost.)

Now **STOP and the window's [x] are the same thing**:

1. The program asks ("Shut down and close" / "Cancel").
2. It goes to Auto mode, Facility Off - from whatever mode and state it was in.  This is exactly
   what an error does: gates closed, turbo motors off, vent and bypass closed; a turbo that is still
   turning keeps its turbo valve, the primary pump and the chiller until it has slowed down (the
   profile's "still spinning" speeds: backing to 15 % / 50 %, cooling to 15 %).
3. The panel shows what it is waiting for, in red: "Closing the program – waiting for the turbos to
   slow down (Turbo 2 63 %, ...)".  Auto buttons and mode changes are ignored.
4. When everything is off the program closes by itself.  From full speed that takes as long as the
   turbos need: about 37 min on the VC100 (HiPace, on the plant model fitted to its logs); on the
   small chamber the turbo's coast-down (not measured yet) plus the primary pump's 5 min power-down
   gap.
5. Closing again while it runs offers "Keep waiting" or "Cancel – keep the program open".  There is
   no way to close the program before the shutdown has finished.

Every other way out - the control loop crashing, `Controller.request_stop()` (tests, `--headless`) -
cannot wait for the turbos.  It closes the valves and stops the motors first, waits the valve
travel, stops the primary pump after that, and **leaves the chiller running if a turbo is still
turning** (the log says so).  With nothing turning it switches everything off as before.

Not covered: the PC losing power, Windows killing the process, the program started again while
turbos still turn (start-up still writes everything off in one step, as the VI does).

Tests: `tests/test_safe_exit.py` (closing from every Auto situation and from Admin / Manual
line-ups under the safety monitor, the loop thread, the panel).

## A dead or frozen pressure gauge stops the facility (2026-11-16)

Until now a gauge that had died was simply believed - a dead foreline gauge reads as a good vacuum.
Two rules, on the chamber gauge, the foreline gauge and the gauge of every turbo in use:

* **dead** - its output is below `thresholds.gauge_dead_below_volts` (1 V) for
  `timings.gauge_fault_delay_s` (3 s).  A dead or unplugged gauge sends about 0 V; in the logs no
  gauge on either chamber ever read below 2.7 V, from atmosphere to base pressure.
* **frozen** - `timings.gauge_frozen_readings` (100) readings in a row are exactly equal, to the
  last digit of the measured voltage.  A live gauge always has noise (in the logs never more than 10
  equal rows running).  Not judged within 0.1 V of the ends of the input range, where a clipped
  signal repeats legitimately.

Either one: a pop-up naming the gauge, **error 5014**, and Auto shuts down as for any error - valves
closed, turbos stopped but kept backed and cooled while they slow.  The error clears by itself when
the gauge reads properly again; nothing restarts by itself.  `null` / `0` switches a rule off; one
gauge can have its own floor (`gauges[].dead_below_volts`).  `tools/audit_logs.py` reports a gauge
below the floor as GAUGE.  Tests: `tests/test_gauge_health.py`, KI-7 in
`tests/test_safety_known_issues.py`.

## Engage Turbo on the VC100: each turbo starts with its own gate (2026-11-13)

The gates open one at a time (shared air line, about 10 s each) and the turbos used to start only
when the sixth was open.  Asked: would it be quicker to start the turbos first and open the gates
afterwards?  Measured on the plant model fitted to the logs, from the first gate command:

| order | chamber at 1e-3 mBar | at 1e-5 mBar | all six at full speed |
|---|---|---|---|
| gates, then turbos (until now) | 126 s | 181 s | 432 s |
| turbos to standby, then gates (as asked) | 292 s | 302 s | 395 s |
| **each turbo starts when its own gate is open (now)** | **102 s** | **152 s** | **412 s** |

Turbos first is slower to vacuum - the chamber waits at 1.7e-1 mBar while the turbos run up behind
closed gates - and it opens the gates onto rotors at about 70 % with 1e-1 mBar still in the chamber.
The logs show what gas on a turbo at speed does: 28 Oct (Admin), a gate opened onto Turbo 2 at 100 %
with 2.4e-1 mBar in the chamber put 11 mBar on its exhaust and cost it 8 % of its speed; 9 Nov,
gates opened onto turbos coasting at 51-57 % at 1.5e-1 mBar cost them 5-7 %.  (Below 1e-2 mBar the
turbos-first order of 2026-10-28 is still used, unchanged.)

`auto_mode.start_turbo_with_its_gate: true` (VC100 profile) takes the waiting out instead: a turbo
starts as soon as its own gate has been commanded open, had its settle time and reads open.  It
still starts from rest with its gate open, as before; a gate that does not open leaves its turbo
stopped.  `false` gives the old behaviour.  Tests: `tests/test_engage_start_with_gate.py`.

## Engage Turbo: the order is decided once, and the wait is shown (2026-11-11)

Reported from the VC100: now and then Pump to High Vac ended with **every gate shut and every turbo
in standby**, no error, nothing on the panel; the operator went to Admin, opened the gates and set
the turbos to full speed by hand.

That state is the *turbos-first* order of 2026-10-28 (`thresholds.turbo_start_before_gates_below_mbar`,
VC100 1e-2 mBar): below it Engage Turbo starts the turbos behind their closed gates — which on the
VC100 means standby (`standby_when_gate_closed`) — waits until each is at standby speed (at most
`timings.turbo_standby_timeout_s`, 10 min) and only then opens the gates one by one.  Two defects:

* **The choice was re-made on every pass of the gate step.**  In the normal gates-first order the
  gates open one at a time (`gate_open_stagger_s` + the 9 s gate settle each), so the step runs for a
  minute or more while the chamber keeps falling through the stopped turbos.  If the reading crossed
  the limit part-way, Auto switched order: it **closed the gates it had just opened** and started all
  six turbos in standby behind them.  The order is now decided the first time the step runs and
  kept until Engage Turbo is left (`AutoMemory.engage_order`); gates never close again part-way.
* **Nothing showed the wait.**  The panel said "substate 6" for up to ten minutes.  It now shows
  `Waiting for turbos to reach standby speed (4 of 6, 03:12) – gates stay closed until then, or until
  10:00.  Still running up: Turbo 1, Turbo 4`, then `Opening the gate valves one at a time (3 of 6)`.

Unchanged, and still open: *which* pressure decides.  It is the lower of the reading now and the
reading when Pump to High Vac was pressed, so a request made at 6e-3 mBar goes turbos-first even if
opening the turbo valves has lifted the chamber to 0.2 mBar by the time the turbos start.  Set the
threshold to `null` for the VIs' order every time.  `tests/test_engage_order.py`.

## Overnight Pump keeps backing the turbo (2026-09-23)

A second incident, and a direct consequence of the rule above being written as only half a rule.
**Overnight Pump** was pressed from high vacuum.  `Main_V4.4.vi`'s Overnight frame switches
*everything* off at once — including the primary pump — while the turbo is still near full speed;
only the chiller stays on, above `overnight_chiller_speed_pct`.  The turbo-valve rule then held that
valve open onto a foreline with nothing pumping it, so the foreline backfilled towards atmosphere
(~5e+02 mBar) and dragged the turbo body up with it (~27 mBar) until the drive tripped — **error
5007**, reproduced exactly in the simulator.

Two changes:

* the global rule now holds the **primary pump on** as well as the turbo valve open, so a turning
  turbo is always genuinely backed rather than merely connected to something;
* `auto_mode.overnight_backing_while_spinning: true` on the small-chamber profile — the behaviour the
  **VC100 VI always had**: primary, chiller and the spinning turbo's valve stay on until the turbo is
  below the slowing threshold, then everything goes off.

Foreline now holds at ~4e-03 mBar through the whole spin-down instead of reaching atmosphere, and the
turbo body at ~1e-03 mBar instead of 27 mBar.

## Changes requested by the test engineer (2026-09-08)

These deviate from the VI on purpose and are all driven by the facility profile:

* **Pumping to Rough start order depends on the chamber pressure.**  Above
  `thresholds.bypass_first_above_torr` (**5e1 mBar**) the bypass valve is opened **first** and the
  primary pump starts after it; below it the **primary pump starts first** and the bypass follows
  after `timings.primary_to_bypass_gap_s` (**25 s**, the requested 20-30 s).  The VI always started
  the pump immediately.
* **"Is the manual vent valve closed?"** – Pump to Rough / Pump to High Vac / Overnight Pump ask this
  first and only run on *Yes* (the manual vent valve is a hand valve, not on the DAQ).
  **Since 2026-09-23 it is only asked at or above `thresholds.vent_confirm_above_mbar` (5e2 mBar).**
  Below that the chamber is plainly already pumped down, which is itself proof the hand valve is
  shut, so the dialog would just be a click in the way.  An unreadable or implausible chamber gauge
  always asks, because then the pressure tells us nothing.  Set the key to `null` to ask every time;
  it is unset on the VC100 profile, which therefore still asks on every press.
* **Overnight Pump below `thresholds.overnight_skip_below_torr` (2e-1 mBar)** skips roughing entirely:
  no primary pump, no bypass, straight into the overnight hold.
* **Primary pump total operating hours** – a maintenance meter on the panel, counted from the pump's
  read-back and kept in `logs/run_hours_<facility>.json` so it survives restarts (also a CSV column).

## Differences from Main_V4.4 (deliberate, all optional)

* Optional non-blocking settle waits and dialogs (`blocking_waits: false`, see above) – off by default.
* CSV logging, zoomable plots, unit switch, Manual mode, config-driven facility layout.
* "Reset Turbos" (error acknowledge) also works in Auto mode.
* The VI appends "Shutt down due to error" every 100 ms while an error persists; here it is logged once.
* HiPace turbos can be driven over **RS-485** (`control_mode: hipace_rs485`, VC100 Turbos 2 and 3 since
  2026-10-09) – see the section below.  The D-SUB modes and the contact interface are unchanged.
* Auto mode stops on error codes 5000–5011 (the VIs: 5000–5010; 5011 is the compressor error, which the
  VC100 VI reports under the gate code 5010).

## VC100: six turbos (2026-10-13)

The medium chamber now has six turbo branches.  `config/facility_vc100.yaml` carries them all:

| Turbo | Pump | Control | Gate valve (cmd / read) | Backed through |
|---|---|---|---|---|
| Turbo 1 | Shimadzu | 6 status contacts (the VI) | Mod1 line5 / Mod2 line5 | Turbo 1 valve |
| Turbo 2, 3 | Pfeiffer HiPace | RS-485 | Mod1 line6, 7 / Mod2 line6, 7 | Turbo 2, 3 valve |
| **Turbo 4** | Shimadzu | **HD15 remote**, new NI-9472 in **slot 5** | **Mod5 line4 / Mod2 line22** | Turbo 1 valve (shared) |
| **Turbo 5** | Pfeiffer HiPace | RS-485 | **Mod5 line3 / Mod2 line23** | Turbo 2 valve (shared) |
| **Turbo 6** | Pfeiffer HiPace | RS-485 | **Mod5 line5 / Mod2 line24** | Turbo 3 valve (shared) |

**Turbo 4 (Shimadzu HD15 remote).**  Outputs: Mod5 line0 = Reset (pulsed by *Reset Turbos*), line1 =
Standby, line2 = Start/Stop.  Status inputs: Mod2 line18 = speed above 80 %, line19 = accelerating *or*
decelerating, line20 = Alarm, line21 = Warning – **24 V = alarm / warning** (not inverted, unlike Turbo 1's
normally-closed contacts).  The program maps these onto the six contacts Turbo 1 has
(`model.normalize_contacts`): the motor command tells acceleration from deceleration, "speed reached"
needs the > 80 % contact *and* not decelerating, and "still spinning" is any of the three.  Alarm and
Warning raise error 5005 "Turbo 4 Alarm/Warning" (`warning_is_error: false` makes the warning display-only).

**Shared turbo valves.**  Turbos 4/5/6 have no turbo valve or gauge of their own; each uses Turbo 1/2/3's
(`turbo_valve:` / `gauge:` in the turbo block).  The diagram draws the turbos that share a valve side by side,
joined above that one turbo valve and its one backing gauge (Turbo 1 + 4, Turbo 2 + 5, Turbo 3 + 6).  Auto mode holds a
shared valve open while *any* turbo behind it is turning, Manual mode refuses to close it while any of
them runs, and a turbo set `in_use: false` never closes the valve the other one still needs.

DAQ use with this profile: **48 lines** (19 DO, 23 DI, 6 AI), none used twice – `tools/daq_check.py`
lists them.

## HiPace turbos over RS-485 in the facility program (VC100, 2026-10-09)

Turbos 2 and 3 on the VC100 now run over RS-485 instead of their D-SUB lines, and the profile has room
for **four HiPace** on the RS-485 link (a commented Turbo 4 / Turbo 5 template sits after Turbo 3 in
`config/facility_vc100.yaml`).  A turbo block selects it with `control_mode: hipace_rs485`:

```yaml
    control_mode: hipace_rs485
    hipace_rs485:
      port: COM6          # turbos with the same port share one RS-485 segment ...
      address: 2          # ... each with its own [P:797] address; one port per turbo also works
```

* **No DAQ lines** for such a turbo: motor ([P:010]), standby ([P:002]), Reset Turbos ([P:009]), speed
  ([P:309] / [P:315]) and the error code ([P:303]) all go over the serial link.  The D-SUB blocks are
  kept in the profile – `control_mode: hipace_dsub25` goes back to them.
* One poller thread per COM port (`hal/hipace_rs485_turbos.py`).  At 9600 baud four pumps take about
  a second per cycle, so the 100 ms control loop reads cached values and never waits on the bus.  A
  command is sent before the next poll; a write that is lost to a timeout is resent.
* Start-up takes RS-485 control (`[P:060] = 2`, `take_control: true`) and then commands everything off,
  as for the D-SUB modes.  Exit sends the stop over RS-485 before the port is released; `[P:060]` is
  left on RS-485 unless `restore_interface_on_exit: true`.
* Errors (stop Auto mode with the turbo's `error_code`, 5007 on the VC100): `[P:303]` = `Err…`; **no
  reply for `comm_timeout_s` (3 s)**; a control write refused (`_LOGIC`: RS-485 does not have control,
  or the D-Sub remote-priority input is active); a COM port that cannot be opened (the program still
  starts; the port is retried every 5 s).  `Wrn…` is shown as *Turbo Warning* and does not stop Auto
  unless `warning_is_error: true`.  When the link is lost the **last known speed is kept**, so the
  plant-safety rules keep backing a turbo that may still be spinning.
* `[P:010]` read-back is cross-checked against the command (error 5005) once `motor_confirm_s` (5 s)
  has passed since the command – the poller needs about a second to confirm it.
* `tools/daq_check.py` polls the RS-485 turbos read-only (nothing is written, control is not taken).
* **Close the bring-up panel before starting the facility program** – only one program can own a COM
  port.  `pyserial` is now a requirement (`requirements.txt`).
* The simulator (`--sim`) treats RS-485 turbos like any other turbo with a speed signal; the protocol
  code itself is tested against the TC 400 simulator (`tests/test_hipace_rs485_facility.py`, including a
  four-HiPace Auto pump-down).  `SIM4` is a simulated bus with four pumps at addresses 1–4.

The protocol core moved from `tools/hipace_rs485.py` to `facility_control/drivers/pfeiffer_pv.py`
unchanged; the tool re-exports it, so its command line, GUI and tests work as before.

## HiPace RS-485 test panel (VC100 Turbos 2 and 3)

A standalone tool, independent of the facility program, for bringing up the Pfeiffer HiPace / TC 400
remote control over RS-485 (Pfeiffer Vacuum protocol, 9600 8N1) before the HAL uses it.

```
tools\hipace_rs485.bat --sim                     # try it on the simulator (any PC)
tools\hipace_rs485.bat                           # lab PC: pick the COM port(s) in the window
python tools\hipace_rs485.py --port COM6 scan    # command-line equivalents: scan / dump / watch /
python tools\hipace_rs485.py --port COM6 --address 1 validate   # validate / read / control
```

* One shared bus (same COM port, two addresses) or one COM port per pump.  Both TC 400 ship at
  address 1: connect one pump alone and change it ("Change RS-485 address…", [P:797]) first.
* Monitoring is read-only.  Writes need **Enable control**; **STOP** is always allowed.
  *Take RS-485 control* writes [P:060] = 2; on exit the tool stops the pumps it started and puts
  [P:060] back to the value it found (unless you choose to leave them).
* **Validation** tab: link, identity, address, every monitoring parameter, link quality over N
  telegrams, error code / history, temperatures, control interface, D-Sub remote priority, and an
  optional harmless write round trip.  *Record CSV* logs every poll to `logs/hipace_rs485_<date>.csv`.
* Close the tool before the facility program uses the COM port.  The safety notes are at the top of
  `tools/hipace_rs485.py`.

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
