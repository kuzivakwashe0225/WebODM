# Deploying Precise Agric System to the CAI Dev Server

This deploys **your customized WebODM** (the `agri` app, `precise_agric` plugin, branding, Season
Progress, boundary reuse, and Farm terminology) onto the shared **Center of AI Contabo dev server**,
following that server's mandatory rules: only 80/443 exposed externally, Caddy owns all routing,
every project on a unique internal port, folderized under `~/cai-apps/`, Docker-first.

> Replace **`preciseagric.cai-servers.com`** below with your real subdomain once you have it (the CAI
> guide's own examples used `cai-servers.com`/`tawananyasha.com` — confirm the actual domain with your
> team). Internal port used throughout: **`8010`** — **register this in the team's port registry**
> before starting (8001 and 36981 were already taken at time of writing).

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
WO_HOST=preciseagric.cai-servers.com
WO_PORT=8010
WO_DEBUG=NO
WO_DEV=NO
WO_SSL=NO
WO_DEFAULT_NODES=1
WO_SECRET_KEY=PASTE_A_LONG_RANDOM_STRING
WO_AGRITRACK_PUBLIC_BASE_URL=https://preciseagric.cai-servers.com
# Real AgriTrack production values (do NOT reuse the ngrok dev key from earlier testing):
WO_AGRITRACK_RESULTS_PUSH_URL=https://<agritrack-host>/orthophoto/analysis/push
WO_AGRITRACK_OUTBOUND_API_KEY=<real key from AgriTrack>
WO_AGRITRACK_INBOUND_API_KEY=<a strong secret you choose, shared with AgriTrack for their sync calls>
```
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
version: '2.1'
services:
  webapp:
    ports:
      - "127.0.0.1:${WO_PORT}:8000"
EOF
```

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
- **Inbound** farm sync: AgriTrack calls `POST https://preciseagric.cai-servers.com/api/v1/mobile/sync`
  with `X-Api-Key: <WO_AGRITRACK_INBOUND_API_KEY>`.
- **Outbound** results push: your server posts to `WO_AGRITRACK_RESULTS_PUSH_URL` with
  `WO_AGRITRACK_OUTBOUND_API_KEY`; asset links use `WO_AGRITRACK_PUBLIC_BASE_URL` (your subdomain) so
  AgriTrack can fetch heatmap/index images over HTTPS.
- **Rotate the dev AgriTrack key** used during local testing (`atk_2184…`) — it was pushed to a public
  fork earlier in this branch's history and should be treated as compromised even though `.env` is now
  untracked going forward.

## Maintenance
- **Logs:** `docker logs -f webapp` / `docker logs -f worker`.
- **Stop:** `docker compose -p preciseagric -f docker-compose.yml -f docker-compose.nodeodm.yml -f docker-compose.localbind.yml down`
- **Restart:** re-run the Step 6 `up -d` command (containers use `restart: unless-stopped`, so they
  also survive a server reboot automatically).
- **⚠️ Never run `./webodm.sh update`** — it pulls the stock image and would wipe out your custom
  build. To deploy new changes: `git pull origin ISAIAH_development` → `./webodm.sh rebuild` → re-run
  Step 6's `up -d`.
- **Backups:** back up `appmedia/` (all imagery/results) and `dbdata/` (all farms/analysis) — these
  hold every farm's data. A simple nightly cron `tar` + off-box copy is enough to start.
- **`.env` hygiene:** `.env` is gitignored and `chmod 600` — never `git add` it, never paste secrets
  into commit messages or PRs.

## Sizing note
Raw drone-image processing (NodeODM) is CPU/RAM/disk heavy; make sure this project's VPS allocation
(or its share of the shared server) has enough headroom for that alongside other teams' projects —
coordinate with whoever manages the CAI server's resource limits if large processing jobs are planned.
