# MerebHub Developer Licensing Integration Guide

This guide shows how to connect a desktop app to the MerebHub licensing server and how to make a license work only for the intended Keygen product/policy.

Production licensing server:

```text
https://license-api.merebhub.com
```

The desktop app should talk only to the MerebHub middleware URL above. Do **not** connect a desktop app directly to the private Keygen CE container.

---

## 1. Concepts

### License key

The customer receives a license key after buying software on MerebHub/WooCommerce.

Example shape:

```text
ABCD-EFGH-IJKL-MNOP
```

Treat the key as customer-owned secret-ish data:

- send it only over HTTPS
- do not print it in logs
- do not hard-code real customer keys in demos

### Device fingerprint

The app sends a stable machine fingerprint to the server. The server stores it as a Keygen machine.

A license with 1 seat can activate 1 fingerprint. A license with 3 seats can activate 3 fingerprints.

### Product scope

Each app must have its own Keygen product UUID.

If an app sends `product_id`, the server now enforces:

```text
license.product_id == requested product_id
```

So a license bought for Product A cannot activate Product B.

### Policy / edition scope

A product can have multiple policies/editions, for example:

```text
Basic
Pro
Lifetime
```

If an app sends `policy`, the server now enforces:

```text
license.policy_id == requested policy
```

Use this when a specific demo app build should only accept a specific edition.

If an app sends only `product_id` and leaves `policy` blank, any valid policy under that product can activate the app.

---

## 2. Required app configuration

Each app build needs an `activation.config` file beside the executable.

Example:

```ini
mode=keygen
baseUrl=https://license-api.merebhub.com
accountId=<KEYGEN_ACCOUNT_UUID>
productId=<KEYGEN_PRODUCT_UUID_FOR_THIS_APP>
policyId=<OPTIONAL_POLICY_UUID_FOR_THIS_EDITION>
signatureKeyHex=<64_HEX_OFFLINE_SIGNATURE_KEY>
timeoutSeconds=15
maxRetries=3
retryBaseDelayMilliseconds=500
allowOfflineFallback=true
revalidateAfterDays=7
```

### Required fields

| Field | Required | Purpose |
|---|---:|---|
| `mode` | Yes | Use `keygen` for production middleware activation. |
| `baseUrl` | Yes | Must be `https://license-api.merebhub.com`. No `/v1` suffix. |
| `accountId` | Recommended | Keygen account UUID. |
| `productId` | Yes for product locking | Keygen product UUID for this app. |
| `policyId` | Optional | Keygen policy UUID if this build requires a specific edition. |
| `signatureKeyHex` | Yes for QR/offline | 64-character hex key matching backend `QR_OFFLINE_SIGNATURE_KEY_HEX`. |

### Product-only vs product+policy locking

Accept any edition for one product:

```ini
productId=<CLOCK_PRODUCT_UUID>
policyId=
```

Accept only the Pro policy for one product:

```ini
productId=<CLOCK_PRODUCT_UUID>
policyId=<CLOCK_PRO_POLICY_UUID>
```

---

## 3. Activation methods the app should support

MerebHub supports three desktop activation methods.

### Method 1: Online activation

Use this when the desktop has internet.

Request:

```http
POST https://license-api.merebhub.com/activate
Content-Type: application/json
```

Body:

```json
{
  "license_key": "CUSTOMER-LICENSE-KEY",
  "fingerprint": "stable-device-fingerprint",
  "product": "Human App Name",
  "product_id": "KEYGEN_PRODUCT_UUID",
  "policy": "OPTIONAL_KEYGEN_POLICY_UUID",
  "nonce": "single-use-random-nonce",
  "requested_at": "2026-07-30T21:34:00Z"
}
```

Successful response:

```json
{
  "status": "activated",
  "code": "OK",
  "message": "This machine is now licensed.",
  "license_id": "KEYGEN_LICENSE_UUID",
  "machines_count": 1,
  "machines_limit": 3,
  "expires_at": null
}
```

Common rejection responses:

```json
{"status":"invalid","code":"NOT_FOUND","message":"That license key was not found."}
{"status":"invalid","code":"PRODUCT_SCOPE_MISMATCH","message":"This license is not valid for this product."}
{"status":"invalid","code":"POLICY_SCOPE_MISMATCH","message":"This license is not valid for this edition."}
{"status":"machine_limit","code":"MACHINE_LIMIT_REACHED","message":"This license is already activated on the maximum number of machines."}
{"status":"expired","code":"EXPIRED","message":"This license has expired."}
{"status":"invalid","code":"SUSPENDED","message":"This license is suspended."}
```

The app should unlock only when `status` is `activated`.

### Method 2: QR / phone-assisted offline activation

Use this when the desktop is offline but the customer has a phone with internet.

Flow:

1. Desktop generates an `MRBREQ1...` request token.
2. Desktop renders this URL as a QR code:

```text
https://license-api.merebhub.com/offline?request=<MRBREQ1_TOKEN>
```

3. Customer scans the QR code with their phone.
4. Server validates the license, product scope, policy scope, and machine limit.
5. Phone page shows a short code like:

```text
ABCD-EFGH-IJKL-MNOP
```

6. Customer types the short code into the desktop app.
7. Desktop verifies the code against the original request fields.

The QR request token must include:

```text
key=<license key>
fp=<device fingerprint>
prod=<human app name>
pid=<Keygen product UUID>
policy=<optional Keygen policy UUID>
nonce=<single-use random nonce>
status=request
```

### Method 3: Offline `.lic` file activation

Use this for true air-gapped machines.

Flow:

1. Desktop generates a `.lreq` request file.
2. Customer sends the `.lreq` file to support/admin through an online device.
3. Admin/support uploads or submits the `.lreq` contents to the protected backend endpoint.
4. Backend validates the license, product scope, policy scope, and machine limit.
5. Backend returns a `.lic` file.
6. Customer imports the `.lic` file into the desktop app.
7. Desktop verifies the signed `MRB1...` token inside the `.lic` file.

Protected backend endpoint:

```http
POST https://license-api.merebhub.com/v1/offline/license-files
Authorization: Bearer <OFFLINE_LICENSE_FILE_TOKEN>
Content-Type: application/json
```

JSON input using raw token:

```json
{
  "request": "MRBREQ1..."
}
```

JSON input using full `.lreq` file contents:

```json
{
  "request_file_contents": "# Mereb offline activation request\n...\nMRBREQ1..."
}
```

JSON response:

```json
{
  "status": "activated",
  "filename": "MerebHubOnlineClock-abcdef12.lic",
  "lic_content": "# MerebHub offline license\n...\nMRB1...\n",
  "response_token": "MRB1...",
  "license_id": "KEYGEN_LICENSE_UUID",
  "fingerprint": "stable-device-fingerprint",
  "product": "Human App Name",
  "machines_count": 1,
  "machines_limit": 3,
  "expires_at": null
}
```

To receive the file body directly instead of JSON:

```json
{
  "request": "MRBREQ1...",
  "format": "file"
}
```

The server will return `Content-Disposition: attachment; filename="...lic"` and a plain-text `.lic` body.

Security rule: this endpoint is intentionally admin/support-only. Do not expose `OFFLINE_LICENSE_FILE_TOKEN` in browser JavaScript or a desktop app.

---

## 4. What the desktop app must verify locally

For QR and `.lic`, the desktop app receives data while it cannot directly ask the server. The app must verify all of this locally before unlocking:

- token prefix is `MRB1`
- HMAC signature is valid using `signatureKeyHex`
- license key matches the request/license typed by the customer
- fingerprint matches this machine
- product name matches this app
- nonce matches the request when the exchange is still in the same session
- status is `activated`
- expiry, if present, is still in the future

Important security note: current offline tokens use HMAC-SHA256. Because HMAC is symmetric, the desktop app contains the verification key and a determined reverse engineer could extract it. This is acceptable for demos and casual-share prevention. For stronger production offline licensing, migrate the offline token signature to asymmetric signing such as Ed25519 so the desktop app contains only a public key.

---

## 5. Building another demo app

For each new demo app:

1. Create a new Keygen product for that app.
2. Create one or more Keygen policies under that product.
3. Create/import the matching WooCommerce product or variation.
4. Store these WooCommerce meta values on the purchasable variation/product:

```text
_keygen_product_id=<KEYGEN_PRODUCT_UUID>
_keygen_policy_id=<KEYGEN_POLICY_UUID>
```

5. In the desktop app's `activation.config`, set:

```ini
productId=<KEYGEN_PRODUCT_UUID>
policyId=<KEYGEN_POLICY_UUID_OR_BLANK>
```

6. Use the shared licensing UI/service in the Windows demo app repo.
7. Verify that:
   - a valid license for this app activates
   - a license for another app returns `PRODUCT_SCOPE_MISMATCH`
   - a license for another edition returns `POLICY_SCOPE_MISMATCH` when `policyId` is set
   - machine limit is enforced
   - QR activation works
   - `.lic` activation works

---

## 6. Product and policy management

### Current production-safe method: Keygen API / Rails runner

Keygen CE can be managed programmatically. Today, bulk product/policy setup is safest through server-side scripts or Rails runner commands on the VPS.

Use this for bulk imports or controlled admin setup.

Basic workflow:

1. SSH into the VPS.
2. Go to the licensing backend directory.
3. Run commands inside the `keygen-api` container.
4. Create/update products and policies.
5. Copy product/policy UUIDs into WooCommerce meta and desktop `activation.config`.

The important output for app developers is always:

```text
Product UUID
Policy UUID
Machine limit / duration / floating settings
```

Do not send developers Keygen admin tokens, database passwords, or VPS credentials.

### Remove/deactivate a product or policy

Prefer disabling/suspending over deleting once sales exist.

Reason: old WooCommerce orders and licenses may still reference the product/policy UUID.

Recommended lifecycle:

```text
Draft/test -> Active -> Hidden/disabled -> Archived
```

Hard delete only if the product/policy was never sold and no license references it.

### WordPress admin dashboard recommendation

A WordPress dashboard is the best long-term admin experience for MerebHub because WooCommerce is already the source of products, orders, and variations.

Recommended architecture:

```text
WordPress admin screen
  -> WordPress plugin PHP server-side request
    -> FastAPI protected admin endpoint
      -> private Keygen CE / database
```

Avoid this architecture:

```text
Browser JavaScript
  -> Keygen CE directly
```

Reasons:

- Keygen CE should stay private.
- Admin tokens should never be exposed to browser JavaScript.
- WordPress can map WooCommerce products/variations to Keygen product/policy UUIDs in one place.

Suggested dashboard features:

- list Keygen products
- create product
- edit product name/code/metadata
- archive/disable product
- list policies under a product
- create policy/edition
- edit machine limit, duration, floating/strict behavior
- attach product/policy UUIDs to WooCommerce products/variations
- show license count per product/policy
- show recent activation failures such as product mismatch or machine limit

Third-party Keygen CE admin UIs exist publicly, but test them only in staging before production. A custom WordPress dashboard is more useful for MerebHub because it can connect Keygen records directly to WooCommerce products and license delivery.

---

## 7. Backend environment variables

Middleware needs these values for desktop activation:

```text
KEYGEN_ACCOUNT_ID=<account UUID>
DATABASE_URL=<private postgres URL>
KEYGEN_API_URL=http://keygen-api:3000/v1
KEYGEN_ADMIN_TOKEN=<server-side Keygen admin token>
QR_OFFLINE_SIGNATURE_KEY_HEX=<64 hex chars matching desktop signatureKeyHex>
OFFLINE_LICENSE_FILE_TOKEN=<strong admin/support token for .lic generation>
```

Never put `KEYGEN_ADMIN_TOKEN`, `DATABASE_URL`, or `OFFLINE_LICENSE_FILE_TOKEN` in a desktop app, browser JavaScript, or public documentation.

---

## 8. Developer checklist

Before handing a new demo app to QA:

- [ ] `activation.config` points to `https://license-api.merebhub.com`
- [ ] `productId` is the correct Keygen product UUID
- [ ] `policyId` is set only if this build requires a specific edition
- [ ] online activation succeeds with the correct license
- [ ] online activation rejects another product's license
- [ ] online activation rejects another policy's license when `policyId` is set
- [ ] QR URL uses `/offline?request=MRBREQ1...`
- [ ] QR short code activates the same offline machine
- [ ] `.lreq` request can be converted by admin/support into `.lic`
- [ ] `.lic` imports successfully on the offline machine
- [ ] no customer license keys or admin tokens are logged
