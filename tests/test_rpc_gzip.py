import gzip
import io
import unittest
from unittest.mock import patch
import app

class RPCGzipTests(unittest.TestCase):
    def test_gzip_rpc_response_and_timeout(self):
        response=io.BytesIO(gzip.compress(b'{"result":"0x38"}'))
        response.headers={'Content-Encoding':'gzip'}
        with patch('app.urlopen',return_value=response) as remote:
            self.assertEqual(app.rpc('https://example.test','eth_chainId',[]),'0x38')
        self.assertEqual(remote.call_args.kwargs['timeout'],30)
        self.assertEqual(remote.call_args.args[0].get_header('Accept-encoding'),'gzip')
    def test_gzip_expansion_limit(self):
        response=io.BytesIO(gzip.compress(b'x'*2_000_001));response.headers={'Content-Encoding':'gzip'}
        with patch('app.urlopen',return_value=response):
            with self.assertRaises(app.RemoteError):app.rpc('https://example.test','eth_chainId',[])
