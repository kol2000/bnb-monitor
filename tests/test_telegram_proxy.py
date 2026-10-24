import os
import unittest
from unittest.mock import patch, MagicMock
from urllib.request import Request, ProxyHandler, HTTPSHandler
import test_monitor as base
import binance
app = base.mod

class ProxyTests(unittest.TestCase):
    def test_explicit_proxy_survives_no_proxy(self):
        with patch.dict(os.environ, {'TELEGRAM_PROXY_URL':'http://192.168.1.13:3128','NO_PROXY':'*'}):
            captured = []
            def transport(handler, req):
                captured.append((req.host, req._tunnel_host))
                raise OSError('offline test')
            with patch.object(HTTPSHandler, 'https_open', transport):
                opener = app.telegram_opener()
                with self.assertRaises(OSError):
                    opener.open(Request('https://api.telegram.org/botTEST/sendMessage',data=b'{}'))
            self.assertEqual(captured, [('192.168.1.13:3128', 'api.telegram.org')])

    def test_invalid_proxy_is_rejected(self):
        for value in ['socks5://host:1080','http://user:secret@host:3128','http://host:bad','http://host:3128/path']:
            with patch.dict(os.environ, {'TELEGRAM_PROXY_URL':value}):
                with self.assertRaises(app.RemoteError): app.telegram_opener()

    def test_telegram_only_and_no_direct_fallback(self):
        with patch('app.telegram_opener') as factory, patch('app.urlopen') as direct:
            factory.return_value.open.side_effect=OSError('secret details')
            with self.assertRaises(app.RemoteError) as error:
                app.post_json('https://api.telegram.org/botTEST/sendMessage',{})
            self.assertNotIn('secret',str(error.exception))
            direct.assert_not_called()
            direct.return_value.__enter__.return_value.read.return_value=b'{"result":"0x1"}'
            factory.reset_mock()
            self.assertEqual(app.post_json('https://rpc.example/',{}),{'result':'0x1'})
            factory.assert_not_called()

    def test_binance_disables_environment_proxy(self):
        client=object.__new__(binance.Client)
        with patch('binance.build_opener') as factory:
            factory.return_value.open.return_value.__enter__.return_value.read.return_value=b'{}'
            client.call('/api/v3/time',signed=False)
            handler=factory.call_args.args[0]
            self.assertIsInstance(handler,ProxyHandler)
            self.assertEqual(handler.proxies,{})

    def test_redirects_are_not_followed(self):
        self.assertIsNone(app.TelegramNoRedirect().redirect_request(None,None,302,'',{},'https://example.org'))
