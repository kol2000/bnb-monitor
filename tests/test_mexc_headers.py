import hashlib
import hmac
import io
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
import mexc

class HeaderTests(unittest.TestCase):
    def client(self):
        c=object.__new__(mexc.Client)
        c.key='fake-key'; c.secret='fake-secret'
        return c

    def test_post_headers_and_signature(self):
        for path in ('/api/v3/order/test','/api/v3/order','/api/v3/capital/withdraw'):
            opener=Mock()
            opener.open.return_value=io.BytesIO(b'{}')
            with patch.object(mexc,'build_opener',return_value=opener):
                self.client().call(path,{'symbol':'BNBUSDT'},method='POST')
            req=opener.open.call_args.args[0]
            self.assertEqual(req.get_header('Content-type'),'application/json')
            self.assertEqual(req.get_header('X-mexc-apikey'),'fake-key')
            query,sig=req.full_url.split('?',1)[1].rsplit('&signature=',1)
            self.assertEqual(sig,hmac.new(b'fake-secret',query.encode(),hashlib.sha256).hexdigest())
            self.assertEqual(req.data,b'')
            self.assertEqual(opener.open.call_count,1)

    def test_http_error_code_without_sensitive_body(self):
        opener=Mock()
        opener.open.side_effect=HTTPError('secret-url',400,'bad',{},io.BytesIO(b'{"code":700013,"msg":"fake-secret"}'))
        with patch.object(mexc,'build_opener',return_value=opener):
            with self.assertRaises(mexc.MexcError) as caught:
                self.client().call('/api/v3/order/test',method='POST')
        self.assertIn('700013',str(caught.exception))
        self.assertNotIn('fake-secret',str(caught.exception))
        self.assertNotIn('secret-url',str(caught.exception))
        self.assertEqual(opener.open.call_count,1)
