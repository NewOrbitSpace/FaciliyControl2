#!/usr/bin/env python3
"""vi2json.py - dump a LabVIEW VI's block diagram to AI-readable JSON.

Reads .vi binaries directly via lvkit/pylabview. No LabVIEW installation and no
VI Scripting needed. Output is a nested dataflow graph: signature, structures
(loops / cases / sequences / event structures) with their bodies nested inside
them, nodes with resolved names and named+typed terminals, wires as edges,
inlined constants, comments in place, and dependencies. Canvas coordinates,
colours and fonts are deliberately discarded.

Usage
-----
    python vi2json.py MyVI.vi                       # -> MyVI.vi.json beside it
    python vi2json.py MyVI.vi -o dump/              # -> dump/MyVI.vi.json
    python vi2json.py src/ -o dump/                 # every .vi under src/
    python vi2json.py MyVI.vi --recurse -o dump/    # follow local subVI deps
    python vi2json.py MyVI.vi --stdout              # print, don't write
    python vi2json.py src/ -o dump/ --hierarchy     # also dump/_hierarchy.json

Noise control (all on by default, disable if you need full fidelity):
    --keep-void          keep unwired Void terminals (LabVIEW pads subVI panes)
    --no-inline-consts   emit constants as their own nodes instead of inlining
    --no-wire-list       omit the top-level flat wire list (terminals keep ids)

Exit code is 1 if any VI failed to parse.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    from lvkit import parse_vi
    from lvkit.primitive_resolver.resolver import PrimitiveResolver
except ImportError:  # pragma: no cover
    sys.exit("lvkit is not installed. Run:  python setup_lvkit.py")

SCHEMA = "lv-vi-dump/2"

# Node attributes carried through when the node subclass has them.
EXTRA_NODE_FIELDS = (
    "vi_path", "poly_variant_name", "operation", "loop_type", "script",
    "object_name", "method_name", "method_code", "delay_depth", "param_idx",
)

# Friendly names for heap classes the primitive tables don't cover.
CLASS_NAMES = {
    "iUse": "SubVI",
    "polyIUse": "SubVI (polymorphic)",
    "dynIUse": "SubVI (dynamic dispatch)",
    "select": "Case Structure",
    "case": "Case Structure",
    "loop": "Loop",
    "flat_sequence": "Flat Sequence",
    "stacked_sequence": "Stacked Sequence",
    "event": "Event Structure",
    "disable": "Disable Structure",
    "in_place_element": "In Place Element Structure",
    "whileLoop": "While Loop",
    "forLoop": "For Loop",
    "flatSequence": "Flat Sequence",
    "seq": "Stacked Sequence",
    "eventStruct": "Event Structure",
    "propNode": "Property Node",
    "invokeNode": "Invoke Node",
    "mergeErrors": "Merge Errors",
    "concat": "Concatenate Strings",
    "gRef": "Control Reference",
    "nMux": "Bundle/Unbundle by index",
    "fBox": "Formula Node",
    "hiddenFBNode": "Feedback Node",
    "slaveFBInputNode": "Feedback Node (input)",
    "feedbackNode": "Feedback Node",
    "statVIRef": "Static VI Reference",
    "callByRef": "Call By Reference",
    "localVar": "Local Variable",
    "globalVar": "Global Variable",
}


# --------------------------------------------------------------------------- #
# types
# --------------------------------------------------------------------------- #
_ERROR_FIELDS = {"status", "code", "source"}


def type_str(t: Any) -> str | None:
    """Render a ParsedType/LVType as a compact human-readable type name."""
    if t is None:
        return None
    kind = getattr(t, "kind", None)
    kind = getattr(kind, "value", kind)

    typedef = getattr(t, "typedef_name", None) or getattr(t, "typedef_path", None)
    if typedef:
        return f"typedef:{typedef}"
    classname = getattr(t, "classname", None)
    if classname:
        return f"class:{classname}"
    ref_type = getattr(t, "ref_type", None)
    if ref_type:
        return f"refnum:{ref_type}"
    flavor = getattr(t, "measure_flavor", None)
    if flavor:
        return f"measure:{flavor}"

    elem = getattr(t, "element_type", None)
    if elem is not None:
        dims = getattr(t, "dimensions", None) or 1
        return f"{type_str(elem)}[{','.join(':' for _ in range(dims))}]"

    fields = getattr(t, "fields", None)
    if fields:
        names = [getattr(f, "name", "?") for f in fields]
        if _ERROR_FIELDS.issubset({n.lower() for n in names}):
            return "ErrorCluster"
        inner = ", ".join(
            f"{n}:{type_str(getattr(f, 'type', None)) or '?'}" for n, f in zip(names, fields)
        )
        return f"cluster{{{inner}}}"

    enum_values = getattr(t, "enum_values", None)
    if enum_values:
        return f"enum{{{', '.join(map(str, list(enum_values)[:12]))}}}"

    return getattr(t, "type_name", None) or (str(kind) if kind else None)


# --------------------------------------------------------------------------- #
# emitter
# --------------------------------------------------------------------------- #
class Dumper:
    def __init__(self, parsed: Any, path: Path, opts: argparse.Namespace) -> None:
        self.p = parsed
        self.path = path
        self.o = opts
        self.bd = parsed.block_diagram
        self.meta = parsed.metadata
        self.res = PrimitiveResolver()
        self.unresolved: dict[str, int] = {}
        self.omitted = {"void_terminals": 0, "inlined_constants": 0, "internal_objects": 0}

        self.nodes_by_uid = {n.uid: n for n in getattr(self.bd, "nodes", []) or []}
        self.consts_by_uid = {c.uid: c for c in getattr(self.bd, "constants", []) or []}
        self.labels_by_uid = {l.uid: l for l in getattr(self.bd, "labels", []) or []}
        self.srn = dict(getattr(self.bd, "srn_to_structure", {}) or {})

        self.term_info: dict[str, Any] = dict(getattr(self.bd, "terminal_info", {}) or {})
        self.terms_by_node: dict[str, list[Any]] = {}
        for uid, info in self.term_info.items():
            self.terms_by_node.setdefault(getattr(info, "parent_uid", ""), []).append(info)
        for lst in self.terms_by_node.values():
            lst.sort(key=lambda i: getattr(i, "index", 0))

        # wires + terminal->wire index
        self.wires: list[dict[str, Any]] = []
        self.wire_of_term: dict[str, str] = {}
        self.wire_ends: dict[str, dict[str, Any]] = {}
        for w in getattr(self.bd, "wires", []) or []:
            ft, tt = getattr(w, "from_term", None), getattr(w, "to_term", None)
            rec = {
                "id": w.uid,
                "from": {"node": self._owner(ft), "terminal": self._term_label(ft)},
                "to": {"node": self._owner(tt), "terminal": self._term_label(tt)},
            }
            self.wires.append(rec)
            self.wire_ends[w.uid] = rec
            for t in (ft, tt):
                if t:
                    self.wire_of_term[t] = w.uid

        # constants that feed exactly one terminal can be inlined there
        self.inline_const: dict[str, Any] = {}
        self.inlined_uids: set[str] = set()
        if not self.o.no_inline_consts:
            fan: dict[str, list[str]] = {}
            for w in getattr(self.bd, "wires", []) or []:
                src = getattr(w, "from_term", None)
                if src in self.consts_by_uid:
                    fan.setdefault(src, []).append(getattr(w, "to_term", ""))
            # Only single-sink constants inline; a fanned-out constant stays a
            # node so the sharing stays visible.
            self.inlined_uids: set[str] = set()
            for cuid, sinks in fan.items():
                if len(sinks) == 1 and sinks[0]:
                    self.inline_const[sinks[0]] = self.consts_by_uid[cuid]
                    self.inlined_uids.add(cuid)

        self.structures = self._index_structures()
        self.child_of: dict[str, str] = {}
        for suid, s in self.structures.items():
            for child in s["all_children"]:
                self.child_of[child] = suid

    # -- helpers ----------------------------------------------------------- #
    def _owner(self, term_uid: str | None) -> str | None:
        if not term_uid:
            return None
        if term_uid in self.consts_by_uid:
            return term_uid
        info = self.term_info.get(term_uid)
        if info is not None and getattr(info, "parent_uid", None):
            return info.parent_uid
        if term_uid in self.consts_by_uid or term_uid in self.nodes_by_uid:
            return term_uid
        return None

    def _term_label(self, term_uid: str | None) -> str | None:
        if not term_uid:
            return None
        info = self.term_info.get(term_uid)
        if info is None:
            return term_uid
        name = getattr(info, "name", None)
        if name:
            return name
        owner = getattr(info, "parent_uid", None)
        rp = self._resolved(owner) if owner else None
        if rp:
            idx = getattr(info, "index", None)
            for t in rp.get("terminals", []):
                if t.get("index") == idx:
                    return t.get("name") or term_uid
        return term_uid

    def _resolved(self, uid: str) -> dict[str, Any] | None:
        """Resolved primitive/node-type info: {'name':..., 'terminals':[...]}"""
        n = self.nodes_by_uid.get(uid)
        if n is None:
            return None
        cached = getattr(n, "_lv2j", None)
        if cached is not None:
            return cached or None

        out: dict[str, Any] | None = None
        nt = getattr(n, "node_type", "")
        if nt == "prim":
            rid = getattr(n, "prim_res_id", None) or getattr(n, "prim_index", None)
            r = self.res.resolve(prim_id=rid)
            if r is not None and r.confidence != "unknown":
                out = {"name": r.name,
                       "terminals": [{"index": t.index, "name": t.name,
                                      "direction": t.direction} for t in r.terminals],
                       "doc_url": getattr(r, "doc_url", None)}
            else:
                key = f"prim:{rid}"
                self.unresolved[key] = self.unresolved.get(key, 0) + 1
        else:
            r = self.res.resolve_by_node_type(nt)
            if r is not None and r.confidence != "unknown":
                out = {"name": r.name,
                       "terminals": [{"index": t.index, "name": t.name,
                                      "direction": t.direction} for t in r.terminals],
                       "doc_url": getattr(r, "doc_url", None)}
        try:
            n._lv2j = out or {}
        except Exception:
            pass
        return out

    def _index_structures(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}

        def frames_of(obj: Any, label_fn) -> list[dict[str, Any]]:
            return [{"frame": label_fn(f, i),
                     "children": list(getattr(f, "inner_node_uids", []) or [])}
                    for i, f in enumerate(getattr(obj, "frames", []) or [])]

        for lp in getattr(self.bd, "loops", []) or []:
            kids = list(getattr(lp, "inner_node_uids", []) or [])
            out[lp.uid] = {"kind": getattr(lp, "loop_type", "loop"),
                           "frames": [{"frame": "body", "children": kids}],
                           "all_children": kids, "obj": lp}
        for cs in getattr(self.bd, "case_structures", []) or []:
            fr = frames_of(cs, lambda f, i: ("default" if getattr(f, "is_default", False)
                                             else str(getattr(f, "selector_value", i))))
            out[cs.uid] = {"kind": "case", "frames": fr,
                           "all_children": [c for f in fr for c in f["children"]], "obj": cs}
        for es in getattr(self.bd, "event_structures", []) or []:
            fr = frames_of(es, lambda f, i: str(getattr(f, "event_label", None) or f"event[{i}]"))
            out[es.uid] = {"kind": "event", "frames": fr,
                           "all_children": [c for f in fr for c in f["children"]], "obj": es}
        for sq in getattr(self.bd, "flat_sequences", []) or []:
            fr = frames_of(sq, lambda f, i: f"step[{getattr(f, 'index', i)}]")
            kind = "flat_sequence" if getattr(sq, "is_flat", True) else "stacked_sequence"
            out[sq.uid] = {"kind": kind, "frames": fr,
                           "all_children": [c for f in fr for c in f["children"]], "obj": sq}
        for ds in getattr(self.bd, "disable_structures", []) or []:
            fr = frames_of(ds, lambda f, i: f"frame[{i}]")
            out[ds.uid] = {"kind": "disable", "frames": fr,
                           "all_children": [c for f in fr for c in f["children"]], "obj": ds}
        for ip in getattr(self.bd, "decompose_structures", []) or []:
            kids = list(getattr(ip, "inner_node_uids", []) or [])
            out[ip.uid] = {"kind": "in_place_element",
                           "frames": [{"frame": "body", "children": kids}],
                           "all_children": kids, "obj": ip}
        return out

    def _terminals(self, uid: str) -> list[dict[str, Any]]:
        rp = self._resolved(uid)
        by_index = {t["index"]: t for t in (rp or {}).get("terminals", [])}
        out = []
        for info in self.terms_by_node.get(uid, []):
            idx = getattr(info, "index", None)
            name = getattr(info, "name", None) or (by_index.get(idx) or {}).get("name")
            ts = type_str(getattr(info, "parsed_type", None))
            wire = self.wire_of_term.get(info.uid)
            const = self.inline_const.get(info.uid)

            if not self.o.keep_void and wire is None and const is None and ts == "Void":
                self.omitted["void_terminals"] += 1
                continue

            d: dict[str, Any] = {"name": name or info.uid,
                                 "dir": "out" if getattr(info, "is_output", False) else "in"}
            if idx is not None:
                d["i"] = idx
            if ts:
                d["type"] = ts
            if const is not None:
                d["const"] = getattr(const, "value", None)
                if getattr(const, "label", None):
                    d["const_label"] = const.label
                self.omitted["inlined_constants"] += 1
            elif wire:
                d["wire"] = wire
            if getattr(info, "inverted", False):
                d["inverted"] = True
            out.append(d)
        return out

    def _tunnels(self, obj: Any) -> list[dict[str, Any]]:
        out = []
        for t in getattr(obj, "tunnels", []) or []:
            mode = getattr(t, "mode", None)
            d: dict[str, Any] = {
                "type": getattr(t, "tunnel_type", None),
                "outer_wire": self.wire_of_term.get(getattr(t, "outer_terminal_uid", "")),
                "inner_wire": self.wire_of_term.get(getattr(t, "inner_terminal_uid", "")),
            }
            if mode is not None:
                d["mode"] = getattr(mode, "value", str(mode))
            if getattr(t, "conditional", False):
                d["conditional"] = True
            if getattr(t, "sr_initialized", None) is not None:
                d["shift_register_initialized"] = t.sr_initialized
            if getattr(t, "sr_stack_depth", None) is not None:
                d["shift_register_depth"] = t.sr_stack_depth
            out.append({k: v for k, v in d.items() if v is not None})
        return out

    # -- node emission ----------------------------------------------------- #
    def _emit_node(self, uid: str, seen: set[str]) -> dict[str, Any] | None:
        if uid in seen:
            return None
        seen.add(uid)

        if uid in self.structures:
            return self._emit_structure(uid, seen)

        # A free label nested in a frame: emit as an in-place comment.
        lbl = self.labels_by_uid.get(uid)
        if lbl is not None:
            return {"class": "comment", "text": lbl.text}

        # Structure inner-diagram bookkeeping objects: not real nodes.
        if uid in self.srn:
            self.omitted["internal_objects"] += 1
            return None

        if uid in self.consts_by_uid:
            c = self.consts_by_uid[uid]
            if uid in self.inlined_uids:
                return None  # already inlined at its consumer
            d: dict[str, Any] = {"id": uid, "class": "Constant",
                                 "type": getattr(c, "type_desc", None),
                                 "value": getattr(c, "value", None)}
            if getattr(c, "label", None):
                d["label"] = c.label
            terms = self._terminals(uid)
            if terms:
                d["terminals"] = terms
            elif self.wire_of_term.get(uid):
                d["wire"] = self.wire_of_term[uid]
            return {k: v for k, v in d.items() if v is not None}

        n = self.nodes_by_uid.get(uid)
        if n is None:
            self.omitted["internal_objects"] += 1
            return {"id": uid, "class": "unknown"}

        nt = getattr(n, "node_type", None)
        rp = self._resolved(uid)
        raw_name = getattr(n, "name", None)
        if raw_name in (None, "", "Primitive"):
            raw_name = None
        target = None
        if nt in ("gRef", "statVIRef", "localVar", "globalVar"):
            target, raw_name = raw_name, None   # the name is what it points AT
        name = raw_name or (rp or {}).get("name") or CLASS_NAMES.get(nt) or nt

        d = {"id": uid, "op": name, "class": nt}
        if target:
            d["target"] = target
        for f in EXTRA_NODE_FIELDS:
            v = getattr(n, f, None)
            if v not in (None, "", [], {}):
                d[f] = v
        props = getattr(n, "properties", None)
        if props:
            d["properties"] = [p.get("name", p) if isinstance(p, dict) else p for p in props]
        if getattr(n, "label", None) and n.label != name:
            d["label"] = n.label
        if getattr(n, "caption", None):
            d["caption"] = n.caption
        qn = (getattr(self.meta, "iuse_to_qualified_name", {}) or {}).get(uid)
        if qn:
            d["calls"] = qn
        terms = self._terminals(uid)
        if terms:
            d["terminals"] = terms
        return {k: v for k, v in d.items() if v is not None}

    def _emit_structure(self, uid: str, seen: set[str]) -> dict[str, Any]:
        s = self.structures[uid]
        obj = s["obj"]
        node = self.nodes_by_uid.get(uid)
        d: dict[str, Any] = {"id": uid, "op": CLASS_NAMES.get(s["kind"], s["kind"]),
                             "class": s["kind"]}
        if node is not None and getattr(node, "label", None):
            d["label"] = node.label

        if s["kind"] in ("whileLoop", "forLoop", "loop"):
            stop = getattr(obj, "stop_condition_terminal_uid", None)
            if stop:
                d["stop_condition"] = {
                    "wire": self.wire_of_term.get(stop),
                    "polarity": "continue_if_true" if getattr(obj, "stop_condition_inverted", False)
                                else "stop_if_true",
                }
            if getattr(obj, "parallel", False):
                d["parallel"] = True
                if getattr(obj, "parallel_static_workers", None):
                    d["parallel_workers"] = obj.parallel_static_workers
        if s["kind"] == "case":
            sel = getattr(obj, "selector_terminal_uid", None)
            if sel:
                d["selector"] = {"wire": self.wire_of_term.get(sel),
                                 "type": getattr(obj, "selector_type", None)}
        if s["kind"] == "disable":
            d["active_frame"] = getattr(obj, "active_frame", None)

        tunnels = self._tunnels(obj)
        if tunnels:
            d["tunnels"] = tunnels

        frames = []
        for fr in s["frames"]:
            body = [b for b in (self._emit_node(c, seen) for c in fr["children"]) if b]
            frames.append({"frame": fr["frame"], "body": body})
        if len(frames) == 1 and frames[0]["frame"] == "body":
            d["body"] = frames[0]["body"]
        else:
            d["frames"] = frames
        return {k: v for k, v in d.items() if v is not None}

    # -- signature / top level --------------------------------------------- #
    def _signature(self) -> dict[str, Any]:
        fp = getattr(self.p, "front_panel", None)
        controls = {c.uid: c for c in (getattr(fp, "controls", []) or [])}
        fp_types = {getattr(t, "fp_dco_uid", None): t
                    for t in (getattr(self.bd, "fp_terminals", []) or [])}

        def entry(ctl: Any, slot: int | None) -> dict[str, Any]:
            term = fp_types.get(ctl.uid)
            d: dict[str, Any] = {
                "name": getattr(ctl, "name", None),
                "type": type_str(getattr(term, "parsed_type", None)) or getattr(ctl, "type_desc", None),
                "control_type": getattr(ctl, "control_type", None),
            }
            if slot is not None:
                d["pane_slot"] = slot
            if getattr(ctl, "default_value", None) is not None:
                d["default"] = ctl.default_value
            if getattr(ctl, "enum_values", None):
                d["enum_values"] = ctl.enum_values
            kids = getattr(ctl, "children", None)
            if kids:
                d["fields"] = [entry(k, None) for k in kids]
            return {k: v for k, v in d.items() if v is not None}

        pane_slot = {}
        cp = getattr(self.p, "connector_pane", None)
        for s in getattr(cp, "slots", []) or []:
            if getattr(s, "fp_dco_uid", None):
                pane_slot[s.fp_dco_uid] = s.index

        ins, outs = [], []
        for uid, ctl in controls.items():
            (outs if getattr(ctl, "is_indicator", False) else ins).append(entry(ctl, pane_slot.get(uid)))
        ins.sort(key=lambda e: e.get("pane_slot", 999))
        outs.sort(key=lambda e: e.get("pane_slot", 999))
        sig: dict[str, Any] = {"inputs": ins, "outputs": outs}
        if cp is not None:
            sig["connector_pane_pattern"] = getattr(cp, "pattern_id", None)
        return sig

    def dump(self) -> dict[str, Any]:
        seen: set[str] = set()
        top_uids = [u for u in list(self.nodes_by_uid) + list(self.consts_by_uid)
                    if u not in self.child_of]
        diagram = [d for d in (self._emit_node(u, seen) for u in top_uids) if d]

        # free labels that were not nested inside a structure frame
        loose = [{"text": l.text} for u, l in self.labels_by_uid.items() if u not in seen]

        deps = [{"name": getattr(r, "name", None),
                 "qualified_name": getattr(r, "qualified_name", None),
                 "path": r.get_relative_path() if hasattr(r, "get_relative_path") else None}
                for r in (getattr(self.meta, "dependency_refs", []) or [])]

        out: dict[str, Any] = {
            "schema": SCHEMA,
            "vi": self.path.name,
            "source_path": str(self.path),
            "qualified_name": getattr(self.meta, "qualified_name", None),
            "owning_libraries": list(getattr(self.meta, "owning_libraries", []) or []),
            "signature": self._signature(),
            "diagram": diagram,
            "comments": loose,
            "subvi_calls": sorted(set(
                list(getattr(self.meta, "subvi_qualified_names", []) or [])
                + list(getattr(self.meta, "subvi_method_names", []) or [])
            )),
            "dependencies": deps,
            "stats": {
                "nodes": len(self.nodes_by_uid),
                "constants": len(self.consts_by_uid),
                "wires": len(self.wires),
                "structures": len(self.structures),
                "omitted": self.omitted,
                "unresolved_ops": self.unresolved,
            },
        }
        if not self.o.no_wire_list:
            out["wires"] = self.wires
        return out


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def dump_vi(path: Path, opts: argparse.Namespace) -> dict[str, Any]:
    return Dumper(parse_vi(path), path, opts).dump()


def resolve_deps(path: Path, dump: dict[str, Any]) -> list[Path]:
    """Local (non-vi.lib) subVI files referenced by this VI, if present on disk."""
    out = []
    for d in dump.get("dependencies", []):
        rel, name = d.get("path") or "", d.get("name") or ""
        if not name.lower().endswith((".vi", ".ctl")) or rel.startswith("<"):
            continue
        for cand in (path.parent / name, path.parent / rel):
            if cand.is_file():
                out.append(cand.resolve())
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path, help=".vi files and/or directories")
    ap.add_argument("-o", "--out", type=Path, help="output directory (default: beside each VI)")
    ap.add_argument("--recurse", action="store_true", help="follow local subVI dependencies")
    ap.add_argument("--hierarchy", action="store_true", help="also write _hierarchy.json")
    ap.add_argument("--indent", type=int, default=2, help="JSON indent, 0 for compact")
    ap.add_argument("--stdout", action="store_true", help="print JSON instead of writing files")
    ap.add_argument("--keep-void", action="store_true", help="keep unwired Void terminals")
    ap.add_argument("--no-inline-consts", action="store_true", help="emit constants as nodes")
    ap.add_argument("--no-wire-list", action="store_true", help="omit the flat wire list")
    ap.add_argument("--quiet", action="store_true", help="suppress lvkit parser warnings")
    args = ap.parse_args()

    if args.quiet:
        import contextlib, io, warnings
        warnings.filterwarnings("ignore")

    queue: list[Path] = []
    for p in args.paths:
        if p.is_dir():
            queue += sorted(q.resolve() for q in p.rglob("*.vi"))
        elif p.is_file():
            queue.append(p.resolve())
        else:
            print(f"! not found: {p}", file=sys.stderr)

    done: set[Path] = set()
    hierarchy: dict[str, list[str]] = {}
    failures = 0

    while queue:
        vi = queue.pop(0)
        if vi in done:
            continue
        done.add(vi)
        try:
            data = dump_vi(vi, args)
        except Exception as exc:  # one unreadable VI must not stop a batch
            failures += 1
            print(f"! {vi.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        hierarchy[vi.name] = data.get("subvi_calls", [])
        text = json.dumps(data, indent=args.indent or None, default=str)

        if args.stdout:
            print(text)
        else:
            out_dir = args.out or vi.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{vi.name}.json"
            dest.write_text(text, encoding="utf-8")
            s = data["stats"]
            unres = sum(s["unresolved_ops"].values())
            print(f"{dest}  ({s['nodes']} nodes, {s['wires']} wires, "
                  f"{s['structures']} structures, {unres} unresolved ops, "
                  f"{len(text) / 1e6:.2f} MB)")

        if args.recurse:
            queue += [d for d in resolve_deps(vi, data) if d not in done]

    if args.hierarchy and not args.stdout:
        out_dir = args.out or Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / "_hierarchy.json"
        dest.write_text(json.dumps(hierarchy, indent=2), encoding="utf-8")
        print(f"{dest}  ({len(hierarchy)} VIs)")

    if failures:
        print(f"\n{failures} VI(s) failed to parse.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
