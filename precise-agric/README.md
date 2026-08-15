# Precise Agric System — Documentation

This folder is the home for everything about turning WebODM into **Precise Agric System**:
a rebranded WebODM that keeps all existing functionality and adds a precision-agriculture
workflow (capture → boundary approval → field analysis → agronomist review → push), built
entirely on WebODM's own stack.

## Contents

| Doc | What's in it |
|---|---|
| [architecture-and-plan.md](architecture-and-plan.md) | Full architecture, data model, engine explainer, reuse map, stages |
| [environment.md](environment.md) | How to run the dev stack + Windows gotchas (CRLF, PowerShell, `--dev`) |
| [progress-log.md](progress-log.md) | Dated log of everything done, in order |
| [testing.md](testing.md) | Automated test inventory + how to run |
| [manual-testing.md](manual-testing.md) | Step-by-step manual test guide |
| [stages/](stages/) | One file per build stage (what was built + how it was verified) |

## Status at a glance

| Stage | Functionality | Status |
|---|---|---|
| 0 | Rebrand → Precise Agric System (name, colours) | ✅ Done, verified live |
| 1 | `agri` app + Boundary draw + approval gate | ✅ Done |
| 2 | First analysis end-to-end (Plant Health) | ✅ Done |
| 3 | Full 6-service analysis fan-out + report | ✅ Done |
| 4 | Agronomist review → outbound push | ✅ Done |
| 5 | Roles & permission hardening | ✅ Done |
| 6 | Frontend map UI (draw/approve/analyze/review from the map) | ✅ Done — see [stage-6](stages/stage-6-frontend-map-ui.md) |
| 7 | AgriTrack mobile app integration (farm sync in, results push out) | ✅ Backend done, 🚧 live end-to-end unconfirmed — see [stage-7](stages/stage-7-agritrack-integration.md) |
| 8 | Interactive map (persistent clickable fields, plant-health heatmap, grid zone popups) + seasonal progress graphs | ✅ Done — see [stage-8](stages/stage-8-interactive-map-and-seasonal.md) |
| 9 | Sentinel satellite monitoring (source tagging, direct Copernicus/Sentinel Hub connection, on-demand "Get from Sentinel" imagery + comparison flow) | ✅ Backend built & test-verified; 🚧 frontend built, not browser-tested — see [stage-9](stages/stage-9-satellite-monitoring.md) |
| 10 | Sentinel roadmap (6 phases) + Phases 1–4 (cloud/quality awareness, field-level targeting, Import-menu entry point, scene-availability picker, satellite eligibility table, broader Sentinel indices) | ✅ Phases 1–4 built & test-verified; 🚧 Phases 5–6 plan-only — see [stage-10](stages/stage-10-sentinel-roadmap.md) |

**106 tests in `agri/tests.py`, 104 green** as of 2026-08-15 (includes 21 new Stage 10 Phase 1–4 tests,
real `./webodm.sh test backend agri.tests` run in `--dev` mode). The two failures are pre-existing and
unrelated to Stages 9/10 (`test_resolve_agri_field_falls_back_to_persistent_field_link` and a flaky local
NodeODM-startup test) — see [stage-9 §13](stages/stage-9-satellite-monitoring.md) for root cause.
Outstanding, tracked in [stage-7](stages/stage-7-agritrack-integration.md#outstanding--not-yet-resolved):
whether AgriTrack's inbound farm sync is REST or Socket.io-only, the exact shape of a requested
"token" field in the outbound push payload, and auth for AgriTrack fetching our output asset URLs.

## Key decisions (locked)

- Build **inside** WebODM; reuse modules; add only gaps. No NestJS/FastAPI/MapLibre.
- **Reuse `Project` = Farm, `Task` = Capture** (imported orthophoto). New code lives in the `agri/` app.
- Processing engine (NodeODM) is **bypassed** — captures are imported orthophotos.
- Approval state lives on agri models, never on `Task.status`.
- AgriTrack integration builds on the same principle: new `agri/agritrack/` module, new
  `AgriFarm`/`AgriField` models, no changes to how `Task`/`Boundary`/`AnalysisRun` fundamentally work.

See [architecture-and-plan.md](architecture-and-plan.md) for the full rationale (§16 for the
AgriTrack addendum).
