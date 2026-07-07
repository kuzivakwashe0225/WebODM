# Precise Agric System — Implementation Plan

> **What this is:** the agreed, refined plan for turning WebODM into **Precise Agric System** — a
> rebranded WebODM that keeps all existing functionality and adds a precision-agriculture workflow
> (boundary approval → multi-part field analysis → agronomist review → outbound push), built
> entirely on WebODM's existing stack. This document is the single source of truth; later sections
> supersede earlier design ideas from the discussion.

---

## 1. Goal (plain language)

Take the working WebODM system, **rebrand it** to "Precise Agric System" (name + colors + logo),
**keep every feature it already has**, and **add agronomic functionality incrementally**, one stage
at a time, reusing WebODM's own modules rather than building anything new alongside it.

**Explicitly dropped** (from the original separate-app idea): NestJS gateway, FastAPI microservices,
MapLibre GL, and any separate frontend. We use only WebODM's stack.

## 2. Locked decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Overall strategy | Build **inside** WebODM; reuse modules; add only the gaps |
| 2 | Product identity | Rebrand WebODM → **Precise Agric System**; keep all existing features |
| 3 | Rebrand method | **Both** — configure the live install via `/admin` **and** bake defaults into code |
| 4 | Domain model | **Reuse `Project` = Farm, `Task` = Capture** (imported orthophoto). No new Farm/Capture models |
| 5 | New backend code | Lives in a new first-class **`agri/` Django app** (Boundary, AnalysisRun/Result, roles, pipeline) |
| 6 | UI approach | Modify WebODM's existing UI **in place** + theming; relabel Project/Task as Farm/Capture in the UI |
| 7 | Field vs Boundary | **Collapsed** — a Capture (Task) has one or more Boundaries; no separate Field entity in v1 |
| 8 | Sharing | **Org-wide reviewers** — Admin/Agronomist see & approve across all; Technician sees own |
| 9 | Processing engine | **Superseded 2026-07-06** — see §3.1. Two capture paths now: (a) pre-stitched orthophoto import, bypassing NodeODM, or (b) raw drone images via WebODM's native/untouched NodeODM pipeline. Both converge into the same agri workflow, proven by `agri.tests.TestAgriRawImageCapture`. |
| 10 | Approval state | Lives on **agri models**, never on `Task.status` |
| 11 | Ingestion | **Async** via `pending_action = IMPORT` (no synchronous upload processing) |
| 12 | Dev environment | **Docker** via `./webodm.sh start --dev` |

## 3. How WebODM works (reference — so the system is understood)

WebODM is a **client/orchestrator**, not the photogrammetry engine.

- **Engine = NodeODM**, a *separate* service (Docker image `opendronemap/nodeodm:stable`), attached via
  `docker-compose.nodeodm.yml` and registered as a DB row by the `addnode` command. The only copy in
  this repo is the dev-only git submodule `nodeodm/external/NodeODM`.
- **The seam:** `nodeodm/models.py` `ProcessingNode` wraps the `pyodm` HTTP client
  (`process_new_task`, `get_task_info`, `download_task_assets`, …).
- **Orchestration lifecycle** (driven by Celery beat every 5s → `worker.tasks.process_task` →
  `Task.process()` at `app/models/task.py:671`):
  1. Assign a node (`find_best_available_node`)
  2. Upload images (`process_new_task`) → node returns a UUID
  3. Poll status/console/progress (`get_task_info`) each tick
  4. On `COMPLETED`, download + extract `all.zip` (`download_task_assets`) → task done
  5. Separately, `update_nodes_info` (30s) refreshes node health
- **Originally, Precise Agric bypassed this engine entirely** for captures that arrive as an
  already-stitched orthophoto: `pending_action = IMPORT` (`handle_import`, `app/models/task.py:598`)
  never assigns a node and never calls pyodm/NodeODM — the Task goes straight to `COMPLETED`.

### 3.1 Two capture paths (revised 2026-07-06)

Per user decision, Precise Agric now supports **both** ways a Capture can originate — no rewrite of
`agri/` code was needed for this, because `Boundary`/`AnalysisRun` hold a plain FK to `app.Task` and
never inspect *how* it reached `COMPLETED`. Both origins call the exact same
`Task.extract_assets_and_complete()` (task.py:598 for imports; task.py:910-925 after a real NodeODM
download) to populate `orthophoto_extent`/`orthophoto_bands` — so the agri workflow (Boundary →
AnalysisRun → 6-service fan-out) is identical downstream regardless of origin.

| Path | Endpoint | Engine | Speed |
|---|---|---|---|
| (a) Pre-stitched orthophoto | `POST /api/agri/captures/` (`agri/capture.py`) | Bypassed (`file://external` import) | Seconds |
| (b) Raw drone images | WebODM's native `POST /api/projects/{id}/tasks/` (unmodified) | Real NodeODM stitching | Minutes (real processing) |

Proven, not assumed: `agri.tests.TestAgriRawImageCapture.test_raw_image_task_flows_through_boundary_and_analysis`
uploads real JPEG fixtures through path (b), drives a local test-mode NodeODM instance to `COMPLETED`,
then runs the full Boundary→AnalysisRun→6-results pipeline on it — passing, confirming convergence.

## 4. Architecture

```
   Browser (Leaflet UI, rebranded)                WebODM stack (rebranded, extended in place)
   ─ MapView + ESRI satellite base    ┌───────────────────────────────────────────────┐
   ─ existing WebODM screens (kept)   │  Django + DRF ──▶ Postgres / PostGIS            │
   ─ new agri screens/panels ─────────┼──▶  app/   (REUSED: import, tiler, formulas,    │
                                       │           measure, cogeo, theming, guardian)    │
                                       │     agri/  (NEW: Boundary, AnalysisRun/Result,  │
                                       │           roles, analysis pipeline)             │
                                       │        ▲                                        │
                                       │        │ Celery (Redis broker)                  │
                                       │   worker ── agri.tasks: chord of 6 analyses     │
                                       │        └──▶ outbound push (webhook) on approval  │
                                       └───────────────────────────────────────────────┘
   ✗ NodeODM engine — NOT used (captures are imported orthophotos)
```

## 5. Data model (refined — reuse Project/Task)

```
User ──owns──< Project(=Farm) ──< Task(=Capture, imported orthophoto) ──< Boundary
                                            │                                 │
                                            └──< AnalysisRun ─────────────────┘ (uses one APPROVED boundary)
                                                    └──< AnalysisResult (×6: rgb_index, plant_health,
                                                                         grid, weed, canopy, report)
```

**Reused as-is (no new model):**
- `Project` → **Farm** (relabeled in UI). Grouping + ownership + guardian permissions already exist.
- `Task` → **Capture / flight** (relabeled). An imported-orthophoto Task already carries
  `orthophoto_extent` (WGS84 bounds), `orthophoto_bands` (→ NIR detection), tiles, asset serving,
  thumbnails, permissions.

**New — in the `agri/` app:**
- **Boundary** — `task(FK app.Task)`, `name`, `geom(PolygonField srid=4326)`,
  `status[DRAFT→APPROVED|REJECTED]`, `created_by`, `approved_by`, `approved_at`
- **AnalysisRun** — `task(FK)`, `boundary(FK)`,
  `status[PENDING→RUNNING→PENDING_REVIEW→APPROVED|REJECTED|FAILED]`, `index_used`,
  `triggered_by`, `reviewed_by`, `celery_group_id`, `created_at`, `completed_at`
- **AnalysisResult** — `run(FK)`, `kind`, `asset_path`, `stats(JSONField)`, `created_at`

**State machines (independent of `Task.status`):**
- Boundary: `DRAFT → APPROVED` — only APPROVED unlocks analysis
- AnalysisRun: `PENDING → RUNNING → PENDING_REVIEW → APPROVED` → triggers push (or `REJECTED`/`FAILED`)

## 6. Workflow (9 steps → WebODM mechanics)

| # | User action | Internally |
|---|---|---|
| 1 | Create farm | Create a `Project` (labeled Farm) |
| 2 | Upload orthophoto | Create a `Task` with `pending_action = IMPORT` pointing at the uploaded TIFF; return immediately |
| 3 | (async) Import | Celery tick → `Task.process()` → `handle_import()` extracts extent + bands → Task `COMPLETED`; capture is READY (NIR flag from `orthophoto_bands`) |
| 4 | View on map | Existing MapView, base = **ESRI Satellite**, overlay = ortho tiles from existing `tiler.py` |
| 5 | Draw boundary | Leaflet.Draw polygon → save `Boundary(DRAFT)` |
| 6 | Approve boundary | `Boundary → APPROVED` (Admin/Agronomist); analysis unlocks |
| 7 | Start analysis | `AnalysisRun(PENDING)` + Celery **chord** → `RUNNING` → results → report → `PENDING_REVIEW` |
| 8 | Agronomist review | Views 6 outputs + report on map/UI |
| 9 | Approve → push | `AnalysisRun → APPROVED` → signal → outbound **webhook** posts report + outputs |

## 7. Analysis pipeline (the "fan-out")

```python
# agri/tasks.py  (registered via CELERY_INCLUDE += ['agri.tasks'])
run_analysis(run_id):
    ortho = run.task.assets_path('orthophoto.tif')
    geom  = run.boundary.geom                       # clip mask
    chord(
      group(rgb_index, plant_health, grid_analysis, weed_mapping, canopy_cover)  # parallel
    )(report_builder)                                                            # callback
```

Each task: `rasterio.mask` clip to boundary → compute → write result asset (COG via `app/cogeo.py`)
under the Task's assets dir → save `AnalysisResult` + stats. `report_builder` aggregates the five →
report (JSON; optional PDF via the `snapshot` pattern) → sets `PENDING_REVIEW`. Concurrency bounded by
`WORKERS_MAX_THREADS`.

**Per-service reuse:**
- `rgb_index`, `plant_health` → **`app/api/formulas.py`** (ExG, VARI, GLI, NDVI; auto band-detect)
- `weed_mapping` → **adapt `coreplugins/weed_detect/api.py`**
- `grid_analysis`, `canopy_cover`, `report_builder` → **new**, small (rasterio / shapely / numpy)

## 8. Reuse vs. new — exhaustive

**Reused untouched:** import flow (`handle_import`, `TaskAssetsImport`), `app/api/tiler.py`,
`MapView.jsx` + `Basemaps.js` (ESRI), `measure` / Leaflet.Draw, `app/api/formulas.py`,
`app/cogeo.py`, `django.contrib.gis` (PostGIS), guardian permissions, `boot.py` group-seeding
pattern, `coreplugins/tasknotification` webhook pattern, the built-in theming
(`Setting` + `Theme`), plugin JS API (`PluginsAPI.Map/.Dashboard`).

**New — `agri/` app:** `models/` (boundary, analysis), `api/` (urls, viewsets, serializers,
permissions), `tasks.py` + `analysis/` compute modules, role/permission seeding.

**UI:** modify WebODM screens in place (relabel Project/Task → Farm/Capture; add boundary +
analysis panels), plus theming for the rebrand.

**Core edits — kept minimal:**
1. `webodm/settings.py`: add `'agri'` to `INSTALLED_APPS`; append `'agri.tasks'` to `CELERY_INCLUDE`;
   `APP_NAME = "Precise Agric System"`
2. `app/urls.py`: `include('agri.api.urls')` under `/api/agri/`
3. `app/boot.py`: seed the 4 role groups + green theme defaults
4. UI relabeling + new panels in existing React views

## 9. Roles & permissions

Four seeded Groups — **SuperAdmin, Admin, Technician, Agronomist**. Custom perms:
`upload_capture`, `approve_boundary`, `run_analysis`, `approve_analysis`. Org-wide reviewers =
Admin + Agronomist get view/approve across all; Technician adds/views own. Enforced in
`agri/api/permissions.py` (DRF classes); frontend gates UI on role. Groups seeded early (Stage 0);
full enforcement in Stage 5.

## 10. API surface (draft)

```
# Farms/Captures reuse existing WebODM project/task endpoints (relabeled in UI)
POST /api/agri/tasks/{id}/boundaries/          POST /api/agri/boundaries/{id}/approve
POST /api/agri/tasks/{id}/analyze              GET  /api/agri/analysis/{id}/
POST /api/agri/analysis/{id}/approve           GET  /api/agri/analysis/{id}/results/{kind}/(tiles|geojson)
# ortho tiles: reuse existing task tile endpoints
```

## 11. Directory layout to create

```
agri/
  apps.py  models/  api/  tasks.py  analysis/  migrations/  boot.py(role + branding seeding)
```

(No separate UI plugin — UI changes go into WebODM's existing React views + theming.)

## 12. Rebranding — config vs. code

WebODM is built to be white-labeled (`app/models/setting.py`, `app/models/theme.py`), all editable at
`/admin`. **Renaming/recoloring needs no code** — but code defaults make fresh installs come up
branded, and the current DB rows must be updated for the change to show on the running install.

**Name:** `Precise Agric System`

**Proposed palette (crop green; change only brand-carrying fields):**

| Theme field | WebODM | Precise Agric |
|---|---|---|
| `header_background` | `#3498db` | `#2e7d32` |
| `tertiary` (nav links) | `#3498db` | `#2e7d32` |
| `button_primary` | `#2c3e50` | `#2e7d32` |
| `primary` (text/icons) | `#2c3e50` | `#1b3a2b` |
| `highlight` (panels) | `#f7f7f7` | `#f2f7f2` |
| `success` / danger / warning / bg / header text | — | keep defaults |

**Logo:** provide a 512×512 PNG (replaces `app/static/app/img/logo512.png`), or keep current for now.

## 13. Build stages (one functionality each; demoable by Stage 2)

| Stage | Functionality | Reuse / New | Done when |
|---|---|---|---|
| **0** | **Rebrand** → Precise Agric System (name, colors, logo) + seed role groups | Theming (`Setting`/`Theme`), `boot.py` | App shows "Precise Agric System" in green on reload |
| **1** | **Boundary draw + approval gate** | measure / Leaflet.Draw, gis; new `Boundary` | Draw → approve → analysis button unlocks |
| **2** | **First analysis end-to-end (Plant Health)** ← first real MVP | `formulas.py`, tiler colormap; new `AnalysisRun` | Approved boundary → analyze → health map appears |
| **3** | **Full 6-service fan-out + report** | `formulas.py`, `weed_detect`; new grid/canopy/report | One run yields all six outputs + report |
| **4** | **Agronomist review → outbound push** | `tasknotification` pattern | Approve run → payload delivered (stub OK) |
| **5** | **Roles & permission hardening** | guardian, `boot.py` seeding | Each role can do only its allowed actions |
| **6** | **Frontend map UI** *(added post-plan, §16)* | `CropButton`, `L.Control` | Full workflow usable from the map, no admin/API needed |
| **7** | **AgriTrack mobile integration** *(added post-plan, §16)* | new `AgriFarm`/`AgriField` | Farm sync → Project; approved run → pushed to AgriTrack |

**Sequencing note:** role *seeding* happens in Stage 0; full *enforcement* is Stage 5 so we don't
fight permissions while iterating. Can be pulled forward if security-first is preferred.

## 14. Highest risk to burn down first

WebODM's import is built around ODM **asset bundles**. Stage 0/1 must confirm hands-on that a **lone
`orthophoto.tif`** imports cleanly and yields correct `orthophoto_extent` + `orthophoto_bands`. If a
bare `.tif` isn't accepted, the fallback is a small "wrap the TIFF into the expected layout" step.

## 15. Environment & status

- Run via **Docker**. **Must use `--dev`:** `./webodm.sh start --dev`. Only the `--dev` overlay
  (`docker-compose.dev.yml`) mounts the source (`.:/webodm`) into the containers. Plain
  `./webodm.sh start` runs the **baked image code** — host edits are invisible, so no code change
  takes effect. Verify with `docker inspect webapp` → expect a `... -> /webodm` mount.
- First `--dev` boot runs `--setup-devenv` (git submodules, npm, pip, webpack watch) — slow, needs
  network. The DB is a named volume and persists across `stop`/`start`.
### Environment gotchas resolved (Windows) — READ before touching the stack

1. **Don't run `webodm.sh` from PowerShell.** It's a bash script; PowerShell silently does nothing
   (no output). Use **Git Bash**, or drive Docker directly (below).
2. **CRLF line endings crash the containers when source is mounted.** Git checked out `.sh` files
   with `\r\n`; the mounted scripts fail with `/bin/bash^M: bad interpreter` (exit 126, crash-loop).
   Fix applied: converted all repo `*.sh` to LF (via Python byte-replace; MSYS `sed \r` does NOT
   work). Keep them LF — consider a `.gitattributes` with `*.sh text eol=lf`.
3. **First `--dev` boot is very slow** (npm, pip into `/webodm/venv`, `translate build` over all
   locales, main webpack ~100s, then each coreplugin's JSX build). One-time; persists after.
4. Stack is currently run via **docker compose directly** (not webodm.sh), project name `webodm`:
   ```bash
   WO_DEV=YES WO_DEBUG=YES docker compose -p webodm \
     -f docker-compose.yml -f docker-compose.nodeodm.yml -f docker-compose.dev.yml up -d
   ```
   Exec commands as: `docker compose -p webodm exec webapp python manage.py <cmd>`.
   Dev server is Django `runserver` (auto-reloads on `.py` edits); `webpack --watch` rebuilds JS.

- **Status — Stage 0 (rebrand): ✅ DONE & VERIFIED LIVE.**
  - Code: `settings.APP_NAME`, `app/branding.py`, `app/boot.py` seeding, `manage.py apply_branding`.
  - Applied to running DB; served login page shows "Precise Agric System" + green `#2e7d32`.
  - Dev environment now correct: source mounted at `/webodm`, bundles built, server up on :8000.
  - Known minor: 1 coreplugin JSX build reported an error during boot (non-fatal; app runs). Check
    only if a specific plugin's UI misbehaves.
- Open inputs: logo (provide 512×512 PNG / keep current / placeholder).
- **Next: Stage 1** — scaffold `agri/` app + `Boundary` model + draw/approve API & UI; prove the
  lone-`.tif` import.

## 16. Addendum — frontend + AgriTrack integration (added 2026-07-06, after Stages 0-5 shipped)

Stages 6 and 7 below were not in the original plan (§13) — they were added once the backend was
complete and the user asked for a real UI and a real mobile-app integration. Full detail lives in
their own stage docs; this section just updates the plan-level picture.

| Stage | Functionality | Reuse / New | Done when |
|---|---|---|---|
| **6** | **Frontend map UI** (`coreplugins/precise_agric`) | `CropButton`, `L.Control`, `PluginsAPI.Dashboard.addImportTaskItem` | Draw/approve/analyze/review all doable from the map, no admin/API needed |
| **7** | **AgriTrack mobile integration** | new `AgriFarm`/`AgriField`; `agri/agritrack/` | Farm sync creates a Project; approved run pushes results to AgriTrack |

See [stages/stage-6-frontend-map-ui.md](stages/stage-6-frontend-map-ui.md) and
[stages/stage-7-agritrack-integration.md](stages/stage-7-agritrack-integration.md) for the full
implementation detail, bugs fixed, and — importantly — what's still open (Socket.io-vs-REST for
inbound sync, the AgriTrack "token" field semantics, output-asset-URL auth, ExG threshold review).

**Architectural note:** AgriTrack introduces a *third* way a `Boundary`'s geometry can originate
(hand-drawn was the only one in the original plan). `Boundary.agri_field` is nullable specifically
so both paths can coexist without a schema change — a boundary is either drawn (`geom` set
directly) or sourced from a synced field (`geom` copied from `agri_field.boundary`).
