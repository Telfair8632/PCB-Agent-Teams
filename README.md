# PCB-Agent-Teams — KiCad PCB Workflow

**English** · [中文](README.zh-CN.md)
<!-- SYNC: this file and README.zh-CN.md are two language versions of one document — edit one, update the other in the same commit. -->

[![License: PolyForm NC 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-informational)](LICENSE.md)
![KiCad 10](https://img.shields.io/badge/KiCad-10-blue)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
![Runtime: Claude Code only](https://img.shields.io/badge/runtime-Claude%20Code%20only-orange)
![Platform: macOS tested, Linux untested](https://img.shields.io/badge/platform-macOS%20tested%20%7C%20Linux%20untested-lightgrey)

**Describe the board you want. Get back a KiCad project and a fab-ready Gerber package** — with every part
checked against live distributor stock, and every stage cleared by scripts, SPICE and DRC rather than by the
model's own say-so.

Under the hood: a multi-project PCB workspace built on KiCad 10, where ten skills drive a Phase 0–5 pipeline
covering the full chain from topology discussion to Gerber shipout.

<!-- TODO(visual): drop a board photo or a generated 3D render here — this is the one thing the page still
     lacks. Put the file in assets/ (tracked; docs/ is gitignored) and reference it as assets/<name>.png.
     Only use a board this pipeline actually produced. -->

### What a run looks like

```text
You:  I need an isolated 400 V DC-bus voltage sensor — 0–3.3 V out to an MCU ADC.

/circuit-design        topology fixed: isolated amplifier + staged HV divider, anchor parts named
/component-selecting-* 3 live candidates per part — all in stock, all with a ready KiCad library
/component-preparing   datasheets fetched, symbols + footprints + 3D vendored, BOM locked
/draw-schematic        .kicad_sch generated from Python source, ERC clean
/check-schematic       divider ratio re-simulated in SPICE, every pin cross-checked vs datasheet
/draw-pcb              HV/LV zones split, isolation barrier held, GND poured, DRC clean
/check-pcb             44 EMC rules + thermal + parasitics + schematic↔PCB netlist diff
/release               Gerber + CPL + production BOM + ORDER_GUIDE.md, zipped for the fab
```

You are never locked into that sequence — every step is a toolbox you can run, redo, hand-edit or skip.

### Before you read on — two things to know

1. **KiCad 10 is required, and macOS is the only platform this has been tested on.** The skills call
   `kicad-cli` and KiCad's bundled `pcbnew` Python API directly. Linux is wired up but unverified — the
   interpreter probe includes `/usr/lib/kicad/python3`, and `kicad-cli` resolution covers Flatpak, Snap and
   Nix. Windows is the real gap: `kicad-cli` resolves there, but no `pcbnew` interpreter path ships, so the
   board-writing steps in `draw-pcb` have nothing to call. Details in [Prerequisites](#prerequisites).
2. **Part selection ships for two locales — Japan and mainland China.** That is Phase 2 alone; *every other
   phase is locale-neutral and runs anywhere*. Elsewhere you either adapt one of the two shipped selection
   skills to your distributors (they are thin shells over a shared engine) or pick parts by hand and feed the
   list to `component-preparing`. The pipeline never silently falls back to another region's skill. Details in
   [First-time setup](#first-time-setup).

> **Runtime — multi-harness (fork).** All skills live in the harness-agnostic `skills/` directory;
> [`bootstrap.py`](bootstrap.py) links them into each agent's discovery directory (junctions on Windows,
> symlinks elsewhere) and generates the harness-specific instruction file from the canonical
> [`AGENTS.md`](AGENTS.md). Verified with ZCode and Claude Code; `SKILL.md` is an open format, so wiring up
> another harness is usually one more entry in `bootstrap.py`. See [Using another agent](#using-another-agent).
>
> **Just cloned this?** Open the folder in your agent and run `/setup`. It opens with one short question, then
> walks you through locale, toolchain and your profile in your own language. It stops firing once `USER.md` exists.

---

## Architecture

```text
PCB-Agent-Teams/
├── README.md             ← this file (English)
├── README.zh-CN.md       ← Chinese version (keep in sync with README.md)
├── AGENTS.md             ← canonical routing table (auto-loaded by ZCode & friends)
├── CLAUDE.md             ← generated from AGENTS.md by bootstrap.py for Claude Code (gitignored)
├── bootstrap.py          ← multi-harness installer: links skills/ into each agent's discovery dir
├── pcbenv.py             ← shared env discovery (kicad-cli / KiCad Python / venv / spec-file walk)
├── LICENSE.md            ← PolyForm Noncommercial 1.0.0
├── THIRD_PARTY_NOTICES.md ← MIT notices for vendored/derived third-party code
├── USER.md.example       ← user-profile template (copy to USER.md)
├── USER.md               ← your profile: hardware on hand / locale / skills / preferences (gitignored)
├── requirements.txt      ← Python dependencies
├── assets/               ← images used by the READMEs (tracked; `docs/` is local-only and gitignored)
├── skills/               ← 10 skills + `setup` (first-run guide) — harness-agnostic
├── tests/                ← repo-level tests (pcbenv & friends)
├── .claude/references/   ← workspace meta-protocols (protocols.md)
├── lib_external/         ← shared component library (starts empty; component-preparing vendors symbols/footprints/3D here)
├── lib_cache/sources/    ← read-only cache of external libs (pre-filter pool)
├── Projects/<name>/      ← project directory (generated by project-init)
│   ├── CLAUDE.md         ← project static compass
│   ├── STATUS.md         ← live progress / artifact index
│   ├── .gitignore        ← project-local ignore rules
│   ├── datasheets/       ← datasheet PDFs + evidence JSON + BOM gate sentinel + procurement BOM
│   ├── kicad/            ← circuit-synth .py + generated .kicad_sch / .kicad_pcb
│   ├── reference_designs/← vendor reference material
│   ├── layout/           ← placement / layout working material
│   ├── docs/             ← BOM / documentation
│   ├── analysis/         ← analyzer JSON / simulation artifacts (created by the check gates)
│   ├── _artifacts/       ← shortlist JSON + 4-axis sourcing prefs (written by selecting, read by release)
│   └── release/<ts>/     ← Gerber + production BOM + CPL (created by release)
├── logs/                 ← run logs
├── .venv/                ← Python 3.12 venv (gitignored)
├── .env.example          ← API-key template (copy to .env)
└── .env                  ← your API keys (gitignored)
```

---

## Highlights

Three gates built around "the things people trust least when an AI draws a PCB."

### 1. Zero-hallucination part selection

Every part number comes off a live distributor API. The model never gets to invent one.

- **Distributor APIs are mandatory** — no MPN comes from memory. The JP locale queries DigiKey / Mouser / LCSC in **three parallel lanes**, so one component gets N candidates compared side by side; the CN locale runs a single key-free LCSC lane
- **Hard filters**: stock active/nrnd + an existing component library available + spec match; anything with zero stock or no ready library is dropped
- **Fast search**: a short list comes back in seconds based on hard metrics (footprint / tolerance / temperature grade / voltage grade, etc.)
- **The whole BOM is worked in a single pass**; it only stops to ask when a spec has no answer on the market

### 2. Nothing gets drawn until the assets are on disk

Worried the AI skips ahead and starts drawing? An entry gate stops it.

- Before the candidate list is finalized, **datasheets are fetched up front** plus the component library (footprint symbol / 3D model / schematic symbol)
- **Four-point fidelity check**: MPN vs the locked BOM / footprint class (through-hole vs SMD) vs the datasheet / generic-symbol masquerading as a real IC / pin count vs the part's actual pin count — one mismatch flips the gate to fail
- **Part numbers are auto-injected** into schematic properties, avoiding manual-entry errors during drawing
- **The drawing entrance stays locked until the assets are ready** — there is no way around it

### 3. Everything is checked twice, two different ways

No number is trusted on a single reading. Every key quantity is verified by two independent layers, and failing any stage rolls the pipeline back — it never routes around.

**Schematic stage** (`check-schematic`):

- ERC triage + analyzer rule IDs, and datasheet pin-level cross-check (the `total_errors == 0` raw-output gate that prevents a "zero errors" false pass runs one stage earlier, in `draw-schematic`)
- SPICE subcircuit simulation (not just connectivity — actual dynamic behavior)
- Design review report — AI-generated analysis written for a human to sign off on, not an auto-pass

**PCB stage** (`check-pcb`):

- DRC design rule check
- EMC pre-compliance analysis — 44 rules across 18 categories (ground-plane integrity / decoupling / I-O interface filtering / switching-regulator EMC / clock routing …)
- Thermal analysis (power dissipation / temperature rise)
- Parasitic simulation (R/L/C back-extracted from the PCB, re-running SPICE)
- **Schematic ↔ PCB netlist cross-check**: the "which pin connects to which pin" list from the schematic is matched item by item against the actual PCB routing, looking for missing, wrong, or shorted connections

**Shipout stage** (`release`):

- **Four-axis sourcing-preference gate**: channel / brand / price-vs-stock / blacklist must be on record before a release is built — the recommended order path in `ORDER_GUIDE.md` is driven entirely by them, so the builder stops and asks instead of guessing
- **Procurement BOM vs production BOM kept on separate tracks**: the purchasing list and the assembly list are generated independently to avoid wrong orders or missing parts

---

## Skill pipeline

Each skill is a standalone toolbox, **not a mandatory pipeline**. At any stage you can: use a skill / do it by hand / skip it. Multi-step skills also support mid-run human review (render / DRC / simulation); roll back if unsatisfied.

| Phase | Skill | Responsibility |
| --- | --- | --- |
| 0 Skeleton | `project-init` | Scaffold the project: 5 subdirs (datasheets / kicad / reference_designs / layout / docs) + CLAUDE.md (9-section compass) + STATUS.md + .gitignore |
| 1 Topology | `circuit-design` | Lock the circuit topology, fix anchor components, write the 9-section project compass |
| 2 Selection gate | `component-selecting-*` | Route by locale (locale → vendor), produce shortlist JSON |
| 2.5 Asset prep + BOM gate | `component-preparing` | Applicability-review each top pick against its circuit role, then fetch datasheets, populate the library, write evidence, drop the BOM gate sentinel + procurement BOM CSV |
| 3 Schematic gen | `draw-schematic` | Source-driven generation of `.kicad_sch` + ERC clean + visual verification |
| 3.5 Schematic check gate | `check-schematic` | ERC triage + analyzer + SPICE subcircuit simulation + datasheet pin-level cross-check + design review |
| 4 PCB gen | `draw-pcb` | Zoned layout + GND zone + DRC + visual PDF, optional auto-routing |
| 4.5 PCB check gate | `check-pcb` | DRC + EMC + thermal + parasitic SPICE + sch↔pcb cross-ref + gerber audit |
| 5 Shipout | `release` | Gerber / CPL / production BOM + documentation PDF + fab decision + packaging |

> **Locale routing**: `component-selecting` is a phase name; the concrete skill is chosen by `USER.md §0` locale. Two locales are implemented: **`component-selecting-JP`** (DigiKey JP + Mouser JP + LCSC, needs a free DigiKey key) and **`component-selecting-CN`** (LCSC jlcsearch + jlcparts data shards — **zero API keys**). The US variant is not yet built. For any other locale, do not silently fall back — tell the user the matching skill is missing.

Any gate that fails: **roll back**, do not bypass. See the detailed Phase table in [CLAUDE.md](CLAUDE.md).

> Plus one non-pipeline skill: **`setup`** — the first-run guide. It is gated on `USER.md` being absent, so it runs once on a fresh clone and never again.

---

## Typical flow

```text
[empty dir]
   │
   ▼  /project-init
[project skeleton]
   │
   ▼  /circuit-design          ← topology + anchor parts
   ▼  /component-selecting-*   ← shortlist
   ▼  /component-preparing     ← datasheet + library + BOM
   ▼  /draw-schematic          ← .kicad_sch + ERC
   ▼  /check-schematic         ← ERC + analyzer + SPICE + datasheet cross-check
   ▼  /draw-pcb                ← .kicad_pcb + DRC
   ▼  /check-pcb               ← DRC + EMC + thermal + parasitics + cross-ref
   ▼  /release                 ← Gerber + production BOM
[release/<ts>/ + release_<ts>.zip]
```

---

## Three-layer documentation split

| File | Role |
| --- | --- |
| `CLAUDE.md` (workspace root) | Skill routing table; no domain knowledge |
| `Projects/<name>/CLAUDE.md` | Project static compass: design intent, parameters, BOM anchors |
| `Projects/<name>/STATUS.md` | Project live dashboard: current phase, artifact index, change log |
| `skills/<skill>/SKILL.md` | Skill manual: cross-project constraints, commands, failure modes |

---

## Two kinds of BOM — don't mix them

| BOM type | Written by | Purpose |
| --- | --- | --- |
| **Procurement BOM** | `component-preparing` | Order parts from distributors |
| **Production BOM / CPL** | `release` | Fab-house assembly / placement |

Upstream of both sits a third file, the selection-evidence CSV (`bom_v01.csv`, written by `component-selecting`) — it records vendor evidence per MPN for later gates, and is never used to order or assemble.

Details in [`component-preparing/references/bom_lifecycle.md`](skills/component-preparing/references/bom_lifecycle.md).

---

## First-time setup

> **Guided path (recommended).** Open the folder in Claude Code and run **`/setup`**.
> It opens with one short question, then handles the rest below in your own language — locale routing, toolchain
> check, `USER.md`, and keys only if your locale needs them. It is gated on `USER.md` being absent, so it
> runs on a fresh clone and never again. The manual steps below are the same thing, by hand.

> **Locale support.** As flagged up top, Phase 2 (component selection) is the only locale-bound stage — every other phase is locale-neutral and runs unchanged. Set yours in `USER.md §0`:
> - **China mainland** — supported out of the box via `component-selecting-CN`: LCSC jlcsearch + jlcparts data shards, **no API keys at all** (lifecycle is honestly labeled `unverified` since LCSC carries no NRND data).
> - **Japan** — the original `component-selecting-JP` (DigiKey JP + Mouser JP + LCSC; needs a free DigiKey key).
> - **Anywhere else** — either **(a)** adapt one of the two shipped skills into a variant for your region (both are thin shells over a shared locale-driven engine — add a locale block to `locale_mapping.yaml` and mirror the CN skill's structure), or **(b)** pick parts by hand and feed the list to `component-preparing`. The pipeline deliberately never falls back to another locale's skill silently.

### Prerequisites

- **[KiCad 10](https://www.kicad.org/download/)**, on **macOS** — the only tested platform. Skills call `kicad-cli` and KiCad's bundled `pcbnew` Python API (no MCP), and each is located differently:
  - `kicad-cli` — `PATH` first, then per-platform install locations: macOS app bundle + Homebrew, Linux Flatpak/Snap/Nix, Windows Program Files/Chocolatey/MSYS2. Putting `kicad-cli` on `PATH` satisfies this anywhere.
  - **`pcbnew` interpreter** — probed only at KiCad's bundled Python: `/Applications/KiCad/KiCad.app/…/Python.framework/…` (macOS) and `/usr/lib/kicad/python3` (Linux). **No Windows path ships**, so `draw-pcb`'s board-writing steps find no interpreter there. Linux has a path but has never been run.
  - (`KICAD_ROOT` is unrelated — it overrides the *project workspace root*, not the KiCad install location.)
- **Python 3.12** (3.13 / 3.14 are not supported).
- **[ngspice](https://ngspice.sourceforge.io/)** on `PATH` — `check-schematic`'s subcircuit simulation is ngspice-only (`brew install ngspice`); `check-pcb`'s parasitic / PDN SPICE also accepts LTspice or Xyce, auto-detected. Without a simulator neither fails — the SPICE steps are skipped and marked as such in the report.
- API keys — **locale-dependent**: the **Japan** pipeline needs at minimum a free [DigiKey developer](https://developer.digikey.com/) account; the **China-mainland** pipeline needs **no keys at all**. See [External APIs](#external-apis) for the full list of services and what each does.

> **Two things ship as source only — build or fetch them on demand:**
> - The Rust grid router (`draw-pcb/.../rust_router/`) ships as Rust source; the compiled library is gitignored. Build it with `python3 build_router.py` in `draw-pcb/scripts/vendor/KiCadRoutingTools/` (needs a [Rust toolchain](https://rustup.rs/)) — only the optional auto-routing phase needs it.
> - `lib_cache/sources/` (the external KiCad library pool used as a pre-filter) is gitignored and ships empty. Re-clone the upstream libraries into it on demand; until then `component-selecting` runs without the local pool.

### Setup

```bash
# 1. Create the Python environment and install dependencies
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium   # one-time browser download — DigiKey page probing / datasheet fetch needs it

# 2. Configure API keys
cp .env.example .env
#    then edit .env: fill in DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET
#    (required for the Japan locale only — the China-mainland locale needs no keys, skip this step)
#    other keys (Mouser / element14 / Firecrawl) are optional — see "External APIs" below

# 3. Create your user profile
cp USER.md.example USER.md
#    then edit USER.md: §0 locale drives which component-selecting skill runs
#    (§0 ships as [待填] — no default, you must fill it; Japan and mainland
#    China are both implemented, and an empty locale never silently falls back). Fill the
#    [待填] fields as they come up; skills read this before recommending any part,
#    so they never suggest hardware you cannot test or a package you cannot solder.
```

### Run

- **Claude Code**: run `python bootstrap.py` once, then open this folder — the root `CLAUDE.md` (generated) loads automatically, and the skills are available via `/<skill-name>`. On a fresh clone, start with `/setup`.
- **ZCode**: run `python bootstrap.py` once — skills land in `~/.zcode/skills` and the root `AGENTS.md` loads automatically.
- **Plain shell**: load the keys first with `set -a && source .env && set +a`, then run any skill script as `.venv/Scripts/python.exe skills/<skill>/scripts/<script>.py` (Windows) or `.venv/bin/python skills/<skill>/scripts/<script>.py` (POSIX).

### Notes

- `.env` (your real keys) and `.venv/` are gitignored and are never committed — keep your keys out of git.
- `Projects/` ships empty; `project-init` scaffolds a new board directory there on first run.
- Shared library naming conventions: [`lib_external/CONVENTIONS.md`](lib_external/CONVENTIONS.md).

---

## Using another agent

**This fork is multi-harness by design.** `bootstrap.py` is the one command that wires the workspace into
whatever agents you use — links every `skills/<name>` into each harness's discovery directory (junctions on
Windows, symlinks on POSIX) and generates the per-harness instruction file from the canonical `AGENTS.md`:

```bash
python bootstrap.py            # install/refresh all known harnesses (ZCode, Claude Code)
python bootstrap.py --list     # dry run: show the plan, change nothing
```

It is idempotent and safe to re-run after every clone or layout change: correct links are left alone,
stale links (e.g. pointing at the pre-2026-10 `.claude/skills` layout) are refreshed, real directories that
happen to share a name are never touched, and `CLAUDE.md` is only rewritten when `AGENTS.md` changed.

Harness coverage:

| Harness | Instruction file | Skills directory | Status |
| --- | --- | --- | --- |
| ZCode | `AGENTS.md` (canonical, tracked) | `~/.zcode/skills/` | ✅ verified |
| Claude Code | `CLAUDE.md` (generated) | `~/.claude/skills/` | ✅ verified |
| Codex / Copilot / Gemini | — | — | add one entry to `HARNESSES` in `bootstrap.py` |

**What to expect.** Skill *discovery* ports cleanly this way. Skill *execution* is harness-dependent — the
skills shell out to `kicad-cli`, the project venv and distributor REST APIs, and agents differ in how they
sandbox commands and network access. Environment discovery itself is centralized in
[`pcbenv.py`](pcbenv.py) (honors `KICAD_CLI` / `KICAD_PYTHON` / `KICAD_ROOT` env overrides), so a harness
that cannot reach the default install locations can still be pointed at them explicitly.

---

## External APIs

> **The keys below only apply to the Japan locale** (`component-selecting-JP` queries DigiKey JP + Mouser JP + LCSC in three parallel lanes, with DigiKey as the primary buyable gate; Mouser / element14 keys are also read by `check-pcb`'s lifecycle audit and `component-preparing`'s distributor query). The **China-mainland pipeline (`component-selecting-CN`) is fully key-free** — LCSC jlcsearch + jlcparts public data, nothing to sign up for. The US locale variant is not yet built.

### Distributor / sourcing APIs

| Service | What it does | Needed? | Env var | Get a key |
| --- | --- | --- | --- | --- |
| **DigiKey** | Primary buyable-gate — live stock / price / lifecycle (NRND/EOL) for the JP warehouse; drives the part shortlist | ✅ Required | `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET` | [developer.digikey.com](https://developer.digikey.com/) — free, OAuth |
| **Mouser** | Second equal source — cross-check stock/price + pull datasheet links | ⭐ Recommended | `MOUSER_SEARCH_API_KEY`, `MOUSER_PART_API_KEY` | [mouser.com/api-hub](https://www.mouser.com/api-hub/) |
| **element14 / Farnell** | Extra distributor source for stock + specs + lifecycle audit | ○ Optional | `ELEMENT14_API_KEY` | [partner.element14.com](https://partner.element14.com/) |
| **LCSC / JLCPCB** | China warehouse that **ships to Japan** (parts can co-order with the JLCPCB fab run), so it stays a valid third source for the JP pipeline + KiCad library fetch via `easyeda2kicad` | — No key (public) | — | — |
| **Firecrawl** | Datasheet web-scrape fallback when a distributor exposes no direct PDF | ○ Optional | `FIRECRAWL_API_KEY` | [firecrawl.dev](https://firecrawl.dev/) |

All keys go in `.env` (copied from `.env.example`). For the JP locale, DigiKey is the primary source; everything else widens coverage or adds a feature. Note the scripts do not abort on a missing key — they warn, and that vendor's queries come back as `fetch_error`, so a keyless JP run silently loses its main buyable gate. For the CN locale, skip this section entirely — no keys needed. Set the optional DigiKey locale vars (`DIGIKEY_LOCALE_SITE=JP` etc.) if you want non-default region/currency.

---

## Documentation entry points

| I want to… | Go to |
| --- | --- |
| Set up a fresh clone | run `/setup` in Claude Code, or [`skills/setup/SKILL.md`](skills/setup/SKILL.md) |
| Understand the overall routing | [CLAUDE.md](CLAUDE.md) |
| Learn a specific skill | `skills/<skill>/SKILL.md` |
| See cross-project electrical invariants | `skills/circuit-design/references/electrical_invariants.md` |
| See shared library naming conventions | [lib_external/CONVENTIONS.md](lib_external/CONVENTIONS.md) |
| See workspace meta-protocols (plan-first / sub-agent split / monitoring) | `.claude/references/protocols.md` |

---

## Acknowledgments

This workspace stands on excellent open-source work. Sincere thanks to these projects and their maintainers:

- **[KiCad](https://www.kicad.org/)** — the EDA foundation of the entire pipeline. Every schematic, board, ERC/DRC check, render, and Gerber goes through `kicad-cli` and the `pcbnew` Python API, on top of the official KiCad symbol / footprint / 3D libraries.
- **[ngspice](https://ngspice.sourceforge.io/)** — powers every SPICE check: subcircuit simulation in `check-schematic` and parasitic re-simulation in `check-pcb`.
- **[kicad-happy](https://github.com/aklofas/kicad-happy)** by aklofas — AI-agent skills for KiCad; the `check-pcb` analysis suite (EMC / thermal / lifecycle audit / project config / issue export) builds on it.
- **[circuit-synth](https://github.com/circuit-synth/circuit-synth)** — Python-source-driven `.kicad_sch` generation, the engine of `draw-schematic`.
- **[kicad-sch-api](https://github.com/circuit-synth/kicad-sch-api)** — programmatic schematic post-processing (label fixing, PWR_FLAG insertion, BOM verification).
- **[easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py)** — converts LCSC / EasyEDA parts into KiCad symbols / footprints / 3D models for `component-preparing`.
- **[KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools)** by drandyhaas — the optional auto-routing phase, including its Rust grid router (MIT, vendored under `skills/draw-pcb/scripts/vendor/`).
- **[jlcsearch](https://github.com/tscircuit/jlcsearch)** by tscircuit — the public LCSC search API: third sourcing channel for the JP pipeline, primary (and key-free) lane for the CN pipeline.
- **[jlcparts](https://github.com/yaqwsx/jlcparts)** by yaqwsx — published parametric data shards of the JLC catalogue; powers the CN pipeline's offline parametric discovery for categories jlcsearch lacks (inductors, ferrite beads).
- **[Playwright](https://playwright.dev/)** — headless-browser probing of distributor pages and datasheet fetching.
- And the Python ecosystem this runs on: NumPy, SciPy, Shapely, Matplotlib, Pillow, ReportLab, odfpy, python-docx, Jinja2, Requests, PyYAML, psutil, pytest.

If this project helps you, consider starring / supporting those upstream projects too.

---

## License

Source-available under the **[PolyForm Noncommercial License 1.0.0](LICENSE.md)**.
Free to use, modify, and share for **noncommercial** purposes (personal, research, education). **Commercial use requires a separate license** — contact the author. © 2026 zhang zheng.

Third-party components (kicad-happy-derived analyzers, vendored KiCadRoutingTools) remain under their original MIT licenses — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
