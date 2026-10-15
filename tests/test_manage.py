import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import manage

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in ('compose.yaml','.env'):(self.root/name).write_text('test')
        for key,value in [('ROOT',self.root),('BACKUPS',self.root/'backups')]:
            p=patch.object(manage,key,value);p.start();self.addCleanup(p.stop)
        self.commands=[]
    def fake(self,args,capture=False):
        self.commands.append(args)
        if args[:3]==['docker','inspect','cid']:
            return json.dumps([{'Image':'sha256:old','Mounts':[{'Destination':'/data','Type':'volume','Name':'bnb-monitor_monitor-data'}]}])
        if args[:3]==['docker','compose','ps']:
            return 'web\nworker\n' if '--services' in args else 'cid\n'
        if args[:3]==['git','rev-parse','HEAD']:return 'abc\n'
        if args[:2]==['docker','run']:
            mount=next(x for x in args if x.startswith('type=bind,src='))
            folder=Path(mount.split('src=')[1].split(',dst=')[0]);(folder/'data.tar.gz').write_bytes(b'archive')
        return ''
    def test_backup_stops_writers_and_saves_image(self):
        with patch.object(manage,'run',side_effect=self.fake):folder=manage.backup()
        self.assertTrue((folder/'manifest.json').exists())
        self.assertEqual((folder/'.env').stat().st_mode&0o777,0o600)
        stop=self.commands.index(['docker','compose','stop','web','worker'])
        tar=next(i for i,c in enumerate(self.commands) if c[:2]==['docker','run'])
        restart=self.commands.index(['docker','compose','start','web','worker'])
        self.assertLess(stop,tar);self.assertLess(tar,restart)
        self.assertIn('--network',self.commands[tar])
    def test_failed_backup_restarts_services(self):
        def fail(args,capture=False):
            if args[:2]==['docker','run']:raise RuntimeError('disk full')
            return self.fake(args,capture)
        with patch.object(manage,'run',side_effect=fail):
            with self.assertRaises(RuntimeError):manage.backup()
        self.assertIn(['docker','compose','start','web','worker'],self.commands)
    def test_update_never_pulls_without_backup(self):
        with patch.object(manage,'run',return_value='') as run,patch.object(manage,'backup',side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError):manage.update()
            self.assertFalse(any('pull' in call.args[0] for call in run.call_args_list))
    def test_failed_update_rolls_back(self):
        with patch.object(manage,'run',return_value=''),patch.object(manage,'backup',return_value=self.root/'backups'/'one'),patch.object(manage,'compose',side_effect=RuntimeError('build error')),patch.object(manage,'rollback') as rollback:
            with self.assertRaises(RuntimeError):manage.update()
            rollback.assert_called_once_with(self.root/'backups'/'one')
    def test_rollback_keeps_live_database(self):
        with patch.object(manage,'run',side_effect=self.fake):folder=manage.backup()
        self.commands=[]
        with patch.object(manage,'run',side_effect=self.fake),patch.object(manage,'wait_healthy'):
            manage.rollback(folder)
        self.assertFalse(any(c[:2]==['docker','run'] for c in self.commands))
        self.assertTrue(any('up' in c and '--no-build' in c for c in self.commands))

    def test_git_uses_invoking_user_with_sudo(self):
        from types import SimpleNamespace
        with patch('manage.os.geteuid',return_value=0),patch.dict('manage.os.environ',{'SUDO_USER':'vmuser'}),patch('manage.pwd.getpwnam',return_value=SimpleNamespace(pw_uid=1000,pw_gid=1000,pw_name='vmuser',pw_dir='/home/vmuser')),patch('manage.os.getgrouplist',return_value=[1000]),patch('manage.subprocess.run',return_value=SimpleNamespace(stdout='')) as command:
            manage.run(['git','pull','--ff-only'])
            self.assertEqual(command.call_args.kwargs['user'],1000)
            self.assertEqual(command.call_args.kwargs['env']['HOME'],'/home/vmuser')
            manage.run(['docker','compose','ps'])
            self.assertNotIn('user',command.call_args.kwargs)
