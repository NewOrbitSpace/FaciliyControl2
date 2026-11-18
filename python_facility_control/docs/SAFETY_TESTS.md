# Safety tests for Auto mode

Written 2026-11-11, after the turbo-exhaust incident (bypass opened with the turbo valves still open
on a vented chamber).  That bug sat in one cell of a table nobody had walked through: *situation*
"chamber vented, turbos spinning down" x *button* "Pump to High Vac".  These tests walk through the
table, on both facility profiles, and watch the **plant** while they do.

They do not replace hardware interlocks.  Since 2026-11-12 the simulated plant uses numbers measured
from the facilities' own logs (`docs/PLANT_FROM_LOGS.md`: roughing and vent times, turbo run-up and
coast-down, the foreline going to atmosphere within seconds of the primary stopping, a rotor stopped
in seconds by air on its exhaust) - it is still a model (see "Limits").  What the tests give is: for
every operator sequence and every fault the simulator can express, the controller never *commands*
the facility into a state the rules below forbid.

`tools/audit_logs.py` applies the same rules to the CSV logs of the real facility.

## Running them

```
cd python_facility_control
python -m pytest tests -k test_safety_                 # normal level (the default)
set SAFETY_LEVEL=quick& python -m pytest tests -k test_safety_     # while developing
set SAFETY_LEVEL=full&  python -m pytest tests -k test_safety_     # before a release
set SAFETY_STRICT=1&    python -m pytest tests -k test_safety_     # known issues count as failures too
python -m pytest tests -k test_safety_ -rx             # list the known issues (xfail) with their reasons
```

(Linux/macOS: `SAFETY_LEVEL=quick python -m pytest ...`.)  They also run as part of a plain
`python -m pytest`.  Times on a 2-core machine: quick about 6 min, normal about 23 min, full
several hours.  With `pytest-xdist` installed, add `-n auto` - every test builds its own plant, they
are independent.

| file | what it does |
|---|---|
| `tests/safety_rig.py` | deterministic plant + controller (`Rig`) and the **safety monitor** (the rules) |
| `tests/safety_scenarios.py` | the named situations, plant variants, the random operator, the fault catalogue, the known-issue list |
| `tests/test_safety_monitor.py` | every rule fires when the plant is forced into the state it forbids; silent in a normal cycle; runs are repeatable |
| `tests/test_safety_sweep.py` | every Auto button in every situation |
| `tests/test_safety_fuzz.py` | random operators: hours of seeded presses and waits |
| `tests/test_safety_faults.py` | fault injection: every fault x situation, then repair and carry on; detectable faults are noticed |
| `tests/test_safety_handover.py` | Admin -> Auto from line-ups that read as a normal state but sit on a vented chamber / coasting turbos |
| `tests/test_safety_known_issues.py` | the regression tests for every unsafe behaviour these tests found (KI-1..KI-11), and a strict-xfail test for the one that stays open |
| `tests/test_gauge_health.py` | the dead-gauge and frozen-gauge rules themselves: delay, count, clipped signals, switches |
| `tests/test_turbo_speed_drop.py` | the speed-drop trip (error 5015): fires within 3 s on every logged incident trace, on none of the fastest normal falls; not during an expected slow-down (motor off, standby, gate opening, chiller off); the reaction; blanking, bad readings, Manual / Admin |
| `tests/test_high_vac_slow_trip.py` | the second, slower chamber-pressure trip in Pumping to High Vac (1e-2 mBar, 30 s) |
| `tests/test_safe_exit.py` | closing the program: the safe shutdown from every Auto situation and from Admin / Manual line-ups under the monitor; the staged stop after a crash; the loop thread; the panel |
| `tests/test_safety_incidents.py` | the three things that really went wrong on the VC100 (28 Oct, 5 Nov, 9 Nov), replayed from its logs |
| `tests/test_audit_logs.py`, `tools/audit_logs.py` | the rules applied to the facility's CSV logs |

## The rig

`tests/conftest.Harness` advances the simulated plant by wall-clock time x `time_scale`, so how long
a settle wait lasts in plant time depends on how fast the test machine is (at `time_scale` 100 a
1 s valve wait is shorter than the valve's travel - spurious "command/read conflict" errors).  Good
enough for functional tests, not for these.  `Rig` advances the plant by exactly the time the
controller waits and by a fixed loop period (1 s) per iteration.  Same seed, same run, to the last
digit - a failure prints the presses and replays.

The monitor samples the plant every 0.25 s of plant time, **including inside the controller's settle
waits** (the plant moves while the loop is frozen).

## The rules

Plant level - valve *positions*, real pressures, real rotor speeds.  "Turning" = above
`turbo_slow_speed_pct` (5 %).  "Un-roughed" = above 4 x the exposure limit (3 Torr small, 3.75 Torr
VC100).  Grace = how long the condition may last before it counts (valve travel, pump start).

| id | rule | grace | why |
|---|---|---|---|
| T1 | a turning turbo never has more than the high-pressure limit (5 Torr / 5 mBar) inside it | 0 | backing-pressure limit: gas friction overheats the rotor; air inrush can crash it |
| T2 | a turning turbo's inlet (open gate) never sees an un-roughed chamber | 0 | engineer's rule 2026-11-11; "helicopter effect" |
| T3 | a turning turbo's exhaust (open turbo valve) never sees an un-roughed foreline | 0 | engineer's rule; the 2026-11-11 incident |
| T4 | vent valve never open together with the gate of a turning turbo | 0 | venting onto a running rotor |
| T5 | vent and bypass never open together (beyond one valve travel) | 2 s | the roughing line would pump atmosphere |
| T6 | bypass and a gate never open together while that turbo turns | 0 | inlet shorted to exhaust: compression gone |
| T7 | a turbo is never driven without cooling | 15 s | no cooling water -> the pump must not run |
| T8 | a turbo is never driven without a backing path (turbo valve open, primary running) | 20 s | 2026-09-22 incident |
| T9 | a turbo valve is never open on a turning turbo with the primary pump stopped | 1 s | "close the fore-vacuum valve before the backing pump stops"; logs: the foreline is at atmosphere within 6 s of the pump stopping |
| T10 | a turbo motor is never on above the hot-start limit | 3 s | the VIs' "high pressure turbo shut-off"; "evacuate before starting" |
| T11 | the bypass never opens from a higher-pressure foreline into an evacuated chamber | 0 | 2026-09-22 incident (back-flow, contamination) |
| T12 | the chiller is never dropped while a turbo is above the profile's own "still spinning" speed | 15 s | cooling, and where the chiller contactor feeds them, the turbo controllers' supply ("never remove power while it rotates") |
| T13 | the primary pump's run relay is never closed without its power relay (two-relay pump) | 0 | 2026-09-14 wiring |
| T14 | a rotor is never crash-braked by gas: more than 20 % of full speed lost in 5 s | 0 | the symptom itself, whatever the cause - what 28 Oct (98 % -> 18 % in 5 s) and 5 Nov (78 % -> 5 % in 10 s) look like in the log |

Command level - hold whatever the hardware does:

| id | rule |
|---|---|
| K1 | vent never commanded open together with a gate or the bypass |
| K2 | bypass and a gate never commanded open together |
| K3 | a turbo never commanded on without chiller, primary and its turbo valve commanded on |
| K4 | a turbo valve never commanded open on a turning turbo with the primary commanded off |

Sources for the "why" column: the engineer's report of 2026-11-11; the two VIs; Shimadzu TMP-3203
instruction manual (evacuate below 200 Pa before starting; on a pressure rise close the fore-vacuum
and main valves; close the fore-vacuum valve before the backing pump stops; vent only when stopped;
do not remove power while rotating; no cooling water - no operation); Pfeiffer/Leybold application
notes (maximum backing pressure 2-3 hPa for pure turbo stages; air inrush / "helicopter effect").

## What is driven through them

**Situations** (`SITUATIONS`, 20): Facility Off cold; roughing just started / mid / holding / for
high vac; Overnight cold and with turbos spinning; high vacuum at speed and still running up; Turbo
slowing fast and slow; Pump to Rough pressed in high vacuum; Venting with the turbos running (just
started, mid, at atmosphere) and coasting at atmosphere (the 2026-11-11 state, by both roads); vent
finished and holding; Facility Off after a vent; Facility Off with the rotors still coasting.

**Plants** (`PLANTS`): the profile's own, which is the plant measured from the logs (VC100: HiPace
pumps coast ~45 min, Shimadzu pumps brake to a stop in ~9.5 min, roughing ~6 min); `long_coast`
(every turbo coasts for an hour, none brakes - a rotor is always still turning when the pressure
arrives; the small chamber's coast time is not measured yet); `fast_rough` (a roughing line four
times stronger).  In all of them **no turbo controller trips by itself** when gas hits the rotor: on
both real occasions the HiPace pumps only reported a warning.

**Operators**: `visible` (only what the page shows), `impatient` (seconds apart, double presses),
`any` (any Auto action at any moment - a script, a stale click).

**Faults** (`fault_catalogue`): primary pump stops; chiller stops; one / all turbos in error; turbo
warning (contacts); RS-485 link lost; power cut; big leak; air inrush; bypass / vent / turbo valve /
gate stuck; slow gate; chamber / foreline / turbo gauge dead; the same three frozen; compressed air lost.

## What the tests found, and what was done about it

Found 2026-11-11.  Fixed 2026-11-12 unless it says otherwise.  Every row has its regression tests in
`test_safety_known_issues.py` under the same number.

| id | chamber | what used to happen | now |
|---|---|---|---|
| KI-1 | both | Facility Off switched the chiller off with the turbos at ~100 % - on **every** error shutdown, and when Auto was handed coasting turbos from Admin. | The chiller stays on while any turbo is above the "still spinning" speed (global rule 3, with the primary and the turbo valve). |
| KI-2 | small | "Shut Off" on the Pumping to Rough page went straight to Facility Off, turbo still coasting. | `auto_mode.shut_off_spins_down_first: true` - through Turbo slowing, as on the VC100. |
| KI-3 | small | Venting / Overnight / Turbo slowing opened the turbo valve of a turning turbo without looking at the foreline: a few Torr right after the bypass closed, **atmosphere** after a program restart or an Admin hand-over with a coasting turbo. | A turbo valve never *opens* onto a turning turbo until the primary runs and the foreline is pumped down (global rule 3b); meanwhile that turbo is not started and its gate not opened. |
| KI-4 | both | The primary pump stopped with turbos turning: the turbo valves stayed open onto the dead foreline until a turbo tripped. | Primary not running (read-back / frequency / run relay) **or** foreline above `turbo_foreline_max` with the bypass shut -> turbo valves shut, motors off, **error 5012**, Auto stops (global rule 1b). On the small chamber the foreline gauge is what tells. |
| KI-5 | both | The chamber came up in Pumping to High Vac (leak, hand vent valve, seal): the gates stayed open. | Chamber above `high_vac_pressure_trip_mbar` (1e-1 mBar; counts once the chamber has pumped down below half of it - until then the exposure limit) for `pressure_trip_delay_s` -> gates closed, **error 5013**, Auto stops; the turbos keep backing and cooling while they slow. |
| KI-9 | small | Admin hand-over of a High Vac line-up on a vented chamber: the turbo was started at 5 Torr with the gate open. | Refused: gate and turbo valve closed, error 5013 / 5012. |
| KI-10 | small | Found 2026-11-12 by the random operator on the path the KI-2 fix opened: turbo coasting isolated while roughing, Shut Off, then Pump to High Vac on the Turbo slowing page - the motor was restarted for one loop behind its shut turbo valve. | The motor follows the valve (global rule 4): never driven behind a shut turbo valve; Engage Turbo starts it. |
| KI-11 | both | Found 2026-11-12 on the plant measured from the logs: a foreline over its limit shut only the turbo valve and left the gate to the error shutdown one loop later.  If the cause is a chamber leak (the turbo pushing more gas into the foreline than the pump takes - on the small chamber's roughing line that reaches the limit before the chamber gauge does) the rotor kept taking gas through its open gate with nowhere to put it. | A backing-lost trip shuts the gate and the turbo valve in the same command frame. |
| KI-7 | both | A dead pressure gauge was believed: a dead chamber gauge blinded the High Vac leak trip, a dead foreline gauge the backing-lost check, a dead turbo gauge the hot-start shut-off. | **Fixed 2026-11-16.** A watched gauge (chamber, foreline, turbo gauges of the turbos in use) is in fault when its output is below `gauge_dead_below_volts` (1 V) for `gauge_fault_delay_s` (3 s), or when `gauge_frozen_readings` (100) readings in a row are exactly equal (not judged within 0.1 V of the ends of the input range, where a clipped signal repeats).  Pop-up, **error 5014**, Auto stops; the reading counts as not readable while the fault lasts; the error clears by itself when the gauge reads properly again. `tests/test_gauge_health.py`. |

Still open by decision; its test is `xfail(strict=True)`:

| id | chamber | what | why it stays |
|---|---|---|---|
| KI-6 | small | A stopped primary pump is not noticed **while no turbo is turning** (roughing from atmosphere just stalls). | The pump has no feedback unless the frequency reader is on, and it is not always fitted. With a turbo turning it *is* noticed now, through the foreline gauge (KI-4). |

`KNOWN_ISSUES` in `safety_scenarios.py` is empty: no violation is tolerated except what a
fault makes physically unavoidable for the few seconds it takes to react (`UNAVOIDABLE`, `REACT_S`
= 15 s) - the backing pump stops and the foreline comes up before a valve can move; a seal lets go
and the rotors are in air.

### Things on the real facility to check before relying on the new protections

They were written against the simulator.  Each can stop a test if its threshold is wrong for the
real plant, so walk through them once with the facility idle:

* **`gauge_dead_below_volts`** (1 V) and **`gauge_frozen_readings`** (100): checked against every logged
  reading - no gauge ever below 2.7 V, never more than 10 equal rows running away from the ends of the
  input range.  A new gauge type whose live signal can be near 0 V needs its own `dead_below_volts`.
* **`turbo_foreline_max_mbar`** (3 mBar on both chambers; the logs show at most 3.9e-1 mBar): if a test
  with a large gas flow legitimately takes the foreline above that with the turbos running, raise it.
* **`high_vac_pressure_trip_mbar`** (1e-1 mBar; normal work - firing, pulling vacuum on new lines - stays
  below 1e-2 mBar) and **`pressure_trip_delay_s`** (2 s): raise the delay if the chamber gauge
  misreads for longer than that while it switches range.
* **`high_vac_pressure_slow_trip_mbar`** (1e-2 mBar) for **`pressure_slow_trip_delay_s`** (30 s),
  2026-11-17: in the logs the chamber never came back above 1e-3 mBar once it had been below that.  If a test
  deliberately holds the chamber above 1e-2 mBar with the gates open for longer than 30 s, raise the
  level or the delay for that test.
* **`turbo_speed_drop_pct`** (5 % of full speed in 1 s - the test engineer's figure) and
  **`turbo_speed_crash_pct`** (30 % in 5 s), 2026-11-17.  The logs have a row every 5 s, so the fast
  rule is only bounded by them: nothing normal lost 5 % even within 10 s (the fastest: 2.8 % in 5 s
  after a gate opened onto a coasting turbo; 0.6 % in 5 s after a motor was switched off).  What the
  logs cannot show is a one-second dip.  If it ever fires on a normal action, the log line names
  the turbo and the two speeds: raise `turbo_speed_drop_expected_s` if it follows something the
  program did, `turbo_speed_drop_pct` if not, or set it to `null` to keep the 30 % rule alone.
  The small chamber's speed signal at a motor STOP has not been logged - until it has, the 120 s
  after a stop are not judged there.

## When a test fails

The message lists each violation - rule, plant time, Auto state, the numbers, how long it lasted,
the fault injected, the operator's last press - and the last presses.  To replay:

```python
from tests.test_safety_fuzz import replay
r = replay("small", "long_coast", "impatient", seed=7, hours=2.0)
print(r.monitor.report()); print(r.history())
```

For a sweep or fault cell: `from tests.safety_scenarios import make; r = make("vc100",
"venting_coasting_atm"); r.press("pump_to_high_vac"); r.run(1200); print(r.monitor.report())`.

## Adding to them

* a rule: `RULES` + one `_flag(...)` line in `SafetyMonitor.sample`, and a `case_<id>` in
  `test_safety_monitor.py` (the test `test_every_rule_has_a_case_that_breaks_it` insists);
* a situation: a `sit_...` function in `safety_scenarios.py` + its entry in `SITUATIONS` - the sweep,
  the random operator and the fault matrix pick it up;
* a fault: an entry in `fault_catalogue`.

## Limits

* The plant model is lumped volumes with one gas node per turbo, no temperatures, no bearing model.
  Its times and pressures are fitted to the logs (`docs/PLANT_FROM_LOGS.md` has the comparison) for
  the VC100; for the small chamber there is one logged pump-down and no coast-down, so its turbo
  coast time is a guess and the `long_coast` plant is the one to trust.  *Which valve is open onto
  what while a rotor turns* is exact.
* A primary pump that stops at full speed puts gas into the turbos before any valve can move (the
  foreline is at atmosphere within seconds).  The tests accept that part (T1, T14) and insist only
  that the turbo valves are shut within 15 s.  Preventing it needs hardware.
* Rules T1-T3 use the profile's own thresholds, not manufacturer limits for the actual pumps.
* The control loop runs at 1 s per iteration here (0.1 s on the facility): reactions are a little
  slower than real.  Settle waits are exact.
* With `blocking_waits: true` (both facilities, the VI's behaviour) nothing is read during a settle
  wait - up to 9 s after a gate moves.  No protection, old or new, acts inside one.
* Only Auto mode is tested here.  Manual-mode interlocks are in `test_admin_manual.py` and
  `test_turbo_exposure.py`; Admin mode has none by design.
* A fault the simulator cannot express is not tested: gauge drift (as opposed to failure), a valve
  that leaks when shut, a partially failed pump, a chiller that runs without flow.
