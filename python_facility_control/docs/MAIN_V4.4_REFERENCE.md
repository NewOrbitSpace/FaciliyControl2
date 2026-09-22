# Main_V4.4.vi — reverse-engineered functional reference

Source: `Main_V4.4.vi` (LabVIEW 24.0, 792 nodes / 2943 wires / 107 structures), read via
`lvkit` (`describe --format lvnet`) with local-variable names corrected to front-panel-heap
order and every flat-sequence tunnel resolved (see `Main_V4.4.semantic.lvnet`).
This is the specification the Python port (`facility_control`) is built against.

The VI controls a **single-turbo** facility: 1 primary pump, 1 chiller, 1 turbo pump,
4 valves (Turbo, Bypass, Vent, Gate), 3 pressure gauges (WRG main chamber, Convectron 1
"Turbo", Convectron 2 "Foreline"), plus an optional Agilent U1250 DMM ("Com Potential").
Pressures are computed and compared in **Torr**.

---

## 1. Program structure

```
[Init]  create DAQmx tasks, all Sys_Cmd locals := False, DAQ_Name := 'cDAQ1',
        Tubro_Control_Mode := BigRed_Turbo_DSUB_15_Control (2), mode := Initialize
   │
   ├── Main while-loop (stops on `stop`), each iteration = flat sequence of 6 frames:
   │     0  READ      all inputs (DI, AI, turbo), convert gauges
   │     1  STATUS    cmd/read cross-checks → error cluster, status text + colours
   │     2  CLEAR     "Clear error?" → clear error cluster; wait 10 ms
   │     3  DECIDE    mode case: Initialize / Admin / Manual(dead) / Auto state machine
   │     4  COMMAND   write DO/turbo commands; settle waits; Sys_Cmd locals := new cmds
   │     5  LOG       append "Opened/Closed X at hh:mm:ss" lines; Wait-until-next-ms 100 ms
   │     Running LED toggles every iteration.
   │
   ├── Graph while-loop (period 1/Sample Freq = 100 ms): Waveform chart with 9 plots
   │     0 Main Gauge, 1 Foreline Convectron, 2 Turbo Convectron, 3 Primary Pump,
   │     4 Turbo Pump, 5 Turbo Valve, 6 Bypass Valve, 7 Gate Valve, 8 Vent Valve
   │     (booleans plotted as 0/1). Time base from a dummy AI read on cDAQ2Mod1/ai7.
   │
   └── [Exit]  "Force stop on exit" = True: write False to every DO (valves, primary,
               chiller, turbo motor/standby/ack), stop+clear tasks, close DMM / RS485.
```
There is **no CSV file logging** in Main_V4.4 (the VC100 VI adds it; the Python port
implements it as an option so the documented VC100 behaviour is available).

Loop shift registers (state carried between iterations): error cluster, Running toggle,
Primary/Chiller/TurboValve/Vent/Bypass/TurboMotor/Gate/TurboStandby command, temp-log
string, MODE (Facility_State), CURRENT state, TARGET state, SUBSTATE (I32).

## 2. Channel map (DAQ_Name = `cDAQ1`; alternative `cDAQ2` if `Real_DAQ?`=False)

| Signal | Dir | Physical channel | Notes |
|---|---|---|---|
| Valve_Turbo_Read | DI | Mod2/port0/line2 | reed switch, 24 V = open |
| Valve_Bypass_Read | DI | Mod2/port0/line3 | |
| Valve_Vent_Read | DI | Mod2/port0/line4 | |
| Valve_Gate_Read | DI | Mod2/port0/line5 | |
| Primary_Pump_Read | DI | Mod2/port0/line0 | Invert Lines = False |
| Chiller_Read | DI | Mod2/port0/line1 | |
| Valve_Turbo_Cmd | DO | Mod4/port0/line0 | 24 V = open |
| Valve_Bypass_Cmd | DO | Mod4/port0/line1 | |
| Valve_Vent_Cmd | DO | Mod4/port0/line2 | |
| Valve_Gate_Cmd | DO | Mod4/port0/line3 | |
| Primary_Pump_Cmd | DO | Mod3/port0/line0 | |
| Chiller_Cmd | DO | Mod3/port0/line1 | |
| Wide Range_Gauge_Read | AI | Mod1/ai5 | 0–10 V, mean of N samples |
| Convectron_1_Read ("Turbo Convectron") | AI | Mod1/ai1 | 0–10 V |
| Convectron_2_Read ("Foreline Convectron") | AI | Mod1/ai2 | 0–10 V |
| Com Potential | AI | Mod1/ai6 | 0–10 V, displayed only |
| **BigRed turbo (active mode = 2)** | | | |
| BRT_Turbo_Speed_Read | AI | Mod1/ai4 | ±10 V, × 10 → % |
| BRT_Error_Read | DI | Mod2/port0/line7 | True = error (not inverted) |
| BRT_Still_Spinning_Read | DI | Mod2/port0/line8 | → "Still Spinning?" indicator |
| BR Turbo Motor Cmd | DO | Mod3/port0/line4 | |
| BR Turbo Motor Standby Cmd | DO | Mod3/port0/line5 | |
| **HiPace 700 D-SUB mode (0)** | | | |
| Turbo_Speed_Read | AI | Mod1/ai3 | 0–10 V × 10 → %, 4000 Hz |
| HP700_Error_Read | DI | Mod2/port0/line6 | **24 V = healthy** → error = NOT(line) |
| HP700 Motor Cmd | DO | Mod3/port0/line2 | |
| Turbo Error Acknowledged Cmd | DO | Mod3/port0/line3 | **pulse: True, wait 1000 ms, False** |
| **HiPace RS485 mode (1)** | serial | COM4, address 2 | Pfeiffer PV lib; motor on/off, error reset, speed (rpm→%: rpm·0.016667/820·100), temps |

Main AI task: sample clock finite, `rate` (1000 Hz) × `samples per channel` (200) each
iteration, mean per channel. In DSUB HP700 mode the speed channel is read at 4000 Hz / 2000 samples.
Turbo motor "read" has **no hardware feedback in either D-SUB mode** — it is the previous
motor command (`Turbo_Motor_Sys_Cmd` local variable). Only RS485 provides a real read.

## 3. Gauge conversions (formula nodes)

* Convectron 1 and 2: `P_Torr = 10**V * 1e-4`
* Ion gauge / WRG:    `P_Torr = 10**(1.667*V - 11.46)`
* Turbo speed %: `V * 10` (D-SUB), `rpm*0.016667/820*100` (RS485)

"Use fake values?" (default True) actually drives the local variable `Use_real_Values?`;
when True the DAQ readings are used, otherwise the `Fake_*` front-panel controls.

## 4. Status / error evaluation (frame 1)

Cross-check XOR(previous command, read) for every valve → LabVIEW error codes:
5001 turbo valve, 5002 bypass, 5003 vent, 5010 gate ("Conflict between X command and read!").
Chiller: XOR(cmd, read) → 5004; status text "Chiller is Off/On/error".
Primary: cross-check **disabled** (selector wired to constant False) — status only follows
the command: "Primary Pump is Off/On"; code 5000 exists but can never fire.
Turbo error: `error_read AND chiller_cmd` ("ignore turbo error if chiller off") →
5006 "HP2300 Error. See Device LEDs for more info" (D-SUB) / 5007 "BRT Error…" (BigRed) /
5005 "Conflict between Turbo Motor command and read!" + 5006 with code string (RS485).
All read errors from DAQmx are merged in as well.

Turbo status enum and colours (LabVIEW BGR-packed U32 → RGB):
| # | Text | Condition | Colour |
|---|---|---|---|
| 0 | Turbo Off | motor read False and speed ≤ 5 % | grey `#C0C0C0` (12632256) |
| 1 | Turbo Spinning Up | motor read True, speed ≤ 95 %, not standby | blue `#339AFF` (3381759) |
| 2 | Turbo Speed Reached | motor read True, speed > 95 % | green `#64FF00` (6618880) |
| 3 | Turbo Spinning Down | motor read False, speed > 5 % | purple `#A286AB` (10650795) |
| 4 | Turbo In Standby | motor read True and standby cmd | yellow `#FFB612` (16757266) |
| 5 | Turbo Error | turbo error (see above) | red `#FF0000` (16711680) |
Primary/Chiller colours: Off grey, On green, Error red (same values). Valve indicators
show the **read** state; "X Valve Error?" LEDs show the XOR.

Clear error (frame 2): "Clear error?" button → Clear Errors.vi (resets status/code/source).
An un-rectified fault re-appears next iteration.

## 5. Modes (Facility_State enum: 0 Intialize, 1 Admin, 2 Manual, 3 Auto)

`change_to_admin_mode` button forces MODE := Admin at any time.

### Initialize (first iteration)
All commands False. Two-button dialog "Select Control Mode" [Auto | Admin] → MODE.
CURRENT := Facility Off, TARGET := Facility Off, SUBSTATE 0.

### Admin (no interlocks)
User-command buttons visible. Each latched button `<X>_User_Cmd` asks a Yes/No dialog:
"Open/Close Turbo|Bypass|Vent|Gate Valve?", "Turn on/off Primary Pump?", "Turn on/off Chiller?",
turbo: "Start Turbo Pump?" (Yes → motor on) / if running: 3-button "Stop Turbo Pump?"
[Yes → off | No | Activate Standby → motor on + standby] / if in standby: "Exit Turbo Standby
Mode?" [Yes, Spin Up → standby off | No | Yes, Spin Down → motor off]. Window close = No.
`Turbo_Error_Acknowlege_Cmd 2` button → error-acknowledge command this iteration.
CURRENT/TARGET are forced to Pumping to Rough (1) for display while in Admin.

**Change to Auto** (state recognition on the *commanded* states):
```
primary OFF : all of turbo motor, turbo valve, gate, vent, bypass OFF   → "Facility off"        (cur 0, tgt 0)
primary ON  : gate OFF, motor OFF, turbo valve OFF                     → "Pumping to Rough"    (cur 1, tgt 4)
              gate OFF, motor ON, bypass OFF, turbo valve ON           → "Venting"             (cur 8, tgt 8)
              gate OFF, motor ON, bypass ON, turbo valve OFF, vent OFF → "Pumping to Rough"    (cur 1, tgt 4)
              gate ON,  turbo valve ON, vent OFF, bypass OFF           → "Pumping to High Vac" (cur 4, tgt 4)
otherwise → message "Target State Not Recognised! ", stay Admin.
```
Recognised "Pumping to Rough" → 3-button "Go to State: 'Pumping to Rough'?"
[Yes, Target High Vac (tgt 4) | Yes, Target Low Vac (tgt 1) | No]; other states → Yes/No.
On confirm MODE := Auto. SUBSTATE := 2 iff (Auto ∧ cur = Pumping to Rough ∧ bypass ON)
("go right into substate 2 if going to primary pump-down with the bypass open").

### Manual (planned, unreachable — MODE is never set to 2)
Dead code containing the intended interlock messages: "Please Turn Turbo Off First",
"Please wait for Turbo to slow down first" (speed > 5 %), "Please Close Vent/Bypass/Turbo Valve
first", "Please Turn Primary Pump On first", "Please Open Turbo Valve first", "Please Turn
Chiller On First", "Main Facility Pressure Too High" (Turbo Convectron ≥ 0.01 Torr),
"Foreline Pressure Too High" (Foreline ≥ 1.6 Torr), "Please Stop Primary Pump First".
The Python port implements Manual mode from these rules (see `interlocks.py`).

### Auto — state machine (Current_Facility_State enum)
0 Facility Off · 1 Pumping to Rough · 2 Overnight Pump · 3 Engage Turbo · 4 Pumping to High Vac ·
5 Disengage Turbo · 6 Vent and Shutdown (target only) · 7 Turbo slowing · 8 Venting

Every Auto iteration first evaluates the error cluster: `error? OR 5000 ≤ code ≤ 5010`
→ CURRENT := TARGET := Facility Off and log "Shutt down due to error at hh:mm:ss".
Then the CURRENT-state case runs. Buttons are latched booleans on the tab page of the state.
Outputs per state: commands (TV = turbo valve, BP = bypass, VV = vent, GV = gate, PP = primary,
CH = chiller, TM = turbo motor, SB = standby), CURRENT', TARGET', SUBSTATE', TAB page.

**0 Facility Off** — all off. Buttons: Pump to Rough → cur 1/tgt 1; Pump to High Vac → cur 1/tgt 4;
Overnight Pump → cur 1/tgt 2; Vent → cur 8 (Venting)/tgt 6 (Vent and Shutdown). TAB 0.

**1 Pumping to Rough** — PP on; VV,GV off; CH = (tgt = 4) ∨ speed > 15 % ∨ substate 0.
Substates: 0 → set "Skip Primary Warm" := primary already running; BP := primary already running;
next 1. · 1 → warm-up timer 60 s **but OR-ed with a constant True, so it passes immediately**;
BP on; next 2. · 2 → BP on; condition `1e-8 < WRG < Turbo-on threshold (0.25 Torr)` held for
30 s (Elapsed-Time reset on change) ⇒ `done`; "Elapsed Time (s)" indicator shows the hold time.
TV := (tgt ≠ 1) ∧ (TV_prev ∨ done) — opens once rough is reached (unless target is Rough Only) and latches.
Transitions: done ∧ tgt = 2 → cur 2 (Overnight); done ∧ tgt = 4 → cur 3 (Engage Turbo);
Shut Off → cur 0; Vent 2 → cur 8, tgt 8, BP off. Buttons: Overnight Pump 3 → tgt 2; Pump to Rough Only →
tgt 1; Pump to High → tgt 4. Leaving the state resets SUBSTATE to 0 and Skip Primary Warm. TAB 1.

**2 Overnight Pump** — everything off (PP off, valves closed, TM off), CH = speed > 10 %.
When now > "Turbo Engage time" (timestamp control) → cur 1, tgt 4, log "Timestamp reached, turning on
pumps at …". Buttons: Vent 4 → cur 8/tgt 8; Pump to Rough 3 → cur 1/tgt 1; Pump to High Vac 3 → cur 1/tgt 4. TAB 3.

**3 Engage Turbo** — TV on, VV off, PP on, CH on, TARGET 4. Substates:
0 BP on, GV off → 1 · 1 check `WRG < 5×threshold (1.25 Torr) ∧ Foreline < 1.25 Torr ∧ WRG > 1e-9`
  → ok: log "Pressure low enough to turn on turbo" (or "…to open gate" if speed > 2 %) → 2;
  fail: log "Pressure NOT low enough to turn on turbo!/open gate!" → cur 1 (back to rough), substate 0
· 2 BP on → 3 · 3 **BP off** → 4 · 4 BP off, **GV on** → 5 · 5 GV on, **TM on** → cur 4, substate 0. TAB 4.
(Each substate is one loop iteration; the command frame adds 1 s after any valve change and 9 s after a gate change.)

**4 Pumping to High Vac** — TV on, GV on, BP/VV off, PP/CH on. TM := NOT(Shutdown ∨ Pump to Rough 4 ∨
Overnight Pump 2 ∨ Vent and shutdown) — the motor is switched off in the same iteration a leave button is
pressed. Any of Shutdown / Pump to Rough 4 / Overnight Pump 2 / Vent and shutdonw / Vent 5 → cur 5
(Disengage) with tgt 0 / 1 / 2 / 6 / 8 respectively. TAB 5.

**5 Disengage Turbo** — GV off immediately, VV off, PP/CH on, TM unchanged. Substates: 0 → 1;
1 → 1 s timer → 2; 2 → decide by target: TV := (tgt = 8); BP := (tgt = 1);
cur := 1 (tgt 1) | 2 (tgt 2) | 8 (tgt 6 or 8) | 7 Turbo slowing (tgt 0 or other). TAB 6.

**8 Venting** — BP/GV off. 10-minute vent timer (substate 0 start, 1 running, 2 finished):
VV := timer running ∨ tgt = 6. TV := speed > 15 % (only while cur stays 8). PP := CH := speed > 15 %.
After the timer: tgt ∈ {0, 6} → cur 7 if speed > 15 % else 0; tgt 8 → stay 8 (vent closed, hold).
Buttons: Overnight Pump 5 → cur 1/tgt 2; Pump to Rough 5 → cur 1/tgt 1; Pump to Hi vac → cur 1/tgt 4;
Shut Off → cur 7 or 0, tgt 0. TAB 7.

**7 Turbo slowing** — TV on, PP/CH on, TM off, VV unchanged, BP/GV off. cur := 7 while speed > 15 %,
then 0 (Facility Off). Button Pump to High Vac 2 → TM on, TV off, VV off, cur 1, tgt 4. TAB 8.

**Global turbo protection (all Auto states):** if TM would be on and "Turbo Convectron" ≥ 5 Torr → TM := off and
log "High pressure turbo shuttoff triggered at hh:mm:ss".

### Command execution (frame 4)
DO writes: primary, [TV,BP,VV,GV], chiller, turbo (mode dependent; HP700 ack = 1 s pulse and the
"Turbo Error Aknowleged" LED is lit during the pulse). Settle waits (blocking the loop):
any valve command changed → 1000 ms (9000 ms if the gate command changed); chiller changed → 10 000 ms;
primary changed → 500 ms. Then `<X>_Sys_Cmd` indicators := new commands.

### Temporary log (frame 5)
For every command that changed this iteration append `"\r<Opened|Closed> <Turbo|Bypass|Vent|Gate> Valve at hh:mm:ss"`,
`"\r<Started|Stopped> Primary Pump|Chiller|Turbo Pump at hh:mm:ss"`. Loop paced to 100 ms.

## 6. Front panel (from the VI + the VC100 screenshot style)
Indicators: Wide Range Gauge Pressure, Foreline Convectron, Turbo Convectron, Com Potential (V),
Primary Pump Status (+colour), Chiller Status (+colour), Turbo Status (+colour), Turbo Speed,
Still Spinning?, valve read LEDs + error LEDs, Sys_Cmd LEDs, Running (blinks), Current/Target Facility
State, Tab Control (state page with the state's buttons), Control Level tab (Admin/Automatic),
Current Time/date, temporary log string, Elapsed Time (s), Turbo on Threshold (torr), error cluster
(status/code/source), Waveform chart (9 plots).
Controls: stop, Clear error?, change_to_admin_mode, Change to Auto, the 7 Admin `_User_Cmd` buttons,
Turbo_Error_Acknowlege_Cmd 2, Turbo on Threshold (torr) 2 (0.25), Turbo Engage time, rate, samples per
channel, Use fake values?, use DMM?, and the per-state Auto buttons (Pump to Rough, Pump to High Vac,
Overnight Pump, Vent, Shut Off, Vent 2, Overnight Pump 3, Pump to Rough Only, Pump to High, Vent 4,
Pump to Rough 3, Pump to High Vac 3, Shutdown, Pump to Rough 4, Overnight Pump 2, Vent and shutdonw,
Vent 5, Overnight Pump 5, Pump to Rough 5, Pump to Hi vac, Pump to High Vac 2, Skip Primary Warm).

## 6b. Hardware change after the VI was written (2026-09-14) — primary pump

The facility was rewired: the primary pump is driven through **two relays** and reports its drive
frequency, so the VI's single `Primary_Pump_Cmd` / `Primary_Pump_Read` pair no longer describes it.

| VI (above) | Now |
|---|---|
| `Primary_Pump_Cmd` DO `Mod3/port0/line0` | **power** relay DO `Mod3/port0/line6` + **run** relay DO `Mod3/port0/line7` |
| `Primary_Pump_Read` DI `Mod2/port0/line0` | retired — no boolean read-back |
| `Com Potential` AI `Mod1/ai6` (display only) | **pump drive frequency**, 0-10 V = 0-210 Hz — acquired only while the operator has the reader switched on; off by default since reading it put noise on the other analog channels (2026-09-21) |

The run relay is inert until the power relay is closed, so the port sequences power → 15 s → run on
start and run → 5 min → power off on stop (`timings.primary_power_to_run_gap_s` /
`primary_run_to_power_off_gap_s`); the safe state on exit still drops both at once.  With the frequency reader off (the default), "primary
pump running" means the run relay is closed and error **5000** stays unraisable exactly as in the
VI; switching the reader on arms the spin-up detection and makes "running" mean real rotation.  Everything above this
section still describes the VI as written; this is the one place the small chamber's hardware has
moved on from it.

## 6c. The VI uses SEPARATE analog tasks (bug found on the facility, 2026-09-14)

Decoded from `Main_V4.4.semantic.lvnet`, the VI builds **two** AI task chains, not one:

| Task | Channels | Range |
|---|---|---|
| `DAQmx_Create_Task_7229` | `Wide Range_Gauge_Read` (ai5) -> Convectrons (ai1, ai2) -> `Com Potential` (ai6) | 0-10 V |
| `DAQmx_Control_Task_39736` | `Turbo_Speed_Read` (ai3, HP700) and `BRT_Turbo_Speed_Read` (ai4, BigRed) | 0-10 V / **-10-10 V** |

All channels are differential (10106) in both — the terminal configuration is *not* the difference.
What matters is that the **turbo speed is alone in its own task**.

The port originally put the speed channel in the same task as the gauges.  One multiplexed ADC serves
a task's channels in sequence, and the BigRed speed output does not settle inside the convert window,
so ai4 returned the residue of the channel scanned immediately before it:

* Turbo Convectron at atmosphere = 6.88 V -> a phantom **"69 % turbo speed"** with the turbo stopped
  (observed: 71 %, = 7.1 V);
* after the pump-frequency channel (ai6) was added ahead of it, the phantom speed tracked the pump
  **linearly**, 0-10 V = 0-100 %.

The LabVIEW panel read a clean 0 on the same wiring throughout, which is what identified the cause.
The port now matches the VI: gauges (+ extras/compressor) in the main task, and one dedicated
single-channel task per turbo speed and for the pump frequency.  This also lets the per-turbo
`speed_sample_rate_hz` / `speed_samples` (the VI's 4000 Hz x 2000 in HP700 mode) take effect — a
shared task can only carry one timing configuration.

## 7. Differences the port deliberately adds (all switchable in config)
* CSV daily logging (VC100 behaviour), zoomable history plots.
* Manual mode (interlocked manual control) actually implemented.
* Pressure unit switch Torr ⇄ mBar for display/logging; all thresholds stored in Torr as in the VI.
* Per-facility config (channel map, number of turbos/valves/gauges, thresholds, timings).
* Simulated hardware backend so the program runs without NI-DAQmx.
