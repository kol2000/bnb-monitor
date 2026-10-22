#!/usr/bin/env python3
"""VM-local backup, update and code rollback. Requires Docker and git."""
import argparse
import datetime
import hashlib
import json
import os
import pwd
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent
BACKUPS = ROOT / 'backups'


def run(args, capture=False):
    options = {}
    # Docker needs root; git must retain the invoking user's ownership and credentials.
    if args[0] == 'git' and os.geteuid() == 0 and os.environ.get('SUDO_USER') not in (None, 'root'):
        user = pwd.getpwnam(os.environ['SUDO_USER'])
        options = dict(user=user.pw_uid, group=user.pw_gid,
                       extra_groups=os.getgrouplist(user.pw_name, user.pw_gid),
                       env=dict(os.environ, HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name))
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, **options).stdout


def compose(*args):
    return run(['docker', 'compose', *args])


def inspect_container(ident):
    return json.loads(run(['docker', 'inspect', ident], True))[0]


def wait_healthy(files=()):
    prefix = ['docker', 'compose', *files]
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        healthy = True
        for service in ('web', 'worker'):
            ident = run([*prefix, 'ps', '-q', service], True).strip()
            if not ident:
                healthy = False
                break
            state = inspect_container(ident)['State']
            if not state.get('Running') or state.get('Health', {}).get('Status') != 'healthy':
                healthy = False
        if healthy:
            return
        time.sleep(3)
    raise RuntimeError('Контейнеры не прошли проверку здоровья за 4 минуты.')


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def backup():
    os.umask(0o077)
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    name = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+os.urandom(3).hex()
    folder = BACKUPS / name
    folder.mkdir(mode=0o700)
    running = run(['docker','compose','ps','--services','--status','running'], True).split()
    ids = run(['docker','compose','ps','-aq','web'], True).split()
    if len(ids) != 1:
        raise RuntimeError('Нужен существующий контейнер web для резервной копии.')
    container = inspect_container(ids[0])
    volumes = [m for m in container['Mounts'] if m['Destination']=='/data' and m['Type']=='volume']
    if len(volumes)!=1:
        raise RuntimeError('Не найден единственный Docker volume /data. Копия не создана.')
    volume = volumes[0]['Name']
    image = container['Image']
    tag = 'bnb-monitor-backup:'+name.lower()
    run(['docker','tag',image,tag])
    for file in ('compose.yaml','.env'):
        shutil.copy2(ROOT/file, folder/file)
        (folder/file).chmod(0o600)
    (folder/'image.json').write_text(json.dumps({'services':{
        s:{'image':tag,'pull_policy':'never'} for s in ('web','worker')}}))
    key = ROOT / 'backup-encryption.key'
    if not key.exists():
        with key.open('xb') as stream:
            stream.write(os.urandom(32))
        key.chmod(0o600)
    if key.is_symlink() or len(key.read_bytes()) != 32 or key.stat().st_mode & 0o077:
        raise RuntimeError('Проверьте backup-encryption.key: 32 байта, права 600.')
    stopped = False
    try:
        stopped = True
        compose('stop','web','worker')
        run(['docker','run','--rm','--user','0','--network','none','--entrypoint','python',
             '--mount',f'type=volume,src={volume},dst=/source,readonly',
             '--mount',f'type=bind,src={folder},dst=/backup',image,'-c',
             "import os,tarfile; os.umask(0o077); "
             "t=tarfile.open('/backup/data.tar.gz','w:gz'); t.add('/source',arcname='data'); t.close()"])
        run(['docker','run','--rm','--user','0','--network','none','--entrypoint','python',
             '--mount',f'type=bind,src={key},dst=/backup-encryption.key,readonly',
             '--mount',f'type=bind,src={ROOT / "backup_crypto.py"},dst=/backup_crypto.py,readonly',
             '--mount',f'type=bind,src={folder},dst=/backup', image,
             '/backup_crypto.py','encrypt','/backup-encryption.key','/backup'])
        digest = checksum(folder/'data.tar.gz.enc')
        manifest = {'created':time.time(),'commit':run(['git','rev-parse','HEAD'],True).strip(),
                    'image':image,'tag':tag,'volume':volume,'sha256':digest,
                    'encrypted':True, 'env_sha256':checksum(folder/'.env.enc')}
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2))
        (folder/'manifest.json').chmod(0o600)
    finally:
        if stopped and running:
            compose('start',*running)
    print('Зашифрованная резервная копия:',folder)
    print('Сохраните backup-encryption.key отдельно: без него восстановить данные нельзя.')
    return folder


def rollback(folder):
    folder = Path(folder).resolve()
    # Only backups made by this installation; never restore financial DB automatically.
    if folder.parent != BACKUPS.resolve():
        raise RuntimeError('Укажите каталог резервной копии внутри backups/.')
    manifest = json.loads((folder/'manifest.json').read_text())
    if checksum(folder/('data.tar.gz.enc' if manifest.get('encrypted') else 'data.tar.gz'))!=manifest['sha256']:
        raise RuntimeError('Контрольная сумма резервной копии не совпала.')
    run(['docker','image','inspect',manifest['tag']],True)
    files = ('--env-file',str(ROOT/'.env'),'-f',str(folder/'compose.yaml'),'-f',str(folder/'image.json'))
    run(['docker','compose',*files,'up','-d','--no-build'])
    wait_healthy(files)
    print('Запущен прежний образ. Актуальная база и журналы операций сохранены.')


def update():
    dirty = run(['git','status','--porcelain','--untracked-files=no'],True).strip()
    if dirty:
        raise RuntimeError('Есть локальные изменения отслеживаемых файлов. Сохраните их перед обновлением.')
    saved = backup()
    try:
        run(['git','pull','--ff-only'])
        compose('build','web')
        compose('up','-d','--no-build')
        wait_healthy()
    except Exception:
        print('Обновление не завершено. Возвращаем прежний образ…')
        rollback(saved)
        raise
    print('Обновление завершено; web и worker healthy.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','update','rollback'])
    parser.add_argument('folder',nargs='?')
    args=parser.parse_args()
    os.umask(0o077)
    try:
        if args.action=='rollback':
            if not args.folder: parser.error('Для rollback укажите backups/имя-копии')
            rollback(args.folder)
        elif args.action=='backup': backup()
        else: update()
    except Exception as exc:
        # No command contents or environment dumps containing credentials.
        print('Не удалось завершить операцию:',str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__)
        raise SystemExit(1)

if __name__=='__main__': main()
