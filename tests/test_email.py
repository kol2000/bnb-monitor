import json
import smtplib
import unittest
from unittest.mock import patch
import test_monitor
import app as mod
import worker
from email_notifications import send_email, EmailError, validate_settings

class EmailTests(unittest.TestCase):
    def setUp(self):
        self.base = test_monitor.MonitorTests()
        self.base.setUp()
        with mod.db() as c:
            c.execute('DELETE FROM email_outbox')
        self.client = self.base.client
        self.post = self.base.post
        self.config = dict(email_enabled=True, smtp_host='smtp.mail.ru', smtp_port=465,
                           smtp_security='ssl', smtp_username='sender@example.org',
                           smtp_password='test-app-password', email_from='sender@example.org',
                           email_to='recipient@example.net')
        self.assertEqual(self.post('settings', self.config).status_code, 200)
        self.wid = self.base.wallet()

    def queue(self, telegram=False):
        if telegram:
            self.post('settings', dict(telegram_enabled=True, telegram_token='123:'+'x'*24, telegram_chat='123'))
        cfg = mod.settings()
        mod.record_balance(self.wid, 0, 100, cfg)
        mod.record_balance(self.wid, 10**18, 101, cfg)

    def email_row(self):
        with mod.db() as c:
            return dict(c.execute('SELECT * FROM email_outbox').fetchone())

    def test_email_without_telegram_and_no_duplicate(self):
        self.queue()
        mod.record_balance(self.wid, 10**18, 101, mod.settings())
        with patch('worker.send_email') as send, patch.object(worker.stop, 'wait'):
            worker.deliver_email(); worker.deliver_email()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0]['email_to'], 'recipient@example.net')
        self.assertEqual(self.email_row()['delivery'], 'sent')
        self.assertEqual(self.base.events()[0]['delivery'], 'off')

    def test_telegram_failure_does_not_block_email(self):
        self.queue(telegram=True)
        with patch('worker.telegram', side_effect=mod.RemoteError('offline')), patch.object(worker.stop, 'wait'):
            worker.deliver()
        with patch('worker.send_email') as send, patch.object(worker.stop, 'wait'):
            worker.deliver_email()
        send.assert_called_once()
        self.assertEqual(self.email_row()['delivery'], 'sent')
        self.assertEqual(self.base.events()[0]['delivery'], 'pending')

    def test_retry_survives_init_and_keeps_message_id(self):
        self.queue(); original=self.email_row()['message_id']
        with patch('worker.send_email', side_effect=EmailError('offline')), patch.object(worker.stop, 'wait'):
            worker.deliver_email()
        self.assertEqual(self.email_row()['attempts'], 1)
        mod.init_db()
        with mod.db() as c: c.execute('UPDATE email_outbox SET retry_at=0')
        with patch('worker.send_email') as send, patch.object(worker.stop, 'wait'):
            worker.deliver_email()
        self.assertEqual(send.call_args.args[2], original)
        self.assertEqual(self.email_row()['delivery'], 'sent')

    def test_change_recipient_cancels_queue(self):
        self.queue()
        self.assertEqual(self.post('settings', {'email_to':'new@example.org'}).status_code,200)
        self.assertEqual(self.email_row()['delivery'], 'cancelled')
        with patch('worker.send_email') as send: worker.deliver_email()
        send.assert_not_called()

    def test_secrets_validation_and_test_endpoint(self):
        state=self.client.get('/api/state').json
        self.assertNotIn('smtp_password',state['settings'])
        self.assertTrue(state['settings']['smtp_password_set'])
        self.assertEqual(self.post('settings',{'smtp_password':''}).status_code,200)
        self.assertEqual(mod.settings()['smtp_password'],'test-app-password')
        self.assertEqual(self.post('settings',{'email_to':'x@example.org\r\nBcc: x@y.org'}).status_code,400)
        with patch('app.send_email') as send:
            self.assertEqual(self.post('email/test',{}).status_code,200)
        send.assert_called_once()
        other=mod.app.test_client()
        self.assertEqual(other.post('/api/email/test',json={}).status_code,403)

    def test_baseline_decrease_threshold_and_notify_off(self):
        cfg=mod.settings()
        mod.record_balance(self.wid,10**18,100,cfg)
        mod.record_balance(self.wid,0,101,cfg)
        mod.record_balance(self.wid,1,102,cfg)
        self.post('wallets/'+str(self.wid),{'notify':False})
        mod.record_balance(self.wid,2*10**18,103,cfg)
        with mod.db() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM email_outbox').fetchone()[0],0)

    def test_smtp_transport_tls_and_recipient(self):
        cfg=mod.settings()
        with patch('email_notifications.smtplib.SMTP_SSL') as smtp:
            send_email(cfg,'Прирост BNB')
            msg=smtp.return_value.send_message.call_args.args[0]
            self.assertEqual(msg['To'],cfg['email_to'])
            self.assertEqual(msg['From'],cfg['email_from'])
        cfg['smtp_security']='starttls';cfg['smtp_port']=587
        with patch('email_notifications.smtplib.SMTP') as smtp:
            send_email(cfg,'test')
            self.assertTrue(smtp.return_value.starttls.called)
            self.assertTrue(smtp.return_value.login.called)
        with patch('email_notifications.smtplib.SMTP',side_effect=smtplib.SMTPAuthenticationError(535,b'secret')):
            with self.assertRaises(EmailError) as err: send_email(cfg,'test')
        self.assertNotIn('secret',str(err.exception))
