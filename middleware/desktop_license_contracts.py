"""Desktop licensing contract helpers shared by online, QR, and .lic flows."""

from __future__ import annotations

import re
import hmac
from datetime import datetime, timezone
from typing import Mapping, Optional

REQUEST_PREFIX = "MRBREQ1."
RESPONSE_PREFIX = "MRB1."


def _read_mapping_value(source: Mapping[str, object], *names: str) -> str:
    for name in names:
        value = source.get(name)
        if value is not None:
            return str(value).strip()
    return ""


def normalize_desktop_scope(body: Mapping[str, object]) -> dict[str, str]:
    """Return product/policy UUID scope requested by a desktop build."""
    return {
        "product_id": _read_mapping_value(body, "product_id", "productId", "product"),
        "policy_id": _read_mapping_value(body, "policy", "policy_id", "policyId"),
    }


def enforce_license_scope(license_row: Mapping[str, object], body: Mapping[str, object]) -> Optional[dict[str, str]]:
    """
    Return an activation rejection when a desktop build asks for a product or
    policy scope that does not match the license row. A blank requested policy
    means any policy under the requested product is allowed.
    """
    requested = normalize_desktop_scope(body)
    requested_product = requested["product_id"]
    requested_policy = requested["policy_id"]
    license_product = _read_mapping_value(license_row, "product_id")
    license_policy = _read_mapping_value(license_row, "policy_id")

    if requested_product and license_product and requested_product.lower() != license_product.lower():
        return {
            "status": "invalid",
            "code": "PRODUCT_SCOPE_MISMATCH",
            "message": "This license is not valid for this product.",
        }

    if requested_policy and license_policy and requested_policy.lower() != license_policy.lower():
        return {
            "status": "invalid",
            "code": "POLICY_SCOPE_MISMATCH",
            "message": "This license is not valid for this edition.",
        }

    return None


def extract_desktop_request_token(contents_or_token: str) -> str:
    """Extract an MRBREQ1 token from raw text or a human-readable .lreq file."""
    text = contents_or_token or ""
    if text.strip().startswith(REQUEST_PREFIX):
        return "".join(text.strip().split())

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(REQUEST_PREFIX):
            return "".join(line.split())

    return ""


def build_desktop_license_file(
    *,
    response_token: str,
    product: str,
    fingerprint: str,
    license_id: str,
    machines_count: int,
    machines_limit: int,
    expires_at: Optional[str],
) -> str:
    """Build a .lic file that current desktop apps can import."""
    issued = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines = [
        "# MerebHub offline license",
        f"# Product: {product or 'Unknown product'}",
        f"# License ID: {license_id or 'unknown'}",
        f"# Machine: {(fingerprint or '')[:32]}",
        f"# Seats: {machines_count}/{machines_limit}",
        f"# Issued: {issued}",
    ]
    if expires_at:
        lines.append(f"# Expires: {expires_at}")
    lines.extend([
        "#",
        "# Import this file in the desktop app: Activation -> Offline file -> Import .lic file.",
        "# Do not edit the activation token below.",
        "",
        response_token.strip(),
        "",
    ])
    return "\n".join(lines)


def build_desktop_license_filename(product: str, fingerprint: str) -> str:
    """Return a safe human-readable .lic filename."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", product or "MerebHub-License").strip("-")
    if not slug:
        slug = "MerebHub-License"
    return f"{slug[:48]}-{(fingerprint or 'machine')[:8]}.lic"


def verify_admin_bearer_token(configured_token: str, authorization: str, header_token: str) -> bool:
    """Verify an admin token from Authorization: Bearer or X-MerebHub-Admin-Token."""
    expected = (configured_token or "").strip()
    if not expected:
        return False

    supplied = (header_token or "").strip()
    auth = (authorization or "").strip()
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()

    return bool(supplied) and hmac.compare_digest(supplied, expected)
