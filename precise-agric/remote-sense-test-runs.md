# Remote-Sense Integration — Test Run Log

Environment: local `./webodm.sh start --dev` stack (source bind-mounted into `webapp`), Django 2.2.27,
`CELERY_TASK_ALWAYS_EAGER=True` (test setting). Runs executed via
`docker exec webapp python manage.py test …`. Date: 2026-07-09.

## Run 1 — new feature tests (`agri.tests.TestRemoteSensePush`)

Command:
```
python manage.py test agri.tests.TestRemoteSensePush --verbosity=2
```
Result: **6/6 passed** (4.4s).
```
test_ingest_creates_capture_and_seeds_agrifields ... ok
test_ingest_requires_file_or_url ... ok
test_ingest_unknown_farm_raises ... ok
test_push_happy_path_returns_201 ... ok
test_push_requires_api_key ... ok        (logs expected "Unauthorized: /api/v1/remote-sense/push")
test_push_unknown_farm_returns_404 ... ok (logs expected "Not Found: /api/v1/remote-sense/push")

Ran 6 tests in 4.406s
OK
```
Covers: API-key auth (401), unsynced-farm rejection (404), happy-path 201 + JSON shape + args forwarded
to ingest, real ingest creating a Capture on the correct Project with the given capture_date, DRAFT
boundary seeded from the AgriField (agri_field link + matching geometry), and both validation errors.

## Run 2 — regression on the changed shared code (`TestAgri`, `TestAgriTrackSync`)

These exercise `agri/capture.py` (which I changed: `seed_boundaries_from_agrifields` + carrying the
`agri_field` link forward in boundary cloning) and the inbound AgriTrack sync.

Command:
```
python manage.py test agri.tests.TestAgri agri.tests.TestAgriTrackSync --verbosity=1
```
Result: **52/52 passed** (58.2s).
```
Ran 52 tests in 58.165s
OK
```

## Run 3 — full module (`agri.tests`)

Command:
```
python manage.py test agri.tests --verbosity=1
```
Result: **58/59 passed, 1 environmental failure** (118.7s).
```
FAIL: test_raw_image_task_flows_through_boundary_and_analysis (agri.tests.TestAgriRawImageCapture)
    self.assertTrue(pnode.is_online(), "local test NodeODM never came online")
AssertionError: False is not true : local test NodeODM never came online

Ran 59 tests in 118.729s
FAILED (failures=1)
```

### Why the one failure is not related to this change
- The test starts a **NodeODM Node.js subprocess** (`start_processing_node()`) and polls up to 60s for
  it to come online; it timed out on `pnode.is_online()` ([agri/tests.py:887](../agri/tests.py#L887)).
  The test's own comment notes this cold-start is slow "on this container's slow Windows bind-mount."
- It exercises the **raw-drone-image → photogrammetry → orthophoto** path (a real processing node),
  building its `Boundary`/`AnalysisRun` directly — it does **not** call `create_capture_from_orthophoto`
  (the node-less import path I touched), nor anything in `agri/remote_sense/`, `results.py`, `settings`,
  or `urls` that this change modified.
- It is an environment/infra dependency (ability to launch a NodeODM subprocess), independent of the
  Python diff. It is expected to pass in CI / a full stack with a working test NodeODM, and to fail in a
  bare `--dev` stack on a slow Windows bind-mount. Every test that touches the changed code passed.

## Summary
| scope | tests | result |
|---|---|---|
| new remote-sense feature | 6 | all pass |
| changed shared code + sync regression | 52 | all pass |
| full agri module | 59 | 58 pass, 1 environmental (NodeODM subprocess startup, unrelated) |
