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
cp crypt.conf.example crypt.conf
# Edit .env + crypt.conf

mkdir -p data
docker compose up --build -d
```

App listens on `http://localhost:8000`.

## Disk guard

Before accepting uploads (and again before each queued job runs), the API estimates staging space:

- incoming file size
- plus another full copy if **Split** is on (parts written while source still exists)
- plus a small encrypt cushion
- always leaving `DISK_RESERVE_BYTES` free (default 2 GiB)

If it will not fit, upload returns **HTTP 507** with a clear message.

Downloads use a soft estimate (512 MiB × link count) plus the same reserve.

## Layout

```text
tg-app-cloud/
|_ app/                 FastAPI + static UI
|_ data/                uploads, downloads, persisted profile.session
|_ Dockerfile
|_ docker-compose.yml
|_ .env.example
|_ crypt.conf.example
|_ entrypoint.sh
```

Authorize once from the UI after deploy; the bot session is copied into `data/profile.session` so rebuilds keep it.
