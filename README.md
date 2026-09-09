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
  Runs on the shared engine with profile `config/facility_main_v4.4.yaml`.
- `chambers/medium/` — the **medium chamber** (VC100, three turbos: Shimadzu contact interface +
  two HiPace D-SUB, chassis cDAQ3). `VC100_Facility_Control_V1.0.vi` + support VIs, the decoded
  block-diagram listings in `analysis/`, panel screenshots, and a README with the differences to the
  small chamber. Runs on the shared engine with profile `config/facility_vc100.yaml`
  (full reference: `python_facility_control/docs/VC100_REFERENCE.md`).
- `chambers/big/` — VC140, to be added the same way when its VI is ported.
- `tooling/` — LabVIEW→JSON export tooling (lvkit, `vi2json.py`) used to reverse-engineer the VIs.

## Running (lab PC)
See `python_facility_control/README.md`. In PowerShell, from `python_facility_control/`:
`.\run.bat --daq` (small chamber, real cDAQ) · `.\run.bat --config config\facility_vc100.yaml --daq`
(medium chamber) · add `--sim` for the simulator on any PC.
