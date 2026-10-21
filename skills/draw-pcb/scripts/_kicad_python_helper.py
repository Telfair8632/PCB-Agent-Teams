#!/usr/bin/env python3
"""KiCad bundled Python helper — uses pcbnew API for all PCB-modifying operations.

Runs with KiCad's bundled Python (NOT workspace .venv). Modes are dispatched
by the `mode` field in the JSON spec; see `MODES` at the bottom for the full
list. Key entry points:

  mode=create_pcb
    Input  : netlist + footprint libs → output .kicad_pcb
    Action : pcbnew.NewBoard, NETINFO_ITEM, FootprintLoad, pad.SetNet, SaveBoard
    Spec   : {mode, components[], nets[], footprint_libs[], output_pcb}

  mode=apply_layout
    Input  : existing .kicad_pcb + placement plan + board geometry
    Action : pcbnew load → set footprint positions → add Edge.Cuts outline +
             isolation slot(s) → save. Uses real courtyard bbox from FP.
             Placement is computed by `placement_v2/orchestrator.py` (in the
             workspace .venv) and handed in via `placements` + `board`.
    Spec   : {mode, pcb_path, placements{}, board{...,slots[]}, output_pcb}
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
import sys
from pathlib import Path

try:
    import pcbnew
except ImportError:
    print(json.dumps({"ok": False, "error": "pcbnew module unavailable",
                      "python_executable": sys.executable}))
    sys.exit(1)


# =============================================================
# Common: footprint search
# =============================================================

def find_footprint_lib_path(footprint_spec, lib_search_dirs):
    """Resolve 'LibName:FpName' to (lib_path, fp_name)."""
    if ":" not in footprint_spec:
        return None, None
    lib_name, fp_name = footprint_spec.split(":", 1)
    for d in lib_search_dirs:
        candidate = Path(d) / f"{lib_name}.pretty"
        if candidate.is_dir() and (candidate / f"{fp_name}.kicad_mod").exists():
            return str(candidate), fp_name
    return None, None


# =============================================================
# Mode: create_pcb
# =============================================================

def mode_create_pcb(spec):
    components = spec["components"]
    nets = spec["nets"]
    lib_search_dirs = spec["footprint_libs"]
    output_pcb = spec["output_pcb"]

    Path(output_pcb).parent.mkdir(parents=True, exist_ok=True)
    board = pcbnew.NewBoard(output_pcb)

    # Inject baseline design rules for DRC.
    # KiCad 10 BOARD_DESIGN_SETTINGS exposes these as struct members, not setters.
    ds = board.GetDesignSettings()
    ds.m_CopperEdgeClearance = pcbnew.FromMM(0.5)
    ds.m_MinClearance = pcbnew.FromMM(0.2)

    # Also match the Default netclass clearance, which DRC reads as the
    # per-net minimum.
    try:
        nc_settings = ds.GetNetClasses()
        default_nc = None
        try:
            default_nc = nc_settings.GetDefault()
        except Exception:
            try:
                default_nc = nc_settings["Default"]
            except Exception:
                pass
        if default_nc is not None:
            # Match netclass clearance to design-rule min_clearance (0.2mm).
            # Bumping higher (e.g. 0.25) creates DRC failures on through-hole
            # pads with naturally tight pitch (TO-92, DIP-3, etc) where
            # adjacent pads are < 0.25mm apart by footprint design.
            default_nc.SetClearance(pcbnew.FromMM(0.2))
    except Exception:
        # API drift across KiCad minor versions — fall back to defaults.
        pass

    # Add nets
    net_obj = {}
    for net in nets:
        name = net["name"]
        if name in ("", "/"):
            continue
        ni = pcbnew.NETINFO_ITEM(board, name)
        board.Add(ni)
        net_obj[name] = ni

    # Build pad → net lookup
    ref_pad_nets = {}
    for net in nets:
        for pin in net["pins"]:
            ref_pad_nets.setdefault(pin["ref"], {})[pin["pin"]] = net["name"]

    # Add footprints
    added = 0
    pad_assigned = 0
    missing = []
    for c in components:
        ref, value, fp_spec = c["ref"], c["value"], c["footprint"]
        if not fp_spec:
            missing.append({"ref": ref, "reason": "no footprint"})
            continue
        lib_path, fp_name = find_footprint_lib_path(fp_spec, lib_search_dirs)
        if not lib_path:
            missing.append({"ref": ref, "footprint": fp_spec, "reason": "lib not found"})
            continue
        try:
            fp = pcbnew.FootprintLoad(lib_path, fp_name)
        except Exception as e:
            missing.append({"ref": ref, "footprint": fp_spec, "reason": str(e)})
            continue
        if fp is None:
            missing.append({"ref": ref, "footprint": fp_spec, "reason": "load returned None"})
            continue

        fp.SetReference(ref)
        fp.SetValue(value)
        fp.SetPosition(pcbnew.VECTOR2I_MM(0, 0))

        for pad in fp.Pads():
            num = pad.GetPadName()
            net_name = ref_pad_nets.get(ref, {}).get(num)
            if net_name and net_name in net_obj:
                pad.SetNet(net_obj[net_name])
                pad_assigned += 1

        board.Add(fp)
        added += 1

    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    # Patch the .kicad_pro design rules + netclass directly. KiCad CLI's DRC
    # reads design rules from .kicad_pro (NOT from the .kicad_pcb's setup
    # block we just wrote). Without this, hand-routed 0.2mm tracks may trip
    # `track_width` violations against an old min_track_width=0.25 from a
    # stale project file.
    # These are only permissive STARTING values so a fresh board is not
    # blocked by a stale project file. To hold the board to a fab's actual
    # spec, run tools/set_design_rules.py — it parameterises every rule here
    # plus netclasses, and echoes back what took effect.
    pro_path = Path(output_pcb).with_suffix(".kicad_pro")
    if pro_path.exists():
        try:
            pro = json.loads(pro_path.read_text(encoding="utf-8"))
            ds_rules = pro.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
            # Permit thin tracks for manual routing.
            ds_rules["min_track_width"] = 0.2
            ds_rules["min_clearance"] = 0.2
            ds_rules["min_copper_edge_clearance"] = 0.5
            # Match Default netclass clearance to global min_clearance.
            for nc in pro.setdefault("net_settings", {}).setdefault("classes", []):
                if nc.get("name") == "Default":
                    nc["clearance"] = 0.2
                    break
            pro_path.write_text(json.dumps(pro, indent=2), encoding="utf-8")
        except Exception as e:
            # Non-fatal: pipeline still works with default rules
            pass

    return {"ok": True, "output_pcb": output_pcb,
            "footprints_added": added, "nets_added": len(net_obj),
            "pad_net_assignments": pad_assigned, "missing": missing,
            "pcbnew_version": pcbnew.GetBuildVersion()}


# =============================================================
# Net classification + footprint helpers
# (shared by classify_fps, check_slot_clearance, etc.)
# =============================================================

# Net classification patterns
HV_NET_PATTERNS = ["hv_gnd", "hv+", "hv-", "vinp", "vinn", "hv_div", "hv_sense", "vdd1"]
LV_NET_PATTERNS = ["gnd", "3v3", "5v", "+5v", "vdda", "vdd2", "vcc"]
HV_EXACT = {"/hv_gnd", "hv_gnd"}
ISO_VALUE_KEYWORDS = ["amc", "iso", "b0505", "ib0505", "acpl", "hcpl", "si86", "isow"]
CONN_LIB_KEYWORDS = ["connector", "pinheader", "terminalblock", "conn"]


def is_hv_net(name):
    n = name.lower().strip("/")
    return n in HV_EXACT or any(p in n for p in HV_NET_PATTERNS)


def is_lv_net(name):
    n = name.lower().strip("/")
    if n in HV_EXACT:
        return False
    return any(p in n for p in LV_NET_PATTERNS)


def classify_fp(fp):
    """Return ('HV'/'LV'/'ISO', is_connector, connector_type).

    ISO zone is reserved for parts that physically span the isolation barrier
    (slot-straddling ICs). Passives (R/C/D/L) sit on one side even when their
    nets touch both domains via shared GND, so they must NOT be classified as
    ISO — otherwise they get bundled into iso_fps and consume vertical slot
    space, pushing real ISO ICs against y_bot and overlapping each other.
    """
    nets = set()
    for pad in fp.Pads():
        net_name = pad.GetNetname()
        if net_name:
            nets.add(net_name)

    has_hv = any(is_hv_net(n) for n in nets)
    has_lv = any(is_lv_net(n) for n in nets)

    ref = fp.GetReference() or ""
    is_passive = ref[:1] in ("R", "C", "D", "L")

    if has_hv and has_lv and not is_passive:
        zone = "ISO"
    elif has_hv and not has_lv:
        zone = "HV"
    elif has_hv and is_passive:
        zone = "HV"  # passive touching both → assign by HV preference (HV pads dominate)
    else:
        zone = "LV"

    val_lower = fp.GetValue().lower()
    if any(k in val_lower for k in ISO_VALUE_KEYWORDS) and not is_passive:
        zone = "ISO"

    fp_id = fp.GetFPIDAsString().lower()
    is_conn = any(k in fp_id for k in CONN_LIB_KEYWORDS)

    # Connector subtype
    conn_type = None
    if is_conn:
        net_str = " ".join(n.lower() for n in nets)
        if "hv+" in net_str or "hv-" in net_str:
            conn_type = "HV_IN"
        elif "adc" in net_str:
            conn_type = "SIGNAL"
        elif "5v_sw" in net_str or "pwr_out" in net_str:
            conn_type = "PWR_OUT"
        elif "+5v" in net_str:
            conn_type = "PWR_IN"

    return zone, is_conn, conn_type, nets


# =============================================================
# Mode: apply_layout — set positions + add Edge.Cuts outline
# =============================================================

def mode_apply_layout(spec):
    pcb_path = spec["pcb_path"]
    placements = spec["placements"]
    board_cfg = spec["board"]
    output_pcb = spec.get("output_pcb", pcb_path)

    board = pcbnew.LoadBoard(pcb_path)

    # ── Set footprint positions (body bbox center at target, not anchor) ──
    # Some KiCad footprints (e.g. Package_DIP:DIP-4_W7.62mm) have anchor at pad 1
    # not body center. We compensate so target = body center, ensuring ISO ICs
    # placed on slot midline have body straddling slot (not pin in slot).
    placed = 0
    not_found = []
    for fp in list(board.GetFootprints()):
        ref = fp.GetReference()
        if ref not in placements:
            not_found.append(ref)
            continue
        x, y, rot = placements[ref]
        target = pcbnew.VECTOR2I_MM(float(x), float(y))
        fp.SetOrientationDegrees(float(rot))
        fp.SetPosition(target)
        # After SetOrientation + SetPosition, body bbox may be offset from target
        # if footprint anchor != body center. Nudge to center body on target.
        bbox = fp.GetBoundingBox(False, False)  # exclude text
        center = bbox.GetCenter()
        delta_x = target.x - center.x
        delta_y = target.y - center.y
        if delta_x != 0 or delta_y != 0:
            fp.Move(pcbnew.VECTOR2I(int(delta_x), int(delta_y)))
        placed += 1

    # ── Edge.Cuts ──
    # A board outline is often NOT ours to choose: an enclosure, a connector
    # cutout pattern or a customer spec fixes it, and the placement has to fit
    # inside what already exists. keep_outline leaves Edge.Cuts untouched.
    edge_layer = board.GetLayerID("Edge.Cuts")
    keep_outline = bool(spec.get("keep_outline", False))
    if not keep_outline:
        drawings_to_remove = []
        for drawing in list(board.GetDrawings()):
            if drawing.GetLayer() == edge_layer:
                drawings_to_remove.append(drawing)
        for d in drawings_to_remove:
            board.Remove(d)

    # Track removal is moved to the end (after silk processing) — doing
    # it here invalidates pcbnew SWIG handles for subsequent
    # footprint loops in some KiCad Python wrappers.

    # ── Add new Edge.Cuts outline ──
    x1 = board_cfg["x_left"] - board_cfg["margin"] / 2
    y1 = board_cfg["y_top"] - board_cfg["margin"] / 2
    x2 = board_cfg["x_right"] + board_cfg["margin"] / 2
    y2 = board_cfg["y_bot"] + board_cfg["margin"] / 2

    # Slots: prefer the list form (board.slots = [{x_mm, width_mm, ...}]) that
    # v2 floorplan produces; fall back to the single slot_x/slot_w pair the
    # orchestrator still emits as a safety net. List is validated entry-by-entry
    # so a malformed entry doesn't take down the rest.
    slots_raw = board_cfg.get("slots") if isinstance(board_cfg.get("slots"), list) else None
    slots: list = []
    if slots_raw:
        for s in slots_raw:
            if not isinstance(s, dict):
                continue
            x = s.get("x_mm")
            w = s.get("width_mm")
            if x is None or w is None or w <= 0:
                continue
            slots.append({"x1": float(x) - float(w) / 2.0,
                          "x2": float(x) + float(w) / 2.0})
    else:
        sx = board_cfg.get("slot_x")
        sw = board_cfg.get("slot_w")
        if sx is not None and sw and float(sw) > 0:
            slots.append({"x1": float(sx), "x2": float(sx) + float(sw)})

    def add_edge_line(sx, sy, ex, ey):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(edge_layer)
        seg.SetStart(pcbnew.VECTOR2I_MM(float(sx), float(sy)))
        seg.SetEnd(pcbnew.VECTOR2I_MM(float(ex), float(ey)))
        seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)

    slot_lines_drawn = 0
    if keep_outline:
        # The existing outline stands, so measure it instead of drawing one —
        # downstream (pad-conflict sweep) needs the real board bbox.
        bb = board.GetBoardEdgesBoundingBox()
        x1 = pcbnew.ToMM(bb.GetX())
        y1 = pcbnew.ToMM(bb.GetY())
        x2 = x1 + pcbnew.ToMM(bb.GetWidth())
        y2 = y1 + pcbnew.ToMM(bb.GetHeight())
    else:
        # Outer rectangle (continuous — slot is INTERNAL cutout, not breaking outer edge)
        add_edge_line(x1, y1, x2, y1)
        add_edge_line(x2, y1, x2, y2)
        add_edge_line(x2, y2, x1, y2)
        add_edge_line(x1, y2, x1, y1)

        # Internal isolation slots — leave generous PCB bridges (3mm) at top + bottom
        # so HV/LV halves are mechanically rigid AND ISO ICs (AMC1311 SOIC-8W,
        # IB0505 DIP-4) have continuous board substrate under their bodies.
        # The slot is a non-conductive gap providing electrical isolation; the bridges
        # provide mechanical strength and routing-friendly connectivity for ground.
        bridge = 3.0  # mm — wide enough to be milled reliably and stay rigid
        for slot in slots:
            sx1 = slot["x1"]
            sx2 = slot["x2"]
            add_edge_line(sx1, y1 + bridge, sx2, y1 + bridge)      # slot top
            add_edge_line(sx2, y1 + bridge, sx2, y2 - bridge)      # slot right wall
            add_edge_line(sx2, y2 - bridge, sx1, y2 - bridge)      # slot bottom
            add_edge_line(sx1, y2 - bridge, sx1, y1 + bridge)      # slot left wall
            slot_lines_drawn += 4

    # ── Pad-pad conflict guard (different-net pads must not overlap) ──
    # Older placement runs occasionally put decoupling caps too close to ISO IC
    # output pads (e.g. C8/LV_GND vs U1.6/AMC_OUTN); DRC then reports shorting_items.
    # Sweep all pad pairs; if two pads on different nets are closer than
    # min_clearance_mm, slide the smaller (passive) footprint outward.
    # 0.5 = baseline DRC clearance + small safety margin so slid passives
    # never end up at exactly the limit (which DRC then flags as "clearance").
    pad_min_clearance = float(spec.get("pad_min_clearance_mm", 0.5))
    # Pass the board outline bbox we just drew — _resolve_pad_conflicts
    # used to discover it via board.GetDrawings(), but that pcbnew
    # wrapper breaks under some KiCad versions ('SwigPyObject not
    # iterable'). We already know the rect, so hand it over directly.
    edge_bbox = (x1, y1, x2, y2)
    # Caller may skip the inline pad-resolve sweep — pipeline runs a fresh
    # `resolve_pad_conflicts` helper invocation afterwards, which avoids the
    # macOS SWIG 'SwigPyObject has no Pads' regression seen on long-lived
    # boards in this process.
    if bool(spec.get("skip_pad_resolve", False)):
        fixed_pad_conflicts = []
    else:
        fixed_pad_conflicts = _resolve_pad_conflicts(board, pad_min_clearance, edge_bbox)

    # ── Hide silk reference text + footprint outline (avoids silk_over_copper) ──
    # KiCad will still render fab-layer references for assembly. Production silk
    # for analog/power boards rarely needs ref designators or body outlines on
    # every part — both routinely overlap pads / mask openings.
    # Set keep_silk_refs=True to keep reference text only; keep_silk_graphics=True
    # to keep body outlines.
    keep_silk_refs = bool(spec.get("keep_silk_refs", False))
    keep_silk_graphics = bool(spec.get("keep_silk_graphics", False))
    silk_layer_ids = {board.GetLayerID("F.Silkscreen"), board.GetLayerID("B.Silkscreen")}
    cmts_layer_id = board.GetLayerID("Cmts.User")
    silk_hidden = 0
    silk_graphics_hidden = 0
    for fp in list(board.GetFootprints()):
        if not keep_silk_refs:
            ref_field = fp.Reference()
            if ref_field and ref_field.IsVisible():
                ref_field.SetVisible(False)
                silk_hidden += 1
        if not keep_silk_graphics:
            # PCB_SHAPE has no IsVisible; reassign to Cmts.User so DRC + fab
            # silk export ignore them while geometry stays for KiCad 3D / debug.
            for item in fp.GraphicalItems():
                if item.GetLayer() in silk_layer_ids:
                    item.SetLayer(cmts_layer_id)
                    silk_graphics_hidden += 1

    # ── Remove old tracks (must be the last mutation before save) ──
    # Without this, replaying placement leaves stale traces pointing at
    # the previous footprint locations, which looks like broken routing
    # in PDF reviews. Done last because Remove() of tracks invalidates
    # SWIG handles for any subsequent footprint iteration.
    tracks_removed = 0
    if not bool(spec.get("keep_tracks", False)):
        for trk in list(board.GetTracks()):
            board.Remove(trk)
            tracks_removed += 1

    # ── Save ──
    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    return {
        "ok": True,
        "output_pcb": output_pcb,
        "footprints_placed": placed,
        "not_in_placement": not_found,
        "edge_lines_drawn": 8,  # 4 outer + 4 slot
        "pad_conflicts_resolved": fixed_pad_conflicts,
        "silk_refs_hidden": silk_hidden,
        "silk_graphics_hidden": silk_graphics_hidden,
        "pcbnew_version": pcbnew.GetBuildVersion(),
    }


def _resolve_pad_conflicts(board, min_clearance_mm, edge_bbox=None):
    """Slide footprints outward until no two different-net pads are closer than
    min_clearance_mm. ICs (any U-prefix), connectors, and large parts are NEVER
    moved — only passives (R/C/D/L) are nudged, since ICs/connectors are placed
    deliberately by the placement pipeline and slot-clearance phases. After each move, the
    mover is also clamped to stay inside the board outline (minus board_inset)
    so we don't push a cap off the board to escape an IC pad.
    Returns list of {ref, dx, dy} moves.
    """
    def is_movable(fp):
        ref = fp.GetReference()
        if not ref:
            return False
        # Lock all U-prefix (ICs incl. ISO IC, LDO, sensors) and J-prefix (connectors)
        if ref[0] in ("U", "J", "X", "Y"):
            return False
        return True

    # Board outline bbox — caller passes the rect we just drew when
    # available (avoids board.GetDrawings() which breaks under some
    # pcbnew Python wrappers). Falls back to no-clamp when unknown.
    board_inset = 0.9
    if edge_bbox is not None:
        ex1, ey1, ex2, ey2 = edge_bbox
        bx_min = min(ex1, ex2) + board_inset
        bx_max = max(ex1, ex2) - board_inset
        by_min = min(ey1, ey2) + board_inset
        by_max = max(ey1, ey2) - board_inset
    else:
        bx_min = by_min = -1e6
        bx_max = by_max = 1e6

    def clamp_inside(fp):
        """If mover's pad bbox extends beyond board, snap fp position so all pads stay inside."""
        bbox = fp.GetBoundingBox(False, False)
        x_min_mm = pcbnew.ToMM(bbox.GetX())
        x_max_mm = pcbnew.ToMM(bbox.GetX() + bbox.GetWidth())
        y_min_mm = pcbnew.ToMM(bbox.GetY())
        y_max_mm = pcbnew.ToMM(bbox.GetY() + bbox.GetHeight())
        snap_dx = snap_dy = 0.0
        if x_min_mm < bx_min: snap_dx = bx_min - x_min_mm
        elif x_max_mm > bx_max: snap_dx = bx_max - x_max_mm
        if y_min_mm < by_min: snap_dy = by_min - y_min_mm
        elif y_max_mm > by_max: snap_dy = by_max - y_max_mm
        if snap_dx or snap_dy:
            fp.Move(pcbnew.VECTOR2I(int(pcbnew.FromMM(snap_dx)),
                                    int(pcbnew.FromMM(snap_dy))))

    moves = []
    max_iters = 20
    for _ in range(max_iters):
        # Build pad list once per pass: (fp, pad, x_mm, y_mm, half_size_mm, net_code)
        pads = []
        for fp in list(board.GetFootprints()):
            for pad in fp.Pads():
                pos = pad.GetPosition()
                size = pad.GetSize()
                pads.append((
                    fp, pad,
                    pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y),
                    max(pcbnew.ToMM(size.x), pcbnew.ToMM(size.y)) / 2.0,
                    pad.GetNetCode(),
                ))
        # O(N^2) sweep — only flag pairs from different fps with different nets
        any_moved = False
        for i in range(len(pads)):
            fp_a, pad_a, ax, ay, ar, anet = pads[i]
            for j in range(i + 1, len(pads)):
                fp_b, pad_b, bx, by, br, bnet = pads[j]
                if fp_a is fp_b:
                    continue
                if anet == bnet and anet != 0:
                    continue
                dx = bx - ax
                dy = by - ay
                dist = (dx * dx + dy * dy) ** 0.5
                need = ar + br + min_clearance_mm
                if dist >= need:
                    continue
                # Decide mover: passive only. If neither is movable, skip this
                # pair (we cannot slide ICs/connectors away from each other).
                a_movable = is_movable(fp_a)
                b_movable = is_movable(fp_b)
                if not a_movable and not b_movable:
                    continue
                if a_movable and not b_movable:
                    mover = fp_a; sign = -1.0
                elif b_movable and not a_movable:
                    mover = fp_b; sign = 1.0
                else:
                    bbox_a = fp_a.GetBoundingBox(False, False)
                    bbox_b = fp_b.GetBoundingBox(False, False)
                    area_a = pcbnew.ToMM(bbox_a.GetWidth()) * pcbnew.ToMM(bbox_a.GetHeight())
                    area_b = pcbnew.ToMM(bbox_b.GetWidth()) * pcbnew.ToMM(bbox_b.GetHeight())
                    if area_a <= area_b:
                        mover = fp_a; sign = -1.0
                    else:
                        mover = fp_b; sign = 1.0
                # Push along (dx, dy) by overlap + small margin
                if dist < 1e-6:
                    push_x, push_y = need, 0.0
                else:
                    push = (need - dist) + 0.05
                    push_x = sign * push * (dx / dist)
                    push_y = sign * push * (dy / dist)
                mover.Move(pcbnew.VECTOR2I(int(pcbnew.FromMM(push_x)),
                                           int(pcbnew.FromMM(push_y))))
                clamp_inside(mover)
                moves.append({
                    "ref": mover.GetReference(),
                    "dx_mm": round(push_x, 3),
                    "dy_mm": round(push_y, 3),
                    "vs": fp_b.GetReference() if mover is fp_a else fp_a.GetReference(),
                    "pad": pad_a.GetPadName() if mover is fp_a else pad_b.GetPadName(),
                })
                any_moved = True
                break  # restart inner; outer will re-scan next iter
            if any_moved:
                break
        if not any_moved:
            break

    # Final pass: clamp every movable fp inside board, even those untouched by
    # the sweep — handles passives the placement pipeline left too close to the
    # edge originally.
    for fp in list(board.GetFootprints()):
        if is_movable(fp):
            clamp_inside(fp)
    return moves


# =============================================================
# Main dispatcher
# =============================================================

# =============================================================
# Mode: classify_fps — read PCB, classify each footprint (read-only)
# =============================================================

def mode_classify_fps(spec):
    """Read PCB, return {ref: {zone, is_connector, connector_type}} for all footprints.

    Used by pipeline phase 2.5 to build anchor list for kicad-tools placement fix.
    """
    pcb_path = spec["pcb_path"]
    board = pcbnew.LoadBoard(pcb_path)

    classifications = {}
    for fp in list(board.GetFootprints()):
        ref = fp.GetReference()
        zone, is_conn, ctype, _nets = classify_fp(fp)
        classifications[ref] = {
            "zone": zone,
            "is_connector": is_conn,
            "connector_type": ctype,
        }

    return {"ok": True, "classifications": classifications}


# =============================================================
# Mode: check_slot_clearance — detect & fix pads inside isolation slot
# =============================================================

def _detect_slot_from_edge_cuts(board):
    """Scan Edge.Cuts segments and detect interior vertical lines = slot."""
    edge_layer = board.GetLayerID("Edge.Cuts")
    edges = [d for d in list(board.GetDrawings()) if d.GetLayer() == edge_layer]
    xs, ys = [], []
    vertical_xs = []
    for e in edges:
        s = e.GetStart()
        en = e.GetEnd()
        sx, sy = pcbnew.ToMM(s.x), pcbnew.ToMM(s.y)
        ex, ey = pcbnew.ToMM(en.x), pcbnew.ToMM(en.y)
        xs.extend([sx, ex])
        ys.extend([sy, ey])
        # Vertical line: same X, different Y
        if abs(sx - ex) < 0.05 and abs(sy - ey) > 1.0:
            vertical_xs.append(round(sx, 2))

    if not xs:
        return None
    bx_min, bx_max = min(xs), max(xs)
    # Slot verticals are interior (away from outer edges)
    interior = sorted(set(x for x in vertical_xs if bx_min + 5 < x < bx_max - 5))
    if len(interior) < 2:
        return None
    return {"left": interior[0], "right": interior[-1],
            "width": interior[-1] - interior[0]}


def _hv_lv_pad_xs(fp):
    """Return (hv_xs, lv_xs): pad center X (mm) split by HV/LV net role.

    Pads whose net is neither HV nor LV (or unnamed) are ignored.
    """
    hv_xs, lv_xs = [], []
    for pad in fp.Pads():
        net_name = pad.GetNetname()
        if not net_name:
            continue
        px = pcbnew.ToMM(pad.GetPosition().x)
        if is_hv_net(net_name):
            hv_xs.append(px)
        elif is_lv_net(net_name):
            lv_xs.append(px)
    return hv_xs, lv_xs


def _barrier_score_for_pads(hv_xs, lv_xs, slot_left, slot_right):
    """Score how well HV/LV pads sit on the correct side of the barrier.

    Correct side: every HV pad at X < slot_left (HV side), every LV pad at
    X > slot_right (LV side). Per-pad signed distance to its own slot edge:
        HV pad -> (slot_left - px)   (>0 when on HV side)
        LV pad -> (px - slot_right)  (>0 when on LV side)

    Score = min over ALL HV+LV pads of that distance. A wrong-side pad gives
    a negative term, so any orientation with a pad on the wrong side scores
    below every fully-correct orientation. The chosen orientation maximizes
    this min distance => pushes the closest pad as far from the barrier as
    possible while keeping both groups on their correct sides.

    Returns the min-distance (mm); larger is better.
    """
    dists = [slot_left - px for px in hv_xs]
    dists += [px - slot_right for px in lv_xs]
    return min(dists) if dists else float("-inf")


def _set_orientation_recentered(fp, bbox_before, new_rot_deg):
    """Rotate fp to absolute new_rot_deg about its body center (preserve center)."""
    center_before = bbox_before.GetCenter()
    fp.SetOrientationDegrees(new_rot_deg)
    bbox_after = fp.GetBoundingBox(False, False)
    center_after = bbox_after.GetCenter()
    dx = center_before.x - center_after.x
    dy = center_before.y - center_after.y
    if dx != 0 or dy != 0:
        fp.Move(pcbnew.VECTOR2I(int(dx), int(dy)))


def _fix_iso_pad_orientation(board, slot_left, slot_right):
    """For each ISO IC straddling the slot, pick the orientation that best
    separates its HV pads (HV side, X < slot_left) from its LV pads
    (LV side, X > slot_right) across the isolation barrier.

    Tries 4 absolute angles (current rot + 0/90/180/270), keeping the body
    center fixed (rotation is about the footprint center, then re-centered),
    so the floorplan's slot geometry guarantee is never disturbed — only the
    IC's pad orientation changes, not the board outline or slot. Selects the
    angle that MAXIMIZES the minimum HV/LV-pad-to-barrier distance while both
    groups stay on their correct sides (see _barrier_score_for_pads). Ties
    break toward the smallest rotation delta (0 < 90 < 180 < 270), keeping the
    result deterministic. Applies only if the winner differs from the current
    orientation.

    Returns list of footprints that were re-oriented.
    """
    rotated = []
    for fp in list(board.GetFootprints()):
        ref = fp.GetReference()
        zone, is_conn, ctype, nets = classify_fp(fp)
        if zone != "ISO" or is_conn:
            continue
        # Skip if footprint is not actually straddling slot
        bbox = fp.GetBoundingBox(False, False)
        bx_min = pcbnew.ToMM(bbox.GetX())
        bx_max = pcbnew.ToMM(bbox.GetX() + bbox.GetWidth())
        if not (bx_min < slot_left and bx_max > slot_right):
            continue

        hv_xs, lv_xs = _hv_lv_pad_xs(fp)
        if not hv_xs or not lv_xs:
            continue

        current_rot = fp.GetOrientationDegrees()
        # Evaluate each candidate angle by actually applying it (re-centered),
        # measuring pad positions, then restoring before trying the next. This
        # is exact for arbitrary footprints/pin maps — no trig assumptions.
        best = None  # (score, delta, abs_rot, hv_mean, lv_mean)
        for delta in (0.0, 90.0, 180.0, 270.0):
            abs_rot = (current_rot + delta) % 360.0
            _set_orientation_recentered(fp, bbox, abs_rot)
            c_hv, c_lv = _hv_lv_pad_xs(fp)
            score = _barrier_score_for_pads(c_hv, c_lv, slot_left, slot_right)
            cand = (score, -delta, abs_rot,
                    sum(c_hv) / len(c_hv) if c_hv else 0.0,
                    sum(c_lv) / len(c_lv) if c_lv else 0.0)
            if best is None or cand[:2] > best[:2]:
                best = cand
        # Restore to current orientation, then apply the winner (if it differs).
        _set_orientation_recentered(fp, bbox, current_rot)

        _score, _negdelta, win_rot, hv_mean, lv_mean = best
        if abs(((win_rot - current_rot) % 360.0)) > 1e-6:
            _set_orientation_recentered(fp, bbox, win_rot)
            rotated.append({"ref": ref, "old_rot": current_rot, "new_rot": win_rot,
                            "hv_mean_x": round(hv_mean, 2), "lv_mean_x": round(lv_mean, 2),
                            "barrier_margin_mm": round(_score, 3)})
    return rotated


def mode_check_slot_clearance(spec):
    """Detect pads inside slot + ISO IC pad orientation. Auto-fix:
       1. Rotate ISO IC 180° if HV pads on LV side
       2. Move footprint out of slot if pads inside

    Spec:
      pcb_path: input PCB
      output_pcb: output (default = pcb_path)
      auto_fix: bool, if True apply rotations and moves
      pad_margin_mm: clearance margin from slot edges (default 0.5mm)
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    auto_fix = spec.get("auto_fix", True)
    margin = spec.get("pad_margin_mm", 0.5)

    board = pcbnew.LoadBoard(pcb_path)
    slot = _detect_slot_from_edge_cuts(board)
    if not slot:
        return {"ok": True, "skipped": True, "reason": "no slot detected in Edge.Cuts"}

    # Step 1: ensure ISO IC pad orientation matches HV/LV zones (rotate 180° if reversed)
    rotated_iso = _fix_iso_pad_orientation(board, slot["left"], slot["right"]) if auto_fix else []

    safe_left = slot["left"] - margin
    safe_right = slot["right"] + margin

    violations = []
    for fp in list(board.GetFootprints()):
        ref = fp.GetReference()
        bad_pads = []
        for pad in fp.Pads():
            pp = pad.GetPosition()
            psize = pad.GetSize()
            px = pcbnew.ToMM(pp.x)
            pw = pcbnew.ToMM(psize.x)
            x_min = px - pw / 2
            x_max = px + pw / 2
            if x_max > safe_left and x_min < safe_right:
                bad_pads.append({"pad": pad.GetPadName(), "x": px})
        if bad_pads:
            # Determine which side to push to: based on net classification
            zone, is_conn, ctype, nets = classify_fp(fp)
            push_left = (zone == "HV")  # HV → left of slot; LV/ISO → right
            violations.append({
                "ref": ref,
                "fp_x": pcbnew.ToMM(fp.GetPosition().x),
                "fp_y": pcbnew.ToMM(fp.GetPosition().y),
                "zone": zone,
                "bad_pads": bad_pads,
                "push_left": push_left,
            })

    fixed = []
    if auto_fix and violations:
        # Push slot-straddling footprints to the safe side WITHOUT colliding
        # with footprints already placed by Phase C layout. Strategy: build a Y→X
        # extent map of "obstacles" (existing fps on the target side) and
        # for each pushed fp scan a Y window of ±half_h around its current Y
        # to find the rightmost (LV) / leftmost (HV) obstacle edge that
        # overlaps in Y. Push behind that, plus a stack cursor so the moved
        # IC's themselves don't collide with each other either.
        gap_mm = 0.6
        moving_refs = {v["ref"] for v in violations}

        # Determine board outline X bounds — push must not exit the board.
        board_xs = []
        edge_layer = board.GetLayerID("Edge.Cuts")
        for d in list(board.GetDrawings()):
            if d.GetLayer() != edge_layer:
                continue
            s = d.GetStart()
            e = d.GetEnd()
            board_xs.extend([pcbnew.ToMM(s.x), pcbnew.ToMM(e.x)])
        if board_xs:
            board_x_min = min(board_xs) + 0.5
            board_x_max = max(board_xs) - 0.5
        else:
            board_x_min = -1e9
            board_x_max = 1e9

        # Snapshot obstacle bboxes (everything not being moved)
        obstacles = []  # list of (xmin, ymin, xmax, ymax)
        for fp in list(board.GetFootprints()):
            if fp.GetReference() in moving_refs:
                continue
            bb = fp.GetBoundingBox(False, False)
            obstacles.append((
                pcbnew.ToMM(bb.GetX()),
                pcbnew.ToMM(bb.GetY()),
                pcbnew.ToMM(bb.GetX() + bb.GetWidth()),
                pcbnew.ToMM(bb.GetY() + bb.GetHeight()),
            ))

        def y_overlap(a_lo, a_hi, b_lo, b_hi):
            return not (a_hi < b_lo or b_hi < a_lo)

        def find_free_y(half_w, half_h, board_x_min, board_x_max,
                        target_left_x, push_left, obs):
            """Sweep Y in 1mm increments to find a row where the fp can sit
            on the slot edge without colliding any obstacle. Returns target
            (center_x, center_y) or None if board is full.
            """
            # Y candidates: centers spaced by 2*half_h within board Y
            # (we don't have explicit board_y_min/max here — use obstacles
            # extent as proxy plus a margin)
            ys = [oy for ob in obs for oy in (ob[1], ob[3])]
            if not ys:
                return None
            y_min = min(ys) - 5
            y_max = max(ys) + 5
            best = None
            y = y_min + half_h
            while y <= y_max - half_h:
                y_lo = y - half_h
                y_hi = y + half_h
                if push_left:
                    cx = target_left_x + half_w
                    new_left = target_left_x
                    for ox_min, oy_min, ox_max, oy_max in obs:
                        if y_overlap(y_lo, y_hi, oy_min, oy_max):
                            cand_right = ox_min - gap_mm
                            cand_left = cand_right - 2 * half_w
                            if cand_left < new_left:
                                new_left = cand_left
                    cx = new_left + half_w
                    if new_left >= board_x_min:
                        if best is None or new_left > best[0] - half_w:
                            best = (cx, y)
                else:
                    new_left = target_left_x
                    for ox_min, oy_min, ox_max, oy_max in obs:
                        if y_overlap(y_lo, y_hi, oy_min, oy_max):
                            cand_left = ox_max + gap_mm
                            if cand_left > new_left:
                                new_left = cand_left
                    cx = new_left + half_w
                    if new_left + 2 * half_w <= board_x_max:
                        if best is None or new_left < best[0] - half_w:
                            best = (cx, y)
                y += max(2.0, half_h)
            return best

        # Stack cursor for already-moved fps (avoid IC-vs-IC collision among
        # the pushed set itself).
        moved_extents = []  # list of (xmin, ymin, xmax, ymax)

        violations_sorted = sorted(
            violations,
            key=lambda v: (v["push_left"], v.get("fp_y", 0.0)),
        )
        for v in violations_sorted:
            fp = board.FindFootprintByReference(v["ref"])
            if not fp:
                continue
            bbox = fp.GetBoundingBox(False, False)
            w_mm = pcbnew.ToMM(bbox.GetWidth())
            h_mm = pcbnew.ToMM(bbox.GetHeight())
            half_w = w_mm / 2
            half_h = h_mm / 2
            body_x = pcbnew.ToMM(bbox.GetCenter().x)
            body_y = pcbnew.ToMM(bbox.GetCenter().y)
            current_y = body_y
            y_lo = current_y - half_h
            y_hi = current_y + half_h

            # Initial target = just past the slot edge
            if v["push_left"]:
                target_left = safe_left - w_mm
            else:
                target_left = safe_right
            # Bump past any obstacle whose Y range intersects ours
            for ob in obstacles + moved_extents:
                ox_min, oy_min, ox_max, oy_max = ob
                if not y_overlap(y_lo, y_hi, oy_min, oy_max):
                    continue
                if v["push_left"]:
                    candidate_right = ox_min - gap_mm
                    new_left = candidate_right - w_mm
                    if new_left < target_left:
                        target_left = new_left
                else:
                    candidate_left = ox_max + gap_mm
                    if candidate_left > target_left:
                        target_left = candidate_left

            target_center_x = target_left + half_w
            target_center_y = current_y

            # Board-edge fallback: if the X-stack would push us past the
            # board outline, scan Y rows for a free spot at the slot edge
            # instead. Without this, anchored ISO ICs get pushed off-board
            # whenever the LV side is densely packed.
            out_of_board = (
                (v["push_left"] and target_left < board_x_min) or
                (not v["push_left"] and target_left + w_mm > board_x_max)
            )
            if out_of_board:
                slot_left_target = safe_left - w_mm if v["push_left"] else safe_right
                free = find_free_y(
                    half_w, half_h, board_x_min, board_x_max,
                    slot_left_target, v["push_left"], obstacles + moved_extents,
                )
                if free is not None:
                    target_center_x, target_center_y = free

            target = pcbnew.VECTOR2I_MM(target_center_x, target_center_y)
            fp.SetPosition(target)
            bbox2 = fp.GetBoundingBox(False, False)
            center = bbox2.GetCenter()
            dx = target.x - center.x
            dy = target.y - center.y
            if dx != 0 or dy != 0:
                fp.Move(pcbnew.VECTOR2I(int(dx), int(dy)))

            # Record moved extent so subsequent moved fps avoid it too
            bb_now = fp.GetBoundingBox(False, False)
            moved_extents.append((
                pcbnew.ToMM(bb_now.GetX()),
                pcbnew.ToMM(bb_now.GetY()),
                pcbnew.ToMM(bb_now.GetX() + bb_now.GetWidth()),
                pcbnew.ToMM(bb_now.GetY() + bb_now.GetHeight()),
            ))
            fixed.append({
                "ref": v["ref"],
                "from_body_x": body_x,
                "to_body_x": pcbnew.ToMM(fp.GetBoundingBox(False, False).GetCenter().x),
                "preserved_body_y": body_y,
                "side": "HV" if v["push_left"] else "LV",
            })

    # Save if any rotation OR any move applied
    if auto_fix and (rotated_iso or fixed):
        if not pcbnew.SaveBoard(output_pcb, board):
            return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    return {
        "ok": True,
        "slot": slot,
        "iso_rotations": rotated_iso,
        "iso_rotations_count": len(rotated_iso),
        "violations": violations,
        "fixed": fixed,
        "fixed_count": len(fixed),
    }


# =============================================================
# Mode: add_ground_zones — add B.Cu copper pour for GND-like nets
# =============================================================

def mode_add_ground_zones(spec):
    """Add a copper pour zone for each ground net (GND, HV_GND, LV_GND, etc).

    Behaviour notes (post-fix):
      1. Idempotent: removes any existing zones for matching (net, layer)
         before adding. Re-running pipeline no longer accumulates duplicates
         (which used to trip DRC zones_intersect on same-net same-priority).
      2. HV/LV split: HV_GND zones are clipped to the slot-LEFT half of the
         board, LV_GND zones to the slot-RIGHT half. Plain '/GND' falls back
         to full board. Avoids HV_GND / LV_GND zone overlap on B.Cu.
      3. Distinct priority per net (HV_GND=10, LV_GND=10, /GND=5, others=0).

    Spec:
      pcb_path: input PCB
      output_pcb: output (default = pcb_path)
      ground_net_keywords: list of substrings to match (default ['GND'])
      net_filter: optional list of substrings; if given, a matched net must
        ALSO contain one of these (e.g. ['LV_GND'] to pour only the LV
        ground). Lets the caller pour one net on a layer and not another.
      layers: copper layer(s); default ['B.Cu']
      clearance_mm: default 0.3
      slot_inset_mm: gap from slot edge (default 1.5)
      board_inset_mm: gap from board edge (default 0.5)
      rect_mm: optional [x_min, y_min, x_max, y_max] in mm. Confines the pour
        to that rectangle, INTERSECTED with the per-net rect (so the isolation
        slot clip still applies and a caller rect can never push copper across
        the barrier). A single zone carries one clearance, which cannot express
        a per-voltage creepage rule — on boards with HV nodes the caller passes
        a rect that keeps the pour off the HV area instead.
      fill_now: bool, run zone filler immediately after add (default True)
      pad_connect: 'thermal' (default, spoked relief — hand-soldering friendly)
        | 'solid' (full copper contact, no spokes to starve) | 'none'
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    keywords = spec.get("ground_net_keywords", ["GND"])
    net_filter = spec.get("net_filter") or None
    layer_names = spec.get("layers", ["B.Cu"])
    if isinstance(layer_names, str):
        layer_names = [layer_names]
    pad_connect = str(spec.get("pad_connect") or "thermal").lower()
    if pad_connect not in ("thermal", "solid", "none"):
        return {"ok": False,
                "error": f"pad_connect must be thermal|solid|none, got "
                         f"{pad_connect!r}"}
    clearance_mm = spec.get("clearance_mm", 0.3)
    slot_inset_mm = float(spec.get("slot_inset_mm", 1.5))
    board_inset_mm = float(spec.get("board_inset_mm", 0.5))
    fill_now = spec.get("fill_now", True)

    board = pcbnew.LoadBoard(pcb_path)

    # Find target nets
    nets = list(board.GetNetInfo().NetsByNetcode().items())
    target_nets = []
    for code, ni in nets:
        name = str(ni.GetNetname())
        if not (name and any(kw.upper() in name.upper() for kw in keywords)):
            continue
        if net_filter and not any(f.upper() in name.upper() for f in net_filter):
            continue
        target_nets.append((code, name))

    if not target_nets:
        return {"ok": True, "skipped": True, "reason": "no ground nets matched"}

    # Determine board outline rectangle from Edge.Cuts
    edge_layer = board.GetLayerID("Edge.Cuts")
    edges = [d for d in list(board.GetDrawings()) if d.GetLayer() == edge_layer]
    xs, ys = [], []
    for e in edges:
        s = e.GetStart()
        en = e.GetEnd()
        xs.extend([s.x, en.x])
        ys.extend([s.y, en.y])
    if not xs:
        return {"ok": False, "error": "no Edge.Cuts found, can't size zone"}
    bx_min, bx_max = min(xs), max(xs)
    by_min, by_max = min(ys), max(ys)
    inset_iu = pcbnew.FromMM(board_inset_mm)
    bx_min += inset_iu; bx_max -= inset_iu
    by_min += inset_iu; by_max -= inset_iu

    # Detect isolation slot to clip HV/LV zones
    slot = _detect_slot_from_edge_cuts(board)
    slot_inset_iu = pcbnew.FromMM(slot_inset_mm)
    slot_left_iu = pcbnew.FromMM(slot["left"]) - slot_inset_iu if slot else None
    slot_right_iu = pcbnew.FromMM(slot["right"]) + slot_inset_iu if slot else None

    # Resolve all layer IDs upfront
    layer_ids = []
    for ln in layer_names:
        lid = board.GetLayerID(ln)
        if lid < 0:
            return {"ok": False, "error": f"unknown layer: {ln}"}
        layer_ids.append((ln, lid))

    # Idempotency: drop any existing zones whose (net_code, layer) matches a
    # target. Without this, repeated pipeline runs stack zones at priority 0
    # and DRC reports zones_intersect for same-net overlaps.
    target_keys = {(code, lid) for code, _name in target_nets for _ln, lid in layer_ids}
    zones_removed = 0
    # board.Remove() hands ownership back to Python. Dropping the last ref frees the
    # C++ ZONE, the allocator hands the address to the ZONE we create below, and SWIG
    # returns the dead proxy for zone.Outline() -> AttributeError on AddOutline.
    # Keep the removed zones alive until this mode returns.
    removed_keepalive = []
    for z in list(board.Zones()):
        if (z.GetNetCode(), z.GetLayer()) in target_keys:
            board.Remove(z)
            removed_keepalive.append(z)
            zones_removed += 1

    def net_priority(name):
        upper = name.upper().lstrip("/")
        if upper == "GND":
            return 5
        if "HV" in upper or "LV" in upper:
            return 10
        return 0

    rect_mm = spec.get("rect_mm")
    caller_rect_iu = ([pcbnew.FromMM(float(v)) for v in rect_mm]
                      if rect_mm else None)

    def net_outline_rect(name):
        """Return outline rect (x_min, y_min, x_max, y_max) in IU based on net role."""
        upper = name.upper().lstrip("/")
        if slot_left_iu is not None and "HV" in upper:
            r = (bx_min, by_min, slot_left_iu, by_max)
        elif slot_right_iu is not None and "LV" in upper and "HV" not in upper:
            r = (slot_right_iu, by_min, bx_max, by_max)
        else:
            r = (bx_min, by_min, bx_max, by_max)  # plain /GND or no slot detected
        if caller_rect_iu is None:
            return r
        # Intersect, never union: the slot clip stays authoritative.
        return (max(r[0], caller_rect_iu[0]), max(r[1], caller_rect_iu[1]),
                min(r[2], caller_rect_iu[2]), min(r[3], caller_rect_iu[3]))

    zones_added = []
    for net_code, net_name in target_nets:
        x1, y1, x2, y2 = net_outline_rect(net_name)
        if x2 <= x1 or y2 <= y1:
            continue  # clipped to nothing
        prio = net_priority(net_name)
        for layer_label, layer_id in layer_ids:
            zone = pcbnew.ZONE(board)
            zone.SetLayer(layer_id)
            zone.SetNetCode(net_code)
            zone.SetLocalClearance(pcbnew.FromMM(clearance_mm))
            zone.SetMinThickness(pcbnew.FromMM(0.25))
            zone.SetThermalReliefGap(pcbnew.FromMM(0.3))
            zone.SetThermalReliefSpokeWidth(pcbnew.FromMM(0.4))

            outline = zone.Outline()
            chain = pcbnew.SHAPE_LINE_CHAIN()
            chain.Append(int(x1), int(y1))
            chain.Append(int(x2), int(y1))
            chain.Append(int(x2), int(y2))
            chain.Append(int(x1), int(y2))
            chain.SetClosed(True)
            outline.AddOutline(chain)

            zone.SetIsRuleArea(False)
            zone.SetAssignedPriority(prio)
            # KiCad default min_thermal_relief_count=2 fails for through-hole
            # mounting pads with 1 GND spoke (thermal relief picks one direction).
            # Lowering to 1 avoids "starved_thermal" warnings on isolated ground pads.
            try:
                zone.SetMinIslandArea(0)  # keep all GND islands even tiny
                if hasattr(zone, "SetThermalReliefSpokes"):
                    zone.SetThermalReliefSpokes(1)
            except Exception:
                pass
            # Pad connection: thermal relief (spokes) is the hand-soldering
            # default, but each spoke needs room — once traces crowd a pad the
            # remaining spokes get starved and DRC says so. Solid connection
            # removes that failure mode at the cost of a heat sink on every
            # pad, which matters for hand rework, not for reflow.
            if pad_connect == "solid":
                zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
            elif pad_connect == "none":
                zone.SetPadConnection(pcbnew.ZONE_CONNECTION_NONE)
            else:
                zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
            board.Add(zone)  # without this the ZONE is orphaned — never saved
            zones_added.append({
                "net": net_name, "code": net_code, "layer": layer_label,
                "priority": prio,
                "rect_mm": [round(pcbnew.ToMM(x1), 2), round(pcbnew.ToMM(y1), 2),
                            round(pcbnew.ToMM(x2), 2), round(pcbnew.ToMM(y2), 2)],
            })

    # Fill zones now
    fill_result = "skipped"
    if fill_now:
        try:
            filler = pcbnew.ZONE_FILLER(board)
            zone_objs = [z for z in board.Zones()]
            filler.Fill(zone_objs)
            fill_result = "filled"
        except Exception as e:
            fill_result = f"fill_failed: {e}"

    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    return {
        "ok": True,
        "output_pcb": output_pcb,
        "zones_added": zones_added,
        "zones_count": len(zones_added),
        "zones_removed": zones_removed,
        "slot_detected": slot is not None,
        "pad_connect": pad_connect,
        "fill_status": fill_result,
    }


# =============================================================
# Mode: validate_zones — assert no copper zone bridges the barrier
# =============================================================

def _zone_filled_x_extent(zone):
    """Return (min_x_mm, max_x_mm) over a zone's FILLED copper polygons,
    across every layer it is filled on. None if the zone has no fill.

    Uses the filled geometry (not the drawn outline) because that is the
    copper that actually exists after the zone filler runs and clips around
    the slot — the outline can legally span the slot while the fill does not.
    """
    x_min = None
    x_max = None
    for layer in zone.GetLayerSet().Seq():
        try:
            poly = zone.GetFilledPolysList(layer)
        except Exception:
            continue
        if poly is None:
            continue
        for i in range(poly.OutlineCount()):
            chain = poly.Outline(i)
            for p in range(chain.PointCount()):
                pt = chain.CPoint(p)
                xmm = pcbnew.ToMM(pt.x)
                x_min = xmm if x_min is None else min(x_min, xmm)
                x_max = xmm if x_max is None else max(x_max, xmm)
    if x_min is None:
        return None
    return (x_min, x_max)


def mode_validate_zones(spec):
    """Assert no copper pour zone bridges the isolation barrier.

    Geometry method (net-agnostic, voltage-agnostic, name-agnostic):
      1. Detect the isolation slot band [slot_left, slot_right] from the
         interior vertical Edge.Cuts lines (same detector the pour/clearance
         modes use).
      2. For each zone, take its FILLED copper extent in X. If a single zone's
         filled copper has area on the HV side (x < slot_left - tol) AND on the
         LV side (x > slot_right + tol), that one continuous net's copper spans
         the barrier -> galvanic isolation is broken -> error.

    A zone whose copper sits entirely on one side, or only inside the slot
    band, passes. This catches the failure DRC clearance only flags
    indirectly, before relying on the final DRC pass.

    Spec:
      pcb_path: input PCB (read-only; never saved)
      tol_mm:   slack added to each slot edge before judging a crossing
                (default 0.05mm, absorbs fill rounding at the slot edge)

    Returns {ok, slot_detected, zones_checked, crossings[], error?}. When a
    crossing is found, ok is True (the check ran) but `crossings` is non-empty
    and `verdict` == "fail"; callers should treat a non-empty `crossings`
    list as a hard failure.

    Limitation: like the pour/clearance modes, slot detection handles a single
    vertical barrier (HV-left / LV-right). Horizontal or multi-segment barriers
    are reported via slot_detected=False (check skipped) rather than wrongly.
    """
    pcb_path = spec["pcb_path"]
    tol = spec.get("tol_mm", 0.05)

    board = pcbnew.LoadBoard(pcb_path)
    slot = _detect_slot_from_edge_cuts(board)
    if not slot:
        return {"ok": True, "skipped": True, "slot_detected": False,
                "reason": "no single vertical isolation slot detected in Edge.Cuts",
                "verdict": "pass", "zones_checked": 0, "crossings": []}

    left = slot["left"] - tol
    right = slot["right"] + tol

    crossings = []
    zones_checked = 0
    for z in list(board.Zones()):
        if z.GetIsRuleArea():
            continue  # keep-out / rule areas carry no copper
        extent = _zone_filled_x_extent(z)
        if extent is None:
            continue  # unfilled zone — no copper to bridge anything
        zones_checked += 1
        zx_min, zx_max = extent
        on_hv_side = zx_min < left
        on_lv_side = zx_max > right
        if on_hv_side and on_lv_side:
            crossings.append({
                "net": z.GetNetname(),
                "layer": board.GetLayerName(z.GetLayer()),
                "fill_x_min_mm": round(zx_min, 3),
                "fill_x_max_mm": round(zx_max, 3),
                "slot_left_mm": round(slot["left"], 3),
                "slot_right_mm": round(slot["right"], 3),
            })

    verdict = "fail" if crossings else "pass"
    out = {
        "ok": True,
        "slot_detected": True,
        "slot": slot,
        "zones_checked": zones_checked,
        "crossings": crossings,
        "verdict": verdict,
    }
    if crossings:
        out["error"] = (f"{len(crossings)} copper zone(s) bridge the isolation "
                        f"barrier (filled copper on both sides of slot "
                        f"{slot['left']:.2f}–{slot['right']:.2f} mm)")
    return out


def mode_resolve_pad_conflicts(spec):
    """Standalone pad-pad clearance sweep — exposed so pipeline can re-run it
    after kicad-tools / slot-clearance phases that may have moved footprints.
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    min_clearance_mm = float(spec.get("pad_min_clearance_mm", 0.4))

    board = pcbnew.LoadBoard(pcb_path)
    moves = _resolve_pad_conflicts(board, min_clearance_mm)
    if moves:
        if not pcbnew.SaveBoard(output_pcb, board):
            return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}
    return {
        "ok": True,
        "output_pcb": output_pcb,
        "moves": moves,
        "moves_count": len(moves),
    }


def mode_ensure_pads_in_board(spec):
    """Sweep every footprint; for any whose pads / body bbox extend past the
    Edge.Cuts outline, slide the footprint inward so it sits at least
    `margin_mm` inside the board. Run AFTER apply_layout but BEFORE GND zones.
    Catches layout corner cases where region-grid placement put a body
    center too close to the region edge.

    Why this exists: Phase C layout's fp_size is pad-bbox + padding. KiCad's
    actual courtyard / pad position can extend beyond that bbox for SIP-4
    vertical, SOIC wide, terminal blocks etc. mode_apply_layout nudges body
    bbox center to target, but if layout placed center too close to the
    edge, body still spills off-board.
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    margin_mm = float(spec.get("margin_mm", 0.6))

    board = pcbnew.LoadBoard(pcb_path)

    # Detect Edge.Cuts outline bounds
    edge_layer = board.GetLayerID("Edge.Cuts")
    xs, ys = [], []
    for d in list(board.GetDrawings()):
        if d.GetLayer() != edge_layer:
            continue
        s = d.GetStart()
        e = d.GetEnd()
        xs.extend([pcbnew.ToMM(s.x), pcbnew.ToMM(e.x)])
        ys.extend([pcbnew.ToMM(s.y), pcbnew.ToMM(e.y)])
    if not xs:
        return {"ok": True, "skipped": True, "reason": "no Edge.Cuts found"}
    bx_min, bx_max = min(xs) + margin_mm, max(xs) - margin_mm
    by_min, by_max = min(ys) + margin_mm, max(ys) - margin_mm

    moved = []
    for fp in list(board.GetFootprints()):
        ref = fp.GetReference()
        # Build extent purely from pads (KiCad 9 fp.GetBoundingBox sometimes
        # returns body-only bbox that misses pads on SIP-4 vertical / through-
        # hole footprints — measured experimentally on TMA0505 SIP-4 where
        # the bbox excluded pin 5 / 6 and ensure_pads_in_board missed real
        # pad-OOB cases).
        pads = list(fp.Pads())
        if not pads:
            continue
        bbx_min = bby_min = float("inf")
        bbx_max = bby_max = float("-inf")
        for pad in pads:
            pp = pad.GetPosition()
            psize = pad.GetSize()
            px = pcbnew.ToMM(pp.x)
            py = pcbnew.ToMM(pp.y)
            pw = pcbnew.ToMM(psize.x) / 2
            ph = pcbnew.ToMM(psize.y) / 2
            bbx_min = min(bbx_min, px - pw)
            bby_min = min(bby_min, py - ph)
            bbx_max = max(bbx_max, px + pw)
            bby_max = max(bby_max, py + ph)

        dx_mm = 0.0
        dy_mm = 0.0
        if bbx_min < bx_min:
            dx_mm = bx_min - bbx_min
        elif bbx_max > bx_max:
            dx_mm = bx_max - bbx_max
        if bby_min < by_min:
            dy_mm = by_min - bby_min
        elif bby_max > by_max:
            dy_mm = by_max - bby_max
        if abs(dx_mm) > 0.01 or abs(dy_mm) > 0.01:
            fp.Move(pcbnew.VECTOR2I_MM(dx_mm, dy_mm))
            moved.append({"ref": ref,
                          "dx_mm": round(dx_mm, 3),
                          "dy_mm": round(dy_mm, 3)})

    if moved:
        if not pcbnew.SaveBoard(output_pcb, board):
            return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    return {
        "ok": True,
        "moved_count": len(moved),
        "moved": moved,
        "board_bounds_mm": [bx_min, by_min, bx_max, by_max],
    }


def mode_move_footprints(spec):
    """Move / rotate specific footprints to absolute targets — the atomic
    action for AI-driven placement. Target (x, y) is where the footprint's
    body bounding-box CENTER should land (matches get_geometry's courtyard
    centre and how the AI sees the board). Touches nothing else — no
    Edge.Cuts, no tracks, no zones — so it is safe to call thousands of times
    inside a placement loop.

    spec["moves"]: {ref: [x, y, rot]} — rot optional / null keeps current.
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    moves = spec["moves"]

    board = pcbnew.LoadBoard(pcb_path)
    moved = []
    for fp in list(board.GetFootprints()):
        ref = fp.GetReference()
        if ref not in moves:
            continue
        t = moves[ref]
        x, y = float(t[0]), float(t[1])
        rot = t[2] if len(t) > 2 and t[2] is not None else None
        if rot is not None:
            fp.SetOrientationDegrees(float(rot))
        target = pcbnew.VECTOR2I_MM(x, y)
        fp.SetPosition(target)
        # Anchor != body centre for some footprints — nudge so the body
        # bbox centre lands on the target the AI asked for.
        bbox = fp.GetBoundingBox(False, False)  # exclude text
        center = bbox.GetCenter()
        dx, dy = target.x - center.x, target.y - center.y
        if dx or dy:
            fp.Move(pcbnew.VECTOR2I(int(dx), int(dy)))
        moved.append(ref)

    not_found = [r for r in moves if r not in moved]
    if moved and not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}
    return {"ok": True, "moved": moved, "moved_count": len(moved),
            "not_found": not_found}


def mode_bridge_slots(spec):
    """Re-draw the isolation slot(s) leaving a solid PCB BRIDGE under every
    barrier device that straddles the slot. A through-hole barrier device
    (e.g. a SIP isolated DC-DC) has pad pitch < slot width, so a continuous
    milled slot cuts its pads — but the device itself IS the isolation
    barrier there, so a bridge under its body is correct (and for a body
    wider than the creepage requirement the bridge still satisfies it).
    Run in Phase D, after placement has converged.

    spec: {pcb_path, output_pcb, barrier_refs:[...], bridge_margin_mm,
           min_segment_mm}
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    barrier_refs = set(spec.get("barrier_refs", []))
    margin = float(spec.get("bridge_margin_mm", 1.0))
    min_seg = float(spec.get("min_segment_mm", 2.0))

    board = pcbnew.LoadBoard(pcb_path)
    edge_layer = board.GetLayerID("Edge.Cuts")

    edge_segs = []
    for d in list(board.GetDrawings()):
        if (d.GetLayer() == edge_layer
                and d.GetShape() == pcbnew.SHAPE_T_SEGMENT):
            s, e = d.GetStart(), d.GetEnd()
            edge_segs.append((d, pcbnew.ToMM(s.x), pcbnew.ToMM(s.y),
                              pcbnew.ToMM(e.x), pcbnew.ToMM(e.y)))
    if not edge_segs:
        return {"ok": False, "error": "no Edge.Cuts segments"}

    xs = [v for _, sx, sy, ex, ey in edge_segs for v in (sx, ex)]
    ys = [v for _, sx, sy, ex, ey in edge_segs for v in (sy, ey)]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    EPS = 0.05

    def on_perimeter(sx, sy, ex, ey):
        return ((abs(sx - bx0) < EPS and abs(ex - bx0) < EPS)
                or (abs(sx - bx1) < EPS and abs(ex - bx1) < EPS)
                or (abs(sy - by0) < EPS and abs(ey - by0) < EPS)
                or (abs(sy - by1) < EPS and abs(ey - by1) < EPS))

    internal = [t for t in edge_segs
                if not on_perimeter(t[1], t[2], t[3], t[4])]
    if not internal:
        return {"ok": True, "skipped": True, "reason": "no internal slot"}

    ixs = [v for _, sx, sy, ex, ey in internal for v in (sx, ex)]
    iys = [v for _, sx, sy, ex, ey in internal for v in (sy, ey)]
    sx1, sx2 = min(ixs), max(ixs)
    sy1, sy2 = min(iys), max(iys)

    # barrier devices straddling the slot → bridge under their body
    bridges = []
    for fp in list(board.GetFootprints()):
        if fp.GetReference() not in barrier_refs:
            continue
        bb = fp.GetBoundingBox(False, False)
        fx0 = pcbnew.ToMM(bb.GetX())
        fx1 = fx0 + pcbnew.ToMM(bb.GetWidth())
        fy0 = pcbnew.ToMM(bb.GetY())
        fy1 = fy0 + pcbnew.ToMM(bb.GetHeight())
        if fx0 <= sx1 + EPS and fx1 >= sx2 - EPS:  # spans the slot
            bridges.append((fy0 - margin, fy1 + margin, fp.GetReference()))

    for d, _, _, _, _ in internal:
        board.Remove(d)

    def add_edge_line(a, b, c, e):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(edge_layer)
        seg.SetStart(pcbnew.VECTOR2I_MM(float(a), float(b)))
        seg.SetEnd(pcbnew.VECTOR2I_MM(float(c), float(e)))
        seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)

    # split [sy1,sy2] into segments around the bridge y-ranges
    cuts = sorted((max(lo, sy1), min(hi, sy2)) for lo, hi, _ in bridges)
    segments, cur = [], sy1
    for lo, hi in cuts:
        if lo > cur:
            segments.append((cur, lo))
        cur = max(cur, hi)
    if cur < sy2:
        segments.append((cur, sy2))

    drawn = 0
    for y_lo, y_hi in segments:
        if y_hi - y_lo >= min_seg:
            add_edge_line(sx1, y_lo, sx2, y_lo)
            add_edge_line(sx2, y_lo, sx2, y_hi)
            add_edge_line(sx2, y_hi, sx1, y_hi)
            add_edge_line(sx1, y_hi, sx1, y_lo)
            drawn += 1

    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}
    return {
        "ok": True,
        "slot_x_mm": [round(sx1, 2), round(sx2, 2)],
        "bridges": [{"ref": r, "y_mm": [round(lo, 2), round(hi, 2)]}
                    for lo, hi, r in bridges],
        "slot_segments_drawn": drawn,
    }


def mode_refit_board(spec):
    """Shrink the Edge.Cuts outline to hug the actual placement.

    The board outline is sized once at init_layout from CLAUDE.md
    pack_density. After the agentic loop compacts the placement, that
    outline is stale (too loose). This mode recomputes the outer
    rectangle from the real footprint extent + margin, and redraws the
    isolation slot continuous (3mm top/bottom bridges) at its existing
    x — run bridge_slots afterwards to carve device bridges. Run before
    add_zones (zones are sized from Edge.Cuts).

    With keep_outline the outline is left exactly as found and this becomes a
    measurement only: fill_ratio against the given board, nothing written. That
    is the right mode whenever an enclosure or customer spec owns the outline.

    spec: {pcb_path, output_pcb, margin_mm, keep_outline}
    Returns board rect + fill_ratio (courtyard area / board area) so the
    caller can judge compactness.
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    margin = float(spec.get("margin_mm", 2.5))
    keep_outline = bool(spec.get("keep_outline", False))

    board = pcbnew.LoadBoard(pcb_path)
    edge_layer = board.GetLayerID("Edge.Cuts")

    # Detect the existing slot x-range before removing Edge.Cuts.
    slot = _detect_slot_from_edge_cuts(board)

    # Real placement extent — footprint bbox excludes text, includes
    # pads + courtyard graphics.
    fxs, fys, fp_area = [], [], 0.0
    for fp in list(board.GetFootprints()):
        bb = fp.GetBoundingBox(False, False)
        x0 = pcbnew.ToMM(bb.GetX())
        y0 = pcbnew.ToMM(bb.GetY())
        w = pcbnew.ToMM(bb.GetWidth())
        h = pcbnew.ToMM(bb.GetHeight())
        fxs.extend([x0, x0 + w])
        fys.extend([y0, y0 + h])
        fp_area += w * h
    if not fxs:
        return {"ok": False, "error": "no footprints to fit"}

    if keep_outline:
        # Measure the outline from Edge.Cuts endpoints, not
        # GetBoardEdgesBoundingBox — that one includes the stroke width, which
        # inflates board area and quietly deflates fill_ratio right where the
        # metric is compared against a threshold.
        exs, eys = [], []
        for d in list(board.GetDrawings()):
            if d.GetLayer() != edge_layer:
                continue
            for pt in (d.GetStart(), d.GetEnd()):
                exs.append(pcbnew.ToMM(pt.x))
                eys.append(pcbnew.ToMM(pt.y))
        if len(exs) < 2:
            return {"ok": False,
                    "error": "keep_outline: board has no Edge.Cuts to measure"}
        bx, by = min(exs), min(eys)
        bw, bh = max(exs) - bx, max(eys) - by
        if bw <= 0 or bh <= 0:
            return {"ok": False,
                    "error": "keep_outline: Edge.Cuts encloses no area"}
        area = bw * bh
        return {
            "ok": True,
            "outline_kept": True,
            "board": {"x": round(bx, 2), "y": round(by, 2),
                      "w": round(bw, 2), "h": round(bh, 2)},
            "fill_ratio": round(fp_area / area, 3) if area else None,
            "placement_extent": {"w": round(max(fxs) - min(fxs), 2),
                                 "h": round(max(fys) - min(fys), 2)},
            "wrote_nothing": True,
        }

    x1, y1 = min(fxs) - margin, min(fys) - margin
    x2, y2 = max(fxs) + margin, max(fys) + margin

    for d in [d for d in list(board.GetDrawings())
              if d.GetLayer() == edge_layer]:
        board.Remove(d)

    def add_edge_line(sx, sy, ex, ey):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(edge_layer)
        seg.SetStart(pcbnew.VECTOR2I_MM(float(sx), float(sy)))
        seg.SetEnd(pcbnew.VECTOR2I_MM(float(ex), float(ey)))
        seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)

    add_edge_line(x1, y1, x2, y1)
    add_edge_line(x2, y1, x2, y2)
    add_edge_line(x2, y2, x1, y2)
    add_edge_line(x1, y2, x1, y1)

    bridge = 3.0
    slot_drawn = 0
    slot_x = None
    if slot and x1 + 2 < slot["left"] < slot["right"] < x2 - 2:
        sx1, sx2 = slot["left"], slot["right"]
        add_edge_line(sx1, y1 + bridge, sx2, y1 + bridge)
        add_edge_line(sx2, y1 + bridge, sx2, y2 - bridge)
        add_edge_line(sx2, y2 - bridge, sx1, y2 - bridge)
        add_edge_line(sx1, y2 - bridge, sx1, y1 + bridge)
        slot_drawn = 4
        slot_x = round((sx1 + sx2) / 2.0, 2)

    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    board_area = (x2 - x1) * (y2 - y1)
    return {
        "ok": True,
        "board": {"x": round(x1, 2), "y": round(y1, 2),
                  "w": round(x2 - x1, 2), "h": round(y2 - y1, 2)},
        "slot_x_mm": slot_x,
        "slot_segments_drawn": slot_drawn,
        "fill_ratio": round(fp_area / board_area, 3) if board_area else None,
    }


def mode_stitch_zones(spec):
    """Tie one net's pours on two layers together with vias.

    Why: on a 2-layer board every track on the back side slices that pour into
    islands, so the return current under a signal has to keep finding a way
    across, and a big thermal pad on the front has nothing carrying heat or
    return current down. Both are the same missing thing — vias where BOTH
    pours are actually filled.

    A candidate is kept only if it lands inside both FILLED polygons after each
    is shrunk by (via radius + clearance), so the via can never touch foreign
    copper: the fill already keeps its own distance from everything not on this
    net. Copper is not the only rule though — a plated through-hole pad sits
    inside the pour, so the copper test says yes right on top of its drill.
    Holes are therefore checked separately against hole-to-hole spacing;
    judging copper alone lands you in `hole_to_hole` violations.

    ONE net per call, named explicitly. Auto-matching every GND-like net would
    happily stitch across an isolation barrier and destroy the very separation
    the barrier exists for.

    spec: {pcb_path, output_pcb, net, layers[2], pitch_mm, via_dia_mm,
           drill_mm, clearance_mm, min_sep_mm, hole_to_hole_mm,
           thermal_pad_min_mm2}
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    net_name = spec.get("net", "GND")
    layer_names = spec.get("layers") or ["F.Cu", "B.Cu"]
    if len(layer_names) != 2:
        return {"ok": False, "error": "stitching needs exactly two layers"}
    pitch = float(spec.get("pitch_mm", 5.0))
    via_dia = float(spec.get("via_dia_mm", 0.6))
    drill = float(spec.get("drill_mm", 0.3))
    clearance = float(spec.get("clearance_mm", 0.2))
    min_sep = float(spec.get("min_sep_mm", 2.0))
    hole_to_hole = float(spec.get("hole_to_hole_mm", 0.5))
    thermal_min = float(spec.get("thermal_pad_min_mm2", 0.0))
    if pitch <= 0:
        return {"ok": False, "error": "pitch_mm must be > 0"}

    board = pcbnew.LoadBoard(pcb_path)
    lay_ids = []
    for nm in layer_names:
        lid = board.GetLayerID(nm)
        if lid < 0:
            return {"ok": False, "error": f"unknown layer: {nm}"}
        lay_ids.append(lid)

    # Fill first: the whole test is "is there copper here", and an unfilled
    # zone reports none.
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception as e:
        return {"ok": False, "error": f"zone fill failed: {e}"}

    net_code = None
    polys = {}
    for z in board.Zones():
        if str(z.GetNetname()) != net_name:
            continue
        net_code = z.GetNetCode()
        for lid in lay_ids:
            if not z.IsOnLayer(lid):
                continue
            # Copy-construct: .Clone() returns a bare SHAPE with no Deflate /
            # BooleanAdd / Contains on it.
            polys.setdefault(lid, []).append(
                pcbnew.SHAPE_POLY_SET(z.GetFilledPolysList(lid)))
    if net_code is None:
        return {"ok": False,
                "error": f"no zone on net {net_name!r} — pour it with "
                         f"add_zones first"}
    missing = [nm for nm, lid in zip(layer_names, lay_ids) if lid not in polys]
    if missing:
        return {"ok": False,
                "error": f"net {net_name!r} has no filled pour on {missing} — "
                         f"nothing to stitch to"}

    shrink = pcbnew.FromMM(via_dia / 2 + clearance)
    safe = {}
    for lid, lst in polys.items():
        acc = pcbnew.SHAPE_POLY_SET()
        for ps in lst:
            ps.Deflate(shrink, pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                       pcbnew.FromMM(0.01))
            acc.BooleanAdd(ps)
        safe[lid] = acc

    existing = [t.GetPosition() for t in list(board.GetTracks())
                if t.Type() == pcbnew.PCB_VIA_T]
    sep = pcbnew.FromMM(min_sep)

    holes = []
    for fp in list(board.GetFootprints()):
        for pad in fp.Pads():
            dr = pad.GetDrillSizeX()
            if dr > 0:
                holes.append((pad.GetPosition(), dr // 2))
    hole_margin = pcbnew.FromMM(drill / 2 + hole_to_hole)

    def place(pt):
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pt)
        v.SetWidth(pcbnew.FromMM(via_dia))
        v.SetDrill(pcbnew.FromMM(drill))
        v.SetNetCode(net_code)
        v.SetLayerPair(lay_ids[0], lay_ids[1])
        board.Add(v)
        existing.append(pt)

    def blocked(pt, need_both=True):
        lids = lay_ids if need_both else lay_ids[1:]
        if not all(safe[l].Contains(pt) for l in lids):
            return "no_copper"
        if any(abs(q.x - pt.x) < sep and abs(q.y - pt.y) < sep
               for q in existing):
            return "too_close_to_via"
        if any((q.x - pt.x) ** 2 + (q.y - pt.y) ** 2 < (r + hole_margin) ** 2
               for q, r in holes):
            return "too_close_to_hole"
        return None

    rejected = {"no_copper": 0, "too_close_to_via": 0, "too_close_to_hole": 0}
    box = board.GetBoardEdgesBoundingBox()
    step = pcbnew.FromMM(pitch)
    added = 0
    y = box.GetTop() + step // 2
    while y < box.GetBottom():
        x = box.GetLeft() + step // 2
        while x < box.GetRight():
            pt = pcbnew.VECTOR2I(int(x), int(y))
            x += step
            why = blocked(pt)
            if why:
                rejected[why] += 1
                continue
            place(pt)
            added += 1
        y += step

    thermal_added = 0
    if thermal_min > 0:
        inset = pcbnew.FromMM(via_dia / 2 + 0.15)
        grid = pcbnew.FromMM(1.2)
        for fp in list(board.GetFootprints()):
            for pad in fp.Pads():
                # SMD only: a pad that already has a hole is its own via.
                if pad.GetNetCode() != net_code or pad.GetDrillSizeX() > 0:
                    continue
                w, h = pad.GetSizeX(), pad.GetSizeY()
                if pcbnew.ToMM(w) * pcbnew.ToMM(h) < thermal_min:
                    continue
                c = pad.GetPosition()
                nx = max(1, int((w - 2 * inset) // grid))
                ny = max(1, int((h - 2 * inset) // grid))
                for i in range(nx):
                    for j in range(ny):
                        px = c.x - (w // 2 - inset) + (
                            0 if nx == 1 else i * (w - 2 * inset) // (nx - 1))
                        py = c.y - (h // 2 - inset) + (
                            0 if ny == 1 else j * (h - 2 * inset) // (ny - 1))
                        pt = pcbnew.VECTOR2I(int(px), int(py))
                        # The pad IS the near-side copper, so only the far side
                        # needs a pour to land on.
                        why = blocked(pt, need_both=False)
                        if why:
                            rejected[why] += 1
                            continue
                        place(pt)
                        thermal_added += 1

    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception as e:
        return {"ok": False, "error": f"re-fill after stitching failed: {e}"}
    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    return {
        "ok": True,
        "output_pcb": output_pcb,
        "net": net_name,
        "layers": layer_names,
        "stitch_vias_added": added,
        "thermal_pad_vias_added": thermal_added,
        "candidates_rejected": rejected,
        "pitch_mm": pitch,
        "next": "run_drc — stitching adds holes, so hole_to_hole and "
                "annular_width are the checks that matter now",
    }


def mode_set_silk_spec(spec):
    """Force silkscreen text to a fab's character spec.

    Fabs state a minimum silk line width and character height (below it the
    screen print smears or drops out), and KiCad's per-footprint defaults come
    from whatever library each part came from — so a board mixes several sizes
    until something normalises them.

    Reference designators are put on the silk layer of the side the part is on.
    Values are left alone by default: they usually carry an MPN, and printing
    that on the board buys nothing while crowding out the refdes.

    Two consequences worth knowing before running this:
      * editing footprint text makes the board differ from its library, so DRC
        reports `lib_footprint_issues` for every part touched. That finding is
        expected here and should be triaged as such, not chased.
      * bigger text overlaps more. Expect `silk_overlap` / `silk_over_copper`
        to rise; that is a real readability problem to fix by moving text, not
        a reason to skip the spec.

    spec: {pcb_path, output_pcb, height_mm, thickness_mm, include_values,
           hide_values}
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    height = spec.get("height_mm")
    thickness = spec.get("thickness_mm")
    include_values = bool(spec.get("include_values", False))
    hide_values = bool(spec.get("hide_values", False))
    if height is None and thickness is None and not hide_values:
        return {"ok": False,
                "error": "nothing to set — give height_mm and/or thickness_mm"}

    board = pcbnew.LoadBoard(pcb_path)
    h_iu = pcbnew.FromMM(float(height)) if height is not None else None
    t_iu = pcbnew.FromMM(float(thickness)) if thickness is not None else None

    def apply(item):
        if h_iu is not None:
            item.SetTextSize(pcbnew.VECTOR2I(h_iu, h_iu))
        if t_iu is not None:
            item.SetTextThickness(t_iu)

    refs = values = board_texts = 0
    for fp in list(board.GetFootprints()):
        back = fp.GetLayer() != pcbnew.F_Cu
        ref = fp.Reference()
        ref.SetLayer(pcbnew.B_SilkS if back else pcbnew.F_SilkS)
        apply(ref)
        refs += 1
        val = fp.Value()
        if hide_values:
            val.SetVisible(False)
            values += 1
        elif include_values:
            apply(val)
            values += 1

    for d in list(board.GetDrawings()):
        if isinstance(d, pcbnew.PCB_TEXT) and d.GetLayer() in (pcbnew.F_SilkS,
                                                               pcbnew.B_SilkS):
            apply(d)
            board_texts += 1

    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    return {
        "ok": True,
        "output_pcb": output_pcb,
        "height_mm": height,
        "thickness_mm": thickness,
        "references_set": refs,
        "values_touched": values,
        "board_texts_set": board_texts,
        "expect": "lib_footprint_issues for every part touched (board now "
                  "differs from library — triage, do not chase), and more "
                  "silk_overlap as text grows",
    }


# =============================================================
# Mode: place_silk_refs — greedy reference-designator placement
# =============================================================
# Cost weights, highest first. Off-board is fatal (the print gets clipped);
# ink on an exposed pad degrades the solder joint; designators on top of each
# other are unreadable; a designator on another part's outline is exactly what
# silk_overlap reports; a body underneath is merely cosmetic.
_SILK_W_PAD = 50.0
_SILK_W_REF = 10.0
_SILK_W_SILK = 8.0
_SILK_W_BODY = 1.0
_SILK_W_OFFBOARD = 1e6


def silk_rect_overlap(a, b):
    """Overlap area (mm²) of two (x0, y0, x1, y1) rects."""
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, ox) * max(0.0, oy)


def silk_candidates(body, tw, th, gaps, allow_rotate):
    """Candidate (angle, w, h, cx, cy) spots for a tw×th designator around a
    body rect: 8 compass directions × each gap, horizontal and (optionally)
    rotated 90° — the rotated form fits narrow gaps between parts — plus dead
    centre when the body dwarfs the text (>3× in both axes)."""
    cx, cy = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
    hw, hh = (body[2] - body[0]) / 2, (body[3] - body[1]) / 2
    forms = [(0.0, tw, th)] + ([(90.0, th, tw)] if allow_rotate else [])
    out = []
    for ang, w, h in forms:
        for gap in gaps:
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0),
                           (-1, -1), (1, -1), (-1, 1), (1, 1)):
                out.append((ang, w, h,
                            cx + dx * (hw + w / 2 + gap),
                            cy + dy * (hh + h / 2 + gap)))
        if hw * 2 > 3 * w and hh * 2 > 3 * h:
            out.append((ang, w, h, cx, cy))
    return out


def silk_edge_keepouts(er, margin):
    """Keepout rects for one Edge.Cuts drawing bbox, inflated by margin.
    A line-like bbox (straight segment) becomes one band. A large bbox — a
    closed outline drawn as a single gr_rect or circle — contributes its four
    perimeter bands instead: inflating the whole bbox would blanket the board
    interior and mark every candidate fatal."""
    x0, y0, x1, y1 = er
    if min(x1 - x0, y1 - y0) <= 2 * margin:
        return [(x0 - margin, y0 - margin, x1 + margin, y1 + margin)]
    return [
        (x0 - margin, y0 - margin, x1 + margin, y0 + margin),
        (x0 - margin, y1 - margin, x1 + margin, y1 + margin),
        (x0 - margin, y0 - margin, x0 + margin, y1 + margin),
        (x1 - margin, y0 - margin, x1 + margin, y1 + margin),
    ]


def silk_cost(r, dist, w_dist, board_rect, edges, pads, silks, refs, bodies,
              own_idx):
    """Score rect r against the board: weighted overlap with every obstacle
    class plus a distance term. `edges` are Edge.Cuts keepout rects (each
    drawing's bbox inflated by the edge margin) — they catch internal slots
    and cutouts, which the outer board_rect test cannot see. refs[own_idx]
    must already be None so the designator never scores against its own
    previous spot; its own body counts triple so centring on the part stays
    a last resort."""
    c = dist * w_dist
    if (r[0] < board_rect[0] or r[1] < board_rect[1]
            or r[2] > board_rect[2] or r[3] > board_rect[3]):
        c += _SILK_W_OFFBOARD
    for eb in edges:
        c += silk_rect_overlap(r, eb) * _SILK_W_OFFBOARD
    for pb in pads:
        c += silk_rect_overlap(r, pb) * _SILK_W_PAD
    for sb in silks:
        c += silk_rect_overlap(r, sb) * _SILK_W_SILK
    for j, rb in enumerate(refs):
        if rb is not None and j != own_idx:
            c += silk_rect_overlap(r, rb) * _SILK_W_REF
    for j, ob in enumerate(bodies):
        c += silk_rect_overlap(r, ob) * (_SILK_W_BODY * 3.0 if j == own_idx
                                         else _SILK_W_BODY)
    return c


def mode_place_silk_refs(spec):
    """Greedy placement of every visible reference designator.

    set_silk_spec decides how BIG silk text is; nothing else in the toolbox
    decides WHERE it sits, and fab-spec text on a dense board lands on pads
    and neighbours. Each designator tries the silk_candidates ring around its
    body and keeps the cheapest spot by silk_cost. Several passes, because a
    designator placed early cannot see the ones placed after it — later
    passes re-optimise against the settled layout.

    spec: {pcb_path, output_pcb, passes, gaps_mm, w_dist, allow_rotate,
           edge_margin_mm}
    """
    pcb_path = spec["pcb_path"]
    output_pcb = spec.get("output_pcb", pcb_path)
    passes = int(spec.get("passes") or 4)
    gaps = [float(g) for g in
            (spec.get("gaps_mm") or (0.15, 0.3, 0.5, 0.8, 1.2))]
    w_dist = float(spec.get("w_dist") or 3.0)
    allow_rotate = bool(spec.get("allow_rotate", True))
    # Text that merely stays inside Edge.Cuts can still sit closer to the
    # edge than the fab's silk-to-edge clearance, so DRC trades silk_overlap
    # for silk_edge_clearance. Shrink the allowed area by this margin.
    edge_margin = float(spec.get("edge_margin_mm", 0.5))

    board = pcbnew.LoadBoard(pcb_path)

    def rect(bb):
        return (pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetTop()),
                pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))

    board_rect = rect(board.GetBoardEdgesBoundingBox())
    board_rect = (board_rect[0] + edge_margin, board_rect[1] + edge_margin,
                  board_rect[2] - edge_margin, board_rect[3] - edge_margin)
    if (board_rect[2] - board_rect[0] <= 0
            or board_rect[3] - board_rect[1] <= 0):
        return {"ok": False,
                "error": "board has no Edge.Cuts extent — every candidate "
                         "would score off-board. Draw the outline first"}

    # Every Edge.Cuts drawing, inflated by the margin, is a keepout — this is
    # what catches internal slots / cutouts sitting far from the outer edge.
    edges = []
    for d in board.GetDrawings():
        if board.GetLayerName(d.GetLayer()) == "Edge.Cuts":
            edges.extend(silk_edge_keepouts(rect(d.GetBoundingBox()),
                                            edge_margin))

    fps = list(board.GetFootprints())
    bodies = [rect(f.GetBoundingBox(False, False)) for f in fps]
    pads = [rect(p.GetBoundingBox()) for f in fps for p in f.Pads()]
    silks = []
    for f in fps:
        for g in f.GraphicalItems():
            if board.GetLayerName(g.GetLayer()) in ("F.Silkscreen",
                                                    "B.Silkscreen"):
                silks.append(rect(g.GetBoundingBox()))
    refs = [rect(f.Reference().GetBoundingBox())
            if f.Reference().IsVisible() else None for f in fps]

    for _ in range(passes):
        for i, fp in enumerate(fps):
            ref = fp.Reference()
            if not ref.IsVisible():
                continue
            # Measure the text horizontal — its box is what gets packed.
            ref.SetTextAngle(pcbnew.EDA_ANGLE(0, pcbnew.DEGREES_T))
            tb = rect(ref.GetBoundingBox())
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
            body = bodies[i]
            cx, cy = (body[0] + body[2]) / 2, (body[1] + body[3]) / 2
            hw, hh = (body[2] - body[0]) / 2, (body[3] - body[1]) / 2
            refs[i] = None
            best, best_cost = None, None
            for ang, w, h, px, py in silk_candidates(body, tw, th, gaps,
                                                     allow_rotate):
                r = (px - w / 2, py - h / 2, px + w / 2, py + h / 2)
                dist = max(0.0, max(abs(px - cx) - hw, abs(py - cy) - hh))
                c = silk_cost(r, dist, w_dist, board_rect, edges, pads,
                              silks, refs, bodies, i)
                if best_cost is None or c < best_cost:
                    best_cost, best = c, (ang, px, py, r)
            ang, px, py, r = best
            ref.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(px),
                                            pcbnew.FromMM(py)))
            ref.SetTextAngle(pcbnew.EDA_ANGLE(ang, pcbnew.DEGREES_T))
            refs[i] = r

    if not pcbnew.SaveBoard(output_pcb, board):
        return {"ok": False, "error": f"SaveBoard failed: {output_pcb}"}

    return {
        "ok": True,
        "output_pcb": output_pcb,
        "refs_placed": sum(1 for r in refs if r is not None),
        "hidden_skipped": sum(1 for r in refs if r is None),
        "passes": passes,
        "gaps_mm": gaps,
        "next": "re-run run_drc and DIFF silk_overlap / silk_over_copper "
                "against the pre-placement counts — the greedy cost "
                "minimises overlap, it cannot guarantee zero on a dense "
                "board. Then render for an eyeball pass",
    }


MODES = {
    "create_pcb": mode_create_pcb,
    "stitch_zones": mode_stitch_zones,
    "set_silk_spec": mode_set_silk_spec,
    "place_silk_refs": mode_place_silk_refs,
    "apply_layout": mode_apply_layout,
    "classify_fps": mode_classify_fps,
    "check_slot_clearance": mode_check_slot_clearance,
    "ensure_pads_in_board": mode_ensure_pads_in_board,
    "add_ground_zones": mode_add_ground_zones,
    "validate_zones": mode_validate_zones,
    "resolve_pad_conflicts": mode_resolve_pad_conflicts,
    "move_footprints": mode_move_footprints,
    "bridge_slots": mode_bridge_slots,
    "refit_board": mode_refit_board,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    spec = json.loads(Path(args.input).read_text(encoding="utf-8"))
    mode = spec.get("mode")
    if mode not in MODES:
        print(json.dumps({"ok": False, "error": f"unknown mode: {mode}"}))
        sys.exit(1)
    try:
        result = MODES[mode](spec)
    except Exception as e:
        import traceback
        result = {"ok": False, "error": f"helper exception: {e}",
                  "traceback": traceback.format_exc()[:2000]}
    # Print JSON to BOTH stdout and stderr — wx subsystem on macOS occasionally
    # eats stdout in subprocess context (multiple LoadBoard / SaveBoard) and
    # the caller sees empty stdout. The caller searches both streams.
    payload = json.dumps(result, ensure_ascii=False)
    print(payload)
    sys.stdout.flush()
    sys.stderr.write(payload + "\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
