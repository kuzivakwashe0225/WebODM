# Testing — Precise Agric System

Covers **automated** tests (Django test runner) and points to the **manual** guide. Kept current as
stages land.

## Running the automated tests

```bash
# All Precise-Agric tests
docker compose -p webodm exec webapp python manage.py test agri --keepdb --verbosity 2

# A single test
docker compose -p webodm exec webapp python manage.py test agri.tests.TestAgri.test_boundary_create_list_approve --keepdb
```

- Tests live in `agri/tests.py`, one class `TestAgri` extending WebODM's `BootTestCase`
  (runs `boot()`, seeds `testsuperuser`/`testuser`/`testuser2` + a project each).
- `--keepdb` reuses the PostGIS test database `test_webodm_dev` between runs (skips migration replay).
- **Why runs are slow here:** `boot()` rebuilds every plugin's JSX because file mtimes are
  inconsistent on the Windows bind mount (`app/plugins/functions.py:130-153`). All tests share a
  single `boot()` (one class) to minimise this. First run ≈ 9–10 min; nothing to do with test count.

## Automated test inventory (`agri/tests.py` → `TestAgri`)

### Stage 1 — Boundary API / model

| Test | Verifies |
|---|---|
| `test_boundary_requires_auth` | `GET`/`POST` `/api/agri/boundaries/` return **403** when unauthenticated |
| `test_boundary_create_list_approve` | `POST` creates a **DRAFT** with `created_by` set; `geom` round-trips as GeoJSON; it appears in the list; `POST {id}/approve/` → **APPROVED** with `approved_by` + `approved_at` |
| `test_boundary_visibility_scoped_to_own_projects` | Another user cannot see a boundary on a project they don't own; the owner can |
| `test_boundary_filter_by_task` | `?task=<id>` returns only that capture's boundaries |
| `test_boundary_model_defaults` | New `Boundary` defaults to `DRAFT`; `__str__` includes the status |
| `test_capture_import_from_orthophoto` | External-import of `app/fixtures/orthophoto.tif` (no node) → `COMPLETED` with `orthophoto_extent`; file placed at canonical asset path |
| `test_capture_upload_endpoint` | `POST /api/agri/captures/` (multipart `.tif`) requires auth (403), else creates an external-import capture that processes to `COMPLETED` |
| `test_capture_upload_requires_file` | `POST` without an `orthophoto` file → 400 |

### Stage 2 — Analysis (Plant Health)

| Test | Verifies |
|---|---|
| `test_analysis_run_and_result` | `AnalysisRun` defaults to `PENDING`; `AnalysisResult` stats JSON round-trips |
| `test_plant_health_compute` | Import fixture → approved boundary → `compute_plant_health` returns `EXG` + real stats and writes the index raster |
| `test_run_analysis_orchestration` | `execute_analysis` → one `PLANT_HEALTH` result + `PENDING_REVIEW` + `completed_at` + raster on disk |
| `test_analysis_trigger_requires_approved_boundary` | `POST /api/agri/analysis/` on a DRAFT boundary → 400 (gate); APPROVED → 201 + `PENDING_REVIEW` |

### Stage 3 — Analysis fan-out services

| Test | Verifies |
|---|---|
| `test_rgb_index_compute` | ExG/VARI/GLI stats over the boundary + representative raster written |
| `test_canopy_cover_compute` | Canopy % in [0,100] + raster written |
| `test_grid_analysis_compute` | ≥1 grid cell; valid GeoJSON FeatureCollection written |
| `test_weed_mapping_compute` | Weed-point GeoJSON written + `weed_count ≥ 0` |
| *(updated)* `test_run_analysis_orchestration` | Full fan-out: all **6** result kinds + report aggregates plant-health + raster on disk + `PENDING_REVIEW` |

### Stage 4 — Review → AgriTrack push

| Test | Verifies |
|---|---|
| `test_analysis_review_approve_and_push` | Agronomist approve → `APPROVED` + `reviewed_by`; webhook POSTed once with report summary + 6 results (requests mocked) |
| `test_analysis_review_reject` | Reject → `REJECTED`; **no** push |
| `test_analysis_review_requires_reviewer` | Technician approve → 403 |
| `test_analysis_review_requires_pending_review` | Approving a non-`PENDING_REVIEW` run → 400 |
| `test_push_skipped_without_url` | `AGRITRACK_PUSH_URL=None` → push returns False, no HTTP call |

### Stage 5 — Roles & permissions

| Test | Verifies |
|---|---|
| `test_roles_seeded` | SuperAdmin/Admin/Technician/Agronomist groups exist after boot |
| `test_reviewer_sees_all_boundaries_and_can_reject` | Org-wide reviewer visibility + boundary reject |
| `test_boundary_create_scoped_to_project_rights` | Non-owner without role cannot attach a boundary to someone else's capture (403) |
| `test_agronomist_is_read_only` | Agronomist: create boundary 403, upload capture 403, trigger analysis 403 |
| *(updated)* `test_boundary_create_list_approve` | Technician approve → 403; Agronomist approve → 200 |

### Stage 0 — Rebrand

| Test | Verifies |
|---|---|
| `test_settings_app_name` | `settings.APP_NAME == "Precise Agric System"` |
| `test_boot_applies_branding_to_fresh_install` | A fresh `boot()` produces the name + full green palette (`THEME_COLORS`) on the default theme |
| `test_apply_branding_repairs_existing_rows` | `apply_branding()` restores a tampered name/colour on existing rows (idempotent repair) |

**Total: 28 tests — ALL GREEN (2026-07-06, `Ran 28 tests ... OK`).** Backend for all five stages
complete.

### Raw-image capture path (separate test class, run independently)

| Test | Verifies |
|---|---|
| `TestAgriRawImageCapture.test_raw_image_task_flows_through_boundary_and_analysis` | A capture created via WebODM's **native, unmodified** raw-image upload (`/api/projects/{id}/tasks/`), processed by a **real local NodeODM instance** to `COMPLETED`, flows through Boundary→AnalysisRun exactly like a pre-stitched-orthophoto capture — 6 results, `PENDING_REVIEW`. Proves the two capture paths converge with zero `agri/` code changes. |

Isolated in its own `BootTransactionTestCase` (matches WebODM's own `test_api_task_import.py`
pattern) because it drives a real subprocess NodeODM instance — slow (~6-7 min) and intentionally
kept separate from the fast `TestAgri` suite. Run with:
```
python manage.py test agri.tests.TestAgriRawImageCapture --keepdb --verbosity 2
```
**Result (2026-07-06): PASS** — `Ran 1 test in 385.3s`, `OK`, zero `FAILED`/`Traceback`/`ERROR` in
output (independently re-verified after fixing two test-harness flakiness causes — a leftover
zombie NodeODM process from an earlier failed run, and a too-tight node-startup retry budget; both
documented in `progress-log.md` and as comments in the test itself). Not accepted on a first "OK" —
each fix was verified with fresh evidence (`ps aux`, manual standalone timing) before re-running.

### Latest result
- 2026-07-05: **8/8 green** for Boundary API + branding. (Earlier 3 failures were caused by WebODM's
  global `ObjectPermissionsFilter` stripping every Boundary — no object perms assigned on Boundary;
  fixed with `filter_backends = []` on the viewset + ownership-scoped `get_queryset`.)
- Then added `test_capture_import_from_orthophoto` (TDD) and its implementation `agri/capture.py`.
- Capture-upload endpoint + 2 tests → **11/11 green** (Stage 1 backend done).
- **Stage 2 (Plant Health):** analysis models, `compute_plant_health` (reuses `formulas.py` +
  `export_raster`), `execute_analysis`, and the gated `POST /api/agri/analysis/` + 4 tests →
  **15/15 green** (Stage 2 backend done). Dev DB migrated.

### Test-speed note
`boot()` was rebuilding all plugin JSX every run (~9 min) due to bind-mount mtimes. Mitigated by
pinning coreplugin source mtimes old + build outputs new, and adding two missing SCSS stubs
(`plant_height/VegetationIndices.scss`, `weed_detect/ObjDetectPanel.scss`) so those two stop
failing/rebuilding. Runs should be much faster now.

### Stage 9 — Satellite pull (`TestSatellitePull`, separate class)

On-demand "Get from Sentinel" flow: pulling satellite imagery for a farm's onboarded boundary, and
pulling Sentinel's own reference index to compare against this system's own analysis. All network calls
mocked at the `agri.remote_sense.sentinel_client` boundary — these tests never hit the real
Copernicus/Sentinel Hub API (that connection is verified separately, live, and documented in
[stage-9-satellite-monitoring.md §7](stages/stage-9-satellite-monitoring.md)).

| Test | Verifies |
|---|---|
| `test_pull_satellite_capture_uses_agrifarm_boundary` | AOI resolution prefers the synced `AgriFarm.boundary`; resulting Capture is tagged `source=SATELLITE` |
| `test_pull_satellite_capture_falls_back_to_boundary_union` | No `AgriFarm.boundary` → falls back to the union of existing field boundaries instead of failing |
| `test_pull_satellite_capture_no_boundary_raises` | No boundary at all (farm never onboarded) → `SatellitePullError`, not a crash |
| `test_pull_satellite_comparison_requires_approved_boundary` | A DRAFT boundary is rejected — comparison requires the same approval gate as our own analysis |
| `test_pull_satellite_comparison_creates_sentinel_run` | Creates an `AnalysisRun(computed_by=SENTINEL, status=PENDING_REVIEW)` with the fetched index/mean stored; requests the correct capture date |
| `test_pull_satellite_comparison_no_data_raises` | Sentinel returning no data points → `SatellitePullError`, not a fabricated result |
| `test_satellite_imagery_endpoint_requires_auth` | `POST /api/agri/satellite/imagery/` → 403 unauthenticated |
| `test_satellite_imagery_endpoint_blocks_agronomist` | Agronomist role → 403 (review-only, matches the capture-upload permission model) |
| `test_satellite_imagery_endpoint_requires_valid_dates` | Missing dates, or `date_from > date_to` → 400 |
| `test_satellite_imagery_endpoint_dispatches_and_creates_capture` | 202 + `celery_task_id`; full poll round-trip via the existing `/api/workers/check/<id>`; a `SATELLITE`-tagged Capture actually lands in the DB |
| `test_satellite_compare_endpoint_requires_approved_boundary` | `POST /api/agri/satellite/compare/` on a DRAFT boundary → 400 |
| `test_satellite_compare_endpoint_dispatches_and_creates_run` | 202; the `computed_by=SENTINEL` `AnalysisRun` actually lands in the DB |
| `test_serializer_exposes_computed_by` | `AnalysisRunSerializer` includes `computed_by` (needed by the frontend to tell the two engines' results apart) |

**Result (2026-08-06): 13/13 green**, run for real via `./webodm.sh test backend agri.tests.TestSatellitePull`
in `--dev` mode (not just compiled/reasoned through). Full `agri.tests` suite re-run afterward:
**84/85 green** — the one failure is `TestAgri.test_resolve_agri_field_falls_back_to_persistent_field_link`,
confirmed pre-existing and unrelated to this work (fails identically in isolation, with zero satellite
code present; root-caused to `_agri_linked_run()`'s setup already creating the `Field` the test then
tries to create again). See [stage-9 §13](stages/stage-9-satellite-monitoring.md) for details.

Three real bugs were found and fixed by actually executing this suite (none were catchable by code
review alone): a `sentinelhub` `DataCollection` service-URL override that silently 401'd every live
Sentinel Hub request; a stray leftover line from an earlier edit causing a `NameError` in an unrelated
`TestRemoteSensePush` test; and the new Celery tasks not following this codebase's own
`TestSafeAsyncResult.set(...)` convention (used elsewhere in `worker/tasks.py`) for making eager-mode
task results visible to `/api/workers/check/` polling under `CELERY_TASK_ALWAYS_EAGER`.

### Stage 10 Phase 1 — Cloud/quality awareness + field targeting (`TestSatellitePull`, extended)

Cloud-masking (SCL-based `valid_pixel_pct`) and single-field targeting for the on-demand pull, per
[stage-10-sentinel-roadmap.md](stages/stage-10-sentinel-roadmap.md). `TestSatellitePull` grew from 13 to
24 tests; new coverage:

| Test | Verifies |
|---|---|
| `test_valid_pixel_pct_pure_function` | Real SCL classification arrays (all-valid, all-cloud, mixed, empty) — no mocking, no network |
| `test_pull_satellite_capture_stores_valid_pixel_pct` | A successful pull's quality metric lands on `CaptureMeta.valid_pixel_pct` |
| `test_pull_satellite_capture_degrades_gracefully_when_quality_check_fails` | `valid_pixel_pct=None` from the client never fails the pull — "flag, don't block" |
| `test_pull_satellite_capture_targets_single_field` | A `field_id` resolves to *that* field's boundary, not the farm-wide union — an unrelated boundary on the same farm is ignored |
| `test_pull_satellite_capture_unknown_field_raises` / `..._field_with_no_boundary_raises` | Bad or boundary-less field id → `SatellitePullError`, not a crash |
| `test_pull_satellite_comparison_creates_sentinel_run` *(updated)* | Now also asserts `valid_pixel_pct` passes through into `AnalysisResult.stats` |
| `test_pull_satellite_comparison_missing_quality_degrades_gracefully` | A point without `valid_pixel_pct` (older client shape) doesn't crash the comparison |
| `test_satellite_imagery_endpoint_accepts_field_target` / `..._rejects_field_from_other_farm` | The endpoint's `field` param is honoured, and cross-farm field ids are rejected |
| `test_fields_endpoint_accepts_project_param` / `..._requires_task_or_project` | `FieldListView` now also accepts `?project=` (needed by the new Import-menu modal, which has no `task` yet) — original `?task=` path re-verified unchanged |

**Result (2026-08-15): `TestSatellitePull` 24/24 green.** Full `agri.tests` suite re-run afterward:
**94/96 green** — the two failures are the *same* pre-existing, unrelated issues already documented
above (confirmed identical error signatures — not new regressions from this work).

Three more real bugs were found and fixed by live-testing the cloud-masking approach against the actual
Sentinel Hub API *before* writing the "final" version of the code (not just once retroactively): SCL
only supports `units: "DN"` and 400s if requested alongside `REFLECTANCE`-unit bands in the same input
block; hand-rolling the DN→reflectance correction formula as a workaround was tested side-by-side against
real server-computed reflectance and found to be systematically wrong by a flat 0.1; and a single-band
Process API response comes back as a 2D array, not 3D like the existing multi-band request, which raised
an `IndexError` the first time the real code ran. Full account in
[stage-10-sentinel-roadmap.md](stages/stage-10-sentinel-roadmap.md) Phase 1.

`main.js`'s new "Get Satellite Imagery" modal (Import-menu entry point) is **not** browser-tested — same
gap already flagged for the rest of the Stage 9/10 frontend.

### Stage 10 Phase 2 — Scene availability picker (`TestSatellitePull`, extended)

A read-only scene-search endpoint backed by Sentinel Hub's Catalog API, per
[stage-10-sentinel-roadmap.md](stages/stage-10-sentinel-roadmap.md) Phase 2. `TestSatellitePull` grew
from 24 to 30 tests; new coverage:

| Test | Verifies |
|---|---|
| `test_dedupe_scenes_by_date_pure_function` | Raw Catalog API results collapse to one entry per calendar day, keeping the least-cloudy scene on a same-day collision, sorted newest-first — no mocking, no network |
| `test_dedupe_scenes_by_date_empty` | Empty input doesn't crash, returns empty list |
| `test_satellite_availability_endpoint_requires_valid_dates` | Missing/malformed `date_from`/`date_to` or `date_from > date_to` → `400`, not a crash |
| `test_satellite_availability_endpoint_returns_scenes` | Happy path returns the deduped scene list for a farm-wide AOI |
| `test_satellite_availability_endpoint_targets_single_field` | `?field=` resolves to that field's boundary (same targeting rule as the Phase 1 pull endpoint) |
| `test_satellite_availability_endpoint_no_boundary_raises_validation_error` | A boundary-less field returns a `400`, not a 500 |

**Result (2026-08-15): `TestSatellitePull` 30/30 green.** Full `agri.tests` suite re-run afterward:
**100/102 green** — the two failures are the *same* pre-existing, unrelated issues already documented
above (confirmed identical error signatures across all three runs this session — Phase 1, Phase 2, and
the original stage-9 baseline — never new regressions).

No new bugs surfaced this time, but the same live-verification discipline was applied before writing the
endpoint: a standalone script confirmed `SentinelHubCatalog` works correctly with the CDSE-redefined
`DataCollection` (the same one whose service-URL override caused a silent 401 in stage-9) before any
endpoint code was written against the assumption — see
[stage-10-sentinel-roadmap.md](stages/stage-10-sentinel-roadmap.md) Phase 2 for the standalone script and
result.

`main.js`'s scene-list extension to the same modal is **not** browser-tested — same gap as Phase 1.

### Stage 10 Phase 3 — Satellite eligibility table (`TestAgri`, extended)

A plain constant dict replacing the two hardcoded `if satellite:` branches in `agri/services.py`
(weed skip, canopy proxy flag), per
[stage-10-sentinel-roadmap.md](stages/stage-10-sentinel-roadmap.md) Phase 3 — a pure refactor, not new
behavior, so this landed in `TestAgri` rather than `TestSatellitePull` (nothing here touches the Sentinel
client).

| Test | Verifies |
|---|---|
| `test_satellite_eligible_table_covers_all_analysis_kinds` | `SATELLITE_ELIGIBLE`'s keys exactly match `AnalysisResult.KIND_CHOICES` (a 7th analysis service added later without a table entry fails this test, not silently falls through), and pins the three non-trivial values (`WEED: False`, `CANOPY: 'proxy'`, the rest `True`) |

No behavior change, confirmed by the pre-existing Stage 9 tests
(`test_weed_mapping_skipped_for_satellite_capture`,
`test_canopy_flagged_low_resolution_proxy_for_satellite`, `test_report_includes_capture_source`,
`test_run_analysis_orchestration`) passing unmodified against the refactored code.

**Result (2026-08-15): 101/103 green.** The two failures are the *same* pre-existing, unrelated issues
already documented above (confirmed identical error signatures across all four runs this session — Phase
1, Phase 2, Phase 3, and the original stage-9 baseline — never new regressions). No live API surface here
(pure in-process refactor), so no standalone verification script was needed this time.

## Manual testing
Step-by-step (rebrand visual checks + Boundary API via curl / browsable API / admin) is in
[manual-testing.md](manual-testing.md).

## Notes / conventions
- Extend `BootTestCase` (not plain `TestCase`) so `boot()` + seed users/projects run.
- Geometry in requests/responses is **GeoJSON** (via WebODM's `PolygonGeometryField`).
- Visibility is ownership-scoped for the MVP; project sharing + org-wide reviewer roles arrive in
  **Stage 5** and will get their own tests then.
