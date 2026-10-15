"""Generate BIP39 wallets locally; fill empty identity rows once, then verify."""
import fcntl
import json
import os
import re
import time
from functools import wraps
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from eth_account import Account

PATH = "m/44'/60'/0'/0/0"
MAX_BATCH = 100

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS generated_batches (
        id TEXT PRIMARY KEY, created REAL NOT NULL, state TEXT NOT NULL,
        count INTEGER NOT NULL, data TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS generator_active ON generated_batches((1)) WHERE state IN ('prepared','sending','uncertain')")

def request_google(cfg, token, suffix, payload=None):
    from app import RemoteError
    url = 'https://sheets.googleapis.com/v4/spreadsheets/' + cfg['sheets_id'] + suffix
    req = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                  headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with build_opener(NoRedirect()).open(req, timeout=25) as response:
            raw=response.read(2000001)
        if len(raw)>2000000:
            raise ValueError()
        return json.loads(raw)
    except Exception:
        # Never expose request bodies, credentials or library exceptions containing seeds.
        raise RemoteError('Не удалось получить подтверждение Google. Проверьте интернет и доступ «Редактор».') from None

def read_range(cfg,token,cell_range):
    tab=cfg['sheets_tab'].replace("'","''")
    return request_google(cfg,token,'/values/'+quote(f"'{tab}'!{cell_range}",safe='')+'?valueRenderOption=FORMULA').get('values',[])

def public(row):
    d=json.loads(row['data'])
    return {'id':row['id'],'created':row['created'],'state':row['state'],'count':row['count'],
            'tab':d['sheets_tab'],'addresses':d.get('addresses',[]),'error':row['error']}

def row_for(ident):
    from app import db
    with db() as c:
        r=c.execute('SELECT * FROM generated_batches WHERE id=?',(ident,)).fetchone()
    if not r:
        raise ValueError('Партия не найдена.')
    return r

def stage_path(ident):
    from app import DATA
    return DATA / ('generated-'+ident+'.json')

def save_seeds(ident, records):
    p=stage_path(ident)
    tmp=p.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(records,f)
        f.flush();os.fsync(f.fileno())
    os.replace(tmp,p)
    fd=os.open(p.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def create_records(count):
    from app import RemoteError
    try:
        Account.enable_unaudited_hdwallet_features()
        records=[]
        for _ in range(count):
            account, mnemonic=Account.create_with_mnemonic(num_words=12,account_path=PATH)
            if Account.from_mnemonic(mnemonic,account_path=PATH).address != account.address:
                raise ValueError()
            records.append({'address':account.address,'seed':mnemonic})
        if len({r['address'] for r in records}) != count:
            raise ValueError()
        return records
    except Exception:
        raise RemoteError('Не удалось создать кошельки. Запись в Google не выполнялась.') from None

def empty_identity(row):
    # Formulas, whitespace and partial identities count as occupied.
    return all(value == '' or value is None for value in row)

def vacant_rows(cfg, token, count):
    limit=min(cfg['row_count'],4999)
    identities=read_range(cfg,token,f'A1:B{limit}')
    candidates=[i for i in range(2,limit+1)
                if empty_identity(identities[i-1] if i<=len(identities) else [])]
    selected=[]
    tab=cfg['sheets_tab'].replace("'","''")
    # Read C only in candidate empty rows, never dump existing wallet seeds.
    for offset in range(0,len(candidates),20):
        group=candidates[offset:offset+20]
        suffix='/values:batchGet?'+ '&'.join('ranges='+quote(f"'{tab}'!C{i}",safe='') for i in group)+'&valueRenderOption=FORMULA'
        values=request_google(cfg,token,suffix).get('valueRanges',[])
        if len(values)!=len(group):raise ValueError('Не удалось проверить свободные строки.')
        for row,result in zip(group,values):
            if all(empty_identity(v) for v in result.get('values',[])):
                selected.append(row)
                if len(selected)==count:return selected
    # Grow the grid only if its existing rows contain no remaining empty slots.
    extra=count-len(selected)
    if cfg['row_count']+extra>4999:raise ValueError('Нет свободных строк в пределах 4999 строк листа.')
    return selected+list(range(cfg['row_count']+1,cfg['row_count']+extra+1))

def append_payload(sheet_id,records,rows,row_count):
    if len(records)!=len(rows) or len(set(rows))!=len(rows):
        raise ValueError('Неверные строки назначения.')
    requests=[]
    if max(rows)>row_count:
        requests.append({'appendDimension':{'sheetId':sheet_id,'dimension':'ROWS','length':max(rows)-row_count}})
    for record,row in zip(records,rows):
        requests.append({'updateCells':{
            'range':{'sheetId':sheet_id,'startRowIndex':row-1,'endRowIndex':row,
                     'startColumnIndex':1,'endColumnIndex':3},
            'fields':'userEnteredValue','rows':[{'values':[
                {'userEnteredValue':{'stringValue':record['address']}},
                {'userEnteredValue':{'stringValue':record['seed']}}]}]}})
    return {'requests':requests}

def prepare_cfg(count):
    from app import settings,db
    from sheets_sync import google_credentials
    cfg=settings()
    if not cfg.get('sheets_enabled') or not cfg.get('sheets_id') or not cfg.get('sheets_tab'):
        raise ValueError('Сначала включите Google Таблицы в настройках.')
    cfg={k:cfg[k] for k in ('sheets_id','sheets_tab')}
    token=google_credentials(write=True).token
    meta=request_google(cfg,token,'?fields=sheets.properties')
    matches=[s['properties'] for s in meta.get('sheets',[]) if s['properties']['title']==cfg['sheets_tab']]
    if len(matches)!=1 or matches[0].get('sheetType','GRID')!='GRID':
        raise ValueError('Лист Google не найден или не является обычной таблицей.')
    cfg['sheet_id']=matches[0]['sheetId']
    cfg['row_count']=matches[0]['gridProperties']['rowCount']
    header=read_range(cfg,token,'A1:C1')
    h=header[0] if header else []
    if len(h)<3 or 'адрес' not in str(h[1]).lower() and 'address' not in str(h[1]).lower():
        raise ValueError('В первой строке столбца B должен быть заголовок «адрес».')
    if not any(word in str(h[2]).lower() for word in ('сид','seed','mnemonic')):
        raise ValueError('В первой строке столбца C должен быть заголовок сид-фразы.')
    existing=read_range(cfg,token,'B1:B5000')
    addresses={str(r[0]).strip().lower() for r in existing if r and re.fullmatch(r'0x[0-9a-fA-F]{40}',str(r[0]).strip())}
    with db() as c:
        addresses.update(r['address'].lower() for r in c.execute('SELECT address FROM wallets'))
    if len(addresses)+count>500:
        raise ValueError('Превышен предел: 500 кошельков в проекте или 4999 строк листа.')
    cfg['target_rows']=vacant_rows(cfg,token,count)
    return cfg,token

def generate(ident,count):
    from app import db,RemoteError
    if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):
        raise ValueError('Неверный идентификатор запроса.')
    if type(count) is not int or not 1<=count<=MAX_BATCH:
        raise ValueError('Введите целое количество от 1 до 100.')
    with db() as c:
        old=c.execute('SELECT * FROM generated_batches WHERE id=?',(ident,)).fetchone()
        if old:
            if old['count']!=count:raise ValueError('Этот запрос уже использован для другого количества.')
            return public(old)
        if c.execute("SELECT 1 FROM generated_batches WHERE state IN ('prepared','sending','uncertain')").fetchone():
            raise ValueError('Сначала проверьте результат предыдущей партии.')
    cfg,token=prepare_cfg(count)
    records=create_records(count)
    cfg['addresses']=[r['address'] for r in records]
    save_seeds(ident,records)
    with db() as c:
        c.execute('INSERT INTO generated_batches(id,created,state,count,data) VALUES (?,?,?,?,?)',
                  (ident,time.time(),'prepared',count,json.dumps(cfg)))
    # Persist dispatch intent BEFORE the single append. No automatic re-append.
    with db() as c:c.execute("UPDATE generated_batches SET state='sending' WHERE id=?",(ident,))
    try:
        request_google(cfg,token,':batchUpdate',append_payload(cfg['sheet_id'],records,cfg['target_rows'],cfg['row_count']))
    except RemoteError:
        with db() as c:c.execute("UPDATE generated_batches SET state='uncertain',error=? WHERE id=?",
            ('Ответ Google не подтверждён. Нажмите «Проверить запись»; повторной генерации не будет.',ident))
        return public(row_for(ident))
    return verify(ident,token)

def verify(ident,token=None):
    from app import db,set_status,RemoteError
    from sheets_sync import google_credentials
    r=row_for(ident)
    if r['state']=='done':
        stage_path(ident).unlink(missing_ok=True)
        return public(r)
    cfg=json.loads(r['data'])
    try:
        if token is None:token=google_credentials(write=True).token
        records=json.loads(stage_path(ident).read_text())
        # Resolve the stable sheet ID in case its title changed after dispatch.
        meta=request_google(cfg,token,'?fields=sheets.properties')
        tabs=[s['properties']['title'] for s in meta['sheets'] if s['properties']['sheetId']==cfg['sheet_id']]
        if len(tabs)!=1:raise ValueError()
        cfg['sheets_tab']=tabs[0]
        rows=read_range(cfg,token,'B1:B5000')
        indices=[]
        for record in records:
            found=[i+1 for i,row in enumerate(rows) if row and str(row[0]).lower()==record['address'].lower()]
            if len(found)!=1:raise ValueError()
            indices.append(found[0])
        tab=cfg['sheets_tab'].replace("'","''")
        values=[]
        for offset in range(0,len(indices),20):
            suffix='/values:batchGet?'+ '&'.join('ranges='+quote(f"'{tab}'!B{i}:C{i}",safe='') for i in indices[offset:offset+20])+'&valueRenderOption=UNFORMATTED_VALUE'
            values.extend(request_google(cfg,token,suffix)['valueRanges'])
        if len(values)!=len(records):raise ValueError()
        for value,record in zip(values,records):
            pair=value.get('values',[])
            if len(pair)!=1 or len(pair[0])!=2 or str(pair[0][0]).lower()!=record['address'].lower() or pair[0][1]!=record['seed']:
                raise ValueError()
        with db() as c:c.execute("UPDATE generated_batches SET state='done',error='' WHERE id=?",(ident,))
    except Exception:
        with db() as c:c.execute("UPDATE generated_batches SET state='uncertain',error=? WHERE id=?",
            ('Запись пока не подтверждена. Сид-фразы сохранены на VM. Повторите проверку; не создавайте дубликат партии.',ident))
        return public(row_for(ident))
    # A cleanup failure must never turn a confirmed append back into an uncertain one.
    try:
        stage_path(ident).unlink(missing_ok=True)
    except OSError:
        pass
    set_status(sheets_requested=True)
    return public(row_for(ident))

def install(app):
    from app import db,DATA,settings
    from flask import request
    def locked(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            with (DATA/'wallet-generator.lock').open('a') as f:
                try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('Генератор уже работает. Дождитесь результата.') from None
                return fn(*args,**kwargs)
        return wrapped
    @app.get('/api/generator')
    def generator_list():
        with db() as c:rows=c.execute('SELECT * FROM generated_batches ORDER BY created DESC LIMIT 30').fetchall()
        cfg=settings()
        return {'items':[public(r) for r in rows],'tab':cfg.get('sheets_tab',''),'enabled':bool(cfg.get('sheets_enabled'))}
    @app.post('/api/generator')
    @locked
    def generator_create():
        d=request.get_json() or {}
        return generate(d.get('id'),d.get('count'))
    @app.post('/api/generator/<ident>/check')
    @locked
    def generator_check(ident):
        if not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Неверный идентификатор.')
        return verify(ident)
