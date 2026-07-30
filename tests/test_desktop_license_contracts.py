import base64
import unittest

from middleware.desktop_license_contracts import (
    build_desktop_license_file,
    build_desktop_license_filename,
    enforce_license_scope,
    extract_desktop_request_token,
    verify_admin_bearer_token,
)
from middleware.qr_short_codes import create_response_token


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


class DesktopLicenseContractTests(unittest.TestCase):
    def test_rejects_license_for_different_product_id(self):
        row = {"product_id": "product-clock", "policy_id": "policy-pro"}
        body = {"product_id": "product-invoice", "policy": "policy-pro"}

        result = enforce_license_scope(row, body)

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["code"], "PRODUCT_SCOPE_MISMATCH")
        self.assertIn("not valid for this product", result["message"])

    def test_rejects_license_for_different_policy_when_policy_is_requested(self):
        row = {"product_id": "product-clock", "policy_id": "policy-basic"}
        body = {"product_id": "product-clock", "policy": "policy-pro"}

        result = enforce_license_scope(row, body)

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["code"], "POLICY_SCOPE_MISMATCH")
        self.assertIn("not valid for this edition", result["message"])

    def test_accepts_any_policy_under_matching_product_when_no_policy_requested(self):
        row = {"product_id": "product-clock", "policy_id": "policy-basic"}
        body = {"product_id": "product-clock", "policy": ""}

        self.assertIsNone(enforce_license_scope(row, body))

    def test_extracts_mrbreq_token_from_lreq_file_contents(self):
        token = "MRBREQ1." + b64url("v=1;key=LIC-123;fp=abc123;prod=Clock;nonce=n-1;status=request")
        contents = "# Mereb offline activation request\n# send this file\n\n" + token + "\n"

        self.assertEqual(extract_desktop_request_token(contents), token)

    def test_builds_desktop_compatible_license_file_around_mrb1_token(self):
        fields = {"key": "LIC-123", "fp": "abc123", "prod": "Clock", "nonce": "n-1"}
        token = create_response_token(fields, bytes(range(32)), seats=3)

        contents = build_desktop_license_file(
            response_token=token,
            product="Clock",
            fingerprint="abc123",
            license_id="license-uuid",
            machines_count=1,
            machines_limit=3,
            expires_at=None,
        )

        self.assertIn("# MerebHub offline license", contents)
        self.assertIn(token, contents)
        non_comment_lines = [line.strip() for line in contents.splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual(non_comment_lines, [token])

    def test_license_filename_is_safe_and_uses_lic_extension(self):
        filename = build_desktop_license_filename("Clock / Pro Edition", "abcdef1234567890")

        self.assertEqual(filename, "Clock-Pro-Edition-abcdef12.lic")

    def test_admin_token_accepts_bearer_or_dedicated_header(self):
        self.assertTrue(verify_admin_bearer_token("secret", "Bearer secret", ""))
        self.assertTrue(verify_admin_bearer_token("secret", "", "secret"))
        self.assertFalse(verify_admin_bearer_token("secret", "Bearer wrong", ""))
        self.assertFalse(verify_admin_bearer_token("", "Bearer secret", ""))


if __name__ == "__main__":
    unittest.main()
