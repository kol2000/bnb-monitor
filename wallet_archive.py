"""Manual inactivity scan and single-dispatch, atomic Google row archival.

No mnemonic is sent to the browser or the explorer. Google performs row copies
server-side. A transaction marker in the same batch resolves uncertain responses.
"""
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode, quote
from urllib.error import HTTPError
from urllib.request import Request, build_opener
from wallet_generator import request_google, NoRedirect
from sheets_guard import sheet_guard

ARCHIVE_TAB = 'bnb кошельки (архив)'
ACTIVE_TAB = 'bnb кошельки'
DAYS = 30
ACTIONS = ('txlist', 'txlistinternal', 'tokentx', 'tokennfttx', 'token1155tx')
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
MARKER = 'bnb-monitor-archive'


def schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS archive_jobs (
        id TEXT PRIMARY KEY, created REAL NOT NULL, state TEXT NOT NULL,
        data TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS archive_one_active ON archive_jobs((1)) WHERE state IN ('scanning','preview','queued','validating','moving','uncertain')")


def key_path():
    from app import DATA
    return DATA/'archive-history.json'


def history_key():
    from app import RemoteError
    try:
        value=json.loads(key_path().read_text())['api_key']
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',value):raise ValueError()
        return value
    except Exception:
        raise RemoteError('Для истории BSC сохраните ключ Etherscan API V2 с доступом к chainid 56.') from None


def save_key(value, clear=False):
    if clear:
        key_path().unlink(missing_ok=True)
    elif value:
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',value.strip()):
            raise ValueError('Неверный формат ключа Etherscan.')
        path=key_path();tmp=path.with_suffix('.'+secrets.token_hex(8)+'.tmp')
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w') as f:
                json.dump({'api_key':value.strip()},f);f.flush();os.fsync(f.fileno())
            os.replace(tmp,path)
        finally:
            tmp.unlink(missing_ok=True)


def explorer_request(address, action, key):
    from app import RemoteError
    params=dict(chainid=56,module='account',action=action,address=address,
                startblock=0,endblock=99999999999,page=1,offset=1,sort='desc',apikey=key)
    req=Request('https://api.etherscan.io/v2/api?'+urlencode(params),headers={'Accept':'application/json'})
    try:
        with build_opener(NoRedirect()).open(req,timeout=20) as response:raw=response.read(1000001)
        if len(raw)>1000000:raise ValueError()
        return json.loads(raw)
    except Exception:
        raise RemoteError('История BSC недоступна. Адрес не признан неактивным.') from None


class BatchRejected(Exception):
    pass


def dispatch_batch(cfg, token, payload):
    req=Request('https://sheets.googleapis.com/v4/spreadsheets/'+cfg['sheets_id']+':batchUpdate',
                data=json.dumps(payload).encode(),
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with build_opener(NoRedirect()).open(req,timeout=25) as response:
            response.read(1000001)
    except HTTPError as exc:
        # Google rejects these requests before applying the atomic batch. Never expose body.
        if exc.code in (400,401,403,404):
            raise BatchRejected('Google отклонил перенос. Проверьте доступ «Редактор», заголовки и защищённые строки.') from None
        raise RuntimeError('Результат Google не подтверждён.') from None
    except Exception:
        raise RuntimeError('Результат Google не подтверждён.') from None


def parse_history(data, address):
    from app import RemoteError
    if not isinstance(data,dict):raise RemoteError('Некорректный ответ истории BSC.')
    rows=data.get('result');status=data.get('status');message=str(data.get('message','')).lower()
    if status=='0' and rows==[] and message in ('no transactions found','no records found'):
        return None
    if status!='1' or message!='ok' or not isinstance(rows,list) or len(rows)>1:
        raise RemoteError('Etherscan не подтвердил историю. Проверьте ключ, доступ к BSC, тариф и лимиты.')
    if not rows:return None
    row=rows[0]
    try:
        stamp=int(row['timeStamp'])
        if stamp<=0 or stamp>time.time()+120 or not re.fullmatch(r'0x[0-9a-fA-F]{64}',row['hash']):raise ValueError()
        if address.lower() not in [str(row.get(k,'')).lower() for k in ('from','to','contractAddress')]:raise ValueError()
    except Exception:
        raise RemoteError('Не удалось подтвердить дату и адрес последней операции BSC.') from None
    return stamp


def last_activity(address, key):
    latest=None
    for action in ACTIONS:
        stamp=parse_history(explorer_request(address,action,key),address)
        if stamp is not None:latest=max(latest or 0,stamp)
        time.sleep(0.4)  # Sequential requests; errors never mean inactivity.
    return latest


def load(ident):
    from app import db
    with db() as c:r=c.execute('SELECT * FROM archive_jobs WHERE id=?',(ident,)).fetchone()
    if not r:raise ValueError('Проверка архива не найдена.')
    return dict(r)|{'data':json.loads(r['data'])}


def save(j, state=None, error=''):
    from app import db
    previous=j['state'];desired=state or previous
    j['data']['updated']=time.time()
    with db() as c:
        if not c.execute('UPDATE archive_jobs SET state=?,data=?,error=? WHERE id=? AND state=?',
                         (desired,json.dumps(j['data']),error,j['id'],previous)).rowcount:
            raise ValueError('Состояние проверки изменилось.')
    j['state']=desired


def assert_not_archiving(c, address):
    if not address:return
    for row in c.execute("SELECT data FROM archive_jobs WHERE state IN ('moving','uncertain')"):
        if address.lower() in json.loads(row['data']).get('selected',[]):
            raise ValueError('Кошелёк переносится в архив. Сначала проверьте результат переноса.')


def public(j):
    d=j['data']
    return {'id':j['id'],'state':j['state'],'created':j['created'],'error':j['error'],
            'archive_tab':ARCHIVE_TAB,'cutoff':d['cutoff'],'total':len(d['wallets']),
            'results':d.get('results',[]),'progress':d.get('progress',0),
            'updated':d.get('updated'),'selected':d.get('selected',[])}


def cfg_current():
    from app import settings
    cfg=settings()
    if not cfg['sheets_enabled'] or cfg['sheets_tab']!=ACTIVE_TAB:
        raise ValueError('Для архивации включите Google и выберите рабочий лист «bnb кошельки».')
    return {k:cfg[k] for k in ('sheets_id','sheets_tab')}


def read_values(cfg,token,title,cell_range):
    name=title.replace("'","''")
    r=request_google(cfg,token,'/values/'+quote(f"'{name}'!{cell_range}",safe='')+'?valueRenderOption=FORMULA')
    if not isinstance(r,dict) or not r.get('range') or not isinstance(r.get('values',[]),list):
        raise ValueError('Не удалось прочитать строки Google.')
    return r.get('values',[])


def properties(cfg,token):
    data=request_google(cfg,token,'?fields=sheets.properties,developerMetadata')
    props={p['properties']['title']:p['properties'] for p in data.get('sheets',[])}
    for title in (ACTIVE_TAB,ARCHIVE_TAB):
        if title not in props or props[title].get('sheetType','GRID')!='GRID':
            raise ValueError('Нужны обычные листы «bnb кошельки» и «bnb кошельки (архив)».')
    return props,data.get('developerMetadata',[])


def identities(rows):
    if not rows or len(rows[0])<2 or str(rows[0][1]).strip().casefold() not in ('адрес','address','bsc-адрес','wallet address'):
        raise ValueError('В первой строке рабочего листа нужен заголовок «адрес» в столбце B.')
    if len(rows)>=5000:raise ValueError('Слишком много строк: предел чтения — 4999.')
    wallets=[];seen=set()
    for index,row in enumerate(rows[1:],2):
        if not isinstance(row,list):raise ValueError('Некорректная строка Google.')
        address=str(row[1]).strip().lower() if len(row)>1 else ''
        if not address:
            if row and str(row[0]).strip():raise ValueError('В рабочем списке есть имя без адреса.')
            continue
        if not ADDRESS.fullmatch(address) or address in seen:
            raise ValueError('Исправьте неверные или повторяющиеся адреса рабочего списка.')
        seen.add(address);wallets.append({'row':index,'address':address,'name':str(row[0])[:80] or address[:8]})
    if len(wallets)>500:raise ValueError('За один проход поддерживается до 500 адресов.')
    return wallets


def start():
    from app import db
    from sheets_sync import google_credentials
    history_key()  # No scan without a configured history source.
    cfg=cfg_current()
    with sheet_guard():
        token=google_credentials().token
        properties(cfg,token)
        wallets=identities(read_values(cfg,token,ACTIVE_TAB,'A1:B5000'))
    ident=secrets.token_hex(16);now=time.time()
    data={'source':cfg,'wallets':wallets,'results':[],'cutoff':now-DAYS*86400,'updated':now}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM archive_jobs WHERE state IN ('scanning','preview','queued','validating','moving','uncertain')").fetchone():
            raise ValueError('Сначала завершите или отмените текущую проверку архива.')
        c.execute('INSERT INTO archive_jobs VALUES (?,?,?,?,?)',(ident,now,'scanning',json.dumps(data),''))
    return public(load(ident))


def pending(c, address):
    for row in c.execute("SELECT plan FROM withdrawals WHERE state IN ('pending1','pending2','blocked')"):
        plan=json.loads(row['plan'])
        if address in (str(plan.get('source','')).lower(),str(plan.get('middle','')).lower()):
            return True
    # Also cover a pending intent whose plan is incomplete but wallet identity is present.
    if c.execute("SELECT 1 FROM withdrawals w JOIN wallets a ON a.id=w.wallet_id WHERE lower(a.address)=? AND w.state IN ('pending1','pending2','blocked')",(address,)).fetchone():
        return True
    for row in c.execute("SELECT data FROM mexc_jobs WHERE state IN ('buying','bought','withdrawing')"):
        if str(json.loads(row['data']).get('address','')).lower()==address:return True
    return False


def scan_one(j):
    from app import db
    if cfg_current()!=j['data']['source']:raise ValueError('Настройки Google изменились; запустите новую проверку.')
    d=j['data'];i=len(d['results'])
    if i>=len(d['wallets']):save(j,'preview');return
    w=d['wallets'][i];r=dict(w,checked=time.time(),last_at=None,state='error',bnb=None,usdt=None)
    try:
        stamp=last_activity(w['address'],history_key());r['last_at']=stamp
        with db() as c:
            busy=pending(c,w['address'])
            balance=c.execute('SELECT balance,usdt_balance,checked,usdt_checked FROM wallets WHERE address=?',(w['address'],)).fetchone()
        if balance:
            from app import bnb
            r.update(bnb=bnb(balance['balance']) if balance['balance'] is not None else None,
                     usdt=bnb(balance['usdt_balance']) if balance['usdt_balance'] is not None else None,
                     balance_checked=balance['checked'],usdt_checked=balance['usdt_checked'])
        r['state']='busy' if busy else 'no_history' if stamp is None else 'candidate' if stamp<d['cutoff'] else 'active'
    except Exception:
        r['reason']='История не подтверждена. Проверьте доступ API, тариф BSC и лимиты.'
    d['results'].append(r)
    if len(d['results'])>=3 and all(v['state']=='error' for v in d['results'][-3:]):
        save(j,'failed','Три ошибки истории подряд. Проверьте ключ Etherscan, тариф для BSC и лимиты; затем запустите проверку заново.')
        return
    save(j,'preview' if len(d['results'])==len(d['wallets']) else 'scanning')


def enqueue(ident, selected):
    from app import db
    if not isinstance(selected,list) or not selected or len(selected)>500 or any(not isinstance(a,str) for a in selected):
        raise ValueError('Выберите кошельки для архивации.')
    selected=[a.lower() for a in selected]
    if len(set(selected))!=len(selected):raise ValueError('Повторяющиеся адреса в выборе.')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM archive_jobs WHERE id=?',(ident,)).fetchone()
        if not r:raise ValueError('Проверка не найдена.')
        d=json.loads(r['data'])
        if r['state'] in ('queued','validating','moving','uncertain','done') and d.get('selected')==selected:
            return public(dict(r)|{'data':d})
        if r['state']!='preview' or time.time()-r['created']>86400:
            raise ValueError('Результаты устарели. Выполните новую проверку.')
        allowed={v['address'] for v in d['results'] if v['state']=='candidate'}
        if not set(selected)<=allowed:raise ValueError('В выборе есть кошелёк без подтверждённой неактивности.')
        d.update(selected=selected,progress=0,updated=time.time())
        c.execute("UPDATE archive_jobs SET state='queued',data=? WHERE id=?",(json.dumps(d),ident))
    return public(load(ident))


def column(n):
    out=''
    while n:n,m=divmod(n-1,26);out=chr(65+m)+out
    return out


def sheet_snapshot(cfg,token):
    props,_=properties(cfg,token)
    result={}
    for title in (ACTIVE_TAB,ARCHIVE_TAB):
        p=props[title];cols=p['gridProperties']['columnCount']
        if not 3<=cols<=702:raise ValueError('Поддерживается от 3 до 702 столбцов.')
        # Seed values stay transient on the VM; no row contents enter jobs or responses.
        rows=read_values(cfg,token,title,f'A1:{column(cols)}5000')
        if len(rows)>=5000:raise ValueError('Предел чтения листа — 4999 заполненных строк.')
        result[title]={'props':p,'rows':rows}
    return result


def build_move(snap, selected, ident):
    src=snap[ACTIVE_TAB];dst=snap[ARCHIVE_TAB]
    wallets=identities(src['rows']);by_address={w['address']:w for w in wallets}
    if not set(selected)<=set(by_address):raise ValueError('Рабочий список изменился. Запустите проверку заново.')
    old={str(r[1]).strip().lower() for r in dst['rows'][1:] if len(r)>1}
    if set(selected)&old:raise ValueError('Некоторые выбранные адреса уже есть в архиве. Исправьте дубликаты.')
    sh=src['rows'][0] if src['rows'] else [];dh=dst['rows'][0] if dst['rows'] else []
    if len(sh)<3 or len(dh)<3:raise ValueError('На обоих листах нужна строка заголовков A–C.')
    norm=lambda x:' '.join(str(x).split()).casefold()
    for n in range(max(len(sh),len(dh))):
        a=sh[n] if n<len(sh) else '';b=dh[n] if n<len(dh) else ''
        if b and norm(a)!=norm(b):raise ValueError('Заголовки листов различаются. Расположите столбцы архива как в рабочем листе.')
    si=src['props']['sheetId'];di=dst['props']['sheetId'];cols=src['props']['gridProperties']['columnCount']
    count=len(selected);start=max(1,len(dst['rows']));requests=[]
    if start+count>4999:raise ValueError('Архив достигнет предела 4999 строк.')
    dc=dst['props']['gridProperties']['columnCount']
    if dc<cols:requests.append({'appendDimension':{'sheetId':di,'dimension':'COLUMNS','length':cols-dc}})
    for n,value in enumerate(sh):
        if value and (n>=len(dh) or not dh[n]):
            requests.append({'copyPaste':{'source':{'sheetId':si,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':n,'endColumnIndex':n+1},
                'destination':{'sheetId':di,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':n,'endColumnIndex':n+1},'pasteType':'PASTE_NORMAL'}})
    # Values API omits trailing empty rows: start is the first empty row after the list.
    if start+count>dst['props']['gridProperties']['rowCount']:
        requests.append({'appendDimension':{'sheetId':di,'dimension':'ROWS',
            'length':start+count-dst['props']['gridProperties']['rowCount']}})
    selected_rows=sorted(by_address[a]['row'] for a in selected)
    for offset,row in enumerate(selected_rows):
        requests.append({'copyPaste':{'source':{'sheetId':si,'startRowIndex':row-1,'endRowIndex':row,'startColumnIndex':0,'endColumnIndex':cols},
            'destination':{'sheetId':di,'startRowIndex':start+offset,'endRowIndex':start+offset+1,'startColumnIndex':0,'endColumnIndex':cols},'pasteType':'PASTE_NORMAL'}})
    for row in reversed(selected_rows):
        requests.append({'deleteDimension':{'range':{'sheetId':si,'dimension':'ROWS','startIndex':row-1,'endIndex':row}}})
    requests.append({'createDeveloperMetadata':{'developerMetadata':{
        'metadataKey':MARKER,'metadataValue':ident,'location':{'spreadsheet':True},'visibility':'DOCUMENT'}}})
    return {'requests':requests},[si,di]


def fingerprint(snap):
    # Used only during a single dispatch, never returned or persisted.
    return hashlib.sha256(json.dumps(snap,sort_keys=True).encode()).hexdigest()


def finish(j,token):
    from app import db
    cfg=j['data']['source']
    props,markers=properties(cfg,token)
    if not any(m.get('metadataKey')==MARKER and m.get('metadataValue')==j['id'] for m in markers):
        save(j,'uncertain','Google пока не подтвердил перенос. Повторная отправка отключена; проверьте результат позже.')
        return
    active=read_values(cfg,token,ACTIVE_TAB,'B1:B5000');arch=read_values(cfg,token,ARCHIVE_TAB,'B1:B5000')
    if len(active)>=5000 or len(arch)>=5000:raise ValueError('Не удалось проверить полный результат переноса.')
    aa=[str(r[0]).strip().lower() for r in active if r];ar=[str(r[0]).strip().lower() for r in arch if r]
    if any(a in aa or ar.count(a)!=1 for a in j['data']['selected']):
        save(j,'uncertain','Перенос принят Google, но строки изменились. Проверьте оба листа; повторной отправки не будет.')
        return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        for address in j['data']['selected']:
            if pending(c,address):raise ValueError('После переноса появился незавершённый перевод; проверьте его.')
        for address in j['data']['selected']:
            c.execute("UPDATE events SET delivery='cancelled' WHERE address=? AND delivery='pending'",(address,))
            c.execute("UPDATE email_outbox SET delivery='cancelled' WHERE delivery='pending' AND event_id IN (SELECT id FROM events WHERE address=?)",(address,))
            c.execute('DELETE FROM wallets WHERE address=?',(address,))
        j['data']['updated']=time.time()
        c.execute("UPDATE archive_jobs SET state='done',error='',data=? WHERE id=?",(json.dumps(j['data']),j['id']))
        c.execute("INSERT OR REPLACE INTO status VALUES ('sheets_requested','true')")


def move(j):
    from app import db,settings
    from sheets_sync import google_credentials
    cfg=j['data']['source']
    if cfg_current()!=cfg:raise ValueError('Настройки Google изменились. Перенос отменён.')
    with sheet_guard(archive=True):
        token=google_credentials(write=True).token
        with db() as c:
            if c.execute("SELECT 1 FROM generated_batches WHERE state IN ('prepared','sending','uncertain')").fetchone():
                raise ValueError('Сначала завершите проверку сгенерированной партии кошельков.')
        snap=sheet_snapshot(cfg,token)
        payload,sheet_ids=build_move(snap,j['data']['selected'],j['id'])
        if fingerprint(sheet_snapshot(cfg,token))!=fingerprint(snap):
            raise ValueError('Строки Google изменились во время подготовки. Перенос отменён.')
        # Persist intent before dispatch. A restart only verifies, never repeats this batch.
        j['data']['sheet_ids']=sheet_ids
        save(j,'moving')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            current={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
            if not current['sheets_enabled'] or any(current[k]!=cfg[k] for k in cfg):
                c.execute("UPDATE archive_jobs SET state='failed',error=? WHERE id=?",('Настройки Google изменились перед переносом.',j['id']))
                return
            if any(pending(c,a) for a in j['data']['selected']):
                c.execute("UPDATE archive_jobs SET state='failed',error=? WHERE id=?",('У выбранного кошелька есть незавершённый перевод.',j['id']))
                return
            # Intent writes for transfers and MEXC cannot race this final check.
            try:
                dispatch_batch(cfg,token,payload)
            except BatchRejected as exc:
                c.execute("UPDATE archive_jobs SET state='failed',error=? WHERE id=?",(str(exc),j['id']))
                return
            except Exception:
                pass  # An ambiguous response is resolved exclusively via the marker.
        finish(j,token)


def tick():
    from app import db
    from sheets_sync import google_credentials
    with db() as c:
        row=c.execute("SELECT id FROM archive_jobs WHERE state IN ('scanning','queued','validating','moving') LIMIT 1").fetchone()
    if not row:return
    j=load(row['id'])
    try:
        if j['state']=='scanning':scan_one(j)
        elif j['state'] in ('queued','validating'):
            if cfg_current()!=j['data']['source']:raise ValueError('Настройки Google изменились. Перенос отменён.')
            i=j['data'].get('progress',0);selected=j['data']['selected']
            if i<len(selected):
                stamp=last_activity(selected[i],history_key())
                if stamp is None or stamp>=time.time()-DAYS*86400:
                    raise ValueError('При повторной проверке обнаружена активность или неполная история. Запустите новый предпросмотр.')
                j['data']['progress']=i+1;save(j,'validating')
            else:move(j)
        else:
            with sheet_guard(archive=True):finish(j,google_credentials(write=True).token)
    except Exception as exc:
        current=load(j['id'])
        if current['state']=='cancelled':return
        if current['state'] in ('moving','uncertain'):
            save(current,'uncertain','Результат Google пока не подтверждён. Нажмите «Проверить результат». Перенос повторно не отправляется.')
        else:
            from app import RemoteError
            msg=str(exc) if isinstance(exc,(ValueError,RemoteError)) else 'Проверка прервана. Таблица не изменялась.'
            save(current,'failed',msg)


def run(stop):
    while not stop.is_set():
        try:tick()
        except Exception:pass
        stop.wait(1)


def install(app):
    from app import db
    from flask import request
    @app.get('/api/archive')
    def archive_list():
        with db() as c:rows=c.execute('SELECT id FROM archive_jobs ORDER BY created DESC LIMIT 10').fetchall()
        return {'items':[public(load(r['id'])) for r in rows], 'key_set':key_path().is_file(),'days':DAYS,'archive_tab':ARCHIVE_TAB}
    @app.post('/api/archive/key')
    def archive_key():
        d=request.get_json() or {};save_key(d.get('key',''),d.get('clear') is True)
        return {'key_set':key_path().is_file()}
    @app.post('/api/archive/scan')
    def archive_scan():return start()
    @app.post('/api/archive/<ident>/confirm')
    def archive_confirm(ident):return enqueue(ident,(request.get_json() or {}).get('addresses'))
    @app.post('/api/archive/<ident>/cancel')
    def archive_cancel(ident):
        with db() as c:
            if not c.execute("UPDATE archive_jobs SET state='cancelled' WHERE id=? AND state IN ('scanning','preview','queued','validating')",(ident,)).rowcount:
                raise ValueError('Этот этап нельзя отменить: дождитесь проверки результата.')
        return {'ok':True}
    @app.post('/api/archive/<ident>/check')
    def archive_check(ident):
        # Queue a read-only verification; no second Google batch can be dispatched.
        with db() as c:c.execute("UPDATE archive_jobs SET state='moving' WHERE id=? AND state='uncertain'",(ident,))
        return public(load(ident))
