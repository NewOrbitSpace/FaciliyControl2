# Reading Main_V4.4.vi from Python — what's in this folder

## Files

| File | What it is |
|---|---|
| `setup_lvkit.py` | Environment setup. Supports fully offline install. |
| `vi2json.py` | Dumps a `.vi` to nested dataflow JSON (schema `lv-vi-dump/2`). |
| `lvkit-wheels/` | All 32 dependency wheels, CPython 3.12 / win_amd64. No network needed. |
| `Main_V4.4.vi.json` | The generated dump (1.9 MB). |
| `Main_V4.4.describe.txt` | `lvkit describe` output — 2331 lines, human-readable. |
| `Main_V4.4.unresolved.txt` | What the parser could NOT map. |

## Install (no network required)

```powershell
python setup_lvkit.py --offline lvkit-wheels
```

If your network gets fixed later, these also work:

```powershell
python setup_lvkit.py --proxy http://proxy.corp:8080
python setup_lvkit.py --index-url https://<internal-pypi>/simple
python setup_lvkit.py --trusted-host
```

## Regenerate the JSON

```powershell
python vi2json.py Main_V4.4.vi                 # -> Main_V4.4.vi.json
python vi2json.py . -o dump --recurse --hierarchy
python -m lvkit describe Main_V4.4.vi > describe.txt
python -m lvkit unresolved Main_V4.4.vi
python -m lvkit render Main_V4.4.vi            # block diagram -> SVG
```

Noise controls (all reductions are ON by default):
`--keep-void` (keep unwired Void terminals), `--no-inline-consts`
(constants as separate nodes), `--no-wire-list` (drop the flat wire list —
smallest output for pasting to an AI).

## What the dump contains

- `signature` — 55 inputs / 56 outputs, ordered by connector-pane slot, with
  types, defaults and enum members.
- `diagram` — nested: each structure's body sits inside it. While loops carry
  stop-condition polarity and shift-register state; case structures carry each
  frame's selector value; sequences carry step order.
- Nodes carry `op` (resolved operation name), `class` (raw heap class),
  `terminals` (name, direction, type, wire id, inlined constant value).
- `comments` — the 182 free labels, in place inside the frame they annotate.
- `subvi_calls` / `dependencies` — 43 subVIs, 49 dependency records.
- `stats.unresolved_ops` — empty for this VI.

## Parse result for Main_V4.4.vi

792 nodes, 2943 wires, 107 structures (103 case, 2 while, 2 flat sequence),
296 primitives — **all 296 resolved to real names**, 0 unknown primitives,
0 unmapped vi.lib VIs. One gap: a terminal-mapping gap on `Initialize.vi`.

LabVIEW version recorded in the file: 24.0.0.
