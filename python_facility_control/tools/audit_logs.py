"""Read the facility's CSV logs and report what the turbos have been through.

    python tools/audit_logs.py logs/facility_vc100_2026-11-*.csv
    python tools/audit_logs.py --config config/facility_main_v4.4.yaml logs/*.csv

The same rules the safety tests hold the simulator to, applied to what really happened.  It finds

  CRASH     a rotor that lost more than 20 % of full speed between two log rows (air on a turning
            rotor - both real incidents on the VC100 look exactly like this)
  EXHAUST   a turning turbo with its turbo valve open onto a foreline above the exposure limit
  INLET     a turning turbo with its gate open onto a chamber above the exposure limit
  BACKING   a turning turbo with its turbo valve open and the primary pump commanded off
  LEAK      Pumping to High Vac with the chamber above the trip level (after it had been below half)
  FORELINE  a turning turbo, bypass shut, turbo valve open, foreline above the backing-lost limit
  GATES     Engage Turbo closing a gate again that it had already opened
  GAUGE     a watched pressure gauge below the "dead" voltage (1 V)
  COOLING   (information) the chiller switched off with a turbo still above the slowing speed
  ERROR     (information) every error the controller raised

and prints the highest foreline and chamber pressures seen with the turbos running, which is what
the trip levels in the profile have to stay clear of.  Exit code 1 if anything above COOLING is found.

The profile is taken from the file name (facility_<id>_<date>.csv) unless --config is given.
Standard library only - it runs on the facility PC as it is.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from facility_control.config import CONFIG_DIR, FacilityConfig, load_config  # noqa: E402
from facility_control.units import torr_to  # noqa: E402

CRASH_PCT = 20.0          # % of full speed lost between two rows no more than MAX_GAP_S apart
MAX_GAP_S = 12.0
SERIOUS = ("CRASH", "EXHAUST", "INLET", "BACKING", "LEAK", "FORELINE", "GATES", "GAUGE")


@dataclass
class Episode:
    kind: str
    start: str
    end: str
    where: str
    what: str
    rows: int = 1


@dataclass
class Report:
    episodes: List[Episode] = field(default_factory=list)
    rows: int = 0
    max_foreline_running_mbar: float = 0.0
    max_foreline_running_at: str = ""
    max_chamber_high_vac_mbar: float = 0.0        # after the chamber first reached 1e-4 mBar in that stay
    max_chamber_high_vac_at: str = ""

    @property
    def serious(self) -> List[Episode]:
        return [e for e in self.episodes if e.kind in SERIOUS]


def _f(row: dict, key: str, default: float = float("nan")) -> float:
    try:
        return float(row.get(key, ""))
    except (TypeError, ValueError):
        return default


def _b(row: dict, key: str) -> bool:
    return str(row.get(key, "0")).strip() in ("1", "True", "true")


def profile_for(path: str) -> Optional[str]:
    """facility_<id>_<date>.csv -> the config whose facility id is <id>."""
    name = os.path.basename(path)
    if not name.startswith("facility_"):
        return None
    fid = name[len("facility_"):].rsplit("_", 1)[0]
    for cfg_path in sorted(glob.glob(str(CONFIG_DIR / "*.yaml"))):
        try:
            if load_config(cfg_path).id == fid:
                return cfg_path
        except Exception:
            continue
    return None


def audit(rows: Iterable[dict], cfg: FacilityConfig) -> Report:
    th = cfg.thresholds
    limit = torr_to("mbar", th.turbo_exposure_max_torr if th.turbo_exposure_max_torr is not None
                    else th.turbo_on_wiggle_room_multiplier * th.engage_threshold(th.turbo_on_threshold_torr))
    fore_limit = torr_to("mbar", th.turbo_foreline_max_torr) if th.turbo_foreline_max_torr is not None else limit
    trip = torr_to("mbar", th.high_vac_pressure_trip_torr) if th.high_vac_pressure_trip_torr is not None else limit
    slow = th.turbo_slow_speed_pct
    wrg_col = f"{cfg.main_gauge.id}_mbar"
    fore_col = f"{cfg.foreline_gauge.id}_mbar" if cfg.foreline_gauge is not None else None
    bypass_ids = [v.id for v in cfg.valves_of_kind("bypass")]
    gates = sorted({t.gate_valve for t in cfg.turbos})
    watched = []
    for g in [cfg.main_gauge, cfg.foreline_gauge] + [cfg.turbo_gauge(t.id) for t in cfg.turbos if t.in_use]:
        if g is not None and g not in watched:
            watched.append(g)
    rep = Report()
    open_ep: Dict[tuple, Episode] = {}
    seen_now: set = set()

    def flag(kind: str, who: str, row: dict, what: str) -> None:
        key = (kind, who)
        seen_now.add(key)
        ep = open_ep.get(key)
        if ep is None:
            ep = Episode(kind, row.get("iso_time", "?"), row.get("iso_time", "?"),
                         f"{row.get('mode', '?')} / {row.get('current_state', '?')}", what)
            open_ep[key] = ep
            rep.episodes.append(ep)
        else:
            ep.end = row.get("iso_time", "?")
            ep.rows += 1

    prev: Optional[dict] = None
    hv_armed = False
    hv_base = False
    engage_opened: set = set()
    for row in rows:
        rep.rows += 1
        seen_now.clear()
        t = _f(row, "timestamp")
        gap_ok = prev is not None and 0 < t - _f(prev, "timestamp") <= MAX_GAP_S
        wrg = _f(row, wrg_col)
        fore = _f(row, fore_col) if fore_col else float("nan")
        state = row.get("current_state", "")
        bypass_open = any(_b(row, f"{b}_cmd") or _b(row, f"{b}_read") for b in bypass_ids)
        any_turning = False
        for tc in cfg.turbos:
            if not tc.in_use:
                continue
            tid = tc.id
            if tc.has_speed:
                spd = _f(row, f"{tid}_speed_pct", 0.0)
                turning = spd > slow
                shown = f"{spd:.0f} %"
                if gap_ok:
                    before = _f(prev, f"{tid}_speed_pct", 0.0)
                    if before - spd > CRASH_PCT:
                        flag("CRASH", tid, row, f"{tc.label}: {before:.0f} % -> {spd:.0f} % in {t - _f(prev, 'timestamp'):.0f} s "
                                                f"(foreline {fore:.1e} mBar, chamber {wrg:.1e} mBar)")
            else:
                turning = _b(row, f"{tid}_rotating") or _b(row, f"{tid}_motor_cmd")
                shown = "rotating"
            any_turning = any_turning or turning
            if not turning:
                continue
            tv_open = _b(row, f"{tc.turbo_valve}_cmd") or _b(row, f"{tc.turbo_valve}_read")
            gate_open = _b(row, f"{tc.gate_valve}_cmd") or _b(row, f"{tc.gate_valve}_read")
            exposed = False
            if tv_open and fore > limit and bypass_open:
                exposed = True
                flag("EXHAUST", tid, row, f"{tc.label} at {shown}: turbo valve open, bypass open, foreline {fore:.1e} mBar")
            elif tv_open and fore > fore_limit:
                exposed = True
                flag("FORELINE" if _b(row, "primary_cmd") else "EXHAUST", tid, row,
                     f"{tc.label} at {shown}: turbo valve open onto the foreline at {fore:.1e} mBar")
            if gate_open and wrg > limit:
                flag("INLET", tid, row, f"{tc.label} at {shown}: gate open onto the chamber at {wrg:.1e} mBar")
            if tv_open and not _b(row, "primary_cmd"):
                flag("BACKING", tid, row, f"{tc.label} at {shown}: turbo valve open, primary pump commanded off")
            if tc.has_speed and _f(row, f"{tid}_speed_pct", 0.0) > th.slowing_pct and not _b(row, "chiller_cmd"):
                flag("COOLING", tid, row, f"{tc.label} at {shown}: chiller off")
            if (tv_open and not exposed and not bypass_open and _b(row, "primary_cmd") and fore == fore
                    and fore > rep.max_foreline_running_mbar):
                rep.max_foreline_running_mbar, rep.max_foreline_running_at = fore, row.get("iso_time", "?")
        # ---- Pumping to High Vac: the chamber after it has pumped down
        if state == "Pumping to High Vac" and row.get("mode") == "AUTO":
            if wrg < 0.5 * trip:
                hv_armed = True
            if wrg < 1e-4:
                hv_base = True
            if hv_armed and wrg > trip:
                flag("LEAK", "chamber", row, f"chamber at {wrg:.1e} mBar in Pumping to High Vac (trip level {trip:.1e} mBar)")
            if hv_base and wrg > rep.max_chamber_high_vac_mbar:
                rep.max_chamber_high_vac_mbar, rep.max_chamber_high_vac_at = wrg, row.get("iso_time", "?")
        else:
            hv_armed = hv_base = False
        # ---- Engage Turbo: a gate that was opened is not closed again
        if state == "Engage Turbo" and row.get("mode") == "AUTO":
            now_open = {g for g in gates if _b(row, f"{g}_cmd")}
            closed_again = engage_opened - now_open
            if closed_again and not now_open:        # (one gate closed again = a stuck gate taken out of use)
                flag("GATES", "engage", row, f"Engage Turbo closed {len(closed_again)} gate(s) again it had opened "
                                             f"(chamber {wrg:.1e} mBar) - turbos left in standby behind closed gates")
            else:
                engage_opened |= now_open
        else:
            engage_opened = set()
        # ---- a watched gauge with no signal
        for g in watched:
            floor = g.dead_below_volts if g.dead_below_volts is not None else th.gauge_dead_below_volts
            v = _f(row, f"{g.id}_volts")
            if floor and v == v and v < floor:
                flag("GAUGE", g.id, row, f"{g.label}: {v:.2f} V (a live gauge reads above {floor:g} V)")
        if _b(row, "error_status"):
            src = re.sub(r"for \d+ s", "for .. s", str(row.get("error_source", "")))
            src = re.sub(r"\(\d+\.\d+ bar\)", "", src)
            flag("ERROR", str(row.get("error_code", "")) + src, row, f"{row.get('error_code', '')}: {src[:90]}")
        for key in [k for k in open_ep if k not in seen_now]:
            del open_ep[key]
        prev = row
    return rep


def read_rows(paths: Iterable[str]) -> Iterable[dict]:
    for path in paths:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                yield row


def format_report(rep: Report, title: str = "") -> str:
    lines = [f"{title}{rep.rows} log rows"]
    if not rep.episodes:
        lines.append("  nothing found")
    order = {k: i for i, k in enumerate(SERIOUS + ("COOLING", "ERROR"))}
    merged: Dict[tuple, List[Episode]] = {}
    for e in rep.episodes:
        merged.setdefault((e.kind, e.what if e.kind == "ERROR" else ""), []).append(e)
    for (kind, _), eps in sorted(merged.items(), key=lambda kv: (order.get(kv[0][0], 99), kv[1][0].start)):
        if kind in ("COOLING", "ERROR"):
            lines.append(f"  {kind:8s} {len(eps):3d} x   first {eps[0].start}   {eps[0].what}")
            continue
        for e in eps:
            span = e.start if e.rows == 1 else f"{e.start} .. {e.end[-8:]}"
            lines.append(f"  {kind:8s} {span}   [{e.where}]   {e.what}")
    if rep.max_foreline_running_mbar:
        lines.append(f"  highest foreline with a turbo running, bypass shut (events above excluded): {rep.max_foreline_running_mbar:.1e} mBar "
                     f"({rep.max_foreline_running_at})")
    if rep.max_chamber_high_vac_mbar:
        lines.append(f"  highest chamber pressure in Pumping to High Vac once pumped down: "
                     f"{rep.max_chamber_high_vac_mbar:.1e} mBar ({rep.max_chamber_high_vac_at})")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", help="facility CSV logs (wildcards allowed)")
    ap.add_argument("--config", "-c", default=None, help="facility YAML (default: from the file name)")
    args = ap.parse_args(argv)
    paths: List[str] = []
    for pat in args.files:
        paths += sorted(glob.glob(pat)) or [pat]
    by_cfg: Dict[str, List[str]] = {}
    for p in paths:
        cfg_path = args.config or profile_for(p)
        if cfg_path is None:
            print(f"{p}: cannot tell which facility this is - give --config")
            return 2
        by_cfg.setdefault(cfg_path, []).append(p)
    bad = 0
    for cfg_path, group in by_cfg.items():
        cfg = load_config(cfg_path)
        rep = audit(read_rows(group), cfg)
        print(format_report(rep, f"{cfg.name}: {len(group)} file(s), "))
        bad += len(rep.serious)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
