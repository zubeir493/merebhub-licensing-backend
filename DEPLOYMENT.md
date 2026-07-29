# MerebHub Licensing Backend Deployment Guide

Use this repo for the backend only. WordPress/WooCommerce stays hosted separately on:

```text
https://merebhub.com
```

Current recommended deployment target:

```text
VPS + Docker Compose + VitoDeploy/Nginx reverse proxy
```

Full step-by-step VPS instructions are in:

```text
VPS-DEPLOYMENT.md
```

---

## What gets deployed on the VPS

`docker-compose.backend.yml` starts:

- `keygen-db` — PostgreSQL
- `keygen-redis` — Redis
- `keygen-api` — Keygen CE API
- `keygen-worker` — Keygen background worker
- `middleware-api` — public FastAPI licensing bridge

Only the middleware should be public.

---

## Public URLs

Use this public HTTPS URL everywhere outside Docker:

```text
https://license-api.merebhub.com
```

Use it for:

- desktop app `activation.config` base URL
- hosted WordPress plugin middleware URL
- any public activation/revalidation calls

Do not add `/v1` to the desktop app base URL.

Middleware talks to WooCommerce using:

```text
https://merebhub.com
```

---

## DNS

Add one Cloudflare record:

```text
Type: A
Name: license-api
Target: <your-vps-ip>
Proxy: DNS only first
```

You do not need `keygen.merebhub.com` unless you intentionally expose Keygen publicly.

---

## VPS quick deploy

On the VPS:

```bash
cd /opt/merebhub
git clone https://github.com/zubeir493/merebhub-licensing-backend.git
cd merebhub-licensing-backend
cp .env.production.example .env.production
nano .env.production
```

Validate and start:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production config --quiet
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production up -d --build
```

Run Keygen setup/migrations:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production run --rm keygen-api setup
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production up -d
```

Health checks on VPS:

```bash
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:3000/v1/health
```

Public health check:

```text
https://license-api.merebhub.com/health
```

---

## VitoDeploy coexistence

VitoDeploy can keep ports `80` and `443`.

This backend binds app ports to VPS localhost only:

```text
127.0.0.1:8000 -> middleware
127.0.0.1:3000 -> Keygen internal/debug only
```

Create a VitoDeploy/Nginx proxy for:

```text
license-api.merebhub.com -> http://127.0.0.1:8000
```

Enable SSL for `license-api.merebhub.com` in VitoDeploy.

---

## Env file

Use:

```text
.env.production.example
```

as the template. Never commit real `.env.production`.

Generate secrets on the VPS:

```bash
python3 -c "import uuid; print(uuid.uuid4())"
openssl rand -hex 32
openssl rand -hex 64
openssl rand -base64 32
```

---

## Product/policy setup

After the stack is healthy, create Keygen products and policies inside the Keygen service, then map policy UUIDs to WooCommerce products/variations using:

```text
_keygen_policy_id
```

---

## Updating later

```bash
cd /opt/merebhub/merebhub-licensing-backend
git pull
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production up -d --build
```

---

## Keep private

Do not publicly expose:

- PostgreSQL
- Redis
- Keygen API, unless intentionally needed

Only expose:

```text
https://license-api.merebhub.com
```
