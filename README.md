# MerebHub Licensing Backend

Backend/docker deployment package for MerebHub licensing.

WooCommerce production site:

```text
https://merebhub.com
```

Recommended public middleware URL:

```text
https://license-api.merebhub.com
```

## Contents

- `render.yaml` — Render Blueprint for Keygen API, Keygen worker, FastAPI middleware, managed Postgres, and managed Key Value/Redis.
- `docker-compose.backend.yml` — backend-only Docker Compose stack for VPS/Docker hosting.
- `.env.production.example` — placeholder-only environment template. Copy to `.env.production` and fill real values for non-Render Docker deploys.
- `Dockerfile.keygen` — Keygen CE image customization.
- `middleware/` — FastAPI middleware source and Dockerfile.
- `DEPLOYMENT.md` — deployment checklist and commands.

## Not included

- Real secrets or `.env.production`.
- Local WordPress/MariaDB/certbot Docker services.
- Local `.config/*.env` files.
- WordPress plugin files. Upload/update those separately on `merebhub.com`.

## Render quick start

1. Create a Render Blueprint from `render.yaml`.
2. Fill every `sync: false` value in Render.
3. Use `https://merebhub.com` for `WOOCOMMERCE_URL`.
4. Point the hosted WooCommerce plugin middleware URL to your public middleware URL.
5. Check `/health` before rebuilding the desktop app.
