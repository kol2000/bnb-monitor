"""Cached CMC page quote; never used for on-chain notification decisions."""
import json
from datetime import datetime
from decimal import Decimal, localcontext
from html.parser import HTMLParser
from urllib.request import Request, urlopen

SOURCE = 'https://coinmarketcap.com/currencies/bnb/'

class PageData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = dict(attrs).get('id') == '__NEXT_DATA__'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

def parse_quote(html):
    parser = PageData()
    parser.feed(html)
    data = json.loads(''.join(parser.parts), parse_float=Decimal)
    detail = data['props']['pageProps']['detailRes']['detail']
    if detail['id'] != 1839 or detail['symbol'] != 'BNB':
        raise ValueError('Wrong asset')
    price = Decimal(str(detail['statistics']['price']))
    if not price.is_finite() or price <= 0 or price > 10**9:
        raise ValueError('Invalid price')
    timestamp = datetime.fromisoformat(detail['latestUpdateTime'].replace('Z', '+00:00')).timestamp()
    return str(price), timestamp

def fetch_quote():
    req = Request(SOURCE, headers={'User-Agent': 'BNB-Monitor/1.2', 'Accept': 'text/html', 'Accept-Language': 'en-US'})
    with urlopen(req, timeout=20) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError('Page too large')
    return parse_quote(raw.decode('utf-8'))

def usd(wei, price):
    if wei is None or price is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 110
        value = Decimal(int(wei)) * Decimal(price) / Decimal(10**18)
        if 0 < value < Decimal('0.01'):
            return '< $0.01'
        return '$' + format(value, ',.2f')
