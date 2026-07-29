# VPS Deployment Guide — MerebHub Licensing Backend

This guide deploys the complete backend on the same VPS that already runs VitoDeploy.

The stack includes:

- FastAPI middleware
- Keygen CE API
- Keygen worker
- PostgreSQL
- Redis

WordPress/WooCommerce stays on:

```text
https://merebhub.com
```

Only the middleware is public:

```text
https://license-api.merebhub.com
```

Keygen, Postgres, and Redis stay private inside Docker.

---

## 1. Cloudflare DNS

Add one record:

```text
Type: A
Name: license-api
Target: <your-vps-ip>
Proxy: DNS only first
```

Do not create `keygen.merebhub.com` unless you intentionally want to expose Keygen publicly.

---

## 2. SSH into the VPS

```bash
ssh root@<your-vps-ip>
```

Use your normal sudo user if you do not log in as root.

---

## 3. Install Docker + Docker Compose plugin

If Docker is already installed by VitoDeploy, first check:

```bash
docker --version
docker compose version
```

If both work, skip to step 4.

For Ubuntu/Debian VPS:

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg git openssl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' "$(dpkg --print-architecture)" "$VERSION_CODENAME" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Verify:

```bash
docker --version
docker compose version
```

---

## 4. Clone the backend repo

```bash
sudo mkdir -p /opt/merebhub
sudo chown -R "$USER":"$USER" /opt/merebhub
cd /opt/merebhub
git clone https://github.com/zubeir493/merebhub-licensing-backend.git
cd merebhub-licensing-backend
```

If the repo is private, use your GitHub credentials/token when prompted.

---

## 5. Create production env file

```bash
cp .env.production.example .env.production
nano .env.production
```

Generate values on the VPS:

```bash
python3 -c "import uuid; print(uuid.uuid4())"
openssl rand -hex 32
openssl rand -hex 64
openssl rand -base64 32
```

Fill these in `.env.production`:

```env
KEYGEN_ACCOUNT_ID=<uuid-from-python>
KEYGEN_ADMIN_EMAIL=admin@merebhub.com
KEYGEN_ADMIN_PASSWORD=<openssl-rand-hex-32>
KEYGEN_ADMIN_TOKEN=<openssl-rand-hex-64>
SECRET_KEY_BASE=<openssl-rand-hex-64>
ENCRYPTION_PRIMARY_KEY=<openssl-rand-base64-32>
ENCRYPTION_DETERMINISTIC_KEY=<openssl-rand-base64-32>
ENCRYPTION_KEY_DERIVATION_SALT=<openssl-rand-base64-32>
POSTGRES_PASSWORD=<openssl-rand-hex-32>
REDIS_PASSWORD=<openssl-rand-hex-32>
```

Important: update the matching passwords inside URL values too:

```env
DATABASE_URL=postgres://keygen_pg_user:<same-postgres-password>@keygen-db:5432/keygen_production
REDIS_URL=redis://:<same-redis-password>@keygen-redis:6379/1
```

Keep these as shown:

```env
PUBLIC_MIDDLEWARE_URL=https://license-api.merebhub.com
WORDPRESS_URL=https://merebhub.com
WOOCOMMERCE_URL=https://merebhub.com
KEYGEN_HOST=keygen-api
KEYGEN_HOSTS=keygen-api,keygen-api:3000,localhost:3000,127.0.0.1:3000
KEYGEN_API_URL=http://keygen-api:3000/v1
MIDDLEWARE_PORT=8000
KEYGEN_PORT=3000
```

---

## 6. Start the backend stack

Use a fixed Compose project name so it does not collide with VitoDeploy containers:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production config --quiet
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production up -d --build
```

Check containers:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml ps
```

Check logs:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml logs -f --tail=100
```

---

## 7. Run Keygen setup/migrations

After containers are up:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production run --rm keygen-api setup
```

Then restart:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production up -d
```

---

## 8. Local VPS health checks

Run on the VPS:

```bash
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:3000/v1/health
```

Expected:

- Middleware: HTTP 200 JSON
- Keygen: HTTP 204 or healthy response

---

## 9. Connect VitoDeploy / reverse proxy

Because VitoDeploy already uses ports `80` and `443`, do not bind this app directly to those ports.

The Compose file binds middleware to VPS localhost only:

```text
127.0.0.1:8000
```

In VitoDeploy, create a site/proxy for:

```text
license-api.merebhub.com
```

Proxy target:

```text
http://127.0.0.1:8000
```

Enable SSL certificate for:

```text
license-api.merebhub.com
```

If you manage Nginx manually instead, use:

```nginx
server {
    listen 80;
    server_name license-api.merebhub.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then use Certbot/VitoDeploy SSL.

---

## 10. Public health check

From your computer/browser:

```text
https://license-api.merebhub.com/health
```

Expected:

```json
{"status":"healthy"}
```

---

## 11. Create Keygen product/policies

Once health checks pass, create your product/policies inside the running Keygen service. Start with a shell:

```bash
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production exec keygen-api bash
```

Then use Keygen Rails/API commands from inside the container. At minimum you need:

- one Product UUID
- one Policy UUID per WooCommerce product/variation license type

After creating policies, save each Policy UUID into the matching WooCommerce product/variation meta:

```text
_keygen_policy_id
```

---

## 12. App/backend URL

Use this URL everywhere outside Docker:

```text
https://license-api.merebhub.com
```

No `/v1` for the desktop app base URL.

---

## 13. Updating later

```bash
cd /opt/merebhub/merebhub-licensing-backend
git pull
docker compose -p merebhub-licensing -f docker-compose.backend.yml --env-file .env.production up -d --build
```

---

## 14. Backup commands

Back up Postgres:

```bash
mkdir -p ~/merebhub-backups
docker compose -p merebhub-licensing -f docker-compose.backend.yml exec -T keygen-db pg_dump -U keygen_pg_user keygen_production > ~/merebhub-backups/keygen_$(date +%F_%H%M).sql
```

Back up Docker volumes using your VPS backup system as well.
