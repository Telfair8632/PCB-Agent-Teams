#!/usr/bin/env python3
"""draw-pcb toolbox helper: connector / switch identification.

Shared by `placement_brief` (edge_devices) and `check_connector_access` so the
two never disagree about what counts as an interface part.

Why not refdes prefixes alone: a refdes prefix is a *drafting convention*, not
a fact about the part. Boards ported from other EDA tools routinely use CN/DC/
TF/P where the KiCad convention would say J, and a prefix-only rule silently
drops those parts out of every edge-related check — the part is then placed
mid-board and nobody is told. The footprint library id names the physical part
and is the strongest available signal.

Why TOKEN matching and not substring: measured against KiCad's own 7.7k
footprints, raw substring matching is catastrophic in both directions.
`tact` ⊂ `PhoenixContact` swallowed 583 of 709 Phoenix footprints into the
"switch" class — which is exempt from the access gate — so the pluggable HV
terminal blocks an isolation board is built around were silently never
checked. In the other direction `usb` ⊂ `SML-LX0404SIUPGUSB` made an LED a
connector, `ffc` ⊂ `Offcenter` made a WLCSP one, and `socket` pulled in every
DIP socket. Both failure modes come from matching inside words, so the fix is
to match whole tokens.

Classification (a part is an interface part if ANY fires):
  1. footprint library id contains a connector / switch keyword TOKEN (strong)
  2. refdes prefix matches AND the body sticks out past the pads   (weak+geom)
  3. caller passed it in `include`                                 (explicit)
…unless a veto fires first (library family or explicit never-list).
"""

# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import re

# Library families that are never interface parts, whatever their part name
# says. Checked against the text before ':' in the library id.
_VETO_LIB_PREFIX = re.compile(
    r"^(package|resistor|capacitor|inductor|diode|led|crystal|oscillator|"
    r"fuse|ferrite|varistor|relay|transformer|filter|rf_|sensor|module|"
    r"mountinghole|testpoint|fiducial|nettie|net_tie|symbol|logo|graphic)",
    re.I)

# Part names that are never interface parts even inside a connector library.
_VETO_TOKENS = frozenset((
    "testpoint", "mountinghole", "fiducial", "nettie", "solderjumper",
    "dipsocket", "smdsocket", "icsocket",
))

_CONNECTOR_KEYWORDS = (
    "conn", "connector", "usb", "typec", "type", "header", "pinheader",
    "boxheader", "jack", "barreljack", "socket", "receptacle",
    "terminal", "terminalblock", "screwterminal", "rj45", "rj11", "modular",
    "sdcard", "microsd", "tfcard", "cardslot", "card", "fpc", "ffc", "zif",
    "jst", "molex", "phoenix", "wago", "harwin", "hirose", "amass",
    "amphenol", "samtec", "te", "jae", "switchcraft", "neutrik", "wurth",
    "barrel", "banana", "idc", "microfit", "micromatch", "milligrid",
    "dsub", "hdmi", "displayport", "audiojack", "batteryholder", "battery",
    "coincell", "sim", "cardedge", "edgeconnector", "tagconnect", "din",
    "xlr", "minixlr", "mezzanine", "boardtoboard", "ffcfpc",
)

_SWITCH_KEYWORDS = (
    "sw", "switch", "pushbutton", "button", "tact", "keyswitch", "keypad",
    "spst", "spdt", "dpdt", "dipswitch", "rotary", "encoder", "slideswitch",
    "tactile",
)

# Weak signal — must be confirmed by body-past-pad geometry (see docstring).
_CONNECTOR_REF_PREFIX = re.compile(
    r"^(J|CN|CON|P|X|DC|TF|TB|USB|HDR|SIM|RJ|MOD|BT|ANT)\d", re.I)
_SWITCH_REF_PREFIX = re.compile(r"^(SW|S|BTN|KEY|ENC)\d", re.I)

# Body must exceed the pad bbox by at least this much on some side for the
# weak refdes signal to be believed (mm).
GEOM_CONFIRM_MM = 0.8

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize(footprint: str) -> list[str]:
    """Library id → lowercase word tokens.

    Splits on separators AND camelCase humps, so `PhoenixContact_MCV` yields
    ['phoenix','contact','mcv'] — 'tact' is then not a token and cannot match.
    """
    name = (footprint or "").split(":")[-1]
    name = _CAMEL.sub(" ", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if t]


def _hits(tokens: list[str], keywords) -> bool:
    tokset = set(tokens)
    return any(k in tokset for k in keywords)


def classify(ref: str, footprint: str, body_overhang_mm: float = 0.0,
             include: set[str] | None = None,
             exclude: set[str] | None = None) -> str | None:
    """→ 'connector' | 'switch' | None.

    body_overhang_mm: how far the body outline sticks out past the pad bbox on
    its most-protruding side. Only consulted for the weak refdes signal.
    """
    if exclude and ref in exclude:
        return None
    if include and ref in include:
        return "connector"

    lib = footprint or ""
    family = lib.split(":")[0] if ":" in lib else ""
    if family and _VETO_LIB_PREFIX.match(family):
        return None

    tokens = tokenize(lib)
    joined = "".join(tokens)
    if set(tokens) & _VETO_TOKENS or any(v in joined for v in _VETO_TOKENS):
        return None

    # Connector before switch: 'Switchcraft' barrel jacks and 'PhoenixContact'
    # terminal blocks must land on their connector token, not a switch one.
    if _hits(tokens, _CONNECTOR_KEYWORDS):
        return "connector"
    if _hits(tokens, _SWITCH_KEYWORDS):
        return "switch"

    if body_overhang_mm >= GEOM_CONFIRM_MM:
        if _SWITCH_REF_PREFIX.match(ref):
            return "switch"
        if _CONNECTOR_REF_PREFIX.match(ref):
            return "connector"
    return None


def looks_vertical(footprint: str) -> bool:
    """True when the library id itself says the part mates from above.

    KiCad names top-entry parts `..._Vertical`; many vendor libraries use
    `_V` or `TopEntry`. Cheap, high-yield, and independent of geometry — which
    is exactly where the body-past-pads rule is weakest.
    """
    tokens = tokenize(footprint)
    if {"vertical", "topentry", "verticalmount"} & set(tokens):
        return True
    return bool(tokens) and tokens[-1] in ("v", "vt")


def looks_like_interface_ref(ref: str) -> bool:
    """Refdes alone suggests an interface part (J1 / CN3 / TB2 / SW1 …).

    Used to report the one blind spot nothing else can see: a part whose
    library name matches no keyword AND whose footprint carries no body
    outline, so neither the name signal nor the geometry signal fires. Its
    refdes is then the only evidence left that it might be a connector.
    """
    return bool(_CONNECTOR_REF_PREFIX.match(ref or "")
                or _SWITCH_REF_PREFIX.match(ref or ""))
