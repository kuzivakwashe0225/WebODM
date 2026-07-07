# Stage 4 — Agronomist review → AgriTrack push ✅ (28/28 suite green, 2026-07-06)

Reviewers approve or reject a `PENDING_REVIEW` analysis run; **approval fires the AgriTrack push**
(outbound webhook, per the plan's `tasknotification` pattern). Stubbed by default: the push is a
no-op until a URL is configured.

## Implementation

- **Endpoints** (`agri/api/views.py`):
  - `POST /api/agri/analysis/{id}/approve/` — reviewers only; run must be `PENDING_REVIEW`;
    sets `APPROVED` + `reviewed_by`, then dispatches the push (Celery task, 3 retries/30 s).
  - `POST /api/agri/analysis/{id}/reject/` — same gates; sets `REJECTED`; **no push**.
- **Push module** (`agri/push.py`): builds payload (run/capture/farm/boundary ids, index used,
  approver, aggregated **report**, all six results' stats + asset paths) and POSTs it to
  `settings.AGRITRACK_PUSH_URL`. Returns `False` (skip) when the URL is unset.
- **Config**: `AGRITRACK_PUSH_URL` in `webodm/settings.py`, from env `WO_AGRITRACK_PUSH_URL`
  (default `None` = disabled). Note: the docker-compose files don't forward this env var yet — set it
  via `settings_override.py`/`--settings` or add it to the compose environment when going live.

## Tests
`test_analysis_review_approve_and_push` (payload has report summary + 6 results; POST mocked),
`test_analysis_review_reject` (no push), `test_analysis_review_requires_reviewer` (technician 403),
`test_analysis_review_requires_pending_review` (400), `test_push_skipped_without_url`.

## Deferred
- Review UI (frontend) — the API is review-complete; screens come with the UI pass.
- Real AgriTrack contract (auth headers, exact schema) — payload is our documented shape until the
  mobile API contract is provided.
