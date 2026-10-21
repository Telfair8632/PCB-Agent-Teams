#!/usr/bin/env python3
"""draw-pcb toolbox tool: net_metrics

Per-net routed length, via count and track widths — the numbers a routing
constraint is actually checked against. Without a measurement you cannot tell a
via budget or a length limit was met; "the router was told to" is not evidence.

Read-only. Thin wrapper over check-pcb's analyze_net_lengths so this tool and
the PCB analyzer never disagree about what a net measures.

Usage:
  net_metrics.py <board.kicad_pcb> [--nets PAT ...] [--max-vias N]
                 [--max-length MM] [--min-width MM] [--pairs A,B ...]

  --max-vias / --max-length / --min-width  optional budgets. Any net that
      breaks one lands in `violations[]` and the exit code becomes 2, so this
      can stand in a gate chain.
  --pairs  comma-joined net names to compare as a matched pair (differential
      pairs, matched buses). Reports the length delta per pair.

Output JSON: {ok, net_count, nets[], violations[], pairs[]}
"""
import argparse
import fnmatch
import json
import sys
from pathlib import Path

_CHECK_PCB = Path(__file__).resolve().parents[3] / "check-pcb" / "scripts"
sys.path.insert(0, str(_CHECK_PCB))

try:
    from analyze_pcb import analyze_pcb
except ImportError as e:  # pragma: no cover - environment guard
    print(json.dumps({"ok": False,
                      "error": f"cannot import check-pcb scripts: {e}"}))
    sys.exit(1)


def net_metrics(pcb_path: str, patterns: list[str] | None = None,
                widths: bool = False) -> list[dict]:
    """Per-net rows straight out of the PCB analyzer, optionally width-aware."""
    data = analyze_pcb(pcb_path, include_trace_segments=widths)
    rows = data.get("net_lengths", []) or []
    out = []
    for r in rows:
        if patterns and not any(fnmatch.fnmatch(r.get("net", ""), p)
                                for p in patterns):
            continue
        row = {k: v for k, v in r.items() if k != "trace_segments"}
        segs = r.get("trace_segments") or []
        if segs:
            ws = [s.get("width_mm") for s in segs if s.get("width_mm")]
            if ws:
                row["min_width_mm"] = round(min(ws), 4)
                row["max_width_mm"] = round(max(ws), 4)
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-net length / via / width")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--nets", nargs="+", metavar="PAT",
                    help="net name patterns to report (default: all routed)")
    ap.add_argument("--max-vias", type=int, metavar="N",
                    help="flag nets with more vias than this")
    ap.add_argument("--max-length", type=float, metavar="MM",
                    help="flag nets longer than this")
    ap.add_argument("--min-width", type=float, metavar="MM",
                    help="flag nets carrying any track thinner than this")
    ap.add_argument("--pairs", nargs="+", metavar="A,B",
                    help="net pairs to compare, e.g. 'D+,D-' 'RS485+,RS485-'")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    try:
        rows = net_metrics(args.pcb, args.nets,
                           widths=args.min_width is not None)
    except Exception as e:  # noqa: BLE001 - tool boundary, report cleanly
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1

    by_net = {r.get("net"): r for r in rows}
    violations = []
    for r in rows:
        net = r.get("net", "?")
        if args.max_vias is not None and r.get("via_count", 0) > args.max_vias:
            violations.append({"net": net, "rule": "max_vias",
                               "measured": r.get("via_count"),
                               "limit": args.max_vias})
        if args.max_length is not None \
                and (r.get("total_length_mm") or 0) > args.max_length:
            violations.append({"net": net, "rule": "max_length",
                               "measured": r.get("total_length_mm"),
                               "limit": args.max_length})
        if args.min_width is not None:
            w = r.get("min_width_mm")
            if w is not None and w < args.min_width:
                violations.append({"net": net, "rule": "min_width",
                                   "measured": w, "limit": args.min_width})

    pairs = []
    for spec in (args.pairs or []):
        names = [n.strip() for n in spec.split(",") if n.strip()]
        if len(names) != 2:
            pairs.append({"pair": spec, "error": "expected exactly two names"})
            continue
        a, b = (by_net.get(n) for n in names)
        if not a or not b:
            missing = [n for n in names if n not in by_net]
            pairs.append({"pair": names, "error":
                          f"not routed / not found: {missing}"})
            continue
        la = a.get("total_length_mm") or 0.0
        lb = b.get("total_length_mm") or 0.0
        pairs.append({"pair": names,
                      "length_mm": [la, lb],
                      "delta_mm": round(abs(la - lb), 4),
                      "via_count": [a.get("via_count"), b.get("via_count")]})

    print(json.dumps({
        "ok": not violations,
        "net_count": len(rows),
        "nets": rows,
        "pairs": pairs,
        "violations": violations,
    }, ensure_ascii=False, indent=2))
    return 2 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
