"""
FastAPI Middleware Integration Service
======================================
Handles Chapa payment webhooks, verifies transactions out-of-band,
enqueues license provisioning jobs to Redis Streams, and serves
a health-check endpoint for Docker orchestration.

Environment Variables:
    REDIS_URL                    Redis connection string (default: redis://localhost:6379/1)
    KEYGEN_ACCOUNT_ID            Keygen account UUID
    KEYGEN_API_URL               Keygen API base URL (default: http://keygen-api:3000/v1)
    KEYGEN_ADMIN_TOKEN           Keygen admin bearer token for license provisioning
    WOOCOMMERCE_URL              WooCommerce site URL (default: https://store.marketplace.et)
    WOOCOMMERCE_CONSUMER_KEY     WooCommerce REST API consumer key
    WOOCOMMERCE_CONSUMER_SECRET  WooCommerce REST API consumer secret
    CHAPA_SECRET_KEY             Chapa live/test secret key
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from licensing import (
    provision_license_direct,
    validate_license_key,
    register_machine,
    activate_via_qr,
    checkout_offline_license,
    validate_offline_lic,
)

from releases import (
    create_release,
    list_releases,
    verify_license_entitlement,
    upload_release_artifact,
    get_release_download_path,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("middleware")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/1")
KEYGEN_ACCOUNT_ID: str = os.getenv("KEYGEN_ACCOUNT_ID", "")
_keygen_api_hostport: str = os.getenv("KEYGEN_API_HOSTPORT", "")
KEYGEN_API_URL: str = os.getenv("KEYGEN_API_URL") or (
    f"http://{_keygen_api_hostport}/v1" if _keygen_api_hostport else "http://keygen-api:3000/v1"
)
KEYGEN_ADMIN_TOKEN: str = os.getenv("KEYGEN_ADMIN_TOKEN", "")
WOOCOMMERCE_URL: str = os.getenv("WOOCOMMERCE_URL", "https://merebhub.com")
WOOCOMMERCE_CONSUMER_KEY: str = os.getenv("WOOCOMMERCE_CONSUMER_KEY", "")
WOOCOMMERCE_CONSUMER_SECRET: str = os.getenv("WOOCOMMERCE_CONSUMER_SECRET", "")
CHAPA_SECRET_KEY: str = os.getenv("CHAPA_SECRET_KEY", "")

# Verify critical configuration at startup
def redact_connection_url(url: str) -> str:
    """Remove credentials from a connection URL before logging it."""
    if "://" in url and "@" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://[REDACTED]@{rest.split('@', 1)[1]}"
    return url

if not KEYGEN_ACCOUNT_ID:
    logger.error("KEYGEN_ACCOUNT_ID is not set — license provisioning will fail")
if not CHAPA_SECRET_KEY or CHAPA_SECRET_KEY.startswith("replace-with-"):
    logger.warning("CHAPA_SECRET_KEY is not set or is a placeholder — webhook verification will fail")

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Marketplace Middleware Bridge",
    version="1.0.0",
    docs_url=None,       # disable in production
    redoc_url=None,
)

# ---------------------------------------------------------------------------
# Redis connection (lazy — created on first request)
# ---------------------------------------------------------------------------
_redis: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    """Return a shared Redis connection, creating it if necessary."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        logger.info("Connected to Redis at %s", redact_connection_url(REDIS_URL))
    return _redis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_license_key() -> str:
    """Generate a Keygen-format license key: XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XX."""
    raw = uuid.uuid4().hex.upper() + uuid.uuid4().hex.upper()
    segments = [raw[i:i + 6] for i in range(0, 30, 6)]
    return "-".join(segments)


def verify_chapa_signature(payload: bytes, signature_header: Optional[str]) -> bool:
    """
    Verify the x-chapa-signature header against the request body using HMAC-SHA256.
    Returns True if the signature is valid or if no signature header is present
    (Chapa does not always send one in test mode).
    """
    if not signature_header:
        logger.warning("No x-chapa-signature header present — skipping verification")
        return True

    expected = hmac.new(
        CHAPA_SECRET_KEY.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        logger.error("Chapa HMAC signature mismatch")
        return False

    return True


async def provision_license(
    tx_ref: str,
    email: str,
    amount: float,
    product_name: str = "Default Software Product",
) -> dict:
    """
    Create a license in Keygen for the given transaction.
    Returns a dict with license_key, license_id, and expiry.
    """
    key = generate_license_key()

    # Build the license payload for Keygen's REST API
    payload = {
        "data": {
            "type": "licenses",
            "attributes": {
                "key": key,
                "name": product_name,
                "email": email,
                "metadata": {
                    "tx_ref": tx_ref,
                    "amount": str(amount),
                    "source": "chapa-webhook",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
    }

    headers = {
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }
    if KEYGEN_ADMIN_TOKEN:
        headers["Authorization"] = f"Bearer {KEYGEN_ADMIN_TOKEN}"

    url = f"{KEYGEN_API_URL}/licenses"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            license_id = data["data"]["id"]
            logger.info(
                "License created: id=%s key=%s tx_ref=%s", license_id, key, tx_ref
            )
            return {
                "license_key": key,
                "license_id": license_id,
                "expiry": None,  # perpetual license — no expiry
            }
        except httpx.HTTPError as exc:
            logger.error("Keygen API error: %s — Response: %s", exc, getattr(exc, "response", None))
            raise


async def update_woocommerce_order_meta(
    order_id: str,
    license_key: str,
    license_id: str,
    expiry: Optional[str],
) -> None:
    """
    Attach license metadata to the WooCommerce order via REST API.
    """
    if not WOOCOMMERCE_CONSUMER_KEY or not WOOCOMMERCE_CONSUMER_SECRET:
        logger.warning("WooCommerce API keys not configured — skipping order meta update")
        return

    url = f"{WOOCOMMERCE_URL}/wp-json/wc/v3/orders/{order_id}"
    auth = (WOOCOMMERCE_CONSUMER_KEY, WOOCOMMERCE_CONSUMER_SECRET)

    meta_data = [
        {"key": "_kgm_license_key", "value": license_key},
        {"key": "_kgm_license_id", "value": license_id},
        {"key": "_kgm_license_expiry", "value": expiry or ""},
    ]

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.put(url, auth=auth, json={"meta_data": meta_data})
            resp.raise_for_status()
            logger.info("WooCommerce order %s updated with license %s", order_id, license_key)
        except httpx.HTTPError as exc:
            logger.error("WooCommerce API error for order %s: %s", order_id, exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Docker health-check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/v1/webhooks/chapa")
async def handle_chapa_webhook(request: Request):
    """
    Handle incoming Chapa payment webhook.

    1. Verify HMAC signature (x-chapa-signature).
    2. Enforce idempotency via Redis key lock (SETNX lock:{tx_ref}).
    3. Out-of-band verify the transaction with Chapa API.
    4. Push verified transaction to Redis Stream for async processing.
    """
    # ── Read raw body for HMAC verification ───────────────────────────────
    raw_body = await request.body()
    signature_header = request.headers.get("x-chapa-signature")

    if not verify_chapa_signature(raw_body, signature_header):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    # ── Parse JSON payload ─────────────────────────────────────────────────
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    tx_ref = payload.get("tx_ref")
    if not tx_ref:
        raise HTTPException(status_code=400, detail="Missing tx_ref in webhook payload")

    logger.info("Webhook received: tx_ref=%s", tx_ref)

    # ── Idempotency lock via Redis ─────────────────────────────────────────
    redis = await get_redis()
    lock_acquired = await redis.set(f"lock:{tx_ref}", "processing", nx=True, ex=300)
    if not lock_acquired:
        logger.info("Duplicate webhook ignored: tx_ref=%s", tx_ref)
        return JSONResponse(
            content={"status": "duplicate_event_ignored", "tx_ref": tx_ref},
            status_code=200,
        )

    # ── Out-of-band verification with Chapa API ────────────────────────────
    verify_url = f"https://api.chapa.co/v1/transaction/verify/{tx_ref}"
    headers = {"Authorization": f"Bearer {CHAPA_SECRET_KEY}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(verify_url, headers=headers)
            resp.raise_for_status()
            verification = resp.json()
        except httpx.HTTPError as exc:
            logger.error("Chapa verification failed for tx_ref=%s: %s", tx_ref, exc)
            # Release the lock so a retry can proceed
            await redis.delete(f"lock:{tx_ref}")
            raise HTTPException(status_code=502, detail="Chapa verification request failed")

    # ── Validate verification response ─────────────────────────────────────
    if (
        verification.get("status") != "success"
        or verification.get("data", {}).get("status") != "success"
    ):
        logger.warning("Chapa verification returned non-success for tx_ref=%s: %s", tx_ref, verification)
        await redis.delete(f"lock:{tx_ref}")
        return JSONResponse(
            content={"status": "payment_not_verified", "tx_ref": tx_ref},
            status_code=200,
        )

    verified_data = verification["data"]

    # ── Push to Redis Streams queue ────────────────────────────────────────
    stream_data = {
        "tx_ref": tx_ref,
        "amount": str(verified_data.get("amount", "0")),
        "email": verified_data.get("email", ""),
        "first_name": verified_data.get("first_name", ""),
        "last_name": verified_data.get("last_name", ""),
        "currency": verified_data.get("currency", "ETB"),
        "payload": json.dumps(verified_data),
    }

    await redis.xadd("stream:license_provisioning", stream_data, maxlen=10000)
    logger.info("Transaction queued: tx_ref=%s amount=%s %s", tx_ref, stream_data["amount"], verified_data.get("currency"))

    return JSONResponse(
        content={"status": "queued", "tx_ref": tx_ref},
        status_code=200,
    )


@app.post("/v1/webhooks/chapa/process")
async def process_license_manually(request: Request):
    """
    Manual endpoint to trigger license provisioning for a given tx_ref.
    Used for testing and recovery. Not exposed publicly.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    tx_ref = body.get("tx_ref")
    if not tx_ref:
        raise HTTPException(status_code=400, detail="Missing tx_ref")

    redis = await get_redis()

    # Fetch the stored verification payload
    stored = await redis.get(f"verified:{tx_ref}")
    if not stored:
        raise HTTPException(status_code=404, detail="No verified transaction found for this tx_ref")

    data = json.loads(stored)

    try:
        result = await provision_license(
            tx_ref=tx_ref,
            email=data.get("email", ""),
            amount=float(data.get("amount", 0)),
            product_name=data.get("product_name", "Default Software Product"),
        )
        return JSONResponse(content={"status": "provisioned", "license": result}, status_code=200)
    except Exception as exc:
        logger.exception("Manual license provisioning failed for tx_ref=%s", tx_ref)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# LICENSING PROTOCOL ENDPOINTS (Requirements 4-6)
# ---------------------------------------------------------------------------

# ── Protocol 0: Direct license provisioning (no payment gateway) ──────────

@app.post("/v1/licenses/direct")
async def direct_license_provision(request: Request):
    """
    Provision a license directly without a payment gateway.

    Body:
        {
            "email": "customer@example.et",
            "product_name": "CAD Enterprise",
            "policy_id": "keygen-policy-uuid",
            "max_machines": 3,
            "max_processes": 5,
            "max_cores": 16,
            "scheme": "ED25519_SIGN",
            "licensing_type": "perpetual"
        }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    email = body.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="Missing 'email' field")

    try:
        result = await provision_license_direct(
            email=email,
            product_name=body.get("product_name", "Default Software Product"),
            policy_id=body.get("policy_id"),
            max_machines=int(body.get("max_machines", 3)),
            max_processes=int(body.get("max_processes", 5)),
            max_cores=int(body["max_cores"]) if body.get("max_cores") is not None else None,
            scheme=body.get("scheme", "ED25519_SIGN"),
            licensing_type=body.get("licensing_type", "perpetual"),
        )
        return JSONResponse(content={"status": "provisioned", "license": result}, status_code=201)
    except Exception as exc:
        logger.exception("Direct license provisioning failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Protocol 1: Online Direct API Activation ─────────────────────────────

@app.post("/v1/licenses/validate")
async def validate_license(request: Request):
    """
    Validate a license key and optionally register the machine.

    Called by client software with active internet access.

    Body:
        {
            "key": "XXXX-XXXX-XXXX-XXXX-XXXX-XX",
            "fingerprint": "sha256-hash-of-machine-hardware"
        }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    key = body.get("key")
    if not key:
        raise HTTPException(status_code=400, detail="Missing 'key' field")

    fingerprint = body.get("fingerprint", "")
    if not fingerprint:
        raise HTTPException(status_code=400, detail="Missing 'fingerprint' field")

    try:
        result = await validate_license_key(key=key, fingerprint=fingerprint)
        return JSONResponse(content=result, status_code=200)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return JSONResponse(
                content={"valid": False, "detail": "License key not found"},
                status_code=200,
            )
        logger.exception("License validation error")
        raise HTTPException(status_code=502, detail="Keygen API error")


@app.post("/v1/machines")
async def register_machine_endpoint(request: Request):
    """
    Register a machine under a license.

    Body:
        {
            "license_id": "...",
            "fingerprint": "sha256-hash",
            "hostname": "DESKTOP-ABC123"
        }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    license_id = body.get("license_id")
    fingerprint = body.get("fingerprint")
    if not license_id or not fingerprint:
        raise HTTPException(status_code=400, detail="Missing 'license_id' or 'fingerprint'")

    try:
        result = await register_machine(
            license_id=license_id,
            fingerprint=fingerprint,
            hostname=body.get("hostname", ""),
        )
        return JSONResponse(content=result, status_code=201)
    except Exception as exc:
        logger.exception("Machine registration failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Desktop demo app activation compatibility ─────────────────────────────

async def _activate_desktop_license(body: dict, register: bool = True) -> dict:
    """
    Validate a WooCommerce-provisioned Keygen license for the Windows demo apps.

    The existing desktop app transport posts to /activate, /revalidate and
    /deactivate. Chapa provisions licenses directly into the Keygen CE database,
    so this compatibility endpoint validates those license rows and records the
    machine fingerprint in Keygen's machines table.
    """
    import asyncpg

    key = (body.get("license_key") or body.get("key") or "").strip()
    fingerprint = (body.get("fingerprint") or "").strip()
    product = (body.get("product") or body.get("product_name") or "Windows Demo App").strip()

    if not key:
        return {"status": "invalid", "code": "EMPTY_KEY", "message": "Enter a license key."}
    if not fingerprint:
        return {"status": "invalid", "code": "EMPTY_FINGERPRINT", "message": "Missing machine fingerprint."}

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        logger.error("DATABASE_URL is not configured; desktop activation cannot validate licenses")
        raise HTTPException(status_code=503, detail="Licensing database is not configured")

    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow(
            """
            SELECT l.id, l.account_id, l.policy_id, l.expiry, l.suspended,
                   COALESCE(l.max_machines_override, p.max_machines, 1) AS max_machines,
                   p.name AS policy_name
              FROM licenses l
              JOIN policies p ON p.id = l.policy_id
             WHERE l.account_id = $1 AND l.key = $2
             LIMIT 1
            """,
            KEYGEN_ACCOUNT_ID,
            key,
        )
        if not row:
            return {"status": "invalid", "code": "NOT_FOUND", "message": "That license key was not found."}
        if row["suspended"]:
            return {"status": "invalid", "code": "SUSPENDED", "message": "This license is suspended."}
        if row["expiry"] and row["expiry"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
            return {
                "status": "expired",
                "code": "EXPIRED",
                "message": "This license has expired.",
                "expires_at": row["expiry"].replace(tzinfo=timezone.utc).isoformat(),
            }

        existing_machine = await conn.fetchrow(
            "SELECT id FROM machines WHERE license_id = $1 AND fingerprint = $2 LIMIT 1",
            row["id"],
            fingerprint,
        )
        machine_count = await conn.fetchval(
            "SELECT COUNT(*) FROM machines WHERE license_id = $1",
            row["id"],
        )
        max_machines = int(row["max_machines"] or 1)

        if not existing_machine and machine_count >= max_machines:
            return {
                "status": "machine_limit",
                "code": "MACHINE_LIMIT_REACHED",
                "message": "This license is already activated on the maximum number of machines.",
            }

        if register and not existing_machine:
            machine_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO machines
                    (id, account_id, license_id, policy_id, fingerprint, hostname, platform, name, metadata, created_at, updated_at)
                VALUES
                    ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, NOW(), NOW())
                """,
                machine_id,
                row["account_id"],
                row["id"],
                row["policy_id"],
                fingerprint,
                body.get("hostname") or f"machine-{fingerprint[:8]}",
                "windows",
                product,
                json.dumps({"source": "windows-demo-app", "product": product}),
            )
            machine_count += 1

        await conn.execute(
            "UPDATE licenses SET machines_count = $1, last_validated_at = NOW(), updated_at = NOW() WHERE id = $2",
            machine_count,
            row["id"],
        )

        return {
            "status": "activated",
            "code": "OK",
            "message": "This machine is now licensed.",
            "expires_at": row["expiry"].replace(tzinfo=timezone.utc).isoformat() if row["expiry"] else None,
            "license_id": str(row["id"]),
            "machines_count": machine_count,
            "machines_limit": max_machines,
        }
    finally:
        await conn.close()


@app.post("/activate")
async def desktop_activate(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return JSONResponse(content=await _activate_desktop_license(body, register=True), status_code=200)


@app.post("/revalidate")
async def desktop_revalidate(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    return JSONResponse(content=await _activate_desktop_license(body, register=False), status_code=200)


@app.post("/deactivate")
async def desktop_deactivate(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    import asyncpg

    key = (body.get("license_key") or body.get("key") or "").strip()
    fingerprint = (body.get("fingerprint") or "").strip()
    if not key or not fingerprint:
        return JSONResponse(content={"status": "invalid", "code": "MISSING_FIELDS", "message": "Missing license key or fingerprint."})

    conn = await asyncpg.connect(os.getenv("DATABASE_URL", ""))
    try:
        row = await conn.fetchrow("SELECT id FROM licenses WHERE account_id = $1 AND key = $2 LIMIT 1", KEYGEN_ACCOUNT_ID, key)
        if row:
            await conn.execute("DELETE FROM machines WHERE license_id = $1 AND fingerprint = $2", row["id"], fingerprint)
            count = await conn.fetchval("SELECT COUNT(*) FROM machines WHERE license_id = $1", row["id"])
            await conn.execute("UPDATE licenses SET machines_count = $1, updated_at = NOW() WHERE id = $2", count, row["id"])
    finally:
        await conn.close()

    return JSONResponse(content={"status": "invalid", "code": "DEACTIVATED", "message": "This machine was deactivated."}, status_code=200)


@app.get("/v1/licenses/{license_id}/machines")
async def desktop_license_machine_counts(license_id: str):
    """Return live machine-seat usage for the WooCommerce account license card."""
    import asyncpg

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        logger.error("DATABASE_URL is not configured; cannot read license machine counts")
        raise HTTPException(status_code=503, detail="Licensing database is not configured")

    try:
        uuid.UUID(license_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid license id")

    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow(
            """
            SELECT l.id,
                   COALESCE(l.machines_count, 0) AS cached_machines_count,
                   COALESCE(l.max_machines_override, p.max_machines, 1) AS max_machines,
                   (SELECT COUNT(*) FROM machines m WHERE m.license_id = l.id) AS live_machines_count
              FROM licenses l
              JOIN policies p ON p.id = l.policy_id
             WHERE l.account_id = $1 AND l.id = $2
             LIMIT 1
            """,
            KEYGEN_ACCOUNT_ID,
            uuid.UUID(license_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="License not found")

        live_count = int(row["live_machines_count"] or 0)
        if live_count != int(row["cached_machines_count"] or 0):
            await conn.execute("UPDATE licenses SET machines_count = $1, updated_at = NOW() WHERE id = $2", live_count, row["id"])

        return JSONResponse(content={
            "license_id": str(row["id"]),
            "machines_count": live_count,
            "machines_limit": int(row["max_machines"] or 1),
        })
    finally:
        await conn.close()


# ── Protocol 2: QR Code Bridge Activation ─────────────────────────────────

@app.get("/activate")
async def qr_activation_page(key: str, fingerprint: str = ""):
    """
    QR code activation landing page.

    The user scans a QR code on their phone, which opens this page.
    The page triggers machine activation on behalf of the device.

    Query params:
        key         — license key
        fingerprint — device hardware fingerprint
    """
    if not fingerprint:
        return JSONResponse(
            content={
                "status": "missing_fingerprint",
                "message": "Please provide a device fingerprint to activate.",
                "license_key": key,
            },
            status_code=400,
        )

    try:
        result = await activate_via_qr(license_key=key, fingerprint=fingerprint)
        return JSONResponse(content=result, status_code=200)
    except Exception as exc:
        logger.exception("QR activation failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Protocol 3: Air-Gapped Offline Licensing ──────────────────────────────

@app.post("/v1/licenses/{license_id}/checkout")
async def checkout_offline_endpoint(license_id: str, request: Request):
    """
    Perform a cryptographic checkout for air-gapped environments.

    Returns a signed .lic certificate file.

    Body:
        {
            "fingerprint": "sha256-hash",
            "ttl_days": 30,
            "scheme": "ED25519_SIGN"
        }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    fingerprint = body.get("fingerprint")
    if not fingerprint:
        raise HTTPException(status_code=400, detail="Missing 'fingerprint' field")

    try:
        result = await checkout_offline_license(
            license_id=license_id,
            fingerprint=fingerprint,
            ttl_days=int(body.get("ttl_days", 30)),
            scheme=body.get("scheme", "ED25519_SIGN"),
        )
        return JSONResponse(content=result, status_code=200)
    except Exception as exc:
        logger.exception("Offline checkout failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/v1/licenses/validate-offline")
async def validate_offline_endpoint(request: Request):
    """
    Validate a .lic file locally.

    Used for testing/verification of the offline licensing protocol.

    Body:
        {
            "lic_content": "-----BEGIN LICENSE FILE-----\n...\n-----END LICENSE FILE-----",
            "public_key": "base64-ed25519-public-key"
        }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    lic_content = body.get("lic_content")
    if not lic_content:
        raise HTTPException(status_code=400, detail="Missing 'lic_content' field")

    public_key = body.get("public_key", "")
    result = await validate_offline_lic(lic_content=lic_content, public_key=public_key)
    return JSONResponse(content=result, status_code=200)


# ---------------------------------------------------------------------------
# RELEASE MANAGEMENT ENDPOINTS (Requirement 5)
# ---------------------------------------------------------------------------

@app.get("/v1/releases")
async def list_releases_endpoint(product_id: str = ""):
    """
    List all software releases, optionally filtered by product.

    Query params:
        product_id — optional Keygen Product UUID to filter by
    """
    try:
        releases = await list_releases(
            product_id=product_id if product_id else None,
        )
        return JSONResponse(content={"releases": releases, "count": len(releases)}, status_code=200)
    except Exception as exc:
        logger.exception("Failed to list releases")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/v1/releases")
async def create_release_endpoint(request: Request):
    """
    Create a new software release (admin only).

    Body:
        {
            "product_id": "uuid",
            "version": "2.4.1",
            "file_path": "/path/to/binary.msi",
            "release_notes": "Bug fixes and performance improvements",
            "constraints": {"policy": "uuid"}
        }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    required = ["product_id", "version", "file_path"]
    for field in required:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing '{field}' field")

    try:
        result = await upload_release_artifact(
            product_id=body["product_id"],
            version=body["version"],
            file_path=body["file_path"],
            release_notes=body.get("release_notes", ""),
            constraints=body.get("constraints"),
        )
        return JSONResponse(content={"status": "created", "release": result}, status_code=201)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to create release")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/v1/releases/download")
async def download_release_endpoint(request: Request):
    """
    Verify license entitlement and return a presigned download URL.

    Body:
        {
            "license_key": "XXXX-XXXX-XXXX-XXXX-XXXX-XX",
            "release_id": "uuid"
        }

    Returns a time-limited download URL on success.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    license_key = body.get("license_key")
    release_id = body.get("release_id")

    if not license_key or not release_id:
        raise HTTPException(status_code=400, detail="Missing 'license_key' or 'release_id'")

    try:
        result = await verify_license_entitlement(
            license_key=license_key,
            release_id=release_id,
        )

        if not result.get("authorized"):
            return JSONResponse(
                content={"status": "unauthorized", "reason": result.get("reason", "Not entitled")},
                status_code=403,
            )

        return JSONResponse(content={"status": "authorized", **result}, status_code=200)
    except Exception as exc:
        logger.exception("Download authorization failed")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Startup event — verify connectivity
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    """Verify Redis connectivity and log configuration on startup."""
    try:
        redis = await get_redis()
        await redis.ping()
        logger.info("Startup: Redis ping successful")
    except Exception as exc:
        logger.error("Startup: Redis connection failed — %s", exc)

    logger.info("Middleware started — Keygen account: %s", KEYGEN_ACCOUNT_ID or "NOT SET")
    logger.info("Chapa webhook endpoint: POST /v1/webhooks/chapa")