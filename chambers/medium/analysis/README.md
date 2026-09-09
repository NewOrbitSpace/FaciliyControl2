# How the VC100 VI was read (reproducible)

The port of `VC100_Facility_Control_V1.0.vi` was made from the block diagram, not from screenshots.
Steps (Python 3.11, tooling in `../../../tooling/`):

1. `python tooling/vi2json.py chambers/medium/VC100_Facility_Control_V1.0.vi --no-inline-consts --keep-void`
   → `VC100.vi.json` (nodes + wires) and `VC100.describe.txt` (lvkit `describe`, included here).
2. pylabview `readRSRC.py -x` → the heap XML (`VC100_FPHb.xml`, `VC100_BDHb.xml`, `VC100.xml` with
   the VCTP/DTHP type tables).  Not included (24 MB) – regenerate when needed.
3. `trace.py` – follows every `DAQmx Create Virtual Channel` back to its physical-channel string and
   task chain → the pinout table in `docs/VC100_REFERENCE.md`.
4. `frames.py <case uid...>` – prints, for a case structure, what each output tunnel carries in each
   frame (constants decoded, local variables named via `refmap.json`, front-panel terminals via the
   FP heap, cluster fields via the type table, shift registers named).  `auto_frames.txt` is the
   result for the Auto state case (38253) and its substate cases; `mode_frames.txt` the Admin/Auto
   mode case (1924); `sub_frames.txt` the Turbo-1 cluster and helper cases.
   `python -c "import frames; frames.explain_node('<uid>')"` explains one node.
5. `fp_rows.json` – front-panel controls (label, class, typedef index) used for the defaults table.

Key uids: 38253 Auto state case · 29946 Rough substates · 29718 Engage substates · 31816 Disengage ·
31021 Venting timer · 1924 mode case · 12494 Turbo 1 cluster / reset dialog · 21728 Turbo 1 status ·
57146 Turbo 2/3 status loop · 63053 Turbo 1 alarm error · 46252 compressor check · 16405/28910/62580 waits.
