# Stage 3 — Full 6-service analysis fan-out + report ✅

The analysis fans out to six services over the APPROVED boundary; the report aggregates them; the
run ends `PENDING_REVIEW`.

## Services (`agri/analysis/`)
- `plant_health.py` — NDVI (NIR) or ExG (RGB) health raster + stats (from Stage 2)
- `rgb_index.py` — ExG / VARI / GLI stats + representative raster
- `grid.py` — ~10 m grid cells → per-cell mean index (GeoJSON, reprojected to WGS84)
- `canopy.py` — % canopy via index threshold
- `weed.py` — adapted `weed_detect` (NGRDI + `scipy.ndimage`) → weed-point GeoJSON + count
- `report.py` — aggregates the five → summary + recommendations (JSON)
- `base.py` — shared clip/index/stats helpers (reuse `formulas.py` + `export_raster`)

## Orchestration
`agri/services.py::execute_analysis(run)` runs the six sequentially, saving an `AnalysisResult`
each, then sets `PENDING_REVIEW`. Dispatched by `agri/tasks.run_analysis` (Celery), triggered by
`POST /api/agri/analysis/` (gated on an APPROVED boundary).

## Acknowledged deviation (execution only)
The plan specifies a Celery `chord` for **parallel** fan-out. We run the services **sequentially**
within the one `run_analysis` task — identical outputs, model and API; parallelising via a real
chord is a later optimisation. Flagged in [../progress-log.md](../progress-log.md).

## Status
✅ **19/19 tests green.** Each service has its own compute test; the orchestration + trigger tests
exercise the full six-service pipeline → all six results + report + `PENDING_REVIEW`.

## Deferred
- Rendering the result layers (health raster, grid cells, weed points, report) on the map — frontend.
