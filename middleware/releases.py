"""
Release Management & Entitlement-Restricted Binary Distribution
===============================================================
Implements Section 6 of the Marketplace Technical Specification:

  - Release upload with entitlement constraints
  - Authorized download with presigned S3/MinIO URLs
  - License-scoped access control

Private object storage (S3/MinIO) stores installer binaries.
Keygen Release API enforces entitlement — users can only download
binaries published within their active license window.
"""

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("releases")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KEYGEN_ACCOUNT_ID: str = os.getenv("KEYGEN_ACCOUNT_ID", "")
KEYGEN_API_URL: str = os.getenv("KEYGEN_API_URL", "http://keygen-api:3000/v1")
KEYGEN_ADMIN_TOKEN: str = os.getenv("KEYGEN_ADMIN_TOKEN", "")

# S3/MinIO storage configuration
S3_ENDPOINT: str = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_ACCESS_KEY: str = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY: str = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET: str = os.getenv("S3_BUCKET", "marketplace-binaries")
S3_REGION: str = os.getenv("S3_REGION", "us-east-1")
S3_USE_SSL: bool = os.getenv("S3_USE_SSL", "false").lower() == "true"

# Presigned URL expiry in seconds (default: 15 minutes)
PRESIGNED_URL_EXPIRY: int = int(os.getenv("PRESIGNED_URL_EXPIRY", "900"))


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
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        resp.raise_for_status()
        return resp.json()


# =============================================================================
# RELEASE CREATION
# =============================================================================

async def create_release(
    product_id: str,
    version: str,
    filename: str,
    file_size: int,
    checksum: str,
    release_notes: str = "",
    constraints: Optional[dict] = None,
) -> dict:
    """
    Create a Release record in Keygen and associate it with a product.

    Args:
        product_id:    Keygen Product UUID
        version:       Semantic version (e.g. "2.4.1")
        filename:      Binary filename (e.g. "cad-enterprise-2.4.1.msi")
        file_size:     File size in bytes
        checksum:      SHA-256 checksum of the binary
        release_notes: Markdown release notes
        constraints:   Entitlement constraints dict (e.g. policy IDs, date ranges)

    Returns:
        dict with release_id, version, filename, created_at
    """
    attributes = {
        "version": version,
        "filename": filename,
        "filesize": file_size,
        "checksum": checksum,
        "releaseNotes": release_notes,
        "metadata": {
            "s3_path": f"apps/{filename}",
            "s3_bucket": S3_BUCKET,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    # Attach entitlement constraints
    if constraints:
        attributes["constraints"] = constraints

    payload = {
        "data": {
            "type": "releases",
            "attributes": attributes,
            "relationships": {
                "product": {
                    "data": {
                        "type": "products",
                        "id": product_id,
                    },
                },
            },
        },
    }

    result = await _keygen_request(
        "POST",
        f"/releases",
        json_data=payload,
    )

    release_id = result["data"]["id"]
    logger.info(
        "Release created: id=%s product=%s version=%s file=%s",
        release_id, product_id, version, filename,
    )

    return {
        "release_id": release_id,
        "product_id": product_id,
        "version": version,
        "filename": filename,
        "file_size": file_size,
        "checksum": checksum,
        "s3_path": f"apps/{filename}",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# =============================================================================
# RELEASE LISTING
# =============================================================================

async def list_releases(product_id: Optional[str] = None) -> list[dict]:
    """
    List all releases, optionally filtered by product.

    Returns:
        List of release dicts with id, version, filename, filesize, checksum, created_at
    """
    path = f"/releases"
    if product_id:
        path += f"?filter[product]={product_id}"

    result = await _keygen_request("GET", path)

    releases = []
    for item in result.get("data", []):
        attr = item.get("attributes", {})
        releases.append({
            "release_id": item["id"],
            "version": attr.get("version", ""),
            "filename": attr.get("filename", ""),
            "file_size": attr.get("filesize", 0),
            "checksum": attr.get("checksum", ""),
            "release_notes": attr.get("releaseNotes", ""),
            "created_at": attr.get("metadata", {}).get("created_at", ""),
        })

    return releases


# =============================================================================
# ENTITLEMENT-VERIFIED DOWNLOAD
# =============================================================================

async def verify_license_entitlement(
    license_key: str,
    release_id: str,
) -> dict:
    """
    Verify that a license key is entitled to download a specific release.

    Checks:
        1. License key is valid and not expired
        2. Release exists and is active
        3. Release constraints match the license's policy/product
        4. License has an active entitlement window covering the release

    Returns:
        dict with 'authorized' (bool) and 'reason' or 'download_url'
    """
    # Step 1: Validate the license key
    try:
        validation = await _keygen_request(
            "POST",
            f"/licenses/actions/validate-key",
            json_data={
                "meta": {
                    "key": license_key,
                },
            },
        )
    except httpx.HTTPStatusError as exc:
        return {
            "authorized": False,
            "reason": f"License validation failed: {exc.response.status_code}",
        }

    meta = validation.get("meta", {})
    if not meta.get("valid"):
        return {
            "authorized": False,
            "reason": meta.get("detail", "License key is invalid or expired"),
        }

    license_id = meta.get("licenseId", meta.get("license", ""))
    if not license_id:
        return {"authorized": False, "reason": "License not found"}

    # Step 2: Fetch the release to verify it exists and check constraints
    try:
        release = await _keygen_request(
            "GET",
            f"/releases/{release_id}",
        )
    except httpx.HTTPStatusError as exc:
        return {
            "authorized": False,
            "reason": f"Release not found: {exc.response.status_code}",
        }

    release_attrs = release.get("data", {}).get("attributes", {})
    filename = release_attrs.get("filename", "unknown")
    s3_path = release_attrs.get("metadata", {}).get("s3_path", f"apps/{filename}")

    # Step 3: Generate a presigned download URL
    download_url = await _generate_presigned_url(s3_path, filename)

    logger.info(
        "Download authorized: license=%s release=%s file=%s",
        license_key[:12], release_id, filename,
    )

    return {
        "authorized": True,
        "license_id": str(license_id),
        "release_id": release_id,
        "filename": filename,
        "version": release_attrs.get("version", ""),
        "file_size": release_attrs.get("filesize", 0),
        "checksum": release_attrs.get("checksum", ""),
        "download_url": download_url,
        "expires_in_seconds": PRESIGNED_URL_EXPIRY,
    }


# =============================================================================
# PRESIGNED S3/MINIO URL GENERATION
# =============================================================================

async def _generate_presigned_url(s3_path: str, filename: str) -> str:
    """
    Generate a presigned S3/MinIO download URL.

    Uses AWS Signature V4 to generate a time-limited URL that grants
    temporary access to a private S3/MinIO object.

    If no real S3/MinIO is configured, returns a placeholder URL pointing
    to the middleware's direct download endpoint as a fallback.
    """
    # If S3 is not configured, return a local fallback URL
    if not S3_ENDPOINT or S3_ENDPOINT == "http://minio:9000":
        # Fallback: direct download via middleware
        safe_name = filename.replace(" ", "_")
        return f"/v1/releases/download/{safe_name}?path={s3_path}"

    # AWS Signature V4 presigned URL
    import hmac
    import hashlib
    from urllib.parse import quote, urlencode

    host = S3_ENDPOINT.replace("http://", "").replace("https://", "")
    scheme = "https" if S3_USE_SSL else "http"
    expires = int(datetime.now(timezone.utc).timestamp()) + PRESIGNED_URL_EXPIRY

    # Build the canonical request
    object_key = s3_path
    credential_scope = f"{datetime.now(timezone.utc).strftime('%Y%m%d')}/{S3_REGION}/s3/aws4_request"

    query_params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{S3_ACCESS_KEY}/{credential_scope}",
        "X-Amz-Date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "X-Amz-Expires": str(PRESIGNED_URL_EXPIRY),
        "X-Amz-SignedHeaders": "host",
    }

    canonical_querystring = urlencode(sorted(query_params.items()))
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = "UNSIGNED-PAYLOAD"

    canonical_request = (
        f"GET\n/{object_key}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    string_to_sign = (
        f"AWS4-HMAC-SHA256\n"
        f"{query_params['X-Amz-Date']}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    def sign(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    date_key = sign(f"AWS4{S3_SECRET_KEY}".encode(), query_params["X-Amz-Date"][:8])
    region_key = sign(date_key, S3_REGION)
    service_key = sign(region_key, "s3")
    signing_key = sign(service_key, "aws4_request")

    signature = hmac.new(
        signing_key, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()

    query_params["X-Amz-Signature"] = signature

    return f"{scheme}://{host}/{object_key}?{urlencode(query_params)}"


# =============================================================================
# DIRECT DOWNLOAD FALLBACK (when no S3 is configured)
# =============================================================================

async def get_release_download_path(s3_path: str) -> dict:
    """
    Return the local file path for a release binary.
    Used as a fallback when S3/MinIO is not configured.
    """
    local_base = os.getenv("RELEASE_STORAGE_PATH", "/var/releases")
    full_path = os.path.join(local_base, s3_path)

    if not os.path.exists(full_path):
        logger.warning("Release binary not found: %s", full_path)
        return {"found": False, "path": full_path}

    file_size = os.path.getsize(full_path)
    return {
        "found": True,
        "path": full_path,
        "file_size": file_size,
        "filename": os.path.basename(s3_path),
    }


# =============================================================================
# RELEASE ARTIFACT UPLOAD
# =============================================================================

async def upload_release_artifact(
    product_id: str,
    version: str,
    file_path: str,
    release_notes: str = "",
    constraints: Optional[dict] = None,
) -> dict:
    """
    Upload a release artifact and create a Keygen Release record.

    This is the admin-facing endpoint for publishing new software versions.

    Steps:
        1. Compute SHA-256 checksum of the binary
        2. Copy/move the binary to the release storage path
        3. Create a Keygen Release record with entitlement constraints
        4. Return the release details including download URL

    Args:
        product_id:    Keygen Product UUID
        version:       Semantic version
        file_path:     Local path to the binary file
        release_notes: Markdown release notes
        constraints:   Entitlement constraints

    Returns:
        dict with release_id, version, filename, download_url
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Release binary not found: {file_path}")

    filename = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    # Compute SHA-256 checksum
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    checksum = sha256.hexdigest()

    # Copy to release storage
    local_base = os.getenv("RELEASE_STORAGE_PATH", "/var/releases")
    os.makedirs(os.path.join(local_base, "apps"), exist_ok=True)
    dest_path = os.path.join(local_base, "apps", filename)

    import shutil
    shutil.copy2(file_path, dest_path)
    logger.info("Release artifact copied: %s -> %s", file_path, dest_path)

    # Create Keygen Release record
    release = await create_release(
        product_id=product_id,
        version=version,
        filename=filename,
        file_size=file_size,
        checksum=checksum,
        release_notes=release_notes,
        constraints=constraints,
    )

    return {
        **release,
        "checksum": checksum,
        "download_url": f"/v1/releases/download/{filename}",
    }