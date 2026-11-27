# Response to the protection review of 2026-11-27

Second pass by Claude, 2026-11-28, on the code as it was on Olgu's laptop after the 27 Nov pop-up /
RS-485 fixes.  Rule followed (Olgu): fix what is a software bug; change how Auto behaves only where
that is obviously the safer thing in a fault; ask about the rest.  Simulator only - nothing here was
tried on a facility.

Tests for everything fixed: `tests/test_review_fixes.py`.  The review's own
`tests/test_protection_review.py` was updated: the fixed cases pass, the four strict xfails that are
left carry the reason they are open.

## The most important thing found: a failed DAQ read

Not a finding of the review as such, but found by following F01 ("a missing turbo reading counts as
not spinning") to where a reading really goes missing.  When a DAQ read fails, the backend returns
what it had read up to the failure - often no turbo readings at all.  Reproduced on both profiles
with the turbos at full speed in Pumping to High Vac and ONE failed read:

| Pass | Before | Now |
|---|---|---|
| the failed read | error 5020, Facility Off, **primary pump OFF, chiller OFF, turbo valves shut** | error 5020, Facility Off, gates shut, motors off, **primary, chiller and turbo valves kept** |
| the next | primary and chiller switched on again | turbos slow down backed and cooled - the ordinary error shutdown |

A failed read now keeps the last known turbo, pump, chiller and valve readings.  Pressures are not
kept: a pressure that was not read stays unknown, which every rule treats as "assume atmosphere".

## Finding by finding

| Finding | What was done | Why |
|---|---|---|
| **F01** unknown speed = stopped | **Fixed.** A failed read keeps the last known readings (above).  A speed that is not a usable number (not-a-number, infinite, far below zero) counts as turning, in Auto and in the Manual interlocks.  In Manual a turbo in use with no reading at all counts as turning. | Unknown must never read as "stopped". |
| F01, rotating contact on the small chamber | **Not changed - question for Olgu.** | LabVIEW only displayed this contact.  Used in Auto, a contact that is stuck on would keep pump and chiller running for ever and block roughing.  The engineers have to say what it does first. |
| F01, a reading of -1 % | Not a defect. | One per cent below zero is ordinary noise on the +/-10 V speed input. |
| **F07** Manual question answered after things changed | **Fixed.** The interlock is asked again when the operator answers, with readings taken at that moment (valves, primary, chiller, turbo start).  If it no longer holds, nothing is switched and the reason is shown. | A plain bug in Manual mode's own rules. |
| **F04** unusable threshold | **Fixed.** 'Turbo on Threshold' only takes a real pressure (above 0, up to 1000 Torr).  A profile with not-a-number, infinite, negative or text where a number belongs in `thresholds`, `timings` or `logging` no longer loads. | Bug. |
| **F09** output writes | **Fixed in part.** Each output group (valves, primary, chiller, each turbo) is written even if an earlier one fails; the failures are reported together.  The whole command set was already written again on every pass. | Bug: a failing valve write stopped the "everything off" from reaching pump, chiller and turbos. |
| F09, write error and Auto | Not changed. | A write that fails is logged and shows as error 5020 for that pass only; Auto does not stop for it.  Valves, pump and chiller that then do not follow are caught by the command / read-back conflict errors, which do stop Auto. |
| **Clock** (timing with the time of day) | **Fixed.** Waits, trip delays and timers use the monotonic clock; only the 'Turbo Engage time' is compared with the time of day. | Windows correcting the PC clock could lengthen or shorten a wait or a trip delay. |
| **F12** division by zero in the link check | **Fixed.** | Bug (only in the bring-up tool). |
| **F05**, stale speed on RS-485 | **Flagged, not an error.** A speed not refreshed for `comm_warn_s` is shown as old and not judged by the speed-drop trip. | On 28 Oct pumps answered writes while query replies stalled; taking a healthy turbo out for that would be worse than an old number on the screen. |
| F05, age of a pressure reading | Not changed. | Readings carry no age the interlocks could check.  The way an old pressure really got used - a question left open - is closed by F07. |
| F02 Admin has no interlocks | Not changed. | Decided by Olgu, 18 Nov ("we leave manual and admin mode alone"). |
| F11 no automatic trips in Manual / Admin | Not changed. | Same decision. |
| F03 backing pump believed without feedback (small chamber) | Not changed. | Known as KI-6; accepted by Olgu, 18 Nov ("fine for now").  Switching the frequency reader on closes it. |
| F06 nothing is read during a settle wait | Not changed. | Decided by Olgu, 18 Nov ("keep as is" - as LabVIEW). |
| F08 program start, kill, power loss | Not changed. | Decided by Olgu, 18 Nov ("this will never happen" / "nothing we can do here"). |
| F10 Clear Error | Not changed. | Works as in LabVIEW: a fault that is still there comes back on the next pass. |
| Cooling flow, vent rate, both-ends valve feedback | Not changed. | No such signals exist on the facilities; nothing for software to read. |

## The 29 existing tests that failed in the review's run

They pass here (Linux).  After these fixes: 579 regular tests passed with 4 expected failures (the
open items in the table above), and 660 safety tests passed with 1 expected failure (KI-6).  The review ran on Windows.  Most of the older tests run the
simulated plant against the wall clock (`tests/conftest.py::Harness`), and Windows cannot sleep for
less than about 15 ms: the plant then runs far ahead of the controller.  Making this PC's sleep
behave like Windows' reproduces the same kind of failure here (for example `test_te_requests`:
"bypass opened after 684 s, wanted 20-30 s").  So these are a weakness of the old test harness on
Windows, not 29 defects in the controller - but they do mean the older tests cannot be trusted on a
Windows PC.  The safety tests (`tests/test_safety_*`, `safety_rig.py`) use their own exact clock and
do not have this problem.  Two of the 29 were the F12 division by zero, now fixed.

Offered, not done: move the older tests onto the exact clock so that the whole suite gives the same
result on Windows.
