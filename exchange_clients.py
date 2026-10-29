"""Gate API v4 / Bybit V5. Direct HTTPS, fixed hosts, no automatic POST retries."""
import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, quote
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
from binance import NoRedirect, fmt

class ExchangeError(ValueError):
    pass

def dec(value):
    try:
        n=Decimal(str(value))
        if not n.is_finite() or n<0 or n>Decimal('1e18'): raise ValueError()
        return n
    except (InvalidOperation,ValueError):
        raise ExchangeError('Биржа вернула неверное числовое значение.') from None

def one(rows):
    if not isinstance(rows, list) or len(rows) != 1:
        raise ExchangeError('Биржа не вернула единственную ожидаемую запись.')
    return rows[0]

def precision(value):
    if not str(value).isdigit() or not 0 <= int(value) <= 18:
        raise ExchangeError('Неизвестна точность биржи.')
    return Decimal(1).scaleb(-int(value))

class Client:
    def __init__(self, venue):
        from app import DATA
        if venue not in ('gate','bybit'):
            raise ExchangeError('Неизвестная биржа.')
        self.venue = venue
        try:
            path = DATA / (venue+'-api.json')
            if path.stat().st_mode & 0o077:
                raise ValueError()
            keys = json.loads(path.read_text())
            self.key, self.secret = keys['api_key'], keys['api_secret']
            if not self.key or not self.secret:
                raise ValueError()
        except Exception:
            raise ExchangeError('Установите ключи: python3 configure_exchange.py '+venue) from None

    def request(self, path, params=None, method='GET', signed=True):
        params = params or {}
        query = urlencode(params) if method == 'GET' else ''
        body = json.dumps(params,separators=(',',':')) if method == 'POST' else ''
        headers = {'Accept':'application/json','Content-Type':'application/json'}
        if self.venue == 'gate':
            base = 'https://api.gateio.ws'
            path = '/api/v4'+path
            stamp = str(int(time.time()))
            if signed:
                message = '\n'.join((method,path,query,hashlib.sha512(body.encode()).hexdigest(),stamp))
                headers.update(KEY=self.key,Timestamp=stamp,SIGN=hmac.new(self.secret.encode(),message.encode(),hashlib.sha512).hexdigest())
        else:
            base = 'https://api.bybit.com'
            stamp = str(int(time.time()*1000))
            if signed:
                message = stamp+self.key+'10000'+(query if method=='GET' else body)
                headers.update({'X-BAPI-API-KEY':self.key,'X-BAPI-TIMESTAMP':stamp,'X-BAPI-RECV-WINDOW':'10000',
                    'X-BAPI-SIGN':hmac.new(self.secret.encode(),message.encode(),hashlib.sha256).hexdigest()})
        req = Request(base+path+('?' + query if query else ''),data=body.encode() if method=='POST' else None,method=method,headers=headers)
        try:
            with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=15) as res:
                raw=res.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:
                raise ExchangeError('Слишком большой ответ биржи.')
            data=json.loads(raw)
            if self.venue=='bybit':
                if data.get('retCode')!=0:
                    code=data.get('retCode')
                    raise ExchangeError('Bybit отклонил запрос: код '+str(code if isinstance(code,int) else 'API'))
                return data['result']
            return data
        except ExchangeError:
            raise
        except HTTPError as e:
            raise ExchangeError('Биржа HTTP %s. Проверьте права, IP, лимиты. Запрос не повторён.' % e.code) from None
        except Exception:
            raise ExchangeError('Нет достоверного ответа биржи. Повторная отправка операции заблокирована.') from None

    def balances(self):
        if self.venue=='gate':
            rows=self.request('/spot/accounts')
            return {r['currency']:fmt(dec(r['available'])) for r in rows if r['currency'] in ('BNB','USDT')}
        data=self.request('/v5/asset/transfer/query-account-coins-balance',{'accountType':'UNIFIED','coin':'BNB,USDT','withBonus':0})
        if data.get('accountType')!='UNIFIED':
            raise ExchangeError('Ожидается единый торговый аккаунт Bybit UNIFIED.')
        # Only own transferable funds; no collateral borrowing or bonus spending.
        return {r['coin']:fmt(min(dec(r['transferBalance']),dec(r['walletBalance']))) for r in data['balance'] if r['coin'] in ('BNB','USDT')}

    def network(self):
        if self.venue=='gate':
            rows=self.request('/wallet/currency_chains',{'currency':'BNB'},signed=False)
            n=one([n for n in rows if n.get('chain')=='BSC'])
            if any(str(n.get(k))!='0' for k in ('is_disabled','is_withdraw_disabled','is_tag')):
                raise ExchangeError('Вывод Gate BNB/BSC недоступен или требует тег.')
            w=one([n for n in self.request('/wallet/withdraw_status',{'currency':'BNB'}) if n.get('currency')=='BNB'])
            fee=dec(w['withdraw_fix_on_chains']['BSC'])
            percent=str(w.get('withdraw_percent_on_chains',{}).get('BSC',w.get('withdraw_percent')))
            rate=dec(percent.rstrip('%'))/(100 if percent.endswith('%') else 1)
            minimum=dec(w['withdraw_amount_mini'])
            maximum=min(dec(w['withdraw_eachtime_limit']),dec(w['withdraw_day_limit_remain']))
            step=precision(n['decimal'])
        else:
            coin=one([r for r in self.request('/v5/asset/coin/query-info',{'coin':'BNB'})['rows'] if r.get('coin')=='BNB'])
            n=one([r for r in coin['chains'] if r.get('chain')=='BSC'])
            if n.get('chainWithdraw')!='1':
                raise ExchangeError('Вывод Bybit BNB/BSC приостановлен.')
            fee=dec(n['withdrawFee']); rate=dec(n['withdrawPercentageFee'] or '0')
            minimum=dec(n['withdrawMin']); maximum=dec(n['withdrawMax'])
            step=precision(n['minAccuracy'])
        # Percentage fees need a separate reviewed calculation; never guess.
        if rate:
            raise ExchangeError('Биржа вернула процентную комиссию вывода. Используйте её интерфейс.')
        return dict(network='BSC',fee=fmt(fee),minimum=fmt(minimum),maximum=fmt(maximum),step=fmt(step))

    def address_check(self,address):
        if self.venue!='bybit':
            return
        cursor=''; seen=set()
        for _ in range(100):
            p={'chain':'BSC','addressType':0,'limit':50}
            if cursor:p['cursor']=cursor
            data=self.request('/v5/asset/withdraw/query-address',p)
            for r in data['rows']:
                if r.get('address')==address and r.get('coin') in ('BNB','baseCoin') and r.get('chain')=='BSC' and not r.get('tag'):
                    if r.get('status')==0 and r.get('verified')==1:
                        return
                    raise ExchangeError('Адрес Bybit ещё не подтверждён или действует 24-часовое ограничение.')
            cursor=data.get('nextPageCursor')
            if not cursor:break
            if cursor in seen:raise ExchangeError('Ошибка пагинации адресов Bybit.')
            seen.add(cursor)
        raise ExchangeError('Сначала добавьте и подтвердите адрес BNB/BSC в белом списке Bybit. Регистр букв должен совпадать.')

    def market(self):
        if self.venue=='gate':
            m=self.request('/spot/currency_pairs/BNB_USDT',signed=False)
            if m.get('id')!='BNB_USDT' or m.get('trade_status')!='tradable':
                raise ExchangeError('Торговля BNB/USDT недоступна.')
            price=dec(one(self.request('/spot/tickers',{'currency_pair':'BNB_USDT'},signed=False))['last'])
            fee=dec(self.request('/wallet/fee',{'currency_pair':'BNB_USDT'})['taker_fee'])
            return dict(price=fmt(price),fee=fmt(fee),base_step=fmt(precision(m['amount_precision'])),quote_step='0.01',
                minimum=str(m['min_quote_amount']),max_quote=str(m['market_order_max_money']),max_base=str(m['market_order_max_stock']))
        m=one(self.request('/v5/market/instruments-info',{'category':'spot','symbol':'BNBUSDT'},signed=False)['list'])
        if m.get('symbol')!='BNBUSDT' or m.get('status')!='Trading':
            raise ExchangeError('Торговля BNB/USDT недоступна.')
        lot=m['lotSizeFilter']
        price=dec(one(self.request('/v5/market/tickers',{'category':'spot','symbol':'BNBUSDT'},signed=False)['list'])['lastPrice'])
        fee=dec(one(self.request('/v5/account/fee-rate',{'category':'spot','symbol':'BNBUSDT'})['list'])['takerFeeRate'])
        return dict(price=fmt(price),fee=fmt(fee),base_step=fmt(dec(lot['basePrecision'])),quote_step=fmt(dec(lot['quotePrecision'])),
            minimum=str(lot['minOrderAmt']),max_quote=fmt(dec(lot['maxMarketOrderQty'])*price),max_base=str(lot['maxMarketOrderQty']))

    def order(self,ident,side,amount):
        if self.venue=='gate':
            r=self.request('/spot/orders',{'text':'t-'+ident,'currency_pair':'BNB_USDT','type':'market','account':'spot',
                'side':side,'amount':amount,'time_in_force':'ioc','auto_borrow':False},'POST')
            return str(r['id'])
        r=self.request('/v5/order/create',{'category':'spot','symbol':'BNBUSDT','side':side.title(),'orderType':'Market',
            'qty':amount,'marketUnit':'quoteCoin' if side=='buy' else 'baseCoin','isLeverage':0,'orderLinkId':ident},'POST')
        return str(r['orderId'])

    def order_status(self,ident,side,order_id=None):
        if self.venue=='gate':
            r=self.request('/spot/orders/'+quote(order_id or 't-'+ident,safe=''),{'currency_pair':'BNB_USDT','account':'spot'})
            if r.get('text')!='t-'+ident or r.get('currency_pair')!='BNB_USDT' or r.get('side')!=side or r.get('type')!='market':
                raise ExchangeError('Ордер Gate не совпал с запросом.')
            terminal=r['status'] in ('closed','cancelled')
            qty=dec(r['filled_amount']); cost=dec(r['filled_total'])
            fee=dec(r['fee']); asset=r['fee_currency']
            return dict(id=str(r['id']),terminal=terminal,quantity=fmt(qty),quote=fmt(cost),fees={asset:fmt(fee)},status=r['status'])
        params={'category':'spot','symbol':'BNBUSDT','orderLinkId':ident}
        rows=self.request('/v5/order/history',params)['list']
        r=one(rows)
        if r.get('orderLinkId')!=ident or r.get('symbol')!='BNBUSDT' or r.get('side')!=side.title() or r.get('orderType')!='Market':
            raise ExchangeError('Ордер Bybit не совпал с запросом.')
        terminal=r['orderStatus'] in ('Filled','Cancelled','Rejected','PartiallyFilledCanceled','Deactivated')
        qty=dec(r['cumExecQty']); fees={}
        if terminal and qty:
            cursor=''; seen=set(); total=Decimal(0); ids=set()
            for _ in range(100):
                p={'category':'spot','symbol':'BNBUSDT','orderId':r['orderId'],'limit':100}
                if cursor:p['cursor']=cursor
                page=self.request('/v5/execution/list',p)
                for t in page['list']:
                    if t['orderId']!=r['orderId'] or t['execId'] in ids:
                        raise ExchangeError('Неверные исполнения Bybit.')
                    ids.add(t['execId']); total+=dec(t['execQty'])
                    asset=t.get('feeCurrency')
                    if not asset:raise ExchangeError('Bybit не сообщил валюту комиссии исполнения.')
                    fees[asset]=fmt(dec(fees.get(asset,0))+dec(t['execFee']))
                cursor=page.get('nextPageCursor')
                if not cursor:break
                if cursor in seen:raise ExchangeError('Ошибка пагинации исполнений.')
                seen.add(cursor)
            if total!=qty or cursor:raise ExchangeError('Получены не все исполнения Bybit.')
        return dict(id=str(r['orderId']),terminal=terminal,quantity=fmt(qty),quote=fmt(dec(r['cumExecValue'])),fees=fees,status=r['orderStatus'])

    def withdraw(self,ident,address,amount):
        if self.venue=='gate':
            r=self.request('/withdrawals',{'withdraw_order_id':ident,'currency':'BNB','address':address,'amount':amount,'chain':'BSC'},'POST')
            return str(r['id'])
        r=self.request('/v5/asset/withdraw/create',{'requestId':ident,'coin':'BNB','chain':'BSC','address':address,'amount':amount,
            'timestamp':int(time.time()*1000),'forceChain':1,'accountType':'UTA','feeType':0},'POST')
        return str(r['id'])

    def history(self,ident=None,withdraw_id=None):
        if self.venue=='gate':
            p={'currency':'BNB','limit':100}
            if ident:p['withdraw_order_id']=ident
            if withdraw_id:p['withdraw_id']=withdraw_id
            rows=self.request('/wallet/withdrawals',p)
            return [dict(id=str(r['id']),request_id=r.get('withdraw_order_id',''),coin=r['currency'],network=r['chain'],
                address=r['address'],amount=str(r['amount']),status=r['status'],txid=r.get('txid',''),
                done=r['status']=='DONE',failed=r['status'] in ('CANCEL','INVALID')) for r in rows]
        # Bybit history does NOT return requestId. A lost create response stays
        # uncertain until the user supplies the ID from Bybit for strict matching.
        p={'coin':'BNB','withdrawType':0,'limit':50}
        if withdraw_id:p['withdrawID']=withdraw_id
        rows=self.request('/v5/asset/withdraw/query-record',p)['rows']
        return [dict(id=str(r['withdrawId']),coin=r['coin'],network=r['chain'],address=r['toAddress'],amount=r['amount'],
            status=r['status'],txid=r.get('txID',''),created=r['createTime'],done=r['status']=='success',
            failed=r['status'] in ('CancelByUser','Reject','Fail')) for r in rows]

# Schema drift must produce a readable failure, never a guessed balance/fee.
def validated(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args,**kwargs):
        try:return fn(*args,**kwargs)
        except (KeyError,TypeError,IndexError):
            raise ExchangeError('Биржа вернула неполные данные ('+fn.__name__+'). Операция остановлена.') from None
    return wrapped
for _name in ('balances','network','market','address_check','order_status','history'):
    setattr(Client,_name,validated(getattr(Client,_name)))
