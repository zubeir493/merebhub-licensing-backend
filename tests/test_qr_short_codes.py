import base64
import hmac
import unittest
from datetime import datetime, timezone

from middleware.qr_short_codes import (
    build_short_code_payload,
    create_response_token,
    create_short_code,
    parse_activation_request,
    verify_short_code,
)


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii').rstrip('=')


class QrShortCodeTests(unittest.TestCase):
    def test_parses_desktop_mrbreq_payload(self):
        request = 'MRBREQ1.' + b64url('v=1;key=LIC-123;fp=abc123;prod=MerebHub Online Clock;iat=2026-07-30T15:00:00Z;nonce=n-1;status=request')

        fields = parse_activation_request(request)

        self.assertEqual(fields['key'], 'LIC-123')
        self.assertEqual(fields['fp'], 'abc123')
        self.assertEqual(fields['prod'], 'MerebHub Online Clock')
        self.assertEqual(fields['nonce'], 'n-1')

    def test_creates_desktop_compatible_signed_response_token(self):
        fields = {
            'key': 'LIC-123',
            'fp': 'abc123',
            'prod': 'MerebHub Online Clock',
            'nonce': 'n-1',
        }
        key = bytes(range(32))

        token = create_response_token(fields, key, issued_at=datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc), seats=3)

        self.assertTrue(token.startswith('MRB1.'))
        _, payload_b64, sig_b64 = token.split('.')
        payload = base64.urlsafe_b64decode(payload_b64 + '===').decode('utf-8')
        signature = base64.urlsafe_b64decode(sig_b64 + '===')
        self.assertIn('key=LIC-123', payload)
        self.assertIn('fp=abc123', payload)
        self.assertIn('prod=MerebHub Online Clock', payload)
        self.assertIn('nonce=n-1', payload)
        self.assertIn('status=activated', payload)
        self.assertTrue(hmac.compare_digest(signature, hmac.digest(key, payload.encode('utf-8'), 'sha256')))

    def test_short_code_is_bound_to_license_fingerprint_product_and_nonce(self):
        fields = {
            'key': 'LIC-123',
            'fp': 'abc123',
            'prod': 'MerebHub Online Clock',
            'nonce': 'n-1',
        }
        key = bytes(range(32))

        code = create_short_code(fields, key)

        self.assertRegex(code, r'^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$')
        self.assertTrue(verify_short_code(code, fields, key))
        changed_device = dict(fields, fp='different-device')
        self.assertFalse(verify_short_code(code, changed_device, key))

    def test_build_short_code_payload_returns_phone_display_values(self):
        fields = {
            'key': 'LIC-123',
            'fp': 'abc123',
            'prod': 'MerebHub Online Clock',
            'nonce': 'n-1',
        }
        key = bytes(range(32))

        payload = build_short_code_payload(fields, key, license_id='license-uuid', machines_count=1, machines_limit=3)

        self.assertEqual(payload['status'], 'activated')
        self.assertIn('short_code', payload)
        self.assertIn('response_token', payload)
        self.assertEqual(payload['license_id'], 'license-uuid')
        self.assertEqual(payload['machines_count'], 1)
        self.assertEqual(payload['machines_limit'], 3)
        self.assertNotIn('LIC-123', payload['short_code'])


if __name__ == '__main__':
    unittest.main()
