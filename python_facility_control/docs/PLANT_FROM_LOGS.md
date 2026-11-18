# What the facility logs say (2026-10-23 .. 2026-11-11)

Source: the CSV logs the controller writes (`logs/facility_<id>_<date>.csv`, a row every 5 s) -
18 days from the VC100, 2 days from the small chamber (one pump-down).  Three things came out of
them: what really went wrong, how the plant really behaves (the simulator and the safety tests
now use those numbers), and whether the new trip levels are clear of normal operation.

`python tools/audit_logs.py logs/facility_vc100_*.csv` repeats the first part on any log.

## 1. What went wrong (VC100) - all three are fixed, each has a test named after its date

| when | what the log shows | cause | fixed by |
|---|---|---|---|
| 2026-10-28 15:06:34 | Admin -> Auto with Turbo 2 and Turbo 5 coasting at 98 %, turbo valves shut, primary off, foreline at 1020 mBar.  Auto commanded Turbo Valve 2 open.  5 s later both rotors were at **18 %**. | "A spinning turbo keeps its valve" opened the valve without looking at the foreline. | `turbo_valve_needs_backing` (2026-10-28) on the VC100; rule 3b (2026-11-12) for every chamber. |
| 2026-11-05 09:46:25 | Vent and Shutdown at full speed; 11 min later, HiPace turbos at 71-78 %, Pump to High Vac.  Bypass opened onto the chamber at 1000 mBar with Turbo Valves 2 and 3 open.  Within 10 s Turbos 2, 3, 5 and 6 were at **5 %**.  No error: the pumps reported "Turbo Warning". | The same rule held the turbo valves open through the roughing of a vented chamber. | Exposure rule (2026-11-11, commit c6a06b4). |
| 2026-11-09 13:43, 16:43, 18:27 | Pump to High Vac with the turbos still coasting at 50-70 %.  Gates 1..4 opened one by one; the coasting turbos took the chamber from 1.5e-1 to below 1e-2 mBar in 30 s; Auto closed the gates again and put all six turbos in standby.  Each time the operator went to Admin and finished by hand. | Gates-first / turbos-first was decided again on every pass of the gate step. | Decision latched (2026-11-11). |

Turbo 1 and Turbo 4 (Shimadzu) were not hit on 5 Nov: they had already braked to a stop, so
their turbo valve was shut.

Since 9 Nov the operators rough the chamber by hand in Admin after a vent (the log shows it five
times) - presumably because of 5 Nov.  With the fix, Pump to High Vac from Venting is safe in Auto.

Not incidents, but in the log: the chiller goes off with the turbos at 40-50 % at the end of every
Turbo slowing (10 times; now it stays on until they have stopped); RS-485 "no reply" errors, nearly
all during the nights of 26-27 Oct in Admin (twice in Auto, which stopped a pump-down); Gate 4 / 5
command-read conflicts during commissioning.

## 2. How the plant behaves, and what the model now uses

| | VC100, logged | VC100 model | small chamber, logged (1 pump-down) | small model |
|---|---|---|---|---|
| roughing, atmosphere -> turbo-on threshold | 5 min 50 s after the bypass opens (6.9 min in the state) | 6.4 min | 17 min | 16 min |
| chamber when Engage Turbo starts | 1.6-1.7e-1 mBar | 1.8e-1 | 2.4e-1 | 1.9e-1 |
| chamber when Pumping to High Vac starts | 1.1-1.4e-1 mBar | 1.5e-1 | 2.3e-1 | 2.0e-1 |
| ... below 5e-2 / 1e-3 / 1e-5 mBar after | 43 s / 59 s / 2.5-5 min | 38 s / 65 s / 2 min | 2.5 min / 3.6 min / 97 min | 1.6 min / 2.2 min / - |
| base pressure | 5e-7 .. 1.6e-6 mBar | 1.8e-6 | 2.6e-6 after 7 h | - |
| foreline, turbos at full speed, no gas | 4-6e-3 mBar | 5.9e-3 | 7.1e-2 mBar | 7.1e-2 |
| foreline, highest during run-up | 2.6-3.9e-1 mBar | 3.6e-1 | 3.9e-1 | 3.4e-1 |
| chamber in high vac with gas flowing | up to 1.2e-4 mBar | - | up to 9.7e-5 | - |
| **foreline after the primary pump stops** | **1020 mBar within 6 s** (every time) | 2 s time constant | at atmosphere when off | 2 s |
| turbo run-up to 100 % | HiPace 130-160 s; Shimadzu "at speed" after 370 s | 150 s / 370 s | 6 min | 6 min |
| turbo coast-down, motor off | HiPace 1.8-2.8 %/min, the same backed or isolated (~45 min to stop); Shimadzu brakes to a stop in 9.5 min | 43 min / 9.5 min | **not in the log** | 2 min (!! guess) |
| **turbo with air on its exhaust** | **78 % -> 5 % in under 10 s**, warning, no error | 15 %/s, warning only | not in the log | assumed the same |
| vent to atmosphere | 500 mBar after 45-80 s, 990 mBar after 80-140 s | 74 s / 141 s | about 2.5 min | 2.9 min |
| chamber leak-up, everything off | 9e-7 mBar/s | - | 1e-5 mBar/s | - |
| turbo gauges, everything off | 0.3-5 mBar | starts at 1.3 | 2.5-7 mBar | starts at 5 |
| standby speed | Turbo 2, 3: 50 %; Turbo 5, 6: 66.6 % | profile | - | - |
| compressed air | 5.8-6.35 bar | 6.1 | - | - |

The numbers are in each profile's `simulation:` block, marked `[log]`.  The older tests
(`tests/conftest.Harness`) keep a quick plant for roughing, venting and spin times - they check the
control logic with iteration counts - and take everything else as measured.

Three of these change what "safe" means:

* **The foreline is at atmosphere within seconds of the primary stopping.**  The model used to
  back-fill over minutes.  A turbo valve open on a turning turbo with the primary stopped is now a
  violation after 1 s (rule T9), and a primary pump that trips at full speed puts gas into the
  turbos before any valve can move - software cannot prevent that part (see the questions).
* **Air on a turning rotor stops it in seconds, and the HiPace pumps do not raise an error.**  The
  safety tests no longer assume any turbo controller trips; the monitor has a rule for the symptom
  itself (T14: more than 20 % of full speed lost in 5 s).
* **The gates open at 1.1-2.4e-1 mBar**, above the 1e-1 mBar High Vac trip level - the "counts once
  below half" arming of that trip is needed on both chambers, on every pump-down.

## 3. The new trip levels against the logs

| trip | level | highest in normal operation | margin |
|---|---|---|---|
| backing lost (error 5012): foreline, turbo turning, bypass shut | 3 mBar, both chambers (set 2026-11-12; it was 1.25 / 1 mBar) | 3.9e-1 mBar, during run-up, on both chambers | x7.7 |
| chamber pressure in Pumping to High Vac (error 5013), once pumped down | 1e-1 mBar | 1.2e-4 mBar | x800 |
| ... while still pumping down (not armed until below 5e-2 mBar) | 1.25 / 1 mBar | 1.4e-1 / 2.3e-1 mBar at entry | x9 / x4 |
| chamber pressure, slow trip (2026-11-17): above the level for 30 s, once the chamber has been below 5e-3 mBar | 1e-2 mBar | 9.8e-4 mBar (VC100), 7.9e-4 mBar (small) once the chamber had been below 1e-3 | x10, and it must last 30 s |
| turbo speed drop (error 5015, 2026-11-17), crash rule: % of full speed lost within 5 s | 30 % | 7.4 % (small chamber's speed signal after a start, blanked); 5 % (gate opened by hand onto a turbo at speed); 0.2-1 % coasting / braking | x4 |
| ... fast rule: % lost within 1 s (off for 15 s after motor off / standby / its gate opening / chiller off) | 5 % | the rows are 5 s apart: nothing normal lost 5 % even within 10 s (2.8 % in 5 s after a gate opened onto a coasting turbo, 0.6 % in 5 s after a motor stop) | at least x2 on what the logs can show |

None of them would have fired on any logged day, except the backing-lost check and the speed-drop
trip at the two incidents - where they should (the incidents fell at 5-16 % a second).

## 4. What re-running the safety tests on the measured plant found

* **Fixed (KI-11):** a foreline over its limit shut the turbo valve but left the gate open for one
  more loop.  With the limit as it then was (1 mBar) a chamber leak on the small chamber's weak
  roughing line pushed the foreline over it before the chamber gauge reached its own trip, so
  that was the trip that fired - and the rotor would have kept taking gas through its gate with
  its exhaust shut.  Gate and turbo valve now shut together.  (The limit has since been set to
  3 mBar, which a leak no longer reaches first in the model; the fix stays.)
* **Accepted, hardware only:** the primary pump stopping at full speed.  The foreline is at
  atmosphere in seconds; the turbo valves are shut 1-2 s after the pump stops, and in the model
  10-25 Torr is inside the turbos by then.
* **Dead gauges (KI-7), fixed 2026-11-16:** left undetected, a dead chamber or foreline gauge on the
  small chamber (real foreline 7e-2 mBar) let the bypass open from the foreline into an evacuated
  chamber.  A gauge below 1 V for 3 s, or frozen for 100 readings, is now error 5014 and a safe
  shutdown.  The logs are what make the simple rule possible: across both chambers the lowest
  voltage any gauge ever put out is 2.7 V (chamber gauges 3.0 / 3.4 V), and away from the ends of
  the input range no gauge repeated a reading more than 10 rows running.
* Everything else passed unchanged: every button in every situation, the random operators, the
  fault matrix, the Admin hand-overs.

## 5. Not answered by the logs

1. Small chamber: how long does the turbo coast after the motor is switched off, and does the
   controller brake it?  (The log ends with it running.)
2. Small chamber: the speed signal reads 96 % the moment the motor starts, falls to 17 % over a
   minute, then climbs to 100 % in six.  Is that the real signal?  Auto reads "Speed Reached" for
   those first seconds.
3. Does the primary pump (either chamber) have an anti-suck-back or isolation valve?  The foreline
   gauge reads atmosphere 6 s after the pump stops.  If the pump trips at full speed the turbos get
   that through their open turbo valves for the 1-2 s the software needs.
4. Have the HiPace pumps (Turbo 2, 3, 5, 6) been checked since 28 Oct and 5 Nov?  Two crash stops
   from speed are in the log; Pfeiffer limits how many a pump may take.
5. The roughing ends at 1.7e-1 mBar in every VC100 log, the profile's turbo-on threshold is
   2.5e-1 mBar.  Is the panel value set to 1.7e-1 on the facility PC?
