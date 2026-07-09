# Deploying Precise Agric System to the CAI Dev Server

This deploys **your customized WebODM** (the `agri` app, `precise_agric` plugin, branding, Season
Progress, boundary reuse, and Farm terminology) onto the shared **Center of AI Contabo dev server**,
following that server's mandatory rules: only 80/443 exposed externally, Caddy owns all routing,
every project on a unique internal port, folderized under `~/cai-apps/`, Docker-first.

> **Live values as of this deployment:** domain `preciseagric.tawananyasha.com`, internal port `8011`
> (not `8010` — `8010` hit a persistent, misleading `"address already in use"` error that turned out to
> be the ports-merge bug described in Step 5, not a real conflict; switching ports was how it was first
> noticed, `!override` is what actually fixed it). VM: Contabo `161.97.176.218`. Register whatever port
> you end up using in the team's port registry before starting.

---

## Why a custom build (not the stock image)
`./webodm.sh start` normally *pulls* the stock `opendronemap/webodm_webapp` image from Docker Hub —
which does **not** contain your `agri/` app, branding, or the "Add Farm"/Season Progress bundles. You
must **build the image from your source** (Step 4). `./webodm.sh rebuild` does this
(`docker-compose.build.yml` → `build: .`) and tags the image so `start` uses your build, not the stock one.

## Why we bypass `./webodm.sh start`'s port/SSL handling
Two of WebODM's built-in behaviors conflict with this shared server and must be avoided:
1. **Port binding.** `docker-compose.yml` maps `${WO_PORT}:8000` with **no bind IP**, which publishes
   on `0.0.0.0` (every network interface) — violating the "only 80/443 exposed" rule. We add a small
   override file that binds to **`127.0.0.1`** only, so the app is unreachable except through Caddy.
2. **`--ssl`.** WebODM's own `--ssl` flag runs its own nginx + Let's Encrypt, which needs port 80/443
   for itself — but **Caddy already owns those ports** on this server. So we run WebODM in plain HTTP
   internally (default, `WO_SSL=NO`) and let **Caddy** be the only thing that terminates HTTPS. This is
   safe: `ALLOWED_HOSTS=['*']` and Django 2.2 don't need extra config to work correctly behind Caddy.

---

## 1. SSH in and prepare the folder
```bash
ssh root@<CONTABO_IP>
mkdir -p ~/cai-apps && cd ~/cai-apps
```

## 2. Clone the repo (currently public — no deploy key needed yet)
```bash
git clone https://github.com/kuzivakwashe0225/WebODM.git precise-agric
cd precise-agric
git checkout ISAIAH_development
chmod +x *.sh
```
> When you later move this to your own **private** repo, follow the CAI guide's Section 4
> (`ssh-keygen -t ed25519`, add as a **read-only Deploy Key** on the repo) instead of HTTPS.

## 3. Create `.env` (never committed — this file must stay local only)
```bash
cp .env.example .env
chmod 600 .env
nano .env
```
Set:
```
WO_HOST=preciseagric.tawananyasha.com
WO_PORT=8011
WO_DEBUG=NO
WO_DEV=NO
WO_SSL=NO
WO_DEFAULT_NODES=1
WO_SECRET_KEY=PASTE_A_LONG_RANDOM_STRING
# Required -- docker-compose.yml has no defaults for these; a blank value produces
# an "invalid spec: :/webodm/app/media:z: empty section between colons" error at startup:
WO_MEDIA_DIR=appmedia
WO_DB_DIR=dbdata
# Required -- without this, WO_BROKER defaults to redis://localhost (settings.py), and
# webapp/worker fail with "Error 111 connecting to localhost:6379. Connection refused."
# because Redis actually lives in the separate `broker` container:
WO_BROKER=redis://broker
WO_AGRITRACK_PUBLIC_BASE_URL=https://preciseagric.tawananyasha.com
WO_AGRITRACK_RESULTS_PUSH_URL=http://<agritrack-backend-host>:<port>/orthophoto/analysis/push
WO_AGRITRACK_OUTBOUND_API_KEY=<key AgriTrack's team gives you, for calls you make to them>
WO_AGRITRACK_INBOUND_API_KEY=<a strong secret you generate, shared with AgriTrack for their sync calls to you>
```
> **`webodm.sh` uses `source .env` (webodm.sh:32), not a docker-compose-style parse** — so any unescaped
> shell-special character in a value breaks it. A literal placeholder like `https://<agritrack-host>/...`
> left in `.env` fails with `.env: line 9: agritrack-host: No such file or directory` — bash reads
> `<agritrack-host` as input redirection. Always replace **every** `< >` placeholder with the real value
> (no angle brackets) before running any `./webodm.sh` command.

Generate the secret key:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

## 4. Build YOUR custom image
```bash
./webodm.sh rebuild
```
Builds the webapp image from your Dockerfile (bakes in the `agri` app, branding, and production
frontend bundles). Takes 15–40 min the first time.

## 5. Add the local-only port-binding override (one-time)
This is the file that keeps WebODM off the public interface — only `127.0.0.1:8010` is bound, so it's
reachable **only** via Caddy, never directly by IP:
```bash
cat > docker-compose.localbind.yml <<'EOF'
services:
  webapp:
    ports: !override
      - "127.0.0.1:${WO_PORT}:8000"
EOF
```
> **Must use `ports: !override`** (needs Compose v2.24+ — check with `docker compose version`), not a
> plain `ports:` list. `docker-compose.yml`'s base `webapp` service already defines
> `ports: - "${WO_PORT}:8000"` (unbound). Without `!override`, Compose **appends** this file's entry
> instead of replacing it, so the container ends up with *two* publish rules for the same host port —
> one unbound, one bound to `127.0.0.1`. The unbound one grabs the port first, and the second bind then
> fails with a misleading `"address already in use"` error that looks like an external conflict but
> isn't. (This cost a long debugging session the first time — see Troubleshooting below.)

## 6. Start the stack (bypassing `webodm.sh start`'s own port/SSL logic)
```bash
set -a; source .env; set +a
docker compose -p preciseagric \
  -f docker-compose.yml -f docker-compose.nodeodm.yml \
  -f docker-compose.localbind.yml up -d --scale node-odm=$WO_DEFAULT_NODES
```
Watch it come up:
```bash
docker logs -f webapp     # wait for "Congratulations!" then Ctrl-C
```
Confirm it's bound to localhost only (never externally reachable by IP):
```bash
ss -tulpn | grep 8010
# expect: 127.0.0.1:8010  (NOT 0.0.0.0:8010)
```

## 7. Set the admin password
```bash
./webodm.sh resetadminpassword "YourStrongPassword"
```

## 8. Wire up Caddy
```bash
sudo nano /etc/caddy/Caddyfile
```
Append:
```
preciseagric.cai-servers.com {
    reverse_proxy 127.0.0.1:8010
}
```
Reload:
```bash
sudo systemctl reload caddy
```
Caddy will automatically obtain and renew the HTTPS certificate for the subdomain.

## 9. Register the port
Add a row to your team's **Active Port Registry** (the portal you shared):
`8010 | Precise Agric System | <your name> | 127.0.0.1 | preciseagric.cai-servers.com | ONLINE`

## 10. Verify
Open **https://preciseagric.cai-servers.com**. You should see the green **Precise Agric System**
branding, an **Add Farm** button, **Season Progress** in the left menu, and the **Center for AI**
footer. Log in as `admin`, create a farm, upload an orthophoto (with capture date), draw + approve a
field, run analysis, confirm the heatmap and grid zones show correctly on the map.

---

## AgriTrack in production
Two independent, one-directional integrations — not a request/response pair:

- **Inbound** farm sync (AgriTrack → you): AgriTrack's backend calls
  `POST https://preciseagric.tawananyasha.com/api/v1/mobile/sync` with header
  `X-Api-Key: <WO_AGRITRACK_INBOUND_API_KEY>` (a secret **you** generate — e.g.
  `python3 -c "import secrets; print(secrets.token_urlsafe(40))"` — and hand to AgriTrack out-of-band,
  never in a doc/ticket). Body is the canonical sync payload (contract §6.2): `farmId`, `farmerId`,
  `farm: {name, location}`, `boundaries: {farm: <geojson>}`, `fields: [{fieldId, name, crop, area_ha,
  boundary: <geojson>}]`. Handled by `MobileSyncView` ([agri/agritrack/views.py](../agri/agritrack/views.py))
  → `sync_farm_payload` ([agri/agritrack/sync.py](../agri/agritrack/sync.py)), which upserts a
  `Project`/`AgriFarm`/`AgriField`, idempotently. `agritrack_farm_id`/`agritrack_field_id` are
  `IntegerField`s ([agri/models/agritrack.py](../agri/models/agritrack.py)) — AgriTrack must send numeric
  IDs.
- **Outbound** results push (you → AgriTrack): fires automatically when an agronomist approves an
  `AnalysisRun` in the WebODM UI (`AnalysisRunViewSet.approve` in
  [agri/api/views.py](../agri/api/views.py) → Celery task `push_analysis` → `push_orthophoto_results` in
  [agri/agritrack/results.py](../agri/agritrack/results.py)). Only fires if the run's boundary is linked
  to an AgriField that came from an inbound sync (nothing to report otherwise). POSTs to
  `WO_AGRITRACK_RESULTS_PUSH_URL` (AgriTrack's live `POST /orthophoto/analysis/push`, or
  `/orthophoto/analysis/push/batch` for batched — endpoint + `WO_AGRITRACK_OUTBOUND_API_KEY` both come
  **from** AgriTrack's team, not chosen by you) with header `X-Api-Key: <WO_AGRITRACK_OUTBOUND_API_KEY>`.
  Asset links (heatmap/index/weed maps) are built from `WO_AGRITRACK_PUBLIC_BASE_URL` (your own subdomain)
  so AgriTrack can fetch them over HTTPS.
  - **Schema gotcha (fixed):** AgriTrack's live endpoint rejects `summary` as an object (`422 "Input
    should be a valid string"`) — our report builder ([agri/analysis/report.py](../agri/analysis/report.py))
    produces it as a dict. `build_orthophoto_result_payload` now renders it to a short string via
    `_summary_to_text()` before sending. Caught by manually `curl`-ing the real endpoint with a dummy
    payload — worth doing again after any change to the report/push payload shape.
  - If AgriTrack's backend and your WebODM server turn out to be **the same physical VM** (same public
    IP, different port), and the push hangs/times out over the public IP, try `127.0.0.1:<port>` instead
    — some hosts don't support a box calling back into its own public IP (hairpin NAT).
- **Rotate the dev AgriTrack key** used during local testing (`atk_2184…`) — it was pushed to a public
  fork earlier in this branch's history and should be treated as compromised even though `.env` is now
  untracked going forward.
- **Manual connectivity test** (run from the server, before relying on a real approval):
  ```bash
  curl -v -X POST <WO_AGRITRACK_RESULTS_PUSH_URL> \
    -H "X-Api-Key: <WO_AGRITRACK_OUTBOUND_API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"field_id":1,"farm_id":1,"analysis_date":"2026-07-09","scope":"field","metrics":{},"outputs":{},"summary":"","recommendations":[],"ext_id":"connectivity-test"}'
  ```
  A `401`/`403` means the key is wrong; a `422` with field-specific errors means you're reaching AgriTrack
  and can iterate on payload shape; a hang/timeout means a network path problem (see hairpin-NAT note
  above).

## Maintenance
- **Logs:** `docker logs -f webapp` / `docker logs -f worker`.
- **Stop:** `docker compose -p preciseagric -f docker-compose.yml -f docker-compose.nodeodm.yml -f docker-compose.localbind.yml down`
- **Restart:** re-run the Step 6 `up -d` command (containers use `restart: unless-stopped`, so they
  also survive a server reboot automatically).
- **⚠️ Never run `./webodm.sh update`** — it pulls the stock image and would wipe out your custom
  build.
- **Backups:** back up `appmedia/` (all imagery/results) and `dbdata/` (all farms/analysis) — these
  hold every farm's data. A simple nightly cron `tar` + off-box copy is enough to start.
- **`.env` hygiene:** `.env` is gitignored and `chmod 600` — never `git add` it, never paste secrets
  into commit messages or PRs.

## Deploying a code change (rebuild)
Whenever `agri/`, `precise_agric`, or any other baked-in source changes on GitHub (only `coreplugins/`
is live-mounted — see `docker-compose.yml`'s `webapp.volumes`), the running container has to be rebuilt
from scratch; a `git pull` alone changes nothing on a running container.

```bash
cd ~/cai-apps/precise-agric
git pull origin ISAIAH_development     # must show real commits, not "Already up to date" --
                                        # if it says that, the change was never pushed to this branch
./webodm.sh rebuild                    # rebuilds the image from source; 15-40 min first time, faster after
docker rm -f webapp worker             # remove the two containers built from the (old) image
set -a; source .env; set +a
docker compose -p preciseagric \
  -f docker-compose.yml -f docker-compose.nodeodm.yml \
  -f docker-compose.localbind.yml up -d
docker logs -f webapp                  # wait for the ready message; confirm no traceback
```
`db`, `broker`, and `node-odm` don't need to be touched — only `webapp`/`worker` run your custom image.

## Troubleshooting log from the first deploy
Kept here because every one of these produced a confusing/misleading error message on the surface:

1. **`.env: line 9: agritrack-host: No such file or directory`** — a leftover `<placeholder>` with
   unescaped `< >` in `.env`, which `webodm.sh` loads via `source` (a real bash parse, not KV-only).
   Fix: replace every placeholder with a real value, no angle brackets.
2. **`invalid spec: :/webodm/app/media:z: empty section between colons`** — `WO_MEDIA_DIR`/`WO_DB_DIR`
   missing from `.env` (no defaults in `docker-compose.yml`). Fix: set both (see Step 3).
3. **`failed to bind host port 127.0.0.1:PORT/tcp: address already in use`, with nothing found in
   `ss`/`lsof`/`docker ps -a`** — not a real external conflict. Caused by `docker-compose.localbind.yml`
   *appending* a second `ports:` entry instead of replacing the base file's unbound one (see Step 5's
   `!override` note). Recreating with `docker rm -f webapp` alone doesn't fix it — the override file
   itself needs the `!override` tag.
4. **`webapp` container stuck with `NetworkSettings.Networks: {}`** (no network at all) — a side effect
   of issue #3: a container that fails during network/port setup can be left half-created. Fix:
   `docker rm -f webapp` and let Compose fully recreate it (don't just `docker start` an existing one).
5. **`could not translate host name "db" to address"`** — usually means the `webapp`/`worker` container
   isn't actually attached to the project's Docker network (see #4), not a DNS problem to chase on its
   own.
6. **`Error 111 connecting to localhost:6379. Connection refused`** — `WO_BROKER` missing from `.env`,
   so Django's cache/Celery config falls back to `redis://localhost` (`webodm/settings.py:326-339`)
   instead of the `broker` container. Fix: `WO_BROKER=redis://broker`.
7. **Changes not appearing on the hosted site** — either (a) not actually pushed to the branch the
   server tracks (`git log origin/<branch>..HEAD` on your dev machine will show unpushed commits if so),
   or (b) pushed but the server was never rebuilt (see "Deploying a code change" above) — a `git pull`
   with no rebuild changes nothing for anything baked into the image.

## Sizing note
Raw drone-image processing (NodeODM) is CPU/RAM/disk heavy; make sure this project's VPS allocation
(or its share of the shared server) has enough headroom for that alongside other teams' projects —
coordinate with whoever manages the CAI server's resource limits if large processing jobs are planned.
