# tg-app-cloud

Dockerized web UI for the same Telegram encrypt/upload and link-download flows as the desktop `tg-app`, with a one-at-a-time job queue and disk-space checks.

Meant to run behind **Coolify** (or similar) with your existing Cloudflare tunnel / wildcard DNS. Auth is **Cloudflare Access** on the app hostname — there is no in-app login.

## Security

- Do not publish this app’s port directly to the public internet; let Coolify + Cloudflare terminate HTTPS.
- Zero Trust → Access → Applications → add **only** this hostname (e.g. `tg.example.com`), not the whole `*.example.com` unless you want Access on every subdomain.

## Coolify

1. Deploy this compose (or Dockerfile) as a Coolify service.
2. Point a subdomain at it (your existing proxied wildcard is fine).
3. Set env vars from `.env.example` (Telegram + disk reserve).
4. Mount/persist `data/` and provide `crypt.conf`.
5. Cloudflare Access on that specific hostname.

## Local run

```bash
cd tg-app-cloud
cp .env.example .env
# Edit .env (Telegram API credentials)

mkdir -p data
docker compose up --build -d
```

App listens on `http://localhost:8000`.

All runtime state lives under the `/data` volume:

```text
data/
|_ downloads/          finished Telegram downloads
|_ uploads/            staging for uploads
|_ profile.session     Telegram auth (survives rebuild)
|_ crypt.conf          rclone crypt config (survives rebuild)
```

## Ugreen NAS mount (required for persistence)

Create/use **Shared Folder → docker → tg-app** (as in Files), then in the Docker container volume settings map that folder to **`/data`**:

| Host (NAS) | Container |
|---|---|
| `…/docker/tg-app` (your Shared Folder path) | `/data` |

Also set env:

- `DATA_DIR=/data`
- `CRYPT_CONFIG=/data/crypt.conf`

Do **not** rely on anonymous Docker volumes for `/data` — rebuild/redeploy will look empty in Files even if old container layers still have files.

After mounting correctly you should see in Files:

`docker/tg-app/downloads/…`, `crypt.conf`, `profile.session`.

Authorize once and save crypt.conf once; both persist across image updates as long as `/data` stays mounted.

## Disk guard

Before accepting uploads (and again before each queued job runs), the API estimates staging space:

- incoming file size
- plus another full copy if **Split** is on (parts written while source still exists)
- plus a small encrypt cushion
- always leaving `DISK_RESERVE_BYTES` free (default 2 GiB)

If it will not fit, upload returns **HTTP 507** with a clear message.

Downloads use a soft estimate (512 MiB × link count) plus the same reserve.

## Large uploads (Cloudflare)

The UI uploads files in **8 MiB chunks** (`/api/upload/init` → `/chunk` → `/complete`). That stays under Cloudflare’s ~100 MiB request body limit so multi‑GB videos work through Access / tunnel. Do not rely on a single multipart POST for large files behind Cloudflare.

## Layout

```text
tg-app-cloud/            # Docker build context root
|_ app/                 FastAPI + static UI
|_ tg-upload/           # Bundled upload backend (copied into image)
|_ data/                uploads, downloads, persisted profile.session
|_ Dockerfile
|_ docker-compose.yml
|_ .env.example
|_ crypt.conf.example
|_ entrypoint.sh
```

Authorize once from the UI after deploy; the bot session is copied into `data/profile.session` so rebuilds keep it.
