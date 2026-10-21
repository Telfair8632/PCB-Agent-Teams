#!/usr/bin/env python3
"""
Anti-aliasing / RC low-pass filter sweep — additive companion to simulate_subcircuits.py.

Why this exists:
  detect_rc_filters in signal_detectors.py excludes voltage-divider resistors
  (KH-121 + voltage_divider exclusion). On any board where the divider's *own*
  shunt cap forms the antialias filter (e.g. R_top + R_bot + C_in to GND),
  the filter is silently skipped.

  Hierarchical schematics fragment a single logical net across UUID-prefixed
  copies (`/<uuid>/AMC_IN` and `AMC_IN`). Detectors that key on net name see
  two separate nets and miss R-C pairs that share one logical net.

  This script bypasses both issues:
    1. Coalesces analyzer nets by basename (strips leading `/<uuid>/` paths).
    2. For every cap whose one end is on a clean ground name, treats the
       other (signal) end as a low-pass filter node.
    3. Computes R_eq = parallel of all R's connected to that hot node from
       any source side, evaluates fc analytically, then runs ngspice .ac
       to confirm the actual −3 dB cutoff.
    4. Emits findings indexed by (R, C) so downstream tooling can compare
       against project spec (CLAUDE.md fc target).

Usage:
    python3 aa_filter_sim.py analysis/<run>/schematic.json \\
            --output analysis/<run>/aa_filter_sim.json

Exit 0 always when the sweep completes (per finding has its own pass/warn);
exit 1 if ngspice not found or analyzer JSON unreadable.

This is intentionally project-agnostic — no MPN strings, no domain-specific
heuristics.  Drop in any project's analyzer JSON.
"""

# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

_UUID_PATH_RE = re.compile(
    r'^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/',
    re.IGNORECASE,
)

_GROUND_NAME_RE = re.compile(
    r'^([AD]?GND|GND[A-Z0-9_]*|[A-Z0-9_]+_GND|VSS|0V)$',
    re.IGNORECASE,
)

# Power rail names — treat caps on these as decoupling, not antialias.
# Match V3V3, V5V_EXT, +5V, VCC, VDD, VBUS, etc.
_RAIL_NAME_RE = re.compile(
    r'^(\+?[\d._]+V[A-Z0-9_]*|V[\d_]+V[A-Z0-9_]*|VCC[A-Z0-9_]*|VDD[A-Z0-9_]*|'
    r'VBAT[A-Z0-9_]*|VBUS[A-Z0-9_]*|VIN[A-Z0-9_]*|VOUT[A-Z0-9_]*|'
    r'V[A-Z0-9]*_(EXT|IN|ISO|SW|FUSED))$',
    re.IGNORECASE,
)


def _is_rail(name: str) -> bool:
    return bool(_RAIL_NAME_RE.match(name.strip()))


def _clean_net_name(name: str) -> str:
    """Strip leading UUID path prefix; keep last segment otherwise."""
    if '/' in name and _UUID_PATH_RE.match(name):
        return name.rsplit('/', 1)[-1]
    if name.startswith('/'):
        return name.rsplit('/', 1)[-1]
    return name


def _is_ground(name: str) -> bool:
    return bool(_GROUND_NAME_RE.match(name.strip()))


_VAL_RE = re.compile(r'^\s*([\d.]+)\s*([fpnumkMGTRr]?)([FHfhΩΩOoRr]?)\s*$')
_SUFFIX = {
    'f': 1e-15, 'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'm': 1e-3,
    '': 1.0, 'k': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12,
    'R': 1.0, 'r': 1.0,
}


def _parse_value(text: str):
    """Parse '1M', '20k', '390pF', '6.8nF X7R' → float in base units. Returns None on failure."""
    if not text:
        return None
    # Take first token only (drop dielectric / voltage suffixes)
    token = text.split()[0]
    m = _VAL_RE.match(token)
    if not m:
        return None
    num, suffix, _ = m.groups()
    try:
        base = float(num)
    except ValueError:
        return None
    return base * _SUFFIX.get(suffix, 1.0)


def coalesce_nets(nets_dict):
    """Merge UUID-prefixed nets with their bare counterparts. Returns dict[clean_name -> set of (ref, pin_number)]."""
    coalesced = {}
    for raw_name, net_data in nets_dict.items():
        clean = _clean_net_name(raw_name)
        for pin in net_data.get('pins', []):
            ref = pin.get('component')
            num = str(pin.get('pin_number', ''))
            if ref:
                coalesced.setdefault(clean, set()).add((ref, num))
    return coalesced


def build_component_index(components):
    """Build {ref: component_dict} index."""
    return {c.get('reference'): c for c in components if c.get('reference')}


def build_ref_to_nets(coalesced_nets):
    """Build {ref: {pin_num: clean_net_name}}."""
    out = {}
    for net, pins in coalesced_nets.items():
        for ref, pin_num in pins:
            out.setdefault(ref, {})[pin_num] = net
    return out


def _walk_r_chain(start_net, ref_to_nets, comp_idx, excluded_refs=None,
                  max_depth=10):
    """
    From `start_net`, walk through any series R's until we hit a ground, a power
    rail, or an IC pin (= signal source termination). Returns total series
    resistance from start_net to that termination.

    `excluded_refs` lets the caller exclude the originating R (the one whose
    chain we are walking from) so it isn't treated as a branch on `start_net`.

    If the chain branches (start_net has 2+ usable R's) returns None — caller
    falls back to "stop at start_net".
    """
    excluded = set(excluded_refs or ())
    visited = {start_net}
    total_r = 0.0
    current_net = start_net
    for _ in range(max_depth):
        rs_here = []
        for ref, pin_to_net in ref_to_nets.items():
            if ref in excluded:
                continue
            comp = comp_idx.get(ref, {})
            if comp.get('type') != 'resistor':
                continue
            nets = list(pin_to_net.values())
            if len(nets) != 2 or current_net not in nets:
                continue
            far = next(n for n in nets if n != current_net)
            if far in visited:
                continue
            r_val = _parse_value(comp.get('value', ''))
            if not r_val or r_val < 1:
                continue
            rs_here.append((ref, r_val, far))

        if len(rs_here) != 1:
            return None if total_r == 0 else (total_r, current_net)
        ref, r_val, far = rs_here[0]
        excluded.add(ref)
        total_r += r_val
        visited.add(far)
        current_net = far
        if _is_ground(far) or _is_rail(far):
            return (total_r, far)
        far_pins = [(r, p) for r, pinmap in ref_to_nets.items()
                    for pn, n in pinmap.items() if n == far for p in [pn]]
        non_passive = any(comp_idx.get(r, {}).get('type') not in
                          ('resistor', 'capacitor', 'inductor')
                          for r, _ in far_pins)
        if non_passive:
            return (total_r, far)
    return (total_r, current_net) if total_r else None


def find_aa_candidates(analysis):
    """
    Walk every capacitor; if one end is on a ground net, treat the hot end as
    a low-pass filter node. Return list of dicts.

    Skip rules:
      - Both ends ground (impossible)
      - Hot end is a power rail (V3V3 / V5V / VCC / VDD ...) — that's
        decoupling, not antialias. simulate_subcircuits.py handles those.
      - No source R's at hot net → not a filter, just a stray cap.
    """
    components = analysis.get('components', [])
    nets = analysis.get('nets', {})
    coalesced = coalesce_nets(nets)
    comp_idx = build_component_index(components)
    ref_to_nets = build_ref_to_nets(coalesced)

    candidates = []

    for ref, comp in comp_idx.items():
        if comp.get('type') != 'capacitor':
            continue
        pin_to_net = ref_to_nets.get(ref, {})
        if len(pin_to_net) != 2:
            continue
        net_a, net_b = list(pin_to_net.values())
        if net_a == net_b:
            continue
        if _is_ground(net_a) and _is_ground(net_b):
            continue
        if _is_ground(net_a):
            hot, gnd = net_b, net_a
        elif _is_ground(net_b):
            hot, gnd = net_a, net_b
        else:
            continue  # differential / cross-rail cap — not a single-ended LP filter

        # Skip caps on power rails — those are bypass/decoupling caps, not AA filters
        if _is_rail(hot):
            continue

        c_val = _parse_value(comp.get('value', ''))
        if not c_val or c_val <= 0:
            continue

        # Find R's with one terminal on `hot`. For each branch, walk the R chain
        # until we hit a ground/rail/IC pin to get the true source impedance.
        # The filter sees R_eq = parallel of all branches' total series R.
        branches = []
        seen_branches = set()
        for other_ref, other_pin_to_net in ref_to_nets.items():
            other_comp = comp_idx.get(other_ref, {})
            if other_comp.get('type') != 'resistor':
                continue
            other_nets = list(other_pin_to_net.values())
            if len(other_nets) != 2 or hot not in other_nets:
                continue
            far = next(n for n in other_nets if n != hot)
            r_val = _parse_value(other_comp.get('value', ''))
            if not r_val or r_val < 1:
                continue
            # Walk the chain starting from `far` to find its termination,
            # excluding the originating R so it isn't counted as a branch.
            chain_result = _walk_r_chain(far, ref_to_nets, comp_idx,
                                          excluded_refs={other_ref})
            if chain_result:
                chain_r, chain_term = chain_result
                total_r = r_val + chain_r
            else:
                total_r, chain_term = r_val, far
            key = (other_ref, round(total_r))
            if key in seen_branches:
                continue
            seen_branches.add(key)
            branches.append({
                'first_r': other_ref,
                'first_r_value': other_comp.get('value', ''),
                'first_r_ohms': r_val,
                'series_total_ohms': total_r,
                'termination_net': chain_term,
                'termination_is_ground': _is_ground(chain_term),
                'termination_is_rail': _is_rail(chain_term),
            })

        if not branches:
            continue

        r_eq = 1.0 / sum(1.0 / b['series_total_ohms'] for b in branches)
        fc_analytic = 1.0 / (2.0 * math.pi * r_eq * c_val)

        candidates.append({
            'hot_net': hot,
            'gnd_net': gnd,
            'cap': {'ref': ref, 'value': comp.get('value', ''), 'farads': c_val},
            'branches': branches,
            'r_eq_ohms': r_eq,
            'fc_analytic_hz': fc_analytic,
        })

    return candidates


def find_diff_aa_candidates(analysis):
    """
    Differential antialias: two series R's drive a differential cap C_diff
    between two output nets, with optional CM caps to ground.
      OUTP --R_a--+-- ADC_P
                  |
                C_diff
                  |
      OUTN --R_b--+-- ADC_N

    Returns list of dicts:
      { p_net, n_net, c_diff: {...}, r_p: {...}, r_n: {...},
        r_total_ohms, fc_diff_analytic_hz }
    """
    components = analysis.get('components', [])
    nets = analysis.get('nets', {})
    coalesced = coalesce_nets(nets)
    comp_idx = build_component_index(components)
    ref_to_nets = build_ref_to_nets(coalesced)

    out = []
    seen_pairs = set()

    for ref, comp in comp_idx.items():
        if comp.get('type') != 'capacitor':
            continue
        pin_to_net = ref_to_nets.get(ref, {})
        if len(pin_to_net) != 2:
            continue
        nets_pair = list(pin_to_net.values())
        if any(_is_ground(n) for n in nets_pair):
            continue  # not a differential cap
        if nets_pair[0] == nets_pair[1]:
            continue
        c_val = _parse_value(comp.get('value', ''))
        if not c_val or c_val <= 0:
            continue

        p_net, n_net = nets_pair

        # Find R driving each side
        def _find_r_into(net):
            """Return (r_ref, r_ohms, far_net) for an R with one terminal on `net`."""
            for other_ref, other_pins in ref_to_nets.items():
                other_comp = comp_idx.get(other_ref, {})
                if other_comp.get('type') != 'resistor':
                    continue
                other_nets = list(other_pins.values())
                if len(other_nets) != 2 or net not in other_nets:
                    continue
                far = next(n for n in other_nets if n != net)
                # Reject if far is the OTHER differential leg
                # (that would be the cap itself reflected, not an R driver)
                r_val = _parse_value(other_comp.get('value', ''))
                if not r_val or r_val < 1:
                    continue
                yield (other_ref, r_val, far)

        rs_p = list(_find_r_into(p_net))
        rs_n = list(_find_r_into(n_net))
        if not rs_p or not rs_n:
            continue

        # Pick lowest-value R on each side (typical series filter R is small;
        # divider R is large → divider R doesn't pair with diff-mode cap)
        rs_p.sort(key=lambda x: x[1])
        rs_n.sort(key=lambda x: x[1])
        r_p_ref, r_p_val, r_p_far = rs_p[0]
        r_n_ref, r_n_val, r_n_far = rs_n[0]

        # The two driving R's must NOT share a net (otherwise it's a single R chain)
        if r_p_far == n_net or r_n_far == p_net:
            continue

        # fc_diff = 1 / (2π × (R_p + R_n) × C_diff)
        r_total = r_p_val + r_n_val
        fc_diff = 1.0 / (2.0 * math.pi * r_total * c_val)

        key = frozenset([ref, r_p_ref, r_n_ref])
        if key in seen_pairs:
            continue
        seen_pairs.add(key)

        out.append({
            'p_net': p_net,
            'n_net': n_net,
            'c_diff': {'ref': ref, 'value': comp.get('value', ''), 'farads': c_val},
            'r_p': {'ref': r_p_ref, 'ohms': r_p_val, 'far_net': r_p_far},
            'r_n': {'ref': r_n_ref, 'ohms': r_n_val, 'far_net': r_n_far},
            'r_total_ohms': r_total,
            'fc_diff_analytic_hz': fc_diff,
        })

    return out


def write_se_aa_cir(cand, cir_path):
    """Write ngspice .cir for single-ended low-pass. Returns f_max for sweep.

    Each branch becomes a single equivalent series R from `in` to `hot`. Branches
    that terminate on ground are connected `0 -> hot` (shunt). Branches that
    terminate on a rail (or IC pin) are connected `in -> hot`. AC source on `in`
    swept to find fc_3dB at `hot`.
    """
    fc = cand['fc_analytic_hz']
    # Sweep from 1 Hz (always far below fc for any practical filter) to 100×fc.
    # Measuring at the second decade above fmin avoids "out of interval" errors
    # when meas tries to interpolate the boundary frequency.
    fmin = 1.0
    fmax = fc * 100
    branches = cand['branches']
    cap = cand['cap']

    lines = ['* Auto-generated AA filter AC sweep (single-ended low-pass)']
    lines.append(f"* hot={cand['hot_net']} gnd={cand['gnd_net']}")
    lines.append("Vin in 0 DC 0 AC 1")
    for i, b in enumerate(branches):
        # If branch terminates on ground, that's a shunt to GND from hot:
        # behaves as series R between hot and 0 (no contribution to AC drive).
        # If branch terminates on rail/IC, treat as AC source via `in`.
        far = '0' if b['termination_is_ground'] else 'in'
        lines.append(f"R{i} {far} hot {b['series_total_ohms']:.6g}")
    lines.append(f"C0 hot 0 {cap['farads']:.6g}")
    lines.append(f".ac dec 50 {fmin:.6g} {fmax:.6g}")
    lines.append(".control")
    lines.append("run")
    lines.append("meas ac g_dc find vdb(hot) at=10")  # 10 Hz, well below any real filter fc
    lines.append("let tgt = g_dc - 3")
    lines.append("meas ac fc_3db when vdb(hot)=tgt")
    lines.append("meas ac vlin_dc find vm(hot) at=10")
    lines.append("print g_dc fc_3db vlin_dc")
    lines.append(".endc")
    lines.append(".end")
    cir_path.write_text('\n'.join(lines), encoding="utf-8")


def write_diff_aa_cir(cand, cir_path):
    """Write ngspice .cir for differential filter.

    Drive +0.5V on outp / -0.5V on outn (1V differential). Series R's in series
    with the differential cap. Use a 1:1 dependent source `Eout diff 0 p n 1`
    so vdb(diff) reads the differential transfer.
    """
    fc = cand['fc_diff_analytic_hz']
    # Sweep from 1 Hz (always far below fc for any practical filter) to 100×fc.
    # Measuring at the second decade above fmin avoids "out of interval" errors
    # when meas tries to interpolate the boundary frequency.
    fmin = 1.0
    fmax = fc * 100
    rp = cand['r_p']; rn = cand['r_n']; cd = cand['c_diff']

    lines = ['* Auto-generated AA filter AC sweep (differential)']
    lines.append(f"* p={cand['p_net']} n={cand['n_net']} cap={cd['ref']}")
    lines.append("Vp outp 0 DC 0 AC 0.5")
    lines.append("Vn outn 0 DC 0 AC -0.5")
    lines.append(f"Rp outp np {rp['ohms']:.6g}")
    lines.append(f"Rn outn nn {rn['ohms']:.6g}")
    lines.append(f"Cd np nn {cd['farads']:.6g}")
    # Diff output buffered to a node so vdb() works in measure
    lines.append("Eout diff 0 np nn 1")
    lines.append(f".ac dec 200 {fmin:.6g} {fmax:.6g}")
    lines.append(".control")
    lines.append("run")
    lines.append("meas ac g_dc find vdb(diff) at=10")
    lines.append("let tgt = g_dc - 3")
    lines.append("meas ac fc_3db when vdb(diff)=tgt")
    lines.append("print g_dc fc_3db")
    lines.append(".endc")
    lines.append(".end")
    cir_path.write_text('\n'.join(lines), encoding="utf-8")


_MEAS_RE = re.compile(r'^([a-zA-Z_][\w]*)\s*=\s*([-\d.eE+]+)', re.M)


def run_ngspice(cir_path, ngspice_bin, timeout=10):
    try:
        result = subprocess.run(
            [ngspice_bin, '-b', str(cir_path)],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
    except subprocess.TimeoutExpired:
        return {'error': 'timeout', 'stdout': '', 'stderr': ''}
    out = result.stdout + '\n' + result.stderr
    measures = {}
    for m in _MEAS_RE.finditer(out):
        try:
            measures[m.group(1)] = float(m.group(2))
        except ValueError:
            pass
    return {
        'returncode': result.returncode,
        'measures': measures,
        'stdout_tail': '\n'.join(result.stdout.splitlines()[-20:]),
    }


def evaluate(cand_kind, cand, sim, spec_fc_hz_list=None, spec_tol_pct=20.0):
    """
    Build a finding dict from analytic + sim data.

    Verdict semantics:
      - `pass`            : sim matches analytic ±5% AND (no spec OR within ±tol of ANY listed spec)
      - `numeric_only`    : sim matches analytic, but no project spec to compare
      - `spec_mismatch`   : sim matches analytic but is outside ±tol of EVERY listed spec
      - `sim_warn`        : sim disagrees with analytic by >5% (.cir / numerical issue)
      - `sim_failed`      : ngspice errored
      - `sim_ran_no_fc`   : ngspice ran but no fc_3dB found

    Multi-spec support: a typical isolation-amp board has differential filter at
    ~Nyquist (e.g. 20kHz) AND common-mode caps targeting much higher (e.g. 3.4MHz).
    spec_fc_hz_list collects all fc values listed in CLAUDE.md so each cap is
    judged against the closest design intent rather than just the first match.
    """
    fc_a = cand['fc_analytic_hz'] if cand_kind == 'single_ended' else cand['fc_diff_analytic_hz']
    fc_sim = sim.get('measures', {}).get('fc_3db')
    g_dc = sim.get('measures', {}).get('g_dc')
    rel_err_num = None
    matched_spec_hz = None
    rel_err_spec = None
    verdict = 'unknown'
    spec_list = [s for s in (spec_fc_hz_list or []) if s and s > 0]
    if fc_sim and fc_sim > 0:
        rel_err_num = (fc_sim - fc_a) / fc_a * 100
        if abs(rel_err_num) >= 5:
            verdict = 'sim_warn'
        elif spec_list:
            # Find closest spec; pass if within tolerance of ANY listed fc
            best_spec, best_err = min(
                ((s, (fc_sim - s) / s * 100) for s in spec_list),
                key=lambda x: abs(x[1]),
            )
            matched_spec_hz = best_spec
            rel_err_spec = best_err
            verdict = 'pass' if abs(best_err) <= spec_tol_pct else 'spec_mismatch'
        else:
            verdict = 'numeric_only'
    elif sim.get('returncode') == 0:
        verdict = 'sim_ran_no_fc'
    else:
        verdict = 'sim_failed'
    return {
        'kind': cand_kind,
        'fc_analytic_hz': fc_a,
        'fc_simulated_hz': fc_sim,
        'fc_spec_hz_list': spec_list,
        'fc_spec_hz_matched': matched_spec_hz,
        'rel_err_pct': rel_err_num,
        'rel_err_spec_pct': rel_err_spec,
        'g_dc_db': g_dc,
        'verdict': verdict,
    }


_FC_LINE_RE = re.compile(r'\b(?:fc|f_c|cutoff|抗混叠|filter|信号\s*BW)\b', re.IGNORECASE)
_FC_VALUE_RE = re.compile(r'([\d]+\.?[\d]*)\s*([kKmMgG]?)\s*[Hh][Zz]')


def _parse_spec_fc_from_claude_md(claude_md_path: Path) -> list:
    """
    Best-effort extract ALL design fc targets from a project CLAUDE.md.

    Two-stage parse:
      1. Find lines mentioning "fc / cutoff / filter / 抗混叠 / 信号 BW"
      2. On each such line, extract every `<num><unit>Hz` literal

    This catches both direct forms ("fc = 20 kHz") and computed-result forms
    ("LV cm fc: 1/(2π × 100Ω × 470pF) = 3.4 MHz"). Returns deduplicated list
    of fc values in Hz.

    Multi-spec is normal for isolation-amp boards (differential at Nyquist +
    CM at MHz range). Caller compares each cap's fc_sim to the closest spec.
    """
    if not claude_md_path or not claude_md_path.exists():
        return []
    text = claude_md_path.read_text(errors='ignore', encoding="utf-8")
    seen = []
    for line in text.splitlines():
        if not _FC_LINE_RE.search(line):
            continue
        for m in _FC_VALUE_RE.finditer(line):
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            suffix = m.group(2).lower()
            mult = {'k': 1e3, 'm': 1e6, 'g': 1e9}.get(suffix, 1.0)
            hz = val * mult
            if hz < 0.1:
                continue
            if not any(abs(hz - x) / x < 0.01 for x in seen):
                seen.append(hz)
    return seen


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('analysis_json')
    p.add_argument('--output', '-o', default=None,
                   help='Output JSON (default: stdout)')
    p.add_argument('--ngspice', default=None,
                   help='ngspice binary path (default: auto-locate)')
    p.add_argument('--timeout', type=int, default=10)
    p.add_argument('--spec-fc', type=float, action='append', default=None,
                   help='Design target fc in Hz (e.g. 20000 for 20kHz). Can '
                        'pass multiple times for multi-fc designs '
                        '(--spec-fc 20000 --spec-fc 3400000). Verdict passes '
                        'if sim is within tolerance of ANY listed fc.')
    p.add_argument('--spec-tol-pct', type=float, default=20.0,
                   help='Tolerance band around --spec-fc, default ±20%% '
                        '(matches typical ±5%% C tolerance × 2 leg + R tol).')
    p.add_argument('--claude-md', type=Path, default=None,
                   help='Path to project CLAUDE.md for auto-extracting fc '
                        'spec. If --spec-fc not given, this file is parsed '
                        'for fc=<num><unit>Hz patterns.')
    args = p.parse_args()

    ngspice = args.ngspice or shutil.which('ngspice') or '/opt/homebrew/bin/ngspice'
    if not Path(ngspice).exists():
        print(f'ngspice not found at {ngspice}', file=sys.stderr)
        sys.exit(1)

    analysis = json.loads(Path(args.analysis_json).read_text(encoding="utf-8"))

    spec_fc_list = list(args.spec_fc or [])
    if not spec_fc_list and args.claude_md is not None:
        spec_fc_list = _parse_spec_fc_from_claude_md(args.claude_md)
        if spec_fc_list:
            specs_str = ', '.join(f'{s/1e3:.2f}kHz' for s in spec_fc_list)
            print(f"  📐 Spec fc auto-detected from CLAUDE.md: {specs_str}")

    se = find_aa_candidates(analysis)
    diff = find_diff_aa_candidates(analysis)

    # Drop SE candidates that are part of a diff candidate (CM cap legs)
    diff_cap_refs = {d['c_diff']['ref'] for d in diff}
    se = [c for c in se if c['cap']['ref'] not in diff_cap_refs]

    # Sim each
    workdir = Path(tempfile.mkdtemp(prefix='aa_filter_sim_'))
    findings = []

    # verdict → severity map for the v1.3 envelope. `pass` / `numeric_only` are
    # informational; mismatches and sim failures escalate.
    _VERDICT_SEVERITY = {
        'pass':           'info',
        'numeric_only':   'info',
        'spec_mismatch':  'warning',
        'sim_warn':       'warning',
        'sim_ran_no_fc':  'warning',
        'sim_failed':     'high',
        'unknown':        'info',
    }

    for cand in se:
        cir = workdir / f"se_{cand['cap']['ref']}.cir"
        write_se_aa_cir(cand, cir)
        sim = run_ngspice(cir, ngspice, timeout=args.timeout)
        ev = evaluate('single_ended', cand, sim, spec_fc_list, args.spec_tol_pct)
        comp_refs = [cand['cap']['ref']] + [b['first_r'] for b in cand.get('branches', []) if b.get('first_r')]
        finding = {
            'detector': 'aa_filter_sim',
            'rule_id': 'AAF-SE',
            'severity': _VERDICT_SEVERITY.get(ev['verdict'], 'info'),
            'confidence': 'deterministic',
            'evidence_source': 'simulation',
            'category': 'anti_aliasing',
            'summary': f"Single-ended AA filter on {cand['hot_net']} (C={cand['cap']['ref']}): {ev['verdict']}",
            'components': comp_refs,
            'nets': [cand['hot_net'], cand['gnd_net']],
            'pins': [],
            'cap': cand['cap'],
            'hot_net': cand['hot_net'],
            'gnd_net': cand['gnd_net'],
            'branches': cand['branches'],
            'r_eq_ohms': cand['r_eq_ohms'],
            **ev,
            'cir_file': str(cir),
        }
        findings.append(finding)

    for cand in diff:
        cir = workdir / f"diff_{cand['c_diff']['ref']}.cir"
        write_diff_aa_cir(cand, cir)
        sim = run_ngspice(cir, ngspice, timeout=args.timeout)
        ev = evaluate('differential', cand, sim, spec_fc_list, args.spec_tol_pct)
        comp_refs = [cand['c_diff']['ref'], cand['r_p']['ref'], cand['r_n']['ref']]
        finding = {
            'detector': 'aa_filter_sim',
            'rule_id': 'AAF-DIFF',
            'severity': _VERDICT_SEVERITY.get(ev['verdict'], 'info'),
            'confidence': 'deterministic',
            'evidence_source': 'simulation',
            'category': 'anti_aliasing',
            'summary': f"Differential AA filter on {cand['p_net']}/{cand['n_net']} (C={cand['c_diff']['ref']}): {ev['verdict']}",
            'components': comp_refs,
            'nets': [cand['p_net'], cand['n_net']],
            'pins': [],
            'cap': cand['c_diff'],
            'p_net': cand['p_net'],
            'n_net': cand['n_net'],
            'r_p': cand['r_p'],
            'r_n': cand['r_n'],
            'r_total_ohms': cand['r_total_ohms'],
            **ev,
            'cir_file': str(cir),
        }
        findings.append(finding)

    summary = {
        'total': len(findings),
        'pass': sum(1 for f in findings if f['verdict'] == 'pass'),
        'numeric_only': sum(1 for f in findings if f['verdict'] == 'numeric_only'),
        'spec_mismatch': sum(1 for f in findings if f['verdict'] == 'spec_mismatch'),
        'sim_warn': sum(1 for f in findings if f['verdict'] == 'sim_warn'),
        'fail': sum(1 for f in findings if f['verdict'] in ('sim_failed', 'sim_ran_no_fc')),
        'spec_fc_hz_list': spec_fc_list,
    }
    report = {
        'analyzer_type': 'aa_filter_sim',
        'schema_version': 1,
        'summary': summary,
        'findings': findings,
        'workdir': str(workdir),
        'simulator': ngspice,
    }
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        bits = [f"{summary['pass']} pass"]
        if summary['numeric_only']:
            bits.append(f"{summary['numeric_only']} numeric_only")
        if summary['spec_mismatch']:
            bits.append(f"{summary['spec_mismatch']} spec_mismatch")
        if summary['sim_warn']:
            bits.append(f"{summary['sim_warn']} sim_warn")
        if summary['fail']:
            bits.append(f"{summary['fail']} fail")
        print(f"AA filter sweep: {summary['total']} candidates — "
              f"{', '.join(bits)} → {args.output}")
    else:
        print(payload)


if __name__ == '__main__':
    main()
