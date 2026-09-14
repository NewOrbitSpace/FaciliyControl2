# Small chamber (Main_V4.4)

Single-turbo chamber (VC40/VC80-class): one turbo (BigRed, **D-SUB15** control), valves
Turbo / Bypass / Vent / Gate, gauges WRG (ion) + 2 Convectrons (Turbo, Foreline).
No compressor-pressure sensor and no "Com Potential" reading on this chamber.

## Contents
- `labview/Main_V4.4.vi` — the original VI (source of truth) and its exports
  (`.describe.txt`, `.vi.json`, `.unresolved.txt`).
- `labview/Small_Chamber_Support VIs/` — LabVIEW type-definitions (`Controls/`) and instrument
  drivers (`Drivers/`: Pfeiffer Vacuum RS485 turbo library, Agilent U1250 DMM driver).

## Hardware change: primary pump rewired (2026-09-14)
The primary pump now has **separate power and run relays** and reports its **drive frequency**:
`Mod3/port0/line6` = power, `Mod3/port0/line7` = run, `Mod1/ai6` = 0-10 V for 0-210 Hz.  The old
`Mod3/port0/line0` command and the `Mod2/port0/line0` read-back are retired, and `Mod1/ai6` no longer
carries 'Com Potential'.  The run relay only acts once the pump is powered, so the controller
sequences power -> 5 s -> run (and the reverse on stop); "running" is the frequency above 5 Hz, and
error 5000 fires if the pump is commanded but never spins up.  Full detail in the engine README and
`docs/MAIN_V4.4_REFERENCE.md` section 6b.  **This is a deviation from `Main_V4.4.vi`** — the VI still
shows the single-relay wiring, so read it with that section alongside.

## How it runs
On the shared engine in `../../python_facility_control/`, profile
`config/facility_main_v4.4.yaml`. Analog inputs are read **differentially** (VI value 10106).

## Support-VI review (2026-09-08)
The type-definitions are the authoritative check on the port, and they confirm it:

| Typedef (`.ctl`)            | Enum (order)                                                                                          | Python            | Result |
|-----------------------------|------------------------------------------------------------------------------------------------------|-------------------|--------|
| `Facility_State`            | Intialize, Admin, Manual, Auto (0-3)                                                                  | `Mode`            | match  |
| `Current_Facility_State`    | Facility Off, Pumping to Rough, Overnight Pump, Engage Turbo, Pumping to High Vac, Disengage Turbo, Vent and Shutdown, Turbo slowing, Venting (0-8) | `FacilityState`   | match  |
| Tab Control                 | Facility Off, Pumping to Rough, Holding at Rough, Overnight Pump, Engaging Turbo, Pumping to High Vac, Closing Gate, Venting, Turbo Slowing (0-8) | `TabPage`         | match  |
| `Turbo_Control_Mode`        | HP700_DSUB25, HP700_RS485, BigRed_DSUB15, BigRed_RS485 (0-3); VI constant = **2 (BigRed D-SUB15)**    | `bigred_dsub15`   | match  |

Conclusion: **no safety change required** — the enums and the active turbo mode all match the port.

Not used by this chamber's active control path (documented, not defects):
- Pfeiffer Vacuum library = the **RS485** turbo driver. This chamber uses the D-SUB15 path, so RS485
  turbo control is not ported.
- U1250 DMM driver = the **Com Potential** reading, removed from this chamber's profile.

Minor cosmetic note: the D-SUB25 turbo family is **HP700** (channel names "HP700 Motor Cmd",
"HP700_Error_Read"); a couple of Python log strings still say "HP2300". Only affects the HP700 path
(not used here); can be tidied later.
