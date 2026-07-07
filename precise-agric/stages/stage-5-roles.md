# Stage 5 — Roles & permission hardening ✅ (28/28 suite green, 2026-07-06; roles seeded in live DB)

Four seeded role groups + per-action gating + org-wide reviewer visibility, per plan §9.

## Roles (`agri/roles.py`, seeded in `app/boot.py` via `setup_roles()`)

| Role | Can do |
|---|---|
| **SuperAdmin** | Everything (Django superusers count as SuperAdmin) |
| **Admin** | Contribute anywhere (upload/draw/trigger) + review/approve + org-wide visibility |
| **Technician** | Upload captures, draw boundaries, trigger analysis — **own projects only; cannot approve** |
| **Agronomist** | **Read-only** for contribution actions; **approves/rejects** boundaries & analysis runs; org-wide visibility |
| *(no role — legacy)* | Owner-scoped contributor; can never approve |

Assign users to roles at `/admin/auth/user/` (Groups). Helpers: `get_role`, `is_reviewer`
(SuperAdmin/Admin/Agronomist), `can_contribute(user, project)`.

## Enforcement (`agri/api/views.py`)
- Boundary **create**: requires contribute rights on the capture's project — closes the hole where
  any authed user could attach a boundary to anyone's task.
- Boundary/Analysis **approve/reject**: `is_reviewer` only (403 otherwise).
- Capture **upload** & analysis **trigger**: contribute rights (Agronomist denied).
- **Visibility**: reviewers see all boundaries/runs (org-wide, per locked decision); others see
  their own projects.

## Tests
`test_roles_seeded`, `test_reviewer_sees_all_boundaries_and_can_reject`,
`test_boundary_create_scoped_to_project_rights`, `test_agronomist_is_read_only`, plus the
updated `test_boundary_create_list_approve` (technician approve → 403; agronomist → 200) and
`test_analysis_review_requires_reviewer`.

## Deliberately deferred
- Per-farm assignment (locked decision: org-wide reviewers for v1).
- Role-aware UI gating (frontend pass).
