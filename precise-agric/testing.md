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

## Manual testing
Step-by-step (rebrand visual checks + Boundary API via curl / browsable API / admin) is in
[manual-testing.md](manual-testing.md).

## Notes / conventions
- Extend `BootTestCase` (not plain `TestCase`) so `boot()` + seed users/projects run.
- Geometry in requests/responses is **GeoJSON** (via WebODM's `PolygonGeometryField`).
- Visibility is ownership-scoped for the MVP; project sharing + org-wide reviewer roles arrive in
  **Stage 5** and will get their own tests then.
