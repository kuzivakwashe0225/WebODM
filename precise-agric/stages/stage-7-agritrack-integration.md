# Stage 7 — AgriTrack mobile app integration ✅ backend done, 🚧 live end-to-end unconfirmed (2026-07-06)

Connects Precise Agric System to **AgriTrack**, a separately-developed mobile app: farmers register
a farm/fields in AgriTrack, which syncs that data in here (creating a `Project`); we analyze drone
orthophotos against those fields; results get pushed back out to AgriTrack so the farmer sees them
on their phone. Two independent data flows, built and tested separately.

## Why this needed new models

`Boundary` (Stage 1) previously only supported hand-drawn polygons. AgriTrack fields have their own
IDs and authoritative boundaries that must be matched to a `Boundary`/`AnalysisRun` so results can
be reported back with the right `farm_id`/`field_id`. New models in `agri/models/agritrack.py`:

- **`AgriFarm`** — `agritrack_farm_id` (unique), `agritrack_farmer_id`, `project`
  (`OneToOneField` to `app.Project` — one AgriTrack farm = one WebODM project), `name`, `location`,
  optional `boundary`, `synced_at`.
- **`AgriField`** — `agritrack_field_id` (unique), `farm` (FK), `name`, `crop`, `area_ha`,
  **required** `boundary` (the authoritative geometry — per the locked decision that a synced
  field's boundary is used as-is, never overwritten from imagery, at least for this MVP).
- `Boundary` gained a nullable `agri_field` FK — a `Boundary` is either hand-drawn (`geom` set
  directly) or sourced from a synced `AgriField` (`geom` copied from `agri_field.boundary` at
  create time). Whichever exists is used; a real per-farm toggle between the two is deferred.

Migration: `agri/migrations/0003_auto_20260706_1448.py`.

## Flow 1 — Inbound: farm/field sync (AgriTrack → us)

- **`POST /api/v1/mobile/sync`** (`agri/agritrack/views.py:MobileSyncView`) — exact literal path
  per AgriTrack's contract, mounted at Django's root `urls.py`, not under `/api/agri/`.
- Auth: `X-Api-Key` header, checked with `hmac.compare_digest` against
  `settings.AGRITRACK_INBOUND_API_KEY` (constant-time comparison — avoids a timing side-channel).
- **`agri/agritrack/sync.py:sync_farm_payload`** — idempotent create-or-update by AgriTrack's own
  `farmId`/`fieldId`. First sync of a farm auto-creates a `Project` (owner = a configurable service
  account, `AGRITRACK_SYNC_OWNER_USERNAME`, falling back to the first superuser — farmers have no
  WebODM login of their own). Subsequent syncs update name/location/boundary in place. A new field
  with no boundary in the payload is rejected (nothing valid to store yet); an existing field can
  omit boundary on a later sync (keeps the last-known one).
- When a `Boundary` is created against a synced `AgriField` (`agri/api/views.py:perform_create`),
  its geometry is copied from the field and it's rejected (400) if the field's farm doesn't match
  the capture's own project — closes a cross-tenant data leak.

**Status: implemented and unit-tested, but NOT confirmed reachable from the real AgriTrack backend
today.** AgriTrack's contract describes this sync as happening over **Socket.io**
(`mobile:farm_sync` event, AgriTrack connecting to us as a client), not this REST endpoint. Whether
AgriTrack can/will actually call `POST /api/v1/mobile/sync` directly is an open question sent back
to the user to confirm with the AgriTrack team. If Socket.io turns out to be required, the plan is
a small standalone sidecar service (not yet started) that receives the `mobile:farm_sync` event and
calls `sync_farm_payload` internally.

## Flow 2 — Outbound: analysis results push (us → AgriTrack)

- **`agri/agritrack/results.py`** — targets AgriTrack's confirmed-live
  **`POST /orthophoto/analysis/push`** (their server, `X-Api-Key` auth), which is a *different*,
  simpler contract than an earlier "rebuild target" document the user had also been given
  (`/integrations/orthophoto/results`, camelCase, farmId/fieldId/scope/metrics/outputs/
  interpretation/extId) — that document describes an aspirational future API, not what's live.
- **`build_orthophoto_result_payload(run)`** maps our `AnalysisResult` rows to their fields:

  | Sent | Source |
  |---|---|
  | `field_id` / `farm_id` | `run.boundary.agri_field` → `AgriField`/`AgriFarm`'s own AgriTrack IDs |
  | `metrics.health_score`, `.classification` | Plant Health result, via `classify_plant_health()` |
  | `metrics.vari_mean/min/max` | RGB-index result's `stats['VARI']` |
  | `metrics.canopy_cover_pct` | Canopy result |
  | `metrics.weed_count` | Weed result |
  | `outputs.heatmap_url/index_map_url/weed_map_url` | Asset paths, built into full URLs via `AGRITRACK_PUBLIC_BASE_URL` |
  | `summary`, `recommendations` | Report result |
  | `ext_id` | `"webodm-run-<id>"` |

  **Deliberately omitted, not invented:** `plant_count`, `height_mean_m`/`height_max_m`,
  `mean_plant_distance_cm`, `avg_crown_diameter_cm`, `uniformity_pct`, `stress_zone_pct`,
  `healthy_zone_pct` — our pipeline has no plant-counting or height/surface-model step. Sending
  fabricated numbers for these would be worse than omitting them.
- **Classification thresholds** (`classify_plant_health`) — **proposed defaults, not yet reviewed
  by an agronomist**:
  - NDVI: poor `< 0.2`, fair `0.2–0.5`, good `≥ 0.5` (reasonably standard in remote-sensing
    literature); `health_score` = NDVI rescaled from `[-0.1, 0.9] → [0, 1]`, clamped.
  - ExG (RGB-only fallback when there's no NIR band): poor `< 5`, fair `5–25`, good `≥ 25`;
    `health_score = clamp(ExG / 60, 0, 1)`. **Flagged as a rough heuristic** — ExG isn't as
    standardized as NDVI; needs recalibration against real field data.
  - AgriTrack's contract accepts a simple 3-value scale (good/fair/poor) for orthophoto
    compatibility, not the fuller 4-tier scale used elsewhere in their system.
- **`agri/push.py:push_analysis`** branches: if the approved run's boundary is linked to a synced
  `AgriField`, it calls `push_orthophoto_results` (this flow); otherwise it falls back to the
  original Stage 4 generic webhook (`AGRITRACK_PUSH_URL`), since a hand-drawn boundary has no
  `farmId`/`fieldId` to report.
- No-op (returns `False`, logs, doesn't raise) if `AGRITRACK_RESULTS_PUSH_URL` or
  `AGRITRACK_OUTBOUND_API_KEY` isn't set, or the boundary has no linked `AgriField` — safe to leave
  unconfigured in environments that don't need it.

## Settings added (`webodm/settings.py`, all `WO_*`-env-driven, default `None`)

| Setting | Purpose |
|---|---|
| `AGRITRACK_INBOUND_API_KEY` | Validates `X-Api-Key` on incoming `/api/v1/mobile/sync` calls |
| `AGRITRACK_SYNC_OWNER_USERNAME` | WebODM user that owns auto-created farm Projects |
| `AGRITRACK_RESULTS_PUSH_URL` | AgriTrack's live results endpoint (their host + `/orthophoto/analysis/push`) |
| `AGRITRACK_OUTBOUND_API_KEY` | The `X-Api-Key` value AgriTrack issued us for that endpoint |
| `AGRITRACK_PUBLIC_BASE_URL` | This server's own public URL, used to build `outputs.*_url` links |

## Deployment (2026-07-06)

- `AGRITRACK_RESULTS_PUSH_URL` set to AgriTrack's ngrok tunnel:
  `https://wasp-drastic-nursery.ngrok-free.dev/orthophoto/analysis/push`.
- `AGRITRACK_OUTBOUND_API_KEY` set to the real key AgriTrack issued
  (`atk_2184...`, stored in `.env`, not in this doc or in code).
- Both wired into `docker-compose.dev.yml` via `${...}` substitution (not hardcoded) so `.env`
  stays the single place secrets live.
- **Caveat, flagged and accepted by the user as-is:** this repo's `.env` has been git-tracked since
  an early commit, despite its own `.gitignore` listing `.env` (gitignore doesn't retroactively
  protect an already-tracked file). The AgriTrack API key currently sits in a tracked file. User's
  explicit decision: leave it tracked, rely on discipline (never `git add -A` / `git commit -a`
  while it's populated) rather than `git rm --cached .env`.
- New env vars require a container recreate to take effect (`./webodm.sh restart`), not just a
  code reload.

## Tests

`agri/tests.py`, `TestAgriTrackSync` (7 tests: API-key gating, create+update idempotency, missing
`farmId` rejection, new-field-without-boundary rejection, boundary-from-synced-field creation,
cross-farm boundary rejection) + 5 more inside `TestAgri`
(`test_classify_plant_health_ndvi/exg`, `test_orthophoto_result_payload_maps_our_metrics`,
`test_push_uses_agritrack_live_endpoint_when_field_linked`,
`test_push_skipped_when_agritrack_not_configured`). **All 12 passing**, confirmed by a full targeted
run (`Ran 12 tests ... OK`), including proof that `vari_mean` is present in the outbound payload.

Notable bug fixed during TDD: `@override_settings(AGRITRACK_INBOUND_API_KEY=...)` had no effect on
these tests (401s even with the correct key) because `agri/agritrack/views.py` reads
`from webodm import settings` (the raw module) — Django's `override_settings` only patches
`django.conf.settings`, a different object. Fixed by mutating `settings.AGRITRACK_INBOUND_API_KEY`
directly in `setUp`/`tearDown`, matching the pattern already used for `AGRITRACK_PUSH_URL` in the
Stage 4 tests.

## Outstanding / not yet resolved

- **Inbound mechanism unconfirmed**: does AgriTrack actually call our REST `/api/v1/mobile/sync`,
  or only emit a `mobile:farm_sync` Socket.io event? Needs an answer from the AgriTrack team.
- **"Token" field**: the user was asked by AgriTrack (or understood AgriTrack to want) "a token
  attached to the payload so the mobile app can know the farm and fields number." Exact semantics
  unclear — not implemented. Do not guess the field name/shape; wait for AgriTrack's team to
  specify it precisely, since sending an unexpected field to a live third-party endpoint risks
  silently breaking their parsing.
- **Output asset URLs** (`heatmap_url`/`index_map_url`/`weed_map_url`) point at WebODM's normal
  authenticated asset-serving endpoints. AgriTrack's server has no WebODM session/login, so it's
  unclear it can actually fetch these. Not solved.
- **ExG classification thresholds** are a rough heuristic pending agronomist review — do not treat
  as calibrated for real farmer-facing decisions yet.
- **End-to-end live push** (real orthophoto → real AgriTrack ngrok endpoint) has not yet been
  exercised — only unit tests against a mocked `requests.post` have run. First real live push is
  pending manual testing.
