"""Response-size regression tests; no network or credentials."""
import io
import json
import unittest
from unittest.mock import patch, Mock
import mexc

class ResponseTests(unittest.TestCase):
    def call(self, raw):
        client = object.__new__(mexc.Client)
        opener = Mock()
        opener.open.return_value = io.BytesIO(raw)
        with patch.object(mexc, 'build_opener', return_value=opener):
            return client.call('/api/v3/capital/config/getall', signed=False)

    def test_configuration_over_four_megabytes(self):
        data = [{'coin':'OTHER','padding':'x'*4000100},
                {'coin':'BNB','networkList':[{'netWork':'BSC'}]}]
        self.assertEqual(self.call(json.dumps(data).encode()), data)

    def test_oversize_is_explicit_not_truncated_json(self):
        with patch.object(mexc,'MAX_RESPONSE_BYTES',64):
            with self.assertRaisesRegex(mexc.MexcError,'превышает лимит'):
                self.call(b' '*65)

    def test_invalid_json_error_does_not_expose_body(self):
        with self.assertRaises(mexc.MexcError) as caught:
            self.call(b'{"private":"do-not-display"')
        self.assertIn('JSON', str(caught.exception))
        self.assertNotIn('do-not-display', str(caught.exception))
