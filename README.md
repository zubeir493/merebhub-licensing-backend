# MerebHub Licensing Backend

Backend/docker deployment package for MerebHub licensing.

WooCommerce production site:

```text
https://merebhub.com
```

Public middleware URL:

```text
https://license-api.merebhub.com
```

## Recommended deploy target

Use a VPS with Docker Compose. If the VPS already runs VitoDeploy, keep VitoDeploy handling ports `80`/`443` and proxy `license-api.merebhub.com` to:

```text
http://127.0.0.1:8000
```

Start here:

```text
VPS-DEPLOYMENT.md
```

## Contents

- `docker-compose.backend.yml` — backend-only VPS Docker Compose stack.
- `.env.production.example` — placeholder-only VPS env template. Copy to `.env.production` on the VPS.
- `Dockerfile.keygen` — Keygen CE image customization.
- `Dockerfile` — default middleware Dockerfile for simple hosts.
- `middleware/` — FastAPI middleware source and Dockerfile.
- `DEPLOYMENT.md` — short deployment overview.
- `VPS-DEPLOYMENT.md` — full VPS + VitoDeploy instructions.
- `render.yaml` — legacy/optional Render Blueprint, not the current recommended path.

## Not included

- Real secrets or `.env.production`.
- Local WordPress/MariaDB/certbot Docker services.
- Local `.config/*.env` files.
- WordPress plugin files.

## VPS quick start

```bash
cd /opt/merebhub
git clone https://github.com/zubeir493/merebhub-licensing-backend.git
cd merebhub-licensing-backend
cp .env.production.example .env.production
nano .env.production
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production up -d --build
```

Then proxy:

```text
license-api.merebhub.com -> http://127.0.0.1:8000
```
