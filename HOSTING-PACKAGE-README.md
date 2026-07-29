# MerebHub Backend Hosting Package

This zip contains only the backend/docker deployment files for the licensing stack.
It is configured for WooCommerce hosted at:

https://merebhub.com

Recommended public middleware URL:

https://license-api.merebhub.com

## Included

- `render.yaml` — Render Blueprint for Keygen API, Keygen worker, middleware, managed Postgres, and managed Key Value/Redis.
- `docker-compose.backend.yml` — backend-only Docker Compose stack for a VPS/Docker host.
- `.env.production.example` — copy to `.env.production` and fill real secrets for non-Render Docker deploys.
- `Dockerfile.keygen` — Keygen CE container customization.
- `middleware/` — FastAPI middleware source and Dockerfile.
- `DEPLOYMENT.md` — deployment checklist and commands.

## Not included

- Real secrets or `.env.production`.
- Local development Docker WordPress/MariaDB/certbot services.
- Local `.config/*.env` files.
- WordPress plugin files. Upload/update those separately on `merebhub.com` if the hosted WooCommerce site does not already have the patched plugins.

## Render quick start

1. Push this package contents to a private GitHub/GitLab repo.
2. In Render, create a Blueprint from `render.yaml`.
3. Fill every `sync: false` value in Render.
4. Set WooCommerce URL to `https://merebhub.com`.
5. Point your hosted WordPress plugin middleware URL to `https://license-api.merebhub.com` or the Render middleware URL.
6. Test `https://license-api.merebhub.com/health` before rebuilding the desktop app.

## Docker/VPS quick start

```bash
cp .env.production.example .env.production
# edit .env.production with real production secrets

docker compose -f docker-compose.backend.yml --env-file .env.production config --quiet
docker compose -f docker-compose.backend.yml --env-file .env.production up -d --build
```

Never commit or upload real secrets in `.env.production` to a public repo.
