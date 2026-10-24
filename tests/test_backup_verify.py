import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backup_crypto import transform
from backup_verify import verify
import manage

class RestoreVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.folder=self.root/'backups'/'one';self.folder.mkdir(parents=True)
        self.key=self.root/'backup-encryption.key';self.key.write_bytes(b'k'*32);self.key.chmod(0o600)
        self.data=self.root/'source';self.data.mkdir()
        with sqlite3.connect(self.data/'monitor.db') as c:
            for name in ('wallets','events','settings','status'):c.execute('CREATE TABLE '+name+' (id INTEGER)')
            c.execute('INSERT INTO wallets VALUES (1)')
        self.work=self.root/'verification';self.work.mkdir()
        self.prepare()
    def prepare(self,unsafe=False):
        raw=self.root/'data.tar.gz'
        with tarfile.open(raw,'w:gz') as a:
            a.add(self.data,arcname='data')
            if unsafe:
                entry=tarfile.TarInfo('../escaped');entry.size=4;a.addfile(entry,io.BytesIO(b'test'))
        env=self.root/'.env';env.write_text('SECRET_KEY=not-a-real-secret')
        for file in (raw,env):
            target=self.folder/(file.name+'.enc');target.unlink(missing_ok=True);transform(file,target,self.key)
        self.manifest={'created':1,'image':'sha256:test','encrypted':True,
                       'sha256':manage.checksum(self.folder/'data.tar.gz.enc'),
                       'env_sha256':manage.checksum(self.folder/'.env.enc')}
        (self.folder/'manifest.json').write_text(json.dumps(self.manifest))
    def test_actual_restore_and_plaintext_cleanup(self):
        before=(self.data/'monitor.db').read_bytes()
        result=verify(self.folder,self.key,self.work)
        self.assertEqual(result['integrity'],'ok');self.assertEqual(result['wallets'],1)
        self.assertEqual(list(self.work.iterdir()),[])
        self.assertEqual((self.data/'monitor.db').read_bytes(),before)
    def test_wrong_key_removes_plaintext(self):
        self.key.write_bytes(b'x'*32)
        with self.assertRaises(ValueError):verify(self.folder,self.key,self.work)
        self.assertEqual(list(self.work.iterdir()),[])
    def test_archive_path_escape_rejected(self):
        self.prepare(unsafe=True)
        with self.assertRaises(ValueError):verify(self.folder,self.key,self.work)
        self.assertFalse((self.work/'escaped').exists());self.assertEqual(list(self.work.iterdir()),[])
    def test_bad_database_rejected(self):
        (self.data/'monitor.db').write_bytes(b'not SQLite');self.prepare()
        with self.assertRaises(sqlite3.DatabaseError):verify(self.folder,self.key,self.work)
        self.assertEqual(list(self.work.iterdir()),[])
    def test_cli_verifier_cannot_mount_live_volume(self):
        calls=[]
        def run(args,capture=False):
            calls.append(args)
            return json.dumps({'integrity':'ok','wallets':1,'files':2})
        with patch.object(manage,'ROOT',self.root),patch.object(manage,'BACKUPS',self.root/'backups'),patch.object(manage,'run',side_effect=run),patch.object(manage,'publish_backup_report'):
            report=manage.verify_backup()
        self.assertEqual(report['verification']['integrity'],'ok')
        command=calls[0];self.assertIn('none',command);self.assertIn('--read-only',command)
        self.assertFalse(any('type=volume' in a for a in command))
        self.assertTrue(all(a.endswith(',readonly') for a in command if a.startswith('type=bind,')))
    def test_changed_checksum_stops_before_container(self):
        (self.folder/'.env.enc').write_bytes(b'bad')
        with patch.object(manage,'ROOT',self.root),patch.object(manage,'BACKUPS',self.root/'backups'),patch.object(manage,'run') as run,patch.object(manage,'publish_backup_report'):
            with self.assertRaises(RuntimeError):manage.verify_backup()
            run.assert_not_called()
