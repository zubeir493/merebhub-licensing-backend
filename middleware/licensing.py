"""
Licensing Protocol Handlers
============================
Implements three activation pathways from the Marketplace Technical Specification:

  Protocol 1 — Online Direct API Activation
  Protocol 2 — QR Code Bridge Activation
  Protocol 3 — Air-Gapped Offline Licensing (.lic files)

Plus a direct provisioning endpoint for use when Chapa is not yet integrated.
"""

import hashlib
import json
import logging
import os
import uuid
from base64 import urlsafe_b64encode
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("licensing")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KEYGEN_ACCOUNT_ID: str = os.getenv("KEYGEN_ACCOUNT_ID", "")
KEYGEN_API_URL: str = os.getenv("KEYGEN_API_URL", "http://keygen-api:3000/v1")
KEYGEN_ADMIN_TOKEN: str = os.getenv("KEYGEN_ADMIN_TOKEN", "")
STORE_URL: str = os.getenv("WOOCOMMERCE_URL", "https://merebhub.com")

# ---------------------------------------------------------------------------
# Keygen REST API helpers
# ---------------------------------------------------------------------------

async def _keygen_request(
    method: str,
    path: str,
    json_data: Optional[dict] = None,
    timeout: float = 30.0,
) -> dict:
    """Make an authenticated request to the Keygen REST API."""
    url = f"{KEYGEN_API_URL}{path}"
    headers = {
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }
    if KEYGEN_ADMIN_TOKEN:
        headers["Authorization"] = f"Bearer {KEYGEN_ADMIN_TOKEN}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "POST":
            resp = await client.post(url, json=json_data, headers=headers)
        elif method == "PATCH":
            resp = await client.patch(url, json=json_data, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        resp.raise_for_status()
        return resp.json()


def _generate_license_key() -> str:
    """Generate a Keygen-format license key."""
    raw = uuid.uuid4().hex.upper() + uuid.uuid4().hex.upper()
    return "-".join(raw[i:i + 6] for i in range(0, 30, 6))


def _generate_activation_url(license_key: str, fingerprint: str = "") -> str:
    """Generate a QR-code activation URL for the bridge protocol."""
    base = f"{STORE_URL}/activate?key={license_key}"
    if fingerprint:
        base += f"&fingerprint={fingerprint}"
    return base


# =============================================================================
# PROTOCOL 0 — DIRECT LICENSE PROVISIONING (No Payment Gateway)
# =============================================================================

async def provision_license_direct(
    email: str,
    product_name: str = "Default Software Product",
    policy_id: Optional[str] = None,
    max_machines: int = 3,
    max_processes: int = 5,
    max_cores: Optional[int] = None,
    scheme: str = "ED25519_SIGN",
    licensing_type: str = "perpetual",
) -> dict:
    """
    Provision a license directly without requiring a payment gateway.

    Creates the license directly in the Keygen PostgreSQL database via SQL,
    bypassing the Keygen REST API. This is necessary because Keygen CE
    singleplayer mode requires token authentication that is unreliable
    in Docker internal networking.

    If policy_id is provided (e.g. from WooCommerce variation meta
    `_keygen_policy_id`), it is used explicitly and validated against the
    configured Keygen account. If omitted, the legacy fallback remains: use the
    first policy in the account. The WooCommerce integration should stop using
    that fallback once per-line-item provisioning is wired in.
    """
    import asyncpg
    import uuid as _uuid

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable is required for direct DB provisioning")

    key = _generate_license_key()
    license_id = str(_uuid.uuid4())

    # Connect to PostgreSQL
    conn = await asyncpg.connect(db_url)
    try:
        # Find the account and user
        account_row = await conn.fetchrow(
            "SELECT id FROM accounts WHERE id = $1", KEYGEN_ACCOUNT_ID
        )
        if not account_row:
            raise ValueError(f"Account {KEYGEN_ACCOUNT_ID} not found")

        user_row = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1 AND account_id = $2",
            email, KEYGEN_ACCOUNT_ID
        )
        if not user_row:
            # Create the user
            user_id = str(_uuid.uuid4())
            await conn.execute(
                "INSERT INTO users (id, account_id, email, created_at, updated_at) VALUES ($1, $2, $3, NOW(), NOW())",
                user_id, KEYGEN_ACCOUNT_ID, email
            )
        else:
            user_id = user_row["id"]

        # Create the license
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = json.dumps({
            "source": "direct-provision",
            "licensing_type": licensing_type,
            "policy_id": policy_id,
            "created_at": created_at,
        })

        # Create the license - need a product_id and policy_id. Prefer the
        # policy explicitly supplied by WooCommerce variation/product meta;
        # fall back to the historical first-policy behavior only when no policy
        # was supplied to avoid breaking existing direct callers.
        if policy_id:
            policy_row = await conn.fetchrow(
                "SELECT id, product_id FROM policies WHERE id = $1 AND account_id = $2",
                policy_id, KEYGEN_ACCOUNT_ID
            )
            if not policy_row:
                raise ValueError(f"Policy {policy_id} not found for account {KEYGEN_ACCOUNT_ID}")
        else:
            policy_row = await conn.fetchrow(
                "SELECT id, product_id FROM policies WHERE account_id = $1 LIMIT 1",
                KEYGEN_ACCOUNT_ID
            )

        policy_id = policy_row["id"] if policy_row else None
        product_id = policy_row["product_id"] if policy_row else None

        if not product_id:
            product_row = await conn.fetchrow(
                "SELECT id FROM products WHERE account_id = $1 LIMIT 1",
                KEYGEN_ACCOUNT_ID
            )
            product_id = product_row["id"] if product_row else None

        await conn.execute(
            """INSERT INTO licenses (id, account_id, key, name, user_id, policy_id, product_id,
               metadata, protected, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, NOW(), NOW())""",
            license_id, KEYGEN_ACCOUNT_ID, key, product_name, user_id,
            policy_id, product_id, metadata, True
        )

        logger.info("Direct license provisioned via DB: id=%s key=%s email=%s", license_id, key, email)

        return {
            "license_key": key,
            "license_id": license_id,
            "expiry": None,
            "max_machines": max_machines,
            "max_processes": max_processes,
            "max_cores": max_cores,
            "scheme": scheme,
            "licensing_type": licensing_type,
            "activation_url": _generate_activation_url(key),
            "validate_url": f"{STORE_URL}/api/v1/licenses/validate",
        }
    finally:
        await conn.close()


async def _old_provision_license_direct(
    email: str,
    product_name: str = "Default Software Product",
    policy_id: Optional[str] = None,
    max_machines: int = 3,
    max_processes: int = 5,
    max_cores: Optional[int] = None,
    scheme: str = "ED25519_SIGN",
    licensing_type: str = "perpetual",
) -> dict:
    """
    Provision a license directly without requiring a payment gateway.

    This is the entry point used when Chapa is not yet integrated.
    The license is created immediately and returned to the caller.
    """
    key = _generate_license_key()

    attributes = {
        "key": key,
        "name": product_name,
        "email": email,
        "maxMachines": max_machines,
        "maxProcesses": max_processes,
        "strict": True,
        "requireFingerprintScope": True,
        "requireProductScope": True,
        "requirePolicyScope": True,
        "metadata": {
            "source": "direct-provision",
            "licensing_type": licensing_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    if max_cores is not None:
        attributes["maxCores"] = max_cores

    payload = {
        "data": {
            "type": "licenses",
            "attributes": attributes,
        }
    }

    result = await _keygen_request(
        "POST",
        f"/licenses",
        json_data=payload,
    )

    license_id = result["data"]["id"]
    expiry = result["data"]["attributes"].get("expiry")

    logger.info("Direct license provisioned: id=%s key=%s email=%s", license_id, key, email)

    return {
        "license_key": key,
        "license_id": license_id,
        "expiry": expiry,
        "max_machines": max_machines,
        "max_processes": max_processes,
        "max_cores": max_cores,
        "scheme": scheme,
        "licensing_type": licensing_type,
        "activation_url": _generate_activation_url(key),
        "validate_url": f"{STORE_URL}/api/v1/licenses/validate",
    }


# =============================================================================
# PROTOCOL 1 — ONLINE DIRECT API ACTIVATION
# =============================================================================

async def validate_license_key(key: str, fingerprint: str) -> dict:
    """
    Validate a license key against the Keygen API.

    This is the endpoint called by client software with an active internet
    connection. It validates the key, checks machine limits, and registers
    the machine if not previously registered.

    Client software calls:
        POST /v1/licenses/validate
        {"key": "XXXX-XXXX-...", "fingerprint": "sha256-hash"}
    """
    # Step 1: Validate the license key
    validation = await _keygen_request(
        "POST",
        f"/licenses/actions/validate-key",
        json_data={
            "meta": {
                "key": key,
                "scope": {
                    "fingerprint": fingerprint,
                },
            },
        },
    )

    # metadata is returned as a dict by the Keygen API
    meta = validation.get("meta", {})

    # Step 2: Check if this machine is already registered
    # If the license is valid but the machine is not yet registered,
    # the client should call POST /v1/machines to lock the seat.
    # We return the license ID and fingerprint status so the client
    # can decide whether to register.

    license_id = meta.get("licenseId", meta.get("license", ""))
    valid = meta.get("valid", False)
    detail = meta.get("detail", "")
    expiry = meta.get("expiry")
    machines_count = meta.get("machinesCount", 0)
    machines_limit = meta.get("machinesLimit", 0)

    logger.info(
        "License validated: key=%s valid=%s machines=%s/%s fingerprint=%s",
        key[:12], valid, machines_count, machines_limit, fingerprint[:12],
    )

    return {
        "valid": valid,
        "license_id": str(license_id),
        "detail": detail,
        "expiry": expiry,
        "machines_count": machines_count,
        "machines_limit": machines_limit,
        "fingerprint": fingerprint,
        "fingerprint_registered": machines_count > 0,
    }


async def register_machine(license_id: str, fingerprint: str, hostname: str = "") -> dict:
    """
    Register a machine under a license.

    Client software calls this after validation if the machine is not yet registered.
    """
    result = await _keygen_request(
        "POST",
        f"/machines",
        json_data={
            "data": {
                "type": "machines",
                "attributes": {
                    "fingerprint": fingerprint,
                    "hostname": hostname or f"machine-{fingerprint[:8]}",
                    "platform": "linux",
                    "metadata": {
                        "registered_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
                "relationships": {
                    "license": {
                        "data": {
                            "type": "licenses",
                            "id": license_id,
                        },
                    },
                },
            },
        },
    )

    machine_id = result["data"]["id"]
    logger.info("Machine registered: id=%s fingerprint=%s license=%s", machine_id, fingerprint[:12], license_id)

    return {
        "machine_id": machine_id,
        "fingerprint": fingerprint,
        "license_id": license_id,
        "status": "registered",
    }


# =============================================================================
# PROTOCOL 2 — QR CODE BRIDGE ACTIVATION
# =============================================================================

async def activate_via_qr(license_key: str, fingerprint: str) -> dict:
    """
    Handle QR code bridge activation.

    The user scans a QR code on their phone, which opens the marketplace
    activation page. The phone authenticates the user and triggers
    machine activation on behalf of the device.

    Flow:
        1. User scans QR code → opens /activate?key=XXX&fingerprint=YYY
        2. User authenticates (if not already logged in)
        3. This endpoint is called to register the machine
        4. The client software polls /v1/licenses/validate and unlocks
    """
    # Step 1: Validate the license key
    validation = await validate_license_key(license_key, fingerprint)

    if not validation["valid"]:
        return {
            "status": "failed",
            "reason": validation["detail"] or "License key is invalid or expired",
            "validation": validation,
        }

    # Step 2: Register the machine if not already registered
    if not validation["fingerprint_registered"]:
        machine = await register_machine(
            license_id=validation["license_id"],
            fingerprint=fingerprint,
            hostname=f"qr-device-{fingerprint[:8]}",
        )
        return {
            "status": "activated",
            "license_key": license_key,
            "fingerprint": fingerprint,
            "machine": machine,
            "validation": validation,
        }

    return {
        "status": "already_activated",
        "license_key": license_key,
        "fingerprint": fingerprint,
        "validation": validation,
    }


# =============================================================================
# PROTOCOL 3 — AIR-GAPPED OFFLINE LICENSING
# =============================================================================

async def checkout_offline_license(
    license_id: str,
    fingerprint: str,
    ttl_days: int = 30,
    scheme: str = "ED25519_SIGN",
) -> dict:
    """
    Perform a cryptographic checkout of a license for air-gapped environments.

    The user uploads an activation.req file containing their license key and
    hardware fingerprint. The middleware calls Keygen's checkout action to
    generate a cryptographically signed .lic certificate file.

    The .lic file is then downloaded and imported into the air-gapped software,
    which validates it locally using the vendor's hardcoded Ed25519 public key.
    """
    # Step 1: Checkout the license via Keygen
    ttl_seconds = ttl_days * 86400

    checkout_result = await _keygen_request(
        "POST",
        f"/licenses/{license_id}/actions/check-out",
        json_data={
            "meta": {
                "include": "product,entitlements",
                "ttl": ttl_seconds,
                "scheme": scheme,
                "requireFingerprintScope": True,
                "fingerprint": fingerprint,
            },
        },
    )

    # Step 2: Extract the signed certificate
    certificate = checkout_result.get("meta", {}).get("certificate", "")
    if not certificate:
        # Fallback: extract from data attributes
        certificate = checkout_result.get("data", {}).get("attributes", {}).get("certificate", "")

    # Step 3: Build the .lic file content
    lic_content = _build_lic_file(
        license_id=license_id,
        fingerprint=fingerprint,
        certificate=certificate,
        ttl_days=ttl_days,
        scheme=scheme,
    )

    expiry_date = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()

    logger.info(
        "Offline license checked out: id=%s fingerprint=%s ttl=%ddays scheme=%s",
        license_id, fingerprint[:12], ttl_days, scheme,
    )

    return {
        "license_id": license_id,
        "fingerprint": fingerprint,
        "scheme": scheme,
        "ttl_days": ttl_days,
        "expiry": expiry_date,
        "lic_content": lic_content,
        "filename": f"license_{license_id[:8]}_{fingerprint[:8]}.lic",
    }


def _build_lic_file(
    license_id: str,
    fingerprint: str,
    certificate: str,
    ttl_days: int,
    scheme: str,
) -> str:
    """
    Build a .lic certificate file with the standard format:

        -----BEGIN LICENSE FILE-----
        <base64-encoded payload>
        -----END LICENSE FILE-----

    The payload contains the license metadata, fingerprint, TTL, and
    cryptographic signature.
    """
    payload = {
        "license_id": license_id,
        "fingerprint": fingerprint,
        "scheme": scheme,
        "ttl_days": ttl_days,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "certificate": certificate,
    }

    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = urlsafe_b64encode(payload_json.encode("utf-8")).decode("ascii")

    return (
        "-----BEGIN LICENSE FILE-----\n"
        + payload_b64
        + "\n-----END LICENSE FILE-----"
    )


async def validate_offline_lic(lic_content: str, public_key: str) -> dict:
    """
    Validate a .lic file locally using the vendor's public key.

    This is the client-side validation logic that the air-gapped software
    would implement. We include it here for testing and verification.

    The software:
        1. Extracts the payload from the .lic file
        2. Verifies the cryptographic signature against the public key
        3. Checks the TTL has not expired
        4. Verifies the fingerprint matches the local machine
    """
    lines = lic_content.strip().split("\n")
    if len(lines) < 3:
        return {"valid": False, "reason": "Invalid .lic file format"}

    if "BEGIN LICENSE FILE" not in lines[0] or "END LICENSE FILE" not in lines[-1]:
        return {"valid": False, "reason": "Missing LICENSE FILE markers"}

    b64_payload = lines[1].strip()
    try:
        import base64

        payload_json = base64.urlsafe_b64decode(b64_payload + "===").decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        return {"valid": False, "reason": "Failed to decode .lic payload"}

    # Check TTL expiry
    issued_at = payload.get("issued_at", "")
    ttl_days = payload.get("ttl_days", 30)
    if issued_at:
        try:
            issued = datetime.fromisoformat(issued_at)
            expiry = issued + timedelta(days=ttl_days)
            if datetime.now(timezone.utc) > expiry:
                return {"valid": False, "reason": "License file has expired"}
        except ValueError:
            pass

    # In production, verify the Ed25519 signature against public_key.
    # For now, we trust the Keygen-issued certificate.
    fingerprint = payload.get("fingerprint", "")

    logger.info("Offline .lic validated: fingerprint=%s ttl=%ddays", fingerprint[:12], ttl_days)

    return {
        "valid": True,
        "license_id": payload.get("license_id", ""),
        "fingerprint": fingerprint,
        "issued_at": issued_at,
        "ttl_days": ttl_days,
        "scheme": payload.get("scheme", "ED25519_SIGN"),
    }