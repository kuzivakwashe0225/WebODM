# Stage 10 — Sentinel Integration Roadmap (post Stage 9 audit)

> **Status (2026-08-15): All 6 phases built and test-verified.** `TestSatellitePull` grew from 13 → 24 →
> 30 → 33 tests across Phases 1, 2, 4; Phases 3, 5, and 6 each added tests to `TestAgri` instead (none
> touch the Sentinel client directly). Full `agri.tests` re-run after every phase: 94/96, 100/102, 101/103,
> 104/106, 106/107, 108/110 — the failures throughout are the same pre-existing, unrelated-to-this-work
> issues already documented in stage-9 §13 (Phase 5's run hit only 1 of the usual 2 — the flaky
> local-NodeODM test happened to pass that time, consistent with it being environmental — and both were
> back by Phase 6's run; every failure signature confirmed identical across all six runs, never a new
> regression). Three real bugs were found live-testing Phase 1's cloud-masking approach against the real
> Sentinel Hub API *before* they became silent data-quality bugs — see §3 Phase 1. Phase 4's broader
> indices worked cleanly on the first live run, no new bugs. Phase 5 found that its planned new endpoint
> was unnecessary — the Stage 8 `SeasonalView` already did the job, so it was extended by one field instead
> of duplicated. Phase 6 (anomaly detection) could only be built and fixture-tested, not live-verified — no
> real production history or Sentinel API surface is involved — and its two open product questions
> (threshold rule, where it surfaces) were asked of the user directly rather than guessed.

## 1. Why this document exists

Stage 9 ([stage-9-satellite-monitoring.md](stage-9-satellite-monitoring.md)) already gave Precise-Agric a
direct, live connection to Sentinel-2 via the Copernicus Data Space Ecosystem: on-demand imagery pulls,
a Sentinel-vs-our-own-analysis comparison feature, and full source/engine tagging so drone and satellite
data never collide in the seasonal graphs. Backend-verified with a real test run: 84/85 in `agri.tests`.

An externally-drafted architecture review (not written against this codebase — produced without reading
it) was brought back for evaluation, recommending a "multi-source observation platform" direction. This
document is that evaluation, plus the roadmap that came out of it. Its own closing recommendation —
*audit the existing implementation before rebuilding* — is correct, and this document is that audit.

Two decisions were locked before finalizing this roadmap:

- **Scope:** this pass is planning only. No code changes here.
- **Cloud-quality handling (Phase 1, below):** when a pulled scene is cloudy, **flag it and let it
  through** — store the quality metric, surface a warning, let a human (the agronomist) judge — rather
  than reject the pull outright. This matches the precedent Stage 9 already set for the comparison
  feature: show both numbers, don't auto-reconcile.

---

## 2. Skeptical review of the external proposal

The proposal's headline direction — *"Sentinel is another observation source feeding the same
farm/field/boundary/analysis workflow, not a separate system"* — is correct, and is already exactly how
Stage 9 was built: `Capture` (`app.Task`, reused) is shared regardless of source, gated per-analysis
rather than forked into a parallel pipeline, and the AgriTrack boundary stays authoritative over
everything imagery-derived.

Three factual corrections, found by reading the actual code rather than trusting the proposal's
assumptions about it:

### 2.1 "Rename `computed_by`, split into `processing_source` + `analysis_type`" — unnecessary

`AnalysisRun.computed_by` ([agri/models/analysis.py](../../agri/models/analysis.py)) already carries this
distinction, and its display label is already `"Precise-Agric (WebODM)"` vs `"Sentinel remote-sense
engine"` — the branding concern the proposal raises is already handled at the label level.
`AnalysisResult.kind` already *is* the "analysis_type" axis the proposal proposes reinventing. Renaming a
live, migrated, tested, frontend-consumed field for cosmetic clarity is pure churn with no functional
gain. **Rejected.**

### 2.2 "Add a `SatelliteAnalysis` model for NDVI/EVI/statistics/temporal metrics" — unnecessary right now

`AnalysisResult.stats` is already a `JSONField`; a second or third index just adds another key
(`{'NDVI': ..., 'EVI': ...}`) — no schema change required. A dedicated model is only justified once we
need to query *across* many satellite-metric rows relationally, which nothing currently needs.
**Rejected for now, not forever** — revisit if a real relational-query need shows up.

### 2.3 The capability matrix lists `plant_counting` and `plant_height` as drone-only capabilities — these don't exist

`agri/analysis/` has exactly six services: `plant_health`, `rgb_index`, `grid`, `canopy`, `weed`,
`report` (confirmed directly — `Glob agri/analysis/*.py` and a full read of
[agri/services.py](../../agri/services.py)). `plant_height` is an unrelated, separate WebODM core plugin
(`coreplugins/plant_height/`) with nothing to do with this fan-out; there is no plant-counting service at
all anywhere in this codebase. The proposal appears to have guessed at capabilities it never actually
saw. **Corrected in §4 below** — the real six-service list, not an invented eight-service one.

### 2.4 What's genuinely new and confirmed as real gaps

Checked directly against [agri/remote_sense/sentinel_client.py](../../agri/remote_sense/sentinel_client.py)
(read in full for this review):

- **No cloud/shadow masking anywhere.** `fetch_field_imagery` uses `MosaickingOrder.LEAST_CC` (picks the
  least-cloudy *scene*) but never inspects the SCL band or excludes cloudy *pixels within* the chosen
  scene. `fetch_field_statistics` uses `dataMask` (excludes only true no-data/off-AOI pixels, not
  clouds). This was already the #1 open gap flagged in the original Stage 9 doc; the proposal just
  re-confirms it's the right priority.
- **No scene-availability check.** The date-range picker in `season.html` blindly trusts
  `MosaickingOrder.LEAST_CC` with no visibility into what was actually available or how cloudy it was.
  Sentinel Hub's Catalog API (not used anywhere in this codebase today) would supply this.
- **`fetch_field_statistics` is hardcoded to NDVI** (`if index != 'NDVI': raise NotImplementedError`) —
  parametrizing the evalscript per index is a small, legitimate enhancement, just not urgent.
- **No richer capture metadata.** `CaptureMeta` only holds `capture_date` + `source` — no cloud
  percentage, product id, or resolution. Worth adding once cloud masking exists (so there's something
  real to store there).
- **The two existing gating decisions (weed off, canopy flagged) are ad hoc `if satellite:` branches**,
  not a formal table. Worth consolidating once a third case shows up — not before.

---

## 3. Recommended roadmap

Six phases, ordered by dependency and value. Phases 1–4 extend the existing Sentinel client/pull
machinery; Phase 5 is a real new frontend feature; Phase 6 is explicitly later-stage.

### Phase 1 — Cloud & quality awareness (highest priority) — ✅ BUILT (2026-08-15)

**Why:** an unmasked cloudy pixel produces a plausible-looking but meaningless index value — worse than a
gap, because nothing flags it as wrong. This is the one gap with real correctness risk.

**What was built:**
- `agri/remote_sense/sentinel_client.py`: `SCL_VALID_CLASSES`/`SCL_EXCLUDED_CLASSES` (Sen2Cor
  classification codes), `_valid_pixel_pct()` (pure function, unit-tested directly), `_fetch_scl()`,
  `fetch_field_imagery()` now returns `{'path', 'valid_pixel_pct'}` instead of a bare path,
  `fetch_field_statistics()` folds SCL-based cloud exclusion into its existing `dataMask` logic and
  returns `valid_pixel_pct` per point.
- `agri/models/capture_meta.py`: `valid_pixel_pct` (nullable `FloatField`) — migration `0006`.
- `agri/remote_sense/sentinel_pull.py`: `_field_geometry()` (new — see below), stores `valid_pixel_pct`
  on `CaptureMeta` post-import (best-effort — never undoes an already-successful pull), passes it through
  into `AnalysisResult.stats` for the comparison path.
- **Locked decision: flag, don't block** — implemented exactly as decided. A failed/low quality check
  never fails or discards a successful pull; it just means `valid_pixel_pct` is `None` or low.
- `PreciseAgricPanel.jsx`: `renderQualityBadge()` — ✓ *N%* clear (green) or ⚠ *N%* clear — low confidence
  (amber), 70% threshold, next to "Satellite Reference". `PreciseAgricPanel.scss`: `.quality-badge`.
- **Also folded in this pass** (tightly coupled to Phase 1's AOI resolution, and approved in the UX
  review as "both as options"): field-level targeting. `pull_satellite_capture()` accepts an optional
  `field_id`; `_field_geometry()` resolves a single Field's boundary (AgriTrack-synced boundary if
  linked, else its most recent drawn `Boundary`) instead of the whole-farm union.
  `agri/api/satellite.py`'s imagery endpoint accepts an optional `field` param.
  `agri/api/views.py`'s `FieldListView` now also accepts `?project=` (not just `?task=`), since the new
  entry point needs a field picker before any capture/task exists yet.
- **New UI entry point**: `coreplugins/precise_agric/public/main.js` — "Get Satellite Imagery" added as a
  sibling to "Upload Orthophoto" in the project's Import dropdown (same mental slot a user already looks
  in), opening a small modal (whole farm or a specific field, date range) rather than the season.html
  picker being the only way in.

**Three real bugs found by live-testing against the actual Copernicus/Sentinel Hub API before they
became silent data-quality bugs** (not catchable by code review or documentation alone):

1. **SCL only supports `units: "DN"`** — requesting it alongside `REFLECTANCE`-unit bands in the same
   input block is rejected outright by the server ("Band 'SCL' ... requested in unsupported units
   'REFLECTANCE'!"). Multi-input-block evalscripts (Sentinel Hub's mechanism for combining *different*
   collections) don't solve this either — tested, got "Dataset with id: 1 not found." Fix:
   `fetch_field_imagery()` fetches SCL via a *separate* request, relying on Sentinel Hub's scene
   selection (`LEAST_CC` over the same geometry/date range) being a property of the resolved product, not
   the requested bands.
2. **Hand-rolling the DN→reflectance formula is a trap.** Tried substituting
   `ρ = (DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE` (the textbook formula) for `units="REFLECTANCE"` so
   SCL and reflectance bands could share one request — live-tested side by side against the real,
   Sentinel-Hub-computed reflectance value, and it was off by a flat 0.1 across every pixel. The assumed
   `BOA_ADD_OFFSET=-1000` does not match what this endpoint's conversion actually does internally.
   **Never replicate Sentinel Hub's reflectance conversion locally — always request `REFLECTANCE` units
   and let the server do it.**
3. **Single-band Process API responses come back 2D** (`height, width`), not 3D
   (`height, width, bands`) like the existing multi-band imagery request. Assuming 3D unconditionally in
   `_fetch_scl()` raised `IndexError: too many indices for array` the first time it ran for real.

`fetch_field_statistics()` escaped all three traps: its evalscript never requested `units="REFLECTANCE"`
in the first place (NDVI is a scale-invariant ratio), so SCL-based cloud exclusion folded directly into
its existing `dataMask` logic with no separate request and no unit conflict — confirmed live, and its
`sampleCount`/`noDataCount` response fields (already present, previously unused) turned out to be exactly
the valid-pixel-percentage signal needed, for free.

**Files touched:** `agri/remote_sense/sentinel_client.py`, `agri/remote_sense/sentinel_pull.py`,
`agri/models/capture_meta.py` (+ migration `0006`), `agri/tasks.py`, `agri/api/satellite.py`,
`agri/api/views.py` (`FieldListView`), `coreplugins/precise_agric/public/PreciseAgricPanel.jsx`,
`coreplugins/precise_agric/public/PreciseAgricPanel.scss`, `coreplugins/precise_agric/public/main.js`,
`agri/tests.py` (11 new tests in `TestSatellitePull`, now 24; 2 new tests for `FieldListView`).

**Verification:** `_valid_pixel_pct()` unit-tested directly (pure function, real SCL class combinations,
no mocking). `fetch_field_imagery()`/`fetch_field_statistics()` live-tested end-to-end against the real
account after every fix, not just once at the start. Full orchestration layer
(`sentinel_pull.py`/`agri/api/satellite.py`/Celery tasks) tested with the network mocked, matching the
existing `TestSatellitePull` convention — real `./webodm.sh test backend agri.tests` run in `--dev` mode:
`TestSatellitePull` 24/24, full suite 94/96 (2 pre-existing, unrelated). `main.js`'s new modal is
**not** browser-tested — same gap already flagged for the rest of the Stage 9/10 frontend.

### Phase 2 — Scene availability / picker

**Why:** right now the date-range picker is a blind-trust exercise. Showing what's actually available
(and how cloudy) turns "get satellite imagery" into an informed choice instead of a guess.

**What was built:**
- `agri/remote_sense/sentinel_client.py`: `search_available_scenes(geom, date_from, date_to)` via
  Sentinel Hub's Catalog API (`SentinelHubCatalog`) — live-verified against the real account (13 real
  scenes found for the test AOI over 45 days). `_dedupe_scenes_by_date()` is a pure, separately
  unit-tested helper — collapses raw per-scene/per-tile results to one entry per calendar day (keeping
  the least-cloudy when two scenes share a day), sorted newest first.
- **Live-verified, not assumed:** unlike Process/Statistical API (which needed the CDSE-redefined
  collection to avoid 401s — see Phase 1's history), the Catalog API worked identically with the bare
  `DataCollection.SENTINEL2_L2A` and the redefined one. Used the redefined one anyway, for consistency
  with the rest of the module rather than relying on an unverified assumption holding for other accounts.
- `agri/api/satellite.py`: `SatelliteAvailabilityView` — `GET /api/agri/satellite/availability/
  ?project=&date_from=&date_to=[&field=]`, read-only and synchronous (a lookup this light doesn't need
  Celery/polling). Reuses `sentinel_pull.py`'s existing `_farm_geometry()`/`_field_geometry()` AOI
  resolution directly rather than duplicating it, so "available scenes" always reflects the *same* area
  the actual pull would use.
- **Wired into the Phase 1 modal, not `season.html`** (the roadmap originally assumed season.html was
  still the primary entry point — Phase 1 superseded that with the Import-menu modal, so Phase 2 extends
  *that* instead): an "Available scenes" list appears between the date range and the submit button,
  auto-refreshing on date/field change, defaulting to "Auto" (the original range-wide least-cloudy
  behaviour, unchanged). Picking a specific scene narrows the actual pull request to that exact day —
  no new backend parameter needed, since a single-day window deterministically resolves to that one scene.

**Files touched:** `agri/remote_sense/sentinel_client.py`, `agri/api/satellite.py`, `agri/api/urls.py`,
`coreplugins/precise_agric/public/main.js`, `agri/tests.py` (6 new tests in `TestSatellitePull`, now 30).

**Verification:** `_dedupe_scenes_by_date()` unit-tested directly (pure function — same-day collision
handling, missing-field skipping, empty input — no mocking). `search_available_scenes()` live-tested
end-to-end against the real account. Orchestration (`SatelliteAvailabilityView`, AOI resolution reuse,
validation) tested with the network mocked. Real `./webodm.sh test backend agri.tests` run in `--dev`
mode: `TestSatellitePull` 30/30, full suite 100/102 (the same 2 pre-existing, unrelated failures —
confirmed identical error signatures, zero new regressions). `main.js`'s scene list is **not**
browser-tested — same gap flagged for the rest of the Stage 9/10 frontend.

**Depends on:** nothing from Phase 1 functionally, but naturally follows it since both touch the same
client module and the same modal.

### Phase 3 — Capability-table cleanup — ✅ BUILT (2026-08-15)

**Why:** today's satellite gating is two hardcoded `if satellite:` branches in `agri/services.py` (weed
off, canopy flagged). Fine at two cases; worth formalizing *before* it grows to five ad-hoc branches.

**What was built:** a plain constant dict in `agri/services.py`, keyed by the real `AnalysisResult.kind`
constants rather than bare strings (so a typo or a renamed kind fails loudly, not silently) — not a
self-reporting plugin system, six fixed services don't need one:

```python
SATELLITE_ELIGIBLE = {
    AnalysisResult.PLANT_HEALTH: True,
    AnalysisResult.RGB_INDEX: True,
    AnalysisResult.GRID: True,
    AnalysisResult.CANOPY: 'proxy',
    AnalysisResult.WEED: False,
    AnalysisResult.REPORT: True,
}
```

`execute_analysis()`'s two inline `if satellite:` branches now read `SATELLITE_ELIGIBLE[...]` instead of
hardcoding the decision a second time. **No behavior change**, confirmed by the pre-existing Stage 9
tests (`test_weed_mapping_skipped_for_satellite_capture`,
`test_canopy_flagged_low_resolution_proxy_for_satellite`, `test_report_includes_capture_source`,
`test_run_analysis_orchestration`) passing unmodified — this was a refactor of the existing two decisions,
not a new one.

**New test:** `test_satellite_eligible_table_covers_all_analysis_kinds` — asserts the table's keys exactly
match `AnalysisResult.KIND_CHOICES` (so adding a 7th analysis service without an entry here fails a test,
not silently falls through) and pins the three non-trivial values (`WEED: False`, `CANOPY: 'proxy'`, the
rest `True`).

**Files touched:** `agri/services.py`, `agri/tests.py` (1 new test, `TestAgri` — this phase didn't touch
`TestSatellitePull`, since nothing here is Sentinel-client-specific).

**Verification:** no live API surface involved (pure in-process refactor), so no standalone
live-verification script needed this time — same judgment call as Stage 9's non-network code. Real
`./webodm.sh test backend agri.tests` run in `--dev` mode confirmed no regressions.

### Phase 4 — Broaden Sentinel's own indices beyond NDVI — ✅ BUILT (2026-08-15)

**Why:** `fetch_field_statistics(index='NDVI')` hard-failed on anything else; EVI (or others) is a small
evalscript-parametrization change, useful once the comparison feature sees real usage.

**What was built:**
- `agri/remote_sense/sentinel_client.py`: `SENTINEL_INDEX_DEFS` — a constant dict of 5 supported indices
  (`NDVI`, `GNDVI`, `NDRE`, `SAVI`, `EVI`), each with its Sentinel-2 band list and evalscript formula.
  NDVI/GNDVI/NDRE reuse Sentinel Hub's built-in `index()` normalized-difference helper; EVI/SAVI are
  written out directly. Formulas were chosen to **match this codebase's own drone-side algos**
  (`app/api/formulas.py`'s N/R/G/B/Re shorthand, mapped to Sentinel-2 B08/B04/B03/B02/B05) band-for-band,
  so a Sentinel value for a given index is actually comparable to our own analysis for that same index —
  not just a different, unrelated ratio. `_index_definition(index)` is a pure lookup function (raises
  `NotImplementedError` for anything outside the 5, same behavior as before, just table-driven now).
  `fetch_field_statistics()`'s evalscript now builds its `input.bands` list and formula from this table
  instead of hardcoding NDVI's `["B04", "B08", "SCL", "dataMask"]` / `index(samples.B08, samples.B04)`.
- `agri/remote_sense/sentinel_pull.py`: `pull_satellite_comparison(boundary_id, index='NDVI')` — the
  `index` param flows through to `fetch_field_statistics()`, `AnalysisRun.index_used`, and
  `AnalysisResult.stats['index']`. A `NotImplementedError` from an unsupported index is translated to
  `SatellitePullError`, matching how the other Sentinel-client exceptions are handled here.
- **Not wired to the API/Celery/frontend yet** — the locked Phase 4 scope (see plan file) only extends the
  two Python functions' caller surface; `SatelliteComparisonPullView` still calls
  `pull_satellite_comparison.delay(boundary.id)` with the implicit NDVI default, same as before. Wiring an
  index picker into the "Compare with Satellite" UI is real, scoped frontend work of its own and stays
  deferred until there's a concrete reason to pick anything other than NDVI (this phase was explicitly
  low-priority/build-ahead-of-demand in the original plan).

**Live-verified, not assumed:** all 5 indices were run against the real CDSE account for the same AOI/
date-range Phase 1 used — 6 points each, no evalscript errors. NDVI's mean matched Phase 1's original
live-test result almost exactly (~0.297 here vs. the 0.29–0.36 range recorded then), confirming the
evalscript restructuring introduced no regression; GNDVI ~0.44, NDRE ~0.18, SAVI ~0.18, EVI ~0.17 — all
plausible for the same vegetated AOI. No new bugs found this time; the parametrization worked cleanly on
the first live run (unlike Phase 1, where the equivalent first attempt hit a real 400).

**Files touched:** `agri/remote_sense/sentinel_client.py`, `agri/remote_sense/sentinel_pull.py`,
`agri/tests.py` (3 new tests in `TestSatellitePull`, now 33).

**Verification:** `_index_definition()` unit-tested directly (pure function — all 5 supported indices,
plus the unsupported-index error case — no mocking, no network). `pull_satellite_comparison()`'s new
`index` parameter and its `NotImplementedError`→`SatellitePullError` translation tested with the network
mocked, alongside re-running the 4 pre-existing comparison tests to confirm the NDVI-default path is
unchanged. Real `./webodm.sh test backend agri.tests` run in `--dev` mode: targeted comparison-path tests
7/7, full suite regression-checked (see status header for the run-by-run count).

### Phase 5 — Observation timeline (field-level, multi-source) — ✅ BUILT (2026-08-15)

**Why:** the genuinely good UX idea in the proposal — a field's full capture history (drone *and*
satellite, interleaved by date) in one view, not scattered per-boundary-per-capture as today.

**Placement decision:** the original plan mocked two options — a new field-scoped tab, or extending the
existing whole-farm season page. Asked the user directly rather than guessing; the answer was **extend
`season.html`**, with the timeline as a new section below the trend charts, field-selectable via a
dropdown (not a separate page/tab).

**Audit before building (same discipline as Phase 3's cleanup):** the plan called for "a new read
endpoint aggregating all Tasks for a Field across every Capture" — but `agri/api/seasonal.py`'s
`SeasonalView` (built in Stage 8) **already does almost exactly this**: per field, a `series` of points
across every finished `AnalysisRun`, each carrying `date`, `source` (drone/satellite), `computed_by`
(our-own/Sentinel), and the run's metrics, already keyed to never silently blend a same-day drone +
satellite pair. Building a second, parallel aggregation endpoint would have duplicated that logic and
risked the two disagreeing over time. The only real gap: `valid_pixel_pct` (Stage 10 Phase 1's SCL-derived
quality metric) was captured on `CaptureMeta` but never surfaced in `SeasonalView`'s points. **No new
endpoint was built — the existing one was extended by one field.**

**What was built:**
- `agri/api/seasonal.py`: `_capture_valid_pixel_pct(task)` (mirrors the existing `_capture_source()`
  pattern) — each point now also carries `valid_pixel_pct` (`None` for drone, the real value for
  satellite). Not added to `NUMERIC_METRICS`/the whole-farm average — averaging a quality percentage
  across fields isn't a meaningful farm-level number; the flag matters at the individual-point level the
  timeline reads it at.
- `coreplugins/precise_agric/templates/season.html`: a new "Observation Timeline" section — a field
  `<select>` (populated from the same `/api/agri/seasonal/` response the charts already fetch, no second
  request) and a scrollable, newest-first list of that field's capture events. Each row: date, a
  🚁/🛰️ source icon, the headline metric (`index` + `health_mean` when present, falling back to canopy %
  or weed count), which engine computed it (our own vs. Sentinel's, from `computed_by`), and — for
  satellite points only — the same ✓/⚠ quality badge convention `PreciseAgricPanel.jsx` already uses
  (≥70% valid pixels = good), so a cloudy pull is visibly flagged right in the history, not just on the
  comparison card.

**Files touched:** `agri/api/seasonal.py`, `coreplugins/precise_agric/templates/season.html`,
`agri/tests.py` (1 new test in `TestAgri`).

**Verification:** `test_seasonal_points_include_valid_pixel_pct_for_satellite` (new) plus the 3
pre-existing seasonal tests re-run to confirm no regression to the existing chart data shape. Real
`./webodm.sh test backend agri.tests` run in `--dev` mode: 106/107 — the flaky local-NodeODM test
happened to pass this run (environmental, not new), and the one remaining failure is the same
pre-existing, unrelated `test_resolve_agri_field_falls_back_to_persistent_field_link` issue documented
since stage-9 §13. `season.html`'s new JS was syntax-checked (template tags neutralized, `node --check` on
the extracted script) but **not exercised in an actual browser** — same gap already flagged for the rest
of the Stage 9/10 frontend.

### Phase 6 — Historical baselines & anomaly-driven drone dispatch — ✅ BUILT (2026-08-15)

**Why:** matches this project's own pre-existing ambition in
[stage-9-satellite-monitoring.md §10](stage-9-satellite-monitoring.md) ("Stage D": per-field historical
baselines, anomaly scoring → drone-dispatch triage). The external proposal converges on the same idea
independently — a good sign, not new information.

**Different from Phases 1–5:** this phase can only be *built and fixture-tested* today, not live-verified —
there is no real accumulated production history yet for the anomaly rule to run against, and no Sentinel
Hub API surface involved at all (this is pure computation over already-stored `AnalysisResult` stats, not
a new external call). Two product decisions had no right answer without that history to tune against, so
they were asked of the user directly rather than guessed:

- **Anomaly rule: a fixed percentage-drop threshold**, not a statistical z-score. A z-score needs enough
  points per field for a standard deviation to mean anything — fragile with the sparse data that actually
  exists this early — while a fixed threshold is simple, explainable to a farmer, and works from the third
  data point on.
- **Surface: a badge on the Phase 5 Observation Timeline**, not a new dashboard-level alert list — reuses
  the surface just built rather than opening a second one.

**What was built:**
- `agri/api/seasonal.py`: `ANOMALY_METRIC = 'health_mean'`, `ANOMALY_DROP_THRESHOLD_PCT = 15.0`,
  `MIN_BASELINE_POINTS = 2`. `_anomaly_for_latest(chronological_points, metric)` — a pure function that
  checks whether the last point in a list dropped ≥15% below the trailing average of everything before
  it (only ever flags drops, never rises; needs ≥2 prior points; guards against a zero trailing average).
  `compute_field_anomalies(series)` — groups a field's full series strictly by `(source, computed_by)`
  before comparing, so a drone ExG value is never compared against a Sentinel NDVI value (same "never
  blend" rule this module already applies to the farm-level average). `SeasonalView.get()` now attaches
  `anomaly` (`None`, or `{trailing_avg, deviation_pct}`) to every point.
- `coreplugins/precise_agric/templates/season.html`: each Observation Timeline row that carries an
  `anomaly` now shows a second line — "⚠ N% below this field's own trailing average (was X) — consider a
  drone follow-up" — styled as a warning bar directly under that row.

**Files touched:** `agri/api/seasonal.py`, `coreplugins/precise_agric/templates/season.html`,
`agri/tests.py` (3 new tests in `TestAgri`).

**Verification:** `_anomaly_for_latest()` and `compute_field_anomalies()` unit-tested directly against
plain dicts — no DB, no network, no fixtures beyond hand-built lists (insufficient history, a dip under
threshold, a rise, a real drop, a missing value, a zero trailing average; and cross-source-group isolation
specifically). One end-to-end test constructs 3 real `AnalysisRun`/`AnalysisResult` rows with controlled
`stats={'mean': ...}` across 3 dates for one field and confirms the real `/api/agri/seasonal/` response
flags only the third point. Real `./webodm.sh test backend agri.tests` run in `--dev` mode: 108/110 — the
same two pre-existing, unrelated failures as the original stage-9 baseline (both present this run, unlike
Phase 5's run where one happened to pass). `season.html`'s new anomaly-badge JS was syntax-checked the same way as
Phase 5's but **not exercised in an actual browser** — same gap flagged for the rest of the Stage 9/10
frontend, and here doubly honest: even if it rendered correctly, there's no real data yet to see it fire
against in practice.

**Not done (explicitly, matching the roadmap's own original caveat):** tuning `ANOMALY_DROP_THRESHOLD_PCT`
against real farmer feedback, and the drone-*dispatch* half of "anomaly-driven drone dispatch" (this phase
flags and surfaces; it doesn't create or suggest a specific drone task) — both need real usage this
codebase doesn't have yet.

---

## 4. Explicitly rejected (do not build)

- Renaming `AnalysisRun.computed_by` or adding a parallel `processing_source`/`analysis_type` axis —
  redundant with `computed_by` + `AnalysisResult.kind`.
- A new `SatelliteAnalysis` model — redundant with `AnalysisResult.stats` (JSONField) until proven
  otherwise.
- Renaming/forking `create_capture_from_orthophoto()` into `create_capture_from_satellite()` — no
  behavior difference to justify a second entry point; satellite-specific metadata is added *alongside*
  the existing shared call (Phase 1), not by forking it.
- A self-reporting "analysis service capability" plugin system — six fixed services don't need dynamic
  capability discovery; a constant dict (Phase 3) is sufficient.
- Gating `plant_counting`/`plant_height` for satellite — these analyses don't exist in this codebase's
  fan-out; nothing to gate.
- Automatically blocking cloudy pulls — locked decision is flag-and-let-through, not reject (§3, Phase 1).

---

## 5. Related documents

| Doc | Relationship |
|---|---|
| [stage-9-satellite-monitoring.md](stage-9-satellite-monitoring.md) | The implementation this roadmap extends — read it first for the full existing design |
| [testing.md](../testing.md) | Test inventory; each phase above gets its own entry here when implemented |
| [progress-log.md](../progress-log.md) | Dated build log; each phase gets its own entry here when implemented |
