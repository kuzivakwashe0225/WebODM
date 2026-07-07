# Manual testing guide — full walkthrough (Stages 0–5)

One continuous, ordered pass through everything built so far: rebrand → farm → capture upload →
boundary draw/approve → analysis (6-service fan-out) → agronomist review → AgriTrack push → role
enforcement. There's no boundary-drawing **map UI** yet, so boundaries/analysis are driven via the
API (curl or the DRF browsable API) — every backend flow is live and testable.

Every step below is checked against the actual code in `agri/api/views.py` and `agri/roles.py` —
not assumed — including two real gaps called out honestly in **§8 Known limitations**.

---

## 0. Prereqs

- Stack up: `docker compose -p webodm -f docker-compose.yml -f docker-compose.nodeodm.yml -f docker-compose.dev.yml up -d` (or your usual `./webodm.sh start --dev` from a shell that can run it).
- App at `http://localhost:8000`. You have (or can create) an admin/superuser login.
- `curl` available. All commands assume you run them **from the repo root on the host** (not inside
  the container) so relative file paths like `app/fixtures/orthophoto.tif` resolve.

Set these once and reuse them throughout:
```bash
BASE=http://localhost:8000
ADMIN='-u YOUR_ADMIN_USER:YOUR_ADMIN_PASS'
```
(WebODM's DRF API accepts HTTP Basic auth — confirmed via `BasicAuthentication` in
`REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES` — so `-u user:pass` works directly on every
`/api/...` call below; no token dance needed.)

---

## 1. Stage 0 — Rebrand (visual, 1 min)

1. Open http://localhost:8000 and **hard-refresh** (Ctrl+F5).
2. Check: header shows **"Precise Agric System"** (not WebODM), header bar + nav links + primary
   buttons are **green**, browser tab title matches.
3. Admin cross-check: `/admin/` → **Application** → `App name` = "Precise Agric System"; **Theme** →
   `Header background` = `#2e7d32`.

✅ Pass if the name + green theme show everywhere.

---

## 2. Create test users with roles (needed for Stages 1–5)

Roles are Django Groups, assigned via `/admin/auth/user/` (there is no role-assignment API yet).
Fastest way to create two clearly-labeled test accounts:

```bash
docker compose -p webodm exec webapp python manage.py shell -c "
from django.contrib.auth.models import User, Group
tech, _ = User.objects.get_or_create(username='pa_tech', defaults={'email': 'tech@test.com'})
tech.set_password('test12345'); tech.is_active = True; tech.save()
tech.groups.add(Group.objects.get(name='Technician'))

agro, _ = User.objects.get_or_create(username='pa_agro', defaults={'email': 'agro@test.com'})
agro.set_password('test12345'); agro.is_active = True; agro.save()
agro.groups.add(Group.objects.get(name='Agronomist'))
print('OK: pa_tech (Technician), pa_agro (Agronomist), password test12345')
"
```
✅ Pass: prints the confirmation line. Cross-check at `/admin/auth/user/` → both users exist with
their Group assigned (also confirms the four groups **SuperAdmin / Admin / Technician / Agronomist**
exist — they're seeded automatically on every boot).

```bash
TECH='-u pa_tech:test12345'
AGRO='-u pa_agro:test12345'
```

---

## 3. Farm + Capture (Stage 1)

**3.1 Create a farm** (as the technician — becomes its owner):
```bash
curl -s $TECH -X POST "$BASE/api/projects/" -d "name=Test Farm"
```
✅ `201`. Note the `id` → `PROJECT_ID`.

**3.2 Upload a capture** — two options, both produce a normal capture usable in every step below.
Use whichever matches what a technician actually has.

**Option A — already have a stitched orthophoto (.tif):**
```bash
curl -s $TECH -X POST "$BASE/api/agri/captures/" \
  -F "project=PROJECT_ID" -F "name=Field Flight 1" \
  -F "orthophoto=@app/fixtures/orthophoto.tif"
```
✅ `201` with a task `id` (a UUID) → `TASK_ID`. This is an **external import** (no processing node) —
completes in seconds.

**Option B — only have raw drone photos:** use WebODM's own native, unmodified "Select Images and
GCP" flow — either through the dashboard UI, or via its API directly:
```bash
curl -s $TECH -X POST "$BASE/api/projects/PROJECT_ID/tasks/" \
  -F "images=@app/fixtures/tiny_drone_image.jpg" \
  -F "images=@app/fixtures/tiny_drone_image_2.jpg"
```
✅ `201` with a task `id` → `TASK_ID`. This requires a working **processing node** (real NodeODM,
already running in this stack as `webodm-node-odm-1`) and takes **real processing time** (minutes,
not seconds) — check progress on the dashboard or poll `GET /api/projects/PROJECT_ID/tasks/TASK_ID/`.

Both options converge into the exact same capture — everything from §3.3 onward is identical
regardless of which you picked. (Proven by an automated test, not just claimed: see
`testing.md` → `TestAgriRawImageCapture`.)

**3.3 Confirm it completed:**
```bash
curl -s $TECH "$BASE/api/projects/PROJECT_ID/tasks/TASK_ID/" | grep -o '"status":[0-9]*'
```
✅ `"status":40` (`COMPLETED`). If still processing, wait a few seconds and retry.

**3.4 View on the map (visual):** dashboard → open "Test Farm" → the task → "View Map" — the
orthophoto renders over the ESRI satellite base layer.

**3.5 Get the capture's extent as GeoJSON** (used as the boundary geometry below, guaranteeing it
overlaps the raster — hand-drawing exact coordinates by curl isn't practical without the map UI):
```bash
docker compose -p webodm exec webapp python manage.py shell -c "
from app.models import Task
print(Task.objects.get(pk='TASK_ID').orthophoto_extent.geojson)
"
```
Copy the printed `{"type": "Polygon", "coordinates": [...]}` → `EXTENT_GEOJSON`.

---

## 4. Boundary draw + approval gate (Stage 1)

**4.1 Auth required:**
```bash
curl -s -o /dev/null -w "%{http_code}\n" "$BASE/api/agri/boundaries/"   # expect 403
```

**4.2 Technician draws a boundary (DRAFT)** on their own capture:
```bash
curl -s $TECH -H "Content-Type: application/json" -X POST "$BASE/api/agri/boundaries/" \
  -d "{\"task\": \"TASK_ID\", \"name\": \"Field A\", \"geom\": $EXTENT_GEOJSON}"
```
✅ `201`, `"status": "DRAFT"`. Note the `id` → `BOUNDARY_ID`.

**4.3 Technician cannot approve their own boundary** (reviewers only, Stage 5):
```bash
curl -s -o /dev/null -w "%{http_code}\n" $TECH -X POST "$BASE/api/agri/boundaries/BOUNDARY_ID/approve/"
```
✅ `403`.

**4.4 Agronomist approves it:**
```bash
curl -s $AGRO -X POST "$BASE/api/agri/boundaries/BOUNDARY_ID/approve/"
```
✅ `200`, `"status": "APPROVED"`, `approved_by`/`approved_at` populated.

**4.5 Visibility check** — the agronomist didn't create or own this farm but can still see the
boundary (org-wide reviewer visibility):
```bash
curl -s $AGRO "$BASE/api/agri/boundaries/" | grep -o "\"id\":$BOUNDARY_ID"
```
✅ Present. A third, unrelated user (not owner, no role) would **not** see it — try creating one and
confirm an empty/absent result if you want to verify this explicitly.

**4.6 Admin cross-check:** `/admin/agri/boundary/` shows it, status **APPROVED**.

---

## 5. Analysis — the 6-service fan-out (Stages 2 & 3)

**5.1 Gate check — cannot analyze a DRAFT boundary.** Draw a second boundary (repeat 4.2, name
"Field B") and, **without** approving it:
```bash
curl -s -o /dev/null -w "%{http_code}\n" $TECH -X POST "$BASE/api/agri/analysis/" \
  -H "Content-Type: application/json" -d '{"boundary": DRAFT_BOUNDARY_ID}'
```
✅ `400` (boundary must be APPROVED).

**5.2 Agronomist is read-only — cannot trigger analysis**, even on the APPROVED boundary from §4:
```bash
curl -s -o /dev/null -w "%{http_code}\n" $AGRO -X POST "$BASE/api/agri/analysis/" \
  -H "Content-Type: application/json" -d '{"boundary": BOUNDARY_ID}'
```
✅ `403`.

**5.3 Technician triggers analysis** on their APPROVED boundary:
```bash
curl -s $TECH -X POST "$BASE/api/agri/analysis/" \
  -H "Content-Type: application/json" -d '{"boundary": BOUNDARY_ID}'
```
✅ `201`. Note the run `id` → `RUN_ID`. Status starts `PENDING`/`RUNNING`.

**5.4 Poll until done** (runs sequentially through all six services — plant health, RGB indices,
grid, canopy, weed, report — typically well under a minute for the small test fixture):
```bash
watch -n2 "curl -s $TECH $BASE/api/agri/analysis/RUN_ID/ | python -m json.tool"
```
(No `watch`? Just re-run the `curl ... | python -m json.tool` line every couple of seconds.)

✅ Pass when `"status": "PENDING_REVIEW"` and `"results"` has **6** entries:
`plant_health, rgb_index, grid, canopy, weed, report`, each with a non-empty `stats` object. Sanity
spot-checks:
- `plant_health.stats.index` is `"EXG"` (the fixture is RGB-only, no NIR — confirms the plan's
  "RGB only → best available index" behaviour, not NDVI).
- `canopy.stats.canopy_pct` is between 0–100.
- `report.stats.summary` and `report.stats.recommendations` are populated.

If it lands `FAILED` instead, check `"error"` in the response and the worker logs
(`docker logs --tail 50 worker`).

---

## 6. Agronomist review → AgriTrack push (Stage 4)

**6.1 Technician cannot approve/reject the run:**
```bash
curl -s -o /dev/null -w "%{http_code}\n" $TECH -X POST "$BASE/api/agri/analysis/RUN_ID/approve/"
```
✅ `403`.

**6.2 Agronomist approves it:**
```bash
curl -s $AGRO -X POST "$BASE/api/agri/analysis/RUN_ID/approve/"
```
✅ `200`, `"status": "APPROVED"`, `reviewed_by` = `pa_agro`.

**6.3 Approving twice is rejected** (must be `PENDING_REVIEW`):
```bash
curl -s -o /dev/null -w "%{http_code}\n" $AGRO -X POST "$BASE/api/agri/analysis/RUN_ID/approve/"
```
✅ `400`.

**6.4 AgriTrack push — default (disabled) behaviour.** Out of the box `AGRITRACK_PUSH_URL` is unset,
so approval logs a skip rather than pushing:
```bash
docker logs --tail 20 worker | grep -i agritrack
```
✅ You should see `AGRITRACK_PUSH_URL not set; skipping push for run <RUN_ID>`. This is the
**expected default** — not a failure.

**6.5 (Optional) Exercise a real push.** Get a throwaway endpoint from
[webhook.site](https://webhook.site), then:
```bash
# 1. Add the URL so it survives past this shell session:
echo "AGRITRACK_PUSH_URL = 'https://webhook.site/YOUR-ID'" >> webodm/settings_override.py
# 2. Restart the worker so it picks up the new setting:
docker compose -p webodm restart worker
```
Then repeat 5.3–6.2 with a **new** run (a run already `APPROVED` won't re-push). Check
webhook.site for a POST containing `run_id`, `report`, and all 6 `results`. Remove the line from
`settings_override.py` and restart the worker again afterwards to restore the default.

**6.6 Reject path** (on a *different*, still-`PENDING_REVIEW` run): `.../analysis/OTHER_RUN_ID/reject/`
as the agronomist → `200`, `"status": "REJECTED"`, and **no** push logged for that run.

---

## 7. Role/permission matrix — quick reference

All verified against `agri/roles.py` + `agri/api/views.py`. ✅ = allowed, ❌ = blocked (code shown).

| Action | Technician (own farm) | Technician (other's farm) | Agronomist | Admin (non-superuser) | SuperAdmin |
|---|---|---|---|---|---|
| Create boundary | ✅ 201 | ❌ 403 | ❌ 403 (read-only) | ✅ 201 (any farm) | ✅ 201 |
| Approve/reject boundary | ❌ 403 | ❌ 403 | ✅ 200 | ✅ 200 | ✅ 200 |
| Upload capture (own farm) | ✅ 201 | — | ❌ 403 | — | — |
| Upload capture (**other's** farm) | — | ❌ 404 † | ❌ 403 | ❌ 404 † | ✅ 201 |
| Trigger analysis (approved boundary) | ✅ 201 | ❌ 403 | ❌ 403 (read-only) | ✅ 201 (any farm) | ✅ 201 |
| Approve/reject analysis run | ❌ 403 | ❌ 403 | ✅ 200 | ✅ 200 | ✅ 200 |
| List boundaries/runs | own only | own only | **all** (org-wide) | **all** (org-wide) | **all** |

† See §8 — this is a real, documented gap, not a typo.

---

## 8. Known limitations (read before assuming a "failure")

1. **Admin capture-upload gap.** Boundary-create and analysis-trigger check our own
   `can_contribute()` role logic, which lets **Admin** act on *any* farm. Capture **upload**
   (`CaptureUploadView`) instead reuses WebODM's own `get_and_check_project(..., 'change_project')` —
   a django-guardian **object-level** permission that's only auto-granted to a project's owner. A
   non-superuser Admin who doesn't own the farm gets **404** (not 403) trying to upload to it. Django
   **superusers** bypass this (Django's permission backend short-circuits for `is_superuser`), so
   SuperAdmin is unaffected. This is a real inconsistency versus the "Admin: anywhere" framing in
   `stages/stage-5-roles.md` — flagged here rather than hidden. Fix (not yet done): either grant
   guardian `change_project` to Admins on capture upload, or add an explicit role check like the
   other two endpoints.
2. **No boundary-drawing map UI yet.** Everything above is exercised via the API. The boundary geom
   in §4.2 comes from the capture's own extent (§3.5) specifically because there's no map to click
   points on yet — this is expected, not a workaround for a bug.
3. **Sequential, not parallel, analysis fan-out.** The plan describes a Celery `chord` running the
   five analyses in parallel; the current implementation runs them sequentially inside one task.
   Same inputs/outputs, just not parallelised yet.
4. **`docker-compose*.yml` doesn't forward `WO_AGRITRACK_PUSH_URL`** — §6.5's `settings_override.py`
   route is the only way to set it right now without editing compose files.
5. **Boundary approve/reject has no status precondition** (unlike analysis runs) — you can approve an
   already-`REJECTED` boundary. Not tested against in the automated suite as a negative case; noted
   for awareness.

---

## 9. Cleanup (optional)

```bash
docker compose -p webodm exec webapp python manage.py shell -c "
from django.contrib.auth.models import User
User.objects.filter(username__in=['pa_tech', 'pa_agro']).delete()
print('removed test users')
"
```
(Deleting `pa_tech` cascades to their farm/captures/boundaries/runs via FK `on_delete`.)
