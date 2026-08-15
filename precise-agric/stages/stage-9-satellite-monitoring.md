# Stage 9 — Sentinel Satellite Monitoring

Using Sentinel-2 imagery to monitor farms that are **too remote or too costly to reach by drone**, and
to enable comparison over time and between fields.

> **Status (2026-08-06): Stages A/F/G built and BACKEND-VERIFIED against a real test run.**
> `CaptureMeta.source` and `AnalysisRun.computed_by` exist, the seasonal API keys points by
> `(date, source, computed_by)`, weed detection is gated off and canopy flagged for satellite captures,
> and remote-sense ingest tags `SATELLITE` (Stage A).
>
> `agri/remote_sense/sentinel_client.py` (§7) was live-tested against a **real** Copernicus Data Space
> Ecosystem account: both the Process API (imagery) and Statistical API (Sentinel's own NDVI) succeeded
> against a real AOI, returning real, plausible data. One real bug was found and fixed — see §7.
>
> **Stages F/G (the "Get from Sentinel" flow)** are implemented end to end —
> `agri/remote_sense/sentinel_pull.py` (AOI resolution from AgriTrack onboarding + orchestration),
> `agri/tasks.py` (`pull_satellite_capture`, `pull_satellite_comparison` Celery tasks),
> `agri/api/satellite.py` (`POST /api/agri/satellite/imagery/`, `POST /api/agri/satellite/compare/`), a
> branded farm-level date-range picker in the Season Progress page
> (`coreplugins/precise_agric/templates/season.html`), and a per-boundary "Compare with Satellite" action
> + side-by-side result display in `PreciseAgricPanel.jsx`. All Sentinel/Copernicus naming is kept out of
> the UI (labelled "Satellite Imagery" / "Satellite Reference" throughout), per the product decision to
> keep this a Precise Agric capability, not a visibly third-party one.
>
> **`./webodm.sh test backend agri.tests` was actually run** (not just compiled) in a `--dev`-mode
> container against a real Postgres + Celery-eager test run. `TestSatellitePull`: **13/13 pass**. Full
> `agri.tests` suite: **84/85 pass** — the one failure
> (`test_resolve_agri_field_falls_back_to_persistent_field_link`) is a **pre-existing bug unrelated to
> this work**, confirmed by running it in isolation (fails the same way with zero satellite code present)
> and by reading its setup helper (`_agri_linked_run`): AgriTrack sync already auto-creates the
> persistent `Field` link for a synced `AgriField`, and the test's own manual
> `Field.objects.create(..., agri_field=agri_field)` collides with it. Not touched or introduced here.
> Three real bugs were found and fixed by actually running the tests (not from code review alone): a
> `DataCollection` service-URL override in the Sentinel client (§7), a stray leftover line from an
> earlier edit causing a `NameError` in an unrelated test, and the new Celery tasks not following this
> codebase's own `TestSafeAsyncResult` pattern for eager-mode result polling.
>
> **Not done:** the frontend (season.html date picker, PreciseAgricPanel.jsx comparison UI) has **not
> been exercised in an actual browser** — implemented and reasoned through against existing conventions,
> not click-tested. See §13 for the full file-by-file status and remaining gaps.

---

## 1. The core reframe: satellite is a **scout**, not a drone replacement

The motivating idea — "use satellite where we can't fly a drone, to save transport cost" — is sound as a
business goal but wrong as a technical substitution. Sentinel-2 cannot produce what a drone capture
produces. It can, however, answer a question drones never can: **"which of my farms needs attention this
week?"**

```
Sentinel (every ~5 days, free, coarse)
    → whole-field index + trend vs that field's own history
    → flags anomalies:  "Field 12 is well below its own seasonal norm"
             │
             ▼
    Drone dispatched ONLY to flagged fields
    → weed maps, canopy detail, zone-level prescriptions
```

This saves **more** than blind substitution: routine flights over healthy fields stop, and problem fields
are never missed because satellite watches all of them continuously. It also dissolves the resolution
problem — we never ask Sentinel for a weed map, only "does this field deviate from its own baseline",
which is well within its ability.

**Locked framing:** satellite captures are a *monitoring tier*, not equivalent captures. They must never
silently stand in for drone data.

---

## 2. The constraint everything else follows from

Sentinel-2 is **10 m/pixel** (bands B02/B03/B04/B08). A drone orthophoto is ~**5 cm/pixel**.

| Source | Pixels over a 1 ha field |
|---|---|
| Drone @ 5 cm | ~4,000,000 |
| Sentinel @ 10 m | **~100** |

One Sentinel pixel covers **100 m²**. Every downstream limitation in this document is a consequence of
that single fact.

---

## 3. Field-size tiers (because we serve all farm sizes)

Farmers onboard via AgriTrack and draw the areas they want analyzed — anything from one sub-hectare plot
to many large fields. Field size therefore cannot be assumed; it must be **measured per field and used to
decide what analysis is honest to run**.

Usable pixels ≈ field area ÷ 100 m², minus a one-pixel boundary ring discarded as mixed
(part crop, part road/neighbour):

| Field area | Total px | Usable px after erosion | Tier | What satellite may report |
|---|---|---|---|---|
| < 0.5 ha | < 50 | **< ~25** | **T0** | ❌ No per-field metrics. Farm-level aggregate only. |
| 0.5 – 2 ha | 50–200 | ~25–150 | **T1** | Whole-field **mean index + trend only**, flagged low-confidence |
| 2 – 5 ha | 200–500 | ~150–400 | **T2** | Mean + trend + **coarse zones** (cells ≥ 30 m) |
| > 5 ha | > 500 | > 400 | **T3** | Full satellite set, incl. meaningful zone maps |

**Weed mapping is never available from satellite, at any tier** (§4).

**Confirmed, not hypothetical:** real farmer fields on this platform go down to **0.1 ha** (1,000 m² —
about **10 raw Sentinel pixels, ~4–6 usable** after edge erosion). T0 is the common case for
smallholders, not an edge case to design around later.

### The uncomfortable tension — state it plainly

The farmers who can *least* afford drone flights (smallholders) have the *smallest* fields, where
satellite works *worst*. Satellite does not fully solve the cost problem for T0 farms. What T0/T1 farms
can still get honestly:

- **Farm-level** aggregate index and trend (several small fields combined may reach a usable pixel count)
- **Change detection over time** — a field's own trajectory is more robust than its absolute value
- **Historical baseline** from the Sentinel archive (§6), which no drone can retroactively provide
- **Regional context** — how this farm compares to its surroundings on the same sensor

What they must **not** be given is a precise-looking per-field number derived from 9 mixed pixels
presented as equivalent to a drone measurement.

---

## 4. What breaks in the existing analysis pipeline

| Module | Behaviour at 10 m | Verdict |
|---|---|---|
| [plant_health.py](../../agri/analysis/plant_health.py) → `health_mean` | Whole-field mean index | ✅ **Works — satellite's sweet spot** |
| [canopy.py](../../agri/analysis/canopy.py) | Every pixel mixes canopy + soil; the 0.25 threshold saturates rather than tracking true cover | ⚠️ Proxy only; relabel, don't present as measured cover |
| [grid.py](../../agri/analysis/grid.py) | `cell_size_m=10.0` ÷ 10 m pixel → `cell_px = 1`: each "zone" is **one raw pixel**, no averaging | ⚠️ Degenerate — enforce a minimum cell of ≥ 3 px (30 m), T2+ only |
| [weed.py](../../agri/analysis/weed.py) | `min_blob_px=10` → smallest detectable "weed patch" = **1,000 m² = 0.1 ha** | ❌ **Must be disabled for satellite** |
| [rgb_index.py](../../agri/analysis/rgb_index.py) | ExG/VARI/GLI compute fine, but are coarse proxies | ⚠️ Allowed, labelled |

**Weed detection is the dangerous one.** It will not crash. It will emit a `weed_count` from NGRDI
thresholding that actually reflects *sparse or stressed vegetation*, and that fabricated number flows
into the seasonal graph and is **pushed to farmers through AgriTrack**. That is manufacturing false
agronomic data, not a degraded feature. It must be gated off by capture source, not left to a threshold.

---

## 5. Data-integrity defect this feature would introduce

**This is the blocking item.** [`CaptureMeta`](../../agri/models/capture_meta.py) has **no source field** —
nothing distinguishes a drone capture from a satellite one. Combined with
[seasonal.py](../../agri/api/seasonal.py):

```python
entry['points'][date_str] = point   # later run on the same date OVERWRITES the earlier one
```

Two silent failures:

1. **Same-date collision** — a drone and a satellite capture on one date: one arbitrarily overwrites the
   other. The farmer sees whichever landed last.
2. **Mixed-sensor trend lines** — drone NDVI (sharp pixels, high values) and satellite NDVI (mixed
   pixels, lower values) plot on the **same line**. The farmer sees a crop-health "crash" that is purely
   an artifact of switching sensors.

This is not a calibration bug that better processing fixes. NDVI is a **non-linear ratio**, so
mean(NDVI at 5 cm) ≠ NDVI(mean reflectance at 10 m). Drone and satellite NDVI for the same field on the
same day legitimately differ. They can share a chart only if they are **separate, labelled series**.

**Required before any satellite capture reaches the seasonal graph:**
- `CaptureMeta.source` ∈ {`DRONE`, `SATELLITE`} (+ migration, existing rows default `DRONE`)
- Seasonal points keyed by `(date, source)`, returned as distinct source-tagged series
- Analysis gating driven by `source` (weed off, canopy relabelled, grid cell floor)
- **A second, independent tag** on the analysis result itself — see §5.2 — because §5.2 means the same
  field/date can now carry *two* numbers from *two different engines*, not just two image sources.

### 5.1 User-facing: choosing the source (locked requirement)

When a farm has more than one usable capture — e.g. a drone flight and a satellite pass, possibly on
different dates — **staff must be able to pick which one to view or analyze**, not have the system choose
silently. Concretely:

- The map/capture UI ([stage-6](stage-6-frontend-map-ui.md)) lists available captures per farm/field with
  a visible **source badge** (Drone / Satellite), so staff can tell them apart before selecting one.
- The analysis screen works the same way it already does — analysis runs against a chosen capture
  (`Task`); this only requires *showing* the source clearly and letting staff filter/pick by it. No new
  backend concept is needed beyond the `source` tag in §5 — this is primarily a frontend change once
  that tag exists.
- Every result screen (analysis review, seasonal graph, exported report) must **carry the source label
  through**, so nothing downstream ever presents a satellite-derived number as if it were a drone
  measurement, or vice versa.

### 5.2 A second tag: who calculated the number, not just where the picture came from

§5.1 is about the **image**. This is about the **analysis** — and it's a separate axis, made necessary by
§9 below: Sentinel doesn't only send us pictures. It can also send us **its own already-computed
metrics** (its own NDVI/health numbers, computed with its own engine, on its own side) — entirely
independent of whether we *also* compute a number from a satellite image ourselves.

So the same field, on the same date, can now legitimately hold **two numbers from two different
engines**:

| | Computed by WebODM (this system) | Computed by Sentinel (their engine) |
|---|---|---|
| From a drone image | ✅ normal case today | — (they never see drone imagery) |
| From a satellite image | ✅ possible (§9) | ✅ possible (§9) |

That is not a bug to prevent — per your direction, it's a **feature to support**: seeing our number next
to their number for the same field/date is a built-in sanity check. But it means results must carry
**two labels, not one**: *image source* (Drone/Satellite) **and** *analysis engine*
(WebODM/Sentinel) — and the two must never be silently merged or averaged together. See §9 for how this
is ingested and compared.

---

## 6. Cloud cover — plan around it, don't discover it later

Zimbabwe's growing season (Nov–April) **is** the rainy season. Sentinel-2's nominal 5-day revisit
degrades under persistent cloud precisely during the critical growth window.

- **Do not** promise farmers a fixed cadence until measured. Sample the archive over real farm AOIs and
  compute actual cloud-free frequency by month.
- **Cloud masking is mandatory** — an unmasked cloudy pixel produces a plausible-looking but meaningless
  index value, which is worse than a gap.
- Masking uses Sentinel's **SCL** (scene classification) band, which the current formula engine cannot
  handle: its band vocabulary is `red/green/blue/nir/rededge` only
  ([app/api/formulas.py](../../app/api/formulas.py)). SCL handling is **custom work**, not configuration.
- Trend logic must be **gap-tolerant** — irregular, sometimes long intervals are the norm, not an error.

**Upside to bank:** the Sentinel-2 archive (reliable L2A from ~2017) allows building a **multi-year
baseline for a field that has never been flown**. This is what makes anomaly detection meaningful in a
farm's first season, and it is impossible with drones.

---

## 7. How the on-demand request reaches Sentinel — RESOLVED: direct integration, no partner system

**Superseded (2026-07-10):** there is no external "remote-sense" team/app. This system connects
**directly** to Sentinel's real public data service — the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/) (CDSE), which bundles the
Sentinel Hub Process API (imagery) and Statistical API (pre-computed zonal stats — this **is** "Sentinel's
own analysis engine" from §9, not a custom partner pipeline). Free account, OAuth client credentials
(Client ID/Secret) generated in their Dashboard. Client implemented in
[agri/remote_sense/sentinel_client.py](../../agri/remote_sense/sentinel_client.py) using the official
`sentinelhub` Python package (added to `requirements.txt`).

This simplifies two things that were open questions when a partner team was assumed:
- **Reflectance vs raw DN** (§9.4): request Sentinel-2 L2A with `units="REFLECTANCE"` directly — Sentinel
  Hub returns the atmospherically-corrected value, no manual correction needed on our side.
- **Sentinel's own analysis** (§9.1): the Statistical API computes NDVI (or other indices) **per polygon**
  server-side and returns it directly — no custom inbound endpoint/schema needed, because *we* are the
  one requesting it, for a field boundary we already have on file.

The old `/api/v1/remote-sense/push` inbound endpoint is **not removed** — it still works as a manual
upload path (e.g. a `.tif` obtained by hand) — but it is no longer the primary path.

§8.1 / §9 locked the model: nothing arrives from Sentinel unless a staff member requests it in our UI.

```
Staff clicks "Get satellite [imagery | analysis]" for a field in our UI
        │
        ▼
precise-agric calls OUT to Copernicus Data Space Ecosystem (Sentinel Hub APIs)
  via agri/remote_sense/sentinel_client.py, using this server's own OAuth
  client credentials (WO_SENTINEL_CLIENT_ID / WO_SENTINEL_CLIENT_SECRET)
        │
        ├── fetch_field_imagery()    -> Process API -> tagged multi-band GeoTIFF
        │       -> agri.capture.create_capture_from_orthophoto(source=SATELLITE)
        │
        └── fetch_field_statistics() -> Statistical API -> Sentinel's own NDVI
                per date, no image download
                -> lands as computed_by=SENTINEL (§9 inbound path, still to build)
```

**Live-verified (2026-08-06)** against a real CDSE account (a free account was created; OAuth client
credentials generated and put in `.env` as `WO_SENTINEL_CLIENT_ID`/`WO_SENTINEL_CLIENT_SECRET`):

- **Statistical API** (`fetch_field_statistics`): returned 13 real NDVI points over a 45-day window for a
  test AOI near Norton, Zimbabwe (mean ~0.29–0.36 — plausible cropland values). The response-parsing logic
  worked correctly against the live payload with no changes needed.
- **Process API** (`fetch_field_imagery`): returned a real 4-band image; pixel values landed correctly in
  the ~0–1 reflectance range, confirming `units="REFLECTANCE"` works as intended.

**One real bug found and fixed by this testing:** the built-in `DataCollection.SENTINEL2_L2A` carries its
own default `service_url` (the classic `services.sentinel-hub.com` host), which **overrides**
`config.sh_base_url` and made every request 401 against CDSE — even though the config was set exactly per
Sentinel Hub's own documentation. Fixed by redefining the collection explicitly
(`DataCollection.SENTINEL2_L2A.define_from('cdse_s2l2a', service_url=SH_BASE_URL)`), matching the pattern
CDSE's own example notebooks use. This is the kind of thing that only surfaces by actually running the
code — the pre-test version looked correct against every piece of documentation checked.

Not yet built: the UI button, the endpoint/task that calls these functions, and the `computed_by=SENTINEL`
`AnalysisRun` creation path for `fetch_field_statistics()`'s output (§9 Stage G).

**Operational note:** the client secret used for this test was pasted into chat by the operator before
this verification; it should be rotated in the CDSE dashboard after this testing session.

---

## 8. Cadence exposes two scaling problems

Satellite delivers on a fixed orbital schedule, not when a technician books a flight. That changes the
operating profile of the whole system.

### 8.1 The review queue will not survive it — RESOLVED: pull, not push

**Decided:** satellite imagery/analysis is **not** delivered on Sentinel's own automatic schedule. It is
requested **on demand**, by a staff member, through this system's UI — "connect the Sentinel app through
endpoints... embed it in this system, so if a user clicks, [it] can pull images or analyzed data/results
from Sentinel." The manual approval gate stays exactly as it is for every capture, drone or satellite.

This resolves the original worry cleanly: the review-queue flood only existed under an *automatic push*
model (Sentinel delivering on its own 5-day cadence, for every farm, whether requested or not). Under a
*pull* model, nothing ever arrives unless a person asked for it, so volume is naturally bounded by human
demand — there is no backlog to design around. See §9 for how the pull is wired.

### 8.2 Storage growth is unbounded

Every pass creates a Task with assets, indefinitely, for every farm. Needs an explicit retention policy
distinct from drone captures (which are rare, expensive, and worth keeping forever):

- Keep derived **statistics** permanently — they are small and are what the trends are built from
- Consider pruning **raster assets** for satellite captures after a retention window
- Store stacked bands as scaled `uint16` rather than `float32` (~half the size) unless float is needed
- Check the interaction with WebODM's existing quota/cleanup jobs so satellite volume does not trigger
  deletion of a farmer's drone history

---

## 9. Two engines, deliberately: ingesting Sentinel's own analysis for comparison

**Locked direction (was an open question — resolved):** this is not "who computes the indices, us or
them" as an either/or. We want **both**, on purpose — our own number computed from the image, and
Sentinel's own number computed by their engine, sitting side by side for the same field/date. Agreement
builds confidence; disagreement is an early warning (bad image, a bug, a modelling difference) worth
catching before a farmer sees either number.

### 9.1 This is a new, separate delivery, not a bigger image push

Today's inbound contract ([remote-sense-integration.md](../remote-sense-integration.md)) delivers
**imagery** — `POST /api/v1/remote-sense/push`, farm_id + a `.tif`. Sentinel's own analysis is **not** an
image; it is a small set of already-computed numbers (e.g. their NDVI mean, a health score) for a
farm/field/date. That needs its **own** inbound path, e.g.:

```
POST /api/v1/remote-sense/analysis          (new — plan only, not built)
  farm_id, field reference (or "whole farm"), capture_date,
  engine label ("sentinel"), metric name(s) + value(s)
```

Kept deliberately separate from the imagery push: a farm can receive an image without their analysis (or
vice versa), and the two shouldn't be forced into lock-step by a single payload shape.

### 9.2 Where it lands: reuse `AnalysisRun`/`AnalysisResult`, don't invent a parallel model

Per this project's own locked principle — *"reuse `Project`/`Task`, add only gaps"*
([README.md](../README.md)) — the natural home for an externally-computed metric is the existing
`AnalysisRun`/`AnalysisResult` pair, not a new table:

- Add an **engine tag** (e.g. `computed_by` ∈ `WEBODM` / `SENTINEL`) to `AnalysisRun`.
- A Sentinel-originated analysis skips the local fan-out entirely (no image processing happens here) —
  the run is created directly from their submitted numbers, tagged `computed_by=SENTINEL`, linked to the
  same `Field` + `capture_date` a same-day WebODM run would use.
- This means the *existing* seasonal-progress machinery ([seasonal.py](../../agri/api/seasonal.py))
  already knows how to walk `AnalysisRun`s per field over time — it mainly needs to **group by
  `computed_by` into separate series** instead of the current one-line-per-field assumption (which §5
  already requires for the drone/satellite split, so this extends the same fix rather than adding a new
  one).

### 9.2a Divergence handling — RESOLVED: display only, no automatic logic

**Decided:** when WebODM's and Sentinel's numbers disagree noticeably, the system does nothing clever —
it just shows both, clearly labelled, and it's the agronomist's job to investigate, correct, or write it
up. No auto-flagging, scoring, or alerting is being built for this. Revisit only if real usage shows it's
needed.

### 9.3 The comparison view

A field's seasonal chart should be able to show, for the same metric (say NDVI) across dates: a
**WebODM line** and a **Sentinel line**, clearly labelled — not merged, not averaged. A simple first cut:
extend the seasonal API response with per-point `computed_by`, and let the frontend render one series per
engine. A later refinement could compute an agreement/divergence score per date, but that's not needed to
ship the basic side-by-side view.

### 9.4 The reflectance problem from before still matters — for our own numbers only

Nothing above removes the earlier math concern; it just narrows where it applies. When **we** compute a
number from a satellite image (the WebODM-side line above), the input must be **reflectance**, not raw
digital numbers (DN) — otherwise our own line is internally wrong and stops being comparable across
dates, regardless of what Sentinel's line shows:

$$\text{NDVI}_\rho=\frac{DN_N-DN_R}{DN_N+DN_R+2\cdot\text{offset}} \;\ne\; \text{NDVI}_{DN}=\frac{DN_N-DN_R}{DN_N+DN_R}$$

(ρ = (DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE, read per scene — the additive offset does not cancel
in the ratio.) Verify by inspecting pixel ranges: ~0.0–1.0 = reflectance; ~0–10000 (or a −1000 offset) =
raw DN. This only affects the WebODM-computed line — Sentinel's own line (§9.1) is already correct on
their side by construction, since it's their engine's output, not ours.

---

## 10. Staged plan

| Stage | Work | Depends on |
|---|---|---|
| **A — Data integrity** *(blocks everything)* | `CaptureMeta.source` + migration; `AnalysisRun.computed_by`; seasonal series keyed by `(date, source)` / `(date, computed_by)`; analysis gating by source (weed **off**, canopy relabelled, grid cell floor) | — |
| **B — Ingest** | Band stacking → tagged multi-band COG (built, verified, not wired); set `source=SATELLITE`; resolution-aware `grid.py`; field-size tier computed per field | A |
| **C — Satellite correctness** | SCL cloud masking; negative-buffer boundary erosion; usable-pixel-count guard driving the T0–T3 tiers and confidence labelling | B |
| **F — Source selection UI + on-demand pull button** | Capture list shows a Drone/Satellite badge; staff pick which capture to view/analyze (§5.1); "Get from Sentinel" UI action calling the internal `agri/remote_sense/client.py` service boundary (§7) | A |
| **G — Comparative analysis** | New `POST /api/v1/remote-sense/analysis` inbound path; `AnalysisRun.computed_by=SENTINEL` runs created directly from submitted numbers (no local fan-out); seasonal API returns per-engine series; frontend renders WebODM vs Sentinel lines side by side (§9) | A |
| **D — The actual product** | Per-field historical baselines from the archive; anomaly scoring; "fields needing attention" triage list driving drone dispatch | C + §9.4 resolved |
| **H — Live-verify + wire the UI/task** | Smoke-test `sentinel_client.py` against a real CDSE account; build the "Get from Sentinel" button, the endpoint/Celery task that calls it, and the `computed_by=SENTINEL` ingestion for statistics (§7, §9) | F + a CDSE account (see §7) |

Stage A is small and worth doing regardless — it prevents satellite data corrupting existing drone
trends, and nothing else is safe to ship without it.

---

## 11. Open questions to settle before building

**Resolved (2026-07-10):** field sizes down to 0.1 ha are real and common (§3) — per-field satellite
metrics must carry confidence tiers, not be assumed reliable. No auto-approval; on-demand pull instead
(§8.1). Divergence between engines is shown, not auto-handled (§9.2a).

**Also resolved (2026-07-10):** no partner "remote-sense" system exists; this server connects directly to
Copernicus Data Space Ecosystem / Sentinel Hub (§7). Reflectance is requested directly from Sentinel Hub
(`units="REFLECTANCE"`, L2A), so §9.4's manual-correction concern doesn't apply to this path. "Sentinel's
own analysis payload" (§9.1) is the Statistical API's response shape — a documented, fixed contract, not
something to negotiate with anyone.

**Still open:**

1. **A CDSE account + OAuth credentials must actually be created and put in `.env`** (§7) — blocks live
   testing of `sentinel_client.py` and all of Stage H.
2. **`sentinel_client.py` needs a live smoke-test** — written against documentation, never run against a
   real account; the Statistical API response parsing in particular should be checked against a live
   payload.
3. **Measured cloud-free frequency** by month over real AOIs — sets honest expectations for what a pull
   returns.
4. **Retention policy** (§8.2) — how long satellite rasters are kept.

---

## 12. Related documents

| Doc | Relationship |
|---|---|
| [remote-sense-integration.md](../remote-sense-integration.md) | The transport layer this builds on — endpoint, auth, capture import |
| [stage-7-agritrack-integration.md](stage-7-agritrack-integration.md) | Farm/field sync that supplies the authoritative boundaries |
| [stage-8-interactive-map-and-seasonal.md](stage-8-interactive-map-and-seasonal.md) | The seasonal graphs §5 protects from mixed-sensor corruption |
| [architecture-and-plan.md](../architecture-and-plan.md) | Overall architecture and locked decisions |

---

## 13. File-by-file status (2026-08-06)

The on-demand "Get from Sentinel" flow (Stages A, F, G) is implemented and **backend-verified with a real
test run** (`./webodm.sh test backend agri.tests`, `--dev` mode, real Postgres, Celery eager):
`TestSatellitePull` 13/13, full `agri.tests` suite 84/85 (the one failure is pre-existing and unrelated —
see the status block above). Not yet done: Stage H's browser click-through, and Stages B–E/D (cloud
masking, historical baselines, retention, tiering).

| File | Role | Status |
|---|---|---|
| `agri/models/capture_meta.py` | `CaptureMeta.source` | ✅ implemented, tested |
| `agri/models/analysis.py` | `AnalysisRun.computed_by` | ✅ implemented, tested |
| `agri/migrations/0005_capture_source_and_computed_by.py` | migration for both | ✅ applied cleanly in real test run |
| `agri/capture.py` | `create_capture_from_orthophoto(..., source=)` | ✅ tested |
| `agri/services.py` | weed gating + canopy proxy flag for satellite | ✅ tested |
| `agri/analysis/report.py` | `capture_source` in report summary | ✅ tested |
| `agri/analysis/grid.py` | 3px cell-size floor | ✅ (general fix, not satellite-specific) |
| `agri/api/seasonal.py` | `(date, source, computed_by)`-keyed points | ✅ tested |
| `agri/remote_sense/bands.py` | manual multi-band-file stacking (fallback path) | ✅ tested (standalone + suite) |
| `agri/remote_sense/sentinel_client.py` | direct CDSE/Sentinel Hub client | ✅ **live-verified against a real account** (bug found + fixed) |
| `agri/remote_sense/sentinel_pull.py` | AOI resolution + pull orchestration | ✅ implemented, **real test run**, network mocked |
| `agri/tasks.py` | `pull_satellite_capture`, `pull_satellite_comparison` | ✅ **real test run** (bug found + fixed: eager-result polling) |
| `agri/api/satellite.py` | `POST .../satellite/imagery/`, `.../compare/` | ✅ **real test run**, incl. 202 + full poll round-trip |
| `agri/api/serializers.py` | `AnalysisRunSerializer` exposes `computed_by` | ✅ tested |
| `coreplugins/precise_agric/templates/season.html` | farm-level date-range picker + pull button | ✅ implemented, **not exercised in a browser** |
| `coreplugins/precise_agric/public/PreciseAgricPanel.jsx` | per-boundary "Compare with Satellite" + dual-run display | ✅ implemented, **not exercised in a browser** |
| `agri/tests.py::TestSatellitePull` | backend test coverage for the above | ✅ **13/13 pass, real run** (bug found + fixed: stray line from an earlier edit) |

**Explicit gaps, stated plainly:**
- The frontend (season.html, PreciseAgricPanel.jsx) has not been clicked through in an actual browser —
  only reasoned through against this codebase's own conventions (jQuery/CSRF availability, existing
  polling patterns, existing component state shape). Verify manually before calling this done.
- `pull_satellite_capture`'s AOI-union fallback (§7, when no `AgriFarm.boundary` exists) is tested for
  "doesn't crash" but not for producing a geometrically sensible union on real multi-field data.
- Satellite Reference results reuse `AnalysisResult.PLANT_HEALTH` as their storage kind (§9.2) rather than
  a dedicated kind — a deliberate reuse-over-new-concept choice, not an oversight; revisit only if it
  causes a real conflict.
- **Pre-existing, unrelated bug found while verifying this work:**
  `TestAgri.test_resolve_agri_field_falls_back_to_persistent_field_link` fails
  (`IntegrityError: duplicate key value violates unique constraint "agri_field_agri_field_id_key"`) even
  with zero satellite code present — its `_agri_linked_run()` setup helper syncs a farm (which
  auto-creates a persistent `Field` linked to the synced `AgriField`), then the test itself creates a
  *second* `Field` for the same `AgriField`, violating the `OneToOneField` constraint. Out of scope for
  this stage; flagged here so it isn't lost.
