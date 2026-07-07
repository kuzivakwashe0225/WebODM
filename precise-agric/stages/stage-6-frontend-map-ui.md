# Stage 6 — Frontend map UI (`coreplugins/precise_agric`) ✅ (2026-07-06)

The first real UI for the agri workflow, built as a WebODM plugin (`coreplugins/precise_agric/`)
rather than a modification of core `app/` — per the "prefer a plugin for optional/toggleable
features" convention.

## What it is

- **`plugin.py`** registers `main.js` (plain script, `include_js_files()`) and `app.jsx` (webpack
  bundle, `build_jsx_components()`).
- **`public/app.jsx`** — `PreciseAgricControl` (an `L.Control.extend` button, list icon, top-right)
  toggles a side panel (`PreciseAgricPanel.jsx`). The `App` class owns the panel and a
  **reused `CropButton`** (`webodm/components/CropButton.jsx`) for polygon drawing — not a custom
  hand-rolled tool (see "Errors and fixes" below for why).
- **`public/PreciseAgricPanel.jsx`** — the React panel: boundary list with status badges
  (DRAFT/APPROVED/REJECTED), Approve/Reject actions, Delete (with confirm), Start Analysis once
  APPROVED, run-result summary with its own Approve/Reject, a "Show boundaries on map" checkbox
  (default on), and the pending-boundary name+save form.
- **`public/main.js`** adds **"Upload Orthophoto (Precise Agric)"** to WebODM's native Import
  dropdown via `PluginsAPI.Dashboard.addImportTaskItem` (the same mount point used by "Assets" /
  "External Data" — found by reading `ProjectListItem.jsx:371`). File picker → prompt for a name →
  `POST /api/agri/captures/` (multipart) → refreshes the task list.
- **`public/app.scss` / `PreciseAgricPanel.scss`** — panel and boundary-overlay styling.

## Boundary overlays

Every boundary (not just APPROVED) renders as a colored, **permanently labeled** Leaflet layer
(`L.geoJSON(...).bindTooltip(name, {permanent: true})`): DRAFT = grey, APPROVED = green,
REJECTED = red. Overlays are plain Leaflet layers owned by `App` (`app.jsx`), so they persist on
the map independent of the side panel being open or closed.

## Errors and fixes (kept here since they explain *why* the code looks the way it does)

- **Empty `coreplugins/precise_agric/__init__.py`** → plugin loader raised
  `AttributeError: module 'coreplugins.precise_agric' has no attribute 'Plugin'`. The loader does
  `getattr(import_module("coreplugins.precise_agric"), "Plugin")` on the **package**, not the
  `plugin.py` submodule. Fixed: `from .plugin import *`, matching every other coreplugin.
- **Custom hand-rolled polygon drawing** (`map.on('click', ...)`) silently failed to capture
  clicks. Corrected by the user ("why not we use the crop button already there") — replaced with
  `CropButton`, WebODM's own reusable, non-destructive polygon-drawing control (confirmed via
  `coreplugins/measure` and `coreplugins/contours` as the established `L.Control.extend` pattern).
- **`.precise-agric-panel-container { position: absolute; }` with no size/offsets** collapsed to a
  zero-size point, breaking the child panel's `top/right` positioning — this was the actual root
  cause of "the Precise Agric button doesn't display anything." Fixed with a full-viewport
  `top/left/right/bottom: 0; pointer-events: none;` container, `pointer-events: auto` on the panel.
- **File `<input type=file>` never appended to the DOM before `.click()`** — silently did nothing
  in some browsers. Fixed by appending a hidden input to `document.body`, cleaned up in `onchange`.
- **`CropButton`'s `color` prop only tints the drawn polygon, not the button icon** (confirmed by
  reading its source) — fixed the "draw button isn't green" complaint via a CSS attribute selector
  on the tooltip text instead: `a[title="Draw Field Boundary"] { color: #2e7d32 !important; }`.

## Deliberately deferred

- **In-place boundary shape editing** (dragging existing vertices) — delete + redraw is the
  current workaround. Would need a heavier tool (Leaflet.Editable / Geoman).
- Role-aware UI gating (buttons are shown to everyone; the API still enforces permissions
  server-side per [stage-5-roles.md](stage-5-roles.md), so this is a UX polish gap, not a security
  gap).

## Verification

Confirmed via served-bundle inspection, not assumption: after each rebuild, the actual served
`app.js` was fetched and grepped for the new strings (e.g. `"Show boundaries on map"`,
`"boundary-label"`) to rule out a stale cached bundle. Boundary render path was confirmed against
real DB data (`Boundary.objects.all()`) and the user's own browser console output before being
declared fixed.
