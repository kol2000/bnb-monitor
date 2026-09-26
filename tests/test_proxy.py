import json
import unittest
from unittest.mock import patch, MagicMock
import test_monitor
import app as mod

class ProxyTests(unittest.TestCase):
    def setUp(self):
        base = test_monitor.MonitorTests()
        base.setUp()
        self.post = base.post
        self.client = base.client
        self.cfg = dict(telegram_proxy_enabled=True, telegram_proxy_host='proxy.example.org',
                        telegram_proxy_port=1080, telegram_proxy_username='user@:/',
                        telegram_proxy_password='pass@:/#%')
        self.assertEqual(self.post('settings',self.cfg).status_code,200)
        self.post('settings',dict(telegram_token='123:'+'a'*25,telegram_chat='123'))

    def test_secret_preservation_and_redaction(self):
        state=self.client.get('/api/state').json['settings']
        self.assertNotIn('telegram_proxy_password',state)
        self.assertTrue(state['telegram_proxy_password_set'])
        self.post('settings',{'telegram_proxy_password':''})
        self.assertEqual(mod.settings()['telegram_proxy_password'],self.cfg['telegram_proxy_password'])
        self.assertEqual(self.post('settings',{'telegram_proxy_enabled':False,'clear_telegram_proxy_password':True}).status_code,200)
        self.assertEqual(mod.settings()['telegram_proxy_password'],'')

    def test_validation(self):
        for data in ({'telegram_proxy_host':'socks5://user:pass@host:80'},
                     {'telegram_proxy_host':'host/path'},{'telegram_proxy_port':0},
                     {'telegram_proxy_enabled':'true'},
                     {'telegram_proxy_username':'x\nY'},
                     {'clear_telegram_proxy_password':True}):
            self.assertEqual(self.post('settings',data).status_code,400)
        self.assertEqual(self.post('settings',{'telegram_proxy_host':'[2001:db8::1]'}).status_code,200)

    def test_socks_dns_auth_and_no_environment_proxy(self):
        with patch('requests.Session') as session, patch('app.post_json') as direct:
            client=session.return_value.__enter__.return_value
            response=client.post.return_value.__enter__.return_value
            response.status_code=200
            response.iter_content.return_value=[b'{"ok":true}']
            mod.telegram(mod.settings(),'test')
            self.assertFalse(client.trust_env)
            kw=client.post.call_args.kwargs
            self.assertEqual(kw['proxies']['https'],'socks5h://user%40%3A%2F:pass%40%3A%2F%23%25@proxy.example.org:1080')
            self.assertFalse(kw['allow_redirects'])
            self.assertEqual(kw['timeout'],(10,12))
            direct.assert_not_called()

    def test_failure_is_redacted_and_no_direct_fallback(self):
        with patch('requests.Session',side_effect=Exception('pass@:/#% bot secret')), patch('app.post_json') as direct:
            with self.assertRaises(mod.RemoteError) as err: mod.telegram(mod.settings(),'test')
            self.assertNotIn('secret',str(err.exception))
            self.assertNotIn('pass@',str(err.exception))
            direct.assert_not_called()

    def test_disabled_proxy_and_rpc_stay_direct(self):
        cfg=mod.settings();cfg['telegram_proxy_enabled']=False
        with patch('app.post_json',return_value={'ok':True}) as direct, patch('app.telegram_proxy_post') as proxy:
            mod.telegram(cfg,'test')
            direct.assert_called_once();proxy.assert_not_called()
        with patch('app.post_json',return_value={'result':'0x38'}) as direct, patch('app.telegram_proxy_post') as proxy:
            self.assertEqual(mod.rpc('https://rpc.example.org','eth_chainId',[]),'0x38')
            direct.assert_called_once();proxy.assert_not_called()

    def test_test_endpoint_uses_saved_proxy(self):
        with patch('app.telegram_proxy_post',return_value={'ok':True}) as proxy:
            self.assertEqual(self.post('telegram/test',{}).status_code,200)
        self.assertEqual(proxy.call_args.args[0]['telegram_proxy_host'],'proxy.example.org')

    def test_oversized_response_rejected(self):
        with patch('requests.Session') as session:
            response=session.return_value.__enter__.return_value.post.return_value.__enter__.return_value
            response.status_code=200
            response.iter_content.return_value=[b'x'*2_000_001]
            with self.assertRaises(mod.RemoteError):mod.telegram(mod.settings(),'test')
