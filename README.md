# NewOrbit Vacuum Facility Control

Python replacement for the LabVIEW vacuum-chamber facility-control VIs, for all NewOrbit chambers
(small / medium / big). One shared, config-driven application; each chamber is a configuration plus its
own LabVIEW source and notes.

## Layout
- `python_facility_control/` — **the shared control application** (one codebase for every chamber).
  A chamber is selected by its YAML profile in `python_facility_control/config/`, e.g.
  `run.bat --config config/facility_main_v4.4.yaml`. A fix or safety change here helps every chamber.
- `chambers/small/` — the **small chamber** (VC40/VC80-class, single turbo, BigRed D-SUB15).
  LabVIEW source and support VIs in `chambers/small/labview/`; notes in its README.
  Runs on the shared engine with profile `config/facility_main_v4.4.yaml`
  (to be renamed `small_chamber.yaml` once hardware bring-up is finished).
- `chambers/medium/`, `chambers/big/` — VC100 / VC140, added the same way when their VIs are ported.
- `tooling/` — LabVIEW→JSON export tooling (lvkit, `vi2json.py`) used to reverse-engineer the VIs.

## Running (small chamber, lab PC)
See `python_facility_control/README.md`. In PowerShell, from `python_facility_control/`:
`.\run.bat --daq` (real cDAQ) or `.\run.bat --sim` (simulator).
