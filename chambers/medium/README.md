# Medium chamber (VC100)

Three-turbo chamber controlled by `VC100_Facility_Control_V1.0.vi` on chassis **cDAQ3**:
Turbo 1 = Shimadzu with a dry-contact interface (Motor/Standby/Reset outputs; Rotating, Accelerating,
At Speed, Breaking, Alarm, Warning inputs – no speed signal), Turbos 2 and 3 = Pfeiffer HiPace on
D-SUB (±10 V speed, error line, Motor/Standby, Reset on Turbo 2 only).  Valves: Bypass, Vent, three
Turbo valves, three Gates.  Gauges: Edwards WRG (chamber) + four APG/Pirani (foreline, one per turbo),
all in mBar.  Compressed-air pressure sensor for the pneumatic valves.

## Contents
- `VC100_Facility_Control_V1.0.vi` — the original VI (source of truth).
- `Support_VIs/` — the type-definitions (`Current_Facility_State.ctl`, `Facility_State.ctl`,
  `Turbo_Control_Mode.ctl`) – identical to the small chamber's, so the shared `Mode`, `FacilityState`
  and `TabPage` enums apply unchanged.
- `analysis/` — the scripts and decoded frame listings the port was made from (see its README).

## How it runs
On the shared engine in `../../python_facility_control/` with the profile
**`config/facility_vc100.yaml`**:
```
cd python_facility_control
.\run.bat --config config\facility_vc100.yaml --sim --time-scale 20     # simulator, any PC
.\run.bat --config config\facility_vc100.yaml --daq --mode admin        # lab PC, real cDAQ3
python tools\daq_check.py --config config\facility_vc100.yaml            # read-only channel check first
```
The full description of the VI – pinout with the VI node each line comes from, every constant, the
Auto state machine frame by frame and every difference to the small chamber – is
`python_facility_control/docs/VC100_REFERENCE.md`.

## What is different from the small chamber (all of it is in the profile, none in the code)
| | small (Main_V4.4) | medium (VC100) |
|---|---|---|
| chassis / modules | cDAQ1, DO Mod3+4, DI Mod2, AI Mod1 | **cDAQ3**, DO Mod1 (valves) + Mod3 (turbos) + Mod4 (pumps), DI Mod2 (18 lines), AI Mod8 |
| turbos | 1 × BigRed D-SUB15 | **3**: Shimadzu contacts + 2 × HiPace D-SUB |
| gauges | WRG (ion) + 2 Convectron, Torr formulas | Edwards WRG + 4 APG, **mBar formulas** |
| compressor sensor | none | Mod8/ai7, `bar = V/5*10`, **< 5 bar for 5 s → error, Auto stops** |
| primary read-back check | disabled in the VI | **enabled** (error 5000); 10 s wait after switching the pump on |
| turbo-on threshold | 2e-1 mBar (test engineer) | **0.25 mBar** (VI default; "(torr)" in the name, mBar in use) |
| rough sequence | primary → bypass (engineer's pressure rule) | primary → **20 s** → bypass (VI); engineer's rule available as one line |
| vent time | 10 min | **15 min** |
| valve settle waits | 1 s / gate 9 s | 1 s / **bypass 2 s** / gate 9 s |
| Overnight while turbos spin | everything off, chiller > 10 % | **primary + chiller on, spinning turbos' valves open** until < 50 % |
| turbo valves in Rough / Venting / slowing | open with the 30 s rule / while spinning | **compared with the turbo gauges** (never blow gas into a turbo) |
| standby line | Admin only | **Auto: motor on while its gate is closed** |
| turbo error | HP/BRT error, ignored while chiller off | Turbo 1 **Alarm or Warning → 5005**; Turbo 2/3 error → **5007**, not gated on the chiller |
| buttons | Shut Off (Rough, Venting) | + **Shut Off once rough**, **Shutdown Now**, **Shutdown After Vent**, Vent on the slowing page |

## Support-VI review
`Turbo_Control_Mode.ctl` lists HP700_DSUB25, HP700_RS485, BigRed_DSUB15, BigRed_RS485 – the VC100 VI
does not use this typedef for its turbos (the contact interface and the two HiPace D-SUB channels are
wired directly), so the profile names the modes explicitly (`shimadzu_contacts`, `hipace_dsub25`).
`Current_Facility_State.ctl` / `Facility_State.ctl` match the shared enums.  No safety change needed.

## Before the first run on the facility
1. `daq_check.py --config config\facility_vc100.yaml` with LabVIEW closed: every one of the 44 lines
   must be found on cDAQ3; compare the gauge readings (mBar) and the reed/turbo contacts with the
   LabVIEW panel.  Check especially that the **Alarm / Warning contacts read TRUE when healthy**
   (the profile inverts them) and that the Turbo 2/3 error lines read TRUE when healthy.
2. Admin mode, one device at a time (vent valve first), watch for 5000–5010 conflicts.
3. Turbo 3 has no reset line in the VI; if the cable has one, add `error_ack_do: Mod3/port0/line8`.
