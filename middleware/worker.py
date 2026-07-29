"""
Background License Provisioning Worker
======================================
Consumes events from Redis Stream ``stream:license_provisioning``,
interacts with the Keygen REST API to create licenses, optionally
attaches license metadata to WooCommerce orders, and dispatches
customer confirmation emails.

Designed to run as a long-lived process alongside the FastAPI server.
"""

import asyncio
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("worker")

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

# ---------------------------------------------------------------------------
# Stream constants
# ---------------------------------------------------------------------------
def redact_connection_url(url: str) -> str:
    """Remove credentials from a connection URL before logging it."""
    if "://" in url and "@" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://[REDACTED]@{rest.split('@', 1)[1]}"
    return url

STREAM_NAME = "stream:license_provisioning"
CONSUMER_GROUP = "license-workers"
CONSUMER_NAME = f"worker-{uuid.uuid4().hex[:8]}"
BATCH_SIZE = 5
BLOCK_MS = 5000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_license_key() -> str:
    """Generate a Keygen-format license key: XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XX."""
    raw = uuid.uuid4().hex.upper() + uuid.uuid4().hex.upper()
    segments = [raw[i:i + 6] for i in range(0, 30, 6)]
    return "-".join(segments)


async def create_keygen_license(
    email: str,
    product_name: str,
    tx_ref: str,
    amount: float,
    policy_id: Optional[str] = None,
) -> dict:
    """
    Create a license via the Keygen REST API.

    Returns a dict with:
        license_key  — the generated key string
        license_id   — Keygen license UUID
        expiry       — ISO 8601 datetime string or None
    """
    key = generate_license_key()

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
                    "customer_email": email,
                    "policy_id": policy_id,
                    "source": "chapa-webhook",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
    }

    if policy_id:
        payload["data"]["relationships"] = {
            "policy": {
                "data": {
                    "type": "policies",
                    "id": policy_id,
                }
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
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    license_id = data["data"]["id"]
    expiry = data["data"]["attributes"].get("expiry")

    logger.info("License provisioned: id=%s key=%s email=%s", license_id, key, email)

    return {
        "license_key": key,
        "license_id": license_id,
        "expiry": expiry,
    }


async def update_woocommerce_order(
    order_id: str,
    license_key: str,
    license_id: str,
    expiry: Optional[str],
) -> bool:
    """
    Attach license metadata to a WooCommerce order via REST API.

    Returns True on success, False on failure.
    """
    if not WOOCOMMERCE_CONSUMER_KEY or not WOOCOMMERCE_CONSUMER_SECRET:
        logger.warning("WooCommerce API keys not configured — skipping order meta update")
        return False

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
            return True
        except httpx.HTTPError as exc:
            logger.error(
                "WooCommerce API error for order %s: %s — Response: %s",
                order_id,
                exc,
                getattr(exc, "response", None),
            )
            return False


async def process_single_event(message_id: str, fields: dict) -> bool:
    """
    Process a single event from the Redis Stream.

    Steps:
        1. Extract tx_ref, email, amount from the stream fields.
        2. Provision a license via Keygen API.
        3. If tx_ref contains an order ID, update WooCommerce order meta.
        4. Store the result in Redis for the customer portal to read.

    Returns True on success, False on failure.
    """
    tx_ref = fields.get("tx_ref", "unknown")
    email = fields.get("email", "")
    amount = float(fields.get("amount", "0"))
    first_name = fields.get("first_name", "")
    last_name = fields.get("last_name", "")
    full_payload = fields.get("payload", "{}")

    logger.info(
        "Processing event %s: tx_ref=%s email=%s amount=%s",
        message_id, tx_ref, email, amount,
    )

    try:
        # ── Step 1: Provision the license ──────────────────────────────────
        result = await create_keygen_license(
            email=email,
            product_name=f"Marketplace Purchase — {first_name} {last_name}".strip(),
            tx_ref=tx_ref,
            amount=amount,
        )

        # ── Step 2: Update WooCommerce order (if tx_ref contains order ID) ─
        # Chapa tx_ref format: "WOO-ORD-{order_id}-{timestamp}"
        order_id = None
        if tx_ref.startswith("WOO-ORD-"):
            parts = tx_ref.split("-")
            if len(parts) >= 3:
                order_id = parts[2]

        if order_id:
            await update_woocommerce_order(
                order_id=order_id,
                license_key=result["license_key"],
                license_id=result["license_id"],
                expiry=result["expiry"],
            )

        # ── Step 3: Store result in Redis for the customer portal ──────────
        redis = await get_worker_redis()
        result_data = {
            "tx_ref": tx_ref,
            "license_key": result["license_key"],
            "license_id": result["license_id"],
            "expiry": result["expiry"],
            "email": email,
            "provisioned_at": datetime.now(timezone.utc).isoformat(),
        }
        await redis.set(f"license:{tx_ref}", json.dumps(result_data), ex=86400 * 30)
        await redis.set(f"license:email:{email}:{tx_ref}", json.dumps(result_data), ex=86400 * 30)

        logger.info("Event %s processed successfully: license=%s", message_id, result["license_key"])
        return True

    except Exception:
        logger.exception("Failed to process event %s (tx_ref=%s)", message_id, tx_ref)
        return False


# ---------------------------------------------------------------------------
# Redis connection (worker-specific)
# ---------------------------------------------------------------------------
_worker_redis: Optional[aioredis.Redis] = None


async def get_worker_redis() -> aioredis.Redis:
    global _worker_redis
    if _worker_redis is None:
        _worker_redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await _worker_redis.ping()
        logger.info("Worker connected to Redis at %s", redact_connection_url(REDIS_URL))
    return _worker_redis


# ---------------------------------------------------------------------------
# Main consumer loop
# ---------------------------------------------------------------------------

async def ensure_consumer_group(redis: aioredis.Redis) -> None:
    """Create the consumer group if it does not exist."""
    try:
        await redis.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("Consumer group '%s' created on stream '%s'", CONSUMER_GROUP, STREAM_NAME)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            logger.debug("Consumer group '%s' already exists", CONSUMER_GROUP)
        else:
            raise


async def consumer_loop() -> None:
    """
    Main worker loop: reads from Redis Stream, processes events, acknowledges.
    """
    redis = await get_worker_redis()
    await ensure_consumer_group(redis)

    logger.info(
        "Worker %s started — listening on stream '%s' group '%s'",
        CONSUMER_NAME, STREAM_NAME, CONSUMER_GROUP,
    )

    while True:
        try:
            # ── Read pending + new messages ────────────────────────────────
            # First, claim any pending messages older than 60 seconds
            pending = await redis.xpending_range(
                STREAM_NAME, CONSUMER_GROUP, min="-", max="+", count=10
            )
            for entry in pending:
                claimed = await redis.xclaim(
                    STREAM_NAME,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    min_idle_time=60000,  # 60 seconds
                    message_ids=[entry["message_id"]],
                )
                for msg_id, fields in claimed:
                    success = await process_single_event(msg_id, fields)
                    if success:
                        await redis.xack(STREAM_NAME, CONSUMER_GROUP, msg_id)
                        await redis.xdel(STREAM_NAME, msg_id)

            # ── Read new messages ──────────────────────────────────────────
            messages = await redis.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={STREAM_NAME: ">"},
                count=BATCH_SIZE,
                block=BLOCK_MS,
            )

            for stream_name, entries in messages:
                for message_id, fields in entries:
                    success = await process_single_event(message_id, fields)
                    if success:
                        await redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)
                        await redis.xdel(STREAM_NAME, message_id)

        except asyncio.CancelledError:
            logger.info("Worker %s shutting down", CONSUMER_NAME)
            break
        except aioredis.ConnectionError:
            logger.warning("Redis connection lost — reconnecting in 5s...")
            await asyncio.sleep(5)
            _worker_redis = None  # force reconnect
        except Exception:
            logger.exception("Unexpected error in consumer loop — retrying in 3s")
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting license provisioning worker...")
    logger.info("  Redis:     %s", redact_connection_url(REDIS_URL))
    logger.info("  Keygen:    %s/accounts/%s", KEYGEN_API_URL, KEYGEN_ACCOUNT_ID)
    logger.info("  Stream:    %s", STREAM_NAME)
    logger.info("  Consumer:  %s / %s", CONSUMER_GROUP, CONSUMER_NAME)

    asyncio.run(consumer_loop())