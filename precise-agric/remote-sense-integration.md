# Remote-Sense (Sentinel) GeoTIFF Integration

How the custom remote-sensing / Sentinel pipeline delivers georeferenced imagery into Precise Agric
System, and how that imagery is tied to the right farm and made analyzable.

## Goal

A third external system (the remote-sense pipeline) produces a **georeferenced GeoTIFF** for a farm,
tagged with the **AgriTrack farm id** the mobile app assigned. It pushes that `.tif` to this system,
which imports it as a **Capture** on the matching farm's Project so it can be reviewed and analyzed, and
its results pushed back to AgriTrack per-field.

## Why this is an *import*, not a *processing* job

WebODM normally ingests many overlapping drone JPEGs and runs them through NodeODM (photogrammetry) to
produce an orthophoto. A Sentinel GeoTIFF is **already** a single georeferenced orthophoto — there is
nothing to stitch. So this reuses WebODM's existing **external-import** path
([agri/capture.py](../agri/capture.py) `create_capture_from_orthophoto`), the exact same path a manual
single-`.tif` upload uses: create a Task with `import_url="file://external"` +
`pending_action=IMPORT`, drop the file at the canonical orthophoto asset path, and let the worker skip
straight to extent/band detection → `COMPLETED`. No processing node is involved.

## The three-system flow

```
Mobile app (AgriTrack) creates a farm  ─────────────▶ farm_id
        │
        │ AgriTrack backend syncs farm + fields to us
        ▼
  POST /api/v1/mobile/sync        (X-Api-Key: WO_AGRITRACK_INBOUND_API_KEY)
        │   → creates AgriFarm (+ Project) and AgriFields (authoritative boundaries)
        ▼
  Remote-sense pipeline produces a GeoTIFF for that farm_id and pushes it
        ▼
  POST /api/v1/remote-sense/push  (X-Api-Key: WO_REMOTE_SENSE_INBOUND_API_KEY)
        │   → resolves farm_id → Project, imports .tif as a Capture,
        │     seeds field boundaries from the farm's AgriFields
        ▼
  Agronomist approves boundary → runs analysis → approves run
        ▼
  POST <WO_AGRITRACK_RESULTS_PUSH_URL>/orthophoto/analysis/push  (results back to AgriTrack per-field)
```

**Ordering is mandatory:** the farm must be synced from AgriTrack *before* imagery arrives. An unknown
`farm_id` on the push endpoint returns `404` — we never auto-create a farm from imagery, because without
the synced farm and its fields there is nothing to fit the image onto or analyze.

## The endpoint

`POST /api/v1/remote-sense/push` — server-to-server, authenticated with header
`X-Api-Key: <WO_REMOTE_SENSE_INBOUND_API_KEY>` (a **separate** key from AgriTrack's, so each can be
rotated independently and a leak of one doesn't compromise the other). Implemented by
`RemoteSensePushView` ([agri/remote_sense/views.py](../agri/remote_sense/views.py)); core logic in
[agri/remote_sense/ingest.py](../agri/remote_sense/ingest.py); mounted as a literal path in
[webodm/urls.py](../webodm/urls.py) (like the mobile-sync route, because the remote-sense client calls
this path directly).

### Two delivery modes

1. **File push (primary)** — `multipart/form-data`:
   | field | required | meaning |
   |---|---|---|
   | `farm_id` | yes | the AgriTrack farm id the image belongs to (integer) |
   | `orthophoto` | yes* | the `.tif` file |
   | `capture_date` | no | acquisition date `YYYY-MM-DD` (the seasonal-graph x-axis; defaults to today) |
   | `name` | no | capture name (defaults to `Sentinel capture (farm <id>)`) |

2. **URL pull (optional)** — JSON `{"farm_id": …, "image_url": "https://<host>/…tif", …}`. We fetch
   `image_url` ourselves. *SSRF-guarded:* the URL must live under `WO_REMOTE_SENSE_BASE_URL` (path
   boundary enforced, so `https://host` cannot be spoofed by `https://host.evil.com`), and that env var
   must be set or pull mode is refused. The configured key is sent as `X-Api-Key` on the fetch.

\*Exactly one of `orthophoto` or `image_url` per request.

### Responses

| status | when |
|---|---|
| `201` | imported; body `{"status":"ok","captureId":…,"projectId":…,"farmId":…,"captureStatus":…}` |
| `401` | missing/invalid `X-Api-Key` |
| `404` | `farm_id` was never synced from AgriTrack |
| `400` | missing `farm_id`, non-integer `farm_id`, neither file nor url, or a disallowed `image_url` |

## "Fits exactly on the farm"

Two halves:

1. **Georeferencing** is the remote-sense system's responsibility — the `.tif` must carry a correct
   CRS/extent that actually covers the farm's field polygons. This system does **not** warp or
   reproject; it trusts the embedded geo, exactly as WebODM does for any imported orthophoto.
2. **Field boundaries** are seeded automatically. On a farm's *first* capture there is no prior capture
   to copy boundaries from, so `create_capture_from_orthophoto` falls back to
   `seed_boundaries_from_agrifields` ([agri/capture.py](../agri/capture.py)), which creates a DRAFT
   `Boundary` for each of the farm's synced `AgriField`s using their authoritative boundary, linked via
   `agri_field`. Subsequent captures clone boundaries from the most recent prior capture — and that
   clone now carries the `agri_field` link forward too, so **every** capture's analysis (not just the
   first) is pushable back to AgriTrack per-field.

Boundaries land as **DRAFT** — the human review gate (an Admin/Agronomist approves before analysis) is
deliberately preserved. Full unattended automation (auto-approve boundary → auto-run → auto-push) is a
separate, deliberate decision, not the default.

## Configuration

```
WO_REMOTE_SENSE_INBOUND_API_KEY=<shared secret the remote-sense system sends as X-Api-Key>
WO_REMOTE_SENSE_BASE_URL=<remote-sense host; only needed for URL-pull mode, else leave blank>
```
Both read in [webodm/settings.py](../webodm/settings.py) as `REMOTE_SENSE_INBOUND_API_KEY` /
`REMOTE_SENSE_BASE_URL`. With no inbound key set, the endpoint rejects everything.

## Manual test

After at least one farm has been synced (say `farm_id=3`):
```bash
curl -v -X POST https://preciseagric.tawananyasha.com/api/v1/remote-sense/push \
  -H "X-Api-Key: <WO_REMOTE_SENSE_INBOUND_API_KEY>" \
  -F "farm_id=3" -F "capture_date=2026-05-01" \
  -F "orthophoto=@/path/to/sentinel.tif"
```
`201` = imported (a new Capture appears under that farm's Project); `404` = that farm id was never
synced; `401` = wrong key.

## Files

| file | role |
|---|---|
| [agri/remote_sense/views.py](../agri/remote_sense/views.py) | `RemoteSensePushView` + `X-Api-Key` check |
| [agri/remote_sense/ingest.py](../agri/remote_sense/ingest.py) | farm resolution, SSRF-guarded fetch, capture creation |
| [agri/capture.py](../agri/capture.py) | `seed_boundaries_from_agrifields`; `agri_field` carried forward in cloning |
| [webodm/urls.py](../webodm/urls.py) | mounts `/api/v1/remote-sense/push` |
| [webodm/settings.py](../webodm/settings.py) | `REMOTE_SENSE_INBOUND_API_KEY`, `REMOTE_SENSE_BASE_URL` |
| [agri/tests.py](../agri/tests.py) | `TestRemoteSensePush` |

## Tests

`TestRemoteSensePush` in [agri/tests.py](../agri/tests.py):
- `test_push_requires_api_key` — 401 with no key / wrong key
- `test_push_unknown_farm_returns_404` — valid key, unsynced farm → 404
- `test_push_happy_path_returns_201` — 201 + correct JSON; farm_id/file/parsed date forwarded to ingest
- `test_ingest_creates_capture_and_seeds_agrifields` — capture on the farm's Project, capture_date set,
  one DRAFT boundary seeded and linked to the AgriField with matching geometry
- `test_ingest_unknown_farm_raises` — `FarmNotFoundError`
- `test_ingest_requires_file_or_url` — `RemoteSenseValidationError`

Run: `./webodm.sh test backend agri.tests.TestRemoteSensePush`

_Test-run results are recorded in [remote-sense-test-runs.md](remote-sense-test-runs.md)._
