# Environment — running the dev stack

The stack runs in Docker. On this Windows machine there are three gotchas that cost us time;
they're all resolved now, but **read this before touching the stack**.

## Gotchas (Windows)

1. **Don't run `webodm.sh` from PowerShell.** It's a bash script; in PowerShell it silently does
   nothing (no output). Use **Git Bash**, or drive Docker directly (below).
2. **CRLF line endings crash the containers when the source is mounted.** Git checked out `.sh`
   files with `\r\n`; the mounted scripts fail with `/bin/bash^M: bad interpreter` (exit 126,
   crash-loop). Fix: all repo `*.sh` were converted to LF. Keep them LF — a `.gitattributes` with
   `*.sh text eol=lf` is recommended. (MSYS `sed 's/\r$//'` does NOT work; use Python byte-replace.)
3. **Every `--dev` boot is slow, not just the first — this was a mistaken claim, corrected here.**
   `docker-compose.dev.yml`'s entrypoint unconditionally appends `--setup-devenv` on every start (no
   "already done" check), and `start.sh`'s `--setup-devenv` block always re-runs, in order:
   `git submodule update`, `npm install` (root + `nodeodm/external/NodeODM`), `pip install -r
   requirements.txt`, `python manage.py translate build --safe`, then `webpack --watch`.
   `translate build --safe` has **no caching** — `--safe` only means "skip invalid languages," not
   "skip unchanged ones" (confirmed by reading `app/management/commands/translate.py`) — so it
   recompiles every `.po`→`.mo` for every locale, every boot. This is the single slowest step.
   - `pip install` becomes fast once `/webodm/venv` is a **named** volume (fixed — see `devvenv` in
     `docker-compose.dev.yml`) so packages persist across container recreation.
   - `npm install`/`git submodule` are fast once already present, since `/webodm` itself is a bind
     mount (host files persist regardless of container recreation).
**Fixes applied (2026-07-06) so future boots are fast:**
1. `docker-compose.dev.yml`: `/webodm/venv` changed from an anonymous volume to a **named** volume
   (`devvenv`) — installed pip packages now persist across container recreation.
2. `start.sh`: the `--setup-devenv` block (submodules, npm, pip, `translate build`) is gated behind a
   marker `/webodm/venv/.devenv_done` on that persistent volume — it runs **once**, then is skipped.
   Force a redo with `rm /webodm/venv/.devenv_done` or `WO_FORCE_DEVENV=YES`. `webpack --watch` still
   runs every boot (it's the live-reload watcher).
3. `app/plugins/functions.py` + `docker-              compose.dev.yml`: `WO_SKIP_PLUGIN_REBUILD=YES` makes
   `boot()` trust existing plugin builds instead of rebuilding all plugin JSX every boot (the mtime
   check is unreliable over the Windows bind mount). Only plugins with **no** build output are built.

Net effect: the **next** restart still does the full setup **once** (fresh named volume), then every
restart after that skips setup + plugin rebuild → boots in ~1–2 min (just webpack's initial compile +
migrate + runserver) instead of 10–15+.

**How to restart cleanly** (from any shell that can run docker; PowerShell is fine for this):
```
docker compose -p webodm -f docker-compose.yml -f docker-compose.nodeodm.yml -f docker-compose.dev.yml up -d
```
(with `WO_DEV=YES WO_DEBUG=YES` env if not already set). Do NOT `down -v` — that deletes the named
volume and the DB volume, forcing full setup again.

## How the stack is currently run

Because PowerShell can't run `webodm.sh`, the stack is brought up with **docker compose directly**
(project name `webodm`), replicating exactly what `webodm.sh start --dev` assembles:

```bash
WO_DEV=YES WO_DEBUG=YES docker compose -p webodm \
  -f docker-compose.yml -f docker-compose.nodeodm.yml -f docker-compose.dev.yml up -d
```

- Dev server = Django `runserver` on :8000 — **auto-reloads on `.py` edits**.
- `webpack --watch` rebuilds JS bundles on change.
- The `.:/webodm` mount (from `docker-compose.dev.yml`) is what makes host edits visible in the
  container. Verify with: `docker inspect webapp --format '{{range .Mounts}}{{.Destination}} {{end}}'`
  → must include `/webodm`.

## Running commands / migrations

```bash
docker compose -p webodm exec webapp python manage.py <command>
# on Git Bash, prefix MSYS_NO_PATHCONV=1 if a path arg gets mangled
```

## Verifying the app

```bash
curl -s http://localhost:8000/login/ | grep "Precise Agric System"   # brand check
docker logs --tail 40 webapp                                          # boot / errors
```

## New gotcha: `./webodm.sh restart` drops dev mode (found 2026-07-06)

`restart` in `webodm.sh` is literally `down` + `start` with **no flags forwarded** — it does not
remember `--dev`. Running plain `./webodm.sh restart` on an already-`--dev` stack brings it back up
**without** the `docker-compose.dev.yml` overlay: no `.:/webodm` source bind-mount, no
`WO_SKIP_PLUGIN_REBUILD`, none of the AgriTrack env vars — i.e. back on the **baked image**, all
host source edits invisible. Confirmed via `docker inspect webapp` showing only the `coreplugins`
bind-mount and `appmedia` volume, no `/webodm` mount.

**Always restart a dev stack with:**
```bash
docker compose --profile dev down && ./webodm.sh start --dev
```
never bare `./webodm.sh restart`. Verify afterwards with
`docker inspect webapp --format '{{range .Mounts}}{{.Destination}} {{end}}'` → must include
`/webodm`, and `docker exec webapp printenv | grep AGRITRACK` if AgriTrack env vars matter for what
you're testing.

**Unresolved sub-issue, flagged not fixed:** even via the correct `start --dev` path, the
`--setup-devenv` block re-ran its full `translate build` over every locale (~10+ min) on this
restart, despite the `.devenv_done` marker gate (§ above) that's supposed to skip it on a warm
`devvenv` volume. Root cause not yet confirmed — possibly the marker not surviving whatever state
change happened during the bad plain-`restart` detour, possibly something else. Next time this
happens, check `docker exec webapp ls -la /webodm/venv/.devenv_done` before assuming the fix
regressed.

## Known non-fatal issues

- Two core plugins fail their JSX build (pre-existing): `plant_height` (`Can't resolve
  ./VegetationIndices.scss`) and `weed_detect` (`Can't resolve ./ObjDetectPanel.scss`). The app runs
  fine; only those plugins' frontend bundles are affected. Will be fixed when Stage 3 reuses them.
