"""One-shot authorized archive move, 2026-11-19. No transactions or key output."""
import hashlib
import json
import os
import time
import uuid
from urllib.parse import quote
from urllib.request import Request, urlopen
from app import DATA, db, settings, RemoteError
from sheets_sync import google_credentials
from archive_preview import verify_candidate

SOURCE = 'bnb кошельки'
DEST = 'bnb кошельки (архив)'
# Exact IDs AND addresses approved in the conversation; never a live status filter.
APPROVED_TEXT = '''
380 3ea84b3f864f99b2b71b18438d4eb6a271b5a315
381 77c02454562e0ff995af45bd4658b4257b71fb9b
382 575ed022a5af0fcb8f7c0e56875aa53b918fc89c
383 8b0a38bd7b4dff688f23db315e10a44801505d09
384 884092ed3f578cc932a5dd941c7c7b8675b771c2
385 9d43e9f7694c5c875b77f4d61666bd0641f694d5
387 abcaabad109e67f9a595f1a1ee7490576c5564ae
388 5c8d1a5aae34a18fffac3cb124da2de20e99d542
389 95822f3e891344e11168a4e72aea208e3ac74bce
390 821fcc10b9af55cf17a747845a61840752d08e3c
391 ef14a83ad57b5eccfad37e56d795bc354f0bf4d0
392 802db60af14a500f92d306449cc7a26eb30fe5a3
393 e7e3aea2a556b19ecf925638ff3cb1528a66ba55
397 1d3bb8e54f078824943da977a98cc918d2d0a68b
398 b2d1cb34683bc049813b95b20a88689f02697b31
399 33c787165f49334ab45044ebffcffce90dd43ad4
400 1e0cbd838f51daeb6493966484f6be6ee55ba0af
401 607fee10ddc1df8531183046d7312841001c97fd
142 99802db6a9f1f7af13117fbc753223377660b204
147 d68682442f0c5e6126493ac4bd22e6a52d704bf1
150 f02c0d65d149bb72054cd49c898d05fd279a44e4
386 58e8cd2caf81eb5b743b6cb4b4a5f1486d54f497
402 863d47a9220bfa660d47fb38e31f867b1eaa1e95
403 5b843ff929c4eab7cd0db85397324e14c9b5c9de
407 d4b674a5a6989b3258d485453abad82c8aa5866c
409 42be0460cd7036242c374e307f17fb3d75c17b03
410 63dc7856de623a67fb744e0bf5b61e8721ff7551
411 98f91db7981722ba7ec876fe5c9aa8d97c134cf5
412 777ff5e633e933190d2ae7d550716d07a6650a2d
413 04e3d081df91125235365fa239718329e884dbad
415 0d17e1b00128dda413132cc43434b4c3ca9e3f00
416 e06219c4d4407605b3a533150f68b7b29cafb185
418 0db2b91ff7df40a2ded7370df2cf4e7e15c7d8be
420 47a11e642d920e21b8a7c8583d10c893ce84ff4f
421 fafaf357c9d0a844bb34f75cf784795f1f98932c
424 18615c4bb8c884dc45dfc934d6b30c76edea28d6
425 3a1cd433092e3019f003f0eba78a411ed28ee9d5
426 9369d5e2b5e63775a407b45cfbbc82a804d4aced
427 38351f4291d578c0d80661c882aced8415c3c588
428 259d914c687053a8731bd864ba1b581d2f55c228
429 65e591dc6b5dfc8d8b44e5ee1c0d3d34ec09c44f
430 2e95e8ced1cb3db7d72e262b4676c4432a10f57f
431 101956914acbee0bba696d1179d81b6767636640
432 517da08a25f9d626bb7a73e9e5651bcfc663490f
433 a3ae4e9caa49aab4a73a406e530c245fe877113b
434 dedd3a8e3339fee48534420af0c1c3d575b4e1f1
435 0655d46848c836378a035d8998547e5ab60364ec
436 89467595c8e1009736033c512f150bb7c8832d77
438 a6574601cd328ed54af82bbcc12237f6084dfa99
439 05a60bdb2b0c0e195ec80c923f99be875cd46c1c
440 328b01c48f7f522dba94e87a61cdab38590b9bf4
441 bdab4829c947ddaff726a08a2e8f049d582f7e61
443 8bafb89fc97c0292977403d2471c4c0298c38911
444 fd73e25d7087cb9c9c87ddc45c74da4ea4f35a29
446 14e77dcb206cf9134bc97ea4f12d322435250928
448 493a0a1c6b2b10f7a3e5ccfcbe741d2fb353daea
449 012a408a10f9268df1f1321e61972a019d9f9c01
450 3a8428cf9a6622986ce4953334904ef31d3760d4
451 f6877175693ae57553cae70839cc947f049eec28
452 af2800c3ba3b749c29840e1b1c43d2277f974e8e
453 a3a64f65a21e82a0f4dfc720542b59ba0c8d05ea
454 54960ca5897d3cc98795498b93b8417b9cc0da12
455 b1236d3683b7df6c461ec1abc5809c1dff237647
456 bc28bf3869d7f815876227b5a5d3fc20c3868d07
457 dc355431662a95ba5a73982c34f4b794751cd975
458 4478e891568e78264f544ab8c8568d70ea45c83d
459 78eb978b7463955d2cf976030e58555c553bab36
460 b063bb73d6455e5fd4b9e6e01301164535d93ff0
461 97d0223ae15ef4f99d4fbba4e39b5c14a2410b5d
'''
APPROVED = {int(i):'0x'+a for i,a in (line.split() for line in APPROVED_TEXT.splitlines() if line)}
JOURNAL = DATA / 'archive-approved-20261119.json'
MAX_BNB = 537350000000  # largest BNB dust explicitly reviewed
MAX_USDT = 100000000000000  # 0.0001 USDT, 18 decimals on BSC


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def normalize(rows):
    rows = [list(r) for r in rows]
    for r in rows:
        while r and r[-1] == '': r.pop()
    while rows and not rows[-1]: rows.pop()
    return rows


def address(row):
    return str(row[1]).strip().lower() if len(row)>1 else ''


def save(journal):
    temporary=JOURNAL.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(journal,f); f.flush(); os.fsync(f.fileno())
    os.replace(temporary,JOURNAL)
    fd=os.open(JOURNAL.parent,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


class Sheets:
    def __init__(self,cfg):
        self.cfg=cfg
        self.creds=google_credentials(write=True)
    def request(self,suffix,payload=None):
        if not self.creds.valid: self.creds=google_credentials(write=True)
        url='https://sheets.googleapis.com/v4/spreadsheets/'+self.cfg['sheets_id']+suffix
        req=Request(url,data=None if payload is None else json.dumps(payload).encode(),
                    headers={'Authorization':'Bearer '+self.creds.token,'Content-Type':'application/json'})
        # Never retry a write. Any exception is ambiguous; journal+metadata readback resolve it.
        try:
            with urlopen(req,timeout=60) as r: raw=r.read(8_000_001)
            if len(raw)>8_000_000: raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict): raise ValueError()
            return value
        except Exception:
            raise RemoteError('Google: запрос не подтверждён; секреты и ответ API не выводятся.') from None
    def metadata(self):
        return self.request('?fields=sheets(properties),developerMetadata')
    def rows(self,title,columns,render="FORMULA"):
        label=''
        while columns:
            columns,rem=divmod(columns-1,26); label=chr(65+rem)+label
        rng="'"+title.replace("'","''")+"'!A:"+label
        value=self.request('/values/'+quote(rng,safe='')+'?valueRenderOption='+render)
        if not value.get('range') or not isinstance(value.get('values',[]),list):
            raise RemoteError('Google вернул неполный диапазон.')
        rows=normalize(value.get('values',[]))
        if len(rows)>=5000: raise RemoteError('Больше 4999 заполненных строк; перенос остановлен.')
        return rows


def properties(meta):
    found={s['properties']['title']:s['properties'] for s in meta['sheets']}
    if SOURCE not in found or DEST not in found: raise RemoteError('Не найдены оба листа: рабочий и архивный.')
    return found[SOURCE],found[DEST]


def plan_rows(src,dst,selected):
    mapping={}
    for index,row in enumerate(src):
        a=address(row)
        if a in selected.values():
            if index==0 or a in mapping: raise RemoteError('Дубликат адреса или адрес в заголовке.')
            mapping[a]=index
    if set(mapping)!=set(selected.values()): raise RemoteError('Согласованные адреса отсутствуют в рабочем листе.')
    if any(address(r) in mapping for r in dst): raise RemoteError('Согласованный адрес уже есть в архиве; нужна сверка дубликата.')
    if not dst: raise RemoteError('В архивном листе нет заголовка.')
    return sorted(mapping.values())


def frozen_rows(src,effective,indices):
    result=[]
    for i in indices:
        row=effective[i] if i<len(effective) else []
        if address(row)!=address(src[i]):
            raise RemoteError('Адрес изменился при чтении значений формул.')
        # Only formulas are replaced; literal secrets/text are preserved exactly.
        values=[]
        for col,value in enumerate(src[i]):
            if isinstance(value,str) and value.startswith('='):
                value=row[col] if col<len(row) else ''
            values.append(value)
        result.append(normalize([values])[0] if any(v!='' for v in values) else [])
    return result


def cell_value(value):
    # Typed values prevent a calculated string beginning '=' becoming a formula.
    if value=='': return {}
    if isinstance(value,bool): key='boolValue'
    elif isinstance(value,(int,float)): key='numberValue'
    elif isinstance(value,str): key='stringValue'
    else: raise RemoteError('Неподдерживаемое значение ячейки.')
    return {'userEnteredValue':{key:value}}


def requests_for(srcprop,dstprop,indices,offset,job,frozen):
    source=srcprop['sheetId']; target=dstprop['sheetId']
    width=srcprop['gridProperties']['columnCount']
    # Backups and all moves form ONE atomic Google batch.
    requests=[{'duplicateSheet':{'sourceSheetId':s,'newSheetName':name}}
        for s,name in [(source,'backup-active-'+job[:12]),(target,'backup-archive-'+job[:12])]]
    for dimension,needed,current in [('ROWS',offset+len(indices),dstprop['gridProperties']['rowCount']),
                                     ('COLUMNS',width,dstprop['gridProperties']['columnCount'])]:
        if needed>current: requests.append({'appendDimension':{'sheetId':target,'dimension':dimension,'length':needed-current}})
    for n,index in enumerate(indices):
        requests.append({'copyPaste':{'source':{'sheetId':source,'startRowIndex':index,'endRowIndex':index+1,'startColumnIndex':0,'endColumnIndex':width},
           'destination':{'sheetId':target,'startRowIndex':offset+n,'endRowIndex':offset+n+1,'startColumnIndex':0,'endColumnIndex':width},
           'pasteType':'PASTE_NORMAL'}})
        requests.append({'updateCells':{
            'range':{'sheetId':target,'startRowIndex':offset+n,'endRowIndex':offset+n+1,'startColumnIndex':0,'endColumnIndex':width},
            'rows':[{'values':[cell_value(v) for v in frozen[n]]}],
            'fields':'userEnteredValue'}})
    # Clear values without deleting rows: unrelated formulas retain their references.
    for index in indices:
        requests.append({'updateCells':{
            'range':{'sheetId':source,'startRowIndex':index,'endRowIndex':index+1,'startColumnIndex':0,'endColumnIndex':width},
            'rows':[], 'fields':'userEnteredValue'}})
    requests.append({'createDeveloperMetadata':{'developerMetadata':{
        'metadataKey':'bnb_archive_approved','metadataValue':job,
        'visibility':'DOCUMENT','location':{'spreadsheet':True}}}})
    return requests


def assert_idle(c):
    # All financial operations must be settled before temporarily stopping services.
    for table in ('withdrawals','swaps','binance_jobs','exchange_jobs','usdt_withdrawals'):
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            if c.execute('SELECT 1 FROM '+table+" WHERE state NOT IN ('done','failed','cancelled','preview','expired','closed') LIMIT 1").fetchone():
                raise RemoteError('Есть незавершённая финансовая операция в '+table+'. Перенос остановлен.')


def finish_local(j):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        assert_idle(c)
        c.execute('CREATE TABLE IF NOT EXISTS archived_wallet_records (job TEXT,wallet_id INTEGER,address TEXT,data TEXT,created REAL,PRIMARY KEY(job,wallet_id))')
        for rawid,a in j['selected'].items():
            wid=int(rawid)
            row=c.execute('SELECT * FROM wallets WHERE id=?',(wid,)).fetchone()
            if not row: continue
            if row['address'].lower()!=a: raise RemoteError('Адрес кошелька изменился; удаление из мониторинга остановлено.')
            c.execute('INSERT OR IGNORE INTO archived_wallet_records VALUES(?,?,?,?,?)',(j['job'],wid,a,json.dumps(dict(row)),time.time()))
            c.execute("UPDATE email_outbox SET delivery='cancelled' WHERE delivery='pending' AND event_id IN (SELECT id FROM events WHERE wallet_id=?)",(wid,))
            c.execute("UPDATE events SET delivery='cancelled' WHERE wallet_id=? AND delivery='pending'",(wid,))
            c.execute('DELETE FROM wallets WHERE id=? AND address=?',(wid,row['address']))
        for k,v in {'check_requested':True,'archive_move_report':{'count':len(j['selected']),'completed':time.time(),'job':j['job']}}.items():
            c.execute('INSERT OR REPLACE INTO status VALUES(?,?)',(k,json.dumps(v)))
    j['state']='done'; save(j)
    print('Перенос подтверждён. В архиве и исключены из мониторинга:',len(j['selected']),flush=True)


def reconcile(api,j):
    meta=api.metadata()
    marker=any(m.get('metadataKey')=='bnb_archive_approved' and m.get('metadataValue')==j['job'] for m in meta.get('developerMetadata',[]))
    if not marker: raise RemoteError('Результат отправки не установлен. Повторная запись заблокирована; журнал сохранён.')
    sp,dp=properties(meta)
    src=api.rows(SOURCE,sp['gridProperties']['columnCount']); dst=api.rows(DEST,dp['gridProperties']['columnCount'])
    if digest(src)!=j['source_after_hash'] or digest(dst)!=j['archive_after_hash']:
        raise RemoteError('Содержимое после переноса не совпало с планом. Резервные листы сохранены; повторная запись запрещена.')
    finish_local(j)


def main():
    os.umask(0o077)
    import fcntl
    with (DATA/'archive-verification.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        cfg=settings()
        if not cfg['sheets_enabled'] or cfg['sheets_tab']!=SOURCE:
            raise RemoteError('Нужна включённая синхронизация рабочего листа «bnb кошельки».')
        api=Sheets(cfg)
        if JOURNAL.exists():
            j=json.loads(JOURNAL.read_text())
            if j['spreadsheet']!=cfg['sheets_id']: raise RemoteError('Изменилась Google Таблица.')
            if j['state']=='done':
                print('Этот перенос уже завершён. Повторная запись не выполняется.'); return
            reconcile(api,j); return
        with db() as c:
            assert_idle(c)
            rows=[]
            for wid,a in APPROVED.items():
                r=c.execute('SELECT p.* FROM archive_activity_preview p JOIN wallets w ON w.id=p.wallet_id AND w.address=p.address WHERE w.id=? AND lower(w.address)=?',(wid,a)).fetchone()
                if not r: raise RemoteError('Согласованный кошелёк отсутствует или адрес изменён: #'+str(wid))
                rows.append(dict(r))
        selected={}; checked={}
        for n,row in enumerate(rows,1):
            status,detail,evidence=verify_candidate(row)
            good=(status in ('verified_snapshot','has_balance') and
                0<=int(evidence.get('bnb_wei','-1'))<=MAX_BNB and
                0<=int(evidence.get('usdt_units','-1'))<=MAX_USDT)
            if good:
                selected[row['wallet_id']]=row['address'].lower(); checked[row['wallet_id']]=time.time()
            print(f"Проверено {n}/{len(rows)}: #{row['wallet_id']} — "+('включён' if good else 'пропущен: '+status),flush=True)
        if not selected: raise RemoteError('Нет кошельков, прошедших актуальную проверку. Таблица не изменена.')
        if time.time()-min(checked.values())>3600: raise RemoteError('Проверка заняла более часа; перенос остановлен из-за давности первых результатов.')
        meta=api.metadata(); sp,dp=properties(meta)
        src=api.rows(SOURCE,sp['gridProperties']['columnCount']); dst=api.rows(DEST,dp['gridProperties']['columnCount'])
        indices=plan_rows(src,dst,selected)
        effective=api.rows(SOURCE,sp['gridProperties']['columnCount'],render='UNFORMATTED_VALUE')
        frozen=frozen_rows(src,effective,indices)
        job=uuid.uuid4().hex
        aftersrc=normalize([[] if i in indices else r for i,r in enumerate(src)])
        afterdst=normalize(dst+frozen)
        payload={'requests':requests_for(sp,dp,indices,len(dst),job,frozen)}
        # Last-minute re-read before atomic write. Do not edit Sheets during this job.
        if src!=api.rows(SOURCE,sp['gridProperties']['columnCount']) or dst!=api.rows(DEST,dp['gridProperties']['columnCount']):
            raise RemoteError('Таблица изменена во время подготовки; ничего не отправлено.')
        with db() as c: assert_idle(c)
        j={'job':job,'spreadsheet':cfg['sheets_id'],'state':'submitting','selected':selected,
           'source_after_hash':digest(aftersrc),'archive_after_hash':digest(afterdst)}
        save(j)  # Durable intent BEFORE the one and only write.
        try: api.request(':batchUpdate',payload)
        except RemoteError: print('Ответ Google потерян; сверяем результат без повторной отправки.',flush=True)
        reconcile(api,j)


if __name__=='__main__':
    try: main()
    except Exception as e:
        print('ОСТАНОВЛЕНО:',str(e) if isinstance(e,RemoteError) else type(e).__name__,flush=True)
        raise SystemExit(1)
