# VC100_Facility_Control_V1.0.vi – what the medium-chamber VI does, and how the port follows it

Companion to `MAIN_V4.4_REFERENCE.md` (small chamber).  Everything below was read out of the VI's
block diagram (lvkit/vi2json export + the heap XML from pylabview; the scripts are in
`chambers/medium/analysis/`).  Where the two VIs differ, the profile `config/facility_vc100.yaml`
selects the behaviour – the shared engine has no facility-specific code.

## 1. Hardware / pinout (chassis **cDAQ3** – the VI writes `'cDAQ3'` into `DAQ_Name` at start-up)

Every physical channel is `Concatenate Strings(DAQ_Name, '<Mod>/<line>')`.  The tables give the VI's
channel name, the module line, and where the port puts it.

### Analog inputs – NI-9205 in slot 8, **all Differential** (DAQmx 10106), gauges 0…10 V, speeds ±10 V

| index in the VI's AI array | line | VI channel name | conversion (formula node) | profile |
|---|---|---|---|---|
| 0 | Mod8/ai0 | Wide Range_Gauge_Read | `P_WRG_mBar = 10**((V-6.8)/0.6)` | gauge `wrg`, `edwards_wrg_mbar` |
| 1 | Mod8/ai1 | Foreline_gauge_1_Read | `P_Gage_mBar = 10**((V-6.143)/1.286)` | gauge `fore`, `edwards_apg_mbar` |
| 2 | Mod8/ai2 | Turbo_gauge_1_Read | same | gauge `turbo1_gauge` |
| 3 | Mod8/ai3 | Turbo_gauge_2_Read | same | gauge `turbo2_gauge` |
| 4 | Mod8/ai4 | Turbo_gauge_3_Read | same | gauge `turbo3_gauge` |
| 5 | Mod8/ai7 | Air_Com_pressure | `Pressure_Bar = V/5*10` | `compressor: {channel, scale: 2}` |
| 6 | Mod8/ai5 | Turbo_2_Speed_Read (±10 V) | `mean × 10` = % | turbo2 `speed_ai` |
| 7 | Mod8/ai6 | Turbo_3_Speed_Read (±10 V) | `mean × 10` = % | turbo3 `speed_ai` |

The speed channels are appended to the same task after a `DAQmx Control Task`, which is why they sit
at indices 6/7 although they are ai5/ai6.  Timing: 1000 Hz × 25 samples (`rate` / `samples per
channel` controls); the speeds are read a second time with 4000 Hz × 2000 samples – the port reads
all eight channels once per loop with the profile's rate/samples and takes the mean, like the VI.
A separate loop reads a "Dummy Variable to get nice time data" on the hard-coded string
`cDAQ2Mod8/ai0` (note: cDAQ**2**) only to timestamp the charts – not ported.

### Digital outputs

| line | VI channel name | profile |
|---|---|---|
| Mod1/port0/line0 | Valve_Bypass_Cmd | `bypass.cmd` |
| Mod1/port0/line1 | Valve_Vent_Cmd | `vent.cmd` |
| Mod1/port0/line2 | Valve_Turbo_1_Cmd | `turbo1_valve.cmd` |
| Mod1/port0/line3 | Valve_Turbo_2_Cmd | `turbo2_valve.cmd` |
| Mod1/port0/line4 | Valve_Turbo_3_Cmd | `turbo3_valve.cmd` |
| Mod1/port0/line5 | Valve_Gate_1_Cmd | `gate1.cmd` |
| Mod1/port0/line6 | Valve_Gate_2_Cmd | `gate2.cmd` |
| Mod1/port0/line7 | Valve_Gate_3_Cmd | `gate3.cmd` |
| Mod3/port0/line0 | Turbo 1 Motor CMD | turbo1 `motor_do` |
| Mod3/port0/line1 | Turbo 1 Standby CMD | turbo1 `standby_do` |
| Mod3/port0/line2 | Turbo 1 Reset CMD | turbo1 `error_ack_do` |
| Mod3/port0/line3 | Turbo 2 Motor CMD | turbo2 `motor_do` |
| Mod3/port0/line4 | Turbo 2 Standby CMD | turbo2 `standby_do` |
| Mod3/port0/line5 | Turbo 2 Reset CMD | turbo2 `error_ack_do` |
| Mod3/port0/line6 | Turbo 3 Motor CMD | turbo3 `motor_do` |
| Mod3/port0/line7 | Turbo 3 Standby CMD | turbo3 `standby_do` |
| Mod4/port0/line0 | Primary_Pump_Cmd | `primary_pump.cmd` |
| Mod4/port0/line1 | Chiller_Cmd | `chiller.cmd` |

The turbo DO lines are created in For loops from string arrays: Turbo 1 and 2 `['Motor CMD','Standby
CMD','Reset CMD']` from line 0 and line 3, Turbo 3 `['Motor CMD','Standby CMD']` from line 6 – **Turbo 3
has no reset line** in the VI (the dialog button "Yes, Reset Both HiPaces" only pulses Turbo 2's).  The
8-element write is `[T1 motor, T1 standby, T1 reset, T2 motor, T2 standby, T2 reset, T3 motor, T3 standby]`.

### Digital inputs – slot 2

| line | VI channel name | profile |
|---|---|---|
| Mod2/port0/line0…7 | Valve_Bypass/Vent/Turbo_1/2/3/Gate_1/2/3_Read | `valves.*.read` (same order as the commands) |
| Mod2/port0/line8 | Primary_Pump_Read | `primary_pump.read` |
| Mod2/port0/line9 | Chiller_Read | `chiller.read` |
| Mod2/port0/line10 | Turbo 1 Rotating | turbo1 `rotating_di` |
| Mod2/port0/line11 | Turbo 1 Accelerating | turbo1 `accelerating_di` |
| Mod2/port0/line12 | Turbo 1 At Speed | turbo1 `at_speed_di` |
| Mod2/port0/line13 | Turbo 1 Breaking | turbo1 `braking_di` |
| Mod2/port0/line14 | Turbo 1 Alarm (normally closed – FALSE = alarm) | turbo1 `alarm_di`, `alarm_di_inverted: true` |
| Mod2/port0/line15 | Turbo 1 Warning (normally closed – FALSE = warning) | turbo1 `warning_di`, `warning_di_inverted: true` |
| Mod2/port0/line16 | Turbo 2 Error read (24 V = healthy) | turbo2 `error_di`, `error_di_inverted: true` |
| Mod2/port0/line17 | Turbo 3 Error read (24 V = healthy) | turbo3 `error_di`, `error_di_inverted: true` |

The six Turbo-1 lines are the fields 0–5 of the VI's `Turbo 1 Cluseter` typedef (fields 6–8 are the
Motor / Standby / Reset commands).  `Use fake values?` (front panel) selects front-panel "Fake_*"
controls instead of the DAQ – the port's simulator replaces that.

## 2. Constants (front-panel defaults – nothing is written into them at start-up except `DAQ_Name`)

| VI control / constant | value | profile key |
|---|---|---|
| Turbo on Threshold (torr) 2 (copied into "Turbo on Threshold (torr)" every loop; "…3" unused) | **0.25 (mBar!)** | `turbo_on_threshold_mbar: 0.25` |
| Threshold_wiggle_room_multiplier | 5 | `turbo_on_wiggle_room_multiplier` |
| Elapsed Time (below threshold) | 30 s | `min_time_below_threshold_s` |
| Elapsed Time3 (primary warm-up before the bypass opens) | 20 s | `primary_to_bypass_gap_s: 20` |
| Elapsed Time2 (Disengage) | 1 s | `disengage_wait_s` |
| Elapsed Time4 (Venting) | 60 × 15 = 900 s | `vent_duration_s: 900` |
| Elapsed Time5 (compressor low) / `< 5.0` bar | 5 s / 5 bar | `compressor_low_time_s`, `compressor_min_bar` |
| Turbo Speed Reached / Slow Speed Threshold | 95 % / 5 % | `turbo_speed_reached_pct`, `turbo_slow_speed_pct` |
| "still spinning" constant in Rough / Engage text | 15 % | `turbo_spinning_pct`, `turbo_engage_msg_speed_pct` |
| Turbo slowing Threshold (%) | 50 % | `turbo_slowing_threshold_pct` |
| HiPace standby display `speed > 40` | 40 % | turbo `standby_speed_min_pct` |
| Turbo N Gauge `< 5.0` (hot-start protection) | 5 mBar | `turbo_high_pressure_shutoff_mbar: 5` |
| Wait after valve change: bypass / gate / other | 2000 / 9000 / 1000 ms | `bypass_settle_ms`, `gate_settle_ms`, `valve_settle_ms` |
| Wait after chiller change; after primary **on** (none when off) | 10 000 ms; 10 000 ms | `chiller_settle_ms`, `primary_settle_ms`, `primary_settle_off_ms: 0` |
| Turbo N in use? | all True | `in_use` |
| CSV every 5 s to `C:/Users/Lab Admin/Desktop/Pressure_Data` | | `logging.csv_*` |

## 3. Status words and errors (status frame)

* **Turbo 1 (contacts):** `¬Alarm → Error (red)`, `¬Warning → Warning`, `Accelerating → Accel`,
  `Standby cmd → In Standby`, `At Speed → Normal`, `Rotating ∨ Breaking → Decel`, else `Off`.
  `¬Alarm ∨ ¬Warning` raises **error 5005 "Turbo1 Alarm" / "Turbo1 Warning"** (status TRUE) – so a
  *warning* also stops Auto mode.  Profile: `warning_is_error: true` (set false to only display it).
* **Turbos 2/3 (HiPace):** `in use ∧ ¬error line → Error` + **5007 "Turbo 2 Error." / "Turbo 3 Error."**
  (not gated on the chiller – Main_V4.4 ignored the turbo error while the chiller was off);
  motor commanded: `speed > 95 → Normal`, `speed > 40 ∧ standby → In Standby`, else `Accel`;
  motor off: `speed > 5 → Decel` else `Off`.
* **Valve conflicts** (`command XOR read`): 5000 primary (*checked* on this facility), 5001 turbo valves
  (all three), 5002 bypass, 5003 vent, 5004 chiller, 5010 gates (all three, message always says
  "Gate 1").  These clusters have `status = FALSE` in the VI (warnings) – Auto mode nevertheless stops
  because it tests `error? ∨ 5000 ≤ code ≤ 5010`; the port reports them as errors with the same codes.
* **Compressor:** `Air pressure < 5 bar` for 5 s → "Compressor Pressure Below 5 Bar!" with code 5010 in
  the VI (the port uses **5011** so it is distinguishable from a gate conflict; both stop Auto).
* Primary status: `read XOR cmd → Error`, `read → is On`, else `is Off`.  With `Use Ecodry As Primary?`
  the VI shows the status from an "Ecodry Speed" that is a placeholder constant 0 (not implemented in
  the VI) – the equivalent here is `primary_pump.cross_check: false`.
* "Measurement" / `Read (Single Point).vi` / `Backlight Command.vi`: inside case structures with a
  constant FALSE selector – dead code, not ported.

## 4. Auto mode – frame by frame (differences to Main_V4.4 in **bold**)

Error override first: `error? ∨ 5000 ≤ code ≤ 5010 → CURRENT := TARGET := Facility Off`, log "Shutt down
due to error at …".  Substate is one shift register shared by all states.  "spinning" =
`Turbo1.Rotating ∨ T2 % > thr ∨ T3 % > thr` with thr = 15 % in Rough/Engage and the 50 % control elsewhere.

**0 Facility Off** – all off.  Pump to Rough → cur 1/tgt 1; Pump to High Vac → 1/4; Overnight → 1/2;
Vent → cur 8 Venting / tgt 6 Vent and Shutdown; **Shut Off once rough → cur 1 / tgt 0** (rough the
chamber, then everything off).

**1 Pumping to Rough** – PP on; VV, GV off; CH = tgt = 4 ∨ spinning(15 %) ∨ substate 0.
Substates: 0 → 1 at once · 1: **20 s timer** (`Elapsed Time3`) → BP on, → 2 · 2: `WRG < 0.25 mBar` held
30 s (`Elapsed Time`, reset on change) ⇒ done.
Turbo valves per turbo: **`TV_i := (tgt ≠ 1) ∧ in_use_i ∧ (done ∨ TV_i_prev ∨ (BP_prev ∧ WRG < turbo gauge_i))`**
– a turbo valve may open before the 30 s wait once the chamber is below that turbo's body pressure.
Transitions: done ∧ tgt 2 → Overnight; done ∧ tgt 4 → Engage; **done ∧ tgt 0 → Turbo slowing if spinning
else Facility Off**; **Shut Off → Turbo slowing if spinning(15 %) else Off**; Vent → 8.  Buttons: Pump to
Rough → tgt 1, Pump to High Vac → tgt 4, Overnight → tgt 2, **Shut Off / Shut Off once rough → tgt 0**.
(The VI also closes the bypass for one iteration when Overnight is pressed – a glitch, not reproduced.)

**2 Overnight Pump** – motors off, BP/VV/GV off; **PP = CH = spinning(50 %)**; **TV_i := spinning_i**
(Turbo 1: Rotating).  `now > Turbo Engage time` → cur 1 / tgt 4, log "Timestamp reached…".  Buttons as
Main_V4.4 (Vent → 8/8, Pump to Rough → 1/1, Pump to High Vac → 1/4).

**3 Engage Turbo** – identical to Main_V4.4 (TV on, PP/CH on; substates BP on → check `WRG, foreline <
5 × 0.25 = 1.25 mBar` → BP off → GV on → motors on → 4).  Log text "…open gates / turn on turbos" chosen
by spinning(15 %).  A failed check goes back to Rough, substate 0.

**4 Pumping to High Vac** – identical: motors := ¬(Shutdown ∨ Pump to Rough ∨ Overnight ∨ Vent and
shutdown); those → Disengage with tgt 0/1/2/6; Vent → Disengage with tgt 8 (turbos keep running).

**5 Disengage Turbo** – identical (GV off, 1 s, then TV := tgt = 8, BP := tgt = 1, cur by target).

**8 Venting** – BP/GV off; **15 min** vent timer; VV := timer running (∨ tgt 6); PP = CH = spinning(50 %);
**TV_i := spinning_i ∧ (TV_i_prev ∨ foreline < turbo gauge_i)**.  After the timer: tgt ∈ {0, 6} → Turbo
slowing if spinning else Off; tgt 8 → hold.  Buttons: **Shutdown Now → cur 7/0 (vent closes at once)**;
**Shutdown After Vent → tgt 0 (vent finishes first)**; Pump to Hi vac → 1/4; Overnight → 1/2; Pump to
Rough → 1/1; **all of these switch the motors off**.

**7 Turbo slowing** – PP/CH on, motors off, **VV off**, BP/GV off; **TV_i := spinning_i ∧ (TV_i_prev ∨
foreline < turbo gauge_i)**; cur := 7 while spinning(50 %) else 0.  Pump to High Vac → motors on, cur 1 /
tgt 4; **Vent → cur 8 / tgt 0**.  (The VI closes all turbo valves for the first iteration of Venting and
Turbo slowing – a 100 ms glitch, not reproduced.)

**Standby (all states):** `Standby_i := Motor_i ∧ ¬Gate_i` – a turbo commanded on while its gate is
closed runs in standby (e.g. Turbo slowing → Pump to High Vac).

**Hot-start protection:** motor_i forced off while `turbo gauge_i ≥ 5 mBar`, log "High pressure turbo N
shuttoff triggered at …".

## 5. Admin mode

Same dialogs as Main_V4.4 with the turbo number in the text ("Open Turbo 2 Valve?", "Stop Turbo 3?" +
"Activate Standby", "Exit Turbo 2 Standby Mode?").  "Reset Turbo?" is a three-button dialog: *Yes, Reset
Shimadzu* (Turbo 1 reset line), *Yes, Reset Both HiPaces* (Turbo 2 reset line – Turbo 3 has none), *No*.
The port's **Reset Turbos** pulses every configured reset/ack line for 1 s.  State recognition
("Change to Auto") uses the same decision tree, with `Index_of_working_turbo` selecting which turbo's
valves are inspected and a check that all turbo valves / gates agree; the port evaluates the tree over
all turbos in use.

## 6. What the profile switches on (all in `config/facility_vc100.yaml`)

`auto_mode:` `overnight_backing_while_spinning`, `turbo_valve_gauge_rule`, `standby_when_gate_closed`,
`shut_off_spins_down_first`, `vent_closes_on_shutdown`, `venting_buttons_stop_turbos` – each one is a
single rule above; all default to *off* so the small chamber is unchanged.  `auto_buttons:` lists the
VI's tab-page buttons.  Turbo blocks carry the error code/message, the chiller gating and the standby
speed rule.  `thresholds` accept `_mbar` keys (converted to Torr internally, as both VIs keep Torr
in their control names).

## 7. Deliberate deviations / open points

* Start order of Pumping to Rough is the VI's (primary first, bypass after 20 s).  The test engineer's
  rule from the small chamber (bypass first above 5e1 mBar) is one line: `bypass_first_above_mbar: 50`.
  Same for `overnight_skip_below_mbar: 0.2`.
* "Is the manual vent valve closed?" is asked before every pump-down button, as on the small chamber.
* Compressor error code 5011 instead of the VI's reused 5010.
* The two one-iteration glitches (bypass pulse on the Overnight button, turbo-valve pulse when entering
  Venting / Turbo slowing) are not reproduced.
* Turbo 3 reset: no line in the VI; if the cable has one, add `error_ack_do: Mod3/port0/line8`.
* `Use Ecodry As Primary?` / `Ecodry Speed` and the `Measurement` sensor are unfinished in the VI and
  not ported.
