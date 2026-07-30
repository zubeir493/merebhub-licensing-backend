"""QR short-code helpers for phone-assisted desktop activation."""

import base64
import binascii
import hashlib
import hmac
import html
from datetime import datetime, timezone
from typing import Dict, Optional

REQUEST_PREFIX = "MRBREQ1"
RESPONSE_PREFIX = "MRB1"
SHORT_CODE_VERSION = "MRBSC1"
SHORT_CODE_LENGTH = 16


class ActivationRequestError(ValueError):
    """Raised when a desktop QR activation request cannot be read."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ActivationRequestError("The activation request is not valid base64url.") from exc


def _read_token(text: str, index: int, terminator: str) -> tuple[Optional[str], int]:
    chars = []
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 1
            if index >= len(text):
                return None, index
            chars.append(text[index])
            index += 1
            continue
        if char == terminator:
            index += 1
            return "".join(chars), index
        chars.append(char)
        index += 1
    return "".join(chars), index


def parse_field_string(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    index = 0
    while index < len(text):
        name, index = _read_token(text, index, "=")
        if name is None or not name:
            raise ActivationRequestError("The activation request contains an invalid field name.")
        value, index = _read_token(text, index, ";")
        if value is None:
            raise ActivationRequestError("The activation request contains an invalid field value.")
        fields[name] = value
    return fields


def _escape_value(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("\\", "\\\\").replace(";", "\\;").replace("=", "\\=")


def _canonical_response_fields(fields: Dict[str, str], *, issued_at: Optional[datetime] = None, expires_at: Optional[str] = None, seats: int = 0, status: str = "activated") -> str:
    issued = issued_at or datetime.now(timezone.utc)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    pairs = [
        ("v", "1"),
        ("key", fields.get("key", "")),
        ("fp", fields.get("fp", "")),
        ("prod", fields.get("prod", "")),
        ("iat", issued.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")),
    ]
    if expires_at:
        pairs.append(("exp", expires_at))
    pairs.append(("nonce", fields.get("nonce", "")))
    if seats > 0:
        pairs.append(("seats", str(seats)))
    pairs.append(("status", status))
    return ";".join(f"{name}={_escape_value(value)}" for name, value in pairs)


def parse_activation_request(request_token: str) -> Dict[str, str]:
    cleaned = "".join((request_token or "").split())
    parts = cleaned.split(".", 1)
    if len(parts) != 2 or parts[0] != REQUEST_PREFIX:
        raise ActivationRequestError("Expected an MRBREQ1 activation request.")
    payload = _b64url_decode(parts[1]).decode("utf-8")
    fields = parse_field_string(payload)
    if not fields.get("key"):
        raise ActivationRequestError("The activation request is missing its license key.")
    if not fields.get("fp"):
        raise ActivationRequestError("The activation request is missing its device fingerprint.")
    if not fields.get("nonce"):
        raise ActivationRequestError("The activation request is missing its request nonce.")
    return fields


def create_response_token(fields: Dict[str, str], signing_key: bytes, *, issued_at: Optional[datetime] = None, expires_at: Optional[str] = None, seats: int = 0, status: str = "activated") -> str:
    if not signing_key:
        raise ValueError("A signing key is required.")
    payload = _canonical_response_fields(fields, issued_at=issued_at, expires_at=expires_at, seats=seats, status=status)
    payload_bytes = payload.encode("utf-8")
    signature = hmac.digest(signing_key, payload_bytes, hashlib.sha256)
    return f"{RESPONSE_PREFIX}.{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def _short_code_material(fields: Dict[str, str]) -> bytes:
    pieces = [
        SHORT_CODE_VERSION,
        fields.get("key", ""),
        fields.get("fp", ""),
        fields.get("prod", ""),
        fields.get("nonce", ""),
    ]
    return "|".join(pieces).encode("utf-8")


def create_short_code(fields: Dict[str, str], signing_key: bytes) -> str:
    if not signing_key:
        raise ValueError("A signing key is required.")
    digest = hmac.digest(signing_key, _short_code_material(fields), hashlib.sha256)
    compact = base64.b32encode(digest).decode("ascii").rstrip("=")[:SHORT_CODE_LENGTH]
    return "-".join(compact[i:i + 4] for i in range(0, len(compact), 4))


def verify_short_code(code: str, fields: Dict[str, str], signing_key: bytes) -> bool:
    normalized = "".join((code or "").upper().split()).replace("-", "")
    expected = create_short_code(fields, signing_key).replace("-", "")
    return hmac.compare_digest(normalized, expected)


def build_short_code_payload(fields: Dict[str, str], signing_key: bytes, *, license_id: str, machines_count: int, machines_limit: int, expires_at: Optional[str] = None) -> dict:
    response_token = create_response_token(fields, signing_key, expires_at=expires_at, seats=machines_limit)
    short_code = create_short_code(fields, signing_key)
    return {
        "status": "activated",
        "short_code": short_code,
        "response_token": response_token,
        "license_id": license_id,
        "fingerprint": fields.get("fp", ""),
        "product": fields.get("prod", ""),
        "nonce": fields.get("nonce", ""),
        "machines_count": machines_count,
        "machines_limit": machines_limit,
        "expires_at": expires_at,
        "message": "Enter this short code in the desktop app to finish activation.",
    }


def render_short_code_html(payload: dict) -> str:
    code = html.escape(payload.get("short_code", ""))
    product = html.escape(payload.get("product") or "this app")
    fingerprint = html.escape((payload.get("fingerprint") or "")[:16])
    token = html.escape(payload.get("response_token", ""))
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>MerebHub offline activation</title>
  <style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;line-height:1.45;color:#111}}.card{{max-width:560px;margin:auto;border:1px solid #ddd;border-radius:16px;padding:24px;box-shadow:0 8px 24px #0001}}.code{{font-size:34px;font-weight:800;letter-spacing:3px;text-align:center;padding:18px;border-radius:12px;background:#f4f7ff;color:#123}}textarea{{width:100%;height:92px;font-family:monospace;font-size:12px}}</style>
</head>
<body>
  <main class=\"card\">
    <h1>Activation code</h1>
    <p>Enter this code on the offline computer to activate <strong>{product}</strong>.</p>
    <div class=\"code\">{code}</div>
    <p>Device: <code>{fingerprint}</code>...</p>
    <details><summary>Compatibility response token</summary><p>If the desktop build asks for a long response token, paste this instead:</p><textarea readonly>{token}</textarea></details>
  </main>
</body>
</html>"""
