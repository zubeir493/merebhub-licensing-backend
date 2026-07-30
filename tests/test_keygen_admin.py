import unittest
import uuid
from middleware.keygen_admin import normalize_keygen_row, safe_slug


class KeygenAdminHelperTests(unittest.TestCase):
    def test_safe_slug_builds_keygen_code(self):
        self.assertEqual(safe_slug('MerebHub Clock Pro!'), 'merebhub-clock-pro')
        self.assertEqual(safe_slug(''), 'merebhub-product')

    def test_normalize_keygen_row_converts_uuid_and_datetime_to_strings(self):
        row_id = uuid.uuid4()
        row = {'id': row_id, 'name': 'Clock', 'metadata': {'a': 1}}
        self.assertEqual(normalize_keygen_row(row), {'id': str(row_id), 'name': 'Clock', 'metadata': {'a': 1}})


if __name__ == '__main__':
    unittest.main()
