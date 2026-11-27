# Verification evidence and reproduction

Hardware verified: **NONE**. No control application, real NI driver or physical serial port was opened. Production code/configuration unchanged. New tests use records, a recording backend, or an in-memory serial simulator.

New review suite: **13 passed, 15 strict expected failures** (`new-tests.xml`). Expected failures represent **15 unsatisfied requirement cases**, not accepted safe behavior. Running with `--runxfail` produced **15 failed, 13 passed** (`requirements-failures.xml`); all failures reached their intended assertions. These cases cover F01, F02, F03, F04, F05 and F07. Passing cases include characterization of known gaps and therefore must not all be counted as satisfied protection requirements.

The new tests include a 500-request generated Manual valve-state sequence (seed 615083) at atmospheric chamber pressure with a full-speed rotor. The invariant prohibits gate opening. Replacing the interlock with unconditional permission in memory makes the same checker fail, as required by the mutation test. This is a bounded state-machine property test, not exhaustive exploration or a physical vent-rate model.

Independent existing safety-monitor run: **42 passed, 1 skipped in 15.35 seconds** (`monitor.xml`). This includes normal-cycle and reproducibility checks on the deterministic plant plus deliberate rule violations to check the monitor detects them. It verifies the monitor within its modeled scope, not manufacturer limits or hardware.

Standalone deterministic controller/plant evidence: **3 passed in 9.61 seconds** (`selected-state-faults.xml`): `test_random_operator[small-long_coast-any-0-1.5]` and `test_a_stuck_open_gate_is_never_vented_onto` for both chamber profiles. This preserves independently completed evidence for a 1.5-hour seeded simulated command sequence and stuck-open-gate protection after the broad attempt was stopped. The existing fault oracle limitations below still apply.

Standalone telemetry/I/O batch: **64 passed, 2 failed, 1 skipped in 8.92 seconds** (`telemetry-io.xml`). Files: `test_hipace_rs485.py`, `test_nidaqmx_backend.py`, `test_gauge_health.py`, `test_gauges_units.py`. Failures are both link-validation `ZeroDivisionError` at `drivers/pfeiffer_pv.py:722` (F12). Protocol corruption/timeout and fake NI decoding cases in this batch passed. GUI-related case skipped. This remains simulation/mock evidence only.

The restart characterization covers all nine `FacilityState` enum values; every fresh controller loses the prior state and initializes all commands off. It does not test true process termination, drive memory, DAQ retention or each substate.

The broad attempt was stopped after progressing past the 77% batch into legacy turbo-exposure startup loops. `suite-output.txt` and `interrupted-stack.txt` preserve its partial evidence. It **did not complete and produced no final suite XML**; no aggregate pass claim is made. Fault, seeded-operator, handover and transition-sweep sections had completed; later legacy exposure cases were not completed. These legacy cases allow 400,000 startup iterations per condition, and repeated startup failures prevented useful progress to their hazard scenarios. Remaining control/interface files were run separately; final execution summaries are appended below. An early diagnostic rerun of `test_admin_manual.py` and `test_automode.py` produced 8 failures / 9 passes (`early-failures.xml`). These include failure to reach high vacuum, bypass command/read mismatch and manual-start expectations. They are unresolved existing-suite failures, not automatically eight independent production defects. The legacy `Harness` advances the simulated plant with wall time and shortened sleeps; `safety_rig.Rig` and the new review tests instead use controlled clocks. Environment-sensitive timing and stale expectations require triage rather than adjusting protection settings to make tests pass.

## Requirement-to-test mapping

`legacy-diagnostic.txt` records one standalone legacy pump-down failing first with error 5001 (turbo-valve command/read mismatch), then routing to Facility Off. The timing harness is a plausible contributor, not a proved root cause of every broad-suite failure. No assertions were weakened or protection settings changed to obtain passes.

The long-run stack (`long-run-stack.txt`, captured using py-spy 0.4.4 against only this review's Python process) showed bounded `Harness.run_until` startup loops, not an unresponsive hardware call. `test_spinning_turbo_safety._to_high_vac` allows 40,000 / 100,000 / 300,000 iterations for successive startup conditions. Such tests can spend substantial wall time when the expected state is not reached. The RS485 thread visible in that stack belongs to mocked backend tests; physical serial access remained blocked by the runner.

| Requirement | Executed/planned evidence | Limit or open issue |
|---|---|---|
| R01 | new `test_R01_restart_loses_state_without_reconciliation` (all nine enums); existing safe-exit tests | Characterization confirms missing recovery; physical/process restart incomplete |
| R02 | existing `test_spinning_turbo_safety`, `test_safe_exit`, `test_engage_order`, `test_safety_sweep` | Profile limits unapproved; broad-run failures need triage |
| R03 | new `test_R03_missing_backing_feedback_is_not_running`; existing backing-loss/safety-fault/primary-two-relay tests | New requirement FAIL; no independent hardware protection established |
| R04 | unknown-speed cooling removal test; existing chiller-fault campaign | Missing/invalid speed FAIL; no flow/thermal model, temperature faults untested |
| R05 | new configured boundary and atmospheric-exposure cases; existing turbo-exposure/high-vac trip tests | Numeric limits are settings only; unknown rotor exposure FAIL |
| R06 | existing venting and stuck-gate simulations | Actual vent-rate requirement unresolved and unverified |
| R07 | new missing/NaN/negative speed, stale-pressure, rotating-contact cases; existing gauge-health, gauges/units and speed-drop tests | New requirement cases FAIL; calibration drift and every malformed input not covered |
| R08 | new partial-write characterization; existing fake NI and stuck-valve tests | Partial write reported but mitigation unproved; contradictory endpoints/leakage unavailable in model |
| R09 | new partial-serial-success test (10 controlled seconds with speed replies absent); existing checksum/noise/timeout/retry tests | New stale-speed requirement FAIL; complete hardware bus timing unverified |
| R10 | new enum restart characterization; existing crash/safe-exit/power-cut simulations | Hard kill, power retention and independent protection unverified |
| R11 | new Admin, delayed Manual confirmation, generated sequences and mutation; existing seeded fuzz, handover, sweep | New Admin and confirmation requirements FAIL; finite seeds, incomplete concurrent interleavings |
| R12 | new invalid threshold API cases; existing config and units tests | New numeric validation cases FAIL; complete configuration constraints unresolved |
| R13 | existing fault-clear/recovery sequences; static reset review | Persistent-fault clear/queued commands not independently verified; no recovery state |

## Reproduce

From `python_facility_control`, using Python 3.12 and the dependencies below:

```powershell
python tools/run_protection_review.py tests/test_protection_review.py -q -rxX
python tools/run_protection_review.py tests/test_protection_review.py --runxfail -q --tb=short
$env:SAFETY_LEVEL = 'quick'
python tools/run_protection_review.py tests -q --tb=short --junitxml=docs/protection_review/suite.xml
```

The second command intentionally exits nonzero until open defects are corrected. The runner prepends workspace `.review-deps` when present and disables third-party pytest plugin autoload. It blocks real `nidaqmx` imports and `serial.Serial` / `serial_for_url`; fake modules/injected serial factories remain available. Do not substitute the application launchers: they can select real hardware by default.

This host used the bundled interpreter at `C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1119-907-0_26.1119.907.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe`. Python was not on PATH. Dependencies were installed only in workspace `.review-deps`: pytest 9.2.0, PyYAML 6.0.4, numpy 2.5.5, pyserial 3.5, colorama 0.4.6, iniconfig 2.3.1, packaging 26.4, pluggy 1.7.0, pygments 2.21.0. PySide6 was not installed: GUI tests are skipped, not verified. `source-hashes.json` identifies reviewed production/config files.

The broad run collected an earlier revision of the new test file; the separate final new-test XML is authoritative for all 28 new cases. Do not add counts across overlapping runs. No code-coverage claim is made.

## Final completed control/interface batch

**126 passed, 19 failed, 2 skipped in 601.29 seconds** (`remaining.xml`, `remaining-output.txt`). Files: `test_te_requests.py`, `test_turbo_speed_drop.py`, `test_vc100.py`, `test_vc100_gate_stagger.py`, `test_vc100_new_tmps.py`, `test_vc100_turbo_speed.py`.

Failures cover primary/bypass order, slowing/overnight/vent state expectations, alarm/warning and chiller-independent error handling, compressor timing, gate sequencing, shared backing valves and high-vac startup. Some explicitly stop with error 5001 (turbo-valve command/read mismatch); others fail different state, error or log assertions. They are **not all explained by timing** and are not dismissed as flaky. Their full root-cause triage remains open. Every assertion and traceback is retained in the output/XML. No protection values or existing assertions were changed.

This batch, the early diagnostic batch and telemetry/I/O batch contain 29 existing-test failures across distinct test cases. That is not 29 established production defects. F12 is independently identified from two of those failures; the other existing failures require further diagnosis. The 15 new failing requirement cases are separate, verified by `--runxfail`, and grouped by root finding in the report.

Reproduce completed evidence individually with the same runner, for example:

```powershell
python tools/run_protection_review.py tests/test_safety_monitor.py -q
python tools/run_protection_review.py 'tests/test_safety_fuzz.py::test_random_operator[small-long_coast-any-0-1.5]' tests/test_safety_faults.py::test_a_stuck_open_gate_is_never_vented_onto -q
python tools/run_protection_review.py tests/test_hipace_rs485.py tests/test_nidaqmx_backend.py tests/test_gauge_health.py tests/test_gauges_units.py -q
python tools/run_protection_review.py tests/test_te_requests.py tests/test_turbo_speed_drop.py tests/test_vc100.py tests/test_vc100_gate_stagger.py tests/test_vc100_new_tmps.py tests/test_vc100_turbo_speed.py -q
```

Acceptance status: **open protection defects; unresolved existing-test failures; incomplete broad run; no hardware verification**. Full/normal safety-campaign levels, remaining legacy exposure cases, GUI, real process/power restart, temperature/flow protection, vent rate and equipment-specific timing remain unverified. These results provide review evidence, not proof of safety.
