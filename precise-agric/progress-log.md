# Progress log

Chronological record of everything done. Newest at the bottom of each stage.

---

## Stage 0 — Rebrand → "Precise Agric System" ✅

**Goal:** rename WebODM and recolour it to a crop-green theme, keeping all features.

**Findings**
- WebODM is built to be white-labeled: `app/models/setting.py` (`app_name`, `app_logo`) and
  `app/models/theme.py` (14 colour fields + CSS/HTML injection), all editable at `/admin`.
  Renaming/recolouring is **config, not code** — but code defaults matter for fresh installs, and
  the existing DB rows must be updated for the change to show on a running install.

**Changes**
- `webodm/settings.py`: `APP_NAME = "Precise Agric System"`.
- `app/branding.py` (new): single source of truth — green palette + idempotent
  `apply_branding()` / `apply_theme_colors()`.
- `app/boot.py`: applies green colours when the default Theme is created (fresh installs).
- `app/management/commands/apply_branding.py` (new): updates the existing DB's name + colours.

**Palette:** `header_background`/`tertiary`/`button_primary` → `#2e7d32`, `primary` → `#1b3a2b`,
`highlight` → `#f2f7f2`; other colours kept.

**Verification**
- Applied to the running DB; `apply_branding` reported
  `name='Precise Agric System', theme='PreciseAgri'`.
- Served login page contains "Precise Agric System"; theme CSS contains `#2e7d32`.

---

## Environment fix (unblocking dev) ✅

The rebrand code initially "wasn't visible" — root cause was environment, not code:
1. `webodm.sh` was being run from **PowerShell** (bash script → no-op); the `--dev` restart never
   happened, so source was never mounted.
2. Once mounted, **CRLF** line endings crashed the containers (`/bin/bash^M: bad interpreter`).
3. First `--dev` boot ran the full slow `setup-devenv` (npm/pip/translations/webpack).

**Actions**
- Brought the stack up via `docker compose ... -f docker-compose.dev.yml up -d` (project `webodm`).
- Converted all repo `*.sh` to LF (Python byte-replace).
- Let deps install; skipped the pathological all-locale `translate build`; main webpack + plugin
  bundles built; Django `runserver` came up on :8000.
- Verified `/webodm` mount active and `manage.py apply_branding` now runs (mounted code live).

See [environment.md](environment.md) for the durable how-to.

---

## Stage 1 — `agri` app + Boundary draw + approval gate 🚧

**§14 import risk — RESOLVED (design).** WebODM's import saves any uploaded file as `all.zip` and
unzips it (`app/api/tasks.py:745`, `app/models/task.py:handle_import`), so a **lone `.tif` cannot be
imported directly**. Solution: use `import_url="file://external"` (task.py:606) — place the
orthophoto at the canonical asset path `odm_orthophoto/odm_orthophoto.tif` before dispatching
`process_task`; extraction is skipped and it goes straight to extent/band detection → `COMPLETED`.
Efficient (no zipping large files) and reuses WebODM's completion pipeline. Sample orthophotos exist
at `app/fixtures/orthophoto.tif` (RGB) and `app/fixtures/tiny_drone_image_multispec.tif` (has NIR).

**Changes (this step)**
- New `agri/` Django app: `apps.py`, `models/boundary.py` (`Boundary`), `admin.py`,
  `api/{serializers,views,urls}.py`, `migrations/`.
- `Boundary` model: FK to `app.Task` (the capture), `geom` (PolygonField, SRID 4326),
  `status` DRAFT/APPROVED/REJECTED, `created_by`/`approved_by`/`approved_at`.
- API at `/api/agri/boundaries/` (list/create; `POST .../{id}/approve`), GeoJSON via WebODM's
  `PolygonGeometryField`. Visibility scoped to viewable projects (role hardening deferred to Stage 5).
- Wired: `agri` added to `INSTALLED_APPS`; `/api/agri/` included in `app/urls.py`.

**Verification (done):**
- `agri.0001_initial` applied OK; PostGIS table `agri_boundary` exists with
  `geom geometry(Polygon,4326)`, `task_id uuid` FK → `app_task`, GiST spatial index.
- `GET /api/agri/boundaries/` → `403` (route wired, auth-guarded; not 404).

**Dev-mode friction noted:** each `.py` edit makes `runserver` reload → `boot()` → `init_plugins`
rebuilds plugin JSX (~1–2 min). Management commands (`makemigrations`/`migrate`) are slow while this
churns. Tolerable; can be optimised later if it becomes painful.

**Automated tests (TDD):** `agri/tests.py` → `TestAgri` (extends `BootTestCase`). **8/8 green** for
Boundary API + branding, after fixing the global `ObjectPermissionsFilter` that was stripping every
Boundary (no object perms on Boundary) via `filter_backends = []` on the viewset. See
[testing.md](testing.md).

**Capture import (TDD, validating):** added `test_capture_import_from_orthophoto` first, then
`agri/capture.py` `create_capture_from_orthophoto`: creates a Task with `import_url="file://external"`
+ `pending_action=IMPORT`, places the `.tif` at `odm_orthophoto/odm_orthophoto.tif`, and processing
completes it with `orthophoto_extent` populated — no processing node. Validated against
`app/fixtures/orthophoto.tif`.

**Test-speed fix:** pinned coreplugin source mtimes old + build outputs new, and added two missing
SCSS stubs (`plant_height`, `weed_detect`) so `boot()` stops rebuilding all plugins each run.

**Capture-upload endpoint (done):** `POST /api/agri/captures/` (`CaptureUploadView`) + 2 TDD tests.
**Stage 1 backend complete — 11/11 tests green.**

**Remaining Stage 1 (deferred):** boundary drawing UI on the Leaflet map (frontend, manual-verified).

---

## Stage 2 — Analysis pipeline (Plant Health first) 🚧

TDD, backend first. Started with the data model: `AnalysisRun` (task + boundary; status
PENDING→RUNNING→PENDING_REVIEW→APPROVED/REJECTED/FAILED) and `AnalysisResult` (kind, asset_path,
stats JSON).

**Plant-health compute (`agri/analysis/plant_health.py`):** picks NDVI (NIR) or ExG (RGB), then
reuses `formulas.py` (`get_auto_bands`/`lookup_formula`) + `raster_utils.export_raster` to clip to
the boundary (`gdalwarp -cutline`) and evaluate the index → single-band float32 GeoTIFF; stats read
back. **Orchestration** `execute_analysis` (+ Celery `run_analysis`, registered in `CELERY_INCLUDE`)
saves a `PLANT_HEALTH` result and sets `PENDING_REVIEW`. **Gated trigger** `POST /api/agri/analysis/`
refuses unless the boundary is APPROVED.

**Stage 2 complete — 15/15 tests green.** Dev DB migrated (`agri_analysisrun`, `agri_analysisresult`).

**Next: Stage 3** — fan out to the other five services (RGB index, grid analysis, weed mapping,
canopy cover, report builder) via a Celery `chord`, per the plan.

---

## Stage 3 — Full 6-service fan-out + report ✅

Services in `agri/analysis/`: `rgb_index` (ExG/VARI/GLI), `grid` (~10 m cells → GeoJSON),
`canopy` (% via index threshold), `weed` (adapted `weed_detect` NGRDI + ndimage), `report`
(aggregation + recommendations), on shared helpers in `base.py` (reuse `formulas.py` +
`export_raster`). `execute_analysis` runs all six → 6 `AnalysisResult`s → `PENDING_REVIEW`.
Individual services validated **19/19 green**; the full-fan-out orchestration test validated in the
Stage 4/5 suite run.

**Acknowledged deviation (execution only):** services run **sequentially** inside the one
`run_analysis` Celery task instead of the plan's parallel `chord` — identical outputs/model/API;
eager-mode chords are unreliable under test, and parallelism is a later optimisation.

---

## Stage 4 + Stage 5 — Review→push + Roles (implemented together, TDD) 🚧 validating

**Stage 4:** `POST /api/agri/analysis/{id}/approve|reject/` (reviewers only, from `PENDING_REVIEW`);
approval dispatches the AgriTrack push (`agri/push.py`, Celery retry ×3). Config:
`AGRITRACK_PUSH_URL` (`WO_AGRITRACK_PUSH_URL`, default None = stub/skip).

**Stage 5:** `agri/roles.py` — SuperAdmin/Admin/Technician/Agronomist groups seeded in `boot()`;
reviewers (SuperAdmin/Admin/Agronomist) get org-wide visibility + approval authority; Technician
contributes to own projects, cannot approve; Agronomist read-only except approvals. Also **fixed a
real hole**: boundary create now requires contribute rights on the capture's project (previously any
authed user could attach a boundary to any task).

**Breaking-by-design test update:** `test_boundary_create_list_approve` — the technician creator now
gets 403 on approve; an agronomist approves.

**Result (2026-07-06): 28/28 tests GREEN (`Ran 28 tests ... OK`).** Stages 3, 4, 5 backend complete.
Role groups seeded into the live dev DB (`setup_roles()` → SuperAdmin/Admin/Technician/Agronomist);
they also re-seed automatically on every boot.

---

## ALL BACKEND STAGES COMPLETE ✅ (2026-07-06)

Pipeline live end-to-end: rebranded app → upload orthophoto (`POST /api/agri/captures/`) → draw
boundary → reviewer approves → trigger analysis (gated) → 6-service fan-out (plant health, RGB
indices, grid, canopy, weed, report) → `PENDING_REVIEW` → agronomist approve → AgriTrack push
(stub until `WO_AGRITRACK_PUSH_URL` is set) — with 4-role permissions enforced.

**Remaining (known, deliberate):**
- Frontend pass: boundary drawing on the Leaflet map, analysis/review screens, role-aware UI,
  result layers (health raster / grid cells / weed points) on the map.
- Real AgriTrack API contract (payload shape is ours, documented in `agri/push.py`).
- Parallel `chord` execution (optimisation; currently sequential — documented deviation).
- Per-farm assignment (v2; org-wide reviewers per locked decision).

---

## Capture path revised: raw drone images now supported (2026-07-06)

User feedback: pre-stitched-orthophoto-only was too narrow — a technician with just raw drone photos
should be able to upload them and have real NodeODM stitching happen, converging into the same
Boundary/Analysis workflow. See `architecture-and-plan.md` §3.1 for the design (superseded decision #9).

**Finding (from code, not assumption):** `Boundary`/`AnalysisRun` only ever hold a FK to `app.Task`
and read `task.orthophoto_extent`/`orthophoto_bands`/asset files — never `import_url` or
`processing_node`. Both the external-import path and the real-NodeODM-download path call the same
`Task.extract_assets_and_complete()`. **Zero changes to `agri/` code were needed** — only a test
proving the convergence, using WebODM's native, unmodified `/api/projects/{id}/tasks/` endpoint.

**Test:** `agri/tests.py::TestAgriRawImageCapture` (separate `BootTransactionTestCase`, matching
WebODM's own `test_api_task_import.py` pattern — drives a real local test-mode NodeODM subprocess).
Uploads `tiny_drone_image.jpg`/`_2.jpg` via the native endpoint, drives it to `COMPLETED`, then runs
the identical Boundary→AnalysisRun→6-results pipeline. **Result: PASS** (`Ran 1 test ... OK`,
385s, zero `FAILED`/`Traceback`/`ERROR` — independently re-verified after fixing two flakiness causes
below, not accepted on a first "OK").

**Two real bugs found and fixed while stabilizing this test** (both in test-only code — no
production/`agri` fix needed):
1. **Leftover zombie NodeODM subprocess.** `app/tests/utils.py`'s `start_processing_node()` (WebODM's
   own helper) sends SIGTERM + waits 1s on cleanup — not reliably enough to kill this Node.js process
   in this container. A leftover instance from an earlier failed run stayed bound to port 11223,
   colliding with each new test's fresh subprocess (confirmed via matching `pid` in `ps aux` and the
   log's mid-test "second startup" + "Found orphaned directory ... removing" deleting the *original*
   instance's in-progress task). Fixed with a `pkill -9 -f 'node index.js.*--port 11223'` pre-flight
   in `TestAgriRawImageCapture.setUp()` — scoped to this test file only, WebODM's shared util untouched.
2. **Retry budget too tight for this container.** Directly measured (manual standalone start, not
   guessed): a cold Node.js start takes 5-10s in isolation on this Windows bind-mount, longer under
   the concurrent I/O load of Django's own `BootTransactionTestCase` setup. Widened the
   `update_node_info()` retry loop from 15 to 60 one-second attempts.

Both capture paths now documented + proven:
| Path | Endpoint | Engine | Speed |
|---|---|---|---|
| Pre-stitched orthophoto | `POST /api/agri/captures/` | Bypassed | Seconds |
| Raw drone images | `POST /api/projects/{id}/tasks/` (native, unmodified) | Real NodeODM | Minutes |

---

## Other fixes this session (manual-testing feedback)

- **Logo replaced** with the user-provided AgriTrack image, applied to both the live `Setting.app_logo`
  and the fresh-install default (`app/static/app/img/logo512.png`). Flagged: it's a wide banner logo,
  will appear thin/cropped as the 36px sidebar icon / favicon (both square) — revisit if it looks off.
- **Clarified, not changed:** "Add Project" *is* "Add Farm" (locked decision #4); UI relabeling was
  always planned but not yet done (decision #6, still open).
- **Clarified, not changed:** `pa_tech` must log in with username `pa_tech`, not the email
  `tech@test.com` — standard Django username-based auth, not a bug.
- **Confirmed via DB query:** the user's own logged-in account is already a Django superuser →
  automatically `SuperAdmin` per `agri/roles.py::get_role()`. No separate test superadmin needed.
- **Clarified, not changed:** the "unreachable URLs" the user reported were actually a `200` (project
  list) and a correct `405 Method Not Allowed` (GET on a POST-only upload endpoint) — both proof the
  routes are wired correctly, not evidence of failure.

---

## First frontend pass: `coreplugins/precise_agric` map panel (2026-07-06)

Built a real, working map plugin (see MapView/plugin research summary): a "Precise Agric" toggle
button opens a side panel (boundary list, analysis trigger, review); reused WebODM's own plugin
architecture (`PluginsAPI.Map.willAddControls`, matching `coreplugins/measure`/`contours`).

**Bug found + fixed:** `__init__.py` was empty; WebODM's plugin loader does
`getattr(importlib.import_module("coreplugins.X"), "Plugin")` — i.e. on the **package**, not the
`plugin.py` submodule. Every other coreplugin re-exports via `from .plugin import *` in `__init__.py`;
mine didn't. Fixed. (Also hit a stale-`.pyc` red herring and had to trigger reloads via actual
Edit/Write tool calls — bare `touch` doesn't reliably bump mtime on this Windows bind-mount for
Django's autoreloader, same root cause as earlier session issues.)

**Redesigned per user feedback:** the first version used a hand-rolled `map.on('click', ...)`
polygon-drawing implementation — it silently failed to capture clicks (very likely intercepted by
other map layers; a known Leaflet issue). User correctly pointed out WebODM already has a proven
polygon-drawing tool: `app/static/app/js/components/CropButton.jsx`. Investigated it directly:
**`CropButton` is a generic, reusable polygon-drawing component** — full-screen invisible click-capture
layer, live preview, accept-marker, shift-angle-snap, right-click/double-click to finish — it does
**not** itself crop anything; whatever consumes its `onPolygonChange` callback decides what to do
with the resulting GeoJSON. Reused it directly (`import CropButton from 'webodm/components/CropButton'`)
purely to *capture* a boundary polygon — the orthophoto is never touched, so multiple named boundaries
can coexist without any "rest of the image disappearing" (that destructive behavior lives in
whatever WebODM's own Map.jsx does when *its* Crop button's polygon is actually applied — we never
call that path). Verified: webpack compiled clean (0 errors), served `app.js` confirmed to contain
the new `"Draw Field Boundary"` control (i.e. not a stale cached build).

**Still not done (flagged, not silently skipped):** a UI for uploading a pre-stitched orthophoto
(`POST /api/agri/captures/`) — still curl/Postman-only. Next candidate for a UI pass.

---

## Panel fixes + UX pass, round 2 (2026-07-06, same day)

User confirmed drawing now works after the CropButton swap, then reported: panel invisible when
"Precise Agric" icon clicked; draw button not green; wants an explicit name-prompt immediately after
drawing; wants drawn boundaries to persist as visible, named overlays (user's choice to show/hide);
wants delete/undo and asked to be treated skeptically as a real user, not just told it works.

**Real bug found (not cosmetic):** `.precise-agric-panel-container { position: absolute; }` had no
size or offsets — an empty div appended to the map collapses to a zero-size point, so the panel
inside it (`top: 50px; right: 10px`) resolved those offsets against a meaningless point instead of
the map viewport. This — not a missing feature — is why nothing appeared. Fixed: container now
spans the full map (`top/left/right/bottom: 0`) with `pointer-events: none` so it doesn't block map
interaction, and the panel itself gets `pointer-events: auto` back.

**Icon color:** `CropButton`'s `color` prop only tints the *drawn polygon*, not the button icon
itself (confirmed by reading its source) — so passing `color: '#2e7d32'` never affected the button.
Fixed via a CSS attribute selector on the button's own tooltip text
(`a[title="Draw Field Boundary"] { color: #2e7d32 !important; }`), since CropButton's own markup
doesn't expose a class hook for this.

**UX additions, per feedback:**
- All boundaries (not just APPROVED) now render on the map as colored, **permanently labeled**
  overlays (DRAFT=grey, APPROVED=green, REJECTED=red) via `L.geoJSON(...).bindTooltip(name, {permanent:
  true})` — addresses "fields should show their name embedded on the map."
- A **"Show boundaries on map" checkbox** (default on) gives the user explicit control, per their
  request — not automatic-only.
- Boundary rows now have a **Delete** button (confirm dialog, warns that deleting an APPROVED
  boundary cascades to delete its analysis runs too) — the "get back in case of a mistake" ask.
- The pending-save form is clearer ("Field drawn — give it a name to save it:"), auto-focuses and
  auto-selects the name input, and has a "Discard" (undo icon) action.
- Boundary overlays persist on the map independent of the side panel being open/closed (they're
  plain Leaflet layers owned by `app.jsx`, not part of the React panel's lifecycle).

**Explicitly deferred, not silently dropped:** in-place boundary **shape editing** (dragging existing
vertices) is not built — delete + redraw is the current workaround. Flagged as a candidate follow-up
(would need a heavier tool like Leaflet.Editable/Geoman).

**Verification:** local babel transform of both changed `.jsx` files passed with no syntax errors
before triggering a container rebuild (faster feedback than waiting ~5 min per cycle). Full rebuild
then confirmed: `webpack ... compiled with 3 warnings` (zero errors), `Registered
[coreplugins.precise_agric.plugin]`, and the **served** `app.js` was fetched and grepped to confirm
it contains `"Show boundaries on map"` and `"boundary-label"` — i.e. verified as the new build, not
a stale cached one.

---

## Boundary overlays confirmed working; Capture Upload UI shipped (2026-07-06)

**Diagnosed "boundaries not showing" with real evidence, not another guess.** Added temporary debug
logging (`console.log`) plus a `window.__pa` global debug handle to `app.jsx`/`PreciseAgricPanel.jsx`,
and checked actual DB data first (`Boundary.objects.all()` — 3 real APPROVED boundaries with valid
polygon geometry, ruling out empty/corrupt data). User's console output then showed all 3 boundaries
successfully added (`layer added OK`, `map has layer= true`, correct bounds) — confirming the render
path works; the earlier "not showing" was the panel-container CSS bug from the prior fix round,
already resolved. Debug logging left in for one more round pending final visual confirmation.

**Explained, not a new bug:** the `ERR_EMPTY_RESPONSE` errors the user saw while testing WebODM's
native "External Data" import coincided with a server reload I was triggering in parallel for the
debug build — confirmed server healthy immediately after (`200` on login, correct `405` on the
import endpoint via GET).

**Shipped: Capture Upload UI** (the "still not done" item flagged repeatedly). Found the real
integration point by reading `ProjectListItem.jsx`: `PluginsAPI.Dashboard.addImportTaskItem` is
already wired into the **native "Import" dropdown** (`triggerAddImportTaskItem` at
`ProjectListItem.jsx:371`, appending directly after "Assets / Backups" / "External Data"). Added
**"Upload Orthophoto (Precise Agric)"** as a new item in that same dropdown — file picker (.tif/.tiff)
→ prompt for a name → `POST /api/agri/captures/` (multipart) → refreshes the task list on success.

Implemented in `main.js` (not a new webpack entry) using `React.createElement` directly, since
`main.js` is loaded as a plain script (no JSX/babel transform) — confirmed by design (`include_js_files()`
serves it raw, unlike `build_jsx_components()` entries). This also means **no container rebuild was
needed**: `main.js` is served straight from disk, and `curl`ing it immediately showed the new code
live.

This closes out Stage 6 (frontend map UI) — see [stage-6-frontend-map-ui.md](stages/stage-6-frontend-map-ui.md).

---

## Stage 7 begins: AgriTrack mobile app integration (2026-07-06)

User's focus shifted from "finish the frontend" to "make communication with the real AgriTrack
mobile app work" — a separately-developed app where farmers register farms/fields, and expect to
see drone-analysis results back on their phone. Two contract documents were provided across the
session: an earlier "Full Remote Sensing Schema" (aspirational rebuild target — camelCase,
`/integrations/orthophoto/results`) and a second, later one describing what AgriTrack **actually
has live today** (Socket.io `mobile:farm_sync` for inbound farm data, and a real REST
`POST /orthophoto/analysis/push` for outbound results, snake_case, `X-Api-Key` auth). Built against
the second, live document — treating the first as background context only.

**New models** (`agri/models/agritrack.py`, migration `0003_auto_20260706_1448`): `AgriFarm`
(AgriTrack's farm ID, farmer ID, one-to-one with a `Project`) and `AgriField` (AgriTrack's field
ID, crop, area, **required** boundary — treated as authoritative, never overwritten from imagery
for this MVP). `Boundary` gained a nullable `agri_field` FK so a boundary can originate from either
a hand-drawn polygon or a synced field.

**Inbound sync** (`agri/agritrack/sync.py`, `agri/agritrack/views.py`): `POST /api/v1/mobile/sync`,
`X-Api-Key`-gated (constant-time `hmac.compare_digest`), idempotent create-or-update by AgriTrack's
own farm/field IDs. First sync of a farm auto-creates a `Project`, owned by a configurable service
account (farmers have no WebODM login).

**Outbound results push** (`agri/agritrack/results.py`): targets the confirmed-live
`POST /orthophoto/analysis/push`. Maps our Plant Health / RGB-index / Canopy / Weed results to
their `metrics` object (including `vari_mean/min/max`), builds output asset URLs, and — this was a
deliberate choice, not an oversight — **honestly omits** metrics we don't compute (plant count,
height, spacing, crown diameter) rather than inventing plausible-looking numbers. Health
classification (good/fair/poor) uses proposed NDVI/ExG thresholds, flagged in-code as pending
agronomist review (ExG especially — it's a much rougher heuristic than NDVI).
`agri/push.py:push_analysis` now branches: AgriTrack-linked boundaries use this new path; hand-drawn
ones keep the original Stage 4 generic webhook.

**Bug found and fixed via TDD**: `@override_settings(AGRITRACK_INBOUND_API_KEY=...)` had no effect
on the new sync tests (401 even with the right key) — `agri/agritrack/views.py` reads
`from webodm import settings` (the raw module), and Django's `override_settings` only patches
`django.conf.settings`, a different object. Fixed by mutating the module attribute directly in
`setUp`/`tearDown`, matching the pattern already proven in the Stage 4 push tests.

**Result: 12 new tests, all green** (`TestAgriTrackSync` ×7 + 5 more in `TestAgri`), confirmed via a
full background test run (`Ran 12 tests in 718.764s ... OK`) — including
`test_orthophoto_result_payload_maps_our_metrics`, which explicitly asserts `vari_mean` is present,
directly answering the user's later question of whether VARI results can be sent to AgriTrack.

Full detail: [stage-7-agritrack-integration.md](stages/stage-7-agritrack-integration.md).

---

## Real AgriTrack endpoint wired in (2026-07-06, same day)

User provided AgriTrack's live ngrok tunnel (`https://wasp-drastic-nursery.ngrok-free.dev` →
their `localhost:8001`) and the real `X-Api-Key` issued for the results-push endpoint. Wired both
into the environment rather than hardcoding: `WO_AGRITRACK_RESULTS_PUSH_URL` and
`WO_AGRITRACK_OUTBOUND_API_KEY` added to `.env`, referenced from `docker-compose.dev.yml` via
`${...}` substitution (avoids putting a real secret directly into a file that's part of the
tracked diff).

**Secret-handling caveat surfaced and resolved with the user:** this repo's `.env` turned out to
already be git-tracked (since an early commit), which means its own `.gitignore` entry for `.env`
doesn't actually protect it — gitignore doesn't retroactively apply to already-tracked files. Flagged
this directly rather than silently proceeding. User's explicit decision: leave `.env` tracked as-is
and rely on discipline (no `git add -A` / `git commit -a` while the key is populated) rather than
untracking it. Noted for future sessions: **never stage this file broadly.**

**Two items deliberately left unimplemented, pending clarification:**
- A "token" the user was told should be attached to the outbound payload "so the mobile app can
  know the farm and fields number." We already send explicit `field_id`/`farm_id`, so it's unclear
  what additional token is meant (the API key itself? a separate per-farmer/device token?). Asked
  the user directly rather than guessing a field name that might not match AgriTrack's real
  contract — answer: **not sure, needs to be confirmed with the AgriTrack team first.**
- Whether AgriTrack's inbound farm sync can actually call our REST endpoint, vs. Socket.io-only —
  still unconfirmed, still blocking any sidecar-service work.

Stack restarted (`./webodm.sh restart`) to pick up the new environment variables — a code reload
alone doesn't apply new container env vars, only a recreate does.
