# Stage 8 — Interactive map analysis + seasonal progress ✅ (2026-07-06)

Five features requested together, built in one pass. Backend fully TDD-tested (12 new tests green +
regression on boundary/AgriTrack flows); frontend verified via route/bundle checks + manual testing.

Plan: `.claude/plans/eventual-floating-galaxy.md`.

## Data model (migration `0004`)

- **`agri.Field`** (`agri/models/field.py`) — persistent field identity within a Farm (Project) so a
  field can be tracked across many captures. `unique_together (project, name)`; optional OneToOne to
  `AgriField` for AgriTrack-synced fields.
- **`agri.CaptureMeta`** (`agri/models/capture_meta.py`) — `OneToOne(Task)` + `capture_date`. Keeps
  the acquisition date (seasonal x-axis) without modifying the core Task model.
- **`Boundary.field`** — nullable FK → `agri.Field`. A boundary is one capture's outline of a Field.

## Feature A — Boundaries persist + are clickable

Root cause of "saved boundaries vanish": overlays were only ever drawn from inside the panel's
lifecycle. Fixed by making `App` (`public/app.jsx`) the **sole owner** of boundary layers — it fetches
`/api/agri/boundaries/?task=` on construction and draws them immediately, independent of the panel.
Each layer is clickable → selects the field (highlight + fit bounds + open panel + auto-show heatmap).
The old panel/render coupling and all debug scaffolding (`window.__pa`, console logs) were removed.

## Feature B — Plant-health heatmap overlay (reuses WebODM's tiler, zero new raster serving)

`AnalysisRunSerializer.plant_health_tile_url` (`agri/tiles.py`) builds a Leaflet XYZ URL against the
**existing** tiler: `/api/projects/{p}/tasks/{t}/orthophoto/tiles/{z}/{x}/{y}.png?formula=…&bands=…&
color_map=rdylgn&rescale={min},{max}&boundaries={geojson}`. Rescale comes from the plant_health
result's own min/max stats (data-driven, consistent with the mean shown, works for NDVI and raw ExG).
The panel toggles the overlay + an opacity slider + a red→yellow→green legend (`_showLegend`). None
until a run has a plant_health result.

## Feature C — Grid zones with click-popups (reuses the grid GeoJSON asset)

`GET /api/agri/analysis/<id>/grid/` serves the stored per-cell FeatureCollection (each cell already
carries `mean_index`). The panel's "Show zones" fetches it; `App.showGrid` renders `L.geoJSON` with
each cell filled on the same red→green scale and a click popup: value + plain-language reading
(`interpretIndex`, normalized within the field's own min/max → Low / Moderate / Healthy).

## Feature D — UX restyle + "talking to the user"

`PreciseAgricPanel.jsx` reworked into card-based fields with a gradient header, status pills, a
friendly plant-health headline per run (`healthHeadline`), inline metric values, map-toggle buttons,
and warm empty/loading copy ("🌱 No fields yet — draw your first one…", "Analyzing your field…").
Restyled `app.scss` (legend, grid popup, label) and `PreciseAgricPanel.scss` (full theme).

## Feature E — Seasonal progress

- **Capture date at upload:** `main.js` prompts for a date (default today); `CaptureUploadView` →
  `create_capture_from_orthophoto(capture_date=…)` → `CaptureMeta`. Fallback = `task.created_at`.
- **Field at boundary save:** the save form has a Field autocomplete (`/api/agri/fields/?task=`);
  `field_name` is get_or_create'd within the farm in `BoundaryViewSet.perform_create` (rejects a Field
  from another farm). AgriTrack sync auto-creates a linked Field per AgriField.
- **Seasonal API:** `GET /api/agri/seasonal/?project=` (`agri/api/seasonal.py`) → per-field and
  whole-farm (averaged) time series of health/canopy/weed/VARI, x-axis = capture_date.
- **Season page:** plugin `main_menu()` "Season Progress" + `app_mount_points()` rendering
  `templates/season.html` — a farm dropdown + Chart.js (vendored `Chart.min.js`, loaded only on this
  page) line charts, one line per field plus a bold "Whole farm (avg)" line.

## Tests (all green)

`test_field_model_and_boundary_link`, `test_capture_meta_created_on_upload_with_date`,
`test_capture_meta_defaults_to_today_when_omitted`, `test_boundary_create_with_field_name_creates_field`,
`test_boundary_rejects_field_from_other_farm`, `test_plant_health_tile_url_present_and_shaped`,
`test_plant_health_tile_url_none_before_analysis`, `test_grid_endpoint_returns_featurecollection`,
`test_seasonal_series_per_field_and_farm`, `test_seasonal_excludes_boundaries_without_field`, plus the
AgriTrack sync persistent-Field assertion. Regression: boundary create + AgriTrack approve/push green.

## Verified live (routes/bundle)

`/plugins/precise_agric/` → 302 (login-gated); `build/app.js` + `Chart.min.js` → 200;
`/api/agri/{boundaries,seasonal,fields}` → 403 unauth (registered, not 404). Rebuilt the webpack
bundle (`webpack-cli` in the plugin's public dir) and reloaded Django to register the new menu/mount.

## Follow-up fixes (2026-07-07)

- **Axis-order bug (root cause of "heatmap dead + boundaries on black + orthophoto missing").**
  `GEOSGeometry.geojson` swaps EPSG:4326 to `[lat, lng]` under GDAL 3 (OGR authority axis order),
  while the geometry is stored `[lng, lat]`. So the API returned swapped coords → boundaries rendered
  off Morocco, the map panned away from the orthophoto (black), and the heatmap cutline 404'd.
  Fixed with `agri/geo.py` (builds GeoJSON from `.coords`, which is correctly lng/lat), applied in
  `BoundarySerializer.to_representation` and `agri/tiles.py`. Locked by
  `test_boundary_geom_axis_order_is_lng_lat`. Grid GeoJSON was already correct (rasterio, not OGR).
- **Boundaries visibility toggle** — a show/hide "Boundaries" button (eye icon) added to the panel
  header (`App.setBoundariesVisible`).
- **Boundary reuse across captures** — `agri/capture.py:clone_farm_fields_to_capture` copies each
  Field's most-recent boundary onto every new capture of the same farm as an already-APPROVED
  Boundary. First upload sets fields up; later uploads inherit them (no redrawing). Test:
  `test_boundaries_reused_on_subsequent_capture`.
- **"Project" → "Farm" relabel** (user-facing): "Add Project"→"Add Farm" (Dashboard.jsx),
  New/Create/Edit dialog labels (EditProjectDialog.jsx, ProjectListItem.jsx), delete warnings.
  Other deeper "Project" strings remain; can be swept further if desired.

## Follow-up fixes (2026-07-07, round 2)

- **Boundary reuse was broken in practice** — the first version keyed off `Boundary.field`, but
  real drawn boundaries had `field=None` (drawn before the Field feature / on a stale bundle), so
  nothing was reused. Rewrote `clone_farm_fields_to_capture` to copy from the **most recent prior
  capture that has boundaries** (robust, independent of Field links), auto-creating a Field from each
  boundary name and back-filling the source so the season series lines up. Reused copies are DRAFT.
  Test rewritten to cover the `field=None` path + a 3rd upload.
- **Retroactive reuse** — `POST /api/agri/reuse-boundaries/ {task}` + a "Reuse fields from a previous
  capture" button in the panel's empty state, so captures that were uploaded before the fix (with no
  boundaries) can be populated without re-uploading.
- **Seasonal graphs, richer + farmer-facing** — seasonal API now also returns `gli_mean`, `exg_mean`
  (alongside `vari_mean`, `health_mean`, `canopy_pct`, `weed_count`). `season.html` rebuilt: summary
  stat strip, one card per index with a plain-language "what it means / how to forecast" description,
  a colored trend badge (Improving / Needs attention / Holding steady, direction-aware — down is good
  for weeds), latest whole-farm value, and filled Chart.js line charts (per-field + bold farm avg).

## Open / deferred

- **ExG heatmap coloring** uses the field's own min/max (fine visually) — not agronomically
  calibrated thresholds; NDVI uses standard bands. Same caveat as the classification thresholds.
- Boundaries **without** a Field are excluded from seasonal trends by design (documented).
- Season page uses `window.prompt` for the capture date (matches the plain-JS upload flow); a proper
  date-picker modal is a later polish.
- In-place boundary **shape editing** still deferred (delete + redraw).
