# Stage 2 — First analysis end-to-end (Plant Health) 🚧

**Per the agreed plan, Stage 2 is the *single* Plant-Health analysis end-to-end** — NOT the full
6-service fan-out (that's Stage 3). Plant Health = true NDVI when a NIR band exists, else the best
RGB index (ExG); output is a spatial health raster + zonal stats over the APPROVED boundary.

## Design

- **Models** (`agri/models/analysis.py`): `AnalysisRun` (task + boundary; status
  `PENDING→RUNNING→PENDING_REVIEW→APPROVED/REJECTED/FAILED`) and `AnalysisResult` (kind, `asset_path`,
  `stats` JSON).
- **Compute** (`agri/analysis/plant_health.py`): choose index (NDVI/ExG from `orthophoto_bands`),
  then **reuse** `app/api/formulas.py` (`get_auto_bands` + `lookup_formula`) and
  `app/raster_utils.export_raster` — which clips to the boundary via `gdalwarp -cutline` (GeoJSON
  cutline treated as EPSG:4326 and reprojected to the ortho CRS) and evaluates the numexpr expression
  into a single-band float32 GeoTIFF. Stats (mean/min/max/std/count) read back from that raster.
- **Orchestration** (next): `run_analysis(run)` runs plant-health, saves an `AnalysisResult`, sets the
  run to `PENDING_REVIEW`.
- **Trigger + approval gate** (next): `POST /api/agri/analysis/` creating an `AnalysisRun` — **rejected
  unless the boundary is APPROVED** (this is the plan's "Start Analysis unlocks only after approval").

## Task checklist

- [x] `AnalysisRun` + `AnalysisResult` models + migration (`0002`) + model test
- [x] `compute_plant_health` (clip + index + stats) + test
- [x] `run_analysis` orchestration (creates PLANT_HEALTH result, sets `PENDING_REVIEW`) + test
- [x] Trigger endpoint `POST /api/agri/analysis/` gated on APPROVED boundary + tests
- [x] **Stage 2 backend complete — 15/15 green; dev DB migrated**
- [ ] View the health raster on the map (frontend, later)

## Verification plan

1. Model test: `AnalysisRun` defaults to `PENDING`; `AnalysisResult` stats JSON round-trips.
2. Compute test: import `app/fixtures/orthophoto.tif` → approved boundary (full extent) →
   `compute_plant_health` returns `EXG` + real stats and writes the raster.
3. Orchestration test: `run_analysis` on an approved boundary → one `PLANT_HEALTH` result +
   `PENDING_REVIEW`.
4. Gate test: triggering analysis on a `DRAFT` boundary → 400/validation error.

## Staying on plan
Stage 2 delivers **one** analysis + the approval gate. The remaining five services (RGB index, grid,
weed, canopy, report) and the Celery `chord` fan-out are **Stage 3**.
