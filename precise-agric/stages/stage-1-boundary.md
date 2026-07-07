# Stage 1 — `agri` app + Boundary draw + approval gate 🚧

**Objective:** stand up the `agri` app, let a technician draw a field boundary on a capture's map,
and let an Admin/Agronomist approve it. Approval unlocks analysis (Stage 2).

## Design

- **Capture = an imported-orthophoto `app.Task`** (no new model). Import a lone `.tif` via
  `import_url="file://external"`: place it at `odm_orthophoto/odm_orthophoto.tif`, dispatch
  `process_task`; WebODM computes extent + bands → `COMPLETED`. (See §14 in architecture doc.)
- **`Boundary`** (new, `agri/models/boundary.py`): FK `app.Task`, `geom` PolygonField (SRID 4326),
  `status` DRAFT→APPROVED→(REJECTED), `created_by`/`approved_by`/`approved_at`.
- **API** `/api/agri/boundaries/`: list/create (DRAFT), `POST {id}/approve`. GeoJSON via
  WebODM's `PolygonGeometryField`.
- **UI:** draw on the existing Leaflet map (reuse `measure`/Leaflet.Draw), list boundaries, approve
  button, and gate a "Start Analysis" action on an APPROVED boundary.

## Task checklist

- [x] Scaffold `agri` app (apps, models, admin, api, migrations)
- [x] `Boundary` model
- [x] Boundary API (list/create/approve) + `PolygonGeometryField`
- [x] Wire `agri` into `INSTALLED_APPS` + `/api/agri/`
- [x] Migration generated + applied + `agri_boundary` PostGIS table verified (geom Polygon/4326, FK→app_task)
- [x] API route verified live (`GET /api/agri/boundaries/` → 403 auth-guarded, not 404)
- [x] Automated tests **8/8 green** (Boundary API + branding)
- [x] Capture import service `create_capture_from_orthophoto` (external-import) + TDD test
- [x] Capture-upload API endpoint (`POST /api/agri/captures/`) + TDD tests — **11/11 green, backend done**
- [ ] Boundary drawing UI on the map (Leaflet)  ← frontend (manual-verified)
- [ ] Approval → unlock "Start Analysis" (gate)

## Verification plan

1. `showmigrations agri` shows the applied migration; `agri_boundary` table exists in PostGIS.
2. `GET /api/agri/boundaries/` returns 200 (empty) for an authed user; `POST` with a GeoJSON polygon
   creates a DRAFT; `POST {id}/approve` flips it to APPROVED.
3. Import `app/fixtures/orthophoto.tif` → a `COMPLETED` task with `orthophoto_extent` populated,
   viewable on the map.

## Role note
Visibility is currently scoped to viewable projects; fine-grained "who can approve" +
org-wide reviewer visibility is deferred to **Stage 5**.
