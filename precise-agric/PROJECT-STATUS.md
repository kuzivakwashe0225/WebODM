# Precise Agric System — Project Status

**Harare Institute of Technology · Center for AI**
_Last updated: 2026-07-07_

A precision-agriculture platform built **inside WebODM**, reusing its proven drone-mapping engine and
adding an agronomy workflow: upload drone orthophotos → demarcate fields → analyze crop health →
review → track progress across the season → sync with the **AgriTrack** farmer mobile app.

---

## Where we are — at a glance

| Area | Status |
|---|---|
| Rebrand → "Precise Agric System" (name, green theme, farm terminology) | ✅ Live |
| Field boundaries: draw, name, approve, delete | ✅ Live |
| 6-service crop analysis (plant health, RGB indices, grid zones, canopy, weed, report) | ✅ Live |
| Agronomist review workflow | ✅ Live |
| Roles & permissions (SuperAdmin / Admin / Technician / Agronomist) | ✅ Live |
| Interactive map: clickable fields, plant-health **heatmap**, **grid zone pop-ups** | ✅ Live |
| **Season Progress** trend graphs (per field + whole farm, by capture date) | ✅ Live |
| **Boundary reuse** across weekly captures (draw once, reuse every upload) | ✅ Live |
| AgriTrack integration: farm sync in, results push out (VARI/canopy/weed/health) | ✅ Backend live; end-to-end pending real device test |
| Raw drone-image path (full NodeODM processing) | ✅ Proven |

Backend is covered by an automated test suite (`agri/tests.py`). Frontend verified via live route/bundle
checks and manual testing.

---

## The farmer's journey (how it's used)

1. **Set up the farm** — create a Farm (formerly "Project"), upload the first orthophoto for a chosen
   **capture date** (the flight date, asked at upload).
2. **Demarcate fields** — draw each field on the map, give it a name; an agronomist **approves** it.
3. **Analyze** — one click runs the full analysis; results appear as numbers, a red→green **heatmap**,
   and clickable **grid zones**.
4. **Review & push** — an agronomist approves the run; results are pushed to the **AgriTrack** app.
5. **Repeat weekly (or at any cadence)** — every later upload of the same farm **reuses the same
   fields automatically** (as DRAFT, re-confirmed each time), so the same areas are compared like-for-like.
6. **Watch the season** — the **Season Progress** page charts every field's and the whole farm's trend
   over time, by capture date.

See **[value-guide.md](value-guide.md)** for plain-language explanations of every number (for demos).

---

## Recent fixes & improvements (2026-07-06 → 07)

### Map & analysis
- **Fixed: heatmap wouldn't display / boundaries appeared on a black screen / orthophoto vanished.**
  Root cause was a single axis-order bug (`GEOSGeometry.geojson` swaps EPSG:4326 to lat/long under
  GDAL 3), which put every boundary on the wrong side of the planet and 404'd the heatmap tiles. Fixed
  centrally (`agri/geo.py`); boundaries and heatmap now sit correctly on the orthophoto.
- **Plant-health heatmap** overlay (red→green, clipped to each field) reusing WebODM's own tiler — no
  new imagery pipeline.
- **Grid zone pop-ups** — click any zone to read its value + a plain Low/Moderate/Healthy note.
- **Boundaries show/hide toggle** and click-to-select fields.

### Fields & season
- **Boundary reuse made reliable.** Draw fields once on the first upload; every subsequent upload of
  the same farm inherits them automatically (copied from the most recent capture that has fields),
  arriving as **DRAFT** for a quick re-confirm. Works even for fields drawn earlier.
- **"Reuse fields from a previous capture"** button to back-fill captures that were uploaded before a
  field set-up existed.
- **Capture date drives the timeline** — the day the imagery was *taken*, not uploaded. You can collect
  all season and process later; the graphs still line up with reality.
- **Season Progress page redesigned** — a card per index (Plant Health, VARI, Green Leaf Index, Canopy
  Cover, Weed Hotspots), each with a plain-language explanation, a colour-coded **trend badge**
  (Improving / Needs attention / Holding steady), the latest whole-farm value, and filled trend charts
  (per-field lines + a bold whole-farm average).

### Product
- **"Project" renamed to "Farm"** across the main UI (Add Farm, New/Edit Farm, etc.).
- **Attribution footer** on every page: _© Harare Institute of Technology · Center for AI. All rights reserved._

---

## Known limitations (honest list)
- **EXG** (RGB-only greenness) isn't a fixed 0–100 scale and can be negative — best read as a trend and
  via the heatmap, not as an absolute score (explained in the value guide).
- Season graphs are **descriptive** (they show and explain trends) — not yet a predictive forecast model.
- AgriTrack **inbound** sync mechanism (REST vs Socket.io) and output-asset authentication are still to
  be confirmed with the AgriTrack team; the outbound results push is built and unit-tested.
- The "Project → Farm" rename covers the main screens; a few deeper strings still say "Project".

---

## Documentation map
- **[value-guide.md](value-guide.md)** — plain-language meaning of every metric (for demos).
- **[architecture-and-plan.md](architecture-and-plan.md)** — full architecture & data model.
- **[stages/](stages/)** — one document per build stage, including what was built and how it was verified.
- **[progress-log.md](progress-log.md)** — dated running log of the work.
- **[testing.md](testing.md)** / **[manual-testing.md](manual-testing.md)** — how to test.
